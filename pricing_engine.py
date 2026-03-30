from __future__ import annotations

from typing import Any

from cost_engine import calculate_cost_breakdown


def _first_number(*values: Any, default: float = 0.0) -> float:
    for value in values:
        if value is None:
            continue
        if isinstance(value, (int, float)):
            return float(value)
        txt = str(value).strip().replace("R$", "").replace(" ", "")
        if not txt:
            continue
        if "," in txt and "." in txt:
            txt = txt.replace(".", "").replace(",", ".")
        else:
            txt = txt.replace(",", ".")
        try:
            return float(txt)
        except Exception:
            continue
    return default


def solve_target_price(base_data: dict[str, Any], margem_alvo_percentual: float, *, max_iter: int = 25) -> dict[str, Any]:
    margem_fracao = max(min(float(margem_alvo_percentual), 99.0), 0.0) / 100.0
    initial_cost = (
        _first_number(base_data.get("custo_produto"))
        + _first_number(base_data.get("embalagem"))
        + _first_number(base_data.get("insumos"))
        + _first_number(base_data.get("frete"))
        + _first_number(base_data.get("taxa_fixa"))
        + _first_number(base_data.get("custo_operacional_unitario"))
        + _first_number(base_data.get("custo_estrutural_rateado"))
    )
    if initial_cost <= 0:
        initial_cost = 0.01

    preco = max(initial_cost / max(1.0 - margem_fracao, 0.01), 0.01)
    breakdown = {}
    for _ in range(max_iter):
        breakdown = calculate_cost_breakdown({**base_data, "preco_venda": preco})
        divisor = max(1.0 - margem_fracao, 0.01)
        novo_preco = breakdown["custo_total"] / divisor
        if abs(novo_preco - preco) < 0.01:
            preco = novo_preco
            break
        preco = novo_preco

    breakdown = calculate_cost_breakdown({**base_data, "preco_venda": preco})
    lucro = preco - breakdown["custo_total"]
    margem_real = (lucro / preco * 100.0) if preco > 0 else 0.0
    return {
        "preco_sugerido": round(preco, 2),
        "lucro": round(lucro, 2),
        "margem_real_percentual": round(margem_real, 2),
        "custo_total": round(breakdown["custo_total"], 2),
        "custos": breakdown,
    }


def build_preview(produto: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    custo = _first_number(produto.get("precoCusto"), produto.get("preco_custo"), produto.get("custo"), default=0.0)
    preco_atual = _first_number(produto.get("preco"), produto.get("precoVenda"), produto.get("valor"), default=0.0)
    embalagem = _first_number(payload.get("embalagem"), default=0.0)
    insumos = _first_number(payload.get("insumos"), default=0.0)
    frete = _first_number(payload.get("frete"), default=0.0)
    taxa_fixa = _first_number(payload.get("taxa_fixa"), default=0.0)
    imposto = _first_number(payload.get("imposto"), default=0.0)
    valor_alvo = _first_number(payload.get("valor_alvo"), payload.get("margem"), default=30.0)
    comissao_percentual = _first_number(payload.get("comissao_percentual"), default=0.0)
    custo_operacional_unitario = _first_number(payload.get("custo_operacional_unitario"), default=0.0)
    custo_estrutural_rateado = _first_number(payload.get("custo_estrutural_rateado"), default=0.0)
    devolucao_defeito = _first_number(payload.get("devolucao_defeito"), default=0.0)
    devolucao_transporte = _first_number(payload.get("devolucao_transporte"), default=0.0)

    calc = solve_target_price(
        {
            "custo_produto": custo,
            "embalagem": embalagem,
            "insumos": insumos,
            "frete": frete,
            "taxa_fixa": taxa_fixa,
            "comissao_percentual": comissao_percentual,
            "imposto_percentual": imposto,
            "custo_operacional_unitario": custo_operacional_unitario,
            "custo_estrutural_rateado": custo_estrutural_rateado,
            "devolucao_defeito": devolucao_defeito,
            "devolucao_transporte": devolucao_transporte,
        },
        valor_alvo,
    )

    return {
        "produto_bling": produto,
        "precificacao": {
            "preco_atual": round(preco_atual, 2),
            "preco_sugerido": calc["preco_sugerido"],
            "lucro": calc["lucro"],
            "margem_real_percentual": calc["margem_real_percentual"],
            "custo_total": calc["custo_total"],
            "custos": calc["custos"],
            "parametros": {
                "embalagem": embalagem,
                "insumos": insumos,
                "frete": frete,
                "taxa_fixa": taxa_fixa,
                "imposto_percentual": imposto,
                "comissao_percentual": comissao_percentual,
                "margem_alvo_percentual": valor_alvo,
                "custo_operacional_unitario": custo_operacional_unitario,
                "custo_estrutural_rateado": custo_estrutural_rateado,
                "devolucao_defeito": devolucao_defeito,
                "devolucao_transporte": devolucao_transporte,
            },
        },
    }
