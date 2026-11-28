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

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests
from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter(tags=["estoque-universal"])

DATA_DIR = Path(__file__).parent.parent / "data"

# ─────────────────────────────────────────────────────────────────────────────
# Tokens / helpers
# ─────────────────────────────────────────────────────────────────────────────

BLING_BASE = "https://api.bling.com.br/Api/v3"
ML_BASE    = "https://api.mercadolibre.com"


def _bling_token(empresa: str) -> str:
    """Lê token Bling dos arquivos locais (tokens frescos, atualizados pelo OAuth)."""
    if empresa == "akg":
        f = DATA_DIR / "bling_tokens_akg.json"
        if f.exists():
            try:
                return json.loads(f.read_text()).get("access_token", "")
            except Exception:
                pass
        return os.getenv("BLING_ACCESS_TOKEN_AKG", "")
    # Shinsei: usar BlingClient
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from bling_client import BlingClient
        bc = BlingClient()
        if bc.has_local_tokens():
            return bc._load_tokens().get("access_token", "")
    except Exception:
        pass
    return os.getenv("BLING_ACCESS_TOKEN", "")


def _bling_hdrs(empresa: str) -> dict:
    return {"Authorization": f"Bearer {_bling_token(empresa)}"}


def _ml_token(empresa: str) -> str:
    """Lê token ML dos arquivos locais (tokens frescos, atualizados pelo OAuth).

    NÃO usa os.getenv — a env var é definida no startup do Render e expira
    em ~6h sem redeploy. Os arquivos ml_tokens*.json são atualizados pelo refresh.
    """
    if empresa == "akg":
        f = DATA_DIR / "ml_tokens_akg.json"
    else:
        f = DATA_DIR / "ml_tokens.json"
    if f.exists():
        try:
            return json.loads(f.read_text()).get("access_token", "")
        except Exception:
            pass
    # Fallback para env var se arquivo não existir
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

def _coletar_ml(empresa: str) -> dict[str, dict]:
    """
    Retorna {sku: {"qty": N, "item_ids": ["MLB123", ...]}} para itens ML ativos.
    Um SKU pode ter múltiplos anúncios (item_ids) — todos são registrados.
    """
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

    result: dict[str, dict] = {}
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
                    params={"ids": ids_str, "attributes": "id,seller_custom_field,available_quantity"},
                    timeout=25,
                )
                if not rb.ok:
                    continue
                for entry in rb.json():
                    item = entry.get("body", {}) if "body" in entry else entry
                    mlb_id = str(item.get("id") or "").strip()
                    sku    = str(item.get("seller_custom_field") or "").strip()
                    qty    = int(item.get("available_quantity", 0) or 0)
                    if not sku:
                        continue
                    if sku not in result:
                        result[sku] = {"qty": 0, "item_ids": [], "qtys": {}}
                    # Cada anúncio tem o estoque do SKU inteiro — NÃO somar anúncios
                    # (bug anterior: 2 anúncios do mesmo SKU mostravam o dobro)
                    if mlb_id:
                        result[sku]["qtys"][mlb_id] = qty
                    result[sku]["qty"] = max(result[sku]["qtys"].values() or [qty])
                    if mlb_id and mlb_id not in result[sku]["item_ids"]:
                        result[sku]["item_ids"].append(mlb_id)
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
    import re
    # {sku: {variant_id: qty}} — dedupe por variante (evita somar a mesma variante 2x)
    por_sku: dict[str, dict] = {}
    page_info = None
    vistos: set = set()

    while True:
        params: dict = {"limit": 250, "fields": "id,status,variants"}
        if page_info:
            params = {"limit": 250, "page_info": page_info, "fields": "id,status,variants"}
        try:
            r = requests.get(f"{base}/products.json", headers=hdrs, params=params, timeout=30)
            if not r.ok:
                break
            for prod in r.json().get("products", []):
                if prod.get("status") and prod.get("status") != "active":
                    continue
                for v in prod.get("variants", []):
                    sku = str(v.get("sku") or "").strip()
                    qty = int(v.get("inventory_quantity", 0) or 0)
                    if sku:
                        por_sku.setdefault(sku, {})[v.get("id")] = qty
            # Paginação via Link header — pegar SOMENTE o link rel="next"
            # (bug anterior: regex gulosa pegava o page_info do rel="previous",
            #  relendo páginas e somando estoque centenas de vezes)
            page_info = None
            for parte in r.headers.get("Link", "").split(","):
                if 'rel="next"' in parte:
                    m = re.search(r"page_info=([^&>]+)", parte)
                    page_info = m.group(1) if m else None
            if page_info in vistos:
                break
            if page_info:
                vistos.add(page_info)
        except Exception:
            break
        if not page_info:
            break
        time.sleep(0.2)

    # Uma variante por SKU é o normal; com várias, usa a de maior estoque
    return {sku: max(vs.values()) for sku, vs in por_sku.items()}


# ─────────────────────────────────────────────────────────────────────────────
# Coleta Shopee Shinsei
# ─────────────────────────────────────────────────────────────────────────────

def _coletar_shopee() -> dict[str, int]:
    """
    Retorna {sku: stock} da Shopee Shinsei.
    Usa services.shopee (token do arquivo + auto-refresh) — a versão anterior lia
    SHOPEE_ACCESS_TOKEN da env var, que fica velha porque o token rotaciona (voltava vazio).
    Itens com variações: SKU e estoque vêm de get_model_list (model_sku).
    """
    try:
        from services.shopee import ShopeeService, obter_token_shopee, API_BASE
        obter_token_shopee()          # renova se expirado
        svc = ShopeeService()
    except Exception as e:
        logger.warning("estoque_universal Shopee: sem token (%s)", e)
        return {}

    import httpx

    def _stock_model(m: dict) -> int:
        si = m.get("stock_info_v2") or {}
        summ = si.get("summary_info") or {}
        if "total_available_stock" in summ:
            return int(summ.get("total_available_stock") or 0)
        return int(sum(int(s.get("stock") or 0) for s in (si.get("seller_stock") or [])))

    result: dict[str, int] = {}
    offset = 0
    while True:
        data = svc.listar_produtos(offset=offset, page_size=100)
        resp = (data or {}).get("response") or {}
        items = resp.get("item") or []
        if not items:
            if data.get("error"):
                logger.warning("estoque_universal Shopee get_item_list: %s", data.get("message") or data.get("error"))
            break
        ids = [i["item_id"] for i in items]
        for i in range(0, len(ids), 50):
            info = svc.obter_info_items(ids[i:i+50])
            for it in ((info or {}).get("response") or {}).get("item_list") or []:
                if it.get("has_model"):
                    path = "/api/v2/product/get_model_list"
                    params = svc._params_auth(path)
                    params["item_id"] = int(it["item_id"])
                    try:
                        with httpx.Client(timeout=20) as c:
                            md = c.get(f"{API_BASE}/product/get_model_list", params=params).json()
                        for m in (md.get("response") or {}).get("model") or []:
                            sku = str(m.get("model_sku") or "").strip()
                            if sku:
                                result[sku] = max(result.get(sku, 0), _stock_model(m))
                    except Exception as e:
                        logger.warning("estoque_universal Shopee models %s: %s", it.get("item_id"), e)
                    time.sleep(0.1)
                else:
                    sku = str(it.get("item_sku") or "").strip()
                    if sku:
                        result[sku] = max(result.get(sku, 0), _stock_model(it))
            time.sleep(0.2)
        if not resp.get("has_next_page"):
            break
        offset = resp.get("next_offset") or (offset + 100)
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

        # Canais e seus saldos — ML retorna {"qty": N, "item_ids": [...]}
        akg_saldo      = akg.get(sku, {}).get("saldo")          # None = não cadastrado
        ml_shin_info   = ml_shin.get(sku)
        ml_akg_info    = ml_akg_data.get(sku)
        # Exibe o anúncio mais distante do Bling (o que precisa de atenção)
        def _pior(info):
            if not info:
                return None
            qs = list((info.get("qtys") or {}).values()) or [info["qty"]]
            return max(qs, key=lambda q: abs(q - shin_saldo))
        ml_shin_qty    = _pior(ml_shin_info)
        ml_akg_qty     = _pior(ml_akg_info)
        ml_shin_ids    = ml_shin_info["item_ids"] if ml_shin_info else []
        ml_akg_ids     = ml_akg_info["item_ids"]  if ml_akg_info  else []
        shopify_qty    = shopify.get(sku)
        shopee_qty     = shopee.get(sku)

        # Detectar divergências (canais presentes mas com valor diferente)
        canais_divergentes = []
        # Bling AKG: desde 10/10 o estoque físico mora na Shinsei Geral e a rota 6
        # (AKG vende → sai da Shinsei) mantém a AKG Geral zerada. Esperado = 0;
        # comparar com o saldo da Shinsei marcava ~690 falsos positivos.
        if akg_saldo is not None and akg_saldo != 0:
            canais_divergentes.append("bling_akg")
        # ML: diverge se QUALQUER anúncio do SKU estiver diferente do Bling
        if ml_shin_info and any(q != shin_saldo for q in (ml_shin_info.get("qtys") or {"_": ml_shin_qty}).values()):
            canais_divergentes.append("ml_shinsei")
        if ml_akg_info and any(q != shin_saldo for q in (ml_akg_info.get("qtys") or {"_": ml_akg_qty}).values()):
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
            "ml_shinsei_ids": ml_shin_ids,   # MLBs do canal Shinsei com divergência
            "ml_akg_ids":     ml_akg_ids,    # MLBs do canal AKG com divergência
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
