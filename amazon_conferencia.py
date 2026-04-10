"""
amazon_conferencia.py — Shinsei Pricing
Conferência de estoque e pedidos Amazon vs Bling.
"""
from __future__ import annotations
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).parent
FILA_PATH = BASE_DIR / "data" / "fila_amazon.json"


def carregar_fila() -> list:
    if not FILA_PATH.exists():
        return []
    try:
        return json.loads(FILA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def salvar_fila(itens: list):
    FILA_PATH.write_text(json.dumps(itens, ensure_ascii=False, indent=2), encoding="utf-8")


def stats_fila() -> dict:
    itens = carregar_fila()
    return {
        "pendente": sum(1 for i in itens if i.get("status") == "pendente"),
        "corrigido": sum(1 for i in itens if i.get("status") == "corrigido"),
        "ignorado": sum(1 for i in itens if i.get("status") == "ignorado"),
        "total": len(itens)
    }


def conferir_amazon(bling_client=None, max_items: int = 200, tipo: str = "") -> dict:
    """
    Confere estoque e pedidos Amazon vs Bling.
    tipo: '' = tudo, 'estoque' = só estoque, 'pedidos' = só pedidos
    """
    try:
        from amazon_client import AmazonClient
        amazon = AmazonClient()
    except Exception as e:
        return {"ok": False, "erro": f"Amazon não configurado: {e}"}

    fila = carregar_fila()
    chaves_pendentes = {(i.get("sku"), i.get("tipo")) for i in fila if i.get("status") == "pendente"}

    verificados = 0
    divergencias_estoque = 0
    divergencias_pedidos = 0
    erros = 0
    agora = datetime.now().isoformat()

    # ── Conferência de estoque FBA ────────────────────────────
    if tipo in ("", "estoque"):
        try:
            res = amazon.get_inventory()
            summaries = res.get("payload", {}).get("inventorySummaries", [])
            logger.info("Amazon: %d itens de estoque encontrados", len(summaries))

            for item in summaries[:max_items]:
                sku = item.get("sellerSku", "").strip()
                if not sku:
                    continue

                estoque_amazon = int(item.get("totalQuantity") or item.get("fulfillableQuantity") or 0)
                asin = item.get("asin", "")
                nome = item.get("productName", "")[:60]
                verificados += 1

                # Busca estoque no Bling
                estoque_bling = 0
                if bling_client:
                    try:
                        time.sleep(0.3)
                        busca = bling_client.get_product_by_sku(sku)
                        if busca.get("encontrado"):
                            prod = busca.get("produto", {})
                            estoque_bling = int((prod.get("estoque") or {}).get("saldoVirtualTotal") or 0)
                    except Exception as e:
                        logger.debug("Erro ao buscar SKU %s no Bling: %s", sku, e)
                        erros += 1
                        continue

                if estoque_bling != estoque_amazon:
                    divergencias_estoque += 1
                    chave = (sku, "estoque")
                    if chave not in chaves_pendentes:
                        fila.append({
                            "id": f"amz_est_{sku}_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                            "sku": sku,
                            "asin": asin,
                            "nome": nome,
                            "tipo": "estoque",
                            "estoque_bling": estoque_bling,
                            "estoque_amazon": estoque_amazon,
                            "diferenca": estoque_bling - estoque_amazon,
                            "detectado_em": agora,
                            "status": "pendente"
                        })
                        chaves_pendentes.add(chave)
                        logger.info("Divergência Amazon estoque: sku=%s bling=%d amazon=%d",
                                    sku, estoque_bling, estoque_amazon)

        except Exception as e:
            logger.error("Erro ao conferir estoque Amazon: %s", e)

    # ── Conferência de pedidos recentes ───────────────────────
    if tipo in ("", "pedidos"):
        try:
            created_after = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
            res = amazon.get_orders(created_after=created_after)
            orders = res.get("payload", {}).get("Orders", [])
            logger.info("Amazon: %d pedidos encontrados", len(orders))

            for order in orders[:50]:
                order_id = order.get("AmazonOrderId", "")
                status = order.get("OrderStatus", "")
                if status not in ("Shipped", "Delivered", "Unshipped", "PartiallyShipped"):
                    continue

                try:
                    time.sleep(0.5)
                    items_res = amazon.get_order_items(order_id)
                    items = items_res.get("payload", {}).get("OrderItems", [])

                    for item in items:
                        sku = item.get("SellerSKU", "").strip()
                        if not sku:
                            continue

                        preco_amazon = float((item.get("ItemPrice") or {}).get("Amount") or 0)
                        qtd = int(item.get("QuantityOrdered") or 1)
                        nome = item.get("Title", "")[:60]
                        asin = item.get("ASIN", "")

                        # Compara com preço do Bling
                        if bling_client and preco_amazon > 0:
                            try:
                                busca = bling_client.get_product_by_sku(sku)
                                if busca.get("encontrado"):
                                    prod = busca.get("produto", {})
                                    preco_bling = float(prod.get("preco") or 0)

                                    if preco_bling > 0 and abs(preco_bling - preco_amazon) > 0.50:
                                        divergencias_pedidos += 1
                                        chave = (sku, "preco")
                                        if chave not in chaves_pendentes:
                                            fila.append({
                                                "id": f"amz_preco_{sku}_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                                                "sku": sku,
                                                "asin": asin,
                                                "nome": nome,
                                                "tipo": "preco",
                                                "order_id": order_id,
                                                "preco_bling": preco_bling,
                                                "preco_amazon": preco_amazon,
                                                "diferenca": round(preco_bling - preco_amazon, 2),
                                                "detectado_em": agora,
                                                "status": "pendente"
                                            })
                                            chaves_pendentes.add(chave)
                                            logger.info("Divergência Amazon preço: sku=%s bling=%.2f amazon=%.2f",
                                                        sku, preco_bling, preco_amazon)
                            except Exception:
                                pass

                except Exception as e:
                    logger.debug("Erro ao buscar itens do pedido %s: %s", order_id, e)

        except Exception as e:
            logger.error("Erro ao conferir pedidos Amazon: %s", e)

    salvar_fila(fila)

    return {
        "ok": True,
        "verificados": verificados,
        "divergencias_estoque": divergencias_estoque,
        "divergencias_pedidos": divergencias_pedidos,
        "erros": erros,
        "total_pendentes": sum(1 for i in fila if i.get("status") == "pendente")
    }
