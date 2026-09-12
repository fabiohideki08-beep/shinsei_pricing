"""
TikTok Shop — rotas FastAPI (via Bling Shinsei)
Prefix: /tiktok

idLoja: 206293681 | tipoIntegracao: "TikTok Shop"

Endpoints:
  GET  /tiktok/status                — estado da integração
  GET  /tiktok/anuncios              — listar anúncios TikTok no Bling
  GET  /tiktok/anuncios/{id}         — detalhe de um anúncio
  POST /tiktok/anuncios              — publicar produto no TikTok Shop
  PUT  /tiktok/anuncios/{id}/preco   — atualizar preço de anúncio
  POST /tiktok/anuncios/lote         — publicar vários produtos em lote
  GET  /tiktok/pedidos               — listar pedidos TikTok
  GET  /tiktok/anuncios/status       — status do job de publicação em lote
"""
import logging
import time
import threading
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel

from auth import verificar_api_key
import services.tiktok_shop as tiktok

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tiktok", tags=["TikTok Shop"])


# ── Estado do job de publicação em lote ──────────────────────────────────
_job: dict = {
    "rodando": False, "concluido": False, "erro": None,
    "total": 0, "processados": 0, "publicados": 0, "erros_n": 0,
    "iniciado_em": None, "concluido_em": None, "erros_lista": [],
}
_job_lock = threading.Lock()


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.get("/status")
def tiktok_status():
    return tiktok.status()


@router.get("/anuncios")
def listar_anuncios(
    pagina: int = 1,
    limite: int = 100,
    todos: bool = False,
    _=Depends(verificar_api_key),
):
    try:
        if todos:
            data = tiktok.listar_todos_anuncios()
            return {"total": len(data), "data": data}
        return tiktok.listar_anuncios(pagina=pagina, limite=limite)
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/anuncios/{anuncio_id}")
def detalhe_anuncio(anuncio_id: int, _=Depends(verificar_api_key)):
    try:
        return tiktok.buscar_anuncio(anuncio_id)
    except Exception as e:
        raise HTTPException(500, str(e))


class PublicarRequest(BaseModel):
    produto_id: int
    preco: float
    titulo: Optional[str] = None


@router.post("/anuncios")
def publicar_anuncio(body: PublicarRequest, _=Depends(verificar_api_key)):
    try:
        return tiktok.criar_anuncio(body.produto_id, body.preco, body.titulo)
    except Exception as e:
        raise HTTPException(500, str(e))


class AtualizarPrecoRequest(BaseModel):
    preco: float


@router.put("/anuncios/{anuncio_id}/preco")
def atualizar_preco(anuncio_id: int, body: AtualizarPrecoRequest, _=Depends(verificar_api_key)):
    try:
        return tiktok.atualizar_preco_anuncio(anuncio_id, body.preco)
    except Exception as e:
        raise HTTPException(500, str(e))


class LoteRequest(BaseModel):
    skus: Optional[List[str]] = None       # Se vazio, busca todos os produtos Bling
    margem_tiktok: float = 0.10            # Margem adicional sobre preço Bling (10% padrão)


def _publicar_lote_bg(skus: Optional[List[str]], margem: float):
    global _job
    import requests as _req

    with _job_lock:
        _job.update({
            "rodando": True, "concluido": False, "erro": None,
            "processados": 0, "publicados": 0, "erros_n": 0,
            "erros_lista": [],
            "iniciado_em": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

    try:
        from bling_client import BlingClient
        client = BlingClient()
        token = client.access_token
        hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        # Buscar produtos Bling (todos ou filtrado por SKU)
        produtos = []
        pagina = 1
        while True:
            params = {"pagina": pagina, "limite": 100, "tipo": "P"}
            r = _req.get("https://api.bling.com.br/Api/v3/produtos", params=params, headers=hdrs, timeout=30)
            data = r.json().get("data", [])
            if skus:
                data = [p for p in data if p.get("codigo", "") in skus]
            produtos.extend(data)
            if len(r.json().get("data", [])) < 100:
                break
            pagina += 1
            time.sleep(0.3)

        _job["total"] = len(produtos)
        logger.info(f"TikTok lote: {len(produtos)} produtos para publicar")

        for prod in produtos:
            prod_id = prod["id"]
            preco_base = float(prod.get("preco", 0) or 0)
            preco_tiktok = round(preco_base * (1 + margem), 2)

            if preco_tiktok <= 0:
                _job["erros_n"] += 1
                _job["erros_lista"].append({"sku": prod.get("codigo"), "erro": "preço zero"})
                _job["processados"] += 1
                continue

            try:
                tiktok.criar_anuncio(prod_id, preco_tiktok)
                _job["publicados"] += 1
            except Exception as e:
                _job["erros_n"] += 1
                _job["erros_lista"].append({"sku": prod.get("codigo"), "erro": str(e)})

            _job["processados"] += 1
            time.sleep(0.4)

        _job.update({
            "rodando": False,
            "concluido": True,
            "concluido_em": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        logger.info(f"TikTok lote concluído: {_job['publicados']} publicados, {_job['erros_n']} erros")

    except Exception as e:
        _job.update({"rodando": False, "erro": str(e)})
        logger.error(f"TikTok lote erro: {e}")


@router.post("/anuncios/lote")
def publicar_lote(
    body: LoteRequest,
    background_tasks: BackgroundTasks,
    _=Depends(verificar_api_key),
):
    if _job["rodando"]:
        raise HTTPException(409, "Job de publicação em lote já está rodando")
    background_tasks.add_task(_publicar_lote_bg, body.skus, body.margem_tiktok)
    return {
        "ok": True,
        "loja_id": tiktok.TIKTOK_LOJA_ID,
        "mensagem": "Publicação em lote iniciada. Consulte GET /tiktok/anuncios/status",
    }


@router.get("/anuncios/status")
def status_lote(_=Depends(verificar_api_key)):
    return _job


@router.get("/pedidos")
def listar_pedidos(
    pagina: int = 1,
    limite: int = 100,
    situacao: Optional[int] = None,
    _=Depends(verificar_api_key),
):
    try:
        return tiktok.listar_pedidos_tiktok(pagina=pagina, limite=limite, situacao=situacao)
    except Exception as e:
        raise HTTPException(500, str(e))
