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
