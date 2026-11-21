# pypixel-eq

![CI](https://github.com/kidelo/pypixel_eq/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.10+blue)
![License](https://img.shields.io/badge/license-MIT-green)

Audio-reactive equalizer for an [iPixel Color](https://ipixel.com.tw/) LED panel. It captures the system audio output, analyzes it into **11 log-spaced
frequency bands** (20 Hz – 12 kHz) and streams the result to the panel over BLE — either as band levels for the panel's own visualizer, or as rendered
64×16 frames. The panel's own brightness can follow the music too (a dim/loudness ramp, or a kick flash on the bass).

![Equalizer bars rendered for a 64×16 panel](docs/banner.png)

## What you get

- 11 log-spaced bands with attack/release smoothing, levels 0..15.
- **Adaptive max** (auto-gain): the recent peak normalizes all bars together, so quiet passages stay visible and loud ones do not pin at the top.
- Two transports: `rhythm` (16 bytes/frame, panel draws the bars) and `png` (rendered frame, true full replace, stable 10 fps).
- **Panel brightness control** over the panel's own hardware brightness: `static` (fixed), `auto` (follows the loudness), or `bass` (kick flashes).
- **Non-blocking writer**: frames are handed to a background thread (newest wins), so BLE latency never stalls the audio analysis.
- Reconnects automatically with backoff; the GUI and analysis keep running.
- Live tkinter view with a 64×16 panel emulation, and a `--test` mode that needs no audio device and no hardware.

## Install

```bash
git clone --recurse-submodules https://github.com/kidelo/pypixel_eq.git
cd pypixel_eq
uv sync --extra dev --extra audio
```

The BLE library is vendored as a **git submodule** at a pinned commit of [kidelo/pypixelcolor-ext](https://github.com/kidelo/pypixelcolor-ext), a fork of [pypixelcolor](https://github.com/lucagoc/pypixelcolor) that carries hardware-verified fixes (fire-and-forget `set_fun_mode`, `write_with_response`, bounded pre-connect scan). It is consumed as a **path dependency**, so `uv` builds and installs the exact pinned commit — not a PyPI release.

Forgot `--recurse-submodules`? Run:

```bash
git submodule update --init --recursive
uv sync --extra dev --extra audio
```

## Quick start

Find your panel's MAC (a BLE scanner works), then:

```bash
uv run python -m pypixel_eq --gui
```

Drive a panel (rhythm transport, the default):

```bash
uv run python -m pypixel_eq -a <MAC>
```

No audio device? Synthetic 11-band stimulus through the same pipeline:

```bash
uv run python -m pypixel_eq --test --gui
```

`--gui` works with or without a panel: with `-a <MAC>` it also drives the panel while showing the emulation; without it, it shows the view only. `--test` checks the render/send path with no hardware.

## Transports

| Transport | What is sent | Notes |
|---|---|---|
| `rhythm` (default) | Only the 11 band levels (0..15) | The panel's **native** music visualizer draws the bars itself. ~16 bytes/update, ~110-byte wire frame. |
| `png` | A rendered 64×16 static-image frame | A TRUE full replace of the panel each update; stable up to 10 fps. |

Both transports are **fire-and-forget** (no panel protocol ACK is awaited): the panel does not reliably ACK these writes, and waiting for one stalls the frame loop. Data frames are prioritized over brightness commands; each uses a single latest-wins slot (queue depth 1, older unsent items are discarded). Under sustained data load a pending brightness command is still sent at most once per `--brightness-interval` seconds (default 1.0) so it cannot starve.

## How it works

```
system audio ─► WASAPI/monitor ─► Hann-windowed FFT (2048) ─► 11 bands
      ─► dB ─► levels 0..15 (attack/release, auto-gain)
      ─► render (png only) ─► payload ─► background writer ─► BLE
```

### Panel brightness (`--brightness-min PCT`, `--brightness-max PCT`, default 100)

Sets the panel's own brightness — a hardware command, not image scaling. Unlike image scaling it also works for the `rhythm` transport. The panel keeps its last display across BLE links, so the value is sent at connect and after every reconnect: run `--brightness-min 30` once and it stays 30 on the next run too (connect blanks the display, then the brightness is raised). 100 is the panel's own hardware default.

| mode (`--brightness-mode`) | behaviour |
|---|---|
| `static` (default) | the panel holds `--brightness-min`. |
| `auto` | the brightness follows the overall loudness: quiet passages fall to `--brightness-min` (-40 dBFS), loud ones rise to `--brightness-max` (-10 dBFS), in between they are linear. The panel only receives a write when the level has moved a full step (~5 %), so quiet passages hold their value and loud ones step up. |
| `bass` | the brightness follows the bass level derived from the selected band levels (default B0..B2, ~20-115 Hz, set with `--brightness-bass-bands`): quiet bass falls toward `--brightness-min`, strong bass rises toward `--brightness-max`, updated only on ~5 % steps. |

```bash
uv run python -m pypixel_eq -a <MAC> --brightness-min 30
uv run python -m pypixel_eq -a <MAC> --brightness-mode auto \
      --brightness-min 20 --brightness-max 100
uv run python -m pypixel_eq -a <MAC> --brightness-mode bass \
      --brightness-min 1 --brightness-max 100
```

### Panel update rate (`--overlap` 0-2, default 0)

The analysis loop re-analyzes a rolling 2048-sample window every `FRAME/(1+overlap)` samples, so each overlap level adds a full analysis pass per second of audio:

| `--overlap` | hop (samples) | panel rate |
|---|---|---|
| 0 | 2048 | 23.4 Hz |
| 1 | 1024 | 46.9 Hz |
| 2 | 683 | 70.3 Hz |

Frequency resolution is preserved (the same 2048-sample FFT every frame), so the bass bands keep their bins at every level. The range is capped at 2: on the panel, higher rates overload it and the high bands get damped or become invisible. `rhythm` is not rate-capped: it sends every analysis frame and runs at the link rate. The `png` image path is the limited one (stable up to 10 fps). `--fps` still thins the writes below the resulting rate.

### Bar shape (`--band-stat`, `--tilt`, `--attack`, `--release`)

- `--band-stat peak|rms` (default `peak`): each band reduced to one dB value. `peak` takes the loudest bin; `rms` takes the band's RMS (power summed over bins × band width).
- `--tilt N` (±6 dB/octave around 1 kHz): fixed gain per band, positive = brighter, negative = warmer.
- `--attack S` / `--release S` (defaults 0.001 / 0.05): seconds for a bar to rise to a louder level / fall back.
- Music is not flat: a balanced mix falls ~5 dB/octave from 100 Hz to 4 kHz. With `--band-stat rms` a music-like spectrum reads roughly level 14 in the bass and 10 in the highs.

| `--tilt` | effect |
|---|---|
| `0` | unchanged (default) |
| `-1` | warmer: +5 dB at 30 Hz, −3 dB at 9 kHz |
| `+1` | brighter: −5 dB at 30 Hz, +3 dB at 9 kHz |
| `±6` | maximum slope, clamped at ±12 dB per band |

The tilt is applied before the auto-gain ceiling, so the ceiling follows the shaped spectrum. With `--no-auto-gain` the rhythm transport's energy fold (the fixed -70/-20 dB span) also shifts with it.

### Adaptive max (auto-gain, on by default)

The bars are normalized to the **recent maximum over all bands**: the max of the loudest band's level over the last `--gain-window` seconds (default 2) is mapped to level 15. Loud passages stop pinning at the top, quiet passages grow, and the loudest band of the window sits at the top while every other bar stays proportionally scaled to it — a quiet band does NOT grow to its own peak (which would flatten the relative balance). The floor stays at -70 dB (a band can fall back to 0); only the ceiling adapts. A silent signal never gets full-height bars (the span is floored at 10 dB).

`--gain-percentile` chooses which value of the window maps to level 15. Default 100 is the maximum, so a single loud transient lifts the scale for the whole window and the other bars drop ("pumping"). Lower values such as 95 ignore the loudest 5 % of frames: one hit no longer squashes every bar, it just clamps at level 15 itself.

```bash
uv run python -m pypixel_eq --gui
uv run python -m pypixel_eq --gui --gain-percentile 95
uv run python -m pypixel_eq --no-auto-gain --gain-window 10
```

## Flags

| Flag | Meaning |
|---|---|
| `-a, --address <MAC>` | BLE address; without it nothing is sent to the panel. |
| `--gui` | Live tkinter view (bands + panel emulation). |
| `--test` | Synthetic 11-band stimulus (no audio device needed). |
| `--transport png\|rhythm` | Device transport (default `rhythm`). |
| `--rhythm-style N` | Rhythm-transport panel preset 0..4 (default 0). |
| `--fps F` | Throttle device writes to F/s (0 = every frame for `rhythm`, the stable 10 fps for `png`; analysis rate unchanged). |
| `--overlap N` | Panel update rate: 23.4 Hz x (1+N), N = 0..2 (default 0 = 23.4 Hz). |
| `--queue N` | Frames that may wait for the BLE write (default 1; oldest unsent frame is dropped). |
| `--no-response` | Write BLE chunks without a link-layer response (experiment: lower latency). |
| **Bars** | |
| `--band-stat peak\|rms` | Per-band value: `peak` (default, loudest bin) or `rms` (total band power: steadier, lifts broadband highs). |
| `--tilt N` | Fixed spectral tilt in dB/octave around 1 kHz (default 0; positive = brighter, negative = warmer). |
| `--attack S` | Seconds for a band to rise to a louder level (default 0.001 = instant). |
| `--release S` | Seconds for a band to fall back (default 0.05; raise it for a slower decay). |
| `--auto-gain` / `--no-auto-gain` | Bars scale to the recent max over all bands (on by default). |
| `--gain-window S` | Auto-gain history: seconds of "recent max" (default 2). |
| `--gain-percentile P` | Which value of the gain window maps to level 15 (default 100 = max; 95 ignores the loudest 5 % of frames). |
| **Brightness** | |
| `--brightness-min PCT` | Panel brightness 0..100 (default 100). Static: the fixed value, set at connect and after every reconnect. Auto: the brightness at a quiet level (-40 dBFS). Bass: the dark base the flash decays back to. |
| `--brightness-max PCT` | Panel brightness 0..100 (default 100). Auto: the ceiling at a loud level (-10 dBFS). Bass: the peak the flash snaps to. Ignored in static mode. |
| `--brightness-mode MODE` | `static` (default, holds the minimum), `auto` (follows the loudness) or `bass` (kick flashes: dark base, flash to the maximum on each hit, decay back). |
| `--bass-sensitivity F` | Kick-detection threshold scale (default 1.0, bass mode only): higher = only strong kicks flash (fewer flashes), lower = softer kicks flash too. |
| `--test` | Synthetic 11-band stimulus (no audio device needed). |

## Tests

Hardware-free and byte-exact (the exact wire bytes are asserted, so any byte change in a fork command shows up here):

```bash
uv run pytest -q                 # this repo
uv run pytest pypixelcolor/tests -q   # the pinned library fork
```

Coverage: band masks partition the FFT bins, a single tone peaks in its own band, quiet → level 0, attack/release timing, stereo downmix, `dt` at 44.1 kHz, reconnect behaviour, GUI error rescheduling, and byte-identical `rhythm` / `png` payloads against the fork's command builders.

## Troubleshooting

| Symptom | Check |
|---|---|
| PanelNotAdvertisingError / panel not found | The panel must not be connected to the phone app; power-cycle it and retry. Check the MAC with a BLE scanner. |
| Connects but nothing changes on the panel | Try `--transport png` once. If that works, the rhythm preset is hiding the effect (`--rhythm-style 0..4`). |
| Panel flickers | Stay at the default `rhythm` transport, or lower `--fps`; the image path is only stable up to 10 fps. |
| Levels barely move | Check the Windows output device (loopback follows the **default** output), disable `--no-auto-gain`, or raise `--overlap`. |
| No audio device | Use `--test` to verify the rest of the pipeline. |
| Installed code lacks a command | `uv sync` — the venv may still hold an older build of the submodule. |

## Limitations

- Windows is the primary target; the Linux monitor path is implemented but less exercised.
- The BLE link, not the analysis, is the bottleneck: `rhythm` tops out around 22 fps with link-layer responses.
- `--no-response` is an experiment; delivery is not confirmed per chunk.
- Single panel, single connection; no multi-device output.
- The level mapping is tuned by ear, not calibrated (see `audio.py`).

## Layout

```
pypixel_eq/
├── pypixelcolor/          # submodule -> kidelo/pypixelcolor-ext @ pinned commit
├── src/pypixel_eq/
│   ├── audio.py           # capture + FFT -> 11 band levels, brightness/kick
│   ├── transport.py       # payload builders + non-blocking Device writer
│   ├── equalizer.py       # worker-thread engine (capture -> render -> send)
│   ├── gui.py             # live tkinter view (bands + panel emulation)
└── __main__.py            # CLI entry point
├── tests/                 # hardware-free pytest tests (byte-exact payloads)
├── docs/                  # README images
└── pyproject.toml
```

## License

[MIT](LICENSE) © 2026 Kidelo.
