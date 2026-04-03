
from __future__ import annotations
import importlib, json, uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
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
@app.on_event("startup")
def startup():
    init_db()
    migrar_json_legado()
    iniciar_scheduler_background()

@app.on_event("shutdown")
def shutdown():
    parar_scheduler()

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
    itens = _load_json(FILA_PATH, [])
    return itens if isinstance(itens, list) else []

def salvar_fila(itens: list[dict]) -> None:
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
    itens = carregar_fila()
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
        patch = _prepare_product_patch(existing)
        patch["id"] = int(payload.produto_id)
        patch["preco"] = round(float(payload.valor), 2)
        result = client.update_product(int(payload.produto_id), patch)
        atualizado = client.get_product(int(payload.produto_id))
        return {"ok": True, "message": "Preço atualizado no Bling.", "produto": atualizado, "raw": result}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Falha ao atualizar preço no Bling: {exc}")

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
            itens_fila = carregar_fila()
            sku = preview["auditoria"].get("sku") or preview["produto"].get("codigo") or ""
            if _ja_existe_pendente_semelhante(itens_fila, sku, preview["auditoria"]):
                fila_auto = {"adicionado":False,"motivo":"Já existe item pendente equivalente na fila."}
            else:
                item = _montar_item_fila(preview, payload.dict())
                itens_fila.insert(0, item); salvar_fila(itens_fila)
                _append_jsonl(LOG_PATH, {"evento":"fila_auto_preview","item_id":item["id"],"sku":item["sku"],"quando":item["criado_em"]})
                fila_auto = {"adicionado":True,"item_id":item["id"]}
        else:
            fila_auto = {"adicionado":False,"motivo":motivo or diagnostico.get("mensagem") or "Fila automática desativada."}
        preview["fila_auto"] = fila_auto
        return preview
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Falha no preview: {exc}")

@app.get("/fila/lista")
def fila_lista():
    itens = carregar_fila()
    return {"itens":itens,"stats":_fila_stats(itens)}

@app.post("/fila/adicionar")
def fila_adicionar(payload: dict = Body(...)):
    preview = {"ok":payload.get("ok", True),"produto":payload.get("produto_bling") or payload.get("produto") or {},"marketplaces":payload.get("marketplaces") or {},"auditoria":payload.get("auditoria") or {},"raw":payload.get("raw") or {}}
    diag = _diagnostico_preview(preview)
    if not diag.get("ok"): raise HTTPException(status_code=400, detail=f"Preview inválido para fila: {diag.get('mensagem')}")
    itens = carregar_fila()
    sku = (preview.get("auditoria") or {}).get("sku") or (preview.get("produto") or {}).get("codigo") or ""
    if _ja_existe_pendente_semelhante(itens, sku, preview.get("auditoria") or {}):
        return {"ok":True,"duplicado":True,"message":"Já existe item pendente equivalente na fila.","stats":_fila_stats(itens)}
    item = _montar_item_fila(preview, payload.get("payload_original") or payload.get("raw") or {})
    itens.insert(0, item); salvar_fila(itens)
    _append_jsonl(LOG_PATH, {"evento":"fila_adicionar_manual","item_id":item["id"],"sku":item["sku"],"quando":item["criado_em"]})
    return {"ok":True,"item":item,"stats":_fila_stats(itens)}

@app.post("/fila/limpar-invalidos")
def fila_limpar_invalidos():
    itens = carregar_fila()
    validos, removidos = [], []
    for item in itens:
        preview_like = {"ok":True,"produto":item.get("produto_bling") or {},"marketplaces":item.get("marketplaces") or {},"auditoria":item.get("auditoria") or {}}
        diag = _diagnostico_preview(preview_like)
        if diag.get("ok") or item.get("status") in {"aprovado","rejeitado"}: validos.append(item)
        else: removidos.append({"id":item.get("id"),"sku":item.get("sku"),"motivo":diag.get("mensagem")})
    salvar_fila(validos)
    return {"ok":True,"removidos":removidos,"stats":_fila_stats(validos)}

@app.post("/fila/reset-total")
def fila_reset_total():
    salvar_fila([])
    return {"ok":True,"message":"Fila completamente limpa","stats":_fila_stats([])}

@app.post("/fila/aprovar/{item_id}")
def fila_aprovar(item_id: str):
    itens = carregar_fila()
    item = next((i for i in itens if i.get("id") == item_id), None)
    if not item: raise HTTPException(status_code=404, detail="Item não encontrado na fila.")
    if not BlingClient or not aplicar_precos_multicanal:
        raise HTTPException(status_code=500, detail="Integração de aplicação no Bling indisponível.")
    try:
        client = BlingClient(); resultado = aplicar_precos_multicanal(client, item)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Falha ao aplicar preços no Bling: {exc}")
    agora = datetime.utcnow().isoformat()
    item["status"] = "aprovado"; item["atualizado_em"] = agora; item["resultado_aplicacao"] = resultado
    item.setdefault("historico_decisao", []).append({"acao":"aprovado","quando":agora,"resultado_resumido":{"ok":True,"estrategia":resultado.get("estrategia")}})
    salvar_fila(itens); _append_jsonl(LOG_PATH, {"evento":"fila_aprovado","item_id":item_id,"quando":agora})
    return {"ok":True,"message":"Preços aplicados no Bling.","stats":_fila_stats(itens)}

@app.post("/fila/rejeitar/{item_id}")
def fila_rejeitar(item_id: str, payload: dict = Body(default={})):
    itens = carregar_fila()
    item = next((i for i in itens if i.get("id") == item_id), None)
    if not item: raise HTTPException(status_code=404, detail="Item não encontrado na fila.")
    agora = datetime.utcnow().isoformat(); motivo = payload.get("motivo") or "Rejeitado manualmente."
    item["status"] = "rejeitado"; item["atualizado_em"] = agora
    item.setdefault("historico_decisao", []).append({"acao":"rejeitado","quando":agora,"motivo":motivo})
    salvar_fila(itens); _append_jsonl(LOG_PATH, {"evento":"fila_rejeitado","item_id":item_id,"quando":agora,"motivo":motivo})
    return {"ok":True,"message":"Item marcado como rejeitado.","stats":_fila_stats(itens)}

if not FILA_PATH.exists(): _save_json(FILA_PATH, [])
if not CFG_PATH.exists(): _save_json(CFG_PATH, DEFAULT_CFG)

# ─────────────────────────────────────────────
# MÓDULO REGRAS
# ─────────────────────────────────────────────

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
    regras = _load_json(REGRAS_PATH, [])
    if not isinstance(regras, list): regras = []
    for i, r in enumerate(regras):
        if isinstance(r, dict): r["_idx"] = i
    return {"regras": regras, "total": len(regras)}

@app.post("/regras/adicionar")
def regras_adicionar(payload: dict = Body(...)):
    regras = _load_json(REGRAS_PATH, [])
    if not isinstance(regras, list): regras = []
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
    regras.append(nova)
    _save_json(REGRAS_PATH, regras)
    return {"ok": True, "total": len(regras)}

@app.post("/regras/editar/{idx}")
def regras_editar(idx: int, payload: dict = Body(...)):
    regras = _load_json(REGRAS_PATH, [])
    if not isinstance(regras, list) or idx >= len(regras):
        raise HTTPException(status_code=404, detail="Regra não encontrada.")
    regras[idx] = {
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
    _save_json(REGRAS_PATH, regras)
    return {"ok": True, "total": len(regras)}

@app.delete("/regras/excluir/{idx}")
def regras_excluir(idx: int):
    regras = _load_json(REGRAS_PATH, [])
    if not isinstance(regras, list) or idx >= len(regras):
        raise HTTPException(status_code=404, detail="Regra não encontrada.")
    regras.pop(idx)
    _save_json(REGRAS_PATH, regras)
    return {"ok": True, "total": len(regras)}

@app.post("/regras/importar-excel")
async def regras_importar_excel(file: UploadFile = File(...)):
    if not file.filename.endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Apenas arquivos .xlsx são aceitos.")
    try:
        import io
        import openpyxl
        contents = await file.read()
        wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True)
        # Aceita aba "Regras", "Aba2" ou a primeira aba
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
        _save_json(REGRAS_PATH, regras)
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
