# AGENTS.md — cass-hud

Repo-specific instructions for any agent (Cass or otherwise) working in this
codebase. This is separate from Cass's personal workspace `AGENTS.md`/`MEMORY.md`
— those cover general behavior and cross-repo preferences; this file covers
facts and conventions specific to this project.

## What this repo is

A CRT-style status display for a Raspberry Pi, running as a systemd service
(`cass-display.service`) on the same Pi that hosts the OpenClaw Gateway. It
renders to a physical HDMI display and exposes a Unix socket
(`/tmp/cass-display.sock`) that any local process can push state to.

## Critical: code on disk is not code that's running

`cass-display.py` runs as a long-lived systemd service, not a script invoked
per-call. Editing the file, committing, pushing, or even merging a PR into
`main` has **no effect on the live display** until the service is restarted:

```bash
sudo systemctl restart cass-display.service
```

After *any* change that touches `cass-display.py` or `cass-display-send` lands
on `main` on this Pi, restart the service and confirm a clean start before
reporting the change as live:

```bash
sudo systemctl restart cass-display.service
sleep 2
systemctl status cass-display.service --no-pager | head -8
journalctl -u cass-display.service --since "10 seconds ago" --no-pager
```

Look for no tracebacks and `Active: active (running)`. This has been missed
more than once — treat "merged" and "live" as two separate facts, and verify
the second one explicitly rather than assuming it follows from the first.

## Branching and PRs

- Branch naming: `feat/`, `fix/`, `docs/` prefixes, descriptive slug (e.g.
  `feat/busy-indicator`).
- One logical change per PR, atomic commits within it.
- PRs need review before merge. Don't merge your own PRs — open it and wait.
- After opening a PR via the GitHub API, verify the actual HTTP response
  (expect `201` and a real `html_url`/`number`) before reporting it as open.
  Do the same after any merge/push claim: check the live API state, don't
  assume success from having issued the request.

## Testing a change before opening a PR

There's no automated test suite. Before opening a PR touching the display:

1. Check syntax: `python3 -c "import ast; ast.parse(open('cass-display.py').read())"`
2. Switch the live checkout on this Pi to the feature branch, restart the
   service (see above), and exercise the change for real — e.g. push test
   messages over the socket with `cass-display-send`, or wait out a timing
   threshold (busy state, sleep animation) rather than only reading the code.
3. Confirm no errors in `journalctl -u cass-display.service`.

## Socket protocol

Newline-delimited JSON on `/tmp/cass-display.sock`. Current commands:
`message`, `status`, `busy`, `clear`, `ping`. See `README.md` for the current
field-level protocol — keep both in sync when the protocol changes; a socket
command added in code but missing from `README.md` has happened before.

## Interface changes need a docs pass

A change to the face, socket protocol, or CLI (`cass-display-send`) usually
means `README.md`'s Features/Usage/Socket Protocol sections are now stale.
Check and update them in the same PR as the behavior change, not as a
follow-up someone has to notice and ask for separately.

## Presto bridge (separate process, separate service)

`presto-bridge.py` is a second, independent long-lived process on this Pi —
not part of `cass-display.service`, running as its own systemd unit
(`presto-bridge.service`). Same "code on disk ≠ code running" rule applies:
changes need `sudo systemctl restart presto-bridge.service` before they're
live, verified the same way (`systemctl status`, `journalctl`).

It owns a single long-lived serial connection to the Presto board
(`/dev/ttyACM0`). **Never add code that opens and closes that connection per
message** — see `docs/presto-probing-findings.md` for why that's unreliable
on this specific board (close can hang unpredictably). Open once at process
start, keep it open for the life of the process, let a crash/restart be the
only way the connection gets re-established.

Full protocol (socket commands, serial message shapes, Gateway wake
integration): `docs/presto-bridge-protocol.md`. Keep it in sync with
`presto-bridge.py` and `presto-send` the same way the main socket protocol is
kept in sync with `README.md` — a drift between the two has already
happened once in this repo for the other protocol, don't repeat it here.

The Presto's own firmware (what actually renders to the screen and reports
touches) lives in `presto-firmware/` (issue #12) — this process is only the
Pi-side relay half.

## Presto firmware (`presto-firmware/`, runs on the board, not the Pi)

This is a third kind of "code on disk ≠ code running" case, different from
the other two: it's not a systemd service on the Pi at all. It's MicroPython
that runs directly on the Presto board's own flash, deployed via
`mpremote connect /dev/ttyACM0 fs cp presto-firmware/main.py :main.py`
followed by a board reset — see `presto-firmware/README.md`.

- Use `mpremote`, not hand-rolled `pyserial`/raw file-descriptor code, for
  any interactive testing against the real board. See
  `docs/presto-probing-findings.md` and the `micropython-usb-serial` skill
  for why — a prior probing session lost significant time to a
  self-inflicted serial-protocol bug before switching tools.
- No automated test suite exists for this code (MicroPython running on
  dedicated hardware). Before claiming a change works, actually copy it to
  the board as a throwaway test file and exercise it via `mpremote exec`
  against the real `Presto()`/`PicoGraphics` instance — don't rely on
  `python3 -m py_compile` alone, which only catches syntax errors, not
  calls to APIs that don't exist on this board's actual firmware.
- Remove any throwaway test files from the board's filesystem
  (`mpremote ... fs rm`) once done; confirm via `fs ls` that only the
  intended files remain.
- The exact FYI/choice colour palette is an open question (issue #14/#15
  ask for 2-3 mocked-up options, not a guessed final answer) — don't
  present a palette choice as final in a PR unless Bear has actually
  picked one.
