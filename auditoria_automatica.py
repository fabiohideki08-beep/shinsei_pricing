"""
auditoria_automatica.py — Shinsei Pricing
Módulo de auditoria automática: estoque e preço.
Roda junto ao scheduler.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
FILA_AUDITORIA_PATH = DATA_DIR / "fila_auditoria.json"


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


def carregar_fila_auditoria() -> list[dict]:
    itens = _load_json(FILA_AUDITORIA_PATH, [])
    return itens if isinstance(itens, list) else []


def salvar_fila_auditoria(itens: list[dict]) -> None:
    _save_json(FILA_AUDITORIA_PATH, itens)


def stats_fila_auditoria() -> dict:
    itens = carregar_fila_auditoria()
    stats = {"pendente": 0, "corrigido": 0, "ignorado": 0}
    for item in itens:
        s = str(item.get("status", "pendente")).lower()
        if s in stats:
            stats[s] += 1
    stats["total"] = len(itens)
    # Por tipo
    stats["estoque"] = sum(1 for i in itens if i.get("tipo") == "estoque")
    stats["preco"] = sum(1 for i in itens if i.get("tipo") == "preco")
    return stats


def _ja_existe_pendente(fila: list[dict], sku: str, canal: str, tipo: str) -> bool:
    return any(
        i.get("sku") == sku and i.get("canal") == canal and
        i.get("tipo") == tipo and i.get("status") == "pendente"
        for i in fila
    )


# ─── CONFERÊNCIA DE ESTOQUE ──────────────────────────────

def _estoque_bling(produto: dict) -> int:
    estoques = produto.get("estoques") or produto.get("estoque") or []
    if isinstance(estoques, list):
        total = sum(int(float(d.get("saldoFisico") or d.get("saldo") or 0))
                   for d in estoques if isinstance(d, dict))
        if total > 0:
            return total
    for campo in ["saldoFisico", "saldo", "estoque", "quantidade"]:
        v = produto.get(campo)
        if v is not None:
            try:
                return int(float(v))
            except Exception:
                pass
    # Tenta dentro de estrutura de estoque
    est = produto.get("estoque") or {}
    if isinstance(est, dict):
        v = est.get("saldoVirtualTotal") or est.get("saldoFisico")
        if v:
            try:
                return int(float(v))
            except Exception:
                pass
    return 0


def _estoque_ml(ml_service, item_id: str) -> Optional[int]:
    try:
        res = ml_service.obter_preco_venda(item_id)
        if not res.get("success"):
            return None
        data = res.get("data", {})
        qty = data.get("available_quantity") or data.get("initial_quantity")
        return int(qty) if qty is not None else None
    except Exception as e:
        logger.warning("Erro ao buscar estoque ML %s: %s", item_id, e)
        return None


def _preco_ml(ml_service, item_id: str) -> dict:
    """Retorna preço e preço promocional do anúncio no ML."""
    try:
        res = ml_service.obter_preco_venda(item_id)
        if not res.get("success"):
            return {}
        data = res.get("data", {})
        return {
            "preco": float(data.get("price") or 0),
            "preco_promocional": float(
                (data.get("sale_price") or {}).get("amount") or
                data.get("original_price") or 0
            ),
            "status": data.get("status", ""),
            "titulo": data.get("title", ""),
        }
    except Exception as e:
        logger.warning("Erro ao buscar preço ML %s: %s", item_id, e)
        return {}


def _buscar_ultimo_preco_aprovado(sku: str, canal: str) -> Optional[dict]:
    """Busca o último preço aprovado pelo Shinsei para esse SKU/canal."""
    try:
        fila_path = DATA_DIR / "fila_aprovacao.json"
        fila = _load_json(fila_path, [])
        # Filtra aprovados para esse SKU
        aprovados = [
            i for i in fila
            if i.get("sku") == sku and i.get("status") == "aprovado"
        ]
        if not aprovados:
            return None
        # Pega o mais recente
        ultimo = sorted(aprovados, key=lambda x: x.get("atualizado_em") or "", reverse=True)[0]
        marketplaces = ultimo.get("marketplaces") or {}
        # Normaliza canal key
        canal_norm = canal.lower().replace(" ", "_").replace("-", "_")
        dados_canal = marketplaces.get(canal_norm) or marketplaces.get(canal)
        if not dados_canal:
            return None
        raw = dados_canal.get("raw") or dados_canal
        return {
            "preco_calculado": float(raw.get("preco_promocional") or raw.get("preco_final") or 0),
            "preco_virtual": float(raw.get("preco_virtual") or raw.get("preco") or 0),
            "aprovado_em": ultimo.get("atualizado_em"),
        }
    except Exception:
        return None


# ─── CICLO PRINCIPAL ─────────────────────────────────────

def rodar_auditoria(bling_client, ml_service=None) -> dict:
    """
    Roda auditoria completa: estoque + preço.
    Adiciona divergências na fila de auditoria.
    """
    novos_estoque = 0
    novos_preco = 0
    verificados = 0
    erros = 0

    try:
        res = bling_client.list_products(page=1, limit=100)
        produtos_raw = res if isinstance(res, list) else (res.get("data") or [])
    except Exception as e:
        logger.error("Auditoria: erro ao listar produtos: %s", e)
        return {"ok": False, "erro": str(e)}

    fila = carregar_fila_auditoria()

    for prod_raw in produtos_raw:
        if not isinstance(prod_raw, dict):
            continue
        try:
            prod = prod_raw.get("produto") or prod_raw
            sku = str(prod.get("codigo") or "").strip()
            if not sku:
                continue

            # Busca produto completo
            prod_id = prod.get("id")
            if prod_id:
                try:
                    prod_full = bling_client.get_product(int(prod_id))
                    if prod_full:
                        prod = prod_full
                except Exception:
                    pass

            nome = str(prod.get("nome") or prod.get("descricao") or sku)
            estoque_bling = _estoque_bling(prod)
            lista_ecommerce = prod.get("listaEcommerce") or []
            verificados += 1

            if not ml_service or not lista_ecommerce:
                continue

            for loja in lista_ecommerce:
                if not isinstance(loja, dict):
                    continue
                nome_loja = str(loja.get("nomeLoja") or "").lower()
                if "mercado" not in nome_loja and "ml" not in nome_loja:
                    continue
                item_id = str(loja.get("idAnuncio") or loja.get("id") or "").strip()
                if not item_id:
                    continue

                canal = "Mercado Livre"

                # ── Conferência de estoque ──
                estoque_ml = _estoque_ml(ml_service, item_id)
                if estoque_ml is not None and estoque_ml != estoque_bling:
                    if not _ja_existe_pendente(fila, sku, canal, "estoque"):
                        fila.append({
                            "id": f"aud_est_{sku}_{item_id}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
                            "tipo": "estoque",
                            "sku": sku,
                            "nome": nome,
                            "canal": canal,
                            "item_id_marketplace": item_id,
                            "valor_shinsei": estoque_bling,
                            "valor_marketplace": estoque_ml,
                            "diferenca": estoque_bling - estoque_ml,
                            "status": "pendente",
                            "criado_em": datetime.utcnow().isoformat(),
                            "atualizado_em": None,
                            "resultado_correcao": None,
                        })
                        novos_estoque += 1
                        logger.info("Auditoria estoque: SKU=%s Bling=%s ML=%s", sku, estoque_bling, estoque_ml)

                # ── Conferência de preço ──
                precos_ml = _preco_ml(ml_service, item_id)
                if precos_ml:
                    preco_shinsei = _buscar_ultimo_preco_aprovado(sku, canal)
                    if preco_shinsei:
                        preco_calc = preco_shinsei["preco_calculado"]
                        preco_virt = preco_shinsei["preco_virtual"]
                        preco_pub = precos_ml.get("preco", 0)
                        promo_pub = precos_ml.get("preco_promocional", 0)

                        # Verifica divergência (tolerância de R$0,10)
                        diverge_preco = abs(preco_pub - preco_virt) > 0.10
                        diverge_promo = promo_pub > 0 and abs(promo_pub - preco_calc) > 0.10

                        if (diverge_preco or diverge_promo) and not _ja_existe_pendente(fila, sku, canal, "preco"):
                            fila.append({
                                "id": f"aud_preco_{sku}_{item_id}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
                                "tipo": "preco",
                                "sku": sku,
                                "nome": nome,
                                "canal": canal,
                                "item_id_marketplace": item_id,
                                "preco_shinsei_calculado": preco_calc,
                                "preco_shinsei_virtual": preco_virt,
                                "preco_marketplace": preco_pub,
                                "promo_marketplace": promo_pub,
                                "aprovado_em": preco_shinsei.get("aprovado_em"),
                                "status": "pendente",
                                "criado_em": datetime.utcnow().isoformat(),
                                "atualizado_em": None,
                                "resultado_correcao": None,
                            })
                            novos_preco += 1
                            logger.info("Auditoria preço: SKU=%s Shinsei=%.2f ML=%.2f", sku, preco_virt, preco_pub)

        except Exception as e:
            erros += 1
            logger.warning("Auditoria: erro no produto %s: %s", prod_raw.get("id"), e)

    salvar_fila_auditoria(fila)
    logger.info("Auditoria concluída: %d verificados, %d estoque, %d preço, %d erros",
                verificados, novos_estoque, novos_preco, erros)

    return {
        "ok": True,
        "verificados": verificados,
        "novas_divergencias_estoque": novos_estoque,
        "novas_divergencias_preco": novos_preco,
        "erros": erros,
        "total_pendente": sum(1 for i in fila if i.get("status") == "pendente"),
    }


def corrigir_estoque(item_id_fila: str, ml_service=None) -> dict:
    fila = carregar_fila_auditoria()
    item = next((i for i in fila if i.get("id") == item_id_fila), None)
    if not item:
        return {"ok": False, "erro": "Item não encontrado."}
    if item.get("status") != "pendente":
        return {"ok": False, "erro": f"Item já com status '{item.get('status')}'."}

    item_id_mp = item.get("item_id_marketplace", "")
    estoque_bling = int(item.get("valor_shinsei", 0))
    resultado = {"ok": False, "erro": "Serviço ML não disponível."}

    if ml_service:
        try:
            import requests
            res = requests.put(
                f"https://api.mercadolibre.com/items/{item_id_mp}",
                json={"available_quantity": estoque_bling},
                headers=ml_service._headers(),
                timeout=30,
            )
            resultado = {"ok": res.status_code in [200, 201], "canal": "Mercado Livre"}
            if not resultado["ok"]:
                resultado["erro"] = res.text
        except Exception as e:
            resultado = {"ok": False, "erro": str(e)}

    item["status"] = "corrigido" if resultado.get("ok") else "pendente"
    item["atualizado_em"] = datetime.utcnow().isoformat()
    item["resultado_correcao"] = resultado
    salvar_fila_auditoria(fila)
    return resultado


def corrigir_preco(item_id_fila: str, ml_service=None) -> dict:
    fila = carregar_fila_auditoria()
    item = next((i for i in fila if i.get("id") == item_id_fila), None)
    if not item:
        return {"ok": False, "erro": "Item não encontrado."}
    if item.get("status") != "pendente":
        return {"ok": False, "erro": f"Item já com status '{item.get('status')}'."}

    item_id_mp = item.get("item_id_marketplace", "")
    preco_virt = float(item.get("preco_shinsei_virtual", 0))
    preco_calc = float(item.get("preco_shinsei_calculado", 0))
    resultado = {"ok": False, "erro": "Serviço ML não disponível."}

    if ml_service and preco_virt > 0:
        try:
            import requests
            payload = {"price": preco_virt}
            if preco_calc > 0:
                payload["sale_price"] = {
                    "amount": preco_calc,
                    "currency_id": "BRL",
                    "type": "discounted"
                }
            res = requests.put(
                f"https://api.mercadolibre.com/items/{item_id_mp}",
                json=payload,
                headers=ml_service._headers(),
                timeout=30,
            )
            resultado = {"ok": res.status_code in [200, 201], "canal": "Mercado Livre"}
            if not resultado["ok"]:
                resultado["erro"] = res.text
        except Exception as e:
            resultado = {"ok": False, "erro": str(e)}

    item["status"] = "corrigido" if resultado.get("ok") else "pendente"
    item["atualizado_em"] = datetime.utcnow().isoformat()
    item["resultado_correcao"] = resultado
    salvar_fila_auditoria(fila)
    return resultado


def ignorar_item(item_id_fila: str) -> dict:
    fila = carregar_fila_auditoria()
    item = next((i for i in fila if i.get("id") == item_id_fila), None)
    if not item:
        return {"ok": False, "erro": "Item não encontrado."}
    item["status"] = "ignorado"
    item["atualizado_em"] = datetime.utcnow().isoformat()
    salvar_fila_auditoria(fila)
    return {"ok": True}


def limpar_resolvidos() -> int:
    fila = carregar_fila_auditoria()
    antes = len(fila)
    fila = [i for i in fila if i.get("status") == "pendente"]
    salvar_fila_auditoria(fila)
    return antes - len(fila)
