"""
routes/batch.py — Shinsei Pricing
Endpoint de precificação em lote.

POST /bling/precificar-lote
    Recebe uma lista de SKUs e retorna todos calculados de uma vez,
    sem fazer N chamadas individuais ao /integracao/preview.

Uso:
    No app.py, registre o router:
        from routes.batch import router as batch_router
        app.include_router(batch_router)

Payload de entrada:
    {
        "skus": ["7897042019328", "7896007816344", ...],
        "embalagem": 1.0,
        "imposto": 4.0,
        "objetivo": "lucro_liquido",
        "tipo_alvo": "percentual",
        "valor_alvo": 30.0,
        "modo_aprovacao": "manual",
        "modo_preco_virtual": "percentual_acima",
        "acrescimo_percentual": 20.0,
        "arredondamento": "90",
        "enfileirar": true   // adiciona automaticamente na fila de aprovação
    }

Resposta:
    {
        "total": 3,
        "sucesso": 2,
        "erro": 1,
        "resultados": [
            {"sku": "...", "ok": true, "melhor_canal": "Shopee", "marketplaces": {...}},
            {"sku": "...", "ok": false, "erro": "Produto sem custo no Bling."},
        ],
        "enfileirados": 2,
        "duracao_segundos": 4.2
    }
"""

from __future__ import annotations

import importlib
import logging
import time
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()

# ─────────────────────────────────────────────
# Schema de entrada
# ─────────────────────────────────────────────

class BatchPayload(BaseModel):
    skus: list[str] = Field(..., min_items=1, max_items=200,
                             description="Lista de SKUs para precificar (máx. 200)")
    embalagem: float = 0.0
    imposto: float = 4.0
    objetivo: str = "lucro_liquido"
    tipo_alvo: str = "percentual"
    valor_alvo: float = 30.0
    peso_override: float = 0.0
    modo_aprovacao: str = "manual"
    modo_preco_virtual: str = "percentual_acima"
    acrescimo_percentual: float = 20.0
    acrescimo_nominal: float = 0.0
    preco_manual: float = 0.0
    arredondamento: str = "90"
    enfileirar: bool = True
    ignorar_ja_pendentes: bool = True   # pula SKUs que já têm pendente na fila


# ─────────────────────────────────────────────
# Helpers — imports opcionais (mesmo padrão do app.py)
# ─────────────────────────────────────────────

def _get_motor():
    mod = importlib.import_module("pricing_engine_real") if True else None
    try:
        mod = importlib.import_module("pricing_engine_real")
    except Exception:
        try:
            mod = importlib.import_module("pricing_engine")
        except Exception:
            return None
    return getattr(mod, "montar_precificacao_bling", None)


def _get_db():
    try:
        return importlib.import_module("database")
    except Exception:
        return None


def _normalizar_marketplaces(itens: list) -> dict:
    """Reutiliza a lógica do app.py sem duplicar código."""
    try:
        app_mod = importlib.import_module("app")
        return app_mod._normalizar_marketplaces(itens)
    except Exception:
        # Fallback simples
        resultado = {}
        for item in (itens or []):
            canal = item.get("canal", "")
            if canal:
                resultado[canal.lower().replace(" ", "_")] = {
                    "label": canal,
                    "preco": item.get("preco_virtual") or item.get("preco_final") or 0,
                    "preco_promocional": item.get("preco_promocional") or 0,
                    "lucro": item.get("lucro_liquido") or item.get("lucro") or 0,
                    "margem": item.get("margem") or 0,
                }
        return resultado


def _carregar_regras():
    db = _get_db()
    if db:
        return db.listar_regras(apenas_ativas=True)
    # Fallback JSON
    import json
    from pathlib import Path
    regras_path = Path("data/regras.json")
    if regras_path.exists():
        regras = json.loads(regras_path.read_text(encoding="utf-8"))
        return [r for r in regras if isinstance(r, dict) and r.get("ativo", True)]
    return []


def _carregar_cfg() -> dict:
    db = _get_db()
    if db:
        return db.get_config("app_config") or {}
    import json
    from pathlib import Path
    cfg_path = Path("data/config.json")
    if cfg_path.exists():
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    return {}


def _enfileirar(db, sku: str, preview: dict, payload: BatchPayload) -> bool:
    """Insere o resultado na fila de aprovação. Retorna True se enfileirado."""
    if db is None:
        return False
    if payload.ignorar_ja_pendentes and db.ja_existe_pendente(sku):
        logger.debug("Batch: SKU %s já pendente — ignorado", sku)
        return False

    agora = datetime.now().isoformat()
    item = {
        "id": str(uuid.uuid4()),
        "status": "pendente",
        "sku": sku,
        "nome": preview.get("produto", {}).get("nome", ""),
        "criado_em": agora,
        "atualizado_em": agora,
        "marketplaces": preview.get("marketplaces", {}),
        "auditoria": preview.get("auditoria", {}),
        "payload_original": {
            **payload.dict(),
            "origem": "batch",
            "criterio": "sku",
            "valor_busca": sku,
        },
        "historico_decisao": [],
        "resultado_aplicacao": None,
    }
    try:
        db.inserir_item_fila(item)
        return True
    except Exception as e:
        logger.error("Batch: erro ao enfileirar SKU %s: %s", sku, e)
        return False


# ─────────────────────────────────────────────
# Endpoint principal
# ─────────────────────────────────────────────

@router.post("/bling/precificar-lote")
def precificar_lote(payload: BatchPayload):
    """
    Precifica uma lista de SKUs de uma vez.
    Cada SKU é calculado independentemente — falhas individuais não param o lote.
    """
    inicio = time.time()

    montar = _get_motor()
    if not montar:
        raise HTTPException(status_code=500, detail="pricing_engine_real.py não encontrado.")

    regras = _carregar_regras()
    if not regras:
        raise HTTPException(status_code=400, detail="Nenhuma regra ativa. Importe a Aba2 primeiro.")

    cfg = _carregar_cfg()
    db = _get_db()
    regra_estoque = cfg.get("regra_estoque")

    # Deduplica SKUs mantendo a ordem
    skus_unicos = list(dict.fromkeys(s.strip() for s in payload.skus if s.strip()))

    resultados = []
    enfileirados = 0
    sucessos = 0
    erros = 0

    for sku in skus_unicos:
        resultado_item: dict = {"sku": sku}

        try:
            resultado = montar(
                regras=regras,
                criterio="sku",
                valor_busca=sku,
                embalagem=payload.embalagem,
                imposto=payload.imposto,
                quantidade=1,
                objetivo=payload.objetivo,
                tipo_alvo=payload.tipo_alvo,
                valor_alvo=payload.valor_alvo,
                peso_override=payload.peso_override,
                intelligence_config={},
                modo_aprovacao=payload.modo_aprovacao,
                modo_preco_virtual=payload.modo_preco_virtual,
                acrescimo_percentual=payload.acrescimo_percentual,
                acrescimo_nominal=payload.acrescimo_nominal,
                preco_manual=payload.preco_manual,
                arredondamento=payload.arredondamento,
                regra_estoque=regra_estoque,
            )

            if resultado.get("erro"):
                resultado_item["ok"] = False
                resultado_item["erro"] = str(resultado.get("erro"))
                erros += 1
                resultados.append(resultado_item)
                continue

            # Monta preview resumido (mesmo formato do /integracao/preview)
            itens = (
                (resultado.get("integracao") or {}).get("itens")
                or resultado.get("itens_precificacao")
                or resultado.get("itens")
                or []
            )
            marketplaces = _normalizar_marketplaces(itens or resultado.get("canais", []))

            preview = {
                "ok": True,
                "produto": resultado.get("produto_bling") or {},
                "melhor_canal": resultado.get("melhor_canal") or "",
                "marketplaces": marketplaces,
                "auditoria": resultado.get("auditoria") or {},
            }

            resultado_item["ok"] = True
            resultado_item["nome"] = preview["produto"].get("nome", "")
            resultado_item["melhor_canal"] = preview["melhor_canal"]
            resultado_item["marketplaces"] = marketplaces
            resultado_item["auditoria"] = {
                "custo_usado": preview["auditoria"].get("custo_usado"),
                "peso_usado": preview["auditoria"].get("peso_usado"),
                "tipo_custo": preview["auditoria"].get("tipo_custo"),
            }
            sucessos += 1

            # Enfileira se solicitado
            if payload.enfileirar and marketplaces:
                enfileirou = _enfileirar(db, sku, preview, payload)
                resultado_item["enfileirado"] = enfileirou
                if enfileirou:
                    enfileirados += 1

        except Exception as e:
            logger.error("Batch: erro inesperado SKU %s: %s", sku, e, exc_info=True)
            resultado_item["ok"] = False
            resultado_item["erro"] = f"Erro interno: {str(e)}"
            erros += 1

        resultados.append(resultado_item)

    duracao = round(time.time() - inicio, 2)
    logger.info(
        "Batch concluído: %d SKUs, %d OK, %d erros, %d enfileirados, %.1fs",
        len(skus_unicos), sucessos, erros, enfileirados, duracao,
    )

    return {
        "total": len(skus_unicos),
        "sucesso": sucessos,
        "erro": erros,
        "enfileirados": enfileirados,
        "duracao_segundos": duracao,
        "resultados": resultados,
    }


# ─────────────────────────────────────────────
# Endpoint de status do lote (resumo da fila)
# ─────────────────────────────────────────────

@router.get("/bling/precificar-lote/status")
def batch_status():
    """Retorna um resumo da fila e das últimas precificações em lote."""
    db = _get_db()
    if not db:
        return {"db": "indisponível"}
    return {
        "fila": db.stats_fila(),
        "timestamp": datetime.now().isoformat(),
    }
