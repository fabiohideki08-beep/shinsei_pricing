"""
routes/frete.py — FastAPI routes para cálculo de frete (Shinsei Market / Shopify)

Endpoints:
  POST /frete/shopify-callback   — Shopify Carrier Service webhook
  GET  /frete/calcular           — Chamado pelo widget de carrinho via AJAX
  GET  /frete/progresso          — Cálculo rápido sem CEP para updates em tempo real
  GET  /frete/painel             — Página HTML de gestão
  GET  /frete/config             — Leitura da configuração
  PUT  /frete/config             — Escrita da configuração
  GET  /frete/historico          — Últimas N entradas do histórico
  GET  /frete/simular            — Simulação avulsa (sem salvar no histórico)
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from services.frete import (
    SUBSIDY_PER_ITEM,
    SUBSIDY_FIRST_ITEM,
    FreightResult,
    calculate_freight,
    calcular_subsidio_total,
    carregar_config_frete,
    salvar_config_frete,
    registrar_historico_frete,
    ler_historico_frete,
    normalize_cep,
)

logger = logging.getLogger("shinsei.frete.routes")

router = APIRouter(prefix="/frete", tags=["frete"])

_PAGES_DIR = Path(__file__).parent.parent / "pages"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _isodate(days_from_now: int = 0) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=days_from_now)
    return dt.strftime("%Y-%m-%dT00:00:00Z")


def _cents(value_brl: float) -> str:
    """Converte reais para centavos como string (formato Shopify)."""
    return str(int(round(value_brl * 100)))


def _service_code(name: str) -> str:
    return name.lower().replace(" ", "_").replace("-", "_")


def _shopify_service_name(option_name: str, is_free: bool) -> str:
    if is_free:
        return f"{option_name} — Frete Grátis 🎉"
    return option_name


def _shopify_rates_from_result(result: FreightResult) -> list[dict]:
    rates = []
    for opt in result.options:
        rates.append(
            {
                "service_name": _shopify_service_name(opt.name, opt.is_free),
                "service_code": _service_code(opt.name) + ("_gratis" if opt.is_free else ""),
                "total_price": _cents(opt.price_final),
                "currency": "BRL",
                "min_delivery_date": _isodate(opt.delivery_days),
                "max_delivery_date": _isodate(opt.delivery_days + 2),
                "description": (
                    "Frete grátis com subsídio Shinsei!"
                    if opt.is_free
                    else f"Subsídio R${opt.subsidy:.2f} aplicado"
                ),
            }
        )
    return rates


# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------


class ShopifyOrigin(BaseModel):
    postal_code: str | None = None
    country: str | None = None
    province: str | None = None
    city: str | None = None

    class Config:
        extra = "allow"


class ShopifyDestination(BaseModel):
    postal_code: str | None = None
    country: str | None = None
    province: str | None = None
    city: str | None = None

    class Config:
        extra = "allow"


class ShopifyItem(BaseModel):
    name: str | None = None
    quantity: int = 1
    grams: int = 300
    price: int = 0  # centavos

    class Config:
        extra = "allow"


class ShopifyRateRequest(BaseModel):
    origin: ShopifyOrigin | None = None
    destination: ShopifyDestination | None = None
    items: list[ShopifyItem] = []
    currency: str = "BRL"

    class Config:
        extra = "allow"


class ShopifyCallbackBody(BaseModel):
    rate: ShopifyRateRequest | None = None

    class Config:
        extra = "allow"


# ---------------------------------------------------------------------------
# POST /frete/shopify-callback
# ---------------------------------------------------------------------------


@router.post("/shopify-callback")
async def shopify_callback(body: ShopifyCallbackBody) -> dict:
    """
    Webhook do Shopify Carrier Service.
    Recebe o payload de rate request e retorna tarifas ajustadas com subsídio.
    """
    rate_req = body.rate
    if rate_req is None:
        raise HTTPException(status_code=400, detail="Campo 'rate' ausente no payload.")

    destination = rate_req.destination or ShopifyDestination()
    dest_cep = normalize_cep(destination.postal_code or "")

    if not dest_cep or len(dest_cep) < 8:
        logger.warning("shopify-callback: CEP de destino inválido: %s", destination.postal_code)
        raise HTTPException(status_code=422, detail="CEP de destino inválido ou ausente.")

    items = rate_req.items or []
    qty_items = sum(max(1, i.quantity) for i in items)
    total_grams = sum(max(100, i.grams) * max(1, i.quantity) for i in items)
    total_weight_kg = total_grams / 1000.0
    order_value_brl = sum((i.price / 100.0) * max(1, i.quantity) for i in items)

    logger.info(
        "shopify-callback: dest=%s qty=%d peso=%.3fkg valor=R$%.2f",
        dest_cep, qty_items, total_weight_kg, order_value_brl,
    )

    try:
        result = await calculate_freight(dest_cep, qty_items, total_weight_kg, order_value_brl)
    except Exception as exc:
        logger.error("shopify-callback: erro no cálculo de frete: %s", exc)
        return {
            "rates": [
                {
                    "service_name": "Frete Padrão",
                    "service_code": "frete_padrao",
                    "total_price": "1800",
                    "currency": "BRL",
                    "min_delivery_date": _isodate(5),
                    "max_delivery_date": _isodate(8),
                }
            ]
        }

    # Salva no histórico via serviço
    try:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "dest_cep": dest_cep,
            "city": result.city,
            "state": result.state,
            "rmsp": result.is_rmsp,
            "qty": qty_items,
            "peso_kg": round(total_weight_kg, 3),
            "valor": round(order_value_brl, 2),
            "subsidio": result.subsidy_total,
            "gratis": result.is_free,
            "opcoes": [
                {"nome": o.name, "real": o.price_real, "final": o.price_final}
                for o in result.options
            ],
            "frete_min_real": min((o.price_real for o in result.options), default=0),
            "frete_min_final": result.cheapest_final,
        }
        registrar_historico_frete(entry)
    except Exception:
        pass

    rates = _shopify_rates_from_result(result)

    if not rates:
        rates = [
            {
                "service_name": "Frete Padrão",
                "service_code": "frete_padrao",
                "total_price": "1800",
                "currency": "BRL",
                "min_delivery_date": _isodate(5),
                "max_delivery_date": _isodate(8),
            }
        ]

    return {"rates": rates}


# ---------------------------------------------------------------------------
# GET /frete/calcular
# ---------------------------------------------------------------------------


@router.get("/calcular", response_model=FreightResult)
async def calcular_frete(
    cep: str = Query(..., description="CEP de destino (8 dígitos)"),
    qty: int = Query(1, ge=1, description="Quantidade de itens no carrinho"),
    peso: float = Query(0.3, ge=0.01, description="Peso total em kg"),
    valor: float = Query(0.0, ge=0.0, description="Valor total do pedido em R$"),
) -> FreightResult:
    """
    Chamado pelo widget do carrinho via AJAX.
    Retorna FreightResult JSON completo.
    """
    dest_cep = normalize_cep(cep)
    if len(dest_cep) < 8:
        raise HTTPException(status_code=422, detail="CEP inválido. Informe 8 dígitos.")

    try:
        result = await calculate_freight(dest_cep, qty, peso, valor)
    except Exception as exc:
        logger.error("GET /frete/calcular: %s", exc)
        raise HTTPException(status_code=500, detail=f"Erro no cálculo de frete: {exc}")

    return result


# ---------------------------------------------------------------------------
# GET /frete/progresso
# ---------------------------------------------------------------------------


@router.get("/progresso")
async def progresso_frete(
    qty: int = Query(1, ge=1, description="Quantidade de itens no carrinho"),
    frete_real: float = Query(0.0, ge=0.0, description="Frete real estimado em R$ (sem subsídio). 0 = usa o default da config."),
) -> dict:
    """
    Cálculo rápido sem CEP para updates em tempo real no widget.
    Usa o frete_real fornecido ou o default da config (R$18).
    Regra de subsídio lida dinamicamente da configuração.
    """
    cfg = carregar_config_frete()
    subsidio_primeiro = float(cfg.get("subsidio_primeiro_item", SUBSIDY_FIRST_ITEM))
    subsidio_por_item = float(cfg.get("subsidio_por_item", SUBSIDY_PER_ITEM))
    frete_real_default = float(cfg.get("frete_real_default", 18.0))

    if frete_real <= 0:
        frete_real = frete_real_default

    subsidio = calcular_subsidio_total(qty, cfg)
    frete_final = max(0.0, round(frete_real - subsidio, 2))
    eh_gratis = frete_final == 0.0

    if eh_gratis:
        itens_faltando = 0
        mensagem = "🎉 Parabéns! Você ganhou frete grátis!"
    else:
        if frete_real <= subsidio_primeiro:
            itens_necessarios = 1
        else:
            itens_necessarios = 1 + math.ceil((frete_real - subsidio_primeiro) / subsidio_por_item)
        itens_faltando = max(0, itens_necessarios - qty)
        if itens_faltando == 1:
            mensagem = "Adicione 1 item para frete grátis!"
        else:
            mensagem = f"Adicione {itens_faltando} itens para frete grátis!"

    progresso_pct = min(100.0, round((subsidio / frete_real * 100) if frete_real > 0 else 100.0, 1))

    return {
        "qty_items": qty,
        "frete_real": frete_real,
        "subsidio": subsidio,
        "subsidio_primeiro_item": subsidio_primeiro,
        "subsidio_por_item": subsidio_por_item,
        "frete_final": frete_final,
        "itens_faltando": itens_faltando,
        "eh_gratis": eh_gratis,
        "progresso_pct": progresso_pct,
        "mensagem": mensagem,
    }


# ---------------------------------------------------------------------------
# GET /frete/painel  — Página HTML de gestão
# ---------------------------------------------------------------------------


@router.get("/painel", response_class=HTMLResponse)
async def frete_painel():
    html_file = _PAGES_DIR / "frete_painel.html"
    if html_file.exists():
        return HTMLResponse(html_file.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="Página frete_painel.html não encontrada.")


# ---------------------------------------------------------------------------
# GET/PUT /frete/config  — Leitura e escrita da configuração
# ---------------------------------------------------------------------------


@router.get("/config")
async def get_frete_config() -> dict:
    return carregar_config_frete()


class FreteConfigPayload(BaseModel):
    subsidio_primeiro_item: float
    subsidio_por_item: float
    frete_real_default: float
    cep_origem: str = "06036003"


@router.put("/config")
async def put_frete_config(payload: FreteConfigPayload) -> dict:
    cfg = {
        "subsidio_primeiro_item": payload.subsidio_primeiro_item,
        "subsidio_por_item": payload.subsidio_por_item,
        "frete_real_default": payload.frete_real_default,
        "cep_origem": payload.cep_origem,
    }
    salvar_config_frete(cfg)
    return {"ok": True, "config": cfg}


# ---------------------------------------------------------------------------
# GET /frete/historico  — Últimas N entradas do histórico
# ---------------------------------------------------------------------------


@router.get("/historico")
async def get_frete_historico(limit: int = Query(100, ge=1, le=500)) -> dict:
    try:
        historico = ler_historico_frete(limit)
        return {"total": len(historico), "itens": historico}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# GET /frete/simular  — Simulação avulsa (sem salvar no histórico)
# ---------------------------------------------------------------------------


@router.get("/simular")
async def simular_frete(
    cep: str = Query(...),
    qty: int = Query(1, ge=1),
    peso: float = Query(0.3),
    valor: float = Query(50.0),
) -> dict:
    dest_cep = normalize_cep(cep)
    if len(dest_cep) < 8:
        raise HTTPException(status_code=422, detail="CEP inválido.")
    result = await calculate_freight(dest_cep, qty, peso, valor)
    cfg = carregar_config_frete()
    return {
        "city": result.city,
        "state": result.state,
        "rmsp": result.is_rmsp,
        "subsidio_total": result.subsidy_total,
        "is_free": result.is_free,
        "items_for_free": result.items_for_free_shipping,
        "config": cfg,
        "opcoes": [
            {
                "nome": o.name,
                "carrier": o.carrier,
                "real": o.price_real,
                "final": o.price_final,
                "gratis": o.is_free,
                "dias": o.delivery_days,
            }
            for o in result.options
        ],
    }
