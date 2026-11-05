"""
services/frete_cobertura.py — Cobertura da entrega própria Shinsei, por CEP.

  - Micro-região: até 4 km em linha reta do depósito (coordenadas por CEP da AwesomeAPI).
  - RMSP: área onde o Flex do Mercado Livre entrega para a conta Shinsei. A cobertura é por CEP,
    não por cidade, então é consultada em shipping_options de um anúncio Flex com estoque.

Resultados ficam em cache no disco. Retorno None significa "não foi possível consultar":
quem chama decide o fallback.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from pathlib import Path

import httpx

logger = logging.getLogger("shinsei.frete.cobertura")

_CACHE_PATH = Path(__file__).parent.parent / "data" / "frete_cobertura_cache.json"
_LOCK = threading.Lock()
_DIA = 86400
_TTL_GEO = 180 * _DIA
_TTL_FLEX_COBERTO = 7 * _DIA
_TTL_FLEX_FORA = 1 * _DIA
_CACHE_MAX = 20000

# Depósito: CEP 06036-003 (Av. Padre Vicente Melillo, Osasco), mesma fonte das coordenadas dos clientes
DEPOSITO_LAT = -23.5545295
DEPOSITO_LON = -46.7790483
RAIO_MICROREGIAO_KM = float(os.getenv("FRETE_RAIO_MICROREGIAO_KM", "4.0"))

# Usadas só se a consulta de coordenadas falhar
_MICROREGIAO_FAIXAS_CEP: list[tuple[int, int]] = [
    (6020000, 6029999),  # Vila Yara / Vila Campesina
    (6030000, 6036999),  # Adalgisa / Umuarama
    (6040000, 6045999),  # Jardim D'Abril
    (6050000, 6069999),  # Jaguaribe / Novo Osasco / Cipava
]

# Anúncios Flex da conta Shinsei com estoque alto; usa o próximo se um ficar sem estoque
_FLEX_ITENS_PADRAO = "MLB2779061578,MLB7260663398,MLB2797380605,MLB5148789208"
_METODO_FLEX_MESMO_DIA = 512746
_METODO_DIA_SEGUINTE = 507945


def _ler_cache() -> dict:
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _gravar_cache(secao: str, cep: str, valor) -> None:
    with _LOCK:
        cache = _ler_cache()
        dados = cache.setdefault(secao, {})
        dados[cep] = [valor, time.time()]
        if len(dados) > _CACHE_MAX:
            for chave in sorted(dados, key=lambda c: dados[c][1])[: len(dados) - _CACHE_MAX]:
                del dados[chave]
        try:
            _CACHE_PATH.parent.mkdir(exist_ok=True)
            _CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
        except Exception as exc:
            logger.warning("Falha ao gravar cache de cobertura: %s", exc)


def _do_cache(secao: str, cep: str):
    """Retorna (encontrado, valor) respeitando o prazo de validade da seção."""
    registro = _ler_cache().get(secao, {}).get(cep)
    if not registro:
        return False, None
    valor, ts = registro
    if secao == "geo":
        ttl = _TTL_GEO
    else:
        ttl = _TTL_FLEX_COBERTO if valor else _TTL_FLEX_FORA
    return (True, valor) if time.time() - ts < ttl else (False, None)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(a))


def distancia_deposito_km(cep: str) -> float | None:
    """Distância em linha reta do CEP até o depósito, ou None se não houver coordenada."""
    achou, valor = _do_cache("geo", cep)
    if achou:
        return valor
    try:
        resp = httpx.get(f"https://cep.awesomeapi.com.br/json/{cep}", timeout=3.0)
        resp.raise_for_status()
        dados = resp.json()
        km = round(_haversine_km(DEPOSITO_LAT, DEPOSITO_LON, float(dados["lat"]), float(dados["lng"])), 3)
    except Exception as exc:
        logger.warning("Coordenadas indisponíveis para CEP %s: %s", cep, exc)
        return None
    _gravar_cache("geo", cep, km)
    return km


def esta_na_microregiao(cep: str) -> bool:
    km = distancia_deposito_km(cep)
    if km is not None:
        return km <= RAIO_MICROREGIAO_KM
    numero = int(cep) if cep.isdigit() else -1
    return any(ini <= numero <= fim for ini, fim in _MICROREGIAO_FAIXAS_CEP)


def _opcoes_indicam_flex(opcoes: list[dict]) -> bool:
    """Mesmo dia, ou dia seguinte sem custo quando o horário de corte do mesmo dia já passou."""
    for opcao in opcoes:
        metodo = opcao.get("shipping_method_id")
        if metodo == _METODO_FLEX_MESMO_DIA:
            return True
        if metodo == _METODO_DIA_SEGUINTE and not opcao.get("cost"):
            return True
    return False


def tem_cobertura_flex(cep: str) -> bool | None:
    """True/False conforme o Flex da conta Shinsei atende o CEP; None se não deu para consultar."""
    achou, valor = _do_cache("flex", cep)
    if achou:
        return valor
    try:
        from services.mercado_livre import obter_token_ml
        token = obter_token_ml()
    except Exception as exc:
        logger.warning("Flex: token ML indisponível: %s", exc)
        return None

    itens = [i.strip() for i in os.getenv("FRETE_FLEX_ITENS", _FLEX_ITENS_PADRAO).split(",") if i.strip()]
    for item in itens:
        try:
            resp = httpx.get(
                f"https://api.mercadolibre.com/items/{item}/shipping_options",
                params={"zip_code": cep},
                headers={"Authorization": f"Bearer {token}", "x-format-new": "true"},
                timeout=4.0,
            )
        except Exception as exc:
            logger.warning("Flex: falha ao consultar CEP %s: %s", cep, exc)
            return None
        if resp.status_code == 200:
            coberto = _opcoes_indicam_flex(resp.json().get("options") or [])
            _gravar_cache("flex", cep, coberto)
            return coberto
        # 404 = anúncio sem estoque ou CEP inválido: tenta o próximo anúncio
        if resp.status_code != 404:
            logger.warning("Flex: HTTP %s ao consultar CEP %s", resp.status_code, cep)
            return None
    logger.warning("Flex: nenhum anúncio de referência respondeu para o CEP %s", cep)
    return None


def e_entrega_propria(cep: str) -> bool | None:
    """Micro-região ou área Flex; None se a cobertura Flex não pôde ser consultada."""
    return True if esta_na_microregiao(cep) else tem_cobertura_flex(cep)
