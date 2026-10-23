"""Command line front end for hpose.

    python -m hpose.cli detect  --source video.mp4 --out out.mp4
    python -m hpose.cli image   --source photo.jpg --out photo_annotated.jpg
    python -m hpose.cli webcam  --out webcam.mp4
    python -m hpose.cli train   --samples 20000
    python -m hpose.cli info

The ``detect`` command writes an annotated video and, with ``--json``,
a per-frame record of every track: where it was, which way it was
facing, what it was doing and how fast it was moving.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

from .analyzer import Analyzer, draw
from .bundle import default_bundle_path, load_bundle


def _open_source(source: str):
    """Return ``(reader, frames, is_video)`` for a source."""
    import cv2
    if source in ("webcam", "0"):
        cap = cv2.VideoCapture(0)
        return cap, None, True
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise RuntimeError("could not open %r" % (source,))
    return cap, None, True


def _write_video(path: str, size, fps: float):
    import cv2
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(path, fourcc, max(1.0, fps), size)
    return writer


def cmd_detect(args) -> int:
    import cv2
    bundle = args.bundle or default_bundle_path()
    analyzer = Analyzer(bundle_path=bundle, conf=args.conf,
                        use_face=not args.no_face)
    print(analyzer.describe())
    print("bundle: %s" % (bundle,))

    cap, _, _ = _open_source(args.source)
    writer = None
    records = []
    t0 = time.perf_counter()
    frame_no = 0
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if fps <= 1.0 or fps > 240.0:
        fps = 30.0
    src = args.source
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = time.perf_counter() - t0
        tracks = analyzer.analyze(frame, t=t)
        if args.draw:
            out = draw(frame, tracks)
        else:
            out = frame
        if args.out:
            if writer is None:
                h, w = out.shape[:2]
                writer = _write_video(args.out, (w, h), fps)
            writer.write(out)
        if args.json:
            records.append({
                "t": round(t, 4),
                "frame": frame_no,
                "tracks": [tr.as_dict() for tr in tracks],
            })
        frame_no += 1
        if args.max_frames and frame_no >= args.max_frames:
            break
    cap.release()
    if writer is not None:
        writer.release()
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=1)
        print("wrote %s" % (args.json,))
    if args.out:
        print("wrote %s (%d frames, %.1f fps)"
              % (args.out, frame_no,
                 frame_no / max(1e-6, time.perf_counter() - t0)))
    return 0


def cmd_image(args) -> int:
    import cv2
    bundle = args.bundle or default_bundle_path()
    analyzer = Analyzer(bundle_path=bundle, conf=args.conf)
    img = cv2.imread(args.source)
    if img is None:
        raise RuntimeError("could not read %r" % (args.source,))
    tracks = analyzer.analyze(img, t=0.0, reset=True)
    for tr in tracks:
        print(json.dumps(tr.as_dict()))
    if args.out:
        out = draw(img, tracks) if args.draw else img
        cv2.imwrite(args.out, out)
        print("wrote %s" % (args.out,))
    return 0


def cmd_webcam(args) -> int:
    return cmd_detect(args)


def cmd_train(args) -> int:
    from .train import main as train_main
    argv = ["--samples", str(args.samples), "--epochs", str(args.epochs),
            "--out", args.out]
    if args.real_dir:
        argv += ["--real-dir", args.real_dir,
                 "--real-rounds", str(args.real_rounds)]
    return train_main(argv)


def cmd_eval_real(args) -> int:
    from .eval_real import evaluate
    return evaluate(args.photos, args.bundle, args.conf, not args.no_face)


def cmd_demo(args) -> int:
    from .demo import main as demo_main
    argv = ["--frames", str(args.frames)]
    if args.image:
        argv += ["--image", args.image]
    if args.out:
        argv += ["--out", args.out]
    if args.bundle:
        argv += ["--bundle", args.bundle]
    return demo_main(argv)


def cmd_info(args) -> int:
    bundle = load_bundle(args.bundle or default_bundle_path())
    print(bundle.describe())
    from .detector import cache_dir
    print("model cache: %s" % (cache_dir(),))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hpose.cli")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("detect", help="run over a video file")
    p.add_argument("--source", required=True)
    p.add_argument("--out", default=None)
    p.add_argument("--json", default=None)
    p.add_argument("--bundle", default=None)
    p.add_argument("--conf", type=float, default=0.25)
    p.add_argument("--max-frames", type=int, default=0)
    p.add_argument("--no-draw", dest="draw", action="store_false")
    p.add_argument("--no-face", action="store_true")
    p.set_defaults(func=cmd_detect, draw=True)

    p = sub.add_parser("image", help="run over a single picture")
    p.add_argument("--source", required=True)
    p.add_argument("--out", default=None)
    p.add_argument("--bundle", default=None)
    p.add_argument("--conf", type=float, default=0.25)
    p.add_argument("--no-draw", dest="draw", action="store_false")
    p.set_defaults(func=cmd_image, draw=True)

    p = sub.add_parser("webcam", help="run over the webcam")
    p.add_argument("--out", default=None)
    p.add_argument("--json", default=None)
    p.add_argument("--bundle", default=None)
    p.add_argument("--conf", type=float, default=0.25)
    p.add_argument("--max-frames", type=int, default=0)
    p.add_argument("--no-draw", dest="draw", action="store_false")
    p.add_argument("--no-face", action="store_true")
    p.set_defaults(func=cmd_webcam, draw=True)

    p = sub.add_parser("train", help="train the heads")
    p.add_argument("--samples", type=int, default=20000)
    p.add_argument("--epochs", type=int, default=80)
    p.add_argument("--out", default=default_bundle_path())
    p.add_argument("--real-dir", default=None,
                   help="a folder of photographs to learn from")
    p.add_argument("--real-rounds", type=int, default=1)
    p.set_defaults(func=cmd_train)

    p = sub.add_parser("eval-real",
                        help="score against the hand labelled photographs")
    p.add_argument("--photos", required=True)
    p.add_argument("--bundle", default=None)
    p.add_argument("--conf", type=float, default=0.25)
    p.add_argument("--no-face", action="store_true")
    p.set_defaults(func=cmd_eval_real)

    p = sub.add_parser("demo", help="watch a moving target being tracked")
    p.add_argument("--image", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--bundle", default=None)
    p.add_argument("--frames", type=int, default=60)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("info", help="show what is installed")
    p.add_argument("--bundle", default=None)
    p.set_defaults(func=cmd_info)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
