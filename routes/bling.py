from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from bling_client import BlingClient

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent

def get_client():
    return BlingClient()

@router.get("/bling", response_class=HTMLResponse)
def bling_page():
    html = """
    <html>
    <body style="font-family: Arial; padding: 40px;">
        <h2>Bling - Integração</h2>
        <button onclick="connect()">Conectar com Bling</button>
        <pre id="result"></pre>

        <script>
        async function connect() {
            const res = await fetch('/bling/connect');
            const data = await res.json();
            document.getElementById('result').innerText = JSON.stringify(data, null, 2);
        }
        </script>
    </body>
    </html>
    """
    return html

@router.get("/bling/connect")
def bling_connect():
    try:
        client = get_client()
        result = client.testar_conexao()
        return {"success": True, "message": "Conectado com Bling", "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
