from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

import desktop_ui as dui


_SERVER: Optional[ThreadingHTTPServer] = None
_THREAD: Optional[threading.Thread] = None
_LOOP: Optional[asyncio.AbstractEventLoop] = None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        raw = str(os.getenv(name, "") or "").strip()
        return int(raw) if raw else default
    except Exception:
        return default


def _is_local_network_client(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)


def _run_async(coro: Awaitable[object], timeout: float = 360.0) -> object:
    loop = _LOOP
    if loop is None:
        raise RuntimeError("Web UI loop is not configured")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


def _state_payload() -> dict:
    rows = _run_async(dui._collect_servers(), timeout=15.0)
    servers = rows if isinstance(rows, list) else []
    return {
        "servers": servers,
        "metrics": dui._collect_system_metrics(servers),
        "history": [
            {
                "timestamp": str(item[0]),
                "action": str(item[1]),
                "server": str(item[2]),
                "status": str(item[3]),
                "details": str(item[4] or ""),
            }
            for item in dui._collect_history(80)
        ],
        "logs": list(dui._LIVE_LOG_LINES)[-260:],
        "templates": dui._list_templates(),
        "backups": dui._list_backup_files(),
    }


def _json_response(handler: BaseHTTPRequestHandler, payload: object, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _text_response(handler: BaseHTTPRequestHandler, body: str, status: int = 200, content_type: str = "text/html") -> None:
    encoded = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", f"{content_type}; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length", "0") or "0")
    except ValueError:
        length = 0
    if length <= 0:
        return {}
    raw = handler.rfile.read(min(length, 1024 * 1024))
    if not raw:
        return {}
    data = json.loads(raw.decode("utf-8"))
    return data if isinstance(data, dict) else {}


async def _handle_action(data: dict) -> tuple[bool, str]:
    name = str(data.get("name", "") or "").strip()
    action = str(data.get("action", "") or "").strip().lower()
    if action in {"start", "stop", "restart", "update"}:
        return await dui._run_action(name, action)
    if action == "backup":
        return await dui._create_backup_action(name)
    if action == "restore":
        return await dui._restore_backup_action(
            name,
            str(data.get("backup_file", "") or "").strip(),
            overwrite=bool(data.get("overwrite", False)),
        )
    if action == "remove":
        return await dui._remove_server_action(name, backup_before_delete=bool(data.get("backup_first", True)))
    return False, f"Unknown action: {action}"


async def _handle_settings(data: dict) -> tuple[bool, str]:
    return await dui._save_settings(
        name=str(data.get("name", "") or "").strip(),
        executable=str(data.get("executable", "") or "").strip(),
        parameters=str(data.get("parameters", "") or "").strip(),
        auto_start=bool(data.get("auto_start", False)),
        auto_update=bool(data.get("auto_update", False)),
        auto_restart=bool(data.get("auto_restart", True)),
        restart_after_stop=bool(data.get("restart_after_stop", False)),
        stop_time=str(data.get("stop_time", "") or "").strip(),
    )


async def _handle_template(data: dict) -> tuple[bool, str]:
    return await dui._create_template_action(
        template_name=str(data.get("template_name", "") or "").strip(),
        app_id=str(data.get("app_id", "") or "").strip(),
        executable=str(data.get("executable", "") or "").strip(),
        parameters=str(data.get("parameters", "") or "").strip(),
        auto_start=bool(data.get("auto_start", False)),
        auto_update=bool(data.get("auto_update", False)),
        auto_restart=bool(data.get("auto_restart", True)),
        restart_after_stop=bool(data.get("restart_after_stop", False)),
        stop_time=str(data.get("stop_time", "") or "").strip(),
        username=str(data.get("username", "") or "").strip(),
        password=str(data.get("password", "") or "").strip(),
    )


async def _handle_add_server(data: dict) -> tuple[bool, str]:
    return await dui._add_server_action(
        name=str(data.get("name", "") or "").strip(),
        template_name=str(data.get("template_name", "") or "").strip(),
        instance_id=str(data.get("instance_id", "") or "").strip(),
        start_after_install=bool(data.get("start_after_install", True)),
    )


class _WebUiHandler(BaseHTTPRequestHandler):
    server_version = "DGSMWebUI/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        logging.info("[WEB-UI] %s - %s", self.client_address[0], fmt % args)

    def _guard_local_client(self) -> bool:
        if not _env_bool("DGSM_WEB_UI_LOCAL_NETWORK_ONLY", True):
            return True
        if _is_local_network_client(str(self.client_address[0])):
            return True
        _json_response(self, {"ok": False, "message": "Forbidden: local network only"}, status=HTTPStatus.FORBIDDEN)
        return False

    def do_GET(self) -> None:
        if not self._guard_local_client():
            return
        path = urlparse(self.path).path
        try:
            if path in {"/", "/index.html"}:
                _text_response(self, _INDEX_HTML)
                return
            if path == "/api/state":
                _json_response(self, {"ok": True, "data": _state_payload()})
                return
            if path == "/logo.png":
                logo = dui._find_logo_file()
                if logo and os.path.isfile(logo):
                    with open(logo, "rb") as handle:
                        body = handle.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Cache-Control", "public, max-age=3600")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(404)
                return
            self.send_error(404)
        except Exception as exc:
            logging.exception("[WEB-UI] GET failed")
            _json_response(self, {"ok": False, "message": str(exc)}, status=500)

    def do_POST(self) -> None:
        if not self._guard_local_client():
            return
        path = urlparse(self.path).path
        try:
            data = _read_json(self)
            if path == "/api/action":
                ok, message = _run_async(_handle_action(data))
            elif path == "/api/settings":
                ok, message = _run_async(_handle_settings(data))
            elif path == "/api/cli":
                command = str(data.get("command", "") or "").strip()
                dui._LIVE_LOG_LINES.append(f"[WEB] > {html.escape(command)[:220]}")
                ok, message = _run_async(dui._run_cli_user_command(command), timeout=360.0)
            elif path == "/api/template":
                ok, message = _run_async(_handle_template(data), timeout=360.0)
            elif path == "/api/server":
                ok, message = _run_async(_handle_add_server(data), timeout=600.0)
            else:
                self.send_error(404)
                return
            _json_response(self, {"ok": bool(ok), "message": str(message)})
        except Exception as exc:
            logging.exception("[WEB-UI] POST failed: %s", path)
            _json_response(self, {"ok": False, "message": str(exc)}, status=500)


def start_web_ui(
    loop: asyncio.AbstractEventLoop,
    refresh_callback: Optional[Callable[[], Awaitable[None]]] = None,
) -> bool:
    global _LOOP, _SERVER, _THREAD
    if not _env_bool("DGSM_WEB_UI_ENABLED", False):
        logging.info("[WEB-UI] disabled via DGSM_WEB_UI_ENABLED")
        return False
    if _SERVER is not None:
        return True

    host = str(os.getenv("DGSM_WEB_UI_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = _env_int("DGSM_WEB_UI_PORT", 8765)
    if not host:
        host = "127.0.0.1"

    _LOOP = loop
    if refresh_callback is not None:
        dui._REFRESH_CALLBACK = refresh_callback
    dui._bootstrap_log_lines()
    dui._install_live_log_handler()

    try:
        server = ThreadingHTTPServer((host, port), _WebUiHandler)
    except Exception:
        logging.exception("[WEB-UI] could not bind %s:%s", host, port)
        return False

    thread = threading.Thread(target=server.serve_forever, name="DGSM-Web-UI", daemon=True)
    _SERVER = server
    _THREAD = thread
    thread.start()
    logging.info("[WEB-UI] started on http://%s:%s", host, port)
    return True


def is_web_ui_started() -> bool:
    return bool(_SERVER is not None and _THREAD is not None and _THREAD.is_alive())


_INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DGSM Web UI</title>
  <style>
    :root{--bg:#131826;--s1:#1b2235;--s2:#20283d;--s3:#252f47;--s4:#2b3652;--text:#edf2ff;--muted:#a8b4d1;--accent:#4f81ff;--accent2:#67d4ff;--ok:#3ecf8e;--warn:#ffb34a;--danger:#ff5f76;--edge:#49577a;--console:#0f1420}
    *{box-sizing:border-box} body{margin:0;background:linear-gradient(180deg,var(--s1),var(--bg));color:var(--text);font-family:Segoe UI,Arial,sans-serif;font-size:14px}
    button,input,select{font:inherit} button{border:1px solid #5a6a90;border-radius:8px;background:var(--s4);color:var(--text);font-weight:700;padding:8px 12px;cursor:pointer} button:hover{background:#344361} button:disabled{opacity:.48;cursor:not-allowed}.primary{background:var(--accent)}.danger{background:var(--danger)}.ghost{color:var(--accent2)}
    .wrap{max-width:1480px;margin:0 auto;padding:16px}.top{display:flex;align-items:center;gap:18px;background:var(--s2);border:1px solid var(--edge);border-radius:8px;padding:14px}.logo{height:92px;max-width:260px;object-fit:contain}.summary{color:var(--accent2);font-weight:700}.selected-line{color:var(--muted);text-align:right}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-left:auto}
    .metrics{display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:10px;margin:12px 0}.metric{background:var(--s2);border:1px solid #35425f;border-radius:8px;padding:10px}.metric b{display:block;color:var(--muted);font-size:12px}.metric span{display:block;color:var(--accent2);font-weight:800;margin-top:4px}
    .grid{display:grid;grid-template-columns:minmax(420px,1fr) minmax(360px,.75fr);gap:12px}.panel,.card{background:var(--s2);border:1px solid #35425f;border-radius:8px;padding:12px}.panel h2{font-size:16px;margin:0 0 10px}.servers{display:flex;flex-direction:column;gap:10px}.card{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center}.card.selected{background:var(--s3);border-color:#6f8fd8}.name{font-weight:800}.detail,.feedback,.hint{color:var(--muted);font-size:13px}.badge{display:inline-block;border-radius:8px;padding:3px 9px;font-size:12px;font-weight:800;margin-left:8px}.running{background:#1a3b2f;color:var(--ok);border:1px solid #2f6a4f}.stopped{background:#2a3042;color:var(--muted);border:1px solid #3c4764}.updating{background:#463521;color:var(--warn);border:1px solid #7f5b2b}.failed{background:#4d2028;color:var(--danger);border:1px solid #8e3a49}.card-actions{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
    .form{display:grid;gap:8px}.form label{color:var(--muted);font-size:12px;font-weight:700}.form input,.form select{width:100%;border:1px solid #57688d;border-radius:8px;background:var(--s3);color:var(--text);padding:8px}.checks{display:flex;gap:12px;flex-wrap:wrap}.checks label{display:flex;align-items:center;gap:6px;color:var(--text);font-size:13px}.tabs{display:flex;gap:8px;margin-bottom:10px}.tab{display:none}.tab.active{display:block}.log,.history{background:var(--console);color:#d3e2ff;border-radius:8px;padding:10px;min-height:220px;max-height:360px;overflow:auto;white-space:pre-wrap;font-family:Consolas,monospace;font-size:12px}.toast{position:fixed;right:16px;bottom:16px;background:var(--s3);border:1px solid var(--edge);border-radius:8px;padding:12px;max-width:520px;display:none}.toast.ok{border-color:var(--ok)}.toast.fail{border-color:var(--danger)}
    @media(max-width:980px){.grid{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,1fr)}.top{align-items:flex-start;flex-direction:column}.actions{margin-left:0}.card{grid-template-columns:1fr}.card-actions{justify-content:flex-start}}
  </style>
</head>
<body>
  <div class="wrap">
    <header class="top">
      <div><img class="logo" src="/logo.png" alt="DGSM"><div id="summary" class="summary">Running 0 | Stopped 0 | Updating 0</div></div>
      <div class="actions">
        <button class="primary" data-top="start">Start</button><button class="danger" data-top="stop">Stop</button><button data-top="restart">Restart</button><button data-top="update">Update</button><button class="ghost" id="refresh">Refresh</button>
      </div>
      <div id="selectedLine" class="selected-line">Selected: -</div>
    </header>
    <section id="metrics" class="metrics"></section>
    <main class="grid">
      <section class="panel"><h2>Servers</h2><div id="servers" class="servers"></div></section>
      <aside class="panel">
        <div class="tabs"><button data-tab="settings">Settings</button><button data-tab="tools">Tools</button><button data-tab="logs">Logs</button></div>
        <div id="tab-settings" class="tab active">
          <h2>Server Settings</h2>
          <form id="settingsForm" class="form">
            <div class="hint" id="settingsName">Selected: -</div>
            <label>Executable<input name="executable"></label>
            <label>Parameters<input name="parameters"></label>
            <label>Daily stop (HH:MM)<input name="stop_time"></label>
            <div class="checks">
              <label><input type="checkbox" name="auto_start"> Auto start</label>
              <label><input type="checkbox" name="auto_restart"> Auto restart</label>
              <label><input type="checkbox" name="auto_update"> Auto update</label>
              <label><input type="checkbox" name="restart_after_stop"> Restart after stop</label>
            </div>
            <button class="primary" type="submit">Save Settings</button>
          </form>
        </div>
        <div id="tab-tools" class="tab">
          <h2>Tools / Commands</h2>
          <form id="cliForm" class="form"><label>CLI command<input name="command"></label><button type="submit">Run CLI</button></form>
          <hr>
          <form id="templateForm" class="form">
            <h2>Create Template</h2>
            <label>Template name<input name="template_name"></label><label>Steam App ID<input name="app_id"></label><label>Executable<input name="executable"></label><label>Parameters<input name="parameters"></label><label>Daily stop (HH:MM)<input name="stop_time" value="05:00"></label>
            <div class="checks"><label><input type="checkbox" name="auto_start"> Auto start</label><label><input type="checkbox" name="auto_update" checked> Auto update</label><label><input type="checkbox" name="auto_restart" checked> Auto restart</label><label><input type="checkbox" name="restart_after_stop"> Restart after stop</label></div>
            <button type="submit">Create Template</button>
          </form>
          <hr>
          <form id="serverForm" class="form">
            <h2>Add Server</h2>
            <label>Server name<input name="name"></label><label>Template<select name="template_name"></select></label><label>Instance ID<input name="instance_id"></label>
            <div class="checks"><label><input type="checkbox" name="start_after_install" checked> Start after install</label></div>
            <button type="submit">Add Server</button>
          </form>
        </div>
        <div id="tab-logs" class="tab"><h2>Live Log</h2><div id="logs" class="log"></div><h2>Action History</h2><div id="history" class="history"></div></div>
      </aside>
    </main>
  </div>
  <div id="toast" class="toast"></div>
  <script>
    let state={servers:[],metrics:{},templates:[],backups:[],logs:[],history:[]};let selected=null;let busy=false;
    const $=s=>document.querySelector(s);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    function toast(ok,msg){const t=$('#toast');t.className='toast '+(ok?'ok':'fail');t.textContent=msg;t.style.display='block';setTimeout(()=>t.style.display='none',4500)}
    async function api(path,data){busy=true;render();try{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const j=await r.json();toast(j.ok,j.message||'Done');await load();return j}finally{busy=false;render()}}
    async function load(){const r=await fetch('/api/state',{cache:'no-store'});const j=await r.json();if(!j.ok)throw new Error(j.message||'State failed');state=j.data;if(!selected&&state.servers[0])selected=state.servers[0].name;if(selected&&!state.servers.some(s=>s.name===selected))selected=state.servers[0]?.name||null;render()}
    function selectedRow(){return state.servers.find(s=>s.name===selected)}
    function render(){const running=state.servers.filter(s=>s.state==='running').length,stopped=state.servers.filter(s=>s.state==='stopped').length,updating=state.servers.filter(s=>s.state==='updating').length;$('#summary').textContent=`Running ${running} | Stopped ${stopped} | Updating ${updating}`;const row=selectedRow();$('#selectedLine').textContent=row?`Selected: ${row.name} | ${row.status} | ${row.detail}`:'Selected: -';
      $('#metrics').innerHTML=['cpu','ram','disk','network','servers','uptime'].map(k=>`<div class="metric"><b>${k.toUpperCase()}</b><span>${esc(state.metrics[k]||'--')}</span></div>`).join('');
      $('#servers').innerHTML=state.servers.map(s=>{const run=!!s.running,b=s.state==='updating';return `<article class="card ${s.name===selected?'selected':''}" data-name="${esc(s.name)}"><div><span class="name">${esc(s.name)}</span><span class="badge ${esc(s.state)}">${esc(s.status)}</span><div class="detail">${esc(s.detail)}</div><div class="feedback">${esc(s.feedback||'')}</div></div><div class="card-actions"><button class="primary" data-action="start" ${b||run||busy?'disabled':''}>Start</button><button class="danger" data-action="stop" ${b||!run||busy?'disabled':''}>Stop</button><button data-action="restart" ${b||busy?'disabled':''}>Restart</button><button data-action="update" ${b||run||busy?'disabled':''}>Update</button><button class="ghost" data-action="backup" ${b||busy?'disabled':''}>Backup</button><button data-action="restore" ${b||run||busy?'disabled':''}>Restore</button><button class="danger" data-action="remove" ${b||busy?'disabled':''}>Delete</button></div></article>`}).join('');
      const form=$('#settingsForm');$('#settingsName').textContent=row?`Selected: ${row.name}`:'Selected: -';if(row&&!form.dataset.dirty){for(const [k,v] of Object.entries(row.settings||{})){const el=form.elements[k];if(!el)continue;if(el.type==='checkbox')el.checked=!!v;else el.value=v||''}}
      {const L=$('#logs');const atB=L.scrollHeight-L.scrollTop-L.clientHeight<40;L.textContent=(state.logs||[]).join('\n');if(atB)L.scrollTop=L.scrollHeight;}$('#history').textContent=(state.history||[]).map(h=>`${h.timestamp} | ${h.action}/${h.server} | ${h.status} | ${h.details||'-'}`).join('\n');
      const select=$('#serverForm select[name=template_name]');const current=select.value;select.innerHTML=(state.templates||[]).map(t=>`<option>${esc(t)}</option>`).join('');if(current)select.value=current;
    }
    document.addEventListener('click',e=>{const tab=e.target.closest('[data-tab]');if(tab){document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));$('#tab-'+tab.dataset.tab).classList.add('active');return}const card=e.target.closest('.card');if(card)selected=card.dataset.name;const action=e.target.closest('[data-action]')?.dataset.action||e.target.closest('[data-top]')?.dataset.top;if(action){const name=selected;if(!name)return toast(false,'Select a server first');let payload={name,action};if(action==='remove'){if(!confirm(`Remove server '${name}'?`))return;payload.backup_first=confirm('Create backup before delete?')}if(action==='restore'){const backup=prompt('Backup file:',(state.backups||[])[0]||'');if(!backup)return;payload.backup_file=backup;payload.overwrite=confirm('Overwrite existing files before restore?')}api('/api/action',payload);return}if(e.target.id==='refresh')load();render()});
    $('#settingsForm').addEventListener('input',e=>e.currentTarget.dataset.dirty='1');
    $('#settingsForm').addEventListener('submit',e=>{e.preventDefault();if(!selected)return toast(false,'Select a server first');const f=e.currentTarget,d=Object.fromEntries(new FormData(f).entries());for(const n of ['auto_start','auto_update','auto_restart','restart_after_stop'])d[n]=!!f.elements[n].checked;d.name=selected;delete f.dataset.dirty;api('/api/settings',d)});
    $('#cliForm').addEventListener('submit',e=>{e.preventDefault();const d=Object.fromEntries(new FormData(e.currentTarget).entries());api('/api/cli',d)});
    $('#templateForm').addEventListener('submit',e=>{e.preventDefault();const f=e.currentTarget,d=Object.fromEntries(new FormData(f).entries());for(const n of ['auto_start','auto_update','auto_restart','restart_after_stop'])d[n]=!!f.elements[n].checked;api('/api/template',d)});
    $('#serverForm').addEventListener('submit',e=>{e.preventDefault();const f=e.currentTarget,d=Object.fromEntries(new FormData(f).entries());d.start_after_install=!!f.elements.start_after_install.checked;api('/api/server',d)});
    async function pollLogs(){try{const r=await fetch('/api/state',{cache:'no-store'});const j=await r.json();if(j&&j.ok&&j.data){state.logs=j.data.logs||[];state.history=j.data.history||[];const L=$('#logs');const atB=L.scrollHeight-L.scrollTop-L.clientHeight<40;L.textContent=state.logs.join('\n');if(atB)L.scrollTop=L.scrollHeight;$('#history').textContent=state.history.map(h=>`${h.timestamp} | ${h.action}/${h.server} | ${h.status} | ${h.details||'-'}`).join('\n');}}catch(e){}}
    setInterval(()=>{if(!busy)load().catch(err=>toast(false,err.message))},3000);
    setInterval(pollLogs,2000);
    load().catch(err=>toast(false,err.message));
  </script>
</body>
</html>
"""
