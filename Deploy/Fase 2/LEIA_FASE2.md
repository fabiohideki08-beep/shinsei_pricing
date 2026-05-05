# Shinsei Pricing — Fase 2: Consolidação

Pré-requisito: Fase 1 aplicada (database.py rodando, tokens fora do git).

---

## Passo 1 — Instalar dependências de teste

```bash
pip install pytest pytest-cov
```

---

## Passo 2 — Instalar os novos módulos

```bash
cp fase2/auth.py .
cp fase2/logging_config.py .
cp fase2/test_pricing_engine.py .
```

---

## Passo 3 — Rodar os testes

```bash
# Roda todos os testes com detalhes
pytest test_pricing_engine.py -v

# Com cobertura de código
pytest test_pricing_engine.py -v --cov=pricing_engine_real --cov-report=term-missing

# Filtra apenas um grupo
pytest test_pricing_engine.py -k "margem" -v
pytest test_pricing_engine.py -k "Fluxo" -v
```

Saída esperada:
```
test_pricing_engine.py::TestHelpers::test_safe_float_numero_normal PASSED
test_pricing_engine.py::TestHelpers::test_safe_float_string_virgula PASSED
...
test_pricing_engine.py::TestFluxoCompleto::test_fluxo_produto_simples PASSED
========================= 40 passed in 0.82s =========================
```

Se algum teste falhar, o output indica exatamente qual cálculo está errado
antes de você descobrir em produção.

---

## Passo 4 — Configurar a API key no .env

```bash
# Gera uma chave segura
python -c "import secrets; print(secrets.token_urlsafe(32))"
# Exemplo: xK7mN2pQs9vY4hR1wE6jT0cA3bF8dL5g

# Adicione ao .env
echo 'API_KEY=xK7mN2pQs9vY4hR1wE6jT0cA3bF8dL5g' >> .env
echo 'API_KEY_HABILITADO=true' >> .env
```

---

## Passo 5 — Aplicar o patch no app.py

Veja `PATCH_fase2_app.py` para os blocos completos. Resumo:

```python
# 1. No topo do app.py, após os imports existentes:
import logging
from fastapi import Request
from logging_config import configurar_logging
from auth import verificar_api_key

configurar_logging()
logger = logging.getLogger(__name__)

# 2. Após app.add_middleware(CORSMiddleware, ...):
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    return await verificar_api_key(request, call_next)
```

---

## Passo 6 — Testar a autenticação

```bash
python -m uvicorn app:app --reload

# Sem chave — deve retornar 401
curl -s http://localhost:8000/fila/lista | python -m json.tool
# {"detail": "Autenticação obrigatória. Envie o header X-API-Key."}

# Com chave correta — deve funcionar
curl -s -H "X-API-Key: SUA_CHAVE" http://localhost:8000/fila/lista | python -m json.tool

# Health check — deve funcionar sem chave
curl -s http://localhost:8000/health | python -m json.tool
# {"status": "ok", "db": "sqlite", ...}

# Bling OAuth callback — deve funcionar sem chave (fluxo OAuth)
curl -s http://localhost:8000/bling/auth
```

---

## Passo 7 — Verificar os logs

```bash
# Após reiniciar o servidor, verifique:
cat logs/app.log

# Erros ficam separados:
cat logs/erros.log
```

---

## Como usar a API key no frontend

No `index_real.html`, adicione o header em todas as chamadas fetch:

```javascript
// Adicione uma variável global no início do script
const API_KEY = localStorage.getItem('shinsei_api_key') || '';

// Em cada fetch, adicione o header:
const response = await fetch('/integracao/preview', {
    method: 'POST',
    headers: {
        'Content-Type': 'application/json',
        'X-API-Key': API_KEY,
    },
    body: JSON.stringify(payload),
});
```

Para o usuário configurar a chave, adicione um campo de configuração na interface:
```javascript
// Salva a chave no localStorage (local ao navegador)
localStorage.setItem('shinsei_api_key', document.getElementById('api-key-input').value);
```

---

## Verificação final

| Item | Como verificar |
|------|----------------|
| Testes passando | `pytest test_pricing_engine.py -v` — todos PASSED |
| Logging ativo | `cat logs/app.log` após rodar o servidor |
| Auth funcionando | curl sem header → 401, com header → 200 |
| Health público | `curl /health` → 200 sem header |
| OAuth público | `curl /bling/auth` → redirect sem header |
