"""
services/amazon.py — Shinsei Pricing
Client de atualização de preços na Amazon via Selling Partner API (SP-API).

Documentação: https://developer-docs.amazon.com/sp-api/docs/product-pricing-api-v2022-05-01

Configuração no .env:
    AMAZON_CLIENT_ID=seu_client_id           # LWA credentials
    AMAZON_CLIENT_SECRET=seu_client_secret
    AMAZON_REFRESH_TOKEN=seu_refresh_token   # gerado no Seller Central
    AMAZON_SELLER_ID=seu_seller_id
    AMAZON_MARKETPLACE_ID=A2Q3Y263D00KWC     # Brasil = A2Q3Y263D00KWC

Fluxo de autenticação:
    A Amazon SP-API usa LWA (Login with Amazon) com refresh_token fixo
    (ao contrário do ML/Shopee que têm OAuth interativo).
    Configure o refresh_token no Seller Central em:
    Apps & Services → Develop Apps → Credentials
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Endpoint de produção SP-API Brasil
SP_API_BASE = "https://sellingpartnerapi-na.amazon.com"
LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
AMAZON_MARKETPLACE_BR = "A2Q3Y263D00KWC"

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

TOKENS_PATH = DATA_DIR / "amazon_tokens.json"


# ─────────────────────────────────────────────
# Configuração
# ─────────────────────────────────────────────

def _client_id() -> str:
    return os.getenv("AMAZON_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.getenv("AMAZON_CLIENT_SECRET", "").strip()


def _refresh_token() -> str:
    # Primeiro tenta o arquivo de tokens, depois o env
    t = _carregar_tokens()
    return t.get("refresh_token") or os.getenv("AMAZON_REFRESH_TOKEN", "").strip()


def _seller_id() -> str:
    return os.getenv("AMAZON_SELLER_ID", "").strip()


def _marketplace_id() -> str:
    return os.getenv("AMAZON_MARKETPLACE_ID", AMAZON_MARKETPLACE_BR).strip()


def _config_ok() -> bool:
    return bool(_client_id() and _client_secret() and _refresh_token() and _seller_id())


# ─────────────────────────────────────────────
# Tokens LWA (access_token de curta duração)
# ─────────────────────────────────────────────

def _carregar_tokens() -> dict:
    if not TOKENS_PATH.exists():
        return {}
    try:
        return json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _salvar_tokens(data: dict) -> None:
    TOKENS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _obter_access_token() -> str:
    """
    Obtém (ou renova) o access_token LWA.
    Expira em 1h — renova automaticamente quando necessário.
    """
    t = _carregar_tokens()
    expires_at = float(t.get("lwa_expires_at", 0))

    # Margem de 60s para evitar uso de token na borda de expiração
    if t.get("lwa_access_token") and time.time() < expires_at - 60:
        return t["lwa_access_token"]

    # Solicita novo access_token usando o refresh_token
    resp = requests.post(
        LWA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": _refresh_token(),
            "client_id": _client_id(),
            "client_secret": _client_secret(),
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Amazon LWA erro {resp.status_code}: {resp.text}")

    data = resp.json()
    access_token = data["access_token"]
    expires_in = int(data.get("expires_in", 3600))

    # Persiste para evitar nova requisição a cada chamada
    t["lwa_access_token"] = access_token
    t["lwa_expires_at"] = time.time() + expires_in
    t["lwa_renovado_em"] = datetime.now(timezone.utc).isoformat()
    _salvar_tokens(t)

    logger.debug("Amazon: LWA access_token renovado (expira em %ds)", expires_in)
    return access_token


# ─────────────────────────────────────────────
# Status
# ─────────────────────────────────────────────

def status() -> dict:
    if not _config_ok():
        faltando = []
        if not _client_id():       faltando.append("AMAZON_CLIENT_ID")
        if not _client_secret():   faltando.append("AMAZON_CLIENT_SECRET")
        if not _refresh_token():   faltando.append("AMAZON_REFRESH_TOKEN")
        if not _seller_id():       faltando.append("AMAZON_SELLER_ID")
        return {"conectado": False, "motivo": f"Variáveis ausentes: {', '.join(faltando)}"}

    t = _carregar_tokens()
    expires_at = float(t.get("lwa_expires_at", 0))
    token_valido = time.time() < expires_at - 60 and bool(t.get("lwa_access_token"))
    return {
        "conectado": True,
        "seller_id": _seller_id(),
        "marketplace_id": _marketplace_id(),
        "token_valido": token_valido,
        "lwa_expires_at": t.get("lwa_expires_at"),
    }


# ─────────────────────────────────────────────
# Serviço de atualização de preços
# ─────────────────────────────────────────────

class AmazonService:
    def __init__(self):
        if not _config_ok():
            raise RuntimeError(
                "Amazon SP-API não configurada. "
                "Defina AMAZON_CLIENT_ID, AMAZON_CLIENT_SECRET, "
                "AMAZON_REFRESH_TOKEN e AMAZON_SELLER_ID no .env."
            )

    def _headers(self) -> dict:
        return {
            "x-amz-access-token": _obter_access_token(),
            "Content-Type": "application/json",
        }

    def atualizar_preco(
        self,
        sku: str,
        preco: float,
        preco_negocio: float | None = None,
    ) -> dict:
        """
        Atualiza o preço de um SKU via PUT /pricing/v0/price.
        sku: SKU do produto na Amazon (não o ASIN).
        preco: preço de lista (listing price).
        preco_negocio: preço para contas Business (opcional).
        """
        url = f"{SP_API_BASE}/pricing/v0/price"
        payload = {
            "requests": [
                {
                    "MarketplaceId": _marketplace_id(),
                    "Sku": sku,
                    "PriceToEstimateFees": {
                        "ListingPrice": {
                            "Amount": round(preco, 2),
                            "CurrencyCode": "BRL",
                        }
                    },
                    # Preço de venda ao consumidor
                    "SetListingPriceAndShipping": {
                        "ListingPrice": {
                            "Amount": round(preco, 2),
                            "CurrencyCode": "BRL",
                        },
                        "Shipping": {
                            "Amount": 0,
                            "CurrencyCode": "BRL",
                        },
                    },
                }
            ]
        }

        # Preço Business (B2B) — se informado
        if preco_negocio and preco_negocio > 0:
            payload["requests"][0]["BusinessPrice"] = {
                "Amount": round(preco_negocio, 2),
                "CurrencyCode": "BRL",
            }

        try:
            resp = requests.put(
                url,
                json=payload,
                headers=self._headers(),
                timeout=30,
            )
        except requests.RequestException as e:
            logger.error("Amazon atualizar_preco erro de rede: %s", e)
            return {"success": False, "error": str(e)}

        if resp.status_code not in (200, 207):
            logger.error("Amazon atualizar_preco erro %s: %s", resp.status_code, resp.text)
            return {"success": False, "status": resp.status_code, "error": resp.text}

        data = resp.json()
        # 207 Multi-Status — verifica cada item individualmente
        if resp.status_code == 207:
            respostas = data.get("responses", [])
            erros = [r for r in respostas if r.get("status", {}).get("statusCode", 200) >= 400]
            if erros:
                logger.error("Amazon atualizar_preco parcial: %s", erros)
                return {"success": False, "partial": True, "errors": erros, "raw": data}

        logger.info("Amazon: preço atualizado SKU=%s → R$ %.2f", sku, preco)
        return {"success": True, "data": data}

    def atualizar_com_retry(self, sku: str, preco: float, tentativas: int = 3) -> dict:
        """Retenta em caso de throttling (429) com backoff exponencial."""
        ultimo = None
        for i in range(tentativas):
            ultimo = self.atualizar_preco(sku, preco)
            if ultimo["success"]:
                return ultimo
            status_code = ultimo.get("status", 0)
            if status_code == 429 and i < tentativas - 1:
                wait = 2 ** (i + 1)
                logger.warning("Amazon throttling, aguardando %ds...", wait)
                time.sleep(wait)
                continue
            break
        return ultimo or {"success": False, "error": "Falha após retries"}

    def buscar_preco_atual(self, asin: str) -> dict:
        """
        Consulta o preço atual de um ASIN.
        Útil para validar se a atualização foi aplicada.
        """
        url = f"{SP_API_BASE}/products/pricing/v0/price"
        params = {
            "MarketplaceId": _marketplace_id(),
            "Asins": asin,
            "ItemType": "Asin",
        }
        try:
            resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
        except requests.RequestException as e:
            return {"success": False, "error": str(e)}

        if resp.status_code != 200:
            return {"success": False, "status": resp.status_code, "error": resp.text}

        return {"success": True, "data": resp.json()}
