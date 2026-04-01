from __future__ import annotations

from typing import Any


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _share(value: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return value / total


def allocate_by_historical_revenue(cost_total: float, products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total_revenue = sum(_safe_float(p.get("faturamento_periodo")) for p in products)
    results = []
    for p in products:
        revenue = _safe_float(p.get("faturamento_periodo"))
        units = max(_safe_float(p.get("quantidade_vendida_periodo"), 1.0), 1.0)
        share = _share(revenue, total_revenue)
        allocated = cost_total * share
        results.append({
            "sku": p.get("sku"),
            "share_faturamento_periodo": round(share, 6),
            "custo_alocado_historico_vendido": round(allocated, 2),
            "custo_unitario_historico_vendido": round(allocated / units, 4),
        })
    return results


def allocate_inventory_by_purchase_value(cost_total: float, products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total_purchase_value = sum(_safe_float(p.get("valor_compra_estoque")) for p in products)
    results = []
    for p in products:
        purchase_value = _safe_float(p.get("valor_compra_estoque"))
        stock_units = max(_safe_float(p.get("estoque_atual"), 1.0), 1.0)
        share = _share(purchase_value, total_purchase_value)
        allocated = cost_total * share
        results.append({
            "sku": p.get("sku"),
            "share_valor_compra_estoque": round(share, 6),
            "custo_alocado_estoque_por_compra": round(allocated, 2),
            "custo_unitario_estoque_por_compra": round(allocated / stock_units, 4),
        })
    return results


def allocate_inventory_by_sale_value(cost_total: float, products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total_sale_value = sum(_safe_float(p.get("valor_venda_estoque")) for p in products)
    results = []
    for p in products:
        sale_value = _safe_float(p.get("valor_venda_estoque"))
        stock_units = max(_safe_float(p.get("estoque_atual"), 1.0), 1.0)
        share = _share(sale_value, total_sale_value)
        allocated = cost_total * share
        results.append({
            "sku": p.get("sku"),
            "share_valor_venda_estoque": round(share, 6),
            "custo_alocado_estoque_por_venda": round(allocated, 2),
            "custo_unitario_estoque_por_venda": round(allocated / stock_units, 4),
        })
    return results


def allocate_sold_units_by_revenue(cost_total: float, products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total_revenue = sum(_safe_float(p.get("faturamento_periodo")) for p in products if _safe_float(p.get("quantidade_vendida_periodo")) > 0)
    results = []
    for p in products:
        sold_units = _safe_float(p.get("quantidade_vendida_periodo"))
        if sold_units <= 0:
            continue
        revenue = _safe_float(p.get("faturamento_periodo"))
        share = _share(revenue, total_revenue)
        allocated = cost_total * share
        results.append({
            "sku": p.get("sku"),
            "share_produto_vendido_no_faturamento": round(share, 6),
            "custo_alocado_vendido_por_faturamento": round(allocated, 2),
            "custo_unitario_vendido_por_faturamento": round(allocated / sold_units, 4),
        })
    return results


def build_cost_views(cost_total: float, products: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "historico_vendido": allocate_by_historical_revenue(cost_total, products),
        "estoque_por_compra": allocate_inventory_by_purchase_value(cost_total, products),
        "estoque_por_venda": allocate_inventory_by_sale_value(cost_total, products),
        "vendido_por_faturamento": allocate_sold_units_by_revenue(cost_total, products),
    }


def get_structural_cost_for_product(cost_views: dict[str, list[dict[str, Any]]], sku: str, strategy: str = "vendido_por_faturamento") -> float:
    items = cost_views.get(strategy, [])
    for item in items:
        if str(item.get("sku")) == str(sku):
            for key, value in item.items():
                if key.startswith("custo_unitario_"):
                    return _safe_float(value)
    return 0.0
