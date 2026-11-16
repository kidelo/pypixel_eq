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
FRAME = 2048            # samples per analysis window (~43 ms @ 48 kHz)
N_BANDS = 11            # one band per level l1..l11
BAND_LO = 20.0          # Hz, first band edge
BAND_HI = 12000.0       # Hz, last band edge
DB_FLOOR = -70.0        # mapped to level 0
DB_CEIL = -20.0         # mapped to level 15
ATTACK  = 0.001         # seconds to rise to a new level
RELEASE = 0.050         # seconds to fall
LLOUD_FLOOR = -40.0     # loudness (dBFS) read as the minimum brightness
LLOUD_CEIL  = -10.0     # loudness (dBFS) read as the maximum brightness
BATTACK = 0.001         # seconds for the brightness to rise
BRELEASE = 0.050        # seconds for the brightness to fall
BSTEP = 5.0             # panel brightness must move this far (%) before a write

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


def band_centers(n_bands, lo, hi):
    """Geometric midpoints of the log-spaced bands (Hz), one per band."""
    edges = np.geomspace(lo, hi, n_bands + 1)
    return ((edges[:-1] * edges[1:]) ** 0.5).tolist()


def tilt_db(centers, tilt, pivot=1000.0, max_db=12.0):
    """Fixed per-band gain (dB) for a spectral tilt.

    ``tilt`` is a slope in dB per octave, pivoting at ``pivot`` Hz: the
    band at the pivot is unchanged, higher bands move by ``tilt`` per
    octave above it, lower bands by the same amount per octave below (so
    a positive tilt is brighter: highs up, bass down). ``max_db`` clamps
    each band, because a linear slope over >5 octaves would otherwise
    swamp the 50 dB span of the level mapping.

    This is a taste curve, not a measurement correction: the band value
    (peak/rms) already tracks the band width, so this only sets the
    balance between bass, mids and highs.
    """
    return [max(-max_db, min(max_db, tilt * float(np.log2(c / pivot))))
            for c in centers]


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


def analyze_frame(mono, masks, full_scale, stat="peak"):
    """Return per-band dB for one mono frame (mono in [-1,1]).

    ``mono`` is one analysis window, ``masks`` from :func:`make_band_masks`,
    ``full_scale`` the FFT full-scale reference (FRAME / 2 for int16 audio).

    ``stat`` selects the band value: ``"peak"`` (default) is the loudest
    bin, which tracks the strongest component and is what a single tone
    calibrates to. ``"rms"`` is the band RMS scaled by the band width:
    ``sqrt(sum(|X|**2))`` = ``sqrt(n_bins) * sqrt(mean(|X|**2))``.

    That definition matters. The bare mean (RMS per bin) would make the
    wide upper bands read down to 23 dB lower than a one-bin band, so a
    tone would change height just by moving up in frequency; the width
    factor is what a filterbank calls area normalization and keeps a tone
    calibrated in every band (the RMS lands 1.8..3 dB above the peak,
    because it also collects the window's main-lobe leakage). It is also
    the physically correct band level: a flat-in-Hz spectrum (white noise)
    carries more power in a wide band and rises 3 dB per octave, while
    pink noise (equal energy per octave, what music approximates) comes
    out flat. A hand-tuned "pink" gain table is what the same correction
    looks like in projects that take a plain bin mean; here it follows from
    the band width, so do not apply both.

    The gain is temporal: a tone whose position between bins drifts swings
    the peak by ~0.4 dB (scalloping) and the RMS by 0.0 dB, because the
    energy stays inside the band; pink noise jitters 1.4 dB (peak) versus
    1.0 dB (rms).
    """
    if stat not in ("peak", "rms"):
        raise ValueError("band stat must be 'peak' or 'rms'")
    mag = np.abs(np.fft.rfft(mono * _hann(len(mono))))
    out = []
    for mask in masks:
        if not mask.any():
            out.append(DB_FLOOR)
            continue
        if stat == "rms":
            band = float(np.sqrt(np.sum(mag[mask] ** 2))) / full_scale
        else:
            band = float(np.max(mag[mask])) / full_scale
        out.append(20.0 * float(np.log10(band + 1e-9)))
    return out


def loudness_db(mono):
    """Overall level of one window in dBFS (0 dBFS = full scale).

    The plain RMS of the samples: a full-scale sine reads -3 dBFS,
    silence at the noise floor. Independent of the band statistic, the
    tilt and the auto-gain, so it stays meaningful while the bar
    mapping changes.
    """
    rms = float(np.sqrt(np.mean(mono ** 2)))
    return 20.0 * float(np.log10(rms + 1e-9))


def bass_db_from_mag(mag_db, n_bands=3):
    """Bass level (dB) from the lowest ``n_bands`` analyzed bands.

    Combines the power of the first ``n_bands`` (B0..B2 by default,
    ~20-115 Hz) into one dB level, used to drive the panel brightness
    from the bass rather than the overall loudness. ``mag_db`` are the
    per-band dB values from :func:`analyze_frame`.
    """
    n = max(1, int(n_bands))
    bass = mag_db[:n]
    if len(bass) == 0:
        return DB_FLOOR
    # mag_db are 20*log10(amplitude-like band value): sum in the power
    # domain (10^(db/10)) so multiple active bass bands add up.
    power = 0.0
    for db in bass:
        power += 10.0 ** (float(db) / 10.0)
    return 10.0 * float(np.log10(power + 1e-12))


def _brightness_from_db(db, bmin, bmax):
    """One-segment mapping of a level (dBFS) to a panel brightness.

    ``LLOUD_FLOOR`` -> ``bmin``, ``LLOUD_CEIL`` -> ``bmax``: quiet is
    dim, loud is bright, and the two edges clamp. ``bmin == bmax`` is
    a constant brightness (the ramp then never sends), which is the
    unchanged static behaviour.
    """
    if bmin == bmax:
        return float(bmin)
    t = (db - LLOUD_FLOOR) / (LLOUD_CEIL - LLOUD_FLOOR)
    t = max(0.0, min(1.0, t))
    return bmin + (bmax - bmin) * t


class BrightnessRamp:
    """Smooth the panel brightness between a minimum and a maximum.

    The panel's brightness is one 5-byte BLE write, so it must not be
    re-sent on every analysis frame. This tracker smooths the level
    (dBFS) with attack/release and maps it to ``bmin``..``bmax``, but
    it only *returns a brightness to send* when the smoothed value has
    moved at least ``step`` (default 4 %) away from the last value
    that was actually sent - the panel holds whatever it has, so
    between writes it stays at the previous level and the internal
    ramp keeps interpolating. A quiet passage holds its value, a loud
    one steps up only as far as the level demands.

    ``bmin`` is the brightness at a quiet level (``LLOUD_FLOOR``
    dBFS) and ``bmax`` the one at a loud one (``LLOUD_CEIL`` dBFS):
    the two ends of the mapping. The device is set to ``bmin`` at
    connect and after every reconnect (the panel keeps its last
    display across links), and the ramp starts there, so the first
    frame neither dips nor jumps. ``bmin == bmax`` is the static
    case: the brightness is constant and the ramp never sends.
    """

    def __init__(self, bmin, bmax, attack=BATTACK, release=BRELEASE,
                 step=BSTEP):
        bmin = int(round(float(bmin)))
        bmax = int(round(float(bmax)))
        if not (0 <= bmin <= 100 and 0 <= bmax <= 100):
            raise ValueError("brightness must be 0..100")
        if bmin > bmax:
            raise ValueError("bmin must be <= bmax")
        self.bmin = bmin
        self.bmax = bmax
        if attack <= 0 or release <= 0:
            raise ValueError("attack and release must be > 0")
        if step < 0:
            raise ValueError("step must be >= 0")
        self.attack = attack
        self.release = release
        self.step = float(step)
        self._level = LLOUD_FLOOR  # start at the quiet level, not dark
        self._value = bmin         # the brightness sent to the panel

    @property
    def value(self):
        """The brightness currently at the panel (0..100)."""
        return self._value

    def reset(self):
        """Return to the start state (used after a reconnect, when a fresh
        device is set to the minimum brightness at connect)."""
        self._level = LLOUD_FLOOR
        self._value = self.bmin

    def update(self, dt, db):
        """Consume one frame's level (dBFS); return the brightness to
        send, or None when the panel does not need an update (the
        smoothed value moved less than ``step`` from the last one sent)."""
        if self.bmin == self.bmax:
            return None
        if db > self._level:
            k = min(1.0, dt / self.attack)
            self._level += (db - self._level) * k
        else:
            k = min(1.0, dt / self.release)
            self._level += (db - self._level) * k
        target = int(round(_brightness_from_db(self._level, self.bmin,
                                               self.bmax)))
        target = max(0, min(100, target))
        if abs(target - self._value) >= self.step:
            self._value = target
            return target
        return None
