"""
PATCH_fase1_app.py
──────────────────
Este arquivo documenta todas as alterações necessárias no app.py
para integrar o database.py e o scheduler corrigido.

Aplique cada bloco de substituição no app.py original.
As alterações estão agrupadas por seção.
"""

# ══════════════════════════════════════════════════════════════
# ALTERAÇÃO 1 — Imports adicionais no topo do app.py
# Adicione logo após os imports existentes (após "from pydantic import BaseModel")
# ══════════════════════════════════════════════════════════════

IMPORTS_ADICIONAIS = '''
# ── Fase 1: banco de dados e scheduler ──
from database import (
    init_db, listar_regras as db_listar_regras,
    inserir_regra, atualizar_regra, excluir_regra,
    substituir_todas_regras,
    listar_fila as db_listar_fila,
    buscar_item_fila, inserir_item_fila, atualizar_status_fila,
    stats_fila, limpar_invalidos_fila, reset_fila,
    ja_existe_pendente, get_config as db_get_config, set_config as db_set_config,
    migrar_json_legado,
)
from scheduler import iniciar_scheduler_background, parar_scheduler
'''

# ══════════════════════════════════════════════════════════════
# ALTERAÇÃO 2 — Startup e Shutdown do FastAPI
# Substitua o bloco após "app.add_middleware(...)" por este:
# ══════════════════════════════════════════════════════════════

STARTUP_SHUTDOWN = '''
@app.on_event("startup")
def startup():
    # Inicializa banco de dados
    init_db()
    # Migra JSONs legados na primeira execução (seguro para re-executar)
    migrar_json_legado()
    # Inicia scheduler em background
    iniciar_scheduler_background()

@app.on_event("shutdown")
def shutdown():
    parar_scheduler()
'''

# ══════════════════════════════════════════════════════════════
# ALTERAÇÃO 3 — Substituir funções carregar_regras / salvar
# Substitua as funções carregar_regras, carregar_fila, salvar_fila por:
# ══════════════════════════════════════════════════════════════

FUNCOES_DADOS = '''
def carregar_regras(apenas_ativas: bool = False) -> list[dict]:
    """Carrega regras do banco SQLite (com fallback para JSON legado)."""
    try:
        return db_listar_regras(apenas_ativas=apenas_ativas)
    except Exception:
        # Fallback para JSON se o banco ainda não foi migrado
        regras = _load_json(REGRAS_PATH, [])
        if not isinstance(regras, list):
            return []
        for r in regras:
            if isinstance(r, dict):
                r.setdefault("ativo", True)
        return [r for r in regras if isinstance(r, dict) and (r.get("ativo", True) or not apenas_ativas)]


def carregar_fila() -> list[dict]:
    """Carrega fila do banco SQLite."""
    try:
        return db_listar_fila()
    except Exception:
        itens = _load_json(FILA_PATH, [])
        return itens if isinstance(itens, list) else []


def salvar_fila(itens: list[dict]) -> None:
    """
    Compatibilidade: o app.py ainda chama salvar_fila em alguns pontos.
    Com SQLite, cada operação é atômica — esta função sincroniza a lista
    completa (usado apenas em operações de reset/limpeza).
    """
    try:
        reset_fila()
        for item in itens:
            inserir_item_fila(item)
    except Exception:
        # Fallback para JSON
        _save_json(FILA_PATH, itens)
'''

# ══════════════════════════════════════════════════════════════
# ALTERAÇÃO 4 — Endpoint /fila/lista — usar banco
# Substitua o endpoint @app.get("/fila/lista") por:
# ══════════════════════════════════════════════════════════════

ENDPOINT_FILA_LISTA = '''
@app.get("/fila/lista")
def fila_lista(status: str = None, limit: int = 200):
    try:
        itens = db_listar_fila(status=status, limit=limit)
        return {"itens": itens, "stats": stats_fila(), "total": len(itens)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao carregar fila: {e}")
'''

# ══════════════════════════════════════════════════════════════
# ALTERAÇÃO 5 — Endpoint /fila/adicionar — usar banco
# ══════════════════════════════════════════════════════════════

ENDPOINT_FILA_ADICIONAR = '''
@app.post("/fila/adicionar")
def fila_adicionar(payload: dict = Body(...)):
    item = payload
    if not item.get("id"):
        item["id"] = str(uuid.uuid4())
    if not item.get("criado_em"):
        item["criado_em"] = datetime.now().isoformat()
    item.setdefault("atualizado_em", item["criado_em"])
    item.setdefault("status", "pendente")
    try:
        inserir_item_fila(item)
        return {"ok": True, "item_id": item["id"], "stats": stats_fila()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao adicionar na fila: {e}")
'''

# ══════════════════════════════════════════════════════════════
# ALTERAÇÃO 6 — Endpoint /fila/aprovar/{item_id} — usar banco
# ══════════════════════════════════════════════════════════════

ENDPOINT_APROVAR = '''
@app.post("/fila/aprovar/{item_id}")
def fila_aprovar(item_id: str, payload: dict = Body(default={})):
    item = buscar_item_fila(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado na fila.")
    if item.get("status") != "pendente":
        raise HTTPException(status_code=400, detail=f"Item já está com status '{item['status']}'.")

    resultado = None
    # Tenta aplicar os preços se aplicar_precos_multicanal estiver disponível
    if aplicar_precos_multicanal:
        try:
            resultado = aplicar_precos_multicanal(item)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Falha ao aplicar preços: {e}")

    ok = atualizar_status_fila(item_id, "aprovado", resultado=resultado)
    if not ok:
        raise HTTPException(status_code=500, detail="Falha ao atualizar status no banco.")

    _append_jsonl(LOG_PATH, {
        "evento": "aprovado", "item_id": item_id,
        "sku": item.get("sku"), "quando": datetime.now().isoformat()
    })
    return {"ok": True, "item_id": item_id, "resultado": resultado, "stats": stats_fila()}
'''

# ══════════════════════════════════════════════════════════════
# ALTERAÇÃO 7 — Endpoint /fila/rejeitar/{item_id} — usar banco
# ══════════════════════════════════════════════════════════════

ENDPOINT_REJEITAR = '''
@app.post("/fila/rejeitar/{item_id}")
def fila_rejeitar(item_id: str, payload: dict = Body(default={})):
    item = buscar_item_fila(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item não encontrado na fila.")

    motivo = payload.get("motivo", "Rejeitado manualmente.")
    ok = atualizar_status_fila(item_id, "rejeitado", resultado={"motivo": motivo})
    if not ok:
        raise HTTPException(status_code=500, detail="Falha ao atualizar status no banco.")

    _append_jsonl(LOG_PATH, {
        "evento": "rejeitado", "item_id": item_id,
        "sku": item.get("sku"), "motivo": motivo,
        "quando": datetime.now().isoformat()
    })
    return {"ok": True, "item_id": item_id, "stats": stats_fila()}
'''

# ══════════════════════════════════════════════════════════════
# ALTERAÇÃO 8 — Endpoints de regras — usar banco
# Substitua /regras/listar, /regras/adicionar, /regras/editar,
# /regras/excluir, /regras/importar-excel
# ══════════════════════════════════════════════════════════════

ENDPOINTS_REGRAS = '''
@app.get("/regras/listar")
def regras_listar(apenas_ativas: bool = False):
    regras = db_listar_regras(apenas_ativas=apenas_ativas)
    return {"regras": regras, "total": len(regras)}

@app.post("/regras/adicionar")
def regras_adicionar(regra: dict = Body(...)):
    regra.setdefault("ativo", True)
    novo_id = inserir_regra(regra)
    return {"ok": True, "id": novo_id, "total": len(db_listar_regras())}

@app.post("/regras/editar/{idx}")
def regras_editar(idx: int, regra: dict = Body(...)):
    ok = atualizar_regra(idx, regra)
    if not ok:
        raise HTTPException(status_code=404, detail="Regra não encontrada.")
    return {"ok": True, "id": idx}

@app.delete("/regras/excluir/{idx}")
def regras_excluir(idx: int):
    ok = excluir_regra(idx)
    if not ok:
        raise HTTPException(status_code=404, detail="Regra não encontrada.")
    return {"ok": True, "total": len(db_listar_regras())}

@app.post("/regras/importar-excel")
async def regras_importar_excel(file: UploadFile = File(...)):
    # Mesma lógica de parsing do Excel — apenas a persistência muda
    # (chama substituir_todas_regras ao invés de _save_json)
    import importar_regras_excel as imp_mod
    conteudo = await file.read()
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(conteudo)
        tmp_path = tmp.name
    try:
        regras_novas = imp_mod.importar(tmp_path)
        total = substituir_todas_regras(regras_novas)
        return {"ok": True, "importadas": total}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Erro ao importar: {e}")
    finally:
        os.unlink(tmp_path)
'''

# ══════════════════════════════════════════════════════════════
# NOTA SOBRE O integracao/preview
# Não precisa mudar a lógica — só a parte que salva na fila.
# Localize o trecho:
#   itens_fila = carregar_fila()
#   ...
#   itens_fila.insert(0, item); salvar_fila(itens_fila)
#
# E substitua por:
#   if ja_existe_pendente(sku):
#       fila_auto = {"adicionado": False, "motivo": "Já existe item pendente."}
#   else:
#       item = _montar_item_fila(preview, payload.dict())
#       inserir_item_fila(item)
#       fila_auto = {"adicionado": True, "item_id": item["id"]}
# ══════════════════════════════════════════════════════════════

NOTA_PREVIEW = """
No endpoint /integracao/preview, localize o bloco que salva na fila
e substitua carregar_fila() + salvar_fila() por inserir_item_fila().
Veja o comentário acima para o trecho exato.
"""

if __name__ == "__main__":
    print("Este arquivo é documentação de patch — não execute diretamente.")
    print("Aplique cada bloco ALTERAÇÃO no app.py conforme as instruções.")
