from __future__ import annotations

import requests
import time
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
TOKEN_PATH = DATA_DIR / "bling_tokens.json"

DATA_DIR.mkdir(exist_ok=True)


class BlingClient:
    def __init__(self):
        self.client_id = os.getenv("BLING_CLIENT_ID")
        self.client_secret = os.getenv("BLING_CLIENT_SECRET")
        self.redirect_uri = os.getenv("BLING_REDIRECT_URI")

        self.base_url = "https://api.bling.com.br/Api/v3"

        self.tokens = self._load_tokens()

    # ============================================================
    # Token handling
    # ============================================================
    def _load_tokens(self):
        if TOKEN_PATH.exists():
            try:
                return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
            except:
                return {}
        return {}

    def _save_tokens(self, data):
        TOKEN_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.tokens = data

    def has_local_tokens(self):
        return bool(self.tokens.get("access_token"))

    def _token_expired(self):
        expires_at = self.tokens.get("expires_at", 0)
        return time.time() > expires_at

    def _refresh_token(self):
        refresh_token = self.tokens.get("refresh_token")

        url = "https://www.bling.com.br/Api/v3/oauth/token"

        response = requests.post(
            url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            auth=(self.client_id, self.client_secret),
        )

        if response.status_code != 200:
            raise Exception(f"Erro ao renovar token: {response.text}")

        data = response.json()

        data["expires_at"] = time.time() + data["expires_in"]

        self._save_tokens(data)

    def _get_headers(self):
        if not self.tokens:
            raise Exception("Cliente não autenticado com Bling.")

        if self._token_expired():
            self._refresh_token()

        return {
            "Authorization": f"Bearer {self.tokens['access_token']}",
            "Content-Type": "application/json",
        }

    # ============================================================
    # OAuth
    # ============================================================
    def build_authorize_url(self):
        return (
            "https://www.bling.com.br/Api/v3/oauth/authorize?"
            f"response_type=code&client_id={self.client_id}&redirect_uri={self.redirect_uri}"
        )

    def exchange_code_for_token(self, code):
        url = "https://www.bling.com.br/Api/v3/oauth/token"

        response = requests.post(
            url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            },
            auth=(self.client_id, self.client_secret),
        )

        if response.status_code != 200:
            raise Exception(f"Erro ao obter token: {response.text}")

        data = response.json()
        data["expires_at"] = time.time() + data["expires_in"]

        self._save_tokens(data)
        return data

    # ============================================================
    # Produtos
    # ============================================================
    def list_products(self):
        url = f"{self.base_url}/produtos"
        response = requests.get(url, headers=self._get_headers())

        if response.status_code != 200:
            raise Exception(response.text)

        return response.json()

    def get_product(self, product_id: int):
        url = f"{self.base_url}/produtos/{product_id}"
        response = requests.get(url, headers=self._get_headers())

        if response.status_code != 200:
            raise Exception(response.text)

        return response.json()

    def get_product_by_sku(self, sku: str):
        produtos = self.list_products().get("data", [])

        for item in produtos:
            prod = item.get("produto", item)
            if str(prod.get("codigo")) == str(sku):
                return {"produto": prod}

        return {"encontrado": False}

    def get_product_by_ean(self, ean: str):
        produtos = self.list_products().get("data", [])

        for item in produtos:
            prod = item.get("produto", item)
            if str(prod.get("gtin")) == str(ean):
                return {"produto": prod}

        return {"encontrado": False}

    # ============================================================
    # Update produto (base)
    # ============================================================
    def update_product(self, product_id: int, payload: dict):
        url = f"{self.base_url}/produtos/{product_id}"

        response = requests.put(
            url,
            headers=self._get_headers(),
            json=payload,
        )

        if response.status_code not in (200, 201):
            raise Exception(response.text)

        return response.json()