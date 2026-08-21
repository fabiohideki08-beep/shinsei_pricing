# -*- coding: utf-8 -*-
"""
routes/shopee.py — Shinsei Pricing
Endpoints para OAuth, status e atualização de preços na Shopee.
"""
from __future__ import annotations

import difflib
import json
import logging
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel

from services.shopee import (
    ShopeeOAuthService,
    ShopeeService,
    tem_tokens,
    token_expirado,
    _carregar_tokens,
    _config_ok,
)

logger = logging.getLogger(__name__)
router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
PAGES_DIR = BASE_DIR / "pages"


# ─────────────────────────────────────────────
# Página de configuração
# ─────────────────────────────────────────────

@router.get("/shopee", response_class=HTMLResponse)
def shopee_page():
    html_file = PAGES_DIR / "shopee.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="pages/shopee.html não encontrado.")


# ─────────────────────────────────────────────
# Status
# ─────────────────────────────────────────────

@router.get("/shopee/status")
def shopee_status():
    return ShopeeOAuthService().status()


# ─────────────────────────────────────────────
# OAuth — início
# ─────────────────────────────────────────────

@router.get("/shopee/auth")
def shopee_auth(request: Request):
    """Redireciona para a página de autorização da Shopee."""
    if not _config_ok():
        raise HTTPException(
            status_code=400,
            detail="Credenciais Shopee não configuradas. Adicione SHOPEE_PARTNER_ID, "
                   "SHOPEE_PARTNER_KEY e SHOPEE_SHOP_ID no .env.",
        )
    redirect_uri = str(request.base_url).rstrip("/") + "/shopee/callback"
    url = ShopeeOAuthService().url_autorizacao(redirect_uri)
    if not url:
        raise HTTPException(status_code=500, detail="Falha ao gerar URL de autorização da Shopee.")
    return RedirectResponse(url)


# ─────────────────────────────────────────────
# OAuth — callback
# ─────────────────────────────────────────────

@router.get("/shopee/callback")
def shopee_callback(request: Request):
    """
    Shopee redireciona aqui após autorização com ?code=...&shop_id=...
    Troca o code por access_token + refresh_token.
    """
    code = request.query_params.get("code")
    shop_id_str = request.query_params.get("shop_id")
    error = request.query_params.get("error")

    if error:
        raise HTTPException(status_code=400, detail=f"Shopee retornou erro: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Callback sem 'code'. Refaça a autorização.")
    if not shop_id_str:
        raise HTTPException(status_code=400, detail="Callback sem 'shop_id'.")

    try:
        shop_id = int(shop_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"shop_id inválido: {shop_id_str}")

    result = ShopeeOAuthService().trocar_code(code, shop_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "Falha ao trocar code."))

    return {
        "success": True,
        "message": "Shopee conectada com sucesso! Tokens salvos.",
        "data": {
            "shop_id": result["data"]["shop_id"],
            "expires_at": result["data"]["expires_at"],
        },
    }


# ─────────────────────────────────────────────
# OAuth AKG — auth + callback separados
# ─────────────────────────────────────────────

@router.get("/shopee/akg/auth")
def shopee_akg_auth(request: Request):
    """Redireciona para autorização Shopee da conta AKG (salva em shopee_tokens_akg.json)."""
    if not _config_ok():
        raise HTTPException(status_code=400, detail="Credenciais Shopee não configuradas.")
    redirect_uri = str(request.base_url).rstrip("/") + "/shopee/akg/callback"
    url = ShopeeOAuthService(akg=True).url_autorizacao(redirect_uri)
    return RedirectResponse(url)


@router.get("/shopee/akg/callback")
def shopee_akg_callback(request: Request):
    """Callback OAuth AKG — salva tokens em shopee_tokens_akg.json (nunca toca shopee_tokens.json)."""
    code        = request.query_params.get("code")
    shop_id_str = request.query_params.get("shop_id")
    error       = request.query_params.get("error")

    if error:
        raise HTTPException(status_code=400, detail=f"Shopee AKG retornou erro: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Callback sem 'code'.")
    if not shop_id_str:
        raise HTTPException(status_code=400, detail="Callback sem 'shop_id'.")

    try:
        shop_id = int(shop_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"shop_id inválido: {shop_id_str}")

    # akg=True garante que _salvar_tokens() grava em shopee_tokens_akg.json
    result = ShopeeOAuthService(akg=True).trocar_code(code, shop_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "Falha ao trocar code."))

    return {
        "success": True,
        "message": "Shopee AKG conectada! Tokens salvos em shopee_tokens_akg.json.",
        "data": {"shop_id": result["data"]["shop_id"], "expires_at": result["data"]["expires_at"]},
    }


# ─────────────────────────────────────────────
# Renovar token
# ─────────────────────────────────────────────

@router.post("/shopee/refresh")
def shopee_refresh():
    """Renova o access_token usando o refresh_token armazenado."""
    result = ShopeeOAuthService().renovar_token()
    if not result["success"]:
        raise HTTPException(
            status_code=400,
            detail=result.get("error", "Falha ao renovar token da Shopee."),
        )
    return {
        "success": True,
        "message": "Token da Shopee renovado com sucesso.",
        "data": {
            "shop_id": result["data"].get("shop_id"),
            "expires_at": result["data"].get("expires_at"),
            "renovado_em": result["data"].get("renovado_em"),
        },
    }


# ─────────────────────────────────────────────
# Atualização de preço manual
# ─────────────────────────────────────────────

class AtualizarPrecoShopeeRequest(BaseModel):
    item_id: str
    preco: float
    preco_original: float | None = None


@router.post("/shopee/atualizar-preco")
def shopee_atualizar_preco(req: AtualizarPrecoShopeeRequest):
    """
    Atualiza o preço de um item na Shopee diretamente.
    item_id: ID do anúncio na Shopee (não o SKU do Bling).
    preco: preço atual/promocional.
    preco_original: preço riscado (opcional; se omitido usa o mesmo valor de preco).
    """
    if req.preco <= 0:
        raise HTTPException(status_code=400, detail="Preço deve ser maior que zero.")

    try:
        svc = ShopeeService()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    resultado = svc.atualizar_com_retry(
        item_id=req.item_id,
        preco=req.preco,
        tentativas=3,
    )
    if not resultado["success"]:
        raise HTTPException(
            status_code=400,
            detail=resultado.get("error", "Falha ao atualizar preço na Shopee."),
        )
    return {
        "success": True,
        "item_id": req.item_id,
        "preco": req.preco,
        "data": resultado.get("data"),
    }


# ─────────────────────────────────────────────
# Tokens (leitura segura — sem expor segredos)
# ─────────────────────────────────────────────

@router.get("/shopee/tokens")
def shopee_tokens():
    """Retorna metadados dos tokens armazenados (sem expor access/refresh tokens)."""
    t = _carregar_tokens()
    if not t:
        raise HTTPException(status_code=404, detail="Nenhum token Shopee encontrado.")
    return {
        "success": True,
        "shop_id": t.get("shop_id"),
        "expires_at": t.get("expires_at"),
        "expirado": token_expirado(),
        "obtido_em": t.get("obtido_em"),
        "renovado_em": t.get("renovado_em"),
    }


@router.get("/shopee/exportar-tokens")
def shopee_exportar_tokens():
    """Exporta tokens Shopee para configurar como env vars no Cloud Run. Protegido por middleware X-API-Key."""
    t = _carregar_tokens()
    if not t:
        return {"ok": False, "erro": "Nenhum token Shopee encontrado. Faça a autorização em /shopee/auth primeiro."}
    access_token = t.get("access_token", "")
    refresh_token = t.get("refresh_token", "")
    if not access_token or not refresh_token:
        return {"ok": False, "erro": "Tokens inválidos. Reautorize em /shopee/auth."}
    cmd = (
        f"gcloud run services update shinsei-pricing "
        f"--region southamerica-east1 "
        f"--project shinsei-market "
        f"--update-env-vars "
        f"SHOPEE_ACCESS_TOKEN={access_token},"
        f"SHOPEE_REFRESH_TOKEN={refresh_token}"
    )
    return {
        "ok": True,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "shop_id": t.get("shop_id"),
        "expires_at": t.get("expires_at"),
        "gcloud_cmd": cmd,
    }


# ─────────────────────────────────────────────
# Mapeamento SKU → item_id da Shopee
# ─────────────────────────────────────────────

MAPEAMENTO_PATH = BASE_DIR / "data" / "shopee_mapeamento.json"


def _load_mapeamento() -> dict:
    if not MAPEAMENTO_PATH.exists():
        return {}
    try:
        return json.loads(MAPEAMENTO_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_mapeamento(data: dict) -> None:
    MAPEAMENTO_PATH.parent.mkdir(exist_ok=True)
    MAPEAMENTO_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


@router.get("/shopee/mapeamento")
def shopee_mapeamento_listar():
    """Retorna todos os mapeamentos SKU → item_id cadastrados."""
    m = _load_mapeamento()
    return {
        "success": True,
        "total": len(m),
        "mapeamentos": [
            {"sku": sku, "item_id": item_id} for sku, item_id in m.items()
        ],
    }


class MapeamentoPayload(BaseModel):
    sku: str
    item_id: str


@router.post("/shopee/mapeamento")
def shopee_mapeamento_salvar(payload: MapeamentoPayload):
    """Adiciona ou atualiza o mapeamento de um SKU para um item_id da Shopee."""
    sku = payload.sku.strip()
    item_id = payload.item_id.strip()
    if not sku or not item_id:
        raise HTTPException(status_code=400, detail="SKU e item_id são obrigatórios.")
    m = _load_mapeamento()
    m[sku] = item_id
    _save_mapeamento(m)
    return {"success": True, "sku": sku, "item_id": item_id, "total": len(m)}


@router.delete("/shopee/mapeamento/{sku}")
def shopee_mapeamento_remover(sku: str):
    """Remove o mapeamento de um SKU."""
    m = _load_mapeamento()
    if sku not in m:
        raise HTTPException(status_code=404, detail=f"SKU '{sku}' não encontrado no mapeamento.")
    del m[sku]
    _save_mapeamento(m)
    return {"success": True, "removido": sku, "total": len(m)}


# ─────────────────────────────────────────────
# Função utilitária para uso interno (fila_aprovar)
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# Produtos da Shopee (para auto-mapeamento)
# ─────────────────────────────────────────────

@router.get("/shopee/produtos")
def shopee_produtos():
    """
    Retorna todos os produtos ativos da Shopee com item_id e nome.
    Faz paginação automática e busca nomes via get_item_base_info.
    """
    try:
        svc = ShopeeService()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    todos: list[dict] = []
    offset = 0
    max_iter = 20  # segurança: máx 20 × 50 = 1000 itens

    for _ in range(max_iter):
        data = svc.listar_produtos(offset=offset, page_size=50)
        if data.get("error"):
            break
        resp = data.get("response") or {}
        items = resp.get("item") or []
        if not items:
            break

        # Busca nomes em lote
        ids = [i["item_id"] for i in items]
        info_data = svc.obter_info_items(ids)
        info_map = {
            i["item_id"]: i
            for i in ((info_data.get("response") or {}).get("item_list") or [])
        }
        time.sleep(0.3)

        for item in items:
            iid = item["item_id"]
            info = info_map.get(iid, {})
            todos.append({
                "item_id": str(iid),
                "nome": info.get("item_name") or f"Item {iid}",
                "status": item.get("item_status", "NORMAL"),
            })

        if not resp.get("has_next_item"):
            break
        offset = resp.get("next_offset", offset + 50)
        time.sleep(0.3)

    return {"success": True, "total": len(todos), "produtos": todos}


# ─────────────────────────────────────────────
# Auto-mapeamento SKU ↔ item_id
# ─────────────────────────────────────────────

@router.post("/shopee/mapeamento/auto")
def shopee_mapeamento_auto():
    """
    Puxa todos os produtos da Shopee e tenta associar automaticamente
    com SKUs do Bling usando similaridade de nomes.

    Retorna sugestões para revisão — não salva automaticamente.
    """
    try:
        svc = ShopeeService()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # 1. Coleta todos os produtos da Shopee
    shopee_items: list[dict] = []
    offset = 0
    for _ in range(20):
        data = svc.listar_produtos(offset=offset, page_size=50)
        if data.get("error"):
            break
        resp = data.get("response") or {}
        items = resp.get("item") or []
        if not items:
            break
        ids = [i["item_id"] for i in items]
        info_data = svc.obter_info_items(ids)
        info_map = {
            i["item_id"]: i
            for i in ((info_data.get("response") or {}).get("item_list") or [])
        }
        time.sleep(0.3)
        for item in items:
            iid = item["item_id"]
            info = info_map.get(iid, {})
            shopee_items.append({
                "item_id": str(iid),
                "nome": (info.get("item_name") or f"Item {iid}").lower().strip(),
                "nome_original": info.get("item_name") or f"Item {iid}",
            })
        if not resp.get("has_next_item"):
            break
        offset = resp.get("next_offset", offset + 50)
        time.sleep(0.3)

    if not shopee_items:
        raise HTTPException(status_code=400, detail="Nenhum produto encontrado na Shopee.")

    # 2. Coleta produtos do Bling
    bling_produtos: list[dict] = []
    try:
        from bling_client import BlingClient
        bc = BlingClient()
        pagina = 1
        while pagina <= 30:
            r = bc._get(f"produtos?situacao=A&pagina={pagina}&limite=100")
            prods = (r.get("data") or [])
            if not prods:
                break
            for p in prods:
                cod = (p.get("codigo") or "").strip()
                nome = (p.get("nome") or "").lower().strip()
                if cod and nome:
                    bling_produtos.append({"sku": cod, "nome": nome, "nome_original": p.get("nome", "")})
            pagina += 1
            time.sleep(0.2)
    except Exception as exc:
        logger.warning("Auto-mapeamento: erro ao buscar Bling: %s", exc)

    # 3. Mapeamento atual para excluir já mapeados
    mapeamento_atual = _load_mapeamento()
    itens_ja_mapeados = set(mapeamento_atual.values())

    # 4. Matching por similaridade de nomes
    sugestoes: list[dict] = []
    for si in shopee_items:
        if si["item_id"] in itens_ja_mapeados:
            continue  # já mapeado

        melhor_sku = ""
        melhor_nome_bling = ""
        melhor_score = 0.0

        for bp in bling_produtos:
            score = difflib.SequenceMatcher(
                None, si["nome"], bp["nome"]
            ).ratio()
            if score > melhor_score:
                melhor_score = score
                melhor_sku = bp["sku"]
                melhor_nome_bling = bp["nome_original"]

        sugestoes.append({
            "item_id": si["item_id"],
            "nome_shopee": si["nome_original"],
            "sku_sugerido": melhor_sku if melhor_score >= 0.65 else "",
            "nome_bling": melhor_nome_bling if melhor_score >= 0.65 else "",
            "score": round(melhor_score, 3),
            "confianca": (
                "alta" if melhor_score >= 0.85
                else "media" if melhor_score >= 0.65
                else "baixa"
            ),
        })

    sugestoes.sort(key=lambda x: x["score"], reverse=True)
    return {
        "success": True,
        "total_shopee": len(shopee_items),
        "total_bling": len(bling_produtos),
        "ja_mapeados": len(itens_ja_mapeados),
        "sugestoes": sugestoes,
    }


@router.post("/shopee/mapeamento/confirmar")
async def shopee_mapeamento_confirmar(request: Request):
    """Salva em lote os mapeamentos confirmados pelo usuário."""
    body = await request.json()
    confirmados = body.get("confirmados") or []
    if not confirmados:
        raise HTTPException(status_code=400, detail="Nenhum mapeamento enviado.")

    m = _load_mapeamento()
    salvos = 0
    for entry in confirmados:
        sku = str(entry.get("sku", "")).strip()
        item_id = str(entry.get("item_id", "")).strip()
        if sku and item_id:
            m[sku] = item_id
            salvos += 1

    _save_mapeamento(m)
    return {"success": True, "salvos": salvos, "total": len(m)}


# ─────────────────────────────────────────────
# Conferência de estoque Shopee ↔ Bling
# ─────────────────────────────────────────────

@router.get("/shopee/conferencia/fila")
def shopee_conf_fila(status: str = "", tipo: str = ""):
    from shopee_conferencia import carregar_fila, stats_fila
    itens = carregar_fila()
    if status:
        itens = [i for i in itens if i.get("status") == status]
    if tipo:
        itens = [i for i in itens if i.get("tipo") == tipo]
    return {"itens": itens, "stats": stats_fila()}


@router.post("/shopee/conferencia/conferir")
def shopee_conf_conferir():
    from shopee_conferencia import conferir_shopee
    try:
        from bling_client import BlingClient
        bc = BlingClient()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Bling não disponível: {exc}")
    resultado = conferir_shopee(bc)
    if not resultado.get("ok"):
        raise HTTPException(status_code=400, detail=resultado.get("erro", "Erro ao conferir."))
    return resultado


@router.post("/shopee/conferencia/corrigir/{item_id}")
def shopee_conf_corrigir(item_id: str):
    from shopee_conferencia import corrigir_shopee
    resultado = corrigir_shopee(item_id)
    if not resultado.get("ok"):
        raise HTTPException(status_code=400, detail=resultado.get("erro", "Erro ao corrigir."))
    return resultado


@router.post("/shopee/conferencia/ignorar/{item_id}")
def shopee_conf_ignorar(item_id: str):
    from shopee_conferencia import ignorar_shopee
    return ignorar_shopee(item_id)


@router.post("/shopee/conferencia/limpar")
def shopee_conf_limpar():
    from shopee_conferencia import carregar_fila, salvar_fila, stats_fila
    itens = [i for i in carregar_fila() if i.get("status") == "pendente"]
    salvar_fila(itens)
    return {"ok": True, "stats": stats_fila()}


# ─────────────────────────────────────────────
# Função utilitária para uso interno (fila_aprovar)
# ─────────────────────────────────────────────

def aplicar_preco_shopee_por_sku(sku: str, marketplaces: dict) -> dict | None:
    """
    Tenta aplicar o preço Shopee para um SKU mapeado.
    Chamada internamente pelo fila_aprovar do app.py.
    Retorna dict com resultado ou None se o SKU não estiver mapeado.
    """
    m = _load_mapeamento()
    item_id = m.get(str(sku).strip())
    if not item_id:
        return None  # SKU não mapeado — sem ação

    # Extrai preço do canal Shopee
    shopee_data = marketplaces.get("shopee") or marketplaces.get("Shopee")
    if not shopee_data:
        return {"success": False, "motivo": "Canal 'Shopee' não encontrado nos marketplaces do item."}

    preco = float(shopee_data.get("preco_promocional") or shopee_data.get("preco") or 0)
    if preco <= 0:
        return {"success": False, "motivo": "Preço Shopee calculado é zero ou inválido."}

    try:
        svc = ShopeeService()
        resultado = svc.atualizar_com_retry(item_id=item_id, preco=preco, tentativas=3)
        resultado["item_id"] = item_id
        resultado["sku"] = sku
        resultado["preco_aplicado"] = preco
        return resultado
    except RuntimeError as exc:
        return {"success": False, "motivo": str(exc), "item_id": item_id, "sku": sku}


# ── Exportar produto Bling → Shopee ───────────────────────────────────────────

import re as _re
import tempfile as _tempfile
import os as _os
import urllib.request as _urllib_req

class ExportarShopeePayload(BaseModel):
    produto_id: int
    category_id: int
    attr_nome_variacao: str = "Cor"
    max_variacoes: int = 0  # 0 = todas; >0 = limitar (para teste)
    bling_access_token: str = ""  # override opcional — evita depender do Secret Manager
    titulo_override: str = ""     # se preenchido, substitui o nome do produto Bling
    preco_override: float = 0.0   # se >0, substitui o preço do produto Bling
    imagens_fallback: list = []   # URLs de imagem para usar quando o produto não tem imagens no Bling
    descricao_override: str = ""  # se preenchido, substitui a descrição do produto Bling


def _shopee_req(method: str, path: str, svc: ShopeeService, **kwargs):
    """path = relativo ex: '/product/get_category'. Assina com /api/v2{path}."""
    import httpx
    from services.shopee import API_BASE
    full_path = f"/api/v2{path}"
    params = svc._params_auth(full_path)
    url = f"{API_BASE}{path}"
    with httpx.Client(timeout=60) as client:
        if method == "GET":
            params.update(kwargs.get("params", {}))
            r = client.get(url, params=params)
        else:
            r = client.post(url, params=params, json=kwargs.get("json", {}))
    logger.warning("_shopee_req %s %s → status=%s body=%s", method, path, r.status_code, r.text[:300])
    try:
        return r.json()
    except Exception as exc:
        logger.error("_shopee_req JSON parse error: %s | body: %s", exc, r.text[:200])
        return {"error": "json_parse_error", "message": r.text[:200]}


def _upload_img_shopee(url: str, svc: ShopeeService) -> str | None:
    from services.shopee import API_BASE
    import httpx
    try:
        tmp = _tempfile.mktemp(suffix=".jpg")
        req = _urllib_req.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _urllib_req.urlopen(req, timeout=20) as resp:
            open(tmp, "wb").write(resp.read())
        path = "/media_space/upload_image"
        params = svc._params_auth(f"/api/v2{path}")
        with open(tmp, "rb") as f:
            with httpx.Client(timeout=30) as client:
                r = client.post(f"{API_BASE}{path}", params=params,
                                files={"image": ("img.jpg", f, "image/jpeg")})
        _os.unlink(tmp)
        d = r.json()
        if d.get("error"):
            logger.warning("Upload imagem Shopee erro API: %s", d)
            return None
        img_info = d.get("response", {}).get("image_info", {})
        img_id = img_info.get("image_id") or (img_info.get("image_url_list") or [None])[0]
        logger.info("Upload imagem Shopee OK: %s", img_id)
        return img_id
    except Exception as e:
        logger.warning("Upload imagem Shopee falhou: %s", e)
        return None


@router.get("/shopee/categorias")
def shopee_categorias(q: str = ""):
    """Lista categorias Shopee, opcionalmente filtrando por palavra-chave."""
    svc = ShopeeService()
    d = _shopee_req("GET", "/product/get_category", svc, params={"language": "pt-BR"})
    cats = d.get("response", {}).get("category_list", [])
    if q:
        cats = [c for c in cats if q.lower() in c.get("display_category_name", "").lower()]
    return {"total": len(cats), "categorias": cats[:50]}


@router.get("/shopee/atributos/{category_id}")
def shopee_atributos(category_id: int):
    """Retorna atributos obrigatórios de uma categoria Shopee."""
    svc = ShopeeService()
    d = _shopee_req("GET", "/product/get_attributes", svc,
                    params={"category_id": category_id, "language": "pt-BR"})
    attrs = d.get("response", {}).get("attribute_list", [])
    obrig = [a for a in attrs if a.get("is_mandatory")]
    return {"category_id": category_id, "total": len(attrs), "obrigatorios": obrig}


@router.post("/shopee/exportar-produto")
def shopee_exportar_produto(payload: ExportarShopeePayload):
    """Exporta um produto do Bling para a Shopee como anúncio com variações."""
    import httpx, time as _time

    svc = ShopeeService()

    # 1) Buscar token Bling — ordem: payload override → BlingClient → Secret Manager
    bling_token = payload.bling_access_token.strip()

    if not bling_token:
        try:
            from bling_client import BlingClient
            bc = BlingClient()
            bling_token = bc._get_headers()["Authorization"].replace("Bearer ", "")
        except Exception as _be:
            logger.warning("BlingClient falhou (%s), tentando Secret Manager direto", _be)

    if not bling_token:
        try:
            from token_persistence import load_bling_tokens
            _bt = load_bling_tokens() or {}
            bling_token = _bt.get("access_token", "")
        except Exception as _se:
            logger.error("Secret Manager falhou: %s", _se)

    if not bling_token:
        raise HTTPException(status_code=503, detail="Não foi possível obter token Bling. Reautorize em /bling/auth ou passe bling_access_token no payload.")

    bling_hdrs = {"Authorization": f"Bearer {bling_token}", "Accept": "application/json"}
    with httpx.Client(timeout=30) as _hx:
        _r = _hx.get(f"https://api.bling.com.br/Api/v3/produtos/{payload.produto_id}",
                     headers=bling_hdrs)
    if _r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Bling API {_r.status_code}: {_r.text[:200]}")
    data = _r.json()
    p    = data.get("data", data) if isinstance(data, dict) else {}
    if not p.get("nome"):
        raise HTTPException(status_code=404, detail="Produto não encontrado no Bling.")

    nome       = payload.titulo_override.strip() or p.get("nome", "")
    _desc_raw  = p.get("descricaoCurta") or p.get("descricao") or nome
    descricao  = _re.sub(r"<[^>]+>", "", _desc_raw)
    descricao  = descricao.replace("\xa0", " ").replace("\r\n", "\n").strip()
    if payload.descricao_override:
        descricao = payload.descricao_override
    preco      = payload.preco_override if payload.preco_override > 0 else float(p.get("preco") or 0)
    peso_kg    = float(p.get("pesoBruto") or p.get("pesoLiquido") or 0.3)
    dims       = p.get("dimensoes") or {}
    variacoes  = p.get("variacoes", [])
    if payload.max_variacoes > 0:
        variacoes = variacoes[:payload.max_variacoes]
    midia      = p.get("midia") or {}
    imgs_int   = (midia.get("imagens") or {}).get("internas", [])

    # 2) Marca (brand) da categoria — busca "Alfaparf" ou usa brand_id=0
    brand_d = _shopee_req("GET", "/product/get_brand_list", svc,
                          params={"category_id": payload.category_id, "status": 1,
                                  "offset": 0, "page_size": 100, "language": "pt-BR"})
    brand_list = brand_d.get("response", {}).get("brand_list", [])
    brand_id = 0
    for b in brand_list:
        if "alfaparf" in b.get("display_brand_name", "").lower():
            brand_id = b["brand_id"]
            break

    # 2b) Atributos obrigatórios da categoria
    attrs_d = _shopee_req("GET", "/product/get_attributes", svc,
                          params={"category_id": payload.category_id, "language": "pt-BR"})
    obrig_attrs = [a for a in attrs_d.get("response", {}).get("attribute_list", [])
                   if a.get("is_mandatory")]
    attribute_list = []
    for a in obrig_attrs:
        aid  = a["attribute_id"]
        vals = a.get("attribute_value_list", [])
        if vals:
            attribute_list.append({"attribute_id": aid,
                                   "attribute_value_list": [{"value_id": vals[0]["value_id"]}]})

    # 3) Upload de imagens do produto pai (até 9)
    image_ids = []
    for img in imgs_int[:9]:
        url = img.get("link", "")
        if not url:
            continue
        img_id = _upload_img_shopee(url, svc)
        if img_id:
            image_ids.append(img_id)
        _time.sleep(0.3)

    # Se não há imagens no pai, tentar pegar imagemURL das variações
    if not image_ids:
        seen_urls = set()
        for v in variacoes[:9]:
            url = v.get("imagemURL", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                img_id = _upload_img_shopee(url, svc)
                if img_id:
                    image_ids.append(img_id)
                _time.sleep(0.3)

    # Se ainda não há imagens, usar fallback fornecido no payload
    if not image_ids:
        for url in (payload.imagens_fallback or [])[:9]:
            img_id = _upload_img_shopee(url, svc)
            if img_id:
                image_ids.append(img_id)
            _time.sleep(0.3)

    # 4) Logística ativa
    log_d   = _shopee_req("GET", "/logistics/get_channel_list", svc)
    logistics = [
        {"logistic_id": ch.get("logistic_id") or ch.get("logistics_channel_id"),
         "enabled": True, "is_free": False}
        for ch in log_d.get("response", {}).get("logistics_channel_list", [])
        if (ch.get("enabled") or ch.get("enabled_list_time"))
           and (ch.get("logistic_id") or ch.get("logistics_channel_id"))
    ]

    # 5) Montar tier_variation + models (90 cores)
    def _extrair_cor(nome_v):
        m = _re.search(r'Cor:\s*(\S+)', nome_v or '', _re.IGNORECASE)
        return m.group(1) if m else nome_v

    opcoes  = []
    models  = []
    for v in variacoes:
        nome_v     = v.get("nome", "")
        cor_label  = _extrair_cor(nome_v) or nome_v or f"Cor {len(opcoes)+1}"
        # Prefixar com "Cor " se começar com ponto ou número (Shopee pode rejeitar)
        if cor_label and cor_label[0] in ".0123456789":
            cor_label = f"Cor {cor_label}"
        preco_v    = float(v.get("preco") or preco or 1)
        estoque_v  = int(float((v.get("estoque") or {}).get("saldoVirtualTotal") or 0))
        opcoes.append(cor_label)
        stock_qty = max(estoque_v, 1)
        models.append({
            "tier_index":     [len(opcoes) - 1],
            "normal_stock":   stock_qty,
            "seller_stock":   [{"location_id": "", "stock": stock_qty}],
            "original_price": round(preco_v, 2),
            "model_sku":      v.get("codigo", "") or "",
        })

    # Shopee BR: limite de 50 opções por tier
    SHOPEE_MAX_OPCOES = 50
    if len(opcoes) > SHOPEE_MAX_OPCOES:
        logger.warning("Shopee: %d variações excedem limite de %d — truncando", len(opcoes), SHOPEE_MAX_OPCOES)
        opcoes  = opcoes[:SHOPEE_MAX_OPCOES]
        models  = models[:SHOPEE_MAX_OPCOES]
        # corrigir tier_index após truncamento
        for i, m in enumerate(models):
            m["tier_index"] = [i]

    if not image_ids:
        raise HTTPException(status_code=400,
                            detail=f"Nenhuma imagem foi carregada na Shopee. "
                                   f"Verifique as URLs das imagens do produto Bling ({len(imgs_int)} disponíveis).")

    # Produto individual (sem variacoes Bling) ou com variacoes
    produto_simples = len(opcoes) == 0
    if produto_simples:
        estoque_prod = int(float((p.get("estoque") or {}).get("saldoVirtualTotal") or 0))
        total_stock  = max(estoque_prod, 1)
    else:
        total_stock = sum(m["normal_stock"] for m in models)

    # add_item: com ou sem tier_variation
    models_payload = [
        {
            "tier_index":     [i],
            "original_price": models[i]["original_price"],
            "model_sku":      models[i].get("model_sku", ""),
        }
        for i in range(len(opcoes))
    ]

    item_body = {
        "category_id":    payload.category_id,
        "item_name":      nome[:120],
        "description":    descricao[:3000] or nome,
        "item_sku":       p.get("codigo", "") or "",
        "weight":         round(max(peso_kg, 0.1), 3),
        "condition":      "NEW",
        "original_price": round(preco, 2) or 1,
        "dimension": {
            "package_length": max(int(float(dims.get("profundidade") or 0)) or 18, 1),
            "package_width":  max(int(float(dims.get("largura") or 0)) or 12, 1),
            "package_height": max(int(float(dims.get("altura") or 0)) or 8, 1),
        },
        "brand":          {"brand_id": brand_id} if brand_id else {"brand_id": 0, "original_brand_name": "No Brand"},
        "image":          {"image_id_list": image_ids},
        "seller_stock":   [{"location_id": "", "stock": total_stock}],
    }
    if not produto_simples:
        item_body["tier_variation"] = [{"name": payload.attr_nome_variacao,
                                        "option_list": [{"option": o} for o in opcoes]}]
        item_body["model"] = models_payload
    if logistics:
        item_body["logistic_info"] = [
            {"logistic_id": ch["logistic_id"], "enabled": True}
            for ch in logistics
        ]
    if attribute_list:
        item_body["attribute_list"] = attribute_list

    logger.info("SHOPEE add_item: simples=%s opcoes=%d models=%d sample=%s",
                produto_simples, len(opcoes), len(models_payload), models_payload[0] if models_payload else {})
    resp = _shopee_req("POST", "/product/add_item", svc, json=item_body)
    logger.warning("SHOPEE add_item RESP: %s", resp)
    item_id = (resp.get("response") or {}).get("item_id")
    if not item_id:
        raise HTTPException(status_code=400,
                            detail=f"add_item falhou: {resp.get('message') or str(resp)[:500]}")

    # Verificar se modelos foram criados
    _time.sleep(2)
    debug_r = _shopee_req("GET", "/product/get_item_base_info", svc,
                          params={"item_id_list": str(item_id),
                                  "need_tax_info": "false",
                                  "need_complaint_policy": "false"})
    item_info = (debug_r.get("response") or {}).get("item_list", [{}])[0]
    has_model = item_info.get("has_model", False)

    return {
        "ok":                True,
        "item_id":           item_id,
        "imagens_upadas":    len(image_ids),
        "variacoes_total":   len(variacoes),
        "variacoes_enviadas": len(opcoes),
        "produto_simples":   produto_simples,
        "has_model":         has_model,
        "add_item_warning":  resp.get("warning", "")[:200],
        "url":               "https://seller.shopee.com.br/portal/product/list",
    }


@router.delete("/shopee/item/{item_id}")
def shopee_deletar_item(item_id: int):
    """Deleta um item da Shopee pelo item_id."""
    svc = ShopeeService()
    resp = _shopee_req("POST", "/product/delete_item", svc, json={"item_id": item_id})
    logger.warning("SHOPEE delete_item %s → %s", item_id, resp)
    if resp.get("error"):
        raise HTTPException(status_code=400, detail=resp.get("message") or str(resp))
    return {"ok": True, "item_id": item_id, "resp": resp}


@router.get("/shopee/debug/item/{item_id}")
def shopee_debug_item(item_id: int):
    """Debug: ver estrutura completa de um item da Shopee."""
    svc = ShopeeService()
    import httpx
    from services.shopee import API_BASE
    path = "/product/get_item_base_info"
    params = svc._params_auth(f"/api/v2{path}")
    params["item_id_list"] = str(item_id)
    params["need_tax_info"] = "false"
    params["need_complaint_policy"] = "false"
    with httpx.Client(timeout=20) as client:
        r = client.get(f"{API_BASE}{path}", params=params)
    d = r.json()
    items = d.get("response", {}).get("item_list", [])
    return {"item_count": len(items), "first_item": items[0] if items else None, "raw": d}


class ShopeeRawPayload(BaseModel):
    path: str   # ex: "/product/initialise_tier_variation"
    body: dict = {}
    params: dict = {}
    method: str = "POST"


@router.post("/shopee/debug/raw")
def shopee_debug_raw(payload: ShopeeRawPayload):
    """Debug: chamar qualquer endpoint Shopee com body/params arbitrário."""
    svc = ShopeeService()
    resp = _shopee_req(payload.method, payload.path, svc,
                       json=payload.body, params=payload.params)
    return {"path": payload.path, "resp": resp}


@router.get("/shopee/debug/models/{item_id}")
def shopee_debug_models(item_id: int):
    """Debug: listar modelos de um item."""
    svc = ShopeeService()
    resp = _shopee_req("GET", "/product/get_model_list", svc,
                       params={"item_id": str(item_id)})
    return resp


# ── Alfaparf Evolution Ox 20 – exportação em lote Shopee ──────────────────────
_ALFAPARF_SHOPEE_JOB: dict = {"status": "idle", "ok": 0, "erros": 0, "total": 0, "log": []}

ALFAPARF_SHOPEE_PRECO   = 54.90
ALFAPARF_SHOPEE_REMOVER = "Todas As Cores"
ALFAPARF_SHOPEE_DESC = (
    "O kit contém:\n"
    "1 Coloração Alfaparf Evolution (Numeração e Cor referente ao anúncio)\n"
    "1 Emulsão ativadora alfaparf oxid'O (Volume e mls referentes ao anúncio)"
)


@router.post("/shopee/exportar-alfaparf-kits")
def shopee_exportar_alfaparf_kits(category_id: int, bling_access_token: str = ""):
    """
    Exporta individualmente todos os 'Kit Coloração + Ox 20 Vol Alfaparf Evolution'
    para a Shopee como produtos simples (sem tier_variation), removendo
    'Todas As Cores' do título e forçando preço R$54,90.

    Parâmetros:
      category_id         — categoria Shopee para kits de coloração (obrigatório)
      bling_access_token  — token Bling (opcional; usa BlingClient se omitido)

    Acompanhe em /shopee/exportar-alfaparf-kits/status.
    """
    import threading, re as _re2, requests as _req

    def _run(cat_id: int, b_token: str):
        import time as _time2

        # Token Bling
        if not b_token:
            try:
                from bling_client import BlingClient
                b_token = BlingClient()._get_headers()["Authorization"].replace("Bearer ", "")
            except Exception:
                pass
        if not b_token:
            try:
                import json as _j
                from pathlib import Path as _P
                from bling_client import _decrypt_tokens
                _raw = _j.loads((_P(__file__).parent.parent / "data" / "bling_tokens.json").read_text())
                if "encrypted" in _raw:
                    b_token = _decrypt_tokens(_raw["encrypted"]).get("access_token", "")
            except Exception as _e:
                logger.warning("[ALFAPARF-SHOPEE] fallback decrypt falhou: %s", _e)
        if not b_token:
            logger.error("[ALFAPARF-SHOPEE] Sem token Bling")
            return

        _ALFAPARF_SHOPEE_JOB.update({"status": "running", "ok": 0, "erros": 0, "total": 0, "log": []})

        # Coletar produtos
        produtos = []
        vistos   = set()
        pagina   = 1
        while True:
            r = _req.get(
                "https://api.bling.com.br/Api/v3/produtos",
                headers={"Authorization": f"Bearer {b_token}", "Accept": "application/json"},
                params={"nome": "Kit Coloração + Ox 20", "pagina": pagina, "limite": 100},
                timeout=30,
            )
            if r.status_code != 200:
                logger.error("[ALFAPARF-SHOPEE] Bling API status=%d pag=%d: %s", r.status_code, pagina, r.text[:200])
                break
            itens = r.json().get("data") or []
            if not itens:
                break
            for p in itens:
                pid  = p.get("id")
                nome = p.get("nome") or ""
                if pid not in vistos and "Alfaparf" in nome and "Todas As Cores" in nome:
                    vistos.add(pid)
                    produtos.append(p)
                else:
                    vistos.add(pid)
            if len(itens) < 100:
                break
            pagina += 1
            _time2.sleep(0.3)

        # Coletar imagens do produto PAI para usar como fallback
        imagens_pai = []
        for p in produtos:
            if p.get("nome", "").strip() == "Kit Coloração + Ox 20 Vol Alfaparf Evolution Todas As Cores":
                midia = p.get("midia") or {}
                imagens_pai = [img.get("link", "") for img in
                               (midia.get("imagens") or {}).get("internas", []) if img.get("link")]
                break
        # Se não achou no lote, buscar via API
        if not imagens_pai:
            r_pai = _req.get(
                "https://api.bling.com.br/Api/v3/produtos",
                headers={"Authorization": f"Bearer {b_token}", "Accept": "application/json"},
                params={"nome": "Kit Coloração + Ox 20 Vol Alfaparf Evolution Todas As Cores", "pagina": 1, "limite": 5},
                timeout=30,
            )
            if r_pai.status_code == 200:
                for item in r_pai.json().get("data") or []:
                    if item.get("nome", "").strip() == "Kit Coloração + Ox 20 Vol Alfaparf Evolution Todas As Cores":
                        midia = item.get("midia") or {}
                        imagens_pai = [img.get("link", "") for img in
                                       (midia.get("imagens") or {}).get("internas", []) if img.get("link")]
                        break
        logger.info("[ALFAPARF-SHOPEE] %d imagens do produto pai para fallback", len(imagens_pai))

        # Excluir o PAI
        produtos = [p for p in produtos if p.get("nome", "").strip()
                    != "Kit Coloração + Ox 20 Vol Alfaparf Evolution Todas As Cores"]

        _ALFAPARF_SHOPEE_JOB["total"] = len(produtos)
        logger.info("[ALFAPARF-SHOPEE] %d produtos a exportar", len(produtos))

        for prod in produtos:
            pid   = prod.get("id")
            nome  = prod.get("nome") or ""
            sku   = prod.get("codigo") or ""
            titulo = _re2.sub(r"\s{2,}", " ",
                               nome.replace(ALFAPARF_SHOPEE_REMOVER, "")).strip()
            try:
                pl = ExportarShopeePayload(
                    produto_id=int(pid),
                    category_id=cat_id,
                    bling_access_token=b_token,
                    titulo_override=titulo,
                    preco_override=ALFAPARF_SHOPEE_PRECO,
                    imagens_fallback=imagens_pai,
                    descricao_override=ALFAPARF_SHOPEE_DESC,
                )
                resultado = shopee_exportar_produto(pl)
                logger.info("[ALFAPARF-SHOPEE] OK pid=%d item_id=%s titulo=%s",
                            pid, resultado.get("item_id"), titulo[:60])
                _ALFAPARF_SHOPEE_JOB["ok"] += 1
                _ALFAPARF_SHOPEE_JOB["log"].append({
                    "pid": pid, "sku": sku, "titulo": titulo[:80],
                    "item_id": resultado.get("item_id"), "status": "ok",
                })
            except Exception as exc:
                logger.error("[ALFAPARF-SHOPEE] ERRO pid=%d: %s", pid, exc)
                _ALFAPARF_SHOPEE_JOB["erros"] += 1
                _ALFAPARF_SHOPEE_JOB["log"].append({
                    "pid": pid, "sku": sku, "titulo": titulo[:80],
                    "status": "erro", "erro": str(exc)[:300],
                })
            _time2.sleep(2)

        _ALFAPARF_SHOPEE_JOB["status"] = "done"
        logger.info("[ALFAPARF-SHOPEE] Concluído: %d ok, %d erros",
                    _ALFAPARF_SHOPEE_JOB["ok"], _ALFAPARF_SHOPEE_JOB["erros"])

    threading.Thread(target=_run, args=(category_id, bling_access_token), daemon=True).start()
    return {
        "ok":  True,
        "msg": "Job iniciado. Acompanhe em /shopee/exportar-alfaparf-kits/status",
    }


@router.get("/shopee/exportar-alfaparf-kits/status")
def shopee_exportar_alfaparf_kits_status():
    """Status do job de exportação dos kits Alfaparf Evolution Ox 20 para a Shopee."""
    return _ALFAPARF_SHOPEE_JOB


_ALFAPARF_DESC_JOB: dict = {"status": "idle", "ok": 0, "erros": 0, "total": 0, "log": []}


@router.post("/shopee/atualizar-desc-alfaparf-kits")
def shopee_atualizar_desc_alfaparf_kits():
    """
    Busca todos os anúncios 'Kit Coloração + Ox 20 Vol Alfaparf Evolution' na Shopee
    e atualiza a descrição com o texto padrão de composição do kit.
    """
    import threading, time as _t
    import httpx as _hx
    from services.shopee import API_BASE as _API

    def _buscar_todos_alfaparf(svc):
        """Coleta todos os item_ids de kits Alfaparf na loja Shopee."""
        item_ids = []
        offset = 0
        while True:
            path = "/product/get_item_list"
            params = svc._params_auth(f"/api/v2{path}")
            params.update({"offset": offset, "page_size": 100, "item_status": "NORMAL"})
            r = _hx.get(f"{_API}{path}", params=params, timeout=20)
            d = r.json()
            items = (d.get("response") or {}).get("item", []) or []
            if not items:
                break
            item_ids.extend(i["item_id"] for i in items)
            if not (d.get("response") or {}).get("has_next_page"):
                break
            offset += 100
            _t.sleep(0.3)
        return item_ids

    def _filtrar_alfaparf(svc, todos_ids):
        """Filtra apenas os kits Alfaparf Evolution Ox 20 pelo nome."""
        alfaparf = []
        for i in range(0, len(todos_ids), 50):
            batch = todos_ids[i:i+50]
            path = "/product/get_item_base_info"
            params = svc._params_auth(f"/api/v2{path}")
            params["item_id_list"] = ",".join(str(x) for x in batch)
            params["need_tax_info"] = "false"
            r = _hx.get(f"{_API}{path}", params=params, timeout=20)
            for item in (r.json().get("response") or {}).get("item_list") or []:
                nome = item.get("item_name", "")
                if "Alfaparf" in nome and "Ox 20" in nome:
                    alfaparf.append(item["item_id"])
            _t.sleep(0.3)
        return alfaparf

    def _run():
        svc = ShopeeService()
        _ALFAPARF_DESC_JOB.update({"status": "running", "ok": 0, "erros": 0, "total": 0, "log": []})
        try:
            logger.info("[ALFAPARF-DESC] Buscando todos os items da loja...")
            todos = _buscar_todos_alfaparf(svc)
            logger.info("[ALFAPARF-DESC] %d items totais — filtrando Alfaparf...", len(todos))
            item_ids = _filtrar_alfaparf(svc, todos)
            logger.info("[ALFAPARF-DESC] %d kits Alfaparf encontrados", len(item_ids))
        except Exception as exc:
            logger.error("[ALFAPARF-DESC] Erro ao buscar items: %s", exc)
            _ALFAPARF_DESC_JOB["status"] = "error"
            return

        _ALFAPARF_DESC_JOB["total"] = len(item_ids)
        path = "/product/update_item"
        for item_id in item_ids:
            try:
                params = svc._params_auth(f"/api/v2{path}")
                body = {"item_id": item_id, "description": ALFAPARF_SHOPEE_DESC}
                r = _hx.post(f"{_API}{path}", params=params, json=body, timeout=20)
                d = r.json()
                if d.get("error"):
                    raise ValueError(f"update_item erro: {d.get('message','')}")
                _ALFAPARF_DESC_JOB["ok"] += 1
                _ALFAPARF_DESC_JOB["log"].append({"item_id": item_id, "status": "ok"})
                logger.info("[ALFAPARF-DESC] OK item_id=%d", item_id)
            except Exception as exc:
                _ALFAPARF_DESC_JOB["erros"] += 1
                _ALFAPARF_DESC_JOB["log"].append({"item_id": item_id, "status": "erro", "erro": str(exc)[:200]})
                logger.error("[ALFAPARF-DESC] ERRO item_id=%d: %s", item_id, exc)
            _t.sleep(0.5)
        _ALFAPARF_DESC_JOB["status"] = "done"
        logger.info("[ALFAPARF-DESC] Concluído: %d ok, %d erros",
                    _ALFAPARF_DESC_JOB["ok"], _ALFAPARF_DESC_JOB["erros"])

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "msg": "Job iniciado. Acompanhe em /shopee/atualizar-desc-alfaparf-kits/status"}


@router.get("/shopee/atualizar-desc-alfaparf-kits/status")
def shopee_atualizar_desc_alfaparf_kits_status():
    return _ALFAPARF_DESC_JOB


# ── Atualização de preço dos kits Alfaparf na Shopee ─────────────────────────
_ALFAPARF_PRECO_JOB: dict = {"status": "idle", "ok": 0, "erros": 0, "total": 0, "preco": 0.0, "log": []}


@router.post("/shopee/atualizar-preco-alfaparf-kits")
def shopee_atualizar_preco_alfaparf_kits(preco: float = 47.90):
    """
    Atualiza o preço de todos os kits Alfaparf Evolution Ox 20 na Shopee.
    Padrão: R$47,90. Roda em background.
    """
    import threading

    def _run():
        import httpx as _hx
        import time as _t
        from services.shopee import API_BASE as _API

        def _buscar_todos(svc):
            item_ids = []
            offset = 0
            while True:
                path = "/product/get_item_list"
                params = svc._params_auth(f"/api/v2{path}")
                params.update({"offset": offset, "page_size": 100, "item_status": "NORMAL"})
                r = _hx.get(f"{_API}{path}", params=params, timeout=20)
                d = r.json()
                items = (d.get("response") or {}).get("item", []) or []
                if not items:
                    break
                item_ids.extend(i["item_id"] for i in items)
                if not (d.get("response") or {}).get("has_next_page"):
                    break
                offset += 100
                _t.sleep(0.3)
            return item_ids

        def _filtrar(svc, todos_ids):
            alfaparf = []
            for i in range(0, len(todos_ids), 50):
                batch = todos_ids[i:i+50]
                path = "/product/get_item_base_info"
                params = svc._params_auth(f"/api/v2{path}")
                params["item_id_list"] = ",".join(str(x) for x in batch)
                params["need_tax_info"] = "false"
                r = _hx.get(f"{_API}{path}", params=params, timeout=20)
                for item in (r.json().get("response") or {}).get("item_list") or []:
                    nome = item.get("item_name", "")
                    if "Alfaparf" in nome and "Ox 20" in nome:
                        alfaparf.append(item["item_id"])
                _t.sleep(0.3)
            return alfaparf

        _ALFAPARF_PRECO_JOB.update({
            "status": "running", "ok": 0, "erros": 0, "total": 0,
            "preco": preco, "log": []
        })
        try:
            svc = ShopeeService()
        except Exception as exc:
            logger.error("[ALFAPARF-PRECO] ShopeeService falhou: %s", exc)
            _ALFAPARF_PRECO_JOB["status"] = "error"
            return
        try:
            todos = _buscar_todos(svc)
            item_ids = _filtrar(svc, todos)
            logger.info("[ALFAPARF-PRECO] %d kits encontrados", len(item_ids))
        except Exception as exc:
            logger.error("[ALFAPARF-PRECO] Erro ao buscar items: %s", exc)
            _ALFAPARF_PRECO_JOB["status"] = "error"
            return

        _ALFAPARF_PRECO_JOB["total"] = len(item_ids)
        preco_centavos = int(round(preco * 100000))  # Shopee usa microprice

        for item_id in item_ids:
            try:
                resultado = svc.atualizar_com_retry(item_id=item_id, preco=preco, tentativas=3)
                if not resultado.get("success"):
                    raise ValueError(resultado.get("error", "Falha desconhecida"))
                _ALFAPARF_PRECO_JOB["ok"] += 1
                _ALFAPARF_PRECO_JOB["log"].append({"item_id": item_id, "status": "ok"})
                logger.info("[ALFAPARF-PRECO] OK item_id=%d preco=%.2f", item_id, preco)
            except Exception as exc:
                _ALFAPARF_PRECO_JOB["erros"] += 1
                _ALFAPARF_PRECO_JOB["log"].append({
                    "item_id": item_id, "status": "erro", "erro": str(exc)[:200]
                })
                logger.error("[ALFAPARF-PRECO] ERRO item_id=%d: %s", item_id, exc)
            _t.sleep(0.5)

        _ALFAPARF_PRECO_JOB["status"] = "done"
        logger.info("[ALFAPARF-PRECO] Concluído: ok=%d erros=%d preco=%.2f",
                    _ALFAPARF_PRECO_JOB["ok"], _ALFAPARF_PRECO_JOB["erros"], preco)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "msg": f"Job iniciado — atualizando para R${preco:.2f}. Acompanhe em /shopee/atualizar-preco-alfaparf-kits/status"}


@router.get("/shopee/atualizar-preco-alfaparf-kits/status")
def shopee_atualizar_preco_alfaparf_kits_status():
    return _ALFAPARF_PRECO_JOB


# ── Verificação de estrutura Bling para kits Alfaparf ─────────────────────────
_VERIF_ESTRUTURA_JOB: dict = {"status": "idle", "total": 0, "ok": 0,
                               "sem_estrutura": 0, "erros": 0, "log": []}


@router.post("/bling/verificar-estrutura-alfaparf")
def bling_verificar_estrutura_alfaparf():
    """
    Varre todos os produtos 'Kit Coloração + Ox 20 Vol Alfaparf Evolution' no Bling,
    verifica se cada um tem estrutura.componentes preenchida e retorna relatório.
    """
    import threading, time as _t, requests as _req2

    def _get_bling_token():
        try:
            from bling_client import BlingClient
            return BlingClient()._get_headers()["Authorization"].replace("Bearer ", "")
        except Exception:
            pass
        try:
            import json as _j
            from pathlib import Path as _P
            from bling_client import _decrypt_tokens
            _raw = _j.loads((_P(__file__).parent.parent / "data" / "bling_tokens.json").read_text())
            if "encrypted" in _raw:
                return _decrypt_tokens(_raw["encrypted"]).get("access_token", "")
        except Exception:
            pass
        return ""

    def _run():
        _VERIF_ESTRUTURA_JOB.update({"status": "running", "total": 0, "ok": 0,
                                     "sem_estrutura": 0, "erros": 0, "log": []})
        token = _get_bling_token()
        if not token:
            _VERIF_ESTRUTURA_JOB["status"] = "error"
            _VERIF_ESTRUTURA_JOB["log"].append({"erro": "Token Bling indisponível"})
            return

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        # 1. Coletar todos os IDs
        ids = []
        pagina = 1
        while True:
            r = _req2.get("https://api.bling.com.br/Api/v3/produtos",
                          headers=headers,
                          params={"nome": "Kit Coloração + Ox 20", "pagina": pagina, "limite": 100},
                          timeout=30)
            if r.status_code == 401:
                _VERIF_ESTRUTURA_JOB["status"] = "error"
                _VERIF_ESTRUTURA_JOB["log"].append({"erro": "Token Bling expirado (401)"})
                return
            if r.status_code != 200:
                break
            itens = r.json().get("data") or []
            if not itens:
                break
            for p in itens:
                nome = p.get("nome") or ""
                if "Alfaparf" in nome and "Todas As Cores" in nome:
                    # excluir o produto PAI com variações (formato V)
                    if p.get("formato") != "V":
                        ids.append({"id": p["id"], "nome": nome, "sku": p.get("codigo","")})
            if len(itens) < 100:
                break
            pagina += 1
            _t.sleep(0.3)

        # Excluir produto pai exato
        ids = [p for p in ids if p["nome"].strip() != "Kit Coloração + Ox 20 Vol Alfaparf Evolution Todas As Cores"]
        _VERIF_ESTRUTURA_JOB["total"] = len(ids)
        logger.info("[VERIF-ESTRUTURA] %d produtos a verificar", len(ids))

        # 2. Verificar estrutura de cada um
        for item in ids:
            pid = item["id"]
            try:
                r2 = _req2.get(f"https://api.bling.com.br/Api/v3/produtos/{pid}",
                               headers=headers, timeout=15)
                if r2.status_code != 200:
                    raise ValueError(f"HTTP {r2.status_code}")
                dados = r2.json().get("data") or {}
                estrutura = dados.get("estrutura") or {}
                componentes = estrutura.get("componentes") or []
                nomes_comp = [
                    f"{c.get('produto', {}).get('nome', '') or c.get('produto', {}).get('codigo', '') or str(c.get('produto', {}).get('id', ''))} (qtd:{c.get('quantidade',1)})"
                    for c in componentes
                ]
                if componentes:
                    _VERIF_ESTRUTURA_JOB["ok"] += 1
                    _VERIF_ESTRUTURA_JOB["log"].append({
                        "id": pid, "sku": item["sku"],
                        "nome": item["nome"][:70],
                        "status": "ok",
                        "componentes": nomes_comp,
                    })
                else:
                    _VERIF_ESTRUTURA_JOB["sem_estrutura"] += 1
                    _VERIF_ESTRUTURA_JOB["log"].append({
                        "id": pid, "sku": item["sku"],
                        "nome": item["nome"][:70],
                        "status": "sem_estrutura",
                    })
            except Exception as exc:
                _VERIF_ESTRUTURA_JOB["erros"] += 1
                _VERIF_ESTRUTURA_JOB["log"].append({
                    "id": pid, "sku": item["sku"],
                    "nome": item["nome"][:70],
                    "status": "erro", "erro": str(exc)[:100],
                })
            _t.sleep(0.2)

        _VERIF_ESTRUTURA_JOB["status"] = "done"
        logger.info("[VERIF-ESTRUTURA] ok=%d sem_estrutura=%d erros=%d",
                    _VERIF_ESTRUTURA_JOB["ok"],
                    _VERIF_ESTRUTURA_JOB["sem_estrutura"],
                    _VERIF_ESTRUTURA_JOB["erros"])

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "msg": "Verificação iniciada. Acompanhe em /bling/verificar-estrutura-alfaparf/status"}


@router.get("/bling/verificar-estrutura-alfaparf/status")
def bling_verificar_estrutura_alfaparf_status():
    return _VERIF_ESTRUTURA_JOB


# ── Verificação de EAN dos componentes dos kits Alfaparf ──────────────────────
_VERIF_EAN_JOB: dict = {"status": "idle", "total": 0, "com_ean": 0,
                         "sem_ean": 0, "erros": 0, "log": []}


@router.post("/bling/verificar-ean-componentes-alfaparf")
def bling_verificar_ean_componentes_alfaparf():
    """
    Para cada kit Alfaparf com estrutura, busca os componentes e verifica
    se têm EAN/GTIN cadastrado no Bling. Retorna relatório por kit.
    """
    import threading, time as _t, requests as _req3

    def _get_bling_token():
        try:
            from bling_client import BlingClient
            return BlingClient()._get_headers()["Authorization"].replace("Bearer ", "")
        except Exception:
            pass
        try:
            import json as _j
            from pathlib import Path as _P
            from bling_client import _decrypt_tokens
            _raw = _j.loads((_P(__file__).parent.parent / "data" / "bling_tokens.json").read_text())
            if "encrypted" in _raw:
                return _decrypt_tokens(_raw["encrypted"]).get("access_token", "")
        except Exception:
            pass
        return ""

    def _run():
        _VERIF_EAN_JOB.update({"status": "running", "total": 0, "com_ean": 0,
                                "sem_ean": 0, "erros": 0, "log": []})
        token = _get_bling_token()
        if not token:
            _VERIF_EAN_JOB["status"] = "error"
            _VERIF_EAN_JOB["log"].append({"erro": "Token Bling indisponível"})
            return

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        _comp_cache: dict = {}  # cache EAN por produto_id

        def _ean_componente(comp_id: int) -> str:
            if comp_id in _comp_cache:
                return _comp_cache[comp_id]
            try:
                r = _req3.get(f"https://api.bling.com.br/Api/v3/produtos/{comp_id}",
                              headers=headers, timeout=15)
                if r.status_code == 200:
                    d = r.json().get("data") or {}
                    ean = d.get("gtin") or d.get("codigoBarras") or ""
                    # gtin pode ser lista
                    if isinstance(ean, list):
                        ean = ean[0] if ean else ""
                    _comp_cache[comp_id] = ean
                    return ean
            except Exception:
                pass
            _comp_cache[comp_id] = ""
            return ""

        # Coletar kits
        ids = []
        pagina = 1
        while True:
            r = _req3.get("https://api.bling.com.br/Api/v3/produtos",
                          headers=headers,
                          params={"nome": "Kit Coloração + Ox 20", "pagina": pagina, "limite": 100},
                          timeout=30)
            if r.status_code != 200:
                break
            itens = r.json().get("data") or []
            if not itens:
                break
            for p in itens:
                nome = p.get("nome") or ""
                if "Alfaparf" in nome and "Todas As Cores" in nome and p.get("formato") != "V":
                    if nome.strip() != "Kit Coloração + Ox 20 Vol Alfaparf Evolution Todas As Cores":
                        ids.append({"id": p["id"], "nome": nome, "sku": p.get("codigo", "")})
            if len(itens) < 100:
                break
            pagina += 1
            _t.sleep(0.3)

        _VERIF_EAN_JOB["total"] = len(ids)
        logger.info("[VERIF-EAN] %d kits a verificar", len(ids))

        for item in ids:
            pid = item["id"]
            try:
                r2 = _req3.get(f"https://api.bling.com.br/Api/v3/produtos/{pid}",
                               headers=headers, timeout=15)
                if r2.status_code != 200:
                    raise ValueError(f"HTTP {r2.status_code}")
                dados = r2.json().get("data") or {}
                estrutura = dados.get("estrutura") or {}
                componentes = estrutura.get("componentes") or []

                if not componentes:
                    _VERIF_EAN_JOB["sem_ean"] += 1
                    _VERIF_EAN_JOB["log"].append({
                        "kit_id": pid, "sku": item["sku"],
                        "nome": item["nome"][:70],
                        "status": "sem_estrutura",
                    })
                    continue

                comp_info = []
                todos_com_ean = True
                for c in componentes:
                    comp_id = (c.get("produto") or {}).get("id")
                    comp_nome = (c.get("produto") or {}).get("nome", "") or ""
                    comp_sku  = (c.get("produto") or {}).get("codigo", "") or ""
                    ean = _ean_componente(comp_id) if comp_id else ""
                    if not ean:
                        todos_com_ean = False
                    comp_info.append({
                        "id": comp_id, "nome": comp_nome or comp_sku,
                        "ean": ean, "qtd": c.get("quantidade", 1)
                    })
                    _t.sleep(0.15)

                if todos_com_ean:
                    _VERIF_EAN_JOB["com_ean"] += 1
                    status = "ok"
                else:
                    _VERIF_EAN_JOB["sem_ean"] += 1
                    status = "sem_ean"

                _VERIF_EAN_JOB["log"].append({
                    "kit_id": pid, "sku": item["sku"],
                    "nome": item["nome"][:70],
                    "status": status,
                    "componentes": comp_info,
                })
                logger.info("[VERIF-EAN] %s kit=%s status=%s", item["sku"], pid, status)
            except Exception as exc:
                _VERIF_EAN_JOB["erros"] += 1
                _VERIF_EAN_JOB["log"].append({
                    "kit_id": pid, "sku": item["sku"],
                    "nome": item["nome"][:70],
                    "status": "erro", "erro": str(exc)[:100],
                })
            _t.sleep(0.2)

        _VERIF_EAN_JOB["status"] = "done"
        logger.info("[VERIF-EAN] com_ean=%d sem_ean=%d erros=%d",
                    _VERIF_EAN_JOB["com_ean"], _VERIF_EAN_JOB["sem_ean"], _VERIF_EAN_JOB["erros"])

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "msg": "Verificação iniciada. Acompanhe em /bling/verificar-ean-componentes-alfaparf/status"}


@router.get("/bling/verificar-ean-componentes-alfaparf/status")
def bling_verificar_ean_componentes_alfaparf_status():
    return _VERIF_EAN_JOB
