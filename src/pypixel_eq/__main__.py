# -*- coding: utf-8 -*-
"""CLI entry point for the audio-reactive equalizer.

Run:
    python -m pypixel_eq [-a <MAC>] [--gui]
                         [--transport png|rhythm] [--rhythm-style 0]
                         [--fps F] [--overlap N] [--queue N]
                         [--attack S] [--release S] [--gain-percentile P]
                         [--band-stat peak|rms] [--tilt DB_PER_OCT]
                         [--brightness PCT] [--no-response] [--test]
                         [--verbose]

  -a <MAC>            send to the panel
  --gui               live view (bands + panel emulation)
  --transport         device transport (default rhythm)
  --rhythm-style N    rhythm-transport panel preset 0..4 (default 0)
  --fps F             throttle device writes to F frames/s (default: every
                      frame for rhythm, 10 fps for png - the rate the image
                      path stays stable at; analysis rate unchanged)
  --overlap N         panel update rate: 23.4 Hz x (1+N), N = 0..2
                      (default 0; each level re-analyzes the rolling
                      2048-sample window more often, keeping the same
                      frequency resolution)
  --queue N           frames that may wait for the BLE write (default 1;
                      the oldest unsent frame is dropped when full)
  --attack S          seconds for a band to rise to a louder level
                      (default 0.001 = instant)
  --release S         seconds for a band to fall back (default 0.05;
                      raise it for a slower, smoother decay)
  --gain-percentile P which value of the recent gain window maps to
                      level 15: 100 = the maximum (default), 95 ignores
                      the loudest 5 % of frames so a single hit no longer
                      squashes every bar
  --band-stat MODE    per-band value: peak (default, the loudest bin) or
                      rms (total band power: steadier bars, broadband
                      content lifts the wide upper bands)
  --tilt N            fixed spectral tilt in dB per octave around 1 kHz
                      (default 0). Positive = brighter (highs up, bass
                      down), negative = warmer; clamped to +/-12 dB per
                      band
  --brightness PCT    panel brightness in % (default 100 = the panel's
                      own default). Set at connect and after every
                      reconnect - the panel keeps its last display
                      across links, so this is also how a re-run
                      restores the brightness
  --no-response       write BLE chunks without a link-layer response
                      (experiment: lower latency, less reliable)
  --test              feed a synthetic 11-band stimulus instead of
                      capturing system audio (no audio device needed)
  --verbose           DEBUG logging (BLE traffic, sends, ACKs)
"""
import argparse
import logging
import sys

from pypixelcolor.lib.logging import setup_logging

from .audio import ATTACK, RELEASE
from .equalizer import Equalizer, console_loop

logger = logging.getLogger(__name__)

#: The image path (``png`` transport) is stable up to ~10 fps on the 64x16
#: panel; at the link's own ~11.5 fps frames are dropped/redrawn, which is
#: visible as flicker. Hold this rate unless the user asks for one.
PNG_SEND_HZ = 10.0


def resolve_send_hz(transport, fps):
    """Panel write rate: ``png`` defaults to the stable rate, rhythm to max."""
    if fps > 0:
        return fps
    return PNG_SEND_HZ if transport == "png" else fps


def build_parser():
    """Build the CLI parser (kept separate so tests can check defaults)."""
    parser = argparse.ArgumentParser(description="Audio-reactive equalizer")
    parser.add_argument("-a", "--address",
                        help="BLE address; without it nothing is sent to the panel")
    parser.add_argument("--gui", action="store_true",
                        help="show a live tkinter view of the bands + panel emulation")
    parser.add_argument("--transport",
                        choices=("png", "rhythm"), default="rhythm",
                        help="device transport (default rhythm: panel-drawn "
                             "native visualizer; png: true full replace)")
    parser.add_argument("--rhythm-style", type=int, default=0,
                        help="rhythm-transport panel preset 0..4 (default 0)")
    parser.add_argument("--fps", type=float, default=0.0,
                        help="throttle device writes to this frame rate "
                             "(0 = every frame for rhythm, link-limited ~22 fps; "
                             f"the stable {PNG_SEND_HZ:g} fps for png; "
                             "analysis rate unchanged)")
    parser.add_argument("--overlap", type=int, default=0,
                        help="panel update rate: 23.4 Hz x (1+N), "
                             "N = 0..2 (default 0 = 23.4 Hz; each level "
                             "re-analyzes the rolling 2048-sample window "
                             "more often without losing frequency resolution; "
                             "3+ overloads the panel: high bands get damped)")
    parser.add_argument("--queue", type=int, default=1,
                        help="frames that may wait for the BLE write "
                             "(default 1; the oldest unsent frame is "
                             "dropped when the queue is full)")
    parser.add_argument("--attack", type=float, default=ATTACK,
                        help=f"seconds for a band to rise to a louder level "
                             f"(default {ATTACK:g} s = instant)")
    parser.add_argument("--release", type=float, default=RELEASE,
                        help=f"seconds for a band to fall back to a quieter "
                             f"level (default {RELEASE:g} s; raise it for a "
                             f"slower, smoother decay)")
    parser.add_argument("--no-response", dest="write_with_response",
                        action="store_false", default=True,
                        help="write BLE chunks without a link-layer "
                             "response (experiment: lower latency, less "
                             "reliable)")
    parser.add_argument("--auto-gain", dest="auto_gain", action="store_true",
                        default=True,
                        help="scale the bars to the recent max over all "
                             "bands (default on)")
    parser.add_argument("--no-auto-gain", dest="auto_gain", action="store_false",
                        help="use the fixed -70/-20 dB span (no auto-gain)")
    parser.add_argument("--gain-window", type=float, default=2.0,
                        help="seconds of history for the auto-gain max "
                             "(default 2; the recent peak normalizes the "
                             "bars as a whole)")
    parser.add_argument("--gain-percentile", type=float, default=100.0,
                        help="which value of the gain window maps to level 15 "
                             "(default 100 = the maximum; 95 ignores the "
                             "loudest 5 %% of frames so a single transient "
                             "no longer squashes every bar)")
    parser.add_argument("--band-stat", choices=("peak", "rms"),
                        default="peak",
                        help="per-band value: peak (default, the loudest bin, "
                             "tracks the strongest component) or rms (total "
                             "band power: steadier bars, broadband content "
                             "lifts the wide upper bands)")
    parser.add_argument("--tilt", type=float, default=0.0,
                        help="fixed spectral tilt in dB per octave around "
                             "1 kHz (default 0; positive = brighter, highs up "
                             "and bass down; negative = warmer; each band is "
                             "clamped to +/-12 dB)")
    parser.add_argument("--brightness", type=int, default=100,
                        help="panel brightness in %% (default 100 = the "
                             "panel's own default); set at connect and "
                             "after every reconnect")
    parser.add_argument("--test", action="store_true",
                        help="feed a synthetic 11-band stimulus instead of "
                             "capturing system audio (no audio device needed)")
    parser.add_argument("--verbose", action="store_true",
                        help="DEBUG logging: BLE writes, notify frames, "
                             "connection state (diagnoses drop/revert glitches)")
    return parser


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    args = build_parser().parse_args()
    if not (0 <= args.rhythm_style <= 4):
        sys.exit("--rhythm-style must be 0..4")
    if args.fps < 0:
        sys.exit("--fps must be >= 0")
    if args.gain_window <= 0:
        sys.exit("--gain-window must be > 0")
    if not (0 < args.gain_percentile <= 100):
        sys.exit("--gain-percentile must be in (0, 100]")
    if not (0 <= args.overlap <= 2):
        sys.exit("--overlap must be 0..2")
    if args.queue < 1:
        sys.exit("--queue must be >= 1")
    if args.attack <= 0:
        sys.exit("--attack must be > 0")
    if args.release <= 0:
        sys.exit("--release must be > 0")
    if abs(args.tilt) > 6:
        sys.exit("--tilt must be within +/-6 dB per octave")
    if not 0 <= args.brightness <= 100:
        sys.exit("--brightness must be 0..100")
    # Always log at INFO so a "BLE device disconnected" event is visible
    # without any flag; --verbose adds DEBUG (wire traffic, notifies).
    setup_logging("DEBUG" if args.verbose else "INFO")

    send_hz = resolve_send_hz(args.transport, args.fps)
    if args.transport == "png" and args.fps <= 0:
        logger.info(f"png transport: holding {send_hz:g} fps (stable image "
                    "rate; pass --fps to override)")

    eq = Equalizer(args.address, transport=args.transport,
                   rhythm_style=args.rhythm_style, send_hz=send_hz,
                   auto_gain=args.auto_gain, gain_window=args.gain_window,
                   gain_percentile=args.gain_percentile,
                   band_stat=args.band_stat, tilt=args.tilt,
                   brightness=args.brightness,
                   overlap=args.overlap, queue_size=args.queue,
                   write_with_response=args.write_with_response,
                   attack=args.attack, release=args.release)
    try:
        eq.start(test=args.test)
    except Exception as e:
        sys.exit(f"setup failed: {e}")
    try:
        if args.gui:
            from .gui import EQGui
            EQGui(eq).run()
        else:
            console_loop(eq)
    except KeyboardInterrupt:
        pass
    finally:
        eq.stop()


if __name__ == "__main__":
    main()
