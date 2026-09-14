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

@router.get("/multiempresa", response_class=HTMLResponse)
def get_page():
    return HTMLResponse((PAGES_DIR / "multiempresa_movimentacoes.html").read_text(encoding="utf-8"))


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


@router.post("/multiempresa/transferir-akg-shinsei")
def transferir_akg_para_shinsei(dry_run: bool = True, data_corte: str = "2026-09-04"):
    """
    Corrige o bug multiempresa transferindo estoque AKG Geral → Shinsei Geral.

    Apenas as quantidades vendidas na Shinsei APÓS data_corte (padrão: 04/09/2026,
    data da última nota da General Corporate na AKG). Não cobre negativos históricos.

    Para cada SKU afetado:
      qtd_transferir = min(qty_vendida_após_corte, abs(saldo_negativo_shinsei))
      1. ENTRADA no Shinsei Geral (zera o negativo causado pela venda)
      2. SAÍDA no AKG Geral (debita de onde o estoque realmente saiu)

    dry_run=true (padrão): apenas lista, não altera.
    """
    import re
    from datetime import datetime

    DEP_SHINSEI = 14636070822
    DEP_AKG     = 14889056234
    base        = "https://api.bling.com.br/Api/v3"
    OBS         = f"Correcao bug multiempresa pos {data_corte} — AKG Geral -> Shinsei Geral"

    hdrs_s = _hdrs(EMPRESA_SHINSEI)
    hdrs_a = _hdrs(EMPRESA_AKG)

    # ── 1) Saldos negativos atuais no Shinsei Geral ────────────────────────────
    saldo_negativo: dict[str, dict] = {}   # sku → {produto_id, nome, saldo}
    pagina = 1
    while True:
        r = _req.get(f"{base}/estoques/saldos", headers=hdrs_s,
                     params={"pagina": pagina, "limite": 100, "deposito": DEP_SHINSEI},
                     timeout=30)
        if not r.ok:
            return {"ok": False, "erro": f"Shinsei saldos HTTP {r.status_code}: {r.text[:300]}"}
        itens = r.json().get("data", [])
        if not itens:
            break
        for it in itens:
            saldo = it.get("saldoVirtualTotal", 0)
            if saldo < 0:
                sku = it.get("produto", {}).get("codigo", "")
                if sku:
                    saldo_negativo[sku] = {
                        "produto_id_shinsei": it["produto"]["id"],
                        "nome": it["produto"].get("nome", ""),
                        "saldo_shinsei": saldo,
                    }
        pagina += 1

    if not saldo_negativo:
        return {"ok": True, "msg": "Nenhum saldo negativo no Shinsei Geral.", "total": 0}

    # ── 2) Pedidos de venda Shinsei confirmados após data_corte ───────────────
    # situacoes confirmadas: 9=Atendido, 12=Em andamento, 15=Em andamento
    SITUACOES_OK = {9, 12, 15}
    vendas_pos_corte: dict[str, int] = {}   # sku → qtd vendida após corte

    pagina = 1
    while True:
        r = _req.get(f"{base}/pedidos/vendas", headers=hdrs_s,
                     params={"pagina": pagina, "limite": 100,
                             "dataInicial": data_corte, "dataFinal": "2099-12-31"},
                     timeout=30)
        if not r.ok:
            break
        pedidos = r.json().get("data", [])
        if not pedidos:
            break
        for ped in pedidos:
            sit_id = (ped.get("situacao") or {}).get("id", 0)
            if int(sit_id or 0) not in SITUACOES_OK:
                continue
            for item in (ped.get("itens") or []):
                sku = item.get("codigo", "")
                qtd = float(item.get("quantidade", 0))
                if sku and qtd > 0:
                    vendas_pos_corte[sku] = vendas_pos_corte.get(sku, 0) + int(qtd)
        pagina += 1

    # ── 3) Calcular quantidade a transferir por SKU ────────────────────────────
    transferencias: list[dict] = []
    for sku, info in saldo_negativo.items():
        vendido = vendas_pos_corte.get(sku, 0)
        if vendido == 0:
            continue  # negativo histórico — pula
        qtd = min(vendido, abs(info["saldo_shinsei"]))
        if qtd > 0:
            transferencias.append({
                "sku": sku,
                "nome": info["nome"],
                "produto_id_shinsei": info["produto_id_shinsei"],
                "saldo_shinsei": info["saldo_shinsei"],
                "vendido_pos_corte": vendido,
                "qtd_transferir": qtd,
                "produto_id_akg": None,
            })

    if not transferencias:
        return {
            "ok": True,
            "msg": "Nenhum SKU negativo com vendas após data_corte encontrado.",
            "negativos_total": len(saldo_negativo),
            "negativos_historicos": len(saldo_negativo),
        }

    # ── 4) Buscar IDs AKG para os SKUs afetados ───────────────────────────────
    skus_afetados = {t["sku"] for t in transferencias}
    sku_to_akg_id: dict[str, int] = {}
    pagina = 1
    while True:
        r = _req.get(f"{base}/produtos", headers=hdrs_a,
                     params={"pagina": pagina, "limite": 100, "situacao": "A"},
                     timeout=30)
        if not r.ok:
            break
        prods = r.json().get("data", [])
        if not prods:
            break
        for p in prods:
            cod = p.get("codigo", "")
            if cod in skus_afetados:
                sku_to_akg_id[cod] = p["id"]
        if len(sku_to_akg_id) == len(skus_afetados):
            break  # já encontrou todos
        pagina += 1

    for t in transferencias:
        t["produto_id_akg"] = sku_to_akg_id.get(t["sku"])

    if dry_run:
        sem_akg = [t for t in transferencias if not t["produto_id_akg"]]
        return {
            "ok": True,
            "dry_run": True,
            "data_corte": data_corte,
            "negativos_total": len(saldo_negativo),
            "negativos_historicos_ignorados": len(saldo_negativo) - len(transferencias),
            "a_transferir": len(transferencias),
            "sem_id_akg": len(sem_akg),
            "itens": transferencias,
        }

    # ── 5) Aplicar movimentações ───────────────────────────────────────────────
    corrigidos, erros = [], []
    for t in transferencias:
        pid_s = t["produto_id_shinsei"]
        pid_a = t["produto_id_akg"]
        qtd   = t["qtd_transferir"]
        entry = {"sku": t["sku"], "nome": t["nome"], "qtd": qtd}

        # Entrada Shinsei Geral
        r_e = _req.post(f"{base}/estoques", headers=hdrs_s, json={
            "produto": {"id": pid_s}, "deposito": {"id": DEP_SHINSEI},
            "operacao": "E", "quantidade": qtd, "observacoes": OBS,
        }, timeout=20)
        if not r_e.ok:
            erros.append({**entry, "etapa": "entrada_shinsei",
                          "erro": f"HTTP {r_e.status_code}: {r_e.text[:150]}"})
            continue

        # Saída AKG Geral
        saida_ok, saida_err = False, "SKU não encontrado na AKG"
        if pid_a:
            r_s = _req.post(f"{base}/estoques", headers=hdrs_a, json={
                "produto": {"id": pid_a}, "deposito": {"id": DEP_AKG},
                "operacao": "S", "quantidade": qtd, "observacoes": OBS,
            }, timeout=20)
            saida_ok  = r_s.ok
            saida_err = "" if saida_ok else f"HTTP {r_s.status_code}: {r_s.text[:150]}"

        corrigidos.append({**entry, "entrada_shinsei": True,
                           "saida_akg": saida_ok, "saida_akg_err": saida_err})

    return {
        "ok": True,
        "dry_run": False,
        "data_corte": data_corte,
        "total_transferencias": len(transferencias),
        "corrigidos": len(corrigidos),
        "erros": len(erros),
        "detalhes_erro": erros[:10],
        "detalhes": corrigidos[:30],
    }


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
