# -*- coding: utf-8 -*-
"""
teste_frete.py
Testa o motor de cálculo de frete localmente (sem servidor).

Uso:
  python teste_frete.py
"""
import asyncio
import sys

def pr(m=""): sys.stdout.buffer.write((str(m)+"\n").encode("utf-8","replace")); sys.stdout.buffer.flush()
def sec(t):   pr(); pr("="*65); pr(f"  {t}"); pr("="*65)

async def main():
    from services.frete import calculate_freight, SUBSIDY_PER_ITEM, ORIGIN_CEP

    pr(f"  SUBSIDY_PER_ITEM : R${SUBSIDY_PER_ITEM}")
    pr(f"  ORIGIN_CEP       : {ORIGIN_CEP}")

    casos = [
        # (descricao,                  cep,       qty, peso_kg, valor_R$)
        ("São Paulo - SP (RMSP)",      "01310100", 1,   0.3,    50.0),
        ("Osasco - SP (RMSP)",         "06036003", 2,   0.6,   100.0),
        ("Campinas - SP (Interior)",   "13010000", 1,   0.3,    50.0),
        ("Campinas 2 itens",           "13010000", 2,   0.6,   100.0),
        ("Rio de Janeiro - RJ",        "20040020", 1,   0.3,    50.0),
        ("Rio 3 itens",                "20040020", 3,   0.9,   150.0),
        ("Manaus - AM",                "69005010", 1,   0.3,    50.0),
        ("Manaus 5 itens",             "69005010", 5,   1.5,   250.0),
    ]

    for descricao, cep, qty, peso, valor in casos:
        sec(f"{descricao} — CEP {cep} — {qty} item(ns)")
        try:
            result = await calculate_freight(cep, qty, peso, valor)
            pr(f"  Cidade  : {result.city} - {result.state}  (RMSP={result.is_rmsp})")
            pr(f"  Subsídio: R${result.subsidy_total:.2f}  ({qty} × R${SUBSIDY_PER_ITEM})")
            pr(f"  Grátis  : {'SIM 🎉' if result.is_free else 'NÃO'}")
            if not result.is_free:
                pr(f"  Faltam  : {result.items_for_free_shipping} item(ns) para frete grátis")
            pr(f"  Opções  :")
            for opt in result.options:
                tag = "GRÁTIS" if opt.is_free else f"R${opt.price_final:.2f}"
                pr(f"    {opt.name:<8}  real=R${opt.price_real:.2f}  final={tag}  prazo={opt.delivery_days}d")
        except Exception as e:
            pr(f"  ERRO: {e}")

    sec("TESTE ENDPOINT /frete/progresso")
    from routes.frete import progresso_frete
    for qty in [1, 2, 3]:
        resp = await progresso_frete(qty=qty, frete_real=18.0)
        pr(f"  qty={qty}  subsidio=R${resp['subsidio']:.2f}  final=R${resp['frete_final']:.2f}  msg={resp['mensagem']}")

    pr()
    pr("  ✅ Testes concluídos.")

if __name__ == "__main__":
    asyncio.run(main())
