"""
token_autorefresh.py — Shinsei Pricing
Thread de background que renova tokens automaticamente antes de expirarem.
Após cada renovação, persiste no Secret Manager para sobreviver a deploys.

Cronograma:
  - Verifica a cada 20 min
  - Renova Bling se < 90 min para expirar (token dura 6h)
  - Renova Shopee se < 60 min para expirar (token dura 4h)
  - Renova ML se < 60 min para expirar (token dura ~6h)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
import base64
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR      = Path(__file__).parent / "data"
INTERVALO     = 20 * 60   # 20 min entre verificações
LIMITE_BLING  = 90 * 60   # renova Bling com 90 min de antecedência
LIMITE_SHOPEE = 60 * 60   # renova Shopee com 60 min de antecedência
LIMITE_ML     = 60 * 60   # renova ML com 60 min de antecedência


# ─── Bling ───────────────────────────────────────────────────────────────────

def _renovar_bling() -> bool:
    """Renova tokens Bling via refresh_token e persiste no Secret Manager."""
    try:
        from token_persistence import load_bling_tokens, save_bling_tokens

        cid  = os.getenv("BLING_CLIENT_ID", "")
        csec = os.getenv("BLING_CLIENT_SECRET", "")
        if not cid or not csec:
            logger.warning("Bling auto-refresh: BLING_CLIENT_ID/SECRET não configurados")
            return False

        # Pega refresh_token do Secret Manager (mais atual) ou do arquivo local
        secret = load_bling_tokens() or {}
        refresh_token = secret.get("refresh_token", "")

        if not refresh_token:
            # Fallback: arquivo local (descriptografado via BlingClient interno)
            bling_path = DATA_DIR / "bling_tokens.json"
            if bling_path.exists():
                try:
                    import hashlib
                    raw = json.loads(bling_path.read_text(encoding="utf-8"))
                    if "encrypted" in raw:
                        key = hashlib.sha256((csec + (cid or "token-key")).encode()).digest()
                        enc_bytes = base64.b64decode(raw["encrypted"].encode())
                        dec = bytes(b ^ key[i % len(key)] for i, b in enumerate(enc_bytes))
                        tok = json.loads(dec.decode())
                        refresh_token = tok.get("refresh_token", "")
                    else:
                        refresh_token = raw.get("refresh_token", "")
                except Exception as e:
                    logger.warning("Bling: falha ao ler token local: %s", e)

        if not refresh_token:
            refresh_token = os.getenv("BLING_REFRESH_TOKEN", "")

        if not refresh_token:
            logger.warning("Bling auto-refresh: sem refresh_token disponível")
            return False

        # Requisição de renovação
        basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
        data  = urllib.parse.urlencode({
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
        }).encode()
        req = urllib.request.Request(
            "https://www.bling.com.br/Api/v3/oauth/token",
            data=data,
            headers={
                "Authorization":  f"Basic {basic}",
                "Content-Type":   "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            new = json.loads(resp.read())

        access  = new.get("access_token", "")
        refresh = new.get("refresh_token", "")
        if not access or not refresh:
            logger.error("Bling auto-refresh: resposta sem tokens: %s", new)
            return False

        # Persiste no Secret Manager
        save_bling_tokens(access, refresh)

        # Persiste nas env vars do Render (sobrevive a deploys sem Secret Manager)
        try:
            from render_persistence import save_bling_tokens as _render_save
            _render_save(access, refresh)
        except Exception as _re:
            logger.warning("Bling auto-refresh: falha ao salvar no Render env vars: %s", _re)

        # Atualiza arquivo local (formato criptografado)
        import hashlib
        raw_tok = {
            "access_token":  access,
            "refresh_token": refresh,
            "token_type":    "Bearer",
            "expires_in":    21600,
            "expires_at":    time.time() + 21600,
        }
        key = hashlib.sha256((csec + (cid or "token-key")).encode()).digest()
        enc = base64.b64encode(bytes(b ^ key[i % len(key)] for i, b in enumerate(
            json.dumps(raw_tok).encode()
        ))).decode()
        bling_path = DATA_DIR / "bling_tokens.json"
        DATA_DIR.mkdir(exist_ok=True)
        bling_path.write_text(json.dumps({"encrypted": enc}, ensure_ascii=False), encoding="utf-8")

        logger.info("Bling: tokens renovados automaticamente ✓")
        return True

    except Exception as e:
        logger.error("Bling auto-refresh erro: %s", e)
        return False


def _renovar_bling_akg() -> bool:
    """Renova tokens Bling AKG via refresh_token e persiste no Render."""
    try:
        import json as _json
        akg_path = DATA_DIR / "bling_tokens_akg.json"
        refresh_token = os.getenv("BLING_AKG_REFRESH_TOKEN", "")

        if akg_path.exists():
            try:
                tok = _json.loads(akg_path.read_text(encoding="utf-8"))
                refresh_token = tok.get("refresh_token", refresh_token)
            except Exception:
                pass

        if not refresh_token:
            logger.warning("Bling AKG auto-refresh: sem refresh_token disponível")
            return False

        # Credenciais do credentials.json
        cred_path = DATA_DIR.parent / "data" / "credentials.json"
        cid, csec = "", ""
        try:
            creds = _json.loads((DATA_DIR.parent / "data" / "credentials.json").read_text(encoding="utf-8"))
            akg = creds.get("bling_akg", {})
            cid = akg.get("client_id", "") or os.getenv("BLING_AKG_CLIENT_ID", "")
            csec = akg.get("client_secret", "") or os.getenv("BLING_AKG_CLIENT_SECRET", "")
        except Exception:
            cid = os.getenv("BLING_AKG_CLIENT_ID", "")
            csec = os.getenv("BLING_AKG_CLIENT_SECRET", "")

        if not cid or not csec:
            logger.warning("Bling AKG auto-refresh: credenciais não configuradas")
            return False

        basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
        data  = urllib.parse.urlencode({
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
        }).encode()
        req = urllib.request.Request(
            "https://www.bling.com.br/Api/v3/oauth/token",
            data=data,
            headers={
                "Authorization":  f"Basic {basic}",
                "Content-Type":   "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            new = _json.loads(resp.read())

        access  = new.get("access_token", "")
        refresh = new.get("refresh_token", "")
        if not access or not refresh:
            logger.error("Bling AKG auto-refresh: resposta sem tokens: %s", new)
            return False

        # Salva no arquivo local
        if akg_path.exists():
            try:
                tok = _json.loads(akg_path.read_text(encoding="utf-8"))
            except Exception:
                tok = {}
        else:
            tok = {}
        tok.update({"access_token": access, "refresh_token": refresh, "expires_in": 21600})
        DATA_DIR.mkdir(exist_ok=True)
        akg_path.write_text(_json.dumps(tok, ensure_ascii=False, indent=2), encoding="utf-8")

        # Persiste nas env vars do Render
        try:
            from render_persistence import save_bling_tokens_akg as _render_save
            _render_save(access, refresh)
        except Exception as _re:
            logger.warning("Bling AKG auto-refresh: falha ao salvar no Render env vars: %s", _re)

        logger.info("Bling AKG: tokens renovados automaticamente ✓")
        return True

    except Exception as e:
        logger.error("Bling AKG auto-refresh erro: %s", e)
        return False


def _checar_expiry_bling() -> float:
    """Retorna segundos até expirar o token Bling. -1 se desconhecido."""
    try:
        cid  = os.getenv("BLING_CLIENT_ID", "")
        csec = os.getenv("BLING_CLIENT_SECRET", "")
        bling_path = DATA_DIR / "bling_tokens.json"
        if not bling_path.exists():
            return -1
        import hashlib
        raw = json.loads(bling_path.read_text(encoding="utf-8"))
        if "encrypted" in raw:
            key = hashlib.sha256((csec + (cid or "token-key")).encode()).digest()
            enc_bytes = base64.b64decode(raw["encrypted"].encode())
            dec = bytes(b ^ key[i % len(key)] for i, b in enumerate(enc_bytes))
            tok = json.loads(dec.decode())
        else:
            tok = raw
        expires_at = tok.get("expires_at", 0)
        return max(0, expires_at - time.time())
    except Exception:
        return -1


# ─── Shopee ──────────────────────────────────────────────────────────────────

def _renovar_shopee() -> bool:
    """Renova tokens Shopee via refresh_token e persiste no Secret Manager."""
    try:
        from token_persistence import load_shopee_tokens, save_shopee_tokens
        from services.shopee import ShopeeOAuthService

        svc    = ShopeeOAuthService()
        result = svc.renovar_token()
        if not result.get("success"):
            logger.error("Shopee auto-refresh falhou: %s", result.get("error"))
            return False

        # Lê tokens atualizados do arquivo local (renovar_token() já salva em disco)
        shopee_path = DATA_DIR / "shopee_tokens.json"
        if shopee_path.exists():
            tok = json.loads(shopee_path.read_text(encoding="utf-8"))
            _access  = tok.get("access_token", "")
            _refresh = tok.get("refresh_token", "")
            _shop_id = int(tok.get("shop_id", 0))
            save_shopee_tokens(_access, _refresh, _shop_id)
            # Persiste nas env vars do Render
            try:
                from render_persistence import save_shopee_tokens as _render_save
                _render_save(_access, _refresh, _shop_id)
            except Exception as _re:
                logger.warning("Shopee auto-refresh: falha ao salvar no Render env vars: %s", _re)

        logger.info("Shopee: tokens renovados automaticamente ✓")
        return True

    except Exception as e:
        logger.error("Shopee auto-refresh erro: %s", e)
        return False


def _checar_expiry_shopee() -> float:
    """Retorna segundos até expirar o token Shopee. -1 se desconhecido."""
    try:
        shopee_path = DATA_DIR / "shopee_tokens.json"
        if not shopee_path.exists():
            return -1
        tok = json.loads(shopee_path.read_text(encoding="utf-8"))
        expires_at = tok.get("expires_at", 0)
        return max(0, expires_at - time.time())
    except Exception:
        return -1


# ─── Mercado Livre ────────────────────────────────────────────────────────────

def _renovar_ml() -> bool:
    """Renova tokens ML via refresh_token e persiste no Secret Manager."""
    try:
        from token_persistence import load_ml_tokens, save_ml_tokens

        ml_path = DATA_DIR / "ml_tokens.json"
        if not ml_path.exists():
            return False
        tok = json.loads(ml_path.read_text(encoding="utf-8"))
        refresh_token = tok.get("refresh_token", "")
        client_id     = tok.get("client_id", "") or os.getenv("ML_CLIENT_ID", "")
        client_secret = os.getenv("ML_CLIENT_SECRET", "")
        user_id       = tok.get("user_id", "")

        if not refresh_token or not client_id or not client_secret:
            return False

        data = urllib.parse.urlencode({
            "grant_type":    "refresh_token",
            "client_id":     client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }).encode()
        req = urllib.request.Request("https://api.mercadolibre.com/oauth/token", data=data)
        with urllib.request.urlopen(req, timeout=15) as resp:
            new = json.loads(resp.read())

        access  = new.get("access_token", "")
        refresh = new.get("refresh_token", "")
        if not access:
            return False
        if not user_id:
            user_id = str(new.get("user_id", ""))

        # Atualiza arquivo local
        tok.update({
            "access_token":  access,
            "refresh_token": refresh or refresh_token,
            "user_id":       user_id,
        })
        ml_path.write_text(json.dumps(tok, ensure_ascii=False, indent=2), encoding="utf-8")

        # Persiste no Secret Manager
        save_ml_tokens(access, refresh or refresh_token, client_id, client_secret, user_id)

        # Persiste nas env vars do Render
        try:
            from render_persistence import save_ml_tokens_shinsei as _render_save
            _render_save(access, refresh or refresh_token, user_id)
        except Exception as _re:
            logger.warning("ML auto-refresh: falha ao salvar no Render env vars: %s", _re)

        logger.info("ML: tokens renovados automaticamente ✓")
        return True

    except Exception as e:
        logger.error("ML auto-refresh erro: %s", e)
        return False


def _checar_expiry_ml() -> float:
    """ML tokens não têm expires_at local — assume 5h de vida."""
    return -1


def _renovar_ml_akg() -> bool:
    """Renova tokens ML AKG via refresh_token e persiste no Secret Manager + Render."""
    try:
        ml_path = DATA_DIR / "ml_tokens_akg.json"
        if not ml_path.exists():
            return False
        tok = json.loads(ml_path.read_text(encoding="utf-8"))
        refresh_token = tok.get("refresh_token", "") or os.getenv("ML_AKG_REFRESH_TOKEN", "")
        client_id     = tok.get("client_id", "") or os.getenv("ML_AKG_CLIENT_ID", "")
        client_secret = os.getenv("ML_AKG_CLIENT_SECRET", "")
        user_id       = tok.get("user_id", "")

        if not refresh_token or not client_id or not client_secret:
            return False

        data = urllib.parse.urlencode({
            "grant_type":    "refresh_token",
            "client_id":     client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }).encode()
        req = urllib.request.Request("https://api.mercadolibre.com/oauth/token", data=data)
        with urllib.request.urlopen(req, timeout=15) as resp:
            new = json.loads(resp.read())

        access  = new.get("access_token", "")
        refresh = new.get("refresh_token", "")
        if not access:
            return False
        if not user_id:
            user_id = str(new.get("user_id", ""))

        tok.update({"access_token": access, "refresh_token": refresh or refresh_token, "user_id": user_id})
        ml_path.write_text(json.dumps(tok, ensure_ascii=False, indent=2), encoding="utf-8")

        # Render env vars
        try:
            from render_persistence import save_ml_tokens_akg as _render_save
            _render_save(access, refresh or refresh_token, user_id)
        except Exception as _re:
            logger.warning("ML AKG auto-refresh: falha ao salvar no Render env vars: %s", _re)

        logger.info("ML AKG: tokens renovados automaticamente ✓")
        return True

    except Exception as e:
        logger.error("ML AKG auto-refresh erro: %s", e)
        return False


# ─── Loop principal ───────────────────────────────────────────────────────────

_ultima_renovacao: dict[str, float] = {}
COOLDOWN       = 30 * 60   # não renova o mesmo canal em menos de 30 min
FORCA_BLING    = 3 * 3600  # FIX: refresh forçado Bling a cada 3h (independente de expiry)
                            # Garante rotação frequente e salva par access+refresh no Render


def _loop():
    logger.info("Token auto-refresh: thread iniciada (intervalo=%ds)", INTERVALO)
    # Aguarda 2 min antes da primeira verificação (dá tempo ao startup terminar)
    time.sleep(120)

    while True:
        agora = time.time()

        # ── Bling Shinsei ───────────────────────────────────────────────────────
        # FIX: renova por expiry (90 min antecipado) OU por tempo forçado (3h)
        # Motivo: refresh_token ROTA a cada uso — se o Render save falhar,
        # o próximo restart fica com token antigo → invalid_grant
        try:
            restante = _checar_expiry_bling()
            ult = _ultima_renovacao.get("bling", 0)
            renovar = False
            razao   = ""
            if restante >= 0 and restante < LIMITE_BLING and (agora - ult) > COOLDOWN:
                renovar, razao = True, f"expira em {restante/60:.0f} min"
            elif (agora - ult) > FORCA_BLING:
                renovar, razao = True, "refresh forcado 3h (garantia de rotacao)"
            if renovar:
                logger.info("Bling Shinsei: renovando — %s", razao)
                if _renovar_bling():
                    _ultima_renovacao["bling"] = agora
        except Exception as e:
            logger.error("Auto-refresh Bling inesperado: %s", e)

        # ── Bling AKG ──────────────────────────────────────────────────────────
        try:
            ult = _ultima_renovacao.get("bling_akg", 0)
            if (agora - ult) > FORCA_BLING:  # FIX: 3h em vez de 4h
                logger.info("Bling AKG: refresh forcado 3h")
                if _renovar_bling_akg():
                    _ultima_renovacao["bling_akg"] = agora
        except Exception as e:
            logger.error("Auto-refresh Bling AKG inesperado: %s", e)

        # ── Shopee ─────────────────────────────────────────────────────────────
        try:
            restante = _checar_expiry_shopee()
            ult = _ultima_renovacao.get("shopee", 0)
            if restante >= 0 and restante < LIMITE_SHOPEE and (agora - ult) > COOLDOWN:
                logger.info("Shopee: expira em %.0f min — renovando…", restante / 60)
                if _renovar_shopee():
                    _ultima_renovacao["shopee"] = agora
        except Exception as e:
            logger.error("Auto-refresh Shopee inesperado: %s", e)

        # ── ML Shinsei ─────────────────────────────────────────────────────────
        try:
            ult = _ultima_renovacao.get("ml", 0)
            if (agora - ult) > 4 * 3600:
                if _renovar_ml():
                    _ultima_renovacao["ml"] = agora
        except Exception as e:
            logger.error("Auto-refresh ML inesperado: %s", e)

        # ── ML AKG ─────────────────────────────────────────────────────────────
        try:
            ult = _ultima_renovacao.get("ml_akg", 0)
            if (agora - ult) > 4 * 3600:
                if _renovar_ml_akg():
                    _ultima_renovacao["ml_akg"] = agora
        except Exception as e:
            logger.error("Auto-refresh ML AKG inesperado: %s", e)

        time.sleep(INTERVALO)


_thread: threading.Thread | None = None


def iniciar():
    """Inicia a thread de auto-refresh em daemon. Chame uma vez no startup do app."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, daemon=True, name="token-autorefresh")
    _thread.start()
    logger.info("Token auto-refresh: thread %s iniciada", _thread.name)
