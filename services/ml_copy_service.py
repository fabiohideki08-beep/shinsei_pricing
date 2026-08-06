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

LISTING_TYPE = "bronze"
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
    """Itera todos os IDs de anúncios ativos da Shinsei em batches de BATCH_SIZE.
    Usa scroll_id para ultrapassar o limite de ~1060 itens do offset-based search.
    """
    limit = 100
    scroll_id: str | None = None
    first_page = True
    pages_yielded = 0

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

        # Na primeira página, pula as já processadas (offset_start)
        if first_page and offset_start > 0:
            skip = min(offset_start, len(ids))
            ids = ids[skip:]
            first_page = False

        # Yield em batches de BATCH_SIZE
        for i in range(0, len(ids), BATCH_SIZE):
            yield ids[i:i + BATCH_SIZE]
            pages_yielded += 1

        if not scroll_id:
            break


def _get_items_details(ids: list[str], token: str) -> list[dict]:
    """Busca detalhes individualmente para receber todos os campos (incluindo family_name)."""
    results = []
    for item_id in ids:
        try:
            r = requests.get(
                f"{ML_API}/items/{item_id}",
                headers=_headers(token),
                timeout=15,
            )
            if r.status_code == 200:
                results.append(r.json())
            else:
                logger.warning("Falha ao buscar %s: HTTP %d", item_id, r.status_code)
        except Exception as e:
            logger.warning("Erro ao buscar %s: %s", item_id, e)
        time.sleep(0.1)
    return results


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

def _get_catalog_product_id(family_name: str, category_id: str, token: str) -> str | None:
    """Busca catalog_product_id via /products/search. Necessário para criar itens de catálogo."""
    try:
        r = requests.get(
            f"{ML_API}/products/search",
            params={"q": family_name, "site_id": "MLB", "category": category_id},
            headers=_headers(token),
            timeout=15,
        )
        if r.status_code != 200:
            return None
        results = r.json().get("results", [])
        # Prefere active, aceita qualquer
        for p in results:
            if p.get("status") == "active":
                return p.get("id")
        if results:
            return results[0].get("id")
    except Exception as e:
        logger.warning("Erro buscando catalog_product_id para '%s': %s", family_name, e)
    return None


def _build_payload(item: dict, catalog_product_id: str | None = None) -> dict | None:
    """Monta o payload para POST /items na conta AKG."""
    title = item.get("title", "")
    category_id = item.get("category_id", "")
    price = item.get("price")
    currency_id = item.get("currency_id", "BRL")
    condition = item.get("condition", "new")
    sku = _extract_sku(item)

    if not title or not category_id or not price:
        return None

    # Fotos — usa source URL para ML fazer o upload na conta AKG (força https)
    pictures = []
    for pic in item.get("pictures", []):
        url = pic.get("secure_url") or pic.get("url") or ""
        if url:
            url = url.replace("http://", "https://", 1)
            pictures.append({"source": url})

    # Atributos — preserva tudo exceto SELLER_SKU (vai no seller_custom_field)
    attributes = [
        a for a in item.get("attributes", [])
        if a.get("id") not in ("SELLER_SKU",)
    ]

    family_name = item.get("family_name") or ""
    domain_id = item.get("domain_id") or ""
    is_catalog = bool(family_name)

    payload: dict = {
        "category_id": category_id,
        "price": price,
        "currency_id": currency_id,
        "available_quantity": INITIAL_QUANTITY,
        "listing_type_id": LISTING_TYPE,
        "condition": condition,
        "pictures": pictures,
    }

    if is_catalog:
        # Itens de catálogo: ML exige family_name + catalog_product_id juntos
        # ML limita family_name a 60 caracteres
        payload["family_name"] = family_name[:60]
        if catalog_product_id:
            payload["catalog_product_id"] = catalog_product_id
        if domain_id:
            payload["domain_id"] = domain_id
    else:
        # Itens normais: title e attributes são livres
        payload["title"] = title
        payload["attributes"] = attributes

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

    # Shipping — não copia (deixa ML usar padrão da conta AKG)

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
        progress.setdefault("log", []).append(msg)
        if status_callback:
            status_callback(msg)

    _log(f"Iniciando cópia Shinsei→AKG | seller_shinsei={seller_id} | dry_run={dry_run} | offset_start={offset_start}")
    # Verifica primeiro batch para diagnóstico
    import requests as _req2
    _r = _req2.get(f"{ML_API}/users/{seller_id}/items/search",
                   params={"status": "active", "offset": 0, "limit": 1},
                   headers=_headers(shin_token), timeout=20)
    _total = _r.json().get("paging", {}).get("total", "ERR") if _r.status_code == 200 else f"HTTP {_r.status_code}"
    _log(f"Total de itens ativos na Shinsei: {_total}")

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

            # Para itens de catálogo, busca catalog_product_id antes de montar o payload
            catalog_pid: str | None = None
            fn = item.get("family_name") or ""
            if fn:
                catalog_pid = _get_catalog_product_id(fn, item.get("category_id", ""), akg_token)
                if catalog_pid:
                    _log(f"  catalog_product_id={catalog_pid} para '{fn[:40]}'")
                else:
                    _log(f"  Aviso: catalog_product_id não encontrado para '{fn[:40]}' — tentando só com family_name")

            payload = _build_payload(item, catalog_product_id=catalog_pid)
            if not payload:
                _log(f"Skip {item_id} — payload inválido (sem título/categoria/preço)")
                total_skipped += 1
                processados += 1
                continue

            if dry_run:
                _log(f"[DRY] Publicaria: {item_id} SKU={sku} → {payload.get('title','')[:50]}")
                progress.setdefault("criados", []).append({"sku": sku, "shinsei_id": item_id, "akg_id": "DRY_RUN", "title": payload.get("title","")[:60]})
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

        progress["ultimo_offset"] = offset_start + processados
        _save_progress(progress)

    _save_progress(progress)
    return _summary(progress)


def _summary(progress: dict) -> dict:
    return {
        "criados": len(progress.get("criados", [])),
        "falhas": len(progress.get("falhas", [])),
        "skipped": len(progress.get("skipped", [])),
        "criados_detalhe": progress.get("criados", [])[:10],
        "falhas_detalhe": progress.get("falhas", [])[:20],
        "log": progress.get("log", [])[-20:],
    }
