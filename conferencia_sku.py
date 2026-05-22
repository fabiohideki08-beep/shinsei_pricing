"""
conferencia_sku.py — Shinsei Pricing
Conferência cruzada de SKUs: Bling ↔ ML, Shopify, Amazon, Shopee.

Lógica:
- Coleta todos os SKUs do Bling (produtos ativos)
- Coleta todos os SKUs de cada canal conectado
- Cruza: quais do Bling faltam em cada canal?
         quais de cada canal não existem no Bling?
- Resultado salvo em data/conferencia_sku.json
"""

from __future__ import annotations

import json
import logging
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

RESULTADO_PATH = DATA_DIR / "conferencia_sku.json"
_ESTADO_LOCK = threading.Lock()
_estado: dict = {"status": "ocioso", "pct": 0, "msg": "", "etapa": ""}


# ─────────────────────────────────────────────
# Estado / progresso
# ─────────────────────────────────────────────

def get_estado() -> dict:
    with _ESTADO_LOCK:
        return dict(_estado)


def _set_estado(status: str, pct: int, etapa: str, msg: str = "") -> None:
    with _ESTADO_LOCK:
        _estado["status"] = status
        _estado["pct"] = pct
        _estado["etapa"] = etapa
        _estado["msg"] = msg


def get_resultado() -> dict | None:
    if not RESULTADO_PATH.exists():
        return None
    try:
        return json.loads(RESULTADO_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _salvar_resultado(dados: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    RESULTADO_PATH.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")


# ─────────────────────────────────────────────
# Fetch Bling — todos os produtos ativos
# ─────────────────────────────────────────────

def _fetch_bling(bling_client) -> tuple[dict[str, dict], int, str | None]:
    """
    Retorna (skus_bling, total_sem_sku, erro)
    skus_bling: {sku -> {nome, estoque, preco, id_bling}}
    erro: None se OK, string com mensagem se falhou
    """
    skus: dict[str, dict] = {}
    sem_sku = 0
    pagina = 1
    primeiro_erro: str | None = None

    while pagina <= 300:
        try:
            payload = bling_client._get(
                "/produtos",
                params={"pagina": pagina, "limite": 100}
            )
        except Exception as e:
            msg = str(e)[:200]
            logger.error("Bling página %d erro: %s", pagina, msg)
            if pagina == 1:
                # Falha na primeira página = Bling inacessível (token expirado, etc.)
                return {}, 0, f"Erro na página 1 do Bling: {msg}"
            # Páginas seguintes: para mas usa o que já coletou
            primeiro_erro = f"Interrompido na página {pagina}: {msg}"
            break

        data = payload.get("data", [])
        if not data:
            logger.info("Bling: página %d retornou vazia (total: %d SKUs)", pagina, len(skus))
            break

        for item in data:
            try:
                prod = bling_client._normalize_product(item) if hasattr(bling_client, "_normalize_product") else item
            except Exception:
                prod = item

            sku = str(prod.get("codigo") or prod.get("sku") or "").strip()
            if not sku:
                sem_sku += 1
                continue

            nome = str(prod.get("descricao") or prod.get("nome") or "").strip()
            estoque = 0
            preco = 0.0
            try:
                est_info = prod.get("estoque") or {}
                estoque = int(
                    est_info.get("saldoVirtualTotal")
                    or est_info.get("saldo_fisico_total")
                    or 0
                )
                preco = float(prod.get("preco") or 0)
            except Exception:
                pass

            situacao = str(prod.get("situacao") or item.get("situacao") or "").strip().upper()
            skus[sku] = {
                "nome": nome,
                "estoque": estoque,
                "preco": preco,
                "id_bling": str(prod.get("id") or ""),
                "situacao": situacao,
            }

        pagina += 1
        time.sleep(0.15)

    logger.info("Bling: %d SKUs coletados em %d páginas (sem_sku=%d)", len(skus), pagina - 1, sem_sku)
    return skus, sem_sku, primeiro_erro


# ─────────────────────────────────────────────
# Fetch ML — todos os anúncios ativos com SKU
# ─────────────────────────────────────────────

def _fetch_ml() -> tuple[dict[str, dict], list[dict], bool]:
    """
    Retorna (skus_ml, sem_sku_ml, conectado)
    skus_ml: {sku -> {"ml_id": str, "status": str}}
      status: "active" | "paused" | "closed" | "inactive" | ...
    sem_sku_ml: lista de {id, titulo} sem SKU
    """
    try:
        import requests
        tp = DATA_DIR / "ml_tokens.json"
        if not tp.exists():
            return {}, [], False
        tokens = json.loads(tp.read_text(encoding="utf-8"))
        token = tokens.get("access_token", "")
        if not token:
            return {}, [], False

        h = {"Authorization": f"Bearer {token}"}
        skus: dict[str, dict] = {}
        sem_sku: list[dict] = []

        # Descobre user_id — tenta refresh automático se token expirado
        me = requests.get("https://api.mercadolibre.com/users/me", headers=h, timeout=10)
        if me.status_code == 401:
            import os, urllib.parse, urllib.request
            refresh_token = tokens.get("refresh_token", "")
            client_id = tokens.get("client_id") or os.getenv("ML_CLIENT_ID", "")
            client_secret = os.getenv("ML_CLIENT_SECRET", "")
            if refresh_token and client_id and client_secret:
                try:
                    _data = urllib.parse.urlencode({
                        "grant_type": "refresh_token",
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "refresh_token": refresh_token,
                    }).encode()
                    _req = urllib.request.Request("https://api.mercadolibre.com/oauth/token", data=_data)
                    with urllib.request.urlopen(_req, timeout=15) as _resp:
                        _new = json.loads(_resp.read())
                        token = _new.get("access_token", token)
                        tokens.update(_new)
                        tp.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")
                        h = {"Authorization": f"Bearer {token}"}
                        me = requests.get("https://api.mercadolibre.com/users/me", headers=h, timeout=10)
                except Exception as _e:
                    logger.warning("ML refresh falhou na conferencia: %s", _e)

        if me.status_code != 200:
            return {}, [], False
        user_id = str(me.json().get("id", ""))

        # Busca IDs por status: active + paused + closed
        def _buscar_ids_por_status(status_filter: str) -> list[tuple[str, str]]:
            """Retorna lista de (ml_id, status_filter)."""
            resultado = []
            offset = 0
            limit = 100
            MAX = 5000
            while offset < MAX:
                r = requests.get(
                    f"https://api.mercadolibre.com/users/{user_id}/items/search",
                    params={"status": status_filter, "limit": limit, "offset": offset},
                    headers=h, timeout=15,
                )
                if r.status_code != 200:
                    break
                data = r.json()
                items = data.get("results", [])
                if not items:
                    break
                resultado.extend((item_id, status_filter) for item_id in items)
                offset += limit
                if len(items) < limit:
                    break
                time.sleep(0.1)
            return resultado

        # Coleta IDs de todos os status relevantes
        todos_ids: list[tuple[str, str]] = []
        for st in ["active", "paused", "closed", "inactive"]:
            ids_st = _buscar_ids_por_status(st)
            logger.info("ML status=%s: %d anúncios", st, len(ids_st))
            todos_ids.extend(ids_st)
            time.sleep(0.2)

        # Mapa id → status_filtro (para atribuir status ao detalhar)
        id_status_map: dict[str, str] = {ml_id: st for ml_id, st in todos_ids}

        # Detalha em lotes de 20
        ids_uniq = list(id_status_map.keys())
        for i in range(0, len(ids_uniq), 20):
            batch = ids_uniq[i: i + 20]
            ids_str = ",".join(batch)
            r2 = requests.get(
                "https://api.mercadolibre.com/items",
                params={"ids": ids_str, "attributes": "id,title,available_quantity,seller_custom_field,status"},
                headers=h, timeout=15,
            )
            if r2.status_code != 200:
                continue
            for entry in r2.json():
                item = entry.get("body") or entry
                sku = str(item.get("seller_custom_field") or "").strip()
                ml_id = item.get("id", "")
                titulo = (item.get("title") or "")[:80]
                estoque = item.get("available_quantity", 0)
                # status real do item (pode diferir do filtro de busca)
                status_real = str(item.get("status") or id_status_map.get(ml_id, "")).strip()
                if sku:
                    # Mantém o mais recente (active tem prioridade)
                    existing = skus.get(sku)
                    if not existing or status_real == "active":
                        skus[sku] = {"ml_id": ml_id, "status": status_real}
                else:
                    sem_sku.append({"id": ml_id, "titulo": titulo, "estoque": estoque})
            time.sleep(0.1)

        logger.info("ML: %d SKUs coletados (active+paused+closed+inactive)", len(skus))
        return skus, sem_sku, True

    except Exception as e:
        logger.warning("Erro ao buscar ML: %s", e)
        return {}, [], False


# ─────────────────────────────────────────────
# Fetch Shopify — todos os SKUs de variantes
# ─────────────────────────────────────────────

def _fetch_shopify() -> tuple[dict[str, dict], bool]:
    """
    Retorna (skus_shopify, conectado)
    skus_shopify: {sku -> {"variant_id": str, "status": str}}
      status: "active" | "archived" | "draft"
    """
    try:
        cfg_path = DATA_DIR / "shopify_config.json"
        if not cfg_path.exists():
            return {}, False
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        token = cfg.get("access_token") or cfg.get("token", "")
        shop = cfg.get("shop_url") or cfg.get("myshopify_domain") or cfg.get("shop", "")
        # Garante domínio completo
        if shop and "." not in shop:
            shop = f"{shop}.myshopify.com"
        if not token or not shop:
            return {}, False

        import requests
        h = {
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        }
        # Busca todos os produtos (active + archived + draft) para poder filtrar no frontend
        base = f"https://{shop}/admin/api/2024-01/products.json"
        skus: dict[str, dict] = {}
        page_info = None

        while True:
            params: dict = {"limit": 250, "fields": "id,status,variants"}
            if page_info:
                params["page_info"] = page_info
            else:
                # Busca todos os status: active, archived, draft
                params["status"] = "active,archived,draft"

            r = requests.get(base, headers=h, params=params, timeout=20)
            if r.status_code != 200:
                break

            produtos = r.json().get("products", [])
            for p in produtos:
                pstatus = str(p.get("status") or "active")
                for v in p.get("variants", []):
                    sku = str(v.get("sku") or "").strip()
                    if sku:
                        skus[sku] = {
                            "variant_id": str(v.get("id") or ""),
                            "status": pstatus,  # "active" | "archived" | "draft"
                        }

            # Paginação via Link header
            # Formato: <url?page_info=ABC>; rel="previous", <url?page_info=XYZ>; rel="next"
            link = r.headers.get("Link", "")
            page_info = None
            if 'rel="next"' in link:
                import re
                # Separa os segmentos por vírgula e encontra o que tem rel="next"
                for seg in link.split(","):
                    if 'rel="next"' in seg:
                        m = re.search(r'page_info=([^&>]+)', seg)
                        if m:
                            page_info = m.group(1)
                        break
            if not page_info:
                break

            if not produtos:
                break
            time.sleep(0.1)

        return skus, True

    except Exception as e:
        logger.warning("Erro ao buscar Shopify: %s", e)
        return {}, False


# ─────────────────────────────────────────────
# Fetch Amazon — inventário FBA/MFN
# ─────────────────────────────────────────────

def _fetch_amazon() -> tuple[dict[str, dict], bool]:
    """
    Retorna (skus_amazon, conectado)
    skus_amazon: {sellerSku -> {"qty": int, "status": str}}
      status: "active" (BUYABLE) | "inactive" (outros)
    Usa Listings Items API v2021-08-01.
    """
    try:
        from amazon_client import AmazonClient
        client = AmazonClient()
        skus: dict[str, dict] = {}

        page_token = None
        pagina = 0
        while True:
            pagina += 1
            resp = client.get_listings(page_token=page_token)

            # A API pode retornar erros dentro do JSON (HTTP 200 com campo "errors")
            api_errors = resp.get("errors") or []
            if api_errors:
                logger.warning("Amazon Listings API retornou erros na página %d: %s", pagina, api_errors)
                if pagina == 1:
                    # Falha na primeira página = canal não conectado
                    return {}, False
                # Páginas seguintes com erro: usa o que foi coletado
                break

            items = resp.get("items", [])
            logger.info("Amazon página %d: %d anúncios recebidos", pagina, len(items))

            # Se a primeira página retornar vazia (sem erro HTTP, sem api_errors),
            # é sinal de configuração incorreta ou token sem permissão — não é
            # "Amazon com 0 produtos", o que causaria falsos positivos de cobertura.
            if pagina == 1 and not items:
                logger.warning(
                    "Amazon Listings API retornou 0 itens na 1ª página "
                    "(seller_id=%s, marketplace=%s) — tratando como não conectado.",
                    client.config.get("seller_id", "?"),
                    client.config.get("marketplace_id", "?"),
                )
                return {}, False

            for item in items:
                sku = str(item.get("sku") or "").strip()
                summaries = item.get("summaries") or []
                if not sku:
                    for s in summaries:
                        sku = str(s.get("sku") or s.get("sellerSku") or "").strip()
                        if sku:
                            break
                if sku:
                    # Extrai status do primeiro summary (lista de strings ex: ["BUYABLE"])
                    status_list = []
                    for s in summaries:
                        st = s.get("status")
                        if isinstance(st, list):
                            status_list.extend(st)
                        elif isinstance(st, str) and st:
                            status_list.append(st)
                    amz_status = "active" if "BUYABLE" in status_list else ("inactive" if status_list else "active")
                    skus[sku] = {"qty": 1, "status": amz_status}

            page_token = resp.get("pagination", {}).get("nextToken")
            if not page_token or not items:
                break
            time.sleep(0.5)

        logger.info("Amazon: %d SKUs MFN coletados", len(skus))
        return skus, True

    except Exception as e:
        logger.warning("Amazon indisponível: %s", e)
        return {}, False


# ─────────────────────────────────────────────
# Fetch Shopee — via API (get_item_list + get_item_base_info)
# ─────────────────────────────────────────────

def _fetch_shopee() -> tuple[dict[str, dict], bool]:
    """
    Retorna (skus_shopee, conectado)
    skus_shopee: {item_sku -> {"item_id": str, "status": str}}
      status: "active" (NORMAL) | "inactive" (UNLIST/BANNED)
    """
    try:
        from services.shopee import ShopeeService, tem_tokens
        if not tem_tokens():
            return {}, False

        svc = ShopeeService()
        skus: dict[str, dict] = {}

        def _fetch_por_status(item_status: str, label: str):
            """Busca todos os itens de um determinado status Shopee."""
            offset = 0
            page_size = 50
            MAX = 5000
            primeira = True
            while offset < MAX:
                resp = svc.listar_produtos(offset=offset, page_size=page_size, item_status=item_status)
                if resp.get("error") or not resp.get("response"):
                    if primeira:
                        logger.warning("Shopee %s: primeira chamada com erro: %s", label, resp.get("error", ""))
                        return False
                    break
                primeira = False
                r = resp["response"]
                item_ids = [i["item_id"] for i in r.get("item", []) if i.get("item_id")]
                if not item_ids:
                    break
                for i in range(0, len(item_ids), 50):
                    batch = item_ids[i:i+50]
                    info_resp = svc.obter_info_items(batch)
                    items_list = (info_resp.get("response") or {}).get("item_list") or []
                    for item in items_list:
                        item_id = str(item.get("item_id", ""))
                        sku = str(item.get("item_sku") or "").strip()
                        if sku and sku not in skus:
                            skus[sku] = {"item_id": item_id, "status": label}
                    time.sleep(0.2)
                if not r.get("has_next_page"):
                    break
                offset += page_size
                time.sleep(0.3)
            return True

        # Busca ativos (NORMAL) — se falhar aqui é erro de conexão
        ok = _fetch_por_status("NORMAL", "active")
        if not ok:
            return {}, False
        logger.info("Shopee NORMAL: %d SKUs", len(skus))

        # Busca inativos (UNLIST) — falha silenciosa, só adiciona se não já presente
        _fetch_por_status("UNLIST", "inactive")
        logger.info("Shopee total (NORMAL+UNLIST): %d SKUs", len(skus))

        return skus, True

    except Exception as e:
        logger.warning("Erro ao buscar Shopee: %s", e)
        return {}, False



# ─────────────────────────────────────────────
# Conferência principal
# ─────────────────────────────────────────────

def executar_conferencia(bling_client) -> dict:
    """
    Executa a conferência completa e salva resultado.
    Deve ser chamada em thread separada.
    """
    inicio = time.time()
    _set_estado("rodando", 5, "bling", "Coletando produtos do Bling...")

    try:
        # 1. Bling
        skus_bling, sem_sku_bling, bling_erro = _fetch_bling(bling_client)

        if not skus_bling:
            # Bling retornou 0 produtos — abortamos para não gerar resultados enganosos
            msg = bling_erro or "Bling retornou 0 produtos (token expirado ou API indisponível)"
            logger.error("Conferência abortada: %s", msg)
            _set_estado("erro", 0, "bling", msg)
            return {"ok": False, "erro": msg}

        logger.info("Bling: %d SKUs carregados%s",
                    len(skus_bling),
                    f" (aviso: {bling_erro})" if bling_erro else "")
        _set_estado("rodando", 30, "ml", "Coletando anúncios do Mercado Livre...")

        # 2. ML
        skus_ml, sem_sku_ml, ml_ok = _fetch_ml()
        _set_estado("rodando", 50, "shopify", "Coletando variantes da Shopify...")

        # 3. Shopify
        skus_shopify, shopify_ok = _fetch_shopify()
        _set_estado("rodando", 65, "amazon", "Coletando inventário da Amazon...")

        # 4. Amazon
        skus_amazon, amazon_ok = _fetch_amazon()
        _set_estado("rodando", 78, "shopee", "Coletando mapeamento da Shopee...")

        # 5. Shopee
        skus_shopee, shopee_ok = _fetch_shopee()
        _set_estado("rodando", 88, "cruzamento", "Cruzando SKUs...")

        # ── Cruzamento ──────────────────────────────────────────
        # Todos os SKUs únicos (union de todos os canais)
        todos_skus: set[str] = set(skus_bling.keys())
        if ml_ok:
            todos_skus |= set(skus_ml.keys())
        if shopify_ok:
            todos_skus |= set(skus_shopify.keys())
        if amazon_ok:
            todos_skus |= set(skus_amazon.keys())
        if shopee_ok:
            todos_skus |= set(skus_shopee.keys())

        matrix: list[dict] = []
        for sku in sorted(todos_skus):
            no_bling = sku in skus_bling
            no_ml = sku in skus_ml if ml_ok else None
            no_shopify = sku in skus_shopify if shopify_ok else None
            no_amazon = sku in skus_amazon if amazon_ok else None
            no_shopee = sku in skus_shopee if shopee_ok else None

            canais_conectados = (
                (["ml"] if ml_ok else [])
                + (["shopify"] if shopify_ok else [])
                + (["amazon"] if amazon_ok else [])
                + (["shopee"] if shopee_ok else [])
            )
            n_conectados = len(canais_conectados)
            n_presentes = sum(
                1
                for v in [no_ml, no_shopify, no_amazon, no_shopee]
                if v is True
            )
            cobertura = round(n_presentes / n_conectados, 2) if n_conectados else 1.0

            bling_info = skus_bling.get(sku, {})
            row: dict[str, Any] = {
                "sku": sku,
                "nome": bling_info.get("nome", ""),
                "estoque_bling": bling_info.get("estoque", 0),
                "preco_bling": bling_info.get("preco", 0.0),
                "id_bling": bling_info.get("id_bling", ""),
                "situacao_bling": bling_info.get("situacao", ""),  # "A" | "I" | ""
                "presente_bling": no_bling,
                "cobertura": cobertura,
            }
            if ml_ok:
                ml_info = skus_ml.get(sku) or {}
                row["presente_ml"] = no_ml
                row["ml_id"] = ml_info.get("ml_id", "")
                row["status_ml"] = ml_info.get("status", "")  # "active" | "paused" | "closed" | "inactive"
            if shopify_ok:
                sh_info = skus_shopify.get(sku) or {}
                row["presente_shopify"] = no_shopify
                row["shopify_variant_id"] = sh_info.get("variant_id", "")
                row["status_shopify"] = sh_info.get("status", "")  # "active" | "archived" | "draft"
            if amazon_ok:
                amz_info = skus_amazon.get(sku) or {}
                row["presente_amazon"] = no_amazon
                row["amazon_qty"] = amz_info.get("qty", 0)
                row["status_amazon"] = amz_info.get("status", "")  # "active" | "inactive"
            if shopee_ok:
                sp_info = skus_shopee.get(sku) or {}
                row["presente_shopee"] = no_shopee
                row["shopee_item_id"] = sp_info.get("item_id", "")
                row["status_shopee"] = sp_info.get("status", "")  # "active" | "inactive"

            matrix.append(row)

        # Ordena: primeiro os que têm mais lacunas
        matrix.sort(key=lambda x: (x["cobertura"], x["sku"]))

        # Stats por canal
        def _stats_canal(skus_canal: dict, canal_ok: bool) -> dict:
            if not canal_ok:
                return {"conectado": False}
            presentes = sum(1 for s in skus_canal if s in skus_bling)
            sem_bling = sum(1 for s in skus_canal if s not in skus_bling)
            return {
                "conectado": True,
                "total": len(skus_canal),
                "presentes_em_bling": presentes,
                "sem_bling": sem_bling,
            }

        # Bling sem canal
        def _bling_sem(skus_canal: dict, canal_ok: bool) -> int:
            if not canal_ok:
                return 0
            return sum(1 for s in skus_bling if s not in skus_canal)

        resultado = {
            "executado_em": datetime.now(timezone.utc).isoformat(),
            "duracao_segundos": round(time.time() - inicio, 1),
            "bling_aviso": bling_erro,  # None ou mensagem se coleta foi parcial
            "stats": {
                "total_bling": len(skus_bling),
                "sem_sku_no_bling": sem_sku_bling,
                "total_skus_universo": len(todos_skus),
                "ml": _stats_canal(skus_ml, ml_ok),
                "shopify": _stats_canal(skus_shopify, shopify_ok),
                "amazon": _stats_canal(skus_amazon, amazon_ok),
                "shopee": _stats_canal(skus_shopee, shopee_ok),
                "bling_sem_ml": _bling_sem(skus_ml, ml_ok),
                "bling_sem_shopify": _bling_sem(skus_shopify, shopify_ok),
                "bling_sem_amazon": _bling_sem(skus_amazon, amazon_ok),
                "bling_sem_shopee": _bling_sem(skus_shopee, shopee_ok),
            },
            "sem_sku_ml": sem_sku_ml[:200],  # cap 200
            "matrix": matrix,
        }

        _salvar_resultado(resultado)
        concluido_msg = f"Conferência concluída — {len(matrix)} SKUs analisados"
        if bling_erro:
            concluido_msg += f" (⚠️ Bling parcial: {bling_erro})"
        _set_estado("concluido", 100, "concluido", concluido_msg)
        return resultado

    except Exception as e:
        logger.exception("Erro na conferência de SKU: %s", e)
        _set_estado("erro", 0, "erro", str(e))
        return {"ok": False, "erro": str(e)}


# ─────────────────────────────────────────────
# Thread helper
# ─────────────────────────────────────────────

_thread_conf: threading.Thread | None = None


def iniciar_conferencia_em_background(bling_client) -> bool:
    """Inicia a conferência em thread separada. Retorna False se já estiver rodando."""
    global _thread_conf
    if _thread_conf and _thread_conf.is_alive():
        return False
    _thread_conf = threading.Thread(
        target=executar_conferencia,
        args=(bling_client,),
        daemon=True,
        name="conferencia-sku",
    )
    _thread_conf.start()
    return True
