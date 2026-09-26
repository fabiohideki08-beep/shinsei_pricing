# -*- coding: utf-8 -*-
"""
limpar_barcodes_kits.py
=======================
Remove o barcode (GTIN/EAN) de todos os produtos Kit/Combo/Duo no Shopify.

MOTIVO:
  Kits não têm um único EAN — cada componente tem o seu próprio.
  O Google Merchant Center cruza GTINs com bases de dados externas e pode
  reprovar um kit inteiro se o barcode de um componente coincidir com um
  produto de categoria restrita (ex: tabaco).

IDENTIFICAÇÃO DE KIT:
  Produto é considerado kit se o título contiver qualquer das keywords abaixo.
  Para adicionar novas keywords, edite KIT_KEYWORDS.

USO:
  python limpar_barcodes_kits.py            # executa e limpa
  python limpar_barcodes_kits.py --dry-run  # só mostra o que faria, sem alterar
"""

import sys
import io
import json
import time
import logging
import argparse
import requests
from pathlib import Path
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ─────────────────────────────────────────────────────────────
# Configuração
# ─────────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).parent / "data"

shopify_cfg = json.loads((DATA_DIR / "shopify_config.json").read_text(encoding="utf-8"))
SHOPIFY_TOKEN = shopify_cfg["access_token"]
SHOPIFY_BASE = "https://pknw4n-eg.myshopify.com/admin/api/2024-01"
SH = {"X-Shopify-Access-Token": SHOPIFY_TOKEN, "Content-Type": "application/json"}

# Keywords que identificam um produto como kit/combo
# (busca case-insensitive no título)
KIT_KEYWORDS = [
    "kit",
    "combo",
    "duo ",
    "par ",
    "2 x ",
    "2x ",
    "3 x ",
    "3x ",
    "4 x ",
    "4x ",
    "5 x ",
    "5x ",
    "6 x ",
    "6x ",
    "10 x ",
    "10x ",
    "12 x ",
    "12x ",
    "c/2",
    "c/3",
    "c/4",
    "c/6",
    "pack",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("limpar_barcodes_kits")


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def is_kit(product: dict) -> bool:
    """Retorna True se o título do produto indicar que é um kit/combo."""
    title = product.get("title", "").lower()
    return any(kw in title for kw in KIT_KEYWORDS)


def fetch_all_products() -> list:
    """Busca todos os produtos ativos do Shopify com paginação."""
    products = []
    url = f"{SHOPIFY_BASE}/products.json"
    params = {
        "limit": 250,
        "status": "active",
        "fields": "id,title,variants",
    }

    while url:
        r = requests.get(url, headers=SH, params=params, timeout=30)
        r.raise_for_status()
        products.extend(r.json().get("products", []))
        params = {}

        # Paginação via Link header
        url = None
        for part in r.headers.get("Link", "").split(","):
            if 'rel="next"' in part:
                url = part.strip().split(";")[0].strip().strip("<>")

        log.debug(f"  Carregados {len(products)} produtos até agora...")

    return products


def clear_variant_barcode(variant_id: int, retries: int = 3) -> bool:
    """Remove o barcode de uma variante. Retorna True se OK. Tenta até `retries` vezes."""
    for attempt in range(1, retries + 1):
        try:
            r = requests.put(
                f"{SHOPIFY_BASE}/variants/{variant_id}.json",
                headers=SH,
                json={"variant": {"id": variant_id, "barcode": ""}},
                timeout=15,
            )
            if r.status_code == 429:
                # Rate limited — espera e tenta de novo
                wait = float(r.headers.get("Retry-After", "2"))
                log.warning(f"    Rate limit (429) na variante {variant_id}. Aguardando {wait}s...")
                time.sleep(wait)
                continue
            return r.status_code == 200
        except requests.exceptions.ConnectionError as e:
            log.warning(f"    ConnectionError na variante {variant_id} (tentativa {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(2 * attempt)  # backoff: 2s, 4s
            else:
                return False
        except requests.exceptions.Timeout:
            log.warning(f"    Timeout na variante {variant_id} (tentativa {attempt}/{retries})")
            if attempt < retries:
                time.sleep(2 * attempt)
            else:
                return False
    return False


# ─────────────────────────────────────────────────────────────
# Função principal (pode ser importada pelo scheduler)
# ─────────────────────────────────────────────────────────────

def limpar_barcodes_kits(dry_run: bool = False) -> dict:
    """
    Identifica produtos kit/combo e remove seus barcodes no Shopify.

    Args:
        dry_run: Se True, apenas reporta o que faria sem alterar nada.

    Returns:
        dict com estatísticas da execução.
    """
    inicio = datetime.now()
    log.info("=" * 65)
    log.info(f"Limpeza de barcodes de kits — {'DRY-RUN' if dry_run else 'EXECUÇÃO REAL'}")
    log.info("=" * 65)

    log.info("Buscando produtos ativos no Shopify...")
    all_products = fetch_all_products()
    log.info(f"  {len(all_products)} produtos ativos encontrados")

    # Filtra os kits com barcode
    kits_com_barcode = []
    for p in all_products:
        if not is_kit(p):
            continue
        variants_com_barcode = [
            v for v in p.get("variants", []) if v.get("barcode")
        ]
        if variants_com_barcode:
            kits_com_barcode.append((p, variants_com_barcode))

    total_kits   = sum(1 for p in all_products if is_kit(p))
    total_vars   = sum(len(vs) for _, vs in kits_com_barcode)

    log.info(f"  {total_kits} kits identificados no total")
    log.info(f"  {len(kits_com_barcode)} kits têm barcode (precisam limpeza)")
    log.info(f"  {total_vars} variantes com barcode para limpar")

    if not kits_com_barcode:
        log.info("Nenhum barcode para limpar — tudo OK!")
        return {
            "status": "ok",
            "kits_total": total_kits,
            "kits_limpos": 0,
            "variantes_limpas": 0,
            "erros": 0,
            "dry_run": dry_run,
        }

    ok_count = err_count = var_count = 0

    for produto, variants in kits_com_barcode:
        prod_id = produto["id"]
        title   = produto["title"][:55]
        log.info(f"\n  [{prod_id}] {title}")
        log.info(f"    {len(variants)} variantes com barcode")

        for v in variants:
            vid     = v["id"]
            barcode = v["barcode"]
            log.info(f"    -> variant {vid}: '{barcode}' => ''")

            if dry_run:
                var_count += 1
                continue

            time.sleep(0.15)  # Respeita rate limit Shopify (40 req/s)
            if clear_variant_barcode(vid):
                ok_count  += 1
                var_count += 1
            else:
                log.warning(f"    ERRO ao limpar variant {vid}")
                err_count += 1

        if not dry_run:
            ok_count += 0  # já contado por variante

    duracao = (datetime.now() - inicio).seconds

    log.info("\n" + "=" * 65)
    if dry_run:
        log.info(f"DRY-RUN — nada foi alterado.")
        log.info(f"  Seriam limpas: {var_count} variantes em {len(kits_com_barcode)} kits")
    else:
        log.info(f"CONCLUÍDO em {duracao}s")
        log.info(f"  Variantes limpas: {ok_count}")
        log.info(f"  Erros:            {err_count}")
    log.info("=" * 65)

    return {
        "status": "ok" if err_count == 0 else "parcial",
        "kits_com_barcode": len(kits_com_barcode),
        "variantes_limpas": ok_count if not dry_run else var_count,
        "erros": err_count,
        "duracao_segundos": duracao,
        "dry_run": dry_run,
    }


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Remove barcodes de produtos kit/combo no Shopify"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Apenas lista o que seria alterado, sem modificar nada",
    )
    args = parser.parse_args()

    result = limpar_barcodes_kits(dry_run=args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False))
