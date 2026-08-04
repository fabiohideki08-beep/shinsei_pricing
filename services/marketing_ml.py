"""
services/marketing_ml.py — Shinsei Pricing
Analise de viabilidade de campanhas/promocoes do ML.

Dado um desconto percentual, classifica todos os anuncios ativos em:
  - dentro_margem   : margem campanha >= margem_alvo
  - lucro_reduzido  : 0 < margem campanha < margem_alvo
  - prejuizo        : margem campanha <= 0
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

ML_API = "https://api.mercadolibre.com"

LISTING_TYPE_CANAL = {
    "gold_pro": "Mercado Livre Premium",
    "gold_special": "Mercado Livre Classico",
    "gold": "Mercado Livre Classico",
    "bronze": "Mercado Livre Classico",
    "silver": "Mercado Livre Classico",
    "free": "Mercado Livre Classico",
}


def _ml_token() -> str:
    from pathlib import Path
    import json
    p = Path(__file__).resolve().parent.parent / "data" / "ml_tokens.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8")).get("access_token", "")
    return ""


def _ml_headers() -> dict:
    return {"Authorization": f"Bearer {_ml_token()}"}


def _buscar_ids_ativos(seller_id: str, limit: int = 1000) -> list[str]:
    """Retorna lista de item_ids ativos do vendedor."""
    ids = []
    offset = 0
    while True:
        url = f"{ML_API}/users/{seller_id}/items/search"
        params = {"status": "active", "limit": 100, "offset": offset}
        try:
            res = requests.get(url, headers=_ml_headers(), params=params, timeout=20)
            if res.status_code != 200:
                break
            data = res.json()
            batch = data.get("results", [])
            ids.extend(batch)
            if len(ids) >= limit or len(batch) < 100:
                break
            offset += 100
            time.sleep(0.3)
        except Exception as e:
            logger.warning("Erro ao buscar ids ML: %s", e)
            break
    return ids[:limit]


def _extrair_sku(item: dict) -> str:
    """Extrai SKU do anuncio: tenta seller_sku, seller_custom_field e attributes[SELLER_SKU]."""
    sku = (item.get("seller_sku") or "").strip()
    if sku:
        return sku
    sku = (item.get("seller_custom_field") or "").strip()
    if sku:
        return sku
    for attr in item.get("attributes") or []:
        if str(attr.get("id") or "").upper() == "SELLER_SKU":
            sku = (attr.get("value_name") or attr.get("values", [{}])[0].get("name") or "").strip()
            if sku:
                return sku
    return ""


def _buscar_detalhes_batch(ids: list[str]) -> list[dict]:
    """Busca detalhes de ate 20 itens por vez via multiget."""
    resultados = []
    for i in range(0, len(ids), 20):
        batch = ids[i:i + 20]
        try:
            res = requests.get(
                f"{ML_API}/items",
                headers=_ml_headers(),
                params={"ids": ",".join(batch), "attributes": "id,title,price,seller_sku,seller_custom_field,listing_type_id,status,attributes"},
                timeout=20,
            )
            if res.status_code == 200:
                for entry in res.json():
                    item = entry.get("body") or {}
                    if item.get("status") == "active":
                        item["_sku"] = _extrair_sku(item)
                        resultados.append(item)
            time.sleep(0.3)
        except Exception as e:
            logger.warning("Erro multiget ML: %s", e)
    return resultados


def _calcular_margem_preco(preco: float, custo: float, peso: float, imposto_pct: float,
                            canal: str, regras: list) -> dict:
    """Dado preco e custo, retorna margem e demais indicadores para o canal."""
    from pricing_engine_real import _achar_regra, _pct_excel, _round2
    try:
        regra = _achar_regra(regras, canal, peso, preco)
    except ValueError:
        return {"ok": False, "erro": "sem_regra"}

    comissao_pct = regra["comissao"]
    taxa_frete = regra["taxa_frete"]
    taxa_fixa = regra["taxa_fixa"]
    imposto = _pct_excel(imposto_pct)

    comissao_valor = _round2(preco * comissao_pct)
    receita_liquida = _round2(preco - taxa_frete - taxa_fixa - comissao_valor)
    lucro_bruto = _round2(receita_liquida - custo)
    imposto_valor = _round2(preco * imposto)
    lucro_liquido = _round2(lucro_bruto - imposto_valor)
    margem = _round2((lucro_liquido / preco) * 100) if preco else 0

    return {
        "ok": True,
        "preco": _round2(preco),
        "custo": _round2(custo),
        "comissao_valor": comissao_valor,
        "taxa_frete": taxa_frete,
        "taxa_fixa": taxa_fixa,
        "receita_liquida": receita_liquida,
        "lucro_bruto": lucro_bruto,
        "lucro_liquido": lucro_liquido,
        "margem": margem,
        "imposto_valor": imposto_valor,
        "faixa": f"{regra.get('peso_min',0):g}-{regra.get('peso_max',0):g}kg",
    }


def analisar_campanhas_ml(
    seller_id: str,
    bling_client: Any,
    desconto_pct: float,
    margem_alvo: float,
    imposto_padrao: float = 12.0,
    max_itens: int = 500,
    callback=None,
) -> dict:
    """
    Analisa viabilidade de participar de campanhas ML com desconto X%.

    Retorna dict com listas:
      dentro_margem, lucro_reduzido, prejuizo, sem_dados
    """
    from app import carregar_regras

    regras = carregar_regras(apenas_ativas=True)
    if not regras:
        return {"ok": False, "erro": "Nenhuma regra de precificacao cadastrada."}

    ids = _buscar_ids_ativos(seller_id, limit=max_itens)
    if not ids:
        return {"ok": False, "erro": "Nenhum anuncio ativo encontrado no ML."}

    itens_ml = _buscar_detalhes_batch(ids)

    dentro_margem = []
    lucro_reduzido = []
    prejuizo = []
    sem_dados = []

    total = len(itens_ml)
    for idx, item in enumerate(itens_ml):
        if callback:
            callback(idx + 1, total)

        sku = item.get("_sku") or (item.get("seller_sku") or "").strip()
        titulo = item.get("title", "")[:60]
        preco_atual = float(item.get("price") or 0)
        listing_type = item.get("listing_type_id", "gold_special")
        canal = LISTING_TYPE_CANAL.get(listing_type, "Mercado Livre Premium")
        item_id = item.get("id", "")

        if not sku or preco_atual <= 0:
            sem_dados.append({
                "item_id": item_id, "titulo": titulo, "sku": sku or "sem SKU",
                "motivo": "sem SKU ou preco zerado",
            })
            continue

        # Usa montar_precificacao_bling para extrair custo (suporta composicoes)
        try:
            from pricing_engine_real import montar_precificacao_bling
            resultado = montar_precificacao_bling(
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
            if resultado.get("erro"):
                sem_dados.append({
                    "item_id": item_id, "titulo": titulo, "sku": sku,
                    "motivo": resultado.get("erro", "erro no pricing engine"),
                })
                continue
            custo = float(resultado.get("custo_extraido", {}).get("custo_total") or 0)
            peso = float(resultado.get("peso_extraido", {}).get("peso") or 0)
            imposto_prod = float(resultado.get("imposto_usado") or imposto_padrao)
        except Exception as e:
            logger.warning("Erro pricing engine SKU %s: %s", sku, e)
            sem_dados.append({
                "item_id": item_id, "titulo": titulo, "sku": sku,
                "motivo": f"erro pricing engine: {e}",
            })
            continue

        if custo <= 0:
            sem_dados.append({
                "item_id": item_id, "titulo": titulo, "sku": sku,
                "motivo": "custo zerado ou nao cadastrado no Bling",
            })
            continue
        imposto = imposto_prod

        # Margem ao preco atual
        margem_atual_info = _calcular_margem_preco(preco_atual, custo, peso, imposto, canal, regras)

        # Preco e margem com o desconto da campanha
        preco_campanha = round(preco_atual * (1 - desconto_pct / 100), 2)
        margem_camp_info = _calcular_margem_preco(preco_campanha, custo, peso, imposto, canal, regras)

        if not margem_camp_info.get("ok"):
            sem_dados.append({
                "item_id": item_id, "titulo": titulo, "sku": sku,
                "motivo": "sem regra de precificacao para este canal/peso/preco",
            })
            continue

        row = {
            "item_id": item_id,
            "sku": sku,
            "titulo": titulo,
            "canal": canal,
            "listing_type": listing_type,
            "preco_atual": preco_atual,
            "preco_campanha": preco_campanha,
            "margem_atual": margem_atual_info.get("margem", 0) if margem_atual_info.get("ok") else None,
            "margem_campanha": margem_camp_info["margem"],
            "lucro_campanha": margem_camp_info["lucro_liquido"],
            "variacao_margem": round(
                margem_camp_info["margem"] - (margem_atual_info.get("margem", 0) if margem_atual_info.get("ok") else 0),
                2,
            ),
            "comissao": margem_camp_info["comissao_valor"],
            "frete": margem_camp_info["taxa_frete"],
            "custo": custo,
            "desconto_pct": desconto_pct,
        }

        m = margem_camp_info["margem"]
        if m >= margem_alvo:
            dentro_margem.append(row)
        elif m > 0:
            lucro_reduzido.append(row)
        else:
            prejuizo.append(row)

        time.sleep(0.1)

    # Ordena cada grupo por margem desc
    for lst in (dentro_margem, lucro_reduzido):
        lst.sort(key=lambda x: x["margem_campanha"], reverse=True)
    prejuizo.sort(key=lambda x: x["margem_campanha"])

    return {
        "ok": True,
        "total_analisados": len(itens_ml),
        "desconto_pct": desconto_pct,
        "margem_alvo": margem_alvo,
        "dentro_margem": dentro_margem,
        "lucro_reduzido": lucro_reduzido,
        "prejuizo": prejuizo,
        "sem_dados": sem_dados,
        "resumo": {
            "dentro_margem": len(dentro_margem),
            "lucro_reduzido": len(lucro_reduzido),
            "prejuizo": len(prejuizo),
            "sem_dados": len(sem_dados),
        },
    }


def atualizar_preco_ml(item_id: str, novo_preco: float) -> dict:
    """Atualiza preco de um anuncio ML."""
    try:
        res = requests.put(
            f"{ML_API}/items/{item_id}",
            json={"price": round(novo_preco, 2)},
            headers=_ml_headers(),
            timeout=15,
        )
        if res.status_code == 200:
            return {"ok": True, "item_id": item_id, "preco": novo_preco}
        return {"ok": False, "item_id": item_id, "erro": res.text[:200]}
    except Exception as e:
        return {"ok": False, "item_id": item_id, "erro": str(e)}
