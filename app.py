from __future__ import annotations
from dotenv import load_dotenv
load_dotenv()
import hashlib
import hmac as _hmac
import importlib, json, uuid, threading
import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from fastapi import BackgroundTasks, Body, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel
from database import (
    init_db, listar_regras as db_listar_regras,
    inserir_regra, atualizar_regra, excluir_regra,
    substituir_todas_regras,
    listar_fila as db_listar_fila,
    buscar_item_fila, inserir_item_fila, atualizar_status_fila,
    stats_fila, limpar_invalidos_fila, reset_fila,
    ja_existe_pendente, get_config as db_get_config,
    migrar_json_legado,
)
from scheduler import iniciar_scheduler_background, parar_scheduler
from scbot import iniciar_scbot, parar_scbot, executar_ciclo as scbot_executar, carregar_status as scbot_status
from logging_config import configurar_logging
from auth import verificar_api_key

configurar_logging()

# Cache de links por SKU (evita chamadas repetidas à API)
_links_cache: dict = {}
_links_cache_ttl: dict = {}
_LINKS_CACHE_SECONDS = 3600  # 1 hora

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
PAGES_DIR = BASE_DIR / "pages"
REGRAS_PATH = DATA_DIR / "regras.json"
FILA_PATH = DATA_DIR / "fila_aprovacao.json"
CFG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "historico_precificacao.jsonl"
DATA_DIR.mkdir(exist_ok=True)
PAGES_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Shinsei Pricing")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ── Estado da conferência ML em background ────────────────────────────
_conf_ml: dict = {
    "rodando": False, "concluido": False, "erro": None,
    "pagina": 0, "max_paginas": 20,
    "verificados": 0, "divergencias": 0, "sem_sku": 0, "erros": 0,
    "iniciado_em": None, "concluido_em": None, "resultado": None,
}

def _conf_ml_callback(pagina, max_paginas, verificados, divergencias, sem_sku, erros):
    _conf_ml.update({"pagina": pagina, "max_paginas": max_paginas,
                     "verificados": verificados, "divergencias": divergencias,
                     "sem_sku": sem_sku, "erros": erros})

def _rodar_conf_ml_bg():
    try:
        from ml_estoque_conferencia import conferir_estoques_ml
        from bling_client import BlingClient as _BC
        client = _BC()
        resultado = conferir_estoques_ml(client, max_paginas=_conf_ml["max_paginas"],
                                         progresso_callback=_conf_ml_callback)
        _conf_ml.update({"rodando": False, "concluido": True,
                         "resultado": resultado, "concluido_em": datetime.utcnow().isoformat()})
    except Exception as e:
        _conf_ml.update({"rodando": False, "concluido": True, "erro": str(e),
                         "concluido_em": datetime.utcnow().isoformat()})
from routes.batch import router as batch_router
from routes.ml_unificado import router as ml_router
from routes.bling import router as bling_page_router
from monitoring import router as monitoring_router
from routes.gmc import router as gmc_router
from routes.shopee import router as shopee_router, aplicar_preco_shopee_por_sku
app.include_router(batch_router)
from routes.mercado_livre import router as ml_page_router
app.include_router(ml_page_router)
app.include_router(ml_router)
app.include_router(bling_page_router)
app.include_router(monitoring_router)
app.include_router(gmc_router)
app.include_router(shopee_router)
try:
    from routes.frete import router as frete_router
    app.include_router(frete_router)
    logger.info("Motor de frete Shinsei registrado em /frete")
except Exception as _frete_exc:
    logger.warning("Motor de frete não carregado: %s", _frete_exc)

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    return await verificar_api_key(request, call_next)

@app.on_event("startup")
def startup():
    init_db()
    migrar_json_legado()
    iniciar_scheduler_background()
    iniciar_scbot()
    logger.info("Shinsei Pricing iniciado")

@app.on_event("shutdown")
def shutdown():
    parar_scheduler()
    parar_scbot()

def _optional_import(module_name: str):
    try: return importlib.import_module(module_name)
    except Exception: return None

pricing_module = _optional_import("pricing_engine_real") or _optional_import("pricing_engine")
if pricing_module is None:
    raise RuntimeError("pricing_engine_real.py ou pricing_engine.py não encontrado.")
montar_precificacao_bling: Optional[Callable[..., dict]] = getattr(pricing_module, "montar_precificacao_bling", None)
if montar_precificacao_bling is None:
    raise RuntimeError("Seu motor atual não expõe montar_precificacao_bling().")

bling_mod = _optional_import("bling_client")
BlingClient = getattr(bling_mod, "BlingClient", None) if bling_mod else None
bling_update_module = _optional_import("bling_update_engine")
aplicar_precos_multicanal = getattr(bling_update_module, "aplicar_precos_multicanal", None) if bling_update_module else None

DEFAULT_CFG = {"modo_aprovacao":"manual","fila_auto_ao_calcular":True,"peso_forca":0.4,"peso_equilibrio":0.4,"peso_lucro":0.2,"forcas_canais":{"Mercado Livre Classico":0.8,"Mercado Livre Premium":0.75,"Shopee":0.6,"Amazon":0.7,"Shein":0.55,"Shopify":0.65},"regra_estoque":{"ativo":False,"limite":2,"tipo":"percentual","valor":0}}
CANAL_ALIAS = {"Mercado Livre Classico":"mercado_livre_classico","Mercado Livre Premium":"mercado_livre_premium","Shopee":"shopee","Amazon":"amazon","Shein":"shein","Shopify":"shopify","Shopfy":"shopify"}

def _load_json(path: Path, default: Any):
    if not path.exists(): return default
    try: return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return default

def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

def _first_existing(path_options: list[Path]):
    for path in path_options:
        if path.exists(): return path
    return None

def carregar_regras(apenas_ativas: bool = False) -> list[dict]:
    try:
        return db_listar_regras(apenas_ativas=apenas_ativas)
    except Exception:
        regras = _load_json(REGRAS_PATH, [])
        if not isinstance(regras, list): return []
        for r in regras:
            if isinstance(r, dict): r.setdefault("ativo", True)
        return [r for r in regras if isinstance(r, dict) and (r.get("ativo", True) or not apenas_ativas)]

def carregar_cfg() -> dict:
    data = _load_json(CFG_PATH, {})
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    if isinstance(data, dict):
        cfg.update(data)
        cfg["forcas_canais"] = {**DEFAULT_CFG["forcas_canais"], **data.get("forcas_canais", {})}
        cfg["regra_estoque"] = {**DEFAULT_CFG["regra_estoque"], **data.get("regra_estoque", {})}
    return cfg

def carregar_fila() -> list[dict]:
    try:
        return db_listar_fila()
    except Exception:
        itens = _load_json(FILA_PATH, [])
        return itens if isinstance(itens, list) else []

def salvar_fila(itens: list[dict]) -> None:
    try:
        reset_fila()
        for item in itens:
            inserir_item_fila(item)
    except Exception:
        _save_json(FILA_PATH, itens)

def _fila_stats(itens: list[dict]) -> dict:
    stats = {"pendente":0,"aprovado":0,"rejeitado":0}
    for item in itens:
        status = str(item.get("status","pendente")).lower()
        if status in stats: stats[status] += 1
    stats["total"] = len(itens)
    return stats

def _normalizar_marketplaces(itens: list[dict]) -> dict[str, dict]:
    marketplaces = {}
    for item in itens or []:
        canal = item.get("canal") or "Canal"
        key = CANAL_ALIAS.get(canal, canal.lower().replace(" ","_"))
        marketplaces[key] = {
            "label": canal,
            "preco": item.get("preco_virtual") or item.get("preco_cheio") or item.get("preco_sugerido") or item.get("preco_promocional") or item.get("preco_final") or 0,
            "preco_promocional": item.get("preco_promocional") or item.get("preco_final") or 0,
            "lucro": item.get("lucro_liquido") or item.get("lucro") or 0,
            "margem": item.get("margem") or item.get("margem_liquida_percentual") or 0,
            "comissao": item.get("comissao") or 0,
            "frete": item.get("frete") or 0,
            "taxa_fixa": item.get("taxa_fixa") or 0,
            "imposto": item.get("imposto") or 0,
            "custo_total": item.get("custo_total") or item.get("custo_produto") or 0,
            "faixa_aplicada": item.get("faixa_aplicada") or "",
            "indice_final": item.get("indice_final") or 0,
            "raw": item,
        }
    return marketplaces

def _marketplaces_validos(marketplaces: dict) -> bool:
    return isinstance(marketplaces, dict) and len(marketplaces) > 0

def _diagnostico_preview(preview: dict) -> dict:
    aud = preview.get("auditoria") or {}
    produto = preview.get("produto") or {}
    marketplaces = preview.get("marketplaces") or {}
    if aud.get("erro"):
        codigo = str(aud.get("erro_codigo") or "").strip()
        if codigo not in {"custo_ausente", "peso_ausente", "composicao_sem_custo"}:
            erro_txt = str(aud.get("erro") or "").lower()
            if "sem peso" in erro_txt:
                codigo = "peso_ausente"
            elif "composição" in erro_txt or "composicao" in erro_txt:
                codigo = "composicao_sem_custo"
            elif "sem custo" in erro_txt:
                codigo = "custo_ausente"
            else:
                codigo = "erro_motor"
        return {"ok":False,"codigo":codigo,"mensagem":str(aud.get("erro")),"detalhe":aud.get("acao") or ""}
    if not (produto.get("codigo") or aud.get("sku")):
        return {"ok":False,"codigo":"sku_ausente","mensagem":"SKU ausente no retorno do Bling.","detalhe":"Verifique se o produto encontrado possui Código (SKU) cadastrado."}
    custo = float(aud.get("custo_usado") or 0)
    peso = float(aud.get("peso_usado") or 0)
    tipo_custo = str(aud.get("tipo_custo") or "").lower()
    componentes = aud.get("componentes_custo") or []
    if peso <= 0:
        return {"ok":False,"codigo":"peso_ausente","mensagem":"Produto sem peso no Bling.","detalhe":"Preencha o peso no produto ou use peso override no simulador."}
    if custo <= 0 and tipo_custo == "composicao":
        faltando = [c.get("sku") or str(c.get("id") or "-") for c in componentes if float(c.get("custo_unitario") or 0) <= 0]
        return {"ok":False,"codigo":"composicao_sem_custo","mensagem":"Composição sem custo válido nos componentes.","detalhe":("Componentes sem custo: " + ", ".join(faltando)) if faltando else "Nenhum componente retornou custo válido."}
    if custo <= 0:
        return {"ok":False,"codigo":"custo_ausente","mensagem":"Produto sem custo no estoque do Bling.","detalhe":"Preencha o preço de compra/custo do produto no estoque."}
    if not _marketplaces_validos(marketplaces):
        return {"ok":False,"codigo":"sem_canais","mensagem":"Nenhum canal válido foi calculado.","detalhe":"Verifique peso, custo e faixas da Aba2 para este produto."}
    return {"ok":True,"codigo":"preview_valido","mensagem":"Preview válido.","detalhe":""}

def _preview_valido(preview: dict):
    diag = _diagnostico_preview(preview)
    return bool(diag.get("ok")), str(diag.get("mensagem") or "Preview inválido.")

def _montar_item_fila(preview: dict, payload_original: dict | None = None) -> dict:
    aud = preview.get("auditoria") or {}
    produto = preview.get("produto") or {}
    agora = datetime.utcnow().isoformat()
    return {"id":str(uuid.uuid4()),"status":"pendente","criado_em":agora,"atualizado_em":agora,"sku":aud.get("sku") or produto.get("codigo") or "","nome":produto.get("nome") or "","produto_bling":produto,"marketplaces":preview.get("marketplaces") or {},"auditoria":aud,"payload_original":payload_original or preview.get("raw") or {},"historico_decisao":[],"resultado_aplicacao":None}

def _ja_existe_pendente_semelhante(itens: list[dict], sku: str, auditoria: dict) -> bool:
    custo = round(float(auditoria.get("custo_usado") or 0), 2)
    peso = round(float(auditoria.get("peso_usado") or 0), 3)
    for item in itens:
        if item.get("status") != "pendente": continue
        if str(item.get("sku") or "").strip() != str(sku).strip(): continue
        aud = item.get("auditoria") or {}
        if round(float(aud.get("custo_usado") or 0), 2) == custo and round(float(aud.get("peso_usado") or 0), 3) == peso:
            return True
    return False

class IntegracaoPayload(BaseModel):
    criterio: str = "sku"
    valor_busca: str = ""
    embalagem: float = 0
    imposto: float = 4
    quantidade: int = 1
    objetivo: str = "lucro_liquido"
    tipo_alvo: str = "percentual"
    valor_alvo: float = 30
    peso_override: float = 0
    score_config: Optional[dict] = None
    modo_aprovacao: str = "manual"
    modo_preco_virtual: str = "percentual_acima"
    acrescimo_percentual: float = 20
    acrescimo_nominal: float = 0
    preco_manual: float = 0
    arredondamento: str = "90"
    preco_compra_anterior_bling: float = 0

class DebugSkuPayload(BaseModel):
    sku: str

class AtualizacaoCampoBlingPayload(BaseModel):
    produto_id: int
    valor: float

FALLBACK_HTML = "<!DOCTYPE html><html lang='pt-BR'><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'><title>Shinsei Pricing</title></head><body><h1>Shinsei Pricing</h1></body></html>"

def _prepare_product_patch(existing: dict) -> dict:
    patch = dict(existing) if isinstance(existing, dict) else {}
    if "data" in patch and isinstance(patch["data"], dict):
        patch = dict(patch["data"])
    return patch



@app.get("/auditoria/ml-sem-sku")
def auditoria_ml_sem_sku():
    """Lista anúncios ML ativos sem seller_custom_field (SKU) vinculado."""
    try:
        import json as _json, requests as _req
        from pathlib import Path
        _tokens_path = BASE_DIR / "data" / "ml_tokens.json"
        if not _tokens_path.exists():
            return {"ok": False, "erro": "Token ML não configurado.", "itens": [], "stats": {"sem_sku": 0}}
        _tokens = _json.loads(_tokens_path.read_text(encoding="utf-8"))
        _token = _tokens.get("access_token", "")
        _h = {"Authorization": f"Bearer {_token}"}
        
        # Busca anúncios ativos
        _sem_sku = []
        _offset = 0
        _limit = 100
        while _offset < 500:  # máximo 500 anúncios por vez
            _r = _req.get(
                f"https://api.mercadolibre.com/users/733168645/items/search",
                params={"status": "active", "limit": _limit, "offset": _offset},
                headers=_h, timeout=15
            )
            if _r.status_code != 200:
                break
            _data = _r.json()
            _items = _data.get("results", [])
            if not _items:
                break
            
            # Busca detalhes em lote (até 20 por vez)
            for i in range(0, len(_items), 20):
                _batch = _items[i:i+20]
                _ids = ",".join(_batch)
                _r2 = _req.get(
                    f"https://api.mercadolibre.com/items",
                    params={"ids": _ids, "attributes": "id,title,available_quantity,seller_custom_field,status,catalog_product_id"},
                    headers=_h, timeout=15
                )
                if _r2.status_code == 200:
                    for entry in _r2.json():
                        item = entry.get("body") or entry
                        if not item.get("seller_custom_field"):
                            _sem_sku.append({
                                "id": item.get("id"),
                                "titulo": item.get("title", "")[:60],
                                "estoque": item.get("available_quantity", 0),
                                "status": item.get("status"),
                                "catalogo": bool(item.get("catalog_product_id")),
                                "link": f"https://www.mercadolivre.com.br/anuncios/{item.get('id')}/editar",
                            })
            
            _offset += _limit
            if len(_items) < _limit:
                break
        
        return {
            "ok": True,
            "itens": _sem_sku,
            "stats": {"sem_sku": len(_sem_sku)},
        }
    except Exception as e:
        logger.error("Erro ao verificar ML sem SKU: %s", e)
        return {"ok": False, "erro": str(e), "itens": [], "stats": {"sem_sku": 0}}

@app.get("/", response_class=HTMLResponse)
def home():
    html_file = _first_existing([BASE_DIR / "index.html", PAGES_DIR / "simulador.html"])
    return HTMLResponse(html_file.read_text(encoding="utf-8")) if html_file else HTMLResponse(FALLBACK_HTML)

@app.get("/simulador", response_class=HTMLResponse)
def simulador_page():
    html_file = _first_existing([PAGES_DIR / "simulador.html", BASE_DIR / "index.html"])
    return HTMLResponse(html_file.read_text(encoding="utf-8")) if html_file else HTMLResponse(FALLBACK_HTML)

@app.get("/fila", response_class=HTMLResponse)
def fila_page():
    html_file = PAGES_DIR / "fila.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="pages/fila.html não encontrado.")

@app.get("/health")
def health():
    itens = [i for i in carregar_fila() if i.get("status") in ("pendente","incompleto")]
    return {"status":"Shinsei Pricing rodando","engine":pricing_module.__name__,"bling_client":bool(BlingClient),"bling_update_engine":bool(aplicar_precos_multicanal),"modo_busca":"sku_only","fila":_fila_stats(itens)}

@app.get("/bling/status")
def bling_status():
    if not BlingClient: return {"ok":False,"erro":"bling_client.py não encontrado."}
    try:
        client = BlingClient()
        return {"ok":True,"configurado":bool(getattr(client,"client_id","") and getattr(client,"client_secret","") and getattr(client,"redirect_uri","")),"token_local":bool(client.has_local_tokens())}
    except Exception as exc:
        return {"ok":False,"erro":str(exc)}

@app.get("/bling/auth")
def bling_auth():
    if not BlingClient: raise HTTPException(status_code=500, detail="bling_client.py não encontrado.")
    try:
        client = BlingClient()
        return RedirectResponse(client.build_authorize_url())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.get("/bling/callback")
def bling_callback(code: str | None = Query(None), state: str | None = Query(None), error: str | None = Query(None), error_description: str | None = Query(None)):
    if not BlingClient: raise HTTPException(status_code=500, detail="bling_client.py não encontrado.")
    if error: raise HTTPException(status_code=400, detail=f"Bling OAuth retornou erro: {error}. {error_description or ''}".strip())
    if not code: raise HTTPException(status_code=400, detail="Callback do Bling sem code de autorização.")
    try:
        client = BlingClient()
        token = client.exchange_code_for_token(code, state=state)
        return {"ok":True,"message":"Conexão com Bling realizada.","expires_in":token.get("expires_in")}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/bling/debug/sku")
def bling_debug_sku(payload: DebugSkuPayload):
    if not BlingClient: raise HTTPException(status_code=500, detail="bling_client.py não encontrado.")
    try:
        client = BlingClient()
        return client.debug_product_by_sku(payload.sku)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/bling/produto/buscar")
def bling_produto_buscar(payload: DebugSkuPayload):
    if not BlingClient: raise HTTPException(status_code=500, detail="bling_client.py não encontrado.")
    try:
        client = BlingClient()
        return client.get_product_by_sku(payload.sku)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/bling/produto/atualizar-peso")
def bling_produto_atualizar_peso(payload: AtualizacaoCampoBlingPayload):
    if not BlingClient:
        raise HTTPException(status_code=500, detail="bling_client.py não encontrado.")
    if float(payload.valor or 0) <= 0:
        raise HTTPException(status_code=400, detail="Informe um peso válido.")
    try:
        client = BlingClient()
        existing = client.get_product(int(payload.produto_id))
        patch = _prepare_product_patch(existing)
        patch["id"] = int(payload.produto_id)
        patch["pesoLiquido"] = float(payload.valor)
        patch["peso"] = float(payload.valor)
        if not patch.get("pesoBruto"):
            patch["pesoBruto"] = float(payload.valor)
        result = client.update_product(int(payload.produto_id), patch)
        atualizado = client.get_product(int(payload.produto_id))
        return {"ok": True, "message": "Peso atualizado no Bling.", "produto": atualizado, "raw": result}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Falha ao atualizar peso no Bling: {exc}")

@app.post("/bling/produto/atualizar-preco")
def bling_produto_atualizar_preco(payload: AtualizacaoCampoBlingPayload):
    if not BlingClient:
        raise HTTPException(status_code=500, detail="bling_client.py não encontrado.")
    if float(payload.valor or 0) <= 0:
        raise HTTPException(status_code=400, detail="Informe um preço válido.")
    try:
        client = BlingClient()
        existing = client.get_product(int(payload.produto_id))
        valor = round(float(payload.valor), 2)

        # Tenta via fornecedor primeiro
        fornecedor = existing.get("fornecedor") if isinstance(existing, dict) else None
        if isinstance(fornecedor, dict) and fornecedor.get("id"):
            patch = _prepare_product_patch(existing) if "_prepare_product_patch" in dir() else dict(existing)
            patch["id"] = int(payload.produto_id)
            fornecedor_patch = dict(fornecedor)
            fornecedor_patch["precoCusto"] = valor
            fornecedor_patch["precoCompra"] = valor
            patch["fornecedor"] = fornecedor_patch
            client.update_product(int(payload.produto_id), patch)
            logger.info("Custo atualizado via fornecedor: produto_id=%s valor=%s", payload.produto_id, valor)
            return {"ok": True, "message": f"Custo R${valor:.2f} atualizado no Bling."}
        else:
            # Fallback: salva custo localmente como override
            custo_override_path = DATA_DIR / "custo_override.json"
            overrides = _load_json(custo_override_path, {})
            sku = existing.get("codigo") or str(payload.produto_id) if isinstance(existing, dict) else str(payload.produto_id)
            prod_id_str = str(payload.produto_id)
            entry = {"custo": valor, "produto_id": payload.produto_id, "atualizado_em": datetime.utcnow().isoformat()}
            overrides[sku] = entry
            overrides[prod_id_str] = entry
            _save_json(custo_override_path, overrides)
            logger.info("Custo salvo localmente (sem fornecedor): sku=%s valor=%s", sku, valor)
            return {"ok": True, "message": f"Custo R${valor:.2f} salvo localmente. Recalcule para ver o resultado.", "local_only": True}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Falha ao atualizar custo: {exc}")


@app.post("/integracao/preview")
def integracao_preview(payload: IntegracaoPayload):
    regras = carregar_regras(apenas_ativas=True)
    if not regras:
        raise HTTPException(status_code=400, detail="Nenhuma regra cadastrada. Importe a Aba2 primeiro.")
    if (payload.criterio or "sku").strip().lower() != "sku":
        raise HTTPException(status_code=400, detail="A precificação integrada aceita apenas busca por SKU. Use criterio='sku'.")
    try:
        resultado = montar_precificacao_bling(
            regras=regras, criterio="sku", valor_busca=payload.valor_busca, embalagem=payload.embalagem, imposto=payload.imposto,
            quantidade=payload.quantidade, objetivo=payload.objetivo, tipo_alvo=payload.tipo_alvo, valor_alvo=payload.valor_alvo,
            peso_override=payload.peso_override, intelligence_config=payload.score_config or {}, modo_aprovacao=payload.modo_aprovacao,
            preco_compra_anterior_bling=payload.preco_compra_anterior_bling, modo_preco_virtual=payload.modo_preco_virtual,
            acrescimo_percentual=payload.acrescimo_percentual, acrescimo_nominal=payload.acrescimo_nominal, preco_manual=payload.preco_manual,
            arredondamento=payload.arredondamento, regra_estoque=carregar_cfg().get("regra_estoque"),
        )
        if resultado.get("erro"):
            preview = {"ok":False,"criterio_usado":"sku","produto":resultado.get("produto_bling") or {},"melhor_canal":"","modo_aprovacao":payload.modo_aprovacao,"marketplaces":{},"auditoria":resultado,"raw":resultado}
            preview["diagnostico"] = _diagnostico_preview(preview)
            preview["fila_auto"] = {"adicionado":False,"motivo":preview["diagnostico"]["mensagem"]}
            return preview
        itens = (resultado.get("integracao") or {}).get("itens") or resultado.get("itens_precificacao") or resultado.get("itens") or []
        preview = {"ok":True,"criterio_usado":"sku","produto":resultado.get("produto_bling") or {},"melhor_canal":resultado.get("melhor_canal") or "","modo_aprovacao":payload.modo_aprovacao,"marketplaces":_normalizar_marketplaces(itens or resultado.get("canais", [])),"auditoria":resultado.get("auditoria") or resultado,"raw":resultado}
        diagnostico = _diagnostico_preview(preview)
        preview["diagnostico"] = diagnostico
        valido, motivo = _preview_valido(preview)
        fila_auto = {"adicionado":False,"motivo":""}
        if carregar_cfg().get("fila_auto_ao_calcular", True) and valido:
            sku = preview["auditoria"].get("sku") or preview["produto"].get("codigo") or ""
            if ja_existe_pendente(sku):
                fila_auto = {"adicionado":False,"motivo":"Já existe item pendente equivalente na fila."}
            else:
                item = _montar_item_fila(preview, payload.dict())
                inserir_item_fila(item)
                _append_jsonl(LOG_PATH, {"evento":"fila_auto_preview","item_id":item["id"],"sku":item["sku"],"quando":item["criado_em"]})
                fila_auto = {"adicionado":True,"item_id":item["id"]}
        else:
            fila_auto = {"adicionado":False,"motivo":motivo or diagnostico.get("mensagem") or "Fila automática desativada."}
        preview["fila_auto"] = fila_auto
        logger.info("Precificação: SKU=%s melhor_canal=%s fila=%s", payload.valor_busca, preview.get("melhor_canal"), fila_auto.get("adicionado"))
        return preview
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Falha no preview: {exc}")

@app.get("/fila/lista")
def fila_lista():
    itens = [i for i in carregar_fila() if i.get("status") in ("pendente","incompleto")]
    return {"itens":itens,"stats":stats_fila()}

@app.post("/fila/adicionar")
def fila_adicionar(payload: dict = Body(...)):
    preview = {"ok":payload.get("ok", True),"produto":payload.get("produto_bling") or payload.get("produto") or {},"marketplaces":payload.get("marketplaces") or {},"auditoria":payload.get("auditoria") or {},"raw":payload.get("raw") or {}}
    diag = _diagnostico_preview(preview)
    if not diag.get("ok"): raise HTTPException(status_code=400, detail=f"Preview inválido para fila: {diag.get('mensagem')}")
    sku = (preview.get("auditoria") or {}).get("sku") or (preview.get("produto") or {}).get("codigo") or ""
    if ja_existe_pendente(sku):
        return {"ok":True,"duplicado":True,"message":"Já existe item pendente equivalente na fila.","stats":stats_fila()}
    item = _montar_item_fila(preview, payload.get("payload_original") or payload.get("raw") or {})
    inserir_item_fila(item)
    _append_jsonl(LOG_PATH, {"evento":"fila_adicionar_manual","item_id":item["id"],"sku":item["sku"],"quando":item["criado_em"]})
    return {"ok":True,"item":item,"stats":stats_fila()}

@app.post("/fila/limpar-invalidos")
def fila_limpar_invalidos():
    removidos_n = limpar_invalidos_fila()
    return {"ok":True,"removidos":removidos_n,"stats":stats_fila()}

@app.post("/fila/reset-total")
def fila_reset_total():
    reset_fila()
    return {"ok":True,"message":"Fila completamente limpa","stats":stats_fila()}


@app.get("/fila/links/{sku}")
async def fila_links(sku: str):
    """Retorna links diretos para edição do produto em cada marketplace."""
    import time
    # Verifica cache
    now = time.time()
    if sku in _links_cache and now - _links_cache_ttl.get(sku, 0) < _LINKS_CACHE_SECONDS:
        return {"ok": True, "sku": sku, "links": _links_cache[sku], "cached": True}
    links = {}
    try:
        client = BlingClient()
        busca = client.get_product_by_sku(sku)
        if busca.get("encontrado"):
            produto_id = busca.get("produto", {}).get("id")
            if produto_id:
                links["bling"] = f"https://www.bling.com.br/produtos.php#edit/{produto_id}"
    except Exception:
        pass
    # ML - busca MLB IDs pelo SKU
    try:
        import json as _j, requests as _rq
        _ml_tokens = _j.loads((BASE_DIR / "data" / "ml_tokens.json").read_text(encoding="utf-8"))
        _ml_token = _ml_tokens.get("access_token", "")
        _ml_r = _rq.get(
            f"https://api.mercadolibre.com/users/733168645/items/search?seller_custom_field={sku}",
            headers={"Authorization": f"Bearer {_ml_token}"},
            timeout=8
        )
        if _ml_r.status_code == 200:
            items = _ml_r.json().get("results", [])
            if items:
                links["ml"] = f"https://www.mercadolivre.com.br/anuncios/{items[0]}/editar"
    except Exception:
        pass
    # Amazon - link com SKU no inventário
    links["amazon"] = f"https://sellercentral.amazon.com.br/myinventory/inventory?searchField=all&searchTerm={sku}"
    # Shopify - busca product_id pelo SKU via API
    try:
        import requests as _req, json as _json
        _cfg = _json.loads((BASE_DIR / "data" / "shopify_config.json").read_text(encoding="utf-8"))
        _shop = _cfg.get("shop_url", "pknw4n-eg.myshopify.com")
        _token = _cfg.get("access_token", "")
        _r = _req.get(
            f"https://{_shop}/admin/api/2024-01/variants.json?sku={sku}&limit=1",
            headers={"X-Shopify-Access-Token": _token},
            timeout=8
        )
        if _r.status_code == 200:
            variants = _r.json().get("variants", [])
            if variants:
                product_id = variants[0].get("product_id")
                if product_id:
                    links["shopify"] = f"https://admin.shopify.com/store/pknw4n-eg/products/{product_id}"
    except Exception:
        pass
    # Salva no cache
    _links_cache[sku] = links
    _links_cache_ttl[sku] = time.time()
    return {"ok": True, "sku": sku, "links": links}

@app.post("/fila/aprovar/{item_id}")
def fila_aprovar(item_id: str):
    item = buscar_item_fila(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado na fila.")
    if item.get("status") != "pendente":
        raise HTTPException(status_code=400, detail=f"Item já com status '{item.get("status")}'.")
    if not BlingClient or not aplicar_precos_multicanal:
        raise HTTPException(status_code=500, detail="Integração de aplicação no Bling indisponível.")
    item_com_gordura = _aplicar_gordura_no_item(item)
    try:
        client = BlingClient()
        resultado = aplicar_precos_multicanal(client, item_com_gordura)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Falha ao aplicar preços no Bling: {exc}")

    # ── Tenta aplicar na Shopee se SKU estiver mapeado ──
    try:
        sku = item.get("sku") or (item.get("produto_bling") or {}).get("codigo") or ""
        marketplaces = item.get("marketplaces") or {}
        shopee_resultado = aplicar_preco_shopee_por_sku(sku, marketplaces)
        if shopee_resultado:
            resultado["shopee"] = shopee_resultado
            if shopee_resultado.get("success"):
                logger.info("Shopee: preço aplicado sku=%s item_id=%s preco=%.2f",
                            sku, shopee_resultado.get("item_id"), shopee_resultado.get("preco_aplicado", 0))
            else:
                logger.warning("Shopee: falha ao aplicar sku=%s motivo=%s", sku, shopee_resultado.get("motivo"))
    except Exception as exc_shopee:
        logger.warning("Shopee: exceção ao aplicar preço sku=%s: %s", item.get("sku"), exc_shopee)

    agora = datetime.utcnow().isoformat()
    atualizar_status_fila(item_id, "aprovado", resultado=resultado)
    _append_jsonl(LOG_PATH, {"evento": "fila_aprovado", "item_id": item_id, "quando": agora})
    logger.info("Item aprovado: id=%s sku=%s estrategia=%s", item_id, item.get("sku"), resultado.get("estrategia"))
    return {"ok": True, "message": "Preços aplicados no Bling.", "resultado": resultado, "stats": stats_fila()}



@app.post("/fila/completar/{item_id}")
async def fila_completar(item_id: str, request: Request):
    """Salva peso ou custo localmente para produto incompleto e agenda recalculo."""
    import json as _json
    data = await request.json()
    tipo = data.get("tipo")  # "peso" ou "custo"
    valor = float(data.get("valor", 0))
    if tipo not in ("peso", "custo") or valor <= 0:
        raise HTTPException(status_code=400, detail="Tipo ou valor inv\u00e1lido.")
    item = buscar_item_fila(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item n\u00e3o encontrado na fila.")
    sku = item.get("sku")
    if not sku:
        raise HTTPException(status_code=400, detail="SKU n\u00e3o encontrado no item.")
    try:
        if tipo == "peso":
            _override_path = BASE_DIR / "data" / "peso_override.json"
            _overrides = {}
            if _override_path.exists():
                try:
                    _overrides = _json.loads(_override_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            _overrides[str(sku)] = valor
            _override_path.write_text(_json.dumps(_overrides, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("Peso override salvo: SKU %s = %s kg", sku, valor)
        else:
            _override_path = BASE_DIR / "data" / "custo_override.json"
            _overrides = {}
            if _override_path.exists():
                try:
                    _overrides = _json.loads(_override_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            # Salva override para o SKU principal
            _overrides[str(sku)] = {"custo": valor, "origem": "manual_fila"}
            # Se for composição, salva também para o componente e tenta atualizar no Bling
            dados_inc = item.get("dados_incompletos") or {}
            comps_sem_custo = dados_inc.get("componentes_sem_custo") or []
            if comps_sem_custo:
                comp = comps_sem_custo[0]
                comp_sku = comp.get("sku")
                comp_id = comp.get("id")
                if comp_sku:
                    _overrides[str(comp_sku)] = {"custo": valor, "origem": "manual_fila"}
                    logger.info("Custo override componente: SKU %s = R$%s", comp_sku, valor)
                # Tenta atualizar custo do componente no Bling via fornecedor
                if comp_id and BlingClient:
                    try:
                        _bling = BlingClient()
                        _prod_comp = _bling.get_product(int(comp_id))
                        _forn = _prod_comp.get("fornecedor") or {}
                        if _forn.get("id"):
                            _forn_patch = {**_forn, "precoCusto": valor, "precoCompra": valor}
                            _patch = {k: v for k, v in _prod_comp.items() if k not in ("estoque", "variacoes", "estrutura", "midia")}
                            _patch["fornecedor"] = _forn_patch
                            _bling.update_product(int(comp_id), _patch)
                            logger.info("Custo componente atualizado no Bling: id=%s valor=%s", comp_id, valor)
                    except Exception as _e:
                        logger.warning("Erro ao atualizar componente %s no Bling: %s", comp_id, _e)
            _override_path.write_text(_json.dumps(_overrides, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("Custo override salvo: SKU %s = R$%s", sku, valor)
        atualizar_status_fila(item_id, "rejeitado", resultado={"motivo": f"{tipo} preenchido: {valor}"})
        return {"ok": True, "mensagem": f"{tipo.capitalize()} salvo. O produto ser\u00e1 recalculado no pr\u00f3ximo ciclo do scheduler."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Erro ao completar produto %s: %s", sku, e)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/fila/rejeitar/{item_id}")
def fila_rejeitar(item_id: str, payload: dict = Body(default={})):
    item = buscar_item_fila(item_id)
    if not item: raise HTTPException(status_code=404, detail="Item não encontrado na fila.")
    agora = datetime.utcnow().isoformat()
    motivo = payload.get("motivo") or "Rejeitado manualmente."
    atualizar_status_fila(item_id, "rejeitado", resultado={"motivo": motivo})
    _append_jsonl(LOG_PATH, {"evento":"fila_rejeitado","item_id":item_id,"quando":agora,"motivo":motivo})
    logger.info("Item rejeitado: id=%s sku=%s motivo=%s", item_id, item.get("sku"), motivo)
    return {"ok":True,"message":"Item marcado como rejeitado.","stats":stats_fila()}

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# FASE 5 â€” Integração Comercial
# Cole este bloco no app.py logo antes de:
#   
def _aplicar_gordura_no_item(item: dict) -> dict:
    import copy
    item_mod = copy.deepcopy(item)
    cfg = carregar_integracao_cfg()
    gordura_por_canal = cfg.get("gordura_por_canal", {})
    arredondamento = str(cfg.get("arredondamento", "90"))
    marketplaces = item_mod.get("marketplaces", {})
    if isinstance(marketplaces, dict):
        for canal_key, dados in marketplaces.items():
            if not isinstance(dados, dict): continue
            preco_calculado = float(dados.get("preco_promocional") or dados.get("preco_final") or dados.get("preco") or 0)
            if preco_calculado <= 0: continue
            gordura = _buscar_gordura_canal(canal_key, gordura_por_canal)
            preco_virtual = calcular_preco_virtual(preco_calculado, gordura, arredondamento)
            dados["preco_promocional"] = round(preco_calculado, 2)
            dados["preco"] = preco_virtual
            dados["preco_virtual"] = preco_virtual
            dados["gordura_aplicada"] = gordura
    itens_lista = item_mod.get("itens", [])
    if isinstance(itens_lista, list):
        for it in itens_lista:
            if not isinstance(it, dict): continue
            canal_key = it.get("canal", "")
            preco_calculado = float(it.get("preco_promocional") or it.get("preco_final") or it.get("preco") or 0)
            if preco_calculado <= 0: continue
            gordura = _buscar_gordura_canal(canal_key, gordura_por_canal)
            preco_virtual = calcular_preco_virtual(preco_calculado, gordura, arredondamento)
            it["preco_promocional"] = round(preco_calculado, 2)
            it["preco"] = preco_virtual
            it["preco_virtual"] = preco_virtual
    return item_mod


def _buscar_gordura_canal(canal_key: str, gordura_por_canal: dict) -> dict:
    padrao = {"tipo": "percentual", "valor": 20.0}
    if canal_key in gordura_por_canal: return gordura_por_canal[canal_key]
    def _norm(s): return s.lower().replace(" ", "_").replace("-", "_")
    canal_norm = _norm(canal_key)
    for nome, gordura in gordura_por_canal.items():
        if _norm(nome) == canal_norm: return gordura
    aliases = {
        "mercado_livre_classico": ["ml_classico","mercadolivre_classico","mercado livre classico"],
        "mercado_livre_premium": ["ml_premium","mercadolivre_premium","mercado livre premium"],
        "shopee": ["shopee"], "amazon": ["amazon"], "shein": ["shein"], "shopify": ["shopify","shopfy"],
    }
    for chave_cfg, alias_list in aliases.items():
        if canal_norm in [_norm(a) for a in alias_list]:
            if chave_cfg in gordura_por_canal: return gordura_por_canal[chave_cfg]
    return padrao


def _verificar_assinatura_bling(body_bytes: bytes, header: str) -> bool:
    """Valida HMAC-SHA256 do webhook Bling (X-Bling-Signature-256: sha256=<hex>)."""
    secret = os.getenv("BLING_WEBHOOK_SECRET", "")
    if not secret or not header:
        return True  # Sem segredo configurado: aceita tudo
    try:
        expected = "sha256=" + _hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
        return _hmac.compare_digest(expected, header)
    except Exception:
        return False


@app.post("/webhooks/bling")
async def webhook_bling(request: Request):
    raw = await request.body()
    sig = request.headers.get("X-Bling-Signature-256", "")
    if not _verificar_assinatura_bling(raw, sig):
        logger.warning("Webhook Bling: assinatura inválida — ignorando")
        return {"ok": False, "erro": "assinatura inválida"}
    try:
        body = json.loads(raw)
    except Exception:
        body = {}
    evento = body.get("evento") or body.get("event") or "desconhecido"
    logger.info("Webhook Bling recebido: evento=%s", evento)
    _append_jsonl(LOG_PATH, {"evento":"webhook_bling","tipo":evento,"quando":datetime.utcnow().isoformat(),"payload":body})
    return {"ok": True, "recebido": True}

@app.post("/webhooks/ml")
async def webhook_ml(request: Request, background_tasks: BackgroundTasks):
    """
    Recebe notificacoes do Mercado Livre.
    O ML envia POST: {"resource": "...", "user_id": 123, "topic": "price_suggestion"}
    Responde 200 imediatamente; processamento pesado vai para background task.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    topic = body.get("topic") or body.get("type") or "desconhecido"
    resource = body.get("resource", "")
    user_id = body.get("user_id", "")
    logger.info("Webhook ML recebido: topic=%s resource=%s user_id=%s", topic, resource, user_id)
    _append_jsonl(LOG_PATH, {
        "evento": "webhook_ml",
        "topic": topic,
        "resource": resource,
        "user_id": user_id,
        "quando": datetime.utcnow().isoformat(),
        "payload": body,
    })

    # Processa sugestoes de preco em background (nao bloqueia resposta ao ML)
    if topic == "price_suggestion" and resource:
        def _processar_bg():
            try:
                from services.ml_price_suggestions import processar_price_suggestion
                regras = carregar_regras(apenas_ativas=True)
                client = BlingClient() if BlingClient else None
                processar_price_suggestion(
                    resource=resource,
                    user_id=str(user_id),
                    bling_client=client,
                    regras=regras,
                )
            except Exception as exc:
                import traceback
                logger.warning("Erro bg price_suggestion %s: %s\n%s", resource, exc, traceback.format_exc())
        background_tasks.add_task(_processar_bg)

    # Captura candidatos a promocao — ML envia preco sugerido diretamente!
    elif topic == "public_candidates" and resource:
        def _processar_candidate_bg():
            try:
                from services.ml_price_suggestions import _buscar_candidate_promo, _load_sugestoes, _save_sugestoes
                data = _buscar_candidate_promo(resource)
                if data:
                    # Salva o payload bruto para analise posterior
                    _append_jsonl(LOG_PATH, {
                        "evento": "public_candidate_ml",
                        "resource": resource,
                        "user_id": str(user_id),
                        "quando": datetime.utcnow().isoformat(),
                        "payload": data,
                    })
                    logger.info("public_candidate salvo: %s", json.dumps(data, ensure_ascii=False)[:400])
            except Exception as exc:
                logger.warning("Erro bg public_candidate %s: %s", resource, exc)
        background_tasks.add_task(_processar_candidate_bg)

    return {"ok": True}


@app.get("/marketing/ml/sugestoes")
def marketing_ml_sugestoes():
    """Retorna sugestoes de preco recebidas via webhook price_suggestion."""
    from services.ml_price_suggestions import carregar_sugestoes, resumo_sugestoes
    return {
        "ok": True,
        "resumo": resumo_sugestoes(),
        "sugestoes": carregar_sugestoes(),
    }


@app.delete("/marketing/ml/sugestoes")
def marketing_ml_sugestoes_limpar():
    """Limpa todas as sugestoes salvas."""
    from services.ml_price_suggestions import limpar_sugestoes
    limpar_sugestoes()
    return {"ok": True}

@app.post("/marketing/ml/sugestoes/limpar")
def marketing_ml_sugestoes_limpar_post():
    """Alternativa POST para limpar sugestoes (compativel com ngrok/proxies)."""
    from services.ml_price_suggestions import limpar_sugestoes
    limpar_sugestoes()
    return {"ok": True}


@app.post("/marketing/ml/sugestoes/reprocessar")
async def marketing_ml_sugestoes_reprocessar(background_tasks: BackgroundTasks):
    """Reprocessa todas as sugestoes salvas para calcular margem atual."""
    from services.ml_price_suggestions import carregar_sugestoes

    sugestoes = carregar_sugestoes()
    if not sugestoes:
        return {"ok": True, "mensagem": "Nenhuma sugestao para reprocessar."}

    def _reprocessar_bg():
        try:
            from services.ml_price_suggestions import (
                processar_price_suggestion, _load_sugestoes, _save_sugestoes
            )
            regras = carregar_regras(apenas_ativas=True)
            client = BlingClient() if BlingClient else None
            lista = _load_sugestoes()
            if not lista:
                return
            # Zera cooldown (usa timestamp antigo mas válido)
            for s in lista:
                s["recebido_em"] = "2020-01-01T00:00:00"
            _save_sugestoes(lista)
            # Reprocessa cada item
            for s in lista:
                try:
                    processar_price_suggestion(
                        resource=s.get("resource", f"/marketplace/benchmarks/items/{s['item_id']}/details"),
                        user_id=s.get("user_id", ""),
                        bling_client=client,
                        regras=regras,
                    )
                    import time as _t; _t.sleep(0.2)
                except Exception as e:
                    logger.warning("Reprocessar %s: %s", s.get("item_id"), e)
            logger.info("Reprocessamento concluido: %d sugestoes", len(lista))
        except Exception as exc:
            logger.warning("Erro reprocessar sugestoes: %s", exc)

    background_tasks.add_task(_reprocessar_bg)
    return {"ok": True, "mensagem": f"Reprocessando {len(sugestoes)} sugestão(ões) em background..."}



@app.get("/conferencia-estoque", response_class=HTMLResponse)
def conferencia_estoque_page():
    html_file = PAGES_DIR / "conferencia_estoque.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="conferencia_estoque.html não encontrado.")

@app.get("/estoque/fila")
def estoque_fila_lista(status: str = ""):
    from estoque_conferencia import carregar_fila_estoque, stats_fila_estoque
    itens = carregar_fila_estoque()
    if status:
        itens = [i for i in itens if i.get("status") == status]
    return {"itens": itens, "stats": stats_fila_estoque()}

@app.post("/estoque/conferir")
def estoque_conferir():
    from estoque_conferencia import conferir_estoques
    if not BlingClient:
        raise HTTPException(status_code=500, detail="Bling não disponível.")
    try:
        client = BlingClient()
        ml_svc = None
        try:
            from services.mercado_livre import MercadoLivreService
            ml_svc = MercadoLivreService(BASE_DIR)
        except Exception:
            pass
        return conferir_estoques(client, ml_svc)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/estoque/corrigir/{item_id}")
def estoque_corrigir(item_id: str):
    from estoque_conferencia import corrigir_item_estoque, stats_fila_estoque
    ml_svc = None
    try:
        from services.mercado_livre import MercadoLivreService
        ml_svc = MercadoLivreService(BASE_DIR)
    except Exception:
        pass
    resultado = corrigir_item_estoque(item_id, ml_svc)
    if not resultado.get("ok"):
        raise HTTPException(status_code=400, detail=resultado.get("erro","Falha ao corrigir."))
    return {"ok": True, "resultado": resultado, "stats": stats_fila_estoque()}

@app.post("/estoque/ignorar/{item_id}")
def estoque_ignorar(item_id: str):
    from estoque_conferencia import ignorar_item_estoque, stats_fila_estoque
    resultado = ignorar_item_estoque(item_id)
    return {"ok": resultado.get("ok"), "stats": stats_fila_estoque()}

@app.post("/estoque/limpar-resolvidos")
def estoque_limpar_resolvidos():
    from estoque_conferencia import carregar_fila_estoque, salvar_fila_estoque, stats_fila_estoque
    itens = carregar_fila_estoque()
    itens = [i for i in itens if i.get("status") == "pendente"]
    salvar_fila_estoque(itens)
    return {"ok": True, "stats": stats_fila_estoque()}


@app.post("/shopify-flow/pricing-suggestion")
async def shopify_flow_pricing(request: Request):
    """
    Endpoint para Shopify Flow.
    Recebe payload com SKU do produto, busca no Bling,
    calcula preços pelo motor e retorna sugestão por canal.
    Não aplica preços automaticamente.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    # Extrai SKU do payload Shopify (vários formatos possíveis)
    sku = (
        body.get("sku")
        or body.get("variant_sku")
        or body.get("product", {}).get("variants", [{}])[0].get("sku", "") if isinstance(body.get("product"), dict) else ""
        or body.get("codigo")
        or ""
    )
    sku = str(sku).strip()

    if not sku:
        return {
            "ok": False,
            "erro": "SKU não encontrado no payload.",
            "payload_recebido": body,
        }

    if not BlingClient or not montar_precificacao_bling:
        return {"ok": False, "erro": "Motor de precificação indisponível."}

    # Carrega configuração comercial
    cfg = carregar_integracao_cfg()
    objetivo = cfg.get("objetivo", "lucro_liquido")
    tipo_alvo = cfg.get("tipo_alvo", "percentual")
    valor_alvo = float(cfg.get("valor_alvo", 30))
    arredondamento = str(cfg.get("arredondamento", "90"))

    try:
        regras = carregar_regras(apenas_ativas=True)

        resultado = montar_precificacao_bling(
            regras=regras,
            criterio="sku",
            valor_busca=sku,
            embalagem=float(body.get("embalagem", 1)),
            imposto=float(body.get("imposto", 4)),
            quantidade=int(body.get("quantidade", 1)),
            objetivo=objetivo,
            tipo_alvo=tipo_alvo,
            valor_alvo=valor_alvo,
            peso_override=float(body.get("peso_override", 0)),
            arredondamento=arredondamento,
            regra_estoque=cfg.get("regra_estoque"),
        )
    except Exception as exc:
        logger.warning("Shopify Flow: erro no motor para SKU=%s: %s", sku, exc)
        return {"ok": False, "sku": sku, "erro": str(exc)}

    if resultado.get("erro"):
        return {"ok": False, "sku": sku, "erro": resultado["erro"]}

    # Monta sugestão por canal com gordura
    gordura_por_canal = cfg.get("gordura_por_canal", {})
    itens = (resultado.get("integracao") or {}).get("itens") or resultado.get("itens") or []
    sugestoes = []

    for item in itens:
        if not isinstance(item, dict):
            continue
        canal = item.get("canal", "")
        preco_calculado = float(
            item.get("preco_promocional") or item.get("preco_final") or item.get("preco") or 0
        )
        if preco_calculado <= 0:
            continue

        gordura = gordura_por_canal.get(canal, {"tipo": "percentual", "valor": 20})
        preco_virtual = calcular_preco_virtual(preco_calculado, gordura, arredondamento)

        sugestoes.append({
            "canal": canal,
            "preco_calculado": round(preco_calculado, 2),
            "preco_virtual": preco_virtual,
            "lucro_liquido": float(item.get("lucro_liquido") or item.get("lucro") or 0),
            "margem": float(item.get("margem") or item.get("margem_liquida_percentual") or 0),
        })

    # Sugestão do canal Shopify especificamente
    shopify_sugestao = next((s for s in sugestoes if "shopify" in s["canal"].lower()), None)

    produto = resultado.get("produto_bling") or {}
    logger.info("Shopify Flow: SKU=%s canais=%d", sku, len(sugestoes))
    _append_jsonl(LOG_PATH, {
        "evento": "shopify_flow_suggestion",
        "sku": sku,
        "quando": datetime.utcnow().isoformat(),
        "canais": len(sugestoes),
    })

    return {
        "ok": True,
        "sku": sku,
        "produto": {
            "nome": produto.get("nome") or produto.get("descricao") or "",
            "codigo": produto.get("codigo") or sku,
        },
        "sugestao_shopify": shopify_sugestao,
        "todos_canais": sugestoes,
        "objetivo_usado": objetivo,
        "valor_alvo_usado": valor_alvo,
    }


@app.get("/auditoria-automatica", response_class=HTMLResponse)
def auditoria_automatica_page():
    html_file = PAGES_DIR / "auditoria_automatica.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="auditoria_automatica.html não encontrado.")

@app.get("/auditoria/fila")
def auditoria_fila_lista(status: str = "", tipo: str = ""):
    from auditoria_automatica import carregar_fila_auditoria, stats_fila_auditoria
    itens = carregar_fila_auditoria()
    if status:
        itens = [i for i in itens if i.get("status") == status]
    if tipo:
        itens = [i for i in itens if i.get("tipo") == tipo]
    return {"itens": itens, "stats": stats_fila_auditoria()}

@app.post("/auditoria/rodar")
def auditoria_rodar():
    from auditoria_automatica import rodar_auditoria
    if not BlingClient:
        raise HTTPException(status_code=500, detail="Bling não disponível.")
    try:
        client = BlingClient()
        ml_svc = None
        try:
            from services.mercado_livre import MercadoLivreService
            ml_svc = MercadoLivreService(BASE_DIR)
        except Exception:
            pass
        return rodar_auditoria(client, ml_svc)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/auditoria/corrigir/{item_id}")
def auditoria_corrigir(item_id: str):
    from auditoria_automatica import carregar_fila_auditoria, corrigir_estoque, corrigir_preco, stats_fila_auditoria
    fila = carregar_fila_auditoria()
    item = next((i for i in fila if i.get("id") == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    ml_svc = None
    try:
        from services.mercado_livre import MercadoLivreService
        ml_svc = MercadoLivreService(BASE_DIR)
    except Exception:
        pass
    if item.get("tipo") == "estoque":
        resultado = corrigir_estoque(item_id, ml_svc)
    else:
        resultado = corrigir_preco(item_id, ml_svc)
    if not resultado.get("ok"):
        raise HTTPException(status_code=400, detail=resultado.get("erro", "Falha ao corrigir."))
    return {"ok": True, "resultado": resultado, "stats": stats_fila_auditoria()}

@app.post("/auditoria/ignorar/{item_id}")
def auditoria_ignorar(item_id: str):
    from auditoria_automatica import ignorar_item, stats_fila_auditoria
    resultado = ignorar_item(item_id)
    return {"ok": resultado.get("ok"), "stats": stats_fila_auditoria()}

@app.post("/auditoria/limpar-resolvidos")
def auditoria_limpar():
    from auditoria_automatica import limpar_resolvidos, stats_fila_auditoria
    removidos = limpar_resolvidos()
    return {"ok": True, "removidos": removidos, "stats": stats_fila_auditoria()}


@app.get("/auditoria/ml-estoque")
def auditoria_ml_estoque_lista(status: str = "", tipo: str = ""):
    from ml_estoque_conferencia import carregar_fila_estoque_ml, stats_fila_estoque_ml
    itens = carregar_fila_estoque_ml()
    if status:
        itens = [i for i in itens if i.get("status") == status]
    if tipo:
        itens = [i for i in itens if i.get("tipo") == tipo]
    return {"itens": itens, "stats": stats_fila_estoque_ml()}

@app.post("/auditoria/ml-estoque/conferir")
def auditoria_ml_estoque_conferir():
    if not BlingClient:
        raise HTTPException(status_code=500, detail="Bling não disponível.")
    if _conf_ml["rodando"]:
        return {"ok": True, "em_andamento": True, "message": "Conferência já em andamento.", "estado": _conf_ml}
    _conf_ml.update({
        "rodando": True, "concluido": False, "erro": None,
        "pagina": 0, "verificados": 0, "divergencias": 0,
        "sem_sku": 0, "erros": 0, "resultado": None,
        "iniciado_em": datetime.utcnow().isoformat(), "concluido_em": None,
    })
    threading.Thread(target=_rodar_conf_ml_bg, daemon=True).start()
    return {"ok": True, "em_andamento": True, "message": "Conferência iniciada em background."}

@app.get("/auditoria/ml-estoque/conferir/status")
def auditoria_ml_estoque_conferir_status():
    return {"ok": True, **_conf_ml}

@app.post("/auditoria/ml-estoque/corrigir/{item_id}")
def auditoria_ml_estoque_corrigir(item_id: str):
    from ml_estoque_conferencia import corrigir_estoque_ml
    resultado = corrigir_estoque_ml(item_id)
    if not resultado.get("ok"):
        raise HTTPException(status_code=400, detail=resultado.get("erro", "Falha."))
    return {"ok": True}

@app.post("/auditoria/ml-estoque/cadastrar-sku/{item_id}")
async def auditoria_ml_cadastrar_sku(item_id: str, request: Request):
    from ml_estoque_conferencia import cadastrar_sku_ml
    data = await request.json()
    sku = data.get("sku", "").strip()
    if not sku:
        raise HTTPException(status_code=400, detail="SKU obrigatório.")
    client = BlingClient() if BlingClient else None
    resultado = cadastrar_sku_ml(item_id, sku, client)
    if not resultado.get("ok"):
        raise HTTPException(status_code=400, detail=resultado.get("erro", "Falha."))
    return resultado

@app.post("/auditoria/ml-estoque/ignorar/{item_id}")
def auditoria_ml_estoque_ignorar(item_id: str):
    from ml_estoque_conferencia import ignorar_item_ml
    return ignorar_item_ml(item_id)

@app.post("/auditoria/ml-estoque/limpar-resolvidos")
def auditoria_ml_limpar_resolvidos():
    from ml_estoque_conferencia import carregar_fila_estoque_ml, salvar_fila_estoque_ml, stats_fila_estoque_ml
    itens = carregar_fila_estoque_ml()
    itens = [i for i in itens if i.get("status") == "pendente"]
    salvar_fila_estoque_ml(itens)
    return {"ok": True, "stats": stats_fila_estoque_ml()}

@app.post("/auditoria/ml-estoque/limpar-tudo")
def auditoria_ml_limpar_tudo():
    from ml_estoque_conferencia import salvar_fila_estoque_ml, stats_fila_estoque_ml
    salvar_fila_estoque_ml([])
    return {"ok": True, "stats": stats_fila_estoque_ml()}


@app.get("/integracoes", response_class=HTMLResponse)
def integracoes_page():
    html_file = PAGES_DIR / "integracoes.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404)



@app.get("/ml/status")
def ml_status_endpoint():
    import json
    from pathlib import Path as _P
    tp = _P("data/ml_tokens.json")
    if not tp.exists(): return {"connected": False}
    tokens = json.loads(tp.read_text(encoding="utf-8"))
    at = tokens.get("access_token", "")
    return {"connected": bool(at) and at != ".", "seller_id": tokens.get("user_id"), "expires_at": tokens.get("expires_at")}

@app.get("/auditoria/shopify")
def auditoria_shopify_lista(status: str = "", tipo: str = ""):
    from shopify_conferencia import carregar_fila_shopify, stats_fila_shopify
    itens = carregar_fila_shopify()
    if status:
        itens = [i for i in itens if i.get("status") == status]
    if tipo:
        itens = [i for i in itens if i.get("tipo") == tipo]
    return {"itens": itens, "stats": stats_fila_shopify()}

@app.post("/auditoria/shopify/conferir")
def auditoria_shopify_conferir(tipo: str = ""):
    from shopify_conferencia import conferir_shopify
    if not BlingClient:
        raise HTTPException(status_code=500, detail="Bling nao disponivel.")
    global _scheduler_pausado
    _scheduler_pausado = True
    try:
        client = BlingClient()
        resultado = conferir_shopify(client, tipo=tipo)
        return resultado
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        _scheduler_pausado = False

@app.post("/auditoria/shopify/corrigir/{item_id}")
def auditoria_shopify_corrigir(item_id: str):
    from shopify_conferencia import corrigir_shopify
    resultado = corrigir_shopify(item_id)
    if not resultado.get("ok"):
        raise HTTPException(status_code=400, detail=resultado.get("erro", "Falha."))
    return {"ok": True}

@app.post("/auditoria/shopify/ignorar/{item_id}")
def auditoria_shopify_ignorar(item_id: str):
    from shopify_conferencia import ignorar_shopify
    return ignorar_shopify(item_id)

@app.post("/auditoria/shopify/limpar-resolvidos")
def auditoria_shopify_limpar():
    from shopify_conferencia import carregar_fila_shopify, salvar_fila_shopify, stats_fila_shopify
    itens = carregar_fila_shopify()
    itens = [i for i in itens if i.get("status") == "pendente"]
    salvar_fila_shopify(itens)
    return {"ok": True, "stats": stats_fila_shopify()}

@app.post("/auditoria/shopify/token")
async def auditoria_shopify_token(request: Request):
    from shopify_conferencia import salvar_shopify_token
    data = await request.json()
    token = data.get("access_token", "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Token invalido.")
    salvar_shopify_token(token)
    return {"ok": True}


def _shopify_redirect_uri(request: Request) -> str:
    """Constrói redirect_uri correto mesmo atrás de proxy HTTPS (Railway/ngrok)."""
    env_url = os.getenv("SHOPIFY_CALLBACK_URL", "")
    if env_url:
        return env_url
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    return f"{scheme}://{host}/shopify/callback"

@app.get("/shopify/auth")
def shopify_auth(request: Request):
    from shopify_oauth import gerar_url_auth
    from fastapi.responses import RedirectResponse
    redirect_uri = _shopify_redirect_uri(request)
    url = gerar_url_auth(redirect_uri)
    return RedirectResponse(url)

@app.get("/shopify/callback")
def shopify_callback(code: str = "", state: str = "", request: Request = None):
    from shopify_oauth import processar_callback
    from fastapi.responses import HTMLResponse
    redirect_uri = _shopify_redirect_uri(request)
    resultado = processar_callback(code, state, redirect_uri)
    if resultado.get("ok"):
        return HTMLResponse("<h2>✅ Shopify conectado! Token salvo com sucesso.</h2><p><a href='/integracoes'>Voltar para Integrações</a></p>")
    return HTMLResponse(f"<h2>❌ Erro: {resultado.get('erro')}</h2>")

@app.get("/shopify/status")
def shopify_status():
    import json
    from pathlib import Path as _P
    cfg = _P("data/shopify_config.json")
    if not cfg.exists(): return {"connected": False}
    data = json.loads(cfg.read_text(encoding="utf-8"))
    token = data.get("access_token", "")
    return {"connected": bool(token) and token != ".", "scope": data.get("scope", ""), "salvo_em": data.get("salvo_em")}

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

INTEGRACAO_CFG_PATH = DATA_DIR / "integracao_comercial.json"

DEFAULT_INTEGRACAO_CFG = {
    "objetivo": "lucro_liquido",
    "tipo_alvo": "percentual",
    "valor_alvo": 30.0,
    "arredondamento": "90",
    "modo_aprovacao": "manual",
    "fila_auto_ao_calcular": True,
    "gordura_por_canal": {
        "Mercado Livre Classico": {"tipo": "percentual", "valor": 20.0},
        "Mercado Livre Premium": {"tipo": "percentual", "valor": 20.0},
        "Shopee": {"tipo": "percentual", "valor": 20.0},
        "Amazon": {"tipo": "percentual", "valor": 20.0},
        "Shein": {"tipo": "percentual", "valor": 20.0},
        "Shopify": {"tipo": "percentual", "valor": 20.0},
    },
    "forcas_canais": {
        "Mercado Livre Classico": 0.8,
        "Mercado Livre Premium": 0.75,
        "Shopee": 0.6,
        "Amazon": 0.7,
        "Shein": 0.55,
        "Shopify": 0.65,
    },
    "peso_forca": 0.4,
    "peso_equilibrio": 0.4,
    "peso_lucro": 0.2,
    "regra_estoque": {"ativo": False, "limite": 2, "tipo": "percentual", "valor": 0},
    "modo_auto": False,
    "auto_margem_ok": 25.0,
    "auto_margem_fila": 15.0,
}


def carregar_integracao_cfg() -> dict:
    data = _load_json(INTEGRACAO_CFG_PATH, {})
    cfg = json.loads(json.dumps(DEFAULT_INTEGRACAO_CFG))
    if isinstance(data, dict):
        cfg.update(data)
        # merge profundo para sub-dicts
        if "gordura_por_canal" in data:
            cfg["gordura_por_canal"] = {
                **DEFAULT_INTEGRACAO_CFG["gordura_por_canal"],
                **data["gordura_por_canal"],
            }
        if "forcas_canais" in data:
            cfg["forcas_canais"] = {
                **DEFAULT_INTEGRACAO_CFG["forcas_canais"],
                **data["forcas_canais"],
            }
        if "regra_estoque" in data:
            cfg["regra_estoque"] = {
                **DEFAULT_INTEGRACAO_CFG["regra_estoque"],
                **data["regra_estoque"],
            }
    return cfg


def calcular_preco_virtual(preco_calculado: float, gordura: dict, arredondamento: str = "90") -> float:
    """Aplica a gordura sobre o preço calculado e arredonda."""
    tipo = gordura.get("tipo", "percentual")
    valor = float(gordura.get("valor", 20))

    if tipo == "percentual":
        virtual = preco_calculado * (1 + valor / 100)
    else:
        virtual = preco_calculado + valor

    return _arredondar_preco(virtual, arredondamento)


def _arredondar_preco(v: float, modo: str) -> float:
    if modo == "sem":
        return round(v, 2)
    sufixo = int(modo) / 100  # "90" â†’ 0.90
    base = int(v)
    proposto = base + sufixo
    if proposto >= v:
        return round(proposto, 2)
    return round(base + 1 + sufixo, 2)


# â”€â”€â”€ ROTA: página de integração comercial â”€â”€â”€
@app.get("/integracao-comercial", response_class=HTMLResponse)
def integracao_comercial_page():
    html_file = PAGES_DIR / "integracao_comercial.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="integracao_comercial.html não encontrado.")


# â”€â”€â”€ ROTA: GET config â”€â”€â”€
@app.get("/config/integracao-comercial")
def get_integracao_config():
    return carregar_integracao_cfg()


# â”€â”€â”€ ROTA: POST config â”€â”€â”€
@app.post("/config/integracao-comercial")
async def set_integracao_config(request: Request):
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Payload inválido.")

    cfg_atual = carregar_integracao_cfg()

    # Campos simples
    for campo in ["objetivo", "tipo_alvo", "arredondamento", "modo_aprovacao"]:
        if campo in data:
            cfg_atual[campo] = str(data[campo])

    for campo in ["valor_alvo", "peso_forca", "peso_equilibrio", "peso_lucro", "auto_margem_ok", "auto_margem_fila"]:
        if campo in data:
            try:
                cfg_atual[campo] = float(data[campo])
            except (TypeError, ValueError):
                pass

    for campo in ["fila_auto_ao_calcular", "modo_auto", "ml_api_real"]:
        if campo in data:
            cfg_atual[campo] = bool(data[campo])
    for campo in ["embalagem_padrao", "imposto_padrao"]:
        if campo in data:
            try:
                cfg_atual[campo] = float(data[campo])
            except (TypeError, ValueError):
                pass

    # Sub-dicts
    if "gordura_por_canal" in data and isinstance(data["gordura_por_canal"], dict):
        cfg_atual["gordura_por_canal"] = {
            **cfg_atual.get("gordura_por_canal", {}),
            **data["gordura_por_canal"],
        }

    if "forcas_canais" in data and isinstance(data["forcas_canais"], dict):
        cfg_atual["forcas_canais"] = {
            **cfg_atual.get("forcas_canais", {}),
            **data["forcas_canais"],
        }

    if "regra_estoque" in data and isinstance(data["regra_estoque"], dict):
        cfg_atual["regra_estoque"] = {
            **cfg_atual.get("regra_estoque", {}),
            **data["regra_estoque"],
        }

    _save_json(INTEGRACAO_CFG_PATH, cfg_atual)
    logger.info("Configuração de integração comercial atualizada: objetivo=%s", cfg_atual.get("objetivo"))

    return {"ok": True, "config": cfg_atual}


# â”€â”€â”€ ROTA: calcular preço virtual para um canal â”€â”€â”€
@app.post("/config/calcular-preco-virtual")
async def calcular_preco_virtual_endpoint(request: Request):
    """Dado um preço calculado, retorna o preço virtual por canal com a gordura configurada."""
    data = await request.json()
    preco = float(data.get("preco", 0))
    if preco <= 0:
        raise HTTPException(status_code=400, detail="Preço inválido.")

    cfg = carregar_integracao_cfg()
    gordura_por_canal = cfg.get("gordura_por_canal", {})
    arredondamento = str(data.get("arredondamento") or cfg.get("arredondamento", "90"))

    resultado = {}
    for canal, gordura in gordura_por_canal.items():
        virtual = calcular_preco_virtual(preco, gordura, arredondamento)
        dif_nominal = round(virtual - preco, 2)
        dif_pct = round((dif_nominal / preco) * 100, 2) if preco > 0 else 0
        resultado[canal] = {
            "preco_calculado": round(preco, 2),
            "preco_virtual": virtual,
            "diferenca_nominal": dif_nominal,
            "diferenca_percentual": dif_pct,
            "gordura": gordura,
        }

    return {"ok": True, "canais": resultado}


@app.get("/auditoria/mp-status")
def auditoria_mp_status():
    import json as _json
    from pathlib import Path as _P
    mp = _P("data/mp_token.json")
    data = _json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
    token = data.get("access_token", "")
    return {"configurado": bool(token) and token != ".", "salvo_em": data.get("salvo_em")}
if not FILA_PATH.exists(): _save_json(FILA_PATH, [])
if not CFG_PATH.exists(): _save_json(CFG_PATH, DEFAULT_CFG)

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# MÓDULO REGRAS
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

MAPA_CANAIS_EXCEL = {
    "Classico": "Mercado Livre Classico",
    "Classic": "Mercado Livre Classico",
    "Premium": "Mercado Livre Premium",
    "Shopfy": "Shopify",
}

def _normalizar_canal_excel(canal: str) -> str:
    s = str(canal or "").strip()
    return MAPA_CANAIS_EXCEL.get(s, s)

def _para_float(v, default=0.0):
    if v is None or v == "": return default
    try: return float(v)
    except Exception:
        try: return float(str(v).strip().replace(".", "").replace(",", "."))
        except Exception: return default

@app.get("/regras", response_class=HTMLResponse)
def regras_page():
    html_file = PAGES_DIR / "regras.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="pages/regras.html não encontrado.")

@app.get("/regras/listar")
def regras_listar():
    regras = carregar_regras()
    for i, r in enumerate(regras):
        if isinstance(r, dict): r["_idx"] = i
    return {"regras": regras, "total": len(regras)}

@app.post("/regras/adicionar")
def regras_adicionar(payload: dict = Body(...)):
    nova = {
        "canal": str(payload.get("canal", "")).strip(),
        "peso_min": _para_float(payload.get("peso_min"), 0),
        "peso_max": _para_float(payload.get("peso_max"), 999999),
        "preco_min": _para_float(payload.get("preco_min"), 0),
        "preco_max": _para_float(payload.get("preco_max"), 999999999),
        "taxa_fixa": _para_float(payload.get("taxa_fixa"), 0),
        "taxa_frete": _para_float(payload.get("taxa_frete"), 0),
        "comissao": _para_float(payload.get("comissao"), 0),
        "ativo": bool(payload.get("ativo", True)),
    }
    if not nova["canal"]:
        raise HTTPException(status_code=400, detail="Canal obrigatório.")
    novo_id = inserir_regra(nova)
    return {"ok": True, "id": novo_id, "total": len(carregar_regras())}

@app.post("/regras/editar/{idx}")
def regras_editar(idx: int, payload: dict = Body(...)):
    nova = {
        "canal": str(payload.get("canal", "")).strip(),
        "peso_min": _para_float(payload.get("peso_min"), 0),
        "peso_max": _para_float(payload.get("peso_max"), 999999),
        "preco_min": _para_float(payload.get("preco_min"), 0),
        "preco_max": _para_float(payload.get("preco_max"), 999999999),
        "taxa_fixa": _para_float(payload.get("taxa_fixa"), 0),
        "taxa_frete": _para_float(payload.get("taxa_frete"), 0),
        "comissao": _para_float(payload.get("comissao"), 0),
        "ativo": bool(payload.get("ativo", True)),
    }
    ok = atualizar_regra(idx, nova)
    if not ok:
        raise HTTPException(status_code=404, detail="Regra não encontrada.")
    return {"ok": True, "total": len(carregar_regras())}

@app.delete("/regras/excluir/{idx}")
def regras_excluir(idx: int):
    ok = excluir_regra(idx)
    if not ok:
        raise HTTPException(status_code=404, detail="Regra não encontrada.")
    return {"ok": True, "total": len(carregar_regras())}

@app.post("/regras/importar-excel")
async def regras_importar_excel(file: UploadFile = File(...)):
    if not file.filename.endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Apenas arquivos .xlsx são aceitos.")
    try:
        import io
        import openpyxl
        contents = await file.read()
        wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True)
        nome_aba = next((n for n in wb.sheetnames if n.lower() in ("regras", "aba2")), wb.sheetnames[0])
        ws = wb[nome_aba]
        regras = []
        for row in ws.iter_rows(min_row=2, max_col=8, values_only=True):
            canal, peso_min, peso_max, preco_min, preco_max, taxa_fixa, taxa_frete, comissao = row
            canal = _normalizar_canal_excel(canal)
            if not canal: continue
            regras.append({
                "canal": canal,
                "peso_min": _para_float(peso_min, 0),
                "peso_max": _para_float(peso_max, 999999),
                "preco_min": _para_float(preco_min, 0),
                "preco_max": _para_float(preco_max, 999999999),
                "taxa_fixa": _para_float(taxa_fixa, 0),
                "taxa_frete": _para_float(taxa_frete, 0),
                "comissao": _para_float(comissao, 0),
                "ativo": True,
            })
        if not regras:
            raise HTTPException(status_code=400, detail="Nenhuma regra encontrada na planilha. Verifique se a aba se chama 'Regras' ou 'Aba2'.")
        substituir_todas_regras(regras)
        return {"ok": True, "total": len(regras), "aba": nome_aba}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erro ao processar Excel: {e}")

@app.get("/regras/modelo/download")
def regras_modelo_download():
    modelo = BASE_DIR / "Simulador_modelo.xlsx"
    if not modelo.exists():
        raise HTTPException(status_code=404, detail="Arquivo modelo não encontrado.")
    return FileResponse(
        path=str(modelo),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="Shinsei_Regras_Modelo.xlsx",
    )

@app.get("/auditoria/estoque-negativo")
def auditoria_estoque_negativo_lista(status: str = ""):
    from pathlib import Path as _P
    import json as _j
    fila_path = _P("data/fila_estoque_negativo.json")
    itens = _j.loads(fila_path.read_text(encoding="utf-8")) if fila_path.exists() else []
    # Exibe apenas itens com estoque realmente negativo
    itens = [i for i in itens if int(i.get("estoque", 0)) < 0]
    if status:
        itens = [i for i in itens if i.get("status") == status]
    total = len(itens)
    pendentes = sum(1 for i in itens if i.get("status") == "pendente")
    return {"itens": itens, "stats": {"pendente": pendentes, "total": total}}

@app.post("/auditoria/estoque-negativo/ignorar/{item_id}")
def auditoria_estoque_negativo_ignorar(item_id: str):
    from pathlib import Path as _P
    import json as _j
    fila_path = _P("data/fila_estoque_negativo.json")
    itens = _j.loads(fila_path.read_text(encoding="utf-8")) if fila_path.exists() else []
    for i in itens:
        if i.get("id") == item_id:
            i["status"] = "ignorado"
    fila_path.write_text(_j.dumps(itens, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}

@app.post("/auditoria/estoque-negativo/limpar")
def auditoria_estoque_negativo_limpar():
    from pathlib import Path as _P
    import json as _j
    fila_path = _P("data/fila_estoque_negativo.json")
    itens = _j.loads(fila_path.read_text(encoding="utf-8")) if fila_path.exists() else []
    itens = [i for i in itens if i.get("status") == "pendente"]
    fila_path.write_text(_j.dumps(itens, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "pendentes": len(itens)}

@app.post("/auditoria/shopify/limpar-tudo")
def auditoria_shopify_limpar_tudo():
    from shopify_conferencia import salvar_fila_shopify
    salvar_fila_shopify([])
    return {"ok": True}

@app.post("/auditoria/estoque-negativo/limpar-tudo")
def auditoria_negativo_limpar_tudo():
    from pathlib import Path as _P
    import json as _j
    _P("data/fila_estoque_negativo.json").write_text("[]", encoding="utf-8")
    return {"ok": True}

@app.get("/auditoria/amazon")
def auditoria_amazon_lista(status: str = "", tipo: str = ""):
    from amazon_conferencia import carregar_fila, stats_fila
    itens = [i for i in carregar_fila() if i.get("status") in ("pendente","incompleto")]
    if status:
        itens = [i for i in itens if i.get("status") == status]
    if tipo:
        itens = [i for i in itens if i.get("tipo") == tipo]
    return {"itens": itens, "stats": stats_fila()}

@app.post("/auditoria/amazon/conferir")
def auditoria_amazon_conferir(tipo: str = ""):
    from amazon_conferencia import conferir_amazon
    from amazon_client import AmazonClient
    if not BlingClient:
        raise HTTPException(status_code=500, detail="Bling não disponível.")
    try:
        bling = BlingClient()
        amazon = AmazonClient()
        resultado = conferir_amazon(bling_client=bling, tipo=tipo)
        return resultado
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/auditoria/amazon/corrigir/{item_id}")
def auditoria_amazon_corrigir(item_id: str):
    from amazon_conferencia import carregar_fila, salvar_fila
    from datetime import datetime, timezone
    fila = carregar_fila()
    item = next((i for i in fila if i.get("id") == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado na fila.")
    if item.get("status") != "pendente":
        raise HTTPException(status_code=400, detail=f"Item já está com status '{item['status']}'.")
    try:
        from services.amazon import AmazonService
        service = AmazonService()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erro ao conectar à Amazon: {exc}")
    tipo = item.get("tipo")
    sku = item.get("sku")
    if tipo == "estoque":
        res = service.atualizar_estoque(sku, item["estoque_bling"])
    elif tipo == "preco":
        res = service.atualizar_com_retry(sku, item["preco_bling"])
    else:
        raise HTTPException(status_code=400, detail=f"Tipo desconhecido: {tipo}")
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=f"Erro ao corrigir na Amazon: {res.get('error')}")
    item["status"] = "corrigido"
    item["corrigido_em"] = datetime.now(timezone.utc).isoformat()
    salvar_fila(fila)
    return {"ok": True, "item_id": item_id, "tipo": tipo, "sku": sku}

@app.post("/auditoria/amazon/ignorar/{item_id}")
def auditoria_amazon_ignorar(item_id: str):
    from amazon_conferencia import carregar_fila, salvar_fila
    itens = [i for i in carregar_fila() if i.get("status") in ("pendente","incompleto")]
    for i in itens:
        if i.get("id") == item_id:
            i["status"] = "ignorado"
    salvar_fila(itens)
    return {"ok": True}

@app.post("/auditoria/amazon/limpar-resolvidos")
def auditoria_amazon_limpar_resolvidos():
    from amazon_conferencia import carregar_fila, salvar_fila, stats_fila
    itens = [i for i in carregar_fila() if i.get("status") == "pendente"]
    salvar_fila(itens)
    return {"ok": True, "stats": stats_fila()}

@app.post("/auditoria/amazon/limpar-tudo")
def auditoria_amazon_limpar_tudo():
    from pathlib import Path as _P
    _P("data/fila_amazon.json").write_text("[]", encoding="utf-8")
    return {"ok": True}

@app.get("/amazon/status")
def amazon_status():
    try:
        from amazon_client import AmazonClient
        c = AmazonClient()
        token = c._get_access_token()
        return {"ok": True, "configurado": True, "conectado": bool(token)}
    except Exception as e:
        return {"ok": False, "configurado": False, "conectado": False, "erro": str(e)}


# ── Amazon SP-API OAuth (self-authorization) ──────────────────────────────────

@app.get("/amazon/auth")
def amazon_auth(request: Request):
    """
    Gera URL de autorização para o SP-API (self-authorization Draft).
    Defina AMAZON_APP_ID com o Application ID do SP-API Developer Portal.
    """
    import json as _json, secrets as _sec
    app_id = os.getenv("AMAZON_APP_ID", "")
    if not app_id:
        return {"ok": False, "erro": "Defina AMAZON_APP_ID no Railway com o ID do app SP-API"}
    state = _sec.token_hex(16)
    (DATA_DIR / "amazon_oauth_state.json").write_text(
        _json.dumps({"state": state}), encoding="utf-8"
    )
    redirect_uri = os.getenv("AMAZON_CALLBACK_URL",
                             f"{request.headers.get('x-forwarded-proto', request.url.scheme)}"
                             f"://{request.headers.get('x-forwarded-host', request.url.netloc)}/amazon/callback")
    url = (
        f"https://sellercentral.amazon.com.br/apps/authorize/consent"
        f"?application_id={app_id}"
        f"&state={state}"
        f"&version=beta"
        f"&redirect_uri={redirect_uri}"
    )
    return {"ok": True, "url": url, "instrucao": "Abra esta URL no navegador logado no Seller Central"}


@app.get("/amazon/callback")
def amazon_callback(
    spapi_oauth_code: str = "",
    selling_partner_id: str = "",
    state: str = "",
    mws_auth_token: str = "",
):
    """
    Callback do SP-API OAuth. Troca o código por refresh_token e salva em
    data/amazon_tokens.json. Copie o refresh_token para AMAZON_REFRESH_TOKEN no Railway.
    """
    import json as _json, requests as _req
    from datetime import datetime, timezone
    try:
        saved_state_path = DATA_DIR / "amazon_oauth_state.json"
        if saved_state_path.exists():
            saved = _json.loads(saved_state_path.read_text(encoding="utf-8"))
            if saved.get("state") and saved.get("state") != state:
                return {"ok": False, "erro": "State inválido"}

        client_id     = os.getenv("AMAZON_CLIENT_ID", "")
        client_secret = os.getenv("AMAZON_CLIENT_SECRET", "")
        redirect_uri  = os.getenv("AMAZON_CALLBACK_URL",
                                   "https://elegant-encouragement-production-6829.up.railway.app/amazon/callback")

        resp = _req.post(
            "https://api.amazon.com/auth/o2/token",
            data={
                "grant_type":    "authorization_code",
                "code":          spapi_oauth_code,
                "redirect_uri":  redirect_uri,
                "client_id":     client_id,
                "client_secret": client_secret,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return {"ok": False, "status": resp.status_code, "erro": resp.text[:300]}

        data = resp.json()
        tokens = {
            "access_token":       data.get("access_token"),
            "refresh_token":      data.get("refresh_token"),
            "selling_partner_id": selling_partner_id,
            "salvo_em":           datetime.now(timezone.utc).isoformat(),
        }
        (DATA_DIR / "amazon_tokens.json").write_text(
            _json.dumps(tokens, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        refresh_token = data.get("refresh_token", "")
        return {
            "ok": True,
            "refresh_token": refresh_token,
            "instrucao": f"Copie este refresh_token e defina AMAZON_REFRESH_TOKEN={refresh_token} no Railway",
        }
    except Exception as e:
        return {"ok": False, "erro": str(e)}


# ── Fila unificada de preços (Amazon + Shopify) ───────────────────────────────

def _normalizar_item_preco(item: dict, canal: str) -> dict:
    """Normaliza item de fila de preço para o formato esperado pelo frontend."""
    preco_mkt = item.get("preco_amazon") or item.get("preco_shopify") or 0
    return {
        "id": item.get("id"),
        "sku": item.get("sku"),
        "nome": item.get("nome") or item.get("titulo") or "",
        "canal": canal,
        "preco_shinsei": float(item.get("preco_bling") or 0),
        "preco_marketplace_promocional": float(preco_mkt),
        "preco_virtual_shinsei": float(item.get("preco_bling") or 0),
        "preco_marketplace": float(preco_mkt),
        "diferenca": float(item.get("diferenca") or 0),
        "status": item.get("status", "pendente"),
        "detectado_em": item.get("detectado_em") or item.get("criado_em") or "",
    }


@app.get("/auditoria/precos")
def auditoria_precos_lista(status: str = ""):
    import json as _json
    from pathlib import Path as _P
    itens = []
    for path, canal in [("data/fila_amazon.json", "Amazon"), ("data/fila_shopify.json", "Shopify")]:
        try:
            raw = _json.loads(_P(path).read_text(encoding="utf-8")) if _P(path).exists() else []
            for i in (raw if isinstance(raw, list) else []):
                if i.get("tipo") == "preco":
                    if not status or i.get("status") == status:
                        itens.append(_normalizar_item_preco(i, canal))
        except Exception:
            pass
    return {"itens": itens, "total": len(itens), "pendentes": sum(1 for i in itens if i["status"] == "pendente")}


@app.post("/auditoria/conferir-precos")
def auditoria_conferir_precos():
    if not BlingClient:
        raise HTTPException(status_code=500, detail="bling_client.py nao encontrado.")
    bling = BlingClient()
    total_verificados = 0
    total_divergencias = 0
    erros = []
    try:
        from amazon_conferencia import conferir_amazon
        res = conferir_amazon(bling_client=bling, tipo="preco")
        if res.get("ok"):
            total_verificados += res.get("verificados", 0)
            total_divergencias += res.get("divergencias_preco", 0)
        else:
            erros.append("Amazon: " + str(res.get("erro", "falha")))
    except Exception as e:
        erros.append(f"Amazon: {e}")
    try:
        from shopify_conferencia import conferir_shopify
        res = conferir_shopify(bling_client=bling, tipo="preco")
        if res.get("ok"):
            total_verificados += res.get("verificados", 0)
            total_divergencias += res.get("divergencias_preco", 0)
        else:
            erros.append("Shopify: " + str(res.get("erro", "falha")))
    except Exception as e:
        erros.append(f"Shopify: {e}")
    return {
        "ok": True,
        "verificados": total_verificados,
        "novas_divergencias": total_divergencias,
        "erros": erros,
    }


@app.post("/auditoria/corrigir-preco/{item_id}")
def auditoria_corrigir_preco(item_id: str):
    import json as _json
    from pathlib import Path as _P
    from datetime import datetime, timezone

    # Tenta Amazon
    if item_id.startswith("amz_"):
        from amazon_conferencia import carregar_fila, salvar_fila
        fila = carregar_fila()
        item = next((i for i in fila if i.get("id") == item_id), None)
        if not item:
            raise HTTPException(status_code=404, detail="Item nao encontrado.")
        if item.get("status") != "pendente":
            raise HTTPException(status_code=400, detail=f"Item ja esta '{item['status']}'.")
        try:
            from services.amazon import AmazonService
            res = AmazonService().atualizar_com_retry(item["sku"], item["preco_bling"])
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        if not res.get("success"):
            raise HTTPException(status_code=400, detail=res.get("error", "Falha ao corrigir preco na Amazon."))
        item["status"] = "corrigido"
        item["corrigido_em"] = datetime.now(timezone.utc).isoformat()
        salvar_fila(fila)
        return {"ok": True}

    # Tenta Shopify
    if item_id.startswith("shp_"):
        from shopify_conferencia import corrigir_shopify
        res = corrigir_shopify(item_id)
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("erro", "Falha ao corrigir preco na Shopify."))
        return {"ok": True}

    raise HTTPException(status_code=404, detail="Item nao encontrado em nenhuma fila.")


@app.post("/auditoria/ignorar-preco/{item_id}")
def auditoria_ignorar_preco(item_id: str):
    from datetime import datetime, timezone

    if item_id.startswith("amz_"):
        from amazon_conferencia import carregar_fila, salvar_fila
        fila = carregar_fila()
        item = next((i for i in fila if i.get("id") == item_id), None)
        if not item:
            raise HTTPException(status_code=404, detail="Item nao encontrado.")
        item["status"] = "ignorado"
        item["ignorado_em"] = datetime.now(timezone.utc).isoformat()
        salvar_fila(fila)
        return {"ok": True}

    if item_id.startswith("shp_"):
        from shopify_conferencia import ignorar_shopify
        res = ignorar_shopify(item_id)
        return {"ok": res.get("ok", False)}

    raise HTTPException(status_code=404, detail="Item nao encontrado em nenhuma fila.")


@app.post("/auditoria/mp-token")
async def auditoria_salvar_mp_token(request: Request):
    import json as _json
    from pathlib import Path as _P
    from datetime import datetime
    body = await request.json()
    token = (body.get("access_token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Token vazio.")
    path = _P("data/mp_token.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(_json.dumps({"access_token": token, "salvo_em": datetime.utcnow().isoformat()}, indent=2), encoding="utf-8")
    return {"ok": True}





# ─── MARKETING ML ──────────────────────────────────────────────────────────────

@app.get("/marketing", response_class=HTMLResponse)
def marketing_page():
    html_file = PAGES_DIR / "marketing.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="pages/marketing.html nao encontrado.")


@app.post("/marketing/ml/analisar")
async def marketing_ml_analisar(request: Request):
    body = await request.json()
    desconto_pct = float(body.get("desconto_pct", 10))
    margem_alvo = float(body.get("margem_alvo", 20))
    max_itens = int(body.get("max_itens", 500))
    imposto_padrao = float(body.get("imposto_padrao", 12.0))

    import json as _json
    from pathlib import Path as _P
    tokens_path = _P("data/ml_tokens.json")
    if not tokens_path.exists():
        raise HTTPException(status_code=400, detail="Token ML nao configurado. Faca login em /ml/login.")
    tokens = _json.loads(tokens_path.read_text(encoding="utf-8"))
    seller_id = str(tokens.get("user_id", ""))
    if not seller_id:
        raise HTTPException(status_code=400, detail="seller_id nao encontrado no token ML.")

    if not BlingClient:
        raise HTTPException(status_code=500, detail="Bling nao disponivel.")

    from services.marketing_ml import analisar_campanhas_ml
    try:
        client = BlingClient()
        resultado = analisar_campanhas_ml(
            seller_id=seller_id,
            bling_client=client,
            desconto_pct=desconto_pct,
            margem_alvo=margem_alvo,
            imposto_padrao=imposto_padrao,
            max_itens=max_itens,
        )
        return resultado
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/marketing/ml/participar")
async def marketing_ml_participar(request: Request):
    """Atualiza o preco dos itens selecionados no ML para o preco da campanha."""
    body = await request.json()
    itens = body.get("itens", [])  # [{item_id, preco_campanha}, ...]
    if not itens:
        raise HTTPException(status_code=400, detail="Nenhum item selecionado.")

    from services.marketing_ml import atualizar_preco_ml
    resultados = []
    for item in itens:
        res = atualizar_preco_ml(item["item_id"], float(item["preco_campanha"]))
        resultados.append(res)
        import time as _time
        _time.sleep(0.3)

    ok = sum(1 for r in resultados if r.get("ok"))
    erros = len(resultados) - ok
    return {"ok": True, "atualizados": ok, "erros": erros, "detalhes": resultados}


# ─── MARKETING AMAZON ──────────────────────────────────────────────────────────

@app.get("/marketing/amazon/cache")
def marketing_amazon_cache():
    """Retorna resultado cacheado da última análise Amazon (sem refazer a análise)."""
    from services.amazon_marketing import carregar_cache_amazon
    cache = carregar_cache_amazon()
    if not cache:
        return {"ok": False, "erro": "Sem análise em cache"}
    return cache


@app.post("/marketing/amazon/analisar")
async def marketing_amazon_analisar(request: Request):
    body = await request.json()
    margem_alvo = float(body.get("margem_alvo", 20))
    imposto_padrao = float(body.get("imposto_padrao", 12.0))
    max_itens = int(body.get("max_itens", 500))

    if not BlingClient:
        raise HTTPException(status_code=500, detail="Bling nao disponivel.")

    from services.amazon_marketing import analisar_buy_box_amazon
    try:
        client = BlingClient()
        regras = carregar_regras(apenas_ativas=True)
        resultado = analisar_buy_box_amazon(
            bling_client=client,
            regras=regras,
            margem_alvo=margem_alvo,
            imposto_padrao=imposto_padrao,
            max_itens=max_itens,
        )
        return resultado
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/marketing/amazon/participar")
async def marketing_amazon_participar(request: Request):
    """Atualiza preço dos itens selecionados na Amazon para cobrir a Buy Box."""
    body = await request.json()
    itens = body.get("itens", [])
    if not itens:
        raise HTTPException(status_code=400, detail="Nenhum item selecionado.")

    from services.amazon_marketing import atualizar_preco_amazon
    import time as _time
    resultados = []
    for item in itens:
        res = atualizar_preco_amazon(item["sku"], float(item["preco_buy_box"]))
        resultados.append(res)
        _time.sleep(0.3)

    ok = sum(1 for r in resultados if r.get("ok"))
    erros = len(resultados) - ok
    return {"ok": True, "atualizados": ok, "erros": erros, "detalhes": resultados}


# ── SEO Health ────────────────────────────────────────────────────────────────

SEO_CACHE_PATH = DATA_DIR / "seo_health_cache.json"


@app.get("/seo-health", response_class=HTMLResponse)
def seo_health_page():
    path = PAGES_DIR / "seo_health.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="seo_health.html não encontrado.")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/seo-health/dados")
def seo_health_dados():
    cache = _load_json(SEO_CACHE_PATH, None)
    if not cache:
        return {"ok": False, "erro": "Nenhuma análise em cache. Clique em Analisar agora."}
    return {"ok": True, **cache}


@app.post("/seo-health/analisar")
async def seo_health_analisar():
    """Audita coleções, produtos e blog do Shopify e salva cache."""
    import asyncio, importlib.util
    seo_path = BASE_DIR / "shinsei_seo.py"
    if not seo_path.exists():
        raise HTTPException(status_code=500, detail="shinsei_seo.py não encontrado.")

    def _run():
        spec = importlib.util.spec_from_file_location("shinsei_seo", seo_path)
        seo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(seo)
        return seo.health_score()

    try:
        loop = asyncio.get_event_loop()
        resultado = await loop.run_in_executor(None, _run)
        _save_json(SEO_CACHE_PATH, resultado)
        return {"ok": True, **resultado}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/seo-health/pagespeed")
async def seo_health_pagespeed():
    """Busca scores do PageSpeed Insights (mobile + desktop) e atualiza cache."""
    try:
        import importlib.util, os as _os
        seo_path = BASE_DIR / "shinsei_seo.py"
        if not seo_path.exists():
            raise HTTPException(status_code=500, detail="shinsei_seo.py não encontrado.")
        spec = importlib.util.spec_from_file_location("shinsei_seo", seo_path)
        seo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(seo)

        import requests as _req
        store = getattr(seo, "STORE", "pknw4n-eg")
        api_key = _os.getenv("PAGESPEED_API_KEY", "")
        url_loja = f"https://www.shinseimarket.com.br"
        ps_url = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
        scores = {}
        for strategy in ("mobile", "desktop"):
            params = [
                ("url", url_loja),
                ("strategy", strategy),
                ("category", "performance"),
                ("category", "seo"),
                ("category", "accessibility"),
                ("category", "best-practices"),
            ]
            if api_key:
                params.append(("key", api_key))
            r = _req.get(ps_url, params=params, timeout=90)
            if r.status_code == 200:
                d = r.json()
                cats = d.get("lighthouseResult", {}).get("categories", {})
                audits = d.get("lighthouseResult", {}).get("audits", {})
                oportunidades = [
                    v.get("title", "") for v in audits.values()
                    if isinstance(v, dict) and v.get("score") is not None
                    and float(v.get("score", 1)) < 0.9 and v.get("title")
                    and v.get("details", {}).get("type") not in ("table", "list", "criticalrequestchain")
                ]
                scores[strategy] = {
                    "performance": round((cats.get("performance", {}).get("score") or 0) * 100),
                    "seo": round((cats.get("seo", {}).get("score") or 0) * 100),
                    "accessibility": round((cats.get("accessibility", {}).get("score") or 0) * 100),
                    "best_practices": round((cats.get("best-practices", {}).get("score") or 0) * 100),
                    "lcp": audits.get("largest-contentful-paint", {}).get("displayValue", "—"),
                    "cls": audits.get("cumulative-layout-shift", {}).get("displayValue", "—"),
                    "tbt": audits.get("total-blocking-time", {}).get("displayValue", "—"),
                    "fcp": audits.get("first-contentful-paint", {}).get("displayValue", "—"),
                    "ttfb": audits.get("server-response-time", {}).get("displayValue", "—"),
                    "oportunidades": oportunidades[:10],
                }
            else:
                scores[strategy] = None

        cache = _load_json(SEO_CACHE_PATH, {})
        cache["pagespeed"] = scores
        _save_json(SEO_CACHE_PATH, cache)
        return {"ok": True, "pagespeed": scores}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/seo-health/merchant")
async def seo_health_merchant():
    """Audita cobertura do feed Google Merchant Center via Shopify API."""
    import requests as _req, asyncio, random as _rand

    cfg_path = DATA_DIR / "shopify_config.json"
    if not cfg_path.exists():
        raise HTTPException(status_code=500, detail="shopify_config.json não encontrado.")

    token = _load_json(cfg_path, {}).get("access_token", "")
    if not token:
        raise HTTPException(status_code=500, detail="Token Shopify não configurado.")

    hdrs = {"X-Shopify-Access-Token": token}
    store = "pknw4n-eg"
    base = f"https://{store}.myshopify.com/admin/api/2024-01"

    def _fetch_shopify():
        # Buscar 1ª página (250 produtos) para estimativa rápida de cobertura
        all_prods = []
        url = f"{base}/products.json?limit=250&fields=id,vendor,product_type"
        for _ in range(6):  # até 6 páginas = 1500 produtos
            try:
                r = _req.get(url, headers=hdrs, timeout=20)
                prods = r.json().get("products", [])
                if not prods:
                    break
                all_prods.extend(prods)
                links = r.headers.get("Link", "")
                url = None
                for part in links.split(","):
                    if 'rel="next"' in part:
                        url = part.strip().split(";")[0].strip("<> ")
                        break
                if not url:
                    break
            except Exception:
                break

        total = len(all_prods)
        if total == 0:
            return None

        sem_type = sum(1 for p in all_prods if not (p.get("product_type") or "").strip())
        vendor_errado = sum(1 for p in all_prods if p.get("vendor", "") in ("Shinsei Market", ""))

        # Checar google_product_category numa amostra de 40 produtos
        sample = _rand.sample(all_prods, min(40, total))
        com_gcat = 0
        for p in sample:
            try:
                rm = _req.get(f"{base}/products/{p['id']}/metafields.json?namespace=mm-google-shopping&limit=1",
                              headers=hdrs, timeout=10)
                if rm.json().get("metafields"):
                    com_gcat += 1
            except Exception:
                pass
        pct_gcat = round(com_gcat / len(sample) * 100) if sample else 0
        estimado_gcat = round(total * pct_gcat / 100)

        return {
            "analisado_em": datetime.utcnow().isoformat(),
            "total_produtos": total,
            "com_product_type": total - sem_type,
            "sem_product_type": sem_type,
            "pct_product_type": round((total - sem_type) / total * 100),
            "vendor_incorreto": vendor_errado,
            "pct_vendor_ok": round((total - vendor_errado) / total * 100),
            "estimado_google_cat": estimado_gcat,
            "pct_google_cat": pct_gcat,
            "pendencias_merchant": [
                {
                    "prioridade": "alto",
                    "titulo": "Forçar re-sincronização no Merchant Center",
                    "descricao": "product_type, vendor e google_product_category foram atualizados. O Merchant Center precisa re-processar o feed para refletir as mudanças.",
                    "impacto": "Produtos aprovados começam a aparecer no Google Shopping em até 24h",
                    "acao_url": "https://merchants.google.com/",
                    "acao_label": "Abrir Merchant Center",
                },
                {
                    "prioridade": "alto",
                    "titulo": "Verificar produtos reprovados no Merchant Center",
                    "descricao": "Após o re-sync, verifique em Produtos → Problemas se há itens suspensos por dado ausente, preço divergente ou imagem inválida.",
                    "impacto": "Cada produto reprovado é tráfego perdido de Shopping",
                    "acao_url": "https://merchants.google.com/",
                    "acao_label": "Ver Problemas",
                },
                {
                    "prioridade": "medio",
                    "titulo": "Ativar campanhas Performance Max",
                    "descricao": "Com product_type e google_product_category definidos, o Google Ads consegue segmentar campanhas PMax por categoria. Criar grupos de ativos por linha (Coloração, Tratamento, Maquiagem).",
                    "impacto": "Performance Max com dados estruturados pode aumentar impressões em 30-50%",
                    "acao_url": "https://ads.google.com/",
                    "acao_label": "Abrir Google Ads",
                },
                {
                    "prioridade": "medio",
                    "titulo": "Configurar e-mail de review pós-compra (Judge.me)",
                    "descricao": "Reviews com estrelas aparecem nos anúncios Shopping como Seller Ratings. Configurar envio automático 7 dias após entrega no painel Judge.me.",
                    "impacto": "Seller Ratings aumentam CTR do Shopping em média 17%",
                    "acao_url": "https://judge.me/",
                    "acao_label": "Abrir Judge.me",
                },
                {
                    "prioridade": "baixo",
                    "titulo": "Expandir para Google Merchant Center — Promoções",
                    "descricao": "Cadastrar promoções no Merchant Center (ex: frete grátis acima de R$99) para exibir badge 'Promoção' nos anúncios Shopping.",
                    "impacto": "Badge de promoção aumenta CTR em média 12%",
                    "acao_url": "https://merchants.google.com/",
                    "acao_label": "Ver Promoções",
                },
            ],
        }

    try:
        loop = asyncio.get_event_loop()
        resultado = await loop.run_in_executor(None, _fetch_shopify)
        if not resultado:
            raise HTTPException(status_code=500, detail="Não foi possível buscar dados do Shopify.")
        cache = _load_json(SEO_CACHE_PATH, {})
        cache["merchant"] = resultado
        _save_json(SEO_CACHE_PATH, cache)
        return {"ok": True, "merchant": resultado}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── SCBOT — Robô de Indexação Google ──────────────────────────────────────

@app.get("/scbot/status")
def scbot_status_endpoint():
    """Retorna status e histórico do SCBOT."""
    return {"ok": True, "scbot": scbot_status()}


@app.post("/scbot/executar")
async def scbot_executar_endpoint(urls_extras: list[str] = None):
    """Dispara um ciclo manual do SCBOT (ignora agendamento diário)."""
    import asyncio
    loop = asyncio.get_event_loop()
    resultado = await loop.run_in_executor(None, lambda: scbot_executar(urls_extras or []))
    # Salva no cache de SEO Health também
    cache = _load_json(SEO_CACHE_PATH, {})
    cache["scbot"] = scbot_status()
    _save_json(SEO_CACHE_PATH, cache)
    return {"ok": True, "resultado": resultado}
