# -*- coding: utf-8 -*-
"""
services/shopee.py — Shinsei Pricing
Serviço de integração com a Shopee API v2 (OAuth + preços).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("shinsei.shopee")

# ── Caminhos ──────────────────────────────────────────────────────────────────
_BASE_DIR    = Path(__file__).parent.parent
_DATA_DIR    = _BASE_DIR / "data"
_TOKENS_PATH = _DATA_DIR / "shopee_tokens.json"

# ── Configuração (env vars) ───────────────────────────────────────────────────
PARTNER_ID  : int  = int(os.getenv("SHOPEE_PARTNER_ID", "0"))
PARTNER_KEY : str  = os.getenv("SHOPEE_PARTNER_KEY", "").strip()
SHOP_ID     : int  = int(os.getenv("SHOPEE_SHOP_ID", "0"))
IS_SANDBOX  : bool = os.getenv("SHOPEE_SANDBOX", "false").lower() in ("1", "true", "yes")

_API_PROD    = "https://partner.shopeemobile.com/api/v2"
_API_SANDBOX = "https://openplatform.sandbox.test-stable.shopee.com/api/v2"
API_BASE     = _API_SANDBOX if IS_SANDBOX else _API_PROD


# ── Helpers ───────────────────────────────────────────────────────────────────

def _config_ok() -> bool:
    return bool(PARTNER_ID and PARTNER_KEY)


def _assinar(path: str, ts: int, access_token: str = "", shop_id: int = 0) -> str:
    """Gera a assinatura HMAC-SHA256 conforme Shopee API v2."""
    base = f"{PARTNER_ID}{path}{ts}{access_token}{shop_id if shop_id else ''}"
    return hmac.new(PARTNER_KEY.encode(), base.encode(), hashlib.sha256).hexdigest()


def _salvar_tokens(tokens: dict) -> None:
    _DATA_DIR.mkdir(exist_ok=True)
    _TOKENS_PATH.write_text(
        json.dumps(tokens, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _carregar_tokens() -> dict | None:
    try:
        if _TOKENS_PATH.exists():
            return json.loads(_TOKENS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def tem_tokens() -> bool:
    t = _carregar_tokens()
    return bool(t and t.get("access_token"))


def token_expirado() -> bool:
    t = _carregar_tokens()
    if not t:
        return True
    expires_at = float(t.get("expires_at", 0))
    return time.time() >= expires_at - 300  # margem de 5 min


# ── OAuth Service ─────────────────────────────────────────────────────────────

class ShopeeOAuthService:
    """Gerencia o fluxo OAuth da Shopee."""

    def url_autorizacao(self, redirect_uri: str) -> str:
        """Gera a URL de autorização para redirecionar o usuário."""
        ts   = int(time.time())
        path = "/api/v2/shop/auth_partner"
        sign = _assinar(path, ts)
        from urllib.parse import urlencode
        params = {
            "partner_id": PARTNER_ID,
            "timestamp":  ts,
            "sign":       sign,
            "redirect":   redirect_uri,
        }
        base = f"{API_BASE}/shop/auth_partner"
        return f"{base}?{urlencode(params)}"

    def trocar_code(self, code: str, shop_id: int) -> dict:
        """Troca o authorization code por access_token + refresh_token."""
        ts   = int(time.time())
        path = "/api/v2/auth/token/get"
        sign = _assinar(path, ts)

        try:
            with httpx.Client(timeout=30) as client:
                resp = client.post(
                    f"{API_BASE}/auth/token/get",
                    json={"code": code, "shop_id": shop_id, "partner_id": PARTNER_ID},
                    params={"partner_id": PARTNER_ID, "timestamp": ts, "sign": sign},
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:300] if exc.response else ""
            logger.error("Shopee trocar_code HTTP %s: %s", exc.response.status_code, body)
            return {"success": False, "error": f"HTTP {exc.response.status_code}: {body}"}
        except Exception as exc:
            logger.error("Shopee trocar_code erro: %s", exc)
            return {"success": False, "error": str(exc)}

        if data.get("error"):
            msg = data.get("message", str(data["error"]))
            logger.error("Shopee trocar_code API error: %s", msg)
            return {"success": False, "error": msg}

        tokens = {
            "access_token":  data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_at":    time.time() + int(data.get("expire_in", 14400)),
            "shop_id":       shop_id,
            "partner_id":    PARTNER_ID,
            "obtido_em":     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _salvar_tokens(tokens)
        logger.info("Shopee tokens obtidos com sucesso para shop_id=%d", shop_id)
        return {"success": True, "data": tokens}

    def renovar_token(self) -> dict:
        """Renova o access_token usando o refresh_token."""
        t = _carregar_tokens()
        if not t:
            return {"success": False, "error": "Nenhum token encontrado. Faça a autorização primeiro."}

        refresh_token = t.get("refresh_token", "")
        shop_id       = int(t.get("shop_id", SHOP_ID or 0))

        if not refresh_token or not shop_id:
            return {"success": False, "error": "refresh_token ou shop_id ausentes."}

        ts   = int(time.time())
        path = "/api/v2/auth/access_token/get"
        sign = _assinar(path, ts)

        try:
            with httpx.Client(timeout=30) as client:
                resp = client.post(
                    f"{API_BASE}/auth/access_token/get",
                    json={
                        "refresh_token": refresh_token,
                        "shop_id":       shop_id,
                        "partner_id":    PARTNER_ID,
                    },
                    params={"partner_id": PARTNER_ID, "timestamp": ts, "sign": sign},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.error("Shopee renovar_token erro: %s", exc)
            return {"success": False, "error": str(exc)}

        if data.get("error"):
            msg = data.get("message", str(data["error"]))
            logger.error("Shopee renovar_token API error: %s", msg)
            return {"success": False, "error": msg}

        tokens = {
            **t,
            "access_token":  data["access_token"],
            "refresh_token": data.get("refresh_token", refresh_token),
            "expires_at":    time.time() + int(data.get("expire_in", 14400)),
            "shop_id":       shop_id,
            "renovado_em":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _salvar_tokens(tokens)
        logger.info("Shopee token renovado para shop_id=%d", shop_id)
        return {"success": True, "data": tokens}

    def status(self) -> dict:
        """Retorna o status da conexão com a Shopee."""
        if not _config_ok():
            return {
                "connected": False,
                "status":    "not_configured",
                "message":   "SHOPEE_PARTNER_ID ou SHOPEE_PARTNER_KEY não configurados.",
            }

        t = _carregar_tokens()
        if not t:
            return {
                "connected": False,
                "status":    "no_tokens",
                "message":   "Autorização pendente. Acesse /shopee/auth para conectar.",
            }

        expirado = token_expirado()
        return {
            "connected":  not expirado,
            "status":     "expired" if expirado else "connected",
            "shop_id":    t.get("shop_id"),
            "partner_id": PARTNER_ID,
            "expires_at": t.get("expires_at", 0),
            "expirado":   expirado,
            "sandbox":    IS_SANDBOX,
            "message":    "Token expirado — renove em /shopee/refresh." if expirado else "Conectado.",
        }


# ── Shopee API Service ────────────────────────────────────────────────────────

class ShopeeService:
    """Chamadas autenticadas à Shopee API v2."""

    def __init__(self) -> None:
        t = _carregar_tokens()
        if not t or not t.get("access_token"):
            raise RuntimeError("Shopee não autenticada. Faça a autorização em /shopee/auth.")
        self.access_token = t["access_token"]
        self.shop_id      = int(t.get("shop_id", SHOP_ID or 0))
        if not self.shop_id:
            raise RuntimeError("shop_id não configurado nos tokens Shopee.")

    def _params_auth(self, path: str) -> dict:
        ts = int(time.time())
        return {
            "partner_id":   PARTNER_ID,
            "timestamp":    ts,
            "sign":         _assinar(path, ts, self.access_token, self.shop_id),
            "access_token": self.access_token,
            "shop_id":      self.shop_id,
        }

    def atualizar_preco(self, item_id: str | int, preco: float) -> dict:
        """Atualiza o preço de um item na Shopee."""
        path = "/api/v2/product/update_price"
        params = self._params_auth(path)
        body: dict[str, Any] = {
            "item_id": int(item_id),
            "price_list": [
                {"model_id": 0, "original_price": round(preco, 2)}
            ],
        }
        try:
            with httpx.Client(timeout=20) as client:
                resp = client.post(f"{API_BASE}/product/update_price", params=params, json=body)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.error("Shopee update_price erro: %s", exc)
            return {"success": False, "error": str(exc)}

        if data.get("error"):
            msg = data.get("message", str(data["error"]))
            logger.warning("Shopee update_price API error item=%s: %s", item_id, msg)
            return {"success": False, "error": msg, "data": data}

        logger.info("Shopee preço atualizado: item_id=%s preco=%.2f", item_id, preco)
        return {"success": True, "data": data}

    def atualizar_com_retry(self, item_id: str | int, preco: float, tentativas: int = 3) -> dict:
        """Tenta atualizar o preço com retentativas; renova token se necessário."""
        ultimo_erro = ""
        for tentativa in range(1, tentativas + 1):
            resultado = self.atualizar_preco(item_id, preco)
            if resultado["success"]:
                return resultado
            ultimo_erro = resultado.get("error", "Erro desconhecido")
            if "token" in ultimo_erro.lower() or "auth" in ultimo_erro.lower():
                logger.info("Shopee: renovando token antes da próxima tentativa...")
                renovacao = ShopeeOAuthService().renovar_token()
                if renovacao["success"]:
                    novo_t = _carregar_tokens()
                    if novo_t:
                        self.access_token = novo_t["access_token"]
            if tentativa < tentativas:
                time.sleep(1)
        return {"success": False, "error": f"Falhou após {tentativas} tentativas: {ultimo_erro}"}

    def listar_produtos(self, offset: int = 0, page_size: int = 50) -> dict:
        """Lista produtos ativos da loja."""
        path = "/api/v2/product/get_item_list"
        params = self._params_auth(path)
        params.update({"offset": offset, "page_size": page_size, "item_status": "NORMAL"})
        try:
            with httpx.Client(timeout=20) as client:
                resp = client.get(f"{API_BASE}/product/get_item_list", params=params)
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            logger.error("Shopee listar_produtos erro: %s", exc)
            return {"error": str(exc)}
