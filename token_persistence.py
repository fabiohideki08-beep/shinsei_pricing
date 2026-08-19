"""
token_persistence.py — Shinsei Pricing
Lê e grava tokens no Google Secret Manager para persistência entre deploys.
Cada refresh de token (Bling, Shopee, ML) chama save_secret() para manter
a versão mais recente disponível para o próximo container.
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "shinsei-market")

# Nomes dos secrets no Secret Manager
SECRET_BLING  = "shinsei-bling-tokens"
SECRET_SHOPEE = "shinsei-shopee-tokens"
SECRET_ML     = "shinsei-ml-tokens"


def _client():
    from google.cloud import secretmanager  # type: ignore
    return secretmanager.SecretManagerServiceClient()


def load_secret(secret_name: str) -> dict | None:
    """Lê a versão mais recente de um secret. Retorna dict ou None em caso de erro."""
    try:
        client = _client()
        name = f"projects/{PROJECT_ID}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        data = json.loads(response.payload.data.decode("utf-8"))
        logger.info("Secret Manager: %s carregado", secret_name)
        return data
    except Exception as e:
        logger.warning("Secret Manager: falha ao ler %s: %s", secret_name, e)
        return None


def save_secret(secret_name: str, data: dict) -> bool:
    """Adiciona nova versão ao secret. Retorna True se sucesso."""
    try:
        client = _client()
        parent = f"projects/{PROJECT_ID}/secrets/{secret_name}"
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        client.add_secret_version(
            request={"parent": parent, "payload": {"data": payload}}
        )
        logger.info("Secret Manager: %s atualizado", secret_name)
        return True
    except Exception as e:
        logger.error("Secret Manager: falha ao salvar %s: %s", secret_name, e)
        return False


def load_bling_tokens() -> dict | None:
    return load_secret(SECRET_BLING)


def save_bling_tokens(access_token: str, refresh_token: str) -> bool:
    return save_secret(SECRET_BLING, {
        "access_token": access_token,
        "refresh_token": refresh_token,
    })


def load_shopee_tokens() -> dict | None:
    return load_secret(SECRET_SHOPEE)


def save_shopee_tokens(access_token: str, refresh_token: str, shop_id: int) -> bool:
    return save_secret(SECRET_SHOPEE, {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "shop_id": shop_id,
    })


def load_ml_tokens() -> dict | None:
    return load_secret(SECRET_ML)


def save_ml_tokens(access_token: str, refresh_token: str,
                   client_id: str = "", client_secret: str = "",
                   user_id: str = "") -> bool:
    return save_secret(SECRET_ML, {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "user_id": user_id,
    })
