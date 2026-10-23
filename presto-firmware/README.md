# Presto firmware (issue #12)

MicroPython firmware that runs **on the Presto board itself**, replacing
the stock `main.py`. This is the other half of the pair started in issue
#11 — `presto-bridge.py` runs on the Pi and relays JSON over USB serial;
this is the code on the Presto that actually renders the screen and
reports touches.

Full JSON protocol (what this firmware reads from/writes to serial):
`../docs/presto-bridge-protocol.md`.

## What it does

- Reads newline-delimited JSON from stdin (the USB serial link to the Pi
  bridge), non-blocking via `select.poll()` so it never stalls rendering
  or touch polling while waiting for a message.
- Renders exactly one of three states, whichever was most recently
  requested — **no on-device queue**, per issue #12's explicit scope:
  - `idle` — a retro-future ring-sweep animation (issue #13). Tapping
    during idle opens the thermal view (below). If no sensor is fitted the
    tap recolours the animation instead, as originally specced. The colour
    is purely decorative, not saved, and any real incoming message
    overrides it.
  - `thermal` — live AMG8833 heatmap. Tap to return to idle (which also picks
    a fresh idle colour). Pi messages replace it like any other state. The
    Pi can also request a one-off sensor reading in any state with a
    `thermal_snapshot` message; see `../docs/presto-bridge-protocol.md`.
  - `fyi` — non-blocking message (issue #14). Sits on screen until
    replaced by the next message; no dismiss action.
  - `choice` — blocking message with buttons (issue #15). Short option
    labels (≤14 chars) render as full-text buttons; longer ones render as
    numbered on-screen descriptions with matching numbered buttons.
- On a confirmed button tap for a `choice`, writes a `choice_response`
  JSON line back over serial. The Pi-side bridge matches it against the
  choice it last sent and wakes the Gateway.
- A `clear` message (sent by the bridge both for "replaced by the next
  message" and "resolved by an external event, e.g. a PR merge") always
  just returns to idle — the firmware doesn't need to know or care which
  case it is, per issue #15.

## Colour palette — TBD, placeholder options included

Issue #14/#15 call for the exact FYI/choice colours and LED treatment to
be **mocked up as 2-3 options for Bear to react to**, not guessed as final
hex values. `main.py` defines three named palettes (`cool`, `warm`,
`mono`) in `PALETTE_OPTIONS` and currently activates `"cool"` via the
`PALETTE` constant. Flash with a different one active, or build a way to
switch at runtime, to get a real reaction before locking in a final
palette — **this PR does not claim to have picked the final colours**,
only to have built the mechanism so a choice can be made and swapped in
with a one-line change.

## Testing

No physical automated test harness exists for on-device MicroPython code,
so this was tested directly against the real board via `mpremote`
(see `docs/presto-probing-findings.md` / the `micropython-usb-serial`
skill for why `mpremote` rather than hand-rolled serial code):

- Copied `main.py` to the board as a throwaway test file and ran it
  through 60 iterations of the idle render loop with no exceptions.
- Ran each state's render function (`render_idle`, `render_fyi`,
  `render_choice` with both short-label and long-label/numbered option
  sets) directly against the real `PicoGraphics` instance — all completed
  without exceptions, confirming the actual API calls used
  (`display.text`, `display.rectangle`, `display.circle`,
  `display.create_pen_hsv`, `presto.set_led_rgb`, `presto.update()`, etc.)
  are valid against this board's real firmware, not just plausible-looking
  code.
- Verified `handle_choice_touch` correctly resolves a simulated tap
  coordinate to the right button's option id and produces well-formed
  `choice_response` JSON.
- Verified `apply_message({"type": "clear"})` returns state to `"idle"`
  from both `fyi` and `choice`.
- All test files were removed from the board's filesystem afterwards —
  confirmed via `mpremote ... fs ls` that only the original `main.py` and
  `cherry-hq.af` remain.

## Deploying

```bash
mpremote connect /dev/ttyACM0 fs cp presto-firmware/main.py :main.py
```

The board picks up the new `main.py` on its next reset/power-cycle (hard
reset, or `mpremote connect /dev/ttyACM0 reset`).

**Back up first if you've changed anything on the board** — this
overwrites `main.py` directly, same caveat as flashing a
`-with-filesystem` firmware build.
