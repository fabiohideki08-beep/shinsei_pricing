"""
TikTok Shop Open Platform API — cliente com HMAC-SHA256 signing
Docs: https://partner.tiktokshop.com/docv2/page/product
"""
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Optional
import requests

logger = logging.getLogger(__name__)

TIKTOK_BASE_URL = "https://open-api.tiktokglobalshop.com"
TIKTOK_AUTH_URL = "https://services.us.tiktokshop.com/open/authorize"


class TikTokShopClient:

    def __init__(self):
        self.app_key    = os.environ.get("TIKTOK_APP_KEY", "")
        self.app_secret = os.environ.get("TIKTOK_APP_SECRET", "")
        self.access_token = os.environ.get("TIKTOK_ACCESS_TOKEN", "")
        self.shop_id    = os.environ.get("TIKTOK_SHOP_ID", "")

    def _sign(self, path: str, params: dict) -> str:
        """
        Gera assinatura HMAC-SHA256 para chamadas TikTok Shop API v2.
        Regra: HMAC-SHA256(app_secret, app_secret + sorted_params_string + app_secret)
        """
        # Remove campos que não entram na assinatura
        exclude = {"sign", "access_token"}
        sorted_params = sorted(
            [(k, str(v)) for k, v in params.items() if k not in exclude]
        )
        param_str = "".join(f"{k}{v}" for k, v in sorted_params)
        base_str = f"{self.app_secret}{path}{param_str}{self.app_secret}"
        sig = hmac.new(
            self.app_secret.encode("utf-8"),
            base_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return sig

    def _get(self, path: str, extra_params: Optional[dict] = None) -> dict:
        params = {
            "app_key":      self.app_key,
            "timestamp":    int(time.time()),
            "version":      "202309",
            "access_token": self.access_token,
            "shop_id":      self.shop_id,
        }
        if extra_params:
            params.update(extra_params)
        params["sign"] = self._sign(path, params)
        r = requests.get(f"{TIKTOK_BASE_URL}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict, extra_params: Optional[dict] = None) -> dict:
        params = {
            "app_key":      self.app_key,
            "timestamp":    int(time.time()),
            "version":      "202309",
            "access_token": self.access_token,
            "shop_id":      self.shop_id,
        }
        if extra_params:
            params.update(extra_params)
        params["sign"] = self._sign(path, params)
        r = requests.post(
            f"{TIKTOK_BASE_URL}{path}",
            params=params,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def _put(self, path: str, body: dict, extra_params: Optional[dict] = None) -> dict:
        params = {
            "app_key":      self.app_key,
            "timestamp":    int(time.time()),
            "version":      "202309",
            "access_token": self.access_token,
            "shop_id":      self.shop_id,
        }
        if extra_params:
            params.update(extra_params)
        params["sign"] = self._sign(path, params)
        r = requests.put(
            f"{TIKTOK_BASE_URL}{path}",
            params=params,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    # ── OAuth ────────────────────────────────────────────────────────────

    def get_auth_url(self, redirect_uri: str, state: str = "") -> str:
        return (
            f"{TIKTOK_AUTH_URL}"
            f"?app_key={self.app_key}"
            f"&redirect_uri={redirect_uri}"
            f"&state={state}"
        )

    def exchange_code(self, code: str) -> dict:
        path = "/api/token/getByCode"
        params = {
            "app_key":   self.app_key,
            "timestamp": int(time.time()),
            "version":   "202309",
        }
        body = {
            "app_key":    self.app_key,
            "app_secret": self.app_secret,
            "auth_code":  code,
            "grant_type": "authorized_code",
        }
        params["sign"] = self._sign(path, params)
        r = requests.post(
            f"{TIKTOK_BASE_URL}{path}",
            params=params,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def refresh_token(self, refresh_token: str) -> dict:
        path = "/api/token/refresh"
        params = {
            "app_key":   self.app_key,
            "timestamp": int(time.time()),
            "version":   "202309",
        }
        body = {
            "app_key":       self.app_key,
            "app_secret":    self.app_secret,
            "refresh_token": refresh_token,
            "grant_type":    "refresh_token",
        }
        params["sign"] = self._sign(path, params)
        r = requests.post(
            f"{TIKTOK_BASE_URL}{path}",
            params=params,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    # ── Produtos ─────────────────────────────────────────────────────────

    def listar_produtos(self, page_size: int = 100, page_token: str = "") -> dict:
        body = {"page_size": page_size}
        if page_token:
            body["page_token"] = page_token
        return self._post("/api/products/search", body)

    def buscar_produto(self, product_id: str) -> dict:
        return self._get(f"/api/products/{product_id}")

    def atualizar_produto(self, product_id: str, payload: dict) -> dict:
        return self._put(f"/api/products/{product_id}", payload)

    def atualizar_marca_produto(self, product_id: str, brand_id: str) -> dict:
        return self.atualizar_produto(product_id, {"brand_id": brand_id})

    # ── Marcas ───────────────────────────────────────────────────────────

    def listar_marcas(self, brand_name: str = "") -> dict:
        params = {}
        if brand_name:
            params["brand_name"] = brand_name
        return self._get("/api/brands", extra_params=params)

    def buscar_id_outras_marcas(self) -> Optional[str]:
        """Retorna o brand_id de 'Outras marcas' / 'Others' no sistema TikTok."""
        resp = self.listar_marcas("Outras marcas")
        brands = resp.get("data", {}).get("brands", [])
        for b in brands:
            name = b.get("name", "").lower()
            if "outras" in name or "other" in name:
                return str(b.get("id"))
        # Tenta em inglês
        resp2 = self.listar_marcas("Others")
        brands2 = resp2.get("data", {}).get("brands", [])
        for b in brands2:
            name = b.get("name", "").lower()
            if "other" in name or "outras" in name:
                return str(b.get("id"))
        return None

    # ── Lojas ────────────────────────────────────────────────────────────

    def listar_lojas(self) -> dict:
        path = "/api/seller/shops"
        params = {
            "app_key":      self.app_key,
            "timestamp":    int(time.time()),
            "version":      "202309",
            "access_token": self.access_token,
        }
        params["sign"] = self._sign(path, params)
        r = requests.get(f"{TIKTOK_BASE_URL}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def status(self) -> dict:
        """Verifica se as credenciais estão configuradas e funcionando."""
        ok = bool(self.app_key and self.app_secret and self.access_token)
        return {
            "app_key_ok":      bool(self.app_key),
            "app_secret_ok":   bool(self.app_secret),
            "access_token_ok": bool(self.access_token),
            "shop_id_ok":      bool(self.shop_id),
            "pronto":          ok,
        }
