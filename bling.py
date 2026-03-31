from fastapi import APIRouter
from fastapi.responses import RedirectResponse, HTMLResponse

router = APIRouter()


@router.get("/bling", response_class=HTMLResponse)
def bling_page():
    html = """
    <html>
    <head>
        <meta charset="utf-8">
        <title>Bling - Integração</title>
    </head>
    <body style="font-family: Arial; padding: 40px;">
        <h2>Bling - Integração</h2>
        <p>Use o botão abaixo para iniciar a autenticação OAuth correta.</p>
        <a href="/bling/auth">
            <button style="padding: 12px 18px; font-size: 16px; cursor: pointer;">
                Conectar com Bling
            </button>
        </a>
    </body>
    </html>
    """
    return html


@router.get("/bling/connect")
def bling_connect():
    return RedirectResponse(url="/bling/auth")