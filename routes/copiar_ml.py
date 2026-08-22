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

def _extract_id(texto: str) -> str:
    """Aceita MLBU/MLB + número, URL completa ou só o número."""
    texto = texto.strip()
    m = re.search(r"MLB[U]?\d+", texto.upper())
    if m:
        return m.group(0)
    m = re.search(r"\d{10,}", texto)
    if m:
        return f"MLB{m.group(0)}"
    return texto.upper()

# Mantém alias para compatibilidade interna
def _extract_mlb_id(texto: str) -> str:
    return _extract_id(texto)


def _get_item(item_id: str, token: str) -> dict:
    r = _req.get(f"{ML_API}/items/{item_id}", headers=_hdrs(token), timeout=20)
    r.raise_for_status()
    return r.json()


def _get_seller_id(token: str) -> str:
    r = _req.get(f"{ML_API}/users/me", headers=_hdrs(token), timeout=10)
    r.raise_for_status()
    return str(r.json()["id"])


def _get_seller_item_ids(seller_id: str, token: str) -> list[str]:
    """Busca todos os IDs de anúncios do vendedor (até 1000)."""
    ids: list[str] = []
    offset = 0
    while True:
        r = _req.get(
            f"{ML_API}/users/{seller_id}/items/search",
            params={"limit": 100, "offset": offset},
            headers=_hdrs(token), timeout=20,
        )
        if r.status_code != 200:
            break
        data = r.json()
        batch = data.get("results") or []
        ids.extend(batch)
        if len(batch) < 100 or len(ids) >= (data.get("paging", {}).get("total") or 0):
            break
        offset += 100
    return ids


def _get_family_items_by_family_id(family_id: str, seller_id: str, token: str) -> list[str]:
    """Busca todos os MLBs do vendedor com o mesmo family_id (paginado)."""
    ids: list[str] = []
    offset = 0
    while True:
        r = _req.get(
            f"{ML_API}/users/{seller_id}/items/search",
            params={"family_id": family_id, "limit": 100, "offset": offset},
            headers=_hdrs(token), timeout=20,
        )
        if r.status_code != 200:
            break
        data = r.json()
        batch = data.get("results") or []
        ids.extend(batch)
        total = (data.get("paging") or {}).get("total", 0)
        if not batch or len(ids) >= total:
            break
        offset += 100
    return ids


def _get_family_items(family_ref: str, token: str) -> list[str]:
    """
    Retorna lista de MLB IDs de todos os membros de uma família Omni.
    Aceita MLBU (family_id) ou MLB (busca o family_id do item e lista irmãos).
    """
    seller_id = _get_seller_id(token)

    # Se recebeu um MLB filho, extrai o family_id dele
    if not family_ref.startswith("MLBU"):
        r0 = _req.get(f"{ML_API}/items/{family_ref}",
                      headers=_hdrs(token), timeout=20)
        if r0.status_code == 200:
            data = r0.json()
            # Multi-variação tradicional: variações inline
            if data.get("variations"):
                return [family_ref]
            fid = data.get("family_id")
            if fid:
                return _get_family_items_by_family_id(fid, seller_id, token)
        return [family_ref]  # fallback: trata como item único

    # Recebeu MLBU diretamente → usa como family_id
    return _get_family_items_by_family_id(family_ref, seller_id, token)


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

    # family_name é obrigatório para categorias Omni (ex: colorações)
    family_name = item.get("family_name")
    if family_name:
        payload["family_name"] = family_name

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

@router.get("/copiar-ml/preview/{item_id:path}")
def preview_anuncio(item_id: str):
    """
    Busca dados do anúncio Shinsei para preview.
    Aceita MLB ou MLBU (família Omni). Para MLBU, enumera todos os filhos.
    """
    raw_id = _extract_id(item_id)
    try:
        tok_s = _token_shinsei()
    except Exception as e:
        return {"ok": False, "erro": f"Token Shinsei indisponível: {e}"}

    # ── Família MLBU: enumera filhos ──────────────────────────────────────────
    if raw_id.startswith("MLBU"):
        try:
            filhos = _get_family_items(raw_id, tok_s)
        except Exception as e:
            return {"ok": False, "erro": f"Erro ao buscar família {raw_id}: {e}"}
        if not filhos:
            return {"ok": False, "erro": f"Família {raw_id} não retornou filhos. Tente um MLB filho diretamente."}

        # Preview do primeiro filho para pegar título/categoria/preço
        try:
            primeiro = _get_item(filhos[0], tok_s)
        except Exception as e:
            return {"ok": False, "erro": f"Erro ao buscar filho {filhos[0]}: {e}"}

        return {
            "ok":           True,
            "tipo":         "familia",
            "item_id":      raw_id,
            "filhos":       filhos,
            "total_filhos": len(filhos),
            "titulo_base":  primeiro.get("title", "").rsplit(" - ", 1)[0],
            "categoria":    primeiro.get("category_id"),
            "preco":        primeiro.get("price"),
            "tipo_anuncio": primeiro.get("listing_type_id"),
            "condicao":     primeiro.get("condition"),
            "status":       primeiro.get("status"),
        }

    # ── Item individual MLB ───────────────────────────────────────────────────
    try:
        item = _get_item(raw_id, tok_s)
    except Exception as e:
        return {"ok": False, "erro": f"Erro ao buscar {raw_id}: {e}"}

    variacoes = item.get("variations") or []
    family_id = item.get("family_id")

    # Se tem family_id e sem variações inline → é anúncio Omni, auto-expande família
    if family_id and not variacoes:
        try:
            filhos = _get_family_items(raw_id, tok_s)  # passa o MLB filho
        except Exception as e:
            filhos = []
        if filhos and len(filhos) > 1:
            titulo_base = item.get("title", "")
            # Remove sufixo da cor do título: "... - 5-99 Castanho Claro" → "..."
            if " - " in titulo_base:
                titulo_base = titulo_base.rsplit(" - ", 1)[0]
            return {
                "ok":           True,
                "tipo":         "familia",
                "item_id":      f"MLBU{family_id}",
                "filhos":       filhos,
                "total_filhos": len(filhos),
                "titulo_base":  titulo_base,
                "categoria":    item.get("category_id"),
                "preco":        item.get("price"),
                "tipo_anuncio": item.get("listing_type_id"),
                "condicao":     item.get("condition"),
                "status":       item.get("status"),
            }

    familia_hint = f"MLBU{family_id}" if family_id else None

    return {
        "ok":           True,
        "tipo":         "item",
        "item_id":      raw_id,
        "titulo":       item.get("title"),
        "categoria":    item.get("category_id"),
        "preco":        item.get("price"),
        "tipo_anuncio": item.get("listing_type_id"),
        "condicao":     item.get("condition"),
        "status":       item.get("status"),
        "quantidade":   item.get("available_quantity"),
        "fotos":        len(item.get("pictures") or []),
        "variacoes":    len(variacoes),
        "atributos":    len(item.get("attributes") or []),
        "sku":          item.get("seller_custom_field") or "(sem SKU raiz)",
        "skus_variacoes": [
            v.get("seller_custom_field") for v in variacoes
            if v.get("seller_custom_field")
        ][:10],
        "familia_hint": familia_hint,  # MLBU pai, se detectado
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

    # Expande famílias MLBU e MLBs com family_id para lista de filhos
    ids_expandidos: list[str] = []
    for raw in ids_raw:
        eid = _extract_id(raw)
        # MLBU direto ou MLB filho de família Omni → expande
        try:
            filhos = _get_family_items(eid, tok_s)
            if len(filhos) > 1 or (len(filhos) == 1 and filhos[0] != eid):
                ids_expandidos.extend(filhos)
                continue
        except Exception:
            pass
        if True:
            ids_expandidos.append(eid)

    for raw in ids_expandidos:
        mlb = _extract_id(raw)
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


# ── Debug ────────────────────────────────────────────────────────────────────

@router.get("/copiar-ml/debug/{item_id:path}")
def debug_item(item_id: str):
    """Retorna campos brutos do ML para diagnóstico de estrutura MLBU/MLB."""
    raw_id = _extract_id(item_id)
    try:
        tok = _token_shinsei()
    except Exception as e:
        return {"erro": str(e)}
    r = _req.get(f"{ML_API}/items/{raw_id}", headers=_hdrs(tok), timeout=20)
    data = r.json()
    keys_interesse = [
        "id", "parent_item_id", "children_ids", "variations", "item_relations",
        "catalog_product_id", "catalog_listing", "listing_type_id",
        "family_name", "family_id", "seller_id", "status",
    ]
    resumo = {k: data.get(k) for k in keys_interesse}
    resumo["_keys_disponiveis"] = list(data.keys())
    return resumo


# ── Página HTML ───────────────────────────────────────────────────────────────

@router.get("/copiar-ml", response_class=HTMLResponse)
def copiar_ml_page():
    f = PAGES_DIR / "copiar_ml.html"
    if f.exists():
        return HTMLResponse(f.read_text(encoding="utf-8"))
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="pages/copiar_ml.html não encontrado.")
