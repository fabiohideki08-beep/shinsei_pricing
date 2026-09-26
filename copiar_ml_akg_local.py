"""
Script local para copiar anúncios Shinsei → AKG no ML.
Roda na sua máquina, sem depender do Render.
Salva progresso em data/ml_copy_akg_progress.json a cada batch.
Para retomar de onde parou, rode novamente sem --reset.
"""
import json
import sys
import time
import argparse
from collections import defaultdict
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")

ML_API = "https://api.mercadolibre.com"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PROGRESS_PATH = DATA_DIR / "ml_copy_akg_progress.json"
TOKEN_SHINSEI = DATA_DIR / "ml_tokens.json"
TOKEN_AKG = DATA_DIR / "ml_tokens_akg.json"
RENDER_BASE = "https://shinsei-pricing.onrender.com"


def _sync_akg_token():
    """Baixa o token AKG atualizado do servidor Render e salva localmente."""
    try:
        r = requests.get(f"{RENDER_BASE}/ml/tokens2", timeout=15)
        if r.status_code == 200:
            data = r.json().get("data", {})
            if data.get("access_token"):
                TOKEN_AKG.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                print(f"Token AKG sincronizado do servidor.", flush=True)
                return True
        print(f"Aviso: não foi possível sincronizar token AKG ({r.status_code})", flush=True)
    except Exception as e:
        print(f"Aviso: erro ao sincronizar token AKG: {e}", flush=True)
    return False

LISTING_TYPE = "bronze"
INITIAL_QUANTITY = 1
RATE_LIMIT_SLEEP = 0.4
BATCH_SIZE = 20
VARIATION_CATEGORIES = {"MLB264861"}

_cpid_cache: dict[str, str] = {}


def _load_token(path: Path, label: str) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    token = data.get("access_token", "")
    if not token:
        raise RuntimeError(f"Token {label} vazio.")
    return token


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _load_progress() -> dict:
    if PROGRESS_PATH.exists():
        try:
            return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"criados": [], "falhas": [], "skipped": []}


def _save_progress(p: dict):
    PROGRESS_PATH.write_text(json.dumps(p, indent=2, ensure_ascii=False), encoding="utf-8")


def _iter_shinsei_active_ids(token: str, seller_id: str):
    scroll_id = None
    while True:
        params = {"status": "active", "limit": 100, "search_type": "scan"}
        if scroll_id:
            params["scroll_id"] = scroll_id
        r = requests.get(f"{ML_API}/users/{seller_id}/items/search",
                         params=params, headers=_headers(token), timeout=20)
        if r.status_code != 200:
            print(f"Erro busca scroll: {r.text[:100]}", flush=True)
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


def _get_items_details(ids: list, token: str) -> list:
    results = []
    for iid in ids:
        try:
            r = requests.get(f"{ML_API}/items/{iid}", headers=_headers(token), timeout=15)
            if r.status_code == 200:
                results.append(r.json())
        except Exception as e:
            print(f"  Erro detalhe {iid}: {e}", flush=True)
        time.sleep(0.08)
    return results


def _extract_sku(item: dict) -> str:
    sku = item.get("seller_custom_field") or ""
    if sku:
        return sku
    for attr in item.get("attributes", []):
        if attr.get("id") == "SELLER_SKU":
            return attr.get("value_name") or ""
    for attr in item.get("attributes", []):
        if attr.get("id") == "GTIN":
            return attr.get("value_name") or ""
    return ""


def _extract_hair_tone(item: dict):
    for attr in item.get("attributes", []):
        if attr.get("id") == "HAIR_TONE":
            return {"id": "HAIR_TONE",
                    "value_id": attr.get("value_id"),
                    "value_name": attr.get("value_name") or ""}
    return None


def _pics(item: dict) -> list:
    result = []
    for pic in item.get("pictures", []):
        url = pic.get("secure_url") or pic.get("url") or ""
        if url:
            result.append({"source": url.replace("http://", "https://", 1)})
    return result


def _find_current_cpid(family_name: str, category_id: str, token: str) -> str:
    key = f"{family_name}|{category_id}"
    if key in _cpid_cache:
        return _cpid_cache[key]
    r = requests.get(f"{ML_API}/products/search",
                     params={"site_id": "MLB", "q": family_name[:80]},
                     headers=_headers(token), timeout=15)
    if r.status_code != 200:
        return ""
    results = r.json().get("results", [])
    active = [p for p in results if p.get("status") == "active"]
    candidates = active or results
    cpid = candidates[0].get("id", "") if candidates else ""
    _cpid_cache[key] = cpid
    return cpid


def _build_single_payload(item: dict):
    category_id = item.get("category_id", "")
    price = item.get("price")
    if not category_id or not price:
        return None
    family_name = item.get("family_name") or ""
    catalog_product_id = item.get("catalog_product_id") or ""
    domain_id = item.get("domain_id") or ""
    is_catalog = bool(family_name or catalog_product_id)
    payload = {
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
    sku = _extract_sku(item)
    if sku:
        payload["seller_custom_field"] = sku
    return payload


def _build_variation_payload(items: list):
    if not items:
        return None
    anchor = items[0]
    category_id = anchor.get("category_id", "")
    family_name = anchor.get("family_name") or ""
    catalog_product_id = anchor.get("catalog_product_id") or ""
    if not category_id or not (family_name or catalog_product_id):
        return None
    variations = []
    for item in items:
        price = item.get("price")
        if not price:
            continue
        sku = _extract_sku(item)
        hair_tone = _extract_hair_tone(item)
        var = {"price": price, "available_quantity": INITIAL_QUANTITY}
        if sku:
            var["seller_custom_field"] = sku
        if hair_tone:
            var["attribute_combinations"] = [hair_tone]
        variations.append(var)
    if not variations:
        return None
    payload = {
        "category_id": category_id,
        "listing_type_id": LISTING_TYPE,
        "condition": anchor.get("condition", "new"),
        "currency_id": anchor.get("currency_id", "BRL"),
        "pictures": _pics(anchor),
        "variations": variations,
    }
    if family_name:
        payload["family_name"] = family_name[:60]
    if catalog_product_id:
        payload["catalog_product_id"] = catalog_product_id
    return payload


def _post_item(payload: dict, akg_token: str, shin_token: str):
    r = requests.post(f"{ML_API}/items", json=payload,
                      headers=_headers(akg_token), timeout=30)
    if r.status_code in (200, 201):
        return True, r.json().get("id", "")
    # Fallback cpid para 500 com family_name
    if r.status_code == 500 and payload.get("family_name"):
        new_cpid = _find_current_cpid(
            payload["family_name"], payload.get("category_id", ""), shin_token)
        if new_cpid and new_cpid != payload.get("catalog_product_id"):
            p2 = {**payload, "catalog_product_id": new_cpid}
            r2 = requests.post(f"{ML_API}/items", json=p2,
                                headers=_headers(akg_token), timeout=30)
            if r2.status_code in (200, 201):
                return True, r2.json().get("id", "")
    return False, r.text[:300]


def run(reset=False, dry_run=False, limit=0):
    _sync_akg_token()
    shin_token = _load_token(TOKEN_SHINSEI, "Shinsei")
    akg_token = _load_token(TOKEN_AKG, "AKG")

    r = requests.get(f"{ML_API}/users/me", headers=_headers(shin_token), timeout=15)
    seller_id = str(r.json()["id"])
    print(f"Seller Shinsei: {seller_id}")

    progress = ({"criados": [], "falhas": [], "skipped": []}
                if reset else _load_progress())

    ja_skus: set = {c.get("sku", "") for c in progress.get("criados", [])} | \
                   {f.get("sku", "") for f in progress.get("falhas", [])}
    ja_cpids: set = {c.get("catalog_product_id", "") for c in progress.get("criados", [])
                     if c.get("catalog_product_id")}

    # Fase 1: coleta todos os itens
    print("Coletando itens Shinsei...", flush=True)
    all_items = []
    batch_n = 0
    for batch_ids in _iter_shinsei_active_ids(shin_token, seller_id):
        details = _get_items_details(batch_ids, shin_token)
        all_items.extend(details)
        batch_n += 1
        if batch_n % 5 == 0:
            print(f"  coletados {len(all_items)}...", flush=True)
        if limit and len(all_items) >= limit * 3:
            break

    print(f"Total: {len(all_items)} itens", flush=True)

    # Fase 2: agrupa por catalog_product_id
    variation_groups: dict = defaultdict(list)
    individual_items: list = []
    for item in all_items:
        cpid = item.get("catalog_product_id") or ""
        cat = item.get("category_id") or ""
        if cpid and cat in VARIATION_CATEGORIES:
            variation_groups[cpid].append(item)
        else:
            individual_items.append(item)

    print(f"Grupos variação: {len(variation_groups)} | Individuais: {len(individual_items)}", flush=True)

    criados = len(progress.get("criados", []))
    falhas = len(progress.get("falhas", []))
    processados = 0

    def log(msg):
        print(msg, flush=True)

    # Fase 3: publica grupos
    for cpid, group_items in variation_groups.items():
        if limit and processados >= limit:
            break
        group_skus = [_extract_sku(it) for it in group_items]

        if all(sku and sku in ja_skus for sku in group_skus if sku):
            processados += len(group_items)
            continue
        if cpid in ja_cpids:
            processados += len(group_items)
            continue

        fn = (group_items[0].get("family_name","") or "")[:40]

        if len(group_items) == 1:
            item = group_items[0]
            sku = _extract_sku(item)
            payload = _build_single_payload(item)
            if not payload:
                processados += 1
                continue
            if dry_run:
                log(f"[DRY/single] {item.get('id')} cpid={cpid} SKU={sku}")
                criados += 1
            else:
                ok, result = _post_item(payload, akg_token, shin_token)
                if ok:
                    log(f"✓ {item.get('id')} SKU={sku} → {result}")
                    progress["criados"].append({"sku": sku, "shinsei_id": item.get("id"), "akg_id": result, "catalog_product_id": cpid})
                    ja_cpids.add(cpid)
                    criados += 1
                else:
                    log(f"✗ {item.get('id')} SKU={sku} → {result[:80]}")
                    progress["falhas"].append({"sku": sku, "shinsei_id": item.get("id"), "erro": result, "catalog_product_id": cpid})
                    falhas += 1
                time.sleep(RATE_LIMIT_SLEEP)
        else:
            skus = [_extract_sku(it) for it in group_items]
            payload = _build_variation_payload(group_items)
            if not payload:
                processados += len(group_items)
                continue
            if dry_run:
                log(f"[DRY/variation] cpid={cpid} {len(group_items)} itens fn='{fn}'")
                criados += len(group_items)
            else:
                ok, result = _post_item(payload, akg_token, shin_token)
                if ok:
                    log(f"✓ [var/{len(group_items)}] cpid={cpid} fn='{fn}' → {result}")
                    for it, sku in zip(group_items, skus):
                        progress["criados"].append({"sku": sku, "shinsei_id": it.get("id"), "akg_id": result, "catalog_product_id": cpid})
                    ja_cpids.add(cpid)
                    criados += len(group_items)
                else:
                    log(f"✗ [var] cpid={cpid} → {result[:80]}")
                    log("  Fallback individual...")
                    for it, sku in zip(group_items, skus):
                        p2 = _build_single_payload(it)
                        if not p2:
                            continue
                        ok2, res2 = _post_item(p2, akg_token, shin_token)
                        if ok2:
                            log(f"  ✓ {it.get('id')} SKU={sku} → {res2}")
                            progress["criados"].append({"sku": sku, "shinsei_id": it.get("id"), "akg_id": res2, "catalog_product_id": cpid})
                            criados += 1
                        else:
                            progress["falhas"].append({"sku": sku, "shinsei_id": it.get("id"), "erro": res2, "catalog_product_id": cpid})
                            falhas += 1
                        time.sleep(RATE_LIMIT_SLEEP)

        processados += len(group_items)
        if processados % 50 == 0:
            _save_progress(progress)
            print(f"  --- criados={criados} falhas={falhas} processados={processados} ---", flush=True)

    # Fase 4: individuais
    for item in individual_items:
        if limit and processados >= limit:
            break
        sku = _extract_sku(item)
        item_id = item.get("id", "")

        if sku and sku in ja_skus:
            processados += 1
            continue

        payload = _build_single_payload(item)
        if not payload:
            processados += 1
            continue

        if dry_run:
            title = payload.get("title") or payload.get("family_name", "")[:50]
            log(f"[DRY] {item_id} SKU={sku} title={title[:40]!r}")
            criados += 1
        else:
            ok, result = _post_item(payload, akg_token, shin_token)
            if ok:
                log(f"✓ {item_id} SKU={sku} → {result}")
                progress["criados"].append({"sku": sku, "shinsei_id": item_id, "akg_id": result})
                ja_skus.add(sku)
                criados += 1
            else:
                log(f"✗ {item_id} SKU={sku} → {result[:80]}")
                progress["falhas"].append({"sku": sku, "shinsei_id": item_id, "erro": result})
                ja_skus.add(sku)
                falhas += 1
            time.sleep(RATE_LIMIT_SLEEP)

        processados += 1
        if processados % 50 == 0:
            _save_progress(progress)
            print(f"  --- criados={criados} falhas={falhas} processados={processados} ---", flush=True)

    _save_progress(progress)
    print(f"\n=== CONCLUIDO: criados={criados} falhas={falhas} ===", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="Começa do zero")
    parser.add_argument("--dry-run", action="store_true", help="Simula sem criar")
    parser.add_argument("--limit", type=int, default=0, help="Limite de itens")
    args = parser.parse_args()
    run(reset=args.reset, dry_run=args.dry_run, limit=args.limit)
