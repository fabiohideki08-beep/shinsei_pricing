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



    # Busca TODOS os produtos do Bling com paginação completa
    try:
        produtos = []
        page = 1
        limit = 100
        while True:
            resp = client.list_products(page=page, limit=limit)
            data = resp.get("data", []) if isinstance(resp, dict) else resp
            if not data:
                break
            produtos.extend(data)
            logger.debug("Scheduler: página %d — %d produtos acumulados", page, len(produtos))
            if len(data) < limit:
                break
            page += 1
        resumo["produtos_buscados"] = len(produtos)
        logger.info("Scheduler: %d produtos buscados do Bling (%d páginas)", len(produtos), page)
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

                objetivo=cfg_raw.get("objetivo", "lucro_liquido"),

                tipo_alvo=cfg_raw.get("tipo_alvo", "percentual"),

                valor_alvo=float(cfg_raw.get("valor_alvo") or cfg_raw.get("valor_alvo_padrao") or 30.0),

                peso_override=0,

                intelligence_config={},

                modo_aprovacao=modo_aprovacao,

                regra_estoque=cfg_raw.get("regra_estoque"),

                produto_prefetchado=produto,

            )



            if resultado.get("erro"):
                erro_codigo = resultado.get("erro_codigo", "")
                logger.warning("SKU %s: erro no motor — %s", sku, resultado["erro"])
                # Enfileira produtos com dados incompletos para preenchimento manual
                if erro_codigo in ("peso_ausente", "custo_ausente", "composicao_sem_custo") and db_mod:
                    import uuid as _uuid, datetime as _dt
                    _agora = _dt.datetime.now().isoformat()
                    _item = {
                        "id": _uuid.uuid4().hex,
                        "status": "incompleto",
                        "campos_faltando": ["peso"] if erro_codigo == "peso_ausente" else ["custo", "composicao"] if erro_codigo == "composicao_sem_custo" else ["custo"] if erro_codigo == "custo_ausente" else [],
                        "sku": sku,
                        "nome": produto.get("nome") or resultado.get("acao", sku),
                        "criado_em": _agora,
                        "atualizado_em": _agora,
                        "marketplaces": {},
                        "auditoria": resultado,
                        "payload_original": {"origem": "scheduler", "modo_aprovacao": modo_aprovacao},
                        "historico_decisao": [],
                        "resultado_aplicacao": None,
                        "formato": produto.get("formato", ""),
                        "dados_incompletos": {
                            "peso_ausente": erro_codigo == "peso_ausente",
                            "custo_ausente": erro_codigo in ("custo_ausente", "composicao_sem_custo"),
                            "composicao": erro_codigo == "composicao_sem_custo" or produto.get("formato") == "E",
                            "erro": resultado.get("erro"),
                            "componentes_sem_custo": resultado.get("custo_extraido", {}).get("componentes", []) if erro_codigo == "composicao_sem_custo" else [],
                        }
                    }
                    if not db_mod.ja_existe_pendente(sku) and not _ja_existe_incompleto(db_mod, sku):
                        db_mod.inserir_item_fila(_item)
                        logger.info("SKU %s enfileirado como incompleto: %s", sku, erro_codigo)
                resumo["erros"] += 1
                continue
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






def _ja_existe_incompleto(db_mod, sku: str) -> bool:
    """Verifica se já existe um item incompleto pendente para o SKU."""
    try:
        itens = db_mod.listar_fila(status="incompleto")
        return any(i.get("sku") == sku for i in itens)
    except Exception:
        return False

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
    marketplaces = {}
    for _it in (itens or []):
        _canal = _it.get("canal", "")
        if not _canal: continue
        _chave = _canal.lower().replace(" ", "_")
        marketplaces[_chave] = {"label": _canal, "preco": _it.get("preco_final") or _it.get("preco", 0), "preco_promocional": _it.get("preco_promocional", 0), "lucro": _it.get("lucro_liquido", 0), "margem": _it.get("margem_liquida_percentual") or _it.get("margem", 0), "comissao": _it.get("comissao", 0), "frete": _it.get("frete", 0), "taxa_fixa": _it.get("taxa_fixa", 0), "imposto": _it.get("imposto", 0), "custo_total": _it.get("custo_total", 0), "faixa_aplicada": _it.get("faixa_aplicada", ""), "indice_final": _it.get("indice_final", 0), "raw": _it}



    item = {

        "id": str(uuid.uuid4()),

        "status": "pendente",

        "sku": sku,

        "nome": resultado.get("produto_bling", {}).get("nome", "") or resultado.get("acao", sku),

        "criado_em": agora,

        "atualizado_em": agora,

        "marketplaces": marketplaces,

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

# Intervalo mínimo entre limpezas de barcode de kits (padrão: 7 dias)
# NOTA: A defesa principal é o webhook /webhooks/shopify/produto (tempo real).
# Este ciclo é um fallback para cobrir: CSV bulk imports, downtime do app,
# produtos criados antes do registro do webhook.
_INTERVALO_LIMPEZA_KITS = int(os.getenv("INTERVALO_LIMPEZA_KITS", str(7 * 24 * 3600)))
_ultima_limpeza_kits: Optional[float] = None

# Intervalo para sync da coleção de descontos (padrão: 1 hora)
_INTERVALO_SYNC_DESCONTOS = int(os.getenv("INTERVALO_SYNC_DESCONTOS", str(3600)))
_ultima_sync_descontos: Optional[float] = None

# Intervalo para sync de estoque Amazon (padrão: 2 horas)

# Intervalo para scan GMC (padrão: 15 minutos)
# Qualquer produto bloqueado no Shopping é removido automaticamente após cada scan.
# Intervalo curto = janela mínima de exposição de produtos problemáticos.
_INTERVALO_GMC_SCAN = int(os.getenv("INTERVALO_GMC_SCAN", str(900)))
_ultima_gmc_scan: Optional[float] = None

# Controle SCBOT (executa às 09:00, via scheduler para sobreviver idle do Cloud Run)
_ultimo_dia_scbot: Optional[object] = None
_ultimo_dia_sort: Optional[object] = None

# Controle do refresh do cache de produtos Shopify (executa às 06:00, diário)
_ultimo_dia_cache_produtos: Optional[object] = None


def _ciclo_gmc_scan() -> None:
    """
    Scan periódico (15 min) do Google Merchant Center.
    Todo produto que bloqueia exibição no Shopping é removido imediatamente
    e enviado à fila de correção (via _auto_delete_after_scan).
    O resultado é persistido em JSON para que a UI mostre o status mesmo
    em caso de troca de instância no Cloud Run.
    """
    global _ultima_gmc_scan

    agora = time.time()
    if _ultima_gmc_scan and (agora - _ultima_gmc_scan) < _INTERVALO_GMC_SCAN:
        return

    logger.info("[GMC] Iniciando scan automático (intervalo=%ds)...", _INTERVALO_GMC_SCAN)
    try:
        from routes.gmc import _scan_gmc, _auto_delete_after_scan, _save_scan_status
        resultado = _scan_gmc()
        deleted = _auto_delete_after_scan(resultado)
        ok_count   = sum(1 for d in deleted if d.get("ok"))
        fail_count = len(deleted) - ok_count
        blocked    = len(resultado.get("disapproved", [])) + sum(
            1 for p in resultado.get("limited", [])
            if any(
                d.get("destination","") in ("Shopping","Shopping ads","SurfacesAcrossGoogle")
                and d.get("status") in ("disapproved","excluded")
                for d in p.get("destinations", [])
            )
        )
        _ultima_gmc_scan = agora
        logger.info(
            "[GMC] Scan concluído — %d escaneados | %d bloqueados detectados | %d removidos | %d falhas",
            resultado.get("total_scanned", 0), blocked, ok_count, fail_count,
        )
        # Persiste metadados para a UI ler mesmo em instâncias diferentes
        _save_scan_status({
            "last_scan_at":      datetime.utcnow().isoformat(),
            "next_scan_in_s":    _INTERVALO_GMC_SCAN,
            "total_scanned":     resultado.get("total_scanned", 0),
            "blocked_detected":  blocked,
            "removed_ok":        ok_count,
            "removed_fail":      fail_count,
            "interval_s":        _INTERVALO_GMC_SCAN,
        })
    except Exception as e:
        logger.exception("[GMC] Erro no scan automático: %s", e)
        _ultima_gmc_scan = agora  # evita loop de retentativas


def _ciclo_sync_descontos() -> None:
    """Sincroniza a coleção 'Descontos acima de 50%' a cada hora."""
    global _ultima_sync_descontos

    agora = time.time()
    if _ultima_sync_descontos and (agora - _ultima_sync_descontos) < _INTERVALO_SYNC_DESCONTOS:
        return

    logger.info("[DESCONTOS] Iniciando sync da coleção de descontos...")
    try:
        from routes.sync_descontos import sync
        result = sync()
        logger.info(
            "[DESCONTOS] Sync concluído — adicionados: %d | removidos: %d | total: %d",
            result.get("added", 0), result.get("removed", 0), result.get("total", 0),
        )
        _ultima_sync_descontos = agora
    except Exception as e:
        logger.exception("[DESCONTOS] Erro no sync: %s", e)
        _ultima_sync_descontos = agora



def _ciclo_limpeza_kits() -> None:
    """
    Fallback semanal: remove barcodes de Kit/Combo que o webhook possa ter perdido.
    Defesa principal = webhook Shopify products/create+update em tempo real.
    """
    global _ultima_limpeza_kits

    agora = time.time()
    if _ultima_limpeza_kits and (agora - _ultima_limpeza_kits) < _INTERVALO_LIMPEZA_KITS:
        return  # Ainda não é hora

    logger.info("[KIT-BARCODE] Iniciando limpeza semanal de barcodes de kits...")
    try:
        from limpar_barcodes_kits import limpar_barcodes_kits
        result = limpar_barcodes_kits(dry_run=False)
        logger.info(
            "[KIT-BARCODE] Concluído — %d variantes limpas em %d kits. Erros: %d",
            result.get("variantes_limpas", 0),
            result.get("kits_com_barcode", 0),
            result.get("erros", 0),
        )
        _ultima_limpeza_kits = agora
    except Exception as e:
        logger.exception("[KIT-BARCODE] Erro na limpeza de barcodes: %s", e)
        _ultima_limpeza_kits = agora - _INTERVALO_LIMPEZA_KITS + 3600



def _ciclo_scbot() -> None:
    """
    Executa o SCBOT (Google Indexing API) às 09:00 e ordena coleções às 04:00.
    Chamado a cada ciclo do scheduler (300s) para compensar idle kills do Cloud Run.
    """
    global _ultimo_dia_scbot, _ultimo_dia_sort
    from datetime import date as _date

    agora = datetime.now()
    hoje = agora.date()

    # Sort collections às 04:00
    if agora.hour == 4 and _ultimo_dia_sort != hoje:
        logger.info("[SCBOT] Disparando sort_collections_by_stock — %s", hoje.isoformat())
        try:
            from seo_sort_collections import sort_collections_by_stock
            sort_collections_by_stock()
        except Exception as e:
            logger.error("[SCBOT] Erro no sort_collections: %s", e)
        _ultimo_dia_sort = hoje

    # Indexação Google às 09:00
    if agora.hour == 9 and _ultimo_dia_scbot != hoje:
        logger.info("[SCBOT] Disparando ciclo diário de indexação — %s", hoje.isoformat())
        try:
            from scbot import executar_ciclo
            executar_ciclo()
        except Exception as e:
            logger.error("[SCBOT] Erro no ciclo diário: %s", e)
        _ultimo_dia_scbot = hoje


def _ciclo_carrinhos() -> None:
    """Delega para processar_fila_carrinhos() em shopify_webhooks."""
    try:
        from routes.shopify_webhooks import processar_fila_carrinhos
        processar_fila_carrinhos()
    except Exception as e:
        logger.exception("Erro em processar_fila_carrinhos: %s", e)


def _ciclo_refresh_cache_produtos() -> None:
    """
    Atualiza data/all_products_cache.json às 06:00 diariamente, incluindo o campo handle.
    Sem handle, o SCBOT só envia 13 URLs (páginas fixas) em vez de até 200.
    """
    global _ultimo_dia_cache_produtos
    from datetime import date as _date

    agora = datetime.now()
    hoje = agora.date()

    if agora.hour != 6 or _ultimo_dia_cache_produtos == hoje:
        return

    logger.info("[CACHE-PRODUTOS] Iniciando refresh do cache Shopify (com handle)...")
    try:
        import requests as _req
        import json as _json

        cfg_path = BASE_DIR / "data" / "shopify_config.json"
        if not cfg_path.exists():
            logger.warning("[CACHE-PRODUTOS] shopify_config.json não encontrado — skip")
            return

        cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
        token = cfg.get("access_token") or cfg.get("token", "")
        shop = cfg.get("shop_url") or cfg.get("myshopify_domain") or cfg.get("shop", "")
        if shop and "." not in shop:
            shop = f"{shop}.myshopify.com"
        if not token or not shop:
            logger.warning("[CACHE-PRODUTOS] Credenciais Shopify ausentes — skip")
            return

        headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
        base_url = f"https://{shop}/admin/api/2024-01/products.json"
        produtos: list[dict] = []
        page_info = None

        while True:
            params: dict = {
                "limit": 250,
                "fields": "id,title,handle,vendor,product_type,variants",
                "status": "active",
            }
            if page_info:
                params = {"limit": 250, "page_info": page_info}

            r = _req.get(base_url, headers=headers, params=params, timeout=60)
            if r.status_code != 200:
                logger.error("[CACHE-PRODUTOS] Shopify retornou %d: %s", r.status_code, r.text[:200])
                break

            batch = r.json().get("products", [])
            produtos.extend(batch)

            link = r.headers.get("Link", "")
            page_info = None
            if 'rel="next"' in link:
                import re as _re
                m = _re.search(r'<[^>]*[?&]page_info=([^&>]+)[^>]*>;\s*rel="next"', link)
                if m:
                    page_info = m.group(1)
            if not page_info:
                break

        cache_path = BASE_DIR / "data" / "all_products_cache.json"
        cache_path.write_text(
            _json.dumps(produtos, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("[CACHE-PRODUTOS] Cache atualizado: %d produtos com handle", len(produtos))
        _ultimo_dia_cache_produtos = hoje

    except Exception as e:
        logger.exception("[CACHE-PRODUTOS] Erro ao atualizar cache: %s", e)
        _ultimo_dia_cache_produtos = hoje  # evita loop de retentativas


def _loop():

    logger.info("Scheduler iniciado (intervalo: %ds)", _intervalo())

    while not _stop_event.is_set():

        # ── PRIORIDADE 1: GMC scan — roda primeiro para garantir que o catálogo
        # esteja limpo o mais rápido possível após cada reinicialização/deploy.
        # Na primeira iteração _ultima_gmc_scan=None → roda imediatamente.
        try:
            _ciclo_gmc_scan()
        except Exception as e:
            logger.exception("Erro no ciclo de scan GMC: %s", e)

        # ── Atualização de preços (Bling → fila de aprovação)
        try:

            _ciclo_atualizacao()

        except Exception as e:

            logger.exception("Erro inesperado no ciclo do scheduler: %s", e)

        # Limpeza semanal de barcodes de kits (anti-tobacco GMC)
        try:
            _ciclo_limpeza_kits()
        except Exception as e:
            logger.exception("Erro no ciclo de limpeza de kits: %s", e)

        # Sync da coleção "Descontos acima de 50%" (a cada hora)
        try:
            _ciclo_sync_descontos()
        except Exception as e:
            logger.exception("Erro no sync de descontos: %s", e)

        # Refresh do cache de produtos Shopify às 06:00 (necessário para o SCBOT)
        try:
            _ciclo_refresh_cache_produtos()
        except Exception as e:
            logger.exception("Erro no refresh do cache de produtos: %s", e)

        # SCBOT: indexação Google às 09:00, sort collections às 04:00
        try:
            _ciclo_scbot()
        except Exception as e:
            logger.exception("Erro no ciclo SCBOT: %s", e)

        # Carrinho abandonado: processa fila persistente (email 1h, WhatsApp 24h/72h)
        try:
            _ciclo_carrinhos()
        except Exception as e:
            logger.exception("Erro no ciclo de carrinhos abandonados: %s", e)

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

