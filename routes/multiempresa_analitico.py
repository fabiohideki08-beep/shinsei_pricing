"""
Multiempresa — Analítico de Distribuição de Estoque

Analisa se um SKU vende mais pela Shinsei ou pela AKG, e recomenda:
  TRANSFERIR_PARA_SHINSEI   → ≥90% das vendas via Shinsei; mover tudo AKG→Shinsei
  TRANSFERIR_PARA_AKG       → ≥90% das vendas via AKG; mover tudo Shinsei→AKG
  REDISTRIBUIR_PARA_SHINSEI → 70-90% via Shinsei; ajustar proporção
  REDISTRIBUIR_PARA_AKG     → 70-90% via AKG; ajustar proporção
  PROPORCIONAL              → distribuição equilibrada, manter proporção de vendas
  SEM_HISTORICO             → SKU sem vendas no período — analise manual

Fonte de dados de vendas: me_ajustes (status=concluido) × me_vendas (data_venda)
Fonte de estoque atual:   GET /estoques/saldos Bling (por depósito)
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/multiempresa", tags=["multiempresa-analitico"])

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH  = BASE_DIR / "data" / "shinsei.db"

BLING_BASE        = "https://api.bling.com.br/Api/v3"
DEP_SHINSEI_GERAL = 14636070822
DEP_AKG_GERAL     = 14889056234

# Thresholds de decisão
LIMIAR_TRANSFERENCIA = 0.90   # ≥90% → transferir tudo
LIMIAR_REDISTRIBUICAO = 0.70  # 70-89% → redistribuir


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _hdrs(empresa: str) -> dict:
    import os
    if empresa == "shinsei":
        tok = os.getenv("BLING_ACCESS_TOKEN", "")
    else:
        tok = os.getenv("BLING_AKG_ACCESS_TOKEN", "")
    return {
        "Authorization": f"Bearer {tok}",
        "Accept": "application/json",
        "enable-jwt": "1",
    }


def _saldo_deposito(empresa: str, id_produto: int, dep_id: int) -> float:
    """Retorna o saldo virtual no depósito especificado."""
    try:
        r = httpx.get(
            f"{BLING_BASE}/estoques/saldos",
            params={"produto": id_produto, "deposito": dep_id},
            headers=_hdrs(empresa),
            timeout=10,
        )
        if not r.is_success:
            return 0.0
        for s in r.json().get("data", []):
            if int(s.get("deposito", {}).get("id", 0)) == dep_id:
                return float(s.get("saldoFisico", 0) or 0)
    except Exception:
        pass
    return 0.0


def _buscar_id_produto(empresa: str, sku: str) -> int | None:
    try:
        r = httpx.get(
            f"{BLING_BASE}/produtos",
            params={"codigo": sku, "situacao": "A"},
            headers=_hdrs(empresa),
            timeout=10,
        )
        if not r.is_success:
            return None
        items = r.json().get("data", [])
        if items:
            return int(items[0]["id"])
    except Exception:
        pass
    return None


@dataclass
class VendasSku:
    sku: str
    nome: str
    total_qtd: float
    qtd_shinsei: float
    qtd_akg: float
    num_pedidos_shinsei: int
    num_pedidos_akg: int
    pct_shinsei: float
    pct_akg: float
    canal_dominante: str   # "shinsei" | "akg" | "equilibrado"
    recomendacao: str
    descricao: str
    # estoque (preenchido apenas com include_estoque=true)
    estoque_shinsei: Optional[float] = None
    estoque_akg: Optional[float] = None
    estoque_total: Optional[float] = None
    estoque_desalinhado: Optional[bool] = None
    qtd_sugerida_shinsei: Optional[float] = None
    qtd_sugerida_akg: Optional[float] = None
    qtd_a_transferir: Optional[float] = None
    direcao_transferencia: Optional[str] = None  # "shinsei→akg" | "akg→shinsei"


def _recomendar(pct_shinsei: float, pct_akg: float) -> tuple[str, str]:
    if pct_shinsei >= LIMIAR_TRANSFERENCIA:
        return (
            "TRANSFERIR_PARA_SHINSEI",
            f"{pct_shinsei:.0%} das vendas via Shinsei — transferir todo o estoque AKG para Shinsei",
        )
    if pct_akg >= LIMIAR_TRANSFERENCIA:
        return (
            "TRANSFERIR_PARA_AKG",
            f"{pct_akg:.0%} das vendas via AKG — transferir todo o estoque Shinsei para AKG",
        )
    if pct_shinsei >= LIMIAR_REDISTRIBUICAO:
        return (
            "REDISTRIBUIR_PARA_SHINSEI",
            f"{pct_shinsei:.0%} das vendas via Shinsei — redistribuir: concentrar {pct_shinsei:.0%} do estoque em Shinsei",
        )
    if pct_akg >= LIMIAR_REDISTRIBUICAO:
        return (
            "REDISTRIBUIR_PARA_AKG",
            f"{pct_akg:.0%} das vendas via AKG — redistribuir: concentrar {pct_akg:.0%} do estoque em AKG",
        )
    return (
        "PROPORCIONAL",
        f"Shinsei {pct_shinsei:.0%} / AKG {pct_akg:.0%} — manter distribuição proporcional às vendas",
    )


def _calcular_estoque_alinhamento(v: VendasSku, est_shinsei: float, est_akg: float) -> VendasSku:
    """Preenche campos de estoque e verifica desalinhamento."""
    v.estoque_shinsei = est_shinsei
    v.estoque_akg = est_akg
    v.estoque_total = est_shinsei + est_akg

    if v.estoque_total == 0:
        v.estoque_desalinhado = False
        return v

    # Proporção atual de estoque
    pct_est_shinsei = est_shinsei / v.estoque_total

    # Ideal: mesma proporção das vendas
    ideal_shinsei = round(v.estoque_total * v.pct_shinsei / 100)
    ideal_akg = round(v.estoque_total * v.pct_akg / 100)
    v.qtd_sugerida_shinsei = ideal_shinsei
    v.qtd_sugerida_akg = ideal_akg

    # Desalinhamento: diferença > 20% do total
    diff = abs(pct_est_shinsei - v.pct_shinsei / 100)
    v.estoque_desalinhado = diff > 0.20

    if v.recomendacao == "TRANSFERIR_PARA_SHINSEI" and est_akg > 0:
        v.qtd_a_transferir = est_akg
        v.direcao_transferencia = "akg→shinsei"
    elif v.recomendacao == "TRANSFERIR_PARA_AKG" and est_shinsei > 0:
        v.qtd_a_transferir = est_shinsei
        v.direcao_transferencia = "shinsei→akg"
    elif v.recomendacao in ("REDISTRIBUIR_PARA_SHINSEI", "REDISTRIBUIR_PARA_AKG", "PROPORCIONAL"):
        delta = ideal_shinsei - est_shinsei
        if delta > 0:
            v.qtd_a_transferir = delta
            v.direcao_transferencia = "akg→shinsei"
        elif delta < 0:
            v.qtd_a_transferir = abs(delta)
            v.direcao_transferencia = "shinsei→akg"

    return v


# ── Queries ───────────────────────────────────────────────────────────────────

def _query_vendas_cruzadas(
    dias: int,
    min_vendas: int,
    sku_filtro: Optional[str],
    recomendacao_filtro: Optional[str],
    pagina: int,
    limite: int,
) -> tuple[list[VendasSku], int]:
    """
    Busca vendas cruzadas do banco local (rápido, sem Bling).
    Retorna lista de VendasSku e total de registros.
    """
    data_ini = (datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%d")

    conn = _get_conn()
    try:
        # Agrega vendas por SKU e empresa
        rows = conn.execute("""
            SELECT
                a.sku,
                MAX(COALESCE(a.canal_venda, v.empresa_vendedora)) AS nome_ref,
                v.empresa_vendedora,
                SUM(a.quantidade)                       AS total_qtd,
                COUNT(DISTINCT v.id_pedido_bling)       AS num_pedidos
            FROM me_ajustes a
            JOIN me_vendas v ON v.id = a.id_venda_ctrl
            WHERE a.status = 'concluido'
              AND v.data_venda >= ?
              AND (? IS NULL OR a.sku = ?)
            GROUP BY a.sku, v.empresa_vendedora
        """, (data_ini, sku_filtro, sku_filtro)).fetchall()

        # Pivot: {sku → {shinsei: {qtd, pedidos}, akg: {qtd, pedidos}}}
        pivot: dict[str, dict] = {}
        for row in rows:
            sku = row["sku"]
            if sku not in pivot:
                pivot[sku] = {
                    "shinsei": {"qtd": 0.0, "pedidos": 0},
                    "akg":     {"qtd": 0.0, "pedidos": 0},
                }
            emp = row["empresa_vendedora"]
            if emp in ("shinsei", "akg"):
                pivot[sku][emp]["qtd"]    += row["total_qtd"]
                pivot[sku][emp]["pedidos"] += row["num_pedidos"]

        resultados: list[VendasSku] = []
        for sku, dados in pivot.items():
            q_sh = dados["shinsei"]["qtd"]
            q_ak = dados["akg"]["qtd"]
            total = q_sh + q_ak
            if total < min_vendas:
                continue

            pct_sh = round(q_sh / total * 100, 1)
            pct_ak = round(q_ak / total * 100, 1)
            dominante = (
                "shinsei" if pct_sh > 55 else
                "akg"     if pct_ak > 55 else
                "equilibrado"
            )
            rec, desc = _recomendar(pct_sh, pct_ak)

            if recomendacao_filtro and rec != recomendacao_filtro:
                continue

            resultados.append(VendasSku(
                sku=sku,
                nome=sku,
                total_qtd=total,
                qtd_shinsei=q_sh,
                qtd_akg=q_ak,
                num_pedidos_shinsei=dados["shinsei"]["pedidos"],
                num_pedidos_akg=dados["akg"]["pedidos"],
                pct_shinsei=pct_sh,
                pct_akg=pct_ak,
                canal_dominante=dominante,
                recomendacao=rec,
                descricao=desc,
            ))

        # Ordenar por impacto (mais vendido primeiro)
        resultados.sort(key=lambda x: x.total_qtd, reverse=True)

        total_registros = len(resultados)
        offset = (pagina - 1) * limite
        return resultados[offset : offset + limite], total_registros

    finally:
        conn.close()


def _resumo_recomendacoes(dias: int, min_vendas: int) -> dict:
    data_ini = (datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%d")
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT a.sku, v.empresa_vendedora,
                   SUM(a.quantidade) AS total_qtd
            FROM me_ajustes a
            JOIN me_vendas v ON v.id = a.id_venda_ctrl
            WHERE a.status = 'concluido'
              AND v.data_venda >= ?
            GROUP BY a.sku, v.empresa_vendedora
        """, (data_ini,)).fetchall()

        pivot: dict[str, dict] = {}
        for row in rows:
            sku = row["sku"]
            if sku not in pivot:
                pivot[sku] = {"shinsei": 0.0, "akg": 0.0}
            emp = row["empresa_vendedora"]
            if emp in ("shinsei", "akg"):
                pivot[sku][emp] += row["total_qtd"]

        contagem = {
            "TRANSFERIR_PARA_SHINSEI":    0,
            "TRANSFERIR_PARA_AKG":        0,
            "REDISTRIBUIR_PARA_SHINSEI":  0,
            "REDISTRIBUIR_PARA_AKG":      0,
            "PROPORCIONAL":               0,
        }
        total_skus = 0
        for sku, dados in pivot.items():
            total = dados["shinsei"] + dados["akg"]
            if total < min_vendas:
                continue
            pct_sh = dados["shinsei"] / total * 100
            pct_ak = dados["akg"] / total * 100
            rec, _ = _recomendar(pct_sh, pct_ak)
            contagem[rec] = contagem.get(rec, 0) + 1
            total_skus += 1

        skus_only_shinsei = sum(1 for d in pivot.values() if d["akg"] == 0 and d["shinsei"] >= min_vendas)
        skus_only_akg     = sum(1 for d in pivot.values() if d["shinsei"] == 0 and d["akg"] >= min_vendas)

        return {
            "periodo_dias": dias,
            "min_vendas":   min_vendas,
            "total_skus_analisados": total_skus,
            "skus_exclusivos_shinsei": skus_only_shinsei,
            "skus_exclusivos_akg":     skus_only_akg,
            "recomendacoes": contagem,
        }
    finally:
        conn.close()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/analitico/vendas-cruzadas")
def vendas_cruzadas(
    dias:          int            = Query(90,  description="Janela de análise em dias"),
    min_vendas:    int            = Query(3,   description="Mínimo de unidades vendidas para incluir"),
    sku:           Optional[str]  = Query(None, description="Filtrar por SKU específico"),
    recomendacao:  Optional[str]  = Query(None, description="Filtrar por tipo: TRANSFERIR_PARA_SHINSEI | TRANSFERIR_PARA_AKG | REDISTRIBUIR_PARA_SHINSEI | REDISTRIBUIR_PARA_AKG | PROPORCIONAL"),
    pagina:        int            = Query(1),
    limite:        int            = Query(50,  le=200),
    include_estoque: bool         = Query(False, description="Buscar estoque atual no Bling (mais lento — max 20 SKUs)"),
):
    """
    Analisa vendas cruzadas entre Shinsei e AKG por SKU.

    Para cada SKU calcula:
    - Quantas unidades foram vendidas por cada empresa
    - Percentual de vendas por canal
    - Recomendação de distribuição de estoque

    Parâmetros:
    - `dias`: janela de análise (padrão 90 dias)
    - `min_vendas`: mínimo de unidades para aparecer no resultado (filtra ruído)
    - `include_estoque`: busca estoque atual no Bling (mais lento, limite 20 SKUs/req)
    """
    itens, total = _query_vendas_cruzadas(
        dias=dias,
        min_vendas=min_vendas,
        sku_filtro=sku,
        recomendacao_filtro=recomendacao,
        pagina=pagina,
        limite=limite,
    )

    if include_estoque and itens:
        # Limitar lookup a no máximo 20 SKUs para não estourar rate limit Bling
        skus_com_estoque = itens[:20]
        for v in skus_com_estoque:
            id_sh = _buscar_id_produto("shinsei", v.sku)
            id_ak = _buscar_id_produto("akg",     v.sku)
            est_sh = _saldo_deposito("shinsei", id_sh, DEP_SHINSEI_GERAL) if id_sh else 0.0
            est_ak = _saldo_deposito("akg",     id_ak, DEP_AKG_GERAL)     if id_ak else 0.0
            _calcular_estoque_alinhamento(v, est_sh, est_ak)
            time.sleep(0.4)  # rate limit Bling

    return {
        "ok":     True,
        "total":  total,
        "pagina": pagina,
        "limite": limite,
        "dias":   dias,
        "thresholds": {
            "transferencia":  f"≥{LIMIAR_TRANSFERENCIA:.0%}",
            "redistribuicao": f"≥{LIMIAR_REDISTRIBUICAO:.0%}",
        },
        "itens": [asdict(v) for v in itens],
    }


@router.get("/analitico/resumo")
def resumo_recomendacoes(
    dias:       int = Query(90, description="Janela de análise em dias"),
    min_vendas: int = Query(3,  description="Mínimo de unidades vendidas"),
):
    """
    Resumo de quantos SKUs precisam de cada ação de redistribuição.
    Rápido — não consulta Bling.
    """
    return _resumo_recomendacoes(dias, min_vendas)


@router.get("/analitico/top-desalinhados")
def top_desalinhados(
    dias:       int = Query(90),
    min_vendas: int = Query(5),
    top:        int = Query(20, le=50, description="Top N SKUs mais críticos"),
):
    """
    Retorna os SKUs mais desalinhados (maior volume de vendas + recomendação crítica).
    Inclui estoque atual do Bling — pode demorar até 30s para 20 SKUs.
    """
    itens, _ = _query_vendas_cruzadas(
        dias=dias,
        min_vendas=min_vendas,
        sku_filtro=None,
        recomendacao_filtro=None,
        pagina=1,
        limite=500,  # busca mais e filtra
    )

    # Prioridade: TRANSFERIR > REDISTRIBUIR > PROPORCIONAL, depois por volume
    pesos = {
        "TRANSFERIR_PARA_SHINSEI":   3,
        "TRANSFERIR_PARA_AKG":       3,
        "REDISTRIBUIR_PARA_SHINSEI": 2,
        "REDISTRIBUIR_PARA_AKG":     2,
        "PROPORCIONAL":              1,
    }
    itens.sort(key=lambda x: (pesos.get(x.recomendacao, 0), x.total_qtd), reverse=True)
    selecionados = itens[:top]

    # Buscar estoque para os selecionados
    for v in selecionados:
        id_sh = _buscar_id_produto("shinsei", v.sku)
        id_ak = _buscar_id_produto("akg",     v.sku)
        est_sh = _saldo_deposito("shinsei", id_sh, DEP_SHINSEI_GERAL) if id_sh else 0.0
        est_ak = _saldo_deposito("akg",     id_ak, DEP_AKG_GERAL)     if id_ak else 0.0
        _calcular_estoque_alinhamento(v, est_sh, est_ak)
        time.sleep(0.35)

    return {
        "ok":   True,
        "dias": dias,
        "top":  top,
        "itens": [asdict(v) for v in selecionados],
    }


@router.get("/analitico/sku/{sku}")
def detalhe_sku(
    sku: str,
    dias: int = Query(180, description="Janela histórica em dias"),
):
    """
    Detalhe completo de um SKU: histórico por mês, estoque atual, recomendação.
    """
    data_ini = (datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%d")

    conn = _get_conn()
    try:
        # Série mensal
        serie = conn.execute("""
            SELECT
                substr(v.data_venda, 1, 7)  AS mes,
                v.empresa_vendedora,
                SUM(a.quantidade)           AS qtd,
                COUNT(DISTINCT v.id_pedido_bling) AS pedidos
            FROM me_ajustes a
            JOIN me_vendas v ON v.id = a.id_venda_ctrl
            WHERE a.status = 'concluido'
              AND a.sku = ?
              AND v.data_venda >= ?
            GROUP BY mes, v.empresa_vendedora
            ORDER BY mes
        """, (sku, data_ini)).fetchall()

        # Totais gerais no período
        totais = conn.execute("""
            SELECT
                v.empresa_vendedora,
                SUM(a.quantidade)           AS total_qtd,
                COUNT(DISTINCT v.id_pedido_bling) AS total_pedidos
            FROM me_ajustes a
            JOIN me_vendas v ON v.id = a.id_venda_ctrl
            WHERE a.status = 'concluido'
              AND a.sku = ?
              AND v.data_venda >= ?
            GROUP BY v.empresa_vendedora
        """, (sku, data_ini)).fetchall()

        dados_por_empresa = {"shinsei": {"qtd": 0.0, "pedidos": 0}, "akg": {"qtd": 0.0, "pedidos": 0}}
        for row in totais:
            emp = row["empresa_vendedora"]
            if emp in dados_por_empresa:
                dados_por_empresa[emp]["qtd"]    = row["total_qtd"]
                dados_por_empresa[emp]["pedidos"] = row["total_pedidos"]

        total_qtd = dados_por_empresa["shinsei"]["qtd"] + dados_por_empresa["akg"]["qtd"]

    finally:
        conn.close()

    pct_sh = round(dados_por_empresa["shinsei"]["qtd"] / total_qtd * 100, 1) if total_qtd else 0
    pct_ak = round(dados_por_empresa["akg"]["qtd"] / total_qtd * 100, 1) if total_qtd else 0

    rec, desc = _recomendar(pct_sh, pct_ak) if total_qtd >= 3 else ("SEM_HISTORICO", "Menos de 3 vendas no período")

    # Estoque atual
    id_sh = _buscar_id_produto("shinsei", sku)
    id_ak = _buscar_id_produto("akg",     sku)
    est_sh = _saldo_deposito("shinsei", id_sh, DEP_SHINSEI_GERAL) if id_sh else 0.0
    est_ak = _saldo_deposito("akg",     id_ak, DEP_AKG_GERAL)     if id_ak else 0.0

    item = VendasSku(
        sku=sku, nome=sku,
        total_qtd=total_qtd,
        qtd_shinsei=dados_por_empresa["shinsei"]["qtd"],
        qtd_akg=dados_por_empresa["akg"]["qtd"],
        num_pedidos_shinsei=dados_por_empresa["shinsei"]["pedidos"],
        num_pedidos_akg=dados_por_empresa["akg"]["pedidos"],
        pct_shinsei=pct_sh, pct_akg=pct_ak,
        canal_dominante="shinsei" if pct_sh > 55 else "akg" if pct_ak > 55 else "equilibrado",
        recomendacao=rec, descricao=desc,
    )
    _calcular_estoque_alinhamento(item, est_sh, est_ak)

    # Série mensal formatada
    serie_dict: dict[str, dict] = {}
    for row in serie:
        m = row["mes"]
        if m not in serie_dict:
            serie_dict[m] = {"mes": m, "shinsei": 0.0, "akg": 0.0, "pedidos_shinsei": 0, "pedidos_akg": 0}
        emp = row["empresa_vendedora"]
        if emp == "shinsei":
            serie_dict[m]["shinsei"]         += row["qtd"]
            serie_dict[m]["pedidos_shinsei"]  += row["pedidos"]
        elif emp == "akg":
            serie_dict[m]["akg"]         += row["qtd"]
            serie_dict[m]["pedidos_akg"]  += row["pedidos"]

    return {
        "ok":         True,
        "sku":        sku,
        "dias":       dias,
        "analise":    asdict(item),
        "serie_mensal": list(serie_dict.values()),
    }


@router.get("/analitico", response_class=HTMLResponse)
def pagina_analitico():
    """Página HTML do analítico de distribuição de estoque multiempresa."""
    return open(BASE_DIR / "pages" / "multiempresa_analitico.html", encoding="utf-8").read()
