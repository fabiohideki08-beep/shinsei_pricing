"""
Copia anúncios ativos da Shinsei (ML) para a conta AKG (ML) preservando SKU.
Estratégia:
  - Itens de catálogo: usa catalog_product_id direto do item Shinsei (mais preciso que busca por texto)
  - Itens com mesmo catalog_product_id (mesmo produto, cores diferentes): agrupados em UM anúncio com
    variações HAIR_TONE (para MLB264861) ou como listas separadas (categorias sem variação)
"""
from __future__ import annotations
import json
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterator

import requests

logger = logging.getLogger("shinsei.ml_copy")

ML_API = "https://api.mercadolibre.com"
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
PROGRESS_PATH = DATA_DIR / "ml_copy_akg_progress.json"

LISTING_TYPE = "bronze"
INITIAL_QUANTITY = 1
RATE_LIMIT_SLEEP = 0.5
BATCH_SIZE = 20

# Categorias que suportam variações por HAIR_TONE
VARIATION_CATEGORIES = {"MLB264861"}


# ── Tokens ────────────────────────────────────────────────────────────────────

def _load_token(path: Path, label: str) -> str:
    if not path.exists():
        raise RuntimeError(f"Token {label} não encontrado em {path}. Reconecte.")
    data = json.loads(path.read_text(encoding="utf-8"))
    token = data.get("access_token", "")
    if not token:
        raise RuntimeError(f"access_token vazio em {label}.")
    return token


def _shinsei_token() -> str:
    try:
        from services.mercado_livre import obter_token_ml
        return obter_token_ml()
    except Exception:
        return _load_token(DATA_DIR / "ml_tokens.json", "Shinsei ML")


def _akg_token() -> str:
    # Tenta arquivo local — fallback para env vars se arquivo ausente ou user_id errado
    path = DATA_DIR / "ml_tokens_akg.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if str(data.get("user_id", "")) == "3541432733" and data.get("access_token"):
                return data["access_token"]
        except Exception:
            pass
    # Fallback: env vars
    import os as _os
    refresh = _os.getenv("ML_AKG_REFRESH_TOKEN", "")
    access  = _os.getenv("ML_AKG_ACCESS_TOKEN", "")
    if access:
        return access
    if refresh:
        # Renova via refresh_token
        import requests as _req
        client_id = _os.getenv("ML_CLIENT_ID", "")
        client_secret = _os.getenv("ML_CLIENT_SECRET", "")
        r = _req.post("https://api.mercadolibre.com/oauth/token", data={
            "grant_type": "refresh_token", "client_id": client_id,
            "client_secret": client_secret, "refresh_token": refresh,
        }, headers={"accept": "application/json"}, timeout=20)
        if r.status_code == 200:
            return r.json().get("access_token", "")
    raise RuntimeError("Token AKG ML não disponível — reconecte via /ml/login2")


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── Progresso ─────────────────────────────────────────────────────────────────

def _load_progress() -> dict:
    if PROGRESS_PATH.exists():
        try:
            return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"criados": [], "falhas": [], "skipped": [], "ultimo_offset": 0}


def _save_progress(p: dict):
    PROGRESS_PATH.write_text(json.dumps(p, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Busca anúncios Shinsei ─────────────────────────────────────────────────────

def _get_shinsei_seller_id(token: str) -> str:
    r = requests.get(f"{ML_API}/users/me", headers=_headers(token), timeout=15)
    r.raise_for_status()
    return str(r.json()["id"])


def _iter_shinsei_active_ids(token: str, seller_id: str) -> Iterator[list[str]]:
    """Itera todos os IDs de anúncios ativos da Shinsei via scroll (sem limite de offset)."""
    limit = 100
    scroll_id: str | None = None

    while True:
        params: dict = {"status": "active", "limit": limit, "search_type": "scan"}
        if scroll_id:
            params["scroll_id"] = scroll_id
        r = requests.get(
            f"{ML_API}/users/{seller_id}/items/search",
            params=params,
            headers=_headers(token),
            timeout=20,
        )
        if r.status_code != 200:
            logger.error("Erro buscando itens Shinsei (scroll): %s", r.text[:200])
            break
        data = r.json()
        ids = data.get("results", [])
        scroll_id = data.get("scroll_id")
        if not ids:
            break

        for i in range(0, len(ids), BATCH_SIZE):
            yield ids[i:i + BATCH_SIZE]

        if not scroll_id:
            break


def _get_items_details(ids: list[str], token: str) -> list[dict]:
    """Busca detalhes individualmente para receber todos os campos."""
    results = []
    for item_id in ids:
        try:
            r = requests.get(f"{ML_API}/items/{item_id}", headers=_headers(token), timeout=15)
            if r.status_code == 200:
                results.append(r.json())
            else:
                logger.warning("Falha ao buscar %s: HTTP %d", item_id, r.status_code)
        except Exception as e:
            logger.warning("Erro ao buscar %s: %s", item_id, e)
        time.sleep(0.1)
    return results


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_sku(item: dict) -> str:
    sku = item.get("seller_custom_field") or ""
    if sku:
        return sku
    for attr in item.get("attributes", []):
        if attr.get("id") == "SELLER_SKU":
            return attr.get("value_name") or ""
    # fallback: GTIN
    for attr in item.get("attributes", []):
        if attr.get("id") == "GTIN":
            return attr.get("value_name") or ""
    return ""


def _extract_hair_tone(item: dict) -> dict | None:
    """Extrai atributo HAIR_TONE do item (para variações em MLB264861)."""
    for attr in item.get("attributes", []):
        if attr.get("id") == "HAIR_TONE":
            return {
                "id": "HAIR_TONE",
                "value_id": attr.get("value_id"),
                "value_name": attr.get("value_name") or "",
            }
    return None


def _pics(item: dict) -> list[dict]:
    """Extrai fotos do item forçando https."""
    result = []
    for pic in item.get("pictures", []):
        url = pic.get("secure_url") or pic.get("url") or ""
        if url:
            result.append({"source": url.replace("http://", "https://", 1)})
    return result


# ── Monta payload ─────────────────────────────────────────────────────────────

def _build_single_payload(item: dict) -> dict | None:
    """Payload para um item individual (sem variações)."""
    category_id = item.get("category_id", "")
    price = item.get("price")
    if not category_id or not price:
        return None

    family_name = item.get("family_name") or ""
    catalog_product_id = item.get("catalog_product_id") or ""
    domain_id = item.get("domain_id") or ""
    is_catalog = bool(family_name or catalog_product_id)

    payload: dict = {
        "category_id": category_id,
        "price": price,
        "currency_id": item.get("currency_id", "BRL"),
        "available_quantity": INITIAL_QUANTITY,
        "listing_type_id": LISTING_TYPE,
        "condition": item.get("condition", "new"),
        "pictures": _pics(item),
    }

    if is_catalog:
        if family_name:
            payload["family_name"] = family_name[:60]
        if catalog_product_id:
            payload["catalog_product_id"] = catalog_product_id
        if domain_id:
            payload["domain_id"] = domain_id
    else:
        title = item.get("title", "")
        if not title:
            return None
        payload["title"] = title
        payload["attributes"] = [
            a for a in item.get("attributes", [])
            if a.get("id") not in ("SELLER_SKU", "HAIR_TONE", "MANUAL_TITLE", "GTIN")
            and a.get("id") is not None
        ]

    sku = _extract_sku(item)
    if sku:
        payload["seller_custom_field"] = sku

    return payload


def _build_variation_payload(items: list[dict]) -> dict | None:
    """
    Payload para múltiplos itens com mesmo catalog_product_id agrupados como variações.
    Usa HAIR_TONE como atributo discriminador de variação.
    Cada item vira uma variation com seu HAIR_TONE + SKU + preço.
    """
    if not items:
        return None

    anchor = items[0]
    category_id = anchor.get("category_id", "")
    family_name = anchor.get("family_name") or ""
    catalog_product_id = anchor.get("catalog_product_id") or ""
    domain_id = anchor.get("domain_id") or ""

    if not category_id or not (family_name or catalog_product_id):
        return None

    variations = []
    for item in items:
        price = item.get("price")
        if not price:
            continue
        sku = _extract_sku(item)
        hair_tone = _extract_hair_tone(item)

        var: dict = {
            "price": price,
            "available_quantity": INITIAL_QUANTITY,
        }
        if sku:
            var["seller_custom_field"] = sku
        if hair_tone:
            var["attribute_combinations"] = [hair_tone]
        variations.append(var)

    if not variations:
        return None

    # Preço de referência no topo (variações sobrescrevem individualmente)
    prices = [v["price"] for v in variations if v.get("price")]
    top_price = prices[0] if prices else anchor.get("price", 0)

    # Agrega todas as fotos de todas as variações
    all_pics: list[dict] = []
    seen_urls: set[str] = set()
    for it in items:
        for pic in _pics(it):
            url = pic.get("source", "")
            if url and url not in seen_urls:
                all_pics.append(pic)
                seen_urls.add(url)

    payload: dict = {
        "category_id": category_id,
        "price": top_price,
        "listing_type_id": LISTING_TYPE,
        "condition": anchor.get("condition", "new"),
        "currency_id": anchor.get("currency_id", "BRL"),
        "pictures": all_pics or _pics(anchor),
        "variations": variations,
    }

    if family_name:
        payload["family_name"] = family_name[:60]
    if catalog_product_id:
        payload["catalog_product_id"] = catalog_product_id
    if domain_id:
        payload["domain_id"] = domain_id

    return payload


# ── Catálogo: busca cpid atual ────────────────────────────────────────────────

_cpid_cache: dict[str, str] = {}  # family_name → catalog_product_id atual


def _find_current_cpid(family_name: str, category_id: str, token: str) -> str:
    """Busca o catalog_product_id ATIVO para um family_name quando o cpid do Shinsei dá 500."""
    key = f"{family_name}|{category_id}"
    if key in _cpid_cache:
        return _cpid_cache[key]

    # Pega domain_id via categoria
    domain_r = requests.get(
        f"{ML_API}/categories/{category_id}",
        headers=_headers(token), timeout=10
    )
    domain_id = ""
    if domain_r.status_code == 200:
        domain_id = (domain_r.json().get("settings", {}).get("catalog_domain") or
                     domain_r.json().get("domain_id") or "")

    params: dict = {"site_id": "MLB", "q": family_name[:80]}
    if domain_id:
        params["domain_id"] = domain_id

    r = requests.get(f"{ML_API}/products/search", params=params,
                     headers=_headers(token), timeout=15)
    if r.status_code != 200:
        return ""

    results = r.json().get("results", [])
    # Prefere produto ativo; aceita qualquer se não tiver ativo
    active = [p for p in results if p.get("status") == "active"]
    candidates = active or results
    cpid = candidates[0].get("id", "") if candidates else ""
    _cpid_cache[key] = cpid
    return cpid


# ── Publicação ────────────────────────────────────────────────────────────────

def _post_item_akg(payload: dict, token: str, shin_token: str = "") -> tuple[bool, str, str]:
    sku = payload.get("seller_custom_field", "")
    r = requests.post(f"{ML_API}/items", json=payload, headers=_headers(token), timeout=30)
    if r.status_code in (200, 201):
        return True, r.json().get("id", ""), sku

    # Fallback: se 500 e temos family_name, tenta buscar cpid atual no catálogo
    if r.status_code == 500 and payload.get("family_name") and shin_token:
        family_name = payload["family_name"]
        category_id = payload.get("category_id", "")
        new_cpid = _find_current_cpid(family_name, category_id, shin_token)
        if new_cpid and new_cpid != payload.get("catalog_product_id"):
            payload2 = {**payload, "catalog_product_id": new_cpid}
            r2 = requests.post(f"{ML_API}/items", json=payload2, headers=_headers(token), timeout=30)
            if r2.status_code in (200, 201):
                return True, r2.json().get("id", ""), sku

    return False, r.text[:400], sku


# ── Job principal ─────────────────────────────────────────────────────────────

def run_copy(
    limit: int = 0,
    dry_run: bool = False,
    reset: bool = False,
    status_callback=None,
) -> dict:
    """
    Executa a cópia Shinsei → AKG com agrupamento de variações.
    1. Coleta TODOS os itens Shinsei via scroll
    2. Agrupa por catalog_product_id (categorias com HAIR_TONE) ou trata individualmente
    3. Para grupos com 2+ itens: cria UM anúncio com variações
    4. Para itens únicos: cria anúncio individual
    """
    shin_token = _shinsei_token()
    akg_token = _akg_token()
    seller_id = _get_shinsei_seller_id(shin_token)

    progress = {"criados": [], "falhas": [], "skipped": [], "ultimo_offset": 0} if reset else _load_progress()

    # SKUs já processados (criados ou falhados) — usados para skip
    ja_processados: set[str] = {
        c["sku"] for c in progress.get("criados", []) if c.get("sku")
    } | {
        f["sku"] for f in progress.get("falhas", []) if f.get("sku")
    }
    # catalog_product_ids já publicados como variação agrupada
    ja_cpids: set[str] = {c.get("catalog_product_id", "") for c in progress.get("criados", []) if c.get("catalog_product_id")}

    def _log(msg: str):
        logger.info(msg)
        progress.setdefault("log", []).append(msg)
        if status_callback:
            status_callback(msg)

    _log(f"Iniciando cópia Shinsei→AKG | seller={seller_id} | dry_run={dry_run}")

    # ── Fase 1: coleta todos os itens Shinsei via scroll ──────────────────────
    _log("Fase 1: coletando itens Shinsei via scroll...")
    all_items: list[dict] = []
    batch_count = 0
    for batch_ids in _iter_shinsei_active_ids(shin_token, seller_id):
        details = _get_items_details(batch_ids, shin_token)
        all_items.extend(details)
        batch_count += 1
        if batch_count % 5 == 0:
            _log(f"  coletados {len(all_items)} itens...")
        if limit and len(all_items) >= limit * 3:
            break

    _log(f"Total coletado: {len(all_items)} itens")

    # ── Fase 2: agrupa por catalog_product_id (para variações) ────────────────
    # Grupos: catalog_product_id → [items] (apenas categorias que suportam variação)
    variation_groups: dict[str, list[dict]] = defaultdict(list)
    individual_items: list[dict] = []

    for item in all_items:
        cpid = item.get("catalog_product_id") or ""
        cat = item.get("category_id") or ""
        if cpid and cat in VARIATION_CATEGORIES:
            variation_groups[cpid].append(item)
        else:
            individual_items.append(item)

    _log(f"Grupos de variação: {len(variation_groups)} | Itens individuais: {len(individual_items)}")

    processados = 0

    # ── Fase 3: publica grupos de variação ────────────────────────────────────
    for cpid, group_items in variation_groups.items():
        if limit and processados >= limit:
            break

        # Coleta SKUs do grupo
        group_skus = [_extract_sku(it) for it in group_items]

        # Skip se TODOS os itens do grupo já foram processados
        if all(sku and sku in ja_processados for sku in group_skus if sku):
            total_skipped = len(progress.get("skipped", []))
            for it, sku in zip(group_items, group_skus):
                progress.setdefault("skipped", []).append({"sku": sku, "shinsei_id": it.get("id", "")})
            processados += len(group_items)
            continue

        # Se já publicamos como variação agrupada, skip
        if cpid in ja_cpids:
            for it in group_items:
                sku = _extract_sku(it)
                progress.setdefault("skipped", []).append({"sku": sku, "shinsei_id": it.get("id", "")})
            processados += len(group_items)
            continue

        family_name = group_items[0].get("family_name", "")[:40]

        if len(group_items) == 1:
            # Apenas 1 item no grupo → cria individual
            item = group_items[0]
            sku = _extract_sku(item)
            payload = _build_single_payload(item)
            if not payload:
                processados += 1
                continue
            if dry_run:
                _log(f"[DRY/single] cpid={cpid} SKU={sku} fn='{family_name}'")
                progress.setdefault("criados", []).append({
                    "sku": sku, "shinsei_id": item.get("id"), "akg_id": "DRY_RUN",
                    "catalog_product_id": cpid
                })
            else:
                ok, result, _ = _post_item_akg(payload, akg_token, shin_token)
                if ok:
                    _log(f"✓ [single] {item.get('id')} SKU={sku} → {result}")
                    progress.setdefault("criados", []).append({
                        "sku": sku, "shinsei_id": item.get("id"), "akg_id": result,
                        "catalog_product_id": cpid
                    })
                    ja_cpids.add(cpid)
                else:
                    _log(f"✗ [single] {item.get('id')} SKU={sku} → {result[:80]}")
                    progress.setdefault("falhas", []).append({
                        "sku": sku, "shinsei_id": item.get("id"), "erro": result,
                        "catalog_product_id": cpid
                    })
                time.sleep(RATE_LIMIT_SLEEP)
        else:
            # 2+ itens → tenta publicar como variações agrupadas
            skus = [_extract_sku(it) for it in group_items]
            payload = _build_variation_payload(group_items)

            if not payload:
                for it, sku in zip(group_items, skus):
                    progress.setdefault("falhas", []).append({
                        "sku": sku, "shinsei_id": it.get("id"), "erro": "payload inválido para variação"
                    })
                processados += len(group_items)
                continue

            if dry_run:
                _log(f"[DRY/variation] cpid={cpid} {len(group_items)} itens fn='{family_name}'")
                for it, sku in zip(group_items, skus):
                    progress.setdefault("criados", []).append({
                        "sku": sku, "shinsei_id": it.get("id"), "akg_id": "DRY_RUN_VAR",
                        "catalog_product_id": cpid
                    })
            else:
                ok, result, _ = _post_item_akg(payload, akg_token, shin_token)
                if ok:
                    _log(f"✓ [variation/{len(group_items)}] cpid={cpid} fn='{family_name}' → {result}")
                    for it, sku in zip(group_items, skus):
                        progress.setdefault("criados", []).append({
                            "sku": sku, "shinsei_id": it.get("id"), "akg_id": result,
                            "catalog_product_id": cpid
                        })
                    ja_cpids.add(cpid)
                else:
                    _log(f"✗ [variation] cpid={cpid} → {result[:100]}")
                    # Fallback: tenta publicar itens individuais do grupo
                    _log(f"  Tentando itens individuais como fallback...")
                    for it, sku in zip(group_items, skus):
                        p2 = _build_single_payload(it)
                        if not p2:
                            continue
                        ok2, res2, _ = _post_item_akg(p2, akg_token, shin_token)
                        if ok2:
                            _log(f"  ✓ [fallback] {it.get('id')} SKU={sku} → {res2}")
                            progress.setdefault("criados", []).append({
                                "sku": sku, "shinsei_id": it.get("id"), "akg_id": res2,
                                "catalog_product_id": cpid
                            })
                        else:
                            progress.setdefault("falhas", []).append({
                                "sku": sku, "shinsei_id": it.get("id"), "erro": res2,
                                "catalog_product_id": cpid
                            })
                        time.sleep(RATE_LIMIT_SLEEP)

        processados += len(group_items)
        _save_progress(progress)

    # ── Fase 4: publica itens individuais (não-variation) ─────────────────────
    for item in individual_items:
        if limit and processados >= limit:
            break

        sku = _extract_sku(item)
        item_id = item.get("id", "")

        if sku and sku in ja_processados:
            progress.setdefault("skipped", []).append({"sku": sku, "shinsei_id": item_id})
            processados += 1
            continue

        payload = _build_single_payload(item)
        if not payload:
            _log(f"Skip {item_id} — payload inválido")
            processados += 1
            continue

        if dry_run:
            title = payload.get("title") or item.get("family_name", "")[:50]
            _log(f"[DRY] {item_id} SKU={sku} → {title[:50]}")
            progress.setdefault("criados", []).append({"sku": sku, "shinsei_id": item_id, "akg_id": "DRY_RUN"})
            processados += 1
            continue

        ok, result, sku_used = _post_item_akg(payload, akg_token, shin_token)
        if ok:
            _log(f"✓ {item_id} SKU={sku_used} → {result}")
            progress.setdefault("criados", []).append({"sku": sku_used, "shinsei_id": item_id, "akg_id": result})
        else:
            _log(f"✗ {item_id} SKU={sku_used} → {result[:80]}")
            progress.setdefault("falhas", []).append({"sku": sku_used, "shinsei_id": item_id, "erro": result})

        processados += 1
        if processados % 20 == 0:
            _save_progress(progress)
        time.sleep(RATE_LIMIT_SLEEP)

    _save_progress(progress)
    return _summary(progress)


def _summary(progress: dict) -> dict:
    return {
        "criados": len(progress.get("criados", [])),
        "falhas": len(progress.get("falhas", [])),
        "skipped": len(progress.get("skipped", [])),
        "criados_detalhe": progress.get("criados", [])[:10],
        "falhas_detalhe": progress.get("falhas", [])[-20:],
        "log": progress.get("log", [])[-20:],
    }
