# -*- coding: utf-8 -*-
"""
Serviço de visibilidade automática por estoque — Shopify + GMC.

Lógica:
  - Produto com inventário rastreado e estoque <= 0 → status "draft"
    (some do site Shopify E do feed GMC automaticamente)
  - Produto "draft" marcado por nós e estoque > 0 → status "active"
  - Tag HIDDEN_TAG identifica produtos que ocultamos (nunca tocamos drafts manuais)
  - Tag SKIP_TAG = "mostrar-sem-estoque" → produto fica ativo mesmo sem estoque

Regras adicionais:
  - Só atua em produtos com inventory_management = "shopify" (rastreamento ativo)
  - Produtos com track desabilitado são ignorados
  - Produz relatório completo a cada ciclo
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

HIDDEN_TAG = "auto-oculto-sem-estoque"
SKIP_TAG   = "mostrar-sem-estoque"

SHOPIFY_SHOP  = os.getenv("SHOPIFY_SHOP", "pknw4n-eg")
SHOPIFY_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")

_API_VERSION  = "2024-01"


def _base(shop: str) -> str:
    domain = shop if "." in shop else f"{shop}.myshopify.com"
    return f"https://{domain}/admin/api/{_API_VERSION}"


def _headers(token: str) -> dict:
    return {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}


def _get_all_products(base: str, hdrs: dict, status: str = "active", limit: int = 250) -> list[dict]:
    """Pagina todos os produtos com o status dado."""
    url = f"{base}/products.json"
    params: dict = {"limit": limit, "status": status, "fields": "id,title,status,tags,variants"}
    all_prods: list[dict] = []
    while url:
        r = requests.get(url, headers=hdrs, params=params, timeout=30)
        r.raise_for_status()
        all_prods.extend(r.json().get("products", []))
        link = r.headers.get("Link", "")
        url = None
        params = {}
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
                    break
    return all_prods


def _total_inventory(variants: list[dict]) -> Optional[int]:
    """
    Retorna o estoque total somando variantes rastreadas.
    Retorna None se nenhuma variante tiver rastreamento ativo.
    """
    total = 0
    has_tracking = False
    for v in variants:
        if v.get("inventory_management") == "shopify":
            has_tracking = True
            total += int(v.get("inventory_quantity") or 0)
    return total if has_tracking else None


def _tags_list(product: dict) -> list[str]:
    raw = product.get("tags") or ""
    return [t.strip() for t in raw.split(",") if t.strip()]


def _set_status(base: str, hdrs: dict, product_id: int, new_status: str,
                new_tags: list[str]) -> bool:
    """Atualiza status e tags do produto. Retorna True se sucesso."""
    payload = {
        "product": {
            "id": product_id,
            "status": new_status,
            "tags": ", ".join(new_tags),
        }
    }
    r = requests.put(f"{base}/products/{product_id}.json", headers=hdrs, json=payload, timeout=30)
    if r.status_code == 200:
        return True
    logger.error("[ESTOQUE-AUTO] Erro ao atualizar produto %d: %d %s", product_id, r.status_code, r.text[:200])
    return False


def executar(dry_run: bool = False) -> dict:
    """
    Executa um ciclo completo de auto-ocultação por estoque.

    dry_run=True: só analisa e reporta, sem modificar nada.
    Retorna dict com resumo e listas de produtos afetados.
    """
    shop  = SHOPIFY_SHOP or os.getenv("SHOPIFY_SHOP", "")
    token = SHOPIFY_TOKEN or os.getenv("SHOPIFY_ACCESS_TOKEN", "")

    if not shop or not token:
        logger.error("[ESTOQUE-AUTO] Credenciais Shopify não configuradas")
        return {"erro": "credenciais ausentes"}

    base = _base(shop)
    hdrs = _headers(token)

    resultado: dict[str, Any] = {
        "dry_run": dry_run,
        "ocultados": [],      # ativos → draft (0 estoque)
        "restaurados": [],    # draft → active (estoque voltou)
        "ignorados_skip": [], # tinham tag mostrar-sem-estoque
        "erros": [],
        "total_ativos_verificados": 0,
        "total_drafts_verificados": 0,
    }

    # ── PASSO 1: Produtos ativos com estoque ≤ 0 → ocultar ──────────────────
    logger.info("[ESTOQUE-AUTO] Buscando produtos ativos...")
    ativos = _get_all_products(base, hdrs, status="active")
    resultado["total_ativos_verificados"] = len(ativos)
    logger.info("[ESTOQUE-AUTO] %d produtos ativos encontrados", len(ativos))

    for prod in ativos:
        pid   = prod["id"]
        title = prod.get("title", "")[:60]
        tags  = _tags_list(prod)
        inv   = _total_inventory(prod.get("variants", []))

        if inv is None:
            continue  # sem rastreamento de estoque → ignorar

        if SKIP_TAG in tags:
            resultado["ignorados_skip"].append({"id": pid, "title": title, "inv": inv})
            continue

        if inv <= 0:
            new_tags = [t for t in tags if t != HIDDEN_TAG] + [HIDDEN_TAG]
            logger.info("[ESTOQUE-AUTO] %s %d '%s' inv=%d → DRAFT", "SIMULADO" if dry_run else "OCULTANDO", pid, title, inv)
            ok = True
            if not dry_run:
                ok = _set_status(base, hdrs, pid, "draft", new_tags)
                time.sleep(0.5)  # rate limit
            item = {"id": pid, "title": title, "inv": inv, "ok": ok}
            resultado["ocultados"].append(item)
            if not ok:
                resultado["erros"].append(item)

    # ── PASSO 2: Produtos draft que nós ocultamos e estoque voltou → restaurar
    logger.info("[ESTOQUE-AUTO] Buscando produtos draft com tag '%s'...", HIDDEN_TAG)
    drafts = _get_all_products(base, hdrs, status="draft")
    resultado["total_drafts_verificados"] = len(drafts)

    for prod in drafts:
        pid   = prod["id"]
        title = prod.get("title", "")[:60]
        tags  = _tags_list(prod)

        if HIDDEN_TAG not in tags:
            continue  # draft manual — não tocar

        inv = _total_inventory(prod.get("variants", []))
        if inv is None or inv <= 0:
            continue  # ainda sem estoque

        # Estoque voltou → restaurar
        new_tags = [t for t in tags if t != HIDDEN_TAG]
        logger.info("[ESTOQUE-AUTO] %s %d '%s' inv=%d → ACTIVE", "SIMULADO" if dry_run else "RESTAURANDO", pid, title, inv)
        ok = True
        if not dry_run:
            ok = _set_status(base, hdrs, pid, "active", new_tags)
            time.sleep(0.5)
        item = {"id": pid, "title": title, "inv": inv, "ok": ok}
        resultado["restaurados"].append(item)
        if not ok:
            resultado["erros"].append(item)

    # ── Resumo ───────────────────────────────────────────────────────────────
    logger.info(
        "[ESTOQUE-AUTO] Ciclo concluído — ocultados: %d | restaurados: %d | skip: %d | erros: %d",
        len(resultado["ocultados"]),
        len(resultado["restaurados"]),
        len(resultado["ignorados_skip"]),
        len(resultado["erros"]),
    )
    return resultado
