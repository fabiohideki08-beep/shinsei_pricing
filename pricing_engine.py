from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
import math


# ============================================================
# Constantes / canais
# ============================================================
ORDEM_CANAIS = [
    "Mercado Livre Classico",
    "Mercado Livre Premium",
    "Shopee",
    "Amazon",
    "Shein",
    "Shopify",
    "Shopfy",
]

FORCAS_PADRAO = {
    "Mercado Livre Classico": 0.80,
    "Mercado Livre Premium": 0.75,
    "Shopee": 0.60,
    "Amazon": 0.70,
    "Shein": 0.55,
    "Shopify": 0.65,
    "Shopfy": 0.65,
}


# ============================================================
# Helpers básicos
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


def _round_money(value: float) -> float:
    return round(float(value), 2)


def _round_up_to_ending(value: float, ending: str = "90") -> float:
    """
    Arredonda para cima para terminar em:
    - 90 -> x,90
    - 99 -> x,99
    - 97 -> x,97
    - sem -> sem arredondamento especial
    """
    value = _safe_float(value)

    if ending in (None, "", "sem"):
        return _round_money(value)

    endings = {"90": 0.90, "99": 0.99, "97": 0.97}
    frac_target = endings.get(str(ending), 0.90)

    inteiro = math.floor(value)
    candidato = inteiro + frac_target

    if candidato < value - 1e-9:
        candidato = (inteiro + 1) + frac_target

    return _round_money(candidato)


def _normalize_canal_name(canal: str) -> str:
    txt = str(canal or "").strip()
    if txt.lower() == "shopfy":
        return "Shopify"
    return txt


def _canal_sort_key(canal: str) -> tuple:
    canal_norm = _normalize_canal_name(canal)
    try:
        idx = ORDEM_CANAIS.index(canal_norm)
    except ValueError:
        idx = 999
    return (idx, canal_norm)


# ============================================================
# Regras
# ============================================================
def _regra_ativa(regra: dict) -> bool:
    return bool(regra.get("ativo", True))


def _filtrar_regras_canal(regras: list[dict], canal: str) -> list[dict]:
    canal_norm = _normalize_canal_name(canal)
    itens = []
    for r in regras:
        if not isinstance(r, dict):
            continue
        if not _regra_ativa(r):
            continue
        rc = _normalize_canal_name(r.get("canal", ""))
        if rc == canal_norm:
            itens.append(r)
    return itens


def _faixa_match(regra: dict, peso: float, preco: float) -> bool:
    peso_min = _safe_float(regra.get("peso_min"), 0)
    peso_max = _safe_float(regra.get("peso_max"), 999999999)
    preco_min = _safe_float(regra.get("preco_min"), 0)
    preco_max = _safe_float(regra.get("preco_max"), 999999999)

    return (
        peso >= peso_min
        and peso <= peso_max
        and preco >= preco_min
        and preco <= preco_max
    )


def _buscar_regra_por_preco(
    regras_canal: list[dict],
    peso: float,
    preco: float,
) -> Optional[dict]:
    matches = [r for r in regras_canal if _faixa_match(r, peso, preco)]

    if matches:
        matches.sort(
            key=lambda r: (
                _safe_float(r.get("peso_max"), 999999999) - _safe_float(r.get("peso_min"), 0),
                _safe_float(r.get("preco_max"), 999999999) - _safe_float(r.get("preco_min"), 0),
            )
        )
        return matches[0]

    return None


def _buscar_regra_mais_proxima(
    regras_canal: list[dict],
    peso: float,
    preco: float,
) -> Optional[dict]:
    """
    Fallback caso nenhuma faixa bata exatamente.
    """
    if not regras_canal:
        return None

    def distancia(r: dict) -> float:
        peso_min = _safe_float(r.get("peso_min"), 0)
        peso_max = _safe_float(r.get("peso_max"), 999999999)
        preco_min = _safe_float(r.get("preco_min"), 0)
        preco_max = _safe_float(r.get("preco_max"), 999999999)

        dp = 0.0
        dv = 0.0

        if peso < peso_min:
            dp = peso_min - peso
        elif peso > peso_max:
            dp = peso - peso_max

        if preco < preco_min:
            dv = preco_min - preco
        elif preco > preco_max:
            dv = preco - preco_max

        return dp * 1000 + dv

    return min(regras_canal, key=distancia)


def _buscar_regra_para_preco(
    regras_canal: list[dict],
    peso: float,
    preco: float,
) -> Optional[dict]:
    regra = _buscar_regra_por_preco(regras_canal, peso, preco)
    if regra:
        return regra
    return _buscar_regra_mais_proxima(regras_canal, peso, preco)


def _descricao_faixa(regra: Optional[dict]) -> str:
    if not regra:
        return ""
    return (
        f"Peso {_safe_float(regra.get('peso_min')):.3f}-{_safe_float(regra.get('peso_max')):.3f} kg | "
        f"Preço {_safe_float(regra.get('preco_min')):.2f}-{_safe_float(regra.get('preco_max')):.2f}"
    )


# ============================================================
# Solver do preço
# ============================================================
@dataclass
class CanalResult:
    canal: str
    preco_final: float
    lucro: float
    lucro_liquido: float
    margem: float
    markup: float
    custo_total: float
    custo_produto: float
    custo_base: float
    embalagem: float
    quantidade: int
    peso: float
    frete: float
    comissao: float
    taxa_fixa: float
    imposto: float
    imposto_percentual: float
    faixa_aplicada: str
    regra_aplicada: dict
    indice_forca: float = 0.0
    indice_equilibrio: float = 0.0
    indice_lucro: float = 0.0
    indice_final: float = 0.0

    def to_dict(self) -> dict:
        return {
            "canal": self.canal,
            "preco_final": _round_money(self.preco_final),
            "lucro": _round_money(self.lucro),
            "lucro_liquido": _round_money(self.lucro_liquido),
            "margem": _round_money(self.margem),
            "markup": _round_money(self.markup),
            "custo_total": _round_money(self.custo_total),
            "custo_produto": _round_money(self.custo_produto),
            "custo_base": _round_money(self.custo_base),
            "embalagem": _round_money(self.embalagem),
            "quantidade": self.quantidade,
            "peso": _round_money(self.peso),
            "frete": _round_money(self.frete),
            "comissao": _round_money(self.comissao),
            "taxa_fixa": _round_money(self.taxa_fixa),
            "imposto": _round_money(self.imposto),
            "imposto_percentual": _round_money(self.imposto_percentual),
            "faixa_aplicada": self.faixa_aplicada,
            "regra_aplicada": self.regra_aplicada,
            "indice_forca": _round_money(self.indice_forca),
            "indice_equilibrio": _round_money(self.indice_equilibrio),
            "indice_lucro": _round_money(self.indice_lucro),
            "indice_final": _round_money(self.indice_final),
        }


def _resolver_preco_alvo(
    regras_canal: list[dict],
    peso: float,
    custo_base: float,
    imposto_percentual: float,
    objetivo: str,
    tipo_alvo: str,
    valor_alvo: float,
) -> tuple[float, dict]:
    """
    Resolve o preço final do canal considerando que frete/comissão dependem da faixa de preço.
    """
    imposto_percentual = _safe_float(imposto_percentual)
    valor_alvo = _safe_float(valor_alvo)

    if custo_base <= 0:
        raise ValueError("Custo base inválido para cálculo.")

    objetivo = str(objetivo or "markup").strip().lower()
    tipo_alvo = str(tipo_alvo or "percentual").strip().lower()

    if not regras_canal:
        raise ValueError("Canal sem regras cadastradas.")

    # limites amplos
    lo = max(0.01, custo_base)
    hi = max(lo * 5, 100.0)

    def lucro_e_regra(preco: float) -> tuple[float, dict]:
        regra = _buscar_regra_para_preco(regras_canal, peso, preco)
        if not regra:
            raise ValueError("Nenhuma regra encontrada para o canal.")

        frete = _safe_float(regra.get("taxa_frete"), 0)
        comissao_pct = _safe_float(regra.get("comissao"), 0)
        taxa_fixa = _safe_float(regra.get("taxa_fixa"), 0)

        comissao_rs = preco * (comissao_pct / 100.0)
        imposto_rs = preco * (imposto_percentual / 100.0)

        lucro_bruto = preco - custo_base - frete - comissao_rs - taxa_fixa
        lucro_liquido = lucro_bruto - imposto_rs
        return lucro_liquido, regra

    def objetivo_val(preco: float) -> float:
        lucro_liq, _ = lucro_e_regra(preco)

        if objetivo == "lucro_liquido":
            if tipo_alvo == "nominal":
                return lucro_liq - valor_alvo
            return lucro_liq - (preco * (valor_alvo / 100.0))

        if objetivo == "margem":
            margem = (lucro_liq / preco * 100.0) if preco > 0 else -999999
            if tipo_alvo == "nominal":
                return margem - valor_alvo
            return margem - valor_alvo

        # default: markup
        markup = (preco / custo_base) if custo_base > 0 else 0
        if tipo_alvo == "nominal":
            return markup - valor_alvo
        return markup - (1 + (valor_alvo / 100.0))

    # sobe hi até alcançar sinal positivo
    base_val = objetivo_val(lo)
    tries = 0
    while objetivo_val(hi) < 0 and tries < 50:
        hi *= 2
        tries += 1

    if tries >= 50:
        raise ValueError("Não foi possível encontrar preço viável para o canal.")

    # bisseção
    for _ in range(120):
        mid = (lo + hi) / 2
        val = objetivo_val(mid)
        if abs(val) < 1e-7:
            lo = hi = mid
            break
        if val < 0:
            lo = mid
        else:
            hi = mid

    preco = hi
    _, regra_final = lucro_e_regra(preco)
    return _round_money(preco), regra_final


def _calcular_um_canal(
    canal: str,
    regras: list[dict],
    preco_compra: float,
    embalagem: float,
    peso: float,
    imposto_percentual: float,
    quantidade: int,
    objetivo: str,
    tipo_alvo: str,
    valor_alvo: float,
) -> CanalResult:
    canal = _normalize_canal_name(canal)
    preco_compra = _safe_float(preco_compra)
    embalagem = _safe_float(embalagem)
    peso = _safe_float(peso)
    imposto_percentual = _safe_float(imposto_percentual)
    quantidade = max(1, _safe_int(quantidade, 1))

    custo_produto = preco_compra * quantidade
    custo_base = custo_produto + embalagem

    regras_canal = _filtrar_regras_canal(regras, canal)
    if not regras_canal:
        raise ValueError(f"Sem regras para o canal: {canal}")

    preco_final, regra = _resolver_preco_alvo(
        regras_canal=regras_canal,
        peso=peso,
        custo_base=custo_base,
        imposto_percentual=imposto_percentual,
        objetivo=objetivo,
        tipo_alvo=tipo_alvo,
        valor_alvo=valor_alvo,
    )

    frete = _safe_float(regra.get("taxa_frete"), 0)
    comissao_pct = _safe_float(regra.get("comissao"), 0)
    taxa_fixa = _safe_float(regra.get("taxa_fixa"), 0)

    comissao_rs = preco_final * (comissao_pct / 100.0)
    imposto_rs = preco_final * (imposto_percentual / 100.0)

    lucro_bruto = preco_final - custo_base - frete - comissao_rs - taxa_fixa
    lucro_liquido = lucro_bruto - imposto_rs
    margem = (lucro_liquido / preco_final * 100.0) if preco_final > 0 else 0.0
    markup = (preco_final / custo_base) if custo_base > 0 else 0.0
    custo_total = custo_base + frete + comissao_rs + taxa_fixa + imposto_rs

    return CanalResult(
        canal=canal,
        preco_final=preco_final,
        lucro=lucro_bruto,
        lucro_liquido=lucro_liquido,
        margem=margem,
        markup=markup,
        custo_total=custo_total,
        custo_produto=custo_produto,
        custo_base=custo_base,
        embalagem=embalagem,
        quantidade=quantidade,
        peso=peso,
        frete=frete,
        comissao=comissao_rs,
        taxa_fixa=taxa_fixa,
        imposto=imposto_rs,
        imposto_percentual=imposto_percentual,
        faixa_aplicada=_descricao_faixa(regra),
        regra_aplicada=regra,
    )


# ============================================================
# Índice de competitividade
# ============================================================
def _aplicar_indices(
    canais: list[CanalResult],
    score_config: Optional[dict] = None,
) -> list[CanalResult]:
    if not canais:
        return canais

    score_config = score_config or {}
    peso_forca = _safe_float(score_config.get("peso_forca"), 0.40)
    peso_equilibrio = _safe_float(score_config.get("peso_equilibrio"), 0.40)
    peso_lucro = _safe_float(score_config.get("peso_lucro"), 0.20)
    forcas_canais = {**FORCAS_PADRAO, **(score_config.get("forcas_canais") or {})}

    precos = [c.preco_final for c in canais]
    lucros = [c.lucro_liquido for c in canais]

    media_preco = sum(precos) / len(precos) if precos else 0.0
    media_lucro = sum(lucros) / len(lucros) if lucros else 0.0
    max_lucro = max(lucros) if lucros else 1.0

    for canal in canais:
        forca = _safe_float(forcas_canais.get(canal.canal), FORCAS_PADRAO.get(canal.canal, 0.50))

        if media_preco > 0:
            desvio = abs(canal.preco_final - media_preco) / media_preco
            equilibrio = max(0.0, 1.0 - desvio)
        else:
            equilibrio = 0.0

        if max_lucro > 0:
            lucro_idx = max(0.0, canal.lucro_liquido / max_lucro)
        else:
            lucro_idx = 0.0

        if canal.lucro_liquido < 0:
            lucro_idx = 0.0
            equilibrio *= 0.5

        final = (
            forca * peso_forca
            + equilibrio * peso_equilibrio
            + lucro_idx * peso_lucro
        )

        canal.indice_forca = forca * 100.0
        canal.indice_equilibrio = equilibrio * 100.0
        canal.indice_lucro = lucro_idx * 100.0
        canal.indice_final = final * 100.0

    return canais


# ============================================================
# API principal do engine
# ============================================================
def calcular_canais(
    regras: list[dict],
    preco_compra: float,
    embalagem: float,
    peso: float,
    imposto: float,
    quantidade: int,
    objetivo: str = "markup",
    tipo_alvo: str = "percentual",
    valor_alvo: float = 30,
    score_config: Optional[dict] = None,
) -> dict:
    regras = [r for r in (regras or []) if isinstance(r, dict) and _regra_ativa(r)]
    if not regras:
        raise ValueError("Nenhuma regra disponível.")

    canais_unicos = sorted(
        {_normalize_canal_name(r.get("canal", "")) for r in regras if r.get("canal")},
        key=_canal_sort_key,
    )

    resultados: list[CanalResult] = []
    erros: list[dict] = []

    for canal in canais_unicos:
        try:
            result = _calcular_um_canal(
                canal=canal,
                regras=regras,
                preco_compra=preco_compra,
                embalagem=embalagem,
                peso=peso,
                imposto_percentual=imposto,
                quantidade=quantidade,
                objetivo=objetivo,
                tipo_alvo=tipo_alvo,
                valor_alvo=valor_alvo,
            )
            resultados.append(result)
        except Exception as exc:
            erros.append({"canal": canal, "erro": str(exc)})

    if not resultados:
        raise ValueError("Nenhum canal pôde ser calculado.")

    resultados = _aplicar_indices(resultados, score_config=score_config)

    resultados.sort(key=lambda x: (-x.indice_final, -x.lucro_liquido, _canal_sort_key(x.canal)))

    melhor = resultados[0]

    canais_dict = [r.to_dict() for r in resultados]

    return {
        "ok": True,
        "canais": canais_dict,
        "melhor_canal": melhor.canal,
        "melhor_resultado": melhor.to_dict(),
        "resumo": {
            "preco_compra": _round_money(preco_compra),
            "embalagem": _round_money(embalagem),
            "peso": _round_money(peso),
            "imposto": _round_money(imposto),
            "quantidade": max(1, _safe_int(quantidade, 1)),
            "objetivo": objetivo,
            "tipo_alvo": tipo_alvo,
            "valor_alvo": _round_money(valor_alvo),
        },
        "erros": erros,
    }


# ============================================================
# Preço virtual / integração
# ============================================================
def _preco_virtual(
    preco_base: float,
    modo_preco_virtual: str = "percentual_acima",
    acrescimo_percentual: float = 20,
    acrescimo_nominal: float = 0,
    preco_manual: float = 0,
    arredondamento: str = "90",
) -> float:
    preco_base = _safe_float(preco_base)
    acrescimo_percentual = _safe_float(acrescimo_percentual)
    acrescimo_nominal = _safe_float(acrescimo_nominal)
    preco_manual = _safe_float(preco_manual)

    modo = str(modo_preco_virtual or "percentual_acima").strip().lower()

    if modo == "manual" and preco_manual > 0:
        preco = preco_manual
    elif modo == "valor_acima":
        preco = preco_base + acrescimo_nominal
    else:
        preco = preco_base * (1 + acrescimo_percentual / 100.0)

    return _round_up_to_ending(preco, arredondamento)


def _aplicar_regra_estoque(
    preco_promocional: float,
    preco_virtual: float,
    estoque: int,
    regra_estoque: Optional[dict],
) -> tuple[float, float, dict]:
    regra_estoque = regra_estoque or {}
    ativo = bool(regra_estoque.get("ativo", False))
    limite = _safe_int(regra_estoque.get("limite"), 2)
    tipo = str(regra_estoque.get("tipo", "percentual")).lower()
    valor = _safe_float(regra_estoque.get("valor"), 0)

    auditoria = {
        "regra_estoque_ativa": ativo,
        "estoque": estoque,
        "limite": limite,
        "tipo": tipo,
        "valor": valor,
        "aplicada": False,
    }

    if not ativo or estoque > limite or valor <= 0:
        return preco_promocional, preco_virtual, auditoria

    if tipo == "nominal":
        preco_promocional += valor
        preco_virtual += value if False else valor
    else:
        fator = 1 + (valor / 100.0)
        preco_promocional *= fator
        preco_virtual *= fator

    auditoria["aplicada"] = True
    return _round_money(preco_promocional), _round_money(preco_virtual), auditoria


def gerar_integracao(
    canais: list[dict],
    modo_preco_virtual: str = "percentual_acima",
    acrescimo_percentual: float = 20,
    acrescimo_nominal: float = 0,
    preco_manual: float = 0,
    arredondamento: str = "90",
    modo_aprovacao: str = "manual",
    preco_compra_bling: float = 0,
    preco_compra_anterior_bling: float = 0,
    estoque: int = 0,
    regra_estoque: Optional[dict] = None,
) -> dict:
    itens = []
    auditoria = {
        "modo_aprovacao": modo_aprovacao,
        "modo_preco_virtual": modo_preco_virtual,
        "acrescimo_percentual": _safe_float(acrescimo_percentual),
        "acrescimo_nominal": _safe_float(acrescimo_nominal),
        "preco_manual": _safe_float(preco_manual),
        "arredondamento": arredondamento,
        "preco_compra_bling": _round_money(preco_compra_bling),
        "preco_compra_anterior_bling": _round_money(preco_compra_anterior_bling),
        "estoque": _safe_int(estoque),
        "itens": [],
    }

    for canal in canais or []:
        preco_final = _safe_float(canal.get("preco_final"), 0)
        promo = _round_money(preco_final)

        virtual = _preco_virtual(
            preco_base=promo,
            modo_preco_virtual=modo_preco_virtual,
            acrescimo_percentual=acrescimo_percentual,
            acrescimo_nominal=acrescimo_nominal,
            preco_manual=preco_manual,
            arredondamento=arredondamento,
        )

        promo2, virtual2, audit_estoque = _aplicar_regra_estoque(
            preco_promocional=promo,
            preco_virtual=virtual,
            estoque=_safe_int(estoque),
            regra_estoque=regra_estoque,
        )

        item = {
            "canal": canal.get("canal"),
            "preco_final": promo,
            "preco_promocional": promo2,
            "preco_virtual": virtual2,
            "preco_cheio": virtual2,
            "lucro": _safe_float(canal.get("lucro"), 0),
            "lucro_liquido": _safe_float(canal.get("lucro_liquido"), 0),
            "margem": _safe_float(canal.get("margem"), 0),
            "markup": _safe_float(canal.get("markup"), 0),
            "frete": _safe_float(canal.get("frete"), 0),
            "comissao": _safe_float(canal.get("comissao"), 0),
            "taxa_fixa": _safe_float(canal.get("taxa_fixa"), 0),
            "imposto": _safe_float(canal.get("imposto"), 0),
            "custo_total": _safe_float(canal.get("custo_total"), 0),
            "indice_final": _safe_float(canal.get("indice_final"), 0),
            "faixa_aplicada": canal.get("faixa_aplicada", ""),
            "regra_aplicada": canal.get("regra_aplicada", {}),
            "modo_aprovacao": modo_aprovacao,
            "auditoria_estoque": audit_estoque,
        }
        itens.append(item)
        auditoria["itens"].append(
            {
                "canal": item["canal"],
                "preco_promocional": item["preco_promocional"],
                "preco_virtual": item["preco_virtual"],
                "auditoria_estoque": audit_estoque,
            }
        )

    itens.sort(key=lambda x: (-_safe_float(x.get("indice_final")), -_safe_float(x.get("lucro_liquido"))))

    return {
        "ok": True,
        "itens": itens,
        "auditoria": auditoria,
    }


# ============================================================
# Integração Bling completa
# ============================================================
def _extrair_produto_bling(produto: dict) -> dict:
    produto = produto or {}

    estoque_obj = produto.get("estoque")
    if isinstance(estoque_obj, dict):
        estoque = _safe_int(
            estoque_obj.get("saldoVirtualTotal")
            or estoque_obj.get("saldoFisicoTotal")
            or estoque_obj.get("saldoVirtual")
            or 0
        )
    else:
        estoque = _safe_int(
            produto.get("saldoVirtualTotal")
            or produto.get("saldoFisicoTotal")
            or produto.get("estoque")
            or 0
        )

    return {
        "id": produto.get("id"),
        "nome": produto.get("nome") or produto.get("descricao") or "",
        "codigo": produto.get("codigo") or produto.get("sku") or "",
        "ean": produto.get("gtin") or produto.get("ean") or "",
        "preco": _safe_float(produto.get("preco"), 0),
        "precoCusto": _safe_float(
            produto.get("precoCusto")
            or produto.get("preco_custo")
            or produto.get("custo")
            or 0
        ),
        "peso": _safe_float(
            produto.get("pesoLiquido")
            or produto.get("pesoBruto")
            or produto.get("peso")
            or 0
        ),
        "estoque": estoque,
        "raw": produto,
    }


def _buscar_produto_bling(client: Any, criterio: str, valor_busca: str) -> dict:
    criterio = str(criterio or "ean").strip().lower()
    valor = str(valor_busca or "").strip()

    if not valor:
        raise ValueError("Informe um valor de busca do produto.")

    if criterio == "id":
        if hasattr(client, "get_product"):
            return _extrair_produto_bling(client.get_product(int(valor)))
        if hasattr(client, "get_product_by_id"):
            return _extrair_produto_bling(client.get_product_by_id(valor))

    if criterio == "ean" and hasattr(client, "get_product_by_ean"):
        resp = client.get_product_by_ean(valor)
        if isinstance(resp, dict) and "produto" in resp:
            return _extrair_produto_bling(resp["produto"])
        if isinstance(resp, dict) and "produtos" in resp and resp["produtos"]:
            return _extrair_produto_bling(resp["produtos"][0])
        return _extrair_produto_bling(resp)

    if criterio == "sku" and hasattr(client, "get_product_by_sku"):
        resp = client.get_product_by_sku(valor)
        if isinstance(resp, dict) and "produto" in resp:
            return _extrair_produto_bling(resp["produto"])
        if isinstance(resp, dict) and "produtos" in resp and resp["produtos"]:
            return _extrair_produto_bling(resp["produtos"][0])
        return _extrair_produto_bling(resp)

    if not hasattr(client, "list_products"):
        raise ValueError("Cliente Bling sem método compatível de busca.")

    payload = client.list_products()
    data = payload.get("data", payload if isinstance(payload, list) else [])
    termo = valor.lower()

    encontrados = []
    for item in data:
        prod = item.get("produto", item) if isinstance(item, dict) else {}
        nome = str(prod.get("nome") or "").lower()
        codigo = str(prod.get("codigo") or "").lower()
        gtin = str(prod.get("gtin") or "").lower()
        pid = str(prod.get("id") or "").lower()

        ok = False
        if criterio == "nome":
            ok = termo in nome
        elif criterio == "sku":
            ok = termo == codigo
        elif criterio == "ean":
            ok = termo == gtin
        elif criterio == "id":
            ok = termo == pid

        if ok:
            encontrados.append(prod)

    if not encontrados:
        raise ValueError("Produto não encontrado no Bling.")

    return _extrair_produto_bling(encontrados[0])


def montar_precificacao_bling(
    regras: list[dict],
    criterio: str,
    valor_busca: str,
    embalagem: float,
    imposto: float,
    quantidade: int,
    objetivo: str,
    tipo_alvo: str,
    valor_alvo: float,
    peso_override: float = 0,
    score_config: Optional[dict] = None,
    modo_aprovacao: str = "manual",
    preco_compra_anterior_bling: float = 0,
    modo_preco_virtual: str = "percentual_acima",
    acrescimo_percentual: float = 20,
    acrescimo_nominal: float = 0,
    preco_manual: float = 0,
    arredondamento: str = "90",
):
    try:
        from bling_client import BlingClient
    except Exception as exc:
        raise RuntimeError(f"bling_client.py não disponível: {exc}")

    client = BlingClient()
    produto = _buscar_produto_bling(client, criterio, valor_busca)

    preco_compra = _safe_float(produto.get("precoCusto"), 0)
    if preco_compra <= 0:
        raise ValueError("Produto sem preço de custo válido no Bling.")

    peso = _safe_float(peso_override, 0) if _safe_float(peso_override, 0) > 0 else _safe_float(produto.get("peso"), 0)
    if peso <= 0:
        raise ValueError("Produto sem peso válido no Bling. Informe peso override.")

    calculo = calcular_canais(
        regras=regras,
        preco_compra=preco_compra,
        embalagem=embalagem,
        peso=peso,
        imposto=imposto,
        quantidade=quantidade,
        objetivo=objetivo,
        tipo_alvo=tipo_alvo,
        valor_alvo=valor_alvo,
        score_config=score_config,
    )

    integracao = gerar_integracao(
        canais=calculo.get("canais", []),
        modo_preco_virtual=modo_preco_virtual,
        acrescimo_percentual=acrescimo_percentual,
        acrescimo_nominal=acrescimo_nominal,
        preco_manual=preco_manual,
        arredondamento=arredondamento,
        modo_aprovacao=modo_aprovacao,
        preco_compra_bling=preco_compra,
        preco_compra_anterior_bling=preco_compra_anterior_bling,
        estoque=_safe_int(produto.get("estoque"), 0),
        regra_estoque=None,
    )

    return {
        "ok": True,
        "produto_bling": produto,
        "canais": calculo.get("canais", []),
        "melhor_canal": calculo.get("melhor_canal"),
        "melhor_resultado": calculo.get("melhor_resultado"),
        "integracao": integracao,
        "auditoria": {
            "criterio": criterio,
            "valor_busca": valor_busca,
            "produto": {
                "id": produto.get("id"),
                "nome": produto.get("nome"),
                "codigo": produto.get("codigo"),
                "ean": produto.get("ean"),
                "precoCusto": produto.get("precoCusto"),
                "peso": peso,
                "estoque": produto.get("estoque"),
            },
            "params": {
                "embalagem": embalagem,
                "imposto": imposto,
                "quantidade": quantidade,
                "objetivo": objetivo,
                "tipo_alvo": tipo_alvo,
                "valor_alvo": valor_alvo,
                "modo_preco_virtual": modo_preco_virtual,
                "acrescimo_percentual": acrescimo_percentual,
                "acrescimo_nominal": acrescimo_nominal,
                "preco_manual": preco_manual,
                "arredondamento": arredondamento,
                "modo_aprovacao": modo_aprovacao,
            },
        },
    }