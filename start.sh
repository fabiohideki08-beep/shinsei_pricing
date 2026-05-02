#!/bin/sh
# start.sh — Shinsei Pricing entrypoint para Railway
set -e

echo "[start.sh] PORT=${PORT:-8000}"
python startup.py
exec uvicorn app:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 1 \
    --timeout-keep-alive 30 \
    --log-level info
