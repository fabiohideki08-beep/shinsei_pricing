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


def _build_payload(item: dict) -> dict:
    """
    Monta o payload para criar o anúncio na conta AKG.
    Sempre cria anúncio TRADICIONAL (não-catálogo): nunca inclui
    catalog_product_id e força catalog_listing=false.
    """

    # Fotos: passa as URLs originais — ML re-hospeda automaticamente
    pictures = [{"source": p["url"]} for p in (item.get("pictures") or []) if p.get("url")]

    # Variações com seller_custom_field (SKU)
    # Remove picture_ids pois os IDs são da conta Shinsei e não existem na AKG
    variacoes = []
    for v in (item.get("variations") or []):
        var = {
            "attribute_combinations": v.get("attribute_combinations") or [],
            "price":                  v.get("price") or item.get("price"),
            "available_quantity":     v.get("available_quantity") or 0,
            "seller_custom_field":    v.get("seller_custom_field") or "",
        }
        variacoes.append(var)

    # Atributos — remove campos que o ML rejeita na criação (somente-leitura do sistema)
    # BRAND é obrigatório em MLB264861 — NÃO remover
    # GTIN incluído: obrigatório para MLB264861. Se conflitar (mesmo GTIN já na Shinsei),
    # _criar_item_akg faz retry automático sem GTIN.
    SKIP_ATTR_IDS = {
        "SELLER_SKU", "ITEM_CONDITION", "SELLER_ID", "CATALOG_LISTING",
        "GIFTABLE", "SELLER_PACKAGE_TYPE",
    }
    atributos = [
        {"id": a["id"], "value_name": a.get("value_name")}
        for a in (item.get("attributes") or [])
        if a.get("id") and a.get("id") not in SKIP_ATTR_IDS and a.get("value_name")
    ]

    # listing_type_id: garante tipo tradicional (gold_special ou gold_pro)
    # Nunca usa gold_premium pois esse força catálogo em algumas categorias
    listing_type = item.get("listing_type_id") or "gold_special"
    if listing_type not in ("gold_special", "gold_pro", "bronze", "free"):
        listing_type = "gold_special"

    # Para anúncios omni (family_name), title é adicionado depois se não houver family_name
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
        # Força modo tradicional — nunca catálogo
        "catalog_listing":     False,
    }

    # family_name é obrigatório para categorias Omni (ex: colorações)
    # Quando presente, NÃO enviar title — o ML usa family_name como título automaticamente
    family_name = item.get("family_name")
    if family_name:
        payload["family_name"] = family_name
    else:
        payload["title"] = _title

    if variacoes:
        payload["variations"] = variacoes

    # Frete
    shipping = item.get("shipping") or {}
    if shipping:
        payload["shipping"] = {
            "mode":          shipping.get("mode", "me2"),
            "local_pick_up": shipping.get("local_pick_up", False),
            "free_shipping": shipping.get("free_shipping", False),
            "logistic_type": shipping.get("logistic_type", "fulfillment"),
        }

    return payload


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
    r = _req.post(f"{ML_API}/items", headers=_hdrs(token),
                  json=payload, timeout=30)
    body = r.json()

    if r.status_code in (200, 201):
        return {"status_code": r.status_code, "body": body}

    if r.status_code not in (400, 422) or "attributes" not in payload:
        return {"status_code": r.status_code, "body": body}

    gtin_original = next(
        (a.get("value_name") for a in payload["attributes"] if a.get("id") == "GTIN"),
        None,
    )

    # Conflito de GTIN com outra conta → tenta com GTIN temporário
    if _gtin_conflict(body) and gtin_original:
        gtin_temp = _gerar_gtin_temp()
        payload_temp = {**payload, "attributes": [
            {"id": "GTIN", "value_name": gtin_temp} if a.get("id") == "GTIN" else a
            for a in payload["attributes"]
        ]}
        r2 = _req.post(f"{ML_API}/items", headers=_hdrs(token),
                       json=payload_temp, timeout=30)
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

    # GTIN obrigatório mas não foi enviado → tenta sem (pode travar em catch-22)
    if _gtin_missing(body):
        payload_sem = {**payload, "attributes": [
            a for a in payload["attributes"] if a.get("id") != "GTIN"
        ]}
        r3 = _req.post(f"{ML_API}/items", headers=_hdrs(token),
                       json=payload_sem, timeout=30)
        if r3.status_code in (200, 201):
            return {"status_code": r3.status_code, "body": r3.json()}

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
            payload = _build_payload(item)
            res = _criar_item_akg(payload, tok_a)
            akg_id = res.get("id") or (res.get("body") or {}).get("id")
            if akg_id and res.get("status_code") in (200, 201):
                _save_copy_map_entry(mlb, akg_id)
                _batch_copy_state["ok"] += 1
                _batch_copy_state["log"].append(f"OK:{mlb}->{akg_id}")
            else:
                _batch_copy_state["erros"] += 1
                _batch_copy_state["log"].append(f"ERR:{mlb}:{str(res)[:80]}")
        except Exception as e:
            _batch_copy_state["erros"] += 1
            _batch_copy_state["log"].append(f"EXC:{mlb}:{str(e)[:60]}")
        _batch_copy_state["processados"] = i + 1
        if len(_batch_copy_state["log"]) > 200:
            _batch_copy_state["log"] = _batch_copy_state["log"][-200:]
        _t.sleep(0.5)

    _batch_copy_state["rodando"] = False


from fastapi import BackgroundTasks as _BG


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
    """Faz o POST real ao ML AKG e mostra a resposta completa (para diagnóstico de body.invalid_fields)."""
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
    r = _req.post(f"{ML_API}/items", headers=_hdrs(tok_a), json=payload, timeout=30)
    return {"status_code": r.status_code, "payload_enviado": payload, "resposta_ml": r.json()}


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


# ── Página HTML ───────────────────────────────────────────────────────────────

@router.get("/copiar-ml", response_class=HTMLResponse)
def copiar_ml_page():
    f = PAGES_DIR / "copiar_ml.html"
    if f.exists():
        return HTMLResponse(f.read_text(encoding="utf-8"))
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="pages/copiar_ml.html não encontrado.")
