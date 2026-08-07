"""
Rota para salvar e carregar credenciais de sistemas externos (AKG, Google, etc.)
Armazena em data/credentials.json — GET retorna apenas quais sistemas estão configurados.
"""
from pathlib import Path
import json
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Any

router = APIRouter()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CREDS_PATH = DATA_DIR / "credentials.json"

ALLOWED_SISTEMAS = {
    "bling_akg", "ml_akg", "amazon_akg", "shopee_akg",
    "google_ads", "google_merchant_center", "google_analytics", "google_search_console",
}

ALLOWED_CAMPOS: dict[str, set] = {
    "bling_akg": {"client_id", "client_secret"},
    "ml_akg": {"client_id", "client_secret", "access_token", "refresh_token"},
    "amazon_akg": {"seller_id", "lwa_app_id", "lwa_client_secret", "refresh_token"},
    "shopee_akg": {"partner_id", "partner_key", "shop_id", "access_token"},
    "google_ads": {"developer_token", "customer_id", "client_id", "client_secret", "refresh_token"},
    "google_merchant_center": {"merchant_id", "service_account_email", "service_account_json"},
    "google_analytics": {"property_id", "service_account_json"},
    "google_search_console": {"site_url", "service_account_json"},
}


def _load() -> dict:
    if CREDS_PATH.exists():
        try:
            return json.loads(CREDS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save(data: dict):
    DATA_DIR.mkdir(exist_ok=True)
    CREDS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


@router.get("/config/credenciais")
async def get_credenciais():
    """Retorna quais sistemas estão configurados (sem expor valores)."""
    data = _load()
    resumo = {sis: list(campos.keys()) for sis, campos in data.items()}
    return JSONResponse(resumo)


@router.post("/config/credenciais")
async def post_credenciais(body: dict[str, Any]):
    sistema = body.get("sistema", "")
    if sistema not in ALLOWED_SISTEMAS:
        return JSONResponse({"ok": False, "detail": f"Sistema '{sistema}' não reconhecido."}, status_code=400)

    allowed = ALLOWED_CAMPOS.get(sistema, set())
    campos = {k: v for k, v in body.items() if k in allowed and isinstance(v, str) and v.strip()}
    if not campos:
        return JSONResponse({"ok": False, "detail": "Nenhum campo válido fornecido."}, status_code=400)

    data = _load()
    if sistema not in data:
        data[sistema] = {}
    data[sistema].update(campos)
    _save(data)
    return {"ok": True, "sistema": sistema, "campos_salvos": list(campos.keys())}
