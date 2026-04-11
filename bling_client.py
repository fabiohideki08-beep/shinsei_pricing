from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")
DATA_DIR = BASE_DIR / "data"
TOKEN_PATH = DATA_DIR / "bling_tokens.json"
OAUTH_STATE_PATH = DATA_DIR / "bling_oauth_state.json"
DATA_DIR.mkdir(exist_ok=True)


class BlingAPIError(Exception):
    pass


class BlingAuthError(BlingAPIError):
    pass


class BlingConfigError(BlingAPIError):
    pass


# ─────────────────────────────────────────────
# Token encryption helpers (XOR + base64)
# ─────────────────────────────────────────────

def _derive_key() -> bytes:
    secret = (os.getenv("BLING_CLIENT_SECRET") or "") + (os.getenv("BLING_CLIENT_ID") or "token-key")
    return hashlib.sha256(secret.encode()).digest()


def _xor_crypt(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _encrypt_tokens(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False).encode()
    return base64.b64encode(_xor_crypt(raw, _derive_key())).decode()


def _decrypt_tokens(encoded: str) -> dict:
    raw = _xor_crypt(base64.b64decode(encoded.encode()), _derive_key())
    return json.loads(raw.decode())


# ─────────────────────────────────────────────
# BlingClient
# ─────────────────────────────────────────────

class BlingClient:
    def __init__(self):
        self.client_id = os.getenv("BLING_CLIENT_ID")
        self.client_secret = os.getenv("BLING_CLIENT_SECRET")
        self.redirect_uri = os.getenv("BLING_REDIRECT_URI")
        self.base_url = "https://api.bling.com.br/Api/v3"
        self.tokens = self._load_tokens()
        self._last_request_ts = 0.0
        self._min_interval = 0.40

    # ── Persistência ──────────────────────────

    def _load_json(self, path: Path, default=None):
        default = default or {}
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _save_json(self, path: Path, data: dict):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_tokens(self) -> dict:
        raw = self._load_json(TOKEN_PATH, {})
        if "encrypted" in raw:
            try:
                return _decrypt_tokens(raw["encrypted"])
            except Exception:
                return {}
        return raw  # fallback legado (texto plano)

    def _save_tokens(self, data: dict):
        self._save_json(TOKEN_PATH, {"encrypted": _encrypt_tokens(data)})
        self.tokens = data

    def has_local_tokens(self) -> bool:
        return bool(self.tokens.get("access_token"))

    def _token_expired(self) -> bool:
        return time.time() >= float(self.tokens.get("expires_at", 0) or 0)

    # ── Config / Auth ─────────────────────────

    def _ensure_config(self):
        missing = [k for k, v in {
            "BLING_CLIENT_ID": self.client_id,
            "BLING_CLIENT_SECRET": self.client_secret,
            "BLING_REDIRECT_URI": self.redirect_uri,
        }.items() if not v]
        if missing:
            raise BlingConfigError(f"Variáveis ausentes no .env: {', '.join(missing)}")

    def build_authorize_url(self) -> str:
        self._ensure_config()
        state = secrets.token_urlsafe(32)
        self._save_json(OAUTH_STATE_PATH, {"state": state, "created_at": int(time.time())})
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
        }
        return f"https://www.bling.com.br/Api/v3/oauth/authorize?{urlencode(params)}"

    def exchange_code_for_token(self, code: str, state: str | None = None) -> dict:
        self._ensure_config()
        saved = self._load_json(OAUTH_STATE_PATH, {})
        expected_state = saved.get("state")
        if expected_state:
            if not state:
                raise BlingAuthError("O Bling retornou sem o parâmetro state.")
            if state != expected_state:
                raise BlingAuthError("State OAuth inválido. Tente autorizar novamente em /bling/auth.")
        url = "https://www.bling.com.br/Api/v3/oauth/token"
        response = requests.post(
            url,
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri},
            auth=(self.client_id, self.client_secret),
            timeout=30,
        )
        if response.status_code != 200:
            raise BlingAuthError(f"Erro ao obter token: {response.text}")
        data = response.json()
        data["expires_at"] = time.time() + int(data.get("expires_in", 0))
        self._save_tokens(data)
        return data

    def _refresh_token(self):
        self._ensure_config()
        refresh_token = self.tokens.get("refresh_token")
        if not refresh_token:
            raise BlingAuthError("Refresh token ausente. Refaça a autorização em /bling/auth.")
        url = "https://www.bling.com.br/Api/v3/oauth/token"
        response = requests.post(
            url,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth=(self.client_id, self.client_secret),
            timeout=30,
        )
        if response.status_code != 200:
            raise BlingAuthError(f"Erro ao renovar token: {response.text}")
        data = response.json()
        data["expires_at"] = time.time() + int(data.get("expires_in", 0))
        self._save_tokens(data)

    def _get_headers(self) -> dict:
        if not self.tokens:
            raise BlingAuthError("Cliente não autenticado com Bling.")
        if self._token_expired():
            self._refresh_token()
        return {
            "Authorization": f"Bearer {self.tokens['access_token']}",
            "Content-Type": "application/json",
        }

    # ── Rate limit ────────────────────────────

    def _respect_rate_limit(self):
        now = time.time()
        elapsed = now - self._last_request_ts
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_ts = time.time()

    # ── HTTP ──────────────────────────────────

    def _get(self, path: str, params=None, retries: int = 2) -> dict:
        url = f"{self.base_url}{path}"
        for attempt in range(retries + 1):
            self._respect_rate_limit()
            response = requests.get(url, headers=self._get_headers(), params=params or {}, timeout=30)
            if response.status_code == 200:
                return response.json()
            if response.status_code == 429 and attempt < retries:
                time.sleep(1.2 + attempt * 0.8)
                continue
            raise BlingAPIError(response.text)
        raise BlingAPIError("Falha inesperada na requisição GET.")

    def _put(self, path: str, payload: dict, retries: int = 2) -> dict:
        url = f"{self.base_url}{path}"
        for attempt in range(retries + 1):
            self._respect_rate_limit()
            response = requests.put(url, headers=self._get_headers(), json=payload, timeout=30)
            if response.status_code in (200, 201):
                return response.json()
            if response.status_code == 429 and attempt < retries:
                time.sleep(1.2 + attempt * 0.8)
                continue
            raise BlingAPIError(response.text)
        raise BlingAPIError("Falha inesperada na requisição PUT.")

    # ── Normalização ──────────────────────────

    def _normalize_product(self, item: dict) -> dict:
        if not isinstance(item, dict):
            return {}
        return item.get("produto", item)

    def _extract_product_data(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            return {}
        if isinstance(payload.get("data"), dict):
            return self._normalize_product(payload["data"])
        if isinstance(payload.get("data"), list) and payload["data"]:
            return self._normalize_product(payload["data"][0])
        return self._normalize_product(payload)

    # ── Busca de produtos ─────────────────────

    def list_products(self, page: int = 1, limit: int = 100) -> dict:
        return self._get("/produtos", params={"pagina": page, "limite": limit})

    def get_product(self, product_id: int) -> dict:
        payload = self._get(f"/produtos/{int(product_id)}")
        return self._extract_product_data(payload)

    def _hydrate_product(self, prod: dict) -> dict:
        prod = self._normalize_product(prod)
        product_id = prod.get("id")
        if not product_id:
            return prod
        try:
            full = self.get_product(int(product_id))
            if isinstance(full, dict) and full:
                return full
        except Exception:
            pass
        return prod

    # ─────────────────────────────────────────
    # BUSCA DIRETA POR SKU — 1 chamada vs ~300
    # ─────────────────────────────────────────

    def _search_by_codigo(self, sku: str) -> list[dict]:
        """Busca direta usando ?codigo= — uma única chamada à API."""
        try:
            payload = self._get("/produtos", params={"codigo": sku, "limite": 10})
            data = payload.get("data", [])
            return [self._normalize_product(item) for item in data if isinstance(item, dict)]
        except BlingAPIError:
            return []

    def _search_fallback_scan(self, sku: str, max_pages: int = 3, limit: int = 100) -> dict:
        """
        Fallback por varredura de páginas, usado apenas quando a busca direta
        não retorna resultado (catálogos com indexação inconsistente do campo codigo).
        """
        sku_norm = str(sku or "").strip().lower()
        exact_matches, partial_matches, sample_codes = [], [], []
        pages_lidas = 0

        for page in range(1, max_pages + 1):
            payload = self.list_products(page=page, limit=limit)
            data = payload.get("data", [])
            if not data:
                break
            pages_lidas += 1
            for item in data:
                prod = self._normalize_product(item)
                codigo = str(prod.get("codigo") or prod.get("sku") or "").strip()
                nome = str(prod.get("nome") or prod.get("descricao") or "").strip()
                if len(sample_codes) < 20:
                    sample_codes.append({"codigo": codigo, "nome": nome, "id": prod.get("id")})
                codigo_norm = codigo.lower()
                if codigo_norm and codigo_norm == sku_norm:
                    exact_matches.append(prod)
                elif sku_norm and (sku_norm in codigo_norm or codigo_norm in sku_norm):
                    partial_matches.append(prod)
            if len(data) < limit:
                break

        return {
            "exact_matches": exact_matches[:10],
            "partial_matches": partial_matches[:10],
            "sample_codes": sample_codes,
            "pages_lidas": pages_lidas,
        }

    def debug_product_by_sku(self, sku: str) -> dict:
        sku_raw = str(sku or "").strip()
        sku_norm = sku_raw.lower()

        # 1ª tentativa: busca direta por ?codigo= (1 chamada)
        diretos = self._search_by_codigo(sku_raw)
        exact_direct = [p for p in diretos if str(p.get("codigo") or "").strip().lower() == sku_norm]

        if exact_direct:
            return {
                "sku_informado": sku_raw,
                "metodo": "busca_direta",
                "pages_lidas": 1,
                "itens_lidos": len(diretos),
                "exact_matches": [
                    {"id": p.get("id"), "codigo": p.get("codigo"), "nome": p.get("nome")}
                    for p in exact_direct[:10]
                ],
                "partial_matches": [],
                "sample_codes": [],
                "ok": True,
                "mensagem": "SKU encontrado via busca direta (campo Código).",
            }

        # 2ª tentativa: varredura de páginas (fallback)
        scan = self._search_fallback_scan(sku_raw)
        exact_scan = scan["exact_matches"]
        encontrado = bool(exact_scan)

        mensagem = "SKU encontrado no campo Código (SKU)." if encontrado else "SKU não encontrado no campo Código (SKU)."
        if not encontrado and scan["partial_matches"]:
            mensagem += " Existem códigos parecidos no Bling."
        if not encontrado and not scan["sample_codes"]:
            mensagem += " A listagem retornou vazia."

        return {
            "sku_informado": sku_raw,
            "metodo": "varredura_paginas",
            "pages_lidas": scan["pages_lidas"],
            "itens_lidos": scan["pages_lidas"] * 100,
            "exact_matches": [
                {"id": p.get("id"), "codigo": p.get("codigo"), "nome": p.get("nome")}
                for p in exact_scan[:10]
            ],
            "partial_matches": [
                {"id": p.get("id"), "codigo": p.get("codigo"), "nome": p.get("nome")}
                for p in scan["partial_matches"][:10]
            ],
            "sample_codes": scan["sample_codes"],
            "ok": encontrado,
            "mensagem": mensagem,
        }

    def get_product_by_sku(self, sku: str) -> dict:
        report = self.debug_product_by_sku(sku)
        if not report["ok"]:
            return {
                "encontrado": False,
                "erro": "Produto não encontrado por SKU",
                "acao": "Verifique o campo Código (SKU) no Bling.",
                "debug_sku": report,
            }
        prod_meta = report["exact_matches"][0]
        produto = self._hydrate_product({
            "id": prod_meta["id"],
            "codigo": prod_meta["codigo"],
            "nome": prod_meta["nome"],
        })
        return {
            "encontrado": True,
            "quantidade": len(report["exact_matches"]),
            "metodo_busca": report.get("metodo", "desconhecido"),
            "produto": produto,
            "produtos": [produto],
            "debug_sku": report,
        }

    def update_product(self, product_id: int, payload: dict) -> dict:
        return self._put(f"/produtos/{int(product_id)}", payload)
