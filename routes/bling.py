from fastapi import APIRouter
from fastapi.responses import RedirectResponse, HTMLResponse

router = APIRouter()


@router.get("/bling/produtos")
def bling_produtos_proxy(pagina: int = 1, limite: int = 100, situacao: str = "A", nome: str = ""):
    """Proxy para GET /produtos do Bling usando token interno."""
    import requests as _req
    from routes.amazon import _bling_token
    token = _bling_token()
    if not token:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Token Bling indisponível.")
    params = {"pagina": pagina, "limite": limite, "situacao": situacao}
    if nome:
        params["nome"] = nome
    r = _req.get("https://api.bling.com.br/Api/v3/produtos",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params=params, timeout=30)
    return r.json()


@router.get("/bling/produtos/{produto_id}")
def bling_produto_proxy(produto_id: int):
    """Proxy para GET /produtos/{id} do Bling usando token interno."""
    import requests as _req
    from routes.amazon import _bling_token
    token = _bling_token()
    if not token:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Token Bling indisponível.")
    r = _req.get(f"https://api.bling.com.br/Api/v3/produtos/{produto_id}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=30)
    return r.json()


@router.get("/bling/token")
def bling_token_endpoint():
    """Retorna o access token atual do Bling (uso interno/scripts)."""
    try:
        from bling_client import BlingClient
        client = BlingClient()
        token = client.tokens.get("access_token", "") if hasattr(client, "tokens") else ""
        if not token:
            from fastapi import HTTPException
            raise HTTPException(status_code=503, detail="Token Bling não disponível.")
        return {"access_token": token}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail=str(e))


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
        <p>Esta rota foi mantida apenas por compatibilidade.</p>
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


@router.post("/admin/refresh-tokens")
@router.get("/admin/refresh-tokens")
def refresh_all_tokens():
    """Renova todos os tokens de marketplace (ML, Shopee, Amazon, Bling)."""
    import time as _time
    results = {}

    # ML
    try:
        from services.mercado_livre import _renovar_token_ml
        _renovar_token_ml()
        results["ml"] = "ok"
    except Exception as e:
        results["ml"] = f"erro: {e}"

    # Shopee
    try:
        from services.shopee import ShopeeOAuthService, token_expirado
        r = ShopeeOAuthService().renovar_token()
        results["shopee"] = "ok" if r.get("success") else f"erro: {r.get('error')}"
    except Exception as e:
        results["shopee"] = f"erro: {e}"

    # Amazon (auto-refresh — forçar renovando o cache)
    try:
        from services.amazon import _carregar_tokens, _salvar_tokens
        t = _carregar_tokens()
        t["lwa_expires_at"] = 0  # invalida para forçar renovação na próxima chamada
        _salvar_tokens(t)
        from services.amazon import _obter_access_token
        _obter_access_token()
        results["amazon"] = "ok"
    except Exception as e:
        results["amazon"] = f"erro: {e}"

    # Bling
    try:
        from routes.amazon import _bling_token
        tok = _bling_token(force_refresh=True)
        results["bling"] = "ok" if tok else "erro: token vazio"
    except Exception as e:
        results["bling"] = f"erro: {e}"

    # Shopify — token permanente, apenas verifica se ainda é válido
    try:
        import requests as _req
        from pathlib import Path as _Path
        import json as _json
        _cfg_path = _Path(__file__).parent.parent / "data" / "shopify_config.json"
        if _cfg_path.exists():
            _cfg = _json.loads(_cfg_path.read_text(encoding="utf-8"))
            _tok = _cfg.get("access_token", "")
            if _tok:
                from shopify_oauth import SHOPIFY_STORE
                _r = _req.get(
                    f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/2024-01/shop.json",
                    headers={"X-Shopify-Access-Token": _tok},
                    timeout=10,
                )
                if _r.status_code == 200:
                    results["shopify"] = "ok (token permanente válido)"
                else:
                    results["shopify"] = f"erro: token inválido HTTP {_r.status_code} — reautorize em /shopify/auth"
            else:
                results["shopify"] = "erro: token não encontrado"
        else:
            results["shopify"] = "erro: shopify_config.json não existe — autentique em /shopify/auth"
    except Exception as e:
        results["shopify"] = f"erro: {e}"

    results["renovado_em"] = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
    return results
