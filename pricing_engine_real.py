from __future__ import annotations

from typing import Dict, List, Any

# ML API em tempo real (opcional)
try:
    from ml_pricing_engine import get_ml_taxa_real as _get_ml_taxa_real
    _ML_API_DISPONIVEL = True
except ImportError:
    _ML_API_DISPONIVEL = False
    _get_ml_taxa_real = None

# Flag global para usar API ML em tempo real
_ML_API_REAL = False
_ML_PESO_G = 0
_ML_CATEGORY_ID = ""

def configurar_ml_api(usar_api: bool, peso_g: int = 0, category_id: str = ""):
    global _ML_API_REAL, _ML_PESO_G, _ML_CATEGORY_ID
    _ML_API_REAL = usar_api and _ML_API_DISPONIVEL
    _ML_PESO_G = peso_g
    _ML_CATEGORY_ID = category_id


FORMULA_VERSION = "v3.4.0-composicao"

def _safe_float(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        s = str(v).strip().replace("R$", "").replace("%", "").replace(" ", "")
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", ".")
        return float(s)
    except Exception:
        return default

def _safe_int(v, default=0):
    try:
        return int(float(v))
    except Exception:
        return default

def _pct_excel(v):
    v = _safe_float(v, 0.0)
    return v / 100.0 if v > 1 else v

def _round2(v: float) -> float:
    return round(float(v or 0), 2)

def _normalizar_regra(r: Dict) -> Dict:
    return {
        "canal": str(r.get("canal", "")).strip(),
        "peso_min": _safe_float(r.get("peso_min"), 0),
        "peso_max": _safe_float(r.get("peso_max"), 999999),
        "preco_min": _safe_float(r.get("preco_min"), 0),
        "preco_max": _safe_float(r.get("preco_max"), 999999999),
        "taxa_frete": _safe_float(r.get("taxa_frete") if "taxa_frete" in r else r.get("frete"), 0),
        "comissao": _pct_excel(r.get("comissao", 0)),
        "taxa_fixa": _safe_float(r.get("taxa_fixa"), 0),
        "ativo": bool(r.get("ativo", True)),
        "raw": r,
    }

def _filtrar_regras(regras: List[Dict], canal: str, peso: float, preco: float) -> List[Dict]:
    saida = []
    for r in regras:
        rr = _normalizar_regra(r)
        if not rr["ativo"]:
            continue
        if rr["canal"].strip().lower() != canal.strip().lower():
            continue
        if not (rr["peso_min"] <= peso <= rr["peso_max"]):
            continue
        if not (rr["preco_min"] <= preco <= rr["preco_max"]):
            continue
        saida.append(rr)
    return saida

def _achar_regra(regras: List[Dict], canal: str, peso: float, preco: float) -> Dict:
    candidatas = _filtrar_regras(regras, canal, peso, preco)
    if candidatas:
        candidatas.sort(key=lambda x: (x["preco_min"], x["peso_min"]))
        return candidatas[0]
    fallback = []
    for r in regras:
        rr = _normalizar_regra(r)
        if not rr["ativo"]:
            continue
        if rr["canal"].strip().lower() != canal.strip().lower():
            continue
        if rr["peso_min"] <= peso <= rr["peso_max"]:
            fallback.append(rr)
    if fallback:
        fallback.sort(key=lambda x: (abs(preco - x["preco_min"]), x["preco_min"]))
        return fallback[0]
    raise ValueError(f"Nenhuma regra encontrada para canal={canal}, peso={peso}, preco={preco:.2f}")

def _faixa_texto(regra: Dict):
    return f"Peso {regra.get('peso_min', 0):g}-{regra.get('peso_max', 0):g} kg | Preço {regra.get('preco_min', 0):g}-{regra.get('preco_max', 0):g}"

def _resolver_preco_por_objetivo(custo_base, frete, taxa_fixa, comissao, imposto, objetivo, tipo_alvo, valor_alvo):
    objetivo = (objetivo or "markup").lower()
    tipo_alvo = (tipo_alvo or "percentual").lower()
    valor = _safe_float(valor_alvo, 0)
    if objetivo == "markup":
        multiplicador = valor if 0 < valor <= 3 else 1 + (valor / 100.0)
        return ((custo_base * multiplicador) + frete + taxa_fixa) / max(1 - comissao - imposto, 0.0001)
    if objetivo == "margem":
        margem_alvo = valor / 100.0 if valor > 1 else valor
        return (custo_base + frete + taxa_fixa) / max(1 - (comissao + imposto + margem_alvo), 0.0001)
    if objetivo == "lucro_liquido":
        lucro_alvo = valor if tipo_alvo == "nominal" else custo_base * (valor / 100.0 if valor > 1 else valor)
        return (custo_base + frete + taxa_fixa + lucro_alvo) / max(1 - comissao - imposto, 0.0001)
    raise ValueError("Objetivo inválido.")

def _calcular_um_canal(regras: List[Dict], canal: str, custo_base: float, peso: float, imposto: float, objetivo: str, tipo_alvo: str, valor_alvo: float):
    preco = max(custo_base * 1.5, 1.0)
    regra = None
    for _ in range(25):
        regra = _achar_regra(regras, canal, peso, preco)
        preco_novo = _resolver_preco_por_objetivo(custo_base, regra["taxa_frete"], regra["taxa_fixa"], regra["comissao"], imposto, objetivo, tipo_alvo, valor_alvo)
        if abs(preco_novo - preco) < 0.01:
            preco = preco_novo
            break
        preco = preco_novo
    if regra is None:
        raise ValueError(f"Sem regra para {canal}")
    preco_final = _round2(preco)
    frete = _round2(regra["taxa_frete"])
    taxa_fixa = _round2(regra["taxa_fixa"])
    comissao_pct = regra["comissao"]
    frete_op_api = 0.0
    # Usa taxa ML em tempo real se configurado
    if _ML_API_REAL and _get_ml_taxa_real and canal in ("Mercado Livre Classico", "Mercado Livre Premium"):
        _listing = "gold_special" if "Classico" in canal else "gold_pro"
        try:
            _taxa = _get_ml_taxa_real(_listing, preco_final, _ML_PESO_G or 300, _ML_CATEGORY_ID)
            comissao_pct = _taxa.get("comissao_pct", comissao_pct)
            frete_op_api = _taxa.get("frete_operacional", 0.0)
        except Exception:
            pass
    imposto_pct = imposto
    comissao_valor = _round2(preco_final * comissao_pct)
    imposto_valor = _round2(preco_final * imposto_pct)
    receita_liquida = _round2(preco_final - frete - taxa_fixa - comissao_valor - frete_op_api)
    lucro_bruto = _round2(receita_liquida - custo_base)
    lucro_liquido = _round2(lucro_bruto - imposto_valor)
    margem_liquida_percentual = _round2((lucro_liquido / preco_final) * 100 if preco_final else 0)
    return {
        "canal": canal,
        "preco_final": preco_final,
        "receita_liquida": receita_liquida,
        "lucro_bruto": lucro_bruto,
        "lucro": lucro_bruto,
        "lucro_liquido": lucro_liquido,
        "margem": margem_liquida_percentual,
        "margem_liquida_percentual": margem_liquida_percentual,
        "frete": frete,
        "taxa_fixa": taxa_fixa,
        "comissao": _round2(comissao_pct * 100),
        "comissao_valor": comissao_valor,
        "imposto": imposto_valor,
        "custo_total": _round2(custo_base),
        "faixa_aplicada": _faixa_texto(regra),
        "indice_final": round(lucro_liquido, 4),
    }

def calcular_canais(regras, preco_compra, embalagem, peso, imposto, quantidade, objetivo, tipo_alvo, valor_alvo, intelligence_config=None, historical_data=None, sku=None, score_config=None):
    custo_base = (_safe_float(preco_compra, 0) * _safe_int(quantidade, 1)) + _safe_float(embalagem, 0)
    peso_usado = _safe_float(peso, 0)
    imposto = _pct_excel(imposto)
    canais = []
    nomes = []
    for r in regras or []:
        canal = str(r.get("canal", "")).strip()
        if canal and canal not in nomes and bool(r.get("ativo", True)):
            nomes.append(canal)
            canais.append(canal)
    resultados = []
    for canal in canais:
        try:
            resultados.append(_calcular_um_canal(regras, canal, custo_base, peso_usado, imposto, objetivo, tipo_alvo, valor_alvo))
        except Exception:
            continue
    resultados_ordenados = sorted(resultados, key=lambda x: (x.get("indice_final", 0), x.get("lucro_liquido", 0)), reverse=True)
    return {
        "custo_total": _round2(custo_base),
        "peso_total": round(peso_usado, 3),
        "melhor_canal": resultados_ordenados[0]["canal"] if resultados_ordenados else "",
        "pior_canal": resultados_ordenados[-1]["canal"] if resultados_ordenados else "",
        "canais": resultados_ordenados,
        "formula_version": FORMULA_VERSION,
    }

def _arredondar_preco(valor, modo):
    valor = _safe_float(valor, 0)
    if modo == "sem":
        return _round2(valor)
    cents = {"90": 0.90, "99": 0.99, "97": 0.97}.get(modo)
    if cents is None:
        return _round2(valor)
    base = int(valor)
    candidato = base + cents
    if candidato < valor:
        candidato = (base + 1) + cents
    return _round2(candidato)

def _aplicar_regra_estoque(preco, estoque, regra_estoque):
    if not regra_estoque or not regra_estoque.get("ativo"):
        return _round2(preco), False
    limite = _safe_int(regra_estoque.get("limite"), 2)
    if estoque > limite:
        return _round2(preco), False
    valor = _safe_float(regra_estoque.get("valor"), 0)
    tipo = regra_estoque.get("tipo", "percentual")
    preco = preco * (1 + (valor / 100.0)) if tipo == "percentual" else preco + valor
    return _round2(preco), True

def gerar_integracao(canais, modo_preco_virtual, acrescimo_percentual, acrescimo_nominal, preco_manual, arredondamento, modo_aprovacao="manual", preco_custo_bling=0, preco_compra_anterior_bling=0, estoque=0, regra_estoque=None):
    mudanca_custo_detectada = _safe_float(preco_compra_anterior_bling, 0) > 0 and round(_safe_float(preco_custo_bling, 0), 4) != round(_safe_float(preco_compra_anterior_bling, 0), 4)
    itens = []
    for canal in canais:
        preco_promocional = _safe_float(canal.get("preco_final"), 0)
        if modo_preco_virtual == "percentual_acima":
            preco_virtual = preco_promocional * (1 + (_safe_float(acrescimo_percentual, 0) / 100.0))
        elif modo_preco_virtual == "valor_acima":
            preco_virtual = preco_promocional + _safe_float(acrescimo_nominal, 0)
        else:
            preco_virtual = _safe_float(preco_manual, 0)
        preco_promocional, regra_aplicada = _aplicar_regra_estoque(preco_promocional, estoque, regra_estoque)
        preco_virtual, _ = _aplicar_regra_estoque(preco_virtual, estoque, regra_estoque)
        preco_promocional = _arredondar_preco(preco_promocional, arredondamento)
        preco_virtual = _arredondar_preco(preco_virtual, arredondamento)
        itens.append({
            "canal": canal.get("canal", ""),
            "preco_promocional": _round2(preco_promocional),
            "preco_virtual": _round2(preco_virtual),
            "diferenca_nominal": _round2(preco_virtual - preco_promocional),
            "diferenca_percentual": _round2(((preco_virtual - preco_promocional) / preco_promocional * 100) if preco_promocional else 0),
            "receita_liquida": canal.get("receita_liquida", 0),
            "lucro_liquido": canal.get("lucro_liquido", 0),
            "lucro": canal.get("lucro", canal.get("lucro_bruto", 0)),
            "margem": canal.get("margem", canal.get("margem_liquida_percentual", 0)),
            "frete": canal.get("frete", 0),
            "taxa_fixa": canal.get("taxa_fixa", 0),
            "comissao": canal.get("comissao", 0),
            "imposto": canal.get("imposto", 0),
            "custo_total": canal.get("custo_total", 0),
            "indice_final": canal.get("indice_final", 0),
            "faixa_aplicada": canal.get("faixa_aplicada", ""),
            "aprovacao_status": "Pendente aprovação manual" if modo_aprovacao == "manual" else "Pronto para enviar",
            "estoque": estoque,
            "regra_estoque_aplicada": regra_aplicada,
            "mudanca_custo_detectada": mudanca_custo_detectada,
        })
    observacao = "Resultado enviado para fila de aprovação manual." if modo_aprovacao == "manual" else "Preços prontos para envio ao Bling."
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
    if candidatos:
        origem, peso = candidatos[0]
        return {"peso": round(peso, 3), "origem": origem, "warning": None}
    return {"peso": 0.0, "origem": None, "warning": "Produto sem peso mapeado no retorno do Bling. Use peso override."}

def _candidate_cost_keys():
    return [
        "precoCompra", "preco_compra", "preco compra",
        "precoCusto", "preco_custo", "preco custo",
        "custo", "custoMedio", "custo_medio", "custo médio",
        "valorCompra", "valor_compra", "valor compra",
        "precoUltimaCompra", "preco_ultima_compra", "última compra", "ultima compra",
    ]

def _buscar_custo_em_objeto(obj: Any, prefixo: str = ""):
    candidatos = []

    if isinstance(obj, dict):
        for chave, valor in obj.items():
            caminho = f"{prefixo}.{chave}" if prefixo else str(chave)
            chave_norm = str(chave).strip().lower().replace("_", "").replace(" ", "")
            for ref in _candidate_cost_keys():
                ref_norm = ref.strip().lower().replace("_", "").replace(" ", "")
                if chave_norm == ref_norm and valor not in (None, "", 0, "0"):
                    try:
                        candidatos.append((caminho, float(valor)))
                    except Exception:
                        pass
            candidatos.extend(_buscar_custo_em_objeto(valor, caminho))

    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            caminho = f"{prefixo}[{i}]"
            candidatos.extend(_buscar_custo_em_objeto(item, caminho))

    return candidatos

def extrair_custo_do_estoque_bling(produto: dict) -> dict:
    estoque = produto.get("estoque") or {}
    candidatos_diretos = [
        ("estoque.precoCompra", estoque.get("precoCompra")),
        ("estoque.preco_compra", estoque.get("preco_compra")),
        ("estoque.valorCompra", estoque.get("valorCompra")),
        ("estoque.precoUltimaCompra", estoque.get("precoUltimaCompra")),
        ("estoque.precoCusto", estoque.get("precoCusto")),
        ("estoque.custo", estoque.get("custo")),
        ("estoque.custoMedio", estoque.get("custoMedio")),
        ("produto.precoCompra", produto.get("precoCompra")),
        ("produto.preco_compra", produto.get("preco_compra")),
        ("produto.precoCusto", produto.get("precoCusto")),
        ("fornecedor.precoCusto", (produto.get("fornecedor") or {}).get("precoCusto")),
        ("fornecedor.precoCompra", (produto.get("fornecedor") or {}).get("precoCompra")),
    ]

    for origem, valor in candidatos_diretos:
        if valor not in (None, "", 0, "0"):
            try:
                return {"custo": round(float(valor), 4), "origem": origem, "warning": None}
            except Exception:
                pass

    candidatos_recursivos = _buscar_custo_em_objeto(estoque, "estoque")
    if candidatos_recursivos:
        origem, valor = candidatos_recursivos[0]
        return {"custo": round(float(valor), 4), "origem": origem, "warning": "Custo encontrado por varredura no objeto de estoque."}

    candidatos_produto = _buscar_custo_em_objeto(produto, "produto")
    for origem, valor in candidatos_produto:
        if not origem.startswith("produto.estoque"):
            return {"custo": round(float(valor), 4), "origem": origem, "warning": "Fallback por varredura no produto."}

    # Override local de custo (salvo pelo simulador para produtos sem fornecedor)
    try:
        from pathlib import Path as _Path
        import json as _json
        _override_path = _Path(__file__).parent / "data" / "custo_override.json"
        if _override_path.exists():
            _overrides = _json.loads(_override_path.read_text(encoding="utf-8"))
            _sku = produto.get("codigo") or ""
            _pid = str(produto.get("id") or "")
            _override = _overrides.get(_sku) or _overrides.get(_pid)
            if _override and float(_override.get("custo", 0)) > 0:
                return {"custo": round(float(_override["custo"]), 4), "origem": "override_local", "warning": "Custo salvo localmente pelo simulador."}
    except Exception:
        pass
    return {"custo": 0.0, "origem": None, "warning": "Preço de compra não encontrado no estoque nem no produto."}

def _buscar_componentes_em_objeto(obj: Any, prefixo: str = "") -> list[dict]:
    encontrados: list[dict] = []

    if isinstance(obj, dict):
        for chave, valor in obj.items():
            caminho = f"{prefixo}.{chave}" if prefixo else str(chave)
            chave_norm = str(chave).strip().lower()

            if chave_norm in {
                "estrutura", "estruturas", "componentes", "composicao", "composição",
                "itens", "produtoitens", "produto_itens", "componentesproduto",
            } and isinstance(valor, list):
                for item in valor:
                    if isinstance(item, dict):
                        encontrados.append({"origem": caminho, "item": item})

            encontrados.extend(_buscar_componentes_em_objeto(valor, caminho))

    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            caminho = f"{prefixo}[{i}]"
            encontrados.extend(_buscar_componentes_em_objeto(item, caminho))

    return encontrados

def _extract_component_identity(item: dict) -> dict:
    produto = item.get("produto") if isinstance(item.get("produto"), dict) else {}

    sku = (
        item.get("codigo")
        or item.get("sku")
        or produto.get("codigo")
        or produto.get("sku")
    )

    product_id = (
        item.get("idProduto")
        or item.get("produto_id")
        or item.get("id")
        or produto.get("id")
    )

    quantidade = (
        item.get("quantidade")
        or item.get("qtd")
        or item.get("qtde")
        or item.get("quant")
        or 1
    )

    return {
        "sku": str(sku).strip() if sku not in (None, "") else None,
        "id": _safe_int(product_id, 0) if product_id not in (None, "") else None,
        "quantidade": max(_safe_float(quantidade, 1), 1),
        "raw": item,
    }

def extrair_componentes_produto(produto: dict) -> list[dict]:
    candidatos = _buscar_componentes_em_objeto(produto)
    componentes = []
    vistos = set()

    for c in candidatos:
        base = _extract_component_identity(c["item"])
        chave = (base.get("sku"), base.get("id"), base.get("quantidade"))
        if (base.get("sku") or base.get("id")) and chave not in vistos:
            vistos.add(chave)
            base["origem"] = c["origem"]
            componentes.append(base)

    return componentes

def resolver_custo_produto_ou_composicao(client, produto: dict) -> dict:
    componentes = extrair_componentes_produto(produto)

    if not componentes:
        custo = extrair_custo_do_estoque_bling(produto)
        return {
            "tipo_custo": "simples",
            "custo_total": _round2(custo.get("custo", 0)),
            "origem": custo.get("origem"),
            "warning": custo.get("warning"),
            "componentes": [],
        }

    custo_total = 0.0
    detalhes = []

    for comp in componentes:
        produto_comp = None
        origem_busca = None

        if comp.get("sku"):
            try:
                resp = client.get_product_by_sku(comp["sku"])
                if resp.get("encontrado"):
                    produto_comp = resp.get("produto")
                    origem_busca = "sku"
            except Exception:
                pass

        if produto_comp is None and comp.get("id"):
            try:
                produto_comp = client.get_product(int(comp["id"]))
                if produto_comp:
                    origem_busca = "id"
            except Exception:
                pass

        if produto_comp is None:
            detalhes.append({
                "sku": comp.get("sku"),
                "id": comp.get("id"),
                "quantidade": comp.get("quantidade"),
                "custo_unitario": 0.0,
                "subtotal": 0.0,
                "encontrado": False,
                "warning": "Componente não encontrado no Bling.",
            })
            continue

        custo_comp = extrair_custo_do_estoque_bling(produto_comp)
        custo_unit = _safe_float(custo_comp.get("custo"), 0)
        subtotal = custo_unit * _safe_float(comp.get("quantidade"), 1)
        custo_total += subtotal

        detalhes.append({
            "sku": (produto_comp.get("codigo") or comp.get("sku")),
            "id": produto_comp.get("id") or comp.get("id"),
            "nome": produto_comp.get("nome") or produto_comp.get("descricao") or "",
            "quantidade": _safe_float(comp.get("quantidade"), 1),
            "custo_unitario": _round2(custo_unit),
            "subtotal": _round2(subtotal),
            "origem_busca": origem_busca,
            "origem_custo": custo_comp.get("origem"),
            "warning": custo_comp.get("warning"),
            "encontrado": True,
        })

    return {
        "tipo_custo": "composicao",
        "custo_total": _round2(custo_total),
        "origem": "componentes_do_anuncio",
        "warning": None if custo_total > 0 else "Nenhum custo válido encontrado nos componentes.",
        "componentes": detalhes,
    }

def _selecionar_produto_bling_por_sku(client, sku: str) -> dict:
    resp = client.get_product_by_sku(sku)
    if resp.get("encontrado"):
        resp["criterio_usado"] = "sku"
        return resp
    return {
        "encontrado": False,
        "criterio_usado": "sku",
        "erro": "Produto não encontrado por SKU",
        "acao": "Verifique o SKU cadastrado no Bling.",
        "sku_informado": sku,
    }

def montar_precificacao_bling(regras, criterio, valor_busca, embalagem, imposto, quantidade, objetivo, tipo_alvo, valor_alvo, peso_override=0, intelligence_config=None, historical_data=None, modo_aprovacao="manual", preco_compra_anterior_bling=0, modo_preco_virtual="percentual_acima", acrescimo_percentual=20, acrescimo_nominal=0, preco_manual=0, arredondamento="sem", regra_estoque=None):
    from bling_client import BlingClient
    criterio = (criterio or "sku").strip().lower()
    if criterio != "sku":
        return {
            "erro": "Apenas SKU é aceito para busca",
            "acao": "Use criterio='sku' e informe o SKU único do produto.",
        }

    client = BlingClient()
    busca = _selecionar_produto_bling_por_sku(client, valor_busca)
    if not busca.get("encontrado"):
        return busca

    produto = busca.get("produto", {})
    custo_resolvido = resolver_custo_produto_ou_composicao(client, produto)
    preco_custo = float(custo_resolvido["custo_total"] or 0)


    estoque = int(((produto.get("estoque") or {}).get("saldoVirtualTotal") or 0))
    peso_extraido = extrair_peso_do_produto_bling(produto)
    peso_usado = float(peso_override or 0) if float(peso_override or 0) > 0 else float(peso_extraido["peso"] or 0)

    if preco_custo <= 0:
        return {
            "erro": "Produto sem custo válido no Bling",
            "erro_codigo": "composicao_sem_custo" if str(custo_resolvido.get("origem") or "").lower() == "componentes_do_anuncio" else "custo_ausente",
            "acao": "Preencha o preço de compra no estoque ou revise a composição do anúncio.",
            "custo_extraido": custo_resolvido,
        }
    if peso_usado <= 0:
        return {
            "erro": "Produto sem peso",
            "erro_codigo": "peso_ausente",
            "acao": "Preencha o peso no Bling ou use peso override.",
        }

    sku = produto.get("codigo") or valor_busca
    calculo = calcular_canais(regras, preco_custo, embalagem, peso_usado, imposto, quantidade, objetivo, tipo_alvo, valor_alvo, intelligence_config=intelligence_config, historical_data=historical_data, sku=sku)
    integracao = gerar_integracao(calculo["canais"], modo_preco_virtual, acrescimo_percentual, acrescimo_nominal, preco_manual, arredondamento, modo_aprovacao=modo_aprovacao, preco_custo_bling=preco_custo, preco_compra_anterior_bling=preco_compra_anterior_bling, estoque=estoque, regra_estoque=regra_estoque)
    melhor_item = integracao["itens"][0] if integracao["itens"] else None
    auditoria = {
        "formula_version": FORMULA_VERSION,
        "sku": sku,
        "tipo_custo": custo_resolvido.get("tipo_custo"),
        "custo_usado": preco_custo,
        "origem_custo": custo_resolvido.get("origem"),
        "componentes_custo": custo_resolvido.get("componentes", []),
        "warning_custo": custo_resolvido.get("warning"),
        "peso_usado": peso_usado,
        "origem_peso": peso_extraido.get("origem"),
        "warning_peso": peso_extraido.get("warning"),
        "criterio_usado": "sku",
    }
    return {
        "criterio": "sku",
        "criterio_usado": "sku",
        "valor_busca": valor_busca,
        "produto_bling": {"id": produto.get("id"), "nome": produto.get("nome"), "codigo": produto.get("codigo"), "preco": produto.get("preco"), "precoCusto": produto.get("precoCusto"), "saldoVirtualTotal": estoque},
        "busca_quantidade": busca.get("quantidade", 1),
        "custo_extraido": custo_resolvido,
        "peso_extraido": peso_extraido,
        "peso_usado": peso_usado,
        "melhor_canal": calculo["melhor_canal"],
        "pior_canal": calculo["pior_canal"],
        "canais": calculo["canais"],
        "itens_precificacao": integracao["itens"],
        "integracao": integracao,
        "itens": integracao["itens"],
        "melhor_resultado": melhor_item,
        "observacao": integracao["observacao"],
        "auditoria": auditoria,
    }
