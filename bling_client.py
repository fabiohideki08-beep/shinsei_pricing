from __future__ import annotations

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


class BlingClient:
    def __init__(self):
        self.client_id = os.getenv("BLING_CLIENT_ID")
        self.client_secret = os.getenv("BLING_CLIENT_SECRET")
        self.redirect_uri = os.getenv("BLING_REDIRECT_URI")
        self.base_url = "https://api.bling.com.br/Api/v3"
        self.tokens = self._load_tokens()

    def _load_json(self, path: Path, default: dict | None = None) -> dict:
        default = default or {}
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _save_json(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_tokens(self) -> dict:
        return self._load_json(TOKEN_PATH, {})

    def _save_tokens(self, data: dict) -> None:
        self._save_json(TOKEN_PATH, data)
        self.tokens = data

    def has_local_tokens(self) -> bool:
        return bool(self.tokens.get("access_token"))

    def _token_expired(self) -> bool:
        expires_at = self.tokens.get("expires_at", 0)
        return time.time() >= float(expires_at or 0)

    def _ensure_config(self) -> None:
        missing = []
        if not self.client_id:
            missing.append("BLING_CLIENT_ID")
        if not self.client_secret:
            missing.append("BLING_CLIENT_SECRET")
        if not self.redirect_uri:
            missing.append("BLING_REDIRECT_URI")
        if missing:
            raise BlingConfigError(f"Variáveis ausentes no .env: {', '.join(missing)}")

    def build_authorize_url(self) -> str:
        self._ensure_config()

        state = secrets.token_urlsafe(32)
        self._save_json(
            OAUTH_STATE_PATH,
            {
                "state": state,
                "created_at": int(time.time()),
            },
        )

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
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            },
            auth=(self.client_id, self.client_secret),
            timeout=30,
        )

        if response.status_code != 200:
            raise BlingAuthError(f"Erro ao obter token: {response.text}")

        data = response.json()
        data["expires_at"] = time.time() + int(data.get("expires_in", 0))
        self._save_tokens(data)
        return data

    def _refresh_token(self) -> None:
        self._ensure_config()

        refresh_token = self.tokens.get("refresh_token")
        if not refresh_token:
            raise BlingAuthError("Refresh token ausente. Refaça a autorização em /bling/auth.")

        url = "https://www.bling.com.br/Api/v3/oauth/token"
        response = requests.post(
            url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
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

    def list_products(self) -> dict:
        url = f"{self.base_url}/produtos"
        response = requests.get(url, headers=self._get_headers(), timeout=30)

        if response.status_code != 200:
            raise BlingAPIError(response.text)

        return response.json()

    def get_product(self, product_id: int) -> dict:
        url = f"{self.base_url}/produtos/{product_id}"
        response = requests.get(url, headers=self._get_headers(), timeout=30)

        if response.status_code != 200:
            raise BlingAPIError(response.text)

        return response.json()

    def get_product_by_sku(self, sku: str) -> dict:
        produtos = self.list_products().get("data", [])

        for item in produtos:
            prod = item.get("produto", item)
            if str(prod.get("codigo")) == str(sku):
                return {"produto": prod}

        return {"encontrado": False}

    def get_product_by_ean(self, ean: str) -> dict:
        produtos = self.list_products().get("data", [])

        for item in produtos:
            prod = item.get("produto", item)
            if str(prod.get("gtin")) == str(ean):
                return {"produto": prod}

        return {"encontrado": False}

    def update_product(self, product_id: int, payload: dict) -> dict:
        url = f"{self.base_url}/produtos/{product_id}"

        response = requests.put(
            url,
            headers=self._get_headers(),
            json=payload,
            timeout=30,
        )

        if response.status_code not in (200, 201):
            raise BlingAPIError(response.text)

        return response.json()