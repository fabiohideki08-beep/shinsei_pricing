"""
services/bling_dimensions.py
Lookup de peso de produtos usando o cache Shopify local.

O Shopify já importa o peso do Bling via sincronização (campo variant.weight).
O índice é construído a partir de data/all_products_cache.json
e indexado por product_id Shopify (acessível via data-product-id nos cards).

Dimensões físicas (largura/altura/profundidade) não são campos nativos do Shopify,
portanto são mantidos com valores padrão razoáveis para o cálculo do Melhor Envio.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("shinsei.bling_dimensions")

_SHOPIFY_CACHE_PATH = Path(__file__).parent.parent / "data" / "all_products_cache.json"

DEFAULT_DIMS: dict = {
    "peso_kg":      0.3,
    "largura":      12.0,   # cm
    "altura":       10.0,   # cm
    "profundidade": 15.0,   # cm
}

# Índice em memória: {str(product_id): peso_kg}
_weight_index: dict[str, float] | None = None


def _load_weight_index() -> dict[str, float]:
    """Lê all_products_cache.json e devolve {str(product_id): peso_kg}."""
    global _weight_index
    if _weight_index is not None:
        return _weight_index

    index: dict[str, float] = {}
    try:
        data = json.loads(_SHOPIFY_CACHE_PATH.read_bytes())
        products = data if isinstance(data, list) else data.get("products", data.get("data", []))
        for p in products:
            pid = str(p.get("id") or "").strip()
            if not pid:
                continue
            for v in p.get("variants", []):
                peso = float(v.get("weight") or 0)
                if peso > 0:
                    index[pid] = peso
                    break   # primeiro variant com peso é suficiente
    except Exception as exc:
        logger.warning("Falha ao carregar índice de pesos Shopify: %s", exc)

    _weight_index = index
    logger.info("Índice de pesos carregado: %d produtos", len(index))
    return index


def get_dims_for_product(product_id: str | int | None) -> dict:
    """
    Retorna dimensões para um produto Shopify pelo ID.
    Peso vem do cache Shopify; largura/altura/profundidade usam defaults.
    """
    dims = dict(DEFAULT_DIMS)

    if not product_id:
        return dims

    index = _load_weight_index()
    peso = index.get(str(product_id).strip())
    if peso and peso > 0:
        dims["peso_kg"] = peso

    return dims


def invalidate_cache() -> None:
    """Força recarga do índice na próxima chamada (ex: após atualizar cache Shopify)."""
    global _weight_index
    _weight_index = None
