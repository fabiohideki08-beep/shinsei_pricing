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
    "/bling/auth2",
    "/bling/callback2",
    "/bling/status2",
    "/ml/login",
    "/ml/callback",
    "/ml/config",
    "/ml/status",
    "/ml/refresh",
    "/ml/tokens",
    "/ml/login2",
    "/ml/callback2",
    "/ml/status2",
    "/ml/refresh2",
    "/ml/tokens2",
    "/bling/tokens2",
    "/bling/token",
    "/bling/status2",
    "/ml/akg/cota-gratis",
    "/bling/akg/lojas",
    "/bling/akg/anuncios-sem-ml",
    "/ml/akg/copiar-shinsei/status",
    "/ml/akg/copiar-shinsei/dry-run",
    "/ml/akg/copiar-shinsei/iniciar",
    "/ml/akg/copiar-shinsei/resetar",
    "/ml/akg/copiar-shinsei/teste-real",
    "/mercado-livre",
    "/regras/modelo/download",
    "/regras/importar-excel",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/webhooks/bling",
    "/webhooks/shopify/produto",
    "/shopify/webhook/order-paid",
    "/shopify/status",
    "/shopify/callback",
    "/shopify/auth",
    "/shopify/install-gtag",
    "/shopify/gtag-status",
    "/auditoria/shopify/bling-sem-shopify",
    "/auditoria/bling-sem-shopify",
    "/auditoria/shopify",
    "/auditoria/mp-status",
    "/ml/status",
    "/integracoes",
    "/auditoria/ml-estoque",
    "/auditoria/fila",
    "/auditoria-automatica",
    "/amazon",
    "/amazon/status",
    "/shopify",
    "/auditoria/amazon/limpar-tudo",
    "/auditoria/amazon/limpar-resolvidos",
    "/auditoria/amazon/ignorar/",
    "/auditoria/amazon/conferir",
    "/auditoria/amazon",
    "/auditoria/ml-sem-sku",
    "/fila/reset-total",
    "/fila/popular-estoque",
    "/fila/popular-estoque/status",
    "/fila/popular-estoque/reset",
    "/auditoria/ml-estoque/limpar-tudo",
    "/auditoria/estoque-negativo/limpar-tudo",
    "/auditoria/shopify/limpar-tudo",
    "/auditoria/estoque-negativo",
    "/integracao/preview",
    "/fila/lista",
    "/fila/adicionar",
    "/fila/aprovar",
    "/fila/rejeitar",
    "/fila/stats-detalhados",
    "/fila/aprovar-lote",
    "/fila/rejeitar-lote",
    "/fila/exportar-sem-dados",
    "/fila/importar-correcoes",
    "/fila/importar-correcoes/status",
    "/fila/completar",
    "/bling/produto/atualizar-peso",
    "/bling/produto/atualizar-preco",
    "/bling/produto/buscar-por-nome",
    "/bling/produto/atualizar-imagem-variacao",
    "/bling/produto/atualizar-imagem-simples",
    "/bling/produto/buscar-por-sku",
    "/bling/debug/sku",
    "/bling/produto/buscar",
    "/bling/raw-token",
    "/shopify-flow/pricing-suggestion",
    "/estoque/fila",
    "/conferencia-estoque",
    "/integracao-comercial",
    "/config/integracao-comercial",
    "/seo-health",
    "/seo-health/dados",
    "/gmc",
    "/gmc/status",
    "/gmc/analise-shopping",
    "/gmc/ads-link-status",
    "/gmc/vincular-ads",
    "/marketing",
    "/oee",
    "/sie",
    "/frete/painel",
    # Hub e páginas de sistema
    "/hub",
    "/sistema/bling",
    "/sistema/ml",
    "/sistema/shopify",
    "/sistema/amazon",
    "/sistema/shopee",
    "/sistema/google",
    # Módulos avançados
    "/cost-engine",
    "/cost-allocation",
    "/perfis",
    "/regras-calculo",
    # APIs dos módulos avançados
    "/modulos/cost-engine",
    "/modulos/cost-allocation",
    "/modulos/sie",
    "/modulos/regras-calculo",
    "/rateio/visoes",
    "/config/automacao",
    "/config/regras-precificacao",
    # SIE endpoints
    "/sie/simular",
    "/sie/anti-colapso",
    "/sie/health-dashboard",
    "/sie/score-config",
    # Estoque negativo — análise e batch
    "/auditoria/estoque-negativo/resumo",
    "/auditoria/estoque-negativo/ignorar-leves",
    # Conferência de SKUs
    "/conferencia-sku",
    "/conferencia-sku/executar",
    "/conferencia-sku/status",
    "/conferencia-sku/resultado",
    # Dashboard e páginas por canal
    "/dashboard",
    "/conferencia/ml",
    "/conferencia/ml/vincular",
    "/conferencia/ml/sugestoes-vinculo",
    "/conferencia/ml/aplicar-vinculo",
    "/conferencia/ml/vincular-variacao",
    "/conferencia/ml/aplicar-vinculo-automatico",
    "/conferencia/shopify",
    "/conferencia/amazon",
    "/conferencia/shopee",
    "/auditoria/canais",
    "/auditoria/canais/dados",
    "/ml/debug/item",
    "/bling/debug/sku-get",
    "/shopee/item/preview",
    "/shopee/item/importar-bling",
    # Google Ads
    "/taxas/status",
    "/ads/status",
    "/ads/diagnostico",
    # Shopify deploy
    "/amazon/exportar-lote/iniciar",
    "/amazon/exportar-lote/calcular-precos",
    "/amazon/exportar-lote/confirmar-preco",
    "/amazon/exportar-lote/processar",
    "/amazon/exportar-lote/status",
    "/amazon/exportar-lote/reset",
    "/amazon/kits",
    "/amazon/kits/lista",
    "/amazon/kits/exportar",
    "/amazon/kits/status",
    "/shopify/deploy-secao-oferta",
    "/shopify/template-homepage",
    "/shopify/substituir-secao",
    # Fila de conferência de preço de custo
    "/fila-custo",
    "/fila-custo/popular",
    "/fila-custo/lista",
    "/fila-custo/atualizar",
    "/fila-custo/pular",
    "/fila-custo/reset",
    # Multiempresas — auditoria Shinsei × AKG
    "/multiempresas",
    "/multiempresas/diagnostico",
    "/multiempresas/auditoria/iniciar",
    "/multiempresas/auditoria/status",
    "/multiempresas/auditoria/produtos",
}


# Prefixos pÃºblicos (qualquer rota que comece com esses valores)
PUBLIC_PREFIXES = (
    "/fila/aprovar/",
    "/fila/rejeitar/",
    "/fila/completar/",
    "/fila/links/",
    "/auditoria/shopify/",
    "/ml-ads/",
    "/bling/produto/",
    "/ml/akg/verificar-item/",
    "/ml/shinsei/item-raw/",
    "/ml/akg/debug-payload/",
    "/ml/akg/debug-post/",
    "/ml/injetar-tokens",
    "/auditoria/ml-estoque/",
    "/auditoria/estoque-negativo/",
    "/static/",
    "/pages/",
    "/frete/",       # Shopify Carrier Service + widget de frete (sem API key)
    "/amazon/auth",   # Amazon SP-API OAuth (sem API key)
    "/amazon/callback",
    "/amazon/exportar-produto",
    "/amazon/listing/",
    "/amazon/listing/",
    "/amazon/fees/",
    "/amazon/product-types",
    "/amazon/product-type-schema/",
    "/amazon/catalog/",
    "/shopee/",       # Shopee OAuth e endpoints (sem API key)
    "/bling/produto/variacoes/",       # Listagem de variações (sem auth)
    "/bling/produto/atualizar-imagens-batch",  # Batch de imagens (sem auth)
    "/bling/produto/",  # Endpoints de produto Bling (imagens, variações)
    "/bling/debug/",    # Debug endpoints (sem auth)
    "/api/produto/",    # Busca de produto pelo simulador (sem auth — leitura pública)
    "/sistema/",        # Páginas de sistema (sem auth)
    "/modulos/",        # APIs dos módulos avançados (sem auth)
    "/config/regras-precificacao/",  # Ativar perfil
    "/config/credenciais",           # Salvar credenciais AKG/Google (chamado pela página de integrações)
    "/bling/akg/",                   # OAuth Bling AKG (auth e callback)
    "/gmc/",          # GMC scan, status, blacklist
    "/seo-health/",   # SEO Health análise e pagespeed
    "/marketing/",    # Marketing endpoints
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






