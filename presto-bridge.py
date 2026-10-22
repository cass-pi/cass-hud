#!/usr/bin/env python3
"""
Presto Bridge — standalone bidirectional bridge process (issue #11)

Relays JSON between:
  - A local Unix socket (how Cass/the Gateway tells the Presto what to show)
  - The Presto's own USB serial connection (how the firmware is told what to
    render, and how it reports button taps back)

A confirmed button tap on a "choice" message is forwarded to the Gateway via
POST /hooks/wake, the same pattern already proven working for the GitHub
webhook receiver.

Protocol details: see docs/presto-bridge-protocol.md

Serial handling follows the findings in docs/presto-probing-findings.md:
  - Open the port once, keep it open for the life of the process
  - Never attempt an explicit clean close() — it can hang unpredictably on
    this board; just let the process exit and let systemd restart it
  - Reads happen on a dedicated background thread so a stuck/slow board
    can't block writes going the other direction
"""

import os
import sys
import json
import time
import socket
import logging
import threading
import urllib.request
import urllib.error

try:
    import serial
except ImportError:
    serial = None

# ── Config (env-overridable, see presto-bridge.service) ─────────────────────
SOCKET_PATH        = os.environ.get("PRESTO_SOCKET_PATH", "/tmp/presto-bridge.sock")
SERIAL_PORT        = os.environ.get("PRESTO_SERIAL_PORT", "/dev/ttyACM0")
SERIAL_BAUD        = int(os.environ.get("PRESTO_BAUD", "115200"))
GATEWAY_URL        = os.environ.get("GATEWAY_URL", "http://localhost:18789")
HOOKS_TOKEN        = os.environ.get("HOOKS_TOKEN", "")
SERIAL_RECONNECT_S = float(os.environ.get("PRESTO_RECONNECT_S", "3"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("presto-bridge")

# ── Shared state ─────────────────────────────────────────────────────────────
_serial_lock = threading.Lock()
_ser = None  # current pyserial connection, or None while reconnecting

_last_choice_lock = threading.Lock()
_last_choice = None  # {"text": ..., "options": [{"id":..., "label":...}, ...]}


# ── Gateway wake ─────────────────────────────────────────────────────────────
def wake_gateway(text):
    """POST /hooks/wake — same pattern as cass-github-webhook's wake()."""
    body = json.dumps({"text": text, "mode": "now"}).encode()
    req = urllib.request.Request(
        GATEWAY_URL.rstrip("/") + "/hooks/wake",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {HOOKS_TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("wake -> %s: %s", resp.status, text[:120])
    except urllib.error.URLError as e:
        log.error("wake failed: %s", e)


# ── Serial: connection management ───────────────────────────────────────────
def serial_connect_loop():
    """Keep exactly one serial connection open, reconnecting on failure.

    Never explicitly close() the port — per the probing findings this can
    hang unpredictably on this board. If the connection breaks, just drop
    the reference and open a fresh one; the old object is left for the
    garbage collector / process exit to deal with.
    """
    global _ser
    while True:
        if serial is None:
            log.error("pyserial not installed; cannot open %s", SERIAL_PORT)
            time.sleep(SERIAL_RECONNECT_S)
            continue
        try:
            conn = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.2)
            with _serial_lock:
                _ser = conn
            log.info("serial connected: %s @ %d", SERIAL_PORT, SERIAL_BAUD)
            serial_read_loop(conn)
        except (OSError, serial.SerialException if serial else OSError) as e:
            log.warning("serial connect/read failed: %s (retrying in %.1fs)", e, SERIAL_RECONNECT_S)
        with _serial_lock:
            _ser = None
        time.sleep(SERIAL_RECONNECT_S)


def serial_read_loop(conn):
    """Read newline-delimited JSON from the Presto until the connection breaks."""
    buf = b""
    while True:
        chunk = conn.read(4096)
        if chunk:
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                handle_serial_line(line)
        # A zero-length read with timeout>0 just means "nothing waiting yet" —
        # not an error. Only an exception from conn.read() ends this loop
        # (handled by the caller in serial_connect_loop).


def handle_serial_line(line):
    try:
        msg = json.loads(line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        log.warning("serial: bad json: %r", line[:200])
        return

    mtype = msg.get("type")
    if mtype == "choice_response":
        chosen_id = msg.get("id")
        with _last_choice_lock:
            choice = _last_choice
        if choice is None:
            log.warning("choice_response for id=%r but no choice on record", chosen_id)
            return
        label = None
        for opt in choice.get("options", []):
            if opt.get("id") == chosen_id:
                label = opt.get("label")
                break
        label = label or chosen_id
        text = f'Presto: Bear chose "{label}" for: "{choice.get("text", "")}"'
        log.info("choice_response: %s", text)
        wake_gateway(text)
    elif mtype == "hello":
        log.info("serial: Presto firmware said hello")
    else:
        log.info("serial: unhandled message type=%r: %s", mtype, msg)


def serial_write(obj):
    """Write one JSON line to the Presto, if currently connected."""
    data = (json.dumps(obj) + "\n").encode()
    with _serial_lock:
        conn = _ser
        if conn is None:
            log.warning("serial not connected; dropping message: %s", obj)
            return False
        try:
            conn.write(data)
            conn.flush()
            return True
        except OSError as e:
            log.warning("serial write failed: %s", e)
            return False


# ── Local socket: commands from Cass/the Gateway ────────────────────────────
def handle_socket_client(conn):
    try:
        buf = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                reply = handle_socket_line(line)
                conn.sendall((reply + "\n").encode())
    except Exception as e:
        log.warning("socket client error: %s", e)
    finally:
        conn.close()


def handle_socket_line(line):
    global _last_choice
    try:
        cmd = json.loads(line)
    except json.JSONDecodeError:
        return '{"error":"invalid json"}'

    c = cmd.get("cmd", "")
    if c == "fyi":
        ok = serial_write({
            "type": "fyi",
            "text": cmd.get("text", ""),
            "level": cmd.get("level", "info"),
        })
        with _last_choice_lock:
            _last_choice = None  # an FYI replaces any pending choice on screen
        return '{"ok":true}' if ok else '{"ok":false,"error":"serial not connected"}'

    if c == "choice":
        options = cmd.get("options", [])
        payload = {"text": cmd.get("text", ""), "options": options}
        ok = serial_write({"type": "choice", **payload})
        if ok:
            with _last_choice_lock:
                _last_choice = payload
        return '{"ok":true}' if ok else '{"ok":false,"error":"serial not connected"}'

    if c == "clear":
        ok = serial_write({"type": "clear"})
        with _last_choice_lock:
            _last_choice = None
        return '{"ok":true}' if ok else '{"ok":false,"error":"serial not connected"}'

    if c == "ping":
        return '"pong"'

    return '{"error":"unknown command"}'


def socket_server():
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o666)
    srv.listen(5)
    log.info("listening on %s", SOCKET_PATH)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle_socket_client, args=(conn,), daemon=True).start()


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    if serial is None:
        log.error("pyserial is required (pip install pyserial); exiting")
        sys.exit(1)
    if not HOOKS_TOKEN:
        log.warning("HOOKS_TOKEN not set — choice responses will fail to wake the Gateway")

    threading.Thread(target=serial_connect_loop, daemon=True).start()
    socket_server()  # blocks forever


if __name__ == "__main__":
    main()
