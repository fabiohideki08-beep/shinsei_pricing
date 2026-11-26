# -*- coding: utf-8 -*-
"""
routes/estoque_universal.py — Estoque Universal Shinsei Pricing
===============================================================
Auditoria de sincronização de estoque em todos os canais de venda.

Fonte de verdade: Bling Shinsei (saldoVirtualTotal).
Canais auditados: Bling AKG, ML Shinsei, ML AKG, Shopee Shinsei, Shopify.

Endpoints:
  POST /estoque-universal/iniciar  — dispara job assíncrono de coleta
  GET  /estoque-universal/resultado — retorna resultado/andamento do job
  GET  /estoque-universal/status   — status resumido do último job
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import requests
from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter(tags=["estoque-universal"])

# ─────────────────────────────────────────────────────────────────────────────
# Tokens / helpers
# ─────────────────────────────────────────────────────────────────────────────

BLING_BASE = "https://api.bling.com.br/Api/v3"
ML_BASE    = "https://api.mercadolibre.com"


def _bling_token(empresa: str) -> str:
    if empresa == "akg":
        return os.getenv("BLING_ACCESS_TOKEN_AKG", "")
    return os.getenv("BLING_ACCESS_TOKEN", "")


def _bling_hdrs(empresa: str) -> dict:
    return {"Authorization": f"Bearer {_bling_token(empresa)}"}


def _ml_token(empresa: str) -> str:
    if empresa == "akg":
        return os.getenv("ML_ACCESS_TOKEN_AKG", "")
    return os.getenv("ML_ACCESS_TOKEN", "")


def _shopify_hdrs() -> dict:
    return {
        "X-Shopify-Access-Token": os.getenv("SHOPIFY_ACCESS_TOKEN", ""),
        "Content-Type": "application/json",
    }


def _shopee_token_shinsei() -> dict:
    return {
        "access_token": os.getenv("SHOPEE_ACCESS_TOKEN", ""),
        "shop_id":      os.getenv("SHOPEE_SHOP_ID", ""),
        "partner_id":   os.getenv("SHOPEE_PARTNER_ID", ""),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Cache do job
# ─────────────────────────────────────────────────────────────────────────────

_eu_cache: dict = {}


# ─────────────────────────────────────────────────────────────────────────────
# Coleta Bling (paginada)
# ─────────────────────────────────────────────────────────────────────────────

def _coletar_bling(empresa: str) -> dict[str, dict]:
    """Retorna {sku: {saldo, nome, formato, id}} para todos os produtos ativos."""
    result: dict[str, dict] = {}
    pagina = 1
    while True:
        for _ in range(3):
            try:
                r = requests.get(
                    f"{BLING_BASE}/produtos",
                    headers=_bling_hdrs(empresa),
                    params={"pagina": pagina, "limite": 100, "situacao": "A", "tipo": "P"},
                    timeout=25,
                )
                if r.status_code == 429:
                    time.sleep(3)
                    continue
                break
            except Exception:
                time.sleep(2)
        if not r.ok:
            break
        prods = r.json().get("data", [])
        if not prods:
            break
        for p in prods:
            sku = str(p.get("codigo") or "").strip()
            if not sku:
                continue
            est = p.get("estoque") or {}
            saldo = float(est.get("saldoVirtualTotal", 0) or 0)
            result[sku] = {
                "saldo": saldo,
                "nome": str(p.get("nome") or "")[:80],
                "formato": p.get("formato", ""),
                "id": p.get("id"),
            }
        pagina += 1
        time.sleep(0.3)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Coleta ML (todos os items ativos)
# ─────────────────────────────────────────────────────────────────────────────

def _coletar_ml(empresa: str) -> dict[str, int]:
    """Retorna {sku: available_quantity} para itens ML ativos."""
    token = _ml_token(empresa)
    if not token:
        return {}

    # Descobrir user_id
    try:
        me = requests.get(f"{ML_BASE}/users/me",
                          headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if not me.ok:
            return {}
        user_id = me.json().get("id")
    except Exception:
        return {}

    result: dict[str, int] = {}
    offset = 0
    limit = 100
    while True:
        try:
            r = requests.get(
                f"{ML_BASE}/users/{user_id}/items/search",
                headers={"Authorization": f"Bearer {token}"},
                params={"status": "active", "offset": offset, "limit": limit},
                timeout=25,
            )
        except Exception:
            break
        if not r.ok:
            break
        data = r.json()
        item_ids = data.get("results", [])
        if not item_ids:
            break

        # Buscar detalhes em batch (até 20 por vez)
        for i in range(0, len(item_ids), 20):
            batch = item_ids[i:i+20]
            ids_str = ",".join(batch)
            try:
                rb = requests.get(
                    f"{ML_BASE}/items",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"ids": ids_str, "attributes": "id,seller_custom_field,available_quantity,variations"},
                    timeout=25,
                )
                if not rb.ok:
                    continue
                for entry in rb.json():
                    item = entry.get("body", {}) if "body" in entry else entry
                    sku = str(item.get("seller_custom_field") or "").strip()
                    qty = int(item.get("available_quantity", 0) or 0)
                    if sku:
                        # Se já existe (vários items com mesmo SKU), soma
                        result[sku] = result.get(sku, 0) + qty
                    # Variações
                    for v in (item.get("variations") or []):
                        for attr in (v.get("attribute_combinations") or []):
                            pass  # SKU de variação fica no seller_custom_field do pai
            except Exception:
                pass
            time.sleep(0.1)

        total = data.get("paging", {}).get("total", 0)
        offset += limit
        if offset >= total:
            break
        time.sleep(0.3)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Coleta Shopify
# ─────────────────────────────────────────────────────────────────────────────

def _coletar_shopify() -> dict[str, int]:
    """Retorna {sku: inventory_quantity} para variantes Shopify ativas."""
    store = os.getenv("SHOPIFY_SHOP", "pknw4n-eg")
    base = f"https://{store}.myshopify.com/admin/api/2024-01"
    hdrs = _shopify_hdrs()
    result: dict[str, int] = {}
    page_info = None

    while True:
        params: dict = {"limit": 250, "fields": "id,variants"}
        if page_info:
            params = {"limit": 250, "page_info": page_info}
        try:
            r = requests.get(f"{base}/products.json", headers=hdrs, params=params, timeout=30)
            if not r.ok:
                break
            for prod in r.json().get("products", []):
                for v in prod.get("variants", []):
                    sku = str(v.get("sku") or "").strip()
                    qty = int(v.get("inventory_quantity", 0) or 0)
                    if sku:
                        result[sku] = result.get(sku, 0) + qty
            # Paginação via Link header
            link = r.headers.get("Link", "")
            if 'rel="next"' in link:
                import re
                m = re.search(r'page_info=([^&>]+).*rel="next"', link)
                page_info = m.group(1) if m else None
            else:
                page_info = None
        except Exception:
            break
        if not page_info:
            break
        time.sleep(0.2)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Coleta Shopee Shinsei
# ─────────────────────────────────────────────────────────────────────────────

def _coletar_shopee() -> dict[str, int]:
    """Retorna {sku: stock} via Shopee API."""
    cfg = _shopee_token_shinsei()
    access_token = cfg.get("access_token", "")
    shop_id_str  = cfg.get("shop_id", "")
    partner_id_str = cfg.get("partner_id", "")
    if not access_token or not shop_id_str:
        return {}

    try:
        shop_id    = int(shop_id_str)
        partner_id = int(partner_id_str) if partner_id_str else 0
    except ValueError:
        return {}

    shopee_host = "https://partner.shopeemobile.com"
    result: dict[str, int] = {}
    offset = 0
    page_size = 100

    while True:
        import hashlib, hmac
        ts = int(time.time())
        path = "/api/v2/product/get_item_list"
        base_str = f"{partner_id}{path}{ts}{access_token}{shop_id}"
        sign = hmac.new(
            os.getenv("SHOPEE_PARTNER_KEY", "").encode(),
            base_str.encode(), hashlib.sha256
        ).hexdigest()
        params = {
            "partner_id": partner_id, "shop_id": shop_id,
            "access_token": access_token, "timestamp": ts, "sign": sign,
            "offset": offset, "page_size": page_size,
            "item_status": "NORMAL",
        }
        try:
            r = requests.get(f"{shopee_host}{path}", params=params, timeout=20)
            if not r.ok:
                break
            data = r.json()
            items = data.get("response", {}).get("item", []) or []
            if not items:
                break

            # Buscar estoque de cada item
            item_ids = [i["item_id"] for i in items]
            for chunk in [item_ids[i:i+50] for i in range(0, len(item_ids), 50)]:
                ts2 = int(time.time())
                path2 = "/api/v2/product/get_item_base_info"
                base_str2 = f"{partner_id}{path2}{ts2}{access_token}{shop_id}"
                sign2 = hmac.new(
                    os.getenv("SHOPEE_PARTNER_KEY", "").encode(),
                    base_str2.encode(), hashlib.sha256
                ).hexdigest()
                params2 = {
                    "partner_id": partner_id, "shop_id": shop_id,
                    "access_token": access_token, "timestamp": ts2, "sign": sign2,
                    "item_id_list": ",".join(str(i) for i in chunk),
                    "need_tax_info": False, "need_complaint_policy": False,
                }
                try:
                    r2 = requests.get(f"{shopee_host}{path2}", params=params2, timeout=20)
                    if r2.ok:
                        for item in r2.json().get("response", {}).get("item_list", []) or []:
                            sku = str(item.get("item_sku") or "").strip()
                            stock = int((item.get("stock_info_v2") or {}).get("summary_info", {}).get("total_available_stock", 0) or 0)
                            if sku:
                                result[sku] = result.get(sku, 0) + stock
                except Exception:
                    pass
                time.sleep(0.2)

            has_more = data.get("response", {}).get("has_next_page", False)
            if not has_more:
                break
            offset += page_size
        except Exception:
            break
        time.sleep(0.3)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Job principal
# ─────────────────────────────────────────────────────────────────────────────

def _job_estoque_universal():
    _eu_cache.clear()
    _eu_cache.update({
        "status": "em_andamento",
        "iniciado_em": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "etapa": "coletando Bling Shinsei",
        "total_skus_shinsei": 0,
        "total_divergencias": 0,
        "canais": {},
        "skus": [],
    })

    # 1. Bling Shinsei (fonte de verdade)
    _eu_cache["etapa"] = "coletando Bling Shinsei"
    shinsei = _coletar_bling("shinsei")
    _eu_cache["total_skus_shinsei"] = len(shinsei)
    _eu_cache["canais"]["bling_shinsei"] = {"total": len(shinsei), "ok": True}

    # 2. Bling AKG
    _eu_cache["etapa"] = "coletando Bling AKG"
    akg = _coletar_bling("akg")
    _eu_cache["canais"]["bling_akg"] = {"total": len(akg), "ok": True}

    # 3. ML Shinsei
    _eu_cache["etapa"] = "coletando ML Shinsei"
    ml_shin = _coletar_ml("shinsei")
    _eu_cache["canais"]["ml_shinsei"] = {"total": len(ml_shin), "ok": bool(ml_shin)}

    # 4. ML AKG
    _eu_cache["etapa"] = "coletando ML AKG"
    ml_akg_data = _coletar_ml("akg")
    _eu_cache["canais"]["ml_akg"] = {"total": len(ml_akg_data), "ok": bool(ml_akg_data)}

    # 5. Shopify
    _eu_cache["etapa"] = "coletando Shopify"
    shopify = _coletar_shopify()
    _eu_cache["canais"]["shopify"] = {"total": len(shopify), "ok": bool(shopify)}

    # 6. Shopee Shinsei
    _eu_cache["etapa"] = "coletando Shopee"
    shopee = _coletar_shopee()
    _eu_cache["canais"]["shopee"] = {"total": len(shopee), "ok": bool(shopee)}

    # 7. Cruzamento — iterar sobre todos os SKUs Shinsei
    _eu_cache["etapa"] = "cruzando dados"
    skus_result = []
    divergencias = 0

    for sku, shin_info in shinsei.items():
        shin_saldo = shin_info["saldo"]

        # Canais e seus saldos
        akg_saldo      = akg.get(sku, {}).get("saldo")     # None = não cadastrado
        ml_shin_qty    = ml_shin.get(sku)                   # None = não listado
        ml_akg_qty     = ml_akg_data.get(sku)
        shopify_qty    = shopify.get(sku)
        shopee_qty     = shopee.get(sku)

        # Detectar divergências (canais presentes mas com valor diferente)
        canais_divergentes = []
        if akg_saldo is not None and akg_saldo != shin_saldo:
            canais_divergentes.append("bling_akg")
        if ml_shin_qty is not None and ml_shin_qty != shin_saldo:
            canais_divergentes.append("ml_shinsei")
        if ml_akg_qty is not None and ml_akg_qty != shin_saldo:
            canais_divergentes.append("ml_akg")
        if shopify_qty is not None and shopify_qty != shin_saldo:
            canais_divergentes.append("shopify")
        if shopee_qty is not None and shopee_qty != shin_saldo:
            canais_divergentes.append("shopee")

        # Canais onde o SKU não foi encontrado
        canais_ausentes = []
        if akg_saldo is None:
            canais_ausentes.append("bling_akg")
        if ml_shin_qty is None:
            canais_ausentes.append("ml_shinsei")
        if ml_akg_qty is None:
            canais_ausentes.append("ml_akg")
        if shopify_qty is None:
            canais_ausentes.append("shopify")
        if shopee_qty is None:
            canais_ausentes.append("shopee")

        tem_divergencia = bool(canais_divergentes)
        if tem_divergencia:
            divergencias += 1

        skus_result.append({
            "sku": sku,
            "nome": shin_info["nome"],
            "formato": shin_info["formato"],
            "bling_shinsei": shin_saldo,
            "bling_akg":     akg_saldo,
            "ml_shinsei":    ml_shin_qty,
            "ml_akg":        ml_akg_qty,
            "shopify":       shopify_qty,
            "shopee":        shopee_qty,
            "divergencias":  canais_divergentes,
            "ausentes":      canais_ausentes,
            "tem_divergencia": tem_divergencia,
        })

    _eu_cache["total_divergencias"] = divergencias
    _eu_cache["skus"] = skus_result
    _eu_cache["status"] = "concluido"
    _eu_cache["etapa"] = "concluido"
    _eu_cache["concluido_em"] = time.strftime("%Y-%m-%dT%H:%M:%S")


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/estoque-universal/iniciar")
def iniciar_estoque_universal():
    """
    Dispara o job de coleta e cruzamento de estoque em todos os canais.
    Pode demorar 3–10 minutos dependendo do volume.
    Acompanhe em GET /estoque-universal/resultado
    """
    if _eu_cache.get("status") == "em_andamento":
        return {"ok": False, "msg": "Job já em andamento", "etapa": _eu_cache.get("etapa")}

    t = threading.Thread(target=_job_estoque_universal, daemon=True)
    t.start()
    return {"ok": True, "msg": "Job iniciado em background", "resultado_em": "/estoque-universal/resultado"}


@router.get("/estoque-universal/resultado")
def resultado_estoque_universal(apenas_divergencias: bool = False):
    """
    Retorna o resultado do último job de estoque universal.
    apenas_divergencias=true filtra apenas SKUs com diferença entre canais.
    """
    if not _eu_cache:
        return {"ok": False, "msg": "Nenhum job iniciado. Use POST /estoque-universal/iniciar"}

    resp = {k: v for k, v in _eu_cache.items() if k != "skus"}
    skus = _eu_cache.get("skus", [])
    if apenas_divergencias:
        skus = [s for s in skus if s.get("tem_divergencia")]
    resp["skus"] = skus
    resp["total_retornado"] = len(skus)
    return {"ok": True, **resp}


@router.get("/estoque-universal/status")
def status_estoque_universal():
    """Retorna apenas o status/progresso do job (sem os dados dos SKUs)."""
    if not _eu_cache:
        return {"ok": False, "msg": "Nenhum job iniciado"}
    return {
        "ok": True,
        "status": _eu_cache.get("status"),
        "etapa": _eu_cache.get("etapa"),
        "total_skus_shinsei": _eu_cache.get("total_skus_shinsei", 0),
        "total_divergencias": _eu_cache.get("total_divergencias", 0),
        "canais": _eu_cache.get("canais", {}),
        "iniciado_em": _eu_cache.get("iniciado_em"),
        "concluido_em": _eu_cache.get("concluido_em"),
    }
