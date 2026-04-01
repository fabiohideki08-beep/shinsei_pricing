SHINSEI PRICING — PACOTE COMPLETO DE SOBREPOSIÇÃO

Arquivos incluídos:
- app.py
- bling_client.py
- requirements.txt
- pages/simulador.html
- pages/fila.html
- data/config.json
- data/fila_aprovacao.json
- data/regras.json

O que este pacote já entrega:
- rota /simulador
- rota /fila
- rota /integracao/preview
- rota /bling/debug/sku
- rota /bling/produto/buscar
- rota /fila/limpar-invalidos
- rota /fila/reset-total
- diagnóstico inteligente
- fila automática
- proteção contra duplicados pendentes
- debug do campo Código (SKU) no Bling

Dependências externas que DEVEM continuar no seu projeto:
- pricing_engine_real.py ou pricing_engine.py com a função montar_precificacao_bling()
- bling_update_engine.py com a função aplicar_precos_multicanal()

Aplicação:
1. Faça backup do projeto atual.
2. Substitua os arquivos do pacote nas mesmas pastas.
3. Instale dependências:
   pip install -r requirements.txt
4. Rode:
   python -m uvicorn app:app --reload
