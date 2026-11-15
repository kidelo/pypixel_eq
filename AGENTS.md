# AGENTS.md

Guidance for AI agents and contributors working on **pypixel-eq**.

## The project

`pypixel-eq` captures the system audio output, analyzes it into 11 log-spaced
frequency bands (20 Hz – 12 kHz) and streams the result to an iPixel Color LED
panel over BLE. Two transports are supported: `rhythm` (the panel's native
visualizer driven by the 11 levels) and `png` (a rendered 64×16 frame, true
full replace).

The BLE side lives in **[pypixelcolor](https://github.com/lucagoc/pypixelcolor)**.
This project pins the **fork**
[kidelo/pypixelcolor-ext](https://github.com/kidelo/pypixelcolor-ext) as a git
submodule; the fork carries hardware-verified fixes that upstream has not
released (fire-and-forget `set_fun_mode`, `write_with_response`, bounded
pre-connect scan).

## Layout

```
pypixel_eq/
├── src/pypixel_eq/      # the equalizer package (audio, transport, equalizer, gui, CLI)
├── tests/               # hardware-free pytest suite (byte-exact payloads)
├── docs/                # images used by the README
├── pypixelcolor/        # submodule -> kidelo/pypixelcolor-ext @ pinned commit
├── pyproject.toml
└── uv.lock
```

Local scratch directories (`local_test/`, notes, capture dumps) are
**untracked and ignored**. They must never be committed or referenced in
commits, PRs, docstrings or repository docs.

## Repositories and remotes

This repository is **pypixel-eq**, not the library:

| Where | Remote | Repository | Use |
|---|---|---|---|
| this repo | `origin` | `kidelo/pypixel_eq` | publish, all branches |
| `pypixelcolor/` (submodule) | `origin` | `kidelo/pypixelcolor-ext` | fork changes, push allowed |
| `pypixelcolor/` (submodule) | `upstream` | `lucagoc/pypixelcolor` | fetch only, never push |

```bash
git remote -v                                  # this repo: origin only
git -C pypixelcolor remote -v                  # submodule: origin + upstream
```

- `main` here is the integration branch. Once the repository is public it is
  **published**: never rebase, rewrite or force-push it.
- Upstream PRs are opened from the **submodule fork**, not from this repo.

## Setup

```bash
git clone --recurse-submodules https://github.com/kidelo/pypixel_eq.git
cd pypixel_eq
uv sync --extra dev --extra audio
```

`uv sync --extra dev` is enough for the test suite; `audio` adds
`pyaudiowpatch` (Windows WASAPI loopback) and is only needed to run the
equalizer against real audio.

The fork is consumed as a **path dependency**
(`[tool.uv.sources] pypixelcolor = { path = "pypixelcolor" }`), so `uv sync`
builds and installs the exact pinned submodule commit — never a PyPI release.
When the installed code and the submodule drift (missing commands, stale
behaviour), re-run `uv sync`.

## Tests

```bash
uv run pytest -q                 # this repo
uv run pytest pypixelcolor/tests -q   # the fork's own suite
```

The EQ suite is hardware-free and byte-exact: payloads are asserted against
the fork's command builders, so any wire-byte change in the fork fails here.

## Branches and commits

| Prefix | Meaning | Base |
|---|---|---|
| `fix/`, `try/`, `investigate/` | development in this repo | `main` |
| `pr/` | upstream PR branch (in the **submodule**) | `upstream/main` |

- Small, thematic commits; English, imperative mood.
- Never mix unrelated tuning into a feature commit.
- Do not commit `local_test/`, hardware logs, captures or `todo` notes.

### Hardware-verified bugs

A fix for a bug that was reproduced on a panel must carry the evidence in the
commit message: the GOOD and BAD raw notify byte sequences plus a one-line
interpretation, so the change is reviewable and reproducible. Keep the longer
investigation log local and **uncommitted**.

## Upstream PR workflow (in `pypixelcolor/`)

```bash
git -C pypixelcolor fetch upstream
git -C pypixelcolor checkout -B pr/<short-name> upstream/main
# ... commits ...
git -C pypixelcolor push -u origin pr/<short-name>
```

Then open the PR manually in the GitHub UI (no `gh` CLI in this environment):

- Base: `lucagoc/pypixelcolor` → `main`
- Head: `kidelo/pypixelcolor-ext` → `pr/<short-name>`

If upstream moves while the PR is open:

```bash
git -C pypixelcolor fetch upstream
git -C pypixelcolor merge upstream/main --no-edit
git -C pypixelcolor push
```

Rules:

1. Never push to `upstream`.
2. Never branch a `pr/` off the fork's `main` — fork-only commits must not
   leak into an upstream PR.
3. If a commit depends on fork-only code, stop and ask: split it, or move the
   dependency into its own upstream PR first.
4. Keep PR branches linear (no `main` merges inside `pr/`).
5. `git -C pypixelcolor fetch upstream` before any PR work.

After the PR is merged upstream:

```bash
git -C pypixelcolor fetch upstream
git -C pypixelcolor checkout main
git -C pypixelcolor merge upstream/main --no-edit
git -C pypixelcolor push origin main
```

## Protocol references

- When a wire byte layout or protocol value needs a citation, cite a public
  project or the hardware verification — never private notes or captures.
- Nothing about where a byte layout came from belongs in commits, PRs, code
  comments, docstrings or repository docs; cite the reference, not the source
  of the discovery.

Public projects:

- https://github.com/CharlesLennon/coolledux-ble
- https://github.com/Madhat69/pyidotmatrix
- https://github.com/lucagoc/pypixelcolor
- https://github.com/TheMicDiet/chihiros-led-control
- https://github.com/FRSname/iPIXEL-Control
- https://github.com/FRSname/iPIXEL-Control-PC-App
- https://github.com/cagcoach/ha-ipixel-color (iPIXEL-Protocol-Documentation.md)
- https://github.com/sdolphin-JP/ipixel-ctrl (docs/DeviceCommands.md)

Hardware verification is the strongest reference: cite the panel (device byte,
size) and the observed frames.

## Publishing (GitHub)

The repository is published as `kidelo/pypixel_eq`. Checklist for a release
push:

```bash
# 1. Clean tree, green tests
git status --short
uv run pytest -q

# 2. No local scratch or binaries in the index
git ls-files | grep -E "^local_test/|^\.docs/"   # must print nothing
git ls-files | grep -Ei "\.(bin|jar|zip|exe)$"   # must print nothing

# 3. The submodule pin must be a PUSHED commit of the fork
git -C pypixelcolor status --short
git -C pypixelcolor ls-remote origin main
git ls-tree HEAD pypixelcolor

# 4. Smoke-test a fresh clone
git clone --recurse-submodules <url> /tmp/pypixel-eq-clone
cd /tmp/pypixel-eq-clone && uv sync --extra dev && uv run pytest -q

# 5. Push
git push -u origin main
```

Notes:

- The GitHub repository must exist before the first push (`gh` is not
  available, create it in the UI).
- Local-only fork work (uncommitted changes in `pypixelcolor/`) must be pushed
  to `kidelo/pypixelcolor-ext` before its commit can be pinned here.

## Open work

### pypixelcolor (fork / upstream PRs)

- **PC-3** — `requires-python = ">=3.9"` is advertised, but PEP 604 unions in
  `lib/transport/send_plan.py` and `commands/send_text/encoding.py` need 3.10
  without `from __future__ import annotations`.
- **PC-4** — `set_rhythm_mode` requests an ACK the panel never sends; the EQ
  rebuilds a no-ACK plan as a workaround.
- **PC-6** — `Client.connect()` docs promise device info but return `None`.
- **PC-7** — stub generator omits `write_with_response` and the newer
  `send_plan` / `send_payload` methods.
- **PC-8** — expose `connect_scan_timeout`, `ack_timeout`, `is_connected`,
  and a public `DeviceSession`.
- **PC-9** — `cli.py` logs "Exiting..." inside the command loop.
- **PC-10** — `websocket.py` mutates a private `_connected` flag; French error
  string.
- **PC-11** — BLE scanner only matches names containing `LED`.
- **PC-12** — `show_slot` masks `number & 0xFF` without validating.
- **PC-13** — `set_schedule` builds `int(days) | 1` unmasked.
- **PC-14** — text rendering uses a fixed 1000 px canvas; long text is clipped.
- **PC-15** — `send_text` docstring says `save_slot=1`, the default is 0.
- **PC-16** — emoji CDN uses `@latest`; pin a version.
- **PC-17** — `parse_device_info` hardcodes `has_wifi=False`.
- **PC-18** — add `py.typed`; keep version metadata in sync; test more Python
  versions in CI.

### pypixel-eq

- **EQ-15** — `--scan` / `--list-devices` via the client scanner.
- **EQ-17** — separate sender thread/queue if BLE latency stretches the
  analysis period (the loop-period watchdog already reports it).

### Protocol quirks (decide whether to address)

- **Q1** — chunk size is hardcoded to 12 KiB; `ledFrameSize` is 4096 for some
  panels, which can break ACK timing on large images.
- **Q2** — the channel byte defaults to `save_slot` (0); verify the
  "new image, no slot" path.
- **Q3** — no retry: one 8 s timeout per window.
- **Q4** — 244-byte chunking can split the length prefix from the payload.
- **Q5** — no preview (non-committing) stream; the library can only commit.

### Unverified Python-only commands

- **UV-1** — `show_slot` may target a different device family.
- **UV-2** — `set_pixel` has no direct protocol equivalent; the DIY type-5
  paint path is the one verified on hardware.

### Features requested (each as its own submodule PR)

Countdown `[7,0,13,0x80,flag,min,sec]`, sport `[7,0,6,0,mode,speed,decimal]`,
set-weekday `[5,0,18,0x80,weekday]`, text speed `[5,0,3,1,speed]`, exit
`[4,0,1,1]`, password set/verify, 9-byte set-time variant, mecha set-time-date,
camera (type 0), TEM/subzone (type 7), rhythm-chart completeness.

## Closed work

| Item | Result |
|---|---|
| EQ-1 … EQ-14 | Fixed: CLI default vs docs, `dt` from the real device rate, GUI rescheduling, reconnect with backoff, stereo downmix, stream/device cleanup, Hann cache, connect before capture, shared payload/send path, pure `_should_send`, single frame counter, status units, best-effort notes, `bar_color` comment. |
| EQ-16 | Tests for `dt` at 44.1 kHz, downmix, CLI defaults, reconnect, GUI errors, orphan cleanup. |
| PC-1 | `set_pixel` colour validation fixed. |
| PC-2 | `set_clock_mode` accepts 4-digit years, validates the real calendar. |
| PC-5 | `DeviceSession.connect()` can no longer leave a half-open connection. |
| PR-A1 | Dropped — not verifiable on a 64×16 panel. |
| PR-A2 | Fixed: text item count is 16-bit (`pr/fix-text-item-count`). |
| PR-A3 | Dropped — device-type map extension not pursued. |
| PR-A4 | Skipped — ACK error handling needs a known-good whitelist plus send context; GOOD path verified on hardware. |
| FR DIY image | `set_pixels` type-5 builder verified on hardware (`pr/set-pixels-multipoint`). |

## Common commands

```bash
# Test everything
uv run pytest -q
uv run pytest pypixelcolor/tests -q

# Keep the fork in sync with upstream
git -C pypixelcolor fetch upstream
git -C pypixelcolor checkout main
git -C pypixelcolor merge upstream/main --no-edit
git -C pypixelcolor push origin main

# Bump the submodule pin
git -C pypixelcolor fetch origin
git -C pypixelcolor checkout origin/main
git add pypixelcolor
git commit -m "Bump pypixelcolor pin to <short>"
```
