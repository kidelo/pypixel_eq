# -*- coding: utf-8 -*-
"""Tests for the equalizer engine (hardware-free)."""

import queue
import sys
import threading
import time
import types

import numpy as np
import pytest

from pypixel_eq import audio
from pypixel_eq import equalizer
from pypixel_eq.audio import DB_FLOOR, DB_CEIL, N_BANDS, BAND_LO, BAND_HI
from pypixel_eq.equalizer import Equalizer, rhythm_levels_with_energy


def test_rhythm_energy_scales_bars():
    """Quiet frame -> bars near 0; loud frame -> bars keep their shape."""
    quiet = [DB_FLOOR] * N_BANDS
    assert all(lv == 0 for lv in rhythm_levels_with_energy([15] * N_BANDS, quiet))
    loud = [DB_CEIL] * N_BANDS
    assert all(lv == 15 for lv in rhythm_levels_with_energy([15] * N_BANDS, loud))
    # Half energy halves the bar height (approximately).
    half = [DB_FLOOR + (DB_CEIL - DB_FLOOR) / 2] * N_BANDS
    levels = rhythm_levels_with_energy([15] * N_BANDS, half)
    assert 6 <= max(levels) <= 9


def test_rhythm_energy_clamps_to_range():
    loud = [DB_CEIL] * N_BANDS
    levels = rhythm_levels_with_energy([0] * N_BANDS, loud)
    assert all(lv == 0 for lv in levels)
    levels = rhythm_levels_with_energy([15] * N_BANDS, loud)
    assert all(0 <= lv <= 15 for lv in levels)


def test_should_send_throttles():
    eq = Equalizer(send_hz=5)  # max 5 sends/second
    assert eq._should_send()  # first frame always sent
    eq._last_send = time.perf_counter()
    assert not eq._should_send()  # immediately after: throttled
    eq._last_send = time.perf_counter() - 1.0
    assert eq._should_send()  # after >= 1/5 s: allowed
    # send_hz=0 disables throttling entirely.
    eq0 = Equalizer(send_hz=0)
    assert eq0._should_send()
    assert eq0._should_send()


def test_should_send_is_a_pure_predicate():
    """A throttled/deciding call must not stamp the send time: only a
    successful write does, so a failed send is retried on the next frame."""
    eq = Equalizer(send_hz=5)
    assert eq._should_send()
    assert eq._last_send is None
    eq._mark_sent()
    stamped = eq._last_send
    assert stamped is not None
    assert not eq._should_send()
    assert eq._last_send == stamped


def test_dt_follows_the_capture_rate():
    """dt (attack/release + auto-gain window) uses the real device rate,
    not a hardcoded 48 kHz."""
    eq = Equalizer(overlap=0)
    eq._set_rate(44100)
    assert eq._device_rate == 44100
    assert eq.dt == pytest.approx(eq.hop / 44100)
    assert eq._auto._window.maxlen == max(1, round(5.0 / eq.dt))
    eq._set_rate(audio.SAMPLE_RATE)
    assert eq.dt == pytest.approx(eq.hop / audio.SAMPLE_RATE)


def test_invalid_overlap_and_gain_window_rejected():
    with pytest.raises(ValueError):
        Equalizer(overlap=-1)
    with pytest.raises(ValueError):
        Equalizer(gain_window=0)


def test_invalid_smoothing_rejected():
    with pytest.raises(ValueError):
        Equalizer(attack=0)
    with pytest.raises(ValueError):
        Equalizer(release=-0.1)


def test_engine_uses_the_configured_smoothing():
    """attack/release reach the level mapping (the engine's smoothing):
    a slow release holds the bars up where a fast one has already fallen."""
    slow = Equalizer(auto_gain=False, release=2.0)
    fast = Equalizer(auto_gain=False, release=0.01)
    state_slow = [15.0] * N_BANDS
    state_fast = [15.0] * N_BANDS
    quiet = [DB_FLOOR] * N_BANDS
    assert all(lv >= 14 for lv in slow._levels(quiet, state_slow))
    assert all(lv == 0 for lv in fast._levels(quiet, state_fast))


def test_invalid_gain_percentile_rejected():
    with pytest.raises(ValueError):
        Equalizer(gain_percentile=0)
    with pytest.raises(ValueError):
        Equalizer(gain_percentile=101)


def test_engine_gain_percentile_survives_a_rate_change():
    """--gain-percentile is stored and threaded into a fresh tracker when
    the capture rate (and therefore dt) changes."""
    eq = Equalizer(gain_percentile=95.0, overlap=0)
    assert eq._auto._percentile == 95.0
    eq._set_rate(44100)
    assert eq._auto._percentile == 95.0
    assert eq._auto._window.maxlen == max(1, round(eq.gain_window / eq.dt))


def test_engine_percentile_ignores_a_transient():
    """A single loud frame does not squash the bars with percentile 95,
    while the default (100) keeps the transient as the window's ceiling."""
    loud = [DB_CEIL] * N_BANDS
    steady = [DB_FLOOR + 20.0] * N_BANDS
    p95 = Equalizer(auto_gain=True, gain_percentile=95.0, overlap=0)
    maxed = Equalizer(auto_gain=True, overlap=0)
    for _ in range(20):
        p95._levels(steady, [0.0] * N_BANDS)
        maxed._levels(steady, [0.0] * N_BANDS)
    p95._levels(loud, [0.0] * N_BANDS)
    maxed._levels(loud, [0.0] * N_BANDS)
    # The transient is ~5 % of the window: the 95th percentile still sits
    # at the steady envelope, the max ceiling is the transient.
    assert p95._auto.update(steady) == pytest.approx(DB_FLOOR + 20.0)
    assert maxed._auto.update(steady) == pytest.approx(DB_CEIL)


def test_cli_gain_percentile_flag():
    """--gain-percentile defaults to 100 (the existing max behaviour) and
    is forwarded to the engine."""
    from pypixel_eq.__main__ import build_parser
    args = build_parser().parse_args([])
    assert args.gain_percentile == 100.0
    custom = build_parser().parse_args(["--gain-percentile", "95"])
    assert custom.gain_percentile == 95.0
    assert Equalizer(gain_percentile=custom.gain_percentile)._auto._percentile == 95.0


def test_invalid_band_stat_rejected():
    with pytest.raises(ValueError):
        Equalizer(band_stat="mean")


def test_engine_bands_use_the_configured_stat():
    """The engine's analysis step uses --band-stat: rms reads a broadband
    frame higher than peak in the wide upper band."""
    rng = np.random.default_rng(2)
    mono = rng.normal(size=equalizer.FRAME) * 0.05
    edges, masks = audio.make_band_masks(audio.SAMPLE_RATE, equalizer.FRAME,
                                         N_BANDS, audio.BAND_LO, audio.BAND_HI)
    full_scale = equalizer.FRAME / 2.0
    rms = Equalizer(band_stat="rms")._analyze(mono, masks, full_scale)
    peak = Equalizer()._analyze(mono, masks, full_scale)
    assert rms[-1] - peak[-1] > 9.0
    assert rms[:2] == pytest.approx(peak[:2])


def test_cli_band_stat_flag():
    """--band-stat defaults to peak (the existing behaviour) and is
    forwarded to the engine."""
    from pypixel_eq.__main__ import build_parser
    args = build_parser().parse_args([])
    assert args.band_stat == "peak"
    custom = build_parser().parse_args(["--band-stat", "rms"])
    assert custom.band_stat == "rms"
    assert Equalizer(band_stat=custom.band_stat).band_stat == "rms"


def test_tilt_curve_is_zero_without_tilt():
    centers = audio.band_centers(N_BANDS, BAND_LO, BAND_HI)
    assert audio.tilt_db(centers, 0.0) == [0.0] * N_BANDS
    assert len(centers) == N_BANDS


def test_tilt_curve_slopes_around_one_khz_and_clamps():
    centers = audio.band_centers(N_BANDS, BAND_LO, BAND_HI)
    gains = audio.tilt_db(centers, 1.0)
    # Positive tilt = brighter: the bands rise with frequency.
    assert gains == sorted(gains)
    # The pivot (1 kHz) lies between band 6 (876 Hz) and band 7 (1568 Hz),
    # so the nearest band is within one octave of 0 dB.
    pivot_band = int(np.argmin([abs(np.log2(c / 1000.0)) for c in centers]))
    assert abs(gains[pivot_band]) < 0.5
    # One octave away is 1 dB at a slope of 1 dB/octave.
    for c, g in zip(centers, gains):
        octaves = np.log2(c / 1000.0)
        assert g == pytest.approx(min(12.0, max(-12.0, octaves)), abs=1e-9)
    # A steep slope is clamped instead of swamping the 50 dB span.
    steep = audio.tilt_db(centers, 6.0)
    assert max(steep) == pytest.approx(12.0)
    assert min(steep) == pytest.approx(-12.0)


def test_engine_tilt_is_applied_before_the_gain_tracker():
    """--tilt shifts the per-band dB before auto-gain/levels, so the ceiling
    follows the shaped spectrum; 0 leaves the analysis untouched."""
    rng = np.random.default_rng(4)
    mono = rng.normal(size=equalizer.FRAME) * 0.05
    edges, masks = audio.make_band_masks(audio.SAMPLE_RATE, equalizer.FRAME,
                                         N_BANDS, audio.BAND_LO, audio.BAND_HI)
    full_scale = equalizer.FRAME / 2.0
    flat = Equalizer()
    warm = Equalizer(tilt=-1.0)
    assert flat._analyze(mono, masks, full_scale) == \
        audio.analyze_frame(mono, masks, full_scale)
    shifted = warm._analyze(mono, masks, full_scale)
    base = flat._analyze(mono, masks, full_scale)
    gains = audio.tilt_db(audio.band_centers(N_BANDS, audio.BAND_LO,
                                             audio.BAND_HI), -1.0)
    assert shifted == pytest.approx([b + g for b, g in zip(base, gains)])
    # A warm tilt lowers the top band and lifts the bass.
    assert shifted[-1] < base[-1]
    assert shifted[0] > base[0]


def test_invalid_tilt_rejected():
    for bad in (6.5, -7.0):
        with pytest.raises(ValueError):
            Equalizer(tilt=bad)


def test_cli_tilt_flag():
    """--tilt defaults to 0 (no change) and is forwarded to the engine."""
    from pypixel_eq.__main__ import build_parser
    args = build_parser().parse_args([])
    assert args.tilt == 0.0
    custom = build_parser().parse_args(["--tilt", "-1.5"])
    assert custom.tilt == -1.5
    assert Equalizer(tilt=custom.tilt).tilt == -1.5


def test_cli_brightness_flag():
    """--brightness defaults to 100 (the panel's own value) and is
    validated and forwarded to the engine."""
    from pypixel_eq.__main__ import build_parser
    args = build_parser().parse_args([])
    assert args.brightness == 100
    custom = build_parser().parse_args(["--brightness", "30"])
    assert custom.brightness == 30
    eq = Equalizer(brightness=custom.brightness)
    assert eq.brightness == 30


def test_invalid_brightness_rejected():
    for bad in (-1, 101, 150):
        with pytest.raises(ValueError):
            Equalizer(brightness=bad)


def test_cli_attack_and_release_flags():
    """--attack/--release default to the module tunables and are forwarded."""
    from pypixel_eq.__main__ import build_parser
    args = build_parser().parse_args([])
    assert args.attack == audio.ATTACK
    assert args.release == audio.RELEASE
    custom = build_parser().parse_args(["--attack", "0.05", "--release", "0.4"])
    assert custom.attack == 0.05
    assert custom.release == 0.4
    eq = Equalizer(attack=custom.attack, release=custom.release)
    assert (eq.attack, eq.release) == (0.05, 0.4)


def test_cli_default_transport_is_rhythm():
    """The CLI default must match the help text and README."""
    from pypixel_eq.__main__ import build_parser
    args = build_parser().parse_args([])
    assert args.transport == "rhythm"
    assert build_parser().parse_args(["--transport", "png"]).transport == "png"
    assert build_parser().parse_args(["--transport", "rhythm"]).transport == "rhythm"


def test_cli_png_defaults_to_the_stable_rate():
    """png holds 10 fps unless a rate is given: the image path flickers at
    the link's own ~11.5 fps, while 10 fps is stable (HW-verified)."""
    from pypixel_eq.__main__ import PNG_SEND_HZ, resolve_send_hz
    assert PNG_SEND_HZ == 10.0
    assert resolve_send_hz("png", 0.0) == PNG_SEND_HZ
    assert resolve_send_hz("rhythm", 0.0) == 0.0
    assert resolve_send_hz("png", 7.5) == 7.5
    assert resolve_send_hz("rhythm", 15.0) == 15.0


def test_start_terminates_pyaudio_on_setup_failure(monkeypatch):
    """A failed capture setup must not leak the PortAudio instance."""
    class FakePA:
        terminated = False

        def terminate(self):
            self.terminated = True

    pa = FakePA()
    monkeypatch.setitem(sys.modules, "pyaudiowpatch",
                        types.SimpleNamespace(PyAudio=lambda: pa))
    monkeypatch.setattr(equalizer, "find_loopback_input", lambda p: None)
    eq = Equalizer()
    with pytest.raises(RuntimeError):
        eq.start()
    assert pa.terminated
    assert eq._pyaudio is None


def test_send_reconnects_after_a_write_error():
    """A failed BLE write triggers one reconnect and the NEXT frame goes
    out over the recovered link (the worker does not die)."""
    class FlakyDev:
        width = height = 16

        def __init__(self):
            self.calls = 0
            self.reconnects = 0
            self.closed = False
            self.send_ms = None

        def update(self, payload):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("link lost")
            self.send_ms = 12.0

        def reconnect(self):
            self.reconnects += 1

        def close(self):
            self.closed = True

    eq = Equalizer(address="AA:BB:CC:DD:EE:FF")
    eq.reconnect_delay = 0.01
    dev = FlakyDev()

    same, send_ms = eq._send(dev, b"x")
    assert same is dev
    assert send_ms is None
    assert dev.reconnects == 1

    same, send_ms = eq._send(dev, b"x")
    assert same is dev
    assert send_ms is not None
    assert dev.calls == 2


def test_put_drops_oldest_when_full():
    eq = Equalizer()
    # Fill the bounded queue (maxsize 8).
    for i in range(8):
        eq.q.put(("note", i))
    # A new item drops the oldest (0) and keeps the rest.
    eq._put(("note", 99))
    assert eq.q.qsize() == 8
    drained = [item[1] for item in
               (eq.q.get_nowait() for _ in range(8))]
    assert 0 not in drained  # oldest dropped
    assert 99 in drained     # newest present
    assert sorted(drained) == [1, 2, 3, 4, 5, 6, 7, 99]


def test_engine_reuses_audio_tunables():
    """The engine must share the same tunables as the audio module
    (it imports them, so the objects are identical)."""
    assert equalizer.N_BANDS is audio.N_BANDS
    assert equalizer.FRAME is audio.FRAME
    assert equalizer.DB_FLOOR is audio.DB_FLOOR
    assert equalizer.DB_CEIL is audio.DB_CEIL
    assert equalizer.BAND_LO is audio.BAND_LO
    assert equalizer.BAND_HI is audio.BAND_HI


def test_engine_levels_apply_auto_gain():
    """With auto-gain, the overall recent max maps to level 15 (the same
    envelope that only reaches ~6 on the fixed span)."""
    eq = Equalizer(auto_gain=True)
    state = [0.0] * N_BANDS
    env = [-50.0] * N_BANDS  # (DB_FLOOR + 20 dB)
    for _ in range(20):
        levels = eq._levels(env, state)
    assert all(lv == 15 for lv in levels)


def test_engine_no_auto_gain_uses_fixed_span():
    """auto_gain=False: the fixed DB_FLOOR/DB_CEIL span (byte-identical
    to the original behavior; the engine never touches the tracker)."""
    eq = Equalizer(auto_gain=False)
    state = [0.0] * N_BANDS
    levels = eq._levels([-50.0] * N_BANDS, state)
    # 20 dB above the floor on the (DB_CEIL-DB_FLOOR) span:
    # (20/50)*15 = 6.
    assert all(lv == 6 for lv in levels)


def test_engine_rhythm_bypasses_energy_fold_with_auto_gain():
    """Auto-gain already scales the bars to the music's loudness, so the
    separate energy fold is skipped in that mode (it would double-
    normalize); off, the energy fold is applied as before."""
    quiet = [DB_FLOOR] * N_BANDS
    loud = [DB_CEIL] * N_BANDS
    eq_auto = Equalizer(auto_gain=True)
    assert eq_auto._rhythm_levels([8] * N_BANDS, quiet) == [8] * N_BANDS
    eq_fixed = Equalizer(auto_gain=False)
    assert eq_fixed._rhythm_levels([15] * N_BANDS, quiet) == [0] * N_BANDS
    assert eq_fixed._rhythm_levels([15] * N_BANDS, loud) == [15] * N_BANDS


def test_overlap_rate_matches_level():
    """Panel rate = 23.4 Hz x (1+overlap): dt shrinks and the hop
    advances accordingly; smoothing/gain stay in seconds."""
    for n, hz in ((0, 23.4), (1, 46.9), (2, 70.3)):
        eq = Equalizer(overlap=n)
        assert eq.hop == audio.FRAME / (1.0 + n)
        assert eq.dt == eq.hop / audio.SAMPLE_RATE
        assert 1.0 / eq.dt == pytest.approx(hz, abs=0.2)
        assert eq._auto._window.maxlen == max(
            1, round(5.0 / eq.dt))  # 5 s window, in frames


def test_test_mode_hop_rate_and_phase_continuity():
    """--test drives the rolling buffer at the exact hop rate: a short
    run of the real worker yields the expected frame rate, so the panel
    is fed faster with overlap (and the sine stream advances per sample
    position, so no per-frame phase discontinuity)."""
    for overlap, hz in ((0, 23.4), (1, 46.9)):
        eq = Equalizer(overlap=overlap)
        eq.start(test=True)
        try:
            t0 = time.time()
            deadline = t0 + 1.0
            count = 0
            while time.time() < deadline:
                try:
                    eq.q.get(timeout=0.2)
                except Exception:
                    continue
                count += 1
        finally:
            eq.stop()
        elapsed = time.time() - t0
        assert count > 0
        # Skip the first/last ~0.3 s (worker start, queue teardown); the
        # sustained rate over the middle must be near the hop rate.
        assert count - 3 >= 0.75 * hz * (elapsed - 0.3), (
            f"overlap {overlap}: {count} frames in {elapsed:.2f} s, "
            f"expected ~{hz} Hz")


class _FakeStream:
    """Loopback stream with a tone in the RIGHT channel only."""

    def __init__(self, freq=1000.0, fail=False):
        self.n = 0
        self.freq = freq
        self.fail = fail
        self.closed = False

    def read(self, count, exception_on_overflow=False):
        if self.fail:
            raise RuntimeError("capture died")
        out = np.zeros(count * 2, dtype=np.int16)
        n = np.arange(self.n, self.n + count)
        tone = 0.9 * np.sin(2 * np.pi * self.freq * n / audio.SAMPLE_RATE)
        out[1::2] = (tone * 32767).astype(np.int16)
        self.n += count
        return out.tobytes()

    def close(self):
        self.closed = True


class _FakePyAudio:
    def __init__(self, stream):
        self.stream = stream

    def get_device_info_by_index(self, index):
        return {"maxInputChannels": 2}

    def open(self, **kwargs):
        return self.stream


class _FakeDevice:
    width = height = 16

    def __init__(self):
        self.closed = False

    def update(self, payload):
        pass

    def reconnect(self):
        pass

    def close(self):
        self.closed = True


def _setup_live(monkeypatch, eq, stream, device=None):
    monkeypatch.setitem(sys.modules, "pyaudiowpatch",
                        types.SimpleNamespace(paInt16=8))
    if device is not None:
        monkeypatch.setattr(equalizer, "Device", lambda *a, **k: device)
    eq._pyaudio = _FakePyAudio(stream)
    eq._device_index = 0
    eq._device_name = "fake"
    eq._set_rate(audio.SAMPLE_RATE)


def _run_live(monkeypatch, eq, stream, device=None):
    _setup_live(monkeypatch, eq, stream, device)
    eq._work_live()


def test_live_downmixes_both_channels(monkeypatch):
    """A right-panned tone must still register (the old mono[0::2] kept
    only the left channel, so this frame would be silent)."""
    stream = _FakeStream()
    eq = Equalizer(address=None)
    _setup_live(monkeypatch, eq, stream)
    thread = threading.Thread(target=eq._work_live, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 3.0
        frame = None
        while time.time() < deadline:
            item = eq.q.get(timeout=0.2)
            if item[0] == "frame":
                frame = item
                break
        assert frame is not None, "no analysis frame produced"
    finally:
        eq.stop_event.set()
        thread.join(timeout=3)

    _kind, mag_db, levels, _img, _send_ms, _n = frame
    assert max(mag_db) > DB_FLOOR
    assert any(lv > 0 for lv in levels)
    assert stream.closed


def test_live_closes_stream_and_device_on_error(monkeypatch):
    """An exception inside the loop must still release the stream and the
    BLE device (they used to be closed only on the success path)."""
    stream = _FakeStream(fail=True)
    dev = _FakeDevice()
    eq = Equalizer(address="AA:BB:CC:DD:EE:FF")
    _run_live(monkeypatch, eq, stream, device=dev)

    assert stream.closed
    assert dev.closed
    items = []
    while True:
        try:
            items.append(eq.q.get_nowait())
        except queue.Empty:
            break
    assert any(i[0] == "err" and "capture died" in str(i[1]) for i in items)


def test_gui_reschedules_after_error():
    """The GUI tick loop must reschedule even when a worker error arrives
    (the old code returned without after(), freezing the window)."""
    from pypixel_eq import gui

    class FakeStatus:
        def __init__(self):
            self.value = ""

        def set(self, value):
            self.value = value

    eq = Equalizer()
    view = gui.EQGui.__new__(gui.EQGui)
    view.eq = eq
    view.status = FakeStatus()
    scheduled = []
    view.root = types.SimpleNamespace(
        after=lambda ms, fn: scheduled.append((ms, fn)))

    eq.q.put(("err", RuntimeError("boom")))
    view._tick()
    assert "boom" in view.status.value
    assert scheduled and scheduled[0][0] == 40
    assert scheduled[0][1] == view._tick

    # The empty-queue path must reschedule too.
    view._tick()
    assert len(scheduled) == 2
