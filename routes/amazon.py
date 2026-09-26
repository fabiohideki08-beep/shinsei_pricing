"""
routes/amazon.py — Exportação de produtos Bling → Amazon SP-API
Cria listings novos (PUT) via Listings Items API 2021-08-01.
Fluxo: produto simples → productType BEAUTY/HAIR_CARE → atributos mínimos obrigatórios.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import httpx
import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Constantes SP-API ─────────────────────────────────────────────────────────
AMAZON_SP_BASE    = "https://sellingpartnerapi-na.amazon.com"
AMAZON_LWA_URL    = "https://api.amazon.com/auth/o2/token"
AMAZON_MARKETPLACE = os.getenv("AMAZON_MARKETPLACE_ID", "A2Q3Y263D00KWC")  # BR


# ── Auth LWA ──────────────────────────────────────────────────────────────────
_lwa_cache: dict = {}
_CREDS_PATH = Path(__file__).resolve().parent.parent / "data" / "credentials.json"


def _get_amazon_creds() -> tuple[str, str, str]:
    """Retorna (client_id, client_secret, refresh_token) — env vars têm prioridade, fallback em credentials.json."""
    cid  = os.getenv("AMAZON_CLIENT_ID", "").strip()
    csec = os.getenv("AMAZON_CLIENT_SECRET", "").strip()
    rtok = os.getenv("AMAZON_REFRESH_TOKEN", "").strip()
    if not (cid and csec and rtok):
        try:
            saved = json.loads(_CREDS_PATH.read_text(encoding="utf-8")).get("amazon", {})
            cid  = cid  or saved.get("AMAZON_CLIENT_ID", "").strip()
            csec = csec or saved.get("AMAZON_CLIENT_SECRET", "").strip()
            rtok = rtok or saved.get("AMAZON_REFRESH_TOKEN", "").strip()
        except Exception:
            pass
    return cid, csec, rtok


def _lwa_token() -> str:
    now = time.time()
    if _lwa_cache.get("token") and now < _lwa_cache.get("expires", 0) - 60:
        return _lwa_cache["token"]

    cid, csec, rtok = _get_amazon_creds()
    if not (cid and csec and rtok):
        raise HTTPException(status_code=400, detail="Credenciais Amazon não configuradas. Preencha na página de Integrações.")

    r = requests.post(AMAZON_LWA_URL, data={
        "grant_type":    "refresh_token",
        "refresh_token": rtok,
        "client_id":     cid,
        "client_secret": csec,
    }, timeout=20)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Amazon LWA erro {r.status_code}: {r.text[:200]}")
    d = r.json()
    _lwa_cache["token"]   = d["access_token"]
    _lwa_cache["expires"] = now + int(d.get("expires_in", 3600))
    return _lwa_cache["token"]


@router.get("/amazon/status")
async def amazon_status():
    """Verifica se as credenciais Amazon estão configuradas e funcionando."""
    cid, csec, rtok = _get_amazon_creds()
    seller_id = os.getenv("AMAZON_SELLER_ID", "") or ""
    try:
        saved = json.loads(_CREDS_PATH.read_text(encoding="utf-8")).get("amazon", {})
        seller_id = seller_id or saved.get("AMAZON_SELLER_ID", "")
    except Exception:
        pass

    if not (cid and csec and rtok):
        return {"connected": False, "status": "not_configured",
                "message": "Preencha as credenciais SP-API na página de Integrações."}
    try:
        token = _lwa_token()
        return {"connected": True, "status": "connected", "seller_id": seller_id,
                "message": "Token LWA obtido com sucesso."}
    except Exception as e:
        return {"connected": False, "status": "error", "message": str(e)}


@router.get("/amazon/auth")
async def amazon_auth():
    """Testa a conexão Amazon (LWA não usa redirect OAuth — basta ter as credenciais)."""
    cid, csec, rtok = _get_amazon_creds()
    if not (cid and csec and rtok):
        return {"ok": False, "detail": "Credenciais Amazon não configuradas. Preencha na página de Integrações."}
    try:
        _lwa_token()
        return {"ok": True, "message": "Amazon SP-API conectada com sucesso."}
    except HTTPException as e:
        return {"ok": False, "detail": e.detail}


def _amz_headers() -> dict:
    return {
        "x-amz-access-token": _lwa_token(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").replace("\xa0", " ").strip()


def _bling_token(force_refresh: bool = False) -> str:
    # 1) Tentar via BlingClient (com refresh se possivel)
    try:
        from bling_client import BlingClient
        bc = BlingClient()
        if force_refresh:
            bc._refresh_token()
        return bc._get_headers()["Authorization"].replace("Bearer ", "")
    except Exception as _e1:
        logger.debug("[BLING-TOKEN] BlingClient falhou: %s", _e1)
    # 2) Ler diretamente do arquivo criptografado (bypass da verificacao de expiracao)
    try:
        import json as _json
        from pathlib import Path as _Path
        from bling_client import _decrypt_tokens
        _raw = _json.loads((_Path(__file__).parent.parent / "data" / "bling_tokens.json").read_text())
        if "encrypted" in _raw:
            _tok = _decrypt_tokens(_raw["encrypted"]).get("access_token", "")
            if _tok:
                return _tok
            logger.warning("[BLING-TOKEN] decrypt OK mas access_token vazio")
        else:
            logger.warning("[BLING-TOKEN] bling_tokens.json sem campo 'encrypted'")
    except Exception as _e2:
        logger.warning("[BLING-TOKEN] fallback decrypt falhou: %s", _e2)
    # 3) Secret Manager
    try:
        from token_persistence import load_bling_tokens
        t = load_bling_tokens() or {}
        tok3 = t.get("access_token", "")
        if tok3:
            return tok3
        logger.warning("[BLING-TOKEN] load_bling_tokens retornou vazio")
    except Exception as _e3:
        logger.warning("[BLING-TOKEN] load_bling_tokens falhou: %s", _e3)
    return ""


_ASIN_CACHE: dict = {}

def _buscar_asin_por_ean(ean: str) -> str:
    """Busca ASIN existente no catálogo Amazon pelo EAN/GTIN. Retorna ASIN ou ''."""
    if not ean:
        return ""
    if ean in _ASIN_CACHE:
        return _ASIN_CACHE[ean]

    def _extract_asin(items: list, ean_val: str) -> str:
        for item in items:
            for id_group in (item.get("identifiers") or []):
                for identifier in (id_group.get("identifiers") or []):
                    if identifier.get("identifier") == ean_val:
                        return item.get("asin", "")
            # fallback: aceita qualquer item se só veio 1 resultado
        if len(items) == 1:
            return items[0].get("asin", "")
        return ""

    try:
        headers = _amz_headers()
        base_params = {"marketplaceIds": AMAZON_MARKETPLACE, "includedData": "identifiers,summaries"}

        # Tentativa 1: busca por identifier type EAN (mais preciso)
        r = requests.get(
            f"{AMAZON_SP_BASE}/catalog/2022-04-01/items",
            headers=headers,
            params={**base_params, "identifiers": ean, "identifiersType": "EAN"},
            timeout=15,
        )
        if r.status_code == 200:
            items = r.json().get("items") or []
            if items:
                asin = items[0].get("asin", "")  # identifier search retorna exato
                if asin:
                    _ASIN_CACHE[ean] = asin
                    logger.info("[AMAZON-CATALOG] EAN %s → ASIN %s (identifier)", ean, asin)
                    return asin

        # Tentativa 2: busca por keyword (fallback)
        r2 = requests.get(
            f"{AMAZON_SP_BASE}/catalog/2022-04-01/items",
            headers=headers,
            params={**base_params, "keywords": ean},
            timeout=15,
        )
        if r2.status_code == 200:
            items2 = r2.json().get("items") or []
            asin = _extract_asin(items2, ean)
            if asin:
                _ASIN_CACHE[ean] = asin
                logger.info("[AMAZON-CATALOG] EAN %s → ASIN %s (keyword)", ean, asin)
                return asin

        _ASIN_CACHE[ean] = ""
        logger.debug("[AMAZON-CATALOG] EAN %s sem ASIN no catálogo", ean)
        return ""
    except Exception as exc:
        logger.debug("[AMAZON-CATALOG] erro busca EAN %s: %s", ean, exc)
        _ASIN_CACHE[ean] = ""
        return ""


def _amazon_fees(sku: str, preco: float) -> dict:
    """Consulta Product Fees API da Amazon e retorna taxas reais para um SKU/preço.
    Retorna dict: referral_fee_pct, referral_fee_valor, closing_fee, total_fees ou {} se falhar."""
    seller_id = os.getenv("AMAZON_SELLER_ID", "")
    if not seller_id or preco <= 0:
        return {}
    try:
        body = {
            "FeesEstimateRequest": {
                "MarketplaceId": AMAZON_MARKETPLACE,
                "IsAmazonFulfilled": False,
                "PriceToEstimateFees": {
                    "ListingPrice": {"CurrencyCode": "BRL", "Amount": round(preco, 2)},
                },
                "Identifier": sku,
            }
        }
        url = f"{AMAZON_SP_BASE}/products/fees/v0/listings/{sku}/feesEstimate"
        r = requests.post(url, headers=_amz_headers(), json=body, timeout=20)
        if r.status_code != 200:
            logger.warning("[AMAZON-FEES] HTTP %s para SKU=%s: %s", r.status_code, sku, r.text[:200])
            return {}
        data = r.json()
        estimate = (data.get("payload") or {}).get("FeesEstimateResult", {}).get("FeesEstimate", {})
        total_fees = float((estimate.get("TotalFeesEstimate") or {}).get("Amount", 0))
        fee_list = estimate.get("FeeDetailList") or []
        result = {"total_fees": total_fees, "detalhes": []}
        referral = 0.0
        closing  = 0.0
        for fee in fee_list:
            nome  = fee.get("FeeType", "")
            valor = float((fee.get("FeeAmount") or {}).get("Amount", 0))
            result["detalhes"].append({"tipo": nome, "valor": valor})
            if "Referral" in nome:
                referral = valor
            elif "Closing" in nome or "Fixed" in nome:
                closing = valor
        result["referral_fee_valor"] = referral
        result["referral_fee_pct"]   = round(referral / preco * 100, 2) if preco > 0 else 0
        result["closing_fee"]        = closing
        # Se a API retornou 0% (SKU não existe na Amazon ainda), usa fallback por categoria
        if result["referral_fee_pct"] == 0:
            return {}
        return result
    except Exception as exc:
        logger.warning("[AMAZON-FEES] erro para SKU=%s: %s", sku, exc)
        return {}


# Taxas de referral padrão por product_type quando o SKU não existe na Amazon
_REFERRAL_FALLBACK: dict[str, float] = {
    "BEAUTY":       13.0,
    "HAIR_CARE":    13.0,
    "SKIN_CARE":    13.0,
    "COSMETIC":     13.0,
    "EYE_SHADOW":   13.0,
    "NAIL_CARE":    13.0,
    "FRAGRANCE":    13.0,
    "HEALTH":       8.0,
    "SUPPLEMENT":   8.0,
    "DEFAULT":      13.0,
}
_CLOSING_FEE_FALLBACK = 0.0  # MFN no Brasil geralmente sem closing fee fixo


def _fees_fallback(product_type: str, preco: float) -> dict:
    """Retorna taxas estimadas quando a API não tem o SKU cadastrado."""
    pct = _REFERRAL_FALLBACK.get(product_type.upper(), _REFERRAL_FALLBACK["DEFAULT"])
    referral_valor = round(preco * pct / 100, 2)
    return {
        "referral_fee_pct":   pct,
        "referral_fee_valor": referral_valor,
        "closing_fee":        _CLOSING_FEE_FALLBACK,
        "total_fees":         referral_valor + _CLOSING_FEE_FALLBACK,
        "detalhes":           [{"tipo": "ReferralFee (estimado)", "valor": referral_valor}],
        "estimado":           True,
    }


def _calcular_preco_com_taxas_amazon(custo_produto: float, fees: dict,
                                      margem_alvo: float = 15.0,
                                      imposto_pct: float = 0.76) -> float:
    """Calcula o preço de venda a partir do custo + taxas reais Amazon + margem alvo.
    Fórmula: preco = custo / (1 - referral% - imposto% - margem%)"""
    referral_pct = fees.get("referral_fee_pct", 0) / 100
    closing      = fees.get("closing_fee", 0)
    imposto      = imposto_pct / 100
    margem       = margem_alvo / 100
    divisor = 1 - referral_pct - imposto - margem
    if divisor <= 0:
        return 0.0
    preco = (custo_produto + closing) / divisor
    # arredondamento .90
    import math
    preco_arred = math.floor(preco) + 0.90
    if preco_arred < preco:
        preco_arred += 1
    return round(preco_arred, 2)


def _preco_simulador(sku: str) -> dict:
    """Chama /integracao/preview e retorna o preço calculado para Amazon.
    Retorna dict com preco, margem, lucro, faixa_aplicada (ou vazio se falhar)."""
    try:
        base_url = os.getenv("SELF_BASE_URL", "http://localhost:8080")
        r = requests.post(
            f"{base_url}/integracao/preview",
            json={"valor_busca": sku, "criterio": "sku"},
            timeout=30,
        )
        if r.status_code != 200:
            logger.warning("[AMAZON-SIM] /integracao/preview %s → HTTP %s", sku, r.status_code)
            return {}
        data = r.json()
        mp = data.get("marketplaces") or {}
        amz = mp.get("amazon") or mp.get("Amazon") or {}
        return amz
    except Exception as exc:
        logger.warning("[AMAZON-SIM] erro ao chamar simulador para SKU=%s: %s", sku, exc)
        return {}


_REGRAS_CACHE: dict = {}
_REGRAS_PATH = Path(__file__).parent.parent / "data" / "regras.json"

def _regra_amazon(preco_estimado: float) -> dict:
    """Retorna a regra Amazon para a faixa de preço estimada."""
    global _REGRAS_CACHE
    try:
        mtime = _REGRAS_PATH.stat().st_mtime
        if _REGRAS_CACHE.get("mtime") != mtime:
            with open(_REGRAS_PATH, encoding="utf-8") as f:
                _REGRAS_CACHE = {"mtime": mtime, "regras": json.load(f)}
        regras = _REGRAS_CACHE.get("regras") or []
    except Exception:
        return {}

    amazon_regras = [
        r for r in regras
        if (r.get("canal") or "").lower() == "amazon" and r.get("ativo", True)
    ]
    if not amazon_regras:
        return {}

    candidatas = [
        r for r in amazon_regras
        if float(r.get("preco_min") or 0) <= preco_estimado <= float(r.get("preco_max") or 999999)
    ]
    return candidatas[0] if candidatas else amazon_regras[0]


def _bling_produto(produto_id: int) -> dict:
    for tentativa in range(2):
        token = _bling_token(force_refresh=(tentativa > 0))
        if not token:
            raise HTTPException(status_code=503, detail="Token Bling indisponível. Reautorize em /bling/auth.")
        with httpx.Client(timeout=30) as hx:
            r = hx.get(
                f"https://api.bling.com.br/Api/v3/produtos/{produto_id}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        if r.status_code == 401 and tentativa == 0:
            logger.warning("[BLING] Token expirado (401), renovando e tentando novamente…")
            continue
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Bling {r.status_code}: {r.text[:200]}")
        return r.json().get("data", {})
    raise HTTPException(status_code=503, detail="Token Bling inválido após renovação. Reautorize em /bling/auth.")


_ESTOQUE_PRECO_CACHE: dict = {}

def _bling_preco_ultima_entrada(produto_id: int) -> float:
    """
    Busca o preço de compra da última entrada de estoque no Bling.
    Usa cache de sessão para evitar chamadas repetidas durante o batch.
    """
    if produto_id in _ESTOQUE_PRECO_CACHE:
        return _ESTOQUE_PRECO_CACHE[produto_id]

    try:
        token = _bling_token()
        if not token:
            return 0.0
        with httpx.Client(timeout=20) as hx:
            r = hx.get(
                "https://api.bling.com.br/Api/v3/estoques",
                params={"idProduto": produto_id, "tipo": "E", "pagina": 1, "limite": 5},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        if r.status_code != 200:
            _ESTOQUE_PRECO_CACHE[produto_id] = 0.0
            return 0.0
        data = r.json().get("data") or []
        preco = 0.0
        for mov in data:
            pc = float(mov.get("precoCompra") or mov.get("precoCusto") or 0)
            if pc > 0:
                preco = pc
                break
        _ESTOQUE_PRECO_CACHE[produto_id] = preco
        return preco
    except Exception as exc:
        logger.debug("[ESTOQUE-PRECO] produto_id=%s erro=%s", produto_id, exc)
        _ESTOQUE_PRECO_CACHE[produto_id] = 0.0
        return 0.0


# ── Payload ───────────────────────────────────────────────────────────────────

class ExportarAmazonPayload(BaseModel):
    produto_id: int
    product_type: str = "BEAUTY"          # ex: BEAUTY, HAIR_CARE, EYE_SHADOW
    condition_type: str = "new_new"       # new_new, used_good, etc.
    preco_override: float = 0.0           # 0 = calcula automaticamente; >0 = força esse valor
    fulfillment_channel: str = "DEFAULT"  # DEFAULT = MFN/FBM
    usar_simulador: bool = True           # usa simulador como base (custo + margem)
    margem_alvo: float = 15.0            # margem líquida desejada (%) ao calcular com taxas reais
    titulo_override: str = ""            # se preenchido, substitui o nome do produto Bling no listing
    ean_override: str = ""               # EAN a usar no lugar do gtin do produto (para bundles/kits)
    is_bundle: bool = False              # indica que é um kit/bundle composto de múltiplos itens
    numero_itens: int = 0               # quantidade de itens no bundle (0 = não informar)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/amazon/fees/{sku}")
def amazon_fees(sku: str, preco: float = 0.0):
    """Consulta as taxas reais da Amazon para um SKU via Product Fees API."""
    if preco <= 0:
        preco = 20.0  # preço de referência para estimativa
    fees = _amazon_fees(sku, preco)
    if not fees:
        raise HTTPException(status_code=502, detail="Não foi possível obter taxas da Amazon para este SKU.")
    return {"sku": sku, "preco_referencia": preco, **fees}


@router.get("/amazon/product-types")
def amazon_buscar_product_types(keywords: str):
    """Busca productTypes Amazon BR compatíveis com as keywords."""
    params = {
        "keywords":       keywords,
        "marketplaceIds": AMAZON_MARKETPLACE,
        "locale":         "pt_BR",
    }
    r = requests.get(
        f"{AMAZON_SP_BASE}/definitions/2020-09-01/productTypes",
        headers=_amz_headers(), params=params, timeout=20,
    )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text[:400])
    return r.json()


@router.get("/amazon/product-type-schema/{product_type}")
def amazon_schema_product_type(product_type: str):
    """Retorna o schema JSON de um productType Amazon (atributos obrigatórios)."""
    params = {
        "marketplaceIds": AMAZON_MARKETPLACE,
        "requirements":   "LISTING",
        "locale":         "pt_BR",
    }
    r = requests.get(
        f"{AMAZON_SP_BASE}/definitions/2020-09-01/productTypes/{product_type}",
        headers=_amz_headers(), params=params, timeout=30,
    )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text[:400])
    d = r.json()
    # Retornar só o schema link e propriedades required para não encher o payload
    schema_url = d.get("schema", {}).get("link", {}).get("resource", "")
    return {"product_type": product_type, "schema_url": schema_url, "raw": d}


class PatchListingPayload(BaseModel):
    sku: str
    product_type: str = "BEAUTY"
    patches: list  # lista de {op, path, value}


@router.post("/amazon/patch-listing")
def amazon_patch_listing(payload: PatchListingPayload):
    """Aplica um PATCH parcial a um listing Amazon existente."""
    seller_id = os.getenv("AMAZON_SELLER_ID", "")
    if not seller_id:
        raise HTTPException(status_code=503, detail="AMAZON_SELLER_ID não configurado.")
    body = {"productType": payload.product_type, "patches": payload.patches}
    r = requests.patch(
        f"{AMAZON_SP_BASE}/listings/2021-08-01/items/{seller_id}/{payload.sku}",
        headers=_amz_headers(),
        params={"marketplaceIds": AMAZON_MARKETPLACE, "issueLocale": "pt_BR"},
        json=body,
        timeout=20,
    )
    if r.status_code not in (200, 202):
        raise HTTPException(status_code=r.status_code, detail=r.text[:500])
    return {"ok": True, "sku": payload.sku, "status": r.status_code, "response": r.json()}


@router.get("/amazon/listing-detail")
def amazon_listing_detail(sku: str):
    """Retorna detalhes e issues de um listing Amazon pelo seller SKU."""
    seller_id = os.getenv("AMAZON_SELLER_ID", "")
    if not seller_id:
        raise HTTPException(status_code=503, detail="AMAZON_SELLER_ID não configurado.")
    url = f"{AMAZON_SP_BASE}/listings/2021-08-01/items/{seller_id}/{sku}"
    params = {"marketplaceIds": AMAZON_MARKETPLACE, "includedData": "attributes,issues,summaries,fulfillmentAvailability,offers"}
    r = requests.get(url, headers=_amz_headers(), params=params, timeout=20)
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text[:500])
    d = r.json()
    issues = d.get("issues") or []
    summaries = d.get("summaries") or []
    return {
        "sku": sku,
        "status": [s.get("status") for s in summaries],
        "issues": [{"severity": i.get("severity"), "code": i.get("code"), "message": i.get("message")} for i in issues],
        "raw": d,
    }


@router.get("/amazon/buscar-bling")
def amazon_buscar_bling(nome: str = "", gtin: str = "", pagina: int = 1):
    """Busca produtos no Bling por nome ou GTIN. Útil para encontrar o produto_id."""
    token = _bling_token()
    if not token:
        raise HTTPException(status_code=503, detail="Token Bling indisponível.")
    params: dict = {"pagina": pagina, "limite": 50}
    if nome:
        params["nome"] = nome
    if gtin:
        params["gtin"] = gtin
    r = requests.get("https://api.bling.com.br/Api/v3/produtos",
                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                     params=params, timeout=20)
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text[:300])
    itens = r.json().get("data") or []
    return {"total": len(itens), "data": [
        {"id": p.get("id"), "sku": p.get("codigo"), "gtin": p.get("gtin"), "nome": p.get("nome")}
        for p in itens
    ]}


@router.post("/amazon/exportar-produto")
def amazon_exportar_produto(payload: ExportarAmazonPayload):
    """
    Cria (PUT) um novo listing na Amazon a partir de um produto Bling.
    Para produtos novos sem ASIN, a Amazon criará o ASIN após revisão.
    """
    seller_id = os.getenv("AMAZON_SELLER_ID", "")
    if not seller_id:
        raise HTTPException(status_code=503, detail="AMAZON_SELLER_ID não configurado.")

    # 1) Buscar produto no Bling
    p = _bling_produto(payload.produto_id)
    if not p.get("nome"):
        raise HTTPException(status_code=404, detail="Produto não encontrado no Bling.")

    nome    = payload.titulo_override.strip() or p.get("nome", "")
    sku     = p.get("codigo", "")
    gtin    = p.get("gtin", "") or ""
    peso_kg = float(p.get("pesoBruto") or p.get("pesoLiquido") or 0.5)
    dims    = p.get("dimensoes") or {}

    desc_raw = p.get("descricaoCurta") or p.get("descricao") or ""
    descricao = _strip_html(desc_raw)[:2000]

    midia     = p.get("midia") or {}
    imgs      = (midia.get("imagens") or {}).get("internas", [])
    img_urls  = [i.get("link", "") for i in imgs if i.get("link")]

    if not sku:
        raise HTTPException(status_code=400, detail="Produto sem SKU no Bling.")

    # 2) Preço: override manual → ou calcula com custo + taxas reais Amazon
    sim_resultado: dict = {}
    fees_reais:    dict = {}
    fonte_preco = "bling"
    preco = 0.0

    if payload.preco_override > 0:
        preco = payload.preco_override
        fonte_preco = "override"
    else:
        # 1ª opção: preço do simulador (já tem impostos, embalagem e margem das regras)
        if payload.usar_simulador:
            sim_resultado = _preco_simulador(sku)

        sim_preco = float(sim_resultado.get("preco") or sim_resultado.get("preco_promocional") or 0)
        if sim_preco > 0:
            preco = sim_preco
            fonte_preco = "simulador"
            logger.info("[AMAZON-EXPORT] SKU=%s preco=%.2f via simulador (margem=%.1f%%)",
                        sku, preco, float(sim_resultado.get("margem") or 0))
        else:
            # 2ª opção: custo do Bling/simulador + taxas Amazon + margem_alvo
            sim_raw = sim_resultado.get("raw") or {}
            fornecedor = p.get("fornecedor") or {}
            custo = float(
                fornecedor.get("precoCompra") or fornecedor.get("precoCusto")
                or sim_raw.get("custo_total") or sim_resultado.get("custo_total")
                or 0
            )
            taxa_fixa_regra = float(sim_raw.get("taxa_fixa") or sim_resultado.get("taxa_fixa") or 0)

            if custo > 0:
                custo_total_com_fixo = custo + taxa_fixa_regra
                fees_reais = _amazon_fees(sku, custo_total_com_fixo * 2.5)
                if not fees_reais:
                    fees_reais = _fees_fallback(payload.product_type, custo_total_com_fixo * 2.5)
                    fonte_preco = "taxas_estimadas_categoria"
                else:
                    fonte_preco = "taxas_reais_amazon"
                preco = _calcular_preco_com_taxas_amazon(
                    custo_produto=custo_total_com_fixo,
                    fees=fees_reais,
                    margem_alvo=payload.margem_alvo,
                )
                if not fees_reais.get("estimado"):
                    fees_reais2 = _amazon_fees(sku, preco)
                    if fees_reais2:
                        fees_reais = fees_reais2
                        preco = _calcular_preco_com_taxas_amazon(
                            custo_produto=custo_total_com_fixo,
                            fees=fees_reais,
                            margem_alvo=payload.margem_alvo,
                        )

            # 3ª opção: preço de venda do Bling como último recurso
            if preco <= 0:
                preco = float(p.get("preco") or 0)
                fonte_preco = "bling"

    if preco <= 0:
        raise HTTPException(status_code=400, detail="Produto sem preço calculado. Verifique o custo no Bling ou use preco_override.")

    logger.info("[AMAZON-EXPORT] SKU=%s preco=%.2f fonte=%s productType=%s",
                sku, preco, fonte_preco, payload.product_type)

    # 3) Resolver EAN e ASIN antes de montar atributos
    # EAN: usa ean_override (bundle), ou gtin do Bling, ou SKU numérico
    ean_efetivo = payload.ean_override.strip() or gtin or ""
    if not ean_efetivo and sku.isdigit() and 8 <= len(sku) <= 14:
        ean_efetivo = sku
    if not ean_efetivo:
        raise HTTPException(
            status_code=400,
            detail=f"Produto {sku} sem EAN/GTIN. Cadastre o código de barras no Bling antes de exportar para a Amazon.",
        )

    # ASIN direto: se o próprio SKU é um ASIN Amazon (B + 9 chars alfanuméricos)
    import re as _re
    asin_existente = ""
    if _re.match(r'^B[A-Z0-9]{9}$', sku):
        asin_existente = sku
        logger.info("[AMAZON-EXPORT] SKU %s é ASIN direto — vinculando oferta", sku)
    else:
        asin_existente = _buscar_asin_por_ean(ean_efetivo)
        if asin_existente:
            logger.info("[AMAZON-EXPORT] EAN %s → ASIN %s — vinculando oferta", ean_efetivo, asin_existente)

    # 4) Montar atributos completos do listing (obrigatório mesmo para vinculação com ASIN)
    marca_raw = p.get("marca") or {}
    marca_str = (marca_raw.get("value", "") if isinstance(marca_raw, dict) else str(marca_raw or "")).strip()
    if not marca_str:
        marca_str = (p.get("nomeMarca") or p.get("marca_nome") or nome.split()[0])

    if not descricao:
        descricao = nome

    cor_valor = ""
    variacoes = p.get("variacoes") or []
    if variacoes:
        for v in variacoes:
            for attr in (v.get("atributos") or []):
                if attr.get("nome", "").lower() in ("cor", "color"):
                    cor_valor = attr.get("valor", "")
                    break
            if cor_valor:
                break

    bullets = [l.strip() for l in descricao.split("\n") if l.strip()][:5]
    if not bullets:
        bullets = [nome[:200]]

    attributes: dict = {
        "item_name": [{"value": nome[:500], "marketplace_id": AMAZON_MARKETPLACE}],
        "brand":     [{"value": marca_str[:100], "marketplace_id": AMAZON_MARKETPLACE}],
        "condition_type": [{"value": payload.condition_type, "marketplace_id": AMAZON_MARKETPLACE}],
        "list_price": [{
            "currency": "BRL",
            "value_with_tax": round(preco, 2),
            "marketplace_id": AMAZON_MARKETPLACE,
        }],
        "purchasable_offer": [{
            "currency": "BRL",
            "our_price": [{"schedule": [{"value_with_tax": round(preco, 2)}]}],
            "marketplace_id": AMAZON_MARKETPLACE,
        }],
        "fulfillment_availability": [{
            "fulfillment_channel_code": payload.fulfillment_channel,
            "quantity": 0,
            "marketplace_id": AMAZON_MARKETPLACE,
        }],
        "product_description": [{"value": descricao[:2000], "marketplace_id": AMAZON_MARKETPLACE}],
        "bullet_point": [{"value": b[:500], "marketplace_id": AMAZON_MARKETPLACE} for b in bullets],
        **({"externally_assigned_product_identifier": [{
            "type":  "ean",
            "value": ean_efetivo,
            "marketplace_id": AMAZON_MARKETPLACE,
        }]} if ean_efetivo else {}),
        "is_heat_sensitive":        [{"value": False, "marketplace_id": AMAZON_MARKETPLACE}],
        "contains_liquid_contents": [{"value": not payload.is_bundle, "marketplace_id": AMAZON_MARKETPLACE}],
        "country_of_origin":        [{"value": "BR",  "marketplace_id": AMAZON_MARKETPLACE}],
        "manufacturer":        [{"value": marca_str or nome.split()[0], "marketplace_id": AMAZON_MARKETPLACE}],
        "unit_count":          [{"type": {"language_tag": "pt_BR", "value": "unidade"}, "value": 1, "marketplace_id": AMAZON_MARKETPLACE}],
        "target_audience_keyword": [{"value": "Adultos", "marketplace_id": AMAZON_MARKETPLACE}],
        "lifestyle":           [{"value": "Moderno", "marketplace_id": AMAZON_MARKETPLACE}],
        "power_source_type":   [{"value": "Sem energia necessária", "marketplace_id": AMAZON_MARKETPLACE}],
        "color":               [{"value": cor_valor or "Multicolor", "marketplace_id": AMAZON_MARKETPLACE}],
        "supplier_declared_dg_hz_regulation": [{"value": "not_applicable", "marketplace_id": AMAZON_MARKETPLACE}],
        "safety_warning":      [{"value": "Manter fora do alcance de crianças.", "marketplace_id": AMAZON_MARKETPLACE}],
        "generic_keyword":     [{"value": " ".join(nome.split()[:8]), "marketplace_id": AMAZON_MARKETPLACE}],
        "hair_type":           [{"value": "Todos", "marketplace_id": AMAZON_MARKETPLACE}],
        "allergen_information": [{"value": "gluten_free", "marketplace_id": AMAZON_MARKETPLACE}],
        "number_of_items":     [{"value": payload.numero_itens if payload.numero_itens > 0 else 1, "marketplace_id": AMAZON_MARKETPLACE}],
        "item_form":           [{"value": "Creme", "marketplace_id": AMAZON_MARKETPLACE}],
    }

    # Atributos adicionais para bundle/kit
    if payload.is_bundle:
        attributes["is_bundle_product"] = [{"value": True, "marketplace_id": AMAZON_MARKETPLACE}]
        attributes["item_package_quantity"] = [{"value": 1, "marketplace_id": AMAZON_MARKETPLACE}]

    if peso_kg > 0:
        attributes["item_package_weight"] = [{
            "unit": "kilograms",
            "value": round(peso_kg, 3),
            "marketplace_id": AMAZON_MARKETPLACE,
        }]

    comp_cm = int(float(dims.get("profundidade") or 0)) or 10
    larg_cm = int(float(dims.get("largura") or 0)) or 10
    alt_cm  = int(float(dims.get("altura") or 0)) or 5
    attributes["item_package_dimensions"] = [{
        "length": {"unit": "centimeters", "value": comp_cm},
        "width":  {"unit": "centimeters", "value": larg_cm},
        "height": {"unit": "centimeters", "value": alt_cm},
        "marketplace_id": AMAZON_MARKETPLACE,
    }]

    if asin_existente:
        # Produto já existe na Amazon — sugere vinculação ao ASIN existente
        attributes["merchant_suggested_asin"] = [{"value": asin_existente, "marketplace_id": AMAZON_MARKETPLACE}]
        logger.info("[AMAZON-EXPORT] SKU %s → ASIN %s: merchant_suggested_asin incluído", sku, asin_existente)
    else:
        # Produto novo — envia imagens do Bling
        if img_urls:
            attributes["main_product_image_locator"] = [{"media_location": img_urls[0], "marketplace_id": AMAZON_MARKETPLACE}]
        if len(img_urls) > 1:
            attributes["other_product_image_locator_1"] = [{"media_location": img_urls[1], "marketplace_id": AMAZON_MARKETPLACE}]
        logger.info("[AMAZON-EXPORT] EAN %s sem ASIN — criando produto novo", ean_efetivo)

    # 3) PUT listing
    body = {
        "productType": payload.product_type,
        "attributes":  attributes,
    }

    url = f"{AMAZON_SP_BASE}/listings/2021-08-01/items/{seller_id}/{sku}"
    params = {"marketplaceIds": AMAZON_MARKETPLACE}

    logger.info("[AMAZON-EXPORT] PUT %s productType=%s atributos=%s",
                url, payload.product_type, list(attributes.keys()))

    r = requests.put(url, headers=_amz_headers(), params=params, json=body, timeout=30)
    resp_body = {}
    try:
        resp_body = r.json()
    except Exception:
        pass

    logger.warning("[AMAZON-EXPORT] resp HTTP=%s body=%s", r.status_code, str(resp_body)[:500])

    if r.status_code in (200, 201):
        amazon_status = resp_body.get("status", "")
        issues        = resp_body.get("issues", [])
        erros_criticos = [i for i in issues if i.get("severity") == "ERROR"]
        if erros_criticos:
            msgs = "; ".join(i.get("message", "")[:120] for i in erros_criticos[:3])
            raise HTTPException(
                status_code=400,
                detail=f"Amazon rejeitou o listing ({amazon_status}): {msgs}",
            )
        return {
            "ok":              True,
            "sku":             sku,
            "nome":            nome,
            "status":          amazon_status,
            "submission_id":   resp_body.get("submissionId"),
            "issues":          issues,
            "product_type":    payload.product_type,
            "preco_exportado": round(preco, 2),
            "fonte_preco":     fonte_preco,
            "taxas_amazon": {
                "fonte":              "product_fees_api" if fees_reais else "nao_disponivel",
                "referral_fee_pct":   fees_reais.get("referral_fee_pct", 0),
                "referral_fee_valor": fees_reais.get("referral_fee_valor", 0),
                "closing_fee":        fees_reais.get("closing_fee", 0),
                "total_fees":         fees_reais.get("total_fees", 0),
                "detalhes":           fees_reais.get("detalhes", []),
            },
            "simulador": {
                "usado":          bool(sim_resultado),
                "margem":         sim_resultado.get("margem") or 0,
                "lucro":          sim_resultado.get("lucro") or 0,
                "faixa_aplicada": sim_resultado.get("faixa_aplicada") or "",
                "comissao_regra": sim_resultado.get("comissao") or 0,
            },
        }

    # Erro — retornar detalhes para diagnóstico
    issues = resp_body.get("errors") or resp_body.get("issues") or []
    raise HTTPException(
        status_code=400,
        detail={
            "amazon_http": r.status_code,
            "issues":      issues[:10],
            "raw":         str(resp_body)[:1000],
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# EXPORTAÇÃO EM LOTE — Bling → Amazon
# ══════════════════════════════════════════════════════════════════════════════

_LOTE_STATE_FILE = Path(__file__).parent.parent / "data" / "amazon_exportar_lote.json"


def _lote_salvar(state: dict):
    _LOTE_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _lote_carregar() -> dict:
    try:
        if _LOTE_STATE_FILE.exists():
            return json.loads(_LOTE_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _amazon_skus_existentes() -> set[str]:
    """Busca todos os SKUs já cadastrados na Amazon via Listings API."""
    seller_id = os.getenv("AMAZON_SELLER_ID", "")
    skus = set()
    page_token = None
    for _ in range(300):
        params: dict = {"marketplaceIds": AMAZON_MARKETPLACE, "includedData": "summaries"}
        if page_token:
            params["pageToken"] = page_token
        try:
            r = requests.get(
                f"{AMAZON_SP_BASE}/listings/2021-08-01/items/{seller_id}",
                headers=_amz_headers(), params=params, timeout=30,
            )
            if r.status_code != 200:
                break
            d = r.json()
            for item in (d.get("items") or []):
                sku = item.get("sku", "").strip()
                if sku:
                    skus.add(sku)
            page_token = (d.get("pagination") or {}).get("nextToken")
            if not page_token:
                break
        except Exception:
            break
    return skus


def _bling_produtos_com_estoque() -> list[dict]:
    """Retorna todos produtos simples com estoque do Bling."""
    token = _bling_token()
    if not token:
        return []
    resultados = []
    pagina = 1
    while True:
        try:
            with httpx.Client(timeout=30) as hx:
                r = hx.get(
                    "https://api.bling.com.br/Api/v3/produtos",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    params={
                        "pagina":   pagina,
                        "limite":   100,
                        "situacao": "A",     # apenas ativos
                        "criterio": 1,       # 1 = com estoque positivo (filtro Bling)
                        "tipo":     "P",     # produtos simples
                    },
                )
            if r.status_code != 200:
                break
            itens = r.json().get("data") or []
            if not itens:
                break
            for item in itens:
                # Exclui kits e compostos
                if item.get("formato") in ("K",):
                    continue
                estrutura = item.get("estrutura") or {}
                if estrutura.get("tipo") in ("M", "K"):
                    continue
                sku = item.get("codigo") or ""
                if not sku:
                    continue
                # Exclui SKUs de outros canais
                sku_upper = sku.upper()
                if any(sku_upper.startswith(p) for p in ("MLB", "MLBU", "SHOP", "SHP")):
                    continue
                # Exclui kits — por SKU (contém "KIT") ou por nome (contém "kit")
                nome_prod = (item.get("nome") or "").strip().lower()
                if "kit" in sku_upper or "kit" in nome_prod:
                    continue
                # Verifica saldo real (double-check mesmo com criterio=1)
                estoque_obj = item.get("estoque") or {}
                saldo = max(
                    float(estoque_obj.get("fisico") or 0),
                    float(estoque_obj.get("saldoFisico") or 0),
                    float(estoque_obj.get("saldoVirtualTotal") or 0),
                )
                resultados.append({
                    "id":    item.get("id"),
                    "sku":   sku,
                    "nome":  item.get("nome", "")[:120],
                    "saldo": saldo,
                })
            pagina += 1
            time.sleep(0.2)
        except Exception as exc:
            logger.warning("[LOTE-AMZ] erro coleta pg %s: %s", pagina, exc)
            break
    return resultados


def _calcular_preco_produto(prod_id: int, sku: str, product_type: str, margem_alvo: float) -> dict:
    """
    Calcula o preço de venda Amazon.
    1ª opção: simulador (/integracao/preview) — já tem impostos, embalagem e margem das regras.
    2ª opção: custo Bling + taxa fixa das regras + referral Amazon.
    3ª opção: preço de venda do Bling.
    """
    try:
        p = _bling_produto(prod_id)
    except Exception as exc:
        return {"erro": str(exc), "preco": 0.0, "fonte_preco": "erro_bling"}

    nome = p.get("nome", "")[:120]

    # ── 1. Tenta o simulador primeiro ────────────────────────────────────────────
    sim = _preco_simulador(sku)
    sim_preco = float(sim.get("preco") or sim.get("preco_promocional") or 0)
    if sim_preco > 0:
        sim_raw = sim.get("raw") or {}
        custo = float(sim_raw.get("custo_base") or sim_raw.get("custo_produto") or sim.get("custo_total") or 0)
        taxa_fixa = float(sim_raw.get("taxa_fixa") or sim.get("taxa_fixa") or 0)
        return {
            "preco":        round(sim_preco, 2),
            "custo":        round(custo, 2),
            "taxa_fixa":    round(taxa_fixa, 2),
            "referral_pct": 0,
            "closing_fee":  0,
            "fonte_preco":  "simulador",
            "margem_alvo":  margem_alvo,
            "nome":         nome,
            "gtin":         p.get("gtin") or p.get("codigoBarras") or "",
            "_produto":     p,
        }

    # ── 2. Custo base: prioriza última entrada de estoque ────────────────────────
    fornecedor = p.get("fornecedor") or {}
    estoque_obj = p.get("estoque") or {}
    custo = float(
        estoque_obj.get("precoUltimaCompra") or
        estoque_obj.get("precoCompra") or estoque_obj.get("precoCusto") or
        estoque_obj.get("valorCompra") or
        fornecedor.get("precoCompra") or fornecedor.get("precoCusto") or
        p.get("precoCusto") or p.get("precoCompra") or 0
    )
    if custo <= 0:
        custo = _bling_preco_ultima_entrada(prod_id)

    # ── 3. Taxa fixa das regras Amazon (embalagem, handling, etc.) ───────────────
    regra = _regra_amazon(custo * 2.5)
    taxa_fixa = float(regra.get("taxa_fixa") or 0)

    preco = 0.0
    fonte_preco = "sem_custo"
    fees: dict = {}

    if custo > 0:
        custo_total = custo + taxa_fixa
        fees = _amazon_fees(sku, custo_total * 2.5)
        if not fees:
            fees = _fees_fallback(product_type, custo_total * 2.5)
            fonte_preco = "regras+referral_estimado"
        else:
            fonte_preco = "regras+taxas_reais_amazon"

        preco = _calcular_preco_com_taxas_amazon(custo_total, fees, margem_alvo)

        regra2 = _regra_amazon(preco)
        taxa_fixa2 = float(regra2.get("taxa_fixa") or taxa_fixa)
        if taxa_fixa2 != taxa_fixa:
            taxa_fixa = taxa_fixa2
            custo_total = custo + taxa_fixa
            preco = _calcular_preco_com_taxas_amazon(custo_total, fees, margem_alvo)

        if not fees.get("estimado"):
            fees2 = _amazon_fees(sku, preco)
            if fees2:
                fees = fees2
                preco = _calcular_preco_com_taxas_amazon(custo_total, fees, margem_alvo)

    # ── 4. Fallback: preço de venda do Bling ─────────────────────────────────────
    if preco <= 0:
        preco = float(p.get("preco") or 0)
        fonte_preco = "preco_bling"

    return {
        "preco":          round(preco, 2),
        "custo":          round(custo, 2),
        "taxa_fixa":      round(taxa_fixa, 2),
        "referral_pct":   fees.get("referral_fee_pct", 0),
        "closing_fee":    fees.get("closing_fee", 0),
        "fonte_preco":    fonte_preco,
        "margem_alvo":    margem_alvo,
        "nome":           nome,
        "gtin":           p.get("gtin") or p.get("codigoBarras") or "",
    }


@router.post("/amazon/exportar-lote/iniciar")
def amazon_lote_iniciar(product_type: str = "BEAUTY", margem_alvo: float = 15.0, force: bool = False):
    """
    Fase 1: coleta produtos Bling com estoque e filtra os que já existem na Amazon.
    force=True inclui produtos já na Amazon (para re-exportar e corrigir dados).
    Salva a fila em arquivo e retorna o total a exportar.
    """
    logger.info("[LOTE-AMZ] Coletando produtos Bling com estoque…")
    bling_prods = _bling_produtos_com_estoque()

    logger.info("[LOTE-AMZ] Coletando SKUs já cadastrados na Amazon…")
    amazon_skus = _amazon_skus_existentes()
    logger.info("[LOTE-AMZ] Amazon tem %d SKUs cadastrados", len(amazon_skus))

    if force:
        pendentes = bling_prods
        logger.info("[LOTE-AMZ] force=True — re-exportando todos os %d produtos (inclusive %d já na Amazon)",
                    len(pendentes), len(amazon_skus))
    else:
        pendentes = [p for p in bling_prods if p["sku"] not in amazon_skus]
        logger.info("[LOTE-AMZ] %d produtos a exportar (de %d Bling, %d já na Amazon)",
                    len(pendentes), len(bling_prods), len(amazon_skus))

    state = {
        "pendentes":    pendentes,
        "cursor":       0,
        "product_type": product_type,
        "margem_alvo":  margem_alvo,
        "exportados":   [],
        "erros":        [],
        "total":        len(pendentes),
        "concluido":    False,
        "iniciado_em":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _lote_salvar(state)
    return {
        "ok": True,
        "total_bling": len(bling_prods),
        "ja_na_amazon": len(amazon_skus),
        "a_exportar": len(pendentes),
        "msg": f"{len(pendentes)} produtos para exportar.",
    }


@router.post("/amazon/exportar-lote/calcular-precos")
def amazon_lote_calcular_precos(lote: int = 5):
    """
    Fase 2: calcula o preço para os próximos `lote` produtos da fila SEM exportar.
    Chame repetidamente até cursor_preview >= total.
    """
    state = _lote_carregar()
    if not state:
        raise HTTPException(status_code=400, detail="Rode /amazon/exportar-lote/iniciar primeiro.")

    pendentes       = state["pendentes"]
    cursor_preview  = state.get("cursor_preview", 0)
    product_type    = state.get("product_type", "BEAUTY")
    margem_alvo     = float(state.get("margem_alvo", 15.0))
    previews        = state.get("previews", [])

    if cursor_preview >= len(pendentes):
        state["preview_concluido"] = True
        _lote_salvar(state)
        return {"ok": True, "preview_concluido": True, "total": len(pendentes), "calculados": cursor_preview}

    fatia = pendentes[cursor_preview: cursor_preview + lote]
    novos = []
    for prod in fatia:
        resultado = _calcular_preco_produto(
            prod_id=int(prod["id"]),
            sku=prod["sku"],
            product_type=product_type,
            margem_alvo=margem_alvo,
        )
        p_det = resultado.get("_produto") or {}
        novos.append({
            "id":          prod["id"],
            "sku":         prod["sku"],
            "nome":        resultado.get("nome") or prod.get("nome", ""),
            "saldo":       prod.get("saldo", 0),
            "custo":       resultado["custo"],
            "taxa_fixa":   resultado["taxa_fixa"],
            "referral_pct": resultado["referral_pct"],
            "closing_fee": resultado["closing_fee"],
            "preco":       resultado["preco"],
            "preco_editado": None,
            "fonte_preco": resultado["fonte_preco"],
            "gtin":        resultado.get("gtin", ""),
            "erro":        resultado.get("erro"),
        })
        time.sleep(0.3)
        logger.info("[LOTE-AMZ-PREV] %s → R$ %.2f [%s]",
                    prod["sku"], resultado["preco"], resultado["fonte_preco"])

    state["previews"]        = previews + novos
    state["cursor_preview"]  = cursor_preview + len(fatia)
    state["preview_concluido"] = state["cursor_preview"] >= len(pendentes)
    _lote_salvar(state)

    return {
        "ok":               True,
        "preview_concluido": state["preview_concluido"],
        "calculados":       state["cursor_preview"],
        "total":            len(pendentes),
        "batch":            novos,
    }


@router.post("/amazon/exportar-lote/confirmar-preco")
def amazon_lote_confirmar_preco(payload: dict):
    """Permite editar o preço de um produto individual antes do export."""
    sku   = payload.get("sku", "")
    preco = float(payload.get("preco") or 0)
    if not sku or preco <= 0:
        raise HTTPException(status_code=400, detail="sku e preco obrigatórios.")
    state = _lote_carregar()
    if not state:
        raise HTTPException(status_code=400, detail="Nenhuma sessão ativa.")
    for item in state.get("previews", []):
        if item["sku"] == sku:
            item["preco_editado"] = round(preco, 2)
            break
    _lote_salvar(state)
    return {"ok": True, "sku": sku, "preco_confirmado": round(preco, 2)}


@router.post("/amazon/exportar-lote/processar")
def amazon_lote_processar(lote: int = 5):
    """
    Fase 2: processa `lote` produtos da fila por vez.
    Chame repetidamente até concluido=true.
    """
    state = _lote_carregar()
    if not state:
        raise HTTPException(status_code=400, detail="Rode /amazon/exportar-lote/iniciar primeiro.")
    if state.get("concluido"):
        return {"ok": True, "concluido": True, **_lote_resumo(state)}

    pendentes   = state["pendentes"]
    cursor      = state["cursor"]
    product_type = state.get("product_type", "BEAUTY")
    margem_alvo  = float(state.get("margem_alvo", 15.0))

    # Mapa de preços do preview (usa preco_editado se disponível)
    preview_map = {p["sku"]: p for p in state.get("previews", [])}

    fatia = pendentes[cursor: cursor + lote]
    exportados_batch = []
    erros_batch = []

    for prod in fatia:
        sku     = prod["sku"]
        prod_id = prod["id"]
        try:
            prev = preview_map.get(sku) or {}
            preco_final = float(prev.get("preco_editado") or prev.get("preco") or 0)
            if preco_final <= 0:
                raise ValueError(f"Preço calculado R$0,00 — produto sem custo cadastrado. Preencha o preço de compra no Bling antes de exportar.")
            payload = ExportarAmazonPayload(
                produto_id=int(prod_id),
                product_type=product_type,
                usar_simulador=False,
                margem_alvo=margem_alvo,
                preco_override=preco_final,
            )
            resultado = amazon_exportar_produto(payload)
            exportados_batch.append({
                "sku":   sku,
                "nome":  prod["nome"],
                "preco": preco_final,          # preço real enviado à Amazon
                "fonte": "exportado",
            })
            logger.info("[LOTE-AMZ] ✓ %s exportado — R$ %.2f", sku, resultado.get("preco") or 0)
        except Exception as exc:
            detail = str(exc)
            if hasattr(exc, "detail"):
                detail = str(exc.detail)  # type: ignore[union-attr]
            erros_batch.append({"sku": sku, "nome": prod["nome"], "erro": detail[:300]})
            logger.warning("[LOTE-AMZ] ✗ %s: %s", sku, detail[:200])
        time.sleep(0.5)  # evita rate limit SP-API

    state["cursor"]     += len(fatia)
    state["exportados"] += exportados_batch
    state["erros"]      += erros_batch
    state["concluido"]   = state["cursor"] >= len(pendentes)

    _lote_salvar(state)
    return {
        "ok":          True,
        "concluido":   state["concluido"],
        "processados": state["cursor"],
        "exportados_batch": len(exportados_batch),
        "erros_batch":      len(erros_batch),
        **_lote_resumo(state),
    }


def _lote_resumo(state: dict) -> dict:
    return {
        "total":      state.get("total", 0),
        "processados": state.get("cursor", 0),
        "exportados": len(state.get("exportados", [])),
        "erros":      len(state.get("erros", [])),
    }


@router.get("/amazon/catalog/ean/{ean}")
def amazon_catalog_ean(ean: str):
    """Testa busca de ASIN por EAN no catálogo Amazon."""
    _ASIN_CACHE.pop(ean, None)  # limpa cache para forçar nova busca
    asin = _buscar_asin_por_ean(ean)
    return {"ean": ean, "asin": asin or None, "encontrado": bool(asin)}


@router.get("/amazon/listing/{sku}/status")
def amazon_listing_status(sku: str):
    """Verifica se um SKU existe na Amazon e retorna status/preço atual do listing."""
    try:
        token = _lwa_token()
        seller_id = os.getenv("AMAZON_SELLER_ID", "")
        with httpx.Client(timeout=20) as hx:
            r = hx.get(
                f"{AMAZON_SP_BASE}/listings/2021-08-01/items/{seller_id}/{sku}",
                headers={
                    "x-amz-access-token": token,
                    "x-amz-marketplace-id": AMAZON_MARKETPLACE,
                    "Accept": "application/json",
                },
                params={"marketplaceIds": AMAZON_MARKETPLACE, "includedData": "summaries,offers"},
            )
        if r.status_code == 404:
            return {"sku": sku, "existe": False, "status": "nao_encontrado"}
        if r.status_code != 200:
            return {"sku": sku, "existe": False, "status": f"erro_api_{r.status_code}", "detalhe": r.text[:200]}
        data = r.json()
        summaries = (data.get("summaries") or [{}])[0]
        offers    = data.get("offers") or []
        preco_atual = None
        if offers:
            preco_atual = float((offers[0].get("price") or {}).get("amount") or 0) or None
        return {
            "sku":       sku,
            "existe":    True,
            "status":    summaries.get("status", "unknown"),
            "asin":      summaries.get("asin"),
            "nome":      summaries.get("itemName", "")[:120],
            "preco":     preco_atual,
            "url":       f"https://www.amazon.com.br/dp/{summaries.get('asin')}" if summaries.get("asin") else None,
        }
    except HTTPException:
        raise
    except Exception as exc:
        return {"sku": sku, "existe": False, "status": "erro", "detalhe": str(exc)[:200]}


@router.get("/amazon/exportar-lote/status")
def amazon_lote_status():
    state = _lote_carregar()
    if not state:
        return {"iniciado": False}
    return {
        "iniciado":          True,
        "concluido":         state.get("concluido", False),
        "preview_concluido": state.get("preview_concluido", False),
        "iniciado_em":       state.get("iniciado_em"),
        "product_type":      state.get("product_type"),
        "previews_lista":    state.get("previews", []),
        "exportados_lista":  state.get("exportados", []),
        "erros_lista":       state.get("erros", []),
        **_lote_resumo(state),
    }


@router.post("/amazon/exportar-lote/selecionar")
def amazon_lote_selecionar(payload: dict):
    """Filtra a fila de exportação para incluir apenas os SKUs selecionados."""
    skus = set(payload.get("skus") or [])
    if not skus:
        raise HTTPException(status_code=400, detail="Lista de SKUs vazia.")
    state = _lote_carregar()
    if not state:
        raise HTTPException(status_code=404, detail="Nenhuma sessão de exportação ativa.")
    pendentes = [p for p in state.get("pendentes", []) if p.get("sku") in skus]
    state["pendentes"] = pendentes
    state["total"] = len(pendentes)
    state["cursor"] = min(state.get("cursor", 0), len(pendentes))
    _lote_salvar(state)
    return {"ok": True, "total": len(pendentes), "msg": f"{len(pendentes)} produtos selecionados para exportar."}


@router.post("/amazon/exportar-lote/reset")
def amazon_lote_reset():
    _LOTE_STATE_FILE.unlink(missing_ok=True)
    return {"ok": True, "msg": "Fila de exportação limpa."}


@router.post("/amazon/reexportar-marca")
def amazon_reexportar_marca(
    marca: str = "schwarzkopf",
    product_type: str = "BEAUTY",
    margem_alvo: float = 15.0,
    background_tasks: BackgroundTasks = None,
):
    """Re-exporta todos os produtos de uma marca buscando no Bling via token interno."""
    import threading

    palavras = [w.lower() for w in marca.split(",")]

    def _run():
        token = _bling_token()
        if not token:
            logger.error("[REEXPORTAR-MARCA] Token Bling indisponível")
            return
        logger.info("[REEXPORTAR-MARCA] Token Bling obtido (primeiros 8 chars): %s...", token[:8])
        resultados = []
        # Busca por cada palavra-chave diretamente no Bling (evita varrer catálogo inteiro)
        vistos = set()
        for palavra in palavras:
            pagina = 1
            while True:
                r = requests.get(
                    "https://api.bling.com.br/Api/v3/produtos",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    params={"pagina": pagina, "limite": 100, "situacao": "A", "nome": palavra},
                    timeout=30,
                )
                if r.status_code != 200:
                    logger.error("[REEXPORTAR-MARCA] Bling API erro palavra='%s' pag=%d status=%d", palavra, pagina, r.status_code)
                    break
                itens = r.json().get("data") or []
                if not itens:
                    break
                for p in itens:
                    pid = p.get("id")
                    formato = p.get("formato") or ""
                    if pid not in vistos and formato != "E":
                        vistos.add(pid)
                        resultados.append(p)
                if len(itens) < 100:
                    break
                pagina += 1
                time.sleep(0.3)

        logger.info("[REEXPORTAR-MARCA] %d produtos encontrados para marca '%s'", len(resultados), marca)
        ok = erros = 0
        for p in resultados:
            pid = p.get("id")
            try:
                payload = ExportarAmazonPayload(
                    produto_id=pid,
                    product_type=product_type,
                    margem_alvo=margem_alvo,
                    condition_type="new_new",
                    fulfillment_channel="DEFAULT",
                    usar_simulador=True,
                    preco_override=0,
                )
                amazon_exportar_produto(payload)
                ok += 1
                logger.info("[REEXPORTAR-MARCA] OK pid=%s nome=%s", pid, p.get("nome","")[:40])
            except Exception as exc:
                erros += 1
                logger.error("[REEXPORTAR-MARCA] Exceção pid=%s nome=%s: %s", pid, p.get("nome","")[:40], exc)
            time.sleep(1.2)
        logger.info("[REEXPORTAR-MARCA] Concluído: %d ok, %d erros", ok, erros)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {"ok": True, "msg": f"Re-export de '{marca}' iniciado em background. Acompanhe nos logs do Cloud Run."}


def _sync_estoque_marca_exec(marca: str) -> dict:
    """Executa o sync de estoque Bling → Amazon sincronamente. Seguro para chamar do scheduler."""
    token_bling = _bling_token()
    if not token_bling:
        logger.error("[SYNC-ESTOQUE] Token Bling indisponível")
        return {"ok": 0, "erros": 0, "sem_listing": 0, "sem_ean": 0}

    palavras = [w.lower() for w in marca.split(",")]
    vistos: set = set()
    produtos = []
    for palavra in palavras:
        for pagina in range(1, 20):
            r = requests.get(
                "https://api.bling.com.br/Api/v3/produtos",
                headers={"Authorization": f"Bearer {token_bling}"},
                params={"pagina": pagina, "limite": 100, "situacao": "A", "nome": palavra},
                timeout=30,
            )
            if r.status_code != 200:
                break
            itens = r.json().get("data") or []
            for p in itens:
                pid = p.get("id")
                if pid not in vistos and p.get("formato") != "E":
                    vistos.add(pid)
                    produtos.append(p)
            if len(itens) < 100:
                break
            time.sleep(0.3)
        time.sleep(0.4)

    logger.info("[SYNC-ESTOQUE] %d produtos encontrados para marca '%s'", len(produtos), marca)

    seller_id = os.getenv("AMAZON_SELLER_ID", "")
    ok = erros = sem_ean = sem_listing = 0

    for p in produtos:
        pid = p.get("id")
        r2 = requests.get(
            f"https://api.bling.com.br/Api/v3/produtos/{pid}",
            headers={"Authorization": f"Bearer {token_bling}"},
            timeout=20,
        )
        if r2.status_code != 200:
            time.sleep(0.5)
            continue
        d = r2.json().get("data") or {}
        gtin = (d.get("gtin") or "").strip()
        if not gtin or len(gtin) < 13:
            sem_ean += 1
            time.sleep(0.3)
            continue
        estoque = int((d.get("estoque") or {}).get("saldoVirtualTotal") or 0)

        try:
            payload = {
                "productType": "HAIR_COLORING_AGENT",
                "patches": [{
                    "op": "replace",
                    "path": "/attributes/fulfillment_availability",
                    "value": [{
                        "fulfillment_channel_code": "DEFAULT",
                        "quantity": estoque,
                        "marketplace_id": AMAZON_MARKETPLACE,
                    }],
                }],
            }
            r3 = requests.patch(
                f"{AMAZON_SP_BASE}/listings/2021-08-01/items/{seller_id}/{gtin}",
                headers=_amz_headers(),
                params={"marketplaceIds": AMAZON_MARKETPLACE, "issueLocale": "pt_BR"},
                json=payload,
                timeout=20,
            )
            if r3.status_code in (200, 202):
                ok += 1
                logger.info("[SYNC-ESTOQUE] OK sku=%s estoque=%d nome=%s", gtin, estoque, p.get("nome","")[:40])
            elif r3.status_code == 404:
                sem_listing += 1
            else:
                erros += 1
                logger.warning("[SYNC-ESTOQUE] ERRO sku=%s status=%d: %s", gtin, r3.status_code, r3.text[:150])
        except Exception as exc:
            erros += 1
            logger.error("[SYNC-ESTOQUE] Exceção sku=%s: %s", gtin, exc)
        time.sleep(0.5)

    logger.info(
        "[SYNC-ESTOQUE] Concluído: %d ok, %d erros, %d sem_listing, %d sem_ean",
        ok, erros, sem_listing, sem_ean,
    )
    return {"ok": ok, "erros": erros, "sem_listing": sem_listing, "sem_ean": sem_ean}


@router.post("/amazon/sync-estoque-marca")
def amazon_sync_estoque_marca(marca: str = "schwarzkopf"):
    """Sincroniza o estoque (fulfillment_availability) do Bling para a Amazon. Roda em background thread."""
    import threading
    t = threading.Thread(target=_sync_estoque_marca_exec, args=(marca,), daemon=True)
    t.start()
    return {"ok": True, "msg": f"Sync de estoque de '{marca}' iniciado em background."}


@router.post("/amazon/sync-estoque-marca/sincronо")
def amazon_sync_estoque_marca_sincrono(marca: str = "schwarzkopf"):
    """Sync de estoque síncrono — aguarda conclusão. Use para triggers externos (Cloud Scheduler)."""
    resultado = _sync_estoque_marca_exec(marca)
    return {"ok": True, "resultado": resultado}




@router.get("/amazon/listing/{sku}")
def amazon_ver_listing(sku: str):
    """Consulta um listing existente na Amazon pelo SKU."""
    seller_id = os.getenv("AMAZON_SELLER_ID", "")
    params = {
        "marketplaceIds": AMAZON_MARKETPLACE,
        "includedData":   "summaries,attributes,issues,fulfillmentAvailability",
    }
    r = requests.get(
        f"{AMAZON_SP_BASE}/listings/2021-08-01/items/{seller_id}/{sku}",
        headers=_amz_headers(), params=params, timeout=20,
    )
    if r.status_code == 404:
        return {"encontrado": False, "sku": sku}
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text[:400])
    return {"encontrado": True, "sku": sku, "data": r.json()}


# ── Re-exportar kits Igora Royal com preço correto do simulador ───────────────

_KITS_JOB: dict = {}

@router.post("/amazon/reexportar-kits-igora")
def amazon_reexportar_kits_igora(product_type: str = "BEAUTY"):
    """
    Varre todos os kits (formato E) Igora Royal no Bling,
    busca o preço correto no simulador pelo SKU do componente simples,
    e re-exporta cada kit para Amazon com preco_override.
    Roda em background thread.
    """
    import threading

    def _run():
        token = _bling_token()
        if not token:
            logger.error("[KITS-IGORA] Token Bling indisponível")
            return

        _KITS_JOB.update({"status": "running", "ok": 0, "sem_comp": 0,
                           "sem_sim": 0, "erros": 0, "total": 0, "log": []})

        # 1. Buscar todos os kits Igora Royal
        EXCLUIR = ["zero", "fashion", "highlight", "highl", "ox",
                   "água oxigenada", "agua oxigenada", "pastelif", "mixtom", "diluidor"]
        kits = []
        vistos = set()
        pagina = 1
        while True:
            r = requests.get("https://api.bling.com.br/Api/v3/produtos",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params={"pagina": pagina, "limite": 100, "situacao": "A"},
                timeout=30)
            if r.status_code == 401:
                token = _bling_token(force_refresh=True)
                continue
            if r.status_code != 200:
                break
            itens = r.json().get("data") or []
            if not itens:
                break
            for p in itens:
                pid = p.get("id")
                fmt = p.get("formato") or ""
                nome = (p.get("nome") or "").lower()
                if pid not in vistos and fmt == "E" and "igora royal" in nome:
                    if not any(x in nome for x in EXCLUIR):
                        vistos.add(pid)
                        kits.append(p)
                else:
                    vistos.add(pid)
            if len(itens) < 100:
                break
            pagina += 1
            time.sleep(0.3)

        _KITS_JOB["total"] = len(kits)
        logger.info("[KITS-IGORA] Encontrados %d kits Igora Royal", len(kits))

        base_url = os.getenv("SELF_BASE_URL", "http://localhost:8080")

        for kit in kits:
            kit_id = kit.get("id")
            nome_kit = (kit.get("nome") or "")[:70]

            # 2. Buscar estrutura do kit para pegar SKU do componente
            rp = requests.get(f"https://api.bling.com.br/Api/v3/produtos/{kit_id}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=20)
            if rp.status_code != 200:
                logger.warning("[KITS-IGORA] Falha ao buscar kit %d: %d", kit_id, rp.status_code)
                _KITS_JOB["erros"] += 1
                _KITS_JOB["log"].append({"kit_id": kit_id, "nome": nome_kit, "status": f"erro_bling_{rp.status_code}"})
                time.sleep(0.3)
                continue

            data_produto = rp.json().get("data", {})
            estrutura_raw = data_produto.get("estrutura") or {}
            logger.info("[KITS-IGORA] kit %d estrutura_raw type=%s keys=%s comp_count=%s",
                        kit_id, type(estrutura_raw).__name__,
                        list(estrutura_raw.keys()) if isinstance(estrutura_raw, dict) else str(estrutura_raw)[:100],
                        len(estrutura_raw.get("componentes", [])) if isinstance(estrutura_raw, dict) else "?")
            if isinstance(estrutura_raw, dict):
                componentes = (estrutura_raw.get("componentes")
                               or estrutura_raw.get("itens")
                               or estrutura_raw.get("composicao")
                               or [])
            elif isinstance(estrutura_raw, list):
                componentes = estrutura_raw
            else:
                componentes = []
            comp_sku = None
            for comp in componentes:
                if not isinstance(comp, dict):
                    continue
                prod = comp.get("produto")
                if isinstance(prod, str) and prod:
                    comp_sku = prod
                elif isinstance(prod, dict):
                    comp_sku = prod.get("codigo") or prod.get("sku") or ""
                if comp_sku:
                    break

            preco_sim = 0.0
            fonte_preco = ""

            if comp_sku:
                # 3a. Consultar simulador com SKU do componente
                try:
                    rs = requests.post(f"{base_url}/integracao/preview",
                        json={"valor_busca": comp_sku, "criterio": "sku"},
                        timeout=30)
                    sim_data = rs.json() if rs.status_code == 200 else {}
                except Exception:
                    sim_data = {}

                mp = sim_data.get("marketplaces") or {}
                amz = mp.get("amazon") or mp.get("Amazon") or {}
                preco_sim = float(amz.get("preco") or amz.get("preco_calculado") or 0)
                fonte_preco = f"simulador_comp:{comp_sku}"

            if preco_sim <= 0:
                # 3b. Fallback: calcular da AR2M precoCusto do próprio kit
                import math as _math
                fornecedores = data_produto.get("fornecedores") or []
                custo_ar2m = 0.0
                for f in fornecedores:
                    nome_f = ((f.get("fornecedor") or {}).get("nome") or f.get("nome") or "").lower()
                    if "ar2m" in nome_f:
                        custo_ar2m = float(f.get("precoCusto") or f.get("preco") or 0)
                        break
                if custo_ar2m > 0:
                    preco_base = (custo_ar2m + 1.50) * 1.33
                    preco_sim = round(_math.floor(preco_base - 0.90) + 0.90, 2)
                    fonte_preco = f"ar2m_calc:{custo_ar2m:.2f}"
                    logger.info("[KITS-IGORA] Kit %d fallback AR2M custo=%.2f → R$%.2f",
                                kit_id, custo_ar2m, preco_sim)
                else:
                    # 3c. Último fallback: precoCusto do produto (calculado pelo Bling)
                    custo_kit = float(data_produto.get("precoCusto") or 0)
                    if custo_kit > 0:
                        preco_base = (custo_kit + 1.50) * 1.33
                        preco_sim = round(_math.floor(preco_base - 0.90) + 0.90, 2)
                        fonte_preco = f"precoCusto_kit:{custo_kit:.2f}"
                        logger.info("[KITS-IGORA] Kit %d fallback precoCusto=%.2f → R$%.2f",
                                    kit_id, custo_kit, preco_sim)

            if preco_sim <= 0:
                logger.warning("[KITS-IGORA] Kit %d sem preço (comp=%s, sem AR2M)", kit_id, comp_sku)
                _KITS_JOB["sem_comp"] += 1
                _KITS_JOB["log"].append({"kit_id": kit_id, "nome": nome_kit,
                                          "comp_sku": comp_sku, "status": "sem_preco"})
                time.sleep(0.3)
                continue

            # 4. Exportar kit com preco_override
            try:
                payload = ExportarAmazonPayload(
                    produto_id=kit_id,
                    product_type=product_type,
                    margem_alvo=15.0,
                    condition_type="new_new",
                    fulfillment_channel="DEFAULT",
                    usar_simulador=False,
                    preco_override=preco_sim,
                )
                resultado = amazon_exportar_produto(payload)
                sku_exp = resultado.get("sku", "")
                preco_exp = resultado.get("preco_exportado", 0)
                logger.info("[KITS-IGORA] Kit %d SKU=%s exportado R$%.2f (fonte=%s)",
                            kit_id, sku_exp, preco_exp, fonte_preco)
                _KITS_JOB["ok"] += 1
                _KITS_JOB["log"].append({"kit_id": kit_id, "nome": nome_kit,
                                          "fonte": fonte_preco, "sku": sku_exp,
                                          "preco": preco_exp, "status": "ok"})
            except Exception as exc:
                logger.error("[KITS-IGORA] Kit %d erro export: %s", kit_id, exc)
                _KITS_JOB["erros"] += 1
                _KITS_JOB["log"].append({"kit_id": kit_id, "nome": nome_kit,
                                          "comp_sku": comp_sku, "status": f"erro: {str(exc)[:100]}"})

            time.sleep(1.5)

        _KITS_JOB["status"] = "done"
        logger.info("[KITS-IGORA] Concluído: %d ok, %d sem_comp, %d sem_sim, %d erros",
                    _KITS_JOB["ok"], _KITS_JOB["sem_comp"], _KITS_JOB["sem_sim"], _KITS_JOB["erros"])

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "msg": "Iniciado em background. Consulte /amazon/reexportar-kits-igora/status para acompanhar."}


@router.get("/amazon/reexportar-kits-igora/status")
def amazon_reexportar_kits_igora_status():
    """Retorna o status do job de re-exportação de kits Igora Royal."""
    return _KITS_JOB or {"status": "idle"}


# ── Exportar Kits para Amazon (fluxo assistido genérico) ──────────────────────

_KITS_EXPORT_JOB: dict = {"status": "idle", "total": 0, "ok": 0, "erros": 0, "log": []}


@router.get("/amazon/kits/lista")
def amazon_kits_lista(pagina: int = 1, limite: int = 100):
    """
    Lista todos os kits/composições do Bling com preço calculado pelo simulador
    e status na Amazon (já exportado ou não).
    """
    token = _bling_token()
    if not token:
        raise HTTPException(status_code=502, detail="Token Bling indisponível.")

    base_url = os.getenv("SELF_BASE_URL", "http://localhost:8080")

    # 1. Coletar kits do Bling (formato=E ou estrutura.tipo in M/K)
    kits_raw = []
    vistos = set()
    pag = 1
    while True:
        r = requests.get("https://api.bling.com.br/Api/v3/produtos",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={"pagina": pag, "limite": 100, "situacao": "A"},
            timeout=30)
        if r.status_code == 401:
            token = _bling_token(force_refresh=True)
            continue
        if r.status_code != 200:
            break
        itens = r.json().get("data") or []
        if not itens:
            break
        for p in itens:
            pid = p.get("id")
            if pid in vistos:
                continue
            vistos.add(pid)
            fmt = p.get("formato") or ""
            estrutura = p.get("estrutura") or {}
            tipo_est = estrutura.get("tipo") if isinstance(estrutura, dict) else ""
            nome = (p.get("nome") or "").lower()
            if fmt in ("E", "K") or tipo_est in ("M", "K") or "kit" in nome:
                kits_raw.append(p)
        if len(itens) < 100:
            break
        pag += 1
        time.sleep(0.3)

    # 2. Coletar SKUs já na Amazon
    amazon_skus = _amazon_skus_existentes()

    # 3. Para cada kit, calcular preço via simulador (apenas primeiro componente)
    total = len(kits_raw)
    # Paginação simples
    inicio = (pagina - 1) * limite
    fim = inicio + limite
    fatia = kits_raw[inicio:fim]

    resultado = []
    for p in fatia:
        sku = (p.get("codigo") or "").strip()
        nome_kit = (p.get("nome") or "").strip()
        peso = float(p.get("pesoLiquido") or p.get("peso") or 0)
        preco_bling = float(p.get("preco") or 0)
        custo = float(p.get("precoCusto") or 0)
        gtin = (p.get("gtin") or p.get("codigoBarras") or "").strip()

        # Componentes
        estrutura = p.get("estrutura") or {}
        componentes = []
        if isinstance(estrutura, dict):
            raw_comp = (estrutura.get("componentes") or
                        estrutura.get("itens") or
                        estrutura.get("composicao") or [])
            for c in raw_comp:
                if isinstance(c, dict):
                    prod = c.get("produto") or {}
                    comp_sku = prod.get("codigo") if isinstance(prod, dict) else str(prod)
                    comp_nome = prod.get("nome") if isinstance(prod, dict) else ""
                    qty = float(c.get("quantidade") or 1)
                    if comp_sku:
                        componentes.append({"sku": comp_sku, "nome": comp_nome, "qty": qty})

        # Preço via simulador
        preco_sim = 0.0
        margem_sim = None
        fonte_preco = "bling"
        try:
            rs = requests.post(f"{base_url}/integracao/preview",
                json={"valor_busca": sku, "criterio": "sku"}, timeout=15)
            if rs.status_code == 200:
                sim = rs.json()
                mp = sim.get("marketplaces") or {}
                amz = mp.get("amazon") or mp.get("Amazon") or {}
                preco_sim = float(amz.get("preco") or amz.get("preco_calculado") or 0)
                margem_sim = amz.get("margem")
                if preco_sim > 0:
                    fonte_preco = "simulador"
        except Exception:
            pass

        if preco_sim <= 0 and custo > 0:
            import math as _math
            preco_base = (custo + 1.50) * 1.33
            preco_sim = round(_math.floor(preco_base - 0.90) + 0.90, 2)
            fonte_preco = "custo_calc"

        preco_final = preco_sim if preco_sim > 0 else preco_bling

        resultado.append({
            "id": p.get("id"),
            "sku": sku,
            "nome": nome_kit,
            "peso": peso,
            "custo": custo,
            "preco_bling": preco_bling,
            "preco_sugerido": preco_final,
            "margem": margem_sim,
            "fonte_preco": fonte_preco,
            "gtin": gtin,
            "formato": p.get("formato") or "",
            "componentes": componentes,
            "na_amazon": sku in amazon_skus,
            "status_amazon": "ativo" if sku in amazon_skus else "novo",
        })

    return {
        "total": total,
        "pagina": pagina,
        "limite": limite,
        "paginas": max(1, -(-total // limite)),
        "kits": resultado,
    }


class ExportarKitsPayload(BaseModel):
    kits: list[dict]           # [{id, sku, preco_override, product_type}]
    product_type: str = "BEAUTY"
    margem_alvo: float = 15.0


@router.post("/amazon/kits/exportar")
def amazon_kits_exportar(payload: ExportarKitsPayload):
    """Inicia exportação em background dos kits selecionados."""
    import threading

    def _run(kits_payload):
        global _KITS_EXPORT_JOB
        _KITS_EXPORT_JOB.update({"status": "running", "total": len(kits_payload),
                                   "ok": 0, "erros": 0, "log": []})
        for item in kits_payload:
            kit_id  = item.get("id")
            sku     = item.get("sku", "")
            preco   = float(item.get("preco_override") or 0)
            pt      = item.get("product_type") or payload.product_type
            try:
                ep = ExportarAmazonPayload(
                    produto_id=int(kit_id),
                    product_type=pt,
                    margem_alvo=payload.margem_alvo,
                    usar_simulador=(preco <= 0),
                    preco_override=preco,
                )
                res = amazon_exportar_produto(ep)
                _KITS_EXPORT_JOB["ok"] += 1
                _KITS_EXPORT_JOB["log"].append({
                    "sku": sku, "status": "ok",
                    "preco": res.get("preco_exportado"),
                    "issues": [i for i in (res.get("issues") or []) if i.get("severity") == "ERROR"],
                })
            except Exception as exc:
                _KITS_EXPORT_JOB["erros"] += 1
                _KITS_EXPORT_JOB["log"].append({"sku": sku, "status": "erro", "erro": str(exc)[:200]})
            time.sleep(1.2)
        _KITS_EXPORT_JOB["status"] = "done"

    threading.Thread(target=_run, args=(payload.kits,), daemon=True).start()
    return {"ok": True, "total": len(payload.kits),
            "msg": f"{len(payload.kits)} kits enfileirados. Acompanhe em /amazon/kits/status"}


@router.get("/amazon/kits/status")
def amazon_kits_status():
    return _KITS_EXPORT_JOB


# ── Alfaparf Evolution Ox 20 – exportação em lote ─────────────────────────────
_ALFAPARF_AMAZON_JOB: dict = {"status": "idle", "ok": 0, "erros": 0, "total": 0,
                               "ignorados": 0, "log": []}

ALFAPARF_NOME_BASE  = "Kit Coloração + Ox 20 Vol Alfaparf Evolution"
ALFAPARF_REMOVER    = "Todas As Cores"   # trecho removido do título no marketplace
ALFAPARF_PRECO      = 54.90
ALFAPARF_PROD_TYPE  = "BEAUTY"


@router.post("/amazon/exportar-alfaparf-kits")
def amazon_exportar_alfaparf_kits(product_type: str = ALFAPARF_PROD_TYPE):
    """
    Exporta individualmente todos os produtos 'Kit Coloração + Ox 20 Vol Alfaparf Evolution'
    para a Amazon, removendo 'Todas As Cores' do título e forçando preço R$54,90.
    Roda em background — acompanhe em /amazon/exportar-alfaparf-kits/status.
    """
    import threading

    def _run():
        token = _bling_token()
        if not token:
            logger.error("[ALFAPARF-AMAZON] Token Bling indisponível")
            return

        _ALFAPARF_AMAZON_JOB.update({"status": "running", "ok": 0, "erros": 0,
                                      "total": 0, "ignorados": 0, "log": []})

        # 1. Coletar todos os produtos Alfaparf Todas As Cores
        produtos = []
        vistos   = set()
        pagina   = 1
        while True:
            r = requests.get(
                "https://api.bling.com.br/Api/v3/produtos",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params={"nome": "Kit Coloração + Ox 20", "pagina": pagina, "limite": 100},
                timeout=30,
            )
            if r.status_code == 401:
                token = _bling_token(force_refresh=True)
                continue
            if r.status_code != 200:
                break
            itens = r.json().get("data") or []
            if not itens:
                break
            for p in itens:
                pid  = p.get("id")
                nome = p.get("nome") or ""
                if pid not in vistos and "Alfaparf" in nome and "Todas As Cores" in nome:
                    vistos.add(pid)
                    produtos.append(p)
                else:
                    vistos.add(pid)
            if len(itens) < 100:
                break
            pagina += 1
            time.sleep(0.3)

        # Excluir o PAI (nome sem sufixo de cor)
        produtos = [p for p in produtos if p.get("nome", "").strip()
                    != "Kit Coloração + Ox 20 Vol Alfaparf Evolution Todas As Cores"]

        _ALFAPARF_AMAZON_JOB["total"] = len(produtos)
        logger.info("[ALFAPARF-AMAZON] %d produtos a exportar", len(produtos))

        base_url = os.getenv("SELF_BASE_URL", "http://localhost:8080")

        for prod in produtos:
            pid  = prod.get("id")
            nome = prod.get("nome") or ""
            sku  = prod.get("codigo") or ""

            # Remover "Todas As Cores" do título
            titulo = nome.replace(ALFAPARF_REMOVER, "").replace("  ", " ").strip()
            # Remover duplo espaço antes de "Cor:" ou código
            import re as _re2
            titulo = _re2.sub(r"\s{2,}", " ", titulo).strip()

            try:
                payload = ExportarAmazonPayload(
                    produto_id=int(pid),
                    product_type=product_type,
                    condition_type="new_new",
                    fulfillment_channel="DEFAULT",
                    usar_simulador=False,
                    preco_override=ALFAPARF_PRECO,
                    titulo_override=titulo,
                )
                resultado = amazon_exportar_produto(payload)
                sku_exp   = resultado.get("sku", sku)
                logger.info("[ALFAPARF-AMAZON] OK pid=%d sku=%s titulo=%s", pid, sku_exp, titulo[:60])
                _ALFAPARF_AMAZON_JOB["ok"] += 1
                _ALFAPARF_AMAZON_JOB["log"].append({
                    "pid": pid, "sku": sku_exp, "titulo": titulo[:80], "status": "ok",
                    "issues": [i for i in (resultado.get("issues") or []) if i.get("severity") == "ERROR"],
                })
            except Exception as exc:
                logger.error("[ALFAPARF-AMAZON] ERRO pid=%d: %s", pid, exc)
                _ALFAPARF_AMAZON_JOB["erros"] += 1
                _ALFAPARF_AMAZON_JOB["log"].append({
                    "pid": pid, "sku": sku, "titulo": titulo[:80],
                    "status": "erro", "erro": str(exc)[:200],
                })
            time.sleep(1.5)

        _ALFAPARF_AMAZON_JOB["status"] = "done"
        logger.info("[ALFAPARF-AMAZON] Concluído: %d ok, %d erros",
                    _ALFAPARF_AMAZON_JOB["ok"], _ALFAPARF_AMAZON_JOB["erros"])

    threading.Thread(target=_run, daemon=True).start()
    return {
        "ok":  True,
        "msg": "Job iniciado. Acompanhe em /amazon/exportar-alfaparf-kits/status",
    }


@router.get("/amazon/exportar-alfaparf-kits/status")
def amazon_exportar_alfaparf_kits_status():
    """Status do job de exportação dos kits Alfaparf Evolution Ox 20 para a Amazon."""
    return _ALFAPARF_AMAZON_JOB


# ── Exportação Amazon de kits usando EAN dos componentes ──────────────────────
_ALFAPARF_AMAZON_BUNDLE_JOB: dict = {
    "status": "idle", "ok": 0, "erros": 0, "ignorados": 0, "total": 0, "log": []
}


@router.post("/amazon/exportar-alfaparf-kits-bundle")
def amazon_exportar_alfaparf_kits_bundle(product_type: str = ALFAPARF_PROD_TYPE):
    """
    Exporta os kits Alfaparf para a Amazon como bundles:
    - Busca o EAN da coloração (componente 1) na estrutura do kit no Bling
    - Usa esse EAN como identificador do listing
    - Marca o produto como bundle (is_bundle=true, number_of_items=2)
    - Remove 'Todas As Cores' do título, força preço R$54,90
    Ignora kits sem estrutura ou sem EAN nos componentes.
    """
    import threading

    def _ean_componente(get_token_fn, comp_id: int, _cache: dict) -> str:
        if comp_id in _cache:
            return _cache[comp_id]
        try:
            for _att in range(3):
                _tok = get_token_fn()
                r = requests.get(f"https://api.bling.com.br/Api/v3/produtos/{comp_id}",
                                 headers={"Authorization": f"Bearer {_tok}", "Accept": "application/json"},
                                 timeout=15)
                if r.status_code == 401:
                    time.sleep(1)
                    continue
                if r.status_code == 429:
                    time.sleep(5)
                    continue
                if r.status_code == 200:
                    d = r.json().get("data") or {}
                    ean = d.get("gtin") or d.get("codigoBarras") or ""
                    if isinstance(ean, list):
                        ean = ean[0] if ean else ""
                    _cache[comp_id] = str(ean).strip()
                    return _cache[comp_id]
                break
        except Exception:
            pass
        _cache[comp_id] = ""
        return ""

    def _run():
        token = _bling_token()
        if not token:
            logger.error("[ALFAPARF-BUNDLE] Token Bling indisponível")
            return

        _ALFAPARF_AMAZON_BUNDLE_JOB.update({
            "status": "running", "ok": 0, "erros": 0, "ignorados": 0, "total": 0, "log": []
        })

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        ean_cache: dict = {}

        # 1. Coletar kits
        produtos = []
        vistos = set()
        pagina = 1
        _401s = 0
        while True:
            r = requests.get(
                "https://api.bling.com.br/Api/v3/produtos",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params={"nome": "Kit Coloração + Ox 20", "pagina": pagina, "limite": 100},
                timeout=30,
            )
            if r.status_code == 401:
                _401s += 1
                if _401s >= 3:
                    logger.error("[ALFAPARF-BUNDLE] Token inválido após %d tentativas, abortando", _401s)
                    break
                token = _bling_token(force_refresh=True)
                continue
            _401s = 0
            if r.status_code != 200:
                logger.warning("[ALFAPARF-BUNDLE] API Bling retornou %d na página %d", r.status_code, pagina)
                break
            itens = r.json().get("data") or []
            if not itens:
                break
            for p in itens:
                pid  = p.get("id")
                nome = p.get("nome") or ""
                if pid not in vistos and "Alfaparf" in nome and "Todas As Cores" in nome:
                    vistos.add(pid)
                    # Excluir produto genérico "pai" sem cor específica
                    if nome.strip() != "Kit Coloração + Ox 20 Vol Alfaparf Evolution Todas As Cores":
                        produtos.append(p)
                else:
                    vistos.add(pid)
            if len(itens) < 100:
                break
            pagina += 1
            time.sleep(0.3)

        _ALFAPARF_AMAZON_BUNDLE_JOB["total"] = len(produtos)
        logger.info("[ALFAPARF-BUNDLE] %d kits encontrados", len(produtos))

        import re as _re3
        desc_kit = (
            "O kit contém:\n"
            "1 Coloração Alfaparf Evolution (Numeração e Cor referente ao anúncio)\n"
            "1 Emulsão ativadora Alfaparf Oxid'O 20 Vol"
        )

        for prod in produtos:
            pid  = prod.get("id")
            nome = prod.get("nome") or ""
            sku  = prod.get("codigo") or ""
            titulo = _re3.sub(r"\s{2,}", " ", nome.replace(ALFAPARF_REMOVER, "")).strip()

            try:
                # 2. Buscar estrutura completa do kit
                def _bling_get(url):
                    nonlocal token
                    for _attempt in range(3):
                        _r = requests.get(url,
                                          headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                                          timeout=15)
                        if _r.status_code == 401:
                            token = _bling_token(force_refresh=True)
                            time.sleep(1)
                            continue
                        if _r.status_code == 429:
                            time.sleep(5)
                            continue
                        return _r
                    return _r

                r2 = _bling_get(f"https://api.bling.com.br/Api/v3/produtos/{pid}")
                time.sleep(0.5)
                if r2.status_code != 200:
                    raise ValueError(f"Bling GET {pid} retornou {r2.status_code}")
                dados = r2.json().get("data") or {}
                componentes = (dados.get("estrutura") or {}).get("componentes") or []

                if not componentes:
                    logger.info("[ALFAPARF-BUNDLE] Ignorado pid=%d (sem estrutura)", pid)
                    _ALFAPARF_AMAZON_BUNDLE_JOB["ignorados"] += 1
                    _ALFAPARF_AMAZON_BUNDLE_JOB["log"].append({
                        "pid": pid, "sku": sku, "titulo": titulo[:80],
                        "status": "ignorado", "motivo": "sem_estrutura",
                    })
                    time.sleep(0.3)
                    continue

                # 3. Pegar EAN da coloração (primeiro componente sem EAN do oxidante)
                ean_coloring = ""
                for c in componentes:
                    comp_id = (c.get("produto") or {}).get("id")
                    if not comp_id:
                        continue
                    ean = _ean_componente(lambda: token, comp_id, ean_cache)
                    # Oxidante tem sempre o mesmo EAN (7899884221518); coloração é único por cor
                    if ean and ean != "7899884221518":
                        ean_coloring = ean
                        break
                # Fallback: qualquer EAN disponível
                if not ean_coloring:
                    for c in componentes:
                        comp_id = (c.get("produto") or {}).get("id")
                        if comp_id:
                            ean_coloring = _ean_componente(lambda: token, comp_id, ean_cache)
                            if ean_coloring:
                                break

                if not ean_coloring:
                    logger.info("[ALFAPARF-BUNDLE] Ignorado pid=%d (sem EAN nos componentes)", pid)
                    _ALFAPARF_AMAZON_BUNDLE_JOB["ignorados"] += 1
                    _ALFAPARF_AMAZON_BUNDLE_JOB["log"].append({
                        "pid": pid, "sku": sku, "titulo": titulo[:80],
                        "status": "ignorado", "motivo": "sem_ean_componente",
                    })
                    time.sleep(0.3)
                    continue

                # 4. Exportar como bundle
                payload = ExportarAmazonPayload(
                    produto_id=int(pid),
                    product_type=product_type,
                    condition_type="new_new",
                    fulfillment_channel="DEFAULT",
                    usar_simulador=False,
                    preco_override=ALFAPARF_PRECO,
                    titulo_override=titulo,
                    ean_override=ean_coloring,
                    is_bundle=True,
                    numero_itens=2,
                )
                resultado = amazon_exportar_produto(payload)
                sku_exp = resultado.get("sku", sku)
                logger.info("[ALFAPARF-BUNDLE] OK pid=%d sku=%s ean=%s titulo=%s",
                            pid, sku_exp, ean_coloring, titulo[:50])
                _ALFAPARF_AMAZON_BUNDLE_JOB["ok"] += 1
                _ALFAPARF_AMAZON_BUNDLE_JOB["log"].append({
                    "pid": pid, "sku": sku_exp, "ean_usado": ean_coloring,
                    "titulo": titulo[:80], "status": "ok",
                    "issues": [i for i in (resultado.get("issues") or []) if i.get("severity") == "ERROR"],
                })
            except Exception as exc:
                logger.error("[ALFAPARF-BUNDLE] ERRO pid=%d: %s", pid, exc)
                _ALFAPARF_AMAZON_BUNDLE_JOB["erros"] += 1
                _ALFAPARF_AMAZON_BUNDLE_JOB["log"].append({
                    "pid": pid, "sku": sku, "titulo": titulo[:80],
                    "status": "erro", "erro": str(exc)[:200],
                })
            time.sleep(2.5)  # evitar 429 Bling

        _ALFAPARF_AMAZON_BUNDLE_JOB["status"] = "done"
        logger.info("[ALFAPARF-BUNDLE] Concluído: ok=%d erros=%d ignorados=%d",
                    _ALFAPARF_AMAZON_BUNDLE_JOB["ok"],
                    _ALFAPARF_AMAZON_BUNDLE_JOB["erros"],
                    _ALFAPARF_AMAZON_BUNDLE_JOB["ignorados"])

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "msg": "Job iniciado. Acompanhe em /amazon/exportar-alfaparf-kits-bundle/status"}


@router.get("/amazon/exportar-alfaparf-kits-bundle/status")
def amazon_exportar_alfaparf_kits_bundle_status():
    return _ALFAPARF_AMAZON_BUNDLE_JOB
