# -*- coding: utf-8 -*-
"""
GMC (Google Merchant Center) monitoring and correction module.
Corrections are applied to both GMC (Content API) and Shopify (Admin API).
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/gmc", tags=["gmc"])

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"

# ── GMC config ────────────────────────────────────────────────────────────────
SA_FILE          = DATA_DIR / "google_service_account.json"
BLACKLIST_PATH   = DATA_DIR / "gmc_blacklist.json"
MERCHANT_ID      = 5071388981
SCOPES           = ["https://www.googleapis.com/auth/content"]

# Categorias que são excluídas automaticamente do GMC após cada scan
AUTO_DELETE_CATEGORIES: set[str] = {"adult_legit", "adult_false"}

READONLY_FIELDS = {
    "id", "offerId", "feedLabel", "contentLanguage", "targetCountry",
    "channel", "kind", "customAttributes", "source", "warnings",
    "destinations", "status",
}

# ── Shopify config ────────────────────────────────────────────────────────────
SHOPIFY_STORE       = "pknw4n-eg"
SHOPIFY_API_VERSION = "2024-01"
SHOPIFY_CONFIG_PATH = DATA_DIR / "shopify_config.json"


def _shopify_token() -> str:
    try:
        cfg = json.loads(SHOPIFY_CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg.get("access_token", "")
    except Exception:
        return ""


def _shopify_headers() -> dict:
    return {
        "X-Shopify-Access-Token": _shopify_token(),
        "Content-Type": "application/json",
    }


def _shopify_id_from_gmc(gmc_product_id: str) -> str | None:
    """Extract numeric Shopify product ID from GMC product_id.

    Known formats:
      - online:pt:BRL_93913186609:shopify_ZZ_10620245049649_53165442498865
        → 10620245049649  (first long number before the variant number)
      - online:pt:BR:gid://shopify/Product/8089556517056
        → 8089556517056
      - online:pt:BR:8089556517056
        → 8089556517056
    """
    import re
    try:
        # Find two consecutive long numeric segments separated by underscore.
        # Matches product_id_variant_id pattern (both 10+ digits).
        m = re.search(r'_(\d{10,})_(\d{10,})', gmc_product_id)
        if m:
            return m.group(1)
        # GID format: gid://shopify/Product/NUMERIC
        if "/Product/" in gmc_product_id:
            candidate = gmc_product_id.split("/Product/")[-1]
            return candidate if candidate.isdigit() else None
        # Plain numeric suffix after last colon
        candidate = gmc_product_id.split(":")[-1]
        return candidate if candidate.isdigit() else None
    except Exception:
        return None


# ── Issue classification ──────────────────────────────────────────────────────
ADULT_KEYWORDS  = ["virilha", "beijável", "hot candy", "erótic", "intim", "sensu", "sexy"]
RECALLED_ISSUES = {"recalled product", "produto retirado do mercado"}
ADULT_ISSUES    = {"restricted adult content", "personalized advertising: sexual interests"}
AVAIL_ISSUES    = {"automatic updates: mismatched availability"}
PRICE_ISSUES    = {"automatic updates: strikethrough price"}
IMG_ISSUES      = {"image too small"}
CAPS_ISSUES     = {"excessive capitalization [title]"}
GTIN_ISSUES     = {"unsupported gtin value"}


def _classify_product(p: dict) -> str:
    issues_text = " ".join(
        i.get("description", "").lower() for i in p.get("issues", [])
    )
    if any(r in issues_text for r in RECALLED_ISSUES):
        return "recalled"
    if any(a in issues_text for a in ADULT_ISSUES):
        title_lower = p.get("title", "").lower()
        if any(kw in title_lower for kw in ADULT_KEYWORDS):
            return "adult_legit"
        return "adult_false"
    if any(v in issues_text for v in AVAIL_ISSUES):
        return "availability"
    if any(v in issues_text for v in PRICE_ISSUES):
        return "price"
    if any(v in issues_text for v in IMG_ISSUES):
        return "image"
    if any(v in issues_text for v in CAPS_ISSUES):
        return "caps"
    if any(v in issues_text for v in GTIN_ISSUES):
        return "gtin"
    return "other"


# ── Background state ──────────────────────────────────────────────────────────
_scan: dict = {
    "rodando": False, "concluido": False, "erro": None,
    "total": 0, "pagina": 0, "iniciado_em": None, "concluido_em": None,
    "resultado": None,
}
_fix: dict = {
    "rodando": False, "concluido": False, "erro": None,
    "total": 0, "feitos": 0, "erros_count": 0, "log": [],
    "iniciado_em": None, "concluido_em": None,
}


# ── GMC helpers ───────────────────────────────────────────────────────────────

def _build_service():
    import httplib2
    import google_auth_httplib2
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    # Aceita credenciais via env var (JSON ou base64) ou arquivo local
    sa_json_env = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if sa_json_env:
        import base64
        try:
            sa_info = json.loads(sa_json_env)
        except Exception:
            sa_info = json.loads(base64.b64decode(sa_json_env).decode())
        creds = service_account.Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    else:
        creds = service_account.Credentials.from_service_account_file(
            str(SA_FILE), scopes=SCOPES
        )
    http = httplib2.Http(timeout=60)
    authed_http = google_auth_httplib2.AuthorizedHttp(creds, http=http)
    return build("content", "v2.1", http=authed_http)


def _clean(product: dict) -> dict:
    return {k: v for k, v in product.items() if k not in READONLY_FIELDS}


# ── Blacklist ─────────────────────────────────────────────────────────────────

def _load_blacklist() -> dict:
    """Retorna {'product_ids': [...], 'categories': [...]}"""
    try:
        if BLACKLIST_PATH.exists():
            return json.loads(BLACKLIST_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"product_ids": [], "categories": list(AUTO_DELETE_CATEGORIES)}


def _save_blacklist(bl: dict):
    BLACKLIST_PATH.parent.mkdir(exist_ok=True)
    BLACKLIST_PATH.write_text(json.dumps(bl, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Fila de correção ─────────────────────────────────────────────────────────

GMC_FILA_PATH        = DATA_DIR / "gmc_fila_reprovados.json"
GMC_SCAN_STATUS_PATH = DATA_DIR / "gmc_scan_status.json"


def _load_fila() -> dict:
    try:
        if GMC_FILA_PATH.exists():
            return json.loads(GMC_FILA_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"items": []}


def _save_fila(fila: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GMC_FILA_PATH.write_text(json.dumps(fila, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_scan_status() -> dict:
    try:
        if GMC_SCAN_STATUS_PATH.exists():
            return json.loads(GMC_SCAN_STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_scan_status(status: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GMC_SCAN_STATUS_PATH.write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _enqueue_reprovado(product: dict):
    """Adiciona produto reprovado à fila de correção (idempotente por product_id)."""
    import uuid
    fila  = _load_fila()
    items = fila.setdefault("items", [])

    pid      = product["product_id"]
    agora    = datetime.utcnow().isoformat()

    # Se já existe na fila com status aguardando, apenas atualiza os motivos
    existing = next((i for i in items if i["product_id"] == pid and i["status"] == "aguardando"), None)
    if existing:
        existing["issues"]         = product.get("issues", existing.get("issues", []))
        existing["category"]       = product.get("category", existing.get("category", "other"))
        existing["motivo_remocao"] = product.get("motivo_remocao", existing.get("motivo_remocao", ""))
        existing["updated_at"]     = agora
        _save_fila(fila)
        return

    # Extrai o Shopify variant ID do offer_id para construir link de edição
    offer_id   = product.get("offer_id", "")
    shopify_id = ""
    if "shopify_ZZ_" in offer_id:
        parts = offer_id.split("_")
        # formato: shopify_ZZ_{product_id}_{variant_id}
        if len(parts) >= 4:
            shopify_id = parts[2]

    items.append({
        "id":             str(uuid.uuid4()),
        "product_id":     pid,
        "offer_id":       offer_id,
        "shopify_id":     shopify_id,
        "title":          product.get("title", ""),
        "link":           product.get("link", ""),
        "issues":         product.get("issues", []),
        "category":       product.get("category", "other"),
        "motivo_remocao": product.get("motivo_remocao", ""),
        "severity":     product.get("severity", "disapproved"),
        "removed_at":   agora,
        "status":       "aguardando",   # aguardando | corrigido | ignorado
        "notas":        "",
        "updated_at":   agora,
    })
    _save_fila(fila)


# ── GMC helpers ───────────────────────────────────────────────────────────────

def _delete_from_gmc(service, product_id: str) -> dict:
    """Exclui permanentemente um produto do GMC."""
    try:
        service.products().delete(merchantId=MERCHANT_ID, productId=product_id).execute()
        return {"ok": True, "action": "deleted_permanently"}
    except Exception as e:
        return {"ok": False, "action": "delete_failed", "error": str(e)}


def _is_shopping_blocked(product: dict) -> bool:
    """
    Retorna True se o produto está bloqueado de aparecer no Google Shopping.

    Critérios:
    1. Qualquer destination Shopping/Shopping ads/SurfacesAcrossGoogle/DisplayAds
       com status 'disapproved' ou 'excluded' no destinationStatuses.
    2. Se não houver destinationStatuses, verifica se o país BR está na lista
       de países reprovados de algum destino Shopping.
    """
    SHOPPING_DESTS = {
        "Shopping", "Shopping ads",
        "SurfacesAcrossGoogle", "Surfaces across Google",
        "DisplayAds", "Display ads",
        "Free listings", "Free local listings",
    }
    destinations = product.get("destinations", [])
    if not destinations:
        return False

    for d in destinations:
        dest_name = d.get("destination", "")
        # Verifica pelo nome exato ou por substring (Google muda nomes ocasionalmente)
        is_shopping = dest_name in SHOPPING_DESTS or any(
            kw in dest_name for kw in ("Shopping", "Surfaces", "Display")
        )
        if not is_shopping:
            continue
        status = d.get("status", "")
        if status in ("disapproved", "excluded"):
            return True
        # Alguns responses usam disapprovedCountries em vez de status global
        if d.get("disapprovedCountries"):
            return True
    return False


def _auto_delete_after_scan(resultado: dict) -> list[dict]:
    """
    Regra absoluta: qualquer produto que bloqueie a exibição no Google Shopping
    é removido imediatamente e enviado para a fila de correção.

    Critérios de remoção:
      1. Severity = 'disapproved' (reprovado direto)
      2. Severity = 'limited' MAS com Shopping:disapproved no destinationStatuses
         (imagem inválida, sem estoque, imagem pequena, pausado, preço divergente, etc.)
      3. product_id na blacklist manual
    """
    id_set = set((_load_blacklist()).get("product_ids", []))

    disapproved = resultado.get("disapproved", [])
    limited     = resultado.get("limited", [])

    seen: set[str] = set()
    to_delete: list[tuple[str, str]] = []

    # Regra 1: todos os reprovados
    for p in disapproved:
        pid = p["product_id"]
        if pid in seen:
            continue
        seen.add(pid)
        cat = p.get("category", "other")
        to_delete.append((pid, f"disapproved:{cat}"))

    # Regra 2: limitados que estão efetivamente bloqueados no Shopping
    for p in limited:
        pid = p["product_id"]
        if pid in seen:
            continue
        if _is_shopping_blocked(p):
            seen.add(pid)
            cat = p.get("category", "other")
            # Constrói motivo legível baseado nos issues
            issue_descs = list({i.get("description", "") for i in p.get("issues", []) if i.get("description")})
            motivo = f"shopping_blocked:{cat}:{'; '.join(issue_descs[:2])}"
            to_delete.append((pid, motivo))

    # Regra 3: blacklist manual
    for p in disapproved + limited:
        pid = p["product_id"]
        if pid not in seen and pid in id_set:
            seen.add(pid)
            to_delete.append((pid, "blacklist_manual"))

    if not to_delete:
        return []

    service = _build_service()
    product_index = {p["product_id"]: p for p in disapproved + limited}

    log = []
    for pid, motivo in to_delete:
        # Enfileira ANTES de deletar — garante registro mesmo se delete falhar
        if pid in product_index:
            p = product_index[pid]
            # Enriquece com motivo legível para a fila
            p["motivo_remocao"] = motivo
            _enqueue_reprovado(p)

        res = _delete_from_gmc(service, pid)
        log.append({"product_id": pid, "motivo": motivo, **res})
        time.sleep(0.3)

    return log


# ── Scan ──────────────────────────────────────────────────────────────────────

def _scan_gmc() -> dict:
    service = _build_service()
    disapproved: list = []
    limited: list = []
    page_token = None
    total = 0

    while True:
        kwargs: dict = {"merchantId": MERCHANT_ID, "maxResults": 250}
        if page_token:
            kwargs["pageToken"] = page_token

        for _attempt in range(3):
            try:
                response = service.productstatuses().list(**kwargs).execute()
                break
            except Exception as _e:
                if _attempt == 2:
                    raise
                time.sleep(2 ** _attempt)
        resources = response.get("resources", [])
        total    += len(resources)
        _scan["total"]  = total
        _scan["pagina"] = _scan.get("pagina", 0) + 1

        for status in resources:
            offer_id = status.get("productId", "")
            title    = status.get("title", "(sem título)")
            link     = status.get("link", "")
            issues   = status.get("itemLevelIssues", [])
            dest_st  = status.get("destinationStatuses", [])

            disapproved_issues = [i for i in issues if i.get("servability") == "disapproved"]
            # Captura: unaffected (limited) + demoted + vazio — qualquer issue não-disapproved
            # que ainda possa bloquear o Shopping via destinationStatuses
            nondisapproved_issues = [i for i in issues if i.get("servability") != "disapproved"]

            def _fmt(issue_list: list) -> list:
                return [
                    {
                        "description": i.get("description", ""),
                        "detail":      i.get("detail", ""),
                        "servability": i.get("servability", ""),
                        "resolution":  i.get("resolution", ""),
                        "attribute":   i.get("attributeName", ""),
                    }
                    for i in issue_list
                ]

            entry = {
                "product_id":   offer_id,
                "offer_id":     offer_id.split(":")[-1] if offer_id else "",
                "title":        title,
                "link":         link,
                "destinations": dest_st,
            }

            if disapproved_issues:
                entry["issues"]   = _fmt(disapproved_issues)
                entry["category"] = _classify_product(entry)
                entry["severity"] = "disapproved"
                disapproved.append(entry)

            # Inclui neste grupo: unaffected + demoted + sem servability
            # O critério real de remoção é o destinationStatuses (via _is_shopping_blocked)
            if nondisapproved_issues:
                e2 = dict(entry)
                e2["issues"]   = _fmt(nondisapproved_issues)
                e2["category"] = _classify_product(e2)
                e2["severity"] = "limited"
                limited.append(e2)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return {
        "total_scanned": total,
        "disapproved":   disapproved,
        "limited":       limited,
        "counts": {
            "disapproved": dict(Counter(p["category"] for p in disapproved)),
            "limited":     dict(Counter(p["category"] for p in limited)),
        },
        "scanned_at": datetime.utcnow().isoformat(),
    }


def _run_scan_bg():
    try:
        _scan.update({
            "rodando": True, "concluido": False, "erro": None,
            "total": 0, "pagina": 0,
            "iniciado_em": datetime.utcnow().isoformat(),
            "auto_delete_log": [],
        })
        resultado = _scan_gmc()

        # Auto-delete produtos adultos (e qualquer produto na blacklist)
        auto_log = _auto_delete_after_scan(resultado)

        _scan.update({
            "rodando": False, "concluido": True,
            "resultado": resultado,
            "auto_delete_log": auto_log,
            "concluido_em": datetime.utcnow().isoformat(),
        })
    except Exception as e:
        _scan.update({
            "rodando": False, "concluido": True, "erro": str(e),
            "concluido_em": datetime.utcnow().isoformat(),
        })


# ── Shopify fix ───────────────────────────────────────────────────────────────

def _shopify_exists(shopify_id: str) -> bool | None:
    """Return True if the product exists in Shopify, False if 404, None on error."""
    base = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}"
    try:
        r = requests.get(
            f"{base}/products/{shopify_id}.json",
            params={"fields": "id"},
            headers=_shopify_headers(),
            timeout=15,
        )
        if r.status_code == 200:
            return True
        if r.status_code == 404:
            return False
        return None
    except Exception:
        return None


def _fix_shopify(gmc_product_id: str, category: str, title: str) -> dict:
    """Apply the Shopify-side fix for the given category."""
    shopify_id = _shopify_id_from_gmc(gmc_product_id)
    if not shopify_id:
        return {"ok": False, "error": f"shopify_id_not_found (raw: {gmc_product_id!r})", "not_in_shopify": True}

    base = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}"
    h    = _shopify_headers()

    try:
        if category == "caps":
            title_fixed = title.strip().capitalize()
            if title_fixed == title:
                return {"ok": True, "action": "no_change"}
            r = requests.put(
                f"{base}/products/{shopify_id}.json",
                json={"product": {"id": shopify_id, "title": title_fixed}},
                headers=h, timeout=15,
            )
            if r.status_code == 200:
                return {"ok": True, "action": "title_updated"}
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}

        elif category == "gtin":
            # Clear invalid barcode so the feed stops sending the bad GTIN
            r = requests.get(
                f"{base}/products/{shopify_id}.json",
                params={"fields": "id,variants"},
                headers=h, timeout=15,
            )
            if r.status_code != 200:
                return {"ok": False, "error": f"GET HTTP {r.status_code}: {r.text[:200]}"}
            variants = r.json().get("product", {}).get("variants", [])
            cleared = 0
            for v in variants:
                if v.get("barcode"):
                    pu = requests.put(
                        f"{base}/variants/{v['id']}.json",
                        json={"variant": {"id": v["id"], "barcode": ""}},
                        headers=h, timeout=15,
                    )
                    if pu.status_code == 200:
                        cleared += 1
            return {"ok": True, "action": f"barcode_cleared_{cleared}_variants"}

        elif category == "adult_false":
            # Remove any adult-related tags from the product
            r = requests.get(
                f"{base}/products/{shopify_id}.json",
                params={"fields": "id,tags"},
                headers=h, timeout=15,
            )
            if r.status_code != 200:
                return {"ok": False, "error": f"GET HTTP {r.status_code}: {r.text[:200]}"}
            product = r.json().get("product", {})
            tags = [t.strip() for t in product.get("tags", "").split(",") if t.strip()]
            kw   = ["adult", "adulto", "sensual", "erótic", "intim", "sexy"]
            new_tags = [t for t in tags if not any(k in t.lower() for k in kw)]
            r2 = requests.put(
                f"{base}/products/{shopify_id}.json",
                json={"product": {"id": shopify_id, "tags": ", ".join(new_tags)}},
                headers=h, timeout=15,
            )
            if r2.status_code == 200:
                removed = len(tags) - len(new_tags)
                return {"ok": True, "action": f"adult_tags_removed_{removed}"}
            return {"ok": False, "error": f"PUT HTTP {r2.status_code}"}

        elif category in ("adult_legit", "recalled"):
            # Intentionally keep product in Shopify — only GMC is changed
            return {"ok": True, "action": "kept_in_shopify"}

        return {"ok": True, "action": "no_shopify_fix_for_category"}

    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── GMC fix ───────────────────────────────────────────────────────────────────

def _exclude_from_gmc(service, product_id: str) -> dict:
    """Exclude a product from all GMC shopping destinations (product not found in Shopify)."""
    try:
        product = service.products().get(
            merchantId=MERCHANT_ID, productId=product_id
        ).execute()
        product["excludedDestinations"] = ["Shopping ads", "Shopping"]
        service.products().update(
            merchantId=MERCHANT_ID, productId=product_id, body=_clean(product)
        ).execute()
        return {"ok": True, "action": "excluded_not_in_shopify"}
    except Exception as e:
        return {"ok": False, "action": "exclude_failed", "error": str(e)}


def _fix_gmc(service, product_id: str, category: str, title: str) -> dict:
    """Apply the GMC-side fix for the given category."""
    try:
        product = service.products().get(
            merchantId=MERCHANT_ID, productId=product_id
        ).execute()
    except Exception as e:
        return {"ok": False, "action": "get_failed", "error": str(e)}

    changed = False
    action  = "none"

    if category in ("adult_legit", "recalled"):
        product["excludedDestinations"] = ["Shopping ads", "Shopping"]
        changed = True
        action  = "excluded_from_shopping"

    elif category == "adult_false":
        product["adult"] = False
        t = title.lower()
        if "esmalte" in t or "nail" in t:
            product["googleProductCategory"] = "2975"
        elif "óleo" in t or "oleo" in t:
            product["googleProductCategory"] = "2975"
        elif "creme" in t and ("corpo" in t or "corporal" in t):
            product["googleProductCategory"] = "567"
        elif "kit" in t or "linha" in t:
            product["googleProductCategory"] = "2975"
        elif "shampoo" in t or "condicionador" in t:
            product["googleProductCategory"] = "2975"
        changed = True
        action  = "adult_false_set"

    elif category == "caps":
        title_fixed = title.strip().capitalize()
        if title_fixed != title:
            product["title"] = title_fixed
            changed = True
            action  = "title_fixed"

    elif category == "gtin":
        product["identifierExists"] = False
        changed = True
        action  = "identifier_exists_false"

    if changed:
        try:
            service.products().update(
                merchantId=MERCHANT_ID, productId=product_id, body=_clean(product)
            ).execute()
            return {"ok": True, "action": action}
        except Exception as e:
            return {"ok": False, "action": action, "error": str(e)}

    return {"ok": True, "action": "no_change_needed"}


# ── Background fix ────────────────────────────────────────────────────────────

def _run_fix_bg(product_ids_categories: list[tuple[str, str, str]]):
    service = _build_service()
    _fix.update({
        "rodando": True, "concluido": False, "erro": None,
        "total": len(product_ids_categories), "feitos": 0, "erros_count": 0,
        "log": [], "iniciado_em": datetime.utcnow().isoformat(),
    })

    for product_id, category, title in product_ids_categories:
        shopify_id = _shopify_id_from_gmc(product_id)
        exists     = _shopify_exists(shopify_id) if shopify_id else None

        if exists is False:
            # Product no longer exists in Shopify → exclude from GMC feed
            gmc_result     = _exclude_from_gmc(service, product_id)
            shopify_result = {"ok": True, "action": "not_in_shopify_excluded_from_gmc"}
        else:
            gmc_result     = _fix_gmc(service, product_id, category, title)
            shopify_result = _fix_shopify(product_id, category, title)

        _fix["feitos"] += 1
        if not gmc_result["ok"] or not shopify_result["ok"]:
            _fix["erros_count"] += 1

        _fix["log"].append({
            "product_id": product_id,
            "title":      title[:60],
            "category":   category,
            "gmc":        gmc_result,
            "shopify":    shopify_result,
            "ok":         gmc_result["ok"] and shopify_result["ok"],
        })
        time.sleep(0.4)

    _fix.update({
        "rodando": False, "concluido": True,
        "concluido_em": datetime.utcnow().isoformat(),
    })


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/diagnostico")
def diagnostico_gmc():
    """
    Diagnóstico completo alinhado ao GMC Next.

    Contagem POR PRODUTO ÚNICO (não por destino) — igual ao painel do GMC:
      - Aprovado   : qualquer destino Shopping aprovado para BR
      - Em análise : sem aprovação E sem issues bloqueantes (ex: atualização automática)
      - Reprovado  : sem aprovação E com issues bloqueantes (merchant_action, servability=disapproved)
      - Limitado   : aprovado mas com issues que reduzem alcance

    O GMC Next reclassificou como "Em análise" produtos que a Content API ainda
    retorna com status "disapproved" no destino mas cujos issues são do tipo
    "pending_processing" (Google em análise) ou "unaffected"/"demoted" (automáticos).
    """
    service = _build_service()
    problemas: list[dict] = []
    recomendacoes: list[str] = []

    # ── 1. Account-level status ───────────────────────────────────────────────
    conta_ok = True
    conta_issues: list[dict] = []
    conta_status_raw: dict = {}
    try:
        conta_status_raw = service.accountstatuses().get(
            merchantId=MERCHANT_ID,
            accountId=MERCHANT_ID,
        ).execute()
        for issue in conta_status_raw.get("accountLevelIssues", []):
            severity = issue.get("severity", "")
            conta_issues.append({
                "title":         issue.get("title", ""),
                "detail":        issue.get("detail", ""),
                "severity":      severity,
                "destination":   issue.get("destination", ""),
                "country":       issue.get("country", ""),
                "documentation": issue.get("documentation", ""),
            })
            if severity == "critical":
                conta_ok = False
                problemas.append({
                    "nivel": "conta", "gravidade": "critico",
                    "titulo": issue.get("title", ""),
                    "detalhe": issue.get("detail", ""),
                })
    except Exception as e:
        conta_issues.append({"title": f"Erro ao ler status da conta: {e}", "severity": "unknown"})

    # ── 2. Classificação por produto único (alinhada ao GMC Next) ────────────
    SHOPPING_DEST_KEYS = {"Shopping", "Shopping ads", "DisplayAds", "Display ads",
                          "SurfacesAcrossGoogle", "Surfaces across Google",
                          "Free listings", "Free local listings"}

    # Issues com servability "unaffected" ou "demoted" NÃO são bloqueantes.
    # Issues com resolution "pending_processing" = Google ainda está revisando.
    # Apenas "disapproved" + "merchant_action" são realmente bloqueantes.
    NON_BLOCKING_SERVABILITY = {"unaffected", "demoted"}

    n_aprovado   = 0  # aprovado em pelo menos um destino Shopping
    n_reprovado  = 0  # sem aprovação + issues bloqueantes (ação do lojista)
    n_em_analise = 0  # sem aprovação + apenas issues não-bloqueantes / pending
    n_limitado   = 0  # aprovado mas com issues que restringem alcance

    dest_stats: dict = {}
    sample_blocked: list[dict] = []   # reprovados verdadeiros (amostra)
    sample_em_analise: list[dict] = [] # em análise (amostra)
    issues_por_codigo: Counter = Counter()
    total_api = 0
    page_token = None

    while True:
        kwargs: dict = {"merchantId": MERCHANT_ID, "maxResults": 250}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.productstatuses().list(**kwargs).execute()
        for st in resp.get("resources", []):
            total_api += 1
            item_issues  = st.get("itemLevelIssues", [])
            dest_statuses = st.get("destinationStatuses", [])

            # Contagem por destino (para tabela)
            for d in dest_statuses:
                dest_name = d.get("destination", "Outro")
                s = d.get("status", "unknown")
                if dest_name not in dest_stats:
                    dest_stats[dest_name] = {"approved": 0, "disapproved": 0, "pending": 0, "excluded": 0, "unknown": 0}
                dest_stats[dest_name][s if s in dest_stats[dest_name] else "unknown"] += 1

            # ── Classificação por produto único ──────────────────────────────
            # 1) Aprovado em algum destino Shopping?
            aprovado_shopping = any(
                d.get("destination") in SHOPPING_DEST_KEYS
                and (d.get("status") == "approved"
                     or "BR" in d.get("approvedCountries", []))
                for d in dest_statuses
            )

            # 2) Issues bloqueantes = servability "disapproved" + resolution "merchant_action"
            issues_bloqueantes = [
                i for i in item_issues
                if i.get("servability") == "disapproved"
                and i.get("resolution") == "merchant_action"
            ]

            # 3) Issues limitantes = servability "demoted" + merchant_action
            issues_limitantes = [
                i for i in item_issues
                if i.get("servability") == "demoted"
                and i.get("resolution") == "merchant_action"
            ]

            # Conta por código de issue (para resumo)
            for iss in item_issues:
                code = iss.get("code") or iss.get("description", "")[:60]
                if code:
                    issues_por_codigo[code] += 1

            if aprovado_shopping:
                if issues_limitantes:
                    n_limitado += 1
                else:
                    n_aprovado += 1
            else:
                if issues_bloqueantes:
                    n_reprovado += 1
                    if len(sample_blocked) < 20:
                        sample_blocked.append({
                            "product_id": st.get("productId", ""),
                            "title":      st.get("title", "")[:80],
                            "issues": [
                                {"code": i.get("code",""), "desc": i.get("description",""),
                                 "serv": i.get("servability",""), "resolution": i.get("resolution","")}
                                for i in issues_bloqueantes[:3]
                            ],
                        })
                else:
                    n_em_analise += 1
                    if len(sample_em_analise) < 10:
                        sample_em_analise.append({
                            "product_id": st.get("productId", ""),
                            "title":      st.get("title", "")[:80],
                            "issues": [
                                {"code": i.get("code",""), "desc": i.get("description",""),
                                 "serv": i.get("servability",""), "resolution": i.get("resolution","")}
                                for i in item_issues[:3]
                            ],
                        })

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    total_produtos = n_aprovado + n_reprovado + n_em_analise + n_limitado

    # ── 3. Problemas e recomendações ──────────────────────────────────────────
    if n_aprovado == 0 and total_api > 0:
        problemas.append({
            "nivel": "destinos", "gravidade": "critico",
            "titulo": "Nenhum produto aprovado para Shopping",
            "detalhe": "Todos os produtos estão em análise ou reprovados. "
                       "Pode indicar que a conta não está vinculada ao Google Ads ou o feed não está configurado.",
        })

    if n_reprovado > 0:
        top_issues = [f"{code} ({cnt}x)" for code, cnt in issues_por_codigo.most_common(5)]
        problemas.append({
            "nivel": "produtos", "gravidade": "aviso",
            "titulo": f"{n_reprovado} produto(s) reprovado(s) com issues bloqueantes",
            "detalhe": "Issues mais comuns: " + ", ".join(top_issues),
        })

    if n_em_analise > 0 and n_aprovado > 0:
        recomendacoes.append(
            f"{n_em_analise} produtos estão 'Em análise' pelo Google — não requerem ação do lojista. "
            "Aguarde a revisão automática (pode levar dias após atualização do feed)."
        )

    if not problemas:
        recomendacoes.append(
            "O GMC está saudável. Se os anúncios não aparecem, verifique se há campanha "
            "Shopping ou Performance Max ativa com orçamento > 0 em https://ads.google.com."
        )

    if conta_issues and any(i.get("severity") == "critical" for i in conta_issues):
        recomendacoes.append(
            "CRÍTICO: Problema grave na conta Merchant Center. "
            "Acesse https://merchants.google.com e resolva os avisos."
        )

    # Cobertura Free Listings vs paid Shopping
    aprovados_free = sum(dest_stats.get(k, {}).get("approved", 0) for k in dest_stats if "Free" in k or "Surfaces" in k)
    aprovados_paid = sum(dest_stats.get(k, {}).get("approved", 0) for k in dest_stats if "Shopping ads" in k or k == "Shopping")
    tem_free       = any("Free" in k or "Surfaces" in k for k in dest_stats)
    tem_paid       = any("Shopping ads" in k or k == "Shopping" for k in dest_stats)

    return {
        "merchant_id":    MERCHANT_ID,
        "total_api":      total_api,       # produtos retornados pela API (com status)
        "total_produtos": total_produtos,  # soma das classificações
        "nota_total": (
            "O GMC mostra um total maior pois inclui produtos ainda sendo "
            "sincronizados com o feed (sem status na API)."
        ),
        # Contagem por produto único — alinhada ao GMC Next
        "produtos": {
            "aprovado":   n_aprovado,
            "limitado":   n_limitado,
            "reprovado":  n_reprovado,
            "em_analise": n_em_analise,
        },
        "conta": {
            "ok":              conta_ok,
            "issues":          conta_issues,
            "website_claimed": conta_status_raw.get("websiteClaimed", None),
        },
        "destinos": {
            "stats":                   dest_stats,
            "aprovados_free_listings": aprovados_free,
            "aprovados_paid_shopping": aprovados_paid,
            "tem_free_listings":       tem_free,
            "tem_shopping_ads_dest":   tem_paid,
        },
        "top_issues":          [{"code": c, "count": n} for c, n in issues_por_codigo.most_common(15)],
        "amostra_bloqueados":  sample_blocked,
        "amostra_em_analise":  sample_em_analise,
        "problemas":           problemas,
        "recomendacoes":       recomendacoes,
        "diagnosticado_em":    datetime.utcnow().isoformat(),
    }


@router.get("/analise-shopping")
def analise_shopping_disapproved():
    """
    Analisa especificamente os produtos com Shopping disapproved.
    Retorna os issues mais comuns e amostras dos produtos afetados.
    """
    from collections import Counter
    from datetime import datetime

    service = _build_service()

    issues_counter: Counter = Counter()
    issues_detail: dict = {}   # code → {description, servability, resolution, count}
    amostra: list = []
    total_shopping_disapproved = 0
    total_shopping_approved    = 0
    total_sem_issues           = 0
    page_token = None

    while True:
        kwargs = {"merchantId": MERCHANT_ID, "maxResults": 250}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.productstatuses().list(**kwargs).execute()

        for st in resp.get("resources", []):
            dest_statuses = st.get("destinationStatuses", [])
            item_issues   = st.get("itemLevelIssues", [])

            # Verifica status real do Shopping (só status == "approved")
            shopping_status = None
            for d in dest_statuses:
                if d.get("destination") == "Shopping":
                    shopping_status = d.get("status")
                    break

            if shopping_status == "approved":
                total_shopping_approved += 1
                continue
            if shopping_status != "disapproved":
                continue  # pending, excluded, ou sem Shopping

            total_shopping_disapproved += 1

            # Captura amostra independente de ter issues
            if len(amostra) < 30:
                amostra.append({
                    "product_id": st.get("productId", ""),
                    "title": st.get("title", "")[:80],
                    "item_issues": [
                        {
                            "code": i.get("code", ""),
                            "description": i.get("description", ""),
                            "servability": i.get("servability", ""),
                            "resolution": i.get("resolution", ""),
                            "attribute": i.get("attribute", ""),
                        }
                        for i in item_issues
                    ],
                    "destination_statuses": [
                        {
                            "destination": d.get("destination", ""),
                            "status": d.get("status", ""),
                            "approvedCountries": d.get("approvedCountries", []),
                            "disapprovedCountries": d.get("disapprovedCountries", []),
                            "pendingCountries": d.get("pendingCountries", []),
                        }
                        for d in dest_statuses
                    ],
                })

            if not item_issues:
                total_sem_issues += 1
                continue

            for iss in item_issues:
                code = iss.get("code") or iss.get("description", "")[:80]
                desc = iss.get("description", "")
                serv = iss.get("servability", "")
                reso = iss.get("resolution", "")
                attr = iss.get("attribute", "")
                if code:
                    issues_counter[code] += 1
                    if code not in issues_detail:
                        issues_detail[code] = {
                            "code": code,
                            "description": desc,
                            "servability": serv,
                            "resolution": reso,
                            "attribute": attr,
                            "count": 0,
                        }
                    issues_detail[code]["count"] += 1

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    top_issues = sorted(issues_detail.values(), key=lambda x: -x["count"])

    return {
        "total_shopping_disapproved": total_shopping_disapproved,
        "total_shopping_approved":    total_shopping_approved,
        "total_sem_issues":           total_sem_issues,
        "top_issues":                 top_issues[:25],
        "amostra_produtos":           amostra,
        "analisado_em":               datetime.utcnow().isoformat(),
    }


@router.get("/ads-link-status")
def gmc_ads_link_status():
    """
    Verifica o status atual do vínculo entre o Merchant Center e o Google Ads.
    Usa o endpoint accounts.link da Content API v2.1.
    """
    service = _build_service()
    try:
        result = service.accounts().get(
            merchantId=MERCHANT_ID,
            accountId=MERCHANT_ID,
        ).execute()
        ads_links = result.get("adsLinks", [])
        return {
            "merchant_id": MERCHANT_ID,
            "ads_links": ads_links,
            "total_vinculados": len(ads_links),
            "verificado_em": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        return {"ok": False, "erro": str(e)}


@router.post("/vincular-ads")
def gmc_vincular_ads(body: dict = None):
    """
    Vincula o Google Ads ao Merchant Center via Content API.
    Envia uma solicitação de vínculo do GMC para o Ads.
    body: { "ads_customer_id": "2097362078" }  (opcional, usa o do google-ads.yaml por padrão)
    """
    from datetime import datetime

    # Customer ID padrão do google-ads.yaml
    ads_id_default = "2097362078"
    ads_customer_id = str((body or {}).get("ads_customer_id", ads_id_default)).replace("-", "")

    service = _build_service()

    link_body = {
        "action": "request",
        "linkType": "adsApp",
        "linkedAccountId": ads_customer_id,
    }

    try:
        result = service.accounts().link(
            merchantId=MERCHANT_ID,
            accountId=MERCHANT_ID,
            body=link_body,
        ).execute()
        return {
            "ok": True,
            "ads_customer_id": ads_customer_id,
            "merchant_id": MERCHANT_ID,
            "resultado": result,
            "msg": f"Solicitação de vínculo enviada ao Google Ads {ads_customer_id}. "
                   "Se as duas contas pertencem ao mesmo usuário, o vínculo é aceito automaticamente.",
            "vinculado_em": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        err = str(e)
        # Se já estiver vinculado, retorna como sucesso
        if "already" in err.lower() or "linked" in err.lower():
            return {"ok": True, "msg": "Contas já estão vinculadas.", "detalhe": err}
        return {"ok": False, "erro": err}


@router.get("/status")
def gmc_status():
    resultado  = _scan.get("resultado") or {}
    counts_d   = resultado.get("counts", {}).get("disapproved", {})
    counts_l   = resultado.get("counts", {}).get("limited", {})
    total_d    = len(resultado.get("disapproved", []))
    total_l    = len(resultado.get("limited", []))
    auto_log   = _scan.get("auto_delete_log", [])

    return {
        "scan": {
            "rodando":      _scan["rodando"],
            "concluido":    _scan["concluido"],
            "erro":         _scan["erro"],
            "total":        _scan["total"],
            "pagina":       _scan["pagina"],
            "iniciado_em":  _scan["iniciado_em"],
            "concluido_em": _scan["concluido_em"],
        },
        "fix": {
            "rodando":      _fix["rodando"],
            "concluido":    _fix["concluido"],
            "total":        _fix["total"],
            "feitos":       _fix["feitos"],
            "erros_count":  _fix["erros_count"],
        },
        "auto_delete": {
            "total":    len(auto_log),
            "ok":       sum(1 for x in auto_log if x.get("ok")),
            "erros":    sum(1 for x in auto_log if not x.get("ok")),
            "log":      auto_log,
        },
        "resumo": {
            "total_scanned":      resultado.get("total_scanned", 0),
            "total_reprovados":   total_d,
            "total_limitados":    total_l,
            "scanned_at":         resultado.get("scanned_at"),
            "counts_disapproved": counts_d,
            "counts_limited":     counts_l,
        },
    }


@router.post("/scan")
def iniciar_scan(background_tasks: BackgroundTasks):
    if _scan["rodando"]:
        raise HTTPException(status_code=409, detail="Scan já em andamento")
    background_tasks.add_task(_run_scan_bg)
    return {"ok": True, "message": "Scan iniciado"}


@router.get("/produtos")
def listar_produtos(severity: Optional[str] = None, category: Optional[str] = None):
    resultado = _scan.get("resultado")
    if not resultado:
        raise HTTPException(
            status_code=404,
            detail="Nenhum scan realizado. Execute /gmc/scan primeiro.",
        )

    all_products = resultado.get("disapproved", []) + resultado.get("limited", [])
    if severity:
        all_products = [p for p in all_products if p.get("severity") == severity]
    if category:
        all_products = [p for p in all_products if p.get("category") == category]

    return {"total": len(all_products), "produtos": all_products}


@router.post("/corrigir")
def corrigir_produtos(
    background_tasks: BackgroundTasks,
    categorias: list[str] | None = None,
):
    """
    Fix products in both GMC and Shopify.
    categorias: ["adult_legit","adult_false","caps","gtin"] or None for all auto-fixable.
    """
    if _fix["rodando"]:
        raise HTTPException(status_code=409, detail="Correção já em andamento")

    resultado = _scan.get("resultado")
    if not resultado:
        raise HTTPException(status_code=404, detail="Execute /gmc/scan primeiro.")

    AUTO_FIX = {"adult_legit", "adult_false", "caps", "gtin", "recalled"}
    cats = set(categorias) if categorias else AUTO_FIX

    all_products = resultado.get("disapproved", []) + resultado.get("limited", [])
    seen: set = set()
    to_fix: list = []
    for p in all_products:
        cat = p.get("category", "other")
        if cat not in cats:
            continue
        key = (p["product_id"], cat)
        if key in seen:
            continue
        seen.add(key)
        to_fix.append((p["product_id"], cat, p.get("title", "")))

    if not to_fix:
        return {
            "ok": True,
            "message": "Nenhum produto para corrigir nas categorias selecionadas",
            "total": 0,
        }

    background_tasks.add_task(_run_fix_bg, to_fix)
    return {
        "ok": True,
        "message": f"Correção iniciada para {len(to_fix)} produto(s) (GMC + Shopify)",
        "total": len(to_fix),
    }


@router.get("/fix/log")
def fix_log():
    return {"fix": _fix, "log": _fix.get("log", [])}


# ── Blacklist endpoints ───────────────────────────────────────────────────────

@router.get("/blacklist")
def get_blacklist():
    """Retorna a blacklist atual: categorias e IDs específicos banidos do GMC."""
    bl = _load_blacklist()
    return {
        "categories": bl.get("categories", list(AUTO_DELETE_CATEGORIES)),
        "product_ids": bl.get("product_ids", []),
        "total_ids": len(bl.get("product_ids", [])),
    }


@router.post("/blacklist/produto")
def add_product_to_blacklist(payload: dict):
    """
    Adiciona um product_id específico à blacklist.
    Body: {"product_id": "online:pt:BRL_..."}
    """
    pid = (payload.get("product_id") or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="product_id obrigatório")

    bl = _load_blacklist()
    ids = bl.setdefault("product_ids", [])
    if pid not in ids:
        ids.append(pid)
        _save_blacklist(bl)
        # Deleta imediatamente do GMC
        try:
            service = _build_service()
            res = _delete_from_gmc(service, pid)
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        return {"ok": True, "message": "Produto adicionado à blacklist e excluído do GMC", "delete_result": res}
    return {"ok": True, "message": "Produto já estava na blacklist"}


@router.delete("/blacklist/produto")
def remove_product_from_blacklist(payload: dict):
    """Remove um product_id específico da blacklist."""
    pid = (payload.get("product_id") or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="product_id obrigatório")
    bl = _load_blacklist()
    ids = bl.get("product_ids", [])
    if pid in ids:
        ids.remove(pid)
        _save_blacklist(bl)
        return {"ok": True, "message": "Produto removido da blacklist"}
    return {"ok": True, "message": "Produto não estava na blacklist"}


@router.post("/blacklist/categoria")
def update_blacklist_categories(payload: dict):
    """
    Atualiza as categorias de auto-delete.
    Body: {"categories": ["adult_legit", "adult_false", "recalled"]}
    """
    cats = payload.get("categories")
    if not isinstance(cats, list):
        raise HTTPException(status_code=400, detail="'categories' deve ser uma lista")
    bl = _load_blacklist()
    bl["categories"] = cats
    _save_blacklist(bl)
    return {"ok": True, "categories": cats}


# ── Fila de correção — endpoints ─────────────────────────────────────────────

@router.get("/saude")
def saude_gmc():
    """
    Retorna métricas de saúde da conta GMC:
    último scan, próximo scan, produtos escaneados, removidos, taxa de aprovação.
    """
    scan_st = _load_scan_status()
    fila    = _load_fila()
    items   = fila.get("items", [])

    aguard  = sum(1 for i in items if i.get("status") == "aguardando")
    corrig  = sum(1 for i in items if i.get("status") == "corrigido")
    ignorad = sum(1 for i in items if i.get("status") == "ignorado")

    # Calcula segundos até o próximo scan
    segundos_desde_scan = None
    proxim_scan_em_s    = None
    if scan_st.get("last_scan_at"):
        try:
            last = datetime.fromisoformat(scan_st["last_scan_at"])
            diff = (datetime.utcnow() - last).total_seconds()
            segundos_desde_scan = int(diff)
            intervalo = scan_st.get("interval_s", 900)
            proxim_scan_em_s = max(0, int(intervalo - diff))
        except Exception:
            pass

    total_scanned = scan_st.get("total_scanned", 0)
    blocked       = scan_st.get("blocked_detected", 0)
    removed_ok    = scan_st.get("removed_ok", 0)

    # Taxa de aprovação estimada (produtos ok / total escaneados)
    taxa_aprovacao = None
    if total_scanned > 0:
        taxa_aprovacao = round((total_scanned - blocked) / total_scanned * 100, 1)

    return {
        "last_scan_at":        scan_st.get("last_scan_at"),
        "segundos_desde_scan": segundos_desde_scan,
        "proximo_scan_em_s":   proxim_scan_em_s,
        "interval_s":          scan_st.get("interval_s", 900),
        "total_scanned":       total_scanned,
        "blocked_detected":    blocked,
        "removed_ok":          removed_ok,
        "removed_fail":        scan_st.get("removed_fail", 0),
        "taxa_aprovacao":      taxa_aprovacao,
        "fila": {
            "aguardando": aguard,
            "corrigido":  corrig,
            "ignorado":   ignorad,
            "total":      len(items),
        },
        "status_geral": (
            "limpo"       if aguard == 0 else
            "atencao"     if aguard <= 5 else
            "critico"
        ),
    }


@router.get("/fila")
def get_fila(status: Optional[str] = None):
    """Lista a fila de produtos reprovados aguardando correção."""
    fila  = _load_fila()
    items = fila.get("items", [])
    if status:
        items = [i for i in items if i.get("status") == status]
    # Estatísticas
    todos    = fila.get("items", [])
    aguard   = sum(1 for i in todos if i.get("status") == "aguardando")
    corrig   = sum(1 for i in todos if i.get("status") == "corrigido")
    ignorad  = sum(1 for i in todos if i.get("status") == "ignorado")
    return {
        "total": len(items),
        "stats": {"aguardando": aguard, "corrigido": corrig, "ignorado": ignorad},
        "items": sorted(items, key=lambda x: x.get("removed_at", ""), reverse=True),
    }


@router.post("/fila/{item_id}/corrigido")
def marcar_corrigido(item_id: str, payload: dict = {}):
    """Marca item como corrigido. Se _apenas_notas=true, só salva notas sem mudar status."""
    fila  = _load_fila()
    item  = next((i for i in fila["items"] if i["id"] == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado na fila")
    if payload.get("notas") is not None:
        item["notas"] = payload["notas"]
    if not payload.get("_apenas_notas"):
        item["status"]       = "corrigido"
        item["corrigido_em"] = datetime.utcnow().isoformat()
    item["updated_at"] = datetime.utcnow().isoformat()
    _save_fila(fila)
    return {"ok": True, "message": "Marcado como corrigido"}


@router.post("/fila/{item_id}/ignorar")
def ignorar_item(item_id: str, payload: dict = {}):
    """Ignora o item (produto não será re-submetido ao GMC)."""
    fila  = _load_fila()
    item  = next((i for i in fila["items"] if i["id"] == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado na fila")
    item["status"]     = "ignorado"
    item["notas"]      = payload.get("notas", item.get("notas", ""))
    item["updated_at"] = datetime.utcnow().isoformat()
    _save_fila(fila)
    return {"ok": True, "message": "Item ignorado"}


@router.post("/fila/{item_id}/reabrir")
def reabrir_item(item_id: str):
    """Reabre um item corrigido/ignorado de volta para 'aguardando'."""
    fila  = _load_fila()
    item  = next((i for i in fila["items"] if i["id"] == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado na fila")
    item["status"]     = "aguardando"
    item["updated_at"] = datetime.utcnow().isoformat()
    _save_fila(fila)
    return {"ok": True, "message": "Item reaberto"}


@router.post("/fila/{item_id}/verificar")
def verificar_no_gmc(item_id: str):
    """
    Verifica se o produto foi aprovado no GMC após a correção.
    Se aprovado, marca como resolvido automaticamente.
    """
    fila  = _load_fila()
    item  = next((i for i in fila["items"] if i["id"] == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado na fila")

    pid = item["product_id"]
    try:
        service = _build_service()
        status_resp = service.productstatuses().get(
            merchantId=MERCHANT_ID, productId=pid
        ).execute()

        issues   = status_resp.get("itemLevelIssues", [])
        dest     = status_resp.get("destinationStatuses", [])
        reprov   = any(i.get("servability") == "disapproved" for i in issues)
        approved = not reprov and any(
            d.get("status") == "approved" for d in dest
        )

        if approved:
            item["status"]       = "corrigido"
            item["corrigido_em"] = datetime.utcnow().isoformat()
            item["updated_at"]   = datetime.utcnow().isoformat()
            item["notas"]        = (item.get("notas") or "") + " [Aprovado automaticamente pelo GMC]"
            _save_fila(fila)
            return {"ok": True, "aprovado": True, "message": "Produto aprovado no GMC! Marcado como corrigido."}
        elif reprov:
            # Ainda reprovado — atualiza motivos
            item["issues"]     = [{"description": i.get("description",""), "detail": i.get("detail",""),
                                    "resolution": i.get("resolution",""), "attribute": i.get("attributeName","")}
                                   for i in issues if i.get("servability") == "disapproved"]
            item["updated_at"] = datetime.utcnow().isoformat()
            _save_fila(fila)
            return {"ok": True, "aprovado": False, "message": "Produto ainda reprovado no GMC.", "issues": item["issues"]}
        else:
            return {"ok": True, "aprovado": None, "message": "Produto em análise ou não encontrado no GMC."}
    except Exception as e:
        return {"ok": False, "aprovado": None, "message": f"Erro ao verificar: {e}"}


@router.delete("/fila/{item_id}")
def remover_da_fila(item_id: str):
    """Remove permanentemente um item da fila."""
    fila  = _load_fila()
    antes = len(fila.get("items", []))
    fila["items"] = [i for i in fila.get("items", []) if i["id"] != item_id]
    if len(fila["items"]) == antes:
        raise HTTPException(status_code=404, detail="Item não encontrado")
    _save_fila(fila)
    return {"ok": True, "message": "Item removido da fila"}


@router.post("/fila/limpar-resolvidos")
def limpar_resolvidos():
    """Remove da fila todos os itens com status 'corrigido' ou 'ignorado'."""
    fila  = _load_fila()
    antes = len(fila.get("items", []))
    fila["items"] = [i for i in fila.get("items", []) if i.get("status") == "aguardando"]
    removidos = antes - len(fila["items"])
    _save_fila(fila)
    return {"ok": True, "removidos": removidos}


@router.get("/fila/page", response_class=HTMLResponse)
def gmc_fila_page():
    html_file = BASE_DIR / "pages" / "gmc_fila.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="gmc_fila.html não encontrado")


@router.post("/configurar-checkout-conversao")
def configurar_checkout_conversao():
    """
    Configura o script de conversão do Google Ads na página de status do pedido (checkout).
    Requer scope write_checkouts no token Shopify.
    """
    CHECKOUT_SCRIPT = """{% if first_time_accessed %}
<script async src="https://www.googletagmanager.com/gtag/js?id=AW-2097362078"></script>
<script>
window.dataLayer = window.dataLayer || [];
function gtag(){dataLayer.push(arguments);}
gtag('js', new Date());
gtag('config', 'AW-2097362078');
gtag('event', 'conversion', {
  send_to: 'AW-2097362078/7250153929',
  value: {{ checkout.total_price | money_without_currency | replace: ',', '.' }},
  currency: 'BRL',
  transaction_id: '{{ checkout.order_id }}'
});
</script>
{% endif %}"""

    shop = "pknw4n-eg.myshopify.com"
    headers = {**_shopify_headers(), "Content-Type": "application/json"}

    r = requests.put(
        f"https://{shop}/admin/api/2024-01/shop.json",
        headers=headers,
        json={"shop": {"order_status_page_additional_scripts": CHECKOUT_SCRIPT}},
        timeout=15,
    )
    if r.status_code in (200, 201):
        return {"ok": True, "message": "Script de conversão configurado na página de checkout"}
    raise HTTPException(status_code=r.status_code, detail=r.text[:300])


@router.post("/frete/configurar")
def gmc_configurar_frete():
    """Configura política de frete no GMC via Content API v2.1:
    - Frete grátis para pedidos >= R$29,90 (Brasil inteiro)
    - Taxa fixa R$12,90 para SP (RMSP)
    - Frete grátis sem mínimo para micro-região do depósito (SP)
    """
    try:
        svc = _build_service()
        shipping_settings = {
            "accountId": str(MERCHANT_ID),
            "services": [
                {
                    "name": "Frete Grátis acima de R$29,90",
                    "active": True,
                    "shipSpeed": "medium",
                    "currency": "BRL",
                    "deliveryCountry": "BR",
                    "deliveryTime": {
                        "minTransitTimeInDays": 3,
                        "maxTransitTimeInDays": 10,
                    },
                    "rateGroups": [
                        {
                            "mainTable": {
                                "rowHeaders": {
                                    "prices": [
                                        {"value": "29.90", "currency": "BRL"},
                                        {"value": "infinity", "currency": "BRL"},
                                    ]
                                },
                                "rows": [
                                    {
                                        "cells": [
                                            {"flatRate": {"value": "12.90", "currency": "BRL"}}
                                        ]
                                    },
                                    {
                                        "cells": [
                                            {"flatRate": {"value": "0", "currency": "BRL"}}
                                        ]
                                    },
                                ],
                            },
                            "name": "Frete padrão Brasil",
                        }
                    ],
                },
                {
                    "name": "RMSP - Taxa Fixa R$12,90",
                    "active": True,
                    "shipSpeed": "medium",
                    "currency": "BRL",
                    "deliveryCountry": "BR",
                    "deliveryTime": {
                        "minTransitTimeInDays": 1,
                        "maxTransitTimeInDays": 3,
                    },
                    "deliveryRegion": "São Paulo, SP",
                    "rateGroups": [
                        {
                            "singleValue": {
                                "flatRate": {"value": "12.90", "currency": "BRL"}
                            },
                            "name": "RMSP fixo",
                        }
                    ],
                },
            ],
        }
        result = (
            svc.shippingsettings()
            .update(
                merchantId=MERCHANT_ID,
                accountId=MERCHANT_ID,
                body=shipping_settings,
            )
            .execute()
        )
        return {
            "ok": True,
            "msg": "Configurações de frete aplicadas no GMC",
            "servicos": [s["name"] for s in result.get("services", [])],
        }
    except Exception as e:
        return {"ok": False, "erro": str(e)}


@router.get("/frete/status")
def gmc_frete_status():
    """Retorna configuração atual de frete do GMC."""
    try:
        svc = _build_service()
        result = (
            svc.shippingsettings()
            .get(merchantId=MERCHANT_ID, accountId=MERCHANT_ID)
            .execute()
        )
        servicos = result.get("services", [])
        return {
            "ok": True,
            "total_servicos": len(servicos),
            "servicos": [
                {
                    "nome": s.get("name"),
                    "ativo": s.get("active"),
                    "pais": s.get("deliveryCountry"),
                    "moeda": s.get("currency"),
                }
                for s in servicos
            ],
        }
    except Exception as e:
        return {"ok": False, "erro": str(e)}


@router.get("/", response_class=HTMLResponse)
def gmc_page():
    html_file = BASE_DIR / "gmc.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="gmc.html não encontrado")
