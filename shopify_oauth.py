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
SHOPIFY_SCOPES = "read_products,write_products,read_inventory,write_inventory,read_locations,read_shipping,write_shipping,read_themes,write_themes,read_script_tags,write_script_tags,read_content,write_content,write_checkouts,read_orders,write_orders,write_pixels,read_pixels"
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
    saved_state = saved.get("state")
    if saved_state and saved_state != state:
        # State mismatch — pode ser CSRF ou multi-instância no Cloud Run
        logger.warning("Shopify OAuth: state mismatch (esperado=%s recebido=%s) — continuando (Cloud Run multi-instance)", saved_state[:8] if saved_state else "N/A", state[:8] if state else "N/A")
    elif not saved_state:
        logger.warning("Shopify OAuth: state não encontrado no servidor (Cloud Run multi-instance) — continuando sem validação CSRF")
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


_GCLID_CAPTURE_MARKER = "<!-- [Shinsei] GCLID-Capture -->"

_GCLID_CAPTURE_SCRIPT = """<!-- [Shinsei] GCLID-Capture -->
<script>
(function() {
  var KEY = 'sh_gclid', TS = 'sh_gclid_ts', TTL = 90 * 86400000;
  try {
    var p = new URLSearchParams(window.location.search);
    var g = p.get('gclid') || p.get('wbraid') || p.get('gbraid');
    if (g) { localStorage.setItem(KEY, g); localStorage.setItem(TS, Date.now()); }
    var ts = parseInt(localStorage.getItem(TS) || '0');
    if (Date.now() - ts > TTL) { localStorage.removeItem(KEY); localStorage.removeItem(TS); return; }
    var stored = localStorage.getItem(KEY);
    if (stored) {
      fetch('/cart/update.js', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({attributes: {gclid: stored}})
      }).catch(function(){});
    }
  } catch(e) {}
})();
</script>
<!-- [/Shinsei] GCLID-Capture -->"""


def _instalar_gclid_capture(token: str, tema_id: int = 185169445169) -> dict:
    """
    Injeta captura de GCLID no theme.liquid.
    O GCLID é salvo em localStorage e propagado como cart attribute → note_attribute do pedido.
    O webhook server-side lê o note_attribute e inclui o GCLID na conversão enviada ao Google Ads.
    """
    STORE   = f"{SHOPIFY_STORE}.myshopify.com"
    HEADERS = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    # Ler theme.liquid atual
    r = requests.get(
        f"https://{STORE}/admin/api/2024-01/themes/{tema_id}/assets.json",
        params={"asset[key]": "layout/theme.liquid"},
        headers=HEADERS, timeout=15
    )
    if r.status_code != 200:
        raise RuntimeError(f"Não consegui ler theme.liquid: {r.status_code} {r.text[:200]}")

    content = r.json().get("asset", {}).get("value", "")

    # Não injetar duas vezes
    if _GCLID_CAPTURE_MARKER in content:
        logger.info("GCLID capture já instalado no theme.liquid — skip")
        return {"ok": True, "already_installed": True}

    # Injetar antes de </body>
    if "</body>" in content:
        content = content.replace("</body>", _GCLID_CAPTURE_SCRIPT + "\n</body>", 1)
    elif "</head>" in content:
        content = content.replace("</head>", _GCLID_CAPTURE_SCRIPT + "\n</head>", 1)
    else:
        content += "\n" + _GCLID_CAPTURE_SCRIPT

    # Salvar theme.liquid atualizado
    r2 = requests.put(
        f"https://{STORE}/admin/api/2024-01/themes/{tema_id}/assets.json",
        json={"asset": {"key": "layout/theme.liquid", "value": content}},
        headers=HEADERS, timeout=20
    )
    if r2.status_code not in (200, 201):
        raise RuntimeError(f"Erro ao salvar theme.liquid: {r2.status_code} {r2.text[:200]}")

    logger.info("GCLID capture instalado no theme.liquid (tema %s)", tema_id)
    return {"ok": True, "already_installed": False}


def _instalar_gtag_conversion(token: str):
    """
    Instala rastreamento de conversão Google Ads em duas camadas:
    1. GCLID capture no theme.liquid (captura parâmetro ?gclid= e propaga via cart attribute)
    2. Web Pixel via API (dispara gtag na página thank_you do novo checkout Shopify)
    3. Fallback: asset JS + script_tag (funciona em páginas do tema, não no checkout novo)

    Nota: o novo checkout Shopify (Checkout Extensibility, obrigatório desde 2024) NÃO executa
    script_tags com display_scope:'all'. A conversão principal é server-side via webhook orders/paid
    que lê o gclid do note_attribute e faz o upload via Google Ads Offline Conversion API.
    """
    STORE   = f"{SHOPIFY_STORE}.myshopify.com"
    BASE    = f"https://{STORE}/admin/api/2024-01"
    HEADERS = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    TEMA_ID = 185169445169
    AW_ID   = "AW-2097362078"
    CONV_ID = "7227407944"

    result = {}

    # ── 1. GCLID capture no theme.liquid ────────────────────────────────────
    try:
        result["gclid_capture"] = _instalar_gclid_capture(token, TEMA_ID)
    except Exception as ex:
        logger.warning("Falha ao instalar GCLID capture: %s", ex)
        result["gclid_capture"] = {"ok": False, "erro": str(ex)}

    # ── 2. Web Pixel via API (novo checkout) ────────────────────────────────
    pixel_js = f"""
analytics.subscribe('checkout_completed', function(event) {{
  var order = event.data && event.data.checkout;
  var val = order ? parseFloat(order.totalPrice && order.totalPrice.amount || '0') : 0;
  var tid = order ? (order.order && order.order.id || '') : '';
  // Limpar prefixo gid://shopify/Order/
  tid = String(tid).replace(/^gid:\\/\\/[^/]+\\/[^/]+\\//, '');

  // Carregar gtag.js e disparar conversão
  browser.loadScript('https://www.googletagmanager.com/gtag/js?id={AW_ID}').then(function() {{
    var dl = []; function gtag(){{ dl.push(arguments); }}
    gtag('js', new Date());
    gtag('config', '{AW_ID}', {{send_page_view: false}});
    gtag('event', 'conversion', {{
      send_to: '{AW_ID}/{CONV_ID}',
      value: val,
      currency: 'BRL',
      transaction_id: tid
    }});
  }}).catch(function(e){{ console.warn('[Shinsei] gtag load error', e); }});
}});
"""

    try:
        # Verificar se já existe um pixel Shinsei
        existing_pixels = requests.get(f"{BASE}/web_pixels.json", headers=HEADERS, timeout=10)
        pixels = existing_pixels.json().get("web_pixels", []) if existing_pixels.status_code == 200 else []
        shinsei_pixel = next((p for p in pixels if "Shinsei" in p.get("settings", {}).get("name", "")
                              or "Shinsei" in str(p.get("settings", ""))), None)

        if shinsei_pixel:
            # Atualizar pixel existente
            pid = shinsei_pixel["id"]
            r_upd = requests.put(
                f"{BASE}/web_pixels/{pid}.json",
                json={"web_pixel": {"enabled": True, "settings": json.dumps({"name": "Shinsei GAds", "code": pixel_js})}},
                headers=HEADERS, timeout=10
            )
            result["web_pixel"] = {"action": "updated", "id": pid, "status": r_upd.status_code}
        else:
            r_px = requests.post(
                f"{BASE}/web_pixels.json",
                json={"web_pixel": {"enabled": True, "settings": json.dumps({"name": "Shinsei GAds", "code": pixel_js})}},
                headers=HEADERS, timeout=10
            )
            result["web_pixel"] = {"action": "created", "status": r_px.status_code,
                                   "body": r_px.json() if r_px.status_code in (200, 201) else r_px.text[:200]}
    except Exception as ex:
        logger.warning("Web Pixel API não disponível (pode precisar de re-auth): %s", ex)
        result["web_pixel"] = {"ok": False, "erro": str(ex)}

    # ── 3. Asset JS + Script Tag (fallback — funciona nas páginas do tema) ──
    # O Shopify novo checkout não executa script_tags, mas mantemos para
    # páginas de produto/carrinho/conta onde o gtag pode ser útil para remarketing.
    gtag_js = f"""// Shinsei Market — Google Ads gtag loader (tema, não checkout)
// Carrega gtag.js para páginas do tema (produto, carrinho, conta).
// O checkout usa Web Pixel API; a conversão principal é server-side via webhook.
(function() {{
  if (window._shinseiGtagLoaded) return;
  window._shinseiGtagLoaded = true;
  var AW = '{AW_ID}';
  window.dataLayer = window.dataLayer || [];
  window.gtag = window.gtag || function() {{ window.dataLayer.push(arguments); }};
  window.gtag('js', new Date());
  window.gtag('config', AW, {{send_page_view: false}});
  var s = document.createElement('script');
  s.async = true;
  s.src = 'https://www.googletagmanager.com/gtag/js?id=' + AW;
  document.head.appendChild(s);
}})();
"""

    asset_r = requests.put(
        f"https://{STORE}/admin/api/2024-01/themes/{TEMA_ID}/assets.json",
        json={"asset": {"key": "assets/shinsei-ads-conversion.js", "value": gtag_js}},
        headers=HEADERS, timeout=15
    )
    if asset_r.status_code not in (200, 201):
        logger.warning("Erro ao salvar asset JS: %s %s", asset_r.status_code, asset_r.text[:100])
        result["asset"] = {"ok": False, "status": asset_r.status_code}
    else:
        public_url = asset_r.json().get("asset", {}).get("public_url", "")
        # Remover script tags antigas duplicadas
        st_list = requests.get(f"{BASE}/script_tags.json?limit=50", headers=HEADERS, timeout=10).json()
        for st in st_list.get("script_tags", []):
            if "shinsei-ads" in st.get("src", ""):
                requests.delete(f"{BASE}/script_tags/{st['id']}.json", headers=HEADERS, timeout=10)
        # Criar script tag atualizada
        st_r = requests.post(
            f"{BASE}/script_tags.json",
            json={"script_tag": {"event": "onload", "src": public_url,
                                 "display_scope": "online_store", "cache": False}},
            headers=HEADERS, timeout=10
        )
        result["asset"] = {"ok": True, "public_url": public_url,
                           "script_tag_status": st_r.status_code}
        logger.info("gtag asset reinstalado: %s", public_url)

    return result
