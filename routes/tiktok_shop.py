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

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel

from auth import verificar_api_key
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


def _publicar_lote_bg(skus: Optional[List[str]], embalagem: float, imposto: float, markup: float):
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
        token = client.access_token
        hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        BASE = "https://api.bling.com.br/Api/v3"

        # 1. Listar todos os produtos ativos (paginando)
        ids_para_processar = []
        pagina = 1
        while True:
            params = {"pagina": pagina, "limite": 100, "situacao": "A"}
            r = _req.get(f"{BASE}/produtos", params=params, headers=hdrs, timeout=30)
            data = r.json().get("data", [])
            for p in data:
                if skus and p.get("codigo") not in skus:
                    continue
                ids_para_processar.append({"id": p["id"], "sku": p.get("codigo"), "nome": p.get("nome", "")})
            if len(data) < 100:
                break
            pagina += 1
            time.sleep(0.3)

        _job["total"] = len(ids_para_processar)
        logger.info(f"TikTok lote: {len(ids_para_processar)} produtos a precificar")

        for item in ids_para_processar:
            prod_id = item["id"]
            sku = item["sku"]

            try:
                # Busca detalhes completos (fornecedores + estoque + estrutura)
                r_det = _req.get(f"{BASE}/produtos/{prod_id}", headers=hdrs, timeout=20)
                prod_det = r_det.json().get("data", {})

                custo = _extrair_custo_bling(prod_det, hdrs, _req)

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
        body.skus, body.embalagem, body.imposto, body.markup,
    )
    return {
        "ok": True,
        "loja_id": tiktok.TIKTOK_LOJA_ID,
        "formula": f"(custo + {body.embalagem}) * {1 + body.imposto/100:.4f} * {body.markup}",
        "mensagem": "Publicação em lote iniciada. Consulte GET /tiktok/anuncios/status",
    }


@router.get("/anuncios/status")
def status_lote(_=Depends(verificar_api_key)):
    return _job


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
