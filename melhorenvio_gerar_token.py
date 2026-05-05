# -*- coding: utf-8 -*-
"""
melhorenvio_gerar_token.py
Faz o fluxo OAuth do Melhor Envio para obter access_token + refresh_token.
Abre o navegador para autorização e captura o código automaticamente.

Uso:
  python melhorenvio_gerar_token.py
"""
import json
import os
import sys
import time
import webbrowser
import urllib.parse
import http.server
import threading
from pathlib import Path

# ── Credenciais do app Melhor Envio ──────────────────────────────────────────
CLIENT_ID     = "24512"
CLIENT_SECRET = "47aN6TyrEsob8q8SGVddV6InmYUGo9Rwh4EHBrXi"
REDIRECT_URI  = "http://localhost:8765/callback"
SANDBOX       = False   # True = sandbox, False = produção

BASE_AUTH = "https://app.melhorenvio.com.br" if not SANDBOX else "https://sandbox.melhorenvio.com.br"
BASE_API  = "https://www.melhorenvio.com.br/api/v2" if not SANDBOX else "https://sandbox.melhorenvio.com.br/api/v2"

SCOPES = [
    "cart-read",
    "cart-write",
    "companies-read",
    "coupons-read",
    "notifications-read",
    "orders-read",
    "products-read",
    "purchases-read",
    "shipping-calculate",
    "shipping-cancel",
    "shipping-checkout",
    "shipping-generate",
    "shipping-preview",
    "shipping-print",
    "shipping-read",
    "shipping-tracking",
    "transactions-read",
    "users-read",
]

TOKEN_PATH = Path("data/melhorenvio_token.json")
TOKEN_PATH.parent.mkdir(exist_ok=True)

def pr(m=""): sys.stdout.buffer.write((str(m)+"\n").encode("utf-8","replace")); sys.stdout.buffer.flush()

# ── Servidor local para capturar o callback OAuth ─────────────────────────────
_auth_code = None

class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global _auth_code
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if code:
            _auth_code = code
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h2>Autorizado! Pode fechar esta aba.</h2>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<h2>Erro: code nao recebido.</h2>")
    def log_message(self, *a): pass

def _start_server():
    server = http.server.HTTPServer(("localhost", 8765), _Handler)
    server.handle_request()

# ── Fluxo principal ───────────────────────────────────────────────────────────
pr("=" * 60)
pr("  Melhor Envio — Geração de Token OAuth")
pr("=" * 60)

# 1. Monta URL de autorização
auth_params = {
    "client_id":     CLIENT_ID,
    "redirect_uri":  REDIRECT_URI,
    "response_type": "code",
    "scope":         " ".join(SCOPES),
}
auth_url = f"{BASE_AUTH}/oauth/authorize?" + urllib.parse.urlencode(auth_params)

pr()
pr("1. Abrindo navegador para autorização...")
pr(f"   URL: {auth_url}")
pr()

# Inicia servidor local em background
t = threading.Thread(target=_start_server, daemon=True)
t.start()
time.sleep(0.3)

webbrowser.open(auth_url)

pr("   Aguardando autorização no navegador...")
for _ in range(120):
    if _auth_code:
        break
    time.sleep(0.5)

if not _auth_code:
    pr("  ERRO: Tempo esgotado. Tente novamente.")
    sys.exit(1)

pr(f"  Code recebido: {_auth_code[:8]}...")

# 2. Troca code por access_token
pr()
pr("2. Trocando code por access_token...")
import urllib.request

token_data = urllib.parse.urlencode({
    "grant_type":    "authorization_code",
    "client_id":     CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "redirect_uri":  REDIRECT_URI,
    "code":          _auth_code,
}).encode()

req = urllib.request.Request(
    f"{BASE_AUTH}/oauth/token",
    data=token_data,
    headers={"Content-Type": "application/x-www-form-urlencoded",
             "Accept": "application/json",
             "User-Agent": "ShinseMarket/1.0"},
    method="POST"
)

try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        token_resp = json.loads(resp.read().decode())
except Exception as e:
    pr(f"  ERRO na troca de token: {e}")
    sys.exit(1)

access_token  = token_resp.get("access_token", "")
refresh_token = token_resp.get("refresh_token", "")
expires_in    = token_resp.get("expires_in", 0)

if not access_token:
    pr(f"  ERRO: resposta sem access_token: {token_resp}")
    sys.exit(1)

pr(f"  access_token : {access_token[:12]}...  ({len(access_token)} chars)")
pr(f"  refresh_token: {refresh_token[:12]}...  ({len(refresh_token)} chars)")
pr(f"  expires_in   : {expires_in}s ({expires_in//3600}h)")

# 3. Salva token
TOKEN_PATH.write_text(json.dumps({
    "access_token":  access_token,
    "refresh_token": refresh_token,
    "expires_in":    expires_in,
    "client_id":     CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "sandbox":       SANDBOX,
    "gerado_em":     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}, indent=2, ensure_ascii=False), encoding="utf-8")

pr()
pr(f"  Token salvo em: {TOKEN_PATH}")
pr()
pr("  Para usar no Railway, adicione a variável:")
pr(f"    MELHOR_ENVIO_TOKEN={access_token}")
pr("    MELHOR_ENVIO_SANDBOX=false")
pr()
pr("  ✅ Concluído!")
