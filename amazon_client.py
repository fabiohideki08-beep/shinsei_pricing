"""
amazon_client.py — Shinsei Pricing
Cliente SP-API da Amazon com autenticação LWA automática.
"""
from __future__ import annotations
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional
import requests

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
TOKEN_CACHE_PATH = BASE_DIR / "data" / "amazon_token_cache.json"

LWA_URL = "https://api.amazon.com/auth/o2/token"
SP_API_BASE = "https://sellingpartnerapi-na.amazon.com"


class AmazonClient:
    def __init__(self):
        self.config = self._load_config()
        self._access_token: Optional[str] = None
        self._token_expiry: float = 0

    def _load_config(self) -> dict:
        # Prioridade 1: env vars (Cloud Run / Railway)
        cfg_from_env = {
            "seller_id":      os.getenv("AMAZON_SELLER_ID", ""),
            "marketplace_id": os.getenv("AMAZON_MARKETPLACE_ID", "A2Q3Y263D00KWC"),
            "client_id":      os.getenv("AMAZON_CLIENT_ID", ""),
            "client_secret":  os.getenv("AMAZON_CLIENT_SECRET", ""),
            "refresh_token":  os.getenv("AMAZON_REFRESH_TOKEN", ""),
        }
        if all([cfg_from_env["seller_id"], cfg_from_env["client_id"],
                cfg_from_env["client_secret"], cfg_from_env["refresh_token"]]):
            return cfg_from_env

        # Prioridade 2: arquivo de configuração local
        path = BASE_DIR / "data" / "amazon_config.json"
        if path.exists():
            try:
                file_cfg = json.loads(path.read_text(encoding="utf-8"))
                # Preenche campos ausentes com env vars
                for k, v in cfg_from_env.items():
                    if v and not file_cfg.get(k):
                        file_cfg[k] = v
                return file_cfg
            except Exception as e:
                logger.warning("Erro ao ler amazon_config.json: %s", e)

        raise FileNotFoundError(
            "Amazon não configurado: defina AMAZON_SELLER_ID, AMAZON_CLIENT_ID, "
            "AMAZON_CLIENT_SECRET e AMAZON_REFRESH_TOKEN nas variáveis de ambiente."
        )

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
        last_exc = None
        for attempt in range(3):
            try:
                res = requests.get(url, headers=self._headers(), params=params or {}, timeout=20)
                if res.status_code == 429:
                    time.sleep(3 * (attempt + 1))
                    continue
                res.raise_for_status()
                return res.json()
            except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
                last_exc = e
                logger.warning("Amazon _get conexão falhou (tentativa %d/3): %s", attempt + 1, e)
                time.sleep(2 * (attempt + 1))
        raise last_exc

    def get_listings(self, page_token: str = None) -> dict:
        """Lista anúncios MFN/DBA ativos do seller via Listings Items API."""
        # Listings Items API v2021-08-01: GET /listings/2021-08-01/items/{sellerId}
        # status válidos: BUYABLE, DISCOVERABLE (não ACTIVE)
        # includedData=summaries retorna info básica de cada anúncio
        params = {
            "marketplaceIds": self.config["marketplace_id"],
            "includedData": "summaries,fulfillmentAvailability",
            "pageSize": 20,  # máximo suportado pela Listings Items API v2021-08-01
        }
        if page_token:
            params["pageToken"] = page_token
        result = self._get(f"/listings/2021-08-01/items/{self.config['seller_id']}", params)
        logger.debug(
            "get_listings página=%s items=%d nextToken=%s",
            "1" if not page_token else "N",
            len(result.get("items") or []),
            bool(result.get("pagination", {}).get("nextToken")),
        )
        return result

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
