"""
estoque_conferencia.py — Shinsei Pricing Fase 8
Módulo de conferência de estoque entre Bling e marketplaces.

Funcionalidades:
- Compara estoque do Bling vs marketplaces por SKU
- Divergências vão para fila de validação separada
- Ação "Corrigir" sobrescreve estoque do marketplace com valor do Bling
- Roda junto ao scheduler
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
FILA_ESTOQUE_PATH = DATA_DIR / "fila_estoque.json"


# ─── Persistência da fila de estoque ─────────────────────

def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def carregar_fila_estoque() -> list[dict]:
    itens = _load_json(FILA_ESTOQUE_PATH, [])
    return itens if isinstance(itens, list) else []


def salvar_fila_estoque(itens: list[dict]) -> None:
    _save_json(FILA_ESTOQUE_PATH, itens)


def stats_fila_estoque() -> dict:
    itens = carregar_fila_estoque()
    stats = {"pendente": 0, "corrigido": 0, "ignorado": 0}
    for item in itens:
        s = str(item.get("status", "pendente")).lower()
        if s in stats:
            stats[s] += 1
    stats["total"] = len(itens)
    return stats


# ─── Busca estoque no Bling ───────────────────────────────

def _estoque_bling(produto: dict) -> int:
    """Extrai estoque do produto retornado pelo Bling."""
    # Tenta campo estoques (lista de depósitos)
    estoques = produto.get("estoques") or produto.get("estoque") or []
    if isinstance(estoques, list):
        total = 0
        for dep in estoques:
            if isinstance(dep, dict):
                total += int(float(dep.get("saldoFisico") or dep.get("saldo") or dep.get("quantidade") or 0))
        if total > 0:
            return total
    # Tenta campo direto
    for campo in ["saldoFisico", "saldo", "estoque", "quantidade"]:
        v = produto.get(campo)
        if v is not None:
            try:
                return int(float(v))
            except Exception:
                pass
    return 0


# ─── Busca estoque no ML ──────────────────────────────────

def _estoque_ml(ml_service, item_id: str) -> Optional[int]:
    """Busca estoque de um anúncio no Mercado Livre."""
    try:
        res = ml_service.obter_preco_venda(item_id)
        if not res.get("success"):
            return None
        data = res.get("data", {})
        # available_quantity é o estoque disponível no ML
        qty = data.get("available_quantity")
        if qty is not None:
            return int(qty)
        # initial_quantity é o total cadastrado
        qty = data.get("initial_quantity")
        if qty is not None:
            return int(qty)
    except Exception as e:
        logger.warning("Erro ao buscar estoque ML item %s: %s", item_id, e)
    return None


def _atualizar_estoque_ml(ml_service, item_id: str, quantidade: int) -> dict:
    """Atualiza estoque de um anúncio no ML."""
    try:
        import requests
        url = f"https://api.mercadolibre.com/items/{item_id}"
        res = requests.put(
            url,
            json={"available_quantity": quantidade},
            headers=ml_service._headers(),
            timeout=30,
        )
        if res.status_code in [200, 201]:
            return {"ok": True, "canal": "Mercado Livre", "item_id": item_id}
        return {"ok": False, "canal": "Mercado Livre", "item_id": item_id, "erro": res.text}
    except Exception as e:
        return {"ok": False, "canal": "Mercado Livre", "item_id": item_id, "erro": str(e)}


# ─── Ciclo de conferência ─────────────────────────────────

def conferir_estoques(bling_client, ml_service=None) -> dict:
    """
    Compara estoques do Bling vs marketplaces.
    Adiciona divergências à fila de validação.
    Retorna relatório do ciclo.
    """
    novos = 0
    verificados = 0
    erros = 0

    try:
        res = bling_client.list_products(page=1, limit=100)
        produtos_raw = res if isinstance(res, list) else (res.get("data") or [])
    except Exception as e:
        logger.error("Conferência de estoque: erro ao listar produtos Bling: %s", e)
        return {"ok": False, "erro": str(e)}

    fila = carregar_fila_estoque()
    skus_na_fila = {
        (i.get("sku"), i.get("canal"))
        for i in fila
        if i.get("status") == "pendente"
    }

    for prod_raw in produtos_raw:
        if not isinstance(prod_raw, dict):
            continue
        try:
            prod = prod_raw.get("produto") or prod_raw
            sku = str(prod.get("codigo") or "").strip()
            if not sku:
                continue

            # Busca produto completo para ter estoques
            prod_id = prod.get("id")
            if prod_id:
                try:
                    prod_full = bling_client.get_product(int(prod_id))
                    if prod_full:
                        prod = prod_full
                except Exception:
                    pass

            estoque_bling = _estoque_bling(prod)
            nome = str(prod.get("nome") or prod.get("descricao") or sku)

            verificados += 1

            # ── Verifica ML ──────────────────────────────
            if ml_service:
                # Tenta encontrar item_id do ML no produto Bling
                lista_ecommerce = prod.get("listaEcommerce") or []
                for loja in lista_ecommerce if isinstance(lista_ecommerce, list) else []:
                    if not isinstance(loja, dict):
                        continue
                    nome_loja = str(loja.get("nomeLoja") or "").lower()
                    if "mercado" not in nome_loja and "ml" not in nome_loja:
                        continue
                    item_id = str(loja.get("idAnuncio") or loja.get("id") or "").strip()
                    if not item_id:
                        continue

                    estoque_ml = _estoque_ml(ml_service, item_id)
                    if estoque_ml is None:
                        continue

                    if estoque_ml != estoque_bling:
                        chave = (sku, "Mercado Livre")
                        if chave not in skus_na_fila:
                            fila.append({
                                "id": f"est_{sku}_ml_{item_id}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
                                "sku": sku,
                                "nome": nome,
                                "canal": "Mercado Livre",
                                "item_id_marketplace": item_id,
                                "estoque_bling": estoque_bling,
                                "estoque_marketplace": estoque_ml,
                                "diferenca": estoque_bling - estoque_ml,
                                "status": "pendente",
                                "criado_em": datetime.utcnow().isoformat(),
                                "atualizado_em": None,
                                "resultado_correcao": None,
                            })
                            skus_na_fila.add(chave)
                            novos += 1
                            logger.info(
                                "Divergência estoque: SKU=%s ML=%s Bling=%s ML_item=%s",
                                sku, estoque_ml, estoque_bling, item_id
                            )

        except Exception as e:
            erros += 1
            logger.warning("Conferência estoque: erro no produto %s: %s", prod_raw.get("id"), e)

    salvar_fila_estoque(fila)

    logger.info(
        "Conferência estoque: %d verificados, %d divergências novas, %d erros",
        verificados, novos, erros
    )
    return {
        "ok": True,
        "verificados": verificados,
        "novas_divergencias": novos,
        "erros": erros,
        "total_na_fila": len([i for i in fila if i.get("status") == "pendente"]),
    }


def corrigir_item_estoque(item_id_fila: str, ml_service=None) -> dict:
    """
    Corrige o estoque do marketplace com o valor do Bling.
    Marca item como corrigido na fila.
    """
    fila = carregar_fila_estoque()
    item = next((i for i in fila if i.get("id") == item_id_fila), None)

    if not item:
        return {"ok": False, "erro": "Item não encontrado na fila de estoque."}
    if item.get("status") != "pendente":
        return {"ok": False, "erro": f"Item já com status '{item.get('status')}'."}

    canal = item.get("canal", "")
    item_id_mp = item.get("item_id_marketplace", "")
    estoque_bling = int(item.get("estoque_bling", 0))
    resultado = {"ok": False, "erro": "Canal não suportado para correção automática."}

    if "mercado" in canal.lower() or "ml" in canal.lower():
        if ml_service:
            resultado = _atualizar_estoque_ml(ml_service, item_id_mp, estoque_bling)
        else:
            resultado = {"ok": False, "erro": "Serviço ML não disponível."}

    agora = datetime.utcnow().isoformat()
    item["status"] = "corrigido" if resultado.get("ok") else "pendente"
    item["atualizado_em"] = agora
    item["resultado_correcao"] = resultado

    salvar_fila_estoque(fila)
    logger.info(
        "Correção estoque: sku=%s canal=%s ok=%s",
        item.get("sku"), canal, resultado.get("ok")
    )
    return resultado


def ignorar_item_estoque(item_id_fila: str) -> dict:
    """Marca item como ignorado na fila de estoque."""
    fila = carregar_fila_estoque()
    item = next((i for i in fila if i.get("id") == item_id_fila), None)
    if not item:
        return {"ok": False, "erro": "Item não encontrado."}
    item["status"] = "ignorado"
    item["atualizado_em"] = datetime.utcnow().isoformat()
    salvar_fila_estoque(fila)
    return {"ok": True}
