"""
shopify_conferencia.py â€” Shinsei Pricing
MÃ³dulo de conferÃªncia de estoque e preÃ§o entre Bling e Shopify.
"""
from __future__ import annotations
import json
import logging
import requests
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)
DATA_DIR = Path(__file__).parent / "data"
FILA_SHOPIFY_PATH = DATA_DIR / "fila_shopify.json"
SHOPIFY_CONFIG_PATH = DATA_DIR / "shopify_config.json"

SHOPIFY_STORE = "pknw4n-eg"
SHOPIFY_API_VERSION = "2024-01"


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists(): return default
    try: return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return default

def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def carregar_fila_shopify() -> list[dict]:
    itens = _load_json(FILA_SHOPIFY_PATH, [])
    return itens if isinstance(itens, list) else []

def salvar_fila_shopify(itens: list[dict]) -> None:
    _save_json(FILA_SHOPIFY_PATH, itens)

def stats_fila_shopify() -> dict:
    itens = carregar_fila_shopify()
    stats = {"pendente": 0, "corrigido": 0, "ignorado": 0}
    for item in itens:
        s = str(item.get("status", "pendente")).lower()
        if s in stats: stats[s] += 1
    stats["total"] = len(itens)
    return stats

def _shopify_headers(token: str) -> dict:
    return {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json"
    }

def _shopify_get_token() -> Optional[str]:
    config = _load_json(SHOPIFY_CONFIG_PATH, {})
    return config.get("access_token") or "SHOPIFY_TOKEN_REMOVED"

def _shopify_listar_produtos(token: str, limit: int = 250, page_info: str = None) -> tuple[list, Optional[str]]:
    """Lista produtos da Shopify com paginaÃ§Ã£o."""
    url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/products.json"
    params = {"limit": limit, "fields": "id,title,variants,status"}
    if page_info:
        params = {"limit": limit, "page_info": page_info}
    try:
        res = requests.get(url, params=params, headers=_shopify_headers(token), timeout=30)
        if res.status_code == 200:
            data = res.json()
            produtos = data.get("products", [])
            # PaginaÃ§Ã£o via Link header
            next_page = None
            link_header = res.headers.get("Link", "")
            if 'rel="next"' in link_header:
                import re
                match = re.search(r'page_info=([^&>]+).*rel="next"', link_header)
                if match:
                    next_page = match.group(1)
            return produtos, next_page
        else:
            logger.warning("Shopify produtos status %s: %s", res.status_code, res.text[:200])
    except Exception as e:
        logger.warning("Erro ao listar produtos Shopify: %s", e)
    return [], None

def _shopify_get_inventory(token: str, inventory_item_id: int, location_id: int) -> int:
    """Busca estoque de um item em uma localizaÃ§Ã£o."""
    url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/inventory_levels.json"
    params = {"inventory_item_ids": inventory_item_id, "location_ids": location_id}
    try:
        res = requests.get(url, params=params, headers=_shopify_headers(token), timeout=15)
        if res.status_code == 200:
            levels = res.json().get("inventory_levels", [])
            if levels:
                return int(levels[0].get("available", 0) or 0)
    except Exception as e:
        logger.warning("Erro ao buscar estoque Shopify: %s", e)
    return 0

def _shopify_get_locations(token: str) -> list[dict]:
    """Lista localizaÃ§Ãµes ativas da Shopify."""
    url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/locations.json"
    try:
        res = requests.get(url, headers=_shopify_headers(token), timeout=15)
        if res.status_code == 200:
            return res.json().get("locations", [])
    except Exception as e:
        logger.warning("Erro ao buscar localizaÃ§Ãµes Shopify: %s", e)
    return []

def _shopify_atualizar_estoque(token: str, inventory_item_id: int, location_id: int, quantidade: int) -> dict:
    """Atualiza estoque de um item em uma localizaÃ§Ã£o."""
    url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/inventory_levels/set.json"
    payload = {
        "location_id": location_id,
        "inventory_item_id": inventory_item_id,
        "available": quantidade
    }
    try:
        res = requests.post(url, json=payload, headers=_shopify_headers(token), timeout=15)
        if res.status_code in [200, 201]:
            return {"ok": True}
        return {"ok": False, "erro": res.text[:200]}
    except Exception as e:
        return {"ok": False, "erro": str(e)}

def _shopify_atualizar_preco(token: str, variant_id: int, preco: float, preco_comparado: float = None) -> dict:
    """Atualiza preÃ§o de uma variante."""
    url = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{SHOPIFY_API_VERSION}/variants/{variant_id}.json"
    payload = {"variant": {"id": variant_id, "price": str(round(preco, 2))}}
    if preco_comparado:
        payload["variant"]["compare_at_price"] = str(round(preco_comparado, 2))
    try:
        res = requests.put(url, json=payload, headers=_shopify_headers(token), timeout=15)
        if res.status_code == 200:
            return {"ok": True}
        return {"ok": False, "erro": res.text[:200]}
    except Exception as e:
        return {"ok": False, "erro": str(e)}

def conferir_shopify(bling_client, max_produtos: int = 500, tipo: str = "") -> dict:
    """
    Compara produtos Shopify com Bling â€” estoque e preÃ§o.
    Usa inventory_quantity direto da variante (sem precisar de read_locations).
    """
    token = _shopify_get_token()
    if not token:
        return {"ok": False, "erro": "Token da Shopify nÃ£o configurado."}

    # Busca location_id do primeiro depÃ³sito ativo
    locations = _shopify_get_locations(token)
    location_id = locations[0]["id"] if locations else None
    if not location_id:
        logger.warning("Shopify: nenhuma localizaÃ§Ã£o encontrada, usando inventory_quantity direto")

    fila = carregar_fila_shopify()
    chaves_pendentes = {(i.get("sku"), i.get("tipo")) for i in fila if i.get("status") == "pendente"}

    verificados = 0
    divergencias_estoque = 0
    divergencias_preco = 0
    erros = 0

    page_info = None
    produtos_processados = 0

    while produtos_processados < max_produtos:
        produtos, next_page = _shopify_listar_produtos(token, limit=250, page_info=page_info)
        if not produtos:
            break

        for produto in produtos:
            if produto.get("status") != "active":
                continue

            for variant in produto.get("variants", []):
                sku = variant.get("sku", "").strip()
                if not sku:
                    continue

                verificados += 1
                time.sleep(0.3)

                try:
                    # Busca produto no Bling
                    busca = bling_client.get_product_by_sku(sku)
                    if not busca.get("encontrado"):
                        continue

                    prod_bling = busca.get("produto", {})
                    estoque_bling = int((prod_bling.get("estoque") or {}).get("saldoVirtualTotal") or 0)
                    preco_bling = float(prod_bling.get("preco") or 0)

                    # Estoque Shopify â€” direto da variante (inventory_quantity)
                    estoque_shopify = int(variant.get("inventory_quantity") or 0)
                    inventory_item_id = variant.get("inventory_item_id")

                    # PreÃ§o Shopify
                    preco_shopify = float(variant.get("price") or 0)

                    titulo = produto.get("title", "")[:60]
                    variant_id = variant.get("id")

                    # â”€â”€ DivergÃªncia de estoque â”€â”€
                    # ── Filtro por tipo + Divergência de estoque ──
                    if tipo not in ("preco",) and estoque_bling != estoque_shopify:
                        divergencias_estoque += 1
                        chave = (sku, "estoque")
                        if chave not in chaves_pendentes:
                            fila.append({
                                "id": f"shp_est_{sku}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
                                "sku": sku,
                                "titulo": titulo,
                                "tipo": "estoque",
                                "produto_id_shopify": str(produto.get("id")),
                                "variant_id": str(variant_id),
                                "inventory_item_id": str(inventory_item_id),
                                "location_id": str(location_id),
                                "estoque_bling": estoque_bling,
                                "estoque_shopify": estoque_shopify,
                                "diferenca": estoque_bling - estoque_shopify,
                                "preco_bling": preco_bling,
                                "preco_shopify": preco_shopify,
                                "status": "pendente",
                                "criado_em": datetime.utcnow().isoformat(),
                                "atualizado_em": None,
                            })
                            chaves_pendentes.add(chave)
                            logger.info("DivergÃªncia Shopify estoque: sku=%s bling=%d shopify=%d", sku, estoque_bling, estoque_shopify)

                    # â”€â”€ DivergÃªncia de preÃ§o (tolerÃ¢ncia R$0,50) â”€â”€
                    if tipo not in ("estoque",) and abs(preco_bling - preco_shopify) > 0.50 and preco_bling > 0:
                        divergencias_preco += 1
                        chave = (sku, "preco")
                        if chave not in chaves_pendentes:
                            fila.append({
                                "id": f"shp_preco_{sku}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
                                "sku": sku,
                                "titulo": titulo,
                                "tipo": "preco",
                                "produto_id_shopify": str(produto.get("id")),
                                "variant_id": str(variant_id),
                                "preco_bling": preco_bling,
                                "preco_shopify": preco_shopify,
                                "diferenca": round(preco_bling - preco_shopify, 2),
                                "status": "pendente",
                                "criado_em": datetime.utcnow().isoformat(),
                                "atualizado_em": None,
                            })
                            chaves_pendentes.add(chave)
                            logger.info("DivergÃªncia Shopify preÃ§o: sku=%s bling=%.2f shopify=%.2f", sku, preco_bling, preco_shopify)

                except Exception as e:
                    erros += 1
                    logger.warning("Erro ao conferir SKU %s na Shopify: %s", sku, e)

            produtos_processados += 1

        if not next_page:
            break
        page_info = next_page

    salvar_fila_shopify(fila)
    logger.info("ConferÃªncia Shopify: %d verificados, %d div. estoque, %d div. preÃ§o, %d erros",
                verificados, divergencias_estoque, divergencias_preco, erros)
    return {
        "ok": True,
        "verificados": verificados,
        "divergencias_estoque": divergencias_estoque,
        "divergencias_preco": divergencias_preco,
        "erros": erros,
        "total_pendentes": len([i for i in fila if i.get("status") == "pendente"]),
    }

def corrigir_shopify(item_id_fila: str) -> dict:
    """Corrige divergÃªncia de estoque ou preÃ§o na Shopify."""
    fila = carregar_fila_shopify()
    item = next((i for i in fila if i.get("id") == item_id_fila), None)
    if not item: return {"ok": False, "erro": "Item nÃ£o encontrado."}
    if item.get("status") != "pendente": return {"ok": False, "erro": "Item jÃ¡ processado."}

    token = _shopify_get_token()
    tipo = item.get("tipo")
    resultado = {"ok": False, "erro": "Tipo nÃ£o suportado."}

    if tipo == "estoque":
        resultado = _shopify_atualizar_estoque(
            token,
            int(item.get("inventory_item_id", 0)),
            int(item.get("location_id", 0)),
            int(item.get("estoque_bling", 0))
        )
    elif tipo == "preco":
        resultado = _shopify_atualizar_preco(
            token,
            int(item.get("variant_id", 0)),
            float(item.get("preco_bling", 0))
        )

    item["status"] = "corrigido" if resultado.get("ok") else "pendente"
    item["atualizado_em"] = datetime.utcnow().isoformat()
    item["resultado"] = resultado
    salvar_fila_shopify(fila)
    return resultado

def ignorar_shopify(item_id_fila: str) -> dict:
    fila = carregar_fila_shopify()
    item = next((i for i in fila if i.get("id") == item_id_fila), None)
    if not item: return {"ok": False}
    item["status"] = "ignorado"
    item["atualizado_em"] = datetime.utcnow().isoformat()
    salvar_fila_shopify(fila)
    return {"ok": True}

def salvar_shopify_token(token: str) -> None:
    _save_json(SHOPIFY_CONFIG_PATH, {"access_token": token, "salvo_em": datetime.utcnow().isoformat()})

