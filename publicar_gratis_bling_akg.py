"""
Publica todos os produtos do Bling AKG no ML como anúncio Grátis via API Bling v3.
Salva progresso em data/bling_gratis_akg_progress.json.

Uso:
    python publicar_gratis_bling_akg.py --dry-run      # simula sem publicar
    python publicar_gratis_bling_akg.py                # publica de verdade
    python publicar_gratis_bling_akg.py --reset        # começa do zero
    python publicar_gratis_bling_akg.py --limit 10     # testa com 10 itens
"""
import json
import time
import argparse
import sys
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TOKEN_PATH = DATA_DIR / "bling_tokens_akg.json"
PROGRESS_PATH = DATA_DIR / "bling_gratis_akg_progress.json"
RENDER_BASE = "https://shinsei-pricing.onrender.com"
BLING_API = "https://www.bling.com.br/Api/v3"

RATE_LIMIT_SLEEP = 0.4


# ── Token ─────────────────────────────────────────────────────────────────────

def _sync_token():
    """Baixa token Bling AKG do servidor Render e salva localmente."""
    try:
        r = requests.get(f"{RENDER_BASE}/bling/tokens2", timeout=15)
        if r.status_code == 200:
            data = r.json().get("data", {})
            if data.get("access_token"):
                DATA_DIR.mkdir(exist_ok=True)
                TOKEN_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                print("Token Bling AKG sincronizado do servidor.", flush=True)
                return True
        print(f"Aviso: não foi possível sincronizar token Bling AKG ({r.status_code})", flush=True)
    except Exception as e:
        print(f"Aviso: erro ao sincronizar token Bling AKG: {e}", flush=True)
    return False


def _load_token() -> str:
    if not TOKEN_PATH.exists():
        raise RuntimeError(f"Token não encontrado: {TOKEN_PATH}. Autorize o Bling AKG em /integracoes.")
    data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    token = data.get("access_token", "")
    if not token:
        raise RuntimeError("access_token vazio no arquivo de token AKG.")
    return token


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _refresh_if_needed(token: str) -> str:
    """Tenta refresh via servidor se o token estiver expirado."""
    # Testa o token
    r = requests.get(f"{BLING_API}/produtos", params={"pagina": 1, "limite": 1},
                     headers=_headers(token), timeout=10)
    if r.status_code == 401:
        print("Token expirado, tentando refresh via servidor...", flush=True)
        try:
            r2 = requests.post(f"{RENDER_BASE}/bling/akg/refresh", timeout=15)
            if r2.status_code == 200:
                # Baixa token novo
                time.sleep(2)
                token = _load_token()
                print("Token renovado.", flush=True)
        except Exception as e:
            print(f"Erro no refresh: {e}", flush=True)
    return token


# ── Produtos ───────────────────────────────────────────────────────────────────

def _get_all_products(token: str) -> list[dict]:
    """Busca todos os produtos ativos do Bling AKG."""
    all_products = []
    pagina = 1
    while True:
        r = requests.get(f"{BLING_API}/produtos",
                         params={"pagina": pagina, "limite": 100, "situacao": "A"},
                         headers=_headers(token), timeout=20)
        if r.status_code != 200:
            print(f"Erro ao buscar produtos página {pagina}: {r.status_code} {r.text[:200]}", flush=True)
            break
        items = r.json().get("data", [])
        if not items:
            break
        all_products.extend(items)
        print(f"  Página {pagina}: {len(items)} produtos (total={len(all_products)})", flush=True)
        pagina += 1
        time.sleep(0.2)
    return all_products


def _get_product_detail(produto_id: int, token: str) -> dict:
    """Busca detalhes completos de um produto (com preço)."""
    r = requests.get(f"{BLING_API}/produtos/{produto_id}",
                     headers=_headers(token), timeout=15)
    if r.status_code == 200:
        return r.json().get("data", {})
    return {}


# ── Anúncios existentes ────────────────────────────────────────────────────────

def _get_published_skus(id_loja: int, token: str) -> set[str]:
    """Busca SKUs que já têm anúncio Grátis no ML via Bling."""
    skus = set()
    pagina = 1
    while True:
        r = requests.get(f"{BLING_API}/anuncios",
                         params={"idLoja": id_loja, "pagina": pagina, "limite": 100},
                         headers=_headers(token), timeout=20)
        if r.status_code != 200:
            break
        items = r.json().get("data", [])
        if not items:
            break
        for a in items:
            sku = (a.get("produto", {}) or {}).get("codigo", "") or a.get("idVendedor", "")
            if sku:
                skus.add(str(sku))
        pagina += 1
        time.sleep(0.2)
    return skus


# ── Descobrir ID da loja ML ────────────────────────────────────────────────────

def _find_id_loja(token: str) -> int:
    """Tenta descobrir o idLoja do Mercado Livre no Bling AKG."""
    # Tenta via anúncios existentes
    r = requests.get(f"{BLING_API}/anuncios", params={"pagina": 1, "limite": 5},
                     headers=_headers(token), timeout=15)
    if r.status_code == 200:
        items = r.json().get("data", [])
        for a in items:
            loja = (a.get("loja") or {})
            id_loja = loja.get("id") or loja.get("idLoja")
            if id_loja:
                print(f"ID loja ML encontrado via anúncio: {id_loja}", flush=True)
                return int(id_loja)

    # Tenta via /lojas ou /canais-de-venda
    for ep in ["/lojas", "/canais-de-venda", "/integracoes"]:
        r = requests.get(f"{BLING_API}{ep}", headers=_headers(token), timeout=15)
        if r.status_code == 200:
            items = r.json().get("data", [])
            for item in items:
                nome = str(item.get("descricao", "") or item.get("nome", "")).lower()
                if "mercado" in nome or "ml" in nome:
                    id_loja = item.get("id")
                    if id_loja:
                        print(f"ID loja ML encontrado via {ep}: {id_loja}", flush=True)
                        return int(id_loja)

    return 0


# ── Publicar anúncio ──────────────────────────────────────────────────────────

def _publish_listing(produto: dict, id_loja: int, token: str) -> tuple[bool, str]:
    """Cria anúncio Grátis no ML via Bling API."""
    produto_id = produto.get("id")
    nome = produto.get("nome", "")
    codigo = produto.get("codigo", "")

    # Busca detalhes para pegar preço
    detail = _get_product_detail(produto_id, token)
    preco = detail.get("preco") or produto.get("preco", 0)

    if not preco or float(preco) <= 0:
        return False, "preco_zero"

    payload = {
        "idLoja": id_loja,
        "idProduto": produto_id,
        "tipo": "gratis",
        "preco": float(preco),
    }

    r = requests.post(f"{BLING_API}/anuncios", json=payload,
                      headers=_headers(token), timeout=30)

    if r.status_code in (200, 201):
        anuncio_id = r.json().get("data", {}).get("id", "")
        return True, str(anuncio_id)

    err = r.text[:300]
    # Tenta sem idProduto (só com titulo se API não aceitar idProduto diretamente)
    if r.status_code == 400 and "idProduto" in err:
        payload2 = {
            "idLoja": id_loja,
            "tipo": "gratis",
            "preco": float(preco),
            "titulo": nome[:60],
            "codigoProduto": codigo,
        }
        r2 = requests.post(f"{BLING_API}/anuncios", json=payload2,
                           headers=_headers(token), timeout=30)
        if r2.status_code in (200, 201):
            anuncio_id = r2.json().get("data", {}).get("id", "")
            return True, str(anuncio_id)
        err = r2.text[:300]

    return False, err


# ── Progresso ──────────────────────────────────────────────────────────────────

def _load_progress() -> dict:
    if PROGRESS_PATH.exists():
        try:
            return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"publicados": [], "falhas": [], "skipped": [], "id_loja": 0}


def _save_progress(p: dict):
    PROGRESS_PATH.write_text(json.dumps(p, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Main ───────────────────────────────────────────────────────────────────────

def run(reset=False, dry_run=False, limit=0, id_loja_arg=0):
    _sync_token()
    token = _load_token()
    token = _refresh_if_needed(token)

    progress = ({"publicados": [], "falhas": [], "skipped": [], "id_loja": 0}
                if reset else _load_progress())

    # Descobre ID da loja
    id_loja = id_loja_arg or progress.get("id_loja") or 0
    if not id_loja:
        id_loja = _find_id_loja(token)
        if not id_loja:
            print("ERRO: Não foi possível descobrir o ID da loja ML no Bling AKG.", flush=True)
            print("Use --id-loja <ID> para especificar manualmente.", flush=True)
            return
        progress["id_loja"] = id_loja

    print(f"ID loja ML: {id_loja}", flush=True)

    # Busca todos os produtos
    print("Buscando todos os produtos do Bling AKG...", flush=True)
    produtos = _get_all_products(token)
    print(f"Total de produtos ativos: {len(produtos)}", flush=True)

    # Busca SKUs já publicados
    print("Verificando anúncios já existentes...", flush=True)
    ja_publicados_skus = _get_published_skus(id_loja, token)
    ja_ids = {p.get("produto_id", "") for p in progress["publicados"]}
    print(f"Já publicados no ML: {len(ja_publicados_skus)}", flush=True)

    publicados = len(progress["publicados"])
    falhas = len(progress["falhas"])
    processados = 0

    for produto in produtos:
        if limit and processados >= limit:
            break

        produto_id = str(produto.get("id", ""))
        codigo = str(produto.get("codigo", "") or "")
        nome = produto.get("nome", "")[:50]

        # Pula se já publicado
        if produto_id in ja_ids or codigo in ja_publicados_skus:
            progress["skipped"].append({"id": produto_id, "codigo": codigo})
            processados += 1
            continue

        if dry_run:
            preco = produto.get("preco", 0)
            print(f"[DRY] {codigo} | {nome} | R$ {preco}", flush=True)
            publicados += 1
        else:
            ok, result = _publish_listing(produto, id_loja, token)
            if ok:
                print(f"✓ {codigo} | {nome} → anuncio={result}", flush=True)
                progress["publicados"].append({
                    "produto_id": produto_id,
                    "codigo": codigo,
                    "anuncio_bling_id": result,
                })
                publicados += 1
            else:
                print(f"✗ {codigo} | {nome} → {result[:80]}", flush=True)
                progress["falhas"].append({
                    "produto_id": produto_id,
                    "codigo": codigo,
                    "erro": result,
                })
                falhas += 1
            time.sleep(RATE_LIMIT_SLEEP)

        processados += 1
        if processados % 50 == 0:
            _save_progress(progress)
            print(f"  --- publicados={publicados} falhas={falhas} processados={processados} ---", flush=True)

    _save_progress(progress)
    print(f"\n=== CONCLUIDO: publicados={publicados} falhas={falhas} skipped={len(progress['skipped'])} ===", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="Começa do zero")
    parser.add_argument("--dry-run", action="store_true", help="Simula sem publicar")
    parser.add_argument("--limit", type=int, default=0, help="Limite de itens")
    parser.add_argument("--id-loja", type=int, default=0, help="ID da loja ML no Bling AKG")
    args = parser.parse_args()
    run(reset=args.reset, dry_run=args.dry_run, limit=args.limit, id_loja_arg=args.id_loja)
