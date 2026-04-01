from __future__ import annotations

from typing import Any


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return default
    text = text.replace("R$", "").replace(" ", "")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", ".")
    try:
        return float(text)
    except Exception:
        return default


def calculate_cost_breakdown(data: dict[str, Any]) -> dict[str, float]:
    custo_produto = _to_float(data.get("custo_produto"))
    embalagem = _to_float(data.get("embalagem"))
    insumos = _to_float(data.get("insumos"))
    frete = _to_float(data.get("frete"))
    taxa_fixa = _to_float(data.get("taxa_fixa"))
    custo_operacional_unitario = _to_float(data.get("custo_operacional_unitario"))
    custo_estrutural_rateado = _to_float(data.get("custo_estrutural_rateado"))

    preco_venda = _to_float(data.get("preco_venda"))
    comissao_percentual = _to_float(data.get("comissao_percentual"))
    imposto_percentual = _to_float(data.get("imposto_percentual"))

    devolucao_defeito = max(_to_float(data.get("devolucao_defeito")), 0.0)
    devolucao_transporte = max(_to_float(data.get("devolucao_transporte")), 0.0)
    custo_medio_reposicao = _to_float(data.get("custo_medio_reposicao"), default=custo_produto)

    comissao = preco_venda * (comissao_percentual / 100.0)
    imposto = preco_venda * (imposto_percentual / 100.0)

    custo_base = (
        custo_produto
        + embalagem
        + insumos
        + frete
        + taxa_fixa
        + custo_operacional_unitario
        + custo_estrutural_rateado
        + comissao
        + imposto
    )

    custo_risco_defeito = devolucao_defeito * custo_medio_reposicao
    custo_risco_transporte = devolucao_transporte * max(custo_medio_reposicao, custo_produto)
    custo_risco = custo_risco_defeito + custo_risco_transporte

    custo_total = custo_base + custo_risco

    return {
        "custo_produto": round(custo_produto, 2),
        "embalagem": round(embalagem, 2),
        "insumos": round(insumos, 2),
        "frete": round(frete, 2),
        "taxa_fixa": round(taxa_fixa, 2),
        "custo_operacional_unitario": round(custo_operacional_unitario, 2),
        "custo_estrutural_rateado": round(custo_estrutural_rateado, 2),
        "comissao": round(comissao, 2),
        "imposto": round(imposto, 2),
        "custo_risco_defeito": round(custo_risco_defeito, 2),
        "custo_risco_transporte": round(custo_risco_transporte, 2),
        "custo_risco": round(custo_risco, 2),
        "custo_base": round(custo_base, 2),
        "custo_total": round(custo_total, 2),
    }
