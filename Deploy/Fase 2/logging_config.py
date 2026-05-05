"""
logging_config.py — Shinsei Pricing
Logging centralizado com rotação de arquivos.

Uso:
    # No topo do app.py, logo após os imports:
    from logging_config import configurar_logging
    configurar_logging()

    # Em qualquer módulo:
    import logging
    logger = logging.getLogger(__name__)
    logger.info("Produto %s precificado: R$ %.2f", sku, preco)

Saída:
    - Console: nível INFO, formato legível
    - Arquivo logs/app.log: nível DEBUG, com rotação diária (7 dias)
    - Arquivo logs/erros.log: apenas ERROR/CRITICAL, rotação semanal

Variáveis de ambiente:
    LOG_LEVEL      — DEBUG | INFO | WARNING | ERROR (padrão: INFO)
    LOG_DIR        — diretório dos arquivos de log (padrão: ./logs)
    LOG_CONSOLE    — "false" para desativar saída no console (útil em produção com systemd)
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path


def configurar_logging(
    nivel: str | None = None,
    log_dir: Path | None = None,
) -> None:
    """
    Configura o sistema de logging da aplicação.
    Seguro para chamar múltiplas vezes (idempotente).
    """
    nivel_str = (nivel or os.getenv("LOG_LEVEL", "INFO")).upper()
    nivel_num = getattr(logging, nivel_str, logging.INFO)

    log_dir = log_dir or Path(os.getenv("LOG_DIR", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)

    console_ativo = os.getenv("LOG_CONSOLE", "true").strip().lower() != "false"

    # Formato rico para arquivo, compacto para console
    fmt_arquivo = "%(asctime)s %(levelname)-8s [%(name)s:%(lineno)d] %(message)s"
    fmt_console = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = []

    # ── Console ──────────────────────────────────────────────
    if console_ativo:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(nivel_num)
        console_handler.setFormatter(logging.Formatter(fmt_console, datefmt=datefmt))
        handlers.append(console_handler)

    # ── app.log — rotação diária, 7 dias ─────────────────────
    try:
        app_handler = logging.handlers.TimedRotatingFileHandler(
            filename=log_dir / "app.log",
            when="midnight",
            interval=1,
            backupCount=7,
            encoding="utf-8",
        )
        app_handler.setLevel(logging.DEBUG)
        app_handler.setFormatter(logging.Formatter(fmt_arquivo, datefmt=datefmt))
        handlers.append(app_handler)
    except Exception as e:
        print(f"[logging_config] Aviso: não foi possível criar app.log: {e}", file=sys.stderr)

    # ── erros.log — apenas ERROR e CRITICAL, rotação semanal ─
    try:
        erro_handler = logging.handlers.TimedRotatingFileHandler(
            filename=log_dir / "erros.log",
            when="W0",   # toda segunda-feira
            backupCount=4,
            encoding="utf-8",
        )
        erro_handler.setLevel(logging.ERROR)
        erro_handler.setFormatter(logging.Formatter(fmt_arquivo, datefmt=datefmt))
        handlers.append(erro_handler)
    except Exception as e:
        print(f"[logging_config] Aviso: não foi possível criar erros.log: {e}", file=sys.stderr)

    # ── Root logger ───────────────────────────────────────────
    root = logging.getLogger()
    if root.handlers:
        # Evita duplicação ao chamar múltiplas vezes
        root.handlers.clear()
    root.setLevel(logging.DEBUG)   # root captura tudo; handlers filtram
    for h in handlers:
        root.addHandler(h)

    # ── Silencia loggers muito verbosos de libs externas ─────
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info(
        "Logging configurado — nível=%s, arquivo=%s/app.log",
        nivel_str, log_dir
    )


# ─────────────────────────────────────────────
# Guia de migração de prints para logging
# ─────────────────────────────────────────────
#
# ANTES (espalhado no código):
#   print(f"Atualizando produto {id}: {custo} -> {novo_preco}")
#   print("Erro:", e)
#
# DEPOIS:
#   logger.info("Produto %s atualizado: custo=%.2f → preço=%.2f", id, custo, novo_preco)
#   logger.error("Falha ao atualizar produto %s: %s", id, e, exc_info=True)
#
# Níveis:
#   logger.debug(...)    — detalhes internos (só aparece no arquivo)
#   logger.info(...)     — fluxo normal, eventos importantes
#   logger.warning(...)  — algo inesperado mas não crítico
#   logger.error(...)    — falha em uma operação
#   logger.exception()   — igual a error mas captura o traceback automaticamente
#
# Adicione no topo de cada módulo:
#   import logging
#   logger = logging.getLogger(__name__)
# ─────────────────────────────────────────────


# Mapeamento dos prints existentes no projeto para calls de logging
# (referência para migração manual)
PRINTS_PARA_MIGRAR = """
scheduler.py:
  print(f"Atualizando produto {id_produto}: {custo} -> {novo_preco}")
  → logger.info("Produto %s: custo %.2f → novo preço %.2f", id_produto, custo, novo_preco)

  print("Erro:", e)
  → logger.exception("Erro no ciclo do scheduler")

bling_client.py (implícito em exceptions):
  raise BlingAPIError(response.text)
  → logger.error("Bling API erro %s em %s: %s", response.status_code, path, response.text)
    raise BlingAPIError(response.text)

app.py (implícito nos HTTPException):
  → logger.warning("Endpoint %s: %s", request.url.path, detail)
"""
