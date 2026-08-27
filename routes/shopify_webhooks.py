# -*- coding: utf-8 -*-
"""
Shopify webhooks — rastreamento server-side de conversões no Google Ads
e configuração automática de transporte MelhorEnvio no Bling para emissão
da NF com etiqueta ME (impressão automática Bling = DANFE + etiqueta juntos).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import smtplib
import threading
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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

# Bling — loja Shopify
BLING_SHOPIFY_LOJA_ID = 206160746


def _bling_token() -> str:
    """Pega token Bling Shinsei via BlingClient (com auto-refresh)."""
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from bling_client import BlingClient
        bc = BlingClient()
        if bc.has_local_tokens():
            tok = bc._load_tokens().get("access_token", "")
            if tok:
                return tok
    except Exception:
        pass
    # Fallback: ler diretamente do arquivo
    for fname in ("bling_tokens.json", "bling_token_fresh.json"):
        f = DATA_DIR / fname
        if f.exists():
            try:
                data = json.loads(f.read_text())
                tok = data.get("access_token") or data.get("data", {}).get("access_token", "")
                if tok:
                    return tok
            except Exception:
                pass
    return ""


def _bling_headers(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _me_quote_best(dest_cep: str, weight_kg: float) -> dict | None:
    """Cota ME e retorna o serviço mais barato disponível."""
    tok = _me_token()
    if not tok:
        return None
    try:
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
        return min(available, key=lambda q: float(q["price"])) if available else None
    except Exception:
        return None


# Mapeamento nome ME → transportadora Bling (IDs configurados na conta Bling)
_ME_SERVICE_TO_BLING = {
    "PAC":      {"nome": "PAC - Correios", "codigo": "PAC"},
    "SEDEX":    {"nome": "SEDEX - Correios", "codigo": "SEDEX"},
    ".Package": {"nome": "Jadlog .Package", "codigo": "JADLOG_PACKAGE"},
    ".Com":     {"nome": "Jadlog .Com",     "codigo": "JADLOG_COM"},
    "Standard": {"nome": "JeT Standard",   "codigo": "JET_STANDARD"},
    "Mini Envios": {"nome": "Mini Envios - Correios", "codigo": "MINI_ENVIOS"},
}


def _set_bling_transporte_me(order: dict) -> bool:
    """
    Busca o pedido de venda no Bling correspondente ao pedido Shopify,
    e atualiza o transporte com o serviço ME mais barato cotado.
    Quando o usuário emitir a NF no Bling, a impressão automática
    gera a etiqueta ME + DANFE juntos.
    """
    order_name = order.get("name", f"#{order.get('id','?')}")
    order_num  = str(order.get("order_number") or order.get("number") or "")

    bling_tok = _bling_token()
    if not bling_tok:
        print(f"[bling_transporte] {order_name} — token Bling não disponível"); return False

    addr = order.get("shipping_address") or order.get("billing_address") or {}
    dest_cep = (addr.get("zip") or "").replace("-", "").replace(" ", "")
    if len(dest_cep) < 8:
        print(f"[bling_transporte] {order_name} — CEP inválido: {dest_cep}"); return False

    items = order.get("line_items", [])
    weight_kg = max(round(sum(float(i.get("grams") or 300) * int(i.get("quantity", 1)) for i in items) / 1000, 3), 0.1)

    # Cotar ME
    best = _me_quote_best(dest_cep, weight_kg)
    if not best:
        print(f"[bling_transporte] {order_name} — sem cotação ME para CEP {dest_cep}"); return False
    print(f"[bling_transporte] {order_name} — melhor: {best['name']} R${best['price']}")

    # Buscar pedido no Bling pelo número do pedido Shopify (loja integrada)
    try:
        r = requests.get(
            "https://bling.com.br/Api/v3/pedidos/vendas",
            params={"numeroPedido": order_num, "idLoja": BLING_SHOPIFY_LOJA_ID, "pagina": 1, "limite": 5},
            headers=_bling_headers(bling_tok), timeout=15,
        )
        pedidos = r.json().get("data", []) if r.status_code == 200 else []
        if not pedidos:
            # Fallback: buscar por data recente
            r2 = requests.get(
                "https://bling.com.br/Api/v3/pedidos/vendas",
                params={"idLoja": BLING_SHOPIFY_LOJA_ID, "pagina": 1, "limite": 50},
                headers=_bling_headers(bling_tok), timeout=15,
            )
            all_pedidos = r2.json().get("data", []) if r2.status_code == 200 else []
            pedidos = [p for p in all_pedidos if str(p.get("numero", "")) == order_num
                       or str(p.get("numeroExterno", "")) == order_num
                       or str(p.get("numeroExterno", "")) == str(order.get("id", ""))]
    except Exception as e:
        print(f"[bling_transporte] {order_name} — erro busca Bling: {e}"); return False

    if not pedidos:
        print(f"[bling_transporte] {order_name} — pedido não encontrado no Bling (num={order_num})"); return False

    bling_pedido_id = pedidos[0]["id"]
    service_map = _ME_SERVICE_TO_BLING.get(best["name"], {"nome": best["name"], "codigo": best["name"].upper()})

    # Atualizar transporte no pedido Bling
    try:
        r3 = requests.patch(
            f"https://bling.com.br/Api/v3/pedidos/vendas/{bling_pedido_id}",
            json={
                "transporte": {
                    "transportador": {"nome": service_map["nome"]},
                    "tipo": "D",
                    "servico": service_map["codigo"],
                    "prazoEntrega": int(best.get("delivery_time") or 7),
                    "freteValor": float(best["price"]),
                }
            },
            headers=_bling_headers(bling_tok), timeout=15,
        )
        if r3.status_code in (200, 201, 204):
            print(f"[bling_transporte] ✅ {order_name} — transporte ME configurado no Bling (pedido {bling_pedido_id})")
            _log_me_label(str(order.get("id","")), order_name, None,
                          best["name"], float(best["price"]), dest_cep, "bling_transporte_ok")
            return True
        else:
            print(f"[bling_transporte] {order_name} — erro PATCH Bling: {r3.status_code} {r3.text[:200]}")
            return False
    except Exception as e:
        print(f"[bling_transporte] {order_name} — erro update Bling: {e}"); return False


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

        # Destinatário
        name = (addr.get("name") or
                f"{addr.get('first_name','')} {addr.get('last_name','')}".strip() or
                "Destinatário")
        phone = (addr.get("phone") or "").replace(" ", "").replace("-", "")

        # CPF do destinatário — tenta note_attributes, company field e customer.note
        cpf_dest = ""
        for attr in order.get("note_attributes", []):
            if attr.get("name", "").upper() in ("CPF", "CPF/CNPJ", "CNPJ", "DOCUMENTO", "TAX_ID", "TAXID"):
                cpf_dest = "".join(c for c in str(attr.get("value", "")) if c.isdigit())
                break
        if not cpf_dest:
            for a in (addr, order.get("billing_address") or {}):
                raw = "".join(c for c in (a.get("company") or "") if c.isdigit())
                if 11 <= len(raw) <= 14:
                    cpf_dest = raw; break
        if not cpf_dest:
            raw = "".join(c for c in (order.get("customer", {}).get("note") or "") if c.isdigit())
            if 11 <= len(raw) <= 14:
                cpf_dest = raw

        # Sem CPF, descartar serviços que exigem documento (ex: Jadlog)
        if not cpf_dest:
            sem_cpf = [q for q in available if "documents" not in (q.get("requirements") or [])]
            if sem_cpf:
                available = sem_cpf
                print(f"[me_label] {order_name} — sem CPF, excluindo serviços com documento obrigatório")
            else:
                print(f"[me_label] {order_name} — sem CPF e nenhum serviço disponível sem documento"); return

        best = min(available, key=lambda q: float(q["price"]))
        print(f"[me_label] {order_name} — melhor: {best['name']} R${best['price']}")

        # Shopify: address1 = "Rua X, 123" ou "Rua X"; address2 = complemento
        # ME exige address (rua) + number separados
        import re as _re
        # Remove caracteres invisíveis (U+2060, ZWNBSP, etc) que Shopify às vezes insere
        def _clean(s): return _re.sub(r'[⁠​﻿­]', '', s or '').strip()
        address1_raw = _clean(addr.get("address1", ""))
        complement   = _clean(addr.get("address2", "")) or ""
        # Tenta extrair número do fim de address1 (ex: "Rua Rio Mamoré 58" → rua="Rua Rio Mamoré", num="58")
        _m = _re.search(r"^(.*?)[,\s]+(\d+\w*)$", address1_raw)
        if _m:
            address1 = _m.group(1).strip()
            number   = _m.group(2)[:20]  # ME limita a 32 chars mas 20 é seguro
        else:
            address1 = address1_raw
            number   = "SN"
        # Shopify não tem campo bairro — usar cidade como fallback aceito pelo ME
        district = addr.get("city", "Centro") or "Centro"

        # Carrinho ME
        cart_payload = {
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
        }
        r2 = requests.post("https://melhorenvio.com.br/api/v2/me/cart",
                           json=cart_payload, headers=_me_headers(tok), timeout=15)
        if r2.status_code not in (200, 201):
            # 422 = CPF obrigatório no serviço mesmo sem "documents" nos requirements
            # Tentar próximo serviço mais barato excluindo o atual
            if r2.status_code == 422 and "CPF" in r2.text:
                remaining_cart = [q for q in available if q["id"] != best["id"]]
                for fallback in sorted(remaining_cart, key=lambda q: float(q["price"])):
                    print(f"[me_label] {order_name} — {best['name']} exige CPF, tentando {fallback['name']}")
                    cart_payload2 = dict(cart_payload)
                    cart_payload2["service"] = fallback["id"]
                    cart_payload2["agency"] = fallback.get("agency")
                    r2b = requests.post("https://melhorenvio.com.br/api/v2/me/cart",
                                        json=cart_payload2, headers=_me_headers(tok), timeout=15)
                    if r2b.status_code in (200, 201):
                        best = fallback
                        r2 = r2b
                        break
                else:
                    print(f"[me_label] {order_name} — todos os serviços exigem CPF")
                    _log_me_label(order_id, order_name, None, best["name"],
                                  float(best["price"]), dest_cep, "erro_sem_cpf")
                    return
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


def _extract_gclid(order: dict) -> str:
    """
    Extrai GCLID do pedido Shopify.
    O GCLID é capturado no tema (theme.liquid) via ?gclid= na URL de landing,
    salvo em localStorage e propagado como cart attribute → note_attribute do pedido.
    """
    for attr in order.get("note_attributes", []):
        name = (attr.get("name") or "").lower().strip()
        if name in ("gclid", "wbraid", "gbraid"):
            val = (attr.get("value") or "").strip()
            if val:
                return val
    # Fallback: client_details (Shopify preenche automaticamente em alguns casos)
    cd = order.get("client_details") or {}
    ref = cd.get("browser_ip", "")  # apenas para log, não é GCLID
    return ""


def _upload_conversion(order: dict):
    """
    Envia conversão de compra para o Google Ads via Offline Conversion Upload.

    Estratégia de atribuição (em ordem de prioridade):
    1. GCLID presente no note_attributes → ClickConversion com gclid (atribuição direta ao clique)
    2. Sem GCLID → ClickConversion com user identifiers (Enhanced Conversions por email/phone)
       Requer que Enhanced Conversions esteja ativado na conta Google Ads.
    """
    try:
        client = _gads_client()
        svc = client.get_service("ConversionUploadService")

        valor   = float(order.get("total_price") or "0")
        order_id = str(order.get("id", ""))
        email    = (order.get("email") or "").strip().lower()
        phone    = (order.get("phone") or "").strip()
        created  = order.get("created_at", datetime.now(timezone.utc).isoformat())
        gclid    = _extract_gclid(order)

        print(f"[webhook] pedido={order_id} valor=R${valor:.2f} "
              f"gclid={'SIM ('+gclid[:12]+')' if gclid else 'NÃO — usando Enhanced Conversions'}")

        conv = client.get_type("ClickConversion")
        conv.conversion_action    = CONVERSION_ACTION
        conv.conversion_date_time = _fmt_date(created)
        conv.order_id             = order_id
        conv.currency_code        = "BRL"
        conv.conversion_value     = valor

        if gclid:
            # Atribuição direta: GCLID capturado do clique no anúncio
            conv.gclid = gclid
        else:
            # Enhanced Conversions: Google faz o match por email/phone hasheado
            # Requer configuração em Google Ads → Conversões → Configurações → Enhanced Conversions
            if email:
                eid = client.get_type("UserIdentifier")
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
            if not email and not phone:
                print(f"[webhook] pedido={order_id} — sem GCLID, email ou phone. "
                      "Conversão enviada mas provavelmente não será atribuída.")

        req = client.get_type("UploadClickConversionsRequest")
        req.customer_id  = AW_CUSTOMER_ID
        req.conversions.append(conv)
        req.partial_failure     = True
        req.validate_only       = False

        resp = svc.upload_click_conversions(request=req)

        # Logar erros de partial_failure com detalhes
        if resp.partial_failure_error and resp.partial_failure_error.message:
            pf_msg = resp.partial_failure_error.message
            print(f"[webhook] ⚠️ GAds partial_failure: {pf_msg}")
            _log_conversion(order_id, valor, f"partial_failure:{pf_msg[:120]}")
        else:
            status = "ok_gclid" if gclid else "ok_enhanced"
            print(f"[webhook] ✅ GAds conversão enviada: pedido={order_id} valor=R${valor:.2f} modo={status}")
            _log_conversion(order_id, valor, status)

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


def _set_bling_transporte_or_fallback(order: dict):
    """Tenta configurar transporte ME no Bling. Se Bling indisponível, gera etiqueta ME diretamente."""
    ok = _set_bling_transporte_me(order)
    if not ok:
        order_name = order.get("name", "?")
        print(f"[bling_transporte] {order_name} — Bling indisponível, gerando etiqueta ME direto (fallback)")
        _generate_me_label(order)


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
        # Tenta configurar transporte ME no Bling (fluxo principal: NF+etiqueta juntos)
        # Fallback: gera etiqueta ME diretamente se Bling não disponível
        background_tasks.add_task(_set_bling_transporte_or_fallback, order)

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


@router.post("/configurar-transporte/{order_id}")
def configurar_transporte(order_id: str):
    """Busca pedido Shopify e configura o transporte ME no Bling (para emissão de NF com etiqueta)."""
    shopify_token = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
    shopify_store = os.getenv("SHOPIFY_STORE", "pknw4n-eg.myshopify.com")
    r = requests.get(f"https://{shopify_store}/admin/api/2024-01/orders/{order_id}.json",
        headers={"X-Shopify-Access-Token": shopify_token}, timeout=15)
    if r.status_code != 200:
        return {"erro": f"Pedido não encontrado: {r.status_code}"}
    order = r.json()["order"]
    if _is_rmsp(order):
        return {"aviso": f"Pedido {order.get('name')} é RMSP — transporte ME não aplicável"}
    ok = _set_bling_transporte_me(order)
    if ok:
        return {"ok": True, "pedido": order.get("name"), "mensagem": "Transporte ME configurado no Bling — emita a NF para gerar a etiqueta"}
    return {"ok": False, "pedido": order.get("name"), "mensagem": "Falha ao configurar no Bling — verifique os logs"}


@router.post("/reenviar-etiqueta/{order_id}")
def reenviar_etiqueta(order_id: str):
    """Busca pedido no Shopify e gera etiqueta ME manualmente (sem HMAC)."""
    shopify_token = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
    shopify_store = os.getenv("SHOPIFY_STORE", "pknw4n-eg.myshopify.com")
    r = requests.get(
        f"https://{shopify_store}/admin/api/2024-01/orders/{order_id}.json",
        headers={"X-Shopify-Access-Token": shopify_token}, timeout=15,
    )
    if r.status_code != 200:
        return {"erro": f"Pedido não encontrado: {r.status_code}"}
    order = r.json()["order"]
    is_rmsp = _is_rmsp(order)
    if is_rmsp:
        return {"aviso": f"Pedido {order.get('name')} é RMSP — etiqueta ME não aplicável"}
    _generate_me_label(order)
    return {"ok": True, "pedido": order.get("name"), "cep": (order.get("shipping_address") or {}).get("zip")}


def _shopify_headers() -> dict:
    tok = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
    return {"X-Shopify-Access-Token": tok, "Content-Type": "application/json"}


def _checkout_converted(checkout_token: str) -> bool:
    """Verifica se o checkout foi convertido em pedido no Shopify."""
    store = os.getenv("SHOPIFY_STORE", "pknw4n-eg.myshopify.com")
    try:
        r = requests.get(
            f"https://{store}/admin/api/2024-01/orders.json",
            params={"cart_token": checkout_token, "status": "any", "limit": 1},
            headers=_shopify_headers(), timeout=15,
        )
        return bool(r.status_code == 200 and r.json().get("orders"))
    except Exception:
        return False


def _send_abandoned_cart_email(email: str, name: str, items: list, checkout_url: str, total: str):
    """Envia e-mail de recuperação de carrinho abandonado."""
    smtp_login = os.getenv("SMTP_USER", "b6e399001@smtp-brevo.com")   # login Brevo
    smtp_from  = os.getenv("SMTP_FROM", "atendimentoshinsei@gmail.com")  # remetente verificado
    smtp_pass  = os.getenv("SMTP_PASS", "")
    if not smtp_pass:
        print(f"[carrinho] SMTP_PASS não configurada — email não enviado para {email}")
        return False

    first_name = name.split()[0] if name else "cliente"

    # Montar lista de produtos
    items_html = ""
    for item in items[:5]:
        items_html += f"""
        <tr>
          <td style="padding:8px 0;border-bottom:1px solid #f0f0f0;font-size:14px;color:#333">
            {item.get('title','Produto')}
          </td>
          <td style="padding:8px 0;border-bottom:1px solid #f0f0f0;font-size:14px;color:#333;text-align:right">
            R$ {float(item.get('price',0)):,.2f}
          </td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:24px 0">
    <tr><td align="center">
      <table width="580" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;max-width:580px">
        <!-- Header -->
        <tr>
          <td style="background:#111;padding:24px 32px;text-align:center">
            <span style="color:#fff;font-size:22px;font-weight:700;letter-spacing:1px">SHINSEI MARKET</span>
          </td>
        </tr>
        <!-- Body -->
        <tr>
          <td style="padding:32px 32px 24px">
            <h2 style="margin:0 0 12px;font-size:20px;color:#111">Oi, {first_name}! Você esqueceu algo 😊</h2>
            <p style="margin:0 0 20px;font-size:15px;color:#555;line-height:1.6">
              Você deixou produtos no seu carrinho. Eles ainda estão te esperando — mas o estoque é limitado!
            </p>
            <!-- Itens -->
            <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px">
              <thead>
                <tr>
                  <th style="text-align:left;font-size:12px;color:#999;padding-bottom:8px;text-transform:uppercase;letter-spacing:.5px">Produto</th>
                  <th style="text-align:right;font-size:12px;color:#999;padding-bottom:8px;text-transform:uppercase;letter-spacing:.5px">Preço</th>
                </tr>
              </thead>
              <tbody>{items_html}</tbody>
              <tfoot>
                <tr>
                  <td style="padding-top:12px;font-size:15px;font-weight:700;color:#111">Total</td>
                  <td style="padding-top:12px;font-size:15px;font-weight:700;color:#111;text-align:right">R$ {total}</td>
                </tr>
              </tfoot>
            </table>
            <!-- CTA -->
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td align="center" style="padding:8px 0 24px">
                  <a href="{checkout_url}"
                     style="display:inline-block;background:#111;color:#fff;font-size:15px;font-weight:700;
                            padding:14px 36px;border-radius:6px;text-decoration:none;letter-spacing:.5px">
                    Finalizar Minha Compra →
                  </a>
                </td>
              </tr>
            </table>
            <!-- PIX badge -->
            <div style="background:#f0fdf4;border:1px solid #86efac;border-radius:6px;padding:12px 16px;margin-bottom:8px;font-size:14px;color:#15803d">
              💚 <strong>Pague no PIX ou Boleto e ganhe 5% de desconto adicional!</strong>
            </div>
          </td>
        </tr>
        <!-- Footer -->
        <tr>
          <td style="background:#f8f8f8;padding:16px 32px;text-align:center;font-size:12px;color:#aaa">
            Shinsei Market — Cosméticos Profissionais<br>
            Para não receber mais e-mails, <a href="mailto:atendimento@shinseimarket.com?subject=Cancelar+emails" style="color:#aaa">clique aqui</a>.
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["From"] = f"Shinsei Market <{smtp_from}>"
    msg["To"] = email
    msg["Subject"] = f"{first_name}, seu carrinho está te esperando 🛒"
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP("smtp-relay.brevo.com", 587, timeout=30) as server:
            server.starttls()
            server.login(smtp_login, smtp_pass)
            server.sendmail(smtp_from, email, msg.as_string())
        print(f"[carrinho] ✅ E-mail enviado para {email}")
        return True
    except Exception as e:
        print(f"[carrinho] ✗ Erro SMTP para {email}: {e}")
        return False


def _log_abandoned_cart(checkout_id: str, email: str, status: str):
    log_file = DATA_DIR / "carrinhos_abandonados.json"
    try:
        log = json.loads(log_file.read_text(encoding="utf-8")) if log_file.exists() else []
    except Exception:
        log = []
    # Atualizar entry existente ou criar nova
    for entry in log:
        if entry.get("checkout_id") == checkout_id:
            entry["status"] = status
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            log_file.write_text(json.dumps(log[-500:], ensure_ascii=False, indent=2), encoding="utf-8")
            return
    log.append({
        "checkout_id": checkout_id, "email": email, "status": status,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    log_file.write_text(json.dumps(log[-500:], ensure_ascii=False, indent=2), encoding="utf-8")


def _delayed_cart_recovery(checkout_id: str, checkout_token: str, email: str,
                            name: str, items: list, checkout_url: str, total: str,
                            delay_seconds: int = 3600):
    """Roda em thread separada: aguarda delay, verifica conversão, envia e-mail."""
    time.sleep(delay_seconds)
    if _checkout_converted(checkout_token):
        print(f"[carrinho] {checkout_id} — convertido em pedido, e-mail cancelado")
        _log_abandoned_cart(checkout_id, email, "convertido")
        return
    ok = _send_abandoned_cart_email(email, name, items, checkout_url, total)
    _log_abandoned_cart(checkout_id, email, "email_ok" if ok else "email_erro")


@router.post("/checkout-abandoned")
async def checkout_abandoned(
    request: Request,
    x_shopify_hmac_sha256: str = Header(default=""),
):
    """
    Recebe eventos checkouts/update do Shopify.
    Agenda e-mail de recuperação 1 hora após o abandono se o checkout não for convertido.
    """
    body = await request.body()

    if not _verify_hmac(body, x_shopify_hmac_sha256):
        raise HTTPException(status_code=401, detail="HMAC inválido")

    try:
        checkout = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido")

    email = (checkout.get("email") or "").strip().lower()
    if not email:
        return {"ok": True, "skipped": "sem email"}

    line_items = checkout.get("line_items") or []
    if not line_items:
        return {"ok": True, "skipped": "carrinho vazio"}

    checkout_id    = str(checkout.get("id", ""))
    checkout_token = checkout.get("token", "")
    checkout_url   = checkout.get("abandoned_checkout_url", "")
    total          = checkout.get("total_price", "0.00")
    customer       = checkout.get("customer") or {}
    name           = (customer.get("first_name", "") + " " + customer.get("last_name", "")).strip() or email

    items = [
        {"title": li.get("title", li.get("name", "Produto")), "price": li.get("price", "0")}
        for li in line_items
    ]

    # Evitar duplicatas: verificar se já agendamos para este checkout
    log_file = DATA_DIR / "carrinhos_abandonados.json"
    try:
        log = json.loads(log_file.read_text(encoding="utf-8")) if log_file.exists() else []
        if any(e.get("checkout_id") == checkout_id and e.get("status") in ("agendado", "email_ok") for e in log):
            return {"ok": True, "skipped": "já agendado"}
    except Exception:
        pass

    _log_abandoned_cart(checkout_id, email, "agendado")

    t = threading.Thread(
        target=_delayed_cart_recovery,
        args=(checkout_id, checkout_token, email, name, items, checkout_url, total),
        daemon=True,
    )
    t.start()

    print(f"[carrinho] {checkout_id} — agendado para {email} em 1h (itens={len(items)})")
    return {"ok": True, "checkout_id": checkout_id, "email": email, "agendado_em": "1h"}


@router.get("/carrinhos-abandonados")
def listar_carrinhos():
    """Lista os últimos carrinhos abandonados detectados."""
    log_file = DATA_DIR / "carrinhos_abandonados.json"
    if not log_file.exists():
        return {"carrinhos": []}
    return {"carrinhos": json.loads(log_file.read_text(encoding="utf-8"))[-50:]}


@router.post("/test-abandoned-email")
def test_abandoned_email(email: str = "fabiohideki08@gmail.com"):
    """Envia e-mail de teste de carrinho abandonado (sem HMAC, apenas para debug)."""
    ok = _send_abandoned_cart_email(
        email=email,
        name="Fabio Teste",
        items=[
            {"title": "Alfaparf Evolution Color 60g - 7.3 Louro Médio Dourado", "price": "29.90"},
            {"title": "Ox 20 Vol 900ml - Alfaparf", "price": "19.90"},
        ],
        checkout_url="https://shinseimarket.com.br/checkout/test",
        total="49,80",
    )
    return {"ok": ok, "email": email}


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
