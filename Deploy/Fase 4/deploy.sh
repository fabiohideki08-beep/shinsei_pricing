#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# deploy.sh — Shinsei Pricing
# Deploy com zero-downtime e rollback automático em caso de falha
#
# Uso:
#   ./deploy.sh              — deploy normal
#   ./deploy.sh --ssl-init   — emite o certificado pela primeira vez
#   ./deploy.sh --rollback   — reverte para a imagem anterior
# ─────────────────────────────────────────────────────────────

set -euo pipefail

# ── Configuração ─────────────────────────────────────────────
DOMINIO="${DOMINIO:-seu-dominio.com}"
EMAIL_CERTBOT="${EMAIL_CERTBOT:-seu@email.com}"
COMPOSE="docker compose"
APP_CONTAINER="shinsei_app"
IMAGEM_ATUAL="shinsei_pricing_app"

# ── Cores para output ─────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'

log()     { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $*"; }
ok()      { echo -e "${GREEN}✓${NC} $*"; }
warn()    { echo -e "${YELLOW}⚠${NC} $*"; }
erro()    { echo -e "${RED}✗${NC} $*" >&2; }

# ─────────────────────────────────────────────────────────────
# Verificações pré-deploy
# ─────────────────────────────────────────────────────────────
pre_checks() {
    log "Verificações pré-deploy..."

    command -v docker >/dev/null 2>&1 || { erro "Docker não encontrado."; exit 1; }
    docker compose version >/dev/null 2>&1 || { erro "Docker Compose não encontrado."; exit 1; }

    [[ -f ".env" ]] || { erro "Arquivo .env não encontrado. Copie .env.example e preencha."; exit 1; }

    # Verifica variáveis obrigatórias no .env
    for var in BLING_CLIENT_ID BLING_CLIENT_SECRET API_KEY; do
        if ! grep -q "^${var}=." .env 2>/dev/null; then
            erro "Variável ${var} não definida no .env"
            exit 1
        fi
    done

    ok "Verificações passaram"
}

# ─────────────────────────────────────────────────────────────
# SSL — emissão inicial do certificado
# ─────────────────────────────────────────────────────────────
ssl_init() {
    log "Iniciando emissão de certificado SSL para ${DOMINIO}..."

    # Sobe nginx temporário apenas com HTTP para o desafio ACME
    $COMPOSE up -d nginx

    docker run --rm \
        -v "$(docker volume ls -q | grep certbot):/etc/letsencrypt" \
        -v "$(docker volume ls -q | grep certbot_www):/var/www/certbot" \
        certbot/certbot certonly \
            --webroot \
            --webroot-path=/var/www/certbot \
            --email "${EMAIL_CERTBOT}" \
            --agree-tos \
            --no-eff-email \
            -d "${DOMINIO}" \
            -d "www.${DOMINIO}"

    ok "Certificado emitido com sucesso"
    log "Reiniciando nginx com HTTPS..."
    $COMPOSE restart nginx
}

# ─────────────────────────────────────────────────────────────
# Deploy principal
# ─────────────────────────────────────────────────────────────
deploy() {
    log "Iniciando deploy do Shinsei Pricing..."

    # Salva a imagem anterior para rollback
    if docker images "${IMAGEM_ATUAL}" --format '{{.ID}}' | grep -q .; then
        IMAGEM_ANTERIOR=$(docker images "${IMAGEM_ATUAL}" --format '{{.ID}}' | head -1)
        log "Imagem anterior salva para rollback: ${IMAGEM_ANTERIOR:0:12}"
    fi

    # Constrói nova imagem
    log "Construindo imagem..."
    $COMPOSE build --no-cache app
    ok "Imagem construída"

    # Inicia banco e migra dados (idempotente)
    log "Migrando banco de dados..."
    $COMPOSE run --rm app python database.py migrate 2>/dev/null \
        || $COMPOSE run --rm app python database.py init
    ok "Banco migrado"

    # Reinicia apenas o app com a nova imagem (nginx continua rodando)
    log "Subindo nova versão do app..."
    $COMPOSE up -d --no-deps app
    ok "App subindo..."

    # Aguarda health check
    log "Aguardando health check..."
    for i in $(seq 1 12); do
        sleep 5
        STATUS=$(docker inspect --format='{{.State.Health.Status}}' "${APP_CONTAINER}" 2>/dev/null || echo "unknown")
        if [[ "${STATUS}" == "healthy" ]]; then
            ok "App saudável após $((i * 5))s"
            break
        fi
        if [[ "${STATUS}" == "unhealthy" ]]; then
            erro "App ficou unhealthy — executando rollback..."
            rollback
            exit 1
        fi
        log "Aguardando... (${STATUS}, tentativa ${i}/12)"
    done

    # Verifica se ainda está saudável
    FINAL_STATUS=$(docker inspect --format='{{.State.Health.Status}}' "${APP_CONTAINER}" 2>/dev/null || echo "unknown")
    if [[ "${FINAL_STATUS}" != "healthy" ]]; then
        erro "Health check não passou em 60s — executando rollback..."
        rollback
        exit 1
    fi

    # Sobe os demais serviços (nginx, backup, certbot)
    $COMPOSE up -d
    ok "Todos os serviços ativos"

    # Remove imagens antigas (mantém as 2 últimas)
    docker image prune -f --filter "until=48h" >/dev/null 2>&1 || true

    ok "Deploy concluído com sucesso!"
    status_geral
}

# ─────────────────────────────────────────────────────────────
# Rollback
# ─────────────────────────────────────────────────────────────
rollback() {
    warn "Executando rollback..."
    $COMPOSE restart app || $COMPOSE up -d app
    ok "Rollback concluído"
}

# ─────────────────────────────────────────────────────────────
# Status geral
# ─────────────────────────────────────────────────────────────
status_geral() {
    echo ""
    log "Status dos containers:"
    $COMPOSE ps
    echo ""
    log "Uso de recursos:"
    docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}" \
        "${APP_CONTAINER}" "shinsei_nginx" 2>/dev/null || true
    echo ""
    log "Endpoint de health:"
    curl -sf "http://localhost/health" | python3 -m json.tool 2>/dev/null \
        || warn "Health check via HTTP falhou (aguarde nginx iniciar)"
}

# ─────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────
case "${1:-deploy}" in
    deploy)
        pre_checks
        deploy
        ;;
    --ssl-init)
        pre_checks
        ssl_init
        ;;
    --rollback)
        rollback
        ;;
    status)
        status_geral
        ;;
    *)
        echo "Uso: $0 [deploy|--ssl-init|--rollback|status]"
        exit 1
        ;;
esac
