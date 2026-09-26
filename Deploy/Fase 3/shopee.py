"""
services/shopee.py — Shinsei Pricing
Client de atualização de preços na Shopee (Shopee Open Platform API v2).

Documentação: https://open.shopee.com/documents/v2/v2.product.update_price

Configuração no .env:
    SHOPEE_PARTNER_ID=seu_partner_id
    SHOPEE_PARTNER_KEY=seu_partner_key
    SHOPEE_SHOP_ID=seu_shop_id
    SHOPEE_ACCESS_TOKEN=seu_access_token  # renovado via OAuth

Fluxo OAuth simplificado:
    1. Gere a URL de autorização com ShopeeOAuthService.url_autorizacao()
    2. O usuário autoriza e o Shopee redireciona para o callback com code+shop_id
    3. Troque o code por tokens com ShopeeOAuthService.trocar_code()
    4. Salve os tokens — o access_token expira em ~4h, use o refresh_token para renovar
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

SHOPEE_API = "https://partner.shopeemobile.com/api/v2"
SHOPEE_AUTH = "https://partner.shopeemobile.com/api/v2/shop/auth_partner"

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

TOKENS_PATH = DATA_DIR / "shopee_tokens.json"


# ─────────────────────────────────────────────
# Helpers de configuração
# ─────────────────────────────────────────────

def _partner_id() -> int:
    try:
        return int(os.getenv("SHOPEE_PARTNER_ID", "0"))
    except ValueError:
        return 0


def _partner_key() -> str:
    return os.getenv("SHOPEE_PARTNER_KEY", "").strip()


def _shop_id() -> int:
    try:
        return int(os.getenv("SHOPEE_SHOP_ID", "0"))
    except ValueError:
        return 0


def _config_ok() -> bool:
    return bool(_partner_id() and _partner_key() and _shop_id())


# ─────────────────────────────────────────────
# Assinatura HMAC (obrigatória em todos os requests)
# ─────────────────────────────────────────────

def _assinar(path: str, timestamp: int, access_token: str = "") -> str:
    """
    A Shopee exige HMAC-SHA256 em cada request.
    base_string = partner_id + path + timestamp + access_token + shop_id
    """
    partner_id = str(_partner_id())
    shop_id = str(_shop_id())
    base = partner_id + path + str(timestamp) + access_token + shop_id
    return hmac.new(_partner_key().encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()


# ─────────────────────────────────────────────
# Persistência de tokens
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


def tem_tokens() -> bool:
    t = _carregar_tokens()
    return bool(t.get("access_token"))


def token_expirado() -> bool:
    t = _carregar_tokens()
    expires_at = float(t.get("expires_at", 0))
    return time.time() >= expires_at


# ─────────────────────────────────────────────
# OAuth
# ─────────────────────────────────────────────

class ShopeeOAuthService:
    def url_autorizacao(self, redirect_uri: str) -> str | None:
        if not _config_ok():
            return None
        ts = int(time.time())
        path = "/api/v2/shop/auth_partner"
        sign = _assinar(path, ts)
        params = {
            "partner_id": _partner_id(),
            "timestamp": ts,
            "sign": sign,
            "redirect": redirect_uri,
        }
        return f"{SHOPEE_AUTH}?{urlencode(params)}"

    def trocar_code(self, code: str, shop_id: int) -> dict:
        ts = int(time.time())
        path = "/api/v2/auth/token/get"
        sign = _assinar(path, ts)
        url = f"{SHOPEE_API}/auth/token/get"
        payload = {
            "code": code,
            "shop_id": shop_id,
            "partner_id": _partner_id(),
        }
        params = {"partner_id": _partner_id(), "timestamp": ts, "sign": sign}
        resp = requests.post(url, json=payload, params=params, timeout=30)
        if resp.status_code != 200:
            return {"success": False, "error": resp.text}
        data = resp.json()
        if data.get("error"):
            return {"success": False, "error": data.get("message", data["error"])}
        tokens = {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_at": time.time() + int(data.get("expire_in", 14400)),
            "shop_id": shop_id,
            "obtido_em": datetime.now(timezone.utc).isoformat(),
        }
        _salvar_tokens(tokens)
        logger.info("Shopee: tokens obtidos para shop_id=%s", shop_id)
        return {"success": True, "data": tokens}

    def renovar_token(self) -> dict:
        t = _carregar_tokens()
        refresh_token = t.get("refresh_token")
        shop_id = t.get("shop_id") or _shop_id()
        if not refresh_token:
            return {"success": False, "error": "Refresh token ausente. Refaça a autorização."}
        ts = int(time.time())
        path = "/api/v2/auth/access_token/get"
        sign = _assinar(path, ts)
        url = f"{SHOPEE_API}/auth/access_token/get"
        payload = {
            "refresh_token": refresh_token,
            "shop_id": shop_id,
            "partner_id": _partner_id(),
        }
        params = {"partner_id": _partner_id(), "timestamp": ts, "sign": sign}
        resp = requests.post(url, json=payload, params=params, timeout=30)
        if resp.status_code != 200:
            return {"success": False, "error": resp.text}
        data = resp.json()
        if data.get("error"):
            return {"success": False, "error": data.get("message", data["error"])}
        tokens = {**t,
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", refresh_token),
            "expires_at": time.time() + int(data.get("expire_in", 14400)),
            "renovado_em": datetime.now(timezone.utc).isoformat(),
        }
        _salvar_tokens(tokens)
        logger.info("Shopee: token renovado para shop_id=%s", shop_id)
        return {"success": True, "data": tokens}

    def status(self) -> dict:
        if not _config_ok():
            return {"conectado": False, "motivo": "SHOPEE_PARTNER_ID/KEY/SHOP_ID não configurados"}
        t = _carregar_tokens()
        if not t.get("access_token"):
            return {"conectado": False, "motivo": "Sem tokens. Acesse /shopee/auth."}
        if token_expirado():
            return {"conectado": False, "motivo": "Token expirado. Renove em /shopee/refresh."}
        return {"conectado": True, "shop_id": t.get("shop_id"), "expires_at": t.get("expires_at")}


# ─────────────────────────────────────────────
# Serviço de atualização de preços
# ─────────────────────────────────────────────

class ShopeeService:
    def __init__(self):
        t = _carregar_tokens()
        if not t.get("access_token"):
            raise RuntimeError("Shopee não autenticada. Acesse /shopee/auth.")
        if token_expirado():
            logger.info("Shopee: token expirado, tentando renovar automaticamente...")
            resultado = ShopeeOAuthService().renovar_token()
            if not resultado["success"]:
                raise RuntimeError(f"Falha ao renovar token Shopee: {resultado['error']}")
            t = _carregar_tokens()
        self.access_token = t["access_token"]
        self.shop_id = int(t.get("shop_id") or _shop_id())

    def _params_autenticados(self, path: str) -> dict:
        ts = int(time.time())
        return {
            "partner_id": _partner_id(),
            "shop_id": self.shop_id,
            "timestamp": ts,
            "sign": _assinar(path, ts, self.access_token),
            "access_token": self.access_token,
        }

    def atualizar_preco(self, item_id: str, preco: float, preco_original: float | None = None) -> dict:
        """
        Atualiza o preço de um item na Shopee.
        item_id: item_id do anúncio (não o SKU do Bling).
        preco: preço promocional (current_price).
        preco_original: preço riscado (original_price). Se None, usa o mesmo.
        """
        path = "/api/v2/product/update_price"
        url = f"{SHOPEE_API}/product/update_price"
        payload = {
            "shop_id": self.shop_id,
            "item_list": [
                {
                    "item_id": int(item_id),
                    "price_list": [
                        {
                            "model_id": 0,  # 0 = sem variação
                            "original_price": round(preco_original or preco, 2),
                            "current_price": round(preco, 2),
                        }
                    ],
                }
            ],
        }
        resp = requests.post(
            url,
            json=payload,
            params=self._params_autenticados(path),
            timeout=30,
        )
        if resp.status_code != 200:
            logger.error("Shopee atualizar_preco erro %s: %s", resp.status_code, resp.text)
            return {"success": False, "status": resp.status_code, "error": resp.text}
        data = resp.json()
        if data.get("error"):
            logger.error("Shopee atualizar_preco API error: %s", data)
            return {"success": False, "error": data.get("message", str(data["error"]))}
        logger.info("Shopee: preço atualizado item_id=%s → R$ %.2f", item_id, preco)
        return {"success": True, "data": data}

    def atualizar_com_retry(self, item_id: str, preco: float, tentativas: int = 3) -> dict:
        ultimo = None
        for i in range(tentativas):
            ultimo = self.atualizar_preco(item_id, preco)
            if ultimo["success"]:
                return ultimo
            if i < tentativas - 1:
                time.sleep(1.5)
        return ultimo or {"success": False, "error": "Falha após retries"}
