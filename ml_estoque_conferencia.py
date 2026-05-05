"""
ml_estoque_conferencia.py — Shinsei Pricing
Módulo de conferência de estoque entre Bling e Mercado Livre.

Estratégia:
- Varre todos os anúncios ativos do ML via API
- Cruza pelo campo seller_custom_field (SKU do seller) com o Bling
- Anúncios sem SKU → alerta para cadastrar
- Divergência de estoque → alerta na fila de auditoria
"""
from __future__ import annotations
import json
import logging
import requests
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)
DATA_DIR = Path(__file__).parent / "data"
FILA_ESTOQUE_ML_PATH = DATA_DIR / "fila_estoque_ml.json"
ML_TOKENS_PATH = DATA_DIR / "ml_tokens.json"


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists(): return default
    try: return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return default

def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def carregar_fila_estoque_ml() -> list[dict]:
    itens = _load_json(FILA_ESTOQUE_ML_PATH, [])
    return itens if isinstance(itens, list) else []

def salvar_fila_estoque_ml(itens: list[dict]) -> None:
    _save_json(FILA_ESTOQUE_ML_PATH, itens)

def stats_fila_estoque_ml() -> dict:
    itens = carregar_fila_estoque_ml()
    stats = {"pendente": 0, "corrigido": 0, "ignorado": 0, "sem_sku": 0}
    for item in itens:
        s = str(item.get("status", "pendente")).lower()
        if s in stats: stats[s] += 1
    stats["total"] = len(itens)
    return stats

def _ml_headers() -> dict:
    tokens = _load_json(ML_TOKENS_PATH, {})
    token = tokens.get("access_token", "")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def _ml_get_seller_id() -> Optional[int]:
    try:
        res = requests.get("https://api.mercadolibre.com/users/me", headers=_ml_headers(), timeout=10)
        if res.status_code == 200:
            return res.json().get("id")
    except Exception as e:
        logger.warning("Erro ao buscar seller_id ML: %s", e)
    return None

def _ml_get_item_sku(item: dict) -> Optional[str]:
    """Extrai SKU do anúncio — tenta seller_custom_field e attributes SELLER_SKU."""
    sku = item.get("seller_custom_field")
    if sku: return str(sku).strip()
    for attr in item.get("attributes", []):
        if attr.get("id") == "SELLER_SKU":
            v = attr.get("value_name")
            if v: return str(v).strip()
    return None


def _ml_get_attr(item: dict, attr_id: str) -> Optional[str]:
    """Retorna o value_name de um atributo ML pelo ID."""
    for a in item.get("attributes", []):
        if a.get("id") == attr_id:
            v = a.get("value_name")
            return str(v).strip() if v else None
    return None


def _normalizar_titulo(titulo: str) -> str:
    """Remove sufixos de loja e normaliza o título para busca no Bling."""
    import re
    titulo = re.split(r"\s[–—]\s", titulo)[0]
    titulo = re.sub(r"\(\d+\)", "", titulo)
    return titulo.strip()


def _termos_busca_titulo(titulo: str) -> list[str]:
    """
    Gera termos de busca progressivamente menores e mais específicos.
    Prioriza palavras não-genéricas (remove Kit, Ox, volumes, preposições).
    """
    import re
    titulo_limpo = _normalizar_titulo(titulo)
    palavras = titulo_limpo.split()

    # Palavras genéricas que poluem a busca no Bling
    SKIP = {"kit", "ox", "com", "de", "da", "do", "e", "em", "para", "vol",
            "ml", "g", "kg", "un", "unidades", "todas", "as", "cores", "+"}

    # Filtra palavras significativas (sem números isolados, sem skip words)
    significativas = [
        p for p in palavras
        if p.lower() not in SKIP and not re.match(r"^\d+$", p)
    ]

    termos = []
    # Termo 1: título completo (até 8 palavras)
    if len(palavras) >= 4:
        termos.append(" ".join(palavras[:8]))
    # Termo 2: só palavras significativas (até 6)
    if significativas:
        termos.append(" ".join(significativas[:6]))
    # Termo 3: apenas as últimas 4 palavras significativas (marca + produto)
    if len(significativas) > 3:
        termos.append(" ".join(significativas[-4:]))
    # Termo 4: últimas 2 palavras significativas (mínimo fallback)
    if len(significativas) > 1:
        termos.append(" ".join(significativas[-2:]))

    # Deduplica mantendo ordem
    seen, unique = set(), []
    for t in termos:
        if t not in seen and len(t) >= 5:
            seen.add(t)
            unique.append(t)
    return unique


def _buscar_sku_no_bling(bling_client, item: dict) -> dict:
    """
    Busca o SKU no Bling para um anúncio ML sem SKU.
    Estratégia em 3 etapas:
      1. GTIN do item (atributo ML) — mais confiável
      2. Série de buscas por título com termos progressivamente menores
      3. Marca + linha do produto (fallback)

    Retorna:
      - encontrado: bool
      - sku: str | None
      - nome_bling: str | None
      - estoque: int | None
      - candidatos: list
      - confianca: 'alta' | 'media' | 'baixa'
      - metodo: str
    """
    titulo = item.get("title") or item.get("titulo") or ""
    titulo_limpo = _normalizar_titulo(titulo)

    # ── Etapa 1: busca por GTIN ─────────────────────────────────────────
    gtin = _ml_get_attr(item, "GTIN")
    if gtin:
        try:
            candidatos = bling_client.search_by_gtin(gtin)
        except Exception:
            candidatos = []
        if candidatos:
            c = candidatos[0]
            return {
                "encontrado": True,
                "sku":        c["codigo"],
                "nome_bling": c["nome"],
                "estoque":    c["estoque"],
                "candidatos": candidatos,
                "confianca":  "alta",
                "metodo":     "gtin",
            }

    # ── Etapa 2: busca por título com termos progressivos ───────────────
    if titulo_limpo:
        termos = _termos_busca_titulo(titulo_limpo)
        for termo in termos:
            try:
                candidatos = bling_client.search_by_name(termo, limit=10)
            except Exception:
                candidatos = []
            if not candidatos:
                continue

            if len(candidatos) == 1:
                c = candidatos[0]
                return {
                    "encontrado": True,
                    "sku":        c["codigo"],
                    "nome_bling": c["nome"],
                    "estoque":    c["estoque"],
                    "candidatos": candidatos,
                    "confianca":  "media",
                    "metodo":     f"titulo:{termo[:30]}",
                }
            # Múltiplos: retorna para o usuário escolher
            return {
                "encontrado": False,
                "candidatos": candidatos,
                "confianca":  "baixa",
                "metodo":     f"titulo:{termo[:30]}",
            }

    # ── Etapa 3: busca por marca + linha ────────────────────────────────
    brand = _ml_get_attr(item, "BRAND")
    line  = _ml_get_attr(item, "LINE")
    if brand:
        termo_marca = f"{brand} {line}".strip() if line else brand
        try:
            candidatos = bling_client.search_by_name(termo_marca, limit=10)
        except Exception:
            candidatos = []
        if len(candidatos) == 1:
            c = candidatos[0]
            return {
                "encontrado": True,
                "sku":        c["codigo"],
                "nome_bling": c["nome"],
                "estoque":    c["estoque"],
                "candidatos": candidatos,
                "confianca":  "baixa",
                "metodo":     f"marca:{termo_marca}",
            }
        if candidatos:
            return {
                "encontrado": False,
                "candidatos": candidatos,
                "confianca":  "baixa",
                "metodo":     f"marca:{termo_marca}",
            }

    return {"encontrado": False, "candidatos": [], "metodo": "nenhum"}

def _ml_listar_anuncios(seller_id: int, limit: int = 50, offset: int = 0) -> list[str]:
    """Busca IDs de anúncios ativos do seller."""
    try:
        res = requests.get(
            f"https://api.mercadolibre.com/users/{seller_id}/items/search",
            params={"status": "active", "limit": limit, "offset": offset},
            headers=_ml_headers(),
            timeout=15
        )
        if res.status_code == 200:
            data = res.json()
            return data.get("results", []), data.get("paging", {}).get("total", 0)
    except Exception as e:
        logger.warning("Erro ao listar anúncios ML: %s", e)
    return [], 0

def _ml_get_item(item_id: str) -> Optional[dict]:
    """Busca detalhes de um anúncio."""
    try:
        res = requests.get(
            f"https://api.mercadolibre.com/items/{item_id}",
            headers=_ml_headers(),
            timeout=10
        )
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        logger.warning("Erro ao buscar item ML %s: %s", item_id, e)
    return None

def _ml_atualizar_sku(item_id: str, sku: str) -> dict:
    """Atualiza o SKU de um anúncio no ML via atributo SELLER_SKU."""
    try:
        res = requests.put(
            f"https://api.mercadolibre.com/items/{item_id}",
            json={"attributes": [{"id": "SELLER_SKU", "value_name": sku}]},
            headers=_ml_headers(),
            timeout=15
        )
        if res.status_code == 200:
            return {"ok": True, "item_id": item_id, "sku": sku}
        return {"ok": False, "erro": res.text[:200]}
    except Exception as e:
        return {"ok": False, "erro": str(e)}

def _ml_atualizar_estoque(item_id: str, quantidade: int) -> dict:
    """Atualiza estoque de um anúncio no ML."""
    try:
        res = requests.put(
            f"https://api.mercadolibre.com/items/{item_id}",
            json={"available_quantity": quantidade},
            headers=_ml_headers(),
            timeout=15
        )
        if res.status_code == 200:
            return {"ok": True, "item_id": item_id, "quantidade": quantidade}
        return {"ok": False, "erro": res.text[:200]}
    except Exception as e:
        return {"ok": False, "erro": str(e)}

def conferir_estoques_ml(bling_client, max_paginas: int = 5, progresso_cb=None) -> dict:
    """
    Varre anúncios ativos do ML e confere estoque vs Bling.
    """
    seller_id = _ml_get_seller_id()
    if not seller_id:
        return {"ok": False, "erro": "Não foi possível obter seller_id do ML. Reconecte o ML."}

    fila = carregar_fila_estoque_ml()
    chaves_pendentes = {
        (i.get("item_id_ml"), i.get("tipo"))
        for i in fila if i.get("status") == "pendente"
    }

    verificados = 0
    sem_sku = 0
    divergencias = 0
    erros = 0
    limit = 50

    for pagina in range(max_paginas):
        offset = pagina * limit
        item_ids, total = _ml_listar_anuncios(seller_id, limit=limit, offset=offset)
        if not item_ids: break

        for item_id in item_ids:
            try:
                item = _ml_get_item(item_id)
                if not item: continue
                time.sleep(0.2)  # rate limit ML
                verificados += 1

                sku = _ml_get_item_sku(item)
                qty_ml = int(item.get("available_quantity") or 0)
                titulo = item.get("title", "")[:80]  # título para exibição (não afeta busca)

                # ── Caso 1: sem SKU ──────────────────────────────
                if not sku:
                    # Busca SKU no Bling: GTIN → título → marca
                    try:
                        time.sleep(0.3)
                        busca = _buscar_sku_no_bling(bling_client, item)
                    except Exception:
                        busca = {"encontrado": False, "candidatos": [], "metodo": "erro"}

                    sku_sugerido   = busca.get("sku")
                    estoque_bling  = busca.get("estoque", 0) or 0
                    nome_bling     = busca.get("nome_bling", "")
                    confianca      = busca.get("confianca", "")
                    metodo         = busca.get("metodo", "")

                    sem_sku += 1
                    chave = (item_id, "sem_sku")
                    if chave not in chaves_pendentes:
                        entry = {
                            "id":             f"ml_semsku_{item_id}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
                            "item_id_ml":     item_id,
                            "titulo":         item.get("title", titulo),
                            "sku":            None,
                            "sku_sugerido":   sku_sugerido,
                            "nome_bling":     nome_bling,
                            "tipo":           "sem_sku",
                            "estoque_bling":  estoque_bling if sku_sugerido else None,
                            "estoque_ml":     qty_ml,
                            "diferenca":      (estoque_bling - qty_ml) if sku_sugerido else None,
                            "status":         "pendente",
                            "criado_em":      datetime.utcnow().isoformat(),
                            "atualizado_em":  None,
                            "permalink":      item.get("permalink", ""),
                            "candidatos_bling": busca.get("candidatos", []),
                            "confianca_sku":  confianca,
                            "metodo_busca":   metodo,
                        }
                        fila.append(entry)
                        chaves_pendentes.add(chave)

                    if busca.get("encontrado") and sku_sugerido and estoque_bling != qty_ml:
                        divergencias += 1
                    continue

                # ── Caso 2: tem SKU → confere com Bling ──────────
                try:
                    time.sleep(0.5)  # rate limit Bling
                    busca = bling_client.get_product_by_sku(sku)
                    if not busca.get("encontrado"):
                        continue
                    prod = busca.get("produto", {})
                    estoque_bling = int(
                        (prod.get("estoque") or {}).get("saldoVirtualTotal") or 0
                    )

                    if estoque_bling != qty_ml:
                        divergencias += 1
                        chave = (item_id, "divergencia")
                        if chave not in chaves_pendentes:
                            fila.append({
                                "id": f"ml_div_{item_id}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
                                "item_id_ml": item_id,
                                "titulo": titulo,
                                "sku": sku,
                                "nome_bling": prod.get("nome", "")[:60],
                                "tipo": "divergencia",
                                "estoque_bling": estoque_bling,
                                "estoque_ml": qty_ml,
                                "diferenca": estoque_bling - qty_ml,
                                "status": "pendente",
                                "criado_em": datetime.utcnow().isoformat(),
                                "atualizado_em": None,
                                "permalink": item.get("permalink", ""),
                            })
                            chaves_pendentes.add(chave)
                            logger.info(
                                "Divergência ML: item=%s sku=%s bling=%d ml=%d",
                                item_id, sku, estoque_bling, qty_ml
                            )
                except Exception as e:
                    erros += 1
                    err_str = str(e)
                    logger.warning("Erro ao buscar SKU %s no Bling: %s", sku, e)
                    # Se é erro de auth Bling, aborta a conferência inteira (não adianta continuar)
                    if "inativa" in err_str.lower() or "autenticado" in err_str.lower() or "Empresa inativa" in err_str:
                        logger.error("BLING AUTH: %s — conferência interrompida", err_str)
                        salvar_fila_estoque_ml(fila)
                        return {
                            "ok": False,
                            "erro": f"Bling desconectado: {err_str}",
                            "verificados": verificados,
                            "sem_sku": sem_sku,
                            "novas_divergencias": divergencias,
                            "erros": erros,
                            "total_pendentes": len([i for i in fila if i.get("status") == "pendente"]),
                        }

            except Exception as e:
                erros += 1
                logger.warning("Erro ao processar item ML %s: %s", item_id, e)

        if progresso_cb:
            try:
                progresso_cb(pagina + 1, max_paginas, verificados, divergencias, sem_sku, erros)
            except Exception:
                pass

        if (pagina + 1) * limit >= total:
            break

    salvar_fila_estoque_ml(fila)
    logger.info(
        "Conferência ML: %d verificados, %d sem SKU, %d divergências, %d erros",
        verificados, sem_sku, divergencias, erros
    )
    return {
        "ok": True,
        "verificados": verificados,
        "sem_sku": sem_sku,
        "novas_divergencias": divergencias,
        "erros": erros,
        "total_pendentes": len([i for i in fila if i.get("status") == "pendente"]),
    }

def corrigir_estoque_ml(item_id_fila: str) -> dict:
    """Corrige estoque do anúncio ML com o valor do Bling."""
    fila = carregar_fila_estoque_ml()
    item = next((i for i in fila if i.get("id") == item_id_fila), None)
    if not item: return {"ok": False, "erro": "Item não encontrado."}
    if item.get("status") != "pendente": return {"ok": False, "erro": "Item já processado."}
    if item.get("tipo") != "divergencia": return {"ok": False, "erro": "Item sem divergência de estoque."}

    estoque_bling = item.get("estoque_bling", 0)
    item_id_ml = item.get("item_id_ml")
    resultado = _ml_atualizar_estoque(item_id_ml, estoque_bling)

    item["status"] = "corrigido" if resultado.get("ok") else "pendente"
    item["atualizado_em"] = datetime.utcnow().isoformat()
    item["resultado"] = resultado
    salvar_fila_estoque_ml(fila)
    return resultado

def cadastrar_sku_ml(item_id_fila: str, sku: str, bling_client=None) -> dict:
    """Cadastra SKU em anúncio sem SKU e verifica estoque."""
    fila = carregar_fila_estoque_ml()
    item = next((i for i in fila if i.get("id") == item_id_fila), None)
    if not item: return {"ok": False, "erro": "Item não encontrado."}

    item_id_ml = item.get("item_id_ml")
    resultado = _ml_atualizar_sku(item_id_ml, sku)
    if not resultado.get("ok"):
        return resultado

    item["sku"] = sku
    item["status"] = "corrigido"
    item["atualizado_em"] = datetime.utcnow().isoformat()
    item["resultado"] = resultado

    # Verifica se há divergência de estoque agora
    if bling_client:
        try:
            busca = bling_client.get_product_by_sku(sku)
            if busca.get("encontrado"):
                prod = busca.get("produto", {})
                estoque_bling = int((prod.get("estoque") or {}).get("saldoVirtualTotal") or 0)
                item["estoque_bling"] = estoque_bling
                item["diferenca"] = estoque_bling - (item.get("estoque_ml") or 0)
        except Exception:
            pass

    salvar_fila_estoque_ml(fila)
    return {"ok": True, "sku_cadastrado": sku, "item_id_ml": item_id_ml}

def ignorar_item_ml(item_id_fila: str) -> dict:
    fila = carregar_fila_estoque_ml()
    item = next((i for i in fila if i.get("id") == item_id_fila), None)
    if not item: return {"ok": False}
    item["status"] = "ignorado"
    item["atualizado_em"] = datetime.utcnow().isoformat()
    salvar_fila_estoque_ml(fila)
    return {"ok": True}
