# -*- coding: utf-8 -*-
"""
routes/admin_tokens.py
Endpoints internos para monitorar e renovar tokens de todas as integrações.

GET  /admin/tokens/status         — status de todos os tokens (válido/expirado/minutos restantes)
POST /admin/tokens/refresh        — força renovação de todos os tokens
GET  /admin/bling/pedido/{id}     — dados do pedido Bling (inclui CPF do contato)
POST /admin/shopify/token-sync    — atualiza SHOPIFY_ACCESS_TOKEN no Render com o do volume
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests
from fastapi import APIRouter

router = APIRouter(prefix="/admin", tags=["admin-tokens"])

DATA_DIR = Path(__file__).parent.parent / "data"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _bling_tok() -> str:
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from bling_client import BlingClient
        bc = BlingClient()
        if bc.has_local_tokens():
            t = bc._load_tokens()
            return t.get("access_token", "")
    except Exception:
        pass
    for fname in ("bling_tokens.json", "bling_token_fresh.json"):
        f = DATA_DIR / fname
        if f.exists():
            try:
                d = json.loads(f.read_text())
                tok = d.get("access_token") or d.get("data", {}).get("access_token", "")
                if tok:
                    return tok
            except Exception:
                pass
    return os.getenv("BLING_ACCESS_TOKEN", "")


def _check_token_http(url: str, headers: dict) -> bool:
    try:
        r = requests.get(url, headers=headers, timeout=8)
        return r.status_code == 200
    except Exception:
        return False


# ─── Status de todos os tokens ───────────────────────────────────────────────

@router.get("/tokens/status")
def tokens_status():
    """Retorna status atual de todos os tokens de integração."""
    agora = time.time()
    status = {}

    # Bling Shinsei
    try:
        from bling_client import BlingClient
        bc = BlingClient()
        if bc.has_local_tokens():
            t = bc._load_tokens()
            exp = t.get("expires_at", 0)
            restante = exp - agora
            tok = t.get("access_token", "")
            status["bling_shinsei"] = {
                "token_ok": bool(tok),
                "expires_in_min": round(restante / 60, 1) if exp else None,
                "expirado": restante < 0 if exp else None,
            }
        else:
            status["bling_shinsei"] = {"token_ok": False, "motivo": "sem arquivo local"}
    except Exception as e:
        status["bling_shinsei"] = {"erro": str(e)}

    # Bling AKG
    try:
        akg_path = DATA_DIR / "bling_tokens_akg.json"
        if akg_path.exists():
            t = json.loads(akg_path.read_text())
            exp = t.get("expires_at", 0)
            restante = exp - agora
            status["bling_akg"] = {
                "token_ok": bool(t.get("access_token")),
                "expires_in_min": round(restante / 60, 1) if exp else None,
                "expirado": restante < 0 if exp else None,
            }
        else:
            status["bling_akg"] = {"token_ok": False, "motivo": "sem arquivo"}
    except Exception as e:
        status["bling_akg"] = {"erro": str(e)}

    # ML Shinsei
    try:
        ml_path = DATA_DIR / "ml_tokens.json"
        if ml_path.exists():
            t = json.loads(ml_path.read_text())
            tok = t.get("access_token", "")
            ok = _check_token_http("https://api.mercadolibre.com/users/me",
                                   {"Authorization": f"Bearer {tok}"}) if tok else False
            status["ml_shinsei"] = {"token_ok": ok, "user_id": t.get("user_id", "")}
        else:
            status["ml_shinsei"] = {"token_ok": False, "motivo": "sem arquivo"}
    except Exception as e:
        status["ml_shinsei"] = {"erro": str(e)}

    # ML AKG
    try:
        ml_akg_path = DATA_DIR / "ml_tokens_akg.json"
        if ml_akg_path.exists():
            t = json.loads(ml_akg_path.read_text())
            tok = t.get("access_token", "")
            ok = _check_token_http("https://api.mercadolibre.com/users/me",
                                   {"Authorization": f"Bearer {tok}"}) if tok else False
            status["ml_akg"] = {"token_ok": ok, "user_id": t.get("user_id", "")}
        else:
            status["ml_akg"] = {"token_ok": False, "motivo": "sem arquivo"}
    except Exception as e:
        status["ml_akg"] = {"erro": str(e)}

    # Shopify
    try:
        shopify_tok = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
        cfg_path = DATA_DIR / "shopify_config.json"
        if not shopify_tok and cfg_path.exists():
            shopify_tok = json.loads(cfg_path.read_text()).get("access_token", "")
        store = os.getenv("SHOPIFY_STORE", "pknw4n-eg.myshopify.com")
        ok = _check_token_http(
            f"https://{store}/admin/api/2024-01/shop.json",
            {"X-Shopify-Access-Token": shopify_tok}
        ) if shopify_tok else False
        status["shopify"] = {"token_ok": ok, "token_source": "env" if os.getenv("SHOPIFY_ACCESS_TOKEN") else "arquivo"}
    except Exception as e:
        status["shopify"] = {"erro": str(e)}

    # MelhorEnvio
    try:
        me_tok = os.getenv("MELHOR_ENVIO_TOKEN", "")
        if not me_tok:
            f = DATA_DIR / "melhorenvio_token.json"
            if f.exists():
                me_tok = json.loads(f.read_text()).get("access_token", "")
        ok = _check_token_http("https://melhorenvio.com.br/api/v2/me/user",
                               {"Authorization": f"Bearer {me_tok}",
                                "User-Agent": "Aplicacao shinsei-pricing fabiohideki08@gmail.com"}) if me_tok else False
        status["melhor_envio"] = {"token_ok": ok}
    except Exception as e:
        status["melhor_envio"] = {"erro": str(e)}

    # Shopee
    try:
        sp_path = DATA_DIR / "shopee_tokens.json"
        if sp_path.exists():
            t = json.loads(sp_path.read_text())
            exp = t.get("expires_at", 0)
            restante = exp - agora
            status["shopee"] = {
                "token_ok": bool(t.get("access_token")),
                "expires_in_min": round(restante / 60, 1) if exp else None,
                "expirado": restante < 0 if exp else None,
            }
        else:
            status["shopee"] = {"token_ok": False, "motivo": "sem arquivo"}
    except Exception as e:
        status["shopee"] = {"erro": str(e)}

    resumo_ok = sum(1 for v in status.values() if v.get("token_ok"))
    resumo_total = len(status)
    return {"ok": resumo_ok == resumo_total, "tokens_ok": resumo_ok,
            "tokens_total": resumo_total, "detalhes": status}


# ─── Forçar renovação de todos os tokens ─────────────────────────────────────

@router.post("/tokens/refresh")
def refresh_all_tokens():
    """Força renovação imediata de todos os tokens via token_autorefresh."""
    results = {}
    try:
        import token_autorefresh as _tar
        results["bling_shinsei"] = _tar._renovar_bling()
        results["bling_akg"] = _tar._renovar_bling_akg()
        results["ml_shinsei"] = _tar._renovar_ml()
        results["ml_akg"] = _tar._renovar_ml_akg()
        results["shopee"] = _tar._renovar_shopee()
    except Exception as e:
        return {"ok": False, "erro": str(e), "parcial": results}

    ok_count = sum(1 for v in results.values() if v)
    return {"ok": True, "renovados": ok_count, "total": len(results), "detalhes": results}


# ─── Exportar tokens Bling para Render env vars ──────────────────────────────

@router.get("/bling/export-tokens")
def bling_export_tokens():
    """Retorna tokens Bling Shinsei do arquivo local (para bootstrap de env vars)."""
    try:
        import hashlib, base64
        from bling_client import TOKEN_PATH
        if not TOKEN_PATH.exists():
            return {"ok": False, "erro": "Arquivo não encontrado"}
        raw = json.loads(TOKEN_PATH.read_text())
        if "encrypted" in raw:
            cid = os.getenv("BLING_CLIENT_ID", "")
            csec = os.getenv("BLING_CLIENT_SECRET", "")
            key = hashlib.sha256((csec + (cid or "token-key")).encode()).digest()
            enc = base64.b64decode(raw["encrypted"].encode())
            dec = bytes(b ^ key[i % len(key)] for i, b in enumerate(enc))
            tok_data = json.loads(dec.decode())
        else:
            tok_data = raw
        return {
            "ok": True,
            "access_token": tok_data.get("access_token", ""),
            "refresh_token": tok_data.get("refresh_token", ""),
            "expires_at": tok_data.get("expires_at", 0),
        }
    except Exception as e:
        return {"ok": False, "erro": str(e)}


@router.post("/bling/persist-tokens")
def bling_persist_tokens():
    """Lê tokens Bling do arquivo local e salva como env vars no Render."""
    try:
        from bling_client import BlingClient, TOKEN_PATH
        import hashlib, base64
        bc = BlingClient()
        tok_data = bc._load_tokens() if bc.has_local_tokens() else {}
        if not tok_data:
            # Tentar ler arquivo direto
            if TOKEN_PATH.exists():
                raw = json.loads(TOKEN_PATH.read_text())
                if "encrypted" in raw:
                    cid = os.getenv("BLING_CLIENT_ID", "")
                    csec = os.getenv("BLING_CLIENT_SECRET", "")
                    key = hashlib.sha256((csec + (cid or "token-key")).encode()).digest()
                    enc = base64.b64decode(raw["encrypted"].encode())
                    dec = bytes(b ^ key[i % len(key)] for i, b in enumerate(enc))
                    tok_data = json.loads(dec.decode())
                else:
                    tok_data = raw
        if not tok_data.get("access_token"):
            return {"ok": False, "erro": "Token Bling não encontrado no arquivo local"}
        from render_persistence import save_bling_tokens
        ok = save_bling_tokens(tok_data.get("access_token", ""), tok_data.get("refresh_token", ""))
        return {"ok": ok, "access_token_preview": tok_data.get("access_token","")[:20],
                "has_refresh": bool(tok_data.get("refresh_token"))}
    except Exception as e:
        return {"ok": False, "erro": str(e)}


# ─── Lookup Bling pedido (CPF + dados) ────────────────────────────────────────

@router.get("/bling/pedido/{pedido_id}")
def bling_pedido(pedido_id: str):
    """Busca dados do pedido Bling incluindo CPF do contato."""
    tok = _bling_tok()
    if not tok:
        return {"erro": "Token Bling não disponível"}

    h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    r = requests.get(f"https://api.bling.com.br/Api/v3/pedidos/vendas/{pedido_id}",
                     headers=h, timeout=15)
    if r.status_code != 200:
        return {"erro": f"Bling API: {r.status_code}", "body": r.text[:300]}

    pedido = r.json().get("data", {})
    contato = pedido.get("contato", {})
    contato_id = contato.get("id")

    cpf_cnpj = ""
    telefone = ""
    if contato_id:
        r2 = requests.get(f"https://api.bling.com.br/Api/v3/contatos/{contato_id}",
                          headers=h, timeout=15)
        if r2.status_code == 200:
            c = r2.json().get("data", {})
            cpf_cnpj = c.get("cpfCnpj", "")
            telefone = c.get("telefone", "") or c.get("celular", "")

    transporte = pedido.get("transporte", {})
    entrega = pedido.get("enderecoEntrega", {})

    return {
        "pedido_id": pedido_id,
        "numero": pedido.get("numero"),
        "cliente": contato.get("nome", ""),
        "cpf_cnpj": cpf_cnpj,
        "telefone": telefone,
        "total": pedido.get("totalVenda", 0),
        "cep": entrega.get("cep", ""),
        "endereco": f"{entrega.get('endereco','')} {entrega.get('numero','')}".strip(),
        "cidade": entrega.get("municipio", {}).get("nome", "") if isinstance(entrega.get("municipio"), dict) else entrega.get("cidade", ""),
        "uf": entrega.get("uf", ""),
        "transporte": {
            "transportador": transporte.get("transportador", {}).get("nome", ""),
            "servico": transporte.get("servico", ""),
            "valor": transporte.get("freteValor", 0),
        },
    }


# ─── Sincronizar token Shopify do volume → Render env vars ───────────────────

@router.post("/shopify/token-sync")
def shopify_token_sync():
    """Lê token Shopify do volume (shopify_config.json) e atualiza SHOPIFY_ACCESS_TOKEN no Render."""
    cfg_path = DATA_DIR / "shopify_config.json"
    if not cfg_path.exists():
        return {"erro": "shopify_config.json não encontrado no volume"}

    tok = json.loads(cfg_path.read_text()).get("access_token", "")
    if not tok:
        return {"erro": "Token não encontrado em shopify_config.json"}

    # Validar o token antes de salvar
    store = os.getenv("SHOPIFY_STORE", "pknw4n-eg.myshopify.com")
    r = requests.get(f"https://{store}/admin/api/2024-01/shop.json",
                     headers={"X-Shopify-Access-Token": tok}, timeout=10)
    if r.status_code != 200:
        return {"erro": f"Token do volume inválido: {r.status_code}", "token_preview": tok[:20]}

    # Atualizar env var no Render
    try:
        from render_persistence import _patch_env_vars
        ok = _patch_env_vars({"SHOPIFY_ACCESS_TOKEN": tok})
        if ok:
            os.environ["SHOPIFY_ACCESS_TOKEN"] = tok
            return {"ok": True, "mensagem": "SHOPIFY_ACCESS_TOKEN sincronizado com o Render",
                    "token_preview": tok[:20] + "..."}
        return {"ok": False, "mensagem": "Falha ao salvar no Render — verifique RENDER_API_KEY"}
    except Exception as e:
        return {"erro": str(e)}


# ─── Salvar env vars arbitrárias no Render (página de integrações) ─────────────

_ALLOWED_SAVE_ENV = {
    "MELHOR_ENVIO_TOKEN", "SMTP_PASS",
    "ZAPI_TOKEN", "ZAPI_CLIENT_TOKEN", "ZAPI_INSTANCE_ID",
}


@router.post("/tokens/save-env")
def save_env_vars(body: dict):
    """Salva env vars permitidas no Render e injeta no processo atual."""
    updates = {k: v for k, v in body.items() if k in _ALLOWED_SAVE_ENV and isinstance(v, str) and v.strip()}
    if not updates:
        return {"ok": False, "erro": f"Nenhuma var permitida. Vars aceitas: {sorted(_ALLOWED_SAVE_ENV)}"}
    try:
        from render_persistence import _patch_env_vars
        ok = _patch_env_vars(updates)
        for k, v in updates.items():
            os.environ[k] = v
        return {"ok": ok, "vars_salvas": list(updates.keys())}
    except Exception as e:
        return {"ok": False, "erro": str(e)}


# ─── Disparar deploy no Render ────────────────────────────────────────────────

@router.post("/deploy")
def trigger_deploy():
    """Dispara um novo deploy no Render via API."""
    api_key = os.getenv("RENDER_API_KEY", "")
    service_id = os.getenv("RENDER_SERVICE_ID", "srv-d9jhht58nd3s73beak7g")
    if not api_key:
        return {"ok": False, "erro": "RENDER_API_KEY não configurada"}
    r = requests.post(
        f"https://api.render.com/v1/services/{service_id}/deploys",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        json={"clearCache": "do_not_clear"},
        timeout=15,
    )
    if r.status_code in (200, 201):
        d = r.json()
        return {"ok": True, "deploy_id": d.get("id", ""), "status": d.get("status", "")}
    return {"ok": False, "erro": f"HTTP {r.status_code}", "body": r.text[:200]}
