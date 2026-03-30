from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional
from bling_client import get_product_by_ean, get_product_by_id, get_product_by_sku

FORMULA_VERSION = "v2.2.0"


def _pct_excel(v):
    v = float(v or 0)
    return v / 100.0 if v > 1 else v


def _contains(texto: str, trecho: str) -> bool:
    return trecho.lower() in (texto or "").strip().lower()


def _sumifs(regras: List[Dict], campo: str, canal_contains: str = None, peso_valor: float = None, preco_valor: float = None):
    total = 0.0
    encontrados = []
    for r in regras:
        canal = str(r.get("canal", "")).strip()
        if canal_contains and not _contains(canal, canal_contains):
            continue
        peso_min = float(r.get("peso_min", 0) or 0)
        peso_max = float(r.get("peso_max", 0) or 0)
        preco_min = float(r.get("preco_min", 0) or 0)
        preco_max = float(r.get("preco_max", 0) or 0)
        if peso_valor is not None and not (peso_min <= peso_valor <= peso_max):
            continue
        if preco_valor is not None and not (preco_min <= preco_valor <= preco_max):
            continue
        total += float(r.get(campo, 0) or 0)
        encontrados.append(r)
    return total, encontrados


def _primeiro_valor(regras: List[Dict], canal_contains: str, campo: str):
    for r in regras:
        if _contains(str(r.get("canal", "")), canal_contains):
            return float(r.get(campo, 0) or 0)
    return 0.0


def _faixa_texto(regras_encontradas: List[Dict]):
    if not regras_encontradas:
        return ""
    r = regras_encontradas[0]
    return f"Peso {float(r.get('peso_min',0) or 0):g}-{float(r.get('peso_max',0) or 0):g} kg | Preço {float(r.get('preco_min',0) or 0):g}-{float(r.get('preco_max',0) or 0):g}"


def _metricas(preco_final, custo_base, taxa_fixa, frete, comissao, imposto, faixa):
    receita_liquida = preco_final - taxa_fixa - frete - (preco_final * comissao)
    lucro_bruto = receita_liquida - custo_base
    lucro_liquido = lucro_bruto - (preco_final * imposto)
    margem_liquida_percentual = ((lucro_liquido / preco_final) * 100) if preco_final else 0
    return {
        "preco_final": round(preco_final, 2),
        "receita_liquida": round(receita_liquida, 2),
        "lucro_bruto": round(lucro_bruto, 2),
        "lucro_liquido": round(lucro_liquido, 2),
        "margem_liquida_percentual": round(margem_liquida_percentual, 2),
        "faixa_aplicada": _faixa_texto(faixa),
    }


def _fatores_canal(regras, nome_canal, custo_base, peso):
    faixa = []
    taxa_fixa = 0.0
    frete = 0.0
    if nome_canal == "Mercado Livre Classico":
        comissao = _primeiro_valor(regras, "classico", "comissao")
        frete, faixa = _sumifs(regras, "taxa_frete", canal_contains="classico", peso_valor=peso, preco_valor=(custo_base / 0.7))
    elif nome_canal == "Mercado Livre Premium":
        comissao = _primeiro_valor(regras, "premium", "comissao")
        frete, faixa = _sumifs(regras, "taxa_frete", canal_contains="premium", peso_valor=peso, preco_valor=(custo_base / 0.7))
    elif nome_canal == "Shopee":
        comissao = 0.20
        taxa_fixa, _ = _sumifs(regras, "taxa_fixa", canal_contains="shopee", preco_valor=(custo_base / 0.6))
        frete, faixa = _sumifs(regras, "taxa_frete", canal_contains="shopee", preco_valor=(custo_base / 0.6))
    elif nome_canal == "Amazon":
        comissao = 0.13
        taxa_fixa, _ = _sumifs(regras, "taxa_fixa", canal_contains="amazon", peso_valor=peso, preco_valor=(custo_base / 0.6))
        frete, faixa = _sumifs(regras, "taxa_frete", canal_contains="amazon", preco_valor=(custo_base / 0.7))
    elif nome_canal == "Shein":
        comissao = 0.16
        taxa_fixa, _ = _sumifs(regras, "taxa_fixa", canal_contains="shein", peso_valor=peso)
        frete, faixa = _sumifs(regras, "taxa_frete", canal_contains="shein", peso_valor=peso)
    elif nome_canal == "Shopfy":
        comissao = _primeiro_valor(regras, "shopfy", "comissao")
        frete, faixa = _sumifs(regras, "taxa_frete", canal_contains="shopfy", peso_valor=peso, preco_valor=(custo_base / 0.7))
    else:
        return None
    return {"comissao": float(comissao or 0), "taxa_fixa": float(taxa_fixa or 0), "frete": float(frete or 0), "faixa": faixa}


def _value_is_multiplier(valor):
    return 0 < valor <= 3


def _value_as_multiplier(valor):
    return valor if _value_is_multiplier(valor) else 1 + (valor / 100.0)


def _resolver_preco_por_objetivo(custo_base, taxa_fixa, frete, comissao, imposto, objetivo, tipo_alvo, valor_alvo):
    objetivo = (objetivo or "").lower()
    tipo_alvo = (tipo_alvo or "").lower()
    valor = float(valor_alvo or 0)
    if objetivo == "markup":
        markup_divisor = ((valor if _value_is_multiplier(valor) else 1 + (valor / 100.0)) if tipo_alvo == "nominal" else _value_as_multiplier(valor))
        return ((custo_base * markup_divisor) + taxa_fixa + frete) / (1 - comissao - imposto)
    if objetivo == "margem":
        if tipo_alvo == "nominal":
            return (custo_base + taxa_fixa + frete + valor) / (1 - comissao - imposto)
        margem_alvo = valor / 100.0 if valor > 1 else valor
        return (custo_base + taxa_fixa + frete) / (1 - (comissao + margem_alvo + imposto))
    if objetivo == "lucro_liquido":
        lucro_alvo = valor if tipo_alvo == "nominal" else custo_base * (valor / 100.0 if valor > 1 else valor)
        return (custo_base + taxa_fixa + frete + lucro_alvo) / (1 - comissao - imposto)
    raise ValueError("Objetivo inválido.")


def _to_dt(valor):
    try:
        return datetime.fromisoformat(str(valor)[:10])
    except Exception:
        return None


def _filtrar_historico(historical_data, sku: Optional[str], data_inicio: Optional[str], data_fim: Optional[str]):
    if not sku:
        return []
    dt_ini = _to_dt(data_inicio) if data_inicio else None
    dt_fim = _to_dt(data_fim) if data_fim else None
    itens = []
    for item in historical_data or []:
        if str(item.get("sku", "")).strip() != str(sku).strip():
            continue
        dt = _to_dt(item.get("data"))
        if dt_ini and (not dt or dt < dt_ini):
            continue
        if dt_fim and (not dt or dt > dt_fim):
            continue
        itens.append(item)
    return itens


def _dias_periodo(data_inicio: Optional[str], data_fim: Optional[str], fallback_dias=30):
    dt_ini = _to_dt(data_inicio)
    dt_fim = _to_dt(data_fim)
    if dt_ini and dt_fim:
        return max((dt_fim - dt_ini).days + 1, 1)
    return fallback_dias


def _cap_weights(weights: Dict[str, float], max_weight: float) -> Dict[str, float]:
    if not weights:
        return {}
    capped = {k: min(v, max_weight) for k, v in weights.items()}
    total = sum(capped.values()) or 1.0
    return {k: v / total for k, v in capped.items()}


def _ajuste_por_score(score: float, ajuste_maximo_percentual: float):
    ajuste_maximo_percentual = abs(float(ajuste_maximo_percentual or 3.0))
    if score >= 0.90:
        return min(1.0, ajuste_maximo_percentual)
    if score >= 0.75:
        return 0.0
    if score >= 0.60:
        return -min(1.5, ajuste_maximo_percentual)
    return -ajuste_maximo_percentual


def _aplicar_inteligencia_vendas(resultados, historical_data=None, config=None, sku=None):
    cfg = config or {}
    if not cfg.get("ativo"):
        for r in resultados:
            r["historico_utilizado"] = False
            r["score_lucro"] = 0.0
            r["score_liquidez"] = 0.0
            r["peso_canal"] = 0.0
            r["ajuste_competitividade_percentual"] = 0.0
            r["preco_base"] = r["preco_final"]
            r["indice_final"] = round(r.get("lucro_liquido", 0), 4)
        return resultados

    data_inicio = cfg.get("data_inicio")
    data_fim = cfg.get("data_fim")
    itens = _filtrar_historico(historical_data, sku, data_inicio, data_fim)
    dias = _dias_periodo(data_inicio, data_fim)
    min_pedidos = int(cfg.get("min_pedidos", 3) or 3)
    min_unidades = int(cfg.get("min_unidades", 5) or 5)
    peso_lucro = float(cfg.get("peso_lucro", 0.6) or 0.6)
    peso_liquidez = float(cfg.get("peso_liquidez", 0.4) or 0.4)
    ignorar_prejuizo = bool(cfg.get("ignorar_canais_prejuizo", True))
    peso_maximo_canal = float(cfg.get("peso_maximo_canal", 0.70) or 0.70)
    ajuste_maximo_percentual = float(cfg.get("ajuste_maximo_percentual", 3.0) or 3.0)
    usar_share_lucro = bool(cfg.get("usar_share_lucro", True))

    agregados = {}
    for item in itens:
        canal = str(item.get("canal", "")).strip()
        if not canal:
            continue
        base = agregados.setdefault(canal, {"quantidade": 0.0, "pedidos": 0.0, "lucro_liquido": 0.0})
        base["quantidade"] += float(item.get("quantidade", 0) or 0)
        base["pedidos"] += float(item.get("pedidos", 0) or 0)
        base["lucro_liquido"] += float(item.get("lucro_liquido", 0) or 0)

    total_qtd = sum(a["quantidade"] for a in agregados.values())
    total_ped = sum(a["pedidos"] for a in agregados.values())
    historico_suficiente = bool(agregados) and (total_qtd >= min_unidades or total_ped >= min_pedidos)

    if not historico_suficiente:
        for r in resultados:
            r["historico_utilizado"] = False
            r["score_lucro"] = 0.0
            r["score_liquidez"] = 0.0
            r["peso_canal"] = 0.0
            r["ajuste_competitividade_percentual"] = 0.0
            r["preco_base"] = r["preco_final"]
            r["indice_final"] = round(r.get("lucro_liquido", 0), 4)
        return resultados

    maior_lucro = max(max(v["lucro_liquido"], 0.0) for v in agregados.values()) or 1.0
    maior_velocidade = max((v["quantidade"] / dias) for v in agregados.values()) or 1.0
    pesos = {}
    if usar_share_lucro:
        lucros_ajustados = {}
        for canal, valores in agregados.items():
            lucro = float(valores["lucro_liquido"] or 0)
            lucro = max(lucro, 0.0) if ignorar_prejuizo else lucro
            lucros_ajustados[canal] = max(lucro, 0.0)
        soma_lucros = sum(lucros_ajustados.values())
        if soma_lucros > 0:
            pesos = {canal: (valor / soma_lucros) for canal, valor in lucros_ajustados.items()}
            pesos = _cap_weights(pesos, peso_maximo_canal)

    for r in resultados:
        canal = str(r.get("canal", "")).strip()
        hist = agregados.get(canal)
        r["preco_base"] = r["preco_final"]
        if not hist:
            r["historico_utilizado"] = False
            r["score_lucro"] = 0.0
            r["score_liquidez"] = 0.0
            r["peso_canal"] = 0.0
            r["ajuste_competitividade_percentual"] = 0.0
            r["indice_final"] = round(r.get("lucro_liquido", 0), 4)
            continue

        velocidade = float(hist["quantidade"] or 0) / dias
        indice_lucro = max(float(hist["lucro_liquido"] or 0), 0.0) / maior_lucro if maior_lucro else 0.0
        indice_liquidez = velocidade / maior_velocidade if maior_velocidade else 0.0
        score_canal = (peso_lucro * indice_lucro) + (peso_liquidez * indice_liquidez)
        peso_canal = pesos.get(canal, 1.0 / max(len(agregados), 1))
        indice_final = score_canal * peso_canal
        ajuste = _ajuste_por_score(indice_final, ajuste_maximo_percentual)
        preco_ajustado = r["preco_final"] * (1 + (ajuste / 100.0))
        if preco_ajustado <= 0:
            preco_ajustado = r["preco_final"]
            ajuste = 0.0
        r["preco_final"] = round(preco_ajustado, 2)
        r["historico_utilizado"] = True
        r["score_lucro"] = round(indice_lucro, 4)
        r["score_liquidez"] = round(indice_liquidez, 4)
        r["peso_canal"] = round(peso_canal, 4)
        r["score_canal"] = round(score_canal, 4)
        r["ajuste_competitividade_percentual"] = round(ajuste, 2)
        r["indice_final"] = round(indice_final, 4)
    return resultados


def calcular_canais(regras, preco_compra, embalagem, peso, imposto, quantidade, objetivo, tipo_alvo, valor_alvo, intelligence_config=None, historical_data=None, sku=None):
    custo_base = (float(preco_compra or 0) * int(quantidade or 1)) + float(embalagem or 0)
    peso_usado = float(peso or 0)
    imposto = _pct_excel(imposto)
    canais = ["Mercado Livre Classico", "Mercado Livre Premium", "Shopee", "Amazon", "Shein", "Shopfy"]
    resultados = []
    for canal in canais:
        fatores = _fatores_canal(regras, canal, custo_base, peso_usado)
        if not fatores:
            continue
        preco_final = _resolver_preco_por_objetivo(custo_base, fatores["taxa_fixa"], fatores["frete"], fatores["comissao"], imposto, objetivo, tipo_alvo, valor_alvo)
        resultados.append({"canal": canal, **_metricas(preco_final, custo_base, fatores["taxa_fixa"], fatores["frete"], fatores["comissao"], imposto, fatores["faixa"])})
    if not resultados:
        return {"custo_total": round(custo_base, 2), "peso_total": round(peso_usado, 3), "melhor_canal": "", "pior_canal": "", "canais": [], "formula_version": FORMULA_VERSION}
    resultados = _aplicar_inteligencia_vendas(resultados, historical_data=historical_data, config=intelligence_config, sku=sku)
    resultados_ordenados = sorted(resultados, key=lambda x: (x.get("indice_final", 0), x.get("lucro_liquido", 0)), reverse=True)
    return {"custo_total": round(custo_base, 2), "peso_total": round(peso_usado, 3), "melhor_canal": resultados_ordenados[0]["canal"], "pior_canal": resultados_ordenados[-1]["canal"], "canais": resultados_ordenados, "formula_version": FORMULA_VERSION}


def _arredondar_preco(valor, modo):
    valor = float(valor or 0)
    if modo == "sem":
        return round(valor, 2)
    cents = {"90": 0.90, "99": 0.99, "97": 0.97}.get(modo)
    if cents is None:
        return round(valor, 2)
    base = int(valor)
    candidato = base + cents
    if candidato < valor:
        candidato = (base + 1) + cents
    return round(candidato, 2)


def _aplicar_regra_estoque(preco, estoque, regra_estoque):
    if not regra_estoque or not regra_estoque.get("ativo"):
        return round(preco, 2), False
    limite = int(regra_estoque.get("limite", 2) or 2)
    if estoque > limite:
        return round(preco, 2), False
    valor = float(regra_estoque.get("valor", 0) or 0)
    tipo = regra_estoque.get("tipo", "percentual")
    preco = preco * (1 + (valor / 100.0)) if tipo == "percentual" else preco + valor
    return round(preco, 2), True


def gerar_integracao(canais, modo_preco_virtual, acrescimo_percentual, acrescimo_nominal, preco_manual, arredondamento, modo_aprovacao="manual", preco_custo_bling=0, preco_compra_anterior_bling=0, estoque=0, regra_estoque=None):
    mudanca_custo_detectada = float(preco_compra_anterior_bling or 0) > 0 and round(float(preco_custo_bling or 0), 4) != round(float(preco_compra_anterior_bling or 0), 4)
    itens = []
    for canal in canais:
        preco_promocional = float(canal.get("preco_final", 0) or 0)
        if modo_preco_virtual == "percentual_acima":
            preco_virtual = preco_promocional * (1 + (float(acrescimo_percentual or 0) / 100.0))
        elif modo_preco_virtual == "valor_acima":
            preco_virtual = preco_promocional + float(acrescimo_nominal or 0)
        else:
            preco_virtual = float(preco_manual or 0)
        preco_promocional, regra_aplicada = _aplicar_regra_estoque(preco_promocional, estoque, regra_estoque)
        preco_virtual, _ = _aplicar_regra_estoque(preco_virtual, estoque, regra_estoque)
        preco_promocional = _arredondar_preco(preco_promocional, arredondamento)
        preco_virtual = _arredondar_preco(preco_virtual, arredondamento)
        itens.append({
            "canal": canal.get("canal", ""),
            "preco_promocional": round(preco_promocional, 2),
            "preco_virtual": round(preco_virtual, 2),
            "diferenca_nominal": round(preco_virtual - preco_promocional, 2),
            "diferenca_percentual": round(((preco_virtual - preco_promocional) / preco_promocional * 100) if preco_promocional else 0, 2),
            "receita_liquida": canal.get("receita_liquida", 0),
            "lucro_liquido": canal.get("lucro_liquido", 0),
            "indice_final": canal.get("indice_final", 0),
            "faixa_aplicada": canal.get("faixa_aplicada", ""),
            "historico_utilizado": canal.get("historico_utilizado", False),
            "ajuste_competitividade_percentual": canal.get("ajuste_competitividade_percentual", 0),
            "peso_canal": canal.get("peso_canal", 0),
            "aprovacao_status": "Pendente aprovação manual" if modo_aprovacao == "manual" else "Pronto para enviar",
            "estoque": estoque,
            "regra_estoque_aplicada": regra_aplicada,
            "mudanca_custo_detectada": mudanca_custo_detectada,
        })
    observacao = "Resultado enviado para fila de aprovação manual." if modo_aprovacao == "manual" else "Preços prontos para envio ao Bling."
    if regra_estoque and regra_estoque.get("ativo") and estoque <= int(regra_estoque.get("limite", 2) or 2):
        observacao += " Regra de estoque baixo aplicada."
    return {"itens": itens, "mudanca_custo_detectada": mudanca_custo_detectada, "observacao": observacao}


def extrair_peso_do_produto_bling(produto: dict) -> dict:
    candidatos = []
    for chave in ("pesoLiquido", "pesoBruto", "peso", "pesoLiq", "peso_bruto", "peso_liquido"):
        valor = produto.get(chave)
        if valor not in (None, ""):
            try:
                candidatos.append((chave, float(valor)))
            except Exception:
                pass
    if isinstance(produto.get("dimensoes"), dict):
        for chave in ("peso", "pesoBruto", "pesoLiquido"):
            valor = produto["dimensoes"].get(chave)
            if valor not in (None, ""):
                try:
                    candidatos.append((f"dimensoes.{chave}", float(valor)))
                except Exception:
                    pass
    if candidatos:
        origem, peso = candidatos[0]
        return {"peso": round(peso, 3), "origem": origem, "warning": None}
    return {"peso": 0.0, "origem": None, "warning": "Produto sem peso mapeado no retorno do Bling. Use peso override."}


def extrair_custo_do_estoque_bling(produto: dict) -> dict:
    estoque = produto.get("estoque") or {}
    candidatos = [
        ("estoque.precoCusto", estoque.get("precoCusto")),
        ("estoque.custo", estoque.get("custo")),
        ("estoque.custoMedio", estoque.get("custoMedio")),
        ("produto.precoCusto", produto.get("precoCusto")),
    ]
    for origem, valor in candidatos:
        if valor not in (None, "", 0, "0"):
            try:
                return {"custo": round(float(valor), 4), "origem": origem, "warning": None if origem != "produto.precoCusto" else "Fallback para custo do produto."}
            except Exception:
                pass
    return {"custo": 0.0, "origem": None, "warning": "Custo não encontrado no estoque nem no produto."}


def _selecionar_produto_bling(criterio: str, valor_busca: str) -> dict:
    criterio = (criterio or "").lower()
    if criterio == "ean":
        return get_product_by_ean(valor_busca)
    if criterio == "sku":
        return get_product_by_sku(valor_busca)
    if criterio == "id":
        produto = get_product_by_id(valor_busca)
        return {"encontrado": True, "criterio": "id", "valor": valor_busca, "produto": produto, "quantidade": 1}
    raise ValueError("Critério inválido. Use ean, sku ou id.")


def montar_precificacao_bling(regras, criterio, valor_busca, embalagem, imposto, quantidade, objetivo, tipo_alvo, valor_alvo, peso_override=0, intelligence_config=None, historical_data=None, modo_aprovacao="manual", preco_compra_anterior_bling=0, modo_preco_virtual="percentual_acima", acrescimo_percentual=20, acrescimo_nominal=0, preco_manual=0, arredondamento="sem", regra_estoque=None):
    busca = _selecionar_produto_bling(criterio, valor_busca)
    if not busca.get("encontrado"):
        return busca
    produto = busca.get("produto", {})
    custo_extraido = extrair_custo_do_estoque_bling(produto)
    preco_custo = float(custo_extraido["custo"] or 0)
    estoque = int(((produto.get("estoque") or {}).get("saldoVirtualTotal") or 0))
    peso_extraido = extrair_peso_do_produto_bling(produto)
    peso_usado = float(peso_override or 0) if float(peso_override or 0) > 0 else float(peso_extraido["peso"] or 0)
    if preco_custo <= 0:
        return {"erro": "Produto sem custo válido no Bling", "acao": "Preencha o custo no estoque ou ajuste o módulo de leitura do estoque.", "custo_extraido": custo_extraido}
    if peso_usado <= 0:
        return {"erro": "Produto sem peso", "acao": "Preencha o peso no Bling ou use peso override."}
    sku = produto.get("codigo") or valor_busca
    calculo = calcular_canais(regras, preco_custo, embalagem, peso_usado, imposto, quantidade, objetivo, tipo_alvo, valor_alvo, intelligence_config=intelligence_config, historical_data=historical_data, sku=sku)
    integracao = gerar_integracao(calculo["canais"], modo_preco_virtual, acrescimo_percentual, acrescimo_nominal, preco_manual, arredondamento, modo_aprovacao=modo_aprovacao, preco_custo_bling=preco_custo, preco_compra_anterior_bling=preco_compra_anterior_bling, estoque=estoque, regra_estoque=regra_estoque)
    melhor_item = integracao["itens"][0] if integracao["itens"] else None
    auditoria = {
        "formula_version": FORMULA_VERSION,
        "sku": sku,
        "custo_usado": preco_custo,
        "origem_custo": custo_extraido.get("origem"),
        "peso_usado": peso_usado,
        "origem_peso": peso_extraido.get("origem"),
        "warning_custo": custo_extraido.get("warning"),
        "warning_peso": peso_extraido.get("warning"),
    }
    return {
        "criterio": criterio,
        "valor_busca": valor_busca,
        "produto_bling": {"id": produto.get("id"), "nome": produto.get("nome"), "codigo": produto.get("codigo"), "preco": produto.get("preco"), "precoCusto": produto.get("precoCusto"), "saldoVirtualTotal": estoque},
        "busca_quantidade": busca.get("quantidade", 1),
        "custo_extraido": custo_extraido,
        "peso_extraido": peso_extraido,
        "peso_usado": peso_usado,
        "melhor_canal": calculo["melhor_canal"],
        "pior_canal": calculo["pior_canal"],
        "itens_precificacao": integracao["itens"],
        "melhor_resultado": melhor_item,
        "observacao": integracao["observacao"],
        "auditoria": auditoria,
        "proximo_passo": "Validar o payload e então integrar o envio de preço de volta ao Bling.",
    }
