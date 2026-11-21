# -*- coding: utf-8 -*-
"""The equalizer engine: capture -> analyze -> render -> payload -> send.

A single worker thread runs the analysis loop regardless of the display;
the emitted frames are consumed by the GUI (live mirror) or the console
loop, and - when an address is given - throttled and written to the
panel. The full frame is produced even without a device, so the GUI
emulation mirrors the device exactly.

The loop re-analyzes a rolling 2048-sample window every FRAME/(1+overlap)
samples: overlap 0 is 23.4 Hz; each extra level adds a full analysis pass
per second (overlap 1 = 46.9 Hz, overlap 2 = 70.3 Hz) without losing
frequency resolution. dt is the hop interval, so the attack/release
smoothing and the auto-gain window stay time-correct at every level.
(Panel hardware limit: levels 3+ overload the panel - the high bands
get damped - so --overlap is capped at 2.)
"""
from __future__ import annotations

import logging
import queue
import threading
import time

import numpy as np

from .audio import (
    SAMPLE_RATE, FRAME, N_BANDS, BAND_LO, BAND_HI, DB_FLOOR, DB_CEIL,
    ATTACK, RELEASE, LLOUD_FLOOR, LLOUD_CEIL, _brightness_from_db,
    AutoCeiling, band_centers, BrightnessRamp, find_loopback_input,
    loudness_db, make_band_masks, levels_from_db, tilt_db, analyze_frame,
)
from .transport import Device, png_payload, render_image, rhythm_payload

logger = logging.getLogger(__name__)


def rhythm_levels_with_energy(levels, mag_db):
    """Fold the overall loudness into the 11 band levels (0..15).

    The panel's native visualizer has no brightness knob in this
    transport, so the energy (median band, normalized to 0..1) scales
    every bar: quiet passages lower all bars, loud passages raise them.
    ``mag_db`` is the per-band peak dB for the current frame.
    """
    norm = [max(0.0, min(1.0, (db - DB_FLOOR) / (DB_CEIL - DB_FLOOR)))
            for db in mag_db]
    energy = float(np.median(norm)) if norm else 0.0
    return [max(0, min(15, int(round(lv * energy)))) for lv in levels]


class Equalizer:
    """Captures the system output and emits frames (mag_db, levels, image)."""

    def __init__(self, address=None, width=64, height=16, transport="png",
                 rhythm_style=0, send_hz=0.0, auto_gain=True,
                 gain_window=5.0, gain_percentile=100.0, band_stat="peak",
                 tilt=0.0, brightness_min=100, brightness_max=100,
                 brightness_mode="static", brightness_bass_bands=(0, 2),
                 brightness_interval=1.0,
                 overlap=0,
                 write_with_response=True,
                 attack=ATTACK, release=RELEASE):
        self.address = address
        self.width = width
        self.height = height
        self.transport = transport
        self.rhythm_style = rhythm_style
        # Level smoothing time constants (seconds): how fast a bar rises
        # to a louder level and falls back. Time-based, so the same values
        # behave identically at every device rate and --overlap level.
        if attack <= 0:
            raise ValueError("attack must be > 0")
        if release <= 0:
            raise ValueError("release must be > 0")
        self.attack = attack
        self.release = release
        # Device writes run on a background writer in transport.Device:
        # update()/push_brightness() hand over work and return, so the
        # analysis loop keeps its rate. Data has priority over brightness
        # and both use a latest-wins slot (queue depth 1).
        self.write_with_response = write_with_response
        # Minimum seconds between brightness writes under sustained data load
        # (0 = strict data priority).
        if brightness_interval < 0:
            raise ValueError("brightness_interval must be >= 0")
        self.brightness_interval = float(brightness_interval)
        # Throttle the frames SENT to the device (0 = send every frame).
        # Audio analysis always runs at the native hop rate; only the
        # panel writes are thinned, so level smoothing stays correct.
        self.send_hz = send_hz
        if overlap < 0:
            raise ValueError("overlap must be >= 0")
        if gain_window <= 0:
            raise ValueError("gain_window must be > 0")
        self.q = queue.Queue(maxsize=8)
        self.stop_event = threading.Event()
        # Overlap raises the panel rate: one analysis per FRAME/(1+overlap)
        # samples (23.4 Hz at 0, 46.9 at 1, 70.3 at 2; the CLI caps at 2
        # because the panel overloads at 3+). dt is the hop interval in
        # seconds, so smoothing and the gain window stay in real-time
        # terms at every level.
        self.overlap = overlap
        self.hop = FRAME / (1.0 + overlap)
        self._device_rate = SAMPLE_RATE
        self.dt = self.hop / self._device_rate
        self._pyaudio = None
        self._worker = None
        self._last_send = None
        # Seconds before the first reconnect attempt; doubled on each
        # failure up to 10 s. Kept as an attribute so tests can shorten it.
        self.reconnect_delay = 1.0
        # Auto-gain: the overall recent max (over gain_window s, one
        # shared tracker) is remapped to level 15, so the loudest band of
        # the window is at the top and every other bar stays proportionally
        # scaled to it - instead of the fixed DB_FLOOR/DB_CEIL span.
        self.auto_gain = auto_gain
        self.gain_window = gain_window
        # Which value of the recent window becomes level 15: 100 = the max
        # (a single transient lifts the scale for the whole window), 95 =
        # ignore the loudest 5 % of frames so one hit stops squashing every
        # bar. Validated by AutoCeiling.
        self.gain_percentile = gain_percentile
        self._auto = AutoCeiling(self.dt, window_s=gain_window,
                                 percentile=gain_percentile)
        # Per-band value: "peak" (loudest bin, the original behaviour) or
        # "rms" (total band power: steadier bars, broadband content - noise,
        # cymbals - lifts the wide upper bands). Validated by analyze_frame.
        if band_stat not in ("peak", "rms"):
            raise ValueError("band_stat must be 'peak' or 'rms'")
        self.band_stat = band_stat
        # Fixed spectral tilt in dB/octave around 1 kHz (taste curve):
        # positive = brighter. Zero is the unchanged behaviour.
        if abs(float(tilt)) > 6.0:
            raise ValueError("tilt must be within +/-6 dB per octave")
        self.tilt = float(tilt)
        self._tilt_db = tilt_db(band_centers(N_BANDS, BAND_LO, BAND_HI),
                               self.tilt)
        # Panel brightness (0..100), set at connect and after every
        # reconnect (the panel keeps its last display across links).
        # 100/100 = the panel's own default, so it is the unchanged
        # behaviour. The minimum is the static value and the floor the
        # auto mode maps quiet levels to; the maximum is the ceiling
        # the auto mode maps loud levels to.
        brightness_min = int(brightness_min)
        brightness_max = int(brightness_max)
        if not 0 <= brightness_min <= 100:
            raise ValueError("brightness must be 0..100")
        if not 0 <= brightness_max <= 100:
            raise ValueError("brightness must be 0..100")
        self.brightness = brightness_min
        self.brightness_max = brightness_max
        if brightness_mode == "bass":
            brightness_mode = "auto"
            brightness_source = "bass"
        elif brightness_mode == "auto":
            brightness_source = "loudness"
        elif brightness_mode == "static":
            brightness_source = "loudness"
        else:
            raise ValueError(
                "brightness_mode must be 'static', 'auto' or 'bass'")
        self.brightness_mode = brightness_mode
        self.brightness_source = brightness_source
        lo, hi = brightness_bass_bands
        lo = int(lo)
        hi = int(hi)
        if not (0 <= lo <= hi < N_BANDS):
            raise ValueError(
                "brightness_bass_bands must satisfy 0 <= lo <= hi < N_BANDS")
        self.brightness_bass_lo = lo
        self.brightness_bass_hi = hi
        # The ramp smooths the brightness and only emits a value when it
        # moved far enough; the device is set to the minimum at connect,
        # so the ramp starts there and the first frame neither dips nor
        # jumps. In the static mode it is never fed (the panel holds the
        # connect value).
        self._bright = BrightnessRamp(brightness_min, brightness_max)

    def start(self, test=False):
        self._test = test
        if not test:
            import pyaudiowpatch as py
            self._pyaudio = py.PyAudio()
            try:
                found = find_loopback_input(self._pyaudio)
                if found is None:
                    raise RuntimeError(
                        "No loopback/monitor input found.\n"
                        "  Windows: a '[Loopback]' endpoint must exist.\n"
                        "  Linux: enable a sink monitor (e.g. pavucontrol -> "
                        "record from '<sink> monitor').")
                self._device_index, self._device_name = found
                self._device_rate = int(self._pyaudio.get_device_info_by_index(
                    self._device_index).get(
                        "defaultSampleRate", SAMPLE_RATE)) or SAMPLE_RATE
                # The analysis interval must follow the device's real rate:
                # dt drives attack/release and the auto-gain window, so a
                # hardcoded 48 kHz would make them ~9% slow at 44.1 kHz.
                self._set_rate(self._device_rate)
            except Exception:
                # Do not leak the PortAudio instance when setup fails.
                self._pyaudio.terminate()
                self._pyaudio = None
                raise
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()

    def _set_rate(self, rate):
        """Set the capture rate and rebuild the time-based analysis state."""
        self._device_rate = int(rate) or SAMPLE_RATE
        self.dt = self.hop / self._device_rate
        self._auto = AutoCeiling(self.dt, window_s=self.gain_window,
                                 percentile=self.gain_percentile)

    def _put_note(self, note):
        """Post a status note without ever evicting a pending frame."""
        try:
            self.q.put_nowait(("note", note))
        except queue.Full:
            pass

    def _put(self, item):
        """Enqueue an item, dropping the OLDEST when the queue is full.

        The GUI drains at its own pace (one frame per ~40 ms tick); if it
        lags (a tkinter hiccup, window drag, or accumulation) the bounded
        queue fills. Frames are ephemeral - the panel is the real display
        and the GUI is only a mirror - so a full queue drops the oldest
        frame instead of raising and killing the worker thread.
        """
        while True:
            try:
                self.q.put(item, block=False)
                return
            except queue.Full:
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    return

    def _work(self):
        if self._test:
            self._work_test()
        else:
            self._work_live()

    def _analyze(self, buf, masks, full_scale):
        """Per-band dB of the current window, using the configured band
        statistic (peak or rms) and the fixed spectral tilt."""
        mag_db = analyze_frame(buf, masks, full_scale, stat=self.band_stat)
        if self.tilt:
            mag_db = [db + gain for db, gain in zip(mag_db, self._tilt_db)]
        return mag_db

    def _levels(self, mag_db, state):
        """Frame levels, applying auto-gain when enabled.

        With auto-gain the shared ceiling tracks the overall recent max
        over ``gain_window`` seconds (one tracker for all bands), so the
        loudest band of the window maps to level 15 and every other bar
        stays proportionally scaled to it; the bars can also fall again as
        the peak ages out and a quiet ceiling takes over.
        """
        if self.auto_gain:
            return levels_from_db(mag_db, state, self.dt,
                                  ceil=self._auto.update(mag_db),
                                  attack=self.attack, release=self.release)
        return levels_from_db(mag_db, state, self.dt,
                              attack=self.attack, release=self.release)

    def _rhythm_levels(self, levels, mag_db):
        """Levels for the native visualizer transport.

        Auto-gain already scales the bars to the music's loudness, so the
        separate energy fold is skipped in that mode (it would double-
        normalize); otherwise the overall energy is folded into the levels.
        """
        if self.auto_gain:
            return list(levels)
        return rhythm_levels_with_energy(levels, mag_db)

    def _should_send(self):
        """Whether the current frame should be written to the panel.

        Throttles device writes to ``send_hz`` (0 = every frame). The
        audio analysis runs at the native rate regardless, so level
        smoothing stays correct. This is a pure predicate: the caller
        stamps ``_mark_sent()`` only after a write actually succeeded,
        so a failed send is retried on the next frame.
        """
        if self.send_hz <= 0:
            return True
        if self._last_send is None:
            return True
        return time.perf_counter() - self._last_send >= 1.0 / self.send_hz

    def _mark_sent(self):
        self._last_send = time.perf_counter()

    def _send_bright(self, dev, buf, levels):
        """Feed the brightness ramp with the selected audio level.

        In the auto mode the ramp maps the level between the minimum and
        maximum and only returns a new value when it has moved far
        enough (a step, e.g. 5%), so a brightness write is a 5-byte
        command per actual step only - quiet passages hold the last
        value, loud/bass hits step up. Source: overall loudness (RMS of
        the window) by default, or bass presence from the selected
        band levels (default B0..B2, ~20-115 Hz) when
        ``brightness_source`` is ``"bass"``. In the static mode the ramp
        is never fed and the panel holds the connect value. A failed
        write is re-raised by ``push_brightness`` on the next call, so
        the reconnect path still runs on the analysis thread.
        """
        if dev is None or self.brightness_mode != "auto":
            return dev
        if self.brightness_source == "bass":
            lo = self.brightness_bass_lo
            hi = self.brightness_bass_hi
            bass = max(
                (max(0.0, min(15.0, float(lv))) for lv in levels[lo:hi + 1]),
                default=0.0)
            level = LLOUD_FLOOR + (bass / 15.0) * (LLOUD_CEIL - LLOUD_FLOOR)
            to_send = self._bright.update(self.dt, level)
        else:
            level = loudness_db(buf)
            to_send = self._bright.update(self.dt, level)
        logger.debug(
            "brightness[%s] send=%s value=%s",
            self.brightness_source, to_send, self._bright.value)
        if to_send is None:
            return dev
        try:
            dev.push_brightness(to_send)
        except Exception as e:
            self._put_note(f"brightness send failed ({e}) - reconnecting")
            return self._reconnect(dev)
        return dev

    @property
    def current_brightness(self):
        """The panel brightness the view should show (0..100).

        The auto mode follows the ramp (which tracks the audio level);
        otherwise the fixed connect value (the minimum).
        """
        if self.brightness_mode == "auto":
            return self._bright.value
        return self.brightness

    def _connect_device(self):
        """Connect to the configured panel, or return None (display only)."""
        if not self.address:
            return None
        self._put_note(f"connecting {self.address} ...")
        try:
            dev = Device(self.address, transport=self.transport,
                         rhythm_style=self.rhythm_style,
                         write_with_response=self.write_with_response,
                         brightness=self.brightness,
                         brightness_interval=self.brightness_interval)
        except Exception as e:
            self._put_note(f"connect failed ({e}) - display only")
            return None
        self.width, self.height = dev.width, dev.height
        # The device was set to the minimum at connect: re-sync the ramp
        # (auto mode), which otherwise still holds its last value.
        self._bright.reset()
        self._put_note(
            f"connected ({self.width}x{self.height}, "
            f"{self.transport} transport)")
        return dev

    def _reconnect(self, dev):
        """Re-establish the BLE link, backing off until it works or we stop.

        A lost panel must not kill the worker: the analysis and the GUI
        keep running while the link is retried with exponential backoff.
        """
        delay = self.reconnect_delay
        while not self.stop_event.is_set():
            self._put_note(f"reconnecting in {delay:.1f} s ...")
            if self.stop_event.wait(delay):
                break
            try:
                if dev is None:
                    new = Device(self.address, transport=self.transport,
                                 rhythm_style=self.rhythm_style,
                                 write_with_response=self.write_with_response,
                                 brightness=self.brightness,
                                 brightness_interval=self.brightness_interval)
                else:
                    dev.reconnect()
                    new = dev
            except Exception as e:
                self._put_note(f"reconnect failed ({e})")
                delay = min(delay * 2.0, 10.0)
                continue
            self.width, self.height = new.width, new.height
            # The link is fresh and set to the minimum: re-sync the ramp.
            self._bright.reset()
            self._put_note(f"reconnected ({self.width}x{self.height})")
            return new
        if dev is not None:
            dev.close()
        return None

    def _send(self, dev, payload):
        """Hand one payload to the device writer, reconnecting on failure.

        The BLE write runs on the transport's background thread, so this
        call returns immediately and the reported ``send_ms`` is the last
        COMPLETED write (None before the first one, or when the frame was
        throttled). A failure of an earlier write is re-raised by the
        ``update()`` call below, so the reconnect path still runs here.
        Returns ``(device, send_ms)``; the caller keeps the returned
        device (possibly a fresh connection).
        """
        if dev is None or not self._should_send():
            return dev, None
        try:
            dev.update(payload)
        except Exception as e:
            self._put_note(f"send failed ({e}) - reconnecting")
            return self._reconnect(dev), None
        self._mark_sent()
        return dev, dev.send_ms

    def _points_text(self, n_points):
        unit = "px" if self.transport == "png" else "levels"
        return f"{n_points} {unit}"

    def _payload(self, levels, mag_db):
        """Render one frame and build its transport payload.

        Shared by the live and test workers so both transports stay on
        exactly the same pipeline (render -> encode -> (n_points, bytes)).
        """
        img = render_image(levels, self.width, self.height)
        if self.transport == "png":
            n_points, payload = png_payload(img)
        else:
            n_points, payload = rhythm_payload(
                self._rhythm_levels(levels, mag_db), self.rhythm_style)
        return img, n_points, payload

    def _work_test(self):
        """Deterministic spectrum through the SAME pipeline as live audio:
        11 pulsing sines, one per band, so every bar sweeps up and down.
        No capture device needed - exercises render -> payload -> send.

        The sine stream is continuous in sample position, so the hop
        buffer (overlap) exercises exactly the same phase-continuity path
        as live audio.
        """
        self._set_rate(SAMPLE_RATE)
        self._put_note("test stimulus (11 pulsing sines, one per band)")
        masks = make_band_masks(self._device_rate, FRAME, N_BANDS, BAND_LO, BAND_HI)[1]
        full_scale = FRAME / 2.0
        state = [0.0] * N_BANDS

        # Center frequency of each band as a log-midpoint.
        edges = np.geomspace(BAND_LO, BAND_HI, N_BANDS + 1)
        freqs = np.sqrt(edges[:-1] * edges[1:])

        dev = self._connect_device()

        t_last = 0.0
        n = 0
        n_read = 0
        buf = np.zeros(FRAME, dtype=np.float64)
        try:
            while not self.stop_event.is_set():
                t = (n_read + FRAME) / self._device_rate  # new-sample time
                # Advance the stream exactly to the next hop position
                # (integer sample positions, so the hop rate is exact and
                # the sine stream is continuous like live capture).
                to_read = int(self.hop * (n + 1)) - n_read
                ts = np.arange(n_read, n_read + to_read) / self._device_rate
                n_read += to_read
                new = np.zeros(to_read, dtype=np.float64)
                for b in range(N_BANDS):
                    # dB amplitude breathes between -72 (level 0, bar off)
                    # and -12 (level 15, bar full) over a per-band period,
                    # so every bar sweeps the full range and off pixels
                    # are exercised.
                    db = -72.0 + 60.0 * (0.5 + 0.5 * np.sin(
                        2 * np.pi * t / (0.9 + 0.35 * b)
                        + b * 0.6))
                    amp = 10.0 ** (db / 20.0)
                    new += amp * np.sin(2 * np.pi * freqs[b] * ts)
                if to_read < FRAME:
                    buf[:-to_read] = buf[to_read:]
                buf[-to_read:] = new
                mag_db = self._analyze(buf, masks, full_scale)
                levels = self._levels(mag_db, state)
                img, n_points, payload = self._payload(levels, mag_db)

                dev, send_ms = self._send(dev, payload)
                dev = self._send_bright(dev, buf, levels)
                if send_ms is not None:
                    now = time.time()
                    if now - t_last >= 2.0:
                        self._put_note(
                            f"{self._points_text(n_points)} -> "
                            f"{send_ms:.1f} ms/update, "
                            f"{dev.dropped} dropped")
                        t_last = now

                self._put(
                    ("frame", mag_db, levels, img, send_ms, n_points))
                n += 1
                time.sleep(max(0.0, self.dt))
        finally:
            if dev is not None:
                dev.close()

    def _work_live(self):
        import pyaudiowpatch as py
        stream = None
        dev = None
        try:
            masks = make_band_masks(
                self._device_rate, FRAME, N_BANDS, BAND_LO, BAND_HI)[1]
            full_scale = FRAME / 2.0
            state = [0.0] * N_BANDS
            info = self._pyaudio.get_device_info_by_index(self._device_index)
            channels = 2 if int(info.get("maxInputChannels", 2) or 0) >= 2 else 1
            # Connect BEFORE opening the capture stream: the pre-connect
            # BLE scan can take seconds, and audio should not be consumed
            # (and dropped) while it runs.
            dev = self._connect_device()
            stream = self._pyaudio.open(
                input=True, input_device_index=self._device_index,
                channels=channels, rate=self._device_rate,
                format=py.paInt16, frames_per_buffer=FRAME)
            self._put_note(
                f"capturing {self._device_name} @ {self._device_rate} Hz")

            # Rolling hop buffer: every iteration reads the samples up to
            # the next hop position (a constant hop per second), shifts
            # them in, and analyzes the trailing 2048 samples. The pyaudio
            # read paces the loop at the hop rate. The frame counter is
            # shared with the test worker so the fractional hop is
            # truncated identically in both paths.
            n = 0
            n_read = 0
            buf = np.zeros(FRAME, dtype=np.float64)
            t_last = 0.0
            while not self.stop_event.is_set():
                t_iter_start = time.perf_counter()
                to_read = int(self.hop * (n + 1)) - n_read
                data = stream.read(to_read, exception_on_overflow=False)
                # Downmix ALL captured channels to mono. Keeping only the
                # left sample (the old mono[0::2]) discarded right-panned
                # content, so levels reacted to the left channel alone.
                samples = np.frombuffer(
                    data, dtype=np.int16).astype(np.float64) / 32768.0
                samples = samples[:to_read * channels]
                if channels > 1:
                    mono = samples.reshape(-1, channels).mean(axis=1)
                else:
                    mono = samples
                if to_read < FRAME:
                    buf[:-to_read] = buf[to_read:]
                buf[-to_read:] = mono[:to_read]
                n_read += to_read
                mag_db = self._analyze(buf, masks, full_scale)
                levels = self._levels(mag_db, state)
                img, n_points, payload = self._payload(levels, mag_db)

                dev, send_ms = self._send(dev, payload)
                dev = self._send_bright(dev, buf, levels)
                if send_ms is not None:
                    now = time.time()
                    if now - t_last >= 2.0:
                        self._put_note(
                            f"{self._points_text(n_points)} -> "
                            f"{send_ms:.1f} ms/update, "
                            f"{dev.dropped} dropped")
                        t_last = now

                # Loop-period watchdog: the loop (analysis rate) must stay
                # near the target dt. A large period means the worker was
                # stalled (audio read, BLE write, scheduler). With --fps
                # the panel writes are throttled *slower* than the loop;
                # without it, every iteration is one write, so the period
                # IS the panel frame interval. The BLE link stays up (no
                # disconnect in the log), so if the 1-2-frame revert
                # coincides with a large period, it is a stall, not a
                # link drop; if it happens at a normal period, the panel
                # is interrupting an in-flight frame (send-too-fast).
                period = time.perf_counter() - t_iter_start
                logger.debug(f"loop {period * 1000:.0f} ms "
                             f"(target {self.dt * 1000:.0f} ms)")
                if period > max(2.0 * self.dt, 0.05):
                    logger.warning(
                        f"frame interval {period * 1000:.0f} ms "
                        f"(target {self.dt * 1000:.0f} ms) - panel got no "
                        f"frame for that window")

                self._put(
                    ("frame", mag_db, levels, img, send_ms, n_points))
                n += 1
        except Exception as e:
            self._put(("err", e))
        finally:
            # Always release the stream and the BLE link, including when
            # the loop raises; the old in-try closes leaked both.
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    logger.debug("stream close failed", exc_info=True)
            if dev is not None:
                dev.close()

    def stop(self):
        self.stop_event.set()
        if self._worker is not None:
            self._worker.join(timeout=2)
        if self._pyaudio is not None:
            self._pyaudio.terminate()


def console_loop(eq):
    """No-GUI mode: print notes and the levels once per second."""
    print("capturing; Ctrl+C to stop")
    t_last = time.time()
    while not eq.stop_event.is_set():
        try:
            item = eq.q.get(timeout=0.1)
        except queue.Empty:
            continue
        kind = item[0]
        if kind == "err":
            print(f"error: {item[1]}")
            return
        if kind == "note":
            print(item[1])
            continue
        _, mag_db, levels, img, send_ms, n_points = item
        now = time.time()
        if now - t_last >= 1.0:
            extra = ""
            if send_ms is not None:
                extra = f"  ({eq._points_text(n_points)}, {send_ms:.1f} ms)"
            print(f"levels: {levels}{extra}")
            t_last = now
