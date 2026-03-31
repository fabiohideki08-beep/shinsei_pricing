from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path
import json
import uuid
from datetime import datetime
from typing import Optional, Any

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
PAGES_DIR = BASE_DIR / "pages"
DATA_DIR = BASE_DIR / "data"

FILA_PATH = DATA_DIR / "fila_aprovacao.json"

# =========================
# UTIL
# =========================

def carregar_fila():
    if not FILA_PATH.exists():
        return []
    with open(FILA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def salvar_fila(fila):
    with open(FILA_PATH, "w", encoding="utf-8") as f:
        json.dump(fila, f, indent=2, ensure_ascii=False)

# =========================
# EXTRATOR INTELIGENTE
# =========================

def _extrair_identificador_webhook(payload: dict) -> Optional[dict]:
    candidatos = []

    def add(criterio: str, valor: Any):
        if valor is None:
            return
        txt = str(valor).strip()
        if txt:
            candidatos.append({"criterio": criterio, "valor": txt})

    def scan(bloco: dict):
        if not isinstance(bloco, dict):
            return

        add("sku", bloco.get("codigo"))
        add("sku", bloco.get("sku"))
        add("id", bloco.get("id"))
        add("nome", bloco.get("nome"))
        add("ean", bloco.get("gtin"))
        add("ean", bloco.get("ean"))

        produto = bloco.get("produto")
        if isinstance(produto, dict):
            add("id", produto.get("id"))
            add("sku", produto.get("codigo"))
            add("sku", produto.get("sku"))
            add("nome", produto.get("nome"))

    scan(payload)

    for key in ["data", "produto", "item"]:
        bloco = payload.get(key)
        if isinstance(bloco, dict):
            scan(bloco)

    if not candidatos:
        return None

    prioridade = {"sku": 0, "id": 1, "nome": 2, "ean": 3}
    candidatos.sort(key=lambda x: prioridade.get(x["criterio"], 999))

    return candidatos[0]

# =========================
# ROTAS
# =========================

@app.get("/fila", response_class=HTMLResponse)
def pagina_fila():
    file = PAGES_DIR / "fila.html"
    if not file.exists():
        raise HTTPException(status_code=404, detail="pages/fila.html não encontrado.")
    return file.read_text(encoding="utf-8")

@app.get("/fila/lista")
def lista_fila():
    return carregar_fila()

@app.post("/fila/aprovar/{item_id}")
def aprovar_item(item_id: str):
    fila = carregar_fila()

    for item in fila:
        if item["id"] == item_id:
            item["status"] = "aprovado"

    salvar_fila(fila)

    return {"ok": True, "message": "Item aprovado"}

# =========================
# WEBHOOK
# =========================

@app.post("/webhooks/bling")
@app.post("/webhook/bling")
async def webhook_bling(payload: dict = Body(...)):

    identificador = _extrair_identificador_webhook(payload)

    fila = carregar_fila()

    if not identificador:
        item = {
            "id": str(uuid.uuid4()),
            "status": "pendente",
            "origem": "webhook_bling",
            "tipo": "payload_bruto",
            "payload": payload,
            "motivo": "Sem identificador suficiente"
        }

        fila.insert(0, item)
        salvar_fila(fila)

        return {"ok": True, "message": "Enviado para fila (sem identificador)"}

    # 🔥 SIMULAÇÃO DE BUSCA (depois vamos ligar no Bling real)
    criterio = identificador["criterio"]
    valor = identificador["valor"]

    item = {
        "id": str(uuid.uuid4()),
        "status": "pendente",
        "origem": "webhook_bling",
        "criterio": criterio,
        "valor_busca": valor,
        "nome": f"Produto via {criterio}",
        "sku": valor if criterio == "sku" else "-",
        "produto_id": valor if criterio == "id" else "-",
        "preco_sugerido": 9.9
    }

    fila.insert(0, item)
    salvar_fila(fila)

    return {
        "ok": True,
        "message": "Webhook processado",
        "criterio": criterio,
        "valor": valor
    }