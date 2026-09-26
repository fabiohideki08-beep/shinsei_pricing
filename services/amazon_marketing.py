"""
services/amazon_marketing.py — Shinsei Pricing
Análise de viabilidade para cobrir a Buy Box da Amazon.

Busca todos os produtos do inventário FBA, obtém o preço da Buy Box via
Competitive Pricing API e calcula se cobrir esse preço é viável dado o
custo do produto no Bling e as regras de precificação.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_PATH = DATA_DIR / "amazon_marketing_cache.json"


def _salvar_cache_amazon(dados: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    dados_com_ts = {**dados, "cached_at": datetime.utcnow().isoformat()}
    CACHE_PATH.write_text(json.dumps(dados_com_ts, ensure_ascii=False), encoding="utf-8")


def carregar_cache_amazon() -> Optional[dict]:
    if not CACHE_PATH.exists():
        return None
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None

SP_API_BASE = "https://sellingpartnerapi-na.amazon.com"


# ── helpers ───────────────────────────────────────────────────────────────────

def _extrair_precos_competitivos(competitive_data: dict) -> dict:
    """
    Extrai preços competitivos da resposta de GET /products/pricing/v0/competitivePrice.
    Retorna dict com:
      - buy_box: preço da Oferta em Destaque (CompetitivePriceId == "1")
      - menor_preco: menor preço listado (CompetitivePriceId == "2")
      - buy_box_sua: True se o vendedor logado detém a Buy Box
    """
    resultado = {"buy_box": None, "menor_preco": None, "buy_box_sua": None}
    try:
        payload = competitive_data.get("payload") or competitive_data
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            product = item.get("Product") or item
            prices = (
                product.get("CompetitivePricing", {})
                .get("CompetitivePrices", [])
            )
            for cp in prices:
                pid = str(cp.get("CompetitivePriceId", "") or cp.get("CompetitivePriceID", ""))
                landed = cp.get("Price", {}).get("LandedPrice", {})
                val = landed.get("Amount")
                if val is None:
                    continue
                val = round(float(val), 2)
                if pid == "1" and resultado["buy_box"] is None:
                    resultado["buy_box"] = val
                    resultado["buy_box_sua"] = bool(cp.get("belongsToRequester"))
                elif pid == "2" and resultado["menor_preco"] is None:
                    resultado["menor_preco"] = val
    except Exception as e:
        logger.debug("_extrair_precos_competitivos erro: %s", e)
    return resultado


def _extrair_buy_box(competitive_data: dict) -> Optional[float]:
    """Compatibilidade — retorna apenas o preço da Buy Box."""
    return _extrair_precos_competitivos(competitive_data)["buy_box"]


def _buscar_precos_batch(client, asins: list[str]) -> dict[str, dict]:
    """
    Busca preços competitivos para até 20 ASINs.
    Retorna {asin: {buy_box, menor_preco}}.
    """
    resultado: dict[str, dict] = {}
    if not asins:
        return resultado
    marketplace_id = client.config.get("marketplace_id", "")
    chunk = asins[:20]
    for tentativa in range(3):
        try:
            res = client._get("/products/pricing/v0/competitivePrice", {
                "MarketplaceId": marketplace_id,
                "Asins": ",".join(chunk),
                "ItemType": "Asin",
            })
            payload = res.get("payload", [])
            if not isinstance(payload, list):
                payload = [payload]
            for item in payload:
                asin = (item.get("ASIN") or "").strip()
                if asin:
                    precos = _extrair_precos_competitivos(item)
                    resultado[asin] = precos  # includes buy_box_sua
            # Log amostra para debug
            if payload:
                logger.info("Amostra payload pricing: %s", str(payload[0])[:600])
            return resultado
        except Exception as e:
            resp = getattr(e, "response", None)
            status = resp.status_code if resp is not None else 0
            if status == 429:
                espera = 10 * (tentativa + 1)
                logger.warning("Preços batch 429 — aguardando %ds", espera)
                time.sleep(espera)
            else:
                logger.warning("Erro preços batch %s: %s", chunk[:3], e)
                break
    for a in chunk:
        if a not in resultado:
            resultado[a] = {"buy_box": None, "menor_preco": None}
    return resultado


def _buscar_buy_box_batch(client, asins: list[str]) -> dict[str, Optional[float]]:
    """Compatibilidade — retorna apenas {asin: buy_box}."""
    return {asin: v["buy_box"] for asin, v in _buscar_precos_batch(client, asins).items()}


def _buscar_sku_data_batch(client, skus: list[str], tentativas: int = 3) -> dict[str, dict]:
    """
    Consulta Competitive Pricing API por SKU (ItemType=Sku).
    Retorna {sku: {asin, buy_box}} — ASIN é extraído da resposta mesmo sem Buy Box.
    Faz retry automático em 429 com backoff.
    """
    resultado: dict[str, dict] = {}
    if not skus:
        return resultado

    marketplace_id = client.config.get("marketplace_id", "")
    chunk = skus[:20]

    for tentativa in range(tentativas):
        try:
            res = client._get("/products/pricing/v0/competitivePrice", {
                "MarketplaceId": marketplace_id,
                "Skus": ",".join(chunk),
                "ItemType": "Sku",
            })
            payload = res.get("payload", [])
            if not isinstance(payload, list):
                payload = [payload]
            for item in payload:
                sku = (item.get("SellerSKU") or "").strip()
                asin = (item.get("ASIN") or "").strip()
                buy_box = _extrair_buy_box(item)
                if sku:
                    resultado[sku] = {"asin": asin, "buy_box": buy_box}
            return resultado
        except Exception as e:
            resp = getattr(e, "response", None)
            status = resp.status_code if resp is not None else 0
            if status == 429:
                espera = 10 * (tentativa + 1)
                logger.warning("SKU data batch 429 — aguardando %ds (tentativa %d/%d)", espera, tentativa + 1, tentativas)
                time.sleep(espera)
            else:
                body = resp.text[:300] if resp is not None else ""
                logger.warning("Erro SKU data batch %s: %s | %s", chunk[:3], e, body)
                break

    for s in chunk:
        if s not in resultado:
            resultado[s] = {"asin": "", "buy_box": None}
    return resultado


def _calcular_margem_amazon(preco: float, custo: float, peso: float,
                             imposto_pct: float, regras: list) -> dict:
    """
    Calcula margem para um preço dado na Amazon.
    Usa as mesmas regras de precificação do sistema.
    """
    from pricing_engine_real import _achar_regra, _pct_excel, _round2
    canal = "Amazon"
    try:
        regra = _achar_regra(regras, canal, peso, preco)
    except ValueError as e:
        return {"ok": False, "erro": str(e)}

    try:
        comissao_pct = regra["comissao"]
        taxa_frete   = regra["taxa_frete"]
        taxa_fixa    = regra["taxa_fixa"]
        imposto      = _pct_excel(imposto_pct)

        comissao_valor  = _round2(preco * comissao_pct)
        receita_liquida = _round2(preco - taxa_frete - taxa_fixa - comissao_valor)
        lucro_bruto     = _round2(receita_liquida - custo)
        imposto_valor   = _round2(preco * imposto)
        lucro_liquido   = _round2(lucro_bruto - imposto_valor)
        margem          = _round2((lucro_liquido / preco) * 100) if preco else 0

        return {
            "ok": True,
            "preco": preco,
            "custo": custo,
            "comissao_valor": comissao_valor,
            "taxa_frete": taxa_frete,
            "taxa_fixa": taxa_fixa,
            "receita_liquida": receita_liquida,
            "lucro_bruto": lucro_bruto,
            "imposto_valor": imposto_valor,
            "lucro_liquido": lucro_liquido,
            "margem": margem,
        }
    except Exception as e:
        logger.warning("_calcular_margem_amazon erro: %s", e)
        return {"ok": False, "erro": str(e)}


def _buscar_asins_por_ean(client, eans: list[str]) -> dict[str, str]:
    """
    Busca ASINs para uma lista de EANs via Catalog Items API.
    Retorna dict {ean: asin} — só para os EANs encontrados.
    """
    resultado: dict[str, str] = {}
    if not eans:
        return resultado

    marketplace_id = client.config.get("marketplace_id", "")
    chunk = eans[:20]

    try:
        res = client._get("/catalog/2022-04-01/items", {
            "marketplaceIds": marketplace_id,
            "identifiers": ",".join(chunk),   # comma-separated, NOT repeated params
            "identifiersType": "EAN",
            "includedData": "identifiers",
            "pageSize": len(chunk),
        })
        items = res.get("items", [])
        logger.debug("Catalog EAN batch %d EANs → %d items", len(chunk), len(items))
        for item in items:
            if not isinstance(item, dict):
                continue
            asin = item.get("asin", "")
            if not asin:
                continue
            # Mapeia cada EAN do item de volta para o ASIN
            for id_group in (item.get("identifiers") or []):
                if not isinstance(id_group, dict):
                    continue
                for id_item in (id_group.get("identifiers") or []):
                    if not isinstance(id_item, dict):
                        continue
                    if id_item.get("identifierType") in ("EAN", "GTIN"):
                        ean_found = (id_item.get("identifier") or "").lstrip("0") or id_item.get("identifier", "")
                        # Tenta match com e sem zeros à esquerda
                        for candidate in (id_item.get("identifier", ""), ean_found):
                            if candidate in chunk:
                                resultado[candidate] = asin
    except Exception as e:
        resp = getattr(e, "response", None)
        body = resp.text[:400] if resp is not None else ""
        logger.warning("Erro Catalog EAN batch: %s | %s", e, body)

    return resultado


def analisar_buy_box_amazon(
    bling_client,
    regras: list,
    margem_alvo: float = 20.0,
    imposto_padrao: float = 12.0,
    max_itens: int = 500,
) -> dict:
    """
    Análise principal: busca produtos do Bling, obtém Buy Box da Amazon por SKU
    e calcula viabilidade.

    Estratégia:
    1. Lista produtos do Bling (com custo e SKU)
    2. Para cada SKU, busca Buy Box via Competitive Pricing API (ItemType=Sku)
    3. Calcula margem ao cobrir o preço da Buy Box

    Retorna dict com:
    - resumo: contagens por categoria
    - dentro_margem: lista de itens viáveis
    - lucro_reduzido: lista com lucro mas abaixo da meta
    - prejuizo: lista que daria prejuízo
    - sem_dados: lista sem custo no Bling ou sem Buy Box
    """
    from amazon_client import AmazonClient
    try:
        amazon = AmazonClient()
    except Exception as e:
        return {"ok": False, "erro": f"Amazon não configurado: {e}"}

    if not bling_client:
        return {"ok": False, "erro": "Bling não configurado"}

    # 1. Lista produtos do Bling com custo e SKU
    logger.info("Amazon marketing: buscando produtos do Bling...")
    from pricing_engine_real import montar_precificacao_bling

    # Coleta todos os SKUs do Bling em páginas
    skus_bling: list[dict] = []  # [{sku, nome}]
    page = 1
    while len(skus_bling) < max_itens:
        try:
            res = bling_client.list_products(page=page, limit=100)
            items = res.get("data", [])
            if not items:
                break
            for item in items:
                prod = item if isinstance(item, dict) else {}
                prod = prod.get("produto", prod)
                sku = (prod.get("codigo") or "").strip()
                nome = (prod.get("descricao") or sku)[:70]
                if sku:
                    skus_bling.append({"sku": sku, "nome": nome})
            if len(items) < 100:
                break
            page += 1
            time.sleep(0.2)
        except Exception as e:
            logger.warning("Erro listando Bling página %d: %s", page, e)
            break

    logger.info("Amazon marketing: %d SKUs encontrados no Bling", len(skus_bling))

    if not skus_bling:
        return {"ok": False, "erro": "Nenhum produto encontrado no Bling"}

    todos_skus = [s["sku"] for s in skus_bling]

    # 2. Monta mapa SKU → ASIN via Listings API individual
    # Para cada SKU do Bling, consulta GET /listings/.../items/{sellerId}/{sku}
    # que retorna o ASIN do anúncio independente de ter estoque ou não (FBM puro).
    sku_asin_map: dict[str, str] = {}

    import urllib.parse
    logger.info("Amazon marketing: vinculando %d SKUs via Listings API individual...", len(todos_skus))
    for sku in todos_skus:
        try:
            sku_enc = urllib.parse.quote(sku, safe="")
            res_lst = amazon._get(
                f"/listings/2021-08-01/items/{amazon.config['seller_id']}/{sku_enc}",
                {"marketplaceIds": amazon.config["marketplace_id"], "includedData": "summaries"}
            )
            for s in (res_lst.get("summaries") or []):
                asin = (s.get("asin") or "").strip()
                if asin:
                    sku_asin_map[sku] = asin
                    break
            time.sleep(0.1)
        except Exception:
            pass  # SKU não existe na Amazon — normal

    logger.info("Amazon marketing: %d/%d SKUs vinculados a ASIN via Listings API",
                len(sku_asin_map), len(todos_skus))

    # 2b. EAN fallback: Catalog Items API para SKUs não vinculados via Listings
    skus_sem_asin = [s["sku"] for s in skus_bling if s["sku"] not in sku_asin_map]
    if skus_sem_asin:
        logger.info("Amazon marketing: %d SKUs sem ASIN — tentando fallback via EAN...", len(skus_sem_asin))
        ean_sku_map: dict[str, str] = {}  # {ean: sku}
        skus_sem_asin_set = set(skus_sem_asin)
        page_ean = 1
        while True:
            try:
                res_ean = bling_client.list_products(page=page_ean, limit=100)
                items_ean = res_ean.get("data", [])
                if not items_ean:
                    break
                for item_ean in items_ean:
                    prod = item_ean if isinstance(item_ean, dict) else {}
                    prod = prod.get("produto", prod)
                    sku_ean = (prod.get("codigo") or "").strip()
                    if sku_ean not in skus_sem_asin_set:
                        continue
                    gtin = (prod.get("gtin") or "").strip()
                    if gtin and len(gtin) >= 8:
                        ean_sku_map[gtin] = sku_ean
                if len(items_ean) < 100:
                    break
                page_ean += 1
                time.sleep(0.2)
            except Exception as e:
                logger.warning("EAN fallback erro página %d: %s", page_ean, e)
                break

        if ean_sku_map:
            logger.info("Amazon marketing: %d EANs coletados para fallback", len(ean_sku_map))
            eans_list = list(ean_sku_map.keys())
            for i in range(0, len(eans_list), 20):
                chunk_eans = eans_list[i:i+20]
                mapa_ean = _buscar_asins_por_ean(amazon, chunk_eans)
                for ean_found, asin_found in mapa_ean.items():
                    sku_alvo = ean_sku_map.get(ean_found)
                    if sku_alvo and sku_alvo not in sku_asin_map:
                        sku_asin_map[sku_alvo] = asin_found
                if i + 20 < len(eans_list):
                    time.sleep(1)
            novos_ean = sum(1 for s in skus_sem_asin if s in sku_asin_map)
            logger.info("Amazon marketing: EAN fallback encontrou %d ASINs adicionais", novos_ean)

    logger.info("Amazon marketing: %d/%d SKUs com ASIN (após EAN fallback)",
                len(sku_asin_map), len(todos_skus))

    # 3. Busca preços competitivos por ASIN (Buy Box + Menor Preço)
    asins_unicos = list(set(sku_asin_map.values()))
    logger.info("Amazon marketing: buscando preços para %d ASINs únicos...", len(asins_unicos))

    asin_precos_map: dict[str, dict] = {}
    for i in range(0, len(asins_unicos), 20):
        chunk = asins_unicos[i:i+20]
        resultado_chunk = _buscar_precos_batch(amazon, chunk)
        asin_precos_map.update(resultado_chunk)
        if i + 20 < len(asins_unicos):
            time.sleep(2)

    # Monta mapa final: sku → {asin, buy_box, menor_preco, buy_box_sua}
    sku_data_map: dict[str, dict] = {}
    for sku in todos_skus:
        asin = sku_asin_map.get(sku, "")
        precos = asin_precos_map.get(asin, {}) if asin else {}
        sku_data_map[sku] = {
            "asin": asin,
            "buy_box": precos.get("buy_box"),
            "menor_preco": precos.get("menor_preco"),
            "buy_box_sua": precos.get("buy_box_sua"),
        }

    skus_com_asin   = sum(1 for v in sku_data_map.values() if v.get("asin"))
    skus_com_buybox = sum(1 for v in sku_data_map.values() if v.get("buy_box") is not None)
    logger.info("Amazon marketing: %d/%d SKUs com ASIN | %d com Buy Box",
                skus_com_asin, len(todos_skus), skus_com_buybox)

    # 3. Calcula margens cruzando com Bling
    dentro_margem = []
    lucro_reduzido = []
    prejuizo = []
    sem_dados = []
    nao_encontrado_amazon = []

    nome_map = {s["sku"]: s["nome"] for s in skus_bling}

    for sku in todos_skus:
        dados_amz     = sku_data_map.get(sku, {})
        asin          = dados_amz.get("asin", "")
        preco_buy_box = dados_amz.get("buy_box")
        menor_preco   = dados_amz.get("menor_preco")
        buy_box_sua   = dados_amz.get("buy_box_sua")
        nome          = nome_map.get(sku, sku)

        # SKU do Bling sem correspondência na Amazon
        if not asin:
            nao_encontrado_amazon.append({"sku": sku, "titulo": nome})
            continue

        # Busca custo no Bling
        try:
            resultado_bling = montar_precificacao_bling(
                regras=regras,
                criterio="sku",
                valor_busca=sku,
                embalagem=0,
                imposto=imposto_padrao,
                quantidade=1,
                objetivo="margem",
                tipo_alvo="percentual",
                valor_alvo=margem_alvo,
            )
        except Exception as e:
            logger.debug("Bling erro SKU=%s: %s", sku, e)
            resultado_bling = {"erro": str(e)}

        if not isinstance(resultado_bling, dict) or resultado_bling.get("erro"):
            motivo = resultado_bling.get("erro", "sem custo Bling") if isinstance(resultado_bling, dict) else "sem custo Bling"
            sem_dados.append({
                "sku": sku, "asin": asin, "titulo": nome, "estoque": 0,
                "preco_buy_box": preco_buy_box, "menor_preco": menor_preco,
                "motivo": motivo,
            })
            continue

        custo = float(resultado_bling.get("custo_extraido", {}).get("custo_total") or 0)
        peso = float(resultado_bling.get("peso_extraido", {}).get("peso") or 0)
        imposto_prod = float(resultado_bling.get("imposto_usado") or imposto_padrao)

        if custo <= 0:
            sem_dados.append({
                "sku": sku, "asin": asin, "titulo": nome, "estoque": 0,
                "preco_buy_box": preco_buy_box, "menor_preco": menor_preco,
                "motivo": "custo zero no Bling",
            })
            continue

        if not preco_buy_box and not menor_preco:
            sem_dados.append({
                "sku": sku, "asin": asin, "titulo": nome, "estoque": 0,
                "preco_buy_box": None, "menor_preco": None,
                "custo": round(custo, 2),
                "motivo": "sem Buy Box/Menor Preço disponível",
            })
            continue

        # Calcula margem para os dois preços de referência
        def _calc(preco):
            if not preco:
                return None
            r = _calcular_margem_amazon(preco, custo, peso, imposto_prod, regras)
            return r if r.get("ok") else None

        r_bb  = _calc(preco_buy_box)
        r_mp  = _calc(menor_preco)

        # Usa Buy Box como referência principal; menor preço como secundário
        r_ref = r_bb or r_mp
        if not r_ref:
            sem_dados.append({
                "sku": sku, "asin": asin, "titulo": nome, "estoque": 0,
                "preco_buy_box": preco_buy_box, "menor_preco": menor_preco,
                "custo": round(custo, 2),
                "motivo": r_bb.get("erro", "erro cálculo") if r_bb else "sem regra para Amazon",
            })
            continue

        margem = r_ref.get("margem", 0)
        lucro  = r_ref.get("lucro_liquido", 0)

        entry = {
            "sku": sku,
            "asin": asin,
            "titulo": nome,
            "estoque": 0,
            "preco_buy_box": preco_buy_box,
            "menor_preco": menor_preco,
            "buy_box_sua": buy_box_sua,
            "custo": round(custo, 2),
            "peso": peso,
            # Buy Box
            "margem": round(r_bb["margem"], 2) if r_bb else None,
            "lucro": round(r_bb["lucro_liquido"], 2) if r_bb else None,
            # Menor preço
            "margem_menor": round(r_mp["margem"], 2) if r_mp else None,
            "lucro_menor": round(r_mp["lucro_liquido"], 2) if r_mp else None,
        }

        if margem >= margem_alvo:
            dentro_margem.append(entry)
        elif margem > 0:
            lucro_reduzido.append(entry)
        else:
            prejuizo.append(entry)

    # Ordena por margem decrescente
    dentro_margem.sort(key=lambda x: x["margem"], reverse=True)
    lucro_reduzido.sort(key=lambda x: x["margem"], reverse=True)
    prejuizo.sort(key=lambda x: x["margem"])

    total = len(dentro_margem) + len(lucro_reduzido) + len(prejuizo) + len(sem_dados)

    logger.info(
        "Amazon marketing: dentro=%d lucro_reduzido=%d prejuizo=%d sem_dados=%d nao_encontrado=%d",
        len(dentro_margem), len(lucro_reduzido), len(prejuizo), len(sem_dados), len(nao_encontrado_amazon)
    )

    resultado_final = {
        "ok": True,
        "total_analisados": total,
        "total_bling": len(todos_skus),
        "resumo": {
            "dentro_margem": len(dentro_margem),
            "lucro_reduzido": len(lucro_reduzido),
            "prejuizo": len(prejuizo),
            "sem_dados": len(sem_dados),
            "nao_encontrado_amazon": len(nao_encontrado_amazon),
        },
        "dentro_margem": dentro_margem,
        "lucro_reduzido": lucro_reduzido,
        "prejuizo": prejuizo,
        "sem_dados": sem_dados,
        "nao_encontrado_amazon": nao_encontrado_amazon,
    }

    try:
        _salvar_cache_amazon(resultado_final)
    except Exception as e:
        logger.warning("Erro salvando cache Amazon: %s", e)

    return resultado_final


def atualizar_preco_amazon(sku: str, novo_preco: float) -> dict:
    """Atualiza preço de um item na Amazon."""
    try:
        from services.amazon import AmazonService
        svc = AmazonService()
        return svc.atualizar_com_retry(sku, novo_preco)
    except Exception as e:
        logger.warning("Erro atualizar preco Amazon SKU=%s: %s", sku, e)
        return {"ok": False, "erro": str(e)}
