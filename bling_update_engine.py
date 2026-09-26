from __future__ import annotations

from typing import Any, Optional


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    txt = str(value).strip().replace("R$", "").replace("%", "").replace(" ", "")
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
    for src, dst in [
        ("á","a"),("ã","a"),("â","a"),("é","e"),("ê","e"),
        ("í","i"),("ó","o"),("õ","o"),("ô","o"),("ú","u"),("ç","c"),("-"," "),
    ]:
        txt = txt.replace(src, dst)
    txt = "_".join(txt.split())
    aliases = {
        "mercado_livre_classico": "mercado_livre_classico",
        "mercado_livre_clasico":  "mercado_livre_classico",
        "mercado_livre_premium":  "mercado_livre_premium",
        "shopee":   "shopee",
        "amazon":   "amazon",
        "shein":    "shein",
        "shopify":  "shopify",
        "shopfy":   "shopify",
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
    raw_produto: dict = {}
    if isinstance(payload.get("raw"), dict):
        raw_produto = payload["raw"].get("produto_bling") or {}

    base: dict = {}
    for src in [raw_produto, produto_bling, produto]:
        if isinstance(src, dict):
            base.update({k: v for k, v in src.items() if v not in (None, "")})

    return {
        "id":          base.get("id"),
        "nome":        base.get("nome"),
        "codigo":      base.get("codigo") or base.get("sku"),
        "ean":         base.get("ean") or base.get("gtin"),
        "preco":       _safe_float(base.get("preco"), 0),
        "preco_custo": _safe_float(base.get("precoCusto") or base.get("preco_custo") or base.get("custo"), 0),
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
                    item.get("preco_promocional") or item.get("promocional") or item.get("preco_final"),
                    0,
                ),
                "raw": item,
            }
    return resultado


# ─────────────────────────────────────────────
# Estratégias de update no Bling
# ─────────────────────────────────────────────

def _build_patch_preco_base(existing: dict, canais: list[dict]) -> dict:
    """
    Estratégia 1 (fallback seguro): atualiza apenas o campo 'preco' do produto.
    Envia o produto completo de volta para evitar erros de validação do Bling.
    Remove apenas campos que causam problemas (customizados, tipoEstoque inválido).
    """
    src = dict(existing) if isinstance(existing, dict) else {}
    if "data" in src and isinstance(src["data"], dict):
        src = dict(src["data"])

    precos_validos = []
    for canal in canais:
        promo = _safe_float(canal.get("preco_promocional"), 0)
        preco = _safe_float(canal.get("preco"), 0)
        if promo > 0:
            precos_validos.append(promo)
        elif preco > 0:
            precos_validos.append(preco)

    # Começa com o produto completo
    patch = {k: v for k, v in src.items()}

    # Atualiza o preço
    if precos_validos:
        patch["preco"] = round(min(precos_validos), 2)

    # Remove campos que causam erros de validação
    campos_remover = [
        "tipoEstoque",      # deve estar dentro de estrutura, não no root
        "camposCustomizados",
        "customFields",
        "midias",
        "anexos",
        "producao",
    ]
    for campo in campos_remover:
        patch.pop(campo, None)

    # Corrige tipoEstoque dentro de estrutura se existir
    if isinstance(patch.get("estrutura"), dict):
        estrutura = dict(patch["estrutura"])
        tipo = estrutura.get("tipoEstoque")
        if tipo not in ("V", "F"):
            estrutura["tipoEstoque"] = "V"
        patch["estrutura"] = estrutura

    return patch


def _build_patch_multicanal(existing: dict, canais: list[dict]) -> dict:
    """
    Estratégia 2 (multicanal real): tenta preencher o array 'variacoes' e
    'listaEcommerce' / 'integracoes' quando o produto os suporta.

    A API do Bling V3 não expõe endpoints dedicados de anúncio por marketplace,
    mas aceita o campo 'variacoes[].preco' e, para alguns planos,
    'listaEcommerce' com preço por loja.

    Esta função preenche os dois campos caso já existam no produto atual.
    Se o produto não tiver nenhum desses campos, cai no patch de preço base.
    """
    src = dict(existing) if isinstance(existing, dict) else {}
    if "data" in src and isinstance(src["data"], dict):
        src = dict(src["data"])

    # Começa com o produto completo
    patch = {k: v for k, v in src.items()}

    # Remove campos que causam erros de validação
    campos_remover = ["tipoEstoque", "camposCustomizados", "customFields", "midias", "anexos", "producao"]
    for campo in campos_remover:
        patch.pop(campo, None)

    # Corrige tipoEstoque dentro de estrutura
    if isinstance(patch.get("estrutura"), dict):
        estrutura = dict(patch["estrutura"])
        if estrutura.get("tipoEstoque") not in ("V", "F"):
            estrutura["tipoEstoque"] = "V"
        patch["estrutura"] = estrutura

    tem_integracoes = isinstance(src.get("listaEcommerce"), list) and src["listaEcommerce"]
    tem_variacoes = isinstance(src.get("variacoes"), list) and src["variacoes"]

    canal_map = {c["canal"]: c for c in canais}

    # ── Atualiza listaEcommerce (integrações de loja) ──
    if tem_integracoes:
        for loja in patch["listaEcommerce"]:
            if not isinstance(loja, dict):
                continue
            nome_loja = _normalize_channel_key(str(loja.get("nomeLoja") or loja.get("nome") or ""))
            canal_data = canal_map.get(nome_loja)
            if canal_data:
                preco = _safe_float(canal_data.get("preco"), 0)
                promo = _safe_float(canal_data.get("preco_promocional"), 0)
                if preco > 0:
                    loja["preco"] = round(preco, 2)
                if promo > 0:
                    loja["precoPromocional"] = round(promo, 2)

    # ── Atualiza variações (preço base das variações) ──
    if tem_variacoes and canais:
        menor_promo = min(
            (_safe_float(c.get("preco_promocional"), 0) for c in canais if _safe_float(c.get("preco_promocional"), 0) > 0),
            default=0,
        )
        if menor_promo > 0:
            for var in patch["variacoes"]:
                if isinstance(var, dict):
                    var["preco"] = round(menor_promo, 2)

    # ── Sempre atualiza o preço base ──
    precos_validos = [
        _safe_float(c.get("preco_promocional"), 0)
        for c in canais
        if _safe_float(c.get("preco_promocional"), 0) > 0
    ]
    if precos_validos:
        patch["preco"] = round(min(precos_validos), 2)

    return patch


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
        canais.append({
            "canal": canal_key,
            "label": item.get("label") or canal_key,
            "preco": round(preco, 2),
            "preco_promocional": round(promo, 2),
            "raw": item.get("raw", {}),
        })

    return {
        "produto": produto,
        "produto_id": _extract_product_id(payload),
        "canais": canais,
        "payload_original": payload,
    }


# ─────────────────────────────────────────────
# API principal
# ─────────────────────────────────────────────

def aplicar_precos_multicanal(client: Any, payload: dict) -> dict:
    """
    Aplica preços no Bling usando a melhor estratégia disponível:

    1. Busca o produto atual no Bling para inspecionar sua estrutura.
    2. Se tiver 'listaEcommerce' ou 'variacoes', usa a estratégia multicanal
       que atualiza os preços por loja/variação.
    3. Caso contrário, usa o patch simples de preço base (menor preço entre canais).

    Retorna um relatório detalhado com a estratégia aplicada, canais enviados
    e a resposta da API.
    """
    if not isinstance(payload, dict):
        raise ValueError("Payload inválido para aplicação de preços.")

    targets = _build_price_targets(payload)
    produto_id = targets["produto_id"]
    canais = targets["canais"]
    produto_meta = targets["produto"]

    if not produto_id:
        raise ValueError("Não foi possível identificar o ID do produto no Bling.")
    if not canais:
        raise ValueError("Nenhum preço de canal encontrado para aplicar.")

    existing = client.get_product(int(produto_id))

    tem_integracoes = isinstance(existing.get("listaEcommerce"), list) and bool(existing["listaEcommerce"])
    tem_variacoes = isinstance(existing.get("variacoes"), list) and bool(existing["variacoes"])

    if tem_integracoes or tem_variacoes:
        estrategia = "multicanal_completo"
        patch = _build_patch_multicanal(existing, canais)
    else:
        estrategia = "preco_base"
        patch = _build_patch_preco_base(existing, canais)

    response = client.update_product(int(produto_id), patch)

    return {
        "ok": True,
        "estrategia": estrategia,
        "produto_id": produto_id,
        "produto": produto_meta,
        "canais_aplicados": [
            {
                "canal": c["canal"],
                "label": c["label"],
                "preco_virtual": c["preco"],
                "preco_promocional": c["preco_promocional"],
            }
            for c in canais
        ],
        "tem_integracoes_bling": tem_integracoes,
        "tem_variacoes_bling": tem_variacoes,
        "payload_enviado": patch,
        "resultado_api": response,
        "observacao": (
            "Preços aplicados por canal/marketplace (listaEcommerce/variacoes)."
            if estrategia == "multicanal_completo"
            else "Produto sem listaEcommerce/variacoes — apenas o preço base foi atualizado."
        ),
    }
