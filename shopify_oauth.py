"""
shopify_oauth.py — OAuth do Shopify para o Shinsei Pricing
"""
import hashlib
import hmac
import json
import logging
import os
import secrets
import requests
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

SHOPIFY_CLIENT_ID     = os.getenv("SHOPIFY_CLIENT_ID", "3336a3010ee22d2e21018a3ce849b360")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")
SHOPIFY_STORE = "pknw4n-eg"
SHOPIFY_SCOPES = "read_products,write_products,read_inventory,write_inventory,read_locations"
DATA_DIR = Path(__file__).parent / "data"
SHOPIFY_CONFIG_PATH = DATA_DIR / "shopify_config.json"
SHOPIFY_STATE_PATH = DATA_DIR / "shopify_state.json"

def _load_json(path, default):
    if not path.exists(): return default
    try: return json.loads(path.read_text(encoding="utf-8"))
    except: return default

def _save_json(path, data):
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def gerar_url_auth(redirect_uri: str) -> str:
    state = secrets.token_hex(16)
    _save_json(SHOPIFY_STATE_PATH, {"state": state})
    url = (
        f"https://{SHOPIFY_STORE}.myshopify.com/admin/oauth/authorize"
        f"?client_id={SHOPIFY_CLIENT_ID}"
        f"&scope={SHOPIFY_SCOPES}"
        f"&redirect_uri={redirect_uri}"
        f"&state={state}"
    )
    return url

def processar_callback(code: str, state: str, redirect_uri: str) -> dict:
    saved = _load_json(SHOPIFY_STATE_PATH, {})
    if saved.get("state") != state:
        return {"ok": False, "erro": "State inválido."}
    try:
        res = requests.post(
            f"https://{SHOPIFY_STORE}.myshopify.com/admin/oauth/access_token",
            json={
                "client_id": SHOPIFY_CLIENT_ID,
                "client_secret": SHOPIFY_CLIENT_SECRET,
                "code": code,
            },
            timeout=15
        )
        if res.status_code == 200:
            data = res.json()
            token = data.get("access_token", "")
            _save_json(SHOPIFY_CONFIG_PATH, {
                "access_token": token,
                "scope": data.get("scope", ""),
                "salvo_em": datetime.utcnow().isoformat(),
            })
            logger.info("Shopify OAuth concluído. Scopes: %s", data.get("scope"))
            return {"ok": True, "token": token[:10] + "..."}
        return {"ok": False, "erro": res.text[:200]}
    except Exception as e:
        return {"ok": False, "erro": str(e)}
