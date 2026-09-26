from fastapi import APIRouter
from fastapi.responses import RedirectResponse, HTMLResponse, Response
import functools, time

router = APIRouter()

# Cache simples: sku+idx → (bytes, content_type, timestamp)
_img_cache: dict[str, tuple[bytes, str, float]] = {}
_IMG_TTL = 3600 * 6  # 6 horas


@router.get("/bling/produto/{sku}/imagem/{idx}")
def bling_produto_imagem(sku: str, idx: int = 0):
    """
    Serve a imagem de um produto Shinsei como URL pública permanente.
    Usado para referenciar imagens no Bling AKG via imagensURL.
    Público — não requer API key.
    """
    import requests as _req

    cache_key = f"{sku}:{idx}"
    cached = _img_cache.get(cache_key)
    if cached and time.time() - cached[2] < _IMG_TTL:
        return Response(content=cached[0], media_type=cached[1])

    # Busca token Shinsei
    try:
        from bling_client import BlingClient
        client = BlingClient()
        token = client.tokens.get("access_token", "") if hasattr(client, "tokens") else ""
    except Exception:
        token = ""

    if not token:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Token Shinsei indisponível.")

    hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    BASE = "https://api.bling.com.br/Api/v3"

    # Busca produto por SKU
    r = _req.get(f"{BASE}/produtos", params={"codigo": sku, "limite": 1}, headers=hdrs, timeout=15)
    if r.status_code != 200 or not r.json().get("data"):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Produto {sku} não encontrado.")

    prod_id = r.json()["data"][0]["id"]

    # Busca detalhes com imagens
    r2 = _req.get(f"{BASE}/produtos/{prod_id}", headers=hdrs, timeout=15)
    if r2.status_code != 200:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Erro ao buscar produto.")

    det = r2.json().get("data", {})
    internas = det.get("midia", {}).get("imagens", {}).get("internas", [])

    if idx >= len(internas):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Imagem {idx} não existe (total: {len(internas)}).")

    img_url = internas[idx]["link"]
    r3 = _req.get(img_url, timeout=30)
    if r3.status_code != 200:
        from fastapi import HTTPException
        raise HTTPException(status_code=502, detail="Erro ao baixar imagem do Bling.")

    content_type = r3.headers.get("Content-Type", "image/jpeg")
    _img_cache[cache_key] = (r3.content, content_type, time.time())

    return Response(
        content=r3.content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=21600"},
    )


@router.get("/bling/api")
def bling_api_proxy(endpoint: str, pagina: int = 1, limite: int = 100):
    """Proxy genérico GET para qualquer endpoint da API Bling v3."""
    import requests as _req
    from routes.amazon import _bling_token
    token = _bling_token()
    if not token:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Token Bling indisponível.")
    ep = endpoint if endpoint.startswith('/') else f'/{endpoint}'
    r = _req.get(
        f"https://www.bling.com.br/Api/v3{ep}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params={"pagina": pagina, "limite": limite},
        timeout=30
    )
    return r.json()


@router.post("/bling/api")
def bling_api_proxy_post(endpoint: str, body: dict = None):
    """Proxy genérico POST para qualquer endpoint da API Bling v3."""
    import requests as _req
    from routes.amazon import _bling_token
    from fastapi import HTTPException
    token = _bling_token()
    if not token:
        raise HTTPException(status_code=503, detail="Token Bling indisponível.")
    ep = endpoint if endpoint.startswith('/') else f'/{endpoint}'
    r = _req.post(
        f"https://www.bling.com.br/Api/v3{ep}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"},
        json=body or {},
        timeout=60
    )
    try:
        return r.json()
    except Exception:
        return {"status_code": r.status_code, "text": r.text[:500]}


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


@router.get("/bling/vendas/analise")
def bling_vendas_analise(paginas: int = 50, data_inicio: str = "", data_fim: str = ""):
    """
    Consolida pedidos de vendas do Bling para análise estratégica.
    Retorna: total de pedidos, GMV, ticket médio, top SKUs, top canais, vendas por mês.
    paginas: quantas páginas buscar (100 pedidos/página). Default=50 → até 5.000 pedidos.
    """
    import time as _t
    from collections import defaultdict
    from bling_client import BlingClient

    bc = BlingClient()
    todos_pedidos = []
    params_base: dict = {"limite": 100}
    if data_inicio:
        params_base["dataInicio"] = data_inicio
    if data_fim:
        params_base["dataFim"] = data_fim

    for pag in range(1, paginas + 1):
        try:
            r = bc._get("pedidos/vendas", params={**params_base, "pagina": pag})
            itens = r.get("data") or []
            if not itens:
                break
            todos_pedidos.extend(itens)
            if len(itens) < 100:
                break
            _t.sleep(0.3)
        except Exception as e:
            break

    # Consolidar
    gmv = 0.0
    por_mes: dict = defaultdict(float)
    por_canal: dict = defaultdict(int)
    por_sku: dict = defaultdict(lambda: {"qtd": 0, "valor": 0.0, "nome": ""})
    situacoes: dict = defaultdict(int)

    for p in todos_pedidos:
        valor = float(p.get("totalProdutos") or p.get("total") or 0)
        gmv += valor
        data_str = (p.get("data") or "")[:7]  # YYYY-MM
        por_mes[data_str] += valor
        canal = (p.get("canal") or {}).get("descricao") or (p.get("loja") or {}).get("descricao") or "Desconhecido"
        por_canal[canal] += 1
        sit = (p.get("situacao") or {}).get("valor") or p.get("situacao") or ""
        situacoes[str(sit)] += 1
        for item in (p.get("itens") or []):
            sku = (item.get("produto") or {}).get("codigo") or item.get("codigo") or ""
            nome = (item.get("produto") or {}).get("descricao") or item.get("descricao") or ""
            qtd = int(float(item.get("quantidade") or 1))
            val_item = float(item.get("valor") or 0) * qtd
            if sku:
                por_sku[sku]["qtd"] += qtd
                por_sku[sku]["valor"] += val_item
                if nome:
                    por_sku[sku]["nome"] = nome

    top_skus = sorted(por_sku.items(), key=lambda x: x[1]["valor"], reverse=True)[:30]
    top_canais = sorted(por_canal.items(), key=lambda x: x[1], reverse=True)
    meses_ord = sorted(por_mes.items())

    return {
        "total_pedidos": len(todos_pedidos),
        "gmv_total": round(gmv, 2),
        "ticket_medio": round(gmv / len(todos_pedidos), 2) if todos_pedidos else 0,
        "paginas_buscadas": min(paginas, (len(todos_pedidos) // 100) + 1),
        "por_mes": meses_ord,
        "por_canal": top_canais,
        "situacoes": dict(situacoes),
        "top_skus": [
            {"sku": k, "nome": v["nome"][:60], "qtd_vendida": v["qtd"], "valor_total": round(v["valor"], 2)}
            for k, v in top_skus
        ],
        "amostra_pedido": todos_pedidos[0] if todos_pedidos else {},
    }


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
