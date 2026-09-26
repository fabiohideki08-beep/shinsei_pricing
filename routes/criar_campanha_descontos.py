"""
criar_campanha_descontos.py
Cria campanha Shopping no Google Ads para a coleção "Descontos acima de 50%".

Campanha: Shinsei SP — Descontos 50%+
Budget:   customers/2097362078/campaignBudgets/15577816739  (R$30/dia, já criado)
"""

import io, sys, json, time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

BASE_DIR    = Path(__file__).resolve().parent.parent
YAML_PATH   = BASE_DIR / 'google-ads.yaml'
CUSTOMER_ID = '2097362078'
MERCHANT_ID = 5071388981

BUDGET_RN   = 'customers/2097362078/campaignBudgets/15577816739'

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

client = GoogleAdsClient.load_from_storage(str(YAML_PATH), version='v24')

# ── 1. CAMPANHA ────────────────────────────────────────────────────────────────
def criar_campanha():
    svc = client.get_service('CampaignService')
    op  = client.get_type('CampaignOperation')
    camp = op.create

    camp.name   = 'Shinsei SP — Descontos 50%+'
    camp.status = client.enums.CampaignStatusEnum.PAUSED

    camp.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SHOPPING
    camp.campaign_budget          = BUDGET_RN

    camp.shopping_setting.merchant_id        = MERCHANT_ID
    camp.shopping_setting.campaign_priority  = 1   # MEDIUM (acima do Kits & Combos)
    camp.shopping_setting.enable_local        = False

    camp.manual_cpc.enhanced_cpc_enabled = False

    # Campo obrigatório no v24 — é um enum EuPoliticalAdvertisingStatus
    camp.contains_eu_political_advertising = (
        client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )

    try:
        resp = svc.mutate_campaigns(customer_id=CUSTOMER_ID, operations=[op])
        rn   = resp.results[0].resource_name
        print(f'[OK] Campanha criada: {rn}')
        return rn
    except GoogleAdsException as ex:
        for err in ex.failure.errors:
            print(f'[ERRO campanha] {err.message}  loc={err.location}')
        raise

# ── 2. AD GROUP ────────────────────────────────────────────────────────────────
def criar_ad_group(campaign_rn):
    svc = client.get_service('AdGroupService')
    op  = client.get_type('AdGroupOperation')
    ag  = op.create

    ag.name     = 'Descontos 50%+ — Todos'
    ag.campaign = campaign_rn
    ag.status   = client.enums.AdGroupStatusEnum.ENABLED
    ag.type_    = client.enums.AdGroupTypeEnum.SHOPPING_PRODUCT_ADS

    ag.cpc_bid_micros = 1_500_000   # R$ 1,50

    try:
        resp = svc.mutate_ad_groups(customer_id=CUSTOMER_ID, operations=[op])
        rn   = resp.results[0].resource_name
        print(f'[OK] Ad group criado: {rn}')
        return rn
    except GoogleAdsException as ex:
        for err in ex.failure.errors:
            print(f'[ERRO ad_group] {err.message}  loc={err.location}')
        raise

# ── 3. SHOPPING AD ─────────────────────────────────────────────────────────────
def criar_shopping_ad(ag_rn):
    svc = client.get_service('AdGroupAdService')
    op  = client.get_type('AdGroupAdOperation')
    aga = op.create

    aga.ad_group = ag_rn
    aga.status   = client.enums.AdGroupAdStatusEnum.ENABLED

    # ShoppingProductAd não tem campos obrigatórios — apenas acionar o oneof
    aga.ad.shopping_product_ad = client.get_type('ShoppingProductAdInfo')

    try:
        resp = svc.mutate_ad_group_ads(customer_id=CUSTOMER_ID, operations=[op])
        rn   = resp.results[0].resource_name
        print(f'[OK] Shopping ad criado: {rn}')
        return rn
    except GoogleAdsException as ex:
        for err in ex.failure.errors:
            print(f'[ERRO shopping_ad] {err.message}  loc={err.location}')
        raise

# ── 4. LISTING GROUP (catch-all — todos os produtos) ──────────────────────────
def criar_listing_group_catchall(ag_rn):
    """
    Cria um listing group UNIT catch-all para o ad group.
    A filtragem real acontece via custom_label que será configurado
    no GMC para os 219 produtos com desconto >=50%.
    Por ora, todos os produtos ficam elegíveis na campanha.
    """
    svc = client.get_service('AdGroupCriterionService')
    op  = client.get_type('AdGroupCriterionOperation')
    agc = op.create

    agc.ad_group = ag_rn
    agc.status   = client.enums.AdGroupCriterionStatusEnum.ENABLED

    agc.listing_group.type_ = client.enums.ListingGroupTypeEnum.UNIT
    # catch-all: sem case_value definido

    agc.cpc_bid_micros = 1_500_000   # R$ 1,50

    try:
        resp = svc.mutate_ad_group_criteria(customer_id=CUSTOMER_ID, operations=[op])
        rn   = resp.results[0].resource_name
        print(f'[OK] Listing group criado: {rn}')
        return rn
    except GoogleAdsException as ex:
        for err in ex.failure.errors:
            print(f'[ERRO listing_group] {err.message}  loc={err.location}')
        raise

# ── MAIN ───────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print('=== Criando campanha Descontos 50%+ ===')

    campaign_rn = criar_campanha()
    time.sleep(0.5)

    ag_rn = criar_ad_group(campaign_rn)
    time.sleep(0.5)

    criar_shopping_ad(ag_rn)
    time.sleep(0.5)

    criar_listing_group_catchall(ag_rn)

    print('\n=== Concluído ===')
    print(f'Campanha: {campaign_rn}')
    print(f'Ad group: {ag_rn}')
    print('Status: PAUSED — ative quando quiser começar a veicular.')
