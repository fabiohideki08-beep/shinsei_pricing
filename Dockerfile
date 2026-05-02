# ─────────────────────────────────────────────────────────────
# Shinsei Pricing — Dockerfile
# Build multi-stage: dependências separadas do código-fonte
# para imagem final enxuta (~200 MB vs ~600 MB single-stage)
# ─────────────────────────────────────────────────────────────

# ── Stage 1: builder — instala dependências ──────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Instala apenas o necessário para compilar dependências nativas
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copia requirements primeiro (cache de layer — só reinstala se mudar)
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime — imagem final ──────────────────────────
FROM python:3.12-slim AS runtime

# Metadados
LABEL org.opencontainers.image.title="Shinsei Pricing"
LABEL org.opencontainers.image.description="Motor de precificação multicanal"

# Variáveis de ambiente de runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8000

# Cria usuário não-root (segurança)
RUN groupadd -r shinsei && useradd -r -g shinsei -d /app -s /bin/sh shinsei

WORKDIR /app

# Copia dependências instaladas no builder
COPY --from=builder /install /usr/local

# Copia o código-fonte
# (arquivos sensíveis são excluídos pelo .dockerignore)
COPY --chown=shinsei:shinsei . .

# Cria diretórios de dados e logs com permissão correta
RUN mkdir -p data logs \
    && chown -R shinsei:shinsei data logs

# Muda para usuário não-root
USER shinsei

# Expõe a porta da aplicação
EXPOSE $PORT

# Health check — usa o endpoint /health já existente
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request, os; urllib.request.urlopen('http://localhost:' + os.environ.get('PORT','8000') + '/health')" \
    || exit 1

# Comando de inicialização — usa $PORT injetado pelo Railway
CMD ["sh", "-c", "python startup.py && uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-keep-alive 30 --log-level info"]
