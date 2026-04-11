from __future__ import annotations
from dotenv import load_dotenv
load_dotenv()
import importlib, json, uuid
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from fastapi import Body, FastAPI, File, HTTPException, Query, Request, UploadFile
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
from logging_config import configurar_logging
from auth import verificar_api_key

configurar_logging()
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
from routes.batch import router as batch_router
from routes.ml_unificado import router as ml_router
from monitoring import router as monitoring_router
app.include_router(batch_router)
from routes.mercado_livre import router as ml_page_router
app.include_router(ml_page_router)
app.include_router(ml_router)
app.include_router(monitoring_router)

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    return await verificar_api_key(request, call_next)

@app.on_event("startup")
def startup():
    init_db()
    migrar_json_legado()
    iniciar_scheduler_background()
    logger.info("Shinsei Pricing iniciado")

@app.on_event("shutdown")
def shutdown():
    parar_scheduler()

def _optional_import(module_name: str):
    try: return importlib.import_module(module_name)
    except Exception: return None

pricing_module = _optional_import("pricing_engine_real") or _optional_import("pricing_engine")
if pricing_module is None:
    raise RuntimeError("pricing_engine_real.py ou pricing_engine.py nÃ£o encontrado.")
montar_precificacao_bling: Optional[Callable[..., dict]] = getattr(pricing_module, "montar_precificacao_bling", None)
if montar_precificacao_bling is None:
    raise RuntimeError("Seu motor atual nÃ£o expÃµe montar_precificacao_bling().")

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
            elif "composiÃ§Ã£o" in erro_txt or "composicao" in erro_txt:
                codigo = "composicao_sem_custo"
            elif "sem custo" in erro_txt:
                codigo = "custo_ausente"
            else:
                codigo = "erro_motor"
        return {"ok":False,"codigo":codigo,"mensagem":str(aud.get("erro")),"detalhe":aud.get("acao") or ""}
    if not (produto.get("codigo") or aud.get("sku")):
        return {"ok":False,"codigo":"sku_ausente","mensagem":"SKU ausente no retorno do Bling.","detalhe":"Verifique se o produto encontrado possui CÃ³digo (SKU) cadastrado."}
    custo = float(aud.get("custo_usado") or 0)
    peso = float(aud.get("peso_usado") or 0)
    tipo_custo = str(aud.get("tipo_custo") or "").lower()
    componentes = aud.get("componentes_custo") or []
    if peso <= 0:
        return {"ok":False,"codigo":"peso_ausente","mensagem":"Produto sem peso no Bling.","detalhe":"Preencha o peso no produto ou use peso override no simulador."}
    if custo <= 0 and tipo_custo == "composicao":
        faltando = [c.get("sku") or str(c.get("id") or "-") for c in componentes if float(c.get("custo_unitario") or 0) <= 0]
        return {"ok":False,"codigo":"composicao_sem_custo","mensagem":"ComposiÃ§Ã£o sem custo vÃ¡lido nos componentes.","detalhe":("Componentes sem custo: " + ", ".join(faltando)) if faltando else "Nenhum componente retornou custo vÃ¡lido."}
    if custo <= 0:
        return {"ok":False,"codigo":"custo_ausente","mensagem":"Produto sem custo no estoque do Bling.","detalhe":"Preencha o preÃ§o de compra/custo do produto no estoque."}
    if not _marketplaces_validos(marketplaces):
        return {"ok":False,"codigo":"sem_canais","mensagem":"Nenhum canal vÃ¡lido foi calculado.","detalhe":"Verifique peso, custo e faixas da Aba2 para este produto."}
    return {"ok":True,"codigo":"preview_valido","mensagem":"Preview vÃ¡lido.","detalhe":""}

def _preview_valido(preview: dict):
    diag = _diagnostico_preview(preview)
    return bool(diag.get("ok")), str(diag.get("mensagem") or "Preview invÃ¡lido.")

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
    raise HTTPException(status_code=404, detail="pages/fila.html nÃ£o encontrado.")

@app.get("/health")
def health():
    itens = carregar_fila()
    return {"status":"Shinsei Pricing rodando","engine":pricing_module.__name__,"bling_client":bool(BlingClient),"bling_update_engine":bool(aplicar_precos_multicanal),"modo_busca":"sku_only","fila":_fila_stats(itens)}

@app.get("/bling/status")
def bling_status():
    if not BlingClient: return {"ok":False,"erro":"bling_client.py nÃ£o encontrado."}
    try:
        client = BlingClient()
        return {"ok":True,"configurado":bool(getattr(client,"client_id","") and getattr(client,"client_secret","") and getattr(client,"redirect_uri","")),"token_local":bool(client.has_local_tokens())}
    except Exception as exc:
        return {"ok":False,"erro":str(exc)}

@app.get("/bling/auth")
def bling_auth():
    if not BlingClient: raise HTTPException(status_code=500, detail="bling_client.py nÃ£o encontrado.")
    try:
        client = BlingClient()
        return RedirectResponse(client.build_authorize_url())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.get("/bling/callback")
def bling_callback(code: str | None = Query(None), state: str | None = Query(None), error: str | None = Query(None), error_description: str | None = Query(None)):
    if not BlingClient: raise HTTPException(status_code=500, detail="bling_client.py nÃ£o encontrado.")
    if error: raise HTTPException(status_code=400, detail=f"Bling OAuth retornou erro: {error}. {error_description or ''}".strip())
    if not code: raise HTTPException(status_code=400, detail="Callback do Bling sem code de autorizaÃ§Ã£o.")
    try:
        client = BlingClient()
        token = client.exchange_code_for_token(code, state=state)
        return {"ok":True,"message":"ConexÃ£o com Bling realizada.","expires_in":token.get("expires_in")}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/bling/debug/sku")
def bling_debug_sku(payload: DebugSkuPayload):
    if not BlingClient: raise HTTPException(status_code=500, detail="bling_client.py nÃ£o encontrado.")
    try:
        client = BlingClient()
        return client.debug_product_by_sku(payload.sku)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/bling/produto/buscar")
def bling_produto_buscar(payload: DebugSkuPayload):
    if not BlingClient: raise HTTPException(status_code=500, detail="bling_client.py nÃ£o encontrado.")
    try:
        client = BlingClient()
        return client.get_product_by_sku(payload.sku)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/bling/produto/atualizar-peso")
def bling_produto_atualizar_peso(payload: AtualizacaoCampoBlingPayload):
    if not BlingClient:
        raise HTTPException(status_code=500, detail="bling_client.py nÃ£o encontrado.")
    if float(payload.valor or 0) <= 0:
        raise HTTPException(status_code=400, detail="Informe um peso vÃ¡lido.")
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
        raise HTTPException(status_code=400, detail="A precificaÃ§Ã£o integrada aceita apenas busca por SKU. Use criterio='sku'.")
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
                fila_auto = {"adicionado":False,"motivo":"JÃ¡ existe item pendente equivalente na fila."}
            else:
                item = _montar_item_fila(preview, payload.dict())
                inserir_item_fila(item)
                _append_jsonl(LOG_PATH, {"evento":"fila_auto_preview","item_id":item["id"],"sku":item["sku"],"quando":item["criado_em"]})
                fila_auto = {"adicionado":True,"item_id":item["id"]}
        else:
            fila_auto = {"adicionado":False,"motivo":motivo or diagnostico.get("mensagem") or "Fila automÃ¡tica desativada."}
        preview["fila_auto"] = fila_auto
        logger.info("PrecificaÃ§Ã£o: SKU=%s melhor_canal=%s fila=%s", payload.valor_busca, preview.get("melhor_canal"), fila_auto.get("adicionado"))
        return preview
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Falha no preview: {exc}")

@app.get("/fila/lista")
def fila_lista():
    itens = carregar_fila()
    return {"itens":itens,"stats":stats_fila()}

@app.post("/fila/adicionar")
def fila_adicionar(payload: dict = Body(...)):
    preview = {"ok":payload.get("ok", True),"produto":payload.get("produto_bling") or payload.get("produto") or {},"marketplaces":payload.get("marketplaces") or {},"auditoria":payload.get("auditoria") or {},"raw":payload.get("raw") or {}}
    diag = _diagnostico_preview(preview)
    if not diag.get("ok"): raise HTTPException(status_code=400, detail=f"Preview invÃ¡lido para fila: {diag.get('mensagem')}")
    sku = (preview.get("auditoria") or {}).get("sku") or (preview.get("produto") or {}).get("codigo") or ""
    if ja_existe_pendente(sku):
        return {"ok":True,"duplicado":True,"message":"JÃ¡ existe item pendente equivalente na fila.","stats":stats_fila()}
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

@app.post("/fila/aprovar/{item_id}")
def fila_aprovar(item_id: str):
    item = buscar_item_fila(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item nÃ£o encontrado na fila.")
    if item.get("status") != "pendente":
        raise HTTPException(status_code=400, detail=f"Item jÃ¡ com status '{item.get("status")}'.")
    if not BlingClient or not aplicar_precos_multicanal:
        raise HTTPException(status_code=500, detail="IntegraÃ§Ã£o de aplicaÃ§Ã£o no Bling indisponÃ­vel.")
    item_com_gordura = _aplicar_gordura_no_item(item)
    try:
        client = BlingClient()
        resultado = aplicar_precos_multicanal(client, item_com_gordura)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Falha ao aplicar preÃ§os no Bling: {exc}")
    agora = datetime.utcnow().isoformat()
    atualizar_status_fila(item_id, "aprovado", resultado=resultado)
    _append_jsonl(LOG_PATH, {"evento": "fila_aprovado", "item_id": item_id, "quando": agora})
    logger.info("Item aprovado: id=%s sku=%s estrategia=%s", item_id, item.get("sku"), resultado.get("estrategia"))
    return {"ok": True, "message": "PreÃ§os aplicados no Bling.", "resultado": resultado, "stats": stats_fila()}


@app.post("/fila/rejeitar/{item_id}")
def fila_rejeitar(item_id: str, payload: dict = Body(default={})):
    item = buscar_item_fila(item_id)
    if not item: raise HTTPException(status_code=404, detail="Item nÃ£o encontrado na fila.")
    agora = datetime.utcnow().isoformat()
    motivo = payload.get("motivo") or "Rejeitado manualmente."
    atualizar_status_fila(item_id, "rejeitado", resultado={"motivo": motivo})
    _append_jsonl(LOG_PATH, {"evento":"fila_rejeitado","item_id":item_id,"quando":agora,"motivo":motivo})
    logger.info("Item rejeitado: id=%s sku=%s motivo=%s", item_id, item.get("sku"), motivo)
    return {"ok":True,"message":"Item marcado como rejeitado.","stats":stats_fila()}

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# FASE 5 â€” IntegraÃ§Ã£o Comercial
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


@app.post("/webhooks/bling")
async def webhook_bling(request: Request):
    try: body = await request.json()
    except Exception: body = {}
    evento = body.get("evento") or body.get("event") or "desconhecido"
    logger.info("Webhook Bling recebido: evento=%s", evento)
    _append_jsonl(LOG_PATH, {"evento":"webhook_bling","tipo":evento,"quando":datetime.utcnow().isoformat(),"payload":body})
    return {"ok": True, "recebido": True}


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
        client = BlingClient()
        regras = carregar_regras(apenas_ativas=True)

        resultado = montar_precificacao_bling(
            client=client,
            criterio="sku",
            valor_busca=sku,
            regras=regras,
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
    from ml_estoque_conferencia import conferir_estoques_ml
    if not BlingClient:
        raise HTTPException(status_code=500, detail="Bling não disponível.")
    try:
        # Pausa o scheduler durante a conferência para evitar rate limit
        global _scheduler_pausado
        _scheduler_pausado = True
        try:
            client = BlingClient()
            resultado = conferir_estoques_ml(client, max_paginas=10)
        finally:
            _scheduler_pausado = False
        return resultado
    except Exception as e:
        _scheduler_pausado = False
        raise HTTPException(status_code=400, detail=str(e))

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


@app.get("/shopify/auth")
def shopify_auth(request: Request):
    from shopify_oauth import gerar_url_auth
    from fastapi.responses import RedirectResponse
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = base_url + "/shopify/callback"
    url = gerar_url_auth(redirect_uri)
    return RedirectResponse(url)

@app.get("/shopify/callback")
def shopify_callback(code: str = "", state: str = "", request: Request = None):
    from shopify_oauth import processar_callback
    from fastapi.responses import HTMLResponse
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = base_url + "/shopify/callback"
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
if not FILA_PATH.exists(): ...
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
    """Aplica a gordura sobre o preÃ§o calculado e arredonda."""
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


# â”€â”€â”€ ROTA: pÃ¡gina de integraÃ§Ã£o comercial â”€â”€â”€
@app.get("/integracao-comercial", response_class=HTMLResponse)
def integracao_comercial_page():
    html_file = PAGES_DIR / "integracao_comercial.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="integracao_comercial.html nÃ£o encontrado.")


# â”€â”€â”€ ROTA: GET config â”€â”€â”€
@app.get("/config/integracao-comercial")
def get_integracao_config():
    return carregar_integracao_cfg()


# â”€â”€â”€ ROTA: POST config â”€â”€â”€
@app.post("/config/integracao-comercial")
async def set_integracao_config(request: Request):
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Payload invÃ¡lido.")

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
    logger.info("ConfiguraÃ§Ã£o de integraÃ§Ã£o comercial atualizada: objetivo=%s", cfg_atual.get("objetivo"))

    return {"ok": True, "config": cfg_atual}


# â”€â”€â”€ ROTA: calcular preÃ§o virtual para um canal â”€â”€â”€
@app.post("/config/calcular-preco-virtual")
async def calcular_preco_virtual_endpoint(request: Request):
    """Dado um preÃ§o calculado, retorna o preÃ§o virtual por canal com a gordura configurada."""
    data = await request.json()
    preco = float(data.get("preco", 0))
    if preco <= 0:
        raise HTTPException(status_code=400, detail="PreÃ§o invÃ¡lido.")

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


@app.post("/webhooks/bling")
async def webhook_bling(request: Request):
    try: body = await request.json()
    except Exception: body = {}
    evento = body.get("evento") or body.get("event") or "desconhecido"
    logger.info("Webhook Bling recebido: evento=%s", evento)
    _append_jsonl(LOG_PATH, {"evento":"webhook_bling","tipo":evento,"quando":datetime.utcnow().isoformat(),"payload":body})
    return {"ok": True, "recebido": True}


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
        client = BlingClient()
        regras = carregar_regras(apenas_ativas=True)

        resultado = montar_precificacao_bling(
            client=client,
            criterio="sku",
            valor_busca=sku,
            regras=regras,
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
    from ml_estoque_conferencia import conferir_estoques_ml
    if not BlingClient:
        raise HTTPException(status_code=500, detail="Bling não disponível.")
    try:
        # Pausa o scheduler durante a conferência para evitar rate limit
        global _scheduler_pausado
        _scheduler_pausado = True
        try:
            client = BlingClient()
            resultado = conferir_estoques_ml(client, max_paginas=10)
        finally:
            _scheduler_pausado = False
        return resultado
    except Exception as e:
        _scheduler_pausado = False
        raise HTTPException(status_code=400, detail=str(e))

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




@app.get("/integracoes", response_class=HTMLResponse)
def integracoes_page():
    html_file = PAGES_DIR / "integracoes.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404)

@app.get("/auditoria/mp-status")
def auditoria_mp_status():
    import json as _json
    from pathlib import Path as _P
    mp = _P("data/mp_token.json")
    data = _json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
    token = data.get("access_token", "")
    return {"configurado": bool(token) and token != ".", "salvo_em": data.get("salvo_em")}

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


@app.get("/shopify/auth")
def shopify_auth(request: Request):
    from shopify_oauth import gerar_url_auth
    from fastapi.responses import RedirectResponse
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = base_url + "/shopify/callback"
    url = gerar_url_auth(redirect_uri)
    return RedirectResponse(url)

@app.get("/shopify/callback")
def shopify_callback(code: str = "", state: str = "", request: Request = None):
    from shopify_oauth import processar_callback
    from fastapi.responses import HTMLResponse
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = base_url + "/shopify/callback"
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
if not FILA_PATH.exists(): _save_json(FILA_PATH, [])
if not CFG_PATH.exists(): _save_json(CFG_PATH, DEFAULT_CFG)

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# MÃ“DULO REGRAS
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
    raise HTTPException(status_code=404, detail="pages/regras.html nÃ£o encontrado.")

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
        raise HTTPException(status_code=400, detail="Canal obrigatÃ³rio.")
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
        raise HTTPException(status_code=404, detail="Regra nÃ£o encontrada.")
    return {"ok": True, "total": len(carregar_regras())}

@app.delete("/regras/excluir/{idx}")
def regras_excluir(idx: int):
    ok = excluir_regra(idx)
    if not ok:
        raise HTTPException(status_code=404, detail="Regra nÃ£o encontrada.")
    return {"ok": True, "total": len(carregar_regras())}

@app.post("/regras/importar-excel")
async def regras_importar_excel(file: UploadFile = File(...)):
    if not file.filename.endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Apenas arquivos .xlsx sÃ£o aceitos.")
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
        raise HTTPException(status_code=404, detail="Arquivo modelo nÃ£o encontrado.")
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
    itens = carregar_fila()
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

@app.post("/auditoria/amazon/ignorar/{item_id}")
def auditoria_amazon_ignorar(item_id: str):
    from amazon_conferencia import carregar_fila, salvar_fila
    itens = carregar_fila()
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
