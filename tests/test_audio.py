# -*- coding: utf-8 -*-
"""Tests for the audio capture/analysis pipeline (hardware-free)."""

import numpy as np
import pytest

from pypixel_eq import audio
from pypixel_eq.audio import (
    SAMPLE_RATE, FRAME, N_BANDS, BAND_LO, BAND_HI, DB_FLOOR, DB_CEIL,
    AutoCeiling, make_band_masks, analyze_frame, levels_from_db,
    loudness_db,
)


def test_band_mask_edges_are_geometrically_spaced():
    edges, masks = make_band_masks(SAMPLE_RATE, FRAME, N_BANDS, BAND_LO, BAND_HI)
    assert len(edges) == N_BANDS + 1
    assert len(masks) == N_BANDS
    assert edges[0] == pytest.approx(BAND_LO)
    assert edges[-1] == pytest.approx(BAND_HI)
    # Geometric spacing: constant ratio between consecutive edges.
    ratios = [edges[k + 1] / edges[k] for k in range(N_BANDS)]
    assert all(r == pytest.approx(ratios[0], rel=1e-12) for r in ratios)


def test_band_masks_partition_the_in_range_bins():
    edges, masks = make_band_masks(SAMPLE_RATE, FRAME, N_BANDS, BAND_LO, BAND_HI)
    freqs = np.fft.rfftfreq(FRAME, 1.0 / SAMPLE_RATE)
    in_range = (freqs >= BAND_LO) & (freqs < BAND_HI)
    union = np.zeros_like(freqs, dtype=bool)
    for mask in masks:
        # Masks are disjoint.
        assert not (union & mask).any()
        union |= mask
    # Every in-band bin belongs to exactly one mask; out-of-band bins to none.
    assert (union & in_range).sum() == in_range.sum()
    assert not (union & ~in_range).any()


def test_single_tone_peaks_in_its_own_band():
    """A pure sine at a band center peaks at that band's level."""
    edges, masks = make_band_masks(SAMPLE_RATE, FRAME, N_BANDS, BAND_LO, BAND_HI)
    full_scale = FRAME / 2.0
    for b in range(N_BANDS):
        freq = (edges[b] * edges[b + 1]) ** 0.5
        t = np.arange(FRAME) / SAMPLE_RATE
        mono = 0.5 * np.sin(2 * np.pi * freq * t)
        mag_db = analyze_frame(mono, masks, full_scale)
        peak = int(np.argmax(mag_db))
        assert peak == b, f"tone {freq:.0f} Hz peaked in band {peak}, not {b}: {mag_db}"


def test_band_stat_rms_is_invariant_to_bin_alignment():
    """The peak jumps when a tone drifts between bins (scalloping); the RMS
    sums the band's energy and stays put. Measured: ~0.4 dB vs 0.0 dB."""
    edges, masks = make_band_masks(SAMPLE_RATE, FRAME, N_BANDS, BAND_LO, BAND_HI)
    full_scale = FRAME / 2.0
    t = np.arange(FRAME) / SAMPLE_RATE
    band = 6  # 655..1172 Hz, 23 bins
    peaks, rms = [], []
    for k in range(40):
        freq = 1000.0 + 40.0 * (k / 39.0 - 0.5)  # +/-20 Hz inside the band
        mono = 0.5 * np.sin(2 * np.pi * freq * t + k)
        peaks.append(analyze_frame(mono, masks, full_scale)[band])
        rms.append(analyze_frame(mono, masks, full_scale, stat="rms")[band])
    assert np.std(rms) < 0.01
    assert np.std(peaks) > 0.1
    # Same level on average (within ~2.2 dB: the RMS also collects the
    # window's main-lobe leakage, the peak only its loudest bin).
    assert np.mean(rms) == pytest.approx(np.mean(peaks), abs=3.0)


def test_band_stat_rms_keeps_a_tone_calibration():
    """rms = total band power. A tone's energy sits in one bin, so its own
    band reads almost the same as peak (within ~3 dB, the Hann leakage
    of the neighbouring bins); it is NOT lowered by the band width."""
    edges, masks = make_band_masks(SAMPLE_RATE, FRAME, N_BANDS, BAND_LO, BAND_HI)
    full_scale = FRAME / 2.0
    t = np.arange(FRAME) / SAMPLE_RATE
    for b in range(N_BANDS):
        freq = (edges[b] * edges[b + 1]) ** 0.5
        mono = 0.5 * np.sin(2 * np.pi * freq * t)
        peak = analyze_frame(mono, masks, full_scale)
        rms = analyze_frame(mono, masks, full_scale, stat="rms")
        assert rms[b] >= peak[b]
        assert rms[b] - peak[b] < 6.0, (b, peak[b], rms[b])


def test_band_stat_rms_lifts_broadband_upper_bands():
    """White noise fills every bin: rms sums them, so the wide upper bands
    read much higher than their single loudest bin, while a one-bin band
    (band 1) is unchanged."""
    edges, masks = make_band_masks(SAMPLE_RATE, FRAME, N_BANDS, BAND_LO, BAND_HI)
    full_scale = FRAME / 2.0
    rng = np.random.default_rng(0)
    mono = rng.normal(size=FRAME) * 0.05
    peak = analyze_frame(mono, masks, full_scale)
    rms = analyze_frame(mono, masks, full_scale, stat="rms")
    # One bin per band: identical.
    assert rms[0] == pytest.approx(peak[0])
    assert rms[1] == pytest.approx(peak[1])
    # 225 bins: total power is ~10 dB above the loudest single bin.
    assert rms[-1] - peak[-1] > 9.0
    # The lift grows with the band width (not strictly, the loudest bin is
    # random): narrow bands ~unchanged, top bands far above.
    deltas = [rms[i] - peak[i] for i in range(N_BANDS)]
    assert deltas[2] < 1.0
    assert deltas[5] < deltas[-1] - 5.0


def test_band_stat_default_is_the_peak_path():
    """The default stays byte-identical (peak), and an unknown stat is
    rejected instead of silently falling back."""
    edges, masks = make_band_masks(SAMPLE_RATE, FRAME, N_BANDS, BAND_LO, BAND_HI)
    rng = np.random.default_rng(1)
    mono = rng.normal(size=FRAME) * 0.05
    assert analyze_frame(mono, masks, FRAME / 2.0) == analyze_frame(
        mono, masks, FRAME / 2.0, stat="peak")
    for bad in ("", "mean", "RMS"):
        with pytest.raises(ValueError):
            analyze_frame(mono, masks, FRAME / 2.0, stat=bad)


def test_quiet_frame_maps_to_level_zero():
    """A near-silent frame maps every band to level 0."""
    edges, masks = make_band_masks(SAMPLE_RATE, FRAME, N_BANDS, BAND_LO, BAND_HI)
    mag_db = analyze_frame(np.zeros(FRAME), masks, FRAME / 2.0)
    assert all(db < 0 for db in mag_db)
    state = [0.0] * N_BANDS
    levels = levels_from_db(mag_db, state, FRAME / SAMPLE_RATE)
    assert levels == [0] * N_BANDS


def test_levels_have_fast_attack_slow_release():
    """A level jump rises within ~ATTACK and falls within ~RELEASE."""
    span = (DB_CEIL - DB_FLOOR) / 15.0
    mid = [DB_FLOOR + span * 7] * N_BANDS
    state = [0.0] * N_BANDS
    # Attack: one step at dt=ATTACK is a 63% approach to the target.
    dt = audio.ATTACK
    levels = levels_from_db(mid, state, dt)
    assert all(lv >= 5 for lv in levels)
    # Release: falling 7 levels takes ~dt=RELEASE to reach ~4 (one step).
    dt = audio.RELEASE
    levels2 = levels_from_db([DB_FLOOR] * N_BANDS, state, dt)
    assert all(lv <= 5 for lv in levels2)


def test_levels_use_the_configured_time_constants():
    """attack/release are parameters: a long release holds a level where the
    default 50 ms release already dropped it, and a long attack keeps a jump
    near the bottom."""
    dt = 0.05
    quiet = [DB_FLOOR] * N_BANDS
    state = [15.0] * N_BANDS
    # Default release: one 50 ms step falls all the way to 0.
    assert all(lv == 0 for lv in levels_from_db(quiet, state, dt))
    # 2 s release: the same step only drops ~2.5% -> still near the top.
    state = [15.0] * N_BANDS
    assert all(lv >= 14 for lv in levels_from_db(quiet, state, dt, release=2.0))
    # Long attack: one 50 ms step toward the top stays at level 0..1.
    loud = [DB_CEIL] * N_BANDS
    state = [0.0] * N_BANDS
    assert all(lv <= 1 for lv in levels_from_db(loud, state, dt, attack=1.0))
    state = [0.0] * N_BANDS
    assert all(lv == 15 for lv in levels_from_db(loud, state, dt, attack=0.001))


def test_levels_are_integers_in_range():
    state = [0.0] * N_BANDS
    db = [-60.0, DB_FLOOR, -45.0, -20.0, DB_CEIL, 0.0, -80.0, -30.0, -50.0, -10.0, 5.0]
    assert len(db) == N_BANDS
    for _ in range(5):
        levels = levels_from_db(db, state, 0.085)
        assert all(isinstance(lv, int) and 0 <= lv <= 15 for lv in levels)


# ---------------------------------------------------------------- auto-gain


def test_auto_ceiling_tracks_max_over_window():
    """While the level stays at a constant envelope, the ceiling is that
    envelope, so it maps to level 15 (the recent max = the scale)."""
    ac = AutoCeiling(0.085)
    env = [-30.0] * N_BANDS
    for _ in range(20):  # fill the 5 s window (59 frames)
        ceiling = ac.update(env)
    assert ceiling == pytest.approx(-30.0)
    # That envelope now maps to level 15.
    state = [0.0] * N_BANDS
    levels = levels_from_db(env, state, 0.085, ceil=ceiling)
    assert all(lv == 15 for lv in levels)


def test_auto_ceiling_is_global_over_bands():
    """The ceiling is GLOBAL: one window over max(mag_db), shared by all
    bands. The loudest band's level sets the scale; a band with a LOWER
    recent max stays proportionally scaled (it does NOT grow to its own
    peak, which a per-band tracker would do)."""
    ac = AutoCeiling(0.085)
    # Band 0 at -30 dB, band 1 at -60 dB, the rest silent.
    env = [-30.0, -60.0] + [DB_FLOOR] * (N_BANDS - 2)
    for _ in range(20):
        ceiling = ac.update(env)
    # The shared ceiling is the max over bands (-30), not per-band.
    assert ceiling == pytest.approx(-30.0)
    state = [0.0] * N_BANDS
    levels = levels_from_db(env, state, 0.085, ceil=ceiling)
    assert levels[0] == 15  # the loudest band reaches the top
    # Band 1 is 10 dB above the floor on a 40 dB span -> (10/40)*15 = 3.75.
    assert levels[1] == 4  # proportionally scaled, NOT 15 (not its own peak)
    # A silent band stays at 0 (not amplified to its own noise peak).
    assert levels[2] == 0


def test_auto_ceiling_floors_at_min_span():
    """A silent signal's ceiling is floored at DB_FLOOR + min_span, so the
    span never collapses: silence maps to level 0, not full bars."""
    ac = AutoCeiling(0.085, window_s=5.0, min_span=10.0)
    silence = [DB_FLOOR] * N_BANDS
    for _ in range(20):
        ceiling = ac.update(silence)
    assert ceiling == pytest.approx(DB_FLOOR + 10.0)
    state = [0.0] * N_BANDS
    levels = levels_from_db(silence, state, 0.085, ceil=ceiling)
    assert all(lv == 0 for lv in levels)


def test_auto_ceiling_ages_out_after_window():
    """After the window fills, an old loud frame no longer holds the
    ceiling: a quiet signal's ceiling falls to the min-span floor."""
    ac = AutoCeiling(0.085)
    loud = [DB_CEIL] * N_BANDS
    quiet = [DB_FLOOR] * N_BANDS
    for _ in range(5):
        ac.update(loud)
    # Feed silence for longer than the window: the loud frame ages out.
    for _ in range(120):
        ceiling = ac.update(quiet)
    assert ceiling == pytest.approx(DB_FLOOR + 10.0)
    # ...but during the window the loud frame still holds it.
    ac2 = AutoCeiling(0.085)
    for _ in range(5):
        ac2.update(loud)
    ceiling2 = ac2.update(quiet)
    assert ceiling2 == pytest.approx(DB_CEIL)


def test_auto_ceiling_percentile_ignores_a_transient():
    """A single loud frame lifts the max ceiling for the whole window
    (every other bar drops); a percentile below 100 ignores it."""
    loud = [DB_CEIL] * N_BANDS
    steady = [DB_FLOOR + 20.0] * N_BANDS
    auto = AutoCeiling(0.085)  # percentile 100 = max
    p95 = AutoCeiling(0.085, percentile=95.0)
    for _ in range(20):
        steady_ceil = auto.update(steady)
        p95_ceil = p95.update(steady)
    assert steady_ceil == pytest.approx(DB_FLOOR + 20.0)
    assert p95_ceil == pytest.approx(DB_FLOOR + 20.0)
    # One transient frame is the loudest 1/21 ~= 4.8 % of the window...
    assert auto.update(loud) == pytest.approx(DB_CEIL)
    # ...so the 100th percentile jumps, the 95th does not.
    assert p95.update(loud) == pytest.approx(DB_FLOOR + 20.0)


def test_auto_ceiling_percentile_is_the_median_at_50():
    """percentile=50 is the median of the window, not the maximum."""
    ac = AutoCeiling(0.085, percentile=50.0)
    for db in (-50.0, -40.0, -30.0, -20.0, -10.0):
        ceiling = ac.update([db] * N_BANDS)
    assert ceiling == pytest.approx(-30.0)


def test_auto_ceiling_rejects_invalid_percentile():
    for bad in (0, -5, 100.5):
        with pytest.raises(ValueError):
            AutoCeiling(0.085, percentile=bad)


def test_auto_gain_does_not_raise_the_floor():
    """Auto-gain rescales the ceiling only: a signal BELOW its recent
    max is still mapped on the same floor, and can drop to 0 (fixed span
    would never let the bars fall this far below the recent peak)."""
    ac = AutoCeiling(0.085)
    loud = [DB_CEIL] * N_BANDS
    for _ in range(5):
        ac.update(loud)
    ceiling = ac.update([DB_FLOOR] * N_BANDS)
    # The loud frame still holds the ceiling at DB_CEIL...
    assert ceiling == pytest.approx(DB_CEIL)
    # ...so the current silence maps to level 0 (the floor is unchanged).
    state = [0.0] * N_BANDS
    levels = levels_from_db([DB_FLOOR] * N_BANDS, state, 0.085, ceil=ceiling)
    assert all(lv == 0 for lv in levels)


def test_levels_from_db_shared_ceil():
    """levels_from_db accepts a shared float ceil: the same dB maps to a
    different level when the recent max (ceiling) differs."""
    state = [0.0] * N_BANDS
    db = [DB_FLOOR + 20.0] * N_BANDS
    # Fixed span: 20 dB above floor on the 50 dB span -> (20/50)*15 = 6.
    levels_fixed = levels_from_db(db, state, 0.085)
    assert all(lv == 6 for lv in levels_fixed)
    # Ceiling at 20 dB above floor -> that same dB is the recent max -> 15.
    state = [0.0] * N_BANDS
    levels_auto = levels_from_db(db, state, 0.085, ceil=DB_FLOOR + 20.0)
    assert all(lv == 15 for lv in levels_auto)
    # Default (no ceil) is byte-identical to the fixed span.
    state = [0.0] * N_BANDS
    assert levels_from_db(db, state, 0.085) == levels_from_db(
        db, state, 0.085, ceil=DB_CEIL)


def test_hann_window_is_cached():
    """The analysis window is built once per length, not per frame."""
    a = audio._hann(FRAME)
    b = audio._hann(FRAME)
    assert a is b
    assert len(a) == FRAME


def test_loudness_db_tracks_the_level():
    """loudness_db is the overall window RMS in dBFS: silence at the
    floor, a full-scale sine near -3 dBFS, +20 dB at 10x amplitude."""
    quiet = np.zeros(FRAME)
    t = np.arange(FRAME) / SAMPLE_RATE
    sine = 0.5 * np.sin(2 * np.pi * 440.0 * t)  # rms 0.3536 -> -9.0 dBFS
    full = np.sin(2 * np.pi * 440.0 * t)        # rms 0.7071 -> -3.0 dBFS
    assert loudness_db(quiet) < -120
    assert loudness_db(sine) == pytest.approx(-9.0, abs=0.05)
    assert loudness_db(full) == pytest.approx(-3.0, abs=0.05)
    # 10x amplitude is +20 dBFS.
    assert loudness_db(full * 10.0) == pytest.approx(17.0, abs=0.05)


def test_brightness_mapping_is_one_segment_between_min_and_max():
    """The mapping is linear between the two ends: floor -> bmin,
    ceil -> bmax, clamped outside. bmin == bmax is a constant."""
    from pypixel_eq.audio import LLOUD_FLOOR, LLOUD_CEIL
    f = audio._brightness_from_db
    # Floor -> min, ceil -> max.
    assert f(LLOUD_FLOOR, 20, 100) == pytest.approx(20.0)
    assert f(LLOUD_CEIL, 20, 100) == pytest.approx(100.0)
    # The midpoint maps to the midpoint of the range.
    assert f((LLOUD_FLOOR + LLOUD_CEIL) / 2, 20, 100) == pytest.approx(60.0)
    # Clamped below and above.
    assert f(LLOUD_FLOOR - 20, 20, 100) == pytest.approx(20.0)
    assert f(LLOUD_CEIL + 20, 20, 100) == pytest.approx(100.0)
    # A constant range (min == max) is the static case.
    assert f(LLOUD_FLOOR, 50, 50) == 50.0
    assert f(LLOUD_CEIL, 50, 50) == 50.0


def test_brightness_ramp_starts_at_the_minimum():
    """The ramp starts at the minimum (the value set at connect), so the
    first frame neither dips nor jumps."""
    ramp = audio.BrightnessRamp(30, 100)
    assert ramp.value == 30
    # A quiet level maps to the minimum and is a no-op.
    assert ramp.update(0.04, audio.LLOUD_FLOOR) is None
    assert ramp.value == 30


def test_brightness_ramp_only_steps_when_far_enough():
    """A brightness write happens only when the smoothed level has moved
    at least ``step`` from the last value sent (the panel holds the rest)."""
    ramp = audio.BrightnessRamp(50, 100, step=10.0)
    # Loud: the level rises to the ceiling, a big step from the min -> send.
    assert ramp.update(1.0, audio.LLOUD_CEIL) == 100
    # A small further rise is absorbed (no write).
    assert ramp.update(0.5, audio.LLOUD_CEIL) is None
    # Quiet again: the level falls toward the minimum (a step).
    assert ramp.update(2.0, -140.0) == 50
    assert ramp.value == 50


def test_brightness_ramp_attack_is_faster_than_release():
    """Rising brightness catches up in ~1 attack, falling in ~1 release
    (both in seconds, so the value is time-correct at any --overlap).
    step=0 here isolates the timing (every update sends)."""
    ramp = audio.BrightnessRamp(0, 100, step=0.0)
    # Full-scale audio drives the target toward 100 %; a full attack time
    # (0.2 s at the 0.1 s default) lands at the ceiling.
    ramp.update(0.2, -1.0)
    assert ramp.value == 100
    # Silence: 0.085 s of release (0.5 s default) moves only part of the
    # way, so it has fallen below the top but not all the way to 0.
    ramp.update(0.085, -140.0)
    assert 0 < ramp.value < 100
    # A full release time lands at the floor.
    ramp.update(2.0, -140.0)
    assert ramp.value == 0


# ----------------------------------------------------------------- bass mode

SR = audio.SAMPLE_RATE


def _kick_pulse(amp=0.8, freq=60.0):
    """One kick: a fast-decaying 60 Hz hit (an onset, not a tone)."""
    n = int(0.12 * SR)
    t = np.arange(n) / SR
    return amp * np.exp(-t / 0.035) * np.sin(2 * np.pi * freq * t)


def _feed_chunks(sig, chunk=1024):
    """Feed the detector in chunked pieces and collect the fire positions."""
    det = audio.KickDetector(1.0)
    fired = []
    for i in range(0, len(sig), chunk):
        if det.feed(sig[i:i + chunk]):
            fired.append(i / SR)
    return det, fired


def test_kick_fires_once_on_a_single_hit():
    """A single kick in silence fires exactly one flash (the refractory
    collapses the multi-hop onset into a single fire)."""
    sig = np.zeros(10 * SR)
    s = int(5 * SR)
    sig[s:s + len(_kick_pulse())] = _kick_pulse()
    det, fired = _feed_chunks(sig)
    # Exactly one fire, at the onset (the recorded time is the chunk start
    # that contained the fire, so it is within one hop of the true onset).
    assert len(fired) == 1
    assert 4.9 <= fired[0] <= 5.05


def test_kick_rejects_a_sustained_drone():
    """A steady bass line (no onset) never fires: the flux of a constant
    level is edge-only, so the adaptive threshold settles above it."""
    t = np.arange(10 * SR) / SR
    drone = 0.4 * np.sin(2 * np.pi * 50.0 * t)
    det, fired = _feed_chunks(drone)
    assert fired == []


def test_kick_fires_on_a_hit_over_a_drone():
    """A kick ON TOP of a steady bass line is the burst above the line:
    it fires once, where the drone alone never does."""
    t = np.arange(10 * SR) / SR
    sig = 0.4 * np.sin(2 * np.pi * 50.0 * t)
    s = int(5 * SR)
    sig[s:s + len(_kick_pulse())] += _kick_pulse()
    det, fired = _feed_chunks(sig)
    # Exactly one fire, at the onset (the recorded time is the chunk start
    # that contained the fire, so it is within one hop of the true onset).
    assert len(fired) == 1
    assert 4.9 <= fired[0] <= 5.05


def test_kick_rejects_a_high_frequency_onset():
    """A broadband attack OUT of the kick zone (3 kHz) must not fire:
    only the lowest bins count, so a 3 kHz transient is outside the zone."""
    sig = np.zeros(10 * SR)
    n = 1000
    sig[int(5 * SR):int(5 * SR) + n] = 0.5 * np.sin(
        2 * np.pi * 3000.0 * np.arange(n) / SR)
    det, fired = _feed_chunks(sig)
    assert fired == []


def test_kick_rejects_silence_and_faint_noise():
    """Silence and a -40 dBFS noise floor never fire: the absolute flux
    floor (KICK_FLOOR) is a noise gate below any real hit."""
    rng = np.random.default_rng(0)
    noise = 10 ** (-40.0 / 20.0) * rng.normal(size=10 * SR)
    det, fired = _feed_chunks(noise)
    assert fired == []
    det, fired = _feed_chunks(np.zeros(10 * SR))
    assert fired == []


def test_kick_fires_once_per_hit_in_a_beat():
    """A 120 BPM eight-hit run fires once per hit: the refractory (0.25 s)
    is short enough to catch each kick, long enough to collapse one kick's
    several hops into a single fire."""
    t = np.arange(10 * SR) / SR
    sig = 0.3 * np.sin(2 * np.pi * 50.0 * t)
    kick = _kick_pulse()
    for k in range(8):
        s = int((2.5 + 0.5 * k) * SR)
        sig[s:s + len(kick)] += kick
    det, fired = _feed_chunks(sig)
    assert len(fired) == 8
    # One fire per hit, not one per hop.
    assert all(fired[i + 1] - fired[i] >= 0.4 for i in range(7))


def test_kick_sensitivity_scales_the_threshold():
    """Higher sensitivity = a higher threshold = fewer flashes.

    The threshold is ``KICK_DELTA * sensitivity * EMA + floor``: the
    sensitivity scales the ADAPTIVE term, so it only discriminates over
    an active bassline (where the EMA is non-zero), never in silence
    (where the absolute floor rules). A soft kick on a drone fires at
    the default but is rejected at a high sensitivity - the exact
    "higher = fewer flashes" direction the flag promises."""
    t = np.arange(10 * SR) / SR
    drone = 0.4 * np.sin(2 * np.pi * 50.0 * t)
    s = int(5 * SR)

    def fires(amp, sens):
        sig = drone.copy()
        sig[s:s + len(_kick_pulse(amp))] += _kick_pulse(amp)
        det = audio.KickDetector(sens)
        n = 0
        for i in range(0, len(sig), 1024):
            if det.feed(sig[i:i + 1024]):
                n += 1
        return n

    # A strong kick fires at the default.
    assert fires(0.8, 1.0) >= 1
    # A soft kick's onset sits in the threshold band: it fires at the
    # default but is rejected at a high sensitivity (the threshold rose),
    # while it still fires at a low sensitivity (the threshold fell).
    assert fires(0.05, 1.0) == 1
    assert fires(0.05, 3.0) == 0
    assert fires(0.05, 0.5) >= 1


def test_kick_detector_rejects_invalid_sensitivity():
    for bad in (0, -1.0):
        with pytest.raises(ValueError):
            audio.KickDetector(bad)


def test_kick_flash_snaps_to_peak_and_decays_to_base():
    """trigger() snaps to the peak instantly (no attack ramp); update()
    decays back to the base and only returns a value when it moved a full
    step (KICK_STEP), holding in between."""
    flash = audio.KickFlash(base=10, peak=100)
    assert flash.value == 10  # starts at the base
    assert flash.trigger() == 100  # instant snap to the peak
    assert flash.value == 100
    # Decay: the values are monotonically non-increasing back to the base.
    seq = []
    for _ in range(200):
        v = flash.update(0.0427)  # one analysis hop
        if v is not None:
            seq.append(v)
    assert seq, "the decay must send values"
    assert seq[0] <= 100 and seq[-1] == 10  # ends exactly at the base
    assert all(a >= b for a, b in zip(seq, seq[1:]))
    # After settling, update() holds: nothing more is sent.
    assert flash.update(0.0427) is None
    assert flash.value == 10


def test_kick_flash_reset_returns_to_base():
    """reset() returns the flash to the base (used after a reconnect)."""
    flash = audio.KickFlash(base=10, peak=100)
    flash.trigger()
    flash.update(0.01)
    flash.reset()
    assert flash.value == 10
    assert flash.update(0.0427) is None  # settled: nothing to send


def test_kick_flash_equal_ends_do_nothing():
    """peak == base: there is nothing to flash, so trigger() returns None
    and update() never sends (the static case for the flash)."""
    flash = audio.KickFlash(base=50, peak=50)
    assert flash.trigger() is None
    assert flash.update(0.0427) is None
