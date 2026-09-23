"""
Script para inativar produtos com status 'disapproved' no Google Merchant Center.
Adiciona excludedDestinations para remover o produto dos destinos de exibicao.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import google.auth
import google.auth.transport.requests
from google.oauth2 import service_account
import requests

MERCHANT_ID = "5071388981"
SA_PATH = Path("C:/Users/fabio/Downloads/shinsei_pricing/shinsei_pricing/data/google_service_account.json")
SCOPES = ["https://www.googleapis.com/auth/content"]
BASE_URL = f"https://shoppingcontent.googleapis.com/content/v2.1/{MERCHANT_ID}"
EXCLUDED_DESTINATIONS = ["Shopping", "SurfacesAcrossGoogle", "DisplayAds"]


def get_credentials() -> service_account.Credentials:
    creds = service_account.Credentials.from_service_account_file(
        str(SA_PATH), scopes=SCOPES
    )
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)
    return creds


def get_auth_headers(creds: service_account.Credentials) -> dict:
    if not creds.valid:
        auth_req = google.auth.transport.requests.Request()
        creds.refresh(auth_req)
    return {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}


def fetch_all_disapproved(creds: service_account.Credentials) -> list[dict]:
    """Busca todos os produtos disapproved via productstatuses.list com paginacao."""
    disapproved = []
    page_token = None
    page = 0

    while True:
        page += 1
        params = {
            "maxResults": 250,
            "destinations": "Shopping",
        }
        if page_token:
            params["pageToken"] = page_token

        headers = get_auth_headers(creds)
        resp = requests.get(f"{BASE_URL}/productstatuses", headers=headers, params=params)

        if resp.status_code != 200:
            print(f"[ERRO] GET productstatuses pagina {page}: {resp.status_code} {resp.text[:500]}")
            break

        data = resp.json()
        resources = data.get("resources", [])
        print(f"  Pagina {page}: {len(resources)} produtos recebidos")

        for item in resources:
            product_id = item.get("productId", "")
            title = item.get("title", "")
            destination_statuses = item.get("destinationStatuses", [])
            item_issues = item.get("itemLevelIssues", [])

            is_disapproved = False
            reasons = []
            for ds in destination_statuses:
                dest = ds.get("destination", "")
                status = ds.get("status", "")
                if status == "disapproved" and dest in ("Shopping", "SurfacesAcrossGoogle"):
                    is_disapproved = True
                    reasons.append(f"{dest}: disapproved")

            if is_disapproved:
                # Coleta motivos dos item level issues
                issue_descriptions = [
                    f"{iss.get('description', '')} ({iss.get('destination', '')})"
                    for iss in item_issues
                    if iss.get("servability") in ("disapproved", "unaffected")
                ]
                disapproved.append({
                    "productId": product_id,
                    "title": title,
                    "reasons": reasons,
                    "issues": issue_descriptions[:3],  # max 3 issues por produto
                })

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return disapproved


def exclude_product(creds: service_account.Credentials, product_id: str) -> bool:
    """
    Exclui produto dos destinos via PATCH products/{id}.
    O product_id do GMC tem formato: online~pt~BR~<offerId>
    A API aceita o ID codificado.
    """
    # Encode forward-slashes e outros caracteres especiais para a URL
    import urllib.parse
    encoded_id = urllib.parse.quote(product_id, safe="")
    url = f"{BASE_URL}/products/{encoded_id}"

    body = {"excludedDestinations": EXCLUDED_DESTINATIONS}
    headers = get_auth_headers(creds)

    resp = requests.patch(url, headers=headers, json=body)

    if resp.status_code in (200, 204):
        return True
    else:
        print(f"    [AVISO] PATCH falhou ({resp.status_code}): {resp.text[:300]}")
        return False


def main():
    print("=== GMC: Inativar produtos disapproved ===\n")

    print("[1] Autenticando com Service Account...")
    creds = get_credentials()
    print(f"    Token obtido para: {creds.service_account_email}\n")

    print("[2] Buscando produtos com status 'disapproved'...")
    disapproved_products = fetch_all_disapproved(creds)
    print(f"\n    Total de produtos disapproved encontrados: {len(disapproved_products)}\n")

    if not disapproved_products:
        print("Nenhum produto disapproved encontrado. Nada a fazer.")
        return

    print("[3] Aplicando excludedDestinations nos produtos disapproved...\n")
    inativados = []
    falhas = []

    for i, prod in enumerate(disapproved_products, 1):
        product_id = prod["productId"]
        title = prod["title"]
        print(f"  [{i}/{len(disapproved_products)}] {product_id[:60]}...")

        success = exclude_product(creds, product_id)
        if success:
            inativados.append(prod)
            print(f"    OK - inativado")
        else:
            falhas.append(prod)

        # Respeitar rate limit da API
        if i % 10 == 0:
            time.sleep(1)

    print("\n" + "=" * 60)
    print(f"RESULTADO FINAL")
    print(f"  Produtos inativados: {len(inativados)}")
    print(f"  Falhas:              {len(falhas)}")
    print("=" * 60)

    print("\n--- PRODUTOS INATIVADOS ---")
    for p in inativados:
        print(f"\n  ID:     {p['productId']}")
        print(f"  Titulo: {p['title'][:80]}")
        print(f"  Status: {'; '.join(p['reasons'])}")
        if p["issues"]:
            print(f"  Issues: {' | '.join(p['issues'][:2])}")

    if falhas:
        print("\n--- FALHAS ---")
        for p in falhas:
            print(f"  {p['productId']} - {p['title'][:60]}")

    # Salva resultado em JSON
    output = {
        "total_disapproved": len(disapproved_products),
        "total_inativados": len(inativados),
        "total_falhas": len(falhas),
        "inativados": inativados,
        "falhas": falhas,
    }
    output_path = Path("C:/Users/fabio/Downloads/shinsei_pricing/shinsei_pricing/data/gmc_inativados.json")
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nResultado salvo em: {output_path}")


if __name__ == "__main__":
    main()
