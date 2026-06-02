# cass-hud

A retro-futurist CRT-style status display for Raspberry Pi, driven by [Cass](https://github.com/ian-antking) — an AI assistant running on OpenClaw.

Renders to a connected HDMI display (tested on a portrait 480×1280 screen) with phosphor green aesthetics, scan lines, and a pulsing status orb.

## Features

- Phosphor green CRT aesthetic with scan line overlay and subtle flicker
- Live clock and date
- Scrolling message log with levels: `info`, `ok`, `warn`, `error`, `dim`
- Messages fade over time
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
./cass-display-send clear
./cass-display-send ping
```

## Socket Protocol

Newline-delimited JSON on `/tmp/cass-display.sock`:

```json
{"cmd": "message", "text": "hello", "level": "info"}
{"cmd": "status", "text": "BUSY"}
{"cmd": "clear"}
{"cmd": "ping"}
```

## Display Rotation

The daemon renders a landscape canvas (1280×480) and rotates it 90° CCW in software to fill a portrait physical screen. No OS-level rotation needed.
