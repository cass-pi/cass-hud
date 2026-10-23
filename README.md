# cass-hud

A retro-futurist CRT-style status display for Raspberry Pi, driven by [Cass](https://github.com/ian-antking) — an AI assistant running on OpenClaw.

Renders to a connected HDMI display (tested on a portrait 480×1280 screen) with phosphor green aesthetics, scan lines, and a pulsing status orb.

## Features

- Phosphor green CRT aesthetic with scan line overlay and subtle flicker
- Live clock and date
- Animated face — idle blink cycle, mood colour and a brief talking-mouth animation driven by the most recent message (`info`, `ok`, `warn`, `error`, `dim`), with a one-line caption underneath
- Busy/working indicator — steady cyan colour with a chasing-dot mouth animation while a task is in progress, distinct from the mood-flash a message produces
- Status indicator with animated breathing orb
- Unix socket interface — push messages from any process or script
- Systemd service for boot-time startup

## Requirements

- Raspberry Pi (tested on Pi 5)
- Python 3
- `python3-pygame` (`sudo apt install python3-pygame`)
- `fonts-terminus` (`sudo apt install fonts-terminus`)
- `libegl1 libegl-mesa0` (`sudo apt install libegl1 libegl-mesa0`)

## Usage

Start the daemon:
```bash
python3 cass-display.py
```

Or install as a systemd service (see `cass-display.service`).

Send messages:
```bash
./cass-display-send message "Running tests..." --level info
./cass-display-send message "All tests passed" --level ok
./cass-display-send message "Build failed" --level error
./cass-display-send status "busy"
./cass-display-send busy start "Reading issue #7..."
./cass-display-send busy stop
./cass-display-send clear
./cass-display-send ping
```

The `busy` command shows a persistent 'working' state on the face — useful
for wrapping a task so the HUD reflects the full duration of the work, not
just a brief reaction when a message is pushed. `busy start` takes an
optional progress-text argument shown as the caption; `busy stop` clears it
back to idle (or to the next message's mood, if one is pushed right after).

## Socket Protocol

Newline-delimited JSON on `/tmp/cass-display.sock`:

```json
{"cmd": "message", "text": "hello", "level": "info"}
{"cmd": "status", "text": "BUSY"}
{"cmd": "busy", "active": true, "text": "Working on it..."}
{"cmd": "clear"}
{"cmd": "ping"}
```

## K3s Cluster Monitoring

Cass HUD includes a live sidebar showing the health of a k3s cluster (named **STRONKBEAR** in the default config).

### How it works

A poller (`k3s-poller.py`) queries the Kubernetes metrics API every 30 seconds and pushes node data to the display socket. Each node is shown as a row with two hexagon indicators:

- **MEM** — memory usage (% of allocatable)
- **CPU** — CPU usage (% of allocatable)

Hexagons fill from bottom to top. Colour indicates pressure:
- **Green** — < 60%
- **Amber** — 60–80%
- **Red** — > 80%

If a node is `NotReady`, its name turns red and glows.

### Setup

1. Create a service account in your cluster and obtain a token:
   ```bash
   kubectl create serviceaccount cass-hud -n kube-system
   kubectl create clusterrolebinding cass-hud \
     --clusterrole=view \
     --serviceaccount=kube-system:cass-hud
   ```

2. Store the token and config:
   ```bash
   sudo mkdir -p /etc/cass-hud
   sudo nano /etc/cass-hud/k3s-token   # paste your token
   sudo chmod 600 /etc/cass-hud/k3s-token
   ```

   Create `/etc/cass-hud/k3s.conf`:
   ```ini
   [k3s]
   token_file = /etc/cass-hud/k3s-token
   api_server = https://<your-api-server>:6443
   ```

3. Install the systemd timer:
   ```bash
   sudo cp k3s-poller.service k3s-poller.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now k3s-poller.timer
   ```

### Requirements

- `python3-yaml` (`sudo apt install python3-yaml`)
- A k3s/Kubernetes cluster with metrics-server enabled
- A service account token with `view` access

## Display Rotation

The daemon renders a landscape canvas (1280×480) and rotates it 90° CCW in software to fill a portrait physical screen. No OS-level rotation needed.

## Presto Bridge

`presto-bridge.py` is a standalone process (separate from `cass-display.service`)
that relays JSON between Cass/the Gateway and a Pimoroni Presto board connected
over USB serial. It is not a Presto firmware image itself — the Presto-side
code lives in `presto-firmware/` (issue #12); this is the Pi-side half of that
pair.

- Listens on a local Unix socket (`/tmp/presto-bridge.sock`) for `fyi`
  (non-blocking) and `choice` (blocking, button-driven) messages to show on
  the Presto, plus `clear` to dismiss whatever's currently showing.
- Holds one long-lived serial connection to the Presto (never opens/closes
  per message — see `docs/presto-probing-findings.md` for why that's
  unreliable on this board) and relays those commands over it.
- Reads button-tap responses back from the Presto over the same connection
  and forwards confirmed choices to the Gateway via `POST /hooks/wake`, the
  same pattern already proven working for the GitHub webhook receiver.

Full protocol and design rationale: `docs/presto-bridge-protocol.md`.

### Requirements

- `python3-serial` / `pyserial` (`pip install pyserial` or
  `sudo apt install python3-serial`)
- A Presto flashed with firmware that speaks this protocol (issue #12)

### Setup

1. Copy `presto-bridge.env.example` to
   `/home/ian/.openclaw/credentials/presto-bridge.env` and fill in
   `HOOKS_TOKEN` (same value as `hooks.token` in `openclaw.json`).
2. Install the systemd service:
   ```bash
   sudo cp presto-bridge.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now presto-bridge.service
   ```

### Manual testing

```bash
./presto-send fyi "Finished the README update" --level ok
./presto-send choice "Option A or B?" a:"Option A" b:"Option B"
./presto-send clear
./presto-send thermal
./presto-send ping
```

### Presto firmware

The code that actually runs on the Presto board (renders the screen,
detects touch): `presto-firmware/`. See `presto-firmware/README.md` for
what it does, the current palette status (placeholder, pending Bear's
colour choice per issue #14/#15), and how it was tested.
