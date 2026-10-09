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

Reconciliação Bling → Shopify (08/10/2026):
  O sync de estoque é por webhook (routes/estoque_sync.py); se um evento se perde,
  a Shopify fica com 0 e o produto some para sempre. Antes de ocultar/restaurar,
  o ciclo consulta o saldo do Bling Shinsei: se a Shopify tem ≤0 mas o Bling tem
  saldo > 0, o inventário Shopify é corrigido para o saldo do Bling (só para cima,
  nunca zera nada) e o produto permanece/volta ativo.
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


def _bling_saldos() -> Optional[dict[str, int]]:
    """
    Mapa SKU → saldoVirtualTotal de todos os produtos ATIVOS do Bling Shinsei.
    Retorna None se o Bling estiver indisponível (ciclo segue sem reconciliar).
    """
    try:
        from bling_client import BlingClient
        bc = BlingClient()
        saldos: dict[str, int] = {}
        pagina = 1
        while True:
            r = requests.get(f"{bc.base_url}/produtos", headers=bc._get_headers(),
                             params={"pagina": pagina, "limite": 100, "criterio": 2}, timeout=60)
            if r.status_code == 429:
                time.sleep(2)
                continue
            r.raise_for_status()
            dados = r.json().get("data", [])
            if not dados:
                break
            for p in dados:
                sku = (p.get("codigo") or "").strip()
                if sku and p.get("situacao") == "A":
                    saldo = (p.get("estoque") or {}).get("saldoVirtualTotal") or 0
                    saldos[sku] = max(saldos.get(sku, 0), int(float(saldo)))
            pagina += 1
            time.sleep(0.4)  # rate limit Bling (3 req/s)
        logger.info("[ESTOQUE-AUTO] Saldos Bling carregados: %d SKUs", len(saldos))
        return saldos
    except Exception as e:
        logger.warning("[ESTOQUE-AUTO] Bling indisponível, ciclo sem reconciliação: %s", e)
        return None


def _location_id(base: str, hdrs: dict) -> Optional[int]:
    r = requests.get(f"{base}/locations.json", headers=hdrs, timeout=30)
    if r.status_code != 200:
        return None
    locs = [l for l in r.json().get("locations", []) if l.get("active")]
    return locs[0]["id"] if locs else None


def _reconciliar_com_bling(base: str, hdrs: dict, prod: dict, saldos: Optional[dict],
                           location_id: Optional[int], dry_run: bool) -> list[dict]:
    """
    Para variantes rastreadas com estoque Shopify ≤ 0 e saldo Bling > 0,
    seta o inventário Shopify = saldo Bling. Retorna as variantes corrigidas
    e atualiza inventory_quantity em `prod` para o restante do ciclo.
    """
    if not saldos or not location_id:
        return []
    corrigidas = []
    for v in prod.get("variants", []):
        if v.get("inventory_management") != "shopify":
            continue
        atual = int(v.get("inventory_quantity") or 0)
        bling = saldos.get((v.get("sku") or "").strip(), 0)
        if atual > 0 or bling <= 0:
            continue
        ok = True
        if not dry_run:
            r = requests.post(f"{base}/inventory_levels/set.json", headers=hdrs, timeout=30,
                              json={"location_id": location_id,
                                    "inventory_item_id": v["inventory_item_id"],
                                    "available": bling})
            ok = r.status_code == 200
            if not ok:
                logger.error("[ESTOQUE-AUTO] Falha ao setar estoque SKU=%s: %d %s",
                             v.get("sku"), r.status_code, r.text[:200])
            time.sleep(0.5)
        if ok:
            v["inventory_quantity"] = bling
        corrigidas.append({"sku": v.get("sku"), "shopify": atual, "bling": bling, "ok": ok})
    return corrigidas


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
        "reconciliados": [],  # Shopify ≤0 corrigido para saldo do Bling
        "erros": [],
        "total_ativos_verificados": 0,
        "total_drafts_verificados": 0,
    }

    saldos = _bling_saldos()
    location_id = _location_id(base, hdrs) if saldos else None
    resultado["bling_reconciliacao_ativa"] = bool(saldos and location_id)

    def _reconciliar(prod: dict) -> None:
        corr = _reconciliar_com_bling(base, hdrs, prod, saldos, location_id, dry_run)
        if corr:
            logger.info("[ESTOQUE-AUTO] %s %d '%s' estoque Bling → Shopify: %s",
                        "SIMULADO" if dry_run else "RECONCILIANDO", prod["id"],
                        prod.get("title", "")[:60], corr)
            resultado["reconciliados"].append({"id": prod["id"], "title": prod.get("title", "")[:60],
                                               "variantes": corr})
            resultado["erros"].extend({"id": prod["id"], **c} for c in corr if not c["ok"])

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
            _reconciliar(prod)
            inv = _total_inventory(prod.get("variants", []))

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
        if inv is not None and inv <= 0:
            _reconciliar(prod)
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
        "[ESTOQUE-AUTO] Ciclo concluído — ocultados: %d | restaurados: %d | reconciliados: %d | skip: %d | erros: %d",
        len(resultado["ocultados"]),
        len(resultado["restaurados"]),
        len(resultado["reconciliados"]),
        len(resultado["ignorados_skip"]),
        len(resultado["erros"]),
    )
    return resultado
