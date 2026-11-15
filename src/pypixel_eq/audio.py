# -*- coding: utf-8 -*-
"""Audio capture and band analysis for the equalizer.

Source: the current system output.
  - Windows: WASAPI "[Loopback]" of the default output device
    (pyaudiowpatch, an optional dependency).
  - Linux:   PulseAudio/PipeWire "<sink>.monitor" input.

Pipeline per analysis frame (~43 ms at overlap 0): read loopback ->
Hann-windowed FFT -> 11 log-spaced bands (20 Hz .. 12 kHz) -> dB -> 0..15
level per band (attack/release smoothing).
"""
from __future__ import annotations

from collections import deque

import numpy as np

# Tunables
SAMPLE_RATE = 48000
FRAME = 2048            # samples per analysis window (~85 ms @ 48 kHz)
N_BANDS = 11            # one band per level l1..l11
BAND_LO = 20.0          # Hz, first band edge
BAND_HI = 12000.0       # Hz, last band edge
DB_FLOOR = -70.0        # mapped to level 0
DB_CEIL = -20.0         # mapped to level 15
ATTACK  = 0.001         # seconds to rise to a new level
RELEASE = 0.050         # seconds to fall


def find_loopback_input(p):
    """Return (device_index, name) of the input mirroring the current output.

    Windows loopbacks are named "<Device> [Loopback]"; PulseAudio/PipeWire
    monitors are "<sink>.monitor". Prefers the one matching the default output.
    """
    candidates = []
    for i in range(p.get_device_count()):
        d = p.get_device_info_by_index(i)
        name = d.get("name", "")
        if d.get("maxInputChannels", 0) <= 0:
            continue
        if "[Loopback]" in name or ".monitor" in name.lower():
            candidates.append((i, name))
    if not candidates:
        return None
    try:
        default_out = p.get_default_output_device_info()
    except Exception:
        return candidates[0]
    base = (default_out or {}).get("name", "").split(" [")[0].strip()
    if not base:
        return candidates[0]
    for i, name in candidates:
        if name.startswith(base) or base.split(" (")[0] in name:
            return (i, name)
    return (candidates[0])


def make_band_masks(sample_rate, frame, n_bands, lo, hi):
    """Log-spaced frequency band masks over one FFT frame.

    Returns (edges, masks): edges is the n_bands+1 geometrically spaced
    band edges in Hz, masks is one boolean array per band marking the FFT
    bins that fall in [edge[k], edge[k+1]).
    """
    freqs = np.fft.rfftfreq(frame, 1.0 / sample_rate)
    edges = np.geomspace(lo, hi, n_bands + 1)
    masks = [(freqs >= edges[k]) & (freqs < edges[k + 1]) for k in range(n_bands)]
    return edges, masks


def levels_from_db(mag_db, state, dt, ceil=DB_CEIL, attack=ATTACK,
                   release=RELEASE):
    """Map dB magnitudes to 0..15 levels with fast attack / slow release.

    ``state`` (length N_BANDS) is the previous per-band level, mutated in
    place. ``dt`` is the analysis interval in seconds. ``ceil`` is the dB
    level mapped to level 15: a single float (default DB_CEIL, e.g. the
    shared tracker's recent max) or one per band; DB_FLOOR is always
    mapped to 0. ``attack``/``release`` are the time constants (seconds)
    for a rising and a falling level.
    """
    if isinstance(ceil, (list, tuple)):
        spans = [c - DB_FLOOR for c in ceil]
    else:
        spans = [ceil - DB_FLOOR] * len(mag_db)
    targets = [max(0.0, min(15.0, (db - DB_FLOOR) / span * 15.0))
               for db, span in zip(mag_db, spans)]
    rise = min(1.0, dt / attack)
    fall = min(1.0, dt / release)
    for i, tgt in enumerate(targets):
        if tgt > state[i]:
            state[i] += (tgt - state[i]) * rise
        else:
            state[i] += (tgt - state[i]) * fall
    return [min(15, max(0, int(round(s)))) for s in state]


class AutoCeiling:
    """Adaptive level ceiling (auto-gain).

    Tracks the recent peak dB over a rolling window (~``window_s`` seconds)
    and returns it as the ``ceil`` argument of :func:`levels_from_db`, so
    the recent maximum maps to level 15: loud passages stop pinning at 15,
    quiet passages grow, and the loudest band of the last window is at the
    top while every other band stays proportionally scaled to it.

    ``percentile`` picks WHICH value of the window becomes the ceiling:
    100 (default) is the maximum, so a single loud transient lifts the
    scale for the whole window. A lower value (e.g. 95) ignores the
    loudest few percent of frames, so one hit no longer squashes every
    bar; the frames above that ceiling simply clamp at level 15.

    The ceiling is GLOBAL: one rolling window over ``max(mag_db)`` per
    frame, used as the ceiling for every band. A per-band max would scale
    every bar to its own peak, so quiet bars grow to the same height as
    loud ones and the relative balance between the bands is lost; the
    global max keeps the bars' heights proportional while adapting to the
    overall loudness.

    The ceiling is floored at ``DB_FLOOR + min_span`` so the span
    (ceil - floor) never collapses toward zero; otherwise silence would
    drag the ceiling down to the floor and amplify the noise floor into
    full-height bars.
    """

    def __init__(self, dt, window_s=5.0, min_span=10.0, percentile=100.0):
        if not 0.0 < float(percentile) <= 100.0:
            raise ValueError("gain_percentile must be in (0, 100]")
        self.n = N_BANDS
        self._min_span = min_span
        self._percentile = float(percentile)
        maxlen = max(1, int(round(window_s / dt)))
        self._window = deque(maxlen=maxlen)

    def update(self, mag_db):
        """Consume one frame of per-band peak dB; return the ceiling
        (a float; every band shares it)."""
        self._window.append(max(mag_db))
        if self._percentile >= 100.0:
            peak = max(self._window)
        else:
            peak = float(np.percentile(
                np.fromiter(self._window, dtype=float), self._percentile))
        return max(peak, DB_FLOOR + self._min_span)


_HANN = {}


def _hann(length):
    """Cached Hann window (recomputing it every analysis frame is wasteful)."""
    win = _HANN.get(length)
    if win is None:
        win = np.hanning(length)
        _HANN[length] = win
    return win


def analyze_frame(mono, masks, full_scale):
    """Return per-band peak dB for one mono frame (mono in [-1,1]).

    ``mono`` is one analysis window, ``masks`` from :func:`make_band_masks`,
    ``full_scale`` the FFT full-scale reference (FRAME / 2 for int16 audio).
    """
    mag = np.abs(np.fft.rfft(mono * _hann(len(mono))))
    out = []
    for mask in masks:
        if not mask.any():
            out.append(DB_FLOOR)
            continue
        peak = float(np.max(mag[mask]) / full_scale)
        out.append(20.0 * float(np.log10(peak + 1e-9)))
    return out
