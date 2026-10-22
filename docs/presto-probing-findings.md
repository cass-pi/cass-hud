# Pimoroni Presto — hardware probing findings (issue #10)

Findings from probing the Presto over its USB serial connection
(`/dev/ttyACM0`), ahead of building the bridge process and firmware.

## Device identity

Confirmed via `lsusb`/`udevadm`:

- USB CDC-ACM device, vendor `2e8a` (MicroPython), product `0005`
- Driver: `cdc_acm` (standard Linux kernel driver, nothing custom)
- Originally shipped with MicroPython `1.25.0` (preview); updated during
  this probing session to the official `pimoroni/presto` firmware release
  `v2.0.0` (MicroPython `1.29.0`, `_build='presto'`), which includes fixes
  relevant to this board: a permanent PIO stall in frame transfer, FT6236
  (touch controller) I2C failures no longer wedging the frame loop, and a
  core1 stack size fix. Flashed via the standard bootloader procedure (BOOT
  + RESET, copy `.uf2` to the mass-storage drive that appears), plain
  variant (no bundled examples) since this board will run custom firmware.
- ~8MB free heap at idle (`gc.mem_free()`)
- Stock filesystem at the time of probing: `main.py` (launcher) +
  `cherry-hq.af` (packaged stock app); both still present after the
  firmware update, since the plain `.uf2` variant does not touch the
  filesystem

## REPL is reachable, but with a serious caveat

Interactive REPL access over serial works — sending a Ctrl-C interrupt and
reading the response produces the expected MicroPython banner and prompt.
However, **closing the serial connection after opening it is unreliable and
can hang indefinitely**, confirmed at multiple levels:

- Hangs with `pyserial`'s `Serial.close()`
- Hangs with raw `os.close(fd)` on a file descriptor opened via `os.open()`
  directly, bypassing pyserial entirely
- Hangs even when relying on process exit to implicitly close the fd (no
  explicit `close()` call at all)
- A process blocked this way sits in kernel D-state (uninterruptible I/O
  wait) and cannot be killed with `kill -9` until it resolves on its own;
  it is not a permanent wedge, but resolution time is unpredictable
- The hang is intermittent rather than fully deterministic: some
  open/write/read/close cycles complete quickly, others stall for a long
  time with no observable difference in the command sent
- `stty` run against the device from a separate shell can also hang,
  confirming this is a kernel/driver-level condition tied to the device
  itself, not specific to Python, pyserial, or any one process

This was also the cause of an earlier stalled probing attempt in this
session, before the cause was isolated.

## Likely cause (not fully confirmed)

Not root-caused with certainty, but consistent with a known class of
CDC-ACM issue: the RP2350's USB stack (running MicroPython's own firmware,
not a dedicated USB-serial chip) is cooperatively servicing the CDC
control/data transfers from within its own event loop. If the board is
mid-way through other work (running `main.py`, driving the display, etc.)
when a host-side close/control-line change arrives, it can be slow or
inconsistent to respond, and the Linux host blocks waiting for that
acknowledgment.

This is a different failure mode than a simple "device not present" or
"permission denied" error — the device stays enumerated and reachable the
whole time (confirmed via `lsusb` during a stuck close), it just doesn't
release the connection promptly.

## Implications for the bridge process (issue #11)

This is the most important takeaway for the next pieces of work:

- **Never rely on a short-lived process that opens, does one exchange, and
  closes the serial connection per message.** The close step is exactly
  the unreliable one. A request/response-per-connection model (open →
  write → read → close, repeated) will intermittently hang the whole
  bridge process.
- **Open the serial connection once, keep it open for the life of the
  bridge service**, and do all reads/writes against that single
  long-lived file descriptor instead. This avoids hitting the unreliable
  close path on every message.
- Use non-blocking I/O (or a dedicated reader thread with short poll
  timeouts) rather than blocking reads with long timeouts, since even
  write/read calls occasionally took longer than expected.
- Design the bridge service to tolerate the serial connection becoming
  unresponsive without crashing or blocking the rest of the Gateway-facing
  side — e.g. do serial I/O in a separate thread/process so a stuck
  read/write doesn't block JSON delivery to/from the Gateway.
- If reliability remains a problem once the bridge is built, consider
  whether a hardware reset line (toggling DTR, or power-cycling via a
  controllable USB port) is needed as a recovery mechanism, rather than
  trying to fix this purely in software.

## Screen resolution and drawing API

Confirmed via `mpremote` against the `presto` module's own source
(`modules/py_frozen/presto.py` in the `pimoroni/presto` repo) and live
probing, in a follow-up session after the firmware update:

- The board supports **two display modes**, selected at `Presto()`
  construction time via `full_res`:
  - Default (`full_res=False`): `240×240` logical resolution, 2 rendering
    layers, `PEN_RGB565`. Faster to draw since it's a quarter of the pixel
    count, at the cost of a lower-detail image upscaled to the physical
    panel.
  - `Presto(full_res=True)`: `480×480`, matching the panel's true physical
    resolution, single layer by default.
- Drawing goes through **PicoGraphics** (`presto.display`, an instance of
  `picographics.PicoGraphics`), not a bespoke Presto-only API. Standard
  PicoGraphics calls (`set_pen`, `pixel`, `rectangle`, `text`, etc.) apply.
- `presto.update()` pushes the framebuffer to the panel and polls touch in
  one call; `presto.partial_update(x, y, w, h)` updates only a region,
  useful for not redrawing the whole screen on every touch/message change.
- Recommend `full_res=True` for this project: button/text sharpness will
  matter more than redraw speed for a notification/choice display that
  updates occasionally, not an animation-heavy game.

## Touch API

Confirmed via source and live probing:

- Touch is **polling-based**, not callback/interrupt-driven. Call
  `presto.touch_poll()` (or `presto.update()`, which polls touch as part of
  its own work) to refresh touch state, then read `presto.touch_a` /
  `presto.touch_b` (up to 2-point multi-touch, via the FT6236 controller).
- Each is a `namedtuple(x, y, touched)` in screen coordinates, e.g.
  `touch(x=240, y=240, touched=False)` at idle (centre of the 480×480
  full-res screen — the controller reports a resting/last position even
  when `touched` is `False`, so always check `touched` rather than
  inferring from position).
- `presto.touch_delta` gives `(distance, angle)` between the two touch
  points, useful for pinch/rotate gestures — not needed for this project's
  button-tap use case, but available.
- For the firmware work (issue #12): call `touch_poll()` on a regular
  timer/loop alongside rendering, check `touch_a.touched` transitioning
  False→True to detect a tap (not just sampling `touched` once), and map
  the `(x, y)` against whatever button regions are currently drawn.

## LED API

Confirmed via source:

- `presto.NUM_LEDS == 7` — 7 individually addressable back-panel LEDs, not
  a single combined colour as originally guessed in the ticket.
- `presto.set_led_rgb(i, r, g, b)` and `presto.set_led_hsv(i, h, s, v)` set
  one LED (`i` is the index, `0..6`) at a time — call in a loop to set all
  7 to the same colour, or drive them independently for a chase/gradient
  effect.
- `presto.auto_ambient_leds(enable)` exists as an automatic mode (reacts to
  ambient light via a sensor) — not relevant to this project's use of the
  LEDs as a status indicator, should stay disabled so manual colour control
  isn't overridden.

## Safe code deployment method

Confirmed working, replacing the earlier raw-serial approach entirely:

- Install `mpremote` (the official MicroPython remote-control tool) via
  `pip install mpremote` (needs `python3-pip` installed first via apt on a
  minimal Debian image, then `pip install mpremote --break-system-packages`
  or a venv).
- List files: `mpremote connect /dev/ttyACM0 fs ls`
- Copy a file onto the board: `mpremote connect /dev/ttyACM0 fs cp
  local_file.py :remote_name.py`
- Run code directly without saving it: `mpremote connect /dev/ttyACM0 exec
  "<code>"`
- Tested a full write → list → execute → remove cycle against a throwaway
  test file; completed cleanly with no hangs.
- `mpremote` handles the REPL interrupt/prompt-sync/connection-close
  sequencing internally and did not reproduce the close-hang issue in any
  of this session's testing — use it (or a bridge process built on the
  same underlying approach it uses) rather than hand-rolled `pyserial`/raw
  file-descriptor code for issue #11's bridge process.

## Note on the earlier garbled-output problem

The first probing session (before the firmware update) hit two separate
problems that looked related but weren't:

1. The close-hang issue documented above (real, device/driver-level,
   partly improved by a physical board restart).
2. A **separate bug in this session's own hand-rolled serial probing
   script** — writes were being split across multiple `os.write()` calls
   without properly waiting for the board to finish processing each one,
   so commands arrived corrupted (e.g. `print(1+1)` landing as `prn1+1`)
   and the REPL just echoed back endless `...` continuation prompts in
   response. This was mistaken for a possible firmware quirk at the time.

The firmware update to `v2.0.0` was still a reasonable and worthwhile
change on its own merits (real relevant fixes, see above), but it did not
fix problem 2, because problem 2 was never on the firmware side. Switching
to `mpremote` resolved it immediately — a reminder to prefer purpose-built
tooling over hand-rolled protocol handling when something that should be
simple (sending one command, reading one response) turns out not to be.
