# -*- coding: utf-8 -*-
"""
startup.py
Executado antes do uvicorn no Railway.
Gera os arquivos de configuração (data/*.json) a partir das variáveis de ambiente.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

def pr(msg): print(f"[startup] {msg}", flush=True)

# ── Carrega tokens mais recentes do Secret Manager (sobrepõe env vars) ────────
def _carregar_secret_manager():
    """Tenta carregar tokens do Secret Manager e sobrepõe env vars em memória."""
    try:
        from token_persistence import load_bling_tokens, load_shopee_tokens, load_ml_tokens

        # Bling
        bt = load_bling_tokens()
        if bt and bt.get("access_token") and bt.get("refresh_token"):
            os.environ["BLING_ACCESS_TOKEN"]  = bt["access_token"]
            os.environ["BLING_REFRESH_TOKEN"] = bt["refresh_token"]
            pr("Secret Manager: tokens Bling carregados ✓")

        # Shopee
        st = load_shopee_tokens()
        if st and st.get("access_token") and st.get("refresh_token"):
            os.environ["SHOPEE_ACCESS_TOKEN"]  = st["access_token"]
            os.environ["SHOPEE_REFRESH_TOKEN"] = st["refresh_token"]
            if st.get("shop_id"):
                os.environ["SHOPEE_SHOP_ID"] = str(st["shop_id"])
            pr("Secret Manager: tokens Shopee carregados ✓")

        # ML
        mt = load_ml_tokens()
        if mt and mt.get("access_token") and mt.get("refresh_token"):
            os.environ["ML_ACCESS_TOKEN"]  = mt["access_token"]
            os.environ["ML_REFRESH_TOKEN"] = mt["refresh_token"]
            if mt.get("user_id"):
                os.environ["ML_USER_ID"] = mt["user_id"]
            pr("Secret Manager: tokens ML carregados ✓")

    except Exception as e:
        pr(f"AVISO: Secret Manager não disponível ({e}) — usando env vars")

_carregar_secret_manager()

# ── shopify_config.json ───────────────────────────────────────────────────────
shopify_token = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
shopify_shop  = os.getenv("SHOPIFY_SHOP", "pknw4n-eg")

cfg_path = DATA_DIR / "shopify_config.json"

if shopify_token:
    # Só sobrescreve se o arquivo não existe ainda (preserva token atualizado via OAuth no volume)
    if not cfg_path.exists():
        shopify_cfg = {
            "access_token": shopify_token,
            "shop": shopify_shop,
            "shop_url": f"{shopify_shop}.myshopify.com",
            "salvo_em": datetime.now(timezone.utc).isoformat(),
        }
        frete_url = os.getenv("FRETE_CALLBACK_URL", "")
        if frete_url:
            shopify_cfg["frete_callback_url"] = frete_url
        cfg_path.write_text(json.dumps(shopify_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        pr(f"shopify_config.json criado (shop={shopify_shop})")
    else:
        # Garante que frete_callback_url está sempre atualizado sem sobrescrever o token
        frete_url = os.getenv("FRETE_CALLBACK_URL", "")
        if frete_url:
            try:
                existing = json.loads(cfg_path.read_text(encoding="utf-8"))
                if existing.get("frete_callback_url") != frete_url:
                    existing["frete_callback_url"] = frete_url
                    cfg_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        pr(f"shopify_config.json já existe no volume — token OAuth preservado")
elif not cfg_path.exists():
    pr("AVISO: SHOPIFY_ACCESS_TOKEN não definido — shopify_config.json não criado")

# ── bling_tokens.json (criptografado, formato esperado pelo BlingClient) ──────
import hashlib, base64

def _bling_xor(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))

bling_access  = os.getenv("BLING_ACCESS_TOKEN", "")
bling_refresh = os.getenv("BLING_REFRESH_TOKEN", "")
bling_cid     = os.getenv("BLING_CLIENT_ID", "")
bling_csec    = os.getenv("BLING_CLIENT_SECRET", "")
bling_path    = DATA_DIR / "bling_tokens.json"

if bling_access and bling_refresh:
    import time as _time
    import urllib.request as _urllib
    import urllib.parse as _urlparse

    # Se já existe token no volume (salvo via OAuth), não sobrescreve.
    # O BlingClient faz refresh automático quando expirar.
    if bling_path.exists():
        pr("bling_tokens.json já existe no volume — token OAuth preservado")
    else:
        # Primeira vez: tenta renovar via refresh_token para garantir token fresco
        _new = None
        if bling_cid and bling_csec:
            try:
                import base64 as _b64mod
                _basic = _b64mod.b64encode(f"{bling_cid}:{bling_csec}".encode()).decode()
                _data = _urlparse.urlencode({"grant_type": "refresh_token", "refresh_token": bling_refresh}).encode()
                _req = _urllib.Request(
                    "https://www.bling.com.br/Api/v3/oauth/token",
                    data=_data,
                    headers={"Authorization": f"Basic {_basic}", "Content-Type": "application/x-www-form-urlencoded"},
                )
                with _urllib.urlopen(_req, timeout=8) as _resp:
                    _new = json.loads(_resp.read())
                    bling_access  = _new.get("access_token", bling_access)
                    bling_refresh = _new.get("refresh_token", bling_refresh)
                    pr("Bling: token renovado via refresh com sucesso")
            except Exception as _e:
                pr(f"AVISO: refresh do token Bling falhou ({_e}) — usando token da env var")

        # Usa o expires_in real retornado pelo Bling (normalmente 3600s = 1h).
        # _new só existe se o refresh foi bem-sucedido; caso contrário usa 3600.
        _expires_in = int(_new.get("expires_in", 3600)) if _new else 3600
        raw_token = {
            "access_token":  bling_access,
            "refresh_token": bling_refresh,
            "token_type":    "Bearer",
            "expires_in":    _expires_in,
            "expires_at":    _time.time() + _expires_in,
        }
        key = hashlib.sha256((bling_csec + (bling_cid or "token-key")).encode()).digest()
        enc = base64.b64encode(_bling_xor(json.dumps(raw_token).encode(), key)).decode()
        bling_path.write_text(json.dumps({"encrypted": enc}, ensure_ascii=False), encoding="utf-8")
        pr("bling_tokens.json criado (criptografado)")
elif not bling_path.exists():
    pr("INFO: BLING_ACCESS_TOKEN não definido — bling_tokens.json não criado (OAuth necessário)")

# ── Bling AKG tokens (recria bling_tokens_akg.json a partir de env vars) ──────
bling_akg_access  = os.getenv("BLING_AKG_ACCESS_TOKEN", "")
bling_akg_refresh = os.getenv("BLING_AKG_REFRESH_TOKEN", "")
bling_akg_path    = DATA_DIR / "bling_tokens_akg.json"

if bling_akg_access and bling_akg_refresh:
    import time as _time_akg
    akg_tok = {}
    if bling_akg_path.exists():
        try:
            akg_tok = json.loads(bling_akg_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    akg_tok.update({
        "access_token":  bling_akg_access,
        "refresh_token": bling_akg_refresh,
        "expires_in":    21600,
        "expires_at":    _time_akg.time() + 21600 - 60,
    })
    bling_akg_path.write_text(json.dumps(akg_tok, ensure_ascii=False, indent=2), encoding="utf-8")
    pr("bling_tokens_akg.json criado/atualizado a partir de env vars")
elif not bling_akg_path.exists():
    pr("INFO: BLING_AKG_ACCESS_TOKEN não definido — bling_tokens_akg.json não criado (OAuth necessário)")

# ── Melhor Envio token (se fornecido diretamente via env) ────────────────────
me_token = os.getenv("MELHOR_ENVIO_TOKEN", "")
if me_token:
    pr(f"MELHOR_ENVIO_TOKEN configurado ({len(me_token)} chars) — cotações em tempo real ativas")
else:
    pr("INFO: MELHOR_ENVIO_TOKEN não definido — usando tabela regional de fallback")

# ── Mercado Livre config (ml_config.json + ml_tokens.json) ───────────────────
ml_client_id     = os.getenv("ML_CLIENT_ID", "")
ml_client_secret = os.getenv("ML_CLIENT_SECRET", "")
ml_redirect_uri  = os.getenv("ML_REDIRECT_URI", "")
ml_access_token  = os.getenv("ML_ACCESS_TOKEN", "")
ml_refresh_token = os.getenv("ML_REFRESH_TOKEN", "")

ml_config_path = DATA_DIR / "ml_config.json"
ml_tokens_path = DATA_DIR / "ml_tokens.json"

if ml_client_id and ml_client_secret:
    ml_cfg = {
        "client_id":     ml_client_id,
        "client_secret": ml_client_secret,
        "redirect_uri":  ml_redirect_uri,
        "conexao_ativa": bool(ml_access_token),
    }
    ml_config_path.write_text(json.dumps(ml_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    pr(f"ml_config.json criado (client_id={ml_client_id})")
elif not ml_config_path.exists():
    pr("INFO: ML_CLIENT_ID não definido — ml_config.json não criado")

if ml_refresh_token:
    import urllib.request as _urllib
    import urllib.parse as _urlparse

    ml_user_id = os.getenv("ML_USER_ID", "").strip()

    # Se o volume já tem um ml_tokens.json, usa o refresh_token de lá
    # (pode ser mais novo que o da env var, pois o MLClient atualiza em runtime)
    _vol_refresh = ml_refresh_token
    if ml_tokens_path.exists():
        try:
            _vol_tok = json.loads(ml_tokens_path.read_text(encoding="utf-8"))
            _vol_refresh = _vol_tok.get("refresh_token", ml_refresh_token)
            if not ml_user_id:
                ml_user_id = str(_vol_tok.get("user_id", ""))
            pr(f"ml_tokens.json encontrado no volume — usando refresh_token do volume")
        except Exception:
            pass

    # Sempre renova o token via refresh para garantir validade
    if ml_client_id and ml_client_secret:
        # Tenta primeiro com o refresh_token do volume; se falhar, usa o da env var
        for _rt in ([_vol_refresh, ml_refresh_token] if _vol_refresh != ml_refresh_token else [_vol_refresh]):
            try:
                _data = _urlparse.urlencode({
                    "grant_type":    "refresh_token",
                    "client_id":     ml_client_id,
                    "client_secret": ml_client_secret,
                    "refresh_token": _rt,
                }).encode()
                _req = _urllib.Request("https://api.mercadolibre.com/oauth/token", data=_data)
                with _urllib.urlopen(_req, timeout=8) as _resp:
                    _new = json.loads(_resp.read())
                    ml_access_token  = _new.get("access_token", ml_access_token)
                    ml_refresh_token = _new.get("refresh_token", ml_refresh_token)
                    if not ml_user_id:
                        ml_user_id = str(_new.get("user_id", ""))
                    pr(f"Tokens ML renovados via refresh. user_id={ml_user_id}")
                    break
            except Exception as _e:
                pr(f"AVISO: refresh ML falhou com token {'volume' if _rt == _vol_refresh else 'env'} ({_e})")
    else:
        # Sem credenciais para refresh — tenta validar o access_token atual
        if not ml_user_id:
            try:
                _req = _urllib.Request(
                    "https://api.mercadolibre.com/users/me",
                    headers={"Authorization": f"Bearer {ml_access_token}"}
                )
                with _urllib.urlopen(_req, timeout=8) as _resp:
                    _me = json.loads(_resp.read())
                    ml_user_id = str(_me.get("id", ""))
                    pr(f"user_id obtido via /users/me: {ml_user_id}")
            except Exception as _e:
                pr(f"AVISO: /users/me falhou ({_e})")

    ml_tok = {
        "access_token":  ml_access_token,
        "refresh_token": ml_refresh_token,
        "token_type":    "Bearer",
        "client_id":     ml_client_id,
        "user_id":       ml_user_id,
        "salvo_em":      datetime.now(timezone.utc).isoformat(),
    }
    ml_tokens_path.write_text(json.dumps(ml_tok, indent=2, ensure_ascii=False), encoding="utf-8")
    pr(f"ml_tokens.json atualizado (user_id={ml_user_id or 'não obtido'})")
elif not ml_tokens_path.exists():
    pr("INFO: ML_ACCESS_TOKEN não definido — ml_tokens.json não criado (OAuth necessário)")

# ── ML AKG tokens (ml_tokens_akg.json) ──────────────────────────────────────
ml_akg_client_id     = os.getenv("ML_AKG_CLIENT_ID", "") or ml_client_id
ml_akg_client_secret = os.getenv("ML_AKG_CLIENT_SECRET", "") or ml_client_secret
ml_akg_access_token  = os.getenv("ML_AKG_ACCESS_TOKEN", "")
ml_akg_refresh_token = os.getenv("ML_AKG_REFRESH_TOKEN", "")
ml_akg_user_id       = os.getenv("ML_AKG_USER_ID", "").strip()
ml_akg_tokens_path   = DATA_DIR / "ml_tokens_akg.json"

if ml_akg_refresh_token:
    import urllib.request as _urllib2
    import urllib.parse as _urlparse2

    _akg_vol_refresh = ml_akg_refresh_token
    if ml_akg_tokens_path.exists():
        try:
            _akg_vol = json.loads(ml_akg_tokens_path.read_text(encoding="utf-8"))
            _akg_vol_refresh = _akg_vol.get("refresh_token", ml_akg_refresh_token)
            if not ml_akg_user_id:
                ml_akg_user_id = str(_akg_vol.get("user_id", ""))
            pr("ml_tokens_akg.json encontrado — usando refresh_token do volume")
        except Exception:
            pass

    if ml_akg_client_id and ml_akg_client_secret:
        for _rt in ([_akg_vol_refresh, ml_akg_refresh_token] if _akg_vol_refresh != ml_akg_refresh_token else [_akg_vol_refresh]):
            try:
                import time as _time_akg
                _data = _urlparse2.urlencode({
                    "grant_type":    "refresh_token",
                    "client_id":     ml_akg_client_id,
                    "client_secret": ml_akg_client_secret,
                    "refresh_token": _rt,
                }).encode()
                _req2 = _urllib2.Request("https://api.mercadolibre.com/oauth/token", data=_data)
                with _urllib2.urlopen(_req2, timeout=8) as _resp2:
                    _new_akg = json.loads(_resp2.read())
                    ml_akg_access_token  = _new_akg.get("access_token", ml_akg_access_token)
                    ml_akg_refresh_token = _new_akg.get("refresh_token", ml_akg_refresh_token)
                    if not ml_akg_user_id:
                        ml_akg_user_id = str(_new_akg.get("user_id", ""))
                    pr(f"Tokens ML AKG renovados via refresh. user_id={ml_akg_user_id}")
                    break
            except Exception as _e:
                pr(f"AVISO: refresh ML AKG falhou ({_e})")

    import time as _time_akg2
    ml_akg_tok = {
        "access_token":  ml_akg_access_token,
        "refresh_token": ml_akg_refresh_token,
        "token_type":    "Bearer",
        "client_id":     ml_akg_client_id,
        "client_secret": ml_akg_client_secret,
        "user_id":       ml_akg_user_id,
        "expires_at":    _time_akg2.time() + 21600,
        "salvo_em":      datetime.now(timezone.utc).isoformat(),
    }
    ml_akg_tokens_path.write_text(json.dumps(ml_akg_tok, indent=2, ensure_ascii=False), encoding="utf-8")
    pr(f"ml_tokens_akg.json atualizado (user_id={ml_akg_user_id or 'não obtido'})")
elif not ml_akg_tokens_path.exists():
    pr("INFO: ML_AKG_ACCESS_TOKEN não definido — ml_tokens_akg.json não criado (OAuth necessário via /ml/login2)")

# ── Shopee tokens (shopee_tokens.json) ───────────────────────────────────────
shopee_access_token  = os.getenv("SHOPEE_ACCESS_TOKEN", "")
shopee_refresh_token = os.getenv("SHOPEE_REFRESH_TOKEN", "")
shopee_shop_id       = os.getenv("SHOPEE_SHOP_ID", "")
shopee_partner_id    = os.getenv("SHOPEE_PARTNER_ID", "")
shopee_tokens_path   = DATA_DIR / "shopee_tokens.json"

if shopee_access_token and shopee_refresh_token and shopee_shop_id:
    import time as _time2
    # Tenta usar expires_at do Secret Manager (mais preciso que recalcular)
    _sm_shopee = {}
    try:
        from token_persistence import load_shopee_tokens as _lst
        _sm_shopee = _lst() or {}
    except Exception:
        pass
    _expires_at = _sm_shopee.get("expires_at", 0)
    _agora = _time2.time()

    # Sempre escreve o arquivo primeiro com os tokens disponíveis (env var / SM)
    # para que renovar_token() possa lê-los se precisar renovar.
    _tok_base = {
        "access_token":  shopee_access_token,
        "refresh_token": shopee_refresh_token,
        "expires_at":    _expires_at if _expires_at > _agora else 0,  # 0 = forçar renovação
        "shop_id":       int(shopee_shop_id),
        "partner_id":    int(shopee_partner_id) if shopee_partner_id else 0,
        "obtido_em":     datetime.now(timezone.utc).isoformat(),
    }
    shopee_tokens_path.write_text(json.dumps(_tok_base, indent=2, ensure_ascii=False), encoding="utf-8")

    if _expires_at <= _agora + 300:  # expirado ou expirando em <5min → renova
        pr("Shopee: token expirado ou próximo do limite — tentando renovar via refresh_token…")
        try:
            from services.shopee import ShopeeOAuthService as _ShopeeOAuth
            _res = _ShopeeOAuth().renovar_token()
            if _res.get("success"):
                pr("Shopee: token renovado no startup ✓")
            else:
                pr(f"Shopee AVISO: renovação falhou ({_res.get('error')}) — usando token existente")
                # Fallback: escreve arquivo com expires_at estimado para evitar loops
                _tok_base["expires_at"] = _agora + 14400
                shopee_tokens_path.write_text(json.dumps(_tok_base, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as _e:
            pr(f"Shopee AVISO: erro ao renovar token ({_e})")
            _tok_base["expires_at"] = _agora + 14400
            shopee_tokens_path.write_text(json.dumps(_tok_base, indent=2, ensure_ascii=False), encoding="utf-8")
        if shopee_tokens_path.exists():
            pr("Shopee: shopee_tokens.json pronto")
    else:
        # Token ainda válido — atualiza expires_at real no arquivo
        _tok_base["expires_at"] = _expires_at
        shopee_tokens_path.write_text(json.dumps(_tok_base, indent=2, ensure_ascii=False), encoding="utf-8")
        mins = int((_expires_at - _agora) / 60)
        pr(f"shopee_tokens.json criado (shop_id={shopee_shop_id}, expira em {mins}min)")
elif not shopee_tokens_path.exists():
    pr("INFO: SHOPEE_ACCESS_TOKEN não definido — shopee_tokens.json não criado (OAuth necessário)")

# ── Amazon config (amazon_config.json) ───────────────────────────────────────
amazon_client_id     = os.getenv("AMAZON_CLIENT_ID", "")
amazon_client_secret = os.getenv("AMAZON_CLIENT_SECRET", "")
amazon_refresh_token = os.getenv("AMAZON_REFRESH_TOKEN", "")
amazon_seller_id     = os.getenv("AMAZON_SELLER_ID", "")
amazon_marketplace   = os.getenv("AMAZON_MARKETPLACE_ID", "A2Q3Y263D00KWC")
amazon_config_path   = DATA_DIR / "amazon_config.json"

if amazon_client_id and amazon_client_secret and amazon_refresh_token and amazon_seller_id:
    amazon_cfg = {
        "client_id":      amazon_client_id,
        "client_secret":  amazon_client_secret,
        "refresh_token":  amazon_refresh_token,
        "seller_id":      amazon_seller_id,
        "marketplace_id": amazon_marketplace,
    }
    amazon_config_path.write_text(json.dumps(amazon_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    pr(f"amazon_config.json criado (seller_id={amazon_seller_id})")
elif not amazon_config_path.exists():
    pr("INFO: AMAZON_CLIENT_ID não definido — amazon_config.json não criado")

# ── GMC Blacklist (merge do default com o volume a cada deploy) ───────────────
_bl_src  = Path(__file__).parent / "gmc_blacklist_default.json"
_bl_dest = DATA_DIR / "gmc_blacklist.json"
if _bl_src.exists():
    try:
        _default = json.loads(_bl_src.read_text(encoding="utf-8"))
        _existing = {}
        if _bl_dest.exists():
            try:
                _existing = json.loads(_bl_dest.read_text(encoding="utf-8"))
            except Exception:
                pass
        # Merge: categorias e IDs do default são adicionados ao existente (sem remover customizações)
        _merged_cats = list(dict.fromkeys(
            _existing.get("categories", []) + _default.get("categories", [])
        ))
        _merged_ids  = list(dict.fromkeys(
            _existing.get("product_ids", []) + _default.get("product_ids", [])
        ))
        _merged = {"categories": _merged_cats, "product_ids": _merged_ids}
        _bl_dest.write_text(json.dumps(_merged, indent=2, ensure_ascii=False), encoding="utf-8")
        pr(f"gmc_blacklist.json atualizado ({len(_merged_cats)} categorias, {len(_merged_ids)} IDs)")
    except Exception as _e:
        pr(f"AVISO: falha ao atualizar gmc_blacklist.json ({_e})")

# ── Google Service Account (GMC — Content API) ───────────────────────────────
gsa_b64 = os.getenv("GOOGLE_SA_JSON", "")
gsa_path = DATA_DIR / "google_service_account.json"
if gsa_b64:
    if not gsa_path.exists():
        try:
            import json as _json
            gsa_json = None
            # 1) Tenta JSON direto (pode conter \n ou espaços extras)
            _stripped = gsa_b64.strip()
            try:
                _json.loads(_stripped)
                gsa_json = _stripped
            except Exception:
                pass
            # 2) Tenta base64 com padding automático
            if gsa_json is None:
                _b64 = _stripped
                _pad = (4 - len(_b64) % 4) % 4
                _b64 += "=" * _pad
                gsa_json = base64.b64decode(_b64.encode()).decode("utf-8")
            gsa_path.write_text(gsa_json, encoding="utf-8")
            pr("google_service_account.json criado a partir de GOOGLE_SA_JSON")
        except Exception as _e:
            pr(f"AVISO: falha ao gravar google_service_account.json ({_e})")
    else:
        pr("google_service_account.json já existe no volume — preservado")
else:
    if not gsa_path.exists():
        pr("INFO: GOOGLE_SA_JSON não definido — google_service_account.json não criado")

# ── Google Ads YAML (recria a partir de env vars para sobreviver a deploys) ──
_ads_yaml_path = Path(__file__).parent / "google-ads.yaml"
_ads_client_id       = os.getenv("GOOGLE_ADS_CLIENT_ID", "")
_ads_client_secret   = os.getenv("GOOGLE_ADS_CLIENT_SECRET", "")
_ads_dev_token       = os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN", "")
_ads_login_cid       = os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "")
_ads_refresh_token   = os.getenv("GOOGLE_ADS_REFRESH_TOKEN", "")
if _ads_client_id and _ads_client_secret and _ads_refresh_token:
    _ads_yaml_content = (
        f"client_id: {_ads_client_id}\n"
        f"client_secret: {_ads_client_secret}\n"
        f"developer_token: {_ads_dev_token}\n"
        f"login_customer_id: {_ads_login_cid}\n"
        f"refresh_token: {_ads_refresh_token}\n"
        f"use_proto_plus: true\n"
    )
    _ads_yaml_path.write_text(_ads_yaml_content, encoding="utf-8")
    pr("google-ads.yaml recriado a partir de env vars")
else:
    if not _ads_yaml_path.exists():
        pr("INFO: GOOGLE_ADS_CLIENT_ID/REFRESH_TOKEN não definidos — google-ads.yaml não criado")

# ── Cria diretórios necessários ───────────────────────────────────────────────
for d in ["logs", "pages"]:
    Path(__file__).parent.joinpath(d).mkdir(exist_ok=True)

pr("Startup concluído — iniciando servidor...")
