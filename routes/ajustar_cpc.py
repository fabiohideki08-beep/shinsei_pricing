"""
ajustar_cpc.py
Atualiza o CPC máximo de todos os ad groups para R$ 0,40.

Uso:
  python routes/ajustar_cpc.py          # aplica
  python routes/ajustar_cpc.py --dry    # simula sem alterar
"""

import io, sys, argparse
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from pathlib import Path
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

BASE_DIR    = Path(__file__).resolve().parent.parent
YAML_PATH   = BASE_DIR / 'google-ads.yaml'
CUSTOMER_ID = '2097362078'
NOVO_CPC    = 400_000   # R$ 0,40 em micros (1 real = 1.000.000 micros)

client = GoogleAdsClient.load_from_storage(str(YAML_PATH), version='v24')

def listar_ad_groups():
    """Retorna lista de (ad_group_resource_name, nome, cpc_atual, campaign_name)."""
    ga_service = client.get_service('GoogleAdsService')
    query = """
        SELECT
          ad_group.resource_name,
          ad_group.name,
          ad_group.cpc_bid_micros,
          campaign.name,
          campaign.status
        FROM ad_group
        WHERE campaign.status != 'REMOVED'
          AND ad_group.status != 'REMOVED'
        ORDER BY campaign.name, ad_group.name
    """
    response = ga_service.search_stream(customer_id=CUSTOMER_ID, query=query)
    result = []
    for batch in response:
        for row in batch.results:
            result.append((
                row.ad_group.resource_name,
                row.ad_group.name,
                row.ad_group.cpc_bid_micros,
                row.campaign.name,
            ))
    return result

def atualizar_cpc(ad_groups, dry_run=False):
    svc = client.get_service('AdGroupService')
    ops = []
    for rn, nome, cpc_atual, camp_nome in ad_groups:
        cpc_real = cpc_atual / 1_000_000
        label = '[DRY] ' if dry_run else ''
        print(f'{label}Campanha: {camp_nome}')
        print(f'  Ad group: {nome}')
        print(f'  CPC atual: R$ {cpc_real:.2f} → R$ {NOVO_CPC/1_000_000:.2f}')

        if not dry_run:
            op = client.get_type('AdGroupOperation')
            ag = op.update
            ag.resource_name = rn
            ag.cpc_bid_micros = NOVO_CPC
            from google.protobuf import field_mask_pb2
            op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=['cpc_bid_micros']))
            ops.append(op)

    if dry_run:
        print(f'\n[DRY-RUN] {len(ad_groups)} ad groups seriam atualizados para R$ {NOVO_CPC/1_000_000:.2f}')
        return

    ok = 0
    skipped = 0
    for op in ops:
        try:
            svc.mutate_ad_groups(customer_id=CUSTOMER_ID, operations=[op])
            ok += 1
        except GoogleAdsException as ex:
            for err in ex.failure.errors:
                if 'OPERATION_NOT_PERMITTED_FOR_CONTEXT' in str(err.error_code):
                    print(f'  ⚠ Ignorado (Smart/Display Campaign — não suporta CPC manual)')
                    skipped += 1
                else:
                    print(f'  [ERRO] {err.message}')
    print(f'\n[OK] {ok} atualizados | {skipped} ignorados (Smart/Display) | total: {len(ops)}')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry', action='store_true')
    args = parser.parse_args()

    print(f'=== Ajuste de CPC — novo valor: R$ {NOVO_CPC/1_000_000:.2f} ===\n')
    ad_groups = listar_ad_groups()
    print(f'{len(ad_groups)} ad groups encontrados\n')
    atualizar_cpc(ad_groups, dry_run=args.dry)
