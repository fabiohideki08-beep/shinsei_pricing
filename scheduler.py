"""
scheduler.py — Shinsei Pricing
Agendador de atualização automática de preços.

Substitui a versão legada que importava bling_service e pricing_engine
(módulos que não existem mais no projeto).

Pode ser executado de duas formas:

1. Integrado ao FastAPI (recomendado):
   No app.py, adicione no evento de startup:
       from scheduler import iniciar_scheduler_background
       iniciar_scheduler_background()

2. Processo separado:
   python scheduler.py

Variáveis de ambiente relevantes:
   SCHEDULER_INTERVALO   — segundos entre ciclos (padrão: 300)
   SCHEDULER_ATIVO       — "false" para desativar sem remover o código
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent

# ─────────────────────────────────────────────
# Configuração
# ─────────────────────────────────────────────

def _intervalo() -> int:
    try:
        return int(os.getenv("SCHEDULER_INTERVALO", "300"))
    except ValueError:
        return 300


def _scheduler_ativo() -> bool:
    return os.getenv("SCHEDULER_ATIVO", "true").strip().lower() != "false"


# ─────────────────────────────────────────────
# Imports opcionais (mesmo padrão do app.py)
# ─────────────────────────────────────────────

def _optional_import(module_name: str):
    try:
        return importlib.import_module(module_name)
    except Exception:
        return None


# ─────────────────────────────────────────────
# Lógica principal de atualização
# ─────────────────────────────────────────────

def _ciclo_atualizacao() -> dict:
    """
    Executa um ciclo completo:
    1. Busca todos os produtos no Bling
    2. Para cada produto com custo, calcula preços via pricing_engine_real
    3. Envia à fila de aprovação (modo manual) ou aplica diretamente (modo auto)

    Retorna um resumo do ciclo.
    """
    resumo = {
        "inicio": datetime.now().isoformat(),
        "produtos_buscados": 0,
        "calculados": 0,
        "erros": 0,
        "ignorados": 0,
    }

    # Carrega módulos dinamicamente (mesma estratégia do app.py)
    bling_mod = _optional_import("bling_client")
    pricing_mod = _optional_import("pricing_engine_real") or _optional_import("pricing_engine")

    if not bling_mod:
        logger.error("bling_client.py não encontrado — ciclo abortado.")
        return resumo

    if not pricing_mod:
        logger.error("pricing_engine_real.py não encontrado — ciclo abortado.")
        return resumo

    BlingClient = getattr(bling_mod, "BlingClient", None)
    montar_precificacao = getattr(pricing_mod, "montar_precificacao_bling", None)

    if not BlingClient or not montar_precificacao:
        logger.error("Funções necessárias não encontradas nos módulos.")
        return resumo

    # Carrega regras e config via database (com fallback para JSON legado)
    try:
        db_mod = _optional_import("database")
        if db_mod:
            regras = db_mod.listar_regras(apenas_ativas=True)
            cfg_raw = db_mod.get_config("app_config") or {}
        else:
            # Fallback para JSON legado se database.py não estiver disponível
            import json
            regras_path = BASE_DIR / "data" / "regras.json"
            cfg_path = BASE_DIR / "data" / "config.json"
            regras = json.loads(regras_path.read_text(encoding="utf-8")) if regras_path.exists() else []
            cfg_raw = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    except Exception as e:
        logger.error("Erro ao carregar regras: %s", e)
        return resumo

    if not regras:
        logger.warning("Nenhuma regra ativa encontrada — ciclo ignorado.")
        resumo["ignorados"] = -1
        return resumo

    # Modo de aprovação: se for "auto", aplica direto; senão, só enfileira
    modo_aprovacao = cfg_raw.get("modo_aprovacao", "manual")

    try:
        client = BlingClient()
        if not client.has_local_tokens():
            logger.warning("Bling não autenticado — ciclo abortado. Acesse /bling/auth.")
            return resumo
    except Exception as e:
        logger.error("Erro ao instanciar BlingClient: %s", e)
        return resumo

    # Busca produtos com estoque
    try:
        produtos_response = client.list_products(page=1, limit=100)
        produtos = produtos_response.get("data", []) if isinstance(produtos_response, dict) else produtos_response
        resumo["produtos_buscados"] = len(produtos)
        logger.info("Scheduler: %d produtos buscados do Bling", len(produtos))
    except Exception as e:
        logger.error("Erro ao buscar produtos do Bling: %s", e)
        return resumo

    for item in produtos:
        produto = item if isinstance(item, dict) else {}
        sku = produto.get("codigo") or produto.get("sku") or ""
        if not sku:
            logger.debug("Produto id=%s ignorado: sem codigo/SKU", produto.get("id"))
            resumo["ignorados"] += 1
            continue

        # Alerta de estoque negativo no Bling
        estoque_virtual = int((produto.get("estoque") or {}).get("saldoVirtualTotal") or 0)
        if estoque_virtual < 0:
            logger.warning("ESTOQUE NEGATIVO: SKU %s estoque=%d", sku, estoque_virtual)
            try:
                import json as _json
                from datetime import datetime as _dt
                _fila_neg = BASE_DIR / "data" / "fila_estoque_negativo.json"
                _itens = _json.loads(_fila_neg.read_text(encoding="utf-8")) if _fila_neg.exists() else []
                _skus_existentes = {i.get("sku") for i in _itens if i.get("status") == "pendente"}
                if sku not in _skus_existentes:
                    _itens.append({
                        "id": f"neg_{sku}_{_dt.now().strftime('%Y%m%d%H%M%S')}",
                        "sku": sku,
                        "nome": produto.get("nome", ""),
                        "estoque": estoque_virtual,
                        "detectado_em": _dt.now().isoformat(),
                        "status": "pendente"
                    })
                    _fila_neg.write_text(_json.dumps(_itens, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as _e:
                logger.debug("Erro ao salvar alerta estoque negativo: %s", _e)

      
        custo = float((produto.get("fornecedor") or {}).get("precoCusto") or (produto.get("fornecedor") or {}).get("precoCompra") or produto.get("precoCusto") or produto.get("preco_custo") or 0)
        if custo <= 0:
            logger.debug("SKU %s ignorado: sem custo no Bling", sku)
            resumo["ignorados"] += 1
            continue

        # Configura API ML em tempo real se habilitado
        if cfg_raw.get('ml_api_real', False):
            try:
                from pricing_engine_real import configurar_ml_api
                # Busca peso do produto do Bling
                _peso_g = int(float(produto.get('pesoBruto') or produto.get('pesoLiquido') or 0.3) * 1000)
                _dim = produto.get('dimensoes') or {}
                _vol_g = int((_dim.get('largura',10) * _dim.get('altura',5) * _dim.get('profundidade',10)) / 6 )
                _peso_fat = max(_peso_g, _vol_g)
                configurar_ml_api(True, _peso_fat, "")
            except Exception as _e:
                logger.debug("Erro ao configurar ML API: %s", _e)
        else:
            try:
                from pricing_engine_real import configurar_ml_api
                configurar_ml_api(False)
            except Exception:
                pass

        try:
            resultado = montar_precificacao(
                regras=regras,
                criterio="sku",
                valor_busca=sku,
                embalagem=float(cfg_raw.get('embalagem_padrao', 0)),
                imposto=float(cfg_raw.get('imposto_padrao', 4.0)),
                quantidade=1,
                objetivo="lucro_liquido",
                tipo_alvo="percentual",
                valor_alvo=cfg_raw.get("valor_alvo_padrao", 30.0),
                peso_override=0,
                intelligence_config={},
                modo_aprovacao=modo_aprovacao,
                regra_estoque=cfg_raw.get("regra_estoque"),
            )

            if resultado.get("erro"):
                logger.warning("SKU %s: erro no motor — %s", sku, resultado["erro"])
                resumo["erros"] += 1
                continue

            # Enfileira ou aplica conforme o modo
            if db_mod:
                _enfileirar_resultado(db_mod, resultado, sku, modo_aprovacao)
            resumo["calculados"] += 1

        except Exception as e:
            logger.error("Erro ao calcular SKU %s: %s", sku, e)
            resumo["erros"] += 1

    resumo["fim"] = datetime.now().isoformat()
    logger.info(
        "Ciclo concluído: %d calculados, %d ignorados, %d erros",
        resumo["calculados"], resumo["ignorados"], resumo["erros"]
    )
    return resumo


def _enfileirar_resultado(db_mod, resultado: dict, sku: str, modo: str) -> None:
    """Adiciona o resultado do motor à fila de aprovação."""
    import uuid
    from datetime import datetime

    # Evita duplicatas pendentes para o mesmo SKU
    if db_mod.ja_existe_pendente(sku):
        logger.debug("SKU %s: já existe pendente na fila — ignorado", sku)
        return

    agora = datetime.now().isoformat()
    itens = (resultado.get("integracao") or {}).get("itens") or resultado.get("itens", [])

    item = {
        "id": str(uuid.uuid4()),
        "status": "pendente",
        "sku": sku,
        "nome": resultado.get("produto_bling", {}).get("nome", ""),
        "criado_em": agora,
        "atualizado_em": agora,
        "marketplaces": {},  # será preenchido pelo app.py ao normalizar
        "auditoria": resultado.get("auditoria") or resultado,
        "payload_original": {"origem": "scheduler", "modo_aprovacao": modo},
        "historico_decisao": [],
        "resultado_aplicacao": None,
    }

    db_mod.inserir_item_fila(item)
    logger.debug("SKU %s enfileirado (id=%s)", sku, item["id"])


# ─────────────────────────────────────────────
# Loop principal
# ─────────────────────────────────────────────

_scheduler_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def _loop():
    logger.info("Scheduler iniciado (intervalo: %ds)", _intervalo())
    while not _stop_event.is_set():
        try:
            _ciclo_atualizacao()
        except Exception as e:
            logger.exception("Erro inesperado no ciclo do scheduler: %s", e)

        # Aguarda o intervalo em fatias de 5s para poder parar rapidamente
        intervalo = _intervalo()
        for _ in range(intervalo // 5):
            if _stop_event.is_set():
                break
            time.sleep(5)
        # Resto do intervalo
        resto = intervalo % 5
        if resto and not _stop_event.is_set():
            time.sleep(resto)

    logger.info("Scheduler encerrado.")


def iniciar_scheduler_background() -> threading.Thread:
    """
    Inicia o scheduler em background thread.
    Chamar no evento de startup do FastAPI:

        @app.on_event("startup")
        async def startup():
            from scheduler import iniciar_scheduler_background
            iniciar_scheduler_background()
    """
    global _scheduler_thread

    if not _scheduler_ativo():
        logger.info("Scheduler desativado via SCHEDULER_ATIVO=false")
        return None

    if _scheduler_thread and _scheduler_thread.is_alive():
        logger.warning("Scheduler já está rodando.")
        return _scheduler_thread

    _stop_event.clear()
    _scheduler_thread = threading.Thread(target=_loop, name="shinsei-scheduler", daemon=True)
    _scheduler_thread.start()
    return _scheduler_thread


def parar_scheduler() -> None:
    """Para o scheduler graciosamente. Chamar no evento de shutdown do FastAPI."""
    _stop_event.set()
    if _scheduler_thread:
        _scheduler_thread.join(timeout=15)


# ─────────────────────────────────────────────
# Execução como processo independente
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from pathlib import Path

    # Garante que o diretório do projeto está no path
    sys.path.insert(0, str(BASE_DIR))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Inicializa o banco antes de rodar
    db_mod = _optional_import("database")
    if db_mod:
        db_mod.init_db()

    logger.info("Iniciando Shinsei Pricing Scheduler (processo independente)")
    logger.info("Intervalo: %ds | Modo: %s", _intervalo(), "ativo" if _scheduler_ativo() else "desativado")

    if not _scheduler_ativo():
        logger.info("SCHEDULER_ATIVO=false — nada a fazer.")
        sys.exit(0)

    try:
        _loop()
    except KeyboardInterrupt:
        logger.info("Interrompido pelo usuário.")
