"""
conferencia_sku.py — Shinsei Pricing
Conferência cruzada de SKUs: Bling ↔ ML, Shopify, Amazon, Shopee.

Lógica:
- Coleta todos os SKUs do Bling (produtos ativos)
- Coleta todos os SKUs de cada canal conectado
- Cruza: quais do Bling faltam em cada canal?
         quais de cada canal não existem no Bling?
- Resultado salvo em data/conferencia_sku.json
"""

from __future__ import annotations

import json
import logging
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

RESULTADO_PATH = DATA_DIR / "conferencia_sku.json"
_ESTADO_LOCK = threading.Lock()
_estado: dict = {"status": "ocioso", "pct": 0, "msg": "", "etapa": ""}


# ─────────────────────────────────────────────
# Estado / progresso
# ─────────────────────────────────────────────

def get_estado() -> dict:
    with _ESTADO_LOCK:
        return dict(_estado)


def _set_estado(status: str, pct: int, etapa: str, msg: str = "") -> None:
    with _ESTADO_LOCK:
        _estado["status"] = status
        _estado["pct"] = pct
        _estado["etapa"] = etapa
        _estado["msg"] = msg


def get_resultado() -> dict | None:
    if not RESULTADO_PATH.exists():
        return None
    try:
        return json.loads(RESULTADO_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _salvar_resultado(dados: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    RESULTADO_PATH.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")


# ─────────────────────────────────────────────
# Fetch Bling — todos os produtos ativos
# ─────────────────────────────────────────────

def _fetch_bling(bling_client) -> tuple[dict[str, dict], int]:
    """
    Retorna (skus_bling, total_sem_sku)
    skus_bling: {sku -> {nome, estoque, preco, id_bling}}
    """
    skus: dict[str, dict] = {}
    sem_sku = 0
    pagina = 1
    while pagina <= 300:
        try:
            payload = bling_client.list_products(page=pagina, limit=100)
        except Exception as e:
            logger.warning("Bling página %d erro: %s", pagina, e)
            break

        data = payload.get("data", [])
        if not data:
            break

        for item in data:
            try:
                from bling_client import BlingClient as _BC
                prod = _BC._normalize_product(None, item) if hasattr(_BC, "_normalize_product") else item
            except Exception:
                prod = item

            situacao = str(prod.get("situacao") or "").upper()
            if situacao in ("I", "INATIVO", "E", "EXCLUIDO"):
                continue

            sku = str(prod.get("codigo") or prod.get("sku") or "").strip()
            if not sku:
                sem_sku += 1
                continue

            nome = str(prod.get("descricao") or prod.get("nome") or "").strip()
            estoque = 0
            preco = 0.0
            try:
                est_info = prod.get("estoque") or {}
                estoque = int(
                    est_info.get("saldoVirtualTotal")
                    or est_info.get("saldo_fisico_total")
                    or 0
                )
                preco = float(prod.get("preco") or 0)
            except Exception:
                pass

            skus[sku] = {
                "nome": nome,
                "estoque": estoque,
                "preco": preco,
                "id_bling": str(prod.get("id") or ""),
            }

        pagina += 1
        time.sleep(0.15)

    return skus, sem_sku


# ─────────────────────────────────────────────
# Fetch ML — todos os anúncios ativos com SKU
# ─────────────────────────────────────────────

def _fetch_ml() -> tuple[dict[str, str], list[dict], bool]:
    """
    Retorna (skus_ml, sem_sku_ml, conectado)
    skus_ml: {sku -> ml_id}
    sem_sku_ml: lista de {id, titulo} sem SKU
    """
    try:
        import requests
        tp = DATA_DIR / "ml_tokens.json"
        if not tp.exists():
            return {}, [], False
        tokens = json.loads(tp.read_text(encoding="utf-8"))
        token = tokens.get("access_token", "")
        if not token:
            return {}, [], False

        h = {"Authorization": f"Bearer {token}"}
        skus: dict[str, str] = {}
        sem_sku: list[dict] = {}

        # Descobre user_id
        me = requests.get("https://api.mercadolibre.com/users/me", headers=h, timeout=10)
        if me.status_code != 200:
            return {}, [], False
        user_id = me.json().get("id")

        offset = 0
        limit = 100
        total_buscados = 0
        MAX = 5000  # segurança

        while offset < MAX:
            r = requests.get(
                f"https://api.mercadolibre.com/users/{user_id}/items/search",
                params={"status": "active", "limit": limit, "offset": offset},
                headers=h, timeout=15,
            )
            if r.status_code != 200:
                break
            data = r.json()
            items = data.get("results", [])
            if not items:
                break

            # Detalha em lote de 20
            for i in range(0, len(items), 20):
                batch = items[i : i + 20]
                ids = ",".join(batch)
                r2 = requests.get(
                    "https://api.mercadolibre.com/items",
                    params={
                        "ids": ids,
                        "attributes": "id,title,available_quantity,seller_custom_field,status",
                    },
                    headers=h,
                    timeout=15,
                )
                if r2.status_code != 200:
                    continue
                for entry in r2.json():
                    item = entry.get("body") or entry
                    sku = str(item.get("seller_custom_field") or "").strip()
                    ml_id = item.get("id", "")
                    titulo = (item.get("title") or "")[:80]
                    estoque = item.get("available_quantity", 0)
                    if sku:
                        skus[sku] = ml_id
                    else:
                        sem_sku.append({"id": ml_id, "titulo": titulo, "estoque": estoque})

            total_buscados += len(items)
            offset += limit
            if len(items) < limit:
                break
            time.sleep(0.1)

        return skus, sem_sku, True

    except Exception as e:
        logger.warning("Erro ao buscar ML: %s", e)
        return {}, [], False


# ─────────────────────────────────────────────
# Fetch Shopify — todos os SKUs de variantes
# ─────────────────────────────────────────────

def _fetch_shopify() -> tuple[dict[str, str], bool]:
    """
    Retorna (skus_shopify, conectado)
    skus_shopify: {sku -> variant_id}
    """
    try:
        cfg_path = DATA_DIR / "shopify_config.json"
        if not cfg_path.exists():
            return {}, False
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        token = cfg.get("access_token") or cfg.get("token", "")
        shop = cfg.get("shop") or cfg.get("myshopify_domain", "")
        if not token or not shop:
            return {}, False

        import requests
        h = {
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        }
        base = f"https://{shop}/admin/api/2024-01/products.json"
        skus: dict[str, str] = {}
        page_info = None

        while True:
            params: dict = {"limit": 250, "fields": "id,variants"}
            if page_info:
                params["page_info"] = page_info

            r = requests.get(base, headers=h, params=params, timeout=20)
            if r.status_code != 200:
                break

            produtos = r.json().get("products", [])
            for p in produtos:
                for v in p.get("variants", []):
                    sku = str(v.get("sku") or "").strip()
                    if sku:
                        skus[sku] = str(v.get("id") or "")

            # Paginação via Link header
            link = r.headers.get("Link", "")
            if 'rel="next"' in link:
                import re
                m = re.search(r'page_info=([^&>]+)', link.split('rel="next"')[0])
                page_info = m.group(1) if m else None
                if not page_info:
                    break
            else:
                break

            if not produtos:
                break
            time.sleep(0.1)

        return skus, True

    except Exception as e:
        logger.warning("Erro ao buscar Shopify: %s", e)
        return {}, False


# ─────────────────────────────────────────────
# Fetch Amazon — inventário FBA/MFN
# ─────────────────────────────────────────────

def _fetch_amazon() -> tuple[dict[str, int], bool]:
    """
    Retorna (skus_amazon, conectado)
    skus_amazon: {sellerSku -> quantity}
    """
    try:
        from amazon_client import AmazonClient
        client = AmazonClient()
        # Tenta buscar inventário via SP-API
        skus: dict[str, int] = {}

        resp = client.get_inventory_summaries()
        summaries = (
            resp.get("payload", {})
            .get("inventorySummaries", [])
        )
        for s in summaries:
            sku = str(s.get("sellerSku") or "").strip()
            qty = int(s.get("totalQuantity") or s.get("fulfillableQuantity") or 0)
            if sku:
                skus[sku] = qty

        return skus, True

    except Exception as e:
        logger.info("Amazon indisponível: %s", e)
        return {}, False


# ─────────────────────────────────────────────
# Fetch Shopee — via mapeamento.json
# ─────────────────────────────────────────────

def _fetch_shopee() -> tuple[dict[str, str], bool]:
    """
    Retorna (skus_shopee, conectado)
    skus_shopee: {sku -> item_id}
    """
    try:
        mp = DATA_DIR / "shopee_mapeamento.json"
        if not mp.exists():
            return {}, False
        data = json.loads(mp.read_text(encoding="utf-8"))
        # formato: {"SKU": "item_id", ...}  ou lista de {sku, item_id}
        if isinstance(data, dict):
            skus = {k: str(v) for k, v in data.items() if k}
        elif isinstance(data, list):
            skus = {
                str(d.get("sku") or ""): str(d.get("item_id") or "")
                for d in data
                if d.get("sku")
            }
        else:
            return {}, False
        return skus, bool(skus)
    except Exception as e:
        logger.warning("Erro ao buscar Shopee: %s", e)
        return {}, False


# ─────────────────────────────────────────────
# Conferência principal
# ─────────────────────────────────────────────

def executar_conferencia(bling_client) -> dict:
    """
    Executa a conferência completa e salva resultado.
    Deve ser chamada em thread separada.
    """
    inicio = time.time()
    _set_estado("rodando", 5, "bling", "Coletando produtos do Bling...")

    try:
        # 1. Bling
        skus_bling, sem_sku_bling = _fetch_bling(bling_client)
        _set_estado("rodando", 30, "ml", "Coletando anúncios do Mercado Livre...")

        # 2. ML
        skus_ml, sem_sku_ml, ml_ok = _fetch_ml()
        _set_estado("rodando", 50, "shopify", "Coletando variantes da Shopify...")

        # 3. Shopify
        skus_shopify, shopify_ok = _fetch_shopify()
        _set_estado("rodando", 65, "amazon", "Coletando inventário da Amazon...")

        # 4. Amazon
        skus_amazon, amazon_ok = _fetch_amazon()
        _set_estado("rodando", 78, "shopee", "Coletando mapeamento da Shopee...")

        # 5. Shopee
        skus_shopee, shopee_ok = _fetch_shopee()
        _set_estado("rodando", 88, "cruzamento", "Cruzando SKUs...")

        # ── Cruzamento ──────────────────────────────────────────
        # Todos os SKUs únicos (union de todos os canais)
        todos_skus: set[str] = set(skus_bling.keys())
        if ml_ok:
            todos_skus |= set(skus_ml.keys())
        if shopify_ok:
            todos_skus |= set(skus_shopify.keys())
        if amazon_ok:
            todos_skus |= set(skus_amazon.keys())
        if shopee_ok:
            todos_skus |= set(skus_shopee.keys())

        matrix: list[dict] = []
        for sku in sorted(todos_skus):
            no_bling = sku in skus_bling
            no_ml = sku in skus_ml if ml_ok else None
            no_shopify = sku in skus_shopify if shopify_ok else None
            no_amazon = sku in skus_amazon if amazon_ok else None
            no_shopee = sku in skus_shopee if shopee_ok else None

            canais_conectados = (
                (["ml"] if ml_ok else [])
                + (["shopify"] if shopify_ok else [])
                + (["amazon"] if amazon_ok else [])
                + (["shopee"] if shopee_ok else [])
            )
            n_conectados = len(canais_conectados)
            n_presentes = sum(
                1
                for v in [no_ml, no_shopify, no_amazon, no_shopee]
                if v is True
            )
            cobertura = round(n_presentes / n_conectados, 2) if n_conectados else 1.0

            bling_info = skus_bling.get(sku, {})
            row: dict[str, Any] = {
                "sku": sku,
                "nome": bling_info.get("nome", ""),
                "estoque_bling": bling_info.get("estoque", 0),
                "preco_bling": bling_info.get("preco", 0.0),
                "id_bling": bling_info.get("id_bling", ""),
                "presente_bling": no_bling,
                "cobertura": cobertura,
            }
            if ml_ok:
                row["presente_ml"] = no_ml
                row["ml_id"] = skus_ml.get(sku, "")
            if shopify_ok:
                row["presente_shopify"] = no_shopify
                row["shopify_variant_id"] = skus_shopify.get(sku, "")
            if amazon_ok:
                row["presente_amazon"] = no_amazon
                row["amazon_qty"] = skus_amazon.get(sku, 0)
            if shopee_ok:
                row["presente_shopee"] = no_shopee
                row["shopee_item_id"] = skus_shopee.get(sku, "")

            matrix.append(row)

        # Ordena: primeiro os que têm mais lacunas
        matrix.sort(key=lambda x: (x["cobertura"], x["sku"]))

        # Stats por canal
        def _stats_canal(skus_canal: dict, canal_ok: bool) -> dict:
            if not canal_ok:
                return {"conectado": False}
            presentes = sum(1 for s in skus_canal if s in skus_bling)
            sem_bling = sum(1 for s in skus_canal if s not in skus_bling)
            return {
                "conectado": True,
                "total": len(skus_canal),
                "presentes_em_bling": presentes,
                "sem_bling": sem_bling,
            }

        # Bling sem canal
        def _bling_sem(skus_canal: dict, canal_ok: bool) -> int:
            if not canal_ok:
                return 0
            return sum(1 for s in skus_bling if s not in skus_canal)

        resultado = {
            "executado_em": datetime.now(timezone.utc).isoformat(),
            "duracao_segundos": round(time.time() - inicio, 1),
            "stats": {
                "total_bling": len(skus_bling),
                "sem_sku_no_bling": sem_sku_bling,
                "total_skus_universo": len(todos_skus),
                "ml": _stats_canal(skus_ml, ml_ok),
                "shopify": _stats_canal(skus_shopify, shopify_ok),
                "amazon": _stats_canal(skus_amazon, amazon_ok),
                "shopee": _stats_canal(skus_shopee, shopee_ok),
                "bling_sem_ml": _bling_sem(skus_ml, ml_ok),
                "bling_sem_shopify": _bling_sem(skus_shopify, shopify_ok),
                "bling_sem_amazon": _bling_sem(skus_amazon, amazon_ok),
                "bling_sem_shopee": _bling_sem(skus_shopee, shopee_ok),
            },
            "sem_sku_ml": sem_sku_ml[:200],  # cap 200
            "matrix": matrix,
        }

        _salvar_resultado(resultado)
        _set_estado("concluido", 100, "concluido", f"Conferência concluída — {len(matrix)} SKUs analisados")
        return resultado

    except Exception as e:
        logger.exception("Erro na conferência de SKU: %s", e)
        _set_estado("erro", 0, "erro", str(e))
        return {"ok": False, "erro": str(e)}


# ─────────────────────────────────────────────
# Thread helper
# ─────────────────────────────────────────────

_thread_conf: threading.Thread | None = None


def iniciar_conferencia_em_background(bling_client) -> bool:
    """Inicia a conferência em thread separada. Retorna False se já estiver rodando."""
    global _thread_conf
    if _thread_conf and _thread_conf.is_alive():
        return False
    _thread_conf = threading.Thread(
        target=executar_conferencia,
        args=(bling_client,),
        daemon=True,
        name="conferencia-sku",
    )
    _thread_conf.start()
    return True
