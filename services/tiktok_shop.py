"""
TikTok Shop — integração via Bling API v3

Shinsei: idLoja=206293681 (TIKTOK_BLING_LOJA_ID)
AKG:     idLoja=206316394 (TIKTOK_AKG_BLING_LOJA_ID), Shop ID=7494859114033349978
tipoIntegracao: "TikTok Shop"
"""
import logging
import os
import time
from typing import Optional
import requests

logger = logging.getLogger(__name__)

BLING_BASE = "https://api.bling.com.br/Api/v3"
TIKTOK_LOJA_ID     = int(os.environ.get("TIKTOK_BLING_LOJA_ID", "206293681"))
TIKTOK_AKG_LOJA_ID = int(os.environ.get("TIKTOK_AKG_BLING_LOJA_ID", "206316394"))
TIKTOK_TIPO        = os.environ.get("TIKTOK_BLING_TIPO", "TikTok Shop")


def _bling_headers(empresa: str = "shinsei") -> dict:
    """Busca token Bling via BlingClient com auto-refresh."""
    try:
        if empresa == "akg":
            from bling_client import BlingClientAKG
            client = BlingClientAKG()
        else:
            from bling_client import BlingClient
            client = BlingClient()
        hdrs = client._get_headers()
        hdrs["Accept"] = "application/json"
        return hdrs
    except Exception:
        if empresa == "akg":
            token = os.environ.get("BLING_AKG_ACCESS_TOKEN", "")
        else:
            token = os.environ.get("BLING_ACCESS_TOKEN", "")
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _loja_id(empresa: str) -> int:
    return TIKTOK_AKG_LOJA_ID if empresa == "akg" else TIKTOK_LOJA_ID


# ── Anúncios (produtos TikTok no Bling) ──────────────────────────────────

def listar_anuncios(pagina: int = 1, limite: int = 100, empresa: str = "shinsei") -> dict:
    """Lista anúncios TikTok Shop cadastrados no Bling."""
    r = requests.get(
        f"{BLING_BASE}/anuncios",
        params={
            "tipoIntegracao": TIKTOK_TIPO,
            "idLoja": _loja_id(empresa),
            "pagina": pagina,
            "limite": limite,
        },
        headers=_bling_headers(empresa),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def listar_todos_anuncios(empresa: str = "shinsei") -> list:
    """Coleta todos os anúncios TikTok paginando automaticamente."""
    todos = []
    pagina = 1
    while True:
        resp = listar_anuncios(pagina=pagina, limite=100, empresa=empresa)
        data = resp.get("data", [])
        todos.extend(data)
        if len(data) < 100:
            break
        pagina += 1
        time.sleep(0.3)
    return todos


def buscar_anuncio(anuncio_id: int, empresa: str = "shinsei") -> dict:
    r = requests.get(
        f"{BLING_BASE}/anuncios/{anuncio_id}",
        headers=_bling_headers(empresa),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def criar_anuncio(produto_id: int, preco: float, titulo: Optional[str] = None, empresa: str = "shinsei") -> dict:
    """Publica produto do Bling no TikTok Shop."""
    body = {
        "integracao": {"tipo": TIKTOK_TIPO},
        "loja": {"id": _loja_id(empresa)},
        "produto": {"id": produto_id},
        "preco": preco,
    }
    if titulo:
        body["titulo"] = titulo
    r = requests.post(f"{BLING_BASE}/anuncios", json=body, headers=_bling_headers(empresa), timeout=30)
    if not r.ok:
        raise Exception(f"{r.status_code} {r.reason} — {r.text[:300]}")
    return r.json()


def atualizar_anuncio(anuncio_id: int, payload: dict, empresa: str = "shinsei") -> dict:
    r = requests.put(
        f"{BLING_BASE}/anuncios/{anuncio_id}",
        json=payload,
        headers=_bling_headers(empresa),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def atualizar_preco_anuncio(anuncio_id: int, preco: float, empresa: str = "shinsei") -> dict:
    return atualizar_anuncio(anuncio_id, {"preco": preco}, empresa=empresa)


# ── Pedidos TikTok ────────────────────────────────────────────────────────

def listar_pedidos_tiktok(pagina: int = 1, limite: int = 100, situacao: Optional[int] = None, empresa: str = "shinsei") -> dict:
    """Lista pedidos de venda originados do TikTok Shop."""
    params = {"pagina": pagina, "limite": limite, "idLoja": _loja_id(empresa)}
    if situacao:
        params["situacao"] = situacao
    r = requests.get(
        f"{BLING_BASE}/pedidos/vendas",
        params=params,
        headers=_bling_headers(empresa),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


# ── Produtos Bling para vincular ao TikTok ────────────────────────────────

def buscar_produto_bling(sku: str, empresa: str = "shinsei") -> Optional[dict]:
    r = requests.get(
        f"{BLING_BASE}/produtos",
        params={"codigo": sku, "limite": 1},
        headers=_bling_headers(empresa),
        timeout=30,
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    return data[0] if data else None


# ── Status ────────────────────────────────────────────────────────────────

def status(empresa: str = "shinsei") -> dict:
    loja_id = _loja_id(empresa)
    try:
        resp = listar_anuncios(pagina=1, limite=1, empresa=empresa)
        return {
            "ok": True,
            "empresa": empresa,
            "loja_id": loja_id,
            "tipo_integracao": TIKTOK_TIPO,
            "total_anuncios_sample": len(resp.get("data", [])),
            "canal": f"Bling {empresa.upper()} -> TikTok Shop",
        }
    except Exception as e:
        return {"ok": False, "empresa": empresa, "erro": str(e), "loja_id": loja_id}
