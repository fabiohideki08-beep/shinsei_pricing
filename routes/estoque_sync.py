# -*- coding: utf-8 -*-
"""
routes/estoque_sync.py — Shinsei Pricing
=========================================
Motor de sincronização de estoque em tempo real: Bling → Shopify + ML + Shopee + Amazon.

Fluxo:
  1. Bling detecta mudança de estoque → dispara webhook POST /webhooks/bling
  2. app.py chama sync_estoque_bling(payload) em background task
  3. Este módulo extrai (SKU, estoque) do payload
  4. Atualiza Shopify via inventory_levels/set.json  (lazy cache por SKU)
  5. Atualiza ML via PUT /items/{id}                 (cache SKU→item_id, TTL 30min)
  6. Atualiza Shopee via /product/update_stock       (mapeamento data/shopee_mapeamento.json)
  7. Atualiza Amazon via Listings Items API PATCH    (sellerSku = SKU Bling, FBM)
  8. Registra resultado em data/sync_estoque_log.json (ring buffer de 500 entradas)

Rotas expostas:
  GET  /estoque-sync/status        — estado do motor + stats dos canais
  GET  /estoque-sync/log           — últimas N entradas do log de sync
  POST /estoque-sync/rebuild-cache — força rebuild dos caches de SKU (ML)
  POST /estoque-sync/testar        — testa sync manual de um SKU
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from fastapi import APIRouter, BackgroundTasks, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/estoque-sync", tags=["estoque-sync"])

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR                  = Path(__file__).parent.parent
DATA_DIR                  = BASE_DIR / "data"
SYNC_LOG_PATH             = DATA_DIR / "sync_estoque_log.json"
SHOPIFY_CACHE_PATH        = DATA_DIR / "shopify_sku_cache.json"
ML_CACHE_PATH             = DATA_DIR / "ml_sku_cache.json"
SHOPIFY_CONFIG_PATH       = DATA_DIR / "shopify_config.json"
ML_TOKENS_PATH            = DATA_DIR / "ml_tokens.json"
SHOPEE_MAPEAMENTO_PATH    = DATA_DIR / "shopee_mapeamento.json"
AMAZON_TOKENS_PATH        = DATA_DIR / "amazon_tokens.json"

# ── Constantes ────────────────────────────────────────────────────────────────
SHOPIFY_STORE        = os.getenv("SHOPIFY_SHOP", "pknw4n-eg")
SHOPIFY_API_VERSION  = "2024-01"
AMAZON_MARKETPLACE   = os.getenv("AMAZON_MARKETPLACE_ID", "A2Q3Y263D00KWC")
AMAZON_SP_BASE       = "https://sellingpartnerapi-na.amazon.com"
AMAZON_LWA_URL       = "https://api.amazon.com/auth/o2/token"
CACHE_TTL_S          = 1800          # 30 min entre rebuilds
MAX_LOG_ENTRIES      = 500
MAX_RETRY            = 3

# ── Locks ─────────────────────────────────────────────────────────────────────
_shopify_lock       = threading.Lock()
_ml_lock            = threading.Lock()
_amazon_lock        = threading.Lock()   # protege renovação de token LWA
_bling_id_map_lock  = threading.Lock()  # protege cache Bling ID → SKU

# ── Cache Bling produto_id → sku (para webhooks v3 sem codigo) ───────────────
# Persistido em data/bling_id_sku_cache.json
BLING_ID_SKU_CACHE_PATH = DATA_DIR / "bling_id_sku_cache.json"
_bling_id_sku_map: dict[str, str] = {}   # {str(produto_id): "codigo"}

# ── Estado interno (status do motor) ─────────────────────────────────────────
_estado = {
    "syncs_realizados":  0,
    "syncs_ok":          0,
    "syncs_falha":       0,
    "ultimo_sync_at":    None,
    "ultimo_sku":        None,
    "shopify_cache_ts":  None,
    "shopify_cache_size": 0,
    "ml_cache_ts":       None,
    "ml_cache_size":     0,
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers JSON
# ─────────────────────────────────────────────────────────────────────────────

def _load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _save_json(path: Path, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_log(entry: dict):
    log = _load_json(SYNC_LOG_PATH, [])
    if not isinstance(log, list):
        log = []
    log.insert(0, {"when": datetime.utcnow().isoformat(), **entry})
    _save_json(SYNC_LOG_PATH, log[:MAX_LOG_ENTRIES])


# ─────────────────────────────────────────────────────────────────────────────
# Parser do payload Bling  (suporta múltiplos formatos v2/v3)
# ─────────────────────────────────────────────────────────────────────────────

def _bling_id_cache_load() -> dict:
    """Carrega o cache Bling ID→SKU do disco."""
    return _load_json(BLING_ID_SKU_CACHE_PATH, {})


def _bling_id_cache_save(cache: dict):
    """Persiste o cache Bling ID→SKU no disco."""
    _save_json(BLING_ID_SKU_CACHE_PATH, cache)


def _bling_id_cache_refresh():
    """
    Reconstrói o cache completo Bling produto_id → codigo.
    Percorre todos os produtos Bling e indexa por ID.
    Chamado no startup em background e via rebuild-cache.
    """
    try:
        from bling_client import BlingClient
        client = BlingClient()
        with _bling_id_map_lock:
            cache = _bling_id_cache_load()

        page = 1
        total_added = 0
        while True:
            try:
                resultado = client.list_products(page=page, limit=100)
            except Exception as exc:
                logger.warning("[SYNC] Erro ao listar produtos Bling (pág %d): %s", page, exc)
                break
            # Bling retorna {"data": [...], ...}
            if isinstance(resultado, dict):
                items = resultado.get("data") or []
            elif isinstance(resultado, list):
                items = resultado
            else:
                items = []
            if not items:
                break
            with _bling_id_map_lock:
                for p in items:
                    pid = str(p.get("id") or "").strip()
                    codigo = str(p.get("codigo") or p.get("code") or "").strip()
                    if pid and codigo and pid not in cache:
                        cache[pid] = codigo
                        _bling_id_sku_map[pid] = codigo
                        total_added += 1
            page += 1
            if len(items) < 100:
                break

        with _bling_id_map_lock:
            _bling_id_cache_save(cache)
            _bling_id_sku_map.update(cache)

        logger.info("[SYNC] Cache Bling ID→SKU: %d entradas totais (%d novas)",
                    len(cache), total_added)
    except Exception as exc:
        logger.warning("[SYNC] Erro ao rebuildar cache Bling ID→SKU: %s", exc)


def _bling_lookup_codigo(produto_id: int) -> Optional[str]:
    """
    Busca o SKU (codigo) de um produto Bling pelo ID interno.
    Primeiro verifica o cache em memória/disco. Se não encontrar,
    tenta buscar diretamente na API Bling e salva no cache.
    """
    pid = str(produto_id)

    # 1) Cache em memória (mais rápido)
    with _bling_id_map_lock:
        cached = _bling_id_sku_map.get(pid)
    if cached:
        logger.info("[SYNC] Cache hit Bling ID=%s → codigo=%s", pid, cached)
        return cached

    # 2) Cache em disco (persiste entre deploys)
    disk_cache = _bling_id_cache_load()
    if pid in disk_cache:
        codigo = disk_cache[pid]
        with _bling_id_map_lock:
            _bling_id_sku_map[pid] = codigo
        logger.info("[SYNC] Cache disco Bling ID=%s → codigo=%s", pid, codigo)
        return codigo

    # 3) Busca na API Bling (pode falhar se token expirado)
    try:
        from bling_client import BlingClient
        client = BlingClient()
        prod = client.get_product(produto_id)
        if isinstance(prod, dict):
            codigo = str(prod.get("codigo") or prod.get("code") or "").strip()
            if codigo:
                logger.info("[SYNC] API Bling ID=%s → codigo=%s", pid, codigo)
                # Salva no cache para uso futuro
                with _bling_id_map_lock:
                    _bling_id_sku_map[pid] = codigo
                    disk_cache[pid] = codigo
                    _bling_id_cache_save(disk_cache)
                return codigo
    except Exception as exc:
        logger.warning("[SYNC] Erro ao buscar codigo Bling ID=%s: %s", pid, exc)
    return None


def _parse_payload(payload: dict) -> Optional[tuple[str, int]]:
    """
    Tenta extrair (sku, estoque_virtual) do webhook Bling.
    Suporta múltiplos formatos de payload do Bling v2 e v3.

    Formato v3 (virtual_stock.updated): payload.data.produto apenas tem "id"
    (sem "codigo"). Nesse caso, faz lookup via API Bling.

    Retorna (sku, estoque) ou None se não conseguir.
    """
    # Normaliza wrapper: pode vir como {"dados": {...}} ou {"data": {...}}
    dados = payload.get("dados") or payload.get("data") or payload

    # O produto pode estar em dados["produto"] ou diretamente em dados
    produto = dados.get("produto") or dados if isinstance(dados, dict) else {}

    # ── SKU ───────────────────────────────────────────────────────────────────
    sku = str(
        produto.get("codigo") or produto.get("code") or
        produto.get("sku")   or dados.get("codigo") or ""
    ).strip()

    # Bling v3: payload só tem produto.id (sem codigo) → lookup via API
    if not sku and isinstance(produto, dict) and produto.get("id"):
        sku = _bling_lookup_codigo(int(produto["id"])) or ""

    if not sku:
        return None

    # ── Estoque ───────────────────────────────────────────────────────────────
    estoque: Optional[float] = None

    # Formato v3 virtual_stock.updated / stock.created: saldo total direto em dados
    # (usa is not None para aceitar 0 como valor válido)
    if estoque is None:
        sv = dados.get("saldoVirtualTotal")
        sf = dados.get("saldoFisicoTotal")
        if sv is not None:
            estoque = sv
        elif sf is not None:
            estoque = sf

    # Formato 1: estoque como dict com saldoVirtualTotal
    estoque_obj = produto.get("estoque") or {}
    if isinstance(estoque_obj, dict) and estoque is None:
        estoque = (
            estoque_obj.get("saldoVirtualTotal") or
            estoque_obj.get("saldoFisicoTotal")
        )

    # Formato 2: lista de estoques por depósito
    if estoque is None:
        estoques = produto.get("estoques") or []
        if isinstance(estoques, list) and estoques:
            estoque = sum(
                float(e.get("saldoVirtual") or e.get("quantidade") or 0)
                for e in estoques
            )

    # Formato 3: saldo inline no produto
    if estoque is None:
        saldo = produto.get("saldo") or {}
        estoque = (
            saldo.get("virtual") if isinstance(saldo, dict) else None
        ) or produto.get("saldoVirtual") or produto.get("quantidade")

    # Formato 4: campo direto no payload raiz
    if estoque is None:
        estoque = (
            payload.get("quantidade") or
            payload.get("estoque")    or
            dados.get("quantidade")
        )

    if estoque is None:
        return None

    return sku, max(0, int(float(estoque)))


# ─────────────────────────────────────────────────────────────────────────────
# Shopify helpers
# ─────────────────────────────────────────────────────────────────────────────

def _shopify_token() -> str:
    # Prioridade: variável de ambiente > arquivo de config
    token = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
    if token:
        return token
    cfg = _load_json(SHOPIFY_CONFIG_PATH, {})
    return cfg.get("access_token", "")


def _shopify_headers() -> dict:
    return {
        "X-Shopify-Access-Token": _shopify_token(),
        "Content-Type": "application/json",
    }


def _shopify_get_location_id() -> Optional[int]:
    """Busca o primeiro location_id ativo (com cache persistente)."""
    cache = _load_json(SHOPIFY_CACHE_PATH, {})
    if cache.get("location_id") and time.time() - cache.get("location_ts", 0) < CACHE_TTL_S * 4:
        return cache["location_id"]
    try:
        url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/locations.json"
        r = requests.get(url, headers=_shopify_headers(), timeout=15)
        if r.status_code == 200:
            locs = [l for l in r.json().get("locations", []) if l.get("active")]
            if locs:
                lid = locs[0]["id"]
                cache["location_id"] = lid
                cache["location_ts"] = time.time()
                _save_json(SHOPIFY_CACHE_PATH, cache)
                return lid
    except Exception as e:
        logger.warning("[SYNC-SHOPIFY] Erro ao buscar location: %s", e)
    return None


def _shopify_lookup_sku(sku: str) -> Optional[dict]:
    """
    Busca um variant pelo SKU diretamente na API Shopify.
    Usa o endpoint variants.json?sku=X (rápido, ~200ms).
    Resultado é salvo no cache para reuso.
    """
    url = (f"https://{SHOPIFY_STORE}.myshopify.com"
           f"/admin/api/{SHOPIFY_API_VERSION}/variants.json")
    params = {"sku": sku, "limit": 5, "fields": "id,sku,inventory_item_id,product_id"}
    try:
        r = requests.get(url, params=params, headers=_shopify_headers(), timeout=15)
        if r.status_code == 200:
            for v in r.json().get("variants", []):
                if str(v.get("sku") or "").strip() == sku:
                    info = {
                        "inventory_item_id": v.get("inventory_item_id"),
                        "variant_id":        v.get("id"),
                        "product_id":        v.get("product_id"),
                    }
                    # Persiste no cache
                    with _shopify_lock:
                        cache = _load_json(SHOPIFY_CACHE_PATH, {})
                        sku_map = cache.get("sku_map", {})
                        sku_map[sku] = info
                        cache["sku_map"] = sku_map
                        cache["sku_map_ts"] = time.time()
                        _save_json(SHOPIFY_CACHE_PATH, cache)
                        _estado["shopify_cache_size"] = len(sku_map)
                        _estado["shopify_cache_ts"] = datetime.utcnow().isoformat()
                    logger.info("[SYNC-SHOPIFY] SKU=%s encontrado: inventory_item_id=%s",
                                sku, info["inventory_item_id"])
                    return info
        else:
            logger.warning("[SYNC-SHOPIFY] Lookup SKU=%s: HTTP %s", sku, r.status_code)
    except Exception as e:
        logger.warning("[SYNC-SHOPIFY] Erro lookup SKU=%s: %s", sku, e)
    return None


def _shopify_get_variant(sku: str, force: bool = False) -> Optional[dict]:
    """
    Retorna info do variant para o SKU dado.
    Estratégia lazy: verifica cache local primeiro, depois consulta API.
    """
    if not force:
        cache = _load_json(SHOPIFY_CACHE_PATH, {})
        sku_map = cache.get("sku_map", {})
        if sku_map.get(sku):
            return sku_map[sku]
    return _shopify_lookup_sku(sku)


def _sync_shopify(sku: str, estoque: int) -> dict:
    """Atualiza estoque de um SKU no Shopify. Cache lazy por SKU."""
    info = _shopify_get_variant(sku)
    if not info:
        # Produto pode ser novo — já tentou via API direta, sem SKU cadastrado
        return {"ok": False, "skipped": True, "reason": "sku_nao_encontrado_shopify"}

    location_id = _shopify_get_location_id()
    if not location_id:
        return {"ok": False, "reason": "sem_location_shopify"}

    iid = info["inventory_item_id"]
    url = (f"https://{SHOPIFY_STORE}.myshopify.com"
           f"/admin/api/{SHOPIFY_API_VERSION}/inventory_levels/set.json")
    payload = {"location_id": location_id, "inventory_item_id": iid, "available": estoque}

    for attempt in range(MAX_RETRY):
        try:
            r = requests.post(url, json=payload, headers=_shopify_headers(), timeout=15)
            if r.status_code in (200, 201):
                return {"ok": True, "inventory_item_id": iid, "estoque_set": estoque}
            if r.status_code == 422:
                # Item não rastreado por localização — tenta connect primeiro
                requests.post(
                    f"https://{SHOPIFY_STORE}.myshopify.com"
                    f"/admin/api/{SHOPIFY_API_VERSION}/inventory_levels/connect.json",
                    json={"location_id": location_id, "inventory_item_id": iid},
                    headers=_shopify_headers(), timeout=15
                )
                continue
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            return {"ok": False, "http": r.status_code, "erro": r.text[:300]}
        except Exception as e:
            if attempt < MAX_RETRY - 1:
                time.sleep(1.5 ** attempt)
            else:
                return {"ok": False, "erro": str(e)}

    return {"ok": False, "erro": "max_retries_shopify"}


# ─────────────────────────────────────────────────────────────────────────────
# Mercado Livre helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ml_token() -> str:
    tokens = _load_json(ML_TOKENS_PATH, {})
    return tokens.get("access_token", "")


def _ml_headers() -> dict:
    return {"Authorization": f"Bearer {_ml_token()}", "Content-Type": "application/json"}


def _ml_seller_id() -> Optional[str]:
    cache = _load_json(ML_CACHE_PATH, {})
    if cache.get("seller_id"):
        return str(cache["seller_id"])
    try:
        r = requests.get("https://api.mercadolibre.com/users/me",
                         headers=_ml_headers(), timeout=15)
        if r.status_code == 200:
            sid = str(r.json().get("id", ""))
            if sid:
                cache["seller_id"] = sid
                _save_json(ML_CACHE_PATH, cache)
                return sid
    except Exception as e:
        logger.warning("[SYNC-ML] Erro ao buscar seller_id: %s", e)
    return None


def _ml_build_sku_map() -> dict:
    """
    Varre todos os anúncios ativos E pausados do ML e constrói mapa:
      SKU → {"item_id": str, "status": str}
    Inclui pausados para que o sync possa reativar listings quando o
    estoque do Bling subir (correção do bug: cache só cobria "active").
    Usa seller_custom_field como SKU (campo padrão de barcode/EAN no ML Brasil).
    """
    seller_id = _ml_seller_id()
    if not seller_id:
        logger.warning("[SYNC-ML] seller_id não disponível — cache ML vazio")
        return {}

    sku_map: dict = {}
    limit    = 50

    for status_busca in ("active", "paused"):
        offset = 0
        while True:
            try:
                r = requests.get(
                    f"https://api.mercadolibre.com/users/{seller_id}/items/search",
                    params={"offset": offset, "limit": limit, "status": status_busca},
                    headers=_ml_headers(), timeout=20,
                )
                if r.status_code != 200:
                    logger.warning("[SYNC-ML] Erro listing %s: HTTP %s", status_busca, r.status_code)
                    break

                data     = r.json()
                item_ids = data.get("results", [])
                if not item_ids:
                    break

                # Busca detalhes em batches de 20 para pegar seller_custom_field + status
                for i in range(0, len(item_ids), 20):
                    batch   = item_ids[i : i + 20]
                    ids_str = ",".join(batch)
                    r2 = requests.get(
                        "https://api.mercadolibre.com/items",
                        params={"ids": ids_str,
                                "attributes": "id,status,seller_custom_field,attributes"},
                        headers=_ml_headers(), timeout=20,
                    )
                    if r2.status_code == 200:
                        for raw in r2.json():
                            item = raw.get("body", raw) if "body" in raw else raw
                            # seller_custom_field (campo padrão de SKU no ML)
                            sku = str(item.get("seller_custom_field") or "").strip()
                            # Fallback: atributo SELLER_SKU
                            if not sku:
                                for attr in item.get("attributes", []):
                                    if attr.get("id") == "SELLER_SKU":
                                        sku = str(attr.get("value_name") or "").strip()
                                        break
                            item_id     = str(item.get("id") or "").strip()
                            item_status = str(item.get("status") or status_busca)
                            if sku and item_id:
                                # Prefere active sobre paused se mesmo SKU aparece nos dois
                                existing = sku_map.get(sku)
                                if not existing or existing.get("status") != "active":
                                    sku_map[sku] = {"item_id": item_id, "status": item_status}
                    time.sleep(0.3)

                total  = data.get("paging", {}).get("total", 0)
                offset += limit
                if offset >= total:
                    break
            except Exception as e:
                logger.warning("[SYNC-ML] Erro build cache (%s): %s", status_busca, e)
                break

    logger.info("[SYNC-ML] Cache construído: %d SKUs (%d active + paused)",
                len(sku_map),
                sum(1 for v in sku_map.values() if v.get("status") == "active"))
    return sku_map


def _ml_get_sku_map(force: bool = False) -> dict:
    """Retorna mapa SKU→item_id ML, reconstruindo se cache estiver vencido."""
    with _ml_lock:
        cache = _load_json(ML_CACHE_PATH, {})
        ts    = cache.get("sku_map_ts", 0)
        if not force and time.time() - ts < CACHE_TTL_S and cache.get("sku_map"):
            return cache["sku_map"]

        logger.info("[SYNC-ML] Reconstruindo cache SKU→ML...")
        sku_map = _ml_build_sku_cache()
        cache["sku_map"]    = sku_map
        cache["sku_map_ts"] = time.time()
        _save_json(ML_CACHE_PATH, cache)
        _estado["ml_cache_ts"]   = datetime.utcnow().isoformat()
        _estado["ml_cache_size"] = len(sku_map)
        logger.info("[SYNC-ML] Cache pronto: %d SKUs", len(sku_map))
        return sku_map


def _ml_build_sku_cache() -> dict:
    """Alias público — delega para _ml_build_sku_map."""
    return _ml_build_sku_map()


def _sync_ml(sku: str, estoque: int) -> dict:
    """
    Atualiza estoque de um SKU no Mercado Livre. Retry automático.
    Inclui reativação de listings pausados quando estoque sobe (Bling→ML).
    """
    sku_map = _ml_get_sku_map()
    entry   = sku_map.get(sku)
    if not entry:
        # SKU não está no cache ML — produto não listado ou cache desatualizado
        return {"ok": False, "skipped": True, "reason": "sku_nao_encontrado_ml"}

    # Compatibilidade: cache antigo guardava string, novo guarda dict
    if isinstance(entry, str):
        item_id     = entry
        item_status = "active"
    else:
        item_id     = entry.get("item_id", "")
        item_status = entry.get("status", "active")

    if not item_id:
        return {"ok": False, "skipped": True, "reason": "item_id_vazio"}

    # Monta payload:
    # - estoque=0 → pausa o anúncio (ML não aceita qty=0 sem pausar)
    # - estoque>0 + listing pausado → reativa E atualiza qty
    # - estoque>0 + listing ativo   → só atualiza qty
    if estoque == 0:
        payload = {"available_quantity": 1, "status": "paused"}
    elif item_status == "paused":
        # Reativa o listing junto com o novo estoque
        payload = {"available_quantity": estoque, "status": "active"}
        logger.info("[SYNC-ML] Reativando listing pausado %s (SKU=%s, estoque=%d)", item_id, sku, estoque)
    else:
        payload = {"available_quantity": estoque}

    for attempt in range(MAX_RETRY):
        try:
            r = requests.put(
                f"https://api.mercadolibre.com/items/{item_id}",
                json=payload,
                headers=_ml_headers(),
                timeout=15,
            )
            if r.status_code == 200:
                reativado = estoque > 0 and item_status == "paused"
                # Atualiza status no cache em memória para evitar reativação dupla
                entry_updated = {"item_id": item_id, "status": "active" if estoque > 0 else "paused"}
                sku_map[sku] = entry_updated
                return {"ok": True, "item_id": item_id, "estoque_set": estoque,
                        "pausado": estoque == 0, "reativado": reativado}
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            return {"ok": False, "item_id": item_id,
                    "http": r.status_code, "erro": r.text[:300]}
        except Exception as e:
            if attempt < MAX_RETRY - 1:
                time.sleep(1.5 ** attempt)
            else:
                return {"ok": False, "erro": str(e)}

    return {"ok": False, "erro": "max_retries_ml"}


# ─────────────────────────────────────────────────────────────────────────────
# Shopee helpers
# ─────────────────────────────────────────────────────────────────────────────

def _shopee_build_mapeamento() -> dict:
    """
    Reconstrói automaticamente o mapeamento SKU→item_id da Shopee.
    Usa o campo item_sku de cada produto (definido pelo vendedor).
    Preserva entradas manuais existentes.

    Retorna o mapeamento completo atualizado.
    """
    try:
        from services.shopee import ShopeeService
        svc = ShopeeService()
    except Exception as exc:
        logger.warning("[SYNC-SHOPEE] Mapeamento: Shopee não autenticada — %s", exc)
        return _load_json(SHOPEE_MAPEAMENTO_PATH, {})

    # Carrega mapeamento existente (entradas manuais preservadas)
    mapeamento: dict = _load_json(SHOPEE_MAPEAMENTO_PATH, {})

    offset = 0
    total_novos = 0
    page = 0

    logger.info("[SYNC-SHOPEE] Reconstruindo mapeamento SKU→item_id...")

    while page < 30:
        try:
            data = svc.listar_produtos(offset=offset, page_size=100)
            if data.get("error"):
                logger.warning("[SYNC-SHOPEE] Erro listando produtos: %s", data.get("message"))
                break
            resp = data.get("response") or {}
            items = resp.get("item") or []
            if not items:
                break

            ids = [i["item_id"] for i in items]
            # obter_info_items retorna item_sku
            info_data = svc.obter_info_items(ids)
            info_list = (info_data.get("response") or {}).get("item_list") or []

            for info in info_list:
                item_id = str(info.get("item_id") or "")
                sku     = str(info.get("item_sku") or "").strip()
                if sku and item_id:
                    if mapeamento.get(sku) != item_id:
                        mapeamento[sku] = item_id
                        total_novos += 1

            if not resp.get("has_next_item"):
                break
            offset = resp.get("next_offset", offset + 100)
            page += 1
            time.sleep(0.3)

        except Exception as exc:
            logger.warning("[SYNC-SHOPEE] Erro na paginação do mapeamento: %s", exc)
            break

    if mapeamento:
        _save_json(SHOPEE_MAPEAMENTO_PATH, mapeamento)

    logger.info("[SYNC-SHOPEE] Mapeamento pronto: %d SKUs (%d novos)", len(mapeamento), total_novos)
    return mapeamento


def _shopee_get_item_id(sku: str) -> Optional[int]:
    """
    Retorna o item_id Shopee para o SKU dado.
    Lê do arquivo shopee_mapeamento.json (auto-construído + entradas manuais).
    Retorna None se não mapeado.
    """
    mapeamento = _load_json(SHOPEE_MAPEAMENTO_PATH, {})
    val = mapeamento.get(sku)
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _sync_shopee(sku: str, estoque: int) -> dict:
    """Atualiza estoque de um SKU na Shopee via ShopeeService."""
    item_id = _shopee_get_item_id(sku)
    if item_id is None:
        return {"ok": False, "skipped": True, "reason": "sku_nao_mapeado_shopee"}

    try:
        from services.shopee import ShopeeService
        svc = ShopeeService()
    except RuntimeError as exc:
        return {"ok": False, "skipped": True, "reason": f"shopee_nao_autenticada: {exc}"}
    except Exception as exc:
        return {"ok": False, "erro": str(exc)}

    # Shopee não aceita estoque negativo
    qty = max(0, estoque)

    for attempt in range(MAX_RETRY):
        try:
            res = svc.atualizar_estoque(item_id, qty)
            if res.get("success"):
                return {"ok": True, "item_id": item_id, "estoque_set": qty}
            err = res.get("error", "erro_desconhecido")
            # Token expirado — tenta renovar e repete
            if "token" in str(err).lower() or "auth" in str(err).lower():
                if attempt < MAX_RETRY - 1:
                    try:
                        from services.shopee import ShopeeOAuthService
                        ShopeeOAuthService().renovar_token()
                        svc = ShopeeService()  # recarrega com novo token
                    except Exception:
                        pass
                    continue
            return {"ok": False, "item_id": item_id, "erro": err}
        except Exception as exc:
            if attempt < MAX_RETRY - 1:
                time.sleep(1.5 ** attempt)
            else:
                return {"ok": False, "erro": str(exc)}

    return {"ok": False, "erro": "max_retries_shopee"}


# ─────────────────────────────────────────────────────────────────────────────
# Amazon helpers  (Listings Items API — FBM sellers)
# ─────────────────────────────────────────────────────────────────────────────

def _amazon_config_ok() -> bool:
    """Verifica se as credenciais Amazon SP-API estão configuradas."""
    return all([
        os.getenv("AMAZON_CLIENT_ID"),
        os.getenv("AMAZON_CLIENT_SECRET"),
        os.getenv("AMAZON_SELLER_ID"),
    ])


def _amazon_lwa_token() -> Optional[str]:
    """
    Retorna access_token LWA da Amazon (renova automaticamente se expirado).
    Cache em data/amazon_tokens.json.
    """
    with _amazon_lock:
        t = _load_json(AMAZON_TOKENS_PATH, {})
        expires_at = float(t.get("lwa_expires_at", 0))

        if t.get("lwa_access_token") and time.time() < expires_at - 60:
            return t["lwa_access_token"]

        refresh_token = (
            t.get("refresh_token") or
            os.getenv("AMAZON_REFRESH_TOKEN", "")
        )
        if not refresh_token:
            logger.warning("[SYNC-AMAZON] refresh_token não encontrado")
            return None

        try:
            r = requests.post(
                AMAZON_LWA_URL,
                data={
                    "grant_type":    "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id":     os.getenv("AMAZON_CLIENT_ID", ""),
                    "client_secret": os.getenv("AMAZON_CLIENT_SECRET", ""),
                },
                timeout=20,
            )
            if r.status_code != 200:
                logger.warning("[SYNC-AMAZON] LWA erro %s: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
            token = data["access_token"]
            t["lwa_access_token"] = token
            t["lwa_expires_at"]   = time.time() + int(data.get("expires_in", 3600))
            _save_json(AMAZON_TOKENS_PATH, t)
            return token
        except Exception as exc:
            logger.warning("[SYNC-AMAZON] Erro renovando LWA token: %s", exc)
            return None


def _amazon_headers() -> dict:
    token = _amazon_lwa_token()
    return {
        "x-amz-access-token": token or "",
        "Content-Type": "application/json",
    }


def _amazon_get_product_type(sku: str) -> Optional[str]:
    """
    Busca o productType real de um listing via GET.
    Usado como fallback quando PATCH com 'PRODUCT' retorna 400.
    """
    seller_id = os.getenv("AMAZON_SELLER_ID", "")
    url = f"{AMAZON_SP_BASE}/listings/2021-08-01/items/{seller_id}/{sku}"
    params = {"marketplaceIds": AMAZON_MARKETPLACE, "includedData": "summaries"}
    try:
        r = requests.get(url, headers=_amazon_headers(), params=params, timeout=15)
        if r.status_code == 200:
            pt = r.json().get("productType")
            if pt:
                logger.info("[SYNC-AMAZON] SKU=%s → productType real: %s", sku, pt)
                return pt
        else:
            logger.warning("[SYNC-AMAZON] GET listing SKU=%s: HTTP %s %s",
                           sku, r.status_code, r.text[:200])
    except Exception as exc:
        logger.warning("[SYNC-AMAZON] Erro ao buscar productType SKU=%s: %s", sku, exc)
    return None


def _sync_amazon(sku: str, estoque: int) -> dict:
    """
    Atualiza estoque FBM na Amazon via Listings Items API PATCH.
    Funciona para MFN (Merchant Fulfilled Network = FBM).
    Para FBA, a Amazon gerencia o estoque internamente — a chamada falhará com 400/403
    e será registrada como 'skipped'.
    """
    if not _amazon_config_ok():
        return {"ok": False, "skipped": True, "reason": "amazon_nao_configurada"}

    seller_id = os.getenv("AMAZON_SELLER_ID", "")
    url = f"{AMAZON_SP_BASE}/listings/2021-08-01/items/{seller_id}/{sku}"
    params = {"marketplaceIds": AMAZON_MARKETPLACE}

    # Começa com productType genérico; se falhar por tipo inválido, busca o tipo real
    product_type = "PRODUCT"

    def _make_body(pt: str) -> dict:
        return {
            "productType": pt,
            "patches": [{
                "op":    "replace",
                "path":  "/attributes/fulfillment_availability",
                "value": [{
                    "fulfillment_channel_code":    "DEFAULT",
                    "quantity":                    max(0, estoque),
                    "lead_time_to_ship_max_days":  2,
                }],
            }],
        }

    for attempt in range(MAX_RETRY):
        try:
            r = requests.patch(
                url, params=params, json=_make_body(product_type),
                headers=_amazon_headers(), timeout=20,
            )
            if r.status_code in (200, 202):
                logger.info("[SYNC-AMAZON] SKU=%s estoque=%d OK (productType=%s)",
                            sku, estoque, product_type)
                return {"ok": True, "sku": sku, "estoque_set": estoque,
                        "product_type": product_type}
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (400, 403, 404):
                err = {}
                try:
                    err = r.json()
                except Exception:
                    pass
                err_list  = err.get("errors") or [{}]
                err_code  = err_list[0].get("code", "")
                err_msg   = err_list[0].get("message", r.text[:300])
                logger.warning(
                    "[SYNC-AMAZON] SKU=%s HTTP %d | code=%s | msg=%s | productType=%s",
                    sku, r.status_code, err_code, err_msg[:300], product_type,
                )

                # Se falhou com "PRODUCT" genérico, tenta buscar o productType real
                if (attempt == 0 and product_type == "PRODUCT"
                        and r.status_code == 400
                        and err_code in ("INVALID_INPUT", "INVALID_PRODUCT_TYPE",
                                         "PRODUCT_TYPE_NOT_FOUND", "")):
                    real_type = _amazon_get_product_type(sku)
                    if real_type and real_type != "PRODUCT":
                        product_type = real_type
                        logger.info("[SYNC-AMAZON] SKU=%s retentando com productType=%s",
                                    sku, product_type)
                        continue  # retry with real product type

                return {
                    "ok": False, "skipped": True,
                    "reason": f"amazon_http_{r.status_code}_{err_code}",
                    "erro_detalhe": err_msg[:300],
                }
            # HTTP inesperado
            logger.warning("[SYNC-AMAZON] SKU=%s HTTP %d inesperado: %s",
                           sku, r.status_code, r.text[:200])
            return {"ok": False, "http": r.status_code, "erro": r.text[:300]}
        except Exception as exc:
            if attempt < MAX_RETRY - 1:
                time.sleep(1.5 ** attempt)
            else:
                return {"ok": False, "erro": str(exc)}

    return {"ok": False, "erro": "max_retries_amazon"}


# ─────────────────────────────────────────────────────────────────────────────
# Entry point público  (chamado pelo webhook do Bling)
# ─────────────────────────────────────────────────────────────────────────────

def sync_estoque_bling(payload: dict) -> dict:
    """
    Processa payload do webhook Bling e propaga o estoque para
    Shopify, ML, Shopee e Amazon.
    Deve ser chamado em background task (não bloqueia o webhook).

    Eventos relevantes do Bling:
      produto.estoque.alterado | estoque.atualizado | estoque
      produto.alterado | produto.atualizado
    """
    evento = payload.get("evento") or payload.get("event") or "desconhecido"

    # Filtra eventos não relacionados a estoque/produto
    RELEVANTES = {
        "produto.estoque.alterado", "estoque.atualizado", "estoque",
        "produto.alterado", "produto.atualizado", "product.updated",
    }
    eh_relevante = (
        evento in RELEVANTES or
        any(k in str(evento).lower() for k in ("estoque", "produto", "stock", "product"))
    )
    if not eh_relevante:
        logger.debug("[SYNC] Evento ignorado: %s", evento)
        return {"ok": True, "skipped": True, "reason": f"evento_ignorado:{evento}"}

    parsed = _parse_payload(payload)
    if not parsed:
        logger.warning(
            "[SYNC] Não foi possível extrair SKU/estoque do webhook. "
            "Payload (amostra): %s", str(payload)[:400]
        )
        _append_log({
            "evento": evento, "sku": None, "estoque": None,
            "erro": "parse_falhou",
            "payload_amostra": str(payload)[:400],
        })
        return {"ok": False, "reason": "parse_error"}

    sku, estoque = parsed
    logger.info("[SYNC] Bling webhook → SKU=%s estoque=%d | propagando 4 canais...", sku, estoque)

    resultados: dict = {}

    # ── Shopify ───────────────────────────────────────────────────────────────
    try:
        resultados["shopify"] = _sync_shopify(sku, estoque)
    except Exception as e:
        resultados["shopify"] = {"ok": False, "erro": str(e)}
        logger.exception("[SYNC] Erro inesperado ao sincronizar Shopify: %s", e)

    # ── Mercado Livre ─────────────────────────────────────────────────────────
    try:
        resultados["ml"] = _sync_ml(sku, estoque)
    except Exception as e:
        resultados["ml"] = {"ok": False, "erro": str(e)}
        logger.exception("[SYNC] Erro inesperado ao sincronizar ML: %s", e)

    # ── Shopee ────────────────────────────────────────────────────────────────
    try:
        resultados["shopee"] = _sync_shopee(sku, estoque)
    except Exception as e:
        resultados["shopee"] = {"ok": False, "erro": str(e)}
        logger.exception("[SYNC] Erro inesperado ao sincronizar Shopee: %s", e)

    # ── Amazon ────────────────────────────────────────────────────────────────
    try:
        resultados["amazon"] = _sync_amazon(sku, estoque)
    except Exception as e:
        resultados["amazon"] = {"ok": False, "erro": str(e)}
        logger.exception("[SYNC] Erro inesperado ao sincronizar Amazon: %s", e)

    # ── Contagem ──────────────────────────────────────────────────────────────
    ok_n    = sum(1 for r in resultados.values() if r.get("ok"))
    skip_n  = sum(1 for r in resultados.values() if r.get("skipped"))
    fail_n  = len(resultados) - ok_n - skip_n

    # ── Log persistente ───────────────────────────────────────────────────────
    _append_log({
        "evento":     evento,
        "sku":        sku,
        "estoque":    estoque,
        "resultados": resultados,
        "ok":   ok_n,
        "skip": skip_n,
        "fail": fail_n,
    })

    # ── Estado do motor ───────────────────────────────────────────────────────
    _estado["syncs_realizados"] += 1
    _estado["syncs_ok"]         += (1 if fail_n == 0 else 0)
    _estado["syncs_falha"]      += (1 if fail_n  > 0 else 0)
    _estado["ultimo_sync_at"]    = datetime.utcnow().isoformat()
    _estado["ultimo_sku"]        = sku

    logger.info(
        "[SYNC] SKU=%s estoque=%d → %d ok | %d skip | %d falha",
        sku, estoque, ok_n, skip_n, fail_n,
    )

    return {
        "ok":         fail_n == 0,
        "sku":        sku,
        "estoque":    estoque,
        "resultados": resultados,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Rebuild do cache em background (chamado no startup)
# ─────────────────────────────────────────────────────────────────────────────

def _rebuild_caches_bg():
    """
    Reconstrói os caches em background:
    - ML: cache SKU→item_id (TTL 30min)
    - Shopee: mapeamento SKU→item_id via item_sku da API
    - Shopify: usa lookup lazy por SKU (não precisa varredura completa)
    - Bling: cache produto_id→codigo (para webhooks v3 sem codigo)
    """
    def _ml():
        try:
            _ml_get_sku_map(force=True)
        except Exception as e:
            logger.warning("[SYNC] Rebuild ML falhou: %s", e)

    def _shopee_map():
        try:
            _shopee_build_mapeamento()
        except Exception as e:
            logger.warning("[SYNC] Rebuild Shopee mapeamento falhou: %s", e)

    def _bling_ids():
        try:
            _bling_id_cache_refresh()
        except Exception as e:
            logger.warning("[SYNC] Rebuild cache Bling ID→SKU falhou: %s", e)

    threading.Thread(target=_ml,          daemon=True, name="cache-ml-rebuild").start()
    threading.Thread(target=_shopee_map,  daemon=True, name="cache-shopee-map").start()
    threading.Thread(target=_bling_ids,   daemon=True, name="cache-bling-ids").start()
    logger.info("[SYNC] Cache ML + Shopee + Bling ID→SKU sendo reconstruídos em background")


# ─────────────────────────────────────────────────────────────────────────────
# Rotas FastAPI
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/status")
def status_sync():
    """Estado do motor de sync + info dos caches."""
    shopify_cache = _load_json(SHOPIFY_CACHE_PATH, {})
    ml_cache      = _load_json(ML_CACHE_PATH, {})
    sh_ts = shopify_cache.get("sku_map_ts")
    ml_ts = ml_cache.get("sku_map_ts")

    def _age(ts):
        if not ts:
            return None
        return round(time.time() - ts)

    # Shopee — lê mapeamento direto do arquivo
    shopee_mapeamento = _load_json(SHOPEE_MAPEAMENTO_PATH, {})
    shopee_configurado = SHOPEE_MAPEAMENTO_PATH.exists() and bool(shopee_mapeamento)

    # Amazon — verifica credenciais
    amazon_configurada = _amazon_config_ok()

    sh_skus = len(shopify_cache.get("sku_map", {}))
    return {
        **_estado,
        "shopify_cache": {
            "skus":    sh_skus,
            "modo":    "lazy_por_sku",
            "idade_s": _age(sh_ts),
            "valido":  True,
        },
        "ml_cache": {
            "skus":    len(ml_cache.get("sku_map", {})),
            "idade_s": _age(ml_ts),
            "ttl_s":   CACHE_TTL_S,
            "valido":  bool(ml_ts and _age(ml_ts) < CACHE_TTL_S),
        },
        "shopee": {
            "mapeamentos": len(shopee_mapeamento),
            "configurado": shopee_configurado,
            "arquivo":     str(SHOPEE_MAPEAMENTO_PATH.name),
        },
        "amazon": {
            "configurada":  amazon_configurada,
            "marketplace":  AMAZON_MARKETPLACE,
            "seller_id":    os.getenv("AMAZON_SELLER_ID", "—"),
            "nota":         "FBM apenas — FBA gerenciado pela Amazon",
        },
    }


@router.get("/log")
def log_sync(limit: int = Query(100, ge=1, le=500)):
    """Últimas N entradas do log de sincronização."""
    log = _load_json(SYNC_LOG_PATH, [])
    return {"total": len(log), "entries": log[:limit]}


@router.post("/rebuild-cache")
def rebuild_cache(background_tasks: BackgroundTasks):
    """Força rebuild dos caches SKU→Shopify e SKU→ML em background."""
    background_tasks.add_task(_rebuild_caches_bg)
    return {"ok": True, "message": "Rebuild iniciado em background"}


@router.post("/testar")
def testar_sync(payload: dict):
    """
    Testa a sincronização de um SKU manualmente.
    Body: {"sku": "ABC123", "estoque": 10}
    """
    sku     = str(payload.get("sku", "")).strip()
    estoque = int(payload.get("estoque", 0))
    if not sku:
        return JSONResponse(status_code=422, content={"erro": "sku obrigatório"})

    fake_payload = {
        "evento": "produto.estoque.alterado",
        "dados":  {
            "produto": {
                "codigo":  sku,
                "estoque": {"saldoVirtualTotal": estoque},
            }
        },
    }
    resultado = sync_estoque_bling(fake_payload)
    return resultado


@router.post("/testar-amazon")
def testar_amazon(payload: dict):
    """
    Testa APENAS o sync Amazon de um SKU (sem Shopify/ML/Shopee).
    Body: {"sku": "225875", "estoque": 10}
    Útil para diagnosticar erros de productType ou credenciais.
    """
    sku     = str(payload.get("sku", "")).strip()
    estoque = int(payload.get("estoque", 0))
    if not sku:
        return JSONResponse(status_code=422, content={"erro": "sku obrigatório"})

    # Primeiro busca o productType real (info de diagnóstico)
    product_type_real = _amazon_get_product_type(sku)

    resultado = _sync_amazon(sku, estoque)
    return {
        "sku":                sku,
        "estoque_enviado":    estoque,
        "product_type_real":  product_type_real,
        "resultado_patch":    resultado,
        "amazon_config_ok":   _amazon_config_ok(),
        "seller_id":          os.getenv("AMAZON_SELLER_ID", "—"),
        "marketplace":        AMAZON_MARKETPLACE,
    }
