# -*- coding: utf-8 -*-
"""
routes/multiempresa_correcao.py — API de correção de estoque multiempresa

Endpoints:
  GET  /multiempresa/rotas               — listar rotas de estoque
  POST /multiempresa/rotas               — criar rota
  PUT  /multiempresa/rotas/{id}          — atualizar rota
  DELETE /multiempresa/rotas/{id}        — desativar rota

  POST /multiempresa/processar/{empresa}/{id_pedido}  — processar pedido manual
  POST /multiempresa/estornar/{empresa}/{id_pedido}   — estornar pedido manual
  POST /multiempresa/job                  — disparar job completo manualmente

  GET  /multiempresa/vendas              — histórico de vendas processadas
  GET  /multiempresa/ajustes            — histórico de ajustes por item
  GET  /multiempresa/pendentes           — itens sem rota cadastrada
  GET  /multiempresa/status             — sumário geral
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter()

BASE_DIR  = Path(__file__).resolve().parent.parent
PAGES_DIR = BASE_DIR / "pages"

import hashlib
import hmac as _hmac
import os

from fastapi import Request, BackgroundTasks

# Import do serviço
from services.multiempresa_correcao import (
    init_db, listar_rotas, criar_rota, atualizar_rota, desativar_rota,
    processar_pedido, estornar_pedido, job_multiempresa,
    DB_PATH, EMPRESA_SHINSEI, EMPRESA_AKG, _hdrs,
)

import requests as _req


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# CRUD Rotas
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/multiempresa/rotas")
def get_rotas(todas: bool = False):
    init_db()
    return {"data": listar_rotas(apenas_ativas=not todas)}


@router.post("/multiempresa/rotas")
def post_rota(body: dict):
    init_db()
    obrigatorios = ("empresa_fornecedora", "deposito_fornecedor_id")
    for campo in obrigatorios:
        if campo not in body:
            raise HTTPException(400, f"Campo obrigatório ausente: {campo}")
    rid = criar_rota(body)
    return {"id": rid, "ok": True}


@router.put("/multiempresa/rotas/{id_rota}")
def put_rota(id_rota: int, body: dict):
    init_db()
    atualizar_rota(id_rota, body)
    return {"ok": True}


@router.delete("/multiempresa/rotas/{id_rota}")
def delete_rota(id_rota: int):
    init_db()
    desativar_rota(id_rota)
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# Processamento manual
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_pedido(empresa: str, id_pedido: str) -> dict:
    hdrs = _hdrs(empresa)
    resp = _req.get(
        f"https://api.bling.com.br/Api/v3/pedidos/vendas/{id_pedido}",
        headers=hdrs, timeout=20
    )
    if not resp.ok:
        raise HTTPException(502, f"Bling HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json().get("data")
    if not data:
        raise HTTPException(404, "Pedido não encontrado no Bling")
    return data


@router.post("/multiempresa/processar/{empresa}/{id_pedido}")
def post_processar(empresa: str, id_pedido: str):
    if empresa not in (EMPRESA_SHINSEI, EMPRESA_AKG):
        raise HTTPException(400, f"Empresa inválida: {empresa}")
    init_db()
    pedido = _fetch_pedido(empresa, id_pedido)
    resultado = processar_pedido(empresa, pedido)
    return resultado


@router.post("/multiempresa/estornar/{empresa}/{id_pedido}")
def post_estornar(empresa: str, id_pedido: str):
    if empresa not in (EMPRESA_SHINSEI, EMPRESA_AKG):
        raise HTTPException(400, f"Empresa inválida: {empresa}")
    init_db()
    resultado = estornar_pedido(empresa, id_pedido)
    return resultado


@router.post("/multiempresa/job")
def post_job():
    """Dispara o job completo manualmente (bloqueante, pode demorar)."""
    init_db()
    import threading
    t = threading.Thread(target=job_multiempresa, daemon=True)
    t.start()
    return {"ok": True, "msg": "Job disparado em background"}


# ─────────────────────────────────────────────────────────────────────────────
# Consultas / histórico
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/multiempresa/vendas")
def get_vendas(limit: int = 100, status: str | None = None):
    init_db()
    conn = _db()
    where = f"WHERE status='{status}'" if status else ""
    rows = conn.execute(
        f"SELECT * FROM me_vendas {where} ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return {"data": [dict(r) for r in rows], "total": len(rows)}


@router.get("/multiempresa/ajustes")
def get_ajustes(limit: int = 200, status: str | None = None, sku: str | None = None):
    init_db()
    conn = _db()
    conditions = []
    params: list = []
    if status:
        conditions.append("status=?"); params.append(status)
    if sku:
        conditions.append("sku=?"); params.append(sku)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = conn.execute(
        f"SELECT * FROM me_ajustes {where} ORDER BY id DESC LIMIT ?",
        params + [limit]
    ).fetchall()
    conn.close()
    return {"data": [dict(r) for r in rows], "total": len(rows)}


@router.get("/multiempresa/pendentes")
def get_pendentes():
    """Itens aguardando configuração de rota."""
    init_db()
    conn = _db()
    rows = conn.execute(
        """SELECT sku, empresa_vendedora, canal_venda, deposito_venda,
                  COUNT(*) as total_vendas,
                  SUM(quantidade) as total_qtd,
                  MIN(criado_em) as primeira_venda
           FROM me_ajustes
           WHERE status='pendente_configuracao_de_rota'
           GROUP BY sku, empresa_vendedora, canal_venda, deposito_venda
           ORDER BY total_vendas DESC"""
    ).fetchall()
    conn.close()
    return {"data": [dict(r) for r in rows], "total": len(rows)}


@router.get("/multiempresa/status")
def get_status():
    init_db()
    conn = _db()

    def count(tabela, where=""):
        return conn.execute(f"SELECT COUNT(*) FROM {tabela} {where}").fetchone()[0]

    resumo = {
        "rotas_ativas": count("me_rotas", "WHERE ativo=1"),
        "vendas_concluidas": count("me_vendas", "WHERE status='concluido'"),
        "vendas_erro": count("me_vendas", "WHERE status='erro'"),
        "ajustes_concluidos": count("me_ajustes", "WHERE status='concluido'"),
        "ajustes_sem_rota": count("me_ajustes", "WHERE status='pendente_configuracao_de_rota'"),
        "ajustes_erro": count("me_ajustes", "WHERE status='erro'"),
        "estornos_concluidos": count("me_estornos", "WHERE status='concluido'"),
    }
    conn.close()
    return resumo


# ─────────────────────────────────────────────────────────────────────────────
# Webhooks Bling (um endpoint por empresa — URLs distintas no Bling)
# ─────────────────────────────────────────────────────────────────────────────
#
#  Shinsei:  POST /multiempresa/webhook/bling/shinsei
#  AKG:      POST /multiempresa/webhook/bling/akg
#
# Configurar no Bling de cada conta:
#   Alias: shinsei-pricing   URL: https://shinsei-pricing.onrender.com/multiempresa/webhook/bling/<empresa>
#   Evento: Pedidos → Atualização (situação confirmada ou cancelada)
#
# Segurança: HMAC-SHA256 via header X-Bling-Signature-256 (mesmo secret das duas contas)
# Env var:   BLING_WEBHOOK_SECRET  (já existe no sistema)

_SITUACOES_CONFIRMADAS = {9, 12, 15}
_SITUACOES_CANCELADAS  = {11, 14, 76}


def _verificar_sig(raw: bytes, header: str) -> bool:
    secret = os.getenv("BLING_WEBHOOK_SECRET", "")
    if not secret or not header:
        return True  # sem secret configurado: aceita tudo (útil em dev)
    try:
        expected = "sha256=" + _hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return _hmac.compare_digest(expected, header)
    except Exception:
        return False


def _extrair_pedido_do_payload(body: dict) -> tuple[str | None, int | None, dict]:
    """
    Extrai (id_pedido, situacao_id, dados_pedido) do payload Bling.
    Suporta formato v3 (data.id / data.situacao) e legado (retorno.pedidos[]).
    """
    # Formato moderno Bling v3
    data = body.get("data") or {}
    if data.get("id"):
        sit = data.get("situacao") or {}
        sit_id = int(sit.get("id") or 0)
        return str(data["id"]), sit_id, data

    # Formato legado
    retorno = body.get("retorno") or {}
    pedidos = retorno.get("pedidos") or []
    if pedidos:
        ped = (pedidos[0].get("pedido") or pedidos[0])
        sit = ped.get("situacao") or {}
        sit_id = int(sit.get("id") or 0)
        return str(ped.get("id") or ""), sit_id, ped

    return None, None, {}


async def _handle_webhook(request: Request, background_tasks: BackgroundTasks,
                          empresa: str) -> dict:
    raw = await request.body()
    sig = request.headers.get("X-Bling-Signature-256", "")

    if not _verificar_sig(raw, sig):
        logger.warning("webhook [%s]: assinatura inválida", empresa)
        return {"ok": False, "erro": "assinatura_invalida"}

    try:
        body = await request.json() if not raw else __import__("json").loads(raw)
    except Exception:
        return {"ok": False, "erro": "json_invalido"}

    evento = (body.get("evento") or body.get("event") or "").lower()
    logger.info("webhook_bling [%s] evento=%s", empresa, evento)

    # Só processa eventos de pedido de venda
    if "pedido" not in evento and "venda" not in evento:
        return {"ok": True, "ignorado": True, "evento": evento}

    id_pedido, sit_id, dados_pedido = _extrair_pedido_do_payload(body)
    if not id_pedido:
        return {"ok": True, "ignorado": True, "motivo": "id_pedido_nao_encontrado"}

    init_db()

    if sit_id in _SITUACOES_CONFIRMADAS:
        # Se temos os dados do pedido no payload, processa direto
        # Se não (payload parcial), busca no Bling
        if not dados_pedido.get("itens"):
            try:
                dados_pedido = _fetch_pedido(empresa, id_pedido)
            except Exception as e:
                logger.error("webhook fetch pedido [%s/%s]: %s", empresa, id_pedido, e)
                return {"ok": False, "erro": str(e)}
        background_tasks.add_task(processar_pedido, empresa, dados_pedido)
        return {"ok": True, "acao": "processar_agendado", "pedido": id_pedido}

    elif sit_id in _SITUACOES_CANCELADAS:
        background_tasks.add_task(estornar_pedido, empresa, id_pedido)
        return {"ok": True, "acao": "estorno_agendado", "pedido": id_pedido}

    return {"ok": True, "ignorado": True, "situacao": sit_id}


@router.get("/multiempresa/webhook/bling/{empresa}")
def webhook_bling_verify(empresa: str):
    """Ping de verificação do Bling (GET) para manter o webhook ativo."""
    return {"ok": True, "empresa": empresa, "status": "ativo"}


@router.post("/multiempresa/webhook/bling/{empresa}")
async def webhook_bling(empresa: str, request: Request,
                        background_tasks: BackgroundTasks):
    if empresa not in (EMPRESA_SHINSEI, EMPRESA_AKG):
        return {"ok": False, "erro": f"empresa inválida: {empresa}"}
    return await _handle_webhook(request, background_tasks, empresa)


# ─────────────────────────────────────────────────────────────────────────────
# Painel HTML
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/multiempresa", response_class=HTMLResponse)
def painel_multiempresa():
    p = PAGES_DIR / "multiempresa_correcao.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<h2>Painel multiempresa — page não encontrada</h2>", 404)
