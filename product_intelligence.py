from __future__ import annotations

from typing import Any


def _norm(value: float, min_v: float, max_v: float) -> float:
    if max_v <= min_v:
        return 0.0
    v = (value - min_v) / (max_v - min_v)
    return max(0.0, min(1.0, v))


def _inv_norm(value: float, min_v: float, max_v: float) -> float:
    return 1.0 - _norm(value, min_v, max_v)


def classify_sie(score: float) -> str:
    if score >= 0.80:
        return "estrela"
    if score >= 0.60:
        return "saudavel"
    if score >= 0.40:
        return "atencao"
    return "problema"


def calculate_sie(data: dict[str, Any]) -> dict[str, Any]:
    velocidade_venda = float(data.get("velocidade_venda", 0.0))
    share_faturamento = float(data.get("share_faturamento", 0.0))
    margem_real = float(data.get("margem_real", 0.0))
    prazo_entrega_fornecedor = float(data.get("prazo_entrega_fornecedor", 30.0))
    prazo_pagamento_fornecedor = float(data.get("prazo_pagamento_fornecedor", 0.0))
    devolucao_defeito = float(data.get("devolucao_defeito", 0.0))
    devolucao_transporte = float(data.get("devolucao_transporte", 0.0))
    tempo_para_vender = float(data.get("tempo_para_vender", 30.0))

    score_comercial = (
        _norm(velocidade_venda, 0, 100) * 0.40
        + _norm(share_faturamento, 0, 1) * 0.30
        + _norm(margem_real, 0, 100) * 0.30
    )

    score_operacional = (
        _inv_norm(prazo_entrega_fornecedor, 1, 60) * 0.60
        + _norm(prazo_pagamento_fornecedor, 0, 90) * 0.40
    )

    score_risco = 1.0 - (
        _norm(devolucao_defeito, 0, 0.30) * 0.60
        + _norm(devolucao_transporte, 0, 0.30) * 0.40
    )
    score_risco = max(0.0, min(1.0, score_risco))

    icg = prazo_pagamento_fornecedor / tempo_para_vender if tempo_para_vender > 0 else 0.0
    score_financeiro = _norm(icg, 0, 3)

    sie = (
        score_comercial * 0.40
        + score_operacional * 0.15
        + score_risco * 0.25
        + score_financeiro * 0.20
    )

    return {
        "sie": round(sie, 4),
        "classificacao": classify_sie(sie),
        "icg": round(icg, 4),
        "scores": {
            "comercial": round(score_comercial, 4),
            "operacional": round(score_operacional, 4),
            "risco": round(score_risco, 4),
            "financeiro": round(score_financeiro, 4),
        },
        "entradas": {
            "velocidade_venda": velocidade_venda,
            "share_faturamento": share_faturamento,
            "margem_real": margem_real,
            "prazo_entrega_fornecedor": prazo_entrega_fornecedor,
            "prazo_pagamento_fornecedor": prazo_pagamento_fornecedor,
            "tempo_para_vender": tempo_para_vender,
            "devolucao_defeito": devolucao_defeito,
            "devolucao_transporte": devolucao_transporte,
        },
    }
