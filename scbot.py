"""
scbot.py — Shinsei SCBOT: Robô de Indexação Google

Submete URLs ao Google Indexing API diariamente às 09:00.
Estratégia inteligente: prioriza produtos atualizados, usa categorias do dia
da semana (ex: maquiagem/esmaltes às sextas para buscas de fim de semana).

Configuração necessária:
  data/google_service_account.json — chave da conta de serviço Google Cloud
  (deve ter permissão de Proprietário no Search Console)

Quota: 200 URLs/dia (limite Google Indexing API gratuito)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
CREDENTIALS_PATH = DATA_DIR / "google_service_account.json"
LOG_PATH = DATA_DIR / "scbot_log.json"
QUEUE_PATH = DATA_DIR / "scbot_queue.json"

SHOPIFY_BASE_URL = "https://www.shinseimarket.com.br"
DAILY_QUOTA = 200

# Prioridade de URLs por dia da semana (0=seg ... 6=dom)
# Sexta e sábado: foco em maquiagem, esmaltes e perfumaria (buscas de fim de semana)
WEEKDAY_FOCUS = {
    0: ["coloracao", "tratamento", "shampoo"],       # Segunda
    1: ["progressiva", "descolorante", "condicionador"],  # Terça
    2: ["kit", "tratamento", "oleo_capilar"],         # Quarta
    3: ["skin_care", "hidratante", "sabonete"],        # Quinta
    4: ["maquiagem", "esmalte", "perfume"],            # Sexta
    5: ["maquiagem", "esmalte", "perfume", "higiene_feminina"],  # Sábado
    6: ["coloracao", "tratamento", "kit"],             # Domingo
}


# ── Autenticação Google ────────────────────────────────────────────────────

def _get_access_token() -> Optional[str]:
    """Obtém token de acesso via service account JSON."""
    if not CREDENTIALS_PATH.exists():
        logger.error("SCBOT: credenciais não encontradas em %s", CREDENTIALS_PATH)
        return None

    try:
        import google.oauth2.service_account as sa
        import google.auth.transport.requests as ga_req

        credentials = sa.Credentials.from_service_account_file(
            str(CREDENTIALS_PATH),
            scopes=["https://www.googleapis.com/auth/indexing"],
        )
        credentials.refresh(ga_req.Request())
        return credentials.token
    except ImportError:
        logger.error("SCBOT: instale google-auth: pip install google-auth")
        return None
    except Exception as e:
        logger.error("SCBOT: erro ao autenticar: %s", e)
        return None


# ── Envio de URLs ──────────────────────────────────────────────────────────

def _submit_url(url: str, token: str, tipo: str = "URL_UPDATED") -> bool:
    """Submete uma URL ao Google Indexing API."""
    import requests as req
    try:
        r = req.post(
            "https://indexing.googleapis.com/v3/urlNotifications:publish",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"url": url, "type": tipo},
            timeout=15,
        )
        if r.status_code == 200:
            return True
        elif r.status_code == 429:
            logger.warning("SCBOT: rate limit atingido")
            return False
        else:
            logger.warning("SCBOT: erro %d para %s — %s", r.status_code, url, r.text[:100])
            return False
    except Exception as e:
        logger.error("SCBOT: exceção ao submeter %s: %s", url, e)
        return False


# ── Montagem da fila de URLs ───────────────────────────────────────────────

def _carregar_produtos() -> list:
    cache = DATA_DIR / "all_products_cache.json"
    if not cache.exists():
        return []
    try:
        return json.loads(cache.read_text(encoding="utf-8"))
    except Exception:
        return []


def _montar_fila(quota: int = DAILY_QUOTA) -> list[str]:
    """
    Monta fila inteligente de URLs respeitando o quota diário.
    Ordem de prioridade:
      1. Páginas institucionais (sobre, marcas, blog)
      2. Coleções do foco do dia da semana
      3. Produtos das categorias prioritárias do dia
      4. Demais produtos em rodízio
    """
    hoje = date.today().weekday()  # 0=seg ... 6=dom
    focus_labels = WEEKDAY_FOCUS.get(hoje, ["coloracao", "tratamento"])

    urls: list[str] = []

    # 1. Páginas institucionais (sempre)
    paginas = [
        f"{SHOPIFY_BASE_URL}/",
        f"{SHOPIFY_BASE_URL}/pages/sobre",
        f"{SHOPIFY_BASE_URL}/pages/marcas",
        f"{SHOPIFY_BASE_URL}/pages/contact",
        f"{SHOPIFY_BASE_URL}/blogs/novidades",
    ]
    urls.extend(paginas)

    # 2. Coleções
    colecoes = [
        "coloridos-e-platinados", "tratamentos", "mais-vendidos",
        "frageis-e-quebradicos", "lisos-e-longos", "secos-e-sem-brilho",
        "oleosos-e-com-caspa", "igora-zero-amm",
    ]
    for c in colecoes:
        urls.append(f"{SHOPIFY_BASE_URL}/collections/{c}")

    # 3. Produtos por prioridade do dia
    produtos = _carregar_produtos()
    if produtos:
        # Primeiro os do foco do dia
        prioritarios = [
            p for p in produtos
            if _label_categoria(p.get("product_type", "")) in focus_labels
        ]
        # Depois os demais em rodízio pelo dia do ano
        outros = [p for p in produtos if p not in prioritarios]
        dia_ano = date.today().timetuple().tm_yday
        offset = (dia_ano * 17) % max(len(outros), 1)  # rotaciona a cada dia
        outros_hoje = outros[offset:offset + 50] + outros[:max(0, offset + 50 - len(outros))]

        for p in prioritarios + outros_hoje:
            handle = p.get("handle", "")
            if handle:
                urls.append(f"{SHOPIFY_BASE_URL}/products/{handle}")

    # Deduplica e respeita quota
    seen = set()
    fila = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            fila.append(url)
        if len(fila) >= quota:
            break

    return fila


def _label_categoria(product_type: str) -> str:
    MAP = {
        "Coloração Capilar": "coloracao",
        "Descolorante e Oxidante": "descolorante",
        "Shampoo": "shampoo",
        "Condicionador": "condicionador",
        "Máscara e Tratamento Capilar": "tratamento",
        "Creme de Tratamento Capilar": "tratamento",
        "Leave-in e Finalizador Capilar": "finalizador",
        "Óleo Capilar": "oleo_capilar",
        "Silicone e Defrizante Capilar": "finalizador",
        "Progressiva e Alisante": "progressiva",
        "Descolorante e Oxidante": "descolorante",
        "Kit Capilar": "kit",
        "Desodorante e Antitranspirante": "desodorante",
        "Absorvente Feminino": "higiene_feminina",
        "Esmalte para Unhas": "esmalte",
        "Maquiagem": "maquiagem",
        "Skin Care": "skin_care",
        "Body Splash e Perfume": "perfume",
        "Hidratante Corporal": "hidratante",
        "Sabonete": "sabonete",
    }
    return MAP.get(product_type, "outros")


# ── Log de execução ────────────────────────────────────────────────────────

def _salvar_log(resultado: dict):
    try:
        historico = []
        if LOG_PATH.exists():
            historico = json.loads(LOG_PATH.read_text(encoding="utf-8"))
        historico.append(resultado)
        # Mantém últimos 30 dias
        historico = historico[-30:]
        LOG_PATH.write_text(json.dumps(historico, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error("SCBOT: erro ao salvar log: %s", e)


def carregar_status() -> dict:
    """Retorna status atual do SCBOT para a API."""
    try:
        if not LOG_PATH.exists():
            return {"rodadas": [], "ultimo_ciclo": None, "credenciais_ok": CREDENTIALS_PATH.exists()}
        historico = json.loads(LOG_PATH.read_text(encoding="utf-8"))
        ultimo = historico[-1] if historico else None
        return {
            "credenciais_ok": CREDENTIALS_PATH.exists(),
            "ultimo_ciclo": ultimo,
            "total_rodadas": len(historico),
            "historico_recente": historico[-7:],  # últimos 7 dias
        }
    except Exception:
        return {"credenciais_ok": CREDENTIALS_PATH.exists(), "ultimo_ciclo": None}


# ── Ciclo principal ────────────────────────────────────────────────────────

_ultimo_ciclo_data: Optional[object] = None  # date do último ciclo executado


def executar_ciclo(urls_extras: list[str] = None, force: bool = False) -> dict:
    """
    Executa um ciclo completo do SCBOT.
    Pode ser chamado manualmente (endpoint) ou pelo scheduler diário.
    `force=True` pula a verificação de duplicata do dia.
    """
    global _ultimo_ciclo_data
    from datetime import date as _date

    hoje = _date.today()
    if not force and _ultimo_ciclo_data == hoje:
        logger.info("SCBOT: ciclo já executado hoje (%s) — ignorado", hoje.isoformat())
        return {"ok": True, "ignorado": True, "motivo": "já executado hoje"}
    _ultimo_ciclo_data = hoje
    inicio = datetime.now().isoformat()
    weekday = date.today().weekday()
    focus_labels = WEEKDAY_FOCUS.get(weekday, [])

    logger.info("SCBOT: iniciando ciclo — foco do dia: %s", focus_labels)

    token = _get_access_token()
    if not token:
        resultado = {
            "data": inicio, "ok": False,
            "erro": "Credenciais Google não encontradas ou inválidas",
            "enviadas": 0, "erros": 0, "urls": [],
        }
        _salvar_log(resultado)
        return resultado

    fila = _montar_fila()
    if urls_extras:
        for u in urls_extras:
            if u not in fila:
                fila.insert(0, u)  # extras entram na frente
        fila = fila[:DAILY_QUOTA]

    enviadas = erros = 0
    log_urls = []

    for url in fila:
        ok = _submit_url(url, token)
        if ok:
            enviadas += 1
            log_urls.append({"url": url, "ok": True})
        else:
            erros += 1
            log_urls.append({"url": url, "ok": False})
        time.sleep(0.2)  # evita rate limit

    resultado = {
        "data": inicio,
        "ok": True,
        "enviadas": enviadas,
        "erros": erros,
        "total_fila": len(fila),
        "foco_dia": focus_labels,
        "urls": log_urls,
    }
    _salvar_log(resultado)
    logger.info("SCBOT: ciclo concluído — %d enviadas, %d erros", enviadas, erros)
    return resultado


# ── Agendamento diário às 09:00 ────────────────────────────────────────────

_scbot_thread: Optional[threading.Thread] = None
_scbot_stop = threading.Event()


def _loop_diario():
    """Roda em background, dispara ciclo todo dia às 09:00 e sort às 04:00."""
    logger.info("SCBOT: agendador iniciado (SCBOT 09:00 | sort collections 04:00)")
    ultimo_dia_scbot = None
    ultimo_dia_sort  = None

    while not _scbot_stop.is_set():
        agora = datetime.now()
        hoje = agora.date()

        # Sort collections às 04:00 — produtos sem estoque vão para o final
        if agora.hour == 4 and ultimo_dia_sort != hoje:
            logger.info("SCBOT: disparando sort_collections — %s", hoje.isoformat())
            try:
                from seo_sort_collections import sort_collections_by_stock
                sort_collections_by_stock()
            except Exception as e:
                logger.error("SCBOT: erro no sort_collections: %s", e)
            ultimo_dia_sort = hoje

        # Indexação Google às 09:00
        if agora.hour == 9 and ultimo_dia_scbot != hoje:
            logger.info("SCBOT: disparando ciclo diário — %s", hoje.isoformat())
            try:
                executar_ciclo()
            except Exception as e:
                logger.error("SCBOT: erro no ciclo diário: %s", e)
            ultimo_dia_scbot = hoje

        # Dorme 60s entre verificações
        for _ in range(60):
            if _scbot_stop.is_set():
                break
            time.sleep(1)

    logger.info("SCBOT: agendador encerrado.")


def iniciar_scbot():
    """Inicia o SCBOT em background. Chamar no startup do FastAPI."""
    global _scbot_thread
    if _scbot_thread and _scbot_thread.is_alive():
        logger.warning("SCBOT já está rodando.")
        return _scbot_thread

    _scbot_stop.clear()
    _scbot_thread = threading.Thread(target=_loop_diario, name="shinsei-scbot", daemon=True)
    _scbot_thread.start()
    logger.info("SCBOT iniciado (thread: %s)", _scbot_thread.name)
    return _scbot_thread


def parar_scbot():
    """Para o SCBOT graciosamente."""
    _scbot_stop.set()
    if _scbot_thread:
        _scbot_thread.join(timeout=10)
