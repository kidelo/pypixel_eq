# -*- coding: utf-8 -*-
"""Tests for the audio capture/analysis pipeline (hardware-free)."""

import numpy as np
import pytest

from pypixel_eq import audio
from pypixel_eq.audio import (
    SAMPLE_RATE, FRAME, N_BANDS, BAND_LO, BAND_HI, DB_FLOOR, DB_CEIL,
    AutoCeiling, make_band_masks, analyze_frame, levels_from_db,
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
