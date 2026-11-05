"""
services/frete.py — Motor de cálculo de frete para Shinsei Market (Shopify/Brasil)

Regras (valores vigentes em data/frete_config.json):
  - Micro-região (até 4 km do depósito): grátis a partir de R$10
  - RMSP (área coberta pelo Flex do Mercado Livre): grátis a partir de R$29,90
  - Abaixo do mínimo, nas duas regiões: R$12,90 fixo
  - Demais regiões: frete final = max(0, cotação Melhor Envio − subsídio)
  - Subsídio: R$2 no 1º produto, R$4 no 2º e R$5 em cada um dos demais
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_FRETE_CONFIG_PATH = Path(__file__).parent.parent / "data" / "frete_config.json"

import httpx
from pydantic import BaseModel

from services.frete_cobertura import esta_na_microregiao, tem_cobertura_flex

logger = logging.getLogger("shinsei.frete")

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Defaults de env (fallback se frete_config.json não existir)
_SUBSIDY_FIRST_ITEM_DEFAULT: float = float(os.getenv("FRETE_SUBSIDIO_PRIMEIRO_ITEM", "2.0"))
_SUBSIDY_SECOND_ITEM_DEFAULT: float = float(os.getenv("FRETE_SUBSIDIO_SEGUNDO_ITEM", "4.0"))
_SUBSIDY_OTHER_ITEMS_DEFAULT: float = float(os.getenv("FRETE_SUBSIDIO_DEMAIS_ITENS", "5.0"))
_ORIGIN_CEP_DEFAULT: str = os.getenv("FRETE_CEP_ORIGEM", "06036003")
_FRETE_REAL_DEFAULT_DEFAULT: float = float(os.getenv("FRETE_REAL_DEFAULT", "18.0"))

# Para compatibilidade com imports existentes (routes/frete.py importa estes nomes)
ORIGIN_CEP: str = _ORIGIN_CEP_DEFAULT


def carregar_config_frete() -> dict:
    """Lê configuração de frete do JSON (com fallback para defaults de env)."""
    cfg = {
        "subsidio_primeiro_item": _SUBSIDY_FIRST_ITEM_DEFAULT,
        "subsidio_segundo_item": _SUBSIDY_SECOND_ITEM_DEFAULT,
        "subsidio_demais_itens": _SUBSIDY_OTHER_ITEMS_DEFAULT,
        "frete_real_default": _FRETE_REAL_DEFAULT_DEFAULT,
        "cep_origem": _ORIGIN_CEP_DEFAULT,
        "frete_gratis_minimo": FRETE_GRATIS_MINIMO,
        "frete_gratis_minimo_micro": FRETE_GRATIS_MINIMO_MICRO,
    }
    try:
        if _FRETE_CONFIG_PATH.exists():
            saved = json.loads(_FRETE_CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in saved.items() if v is not None})
    except Exception:
        pass
    return cfg


def salvar_config_frete(cfg: dict) -> None:
    """Salva configuração de frete no JSON."""
    _FRETE_CONFIG_PATH.parent.mkdir(exist_ok=True)
    _FRETE_CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )

# Valor mínimo do pedido para frete grátis na RMSP e na micro-região do depósito
FRETE_GRATIS_MINIMO: float = float(os.getenv("FRETE_GRATIS_MINIMO", "29.90"))
FRETE_GRATIS_MINIMO_MICRO: float = float(os.getenv("FRETE_GRATIS_MINIMO_MICRO", "10.0"))

MELHOR_ENVIO_TOKEN: str = os.getenv("MELHOR_ENVIO_TOKEN", "")
MELHOR_ENVIO_SANDBOX: bool = os.getenv("MELHOR_ENVIO_SANDBOX", "false").lower() in ("1", "true", "yes")

_ME_BASE_PROD = "https://www.melhorenvio.com.br/api/v2"
_ME_BASE_SANDBOX = "https://sandbox.melhorenvio.com.br/api/v2"
MELHOR_ENVIO_BASE_URL = _ME_BASE_SANDBOX if MELHOR_ENVIO_SANDBOX else _ME_BASE_PROD

# Serviços que queremos da API do Melhor Envio
MELHOR_ENVIO_SERVICE_IDS = [1, 2, 3, 4, 31, 34]  # PAC, SEDEX, Jadlog .Package, Jadlog .Com, Loggi Express, Loggi Ponto

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
    "franco da rocha", "itapevi", "jandira", "mairiporã", "santana de parnaíba",
    "caieiras", "cajamar", "pirapora do bom jesus", "arujá", "biritiba mirim",
    "ferraz de vasconcelos", "guararema", "juquitiba", "mogi das cruzes",
    "salesópolis", "santa isabel", "são lourenço da serra", "vargem grande paulista",
    "rio grande da serra", "ribeirão pires", "poá", "itapecerica da serra",
    "embu-guaçu", "são caetano do sul",
}

RMSP_FREIGHT_VALUE: float = 8.0  # custo de referência da entrega própria na RMSP
RMSP_FRETE_ABAIXO_MINIMO: float = 12.90  # cobrado na entrega própria em pedidos abaixo do mínimo

_FRETE_HISTORICO_PATH = Path(__file__).parent.parent / "data" / "frete_historico.json"
_HISTORICO_MAX = 300  # máximo de registros mantidos


def registrar_historico_frete(entry: dict) -> None:
    """Salva uma entrada no histórico de fretes (últimos _HISTORICO_MAX registros)."""
    try:
        historico: list = []
        if _FRETE_HISTORICO_PATH.exists():
            historico = json.loads(_FRETE_HISTORICO_PATH.read_text(encoding="utf-8"))
        historico.insert(0, entry)
        historico = historico[:_HISTORICO_MAX]
        _FRETE_HISTORICO_PATH.parent.mkdir(exist_ok=True)
        _FRETE_HISTORICO_PATH.write_text(
            json.dumps(historico, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("Falha ao gravar histórico de frete: %s", exc)


def ler_historico_frete(limit: int = 100) -> list:
    """Retorna os últimos N registros do histórico."""
    try:
        if _FRETE_HISTORICO_PATH.exists():
            return json.loads(_FRETE_HISTORICO_PATH.read_text(encoding="utf-8"))[:limit]
    except Exception:
        pass
    return []


def calcular_subsidio_total(qty_items: int, cfg: dict | None = None) -> float:
    """
    Calcula o subsídio total para N itens.
    Regra: 1º item, 2º item e demais têm valores próprios (ex.: 3 itens = 2 + 4 + 5 = 11).
    Lê configuração do JSON (dinâmico, sem reiniciar).
    """
    if cfg is None:
        cfg = carregar_config_frete()
    if qty_items <= 0:
        return 0.0
    first, second, others = _valores_subsidio(cfg)
    total = first
    if qty_items >= 2:
        total += second
    if qty_items >= 3:
        total += (qty_items - 2) * others
    return round(total, 2)


def _valores_subsidio(cfg: dict) -> tuple[float, float, float]:
    return (
        float(cfg.get("subsidio_primeiro_item", _SUBSIDY_FIRST_ITEM_DEFAULT)),
        float(cfg.get("subsidio_segundo_item", _SUBSIDY_SECOND_ITEM_DEFAULT)),
        float(cfg.get("subsidio_demais_itens", _SUBSIDY_OTHER_ITEMS_DEFAULT)),
    )


def itens_para_frete_gratis(frete_real: float, cfg: dict) -> int:
    """Quantidade total de itens cujo subsídio cobre o frete real (0 se nunca cobrir)."""
    first, second, others = _valores_subsidio(cfg)
    if frete_real <= first:
        return 1
    if frete_real <= first + second:
        return 2
    if others <= 0:
        return 0
    return 2 + math.ceil((frete_real - first - second) / others)


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
    is_microregiao: bool = False


# ---------------------------------------------------------------------------
# Utilidades de CEP
# ---------------------------------------------------------------------------


def normalize_cep(cep: str) -> str:
    """Remove não-dígitos e pad para 8 chars."""
    digits = "".join(c for c in (cep or "") if c.isdigit())
    return digits.zfill(8)[:8]


async def get_city_from_cep(cep: str) -> dict[str, Any]:
    """
    Consulta CEP e retorna dict com city/state.
    Tenta ViaCEP primeiro; se falhar, usa BrasilAPI como fallback.
    """
    cep = normalize_cep(cep)

    # ── Tentativa 1: ViaCEP ───────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(f"https://viacep.com.br/ws/{cep}/json/")
            resp.raise_for_status()
            data = resp.json()
            if not data.get("erro"):
                return {
                    "city": data.get("localidade", ""),
                    "state": data.get("uf", ""),
                    "raw": data,
                }
    except Exception as exc:
        logger.debug("ViaCEP indisponível para CEP %s (%s) — tentando BrasilAPI", cep, exc)

    # ── Tentativa 2: BrasilAPI ────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(
                f"https://brasilapi.com.br/api/cep/v2/{cep}",
                headers={"User-Agent": "ShinseMarket/1.0"},
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "city": data.get("city", ""),
                "state": data.get("state", ""),
                "raw": data,
            }
    except Exception as exc:
        logger.warning("BrasilAPI também falhou para CEP %s: %s", cep, exc)

    return {"city": "", "state": "", "raw": {}}


def is_rmsp(city: str, state: str) -> bool:
    """Retorna True se a cidade/estado pertencem à Região Metropolitana de SP."""
    if (state or "").upper() != "SP":
        return False
    return _normalizar_cidade(city) in {_normalizar_cidade(c) for c in RMSP_MUNICIPALITIES}


def _normalizar_cidade(city: str) -> str:
    """Compara nomes de cidade sem acento, hífen ou diferença de maiúsculas."""
    texto = unicodedata.normalize("NFKD", (city or "").strip().lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.replace("-", " ").split())


# ---------------------------------------------------------------------------
# Integração Melhor Envio
# ---------------------------------------------------------------------------


async def get_real_freight(
    origin_cep: str,
    destination_cep: str,
    weight_kg: float,
    declared_value: float,
    dims: dict | None = None,
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

    _d = dims or {}
    body = {
        "from": {"postal_code": normalize_cep(origin_cep)},
        "to": {"postal_code": normalize_cep(destination_cep)},
        "products": [
            {
                "weight": weight_real_kg,
                "width":  float(_d.get("largura")      or 12),
                "height": float(_d.get("altura")        or 10),
                "length": float(_d.get("profundidade")  or 15),
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

    return sorted(options, key=lambda o: o.price_real)


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
    product_id: str | None = None,
) -> FreightResult:
    """
    Calcula o frete final aplicando a regra de subsídio cumulativo.

    Fórmula:
        subsidy_total = calcular_subsidio_total(qty_items)
        price_final   = max(0, price_real − subsidy_total)
    """
    dest_cep = normalize_cep(destination_cep)
    qty_items = max(1, int(qty_items))
    cfg_frete = carregar_config_frete()
    origin_cep_cfg = cfg_frete.get("cep_origem", ORIGIN_CEP)
    total_weight_kg = max(0.1, float(total_weight_kg))
    order_value = max(0.0, float(order_value))

    # Carrega dimensões do cache Shopify quando product_id fornecido
    dims: dict | None = None
    if product_id:
        try:
            from services.bling_dimensions import get_dims_for_product
            dims = get_dims_for_product(product_id)
            if dims and dims.get("peso_kg"):
                total_weight_kg = dims["peso_kg"]
        except Exception as exc:
            logger.debug("Falha ao carregar dims para product_id=%s: %s", product_id, exc)

    logger.info(
        "calculate_freight: cep=%s qty=%d peso=%.3fkg valor=R$%.2f product_id=%s",
        dest_cep, qty_items, total_weight_kg, order_value, product_id or "-",
    )

    # Dados do CEP de destino
    cep_info = await get_city_from_cep(dest_cep)
    city = cep_info.get("city") or ""
    state = (cep_info.get("state") or "").upper()
    _microregiao = await asyncio.to_thread(esta_na_microregiao, dest_cep)
    rmsp = True if _microregiao else await asyncio.to_thread(tem_cobertura_flex, dest_cep)
    if rmsp is None:
        # Mercado Livre indisponível: usa a lista oficial de cidades da RMSP
        rmsp = is_rmsp(city, state)
        logger.warning("calculate_freight: cobertura Flex indisponível para %s; usando cidade (rmsp=%s)", dest_cep, rmsp)

    subsidy_total = calcular_subsidio_total(qty_items, cfg_frete)

    if rmsp:
        # RMSP: entrega própria Shinsei — mesmo dia (pedidos até 12h, seg-sáb) ou dia seguinte
        _now_sp = datetime.now(timezone(timedelta(hours=-3)))  # horário de Brasília
        _weekday = _now_sp.weekday()   # 0=seg … 5=sáb, 6=dom
        _hour    = _now_sp.hour

        # Regras de entrega RMSP (entrega própria Shinsei):
        #   Seg–Sex antes das 12h  → Entrega Hoje       (0 dias)
        #   Seg–Sex após as 12h    → Receba Amanha       (1 dia)
        #   Sáb antes das 12h     → Entrega Hoje       (0 dias)
        #   Sáb após as 12h       → Receba Segunda-Feira (2 dias)
        #   Dom (qualquer horário) → Receba Segunda-Feira (1 dia)
        if _weekday <= 5 and _hour < 12:          # seg-sáb até 12h
            _delivery_days = 0
            _delivery_name = "Entrega Hoje"
        elif _weekday == 6:                        # domingo
            _delivery_days = 1
            _delivery_name = "Receba Segunda-Feira"
        elif _weekday == 5:                        # sábado após 12h
            _delivery_days = 2
            _delivery_name = "Entrega Segunda-Feira"
        else:                                      # seg-sex após 12h
            _delivery_days = 1
            _delivery_name = "Receba Amanha"

        raw_options = [
            FreightOption(
                name=_delivery_name,
                carrier="Shinsei Market",
                price_real=RMSP_FREIGHT_VALUE,
                price_final=0.0,
                subsidy=subsidy_total,
                delivery_days=_delivery_days,
                is_free=True,
            ),
        ]
        items_for_free = 0
        cheapest_final = 0.0
        is_free_cart = True
    else:
        raw_options = await get_real_freight(origin_cep_cfg, dest_cep, total_weight_kg, order_value, dims=dims)

        # Aplica subsídio a cada opção
        final_options: list[FreightOption] = []
        for opt in raw_options:
            _diff = round(opt.price_real - subsidy_total, 2)
            # Arredonda para zero diferenças menores que R$0,10 (centavos de floating point)
            final_price = 0.0 if 0.0 < _diff < 0.10 else max(0.0, _diff)
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

        cheapest_real = min((o.price_real for o in raw_options), default=0.0)
        cheapest_final = min((o.price_final for o in raw_options), default=0.0)
        is_free_cart = cheapest_final == 0.0

        items_for_free = 0 if is_free_cart else max(0, itens_para_frete_gratis(cheapest_real, cfg_frete) - qty_items)

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
        is_microregiao=_microregiao,
    )

    # ── Regra de mínimo: vale na entrega própria (micro-região e RMSP) ──
    # Abaixo do mínimo cobra R$12,90 fixo. Nas demais regiões o frete grátis depende só do subsídio.
    # Com order_value=0 (widget sem o preço) a regra fica a cargo do widget.
    if _microregiao:
        minimo_gratis = float(cfg_frete.get("frete_gratis_minimo_micro", FRETE_GRATIS_MINIMO_MICRO))
    else:
        minimo_gratis = float(cfg_frete.get("frete_gratis_minimo", FRETE_GRATIS_MINIMO))
    if rmsp and 0.0 < order_value < minimo_gratis:
        logger.info(
            "calculate_freight: entrega própria valor=R$%.2f < mínimo=R$%.2f — frete fixo R$%.2f.",
            order_value, minimo_gratis, RMSP_FRETE_ABAIXO_MINIMO,
        )
        result = result.model_copy(update={
            "options": [
                opt.model_copy(update={
                    "price_final": RMSP_FRETE_ABAIXO_MINIMO,
                    "subsidy": 0.0,
                    "is_free": False,
                })
                for opt in result.options
            ],
            "cheapest_final": RMSP_FRETE_ABAIXO_MINIMO,
            "is_free": False,
        })

    logger.info(
        "calculate_freight result: city=%s state=%s rmsp=%s subsidy=R$%.2f cheapest_final=R$%.2f free=%s",
        city, state, rmsp, subsidy_total, result.cheapest_final, result.is_free,
    )

    return result
