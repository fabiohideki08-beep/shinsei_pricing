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


def _atualizar_seller_custom_field(ml_id: str, bling_sku: str, token: str) -> dict:
    """Aplica seller_custom_field no ML via API. Retorna dict com resultado."""
    import requests as _req
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        r_item = _req.get(
            f"https://api.mercadolibre.com/items/{ml_id}",
            params={"attributes": "id,variations"},
            headers=h, timeout=15,
        )
        if r_item.status_code == 401:
            return {"ml_id": ml_id, "bling_sku": bling_sku, "ok": False, "erro": "token_expirado"}
        if r_item.status_code != 200:
            return {"ml_id": ml_id, "bling_sku": bling_sku, "ok": False, "erro": f"GET {r_item.status_code}"}

        variations = r_item.json().get("variations") or []
        if variations:
            payload = {"variations": [{"id": v["id"], "seller_custom_field": bling_sku} for v in variations]}
        else:
            payload = {"seller_custom_field": bling_sku}

        r_put = _req.put(f"https://api.mercadolibre.com/items/{ml_id}", json=payload, headers=h, timeout=15)
        ok = r_put.status_code in (200, 201)
        if ok:
            logger.info("ML auto-vínculo: %s → %s", ml_id, bling_sku)
        else:
            logger.warning("ML PUT %s falhou (%d): %s", ml_id, r_put.status_code, r_put.text[:200])
        return {
            "ml_id": ml_id, "bling_sku": bling_sku, "ok": ok,
            "status_http": r_put.status_code, "variacoes": len(variations),
            "erro": "" if ok else r_put.text[:150],
        }
    except Exception as e:
        return {"ml_id": ml_id, "bling_sku": bling_sku, "ok": False, "erro": str(e)[:150]}


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


@router.post("/conferencia/ml/vincular-variacao")
async def vincular_variacao(request: Request):
    """
    Vincula uma variação específica de um anúncio ML a um SKU Bling.
    Body: {"ml_id": "MLBU3631020323", "variation_id": 733168645, "bling_sku": "U3631020323"}
    Atualiza seller_custom_field APENAS na variação indicada.
    """
    import requests as _req

    body = await request.json()
    ml_id       = str(body.get("ml_id", "")).strip()
    variation_id = body.get("variation_id")
    bling_sku   = str(body.get("bling_sku", "")).strip()

    if not ml_id or not variation_id or not bling_sku:
        return JSONResponse({"ok": False, "erro": "ml_id, variation_id e bling_sku são obrigatórios."}, status_code=400)

    tp = DATA_DIR / "ml_tokens.json"
    if not tp.exists():
        return JSONResponse({"ok": False, "erro": "Token ML não encontrado."}, status_code=401)
    tokens = json.loads(tp.read_text(encoding="utf-8"))
    token = tokens.get("access_token", "")
    if not token:
        return JSONResponse({"ok": False, "erro": "access_token ML inválido."}, status_code=401)

    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Confirma que a variação existe no item
    r_item = _req.get(
        f"https://api.mercadolibre.com/items/{ml_id}",
        params={"attributes": "id,variations"},
        headers=h, timeout=15,
    )
    if r_item.status_code == 401:
        return JSONResponse({"ok": False, "erro": "Token ML expirado — acesse /ml/login para renovar."}, status_code=401)
    if r_item.status_code != 200:
        return JSONResponse({"ok": False, "erro": f"Erro ao buscar anúncio: {r_item.status_code}"}, status_code=502)

    variations = r_item.json().get("variations") or []
    ids_existentes = [v["id"] for v in variations]
    if int(variation_id) not in ids_existentes:
        return JSONResponse({
            "ok": False,
            "erro": f"Variação {variation_id} não encontrada no anúncio {ml_id}. Variações existentes: {ids_existentes}"
        }, status_code=404)

    # Aplica seller_custom_field só nessa variação
    payload = {"variations": [{"id": int(variation_id), "seller_custom_field": bling_sku}]}
    r_put = _req.put(f"https://api.mercadolibre.com/items/{ml_id}", json=payload, headers=h, timeout=15)
    ok = r_put.status_code in (200, 201)
    if ok:
        logger.info("ML vínculo variação: %s[%s] → %s", ml_id, variation_id, bling_sku)
    else:
        logger.warning("ML PUT variação %s[%s] falhou (%d): %s", ml_id, variation_id, r_put.status_code, r_put.text[:200])

    return {
        "ok":          ok,
        "ml_id":       ml_id,
        "variation_id": variation_id,
        "bling_sku":   bling_sku,
        "status_http": r_put.status_code,
        "erro":        "" if ok else r_put.text[:200],
    }


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


@router.post("/conferencia/ml/aplicar-vinculo-automatico")
async def aplicar_vinculo_automatico(request: Request):
    """
    Aplica automaticamente seller_custom_field no ML para todos os itens sem SKU
    que tiverem match de ALTA confiança (score >= 0.68) na análise de sugestões.

    Query params:
      - confianca_minima: float (default 0.68) — threshold mínimo para aplicar
      - dry_run: bool (default false) — se true, apenas simula sem alterar o ML
    """
    import requests as _req

    params = dict(request.query_params)
    confianca_minima = float(params.get("confianca_minima", 0.68))
    dry_run = params.get("dry_run", "false").lower() == "true"

    from conferencia_sku import get_resultado
    res = get_resultado()
    if not res or not res.get("stats"):
        return JSONResponse({"ok": False, "erro": "Execute a conferência primeiro."}, status_code=404)

    sugestoes = _gerar_sugestoes(res)
    candidatos = [s for s in sugestoes if s["confianca"] >= confianca_minima]

    if not candidatos:
        return {"ok": True, "aplicados": 0, "total_sugestoes": len(sugestoes),
                "msg": f"Nenhuma sugestão com confiança >= {confianca_minima}"}

    # Carrega token ML
    tp = DATA_DIR / "ml_tokens.json"
    if not tp.exists():
        return JSONResponse({"ok": False, "erro": "Token ML não encontrado."}, status_code=401)
    tokens = json.loads(tp.read_text(encoding="utf-8"))
    token = tokens.get("access_token", "")
    if not token:
        return JSONResponse({"ok": False, "erro": "access_token ML inválido."}, status_code=401)

    resultados = []
    ok_count = err_count = 0

    for s in candidatos:
        if dry_run:
            resultados.append({**s, "ok": True, "simulado": True})
            ok_count += 1
            continue

        r = _atualizar_seller_custom_field(s["ml_id"], s["bling_sku"], token)
        if r.get("erro") == "token_expirado":
            return JSONResponse({"ok": False, "erro": "Token ML expirado — acesse /ml/login para renovar."}, status_code=401)

        r["ml_titulo"]   = s["ml_titulo"]
        r["bling_nome"]  = s["bling_nome"]
        r["confianca"]   = s["confianca"]
        r["metodo"]      = s["metodo"]
        resultados.append(r)
        if r["ok"]:
            ok_count += 1
        else:
            err_count += 1
        time.sleep(0.35)

    # Salva log
    log_path = DATA_DIR / "vinculo_automatico_ml.json"
    try:
        log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
    except Exception:
        log = []
    log.append({
        "ts": __import__("datetime").datetime.utcnow().isoformat(),
        "dry_run": dry_run,
        "confianca_minima": confianca_minima,
        "aplicados": ok_count,
        "erros": err_count,
        "resultados": resultados[-500:],
    })
    log_path.write_text(json.dumps(log[-10:], ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok":               err_count == 0,
        "dry_run":          dry_run,
        "confianca_minima": confianca_minima,
        "total_sugestoes":  len(sugestoes),
        "candidatos":       len(candidatos),
        "aplicados":        ok_count,
        "erros":            err_count,
        "resultados":       resultados,
    }


@router.post("/conferencia/ml/varredura-sem-sku")
async def varredura_sem_sku(request: Request):
    """
    Varredura autônoma: busca TODOS os anúncios ML ativos sem seller_custom_field,
    tenta match com Bling por GTIN e título, aplica automaticamente os de alta
    confiança (>= 0.68) e retorna lista completa para revisão.

    Query params:
      - dry_run: bool (default false) — simula sem alterar
      - confianca_auto: float (default 0.68) — aplica automaticamente acima desse valor
    """
    import requests as _req
    import unicodedata

    params = dict(request.query_params)
    dry_run        = params.get("dry_run", "false").lower() == "true"
    confianca_auto = float(params.get("confianca_auto", 0.68))

    # ── Token ML ──────────────────────────────────────────────────────────────
    tp = DATA_DIR / "ml_tokens.json"
    if not tp.exists():
        return JSONResponse({"ok": False, "erro": "Token ML não encontrado."}, status_code=401)
    tokens = json.loads(tp.read_text(encoding="utf-8"))
    ml_token = tokens.get("access_token", "")
    if not ml_token:
        return JSONResponse({"ok": False, "erro": "access_token ML inválido."}, status_code=401)
    ml_h = {"Authorization": f"Bearer {ml_token}"}

    # ── Token Bling via BlingClient (lê Secret Manager se necessário) ────────
    try:
        from bling_client import BlingClient
        _bc = BlingClient()
        _bc._load_tokens()  # garante refresh se necessário
        bling_token = (_bc.tokens or {}).get("access_token", "")
    except Exception as _e:
        return JSONResponse({"ok": False, "erro": f"Token Bling não disponível: {_e}"}, status_code=401)
    if not bling_token:
        return JSONResponse({"ok": False, "erro": "Token Bling vazio — reautentique em /bling/auth."}, status_code=401)
    bling_h = {"Authorization": f"Bearer {bling_token}", "Accept": "application/json"}

    # ── 1. Busca todos os anúncios ML do vendedor ─────────────────────────────
    # Primeiro pega o user_id
    r_me = _req.get("https://api.mercadolibre.com/users/me", headers=ml_h, timeout=15)
    if r_me.status_code == 401:
        return JSONResponse({"ok": False, "erro": "Token ML expirado — acesse /ml/login para renovar."}, status_code=401)
    seller_id = r_me.json().get("id")

    ml_items_sem_sku: list[dict] = []
    offset = 0
    limit  = 100
    logger.info("[VARREDURA] Buscando anúncios ML sem seller_custom_field ...")
    while True:
        r = _req.get(
            f"https://api.mercadolibre.com/users/{seller_id}/items/search",
            params={"status": "active", "offset": offset, "limit": limit},
            headers=ml_h, timeout=20,
        )
        if r.status_code != 200:
            break
        ids = r.json().get("results") or []
        if not ids:
            break

        # Busca detalhes em lote (máx 20 por chamada)
        for i in range(0, len(ids), 20):
            lote = ids[i:i+20]
            r2 = _req.get(
                "https://api.mercadolibre.com/items",
                params={"ids": ",".join(lote),
                        "attributes": "id,title,status,seller_custom_field,variations,catalog_product_id"},
                headers=ml_h, timeout=20,
            )
            if r2.status_code != 200:
                continue
            for entry in r2.json():
                item = entry.get("body") or entry
                if item.get("status") != "active":
                    continue
                scf = item.get("seller_custom_field") or ""
                varis = item.get("variations") or []
                # Variações também podem ter seller_custom_field vazio
                varis_sem = [v for v in varis if not (v.get("seller_custom_field") or "").strip()]
                if not scf.strip() or varis_sem:
                    ml_items_sem_sku.append({
                        "ml_id":  item["id"],
                        "titulo": item.get("title", ""),
                        "scf_atual": scf,
                        "variacoes_sem_sku": len(varis_sem),
                        "total_variacoes": len(varis),
                    })
            time.sleep(0.2)

        offset += limit
        total = r.json().get("paging", {}).get("total", 0)
        if offset >= total:
            break
        time.sleep(0.3)

    logger.info("[VARREDURA] %d anúncios sem SKU encontrados", len(ml_items_sem_sku))

    # ── 2. Busca produtos Bling ───────────────────────────────────────────────
    bling_prods: list[dict] = []
    pag = 1
    while True:
        r = _req.get("https://api.bling.com.br/Api/v3/produtos",
                     headers=bling_h,
                     params={"pagina": pag, "limite": 100, "situacao": "A", "tipo": "P"},
                     timeout=30)
        if r.status_code == 401:
            return JSONResponse({"ok": False, "erro": "Token Bling expirado — reautentique em /bling/auth."}, status_code=401)
        if r.status_code != 200:
            break
        itens = r.json().get("data") or []
        if not itens:
            break
        bling_prods.extend(itens)
        if len(itens) < 100:
            break
        pag += 1
        time.sleep(0.4)

    logger.info("[VARREDURA] %d produtos Bling carregados", len(bling_prods))

    # ── 3. Índices para matching ──────────────────────────────────────────────
    gtin_map: dict[str, str] = {}
    bling_info: dict[str, dict] = {}
    bling_tokens_idx: dict[str, set] = {}

    for p in bling_prods:
        sku  = p.get("codigo", "")
        nome = p.get("nome", "")
        gtin = p.get("gtin") or p.get("codigoBarras") or ""
        if not sku:
            continue
        if gtin and str(gtin).isdigit() and len(str(gtin)) >= 8:
            gtin_map[str(gtin)] = sku
        bling_info[sku] = {"nome": nome, "gtin": gtin}
        bling_tokens_idx[sku] = _tokenize(nome)

    token_inv: dict[str, set] = defaultdict(set)
    for sku, toks in bling_tokens_idx.items():
        for t in toks:
            token_inv[t].add(sku)

    def _match(titulo: str, codigo: str) -> tuple[str | None, float, str]:
        if codigo and str(codigo).isdigit() and len(str(codigo)) >= 8:
            s = gtin_map.get(str(codigo))
            if s:
                return s, 0.99, "gtin_exato"
        ml_tok = _tokenize(titulo)
        candidatos: set[str] = set()
        for t in ml_tok:
            candidatos |= token_inv.get(t, set())
        best_sku, best_score = None, 0.28
        for sku in candidatos:
            b = bling_tokens_idx.get(sku, set())
            score = max(_jaccard(ml_tok, b), _overlap(ml_tok, b) * 0.85)
            if score > best_score:
                best_score, best_sku = score, sku
        return best_sku, round(best_score, 2), "titulo"

    # ── 4. Gera sugestões e aplica automaticamente as de alta confiança ───────
    sugestoes: list[dict] = []
    aplicados = erros = 0

    for item in ml_items_sem_sku:
        sku, score, metodo = _match(item["titulo"], item.get("scf_atual", ""))
        nivel = "alta" if score >= 0.68 else "media" if score >= 0.48 else "baixa" if sku else "sem_match"
        entrada = {
            "ml_id":     item["ml_id"],
            "titulo":    item["titulo"],
            "bling_sku": sku or "",
            "bling_nome": bling_info.get(sku, {}).get("nome", "") if sku else "",
            "confianca": score,
            "nivel":     nivel,
            "metodo":    metodo,
            "aplicado":  False,
            "erro":      "",
        }
        if sku and score >= confianca_auto and not dry_run:
            res = _atualizar_seller_custom_field(item["ml_id"], sku, ml_token)
            if res.get("erro") == "token_expirado":
                return JSONResponse({"ok": False, "erro": "Token ML expirado durante varredura."}, status_code=401)
            entrada["aplicado"] = res["ok"]
            entrada["erro"]     = res.get("erro", "")
            if res["ok"]:
                aplicados += 1
            else:
                erros += 1
            time.sleep(0.4)
        sugestoes.append(entrada)

    sugestoes.sort(key=lambda x: -x["confianca"])

    return {
        "ok":             True,
        "dry_run":        dry_run,
        "total_sem_sku":  len(ml_items_sem_sku),
        "total_bling":    len(bling_prods),
        "aplicados":      aplicados,
        "erros":          erros,
        "alta":           sum(1 for s in sugestoes if s["nivel"] == "alta"),
        "media":          sum(1 for s in sugestoes if s["nivel"] == "media"),
        "baixa":          sum(1 for s in sugestoes if s["nivel"] == "baixa"),
        "sem_match":      sum(1 for s in sugestoes if s["nivel"] == "sem_match"),
        "sugestoes":      sugestoes,
    }
