# -*- coding: utf-8 -*-
"""
routes/ads.py — Google Ads diagnostics via SDK (google-ads>=25)
Usa google-ads.yaml (client_id, client_secret, refresh_token,
developer_token, login_customer_id).
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/ads", tags=["ads"])
logger = logging.getLogger(__name__)

BASE_DIR  = Path(__file__).parent.parent
YAML_PATH = BASE_DIR / "google-ads.yaml"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_yaml_creds() -> dict:
    """Lê google-ads.yaml (formato chave: valor) e retorna dict."""
    if not YAML_PATH.exists():
        raise FileNotFoundError(f"google-ads.yaml não encontrado em {YAML_PATH}")
    out: dict = {}
    for line in YAML_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def _build_client():
    """Constrói GoogleAdsClient a partir do google-ads.yaml."""
    from google.ads.googleads.client import GoogleAdsClient
    creds = _load_yaml_creds()
    config = {
        "client_id":         creds["client_id"],
        "client_secret":     creds["client_secret"],
        "refresh_token":     creds["refresh_token"],
        "developer_token":   creds["developer_token"],
        "login_customer_id": creds.get("login_customer_id", ""),
        "use_proto_plus":    True,
    }
    return GoogleAdsClient.load_from_dict(config), creds.get("login_customer_id", "").replace("-", "")


def _gaql(client, customer_id: str, query: str) -> list:
    """Executa query GAQL e retorna lista de rows."""
    ga_service = client.get_service("GoogleAdsService")
    stream = ga_service.search_stream(customer_id=customer_id, query=query)
    rows = []
    for batch in stream:
        rows.extend(batch.results)
    return rows


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status")
def ads_status():
    """Verifica se as credenciais do Google Ads estão válidas."""
    try:
        client, customer_id = _build_client()
        rows = _gaql(client, customer_id, "SELECT customer.id, customer.descriptive_name FROM customer LIMIT 1")
        nome = rows[0].customer.descriptive_name if rows else ""
        return {"ok": True, "customer_id": customer_id, "nome": nome, "msg": "Credenciais válidas"}
    except FileNotFoundError:
        return {"ok": False, "msg": "google-ads.yaml não encontrado"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


@router.get("/diagnostico")
def ads_diagnostico():
    """
    Diagnóstico completo da conta Google Ads:
    - Resumo de campanhas (últimos 30 dias)
    - Top 20 produtos por gasto (Shopping / PMax)
    - Ações de conversão configuradas
    - Grupos de anúncios ativos
    """
    try:
        client, customer_id = _build_client()
    except FileNotFoundError:
        return {"ok": False, "erro": "google-ads.yaml não encontrado"}
    except Exception as e:
        return {"ok": False, "erro": f"Falha ao obter credenciais: {e}"}

    resultado: dict = {
        "ok": True,
        "customer_id": customer_id,
        "diagnosticado_em": datetime.utcnow().isoformat(),
        "campanhas": [],
        "top_produtos": [],
        "conversoes": [],
        "grupos": [],
        "resumo": {},
    }

    # ── 1. Campanhas ─────────────────────────────────────────────────────────
    try:
        rows = _gaql(client, customer_id, """
            SELECT
                campaign.id,
                campaign.name,
                campaign.status,
                campaign.advertising_channel_type,
                campaign_budget.amount_micros,
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions,
                metrics.ctr,
                metrics.average_cpc,
                metrics.all_conversions_value,
                metrics.search_impression_share
            FROM campaign
            WHERE segments.date DURING LAST_30_DAYS
              AND campaign.status != 'REMOVED'
            ORDER BY metrics.cost_micros DESC
        """)

        campanhas = []
        total_custo = total_cliques = total_impr = total_conv = total_conv_value = 0.0

        for row in rows:
            c = row.campaign
            b = row.campaign_budget
            m = row.metrics

            orc      = (b.amount_micros or 0) / 1e6
            custo    = (m.cost_micros or 0) / 1e6
            cpc      = (m.average_cpc or 0) / 1e6
            cliques  = m.clicks or 0
            impr     = m.impressions or 0
            conv     = m.conversions or 0.0
            ctr      = (m.ctr or 0.0) * 100
            conv_val = m.all_conversions_value or 0.0
            imp_share = (m.search_impression_share or 0.0) * 100

            # status e tipo como string
            status_str = c.status.name if hasattr(c.status, "name") else str(c.status)
            tipo_str   = c.advertising_channel_type.name if hasattr(c.advertising_channel_type, "name") else str(c.advertising_channel_type)

            campanhas.append({
                "id":         str(c.id),
                "nome":       c.name,
                "tipo":       tipo_str,
                "status":     status_str,
                "orc_dia":    round(orc, 2),
                "custo":      round(custo, 2),
                "cliques":    cliques,
                "impressoes": impr,
                "ctr":        round(ctr, 2),
                "cpc":        round(cpc, 2),
                "conversoes": round(conv, 1),
                "conv_valor": round(conv_val, 2),
                "imp_share":  round(imp_share, 1),
            })

            total_custo      += custo
            total_cliques    += cliques
            total_impr       += impr
            total_conv       += conv
            total_conv_value += conv_val

        resultado["campanhas"] = campanhas
        resultado["resumo"] = {
            "total_campanhas":  len(campanhas),
            "total_custo":      round(total_custo, 2),
            "total_cliques":    total_cliques,
            "total_impressoes": int(total_impr),
            "total_conversoes": round(total_conv, 1),
            "total_conv_valor": round(total_conv_value, 2),
            "cpc_medio":        round(total_custo / total_cliques, 2) if total_cliques else 0,
            "custo_por_conv":   round(total_custo / total_conv, 2) if total_conv else 0,
            "roas":             round(total_conv_value / total_custo, 2) if total_custo else 0,
        }

    except Exception as e:
        resultado["campanhas_erro"] = str(e)

    # ── 2. Top produtos (Shopping / PMax) ─────────────────────────────────────
    try:
        rows2 = _gaql(client, customer_id, """
            SELECT
                segments.product_title,
                segments.product_item_id,
                segments.product_type_l1,
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions,
                metrics.ctr
            FROM shopping_performance_view
            WHERE segments.date DURING LAST_30_DAYS
              AND metrics.impressions > 0
            ORDER BY metrics.cost_micros DESC
            LIMIT 20
        """)

        top = []
        for row in rows2:
            s = row.segments
            m = row.metrics
            custo  = (m.cost_micros or 0) / 1e6
            top.append({
                "titulo":     s.product_title or s.product_item_id or "?",
                "item_id":    s.product_item_id,
                "tipo_l1":    s.product_type_l1,
                "custo":      round(custo, 2),
                "cliques":    m.clicks or 0,
                "impressoes": m.impressions or 0,
                "ctr":        round((m.ctr or 0) * 100, 2),
                "conversoes": round(m.conversions or 0, 1),
            })
        resultado["top_produtos"] = top

    except Exception as e:
        resultado["top_produtos_erro"] = str(e)

    # ── 3. Ações de conversão ─────────────────────────────────────────────────
    try:
        rows3 = _gaql(client, customer_id, """
            SELECT
                conversion_action.id,
                conversion_action.name,
                conversion_action.type,
                conversion_action.status,
                conversion_action.category,
                conversion_action.click_through_lookback_window_days
            FROM conversion_action
            WHERE conversion_action.status != 'REMOVED'
        """)
        resultado["conversoes"] = [
            {
                "id":     str(row.conversion_action.id),
                "nome":   row.conversion_action.name,
                "tipo":   row.conversion_action.type.name if hasattr(row.conversion_action.type, "name") else str(row.conversion_action.type),
                "status": row.conversion_action.status.name if hasattr(row.conversion_action.status, "name") else str(row.conversion_action.status),
                "cat":    row.conversion_action.category.name if hasattr(row.conversion_action.category, "name") else str(row.conversion_action.category),
                "janela": row.conversion_action.click_through_lookback_window_days,
            }
            for row in rows3
        ]
    except Exception as e:
        resultado["conversoes_erro"] = str(e)

    # ── 4. Grupos de anúncios (top 20 por gasto) ─────────────────────────────
    try:
        rows4 = _gaql(client, customer_id, """
            SELECT
                campaign.name,
                ad_group.id,
                ad_group.name,
                ad_group.status,
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions
            FROM ad_group
            WHERE segments.date DURING LAST_30_DAYS
              AND metrics.impressions > 0
              AND ad_group.status != 'REMOVED'
            ORDER BY metrics.cost_micros DESC
            LIMIT 20
        """)
        resultado["grupos"] = [
            {
                "campanha":   row.campaign.name,
                "grupo":      row.ad_group.name,
                "status":     row.ad_group.status.name if hasattr(row.ad_group.status, "name") else str(row.ad_group.status),
                "custo":      round((row.metrics.cost_micros or 0) / 1e6, 2),
                "cliques":    row.metrics.clicks or 0,
                "impressoes": row.metrics.impressions or 0,
                "conversoes": round(row.metrics.conversions or 0, 1),
            }
            for row in rows4
        ]
    except Exception as e:
        resultado["grupos_erro"] = str(e)

    return resultado
