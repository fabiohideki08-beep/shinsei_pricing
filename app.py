from __future__ import annotations

import importlib
import json
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import openpyxl
from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel


# ============================================================
# Paths / app
# ============================================================
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
PAGES_DIR = BASE_DIR / "pages"
REGRAS_PATH = DATA_DIR / "regras.json"
FILA_PATH = DATA_DIR / "fila_aprovacao.json"
LOG_PATH = DATA_DIR / "historico_precificacao.jsonl"
CFG_PATH = DATA_DIR / "config.json"

DATA_DIR.mkdir(exist_ok=True)
PAGES_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Shinsei Pricing")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# Dynamic imports: engine / bling / routers
# ============================================================
def _optional_import(module_name: str):
    try:
        return importlib.import_module(module_name)
    except Exception:
        return None


pricing_module = _optional_import("pricing_engine_real") or _optional_import("pricing_engine")
if pricing_module is None:
    raise RuntimeError("pricing_engine.py não encontrado. O app precisa do motor de precificação.")

calcular_canais: Callable[..., dict] = getattr(pricing_module, "calcular_canais")
gerar_integracao: Optional[Callable[..., dict]] = getattr(pricing_module, "gerar_integracao", None)
montar_precificacao_bling: Optional[Callable[..., dict]] = getattr(pricing_module, "montar_precificacao_bling", None)

bling_mod = _optional_import("bling_client")
BlingClient = getattr(bling_mod, "BlingClient", None) if bling_mod else None
BlingAPIError = getattr(bling_mod, "BlingAPIError", Exception) if bling_mod else Exception
BlingAuthError = getattr(bling_mod, "BlingAuthError", Exception) if bling_mod else Exception
BlingConfigError = getattr(bling_mod, "BlingConfigError", Exception) if bling_mod else Exception

bling_update_module = _optional_import("bling_update_engine")
aplicar_precos_multicanal = getattr(bling_update_module, "aplicar_precos_multicanal", None) if bling_update_module else None


# ============================================================
# Optional routers
# ============================================================
def include_optional_router(module_name: str) -> bool:
    try:
        module = importlib.import_module(f"routes.{module_name}")
        router = getattr(module, "router", None)
        if router is None:
            print(f"[WARN] routes.{module_name} encontrado, mas sem atributo 'router'.")
            return False
        app.include_router(router)
        print(f"[OK] Router carregado: routes.{module_name}")
        return True
    except Exception as exc:
        print(f"[WARN] Não foi possível carregar routes.{module_name}: {exc}")
        return False


ROUTERS_STATUS = {
    "mercado_livre": include_optional_router("mercado_livre"),
    "bling": False,
}


# ============================================================
# Defaults / helpers
# ============================================================
DEFAULT_CFG = {
    "modo_aprovacao": "manual",
    "peso_forca": 0.40,
    "peso_equilibrio": 0.40,
    "peso_lucro": 0.20,
    "forcas_canais": {
        "Mercado Livre Classico": 0.80,
        "Mercado Livre Premium": 0.75,
        "Shopee": 0.60,
        "Amazon": 0.70,
        "Shein": 0.55,
        "Shopfy": 0.65,
        "Shopify": 0.65,
    },
    "regra_estoque": {"ativo": False, "limite": 2, "tipo": "percentual", "valor": 0},
}


CANAL_ALIAS = {
    "Mercado Livre Classico": "mercado_livre_classico",
    "Mercado Livre Premium": "mercado_livre_premium",
    "Shopee": "shopee",
    "Amazon": "amazon",
    "Shein": "shein",
    "Shopfy": "shopify",
    "Shopify": "shopify",
}


def _load_json(path: Path, default: Any):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    txt = str(value).strip().replace("R$", "").replace("%", "")
    if "," in txt and "." in txt:
        txt = txt.replace(".", "").replace(",", ".")
    else:
        txt = txt.replace(",", ".")
    try:
        return float(txt)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _first_existing(path_options: list[Path]) -> Optional[Path]:
    for path in path_options:
        if path.exists():
            return path
    return None


def carregar_regras(apenas_ativas: bool = False) -> list[dict]:
    regras = _load_json(REGRAS_PATH, [])
    if not isinstance(regras, list):
        return []
    for r in regras:
        if isinstance(r, dict):
            r.setdefault("ativo", True)
    if apenas_ativas:
        return [r for r in regras if isinstance(r, dict) and r.get("ativo", True)]
    return [r for r in regras if isinstance(r, dict)]


def salvar_regras(regras: list[dict]) -> None:
    normalizadas = []
    for r in regras:
        if not isinstance(r, dict):
            continue
        item = dict(r)
        item["ativo"] = bool(item.get("ativo", True))
        normalizadas.append(item)
    _save_json(REGRAS_PATH, normalizadas)


def carregar_cfg() -> dict:
    data = _load_json(CFG_PATH, {})
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    if isinstance(data, dict):
        cfg.update(data)
        cfg["forcas_canais"] = {**DEFAULT_CFG["forcas_canais"], **data.get("forcas_canais", {})}
        cfg["regra_estoque"] = {**DEFAULT_CFG["regra_estoque"], **data.get("regra_estoque", {})}
    return cfg


def salvar_cfg(cfg: dict) -> None:
    atual = carregar_cfg()
    atual.update(cfg or {})
    if "forcas_canais" in (cfg or {}):
        atual["forcas_canais"] = {**carregar_cfg()["forcas_canais"], **cfg.get("forcas_canais", {})}
    if "regra_estoque" in (cfg or {}):
        atual["regra_estoque"] = {**carregar_cfg()["regra_estoque"], **cfg.get("regra_estoque", {})}
    _save_json(CFG_PATH, atual)


def carregar_fila() -> list[dict]:
    itens = _load_json(FILA_PATH, [])
    return itens if isinstance(itens, list) else []


def salvar_fila(itens: list[dict]) -> None:
    _save_json(FILA_PATH, itens)


def _normalizar_marketplaces(itens: list[dict]) -> dict[str, dict]:
    marketplaces = {}
    for item in itens or []:
        canal = item.get("canal") or "Canal"
        key = CANAL_ALIAS.get(canal, canal.lower().replace(" ", "_"))
        marketplaces[key] = {
            "label": canal,
            "preco": item.get("preco_virtual")
            or item.get("preco_cheio")
            or item.get("preco_sugerido")
            or item.get("preco_promocional")
            or item.get("preco_final")
            or 0,
            "preco_promocional": item.get("preco_promocional") or item.get("preco_final") or 0,
            "lucro": item.get("lucro_liquido") or item.get("lucro") or 0,
            "margem": item.get("margem") or 0,
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


# ============================================================
# Schemas
# ============================================================
class SimulacaoPayload(BaseModel):
    preco_compra: float = 0
    embalagem: float = 0
    peso: float = 0
    imposto: float = 4
    quantidade: int = 1
    objetivo: str = "markup"
    tipo_alvo: str = "percentual"
    valor_alvo: float = 30
    score_config: Optional[dict] = None


class BuscaProdutoPayload(BaseModel):
    criterio: str = "sku"
    valor: str


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


# ============================================================
# HTML fallback
# ============================================================
FALLBACK_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Shinsei Pricing</title>
<style>
:root{--red:#ef0030;--navy:#08162f;--bg:#eef2f7;--card:#fff;--line:#dbe4ef;--text:#10233f;--muted:#6b7d96;--ok:#11875d;--bad:#c53030}
*{box-sizing:border-box} body{margin:0;font-family:Arial,Helvetica,sans-serif;background:var(--bg);color:var(--text)}
.top{background:var(--red);color:#fff;padding:22px 24px}.top h1{margin:0;font-size:32px}.top p{margin:6px 0 0}
.wrap{max-width:1400px;margin:20px auto;padding:0 16px 40px}.grid{display:grid;grid-template-columns:420px 1fr;gap:18px}.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px;box-shadow:0 8px 24px rgba(10,20,40,.05)}
.card h2{margin:0 0 14px}.field{margin-bottom:12px}.field label{display:block;font-size:13px;color:var(--muted);margin-bottom:6px}.field input,.field select{width:100%;height:42px;border:1px solid var(--line);border-radius:12px;padding:0 12px;background:#fff}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}.actions{display:flex;gap:10px;flex-wrap:wrap}.btn{border:0;border-radius:12px;padding:12px 16px;font-weight:700;cursor:pointer}.btn-primary{background:var(--navy);color:#fff}.btn-secondary{background:#fff;border:1px solid var(--line)}
.badges{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}.badge{padding:10px 14px;border-radius:999px;background:#f4f7fb;border:1px solid var(--line);font-size:13px}
.table{width:100%;border-collapse:collapse}.table th{background:var(--navy);color:#fff;text-align:left;padding:12px}.table td{padding:12px;border-bottom:1px solid #edf2f7;background:#fff}.good{color:var(--ok);font-weight:700}.bad{color:var(--bad);font-weight:700}
.muted{color:var(--muted)} pre{white-space:pre-wrap;background:#f8fafc;padding:14px;border-radius:12px;border:1px solid var(--line)}
@media(max-width:1000px){.grid,.row{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="top"><h1>Shinsei Pricing</h1><p>Versão final do app — simulador + engine real + integração Bling</p></div>
<div class="wrap">
  <div class="grid">
    <div class="card">
      <h2>Simulador</h2>
      <div class="field"><label>Busca automática no Bling</label><select id="criterio"><option value="sku">SKU</option><option value="nome">Nome</option><option value="ean">EAN</option><option value="id">ID</option></select></div>
      <div class="field"><label>Nome / SKU / EAN / ID</label><input id="valor_busca" placeholder="Digite nome, sku, ean ou id"></div>
      <div class="row">
        <div class="field"><label>Embalagem</label><input id="embalagem" value="1,00"></div>
        <div class="field"><label>Imposto (%)</label><input id="imposto" value="4"></div>
      </div>
      <div class="row">
        <div class="field"><label>Quantidade</label><input id="quantidade" value="1"></div>
        <div class="field"><label>Peso override (kg)</label><input id="peso_override" value="0"></div>
      </div>
      <div class="row">
        <div class="field"><label>Objetivo</label><select id="objetivo"><option value="lucro_liquido">Lucro líquido</option><option value="markup">Markup</option><option value="margem">Margem</option></select></div>
        <div class="field"><label>Tipo alvo</label><select id="tipo_alvo"><option value="percentual">Percentual</option><option value="nominal">Nominal</option></select></div>
      </div>
      <div class="row">
        <div class="field"><label>Valor alvo</label><input id="valor_alvo" value="30"></div>
        <div class="field"><label>Arredondamento</label><select id="arredondamento"><option value="90">.90</option><option value="99">.99</option><option value="97">.97</option><option value="sem">Sem</option></select></div>
      </div>
      <div class="row">
        <div class="field"><label>Preço virtual</label><select id="modo_preco_virtual"><option value="percentual_acima">Percentual acima</option><option value="valor_acima">Valor acima</option><option value="manual">Manual</option></select></div>
        <div class="field"><label>% / Valor acima</label><input id="acrescimo_percentual" value="20"></div>
      </div>
      <div class="actions">
        <button class="btn btn-primary" onclick="previewIntegracao()">Buscar e simular</button>
        <button class="btn btn-secondary" onclick="statusBling()">Status Bling</button>
      </div>
      <div id="msg" class="muted" style="margin-top:12px"></div>
    </div>
    <div class="card">
      <h2>Resultado</h2>
      <div class="badges">
        <div class="badge" id="badgeProduto">Produto: -</div>
        <div class="badge" id="badgeMelhor">Melhor canal: -</div>
        <div class="badge" id="badgeModo">Modo: -</div>
      </div>
      <table class="table">
        <thead><tr><th>Canal</th><th>Preço</th><th>Promo</th><th>Lucro</th><th>Margem</th><th>Faixa</th></tr></thead>
        <tbody id="tbody"></tbody>
      </table>
      <h3>Auditoria</h3>
      <pre id="audit">Nenhuma simulação ainda.</pre>
    </div>
  </div>
</div>
<script>
function num(v){if(v===null||v===undefined)return 0; return Number(String(v).replace(/\\./g,'').replace(',','.'))||0;}
function money(v){return new Intl.NumberFormat('pt-BR',{style:'currency',currency:'BRL'}).format(Number(v||0));}
async function statusBling(){const r=await fetch('/bling/status'); const j=await r.json(); document.getElementById('msg').innerText=JSON.stringify(j);}
async function previewIntegracao(){
  const payload={
    criterio: document.getElementById('criterio').value,
    valor_busca: document.getElementById('valor_busca').value,
    embalagem: num(document.getElementById('embalagem').value),
    imposto: num(document.getElementById('imposto').value),
    quantidade: num(document.getElementById('quantidade').value),
    peso_override: num(document.getElementById('peso_override').value),
    objetivo: document.getElementById('objetivo').value,
    tipo_alvo: document.getElementById('tipo_alvo').value,
    valor_alvo: num(document.getElementById('valor_alvo').value),
    arredondamento: document.getElementById('arredondamento').value,
    modo_preco_virtual: document.getElementById('modo_preco_virtual').value,
    acrescimo_percentual: num(document.getElementById('acrescimo_percentual').value),
    acrescimo_nominal: num(document.getElementById('acrescimo_percentual').value),
    modo_aprovacao: 'manual'
  };
  const res = await fetch('/integracao/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const data = await res.json();
  if(!res.ok){document.getElementById('msg').innerText=data.detail||data.erro||'Erro'; return;}
  document.getElementById('badgeProduto').innerText='Produto: '+((data.produto&&data.produto.nome)||data.produto||'-');
  document.getElementById('badgeMelhor').innerText='Melhor canal: '+(data.melhor_canal||'-');
  document.getElementById('badgeModo').innerText='Modo: '+(data.modo_aprovacao||'-');
  const tbody=document.getElementById('tbody'); tbody.innerHTML='';
  const rows = Object.values(data.marketplaces||{});
  rows.forEach(item=>{
    const tr=document.createElement('tr');
    tr.innerHTML=`<td>${item.label||'-'}</td><td>${money(item.preco)}</td><td>${money(item.preco_promocional)}</td><td class="${Number(item.lucro||0)<0?'bad':'good'}">${money(item.lucro)}</td><td class="${Number(item.margem||0)<0?'bad':'good'}">${Number(item.margem||0).toFixed(2)}%</td><td>${item.faixa_aplicada||'-'}</td>`;
    tbody.appendChild(tr);
  });
  document.getElementById('audit').innerText = JSON.stringify(data, null, 2);
  document.getElementById('msg').innerText = 'Preview gerado com sucesso.';
}
</script>
</body>
</html>"""


# ============================================================
# Routes base
# ============================================================
@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    html_file = _first_existing([
        BASE_DIR / "index.html",
        PAGES_DIR / "simulador.html",
        BASE_DIR / "simulador.html",
    ])
    if html_file:
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    return HTMLResponse(FALLBACK_HTML)


@app.get("/simulador", response_class=HTMLResponse)
def simulador_page() -> HTMLResponse:
    html_file = _first_existing([
        PAGES_DIR / "simulador.html",
        BASE_DIR / "simulador.html",
        BASE_DIR / "index.html",
    ])
    if html_file:
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    return HTMLResponse(FALLBACK_HTML)


@app.get("/health")
def health() -> dict:
    return {
        "status": "Shinsei Pricing rodando",
        "engine": pricing_module.__name__,
        "routers": ROUTERS_STATUS,
        "bling_client": bool(BlingClient),
        "bling_update_engine": bool(aplicar_precos_multicanal),
    }


# ============================================================
# Regras / config
# ============================================================
@app.get("/api/regras")
def api_get_regras() -> list[dict]:
    return carregar_regras()


@app.post("/api/regras")
def api_save_regras(regras: list[dict]) -> dict:
    salvar_regras(regras)
    return {"ok": True, "total": len(regras)}


@app.get("/api/config")
def api_get_config() -> dict:
    return carregar_cfg()


@app.post("/api/config")
def api_save_config(cfg: dict = Body(...)) -> dict:
    salvar_cfg(cfg)
    return {"ok": True, "config": carregar_cfg()}


@app.post("/upload-excel")
async def upload_excel(file: UploadFile = File(...)) -> dict:
    if not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Envie um arquivo .xlsx")

    temp_path = DATA_DIR / f"upload_{uuid.uuid4().hex}.xlsx"
    try:
        temp_path.write_bytes(await file.read())
        wb = openpyxl.load_workbook(temp_path)
        ws = wb[wb.sheetnames[1] if len(wb.sheetnames) > 1 else wb.sheetnames[0]]
        headers = [str(c.value).strip() if c.value is not None else "" for c in ws[1]]

        mapa = {
            "canal": ["canal"],
            "peso_min": ["peso min", "peso_min", "peso mínimo"],
            "peso_max": ["peso max", "peso_max", "peso máximo"],
            "preco_min": ["preço min", "preco min", "preco_min"],
            "preco_max": ["preço max", "preco max", "preco_max"],
            "taxa_frete": ["frete", "taxa frete", "taxa_frete"],
            "comissao": ["comissão", "comissao"],
            "taxa_fixa": ["taxa fixa", "taxa_fixa"],
        }

        idx: dict[str, int] = {}
        for i, h in enumerate(headers):
            h_norm = h.strip().lower()
            for key, aliases in mapa.items():
                if h_norm in aliases and key not in idx:
                    idx[key] = i

        regras = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            canal = str(row[idx.get("canal", -1)] or "").strip() if idx.get("canal") is not None else ""
            if not canal:
                continue
            regras.append(
                {
                    "canal": canal,
                    "peso_min": _safe_float(row[idx.get("peso_min", -1)] if idx.get("peso_min") is not None else 0),
                    "peso_max": _safe_float(row[idx.get("peso_max", -1)] if idx.get("peso_max") is not None else 0),
                    "preco_min": _safe_float(row[idx.get("preco_min", -1)] if idx.get("preco_min") is not None else 0),
                    "preco_max": _safe_float(row[idx.get("preco_max", -1)] if idx.get("preco_max") is not None else 0),
                    "taxa_frete": _safe_float(row[idx.get("taxa_frete", -1)] if idx.get("taxa_frete") is not None else 0),
                    "comissao": _safe_float(row[idx.get("comissao", -1)] if idx.get("comissao") is not None else 0),
                    "taxa_fixa": _safe_float(row[idx.get("taxa_fixa", -1)] if idx.get("taxa_fixa") is not None else 0),
                    "ativo": True,
                }
            )

        salvar_regras(regras)
        return {"ok": True, "message": "Regras importadas com sucesso.", "total": len(regras)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erro ao importar Excel: {exc}")
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


# ============================================================
# Simulation
# ============================================================
@app.post("/simular")
def simular(payload: SimulacaoPayload) -> dict:
    regras = carregar_regras(apenas_ativas=True)
    if not regras:
        raise HTTPException(status_code=400, detail="Nenhuma regra cadastrada. Importe a Aba2 primeiro.")

    cfg = carregar_cfg()
    score_config = payload.score_config or {
        "peso_forca": cfg.get("peso_forca", 0.40),
        "peso_equilibrio": cfg.get("peso_equilibrio", 0.40),
        "peso_lucro": cfg.get("peso_lucro", 0.20),
        "forcas_canais": cfg.get("forcas_canais", {}),
    }

    try:
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
            score_config=score_config,
        )
    except TypeError:
        modo = payload.objetivo if payload.objetivo in {"markup", "margem"} else "markup"
        resultado = calcular_canais(
            regras,
            payload.preco_compra,
            payload.embalagem,
            payload.peso,
            payload.imposto,
            payload.quantidade,
            modo,
            payload.valor_alvo,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erro no engine: {exc}")

    return {
        "ok": True,
        "engine": pricing_module.__name__,
        **resultado,
        "marketplaces": _normalizar_marketplaces(resultado.get("canais", [])),
    }


# ============================================================
# Bling auth / status / search
# ============================================================
@app.get("/bling/status")
def bling_status() -> dict:
    if not BlingClient:
        return {"ok": False, "erro": "bling_client.py não encontrado."}
    try:
        client = BlingClient()
        return {
            "ok": True,
            "configurado": bool(
                getattr(client, "client_id", "")
                and getattr(client, "client_secret", "")
                and getattr(client, "redirect_uri", "")
            ),
            "token_local": bool(client.has_local_tokens()),
        }
    except Exception as exc:
        return {"ok": False, "erro": str(exc)}


@app.get("/bling/auth")
def bling_auth():
    if not BlingClient:
        raise HTTPException(status_code=500, detail="bling_client.py não encontrado.")
    try:
        client = BlingClient()
        return RedirectResponse(client.build_authorize_url())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/bling/callback")
def bling_callback(
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    error_description: str | None = Query(None),
):
    if not BlingClient:
        raise HTTPException(status_code=500, detail="bling_client.py não encontrado.")

    if error:
        raise HTTPException(
            status_code=400,
            detail=f"Bling OAuth retornou erro: {error}. {error_description or ''}".strip(),
        )

    if not code:
        raise HTTPException(status_code=400, detail="Callback do Bling sem code de autorização.")

    try:
        client = BlingClient()
        token = client.exchange_code_for_token(code, state=state)
        return {
            "ok": True,
            "message": "Conexão com Bling realizada.",
            "expires_in": token.get("expires_in"),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _buscar_produto_bling_local(client: Any, criterio: str, valor: str) -> dict:
    criterio = (criterio or "").strip().lower()
    valor = (valor or "").strip()

    if not valor:
        raise ValueError("Informe um valor para busca no Bling.")

    # prioridade de uso:
    # 1) id
    # 2) sku
    # 3) ean
    # 4) nome
    if criterio == "id" and hasattr(client, "get_product"):
        produto = client.get_product(int(valor))
        return {"encontrado": True, "produto": produto, "quantidade": 1, "criterio": criterio, "valor": valor}

    if criterio == "sku" and hasattr(client, "get_product_by_sku"):
        return client.get_product_by_sku(valor)

    if criterio == "ean" and hasattr(client, "get_product_by_ean"):
        return client.get_product_by_ean(valor)

    if criterio == "nome" and hasattr(client, "get_product_by_name"):
        return client.get_product_by_name(valor)

    if not hasattr(client, "list_products"):
        raise RuntimeError("O cliente do Bling não possui métodos de busca compatíveis.")

    payload = client.list_products(page=1, limit=100)
    data = payload.get("data", [])
    encontrados = []
    termo = valor.lower()

    for item in data:
        prod = item.get("produto", item) if isinstance(item, dict) else {}
        nome = str(prod.get("nome") or "").lower()
        codigo = str(prod.get("codigo") or "").lower()
        gtin = str(prod.get("gtin") or prod.get("ean") or "").lower()
        pid = str(prod.get("id") or "").lower()

        ok = False
        if criterio == "nome":
            ok = termo in nome
        elif criterio == "sku":
            ok = termo == codigo
        elif criterio == "ean":
            ok = termo == gtin
        elif criterio == "id":
            ok = termo == pid

        if ok:
            encontrados.append(prod)

    if not encontrados:
        return {"encontrado": False, "criterio": criterio, "valor": valor, "quantidade": 0}

    return {
        "encontrado": True,
        "criterio": criterio,
        "valor": valor,
        "quantidade": len(encontrados),
        "produto": encontrados[0],
        "produtos": encontrados[:10],
    }


@app.get("/bling/produto/{valor}")
def bling_produto_direto(valor: str, criterio: str = Query("sku")) -> dict:
    if not BlingClient:
        raise HTTPException(status_code=500, detail="bling_client.py não encontrado.")
    try:
        client = BlingClient()
        return _buscar_produto_bling_local(client, criterio, valor)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/bling/produto/buscar")
def bling_produto_buscar(payload: BuscaProdutoPayload) -> dict:
    if not BlingClient:
        raise HTTPException(status_code=500, detail="bling_client.py não encontrado.")
    try:
        client = BlingClient()
        return _buscar_produto_bling_local(client, payload.criterio, payload.valor)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ============================================================
# Integration preview / queue / apply
# ============================================================
@app.post("/integracao/preview")
def integracao_preview(payload: IntegracaoPayload) -> dict:
    regras = carregar_regras(apenas_ativas=True)
    if not regras:
        raise HTTPException(status_code=400, detail="Nenhuma regra cadastrada. Importe a Aba2 primeiro.")

    cfg = carregar_cfg()
    score_config = payload.score_config or {
        "peso_forca": cfg.get("peso_forca", 0.40),
        "peso_equilibrio": cfg.get("peso_equilibrio", 0.40),
        "peso_lucro": cfg.get("peso_lucro", 0.20),
        "forcas_canais": cfg.get("forcas_canais", {}),
    }

    if montar_precificacao_bling and payload.criterio in {"ean", "sku", "id"}:
        try:
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
                score_config=score_config,
                modo_aprovacao=payload.modo_aprovacao,
                preco_compra_anterior_bling=payload.preco_compra_anterior_bling,
                modo_preco_virtual=payload.modo_preco_virtual,
                acrescimo_percentual=payload.acrescimo_percentual,
                acrescimo_nominal=payload.acrescimo_nominal,
                preco_manual=payload.preco_manual,
                arredondamento=payload.arredondamento,
            )
            itens = (resultado.get("integracao") or {}).get("itens") or resultado.get("itens") or []
            melhor = resultado.get("melhor_resultado") or (itens[0] if itens else {})
            marketplaces = _normalizar_marketplaces(itens or resultado.get("canais", []))
            return {
                "ok": True,
                "produto": (resultado.get("produto_bling") or {}),
                "melhor_canal": resultado.get("melhor_canal") or melhor.get("canal") or "",
                "modo_aprovacao": payload.modo_aprovacao,
                "marketplaces": marketplaces,
                "auditoria": resultado.get("auditoria") or resultado,
                "raw": resultado,
            }
        except Exception:
            pass

    produto = None
    preco_compra = 0.0
    peso = float(payload.peso_override or 0)
    estoque = 0

    if payload.valor_busca:
        if not BlingClient:
            raise HTTPException(status_code=500, detail="bling_client.py não encontrado.")
        try:
            client = BlingClient()
            busca = _buscar_produto_bling_local(client, payload.criterio, payload.valor_busca)
            if not busca.get("encontrado"):
                raise HTTPException(status_code=404, detail="Produto não encontrado no Bling.")
            produto = busca.get("produto") or {}
            preco_compra = _safe_float(produto.get("precoCusto") or produto.get("preco_custo") or produto.get("custo") or 0)
            estoque = _safe_int(
                ((produto.get("estoque") or {}).get("saldoVirtualTotal") if isinstance(produto.get("estoque"), dict) else produto.get("saldoVirtualTotal")) or 0
            )
            if peso <= 0:
                peso = _safe_float(produto.get("pesoLiquido") or produto.get("pesoBruto") or produto.get("peso") or 0)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Falha na busca do Bling: {exc}")
    else:
        raise HTTPException(status_code=400, detail="Informe nome, sku, ean ou id para gerar a integração.")

    if preco_compra <= 0:
        raise HTTPException(status_code=400, detail="Produto sem custo válido no Bling.")
    if peso <= 0:
        raise HTTPException(status_code=400, detail="Produto sem peso válido no Bling. Informe peso override.")

    try:
        calculo = calcular_canais(
            regras=regras,
            preco_compra=preco_compra,
            embalagem=payload.embalagem,
            peso=peso,
            imposto=payload.imposto,
            quantidade=payload.quantidade,
            objetivo=payload.objetivo,
            tipo_alvo=payload.tipo_alvo,
            valor_alvo=payload.valor_alvo,
            score_config=score_config,
        )
    except TypeError:
        modo = payload.objetivo if payload.objetivo in {"markup", "margem"} else "markup"
        calculo = calcular_canais(
            regras,
            preco_compra,
            payload.embalagem,
            peso,
            payload.imposto,
            payload.quantidade,
            modo,
            payload.valor_alvo,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erro no engine: {exc}")

    canais = calculo.get("canais", [])
    itens = []
    if gerar_integracao:
        try:
            integracao = gerar_integracao(
                canais=canais,
                modo_preco_virtual=payload.modo_preco_virtual,
                acrescimo_percentual=payload.acrescimo_percentual,
                acrescimo_nominal=payload.acrescimo_nominal,
                preco_manual=payload.preco_manual,
                arredondamento=payload.arredondamento,
                modo_aprovacao=payload.modo_aprovacao,
                preco_compra_bling=preco_compra,
                preco_compra_anterior_bling=payload.preco_compra_anterior_bling,
                estoque=estoque,
                regra_estoque=cfg.get("regra_estoque", {}),
            )
            itens = integracao.get("itens", [])
        except TypeError:
            integracao = gerar_integracao(
                canais,
                payload.modo_preco_virtual,
                payload.acrescimo_percentual,
                payload.acrescimo_nominal,
                payload.preco_manual,
                payload.arredondamento,
                modo_aprovacao=payload.modo_aprovacao,
                preco_compra_bling=preco_compra,
                preco_compra_anterior_bling=payload.preco_compra_anterior_bling,
            )
            itens = integracao.get("itens", [])
        except Exception:
            itens = []

    if not itens:
        itens = canais

    preview = {
        "ok": True,
        "produto": {
            "id": produto.get("id"),
            "nome": produto.get("nome"),
            "codigo": produto.get("codigo"),
            "preco": produto.get("preco"),
            "precoCusto": preco_compra,
            "estoque": estoque,
            "peso": peso,
        },
        "melhor_canal": calculo.get("melhor_canal", ""),
        "modo_aprovacao": payload.modo_aprovacao,
        "marketplaces": _normalizar_marketplaces(itens),
        "canais": canais,
        "itens": itens,
        "raw": {"calculo": calculo},
    }

    _append_jsonl(
        LOG_PATH,
        {
            "evento": "preview_integracao",
            "sku": produto.get("codigo"),
            "criterio": payload.criterio,
            "valor_busca": payload.valor_busca,
            "melhor_canal": preview["melhor_canal"],
        },
    )
    return preview


@app.get("/fila")
def fila_listar() -> list[dict]:
    return carregar_fila()


@app.post("/fila/adicionar")
def fila_adicionar(payload: dict = Body(...)) -> dict:
    itens = carregar_fila()
    item = {
        "id": str(uuid.uuid4()),
        **payload,
    }
    itens.insert(0, item)
    salvar_fila(itens)
    return {"ok": True, "item": item, "total": len(itens)}


@app.post("/fila/rejeitar/{item_id}")
def fila_rejeitar(item_id: str) -> dict:
    itens = carregar_fila()
    novo = [i for i in itens if i.get("id") != item_id]
    salvar_fila(novo)
    return {"ok": True, "message": "Item removido da fila.", "total": len(novo)}


@app.post("/fila/aprovar/{item_id}")
def fila_aprovar(item_id: str) -> dict:
    itens = carregar_fila()
    item = next((i for i in itens if i.get("id") == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado na fila.")

    if not BlingClient or not aplicar_precos_multicanal:
        raise HTTPException(
            status_code=500,
            detail="Integração de aplicação no Bling indisponível. Verifique bling_client.py e bling_update_engine.py.",
        )

    try:
        client = BlingClient()
        resultado = aplicar_precos_multicanal(client, item)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Falha ao aplicar preços no Bling: {exc}")

    novo = [i for i in itens if i.get("id") != item_id]
    salvar_fila(novo)
    _append_jsonl(LOG_PATH, {"evento": "aplicacao_bling", "item_id": item_id, "resultado": resultado})
    return {"ok": True, "message": "Preços aplicados no Bling.", "resultado": resultado}


@app.post("/integracao/aplicar")
def integracao_aplicar(payload: dict = Body(...)) -> dict:
    if not BlingClient or not aplicar_precos_multicanal:
        raise HTTPException(
            status_code=500,
            detail="Integração de aplicação no Bling indisponível. Verifique bling_client.py e bling_update_engine.py.",
        )
    try:
        client = BlingClient()
        resultado = aplicar_precos_multicanal(client, payload)
        _append_jsonl(LOG_PATH, {"evento": "aplicacao_direta", "payload": payload})
        return {"ok": True, "resultado": resultado}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Falha ao aplicar preços: {exc}")


# ============================================================
# Startup defaults
# ============================================================
if not REGRAS_PATH.exists():
    _save_json(REGRAS_PATH, [])
if not FILA_PATH.exists():
    _save_json(FILA_PATH, [])
if not CFG_PATH.exists():
    _save_json(CFG_PATH, DEFAULT_CFG)

from fastapi import Body

@app.post("/bling/aplicar-preco")
def aplicar_preco(payload: dict = Body(...)):
    try:
        from pricing_engine import montar_precificacao_bling
        from bling_client import BlingClient

        client = BlingClient()

        resultado = montar_precificacao_bling(
            regras=payload.get("regras"),
            criterio=payload.get("criterio"),
            valor_busca=payload.get("valor"),
            embalagem=payload.get("embalagem"),
            imposto=payload.get("imposto"),
            quantidade=payload.get("quantidade"),
            objetivo=payload.get("objetivo"),
            tipo_alvo=payload.get("tipo_alvo"),
            valor_alvo=payload.get("valor_alvo"),
        )

        produto = resultado["produto"]
        melhor = resultado["melhor"]

        novo_preco = melhor["preco_final"]

        response = client.update_product(
            produto["id"],
            {
                "preco": novo_preco
            }
        )

        return {
            "ok": True,
            "produto": produto["nome"],
            "preco_aplicado": novo_preco,
            "canal": melhor["canal"],
            "resposta_bling": response
        }

    except Exception as e:
        return {"ok": False, "erro": str(e)}

@app.post("/webhooks/bling")
async def webhook_bling(payload: dict = Body(...)):
    try:
        _append_jsonl(LOG_PATH, {"evento": "webhook_bling_recebido", "payload": payload})

        fila = carregar_fila()
        item = {
            "id": str(uuid.uuid4()),
            "origem": "webhook_bling",
            "status": "pendente",
            "payload": payload,
        }
        fila.insert(0, item)
        salvar_fila(fila)

        return {"ok": True, "message": "Webhook recebido com sucesso."}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))