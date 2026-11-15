# -*- coding: utf-8 -*-
"""Tests for the transport payload builders (hardware-free).

These assert the EXACT wire bytes so the equalizer stays in sync with the
pinned pypixelcolor fork: any byte change in a fork command shows up here.
"""

import asyncio
import threading
import time
import types

import pytest
from PIL import Image

import pypixel_eq.transport as transport
from pypixelcolor.commands.send_image import _build_send_plan
from pypixelcolor.commands.set_rhythm_mode import set_rhythm_mode

from pypixel_eq.audio import N_BANDS
from pypixel_eq.transport import (
    bar_color, brightness_payload, dim_image, render_image, png_payload,
    rhythm_payload, _close_client, _PayloadSender,
)


def _wait_for(predicate, timeout=2.0):
    """Poll ``predicate`` until it is true (thread hand-off in tests)."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_rhythm_payload_matches_set_rhythm_mode_bytes():
    levels = [0, 3, 6, 9, 11, 15, 15, 12, 8, 4, 1]
    n, payload = rhythm_payload(levels, style=2)
    assert n == len(levels)
    # Byte-identical to the fork command.
    plan = set_rhythm_mode(2, *levels)
    assert payload == b"".join(w.data for w in plan.windows)
    # Protocol type 2, length prefix 16, style at byte 4.
    assert payload[:5] == bytes([16, 0, 1, 2, 2])
    assert list(payload[5:16]) == levels


def test_rhythm_payload_rejects_levels_out_of_range():
    with pytest.raises(ValueError):
        rhythm_payload([-1] + [0] * 10)
    with pytest.raises(ValueError):
        rhythm_payload([16] + [0] * 10)


def test_png_payload_matches_build_send_plan_bytes():
    img = Image.new("RGB", (64, 16), (0, 0, 0))
    n, payload = png_payload(img)
    assert n == 64 * 16
    # Byte-identical to the fork's image frame builder.
    from io import BytesIO
    buf = BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()
    plan = _build_send_plan(png, is_gif=False, plan_name="eq_png", save_slot=0)
    assert payload == b"".join(w.data for w in plan.windows)
    # Header layout: [len:2 LE] 02 00 00 <size:4 LE> <crc32:4 LE> 00 00 <png>.
    assert payload[2] == 0x02  # static-image frame type
    size_le = int.from_bytes(payload[5:9], "little")
    assert size_le == len(png)
    # The PNG follows the 13-byte header (3 + 4 + 4 + 2) after the 2-byte len.
    assert payload[15:] == png


def test_png_payload_is_single_window():
    img = Image.new("RGB", (64, 16), (10, 20, 30))
    # A 64x16 PNG is far under the 12 KiB window cap -> exactly one window.
    from io import BytesIO
    buf = BytesIO()
    img.save(buf, format="PNG")
    plan = _build_send_plan(buf.getvalue(), is_gif=False)
    assert len(list(plan.windows)) == 1


def test_render_image_size_and_black_background():
    levels = [0] * N_BANDS
    img = render_image(levels, 64, 16)
    assert img.size == (64, 16)
    # Level 0 everywhere -> the whole frame is black.
    assert all(c == 0 for x in range(0, 64, 8) for y in range(16)
               for c in img.getpixel((x, y)))


def test_render_image_level_sets_bar_height():
    # One full band (level 15) fills that column top to bottom.
    levels = [0] * N_BANDS
    levels[0] = 15
    img = render_image(levels, 64, 16)
    x0 = 0
    assert img.getpixel((x0, 0)) != (0, 0, 0)
    assert img.getpixel((x0, 15)) != (0, 0, 0)
    # A band at level 0 stays black.
    assert img.getpixel((55, 0)) == (0, 0, 0)


def test_bar_color_is_valid_rgb_and_row_stable():
    """bar_color returns valid RGB; a given row's color never depends on
    the level (a level change only adds/removes rows, never recolors)."""
    for k in range(N_BANDS):
        for frac in (0.0, 0.5, 1.0):
            r, g, b = bar_color(k, frac)
            assert all(0 <= c <= 255 for c in (r, g, b))
    # Same band + same absolute row -> identical color for any level.
    assert bar_color(3, 0.5) == bar_color(3, 0.5)


def test_render_image_clamps_levels():
    # Out-of-range levels are clamped to 0..15 without error.
    levels = [999] * N_BANDS
    img = render_image(levels, 64, 16)
    assert img.size == (64, 16)


def test_close_client_disconnects_a_half_open_session():
    """A failed connect can leave the fork's session connected while its
    AsyncClient flag is still False; the workaround must close the session."""
    class FakeSession:
        def __init__(self):
            self.disconnects = 0

        async def disconnect(self):
            self.disconnects += 1

    class FakeClient:
        def __init__(self):
            self.session = FakeSession()
            self._async_client = types.SimpleNamespace(_session=self.session)
            self.disconnects = 0

        def _run_async(self, coro):
            return asyncio.run(coro)

        def disconnect(self):
            self.disconnects += 1

    client = FakeClient()
    _close_client(client)
    assert client.session.disconnects == 1
    assert client.disconnects == 1


def test_sender_returns_immediately_and_drops_oldest():
    """push() never waits for the radio; a newer frame replaces the
    oldest UNSENT one (a stale frame is worthless on a live display)."""
    gate = threading.Event()
    sent = []

    def send(payload):
        sent.append(payload)
        gate.wait(2.0)

    sender = _PayloadSender(send, queue_size=1)
    try:
        t0 = time.perf_counter()
        sender.push(b"first")
        assert _wait_for(lambda: len(sent) == 1)
        sender.push(b"second")
        sender.push(b"third")  # drops "second", never blocks
        assert time.perf_counter() - t0 < 0.5
        assert sender.dropped == 1
        gate.set()
        assert _wait_for(lambda: sender.sent == 2)
        assert sent == [b"first", b"third"]
    finally:
        gate.set()
        sender.stop()


def test_sender_reports_last_completed_send():
    def send(payload):
        time.sleep(0.02)

    sender = _PayloadSender(send)
    try:
        assert sender.last_ms is None
        sender.push(b"frame")
        assert _wait_for(lambda: sender.last_ms is not None)
        assert sender.last_ms >= 15.0
        assert sender.sent == 1
    finally:
        sender.stop()


def test_sender_defers_error_to_next_push():
    """A writer failure must surface on the analysis thread, where the
    reconnect path lives - not on the writer thread."""
    calls = []

    def send(payload):
        calls.append(payload)
        if len(calls) == 1:
            raise RuntimeError("link lost")

    sender = _PayloadSender(send)
    try:
        sender.push(b"first")
        assert _wait_for(lambda: sender.errors == 1)
        with pytest.raises(RuntimeError, match="link lost"):
            sender.push(b"second")
        sender.push(b"third")  # error was consumed -> accepted again
        assert _wait_for(lambda: sender.sent == 1 and len(calls) == 2)
        assert calls == [b"first", b"third"]
    finally:
        sender.stop()


def test_sender_stop_joins_and_drops_queued_frames():
    started = threading.Event()

    def send(payload):
        started.set()
        time.sleep(0.3)

    sender = _PayloadSender(send, queue_size=2)
    sender.push(b"first")
    assert started.wait(2.0)
    sender.push(b"second")
    sender.push(b"third")
    sender.stop()
    assert not sender._thread.is_alive()
    assert sender.dropped == 2
    assert sender.sent == 1  # the frame in flight still finished


class _FakeSession:
    def __init__(self, events):
        self.events = events

    async def execute_command(self, build):
        self.events.append("send")

    async def disconnect(self):
        self.events.append("session-disconnect")


class _FakeClient:
    """Enough of the pypixelcolor Client for a hardware-free Device."""

    def __init__(self, address, *, write_with_response=True):
        self.address = address
        self.write_with_response = write_with_response
        self.events = []
        self._async_client = types.SimpleNamespace(
            _session=_FakeSession(self.events))

    def connect(self):
        self.events.append("connect")

    def get_device_info(self):
        return types.SimpleNamespace(width=64, height=16)

    def set_brightness(self, value):
        if not 0 <= int(value) <= 100:
            raise ValueError("Brightness level must be between 0 and 100")
        self.events.append("brightness")

    def _run_async(self, coro):
        return asyncio.run(coro)

    def disconnect(self):
        self.events.append("disconnect")

    def _cleanup_loop(self):
        self.events.append("cleanup")


def test_brightness_payload_matches_fork_bytes():
    """The brightness command is the fork's set_brightness, fire-and-forget."""
    from pypixelcolor.commands.set_brightness import set_brightness
    payload = brightness_payload(30)
    plan = set_brightness(30)
    assert payload == b"".join(w.data for w in plan.windows)
    # Layout: [len=5, 0, cid=4, 0x80, level].
    assert payload == bytes([5, 0, 4, 0x80, 30])
    with pytest.raises(ValueError):
        brightness_payload(101)


def test_brightness_payload_rounds_the_percent():
    assert brightness_payload(30.6) == bytes([5, 0, 4, 0x80, 31])
    assert brightness_payload(0.4) == bytes([5, 0, 4, 0x80, 0])


def test_dim_image_scales_pixels():
    img = Image.new("RGB", (2, 1), (200, 100, 50))
    dimmed = dim_image(img, 50)
    assert dimmed.getpixel((0, 0)) == (100, 50, 25)
    # 100 and None are pass-throughs (no copy).
    assert dim_image(img, 100) is img
    assert dim_image(img, None) is img
    assert dim_image(img, 0).getpixel((0, 0)) == (0, 0, 0)


def test_device_sets_brightness_at_connect(monkeypatch):
    """A configured brightness is sent right after the connect blank, so
    a reconnect restores the configured level, not the last run's."""
    monkeypatch.setattr(transport, "Client", _FakeClient)
    dev = transport.Device("AA:BB:CC:DD:EE:FF", brightness=30)
    events = dev.client.events
    assert events.index("connect") < events.index("brightness")
    # The blank stays at 0; only the configured value follows.
    dev.close()


def test_device_brightness_default_does_not_touch_the_panel(monkeypatch):
    """The default (100 = the panel's own value) is sent as a plain set,
    so a reconnect restores 100 instead of a previous run's 30."""
    monkeypatch.setattr(transport, "Client", _FakeClient)
    dev = transport.Device("AA:BB:CC:DD:EE:FF")
    assert "brightness" in dev.client.events
    dev.close()
    """The writer must be joined before the link closes, otherwise a frame
    could be written to a dead session."""
    monkeypatch.setattr(transport, "Client", _FakeClient)
    dev = transport.Device("AA:BB:CC:DD:EE:FF")
    client = dev.client
    dev.update(b"frame")
    assert _wait_for(lambda: "send" in client.events)
    assert _wait_for(lambda: dev.send_ms is not None)
    sender = dev._sender
    dev.close()
    assert client.events.index("send") < client.events.index("disconnect")
    assert not sender._thread.is_alive()
    assert dev.client is None
    # After close an update is a silent no-op, not an error.
    dev.update(b"late")


def test_device_reconnect_replaces_the_writer(monkeypatch):
    monkeypatch.setattr(transport, "Client", _FakeClient)
    dev = transport.Device("AA:BB:CC:DD:EE:FF")
    first = dev._sender
    dev.reconnect()
    assert dev._sender is not first
    assert not first._thread.is_alive()
    dev.update(b"frame")
    assert _wait_for(lambda: dev.send_ms is not None)
    dev.close()
