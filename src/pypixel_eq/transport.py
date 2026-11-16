# -*- coding: utf-8 -*-
"""Device transport for the equalizer: payload builders and a
non-blocking sender.

Both transports produce wire payloads via the pinned pypixelcolor fork and
are sent WITHOUT waiting for a panel protocol ACK:

- ``png``: one static-image frame per update - a TRUE full replace of the
  panel, so nothing from a previous frame survives. The panel does ACK
  image frames on the wire, but the plan is wrapped with ACKs off so the
  ACK NOTIFY is never waited on (no round trips, no 8 s stalls).
- ``rhythm``: only the 11 band levels (0..15) - the panel's NATIVE music
  visualizer draws the bars itself from the level stream (~16 bytes/update,
  the lightest transport). It is a live layer: the bars are visible only
  while levels stream, and a gap reverts the panel to its saved show.

A BLE write blocks for tens of milliseconds, so ``Device.update()`` only
hands the frame to a background writer: the analysis loop keeps its rate
and the panel runs at whatever rate the link sustains. Frames are live
data - when the writer cannot keep up, the oldest unsent frame is dropped.
"""
from __future__ import annotations

import colorsys
import logging
import queue
import threading
import time
from io import BytesIO

from PIL import Image, ImageDraw

from pypixelcolor import Client
from pypixelcolor.commands.send_image import _build_send_plan
from pypixelcolor.commands.set_brightness import set_brightness
from pypixelcolor.commands.set_rhythm_mode import set_rhythm_mode
from pypixelcolor.lib.transport.send_plan import SendPlan, Window
from pypixelcolor.lib.transport.ack_manager import AckPolicy

from .audio import N_BANDS

logger = logging.getLogger(__name__)


def _close_client(client):
    """Best-effort disconnect that also handles a half-open session.

    The fork's ``DeviceSession.connect()`` marks itself connected before
    fetching device info, while ``AsyncClient`` only flips its own flag
    afterwards. If that fetch fails, ``AsyncClient.disconnect()`` returns
    early ("not connected") and the Bleak client stays open. Calling the
    session's disconnect directly closes it; this is a local workaround
    so the equalizer does not leak a connection the submodule leaves
    half-open.
    """
    if client is None:
        return
    async_client = getattr(client, "_async_client", None)
    session = getattr(async_client, "_session", None)
    run_async = getattr(client, "_run_async", None)
    if session is not None and run_async is not None:
        try:
            run_async(session.disconnect())
        except Exception:
            pass
    try:
        client.disconnect()
    except Exception:
        pass
    # Stop the wrapper's event-loop thread; otherwise every reconnect
    # leaves a live (daemon) loop thread behind.
    cleanup_loop = getattr(client, "_cleanup_loop", None)
    if cleanup_loop is not None:
        try:
            cleanup_loop()
        except Exception:
            pass


class _PayloadSender:
    """Single background writer: one frame in flight, newest frame wins.

    ``push()`` never blocks on the radio. If a newer payload arrives while
    the writer is busy, the oldest *unsent* payload is dropped - a stale
    frame is worthless on a live display. A send error is remembered and
    re-raised by the next ``push()``, so the caller's reconnect path runs
    on the analysis thread while this writer simply waits for the new
    link.
    """

    def __init__(self, send_fn, queue_size=1):
        self._send_fn = send_fn
        self._queue = queue.Queue(maxsize=max(1, int(queue_size)))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._error = None
        self.sent = 0
        self.dropped = 0
        self.errors = 0
        self.last_ms = None
        self._thread = threading.Thread(
            target=self._run, name="pixel-sender", daemon=True)
        self._thread.start()

    def push(self, payload):
        """Queue one payload; raise the error of a failed send, if any."""
        if self._stop.is_set():
            return
        with self._lock:
            error, self._error = self._error, None
        if error is not None:
            raise error
        while True:
            try:
                self._queue.put_nowait(payload)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    pass

    def stop(self, timeout=2.0):
        """Stop the writer and drop whatever is still queued."""
        self._stop.set()
        self._thread.join(timeout)
        self._drain()

    def _run(self):
        while not self._stop.is_set():
            try:
                payload = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            with self._lock:
                if self._error is not None:
                    # Stale frame from before the failure; never retry it.
                    continue
            t0 = time.perf_counter()
            try:
                self._send_fn(payload)
            except Exception as e:
                with self._lock:
                    self._error = e
                self.errors += 1
                self._drain()
                continue
            self.last_ms = (time.perf_counter() - t0) * 1000.0
            self.sent += 1

    def _drain(self):
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return
            self.dropped += 1


def bar_color(band, frac):
    """Band color: fixed hue per band, brightness by absolute row.

    ``frac`` is the row position from the bottom (0..1). A level change
    therefore never recolors an existing row, only adds/removes rows.
    """
    hue = 0.72 - 0.70 * (band / max(1, N_BANDS - 1))  # blue .. red
    v = max(0.30, 0.35 + 0.65 * frac)
    r, g, b = colorsys.hsv_to_rgb(hue, 0.85, v)
    return (int(r * 255), int(g * 255), int(b * 255))


def render_image(levels, width, height):
    """Render one device frame: black background, one gradient bar per band.

    ``levels`` is one 0..15 value per band. The bar width is the panel
    width split evenly across the 11 bands; a level sets the bar's height
    in rows, colored by :func:`bar_color` per absolute row.
    """
    img = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    edges = [round(width * k / N_BANDS) for k in range(N_BANDS + 1)]
    for k, level in enumerate(levels):
        level = max(0, min(15, int(level)))
        h = int(round(level / 15.0 * height))
        if h == 0:
            continue
        x0, x1 = edges[k], edges[k + 1] - 1
        y_top = height - h
        for y in range(y_top, height):
            frac = y / max(1, height - 1)  # absolute row -> absolute color
            draw.line([x0, y, x1, y], fill=bar_color(k, frac))
    return img


def png_payload(img):
    """Build the static-image (PNG) payload for ``img`` - a TRUE full
    replace of the panel.

    Byte-identical to ``pypixelcolor.commands.send_image._build_send_plan``
    for a single window: ``[len] 02 00 00 <size:4 LE> <crc32:4 LE> 00 00 <png>``.
    The PNG is encoded in memory; a 64x16 RGB PNG is one window, well under
    the 12 KiB cap. Returns (n_points, payload); the PNG covers the whole
    panel, so n_points = width * height.
    """
    buf = BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()
    # Reuse the package's frame builder so the bytes stay in sync with
    # send_image; the plan's ACK flags are forced off at send time.
    plan = _build_send_plan(png, is_gif=False, plan_name="eq_png", save_slot=0)
    payload = b"".join(w.data for w in plan.windows)
    n = img.width * img.height
    return n, payload


def brightness_payload(percent):
    """Build the panel brightness command for ``percent`` (0..100).

    Wire format: ``[5, 0, 4, 0x80, level]`` - the fork's
    ``commands.set_brightness``. It dims the whole panel, so unlike the
    image path it also works for the rhythm transport (the panel draws
    its own bars and there is no colour to scale). The command object
    asks for an ACK; the plan is re-wrapped fire-and-forget at send time.
    """
    plan = set_brightness(int(round(percent)))
    return b"".join(w.data for w in plan.windows)


def dim_image(img, percent):
    """Return ``img`` scaled to ``percent`` brightness (for the G-U-I only).

    The panel dims itself when a brightness command is used, so the frame
    that is sent stays untouched; the live view shows the emulated panel
    at the brightness the real one is running at.
    """
    if percent is None or percent >= 100:
        return img
    factor = max(0.0, min(100.0, float(percent))) / 100.0
    return Image.eval(img, lambda v: int(v * factor))


def rhythm_payload(levels, style=0):
    """Build the native music-visualizer frame for ``levels`` (11 values,
    0..15) - protocol type 2, ``[16,0,1,2,<style>,l1..l11]``.

    The panel draws the bars itself from the level stream, so only the 11
    level bytes are sent. The command sets requires_ack=False, but like the
    image path the panel does not ACK these; we re-wrap fire-and-forget at
    send time. Returns (n_points, payload) with n_points = number of
    levels.
    """
    plan = set_rhythm_mode(style, *[int(lv) for lv in levels])
    payload = b"".join(w.data for w in plan.windows)
    return len(levels), payload


class Device:
    """Sends full-frame payloads to the panel (fire-and-forget, no acks).

    transport "png": one static-image frame (true full replace).
    transport "rhythm": 11 band levels to the panel's native visualizer
        (the panel draws the bars; a live layer - reverts on a gap).

    Writes run on a background thread (see :class:`_PayloadSender`):
    ``update()`` never blocks on the radio, ``send_ms`` reports the last
    completed send, and a failed send surfaces on the next ``update()``.
    """

    def __init__(self, address, transport="png", rhythm_style=0,
                 queue_size=1, write_with_response=True, brightness=100):
        self.address = address
        self.transport = transport
        self.rhythm_style = rhythm_style
        self.queue_size = max(1, int(queue_size))
        self.write_with_response = bool(write_with_response)
        # Panel brightness (0..100) to set at connect and after every
        # reconnect. None = don't touch it (the panel keeps its last value).
        self.brightness = brightness
        self._sender = None
        # Chunks are written with response (the package default): neither
        # transport produces a protocol ACK we wait for (rhythm never
        # does; the image plan's ACK wait is forced off below), so the
        # link-layer round trip costs little and keeps the radio healthy.
        self.client = self._open_client(brightness)
        self._sender = _PayloadSender(self._write_payload, self.queue_size)
        # A second writer for the panel brightness command: one BLE write
        # per change (the ramp throttles these to actual steps), and it
        # rides its own latest-wins slot so it can never be dropped by the
        # frame queue. It shares the client/session (and the error path).
        self._bright_sender = _PayloadSender(self._write_brightness, 1)

    def _open_client(self, brightness=0):
        """Open a fresh client, read the panel size, blank the display.

        The panel keeps its last display across BLE links, so connect
        blanks it: brightness 0, then - when a value is configured - the
        panel's brightness set to it (100 is the panel's own default, so
        passing it back is harmless and makes a reconnect restore the
        configured brightness instead of whatever the last run left).
        """
        client = Client(self.address,
                        write_with_response=self.write_with_response)
        try:
            client.connect()
            info = client.get_device_info()
            client.set_brightness(0)
            if brightness is not None and brightness != 0:
                client.set_brightness(int(brightness))
        except Exception:
            _close_client(client)
            raise
        self.width, self.height = info.width, info.height
        self._session = client._async_client._session
        return client

    def _write_payload(self, payload):
        """Blocking write of one full-frame payload (runs on the writer)."""
        self._execute(payload)

    def _write_brightness(self, percent):
        """Blocking write of one panel brightness command (its writer)."""
        self._execute(brightness_payload(percent))

    def _execute(self, payload):
        """One no-ACK command over the shared session (writer thread)."""
        client, session = self.client, self._session
        if client is None or session is None:
            raise ConnectionError("device is closed")
        plan = SendPlan(
            "eq_" + self.transport,
            [Window(data=payload, requires_ack=False)],
            ack_policy=AckPolicy(ack_per_window=False, ack_final=False),
        )

        def build(*a, **k):
            return plan

        client._run_async(session.execute_command(build))

    def reconnect(self):
        """Drop the BLE link and open a fresh client, refreshing the size.

        A fresh ``Client`` (rather than reusing a wedged session) is the
        reliable recovery path: the old event-loop thread is left to its
        atexit cleanup and the new client starts from a clean state. The
        writers are stopped before the old link closes and restarted after
        the new one is up, so no frame is ever written to a dead session.
        """
        self.close()
        self.client = self._open_client(self.brightness)
        self._sender = _PayloadSender(self._write_payload, self.queue_size)
        self._bright_sender = _PayloadSender(self._write_brightness, 1)

    def update(self, payload):
        """Queue one full-frame payload; returns immediately.

        The frame replaces any unsent frame (latest wins), so the caller
        keeps its analysis rate while the panel runs at the link's rate.
        A send failure is re-raised here on the next call; the caller's
        reconnect path then runs unchanged.
        """
        if not payload or self.client is None or self._sender is None:
            return
        self._sender.push(payload)

    def push_brightness(self, percent):
        """Queue a panel brightness command (0..100); returns immediately.

        Runs on its own background writer (one write per actual change -
        the caller only calls this when the ramp stepped), so it is not
        throttled by the frame rate and cannot be dropped by the frame
        queue. A failed write is re-raised on the next call, so the
        reconnect path still runs on the analysis thread.
        """
        if self.client is None or self._bright_sender is None:
            return
        self._bright_sender.push(percent)

    @property
    def send_ms(self):
        """Duration of the last completed write, or None before the first."""
        if self._sender is None:
            return None
        return self._sender.last_ms

    @property
    def dropped(self):
        """Frames dropped because the link was slower than the analysis."""
        if self._sender is None:
            return 0
        return self._sender.dropped

    def close(self):
        sender, self._sender = self._sender, None
        if sender is not None:
            sender.stop()
        bright_sender, self._bright_sender = self._bright_sender, None
        if bright_sender is not None:
            bright_sender.stop()
        client, self.client = self.client, None
        self._session = None
        if client is None:
            return
        _close_client(client)
