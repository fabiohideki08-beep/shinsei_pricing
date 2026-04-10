"""
ml_pricing_engine.py — Shinsei Pricing
Motor de precificação ML com taxas em tempo real via API.
Cache de 24h para evitar excesso de chamadas.
"""
from __future__ import annotations
import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).parent
CACHE_PATH = BASE_DIR / "data" / "ml_taxas_cache.json"

ML_API = "https://api.mercadolibre.com"
CACHE_TTL = 86400  # 24h


def _load_token() -> Optional[str]:
    path = BASE_DIR / "data" / "ml_tokens.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("access_token")


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict):
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _cache_key(listing_type: str, price: float, weight_g: int, category_id: str) -> str:
    price_bucket = int(price // 10) * 10  # arredonda para dezena
    weight_bucket = int(weight_g // 100) * 100  # arredonda para 100g
    return f"{listing_type}_{price_bucket}_{weight_bucket}_{category_id}"


def get_ml_taxa_real(
    listing_type: str,
    price: float,
    weight_g: int,
    category_id: str = "",
    force_refresh: bool = False
) -> dict:
    """
    Busca taxa real do ML via API com cache de 24h.
    Retorna: {comissao_pct, frete_operacional, total_ml, voce_recebe}
    """
    cache = _load_cache()
    key = _cache_key(listing_type, price, weight_g, category_id)

    # Verifica cache
    if not force_refresh and key in cache:
        entry = cache[key]
        if time.time() - entry.get("ts", 0) < CACHE_TTL:
            return entry["data"]

    # Busca da API
    token = _load_token()
    if not token:
        logger.warning("Token ML não disponível, usando fallback")
        return _fallback_taxa(listing_type, price)

    try:
        import requests
        params = {
            "listing_type_id": listing_type,
            "price": price,
            "billable_weight": weight_g,
        }
        if category_id:
            params["category_id"] = category_id

        r = requests.get(
            f"{ML_API}/sites/MLB/listing_prices",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=8
        )

        if r.status_code == 401:
            logger.warning("Token ML expirado")
            return _fallback_taxa(listing_type, price)

        if r.status_code != 200:
            logger.warning("ML API erro %d", r.status_code)
            return _fallback_taxa(listing_type, price)

        d = r.json()
        if isinstance(d, list):
            d = d[0] if d else {}

        det = d.get("sale_fee_details", {})
        total = float(d.get("sale_fee_amount") or 0)
        comissao_pct = float(det.get("percentage_fee") or det.get("meli_percentage_fee") or 0) / 100
        frete_op = float(det.get("fixed_fee") or 0)

        result = {
            "comissao_pct": comissao_pct,
            "frete_operacional": frete_op,
            "total_ml": total,
            "voce_recebe": round(price - total, 2),
            "source": "api"
        }

        # Salva cache
        cache[key] = {"ts": time.time(), "data": result}
        _save_cache(cache)
        return result

    except Exception as e:
        logger.error("Erro ao buscar taxa ML: %s", e)
        return _fallback_taxa(listing_type, price)


def _fallback_taxa(listing_type: str, price: float) -> dict:
    """Fallback com taxas conhecidas quando API não está disponível."""
    comissao = 0.12 if listing_type == "gold_special" else 0.17
    total = round(price * comissao, 2)
    return {
        "comissao_pct": comissao,
        "frete_operacional": 0,
        "total_ml": total,
        "voce_recebe": round(price - total, 2),
        "source": "fallback"
    }


def calcular_peso_volumetrico(largura_cm: float, altura_cm: float, profundidade_cm: float) -> float:
    """Calcula peso volumétrico em kg (L x A x P / 6000)."""
    return (largura_cm * altura_cm * profundidade_cm) / 6000


def calcular_peso_faturavel(peso_fisico_kg: float, largura_cm: float, altura_cm: float, profundidade_cm: float) -> int:
    """Retorna o maior entre peso físico e volumétrico em gramas."""
    vol_kg = calcular_peso_volumetrico(largura_cm, altura_cm, profundidade_cm)
    return int(max(peso_fisico_kg, vol_kg) * 1000)


def simular_preco_ml(
    custo_produto: float,
    embalagem: float,
    imposto_pct: float,
    peso_kg: float,
    largura_cm: float,
    altura_cm: float,
    profundidade_cm: float,
    objetivo: str,  # "markup", "lucro_liquido", "lucro_nominal"
    valor_alvo: float,
    listing_type: str = "gold_special",
    category_id: str = "",
    quantidade: int = 1
) -> dict:
    """
    Simula o preço ideal para o ML considerando todos os custos reais.
    Retorna o preço calculado e breakdown completo.
    """
    custo_base = (custo_produto * quantidade) + embalagem
    peso_g = calcular_peso_faturavel(peso_kg, largura_cm, altura_cm, profundidade_cm)
    imposto = imposto_pct / 100 if imposto_pct > 1 else imposto_pct

    # Estimativa inicial para buscar a taxa
    preco_estimado = custo_base * (1 + valor_alvo / 100) if objetivo == "markup" else custo_base * 2

    # Busca taxa real da API
    taxa = get_ml_taxa_real(listing_type, preco_estimado, peso_g, category_id)
    comissao = taxa["comissao_pct"]
    frete_op = taxa["frete_operacional"]

    # Calcula preço com a fórmula correta
    if objetivo == "markup":
        multiplicador = 1 + (valor_alvo / 100 if valor_alvo > 1 else valor_alvo)
        preco = ((custo_base * multiplicador) + frete_op) / max(1 - comissao - imposto, 0.0001)
    elif objetivo == "lucro_liquido":
        margem = valor_alvo / 100 if valor_alvo > 1 else valor_alvo
        preco = (custo_base + frete_op) / max(1 - comissao - imposto - margem, 0.0001)
    else:  # lucro_nominal
        lucro = valor_alvo
        preco = (custo_base + frete_op + lucro) / max(1 - comissao - imposto, 0.0001)

    preco = round(preco, 2)

    # Recalcula com o preço final para obter taxa exata
    taxa_final = get_ml_taxa_real(listing_type, preco, peso_g, category_id)
    total_ml = taxa_final["total_ml"]

    receita_liquida = preco - total_ml - imposto * preco
    lucro_bruto = receita_liquida - custo_base
    margem_real = lucro_bruto / preco if preco > 0 else 0

    return {
        "preco": preco,
        "custo_produto": custo_produto,
        "embalagem": embalagem,
        "custo_base": round(custo_base, 2),
        "peso_g": peso_g,
        "comissao_pct": round(comissao * 100, 1),
        "comissao_valor": round(preco * comissao, 2),
        "frete_operacional": frete_op,
        "total_ml": total_ml,
        "imposto_valor": round(preco * imposto, 2),
        "receita_liquida": round(receita_liquida, 2),
        "lucro_bruto": round(lucro_bruto, 2),
        "margem_real_pct": round(margem_real * 100, 1),
        "listing_type": listing_type,
        "source": taxa_final.get("source", "api"),
        "canal": "Mercado Livre Classico" if listing_type == "gold_special" else "Mercado Livre Premium"
    }


if __name__ == "__main__":
    # Teste com produto real
    resultado = simular_preco_ml(
        custo_produto=23.05,
        embalagem=0.50,
        imposto_pct=4.0,
        peso_kg=0.2,
        largura_cm=10,
        altura_cm=5,
        profundidade_cm=18,
        objetivo="markup",
        valor_alvo=33,
        listing_type="gold_special",
        category_id="MLB6284"
    )
    print(json.dumps(resultado, indent=2, ensure_ascii=False))
