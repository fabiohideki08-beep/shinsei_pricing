# -*- coding: utf-8 -*-
"""
Shopify webhooks — rastreamento server-side de conversões no Google Ads.
Quando um pedido é pago (orders/paid), envia conversão via Google Ads API.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

router = APIRouter(prefix="/shopify/webhook", tags=["shopify-webhooks"])

# Google Ads — Conversions API (offline upload)
AW_CUSTOMER_ID   = "2097362078"
CONVERSION_ACTION = "customers/2097362078/conversionActions/7250153929"  # Finalizacao da compra

DATA_DIR = Path(__file__).parent.parent / "data"


def _gads_client():
    import yaml
    from google.ads.googleads.client import GoogleAdsClient
    cfg = yaml.safe_load((Path(__file__).parent.parent / "google-ads.yaml").read_text())
    return GoogleAdsClient.load_from_dict({
        "developer_token": cfg["developer_token"],
        "client_id":       cfg["client_id"],
        "client_secret":   cfg["client_secret"],
        "refresh_token":   cfg["refresh_token"],
        "login_customer_id": str(cfg["login_customer_id"]),
        "use_proto_plus":  True,
    })


def _upload_conversion(order: dict):
    """Envia conversão de compra para o Google Ads via Offline Conversion Upload."""
    try:
        client = _gads_client()
        svc = client.get_service("ConversionUploadService")

        total_micros = order.get("total_price_usd") or order.get("total_price", "0")
        valor = float(total_micros) if total_micros else 0.0

        order_id  = str(order.get("id", ""))
        email     = (order.get("email") or "").strip().lower()
        phone     = (order.get("phone") or "").strip()
        created   = order.get("created_at", datetime.now(timezone.utc).isoformat())

        # Construir conversão
        conv = client.get_type("ClickConversion")
        conv.conversion_action  = CONVERSION_ACTION
        conv.conversion_date_time = _fmt_date(created)
        conv.order_id           = order_id
        conv.currency_code      = "BRL"
        conv.conversion_value   = valor

        # Hashed user identifiers (privacy-safe)
        eid = client.get_type("UserIdentifier")
        if email:
            eid.hashed_email = hashlib.sha256(email.encode()).hexdigest()
            conv.user_identifiers.append(eid)

        if phone:
            phone_clean = "".join(c for c in phone if c.isdigit())
            if len(phone_clean) >= 10:
                pid = client.get_type("UserIdentifier")
                pid.hashed_phone_number = hashlib.sha256(
                    f"+55{phone_clean}".encode()
                ).hexdigest()
                conv.user_identifiers.append(pid)

        req = client.get_type("UploadClickConversionsRequest")
        req.customer_id = AW_CUSTOMER_ID
        req.conversions.append(conv)
        req.partial_failure = True

        resp = svc.upload_click_conversions(request=req)

        if resp.partial_failure_error and resp.partial_failure_error.message:
            print(f"[webhook] GAds conversão parcial: {resp.partial_failure_error.message}")
        else:
            print(f"[webhook] GAds conversão enviada: pedido={order_id} valor=R${valor:.2f}")

        _log_conversion(order_id, valor, "ok")

    except Exception as e:
        print(f"[webhook] ERRO upload conversão: {e}")
        _log_conversion(str(order.get("id", "?")), 0, f"erro:{e}")


def _fmt_date(iso: str) -> str:
    """Converte ISO 8601 para o formato exigido pelo Google Ads API."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S+00:00")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")


def _log_conversion(order_id: str, valor: float, status: str):
    log_file = DATA_DIR / "conversoes_gads.json"
    try:
        log = json.loads(log_file.read_text(encoding="utf-8")) if log_file.exists() else []
    except Exception:
        log = []
    log.append({"order_id": order_id, "valor": valor, "status": status,
                 "ts": datetime.now(timezone.utc).isoformat()})
    log_file.write_text(json.dumps(log[-200:], ensure_ascii=False, indent=2), encoding="utf-8")


def _verify_hmac(body: bytes, hmac_header: str) -> bool:
    secret = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")
    if not secret:
        return True  # sem segredo configurado, aceitar
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    import base64
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, hmac_header or "")


@router.post("/order-paid")
async def order_paid(
    request: Request,
    background_tasks: BackgroundTasks,
    x_shopify_hmac_sha256: str = Header(default=""),
):
    """Recebe pedidos pagos do Shopify e envia conversão ao Google Ads."""
    body = await request.body()

    if not _verify_hmac(body, x_shopify_hmac_sha256):
        raise HTTPException(status_code=401, detail="HMAC inválido")

    try:
        order = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido")

    background_tasks.add_task(_upload_conversion, order)
    return {"ok": True, "order_id": order.get("id")}


@router.get("/conversoes")
def listar_conversoes():
    """Lista as últimas conversões enviadas ao Google Ads."""
    log_file = DATA_DIR / "conversoes_gads.json"
    if not log_file.exists():
        return {"conversoes": []}
    return {"conversoes": json.loads(log_file.read_text(encoding="utf-8"))}
