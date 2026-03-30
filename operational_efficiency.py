from __future__ import annotations

from typing import Any


def _norm(value: float, min_v: float, max_v: float) -> float:
    if max_v <= min_v:
        return 0.0
    v = (value - min_v) / (max_v - min_v)
    return max(0.0, min(1.0, v))


def _inv_norm(value: float, min_v: float, max_v: float) -> float:
    return 1.0 - _norm(value, min_v, max_v)


def calculate_oee(data: dict[str, Any]) -> dict[str, Any]:
    custo_hora_funcionario = float(data.get("custo_hora_funcionario", 0.0))
    pedidos_por_hora = max(float(data.get("pedidos_por_hora", 1.0)), 0.01)
    tempo_medio_tarefa_min = float(data.get("tempo_medio_tarefa_min", 0.0))

    custo_embalagem = float(data.get("custo_embalagem", 0.0))
    taxa_avaria = float(data.get("taxa_avaria", 0.0))
    custo_produto = float(data.get("custo_produto", 0.0))
    reducao_avaria_embalagem_segura = float(data.get("reducao_avaria_embalagem_segura", 0.0))

    custo_fixo_endereco = float(data.get("custo_fixo_endereco", 0.0))
    custo_logistica_endereco = float(data.get("custo_logistica_endereco", 0.0))
    custo_seguranca_endereco = float(data.get("custo_seguranca_endereco", 0.0))
    acessibilidade_cliente = float(data.get("acessibilidade_cliente", 5.0))
    pedidos_mes = max(float(data.get("pedidos_mes", 1.0)), 0.01)

    custo_unitario_funcionario = custo_hora_funcionario / pedidos_por_hora
    score_funcionarios = (
        _inv_norm(custo_unitario_funcionario, 0, 30) * 0.70
        + _inv_norm(tempo_medio_tarefa_min, 0, 30) * 0.30
    )

    custo_total_embalagem = custo_embalagem + max(taxa_avaria - reducao_avaria_embalagem_segura, 0.0) * custo_produto
    score_materiais = _inv_norm(custo_total_embalagem, 0, 50)

    custo_unitario_endereco = (custo_fixo_endereco + custo_logistica_endereco + custo_seguranca_endereco) / pedidos_mes
    score_endereco = (
        _inv_norm(custo_unitario_endereco, 0, 40) * 0.75
        + _norm(acessibilidade_cliente, 0, 10) * 0.25
    )

    oee = score_funcionarios * 0.40 + score_materiais * 0.30 + score_endereco * 0.30

    return {
        "oee": round(oee, 4),
        "scores": {
            "funcionarios": round(score_funcionarios, 4),
            "materiais": round(score_materiais, 4),
            "endereco": round(score_endereco, 4),
        },
        "custos": {
            "custo_unitario_funcionario": round(custo_unitario_funcionario, 2),
            "custo_total_embalagem": round(custo_total_embalagem, 2),
            "custo_unitario_endereco": round(custo_unitario_endereco, 2),
        },
        "entradas": {
            "custo_hora_funcionario": custo_hora_funcionario,
            "pedidos_por_hora": pedidos_por_hora,
            "tempo_medio_tarefa_min": tempo_medio_tarefa_min,
            "custo_embalagem": custo_embalagem,
            "taxa_avaria": taxa_avaria,
            "custo_produto": custo_produto,
            "reducao_avaria_embalagem_segura": reducao_avaria_embalagem_segura,
            "custo_fixo_endereco": custo_fixo_endereco,
            "custo_logistica_endereco": custo_logistica_endereco,
            "custo_seguranca_endereco": custo_seguranca_endereco,
            "acessibilidade_cliente": acessibilidade_cliente,
            "pedidos_mes": pedidos_mes,
        },
    }
