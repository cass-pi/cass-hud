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
