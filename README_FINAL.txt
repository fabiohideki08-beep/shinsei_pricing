SHINSEI PRICING — VERSÃO FINAL ESTRUTURADA

Arquivos principais:
- app.py
- bling_client.py
- pricing_engine.py
- cost_engine.py
- cost_allocation_engine.py
- product_intelligence.py
- operational_efficiency.py
- dashboard_blueprint.py
- index.html
- dashboards_premium.html

Pastas:
- data/
  - automation_config.json
  - allocation_sample.json
  - fila_aprovacao.json
- logs/
  - criados automaticamente conforme o uso

Como subir:
1. instale dependências:
   pip install fastapi uvicorn python-dotenv requests
2. mantenha o .env atual
3. rode:
   python -m uvicorn app:app --reload

Fluxo principal:
- /bling/pre-visualizar-produto
- /bling/precificar-produto
- /webhook/bling
- /fila
- /dashboards

Observações importantes:
- a integração com o Bling foi mantida no desenho atual
- o método atualizar_preco foi incluído no bling_client.py, mas o endpoint do Bling pode variar conforme a conta; se a conta usar outro payload/endpoint, ajuste apenas esse método
- os dashboards estão estruturados e prontos para receber dados reais progressivamente
- o rateio estrutural usa data/allocation_sample.json; esse arquivo deve ser alimentado com os dados reais do período/estoque
