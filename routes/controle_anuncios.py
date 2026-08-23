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
    result = svc.ler_tokens()
    tok = (result.get("data") or {}).get("access_token") or ""
    if not tok:
        raise RuntimeError("Token AKG indisponível — faça login em /ml/login2")
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

_GTIN_PENDENTE_FILE = BASE_DIR / "data" / "akg_gtin_pendente.json"


def _load_gtin_pendente() -> dict[str, str]:
    try:
        if _GTIN_PENDENTE_FILE.exists():
            return json.loads(_GTIN_PENDENTE_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _save_gtin_pendente(data: dict) -> None:
    _GTIN_PENDENTE_FILE.parent.mkdir(exist_ok=True)
    _GTIN_PENDENTE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@router.get("/gtin-pendente")
async def api_gtin_pendente():
    return _load_gtin_pendente()


@router.post("/fix-gtin")
async def api_fix_gtin(bg: BackgroundTasks):
    pendentes = _load_gtin_pendente()
    if not pendentes:
        return {"ok": True, "msg": "Nenhum GTIN pendente", "total": 0}
    bg.add_task(_fix_gtin_bg, pendentes)
    return {"ok": True, "msg": f"Iniciando fix de {len(pendentes)} GTINs", "total": len(pendentes)}


def _fix_gtin_bg(pendentes: dict[str, str]):
    try:
        tok_a = _token_akg()
    except Exception as e:
        logger.error("fix-gtin: token AKG indisponivel: %s", e)
        return

    restantes = dict(pendentes)
    for akg_id, gtin_real in pendentes.items():
        r = _req.put(f"{ML_API}/items/{akg_id}",
                     headers={"Authorization": f"Bearer {tok_a}", "Content-Type": "application/json"},
                     json={"attributes": [{"id": "GTIN", "value_name": gtin_real}]},
                     timeout=15)
        if r.status_code in (200, 201):
            logger.info("fix-gtin OK: %s -> %s", akg_id, gtin_real)
            del restantes[akg_id]
        else:
            logger.warning("fix-gtin FAIL %s: %s %s", akg_id, r.status_code, r.text[:120])
        import time as _t
        _t.sleep(0.5)

    _save_gtin_pendente(restantes)
    aplicados = len(pendentes) - len(restantes)
    logger.info("fix-gtin concluido: %d aplicados, %d ainda pendentes", aplicados, len(restantes))


_fix_sku_state: dict = {"rodando": False, "progresso": 0, "total": 0, "aplicados": 0, "erro": None}

AKG_FAMILY_ID = "4943179930257231"


def _bling_token_shinsei() -> str:
    """Retorna access_token Bling Shinsei via BlingClient (com auto-refresh)."""
    try:
        from bling_client import BlingClient
        client = BlingClient()
        headers = client._get_headers()
        return headers["Authorization"].replace("Bearer ", "")
    except Exception as e:
        raise RuntimeError(f"Token Bling indisponivel: {e}")


# Variantes de nome por linha — mesma linha pode ser cadastrada de formas diferentes no Bling
_VARIANTES_LINHA: dict[str, list[str]] = {
    "igora royal highlifts": ["Igora Royal Highlifts", "Schwarzkopf Igora Royal Highlifts", "Igora Highlifts"],
    "igora royal absolutes": ["Igora Royal Absolutes", "Schwarzkopf Igora Royal Absolutes", "Igora Absolutes"],
    "igora royal vibrance":  ["Igora Royal Vibrance",  "Schwarzkopf Igora Royal Vibrance",  "Igora Vibrance"],
    "igora royal zero amm":  ["Igora Royal ZERO AMM",  "Schwarzkopf Igora Royal ZERO AMM",  "Igora ZERO AMM"],
    "igora royal fashion lights": ["Igora Royal Fashion Lights", "Schwarzkopf Fashion Lights", "Fashion Lights"],
    "igora royal color 10":  ["Igora Royal Color 10",  "Schwarzkopf Color 10",  "Igora Color 10"],
    "igora royal":           ["Igora Royal", "Schwarzkopf Igora Royal", "Igora"],
    "evolution of the color": [
        "Evolution Of The Color",
        "Alfaparf Evolution Of The Color",
        "Alfaparf Milano Evolution Of The Color",
        "Alfaparf Professional Evolution Of The Color",
    ],
    "color wear": [
        "Color Wear",
        "Alfaparf Color Wear",
        "Alfaparf Milano Color Wear",
        "Alfaparf Professional Color Wear",
    ],
    "semi di lino": [
        "Semi Di Lino",
        "Alfaparf Semi Di Lino",
        "Alfaparf Milano Semi Di Lino",
        "Alfaparf Professional Semi Di Lino",
    ],
}


def _bling_construir_indice(token: str) -> dict[str, str]:
    """
    Pagina todo o catálogo Bling Shinsei e monta um índice:
    chave = "linha_normalizada|codigo_cor|volumes" → valor = codigo (SKU)

    O endpoint /produtos?pesquisa= ignora o termo e retorna resultados fixos,
    por isso varremos por paginação e fazemos o match localmente.
    """
    import re as _re

    LINHAS_NORM = [
        "igora royal highlifts", "igora royal absolutes", "igora royal vibrance",
        "igora royal zero amm", "igora royal fashion lights", "igora royal color 10",
        "igora royal",
        "evolution of the color", "color wear", "semi di lino",
        "skala", "inoar", "soul power", "eico", "mystic color", "phytoervas",
    ]
    cor_re_idx  = _re.compile(r"\b(?:(?:nb|NI|i)\s*)?(\d+[.\-]\d+(?:[.\-]\d+)?)\b")
    vol_re_idx  = _re.compile(r"\b(\d+)\s*[Vv]olumes?\b")

    TIPOS_COR = {"coloração", "coloracoes", "colorações", "tinta", "tintura", "coloracao permanente", "coloração permanente"}
    TIPOS_OX   = {"ativador", "agua oxigenada", "água oxigenada", "emulsao ativadora", "emulsão ativadora", "oxidante", "ox creme"}

    def _tipo_produto(nome: str) -> str:
        n = nome.lower()
        for t in TIPOS_OX:
            if t in n:
                return "ox"
        # "ox" isolado (ex: "Ox 20 volumes") — apenas quando não for parte de outra palavra
        import re as _re2
        if _re2.search(r"\box\b", n):
            return "ox"
        for t in TIPOS_COR:
            if t in n:
                return "coloracao"
        if "tonalizante" in n:
            return "tonalizante"
        if "kit" in n:
            return "kit"
        return ""

    def _extrair_chave(nome: str) -> list[str]:
        n = nome.lower()
        linha = next((l for l in LINHAS_NORM if l in n), "")
        tipo  = _tipo_produto(nome)
        mv = vol_re_idx.search(nome)
        vols = mv.group(1) if mv else ""
        cors = cor_re_idx.findall(nome)
        chaves = []
        for cor in cors:
            chave = f"{tipo}|{linha}|{cor}|{vols}"
            chaves.append(chave)
            # Também indexa sem tipo (fallback)
            chaves.append(f"|{linha}|{cor}|{vols}")
        return chaves

    indice: dict[str, str] = {}
    pagina = 1
    while True:
        r = _req.get(
            "https://api.bling.com.br/Api/v3/produtos",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={"limite": 100, "pagina": pagina},
            timeout=20,
        )
        if r.status_code != 200:
            break
        data = r.json().get("data") or []
        if not data:
            break
        for prod in data:
            codigo = prod.get("codigo") or ""
            nome   = prod.get("nome") or ""
            if not codigo:
                continue
            for chave in _extrair_chave(nome):
                if chave not in indice:
                    indice[chave] = codigo
        pagina += 1
        time.sleep(0.2)

    logger.info("fix-sku: índice Bling construído com %d entradas", len(indice))
    return indice


def _bling_buscar_sku_indice(indice: dict[str, str], titulo: str) -> str | None:
    """Busca o SKU no índice local usando tipo + linha + cor + volumes extraídos do título."""
    import re as _re

    LINHAS_NORM = [
        "igora royal highlifts", "igora royal absolutes", "igora royal vibrance",
        "igora royal zero amm", "igora royal fashion lights", "igora royal color 10",
        "igora royal",
        "evolution of the color", "color wear", "semi di lino",
        "skala", "inoar", "soul power", "eico", "mystic color", "phytoervas",
    ]
    TIPOS_OX_S  = {"ativador", "agua oxigenada", "água oxigenada", "emulsao ativadora", "emulsão ativadora", "oxidante", "ox creme"}
    TIPOS_COR_S = {"coloração", "coloracoes", "colorações", "tinta", "tintura", "coloracao permanente", "coloração permanente"}

    cor_re_s = _re.compile(r"\b(?:(?:nb|NI|i)\s*)?(\d+[.\-]\d+(?:[.\-]\d+)?)\b")
    vol_re_s = _re.compile(r"\b(\d+)\s*[Vv]olumes?\b")

    t = titulo.lower()
    linha = next((l for l in LINHAS_NORM if l in t), "")
    mv = vol_re_s.search(titulo)
    vols = mv.group(1) if mv else ""

    # Determina tipo do produto
    tipo = ""
    for tok in TIPOS_OX_S:
        if tok in t:
            tipo = "ox"
            break
    if not tipo and _re.search(r"\box\b", t):
        tipo = "ox"
    if not tipo:
        for tok in TIPOS_COR_S:
            if tok in t:
                tipo = "coloracao"
                break
    if not tipo and "tonalizante" in t:
        tipo = "tonalizante"
    if not tipo and "kit" in t:
        tipo = "kit"

    # Preferência: cor após "Selecione A Cor"
    mc_sc = _re.search(r"selecione a cor\s+(\S+)", t, _re.IGNORECASE)
    cors = [mc_sc.group(1)] if mc_sc else cor_re_s.findall(titulo)

    for cor in cors:
        # Busca mais específica primeiro: tipo|linha|cor|vols
        for chave in [
            f"{tipo}|{linha}|{cor}|{vols}",
            f"{tipo}|{linha}|{cor}|",       # sem volume
            f"|{linha}|{cor}|{vols}",       # fallback sem tipo (indexado no _extrair_chave)
            f"|{linha}|{cor}|",             # fallback sem tipo nem volume
        ]:
            if chave in indice:
                return indice[chave]

    return None


def _fix_sku_bg():
    global _fix_sku_state
    import re
    try:
        _fix_sku_state.update({"rodando": True, "progresso": 0, "aplicados": 0, "erro": None})
        tok_a = _token_akg()
        tok_b = _bling_token_shinsei()

        # Busca itens AKG sem seller_custom_field (active + paused)
        AKG_SELLER_ID = "3541432733"
        sem_sku: list[dict] = []

        def _buscar_sem_sku(status: str):
            offset = 0
            while True:
                r = _req.get(
                    f"{ML_API}/users/{AKG_SELLER_ID}/items/search",
                    headers={"Authorization": f"Bearer {tok_a}"},
                    params={"status": status, "limit": 100, "offset": offset},
                    timeout=20,
                )
                if r.status_code != 200:
                    logger.warning("fix-sku: seller search [%s] %s: %s", status, r.status_code, r.text[:100])
                    break
                body = r.json()
                ids = body.get("results") or []
                total_p = body.get("paging", {}).get("total", 0)
                if not ids:
                    break
                for i in range(0, len(ids), 20):
                    chunk = ids[i:i + 20]
                    r2 = _req.get(
                        f"{ML_API}/items",
                        headers={"Authorization": f"Bearer {tok_a}"},
                        params={"ids": ",".join(chunk), "attributes": "id,title,seller_custom_field"},
                        timeout=20,
                    )
                    if r2.status_code == 200:
                        for entry in r2.json():
                            d = entry.get("body") or entry
                            if d.get("id") and not d.get("seller_custom_field"):
                                sem_sku.append({"id": d["id"], "titulo": d.get("title", "")})
                    time.sleep(0.2)
                offset += len(ids)
                if offset >= total_p:
                    break
                time.sleep(0.3)

        _buscar_sem_sku("active")
        _buscar_sem_sku("paused")

        total = len(sem_sku)
        _fix_sku_state["total"] = total
        logger.info("fix-sku: %d itens AKG sem SKU — construindo índice Bling...", total)

        # Constrói índice local do catálogo Bling (evita endpoint pesquisa quebrado)
        indice_bling = _bling_construir_indice(tok_b)
        aplicados = 0

        for i, item in enumerate(sem_sku):
            sku = _bling_buscar_sku_indice(indice_bling, item["titulo"])
            if not sku:
                logger.warning("fix-sku: sem resultado Bling para '%s'", item["titulo"][:80])
                _fix_sku_state["progresso"] = i + 1
                time.sleep(0.3)
                continue

            put_r = _req.put(
                f"{ML_API}/items/{item['id']}",
                headers={"Authorization": f"Bearer {tok_a}", "Content-Type": "application/json"},
                json={"seller_custom_field": sku},
                timeout=15,
            )
            if put_r.status_code in (200, 201):
                logger.info("fix-sku OK: %s -> SKU=%s", item["id"], sku)
                aplicados += 1
                _fix_sku_state["aplicados"] = aplicados
            else:
                logger.warning("fix-sku FAIL %s: %s %s", item["id"], put_r.status_code, put_r.text[:120])

            _fix_sku_state["progresso"] = i + 1
            time.sleep(0.4)

        _fix_sku_state.update({"rodando": False, "progresso": total})
        logger.info("fix-sku concluido: %d/%d aplicados", aplicados, total)
    except Exception as e:
        logger.exception("Erro no fix-sku")
        _fix_sku_state.update({"rodando": False, "erro": str(e)})


@router.post("/fix-sku")
async def api_fix_sku(bg: BackgroundTasks):
    if _fix_sku_state.get("rodando"):
        return JSONResponse({"ok": False, "msg": "Fix SKU já em andamento"})
    bg.add_task(_fix_sku_bg)
    return {"ok": True, "msg": "Fix SKU iniciado em background"}


@router.get("/fix-sku/status")
async def api_fix_sku_status():
    return _fix_sku_state


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
