# -*- coding: utf-8 -*-
"""Live tkinter view: band bars + a panel emulation (clear + full redraw,
like the panel itself)."""
from __future__ import annotations

import queue
import tkinter as tk

from PIL import Image, ImageTk

from .audio import N_BANDS, BAND_LO, BAND_HI
from .transport import bar_color, dim_image


class EQGui:
    """Live tkinter view: band bars + a 64x16 panel emulation (clear +
    full redraw, same as the device)."""

    def __init__(self, eq):
        self.eq = eq
        self.root = tk.Tk()
        self.root.title("iPixel Equalizer")
        self.status = tk.StringVar(value="starting...")
        self._build_widgets()
        self.root.after(40, self._tick)

    def _build_widgets(self):
        # band bars
        self.bars = tk.Canvas(self.root, width=660, height=150,
                              bg="#101418", highlightthickness=0)
        self.bars.pack(padx=12, pady=(12, 4))
        import numpy as np
        edges = np.geomspace(BAND_LO, BAND_HI, N_BANDS + 1)
        w, h = 660, 150
        margin_l, margin_b, margin_t = 46, 24, 14
        plot_w = w - margin_l - 16
        plot_h = h - margin_b - margin_t
        gap = 8
        bar_w = int((plot_w - gap * (N_BANDS - 1)) / N_BANDS)
        for lvl in range(0, 16, 3):
            y = margin_t + plot_h - lvl / 15.0 * plot_h
            self.bars.create_line(margin_l, y, margin_l + plot_w, y, fill="#2a3138")
            self.bars.create_text(6, y, anchor="w", text=f"{lvl}",
                                  fill="#6b7480", font=("Consolas", 9))
        self.bar_ids = []
        self.level_labels = []
        for k in range(N_BANDS):
            x0 = margin_l + k * (bar_w + gap)
            center = (edges[k] * edges[k + 1]) ** 0.5
            self.bars.create_text(x0 + bar_w / 2, h - 10, anchor="s",
                                  text=f"{int(center)} Hz", fill="#6b7480",
                                  font=("Consolas", 8))
            self.bar_ids.append(self.bars.create_rectangle(
                x0, margin_t, x0 + bar_w, margin_t + plot_h, fill="#1c2228"))
            self.level_labels.append(self.bars.create_text(
                x0 + bar_w / 2, margin_t + plot_h + 8, text="", fill="#e8edf2",
                font=("Consolas", 10)))
        self.plot_h = plot_h
        self.y_top = margin_t

        # device emulation: cleared + full redraw (like the panel)
        tk.Label(self.root, text="panel (emulation, full redraw)",
                 fg="#6b7480").pack()
        self.dev = tk.Canvas(self.root, width=512, height=128,
                             bg="black", highlightthickness=0)
        self.dev.pack(pady=(0, 4))
        self._dev_item = self.dev.create_image(256, 64, anchor="center")
        self._dev_photo = ImageTk.PhotoImage(
            Image.new("RGB", (64, 16), (0, 0, 0)).resize(
                (64 * 8, 16 * 8), Image.NEAREST), master=self.root)
        self.dev.itemconfig(self._dev_item, image=self._dev_photo)

        tk.Label(self.root, textvariable=self.status, fg="#9aa4b0").pack(pady=(0, 8))

    def _render_bars(self, levels):
        for k in range(N_BANDS):
            frac = levels[k] / 15.0
            y = self.y_top + self.plot_h - frac * self.plot_h
            x0, _, x1, _ = self.bars.coords(self.bar_ids[k])
            self.bars.coords(self.bar_ids[k], x0, y, x1, self.y_top + self.plot_h)
            self.bars.itemconfig(
                self.bar_ids[k],
                fill="#%02x%02x%02x" % bar_color(k, levels[k] / 15.0))
            self.bars.itemconfig(self.level_labels[k], text=str(levels[k]))

    def _render_device(self, img):
        # Show the emulated panel at the brightness the real one runs at:
        # the panel dims itself, so the sent frame is untouched.
        scale = max(1, min(512 // img.width, 128 // img.height))
        scaled = dim_image(img, self.eq.brightness).resize(
            (img.width * scale, img.height * scale), Image.NEAREST)
        self._dev_photo = ImageTk.PhotoImage(scaled, master=self.root)
        self.dev.itemconfig(self._dev_item, image=self._dev_photo)

    def _tick(self):
        try:
            try:
                item = self.eq.q.get_nowait()
            except queue.Empty:
                return
            kind = item[0]
            if kind == "err":
                self.status.set(f"error: {item[1]}")
            elif kind == "note":
                self.status.set(item[1])
            else:
                _, _mag_db, levels, img, send_ms, n_points = item
                self._render_bars(levels)
                self._render_device(img)
                if send_ms is not None:
                    self.status.set(
                        f"{self.eq.transport}, "
                        f"{self.eq._points_text(n_points)}, "
                        f"{send_ms:.0f} ms/update")
                else:
                    self.status.set("display only")
        finally:
            # Always reschedule: an error (or a render failure) must not
            # stop the tick loop, or the window freezes for good.
            self.root.after(40, self._tick)

    def run(self):
        self.root.mainloop()
