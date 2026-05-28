"""
routes/vinculo_ml.py — Shinsei Pricing
Ferramenta de vínculo em massa: ML ↔ Bling.

Endpoints:
  GET  /conferencia/ml/vincular             → página HTML
  GET  /conferencia/ml/sugestoes-vinculo    → sugestões de match por GTIN e título
  POST /conferencia/ml/aplicar-vinculo      → aplica vínculos aprovados via ML API
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter()
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DATA_DIR  = BASE_DIR / "data"
PAGES_DIR = BASE_DIR / "pages"


# ─────────────────────────────────────────────────────────────────────────────
# Algoritmo de match
# ─────────────────────────────────────────────────────────────────────────────

_STOP_PT = {
    "de", "da", "do", "das", "dos", "e", "em", "a", "o", "os", "as",
    "um", "uma", "para", "com", "por", "ao", "na", "no", "nas", "nos",
    "kit", "cx", "caixa", "und", "unds", "unid", "unidade", "unidades",
    "ml", "g", "kg", "l", "lt", "mm", "cm", "m",
    "var", "c",
}


def _tokenize(text: str) -> set[str]:
    """Tokeniza texto para matching: minúsculas, sem acentos, sem stopwords curtas."""
    import unicodedata
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    # Remove conteúdo entre parênteses (variantes como "(4 Unds)")
    text = re.sub(r"\(.*?\)", " ", text)
    # Substitui separadores por espaço
    text = re.sub(r"[^\w\s]", " ", text)
    tokens = text.split()
    return {t for t in tokens if t not in _STOP_PT and len(t) >= 2}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _overlap(a: set, b: set) -> float:
    """Overlap coefficient — útil quando o nome Bling é subconjunto do título ML."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _gerar_sugestoes(resultado: dict) -> list[dict]:
    """
    Percorre ml_sem_bling e sem_sku_ml do resultado da conferência e gera
    sugestões de vínculo por:
      1. GTIN exato: código numérico no ML que bate com o gtin do produto Bling
      2. Título: similaridade Jaccard + overlap entre título ML e nome Bling
    """
    # ── Índice de produtos Bling ──────────────────────────────────────────────
    bling_prods: dict[str, dict] = {}
    for row in resultado.get("matrix", []):
        if row.get("presente_bling"):
            bling_prods[row["sku"]] = {
                "nome": row.get("nome", ""),
                "gtin": row.get("gtin_bling", ""),
                "estoque": row.get("estoque_bling", 0),
                "situacao": row.get("situacao_bling", ""),
            }

    # GTIN map: código numérico → Bling SKU
    gtin_map: dict[str, str] = resultado.get("bling_gtin_map", {})
    # Complementa com GTINs do índice (para conferências antigas sem bling_gtin_map)
    for sku, info in bling_prods.items():
        g = info.get("gtin", "")
        if g and g not in gtin_map:
            gtin_map[g] = sku

    # ── Índice invertido para busca por título ────────────────────────────────
    bling_tokens: dict[str, set] = {
        sku: _tokenize(info["nome"])
        for sku, info in bling_prods.items()
    }
    token_idx: dict[str, set] = defaultdict(set)
    for sku, toks in bling_tokens.items():
        for t in toks:
            token_idx[t].add(sku)

    def _melhor_match(titulo: str, codigo_atual: str) -> tuple[str | None, float, str]:
        """Retorna (bling_sku, score, metodo)."""
        # 1. GTIN exato
        if codigo_atual and codigo_atual.isdigit() and len(codigo_atual) >= 8:
            sku_gtin = gtin_map.get(codigo_atual)
            if sku_gtin and sku_gtin in bling_prods:
                return sku_gtin, 0.99, "gtin_exato"

        # 2. Título (inverted index + Jaccard/overlap)
        ml_tok = _tokenize(titulo)
        if not ml_tok:
            return None, 0.0, ""

        candidatos: set[str] = set()
        for t in ml_tok:
            candidatos |= token_idx.get(t, set())

        melhor_sku: str | None = None
        melhor_score: float = 0.28   # threshold mínimo
        for sku in candidatos:
            b_tok = bling_tokens.get(sku, set())
            # Combina Jaccard (precisão) e overlap (recall para títulos longos)
            j = _jaccard(ml_tok, b_tok)
            ov = _overlap(ml_tok, b_tok) * 0.85
            score = max(j, ov)
            if score > melhor_score:
                melhor_score = score
                melhor_sku = sku

        return melhor_sku, round(melhor_score, 2), "titulo"

    sugestoes: list[dict] = []
    seen_mlbs: set[str] = set()  # deduplica por ml_id (evita repetir mesmo MLB para várias variações)

    # ── ml_sem_bling: têm código mas não existe no Bling ─────────────────────
    for item in resultado.get("ml_sem_bling", []):
        if item.get("status") != "active":
            continue
        ml_id = item.get("ml_id", "")
        if not ml_id or ml_id in seen_mlbs:
            continue

        sku, score, metodo = _melhor_match(
            item.get("titulo", ""),
            item.get("sku", ""),
        )
        if sku:
            nivel = "alta" if score >= 0.68 else "media" if score >= 0.48 else "baixa"
            sugestoes.append({
                "ml_id":       ml_id,
                "ml_titulo":   item.get("titulo", ""),
                "codigo_atual": item.get("sku", ""),
                "tipo_atual":  item.get("tipo_divergencia", "sku_nao_encontrado"),
                "bling_sku":   sku,
                "bling_nome":  bling_prods[sku]["nome"],
                "bling_estoque": bling_prods[sku]["estoque"],
                "confianca":   score,
                "nivel":       nivel,
                "metodo":      metodo,
            })
            seen_mlbs.add(ml_id)

    # ── sem_sku_ml: sem nenhum código ────────────────────────────────────────
    for item in resultado.get("sem_sku_ml", []):
        if item.get("status") != "active":
            continue
        ml_id = item.get("id", "")
        if not ml_id or ml_id in seen_mlbs:
            continue

        # Remove sufixo "(var. XXXXXXX)" do título (produto com variações)
        titulo_raw = item.get("titulo", "")
        titulo = re.sub(r"\s*\(var\.?\s*\d+\)", "", titulo_raw).strip()

        sku, score, metodo = _melhor_match(titulo, "")
        if sku:
            nivel = "alta" if score >= 0.68 else "media" if score >= 0.48 else "baixa"
            sugestoes.append({
                "ml_id":       ml_id,
                "ml_titulo":   titulo,
                "codigo_atual": "",
                "tipo_atual":  "sem_sku",
                "bling_sku":   sku,
                "bling_nome":  bling_prods[sku]["nome"],
                "bling_estoque": bling_prods[sku]["estoque"],
                "confianca":   score,
                "nivel":       nivel,
                "metodo":      metodo,
            })
            seen_mlbs.add(ml_id)

    sugestoes.sort(key=lambda x: -x["confianca"])
    return sugestoes


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/conferencia/ml/vincular")
async def vincular_ml_page():
    return FileResponse(PAGES_DIR / "vincular_ml.html")


@router.get("/conferencia/ml/sugestoes-vinculo")
async def get_sugestoes_vinculo():
    """Gera sugestões de vínculo ML ↔ Bling por GTIN e similaridade de título."""
    from conferencia_sku import get_resultado
    res = get_resultado()
    if not res or not res.get("stats"):
        return JSONResponse(
            {"ok": False, "erro": "Execute a conferência primeiro para gerar sugestões."},
            status_code=404,
        )
    try:
        sugestoes = _gerar_sugestoes(res)
        return {
            "ok":    True,
            "total": len(sugestoes),
            "alta":  sum(1 for s in sugestoes if s["nivel"] == "alta"),
            "media": sum(1 for s in sugestoes if s["nivel"] == "media"),
            "baixa": sum(1 for s in sugestoes if s["nivel"] == "baixa"),
            "sugestoes": sugestoes,
        }
    except Exception as e:
        logger.exception("Erro ao gerar sugestões de vínculo ML: %s", e)
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=500)


@router.post("/conferencia/ml/aplicar-vinculo")
async def aplicar_vinculo(request: Request):
    """
    Aplica vínculos aprovados via ML API (PUT seller_custom_field).
    Body: {"vinculos": [{"ml_id": "MLB123", "bling_sku": "SKU001"}, ...]}

    Para itens com variações: atualiza o seller_custom_field de TODAS as variações.
    Para itens simples: atualiza o campo no item diretamente.
    """
    import requests as _req

    body = await request.json()
    vinculos: list[dict] = body.get("vinculos", [])
    if not vinculos:
        return JSONResponse({"ok": False, "erro": "Nenhum vínculo enviado."}, status_code=400)

    # Carrega token ML
    tp = DATA_DIR / "ml_tokens.json"
    if not tp.exists():
        return JSONResponse({"ok": False, "erro": "Token ML não encontrado."}, status_code=401)
    tokens = json.loads(tp.read_text(encoding="utf-8"))
    token = tokens.get("access_token", "")
    if not token:
        return JSONResponse({"ok": False, "erro": "access_token ML inválido."}, status_code=401)

    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    resultados: list[dict] = []
    ok_count = 0
    err_count = 0

    for v in vinculos:
        ml_id    = str(v.get("ml_id", "")).strip()
        bling_sku = str(v.get("bling_sku", "")).strip()
        if not ml_id or not bling_sku:
            continue

        try:
            # Verifica se item tem variações
            r_item = _req.get(
                f"https://api.mercadolibre.com/items/{ml_id}",
                params={"attributes": "id,variations"},
                headers=h,
                timeout=15,
            )
            if r_item.status_code == 401:
                # Token expirado — retorna erro para o cliente tentar novamente
                return JSONResponse(
                    {"ok": False, "erro": "Token ML expirado — acesse /ml/login para renovar."},
                    status_code=401,
                )
            if r_item.status_code != 200:
                resultados.append({
                    "ml_id": ml_id, "bling_sku": bling_sku,
                    "ok": False, "erro": f"GET /items falhou: {r_item.status_code}",
                })
                err_count += 1
                continue

            item_data = r_item.json()
            variations = item_data.get("variations") or []

            if variations:
                # Produto com variações: atualiza seller_custom_field em TODAS as variações
                payload = {
                    "variations": [
                        {"id": var_obj["id"], "seller_custom_field": bling_sku}
                        for var_obj in variations
                    ]
                }
            else:
                # Item simples: atualiza diretamente no item
                payload = {"seller_custom_field": bling_sku}

            r_put = _req.put(
                f"https://api.mercadolibre.com/items/{ml_id}",
                json=payload,
                headers=h,
                timeout=15,
            )
            ok = r_put.status_code in (200, 201)
            if ok:
                ok_count += 1
                logger.info("ML vínculo aplicado: %s → %s", ml_id, bling_sku)
            else:
                err_count += 1
                logger.warning(
                    "ML PUT %s falhou (%d): %s", ml_id, r_put.status_code, r_put.text[:300]
                )

            resultados.append({
                "ml_id":       ml_id,
                "bling_sku":   bling_sku,
                "ok":          ok,
                "status_http": r_put.status_code,
                "variacoes":   len(variations),
                "erro":        "" if ok else r_put.text[:200],
            })

        except Exception as e:
            err_count += 1
            resultados.append({
                "ml_id": ml_id, "bling_sku": bling_sku,
                "ok": False, "erro": str(e)[:200],
            })

        time.sleep(0.35)  # respeita rate limit ML

    return {
        "ok":       err_count == 0,
        "aplicados": ok_count,
        "erros":    err_count,
        "resultados": resultados,
    }
