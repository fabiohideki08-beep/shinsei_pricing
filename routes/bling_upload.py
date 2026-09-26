"""
Rota para upload automático de imagens Bling via Playwright + CDP.
Endpoint: /bling/upload-imagens
"""
from __future__ import annotations
import json, subprocess, sys, threading, time
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()

BASE_DIR     = Path(__file__).parent.parent
SCRIPT_PATH  = BASE_DIR / "bling_upload_imagens_v3.py"
PROGRESS_FILE = BASE_DIR / "data" / "upload_progress.json"
LOG_FILE     = BASE_DIR / "data" / "upload_log.txt"
MAP_FILE     = BASE_DIR / "data" / "kits_imagens_map.json"

# Estado global do processo em background
_proc: subprocess.Popen | None = None
_lock = threading.Lock()


def _total_kits() -> int:
    try:
        return len(json.loads(MAP_FILE.read_text(encoding='utf-8')))
    except Exception:
        return 125


def _progress() -> dict:
    try:
        data = json.loads(PROGRESS_FILE.read_text(encoding='utf-8'))
        ok     = len(data.get('ok', []))
        erros  = data.get('erros', [])
        total  = _total_kits()
        return {'ok': ok, 'erros': len(erros), 'erros_list': erros[-5:], 'total': total,
                'pct': round(ok / total * 100, 1) if total else 0}
    except Exception:
        return {'ok': 0, 'erros': 0, 'erros_list': [], 'total': _total_kits(), 'pct': 0}


def _is_running() -> bool:
    global _proc
    with _lock:
        return _proc is not None and _proc.poll() is None


@router.get("/bling/upload-imagens", response_class=HTMLResponse)
def upload_imagens_page():
    prog = _progress()
    running = _is_running()

    # Últimas linhas do log
    log_lines = ""
    try:
        if LOG_FILE.exists():
            lines = LOG_FILE.read_text(encoding='utf-8', errors='ignore').splitlines()
            log_lines = "\n".join(lines[-20:])
    except Exception:
        pass

    status_badge = (
        '<span style="background:#22c55e;color:#fff;padding:3px 10px;border-radius:9px">Rodando</span>'
        if running else
        '<span style="background:#6b7280;color:#fff;padding:3px 10px;border-radius:9px">Parado</span>'
    )

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="4">
<title>Upload Imagens Bling</title>
<style>
  body{{font-family:system-ui,sans-serif;background:#111;color:#e5e7eb;margin:0;padding:20px}}
  h1{{color:#f9fafb;font-size:1.4rem;margin-bottom:4px}}
  .card{{background:#1f2937;border-radius:10px;padding:16px 20px;margin-bottom:14px}}
  .bar-bg{{background:#374151;border-radius:8px;height:18px;overflow:hidden;margin:10px 0}}
  .bar-fill{{background:#22c55e;height:100%;border-radius:8px;transition:width .5s}}
  .nums{{font-size:1.1rem;font-weight:600}}
  .ok{{color:#22c55e}} .err{{color:#f87171}} .tot{{color:#9ca3af}}
  button{{padding:10px 22px;border:none;border-radius:8px;font-size:.95rem;cursor:pointer;font-weight:600}}
  .btn-start{{background:#22c55e;color:#000}} .btn-stop{{background:#ef4444;color:#fff}}
  .btn-reset{{background:#374151;color:#fff;margin-left:8px}}
  pre{{background:#111;border-radius:6px;padding:12px;font-size:.78rem;max-height:220px;overflow-y:auto;color:#d1d5db;white-space:pre-wrap}}
  .warn{{background:#7c2d12;border-radius:6px;padding:10px 14px;color:#fca5a5;font-size:.85rem;margin-bottom:12px}}
</style>
</head>
<body>
<h1>Upload de Imagens — Kits Bling</h1>
<p style="color:#6b7280;font-size:.85rem">Atualiza automaticamente a cada 4s</p>

<div class="card">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">
    <span class="nums tot">Status:</span> {status_badge}
  </div>
  <div class="bar-bg"><div class="bar-fill" style="width:{prog['pct']}%"></div></div>
  <div class="nums">
    <span class="ok">✓ {prog['ok']}</span> &nbsp;/&nbsp;
    <span class="tot">{prog['total']} total</span>
    &nbsp;&nbsp;
    <span class="err">✗ {prog['erros']} erros</span>
    &nbsp;&nbsp;
    <span style="color:#facc15">{prog['pct']}%</span>
  </div>
</div>

<div class="card">
  <div class="warn">
    ⚠️ <strong>Antes de iniciar:</strong> o script vai abrir o Chrome automaticamente.
    Faça login no Bling quando ele abrir, depois volte ao terminal e pressione Enter.
    <br>Ou use o Terminal diretamente: <code>python bling_upload_imagens_v3.py</code>
  </div>

  <form method="POST" action="/bling/upload-imagens/iniciar" style="display:inline">
    <button class="btn-start" type="submit" {'disabled' if running else ''}>▶ Iniciar Upload</button>
  </form>
  <form method="POST" action="/bling/upload-imagens/parar" style="display:inline">
    <button class="btn-stop" type="submit" {'disabled' if not running else ''}>■ Parar</button>
  </form>
  <form method="POST" action="/bling/upload-imagens/reset" style="display:inline">
    <button class="btn-reset" type="submit">↺ Resetar progresso</button>
  </form>
</div>

{'<div class="card"><b style="color:#f87171">Últimos erros:</b><br>' + '<br>'.join(f"[{e.get('color','')}] {e.get('erro','')[:80]}" for e in prog['erros_list']) + '</div>' if prog['erros_list'] else ''}

<div class="card">
  <b>Log (últimas 20 linhas):</b>
  <pre id="log">{log_lines or '(sem log ainda)'}</pre>
</div>

<script>
// Auto-scroll log para o final
const log = document.getElementById('log');
if(log) log.scrollTop = log.scrollHeight;
</script>
</body></html>"""
    return HTMLResponse(html)


@router.post("/bling/upload-imagens/iniciar")
def upload_iniciar():
    global _proc
    if _is_running():
        return JSONResponse({'status': 'já rodando'})
    with _lock:
        log = open(LOG_FILE, 'a', encoding='utf-8', buffering=1)
        _proc = subprocess.Popen(
            [sys.executable, str(SCRIPT_PATH)],
            stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NEW_CONSOLE,  # abre janela separada no Windows
        )
    from fastapi.responses import RedirectResponse
    return RedirectResponse('/bling/upload-imagens', status_code=303)


@router.post("/bling/upload-imagens/parar")
def upload_parar():
    global _proc
    with _lock:
        if _proc and _proc.poll() is None:
            _proc.terminate()
    from fastapi.responses import RedirectResponse
    return RedirectResponse('/bling/upload-imagens', status_code=303)


@router.post("/bling/upload-imagens/reset")
def upload_reset():
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.write_text('{"ok":[],"erros":[]}', encoding='utf-8')
    if LOG_FILE.exists():
        LOG_FILE.write_text('', encoding='utf-8')
    from fastapi.responses import RedirectResponse
    return RedirectResponse('/bling/upload-imagens', status_code=303)


@router.get("/bling/upload-imagens/status")
def upload_status():
    return JSONResponse({**_progress(), 'running': _is_running()})
