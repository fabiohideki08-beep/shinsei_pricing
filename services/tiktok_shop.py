"""
TikTok Shop — integração via Bling API v3
idLoja TikTok Shop Shinsei: 206293681
tipoIntegracao: "TikTok Shop"

Todos os produtos, anúncios e pedidos TikTok são gerenciados pelo Bling Shinsei.
"""
import logging
import os
import time
from typing import Optional
import requests

logger = logging.getLogger(__name__)

BLING_BASE = "https://api.bling.com.br/Api/v3"
TIKTOK_LOJA_ID   = int(os.environ.get("TIKTOK_BLING_LOJA_ID", "206293681"))
TIKTOK_TIPO      = os.environ.get("TIKTOK_BLING_TIPO", "TikTok Shop")


def _bling_headers() -> dict:
    """Busca token Bling Shinsei via BlingClient."""
    try:
        from bling_client import BlingClient
        client = BlingClient()
        token = client.access_token or client.tokens.get("access_token", "")
    except Exception:
        token = os.environ.get("BLING_ACCESS_TOKEN", "")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


# ── Anúncios (produtos TikTok no Bling) ──────────────────────────────────

def listar_anuncios(pagina: int = 1, limite: int = 100) -> dict:
    """Lista anúncios TikTok Shop cadastrados no Bling Shinsei."""
    hdrs = _bling_headers()
    r = requests.get(
        f"{BLING_BASE}/anuncios",
        params={
            "tipoIntegracao": TIKTOK_TIPO,
            "idLoja": TIKTOK_LOJA_ID,
            "pagina": pagina,
            "limite": limite,
        },
        headers=hdrs,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def listar_todos_anuncios() -> list:
    """Coleta todos os anúncios TikTok paginando automaticamente."""
    todos = []
    pagina = 1
    while True:
        resp = listar_anuncios(pagina=pagina, limite=100)
        data = resp.get("data", [])
        todos.extend(data)
        if len(data) < 100:
            break
        pagina += 1
        time.sleep(0.3)
    return todos


def buscar_anuncio(anuncio_id: int) -> dict:
    r = requests.get(
        f"{BLING_BASE}/anuncios/{anuncio_id}",
        headers=_bling_headers(),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def criar_anuncio(produto_id: int, preco: float, titulo: Optional[str] = None) -> dict:
    """Publica produto do Bling no TikTok Shop."""
    hdrs = _bling_headers()
    body = {
        "integracao": {"tipo": TIKTOK_TIPO},
        "loja": {"id": TIKTOK_LOJA_ID},
        "produto": {"id": produto_id},
        "preco": preco,
    }
    if titulo:
        body["titulo"] = titulo
    r = requests.post(f"{BLING_BASE}/anuncios", json=body, headers=hdrs, timeout=30)
    r.raise_for_status()
    return r.json()


def atualizar_anuncio(anuncio_id: int, payload: dict) -> dict:
    r = requests.put(
        f"{BLING_BASE}/anuncios/{anuncio_id}",
        json=payload,
        headers=_bling_headers(),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def atualizar_preco_anuncio(anuncio_id: int, preco: float) -> dict:
    return atualizar_anuncio(anuncio_id, {"preco": preco})


# ── Pedidos TikTok ────────────────────────────────────────────────────────

def listar_pedidos_tiktok(pagina: int = 1, limite: int = 100, situacao: Optional[int] = None) -> dict:
    """Lista pedidos de venda originados do TikTok Shop."""
    params = {"pagina": pagina, "limite": limite, "idLoja": TIKTOK_LOJA_ID}
    if situacao:
        params["situacao"] = situacao
    r = requests.get(
        f"{BLING_BASE}/pedidos/vendas",
        params=params,
        headers=_bling_headers(),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


# ── Produtos Bling para vincular ao TikTok ────────────────────────────────

def buscar_produto_bling(sku: str) -> Optional[dict]:
    r = requests.get(
        f"{BLING_BASE}/produtos",
        params={"codigo": sku, "limite": 1},
        headers=_bling_headers(),
        timeout=30,
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    return data[0] if data else None


# ── Status ────────────────────────────────────────────────────────────────

def status() -> dict:
    try:
        resp = listar_anuncios(pagina=1, limite=1)
        return {
            "ok": True,
            "loja_id": TIKTOK_LOJA_ID,
            "tipo_integracao": TIKTOK_TIPO,
            "total_anuncios_sample": len(resp.get("data", [])),
            "canal": "Bling Shinsei → TikTok Shop",
        }
    except Exception as e:
        return {"ok": False, "erro": str(e), "loja_id": TIKTOK_LOJA_ID}
