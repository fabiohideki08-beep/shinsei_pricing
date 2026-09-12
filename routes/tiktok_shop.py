"""
TikTok Shop — rotas FastAPI (via Bling Shinsei)
Prefix: /tiktok

idLoja: 206293681 | tipoIntegracao: "TikTok Shop"

Endpoints:
  GET  /tiktok/status                — estado da integração
  GET  /tiktok/anuncios              — listar anúncios TikTok no Bling
  GET  /tiktok/anuncios/{id}         — detalhe de um anúncio
  POST /tiktok/anuncios              — publicar produto no TikTok Shop
  PUT  /tiktok/anuncios/{id}/preco   — atualizar preço de anúncio
  POST /tiktok/anuncios/lote         — publicar vários produtos em lote
  GET  /tiktok/pedidos               — listar pedidos TikTok
  GET  /tiktok/anuncios/status       — status do job de publicação em lote
"""
import logging
import time
import threading
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, Body
from pydantic import BaseModel

from auth import api_key_dep as verificar_api_key
import services.tiktok_shop as tiktok

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tiktok", tags=["TikTok Shop"])


# ── Estado do job de publicação em lote ──────────────────────────────────
_job: dict = {
    "rodando": False, "concluido": False, "erro": None,
    "total": 0, "processados": 0, "publicados": 0, "erros_n": 0,
    "iniciado_em": None, "concluido_em": None, "erros_lista": [],
}
_job_lock = threading.Lock()


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.get("/status")
def tiktok_status():
    return tiktok.status()


@router.get("/anuncios")
def listar_anuncios(
    pagina: int = 1,
    limite: int = 100,
    todos: bool = False,
    _=Depends(verificar_api_key),
):
    try:
        if todos:
            data = tiktok.listar_todos_anuncios()
            return {"total": len(data), "data": data}
        return tiktok.listar_anuncios(pagina=pagina, limite=limite)
    except Exception as e:
        raise HTTPException(500, str(e))


# ATENÇÃO: rotas estáticas DEVEM vir ANTES de /anuncios/{anuncio_id}

# Estado do job de preview (separado do job de publicação)
_preview_job: dict = {
    "rodando": False, "concluido": False, "erro": None,
    "total_ativos": 0, "processados": 0, "produtos": [],
    "iniciado_em": None, "concluido_em": None,
}
_preview_lock = threading.Lock()


def _preview_lote_bg(limite: int, dias_vendas: int):
    """
    Roda em background: rankeia por liquidez, resolve custo via 2 estratégias:
    1. precoCusto da listagem (campo direto) — sem calls individuais
    2. Para custo=0 na lista: GET /produtos/{id} com 3 camadas (composição > fornecedor > estoque)
    Ranking: detalhe dos últimos 3 páginas de pedidos (itens estão só no detalhe, não na lista).
    """
    global _preview_job
    with _preview_lock:
        _preview_job.update({
            "rodando": True, "concluido": False, "erro": None,
            "total_ativos": 0, "processados": 0, "produtos": [],
            "iniciado_em": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

    try:
        from bling_client import BlingClient
        client = BlingClient()

        # 1. Listar todos os produtos ativos — precoCusto JÁ VEM na listagem
        produtos_raw = []
        pagina = 1
        while True:
            resp = client._get("/produtos", {"pagina": pagina, "limite": 100, "situacao": "A"})
            data = resp.get("data", []) if isinstance(resp, dict) else []
            produtos_raw.extend(data)
            if len(data) < 100:
                break
            pagina += 1
            time.sleep(0.2)

        _preview_job["total_ativos"] = len(produtos_raw)

        # 2. Ranking de vendas — busca detalhe dos pedidos (itens só vêm no detalhe)
        from datetime import datetime, timedelta
        data_ini = (datetime.now() - timedelta(days=dias_vendas)).strftime("%Y-%m-%d")
        ranking: dict = {}
        for pag in range(1, 4):  # 3 páginas = até 300 pedidos recentes
            try:
                r = client._get("/pedidos/vendas", {"pagina": pag, "limite": 100, "dataInicio": data_ini})
                pedidos = r.get("data", []) if isinstance(r, dict) else []
                if not pedidos:
                    break
                for p in pedidos:
                    try:
                        det = client._get(f"/pedidos/vendas/{p['id']}")
                        itens = (det.get("data") or {}).get("itens") or []
                        for item in itens:
                            pid = (item.get("produto") or {}).get("id")
                            qtd = float(item.get("quantidade") or 1)
                            if pid:
                                ranking[pid] = ranking.get(pid, 0) + qtd
                        time.sleep(0.1)
                    except Exception:
                        pass
                if len(pedidos) < 100:
                    break
                time.sleep(0.2)
            except Exception:
                break

        # 3. Filtrar produtos com custo direto na listagem (precoCusto > 0)
        #    Produtos sem custo na lista (kits virtuais, etc.) recebem custo via detalhe
        produtos_com_custo = []
        sem_custo_lista = []
        for prod in produtos_raw:
            custo_lista = float(prod.get("precoCusto") or 0)
            if custo_lista > 0:
                produtos_com_custo.append({**prod, "_custo_direto": custo_lista})
            else:
                sem_custo_lista.append(prod)

        logger.info(f"TikTok preview: {len(produtos_com_custo)} com custo na lista, "
                    f"{len(sem_custo_lista)} sem custo na lista")

        # 4. Para produtos sem custo na lista, tentar detalhe (3 camadas) até completar o limite
        #    Limita a 200 calls de detalhe extra para não extrapolar o tempo
        MAX_DETALHE_EXTRA = 200
        detalhe_feitos = 0
        for prod in sem_custo_lista:
            if len(produtos_com_custo) >= limite * 3:  # já temos candidatos suficientes
                break
            if detalhe_feitos >= MAX_DETALHE_EXTRA:
                break
            try:
                det = client._get(f"/produtos/{prod['id']}")
                prod_det = det.get("data", {}) if isinstance(det, dict) else {}
                custo = _extrair_custo_bling_client(prod_det, client)
                if custo > 0:
                    produtos_com_custo.append({**prod, "_custo_direto": custo})
                detalhe_feitos += 1
                time.sleep(0.15)
            except Exception:
                detalhe_feitos += 1

        # 5. Ordenar por vendas (liquidez) e cortar no limite
        produtos_com_custo.sort(key=lambda p: ranking.get(p["id"], 0), reverse=True)
        selecionados = produtos_com_custo[:limite]

        embalagem, imposto, markup = 0.50, 4.0, 1.33
        resultado = []

        for pos, prod in enumerate(selecionados, start=1):
            prod_id = prod["id"]
            sku = prod.get("codigo", "")
            nome = prod.get("nome", "")
            qtd_vendida = ranking.get(prod_id, 0)
            custo = float(prod.get("_custo_direto", 0))
            preco_tiktok = round((custo + embalagem) * (1 + imposto / 100) * markup, 2) if custo > 0 else None
            obs = "" if custo > 0 else "sem custo"

            resultado.append({
                "pos": pos, "sku": sku, "nome": nome,
                "qtd_vendida_90d": int(qtd_vendida),
                "custo": round(custo, 2),
                "preco_tiktok": preco_tiktok,
                "obs": obs,
            })
            _preview_job["processados"] = pos

        _preview_job.update({
            "rodando": False, "concluido": True,
            "concluido_em": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "produtos": resultado,
            "com_custo_lista": len(produtos_com_custo),
            "sem_custo": sum(1 for r in resultado if not r["preco_tiktok"]),
            "possiveis_descontinuados": 0,
            "formula": f"(custo + {embalagem}) * {1 + imposto/100:.4f} * {markup}",
        })
        logger.info(f"TikTok preview-lote concluído: {len(resultado)} produtos")

    except Exception as e:
        _preview_job.update({"rodando": False, "erro": str(e)})
        logger.error(f"TikTok preview-lote erro: {e}")


def _extrair_custo_bling_client(prod_det: dict, client) -> float:
    """Resolve custo via BlingClient nas 3 camadas: composição > fornecedor > estoque."""
    # Camada 1: composição (kit) — estrutura pode ser dict ou string "P"/"K"
    estrutura = prod_det.get("estrutura") or {}
    if not isinstance(estrutura, dict):
        estrutura = {}
    componentes = estrutura.get("componentes") or []
    if isinstance(componentes, list) and componentes:
        total = 0.0
        for comp in componentes:
            if not isinstance(comp, dict):
                continue
            custo_unit = float(comp.get("precoCusto") or 0)
            qtde = float(comp.get("quantidade") or 1)
            prod_comp = comp.get("produto") or {}
            if custo_unit == 0 and isinstance(prod_comp, dict) and prod_comp.get("id"):
                try:
                    det2 = client._get(f"/produtos/{prod_comp['id']}")
                    custo_unit = _extrair_custo_simples_client(det2.get("data", {}))
                except Exception:
                    pass
            total += custo_unit * qtde
        if total > 0:
            return total
    return _extrair_custo_simples_client(prod_det)


def _extrair_custo_simples_client(prod: dict) -> float:
    """Camadas 2 e 3: fornecedor padrão e estoque/NF."""
    if not isinstance(prod, dict):
        return 0.0
    fornecedores = prod.get("fornecedores") or []
    if not isinstance(fornecedores, list):
        fornecedores = []
    # Fornecedor padrão primeiro
    for forn in fornecedores:
        if not isinstance(forn, dict):
            continue
        if forn.get("padrao") or forn.get("padrão"):
            c = float(forn.get("precoCusto") or 0)
            if c > 0:
                return c
    # Qualquer fornecedor com custo
    for forn in fornecedores:
        if not isinstance(forn, dict):
            continue
        c = float(forn.get("precoCusto") or 0)
        if c > 0:
            return c
    # Estoque (última NF)
    estoque = prod.get("estoque") or {}
    if isinstance(estoque, dict):
        c = float(estoque.get("precoCusto") or estoque.get("precoCompra") or 0)
        if c > 0:
            return c
    # Campos raiz
    return float(prod.get("precoCusto") or prod.get("precoCompra") or 0)


@router.get("/anuncios/status")
def status_lote(_=Depends(verificar_api_key)):
    return _job


@router.get("/anuncios/preview-lote")
def preview_lote_status(_=Depends(verificar_api_key)):
    """Retorna o estado atual do job de preview. Use POST /tiktok/anuncios/preview-lote para iniciar."""
    return _preview_job


@router.post("/anuncios/preview-lote")
def iniciar_preview_lote(
    limite: int = Query(300),
    dias_vendas: int = Query(90),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    _=Depends(verificar_api_key),
):
    """Inicia preview em background. Consulte GET /tiktok/anuncios/preview-lote para resultados."""
    if _preview_job["rodando"]:
        raise HTTPException(409, "Preview já está rodando")
    background_tasks.add_task(_preview_lote_bg, limite, dias_vendas)
    return {"ok": True, "mensagem": f"Preview iniciado (top {limite}, últimos {dias_vendas}d). Consulte GET /tiktok/anuncios/preview-lote"}


@router.get("/anuncios/{anuncio_id}")
def detalhe_anuncio(anuncio_id: int, _=Depends(verificar_api_key)):
    try:
        return tiktok.buscar_anuncio(anuncio_id)
    except Exception as e:
        raise HTTPException(500, str(e))


class PublicarRequest(BaseModel):
    produto_id: int
    preco: float
    titulo: Optional[str] = None


@router.post("/anuncios")
def publicar_anuncio(body: PublicarRequest, _=Depends(verificar_api_key)):
    try:
        return tiktok.criar_anuncio(body.produto_id, body.preco, body.titulo)
    except Exception as e:
        raise HTTPException(500, str(e))


class AtualizarPrecoRequest(BaseModel):
    preco: float


@router.put("/anuncios/{anuncio_id}/preco")
def atualizar_preco(anuncio_id: int, body: AtualizarPrecoRequest, _=Depends(verificar_api_key)):
    try:
        return tiktok.atualizar_preco_anuncio(anuncio_id, body.preco)
    except Exception as e:
        raise HTTPException(500, str(e))


class LoteRequest(BaseModel):
    skus: Optional[List[str]] = None   # Se vazio, processa todos os produtos ativos
    embalagem: float = 0.50            # Custo embalagem por produto (R$)
    imposto: float = 4.0               # Imposto % (ex: 4.0 = 4%)
    markup: float = 1.33               # Multiplicador de markup (ex: 1.33 = 33%)
    limite: int = 300                  # Limite de anúncios (TikTok BR: 300 para contas novas)


def _extrair_custo_bling(prod_detail: dict, hdrs: dict, req) -> float:
    """
    3 camadas de custo (ordem de prioridade):
    1. Composição (kit): soma precoCusto * qtde de cada componente
    2. Fornecedor padrão: fornecedores[padrão=true].precoCusto
    3. Estoque (última NF): estoque.precoCusto
    """
    BASE = "https://api.bling.com.br/Api/v3"

    # Camada 3: composição — estrutura do produto
    estrutura = prod_detail.get("estrutura") or {}
    componentes = estrutura.get("componentes") or []
    if componentes:
        total = 0.0
        for comp in componentes:
            custo_unit = float(comp.get("precoCusto") or 0)
            qtde = float(comp.get("quantidade") or 1)
            # Se o componente não tem custo direto, busca pelo id
            if custo_unit == 0 and comp.get("produto", {}).get("id"):
                try:
                    r2 = req.get(f"{BASE}/produtos/{comp['produto']['id']}", headers=hdrs, timeout=20)
                    comp_det = r2.json().get("data", {})
                    custo_unit = _extrair_custo_simples(comp_det, hdrs, req)
                except Exception:
                    pass
            total += custo_unit * qtde
        if total > 0:
            return total

    return _extrair_custo_simples(prod_detail, hdrs, req)


def _extrair_custo_simples(prod: dict, hdrs: dict, req) -> float:
    """Camadas 1 e 2 para produto simples."""
    # Camada 2: fornecedor padrão
    for forn in (prod.get("fornecedores") or []):
        if forn.get("padrao") or forn.get("padrão"):
            custo = float(forn.get("precoCusto") or 0)
            if custo > 0:
                return custo
    # Qualquer fornecedor com custo preenchido
    for forn in (prod.get("fornecedores") or []):
        custo = float(forn.get("precoCusto") or 0)
        if custo > 0:
            return custo

    # Camada 1: estoque.precoCusto (última NF entrada)
    estoque = prod.get("estoque") or {}
    custo = float(estoque.get("precoCusto") or estoque.get("precoCompra") or 0)
    if custo > 0:
        return custo

    # Fallback: campos raiz
    return float(prod.get("precoCusto") or prod.get("precoCompra") or 0)


def _ranking_vendas_bling(hdrs: dict, req, dias: int = 90) -> dict:
    """Retorna dict {produto_id: qtd_vendida} com ranking dos últimos N dias."""
    from datetime import datetime, timedelta
    BASE = "https://api.bling.com.br/Api/v3"
    data_ini = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")
    ranking: dict = {}
    pagina = 1
    while pagina <= 20:  # max 2000 pedidos
        try:
            r = req.get(
                f"{BASE}/pedidos/vendas",
                params={"pagina": pagina, "limite": 100, "dataInicio": data_ini},
                headers=hdrs, timeout=30,
            )
            pedidos = r.json().get("data", [])
            if not pedidos:
                break
            for p in pedidos:
                for item in (p.get("itens") or []):
                    pid = (item.get("produto") or {}).get("id")
                    qtd = float(item.get("quantidade") or 1)
                    if pid:
                        ranking[pid] = ranking.get(pid, 0) + qtd
            if len(pedidos) < 100:
                break
            pagina += 1
            time.sleep(0.25)
        except Exception:
            break
    return ranking


def _publicar_lote_bg(skus: Optional[List[str]], embalagem: float, imposto: float, markup: float, limite: int):
    """
    Precifica e publica produtos no TikTok Shop via Bling.
    Fórmula: preco_venda = (custo + embalagem) * (1 + imposto/100) * markup
    Custo: 3 camadas — composição > fornecedor padrão > estoque/NF
    """
    global _job
    import requests as _req

    with _job_lock:
        _job.update({
            "rodando": True, "concluido": False, "erro": None,
            "total": 0, "processados": 0, "publicados": 0, "erros_n": 0,
            "sem_custo": 0, "erros_lista": [],
            "iniciado_em": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "formula": f"(custo + {embalagem}) * {1 + imposto/100:.4f} * {markup}",
        })

    try:
        from bling_client import BlingClient
        client = BlingClient()

        # 1. Listar todos os produtos ativos via BlingClient
        ids_para_processar = []
        pagina = 1
        while True:
            resp = client._get("/produtos", {"pagina": pagina, "limite": 100, "situacao": "A"})
            data = resp.get("data", [])
            for p in data:
                if skus and p.get("codigo") not in skus:
                    continue
                ids_para_processar.append({"id": p["id"], "sku": p.get("codigo"), "nome": p.get("nome", "")})
            if len(data) < 100:
                break
            pagina += 1
            time.sleep(0.3)

        # 2. Ranking de liquidez (vendas últimos 90 dias) via BlingClient
        logger.info("TikTok lote: buscando ranking de vendas para ordenar por liquidez...")
        from datetime import datetime, timedelta
        data_ini = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        ranking: dict = {}
        for pag in range(1, 21):
            try:
                r = client._get("/pedidos/vendas", {"pagina": pag, "limite": 100, "dataInicio": data_ini})
                pedidos = r.get("data", [])
                if not pedidos:
                    break
                for p in pedidos:
                    for item in (p.get("itens") or []):
                        pid = (item.get("produto") or {}).get("id")
                        if pid:
                            ranking[pid] = ranking.get(pid, 0) + float(item.get("quantidade") or 1)
                if len(pedidos) < 100:
                    break
                time.sleep(0.25)
            except Exception:
                break

        ids_para_processar.sort(key=lambda x: ranking.get(x["id"], 0), reverse=True)
        if len(ids_para_processar) > limite:
            logger.info(f"TikTok lote: limitando {len(ids_para_processar)} → {limite} mais líquidos")
            ids_para_processar = ids_para_processar[:limite]

        _job["total"] = len(ids_para_processar)
        _job["limite_aplicado"] = limite
        logger.info(f"TikTok lote: {len(ids_para_processar)} produtos a precificar (top {limite} por liquidez)")

        for item in ids_para_processar:
            prod_id = item["id"]
            sku = item["sku"]

            try:
                # Busca detalhes completos via BlingClient (fornecedores + estoque + estrutura)
                det = client._get(f"/produtos/{prod_id}")
                prod_det = det.get("data", {})

                custo = _extrair_custo_bling_client(prod_det, client)

                if custo <= 0:
                    _job["sem_custo"] += 1
                    _job["erros_lista"].append({"sku": sku, "erro": "sem custo no Bling"})
                    _job["processados"] += 1
                    time.sleep(0.2)
                    continue

                # Fórmula TikTok: (custo + embalagem) * (1 + imposto%) * markup
                preco_tiktok = round((custo + embalagem) * (1 + imposto / 100) * markup, 2)

                tiktok.criar_anuncio(prod_id, preco_tiktok)
                _job["publicados"] += 1
                logger.debug(f"TikTok [{sku}] custo={custo:.2f} → R${preco_tiktok:.2f}")

            except Exception as e:
                _job["erros_n"] += 1
                _job["erros_lista"].append({"sku": sku, "erro": str(e)})

            _job["processados"] += 1
            time.sleep(0.4)

        _job.update({
            "rodando": False, "concluido": True,
            "concluido_em": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        logger.info(
            f"TikTok lote concluído: {_job['publicados']} publicados, "
            f"{_job['sem_custo']} sem custo, {_job['erros_n']} erros"
        )

    except Exception as e:
        _job.update({"rodando": False, "erro": str(e)})
        logger.error(f"TikTok lote erro fatal: {e}")


@router.post("/anuncios/lote")
def publicar_lote(
    body: LoteRequest,
    background_tasks: BackgroundTasks,
    _=Depends(verificar_api_key),
):
    if _job["rodando"]:
        raise HTTPException(409, "Job de publicação em lote já está rodando")
    background_tasks.add_task(
        _publicar_lote_bg,
        body.skus, body.embalagem, body.imposto, body.markup, body.limite,
    )
    return {
        "ok": True,
        "loja_id": tiktok.TIKTOK_LOJA_ID,
        "formula": f"(custo + {body.embalagem}) * {1 + body.imposto/100:.4f} * {body.markup}",
        "mensagem": "Publicação em lote iniciada. Consulte GET /tiktok/anuncios/status",
    }


@router.get("/pedidos")
def listar_pedidos(
    pagina: int = 1,
    limite: int = 100,
    situacao: Optional[int] = None,
    _=Depends(verificar_api_key),
):
    try:
        return tiktok.listar_pedidos_tiktok(pagina=pagina, limite=limite, situacao=situacao)
    except Exception as e:
        raise HTTPException(500, str(e))
