# -*- coding: utf-8 -*-
"""
GA4 Analytics Data API + Google Search Console — integração completa.

Usa GOOGLE_SA_JSON (base64) já existente no Render + google-auth (já no requirements.txt).
SA: shinsei-indexacao@shinsei-market-seo.iam.gserviceaccount.com

Antes de funcionar: adicionar a SA como Viewer em:
  - GA4: Admin → Property Access Management
  - Search Console: Configurações → Usuários e permissões
"""

from __future__ import annotations
import base64, json, os, time
from datetime import datetime, date, timedelta, timezone

import requests
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/analytics", tags=["analytics"])

GA4_PROPERTY_ID = os.getenv("GA4_PROPERTY_ID", "497827951")
GSC_PROPERTY    = os.getenv("GSC_PROPERTY", "sc-domain:shinseimarket.com.br")
SA_JSON_B64     = os.getenv("GOOGLE_SA_JSON", "")
_SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
]

_token_cache: dict = {"token": None, "expires_at": 0}


def _get_sa_info() -> dict:
    if not SA_JSON_B64:
        raise HTTPException(503, "GOOGLE_SA_JSON não configurado no Render")
    try:
        raw = base64.b64decode(SA_JSON_B64 + "==").decode()
        return json.loads(raw)
    except Exception as e:
        raise HTTPException(503, f"GOOGLE_SA_JSON inválido: {e}")


def _get_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    sa = _get_sa_info()
    try:
        from google.oauth2 import service_account
        import google.auth.transport.requests as ga_req
    except ImportError:
        raise HTTPException(503, "Pacote google-auth não instalado")

    creds = service_account.Credentials.from_service_account_info(sa, scopes=_SCOPES)
    creds.refresh(ga_req.Request())
    _token_cache["token"] = creds.token
    _token_cache["expires_at"] = creds.expiry.timestamp() if creds.expiry else now + 3600
    return _token_cache["token"]


def _ga4_report(body: dict) -> dict:
    token = _get_token()
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{GA4_PROPERTY_ID}:runReport"
    r = requests.post(url, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    if r.status_code == 403:
        raise HTTPException(403,
            "SA sem acesso ao GA4. Adicione shinsei-indexacao@shinsei-market-seo.iam.gserviceaccount.com "
            "como Viewer em GA4 → Admin → Property Access Management.")
    if r.status_code != 200:
        raise HTTPException(502, f"GA4 API {r.status_code}: {r.text[:300]}")
    return r.json()


def _ga4_realtime(body: dict) -> dict:
    token = _get_token()
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{GA4_PROPERTY_ID}:runRealtimeReport"
    r = requests.post(url, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    if r.status_code == 403:
        raise HTTPException(403, "SA sem acesso ao GA4. Veja GET /analytics/setup.")
    if r.status_code != 200:
        raise HTTPException(502, f"GA4 Realtime {r.status_code}: {r.text[:300]}")
    return r.json()


def _gsc_query(body: dict) -> dict:
    token = _get_token()
    prop_enc = requests.utils.quote(GSC_PROPERTY, safe="")
    url = f"https://searchconsole.googleapis.com/webmasters/v3/sites/{prop_enc}/searchAnalytics/query"
    r = requests.post(url, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    if r.status_code == 403:
        raise HTTPException(403,
            "SA sem acesso ao Search Console. Adicione shinsei-indexacao@shinsei-market-seo.iam.gserviceaccount.com "
            "como proprietário em Search Console → Configurações → Usuários e permissões.")
    if r.status_code != 200:
        raise HTTPException(502, f"GSC API {r.status_code}: {r.text[:300]}")
    return r.json()


def _mval(row: dict, idx: int) -> str:
    return (row.get("metricValues") or [{}])[idx].get("value") if idx < len(row.get("metricValues", [])) else "0"

def _dval(row: dict, idx: int) -> str:
    return (row.get("dimensionValues") or [{}])[idx].get("value") if idx < len(row.get("dimensionValues", [])) else ""


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/setup")
def analytics_setup():
    sa = "shinsei-indexacao@shinsei-market-seo.iam.gserviceaccount.com"
    return {
        "sa_email": sa,
        "property_id": GA4_PROPERTY_ID,
        "gsc_property": GSC_PROPERTY,
        "passos_ga4": [
            "1. analytics.google.com → Admin → Property 497827951 → Property Access Management",
            f"2. + Adicionar usuários → {sa} → papel: Viewer",
        ],
        "passos_gsc": [
            "1. search.google.com/search-console → Configurações → Usuários e permissões",
            f"2. Adicionar usuário → {sa} → Proprietário restrito ou Proprietário",
        ],
    }


@router.get("/realtime")
def analytics_realtime():
    """Usuários ativos agora (últimos 30 min)."""
    data = _ga4_realtime({
        "metrics": [{"name": "activeUsers"}],
        "dimensions": [{"name": "country"}],
    })
    total = 0
    by_country = []
    for row in data.get("rows", []):
        u = int(_mval(row, 0) or 0)
        total += u
        by_country.append({"country": _dval(row, 0), "users": u})
    return {
        "ok": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "active_users_30min": total,
        "by_country": sorted(by_country, key=lambda x: -x["users"])[:5],
    }


@router.get("/status")
def analytics_status():
    """Métricas GA4 dos últimos 30 dias + canais + top páginas + dispositivos."""

    def parse_row(report):
        rows = report.get("rows", [])
        if not rows:
            return {}
        row = rows[0]
        headers = report.get("metricHeaders", [])
        result = {}
        for i, h in enumerate(headers):
            try:
                result[h["name"]] = float(_mval(row, i) or 0)
            except Exception:
                result[h["name"]] = 0
        return result

    t30 = parse_row(_ga4_report({
        "dateRanges": [{"startDate": "30daysAgo", "endDate": "today"}],
        "metrics": [
            {"name": "totalUsers"}, {"name": "sessions"}, {"name": "screenPageViews"},
            {"name": "bounceRate"}, {"name": "averageSessionDuration"},
            {"name": "conversions"}, {"name": "totalRevenue"},
        ],
    }))

    t7 = parse_row(_ga4_report({
        "dateRanges": [{"startDate": "7daysAgo", "endDate": "today"}],
        "metrics": [
            {"name": "totalUsers"}, {"name": "sessions"},
            {"name": "conversions"}, {"name": "totalRevenue"},
        ],
    }))

    ch_data = _ga4_report({
        "dateRanges": [{"startDate": "30daysAgo", "endDate": "today"}],
        "dimensions": [{"name": "sessionDefaultChannelGroup"}],
        "metrics": [{"name": "sessions"}, {"name": "conversions"}, {"name": "totalRevenue"}],
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
        "limit": 10,
    })

    pg_data = _ga4_report({
        "dateRanges": [{"startDate": "30daysAgo", "endDate": "today"}],
        "dimensions": [{"name": "pagePath"}],
        "metrics": [{"name": "screenPageViews"}, {"name": "averageSessionDuration"}],
        "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}],
        "limit": 10,
    })

    dv_data = _ga4_report({
        "dateRanges": [{"startDate": "30daysAgo", "endDate": "today"}],
        "dimensions": [{"name": "deviceCategory"}],
        "metrics": [{"name": "sessions"}, {"name": "conversions"}],
    })

    channels = [{"channel": _dval(r, 0), "sessions": int(float(_mval(r, 0) or 0)),
                 "conversions": float(_mval(r, 1) or 0), "revenue": float(_mval(r, 2) or 0)}
                for r in ch_data.get("rows", [])]

    pages = [{"path": _dval(r, 0), "views": int(float(_mval(r, 0) or 0)),
              "avg_duration_s": round(float(_mval(r, 1) or 0))}
             for r in pg_data.get("rows", [])]

    devices = [{"device": _dval(r, 0), "sessions": int(float(_mval(r, 0) or 0)),
                "conversions": float(_mval(r, 1) or 0)}
               for r in dv_data.get("rows", [])]

    rev30 = t30.get("totalRevenue", 0)
    ads_spend = 85 * 30
    roas = round(rev30 / ads_spend, 2) if rev30 > 0 else None

    return {
        "ok": True,
        "property_id": GA4_PROPERTY_ID,
        "coletado_em": datetime.now(timezone.utc).isoformat(),
        "periodo_30d": {
            "usuarios": int(t30.get("totalUsers", 0)),
            "sessoes": int(t30.get("sessions", 0)),
            "pageviews": int(t30.get("screenPageViews", 0)),
            "bounce_rate_pct": round(t30.get("bounceRate", 0) * 100, 1),
            "duracao_media_s": round(t30.get("averageSessionDuration", 0)),
            "conversoes": t30.get("conversions", 0),
            "receita": round(rev30, 2),
            "roas_estimado": roas,
        },
        "periodo_7d": {
            "usuarios": int(t7.get("totalUsers", 0)),
            "sessoes": int(t7.get("sessions", 0)),
            "conversoes": t7.get("conversions", 0),
            "receita": round(t7.get("totalRevenue", 0), 2),
        },
        "canais": channels,
        "top_paginas": pages,
        "dispositivos": devices,
    }


@router.get("/search-console")
def search_console_status():
    """Métricas do Google Search Console — cliques, impressões, CTR, posição (28 dias)."""
    end   = date.today().isoformat()
    start = (date.today() - timedelta(days=28)).isoformat()

    totals = _gsc_query({"startDate": start, "endDate": end, "type": "web", "rowLimit": 1})
    row0 = (totals.get("rows") or [{}])[0]

    pages = _gsc_query({
        "startDate": start, "endDate": end, "type": "web",
        "dimensions": ["page"],
        "rowLimit": 10,
        "orderBy": [{"fieldName": "clicks", "sortOrder": "DESCENDING"}],
    })
    queries = _gsc_query({
        "startDate": start, "endDate": end, "type": "web",
        "dimensions": ["query"],
        "rowLimit": 15,
        "orderBy": [{"fieldName": "clicks", "sortOrder": "DESCENDING"}],
    })

    def fmt_rows(data):
        return [{
            "key": r["keys"][0],
            "clicks": r.get("clicks", 0),
            "impressions": r.get("impressions", 0),
            "ctr_pct": round(r.get("ctr", 0) * 100, 1),
            "position": round(r.get("position", 0), 1),
        } for r in data.get("rows", [])]

    return {
        "ok": True,
        "property": GSC_PROPERTY,
        "periodo": f"{start} → {end}",
        "coletado_em": datetime.now(timezone.utc).isoformat(),
        "totais_28d": {
            "cliques": int(row0.get("clicks", 0)),
            "impressoes": int(row0.get("impressions", 0)),
            "ctr_pct": round(row0.get("ctr", 0) * 100, 1),
            "posicao_media": round(row0.get("position", 0), 1),
        },
        "top_paginas": fmt_rows(pages),
        "top_queries": fmt_rows(queries),
    }


@router.get("/top-produtos")
def analytics_top_produtos():
    """Top páginas /products/ por views + add-to-cart + compras."""
    data = _ga4_report({
        "dateRanges": [{"startDate": "30daysAgo", "endDate": "today"}],
        "dimensions": [{"name": "pagePath"}],
        "metrics": [
            {"name": "screenPageViews"},
            {"name": "addToCarts"},
            {"name": "ecommercePurchases"},
        ],
        "dimensionFilter": {"filter": {
            "fieldName": "pagePath",
            "stringFilter": {"matchType": "BEGINS_WITH", "value": "/products/"},
        }},
        "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}],
        "limit": 20,
    })
    produtos = []
    for r in data.get("rows", []):
        views = int(float(_mval(r, 0) or 0))
        atc   = int(float(_mval(r, 1) or 0))
        purch = int(float(_mval(r, 2) or 0))
        produtos.append({
            "path": _dval(r, 0),
            "views": views,
            "add_to_cart": atc,
            "compras": purch,
            "atc_rate_pct": round(atc / views * 100, 1) if views else 0,
        })
    return {"ok": True, "top_produtos": produtos}


@router.get("/dashboard")
def analytics_dashboard():
    """Endpoint consolidado: GA4 + Search Console em uma chamada."""
    ga4 = analytics_status()
    try:
        gsc = search_console_status()
    except HTTPException as e:
        gsc = {"ok": False, "erro": e.detail}
    try:
        rt = analytics_realtime()
    except Exception:
        rt = {"ok": False, "active_users_30min": None}
    return {
        "ok": True,
        "coletado_em": datetime.now(timezone.utc).isoformat(),
        "ga4": ga4,
        "search_console": gsc,
        "realtime": rt,
    }
