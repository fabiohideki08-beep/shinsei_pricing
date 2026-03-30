from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pathlib import Path
import json
import uuid

from bling_client import BlingAPIError, BlingClient
from bling_update_engine import aplicar_precos_multicanal

# 🔥 NOVO IMPORT
from routes import mercado_livre

app = FastAPI()

# 🔥 NOVO REGISTRO
app.include_router(mercado_livre.router)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
PAGES_DIR = BASE_DIR / "pages"
DATA_DIR.mkdir(exist_ok=True)
PAGES_DIR.mkdir(exist_ok=True)

FILA_FILE = DATA_DIR / "fila.json"

DEFAULT_MARKETPLACES = {
    "mercado_livre_classico": {"label": "ML Clássico", "ajuste": 0},
    "mercado_livre_premium": {"label": "ML Premium", "ajuste": 10},
    "amazon": {"label": "Amazon", "ajuste": 0},
    "shopee": {"label": "Shopee", "ajuste": -2},
    "shein": {"label": "Shein", "ajuste": -3},
    "shopify": {"label": "Shopify", "ajuste": 12},
}

def load_fila():
    if not FILA_FILE.exists():
        return {"itens": []}
    return json.loads(FILA_FILE.read_text(encoding="utf-8"))

def save_fila(data):
    FILA_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def safe_float(value, default=0.0):
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def calcular_custo_produto_bling(produto: dict):
    try:
        formato = (produto.get("formato") or "").strip().upper()

        if formato in {"C", "COM COMPOSICAO", "COM COMPOSIÇÃO"}:
            estrutura = (
                (produto.get("estrutura") or {}).get("componentes")
                or produto.get("componentes")
                or []
            )
            if not estrutura:
                return {"erro": "Produto com composição sem estrutura"}

            custo_total = 0.0
            peso_total = 0.0
            estoque_limitante = None

            for comp in estrutura:
                qtd = safe_float(comp.get("quantidade") or comp.get("qtde") or comp.get("qtd") or 0)
                custo_unit = safe_float(comp.get("precoCusto") or comp.get("preco_custo") or comp.get("custo") or 0)
                peso_unit = safe_float(comp.get("pesoBruto") or comp.get("peso_bruto") or comp.get("peso") or 0)
                custo_total_comp = comp.get("precoCustoTotal") or comp.get("preco_custo_total")
                if custo_total_comp not in (None, ""):
                    custo_total_comp = safe_float(custo_total_comp)
                else:
                    custo_total_comp = custo_unit * qtd

                if custo_total_comp <= 0:
                    nome = comp.get("nome") or comp.get("descricao") or comp.get("codigo") or "componente"
                    return {"erro": f"Componente sem custo: {nome}"}

                custo_total += custo_total_comp
                peso_total += peso_unit * qtd

                estoque_comp = safe_float(comp.get("estoque") or comp.get("saldoVirtualTotal") or comp.get("saldo_fisico") or 0)
                if qtd > 0:
                    kits_possiveis = int(estoque_comp // qtd) if estoque_comp >= 0 else 0
                    estoque_limitante = kits_possiveis if estoque_limitante is None else min(estoque_limitante, kits_possiveis)

            return {"custo": round(custo_total, 2), "peso": round(peso_total, 3), "estoque": int(estoque_limitante or 0), "tipo": "composicao"}

        custo = safe_float(produto.get("precoCusto") or produto.get("preco_custo") or 0)
        peso = safe_float(produto.get("pesoBruto") or produto.get("peso_bruto") or produto.get("peso") or 0)
        estoque = 0
        if isinstance(produto.get("estoque"), dict):
            est = produto.get("estoque") or {}
            estoque = safe_float(est.get("saldoVirtualTotal") or est.get("saldoFisicoTotal") or est.get("saldoVirtual") or est.get("saldoFisico") or 0)
        else:
            estoque = safe_float(produto.get("estoque") or 0)

        if custo <= 0:
            return {"erro": "Produto sem custo"}

        return {"custo": round(custo, 2), "peso": round(peso, 3), "estoque": int(estoque), "tipo": "simples"}
    except Exception as e:
        return {"erro": str(e)}


def calcular_marketplaces(preco_base, custo):
    result = {}
    for key, m in DEFAULT_MARKETPLACES.items():
        preco = round(preco_base * (1 + m["ajuste"] / 100), 2)
        lucro = round(preco - custo, 2)
        margem = round((lucro / preco * 100), 2) if preco > 0 else 0
        promo = round(preco_base, 2) if preco > preco_base else 0.0
        result[key] = {
            "label": m["label"],
            "preco": preco,
            "preco_promocional": promo,
            "lucro": lucro,
            "margem": margem,
        }
    return result

@app.get("/", response_class=HTMLResponse)
def home():
    return (BASE_DIR / "index.html").read_text(encoding="utf-8")

@app.get("/simulador", response_class=HTMLResponse)
def simulador():
    return (PAGES_DIR / "simulador.html").read_text(encoding="utf-8")

@app.get("/fila-aprovacao", response_class=HTMLResponse)
def fila_page():
    return (PAGES_DIR / "fila_aprovacao.html").read_text(encoding="utf-8")

@app.post("/simulador/calcular")
async def calcular(request: Request):
    body = await request.json()

    criterio = body.get("criterio") or "id"
    valor = body.get("valor")
    margem = safe_float(body.get("valor_alvo"), 30)

    if margem >= 100:
        return {"erro": "Margem deve ser menor que 100"}

    client = BlingClient()

    try:
        if valor:
            produto = client.search_product(criterio, str(valor))
            dados = calcular_custo_produto_bling(produto)

            if "erro" in dados:
                return {"erro": dados["erro"], "acao": "fila"}

            custo = dados["custo"]
            preco_base = round(custo / (1 - margem / 100), 2)
            marketplaces = calcular_marketplaces(preco_base, custo)

            return {
                "produto": produto.get("nome"),
                "produto_id": produto.get("id"),
                "sku": produto.get("codigo"),
                "tipo": dados["tipo"],
                "custo": custo,
                "peso": dados.get("peso", 0),
                "estoque": dados.get("estoque", 0),
                "preco_base": preco_base,
                "marketplaces": marketplaces,
            }

        custo = safe_float(body.get("preco_compra"), 0)
        if custo <= 0:
            return {"erro": "Produto sem custo", "acao": "fila"}

        preco_base = round(custo / (1 - margem / 100), 2)
        marketplaces = calcular_marketplaces(preco_base, custo)
        return {"preco_base": preco_base, "custo": custo, "marketplaces": marketplaces, "tipo": "manual"}

    except Exception as e:
        return {"erro": str(e), "acao": "fila"}

@app.get("/fila")
def get_fila():
    return load_fila()

@app.post("/fila/adicionar")
async def add_fila(request: Request):
    body = await request.json()
    data = load_fila()
    item = {
        "id": str(uuid.uuid4()),
        "nome": body.get("nome"),
        "produto_id": body.get("produto_id"),
        "sku": body.get("sku"),
        "preco_base": body.get("preco_base"),
        "marketplaces": body.get("marketplaces"),
        "tipo": body.get("tipo"),
        "peso": body.get("peso"),
        "estoque": body.get("estoque"),
    }
    data["itens"].append(item)
    save_fila(data)
    return {"status": "ok", "item": item}

@app.post("/fila/aprovar/{item_id}")
def aprovar(item_id: str):
    data = load_fila()
    item = next((i for i in data["itens"] if i["id"] == item_id), None)
    if not item:
        return {"erro": "Item não encontrado"}

    client = BlingClient()
    try:
        resultados = aplicar_precos_multicanal(client, item)
    except BlingAPIError as exc:
        return {"erro": str(exc)}
    except Exception as exc:
        return {"erro": f"Falha ao aplicar preços: {exc}"}

    data["itens"] = [i for i in data["itens"] if i["id"] != item_id]
    save_fila(data)
    return {"message": "Aprovado", "resultados": resultados}

@app.post("/fila/rejeitar/{item_id}")
def rejeitar(item_id: str):
    data = load_fila()
    data["itens"] = [i for i in data["itens"] if i["id"] != item_id]
    save_fila(data)
    return {"message": "Rejeitado"}
