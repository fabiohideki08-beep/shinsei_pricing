from pathlib import Path
from urllib.parse import quote
import json

import requests
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from bling_client import BlingClient

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
PAGES_DIR = BASE_DIR / "pages"

TOKEN_FILE = DATA_DIR / "bling_tokens.json"


# =========================
# Utilidades
# =========================
def read_tokens() -> dict:
    if not TOKEN_FILE.exists():
        raise HTTPException(status_code=400, detail="Bling não conectado")

    raw = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Token do Bling vazio")

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # compatibilidade com tokens antigos gravados como str(dict)
        import ast
        try:
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="Arquivo de token do Bling inválido")


# =========================
# Página HTML
# =========================
@router.get("/bling", response_class=HTMLResponse)
def bling_page():
    return "<h1>Bling Integração OK</h1><p>Clique em conectar para autenticar.</p>"


# =========================
# Login OAuth
# =========================
@router.get("/bling/login")
def bling_login():
    client = BlingClient()
    url = client.get_authorization_url()
    return RedirectResponse(url)


# =========================
# Callback OAuth
# =========================
@router.get("/bling/callback")
def bling_callback(code: str = None):

    if not code:
        raise HTTPException(status_code=400, detail="Code não recebido")

    client = BlingClient()

    tokens = client.get_access_token(code)

    if not tokens:
        raise HTTPException(status_code=400, detail="Erro ao gerar token")

    TOKEN_FILE.write_text(json.dumps(tokens, ensure_ascii=False), encoding="utf-8")

    return {
        "success": True,
        "message": "Bling conectado com sucesso"
    }


# =========================
# Status
# =========================
@router.get("/bling/status")
def bling_status():
    if not TOKEN_FILE.exists():
        return {"conectado": False}

    return {"conectado": True}


# =========================
# Teste simples
# =========================
@router.get("/bling/test")
def bling_test():
    if not TOKEN_FILE.exists():
        raise HTTPException(status_code=400, detail="Não conectado")

    return {"ok": True}


# =========================
# Busca de produto por nome, SKU ou EAN
# =========================
@router.get("/bling/produto")
def buscar_produto(q: str = Query(None)):
    if not q or not q.strip():
        return {"erro": "Informe nome, SKU ou EAN"}

    tokens = read_tokens()
    access_token = tokens.get("access_token")

    if not access_token:
        raise HTTPException(status_code=500, detail="Access token do Bling não encontrado")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    termo = q.strip()
    termo_encoded = quote(termo)

    urls = [
        f"https://api.bling.com.br/Api/v3/produtos?descricao={termo_encoded}",
        f"https://api.bling.com.br/Api/v3/produtos?codigo={termo_encoded}",
        f"https://api.bling.com.br/Api/v3/produtos?gtin={termo_encoded}",
    ]

    resultados = []
    ids_vistos = set()

    def extrair_lista(payload):
        data = payload.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
        return []

    for url in urls:
        try:
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                continue

            payload = response.json()
            for produto in extrair_lista(payload):
                pid = str(produto.get("id", "")).strip()
                if not pid or pid in ids_vistos:
                    continue

                ids_vistos.add(pid)
                resultados.append({
                    "id": produto.get("id"),
                    "nome": produto.get("nome") or produto.get("descricao") or "",
                    "sku": produto.get("codigo") or "",
                    "ean": produto.get("gtin") or "",
                    "preco_custo": produto.get("precoCusto", 0) or 0,
                    "peso": produto.get("pesoLiquido", 0) or 0,
                })
        except Exception:
            continue

    if resultados:
        return resultados[:15]

    return {"erro": "Nenhum produto encontrado"}
