#!/bin/sh
# docker-entrypoint.sh
# Roda como root para corrigir permissões do volume antes de iniciar o app.
# O volume Railway é montado como root — sem isso, o usuário "shinsei" não
# consegue escrever em /app/data.

set -e

# Garante que /app/data e /app/logs pertencem ao usuário da aplicação
chown -R shinsei:shinsei /app/data /app/logs 2>/dev/null || true

# Executa start.sh como o usuário shinsei (sem abrir um shell de login)
exec su -s /bin/sh shinsei -c "sh /app/start.sh"
