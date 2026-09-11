"""
TikTok Shop — rotas FastAPI
Prefix: /tiktok

Endpoints:
  GET  /tiktok/status            — credenciais e estado
  GET  /tiktok/auth              — URL OAuth para autorizar loja
  GET  /tiktok/callback          — recebe code do OAuth e salva tokens
  GET  /tiktok/lojas             — lista lojas vinculadas
  GET  /tiktok/produtos          — lista produtos TikTok Shop
  GET  /tiktok/marcas            — lista marcas disponíveis
  POST /tiktok/marcas/aplicar    — aplica marca em lote nos produtos
  PUT  /tiktok/produto/{id}      — atualiza produto
"""
import logging
import os
import time

import requests as _req
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, List

from auth import verificar_api_key
from services.tiktok_shop import TikTokShopClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tiktok", tags=["TikTok Shop"])

RENDER_API_KEY  = os.environ.get("RENDER_API_KEY", "")
RENDER_SVC_ID   = os.environ.get("RENDER_SERVICE_ID", "")
REDIRECT_URI    = os.environ.get("TIKTOK_REDIRECT_URI", "https://shinsei-pricing.onrender.com/tiktok/callback")


def _save_env(key: str, value: str):
    """Persiste variável de ambiente no Render e localmente."""
    os.environ[key] = value
    if not RENDER_API_KEY or not RENDER_SVC_ID:
        return
    try:
        r = _req.get(
            f"https://api.render.com/v1/services/{RENDER_SVC_ID}/env-vars?limit=100",
            headers={"Authorization": f"Bearer {RENDER_API_KEY}"},
            timeout=15,
        )
        env_vars = r.json()
        existing = {item["envVar"]["key"]: item["envVar"]["value"] for item in env_vars}
        existing[key] = value
        updated = [{"key": k, "value": v} for k, v in existing.items()]
        _req.put(
            f"https://api.render.com/v1/services/{RENDER_SVC_ID}/env-vars",
            headers={"Authorization": f"Bearer {RENDER_API_KEY}", "Content-Type": "application/json"},
            json=updated,
            timeout=15,
        )
        logger.info(f"TikTok: {key} salvo no Render")
    except Exception as e:
        logger.warning(f"TikTok: falha ao salvar {key} no Render: {e}")


# ── Estado do job de aplicação de marca ──────────────────────────────────
_job_marca: dict = {
    "rodando": False, "concluido": False, "erro": None,
    "total": 0, "processados": 0, "atualizados": 0, "erros_prod": 0,
    "brand_id": None, "iniciado_em": None, "concluido_em": None,
    "erros_lista": [],
}


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.get("/status")
def tiktok_status():
    client = TikTokShopClient()
    return client.status()


@router.get("/auth")
def tiktok_auth(state: str = "shinsei"):
    client = TikTokShopClient()
    if not client.app_key:
        raise HTTPException(400, "TIKTOK_APP_KEY não configurada. Adicione nas env vars do Render.")
    url = client.get_auth_url(REDIRECT_URI, state=state)
    return {"auth_url": url, "instrucao": "Abra a URL acima no browser para autorizar a loja"}


@router.get("/callback")
def tiktok_callback(code: str = Query(...), state: str = ""):
    """OAuth callback — salva access_token, refresh_token e shop_id."""
    client = TikTokShopClient()
    try:
        resp = client.exchange_code(code)
    except Exception as e:
        raise HTTPException(400, f"Erro ao trocar código: {e}")

    data = resp.get("data", {})
    access_token  = data.get("access_token", "")
    refresh_token = data.get("refresh_token", "")
    shops         = data.get("seller_tiktok_shop_list", [])
    shop_id       = shops[0].get("cipher", "") if shops else ""

    if not access_token:
        raise HTTPException(400, f"Token não retornado: {resp}")

    _save_env("TIKTOK_ACCESS_TOKEN",  access_token)
    _save_env("TIKTOK_REFRESH_TOKEN", refresh_token)
    if shop_id:
        _save_env("TIKTOK_SHOP_ID", shop_id)

    return HTMLResponse(f"""
    <h2>✅ TikTok Shop conectado!</h2>
    <p>Access Token salvo. Lojas encontradas: {len(shops)}</p>
    <ul>{''.join(f"<li>{s.get('name','?')} — {s.get('cipher','')}</li>" for s in shops)}</ul>
    <p><a href="/tiktok/status">Ver status</a></p>
    """)


@router.get("/lojas")
def tiktok_lojas(_=Depends(verificar_api_key)):
    client = TikTokShopClient()
    try:
        return client.listar_lojas()
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/marcas")
def tiktok_marcas(nome: str = "", _=Depends(verificar_api_key)):
    client = TikTokShopClient()
    try:
        return client.listar_marcas(nome)
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/produtos")
def tiktok_produtos(
    page_size: int = 100,
    page_token: str = "",
    _=Depends(verificar_api_key),
):
    client = TikTokShopClient()
    try:
        return client.listar_produtos(page_size=page_size, page_token=page_token)
    except Exception as e:
        raise HTTPException(500, str(e))


class AplicarMarcaRequest(BaseModel):
    brand_id: Optional[str] = None   # Se vazio, busca "Outras marcas" automaticamente
    product_ids: Optional[List[str]] = None  # Se vazio, aplica em todos


def _aplicar_marca_bg(brand_id: str, product_ids: Optional[List[str]]):
    global _job_marca
    client = TikTokShopClient()
    _job_marca.update({
        "rodando": True, "concluido": False, "erro": None,
        "processados": 0, "atualizados": 0, "erros_prod": 0,
        "brand_id": brand_id, "erros_lista": [],
        "iniciado_em": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })

    try:
        # Coleta todos os produtos se não especificados
        if not product_ids:
            ids = []
            page_token = ""
            while True:
                resp = client.listar_produtos(page_size=100, page_token=page_token)
                prods = resp.get("data", {}).get("products", [])
                ids.extend(p["id"] for p in prods)
                next_token = resp.get("data", {}).get("next_page_token", "")
                if not next_token or not prods:
                    break
                page_token = next_token
            product_ids = ids

        _job_marca["total"] = len(product_ids)
        logger.info(f"TikTok: aplicando marca {brand_id} em {len(product_ids)} produtos")

        for prod_id in product_ids:
            try:
                client.atualizar_marca_produto(prod_id, brand_id)
                _job_marca["atualizados"] += 1
            except Exception as e:
                _job_marca["erros_prod"] += 1
                _job_marca["erros_lista"].append({"id": prod_id, "erro": str(e)})
                logger.warning(f"TikTok: erro ao atualizar produto {prod_id}: {e}")
            _job_marca["processados"] += 1
            time.sleep(0.2)  # respeita rate limit

        _job_marca.update({
            "rodando": False,
            "concluido": True,
            "concluido_em": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        logger.info(f"TikTok marca: {_job_marca['atualizados']} atualizados, {_job_marca['erros_prod']} erros")

    except Exception as e:
        _job_marca.update({"rodando": False, "erro": str(e)})
        logger.error(f"TikTok marca job erro: {e}")


@router.post("/marcas/aplicar")
def tiktok_aplicar_marca(
    body: AplicarMarcaRequest,
    background_tasks: BackgroundTasks,
    _=Depends(verificar_api_key),
):
    if _job_marca["rodando"]:
        raise HTTPException(409, "Job de marca já está rodando")

    client = TikTokShopClient()
    brand_id = body.brand_id

    # Se não passou brand_id, busca "Outras marcas"
    if not brand_id:
        brand_id = client.buscar_id_outras_marcas()
        if not brand_id:
            raise HTTPException(404, "Marca 'Outras marcas' não encontrada. Passe brand_id manualmente.")

    background_tasks.add_task(_aplicar_marca_bg, brand_id, body.product_ids)
    return {
        "ok": True,
        "brand_id": brand_id,
        "mensagem": "Job iniciado em background. Consulte GET /tiktok/marcas/status",
    }


@router.get("/marcas/status")
def tiktok_marca_status(_=Depends(verificar_api_key)):
    return _job_marca


@router.put("/produto/{product_id}")
def tiktok_atualizar_produto(
    product_id: str,
    payload: dict,
    _=Depends(verificar_api_key),
):
    client = TikTokShopClient()
    try:
        return client.atualizar_produto(product_id, payload)
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/token/refresh")
def tiktok_refresh_token(_=Depends(verificar_api_key)):
    """Renova o access_token usando o refresh_token salvo."""
    refresh = os.environ.get("TIKTOK_REFRESH_TOKEN", "")
    if not refresh:
        raise HTTPException(400, "TIKTOK_REFRESH_TOKEN não configurado")
    client = TikTokShopClient()
    try:
        resp = client.refresh_token(refresh)
    except Exception as e:
        raise HTTPException(500, str(e))

    data = resp.get("data", {})
    new_access  = data.get("access_token", "")
    new_refresh = data.get("refresh_token", "")
    if new_access:
        _save_env("TIKTOK_ACCESS_TOKEN",  new_access)
    if new_refresh:
        _save_env("TIKTOK_REFRESH_TOKEN", new_refresh)
    return {"ok": bool(new_access), "data": data}
