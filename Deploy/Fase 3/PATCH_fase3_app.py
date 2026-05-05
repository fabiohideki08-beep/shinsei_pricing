"""
PATCH_fase3_app.py
──────────────────
Alterações no app.py para integrar os novos routers da Fase 3.
"""

# ══════════════════════════════════════════════════════════════
# ALTERAÇÃO 1 — Registrar os novos routers
# Adicione logo após "app = FastAPI(title="Shinsei Pricing")"
# e antes do middleware CORS
# ══════════════════════════════════════════════════════════════

REGISTRAR_ROUTERS = '''
from routes.batch import router as batch_router
from routes.ml_unificado import router as ml_router

app.include_router(batch_router)
app.include_router(ml_router)
'''

# ══════════════════════════════════════════════════════════════
# ALTERAÇÃO 2 — Endpoint /fila/aprovar melhorado
# Substitua o @app.post("/fila/aprovar/{item_id}") atual por este,
# que detecta automaticamente o canal e chama o serviço correto.
# ══════════════════════════════════════════════════════════════

ENDPOINT_APROVAR_MULTICANAL = '''
@app.post("/fila/aprovar/{item_id}")
def fila_aprovar(item_id: str, payload: dict = Body(default={})):
    """
    Aprova um item da fila. Se o item tiver item_id_classico/premium no payload,
    tenta aplicar automaticamente no ML. Para outros canais, usa bling_update_engine.
    """
    item = buscar_item_fila(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado na fila.")
    if item.get("status") != "pendente":
        raise HTTPException(status_code=400, detail=f"Item já com status '{item['status']}'.")

    payload_orig = item.get("payload_original") or {}
    resultado = None

    # Detecta se tem IDs do ML para aplicação direta
    tem_ml = payload_orig.get("item_id_classico") or payload_orig.get("item_id_premium")

    if tem_ml:
        # Delega para o serviço ML unificado
        import importlib
        try:
            ml_mod = importlib.import_module("routes.ml_unificado")
            # Reutiliza a função interna sem fazer HTTP
            from pydantic import BaseModel as _BM
            from routes.ml_unificado import ml_aplicar_fila
            resultado = dict(ml_aplicar_fila(item_id))
            status_novo = "aprovado" if resultado.get("ok") else "erro_aplicacao"
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Falha ao aplicar no ML: {e}")
    elif aplicar_precos_multicanal:
        # Usa o bling_update_engine para Bling + outros canais
        try:
            resultado = aplicar_precos_multicanal(item)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Falha ao aplicar preços: {e}")
        status_novo = "aprovado"
    else:
        status_novo = "aprovado"

    atualizar_status_fila(item_id, status_novo, resultado=resultado)
    _append_jsonl(LOG_PATH, {
        "evento": "aprovado", "item_id": item_id,
        "sku": item.get("sku"), "quando": datetime.now().isoformat()
    })
    return {"ok": True, "item_id": item_id, "status": status_novo,
            "resultado": resultado, "stats": stats_fila()}
'''

# ══════════════════════════════════════════════════════════════
# ALTERAÇÃO 3 — Adicionar variáveis de ambiente no .env
# ══════════════════════════════════════════════════════════════

ENV_NOVOS_CANAIS = """
# ── Shopee (Fase 3) ───────────────────────────────────────────
SHOPEE_PARTNER_ID=seu_partner_id
SHOPEE_PARTNER_KEY=seu_partner_key
SHOPEE_SHOP_ID=seu_shop_id

# ── Amazon SP-API (Fase 3) ────────────────────────────────────
AMAZON_CLIENT_ID=seu_client_id
AMAZON_CLIENT_SECRET=seu_client_secret
AMAZON_REFRESH_TOKEN=seu_refresh_token
AMAZON_SELLER_ID=seu_seller_id
AMAZON_MARKETPLACE_ID=A2Q3Y263D00KWC
"""

# ══════════════════════════════════════════════════════════════
# CHECKLIST DE APLICAÇÃO
# ══════════════════════════════════════════════════════════════

CHECKLIST = """
1. Copiar os novos arquivos:
   cp fase3/routes/batch.py routes/batch.py
   cp fase3/routes/ml_unificado.py routes/ml_unificado.py
   cp fase3/services/shopee.py services/shopee.py
   cp fase3/services/amazon.py services/amazon.py

2. Aplicar ALTERAÇÃO 1 no app.py (registrar routers)

3. Aplicar ALTERAÇÃO 2 no app.py (endpoint /fila/aprovar melhorado)

4. Adicionar variáveis dos novos canais ao .env

5. Testar o endpoint de batch:
   curl -X POST -H "X-API-Key: SUA_CHAVE" \\
        -H "Content-Type: application/json" \\
        -d '{"skus": ["SKU1", "SKU2"], "enfileirar": true}' \\
        http://localhost:8000/bling/precificar-lote

6. Verificar os novos endpoints no Swagger:
   http://localhost:8000/docs
"""

if __name__ == "__main__":
    print(CHECKLIST)
