# -*- coding: utf-8 -*-
"""
render_persistence.py
Persiste tokens como env vars no Render via API, substituindo o Secret Manager.
Chamado sempre que um token é renovado em runtime, garantindo que o próximo
deploy inicie já com o token mais recente.

Env vars necessárias no Render:
  RENDER_API_KEY   — chave da API Render (Settings → API Keys)
  RENDER_SERVICE_ID — ID do serviço (Settings → Info, começa com "srv-")
"""
from __future__ import annotations

import json
import logging
import os

import requests as _req

logger = logging.getLogger(__name__)

_API_KEY    = os.getenv("RENDER_API_KEY", "")
_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "")
_BASE_URL   = "https://api.render.com/v1"


def _patch_env_vars(updates: dict[str, str]) -> bool:
    """
    Atualiza variáveis de ambiente no serviço Render sem apagar as demais.
    Faz GET para obter todas as vars existentes, mescla com `updates`, depois PUT.
    """
    if not _API_KEY or not _SERVICE_ID:
        logger.debug("render_persistence: RENDER_API_KEY ou RENDER_SERVICE_ID não definidos — skip")
        return False

    url = f"{_BASE_URL}/services/{_SERVICE_ID}/env-vars"
    headers = {
        "Authorization": f"Bearer {_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        # GET das vars existentes para não apagar as demais
        existing: dict[str, str] = {}
        r_get = _req.get(url, headers=headers, timeout=10)
        if r_get.status_code == 200:
            for item in r_get.json():
                k = item.get("envVar", {}).get("key") or item.get("key", "")
                v = item.get("envVar", {}).get("value") or item.get("value", "")
                if k:
                    existing[k] = v
        # Mescla: updates sobrescreve vars existentes
        merged = {**existing, **updates}
        body = [{"key": k, "value": v} for k, v in merged.items()]
        r = _req.put(url, headers=headers, json=body, timeout=10)
        if r.status_code in (200, 201):
            logger.info("render_persistence: %d env var(s) atualizadas (total=%d)", len(updates), len(merged))
            return True
        logger.warning("render_persistence: falha HTTP %s — %s", r.status_code, r.text[:200])
        return False
    except Exception as e:
        logger.warning("render_persistence: erro ao atualizar env vars: %s", e)
        return False


def save_ml_tokens_shinsei(access_token: str, refresh_token: str, user_id: str = "") -> bool:
    """Persiste tokens ML Shinsei nas env vars do Render."""
    updates: dict[str, str] = {
        "ML_ACCESS_TOKEN":  access_token,
        "ML_REFRESH_TOKEN": refresh_token,
    }
    if user_id:
        updates["ML_USER_ID"] = user_id
    return _patch_env_vars(updates)


def save_ml_tokens_akg(access_token: str, refresh_token: str, user_id: str = "") -> bool:
    """Persiste tokens ML AKG nas env vars do Render."""
    updates: dict[str, str] = {
        "ML_AKG_ACCESS_TOKEN":  access_token,
        "ML_AKG_REFRESH_TOKEN": refresh_token,
    }
    if user_id:
        updates["ML_AKG_USER_ID"] = user_id
    return _patch_env_vars(updates)


def save_bling_tokens(access_token: str, refresh_token: str) -> bool:
    """Persiste tokens Bling nas env vars do Render."""
    return _patch_env_vars({
        "BLING_ACCESS_TOKEN":  access_token,
        "BLING_REFRESH_TOKEN": refresh_token,
    })


def save_shopee_tokens(access_token: str, refresh_token: str, shop_id: int = 0) -> bool:
    """Persiste tokens Shopee nas env vars do Render."""
    updates: dict[str, str] = {
        "SHOPEE_ACCESS_TOKEN":  access_token,
        "SHOPEE_REFRESH_TOKEN": refresh_token,
    }
    if shop_id:
        updates["SHOPEE_SHOP_ID"] = str(shop_id)
    return _patch_env_vars(updates)
