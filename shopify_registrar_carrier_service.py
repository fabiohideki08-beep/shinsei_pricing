# -*- coding: utf-8 -*-
"""
shopify_registrar_carrier_service.py
Registra (ou atualiza) o Carrier Service do motor de frete Shinsei no Shopify.

Pré-requisito:
  - Token com scope write_shipping (ou read_shipping + write_shipping)
  - API rodando em CALLBACK_URL acessível pela internet (ex: ngrok ou servidor)

Uso:
  python shopify_registrar_carrier_service.py

Para remover o carrier service:
  python shopify_registrar_carrier_service.py --remover <carrier_service_id>
"""
import sys
import json
import time
import requests
from pathlib import Path

# ── Configuração ─────────────────────────────────────────────────────────────
cfg   = json.loads(Path("data/shopify_config.json").read_text(encoding="utf-8"))
TOKEN = cfg["access_token"]
STORE = cfg.get("shop", "pknw4n-eg")
BASE  = f"https://{STORE}.myshopify.com/admin/api/2024-01"
HDR   = {"X-Shopify-Access-Token": TOKEN, "Content-Type": "application/json"}

# !! EDITE AQUI com a URL pública do seu servidor !!
CALLBACK_URL = cfg.get("frete_callback_url", "https://SEU-DOMINIO.com/frete/shopify-callback")

def pr(m=""): sys.stdout.buffer.write((str(m)+"\n").encode("utf-8","replace")); sys.stdout.buffer.flush()
def sec(t):   pr(); pr("="*65); pr(f"  {t}"); pr("="*65)


# ── Listar carrier services existentes ───────────────────────────────────────
sec("1. Carrier Services existentes")
r = requests.get(f"{BASE}/carrier_services.json", headers=HDR, timeout=20)
if r.status_code != 200:
    pr(f"  ERRO {r.status_code}: {r.text[:200]}")
    pr()
    pr("  ⚠️  Verifique se o token tem scope 'write_shipping'.")
    sys.exit(1)

services = r.json().get("carrier_services", [])
pr(f"  Carrier services cadastrados: {len(services)}")
shinsei_id = None
for s in services:
    pr(f"  id={s['id']}  nome={s['name']}  ativo={s['active']}  url={s.get('callback_url','')}")
    if "shinsei" in (s.get("name") or "").lower() or "frete" in (s.get("callback_url") or "").lower():
        shinsei_id = s["id"]

# ── Modo remoção ──────────────────────────────────────────────────────────────
if "--remover" in sys.argv:
    idx = sys.argv.index("--remover")
    remove_id = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else (str(shinsei_id) if shinsei_id else "")
    if not remove_id:
        pr("  Informe o ID: python shopify_registrar_carrier_service.py --remover <id>")
        sys.exit(1)
    sec(f"Removendo carrier service id={remove_id}")
    rd = requests.delete(f"{BASE}/carrier_services/{remove_id}.json", headers=HDR, timeout=20)
    if rd.status_code in (200, 204):
        pr(f"  ✅ Removido com sucesso.")
    else:
        pr(f"  ❌ ERRO {rd.status_code}: {rd.text[:200]}")
    sys.exit(0)

# ── Verificar URL de callback ─────────────────────────────────────────────────
if "SEU-DOMINIO" in CALLBACK_URL:
    pr()
    pr("  ⚠️  CALLBACK_URL não configurada!")
    pr("  Edite a variável CALLBACK_URL (ou adicione 'frete_callback_url' ao shopify_config.json)")
    pr(f"  Valor atual: {CALLBACK_URL}")
    pr()
    pr("  Se estiver testando localmente, use ngrok:")
    pr("    ngrok http 8000")
    pr("    → CALLBACK_URL = 'https://xxxx.ngrok.io/frete/shopify-callback'")
    sys.exit(1)

# ── Criar ou atualizar ────────────────────────────────────────────────────────
payload = {
    "carrier_service": {
        "name": "Shinsei Frete (Motor de Subsídio)",
        "callback_url": CALLBACK_URL,
        "service_discovery": True,
        "carrier_service_type": "api",
        "active": True,
        "format": "json",
    }
}

if shinsei_id:
    sec(f"2. Atualizando carrier service existente (id={shinsei_id})")
    resp = requests.put(
        f"{BASE}/carrier_services/{shinsei_id}.json",
        json=payload, headers=HDR, timeout=20
    )
else:
    sec("2. Criando novo carrier service")
    resp = requests.post(
        f"{BASE}/carrier_services.json",
        json=payload, headers=HDR, timeout=20
    )

if resp.status_code in (200, 201):
    cs = resp.json().get("carrier_service", {})
    pr(f"  ✅ {'Atualizado' if shinsei_id else 'Criado'} com sucesso!")
    pr(f"  id           : {cs.get('id')}")
    pr(f"  nome         : {cs.get('name')}")
    pr(f"  callback_url : {cs.get('callback_url')}")
    pr(f"  ativo        : {cs.get('active')}")
    pr()
    pr("  Próximos passos:")
    pr("  1. No checkout, vá em Configurações > Frete > Zonas de frete")
    pr("  2. O serviço 'Shinsei Frete' aparecerá como opção em cada zona")
    pr("  3. Ative-o substituindo as tarifas fixas existentes")
else:
    pr(f"  ❌ ERRO {resp.status_code}: {resp.text[:400]}")
    if resp.status_code == 422:
        pr()
        pr("  Dica: verifique se a URL é acessível publicamente (sem localhost/127.0.0.1).")
    elif resp.status_code == 403:
        pr()
        pr("  Dica: o token precisa do scope 'write_shipping'.")
    sys.exit(1)
