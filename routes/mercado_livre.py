from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel

from services.mercado_livre import (
    MercadoLivreOAuthService,
    MercadoLivreService,
    MercadoLivreConfigStore,
)

router = APIRouter()
BASE_DIR = Path(__file__).resolve().parent.parent
PAGES_DIR = BASE_DIR / "pages"


def get_oauth_service():
    return MercadoLivreOAuthService(base_dir=BASE_DIR)


def get_config_store():
    return MercadoLivreConfigStore(base_dir=BASE_DIR)


class AtualizarPrecoRequest(BaseModel):
    item_id_classico: str | None = None
    item_id_premium: str | None = None
    preco: float


@router.get("/mercado-livre", response_class=HTMLResponse)
def mercado_livre_page():
    return (PAGES_DIR / "mercado_livre.html").read_text(encoding="utf-8")


@router.get("/ml/config")
def ml_config_get():
    data = get_config_store().load()
    data["client_secret"] = ""
    return data


@router.post("/ml/config")
async def ml_config_save(request: Request):
    body = await request.json()
    atual = get_config_store().load()

    novo = {
        "client_id": body.get("client_id", atual.get("client_id", "")).strip(),
        "client_secret": body.get("client_secret", "").strip() or atual.get("client_secret", ""),
        "redirect_uri": body.get("redirect_uri", atual.get("redirect_uri", "")).strip(),
        "conexao_ativa": atual.get("conexao_ativa", False),
    }

    salvo = get_config_store().save(novo)
    salvo["client_secret"] = ""
    return {"success": True, "data": salvo}


@router.get("/ml/status")
def ml_status():
    return get_oauth_service().status()


@router.get("/ml/login")
def ml_login():
    oauth_service = get_oauth_service()
    result = oauth_service.iniciar_login()

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    return RedirectResponse(result["auth_url"])


@router.get("/ml/callback")
async def ml_callback(request: Request):
    code = request.query_params.get("code")
    error = request.query_params.get("error")

    if error:
        return {"success": False, "error": f"Mercado Livre retornou erro: {error}"}

    if not code:
        return {"success": False, "error": "Code não recebido"}

    oauth_service = get_oauth_service()
    result = oauth_service.trocar_code_por_token(code)

    if not result["success"]:
        return {"success": False, "error": result["error"]}

    return {
        "success": True,
        "message": "Mercado Livre conectado com sucesso",
        "data": result["data"],
    }


@router.get("/ml/tokens")
def ml_tokens():
    oauth_service = get_oauth_service()
    result = oauth_service.ler_tokens()

    if not result["success"]:
        raise HTTPException(status_code=404, detail=result["error"])

    return result


@router.post("/ml/refresh")
def ml_refresh():
    oauth_service = get_oauth_service()
    result = oauth_service.refresh_token()

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "Falha ao renovar token"))

    return {
        "success": True,
        "message": "Token renovado com sucesso.",
        "data": result["data"],
    }


@router.post("/precificar-ml")
def precificar_ml(req: AtualizarPrecoRequest):
    oauth_service = get_oauth_service()
    tokens = oauth_service.ler_tokens()

    if not tokens["success"]:
        raise HTTPException(status_code=400, detail="Token ML não encontrado")

    access_token = tokens["data"]["access_token"]
    service = MercadoLivreService(access_token)
    resultados = []

    if req.item_id_classico:
        resultados.append({
            "tipo": "classico",
            "item_id": req.item_id_classico,
            "resultado": service.atualizar_com_retry(req.item_id_classico, req.preco),
        })

    if req.item_id_premium:
        resultados.append({
            "tipo": "premium",
            "item_id": req.item_id_premium,
            "resultado": service.atualizar_com_retry(req.item_id_premium, req.preco),
        })

    if not resultados:
        raise HTTPException(status_code=400, detail="Nenhum item informado")

    return {"success": True, "atualizacoes": resultados}


# ── ML OAuth — conta AKG (segunda empresa) ───────────────────────────────────

import os as _os

class _MercadoLivreOAuthAKG(MercadoLivreOAuthService):
    """Igual ao serviço principal mas usa credenciais e tokens da conta AKG."""
    def __init__(self, base_dir):
        # NÃO chama super().__init__() para evitar carregar config da conta Shinsei
        from pathlib import Path as _P
        from services.mercado_livre import MercadoLivreConfigStore, ML_AUTH
        self.base_dir = _P(base_dir)
        self.data_dir = self.base_dir / "data"
        self.data_dir.mkdir(exist_ok=True)
        self.tokens_file = self.data_dir / "ml_tokens_akg.json"
        self.state_file  = self.data_dir / "ml_oauth_state_akg.json"
        self.client_id     = _os.getenv("ML_AKG_CLIENT_ID", "").strip()
        self.client_secret = _os.getenv("ML_AKG_CLIENT_SECRET", "").strip()
        self.redirect_uri  = _os.getenv("ML_AKG_REDIRECT_URI",
                                        "https://shinsei-pricing.onrender.com/ml/callback2").strip()


def _get_akg_oauth():
    return _MercadoLivreOAuthAKG(BASE_DIR)


@router.get("/ml/login2")
def ml_login2():
    """Inicia OAuth ML para conta AKG (segunda empresa)."""
    svc = _get_akg_oauth()
    result = svc.iniciar_login()
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return RedirectResponse(result["auth_url"])


@router.get("/ml/callback2")
async def ml_callback2(request: Request):
    """Callback OAuth ML conta AKG."""
    code = request.query_params.get("code")
    error = request.query_params.get("error")
    if error:
        return {"success": False, "error": f"ML retornou erro: {error}"}
    if not code:
        return {"success": False, "error": "Code não recebido"}
    svc = _get_akg_oauth()
    result = svc.trocar_code_por_token(code)
    if not result["success"]:
        return {"success": False, "error": result["error"]}
    return {"success": True, "message": "ML AKG conectado com sucesso", "data": result["data"]}


@router.get("/ml/status2")
def ml_status2():
    """Status da conexão ML conta AKG."""
    return _get_akg_oauth().status()


@router.post("/ml/refresh2")
def ml_refresh2():
    """Renova o token ML da conta AKG."""
    result = _get_akg_oauth().refresh_token()
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result.get("error", "Falha ao renovar token AKG"))
    return {"success": True, "message": "Token AKG renovado.", "data": result["data"]}