# Shinsei Pricing — Fase 3: Expansão da API

Pré-requisito: Fases 1 e 2 aplicadas.

---

## O que foi entregue

| Arquivo | O que faz |
|---------|-----------|
| `routes/batch.py` | Endpoint `POST /bling/precificar-lote` — precifica até 200 SKUs de uma vez |
| `routes/ml_unificado.py` | Integração ML conectada ao motor — calcula e aplica em um passo |
| `services/shopee.py` | Client Shopee Open Platform API v2 com OAuth + atualização de preços |
| `services/amazon.py` | Client Amazon SP-API com LWA + atualização de preços |

---

## Passo 1 — Copiar os arquivos

```bash
cp fase3/routes/batch.py routes/batch.py
cp fase3/routes/ml_unificado.py routes/ml_unificado.py
cp fase3/services/shopee.py services/shopee.py
cp fase3/services/amazon.py services/amazon.py
```

---

## Passo 2 — Aplicar o patch no app.py

Veja `PATCH_fase3_app.py`. O essencial:

```python
# Logo após "app = FastAPI(title="Shinsei Pricing")"
from routes.batch import router as batch_router
from routes.ml_unificado import router as ml_router

app.include_router(batch_router)
app.include_router(ml_router)
```

---

## Passo 3 — Endpoint de lote

### Testar com dados reais

```bash
# Precifica 3 SKUs e enfileira automaticamente
curl -X POST \
  -H "X-API-Key: SUA_CHAVE" \
  -H "Content-Type: application/json" \
  -d '{
    "skus": ["7897042019328", "7896007816344", "7891234567890"],
    "imposto": 4.0,
    "objetivo": "lucro_liquido",
    "tipo_alvo": "percentual",
    "valor_alvo": 30.0,
    "arredondamento": "90",
    "enfileirar": true
  }' \
  http://localhost:8000/bling/precificar-lote | python -m json.tool
```

Resposta esperada:
```json
{
  "total": 3,
  "sucesso": 2,
  "erro": 1,
  "enfileirados": 2,
  "duracao_segundos": 3.8,
  "resultados": [
    {"sku": "7897042019328", "ok": true, "melhor_canal": "Shopee", ...},
    {"sku": "7896007816344", "ok": true, "melhor_canal": "Amazon", ...},
    {"sku": "7891234567890", "ok": false, "erro": "Produto sem custo no Bling."}
  ]
}
```

### Limite e rate limit

O endpoint aceita até **200 SKUs por chamada**. Para catálogos maiores, pagine:
```python
import requests, math

skus = [...]  # todos os SKUs
batch_size = 100
headers = {"X-API-Key": "SUA_CHAVE", "Content-Type": "application/json"}

for i in range(0, len(skus), batch_size):
    lote = skus[i:i+batch_size]
    r = requests.post("http://localhost:8000/bling/precificar-lote",
                      json={"skus": lote, "enfileirar": True},
                      headers=headers)
    print(f"Lote {i//batch_size + 1}: {r.json()['sucesso']} OK, {r.json()['erro']} erros")
```

---

## Passo 4 — ML Unificado

### Calcular e enfileirar (recomendado)

```bash
curl -X POST \
  -H "X-API-Key: SUA_CHAVE" \
  -H "Content-Type: application/json" \
  -d '{
    "sku": "7897042019328",
    "item_id_classico": "MLB123456789",
    "item_id_premium": "MLB987654321",
    "valor_alvo": 30.0,
    "aplicar_imediatamente": false
  }' \
  http://localhost:8000/ml/precificar-e-aplicar
```

### Aplicar direto (sem aprovação manual)

```bash
curl -X POST \
  -H "X-API-Key: SUA_CHAVE" \
  -H "Content-Type: application/json" \
  -d '{
    "sku": "7897042019328",
    "item_id_classico": "MLB123456789",
    "aplicar_imediatamente": true
  }' \
  http://localhost:8000/ml/precificar-e-aplicar
```

### Aplicar item já aprovado da fila

```bash
# Após aprovar um item na fila que veio do ML
curl -X POST \
  -H "X-API-Key: SUA_CHAVE" \
  http://localhost:8000/ml/aplicar-fila/UUID-DO-ITEM-NA-FILA
```

---

## Passo 5 — Configurar Shopee (quando necessário)

```bash
# No .env:
SHOPEE_PARTNER_ID=seu_partner_id
SHOPEE_PARTNER_KEY=seu_partner_key
SHOPEE_SHOP_ID=seu_shop_id
```

Acesse o Shopee Open Platform para obter as credenciais:
https://open.shopee.com/developer-guide/20

A Shopee exige que você adicione um endpoint de callback OAuth no painel.
O callback URL deve ser `https://seu-dominio.com/shopee/callback`.

Para adicionar o router de OAuth da Shopee ao app.py (se quiser OAuth interativo):
```python
# routes/shopee_oauth.py — crie se precisar do fluxo OAuth completo
# O services/shopee.py já inclui ShopeeOAuthService com os métodos necessários
```

---

## Passo 6 — Configurar Amazon SP-API (quando necessário)

O processo da Amazon exige um cadastro como Developer no Seller Central:

1. Acesse Seller Central → Apps & Services → Develop Apps
2. Crie um app e obtenha Client ID e Client Secret
3. Autorize o app na sua conta e copie o `refresh_token` gerado
4. Adicione ao `.env`:

```bash
AMAZON_CLIENT_ID=amzn1.application-oa2-client.xxxxx
AMAZON_CLIENT_SECRET=xxxxx
AMAZON_REFRESH_TOKEN=Atzr|xxxxx
AMAZON_SELLER_ID=A1XXXXXXXXXXXXX
AMAZON_MARKETPLACE_ID=A2Q3Y263D00KWC   # Brasil
```

**Atenção:** Na Amazon, o `sku` para atualização de preço é o SKU do anúncio na Amazon, não o EAN/código do Bling. Você precisará de um mapeamento SKU Bling → SKU Amazon.

---

## Novos endpoints disponíveis

```
POST /bling/precificar-lote         — lote de SKUs (até 200)
GET  /bling/precificar-lote/status  — resumo da fila

POST /ml/precificar-e-aplicar       — calcula + aplica no ML
POST /ml/aplicar-fila/{item_id}     — aplica item aprovado no ML
GET  /ml/status-completo            — auth ML + stats da fila
```

Ver todos em: http://localhost:8000/docs

---

## Verificação final

| Item | Como verificar |
|------|----------------|
| Batch funcionando | `POST /bling/precificar-lote` com SKUs reais |
| ML unificado | `GET /ml/status-completo` retorna auth + fila |
| Routers registrados | `/docs` mostra todos os endpoints novos |
| Shopee config | `python -c "from services.shopee import _config_ok; print(_config_ok())"` |
| Amazon config | `python -c "from services.amazon import _config_ok; print(_config_ok())"` |
