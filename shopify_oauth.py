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
SHOPIFY_SCOPES = "read_products,write_products,read_inventory,write_inventory,read_locations,read_shipping,write_shipping,read_themes,write_themes,read_script_tags,write_script_tags"
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
            scope = data.get("scope", "")
            _save_json(SHOPIFY_CONFIG_PATH, {
                "access_token": token,
                "scope": scope,
                "salvo_em": datetime.utcnow().isoformat(),
            })
            logger.info("Shopify OAuth concluído. Scopes: %s", scope)
            # Instalar script tag de conversão do Google Ads automaticamente
            if "write_script_tags" in scope:
                try:
                    _instalar_gtag_conversion(token)
                except Exception as ex:
                    logger.warning("Falha ao instalar gtag script tag: %s", ex)
            return {"ok": True, "token": token[:10] + "...", "scope": scope}
        return {"ok": False, "erro": res.text[:200]}
    except Exception as e:
        return {"ok": False, "erro": str(e)}


def _instalar_gtag_conversion(token: str):
    """Instala o pixel de conversão do Google Ads na página de confirmação de pedido."""
    STORE    = f"{SHOPIFY_STORE}.myshopify.com"
    BASE     = f"https://{STORE}/admin/api/2024-01"
    HEADERS  = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    TEMA_ID  = 185169445169
    AW_ID    = "AW-2097362078"
    CONV_ID  = "7604222139"

    conversion_js = f"""// Google Ads Conversion — Shinsei Market (auto-instalado via OAuth)
(function() {{
  if (typeof Shopify === 'undefined' || !Shopify.Checkout) return;
  if (Shopify.Checkout.step !== 'thank_you') return;
  if (window._gadsConvFired) return;
  window._gadsConvFired = true;
  function fire() {{
    if (typeof gtag === 'undefined') {{ setTimeout(fire, 300); return; }}
    var val = (Shopify.checkout && Shopify.checkout.total_price)
                ? (Shopify.checkout.total_price / 100).toFixed(2) : 0;
    var tid = (Shopify.checkout && Shopify.checkout.order_id)
                ? String(Shopify.checkout.order_id) : '';
    gtag('event', 'conversion', {{
      'send_to': '{AW_ID}/{CONV_ID}',
      'value': parseFloat(val),
      'currency': 'BRL',
      'transaction_id': tid
    }});
    console.log('[Shinsei] Google Ads conversion fired', val, tid);
  }}
  fire();
}})();
"""

    # 1. Salvar JS como asset no tema
    asset_body = json.dumps({
        "asset": {"key": "assets/shinsei-ads-conversion.js", "value": conversion_js}
    }).encode()
    asset_req = requests.put(
        f"https://{STORE}/admin/api/2024-01/themes/{TEMA_ID}/assets.json",
        data=asset_body, headers=HEADERS, timeout=15
    )
    if asset_req.status_code not in (200, 201):
        raise RuntimeError(f"Erro ao salvar asset: {asset_req.status_code} {asset_req.text[:200]}")

    public_url = asset_req.json().get("asset", {}).get("public_url", "")
    logger.info("gtag asset salvo: %s", public_url)

    # 2. Remover script tags antigas de conversão
    existing = requests.get(f"{BASE}/script_tags.json?limit=50", headers=HEADERS, timeout=15).json()
    for st in existing.get("script_tags", []):
        if "shinsei-ads" in st.get("src", "") or "AW-" in st.get("src", ""):
            requests.delete(f"{BASE}/script_tags/{st['id']}.json", headers=HEADERS, timeout=15)
            logger.info("Script tag antiga removida: %s", st["src"])

    # 3. Criar nova script tag
    tag_body = {
        "script_tag": {
            "event": "onload",
            "src": public_url,
            "display_scope": "order_status",
            "cache": False
        }
    }
    resp = requests.post(f"{BASE}/script_tags.json", json=tag_body, headers=HEADERS, timeout=15)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Erro ao criar script tag: {resp.status_code} {resp.text[:200]}")

    tag = resp.json().get("script_tag", {})
    logger.info("Script tag instalada: ID=%s scope=%s src=%s",
                tag.get("id"), tag.get("display_scope"), tag.get("src", "")[:60])
