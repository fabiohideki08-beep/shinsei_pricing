"""
shinsei_seo.py — SEO completo para Shinsei Market
Uso: python shinsei_seo.py [colecoes|blog|health|tudo]
"""
import json, sys, time, requests
from pathlib import Path

TOKEN = json.loads((Path("data/shopify_config.json")).read_text(encoding="utf-8"))["access_token"]
STORE = "pknw4n-eg.myshopify.com"
API   = f"https://{STORE}/admin/api/2024-01"
HDR   = {"X-Shopify-Access-Token": TOKEN, "Content-Type": "application/json"}

import re as _re

GQL = f"https://{STORE}/admin/api/2024-01/graphql.json"

def get(path, params=None):
    r = requests.get(f"{API}{path}", headers=HDR, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def gql(query: str, variables: dict = None) -> dict:
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    r = requests.post(GQL, headers=HDR, json=payload, timeout=30)
    r.raise_for_status()
    return r.json().get("data", {})

def get_all(path, key, params=None):
    """Busca todas as páginas via cursor Link header (API 2024-01 — sem ?page=)."""
    first_params = {"limit": 250, **(params or {})}
    url = f"{API}{path}"
    items = []
    current_params = first_params
    while url:
        r = requests.get(url, headers=HDR, params=current_params, timeout=20)
        r.raise_for_status()
        items.extend(r.json().get(key, []))
        current_params = None  # cursor já está na URL do link next
        nxt = _re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("Link", ""))
        url = nxt.group(1) if nxt else None
    return items

def put(path, data):
    r = requests.put(f"{API}{path}", headers=HDR, json=data, timeout=20)
    r.raise_for_status()
    return r.json()

def post(path, data):
    r = requests.post(f"{API}{path}", headers=HDR, json=data, timeout=20)
    r.raise_for_status()
    return r.json()

def sep(titulo):
    print(f"\n{'='*60}")
    print(f"  {titulo}")
    print('='*60)

# ── 1. SEO COLEÇÕES ──────────────────────────────────────────

COLECOES_SEO = {
    "unilever": {
        "title": "Produtos Unilever para cabelos e beleza | Shinsei Market",
        "desc": "Encontre os melhores produtos Unilever para cabelos, pele e beleza. Dove, TRESemmé, Seda e muito mais com entrega rápida em todo o Brasil na Shinsei Market.",
        "body": "Explore a linha completa de produtos Unilever disponíveis na Shinsei Market. De shampoos e condicionadores a cremes e desodorantes, as marcas Unilever combinam ciência e cuidado para o seu dia a dia.",
    },
    "phytoervas": {
        "title": "Phytoervas — Produtos naturais para cabelos | Shinsei Market",
        "desc": "Conheça a linha Phytoervas de shampoos, condicionadores e tratamentos com extratos naturais. Fórmulas suaves para todos os tipos de cabelo disponíveis na Shinsei Market.",
        "body": "A Phytoervas une o poder das ervas e ingredientes naturais ao cuidado capilar. Encontre na Shinsei Market shampoos, condicionadores e máscaras Phytoervas ideais para cabelos que precisam de leveza e nutrição.",
    },
    "giovanna-baby": {
        "title": "Giovanna Baby — Cuidados para bebês e crianças | Shinsei Market",
        "desc": "Produtos Giovanna Baby com fórmulas suaves, dermatologicamente testadas e sem lágrimas. Shampoos, sabonetes e hidratantes para bebês disponíveis na Shinsei Market.",
        "body": "A Giovanna Baby oferece uma linha completa de higiene e cuidados desenvolvida especialmente para a pele delicada dos bebês e crianças. Fórmulas hipoalergênicas, com fragrância suave e aprovadas por dermatologistas — encontre tudo na Shinsei Market.",
    },
    "triskle": {
        "title": "Triskle — Tratamentos capilares profissionais | Shinsei Market",
        "desc": "Descubra os tratamentos capilares Triskle para cabelos danificados, ressecados ou com química. Reconstrução, hidratação e nutrição profissional disponíveis na Shinsei Market.",
        "body": "A Triskle é especializada em tratamentos capilares de resultado profissional para uso em casa. Encontre na Shinsei Market máscaras, ampolas e cremes Triskle formulados para restaurar a saúde e o brilho dos cabelos.",
    },
    "dabelle": {
        "title": "Dabelle — Linha capilar para cabelos cacheados e crespos | Shinsei Market",
        "desc": "Produtos Dabelle desenvolvidos para cabelos cacheados, crespos e com química. Hidratação intensa, definição e nutrição disponíveis na Shinsei Market.",
        "body": "A Dabelle foi criada para celebrar e cuidar da diversidade dos cabelos brasileiros. Com fórmulas específicas para cachos, crespos e ondulados, a linha Dabelle oferece hidratação, definição e maciez reais. Encontre na Shinsei Market.",
    },
    "risque": {
        "title": "Risqué — Esmaltes e produtos para unhas | Shinsei Market",
        "desc": "Explore a linha Risqué com centenas de cores de esmaltes, bases, top coats e removedores. A marca número 1 em esmaltes no Brasil disponível na Shinsei Market.",
        "body": "Risqué é sinônimo de cor e qualidade nas unhas brasileiras há décadas. Na Shinsei Market você encontra toda a linha Risqué — esmaltes, bases, top coats e tratamentos — para unhas sempre impecáveis.",
    },
    "colorama": {
        "title": "Colorama — Esmaltes coloridos e tratamentos para unhas | Shinsei Market",
        "desc": "Encontre os esmaltes Colorama em centenas de tons vibrantes e coleções exclusivas. Fórmulas de longa duração para unhas com cor e brilho disponíveis na Shinsei Market.",
        "body": "Colorama traz cor, tendência e qualidade para as unhas com uma paleta incrível de esmaltes e produtos para nail care. Confira na Shinsei Market as últimas coleções e tons clássicos Colorama para o seu estilo.",
    },
}

def atualizar_colecoes():
    sep("1/3 — SEO DAS COLEÇÕES")
    custom  = [{**i, "_tipo": "custom_collections"} for i in get_all("/custom_collections.json", "custom_collections")]
    smart   = [{**i, "_tipo": "smart_collections"}  for i in get_all("/smart_collections.json",  "smart_collections")]
    colecoes = custom + smart

    print(f"  Total de coleções encontradas: {len(colecoes)}")
    ok = err = 0
    for col in colecoes:
        handle = col.get("handle", "")
        seo = COLECOES_SEO.get(handle)
        if not seo:
            continue
        col_id = col["id"]
        tipo_url = col["_tipo"]
        chave = "custom_collection" if tipo_url == "custom_collections" else "smart_collection"
        payload = {chave: {
            "id": col_id,
            "body_html": seo["body"],
            "metafields_global_title_tag": seo["title"],
            "metafields_global_description_tag": seo["desc"],
        }}
        try:
            put(f"/{tipo_url}/{col_id}.json", payload)
            print(f"  ✅ {handle}")
            ok += 1
        except Exception as e:
            print(f"  ❌ {handle}: {e}")
            err += 1
        time.sleep(0.5)

    if ok == 0 and err == 0:
        print("  ℹ Nenhuma coleção do mapa encontrada na loja (handles não coincidem?)")
    print(f"\n  Resultado: {ok} OK | {err} erro(s)")


# ── 2. POSTS DE BLOG ──────────────────────────────────────────

POSTS = [
    {
        "title": "Guia completo de cuidados com cabelos asiáticos",
        "handle": "guia-cuidados-cabelos-asiaticos",
        "tags": "cabelos asiáticos, cuidados capilares, produtos japoneses, K-Beauty",
        "seo_title": "Guia completo de cuidados com cabelos asiáticos | Shinsei Market",
        "seo_desc": "Descubra os melhores produtos e rotinas para cabelos asiáticos. Dicas de hidratação, reconstrução e finalização com marcas japonesas e coreanas disponíveis na Shinsei Market.",
        "body_html": """<p>Os cabelos asiáticos possuem características únicas — são geralmente lisos, densos e com cutícula mais espessa — que exigem cuidados específicos para manter o brilho e a saúde capilar.</p>
<h2>Por que cabelos asiáticos precisam de cuidados especiais?</h2>
<p>A estrutura do cabelo asiático é cilíndrica e possui maior densidade de fios por cm², tornando o couro cabeludo mais oleoso e as pontas mais ressecadas. Alisamento e coloração frequentes aumentam a necessidade de reconstrução e hidratação.</p>
<h2>Rotina recomendada</h2>
<ol>
  <li><strong>Shampoo sem sulfato</strong> — preserva a oleosidade natural</li>
  <li><strong>Condicionador hidratante</strong> — da metade para as pontas</li>
  <li><strong>Máscara de reconstrução</strong> — 1x por semana para cabelos com química</li>
  <li><strong>Leave-in ou sérum</strong> — controle do frizz e brilho</li>
  <li><strong>Protetor térmico</strong> — obrigatório antes de chapinha ou babyliss</li>
</ol>
<p><a href="https://www.shinseimarket.com.br/collections/cabelos">Ver todos os produtos para cabelos na Shinsei Market →</a></p>""",
    },
    {
        "title": "K-Beauty: como montar sua rotina de skincare coreana",
        "handle": "k-beauty-rotina-skincare-coreana",
        "tags": "K-Beauty, skincare coreana, rotina 10 passos, cosméticos coreanos",
        "seo_title": "K-Beauty: como montar sua rotina de skincare coreana | Shinsei Market",
        "seo_desc": "Aprenda a montar a famosa rotina de skincare coreana de 10 passos. Conheça os produtos essenciais de K-Beauty disponíveis na Shinsei Market com entrega rápida no Brasil.",
        "body_html": """<p>O K-Beauty revolucionou os cuidados com a pele ao propor uma abordagem completa e preventiva. A rotina coreana de 10 passos pode ser adaptada ao seu tipo de pele.</p>
<h2>Os 10 passos</h2>
<ol>
  <li><strong>Óleo de limpeza</strong> — remove maquiagem e protetor solar</li>
  <li><strong>Limpador aquoso</strong> — limpa os poros em profundidade</li>
  <li><strong>Esfoliante</strong> — 2-3x por semana</li>
  <li><strong>Tônico</strong> — equilibra o pH e prepara a pele</li>
  <li><strong>Essence</strong> — hidratação leve e tratamento inicial</li>
  <li><strong>Sérum ou Ampoule</strong> — concentrado de ativos</li>
  <li><strong>Sheet mask</strong> — hidratação intensa, 2-3x por semana</li>
  <li><strong>Contorno dos olhos</strong> — trata olheiras e linhas de expressão</li>
  <li><strong>Hidratante</strong> — sela a hidratação</li>
  <li><strong>Protetor solar</strong> — o passo mais importante de manhã</li>
</ol>
<p><a href="https://www.shinseimarket.com.br">Explorar K-Beauty na Shinsei Market →</a></p>""",
    },
    {
        "title": "Cosméticos japoneses: os melhores produtos que valem a pena",
        "handle": "cosmeticos-japoneses-vale-a-pena",
        "tags": "cosméticos japoneses, beleza japonesa, J-Beauty, protetor solar japonês",
        "seo_title": "Cosméticos japoneses: os melhores produtos que valem a pena comprar | Shinsei Market",
        "seo_desc": "Confira os cosméticos japoneses mais amados — de protetor solar a shampoo — e descubra quais realmente entregam resultados. Todos disponíveis na Shinsei Market.",
        "body_html": """<p>O Japão é referência mundial em inovação cosmética. Mas com tantas opções, quais realmente valem o investimento?</p>
<h2>Categorias que o Japão domina</h2>
<h3>☀️ Protetor Solar</h3>
<p>Leves, de toque seco, com acabamento matte ou luminoso. Desenvolvidos para uso diário sem engordurar a pele.</p>
<h3>🧴 Shampoos e condicionadores</h3>
<p>Ingredientes como óleo de camélia e queratina de seda fortalecem e dão brilho aos fios — perfeitos para cabelos lisos.</p>
<h3>🌸 Skincare facial</h3>
<p>Loções, essences e cremes com niacinamida, ácido hialurônico e extratos fermentados garantem pele iluminada e uniforme.</p>
<p><a href="https://www.shinseimarket.com.br">Ver produtos japoneses na Shinsei Market →</a></p>""",
    },
    {
        "title": "Protetor solar asiático vs. brasileiro: qual é melhor?",
        "handle": "protetor-solar-asiatico-vs-brasileiro",
        "tags": "protetor solar, protetor solar japonês, protetor solar coreano, FPS, UVA UVB",
        "seo_title": "Protetor solar asiático vs. brasileiro: qual é melhor? | Shinsei Market",
        "seo_desc": "Comparamos protetores solares asiáticos e brasileiros em textura, acabamento e proteção UVA/UVB. Veja qual escolher para cada tipo de pele e compre na Shinsei Market.",
        "body_html": """<p>Quando o assunto é escolher entre um protetor solar asiático e um brasileiro, surgem muitas dúvidas. Fizemos uma comparação honesta.</p>
<h2>Textura e acabamento</h2>
<p><strong>Asiáticos:</strong> Gel aquoso ou fluido, toque seco, sem sensação gordurosa. Ideais para pele oleosa ou mista.</p>
<p><strong>Brasileiros:</strong> Mais cremosos — vantajoso para peles secas, porém pesado em dias de calor.</p>
<h2>Filtros utilizados</h2>
<p><strong>Asiáticos:</strong> Filtros modernos como Tinosorb S e Tinosorb M — ampla proteção UVA/UVB com fórmulas estáveis.</p>
<p><strong>Brasileiros:</strong> Alguns com filtros modernos, muitos ainda com filtros mais antigos.</p>
<h2>Qual escolher?</h2>
<ul>
  <li><strong>Pele oleosa ou mista:</strong> Protetor japonês ou coreano — textura leve</li>
  <li><strong>Pele seca:</strong> Protetor coreano hidratante ou brasileiro cremoso</li>
  <li><strong>Uso diário sob maquiagem:</strong> Protetor japonês em gel ou fluido</li>
  <li><strong>Praia ou atividade física:</strong> Brasileiro water-resistant de alta resistência</li>
</ul>
<p><a href="https://www.shinseimarket.com.br">Ver protetores solares na Shinsei Market →</a></p>""",
    },
]

def criar_posts():
    sep("2/3 — POSTS DE BLOG")
    blogs_data = get("/blogs.json")
    blogs = blogs_data.get("blogs", []) if isinstance(blogs_data, dict) else []
    blog_id = None
    for b in blogs:
        h = b.get("handle", "").lower()
        if any(x in h for x in ("dica", "noticia", "news", "blog")):
            blog_id = b["id"]
            print(f"  Blog: {b['title']} (id={blog_id})")
            break
    if not blog_id and blogs:
        blog_id = blogs[0]["id"]
        print(f"  Usando: {blogs[0]['title']} (id={blog_id})")
    if not blog_id:
        novo = post("/blogs.json", {"blog": {"title": "Dicas e Novidades", "commentable": "no"}})
        blog_id = novo["blog"]["id"]
        print(f"  Blog criado: Dicas e Novidades (id={blog_id})")

    handles_existentes = {a.get("handle", "") for a in get_all(f"/blogs/{blog_id}/articles.json", "articles")}

    ok = err = skip = 0
    for p_data in POSTS:
        if p_data["handle"] in handles_existentes:
            print(f"  ⏭ já existe: {p_data['handle']}")
            skip += 1
            continue
        try:
            payload = {"article": {
                "title": p_data["title"],
                "body_html": p_data["body_html"],
                "tags": p_data["tags"],
                "handle": p_data["handle"],
                "published": True,
                "metafields": [
                    {"namespace": "global", "key": "title_tag",
                     "value": p_data["seo_title"], "type": "single_line_text_field"},
                    {"namespace": "global", "key": "description_tag",
                     "value": p_data["seo_desc"], "type": "single_line_text_field"},
                ],
            }}
            post(f"/blogs/{blog_id}/articles.json", payload)
            print(f"  ✅ {p_data['handle']}")
            ok += 1
        except Exception as e:
            print(f"  ❌ {p_data['handle']}: {e}")
            err += 1
        time.sleep(0.8)

    print(f"\n  Resultado: {ok} criado(s) | {skip} já existia(m) | {err} erro(s)")


# ── 3. HEALTH SCORE ───────────────────────────────────────────

def health_score():
    sep("SEO HEALTH SCORE — CHECKLIST COMPLETO")
    import urllib.request as _ur

    # ── 1. Coleções (GraphQL) ─────────────────────────────────
    _q_col = """
    query($cursor: String) {
      collections(first: 250, after: $cursor) {
        edges { node { id title handle description seo { title description } } }
        pageInfo { hasNextPage endCursor }
      }
    }"""
    col_nodes = []
    _cursor = None
    while True:
        _d = gql(_q_col, {"cursor": _cursor})
        col_nodes.extend(e["node"] for e in (_d.get("collections") or {}).get("edges", []))
        _pi = (_d.get("collections") or {}).get("pageInfo", {})
        if not _pi.get("hasNextPage"): break
        _cursor = _pi.get("endCursor")

    total_col = len(col_nodes)
    col_com_title = sum(1 for c in col_nodes if ((c.get("seo") or {}).get("title") or "").strip())
    col_com_desc  = sum(1 for c in col_nodes if ((c.get("seo") or {}).get("description") or "").strip())
    col_com_body  = sum(1 for c in col_nodes if len((c.get("description") or "").strip()) >= 50)

    # ── 2. Produtos (GraphQL — amostra 100) ───────────────────
    _q_prod = """
    {
      products(first: 100) {
        edges {
          node {
            id title descriptionHtml productType vendor
            seo { title description }
            images(first: 10) { edges { node { altText } } }
          }
        }
      }
    }"""
    _dp = gql(_q_prod)
    prod_nodes = [e["node"] for e in (_dp.get("products") or {}).get("edges", [])]
    total_prod = len(prod_nodes)

    prod_com_title = sum(1 for p in prod_nodes if ((p.get("seo") or {}).get("title") or "").strip())
    prod_com_desc  = sum(1 for p in prod_nodes if ((p.get("seo") or {}).get("description") or "").strip())
    prod_com_body  = sum(1 for p in prod_nodes if len((p.get("descriptionHtml") or "").strip()) >= 100)

    all_imgs = [(img_e.get("node") or {}) for p in prod_nodes
                for img_e in (p.get("images") or {}).get("edges", [])]
    total_imgs   = len(all_imgs)
    imgs_com_alt = sum(1 for img in all_imgs if (img.get("altText") or "").strip())
    avg_imgs_por_prod = total_imgs / total_prod if total_prod else 0

    # ── 3. Blog (REST — GraphQL Admin nao expoe articles de blog facilmente) ──
    total_posts = posts_com_meta = 0
    try:
        _blogs_r = get("/blogs.json")
        for _bl in (_blogs_r.get("blogs", []) if isinstance(_blogs_r, dict) else []):
            for _art in get_all(f"/blogs/{_bl['id']}/articles.json", "articles",
                                {"fields": "id,title"}):
                total_posts += 1
                try:
                    _mf = get(f"/blogs/{_bl['id']}/articles/{_art['id']}/metafields.json",
                              {"namespace": "global", "key": "title_tag"})
                    if (_mf.get("metafields") or []):
                        posts_com_meta += 1
                except Exception:
                    pass
    except Exception:
        pass

    # ── 4. Verificações técnicas ──────────────────────────────
    loja_url = "https://www.shinseimarket.com.br"
    def _check_url(url, max_bytes=131072):
        try:
            req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"})
            with _ur.urlopen(req, timeout=20) as r:
                conteudo = r.read(max_bytes).decode("utf-8", errors="ignore")
                return r.status < 400, conteudo
        except Exception:
            return False, ""

    sitemap_ok, _   = _check_url(f"{loja_url}/sitemap.xml")
    robots_ok,  _   = _check_url(f"{loja_url}/robots.txt")
    _, home_html    = _check_url(loja_url)
    og_ok           = 'property="og:' in home_html or "property='og:" in home_html
    schema_ok       = '"@type"' in home_html
    canonical_ok    = 'rel="canonical"' in home_html or "rel='canonical'" in home_html

    # ── 5. PageSpeed (usa cache se disponível) ────────────────
    from pathlib import Path as _P
    import json as _j
    _cache = _P("data/seo_health_cache.json")
    ps_cache = {}
    if _cache.exists():
        try:
            ps_cache = _j.loads(_cache.read_text(encoding="utf-8")).get("pagespeed") or {}
        except Exception:
            pass
    ps_mobile  = ps_cache.get("mobile")  or {}
    ps_desktop = ps_cache.get("desktop") or {}
    ps_mob_score  = (ps_mobile.get("performance")  if isinstance(ps_mobile,  dict) else None)
    ps_desk_score = (ps_desktop.get("performance") if isinstance(ps_desktop, dict) else None)

    # ═══════════════════════════════════════════════════════════
    #  PONTUAÇÃO — 7 categorias, total 100 pts
    # ═══════════════════════════════════════════════════════════

    pts = {}

    # A) Meta SEO — coleções (15 pts)
    pts["A_meta_col"] = min(15, round(
        (col_com_title / total_col * 8 if total_col else 0) +
        (col_com_desc  / total_col * 7 if total_col else 0)
    ))

    # B) Meta SEO — produtos (12 pts)
    pts["B_meta_prod"] = min(12, round(
        (prod_com_title / total_prod * 7 if total_prod else 0) +
        (prod_com_desc  / total_prod * 5 if total_prod else 0)
    ))

    # C) Imagens (8 pts)
    alt_pct = imgs_com_alt / total_imgs if total_imgs else 1
    avg_bonus = 3 if avg_imgs_por_prod >= 3 else (1 if avg_imgs_por_prod >= 2 else 0)
    pts["C_imagens"] = min(8, round(alt_pct * 5 + avg_bonus))

    # D) Conteúdo / Content (10 pts)
    blog_pts = 5 if total_posts >= 20 else (3 if total_posts >= 10 else (1 if total_posts >= 4 else 0))
    body_pct = prod_com_body / total_prod if total_prod else 0
    col_body_pct = col_com_body / total_col if total_col else 0
    pts["D_conteudo"] = min(10, round(blog_pts + body_pct * 3 + col_body_pct * 2))

    # E) PageSpeed Mobile (20 pts)
    if ps_mob_score is None:
        pts["E_ps_mobile"] = 0   # não analisado = 0
    elif ps_mob_score >= 90: pts["E_ps_mobile"] = 20
    elif ps_mob_score >= 80: pts["E_ps_mobile"] = 16
    elif ps_mob_score >= 70: pts["E_ps_mobile"] = 12
    elif ps_mob_score >= 50: pts["E_ps_mobile"] = 8
    elif ps_mob_score >= 30: pts["E_ps_mobile"] = 4
    else:                    pts["E_ps_mobile"] = 1

    # F) PageSpeed Desktop (10 pts)
    if ps_desk_score is None:
        pts["F_ps_desktop"] = 0
    elif ps_desk_score >= 90: pts["F_ps_desktop"] = 10
    elif ps_desk_score >= 70: pts["F_ps_desktop"] = 7
    elif ps_desk_score >= 50: pts["F_ps_desktop"] = 4
    else:                     pts["F_ps_desktop"] = 1

    # G) SEO Técnico (25 pts)
    tech = 0
    tech += 5 if sitemap_ok   else 0
    tech += 3 if robots_ok    else 0
    tech += 4 if og_ok        else 0
    tech += 5 if schema_ok    else 0
    tech += 3 if canonical_ok else 0
    tech += 3 if posts_com_meta == total_posts and total_posts > 0 else (
        round(posts_com_meta / total_posts * 3) if total_posts else 0)
    tech += 2  # HTTPS — Shopify sempre HTTPS
    pts["G_tecnico"] = min(25, tech)

    score = min(100, sum(pts.values()))

    # ── Relatório ──────────────────────────────────────────────
    cor = "VERDE" if score >= 80 else "AMARELO" if score >= 50 else "VERMELHO"
    print(f"\n  [{cor}] SCORE: {score}/100")
    print(f"  A) Meta SEO coleções:  {pts['A_meta_col']:>2}/15  ({col_com_title}/{total_col} title | {col_com_desc}/{total_col} desc)")
    print(f"  B) Meta SEO produtos:  {pts['B_meta_prod']:>2}/12  ({prod_com_title}/{total_prod} title | {prod_com_desc}/{total_prod} desc)")
    print(f"  C) Imagens:            {pts['C_imagens']:>2}/8   ({imgs_com_alt}/{total_imgs} alt | media {avg_imgs_por_prod:.1f} img/prod)")
    print(f"  D) Conteudo:           {pts['D_conteudo']:>2}/10  (blog={total_posts} posts | prod body={prod_com_body}/{total_prod} | col desc={col_com_body}/{total_col})")
    print(f"  E) PageSpeed Mobile:   {pts['E_ps_mobile']:>2}/20  (score={ps_mob_score})")
    print(f"  F) PageSpeed Desktop:  {pts['F_ps_desktop']:>2}/10  (score={ps_desk_score})")
    print(f"  G) Tecnico:            {pts['G_tecnico']:>2}/25  (sitemap={'OK' if sitemap_ok else 'FAIL'} | robots={'OK' if robots_ok else 'FAIL'} | og={'OK' if og_ok else 'FAIL'} | schema={'OK' if schema_ok else 'FAIL'} | canonical={'OK' if canonical_ok else 'FAIL'})")

    # ── Pendências ─────────────────────────────────────────────
    pendencias = []

    def _pend(pri, titulo, descricao, qtd=None, impacto=None):
        p = {"prioridade": pri, "titulo": titulo, "descricao": descricao}
        if qtd:    p["quantidade"] = qtd
        if impacto: p["impacto"] = impacto
        pendencias.append(p)

    # PageSpeed — maior impacto, não analisado
    if ps_mob_score is None:
        _pend("alto", "PageSpeed nao analisado (-20 pts mobile, -10 desktop)",
              "Clique em 'PageSpeed' para medir performance real. Score mobile impacta ranking no Google.",
              impacto="Core Web Vitals — fator de ranking no Google")
    elif ps_mob_score < 50:
        _pend("alto", f"PageSpeed Mobile critico: {ps_mob_score}/100",
              "Score abaixo de 50 prejudica fortemente o ranking. Otimize imagens, JS e CSS.",
              impacto="Core Web Vitals")
    elif ps_mob_score < 80:
        _pend("medio", f"PageSpeed Mobile precisa melhorar: {ps_mob_score}/100",
              "Score abaixo de 80 ainda perde pontos no Google. Reduza o tempo de carregamento.",
              impacto="Core Web Vitals")

    if not schema_ok:
        _pend("alto", "Structured Data (Schema.org) ausente",
              "Adicione JSON-LD de Product, Organization e BreadcrumbList ao tema Shopify. Habilita rich snippets no Google.",
              impacto="Rich snippets, CTR organico")
    if not og_ok:
        _pend("alto", "Open Graph ausente",
              "Tags og:title, og:description, og:image nao encontradas. Sem elas o compartilhamento no WhatsApp/Facebook nao exibe preview.",
              impacto="Trafego social, CTR")
    if not canonical_ok:
        _pend("alto", "Tag canonical ausente",
              "Sem canonical, o Google pode indexar URLs duplicadas (?variant=, ?ref=). Configure no tema.",
              impacto="Conteudo duplicado, ranking")
    if not sitemap_ok:
        _pend("alto", "sitemap.xml inacessivel",
              "O Google precisa do sitemap para indexar rapidamente novas paginas. Verifique no Shopify admin > Preferencias.",
              impacto="Indexacao")
    if not robots_ok:
        _pend("medio", "robots.txt inacessivel",
              "Arquivo robots.txt nao encontrado. Configure para controlar o que o Google pode rastrear.",
              impacto="Rastreamento")

    sem_title_col = total_col - col_com_title
    if sem_title_col > 0:
        _pend("alto", f"{sem_title_col} colecoes sem meta title",
              f"{sem_title_col} de {total_col} colecoes sem meta title SEO.", sem_title_col)
    sem_desc_col = total_col - col_com_desc
    if sem_desc_col > 0:
        _pend("alto", f"{sem_desc_col} colecoes sem meta description",
              f"{sem_desc_col} de {total_col} colecoes sem meta description.", sem_desc_col)
    sem_title_prod = total_prod - prod_com_title
    if sem_title_prod > 0:
        _pend("alto", f"{sem_title_prod} produtos sem meta title",
              f"Amostra de {total_prod}.", sem_title_prod)
    sem_alt = total_imgs - imgs_com_alt
    if sem_alt > 0:
        _pend("medio", f"{sem_alt} imagens sem alt text",
              f"{sem_alt} de {total_imgs} imagens sem texto alternativo.", sem_alt,
              "Acessibilidade e SEO de imagens")

    if avg_imgs_por_prod < 3:
        _pend("medio", f"Poucos imagens por produto (media: {avg_imgs_por_prod:.1f})",
              "Sites de alta performance tem 3-7 fotos por produto (frontal, lateral, detalhe, em uso). Aumenta conversao e SEO.",
              impacto="Conversao e rich snippets")

    if total_posts < 10:
        _pend("medio", f"Poucos posts no blog ({total_posts} — recomendado: 20+)",
              "Google valoriza sites com conteudo editorial frequente. Publique ao menos 2 posts/mes sobre cuidados, tendencias e produtos.",
              impacto="Autoridade de dominio, long-tail keywords")
    elif total_posts < 20:
        _pend("baixo", f"Blog em crescimento ({total_posts} posts — ideal: 20+)",
              "Continue publicando. 20+ posts consolidam a autoridade topica da loja.")

    sem_body_prod = total_prod - prod_com_body
    if sem_body_prod > 0:
        _pend("medio", f"{sem_body_prod} produtos com descricao muito curta",
              f"Descricoes com < 100 chars nao ajudam no SEO. Adicione informacoes tecnicas, modo de uso e beneficios.",
              sem_body_prod, "SEO de produto, conversao")

    sem_body_col = total_col - col_com_body
    if sem_body_col > 0:
        _pend("baixo", f"{sem_body_col} colecoes com descricao curta",
              f"Colecoes com pelo menos 1 paragrafo de descricao ranqueiam melhor.", sem_body_col)

    posts_sem = total_posts - posts_com_meta
    if posts_sem > 0:
        _pend("baixo", f"{posts_sem} posts do blog sem meta title",
              f"{posts_sem} de {total_posts} posts sem meta title.", posts_sem)

    # Itens manuais que não conseguimos verificar automaticamente
    _pend("medio", "Adicionar avaliações de clientes nos produtos",
          "Reviews com schema Review/AggregateRating habilitam estrelas nos resultados do Google (rich snippets). Use app de reviews no Shopify.",
          impacto="CTR organico, confianca")
    _pend("baixo", "Breadcrumbs com schema BreadcrumbList",
          "Navegacao por migalhas com JSON-LD melhora a navegacao e aparece nos resultados do Google.",
          impacto="Navegacao, rich snippets")
    _pend("baixo", "Paginas de FAQ (schema FAQPage)",
          "Perguntas frequentes com schema FAQPage aparecem expandidas no Google, aumentando a area visivel do resultado.",
          impacto="Rich snippets, CTR")

    from datetime import datetime as _dt
    resultado = {
        "score": score,
        "score_detalhes": pts,
        "analisado_em": _dt.utcnow().isoformat(),
        "colecoes": {
            "total": total_col,
            "sem_title": total_col - col_com_title,
            "sem_desc":  total_col - col_com_desc,
            "sem_body":  total_col - col_com_body,
        },
        "produtos": {
            "total":       total_prod,
            "sem_title":   total_prod - prod_com_title,
            "sem_desc":    total_prod - prod_com_desc,
            "imgs_sem_alt": total_imgs - imgs_com_alt,
            "total_imgs":  total_imgs,
        },
        "blog": {
            "total":     total_posts,
            "sem_title": total_posts - posts_com_meta,
            "publicados": total_posts,
        },
        "tecnico": {
            "sitemap":   sitemap_ok,
            "robots":    robots_ok,
            "og":        og_ok,
            "schema":    schema_ok,
            "canonical": canonical_ok,
            "https":     True,
        },
        "pagespeed": ps_cache if ps_cache else {"mobile": None, "desktop": None},
        "pendencias": pendencias,
    }
    return resultado


# ── 4. SEO AUTOMÁTICO — TODAS AS COLEÇÕES RESTANTES ─────────

def _gerar_seo_colecao(nome: str, handle: str) -> dict:
    """Gera SEO automático a partir do nome da coleção."""
    nome_limpo = nome.strip()
    return {
        "title": f"{nome_limpo} — Comprar online | Shinsei Market",
        "desc": (
            f"Encontre os melhores produtos {nome_limpo} na Shinsei Market. "
            f"Qualidade garantida com entrega rápida para todo o Brasil."
        ),
        "body": (
            f"Explore a linha completa de produtos {nome_limpo} disponíveis na Shinsei Market. "
            f"Confira as opções e aproveite o frete rápido para todo o Brasil."
        ),
    }

def seo_todas_colecoes():
    sep("SEO AUTOMÁTICO — TODAS AS COLEÇÕES")
    custom = [{**i, "_tipo": "custom_collections"} for i in get_all("/custom_collections.json", "custom_collections")]
    smart  = [{**i, "_tipo": "smart_collections"}  for i in get_all("/smart_collections.json",  "smart_collections")]
    todas  = custom + smart

    # Pula as que já têm SEO manual no mapa
    handles_manuais = set(COLECOES_SEO.keys())
    pendentes = [c for c in todas if c.get("handle", "") not in handles_manuais]
    print(f"  {len(todas)} coleções total | {len(pendentes)} sem SEO manual → aplicando auto-SEO")

    ok = err = 0
    for col in pendentes:
        handle   = col.get("handle", "")
        nome     = col.get("title", handle)
        col_id   = col["id"]
        tipo_url = col["_tipo"]
        chave    = "custom_collection" if tipo_url == "custom_collections" else "smart_collection"
        seo      = _gerar_seo_colecao(nome, handle)

        payload = {chave: {
            "id": col_id,
            "body_html": seo["body"] if not (col.get("body_html") or "").strip() else col["body_html"],
            "metafields_global_title_tag": seo["title"],
            "metafields_global_description_tag": seo["desc"],
        }}
        try:
            put(f"/{tipo_url}/{col_id}.json", payload)
            print(f"  ✅ {handle} — {nome[:40]}")
            ok += 1
        except Exception as e:
            print(f"  ❌ {handle}: {e}")
            err += 1
        time.sleep(0.4)

    print(f"\n  Resultado: {ok} OK | {err} erro(s)")


# ── 5. SEO AUTOMÁTICO — PRODUTOS ────────────────────────────

def seo_produtos():
    sep("SEO AUTOMÁTICO — PRODUTOS")
    produtos = get_all("/products.json", "products",
                       {"fields": "id,title,product_type,vendor,metafields_global_title_tag"})
    pendentes = [p for p in produtos if not p.get("metafields_global_title_tag")]
    print(f"  {len(produtos)} produtos | {len(pendentes)} sem meta title → aplicando")

    ok = err = 0
    for prod in pendentes:
        pid   = prod["id"]
        nome  = prod.get("title", "")
        tipo  = prod.get("product_type", "")
        marca = prod.get("vendor", "")

        partes = [p for p in [tipo, marca] if p]
        sufixo = f" — {' | '.join(partes)}" if partes else ""
        seo_title = f"{nome}{sufixo} | Shinsei Market"[:70]
        seo_desc  = (
            f"Compre {nome} na Shinsei Market. "
            f"{'Produto ' + tipo.lower() + ' da marca ' + marca + '.' if tipo and marca else ''} "
            f"Entrega rápida para todo o Brasil."
        ).strip()[:160]

        payload = {"product": {
            "id": pid,
            "metafields_global_title_tag": seo_title,
            "metafields_global_description_tag": seo_desc,
        }}
        try:
            put(f"/products/{pid}.json", payload)
            ok += 1
            if ok % 10 == 0:
                print(f"  ... {ok}/{len(pendentes)} produtos atualizados")
        except Exception as e:
            print(f"  ❌ {nome[:30]}: {e}")
            err += 1
        time.sleep(0.5)

    print(f"\n  Resultado: {ok} OK | {err} erro(s)")


# ── FIX: COLEÇÕES SEM SEO ──────────────────────────────────────

def fix_colecoes_sem_seo():
    sep("FIX — COLEÇÕES SEM SEO (GraphQL mutation)")
    _q = """
    query($cursor: String) {
      collections(first: 250, after: $cursor) {
        edges { node { id title handle seo { title description } } }
        pageInfo { hasNextPage endCursor }
      }
    }"""
    col_nodes = []
    _cursor = None
    while True:
        _d = gql(_q, {"cursor": _cursor})
        _edges = (_d.get("collections") or {}).get("edges", [])
        col_nodes.extend(e["node"] for e in _edges)
        _pi = (_d.get("collections") or {}).get("pageInfo", {})
        if not _pi.get("hasNextPage"):
            break
        _cursor = _pi.get("endCursor")

    sem_seo = [c for c in col_nodes if not ((c.get("seo") or {}).get("title") or "").strip()]
    print(f"  {len(col_nodes)} coleções | {len(sem_seo)} sem SEO → aplicando")

    _mut = """
    mutation($id: ID!, $seoTitle: String!, $seoDesc: String!) {
      collectionUpdate(input: {
        id: $id
        seo: { title: $seoTitle, description: $seoDesc }
      }) {
        userErrors { field message }
      }
    }"""

    ok = err = 0
    for col in sem_seo:
        nome = col.get("title", col.get("handle", ""))
        nome_limpo = nome.strip()
        seo_t = f"{nome_limpo} — Comprar online | Shinsei Market"
        seo_d = (
            f"Encontre os melhores produtos {nome_limpo} na Shinsei Market. "
            f"Qualidade garantida com entrega rápida para todo o Brasil."
        )
        res = gql(_mut, {"id": col["id"], "seoTitle": seo_t, "seoDesc": seo_d})
        erros = (res.get("collectionUpdate") or {}).get("userErrors", [])
        if erros:
            print(f"  ❌ {nome_limpo[:40]}: {erros[0].get('message')}")
            err += 1
        else:
            ok += 1
            if ok % 10 == 0:
                print(f"  ... {ok}/{len(sem_seo)} OK")
        time.sleep(0.25)

    print(f"\n  Resultado: {ok} OK | {err} erro(s)")


# ── FIX: BLOG SEO ───────────────────────────────────────────────

def fix_blog_seo():
    sep("FIX — BLOG POSTS SEO (metafields)")
    _q = """
    {
      blogs(first: 20) {
        edges {
          node {
            id
            articles(first: 50) {
              edges {
                node {
                  id title handle
                  seo { title description }
                }
              }
            }
          }
        }
      }
    }"""
    _d = gql(_q)
    ok = err = skip = 0
    for blog_edge in (_d.get("blogs") or {}).get("edges", []):
        for art_edge in (blog_edge.get("node") or {}).get("articles", {}).get("edges", []):
            art = art_edge.get("node") or {}
            seo = art.get("seo") or {}
            if (seo.get("title") or "").strip():
                skip += 1
                continue
            # Extrair blog_id e article_id do GID
            blog_gid  = blog_edge["node"]["id"]   # gid://shopify/Blog/123
            art_gid   = art["id"]                  # gid://shopify/Article/456
            blog_rest = blog_gid.split("/")[-1]
            art_rest  = art_gid.split("/")[-1]
            titulo = art.get("title", "")
            seo_t = f"{titulo} | Shinsei Market"[:70]
            seo_d = f"Leia {titulo} no blog da Shinsei Market. Dicas e novidades sobre beleza e cosméticos."[:160]
            for p in POSTS:
                if p.get("handle") == art.get("handle"):
                    seo_t = p["seo_title"]
                    seo_d = p["seo_desc"]
                    break
            try:
                post(
                    f"/blogs/{blog_rest}/articles/{art_rest}/metafields.json",
                    {"metafield": {"namespace": "global", "key": "title_tag",
                                   "value": seo_t, "type": "single_line_text_field"}},
                )
                post(
                    f"/blogs/{blog_rest}/articles/{art_rest}/metafields.json",
                    {"metafield": {"namespace": "global", "key": "description_tag",
                                   "value": seo_d, "type": "single_line_text_field"}},
                )
                print(f"  ✅ {titulo[:50]}")
                ok += 1
            except Exception as e:
                print(f"  ❌ {titulo[:40]}: {e}")
                err += 1
            time.sleep(0.5)
    print(f"\n  Resultado: {ok} OK | {skip} já tinham SEO | {err} erro(s)")


# ── FIX: ALT TEXT DAS IMAGENS ──────────────────────────────────

def fix_alt_text():
    sep("FIX — ALT TEXT DAS IMAGENS")
    _q = """
    query($cursor: String) {
      products(first: 250, after: $cursor) {
        edges {
          node {
            id title productType vendor
            images(first: 20) { edges { node { id altText } } }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }"""
    prod_nodes = []
    _cursor = None
    while True:
        _d = gql(_q, {"cursor": _cursor})
        _edges = (_d.get("products") or {}).get("edges", [])
        prod_nodes.extend(e["node"] for e in _edges)
        _pi = (_d.get("products") or {}).get("pageInfo", {})
        if not _pi.get("hasNextPage"):
            break
        _cursor = _pi.get("endCursor")

    total = sem_alt = ok = err = 0
    for prod in prod_nodes:
        pid_gid = prod["id"]
        pid = pid_gid.split("/")[-1]
        nome = prod.get("title", "")
        tipo = prod.get("productType", "")
        marca = prod.get("vendor", "")
        alt = f"{nome} — {tipo} {marca}".strip(" —").strip()[:125]
        for img_edge in (prod.get("images") or {}).get("edges", []):
            img = img_edge.get("node") or {}
            total += 1
            if (img.get("altText") or "").strip():
                continue
            sem_alt += 1
            img_id = img["id"].split("/")[-1]
            try:
                put(f"/products/{pid}/images/{img_id}.json",
                    {"image": {"id": img_id, "alt": alt}})
                ok += 1
            except Exception as e:
                print(f"  ❌ img {img_id} ({nome[:30]}): {e}")
                err += 1
            time.sleep(0.2)

    print(f"  {total} imagens | {sem_alt} sem alt | {ok} corrigidas | {err} erros")


# ── MAIN ─────────────────────────────────────────────────────

if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else "tudo"
    print(f"\n{'='*60}")
    print(f"  SHINSEI MARKET — SEO COMPLETO")
    print(f"  Loja: {STORE}  |  Modo: {modo}")
    print('='*60)
    try:
        if modo in ("colecoes", "tudo"):
            atualizar_colecoes()
        if modo in ("todas_colecoes",):
            seo_todas_colecoes()
        if modo in ("produtos",):
            seo_produtos()
        if modo in ("blog", "tudo"):
            criar_posts()
        if modo in ("health", "tudo"):
            health_score()
        if modo == "completo":
            atualizar_colecoes()
            seo_todas_colecoes()
            seo_produtos()
            criar_posts()
            health_score()
        if modo == "fix100":
            fix_colecoes_sem_seo()
            fix_blog_seo()
            fix_alt_text()
            health_score()
        print("\n✅ Concluído!\n")
    except Exception as e:
        import traceback
        print(f"\n❌ Erro: {e}")
        traceback.print_exc()
