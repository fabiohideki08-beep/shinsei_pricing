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

        self._last_request_ts = 0.0
        self._min_interval = 0.40

    # ============================================================
    # JSON local
    # ============================================================
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

    # ============================================================
    # Tokens
    # ============================================================
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

    # ============================================================
    # Config
    # ============================================================
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

    # ============================================================
    # OAuth
    # ============================================================
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

    # ============================================================
    # Helpers de request
    # ============================================================
    def _respect_rate_limit(self) -> None:
        now = time.time()
        elapsed = now - self._last_request_ts
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_ts = time.time()

    def _get(self, path: str, params: dict | None = None, retries: int = 2) -> dict:
        url = f"{self.base_url}{path}"

        for attempt in range(retries + 1):
            self._respect_rate_limit()

            response = requests.get(
                url,
                headers=self._get_headers(),
                params=params or {},
                timeout=30,
            )

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

            response = requests.put(
                url,
                headers=self._get_headers(),
                json=payload,
                timeout=30,
            )

            if response.status_code in (200, 201):
                return response.json()

            if response.status_code == 429 and attempt < retries:
                time.sleep(1.2 + attempt * 0.8)
                continue

            raise BlingAPIError(response.text)

        raise BlingAPIError("Falha inesperada na requisição PUT.")

    def _normalize_product(self, item: dict) -> dict:
        if not isinstance(item, dict):
            return {}
        return item.get("produto", item)

    def _extract_product_data(self, payload: dict) -> dict:
        """
        Normaliza respostas do Bling para um dicionário de produto simples.
        """
        if not isinstance(payload, dict):
            return {}

        if isinstance(payload.get("data"), dict):
            return self._normalize_product(payload["data"])

        if isinstance(payload.get("data"), list) and payload["data"]:
            return self._normalize_product(payload["data"][0])

        return self._normalize_product(payload)

    # ============================================================
    # Produtos
    # ============================================================
    def list_products(self, page: int = 1, limit: int = 100) -> dict:
        return self._get(
            "/produtos",
            params={
                "pagina": page,
                "limite": limit,
            },
        )

    def get_product(self, product_id: int) -> dict:
        payload = self._get(f"/produtos/{int(product_id)}")
        return self._extract_product_data(payload)

    def _search_products_light(
        self,
        matcher,
        limit: int = 100,
        max_pages: int = 3,
    ) -> list[dict]:
        encontrados: list[dict] = []

        for page in range(1, max_pages + 1):
            payload = self.list_products(page=page, limit=limit)
            data = payload.get("data", [])

            if not data:
                break

            for item in data:
                prod = self._normalize_product(item)
                if matcher(prod):
                    encontrados.append(prod)

            if encontrados:
                return encontrados

            if len(data) < limit:
                break

        return encontrados

    def get_product_by_sku(self, sku: str) -> dict:
        sku = str(sku).strip().lower()

        encontrados = self._search_products_light(
            lambda prod: str(prod.get("codigo") or prod.get("sku") or "").strip().lower() == sku,
            max_pages=3,
        )

        if not encontrados:
            return {"encontrado": False}

        return {
            "encontrado": True,
            "quantidade": len(encontrados),
            "produto": encontrados[0],
            "produtos": encontrados[:10],
        }

    def get_product_by_ean(self, ean: str) -> dict:
        ean = str(ean).strip()

        encontrados = self._search_products_light(
            lambda prod: str(
                prod.get("gtin")
                or prod.get("gtinEan")
                or prod.get("codigoBarras")
                or prod.get("codigo_barras")
                or prod.get("ean")
                or ""
            ).strip() == ean,
            max_pages=3,
        )

        if not encontrados:
            return {"encontrado": False}

        return {
            "encontrado": True,
            "quantidade": len(encontrados),
            "produto": encontrados[0],
            "produtos": encontrados[:10],
        }

    def get_product_by_name(self, nome: str) -> dict:
        nome = str(nome).strip().lower()

        encontrados = self._search_products_light(
            lambda prod: nome in str(prod.get("nome") or prod.get("descricao") or "").strip().lower(),
            max_pages=2,
        )

        if not encontrados:
            return {"encontrado": False, "quantidade": 0}

        return {
            "encontrado": True,
            "quantidade": len(encontrados),
            "produto": encontrados[0],
            "produtos": encontrados[:10],
        }

    # ============================================================
    # Atualização
    # ============================================================
    def update_product(self, product_id: int, payload: dict) -> dict:
        return self._put(f"/produtos/{int(product_id)}", payload)