# -*- coding: utf-8 -*-
"""
routes/ml_ads.py — Simulador de ADS Mercado Livre
Endpoints para coleta de histórico, registro de investimentos e simulação.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import HTMLResponse
from pathlib import Path
from pydantic import BaseModel

from services.ml_ads_data import (
    init_db,
    sync_all,
    get_sync_status,
    get_produtos,
    get_historico_semanal,
    get_historico_semanal_global,
    get_consolidado,
    get_ranking_roi,
    salvar_investimento,
    get_investimentos,
    deletar_investimento,
    simular,
    calcular_meta,
)

logger = logging.getLogger("shinsei.ml_ads_routes")
router = APIRouter(prefix="/ml-ads", tags=["ml-ads"])

BASE_DIR = Path(__file__).parent.parent

# Inicializa o banco na importação
try:
    init_db()
except Exception as e:
    logger.warning("init_db: %s", e)


# ── Modelos ───────────────────────────────────────────────────────────────────

class InvestimentoIn(BaseModel):
    item_id:       Optional[str] = None
    date_from:     str
    date_to:       str
    investment:    float
    campaign_name: Optional[str] = ""
    notes:         Optional[str] = ""


class SimularIn(BaseModel):
    item_id:      str
    investment:   float = 0.0
    price:        Optional[float] = None
    discount_pct: float = 0.0
    weeks:        int = 4


# ── Sincronização ─────────────────────────────────────────────────────────────

@router.post("/sync")
async def iniciar_sync(background_tasks: BackgroundTasks, months: int = 12):
    """Dispara coleta de orders ML em background."""
    status = get_sync_status()
    if status.get("running"):
        return {"ok": False, "msg": "Sincronização já em andamento", "status": status}
    background_tasks.add_task(sync_all, 733168645, months)
    return {"ok": True, "msg": f"Sincronização iniciada (últimos {months} meses)"}


@router.get("/sync/status")
def status_sync():
    """Retorna progresso da sincronização em andamento."""
    return get_sync_status()


# ── Produtos ──────────────────────────────────────────────────────────────────

@router.get("/produtos")
def listar_produtos(limit: int = Query(200, le=500)):
    """Lista produtos com totais de vendas do último ano."""
    produtos = get_produtos(limit)
    return {"total": len(produtos), "produtos": produtos}


@router.get("/produto/{item_id}/historico")
def historico_produto(item_id: str):
    """Histórico semanal de vendas + investimentos de um produto."""
    h = get_historico_semanal(item_id)
    if not h:
        raise HTTPException(404, "Produto sem histórico de vendas")
    return {"item_id": item_id, "semanas": h}


@router.get("/historico-global")
def historico_global():
    """Histórico semanal agregado de todos os produtos."""
    return {"semanas": get_historico_semanal_global()}


@router.get("/consolidado")
def consolidado():
    """Visão consolidada: faturamento, investimento, desconto cruzados por semana."""
    return get_consolidado()


@router.get("/ranking-roi")
def ranking_roi(min_semanas: int = Query(1, ge=1)):
    """Produtos ordenados por ROAS histórico de investimento em ADS."""
    return get_ranking_roi(min_semanas_invest=min_semanas)


# ── Investimentos de campanha ─────────────────────────────────────────────────

@router.get("/investimentos")
def listar_investimentos(item_id: Optional[str] = None):
    """Lista investimentos de campanha registrados."""
    return {"investimentos": get_investimentos(item_id)}


@router.post("/investimentos")
def criar_investimento(body: InvestimentoIn):
    """Registra um período de investimento em campanha."""
    if body.investment <= 0:
        raise HTTPException(400, "Investimento deve ser > 0")
    row_id = salvar_investimento(
        body.item_id, body.date_from, body.date_to,
        body.investment, body.campaign_name or "", body.notes or ""
    )
    return {"ok": True, "id": row_id}


@router.delete("/investimentos/{inv_id}")
def remover_investimento(inv_id: int):
    """Remove um registro de investimento."""
    deletar_investimento(inv_id)
    return {"ok": True}


# ── Simulação ─────────────────────────────────────────────────────────────────

@router.post("/simular")
def simular_ads(body: SimularIn):
    """
    Simula faturamento projetado com base em investimento + preço + desconto.
    """
    resultado = simular(
        item_id=body.item_id,
        investment=body.investment,
        price=body.price or 0,
        discount_pct=body.discount_pct,
        weeks=body.weeks,
    )
    if not resultado.get("ok"):
        raise HTTPException(400, resultado.get("msg", "Erro na simulação"))
    return resultado


@router.get("/simular")
def simular_ads_get(
    item_id: str,
    investment: float = 0,
    price: float = 0,
    discount_pct: float = 0,
    weeks: int = 4,
):
    """Simulação via GET (para uso direto na URL)."""
    resultado = simular(item_id, investment, price, discount_pct, weeks)
    if not resultado.get("ok"):
        raise HTTPException(400, resultado.get("msg", "Erro na simulação"))
    return resultado


# ── Calculadora de meta ───────────────────────────────────────────────────────

class MetaIn(BaseModel):
    item_id:          str
    meta_unidades:    Optional[float] = None
    meta_faturamento: Optional[float] = None
    meta_lucro:       Optional[float] = None
    margem_pct:       Optional[float] = None   # margem bruta do produto (%)
    custo_unitario:   Optional[float] = None   # custo do produto (R$)
    weeks:            int = 4
    preco_atual:      Optional[float] = None


@router.post("/calcular-meta")
def calcular_meta_endpoint(body: MetaIn):
    """
    Dado uma meta de unidades, faturamento ou lucro, retorna três cenários
    (só desconto / só investimento / mix) com preço sugerido e investimento necessário.
    """
    if not body.meta_unidades and not body.meta_faturamento and not body.meta_lucro:
        raise HTTPException(400, "Informe meta_unidades, meta_faturamento ou meta_lucro")
    if body.meta_lucro and not body.custo_unitario:
        raise HTTPException(400, "Para meta de lucro, informe custo_unitario")

    resultado = calcular_meta(
        item_id=body.item_id,
        meta_unidades=body.meta_unidades,
        meta_faturamento=body.meta_faturamento,
        meta_lucro=body.meta_lucro,
        weeks=body.weeks,
        preco_atual=body.preco_atual,
        custo_unitario=body.custo_unitario,
        margem_minima_pct=body.margem_pct,
    )
    if not resultado.get("ok"):
        raise HTTPException(400, resultado.get("msg"))
    return resultado


# ── Interface HTML ────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def pagina_simulador():
    page = BASE_DIR / "pages" / "simulador_ads.html"
    if not page.exists():
        raise HTTPException(404, "Página não encontrada")
    return HTMLResponse(page.read_text(encoding="utf-8"))
