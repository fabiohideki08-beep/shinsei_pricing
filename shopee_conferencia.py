# -*- coding: utf-8 -*-
"""
shopee_conferencia.py — Shinsei Pricing
Módulo de conferência de estoque entre Bling e Shopee.

Estratégia:
- Usa shopee_mapeamento.json (sku → item_id) como fonte de verdade
- Para cada SKU mapeado, busca estoque no Bling e na Shopee
- Registra divergências em fila_shopee.json para revisão/correção
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
DATA_DIR = Path(__file__).parent / "data"
FILA_PATH = DATA_DIR / "fila_shopee.json"
MAPEAMENTO_PATH = DATA_DIR / "shopee_mapeamento.json"


# ── Helpers ───────────────────────────────────────────────────────────────────

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


# ── Fila ──────────────────────────────────────────────────────────────────────

def carregar_fila() -> list[dict]:
    itens = _load_json(FILA_PATH, [])
    return itens if isinstance(itens, list) else []


def salvar_fila(itens: list[dict]) -> None:
    _save_json(FILA_PATH, itens)


def stats_fila() -> dict:
    itens = carregar_fila()
    stats = {"pendente": 0, "corrigido": 0, "ignorado": 0}
    for item in itens:
        s = str(item.get("status", "pendente")).lower()
        if s in stats:
            stats[s] += 1
    stats["total"] = len(itens)
    return stats


# ── Conferência principal ─────────────────────────────────────────────────────

def conferir_shopee(bling_client, max_items: int = 200) -> dict:
    """
    Para cada SKU em shopee_mapeamento.json:
      1. Busca estoque atual no Bling
      2. Busca estoque atual na Shopee via get_model_list
      3. Registra divergência na fila se diferente
    """
    try:
        from services.shopee import ShopeeService
        svc = ShopeeService()
    except RuntimeError as exc:
        return {"ok": False, "erro": f"Shopee não autenticada: {exc}"}

    mapeamento = _load_json(MAPEAMENTO_PATH, {})
    if not mapeamento:
        return {
            "ok": False,
            "erro": "Nenhum mapeamento SKU → item_id encontrado. "
                    "Configure em /shopee e use o auto-mapeamento.",
        }

    fila = carregar_fila()
    chaves_pendentes = {
        (i.get("sku"), i.get("tipo"))
        for i in fila
        if i.get("status") == "pendente"
    }

    verificados = 0
    divergencias = 0
    erros = 0
    skus = list(mapeamento.items())[:max_items]

    for sku, item_id_str in skus:
        try:
            item_id = int(item_id_str)

            # 1. Estoque Shopee
            time.sleep(0.3)
            estoque_shopee = svc.obter_estoque_item(item_id)
            if estoque_shopee < 0:
                logger.warning("Shopee conferencia: erro ao obter estoque item_id=%s", item_id)
                erros += 1
                continue

            # 2. Estoque Bling
            time.sleep(0.4)
            busca = bling_client.get_product_by_sku(sku)
            if not busca.get("encontrado"):
                logger.debug("Shopee conferencia: SKU %s não encontrado no Bling", sku)
                erros += 1
                continue

            prod = busca.get("produto", {})
            estoque_bling = int(
                (prod.get("estoque") or {}).get("saldoVirtualTotal") or 0
            )
            nome = prod.get("nome", "")[:60]
            verificados += 1

            # 3. Verifica divergência
            if estoque_bling != estoque_shopee:
                divergencias += 1
                chave = (sku, "estoque")
                if chave not in chaves_pendentes:
                    fila.append({
                        "id": f"shpe_est_{sku}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
                        "sku": sku,
                        "item_id_shopee": str(item_id),
                        "titulo": nome,
                        "tipo": "estoque",
                        "estoque_bling": estoque_bling,
                        "estoque_shopee": estoque_shopee,
                        "diferenca": estoque_bling - estoque_shopee,
                        "status": "pendente",
                        "criado_em": datetime.utcnow().isoformat(),
                        "atualizado_em": None,
                    })
                    chaves_pendentes.add(chave)
                    logger.info(
                        "Divergência Shopee: sku=%s item_id=%s bling=%d shopee=%d",
                        sku, item_id, estoque_bling, estoque_shopee,
                    )

        except Exception as exc:
            erros += 1
            logger.warning("Erro ao conferir SKU %s (Shopee): %s", sku, exc)

    salvar_fila(fila)
    logger.info(
        "Conferência Shopee: %d verificados, %d divergências, %d erros",
        verificados, divergencias, erros,
    )
    return {
        "ok": True,
        "verificados": verificados,
        "divergencias": divergencias,
        "erros": erros,
        "total_pendentes": len([i for i in fila if i.get("status") == "pendente"]),
    }


# ── Corrigir / Ignorar ────────────────────────────────────────────────────────

def corrigir_shopee(item_id_fila: str) -> dict:
    """Atualiza estoque na Shopee com o valor do Bling."""
    fila = carregar_fila()
    item = next((i for i in fila if i.get("id") == item_id_fila), None)
    if not item:
        return {"ok": False, "erro": "Item não encontrado."}
    if item.get("status") != "pendente":
        return {"ok": False, "erro": "Item já processado."}
    if item.get("tipo") != "estoque":
        return {"ok": False, "erro": "Tipo de divergência não suportado."}

    try:
        from services.shopee import ShopeeService
        svc = ShopeeService()
        item_id = int(item.get("item_id_shopee", 0))
        estoque_bling = int(item.get("estoque_bling", 0))

        resultado = svc.atualizar_estoque(item_id, estoque_bling)
    except Exception as exc:
        logger.error("Erro ao corrigir Shopee %s: %s", item_id_fila, exc)
        return {"ok": False, "erro": str(exc)}

    if resultado.get("success"):
        item["status"] = "corrigido"
    item["atualizado_em"] = datetime.utcnow().isoformat()
    item["resultado"] = resultado
    salvar_fila(fila)
    return {"ok": resultado.get("success", False), "resultado": resultado}


def ignorar_shopee(item_id_fila: str) -> dict:
    fila = carregar_fila()
    item = next((i for i in fila if i.get("id") == item_id_fila), None)
    if not item:
        return {"ok": False}
    item["status"] = "ignorado"
    item["atualizado_em"] = datetime.utcnow().isoformat()
    salvar_fila(fila)
    return {"ok": True}
