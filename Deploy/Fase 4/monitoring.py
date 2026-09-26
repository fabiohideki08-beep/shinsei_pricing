"""
monitoring.py — Shinsei Pricing
Endpoints de monitoramento e métricas.

Adicione ao app.py:
    from monitoring import router as monitoring_router
    app.include_router(monitoring_router)

Endpoints:
    GET /health    — health check completo (substitui o atual)
    GET /metrics   — métricas resumidas do sistema
    GET /ready     — readiness probe (Kubernetes/Docker)
"""

from __future__ import annotations

import importlib
import logging
import os
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# Momento em que o app iniciou (para calcular uptime)
_START_TIME = time.time()

BASE_DIR = Path(__file__).parent


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _check_db() -> dict:
    """Verifica se o banco SQLite está acessível e retorna contagens."""
    try:
        db = importlib.import_module("database")
        stats = db.stats_fila()
        regras = len(db.listar_regras())
        return {"ok": True, "tipo": "sqlite", "fila": stats, "regras": regras}
    except Exception as e:
        return {"ok": False, "erro": str(e)}


def _check_bling() -> dict:
    """Verifica se o Bling está autenticado (sem fazer request externo)."""
    try:
        mod = importlib.import_module("bling_client")
        BlingClient = getattr(mod, "BlingClient", None)
        if not BlingClient:
            return {"ok": False, "motivo": "BlingClient não encontrado"}
        client = BlingClient()
        if not client.has_local_tokens():
            return {"ok": False, "motivo": "Sem tokens — acesse /bling/auth"}
        if client._token_expired():
            return {"ok": False, "motivo": "Token expirado — será renovado no próximo request"}
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "motivo": str(e)}


def _check_ml() -> dict:
    """Verifica se o ML está autenticado."""
    try:
        from pathlib import Path as P
        from services.mercado_livre import MercadoLivreOAuthService
        oauth = MercadoLivreOAuthService(base_dir=BASE_DIR)
        return {"ok": oauth.status().get("conectado", False)}
    except Exception:
        return {"ok": False, "motivo": "Módulo ML não disponível"}


def _check_scheduler() -> dict:
    """Verifica se o scheduler está rodando."""
    try:
        mod = importlib.import_module("scheduler")
        thread = getattr(mod, "_scheduler_thread", None)
        ativo = thread is not None and thread.is_alive()
        return {
            "ok": ativo,
            "ativo": ativo,
            "intervalo_segundos": int(os.getenv("SCHEDULER_INTERVALO", "300")),
        }
    except Exception:
        return {"ok": False, "motivo": "Módulo scheduler não disponível"}


def _disk_usage() -> dict:
    """Uso de disco do diretório de dados."""
    try:
        import shutil
        total, used, free = shutil.disk_usage(BASE_DIR / "data")
        return {
            "total_mb": round(total / 1024 / 1024),
            "used_mb": round(used / 1024 / 1024),
            "free_mb": round(free / 1024 / 1024),
            "uso_pct": round(used / total * 100, 1),
        }
    except Exception:
        return {}


def _db_size() -> str:
    """Tamanho do arquivo do banco SQLite."""
    try:
        db_url = os.getenv("DATABASE_URL", f"sqlite:///data/shinsei.db")
        db_path = db_url.replace("sqlite:///", "")
        size = Path(db_path).stat().st_size
        return f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"
    except Exception:
        return "desconhecido"


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────

@router.get("/health", tags=["Monitoramento"])
def health():
    """
    Health check completo.
    Verifica banco, Bling, scheduler e retorna status consolidado.
    Rota pública — sem autenticação.
    """
    db = _check_db()
    bling = _check_bling()
    scheduler = _check_scheduler()

    # Status geral: ok só se banco estiver funcionando
    # (Bling e ML podem estar desconectados sem impedir o funcionamento básico)
    status = "ok" if db["ok"] else "degraded"

    uptime_s = int(time.time() - _START_TIME)
    uptime_str = f"{uptime_s // 3600}h {(uptime_s % 3600) // 60}m {uptime_s % 60}s"

    return {
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime": uptime_str,
        "versao": "fase4",
        "python": sys.version.split()[0],
        "componentes": {
            "banco": db,
            "bling": bling,
            "scheduler": scheduler,
        },
    }


@router.get("/ready", tags=["Monitoramento"])
def readiness():
    """
    Readiness probe — usado pelo Docker/Kubernetes para saber se o app
    está pronto para receber tráfego.
    Retorna 200 se ok, 503 se não estiver pronto.
    """
    db = _check_db()
    if not db["ok"]:
        return JSONResponse(
            status_code=503,
            content={"ready": False, "motivo": f"Banco indisponível: {db.get('erro')}"},
        )
    return {"ready": True}


@router.get("/metrics", tags=["Monitoramento"])
def metrics():
    """
    Métricas resumidas do sistema.
    Útil para dashboards e alertas simples.
    Requer autenticação (protegido pelo middleware de API key).
    """
    db_info = _check_db()
    ml_info = _check_ml()

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime_segundos": int(time.time() - _START_TIME),
        "sistema": {
            "python": sys.version.split()[0],
            "plataforma": platform.system(),
            "disco": _disk_usage(),
            "banco_tamanho": _db_size(),
        },
        "fila": db_info.get("fila", {}),
        "regras_ativas": db_info.get("regras", 0),
        "integracoes": {
            "bling": _check_bling(),
            "mercado_livre": ml_info,
            "scheduler": _check_scheduler(),
        },
        "env": {
            "modo_aprovacao": "manual",  # carregado da config
            "scheduler_intervalo": int(os.getenv("SCHEDULER_INTERVALO", "300")),
            "log_level": os.getenv("LOG_LEVEL", "INFO"),
        },
    }
