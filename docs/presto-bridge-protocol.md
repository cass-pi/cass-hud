# Presto Bridge — JSON protocol (issue #11)

The bridge (`presto-bridge.py`) is a standalone process on the Pi, separate
from `cass-display.service`. It has two JSON interfaces:

1. **Local socket** (`/tmp/presto-bridge.sock`) — how Cass/the Gateway side
   tells the bridge what to show on the Presto. Same newline-delimited JSON
   pattern as the existing `cass-display.sock`.
2. **Serial** (`/dev/ttyACM0`) — how the bridge talks to the Presto's own
   firmware (issue #12). One connection is opened once and kept open for
   the life of the process, per the findings in
   `docs/presto-probing-findings.md` (opening/closing serial per-message is
   unreliable on this board).

The bridge's job is purely to **relay** between these two interfaces, plus
turn confirmed button presses into Gateway wake calls. It holds no on-device
style queue of its own beyond "what was the last thing I sent" (needed to
build a readable wake message when a response comes back).

## Local socket interface (`/tmp/presto-bridge.sock`)

Newline-delimited JSON, one object per line, same shape as
`cass-display-send`/`cass-display.sock` for consistency:

```
{"cmd": "fyi", "text": "...", "level": "info|ok|warn|error"}
{"cmd": "choice", "text": "...", "options": [{"id": "a", "label": "..."}, ...]}
{"cmd": "clear"}
{"cmd": "thermal"}
{"cmd": "ping"}
```

- `fyi` — non-blocking message (issue #14). `level` picks the colour/LED
  treatment; reusing the same `info/ok/warn/error` vocabulary as
  `cass-display-send` rather than inventing a second palette naming
  scheme. **Exact colours/LED values are still TBD per issue #14** — this
  only fixes the *shape* of the message, not the final hex values.
- `choice` — blocking message with buttons (issue #15). `options` is an
  ordered list of `{id, label}`; firmware decides full-text vs. numbered
  rendering based on label length, per issue #15. Blocks indefinitely —
  no timeout, matching the ticket.
- `clear` — cancel whatever is currently showing (FYI or choice) and
  return the Presto to its idle animation. Used both for "next message
  replaces this one" and for an external event resolving a blocking choice
  (e.g. a PR merge webhook) — the device doesn't know or care which case
  it is, same behaviour either way, per issue #15.
- `presence` / `calibrate` — desk presence from the thermal sensor, and re-taking its empty-desk baseline. See "Desk presence" below.
- `thermal` — read the AMG8833 sensor on the Presto right now. Unlike the other commands this one **waits for a reply** (up to 4s) and returns a summary of the frame, not just `{"ok":true}`. See "Thermal camera" below.
- `ping` — health check, replies `"pong"`.

Replies on the same connection: `{"ok": true}` / `{"error": "..."}` /
`"pong"`.

## Serial interface (bridge ↔ Presto firmware, issue #12)

Also newline-delimited JSON, one object per line, sent over the same
`/dev/ttyACM0` connection used for programming the board.

**Bridge → Presto:**

```
{"type": "fyi", "text": "...", "level": "info|ok|warn|error"}
{"type": "choice", "text": "...", "options": [{"id": "a", "label": "..."}]}
{"type": "clear"}
{"type": "thermal_snapshot", "id": "<request id>"}
```

Mirrors the socket commands 1:1 — the bridge mostly just relabels `cmd` to
`type` and passes the rest straight through. Firmware always renders
exactly the latest state it was told about (no on-device queue, per issue
#12) — if a new message arrives before the previous one was acted on, it
simply replaces it.

**Presto → bridge:**

```
{"type": "choice_response", "id": "a"}
{"type": "thermal_snapshot", "id": "<request id>", "ambient": 23.8, "pixels": [64 floats, row-major]}
{"type": "hello"}
```

- `choice_response` — sent once, when a button is tapped for the
  currently-displayed choice. The bridge looks up the choice it last sent
  (kept in memory) to build a human-readable wake message, then forwards
  it to the Gateway via `POST /hooks/wake`.
- `hello` — optional, sent once by firmware on boot/reset so the bridge's
  log shows when the board came back online (e.g. after a firmware
  update). Not required for correctness.
- Idle-screen taps (open the thermal view, or recolour if no sensor is fitted) are **not** reported
  over serial — that behaviour is self-contained on the device and has no
  effect outside it, so there's nothing for the bridge to relay.

## Thermal camera (AMG8833)

An AMG8833 8x8 infrared array on the Presto's Qw/ST port (I2C0, GPIO 40/41, address `0x69`).

- **On the Presto:** tapping the idle screen opens a live heatmap (8x8 upscaled to the 480x480 screen, iron palette, min/max, centre temperature). Tapping again returns to idle. Any message from the Pi (`fyi`, `choice`, `clear`) replaces it like any other state. If no sensor is detected at boot, the idle tap falls back to the original recolour.
- **From the Pi:** `presto-send thermal` (or `{"cmd": "thermal"}` on the socket) sends a `thermal_snapshot` request to the firmware, which reads the sensor and replies. It works in **any** state and does not change what's on screen.
- The bridge returns: `ambient` (the sensor's own thermistor), `min`/`max`/`mean`/`median`, `spread` (max minus median), `warm_pixels` (pixels more than 4C above the median), `hottest` (row/col/temp) and the full 8x8 `pixels` grid in degrees C.
- Errors come back as `{"ok": false, "error": "..."}`: serial not connected, sensor unavailable, or no reply within 4s.

**`warm_pixels` and `spread` are not usable for presence** (they did not separate empty from occupied when measured). Use `presence` below, which compares against a stored baseline.

### Desk presence

Built on the thermal snapshot, comparing a live frame with a stored empty-desk baseline:

- `presto-send calibrate` (socket: `{"cmd": "calibrate"}`) averages 8 snapshots, about half a second apart, and stores them as the baseline. **Run it with the desk empty.** It refuses if the frames are noisy (something moving in view) or if the current frame already looks occupied against the existing baseline; pass `--force` (`"force": true`) after moving the sensor. The baseline lives at `~/.openclaw/state/presto-thermal-baseline.json` (override with `PRESTO_BASELINE_PATH`), deliberately outside the repo since it depends on where the sensor sits. It is written atomically.
- `presto-send presence` (socket: `{"cmd": "presence"}`) takes a fresh snapshot and returns `present`, `pixels_over`, `max_rise`, the thresholds used and `baseline_age_s`. **`present` is true when at least 4 pixels are at least 2 °C above their baseline value.** Errors (no baseline yet, serial not connected, sensor unavailable) come back as `{"ok": false, "error": "..."}`.

**How the thresholds were chosen:** from real frames, sensor mounted away from electronics. With the desk empty no pixel was ever 2 °C over the baseline (biggest single-pixel rise 0.3 to 1.7 °C, including a check three days later). With someone sitting at the desk, 19 to 24 pixels were, with a biggest rise of 7.1 to 7.8 °C.

**Not yet tested:** leaning back or sitting off-centre, other warm objects in view (a mug, a laptop), sun on the desk, and drift over a full day. Treat the thresholds as starting values and recalibrate if the sensor moves.

## Bridge → Gateway

When a `choice_response` arrives, the bridge calls:

```
POST {GATEWAY_URL}/hooks/wake
Authorization: Bearer {HOOKS_TOKEN}
Content-Type: application/json

{"text": "Presto: Bear chose \"<label>\" for: \"<original choice text>\"", "mode": "now"}
```

using the same `/hooks/wake` endpoint and bearer-token pattern already
proven working for the GitHub webhook (`cass-github-webhook`). This wakes
the main session with the result; what to do next (dismiss, re-prompt with
something else, etc.) is Cass's/the Gateway's job, not the bridge's or the
firmware's, per the "no on-device queue, re-prompting is the Pi side's
responsibility" note in issues #12 and #15.

Configuration (env vars, see `presto-bridge.service` + env file):

| Var | Purpose |
|---|---|
| `GATEWAY_URL` | Base URL of the Gateway, e.g. `http://localhost:18789` |
| `HOOKS_TOKEN` | Bearer token for `/hooks/wake` (same token already configured under `hooks.token` in `openclaw.json`) |
| `PRESTO_SERIAL_PORT` | Optional. If unset, the bridge finds the board via `/dev/serial/by-id/usb-MicroPython*` on every connect attempt (falling back to `/dev/ttyACM0`), so a USB re-plug that renumbers `ttyACM*` doesn't break it |
| `PRESTO_BAUD` | Defaults to `115200` |
| `PRESTO_SOCKET_PATH` | Defaults to `/tmp/presto-bridge.sock` |

## Serial connection handling

Per `docs/presto-probing-findings.md`:

- The serial connection is opened **once**, when the service starts, and
  kept open for the life of the process — never opened/closed per
  message.
- Reads happen on a dedicated background thread using a short read
  timeout (non-blocking-ish polling), so a slow/unresponsive board can't
  block writes going the other direction.
- Writes are serialized with a lock so a `choice` and a `clear` arriving
  close together can't interleave their bytes on the wire.
- On shutdown, the bridge does **not** attempt an explicit clean
  `close()` of the serial port — per the probing findings, close can hang
  unpredictably on this board/driver combination even on a healthy
  connection. The process simply exits; systemd's `Restart=on-failure`
  plus a plain process restart reopens the port fresh next time.

## CLI client (`presto-send`, for manual testing)

A thin wrapper around the socket interface, modelled directly on
`cass-display-send`:

```
presto-send fyi "Finished the README update" --level ok
presto-send choice "Option A or B?" a:"Option A" b:"Option B"
presto-send clear
presto-send ping
```
