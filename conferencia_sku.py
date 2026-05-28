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
            # GTIN/EAN: usado para fallback de mapeamento com ML quando seller_custom_field é EAN
            gtin = str(prod.get("gtin") or item.get("gtin") or "").strip()
            skus[sku] = {
                "nome": nome,
                "estoque": estoque,
                "preco": preco,
                "id_bling": str(prod.get("id") or ""),
                "situacao": situacao,
                "gtin": gtin,
            }

        pagina += 1
        time.sleep(0.15)

    logger.info("Bling: %d SKUs coletados em %d páginas (sem_sku=%d)", len(skus), pagina - 1, sem_sku)
    return skus, sem_sku, primeiro_erro


# ─────────────────────────────────────────────
# Fetch Bling Anúncios ML — mapa MLB → SKU Bling
# ─────────────────────────────────────────────

def _fetch_bling_anuncios_ml(bling_client, skus_bling: dict) -> dict[str, str]:
    """
    Busca os anúncios ML vinculados aos produtos Bling via GET /anuncios.
    Retorna {mlb_code: bling_sku} — mapeamento direto e confiável.

    A API Bling v3 /anuncios retorna os anúncios exportados para o Mercado Livre
    com o campo 'codigo' contendo o MLB (ex: "MLB3037301672") e o campo
    'produto.id' (ou 'produto.codigo') com o produto Bling vinculado.

    Requer o parâmetro idLoja para identificar a integração ML padrão (204058790).
    """
    # ID e tipo do canal Mercado Livre padrão no Bling (idLoja=204058790, tipo=MercadoLivre)
    # Obrigatório para o endpoint /anuncios não retornar 400 BAD_REQUEST.
    ID_LOJA_ML = 204058790
    TIPO_INTEGRACAO_ML = "MercadoLivre"

    mlb_to_sku: dict[str, str] = {}

    # Mapa inverso: id_bling (int/str) → sku, construído a partir de skus_bling
    id_to_sku: dict[str, str] = {}
    for sku, info in skus_bling.items():
        bid = str(info.get("id_bling") or "").strip()
        if bid:
            id_to_sku[bid] = sku

    pagina = 1
    MAX_PAGS = 200
    while pagina <= MAX_PAGS:
        try:
            payload = bling_client._get("/anuncios", params={
                "pagina": pagina,
                "limite": 100,
                "idLoja": ID_LOJA_ML,
                "tipoIntegracao": TIPO_INTEGRACAO_ML,
            })
        except Exception as e:
            logger.warning("Bling /anuncios página %d erro: %s", pagina, e)
            break

        data = payload.get("data", [])
        if not data:
            break

        for ann in data:
            if not isinstance(ann, dict):
                continue

            # MLB code: campo "codigo" contém o ID do anúncio no ML (ex: "MLB3037301672")
            mlb = str(ann.get("codigo") or "").strip()
            if not mlb or not mlb.upper().startswith("MLB"):
                continue  # não é um anúncio ML

            # Produto vinculado — pode ser dict {id, codigo, nome} ou só id
            prod_info = ann.get("produto") or {}
            if isinstance(prod_info, dict):
                # Tenta pegar o SKU diretamente
                sku_direto = str(prod_info.get("codigo") or "").strip()
                if sku_direto and sku_direto in skus_bling:
                    mlb_to_sku[mlb] = sku_direto
                    continue
                # Fallback: mapeia por id_bling
                bid = str(prod_info.get("id") or "").strip()
                if bid and bid in id_to_sku:
                    mlb_to_sku[mlb] = id_to_sku[bid]

        pagina += 1
        time.sleep(0.15)

    logger.info("Bling /anuncios: %d mapeamentos MLB→SKU coletados", len(mlb_to_sku))
    return mlb_to_sku


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

        # Coleta IDs apenas de anúncios relevantes para conferência:
        # "active" = anunciado | "paused" = pausado (ainda existe no ML)
        # Excluímos "closed" e "inactive" — podem ser milhares de anúncios
        # históricos encerrados que travam a coleta sem agregar valor à análise.
        todos_ids: list[tuple[str, str]] = []
        for st in ["active", "paused"]:
            ids_st = _buscar_ids_por_status(st)
            logger.info("ML status=%s: %d anúncios", st, len(ids_st))
            todos_ids.extend(ids_st)
            time.sleep(0.2)

        # Mapa id → status_filtro (para atribuir status ao detalhar)
        id_status_map: dict[str, str] = {ml_id: st for ml_id, st in todos_ids}

        # Detalha em lotes de 20
        # Inclui "variations" para detectar produtos pai (com variações) e processar
        # cada variação individualmente — produtos pai têm available_quantity=0,
        # apenas as variações têm estoque real.
        ids_uniq = list(id_status_map.keys())
        for i in range(0, len(ids_uniq), 20):
            batch = ids_uniq[i: i + 20]
            ids_str = ",".join(batch)
            r2 = requests.get(
                "https://api.mercadolibre.com/items",
                params={"ids": ids_str, "attributes": "id,title,available_quantity,seller_custom_field,status,variations"},
                headers=h, timeout=15,
            )
            if r2.status_code != 200:
                continue
            for entry in r2.json():
                item = entry.get("body") or entry
                ml_id = str(item.get("id") or "")
                titulo = (item.get("title") or "")[:80]
                status_real = str(item.get("status") or id_status_map.get(ml_id, "")).strip()
                variations = item.get("variations") or []

                if variations:
                    # ── Produto pai com variações ──────────────────────────────
                    # Descarta o pai (sem estoque próprio); processa cada variação.
                    # Cada variação tem seu seller_custom_field (SKU) e available_quantity.
                    for v in variations:
                        v_sku = str(v.get("seller_custom_field") or "").strip()
                        v_qty = int(v.get("available_quantity") or 0)
                        v_id  = str(v.get("id") or ml_id)
                        if v_sku:
                            existing = skus.get(v_sku)
                            if not existing or status_real == "active":
                                skus[v_sku] = {"ml_id": ml_id, "status": status_real, "qty": v_qty, "titulo": titulo}
                        else:
                            # Variação sem SKU — inclui na lista sem mapeamento
                            sem_sku.append({
                                "id": ml_id,       # MLB do pai (usado pelo remapeamento via /anuncios)
                                "titulo": f"{titulo} (var. {v_id})",
                                "estoque": v_qty,
                            })
                else:
                    # ── Produto simples (sem variações) ────────────────────────
                    sku = str(item.get("seller_custom_field") or "").strip()
                    estoque = int(item.get("available_quantity") or 0)
                    if sku:
                        existing = skus.get(sku)
                        if not existing or status_real == "active":
                            skus[sku] = {"ml_id": ml_id, "status": status_real, "qty": estoque, "titulo": titulo}
                    else:
                        sem_sku.append({"id": ml_id, "titulo": titulo, "estoque": estoque})
            time.sleep(0.1)

        logger.info(
            "ML: %d SKUs coletados (active+paused), %d sem SKU",
            len(skus), len(sem_sku),
        )
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

        def _shopify_get(params):
            """GET com retry para erros SSL transientes do Shopify."""
            for attempt in range(3):
                try:
                    r = requests.get(base, headers=h, params=params, timeout=30)
                    return r
                except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
                    logger.warning("Shopify SSL/conexão falhou (tentativa %d/3): %s", attempt + 1, e)
                    if attempt < 2:
                        time.sleep(2 * (attempt + 1))
            raise requests.exceptions.ConnectionError("Shopify: 3 tentativas falharam")

        while True:
            params: dict = {"limit": 250, "fields": "id,status,variants"}
            if page_info:
                params["page_info"] = page_info
            else:
                # Busca todos os status: active, archived, draft
                params["status"] = "active,archived,draft"

            r = _shopify_get(params)
            if r.status_code != 200:
                break

            produtos = r.json().get("products", [])
            for p in produtos:
                pstatus = str(p.get("status") or "active")
                for v in p.get("variants", []):
                    sku = str(v.get("sku") or "").strip()
                    if sku:
                        qty = None
                        try:
                            qty = int(v.get("inventory_quantity") or 0)
                        except Exception:
                            qty = None
                        skus[sku] = {
                            "variant_id": str(v.get("id") or ""),
                            "status": pstatus,  # "active" | "archived" | "draft"
                            "qty": qty,
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
                    item_name = ""
                    for s in summaries:
                        st = s.get("status")
                        if isinstance(st, list):
                            status_list.extend(st)
                        elif isinstance(st, str) and st:
                            status_list.append(st)
                        if not item_name:
                            item_name = str(s.get("itemName") or "").strip()
                    amz_status = "active" if "BUYABLE" in status_list else ("inactive" if status_list else "active")

                    # Extrai quantidade de fulfillmentAvailability (MFN=DEFAULT, FBA=AMAZON_NA)
                    qty = 0
                    fa_list = item.get("fulfillmentAvailability") or []
                    for fa in fa_list:
                        q = fa.get("quantity")
                        if q is not None:
                            qty += int(q)  # soma MFN + FBA se ambos presentes
                    if not fa_list:
                        qty = None  # campo não disponível — não exibir diferença

                    skus[sku] = {"qty": qty, "status": amz_status, "nome": item_name}

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

def _fetch_shopee() -> tuple[dict[str, dict], list[dict], bool]:
    """
    Retorna (skus_shopee, sem_sku_shopee, conectado)
    skus_shopee: {item_sku -> {"item_id": str, "status": str}}
      status: "active" (NORMAL) | "inactive" (UNLIST/BANNED)
    sem_sku_shopee: lista de {item_id, titulo, qty} sem item_sku
    """
    try:
        from services.shopee import ShopeeService, tem_tokens
        if not tem_tokens():
            return {}, [], False

        svc = ShopeeService()
        skus: dict[str, dict] = {}
        sem_sku: list[dict] = []

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
                        titulo = str(item.get("item_name") or "").strip()[:80]
                        sku = str(item.get("item_sku") or "").strip()
                        stock_info = item.get("stock_info_v2") or item.get("stock_info") or {}
                        try:
                            qty = int(
                                stock_info.get("summary_info", {}).get("total_available_stock")
                                or stock_info.get("current_stock")
                                or 0
                            )
                        except Exception:
                            qty = None
                        if sku and sku not in skus:
                            skus[sku] = {"item_id": item_id, "status": label, "qty": qty}
                        elif not sku:
                            # Item sem item_sku — rastreia para remapeamento via Bling /anuncios
                            sem_sku.append({"id": item_id, "titulo": titulo, "qty": qty, "status": label})
                    time.sleep(0.2)
                if not r.get("has_next_page"):
                    break
                offset += page_size
                time.sleep(0.3)
            return True

        # Busca ativos (NORMAL) — se falhar aqui é erro de conexão
        ok = _fetch_por_status("NORMAL", "active")
        if not ok:
            return {}, [], False
        logger.info("Shopee NORMAL: %d SKUs, %d sem SKU", len(skus), len(sem_sku))

        # Busca inativos (UNLIST) — falha silenciosa, só adiciona se não já presente
        _fetch_por_status("UNLIST", "inactive")
        logger.info("Shopee total (NORMAL+UNLIST): %d SKUs, %d sem SKU", len(skus), len(sem_sku))

        return skus, sem_sku, True

    except Exception as e:
        logger.warning("Erro ao buscar Shopee: %s", e)
        return {}, [], False



# ─────────────────────────────────────────────
# Fetch Bling Anúncios Shopee — mapa item_id → SKU Bling
# ─────────────────────────────────────────────

def _fetch_bling_anuncios_shopee(bling_client, skus_bling: dict) -> dict[str, str]:
    """
    Busca os anúncios Shopee vinculados aos produtos Bling via GET /anuncios.
    Retorna {shopee_item_id: bling_sku} — mapeamento direto.

    A API Bling v3 /anuncios retorna os anúncios exportados para a Shopee com
    o campo 'codigo' contendo o item_id Shopee e 'produto.codigo' com o SKU Bling.

    Canal Shopee ativo no Bling: idLoja=204584797, tipoIntegracao=Shopee.
    """
    ID_LOJA_SHOPEE      = 204584797
    TIPO_INTEGRACAO_SHOPEE = "Shopee"

    item_id_to_sku: dict[str, str] = {}

    # Mapa inverso: id_bling → sku
    id_to_sku: dict[str, str] = {}
    for sku, info in skus_bling.items():
        bid = str(info.get("id_bling") or "").strip()
        if bid:
            id_to_sku[bid] = sku

    pagina = 1
    MAX_PAGS = 200
    while pagina <= MAX_PAGS:
        try:
            payload = bling_client._get("/anuncios", params={
                "pagina": pagina,
                "limite": 100,
                "idLoja": ID_LOJA_SHOPEE,
                "tipoIntegracao": TIPO_INTEGRACAO_SHOPEE,
            })
        except Exception as e:
            logger.warning("Bling /anuncios Shopee página %d erro: %s", pagina, e)
            break

        data = payload.get("data", [])
        if not data:
            break

        for ann in data:
            if not isinstance(ann, dict):
                continue

            # Código do item Shopee (numérico como string)
            item_id = str(ann.get("codigo") or "").strip()
            if not item_id:
                continue

            prod_info = ann.get("produto") or {}
            if isinstance(prod_info, dict):
                sku_direto = str(prod_info.get("codigo") or "").strip()
                if sku_direto and sku_direto in skus_bling:
                    item_id_to_sku[item_id] = sku_direto
                    continue
                bid = str(prod_info.get("id") or "").strip()
                if bid and bid in id_to_sku:
                    item_id_to_sku[item_id] = id_to_sku[bid]

        pagina += 1
        time.sleep(0.15)

    logger.info("Bling /anuncios Shopee: %d mapeamentos item_id→SKU coletados", len(item_id_to_sku))
    return item_id_to_sku


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

        # Coleta todos os canais em paralelo com timeout máximo por canal
        import concurrent.futures as _cf
        import threading as _threading
        _set_estado("rodando", 35, "canais", "Coletando canais em paralelo (ML, Shopify, Amazon, Shopee)...")

        # Thread de heartbeat: atualiza progresso gradualmente (35→78%) enquanto coleta
        _canais_prontos = {"done": False, "concluidos": []}
        def _heartbeat():
            import time as _time
            pct = 36
            while not _canais_prontos["done"]:
                _time.sleep(4)
                if _canais_prontos["done"]:
                    break
                concl = _canais_prontos["concluidos"]
                nomes_ok = ", ".join(concl) if concl else "aguardando..."
                msg = f"Coletando canais... concluidos: [{nomes_ok}]"
                pct = min(pct + 2, 78)
                _set_estado("rodando", pct, "canais", msg)
        _hb_thread = _threading.Thread(target=_heartbeat, daemon=True)
        _hb_thread.start()

        _ex = _cf.ThreadPoolExecutor(max_workers=4)
        _fut_ml      = _ex.submit(_fetch_ml)
        _fut_shopify = _ex.submit(_fetch_shopify)
        _fut_amazon  = _ex.submit(_fetch_amazon)
        _fut_shopee  = _ex.submit(_fetch_shopee)

        def _get(fut, timeout_s, fallback, nome):
            try:
                result = fut.result(timeout=timeout_s)
                _canais_prontos["concluidos"].append(nome)
                return result
            except _cf.TimeoutError:
                logger.warning("Timeout %ds na coleta de %s — canal ignorado", timeout_s, nome)
                _canais_prontos["concluidos"].append(f"{nome}(timeout)")
                return fallback
            except Exception as e:
                logger.warning("Erro na coleta de %s: %s", nome, e)
                _canais_prontos["concluidos"].append(f"{nome}(erro)")
                return fallback

        skus_ml,      sem_sku_ml,     ml_ok      = _get(_fut_ml,      450, ({}, [], False), "ML")
        skus_shopify, shopify_ok               = _get(_fut_shopify, 180, ({}, False),      "Shopify")
        skus_amazon,  amazon_ok               = _get(_fut_amazon,  120, ({}, False),      "Amazon")
        skus_shopee,  sem_sku_shopee, shopee_ok = _get(_fut_shopee,  480, ({}, [], False), "Shopee")

        _canais_prontos["done"] = True
        _ex.shutdown(wait=False)

        _set_estado("rodando", 82, "anuncios_ml", "Buscando anúncios ML no Bling (MLB→SKU)...")

        remapped_by_anuncio: dict[str, dict] = {}          # inicializado aqui para estar sempre em escopo
        remapped_by_anuncio_shopee: dict[str, dict] = {}   # idem para Shopee

        # ── Passo 1: Mapa definitivo via Bling /anuncios ─────────────────────────
        # Bling armazena quais anúncios ML (MLBs) estão vinculados a cada produto.
        # Isso permite mapear anúncios sem seller_custom_field (sem_sku_ml).
        if ml_ok:
            try:
                mlb_to_sku = _fetch_bling_anuncios_ml(bling_client, skus_bling)
            except Exception as _e:
                logger.warning("_fetch_bling_anuncios_ml falhou: %s", _e)
                mlb_to_sku = {}

            if mlb_to_sku:
                # Aplica remapeamento para sem_sku_ml: anúncios que não tinham seller_custom_field
                # mas estão vinculados no Bling
                nova_sem_sku: list[dict] = []
                for entry in sem_sku_ml:
                    mlb_id = str(entry.get("id") or "").strip()
                    bling_sku = mlb_to_sku.get(mlb_id)
                    if bling_sku and bling_sku not in skus_ml:
                        # Promove: era "sem SKU", agora sabemos o SKU via Bling
                        skus_ml[bling_sku] = {
                            "ml_id": mlb_id,
                            "status": entry.get("status", ""),
                            "qty": entry.get("estoque"),  # carrega estoque da variação/item
                            "matched_by": "anuncio",
                            "original_key": mlb_id,
                        }
                        remapped_by_anuncio[mlb_id] = {"sku": bling_sku, "ml_id": mlb_id}
                    else:
                        nova_sem_sku.append(entry)
                sem_sku_ml = nova_sem_sku

                # Também aplica para entradas existentes em skus_ml cujo "SKU" é na verdade o MLB code
                # (alguns anúncios têm o próprio MLB no seller_custom_field por engano)
                keys_to_remove_ann: list[str] = []
                for key, val in skus_ml.items():
                    if key.upper().startswith("MLB"):
                        bling_sku = mlb_to_sku.get(key)
                        if bling_sku and bling_sku not in skus_ml:
                            skus_ml_entry = {**val, "matched_by": "anuncio", "original_key": key}
                            remapped_by_anuncio[key] = {"sku": bling_sku, "ml_id": val.get("ml_id", key)}
                            keys_to_remove_ann.append(key)
                            skus_ml[bling_sku] = skus_ml_entry  # will be set after loop
                for k in keys_to_remove_ann:
                    old_val = skus_ml.pop(k)
                    bling_sku = mlb_to_sku.get(k)
                    if bling_sku:
                        skus_ml[bling_sku] = {**old_val, "matched_by": "anuncio", "original_key": k}

                if remapped_by_anuncio:
                    logger.info(
                        "ML fallback Bling /anuncios: %d anúncios sem SKU remapeados via MLB",
                        len(remapped_by_anuncio)
                    )

                # ── Passo 1b: Anúncios com GTIN como seller_custom_field ──────────────────
                # Itens cujo seller_custom_field é um GTIN/EAN entram em skus_ml com chave
                # numérica (ex: "2381155123982484"). Tentamos cruzar pelo ml_id real do item
                # contra mlb_to_sku, pois o Bling /anuncios sabe o SKU correto.
                keys_to_remove_gtin_ann: list[str] = []
                for key, val in skus_ml.items():
                    if key.isdigit() and 8 <= len(key) <= 14:
                        ml_id = val.get("ml_id", "")
                        bling_sku = mlb_to_sku.get(ml_id)
                        if bling_sku and bling_sku not in skus_ml:
                            remapped_by_anuncio[ml_id] = {"sku": bling_sku, "ml_id": ml_id}
                            keys_to_remove_gtin_ann.append(key)

                for k in keys_to_remove_gtin_ann:
                    old_val = skus_ml.pop(k)
                    ml_id = old_val.get("ml_id", "")
                    bling_sku = mlb_to_sku.get(ml_id)
                    if bling_sku:
                        skus_ml[bling_sku] = {**old_val, "matched_by": "anuncio_gtin", "original_key": k}

                if keys_to_remove_gtin_ann:
                    logger.info(
                        "ML fallback /anuncios (GTIN-key): %d anúncios com EAN como seller_custom_field "
                        "remapeados via ml_id → Bling SKU",
                        len(keys_to_remove_gtin_ann)
                    )

        _set_estado("rodando", 85, "cruzamento_gtin", "Cruzando GTINs Bling ↔ ML...")

        remapped_by_gtin: dict[str, dict] = {}  # inicializado aqui para estar em escopo no resultado

        # ── Passo 2: Fallback GTIN: ML listings que usam EAN/GTIN como seller_custom_field ──
        # Muitos anúncios no ML têm o GTIN (EAN-13) no seller_custom_field ao invés do SKU Bling.
        # Construímos um mapa reverso gtin→sku_bling e remapeamos esses registros.
        if ml_ok:
            gtin_to_bling_sku: dict[str, str] = {}
            for sku, info in skus_bling.items():
                gtin = info.get("gtin", "")
                if gtin and len(gtin) >= 8:  # aceita EAN-8, UPC-12, EAN-13, ITF-14
                    gtin_to_bling_sku[gtin] = sku

            if gtin_to_bling_sku:
                keys_to_remove: list[str] = []

                for key, val in skus_ml.items():
                    # key parece GTIN se for numérico com 8–14 dígitos
                    if key.isdigit() and 8 <= len(key) <= 14:
                        bling_sku = gtin_to_bling_sku.get(key)
                        if bling_sku and bling_sku not in skus_ml:
                            # Remapeia: troca a chave GTIN pela chave do SKU Bling
                            remapped_by_gtin[bling_sku] = {**val, "matched_by": "gtin", "original_key": key}
                            keys_to_remove.append(key)
                        # Se não achou no Bling, mantém no skus_ml mas ficará em "ml_sem_bling"

                # Remove os GTINs remapeados e insere com chave correta
                for k in keys_to_remove:
                    del skus_ml[k]
                skus_ml.update(remapped_by_gtin)

                if remapped_by_gtin:
                    logger.info(
                        "ML fallback GTIN: %d anúncios remapeados por EAN/GTIN "
                        "(de %d GTINs conhecidos no Bling)",
                        len(remapped_by_gtin), len(gtin_to_bling_sku)
                    )

        _set_estado("rodando", 87, "anuncios_shopee", "Buscando anúncios Shopee no Bling (item_id→SKU)...")

        # ── Passo 3: Remapeamento Shopee via Bling /anuncios ─────────────────────
        # Itens Shopee sem item_sku (ou com item_id como "SKU") são remapeados
        # usando o canal Shopee do Bling (idLoja=204584797, tipoIntegracao=Shopee).
        if shopee_ok:
            try:
                shopee_item_id_to_sku = _fetch_bling_anuncios_shopee(bling_client, skus_bling)
            except Exception as _e:
                logger.warning("_fetch_bling_anuncios_shopee falhou: %s", _e)
                shopee_item_id_to_sku = {}

            if shopee_item_id_to_sku:
                # Remapeia sem_sku_shopee: itens sem item_sku que estão no Bling via /anuncios
                nova_sem_sku_shopee: list[dict] = []
                for entry in sem_sku_shopee:
                    sid = str(entry.get("id") or "").strip()
                    bling_sku = shopee_item_id_to_sku.get(sid)
                    if bling_sku and bling_sku not in skus_shopee:
                        skus_shopee[bling_sku] = {
                            "item_id": sid,
                            "status": entry.get("status", ""),
                            "qty": entry.get("qty"),
                            "matched_by": "anuncio",
                            "original_key": sid,
                        }
                        remapped_by_anuncio_shopee[sid] = {"sku": bling_sku, "item_id": sid}
                    else:
                        nova_sem_sku_shopee.append(entry)
                sem_sku_shopee = nova_sem_sku_shopee

                if remapped_by_anuncio_shopee:
                    logger.info(
                        "Shopee fallback Bling /anuncios: %d itens sem SKU remapeados via item_id",
                        len(remapped_by_anuncio_shopee)
                    )

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
                row["status_ml"] = ml_info.get("status", "")
                row["qty_ml"] = ml_info.get("qty")      # None = sem dados, int = estoque real
                row["ml_matched_by"] = ml_info.get("matched_by", "sku")
                row["ml_gtin"] = ml_info.get("original_key", "")
            if shopify_ok:
                sh_info = skus_shopify.get(sku) or {}
                row["presente_shopify"] = no_shopify
                row["shopify_variant_id"] = sh_info.get("variant_id", "")
                row["status_shopify"] = sh_info.get("status", "")
                row["qty_shopify"] = sh_info.get("qty")
            if amazon_ok:
                amz_info = skus_amazon.get(sku) or {}
                row["presente_amazon"] = no_amazon
                row["qty_amazon"] = amz_info.get("qty")
                row["status_amazon"] = amz_info.get("status", "")
            if shopee_ok:
                sp_info = skus_shopee.get(sku) or {}
                row["presente_shopee"] = no_shopee
                row["shopee_item_id"] = sp_info.get("item_id", "")
                row["status_shopee"] = sp_info.get("status", "")
                row["qty_shopee"] = sp_info.get("qty")

            matrix.append(row)

        # Ordena: primeiro os que têm mais lacunas
        matrix.sort(key=lambda x: (x["cobertura"], x["sku"]))

        # ── Auditoria: categoriza todos os problemas encontrados ─────────────────
        canais_auditaveis = []
        if ml_ok:      canais_auditaveis.append(("ml",      "qty_ml",      "status_ml"))
        if shopify_ok: canais_auditaveis.append(("shopify",  "qty_shopify", "status_shopify"))
        if amazon_ok:  canais_auditaveis.append(("amazon",   "qty_amazon",  "status_amazon"))
        if shopee_ok:  canais_auditaveis.append(("shopee",   "qty_shopee",  "status_shopee"))

        STATUS_INATIVOS = {
            "ml":      {"paused", "closed", "inactive"},
            "shopify": {"archived", "draft"},
            "amazon":  {"inactive"},
            "shopee":  {"inactive", "unlist", "banned"},
        }

        auditoria: list[dict] = []

        for row in matrix:
            sku        = row["sku"]
            bling_qty  = row.get("estoque_bling", 0) or 0
            bling_sit  = row.get("situacao_bling", "")
            bling_ativo = bling_sit.upper() == "A"
            problemas: list[dict] = []

            for canal, qty_key, status_key in canais_auditaveis:
                presente    = row.get(f"presente_{canal}")
                if presente is None:
                    continue  # canal não conectado

                if not presente:
                    # Produto ausente no canal
                    if bling_ativo and bling_qty > 0:
                        problemas.append({
                            "tipo": "ausente_ativo", "canal": canal, "prioridade": "alta",
                            "detalhe": f"Ativo no Bling ({bling_qty} un), sem anúncio no {canal.upper()}",
                        })
                else:
                    canal_qty    = row.get(qty_key)
                    canal_status = (row.get(status_key) or "").lower()

                    esta_inativo = canal_status in STATUS_INATIVOS.get(canal, set())

                    if esta_inativo:
                        problemas.append({
                            "tipo": "anuncio_inativo", "canal": canal, "prioridade": "media",
                            "detalhe": f"Anúncio {canal.upper()} com status '{canal_status}'",
                        })
                        # Pausado pelo Bling (bling=0) mas canal ainda mostra qty residual
                        # → risco crítico: reativação manual causaria venda sem estoque
                        if canal_qty is not None and canal_qty > 0 and bling_qty == 0:
                            problemas.append({
                                "tipo": "pausado_qty_residual", "canal": canal, "prioridade": "critica",
                                "detalhe": (
                                    f"Pausado (Bling=0) mas {canal.upper()} mostra {canal_qty}un residuais — "
                                    f"reativação manual causaria venda sem estoque!"
                                ),
                                "bling_qty": bling_qty, "canal_qty": canal_qty,
                            })
                    else:
                        # Anúncio ativo — verifica divergências de estoque
                        if canal_qty is not None:
                            if bling_qty > 0 and canal_qty == 0:
                                problemas.append({
                                    "tipo": "sem_estoque_canal", "canal": canal, "prioridade": "alta",
                                    "detalhe": f"Bling={bling_qty}un → {canal.upper()}=0 (perde venda)",
                                    "bling_qty": bling_qty, "canal_qty": canal_qty,
                                })
                            elif bling_qty == 0 and canal_qty > 0:
                                problemas.append({
                                    "tipo": "vende_sem_estoque", "canal": canal, "prioridade": "critica",
                                    "detalhe": f"Bling=0 mas {canal.upper()}={canal_qty}un ATIVO (vendendo sem estoque!)",
                                    "bling_qty": bling_qty, "canal_qty": canal_qty,
                                })
                            elif canal_qty != bling_qty:
                                problemas.append({
                                    "tipo": "estoque_divergente", "canal": canal, "prioridade": "media",
                                    "detalhe": f"Bling={bling_qty} ≠ {canal.upper()}={canal_qty}",
                                    "bling_qty": bling_qty, "canal_qty": canal_qty,
                                })

            if problemas:
                auditoria.append({
                    "sku": sku, "nome": row.get("nome", ""),
                    "estoque_bling": bling_qty, "situacao_bling": bling_sit,
                    "problemas": problemas, "n_problemas": len(problemas),
                    "tem_critico": any(p["prioridade"] == "critica" for p in problemas),
                    "tem_alto":    any(p["prioridade"] == "alta"    for p in problemas),
                })

        # Anúncios sem mapeamento
        if ml_ok:
            for entry in sem_sku_ml:
                auditoria.append({
                    "sku": "", "nome": entry.get("titulo", ""),
                    "estoque_bling": None, "situacao_bling": "",
                    "problemas": [{"tipo": "sem_mapeamento", "canal": "ml", "prioridade": "media",
                                   "detalhe": f"Anúncio ML sem SKU vinculado ao Bling",
                                   "ml_id": entry.get("id",""), "canal_qty": entry.get("estoque",0)}],
                    "n_problemas": 1, "tem_critico": False, "tem_alto": False,
                })

        if shopee_ok:
            for entry in sem_sku_shopee:
                auditoria.append({
                    "sku": "", "nome": entry.get("titulo", ""),
                    "estoque_bling": None, "situacao_bling": "",
                    "problemas": [{"tipo": "sem_mapeamento", "canal": "shopee", "prioridade": "media",
                                   "detalhe": "Item Shopee sem SKU vinculado ao Bling",
                                   "shopee_item_id": entry.get("id",""), "canal_qty": entry.get("qty", 0)}],
                    "n_problemas": 1, "tem_critico": False, "tem_alto": False,
                })

        auditoria.sort(key=lambda x: (
            0 if x["tem_critico"] else (1 if x["tem_alto"] else 2),
            -x["n_problemas"], x["sku"],
        ))

        resumo_auditoria = {
            "total_com_problemas": len(auditoria),
            "criticos": sum(1 for a in auditoria if a["tem_critico"]),
            "altos":    sum(1 for a in auditoria if a["tem_alto"] and not a["tem_critico"]),
            "medios":   sum(1 for a in auditoria if not a["tem_critico"] and not a["tem_alto"]),
            "por_tipo": {}, "por_canal": {},
        }
        for item in auditoria:
            for p in item["problemas"]:
                resumo_auditoria["por_tipo"][p["tipo"]]   = resumo_auditoria["por_tipo"].get(p["tipo"],   0) + 1
                resumo_auditoria["por_canal"][p["canal"]] = resumo_auditoria["por_canal"].get(p["canal"], 0) + 1

        logger.info("Auditoria: %d itens (%d críticos, %d altos, %d médios)",
            len(auditoria), resumo_auditoria["criticos"],
            resumo_auditoria["altos"], resumo_auditoria["medios"])

        # Stats por canal
        _STATUS_ATIVOS_CANAL = {"active"}

        def _stats_canal(skus_canal: dict, canal_ok: bool) -> dict:
            if not canal_ok:
                return {"conectado": False}
            presentes = sum(1 for s in skus_canal if s in skus_bling)
            sem_bling = sum(1 for s in skus_canal if s not in skus_bling)
            total_ativos   = sum(1 for v in skus_canal.values() if (v.get("status") or "").lower() in _STATUS_ATIVOS_CANAL)
            total_inativos = len(skus_canal) - total_ativos
            return {
                "conectado": True,
                "total": len(skus_canal),
                "total_ativos": total_ativos,
                "total_inativos": total_inativos,
                "presentes_em_bling": presentes,
                "sem_bling": sem_bling,
            }

        # Bling sem canal
        def _bling_sem(skus_canal: dict, canal_ok: bool) -> int:
            if not canal_ok:
                return 0
            return sum(1 for s in skus_bling if s not in skus_canal)

        # Subconjuntos por situação do Bling
        skus_bling_ativos   = {s: v for s, v in skus_bling.items() if v.get("situacao", "").upper() == "A"}
        skus_bling_inativos = {s: v for s, v in skus_bling.items() if v.get("situacao", "").upper() != "A"}

        def _bling_sem_situacao(skus_canal: dict, canal_ok: bool, subset: dict) -> int:
            if not canal_ok:
                return 0
            return sum(1 for s in subset if s not in skus_canal)

        def _bling_ativo_sem_canal_ativo(skus_canal: dict, canal_ok: bool, subset: dict, status_ativos: set) -> int:
            """Bling ativos sem listing ATIVO no canal (absent + paused/closed/inactive)."""
            if not canal_ok:
                return 0
            count = 0
            for sku in subset:
                info = skus_canal.get(sku)
                if not info:
                    count += 1  # ausente do canal
                elif (info.get("status") or "").lower() not in status_ativos:
                    count += 1  # presente mas pausado/fechado/inativo
            return count

        resultado = {
            "executado_em": datetime.now(timezone.utc).isoformat(),
            "duracao_segundos": round(time.time() - inicio, 1),
            "bling_aviso": bling_erro,  # None ou mensagem se coleta foi parcial
            "stats": {
                "total_bling": len(skus_bling),
                "total_bling_ativos": len(skus_bling_ativos),
                "total_bling_inativos": len(skus_bling_inativos),
                "sem_sku_no_bling": sem_sku_bling,
                "total_skus_universo": len(todos_skus),
                "ml": _stats_canal(skus_ml, ml_ok),
                "shopify": _stats_canal(skus_shopify, shopify_ok),
                "amazon": _stats_canal(skus_amazon, amazon_ok),
                "shopee": _stats_canal(skus_shopee, shopee_ok),
                # Todos os Bling
                "bling_sem_ml": _bling_sem(skus_ml, ml_ok),
                "bling_sem_shopify": _bling_sem(skus_shopify, shopify_ok),
                "bling_sem_amazon": _bling_sem(skus_amazon, amazon_ok),
                "bling_sem_shopee": _bling_sem(skus_shopee, shopee_ok),
                # Só Ativos
                "bling_sem_ml_ativos": _bling_sem_situacao(skus_ml, ml_ok, skus_bling_ativos),
                "bling_sem_shopify_ativos": _bling_sem_situacao(skus_shopify, shopify_ok, skus_bling_ativos),
                "bling_sem_amazon_ativos": _bling_sem_situacao(skus_amazon, amazon_ok, skus_bling_ativos),
                "bling_sem_shopee_ativos": _bling_sem_situacao(skus_shopee, shopee_ok, skus_bling_ativos),
                # Só Inativos
                "bling_sem_ml_inativos": _bling_sem_situacao(skus_ml, ml_ok, skus_bling_inativos),
                "bling_sem_shopify_inativos": _bling_sem_situacao(skus_shopify, shopify_ok, skus_bling_inativos),
                "bling_sem_amazon_inativos": _bling_sem_situacao(skus_amazon, amazon_ok, skus_bling_inativos),
                "bling_sem_shopee_inativos": _bling_sem_situacao(skus_shopee, shopee_ok, skus_bling_inativos),
                # Bling ativo SEM listing ativo no canal (ausente + pausado/fechado/inativo)
                # Métrica real: quantos produtos ativos você tem sem VENDER em cada canal
                "bling_sem_ml_ativo_ativo": _bling_ativo_sem_canal_ativo(skus_ml, ml_ok, skus_bling_ativos, {"active"}),
                "bling_sem_shopify_ativo_ativo": _bling_ativo_sem_canal_ativo(skus_shopify, shopify_ok, skus_bling_ativos, {"active"}),
                "bling_sem_amazon_ativo_ativo": _bling_ativo_sem_canal_ativo(skus_amazon, amazon_ok, skus_bling_ativos, {"active"}),
                "bling_sem_shopee_ativo_ativo": _bling_ativo_sem_canal_ativo(skus_shopee, shopee_ok, skus_bling_ativos, {"active"}),
                # TODOS do Bling SEM listing ativo no canal (canal filter = ativos, bling filter = todos)
                "bling_sem_ml_canal_ativo": _bling_ativo_sem_canal_ativo(skus_ml, ml_ok, skus_bling, {"active"}),
                "bling_sem_shopify_canal_ativo": _bling_ativo_sem_canal_ativo(skus_shopify, shopify_ok, skus_bling, {"active"}),
                "bling_sem_amazon_canal_ativo": _bling_ativo_sem_canal_ativo(skus_amazon, amazon_ok, skus_bling, {"active"}),
                "bling_sem_shopee_canal_ativo": _bling_ativo_sem_canal_ativo(skus_shopee, shopee_ok, skus_bling, {"active"}),
                # Bling INATIVOS SEM listing ativo no canal
                "bling_sem_ml_inativo_canal_ativo": _bling_ativo_sem_canal_ativo(skus_ml, ml_ok, skus_bling_inativos, {"active"}),
                "bling_sem_shopify_inativo_canal_ativo": _bling_ativo_sem_canal_ativo(skus_shopify, shopify_ok, skus_bling_inativos, {"active"}),
                "bling_sem_amazon_inativo_canal_ativo": _bling_ativo_sem_canal_ativo(skus_amazon, amazon_ok, skus_bling_inativos, {"active"}),
                "bling_sem_shopee_inativo_canal_ativo": _bling_ativo_sem_canal_ativo(skus_shopee, shopee_ok, skus_bling_inativos, {"active"}),
            },
            "sem_sku_ml": sem_sku_ml[:200],  # ML sem SKU (não mapeável após todos os fallbacks)
            "ml_anuncios_remapeados": len(remapped_by_anuncio),  # anúncios ML mapeados via Bling /anuncios
            "ml_gtin_remapeados": len(remapped_by_gtin),  # anúncios ML mapeados via fallback GTIN
            "sem_sku_shopee": sem_sku_shopee[:200],  # Shopee sem item_sku (não mapeável após fallback)
            "shopee_anuncios_remapeados": len(remapped_by_anuncio_shopee),  # itens Shopee remapeados via Bling /anuncios
            # Canal sem Bling: itens COM SKU no canal que NÃO existem no Bling
            # Sem integração: anúncios COM SKU no canal que NÃO existem no Bling
            "ml_sem_bling": sorted(
                [{
                    "sku": s,
                    "ml_id": v.get("ml_id",""),
                    "status": v.get("status",""),
                    "qty": v.get("qty"),
                    "titulo": v.get("titulo",""),
                    # Classifica o tipo de divergência:
                    # "ean_divergente"      → chave é EAN/GTIN numérico (8-14 dígitos) sem match Bling
                    # "sku_nao_encontrado"  → SKU alfanumérico não existe no Bling
                    "tipo_divergencia": "ean_divergente" if (s.isdigit() and 8 <= len(s) <= 14) else "sku_nao_encontrado",
                }
                 for s, v in skus_ml.items() if s not in skus_bling],
                key=lambda x: x["sku"]
            )[:1000] if ml_ok else [],
            "shopify_sem_bling": sorted(
                [{"sku": s, "variant_id": v.get("variant_id",""), "status": v.get("status",""), "qty": v.get("qty")}
                 for s, v in skus_shopify.items() if s not in skus_bling],
                key=lambda x: x["sku"]
            )[:1000] if shopify_ok else [],
            "amazon_sem_bling": sorted(
                [{"sku": s, "status": v.get("status",""), "nome": v.get("nome",""), "qty": v.get("qty"),
                  "tipo_divergencia": (
                      "ean_divergente"   if (s.isdigit() and 8 <= len(s) <= 14) else
                      "sku_nao_encontrado"
                  )}
                 for s, v in skus_amazon.items() if s not in skus_bling],
                key=lambda x: x["sku"]
            )[:1000] if amazon_ok else [],
            "shopee_sem_bling": sorted(
                [{"sku": s, "item_id": v.get("item_id",""), "status": v.get("status",""), "qty": v.get("qty"),
                  "tipo_divergencia": (
                      "codigo_catalogo" if (s.isdigit() and len(s) > 14) else
                      "ean_divergente"   if (s.isdigit() and 8 <= len(s) <= 14) else
                      "sku_nao_encontrado"
                  )}
                 for s, v in skus_shopee.items() if s not in skus_bling],
                key=lambda x: x["sku"]
            )[:1000] if shopee_ok else [],
            "matrix": matrix,
            "auditoria": auditoria[:2000],       # itens com problemas, ordenados por gravidade
            "resumo_auditoria": resumo_auditoria, # contagens por tipo/canal/prioridade
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
