# Shinsei Pricing — Fase 4: Go-live

Pré-requisito: Fases 1, 2 e 3 aplicadas e testadas localmente.

---

## O que foi entregue

| Arquivo | O que faz |
|---------|-----------|
| `Dockerfile` | Build multi-stage, usuário não-root, health check embutido |
| `.dockerignore` | Exclui credenciais, caches e backups da imagem |
| `docker-compose.yml` | App + Nginx + Certbot (TLS automático) + Backup diário |
| `nginx/nginx.conf` | Config base com gzip, rate limit e performance |
| `nginx/conf.d/shinsei.conf` | Virtual host HTTPS com headers de segurança |
| `monitoring.py` | `/health`, `/ready`, `/metrics` com diagnóstico real |
| `deploy.sh` | Deploy com health check + rollback automático |
| `requirements.txt` | Dependências pinadas para produção |

---

## Passo 1 — Preparar o servidor

```bash
# Ubuntu 22.04 / Debian 12
# Instale Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# Verifique
docker --version && docker compose version
```

---

## Passo 2 — Copiar os arquivos de infraestrutura

```bash
# No diretório raiz do projeto
cp fase4/Dockerfile .
cp fase4/.dockerignore .
cp fase4/docker-compose.yml .
cp fase4/requirements.txt .
cp fase4/monitoring.py .
cp fase4/deploy.sh . && chmod +x deploy.sh

mkdir -p nginx/conf.d
cp fase4/nginx/nginx.conf nginx/nginx.conf
cp fase4/nginx/conf.d/shinsei.conf nginx/conf.d/shinsei.conf
```

---

## Passo 3 — Configurar domínio e nginx

```bash
# Substitua "seu-dominio.com" pelo domínio real em dois arquivos:
sed -i 's/seu-dominio.com/meudominio.com/g' nginx/conf.d/shinsei.conf
sed -i 's/seu-dominio.com/meudominio.com/g' deploy.sh

# Configure o DNS do domínio para apontar para o IP do servidor ANTES de continuar
# Verifique: dig meudominio.com +short
```

---

## Passo 4 — Configurar o .env de produção

```bash
cp .env.example .env
nano .env
```

Variáveis obrigatórias para produção:
```bash
# Bling OAuth (rotacionados na Fase 1)
BLING_CLIENT_ID=...
BLING_CLIENT_SECRET=...
BLING_REDIRECT_URI=https://meudominio.com/bling/callback

# API key
API_KEY=chave-gerada-com-secrets.token_urlsafe

# CORS restrito ao domínio
CORS_ORIGINS=https://meudominio.com

# Banco
DATABASE_URL=sqlite:///data/shinsei.db

# Scheduler
SCHEDULER_INTERVALO=300
SCHEDULER_ATIVO=true

# Logging
LOG_LEVEL=INFO
```

---

## Passo 5 — Integrar o monitoring.py no app.py

```python
# No app.py, após os imports existentes:
from monitoring import router as monitoring_router
app.include_router(monitoring_router)

# Adicione /health, /ready e /metrics à lista de rotas públicas no auth.py:
# PUBLIC_PATHS (já contém /health — adicione /ready)
```

---

## Passo 6 — Emitir o certificado SSL (primeira vez)

```bash
# Garante que o DNS está propagado antes de continuar
dig meudominio.com +short  # deve mostrar o IP do servidor

# Emite o certificado
DOMINIO=meudominio.com EMAIL_CERTBOT=seu@email.com ./deploy.sh --ssl-init
```

---

## Passo 7 — Deploy

```bash
DOMINIO=meudominio.com ./deploy.sh
```

O script vai:
1. Verificar o `.env` e variáveis obrigatórias
2. Construir a imagem Docker
3. Migrar o banco de dados
4. Subir o novo container com health check
5. Fazer rollback automático se o health check falhar
6. Subir nginx, certbot e backup

---

## Passo 8 — Verificar

```bash
# Todos os containers
docker compose ps

# Health check via HTTPS
curl -sf https://meudominio.com/health | python3 -m json.tool

# Métricas (requer API key)
curl -sf -H "X-API-Key: SUA_CHAVE" https://meudominio.com/metrics | python3 -m json.tool

# Logs em tempo real
docker compose logs -f app

# Logs de erro
docker compose logs app | grep -i error
```

---

## Deploys futuros

Após o SSL estar configurado, todo novo deploy é:

```bash
git pull
./deploy.sh
```

O script detecta a imagem anterior e faz rollback automático se algo der errado.

---

## Monitoramento

### Health check simples (para uptime monitors como UptimeRobot)
```
URL: https://meudominio.com/health
Método: GET
Status esperado: 200
Intervalo: 5 minutos
```

### Verificar o banco de dados
```bash
docker compose exec app python database.py stats
```

### Ver backups automáticos
```bash
docker volume inspect shinsei_backups
docker run --rm -v shinsei_backups:/backups alpine ls -lah /backups
```

### Restaurar um backup
```bash
# Pare o app primeiro
docker compose stop app

# Restaure o backup
docker run --rm \
  -v shinsei_data:/data \
  -v shinsei_backups:/backups \
  alpine cp /backups/shinsei_20260402_030000.db /data/shinsei.db

# Reinicie
docker compose start app
```

---

## Renovação de tokens OAuth após o deploy

Após o deploy, os tokens do Bling e ML não estão no container (foram excluídos pelo `.dockerignore`). Faça o fluxo OAuth novamente:

```bash
# Bling: acesse pelo navegador
https://meudominio.com/bling/auth

# ML: acesse pelo navegador
https://meudominio.com/ml/login
```

Os tokens serão salvos no volume `shinsei_data` e persistirão entre deploys.

---

## Checklist final de go-live

| Item | Verificação |
|------|-------------|
| DNS propagado | `dig meudominio.com +short` → IP do servidor |
| HTTPS funcionando | `curl -sf https://meudominio.com/health` → 200 |
| HTTP redireciona | `curl -I http://meudominio.com` → 301 |
| Auth funcionando | `curl https://meudominio.com/fila/lista` → 401 |
| OAuth Bling | `https://meudominio.com/bling/status` → conectado |
| Backup ativo | `docker compose ps backup` → Up |
| Logs limpos | `docker compose logs app \| grep -i error` → vazio |
| CORS restrito | `CORS_ORIGINS` no .env sem `*` |
