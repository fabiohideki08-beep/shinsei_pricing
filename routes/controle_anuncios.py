# -*- coding: utf-8 -*-
"""
routes/controle_anuncios.py — Controle central de anúncios clonados Shinsei → AKG ML

Cada cópia bem-sucedida registra uma entrada em data/akg_catalog_control.json.
Este módulo serve a UI de gestão e expõe a API de leitura/atualização do catálogo.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests as _req
from fastapi import APIRouter, BackgroundTasks, Query
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/controle-anuncios")

BASE_DIR      = Path(__file__).resolve().parent.parent
PAGES_DIR     = BASE_DIR / "pages"
CATALOG_FILE  = BASE_DIR / "data" / "akg_catalog_control.json"
ML_API        = "https://api.mercadolibre.com"

_refresh_state: dict = {"rodando": False, "progresso": 0, "total": 0, "erro": None}


# ─── Helpers de persistência ─────────────────────────────────────────────────

def load_catalog() -> dict:
    try:
        if CATALOG_FILE.exists():
            return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"meta": {}, "campanhas": {}, "itens": []}


def save_catalog(catalog: dict) -> None:
    CATALOG_FILE.parent.mkdir(exist_ok=True)
    CATALOG_FILE.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")


def register_copy(
    id_shinsei: str,
    id_akg: str,
    titulo: str,
    campanha: str,
    campanha_nome: str,
    sku: str = "",
    preco: float | None = None,
    categoria: str = "",
    fotos_shinsei: int = 0,
) -> None:
    """Registra (ou atualiza) uma entrada no catálogo central após cópia bem-sucedida."""
    catalog = load_catalog()
    if "itens" not in catalog:
        catalog["itens"] = []
    if "campanhas" not in catalog:
        catalog["campanhas"] = {}

    # Garante que a campanha está registrada
    if campanha not in catalog["campanhas"]:
        catalog["campanhas"][campanha] = {"nome": campanha_nome, "criada_em": datetime.utcnow().isoformat()}

    # Atualiza entrada existente ou insere nova
    existente = next((x for x in catalog["itens"] if x.get("id_shinsei") == id_shinsei), None)
    if existente:
        existente.update({"id_akg": id_akg, "status_akg": "active", "copiado": True, "erro": "", "copiado_em": datetime.utcnow().isoformat()})
    else:
        catalog["itens"].append({
            "campanha": campanha,
            "id_shinsei": id_shinsei,
            "id_akg": id_akg,
            "titulo": titulo,
            "sku": sku,
            "preco": preco,
            "categoria": categoria,
            "fotos_shinsei": fotos_shinsei,
            "fotos_akg": 0,
            "status_shinsei": "active",
            "status_akg": "active",
            "copiado": True,
            "erro": "",
            "copiado_em": datetime.utcnow().isoformat(),
        })

    catalog["meta"]["ultima_atualizacao"] = datetime.utcnow().isoformat()
    catalog["meta"]["total_copiados"] = sum(1 for x in catalog["itens"] if x.get("copiado"))
    save_catalog(catalog)


# ─── Refresh de status via API ML ────────────────────────────────────────────

def _token_shinsei() -> str:
    from services.mercado_livre import obter_token_ml
    return obter_token_ml()


def _token_akg() -> str:
    import importlib, sys
    routes_ml = sys.modules.get("routes.mercado_livre") or importlib.import_module("routes.mercado_livre")
    svc = routes_ml._get_akg_oauth()
    tok = svc.get("access_token") or svc.get("token")
    if not tok:
        raise RuntimeError("Token AKG indisponível")
    return tok


def _refresh_bg(filtro_campanha: str | None):
    global _refresh_state
    try:
        _refresh_state.update({"rodando": True, "progresso": 0, "erro": None})
        catalog = load_catalog()
        itens = catalog.get("itens", [])
        if filtro_campanha:
            itens = [x for x in itens if x.get("campanha") == filtro_campanha]

        tok_s = _token_shinsei()
        tok_a = _token_akg()
        total = len(itens)
        _refresh_state["total"] = total

        for i in range(0, total, 20):
            batch = itens[i:i+20]
            ids_s = [x["id_shinsei"] for x in batch if x.get("id_shinsei")]
            ids_a = [x["id_akg"] for x in batch if x.get("id_akg")]

            det_s: dict[str, Any] = {}
            if ids_s:
                r = _req.get(f"{ML_API}/items", params={"ids": ",".join(ids_s), "attributes": "id,status,price,pictures"}, headers={"Authorization": f"Bearer {tok_s}"}, timeout=15)
                for entry in r.json():
                    d = entry.get("body") or entry
                    if d.get("id"):
                        det_s[d["id"]] = d

            det_a: dict[str, Any] = {}
            if ids_a:
                r2 = _req.get(f"{ML_API}/items", params={"ids": ",".join(ids_a), "attributes": "id,status,price,pictures"}, headers={"Authorization": f"Bearer {tok_a}"}, timeout=15)
                for entry in r2.json():
                    d = entry.get("body") or entry
                    if d.get("id"):
                        det_a[d["id"]] = d

            for item in batch:
                s = det_s.get(item.get("id_shinsei"), {})
                a = det_a.get(item.get("id_akg"), {})
                if s:
                    item["status_shinsei"] = s.get("status", item.get("status_shinsei"))
                    item["preco"] = s.get("price", item.get("preco"))
                    item["fotos_shinsei"] = len(s.get("pictures") or [])
                if a:
                    item["status_akg"] = a.get("status", item.get("status_akg"))
                    item["fotos_akg"] = len(a.get("pictures") or [])
                elif item.get("id_akg"):
                    item["status_akg"] = "not_found"

            _refresh_state["progresso"] = min(i + 20, total)
            time.sleep(0.3)

        catalog["meta"]["ultima_atualizacao"] = datetime.utcnow().isoformat()
        save_catalog(catalog)
        _refresh_state.update({"rodando": False, "progresso": total})
    except Exception as e:
        logger.exception("Erro no refresh do catálogo")
        _refresh_state.update({"rodando": False, "erro": str(e)})


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def page_controle():
    html = (PAGES_DIR / "controle_anuncios.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@router.get("/data")
async def api_data(campanha: str | None = Query(None)):
    catalog = load_catalog()
    itens = catalog.get("itens", [])
    if campanha:
        itens = [x for x in itens if x.get("campanha") == campanha]

    campanhas = catalog.get("campanhas", {})
    copiados = sum(1 for x in itens if x.get("copiado"))
    pendentes = sum(1 for x in itens if not x.get("copiado"))
    erros = sum(1 for x in itens if x.get("erro"))
    inativos_akg = sum(1 for x in itens if x.get("status_akg") not in ("active", "paused", "") and x.get("id_akg"))

    return {
        "meta": {**catalog.get("meta", {}), "total": len(itens), "copiados": copiados, "pendentes": pendentes, "erros": erros, "inativos_akg": inativos_akg},
        "campanhas": campanhas,
        "itens": sorted(itens, key=lambda x: (not x.get("copiado"), x.get("titulo", ""))),
        "refresh": _refresh_state,
    }


@router.post("/refresh")
async def api_refresh(bg: BackgroundTasks, campanha: str | None = Query(None)):
    if _refresh_state.get("rodando"):
        return JSONResponse({"ok": False, "msg": "Refresh já em andamento"})
    bg.add_task(_refresh_bg, campanha)
    return {"ok": True, "msg": "Refresh iniciado"}


@router.get("/refresh/status")
async def api_refresh_status():
    return _refresh_state
