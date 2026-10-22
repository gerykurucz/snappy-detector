"""Command line front end.

    python -m snapdet live --source screen --backend dxcam \
            --capture circle --radius 80 --publish udp://127.0.0.1:5005
    python -m snapdet live --source synth --view window
    python -m snapdet image --source shot.jpg --out shot_debug.jpg
    python -m snapdet bench

``live`` is the product: a real-time loop with the debug overlay drawn
LIVE on your screen - a transparent, borderless, always-on-top window
sitting exactly over the captured radius region (``--view overlay``,
the default), an OpenCV viewer as fallback (``--view window``), or
headless for integration (``--view none``).  Nothing is recorded; the
only file output is the optional ``--json`` track log.

``image`` analyses a single photograph with a full sweep of every zoom
grid - slow, maximum recall, for checking what the engine can see.
``bench`` measures this machine and suggests budgets.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import numpy as np

from .config import Config
from .engine import SnapEngine
from .debug import draw_debug


def _build_engine(args) -> SnapEngine:
    over = {}
    if getattr(args, "no_motion", False):
        over["motion_enabled"] = False
    if getattr(args, "no_sweep", False):
        over["sweep_enabled"] = False
    if getattr(args, "no_face", False):
        over["use_face"] = False
    if getattr(args, "budget", 0):
        over["pass_budget"] = args.budget
    if getattr(args, "inspect_budget", 0):
        over["inspect_budget"] = args.inspect_budget
    if getattr(args, "sweep_tiles", 0):
        over["sweep_zooms"] = ((8, args.sweep_tiles),)
    if getattr(args, "gate", 0):
        over["min_classify_height"] = float(args.gate)
    if getattr(args, "conf", 0):
        over["conf_coarse"] = args.conf
    if getattr(args, "capture", None):
        over["capture_mode"] = args.capture
    if getattr(args, "radius", 0):
        over["circle_radius"] = int(args.radius)
        over.setdefault("capture_mode", "circle")
    return SnapEngine(Config(**over))


def _common(p):
    p.add_argument("--budget", type=int, default=0,
                   help="max pose passes per frame")
    p.add_argument("--inspect-budget", type=int, default=0)
    p.add_argument("--sweep-tiles", type=int, default=0,
                   help="use a single zoom-8 grid with this many "
                        "tiles per frame")
    p.add_argument("--gate", type=float, default=0,
                   help="min body height (px) to classify view/stance")
    p.add_argument("--conf", type=float, default=0)
    p.add_argument("--no-motion", action="store_true")
    p.add_argument("--no-sweep", action="store_true")
    p.add_argument("--no-face", action="store_true")
    p.add_argument("--no-draw", action="store_true")
    p.add_argument("--capture", choices=("frame", "circle"), default=None,
                   help="circle = only the aim disc at screen centre")
    p.add_argument("--radius", type=int, default=0,
                   help="circle radius in px (intended 50-100); "
                        "implies --capture circle")


def cmd_image(args) -> int:
    import cv2
    engine = _build_engine(args)
    img = cv2.imread(args.source)
    if img is None:
        raise RuntimeError("could not read %r" % (args.source,))
    print("still mode: full sweep of every zoom grid - this is slow by "
          "design")
    t0 = time.perf_counter()
    tracks = engine.process_still(img, t=0.0,
                                  full_sweep=not args.no_sweep)
    print("%d people in %.1f s (%d passes)"
          % (len(tracks), time.perf_counter() - t0,
             engine.last_debug.get("passes", 0)))
    for tr in tracks:
        print(json.dumps(tr.as_dict()))
    if args.out:
        out = img if args.no_draw else draw_debug(
            img, engine.last_debug, tracks)
        cv2.imwrite(args.out, out)
        print("wrote %s" % args.out)
    return 0


def cmd_bench(args) -> int:
    import cv2
    engine = _build_engine(args)
    sizes = [(1920, 1080), (1280, 720), (960, 540)]
    print("per-pass cost of the pose model (640x640 input):")
    for (w, h) in sizes:
        frame = (np.random.default_rng(0).integers(
            0, 255, (h, w, 3), dtype=np.uint8))
        frame = cv2.GaussianBlur(frame, (31, 31), 9)
        engine.pose.detect(frame)  # warm up
        t0 = time.perf_counter()
        n = 3
        for _ in range(n):
            engine.pose.detect(frame)
        print("  coarse on %4dx%-4d: %6.1f ms" % (w, h, (time.perf_counter()
                                                         - t0) / n * 1000))
    win = np.zeros((80, 80, 3), np.uint8)
    t0 = time.perf_counter()
    for _ in range(5):
        engine.inspector.run_window(win, 0, 0, 64, 8, 0.2, "bench")
    win_ms = (time.perf_counter() - t0) / 5 * 1000
    print("  one zoom window    : %6.1f ms" % win_ms)
    if engine.face is not None:
        frame = np.zeros((1080, 1920, 3), np.uint8)
        t0 = time.perf_counter()
        engine.face.detect(frame)
        print("  yunet on 1080p     : %6.1f ms"
              % ((time.perf_counter() - t0) * 1000))
    prev = np.zeros((540, 960, 3), np.uint8)
    cur = prev.copy()
    cur[100:200, 100:200] = 90
    engine.motion.analyze(prev)
    t0 = time.perf_counter()
    engine.motion.analyze(cur)
    print("  motion analysis    : %6.1f ms" % ((time.perf_counter() - t0)
                                              * 1000))
    print()
    for target in (300, 600):
        n = max(1, int(target // max(1.0, win_ms)))
        print("for ~%d ms/frame on this machine: pass_budget=%d "
              "(e.g. 1 coarse + %d sweep/inspect)" % (target, n, n - 1))
    return 0


def cmd_live(args) -> int:
    """Real-time loop with the overlay on screen.  Nothing is recorded."""
    from .capture import open_source
    from .events import EventBus
    from .live import RealtimeLoop

    engine = _build_engine(args)
    print(engine.describe())
    radius = int(engine.cfg.circle_radius) if engine.circle else 0
    source = open_source(args.source, backend=args.backend or "auto",
                         radius=radius if (engine.circle
                                           and args.source == "screen"
                                           and not args.full_screen)
                         else 0,
                         circle=engine.circle, loop=bool(args.loop))
    print("source: %s  view: %s" % (source.name, args.view))

    ox = oy = None
    w = h = None
    origin = getattr(source, "origin", None)
    shape = getattr(source, "shape", None)
    if origin is not None and shape is not None:
        ox, oy = origin
        h, w = shape[:2]
    loop = RealtimeLoop(source, engine, target_fps=args.target_fps,
                        view="none" if args.view == "overlay" else args.view,
                        view_zoom=args.view_zoom, json_path=args.json,
                        max_seconds=args.seconds,
                        bus=EventBus(publish=args.publish),
                        select_radius=args.select_radius)
    try:
        if args.view == "overlay":
            from .overlay import TkOverlay
            loop.start()
            try:
                ov = TkOverlay(loop, x=ox, y=oy, w=w, h=h)
            except Exception as exc:
                loop.stop()
                loop.join(timeout=2.0)
                print("overlay unavailable (%s); using window view" % (exc,))
                loop.view = "window"
                loop.run()
            else:
                print("overlay window open - ESC or q stops")
                ov.mainloop()
                loop.stop()
                loop.join(timeout=3.0)
        else:
            loop.run()
    finally:
        loop.stop()
        source.release()
    stats = loop.stats()
    print("processed %d frames -> %.1f fps (target %.0f fps)"
          % (stats["processed"], stats["fps"], args.target_fps))
    print("frame cost: p50 %.0f ms  p95 %.0f ms  (target %.0f ms)"
          % (stats["ms_p50"], stats["ms_p95"], stats["target_ms"]))
    print("budget history (budget, median ms): %s"
          % (stats["budget_history"],))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m snapdet")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("live", help="real-time loop with on-screen overlay")
    p.add_argument("--source", default="synth",
                   help="'screen', 'synth' or a video file path")
    p.add_argument("--backend", choices=("auto", "mss", "dxcam"),
                   default="auto", help="screen capture backend")
    p.add_argument("--full-screen", action="store_true",
                   help="capture the whole screen, not the centre square")
    p.add_argument("--view", choices=("overlay", "window", "none"),
                   default="overlay",
                   help="overlay = transparent on-top window over the "
                        "region; window = OpenCV viewer; none = headless")
    p.add_argument("--view-zoom", type=float, default=4.0,
                   help="upscale factor of the 'window' view in circle mode")
    p.add_argument("--json", default=None,
                   help="optional track log (the only file output)")
    p.add_argument("--target-fps", type=float, default=30.0)
    p.add_argument("--seconds", type=float, default=0.0,
                   help="stop after N seconds (0 = until source ends)")
    p.add_argument("--loop", action="store_true",
                   help="loop video/synth sources forever")
    p.add_argument("--publish", default=None, metavar="udp://host:port",
                   help="emit operator select/hold/release events as "
                        "UDP JSON datagrams")
    p.add_argument("--select-radius", type=float, default=48.0,
                   help="click magnet radius in px")
    p.add_argument("--threads", type=int, default=0,
                   help="onnxruntime intra-op threads (0 = default)")
    p.add_argument("--device", choices=("cpu", "cuda", "directml"),
                   default=None, help="inference provider")
    p.add_argument("--pose-model", default=None,
                   help="alternate pose ONNX (e.g. a 320px export)")
    _common(p)
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("image", help="still mode: full sweep, max recall")
    p.add_argument("--source", required=True)
    p.add_argument("--out", default=None)
    _common(p)
    p.set_defaults(func=cmd_image)

    p = sub.add_parser("bench", help="measure this machine")
    _common(p)
    p.set_defaults(func=cmd_bench)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
