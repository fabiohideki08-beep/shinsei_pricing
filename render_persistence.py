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
import time

import requests as _req

logger = logging.getLogger(__name__)

_API_KEY    = os.getenv("RENDER_API_KEY", "")
_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "")
_BASE_URL   = "https://api.render.com/v1"

# Retry com backoff — evita perda silenciosa do novo refresh_token após rotação
_MAX_RETRIES   = 3
_RETRY_BACKOFF = [1, 3, 7]


def _patch_env_vars(updates: dict[str, str]) -> bool:
    """
    Atualiza variáveis de ambiente no serviço Render sem apagar as demais.
    Faz GET para obter todas as vars existentes, mescla com `updates`, depois PUT.
    Retry automático com backoff exponencial — crítico para não perder o novo
    refresh_token após rotação OAuth (token rotation invalida o anterior imediatamente).
    """
    # Recarrega em runtime para pegar vars injetadas após startup
    api_key    = os.getenv("RENDER_API_KEY", "") or _API_KEY
    service_id = os.getenv("RENDER_SERVICE_ID", "") or _SERVICE_ID

    if not api_key or not service_id:
        logger.warning(
            "render_persistence: RENDER_API_KEY ou RENDER_SERVICE_ID ausentes — "
            "tokens NAO persistidos no Render: %s", ", ".join(updates.keys())
        )
        return False

    url     = f"{_BASE_URL}/services/{service_id}/env-vars"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    keys_str = ", ".join(updates.keys())

    for attempt in range(_MAX_RETRIES):
        try:
            # GET das vars existentes para não apagar as demais
            existing: dict[str, str] = {}
            r_get = _req.get(url, headers=headers, params={"limit": 100}, timeout=15)
            if r_get.status_code == 200:
                for item in r_get.json():
                    k = item.get("envVar", {}).get("key") or item.get("key", "")
                    v = item.get("envVar", {}).get("value") or item.get("value", "")
                    if k:
                        existing[k] = v
            elif r_get.status_code == 401:
                logger.error(
                    "render_persistence: RENDER_API_KEY invalida (401) — "
                    "tokens NAO persistidos: %s", keys_str
                )
                return False  # sem retry — credencial errada

            # Mescla: updates sobrescreve vars existentes
            merged = {**existing, **updates}
            body   = [{"key": k, "value": v} for k, v in merged.items()]
            r = _req.put(url, headers=headers, json=body, timeout=15)

            if r.status_code in (200, 201):
                logger.info(
                    "render_persistence: OK [%d/%d] — %s persistidos (total vars=%d)",
                    attempt + 1, _MAX_RETRIES, keys_str, len(merged)
                )
                return True

            logger.warning(
                "render_persistence: tentativa %d/%d falhou HTTP %s — %s",
                attempt + 1, _MAX_RETRIES, r.status_code, r.text[:200]
            )
        except Exception as e:
            logger.warning("render_persistence: tentativa %d/%d excecao — %s", attempt + 1, _MAX_RETRIES, e)

        if attempt < _MAX_RETRIES - 1:
            time.sleep(_RETRY_BACKOFF[attempt])

    logger.error(
        "render_persistence: FALHA apos %d tentativas — %s NAO persistidos. "
        "ATENCAO: no proximo restart o refresh_token sera invalido (token rotation)!",
        _MAX_RETRIES, keys_str
    )
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


def save_bling_tokens_akg(access_token: str, refresh_token: str) -> bool:
    """Persiste tokens Bling AKG nas env vars do Render."""
    return _patch_env_vars({
        "BLING_AKG_ACCESS_TOKEN":  access_token,
        "BLING_AKG_REFRESH_TOKEN": refresh_token,
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
