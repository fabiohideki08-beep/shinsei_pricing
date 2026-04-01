import time
from bling_service import buscar_produtos, atualizar_preco
from pricing_engine import (
    carregar_historico,
    salvar_historico,
    calcular_preco
)
from config import INTERVALO_ATUALIZACAO


def atualizar_produtos():
    produtos = buscar_produtos()
    historico = carregar_historico()

    for item in produtos:
        produto = item.get("produto", {})
        id_produto = produto.get("id")
        custo = float(produto.get("precoCusto", 0))

        if not id_produto:
            continue

        custo_antigo = historico.get(str(id_produto))

        if custo_antigo != custo:
            novo_preco = calcular_preco(custo)

            print(f"Atualizando produto {id_produto}: {custo} -> {novo_preco}")

            atualizar_preco(id_produto, novo_preco)

            historico[str(id_produto)] = custo

    salvar_historico(historico)


def iniciar_scheduler():
    while True:
        try:
            atualizar_produtos()
        except Exception as e:
            print("Erro:", e)

        time.sleep(INTERVALO_ATUALIZACAO)


if __name__ == "__main__":
    iniciar_scheduler()
