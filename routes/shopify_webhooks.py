# -*- coding: utf-8 -*-
"""
Shopify webhooks — rastreamento server-side de conversões no Google Ads
e geração automática de etiquetas MelhorEnvio para pedidos com frete grátis.
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

# MelhorEnvio
ME_ORIGIN_CEP = "06036003"  # SHINSEI MARKETPLACE, Osasco SP
ME_SERVICES   = "1,2,3,4,17,33"  # PAC, SEDEX, Jadlog .Package/.Com, Mini Envios, JeT Standard


def _me_token() -> str:
    tok = os.getenv("MELHOR_ENVIO_TOKEN", "")
    if not tok:
        f = DATA_DIR / "melhorenvio_token.json"
        if f.exists():
            tok = json.loads(f.read_text())["access_token"]
    return tok


def _me_headers(tok: str) -> dict:
    return {
        "Authorization": f"Bearer {tok}",
        "User-Agent": "Aplicação shinsei-pricing fabiohideki08@gmail.com",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _is_free_shipping(order: dict) -> bool:
    for line in order.get("shipping_lines", []):
        if float(line.get("price", "0") or "0") == 0:
            return True
    return not order.get("shipping_lines")


def _is_rmsp(order: dict) -> bool:
    """RMSP é atendido por parceiro próprio — não gerar etiqueta ME."""
    for line in order.get("shipping_lines", []):
        title = (line.get("title") or line.get("code") or "").upper()
        if "RMSP" in title or "REGIÃO METROPOLITANA" in title:
            return True
    addr = order.get("shipping_address") or {}
    cep = (addr.get("zip") or "").replace("-", "").replace(" ", "")
    if len(cep) >= 5:
        prefix = int(cep[:5])
        # CEPs RMSP: 01000-09999 (SP capital), 06000-06299 (Osasco), 07000-07299 (Guarulhos)
        # 09000-09999 (ABC), 06300-06999 (Carapicuíba/Barueri/etc)
        if 1000 <= prefix <= 9999:
            return True
    return False


def _generate_me_label(order: dict):
    """Cota o ME e gera a etiqueta mais barata para pedidos com frete grátis fora da RMSP."""
    order_name = order.get("name", f"#{order.get('id','?')}")
    order_id   = str(order.get("id", ""))
    try:
        tok = _me_token()
        if not tok:
            print(f"[me_label] {order_name} — token ME não encontrado"); return

        addr = order.get("shipping_address") or order.get("billing_address") or {}
        dest_cep = (addr.get("zip") or "").replace("-", "").replace(" ", "")
        if len(dest_cep) < 8:
            print(f"[me_label] {order_name} — CEP inválido: {dest_cep}"); return

        items = order.get("line_items", [])
        weight_g  = sum(float(i.get("grams") or 300) * int(i.get("quantity", 1)) for i in items)
        weight_kg = max(round(weight_g / 1000, 3), 0.1)

        # Cotação
        r = requests.post(
            "https://melhorenvio.com.br/api/v2/me/shipment/calculate",
            json={
                "from": {"postal_code": ME_ORIGIN_CEP},
                "to":   {"postal_code": dest_cep},
                "package": {"weight": weight_kg, "width": 16, "height": 10, "length": 22},
                "options": {"receipt": False, "own_hand": False},
                "services": ME_SERVICES,
            },
            headers=_me_headers(tok), timeout=15,
        )
        quotes = r.json() if r.status_code == 200 else []
        available = [q for q in quotes if q.get("price") and not q.get("error")]
        if not available:
            print(f"[me_label] {order_name} — nenhuma cotação para CEP {dest_cep}"); return

        best = min(available, key=lambda q: float(q["price"]))
        print(f"[me_label] {order_name} — melhor: {best['name']} R${best['price']}")

        # Destinatário
        name = (addr.get("name") or
                f"{addr.get('first_name','')} {addr.get('last_name','')}".strip() or
                "Destinatário")
        phone = (addr.get("phone") or "").replace(" ", "").replace("-", "")

        # CPF do destinatário — Shopify coloca em note_attributes
        cpf_dest = ""
        for attr in order.get("note_attributes", []):
            if attr.get("name", "").upper() in ("CPF", "CPF/CNPJ", "CNPJ", "DOCUMENTO"):
                cpf_dest = "".join(c for c in str(attr.get("value", "")) if c.isdigit())
                break

        # Shopify: address1 = "Rua X, 123" ou "Rua X"; address2 = complemento
        # ME exige address (rua) + number separados
        address1_raw = addr.get("address1", "")
        complement   = addr.get("address2", "") or ""
        # Tenta extrair número do fim de address1 (ex: "Rua Rio Mamoré 58" → rua="Rua Rio Mamoré", num="58")
        import re as _re
        _m = _re.search(r"^(.*?)[,\s]+(\d+\w*)$", address1_raw.strip())
        if _m:
            address1 = _m.group(1).strip()
            number   = _m.group(2)
        else:
            address1 = address1_raw
            number   = complement or "SN"
            complement = ""
        # Shopify não tem campo bairro — usar cidade como fallback aceito pelo ME
        district = addr.get("city", "Centro") or "Centro"

        # Carrinho ME
        r2 = requests.post(
            "https://melhorenvio.com.br/api/v2/me/cart",
            json={
                "service": best["id"],
                "agency":  best.get("agency"),
                "from": {
                    "name":        "SHINSEI MARKETPLACE",
                    "phone":       "1140040140",
                    "email":       "fabiohideki08@gmail.com",
                    "address":     "Rua Norma de Freitas Borges",
                    "number":      "65",
                    "district":    "Presidente Altino",
                    "city":        "Osasco",
                    "state_abbr":  "SP",
                    "postal_code": ME_ORIGIN_CEP,
                    "country_id":  "BR",
                },
                "to": {
                    "name":        name,
                    "phone":       phone or "11999999999",
                    "document":    cpf_dest or None,
                    "address":     address1,
                    "number":      number,
                    "complement":  complement,
                    "district":    district,
                    "city":        addr.get("city", ""),
                    "state_abbr":  (addr.get("province_code") or "").replace("BR-", ""),
                    "postal_code": dest_cep,
                    "country_id":  "BR",
                },
                "products": [
                    {"name": i.get("name", "Produto")[:50],
                     "quantity": int(i.get("quantity", 1)),
                     "unitary_value": float(i.get("price") or "0")}
                    for i in items[:10]
                ],
                "volumes": [{"weight": weight_kg, "width": 16, "height": 10, "length": 22}],
                "tag": [{"tag": order_name, "url": None}],
                "options": {
                    "receipt": False, "own_hand": False,
                    "reverse": False, "non_commercial": False,
                    "insurance_value": round(float(order.get("total_price") or "0") or 1.0, 2),
                },
            },
            headers=_me_headers(tok), timeout=15,
        )
        if r2.status_code not in (200, 201):
            print(f"[me_label] {order_name} — erro carrinho: {r2.status_code} {r2.text[:300]}")
            _log_me_label(order_id, order_name, None, best["name"],
                          float(best["price"]), dest_cep, f"erro_cart:{r2.status_code}")
            return

        cart_id = r2.json().get("id")

        # Checkout — debita saldo ME e gera etiqueta
        r3 = requests.post(
            "https://melhorenvio.com.br/api/v2/me/shipment/checkout",
            json={"orders": [cart_id]},
            headers=_me_headers(tok), timeout=15,
        )
        if r3.status_code not in (200, 201):
            # Remover item do carrinho e tentar próximo serviço
            requests.delete(f"https://melhorenvio.com.br/api/v2/me/cart/{cart_id}",
                            headers=_me_headers(tok), timeout=10)
            remaining = [q for q in available if q["id"] != best["id"]]
            if remaining:
                best = min(remaining, key=lambda q: float(q["price"]))
                print(f"[me_label] {order_name} — retry com {best['name']} R${best['price']}")
                cart_payload["service"] = best["id"]
                cart_payload["agency"] = best.get("agency")
                r2b = requests.post("https://melhorenvio.com.br/api/v2/me/cart",
                                    json=cart_payload, headers=_me_headers(tok), timeout=15)
                if r2b.status_code not in (200, 201):
                    _log_me_label(order_id, order_name, None, best["name"],
                                  float(best["price"]), dest_cep, f"erro_retry:{r2b.status_code}")
                    return
                cart_id = r2b.json().get("id")
                r3 = requests.post("https://melhorenvio.com.br/api/v2/me/shipment/checkout",
                                   json={"orders": [cart_id]}, headers=_me_headers(tok), timeout=15)
            if r3.status_code not in (200, 201):
                print(f"[me_label] {order_name} — erro checkout: {r3.status_code} {r3.text[:300]}")
                _log_me_label(order_id, order_name, cart_id, best["name"],
                              float(best["price"]), dest_cep, f"erro_checkout:{r3.status_code}")
                return

        print(f"[me_label] ✅ {order_name} — etiqueta gerada! id={cart_id} "
              f"serviço={best['name']} R${best['price']} CEP={dest_cep}")
        _log_me_label(order_id, order_name, cart_id, best["name"],
                      float(best["price"]), dest_cep, "ok")

    except Exception as e:
        print(f"[me_label] ERRO {order_name}: {e}")
        _log_me_label(order_id, order_name, None, "", 0, "", f"erro:{e}")


def _log_me_label(order_id, order_name, cart_id, servico, preco, cep, status):
    log_file = DATA_DIR / "etiquetas_me.json"
    try:
        log = json.loads(log_file.read_text(encoding="utf-8")) if log_file.exists() else []
    except Exception:
        log = []
    log.append({"order_id": order_id, "order_name": order_name, "cart_id": cart_id,
                 "servico": servico, "preco": preco, "cep": cep, "status": status,
                 "ts": datetime.now(timezone.utc).isoformat()})
    log_file.write_text(json.dumps(log[-500:], ensure_ascii=False, indent=2), encoding="utf-8")


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

    if not _is_rmsp(order):
        background_tasks.add_task(_generate_me_label, order)

    background_tasks.add_task(_upload_conversion, order)

    return {"ok": True, "order_id": order.get("id")}


@router.get("/conversoes")
def listar_conversoes():
    """Lista as últimas conversões enviadas ao Google Ads."""
    log_file = DATA_DIR / "conversoes_gads.json"
    if not log_file.exists():
        return {"conversoes": []}
    return {"conversoes": json.loads(log_file.read_text(encoding="utf-8"))}


@router.get("/etiquetas-me")
def listar_etiquetas_me():
    """Lista etiquetas MelhorEnvio geradas automaticamente."""
    log_file = DATA_DIR / "etiquetas_me.json"
    if not log_file.exists():
        return {"etiquetas": []}
    return {"etiquetas": json.loads(log_file.read_text(encoding="utf-8"))[-50:]}


@router.post("/test-me-label")
def test_me_label(cep: str = "87043595", peso_kg: float = 0.6):
    """Debug síncrono: cota ME para CEP informado e retorna resultado sem comprar."""
    tok = _me_token()
    if not tok:
        return {"erro": "Token ME não encontrado", "env_var": bool(os.getenv("MELHOR_ENVIO_TOKEN"))}
    try:
        r = requests.post(
            "https://melhorenvio.com.br/api/v2/me/shipment/calculate",
            json={
                "from": {"postal_code": ME_ORIGIN_CEP},
                "to":   {"postal_code": cep.replace("-", "")},
                "package": {"weight": peso_kg, "width": 16, "height": 10, "length": 22},
                "options": {"receipt": False, "own_hand": False},
                "services": ME_SERVICES,
            },
            headers=_me_headers(tok), timeout=15,
        )
        quotes = r.json() if r.status_code == 200 else []
        available = [q for q in quotes if q.get("price") and not q.get("error")]
        if not available:
            return {"status": r.status_code, "raw": quotes}
        best = min(available, key=lambda q: float(q["price"]))
        return {
            "token_ok": True,
            "cep_destino": cep,
            "melhor": {"nome": best["name"], "preco": best["price"], "prazo": best.get("delivery_time")},
            "todas": [{"nome": q["name"], "preco": q["price"]} for q in available],
        }
    except Exception as e:
        return {"erro": str(e)}
