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
    shopify_cfg = {
        "access_token": shopify_token,
        "shop": shopify_shop,
        "shop_url": f"{shopify_shop}.myshopify.com",
        "salvo_em": datetime.now(timezone.utc).isoformat(),
    }
    # Adiciona frete callback URL se configurada
    frete_url = os.getenv("FRETE_CALLBACK_URL", "")
    if frete_url:
        shopify_cfg["frete_callback_url"] = frete_url

    cfg_path.write_text(json.dumps(shopify_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    pr(f"shopify_config.json criado (shop={shopify_shop})")
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
    raw_token = {
        "access_token":  bling_access,
        "refresh_token": bling_refresh,
        "token_type":    "Bearer",
        "expires_in":    21600,
        "expires_at":    _time.time() + 21600,  # BlingClient usa expires_at para checar validade
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
    ml_tok = {
        "access_token":  ml_access_token,
        "refresh_token": ml_refresh_token,
        "token_type":    "Bearer",
        "client_id":     ml_client_id,
        "salvo_em":      datetime.now(timezone.utc).isoformat(),
    }
    ml_tokens_path.write_text(json.dumps(ml_tok, indent=2, ensure_ascii=False), encoding="utf-8")
    pr("ml_tokens.json criado")
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

# ── Cria diretórios necessários ───────────────────────────────────────────────
for d in ["logs", "pages"]:
    Path(__file__).parent.joinpath(d).mkdir(exist_ok=True)

pr("Startup concluído — iniciando servidor...")
