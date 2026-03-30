from fastapi import FastAPI, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pathlib import Path
import importlib

app = FastAPI()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).parent
PAGES_DIR = BASE_DIR / "pages"

PAGES_DIR.mkdir(exist_ok=True)


def include_optional_router(module_name: str) -> bool:
    try:
        module = importlib.import_module(f"routes.{module_name}")
        router = getattr(module, "router", None)
        if router:
            app.include_router(router)
            return True
        return False
    except Exception:
        return False


ROUTERS_STATUS = {
    "mercado_livre": include_optional_router("mercado_livre"),
    "bling": include_optional_router("bling"),
}


@app.get("/")
def home():
    return {
        "status": "Shinsei Pricing rodando",
        "routers": ROUTERS_STATUS,
    }


@app.get("/simulador", response_class=HTMLResponse)
def simulador():
    file = PAGES_DIR / "simulador.html"
    if not file.exists():
        return HTMLResponse("<h1>simulador.html não encontrado</h1>", status_code=404)
    return file.read_text(encoding="utf-8")


# 🔥 ROTA DE CÁLCULO (FUNCIONANDO)
@app.post("/simular")
async def simular(data: dict = Body(...)):
    try:
        nome = data.get("nome", "Produto")
        custo = float(data.get("custo", 50))
        margem = float(data.get("margem", 20))

        preco = round(custo * (1 + margem / 100), 2)
        lucro = round(preco - custo, 2)

        marketplaces = {
            "mercado_livre_classico": {
                "preco": preco,
                "preco_promocional": 0,
                "lucro": lucro,
                "margem": margem,
                "comissao": 0,
                "frete": 0,
                "taxa_fixa": 0,
                "imposto": 0,
                "custo_total": custo,
            },
            "amazon": {
                "preco": round(preco * 1.02, 2),
                "preco_promocional": 0,
                "lucro": round((preco * 1.02) - custo, 2),
                "margem": margem,
                "comissao": 0,
                "frete": 0,
                "taxa_fixa": 0,
                "imposto": 0,
                "custo_total": custo,
            },
        }

        return {
            "success": True,
            "produto": nome,
            "tipo": "simulado",
            "custo": custo,
            "preco_base": preco,
            "marketplaces": marketplaces,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}