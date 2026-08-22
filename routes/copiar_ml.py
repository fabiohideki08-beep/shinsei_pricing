# -*- coding: utf-8 -*-
"""
routes/copiar_ml.py — Copia anúncios do ML Shinsei → ML AKG
Recebe um ou mais MLB IDs, busca o detalhe completo na conta Shinsei
e cria anúncio idêntico na conta AKG preservando SKU (seller_custom_field).
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import requests as _req
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)
router = APIRouter()

BASE_DIR  = Path(__file__).resolve().parent.parent
PAGES_DIR = BASE_DIR / "pages"
ML_API    = "https://api.mercadolibre.com"


# ── Helpers de token ──────────────────────────────────────────────────────────

def _token_shinsei() -> str:
    """Lê o token ML Shinsei via OAuth service (mesmo padrão das rotas existentes)."""
    from services.mercado_livre import MercadoLivreOAuthService
    svc = MercadoLivreOAuthService(str(BASE_DIR))
    result = svc.ler_tokens()
    if not result.get("success"):
        raise ValueError(result.get("error", "Token Shinsei não encontrado — faça login em /ml/login"))
    return result["data"].get("access_token", "")


def _token_akg() -> str:
    """Lê o token ML AKG via OAuth service (mesmo padrão das rotas existentes)."""
    import importlib, sys
    routes_ml = sys.modules.get("routes.mercado_livre") or importlib.import_module("routes.mercado_livre")
    svc = routes_ml._get_akg_oauth()
    result = svc.ler_tokens()
    if not result.get("success"):
        raise ValueError(result.get("error", "Token AKG não encontrado — faça login em /ml/login2"))
    return result["data"].get("access_token", "")


def _hdrs(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── Lógica de cópia ───────────────────────────────────────────────────────────

def _extract_mlb_id(texto: str) -> str:
    """Aceita MLB123456, URL completa ou só o número."""
    texto = texto.strip()
    m = re.search(r"MLB\d+", texto.upper())
    if m:
        return m.group(0)
    m = re.search(r"\d{10,}", texto)
    if m:
        return f"MLB{m.group(0)}"
    return texto.upper()


def _get_item(item_id: str, token: str) -> dict:
    r = _req.get(f"{ML_API}/items/{item_id}", headers=_hdrs(token), timeout=20)
    r.raise_for_status()
    return r.json()


def _get_description(item_id: str, token: str) -> str:
    r = _req.get(f"{ML_API}/items/{item_id}/description", headers=_hdrs(token), timeout=20)
    if r.status_code == 200:
        return r.json().get("plain_text") or r.json().get("text") or ""
    return ""


def _build_payload(item: dict) -> dict:
    """
    Monta o payload para criar o anúncio na conta AKG.
    Sempre cria anúncio TRADICIONAL (não-catálogo): nunca inclui
    catalog_product_id e força catalog_listing=false.
    """

    # Fotos: passa as URLs originais — ML re-hospeda automaticamente
    pictures = [{"source": p["url"]} for p in (item.get("pictures") or []) if p.get("url")]

    # Variações com seller_custom_field (SKU)
    # Remove picture_ids pois os IDs são da conta Shinsei e não existem na AKG
    variacoes = []
    for v in (item.get("variations") or []):
        var = {
            "attribute_combinations": v.get("attribute_combinations") or [],
            "price":                  v.get("price") or item.get("price"),
            "available_quantity":     v.get("available_quantity") or 0,
            "seller_custom_field":    v.get("seller_custom_field") or "",
        }
        variacoes.append(var)

    # Atributos — remove campos somente-leitura que ML rejeita na criação
    SKIP_ATTR_IDS = {
        "SELLER_SKU", "ITEM_CONDITION", "BRAND", "ALPHANUMERIC_MODEL",
        "GTIN", "SELLER_ID", "CATALOG_LISTING",
    }
    atributos = [
        {"id": a["id"], "value_name": a.get("value_name")}
        for a in (item.get("attributes") or [])
        if a.get("id") not in SKIP_ATTR_IDS and a.get("value_name")
    ]

    # listing_type_id: garante tipo tradicional (gold_special ou gold_pro)
    # Nunca usa gold_premium pois esse força catálogo em algumas categorias
    listing_type = item.get("listing_type_id") or "gold_special"
    if listing_type not in ("gold_special", "gold_pro", "bronze", "free"):
        listing_type = "gold_special"

    payload: dict[str, Any] = {
        "title":               item.get("title", ""),
        "category_id":         item.get("category_id", ""),
        "price":               item.get("price"),
        "currency_id":         item.get("currency_id", "BRL"),
        "available_quantity":  item.get("available_quantity", 0),
        "buying_mode":         item.get("buying_mode", "buy_it_now"),
        "listing_type_id":     listing_type,
        "condition":           item.get("condition", "new"),
        "pictures":            pictures,
        "attributes":          atributos,
        "seller_custom_field": item.get("seller_custom_field") or "",
        # Força modo tradicional — nunca catálogo
        "catalog_listing":     False,
    }

    if variacoes:
        payload["variations"] = variacoes

    # Frete
    shipping = item.get("shipping") or {}
    if shipping:
        payload["shipping"] = {
            "mode":          shipping.get("mode", "me2"),
            "local_pick_up": shipping.get("local_pick_up", False),
            "free_shipping": shipping.get("free_shipping", False),
            "logistic_type": shipping.get("logistic_type", "fulfillment"),
        }

    return payload


def _criar_item_akg(payload: dict, token: str) -> dict:
    r = _req.post(f"{ML_API}/items", headers=_hdrs(token),
                  json=payload, timeout=30)
    return {"status_code": r.status_code, "body": r.json()}


def _criar_description_akg(item_id: str, texto: str, token: str):
    if not texto:
        return
    _req.post(f"{ML_API}/items/{item_id}/description",
              headers=_hdrs(token),
              json={"plain_text": texto}, timeout=20)


# ── Endpoint de preview ───────────────────────────────────────────────────────

@router.get("/copiar-ml/preview/{item_id}")
def preview_anuncio(item_id: str):
    """Busca os dados do anúncio Shinsei para exibir preview antes de copiar."""
    mlb = _extract_mlb_id(item_id)
    try:
        tok_s = _token_shinsei()
    except Exception as e:
        return {"ok": False, "erro": f"Token Shinsei indisponível: {e}"}
    try:
        item = _get_item(mlb, tok_s)
    except Exception as e:
        return {"ok": False, "erro": f"Erro ao buscar {mlb}: {e}"}

    return {
        "ok": True,
        "item_id":       mlb,
        "titulo":        item.get("title"),
        "categoria":     item.get("category_id"),
        "preco":         item.get("price"),
        "tipo_anuncio":  item.get("listing_type_id"),
        "condicao":      item.get("condition"),
        "status":        item.get("status"),
        "quantidade":    item.get("available_quantity"),
        "fotos":         len(item.get("pictures") or []),
        "variacoes":     len(item.get("variations") or []),
        "atributos":     len(item.get("attributes") or []),
        "sku":           item.get("seller_custom_field") or "(sem SKU raiz)",
        "skus_variacoes": [
            v.get("seller_custom_field") for v in (item.get("variations") or [])
            if v.get("seller_custom_field")
        ][:10],
    }


# ── Endpoint de cópia ─────────────────────────────────────────────────────────

@router.post("/copiar-ml/copiar")
def copiar_anuncio(body: dict):
    """
    Copia um ou mais anúncios do ML Shinsei → ML AKG.
    Body: { "ids": ["MLB123", "MLB456", ...] }
    """
    ids_raw: list[str] = body.get("ids") or []
    if not ids_raw:
        return {"ok": False, "erro": "Nenhum ID informado"}

    try:
        tok_s = _token_shinsei()
        tok_a = _token_akg()
    except Exception as e:
        return {"ok": False, "erro": f"Tokens indisponíveis: {e}"}

    resultados = []

    for raw in ids_raw:
        mlb = _extract_mlb_id(raw)
        entry: dict = {"id_shinsei": mlb, "ok": False}

        try:
            # 1. Busca detalhe Shinsei
            item = _get_item(mlb, tok_s)
            entry["titulo"] = item.get("title", "")
            entry["sku_raiz"] = item.get("seller_custom_field") or ""

            # 2. Busca descrição
            descricao = _get_description(mlb, tok_s)
            time.sleep(0.3)

            # 3. Monta payload
            payload = _build_payload(item)

            # 4. Cria na AKG
            resp = _criar_item_akg(payload, tok_a)
            entry["status_http"] = resp["status_code"]

            if resp["status_code"] in (200, 201):
                novo_id = resp["body"].get("id")
                entry["ok"] = True
                entry["id_akg"] = novo_id
                entry["msg"] = f"Criado: {novo_id}"
                # 5. Cria descrição
                if novo_id and descricao:
                    _criar_description_akg(novo_id, descricao, tok_a)
            else:
                entry["erro"] = resp["body"].get("message") or str(resp["body"])
                # Detalhes de campo inválido
                causes = resp["body"].get("cause") or []
                if causes:
                    entry["causes"] = [c.get("message") or str(c) for c in causes[:5]]

        except Exception as e:
            entry["erro"] = str(e)

        resultados.append(entry)
        time.sleep(0.5)

    total = len(resultados)
    criados = sum(1 for r in resultados if r["ok"])
    return {
        "ok": True,
        "total": total,
        "criados": criados,
        "erros": total - criados,
        "resultados": resultados,
    }


# ── Página HTML ───────────────────────────────────────────────────────────────

@router.get("/copiar-ml", response_class=HTMLResponse)
def copiar_ml_page():
    f = PAGES_DIR / "copiar_ml.html"
    if f.exists():
        return HTMLResponse(f.read_text(encoding="utf-8"))
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="pages/copiar_ml.html não encontrado.")
