# -*- coding: utf-8 -*-
"""
Rotas de controle automático de visibilidade por estoque — Shopify + GMC.

GET  /shopify/estoque/status      — relatório do último ciclo (dry_run)
POST /shopify/estoque/executar    — executa ciclo real (ocultar/restaurar)
POST /shopify/estoque/simular     — simula sem modificar (dry_run=True)
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from services.shopify_estoque_auto import executar as _executar

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/shopify/estoque", tags=["shopify-estoque"])


@router.get("/status")
def estoque_status():
    """
    Simula o ciclo (dry_run=True) e retorna o relatório sem modificar nada.
    Útil para ver quantos produtos seriam ocultados/restaurados.
    """
    resultado = _executar(dry_run=True)
    return {
        "simulado_em": datetime.now().isoformat(),
        "seria_ocultado": len(resultado.get("ocultados", [])),
        "seria_restaurado": len(resultado.get("restaurados", [])),
        "skip_tags": len(resultado.get("ignorados_skip", [])),
        "detalhes": resultado,
    }


@router.post("/executar")
def estoque_executar():
    """
    Executa o ciclo real:
    - Produtos ativos com estoque = 0 → draft (some do site e GMC)
    - Produtos draft (ocultados por nós) com estoque > 0 → active
    """
    resultado = _executar(dry_run=False)
    return {
        "executado_em": datetime.now().isoformat(),
        "ocultados": len(resultado.get("ocultados", [])),
        "restaurados": len(resultado.get("restaurados", [])),
        "erros": len(resultado.get("erros", [])),
        "detalhes": resultado,
    }


@router.post("/simular")
def estoque_simular():
    """Alias de /status — simula sem modificar."""
    return estoque_status()
