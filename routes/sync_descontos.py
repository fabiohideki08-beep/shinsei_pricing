"""
sync_descontos.py
Mantém a coleção "Descontos acima de 50%" sincronizada automaticamente.
- Adiciona produtos que atingem >= 50% de desconto
- Remove produtos que caíram abaixo de 50%

Uso:
  python routes/sync_descontos.py          # roda uma vez
  python routes/sync_descontos.py --dry    # simula sem alterar

Agendar no Windows Task Scheduler ou cron para rodar de hora em hora.
"""

import sys, json, time, argparse, logging, requests
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).resolve().parent.parent
CONFIG_PATH   = BASE_DIR / 'data' / 'shopify_config.json'
STORE         = 'pknw4n-eg.myshopify.com'
API_VERSION   = '2024-01'
COLLECTION_ID = 531696484657       # "Descontos acima de 50%"
DESCONTO_MIN  = 50.0               # percentual mínimo

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / 'data' / 'sync_descontos.log', encoding='utf-8'),
    ]
)
log = logging.getLogger('sync_descontos')

# ── Helpers ────────────────────────────────────────────────────────────────
def get_headers():
    cfg = json.loads(CONFIG_PATH.read_text())
    return {
        'X-Shopify-Access-Token': cfg['access_token'],
        'Content-Type': 'application/json',
    }

BASE_URL = f'https://{STORE}/admin/api/{API_VERSION}'

def paginate(url, key, headers):
    """Itera sobre todas as páginas de um endpoint REST do Shopify."""
    while url:
        r = requests.get(url, headers=headers)
        r.raise_for_status()
        yield from r.json().get(key, [])
        link = r.headers.get('Link', '')
        url = None
        for part in link.split(','):
            if 'rel="next"' in part:
                url = part.strip().split(';')[0].strip('<> ')
                break
        time.sleep(0.05)

def get_qualifying_products(headers):
    """Retorna set de product_ids com desconto >= DESCONTO_MIN em pelo menos 1 variante."""
    qualifying = set()
    url = f'{BASE_URL}/products.json?limit=250&status=active'
    for prod in paginate(url, 'products', headers):
        for var in prod.get('variants', []):
            price   = float(var.get('price', 0) or 0)
            compare = float(var.get('compare_at_price', 0) or 0)
            if compare > 0 and price > 0:
                pct = (compare - price) / compare * 100
                if pct >= DESCONTO_MIN:
                    qualifying.add(prod['id'])
                    break
    return qualifying

def get_collection_members(headers):
    """Retorna dict {product_id: collect_id} dos produtos atualmente na coleção."""
    members = {}
    url = f'{BASE_URL}/collects.json?collection_id={COLLECTION_ID}&limit=250'
    for c in paginate(url, 'collects', headers):
        members[c['product_id']] = c['id']
    return members

# ── Main ───────────────────────────────────────────────────────────────────
def sync(dry_run=False):
    headers = get_headers()
    label = '[DRY-RUN] ' if dry_run else ''

    log.info(f'{label}Iniciando sync da coleção {COLLECTION_ID} (mínimo {DESCONTO_MIN}%)')

    qualifying  = get_qualifying_products(headers)
    members     = get_collection_members(headers)
    current_ids = set(members.keys())

    to_add    = qualifying - current_ids
    to_remove = current_ids - qualifying

    log.info(f'Qualificados: {len(qualifying)} | Na coleção: {len(current_ids)} | Adicionar: {len(to_add)} | Remover: {len(to_remove)}')

    # Adicionar
    added = 0
    for pid in to_add:
        if not dry_run:
            r = requests.post(
                f'{BASE_URL}/collects.json',
                headers=headers,
                json={'collect': {'collection_id': COLLECTION_ID, 'product_id': pid}}
            )
            if r.status_code in (200, 201, 422):
                added += 1
            time.sleep(0.05)
        else:
            added += 1
    log.info(f'{label}Adicionados: {added}')

    # Remover
    removed = 0
    for pid in to_remove:
        collect_id = members[pid]
        if not dry_run:
            r = requests.delete(f'{BASE_URL}/collects/{collect_id}.json', headers=headers)
            if r.status_code in (200, 204):
                removed += 1
            time.sleep(0.05)
        else:
            removed += 1
    log.info(f'{label}Removidos: {removed}')

    log.info(f'{label}Sync concluído. Coleção agora tem ~{len(qualifying)} produtos.')
    return {'added': added, 'removed': removed, 'total': len(qualifying)}

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry', action='store_true', help='Simula sem alterar')
    args = parser.parse_args()
    result = sync(dry_run=args.dry)
    print(json.dumps(result, indent=2))
