"""
Rota para salvar e carregar credenciais de sistemas externos (AKG, Google, etc.)
Armazena em data/credentials.json — GET retorna apenas quais sistemas estão configurados.
"""
from pathlib import Path
import json
import os
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from typing import Any

router = APIRouter()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CREDS_PATH = DATA_DIR / "credentials.json"

ALLOWED_SISTEMAS = {
    "bling_shinsei", "ml_shinsei",
    "bling_akg", "ml_akg", "amazon_akg", "shopee_akg",
    "google_ads", "google_merchant_center", "google_analytics", "google_search_console",
    "shopee", "amazon",
}

ALLOWED_CAMPOS: dict[str, set] = {
    "bling_shinsei": {"access_token", "refresh_token"},
    "ml_shinsei": {"access_token", "refresh_token"},
    "bling_akg": {"client_id", "client_secret"},
    "ml_akg": {"client_id", "client_secret", "access_token", "refresh_token"},
    "amazon_akg": {"seller_id", "lwa_app_id", "lwa_client_secret", "refresh_token"},
    "shopee_akg": {"partner_id", "partner_key", "shop_id", "access_token"},
    "google_ads": {"developer_token", "customer_id", "client_id", "client_secret", "refresh_token"},
    "google_merchant_center": {"merchant_id", "service_account_email", "service_account_json"},
    "google_analytics": {"property_id", "service_account_json"},
    "google_search_console": {"site_url", "service_account_json"},
    # Shinsei main account — env var names used as keys
    "shopee": {"SHOPEE_PARTNER_ID", "SHOPEE_PARTNER_KEY", "SHOPEE_SHOP_ID", "SHOPEE_ACCESS_TOKEN", "SHOPEE_REFRESH_TOKEN"},
    "amazon": {"AMAZON_CLIENT_ID", "AMAZON_CLIENT_SECRET", "AMAZON_REFRESH_TOKEN", "AMAZON_SELLER_ID", "AMAZON_MARKETPLACE_ID"},
}

# Sistemas cujas chaves são env vars — serão injetadas em os.environ diretamente
ENV_VAR_SISTEMAS = {"shopee", "amazon"}


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

    # Para sistemas baseados em env vars, injeta imediatamente no processo
    if sistema in ENV_VAR_SISTEMAS:
        for k, v in campos.items():
            os.environ[k] = v

    # Persistir tokens ML/Bling no Render para sobreviver a deploys
    try:
        if sistema == "ml_shinsei":
            from render_persistence import save_ml_tokens_shinsei
            save_ml_tokens_shinsei(
                campos.get("access_token", ""),
                campos.get("refresh_token", ""),
                os.getenv("ML_USER_ID", "733168645"),
            )
        elif sistema == "ml_akg" and ("access_token" in campos or "refresh_token" in campos):
            from render_persistence import save_ml_tokens_akg
            creds = data.get("ml_akg", {})
            save_ml_tokens_akg(
                campos.get("access_token", creds.get("access_token", "")),
                campos.get("refresh_token", creds.get("refresh_token", "")),
                os.getenv("ML_AKG_USER_ID", "3541432733"),
            )
        elif sistema == "bling_shinsei":
            from render_persistence import save_bling_tokens
            save_bling_tokens(campos.get("access_token", ""), campos.get("refresh_token", ""))
        elif sistema == "bling_akg" and ("access_token" in campos or "refresh_token" in campos):
            from render_persistence import save_bling_tokens_akg
            creds = data.get("bling_akg", {})
            save_bling_tokens_akg(
                campos.get("access_token", creds.get("access_token", "")),
                campos.get("refresh_token", creds.get("refresh_token", "")),
            )
    except Exception:
        pass  # Render persistence é best-effort

    return {"ok": True, "sistema": sistema, "campos_salvos": list(campos.keys())}
