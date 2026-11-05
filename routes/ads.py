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
        f"&login_hint=fabiohideki08@gmail.com"
    )
    return RedirectResponse(url)


@router.get("/callback")
def ads_oauth_callback(code: str = "", error: str = "", state: str = ""):
    """Callback OAuth unificado — Google Ads e Analytics/GA4.

    state=analytics → salva GA4_REFRESH_TOKEN (fluxo de /analytics/auth)
    state=""        → salva GOOGLE_ADS_REFRESH_TOKEN (fluxo padrão /ads/auth)

    A redirect_uri /ads/callback é a única registrada no GCP OAuth client,
    por isso o analytics a reutiliza com state=analytics para diferenciar.
    """
    if error:
        return HTMLResponse(f"<h2>Erro OAuth: {error}</h2>")
    if not code:
        return HTMLResponse("<h2>Código de autorização não recebido.</h2>")

    is_analytics = (state == "analytics")

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
        msg = "/analytics/auth" if is_analytics else "/ads/auth"
        return HTMLResponse(f"<h2>refresh_token não retornado. Tente {msg} novamente.</h2>")

    if is_analytics:
        # Fluxo Analytics/GA4 — salva GA4_REFRESH_TOKEN
        os.environ["GA4_REFRESH_TOKEN"] = refresh_token
        try:
            from render_persistence import _patch_env_vars
            render_ok = bool(_patch_env_vars({"GA4_REFRESH_TOKEN": refresh_token}))
        except Exception as e:
            logger.warning("Falha ao salvar GA4_REFRESH_TOKEN no Render: %s", e)
            render_ok = False
        status_render = "✅ Salvo no Render" if render_ok else "⚠️ Falha ao salvar no Render — copie o token abaixo"
        return HTMLResponse(f"""
        <html><head><title>GA4 Conectado ✅</title></head>
        <body style="font-family:sans-serif;padding:40px;max-width:600px">
        <h1 style="color:#10b981">✅ GA4 + Search Console Conectados!</h1>
        <p>Status Render: {status_render}</p>
        <p>Teste agora:</p>
        <ul>
          <li><a href="/analytics/status">GET /analytics/status</a> — métricas GA4</li>
          <li><a href="/analytics/realtime">GET /analytics/realtime</a> — usuários ativos</li>
          <li><a href="/analytics/search-console">GET /analytics/search-console</a> — Search Console</li>
          <li><a href="/analytics/dashboard">GET /analytics/dashboard</a> — tudo junto</li>
        </ul>
        <details><summary style="cursor:pointer;color:#6b7280">Mostrar refresh_token</summary>
        <code style="word-break:break-all;font-size:12px">{refresh_token}</code></details>
        </body></html>
        """)

    # Fluxo padrão Google Ads
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

    # ── 3b. IS loss reasons por campanha (Shopping usa impression_share diferente) ──
    try:
        rows_is = _gaql(client, customer_id, """
            SELECT
                campaign.id,
                campaign.name,
                campaign.status,
                campaign.advertising_channel_type,
                metrics.impressions,
                metrics.cost_micros
            FROM campaign
            WHERE segments.date DURING LAST_30_DAYS
              AND campaign.status != 'REMOVED'
            ORDER BY metrics.cost_micros DESC
        """)
        is_analise = []
        for row in rows_is:
            tipo = row.campaign.advertising_channel_type.name if hasattr(row.campaign.advertising_channel_type, "name") else str(row.campaign.advertising_channel_type)
            is_analise.append({
                "campanha": row.campaign.name,
                "status":   row.campaign.status.name if hasattr(row.campaign.status, "name") else str(row.campaign.status),
                "tipo":     tipo,
                "impressoes": row.metrics.impressions or 0,
                "custo":    round((row.metrics.cost_micros or 0) / 1e6, 2),
            })
        resultado["is_analise"] = is_analise
    except Exception as e:
        resultado["is_analise_erro"] = str(e)

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


@router.get("/auditoria")
def ads_auditoria():
    """
    Audita todas as campanhas ENABLED contra o checklist obrigatório de 16 itens:
    budget, lances, rede, geo BR, idioma PT-BR, grupo de anúncios, negativos,
    anúncio criado, sitelinks ≥4, callouts, snippets, programação, device bids,
    UTM tracking template, merchant_id e listing groups.
    """
    try:
        client, customer_id = _build_client()
    except FileNotFoundError:
        return {"ok": False, "erro": "Credenciais Google Ads ausentes"}
    except Exception as e:
        return {"ok": False, "erro": str(e)}

    # 1. Listar campanhas ENABLED
    try:
        rows_camp = _gaql(client, customer_id, """
            SELECT
                campaign.id,
                campaign.name,
                campaign.status,
                campaign.advertising_channel_type,
                campaign.tracking_url_template,
                campaign.network_settings.target_google_search,
                campaign.network_settings.target_content_network,
                campaign.network_settings.target_partner_search_network,
                campaign.shopping_setting.merchant_id,
                campaign_budget.amount_micros
            FROM campaign
            WHERE campaign.status = 'ENABLED'
              AND campaign.advertising_channel_type = 'SHOPPING'
        """)
    except Exception as e:
        return {"ok": False, "erro": f"Erro ao listar campanhas: {e}"}

    if not rows_camp:
        return {"ok": True, "msg": "Nenhuma campanha ENABLED encontrada", "campanhas": []}

    camp_ids = [str(r.campaign.id) for r in rows_camp]
    ids_str  = ", ".join(camp_ids)
    camps_by_id = {str(r.campaign.id): r for r in rows_camp}

    # 2. Geo targets por campanha
    geo_por_camp: dict[str, list] = {cid: [] for cid in camp_ids}
    try:
        for row in _gaql(client, customer_id, f"""
            SELECT campaign.id, campaign_criterion.location.geo_target_constant,
                   campaign_criterion.negative
            FROM campaign_criterion
            WHERE campaign.id IN ({ids_str})
              AND campaign_criterion.type = 'LOCATION'
              AND campaign_criterion.negative = false
        """):
            geo_por_camp.setdefault(str(row.campaign.id), []).append(
                row.campaign_criterion.location.geo_target_constant
            )
    except Exception:
        pass

    # 3. Idiomas por campanha
    lang_por_camp: dict[str, list] = {cid: [] for cid in camp_ids}
    try:
        for row in _gaql(client, customer_id, f"""
            SELECT campaign.id, campaign_criterion.language.language_constant
            FROM campaign_criterion
            WHERE campaign.id IN ({ids_str})
              AND campaign_criterion.type = 'LANGUAGE'
        """):
            lang_por_camp.setdefault(str(row.campaign.id), []).append(
                row.campaign_criterion.language.language_constant
            )
    except Exception:
        pass

    # 4. Negativos por campanha (campaign-level)
    neg_por_camp: dict[str, int] = {cid: 0 for cid in camp_ids}
    try:
        for row in _gaql(client, customer_id, f"""
            SELECT campaign.id, campaign_criterion.keyword.text
            FROM campaign_criterion
            WHERE campaign.id IN ({ids_str})
              AND campaign_criterion.negative = true
              AND campaign_criterion.type = 'KEYWORD'
        """):
            cid = str(row.campaign.id)
            neg_por_camp[cid] = neg_por_camp.get(cid, 0) + 1
    except Exception:
        pass

    # 5. Assets/Extensions — sitelinks, callouts, snippets
    # Shopping campaigns NÃO suportam campaign-level assets; usamos customer_asset (nível de conta)
    assets_conta: dict[str, int] = {"sitelink": 0, "callout": 0, "structured_snippet": 0}
    try:
        for row in _gaql(client, customer_id, """
            SELECT customer_asset.field_type, customer_asset.status
            FROM customer_asset
            WHERE customer_asset.status = 'ENABLED'
        """):
            tipo = row.customer_asset.field_type.name if hasattr(row.customer_asset.field_type, "name") else str(row.customer_asset.field_type)
            if tipo == "SITELINK":
                assets_conta["sitelink"] += 1
            elif tipo == "CALLOUT":
                assets_conta["callout"] += 1
            elif tipo == "STRUCTURED_SNIPPET":
                assets_conta["structured_snippet"] += 1
    except Exception:
        pass
    # Replicar contagem para todas as campanhas (assets de conta valem para todas)
    assets_por_camp: dict[str, dict] = {cid: dict(assets_conta) for cid in camp_ids}

    # 6. Ad schedule por campanha
    schedule_por_camp: dict[str, int] = {cid: 0 for cid in camp_ids}
    try:
        for row in _gaql(client, customer_id, f"""
            SELECT campaign.id, campaign_criterion.type
            FROM campaign_criterion
            WHERE campaign.id IN ({ids_str})
              AND campaign_criterion.type = 'AD_SCHEDULE'
        """):
            cid = str(row.campaign.id)
            schedule_por_camp[cid] = schedule_por_camp.get(cid, 0) + 1
    except Exception:
        pass

    # 7. Device bid adjustments por campanha
    device_por_camp: dict[str, int] = {cid: 0 for cid in camp_ids}
    try:
        for row in _gaql(client, customer_id, f"""
            SELECT campaign.id, campaign_criterion.device.type, campaign_criterion.bid_modifier
            FROM campaign_criterion
            WHERE campaign.id IN ({ids_str})
              AND campaign_criterion.type = 'DEVICE'
        """):
            cid = str(row.campaign.id)
            bm  = row.campaign_criterion.bid_modifier
            if bm and bm != 1.0:
                device_por_camp[cid] = device_por_camp.get(cid, 0) + 1
    except Exception:
        pass

    # 8. AdGroupAds por campanha (anúncio Shopping criado)
    ad_por_camp: dict[str, int] = {cid: 0 for cid in camp_ids}
    try:
        for row in _gaql(client, customer_id, f"""
            SELECT campaign.id, ad_group_ad.status, ad_group_ad.ad.type
            FROM ad_group_ad
            WHERE campaign.id IN ({ids_str})
              AND ad_group_ad.status = 'ENABLED'
        """):
            cid = str(row.campaign.id)
            ad_por_camp[cid] = ad_por_camp.get(cid, 0) + 1
    except Exception:
        pass

    # 9. Ad groups por campanha
    ag_por_camp: dict[str, int] = {cid: 0 for cid in camp_ids}
    try:
        for row in _gaql(client, customer_id, f"""
            SELECT campaign.id, ad_group.status
            FROM ad_group
            WHERE campaign.id IN ({ids_str})
              AND ad_group.status = 'ENABLED'
        """):
            cid = str(row.campaign.id)
            ag_por_camp[cid] = ag_por_camp.get(cid, 0) + 1
    except Exception:
        pass

    # 10. Listing groups por campanha (produto subdividido)
    lg_por_camp: dict[str, int] = {cid: 0 for cid in camp_ids}
    try:
        for row in _gaql(client, customer_id, f"""
            SELECT campaign.id, ad_group_criterion.listing_group.type
            FROM ad_group_criterion
            WHERE campaign.id IN ({ids_str})
              AND ad_group_criterion.type = 'LISTING_GROUP'
              AND ad_group_criterion.status = 'ENABLED'
        """):
            cid = str(row.campaign.id)
            lg_por_camp[cid] = lg_por_camp.get(cid, 0) + 1
    except Exception:
        pass

    # Montar checklist por campanha
    resultado_campanhas = []
    for cid in camp_ids:
        r  = camps_by_id[cid]
        c  = r.campaign
        b  = r.campaign_budget
        nome   = c.name
        budget = (b.amount_micros or 0) / 1e6
        utm    = c.tracking_url_template or ""
        merchant = getattr(c.shopping_setting, "merchant_id", None) if hasattr(c, "shopping_setting") else None

        geos   = geo_por_camp.get(cid, [])
        langs  = lang_por_camp.get(cid, [])
        negs   = neg_por_camp.get(cid, 0)
        assets = assets_por_camp.get(cid, {})
        sched  = schedule_por_camp.get(cid, 0)
        devs   = device_por_camp.get(cid, 0)
        ads_n  = ad_por_camp.get(cid, 0)
        ags_n  = ag_por_camp.get(cid, 0)
        lgs_n  = lg_por_camp.get(cid, 0)

        geo_br    = any("2076" in g for g in geos)   # 2076 = Brasil geo constant
        sitelinks = assets.get("sitelink", 0)
        callouts  = assets.get("callout", 0)
        snippets  = assets.get("structured_snippet", 0)

        checklist = [
            {"item": "Budget configurado",         "ok": budget > 0,     "detalhe": f"R${budget:.2f}/dia"},
            {"item": "Grupo de anúncios ativo",    "ok": ags_n > 0,      "detalhe": f"{ags_n} grupo(s)"},
            {"item": "Anúncio Shopping criado",    "ok": ads_n > 0,      "detalhe": f"{ads_n} anúncio(s)"},
            {"item": "Listing groups configurados","ok": lgs_n > 0,      "detalhe": f"{lgs_n} listing group(s)"},
            {"item": "Merchant Center vinculado",  "ok": bool(merchant), "detalhe": f"Merchant ID: {merchant or 'N/A'}"},
            {"item": "Geo-targeting Brasil",       "ok": geo_br,         "detalhe": f"{len(geos)} geo(s)" if geos else "Nenhum — serve todos os países"},
            {"item": "Negativos (keywords)",       "ok": negs >= 5,      "detalhe": f"{negs} negativas" + (" ⚠️ recomendado ≥5" if negs < 5 else "")},
            {"item": "Sitelinks (≥4)",             "ok": sitelinks >= 4, "detalhe": f"{sitelinks} sitelink(s)"},
            {"item": "Callouts",                   "ok": callouts >= 2,  "detalhe": f"{callouts} callout(s)"},
            {"item": "Snippets estruturados",      "ok": snippets >= 1,  "detalhe": f"{snippets} snippet(s)"},
            {"item": "Programação de anúncios",    "ok": sched > 0,      "detalhe": f"{sched} horário(s)" if sched else "Sem programação — veiculando 24/7"},
            {"item": "Ajuste de lance por device", "ok": devs > 0,       "detalhe": f"{devs} ajuste(s)" if devs else "Sem ajuste (mobile/tablet)"},
            {"item": "UTM tracking template",      "ok": bool(utm),      "detalhe": utm[:60] + "..." if len(utm) > 60 else (utm or "Não configurado")},
        ]

        ok_count    = sum(1 for x in checklist if x["ok"])
        total       = len(checklist)
        score_pct   = round(ok_count / total * 100)
        pendencias  = [x["item"] for x in checklist if not x["ok"]]

        resultado_campanhas.append({
            "id":           cid,
            "nome":         nome,
            "score":        f"{score_pct}%  ({ok_count}/{total})",
            "score_pct":    score_pct,
            "pendencias":   pendencias,
            "checklist":    checklist,
        })

    score_total = sum(c["score_pct"] for c in resultado_campanhas) // max(len(resultado_campanhas), 1)
    return {
        "ok":             True,
        "auditado_em":    datetime.utcnow().isoformat(),
        "total_campanhas": len(resultado_campanhas),
        "score_medio":    f"{score_total}%",
        "campanhas":      resultado_campanhas,
    }


@router.post("/campanha/{campaign_id}/completar")
def ads_completar_campanha(campaign_id: str, payload: dict = {}):
    """
    Aplica automaticamente os itens faltantes do checklist na campanha:
    geo BR, idioma PT-BR, negativos, UTM template, device bid, ad schedule,
    sitelinks (4), callouts (2), snippets (1).

    payload (todos opcionais):
      dry_run: bool          — retorna plano sem executar (default False)
      store_url: str         — URL base da loja (default https://shinsei.com.br)
      skip: list[str]        — itens a pular (ex: ["ad_schedule"])
    """
    try:
        client, customer_id = _build_client()
    except Exception as e:
        return {"ok": False, "erro": str(e)}

    dry_run   = payload.get("dry_run", False)
    store_url = payload.get("store_url", "https://shinsei.com.br").rstrip("/")
    skip      = set(payload.get("skip", []))

    # Obter resource_name da campanha
    camp_rows = _gaql(client, customer_id, f"""
        SELECT campaign.id, campaign.name, campaign.tracking_url_template
        FROM campaign WHERE campaign.id = {campaign_id} LIMIT 1
    """)
    if not camp_rows:
        return {"ok": False, "erro": "Campanha não encontrada"}
    camp_rn   = client.get_service("CampaignService").campaign_path(customer_id, campaign_id)
    camp_nome = camp_rows[0].campaign.name
    utm_atual = camp_rows[0].campaign.tracking_url_template or ""

    aplicados: list[str] = []
    erros:    list[str] = []

    # ── 1. Geo BR (geoTargetConstant/2076 = Brasil) ──────────────────────────
    if "geo" not in skip:
        try:
            geo_existentes = _gaql(client, customer_id, f"""
                SELECT campaign_criterion.location.geo_target_constant
                FROM campaign_criterion
                WHERE campaign.id = {campaign_id}
                  AND campaign_criterion.type = 'LOCATION'
                  AND campaign_criterion.negative = false
            """)
            if not geo_existentes:
                if not dry_run:
                    cc_svc = client.get_service("CampaignCriterionService")
                    op = client.get_type("CampaignCriterionOperation")
                    op.create.campaign = camp_rn
                    op.create.location.geo_target_constant = "geoTargetConstants/2076"
                    cc_svc.mutate_campaign_criteria(customer_id=customer_id, operations=[op])
                aplicados.append("geo_br")
        except Exception as e:
            erros.append(f"geo_br: {e}")

    # ── 2. UTM tracking template ──────────────────────────────────────────────
    if "utm" not in skip and not utm_atual:
        try:
            utm = "{lpurl}?utm_source=google&utm_medium=cpc&utm_campaign={_utmz}&utm_term={keyword}&utm_content={adgroup.name}"
            if not dry_run:
                from google.protobuf import field_mask_pb2
                camp_svc = client.get_service("CampaignService")
                op = client.get_type("CampaignOperation")
                op.update.resource_name = camp_rn
                op.update.tracking_url_template = utm
                op.update_mask.paths.append("tracking_url_template")
                camp_svc.mutate_campaigns(customer_id=customer_id, operations=[op])
            aplicados.append("utm_template")
        except Exception as e:
            erros.append(f"utm: {e}")

    # ── 4. Negativos (10 keywords genéricas para cosméticos) ─────────────────
    if "negativos" not in skip:
        try:
            neg_exist = _gaql(client, customer_id, f"""
                SELECT campaign_criterion.keyword.text
                FROM campaign_criterion
                WHERE campaign.id = {campaign_id}
                  AND campaign_criterion.negative = true
                  AND campaign_criterion.type = 'KEYWORD'
            """)
            if len(neg_exist) < 5:
                negativos = [
                    "grátis", "gratuito", "free", "como fazer",
                    "tutorial", "diy", "receita caseira", "remédio",
                    "doença", "tratamento médico",
                ]
                if not dry_run:
                    cc_svc = client.get_service("CampaignCriterionService")
                    ops = []
                    for kw in negativos:
                        op = client.get_type("CampaignCriterionOperation")
                        op.create.campaign = camp_rn
                        op.create.negative = True
                        op.create.keyword.text = kw
                        op.create.keyword.match_type = client.enums.KeywordMatchTypeEnum.BROAD
                        ops.append(op)
                    cc_svc.mutate_campaign_criteria(customer_id=customer_id, operations=ops)
                aplicados.append(f"negativos_{len(negativos)}")
        except Exception as e:
            erros.append(f"negativos: {e}")

    # ── 5. Device bid adjustment (mobile -10%) ────────────────────────────────
    if "device" not in skip:
        try:
            dev_rows = _gaql(client, customer_id, f"""
                SELECT campaign_criterion.resource_name, campaign_criterion.device.type, campaign_criterion.bid_modifier
                FROM campaign_criterion
                WHERE campaign.id = {campaign_id}
                  AND campaign_criterion.type = 'DEVICE'
            """)
            # Mapear device type → (resource_name, bid_modifier)
            dev_map: dict = {}
            for r in dev_rows:
                dt = r.campaign_criterion.device.type_.name if hasattr(r.campaign_criterion.device.type_, "name") else str(r.campaign_criterion.device.type_)
                dev_map[dt] = (r.campaign_criterion.resource_name, r.campaign_criterion.bid_modifier)
            # Verificar se mobile/tablet já têm ajuste real (!=1.0)
            needs_device = any(
                dev_map.get(dt, (None, 1.0))[1] in (None, 0.0, 1.0)
                for dt in ("MOBILE", "TABLET")
            )
            if needs_device:
                if not dry_run:
                    cc_svc = client.get_service("CampaignCriterionService")
                    for dev_type, modifier in [("MOBILE", 0.9), ("TABLET", 0.8)]:
                        existing = dev_map.get(dev_type)
                        op = client.get_type("CampaignCriterionOperation")
                        if existing and existing[0]:
                            # Atualizar criterion existente
                            op.update.resource_name = existing[0]
                            op.update.bid_modifier = modifier
                            op.update_mask.paths.append("bid_modifier")
                        else:
                            # Criar novo
                            op.create.campaign = camp_rn
                            op.create.device.type_ = client.enums.DeviceEnum[dev_type]
                            op.create.bid_modifier = modifier
                        cc_svc.mutate_campaign_criteria(customer_id=customer_id, operations=[op])
                aplicados.append("device_bids_mobile_tablet")
        except Exception as e:
            erros.append(f"device: {e}")

    # ── 6. Ad schedule (Seg-Dom 06h-23h) ─────────────────────────────────────
    if "ad_schedule" not in skip:
        try:
            sched_exist = _gaql(client, customer_id, f"""
                SELECT campaign_criterion.type
                FROM campaign_criterion
                WHERE campaign.id = {campaign_id}
                  AND campaign_criterion.type = 'AD_SCHEDULE'
            """)
            if not sched_exist:
                if not dry_run:
                    cc_svc = client.get_service("CampaignCriterionService")
                    ops = []
                    for day in range(2, 9):  # MONDAY=2 ... SUNDAY=8
                        op = client.get_type("CampaignCriterionOperation")
                        op.create.campaign = camp_rn
                        op.create.ad_schedule.day_of_week = client.enums.DayOfWeekEnum(day)
                        op.create.ad_schedule.start_hour = 6
                        op.create.ad_schedule.start_minute = client.enums.MinuteOfHourEnum.ZERO
                        op.create.ad_schedule.end_hour = 23
                        op.create.ad_schedule.end_minute = client.enums.MinuteOfHourEnum.ZERO
                        ops.append(op)
                    cc_svc.mutate_campaign_criteria(customer_id=customer_id, operations=ops)
                aplicados.append("ad_schedule_6h_23h_seg_dom")
        except Exception as e:
            erros.append(f"ad_schedule: {e}")

    # ── 7-9. Assets: sitelinks, callouts, snippets via REST v24 ─────────────
    # Usamos REST (igual a criar-subdivisions) pois o gRPC SDK tem instabilidades
    # no mutate_assets com use_proto_plus=True
    if not ({"sitelinks", "callouts", "snippets"} <= skip):
        # Checar assets existentes no nível de conta (Shopping não suporta campaign-level assets)
        try:
            existing_assets_rows = _gaql(client, customer_id, """
                SELECT customer_asset.field_type FROM customer_asset
                WHERE customer_asset.status = 'ENABLED'
            """)
            existing_ft: dict[str, int] = {}
            for row in existing_assets_rows:
                ft = row.customer_asset.field_type.name if hasattr(row.customer_asset.field_type, "name") else str(row.customer_asset.field_type)
                existing_ft[ft] = existing_ft.get(ft, 0) + 1
        except Exception as e:
            existing_ft = {}
            erros.append(f"assets_check: {e}")

        # Obter access_token REST
        try:
            tok = _req_http.post("https://oauth2.googleapis.com/token", data={
                "client_id":     os.environ.get("GOOGLE_ADS_CLIENT_ID"),
                "client_secret": os.environ.get("GOOGLE_ADS_CLIENT_SECRET"),
                "refresh_token": os.environ.get("GOOGLE_ADS_REFRESH_TOKEN"),
                "grant_type":    "refresh_token",
            }, timeout=10)
            access_token = tok.json()["access_token"]
            dev_token    = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "")
            rest_hdrs    = {
                "Authorization":     f"Bearer {access_token}",
                "developer-token":   dev_token,
                "login-customer-id": str(customer_id),
                "Content-Type":      "application/json",
            }
            asset_url      = f"https://googleads.googleapis.com/v24/customers/{customer_id}/assets:mutate"
            # Shopping campaigns não suportam campaign-level assets; usar customerAssets (nível de conta)
            cust_asset_url = f"https://googleads.googleapis.com/v24/customers/{customer_id}/customerAssets:mutate"
        except Exception as e:
            erros.append(f"rest_token: {e}")
            rest_hdrs = None

        def _create_asset_rest(op_body: dict) -> str | None:
            """Cria asset via REST e retorna resource_name, ou None se falhar."""
            if dry_run:
                return "dry_run_asset"
            resp = _req_http.post(asset_url, headers=rest_hdrs, json={"operations": [{"create": op_body}]}, timeout=20)
            if not resp.ok:
                raise ValueError(f"asset create HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json()["results"][0]["resourceName"]

        def _link_asset_rest(asset_rn: str, field_type: str):
            if dry_run:
                return
            # Shopping campaigns requerem customer-level assets (não campaign-level)
            resp = _req_http.post(cust_asset_url, headers=rest_hdrs, json={"operations": [{"create": {
                "asset": asset_rn, "fieldType": field_type
            }}]}, timeout=20)
            if not resp.ok:
                raise ValueError(f"customer_asset link HTTP {resp.status_code}: {resp.text[:2000]}")

        # ── Sitelinks ─────────────────────────────────────────────────────────
        if rest_hdrs and "sitelinks" not in skip and existing_ft.get("SITELINK", 0) < 4:
            try:
                sitelinks_data = [
                    ("Coloracoes Schwarzkopf", f"{store_url}/collections/schwarzkopf-igora", "Igora Royal e Zero Amm", "Alta cobertura brilho intenso"),
                    ("Coloracoes Alfaparf",    f"{store_url}/collections/alfaparf",           "Evolution e Semi Di Lino", "Formula italiana performance"),
                    ("Frete Gratis SP",        f"{store_url}/frete-gratis",                   "Entrega gratis na Grande SP", "Pedidos acima de R 29,90"),
                    ("Mais Vendidos",          f"{store_url}/collections/mais-vendidos",       "Favoritos dos clientes", "Avaliacoes 5 estrelas"),
                ]
                for text, url_sl, desc1, desc2 in sitelinks_data:
                    rn = _create_asset_rest({
                        "name": f"SL {text}",
                        "finalUrls": [url_sl],
                        "sitelinkAsset": {"linkText": text, "description1": desc1, "description2": desc2},
                    })
                    if rn:
                        _link_asset_rest(rn, "SITELINK")
                aplicados.append("sitelinks_4")
            except Exception as e:
                erros.append(f"sitelinks: {e}")

        # ── Callouts ──────────────────────────────────────────────────────────
        if rest_hdrs and "callouts" not in skip and existing_ft.get("CALLOUT", 0) < 2:
            try:
                for text in ["Produtos Originais", "Frete Gratis Grande SP", "Entrega Rapida", "Maior Variedade"]:
                    rn = _create_asset_rest({"name": f"CO {text}", "calloutAsset": {"calloutText": text}})
                    if rn:
                        _link_asset_rest(rn, "CALLOUT")
                aplicados.append("callouts_4")
            except Exception as e:
                erros.append(f"callouts: {e}")

        # ── Snippets ──────────────────────────────────────────────────────────
        if rest_hdrs and "snippets" not in skip and existing_ft.get("STRUCTURED_SNIPPET", 0) == 0:
            try:
                rn = _create_asset_rest({"name": "Snippet Marcas", "structuredSnippetAsset": {
                    "header": "Marcas", "values": ["Schwarzkopf", "Alfaparf", "Igora", "Inoar", "Skala"]
                }})
                if rn:
                    _link_asset_rest(rn, "STRUCTURED_SNIPPET")
                aplicados.append("structured_snippet_marcas")
            except Exception as e:
                erros.append(f"snippets: {e}")

    return {
        "ok":        len(erros) == 0,
        "campanha":  camp_nome,
        "dry_run":   dry_run,
        "aplicados": aplicados,
        "erros":     erros,
        "msg":       f"{len(aplicados)} item(ns) aplicado(s), {len(erros)} erro(s)",
    }


@router.get("/campanha/{campaign_id}/listing-groups")
def ads_listing_groups(campaign_id: str):
    """Retorna listing groups e produtos elegíveis de uma campanha Shopping."""
    try:
        client, customer_id = _build_client()
    except Exception as e:
        return {"ok": False, "erro": str(e)}

    try:
        # Listing groups — campaign.id obrigatório no SELECT quando usado no WHERE
        rows = _gaql(client, customer_id, f"""
            SELECT
                campaign.id,
                ad_group_criterion.resource_name,
                ad_group_criterion.listing_group.type,
                ad_group_criterion.listing_group.case_value.product_channel.channel,
                ad_group_criterion.status,
                ad_group_criterion.cpc_bid_micros
            FROM ad_group_criterion
            WHERE campaign.id = {campaign_id}
              AND ad_group_criterion.type = 'LISTING_GROUP'
            LIMIT 50
        """)

        grupos = []
        for row in rows:
            c = row.ad_group_criterion
            grupos.append({
                "tipo":          c.listing_group.type.name if hasattr(c.listing_group.type, "name") else str(c.listing_group.type),
                "status":        c.status.name if hasattr(c.status, "name") else str(c.status),
                "lance":         round((c.cpc_bid_micros or 0) / 1e6, 2),
                "resource_name": c.resource_name,
                "criterion_id":  c.criterion_id,
                "parent":        c.listing_group.parent_ad_group_criterion,
            })

        # Produtos Shopping desta campanha (últimos 30 dias)
        rows2 = _gaql(client, customer_id, f"""
            SELECT
                campaign.id,
                segments.product_title,
                segments.product_item_id,
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros
            FROM shopping_performance_view
            WHERE campaign.id = {campaign_id}
              AND segments.date DURING LAST_30_DAYS
            ORDER BY metrics.impressions DESC
            LIMIT 20
        """)
        produtos = [
            {
                "titulo":  row.segments.product_title or row.segments.product_item_id or "?",
                "item_id": row.segments.product_item_id,
                "impr":    row.metrics.impressions or 0,
                "cliques": row.metrics.clicks or 0,
                "custo":   round((row.metrics.cost_micros or 0) / 1e6, 2),
            }
            for row in rows2
        ]

        return {"ok": True, "listing_groups": grupos, "produtos": produtos}
    except Exception as e:
        return {"ok": False, "erro": str(e)}


@router.post("/campanha/{campaign_id}/criar-subdivisions")
def ads_criar_subdivisions(campaign_id: str, payload: dict = {}):
    """
    Substitui o listing group raiz UNIT por uma estrutura subdividida por product_type (l2).
    Usado para criar bids diferenciados por linha (Zero Amm, Evolution, etc.) na campanha Stars.

    Payload (opcional):
      bids: { "Schwarzkopf": 1.00, "Alfaparf Milano": 0.80, "__outros__": 0.10 }
      ad_group_id: int  (se omitido, busca o primeiro ad group da campanha)
      dry_run: bool     (se True, retorna o plano sem executar)
      dimensao: "brand" (default) | "product_type_l1"

    Usa brand como dimensão (mais estável): Schwarzkopf → Zero Amm R$1,00,
    Alfaparf Milano → Evolution R$0,80, outros → R$0,10.
    """
    try:
        client, customer_id = _build_client()
    except Exception as e:
        return {"ok": False, "erro": str(e)}

    try:
        bids_config = payload.get("bids", {
            "Igora Zero Amm":     1.00,
            "Alfaparf Evolution": 0.80,
            "__outros__":         0.10,
        })
        dry_run = payload.get("dry_run", False)

        # 1) Descobrir ad_group_id e criterion raiz atual
        ad_group_id = payload.get("ad_group_id")
        if not ad_group_id:
            rows = _gaql(client, customer_id, f"""
                SELECT ad_group.id
                FROM ad_group
                WHERE campaign.id = {campaign_id}
                LIMIT 1
            """)
            if not rows:
                return {"ok": False, "erro": "Nenhum ad group encontrado na campanha"}
            ad_group_id = rows[0].ad_group.id

        # 2) Buscar listing groups atuais
        lg_rows = _gaql(client, customer_id, f"""
            SELECT
                ad_group_criterion.criterion_id,
                ad_group_criterion.resource_name,
                ad_group_criterion.listing_group.type,
                ad_group_criterion.listing_group.parent_ad_group_criterion,
                ad_group_criterion.listing_group.case_value.product_type.level,
                ad_group_criterion.listing_group.case_value.product_type.value,
                ad_group_criterion.status,
                ad_group_criterion.cpc_bid_micros
            FROM ad_group_criterion
            WHERE ad_group.id = {ad_group_id}
              AND ad_group_criterion.type = 'LISTING_GROUP'
        """)

        if not lg_rows:
            return {"ok": False, "erro": "Nenhum listing group encontrado no ad group"}

        # 3) Encontrar a raiz (UNIT sem parent = "Everything")
        root_rn = None
        root_crit_id = None
        for row in lg_rows:
            c = row.ad_group_criterion
            lg = c.listing_group
            parent = str(lg.parent_ad_group_criterion) if lg.parent_ad_group_criterion else ""
            if not parent or parent.endswith("~0"):
                root_rn = c.resource_name
                root_crit_id = c.criterion_id
                break
        if not root_rn:
            # Fallback: menor criterion_id é a raiz
            root_rn = lg_rows[0].ad_group_criterion.resource_name
            root_crit_id = lg_rows[0].ad_group_criterion.criterion_id

        plano = {
            "ad_group_id": ad_group_id,
            "root_criterion_id": root_crit_id,
            "root_resource_name": root_rn,
            "total_lg_atuais": len(lg_rows),
            "bids_a_criar": bids_config,
        }
        if dry_run:
            return {"ok": True, "dry_run": True, "plano": plano}

        # 4) Obter access_token via refresh para chamar REST API diretamente.
        # O SDK proto-plus serializa product_brand.value="" como TOO_SHORT;
        # via REST, "productBrand": {} (objeto vazio) é aceito como "others" case.
        token_resp = _req_http.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id":     os.environ.get("GOOGLE_ADS_CLIENT_ID"),
                "client_secret": os.environ.get("GOOGLE_ADS_CLIENT_SECRET"),
                "refresh_token": os.environ.get("GOOGLE_ADS_REFRESH_TOKEN"),
                "grant_type":    "refresh_token",
            },
            timeout=10,
        )
        if not token_resp.ok:
            return {"ok": False, "erro": f"Falha ao obter access_token: {token_resp.text}"}
        access_token = token_resp.json()["access_token"]
        dev_token    = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "")

        outros_bid   = bids_config.get("__outros__", 0.10)
        linhas_criadas = []

        def _tmp_rn(tmp_id):
            return f"customers/{customer_id}/adGroupCriteria/{ad_group_id}~{tmp_id}"

        ad_group_rn = f"customers/{customer_id}/adGroups/{ad_group_id}"

        # 5) Montar operações JSON (REST):
        # SUBDIVISION raiz [tmp=-1]
        # ├── UNIT brand "Schwarzkopf"    [tmp=-2]
        # ├── UNIT brand "Alfaparf Milano" [tmp=-3]
        # └── UNIT others (productBrand:{}) [tmp=-4]  ← objeto vazio = "everything else"
        operations = []

        # a) Removes
        for row in lg_rows:
            operations.append({"remove": row.ad_group_criterion.resource_name})

        # b) SUBDIVISION raiz
        root_rn = _tmp_rn(-1)
        operations.append({
            "create": {
                "resourceName": root_rn,
                "adGroup":      ad_group_rn,
                "status":       "ENABLED",
                "listingGroup": {"type": "SUBDIVISION"},
            }
        })

        # c) UNIT por brand
        tmp_id = -2
        for marca, bid_reais in bids_config.items():
            if marca == "__outros__":
                continue
            operations.append({
                "create": {
                    "resourceName":  _tmp_rn(tmp_id),
                    "adGroup":       ad_group_rn,
                    "status":        "ENABLED",
                    "cpcBidMicros":  str(int(bid_reais * 1_000_000)),
                    "listingGroup": {
                        "type":                     "UNIT",
                        "parentAdGroupCriterion":   root_rn,
                        "caseValue": {"productBrand": {"value": marca}},
                    },
                }
            })
            linhas_criadas.append({"marca": marca, "bid": bid_reais})
            tmp_id -= 1

        # d) UNIT "others" — productBrand:{} (sem value) = "everything else"
        operations.append({
            "create": {
                "resourceName":  _tmp_rn(tmp_id),
                "adGroup":       ad_group_rn,
                "status":        "ENABLED",
                "cpcBidMicros":  str(int(outros_bid * 1_000_000)),
                "listingGroup": {
                    "type":                   "UNIT",
                    "parentAdGroupCriterion": root_rn,
                    "caseValue":              {"productBrand": {}},
                },
            }
        })

        # 6) Chamar REST API
        url = f"https://googleads.googleapis.com/v24/customers/{customer_id}/adGroupCriteria:mutate"
        headers_rest = {
            "Authorization":    f"Bearer {access_token}",
            "developer-token":  dev_token,
            "login-customer-id": str(customer_id),
            "Content-Type":     "application/json",
        }
        resp = _req_http.post(url, headers=headers_rest, json={"operations": operations}, timeout=30)
        if not resp.ok:
            return {"ok": False, "erro": resp.text, "status_code": resp.status_code}

        result = resp.json()
        criados = len(result.get("results", []))

        return {
            "ok":          True,
            "dimensao":    "brand",
            "removidos":   len(lg_rows),
            "criados":     criados,
            "linhas":      linhas_criadas,
            "outros_bid":  outros_bid,
            "results":     result.get("results", []),
        }
    except Exception as e:
        import traceback
        return {"ok": False, "erro": str(e), "trace": traceback.format_exc()[-1000:]}


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
            op.update_mask.paths.append("target_spend.cpc_bid_ceiling_micros")
        else:
            op.update_mask.paths.append("target_spend")

        response = campaign_service.mutate_campaigns(
            customer_id=customer_id,
            operations=[op],
        )
        return {"ok": True, "resource": response.results[0].resource_name, "msg": "Lance alterado para Maximizar Cliques"}
    except Exception as e:
        return {"ok": False, "erro": str(e)}


@router.post("/campanha/{campaign_id}/restringir-itens")
def ads_restringir_itens(campaign_id: str, payload: dict = {}):
    """
    Substitui o listing group da campanha por uma estrutura que serve APENAS
    os item_ids informados. Tudo mais recebe bid mínimo (R$0,01).

    Payload:
      item_ids: ["shopify_zz_10781580624177_55923953631537", ...]
      bid_reais: 1.00        (bid para os itens permitidos, default 1.00)
      outros_bid_reais: 0.01 (bid para o resto, default 0.01)
      ad_group_id: int       (opcional — busca automaticamente)
      dry_run: bool
    """
    try:
        client, customer_id = _build_client()
    except Exception as e:
        return {"ok": False, "erro": str(e)}

    try:
        item_ids     = payload.get("item_ids", [])
        bid_reais    = float(payload.get("bid_reais", 1.00))
        outros_bid   = float(payload.get("outros_bid_reais", 0.01))
        dry_run      = payload.get("dry_run", False)

        if not item_ids:
            return {"ok": False, "erro": "item_ids obrigatório"}

        ad_group_id = payload.get("ad_group_id")
        if not ad_group_id:
            rows = _gaql(client, customer_id, f"""
                SELECT ad_group.id FROM ad_group
                WHERE campaign.id = {campaign_id} LIMIT 1
            """)
            if not rows:
                return {"ok": False, "erro": "Nenhum ad group encontrado"}
            ad_group_id = rows[0].ad_group.id

        lg_rows = _gaql(client, customer_id, f"""
            SELECT ad_group_criterion.criterion_id, ad_group_criterion.resource_name,
                   ad_group_criterion.listing_group.type, ad_group.id
            FROM ad_group_criterion
            WHERE campaign.id = {campaign_id}
              AND ad_group.id = {ad_group_id}
              AND ad_group_criterion.type = 'LISTING_GROUP'
        """)

        if dry_run:
            return {
                "ok": True, "dry_run": True,
                "ad_group_id": ad_group_id,
                "existentes": len(lg_rows),
                "novos_itens": len(item_ids),
                "rns": [r.ad_group_criterion.resource_name for r in lg_rows],
            }

        # Obter access_token via REST
        token_resp = _req_http.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id":     os.environ.get("GOOGLE_ADS_CLIENT_ID"),
                "client_secret": os.environ.get("GOOGLE_ADS_CLIENT_SECRET"),
                "refresh_token": os.environ.get("GOOGLE_ADS_REFRESH_TOKEN"),
                "grant_type":    "refresh_token",
            },
            timeout=10,
        )
        access_token = token_resp.json()["access_token"]
        dev_token    = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "")

        ad_group_rn = f"customers/{customer_id}/adGroups/{ad_group_id}"

        def _rn(tmp_id):
            return f"customers/{customer_id}/adGroupCriteria/{ad_group_id}~{tmp_id}"

        # Remover folhas (UNIT) antes de pais (SUBDIVISION) — evita cascade-delete errôneo
        def _lg_type_name(row):
            t = row.ad_group_criterion.listing_group.type
            return t.name if hasattr(t, "name") else str(t)

        units_first = sorted(lg_rows, key=lambda r: 0 if _lg_type_name(r) == "UNIT" else 1)

        operations = []
        for row in units_first:
            operations.append({"remove": row.ad_group_criterion.resource_name})

        root_rn = _rn(-1)
        operations.append({
            "create": {
                "resourceName": root_rn,
                "adGroup": ad_group_rn,
                "status": "ENABLED",
                "listingGroup": {"type": "SUBDIVISION"},
            }
        })

        tmp_id = -2
        for item_id in item_ids:
            operations.append({
                "create": {
                    "resourceName": _rn(tmp_id),
                    "adGroup": ad_group_rn,
                    "status": "ENABLED",
                    "cpcBidMicros": str(int(bid_reais * 1_000_000)),
                    "listingGroup": {
                        "type": "UNIT",
                        "parentAdGroupCriterion": root_rn,
                        "caseValue": {"productItemId": {"value": item_id}},
                    },
                }
            })
            tmp_id -= 1

        operations.append({
            "create": {
                "resourceName": _rn(tmp_id),
                "adGroup": ad_group_rn,
                "status": "ENABLED",
                "cpcBidMicros": str(int(outros_bid * 1_000_000)),
                "listingGroup": {
                    "type": "UNIT",
                    "parentAdGroupCriterion": root_rn,
                    "caseValue": {"productItemId": {}},
                },
            }
        })

        url = f"https://googleads.googleapis.com/v24/customers/{customer_id}/adGroupCriteria:mutate"
        headers_rest = {
            "Authorization":     f"Bearer {access_token}",
            "developer-token":   dev_token,
            "login-customer-id": str(customer_id),
            "Content-Type":      "application/json",
        }
        resp = _req_http.post(url, headers=headers_rest, json={"operations": operations}, timeout=60)
        if not resp.ok:
            return {"ok": False, "erro": resp.text, "status_code": resp.status_code}

        result = resp.json()
        return {
            "ok": True,
            "removidos": len(lg_rows),
            "criados": len(result.get("results", [])),
            "itens_bid": len(item_ids),
            "bid_reais": bid_reais,
            "outros_bid": outros_bid,
        }
    except Exception as e:
        return {"ok": False, "erro": str(e)}


@router.post("/campanha/{campaign_id}/budget")
def ads_alterar_budget(campaign_id: str, payload: dict = {}):
    """Altera o budget diário da campanha. payload: {budget_diario_reais: 30}"""
    try:
        client, customer_id = _build_client()
    except Exception as e:
        return {"ok": False, "erro": str(e)}
    try:
        novo_budget_reais = float(payload.get("budget_diario_reais", 0))
        if novo_budget_reais <= 0:
            return {"ok": False, "erro": "budget_diario_reais deve ser > 0"}

        # Buscar budget_id atual da campanha via GAQL
        rows = _gaql(client, customer_id, f"""
            SELECT campaign.id, campaign_budget.id, campaign_budget.amount_micros
            FROM campaign
            WHERE campaign.id = {campaign_id}
            LIMIT 1
        """)
        if not rows:
            return {"ok": False, "erro": "Campanha não encontrada"}

        budget_id = rows[0].campaign_budget.id
        budget_svc = client.get_service("CampaignBudgetService")
        budget_rn  = budget_svc.campaign_budget_path(customer_id, budget_id)

        op = client.get_type("CampaignBudgetOperation")
        op.update.resource_name   = budget_rn
        op.update.amount_micros   = int(novo_budget_reais * 1_000_000)
        op.update_mask.paths.append("amount_micros")

        resp = budget_svc.mutate_campaign_budgets(customer_id=customer_id, operations=[op])
        return {
            "ok": True,
            "budget_id": budget_id,
            "novo_budget_reais": novo_budget_reais,
            "resource": resp.results[0].resource_name,
        }
    except Exception as e:
        return {"ok": False, "erro": str(e)}


@router.post("/campanha/{campaign_id}/ativar")
def ads_ativar_campanha(campaign_id: str, payload: dict = {}):
    """Ativa (ENABLED) ou pausa (PAUSED) a campanha. payload: {status: 'ENABLED'|'PAUSED'}"""
    try:
        client, customer_id = _build_client()
    except Exception as e:
        return {"ok": False, "erro": str(e)}
    try:
        novo_status = payload.get("status", "ENABLED").upper()
        campaign_service = client.get_service("CampaignService")
        resource_name = campaign_service.campaign_path(customer_id, campaign_id)
        op = client.get_type("CampaignOperation")
        op.update.resource_name = resource_name
        op.update.status = client.enums.CampaignStatusEnum[novo_status]
        op.update_mask.paths.append("status")
        response = campaign_service.mutate_campaigns(customer_id=customer_id, operations=[op])
        return {"ok": True, "status": novo_status, "resource": response.results[0].resource_name}
    except Exception as e:
        return {"ok": False, "erro": str(e)}


@router.post("/campanha/criar-shopping")
def ads_criar_campanha_shopping(payload: dict = {}):
    """
    Cria uma campanha Shopping Standard.
    payload: {
      nome: str,                     # ex: "Igora — Kits Coloração + Ox"
      budget_diario_reais: float,    # ex: 25
      cpc_bid_reais: float,          # ex: 0.80
      merchant_id: int,              # default 5071388981 (GMC Shinsei)
      product_type_l1_filter: str,   # ex: "Igora" — se informado, cria ListingGroup filtrando por product_type nível 1
    }
    Retorna: {ok, campaign_id, campaign_resource}
    A campanha é criada PAUSADA. Ativar com POST /ads/campanha/{id}/ativar.
    """
    import uuid as _uuid
    try:
        client, customer_id = _build_client()
    except Exception as e:
        return {"ok": False, "erro": str(e)}
    try:
        nome               = payload.get("nome", f"Nova Campanha {_uuid.uuid4().hex[:6]}")
        budget_reais       = float(payload.get("budget_diario_reais", 20))
        cpc_micros         = int(float(payload.get("cpc_bid_reais", 0.80)) * 1_000_000)
        merchant_id        = int(payload.get("merchant_id", 5071388981))
        pt_filter          = payload.get("product_type_l1_filter", "")

        # 1. Budget
        budget_svc = client.get_service("CampaignBudgetService")
        op_b = client.get_type("CampaignBudgetOperation")
        b = op_b.create
        b.name = f"Budget {nome} {_uuid.uuid4().hex[:6]}"
        b.amount_micros = int(budget_reais * 1_000_000)
        b.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
        r_b = budget_svc.mutate_campaign_budgets(customer_id=customer_id, operations=[op_b])
        budget_rn = r_b.results[0].resource_name

        # 2. Campaign
        camp_svc = client.get_service("CampaignService")
        op_c = client.get_type("CampaignOperation")
        c = op_c.create
        c.name = nome
        c.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SHOPPING
        c.status = client.enums.CampaignStatusEnum.PAUSED
        c.campaign_budget = budget_rn
        c.shopping_setting.merchant_id = merchant_id
        c.shopping_setting.campaign_priority = 0
        c.shopping_setting.enable_local = False
        c.manual_cpc.enhanced_cpc_enabled = False
        r_c = camp_svc.mutate_campaigns(customer_id=customer_id, operations=[op_c])
        camp_rn = r_c.results[0].resource_name
        camp_id = camp_rn.split("/")[-1]

        # 3. AdGroup
        ag_svc = client.get_service("AdGroupService")
        op_ag = client.get_type("AdGroupOperation")
        ag = op_ag.create
        ag.name = f"{nome} — Grupo"
        ag.campaign = camp_rn
        ag.status = client.enums.AdGroupStatusEnum.ENABLED
        ag.cpc_bid_micros = cpc_micros
        r_ag = ag_svc.mutate_ad_groups(customer_id=customer_id, operations=[op_ag])
        ag_rn = r_ag.results[0].resource_name

        # 4. AdGroupAd (Shopping Product Ad)
        agad_svc = client.get_service("AdGroupAdService")
        op_agad = client.get_type("AdGroupAdOperation")
        agad = op_agad.create
        agad.ad_group = ag_rn
        agad.status = client.enums.AdGroupAdStatusEnum.ENABLED
        agad.ad.shopping_product_ad.CopyFrom(client.get_type("ShoppingProductAdInfo")())
        agad_svc.mutate_ad_group_ads(customer_id=customer_id, operations=[op_agad])

        # 5. ListingGroup (opcional — só se product_type_l1_filter informado)
        if pt_filter:
            lg_svc = client.get_service("AdGroupCriterionService")
            # 5a. Root SUBDIVISION
            op_root = client.get_type("AdGroupCriterionOperation")
            root = op_root.create
            root.ad_group = ag_rn
            root.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            root.listing_group.type_ = client.enums.ListingGroupTypeEnum.SUBDIVISION
            r_root = lg_svc.mutate_ad_group_criteria(customer_id=customer_id, operations=[op_root])
            root_rn = r_root.results[0].resource_name
            # 5b. Filho alvo (product_type_l1 = pt_filter) → UNIT
            op_alvo = client.get_type("AdGroupCriterionOperation")
            alvo = op_alvo.create
            alvo.ad_group = ag_rn
            alvo.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            alvo.listing_group.type_ = client.enums.ListingGroupTypeEnum.UNIT
            alvo.listing_group.parent_ad_group_criterion = root_rn
            alvo.listing_group.case_value.product_type.level = client.enums.ListingGroupFilterProductTypeLevelEnum.LEVEL1
            alvo.listing_group.case_value.product_type.value = pt_filter
            alvo.cpc_bid_micros = cpc_micros
            # 5c. Outros → excluído
            op_out = client.get_type("AdGroupCriterionOperation")
            out = op_out.create
            out.ad_group = ag_rn
            out.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            out.listing_group.type_ = client.enums.ListingGroupTypeEnum.UNIT
            out.listing_group.parent_ad_group_criterion = root_rn
            out.negative = True
            lg_svc.mutate_ad_group_criteria(customer_id=customer_id, operations=[op_alvo, op_out])

        return {
            "ok": True,
            "campaign_id": camp_id,
            "campaign_resource": camp_rn,
            "nome": nome,
            "budget_diario_reais": budget_reais,
            "cpc_bid_reais": cpc_micros / 1_000_000,
            "product_type_l1_filter": pt_filter or "todos",
            "status": "PAUSED — ativar com POST /ads/campanha/{id}/ativar",
        }
    except Exception as e:
        return {"ok": False, "erro": str(e)}


# ── Simulador Google ADS ──────────────────────────────────────────────────────

@router.get("/simulador", response_class=HTMLResponse)
def ads_simulador_page():
    """Serve o HTML do simulador de ADS."""
    html_path = BASE_DIR / "pages" / "simulador_google_ads.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h2>Simulador ADS não encontrado</h2>", status_code=404)


@router.get("/buscar-produto")
def ads_buscar_produto(q: str = "", limit: int = 10):
    """Busca produto no Bling por SKU ou título — usado pelo simulador ADS."""
    import sys
    sys.path.insert(0, str(BASE_DIR))
    try:
        from bling_client import BlingClient
        client = BlingClient()
        # Tenta por código (SKU) primeiro
        resultados = []
        if q:
            r = client._get("/produtos", params={"codigo": q, "limite": limit, "ativo": "S"})
            data = r.get("data", []) if isinstance(r, dict) else []
            if not data:
                # Fallback: busca por nome
                r2 = client._get("/produtos", params={"nome": q, "limite": limit, "ativo": "S"})
                data = r2.get("data", []) if isinstance(r2, dict) else []
            for p in data:
                resultados.append({
                    "id": p.get("id"),
                    "sku": p.get("codigo", ""),
                    "nome": p.get("nome", ""),
                    "preco": float(p.get("preco", 0) or 0),
                    "custo": float((p.get("custos") or {}).get("precoCusto", 0) or 0),
                    "estoque": p.get("estoque", {}).get("saldoVirtualTotal", 0) if isinstance(p.get("estoque"), dict) else 0,
                })
        return {"ok": True, "produtos": resultados}
    except Exception as e:
        return {"ok": False, "erro": str(e), "produtos": []}


@router.post("/simular")
def ads_simular(payload: dict):
    """
    Calcula projeções de performance de ADS dado produto e parâmetros.
    Inputs: sku, preco_produto, custo_produto, budget_tipo (nominal|percentual),
            budget_valor, desconto_pct, meta_receita_tipo, meta_receita_valor,
            meta_lucro_tipo, meta_lucro_valor, cpc_estimado, periodo_dias,
            conv_rate_pct, ctr_pct
    Outputs: impressoes, cliques, conversoes, receita, lucro, roas, is_estimado
    """
    from fastapi import Request
    import math

    try:
        preco_original  = float(payload.get("preco_produto", 0) or 0)
        custo           = float(payload.get("custo_produto", 0) or 0)
        desconto_pct    = float(payload.get("desconto_pct", 0) or 0)
        cpc             = float(payload.get("cpc_estimado", 0.42) or 0.42)
        periodo_dias    = int(payload.get("periodo_dias", 30) or 30)
        conv_rate_pct   = float(payload.get("conv_rate_pct", 1.0) or 1.0)
        ctr_pct         = float(payload.get("ctr_pct", 1.2) or 1.2)

        # Budget
        budget_tipo  = payload.get("budget_tipo", "nominal")
        budget_valor = float(payload.get("budget_valor", 0) or 0)
        if budget_tipo == "percentual" and preco_original > 0:
            # % da receita alvo
            meta_r_val = float(payload.get("meta_receita_valor", 0) or 0)
            budget_dia = (budget_valor / 100) * meta_r_val / periodo_dias if meta_r_val else budget_valor
        else:
            budget_dia = budget_valor / periodo_dias if periodo_dias else 0

        budget_total = budget_dia * periodo_dias

        # Preço com desconto
        preco_com_desconto = preco_original * (1 - desconto_pct / 100)
        ticket = preco_com_desconto if preco_com_desconto > 0 else preco_original

        # Projeções
        cliques     = budget_total / cpc if cpc > 0 else 0
        impressoes  = cliques / (ctr_pct / 100) if ctr_pct > 0 else 0
        conversoes  = cliques * (conv_rate_pct / 100)
        receita     = conversoes * ticket
        roas        = receita / budget_total if budget_total > 0 else 0

        # Lucro = receita - custo total - budget
        custo_total = conversoes * custo if custo > 0 else 0
        lucro_bruto = receita - custo_total - budget_total

        # IS estimado (benchmark: mercado beleza ~1.2M impr/mês)
        is_estimado = min((impressoes / 30) / 40_000 * 100, 100)  # 40k impr/dia = 100% IS

        # Meta receita check
        meta_receita_val = float(payload.get("meta_receita_valor", 0) or 0)
        meta_receita_tipo = payload.get("meta_receita_tipo", "nominal")
        meta_receita_atingida = False
        if meta_receita_val and meta_receita_tipo == "nominal":
            meta_receita_atingida = receita >= meta_receita_val
        elif meta_receita_val and meta_receita_tipo == "percentual" and preco_original > 0:
            meta_receita_atingida = (receita / (preco_original * periodo_dias)) * 100 >= meta_receita_val

        # Meta lucro check
        meta_lucro_val = float(payload.get("meta_lucro_valor", 0) or 0)
        meta_lucro_tipo = payload.get("meta_lucro_tipo", "nominal")
        meta_lucro_atingida = False
        if meta_lucro_val and meta_lucro_tipo == "nominal":
            meta_lucro_atingida = lucro_bruto >= meta_lucro_val
        elif meta_lucro_val and meta_lucro_tipo == "percentual" and receita > 0:
            meta_lucro_atingida = (lucro_bruto / receita) * 100 >= meta_lucro_val

        # Cenários de desconto
        cenarios = []
        for d in [0, 5, 10, 15, 20, 25, 30]:
            p = preco_original * (1 - d / 100)
            c = cliques
            conv_c = c * (conv_rate_pct / 100)
            rec_c = conv_c * p
            roas_c = rec_c / budget_total if budget_total > 0 else 0
            lucro_c = rec_c - (conv_c * custo if custo > 0 else 0) - budget_total
            cenarios.append({
                "desconto": d,
                "preco": round(p, 2),
                "roas": round(roas_c, 2),
                "receita": round(rec_c, 2),
                "lucro": round(lucro_c, 2),
                "conversoes": round(conv_c, 1),
            })

        return {
            "ok": True,
            "inputs": {
                "ticket": round(ticket, 2),
                "budget_dia": round(budget_dia, 2),
                "budget_total": round(budget_total, 2),
                "cpc": cpc,
                "conv_rate_pct": conv_rate_pct,
                "ctr_pct": ctr_pct,
                "periodo_dias": periodo_dias,
            },
            "outputs": {
                "impressoes": round(impressoes),
                "cliques": round(cliques),
                "conversoes": round(conversoes, 1),
                "receita": round(receita, 2),
                "lucro_bruto": round(lucro_bruto, 2),
                "roas": round(roas, 2),
                "is_estimado": round(is_estimado, 1),
                "custo_por_conversao": round(budget_total / conversoes, 2) if conversoes > 0 else 0,
            },
            "metas": {
                "receita_atingida": meta_receita_atingida,
                "lucro_atingido": meta_lucro_atingida,
                "roas_20x_atingido": roas >= 20,
                "roas_breakeven": round(20 * budget_total, 2),
            },
            "cenarios_desconto": cenarios,
        }
    except Exception as e:
        return {"ok": False, "erro": str(e)}
