from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI()

class Payload(BaseModel):
    codigo: str
    peso_override: float | None = None

@app.get("/")
def home():
    return FileResponse("index_final.html")

@app.post("/bling/pre-visualizar-produto")
def preview(payload: Payload):
    if not payload.peso_override:
        return JSONResponse({"erro": "Produto sem peso"})

    return {
        "produto": {
            "codigo": payload.codigo,
            "nome": "Produto Exemplo",
            "preco_atual": 50
        },
        "canais": [
            {"canal": "Amazon", "preco_sugerido": 55},
            {"canal": "Mercado Livre", "preco_sugerido": 53}
        ]
    }

@app.post("/bling/precificar")
def precificar(payload: Payload):
    return {"status": "ok", "mensagem": "Preço aplicado"}
from fastapi import Request

@app.post("/webhook/bling")
async def webhook_bling(request: Request):
    try:
        payload = await request.json()
    except:
        payload = {}

    print("📩 Webhook recebido do Bling:")
    print(payload)

    return {"status": "ok"}
