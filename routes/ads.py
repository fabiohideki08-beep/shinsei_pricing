# -*- coding: utf-8 -*-
"""
routes/ads.py — Google Ads diagnostics via SDK (google-ads>=25)
Usa google-ads.yaml (client_id, client_secret, refresh_token,
developer_token, login_customer_id).
"""
from __future__ import annotations

import logging
import os
import requests as _req_http
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import RedirectResponse, HTMLResponse

router = APIRouter(prefix="/ads", tags=["ads"])
logger = logging.getLogger(__name__)

BASE_DIR  = Path(__file__).parent.parent
YAML_PATH = BASE_DIR / "google-ads.yaml"

_GOOGLE_ADS_SCOPE = "https://www.googleapis.com/auth/adwords"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"


# ── OAuth Google Ads ──────────────────────────────────────────────────────────

@router.get("/auth")
def ads_oauth_start():
    """Inicia OAuth Google Ads — redireciona para consentimento Google."""
    import os
    client_id = os.environ.get("GOOGLE_ADS_CLIENT_ID", "")
    app_url   = os.environ.get("APP_URL", "https://shinsei-pricing.onrender.com")
    redirect  = f"{app_url}/ads/callback"
    if not client_id:
        return {"ok": False, "erro": "GOOGLE_ADS_CLIENT_ID não configurado no Render"}
    url = (
        f"{_GOOGLE_AUTH_URL}"
        f"?client_id={client_id}"
        f"&redirect_uri={redirect}"
        f"&response_type=code"
        f"&scope={_GOOGLE_ADS_SCOPE}"
        f"&access_type=offline"
        f"&prompt=consent"
    )
    return RedirectResponse(url)


@router.get("/callback")
def ads_oauth_callback(code: str = "", error: str = ""):
    """Callback OAuth Google Ads — troca code por refresh_token e salva no Render."""
    if error:
        return HTMLResponse(f"<h2>Erro OAuth Google Ads: {error}</h2>")
    if not code:
        return HTMLResponse("<h2>Código de autorização não recebido.</h2>")

    client_id     = os.environ.get("GOOGLE_ADS_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_ADS_CLIENT_SECRET", "")
    app_url       = os.environ.get("APP_URL", "https://shinsei-pricing.onrender.com")
    redirect      = f"{app_url}/ads/callback"

    r = _req_http.post(_GOOGLE_TOKEN_URL, data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
    }, timeout=15)

    if r.status_code != 200:
        return HTMLResponse(f"<h2>Erro ao trocar código: {r.text}</h2>")

    tokens = r.json()
    refresh_token = tokens.get("refresh_token", "")
    if not refresh_token:
        return HTMLResponse("<h2>refresh_token não retornado. Tente /ads/auth novamente.</h2>")

    # Salva GOOGLE_ADS_REFRESH_TOKEN no Render
    _salvar_refresh_token_render(refresh_token)

    render_ok = _salvar_refresh_token_render(refresh_token)
    status_render = "✅ Salvo no Render" if render_ok else "⚠️ Falha ao salvar no Render — copie o token abaixo"
    return HTMLResponse(f"""
    <h2>✅ Google Ads reconectado!</h2>
    <p>Status Render: {status_render}</p>
    <p><b>refresh_token</b> (copie se necessário):<br><code style='word-break:break-all'>{refresh_token}</code></p>
    <p><a href='/ads/status'>Verificar status</a></p>
    """)


def _salvar_refresh_token_render(refresh_token: str) -> bool:
    """Salva GOOGLE_ADS_REFRESH_TOKEN nas env vars do Render."""
    os.environ["GOOGLE_ADS_REFRESH_TOKEN"] = refresh_token
    try:
        from render_persistence import _patch_env_vars
        ok = _patch_env_vars({"GOOGLE_ADS_REFRESH_TOKEN": refresh_token})
        if ok:
            logger.info("GOOGLE_ADS_REFRESH_TOKEN atualizado no Render")
        else:
            logger.warning("_patch_env_vars retornou False — token NÃO persistido no Render")
        return bool(ok)
    except Exception as e:
        logger.warning("Falha ao salvar GOOGLE_ADS_REFRESH_TOKEN no Render: %s", e)
        return False


@router.post("/persistir-token")
def ads_persistir_token():
    """Força salvar o GOOGLE_ADS_REFRESH_TOKEN atual (memória) no Render env vars."""
    token = os.environ.get("GOOGLE_ADS_REFRESH_TOKEN", "")
    if not token:
        return {"ok": False, "erro": "GOOGLE_ADS_REFRESH_TOKEN ausente em memória"}
    ok = _salvar_refresh_token_render(token)
    return {"ok": ok, "token_prefix": token[:20] + "...", "msg": "Token persistido no Render" if ok else "Falha ao persistir"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_yaml_creds() -> dict:
    """Lê google-ads.yaml (formato chave: valor) e retorna dict. Retorna {} se não existir."""
    if not YAML_PATH.exists():
        return {}
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
    """Constrói GoogleAdsClient a partir de env vars (GOOGLE_ADS_*) ou google-ads.yaml como fallback."""
    import os
    from google.ads.googleads.client import GoogleAdsClient
    creds = _load_yaml_creds()
    # Env vars sobrepõem o yaml — no Render não há arquivo, só env vars
    for key in ("refresh_token", "client_id", "client_secret", "developer_token", "login_customer_id"):
        env_val = os.environ.get(f"GOOGLE_ADS_{key.upper()}")
        if env_val:
            creds[key] = env_val
    # Valida que as credenciais obrigatórias estão presentes
    missing = [k for k in ("client_id", "client_secret", "refresh_token", "developer_token") if not creds.get(k)]
    if missing:
        raise FileNotFoundError(f"Credenciais Google Ads ausentes: {missing}. Configure env vars GOOGLE_ADS_* ou google-ads.yaml")
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
        return {"ok": False, "msg": "Credenciais Google Ads ausentes (configure env vars GOOGLE_ADS_* no Render)"}
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
        return {"ok": False, "erro": "Credenciais Google Ads ausentes (configure env vars GOOGLE_ADS_* no Render)"}
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


@router.post("/campanha/{campaign_id}/maximizar-cliques")
def ads_set_maximize_clicks(campaign_id: str, cpc_max_centavos: int = 0):
    """Muda estratégia de lance da campanha para Maximizar Cliques."""
    try:
        client, customer_id = _build_client()
    except Exception as e:
        return {"ok": False, "erro": str(e)}

    try:
        from google.protobuf import field_mask_pb2
        campaign_service = client.get_service("CampaignService")
        resource_name = campaign_service.campaign_path(customer_id, campaign_id)

        op = client.get_type("CampaignOperation")
        op.update.resource_name = resource_name
        # target_spend = Maximizar Cliques
        if cpc_max_centavos:
            op.update.target_spend.cpc_bid_ceiling_micros = cpc_max_centavos * 10_000
            op.update_mask.paths.extend(["target_spend", "target_spend.cpc_bid_ceiling_micros"])
        else:
            op.update_mask.paths.append("target_spend")

        response = campaign_service.mutate_campaigns(
            customer_id=customer_id,
            operations=[op],
        )
        return {"ok": True, "resource": response.results[0].resource_name, "msg": "Lance alterado para Maximizar Cliques"}
    except Exception as e:
        return {"ok": False, "erro": str(e)}
