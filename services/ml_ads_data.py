# -*- coding: utf-8 -*-
"""
services/ml_ads_data.py — Coleta e análise de vendas ML para o simulador de ADS
Puxa orders dos últimos 12 meses, agrega por produto/semana e persiste em SQLite.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("shinsei.ml_ads")

BASE_DIR  = Path(__file__).parent.parent
DATA_DIR  = BASE_DIR / "data"
DB_PATH   = DATA_DIR / "ml_ads_simulator.db"
ML_API    = "https://api.mercadolibre.com"


# ── Token ML ──────────────────────────────────────────────────────────────────

def _get_token() -> str:
    from services.mercado_livre import obter_token_ml
    return obter_token_ml()


def _headers() -> dict:
    return {"Authorization": f"Bearer {_get_token()}"}


# ── Banco de dados ────────────────────────────────────────────────────────────

def init_db() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # Orders brutos (cache)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ml_orders (
            order_id      INTEGER PRIMARY KEY,
            item_id       TEXT NOT NULL,
            item_title    TEXT,
            variation_id  TEXT,
            quantity      INTEGER DEFAULT 1,
            unit_price    REAL,
            total_amount  REAL,
            coupon_amount REAL DEFAULT 0,
            date_created  TEXT,
            week          TEXT,   -- YYYY-Www
            year_month    TEXT    -- YYYY-MM
        )
    """)

    # Agregado por produto + semana
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ml_product_weekly (
            item_id      TEXT NOT NULL,
            week         TEXT NOT NULL,
            units_sold   INTEGER DEFAULT 0,
            revenue      REAL DEFAULT 0,
            avg_price    REAL DEFAULT 0,
            avg_discount REAL DEFAULT 0,
            orders_count INTEGER DEFAULT 0,
            PRIMARY KEY (item_id, week)
        )
    """)

    # Investimento de campanha por produto + período (input manual / CSV)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ml_campaign_investments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id      TEXT,           -- NULL = campanha global
            date_from    TEXT NOT NULL,
            date_to      TEXT NOT NULL,
            investment   REAL NOT NULL,
            campaign_name TEXT,
            notes        TEXT,
            created_at   TEXT DEFAULT (datetime('now'))
        )
    """)

    # Metadados de produtos (título, foto, preço atual)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ml_product_meta (
            item_id      TEXT PRIMARY KEY,
            title        TEXT,
            thumbnail    TEXT,
            current_price REAL,
            category_id  TEXT,
            updated_at   TEXT
        )
    """)

    # Controle de sincronização
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ml_sync_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            ended_at   TEXT,
            orders_fetched INTEGER DEFAULT 0,
            status     TEXT DEFAULT 'running'
        )
    """)

    con.commit()
    con.close()


# ── Coleta de orders ──────────────────────────────────────────────────────────

def _iso_week(dt_str: str) -> str:
    """Converte string de data em YYYY-Www (semana ISO)."""
    try:
        dt = datetime.fromisoformat(dt_str[:19])
        return dt.strftime("%G-W%V")
    except Exception:
        return "0000-W00"


def _year_month(dt_str: str) -> str:
    try:
        return dt_str[:7]
    except Exception:
        return "0000-00"


def fetch_orders(user_id: int, months: int = 12) -> list[dict]:
    """Busca todos os orders pagos dos últimos `months` meses."""
    date_from = (datetime.now(timezone.utc) - timedelta(days=months * 30)).strftime(
        "%Y-%m-%dT00:00:00.000-03:00"
    )
    H = _headers()
    all_orders = []
    offset = 0
    limit = 50

    while True:
        r = requests.get(
            f"{ML_API}/orders/search",
            params={
                "seller": user_id,
                "order.status": "paid",
                "order.date_created.from": date_from,
                "limit": limit,
                "offset": offset,
                "sort": "date_asc",
            },
            headers=H,
            timeout=30,
        )
        if r.status_code == 401:
            # Token expirado — renovar e tentar uma vez
            H = _headers()
            r = requests.get(
                f"{ML_API}/orders/search",
                params={
                    "seller": user_id,
                    "order.status": "paid",
                    "order.date_created.from": date_from,
                    "limit": limit,
                    "offset": offset,
                    "sort": "date_asc",
                },
                headers=H,
                timeout=30,
            )

        if r.status_code != 200:
            logger.error("Orders API error %s: %s", r.status_code, r.text[:200])
            break

        data = r.json()
        results = data.get("results", [])
        total = data.get("paging", {}).get("total", 0)

        for order in results:
            date_created = order.get("date_created", "")
            payments = order.get("payments", [])
            coupon_amount = sum(p.get("coupon_amount", 0) or 0 for p in payments)

            for oi in order.get("order_items", []):
                item = oi.get("item", {})
                all_orders.append({
                    "order_id":     order.get("id"),
                    "item_id":      item.get("id", ""),
                    "item_title":   item.get("title", ""),
                    "variation_id": str(item.get("variation_id", "") or ""),
                    "quantity":     oi.get("quantity", 1),
                    "unit_price":   oi.get("unit_price", 0),
                    "total_amount": (oi.get("quantity", 1) or 1) * (oi.get("unit_price", 0) or 0),
                    "coupon_amount": coupon_amount,
                    "date_created": date_created,
                    "week":         _iso_week(date_created),
                    "year_month":   _year_month(date_created),
                })

        logger.info("Orders: %d/%d (offset=%d)", len(all_orders), total, offset)
        offset += limit

        if offset >= total or not results:
            break

        time.sleep(0.3)

    return all_orders


def save_orders(orders: list[dict]) -> int:
    """Persiste orders no banco, ignorando duplicatas."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    saved = 0
    for o in orders:
        try:
            cur.execute("""
                INSERT OR IGNORE INTO ml_orders
                (order_id, item_id, item_title, variation_id, quantity,
                 unit_price, total_amount, coupon_amount, date_created, week, year_month)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                o["order_id"], o["item_id"], o["item_title"], o["variation_id"],
                o["quantity"], o["unit_price"], o["total_amount"], o["coupon_amount"],
                o["date_created"], o["week"], o["year_month"],
            ))
            saved += cur.rowcount
        except Exception as e:
            logger.warning("Erro ao salvar order %s: %s", o.get("order_id"), e)
    con.commit()
    con.close()
    return saved


# ── Agregação semanal ─────────────────────────────────────────────────────────

def rebuild_weekly_stats() -> None:
    """Recalcula ml_product_weekly a partir dos orders brutos."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("DELETE FROM ml_product_weekly")
    cur.execute("""
        INSERT INTO ml_product_weekly (item_id, week, units_sold, revenue, avg_price, avg_discount, orders_count)
        SELECT
            item_id,
            week,
            SUM(quantity)                            AS units_sold,
            SUM(total_amount)                        AS revenue,
            AVG(unit_price)                          AS avg_price,
            AVG(coupon_amount / NULLIF(total_amount + coupon_amount, 0)) AS avg_discount,
            COUNT(DISTINCT order_id)                 AS orders_count
        FROM ml_orders
        GROUP BY item_id, week
    """)
    con.commit()
    con.close()


# ── Metadados de produtos ─────────────────────────────────────────────────────

def fetch_product_meta(item_ids: list[str]) -> None:
    """Busca título, thumbnail e preço atual para uma lista de item_ids."""
    H = _headers()
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    for i in range(0, len(item_ids), 20):
        batch = item_ids[i:i + 20]
        r = requests.get(
            f"{ML_API}/items",
            params={"ids": ",".join(batch), "attributes": "id,title,thumbnail,price,category_id"},
            headers=H,
            timeout=20,
        )
        if r.status_code != 200:
            continue
        for entry in r.json():
            body = entry.get("body", entry)
            if not body.get("id"):
                continue
            cur.execute("""
                INSERT OR REPLACE INTO ml_product_meta (item_id, title, thumbnail, current_price, category_id, updated_at)
                VALUES (?,?,?,?,?,datetime('now'))
            """, (
                body["id"],
                body.get("title", ""),
                body.get("thumbnail", ""),
                body.get("price", 0),
                body.get("category_id", ""),
            ))
        time.sleep(0.2)

    con.commit()
    con.close()


# ── Sync completo ─────────────────────────────────────────────────────────────

_sync_status: dict = {"running": False, "progress": 0, "total": 0, "msg": ""}


def sync_all(user_id: int = 733168645, months: int = 12) -> dict:
    """Executa sincronização completa: orders → stats → meta."""
    global _sync_status
    if _sync_status.get("running"):
        return {"ok": False, "msg": "Sincronização já em andamento"}

    _sync_status = {"running": True, "progress": 0, "total": 0, "msg": "Iniciando..."}
    init_db()

    con = sqlite3.connect(DB_PATH)
    log_id = con.execute(
        "INSERT INTO ml_sync_log (started_at, status) VALUES (datetime('now'), 'running')"
    ).lastrowid
    con.commit()
    con.close()

    try:
        _sync_status["msg"] = "Buscando orders..."
        orders = fetch_orders(user_id, months)
        _sync_status["total"] = len(orders)
        _sync_status["msg"] = f"Salvando {len(orders)} registros..."

        saved = save_orders(orders)

        _sync_status["msg"] = "Calculando estatísticas semanais..."
        rebuild_weekly_stats()

        # Metadados dos produtos únicos
        item_ids = list({o["item_id"] for o in orders if o["item_id"]})
        _sync_status["msg"] = f"Buscando metadados de {len(item_ids)} produtos..."
        fetch_product_meta(item_ids)

        _sync_status["msg"] = "Concluído"
        _sync_status["running"] = False
        _sync_status["progress"] = len(orders)

        con = sqlite3.connect(DB_PATH)
        con.execute(
            "UPDATE ml_sync_log SET ended_at=datetime('now'), orders_fetched=?, status='ok' WHERE id=?",
            (saved, log_id),
        )
        con.commit()
        con.close()

        return {"ok": True, "orders_fetched": len(orders), "saved": saved, "produtos": len(item_ids)}

    except Exception as e:
        logger.exception("Erro na sincronização ML Ads")
        _sync_status = {"running": False, "progress": 0, "total": 0, "msg": f"Erro: {e}"}
        con = sqlite3.connect(DB_PATH)
        con.execute(
            "UPDATE ml_sync_log SET ended_at=datetime('now'), status='error' WHERE id=?",
            (log_id,),
        )
        con.commit()
        con.close()
        return {"ok": False, "msg": str(e)}


def get_sync_status() -> dict:
    return dict(_sync_status)


# ── Queries para a interface ──────────────────────────────────────────────────

def get_produtos(limit: int = 200) -> list[dict]:
    """Lista produtos com totais do último ano, ordenados por receita."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT
            o.item_id,
            COALESCE(m.title, o.item_id)  AS title,
            m.thumbnail,
            m.current_price,
            SUM(o.quantity)               AS total_units,
            SUM(o.total_amount)           AS total_revenue,
            AVG(o.unit_price)             AS avg_price,
            AVG(o.coupon_amount)          AS avg_discount,
            COUNT(DISTINCT o.order_id)    AS total_orders,
            MIN(o.date_created)           AS first_sale,
            MAX(o.date_created)           AS last_sale
        FROM ml_orders o
        LEFT JOIN ml_product_meta m ON m.item_id = o.item_id
        GROUP BY o.item_id
        ORDER BY total_revenue DESC
        LIMIT ?
    """, (limit,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_historico_semanal(item_id: str) -> list[dict]:
    """Retorna histórico semanal de um produto."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT w.week, w.units_sold, w.revenue, w.avg_price, w.avg_discount, w.orders_count,
               COALESCE(ci.investment, 0) AS investment
        FROM ml_product_weekly w
        LEFT JOIN ml_campaign_investments ci
            ON ci.item_id = w.item_id
            AND substr(w.week, 1, 4) || '-' ||
                printf('%02d', (cast(substr(w.week, 7) as integer) - 1) * 7 / 7 + 1)
                BETWEEN ci.date_from AND ci.date_to
        WHERE w.item_id = ?
        ORDER BY w.week ASC
    """, (item_id,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_historico_semanal_global() -> list[dict]:
    """Histórico semanal de todos os produtos (visão global)."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT
            week,
            SUM(units_sold)   AS units_sold,
            SUM(revenue)      AS revenue,
            AVG(avg_price)    AS avg_price,
            SUM(orders_count) AS orders_count
        FROM ml_product_weekly
        GROUP BY week
        ORDER BY week ASC
    """).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_consolidado() -> dict:
    """
    Visão consolidada: faturamento total, investimento total, desconto médio
    e série semanal com % investido e % desconto para cruzamento.
    """
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    # Totais gerais
    totais = con.execute("""
        SELECT
            SUM(units_sold)              AS total_units,
            SUM(revenue)                 AS total_revenue,
            SUM(orders_count)            AS total_orders,
            AVG(avg_price)               AS avg_price_geral,
            AVG(avg_discount)            AS avg_discount_pct,
            MIN(week)                    AS semana_inicio,
            MAX(week)                    AS semana_fim,
            COUNT(DISTINCT item_id)      AS total_produtos
        FROM ml_product_weekly
    """).fetchone()

    # Investimento total registrado
    invest_row = con.execute(
        "SELECT COALESCE(SUM(investment),0) AS total FROM ml_campaign_investments"
    ).fetchone()
    total_invest = invest_row["total"] if invest_row else 0.0

    # Série semanal consolidada com investimento
    semanas_raw = con.execute("""
        SELECT
            w.week,
            SUM(w.units_sold)          AS units_sold,
            SUM(w.revenue)             AS revenue,
            SUM(w.orders_count)        AS orders_count,
            AVG(w.avg_price)           AS avg_price,
            AVG(w.avg_discount)        AS avg_discount_pct
        FROM ml_product_weekly w
        GROUP BY w.week
        ORDER BY w.week ASC
    """).fetchall()

    # Buscar investimentos por data para cruzar com semanas
    investimentos = con.execute(
        "SELECT date_from, date_to, investment FROM ml_campaign_investments ORDER BY date_from"
    ).fetchall()
    con.close()

    # Cruzar investimento com semanas (distribuição proporcional por dias)
    from datetime import date
    inv_list = [(r["date_from"], r["date_to"], r["investment"]) for r in investimentos]

    semanas = []
    for row in semanas_raw:
        week_str = row["week"]  # ex: "2025-W30"
        try:
            year, wnum = week_str.split("-W")
            # Primeira segunda da semana ISO
            week_start = date.fromisocalendar(int(year), int(wnum), 1)
            week_end   = date.fromisocalendar(int(year), int(wnum), 7)
        except Exception:
            week_start = week_end = None

        week_invest = 0.0
        if week_start:
            for df, dt, amt in inv_list:
                try:
                    d0 = date.fromisoformat(df)
                    d1 = date.fromisoformat(dt)
                    # Interseção com a semana
                    overlap_start = max(d0, week_start)
                    overlap_end   = min(d1, week_end)
                    if overlap_end >= overlap_start:
                        total_days = (d1 - d0).days + 1 or 1
                        overlap_days = (overlap_end - overlap_start).days + 1
                        week_invest += amt * (overlap_days / total_days)
                except Exception:
                    pass

        revenue = row["revenue"] or 0.0
        pct_investido = round(week_invest / revenue * 100, 2) if revenue > 0 else 0.0
        pct_desconto  = round(row["avg_discount_pct"] or 0.0, 2)

        semanas.append({
            "week": week_str,
            "units_sold": row["units_sold"],
            "revenue": round(revenue, 2),
            "orders_count": row["orders_count"],
            "avg_price": round(row["avg_price"] or 0, 2),
            "avg_discount_pct": pct_desconto,
            "investment": round(week_invest, 2),
            "pct_investido": pct_investido,
        })

    total_revenue = float(totais["total_revenue"] or 0)
    pct_invest_total = round(total_invest / total_revenue * 100, 2) if total_revenue > 0 else 0.0

    return {
        "total_revenue":      round(total_revenue, 2),
        "total_units":        int(totais["total_units"] or 0),
        "total_orders":       int(totais["total_orders"] or 0),
        "total_produtos":     int(totais["total_produtos"] or 0),
        "total_investimento": round(total_invest, 2),
        "pct_investido":      pct_invest_total,
        "avg_discount_pct":   round(float(totais["avg_discount_pct"] or 0), 2),
        "avg_price_geral":    round(float(totais["avg_price_geral"] or 0), 2),
        "semana_inicio":      totais["semana_inicio"],
        "semana_fim":         totais["semana_fim"],
        "semanas":            semanas,
    }


def get_ranking_roi(min_semanas_invest: int = 1) -> dict:
    """
    Ranking de produtos por ROAS histórico de ADS.

    Para cada produto com registros de investimento, separa semanas
    COM investimento vs SEM investimento e calcula:
      - ROAS = receita_em_semanas_com_ads / total_investido
      - uplift_velocidade = vel_com_ads / vel_sem_ads - 1
      - roi_incremental = (receita_incremental - invest) / invest
    Ordenado por ROAS decrescente.
    """
    from datetime import date as _date

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    # Investimentos por produto (apenas product-level, não globais)
    inv_rows = con.execute("""
        SELECT item_id, date_from, date_to, investment
        FROM ml_campaign_investments
        WHERE item_id IS NOT NULL
        ORDER BY date_from
    """).fetchall()

    if not inv_rows:
        con.close()
        return {"ok": True, "sem_dados": True,
                "msg": "Nenhum investimento por produto registrado. Registre investimentos no simulador para calcular o ROI histórico.",
                "produtos": []}

    # Agrupar investimentos por item_id
    invest_map: dict[str, list[dict]] = {}
    for r in inv_rows:
        invest_map.setdefault(r["item_id"], []).append(dict(r))

    # Meta dos produtos
    meta_rows = con.execute("SELECT item_id, title, thumbnail, current_price FROM ml_product_meta").fetchall()
    meta_map = {r["item_id"]: dict(r) for r in meta_rows}

    resultados = []

    for item_id, invs in invest_map.items():
        weeks_rows = con.execute(
            "SELECT week, units_sold, revenue, avg_price FROM ml_product_weekly WHERE item_id = ? ORDER BY week",
            (item_id,)
        ).fetchall()

        if not weeks_rows:
            continue

        total_invest = sum(i["investment"] for i in invs)

        # Construir lista de períodos de investimento como (date0, date1, invest)
        periodos = []
        for inv in invs:
            try:
                d0 = _date.fromisoformat(inv["date_from"])
                d1 = _date.fromisoformat(inv["date_to"])
                periodos.append((d0, d1, float(inv["investment"])))
            except Exception:
                pass

        semanas_com: list[dict] = []
        semanas_sem: list[dict] = []
        invest_alocado = 0.0  # investimento rateado para semanas sobrepostas

        for w in weeks_rows:
            week_str = w["week"]
            try:
                year, wnum = week_str.split("-W")
                week_start = _date.fromisocalendar(int(year), int(wnum), 1)
                week_end   = _date.fromisocalendar(int(year), int(wnum), 7)
            except Exception:
                continue

            week_invest = 0.0
            for d0, d1, amt in periodos:
                overlap_start = max(d0, week_start)
                overlap_end   = min(d1, week_end)
                if overlap_end >= overlap_start:
                    total_days   = (d1 - d0).days + 1 or 1
                    overlap_days = (overlap_end - overlap_start).days + 1
                    week_invest += amt * overlap_days / total_days

            row_dict = dict(w)
            row_dict["invest_semana"] = week_invest

            if week_invest > 0:
                semanas_com.append(row_dict)
                invest_alocado += week_invest
            else:
                semanas_sem.append(row_dict)

        if len(semanas_com) < min_semanas_invest:
            continue

        # Métricas
        rev_com   = sum(s["revenue"] for s in semanas_com)
        units_com = sum(s["units_sold"] for s in semanas_com)
        vel_com   = units_com / len(semanas_com)

        rev_sem   = sum(s["revenue"] for s in semanas_sem)
        units_sem = sum(s["units_sold"] for s in semanas_sem)
        vel_sem   = units_sem / len(semanas_sem) if semanas_sem else 0.0

        roas = rev_com / total_invest if total_invest > 0 else None

        # Receita incremental: a parcela acima da baseline (vel_sem)
        avg_price_com = rev_com / units_com if units_com > 0 else 0.0
        vel_incremental = max(0.0, vel_com - vel_sem)
        rev_incremental = vel_incremental * avg_price_com * len(semanas_com)
        roi_incremental = (rev_incremental - total_invest) / total_invest * 100 if total_invest > 0 else None

        uplift_pct = (vel_com / vel_sem - 1) * 100 if vel_sem > 0 else None

        meta = meta_map.get(item_id, {})
        resultados.append({
            "item_id":           item_id,
            "title":             meta.get("title", item_id),
            "thumbnail":         meta.get("thumbnail"),
            "current_price":     meta.get("current_price"),
            "total_invest":      round(total_invest, 2),
            "receita_com_ads":   round(rev_com, 2),
            "roas":              round(roas, 2) if roas is not None else None,
            "roi_incremental_pct": round(roi_incremental, 1) if roi_incremental is not None else None,
            "uplift_velocidade_pct": round(uplift_pct, 1) if uplift_pct is not None else None,
            "vel_com_ads":       round(vel_com, 1),
            "vel_sem_ads":       round(vel_sem, 1),
            "semanas_com_ads":   len(semanas_com),
            "semanas_sem_ads":   len(semanas_sem),
        })

    con.close()

    # Ordenar por ROAS (maior primeiro)
    resultados.sort(key=lambda x: x["roas"] or 0, reverse=True)

    return {
        "ok": True,
        "sem_dados": False,
        "total": len(resultados),
        "produtos": resultados,
    }


def salvar_investimento(item_id: Optional[str], date_from: str, date_to: str,
                        investment: float, campaign_name: str = "", notes: str = "") -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("""
        INSERT INTO ml_campaign_investments (item_id, date_from, date_to, investment, campaign_name, notes)
        VALUES (?,?,?,?,?,?)
    """, (item_id, date_from, date_to, investment, campaign_name, notes))
    con.commit()
    row_id = cur.lastrowid
    con.close()
    return row_id


def get_investimentos(item_id: Optional[str] = None) -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    if item_id:
        rows = con.execute(
            "SELECT * FROM ml_campaign_investments WHERE item_id=? OR item_id IS NULL ORDER BY date_from DESC",
            (item_id,)
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM ml_campaign_investments ORDER BY date_from DESC"
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def deletar_investimento(inv_id: int) -> bool:
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM ml_campaign_investments WHERE id=?", (inv_id,))
    con.commit()
    con.close()
    return True


# ── Modelo de simulação ───────────────────────────────────────────────────────

def simular(item_id: str, investment: float, price: float,
            discount_pct: float, weeks: int = 4) -> dict:
    """
    Projeta vendas com base no histórico do produto.

    Lógica:
    - Calcula velocidade base (unidades/semana) do histórico recente
    - Aplica multiplicador de investimento (regressão linear log-log)
    - Aplica multiplicador de desconto (elasticidade estimada)
    - Projeta faturamento para `weeks` semanas
    """
    historico = get_historico_semanal(item_id)
    if not historico:
        return {"ok": False, "msg": "Produto sem histórico de vendas"}

    # Velocidade base: média das últimas 8 semanas com venda
    semanas_com_venda = [h for h in historico if h["units_sold"] > 0]
    recentes = semanas_com_venda[-8:] if len(semanas_com_venda) >= 8 else semanas_com_venda
    if not recentes:
        return {"ok": False, "msg": "Produto sem vendas recentes"}

    velocidade_base = sum(s["units_sold"] for s in recentes) / len(recentes)
    preco_medio_hist = sum(s["avg_price"] for s in recentes) / len(recentes)

    # Multiplicador de investimento
    # Semanas com investimento > 0 vs semanas sem investimento
    com_invest = [s for s in semanas_com_venda if s.get("investment", 0) > 0]
    sem_invest = [s for s in semanas_com_venda if s.get("investment", 0) == 0]

    if com_invest and sem_invest:
        vel_com = sum(s["units_sold"] for s in com_invest) / len(com_invest)
        vel_sem = sum(s["units_sold"] for s in sem_invest) / len(sem_invest)
        invest_medio = sum(s["investment"] for s in com_invest) / len(com_invest)
        if vel_sem > 0 and invest_medio > 0:
            # Multiplicador proporcional ao investimento relativo
            mult_invest = 1 + (vel_com / vel_sem - 1) * (investment / invest_medio)
        else:
            mult_invest = 1 + (investment / 500) * 0.3  # estimativa padrão
    else:
        # Sem histórico de investimento: +0.3% por R$10 investidos (estimativa conservadora)
        mult_invest = 1 + (investment / 500) * 0.3

    # Multiplicador de desconto (elasticidade)
    if preco_medio_hist > 0 and price > 0:
        variacao_preco = (preco_medio_hist - price) / preco_medio_hist
    else:
        variacao_preco = 0

    # Desconto adicional passado como parâmetro
    desconto_total = variacao_preco + discount_pct / 100

    # Elasticidade estimada: -2 (para cada 1% de desconto, +2% de venda)
    ELASTICIDADE = 2.0
    mult_desconto = 1 + desconto_total * ELASTICIDADE

    # Velocidade projetada
    velocidade_proj = velocidade_base * max(0.1, mult_invest) * max(0.1, mult_desconto)

    # Projeção para N semanas
    unidades_proj = velocidade_proj * weeks
    preco_efetivo = price * (1 - discount_pct / 100)
    faturamento_proj = unidades_proj * preco_efetivo
    investimento_total = investment * weeks
    roi = ((faturamento_proj - investimento_total) / investimento_total * 100) if investimento_total > 0 else None
    roas = faturamento_proj / investimento_total if investimento_total > 0 else None

    return {
        "ok": True,
        "item_id": item_id,
        "velocidade_base_semana": round(velocidade_base, 1),
        "mult_investimento": round(mult_invest, 3),
        "mult_desconto": round(mult_desconto, 3),
        "velocidade_proj_semana": round(velocidade_proj, 1),
        "weeks": weeks,
        "unidades_proj": round(unidades_proj, 0),
        "preco_efetivo": round(preco_efetivo, 2),
        "faturamento_proj": round(faturamento_proj, 2),
        "investimento_total": round(investimento_total, 2),
        "roi_pct": round(roi, 1) if roi is not None else None,
        "roas": round(roas, 2) if roas is not None else None,
        "preco_medio_historico": round(preco_medio_hist, 2),
        "semanas_historico": len(semanas_com_venda),
    }


def calcular_meta(item_id: str, meta_unidades: Optional[float], meta_faturamento: Optional[float],
                  weeks: int = 4, preco_atual: Optional[float] = None,
                  custo_unitario: Optional[float] = None,
                  margem_minima_pct: Optional[float] = None,
                  meta_lucro: Optional[float] = None) -> dict:
    """
    Dado uma meta de unidades OU faturamento, calcula três cenários para atingi-la:
      1. Só com desconto (sem investimento em ADS)
      2. Só com investimento em ADS (sem desconto adicional)
      3. Mix equilibrado (metade desconto, metade investimento)

    Inverte a fórmula do simular():
      vel_target = vel_base × mult_invest × (1 + ELASTICIDADE × desconto)
    """
    historico = get_historico_semanal(item_id)
    if not historico:
        return {"ok": False, "msg": "Produto sem histórico de vendas"}

    semanas_com_venda = [h for h in historico if h["units_sold"] > 0]
    recentes = semanas_com_venda[-8:] if len(semanas_com_venda) >= 8 else semanas_com_venda
    if not recentes:
        return {"ok": False, "msg": "Produto sem vendas recentes"}

    velocidade_base_raw = sum(s["units_sold"] for s in recentes) / len(recentes)
    preco_medio_hist    = sum(s["avg_price"]   for s in recentes) / len(recentes)
    preco = preco_atual or preco_medio_hist

    # Ajustar velocidade base pelo preço atual (se diferente do histórico)
    # As vendas históricas ocorreram a preco_medio_hist. Se o preço atual é diferente,
    # a elasticidade já impacta a velocidade esperada antes de qualquer desconto adicional.
    ELASTICIDADE = 2.0
    if preco_medio_hist > 0 and preco != preco_medio_hist:
        variacao_base = (preco_medio_hist - preco) / preco_medio_hist
        velocidade_base = velocidade_base_raw * max(0.05, 1 + ELASTICIDADE * variacao_base)
    else:
        velocidade_base = velocidade_base_raw

    # Parâmetros do modelo de investimento (igual ao simular())
    com_invest = [s for s in semanas_com_venda if s.get("investment", 0) > 0]
    sem_invest  = [s for s in semanas_com_venda if s.get("investment", 0) == 0]

    if com_invest and sem_invest:
        vel_com = sum(s["units_sold"] for s in com_invest) / len(com_invest)
        vel_sem = sum(s["units_sold"] for s in sem_invest) / len(sem_invest)
        invest_medio = sum(s["investment"] for s in com_invest) / len(com_invest)
        ratio_invest = (vel_com / vel_sem - 1) / invest_medio if vel_sem > 0 and invest_medio > 0 else 0.0006
    else:
        ratio_invest = 0.0006  # +0.06% de mult por R$1 (estimativa conservadora)

    # Desconto máximo que ainda preserva margem mínima
    # preco_min = custo / (1 - margem_minima/100)  →  desc_max = 1 - preco_min/preco
    import math
    if custo_unitario and custo_unitario > 0 and preco > 0:
        margem_min = (margem_minima_pct or 0) / 100
        preco_minimo = custo_unitario / (1 - margem_min) if margem_min < 1 else custo_unitario
        desc_max_margem = max(0.0, (1 - preco_minimo / preco)) * 100
    else:
        desc_max_margem = 60.0  # cap padrão sem custo informado

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _lucro_proj(vel_proj: float, preco_ef: float, inv_total: float) -> Optional[float]:
        """Lucro projetado = receita bruta - custo dos produtos - investimento."""
        if custo_unitario is None:
            return None
        unidades = vel_proj * weeks
        return (preco_ef - custo_unitario) * unidades - inv_total

    def _semanas_para_atingir(vel_proj: float, preco_ef: float,
                               meta_un: Optional[float], meta_fat: Optional[float],
                               meta_luc: Optional[float], inv_sem: float) -> Optional[int]:
        if vel_proj <= 0:
            return None
        if meta_luc is not None and custo_unitario is not None:
            # semanas até lucro acumulado >= meta_lucro
            lucro_semana = (preco_ef - custo_unitario) * vel_proj - inv_sem
            if lucro_semana <= 0:
                return None
            return math.ceil(meta_luc / lucro_semana)
        if meta_fat:
            fat_sem = vel_proj * preco_ef
            return math.ceil(meta_fat / fat_sem) if fat_sem > 0 else None
        if meta_un:
            return math.ceil(meta_un / vel_proj)
        return None

    def _fmt_cenario(investment_semana: float, desconto_pct: float,
                     desconto_limitado: bool = False,
                     meta_un: Optional[float] = None,
                     meta_fat: Optional[float] = None,
                     meta_luc: Optional[float] = None) -> dict:
        mult_i = 1 + ratio_invest * investment_semana
        mult_d = 1 + ELASTICIDADE * desconto_pct / 100
        vel_proj = velocidade_base * max(0.1, mult_i) * max(0.1, mult_d)
        unidades = vel_proj * weeks
        preco_ef = preco * (1 - desconto_pct / 100)
        fat = unidades * preco_ef
        inv_total = investment_semana * weeks
        roi  = (fat - inv_total) / inv_total * 100 if inv_total > 0 else None
        roas = fat / inv_total if inv_total > 0 else None

        # Margem sobre venda
        margem_venda = None
        if custo_unitario and custo_unitario > 0 and preco_ef > 0:
            margem_venda = round((preco_ef - custo_unitario) / preco_ef * 100, 1)

        # Lucro projetado real
        lucro = _lucro_proj(vel_proj, preco_ef, inv_total)

        # Verificar se atinge cada tipo de meta
        atinge_un  = unidades >= ((meta_un  or 0) * 0.95) if meta_un else None
        atinge_fat = fat      >= ((meta_fat or 0) * 0.95) if meta_fat else None
        atinge_luc = lucro    >= ((meta_luc or 0) * 0.95) if (meta_luc and lucro is not None) else None

        # Meta principal atingida?
        if meta_luc is not None:
            atinge_meta_principal = atinge_luc
        elif meta_fat is not None:
            atinge_meta_principal = atinge_fat
        else:
            atinge_meta_principal = atinge_un

        # Se não atinge no período, quanto tempo levaria
        semanas_necessarias = None
        if not atinge_meta_principal:
            semanas_necessarias = _semanas_para_atingir(
                vel_proj, preco_ef, meta_un, meta_fat, meta_luc, investment_semana)

        return {
            "investment_semana":        round(investment_semana, 2),
            "investment_total":         round(inv_total, 2),
            "desconto_pct":             round(desconto_pct, 1),
            "desconto_limitado_margem": desconto_limitado,
            "preco_sugerido":           round(preco_ef, 2),
            "margem_sobre_venda_pct":   margem_venda,
            "velocidade_proj":          round(vel_proj, 1),
            "unidades_proj":            round(unidades, 0),
            "faturamento_proj":         round(fat, 2),
            "lucro_proj":               round(lucro, 2) if lucro is not None else None,
            "roi_pct":                  round(roi, 1) if roi is not None else None,
            "roas":                     round(roas, 2) if roas is not None else None,
            "atinge_meta_principal":    atinge_meta_principal,
            "semanas_para_atingir":     semanas_necessarias,
        }

    # ── Determinar meta de volume/faturamento equivalente ────────────────────

    # Quando meta_lucro é especificada, convertemos para meta de faturamento
    # considerando que investimento TAMBÉM sai do lucro (não apenas custo do produto).
    # Para cenário sem investimento: meta_fat = (meta_lucro + custo*unidades) / 1
    # Simplificação: meta_fat = meta_lucro / margem_bruta (aprox sem investimento)
    if meta_lucro is not None and custo_unitario and custo_unitario > 0:
        # Margem bruta sobre o preço atual
        margem_bruta = (preco - custo_unitario) / preco if preco > custo_unitario else 0.0
        meta_faturamento_base = meta_lucro / margem_bruta if margem_bruta > 0 else meta_lucro * 5
        meta_unidades_raw = meta_faturamento_base / preco
        meta_fat_eff = meta_faturamento_base
        meta_un_eff  = None
    elif meta_faturamento and not meta_unidades:
        meta_unidades_raw = meta_faturamento / preco
        meta_fat_eff = meta_faturamento
        meta_un_eff  = None
    elif meta_unidades:
        meta_unidades_raw = meta_unidades
        meta_fat_eff = None
        meta_un_eff  = meta_unidades
    else:
        return {"ok": False, "msg": "Informe meta_unidades, meta_faturamento ou meta_lucro"}

    vel_target = meta_unidades_raw / weeks
    crescimento_needed = vel_target / velocidade_base if velocidade_base > 0 else 1.0

    # ── Cenário 1: Só desconto (investment=0) ────────────────────────────────
    if meta_lucro is not None and custo_unitario and custo_unitario > 0:
        # Resolver: (preco*(1-d) - custo) * vel_base*(1+E*d) * weeks = meta_lucro
        # f(d) é quadrática em d — usar busca binária
        def _lucro_desc(d: float) -> float:
            pef = preco * (1 - d)
            vel = velocidade_base * max(0.1, 1 + ELASTICIDADE * d)
            return (pef - custo_unitario) * vel * weeks

        # Busca binária: encontrar d mínimo que satisfaz lucro >= meta_lucro
        # (a função pode não ser monotônica, mas em geral aumentar d reduz lucro unitário)
        # Primeiro testa se sem desconto já atinge
        if _lucro_desc(0) >= meta_lucro:
            desc_needed = 0.0
            desc_limitado = False
        else:
            # Varredura para encontrar o máximo de lucro possível (pico da curva)
            best_d, best_lucro = 0.0, _lucro_desc(0)
            for di in range(1, int(desc_max_margem) + 1):
                l = _lucro_desc(di / 100)
                if l > best_lucro:
                    best_lucro, best_d = l, di / 100
            desc_needed = best_d
            desc_limitado = best_d >= desc_max_margem / 100 - 0.001
    else:
        desc_needed = max(0.0, (crescimento_needed - 1) / ELASTICIDADE * 100)
        desc_limitado = desc_needed > desc_max_margem
        desc_needed = min(desc_needed, desc_max_margem)

    cenario_desconto = _fmt_cenario(0, desc_needed, desconto_limitado=desc_limitado,
                                    meta_un=meta_un_eff, meta_fat=meta_fat_eff, meta_luc=meta_lucro)

    # ── Helper: investimento máximo que preserva margem mínima ───────────────
    # Dada a equação lucro/fat >= margem_min:
    #   (preco_ef - custo)*vel_eff*(1+ratio*x)*weeks - x*weeks  >=
    #       margem_min * preco_ef * vel_eff * (1+ratio*x) * weeks
    # Simplificando: A = preco_ef * (mb_ef - margem_min) * vel_eff
    #   A*(1 + ratio*x) >= x  →  x*(1 - A*ratio) <= A
    #   x_max = A / (1 - A*ratio)  se  (1 - A*ratio) > 0
    # Se A <= 0: margem bruta já está abaixo do mínimo → inv_max = 0
    # Se A*ratio >= 1: qualquer investimento é sustentável → inv_max = +inf
    margem_min_ratio = (margem_minima_pct or 0) / 100

    def _inv_max_por_margem(preco_ef: float, vel_eff: float) -> float:
        if not custo_unitario or custo_unitario <= 0 or preco_ef <= 0:
            return float("inf")
        mb_ef = (preco_ef - custo_unitario) / preco_ef
        A = preco_ef * (mb_ef - margem_min_ratio) * vel_eff
        if A <= 0:
            return 0.0
        denom = 1 - A * ratio_invest
        if denom <= 0:
            return float("inf")
        return A / denom

    # ── Cenário 2: Só investimento (desconto=0) ──────────────────────────────
    if meta_lucro is not None and custo_unitario and custo_unitario > 0:
        # Resolver: (preco - custo)*vel_base*(1+ratio*x)*weeks - x*weeks = meta_lucro
        base_lucro = (preco - custo_unitario) * velocidade_base * weeks
        K = (preco - custo_unitario) * velocidade_base * ratio_invest * weeks - weeks
        if K > 0:
            inv_needed = max(0.0, (meta_lucro - base_lucro) / K)
        elif base_lucro >= meta_lucro:
            inv_needed = 0.0
        else:
            inv_needed = None  # impossível algebricamente
    else:
        inv_needed = max(0.0, (crescimento_needed - 1) / ratio_invest) if ratio_invest > 0 else crescimento_needed * 500

    # Aplicar teto de margem mínima (prioridade sobre meta_lucro)
    inv_max_c2 = _inv_max_por_margem(preco, velocidade_base)
    if inv_needed is not None:
        inv_needed = min(inv_needed, inv_max_c2)
        cenario_investimento = _fmt_cenario(inv_needed, 0,
                                            meta_un=meta_un_eff, meta_fat=meta_fat_eff, meta_luc=meta_lucro)
    else:
        cenario_investimento = {
            "investment_semana": None, "investment_total": None,
            "desconto_pct": 0, "preco_sugerido": round(preco, 2),
            "velocidade_proj": round(velocidade_base, 1), "unidades_proj": 0,
            "faturamento_proj": 0, "lucro_proj": None,
            "atinge_meta_principal": False,
            "semanas_para_atingir": None,
            "impossivel": True,
            "msg_impossivel": "O retorno do investimento em ADS é insuficiente para cobrir o custo e gerar o lucro desejado",
        }

    # ── Cenário 3: Mix — desconto até teto de margem + investimento ──────────
    if meta_lucro is not None and custo_unitario and custo_unitario > 0:
        desc_mix = desc_needed  # desconto ótimo para lucro (cenário 1)
        lucro_com_desc = _lucro_desc(desc_mix / 100) if callable(_lucro_desc) else 0

        if lucro_com_desc >= meta_lucro:
            inv_mix = 0.0
        else:
            preco_ef_mix = preco * (1 - desc_mix / 100)
            mult_d_mix = 1 + ELASTICIDADE * desc_mix / 100
            K_mix = (preco_ef_mix - custo_unitario) * velocidade_base * mult_d_mix * ratio_invest * weeks - weeks
            base_lucro_mix = (preco_ef_mix - custo_unitario) * velocidade_base * mult_d_mix * weeks
            if K_mix > 0:
                inv_mix = max(0.0, (meta_lucro - base_lucro_mix) / K_mix)
            else:
                inv_mix = 0.0
    else:
        desc_mix = min(desc_needed, desc_max_margem)
        mult_d_mix = 1 + ELASTICIDADE * desc_mix / 100
        mult_i_needed = crescimento_needed / mult_d_mix if mult_d_mix > 0 else crescimento_needed
        inv_mix = max(0.0, (mult_i_needed - 1) / ratio_invest) if ratio_invest > 0 else 0.0

    # Aplicar teto de margem mínima no mix
    vel_base_mix = velocidade_base * (1 + ELASTICIDADE * desc_mix / 100)
    preco_ef_mix_final = preco * (1 - desc_mix / 100)
    inv_max_c3 = _inv_max_por_margem(preco_ef_mix_final, vel_base_mix)
    inv_mix = min(inv_mix, inv_max_c3)

    cenario_mix = _fmt_cenario(inv_mix, desc_mix,
                                meta_un=meta_un_eff, meta_fat=meta_fat_eff, meta_luc=meta_lucro)

    meta_unidades_raw_out = meta_unidades_raw if 'meta_unidades_raw' in dir() else None

    return {
        "ok": True,
        "item_id": item_id,
        "meta_unidades": meta_unidades_raw,
        "meta_faturamento": meta_fat_eff,
        "meta_lucro": meta_lucro,
        "weeks": weeks,
        "velocidade_base": round(velocidade_base, 1),
        "velocidade_base_raw": round(velocidade_base_raw, 1),
        "preco_medio_historico": round(preco_medio_hist, 2),
        "preco_atual": round(preco, 2),
        "crescimento_necessario_pct": round((crescimento_needed - 1) * 100, 1),
        "cenarios": {
            "so_desconto":     cenario_desconto,
            "so_investimento": cenario_investimento,
            "mix_equilibrado": cenario_mix,
        },
    }
