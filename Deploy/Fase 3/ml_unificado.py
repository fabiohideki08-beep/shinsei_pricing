"""
routes/ml_unificado.py — Shinsei Pricing
Integração Mercado Livre unificada com o motor principal de precificação.

Substitui a lógica fragmentada entre app_ml_integrado.py e routes/mercado_livre.py,
conectando o motor pricing_engine_real diretamente à atualização de preços no ML.

Registre no app.py:
    from routes.ml_unificado import router as ml_router
    app.include_router(ml_router)

Endpoints adicionados:
    POST /ml/precificar-e-aplicar   — calcula via motor e aplica no ML em um passo
    POST /ml/aplicar-fila/{item_id} — aplica um item aprovado da fila no ML
    GET  /ml/status-completo        — status de autenticação + última sincronização
"""

from __future__ import annotations

import importlib
import logging
import time
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()


# ─────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────

class PrecificarMLPayload(BaseModel):
    sku: str
    item_id_classico: Optional[str] = None    # ID do anúncio Clássico no ML
    item_id_premium: Optional[str] = None     # ID do anúncio Premium no ML
    embalagem: float = 0.0
    imposto: float = 4.0
    objetivo: str = "lucro_liquido"
    tipo_alvo: str = "percentual"
    valor_alvo: float = 30.0
    modo_preco_virtual: str = "percentual_acima"
    acrescimo_percentual: float = 20.0
    arredondamento: str = "90"
    aplicar_imediatamente: bool = False  # False = só enfileira, True = aplica direto


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _get_motor():
    for modname in ("pricing_engine_real", "pricing_engine"):
        try:
            mod = importlib.import_module(modname)
            fn = getattr(mod, "montar_precificacao_bling", None)
            if fn:
                return fn
        except Exception:
            continue
    return None


def _get_ml_service():
    """Retorna uma instância de MercadoLivreService com token atual."""
    try:
        from pathlib import Path
        from services.mercado_livre import MercadoLivreOAuthService, MercadoLivreService
        oauth = MercadoLivreOAuthService(base_dir=Path("."))
        tokens = oauth.ler_tokens()
        if not tokens.get("success"):
            raise RuntimeError("ML não autenticado. Acesse /ml/login.")
        return MercadoLivreService(tokens["data"]["access_token"])
    except ImportError:
        raise HTTPException(status_code=500, detail="services/mercado_livre.py não encontrado.")


def _get_db():
    try:
        return importlib.import_module("database")
    except Exception:
        return None


def _carregar_regras() -> list:
    db = _get_db()
    if db:
        return db.listar_regras(apenas_ativas=True)
    import json
    from pathlib import Path
    p = Path("data/regras.json")
    if p.exists():
        regras = json.loads(p.read_text(encoding="utf-8"))
        return [r for r in regras if isinstance(r, dict) and r.get("ativo", True)]
    return []


def _carregar_cfg() -> dict:
    db = _get_db()
    if db:
        return db.get_config("app_config") or {}
    import json
    from pathlib import Path
    p = Path("data/config.json")
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def _extrair_preco_ml(marketplaces: dict) -> tuple[float, float]:
    """
    Extrai preço_virtual (de tabela) e preco_promocional dos canais ML.
    Retorna (preco_virtual, preco_promocional) do melhor canal ML disponível.
    """
    for key in ("mercado_livre_classico", "mercado_livre_premium"):
        canal = marketplaces.get(key, {})
        preco = float(canal.get("preco") or 0)
        promo = float(canal.get("preco_promocional") or 0)
        if preco > 0 or promo > 0:
            return preco, promo
    return 0.0, 0.0


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────

@router.post("/ml/precificar-e-aplicar")
def ml_precificar_e_aplicar(payload: PrecificarMLPayload):
    """
    Calcula o preço via motor e, opcionalmente, aplica direto no(s) anúncio(s) do ML.

    Se aplicar_imediatamente=False (padrão), o resultado vai para a fila de aprovação.
    Se aplicar_imediatamente=True, atualiza o anúncio sem passar pela fila.
    """
    montar = _get_motor()
    if not montar:
        raise HTTPException(status_code=500, detail="Motor de precificação não encontrado.")

    regras = _carregar_regras()
    if not regras:
        raise HTTPException(status_code=400, detail="Nenhuma regra ativa. Importe a Aba2 primeiro.")

    cfg = _carregar_cfg()
    db = _get_db()

    # 1. Calcula via motor
    try:
        resultado = montar(
            regras=regras,
            criterio="sku",
            valor_busca=payload.sku,
            embalagem=payload.embalagem,
            imposto=payload.imposto,
            quantidade=1,
            objetivo=payload.objetivo,
            tipo_alvo=payload.tipo_alvo,
            valor_alvo=payload.valor_alvo,
            peso_override=0,
            intelligence_config={},
            modo_aprovacao="manual",
            modo_preco_virtual=payload.modo_preco_virtual,
            acrescimo_percentual=payload.acrescimo_percentual,
            acrescimo_nominal=0,
            preco_manual=0,
            arredondamento=payload.arredondamento,
            regra_estoque=cfg.get("regra_estoque"),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erro no motor: {e}")

    if resultado.get("erro"):
        raise HTTPException(status_code=422, detail=resultado["erro"])

    # 2. Extrai preços ML do resultado
    itens = (
        (resultado.get("integracao") or {}).get("itens")
        or resultado.get("itens_precificacao")
        or resultado.get("itens")
        or []
    )

    # Normaliza marketplaces para formato padrão
    try:
        app_mod = importlib.import_module("app")
        marketplaces = app_mod._normalizar_marketplaces(itens)
    except Exception:
        marketplaces = {}

    preco_virtual, preco_promocional = _extrair_preco_ml(marketplaces)
    if preco_virtual <= 0 and preco_promocional <= 0:
        raise HTTPException(
            status_code=422,
            detail="Não foi possível extrair preço para o canal Mercado Livre do resultado do motor."
        )

    preco_aplicar = preco_promocional if preco_promocional > 0 else preco_virtual

    resposta = {
        "ok": True,
        "sku": payload.sku,
        "produto": resultado.get("produto_bling", {}),
        "melhor_canal": resultado.get("melhor_canal", ""),
        "marketplaces": marketplaces,
        "preco_ml_calculado": preco_aplicar,
        "aplicado": False,
        "atualizacoes_ml": [],
        "item_fila_id": None,
    }

    # 3a. Aplica imediatamente no ML
    if payload.aplicar_imediatamente:
        if not payload.item_id_classico and not payload.item_id_premium:
            raise HTTPException(
                status_code=400,
                detail="Para aplicar imediatamente, informe item_id_classico e/ou item_id_premium."
            )
        try:
            ml = _get_ml_service()
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

        atualizacoes = []
        if payload.item_id_classico:
            r = ml.atualizar_com_retry(payload.item_id_classico, preco_aplicar)
            atualizacoes.append({"tipo": "classico", "item_id": payload.item_id_classico, "resultado": r})

        if payload.item_id_premium:
            # Premium tem o mesmo preço ou usa o preco_virtual
            preco_premium = preco_virtual if preco_virtual > preco_aplicar else preco_aplicar
            r = ml.atualizar_com_retry(payload.item_id_premium, preco_premium)
            atualizacoes.append({"tipo": "premium", "item_id": payload.item_id_premium, "resultado": r})

        resposta["aplicado"] = all(a["resultado"].get("success") for a in atualizacoes)
        resposta["atualizacoes_ml"] = atualizacoes
        logger.info("ML precificar-e-aplicar: SKU=%s preço=%.2f aplicado=%s",
                    payload.sku, preco_aplicar, resposta["aplicado"])

    # 3b. Enfileira para aprovação manual
    else:
        if db and not db.ja_existe_pendente(payload.sku):
            agora = datetime.now().isoformat()
            item_fila = {
                "id": str(uuid.uuid4()),
                "status": "pendente",
                "sku": payload.sku,
                "nome": resultado.get("produto_bling", {}).get("nome", ""),
                "criado_em": agora,
                "atualizado_em": agora,
                "marketplaces": marketplaces,
                "auditoria": resultado.get("auditoria") or {},
                "payload_original": {
                    **payload.dict(),
                    "item_id_classico": payload.item_id_classico,
                    "item_id_premium": payload.item_id_premium,
                    "origem": "ml_unificado",
                },
                "historico_decisao": [],
                "resultado_aplicacao": None,
            }
            db.inserir_item_fila(item_fila)
            resposta["item_fila_id"] = item_fila["id"]
            logger.info("ML: SKU=%s enfileirado id=%s", payload.sku, item_fila["id"])

    return resposta


@router.post("/ml/aplicar-fila/{item_id}")
def ml_aplicar_fila(item_id: str):
    """
    Aplica um item já aprovado na fila diretamente no ML.
    O item precisa ter item_id_classico e/ou item_id_premium no payload_original.
    """
    db = _get_db()
    if not db:
        raise HTTPException(status_code=500, detail="database.py não disponível.")

    item = db.buscar_item_fila(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado na fila.")

    payload_orig = item.get("payload_original") or {}
    item_id_classico = payload_orig.get("item_id_classico")
    item_id_premium = payload_orig.get("item_id_premium")

    if not item_id_classico and not item_id_premium:
        raise HTTPException(
            status_code=400,
            detail="Item sem item_id_classico nem item_id_premium no payload. "
                   "Use /precificar-ml diretamente informando os IDs."
        )

    # Extrai preço dos marketplaces salvos
    marketplaces = item.get("marketplaces") or {}
    preco_virtual, preco_promocional = _extrair_preco_ml(marketplaces)
    preco_aplicar = preco_promocional if preco_promocional > 0 else preco_virtual

    if preco_aplicar <= 0:
        raise HTTPException(status_code=422, detail="Preço ML inválido ou zerado no item da fila.")

    try:
        ml = _get_ml_service()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    atualizacoes = []
    if item_id_classico:
        r = ml.atualizar_com_retry(item_id_classico, preco_aplicar)
        atualizacoes.append({"tipo": "classico", "item_id": item_id_classico, "resultado": r})

    if item_id_premium:
        preco_premium = preco_virtual if preco_virtual > preco_aplicar else preco_aplicar
        r = ml.atualizar_com_retry(item_id_premium, preco_premium)
        atualizacoes.append({"tipo": "premium", "item_id": item_id_premium, "resultado": r})

    tudo_ok = all(a["resultado"].get("success") for a in atualizacoes)
    novo_status = "aprovado" if tudo_ok else "erro_aplicacao"

    db.atualizar_status_fila(item_id, novo_status, resultado={
        "atualizacoes_ml": atualizacoes,
        "aplicado_em": datetime.now().isoformat(),
    })

    logger.info("ML aplicar-fila: item_id=%s sku=%s status=%s", item_id, item.get("sku"), novo_status)

    return {
        "ok": tudo_ok,
        "item_id": item_id,
        "sku": item.get("sku"),
        "preco_aplicado": preco_aplicar,
        "atualizacoes": atualizacoes,
        "status_fila": novo_status,
    }


@router.get("/ml/status-completo")
def ml_status_completo():
    """
    Retorna o status de autenticação ML + estatísticas da fila
    relacionadas a itens do Mercado Livre.
    """
    try:
        from pathlib import Path
        from services.mercado_livre import MercadoLivreOAuthService
        oauth = MercadoLivreOAuthService(base_dir=Path("."))
        status_auth = oauth.status()
    except Exception as e:
        status_auth = {"conectado": False, "erro": str(e)}

    db = _get_db()
    stats = db.stats_fila() if db else {}

    return {
        "autenticacao": status_auth,
        "fila": stats,
        "timestamp": datetime.now().isoformat(),
    }
