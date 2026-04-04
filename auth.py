"""
auth.py â€” Shinsei Pricing
AutenticaÃ§Ã£o por API key via header X-API-Key.

Uso no app.py:
    from auth import verificar_api_key, PUBLIC_PATHS

    # Adicionar logo apÃ³s app.add_middleware(CORSMiddleware, ...)
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        return await verificar_api_key(request, call_next)

ConfiguraÃ§Ã£o:
    API_KEY=sua_chave_no_.env
    API_KEY_HABILITADO=true   # "false" para desativar sem remover o cÃ³digo

Gerar uma chave segura:
    python -c "import secrets; print(secrets.token_urlsafe(32))"
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from typing import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# ConfiguraÃ§Ã£o
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _api_key() -> str | None:
    return os.getenv("API_KEY", "").strip() or None


def _auth_habilitado() -> bool:
    return os.getenv("API_KEY_HABILITADO", "true").strip().lower() != "false"


# Rotas que nÃ£o exigem autenticaÃ§Ã£o (callbacks OAuth, health check, frontend)
PUBLIC_PATHS = {
    "/",
    "/health",
    "/simulador",
    "/fila",
    "/regras",
    "/bling/auth",
    "/bling/callback",
    "/bling/status",
    "/ml/login",
    "/ml/callback",
    "/mercado-livre",
    "/regras/modelo/download",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/webhooks/bling",
    "/shopify-flow/pricing-suggestion",
    "/estoque/fila",
    "/conferencia-estoque",
    "/integracao-comercial",
    "/config/integracao-comercial",
}

# Prefixos pÃºblicos (qualquer rota que comece com esses valores)
PUBLIC_PREFIXES = (
    "/static/",
    "/pages/",
)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Rate limiting simples (em memÃ³ria)
# Protege contra forÃ§a bruta na API key
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_rate_limit_store: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT_MAX = 60        # mÃ¡ximo de requisiÃ§Ãµes
_RATE_LIMIT_WINDOW = 60.0   # janela em segundos


def _check_rate_limit(client_ip: str) -> bool:
    """Retorna True se o IP estÃ¡ dentro do limite. False se excedeu."""
    agora = time.time()
    janela = _rate_limit_store[client_ip]
    # Remove timestamps fora da janela
    _rate_limit_store[client_ip] = [t for t in janela if agora - t < _RATE_LIMIT_WINDOW]
    if len(_rate_limit_store[client_ip]) >= _RATE_LIMIT_MAX:
        return False
    _rate_limit_store[client_ip].append(agora)
    return True


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Middleware principal
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def verificar_api_key(
    request: Request,
    call_next: Callable[[Request], Awaitable],
):
    """
    Middleware FastAPI que verifica a API key em todas as rotas protegidas.

    A chave pode ser enviada de duas formas:
      - Header:      X-API-Key: <chave>
      - Query param: ?api_key=<chave>  (Ãºtil para testes rÃ¡pidos)
    """
    # Auth desativada via env
    if not _auth_habilitado():
        return await call_next(request)

    path = request.url.path

    # Rotas pÃºblicas â€” passa direto
    if path in PUBLIC_PATHS:
        return await call_next(request)

    if any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES):
        return await call_next(request)

    # Sem API key configurada â€” loga aviso e passa (evita lockout acidental)
    chave_configurada = _api_key()
    if not chave_configurada:
        logger.warning(
            "API_KEY nÃ£o configurada no .env â€” autenticaÃ§Ã£o desativada. "
            "Defina API_KEY para proteger os endpoints."
        )
        return await call_next(request)

    # Rate limit por IP
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        logger.warning("Rate limit excedido para IP %s em %s", client_ip, path)
        return JSONResponse(
            status_code=429,
            content={"detail": "Muitas requisiÃ§Ãµes. Aguarde um momento."},
        )

    # Extrai a chave enviada pelo cliente
    chave_enviada = (
        request.headers.get("X-API-Key")
        or request.headers.get("x-api-key")
        or request.query_params.get("api_key")
    )

    if not chave_enviada:
        return JSONResponse(
            status_code=401,
            content={
                "detail": "AutenticaÃ§Ã£o obrigatÃ³ria. Envie o header X-API-Key.",
                "docs": "/docs",
            },
        )

    # ComparaÃ§Ã£o em tempo constante (evita timing attacks)
    import hmac
    if not hmac.compare_digest(chave_enviada, chave_configurada):
        logger.warning("API key invÃ¡lida de IP %s para %s", client_ip, path)
        return JSONResponse(
            status_code=403,
            content={"detail": "API key invÃ¡lida."},
        )

    return await call_next(request)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# DependÃªncia FastAPI (alternativa ao middleware)
# Use em endpoints individuais se preferir granularidade:
#
#   from fastapi import Depends
#   from auth import api_key_dep
#
#   @app.post("/meu-endpoint", dependencies=[Depends(api_key_dep)])
#   def meu_endpoint(): ...
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

from fastapi import Header, HTTPException, Security
from fastapi.security import APIKeyHeader

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def api_key_dep(x_api_key: str | None = Security(_api_key_header)) -> str:
    """DependÃªncia FastAPI para proteger endpoints individuais."""
    if not _auth_habilitado():
        return "auth-desativada"
    chave = _api_key()
    if not chave:
        return "sem-chave-configurada"
    import hmac
    if not x_api_key or not hmac.compare_digest(x_api_key, chave):
        raise HTTPException(status_code=403, detail="API key invÃ¡lida.")
    return x_api_key

