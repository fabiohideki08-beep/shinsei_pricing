# Shinsei Pricing — Fase 1: Estabilização

Guia de aplicação das mudanças. Execute na ordem abaixo.

---

## Passo 1 — Segurança: remover credenciais do repositório

```bash
# 1. Substitua o .gitignore pelo novo
cp fase1/.gitignore .gitignore

# 2. Remova os arquivos sensíveis do rastreamento git (não apaga do disco)
git rm --cached .env
git rm --cached data/bling_tokens.json data/bling_oauth_state.json
git rm --cached data/ml_tokens.json data/ml_oauth_state.json 2>/dev/null || true
git rm --cached bling_token.json 2>/dev/null || true

# 3. Commit da remoção
git add .gitignore
git commit -m "segurança: remover credenciais do rastreamento git"

# 4. IMPORTANTE: rotacione os tokens no painel do Bling e do Mercado Livre
#    Os tokens que estavam no repositório devem ser considerados comprometidos.
#    Gere novos client_secret e refaça o fluxo OAuth.
```

> ⚠️ Se o repositório é público, o histórico git ainda contém os tokens.
> Execute: `git filter-branch` ou use a ferramenta BFG Repo Cleaner para purgar o histórico.

---

## Passo 2 — Configurar o .env corretamente

```bash
# Copie o template e preencha com os novos tokens rotacionados
cp fase1/.env.example .env
nano .env   # ou code .env
```

Campos obrigatórios:
- `BLING_CLIENT_ID`, `BLING_CLIENT_SECRET`, `BLING_REDIRECT_URI`
- `SCHEDULER_INTERVALO` — sugestão: 300 (5 minutos)

---

## Passo 3 — Instalar dependências

```bash
pip install -r fase1/requirements.txt
```

---

## Passo 4 — Instalar o database.py

```bash
cp fase1/database.py .

# Inicializar o banco e migrar os JSONs existentes
python database.py migrate
```

Saída esperada:
```
INFO Banco de dados inicializado em data/shinsei.db
INFO Migradas 847 regras de data/regras.json
INFO Migrados 23 itens da fila de data/fila_aprovacao.json
INFO Config migrada de data/config.json
Migração concluída: {'regras': 847, 'fila': 23, 'config': True}
```

Verifique:
```bash
python database.py stats
# Fila: {'pendente': 18, 'aprovado': 5, 'rejeitado': 0, 'total': 23}
# Regras: 847
```

---

## Passo 5 — Instalar o scheduler.py corrigido

```bash
cp fase1/scheduler.py .
```

Teste isolado (sem o FastAPI):
```bash
python scheduler.py
# Deve logar: "Bling não autenticado" se ainda não autenticou,
# ou iniciar o ciclo normalmente se já tiver tokens válidos.
# Ctrl+C para parar.
```

---

## Passo 6 — Aplicar o patch no app.py

O arquivo `PATCH_fase1_app.py` documenta cada alteração necessária no `app.py`.
As mudanças são cirúrgicas — você pode aplicar uma de cada vez.

**Alterações obrigatórias:**

1. Adicionar imports do `database.py` e `scheduler`
2. Adicionar eventos `@app.on_event("startup")` e `shutdown`
3. Substituir `carregar_regras`, `carregar_fila`, `salvar_fila`
4. Atualizar endpoints `/fila/lista`, `/fila/adicionar`, `/fila/aprovar`, `/fila/rejeitar`
5. Atualizar endpoints `/regras/listar`, `/regras/adicionar`, `/regras/editar`, `/regras/excluir`
6. Ajustar o trecho de salvar fila dentro de `/integracao/preview`

Veja os blocos completos em `PATCH_fase1_app.py`.

```bash
cp fase1/PATCH_fase1_app.py .
```

---

## Passo 7 — Testar

```bash
# Inicia o servidor
python -m uvicorn app:app --reload

# Em outro terminal — testa os endpoints principais
curl http://localhost:8000/health
curl http://localhost:8000/regras/listar | python -m json.tool | head -20
curl http://localhost:8000/fila/lista | python -m json.tool | head -20
```

---

## Passo 8 — Consolidar o frontend (limpeza de versões)

Após confirmar que tudo funciona:

```bash
# index_real.html é o canônico (33 KB, mais completo)
# Remova as versões antigas
git rm index_v3.html index_v4.html "index_v5.html.html" index_backup.html index_final_backup.html 2>/dev/null || true
git rm app_old.py app_mock_backup.py app_v4_backup.py 2>/dev/null || true
git rm "modulo_regras_precificacao.py.py" 2>/dev/null || true
git rm pages/regras_backup.html pages/simulador_backup.html "pages/fila - Copia.html" 2>/dev/null || true

git commit -m "limpeza: remover versões antigas de frontend e backups"
```

---

## Verificação final

| Item | Como verificar |
|------|----------------|
| Credenciais fora do git | `git log --all --oneline -- .env` deve ter apenas commits antigos |
| SQLite funcionando | `python database.py stats` retorna contagens corretas |
| Scheduler rodando | Log do uvicorn mostra "Scheduler iniciado" no startup |
| Fila via banco | `GET /fila/lista` retorna itens sem ler JSON do disco |
| Regras via banco | `GET /regras/listar` retorna regras sem ler JSON do disco |

---

## Rollback

Se algo der errado, o banco SQLite e os JSONs coexistem:
- O `database.py` tem fallback para JSON em todas as funções
- Os JSONs originais não são apagados pela migração
- Para reverter: comente os imports do `database.py` no `app.py`
