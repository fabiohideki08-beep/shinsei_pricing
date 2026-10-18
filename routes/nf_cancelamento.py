"""
Cancelamento automático de NF-e quando pedido é cancelado no marketplace.

Fluxo:
  1. Busca pedidos cancelados no Bling (situação 12 = cancelado) dos últimos N dias
  2. Para cada pedido, busca NF vinculada com situação "autorizada" (3)
  3. Chama PUT /notas-fiscais/{id}/cancelar
  4. Registra tudo em data/nf_cancelamentos_log.jsonl

Endpoints:
  GET  /nf/cancelamentos          — histórico e status
  POST /nf/cancelar-automatico    — trigger manual (também roda no scheduler)

Bling NF situações (v3):
  1 = Pendente  2 = Em processo  3 = Autorizada  4 = Cancelada  5 = Denegada

Bling pedido situações (v3):
  1=Em Aberto  5=Em Andamento  6=Verificado  7=Atrasado  9=Cancelado  15=Em Aberto (outro)
  Obs: situação 12 pode ser "Cancelado" — validado via filtro.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests as _req
from fastapi import APIRouter, Depends, Query

from auth import verificar_api_key

logger = logging.getLogger(__name__)
router = APIRouter()

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
LOG_PATH = DATA_DIR / "nf_cancelamentos_log.jsonl"

BLING_BASE = "https://api.bling.com.br/Api/v3"

# Situação NF autorizada no Bling v3
NF_SITUACAO_AUTORIZADA = 3
NF_SITUACAO_CANCELADA  = 4

# Justificativa mínima de 15 chars exigida pelo Bling/SEFAZ
JUSTIFICATIVA = "Pedido cancelado no marketplace pelo comprador"

# ─── Helpers de token ────────────────────────────────────────────────────────

def _get_token(empresa: str) -> str:
    if empresa == "akg":
        token = os.getenv("BLING_AKG_ACCESS_TOKEN", "")
        if not token:
            akg_path = DATA_DIR / "bling_tokens_akg.json"
            if akg_path.exists():
                try:
                    token = json.loads(akg_path.read_text("utf-8")).get("access_token", "")
                except Exception:
                    pass
        return token
    else:
        token = os.getenv("BLING_ACCESS_TOKEN", "")
        if not token:
            try:
                from bling_client import BlingClient
                c = BlingClient()
                token = c.tokens.get("access_token", "") if hasattr(c, "tokens") else ""
            except Exception:
                pass
        return token


def _headers(empresa: str) -> dict:
    token = _get_token(empresa)
    return {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
        "enable-jwt":    "1",
    }

# ─── Lógica principal ────────────────────────────────────────────────────────

def _buscar_pedidos_cancelados(empresa: str, dias: int = 7) -> list[dict]:
    """Retorna pedidos cancelados no Bling dos últimos `dias` dias."""
    hdrs = _headers(empresa)
    data_ini = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d %H:%M:%S")
    resultados = []
    pagina = 1

    while True:
        try:
            r = _req.get(
                f"{BLING_BASE}/pedidos/vendas",
                headers=hdrs,
                params={
                    "pagina": pagina,
                    "limite": 100,
                    "idsSituacoes[]": 12,       # 12 = cancelado Bling v3
                    "dataInicial": data_ini,
                },
                timeout=20,
            )
        except Exception as exc:
            logger.warning("[NF-Cancel] %s: erro ao buscar pedidos pág %d: %s", empresa, pagina, exc)
            break

        if r.status_code == 401:
            logger.warning("[NF-Cancel] %s: token inválido", empresa)
            break
        if not r.is_ok:
            logger.warning("[NF-Cancel] %s: HTTP %d ao buscar pedidos", empresa, r.status_code)
            break

        data = r.json().get("data", [])
        if not data:
            break
        resultados.extend(data)
        if len(data) < 100:
            break
        pagina += 1
        time.sleep(0.2)   # rate limit 60/min

    return resultados


def _buscar_nf_por_pedido(pedido_id: int, empresa: str) -> list[dict]:
    """Retorna NFs vinculadas ao pedido que estão autorizadas."""
    hdrs = _headers(empresa)
    try:
        r = _req.get(
            f"{BLING_BASE}/notas-fiscais",
            headers=hdrs,
            params={"idPedidoVenda": pedido_id, "limite": 10, "situacoes[]": NF_SITUACAO_AUTORIZADA},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("data", [])
    except Exception as exc:
        logger.warning("[NF-Cancel] erro ao buscar NF do pedido %d (%s): %s", pedido_id, empresa, exc)
    return []


def _cancelar_nf(nf_id: int, empresa: str) -> dict:
    """Cancela uma NF no Bling. Retorna resultado."""
    hdrs = _headers(empresa)
    try:
        r = _req.put(
            f"{BLING_BASE}/notas-fiscais/{nf_id}/cancelar",
            headers=hdrs,
            json={"justificativa": JUSTIFICATIVA},
            timeout=20,
        )
        return {"status": r.status_code, "ok": r.status_code in (200, 204), "body": r.text[:300]}
    except Exception as exc:
        return {"status": 0, "ok": False, "body": str(exc)}


def _log_entrada(entrada: dict) -> None:
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entrada, ensure_ascii=False) + "\n")


def executar_cancelamento_automatico(dias: int = 7, dry_run: bool = False) -> dict:
    """
    Executa o ciclo de cancelamento automático.
    dry_run=True apenas identifica, não cancela.
    """
    timestamp = datetime.now().isoformat()
    resultado = {
        "timestamp": timestamp,
        "dry_run":   dry_run,
        "dias":      dias,
        "empresas":  {},
        "total_pedidos_cancelados": 0,
        "total_nfs_encontradas":   0,
        "total_nfs_canceladas":    0,
        "total_nfs_erro":          0,
        "acoes":                   [],
    }

    for empresa in ("shinsei", "akg"):
        token = _get_token(empresa)
        if not token:
            resultado["empresas"][empresa] = "token_indisponivel"
            logger.warning("[NF-Cancel] %s: sem token, pulando", empresa)
            continue

        pedidos = _buscar_pedidos_cancelados(empresa, dias=dias)
        resultado["total_pedidos_cancelados"] += len(pedidos)
        resultado["empresas"][empresa] = f"{len(pedidos)} pedidos cancelados"

        for pedido in pedidos:
            ped_id   = pedido.get("id")
            ped_num  = pedido.get("numero") or pedido.get("numeroPedido") or ped_id
            canal    = (pedido.get("contato", {}) or {}).get("nome", "")

            if not ped_id:
                continue

            nfs = _buscar_nf_por_pedido(ped_id, empresa)
            if not nfs:
                continue

            for nf in nfs:
                nf_id     = nf.get("id")
                nf_numero = nf.get("numero") or nf_id
                resultado["total_nfs_encontradas"] += 1

                acao = {
                    "empresa":     empresa,
                    "pedido_id":   ped_id,
                    "pedido_num":  ped_num,
                    "nf_id":       nf_id,
                    "nf_numero":   nf_numero,
                    "dry_run":     dry_run,
                    "timestamp":   timestamp,
                }

                if dry_run:
                    acao["status"] = "identificado"
                    logger.info("[NF-Cancel] DRY-RUN: pedido %s → NF %s (%s)", ped_num, nf_numero, empresa)
                else:
                    res = _cancelar_nf(nf_id, empresa)
                    acao["status"]        = "cancelada" if res["ok"] else "erro"
                    acao["bling_status"]  = res["status"]
                    acao["bling_body"]    = res["body"]
                    if res["ok"]:
                        resultado["total_nfs_canceladas"] += 1
                        logger.info("[NF-Cancel] NF %s do pedido %s (%s): cancelada ✅", nf_numero, ped_num, empresa)
                    else:
                        resultado["total_nfs_erro"] += 1
                        logger.warning("[NF-Cancel] NF %s do pedido %s (%s): erro %d %s",
                                       nf_numero, ped_num, empresa, res["status"], res["body"])
                    time.sleep(0.3)

                resultado["acoes"].append(acao)
                _log_entrada(acao)

    return resultado

# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.post("/nf/cancelar-automatico")
def cancelar_automatico_endpoint(
    dias:    int  = Query(7,     description="Janela de busca em dias"),
    dry_run: bool = Query(False, description="Apenas identificar, não cancelar"),
    _key: str = Depends(verificar_api_key),
):
    """
    Executa cancelamento automático de NF-e para pedidos cancelados.
    Use dry_run=true para verificar quais NFs seriam afetadas sem cancelar.
    """
    return executar_cancelamento_automatico(dias=dias, dry_run=dry_run)


@router.get("/nf/cancelamentos")
def historico_cancelamentos(
    limite:  int = Query(100, description="Últimas N entradas do log"),
    empresa: Optional[str] = Query(None, description="shinsei | akg"),
    _key: str = Depends(verificar_api_key),
):
    """Retorna histórico de cancelamentos de NF."""
    if not LOG_PATH.exists():
        return {"total": 0, "entradas": []}

    linhas = LOG_PATH.read_text("utf-8").splitlines()
    entradas = []
    for linha in reversed(linhas):
        if not linha.strip():
            continue
        try:
            entrada = json.loads(linha)
        except Exception:
            continue
        if empresa and entrada.get("empresa") != empresa:
            continue
        entradas.append(entrada)
        if len(entradas) >= limite:
            break

    return {
        "total":   len(linhas),
        "exibindo": len(entradas),
        "entradas": entradas,
    }
