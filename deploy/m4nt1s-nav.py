#!/usr/bin/env python3
"""
m4nt1s-nav.py - M4NT1S host-side controller.
"""

import argparse
import os
import sys
import time
import threading
import struct
import select
from datetime import datetime

try:
    import serial
    # We do not import serial.tools.list_ports here to avoid the Android error
except ImportError:
    print("pyserial required: pip install pyserial", file=sys.stderr)
    sys.exit(1)

# ---- wire protocol ----
SYNC = 0xAA
TYPE_FRAME       = 0x01
TYPE_COMMAND     = 0x02
TYPE_CREDENTIAL  = 0x03
TYPE_HANDSHAKE   = 0x04
TYPE_STATUS      = 0x05
TYPE_SCAN_AP     = 0x06
TYPE_SCAN_CLIENT = 0x07
CMD_SET_ROTATION = 0x01
CMD_HEARTBEAT    = 0x02
CMD_HOST_NAME    = 0x03
CMD_UNLOCK       = 0x04

HEARTBEAT_INTERVAL_S = 3
STARTUP_RETRY_DELAY_S = 5
STARTUP_RETRY_ATTEMPTS = 2

MAX_LINE = 45

FULL_LABELS = [
    (1 << 0, "OPEN"),
    (1 << 1, "WEP"),
    (1 << 2, "WPA"),
    (1 << 3, "WPA2-PSK"),
    (1 << 4, "WPA2-ENT"),
    (1 << 5, "WPA3-SAE"),
    (1 << 6, "OWE"),
    (1 << 7, "WPS"),
    (1 << 8, "PMF-CAP"),
    (1 << 9, "PMF-REQ"),
]

SHORT_LABELS = {
    (1 << 0): "OPEN",
    (1 << 1): "WEP",
    (1 << 2): "WPA",
    (1 << 3): "WPA2",
    (1 << 4): "WPA2E",
    (1 << 5): "WPA3",
    (1 << 6): "OWE",
    (1 << 7): "WPS",
    (1 << 8): "PMFc",
    (1 << 9): "PMFr",
}

AP_FLAG_HIDDEN    = 1 << 0
AP_FLAG_EVIL_TWIN = 1 << 1
CL_FLAG_ASSOC     = 1 << 0

# ---- global session state ----
running = threading.Event()
running.set()

_first_disconnect_logged = False

def detect_env():
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if ("com.termux" in os.environ.get("PREFIX", "")
            or os.environ.get("TERMUX_VERSION")
            or os.environ.get("ANDROID_ROOT")):
        return "android"
    return "linux"

ENV = detect_env()

def pick_port(arg_port):
    if arg_port:
        return arg_port

    # On Android, we must NEVER call serial.tools.list_ports.comports()
    # It raises "dont know how to enumerate ttys on this system"
    if ENV == "android":
        # Probe the most common CDC-ACM / Gadget nodes
        for node in ("/dev/ttyACM0", "/dev/ttyGS0", "/dev/ttyUSB0"):
            if os.path.exists(node):
                return node
        # If none exist, return the most likely default and let open_serial_with_retry fail gracefully
        return "/dev/ttyACM0"

    # Non-Android platforms (Windows, Linux desktop, macOS)
    try:
        import serial.tools.list_ports
        
        TARGET_VID = 0x4A15
        TARGET_PID = 0x4201
        TARGET_SERIAL = "M4NT1S-0001"
        
        ports = list(serial.tools.list_ports.comports())
        
        # Pass 1: Exact VID/PID match for our custom M4NT1S dongle.
        # This ignores all 30+ ghost ports because they won't have this VID/PID.
        for p in ports:
            if p.vid == TARGET_VID and p.pid == TARGET_PID:
                return p.device

        # Pass 2: Match by our specific serial number string.
        # This is a bulletproof fallback if the driver reports VID/PID weirdly.
        for p in ports:
            if p.serial_number and TARGET_SERIAL in p.serial_number:
                return p.device

        # Pass 3: Fallback to looking at the hardware ID (HWID) string.
        for p in ports: 
            hwid = (p.hwid or "").upper()
            if f"VID_{TARGET_VID:04X}" in hwid and f"PID_{TARGET_PID:04X}" in hwid:
                return p.device

        # Pass 4: Generic Espressif VID for older BROM modes or flashing
        for p in ports:
            if p.vid == 0x303A:
                return p.device

        # Last resort: pick the first available ACM/COM port
        for p in ports:
            if "ttyACM" in p.device or "COM" in p.device:
                return p.device
                
    except Exception:
        pass

    # Last resort fallbacks if the device is completely missing
    if ENV == "windows":
        return "COM3"
    return "/dev/ttyACM0"

def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc

def build_command(cmd_id, args=b""):
    payload = bytes([cmd_id]) + args
    crc = crc16(payload)
    header = bytes([SYNC, TYPE_COMMAND,
                    (len(payload) >> 8) & 0xFF, len(payload) & 0xFF,
                    (crc >> 8) & 0xFF, crc & 0xFF])
    return header + payload

def get_host_name():
    """Best-effort device name. Falls back to the platform identifier."""
    try:
        import socket
        h = socket.gethostname()
        if h and h.lower() != "localhost":
            return h[:32]
    except Exception:
        pass
    try:
        import platform
        n = platform.node()
        if n and n.lower() != "localhost":
            return n[:32]
    except Exception:
        pass
    return ENV.upper()

# ---- output helpers ----
def emit(line=""):
    if len(line) > MAX_LINE:
        line = line[:MAX_LINE]
    print(line, flush=True)

# ---- serial ----
ser = None
ser_lock = threading.Lock()

def signal_disconnect(reason):
    """Log the first disconnect reason and clear the running flag."""
    global _first_disconnect_logged
    if not _first_disconnect_logged:
        _first_disconnect_logged = True
        print(f"[serial] disconnected: {reason}", file=sys.stderr)
    running.clear()

def serial_write(data):
    if not running.is_set():
        return
    with ser_lock:
        try:
            ser.write(data)
        except (serial.SerialException, OSError) as e:
            signal_disconnect(f"write error: {e}")

def open_serial_with_retry(port):
    """Try to open the port. On failure, wait 5 s and try once more.
       Returns the serial object, or None if both attempts fail."""
    for attempt in range(1, STARTUP_RETRY_ATTEMPTS + 1):
        try:
            s = serial.Serial(port, 115200, timeout=0.1)
            return s
        except (FileNotFoundError, serial.SerialException, OSError) as e:
            if attempt < STARTUP_RETRY_ATTEMPTS:
                print(f"[serial] {port} not found (attempt {attempt}/"
                      f"{STARTUP_RETRY_ATTEMPTS}), retrying in "
                      f"{STARTUP_RETRY_DELAY_S}s...", file=sys.stderr)
                time.sleep(STARTUP_RETRY_DELAY_S)
            else:
                print(f"[serial] cannot open {port}: {e}", file=sys.stderr)
    return None

# ---- shared paths ----
def documents_dir():
    if ENV == "windows":
        return os.path.join(os.path.expanduser("~"), "Documents")
    if ENV == "android":
        for p in ("/sdcard/Documents", "/storage/emulated/0/Documents"):
            if os.path.isdir(p):
                return p
        return "/sdcard/Documents"
    return os.path.join(os.path.expanduser("~"), "Documents")

PORTAL_LOG    = os.path.join(documents_dir(), "portal.txt")
HANDSHAKE_LOG = os.path.join(documents_dir(), "handshakes.txt")
SCAN_AP_LOG   = os.path.join(documents_dir(), "scan_aps.csv")
SCAN_CL_LOG   = os.path.join(documents_dir(), "scan_clients.csv")

def security_str(bits):
    labels = [name for bit, name in FULL_LABELS if bits & bit]
    s = "/".join(labels) if labels else "?"
    if len(s) <= 16:
        return s
    labels = [SHORT_LABELS[bit] for bit, _ in FULL_LABELS if bits & bit]
    s = "/".join(labels) if labels else "?"
    if len(s) <= 16:
        return s
    for opt in ("PMFc", "PMFr", "WPS"):
        if opt in labels and len(labels) > 1:
            labels.remove(opt)
            s = "/".join(labels)
            if len(s) <= 16:
                return s
    return s[:16]

def mac_str(mac_bytes):
    return ":".join(f"{b:02X}" for b in mac_bytes)

# ---- scan state ----
scan_aps      = {}
scan_clients  = {}
scan_log_lock = threading.Lock()
scan_started  = False

def ensure_scan_logs():
    global scan_started
    with scan_log_lock:
        if scan_started:
            return
        try:
            os.makedirs(os.path.dirname(SCAN_AP_LOG), exist_ok=True)
            if not os.path.exists(SCAN_AP_LOG):
                with open(SCAN_AP_LOG, "w", encoding="utf-8") as f:
                    f.write("timestamp,bssid,ssid,channel,rssi,rssi_max,security,flags\n")
            if not os.path.exists(SCAN_CL_LOG):
                with open(SCAN_CL_LOG, "w", encoding="utf-8") as f:
                    f.write("timestamp,mac,bssid,probed_ssid,rssi,flags\n")
            scan_started = True
        except Exception as e:
            print(f"[scan] log init failed: {e}", file=sys.stderr)

def handle_scan_ap(payload):
    if len(payload) < 6 + 1:
        return
    p = 0
    bssid = payload[p:p+6]; p += 6
    ssid_len = payload[p]; p += 1
    if len(payload) < p + ssid_len + 5:
        return
    ssid = payload[p:p+ssid_len].decode("utf-8", "replace"); p += ssid_len
    channel  = payload[p]; p += 1
    rssi     = struct.unpack("b", payload[p:p+1])[0]; p += 1
    rssi_max = struct.unpack("b", payload[p:p+1])[0]; p += 1
    sec = payload[p] | (payload[p+1] << 8); p += 2
    flags = payload[p]

    bssid_s = mac_str(bssid)
    is_new  = bssid_s not in scan_aps
    display_name = ssid if ssid else "<hidden>"
    sec_s = security_str(sec)
    scan_aps[bssid_s] = (display_name, channel, rssi, rssi_max, sec_s, flags)

    if is_new:
        emit()
        emit(f"[AP] {display_name}")
        emit(f"{rssi}dBm {bssid_s} ch{channel} {sec_s}")
        ensure_scan_logs()
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with scan_log_lock:
                with open(SCAN_AP_LOG, "a", encoding="utf-8") as f:
                    f.write(f"{ts},{bssid_s},{display_name},{channel},"
                            f"{rssi},{rssi_max},{sec_s},{flags}\n")
        except Exception as e:
            print(f"[scan] AP log write failed: {e}", file=sys.stderr)

def handle_scan_client(payload):
    if len(payload) < 6 + 6 + 1:
        return
    p = 0
    mac_b     = payload[p:p+6];   p += 6
    bssid     = payload[p:p+6];   p += 6
    probed_len = payload[p];      p += 1
    if len(payload) < p + probed_len + 2:
        return
    probed = payload[p:p+probed_len].decode("utf-8", "replace"); p += probed_len
    rssi   = struct.unpack("b", payload[p:p+1])[0]; p += 1
    flags  = payload[p]

    mac_s   = mac_str(mac_b)
    bssid_s = mac_str(bssid)
    is_new  = mac_s not in scan_clients
    scan_clients[mac_s] = (bssid_s, probed, rssi, flags)

    if is_new:
        emit()
        emit(f"[CL] {mac_s}")
        if flags & CL_FLAG_ASSOC:
            emit(f"{rssi}dBm -> {bssid_s}")
        elif probed:
            pdisp = probed if len(probed) <= 20 else probed[:17] + "..."
            emit(f'{rssi}dBm probe "{pdisp}"')
        else:
            emit(f"{rssi}dBm probe")
        ensure_scan_logs()
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with scan_log_lock:
                with open(SCAN_CL_LOG, "a", encoding="utf-8") as f:
                    f.write(f"{ts},{mac_s},{bssid_s},{probed},{rssi},{flags}\n")
        except Exception as e:
            print(f"[scan] client log write failed: {e}", file=sys.stderr)

# ---- status ----
def handle_status(payload):
    text = payload.decode("utf-8", "replace")
    if len(text) > 35:
        text = text[:35]
    emit(f"[dongle] {text}")

# ---- credentials ----
def handle_credential(payload):
    parts = payload.split(b"\x00", 1)
    if len(parts) != 2:
        return
    email    = parts[0].decode("utf-8", "replace")
    password = parts[1].decode("utf-8", "replace")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {email} | {password}"
    emit()
    emit("=" * 45)
    emit("  CAPTIVE PORTAL CREDENTIAL")
    emit(f"  email:    {email}")
    emit(f"  password: {password}")
    emit("=" * 45)
    try:
        os.makedirs(os.path.dirname(PORTAL_LOG), exist_ok=True)
        with open(PORTAL_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[portal] log write failed: {e}", file=sys.stderr)

# ---- handshakes ----
def handle_handshake(payload):
    try:
        text = payload.decode("utf-8", "replace")
        bssid, ssid, path = text.split("|", 2)
    except Exception:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {bssid} | {ssid} | {path}"
    emit()
    emit("=" * 45)
    emit("  HANDSHAKE CAPTURED")
    emit(f"  bssid: {bssid}")
    emit(f"  ssid:  {ssid}")
    emit(f"  file:  {path}")
    emit("=" * 45)
    try:
        os.makedirs(os.path.dirname(HANDSHAKE_LOG), exist_ok=True)
        with open(HANDSHAKE_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[handshake] log write failed: {e}", file=sys.stderr)

# ---- RX loop ----
def rx_observer():
    state = "sync"
    hdr = bytearray()
    payload = bytearray()

    while running.is_set():
        try:
            b = ser.read(1)
        except (serial.SerialException, OSError) as e:
            signal_disconnect(f"read error: {e}")
            return
        if not b:
            continue
        v = b[0]

        if state == "sync":
            if v == SYNC:
                hdr = bytearray([v])
                state = "hdr"
        elif state == "hdr":
            hdr.append(v)
            if len(hdr) == 6:
                length = (hdr[2] << 8) | hdr[3]
                if length > 1024:
                    state = "sync"
                    continue
                payload = bytearray()
                state = "payload"
        elif state == "payload":
            payload.append(v)
            total = (hdr[2] << 8) | hdr[3]
            if len(payload) == total:
                ftype = hdr[1]
                got   = crc16(bytes(payload))
                want  = (hdr[4] << 8) | hdr[5]
                if got == want:
                    if ftype == TYPE_CREDENTIAL:
                        handle_credential(bytes(payload))
                    elif ftype == TYPE_HANDSHAKE:
                        handle_handshake(bytes(payload))
                    elif ftype == TYPE_STATUS:
                        handle_status(bytes(payload))
                    elif ftype == TYPE_SCAN_AP:
                        handle_scan_ap(bytes(payload))
                    elif ftype == TYPE_SCAN_CLIENT:
                        handle_scan_client(bytes(payload))
                state = "sync"

# ---- heartbeat ----
def heartbeat_loop():
    serial_write(build_command(CMD_HEARTBEAT))
    while running.is_set():
        time.sleep(HEARTBEAT_INTERVAL_S)
        if not running.is_set():
            return
        serial_write(build_command(CMD_HEARTBEAT))

def delayed_host_name(host_name_bytes):
    """Send the hostname after USB enumeration is likely complete, and
    once more a couple of seconds later in case the first was dropped."""
    time.sleep(1.0)
    serial_write(build_command(CMD_HOST_NAME, host_name_bytes))
    time.sleep(2.0)
    serial_write(build_command(CMD_HOST_NAME, host_name_bytes))

# ---- input: Windows ----
def input_windows():
    import msvcrt
    while running.is_set():
        if not msvcrt.kbhit():
            time.sleep(0.05)
            continue
        ch = msvcrt.getch()
        if ch == b'\x03':
            return
        if ch in (b'\x00', b'\xe0'):
            ch2 = msvcrt.getch()
            if ch2 == b'H':
                serial_write(b'\x1b[A')
            elif ch2 == b'P':
                serial_write(b'\x1b[B')
            continue
        if ch in (b'\r', b'\n'):
            serial_write(b'\r')
        elif ch == b'\x1b':
            serial_write(b'\x1b[D')
        else:
            serial_write(ch)

# ---- input: POSIX ----
def input_posix():
    import termios, tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while running.is_set():
            r, _, _ = select.select([sys.stdin], [], [], 0.1)
            if not r:
                continue
            ch = sys.stdin.read(1)
            if not ch:
                continue
            if ch == '\x03':
                return
            serial_write(ch.encode('latin-1'))
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

# ---- input: Android evdev ----
EVIOCGRAB     = 0x40044590
KEY_VOLUMEUP  = 115
KEY_VOLUMEDOWN = 114
KEY_F11       = 87
KEY_POWER     = 116
EV_KEY        = 0x01

def eviocgbit(fd, ev_type, nbytes=96):
    import fcntl
    nr  = 0x20 + ev_type
    req = (2 << 30) | (nbytes << 16) | (ord('E') << 8) | nr
    buf = bytearray(nbytes)
    fcntl.ioctl(fd, req, buf, True)
    return buf

def has_key(bits, code):
    byte = code // 8
    bit  = code % 8
    if byte >= len(bits):
        return False
    return (bits[byte] >> bit) & 1

def grab(path):
    import fcntl
    try:
        f = open(path, "rb", buffering=0)
    except Exception:
        return None
    try:
        fcntl.ioctl(f.fileno(), EVIOCGRAB, 1)
        return f
    except Exception:
        f.close()
        return None

def find_power_device():
    import glob
    for p in sorted(glob.glob("/dev/input/event*")):
        try:
            f = open(p, "rb", buffering=0)
        except Exception:
            continue
        try:
            bits = eviocgbit(f.fileno(), EV_KEY)
            if has_key(bits, KEY_POWER):
                f.close()
                return p
        except Exception:
            pass
        f.close()
    return None

def read_events(f, handler):
    fmt = "QQHHi"
    while running.is_set():
        try:
            r, _, _ = select.select([f], [], [], 0.1)
        except Exception:
            return
        if not r:
            continue
        try:
            data = f.read(24)
        except Exception:
            return
        if not data or len(data) < 24:
            return
        try:
            _s, _us, et, ec, ev = struct.unpack(fmt, data)
        except Exception:
            continue
        handler(et, ec, ev)

def input_android():
    vol_dev = grab("/dev/input/event0")
    if vol_dev is None:
        print("[input] failed to grab /dev/input/event0; run as root", file=sys.stderr)
        while running.is_set():
            time.sleep(0.1)
        return

    power_path = find_power_device()
    power_dev  = None
    if power_path and power_path != "/dev/input/event0":
        power_dev = grab(power_path)

    f11_down_at = [None]

    def on_vol(ev_type, code, value):
        if ev_type != EV_KEY:
            return
        if value == 1:
            if code == KEY_VOLUMEUP:
                serial_write(b'\x1b[A')
            elif code == KEY_VOLUMEDOWN:
                serial_write(b'\x1b[B')
            elif code == KEY_F11:
                f11_down_at[0] = time.time()
        elif value == 0:
            if code == KEY_F11 and f11_down_at[0] is not None:
                held = time.time() - f11_down_at[0]
                f11_down_at[0] = None
                if held >= 1.5:
                    serial_write(b'\x1b[D')
                else:
                    serial_write(b'\r')

    def on_power(ev_type, code, value):
        if ev_type != EV_KEY:
            return
        if code == KEY_POWER and value == 1:
            serial_write(b'\x1b[D')

    t1 = threading.Thread(target=read_events, args=(vol_dev, on_vol), daemon=True)
    t1.start()
    if power_dev is not None:
        t2 = threading.Thread(target=read_events, args=(power_dev, on_power), daemon=True)
        t2.start()

    try:
        while running.is_set():
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass

# ---- main ----
def main():
    global ser

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None, help="Explicit port path (e.g., /dev/ttyACM0)")
    ap.add_argument("--rotate", default=None, choices=["portrait", "landscape"])
    args = ap.parse_args()

    port = pick_port(args.port)

    ser = open_serial_with_retry(port)
    if ser is None:
        sys.exit(1)

    # Unlock the device
    serial_write(build_command(CMD_UNLOCK))
    time.sleep(0.1)

    host_name = get_host_name().encode("utf-8", "replace")[:32]

    if args.rotate:
        want_portrait = args.rotate == "portrait"
    else:
        want_portrait = ENV == "android"
    serial_write(build_command(CMD_SET_ROTATION,
                               bytes([1 if want_portrait else 0])))

    rx_thread = threading.Thread(target=rx_observer, daemon=True)
    rx_thread.start()

    hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    hb_thread.start()

    hn_thread = threading.Thread(target=delayed_host_name,
                                 args=(host_name,), daemon=True)
    hn_thread.start()

    print(f"[nav] connected {port} ({ENV}) as {host_name.decode('utf-8', 'replace')}",
          file=sys.stderr)

    try:
        if ENV == "windows":
            input_windows()
        elif ENV == "android":
            input_android()
        else:
            input_posix()
    except KeyboardInterrupt:
        pass

    with ser_lock:
        try:
            ser.close()
        except Exception:
            pass

    if not running.is_set():
        sys.exit(0)

    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)