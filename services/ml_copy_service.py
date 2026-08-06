"""
Copia anúncios ativos da Shinsei (ML) para a conta AKG (ML) preservando SKU.
listing_type_id: "free" (Grátis) para todos.
"""
from __future__ import annotations
import json
import logging
import time
from pathlib import Path
from typing import Iterator

import requests

logger = logging.getLogger("shinsei.ml_copy")

ML_API = "https://api.mercadolibre.com"
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
PROGRESS_PATH = DATA_DIR / "ml_copy_akg_progress.json"

LISTING_TYPE = "free"
INITIAL_QUANTITY = 1
RATE_LIMIT_SLEEP = 0.5   # segundos entre criações
BATCH_SIZE = 20           # multiget ML


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
    return _load_token(DATA_DIR / "ml_tokens_akg.json", "AKG ML")


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


def _iter_shinsei_active_ids(token: str, seller_id: str, offset_start: int = 0) -> Iterator[list[str]]:
    """Itera todos os IDs de anúncios ativos da Shinsei em batches de BATCH_SIZE."""
    offset = offset_start
    limit = 50
    while True:
        r = requests.get(
            f"{ML_API}/users/{seller_id}/items/search",
            params={"status": "active", "offset": offset, "limit": limit},
            headers=_headers(token),
            timeout=20,
        )
        if r.status_code != 200:
            logger.error("Erro buscando itens Shinsei offset=%d: %s", offset, r.text[:200])
            break
        data = r.json()
        ids = data.get("results", [])
        if not ids:
            break
        # Yield em batches de BATCH_SIZE para multiget
        for i in range(0, len(ids), BATCH_SIZE):
            yield ids[i:i + BATCH_SIZE]
        total = data.get("paging", {}).get("total", 0)
        offset += limit
        if offset >= total:
            break


def _get_items_details(ids: list[str], token: str) -> list[dict]:
    """Busca detalhes de até 20 itens de uma vez."""
    r = requests.get(
        f"{ML_API}/items",
        params={"ids": ",".join(ids), "attributes": "id,title,category_id,price,currency_id,condition,pictures,attributes,shipping,seller_custom_field,variations,listing_type_id,status"},
        headers=_headers(token),
        timeout=20,
    )
    if r.status_code != 200:
        return []
    return [entry.get("body", {}) for entry in r.json() if entry.get("code") == 200]


# ── Extrai SKU do item ─────────────────────────────────────────────────────────

def _extract_sku(item: dict) -> str:
    # seller_custom_field é o campo principal
    sku = item.get("seller_custom_field") or ""
    if sku:
        return sku
    # Fallback: atributo SELLER_SKU
    for attr in item.get("attributes", []):
        if attr.get("id") == "SELLER_SKU":
            return attr.get("value_name") or ""
    return ""


# ── Monta payload para ML AKG ─────────────────────────────────────────────────

def _build_payload(item: dict) -> dict | None:
    """Monta o payload para POST /items na conta AKG."""
    title = item.get("title", "")
    category_id = item.get("category_id", "")
    price = item.get("price")
    currency_id = item.get("currency_id", "BRL")
    condition = item.get("condition", "new")
    sku = _extract_sku(item)

    if not title or not category_id or not price:
        return None

    # Fotos — usa source URL para ML fazer o upload na conta AKG
    pictures = []
    for pic in item.get("pictures", []):
        url = pic.get("url") or pic.get("secure_url") or ""
        if url:
            pictures.append({"source": url})

    # Atributos — preserva tudo exceto SELLER_SKU (vai no seller_custom_field)
    attributes = [
        a for a in item.get("attributes", [])
        if a.get("id") not in ("SELLER_SKU",)
           and a.get("value_name")
    ]

    payload: dict = {
        "title": title,
        "category_id": category_id,
        "price": price,
        "currency_id": currency_id,
        "available_quantity": INITIAL_QUANTITY,
        "listing_type_id": LISTING_TYPE,
        "condition": condition,
        "pictures": pictures,
        "attributes": attributes,
    }

    if sku:
        payload["seller_custom_field"] = sku

    # Variações
    variations = item.get("variations", [])
    if variations:
        cleaned_vars = []
        for v in variations:
            cv = {
                "price": v.get("price", price),
                "available_quantity": INITIAL_QUANTITY,
                "attribute_combinations": v.get("attribute_combinations", []),
            }
            if v.get("seller_custom_field"):
                cv["seller_custom_field"] = v["seller_custom_field"]
            # Fotos da variação
            var_pics = []
            for pic in v.get("picture_ids", []):
                # picture_ids são IDs do ML — mantém como referência
                var_pics.append(pic)
            if var_pics:
                cv["picture_ids"] = var_pics
            cleaned_vars.append(cv)
        payload["variations"] = cleaned_vars
        # Remove price/quantity do topo quando tem variações
        payload.pop("price", None)
        payload.pop("available_quantity", None)

    # Shipping — copia modo de envio se existir
    shipping = item.get("shipping", {})
    if shipping.get("mode"):
        payload["shipping"] = {"mode": shipping["mode"]}

    return payload


# ── Publicação ────────────────────────────────────────────────────────────────

def _post_item_akg(payload: dict, token: str) -> tuple[bool, str, str]:
    """Cria item no ML AKG. Retorna (ok, item_id_ou_erro, sku)."""
    sku = payload.get("seller_custom_field", "")
    r = requests.post(f"{ML_API}/items", json=payload, headers=_headers(token), timeout=30)
    if r.status_code in (200, 201):
        new_id = r.json().get("id", "")
        return True, new_id, sku
    return False, r.text[:300], sku


# ── Job principal ─────────────────────────────────────────────────────────────

def run_copy(
    limit: int = 0,
    dry_run: bool = False,
    reset: bool = False,
    status_callback=None,
) -> dict:
    """
    Executa a cópia Shinsei → AKG.
    limit=0 = sem limite.
    dry_run=True = não publica, só retorna o que publicaria.
    reset=True = ignora progresso anterior.
    status_callback(msg) = função chamada a cada item para log em tempo real.
    """
    shin_token = _shinsei_token()
    akg_token = _akg_token()

    seller_id = _get_shinsei_seller_id(shin_token)

    progress = {} if reset else _load_progress()
    if reset:
        progress = {"criados": [], "falhas": [], "skipped": [], "ultimo_offset": 0}

    ja_criados_skus: set[str] = {c["sku"] for c in progress.get("criados", []) if c.get("sku")}
    ja_falha_skus: set[str] = {f["sku"] for f in progress.get("falhas", []) if f.get("sku")}

    offset_start = progress.get("ultimo_offset", 0)

    total_criados = len(progress.get("criados", []))
    total_falhas = len(progress.get("falhas", []))
    total_skipped = len(progress.get("skipped", []))

    def _log(msg: str):
        logger.info(msg)
        if status_callback:
            status_callback(msg)

    _log(f"Iniciando cópia Shinsei→AKG | seller={seller_id} | dry_run={dry_run} | offset_start={offset_start}")

    processados = 0
    offset = 0

    for batch_ids in _iter_shinsei_active_ids(shin_token, seller_id, offset_start):
        items = _get_items_details(batch_ids, shin_token)

        for item in items:
            if limit and processados >= limit:
                _save_progress(progress)
                _log(f"Limite de {limit} atingido.")
                return _summary(progress)

            sku = _extract_sku(item)
            item_id = item.get("id", "")

            if sku and sku in ja_criados_skus:
                total_skipped += 1
                progress.setdefault("skipped", []).append({"sku": sku, "shinsei_id": item_id})
                processados += 1
                continue

            payload = _build_payload(item)
            if not payload:
                _log(f"Skip {item_id} — payload inválido (sem título/categoria/preço)")
                total_skipped += 1
                processados += 1
                continue

            if dry_run:
                _log(f"[DRY] Publicaria: {item_id} SKU={sku} → {payload.get('title','')[:50]}")
                total_criados += 1
                processados += 1
                continue

            ok, result, sku_used = _post_item_akg(payload, akg_token)
            if ok:
                total_criados += 1
                progress.setdefault("criados", []).append({"sku": sku_used, "shinsei_id": item_id, "akg_id": result})
                _log(f"✓ {item_id} SKU={sku_used} → {result}")
            else:
                total_falhas += 1
                progress.setdefault("falhas", []).append({"sku": sku_used, "shinsei_id": item_id, "erro": result})
                _log(f"✗ {item_id} SKU={sku_used} → {result[:100]}")

            processados += 1
            time.sleep(RATE_LIMIT_SLEEP)

        progress["ultimo_offset"] = offset_start + (processados * 50 // max(processados, 1))
        _save_progress(progress)
        offset += BATCH_SIZE

    _save_progress(progress)
    return _summary(progress)


def _summary(progress: dict) -> dict:
    return {
        "criados": len(progress.get("criados", [])),
        "falhas": len(progress.get("falhas", [])),
        "skipped": len(progress.get("skipped", [])),
        "falhas_detalhe": progress.get("falhas", [])[:20],
    }
