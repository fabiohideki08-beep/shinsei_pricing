from __future__ import annotations

from typing import Any
from fastapi import APIRouter

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _default_sales() -> dict[str, Any]:
    return {
        "faturamento_total": 0.0,
        "pedidos": 0,
        "ticket_medio": 0.0,
        "produtos_vendidos": 0,
        "receita_por_canal": [],
    }


def _default_intelligence() -> dict[str, Any]:
    return {
        "lucro_real_total": 0.0,
        "margem_real_media": 0.0,
        "icg_medio": 0.0,
        "oee_medio": 0.0,
        "produtos_estrela": 0,
        "produtos_atencao": 0,
        "produtos_problema": 0,
        "modulos": {
            "pricing": "ativo",
            "cost": "ativo",
            "sie": "ativo",
            "allocation": "ativo",
            "oee": "ativo",
        },
    }


@router.get("/vendas")
def dashboard_vendas() -> dict[str, Any]:
    return _default_sales()


@router.get("/inteligente")
def dashboard_inteligente() -> dict[str, Any]:
    return _default_intelligence()


@router.get("/pricing")
def dashboard_pricing() -> dict[str, Any]:
    return {
        "preco_medio_atual": 0.0,
        "preco_medio_sugerido": 0.0,
        "itens_autoaplicados": 0,
        "pendencias_fila": 0,
        "principais_distorcoes": [],
    }


@router.get("/cost")
def dashboard_cost() -> dict[str, Any]:
    return {
        "custo_base_medio": 0.0,
        "custo_risco_medio": 0.0,
        "custo_real_medio": 0.0,
        "custo_oculto_periodo": 0.0,
        "vazamentos": [],
    }


@router.get("/sie")
def dashboard_sie() -> dict[str, Any]:
    return {
        "produtos_estrela": 0,
        "produtos_saudaveis": 0,
        "produtos_atencao": 0,
        "produtos_problema": 0,
        "ranking": [],
    }


@router.get("/allocation")
def dashboard_allocation() -> dict[str, Any]:
    return {
        "custo_estrutural_total": 0.0,
        "skus_no_rateio": 0,
        "maior_share": 0.0,
        "custo_estrutural_unitario_medio": 0.0,
    }


@router.get("/oee")
def dashboard_oee() -> dict[str, Any]:
    return {
        "eficiencia_equipe": 0.0,
        "eficiencia_materiais": 0.0,
        "eficiencia_endereco": 0.0,
        "oee_consolidado": 0.0,
        "pontos_criticos": [],
    }
