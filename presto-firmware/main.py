"""
Cass Presto firmware (issue #12)

Replaces stock main.py. Responsibilities:
  - Read JSON messages over USB serial from the Pi-side bridge process
    (presto-bridge.py, issue #11)
  - Render exactly the latest state it's been told about: idle animation
    (issue #13), FYI message (issue #14), or blocking choice (issue #15)
  - Detect touch input and report button presses back over serial as JSON

No on-device queue: whatever message arrives most recently simply replaces
whatever was showing before. Re-prompting after a response, and deciding
what to show next, is the Pi side's job (presto-bridge.py / Cass), not
this firmware's — see docs/presto-bridge-protocol.md for the full protocol
this file implements.

Serial reads use non-blocking stdin polling rather than readline(), since
blocking on serial input would also block rendering the idle animation
and polling touch.
"""

import sys
import select
import json
import time
import math
import random

from presto import Presto

# ── Display setup ────────────────────────────────────────────────────────────
presto = Presto(full_res=True, ambient_light=False)
display = presto.display
WIDTH, HEIGHT = display.get_bounds()

BLACK = display.create_pen(0, 0, 0)
WHITE = display.create_pen(255, 255, 255)

display.set_font("bitmap8")

# ── Palette (TBD per issue #14/#15 — Bear to pick final colours) ───────────
# Three rough options sketched here as named constants rather than guessed
# final hex values, per the ticket's explicit "mock up 2-3 options" ask.
# PALETTE selects which one is active; swap this constant to try another.
PALETTE_OPTIONS = {
    "cool": {
        "idle_leds":   (10, 10, 40),
        "fyi_bg":      (10, 30, 60),
        "fyi_text":    (120, 200, 255),
        "fyi_led":     (0, 120, 255),
        "choice_bg":   (50, 15, 60),
        "choice_text": (230, 160, 255),
        "choice_led":  (170, 0, 255),
    },
    "warm": {
        "idle_leds":   (30, 10, 10),
        "fyi_bg":      (10, 45, 20),
        "fyi_text":    (140, 255, 170),
        "fyi_led":     (0, 200, 60),
        "choice_bg":   (60, 30, 0),
        "choice_text": (255, 190, 90),
        "choice_led":  (255, 120, 0),
    },
    "mono": {
        "idle_leds":   (15, 15, 15),
        "fyi_bg":      (20, 20, 20),
        "fyi_text":    (255, 255, 255),
        "fyi_led":     (255, 255, 255),
        "choice_bg":   (40, 0, 0),
        "choice_text": (255, 120, 120),
        "choice_led":  (255, 0, 0),
    },
}
PALETTE = PALETTE_OPTIONS["cool"]

# ── State ────────────────────────────────────────────────────────────────────
# state is one of: "idle", "fyi", "choice"
state = "idle"
fyi_text = ""
fyi_level = "info"
choice_text = ""
choice_options = []  # [{"id":..., "label":...}, ...]

# Idle animation's current tap-to-recolour hue (0.0-1.0), purely decorative
idle_hue = 0.60  # starts as a cool blue, matching the "cool" palette mood

# Touch-button hit regions for the current choice render, rebuilt whenever
# a choice is (re)rendered. List of (x, y, w, h, option_id).
_choice_hit_regions = []

_last_touched = False  # edge-detect: only act on False -> True transitions


# ── Serial I/O ───────────────────────────────────────────────────────────────
_poll = select.poll()
_poll.register(sys.stdin, select.POLLIN)
_rx_buf = ""


def read_serial_messages():
    """Drain any complete JSON lines waiting on stdin. Non-blocking."""
    global _rx_buf
    messages = []
    while _poll.poll(0):
        ch = sys.stdin.read(1)
        if not ch:
            break
        if ch == "\n":
            line = _rx_buf.strip()
            _rx_buf = ""
            if line:
                try:
                    messages.append(json.loads(line))
                except ValueError:
                    pass  # malformed line from the bridge; drop it
        else:
            _rx_buf += ch
    return messages


def send_serial(obj):
    sys.stdout.write(json.dumps(obj) + "\n")


# ── State transitions ────────────────────────────────────────────────────────
def apply_message(msg):
    global state, fyi_text, fyi_level, choice_text, choice_options

    mtype = msg.get("type")
    if mtype == "fyi":
        fyi_text = msg.get("text", "")
        fyi_level = msg.get("level", "info")
        state = "fyi"
    elif mtype == "choice":
        choice_text = msg.get("text", "")
        choice_options = msg.get("options", [])
        state = "choice"
    elif mtype == "clear":
        state = "idle"
    # Unknown types are ignored rather than crashing the render loop.


# ── LEDs ─────────────────────────────────────────────────────────────────────
def set_all_leds(rgb):
    r, g, b = rgb
    for i in range(presto.NUM_LEDS):
        presto.set_led_rgb(i, r, g, b)


# ── Rendering: idle ──────────────────────────────────────────────────────────
def render_idle(tick):
    """Retro-future animation: a slow drifting ring sweep, colour set by
    idle_hue (shifted by tapping, per issue #13). Distinct from the main
    CRT HUD's face — no eyes/mouth here, just an abstract sweep."""
    display.set_pen(BLACK)
    display.clear()

    cx, cy = WIDTH // 2, HEIGHT // 2
    base_pen = display.create_pen_hsv(idle_hue, 0.8, 1.0)
    dim_pen = display.create_pen_hsv(idle_hue, 0.8, 0.35)

    for i in range(3):
        radius = 60 + i * 50 + int(10 * math.sin(tick + i))
        pen = base_pen if i == 0 else dim_pen
        display.set_pen(pen)
        angle = tick * (1 + i * 0.3)
        steps = 48
        for s in range(steps):
            a0 = angle + (s / steps) * 2 * math.pi
            # draw short arcs as a ring of dashes rather than a filled circle
            if s % 3 == 0:
                x = cx + int(radius * math.cos(a0))
                y = cy + int(radius * math.sin(a0))
                display.circle(x, y, 3)

    set_all_leds(PALETTE["idle_leds"])
    presto.update()


def handle_idle_touch():
    """Tapping during idle shifts to a random colour — transient, not saved."""
    global idle_hue
    idle_hue = random.uniform(0.0, 1.0)


# ── Rendering: FYI ───────────────────────────────────────────────────────────
def render_fyi():
    bg = display.create_pen(*PALETTE["fyi_bg"])
    fg = display.create_pen(*PALETTE["fyi_text"])

    display.set_pen(bg)
    display.clear()

    display.set_pen(fg)
    vector_text_block(fyi_text, WIDTH, HEIGHT, scale=3)

    set_all_leds(PALETTE["fyi_led"])
    presto.update()


# ── Rendering: choice ────────────────────────────────────────────────────────
BUTTON_H = 70
BUTTON_MARGIN = 10
SHORT_LABEL_MAX = 14  # labels at/under this render as full-text buttons


def render_choice():
    global _choice_hit_regions
    _choice_hit_regions = []

    bg = display.create_pen(*PALETTE["choice_bg"])
    fg = display.create_pen(*PALETTE["choice_text"])
    btn_bg = display.create_pen(60, 60, 60)
    btn_fg = WHITE

    display.set_pen(bg)
    display.clear()

    display.set_pen(fg)
    use_numbered = any(len(opt.get("label", "")) > SHORT_LABEL_MAX for opt in choice_options)

    if use_numbered:
        # Prompt text + numbered descriptions at the top
        vector_text_block(choice_text, WIDTH, HEIGHT // 2, scale=2)
        y = HEIGHT // 2 + 10
        for i, opt in enumerate(choice_options):
            display.set_pen(fg)
            display.text("%d. %s" % (i + 1, opt.get("label", "")), 10, y, WIDTH - 20, 2)
            y += 26
    else:
        # Prompt text at top, short full-text buttons below
        vector_text_block(choice_text, WIDTH, HEIGHT // 3, scale=2)

    # Button row(s) along the bottom, one per option, numbered either way
    n = max(len(choice_options), 1)
    btn_w = (WIDTH - BUTTON_MARGIN * (n + 1)) // n
    btn_y = HEIGHT - BUTTON_H - BUTTON_MARGIN

    for i, opt in enumerate(choice_options):
        btn_x = BUTTON_MARGIN + i * (btn_w + BUTTON_MARGIN)
        display.set_pen(btn_bg)
        display.rectangle(btn_x, btn_y, btn_w, BUTTON_H)
        display.set_pen(btn_fg)
        label = opt.get("label", "") if not use_numbered else str(i + 1)
        if use_numbered:
            label = str(i + 1)
        tw = display.measure_text(label, 2)
        display.text(label, btn_x + (btn_w - tw) // 2, btn_y + (BUTTON_H - 16) // 2, btn_w, 2)
        _choice_hit_regions.append((btn_x, btn_y, btn_w, BUTTON_H, opt.get("id")))

    set_all_leds(PALETTE["choice_led"])
    presto.update()


def handle_choice_touch(x, y):
    for (bx, by, bw, bh, opt_id) in _choice_hit_regions:
        if bx <= x <= bx + bw and by <= y <= by + bh:
            send_serial({"type": "choice_response", "id": opt_id})
            return


# ── Shared text helper ───────────────────────────────────────────────────────
def vector_text_block(text, max_w, max_h, scale=2):
    """Simple word-wrapped text block, top-aligned, using the bitmap font
    and built-in wordwrap rather than a bespoke layout engine."""
    display.text(text, 10, 10, max_w - 20, scale)


# ── Main loop ────────────────────────────────────────────────────────────────
def main():
    send_serial({"type": "hello"})
    global _last_touched

    while True:
        for msg in read_serial_messages():
            apply_message(msg)

        presto.touch_poll()
        touched = presto.touch_a.touched
        tx, ty = presto.touch_a.x, presto.touch_a.y

        # Edge-detect: only act on a fresh press, not a held finger.
        if touched and not _last_touched:
            if state == "idle":
                handle_idle_touch()
            elif state == "choice":
                handle_choice_touch(tx, ty)
            # FYI messages have no touch response (non-blocking, issue #14).
        _last_touched = touched

        tick = time.ticks_ms() / 300.0

        if state == "idle":
            render_idle(tick)
        elif state == "fyi":
            render_fyi()
        elif state == "choice":
            render_choice()

        time.sleep(0.03)


if __name__ == "__main__":
    main()
