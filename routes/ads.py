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
                   ad_group.id
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

        operations = []
        for row in lg_rows:
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
