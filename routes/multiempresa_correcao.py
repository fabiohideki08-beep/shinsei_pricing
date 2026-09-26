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
_proc = processar_pedido

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
    import time
    from datetime import datetime

    DEP_SHINSEI = 14636070822
    DEP_AKG     = 14889056234
    base        = "https://api.bling.com.br/Api/v3"
    OBS         = f"Correcao bug multiempresa pos {data_corte} — AKG Geral -> Shinsei Geral"

    hdrs_s = _hdrs(EMPRESA_SHINSEI)
    hdrs_a = _hdrs(EMPRESA_AKG)

    def _bling_get(url, headers, params=None, retries=5):
        """GET com retry exponencial em 429."""
        for i in range(retries):
            r = _req.get(url, headers=headers, params=params, timeout=30)
            if r.status_code == 429:
                wait = 2 ** i  # 1s, 2s, 4s, 8s, 16s
                time.sleep(wait)
                continue
            return r
        return r

    # ── 1) Listar produtos Shinsei com saldo total negativo ────────────────────
    # /produtos retorna estoque.saldoVirtualTotal (total geral); depois confirmamos por depósito
    saldo_negativo: dict[str, dict] = {}   # sku → {produto_id, nome, saldo}
    pagina = 1
    while True:
        r = _bling_get(f"{base}/produtos", hdrs_s,
                       params={"pagina": pagina, "limite": 100, "situacao": "A",
                               "tipo": "P"})
        if not r.ok:
            return {"ok": False, "erro": f"Shinsei produtos HTTP {r.status_code}: {r.text[:300]}"}
        prods = r.json().get("data", [])
        if not prods:
            break
        time.sleep(0.35)
        for p in prods:
            est = p.get("estoque") or {}
            saldo_total = float(est.get("saldoVirtualTotal", 0) or 0)
            if saldo_total >= 0:
                continue
            # Confirmar saldo no depósito específico (kits tipo=P não têm registro por depósito)
            prod_id = p["id"]
            time.sleep(0.4)  # Bling rate limit: 3 req/s
            rs = _bling_get(f"{base}/estoques/saldos", hdrs_s,
                            params={"produto": prod_id, "deposito": DEP_SHINSEI})
            if rs.ok:
                saldos = rs.json().get("data", [])
                saldo_dep = float((saldos[0].get("saldoVirtualTotal", 0) if saldos else 0) or 0)
            else:
                # Kits ou produtos sem registro de depósito: usar saldo total como proxy
                saldo_dep = saldo_total
            if saldo_dep < 0:
                sku = p.get("codigo", "")
                if sku:
                    saldo_negativo[sku] = {
                        "produto_id_shinsei": prod_id,
                        "nome": p.get("nome", ""),
                        "saldo_shinsei": saldo_dep,
                    }
        pagina += 1

    if not saldo_negativo:
        return {"ok": True, "msg": "Nenhum saldo negativo no Shinsei Geral.", "total": 0}

    # ── 2) Pedidos de venda Shinsei confirmados após data_corte ───────────────
    # situacoes confirmadas: 9=Atendido, 12=Em andamento, 15=Em andamento
    SITUACOES_OK = {9, 12, 15}
    vendas_pos_corte: dict[str, int] = {}   # sku → qtd vendida após corte

    # Bling limita período a 366 dias — usar hoje+1 como dataFinal
    # Listing /pedidos/vendas NÃO retorna itens — precisa GET /pedidos/vendas/{id} por pedido
    import datetime as _dt
    data_final = (_dt.datetime.now() + _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    pagina = 1
    ids_confirmados: list[int] = []
    while True:
        r = _bling_get(f"{base}/pedidos/vendas", hdrs_s,
                       params={"pagina": pagina, "limite": 100,
                               "dataInicial": data_corte, "dataFinal": data_final})
        if not r.ok:
            break
        pedidos = r.json().get("data", [])
        if not pedidos:
            break
        time.sleep(0.35)
        for ped in pedidos:
            sit_id = (ped.get("situacao") or {}).get("id", 0)
            if int(sit_id or 0) in SITUACOES_OK:
                ids_confirmados.append(ped["id"])
        pagina += 1

    # Buscar detalhe de cada pedido confirmado para extrair itens
    for ped_id in ids_confirmados:
        time.sleep(0.4)
        rd = _bling_get(f"{base}/pedidos/vendas/{ped_id}", hdrs_s)
        if not rd.ok:
            continue
        ped_det = rd.json().get("data", {})
        for item in (ped_det.get("itens") or []):
            sku = item.get("codigo", "")
            qtd = float(item.get("quantidade", 0))
            if sku and qtd > 0:
                vendas_pos_corte[sku] = vendas_pos_corte.get(sku, 0) + int(qtd)

    def _get_componentes(prod_id, hdrs):
        """Retorna lista de {id, qtd_por_kit} dos componentes físicos. Vazio se produto simples."""
        time.sleep(0.4)
        rd = _bling_get(f"{base}/produtos/{prod_id}", hdrs)
        if not rd.ok:
            return []
        estrutura = rd.json().get("data", {}).get("estrutura", {}) or {}
        comps = estrutura.get("componentes", []) or []
        return [{"id": c["produto"]["id"], "qtd_por_kit": float(c.get("quantidade", 1))}
                for c in comps if c.get("produto", {}).get("id")]

    # ── 3) Identificar kits e propagar vendas para componentes físicos ─────────
    # Produtos virtuais (kits) não têm estoque próprio — o débito ocorre nos componentes.
    # O mesmo físico pode ser componente de centenas de kits; ajustar o kit expandido
    # duplicaria a correção se o físico também aparecer na lista de negativos.
    # Solução: propagar vendas dos kits para os IDs dos componentes, ajustar só físicos.
    _comp_cache: dict[int, list] = {}  # prod_id → [componentes]
    vendas_fisico_extra: dict[int, int] = {}  # prod_id_componente → qtd adicional via kits

    for sku, info in saldo_negativo.items():
        qtd_kit_vendida = vendas_pos_corte.get(sku, 0)
        if qtd_kit_vendida == 0:
            continue
        prod_id = info["produto_id_shinsei"]
        comps = _get_componentes(prod_id, hdrs_s)
        _comp_cache[prod_id] = comps
        if comps:
            # Kit virtual — propagar vendas para componentes físicos
            info["eh_kit"] = True
            for c in comps:
                qtd_comp = int(c["qtd_por_kit"] * qtd_kit_vendida)
                vendas_fisico_extra[c["id"]] = vendas_fisico_extra.get(c["id"], 0) + qtd_comp

    # ── 4) Calcular quantidade a transferir — apenas físicos ──────────────────
    # Kits virtuais são pulados; seus componentes físicos recebem as vendas propagadas
    prod_id_to_sku = {v["produto_id_shinsei"]: k for k, v in saldo_negativo.items()}
    transferencias: list[dict] = []
    for sku, info in saldo_negativo.items():
        if info.get("eh_kit"):
            continue  # kit virtual — ajuste vai nos componentes físicos
        prod_id = info["produto_id_shinsei"]
        vendas_diretas = vendas_pos_corte.get(sku, 0)
        vendas_via_kit = vendas_fisico_extra.get(prod_id, 0)
        vendido = vendas_diretas + vendas_via_kit
        if vendido == 0:
            continue  # negativo histórico — pula
        qtd = min(vendido, abs(info["saldo_shinsei"]))
        if qtd > 0:
            transferencias.append({
                "sku": sku,
                "nome": info["nome"],
                "produto_id_shinsei": prod_id,
                "saldo_shinsei": info["saldo_shinsei"],
                "vendido_direto": vendas_diretas,
                "vendido_via_kit": vendas_via_kit,
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

    # ── 5) Buscar IDs AKG para os SKUs afetados ───────────────────────────────
    # Renovar token AKG (pode ter expirado durante saldo checks longos)
    hdrs_a = _hdrs(EMPRESA_AKG)
    skus_afetados = {t["sku"] for t in transferencias}
    sku_to_akg_id: dict[str, int] = {}
    for sku in skus_afetados:
        time.sleep(0.4)
        r = _bling_get(f"{base}/produtos", hdrs_a,
                       params={"codigo": sku, "situacao": "A"})
        if not r.ok:
            continue
        prods = r.json().get("data", [])
        if prods:
            sku_to_akg_id[sku] = prods[0]["id"]

    for t in transferencias:
        t["produto_id_akg"] = sku_to_akg_id.get(t["sku"])

    def _ajustar_estoque(prod_id, deposito, operacao, qtd, hdrs):
        """POST /estoques para produto físico. Retorna (ok, err_msg)."""
        time.sleep(0.4)
        try:
            r = _req.post(f"{base}/estoques", headers=hdrs, json={
                "produto": {"id": prod_id}, "deposito": {"id": deposito},
                "tipoOperacao": operacao, "quantidade": int(qtd), "observacoes": OBS,
            }, timeout=30)
            return r.ok, ("" if r.ok else f"HTTP {r.status_code}: {r.text[:150]}")
        except Exception as exc:
            return False, str(exc)[:150]

    # ── 6) Construir lista de ajustes — apenas físicos ────────────────────────
    # Todos os produtos em transferencias já são físicos (kits foram pulados na etapa 4)
    ajustes: list[dict] = []
    for t in transferencias:
        ajustes.append({
            "sku": t["sku"], "nome": t["nome"],
            "prod_id_s": t["produto_id_shinsei"], "prod_id_a": t["produto_id_akg"],
            "qtd": t["qtd_transferir"], "tipo": "fisico",
        })

    kits_ignorados = sum(1 for info in saldo_negativo.values() if info.get("eh_kit"))
    if dry_run:
        sem_akg = [t for t in transferencias if not t["produto_id_akg"]]
        return {
            "ok": True,
            "dry_run": True,
            "data_corte": data_corte,
            "negativos_total": len(saldo_negativo),
            "negativos_kits_ignorados": kits_ignorados,
            "negativos_historicos_ignorados": len(saldo_negativo) - kits_ignorados - len(transferencias),
            "a_transferir": len(transferencias),
            "sem_id_akg": len(sem_akg),
            "itens": transferencias,
        }

    # ── 7) Aplicar movimentações ───────────────────────────────────────────────
    corrigidos, erros = [], []
    for aj in ajustes:
        entry = {"sku": aj["sku"], "nome": aj["nome"], "qtd": aj["qtd"]}

        # Entrada Shinsei Geral
        ok_e, err_e = _ajustar_estoque(aj["prod_id_s"], DEP_SHINSEI, "E", aj["qtd"], hdrs_s)
        if not ok_e:
            erros.append({**entry, "etapa": "entrada_shinsei", "erro": err_e})
            continue

        # Saída AKG Geral
        saida_ok, saida_err = False, "produto não encontrado na AKG"
        if aj["prod_id_a"]:
            saida_ok, saida_err = _ajustar_estoque(aj["prod_id_a"], DEP_AKG, "S", aj["qtd"], hdrs_a)

        corrigidos.append({**entry, "entrada_shinsei": True,
                           "saida_akg": saida_ok, "saida_akg_err": saida_err})

    return {
        "ok": True,
        "dry_run": False,
        "data_corte": data_corte,
        "total_fisicos": len(transferencias),
        "corrigidos": len(corrigidos),
        "erros": len(erros),
        "detalhes_erro": erros[:10],
        "detalhes": corrigidos[:30],
    }


@router.post("/multiempresa/transferir-shinsei-akg")
def transferir_shinsei_para_akg(dry_run: bool = True, data_corte: str = "2026-09-04"):
    """
    Direção inversa: vendas AKG que debitaram do AKG Geral indevidamente,
    quando o estoque real estava no Shinsei Geral.

    ENTRADA AKG Geral + SAÍDA Shinsei Geral para os físicos negativos na AKG
    com vendas após data_corte.
    """
    import time
    import datetime as _dt

    DEP_SHINSEI = 14636070822
    DEP_AKG     = 14889056234
    base        = "https://api.bling.com.br/Api/v3"
    OBS         = f"Correcao bug multiempresa pos {data_corte} — Shinsei Geral -> AKG Geral"

    hdrs_s = _hdrs(EMPRESA_SHINSEI)
    hdrs_a = _hdrs(EMPRESA_AKG)

    def _bling_get(url, headers, params=None, retries=5):
        for i in range(retries):
            r = _req.get(url, headers=headers, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2 ** i)
                continue
            return r
        return r

    def _get_componentes(prod_id, hdrs):
        time.sleep(0.4)
        rd = _bling_get(f"{base}/produtos/{prod_id}", hdrs)
        if not rd.ok:
            return []
        estrutura = rd.json().get("data", {}).get("estrutura", {}) or {}
        comps = estrutura.get("componentes", []) or []
        return [{"id": c["produto"]["id"], "qtd_por_kit": float(c.get("quantidade", 1))}
                for c in comps if c.get("produto", {}).get("id")]

    def _ajustar_estoque(prod_id, deposito, operacao, qtd, hdrs):
        time.sleep(0.4)
        try:
            r = _req.post(f"{base}/estoques", headers=hdrs, json={
                "produto": {"id": prod_id}, "deposito": {"id": deposito},
                "tipoOperacao": operacao, "quantidade": int(qtd), "observacoes": OBS,
            }, timeout=30)
            return r.ok, ("" if r.ok else f"HTTP {r.status_code}: {r.text[:150]}")
        except Exception as exc:
            return False, str(exc)[:150]

    # 1) Negativos na AKG
    saldo_negativo: dict[str, dict] = {}
    pagina = 1
    while True:
        r = _bling_get(f"{base}/produtos", hdrs_a,
                       params={"pagina": pagina, "limite": 100, "situacao": "A", "tipo": "P"})
        if not r.ok:
            return {"ok": False, "erro": f"AKG produtos HTTP {r.status_code}: {r.text[:300]}"}
        prods = r.json().get("data", [])
        if not prods:
            break
        time.sleep(0.35)
        for p in prods:
            est = p.get("estoque") or {}
            saldo_total = float(est.get("saldoVirtualTotal", 0) or 0)
            if saldo_total >= 0:
                continue
            prod_id = p["id"]
            time.sleep(0.4)
            rs = _bling_get(f"{base}/estoques/saldos", hdrs_a,
                            params={"produto": prod_id, "deposito": DEP_AKG})
            if rs.ok:
                saldos = rs.json().get("data", [])
                saldo_dep = float((saldos[0].get("saldoVirtualTotal", 0) if saldos else 0) or 0)
            else:
                saldo_dep = saldo_total
            if saldo_dep < 0:
                sku = p.get("codigo", "")
                if sku:
                    saldo_negativo[sku] = {
                        "produto_id_akg": prod_id,
                        "nome": p.get("nome", ""),
                        "saldo_akg": saldo_dep,
                    }
        pagina += 1

    if not saldo_negativo:
        return {"ok": True, "msg": "Nenhum saldo negativo na AKG Geral.", "total": 0}

    # 2) Pedidos AKG confirmados após data_corte
    SITUACOES_OK = {9, 12, 15}
    vendas_pos_corte: dict[str, int] = {}
    data_final = (_dt.datetime.now() + _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    pagina = 1
    ids_confirmados: list[int] = []
    while True:
        r = _bling_get(f"{base}/pedidos/vendas", hdrs_a,
                       params={"pagina": pagina, "limite": 100,
                               "dataInicial": data_corte, "dataFinal": data_final})
        if not r.ok:
            break
        pedidos = r.json().get("data", [])
        if not pedidos:
            break
        time.sleep(0.35)
        for ped in pedidos:
            sit_id = (ped.get("situacao") or {}).get("id", 0)
            if int(sit_id or 0) in SITUACOES_OK:
                ids_confirmados.append(ped["id"])
        pagina += 1

    for ped_id in ids_confirmados:
        time.sleep(0.4)
        rd = _bling_get(f"{base}/pedidos/vendas/{ped_id}", hdrs_a)
        if not rd.ok:
            continue
        for item in (rd.json().get("data", {}).get("itens") or []):
            sku = item.get("codigo", "")
            qtd = float(item.get("quantidade", 0))
            if sku and qtd > 0:
                vendas_pos_corte[sku] = vendas_pos_corte.get(sku, 0) + int(qtd)

    # 3) Propagar vendas de kits para componentes físicos
    _comp_cache: dict[int, list] = {}
    vendas_fisico_extra: dict[int, int] = {}
    for sku, info in saldo_negativo.items():
        qtd_kit_vendida = vendas_pos_corte.get(sku, 0)
        if qtd_kit_vendida == 0:
            continue
        prod_id = info["produto_id_akg"]
        comps = _get_componentes(prod_id, hdrs_a)
        _comp_cache[prod_id] = comps
        if comps:
            info["eh_kit"] = True
            for c in comps:
                qtd_comp = int(c["qtd_por_kit"] * qtd_kit_vendida)
                vendas_fisico_extra[c["id"]] = vendas_fisico_extra.get(c["id"], 0) + qtd_comp

    # 4) Transferências — apenas físicos AKG
    transferencias: list[dict] = []
    for sku, info in saldo_negativo.items():
        if info.get("eh_kit"):
            continue
        prod_id = info["produto_id_akg"]
        vendas_diretas = vendas_pos_corte.get(sku, 0)
        vendas_via_kit = vendas_fisico_extra.get(prod_id, 0)
        vendido = vendas_diretas + vendas_via_kit
        if vendido == 0:
            continue
        qtd = min(vendido, abs(info["saldo_akg"]))
        if qtd > 0:
            transferencias.append({
                "sku": sku, "nome": info["nome"],
                "produto_id_akg": prod_id,
                "saldo_akg": info["saldo_akg"],
                "vendido_direto": vendas_diretas,
                "vendido_via_kit": vendas_via_kit,
                "vendido_pos_corte": vendido,
                "qtd_transferir": qtd,
                "produto_id_shinsei": None,
            })

    if not transferencias:
        kits_ign = sum(1 for i in saldo_negativo.values() if i.get("eh_kit"))
        return {
            "ok": True,
            "msg": "Nenhum SKU negativo AKG com vendas após data_corte.",
            "negativos_total": len(saldo_negativo),
            "negativos_kits_ignorados": kits_ign,
            "negativos_historicos": len(saldo_negativo) - kits_ign,
        }

    # 5) Buscar IDs Shinsei
    hdrs_s = _hdrs(EMPRESA_SHINSEI)
    for t in transferencias:
        time.sleep(0.4)
        r = _bling_get(f"{base}/produtos", hdrs_s,
                       params={"codigo": t["sku"], "situacao": "A"})
        if not r.ok:
            continue
        prods = r.json().get("data", [])
        if prods:
            t["produto_id_shinsei"] = prods[0]["id"]

    kits_ignorados = sum(1 for i in saldo_negativo.values() if i.get("eh_kit"))
    if dry_run:
        sem_shinsei = [t for t in transferencias if not t["produto_id_shinsei"]]
        return {
            "ok": True, "dry_run": True, "data_corte": data_corte,
            "negativos_total": len(saldo_negativo),
            "negativos_kits_ignorados": kits_ignorados,
            "negativos_historicos_ignorados": len(saldo_negativo) - kits_ignorados - len(transferencias),
            "a_transferir": len(transferencias),
            "sem_id_shinsei": len(sem_shinsei),
            "itens": transferencias,
        }

    # 6) Aplicar movimentações
    corrigidos, erros = [], []
    for t in transferencias:
        entry = {"sku": t["sku"], "nome": t["nome"], "qtd": t["qtd_transferir"]}
        ok_e, err_e = _ajustar_estoque(t["produto_id_akg"], DEP_AKG, "E", t["qtd_transferir"], hdrs_a)
        if not ok_e:
            erros.append({**entry, "etapa": "entrada_akg", "erro": err_e})
            continue
        saida_ok, saida_err = False, "produto não encontrado na Shinsei"
        if t["produto_id_shinsei"]:
            saida_ok, saida_err = _ajustar_estoque(t["produto_id_shinsei"], DEP_SHINSEI, "S",
                                                    t["qtd_transferir"], hdrs_s)
        corrigidos.append({**entry, "entrada_akg": True,
                           "saida_shinsei": saida_ok, "saida_shinsei_err": saida_err})

    return {
        "ok": True, "dry_run": False, "data_corte": data_corte,
        "total_fisicos": len(transferencias),
        "corrigidos": len(corrigidos), "erros": len(erros),
        "detalhes_erro": erros[:10], "detalhes": corrigidos[:30],
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
        "ajustes_sem_rota": count("me_ajustes", "WHERE status IN ('pendente_configuracao_de_rota','sem_estoque_akg')"),
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
# Reprocessar vendas travadas (total_itens=0 / status=concluido sem ajustes)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/multiempresa/normalizar-status")
def normalizar_status():
    """
    Atualiza me_vendas para 'sem_ajuste' quando todos os ajustes são 'sem_estoque_akg'
    (produto de estoque próprio Shinsei — não há ajuste AKG a fazer).
    """
    import sqlite3 as _sq
    conn = _sq.connect(str(DB_PATH))
    conn.row_factory = _sq.Row
    # 1. Reclassifica ajustes "produto nao encontrado" → sem_estoque_akg
    r1 = conn.execute(
        """UPDATE me_ajustes
           SET status = 'sem_estoque_akg'
           WHERE status = 'erro'
             AND erro_detalhe LIKE '%produto nao encontrado%'"""
    )
    ajustes_reclassificados = r1.rowcount

    # 2. Atualiza vendas onde agora todos os ajustes são sem_estoque_akg
    r2 = conn.execute(
        """UPDATE me_vendas
           SET status = 'sem_ajuste', atualizado_em = datetime('now')
           WHERE status IN ('erro', 'concluido')
             AND id IN (
               SELECT v.id FROM me_vendas v
               WHERE NOT EXISTS (
                 SELECT 1 FROM me_ajustes a
                 WHERE a.id_venda_ctrl = v.id
                   AND a.status NOT IN ('sem_estoque_akg')
               )
               AND EXISTS (
                 SELECT 1 FROM me_ajustes a
                 WHERE a.id_venda_ctrl = v.id
               )
             )"""
    )
    conn.commit()
    alterados = r2.rowcount
    conn.close()
    return {"ok": True, "ajustes_reclassificados": ajustes_reclassificados,
            "vendas_normalizadas": alterados}


@router.post("/multiempresa/reprocessar-zerados")
def reprocessar_zerados(background_tasks: BackgroundTasks, empresa: str = EMPRESA_SHINSEI,
                        limite: int = 100):
    """
    Localiza vendas com total_itens=0 e status=concluido (travadas pela idempotência)
    e força reprocessamento buscando os pedidos novamente no Bling.
    Útil para desbloquear o estado após o bug do job que passava itens=[].
    """
    import sqlite3 as _sq
    conn = _sq.connect(str(DB_PATH))
    conn.row_factory = _sq.Row
    # Vendas bloqueadas: concluído sem itens procesados e sem ajustes concluídos
    zerados = conn.execute(
        """SELECT DISTINCT v.id_pedido_bling, v.empresa_vendedora
           FROM me_vendas v
           WHERE v.empresa_vendedora = ?
             AND v.status IN ('concluido', 'erro', 'sem_ajuste')
             AND NOT EXISTS (
               SELECT 1 FROM me_ajustes a
               WHERE a.id_pedido_bling = v.id_pedido_bling
                 AND a.empresa_vendedora = v.empresa_vendedora
                 AND a.status IN ('concluido', 'sem_estoque_akg')
             )
           LIMIT ?""",
        (empresa, limite)
    ).fetchall()
    conn.close()

    if not zerados:
        return {"ok": True, "mensagem": "Nenhuma venda zerada encontrada", "total": 0}

    agendados = []
    for row in zerados:
        id_pedido = row["id_pedido_bling"]
        emp = row["empresa_vendedora"]
        try:
            pedido_det = _fetch_pedido(emp, id_pedido)
            background_tasks.add_task(processar_pedido, emp, pedido_det)
            agendados.append(id_pedido)
        except Exception as e:
            logger.error("reprocessar_zerados: pedido %s erro: %s", id_pedido, e)

    return {
        "ok": True,
        "total_zerados": len(zerados),
        "total_agendados": len(agendados),
        "pedidos": agendados,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Diagnóstico: SKUs sem_estoque_akg — verificar se são kits com componentes na AKG
# ─────────────────────────────────────────────────────────────────────────────

BLING_API = "https://api.bling.com.br/Api/v3"


@router.post("/multiempresa/reprocessar-sem-estoque-akg")
def reprocessar_sem_estoque_akg(background_tasks: BackgroundTasks,
                                empresa: str = EMPRESA_SHINSEI,
                                limite: int = 200):
    """
    Reseta ajustes sem_estoque_akg para pendente e reprocessa vendas.
    Útil quando o token AKG estava quebrado durante o processamento original.
    """
    import traceback as _tb
    try:
        conn = _db()
        vendas = conn.execute(
            """SELECT DISTINCT v.id, v.id_pedido_bling, v.empresa_vendedora
               FROM me_vendas v
               WHERE v.empresa_vendedora = ?
                 AND EXISTS (
                   SELECT 1 FROM me_ajustes a
                   WHERE a.id_venda_ctrl = v.id AND a.status = 'sem_estoque_akg'
                 )
                 AND NOT EXISTS (
                   SELECT 1 FROM me_ajustes a
                   WHERE a.id_venda_ctrl = v.id AND a.status = 'concluido'
                 )
               LIMIT ?""",
            (empresa, limite)
        ).fetchall()

        if not vendas:
            conn.close()
            return {"ok": True, "mensagem": "Nenhuma venda elegível", "total": 0}

        ids_venda = [v["id"] for v in vendas]
        pedidos = [(v["id_pedido_bling"], v["empresa_vendedora"]) for v in vendas]
        ph = ",".join("?" * len(ids_venda))

        conn.execute(
            f"UPDATE me_ajustes SET status='pendente' "
            f"WHERE id_venda_ctrl IN ({ph}) AND status='sem_estoque_akg'",
            ids_venda
        )
        conn.execute(
            f"UPDATE me_vendas SET status='pendente', atualizado_em=datetime('now') "
            f"WHERE id IN ({ph})",
            ids_venda
        )
        conn.commit()
        conn.close()

        for id_pedido, emp in pedidos:
            background_tasks.add_task(processar_pedido, emp, id_pedido)

        return {
            "ok": True,
            "vendas_resetadas": len(vendas),
            "pedidos_agendados": [p[0] for p in pedidos],
        }
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}\n{_tb.format_exc()}")


@router.get("/multiempresa/diagnostico-skus")
def diagnostico_skus_sem_estoque_akg(limite: int = 200):
    """
    Para cada SKU classificado como sem_estoque_akg, busca no Bling Shinsei:
    - Nome do produto
    - Se é kit (tipoEstoque=V) → expande componentes
    - Verifica se componentes existem na AKG
    Retorna relatório completo para análise.
    """
    conn = _db()
    rows = conn.execute(
        """SELECT DISTINCT sku FROM me_ajustes
           WHERE status = 'sem_estoque_akg'
             AND empresa_vendedora = 'shinsei'
           LIMIT ?""",
        (limite,)
    ).fetchall()
    conn.close()

    skus = [r["sku"] for r in rows]
    if not skus:
        return {"total": 0, "skus": []}

    hdrs_sh = _hdrs("shinsei")
    hdrs_akg = _hdrs("akg")

    resultado = []

    for sku in skus:
        entry = {"sku": sku, "nome": None, "tipo_estoque": None,
                 "componentes": [], "componentes_na_akg": [], "status": None}
        try:
            r = _req.get(f"{BLING_API}/produtos",
                         params={"codigo": sku, "limite": 5},
                         headers=hdrs_sh, timeout=10)
            if r.status_code != 200:
                entry["status"] = f"erro_shinsei_{r.status_code}"
                resultado.append(entry)
                continue

            data = r.json().get("data", [])
            prod = next((p for p in data if p.get("codigo") == sku), None)
            if not prod:
                entry["status"] = "nao_encontrado_shinsei"
                resultado.append(entry)
                continue

            entry["nome"] = prod.get("nome")
            entry["tipo_estoque"] = prod.get("estoque", {}).get("tipoEstoque") or prod.get("estrutura", {}).get("tipoEstoque")

            # Verifica se é kit virtual
            estrutura = prod.get("estrutura", {})
            tipo = estrutura.get("tipoEstoque") or prod.get("estoque", {}).get("tipoEstoque", "")
            componentes_raw = estrutura.get("componentes") or prod.get("componentes", [])

            if tipo == "V" and componentes_raw:
                # Busca detalhes do produto completo para obter componentes
                prod_id = prod.get("id")
                r2 = _req.get(f"{BLING_API}/produtos/{prod_id}", headers=hdrs_sh, timeout=10)
                if r2.status_code == 200:
                    full = r2.json().get("data", {})
                    componentes_raw = full.get("estrutura", {}).get("componentes", [])

                for comp in componentes_raw:
                    comp_sku = comp.get("produto", {}).get("codigo") or comp.get("codigo")
                    comp_nome = comp.get("produto", {}).get("nome") or comp.get("descricao")
                    qtd = comp.get("quantidade", 1)

                    comp_entry = {"sku": comp_sku, "nome": comp_nome, "qtd": qtd, "na_akg": False, "id_akg": None}

                    if comp_sku:
                        ra = _req.get(f"{BLING_API}/produtos",
                                      params={"codigo": comp_sku, "limite": 5},
                                      headers=hdrs_akg, timeout=10)
                        if ra.status_code == 200:
                            da = ra.json().get("data", [])
                            match = next((p for p in da if p.get("codigo") == comp_sku), None)
                            if match:
                                comp_entry["na_akg"] = True
                                comp_entry["id_akg"] = match.get("id")
                                entry["componentes_na_akg"].append(comp_sku)

                    entry["componentes"].append(comp_entry)

                entry["status"] = "kit_componentes_verificados"
            else:
                # Produto simples — confirma que não existe na AKG
                ra = _req.get(f"{BLING_API}/produtos",
                              params={"codigo": sku, "limite": 5},
                              headers=hdrs_akg, timeout=10)
                if ra.status_code == 200:
                    da = ra.json().get("data", [])
                    match = next((p for p in da if p.get("codigo") == sku), None)
                    entry["status"] = "existe_na_akg_agora" if match else "nao_existe_na_akg_confirmado"
                else:
                    entry["status"] = f"erro_akg_{ra.status_code}"

        except Exception as e:
            entry["status"] = f"excecao_{e}"

        resultado.append(entry)

    kits_com_comps_na_akg = [e for e in resultado if e.get("componentes_na_akg")]
    simples_confirmados = [e for e in resultado if e["status"] == "nao_existe_na_akg_confirmado"]
    existem_akg_agora = [e for e in resultado if e["status"] == "existe_na_akg_agora"]

    return {
        "total_skus": len(skus),
        "kits_com_componentes_na_akg": len(kits_com_comps_na_akg),
        "simples_confirmados_sem_akg": len(simples_confirmados),
        "existem_na_akg_agora": len(existem_akg_agora),
        "kits_com_componentes_na_akg_detalhes": kits_com_comps_na_akg,
        "existem_na_akg_agora_detalhes": existem_akg_agora,
        "todos": resultado,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Painel HTML
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/multiempresa", response_class=HTMLResponse)
def painel_multiempresa():
    p = PAGES_DIR / "multiempresa_correcao.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<h2>Painel multiempresa — page não encontrada</h2>", 404)
