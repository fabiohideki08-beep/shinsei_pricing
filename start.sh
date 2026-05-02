#!/bin/sh
# start.sh — Shinsei Pricing entrypoint para Railway
set -e

echo "[start.sh] PORT=$PORT"
echo "[start.sh] Executando startup.py..."
python startup.py

echo "[start.sh] Testando import do app..."
python -u -c "import app; print('[start.sh] APP_IMPORT_OK')"

echo "[start.sh] Iniciando uvicorn na porta $PORT..."
exec uvicorn app:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 1 \
    --timeout-keep-alive 30 \
    --log-level info
