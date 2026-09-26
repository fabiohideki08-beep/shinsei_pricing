# -*- coding: utf-8 -*-
"""
instalar_gtag_checkout.py
Instala o snippet de conversão do Google Ads na página de confirmação
de pedido do Shopify via Script Tags API (display_scope=order_status).

Execute APÓS re-autorizar o Shopify em /shopify/auth com os novos escopos.
"""
import sys, json, urllib.request, urllib.error
from pathlib import Path

def pr(s): sys.stdout.buffer.write((str(s)+"\n").encode("utf-8","replace")); sys.stdout.flush()

cfg   = json.loads(Path("data/shopify_config.json").read_text("utf-8"))
TOKEN = cfg["access_token"]
STORE = "pknw4n-eg.myshopify.com"
BASE  = f"https://{STORE}/admin/api/2024-01"
HEADS = {"X-Shopify-Access-Token": TOKEN, "Content-Type": "application/json"}

AW_ID      = "AW-2097362078"
CONV_LABEL = "7604222139"    # ID da conversão criada: Compra — Shinsei Market (Shopify)
SCRIPT_SRC = "https://shinsei-ads-conversion.netlify.app/gtag-conversion.js"

def api(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(f"{BASE}{path}", data=data, headers=HEADS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return {"error": e.reason, "body": e.read().decode()[:400]}, e.code

# ── Verificar escopo atual ────────────────────────────────────────────────────
pr(f"\n{'='*65}")
pr("INSTALAÇÃO DO SNIPPET DE CONVERSÃO — Shopify")
pr(f"{'='*65}")
pr(f"\nEscopos do token: {cfg.get('scope','?')}")

if "write_script_tags" not in cfg.get("scope",""):
    pr("\n❌ Token sem write_script_tags.")
    pr("   1. Acesse a URL de re-auth da sua loja Railway: /shopify/auth")
    pr("   2. Autorize os novos escopos")
    pr("   3. Execute este script novamente")
    sys.exit(1)

# ── Listar script tags existentes e remover antigas de conversão ──────────────
pr("\nVerificando script tags existentes...")
result, code = api("GET", "/script_tags.json?limit=50")
if code != 200:
    pr(f"❌ Erro ao listar script tags: {result}")
    sys.exit(1)

existing = result.get("script_tags", [])
pr(f"Script tags existentes: {len(existing)}")

# Remover qualquer script tag antiga de conversão do Google Ads
for st in existing:
    src = st.get("src","")
    if "AW-" in src or "gtag" in src.lower() or "ads-conversion" in src.lower():
        del_result, del_code = api("DELETE", f"/script_tags/{st['id']}.json")
        pr(f"  Removida tag antiga: {src[:60]} (status={del_code})")

# ── Criar script inline via asset do tema ────────────────────────────────────
# Script tags apontam para URL externa — usamos um script hospedado inline
# Como a Script Tag API não suporta código inline, hospedamos no próprio tema
# via snippets/gtag-conversion.liquid e injetamos no checkout

# Abordagem alternativa: usar o Shopify Web Pixel / Customer Event
# Mas o mais simples para o plano Professional é injetar via Script Tag
# apontando para um JS externo que dispara o evento apenas no thank_you

# Criar o arquivo JS no tema (assets)
TEMA_ID = 185169445169

conversion_js = f"""// Google Ads Conversion — Shinsei Market
// Dispara apenas na pagina de confirmacao de pedido (thank_you)
(function() {{
  if (typeof Shopify === 'undefined') return;
  if (!Shopify.Checkout) return;
  if (Shopify.Checkout.step !== 'thank_you') return;

  // Verificar se ja foi disparado (evita duplo disparo)
  if (window._gadsConvFired) return;
  window._gadsConvFired = true;

  // Aguardar gtag estar disponivel
  function dispararConversao() {{
    if (typeof gtag === 'undefined') {{
      setTimeout(dispararConversao, 200);
      return;
    }}
    var orderTotal = (window.Shopify && Shopify.checkout && Shopify.checkout.total_price)
      ? (Shopify.checkout.total_price / 100).toFixed(2)
      : 0;
    var orderId = (window.Shopify && Shopify.checkout && Shopify.checkout.order_id)
      ? Shopify.checkout.order_id
      : '';

    gtag('event', 'conversion', {{
      'send_to': '{AW_ID}/{CONV_LABEL}',
      'value': parseFloat(orderTotal),
      'currency': 'BRL',
      'transaction_id': String(orderId)
    }});

    console.log('[Shinsei] Google Ads conversion fired:', orderTotal, orderId);
  }}

  dispararConversao();
}})();
"""

# Salvar como asset no tema
pr(f"\nSalvando conversion.js no tema (ID={TEMA_ID})...")
HEADS_THEME = {"X-Shopify-Access-Token": TOKEN, "Content-Type": "application/json"}
asset_body = {
    "asset": {
        "key": "assets/shinsei-ads-conversion.js",
        "value": conversion_js
    }
}
asset_req = urllib.request.Request(
    f"https://{STORE}/admin/api/2024-01/themes/{TEMA_ID}/assets.json",
    data=json.dumps(asset_body).encode(),
    headers=HEADS_THEME,
    method="PUT"
)
try:
    with urllib.request.urlopen(asset_req, timeout=15) as r:
        asset_resp = json.loads(r.read())
        asset_url = f"https://cdn.shopify.com/s/files/1/0942/1386/5777/assets/shinsei-ads-conversion.js"
        # URL real baseada no public_url do asset
        public_url = asset_resp.get("asset",{}).get("public_url","")
        pr(f"  Asset salvo: {public_url or 'assets/shinsei-ads-conversion.js'}")
        asset_url = public_url if public_url else asset_url
except urllib.error.HTTPError as e:
    pr(f"  Erro ao salvar asset: {e.code} {e.read().decode()[:200]}")
    sys.exit(1)

# ── Criar Script Tag apontando para o asset ───────────────────────────────────
pr(f"\nCriando Script Tag (order_status)...")
tag_body = {
    "script_tag": {
        "event":          "onload",
        "src":            asset_url,
        "display_scope":  "order_status",
        "cache":          False
    }
}
result2, code2 = api("POST", "/script_tags.json", tag_body)
if code2 in (200, 201):
    tag = result2.get("script_tag",{})
    pr(f"\n✅ Script Tag instalada com sucesso!")
    pr(f"   ID:    {tag.get('id')}")
    pr(f"   Scope: {tag.get('display_scope')}")
    pr(f"   Src:   {tag.get('src','')[:80]}")
    pr(f"\n   O evento de conversão do Google Ads vai disparar")
    pr(f"   automaticamente em cada pagina de confirmacao de pedido.")
else:
    pr(f"\n❌ Erro ao criar Script Tag: {code2}")
    pr(f"   {result2}")

pr(f"\n{'='*65}\n")
