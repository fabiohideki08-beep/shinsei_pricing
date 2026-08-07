"""
Cria anúncios tipo "gratuito" no AKG baseados nos bronze já criados.
Fonte de preços: anúncio bronze existente (ou premium se não tiver bronze).
Salva progresso em data/ml_gratuito_akg_progress.json.
"""
import json
import sys
import time
import argparse
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")

ML_API = "https://api.mercadolibre.com"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

COPY_PROGRESS_PATH = DATA_DIR / "ml_copy_akg_progress.json"
PROGRESS_PATH = DATA_DIR / "ml_gratuito_akg_progress.json"
TOKEN_AKG = DATA_DIR / "ml_tokens_akg.json"
TOKEN_SHINSEI = DATA_DIR / "ml_tokens.json"

LISTING_TYPE = "free"
RATE_LIMIT_SLEEP = 0.5
BATCH_SIZE = 20

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


def _get_item(item_id: str, token: str) -> dict | None:
    r = requests.get(f"{ML_API}/items/{item_id}", headers=_headers(token), timeout=15)
    if r.status_code == 200:
        return r.json()
    return None


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


def _pics(item: dict) -> list:
    result = []
    for pic in item.get("pictures", []):
        url = pic.get("secure_url") or pic.get("url") or ""
        if url:
            result.append({"source": url.replace("http://", "https://", 1)})
    return result


def _build_payload(item: dict) -> dict | None:
    category_id = item.get("category_id", "")
    price = item.get("price")
    if not category_id or not price:
        return None

    family_name = item.get("family_name") or ""
    catalog_product_id = item.get("catalog_product_id") or ""
    domain_id = item.get("domain_id") or ""
    sku = item.get("seller_custom_field") or ""

    payload = {
        "category_id": category_id,
        "price": price,
        "currency_id": item.get("currency_id", "BRL"),
        "available_quantity": 1,
        "listing_type_id": LISTING_TYPE,
        "condition": item.get("condition", "new"),
        "pictures": _pics(item),
    }

    if family_name or catalog_product_id:
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

    if sku:
        payload["seller_custom_field"] = sku

    return payload


def _post_item(payload: dict, akg_token: str) -> tuple[bool, str]:
    r = requests.post(f"{ML_API}/items", json=payload,
                      headers=_headers(akg_token), timeout=30)
    if r.status_code in (200, 201):
        return True, r.json().get("id", "")
    # Fallback cpid
    if r.status_code == 500 and payload.get("family_name"):
        new_cpid = _find_current_cpid(
            payload["family_name"], payload.get("category_id", ""), akg_token)
        if new_cpid and new_cpid != payload.get("catalog_product_id"):
            p2 = {**payload, "catalog_product_id": new_cpid}
            r2 = requests.post(f"{ML_API}/items", json=p2,
                                headers=_headers(akg_token), timeout=30)
            if r2.status_code in (200, 201):
                return True, r2.json().get("id", "")
    return False, r.text[:300]


def run(reset=False, dry_run=False, limit=0):
    akg_token = _load_token(TOKEN_AKG, "AKG")

    # Carrega bronze já criados
    copy_data = json.loads(COPY_PROGRESS_PATH.read_text(encoding="utf-8"))
    bronze_items = copy_data.get("criados", [])
    print(f"Bronze disponíveis: {len(bronze_items)}", flush=True)

    progress = ({"criados": [], "falhas": [], "skipped": []}
                if reset else _load_progress())

    ja_akg_ids: set = {c.get("bronze_akg_id", "") for c in progress["criados"]} | \
                      {f.get("bronze_akg_id", "") for f in progress["falhas"]}

    criados = len(progress["criados"])
    falhas = len(progress["falhas"])
    processados = 0

    for entry in bronze_items:
        if limit and processados >= limit:
            break

        bronze_akg_id = entry.get("akg_id", "")
        sku = entry.get("sku", "")

        if not bronze_akg_id:
            processados += 1
            continue

        if bronze_akg_id in ja_akg_ids:
            processados += 1
            continue

        # Busca detalhes do anúncio bronze AKG
        item = _get_item(bronze_akg_id, akg_token)
        if not item:
            print(f"✗ Não encontrou {bronze_akg_id}", flush=True)
            progress["falhas"].append({"sku": sku, "bronze_akg_id": bronze_akg_id, "erro": "item_not_found"})
            falhas += 1
            processados += 1
            time.sleep(0.1)
            continue

        payload = _build_payload(item)
        if not payload:
            processados += 1
            continue

        if dry_run:
            title = payload.get("title") or payload.get("family_name", "")[:50]
            print(f"[DRY] {bronze_akg_id} SKU={sku} preço={payload['price']} → {title[:40]!r}", flush=True)
            criados += 1
        else:
            ok, result = _post_item(payload, akg_token)
            if ok:
                print(f"✓ {bronze_akg_id} SKU={sku} → {result}", flush=True)
                progress["criados"].append({
                    "sku": sku,
                    "bronze_akg_id": bronze_akg_id,
                    "gratuito_akg_id": result,
                })
                criados += 1
            else:
                print(f"✗ {bronze_akg_id} SKU={sku} → {result[:80]}", flush=True)
                progress["falhas"].append({
                    "sku": sku,
                    "bronze_akg_id": bronze_akg_id,
                    "erro": result,
                })
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
