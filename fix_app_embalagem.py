with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

old = '    for campo in ["fila_auto_ao_calcular", "modo_auto"]:\n        if campo in data:\n            cfg_atual[campo] = bool(data[campo])'

new = '    for campo in ["fila_auto_ao_calcular", "modo_auto", "ml_api_real"]:\n        if campo in data:\n            cfg_atual[campo] = bool(data[campo])\n    for campo in ["embalagem_padrao", "imposto_padrao"]:\n        if campo in data:\n            try:\n                cfg_atual[campo] = float(data[campo])\n            except (TypeError, ValueError):\n                pass'

if old in content:
    content = content.replace(old, new, 1)
    print('OK - embalagem_padrao e ml_api_real adicionados ao endpoint')
else:
    print('AVISO - bloco não encontrado')

with open('app.py', 'w', encoding='utf-8') as f:
    f.write(content)
print('app.py atualizado!')
