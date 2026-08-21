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
_BASE_DIR         = Path(__file__).parent.parent
_DATA_DIR         = _BASE_DIR / "data"
_TOKENS_PATH      = _DATA_DIR / "shopee_tokens.json"
_TOKENS_PATH_AKG  = _DATA_DIR / "shopee_tokens_akg.json"


def _tokens_path(akg: bool = False) -> Path:
    return _TOKENS_PATH_AKG if akg else _TOKENS_PATH

# ── Configuração (env vars) ───────────────────────────────────────────────────
IS_SANDBOX  : bool = os.getenv("SHOPEE_SANDBOX", "false").lower() in ("1", "true", "yes")

_API_PROD    = "https://partner.shopeemobile.com/api/v2"
_API_SANDBOX = "https://openplatform.sandbox.test-stable.shopee.com/api/v2"
API_BASE     = _API_SANDBOX if IS_SANDBOX else _API_PROD

_CREDS_PATH = _BASE_DIR / "data" / "credentials.json"


def _get_creds() -> tuple[int, str, int]:
    """Retorna (partner_id, partner_key, shop_id) — env vars têm prioridade, fallback em credentials.json."""
    pid  = int(os.getenv("SHOPEE_PARTNER_ID", "0"))
    pkey = os.getenv("SHOPEE_PARTNER_KEY", "").strip()
    sid  = int(os.getenv("SHOPEE_SHOP_ID", "0"))
    if not pid or not pkey:
        try:
            saved = json.loads(_CREDS_PATH.read_text(encoding="utf-8")).get("shopee", {})
            pid  = pid  or int(saved.get("SHOPEE_PARTNER_ID", 0) or 0)
            pkey = pkey or saved.get("SHOPEE_PARTNER_KEY", "").strip()
            sid  = sid  or int(saved.get("SHOPEE_SHOP_ID", 0) or 0)
        except Exception:
            pass
    return pid, pkey, sid


# Compat: módulos que importam PARTNER_ID/PARTNER_KEY/SHOP_ID diretamente
PARTNER_ID  : int  = int(os.getenv("SHOPEE_PARTNER_ID", "0"))
PARTNER_KEY : str  = os.getenv("SHOPEE_PARTNER_KEY", "").strip()
SHOP_ID     : int  = int(os.getenv("SHOPEE_SHOP_ID", "0"))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _config_ok() -> bool:
    pid, pkey, _ = _get_creds()
    return bool(pid and pkey)


def _assinar(path: str, ts: int, access_token: str = "", shop_id: int = 0) -> str:
    """Gera a assinatura HMAC-SHA256 conforme Shopee API v2."""
    pid, pkey, _ = _get_creds()
    base = f"{pid}{path}{ts}{access_token}{shop_id if shop_id else ''}"
    return hmac.new(pkey.encode(), base.encode(), hashlib.sha256).hexdigest()


def _salvar_tokens(tokens: dict, akg: bool = False) -> None:
    path = _tokens_path(akg)
    _DATA_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(tokens, indent=2, ensure_ascii=False), encoding="utf-8")
    # Secret Manager só para conta Shinsei
    if not akg:
        try:
            from token_persistence import save_shopee_tokens
            save_shopee_tokens(
                tokens.get("access_token", ""),
                tokens.get("refresh_token", ""),
                int(tokens.get("shop_id", 0)),
            )
        except Exception as _e:
            logger.warning("Shopee: falha ao salvar no Secret Manager: %s", _e)


def _carregar_tokens(akg: bool = False) -> dict | None:
    path = _tokens_path(akg)
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def tem_tokens(akg: bool = False) -> bool:
    t = _carregar_tokens(akg=akg)
    return bool(t and t.get("access_token"))


def token_expirado() -> bool:
    t = _carregar_tokens()
    if not t:
        return True
    expires_at = float(t.get("expires_at", 0))
    return time.time() >= expires_at - 300  # margem de 5 min


# ── OAuth Service ─────────────────────────────────────────────────────────────

class ShopeeOAuthService:
    """Gerencia o fluxo OAuth da Shopee. akg=True usa shopee_tokens_akg.json."""

    def __init__(self, akg: bool = False) -> None:
        self._pid, self._pkey, self._sid = _get_creds()
        self._akg = akg

    def url_autorizacao(self, redirect_uri: str) -> str:
        """Gera a URL de autorização para redirecionar o usuário."""
        ts   = int(time.time())
        path = "/api/v2/shop/auth_partner"
        sign = _assinar(path, ts)
        from urllib.parse import urlencode
        params = {
            "partner_id": self._pid,
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
                    json={"code": code, "shop_id": shop_id, "partner_id": self._pid},
                    params={"partner_id": self._pid, "timestamp": ts, "sign": sign},
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
            "partner_id":    self._pid,
            "obtido_em":     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _salvar_tokens(tokens, akg=self._akg)
        logger.info("Shopee tokens obtidos para shop_id=%d (akg=%s)", shop_id, self._akg)
        return {"success": True, "data": tokens}

    def renovar_token(self) -> dict:
        """Renova o access_token usando o refresh_token."""
        t = _carregar_tokens(akg=self._akg)
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
                        "partner_id":    self._pid,
                    },
                    params={"partner_id": self._pid, "timestamp": ts, "sign": sign},
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
        _salvar_tokens(tokens, akg=self._akg)
        logger.info("Shopee token renovado para shop_id=%d (akg=%s)", shop_id, self._akg)
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
            "partner_id": self._pid,
            "expires_at": t.get("expires_at", 0),
            "expirado":   expirado,
            "sandbox":    IS_SANDBOX,
            "message":    "Token expirado — renove em /shopee/refresh." if expirado else "Conectado.",
        }


# ── Auto-refresh do token Shopee ─────────────────────────────────────────────

def obter_token_shopee() -> str:
    """Retorna access_token Shopee válido, renovando automaticamente se necessário."""
    if not token_expirado():
        t = _carregar_tokens()
        if t and t.get("access_token"):
            return t["access_token"]
    result = ShopeeOAuthService().renovar_token()
    if not result.get("success"):
        raise RuntimeError(f"Shopee token refresh falhou: {result.get('error')}")
    return result["data"]["access_token"]


# ── Shopee API Service ────────────────────────────────────────────────────────

class ShopeeService:
    """Chamadas autenticadas à Shopee API v2."""

    def __init__(self, akg: bool = False) -> None:
        self._akg = akg
        t = _carregar_tokens(akg=akg)
        auth_url = "/shopee/akg/auth" if akg else "/shopee/auth"
        if not t or not t.get("access_token"):
            raise RuntimeError(f"Shopee {'AKG ' if akg else ''}não autenticada. Faça a autorização em {auth_url}.")
        self.access_token = t["access_token"]
        _, _, sid = _get_creds()
        self.shop_id = int(t.get("shop_id") or sid or 0)
        if not self.shop_id:
            raise RuntimeError("shop_id não configurado nos tokens Shopee.")
        self._pid, self._pkey, _ = _get_creds()

    def _params_auth(self, path: str) -> dict:
        ts = int(time.time())
        return {
            "partner_id":   self._pid,
            "timestamp":    ts,
            "sign":         _assinar(path, ts, self.access_token, self.shop_id),
            "access_token": self.access_token,
            "shop_id":      self.shop_id,
        }

    def obter_item_completo(self, item_id: int) -> dict:
        """
        Busca detalhes completos de um item + modelos (variações) da Shopee.
        Retorna estrutura pronta para exibição/importação no Bling.
        """
        item_id = int(item_id)

        # ── 1. Informações base do item ───────────────────────────────────────
        path_info = "/api/v2/product/get_item_base_info"
        params_info = self._params_auth(path_info)
        params_info["item_id_list"] = str(item_id)
        params_info["need_tax_info"] = "false"
        params_info["need_complaint_policy"] = "false"

        # ── 2. Modelos (variações) ─────────────────────────────────────────────
        path_models = "/api/v2/product/get_model_list"
        params_models = self._params_auth(path_models)
        params_models["item_id"] = item_id

        try:
            with httpx.Client(timeout=20) as client:
                r_info   = client.get(f"{API_BASE}/product/get_item_base_info",   params=params_info)
                r_models = client.get(f"{API_BASE}/product/get_model_list", params=params_models)
        except Exception as exc:
            logger.error("Shopee obter_item_completo erro HTTP: %s", exc)
            return {"error": str(exc)}

        # ── Processa item base ─────────────────────────────────────────────────
        info_data = r_info.json()
        if info_data.get("error"):
            return {"error": info_data.get("message", info_data["error"])}

        item_list = (info_data.get("response") or {}).get("item_list") or []
        if not item_list:
            return {"error": f"Item {item_id} não encontrado na Shopee."}
        item = item_list[0]

        nome      = item.get("item_name", "")
        sku_pai   = item.get("item_sku", "")
        has_model = item.get("has_model", False)

        # Preço: pega o current_price da lista price_info
        preco = 0.0
        for pi in (item.get("price_info") or []):
            if pi.get("price_type") in ("NORMAL", ""):
                preco = float(pi.get("current_price") or pi.get("original_price") or 0)
                break

        # Estoque total do item (sem variações)
        estoque_total = 0
        for si in (item.get("stock_info") or []):
            if si.get("stock_type") == 1:
                estoque_total += int(si.get("current_stock") or 0)

        # ── Processa modelos (variações) ───────────────────────────────────────
        models_data = r_models.json()
        modelos = []
        if not models_data.get("error"):
            for m in ((models_data.get("response") or {}).get("model") or []):
                m_preco = 0.0
                for pi in (m.get("price_info") or []):
                    if pi.get("price_type") in ("NORMAL", ""):
                        m_preco = float(pi.get("current_price") or pi.get("original_price") or 0)
                        break
                if not m_preco:
                    m_preco = preco

                m_estoque = 0
                for si in (m.get("stock_info") or []):
                    if si.get("stock_type") == 1:
                        m_estoque += int(si.get("current_stock") or 0)

                modelos.append({
                    "model_id":   m.get("model_id"),
                    "nome":       m.get("model_name", ""),
                    "sku":        m.get("model_sku", "") or "",   # vazio = sem SKU
                    "preco":      round(m_preco, 2),
                    "estoque":    m_estoque,
                })

        return {
            "item_id":    item_id,
            "nome":       nome,
            "sku_pai":    sku_pai,
            "preco":      round(preco, 2),
            "estoque":    estoque_total,
            "has_model":  has_model,
            "modelos":    modelos,
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

    def listar_produtos(self, offset: int = 0, page_size: int = 50, item_status: str = "NORMAL") -> dict:
        """Lista produtos da loja. item_status: NORMAL | UNLIST | BANNED | DELETED"""
        path = "/api/v2/product/get_item_list"
        params = self._params_auth(path)
        params.update({"offset": offset, "page_size": page_size, "item_status": item_status})
        try:
            with httpx.Client(timeout=20) as client:
                resp = client.get(f"{API_BASE}/product/get_item_list", params=params)
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            logger.error("Shopee listar_produtos erro: %s", exc)
            return {"error": str(exc)}

    def obter_info_items(self, item_ids: list) -> dict:
        """Busca nome/status de itens em lote (máx 50 por chamada)."""
        if not item_ids:
            return {"response": {"item_list": []}}
        path = "/api/v2/product/get_item_base_info"
        params = self._params_auth(path)
        params["item_id_list"] = ",".join(str(i) for i in item_ids[:50])
        params["need_tax_info"] = "false"
        params["need_complaint_policy"] = "false"
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.get(f"{API_BASE}/product/get_item_base_info", params=params)
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            logger.error("Shopee get_item_base_info erro: %s", exc)
            return {"error": str(exc)}

    def obter_estoque_item(self, item_id: int) -> int:
        """Retorna estoque total do item somando todos os modelos. -1 em caso de erro."""
        path = "/api/v2/product/get_model_list"
        params = self._params_auth(path)
        params["item_id"] = int(item_id)
        try:
            with httpx.Client(timeout=20) as client:
                resp = client.get(f"{API_BASE}/product/get_model_list", params=params)
                resp.raise_for_status()
                data = resp.json()
            if data.get("error"):
                logger.warning("Shopee get_model_list error item=%s: %s", item_id, data.get("message"))
                return -1
            models = (data.get("response") or {}).get("model") or []
            total = 0
            for model in models:
                si = model.get("stock_info") or {}
                # Tenta seller_stock primeiro (estoque real do vendedor)
                seller_stocks = si.get("seller_stock") or []
                if seller_stocks:
                    total += sum(int(s.get("stock") or 0) for s in seller_stocks)
                else:
                    # Fallback: stock_list current_stock
                    for s in (si.get("stock_list") or []):
                        if s.get("stock_type") == 1:  # tipo 1 = seller stock
                            total += int(s.get("current_stock") or 0)
            return total
        except Exception as exc:
            logger.error("Shopee get_model_list erro item=%s: %s", item_id, exc)
            return -1

    def atualizar_estoque(self, item_id: int, quantidade: int, model_id: int = 0) -> dict:
        """Atualiza estoque de um item/modelo na Shopee."""
        path = "/api/v2/product/update_stock"
        params = self._params_auth(path)
        body = {
            "item_id": int(item_id),
            "stock_list": [{
                "model_id": int(model_id),
                "seller_stock": [{"location_id": "", "stock": int(quantidade)}],
            }],
        }
        try:
            with httpx.Client(timeout=20) as client:
                resp = client.post(f"{API_BASE}/product/update_stock", params=params, json=body)
                resp.raise_for_status()
                data = resp.json()
            if data.get("error"):
                msg = data.get("message", str(data["error"]))
                logger.warning("Shopee update_stock error item=%s: %s", item_id, msg)
                return {"success": False, "error": msg, "data": data}
            logger.info("Shopee estoque atualizado: item_id=%s qty=%d", item_id, quantidade)
            return {"success": True, "data": data}
        except Exception as exc:
            logger.error("Shopee update_stock erro: %s", exc)
            return {"success": False, "error": str(exc)}
