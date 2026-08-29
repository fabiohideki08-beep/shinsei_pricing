# -*- coding: utf-8 -*-
"""
routes/copiar_ml.py — Copia anúncios do ML Shinsei → ML AKG
Recebe um ou mais MLB IDs, busca o detalhe completo na conta Shinsei
e cria anúncio idêntico na conta AKG preservando SKU (seller_custom_field).
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import requests as _req
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)
router = APIRouter()

BASE_DIR  = Path(__file__).resolve().parent.parent
PAGES_DIR = BASE_DIR / "pages"
ML_API    = "https://api.mercadolibre.com"


# ── Helpers de token ──────────────────────────────────────────────────────────

def _token_shinsei() -> str:
    """Retorna access_token ML Shinsei válido, renovando automaticamente se expirado."""
    from services.mercado_livre import obter_token_ml
    return obter_token_ml()


def _token_akg() -> str:
    """Retorna access_token ML AKG válido, renovando automaticamente se expirado."""
    import importlib, sys, time as _time
    routes_ml = sys.modules.get("routes.mercado_livre") or importlib.import_module("routes.mercado_livre")
    svc = routes_ml._get_akg_oauth()
    tokens = svc._read_json(svc.tokens_file, {})
    if not tokens:
        raise ValueError("Token AKG não encontrado — faça login em /ml/login2")
    expires_at = float(tokens.get("expires_at", 0))
    if tokens.get("access_token") and _time.time() < expires_at - 300:
        return tokens["access_token"]
    # Renova
    result = svc.refresh_token()
    if not result.get("success"):
        raise ValueError(result.get("error", "Falha ao renovar token AKG"))
    return result["data"]["access_token"]


def _hdrs(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── Lógica de cópia ───────────────────────────────────────────────────────────

def _extract_id(texto: str) -> str:
    """Aceita MLBU/MLB + número, URL completa ou só o número."""
    texto = texto.strip()
    m = re.search(r"MLB[U]?\d+", texto.upper())
    if m:
        return m.group(0)
    m = re.search(r"\d{10,}", texto)
    if m:
        return f"MLB{m.group(0)}"
    return texto.upper()

# Mantém alias para compatibilidade interna
def _extract_mlb_id(texto: str) -> str:
    return _extract_id(texto)


def _get_item(item_id: str, token: str) -> dict:
    r = _req.get(f"{ML_API}/items/{item_id}", headers=_hdrs(token), timeout=20)
    r.raise_for_status()
    return r.json()


def _get_seller_id(token: str) -> str:
    r = _req.get(f"{ML_API}/users/me", headers=_hdrs(token), timeout=10)
    r.raise_for_status()
    return str(r.json()["id"])


def _get_seller_item_ids(seller_id: str, token: str) -> list[str]:
    """Busca todos os IDs de anúncios do vendedor (até 1000)."""
    ids: list[str] = []
    offset = 0
    while True:
        r = _req.get(
            f"{ML_API}/users/{seller_id}/items/search",
            params={"limit": 100, "offset": offset},
            headers=_hdrs(token), timeout=20,
        )
        if r.status_code != 200:
            break
        data = r.json()
        batch = data.get("results") or []
        ids.extend(batch)
        if len(batch) < 100 or len(ids) >= (data.get("paging", {}).get("total") or 0):
            break
        offset += 100
    return ids


def _get_family_items_by_family_id(family_id: str, seller_id: str, token: str) -> list[str]:
    """Busca todos os MLBs do vendedor com o mesmo family_id (paginado)."""
    ids: list[str] = []
    offset = 0
    while True:
        r = _req.get(
            f"{ML_API}/users/{seller_id}/items/search",
            params={"family_id": family_id, "limit": 100, "offset": offset},
            headers=_hdrs(token), timeout=20,
        )
        if r.status_code != 200:
            break
        data = r.json()
        batch = data.get("results") or []
        ids.extend(batch)
        total = (data.get("paging") or {}).get("total", 0)
        if not batch or len(ids) >= total:
            break
        offset += 100
    return ids


def _get_family_items(family_ref: str, token: str) -> list[str]:
    """
    Retorna lista de MLB IDs de todos os membros de uma família Omni.
    Aceita MLBU (family_id) ou MLB (busca o family_id do item e lista irmãos).
    """
    seller_id = _get_seller_id(token)

    # Se recebeu um MLB filho, extrai o family_id dele
    if not family_ref.startswith("MLBU"):
        r0 = _req.get(f"{ML_API}/items/{family_ref}",
                      headers=_hdrs(token), timeout=20)
        if r0.status_code == 200:
            data = r0.json()
            # Multi-variação tradicional: variações inline
            if data.get("variations"):
                return [family_ref]
            fid = data.get("family_id")
            if fid:
                return _get_family_items_by_family_id(fid, seller_id, token)
        return [family_ref]  # fallback: trata como item único

    # Recebeu MLBU diretamente → extrai o número (ML espera family_id sem prefixo "MLBU")
    numeric_id = re.sub(r"^MLBU", "", family_ref, flags=re.IGNORECASE)
    return _get_family_items_by_family_id(numeric_id, seller_id, token)


def _get_description(item_id: str, token: str) -> str:
    r = _req.get(f"{ML_API}/items/{item_id}/description", headers=_hdrs(token), timeout=20)
    if r.status_code == 200:
        return r.json().get("plain_text") or r.json().get("text") or ""
    return ""


_COLOR_CODE_RE = re.compile(
    r'^(?:\d+[-./]\d+(?:[-./]\d+)?|[A-Z]-\d+(?:[-./]\d+)?)$'
)
_OX_RE = re.compile(r'\bOx(?:idante)?\s+(?:de\s+)?(\d+)\s+Vol(?:umes?)?\b', re.IGNORECASE)

_KNOWN_LINES = [
    # ordem do mais longo para o mais curto para match correto
    'Igora ZERO AMM', 'Igora Highlifts', 'Igora Absolutes',
    'Igora Fashion Lights', 'Igora Color 10', 'Igora Vibrance',
    'Igora Royal', 'Igora',
    'Color Wear', 'Evolution', 'Semi Di Lino',
]
_PRODUCT_TYPES = [
    'Colorações', 'Coloração', 'Tonalizantes', 'Tonalizante',
    'Tinta', 'Tintas', 'Kit', 'Kits',
]
_BRANDS_AKG = ['Alfaparf']       # incluir no título AKG
_BRANDS_SKIP = ['Schwarzkopf']   # remover (já implícito em "Igora")


def _smart_family_name(fn: str, max_len: int = 60) -> str:
    """Reconstrói family_name priorizando: código > linha > tipo > ox vol > marca > descrição."""
    if len(fn) <= max_len:
        return fn

    work = fn

    # 1. Extrair código de cor (ex: 7-00, 8.4, L-66)
    codes = [w for w in work.split() if _COLOR_CODE_RE.match(w)]
    for c in codes:
        work = re.sub(r'\b' + re.escape(c) + r'\b', '', work).strip()

    # 2. Extrair linha de produto
    lines_found = []
    for line in _KNOWN_LINES:
        if re.search(re.escape(line), work, re.IGNORECASE):
            lines_found.append(line)
            work = re.sub(re.escape(line), '', work, flags=re.IGNORECASE).strip()
            break  # apenas a primeira/mais longa

    # 3. Extrair tipo de produto
    types_found = []
    for pt in _PRODUCT_TYPES:
        if re.search(r'\b' + re.escape(pt) + r'\b', work, re.IGNORECASE):
            types_found.append(pt)
            work = re.sub(r'\b' + re.escape(pt) + r'\b', '', work, flags=re.IGNORECASE).strip()
            break

    # 4. Extrair OX / oxidante (abreviar para "Ox N Vol")
    ox_found = []
    m = _OX_RE.search(work)
    if m:
        ox_found.append(f"Ox {m.group(1)} Vol")
        work = (work[:m.start()] + work[m.end():]).strip()

    # 5. Remover marcas implícitas (Schwarzkopf já está em "Igora*")
    for brand in _BRANDS_SKIP:
        work = re.sub(r'\b' + re.escape(brand) + r'\b', '', work, flags=re.IGNORECASE).strip()

    # 5b. Extrair marcas a incluir (Alfaparf)
    brands_found = []
    for brand in _BRANDS_AKG:
        if re.search(r'\b' + re.escape(brand) + r'\b', work, re.IGNORECASE):
            brands_found.append(brand)
            work = re.sub(r'\b' + re.escape(brand) + r'\b', '', work, flags=re.IGNORECASE).strip()

    # 6. O restante é descrição de cor / outras palavras
    remainder = re.sub(r'\s+', ' ', work).strip()

    # Montar em ordem de prioridade
    ordered = codes + lines_found + types_found + ox_found + brands_found
    if remainder:
        ordered.append(remainder)

    result = ""
    for part in ordered:
        if not part:
            continue
        candidate = (result + " " + part).strip() if result else part
        if len(candidate) <= max_len:
            result = candidate
        else:
            # tenta encaixar palavra a palavra
            for word in part.split():
                candidate2 = (result + " " + word).strip() if result else word
                if len(candidate2) <= max_len:
                    result = candidate2
                else:
                    break
    return result.strip()


def _build_payload(item: dict) -> dict:
    """
    Monta o payload para criar o anúncio na conta AKG.
    Sempre cria anúncio TRADICIONAL (não-catálogo): nunca inclui
    catalog_product_id e força catalog_listing=false.
    Clone perfeito: title, family_name, SKU, fotos, variações, warranty, descrição.
    """

    # Fotos: passa as URLs originais — ML re-hospeda automaticamente
    # Guardamos também os IDs originais (Shinsei) para mapear picture_ids nas variações
    src_pictures = (item.get("pictures") or [])
    pictures = [{"source": p["url"]} for p in src_pictures if p.get("url")]
    # Índice: shinsei_picture_id → posição na lista (para mapear após criação)
    shinsei_pic_index: dict[str, int] = {
        p["id"]: i for i, p in enumerate(src_pictures) if p.get("id")
    }

    # Variações com seller_custom_field (SKU) — sem picture_ids por ora
    # (picture_ids são mapeados pós-criação em _fix_variation_pictures)
    variacoes = []
    for v in (item.get("variations") or []):
        var = {
            "attribute_combinations": v.get("attribute_combinations") or [],
            "price":                  v.get("price") or item.get("price"),
            "available_quantity":     v.get("available_quantity") or 0,
            "seller_custom_field":    v.get("seller_custom_field") or "",
        }
        # Preserva picture_ids Shinsei no campo auxiliar (não enviado ao ML)
        if v.get("picture_ids"):
            var["_shinsei_picture_ids"] = v["picture_ids"]
        variacoes.append(var)

    # Atributos — remove campos que o ML rejeita na criação (somente-leitura do sistema)
    # BRAND é obrigatório em MLB264861 — NÃO remover
    # GTIN incluído: obrigatório para MLB264861. Retry automático sem GTIN se conflitar.
    # HAIR_TONE: necessário para itens omni identificarem a cor dentro da família.
    #   PORÉM, quando family_name já contém o código da cor (ex: "Alfaparf 5.32 60ml"),
    #   o ML compõe o título como family_name + HAIR_TONE = duplicado.
    #   Solução: excluir HAIR_TONE apenas quando seu código numérico já está no family_name.
    SKIP_ATTR_IDS = {
        "ITEM_CONDITION", "SELLER_ID", "CATALOG_LISTING",
        "GIFTABLE", "SELLER_PACKAGE_TYPE", "MANUAL_TITLE",
        # GTIN/EAN excluídos: o ML vincula automaticamente ao catálogo pelo barcode
        # e trava o título — anúncios de catálogo frequentemente não correspondem ao produto
        "GTIN", "EAN", "GTIN_PRODUCT_IDENTIFIER",
    }

    # Detecta se HAIR_TONE vai duplicar no título
    _family_name_raw = (item.get("family_name") or "").upper()
    _hair_tone_attr = next((a for a in (item.get("attributes") or []) if a.get("id") == "HAIR_TONE"), None)
    _hair_tone_val = (_hair_tone_attr.get("value_name") or "") if _hair_tone_attr else ""
    import re as _re
    _ht_code_m = _re.match(r'([\d\.]+)', _hair_tone_val.strip())
    _ht_code = _ht_code_m.group(1) if _ht_code_m else ""
    _hair_tone_duplica = bool(_ht_code and _ht_code in _family_name_raw)
    # HAIR_TONE sem value_id → ML AKG rejeita com cause_id 410 (conta nova só aceita valores pré-definidos)
    _hair_tone_sem_vid = _hair_tone_attr and not (_hair_tone_attr.get("value_id"))
    if _hair_tone_duplica or _hair_tone_sem_vid:
        SKIP_ATTR_IDS = SKIP_ATTR_IDS | {"HAIR_TONE"}

    def _mk_attr(a: dict) -> dict:
        entry: dict = {"id": a["id"], "value_name": a.get("value_name")}
        if a.get("value_id"):
            entry["value_id"] = a["value_id"]
        return entry

    atributos = [
        _mk_attr(a)
        for a in (item.get("attributes") or [])
        if a.get("id") and a.get("id") not in SKIP_ATTR_IDS and a.get("value_name")
    ]

    # NAME é obrigatório em MLB264861 para contas novas — adicionar se ausente
    if not any(a["id"] == "NAME" for a in atributos):
        brand = next((a.get("value_name","") for a in (item.get("attributes") or []) if a.get("id") == "BRAND"), "")
        line = next((a.get("value_name","") for a in (item.get("attributes") or []) if a.get("id") == "LINE"), "")
        name_val = f"{brand} {line}".strip() or item.get("title", "")[:60]
        if name_val:
            atributos.append({"id": "NAME", "value_name": name_val})

    # listing_type_id: garante tipo tradicional (gold_special ou gold_pro)
    listing_type = item.get("listing_type_id") or "gold_special"
    if listing_type not in ("gold_special", "gold_pro", "bronze", "free"):
        listing_type = "gold_special"

    _title = item.get("title", "")
    payload: dict[str, Any] = {
        "category_id":         item.get("category_id", ""),
        "price":               item.get("price"),
        "currency_id":         item.get("currency_id", "BRL"),
        "available_quantity":  item.get("available_quantity", 0),
        "buying_mode":         item.get("buying_mode", "buy_it_now"),
        "listing_type_id":     listing_type,
        "condition":           item.get("condition", "new"),
        "pictures":            pictures,
        "attributes":          atributos,
        "seller_custom_field": item.get("seller_custom_field") or "",
        "catalog_listing":     False,
    }

    # Não enviar warranty — campo depreciado no ML AKG (causa_id 410 em contas novas)

    # family_name é obrigatório para itens omni (colorações, etc.)
    family_name = item.get("family_name")
    if family_name:
        fn = family_name.strip().replace(" + ", " ")
        if len(fn) > 60:
            fn = _smart_family_name(fn)
        payload["family_name"] = fn
    else:
        payload["title"] = _title

    if variacoes:
        # Envia sem picture_ids (serão associados via PUT após criação)
        payload["variations"] = [
            {k: v for k, v in var.items() if not k.startswith("_")}
            for var in variacoes
        ]

    # Frete
    shipping = item.get("shipping") or {}
    if shipping:
        payload["shipping"] = {
            "mode":          shipping.get("mode", "me2"),
            "local_pick_up": shipping.get("local_pick_up", False),
            "free_shipping": shipping.get("free_shipping", False),
            "logistic_type": shipping.get("logistic_type", "fulfillment"),
        }

    # Metadado auxiliar (não enviado ao ML) para mapeamento de fotos pós-criação
    payload["_shinsei_pic_index"] = shinsei_pic_index
    payload["_variacoes_com_pics"] = variacoes  # contém _shinsei_picture_ids

    return payload


def _fix_variation_pictures(novo_id: str, payload: dict, tok_a: str) -> bool:
    """
    Pós-criação: mapeia picture_ids Shinsei → AKG por posição e faz PUT
    nas variações para associar as fotos corretas a cada variação.
    Retorna True se houve PUT bem-sucedido, False se não havia o que fazer.
    """
    variacoes_orig = payload.get("_variacoes_com_pics") or []
    shinsei_pic_index = payload.get("_shinsei_pic_index") or {}

    # Verifica se alguma variação tinha picture_ids
    tem_pics = any(v.get("_shinsei_picture_ids") for v in variacoes_orig)
    if not tem_pics or not shinsei_pic_index:
        return False

    # Busca o item recém-criado na AKG para obter os novos picture_ids
    r = _req.get(f"{ML_API}/items/{novo_id}", headers=_hdrs(tok_a), timeout=20)
    if r.status_code != 200:
        return False
    akg_item = r.json()

    # Mapeia posição → novo AKG picture_id
    akg_pictures = akg_item.get("pictures") or []
    pos_to_akg_id = {i: p["id"] for i, p in enumerate(akg_pictures) if p.get("id")}

    # Mapeia shinsei_picture_id → akg_picture_id via posição
    shinsei_to_akg: dict[str, str] = {}
    for s_id, pos in shinsei_pic_index.items():
        if pos in pos_to_akg_id:
            shinsei_to_akg[s_id] = pos_to_akg_id[pos]

    if not shinsei_to_akg:
        return False

    # Monta variações com os novos picture_ids para o PUT
    akg_variacoes = akg_item.get("variations") or []
    # Cria índice por seller_custom_field para casar variação Shinsei → AKG
    akg_var_by_sku: dict[str, dict] = {
        v.get("seller_custom_field", ""): v for v in akg_variacoes if v.get("seller_custom_field")
    }

    vars_put = []
    for orig_var in variacoes_orig:
        sku = orig_var.get("seller_custom_field") or ""
        s_pic_ids = orig_var.get("_shinsei_picture_ids") or []
        akg_var = akg_var_by_sku.get(sku)
        if not akg_var or not s_pic_ids:
            continue
        novos_pic_ids = [shinsei_to_akg[pid] for pid in s_pic_ids if pid in shinsei_to_akg]
        if novos_pic_ids:
            vars_put.append({"id": akg_var["id"], "picture_ids": novos_pic_ids})

    if not vars_put:
        return False

    r2 = _req.put(
        f"{ML_API}/items/{novo_id}",
        headers=_hdrs(tok_a),
        json={"variations": vars_put},
        timeout=20,
    )
    return r2.status_code in (200, 201)


def _gtin_conflict(body: dict) -> bool:
    """Retorna True se o erro indica conflito de GTIN com outra conta."""
    keywords = ("código universal", "insira um código universal", "universal code",
                "not used in another", "outra marca")
    # Checa message + causes
    all_text = " ".join([
        (body.get("message") or ""),
        *[c.get("message", "") for c in (body.get("cause") or [])],
    ]).lower()
    return any(k in all_text for k in keywords)


def _gtin_missing(body: dict) -> bool:
    """Retorna True se o erro indica que GTIN é obrigatório para a categoria."""
    all_text = " ".join([
        (body.get("message") or ""),
        *[c.get("message", "") for c in (body.get("cause") or [])],
    ]).lower()
    return "missing_conditional_required" in " ".join(
        c.get("code", "") for c in (body.get("cause") or [])
    ) or "required" in all_text and "gtin" in all_text


def _gerar_gtin_temp() -> str:
    """Gera um EAN-13 válido (checksum correto) com prefixo 9999 improvável de conflitar."""
    import random
    digits = [9, 9, 9, 9] + [random.randint(0, 9) for _ in range(8)]
    # Calcula dígito verificador EAN-13
    soma = sum(d * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits))
    check = (10 - (soma % 10)) % 10
    return "".join(str(d) for d in digits) + str(check)


def _atualizar_gtin_akg(akg_id: str, gtin_real: str, token: str) -> bool:
    """Atualiza o GTIN do item AKG para o valor real após criação com temp."""
    r = _req.put(f"{ML_API}/items/{akg_id}",
                 headers=_hdrs(token),
                 json={"attributes": [{"id": "GTIN", "value_name": gtin_real}]},
                 timeout=15)
    return r.status_code in (200, 201)


def _criar_item_akg(payload: dict, token: str) -> dict:
    # Remove campos auxiliares de metadado (prefixo _) antes de enviar ao ML
    clean = {k: v for k, v in payload.items() if not k.startswith("_")}
    r = _req.post(f"{ML_API}/items", headers=_hdrs(token),
                  json=clean, timeout=30)
    body = r.json()

    if r.status_code in (200, 201):
        return {"status_code": r.status_code, "body": body}

    if r.status_code not in (400, 422) or "attributes" not in clean:
        return {"status_code": r.status_code, "body": body}

    gtin_original = next(
        (a.get("value_name") for a in clean["attributes"] if a.get("id") == "GTIN"),
        None,
    )

    # Conflito de GTIN — detecta tanto erro explícito ("outra marca") quanto
    # body:{} ou cause:[] silenciosos (conflito intra-conta — GTIN travado por item fechado)
    _gtin_silent = not body.get("cause")  # cause ausente ou lista vazia []
    if (_gtin_conflict(body) or _gtin_silent) and gtin_original:
        gtin_temp = _gerar_gtin_temp()
        clean_temp = {**clean, "attributes": [
            {"id": "GTIN", "value_name": gtin_temp} if a.get("id") == "GTIN" else a
            for a in clean["attributes"]
        ]}
        r2 = _req.post(f"{ML_API}/items", headers=_hdrs(token),
                       json=clean_temp, timeout=30)
        if r2.status_code in (200, 201):
            novo_id = r2.json().get("id")
            gtin_atualizado = False
            if novo_id:
                time.sleep(1.0)
                gtin_atualizado = _atualizar_gtin_akg(novo_id, gtin_original, token)
                if not gtin_atualizado:
                    # Persiste para retry posterior via /controle-anuncios/fix-gtin
                    _save_gtin_pendente(novo_id, gtin_original)
            return {
                "status_code": r2.status_code,
                "body": r2.json(),
                "gtin_temp": gtin_temp,
                "gtin_real": gtin_original,
                "gtin_atualizado": gtin_atualizado,
            }

    # GTIN obrigatório mas não existe no Shinsei → cria com GTIN temp e marca pendente
    if _gtin_missing(body):
        gtin_temp = _gerar_gtin_temp()
        attrs_com_temp = [a for a in clean["attributes"] if a.get("id") != "GTIN"]
        attrs_com_temp.append({"id": "GTIN", "value_name": gtin_temp})
        clean_temp = {**clean, "attributes": attrs_com_temp}
        r3 = _req.post(f"{ML_API}/items", headers=_hdrs(token),
                       json=clean_temp, timeout=30)
        if r3.status_code in (200, 201):
            novo_id = r3.json().get("id")
            if novo_id:
                _save_gtin_pendente(novo_id, "__sem_gtin__")
            return {"status_code": r3.status_code, "body": r3.json(),
                    "gtin_temp": gtin_temp, "gtin_real": None}

    return {"status_code": r.status_code, "body": body}


_AKG_FAMILY_ID_CACHE_FILE = BASE_DIR / "data" / "akg_family_ids.json"
_AKG_COPY_MAP_FILE        = BASE_DIR / "data" / "akg_copy_map.json"   # {id_shinsei: id_akg}
_AKG_GTIN_PENDENTE_FILE   = BASE_DIR / "data" / "akg_gtin_pendente.json"  # {akg_id: gtin_real}


def _load_gtin_pendente() -> dict[str, str]:
    try:
        if _AKG_GTIN_PENDENTE_FILE.exists():
            return json.loads(_AKG_GTIN_PENDENTE_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _save_gtin_pendente(akg_id: str, gtin_real: str):
    m = _load_gtin_pendente()
    m[akg_id] = gtin_real
    _AKG_GTIN_PENDENTE_FILE.parent.mkdir(exist_ok=True)
    _AKG_GTIN_PENDENTE_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def _remove_gtin_pendente(akg_id: str):
    m = _load_gtin_pendente()
    if akg_id in m:
        del m[akg_id]
        _AKG_GTIN_PENDENTE_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_akg_family_id_cache() -> dict[str, str]:
    """Carrega mapa {family_name_shinsei: family_id_akg} do cache em disco."""
    try:
        if _AKG_FAMILY_ID_CACHE_FILE.exists():
            return json.loads(_AKG_FAMILY_ID_CACHE_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _load_copy_map() -> dict[str, str]:
    """Carrega mapa {id_shinsei: id_akg} persistido em disco."""
    try:
        if _AKG_COPY_MAP_FILE.exists():
            return json.loads(_AKG_COPY_MAP_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _verificar_status_akg(akg_id: str, token: str) -> str:
    """Retorna o status real do item na AKG via API. Nunca lança exceção."""
    try:
        r = _req.get(f"{ML_API}/items/{akg_id}",
                     params={"attributes": "id,status"},
                     headers=_hdrs(token), timeout=10)
        if r.status_code == 404:
            return "not_found"
        if r.status_code == 200:
            return r.json().get("status", "unknown")
        return f"http_{r.status_code}"
    except Exception as e:
        logger.warning("_verificar_status_akg(%s): %s", akg_id, e)
        return "error"


def _remove_copy_map_entry(shinsei_id: str):
    """Remove uma entrada do copy_map em disco (item fechado que será recriado)."""
    try:
        m = _load_copy_map()
        if shinsei_id in m:
            del m[shinsei_id]
            _AKG_COPY_MAP_FILE.write_text(
                json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    except Exception as e:
        logger.warning("_remove_copy_map_entry(%s): %s", shinsei_id, e)


def _save_copy_map_entry(shinsei_id: str, akg_id: str):
    """Persiste uma entrada no mapa de cópia."""
    try:
        m = _load_copy_map()
        m[shinsei_id] = akg_id
        _AKG_COPY_MAP_FILE.parent.mkdir(exist_ok=True)
        _AKG_COPY_MAP_FILE.write_text(
            json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning("Não foi possível salvar copy_map: %s", e)


def _save_akg_family_id(family_name: str, family_id: str):
    """Persiste o mapeamento family_name → family_id AKG para dedup futura."""
    try:
        cache = _load_akg_family_id_cache()
        cache[family_name] = family_id
        _AKG_FAMILY_ID_CACHE_FILE.parent.mkdir(exist_ok=True)
        _AKG_FAMILY_ID_CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning("Não foi possível salvar cache family_id AKG: %s", e)


def _get_akg_family_existing(family_name: str, seller_id: str, token: str) -> dict[str, str]:
    """
    Retorna mapa {chave: mlb_id_akg} de todos os itens já existentes na família AKG.
    Chave = seller_custom_field (SKU) quando disponível, senão título do item.
    Usado para deduplicação: pular itens cujo SKU ou título já existe na AKG.
    """
    existing: dict[str, str] = {}

    # 1. Tenta usar family_id cacheado em disco (mais confiável)
    fid: str = ""
    cache = _load_akg_family_id_cache()
    if family_name in cache:
        fid = cache[family_name]
        logger.info("Dedup AKG: usando family_id cacheado %s para '%s'", fid, family_name[:40])

    # 2. Se não tem cache, busca via API por nome
    if not fid:
        r0 = _req.get(f"{ML_API}/users/{seller_id}/items/search",
                      params={"q": family_name[:50], "limit": 10},
                      headers=_hdrs(token), timeout=15)
        sample_ids = (r0.json().get("results") or []) if r0.status_code == 200 else []
        if not sample_ids:
            # Tenta com primeiras 3 palavras
            words = " ".join(family_name.split()[:3])
            r0b = _req.get(f"{ML_API}/users/{seller_id}/items/search",
                           params={"q": words, "limit": 10},
                           headers=_hdrs(token), timeout=15)
            sample_ids = (r0b.json().get("results") or []) if r0b.status_code == 200 else []
        for sid in sample_ids:
            r1 = _req.get(f"{ML_API}/items/{sid}",
                          params={"attributes": "id,family_id,family_name"},
                          headers=_hdrs(token), timeout=10)
            if r1.status_code != 200:
                continue
            d1 = r1.json()
            candidate_fid = str(d1.get("family_id") or "")
            if not candidate_fid:
                continue
            akg_name = (d1.get("family_name") or "").lower()
            shinsei_words_set = set(family_name.lower().split())
            akg_words_set = set(akg_name.split())
            overlap = len(shinsei_words_set & akg_words_set) / max(len(shinsei_words_set), 1)
            if overlap >= 0.4:
                fid = candidate_fid
                _save_akg_family_id(family_name, fid)
                logger.info("Dedup AKG: family_id %s encontrado via busca (overlap=%.0f%%)", fid, overlap*100)
                break

    if not fid:
        logger.warning("Dedup AKG: não foi possível determinar family_id para '%s'", family_name[:40])
        return existing
    # Lista todos os membros da família
    family_ids = _get_family_items_by_family_id(str(fid), seller_id, token)
    logger.info("Dedup: família AKG %s tem %d membros", fid, len(family_ids))
    # Busca SKU e título de cada membro em lotes de 20
    for i in range(0, len(family_ids), 20):
        batch = family_ids[i:i+20]
        rb = _req.get(f"{ML_API}/items",
                      params={"ids": ",".join(batch),
                              "attributes": "id,seller_custom_field,title"},
                      headers=_hdrs(token), timeout=15)
        if rb.status_code != 200:
            continue
        for entry in rb.json():
            item_data = entry.get("body") or entry
            mid = item_data.get("id") or ""
            if not mid:
                continue
            sku = (item_data.get("seller_custom_field") or "").strip()
            title = (item_data.get("title") or "").strip()
            if sku:
                existing[sku] = mid
            if title:
                existing[title] = mid  # índice por título como fallback
    return existing


def _verificar_akg(novo_id: str, original: dict, token: str) -> dict:
    """Busca o item criado na AKG e compara campos essenciais com o original Shinsei."""
    try:
        r = _req.get(f"{ML_API}/items/{novo_id}", headers=_hdrs(token), timeout=15)
        if r.status_code != 200:
            return {"ok": False, "erro": f"GET {novo_id} retornou {r.status_code}"}
        akg = r.json()
        divergencias = []
        if akg.get("category_id") != original.get("category_id"):
            divergencias.append(f"category: {akg.get('category_id')} ≠ {original.get('category_id')}")
        if abs((akg.get("price") or 0) - (original.get("price") or 0)) > 0.01:
            divergencias.append(f"price: {akg.get('price')} ≠ {original.get('price')}")
        akg_fotos = len(akg.get("pictures") or [])
        ori_fotos = len(original.get("pictures") or [])
        if akg_fotos != ori_fotos:
            divergencias.append(f"fotos: {akg_fotos} ≠ {ori_fotos}")
        akg_sku = akg.get("seller_custom_field") or ""
        ori_sku = original.get("seller_custom_field") or ""
        if ori_sku and akg_sku != ori_sku:
            divergencias.append(f"sku: '{akg_sku}' ≠ '{ori_sku}'")
        return {
            "ok": True,
            "status": akg.get("status"),
            "divergencias": divergencias,
            "fotos_akg": akg_fotos,
            "sku_akg": akg_sku,
        }
    except Exception as e:
        return {"ok": False, "erro": str(e)}


def _criar_description_akg(item_id: str, texto: str, token: str):
    if not texto:
        return
    _req.post(f"{ML_API}/items/{item_id}/description",
              headers=_hdrs(token),
              json={"plain_text": texto}, timeout=20)


# ── Endpoint de preview ───────────────────────────────────────────────────────

@router.get("/copiar-ml/preview/{item_id:path}")
def preview_anuncio(item_id: str):
    """
    Busca dados do anúncio Shinsei para preview.
    Aceita MLB ou MLBU (família Omni). Para MLBU, enumera todos os filhos.
    """
    raw_id = _extract_id(item_id)
    try:
        tok_s = _token_shinsei()
    except Exception as e:
        return {"ok": False, "erro": f"Token Shinsei indisponível: {e}"}

    # ── Família MLBU: enumera filhos ──────────────────────────────────────────
    if raw_id.startswith("MLBU"):
        try:
            filhos = _get_family_items(raw_id, tok_s)
        except Exception as e:
            return {"ok": False, "erro": f"Erro ao buscar família {raw_id}: {e}"}
        if not filhos:
            return {"ok": False, "erro": f"Família {raw_id} não retornou filhos. Tente um MLB filho diretamente."}

        # Preview do primeiro filho para pegar título/categoria/preço
        try:
            primeiro = _get_item(filhos[0], tok_s)
        except Exception as e:
            return {"ok": False, "erro": f"Erro ao buscar filho {filhos[0]}: {e}"}

        return {
            "ok":           True,
            "tipo":         "familia",
            "item_id":      raw_id,
            "filhos":       filhos,
            "total_filhos": len(filhos),
            "titulo_base":  primeiro.get("title", "").rsplit(" - ", 1)[0],
            "categoria":    primeiro.get("category_id"),
            "preco":        primeiro.get("price"),
            "tipo_anuncio": primeiro.get("listing_type_id"),
            "condicao":     primeiro.get("condition"),
            "status":       primeiro.get("status"),
        }

    # ── Item individual MLB ───────────────────────────────────────────────────
    try:
        item = _get_item(raw_id, tok_s)
    except Exception as e:
        return {"ok": False, "erro": f"Erro ao buscar {raw_id}: {e}"}

    variacoes = item.get("variations") or []
    family_id = item.get("family_id")

    # Se tem family_id e sem variações inline → é anúncio Omni, auto-expande família
    if family_id and not variacoes:
        try:
            filhos = _get_family_items(raw_id, tok_s)  # passa o MLB filho
        except Exception as e:
            filhos = []
        if filhos and len(filhos) > 1:
            titulo_base = item.get("title", "")
            # Remove sufixo da cor do título: "... - 5-99 Castanho Claro" → "..."
            if " - " in titulo_base:
                titulo_base = titulo_base.rsplit(" - ", 1)[0]
            return {
                "ok":           True,
                "tipo":         "familia",
                "item_id":      f"MLBU{family_id}",
                "filhos":       filhos,
                "total_filhos": len(filhos),
                "titulo_base":  titulo_base,
                "categoria":    item.get("category_id"),
                "preco":        item.get("price"),
                "tipo_anuncio": item.get("listing_type_id"),
                "condicao":     item.get("condition"),
                "status":       item.get("status"),
            }

    familia_hint = f"MLBU{family_id}" if family_id else None

    return {
        "ok":           True,
        "tipo":         "item",
        "item_id":      raw_id,
        "titulo":       item.get("title"),
        "categoria":    item.get("category_id"),
        "preco":        item.get("price"),
        "tipo_anuncio": item.get("listing_type_id"),
        "condicao":     item.get("condition"),
        "status":       item.get("status"),
        "quantidade":   item.get("available_quantity"),
        "fotos":        len(item.get("pictures") or []),
        "variacoes":    len(variacoes),
        "atributos":    len(item.get("attributes") or []),
        "sku":          item.get("seller_custom_field") or "(sem SKU raiz)",
        "skus_variacoes": [
            v.get("seller_custom_field") for v in variacoes
            if v.get("seller_custom_field")
        ][:10],
        "familia_hint": familia_hint,  # MLBU pai, se detectado
    }


# ── Endpoint de cópia ─────────────────────────────────────────────────────────

@router.post("/copiar-ml/copiar")
def copiar_anuncio(body: dict):
    """
    Copia um ou mais anúncios do ML Shinsei → ML AKG.
    Body: { "ids": ["MLB123", "MLB456", ...] }
    """
    ids_raw: list[str] = body.get("ids") or []
    if not ids_raw:
        return {"ok": False, "erro": "Nenhum ID informado"}

    try:
        tok_s = _token_shinsei()
        tok_a = _token_akg()
    except Exception as e:
        return {"ok": False, "erro": f"Tokens indisponíveis: {e}"}

    resultados = []

    # ── Copy map: {id_shinsei: id_akg} persistido em disco ──────────────────
    copy_map: dict[str, str] = _load_copy_map()
    logger.info("Dedup AKG: %d mapeamentos carregados do copy_map", len(copy_map))

    # ── Expande famílias MLBU para lista de filhos individuais ───────────────
    ids_expandidos: list[str] = []
    for raw in ids_raw:
        eid = _extract_id(raw)
        try:
            filhos = _get_family_items(eid, tok_s)
            if len(filhos) > 1 or (len(filhos) == 1 and filhos[0] != eid):
                ids_expandidos.extend(filhos)
                continue
        except Exception:
            pass
        ids_expandidos.append(eid)

    for raw in ids_expandidos:
        mlb = _extract_id(raw)
        entry: dict = {"id_shinsei": mlb, "ok": False}

        try:
            # 1. Busca detalhe Shinsei
            item = _get_item(mlb, tok_s)
            entry["titulo"] = item.get("title", "")
            entry["sku_raiz"] = item.get("seller_custom_field") or ""

            # ── PRÉ-CONFERÊNCIA: verifica se já existe na AKG e está ativo ──
            akg_id_existente = copy_map.get(mlb)
            if akg_id_existente:
                status_akg = _verificar_status_akg(akg_id_existente, tok_a)
                if status_akg in ("active", "paused"):
                    # Ativo/pausado → pula, já OK
                    entry["pulado"] = True
                    entry["id_akg"] = akg_id_existente
                    entry["status_akg_pre"] = status_akg
                    entry["erro"] = f"Ja existe na AKG e esta {status_akg} ({akg_id_existente})"
                    resultados.append(entry)
                    continue
                else:
                    # closed/under_review → remove do map, vai recriar
                    logger.info("AKG %s esta %s — removendo do copy_map e recriando", akg_id_existente, status_akg)
                    entry["akg_anterior"] = akg_id_existente
                    entry["status_akg_pre"] = status_akg
                    copy_map.pop(mlb, None)
                    _remove_copy_map_entry(mlb)

            # 2. Busca descrição
            descricao = _get_description(mlb, tok_s)
            time.sleep(0.3)

            # 3. Monta payload e cria na AKG
            payload = _build_payload(item)
            resp = _criar_item_akg(payload, tok_a)
            entry["status_http"] = resp["status_code"]

            if resp["status_code"] in (200, 201):
                novo_id = resp["body"].get("id")
                entry["id_akg"] = novo_id

                # Informa se usou GTIN temporário e se o real foi aplicado
                if resp.get("gtin_temp"):
                    entry["gtin_temp"] = resp["gtin_temp"]
                    entry["gtin_real"] = resp.get("gtin_real")
                    entry["gtin_atualizado"] = resp.get("gtin_atualizado", False)

                # 4. Descrição
                if novo_id and descricao:
                    _criar_description_akg(novo_id, descricao, tok_a)

                # 5. Associa picture_ids às variações (clone perfeito de fotos)
                if novo_id:
                    try:
                        _fix_variation_pictures(novo_id, payload, tok_a)
                    except Exception as _pe:
                        logger.warning("fix_variation_pictures %s: %s", novo_id, _pe)

                # ── PÓS-CONFERÊNCIA: confirma que o novo item está ativo ────
                time.sleep(1.5)  # aguarda ML processar
                status_pos = _verificar_status_akg(novo_id, tok_a) if novo_id else "unknown"
                entry["status_akg_pos"] = status_pos

                if status_pos in ("active", "paused", "under_review"):
                    entry["ok"] = True
                    entry["msg"] = f"Criado e {status_pos}: {novo_id}"
                    # Persiste no copy_map e catálogo apenas se ativo/under_review
                    copy_map[mlb] = novo_id
                    _save_copy_map_entry(mlb, novo_id)
                    try:
                        from routes.controle_anuncios import register_copy as _reg
                        campanha_slug = (item.get("family_name") or item.get("category_id") or "sem_campanha")
                        campanha_slug = campanha_slug.lower().replace(" ", "_")[:40]
                        _reg(
                            id_shinsei=mlb,
                            id_akg=novo_id,
                            titulo=item.get("title", ""),
                            campanha=campanha_slug,
                            campanha_nome=item.get("family_name") or item.get("category_id") or "Sem campanha",
                            sku=item.get("seller_custom_field") or "",
                            preco=item.get("price"),
                            categoria=item.get("category_id") or "",
                            fotos_shinsei=len(item.get("pictures") or []),
                        )
                    except Exception as _re:
                        logger.warning("register_copy falhou: %s", _re)
                    # Verificação detalhada de campos
                    entry["verificacao"] = _verificar_akg(novo_id, item, tok_a)
                else:
                    # Criado mas imediatamente fechado pelo ML
                    entry["ok"] = False
                    entry["erro"] = f"Criado ({novo_id}) mas ML retornou status={status_pos} — nao salvo no copy_map"
                    logger.warning("Item %s criado mas status=%s — descartado do copy_map", novo_id, status_pos)
            else:
                entry["erro"] = resp["body"].get("message") or str(resp["body"])
                causes = resp["body"].get("cause") or []
                if causes:
                    entry["causes"] = [c.get("message") or str(c) for c in causes[:5]]

        except Exception as e:
            entry["erro"] = str(e)

        resultados.append(entry)
        time.sleep(0.5)

    total = len(resultados)
    criados = sum(1 for r in resultados if r["ok"])
    return {
        "ok": True,
        "total": total,
        "criados": criados,
        "erros": total - criados,
        "resultados": resultados,
    }


# ── Cópia em background (batch assíncrono) ───────────────────────────────────

_batch_copy_state: dict = {
    "rodando": False, "total": 0, "processados": 0,
    "ok": 0, "erros": 0, "pulados": 0, "log": [],
}


def _batch_copy_bg(ids: list[str]):
    global _batch_copy_state
    import time as _t
    _batch_copy_state.update({
        "rodando": True, "total": len(ids),
        "processados": 0, "ok": 0, "erros": 0, "pulados": 0, "log": [],
    })
    try:
        tok_s = _token_shinsei()
        tok_a = _token_akg()
    except Exception as e:
        _batch_copy_state.update({"rodando": False, "erro": str(e)})
        return

    # Expande famílias MLBU: se um MLB filho pertence a uma família omni,
    # busca todos os irmãos. Deduplica pelo family_id para não processar
    # a mesma família duas vezes caso múltiplos filhos estejam na lista.
    expanded: list[str] = []
    seen_families: set[str] = set()
    for raw in ids:
        eid = _extract_id(raw)
        try:
            filhos = _get_family_items(eid, tok_s)
            if len(filhos) > 1 or (len(filhos) == 1 and filhos[0] != eid):
                # Pega o family_id real para deduplicação
                r0 = _req.get(f"{ML_API}/items/{filhos[0]}",
                              params={"attributes": "family_id"},
                              headers=_hdrs(tok_s), timeout=10)
                fid = r0.json().get("family_id", eid) if r0.status_code == 200 else eid
                if fid not in seen_families:
                    seen_families.add(fid)
                    expanded.extend(filhos)
                    _batch_copy_state["log"].append(f"EXPAND:{eid}→{len(filhos)} filhos (family {fid})")
                else:
                    _batch_copy_state["log"].append(f"SKIP_FAM:{eid} (family {fid} já expandida)")
                continue
        except Exception as e:
            _batch_copy_state["log"].append(f"EXPAND_ERR:{eid}:{str(e)[:50]}")
        expanded.append(eid)

    _batch_copy_state["total"] = len(expanded)
    copy_map = _load_copy_map()

    for i, raw in enumerate(expanded):
        mlb = _extract_id(raw)
        if mlb in copy_map:
            _batch_copy_state["pulados"] += 1
            _batch_copy_state["processados"] = i + 1
            continue
        try:
            item = _get_item(mlb, tok_s)
            if not item or item.get("error"):
                _batch_copy_state["erros"] += 1
                _batch_copy_state["log"].append(f"NF:{mlb}")
                _batch_copy_state["processados"] = i + 1
                continue
            descricao = _get_description(mlb, tok_s)
            _t.sleep(0.3)
            payload = _build_payload(item)
            res = _criar_item_akg(payload, tok_a)
            akg_id = res.get("id") or (res.get("body") or {}).get("id")
            if akg_id and res.get("status_code") in (200, 201):
                _save_copy_map_entry(mlb, akg_id)
                if descricao:
                    _criar_description_akg(akg_id, descricao, tok_a)
                try:
                    _fix_variation_pictures(akg_id, payload, tok_a)
                except Exception as _pe:
                    logger.warning("fix_var_pics %s: %s", akg_id, _pe)
                _batch_copy_state["ok"] += 1
                _batch_copy_state["log"].append(f"OK:{mlb}->{akg_id}")
            else:
                _batch_copy_state["erros"] += 1
                _causes = (res.get("body") or {}).get("cause") or []
                _cs = ",".join(f"{c.get('cause_id')}({c.get('type','?')})" for c in _causes[:6])
                _batch_copy_state["log"].append(f"ERR:{mlb}:sc={res.get('status_code')} [{_cs}] {(res.get('body') or {}).get('message','')}")
        except Exception as e:
            _batch_copy_state["erros"] += 1
            _batch_copy_state["log"].append(f"EXC:{mlb}:{str(e)[:60]}")
        _batch_copy_state["processados"] = i + 1
        if len(_batch_copy_state["log"]) > 200:
            _batch_copy_state["log"] = _batch_copy_state["log"][-200:]
        _t.sleep(0.5)

    _batch_copy_state["rodando"] = False


from fastapi import BackgroundTasks as _BG


def _scroll_shinsei_ids(tok: str, seller_id: str) -> list[str]:
    """Coleta TODOS os IDs ativos Shinsei via scroll (sem limite de offset)."""
    ids: list[str] = []
    scroll_id = None
    while True:
        params: dict = {"status": "active", "limit": 100, "search_type": "scan"}
        if scroll_id:
            params["scroll_id"] = scroll_id
        r = _req.get(f"{ML_API}/users/{seller_id}/items/search",
                     params=params, headers=_hdrs(tok), timeout=30)
        if r.status_code != 200:
            break
        data = r.json()
        batch = data.get("results", [])
        scroll_id = data.get("scroll_id")
        ids.extend(batch)
        if not batch or not scroll_id:
            break
    return ids


def _resolve_skus_to_ml_ids(skus: set[str], tok: str, seller_id: str) -> list[str]:
    """Mapeia SKUs Bling para IDs ML buscando seller_custom_field em cada item ativo."""
    all_ids = _scroll_shinsei_ids(tok, seller_id)
    matched: list[str] = []
    # Busca em lotes de 20
    for i in range(0, len(all_ids), 20):
        chunk = all_ids[i:i+20]
        ids_str = ",".join(chunk)
        r = _req.get(f"{ML_API}/items",
                     params={"ids": ids_str, "attributes": "id,seller_custom_field"},
                     headers=_hdrs(tok), timeout=20)
        if r.status_code != 200:
            continue
        for entry in r.json():
            body = entry.get("body") or {}
            sku = body.get("seller_custom_field") or ""
            mlb = body.get("id") or ""
            if sku and mlb and sku in skus:
                matched.append(mlb)
        time.sleep(0.2)
    return matched


def _batch_from_faltando_bg(limit: int | None, family_only: bool = False, sort_by_family: bool = False):
    """Background: lê _faltando_akg_capilares.json, resolve IDs ML e copia.
    family_only=True: copia apenas itens omni (com family_name/variações).
    sort_by_family=True: processa famílias maiores primeiro (maior risco primeiro).
    """
    global _batch_copy_state
    DATA_DIR = BASE_DIR / "data"
    faltando_path = DATA_DIR / "_faltando_akg_capilares.json"

    _batch_copy_state.update({
        "rodando": True, "total": 0, "processados": 0,
        "ok": 0, "erros": 0, "pulados": 0, "log": ["Iniciando cópia faltando AKG..."],
    })

    try:
        tok_s = _token_shinsei()
        tok_a = _token_akg()
    except Exception as e:
        _batch_copy_state.update({"rodando": False, "erro": str(e)})
        return

    if not faltando_path.exists():
        _batch_copy_state.update({"rodando": False, "erro": "arquivo _faltando_akg_capilares.json não encontrado"})
        return

    faltando = json.loads(faltando_path.read_text(encoding="utf-8"))
    skus = {item["sku"] for item in faltando}

    _batch_copy_state["log"].append(f"Resolvendo {len(skus)} SKUs → IDs ML...")
    me_r = _req.get(f"{ML_API}/users/me", headers=_hdrs(tok_s), timeout=15)
    seller_id = str(me_r.json()["id"])

    ml_ids = _resolve_skus_to_ml_ids(skus, tok_s, seller_id)
    _batch_copy_state["log"].append(f"{len(ml_ids)} IDs ML encontrados para {len(skus)} SKUs")

    if not ml_ids:
        _batch_copy_state.update({"rodando": False, "erro": "Nenhum ID ML encontrado"})
        return

    # Filtro family_only: busca detalhes e mantém só os que têm family_name
    if family_only:
        _batch_copy_state["log"].append("Filtrando apenas itens com variações (family_name)...")
        family_ids: list[str] = []
        for i in range(0, len(ml_ids), 20):
            chunk = ml_ids[i:i+20]
            r = _req.get(f"{ML_API}/items",
                         params={"ids": ",".join(chunk), "attributes": "id,family_name"},
                         headers=_hdrs(tok_s), timeout=20)
            if r.status_code != 200:
                continue
            for entry in r.json():
                body = entry.get("body") or {}
                if body.get("family_name"):
                    family_ids.append(body["id"])
            time.sleep(0.2)
        _batch_copy_state["log"].append(f"{len(family_ids)} itens com variações encontrados")
        ml_ids = family_ids

    # Ordena pelo tamanho da família (maiores primeiro) antes de aplicar limit
    # Usa batch GET de family_id (~46 chamadas para 905 IDs) — eficiente
    if sort_by_family and ml_ids:
        _batch_copy_state["log"].append("Ordenando famílias por tamanho (maiores primeiro)...")
        fam_of: dict[str, str] = {}
        fam_count: dict[str, int] = {}
        for i in range(0, len(ml_ids), 20):
            chunk = ml_ids[i:i+20]
            r = _req.get(f"{ML_API}/items",
                         params={"ids": ",".join(chunk), "attributes": "id,family_id"},
                         headers=_hdrs(tok_s), timeout=20)
            if r.status_code == 200:
                for entry in r.json():
                    body = entry.get("body") or {}
                    mid = body.get("id")
                    fid = body.get("family_id") or mid or ""
                    if mid:
                        fam_of[mid] = fid
                        fam_count[fid] = fam_count.get(fid, 0) + 1
            time.sleep(0.2)
        ml_ids = sorted(ml_ids, key=lambda x: fam_count.get(fam_of.get(x, x), 1), reverse=True)
        top5 = sorted(fam_count.items(), key=lambda x: x[1], reverse=True)[:5]
        _batch_copy_state["log"].append(f"Top 5 famílias: {top5}")

    if limit:
        ml_ids = ml_ids[:limit]

    # Reutiliza _batch_copy_bg com os IDs resolvidos
    _batch_copy_bg(ml_ids)


@router.post("/copiar-ml/batch-faltando")
def batch_copy_faltando(bg: _BG, body: dict = {}):
    """Copia todos os SKUs de _faltando_akg_capilares.json para a AKG.
    Body opcional: {\"limit\": 50} para testar com subconjunto.
    {\"family_only\": true} para clonar apenas itens com variações (omni).
    {\"sort_by_family\": true} para processar famílias maiores primeiro.
    {\"limit\": 1, \"family_only\": true} para clonar um de cada vez."""
    if _batch_copy_state.get("rodando"):
        return {"ok": False, "msg": "Batch já em andamento"}
    limit = body.get("limit") if body else None
    family_only = bool(body.get("family_only")) if body else False
    sort_by_family = bool(body.get("sort_by_family")) if body else False
    bg.add_task(_batch_from_faltando_bg, limit, family_only, sort_by_family)
    return {"ok": True, "msg": "Iniciando cópia de faltando AKG em background",
            "family_only": family_only, "limit": limit, "sort_by_family": sort_by_family}


@router.post("/copiar-ml/batch")
def batch_copy(body: dict, bg: _BG):
    """Copia lista de IDs Shinsei → AKG em background. Body: {\"ids\": [...]}"""
    if _batch_copy_state.get("rodando"):
        return {"ok": False, "msg": "Batch já em andamento"}
    ids = body.get("ids") or []
    if not ids:
        return {"ok": False, "msg": "ids vazio"}
    bg.add_task(_batch_copy_bg, ids)
    return {"ok": True, "msg": f"Copiando {len(ids)} itens em background"}


@router.get("/copiar-ml/batch/status")
def batch_copy_status():
    return _batch_copy_state


# ── Correção de títulos duplicados ───────────────────────────────────────────

@router.post("/copiar-ml/fix-hair-tone-akg")
def fix_hair_tone_akg(bg: _BG):
    """Remove HAIR_TONE e MANUAL_TITLE de itens AKG do copy_map."""
    copy_map = _load_copy_map()
    akg_ids = list(copy_map.values())
    if not akg_ids:
        return {"ok": False, "msg": "copy_map vazio"}
    bg.add_task(_fix_hair_tone_bg, akg_ids)
    return {"ok": True, "msg": f"Corrigindo {len(akg_ids)} itens AKG em background", "total": len(akg_ids)}


@router.post("/copiar-ml/fix-hair-tone-akg-all")
def fix_hair_tone_akg_all(bg: _BG):
    """Escaneia TODOS os itens AKG ativos e remove HAIR_TONE/MANUAL_TITLE (corrige título duplicado)."""
    bg.add_task(_fix_hair_tone_bg, [], True)
    return {"ok": True, "msg": "Escaneando TODOS os itens AKG ativos em background"}


_fix_state: dict = {}


def _fix_hair_tone_bg(akg_ids: list[str], scan_all: bool = False):
    global _fix_state
    _fix_state = {"rodando": True, "total": len(akg_ids), "ok": 0, "erros": 0, "sem_hair_tone": 0, "log": []}
    try:
        tok_a = _token_akg()
    except Exception as e:
        _fix_state.update({"rodando": False, "erro": str(e)})
        return

    if scan_all:
        # Busca TODOS os IDs ativos da conta AKG via scroll
        _fix_state["log"].append("Coletando IDs AKG via scroll...")
        r_me = _req.get(f"{ML_API}/users/me", headers=_hdrs(tok_a), timeout=15)
        akg_seller_id = str(r_me.json()["id"])
        all_ids: list[str] = []
        scroll_id = None
        while True:
            params: dict = {"status": "active", "limit": 100, "search_type": "scan"}
            if scroll_id:
                params["scroll_id"] = scroll_id
            r = _req.get(f"{ML_API}/users/{akg_seller_id}/items/search",
                         params=params, headers=_hdrs(tok_a), timeout=30)
            if r.status_code != 200:
                break
            data = r.json()
            batch = data.get("results", [])
            scroll_id = data.get("scroll_id")
            all_ids.extend(batch)
            if not batch or not scroll_id:
                break
        akg_ids = all_ids
        _fix_state["total"] = len(akg_ids)
        _fix_state["log"].append(f"{len(akg_ids)} itens AKG ativos encontrados")

    null_attrs = [
        {"id": "HAIR_TONE", "value_name": None},
        {"id": "MANUAL_TITLE", "value_name": None},
    ]

    for akg_id in akg_ids:
        try:
            # Verifica se tem HAIR_TONE antes de PUT
            r = _req.get(f"{ML_API}/items/{akg_id}",
                         params={"attributes": "id,attributes"},
                         headers=_hdrs(tok_a), timeout=15)
            if r.status_code != 200:
                _fix_state["erros"] += 1
                continue

            item_attrs = r.json().get("attributes", [])
            has_hair = any(a.get("id") == "HAIR_TONE" for a in item_attrs)
            has_manual = any(a.get("id") == "MANUAL_TITLE" for a in item_attrs)

            if not has_hair and not has_manual:
                _fix_state["sem_hair_tone"] += 1
                continue

            rp = _req.put(f"{ML_API}/items/{akg_id}",
                          json={"attributes": null_attrs},
                          headers=_hdrs(tok_a), timeout=15)
            if rp.status_code in (200, 201):
                _fix_state["ok"] += 1
                _fix_state["log"].append(f"OK:{akg_id}")
            else:
                _fix_state["erros"] += 1
                _fix_state["log"].append(f"ERR:{akg_id}:{rp.text[:80]}")
        except Exception as e:
            _fix_state["erros"] += 1
            _fix_state["log"].append(f"EXC:{akg_id}:{str(e)[:50]}")
        import time as _t; _t.sleep(0.3)

    _fix_state["rodando"] = False


@router.get("/copiar-ml/fix-hair-tone-akg/status")
def fix_hair_tone_status():
    return _fix_state


@router.post("/copiar-ml/fix-add-hair-tone")
def fix_add_hair_tone(bg: _BG):
    """Para itens do copy_map que estão sem HAIR_TONE na AKG mas têm no Shinsei: adiciona."""
    copy_map = _load_copy_map()
    if not copy_map:
        return {"ok": False, "msg": "copy_map vazio"}
    bg.add_task(_fix_add_hair_tone_bg, copy_map)
    return {"ok": True, "msg": f"Verificando {len(copy_map)} pares para adicionar HAIR_TONE", "total": len(copy_map)}


_fix_add_state: dict = {}


def _fix_add_hair_tone_bg(copy_map: dict[str, str]):
    global _fix_add_state
    _fix_add_state = {"rodando": True, "total": len(copy_map), "adicionados": 0, "sem_ht_shinsei": 0, "ja_ok": 0, "erros": 0, "log": []}
    try:
        tok_s = _token_shinsei()
        tok_a = _token_akg()
    except Exception as e:
        _fix_add_state.update({"rodando": False, "erro": str(e)})
        return

    for shin_id, akg_id in copy_map.items():
        import time as _t
        try:
            # Busca HAIR_TONE do item Shinsei
            rs = _req.get(f"{ML_API}/items/{shin_id}",
                          params={"attributes": "id,attributes,family_name"},
                          headers=_hdrs(tok_s), timeout=15)
            if rs.status_code != 200:
                _fix_add_state["erros"] += 1
                continue

            shin_data = rs.json()
            shin_attrs = shin_data.get("attributes") or []
            family_name = shin_data.get("family_name") or ""
            hair_tone_attr = next((a for a in shin_attrs if a.get("id") == "HAIR_TONE"), None)

            if not hair_tone_attr or not hair_tone_attr.get("value_name"):
                _fix_add_state["sem_ht_shinsei"] += 1
                continue

            ht_val = hair_tone_attr.get("value_name", "")
            ht_vid = hair_tone_attr.get("value_id")

            # Verifica se family_name já tem o código da cor (não deve adicionar HAIR_TONE)
            import re as _re2
            fn_upper = family_name.upper()
            ht_code_m = _re2.match(r'([\d\.]+)', ht_val.strip())
            ht_code = ht_code_m.group(1) if ht_code_m else ""
            if ht_code and ht_code in fn_upper:
                _fix_add_state["sem_ht_shinsei"] += 1
                continue

            # Verifica se AKG já tem HAIR_TONE
            ra = _req.get(f"{ML_API}/items/{akg_id}",
                          params={"attributes": "id,attributes"},
                          headers=_hdrs(tok_a), timeout=15)
            if ra.status_code != 200:
                _fix_add_state["erros"] += 1
                continue

            akg_attrs = ra.json().get("attributes") or []
            akg_ht = next((a for a in akg_attrs if a.get("id") == "HAIR_TONE"), None)
            if akg_ht and akg_ht.get("value_name"):
                _fix_add_state["ja_ok"] += 1
                continue

            # Adiciona HAIR_TONE ao AKG
            ht_payload = {"id": "HAIR_TONE", "value_name": ht_val}
            if ht_vid:
                ht_payload["value_id"] = ht_vid
            rp = _req.put(f"{ML_API}/items/{akg_id}",
                          json={"attributes": [ht_payload]},
                          headers=_hdrs(tok_a), timeout=15)
            if rp.status_code in (200, 201):
                _fix_add_state["adicionados"] += 1
                _fix_add_state["log"].append(f"OK:{shin_id}->{akg_id} ht='{ht_val[:30]}'")
            else:
                _fix_add_state["erros"] += 1
                _fix_add_state["log"].append(f"ERR:{akg_id}:{rp.text[:60]}")
        except Exception as e:
            _fix_add_state["erros"] += 1
        _t.sleep(0.25)

    _fix_add_state["rodando"] = False


@router.get("/copiar-ml/fix-add-hair-tone/status")
def fix_add_hair_tone_status():
    return _fix_add_state


# ── Fechar itens do piloto (criados com código antigo/errado) ─────────────────

_fechar_piloto_state: dict = {}


@router.post("/copiar-ml/fechar-piloto")
def fechar_piloto(bg: _BG):
    """Fecha todos os itens AKG do piloto (prefixo MLB750/MLB512) e limpa do copy_map.
    Esses itens foram criados antes das regras corretas e precisam ser recriados.
    """
    copy_map = _load_copy_map()
    piloto_ids = {k: v for k, v in copy_map.items()
                  if v.startswith("MLB750") or v.startswith("MLB512")}
    if not piloto_ids:
        return {"ok": False, "msg": "Nenhum item de piloto encontrado no copy_map"}
    bg.add_task(_fechar_piloto_bg, piloto_ids)
    return {"ok": True, "total": len(piloto_ids),
            "msg": f"Fechando {len(piloto_ids)} itens do piloto em background"}


def _fechar_piloto_bg(piloto_ids: dict[str, str]):
    global _fechar_piloto_state
    _fechar_piloto_state = {
        "rodando": True, "total": len(piloto_ids),
        "fechados": 0, "erros": 0, "log": []
    }
    try:
        tok_a = _token_akg()
    except Exception as e:
        _fechar_piloto_state.update({"rodando": False, "erro": str(e)})
        return

    copy_map = _load_copy_map()

    for shin_id, akg_id in piloto_ids.items():
        try:
            rp = _req.put(
                f"{ML_API}/items/{akg_id}",
                json={"status": "closed"},
                headers=_hdrs(tok_a), timeout=15
            )
            if rp.status_code in (200, 201):
                _fechar_piloto_state["fechados"] += 1
                _fechar_piloto_state["log"].append(f"OK:fechado:{akg_id}")
                copy_map.pop(shin_id, None)
            else:
                body = rp.text[:80]
                _fechar_piloto_state["erros"] += 1
                _fechar_piloto_state["log"].append(f"ERR:{akg_id}:{rp.status_code}:{body}")
        except Exception as e:
            _fechar_piloto_state["erros"] += 1
            _fechar_piloto_state["log"].append(f"EXC:{akg_id}:{str(e)[:60]}")
        time.sleep(0.3)

    _save_copy_map(copy_map)
    _fechar_piloto_state["rodando"] = False
    _fechar_piloto_state["copy_map_restante"] = len(copy_map)


@router.get("/copiar-ml/fechar-piloto/status")
def fechar_piloto_status():
    return _fechar_piloto_state


# ── Fechar TODOS os itens AKG (reset completo) ────────────────────────────────

_fechar_tudo_state: dict = {}


@router.post("/copiar-ml/fechar-todos-akg")
def fechar_todos_akg(bg: _BG):
    """Fecha TODOS os itens ativos e pausados da conta AKG via scroll.
    Limpa copy_map completamente ao final.
    Usar antes de recriar tudo do zero.
    """
    _fechar_tudo_state.clear()
    bg.add_task(_fechar_todos_akg_bg)
    return {"ok": True, "msg": "Fechando todos os itens AKG em background — acompanhe em /fechar-todos-akg/status"}


def _fechar_todos_akg_bg():
    global _fechar_tudo_state
    _fechar_tudo_state = {
        "rodando": True, "coletados": 0,
        "fechados": 0, "erros": 0, "log": []
    }
    try:
        tok_a = _token_akg()
        # Verifica que o token é realmente AKG
        rme = _req.get(f"{ML_API}/users/me", headers=_hdrs(tok_a), timeout=10)
        me_id = rme.json().get("id")
        if str(me_id) != "3541432733":
            _fechar_tudo_state.update({
                "rodando": False,
                "erro": f"Token AKG está apontando para user_id={me_id} (esperado 3541432733). Abortar."
            })
            return
    except Exception as e:
        _fechar_tudo_state.update({"rodando": False, "erro": str(e)})
        return

    # Coleta todos os IDs via scroll (active + paused)
    all_ids: list[str] = []
    for status in ("active", "paused"):
        scroll_id = None
        while True:
            params = {"status": status, "limit": 100, "search_type": "scan"}
            if scroll_id:
                params["scroll_id"] = scroll_id
            r = _req.get(f"{ML_API}/users/3541432733/items/search",
                         params=params, headers=_hdrs(tok_a), timeout=20)
            if r.status_code != 200:
                break
            data = r.json()
            ids = data.get("results", [])
            if not ids:
                break
            all_ids.extend(ids)
            _fechar_tudo_state["coletados"] = len(all_ids)
            scroll_id = data.get("scroll_id")
            if not scroll_id:
                break
            time.sleep(0.2)

    _fechar_tudo_state["coletados"] = len(all_ids)

    # Fecha todos
    for item_id in all_ids:
        try:
            rp = _req.put(f"{ML_API}/items/{item_id}",
                          json={"status": "closed"},
                          headers=_hdrs(tok_a), timeout=15)
            if rp.status_code in (200, 201):
                _fechar_tudo_state["fechados"] += 1
            else:
                _fechar_tudo_state["erros"] += 1
                _fechar_tudo_state["log"].append(f"ERR:{item_id}:{rp.status_code}:{rp.text[:50]}")
        except Exception as e:
            _fechar_tudo_state["erros"] += 1
            _fechar_tudo_state["log"].append(f"EXC:{item_id}:{str(e)[:40]}")
        time.sleep(0.3)

    # Limpa copy_map
    _save_copy_map({})
    _fechar_tudo_state["rodando"] = False
    _fechar_tudo_state["copy_map_limpo"] = True


@router.get("/copiar-ml/fechar-todos-akg/status")
def fechar_todos_akg_status():
    return _fechar_tudo_state


@router.get("/copiar-ml/copy-map")
def export_copy_map():
    """Retorna o copy_map completo {shinsei_id: akg_id} persistido em disco."""
    return _load_copy_map()


@router.post("/copiar-ml/rebuild-copy-map")
def rebuild_copy_map_endpoint(bg: _BG):
    """Reconstrói o copy_map escaneando todos os itens AKG ativos e cruzando com Shinsei."""
    bg.add_task(_rebuild_copy_map_bg)
    return {"ok": True, "msg": "Reconstruindo copy_map em background"}


_rebuild_state: dict = {}


def _rebuild_copy_map_bg():
    """Escaneia AKG (seller_custom_field) e Shinsei (seller_custom_field) e reconstrói o map."""
    global _rebuild_state
    _rebuild_state = {"rodando": True, "total_akg": 0, "mapeados": 0, "log": []}
    try:
        tok_s = _token_shinsei()
        tok_a = _token_akg()
    except Exception as e:
        _rebuild_state.update({"rodando": False, "erro": str(e)})
        return

    # Coleta AKG: {seller_custom_field: akg_id}
    r_me_a = _req.get(f"{ML_API}/users/me", headers=_hdrs(tok_a), timeout=15)
    akg_seller_id = str(r_me_a.json()["id"])
    akg_by_sku: dict[str, str] = {}
    scroll_id = None
    _rebuild_state["log"].append("Coletando itens AKG...")
    while True:
        params: dict = {"status": "active", "limit": 100, "search_type": "scan"}
        if scroll_id:
            params["scroll_id"] = scroll_id
        r = _req.get(f"{ML_API}/users/{akg_seller_id}/items/search",
                     params=params, headers=_hdrs(tok_a), timeout=30)
        if r.status_code != 200:
            break
        data = r.json()
        batch = data.get("results", [])
        scroll_id = data.get("scroll_id")
        if not batch:
            break
        # Busca seller_custom_field em lotes de 20
        for i in range(0, len(batch), 20):
            chunk = batch[i:i+20]
            rr = _req.get(f"{ML_API}/items",
                          params={"ids": ",".join(chunk), "attributes": "id,seller_custom_field"},
                          headers=_hdrs(tok_a), timeout=20)
            if rr.status_code == 200:
                for entry in rr.json():
                    body = entry.get("body") or {}
                    sku = body.get("seller_custom_field") or ""
                    mlb = body.get("id") or ""
                    if sku and mlb:
                        akg_by_sku[sku] = mlb
            import time as _t; _t.sleep(0.15)
        if not scroll_id:
            break

    _rebuild_state["total_akg"] = len(akg_by_sku)
    _rebuild_state["log"].append(f"{len(akg_by_sku)} itens AKG com SKU coletados")

    # Coleta Shinsei: {seller_custom_field: shinsei_id}
    r_me_s = _req.get(f"{ML_API}/users/me", headers=_hdrs(tok_s), timeout=15)
    shin_seller_id = str(r_me_s.json()["id"])
    _rebuild_state["log"].append("Coletando itens Shinsei...")
    new_map: dict[str, str] = {}  # {shinsei_id: akg_id}
    scroll_id = None
    while True:
        params = {"status": "active", "limit": 100, "search_type": "scan"}
        if scroll_id:
            params["scroll_id"] = scroll_id
        r = _req.get(f"{ML_API}/users/{shin_seller_id}/items/search",
                     params=params, headers=_hdrs(tok_s), timeout=30)
        if r.status_code != 200:
            break
        data = r.json()
        batch = data.get("results", [])
        scroll_id = data.get("scroll_id")
        if not batch:
            break
        for i in range(0, len(batch), 20):
            chunk = batch[i:i+20]
            rr = _req.get(f"{ML_API}/items",
                          params={"ids": ",".join(chunk), "attributes": "id,seller_custom_field"},
                          headers=_hdrs(tok_s), timeout=20)
            if rr.status_code == 200:
                for entry in rr.json():
                    body = entry.get("body") or {}
                    sku = body.get("seller_custom_field") or ""
                    shin_id = body.get("id") or ""
                    if sku and shin_id and sku in akg_by_sku:
                        new_map[shin_id] = akg_by_sku[sku]
            import time as _t; _t.sleep(0.15)
        if not scroll_id:
            break

    _rebuild_state["mapeados"] = len(new_map)
    _rebuild_state["log"].append(f"{len(new_map)} pares Shinsei→AKG mapeados")

    # Salva em disco
    _AKG_COPY_MAP_FILE.parent.mkdir(exist_ok=True)
    _AKG_COPY_MAP_FILE.write_text(json.dumps(new_map, indent=2, ensure_ascii=False), encoding="utf-8")
    _rebuild_state["log"].append(f"copy_map salvo em {_AKG_COPY_MAP_FILE}")
    _rebuild_state["rodando"] = False


@router.get("/copiar-ml/rebuild-copy-map/status")
def rebuild_copy_map_status():
    return _rebuild_state


# ── Verificação cruzada ───────────────────────────────────────────────────────

@router.get("/copiar-ml/verificar-clone")
def verificar_clone_batch(pares: str = ""):
    """Verifica se anúncios AKG são clones dos Shinsei.
    Query param: pares=MLB123:MLB456,MLB789:MLB012
    Compara: título/family_name, categoria, preço, fotos, SKU, atributos chave.
    """
    if not pares:
        return {"erro": "pares param obrigatório: MLB123:MLB456,MLB789:MLB012"}
    try:
        tok_s = _token_shinsei()
        tok_a = _token_akg()
    except Exception as e:
        return {"erro": str(e)}

    resultados = []
    for par in pares.split(","):
        parts = par.strip().split(":")
        if len(parts) != 2:
            continue
        shin_id = _extract_id(parts[0])
        akg_id = _extract_id(parts[1])
        try:
            rs = _get_item(shin_id, tok_s)
            ra = _get_item(akg_id, tok_a)
        except Exception as e:
            resultados.append({"par": par, "erro": str(e)})
            continue

        diffs = []
        # Título / family_name
        s_title = rs.get("title", "")
        a_title = ra.get("title", "")
        s_fn = (rs.get("family_name") or "").replace(" + ", " ").strip()[:60]
        a_fn = (ra.get("family_name") or "").replace(" + ", " ").strip()[:60]
        # normalize: Shinsei title/fn toggled = expected AKG value
        if s_title and a_title and s_title != a_title:
            diffs.append(f"titulo: '{s_title[:50]}' ≠ '{a_title[:50]}'")
        if s_fn and a_fn and s_fn != a_fn:
            diffs.append(f"family_name: '{s_fn}' ≠ '{a_fn}'")
        # Categoria
        if rs.get("category_id") != ra.get("category_id"):
            diffs.append(f"categoria: {rs.get('category_id')} ≠ {ra.get('category_id')}")
        # SKU
        s_sku = rs.get("seller_custom_field", "")
        a_sku = ra.get("seller_custom_field", "")
        if s_sku != a_sku:
            diffs.append(f"sku: '{s_sku}' ≠ '{a_sku}'")
        # Fotos
        s_pics = len(rs.get("pictures", []))
        a_pics = len(ra.get("pictures", []))
        if s_pics != a_pics:
            diffs.append(f"fotos: {s_pics} ≠ {a_pics}")
        # Atributos importantes
        s_attrs = {a["id"]: a.get("value_name", "") for a in rs.get("attributes", []) if a.get("id")}
        a_attrs = {a["id"]: a.get("value_name", "") for a in ra.get("attributes", []) if a.get("id")}
        for attr in ("BRAND", "LINE", "NET_VOLUME", "HAIR_TONE"):
            if s_attrs.get(attr) and a_attrs.get(attr) and s_attrs[attr] != a_attrs[attr]:
                diffs.append(f"attr {attr}: '{s_attrs[attr]}' ≠ '{a_attrs[attr]}'")

        resultados.append({
            "shin_id": shin_id,
            "akg_id": akg_id,
            "clone": len(diffs) == 0,
            "titulo_shin": (s_title or s_fn)[:60],
            "titulo_akg": (a_title or a_fn)[:60],
            "sku_shin": s_sku,
            "sku_akg": a_sku,
            "fotos_shin": s_pics,
            "fotos_akg": a_pics,
            "diffs": diffs,
        })
        import time as _t; _t.sleep(0.1)

    clones = sum(1 for r in resultados if r.get("clone"))
    return {
        "total": len(resultados),
        "clones": clones,
        "divergentes": len(resultados) - clones,
        "resultados": resultados,
    }


# ── Debug ────────────────────────────────────────────────────────────────────

@router.get("/copiar-ml/debug-payload/{item_id:path}")
def debug_payload(item_id: str):
    """Mostra o payload exato que seria enviado ao ML AKG para criação."""
    raw_id = _extract_id(item_id)
    try:
        tok = _token_shinsei()
    except Exception as e:
        return {"erro": str(e)}
    try:
        item = _get_item(raw_id, tok)
    except Exception as e:
        return {"erro": str(e)}
    return _build_payload(item)


@router.get("/copiar-ml/debug-post/{item_id:path}")
def debug_post(item_id: str):
    """Faz o POST real ao ML AKG via _criar_item_akg (igual ao batch) e mostra resposta completa."""
    raw_id = _extract_id(item_id)
    try:
        tok_s = _token_shinsei()
        tok_a = _token_akg()
    except Exception as e:
        return {"erro": str(e)}
    try:
        item = _get_item(raw_id, tok_s)
    except Exception as e:
        return {"erro": str(e)}
    payload = _build_payload(item)
    clean = {k: v for k, v in payload.items() if not k.startswith("_")}
    res = _criar_item_akg(payload, tok_a)
    return {"status_code": res.get("status_code"), "payload_enviado": clean, "resposta_ml": res.get("body"), "meta": {k: v for k, v in res.items() if k not in ("status_code","body")}}


@router.get("/copiar-ml/debug/{item_id:path}")
def debug_item(item_id: str):
    """Retorna campos brutos do ML para diagnóstico de estrutura MLBU/MLB."""
    raw_id = _extract_id(item_id)
    try:
        tok = _token_shinsei()
    except Exception as e:
        return {"erro": str(e)}
    r = _req.get(f"{ML_API}/items/{raw_id}", headers=_hdrs(tok), timeout=20)
    data = r.json()
    keys_interesse = [
        "id", "parent_item_id", "children_ids", "variations", "item_relations",
        "catalog_product_id", "catalog_listing", "listing_type_id",
        "family_name", "family_id", "seller_id", "status",
    ]
    resumo = {k: data.get(k) for k in keys_interesse}
    resumo["_keys_disponiveis"] = list(data.keys())
    return resumo


# ── Fix family_name truncado ──────────────────────────────────────────────────
# Itens criados antes do _smart_family_name() têm family_name exatamente 60 chars
# (truncado pelo [:60] bruto). Precisam ser fechados e recriados com o nome correto.

_fix_fn_state: dict = {}


@router.get("/copiar-ml/fix-family-name/diagnostico")
def fix_family_name_diagnostico():
    """
    Identifica itens AKG com family_name de exatamente 60 chars (truncados pelo bug antigo).
    Cruza com o Shinsei para mostrar o nome correto que deveria ter.
    """
    copy_map = _load_copy_map()  # {shinsei_id: akg_id}
    if not copy_map:
        return {"ok": False, "erro": "copy_map vazio — rode /copiar-ml/rebuild-copy-map primeiro"}

    try:
        tok_a = _token_akg()
        tok_s = _token_shinsei()
    except Exception as e:
        return {"ok": False, "erro": str(e)}

    akg_ids = list(copy_map.values())
    truncados = []

    for i in range(0, len(akg_ids), 20):
        chunk = akg_ids[i:i + 20]
        r = _req.get(f"{ML_API}/items",
                     params={"ids": ",".join(chunk), "attributes": "id,family_name,seller_custom_field"},
                     headers=_hdrs(tok_a), timeout=20)
        if r.status_code != 200:
            continue
        for entry in r.json():
            body = entry.get("body") or {}
            fn = body.get("family_name") or ""
            if len(fn) == 60:
                truncados.append({
                    "akg_id": body.get("id"),
                    "sku": body.get("seller_custom_field"),
                    "family_name_atual": fn,
                })
        time.sleep(0.1)

    # Cruza com Shinsei para obter o family_name correto
    akg_sku_to_shin = {v: k for k, v in copy_map.items()}  # akg_id → shinsei_id
    shin_ids_needed = [akg_sku_to_shin.get(t["akg_id"]) for t in truncados if akg_sku_to_shin.get(t["akg_id"])]
    shin_fn: dict[str, str] = {}

    for i in range(0, len(shin_ids_needed), 20):
        chunk = [s for s in shin_ids_needed[i:i + 20] if s]
        if not chunk:
            continue
        r = _req.get(f"{ML_API}/items",
                     params={"ids": ",".join(chunk), "attributes": "id,family_name"},
                     headers=_hdrs(tok_s), timeout=20)
        if r.status_code == 200:
            for entry in r.json():
                body = entry.get("body") or {}
                shin_fn[body.get("id", "")] = body.get("family_name") or ""
        time.sleep(0.1)

    for t in truncados:
        akg_id = t["akg_id"]
        shin_id = akg_sku_to_shin.get(akg_id, "")
        fn_shin = shin_fn.get(shin_id, "")
        fn_correto = _smart_family_name(fn_shin) if fn_shin else ""
        t["shinsei_id"] = shin_id
        t["family_name_correto"] = fn_correto
        t["diverge"] = fn_correto != t["family_name_atual"]

    divergentes = [t for t in truncados if t.get("diverge")]
    return {
        "ok": True,
        "total_truncados_60chars": len(truncados),
        "total_divergentes": len(divergentes),
        "amostra": divergentes[:20],
    }


@router.post("/copiar-ml/fix-family-name/executar")
def fix_family_name_executar(bg: _BG):
    """
    Fecha todos os itens AKG com family_name de exatamente 60 chars (truncados) e
    remove do copy_map para que o batch-faltando os recrie com o nome correto.
    """
    global _fix_fn_state
    copy_map = _load_copy_map()
    if not copy_map:
        return {"ok": False, "erro": "copy_map vazio"}
    _fix_fn_state = {"rodando": True, "fechados": 0, "erros": 0, "removidos_do_map": 0, "log": []}
    bg.add_task(_fix_family_name_bg, copy_map)
    return {"ok": True, "msg": "Executando fix de family_name em background"}


def _fix_family_name_bg(copy_map: dict):
    global _fix_fn_state
    try:
        tok_a = _token_akg()
    except Exception as e:
        _fix_fn_state.update({"rodando": False, "erro": str(e)})
        return

    akg_ids = list(copy_map.values())
    reverse_map = {v: k for k, v in copy_map.items()}  # akg_id → shinsei_id

    _fix_fn_state["log"].append(f"Escaneando {len(akg_ids)} itens AKG...")

    # Coleta todos os itens com family_name de 60 chars
    fn_60: list[tuple[str, str, str]] = []  # (akg_id, shinsei_id, family_name)
    for i in range(0, len(akg_ids), 20):
        chunk = akg_ids[i:i + 20]
        r = _req.get(f"{ML_API}/items",
                     params={"ids": ",".join(chunk), "attributes": "id,family_name"},
                     headers=_hdrs(tok_a), timeout=20)
        if r.status_code != 200:
            continue
        for entry in r.json():
            body = entry.get("body") or {}
            fn = body.get("family_name") or ""
            akg_id = body.get("id") or ""
            if len(fn) == 60 and akg_id:
                fn_60.append((akg_id, reverse_map.get(akg_id, ""), fn))
        time.sleep(0.1)

    # Detecta family_names compartilhados por múltiplos itens = família de kit — NÃO fechar
    from collections import Counter
    fn_counts = Counter(fn for _, _, fn in fn_60)
    to_close = [(shin_id, akg_id, fn) for akg_id, shin_id, fn in fn_60 if fn_counts[fn] == 1]
    skipped_kit = [(akg_id, fn) for akg_id, _, fn in fn_60 if fn_counts[fn] > 1]

    _fix_fn_state["log"].append(f"{len(fn_60)} truncados total; {len(to_close)} únicos (fechar); {len(skipped_kit)} em família compartilhada (kit — pular)")
    _fix_fn_state["pulados_kit"] = len(skipped_kit)

    fresh_map = _load_copy_map()
    for shin_id, akg_id, _fn in to_close:
        try:
            r = _req.put(f"{ML_API}/items/{akg_id}",
                         json={"status": "closed"},
                         headers=_hdrs(tok_a), timeout=15)
            if r.status_code in (200, 201):
                _fix_fn_state["fechados"] += 1
                fresh_map.pop(shin_id, None)
                _fix_fn_state["removidos_do_map"] += 1
                _fix_fn_state["log"].append(f"OK:{akg_id}")
            else:
                _fix_fn_state["erros"] += 1
                _fix_fn_state["log"].append(f"ERR:{akg_id}:{r.status_code}:{r.text[:60]}")
        except Exception as e:
            _fix_fn_state["erros"] += 1
            _fix_fn_state["log"].append(f"EXC:{akg_id}:{str(e)[:60]}")
        time.sleep(0.3)

    _save_copy_map(fresh_map)
    _fix_fn_state.update({
        "rodando": False,
        "copy_map_restante": len(fresh_map),
        "proximo_passo": "Execute /copiar-ml/batch-faltando para recriar os itens fechados",
    })


@router.get("/copiar-ml/fix-family-name/status")
def fix_family_name_status():
    return _fix_fn_state


# ── Página HTML ───────────────────────────────────────────────────────────────

@router.get("/copiar-ml", response_class=HTMLResponse)
def copiar_ml_page():
    f = PAGES_DIR / "copiar_ml.html"
    if f.exists():
        return HTMLResponse(f.read_text(encoding="utf-8"))
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="pages/copiar_ml.html não encontrado.")
