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


def _has_method(obj: Any, name: str) -> bool:
    return hasattr(obj, name) and callable(getattr(obj, name))


# ============================================================
# Mapeamento de preços por canal
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


# ============================================================
# Estratégias de update
# ============================================================
def _try_update_direct_methods(client: Any, produto_id: int, canais: list[dict]) -> list[dict]:
    """
    Tenta métodos específicos do cliente, se existirem.
    """
    resultados = []

    for canal in canais:
        canal_key = canal["canal"]
        preco = canal["preco"]
        promo = canal["preco_promocional"]

        sucesso = False
        detalhe = None

        attempts = [
            ("update_product_price_marketplace", {"product_id": produto_id, "marketplace": canal_key, "price": preco, "promotional_price": promo}),
            ("update_marketplace_price", {"product_id": produto_id, "marketplace": canal_key, "price": preco, "promotional_price": promo}),
            ("set_marketplace_price", {"product_id": produto_id, "marketplace": canal_key, "price": preco, "promotional_price": promo}),
            ("update_channel_price", {"product_id": produto_id, "channel": canal_key, "price": preco, "promotional_price": promo}),
        ]

        for method_name, kwargs in attempts:
            if not _has_method(client, method_name):
                continue
            try:
                response = getattr(client, method_name)(**kwargs)
                resultados.append(
                    {
                        "canal": canal_key,
                        "preco": preco,
                        "preco_promocional": promo,
                        "metodo": method_name,
                        "ok": True,
                        "response": response,
                    }
                )
                sucesso = True
                break
            except TypeError:
                try:
                    response = getattr(client, method_name)(produto_id, canal_key, preco, promo)
                    resultados.append(
                        {
                            "canal": canal_key,
                            "preco": preco,
                            "preco_promocional": promo,
                            "metodo": method_name,
                            "ok": True,
                            "response": response,
                        }
                    )
                    sucesso = True
                    break
                except Exception as exc:
                    detalhe = str(exc)
            except Exception as exc:
                detalhe = str(exc)

        if not sucesso:
            resultados.append(
                {
                    "canal": canal_key,
                    "preco": preco,
                    "preco_promocional": promo,
                    "ok": False,
                    "erro": detalhe or "Nenhum método direto compatível encontrado.",
                }
            )

    return resultados


def _build_patch_payload_existing_product(existing: dict, canais: list[dict]) -> dict:
    """
    Monta um payload amplo e defensivo para update completo do produto.
    """
    marketplaces = []
    for canal in canais:
        marketplaces.append(
            {
                "canal": canal["canal"],
                "preco": canal["preco"],
                "precoPromocional": canal["preco_promocional"],
                "preco_promocional": canal["preco_promocional"],
            }
        )

    patch = {
        "id": existing.get("id"),
        "nome": existing.get("nome"),
        "codigo": existing.get("codigo"),
        "preco": existing.get("preco"),
        "precoCusto": existing.get("precoCusto") or existing.get("preco_custo") or existing.get("custo"),
        "marketplaces": marketplaces,
        "precosMarketplaces": marketplaces,
        "precos_marketplaces": marketplaces,
    }

    # preserva campos úteis se existirem
    preserve_fields = [
        "tipo",
        "situacao",
        "formato",
        "descricaoCurta",
        "descricaoComplementar",
        "unidade",
        "pesoLiquido",
        "pesoBruto",
        "gtin",
        "gtinEmbalagem",
        "estoque",
        "fornecedor",
        "categoria",
        "marca",
        "dimensoes",
    ]
    for field in preserve_fields:
        if field in existing:
            patch[field] = existing[field]

    return patch


def _try_update_full_product(client: Any, produto_id: int, canais: list[dict]) -> dict:
    """
    Busca o produto e tenta enviar um update completo preservando os dados.
    """
    existing = None
    get_attempts = [
        ("get_product", {"product_id": produto_id}),
        ("get_product_by_id", {"product_id": produto_id}),
    ]

    for method_name, kwargs in get_attempts:
        if not _has_method(client, method_name):
            continue
        try:
            try:
                existing = getattr(client, method_name)(**kwargs)
            except TypeError:
                existing = getattr(client, method_name)(produto_id)
            break
        except Exception:
            continue

    if existing is None:
        raise RuntimeError("Não foi possível carregar o produto atual no Bling para update completo.")

    if isinstance(existing, dict) and "data" in existing and isinstance(existing["data"], dict):
        existing = existing["data"]

    patch = _build_patch_payload_existing_product(existing if isinstance(existing, dict) else {}, canais)

    update_attempts = [
        ("update_product", {"product_id": produto_id, "payload": patch}),
        ("update_product_by_id", {"product_id": produto_id, "payload": patch}),
        ("save_product", {"product_id": produto_id, "payload": patch}),
    ]

    ultimo_erro = None
    for method_name, kwargs in update_attempts:
        if not _has_method(client, method_name):
            continue
        try:
            try:
                response = getattr(client, method_name)(**kwargs)
            except TypeError:
                response = getattr(client, method_name)(produto_id, patch)
            return {
                "ok": True,
                "metodo": method_name,
                "response": response,
                "payload_enviado": patch,
            }
        except Exception as exc:
            ultimo_erro = str(exc)

    raise RuntimeError(ultimo_erro or "Nenhum método de update completo compatível encontrado.")


# ============================================================
# API principal
# ============================================================
def aplicar_precos_multicanal(client: Any, payload: dict) -> dict:
    """
    Aplica preços no Bling a partir de um payload vindo da fila ou do preview.

    Estruturas aceitas:
    - payload["produto"]["id"] + payload["marketplaces"]
    - payload["produto_bling"]["id"] + payload["itens"]
    - payload["raw"]["integracao"]["itens"]
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

    # 1) tenta métodos diretos por canal
    resultados_diretos = _try_update_direct_methods(client, produto_id, canais)
    diretos_ok = [r for r in resultados_diretos if r.get("ok")]

    if diretos_ok and len(diretos_ok) == len(canais):
        return {
            "ok": True,
            "estrategia": "metodos_diretos_por_canal",
            "produto_id": produto_id,
            "produto": produto,
            "resultados": resultados_diretos,
            "canais_aplicados": len(diretos_ok),
            "canais_totais": len(canais),
        }

    # 2) fallback: update completo do produto com marketplaces
    try:
        resultado_full = _try_update_full_product(client, produto_id, canais)
        return {
            "ok": True,
            "estrategia": "update_completo_produto",
            "produto_id": produto_id,
            "produto": produto,
            "resultados_diretos": resultados_diretos,
            "resultado_full": resultado_full,
            "canais_aplicados": len(canais),
            "canais_totais": len(canais),
        }
    except Exception as exc:
        return {
            "ok": False,
            "produto_id": produto_id,
            "produto": produto,
            "erro": str(exc),
            "resultados_diretos": resultados_diretos,
            "canais_totais": len(canais),
            "payload_processado": {
                "produto_id": produto_id,
                "canais": canais,
            },
        }