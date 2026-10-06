"""
Taxas TikTok Shop Brasil (vigentes desde 15/07/2026).

| Preço do item   | Comissão | Taxa fixa      |
|-----------------|----------|----------------|
| abaixo de R$50  | 10%      | + R$4,00/item  |
| R$50 ou mais    | 6%       | + R$6,00/item  |

- Base de cálculo: preço após desconto do próprio vendedor (cupons TikTok não entram).
- Isenção para novos vendedores: 0% de comissão da plataforma por 60 dias (limite R$17.000 GMV).
- Sempre cobrados, inclusive na isenção (confirmado no extrato AKG de 05/10/2026, 41 linhas):
  taxa fixa R$4/item, SFP 6% e ~3% de taxa de transação sobre as vendas líquidas.

Fonte das taxas, em ordem:
1. API TikTok Shop (Partner API / Finance): extratos reais de pedidos liquidados → regressão
   taxa = comissão% × receita + taxa_fixa. Requer TIKTOK_APP_KEY, TIKTOK_APP_SECRET,
   TIKTOK_ACCESS_TOKEN (ou TIKTOK_REFRESH_TOKEN) e TIKTOK_SHOP_CIPHER. Cache 24h.
2. Tabela oficial acima, sobrescrevível por env var (TIKTOK_LIMIAR_FAIXA, TIKTOK_COMISSAO_BAIXA,
   TIKTOK_TAXA_FIXA_BAIXA, TIKTOK_COMISSAO_ALTA, TIKTOK_TAXA_FIXA_ALTA, TIKTOK_ISENCAO_ATIVA).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

TIKTOK_CANAIS = ("TikTok Shop", "TikTok Shop AKG")

TTS_API = "https://open-api.tiktokglobalshop.com"
TTS_AUTH = "https://auth.tiktok-shops.com"
_CACHE_PATH = Path(__file__).parent / "data" / "tiktok_taxas_cache.json"
_CACHE_TTL = 24 * 3600
_MIN_AMOSTRAS = 5


# ── Partner API ───────────────────────────────────────────────────────────────

def _credenciais() -> dict:
    return {
        "app_key": os.getenv("TIKTOK_APP_KEY", "").strip(),
        "app_secret": os.getenv("TIKTOK_APP_SECRET", "").strip(),
        "access_token": os.getenv("TIKTOK_ACCESS_TOKEN", "").strip(),
        "refresh_token": os.getenv("TIKTOK_REFRESH_TOKEN", "").strip(),
        "shop_cipher": os.getenv("TIKTOK_SHOP_CIPHER", "").strip(),
    }


def api_configurada() -> bool:
    c = _credenciais()
    return bool(c["app_key"] and c["app_secret"] and c["shop_cipher"] and (c["access_token"] or c["refresh_token"]))


def _assinar(path: str, params: dict, app_secret: str, body: str = "") -> str:
    """Assinatura TikTok Shop: HMAC-SHA256(secret, secret + path + k1v1k2v2... + body + secret)."""
    base = path + "".join(f"{k}{params[k]}" for k in sorted(params) if k not in ("sign", "access_token"))
    base = app_secret + base + body + app_secret
    return hmac.new(app_secret.encode(), base.encode(), hashlib.sha256).hexdigest()


def _renovar_access_token(c: dict) -> str:
    r = requests.get(f"{TTS_AUTH}/api/v2/token/refresh", params={
        "app_key": c["app_key"], "app_secret": c["app_secret"],
        "refresh_token": c["refresh_token"], "grant_type": "refresh_token",
    }, timeout=20)
    data = (r.json() or {}).get("data") or {}
    token = data.get("access_token") or ""
    if token:
        os.environ["TIKTOK_ACCESS_TOKEN"] = token
        if data.get("refresh_token"):
            os.environ["TIKTOK_REFRESH_TOKEN"] = data["refresh_token"]
        try:
            from render_persistence import _patch_env_vars  # mesmo padrão dos tokens Bling/ML/Shopee
            _patch_env_vars({"TIKTOK_ACCESS_TOKEN": token, "TIKTOK_REFRESH_TOKEN": data.get("refresh_token") or c["refresh_token"]})
        except Exception as exc:
            logger.warning("tiktok: não persistiu token no Render: %s", exc)
    return token


def _get(path: str, params: dict | None = None, _retry: bool = True) -> dict:
    c = _credenciais()
    if not c["access_token"] and c["refresh_token"]:
        c["access_token"] = _renovar_access_token(c)
    q = {"app_key": c["app_key"], "timestamp": str(int(time.time())), "shop_cipher": c["shop_cipher"], **(params or {})}
    q["sign"] = _assinar(path, q, c["app_secret"])
    r = requests.get(TTS_API + path, params=q, headers={"x-tts-access-token": c["access_token"], "Content-Type": "application/json"}, timeout=30)
    js = r.json() if r.content else {}
    # 105002 = access token expirado
    if js.get("code") in (105002, 36004004) and _retry and c["refresh_token"]:
        _renovar_access_token(c)
        return _get(path, params, _retry=False)
    if js.get("code") not in (0, None):
        raise RuntimeError(f"TikTok API {path}: {js.get('code')} {js.get('message')}")
    return js.get("data") or {}


def _amostras_extratos(max_extratos: int = 10) -> list[tuple[float, float]]:
    """[(receita, taxa)] por transação de pedido nos extratos mais recentes."""
    stmts = _get("/finance/202309/statements", {"sort_field": "statement_time", "sort_order": "DESC", "page_size": str(max_extratos)})
    amostras = []
    for st in (stmts.get("statements") or [])[:max_extratos]:
        token = ""
        for _ in range(5):
            params = {"sort_field": "order_create_time", "page_size": "100"}
            if token:
                params["page_token"] = token
            d = _get(f"/finance/202309/statements/{st['id']}/statement_transactions", params)
            for t in d.get("statement_transactions") or d.get("transactions") or []:
                if (t.get("type") or "ORDER").upper() != "ORDER":
                    continue
                receita = float(t.get("revenue_amount") or 0)
                taxa = abs(float(t.get("fee_amount") or 0))
                if receita > 0:
                    amostras.append((receita, taxa))
            token = d.get("next_page_token") or ""
            if not token:
                break
    return amostras


def _regressao(amostras: list[tuple[float, float]], limiar: float) -> list[dict] | None:
    """Ajusta taxa = pct × receita + fixo em cada faixa (abaixo/acima do limiar)."""
    faixas = []
    for lo, hi in ((0.0, limiar - 0.01), (limiar, float("inf"))):
        pts = [(x, y) for x, y in amostras if lo <= x <= hi]
        if len(pts) < _MIN_AMOSTRAS:
            return None
        n = len(pts)
        mx = sum(x for x, _ in pts) / n
        my = sum(y for _, y in pts) / n
        var = sum((x - mx) ** 2 for x, _ in pts)
        pct = sum((x - mx) * (y - my) for x, y in pts) / var if var else my / mx
        fixo = my - pct * mx
        faixas.append({"preco_min": lo, "preco_max": hi, "comissao_pct": round(max(pct, 0), 4),
                       "taxa_fixa": round(max(fixo, 0), 2), "amostras": n})
    return faixas


def _faixas_api() -> list[dict] | None:
    """Faixas derivadas da API (cache 24h). None se API indisponível ou amostra insuficiente."""
    try:
        cache = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if time.time() - cache.get("ts", 0) < _CACHE_TTL:
            return cache.get("faixas")
    except Exception:
        pass
    if not api_configurada():
        return None
    try:
        amostras = _amostras_extratos()
        faixas = _regressao(amostras, _env_float("TIKTOK_LIMIAR_FAIXA", 50.0))
        if faixas and faixas[-1]["preco_max"] == float("inf"):
            faixas[-1]["preco_max"] = 1e12  # JSON não serializa inf
        _CACHE_PATH.parent.mkdir(exist_ok=True)
        _CACHE_PATH.write_text(json.dumps({"ts": time.time(), "faixas": faixas, "amostras": len(amostras)}), encoding="utf-8")
        return faixas
    except Exception as exc:
        logger.warning("tiktok: falha ao buscar taxas via API, usando tabela: %s", exc)
        return None


def status_fonte() -> dict:
    if not api_configurada():
        return {"disponivel": False, "fonte": "tabela_br"}
    return {"disponivel": True, "fonte": "api_extratos" if _faixas_api() else "tabela_br"}


def _env_float(nome: str, default: float) -> float:
    try:
        return float(os.getenv(nome, default))
    except (TypeError, ValueError):
        return default


def tabela_tiktok() -> dict:
    return {
        "limiar": _env_float("TIKTOK_LIMIAR_FAIXA", 50.0),
        "comissao_baixa": _env_float("TIKTOK_COMISSAO_BAIXA", 0.10),
        "taxa_fixa_baixa": _env_float("TIKTOK_TAXA_FIXA_BAIXA", 4.0),
        "comissao_alta": _env_float("TIKTOK_COMISSAO_ALTA", 0.06),
        "taxa_fixa_alta": _env_float("TIKTOK_TAXA_FIXA_ALTA", 6.0),
        "isencao_ativa": os.getenv("TIKTOK_ISENCAO_ATIVA", "false").strip().lower() in ("1", "true", "sim", "yes"),
        # Cobranças sobre vendas líquidas que valem mesmo na isenção (extrato AKG 05/10/2026):
        "sfp_pct": _env_float("TIKTOK_SFP_PCT", 0.06),               # Programa de frete (SFP)
        "transacao_pct": _env_float("TIKTOK_TRANSACAO_PCT", 0.03),   # taxa de transação (não detalhada no extrato)
    }


def faixas_tiktok(isento: bool | None = None) -> list[dict]:
    """
    Faixas de taxa em ordem crescente de preço: [{preco_min, preco_max, comissao_pct, taxa_fixa}].
    `comissao_pct` é o percentual total (comissão + SFP + transação). A isenção de novo
    vendedor zera só a comissão da plataforma — taxa fixa, SFP e transação continuam.
    """
    t = tabela_tiktok()
    if isento is None:
        isento = t["isencao_ativa"]
    api = _faixas_api()
    if api and not isento:
        return [{**f, "source": "api_tiktok"} for f in api]
    extras = t["sfp_pct"] + t["transacao_pct"]
    source = "tiktok_isencao" if isento else "tabela_tiktok_br"
    faixas = [
        (0.0, t["limiar"] - 0.01, t["comissao_baixa"], t["taxa_fixa_baixa"]),
        (t["limiar"], float("inf"), t["comissao_alta"], t["taxa_fixa_alta"]),
    ]
    return [
        {"preco_min": lo, "preco_max": hi, "comissao_pct": (0.0 if isento else com) + extras,
         "comissao_plataforma_pct": 0.0 if isento else com, "sfp_pct": t["sfp_pct"],
         "transacao_pct": t["transacao_pct"], "taxa_fixa": fixa, "source": source}
        for lo, hi, com, fixa in faixas
    ]


def get_tiktok_taxa(preco: float, isento: bool | None = None) -> dict:
    """Taxa TikTok aplicável a um preço de venda."""
    preco = float(preco or 0)
    faixas = faixas_tiktok(isento)
    return next((f for f in faixas if preco <= f["preco_max"]), faixas[-1])


def resolver_preco_tiktok(resolver, isento: bool | None = None) -> tuple[float, dict]:
    """
    Resolve o preço respeitando a troca de faixa em R$50 sem oscilar.

    `resolver(comissao_pct, taxa_fixa) -> preco` calcula o preço para uma faixa fixa.
    Testa cada faixa em ordem e devolve o primeiro preço que cai dentro dela. Se nenhum
    cai (lacuna entre faixas), usa o piso da faixa seguinte — preço mínimo que cumpre o alvo.
    """
    faixas = faixas_tiktok(isento)
    for f in faixas:
        preco = resolver(f["comissao_pct"], f["taxa_fixa"])
        if preco <= f["preco_max"]:
            if preco >= f["preco_min"]:
                return preco, f
            # Preço caiu abaixo desta faixa: a faixa anterior já o rejeitou → lacuna, usa o piso
            return f["preco_min"], f
    f = faixas[-1]
    return resolver(f["comissao_pct"], f["taxa_fixa"]), f
