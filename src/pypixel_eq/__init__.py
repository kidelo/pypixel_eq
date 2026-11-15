# -*- coding: utf-8 -*-
"""Audio-reactive equalizer for the iPixel Color panel.

Captures the system audio output (Windows WASAPI loopback / PulseAudio
sink monitor), analyzes it into 11 log-spaced frequency bands, and
streams the result to the panel over one of two transports:

  png     Render a 64x16 frame and send it as a static-image frame -
         a TRUE full replace of the panel (default).
  rhythm  Send only the 11 band levels to the panel's native music
         visualizer; the panel draws the bars itself (~16 bytes/update).

Both transports are fire-and-forget (no panel protocol ACK is awaited).

Levels are auto-gained (on by default): the recent max over ALL bands
in a rolling window is mapped to level 15, so the loudest band is at
the top and every other bar stays proportionally scaled; the floor
stays fixed at -70 dB.

The analysis loop re-analyzes a rolling 2048-sample window every
FRAME/(1+overlap) samples (--overlap 0-2): 23.4 Hz at 0, up to 70.3 Hz
at 2 (panel limit - higher rates overload it), so the panel can be
driven faster without losing frequency resolution.

Modules:
    audio      audio capture + FFT -> 11 band levels + AutoCeiling
    transport  payload builders + fire-and-forget Device sender
    equalizer  worker-thread engine (capture -> render -> send)
    gui        live tkinter view (bands + panel emulation)
"""
