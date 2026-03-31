# ================================
# SHINSEI PRICING ENGINE (FINAL)
# ================================

from dataclasses import dataclass
from typing import Any, Optional
import math


# ================================
# HELPERS
# ================================
def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", "."))
    except:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except:
        return default


def _round(value: float) -> float:
    return round(float(value), 2)


def _round_ending(value: float, ending="90") -> float:
    if ending == "sem":
        return _round(value)

    endings = {"90": 0.90, "99": 0.99, "97": 0.97}
    frac = endings.get(ending, 0.90)

    base = int(value)
    result = base + frac

    if result < value:
        result = base + 1 + frac

    return _round(result)


# ================================
# EXTRAÇÃO BLING (CORRIGIDO)
# ================================
def _extrair_produto_bling(produto: dict) -> dict:
    produto = produto or {}

    estoque = _safe_int(
        produto.get("saldoVirtualTotal")
        or produto.get("saldoFisicoTotal")
        or produto.get("estoque")
        or 0
    )

    ean = (
        produto.get("gtin")
        or produto.get("gtinEan")
        or produto.get("codigoBarras")
        or produto.get("codigo_barras")
        or produto.get("ean")
        or ""
    )

    return {
        "id": produto.get("id"),
        "nome": produto.get("nome") or "",
        "codigo": produto.get("codigo") or produto.get("sku") or "",
        "ean": ean,
        "preco": _safe_float(produto.get("preco")),
        "precoCusto": _safe_float(
            produto.get("precoCusto")
            or produto.get("preco_custo")
            or produto.get("custo")
        ),
        "peso": _safe_float(
            produto.get("pesoLiquido")
            or produto.get("pesoBruto")
            or produto.get("peso")
        ),
        "estoque": estoque,
        "raw": produto,
    }


# ================================
# CÁLCULO PRINCIPAL
# ================================
@dataclass
class Resultado:
    canal: str
    preco_final: float
    lucro: float
    lucro_liquido: float
    margem: float


def calcular_preco(
    custo,
    embalagem,
    frete,
    comissao,
    imposto,
    margem_alvo,
):
    custo_total = custo + embalagem

    preco = custo_total / (1 - (comissao + imposto + margem_alvo) / 100)

    lucro_bruto = preco - custo_total - frete - (preco * comissao / 100)
    imposto_valor = preco * imposto / 100
    lucro_liquido = lucro_bruto - imposto_valor

    margem = (lucro_liquido / preco) * 100

    return preco, lucro_bruto, lucro_liquido, margem


# ================================
# SIMULADOR MULTICANAL
# ================================
def calcular_canais(
    regras,
    preco_compra,
    embalagem,
    peso,
    imposto,
    quantidade,
    objetivo,
    tipo_alvo,
    valor_alvo,
):
    resultados = []

    for regra in regras:
        canal = regra.get("canal")

        frete = _safe_float(regra.get("taxa_frete"))
        comissao = _safe_float(regra.get("comissao"))
        taxa_fixa = _safe_float(regra.get("taxa_fixa"))

        preco, lucro, lucro_liquido, margem = calcular_preco(
            custo=preco_compra * quantidade,
            embalagem=embalagem,
            frete=frete,
            comissao=comissao,
            imposto=imposto,
            margem_alvo=valor_alvo,
        )

        resultados.append({
            "canal": canal,
            "preco_final": _round(preco),
            "lucro": _round(lucro),
            "lucro_liquido": _round(lucro_liquido),
            "margem": _round(margem),
        })

    melhor = max(resultados, key=lambda x: x["lucro_liquido"])

    return {
        "ok": True,
        "canais": resultados,
        "melhor_canal": melhor["canal"],
        "melhor_resultado": melhor,
    }


# ================================
# BUSCA PRODUTO BLING
# ================================
def _buscar_produto_bling(client, criterio, valor):
    criterio = criterio.lower()

    if criterio == "id":
        return _extrair_produto_bling(client.get_product(int(valor)))

    if criterio == "sku":
        resp = client.get_product_by_sku(valor)
    elif criterio == "ean":
        resp = client.get_product_by_ean(valor)
    else:
        resp = client.get_product_by_name(valor)

    if resp and "produtos" in resp and resp["produtos"]:
        return _extrair_produto_bling(resp["produtos"][0])

    raise ValueError("Produto não encontrado")


# ================================
# FUNÇÃO FINAL (BLING)
# ================================
def montar_precificacao_bling(
    regras,
    criterio,
    valor_busca,
    embalagem,
    imposto,
    quantidade,
    objetivo,
    tipo_alvo,
    valor_alvo,
):
    from bling_client import BlingClient

    client = BlingClient()

    produto = _buscar_produto_bling(client, criterio, valor_busca)

    resultado = calcular_canais(
        regras=regras,
        preco_compra=produto["precoCusto"],
        embalagem=embalagem,
        peso=produto["peso"],
        imposto=imposto,
        quantidade=quantidade,
        objetivo=objetivo,
        tipo_alvo=tipo_alvo,
        valor_alvo=valor_alvo,
    )

    return {
        "ok": True,
        "produto": produto,
        "canais": resultado["canais"],
        "melhor": resultado["melhor_resultado"],
    }