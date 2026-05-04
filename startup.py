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
                with _urllib.urlopen(_req, timeout=20) as _resp:
                    _new = json.loads(_resp.read())
                    bling_access  = _new.get("access_token", bling_access)
                    bling_refresh = _new.get("refresh_token", bling_refresh)
                    pr("Bling: token renovado via refresh com sucesso")
            except Exception as _e:
                pr(f"AVISO: refresh do token Bling falhou ({_e}) — usando token da env var")

        raw_token = {
            "access_token":  bling_access,
            "refresh_token": bling_refresh,
            "token_type":    "Bearer",
            "expires_in":    21600,
            "expires_at":    _time.time() + 21600,
        }
        key = hashlib.sha256((bling_csec + (bling_cid or "token-key")).encode()).digest()
        enc = base64.b64encode(_bling_xor(json.dumps(raw_token).encode(), key)).decode()
        bling_path.write_text(json.dumps({"encrypted": enc}, ensure_ascii=False), encoding="utf-8")
        pr("bling_tokens.json criado (criptografado)")
elif not bling_path.exists():
    pr("INFO: BLING_ACCESS_TOKEN não definido — bling_tokens.json não criado (OAuth necessário)")

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

if ml_access_token and ml_refresh_token:
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
                with _urllib.urlopen(_req, timeout=15) as _resp:
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
                with _urllib.urlopen(_req, timeout=10) as _resp:
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

# ── Shopee tokens (shopee_tokens.json) ───────────────────────────────────────
shopee_access_token  = os.getenv("SHOPEE_ACCESS_TOKEN", "")
shopee_refresh_token = os.getenv("SHOPEE_REFRESH_TOKEN", "")
shopee_shop_id       = os.getenv("SHOPEE_SHOP_ID", "")
shopee_partner_id    = os.getenv("SHOPEE_PARTNER_ID", "")
shopee_tokens_path   = DATA_DIR / "shopee_tokens.json"

if shopee_access_token and shopee_refresh_token and shopee_shop_id:
    import time as _time2
    shopee_tok = {
        "access_token":  shopee_access_token,
        "refresh_token": shopee_refresh_token,
        "expires_at":    _time2.time() + 14400,  # será renovado automaticamente
        "shop_id":       int(shopee_shop_id),
        "partner_id":    int(shopee_partner_id) if shopee_partner_id else 0,
        "obtido_em":     datetime.now(timezone.utc).isoformat(),
    }
    shopee_tokens_path.write_text(json.dumps(shopee_tok, indent=2, ensure_ascii=False), encoding="utf-8")
    pr(f"shopee_tokens.json criado (shop_id={shopee_shop_id})")
elif not shopee_tokens_path.exists():
    pr("INFO: SHOPEE_ACCESS_TOKEN não definido — shopee_tokens.json não criado (OAuth necessário)")

# ── GMC Blacklist (copia do código para o volume na primeira vez) ─────────────
_bl_src  = Path(__file__).parent / "gmc_blacklist_default.json"
_bl_dest = DATA_DIR / "gmc_blacklist.json"
if _bl_src.exists() and not _bl_dest.exists():
    try:
        import shutil as _shutil
        _shutil.copy2(str(_bl_src), str(_bl_dest))
        pr("gmc_blacklist.json copiado para o volume")
    except Exception as _e:
        pr(f"AVISO: falha ao copiar gmc_blacklist.json ({_e})")

# ── Google Service Account (GMC — Content API) ───────────────────────────────
gsa_b64 = os.getenv("GOOGLE_SA_JSON", "")
gsa_path = DATA_DIR / "google_service_account.json"
if gsa_b64:
    if not gsa_path.exists():
        try:
            gsa_json = base64.b64decode(gsa_b64.encode()).decode("utf-8")
            gsa_path.write_text(gsa_json, encoding="utf-8")
            pr("google_service_account.json criado a partir de GOOGLE_SA_JSON")
        except Exception as _e:
            pr(f"AVISO: falha ao gravar google_service_account.json ({_e})")
    else:
        pr("google_service_account.json já existe no volume — preservado")
else:
    if not gsa_path.exists():
        pr("INFO: GOOGLE_SA_JSON não definido — google_service_account.json não criado")

# ── Cria diretórios necessários ───────────────────────────────────────────────
for d in ["logs", "pages"]:
    Path(__file__).parent.joinpath(d).mkdir(exist_ok=True)

pr("Startup concluído — iniciando servidor...")
