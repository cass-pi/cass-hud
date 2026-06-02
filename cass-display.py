#!/usr/bin/env python3
"""
Cass Display Daemon — CRT Edition
Phosphor green, scan lines, flicker. Retro-futurist terminal aesthetic.

Protocol (newline-delimited JSON over Unix socket):
  {"cmd": "message", "text": "...", "level": "info|ok|warn|error|dim"}
  {"cmd": "status", "text": "..."}
  {"cmd": "clear"}
  {"cmd": "ping"}
"""

import os, sys, json, socket, threading, time, math, random
import pygame

SOCKET_PATH  = "/tmp/cass-display.sock"
PHYS_W, PHYS_H     = 480, 1280
RENDER_W, RENDER_H = 1280, 480
FPS          = 30
MAX_MESSAGES = 10

FONT_PATH = "/usr/share/fonts/truetype/terminus/TerminusTTF-4.46.0.ttf"

# ── CRT Palette ───────────────────────────────────────────────────────────────
BG          = (4, 8, 4)           # near-black with green tint
PHOSPHOR    = (0, 255, 70)        # classic P1 green
PHOSPHOR_DIM= (0, 140, 40)
PHOSPHOR_HI = (180, 255, 180)     # bright highlights
AMBER       = (255, 176, 0)       # amber accent for warnings
RED_P       = (255, 60, 60)
DARK_GREEN  = (0, 40, 10)
SCANLINE    = (0, 0, 0)

LEVEL_COLORS = {
    "info":  PHOSPHOR,
    "ok":    PHOSPHOR_HI,
    "warn":  AMBER,
    "error": RED_P,
    "dim":   PHOSPHOR_DIM,
}

# ── State ─────────────────────────────────────────────────────────────────────
_lock     = threading.Lock()
_messages = []
_status   = "ONLINE"
_flicker  = 1.0

def push_message(text, level="info"):
    with _lock:
        _messages.append({"text": text, "level": level, "ts": time.time()})
        if len(_messages) > MAX_MESSAGES:
            _messages.pop(0)

def set_status(text):
    global _status
    with _lock:
        _status = text.upper()

def clear_messages():
    with _lock:
        _messages.clear()

# ── Socket ────────────────────────────────────────────────────────────────────
def handle_client(conn):
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
                try:
                    cmd = json.loads(line)
                except json.JSONDecodeError:
                    conn.sendall(b'{"error":"invalid json"}\n')
                    continue
                c = cmd.get("cmd", "")
                if c == "message":
                    push_message(cmd.get("text",""), cmd.get("level","info"))
                    conn.sendall(b'{"ok":true}\n')
                elif c == "status":
                    set_status(cmd.get("text",""))
                    conn.sendall(b'{"ok":true}\n')
                elif c == "clear":
                    clear_messages()
                    conn.sendall(b'{"ok":true}\n')
                elif c == "ping":
                    conn.sendall(b'"pong"\n')
                else:
                    conn.sendall(b'{"error":"unknown command"}\n')
    except Exception:
        pass
    finally:
        conn.close()

def socket_server():
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o666)
    srv.listen(5)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle_client, args=(conn,), daemon=True).start()

# ── CRT effects ───────────────────────────────────────────────────────────────
_scanline_surf = None

def make_scanline_surf(w, h):
    s = pygame.Surface((w, h), pygame.SRCALPHA)
    for y in range(0, h, 3):
        pygame.draw.line(s, (0, 0, 0, 60), (0, y), (w, y))
    return s

def draw_phosphor_text(surface, font, text, x, y, color, glow=True):
    """Render text with a subtle phosphor glow."""
    if glow:
        # Glow layer — dim, offset slightly
        glow_color = tuple(max(0, min(255, int(c * 0.35))) for c in color)
        for dx, dy in [(-1,0),(1,0),(0,-1),(0,1)]:
            gs = font.render(text, True, glow_color)
            surface.blit(gs, (x+dx, y+dy))
    surf = font.render(text, True, color)
    surface.blit(surf, (x, y))
    return surf.get_width()

def draw_orb(surface, cx, cy, t, status):
    """Pulsing CRT-style indicator."""
    pulse = 0.5 + 0.5 * math.sin(t * 2.5)
    color = LEVEL_COLORS.get(status.lower(), PHOSPHOR) if status.lower() in LEVEL_COLORS else PHOSPHOR

    # Outer glow rings
    for i in range(4, 0, -1):
        r = int(6 + i * 5 + pulse * 4)
        a = int((15 + pulse * 10) / i)
        s = pygame.Surface((r*2, r*2), pygame.SRCALPHA)
        pygame.draw.circle(s, (*color, min(255, a * 2)), (r, r), r)
        surface.blit(s, (cx-r, cy-r))

    # Core
    core_r = int(5 + pulse * 3)
    pygame.draw.circle(surface, color, (cx, cy), core_r)
    # Bright centre
    if core_r > 2:
        pygame.draw.circle(surface, PHOSPHOR_HI, (cx, cy), max(1, core_r-3))

def draw_border(surface, w, h, t):
    """Rounded CRT bezel-style border."""
    flicker_alpha = int(180 + 40 * math.sin(t * 0.7))
    color = (*PHOSPHOR_DIM, flicker_alpha)
    # Corner brackets
    size = 16
    thick = 2
    corners = [(0,0),(w,0),(0,h),(w,h)]
    for cx, cy in corners:
        sx = 1 if cx == 0 else -1
        sy = 1 if cy == 0 else -1
        pygame.draw.line(surface, PHOSPHOR_DIM, (cx, cy), (cx + sx*size, cy), thick)
        pygame.draw.line(surface, PHOSPHOR_DIM, (cx, cy), (cx, cy + sy*size), thick)

def render(canvas, fonts, t, RENDER_W=1280, RENDER_H=480):
    global _flicker

    W, H = RENDER_W, RENDER_H
    PAD = 5  # safe margin so nothing clips at screen edges

    # Subtle flicker
    _flicker = 0.97 + 0.03 * (0.5 + 0.5 * math.sin(t * 17.3 + random.uniform(-0.1,0.1)))

    canvas.fill(BG)

    # Subtle vignette bg gradient (darkened edges)
    for i in range(8):
        alpha = 18 - i*2
        s = pygame.Surface((W - i*30, H - i*12), pygame.SRCALPHA)
        s.fill((0, 0, 0, alpha))
        canvas.blit(s, (i*15, i*6))

    fn_large  = fonts["large"]
    fn_medium = fonts["medium"]
    fn_small  = fonts["small"]

    with _lock:
        status_text = _status
        msgs = list(_messages)

    # ── Header ───────────────────────────────────────────────────────────────
    header_h = 52
    pygame.draw.rect(canvas, DARK_GREEN, (0, 0, W, header_h))

    # Top border line with flicker
    line_bright = tuple(int(c * _flicker) for c in PHOSPHOR)
    pygame.draw.line(canvas, line_bright, (0, header_h-1), (W, header_h-1), 1)
    pygame.draw.line(canvas, PHOSPHOR_DIM, (0, header_h), (W, header_h), 1)

    # Orb + name
    orb_x, orb_y = 28, header_h // 2
    draw_orb(canvas, orb_x, orb_y, t, status_text)

    draw_phosphor_text(canvas, fn_large, "CASS", 52, header_h//2 - fn_large.get_height()//2, PHOSPHOR_HI)

    # Status badge
    badge_x = 52 + fn_large.size("CASS")[0] + 20
    badge_color = LEVEL_COLORS.get(status_text.lower(), PHOSPHOR)
    draw_phosphor_text(canvas, fn_small, f"[ {status_text} ]", badge_x,
                       header_h//2 - fn_small.get_height()//2, badge_color, glow=False)

    # Clock (right-aligned)
    clock_str = time.strftime("%H:%M:%S")
    date_str  = time.strftime("%Y-%m-%d")
    draw_phosphor_text(canvas, fn_medium, clock_str,
                             W - fn_medium.size(clock_str)[0] - 20, 6, PHOSPHOR)
    draw_phosphor_text(canvas, fn_small, date_str,
                       W - fn_small.size(date_str)[0] - 20, 32, PHOSPHOR_DIM, glow=False)

    # ── Message log ──────────────────────────────────────────────────────────
    log_y  = header_h + 8
    line_h = 34

    if not msgs:
        draw_phosphor_text(canvas, fn_small, "-- NO MESSAGES --",
                           W//2 - fn_small.size("-- NO MESSAGES --")[0]//2,
                           H//2 - 10, PHOSPHOR_DIM, glow=False)
    else:
        for i, msg in enumerate(reversed(msgs)):
            y = log_y + i * line_h
            if y + line_h > H - 28:
                break

            age    = time.time() - msg["ts"]
            fade   = max(0.25, 1.0 - age / 180)
            color  = LEVEL_COLORS.get(msg["level"], PHOSPHOR)
            faded  = tuple(int(c * fade * _flicker) for c in color)

            # Timestamp
            ts = time.strftime("%H:%M:%S", time.localtime(msg["ts"]))
            draw_phosphor_text(canvas, fn_small, ts, 10, y, PHOSPHOR_DIM, glow=False)

            # Level
            tag = f"{msg['level'].upper():<5}"
            draw_phosphor_text(canvas, fn_small, tag, 90, y, faded, glow=False)

            # Message
            draw_phosphor_text(canvas, fn_small, msg["text"], 160, y, faded, glow=(age < 5))

    # ── Footer ───────────────────────────────────────────────────────────────
    pygame.draw.line(canvas, PHOSPHOR_DIM, (0, H-30), (W, H-30), 1)
    uptime = int(time.time()) % 86400
    h, m = divmod(uptime // 60, 60)
    footer = f"UP {h:02d}:{m:02d}  |  {SOCKET_PATH}"
    draw_phosphor_text(canvas, fn_small, footer, 10, H-26, PHOSPHOR_DIM, glow=False)

    # Scan lines overlay
    global _scanline_surf
    if _scanline_surf:
        canvas.blit(_scanline_surf, (0, 0))

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global _scanline_surf

    os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

    pygame.init()
    pygame.mouse.set_visible(False)

    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    PHYS_W, PHYS_H = screen.get_size()
    # We render landscape and rotate 90 CCW to fill a portrait screen
    RENDER_W, RENDER_H = PHYS_H, PHYS_W
    print(f"Screen: {PHYS_W}x{PHYS_H}, canvas: {RENDER_W}x{RENDER_H}")
    canvas = pygame.Surface((RENDER_W, RENDER_H))
    _scanline_surf = make_scanline_surf(RENDER_W, RENDER_H)

    fonts = {
        "large":  pygame.font.Font(FONT_PATH, 36),
        "medium": pygame.font.Font(FONT_PATH, 28),
        "small":  pygame.font.Font(FONT_PATH, 20),
    }

    threading.Thread(target=socket_server, daemon=True).start()
    push_message("SYSTEM ONLINE. DISPLAY READY.", "ok")
    push_message(f"SOCKET: {SOCKET_PATH}", "dim")

    clock = pygame.time.Clock()
    t = 0.0

    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit(); sys.exit(0)
            if event.type == pygame.KEYDOWN and event.key == pygame.K_q:
                pygame.quit(); sys.exit(0)

        render(canvas, fonts, t, RENDER_W, RENDER_H)
        rotated = pygame.transform.rotate(canvas, 90)
        screen.fill(BG)
        screen.blit(rotated, (0, 0))
        pygame.display.flip()
        t += 1.0 / FPS
        clock.tick(FPS)

if __name__ == "__main__":
    main()
