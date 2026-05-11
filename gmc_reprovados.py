# -*- coding: utf-8 -*-
"""
Varredura GMC: lista todos os produtos reprovados/limitados com motivos.
"""
import sys
import json
import time
from pathlib import Path
from collections import defaultdict

from google.oauth2 import service_account
from googleapiclient.discovery import build

BASE_DIR    = Path(__file__).parent
DATA_DIR    = BASE_DIR / "data"
SA_FILE     = DATA_DIR / "google_service_account.json"
MERCHANT_ID = 5071388981
SCOPES      = ["https://www.googleapis.com/auth/content"]

def pr(s):
    sys.stdout.buffer.write((s + "\n").encode("utf-8", "replace"))
    sys.stdout.flush()

def build_service():
    creds = service_account.Credentials.from_service_account_file(str(SA_FILE), scopes=SCOPES)
    return build("content", "v2.1", credentials=creds)

def listar_status_produtos(service):
    """Busca status de todos os produtos via productstatuses."""
    produtos = []
    page_token = None

    while True:
        kwargs = {"merchantId": MERCHANT_ID, "maxResults": 250}
        if page_token:
            kwargs["pageToken"] = page_token

        resp = service.productstatuses().list(**kwargs).execute()
        items = resp.get("resources", [])
        produtos.extend(items)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break
        time.sleep(0.2)

    return produtos

ISSUES_TEMPORARIAS = {"availability_updated", "price_updated"}

# Severidade que bloqueia veiculação
SEVERITY_BLOQUEANTE = {"disapproved", "unaffected"}

def classificar(produtos):
    reprovados   = []
    limitados    = []
    pendentes    = []
    aprovados    = 0

    motivos_count = defaultdict(int)
    destinos_count = defaultdict(lambda: defaultdict(int))

    for p in produtos:
        pid    = p.get("productId", "")
        titulo = p.get("title", "?")[:60]
        destinos = p.get("destinationStatuses", [])

        # Foca no destino Shopping (principal)
        # IMPORTANTE: status="disapproved" pode coexistir com approvedCountries=["BR"],
        # o que significa que o produto ESTÁ servindo no Brasil mas foi reprovado em
        # outro país. Para uma loja BR, o que importa é se "BR" está em approvedCountries.
        shopping_status = "approved"
        for dest in destinos:
            nome_dest = dest.get("destination", "")
            status    = dest.get("status", "")
            destinos_count[nome_dest][status] += 1
            if nome_dest == "Shopping":
                approved_countries    = dest.get("approvedCountries", [])
                disapproved_countries = dest.get("disapprovedCountries", [])
                if "BR" in approved_countries:
                    # Produto aprovado no Brasil — está servindo normalmente
                    shopping_status = "approved"
                elif "BR" in disapproved_countries:
                    # Produto explicitamente reprovado no Brasil
                    shopping_status = "disapproved"
                elif status == "disapproved" and not approved_countries:
                    # Reprovado globalmente (sem aprovação em nenhum país)
                    shopping_status = "disapproved"
                else:
                    # Fallback para o status geral (pending, etc.)
                    shopping_status = status

        # Issues reais (ignora temporárias)
        item_issues = p.get("itemLevelIssues", [])
        issues_produto = []
        for issue in item_issues:
            code     = issue.get("code", "?")
            if code in ISSUES_TEMPORARIAS:
                continue
            severity    = issue.get("servability", "?")
            desc        = issue.get("description", "")
            resolution  = issue.get("resolution", "")
            attribute   = issue.get("attribute", "")
            motivos_count[code] += 1
            issues_produto.append({
                "code": code,
                "severity": severity,
                "desc": desc,
                "resolution": resolution,
                "attribute": attribute,
            })

        entrada = {
            "id": pid,
            "titulo": titulo,
            "issues": issues_produto,
            "destinos": destinos,
            "shopping_status": shopping_status,
        }

        if shopping_status == "disapproved":
            reprovados.append(entrada)
        elif issues_produto:
            limitados.append(entrada)
        elif shopping_status in ("pending", "unreviewed"):
            pendentes.append(entrada)
        else:
            aprovados += 1

    return reprovados, limitados, pendentes, aprovados, motivos_count, destinos_count


def main():
    pr(f"\n{'='*70}")
    pr("VARREDURA GMC — PRODUTOS REPROVADOS E LIMITADOS")
    pr(f"{'='*70}")

    service = build_service()

    pr("Buscando status de todos os produtos...")
    produtos = listar_status_produtos(service)
    pr(f"Total de produtos encontrados: {len(produtos)}")

    reprovados, limitados, pendentes, aprovados, motivos_count, destinos_count = classificar(produtos)

    # ── Resumo ────────────────────────────────────────────────
    pr(f"\n{'─'*70}")
    pr("RESUMO")
    pr(f"{'─'*70}")
    pr(f"  ✅ Aprovados:   {aprovados}")
    pr(f"  ❌ Reprovados:  {len(reprovados)}")
    pr(f"  ⚠️  Limitados:   {len(limitados)}")
    pr(f"  ⏳ Pendentes:   {len(pendentes)}")

    # ── Top motivos ───────────────────────────────────────────
    pr(f"\n{'─'*70}")
    pr("TOP MOTIVOS DE REPROVAÇÃO / LIMITAÇÃO")
    pr(f"{'─'*70}")
    sorted_motivos = sorted(motivos_count.items(), key=lambda x: -x[1])
    for code, count in sorted_motivos[:20]:
        pr(f"  {count:>5}x  {code}")

    # ── Destinos ──────────────────────────────────────────────
    pr(f"\n{'─'*70}")
    pr("STATUS POR DESTINO")
    pr(f"{'─'*70}")
    for dest, statuses in destinos_count.items():
        linha = f"  {dest:<35}"
        for st, n in sorted(statuses.items()):
            linha += f"  {st}:{n}"
        pr(linha)

    # ── Detalhes reprovados ───────────────────────────────────
    pr(f"\n{'─'*70}")
    pr(f"DETALHES — REPROVADOS ({len(reprovados)}) [primeiros 30]")
    pr(f"{'─'*70}")
    for p in reprovados[:30]:
        pr(f"\n  Produto: {p['titulo']}")
        pr(f"  ID: {p['id']}")
        for iss in p["issues"]:
            pr(f"    [{iss['severity']}] {iss['code']} — {iss['desc']}")
            if iss["resolution"]:
                pr(f"           → Resolução: {iss['resolution']}")
            if iss["attribute"]:
                pr(f"           → Atributo:  {iss['attribute']}")

    # ── Detalhes limitados ────────────────────────────────────
    pr(f"\n{'─'*70}")
    pr(f"DETALHES — LIMITADOS ({len(limitados)}) [primeiros 30]")
    pr(f"{'─'*70}")
    for p in limitados[:30]:
        pr(f"\n  Produto: {p['titulo']}")
        pr(f"  ID: {p['id']}")
        for iss in p["issues"]:
            pr(f"    [{iss['severity']}] {iss['code']} — {iss['desc']}")
            if iss["resolution"]:
                pr(f"           → Resolução: {iss['resolution']}")

    # ── Salva JSON ────────────────────────────────────────────
    out = DATA_DIR / "gmc_reprovados.json"
    resultado = {
        "total": len(produtos),
        "aprovados": aprovados,
        "reprovados": len(reprovados),
        "limitados": len(limitados),
        "pendentes": len(pendentes),
        "top_motivos": sorted_motivos[:30],
        "reprovados_detalhe": reprovados,
        "limitados_detalhe": limitados,
    }
    out.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    pr(f"\n  Resultado completo salvo em: {out}")
    pr(f"{'='*70}")

if __name__ == "__main__":
    main()
