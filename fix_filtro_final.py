with open('pages/auditoria_automatica.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Adiciona variável de filtro global e modifica carregarAmazon
old = "async function carregarAmazon(){\n  const res = await fetch('/auditoria/amazon').catch(()=>null);\n  if(!res) return;\n  const data = await res.json();\n  const n = data.stats?.pendente || 0;\n  const el = document.getElementById('statAmazon');\n  if(el) el.textContent = n || '\u2014';\n  const badge = document.getElementById('badge-amazon');\n  if(badge) badge.textContent = n;\n  const tbody = document.getElementById('tabela-amazon');\n  const empty = document.getElementById('amazon-empty');\n  const itens = data.itens || [];\n  if(!tbody) return;\n  if(!itens.length){ tbody.innerHTML=''; if(empty){empty.style.display='block';} return; }\n  if(empty) empty.style.display='none';"

new = "let _amazonFiltroTipo = '';\nasync function carregarAmazon(filtroTipo){\n  if(filtroTipo !== undefined) _amazonFiltroTipo = filtroTipo;\n  const res = await fetch('/auditoria/amazon').catch(()=>null);\n  if(!res) return;\n  const data = await res.json();\n  const n = data.stats?.pendente || 0;\n  const el = document.getElementById('statAmazon');\n  if(el) el.textContent = n || '\u2014';\n  const badge = document.getElementById('badge-amazon');\n  if(badge) badge.textContent = n;\n  const tbody = document.getElementById('tabela-amazon');\n  const empty = document.getElementById('amazon-empty');\n  let itens = data.itens || [];\n  if(_amazonFiltroTipo) itens = itens.filter(i=>i.tipo===_amazonFiltroTipo);\n  if(!tbody) return;\n  if(!itens.length){ tbody.innerHTML=''; if(empty){empty.style.display='block';} return; }\n  if(empty) empty.style.display='none';"

if old in content:
    content = content.replace(old, new, 1)
    print('OK - filtro adicionado ao carregarAmazon')
elif 'let _amazonFiltroTipo' in content:
    print('Filtro já existe')
else:
    print('AVISO - bloco não encontrado')
    # Debug
    idx = content.find('async function carregarAmazon')
    print(f'  carregarAmazon em posição: {idx}')
    if idx > 0:
        print(f'  contexto: {repr(content[idx:idx+200])}')

# Atualiza conferirAmazon para passar tipo como filtro
old2 = "  carregarAmazon();\n  if(btn){btn.disabled=false;btn.textContent=btnLabel[tipo]||'Conferir';}"
new2 = "  carregarAmazon(tipo);\n  if(btn){btn.disabled=false;btn.textContent=btnLabel[tipo]||'Conferir';}"
if old2 in content:
    content = content.replace(old2, new2, 1)
    print('OK - conferirAmazon passa tipo como filtro')

with open('pages/auditoria_automatica.html', 'w', encoding='utf-8') as f:
    f.write(content)
print('Pronto!')
