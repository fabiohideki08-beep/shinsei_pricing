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

# ── bling_token.json (se tokens fornecidos via env) ───────────────────────────
bling_access  = os.getenv("BLING_ACCESS_TOKEN", "")
bling_refresh = os.getenv("BLING_REFRESH_TOKEN", "")
bling_path    = Path(__file__).parent / "bling_token.json"

if bling_access and bling_refresh:
    bling_cfg = {
        "access_token":  bling_access,
        "refresh_token": bling_refresh,
        "token_type":    "Bearer",
        "salvo_em":      datetime.now(timezone.utc).isoformat(),
    }
    bling_path.write_text(json.dumps(bling_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    pr("bling_token.json criado")
elif not bling_path.exists():
    pr("INFO: BLING_ACCESS_TOKEN não definido — bling_token.json não criado (OAuth necessário)")

# ── Melhor Envio token (se fornecido diretamente via env) ────────────────────
me_token = os.getenv("MELHOR_ENVIO_TOKEN", "")
if me_token:
    pr(f"MELHOR_ENVIO_TOKEN configurado ({len(me_token)} chars) — cotações em tempo real ativas")
else:
    pr("INFO: MELHOR_ENVIO_TOKEN não definido — usando tabela regional de fallback")

# ── Cria diretórios necessários ───────────────────────────────────────────────
for d in ["logs", "pages"]:
    Path(__file__).parent.joinpath(d).mkdir(exist_ok=True)

pr("Startup concluído — iniciando servidor...")
