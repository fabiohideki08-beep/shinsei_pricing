import json
from pathlib import Path
from uuid import uuid4
from datetime import datetime
from typing import Optional, Literal, List
import openpyxl
from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from pydantic import BaseModel
from bling_client import (
    build_authorize_url,
    exchange_code_for_token,
    get_product_by_id,
    get_product_by_ean,
    get_product_by_sku,
    load_tokens,
)
from pricing_engine import calcular_canais, montar_precificacao_bling

load_dotenv()

app = FastAPI(title="Shinsei Pricing V2")
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

REGRAS_PATH = DATA_DIR / "regras.json"
CFG_PATH = DATA_DIR / "integracao_config.json"
APR_PATH = DATA_DIR / "aprovacoes.json"
HIST_PATH = DATA_DIR / "historico.json"
PEND_PATH = DATA_DIR / "pendentes.json"
LOGO_PATH = BASE_DIR / "shinsei_logo.png"
PRICING_LOG_PATH = DATA_DIR / "pricing_logs.jsonl"
WEBHOOK_LOG_PATH = DATA_DIR / "webhook_logs.jsonl"
ERROR_LOG_PATH = DATA_DIR / "error_logs.jsonl"


class InteligenciaVendasConfig(BaseModel):
    ativo: bool = False
    data_inicio: Optional[str] = None
    data_fim: Optional[str] = None
    peso_lucro: float = 0.6
    peso_liquidez: float = 0.4
    min_pedidos: int = 3
    min_unidades: int = 5
    ignorar_canais_prejuizo: bool = True
    peso_maximo_canal: float = 0.70
    ajuste_maximo_percentual: float = 3.0
    usar_share_lucro: bool = True


class SimulacaoPayload(BaseModel):
    preco_compra: float
    embalagem: float
    peso: float
    imposto: float
    quantidade: int
    objetivo: Literal["markup", "margem", "lucro_liquido"] = "lucro_liquido"
    tipo_alvo: Literal["percentual", "nominal"] = "nominal"
    valor_alvo: float
    inteligencia_vendas: Optional[InteligenciaVendasConfig] = None
    sku: Optional[str] = None


class PrecificacaoBlingPayload(BaseModel):
    criterio: Literal["ean", "sku", "id"] = "ean"
    valor_busca: str
    embalagem: float
    imposto: float
    quantidade: int
    objetivo: Literal["markup", "margem", "lucro_liquido"] = "lucro_liquido"
    tipo_alvo: Literal["percentual", "nominal"] = "nominal"
    valor_alvo: float
    peso_override: float = 0
    inteligencia_vendas: Optional[InteligenciaVendasConfig] = None
    modo_aprovacao: Literal["automatico", "manual"] = "manual"
    preco_compra_anterior_bling: float = 0
    modo_preco_virtual: Literal["percentual_acima", "valor_acima", "manual"] = "percentual_acima"
    acrescimo_percentual: float = 20
    acrescimo_nominal: float = 0
    preco_manual: float = 0
    arredondamento: Literal["sem", "90", "99", "97"] = "sem"
    regra_estoque_ativo: bool = False
    estoque_limite: int = 2
    ajuste_estoque_tipo: Literal["percentual", "nominal"] = "percentual"
    ajuste_estoque_valor: float = 0


class AprovacaoAcaoPayload(BaseModel):
    observacao: Optional[str] = None


def _to_float(v, default=0.0):
    if v in (None, ""):
        return default
    try:
        return float(str(v).replace("R$", "").replace("%", "").replace(".", "").replace(",", "."))
    except Exception:
        return default


def _cfg_default():
    return {
        "embalagem": 1.5,
        "imposto": 4.0,
        "quantidade": 1,
        "objetivo": "lucro_liquido",
        "tipo_alvo": "nominal",
        "valor_alvo": 10.0,
        "modo_aprovacao": "manual",
        "preco_compra_anterior_bling": 0.0,
        "modo_preco_virtual": "percentual_acima",
        "acrescimo_percentual": 20.0,
        "acrescimo_nominal": 0.0,
        "preco_manual": 0.0,
        "arredondamento": "sem",
        "regra_estoque_ativo": False,
        "estoque_limite": 2,
        "ajuste_estoque_tipo": "percentual",
        "ajuste_estoque_valor": 10.0,
        "tolerancia_variacao_percentual": 0.5,
        "tolerancia_variacao_nominal": 0.10,
        "inteligencia_vendas": InteligenciaVendasConfig().model_dump(),
    }


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_jsonl(path: Path, payload: dict):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _now_iso():
    return datetime.now().isoformat()


def carregar_regras(apenas_ativas=False):
    regras = _load_json(REGRAS_PATH, [])
    for r in regras:
        r.setdefault("ativo", True)
    return [r for r in regras if r.get("ativo", True)] if apenas_ativas else regras


def salvar_regras(regras):
    for r in regras:
        r["ativo"] = bool(r.get("ativo", True))
    _save_json(REGRAS_PATH, regras)


def carregar_cfg():
    data = _load_json(CFG_PATH, {})
    cfg = _cfg_default()
    if isinstance(data, dict):
        cfg.update(data)
    return cfg


def salvar_cfg(cfg):
    data = _cfg_default()
    if isinstance(cfg, dict):
        data.update(cfg)
    _save_json(CFG_PATH, data)


def carregar_aprovacoes():
    return _load_json(APR_PATH, [])


def salvar_aprovacoes(itens):
    _save_json(APR_PATH, itens)


def carregar_historico():
    return _load_json(HIST_PATH, [])


def salvar_historico(itens):
    _save_json(HIST_PATH, itens)


def add_aprovacao(item):
    itens = carregar_aprovacoes()
    itens.insert(0, item)
    salvar_aprovacoes(itens)
    return item


def _definir_prioridade(diferenca_percentual: float, lucro_liquido: float):
    if lucro_liquido < 0 or abs(diferenca_percentual) >= 10:
        return "alta"
    if abs(diferenca_percentual) >= 3:
        return "media"
    return "baixa"


def _comparar_variacao(preco_atual: float, preco_sugerido: float, cfg: dict):
    diff_nom = round(preco_sugerido - preco_atual, 2)
    diff_pct = round(((diff_nom / preco_atual) * 100) if preco_atual else 0.0, 2)
    passou_nominal = abs(diff_nom) >= float(cfg.get("tolerancia_variacao_nominal", 0.10) or 0.10)
    passou_percentual = abs(diff_pct) >= float(cfg.get("tolerancia_variacao_percentual", 0.5) or 0.5)
    return {
        "diferenca_nominal": diff_nom,
        "diferenca_percentual": diff_pct,
        "entrou_na_fila": bool(passou_nominal or passou_percentual),
    }


def _criar_item_aprovacao(resultado: dict, origem: str, valor_busca: str, motivo: str):
    melhor = resultado.get("melhor_resultado") or {}
    comparacao = resultado.get("comparacao_preco") or {}
    auditoria = resultado.get("auditoria") or {}
    item = {
        "id": str(uuid4()),
        "produto_nome": (resultado.get("produto_bling") or {}).get("nome") or valor_busca,
        "sku": (resultado.get("produto_bling") or {}).get("codigo") or valor_busca,
        "valor_busca": valor_busca,
        "canal": melhor.get("canal"),
        "motivo": motivo,
        "status_aprovacao": "pendente",
        "data_criacao": _now_iso(),
        "origem": origem,
        "preco_atual": comparacao.get("preco_atual", 0),
        "preco_sugerido": comparacao.get("preco_sugerido", 0),
        "diferenca_nominal": comparacao.get("diferenca_nominal", 0),
        "diferenca_percentual": comparacao.get("diferenca_percentual", 0),
        "prioridade": _definir_prioridade(
            comparacao.get("diferenca_percentual", 0),
            float(melhor.get("lucro_liquido", 0) or 0),
        ),
        "historico_utilizado": melhor.get("historico_utilizado", False),
        "score_final": melhor.get("indice_final", 0),
        "resultado": resultado,
        "auditoria": auditoria,
    }
    return add_aprovacao(item)


def set_status(item_id, status, observacao: Optional[str] = None):
    itens = carregar_aprovacoes()
    out = None
    for item in itens:
        if item.get("id") == item_id:
            item["status_aprovacao"] = status
            item["data_aprovacao"] = _now_iso()
            if observacao:
                item["observacao"] = observacao
            out = item
            break
    salvar_aprovacoes(itens)
    return out


@app.get("/logo")
def logo_file():
    if LOGO_PATH.exists():
        return FileResponse(LOGO_PATH)
    raise HTTPException(404, "Logo não encontrado.")


@app.get("/", response_class=HTMLResponse)
def home():
    for nome in ("index_v3.html", "index.html"):
        index_path = BASE_DIR / nome
        if index_path.exists():
            return HTMLResponse(index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h2>Shinsei Pricing V3</h2><p>index_v3.html não encontrado.</p>")


@app.get("/regras")
def get_regras():
    return carregar_regras(False)


@app.post("/regras")
def post_regras(regras: List[dict]):
    salvar_regras(regras)
    return {"message": "Regras salvas com sucesso."}


@app.get("/integracao-config")
def get_cfg():
    return carregar_cfg()


@app.post("/integracao-config")
def post_cfg(config: dict):
    salvar_cfg(config)
    return {"message": "Configuração da integração salva com sucesso."}




@app.get("/inteligencia-config")
def get_inteligencia_config():
    cfg = carregar_cfg()
    return cfg.get("inteligencia_vendas", InteligenciaVendasConfig().model_dump())


@app.post("/inteligencia-config")
def post_inteligencia_config(config: dict):
    cfg = carregar_cfg()
    base = cfg.get("inteligencia_vendas", InteligenciaVendasConfig().model_dump())
    if isinstance(config, dict):
        base.update(config)
    cfg["inteligencia_vendas"] = base
    salvar_cfg(cfg)
    return {"message": "Configuração da inteligência de vendas salva com sucesso.", "config": base}


@app.get("/inteligencia/resumo")
def inteligencia_resumo(data_inicio: Optional[str] = None, data_fim: Optional[str] = None, sku: Optional[str] = None):
    historico = carregar_historico()
    cfg = carregar_cfg().get("inteligencia_vendas", {})
    data_inicio = data_inicio or cfg.get("data_inicio")
    data_fim = data_fim or cfg.get("data_fim")

    def _to_date(v):
        try:
            return datetime.fromisoformat(str(v)[:10]).date()
        except Exception:
            return None

    ini = _to_date(data_inicio) if data_inicio else None
    fim = _to_date(data_fim) if data_fim else None
    canais = {}
    total_registros = 0
    skus = set()
    for item in historico:
        item_sku = str(item.get("sku", "")).strip()
        if sku and item_sku != str(sku).strip():
            continue
        dt = _to_date(item.get("data"))
        if ini and (not dt or dt < ini):
            continue
        if fim and (not dt or dt > fim):
            continue
        total_registros += 1
        if item_sku:
            skus.add(item_sku)
        canal = str(item.get("canal", "Sem canal")).strip() or "Sem canal"
        base = canais.setdefault(canal, {"quantidade": 0.0, "pedidos": 0.0, "receita": 0.0, "lucro_liquido": 0.0})
        base["quantidade"] += float(item.get("quantidade", 0) or 0)
        base["pedidos"] += float(item.get("pedidos", 0) or 0)
        base["receita"] += float(item.get("receita", 0) or 0)
        base["lucro_liquido"] += float(item.get("lucro_liquido", 0) or 0)

    lucro_total_positivo = sum(max(v["lucro_liquido"], 0.0) for v in canais.values())
    dias = 30
    if ini and fim:
        dias = max((fim - ini).days + 1, 1)
    canais_out = []
    for canal, valores in canais.items():
        share = (max(valores["lucro_liquido"], 0.0) / lucro_total_positivo) if lucro_total_positivo > 0 else 0.0
        velocidade = valores["quantidade"] / dias if dias else 0.0
        canais_out.append({
            "canal": canal,
            "quantidade": round(valores["quantidade"], 2),
            "pedidos": round(valores["pedidos"], 2),
            "receita": round(valores["receita"], 2),
            "lucro_liquido": round(valores["lucro_liquido"], 2),
            "share_lucro": round(share, 4),
            "velocidade_dia": round(velocidade, 4),
        })
    canais_out.sort(key=lambda x: x["lucro_liquido"], reverse=True)
    return {
        "ativo": bool(cfg.get("ativo", False)),
        "data_inicio": data_inicio,
        "data_fim": data_fim,
        "dias_periodo": dias,
        "total_registros": total_registros,
        "total_skus": len(skus),
        "lucro_total_positivo": round(lucro_total_positivo, 2),
        "canais": canais_out,
    }

@app.get("/historico")
def get_historico():
    return carregar_historico()


@app.post("/historico")
def post_historico(historico: List[dict]):
    salvar_historico(historico)
    return {"message": "Histórico salvo com sucesso.", "total_registros": len(historico)}


@app.get("/aprovacoes")
def get_apr(status: Optional[str] = None):
    itens = carregar_aprovacoes()
    if status:
        itens = [i for i in itens if i.get("status_aprovacao") == status]
    return itens


@app.post("/aprovacoes/{item_id}/aprovar")
def aprovar(item_id: str, payload: AprovacaoAcaoPayload | None = None):
    item = set_status(item_id, "aprovado", payload.observacao if payload else None)
    if not item:
        raise HTTPException(404, "Item não encontrado.")
    return {"message": "Ajuste aprovado.", "item": item}


@app.post("/aprovacoes/{item_id}/reprovar")
def reprovar(item_id: str, payload: AprovacaoAcaoPayload):
    if not payload.observacao:
        raise HTTPException(400, "Informe uma observação para reprovar.")
    item = set_status(item_id, "reprovado", payload.observacao)
    if not item:
        raise HTTPException(404, "Item não encontrado.")
    return {"message": "Ajuste reprovado.", "item": item}


@app.get("/aprovacoes/resumo")
def aprovacoes_resumo():
    itens = carregar_aprovacoes()
    resumo = {"pendente": 0, "aprovado": 0, "reprovado": 0, "erro": 0}
    for item in itens:
        status = item.get("status_aprovacao", "pendente")
        resumo[status] = resumo.get(status, 0) + 1
    return resumo


@app.post("/upload-excel")
async def upload_excel(file: UploadFile = File(...)):
    wb = openpyxl.load_workbook(file.file, data_only=True)
    ws = wb.active
    regras = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or row[0] in (None, ""):
            continue
        regras.append(
            {
                "canal": str(row[0]).strip(),
                "peso_min": _to_float(row[1]),
                "peso_max": _to_float(row[2]),
                "preco_min": _to_float(row[3]),
                "preco_max": _to_float(row[4]),
                "taxa_fixa": _to_float(row[5]),
                "taxa_frete": _to_float(row[6]),
                "comissao": _to_float(row[7]),
                "ativo": True,
            }
        )
    salvar_regras(regras)
    return {"message": f"{len(regras)} regras importadas com sucesso."}


def _inteligencia_from_payload(payload_cfg):
    if hasattr(payload_cfg, "model_dump"):
        return payload_cfg.model_dump()
    if isinstance(payload_cfg, dict):
        return payload_cfg
    cfg = carregar_cfg()
    return cfg.get("inteligencia_vendas", {})


@app.post("/calcular")
def calcular(payload: SimulacaoPayload):
    regras = carregar_regras(True)
    if not regras:
        raise HTTPException(400, "Nenhuma regra ativa cadastrada.")
    resultado = calcular_canais(
        regras=regras,
        preco_compra=payload.preco_compra,
        embalagem=payload.embalagem,
        peso=payload.peso,
        imposto=payload.imposto,
        quantidade=payload.quantidade,
        objetivo=payload.objetivo,
        tipo_alvo=payload.tipo_alvo,
        valor_alvo=payload.valor_alvo,
        intelligence_config=_inteligencia_from_payload(payload.inteligencia_vendas),
        historical_data=carregar_historico(),
        sku=payload.sku,
    )
    _append_jsonl(
        PRICING_LOG_PATH,
        {"tipo": "simulacao", "data": _now_iso(), "sku": payload.sku, "resultado": resultado},
    )
    return resultado


@app.get("/bling/login")
def bling_login():
    return RedirectResponse(build_authorize_url())


@app.get("/bling/callback")
def bling_callback(code: str | None = None, error: str | None = None):
    if error:
        raise HTTPException(400, f"Erro retornado pelo Bling: {error}")
    if not code:
        raise HTTPException(400, "Authorization code não recebido.")
    tokens = exchange_code_for_token(code)
    return HTMLResponse(
        f"<html><body style='font-family:Arial;padding:30px'>"
        f"<h2>Conexão com Bling realizada</h2>"
        f"<p>Tokens salvos com sucesso.</p>"
        f"<pre>{json.dumps({'scope': tokens.get('scope'), 'expires_in': tokens.get('expires_in')}, ensure_ascii=False, indent=2)}</pre>"
        f"<p><a href='/'>Voltar ao sistema</a></p></body></html>"
    )


@app.get("/bling/status")
def bling_status():
    tokens = load_tokens()
    return {
        "conectado": bool(tokens.get("access_token")),
        "tem_refresh_token": bool(tokens.get("refresh_token")),
        "arquivo_tokens": str(DATA_DIR / "bling_tokens.json"),
    }


@app.get("/bling/produto/{produto_id}")
def produto_id(produto_id: str):
    return get_product_by_id(produto_id)


@app.get("/bling/produto-por-ean/{ean}")
def produto_ean(ean: str):
    return get_product_by_ean(ean)


@app.get("/bling/produto-por-sku/{sku}")
def produto_sku(sku: str):
    return get_product_by_sku(sku)


@app.post("/bling/precificar-produto")
def bling_precificar(payload: PrecificacaoBlingPayload):
    regras = carregar_regras(True)
    if not regras:
        raise HTTPException(400, "Nenhuma regra ativa cadastrada.")

    cfg = carregar_cfg()
    resultado = montar_precificacao_bling(
        regras=regras,
        criterio=payload.criterio,
        valor_busca=payload.valor_busca,
        embalagem=payload.embalagem,
        imposto=payload.imposto,
        quantidade=payload.quantidade,
        objetivo=payload.objetivo,
        tipo_alvo=payload.tipo_alvo,
        valor_alvo=payload.valor_alvo,
        peso_override=payload.peso_override,
        intelligence_config=_inteligencia_from_payload(payload.inteligencia_vendas),
        historical_data=carregar_historico(),
        modo_aprovacao=payload.modo_aprovacao,
        preco_compra_anterior_bling=payload.preco_compra_anterior_bling,
        modo_preco_virtual=payload.modo_preco_virtual,
        acrescimo_percentual=payload.acrescimo_percentual,
        acrescimo_nominal=payload.acrescimo_nominal,
        preco_manual=payload.preco_manual,
        arredondamento=payload.arredondamento,
        regra_estoque={
            "ativo": payload.regra_estoque_ativo,
            "limite": payload.estoque_limite,
            "tipo": payload.ajuste_estoque_tipo,
            "valor": payload.ajuste_estoque_valor,
        },
    )

    if "erro" not in resultado:
        comparacao = _comparar_variacao(
            float((resultado.get("produto_bling") or {}).get("preco", 0) or 0),
            float((resultado.get("melhor_resultado") or {}).get("preco_final", 0) or 0),
            cfg,
        )
        comparacao["preco_atual"] = float((resultado.get("produto_bling") or {}).get("preco", 0) or 0)
        comparacao["preco_sugerido"] = float((resultado.get("melhor_resultado") or {}).get("preco_final", 0) or 0)
        resultado["comparacao_preco"] = comparacao

        if payload.modo_aprovacao == "manual" and resultado.get("melhor_resultado") and comparacao["entrou_na_fila"]:
            item = _criar_item_aprovacao(
                resultado=resultado,
                origem="precificacao_manual",
                valor_busca=payload.valor_busca,
                motivo="recalculo_manual",
            )
            resultado["fila_aprovacao"] = {"criado": True, "status": "pendente", "item_id": item["id"]}
        elif payload.modo_aprovacao == "manual":
            resultado["fila_aprovacao"] = {"criado": False, "status": "ignorado_por_tolerancia"}
        else:
            resultado["fila_aprovacao"] = {"criado": False, "status": "pronto_para_aplicacao_automatica"}

    _append_jsonl(
        PRICING_LOG_PATH,
        {
            "tipo": "bling_precificar",
            "data": _now_iso(),
            "valor_busca": payload.valor_busca,
            "resultado": resultado,
        },
    )
    return resultado


@app.post("/webhook/bling")
async def webhook_bling(request: Request):
    try:
        data = await request.json()
        _append_jsonl(WEBHOOK_LOG_PATH, {"data": _now_iso(), "payload": data})

        cfg = carregar_cfg()
        regras = carregar_regras(True)
        criterio = "sku" if data.get("sku") else "ean" if data.get("ean") else "id"
        valor_busca = data.get("sku") or data.get("ean") or data.get("produto_id")

        if not valor_busca:
            item = {
                "id": str(uuid4()),
                "produto_nome": "Evento Bling",
                "valor_busca": "",
                "motivo": data.get("motivo", "evento_bling"),
                "status_aprovacao": "pendente",
                "data_criacao": _now_iso(),
                "origem": "webhook",
                "payload_webhook": data,
            }
            add_aprovacao(item)
            return {"status": "ok", "message": "Webhook recebido sem identificador; item básico criado."}

        resultado = montar_precificacao_bling(
            regras=regras,
            criterio=criterio,
            valor_busca=str(valor_busca),
            embalagem=float(cfg.get("embalagem", 0) or 0),
            imposto=float(cfg.get("imposto", 0) or 0),
            quantidade=int(cfg.get("quantidade", 1) or 1),
            objetivo=cfg.get("objetivo", "lucro_liquido"),
            tipo_alvo=cfg.get("tipo_alvo", "nominal"),
            valor_alvo=float(cfg.get("valor_alvo", 0) or 0),
            intelligence_config=cfg.get("inteligencia_vendas", {}),
            historical_data=carregar_historico(),
            modo_aprovacao="manual",
            preco_compra_anterior_bling=float(cfg.get("preco_compra_anterior_bling", 0) or 0),
            modo_preco_virtual=cfg.get("modo_preco_virtual", "percentual_acima"),
            acrescimo_percentual=float(cfg.get("acrescimo_percentual", 0) or 0),
            acrescimo_nominal=float(cfg.get("acrescimo_nominal", 0) or 0),
            preco_manual=float(cfg.get("preco_manual", 0) or 0),
            arredondamento=cfg.get("arredondamento", "sem"),
            regra_estoque={
                "ativo": cfg.get("regra_estoque_ativo", False),
                "limite": cfg.get("estoque_limite", 2),
                "tipo": cfg.get("ajuste_estoque_tipo", "percentual"),
                "valor": cfg.get("ajuste_estoque_valor", 0),
            },
        )

        if "erro" in resultado:
            _append_jsonl(ERROR_LOG_PATH, {"data": _now_iso(), "tipo": "webhook", "erro": resultado})
            return {"status": "erro", "resultado": resultado}

        comparacao = _comparar_variacao(
            float((resultado.get("produto_bling") or {}).get("preco", 0) or 0),
            float((resultado.get("melhor_resultado") or {}).get("preco_final", 0) or 0),
            cfg,
        )
        comparacao["preco_atual"] = float((resultado.get("produto_bling") or {}).get("preco", 0) or 0)
        comparacao["preco_sugerido"] = float((resultado.get("melhor_resultado") or {}).get("preco_final", 0) or 0)
        resultado["comparacao_preco"] = comparacao

        if comparacao["entrou_na_fila"]:
            item = _criar_item_aprovacao(
                resultado=resultado,
                origem="webhook",
                valor_busca=str(valor_busca),
                motivo=data.get("motivo", "evento_bling"),
            )
            return {"status": "ok", "message": "Webhook processado e fila atualizada.", "item_id": item["id"]}

        return {"status": "ok", "message": "Webhook processado sem gerar fila por tolerância."}

    except Exception as e:
        _append_jsonl(ERROR_LOG_PATH, {"data": _now_iso(), "tipo": "webhook_exception", "erro": str(e)})
        return {"status": "erro", "message": str(e)}
