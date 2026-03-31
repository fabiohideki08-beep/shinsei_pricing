from __future__ import annotations

from typing import Any, Optional


# ============================================================
# Helpers
# ============================================================
def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)

    txt = str(value).strip()
    txt = txt.replace("R$", "").replace("%", "").replace(" ", "")

    if "," in txt and "." in txt:
        txt = txt.replace(".", "").replace(",", ".")
    else:
        txt = txt.replace(",", ".")

    try:
        return float(txt)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _normalize_channel_key(canal: str) -> str:
    txt = str(canal or "").strip().lower()
    txt = txt.replace("á", "a").replace("ã", "a").replace("â", "a")
    txt = txt.replace("é", "e").replace("ê", "e")
    txt = txt.replace("í", "i")
    txt = txt.replace("ó", "o").replace("õ", "o").replace("ô", "o")
    txt = txt.replace("ú", "u")
    txt = txt.replace("ç", "c")
    txt = txt.replace("-", " ")
    txt = "_".join(txt.split())

    aliases = {
        "mercado_livre_classico": "mercado_livre_classico",
        "mercado_livre_clasico": "mercado_livre_classico",
        "mercado_livre_premium": "mercado_livre_premium",
        "shopee": "shopee",
        "amazon": "amazon",
        "shein": "shein",
        "shopify": "shopify",
        "shopfy": "shopify",
    }
    return aliases.get(txt, txt)


def _extract_product_id(payload: dict) -> Optional[int]:
    candidates = [
        payload.get("produto_id"),
        payload.get("id_produto"),
        payload.get("bling_id"),
        (payload.get("produto") or {}).get("id") if isinstance(payload.get("produto"), dict) else None,
        (payload.get("produto_bling") or {}).get("id") if isinstance(payload.get("produto_bling"), dict) else None,
        (payload.get("raw") or {}).get("produto_bling", {}).get("id") if isinstance(payload.get("raw"), dict) else None,
    ]
    for c in candidates:
        if c is None or c == "":
            continue
        try:
            return int(c)
        except Exception:
            continue
    return None


def _extract_product_meta(payload: dict) -> dict:
    produto = payload.get("produto") if isinstance(payload.get("produto"), dict) else {}
    produto_bling = payload.get("produto_bling") if isinstance(payload.get("produto_bling"), dict) else {}
    raw_produto = {}
    if isinstance(payload.get("raw"), dict):
        raw_produto = payload["raw"].get("produto_bling") or {}

    base = {}
    for src in [raw_produto, produto_bling, produto]:
        if isinstance(src, dict):
            base.update({k: v for k, v in src.items() if v not in (None, "")})

    return {
        "id": base.get("id"),
        "nome": base.get("nome"),
        "codigo": base.get("codigo") or base.get("sku"),
        "ean": base.get("ean") or base.get("gtin"),
        "preco": _safe_float(base.get("preco"), 0),
        "preco_custo": _safe_float(
            base.get("precoCusto") or base.get("preco_custo") or base.get("custo"), 0
        ),
    }


def _extract_marketplaces(payload: dict) -> dict[str, dict]:
    marketplaces = payload.get("marketplaces")
    if isinstance(marketplaces, dict) and marketplaces:
        return marketplaces

    itens = payload.get("itens")
    if not itens and isinstance(payload.get("raw"), dict):
        raw = payload["raw"]
        if isinstance(raw.get("integracao"), dict):
            itens = raw["integracao"].get("itens")
        if not itens:
            itens = raw.get("itens")
        if not itens and isinstance(raw.get("calculo"), dict):
            itens = raw["calculo"].get("canais")

    if not itens:
        itens = payload.get("canais")

    resultado: dict[str, dict] = {}
    if isinstance(itens, list):
        for item in itens:
            if not isinstance(item, dict):
                continue
            canal = item.get("canal") or item.get("label") or ""
            if not canal:
                continue
            key = _normalize_channel_key(canal)
            resultado[key] = {
                "label": canal,
                "preco": _safe_float(
                    item.get("preco_virtual")
                    or item.get("preco_cheio")
                    or item.get("preco_sugerido")
                    or item.get("preco")
                    or item.get("preco_final"),
                    0,
                ),
                "preco_promocional": _safe_float(
                    item.get("preco_promocional")
                    or item.get("promocional")
                    or item.get("preco_final"),
                    0,
                ),
                "raw": item,
            }
    return resultado


# ============================================================
# Build patch payload
# ============================================================
def _build_price_targets(payload: dict) -> dict:
    marketplaces = _extract_marketplaces(payload)
    produto = _extract_product_meta(payload)

    canais = []
    for canal_key, item in marketplaces.items():
        preco = _safe_float(item.get("preco"), 0)
        promo = _safe_float(item.get("preco_promocional"), 0)

        if preco <= 0 and promo > 0:
            preco = promo
        if promo <= 0 and preco > 0:
            promo = preco

        canais.append(
            {
                "canal": canal_key,
                "label": item.get("label") or canal_key,
                "preco": round(preco, 2),
                "preco_promocional": round(promo, 2),
                "raw": item.get("raw", {}),
            }
        )

    return {
        "produto": produto,
        "produto_id": _extract_product_id(payload),
        "canais": canais,
        "payload_original": payload,
    }


def _build_patch_payload_existing_product(existing: dict, canais: list[dict]) -> dict:
    """
    Atualiza preço base do produto usando o melhor preço disponível.
    Mantém campos existentes para não quebrar cadastro.
    """
    patch = dict(existing) if isinstance(existing, dict) else {}

    if "data" in patch and isinstance(patch["data"], dict):
        patch = dict(patch["data"])

    if not canais:
        return patch

    # Estratégia:
    # - usa o menor preço promocional/preço entre os canais como preço base seguro
    precos_validos = []
    for canal in canais:
        preco = _safe_float(canal.get("preco"), 0)
        promo = _safe_float(canal.get("preco_promocional"), 0)

        if promo > 0:
            precos_validos.append(promo)
        elif preco > 0:
            precos_validos.append(preco)

    if precos_validos:
        patch["preco"] = round(min(precos_validos), 2)

    # campos mínimos importantes
    patch["id"] = patch.get("id")
    patch["nome"] = patch.get("nome")
    patch["codigo"] = patch.get("codigo")

    return patch


# ============================================================
# API principal
# ============================================================
def aplicar_precos_multicanal(client: Any, payload: dict) -> dict:
    """
    Estratégia atual segura:
    - identifica o produto
    - lê o produto atual no Bling
    - atualiza o preço base do produto usando update_product()

    Observação:
    esta versão evita depender de métodos específicos de anúncios/marketplaces
    que não existem no bling_client atual.
    """
    if not isinstance(payload, dict):
        raise ValueError("Payload inválido para aplicação de preços.")

    targets = _build_price_targets(payload)
    produto_id = targets["produto_id"]
    canais = targets["canais"]
    produto = targets["produto"]

    if not produto_id:
        raise ValueError("Não foi possível identificar o ID do produto no Bling.")
    if not canais:
        raise ValueError("Nenhum preço de canal encontrado para aplicar.")

    existing = client.get_product(int(produto_id))
    patch = _build_patch_payload_existing_product(existing, canais)
    response = client.update_product(int(produto_id), patch)

    return {
        "ok": True,
        "estrategia": "update_product_preco_base",
        "produto_id": produto_id,
        "produto": produto,
        "payload_enviado": patch,
        "canais_recebidos": canais,
        "resultado_api": response,
    }