# -*- coding: utf-8 -*-
"""
GMC (Google Merchant Center) monitoring and correction module.
Corrections are applied to both GMC (Content API) and Shopify (Admin API).
"""
from __future__ import annotations

import json
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
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(
        str(SA_FILE), scopes=SCOPES
    )
    return build("content", "v2.1", credentials=creds)


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


def _delete_from_gmc(service, product_id: str) -> dict:
    """Exclui permanentemente um produto do GMC."""
    try:
        service.products().delete(merchantId=MERCHANT_ID, productId=product_id).execute()
        return {"ok": True, "action": "deleted_permanently"}
    except Exception as e:
        return {"ok": False, "action": "delete_failed", "error": str(e)}


def _auto_delete_after_scan(resultado: dict) -> list[dict]:
    """
    Após o scan, exclui automaticamente do GMC:
      - Produtos em AUTO_DELETE_CATEGORIES (ou categorias configuradas na blacklist)
      - Produtos cujo product_id está na blacklist de IDs específicos
    Retorna log das exclusões realizadas.
    """
    bl = _load_blacklist()
    cat_set = set(bl.get("categories", [])) | AUTO_DELETE_CATEGORIES
    id_set  = set(bl.get("product_ids", []))

    all_products = resultado.get("disapproved", []) + resultado.get("limited", [])

    # Deduplica por product_id (mesmo produto pode aparecer em disapproved e limited)
    seen: set[str] = set()
    to_delete: list[tuple[str, str]] = []  # (product_id, motivo)
    for p in all_products:
        pid = p["product_id"]
        if pid in seen:
            continue
        cat = p.get("category", "other")
        if cat in cat_set:
            seen.add(pid)
            to_delete.append((pid, f"categoria:{cat}"))
        elif pid in id_set:
            seen.add(pid)
            to_delete.append((pid, "blacklist_id"))

    if not to_delete:
        return []

    service = _build_service()
    log = []
    for pid, motivo in to_delete:
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

        response  = service.productstatuses().list(**kwargs).execute()
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
            limited_issues     = [i for i in issues if i.get("servability") == "unaffected"]

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

            if limited_issues:
                e2 = dict(entry)
                e2["issues"]   = _fmt(limited_issues)
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


@router.get("/", response_class=HTMLResponse)
def gmc_page():
    html_file = BASE_DIR / "gmc.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="gmc.html não encontrado")
