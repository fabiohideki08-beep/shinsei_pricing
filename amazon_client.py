"""
amazon_client.py — Shinsei Pricing
Cliente SP-API da Amazon com autenticação LWA automática.
"""
from __future__ import annotations
import json
import logging
import time
from pathlib import Path
from typing import Optional
import requests

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "data" / "amazon_config.py"
TOKEN_CACHE_PATH = BASE_DIR / "data" / "amazon_token_cache.json"

LWA_URL = "https://api.amazon.com/auth/o2/token"
SP_API_BASE = "https://sellingpartnerapi-na.amazon.com"


class AmazonClient:
    def __init__(self):
        self.config = self._load_config()
        self._access_token: Optional[str] = None
        self._token_expiry: float = 0

    def _load_config(self) -> dict:
        path = BASE_DIR / "data" / "amazon_config.json"
        if not path.exists():
            raise FileNotFoundError("data/amazon_config.json não encontrado")
        return json.loads(path.read_text(encoding="utf-8"))

    def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._token_expiry - 60:
            return self._access_token
        res = requests.post(LWA_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": self.config["refresh_token"],
            "client_id": self.config["client_id"],
            "client_secret": self.config["client_secret"],
        }, timeout=10)
        res.raise_for_status()
        data = res.json()
        self._access_token = data["access_token"]
        self._token_expiry = time.time() + data.get("expires_in", 3600)
        return self._access_token

    def _headers(self) -> dict:
        return {
            "x-amz-access-token": self._get_access_token(),
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict = None) -> dict:
        url = SP_API_BASE + path
        res = requests.get(url, headers=self._headers(), params=params or {}, timeout=15)
        if res.status_code == 429:
            time.sleep(2)
            res = requests.get(url, headers=self._headers(), params=params or {}, timeout=15)
        res.raise_for_status()
        return res.json()

    def get_listings(self, page_token: str = None) -> dict:
        """Lista anúncios ativos do seller."""
        params = {
            "marketplaceIds": self.config["marketplace_id"],
            "status": "ACTIVE",
            "pageSize": 50,
        }
        if page_token:
            params["pageToken"] = page_token
        return self._get(f"/listings/2021-08-01/items/{self.config['seller_id']}", params)

    def get_inventory(self, skus: list = None) -> dict:
        """Consulta estoque via FBA Inventory API."""
        params = {
            "marketplaceIds": self.config["marketplace_id"],
            "granularityType": "Marketplace",
            "granularityId": self.config["marketplace_id"],
        }
        if skus:
            params["sellerSkus"] = ",".join(skus[:50])
        return self._get("/fba/inventory/v1/summaries", params)

    def get_orders(self, created_after: str = None, max_results: int = 50) -> dict:
        """Busca pedidos recentes."""
        params = {
            "MarketplaceIds": self.config["marketplace_id"],
            "MaxResultsPerPage": min(max_results, 100),
        }
        if created_after:
            params["CreatedAfter"] = created_after
        return self._get("/orders/v0/orders", params)

    def get_order_items(self, order_id: str) -> dict:
        """Busca itens de um pedido específico."""
        return self._get(f"/orders/v0/orders/{order_id}/orderItems")

    def get_finances(self, posted_after: str = None) -> dict:
        """Busca eventos financeiros (comissões, fretes, etc)."""
        params = {"MaxResultsPerPage": 100}
        if posted_after:
            params["PostedAfter"] = posted_after
        return self._get("/finances/v0/financialEvents", params)

    def get_catalog_item(self, asin: str) -> dict:
        """Busca detalhes de um produto pelo ASIN."""
        params = {"marketplaceIds": self.config["marketplace_id"]}
        return self._get(f"/catalog/2022-04-01/items/{asin}", params)

    def has_config(self) -> bool:
        try:
            self._load_config()
            return True
        except Exception:
            return False
