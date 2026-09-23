"""
services/frete.py — Motor de cálculo de frete para Shinsei Market (Shopify/Brasil)

Regras:
  - Todo produto embute R$8 de subsídio de frete no preço
  - Subsídio é cumulativo: R$8 × qty_items
  - Frete final cobrado = max(0, frete_real − R$8 × qty_items)
  - RMSP: frete_real = R$8 → sempre grátis
  - Outros: calcula via Melhor Envio ou tabela de fallback
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from pydantic import BaseModel

logger = logging.getLogger("shinsei.frete")

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

SUBSIDY_PER_ITEM: float = float(os.getenv("FRETE_SUBSIDIO_POR_ITEM", "8.0"))
ORIGIN_CEP: str = os.getenv("FRETE_CEP_ORIGEM", "06036003")

MELHOR_ENVIO_TOKEN: str = os.getenv("MELHOR_ENVIO_TOKEN", "")
MELHOR_ENVIO_SANDBOX: bool = os.getenv("MELHOR_ENVIO_SANDBOX", "true").lower() in ("1", "true", "yes")

_ME_BASE_PROD = "https://www.melhorenvio.com.br/api/v2"
_ME_BASE_SANDBOX = "https://sandbox.melhorenvio.com.br/api/v2"
MELHOR_ENVIO_BASE_URL = _ME_BASE_SANDBOX if MELHOR_ENVIO_SANDBOX else _ME_BASE_PROD

# Serviços que queremos da API do Melhor Envio
MELHOR_ENVIO_SERVICE_IDS = [1, 2, 3, 4]  # SEDEX, PAC, Jadlog Package, Jadlog .com

# Tabela fallback (frete_real em R$) por estado/região quando sem token
_FALLBACK_STATES: dict[str, float] = {
    # São Paulo interior (não-RMSP) tratado à parte por CEP
    # Sul/Sudeste
    "RJ": 22.0, "MG": 22.0, "ES": 22.0, "PR": 22.0, "SC": 22.0, "RS": 22.0,
    # Centro-Oeste
    "GO": 28.0, "MT": 28.0, "MS": 28.0, "DF": 28.0,
    # Nordeste/Norte
    "BA": 38.0, "SE": 38.0, "AL": 38.0, "PE": 38.0, "PB": 38.0, "RN": 38.0,
    "CE": 38.0, "PI": 38.0, "MA": 38.0,
    "AM": 38.0, "PA": 38.0, "AP": 38.0, "RR": 38.0, "RO": 38.0, "AC": 38.0, "TO": 38.0,
}
_FALLBACK_SP_INTERIOR: float = 18.0
_FALLBACK_DEFAULT: float = 38.0

RMSP_MUNICIPALITIES: set[str] = {
    "são paulo", "guarulhos", "osasco", "santo andré", "são bernardo do campo",
    "mauá", "diadema", "carapicuíba", "itaquaquecetuba", "taboão da serra",
    "suzano", "barueri", "embu das artes", "cotia", "francisco morato",
    "franco da rocha", "jandira", "mairiporã", "santana de parnaíba",
    "caieiras", "cajamar", "pirapora do bom jesus", "arujá", "biritiba mirim",
    "ferraz de vasconcelos", "guararema", "juquitiba", "mogi das cruzes",
    "salesópolis", "santa isabel", "são lourenço da serra", "vargem grande paulista",
    "rio grande da serra", "ribeirão pires", "poá", "itapecerica da serra",
    "embu-guaçu", "são caetano do sul",
}

RMSP_FREIGHT_VALUE: float = 8.0  # valor simbólico usado para RMSP (sempre grátis após subsídio)

# ---------------------------------------------------------------------------
# Modelos Pydantic
# ---------------------------------------------------------------------------


class FreightOption(BaseModel):
    name: str
    carrier: str
    price_real: float
    price_final: float
    subsidy: float
    delivery_days: int
    is_free: bool


class FreightResult(BaseModel):
    destination_cep: str
    city: str
    state: str
    is_rmsp: bool
    qty_items: int
    subsidy_total: float
    options: list[FreightOption]
    items_for_free_shipping: int
    cheapest_final: float
    is_free: bool


# ---------------------------------------------------------------------------
# Utilidades de CEP
# ---------------------------------------------------------------------------


def normalize_cep(cep: str) -> str:
    """Remove não-dígitos e pad para 8 chars."""
    digits = "".join(c for c in (cep or "") if c.isdigit())
    return digits.zfill(8)[:8]


async def get_city_from_cep(cep: str) -> dict[str, Any]:
    """Consulta ViaCEP e retorna dict com city/state (e demais campos)."""
    cep = normalize_cep(cep)
    url = f"https://viacep.com.br/ws/{cep}/json/"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            if data.get("erro"):
                logger.warning("ViaCEP: CEP %s não encontrado", cep)
                return {"city": "", "state": "", "raw": data}
            return {
                "city": data.get("localidade", ""),
                "state": data.get("uf", ""),
                "raw": data,
            }
    except Exception as exc:
        logger.error("ViaCEP error for CEP %s: %s", cep, exc)
        return {"city": "", "state": "", "raw": {}}


def is_rmsp(city: str, state: str) -> bool:
    """Retorna True se a cidade/estado pertencem à Região Metropolitana de SP."""
    if (state or "").upper() != "SP":
        return False
    normalized = (city or "").strip().lower()
    # normaliza acentos de forma simples para comparação
    import unicodedata
    normalized = unicodedata.normalize("NFKD", normalized)
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    # também tenta versão original
    original = (city or "").strip().lower()
    return normalized in RMSP_MUNICIPALITIES or original in RMSP_MUNICIPALITIES


# ---------------------------------------------------------------------------
# Integração Melhor Envio
# ---------------------------------------------------------------------------


async def get_real_freight(
    origin_cep: str,
    destination_cep: str,
    weight_kg: float,
    declared_value: float,
) -> list[FreightOption]:
    """
    Calcula frete real via Melhor Envio API.
    Se token não disponível ou erro, retorna fallback.
    """
    if not MELHOR_ENVIO_TOKEN:
        logger.info("Melhor Envio token não configurado — usando tabela fallback.")
        return await _fallback_freight(destination_cep, weight_kg)

    url = f"{MELHOR_ENVIO_BASE_URL}/me/shipment/calculate"
    headers = {
        "Authorization": f"Bearer {MELHOR_ENVIO_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "ShinseMarket/1.0 (contato@shinseimarket.com.br)",
    }

    # Melhor Envio espera peso em kg, dimensões em cm
    weight_g = max(weight_kg * 1000, 100)  # mínimo 100g
    weight_real_kg = weight_g / 1000

    body = {
        "from": {"postal_code": normalize_cep(origin_cep)},
        "to": {"postal_code": normalize_cep(destination_cep)},
        "products": [
            {
                "weight": weight_real_kg,
                "width": 12,
                "height": 10,
                "length": 15,
                "quantity": 1,
                "insurance_value": max(declared_value, 1.0),
            }
        ],
        "services": ",".join(str(s) for s in MELHOR_ENVIO_SERVICE_IDS),
        "options": {
            "receipt": False,
            "own_hand": False,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code == 401:
                logger.warning("Melhor Envio: token inválido/expirado — usando fallback.")
                return await _fallback_freight(destination_cep, weight_kg)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error("Melhor Envio HTTP error %s: %s", exc.response.status_code, exc)
        return await _fallback_freight(destination_cep, weight_kg)
    except Exception as exc:
        logger.error("Melhor Envio request error: %s", exc)
        return await _fallback_freight(destination_cep, weight_kg)

    options: list[FreightOption] = []
    for item in data if isinstance(data, list) else []:
        if item.get("error"):
            logger.debug("Melhor Envio service %s error: %s", item.get("id"), item.get("error"))
            continue
        price = float(item.get("price") or 0)
        if price <= 0:
            continue
        delivery_time = int(item.get("delivery_time") or item.get("delivery_range", {}).get("max", 7))
        options.append(
            FreightOption(
                name=str(item.get("name") or item.get("service") or "Correios"),
                carrier=str(item.get("company", {}).get("name") or item.get("carrier") or ""),
                price_real=price,
                price_final=price,  # subsídio aplicado depois em calculate_freight
                subsidy=0.0,
                delivery_days=delivery_time,
                is_free=False,
            )
        )

    if not options:
        logger.warning("Melhor Envio retornou zero opções válidas — usando fallback.")
        return await _fallback_freight(destination_cep, weight_kg)

    return options


async def _fallback_freight(destination_cep: str, weight_kg: float) -> list[FreightOption]:
    """Tabela de frete fallback por região quando Melhor Envio não está disponível."""
    cep_info = await get_city_from_cep(destination_cep)
    state = (cep_info.get("state") or "").upper()
    city = cep_info.get("city") or ""

    if is_rmsp(city, state):
        price = RMSP_FREIGHT_VALUE
    elif state == "SP":
        price = _FALLBACK_SP_INTERIOR
    else:
        price = _FALLBACK_STATES.get(state, _FALLBACK_DEFAULT)

    # Oferece PAC e SEDEX (estimativa simples)
    pac_days = _estimate_delivery_days(state, "pac")
    sedex_days = _estimate_delivery_days(state, "sedex")
    sedex_price = round(price * 1.8, 2)  # SEDEX ~80% mais caro que PAC

    options = [
        FreightOption(
            name="PAC",
            carrier="Correios",
            price_real=price,
            price_final=price,
            subsidy=0.0,
            delivery_days=pac_days,
            is_free=False,
        ),
        FreightOption(
            name="SEDEX",
            carrier="Correios",
            price_real=sedex_price,
            price_final=sedex_price,
            subsidy=0.0,
            delivery_days=sedex_days,
            is_free=False,
        ),
    ]
    return options


def _estimate_delivery_days(state: str, service: str) -> int:
    """Estimativa simples de prazo por região."""
    base = {"pac": 7, "sedex": 3}
    far_states = {"AM", "PA", "AP", "RR", "RO", "AC", "TO", "MA", "PI"}
    days = base.get(service, 7)
    if state in far_states:
        days += 3
    elif state in {"BA", "SE", "AL", "PE", "PB", "RN", "CE"}:
        days += 2
    elif state in {"GO", "MT", "MS", "DF"}:
        days += 1
    return days


# ---------------------------------------------------------------------------
# Função principal de cálculo
# ---------------------------------------------------------------------------


async def calculate_freight(
    destination_cep: str,
    qty_items: int,
    total_weight_kg: float,
    order_value: float,
) -> FreightResult:
    """
    Calcula o frete final aplicando a regra de subsídio cumulativo.

    Fórmula:
        subsidy_total = SUBSIDY_PER_ITEM × qty_items
        price_final   = max(0, price_real − subsidy_total)
    """
    dest_cep = normalize_cep(destination_cep)
    qty_items = max(1, int(qty_items))
    total_weight_kg = max(0.1, float(total_weight_kg))
    order_value = max(0.0, float(order_value))

    logger.info(
        "calculate_freight: cep=%s qty=%d peso=%.3fkg valor=R$%.2f",
        dest_cep, qty_items, total_weight_kg, order_value,
    )

    # Dados do CEP de destino
    cep_info = await get_city_from_cep(dest_cep)
    city = cep_info.get("city") or ""
    state = (cep_info.get("state") or "").upper()
    rmsp = is_rmsp(city, state)

    subsidy_total = round(SUBSIDY_PER_ITEM * qty_items, 2)

    if rmsp:
        # RMSP: frete_real simbólico = R$8, sempre grátis
        raw_options = [
            FreightOption(
                name="PAC",
                carrier="Correios",
                price_real=RMSP_FREIGHT_VALUE,
                price_final=0.0,
                subsidy=subsidy_total,
                delivery_days=3,
                is_free=True,
            ),
            FreightOption(
                name="SEDEX",
                carrier="Correios",
                price_real=RMSP_FREIGHT_VALUE,
                price_final=0.0,
                subsidy=subsidy_total,
                delivery_days=1,
                is_free=True,
            ),
        ]
        items_for_free = 0
        cheapest_final = 0.0
        is_free_cart = True
    else:
        raw_options = await get_real_freight(ORIGIN_CEP, dest_cep, total_weight_kg, order_value)

        # Aplica subsídio a cada opção
        final_options: list[FreightOption] = []
        for opt in raw_options:
            final_price = max(0.0, round(opt.price_real - subsidy_total, 2))
            final_options.append(
                FreightOption(
                    name=opt.name,
                    carrier=opt.carrier,
                    price_real=opt.price_real,
                    price_final=final_price,
                    subsidy=subsidy_total,
                    delivery_days=opt.delivery_days,
                    is_free=(final_price == 0.0),
                )
            )
        raw_options = final_options

        cheapest_real = min((o.price_real for o in raw_options), default=SUBSIDY_PER_ITEM)
        cheapest_final = min((o.price_final for o in raw_options), default=0.0)
        is_free_cart = cheapest_final == 0.0

        if is_free_cart:
            items_for_free = 0
        else:
            # Quantos itens precisam para zerare o frete mais barato
            items_needed = math.ceil(cheapest_real / SUBSIDY_PER_ITEM)
            items_for_free = max(0, items_needed - qty_items)

    result = FreightResult(
        destination_cep=dest_cep,
        city=city,
        state=state,
        is_rmsp=rmsp,
        qty_items=qty_items,
        subsidy_total=subsidy_total,
        options=raw_options,
        items_for_free_shipping=items_for_free if not rmsp else 0,
        cheapest_final=cheapest_final if not rmsp else 0.0,
        is_free=is_free_cart,
    )

    logger.info(
        "calculate_freight result: city=%s state=%s rmsp=%s subsidy=R$%.2f cheapest_final=R$%.2f free=%s",
        city, state, rmsp, subsidy_total, result.cheapest_final, result.is_free,
    )

    return result
