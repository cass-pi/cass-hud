# Pimoroni Presto — hardware probing findings (issue #10)

Findings from probing the Presto over its USB serial connection
(`/dev/ttyACM0`), ahead of building the bridge process and firmware.

## Device identity

Confirmed via `lsusb`/`udevadm`:

- USB CDC-ACM device, vendor `2e8a` (MicroPython), product `0005`
- Driver: `cdc_acm` (standard Linux kernel driver, nothing custom)
- MicroPython `1.25.0` (preview), `presto` build, running on RP2350
- ~8MB free heap at idle (`gc.mem_free()`)
- Stock filesystem: `main.py` (launcher) + `cherry-hq.af` (packaged stock app)

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

## Still unconfirmed (needs follow-up during firmware work, issue #12)

Given the serial reliability issue consumed most of this probing session,
the following from the original ticket scope were not fully nailed down
and should be confirmed early during firmware work rather than assumed:

- Exact screen resolution and the specific `presto`/`picographics` drawing
  API calls (expected ~480×480 based on public Presto specs, not directly
  confirmed via a stable REPL session here)
- Touch API shape (coordinate reporting, polling vs. callback-based)
- Back-panel LED control API and addressable granularity
- Confirmed safe method for deploying new code to the board (e.g.
  overwriting `main.py`) given the close-on-exit unreliability — likely
  needs a tool/workflow that tolerates a slow or failed clean disconnect
  rather than assuming a quick file copy always completes cleanly
