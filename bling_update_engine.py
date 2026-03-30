
from __future__ import annotations

from typing import Any

from bling_client import BlingClient, BlingAPIError


def _normalizar_nome_loja(nome: str) -> str:
    n = (nome or "").strip().lower()
    n = n.replace("á", "a").replace("ã", "a").replace("â", "a")
    n = n.replace("é", "e").replace("ê", "e")
    n = n.replace("í", "i")
    n = n.replace("ó", "o").replace("õ", "o").replace("ô", "o")
    n = n.replace("ú", "u")
    return n


def _extrair_anuncios_por_loja(response: dict[str, Any]) -> dict[str, dict[str, Any]]:
    anuncios = {}
    for item in response.get("data", []):
        loja = item.get("loja") or {}
        nome_loja = _normalizar_nome_loja(loja.get("nome") or item.get("nomeLoja") or "")
        anuncios[nome_loja] = item
    return anuncios


def aplicar_precos_multicanal(client: BlingClient, item: dict[str, Any]) -> dict[str, Any]:
    produto_id = item.get("produto_id")
    if not produto_id:
        raise BlingAPIError("O item da fila não possui produto_id. Sem isso não dá para localizar anúncios no Bling.")

    anuncios_response = client.buscar_anuncios_por_produto(int(produto_id))
    anuncios_por_loja = _extrair_anuncios_por_loja(anuncios_response)

    marketplaces = item.get("marketplaces") or {}
    resultados: dict[str, Any] = {}

    # Mercado Livre: 1 anúncio, 2 modalidades
    ml_classico = marketplaces.get("mercado_livre_classico")
    ml_premium = marketplaces.get("mercado_livre_premium")
    anuncio_ml = anuncios_por_loja.get("mercado livre") or anuncios_por_loja.get("mercado livre full")
    if anuncio_ml and ml_classico and ml_premium:
        try:
            resultados["mercado_livre"] = client.atualizar_anuncio_ml_modalidades(
                anuncio_id=anuncio_ml.get("id"),
                preco_classico=ml_classico.get("preco", 0),
                preco_premium=ml_premium.get("preco", 0),
            )
        except Exception as exc:
            resultados["mercado_livre"] = {"erro": str(exc)}
    else:
        resultados["mercado_livre"] = {"erro": "Anúncio do Mercado Livre não localizado ou preços ausentes."}

    # Amazon
    amazon = marketplaces.get("amazon")
    anuncio_amazon = anuncios_por_loja.get("amazon")
    if anuncio_amazon and amazon:
        try:
            resultados["amazon"] = client.atualizar_anuncio_simples(
                anuncio_id=anuncio_amazon.get("id"),
                preco=amazon.get("preco", 0),
                preco_promocional=amazon.get("preco_promocional") or None,
            )
        except Exception as exc:
            resultados["amazon"] = {"erro": str(exc)}
    else:
        resultados["amazon"] = {"erro": "Anúncio Amazon não localizado ou preço ausente."}

    # Shopee
    shopee = marketplaces.get("shopee")
    anuncio_shopee = anuncios_por_loja.get("shopee")
    if anuncio_shopee and shopee:
        try:
            resultados["shopee"] = client.atualizar_anuncio_simples(
                anuncio_id=anuncio_shopee.get("id"),
                preco=shopee.get("preco", 0),
                preco_promocional=shopee.get("preco_promocional") or None,
            )
        except Exception as exc:
            resultados["shopee"] = {"erro": str(exc)}
    else:
        resultados["shopee"] = {"erro": "Anúncio Shopee não localizado ou preço ausente."}

    # Shein
    shein = marketplaces.get("shein")
    anuncio_shein = anuncios_por_loja.get("shein")
    if anuncio_shein and shein:
        try:
            resultados["shein"] = client.atualizar_anuncio_simples(
                anuncio_id=anuncio_shein.get("id"),
                preco=shein.get("preco", 0),
                preco_promocional=shein.get("preco_promocional") or None,
            )
        except Exception as exc:
            resultados["shein"] = {"erro": str(exc)}
    else:
        resultados["shein"] = {"erro": "Anúncio Shein não localizado ou preço ausente."}

    # Shopify: fallback pelo produto enquanto não houver endpoint de anúncio confirmado
    shopify = marketplaces.get("shopify")
    if shopify:
        try:
            resultados["shopify"] = client.atualizar_preco(
                produto_id=int(produto_id),
                novo_preco=shopify.get("preco", 0),
            )
        except Exception as exc:
            resultados["shopify"] = {"erro": str(exc)}
    else:
        resultados["shopify"] = {"erro": "Preço Shopify ausente."}

    return resultados
