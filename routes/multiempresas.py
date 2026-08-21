# -*- coding: utf-8 -*-
"""
routes/multiempresas.py — Shinsei Pricing
Auditoria cruzada Shinsei ↔ AKG: produtos, anúncios, estoque, imagens,
descrições, variações, composições, preços por canal.
Chave universal: SKU (igual em Shinsei, AKG e todos os marketplaces).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import requests as _req
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)
router = APIRouter()

BASE_DIR   = Path(__file__).resolve().parent.parent
PAGES_DIR  = BASE_DIR / "pages"
BLING_API  = "https://api.bling.com.br/Api/v3"

# ── Estado do job de auditoria ────────────────────────────────────────────────
_JOB: dict = {
    "status": "idle",       # idle | running | done | error
    "iniciado_em": None,
    "concluido_em": None,
    "progresso": {"etapa": "", "atual": 0, "total": 0},
    "resumo": {},
    "produtos": [],         # lista completa com divergências
    "erros_log": [],
}
_JOB_LOCK = threading.Lock()


# ── Helpers de acesso ao Bling ────────────────────────────────────────────────

def _hdrs_shinsei() -> dict:
    from bling_client import BlingClient
    bc = BlingClient()
    return bc._get_headers()


def _hdrs_akg() -> dict:
    import importlib, sys
    app_mod = sys.modules.get("app") or importlib.import_module("app")
    return app_mod._bling_akg_headers()


def _bling_get_all(hdrs: dict, endpoint: str, extra_params: dict | None = None,
                   label: str = "", rate: float = 0.2) -> list[dict]:
    """Busca todas as páginas de um endpoint Bling (100 itens/pág)."""
    todos: list[dict] = []
    pagina = 1
    while True:
        params = {"pagina": pagina, "limite": 100, **(extra_params or {})}
        try:
            r = _req.get(f"{BLING_API}/{endpoint}", headers=hdrs, params=params, timeout=30)
            if r.status_code == 401:
                logger.error("Bling 401 em %s (%s)", endpoint, label)
                break
            if r.status_code == 429:
                time.sleep(1.5)
                continue
            if r.status_code != 200:
                logger.warning("Bling %d em %s pag=%d", r.status_code, endpoint, pagina)
                break
            data = r.json().get("data") or []
            if not data:
                break
            todos.extend(data)
            if len(data) < 100:
                break
            pagina += 1
            time.sleep(rate)
        except Exception as e:
            logger.warning("Bling get_all erro %s pag=%d: %s", endpoint, pagina, e)
            break
    return todos


def _bling_get_detail(hdrs: dict, produto_id: int | str, rate: float = 0.1) -> dict:
    try:
        r = _req.get(f"{BLING_API}/produtos/{produto_id}", headers=hdrs, timeout=20)
        if r.status_code == 200:
            return r.json().get("data") or {}
    except Exception as e:
        logger.warning("Detalhe produto %s erro: %s", produto_id, e)
    return {}


# ── Lógica de comparação ──────────────────────────────────────────────────────

def _imgs(det: dict) -> list[str]:
    midia = det.get("midia") or {}
    imgs  = midia.get("imagens") or {}
    return [i.get("link", "") for i in (imgs.get("internas") or []) if i.get("link")]


def _variacoes(det: dict) -> list[dict]:
    """Retorna lista de variações com SKU, nome e preço para comparação completa."""
    result = []
    for v in (det.get("variacoes") or []):
        result.append({
            "sku":   (v.get("codigo") or "").strip(),
            "nome":  (v.get("nome") or "").strip(),
            "preco": float(v.get("preco") or 0),
        })
    return sorted(result, key=lambda x: x["sku"])


def _composicao(det: dict) -> list[dict]:
    est = det.get("estrutura") or {}
    return [
        {"sku": (c.get("produto") or {}).get("codigo", ""), "qtd": c.get("quantidade", 1)}
        for c in (est.get("componentes") or [])
    ]


def _atributos(det: dict) -> dict[str, str]:
    """Retorna dicionário de características/atributos do produto."""
    return {
        (c.get("descricao") or c.get("nome") or "").strip(): str(c.get("resposta") or c.get("valor") or "").strip()
        for c in (det.get("caracteristicas") or [])
        if c.get("descricao") or c.get("nome")
    }


def _fornecedores(det: dict) -> list[dict]:
    """Retorna lista normalizada de fornecedores para comparação."""
    result = []
    for f in (det.get("fornecedores") or []):
        cnpj = str((f.get("contato") or {}).get("numeroDocumento") or f.get("cnpj") or "").strip()
        result.append({
            "cnpj":       cnpj,
            "codigo_fab": (f.get("codigoProdutoFornecedor") or "").strip(),
            "preco_custo": float(f.get("precoCusto") or 0),
        })
    return sorted(result, key=lambda x: x["cnpj"])


def _campos_basicos(det: dict) -> dict:
    cat  = det.get("categoria") or {}
    marc = det.get("marca") or {}
    trib = det.get("tributacao") or {}
    dim  = det.get("dimensoes") or {}
    return {
        "nome":         (det.get("nome") or "").strip(),
        "tipo":         det.get("tipo") or "",
        "formato":      det.get("formato") or "",
        "gtin":         det.get("gtin") or "",
        "peso_bruto":   float(det.get("pesoBruto") or 0),
        "peso_liquido": float(det.get("pesoLiquido") or 0),
        "largura":      float(dim.get("largura") or 0),
        "altura":       float(dim.get("altura") or 0),
        "profundidade": float(dim.get("profundidade") or 0),
        "categoria_id": str(cat.get("id") or ""),
        "categoria":    (cat.get("descricao") or "").strip(),
        "marca":        (marc.get("descricao") or "").strip(),
        # Fiscal
        "ncm":          (trib.get("ncm") or "").strip(),
        "origem":       str(trib.get("origem") or ""),
        "cst_ipi":      str(trib.get("cstIpi") or ""),
        "csosn":        str(trib.get("csosn") or ""),
        "cst":          str(trib.get("cst") or ""),
        "cest":         (trib.get("cest") or "").strip(),
        "ipi_aliquota": float(trib.get("ipiAliquota") or 0),
        "pis_aliquota": float(trib.get("pisAliquota") or 0),
        "cofins_aliquota": float(trib.get("cofinsAliquota") or 0),
        # Preços
        "preco_custo":  float(det.get("precoCusto") or 0),
        # Descrições
        "descricao_curta": (det.get("descricaoCurta") or "").strip(),
        "descricao_completa": (det.get("descricao") or "").strip(),
    }


def _estoque(det: dict) -> float:
    return float((det.get("estoque") or {}).get("saldoVirtualTotal") or 0)


def _anuncios_akg(hdrs_akg: dict) -> dict[str, list[dict]]:
    """Retorna mapa SKU → lista de anúncios AKG em todos os canais."""
    anuncios_raw = _bling_get_all(hdrs_akg, "anuncios", label="AKG anuncios")
    mapa: dict[str, list[dict]] = defaultdict(list)
    for a in anuncios_raw:
        sku = (a.get("produto") or {}).get("codigo", "") or a.get("idVendedor", "")
        if sku:
            mapa[sku].append({
                "id":       a.get("id"),
                "canal":    (a.get("loja") or {}).get("descricao") or a.get("canal") or "",
                "titulo":   a.get("titulo") or "",
                "url":      a.get("urlAnuncio") or a.get("url") or "",
                "situacao": (a.get("situacao") or {}).get("valor") or "",
            })
    return dict(mapa)


def _comparar(s: dict, a: dict) -> list[str]:
    """Compara 100% dos campos entre produto Shinsei e AKG. Retorna lista de divergências."""
    divs = []
    s_bas, a_bas = _campos_basicos(s), _campos_basicos(a)

    # ── Campos de identidade ──────────────────────────────
    for campo in ("nome", "tipo", "formato", "gtin", "ncm", "origem"):
        sv, av = s_bas.get(campo, ""), a_bas.get(campo, "")
        if sv and av and sv != av:
            divs.append(f"{campo}: SHN={sv!r} | AKG={av!r}")

    # ── Dimensões e peso ──────────────────────────────────
    for campo in ("peso_bruto", "peso_liquido", "largura", "altura", "profundidade"):
        sv, av = float(s_bas.get(campo) or 0), float(a_bas.get(campo) or 0)
        if sv and av and abs(sv - av) > 0.01:
            divs.append(f"{campo}: SHN={sv} | AKG={av}")

    # ── Categoria ─────────────────────────────────────────
    sc, ac = s_bas.get("categoria", ""), a_bas.get("categoria", "")
    if sc and ac and sc != ac:
        divs.append(f"categoria: SHN={sc!r} | AKG={ac!r}")

    # ── Marca ─────────────────────────────────────────────
    sm, am = s_bas.get("marca", ""), a_bas.get("marca", "")
    if sm and am and sm != am:
        divs.append(f"marca: SHN={sm!r} | AKG={am!r}")

    # ── Preço de custo ────────────────────────────────────
    sc_p, ac_p = float(s_bas.get("preco_custo") or 0), float(a_bas.get("preco_custo") or 0)
    if sc_p and ac_p and abs(sc_p - ac_p) > 0.01:
        divs.append(f"preco_custo: SHN=R${sc_p:.2f} | AKG=R${ac_p:.2f}")

    # ── Descrição curta ───────────────────────────────────
    sd, ad = s_bas.get("descricao_curta", ""), a_bas.get("descricao_curta", "")
    if sd and ad and sd != ad:
        divs.append("descricao_curta_diverge")

    # ── Descrição completa ────────────────────────────────
    sd2, ad2 = s_bas.get("descricao_completa", ""), a_bas.get("descricao_completa", "")
    if sd2 and ad2 and sd2 != ad2:
        divs.append("descricao_completa_diverge")

    # ── Imagens ───────────────────────────────────────────
    s_imgs = _imgs(s)
    a_imgs = _imgs(a)
    if len(s_imgs) != len(a_imgs):
        divs.append(f"imagens_qtd: SHN={len(s_imgs)} | AKG={len(a_imgs)}")
    # Compara nomes de arquivo (ignora domínio CDN)
    def _fname(url: str) -> str:
        return url.split("/")[-1].split("?")[0]
    s_fnames = sorted([_fname(u) for u in s_imgs])
    a_fnames = sorted([_fname(u) for u in a_imgs])
    if s_fnames != a_fnames:
        missing = set(s_fnames) - set(a_fnames)
        extra   = set(a_fnames) - set(s_fnames)
        if missing:
            divs.append(f"imagens_faltando_akg: {len(missing)} arquivo(s)")
        if extra:
            divs.append(f"imagens_extra_akg: {len(extra)} arquivo(s)")

    # ── Variações ─────────────────────────────────────────
    s_vars = _variacoes(s)
    a_vars = _variacoes(a)
    s_skus_var = {v["sku"] for v in s_vars}
    a_skus_var = {v["sku"] for v in a_vars}
    missing_var = s_skus_var - a_skus_var
    extra_var   = a_skus_var - s_skus_var
    if missing_var:
        divs.append(f"variacoes_faltando_akg: {len(missing_var)}")
    if extra_var:
        divs.append(f"variacoes_extra_akg: {len(extra_var)}")
    # Compara preços individuais das variações em comum
    a_var_map = {v["sku"]: v for v in a_vars}
    for sv in s_vars:
        av = a_var_map.get(sv["sku"])
        if av and sv["preco"] and av["preco"] and abs(sv["preco"] - av["preco"]) > 0.01:
            divs.append(f"preco_variacao_{sv['sku']}: SHN=R${sv['preco']:.2f} | AKG=R${av['preco']:.2f}")

    # ── Composição / Estrutura ────────────────────────────
    s_comp = sorted(_composicao(s), key=lambda x: x["sku"])
    a_comp = sorted(_composicao(a), key=lambda x: x["sku"])
    if s_comp != a_comp:
        divs.append(f"composicao: SHN={len(s_comp)} comp | AKG={len(a_comp)} comp")

    # ── Atributos / Características ───────────────────────
    s_attr = _atributos(s)
    a_attr = _atributos(a)
    for key, sval in s_attr.items():
        aval = a_attr.get(key)
        if aval is not None and sval != aval:
            divs.append(f"atributo_{key!r}: SHN={sval!r} | AKG={aval!r}")
    atrib_faltando = set(s_attr.keys()) - set(a_attr.keys())
    if atrib_faltando:
        divs.append(f"atributos_faltando_akg: {', '.join(sorted(atrib_faltando)[:5])}")

    # ── Fiscal ────────────────────────────────────────────
    for campo in ("ncm", "origem", "cst_ipi", "csosn", "cst", "cest"):
        sv, av = s_bas.get(campo, ""), a_bas.get(campo, "")
        if sv and av and sv != av:
            divs.append(f"{campo}: SHN={sv!r} | AKG={av!r}")
    for campo in ("ipi_aliquota", "pis_aliquota", "cofins_aliquota"):
        sv, av = float(s_bas.get(campo) or 0), float(a_bas.get(campo) or 0)
        if sv and av and abs(sv - av) > 0.001:
            divs.append(f"{campo}: SHN={sv} | AKG={av}")

    # ── Fornecedores ──────────────────────────────────────
    s_forn = _fornecedores(s)
    a_forn = _fornecedores(a)
    if s_forn != a_forn:
        s_cnpjs = {f["cnpj"] for f in s_forn}
        a_cnpjs = {f["cnpj"] for f in a_forn}
        faltando = s_cnpjs - a_cnpjs
        if faltando:
            divs.append(f"fornecedores_faltando_akg: {len(faltando)}")
        extras = a_cnpjs - s_cnpjs
        if extras:
            divs.append(f"fornecedores_extra_akg: {len(extras)}")
        # Verifica código de produto no fornecedor em comum
        a_forn_map = {f["cnpj"]: f for f in a_forn}
        for sf in s_forn:
            af = a_forn_map.get(sf["cnpj"])
            if af and sf["codigo_fab"] and af["codigo_fab"] and sf["codigo_fab"] != af["codigo_fab"]:
                divs.append(f"codigo_fornecedor: SHN={sf['codigo_fab']!r} | AKG={af['codigo_fab']!r}")

    # ── Estoque ───────────────────────────────────────────
    s_est = _estoque(s)
    a_est = _estoque(a)
    if s_est != a_est:
        divs.append(f"estoque: SHN={s_est} | AKG={a_est}")

    return divs


# ── Job principal de auditoria ────────────────────────────────────────────────

def _run_auditoria():
    with _JOB_LOCK:
        _JOB.update({
            "status": "running",
            "iniciado_em": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "concluido_em": None,
            "progresso": {"etapa": "Buscando produtos Shinsei...", "atual": 0, "total": 0},
            "resumo": {},
            "produtos": [],
            "erros_log": [],
        })

    try:
        hdrs_s = _hdrs_shinsei()
        hdrs_a = _hdrs_akg()
    except Exception as e:
        with _JOB_LOCK:
            _JOB["status"] = "error"
            _JOB["erros_log"].append(f"Autenticação falhou: {e}")
        return

    # 1. Produtos Shinsei (ativos)
    _JOB["progresso"]["etapa"] = "Buscando produtos Shinsei..."
    shinsei_raw = _bling_get_all(hdrs_s, "produtos", {"situacao": "A"}, "Shinsei", rate=0.2)
    # Mapa SKU → produto resumido Shinsei
    shinsei_map: dict[str, dict] = {}
    for p in shinsei_raw:
        sku = (p.get("codigo") or "").strip()
        if sku:
            shinsei_map[sku] = p

    # 2. Produtos AKG (ativos) — tenta sem filtro se situacao=A retornar vazio
    _JOB["progresso"]["etapa"] = "Buscando produtos AKG..."
    akg_raw = _bling_get_all(hdrs_a, "produtos", {"situacao": "A"}, "AKG", rate=0.2)
    if not akg_raw:
        logger.warning("AKG situacao=A retornou vazio — tentando sem filtro")
        akg_raw = _bling_get_all(hdrs_a, "produtos", {}, "AKG-sem-filtro", rate=0.2)
        with _JOB_LOCK:
            _JOB["erros_log"].append(f"AKG situacao=A vazio — buscou sem filtro, encontrou {len(akg_raw)} produtos")
    akg_map: dict[str, dict] = {}
    for p in akg_raw:
        sku = (p.get("codigo") or "").strip()
        if sku:
            akg_map[sku] = p

    # 3. Anúncios AKG por SKU
    _JOB["progresso"]["etapa"] = "Buscando anúncios AKG nos marketplaces..."
    anuncios_akg = _anuncios_akg(hdrs_a)

    # 4. Cruzamento e auditoria detalhada
    todos_skus = sorted(set(shinsei_map.keys()) | set(akg_map.keys()))
    total = len(todos_skus)
    _JOB["progresso"]["total"] = total
    _JOB["progresso"]["etapa"] = "Auditando produtos..."

    resultados: list[dict] = []
    sem_akg = 0
    sem_shinsei = 0
    com_divergencia = 0
    sem_anuncio_akg = 0
    ok = 0

    for i, sku in enumerate(todos_skus):
        _JOB["progresso"]["atual"] = i + 1

        s_resumo = shinsei_map.get(sku)
        a_resumo = akg_map.get(sku)
        anuncios = anuncios_akg.get(sku, [])

        status_vinculo = "ok"
        divergencias: list[str] = []
        s_det: dict = {}
        a_det: dict = {}

        if not s_resumo:
            status_vinculo = "so_akg"
            sem_shinsei += 1
        elif not a_resumo:
            status_vinculo = "sem_akg"
            sem_akg += 1
        else:
            # Busca detalhes para comparação profunda
            s_det = _bling_get_detail(hdrs_s, s_resumo["id"], rate=0.1)
            a_det = _bling_get_detail(hdrs_a, a_resumo["id"], rate=0.1)
            divergencias = _comparar(s_det, a_det)
            if divergencias:
                status_vinculo = "divergente"
                com_divergencia += 1
            else:
                ok += 1

        if not anuncios and a_resumo:
            sem_anuncio_akg += 1

        resultados.append({
            "sku":            sku,
            "nome":           (s_resumo or a_resumo or {}).get("nome", "")[:80],
            "status":         status_vinculo,
            "divergencias":   divergencias,
            "shinsei": {
                "id":      (s_resumo or {}).get("id"),
                "estoque": _estoque(s_det) if s_det else float((s_resumo or {}).get("saldoVirtualTotal") or 0),
                "preco":   float((s_resumo or {}).get("preco") or 0),
                "imagens": len(_imgs(s_det)) if s_det else 0,
                "variacoes": len(_variacoes(s_det)) if s_det else 0,
                "composicao": len(_composicao(s_det)) if s_det else 0,
            },
            "akg": {
                "id":      (a_resumo or {}).get("id"),
                "estoque": _estoque(a_det) if a_det else float((a_resumo or {}).get("saldoVirtualTotal") or 0),
                "preco":   float((a_resumo or {}).get("preco") or 0),
                "imagens": len(_imgs(a_det)) if a_det else 0,
                "variacoes": len(_variacoes(a_det)) if a_det else 0,
                "composicao": len(_composicao(a_det)) if a_det else 0,
            },
            "anuncios_akg": anuncios,
        })

        time.sleep(0.05)

    with _JOB_LOCK:
        _JOB["status"] = "done"
        _JOB["concluido_em"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _JOB["progresso"]["etapa"] = "Concluído"
        _JOB["resumo"] = {
            "total_shinsei":     len(shinsei_map),
            "total_akg":         len(akg_map),
            "total_skus":        total,
            "ok":                ok,
            "sem_akg":           sem_akg,
            "so_akg":            sem_shinsei,
            "divergentes":       com_divergencia,
            "sem_anuncio_akg":   sem_anuncio_akg,
            "pct_vinculados":    round(len(akg_map) / len(shinsei_map) * 100, 1) if shinsei_map else 0,
        }
        _JOB["produtos"] = resultados

    logger.info(
        "Auditoria concluída: %d SKUs | ok=%d sem_akg=%d divergentes=%d",
        total, ok, sem_akg, com_divergencia,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/multiempresas/diagnostico")
def diagnostico():
    """Testa conexões Bling Shinsei e AKG — mostra quantos produtos cada conta retorna."""
    import traceback
    result = {"shinsei": {}, "akg": {}}

    # Shinsei
    try:
        hdrs_s = _hdrs_shinsei()
        r = _req.get(f"{BLING_API}/produtos", headers=hdrs_s,
                     params={"situacao": "A", "pagina": 1, "limite": 5}, timeout=15)
        result["shinsei"] = {
            "status_http": r.status_code,
            "total_pagina1": len((r.json().get("data") or [])),
            "sample": [(p.get("codigo"), p.get("nome","")[:40]) for p in (r.json().get("data") or [])[:3]],
            "error": r.json().get("error"),
        }
    except Exception as e:
        result["shinsei"] = {"erro": str(e), "trace": traceback.format_exc()[-500:]}

    # AKG
    try:
        hdrs_a = _hdrs_akg()
        # Testa sem filtro de situação primeiro
        r2 = _req.get(f"{BLING_API}/produtos", headers=hdrs_a,
                      params={"pagina": 1, "limite": 5}, timeout=15)
        r2_sit = _req.get(f"{BLING_API}/produtos", headers=hdrs_a,
                          params={"situacao": "A", "pagina": 1, "limite": 5}, timeout=15)
        result["akg"] = {
            "status_http_sem_filtro": r2.status_code,
            "total_sem_filtro": len((r2.json().get("data") or [])),
            "status_http_com_situacaoA": r2_sit.status_code,
            "total_situacaoA": len((r2_sit.json().get("data") or [])),
            "sample_sem_filtro": [(p.get("codigo"), p.get("nome","")[:40], p.get("situacao")) for p in (r2.json().get("data") or [])[:3]],
            "error_sem_filtro": r2.json().get("error"),
            "error_situacaoA": r2_sit.json().get("error"),
            "headers_ok": bool(hdrs_a.get("Authorization")),
        }
    except Exception as e:
        result["akg"] = {"erro": str(e), "trace": traceback.format_exc()[-500:]}

    return result


@router.get("/multiempresas", response_class=HTMLResponse)
def multiempresas_page():
    f = PAGES_DIR / "multiempresas.html"
    if f.exists():
        return HTMLResponse(f.read_text(encoding="utf-8"))
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="pages/multiempresas.html não encontrado.")


@router.post("/multiempresas/auditoria/iniciar")
def auditoria_iniciar():
    """Dispara o job de auditoria em background."""
    with _JOB_LOCK:
        if _JOB["status"] == "running":
            return {"ok": False, "msg": "Auditoria já em andamento."}
    t = threading.Thread(target=_run_auditoria, daemon=True)
    t.start()
    return {"ok": True, "msg": "Auditoria iniciada. Consulte /multiempresas/auditoria/status."}


@router.get("/multiempresas/auditoria/status")
def auditoria_status():
    """Retorna progresso e resultado da última auditoria."""
    with _JOB_LOCK:
        return {
            "status":       _JOB["status"],
            "iniciado_em":  _JOB["iniciado_em"],
            "concluido_em": _JOB["concluido_em"],
            "progresso":    _JOB["progresso"],
            "resumo":       _JOB["resumo"],
            "erros_log":    _JOB["erros_log"][:20],
        }


@router.get("/multiempresas/auditoria/resumo-divergencias")
def resumo_divergencias():
    """
    Agrega e categoriza todas as divergências encontradas nos 4.197+ produtos divergentes.
    Retorna contagem por tipo de campo, exemplos e distribuição.
    """
    import re
    from collections import Counter

    with _JOB_LOCK:
        prods = [p for p in _JOB["produtos"] if p["status"] == "divergente"]

    if not prods:
        return {"total_divergentes": 0, "categorias": []}

    # Contadores por categoria
    categorias: dict[str, dict] = {}

    def _cat(div: str) -> str:
        """Normaliza o texto de divergência para uma categoria canônica."""
        d = div.lower()
        if "imagem" in d:          return "imagens"
        if "estoque" in d:         return "estoque"
        if "variacao" in d or "variação" in d or "preco_variacao" in d: return "variacoes"
        if "composicao" in d or "composição" in d: return "composicao"
        if "descricao" in d or "descrição" in d: return "descricao"
        if "nome" in d:            return "nome"
        if "peso" in d or "largura" in d or "altura" in d or "profundidade" in d: return "peso_dimensoes"
        if "gtin" in d or "ean" in d: return "gtin_ean"
        if "tipo" in d or "formato" in d: return "tipo_formato"
        if "categoria" in d:       return "categoria"
        if "marca" in d:           return "marca"
        if "ncm" in d or "origem" in d or "cst" in d or "csosn" in d or "cest" in d or "ipi" in d or "pis" in d or "cofins" in d: return "fiscal"
        if "fornecedor" in d or "codigo_fornecedor" in d: return "fornecedores"
        if "preco_custo" in d:     return "preco_custo"
        if "atributo" in d:        return "atributos"
        return "outros"

    cat_labels = {
        "imagens":        "Imagens (quantidade / arquivos diferentes)",
        "estoque":        "Estoque (saldo divergente)",
        "variacoes":      "Variações (faltando, extras ou preço diferente)",
        "composicao":     "Composição / Estrutura de kit",
        "descricao":      "Descrição (curta ou completa)",
        "nome":           "Nome do produto",
        "peso_dimensoes": "Peso / Dimensões",
        "gtin_ean":       "GTIN / EAN / Código de barras",
        "tipo_formato":   "Tipo ou Formato do produto",
        "categoria":      "Categoria (classificação Bling)",
        "marca":          "Marca",
        "fiscal":         "Dados fiscais (NCM, CST, CSOSN, CEST, alíquotas)",
        "fornecedores":   "Fornecedores (CNPJ / código do produto)",
        "preco_custo":    "Preço de custo divergente",
        "atributos":      "Atributos / Características",
        "outros":         "Outros campos",
    }

    contagem: Counter = Counter()
    exemplos_raw: dict[str, list[dict]] = defaultdict(list)
    detalhes_raw: dict[str, list[str]] = defaultdict(list)

    for p in prods:
        cats_vistas = set()
        for div in (p.get("divergencias") or []):
            cat = _cat(div)
            if cat not in cats_vistas:
                contagem[cat] += 1
                cats_vistas.add(cat)
            if len(exemplos_raw[cat]) < 5:
                exemplos_raw[cat].append({
                    "sku":  p["sku"],
                    "nome": p["nome"][:50],
                    "detalhe": div,
                })
            if div not in detalhes_raw[cat]:
                detalhes_raw[cat].append(div)

    # Montar resposta ordenada por frequência
    total_divs = len(prods)
    resultado = []
    for cat, cnt in contagem.most_common():
        resultado.append({
            "categoria":  cat,
            "label":      cat_labels.get(cat, cat),
            "quantidade": cnt,
            "pct_dos_divergentes": round(cnt / total_divs * 100, 1),
            "exemplos_de_divergencia": detalhes_raw[cat][:8],
            "exemplos_produtos": exemplos_raw[cat],
        })

    # Produtos com múltiplas divergências
    multi = [p for p in prods if len(p.get("divergencias", [])) > 1]

    return {
        "total_divergentes": total_divs,
        "com_multiplas_divergencias": len(multi),
        "categorias": resultado,
    }


@router.get("/multiempresas/auditoria/produtos")
def auditoria_produtos(
    status: str = "",
    q: str = "",
    pagina: int = 1,
    limite: int = 100,
):
    """
    Lista produtos auditados com filtros.
    status: ok | sem_akg | so_akg | divergente
    q: busca por SKU ou nome
    """
    with _JOB_LOCK:
        prods = list(_JOB["produtos"])

    if status:
        prods = [p for p in prods if p["status"] == status]
    if q:
        ql = q.lower()
        prods = [p for p in prods if ql in p["sku"].lower() or ql in p["nome"].lower()]

    total = len(prods)
    inicio = (pagina - 1) * limite
    pagina_prods = prods[inicio: inicio + limite]

    return {
        "total": total,
        "pagina": pagina,
        "limite": limite,
        "paginas": (total + limite - 1) // limite,
        "produtos": pagina_prods,
    }
