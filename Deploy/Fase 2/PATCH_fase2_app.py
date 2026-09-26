"""
PATCH_fase2_app.py
──────────────────
Alterações necessárias no app.py para integrar auth.py e logging_config.py.
Cada bloco indica exatamente onde e o que alterar.
"""

# ══════════════════════════════════════════════════════════════
# ALTERAÇÃO 1 — Imports (adicionar após imports existentes)
# ══════════════════════════════════════════════════════════════

IMPORTS_ADICIONAIS = '''
import logging
from fastapi import Request
from logging_config import configurar_logging
from auth import verificar_api_key, PUBLIC_PATHS

# Inicia logging antes de qualquer outra coisa
configurar_logging()
logger = logging.getLogger(__name__)
'''

# ══════════════════════════════════════════════════════════════
# ALTERAÇÃO 2 — Middleware de autenticação
# Adicione APÓS o app.add_middleware(CORSMiddleware, ...)
# e ANTES dos @app.on_event
# ══════════════════════════════════════════════════════════════

MIDDLEWARE_AUTH = '''
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    return await verificar_api_key(request, call_next)
'''

# ══════════════════════════════════════════════════════════════
# ALTERAÇÃO 3 — Substituir prints por logging nos endpoints
# Localize cada print no app.py e substitua conforme abaixo.
# ══════════════════════════════════════════════════════════════

# Nenhum print direto encontrado no app.py —
# os erros são tratados via HTTPException (correto).
# Adicione apenas logs de INFO nos endpoints principais:

LOGS_ENDPOINT_PREVIEW = '''
# No endpoint /integracao/preview, após o cálculo bem-sucedido:
logger.info(
    "Precificação calculada: SKU=%s, melhor_canal=%s, fila_auto=%s",
    payload.valor_busca,
    preview.get("melhor_canal"),
    fila_auto.get("adicionado"),
)
'''

LOGS_ENDPOINT_APROVAR = '''
# No endpoint /fila/aprovar/{item_id}:
logger.info("Item aprovado: id=%s, sku=%s", item_id, item.get("sku"))
'''

LOGS_ENDPOINT_REJEITAR = '''
# No endpoint /fila/rejeitar/{item_id}:
logger.info("Item rejeitado: id=%s, sku=%s, motivo=%s", item_id, item.get("sku"), motivo)
'''

LOGS_STARTUP = '''
# No @app.on_event("startup"):
logger.info("Shinsei Pricing iniciado — banco=%s, scheduler=%s",
            _db_path(), "ativo" if _scheduler_ativo() else "inativo")
'''

# ══════════════════════════════════════════════════════════════
# ALTERAÇÃO 4 — CORS restrito (preparação para produção)
# Substitua o allow_origins=["*"] por origens específicas.
# Em desenvolvimento, mantenha ["*"]; em produção, defina o domínio.
# ══════════════════════════════════════════════════════════════

CORS_PRODUCAO = '''
import os

_CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")
_origins = [o.strip() for o in _CORS_ORIGINS.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "X-API-Key"],
)
'''

# Adicione ao .env:
# CORS_ORIGINS=https://seu-dominio.com,https://app.seu-dominio.com
# (Deixe em branco ou "*" para desenvolvimento local)

# ══════════════════════════════════════════════════════════════
# ALTERAÇÃO 5 — Endpoint /health melhorado
# Substitua o @app.get("/health") atual por este:
# ══════════════════════════════════════════════════════════════

ENDPOINT_HEALTH = '''
@app.get("/health")
def health():
    """Health check — sem autenticação (rota pública)."""
    import time
    db_ok = False
    try:
        from database import stats_fila
        stats_fila()
        db_ok = True
    except Exception:
        pass

    return {
        "status": "ok" if db_ok else "degraded",
        "timestamp": datetime.now().isoformat(),
        "db": "sqlite" if db_ok else "error",
        "version": "fase2",
    }
'''

# ══════════════════════════════════════════════════════════════
# CHECKLIST DE APLICAÇÃO
# ══════════════════════════════════════════════════════════════

CHECKLIST = """
Ordem de aplicação:
1. cp fase2/auth.py .
2. cp fase2/logging_config.py .
3. Aplicar ALTERAÇÃO 1 (imports) no app.py
4. Aplicar ALTERAÇÃO 2 (middleware) no app.py
5. Aplicar ALTERAÇÃO 4 (CORS) no app.py — opcional em dev
6. Aplicar ALTERAÇÃO 5 (health melhorado) no app.py
7. Adicionar API_KEY ao .env
8. python -m uvicorn app:app --reload
9. Testar:
   curl -H "X-API-Key: SUA_CHAVE" http://localhost:8000/fila/lista
   curl http://localhost:8000/fila/lista  # deve retornar 401
   curl http://localhost:8000/health      # deve funcionar sem chave
"""

if __name__ == "__main__":
    print(CHECKLIST)
