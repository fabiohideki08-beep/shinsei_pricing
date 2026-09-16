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

import requests as _requests
from fastapi import APIRouter, Depends
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
        "versao": "fase5-kits2",
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


# ─────────────────────────────────────────────
# Daily Check — GMC + Bling + WhatsApp Alert
# ─────────────────────────────────────────────

_ZAPI_URL = (
    "https://api.z-api.io/instances/3F848712DB88C2FADEE6A6D84865BDBD"
    "/token/7D0763C39F052F8A110678A9/send-text"
)
_ZAPI_CLIENT_TOKEN = "F4bc283e090774be2abceb813e1d5c713S"
_DONO_PHONE = "5511994697944"
_SELF_URL = "https://shinsei-pricing.onrender.com"


def _whatsapp_alert(message: str) -> dict:
    try:
        r = _requests.post(
            _ZAPI_URL,
            headers={"Client-Token": _ZAPI_CLIENT_TOKEN, "Content-Type": "application/json"},
            json={"phone": _DONO_PHONE, "message": message},
            timeout=15,
        )
        return {"ok": r.status_code == 200, "status_code": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/daily-check", tags=["Monitoramento"])
def daily_check():
    """
    Check diário do sistema: GMC scan, Bling health, WhatsApp alert se houver problemas.
    Rota pública — protegida pelo middleware de API key global.
    """
    api_key = os.getenv("API_KEY", "")
    hdrs = {"X-Api-Key": api_key}
    problemas = []
    acoes = []
    resultado: dict = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "gmc": {},
        "bling_shinsei": {},
        "bling_akg": {},
        "whatsapp_enviado": False,
        "problemas": [],
    }

    # 1. Disparar scan GMC
    try:
        _requests.post(f"{_SELF_URL}/gmc/scan", headers=hdrs, timeout=10)
    except Exception as e:
        resultado["gmc"]["scan_error"] = str(e)

    # 2. Poll status até concluir (max 5min)
    scan_status: dict = {}
    for _ in range(20):
        time.sleep(15)
        try:
            r = _requests.get(f"{_SELF_URL}/gmc/status", headers=hdrs, timeout=10)
            scan_status = r.json()
            if not scan_status.get("scan", {}).get("rodando", True):
                break
        except Exception:
            pass
    resultado["gmc"]["status"] = scan_status

    resumo = scan_status.get("resumo", {})
    reprovados = resumo.get("total_reprovados", 0)
    delete_erros = len((scan_status.get("auto_delete") or {}).get("erros", []))

    if reprovados > 0:
        try:
            _requests.post(f"{_SELF_URL}/gmc/corrigir", headers=hdrs, timeout=10)
            time.sleep(30)
            r = _requests.get(f"{_SELF_URL}/gmc/status", headers=hdrs, timeout=10)
            scan_status = r.json()
            resumo = scan_status.get("resumo", {})
            reprovados = resumo.get("total_reprovados", 0)
            resultado["gmc"]["status_pos_correcao"] = scan_status
        except Exception:
            pass
        if reprovados > 0:
            motivos = resumo.get("counts_disapproved", {})
            problemas.append(f"GMC: {reprovados} produto(s) reprovado(s) — motivos: {motivos}")
            acoes.append("Verificar: https://merchants.google.com")

    if delete_erros > 0:
        problemas.append(f"GMC auto-delete: {delete_erros} erro(s)")

    # 3. Bling Shinsei health
    try:
        r = _requests.get(f"{_SELF_URL}/bling/token-health", headers=hdrs, timeout=10)
        health = r.json()
        resultado["bling_shinsei"] = health
        min_rest = (health.get("shinsei") or {}).get("expires_in_min", 999)
        if min_rest < 60:
            problemas.append(f"Bling Shinsei: token expira em {min_rest:.0f} min")
            acoes.append(f"Reconectar Shinsei: {_SELF_URL}/bling/callback")
    except Exception as e:
        resultado["bling_shinsei"] = {"error": str(e)}

    # 4. Bling AKG status
    try:
        r = _requests.get(f"{_SELF_URL}/bling/status2", timeout=10)
        akg = r.json()
        resultado["bling_akg"] = akg
        if akg.get("expirado", False):
            problemas.append("Bling AKG: token expirado — sincronizacao AKG bloqueada")
            acoes.append(f"Reconectar AKG: {_SELF_URL}/bling/callback2")
    except Exception as e:
        resultado["bling_akg"] = {"error": str(e)}

    resultado["problemas"] = problemas

    # 5. WhatsApp alert se houver problemas
    if problemas:
        ts = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
        linhas = ["*[Shinsei Pricing - Alerta Diario]*", f"Data: {ts}", ""]
        linhas += [f"- {p}" for p in problemas]
        if acoes:
            linhas += ["", "Acoes necessarias:"]
            linhas += [f"- {a}" for a in acoes]
        msg = "\n".join(linhas)
        resultado["whatsapp_enviado"] = True
        resultado["whatsapp_resultado"] = _whatsapp_alert(msg)
        resultado["whatsapp_mensagem"] = msg
    else:
        resultado["status"] = "OK — sistema saudavel, nenhum alerta enviado"

    return {"ok": True, "resultado": resultado}
