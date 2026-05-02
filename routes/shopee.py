# -*- coding: utf-8 -*-
"""
routes/shopee.py — Shinsei Pricing
Endpoints para OAuth, status e atualização de preços na Shopee.
"""
from __future__ import annotations

import json
import logging
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
