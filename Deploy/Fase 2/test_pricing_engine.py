"""
test_pricing_engine.py — Shinsei Pricing
Testes unitários do pricing_engine_real.py

Execução:
    pytest test_pricing_engine.py -v
    pytest test_pricing_engine.py -v --tb=short   # traceback resumido
    pytest test_pricing_engine.py -k "margem"     # filtra por nome

Cobertura:
    pytest test_pricing_engine.py --cov=pricing_engine_real --cov-report=term-missing
    pip install pytest-cov  # se necessário
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Garante que o diretório do projeto está no path
sys.path.insert(0, str(Path(__file__).parent))

# ── Importação do motor ───────────────────────────────────────
from pricing_engine_real import (
    _safe_float,
    _pct_excel,
    _round2,
    _normalizar_regra,
    _filtrar_regras,
    _achar_regra,
    _arredondar_preco,
    _aplicar_regra_estoque,
    _resolver_preco_por_objetivo,
    _calcular_um_canal,
    calcular_canais,
    gerar_integracao,
)


# ══════════════════════════════════════════════════════════════
# Fixtures — dados reutilizáveis
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def regras_ml():
    """Regras realistas do Mercado Livre para peso leve (< 300g)."""
    return [
        {
            "canal": "Mercado Livre Classico",
            "peso_min": 0.001,
            "peso_max": 0.299,
            "preco_min": 0.01,
            "preco_max": 18.99,
            "taxa_frete": 5.65,
            "comissao": 0.12,
            "taxa_fixa": 0.0,
            "ativo": True,
        },
        {
            "canal": "Mercado Livre Classico",
            "peso_min": 0.001,
            "peso_max": 0.299,
            "preco_min": 19.0,
            "preco_max": 48.99,
            "taxa_frete": 5.65,
            "comissao": 0.12,
            "taxa_fixa": 0.0,
            "ativo": True,
        },
    ]


@pytest.fixture
def regras_multi():
    """Regras para múltiplos canais — simula cenário real do projeto."""
    return [
        {
            "canal": "Mercado Livre Classico",
            "peso_min": 0.0,
            "peso_max": 999999,
            "preco_min": 0.01,
            "preco_max": 999999,
            "taxa_frete": 6.85,
            "comissao": 0.12,
            "taxa_fixa": 0.0,
            "ativo": True,
        },
        {
            "canal": "Shopee",
            "peso_min": 0.0,
            "peso_max": 999999,
            "preco_min": 0.01,
            "preco_max": 999999,
            "taxa_frete": 4.0,
            "comissao": 0.215,
            "taxa_fixa": 0.0,
            "ativo": True,
        },
        {
            "canal": "Amazon",
            "peso_min": 0.0,
            "peso_max": 999999,
            "preco_min": 0.01,
            "preco_max": 999999,
            "taxa_frete": 0.0,
            "comissao": 0.13,
            "taxa_fixa": 4.5,
            "ativo": True,
        },
        {
            "canal": "Inativo",
            "peso_min": 0.0,
            "peso_max": 999999,
            "preco_min": 0.01,
            "preco_max": 999999,
            "taxa_frete": 3.0,
            "comissao": 0.10,
            "taxa_fixa": 0.0,
            "ativo": False,  # deve ser ignorado
        },
    ]


# ══════════════════════════════════════════════════════════════
# 1. Helpers — _safe_float, _pct_excel, _round2
# ══════════════════════════════════════════════════════════════

class TestHelpers:
    def test_safe_float_numero_normal(self):
        assert _safe_float(10.5) == 10.5

    def test_safe_float_string_virgula(self):
        assert _safe_float("10,50") == 10.5

    def test_safe_float_string_com_rs(self):
        assert _safe_float("R$ 25,90") == 25.9

    def test_safe_float_string_milhar_virgula(self):
        # "1.000,50" → 1000.50
        assert _safe_float("1.000,50") == 1000.5

    def test_safe_float_none_retorna_default(self):
        assert _safe_float(None, default=99.0) == 99.0

    def test_safe_float_string_vazia_retorna_default(self):
        assert _safe_float("", default=0.0) == 0.0

    def test_safe_float_valor_invalido_retorna_default(self):
        assert _safe_float("abc", default=-1.0) == -1.0

    def test_pct_excel_percentual_alto(self):
        # 12 → 0.12
        assert _pct_excel(12) == pytest.approx(0.12)

    def test_pct_excel_ja_fracionario(self):
        # 0.12 → 0.12 (já é fração)
        assert _pct_excel(0.12) == pytest.approx(0.12)

    def test_pct_excel_limite_1(self):
        # Exatamente 1 → considera percentual → 0.01
        assert _pct_excel(1) == pytest.approx(0.01)

    def test_round2_arredonda_corretamente(self):
        assert _round2(10.005) == 10.01 or _round2(10.005) == 10.0  # banker's rounding
        assert _round2(10.999) == 11.0
        assert _round2(0.0) == 0.0


# ══════════════════════════════════════════════════════════════
# 2. Normalização e filtro de regras
# ══════════════════════════════════════════════════════════════

class TestRegras:
    def test_normalizar_regra_campos_basicos(self):
        r = _normalizar_regra({
            "canal": "Shopee",
            "peso_min": 0,
            "peso_max": 999999,
            "preco_min": 0.01,
            "preco_max": 100,
            "taxa_frete": 4.0,
            "comissao": 21.5,
            "taxa_fixa": 0,
            "ativo": True,
        })
        assert r["canal"] == "Shopee"
        assert r["comissao"] == pytest.approx(0.215)  # percentual → fração
        assert r["ativo"] is True

    def test_normalizar_regra_alias_frete(self):
        # campo "frete" como alias de "taxa_frete"
        r = _normalizar_regra({"canal": "X", "frete": 5.0, "comissao": 10, "taxa_fixa": 0})
        assert r["taxa_frete"] == 5.0

    def test_filtrar_regras_encontra_faixa(self, regras_ml):
        resultado = _filtrar_regras(regras_ml, "Mercado Livre Classico", peso=0.2, preco=25.0)
        assert len(resultado) == 1
        assert resultado[0]["preco_min"] == 19.0

    def test_filtrar_regras_sem_resultado(self, regras_ml):
        # Peso fora de todas as faixas
        resultado = _filtrar_regras(regras_ml, "Mercado Livre Classico", peso=5.0, preco=25.0)
        assert resultado == []

    def test_filtrar_regras_ignora_canal_diferente(self, regras_ml):
        resultado = _filtrar_regras(regras_ml, "Shopee", peso=0.1, preco=15.0)
        assert resultado == []

    def test_achar_regra_encontra_primeira_faixa(self, regras_ml):
        regra = _achar_regra(regras_ml, "Mercado Livre Classico", peso=0.2, preco=12.0)
        assert regra["preco_max"] == 18.99

    def test_achar_regra_sem_match_lanca_erro(self):
        with pytest.raises(ValueError, match="Nenhuma regra"):
            _achar_regra([], "Canal X", peso=1.0, preco=10.0)


# ══════════════════════════════════════════════════════════════
# 3. Arredondamento de preço
# ══════════════════════════════════════════════════════════════

class TestArredondamento:
    @pytest.mark.parametrize("valor,modo,esperado", [
        (25.3,  "90", 25.90),
        (25.91, "90", 26.90),
        (25.3,  "99", 25.99),
        (25.99, "99", 25.99),
        (26.0,  "99", 26.99),
        (25.3,  "97", 25.97),
        (25.3,  "sem", 25.30),
        (25.999,"sem", 26.0),
    ])
    def test_arredondar_preco(self, valor, modo, esperado):
        assert _arredondar_preco(valor, modo) == pytest.approx(esperado, abs=0.01)


# ══════════════════════════════════════════════════════════════
# 4. Regra de estoque
# ══════════════════════════════════════════════════════════════

class TestRegraEstoque:
    def test_sem_regra_retorna_preco_original(self):
        preco, aplicada = _aplicar_regra_estoque(30.0, estoque=5, regra_estoque=None)
        assert preco == 30.0
        assert aplicada is False

    def test_regra_inativa_nao_aplica(self):
        regra = {"ativo": False, "limite": 10, "tipo": "percentual", "valor": 20}
        preco, aplicada = _aplicar_regra_estoque(30.0, estoque=5, regra_estoque=regra)
        assert preco == 30.0
        assert aplicada is False

    def test_estoque_acima_do_limite_nao_aplica(self):
        regra = {"ativo": True, "limite": 3, "tipo": "percentual", "valor": 20}
        preco, aplicada = _aplicar_regra_estoque(30.0, estoque=5, regra_estoque=regra)
        assert preco == 30.0
        assert aplicada is False

    def test_estoque_abaixo_aplica_percentual(self):
        regra = {"ativo": True, "limite": 5, "tipo": "percentual", "valor": 20}
        preco, aplicada = _aplicar_regra_estoque(30.0, estoque=2, regra_estoque=regra)
        assert preco == pytest.approx(36.0)
        assert aplicada is True

    def test_estoque_abaixo_aplica_nominal(self):
        regra = {"ativo": True, "limite": 5, "tipo": "nominal", "valor": 5.0}
        preco, aplicada = _aplicar_regra_estoque(30.0, estoque=2, regra_estoque=regra)
        assert preco == pytest.approx(35.0)
        assert aplicada is True


# ══════════════════════════════════════════════════════════════
# 5. Resolver preço por objetivo
# ══════════════════════════════════════════════════════════════

class TestResolverPreco:
    """Testa a matemática central do motor para cada objetivo."""

    def test_objetivo_markup_percentual(self):
        # Markup 200% = multiplicador 3x
        # (custo * 3 + frete + fixa) / (1 - comissao - imposto)
        custo, frete, fixa, comissao, imposto = 10.0, 5.0, 0.0, 0.12, 0.04
        preco = _resolver_preco_por_objetivo(custo, frete, fixa, comissao, imposto,
                                              "markup", "percentual", 200)
        receita_liquida = preco * (1 - comissao - imposto) - frete - fixa
        assert receita_liquida == pytest.approx(custo * 3, abs=0.05)

    def test_objetivo_margem(self):
        # Margem 25% significa lucro_liquido / preco = 0.25
        custo, frete, fixa, comissao, imposto = 10.0, 5.0, 0.0, 0.12, 0.04
        preco = _resolver_preco_por_objetivo(custo, frete, fixa, comissao, imposto,
                                              "margem", "percentual", 25)
        lucro = preco - preco * comissao - preco * imposto - frete - fixa - custo
        margem_calc = lucro / preco * 100
        assert margem_calc == pytest.approx(25.0, abs=0.5)

    def test_objetivo_lucro_liquido_percentual(self):
        # Lucro 30% do custo → lucro_alvo = 10 * 0.30 = 3.0
        custo, frete, fixa, comissao, imposto = 10.0, 5.0, 0.0, 0.12, 0.04
        preco = _resolver_preco_por_objetivo(custo, frete, fixa, comissao, imposto,
                                              "lucro_liquido", "percentual", 30)
        lucro = preco - preco * comissao - preco * imposto - frete - fixa - custo
        assert lucro == pytest.approx(3.0, abs=0.1)

    def test_objetivo_lucro_liquido_nominal(self):
        # Lucro fixo de R$ 5,00
        custo, frete, fixa, comissao, imposto = 10.0, 5.0, 0.0, 0.12, 0.04
        preco = _resolver_preco_por_objetivo(custo, frete, fixa, comissao, imposto,
                                              "lucro_liquido", "nominal", 5.0)
        lucro = preco - preco * comissao - preco * imposto - frete - fixa - custo
        assert lucro == pytest.approx(5.0, abs=0.1)

    def test_objetivo_invalido_lanca_erro(self):
        with pytest.raises(ValueError):
            _resolver_preco_por_objetivo(10, 0, 0, 0.1, 0.04,
                                          "objetivo_inexistente", "percentual", 30)


# ══════════════════════════════════════════════════════════════
# 6. Calcular um canal
# ══════════════════════════════════════════════════════════════

class TestCalcularUmCanal:
    def test_retorna_campos_obrigatorios(self, regras_multi):
        resultado = _calcular_um_canal(
            regras_multi, "Mercado Livre Classico",
            custo_base=10.0, peso=0.5, imposto=0.04,
            objetivo="lucro_liquido", tipo_alvo="percentual", valor_alvo=30
        )
        campos = ["canal", "preco_final", "lucro_liquido", "margem",
                  "frete", "comissao", "imposto", "custo_total", "faixa_aplicada"]
        for campo in campos:
            assert campo in resultado, f"Campo ausente: {campo}"

    def test_lucro_positivo_com_margem_viavel(self, regras_multi):
        resultado = _calcular_um_canal(
            regras_multi, "Shopee",
            custo_base=8.56, peso=1.0, imposto=0.04,
            objetivo="lucro_liquido", tipo_alvo="percentual", valor_alvo=30
        )
        assert resultado["lucro_liquido"] > 0
        assert resultado["preco_final"] > resultado["custo_total"]

    def test_canal_sem_regra_lanca_erro(self):
        with pytest.raises((ValueError, Exception)):
            _calcular_um_canal(
                [], "Canal Inexistente",
                custo_base=10.0, peso=1.0, imposto=0.04,
                objetivo="margem", tipo_alvo="percentual", valor_alvo=20
            )


# ══════════════════════════════════════════════════════════════
# 7. Calcular todos os canais (calcular_canais)
# ══════════════════════════════════════════════════════════════

class TestCalcularCanais:
    def test_calcula_multiplos_canais(self, regras_multi):
        resultado = calcular_canais(
            regras_multi,
            preco_compra=10.0, embalagem=0, peso=1.0,
            imposto=4, quantidade=1,
            objetivo="lucro_liquido", tipo_alvo="percentual", valor_alvo=30
        )
        # Deve calcular os 3 canais ativos (o "Inativo" deve ser ignorado)
        assert len(resultado["canais"]) == 3
        nomes = [c["canal"] for c in resultado["canais"]]
        assert "Inativo" not in nomes

    def test_melhor_canal_e_retornado(self, regras_multi):
        resultado = calcular_canais(
            regras_multi,
            preco_compra=10.0, embalagem=0, peso=1.0,
            imposto=4, quantidade=1,
            objetivo="lucro_liquido", tipo_alvo="percentual", valor_alvo=30
        )
        assert resultado["melhor_canal"] in [c["canal"] for c in resultado["canais"]]

    def test_canais_ordenados_por_lucro(self, regras_multi):
        resultado = calcular_canais(
            regras_multi,
            preco_compra=10.0, embalagem=0, peso=1.0,
            imposto=4, quantidade=1,
            objetivo="lucro_liquido", tipo_alvo="percentual", valor_alvo=30
        )
        lucros = [c["lucro_liquido"] for c in resultado["canais"]]
        assert lucros == sorted(lucros, reverse=True)

    def test_embalagem_soma_ao_custo(self, regras_multi):
        r_sem = calcular_canais(regras_multi, 10.0, embalagem=0, peso=1.0,
                                 imposto=4, quantidade=1,
                                 objetivo="lucro_liquido", tipo_alvo="percentual", valor_alvo=30)
        r_com = calcular_canais(regras_multi, 10.0, embalagem=2.0, peso=1.0,
                                 imposto=4, quantidade=1,
                                 objetivo="lucro_liquido", tipo_alvo="percentual", valor_alvo=30)
        assert r_com["custo_total"] == pytest.approx(r_sem["custo_total"] + 2.0)

    def test_quantidade_multiplica_custo(self, regras_multi):
        r1 = calcular_canais(regras_multi, 10.0, 0, 1.0, 4, 1,
                              "lucro_liquido", "percentual", 30)
        r3 = calcular_canais(regras_multi, 10.0, 0, 1.0, 4, 3,
                              "lucro_liquido", "percentual", 30)
        assert r3["custo_total"] == pytest.approx(r1["custo_total"] * 3)

    def test_regras_vazias_retorna_sem_canais(self):
        resultado = calcular_canais(
            [], 10.0, 0, 1.0, 4, 1,
            "lucro_liquido", "percentual", 30
        )
        assert resultado["canais"] == []
        assert resultado["melhor_canal"] == ""

    def test_custo_zero_nao_explode(self, regras_multi):
        """Motor deve lidar graciosamente com custo zerado."""
        resultado = calcular_canais(
            regras_multi, 0.0, 0, 1.0, 4, 1,
            "lucro_liquido", "percentual", 30
        )
        # Pode calcular ou retornar vazio, mas não deve lançar exceção
        assert "canais" in resultado

    def test_formula_version_presente(self, regras_multi):
        resultado = calcular_canais(
            regras_multi, 10.0, 0, 1.0, 4, 1,
            "lucro_liquido", "percentual", 30
        )
        assert "formula_version" in resultado
        assert resultado["formula_version"].startswith("v")


# ══════════════════════════════════════════════════════════════
# 8. Geração de integração (preco_virtual / preco_promocional)
# ══════════════════════════════════════════════════════════════

class TestGerarIntegracao:
    @pytest.fixture
    def canais_exemplo(self, regras_multi):
        r = calcular_canais(
            regras_multi, 10.0, 0, 1.0, 4, 1,
            "lucro_liquido", "percentual", 30
        )
        return r["canais"]

    def test_preco_virtual_maior_que_promocional(self, canais_exemplo):
        resultado = gerar_integracao(
            canais_exemplo,
            modo_preco_virtual="percentual_acima",
            acrescimo_percentual=20,
            acrescimo_nominal=0,
            preco_manual=0,
            arredondamento="90",
        )
        for item in resultado["itens"]:
            assert item["preco_virtual"] >= item["preco_promocional"]

    def test_modo_valor_acima(self, canais_exemplo):
        resultado = gerar_integracao(
            canais_exemplo,
            modo_preco_virtual="valor_acima",
            acrescimo_percentual=0,
            acrescimo_nominal=5.0,
            preco_manual=0,
            arredondamento="sem",
        )
        for item in resultado["itens"]:
            diff = item["preco_virtual"] - item["preco_promocional"]
            assert diff == pytest.approx(5.0, abs=0.05)

    def test_arredondamento_90_aplicado(self, canais_exemplo):
        resultado = gerar_integracao(
            canais_exemplo,
            modo_preco_virtual="percentual_acima",
            acrescimo_percentual=20,
            acrescimo_nominal=0,
            preco_manual=0,
            arredondamento="90",
        )
        for item in resultado["itens"]:
            centavos = round(item["preco_virtual"] % 1, 2)
            assert centavos == pytest.approx(0.90, abs=0.01), (
                f"Esperado .90, encontrado {item['preco_virtual']}"
            )

    def test_deteccao_mudanca_custo(self, canais_exemplo):
        resultado = gerar_integracao(
            canais_exemplo,
            modo_preco_virtual="percentual_acima",
            acrescimo_percentual=20,
            acrescimo_nominal=0,
            preco_manual=0,
            arredondamento="sem",
            preco_custo_bling=10.5,
            preco_compra_anterior_bling=9.0,  # mudou
        )
        assert resultado.get("mudanca_custo_detectada") is True

    def test_sem_mudanca_custo(self, canais_exemplo):
        resultado = gerar_integracao(
            canais_exemplo,
            modo_preco_virtual="percentual_acima",
            acrescimo_percentual=20,
            acrescimo_nominal=0,
            preco_manual=0,
            arredondamento="sem",
            preco_custo_bling=10.0,
            preco_compra_anterior_bling=10.0,  # igual
        )
        assert resultado.get("mudanca_custo_detectada") is False


# ══════════════════════════════════════════════════════════════
# 9. Testes de integração — fluxo completo (sem Bling)
# ══════════════════════════════════════════════════════════════

class TestFluxoCompleto:
    """Simula o fluxo que o app.py executa sem precisar do Bling."""

    def test_fluxo_produto_simples(self, regras_multi):
        """Produto simples: custo R$ 8,56 + embalagem R$ 1,00, peso 1kg."""
        canais = calcular_canais(
            regras_multi,
            preco_compra=8.56, embalagem=1.0, peso=1.0,
            imposto=4, quantidade=1,
            objetivo="lucro_liquido", tipo_alvo="percentual", valor_alvo=30
        )
        assert canais["custo_total"] == pytest.approx(9.56)
        assert len(canais["canais"]) == 3

        integracao = gerar_integracao(
            canais["canais"],
            modo_preco_virtual="percentual_acima",
            acrescimo_percentual=20,
            acrescimo_nominal=0,
            preco_manual=0,
            arredondamento="90",
            modo_aprovacao="manual",
        )
        assert len(integracao["itens"]) == 3
        for item in integracao["itens"]:
            assert item["aprovacao_status"] == "Pendente aprovação manual"

    def test_fluxo_produto_composicao(self, regras_multi):
        """Produto composto: custo = soma dos componentes."""
        custo_total_composicao = 5.0 + 3.5 + 2.0  # = 10.50
        canais = calcular_canais(
            regras_multi,
            preco_compra=custo_total_composicao, embalagem=0, peso=0.8,
            imposto=4, quantidade=1,
            objetivo="margem", tipo_alvo="percentual", valor_alvo=20
        )
        assert canais["custo_total"] == pytest.approx(10.5)
        # Todos os canais devem ter margem >= 20% (aproximadamente)
        for canal in canais["canais"]:
            assert canal["margem"] >= 15.0, (
                f"Canal {canal['canal']}: margem {canal['margem']} abaixo do esperado"
            )

    def test_margem_negativa_identificavel(self, regras_multi):
        """Produto com custo muito alto deve resultar em lucro negativo ou baixo."""
        canais = calcular_canais(
            regras_multi,
            preco_compra=100.0, embalagem=0, peso=1.0,
            imposto=4, quantidade=1,
            objetivo="lucro_liquido", tipo_alvo="percentual", valor_alvo=0
        )
        # Com lucro alvo 0, o preço deve cobrir apenas os custos
        for canal in canais["canais"]:
            assert canal["lucro_liquido"] == pytest.approx(0.0, abs=0.5)
