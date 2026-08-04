"""
services/ml_price_suggestions.py — Shinsei Pricing
Processa webhooks price_suggestion do Mercado Livre.

Quando o ML envia topic=price_suggestion com resource=/marketplace/benchmarks/items/MLB.../details,
buscamos o benchmark, calculamos a margem e salvamos o resultado para exibicao na pagina de Marketing.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

ML_API = "https://api.mercadolibre.com"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SUGESTOES_PATH = DATA_DIR / "ml_price_suggestions.json"

LISTING_TYPE_CANAL = {
    "gold_pro": "Mercado Livre Premium",
    "gold_special": "Mercado Livre Classico",
    "gold": "Mercado Livre Classico",
    "bronze": "Mercado Livre Classico",
    "silver": "Mercado Livre Classico",
    "free": "Mercado Livre Classico",
}


# ── helpers ──────────────────────────────────────────────────────────────────

def _ml_token() -> str:
    """Retorna access_token ML, renovando automaticamente se expirado (<10 min restantes)."""
    p = DATA_DIR / "ml_tokens.json"
    if not p.exists():
        return ""
    try:
        tokens = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return ""

    at = tokens.get("access_token", "")
    connected_at_str = tokens.get("connected_at", "")
    expires_in = int(tokens.get("expires_in") or 21600)

    if at and connected_at_str:
        try:
            from datetime import datetime as _dt, timezone as _tz
            connected_at = _dt.fromisoformat(connected_at_str.replace("Z", "+00:00"))
            elapsed = (_dt.now(_tz.utc) - connected_at).total_seconds()
            if elapsed > (expires_in - 600):
                logger.info("ML token expirando (elapsed=%.0fs) — renovando...", elapsed)
                try:
                    from services.mercado_livre import MercadoLivreService
                    svc = MercadoLivreService(DATA_DIR.parent)
                    result = svc.refresh_token()
                    if result.get("success"):
                        at = result["data"].get("access_token", at)
                        logger.info("ML token renovado com sucesso")
                    else:
                        logger.warning("ML token refresh falhou: %s", result.get("error"))
                except Exception as e:
                    logger.warning("ML token refresh erro: %s", e)
        except Exception as e:
            logger.debug("Erro verificando expiração token ML: %s", e)

    return at


def _ml_headers() -> dict:
    return {"Authorization": f"Bearer {_ml_token()}"}


def _load_sugestoes() -> list[dict]:
    if not SUGESTOES_PATH.exists():
        return []
    try:
        data = json.loads(SUGESTOES_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        # Filtra entradas corrompidas (nao-dict)
        return [s for s in data if isinstance(s, dict)]
    except Exception:
        return []


def _save_sugestoes(data: list[dict]) -> None:
    SUGESTOES_PATH.parent.mkdir(exist_ok=True)
    # Salva apenas dicts validos
    limpa = [s for s in data if isinstance(s, dict)]
    SUGESTOES_PATH.write_text(
        json.dumps(limpa, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def carregar_sugestoes() -> list[dict]:
    return _load_sugestoes()


def limpar_sugestoes() -> None:
    _save_sugestoes([])


def _extrair_item_id(resource: str) -> Optional[str]:
    """Extrai MLB... do resource URL do webhook."""
    m = re.search(r"(MLB\d+)", resource)
    return m.group(1) if m else None


def _extrair_sku(item: dict) -> str:
    sku = (item.get("seller_sku") or "").strip()
    if sku:
        return sku
    sku = (item.get("seller_custom_field") or "").strip()
    if sku:
        return sku
    for attr in item.get("attributes") or []:
        if not isinstance(attr, dict):
            continue
        if str(attr.get("id") or "").upper() == "SELLER_SKU":
            sku = (
                attr.get("value_name")
                or (attr.get("values") or [{}])[0].get("name")
                or ""
            ).strip()
            if sku:
                return sku
    return ""


# ── ML API calls ─────────────────────────────────────────────────────────────

def _buscar_benchmark(item_id: str) -> Optional[dict]:
    """
    Tenta obter preco sugerido via varios endpoints ML.
    1. /marketplace/benchmarks/items/{id}/details  (requer permissao especial)
    2. /items/{id}/prices                          (disponivel para todos)
    3. /marketplace/benchmarks/items/{id}          (path alternativo)
    """
    hdrs = _ml_headers()

    # Endpoint 1: benchmark oficial (pode dar 403)
    url1 = f"{ML_API}/marketplace/benchmarks/items/{item_id}/details"
    try:
        r = requests.get(url1, headers=hdrs, timeout=12)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                return {"_source": "benchmark_details", **data}
        logger.info("Benchmark/details %s: HTTP %d", item_id, r.status_code)
    except Exception as e:
        logger.debug("benchmark/details erro %s: %s", item_id, e)

    # Endpoint 2: /items/{id}/prices — disponivel sem permissao especial
    url2 = f"{ML_API}/items/{item_id}/prices"
    try:
        r = requests.get(url2, headers=hdrs, timeout=12)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                return {"_source": "item_prices", **data}
        logger.info("items/prices %s: HTTP %d", item_id, r.status_code)
    except Exception as e:
        logger.debug("items/prices erro %s: %s", item_id, e)

    # Endpoint 3: benchmark sem /details
    url3 = f"{ML_API}/marketplace/benchmarks/items/{item_id}"
    try:
        r = requests.get(url3, headers=hdrs, timeout=12)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                return {"_source": "benchmark_base", **data}
        logger.info("Benchmark/base %s: HTTP %d", item_id, r.status_code)
    except Exception as e:
        logger.debug("benchmark/base erro %s: %s", item_id, e)

    return None


def _buscar_candidate_promo(resource: str) -> Optional[dict]:
    """
    Busca dados de um candidato a promocao ML.
    resource ex: /seller-promotions/candidates/CANDIDATE-MLB...-...
    Tenta diferentes versoes de app_version ate encontrar uma que funcione.
    """
    url = f"{ML_API}{resource}" if resource.startswith("/") else resource
    hdrs = _ml_headers()
    # ML exige app_version — tenta versoes conhecidas
    for version in ["v1", "v2", "v3", ""]:
        params = {"app_version": version} if version else {}
        try:
            r = requests.get(url, headers=hdrs, params=params, timeout=12)
            if r.status_code == 200:
                data = r.json()
                logger.info(
                    "public_candidates %s (v=%s): OK — campos: %s",
                    resource, version or "sem",
                    list(data.keys()) if isinstance(data, dict) else type(data).__name__
                )
                return data if isinstance(data, dict) else {"_lista": data, "_source": "candidates"}
            elif r.status_code == 400 and "app_version" in r.text:
                continue  # Tenta proxima versao
            else:
                logger.info("public_candidates %s (v=%s): HTTP %d — %s", resource, version, r.status_code, r.text[:200])
                break
        except Exception as e:
            logger.warning("Erro public_candidates %s: %s", resource, e)
            break
    return None


def _buscar_item(item_id: str) -> Optional[dict]:
    """Busca detalhes do anuncio ML."""
    try:
        res = requests.get(
            f"{ML_API}/items/{item_id}",
            headers=_ml_headers(),
            params={
                "attributes": (
                    "id,title,price,seller_sku,seller_custom_field,"
                    "listing_type_id,status,attributes"
                )
            },
            timeout=15,
        )
        if res.status_code == 200:
            data = res.json()
            return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("Erro buscar item %s: %s", item_id, e)
    return None


def _extrair_preco_benchmark(benchmark: dict, preco_atual: float) -> Optional[float]:
    """
    Tenta extrair o preco sugerido da resposta do benchmark.
    A estrutura pode variar dependendo das permissoes da conta ML.
    Testamos varios campos possiveis.
    """
    if not benchmark:
        return None

    def _safe_dict_get(val, key):
        return val.get(key) if isinstance(val, dict) else None

    prices_val = benchmark.get("prices")

    # Extrai valor de um item de lista de precos ML (ex: {"type":"standard","amount":25.0})
    def _amount_from_list(lst):
        if not isinstance(lst, list) or not lst:
            return None
        first = lst[0]
        if isinstance(first, dict):
            return first.get("amount") or first.get("value") or first.get("price")
        return first  # pode ser um numero direto

    # Campos conhecidos da API de benchmark ML
    candidatos = [
        benchmark.get("suggested_price"),
        benchmark.get("price"),
        _safe_dict_get(benchmark.get("competitive_price"), "value"),
        _safe_dict_get(benchmark.get("suggested"), "price"),
        _safe_dict_get(benchmark.get("min_price"), "value"),
        benchmark.get("min_price") if not isinstance(benchmark.get("min_price"), dict) else None,
        _safe_dict_get(prices_val, "suggested"),
        _safe_dict_get(prices_val, "amount"),
        # Para arrays de precos (ex: /items/{id}/prices retorna lista de dicts)
        _amount_from_list(prices_val),
    ]

    for val in candidatos:
        if val is not None:
            try:
                p = float(val)
                # Ignora precos iguais ao atual — /items/{id}/prices retorna o preco do
                # proprio vendedor, nao o da buy box; so aceita se for diferente do atual
                if p > 0 and (preco_atual <= 0 or abs(p - preco_atual) >= 0.01):
                    return round(p, 2)
            except (TypeError, ValueError):
                pass

    return None


# ── processamento principal ───────────────────────────────────────────────────

def processar_price_suggestion(
    resource: str,
    user_id: str,
    bling_client=None,
    regras: Optional[list] = None,
    imposto_padrao: float = 12.0,
) -> dict:
    """
    Processa uma notificacao price_suggestion do ML.

    1. Extrai item_id do resource URL
    2. Busca detalhes do anuncio (titulo, preco, SKU, canal)
    3. Busca dados do benchmark de preco
    4. Calcula margem ao preco sugerido (se temos custo no Bling)
    5. Salva resultado na fila de sugestoes

    Retorna dict com resultado.
    """
    item_id = _extrair_item_id(resource)
    if not item_id:
        logger.warning("price_suggestion: nao extraiu item_id de '%s'", resource)
        return {"ok": False, "erro": f"item_id nao encontrado em: {resource}"}

    # Verifica se ja processamos recentemente (evita reprocessar em burst)
    lista_existente = _load_sugestoes()
    existente = next((s for s in lista_existente if s.get("item_id") == item_id), None)
    if existente:
        ultimo = existente.get("recebido_em", "")
        if ultimo:
            try:
                diff = (datetime.utcnow() - datetime.fromisoformat(ultimo)).total_seconds()
                if diff < 120:  # 2 minutos de cooldown por item
                    logger.debug("price_suggestion %s ignorado (cooldown %ds)", item_id, int(diff))
                    return {"ok": True, "item_id": item_id, "ignorado": True, "motivo": "cooldown"}
            except Exception:
                pass

    # Busca dados do anuncio
    item = _buscar_item(item_id)
    if not item:
        return {"ok": False, "item_id": item_id, "erro": "anuncio nao encontrado na API ML"}

    titulo = item.get("title", "")[:70]
    sku = _extrair_sku(item)
    preco_atual = float(item.get("price") or 0)
    listing_type = item.get("listing_type_id", "gold_special")
    canal = LISTING_TYPE_CANAL.get(listing_type, "Mercado Livre Premium")

    # Busca benchmark
    time.sleep(0.3)
    benchmark = _buscar_benchmark(item_id)
    preco_sugerido = _extrair_preco_benchmark(benchmark, preco_atual) if benchmark else None

    desconto_sugerido = None
    if preco_sugerido and preco_atual > 0:
        desconto_sugerido = round((1 - preco_sugerido / preco_atual) * 100, 1)

    # Analise de margem — roda sempre que tiver SKU + Bling + regras
    # (margem atual calculada mesmo sem preco_sugerido)
    analise = None
    if sku and bling_client and regras:
        try:
            from pricing_engine_real import montar_precificacao_bling
            resultado_bling = montar_precificacao_bling(
                regras=regras,
                criterio="sku",
                valor_busca=sku,
                embalagem=0,
                imposto=imposto_padrao,
                quantidade=1,
                objetivo="margem",
                tipo_alvo="percentual",
                valor_alvo=20,
            )
            # Garante que resultado é dict antes de chamar .get()
            if not isinstance(resultado_bling, dict):
                raise ValueError(f"montar_precificacao_bling retornou {type(resultado_bling).__name__} em vez de dict")
            if not resultado_bling.get("erro"):
                custo = float(
                    resultado_bling.get("custo_extraido", {}).get("custo_total") or 0
                )
                peso = float(
                    resultado_bling.get("peso_extraido", {}).get("peso") or 0
                )
                imposto_prod = float(resultado_bling.get("imposto_usado") or imposto_padrao)

                if custo > 0:
                    from services.marketing_ml import _calcular_margem_preco
                    m_atual = _calcular_margem_preco(
                        preco_atual, custo, peso, imposto_prod, canal, regras
                    )
                    analise = {
                        "custo": round(custo, 2),
                        "peso": peso,
                        "margem_atual": m_atual.get("margem") if m_atual.get("ok") else None,
                        "lucro_atual": m_atual.get("lucro_liquido") if m_atual.get("ok") else None,
                        "margem_sugerida": None,
                        "lucro_sugerido": None,
                        "variacao_margem": None,
                        "viavel": None,
                        "dentro_meta": None,
                    }
                    # Se tiver preco sugerido, calcula margem projetada
                    if preco_sugerido and preco_sugerido > 0:
                        m_sug = _calcular_margem_preco(
                            preco_sugerido, custo, peso, imposto_prod, canal, regras
                        )
                        if m_sug.get("ok"):
                            analise["margem_sugerida"] = m_sug.get("margem")
                            analise["lucro_sugerido"] = m_sug.get("lucro_liquido")
                            analise["variacao_margem"] = round(
                                (m_sug.get("margem") or 0) - (analise.get("margem_atual") or 0), 2
                            )
                            analise["viavel"] = (m_sug.get("margem") or 0) > 0
                            analise["dentro_meta"] = (m_sug.get("margem") or 0) >= 20
        except Exception as e:
            logger.warning("Erro analise margem SKU=%s: %s", sku, e)
            analise = {"erro": str(e)}

    # Classifica viabilidade
    classificacao = "sem_dados"
    if analise and analise.get("margem_sugerida") is not None:
        m = analise["margem_sugerida"]  # classifica pelo preco sugerido
    elif analise and analise.get("margem_atual") is not None:
        m = analise["margem_atual"]      # fallback: classifica pelo preco atual
    else:
        m = None

    if m is not None:
        if m >= 20:
            classificacao = "dentro_margem"
        elif m > 0:
            classificacao = "lucro_reduzido"
        else:
            classificacao = "prejuizo"
    elif preco_sugerido is None:
        classificacao = "sem_benchmark"

    sugestao = {
        "item_id": item_id,
        "sku": sku or "sem SKU",
        "titulo": titulo,
        "canal": canal,
        "listing_type": listing_type,
        "preco_atual": preco_atual,
        "preco_sugerido": preco_sugerido,
        "desconto_sugerido": desconto_sugerido,
        "benchmark_disponivel": benchmark is not None,
        "benchmark_campos": list(benchmark.keys()) if benchmark else [],
        "analise": analise,
        "classificacao": classificacao,
        "recebido_em": datetime.utcnow().isoformat(),
        "resource": resource,
        "user_id": str(user_id),
        "status": "pendente",
    }

    # Atualiza lista (dedup por item_id, mantém 500 mais recentes)
    lista = [s for s in lista_existente if s.get("item_id") != item_id]
    lista.insert(0, sugestao)
    lista = lista[:500]
    _save_sugestoes(lista)

    logger.info(
        "price_suggestion: item=%s sku=%s preco=%.2f→%s desc=%s%% benchmark=%s classif=%s",
        item_id, sku or "—", preco_atual,
        f"{preco_sugerido:.2f}" if preco_sugerido else "N/A",
        desconto_sugerido or "—",
        "ok" if benchmark else "403/erro",
        classificacao,
    )
    return {"ok": True, "item_id": item_id, "classificacao": classificacao, "sugestao": sugestao}


def resumo_sugestoes() -> dict:
    """Retorna contagens agrupadas por classificacao."""
    lista = _load_sugestoes()
    counts: dict[str, int] = {}
    for s in lista:
        c = s.get("classificacao", "sem_dados")
        counts[c] = counts.get(c, 0) + 1
    return {
        "total": len(lista),
        "dentro_margem": counts.get("dentro_margem", 0),
        "lucro_reduzido": counts.get("lucro_reduzido", 0),
        "prejuizo": counts.get("prejuizo", 0),
        "sem_benchmark": counts.get("sem_benchmark", 0),
        "sem_dados": counts.get("sem_dados", 0),
    }
