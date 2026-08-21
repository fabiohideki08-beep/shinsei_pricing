"""
shopee_pricing_engine.py — Shinsei Pricing
Taxas Shopee Brasil em tempo real, derivadas de pedidos recentes.
Cache de 12h.

A Shopee não expõe comissões via API pública. Este engine:
  1. Analisa os últimos 20 pedidos via /payment/get_escrow_detail
     para calcular a comissão efetiva média real da loja
  2. Fallback: tabela de comissões Shopee Brasil por categoria (2024-2025)
"""
from __future__ import annotations
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR   = Path(__file__).parent
CACHE_PATH = BASE_DIR / "data" / "shopee_taxas_cache.json"
CACHE_TTL  = 43200  # 12h

# ── Tabela Shopee Brasil (valores sem negociação especial) ────────────────────
# commission + payment_fee (médias publicadas seller center BR 2024-2025)
_SHOPEE_FEES_BR: dict[str, dict] = {
    "beleza":            {"comissao": 0.1257, "pagamento": 0.02},
    "saude":             {"comissao": 0.1257, "pagamento": 0.02},
    "moda":              {"comissao": 0.12,   "pagamento": 0.02},
    "calcados":          {"comissao": 0.12,   "pagamento": 0.02},
    "eletronicos":       {"comissao": 0.10,   "pagamento": 0.02},
    "informatica":       {"comissao": 0.10,   "pagamento": 0.02},
    "games":             {"comissao": 0.10,   "pagamento": 0.02},
    "casa":              {"comissao": 0.11,   "pagamento": 0.02},
    "cozinha":           {"comissao": 0.11,   "pagamento": 0.02},
    "esportes":          {"comissao": 0.11,   "pagamento": 0.02},
    "brinquedos":        {"comissao": 0.11,   "pagamento": 0.02},
    "pet":               {"comissao": 0.10,   "pagamento": 0.02},
    "alimentos":         {"comissao": 0.10,   "pagamento": 0.02},
    "automotivo":        {"comissao": 0.10,   "pagamento": 0.02},
    "ferramentas":       {"comissao": 0.10,   "pagamento": 0.02},
    "default":           {"comissao": 0.1257, "pagamento": 0.02},  # derivado de pedidos reais Shopee BR
}


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict):
    try:
        CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _load_tokens(akg: bool = False) -> dict | None:
    if akg:
        # AKG: token file dedicado (ainda sem suporte a auto-refresh)
        path = BASE_DIR / "data" / "shopee_tokens_akg.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return None
    # Shinsei: tenta via service (suporta auto-refresh)
    try:
        from services.shopee import _carregar_tokens
        t = _carregar_tokens()
        if t and t.get("access_token"):
            return t
    except Exception:
        pass
    path = BASE_DIR / "data" / "shopee_tokens.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _assinar(path: str, ts: int, partner_id: int, partner_key: str,
             access_token: str = "", shop_id: int = 0) -> str:
    # Tenta usar o signing do service (credenciais corretas)
    try:
        from services.shopee import _assinar as _svc_assinar
        return _svc_assinar(path, ts, access_token, shop_id)
    except Exception:
        pass
    base = f"{partner_id}{path}{ts}{access_token}{shop_id if shop_id else ''}"
    return hmac.new(partner_key.encode(), base.encode(), hashlib.sha256).hexdigest()


def _fallback_taxa(price: float, category_hint: str = "") -> dict:
    cat   = (category_hint or "default").lower()
    rates = _SHOPEE_FEES_BR.get(cat, _SHOPEE_FEES_BR["default"])
    com   = rates["comissao"]
    pag   = rates["pagamento"]
    total = round(price * (com + pag), 2)
    return {
        "comissao_pct":  com,
        "pagamento_pct": pag,
        "total_pct":     com + pag,
        "total_shopee":  total,
        "voce_recebe":   round(price - total, 2),
        "source":        "tabela_br",
        "categoria":     cat,
    }


def _get_shopee_http(path: str, params: dict, tokens: dict) -> dict:
    """Faz GET na Shopee API. path deve ser sem /api/v2 (ex: /order/get_order_list)."""
    try:
        import httpx
        from services.shopee import _assinar as _svc_sign, PARTNER_ID, SHOP_ID, API_BASE
        ts    = int(time.time())
        at    = tokens.get("access_token", "")
        sid   = int(tokens.get("shop_id", SHOP_ID or 0))
        # Shopee exige /api/v2 + path no base_string para signing
        sign_path = f"/api/v2{path}"
        p = {"partner_id": PARTNER_ID, "shop_id": sid, "timestamp": ts,
             "access_token": at, "sign": _svc_sign(sign_path, ts, at, sid), **params}
        r = httpx.get(API_BASE + path, params=p, timeout=15)
        return r.json()
    except Exception as e:
        logger.warning("Shopee HTTP falhou: %s", e)
        return {}


def _calcular_taxa_efetiva_de_pedidos() -> dict | None:
    """
    Analisa os últimos pedidos via Shopee API para calcular a taxa efetiva real.
    Retorna {comissao_pct, pagamento_pct, total_pct} ou None se não disponível.
    """
    tokens = _load_tokens()
    if not tokens or not tokens.get("access_token"):
        return None

    try:
        # Pedidos dos últimos 7 dias (Shopee limita janela a 15 dias)
        r = _get_shopee_http("/order/get_order_list", {
            "time_range_field": "create_time",
            "time_from": int(time.time()) - 7 * 86400,
            "time_to":   int(time.time()),
            "page_size": 20,
            "cursor":    "",
        }, tokens)

        orders = r.get("response", {}).get("order_list", [])
        if not orders or r.get("error"):
            logger.debug("Shopee orders: %s %s", r.get("error"), r.get("message",""))
            return None

        total_receita   = 0.0
        total_comissao  = 0.0
        total_pagamento = 0.0
        count = 0

        for order in orders[:10]:
            sn = order.get("order_sn", "")
            r2 = _get_shopee_http("/payment/get_escrow_detail", {"order_sn": sn}, tokens)
            resp = r2.get("response", {})
            oi   = resp.get("order_income") or resp
            # Usa preço do produto (sem frete) como base do cálculo
            items = oi.get("items") or []
            product_price = sum(
                float(it.get("discounted_price", 0) or 0) * int(it.get("quantity_purchased", 1))
                for it in items
            ) or float(oi.get("order_discounted_price", 0) or oi.get("buyer_total_amount", 0) or 0)
            if product_price <= 0:
                continue
            # Usa taxa de serviço proporcional (exclui taxas fixas)
            # Aproximação: 2% de taxa de pagamento publicada pela Shopee BR
            total_receita   += product_price
            total_comissao  += float(oi.get("commission_fee", 0) or 0)
            total_pagamento += product_price * 0.02  # taxa de pagamento padrão Shopee BR
            count += 1
            time.sleep(0.2)

        if count == 0 or total_receita == 0:
            return None

        return {
            "comissao_pct":  round(total_comissao  / total_receita, 4),
            "pagamento_pct": round(total_pagamento / total_receita, 4),
            "total_pct":     round((total_comissao + total_pagamento) / total_receita, 4),
            "source":        "pedidos_reais",
            "amostras":      count,
        }
    except Exception as e:
        logger.warning("Shopee análise de pedidos falhou: %s", e)
        return None


def get_shopee_taxa_real(
    price: float,
    category_hint: str = "",
    force_refresh: bool = False,
    akg: bool = False,
) -> dict:
    """
    Retorna taxas Shopee com cache de 12h.
    Tenta derivar da análise de pedidos reais; fallback para tabela Brasil.
    akg=True usa tokens da conta AKG (shopee_tokens_akg.json).
    """
    cache = _load_cache()
    account_suffix = "_akg" if akg else ""
    key   = f"shopee_{category_hint or 'default'}{account_suffix}"

    if not force_refresh and key in cache:
        entry = cache[key]
        if time.time() - entry.get("ts", 0) < CACHE_TTL:
            rates = entry["data"]
            total = round(price * rates["total_pct"], 2)
            return {**rates, "total_shopee": total, "voce_recebe": round(price - total, 2)}

    # Tentar derivar de pedidos reais (usa token da conta correta)
    rates = _calcular_taxa_efetiva_de_pedidos() if not akg else None  # AKG sem pedidos ainda → tabela

    if rates:
        cache[key] = {"ts": time.time(), "data": rates}
        _save_cache(cache)
        total = round(price * rates["total_pct"], 2)
        return {**rates, "total_shopee": total, "voce_recebe": round(price - total, 2)}

    # Fallback: tabela
    result = _fallback_taxa(price, category_hint)
    cache[key] = {
        "ts":   time.time(),
        "data": {
            "comissao_pct":  result["comissao_pct"],
            "pagamento_pct": result["pagamento_pct"],
            "total_pct":     result["total_pct"],
            "source":        result["source"],
            "categoria":     result.get("categoria", "default"),
        },
    }
    _save_cache(cache)
    return result
