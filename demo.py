"""A demonstration of the moving target tracking.

    python -m hpose.demo --image photo.jpg --out demo.mp4

The picture is panned across the frame, which makes everything in it
move, and the analyser is asked what it sees.  The report that comes
out shows the identity of every track, how fast it is moving, which
way it is facing and where it will be in a moment's time.

With no picture it makes its own: a synthetic body walks across the
frame, which is the one case where the answer is known exactly.
"""

from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np

from . import synth
from .analyzer import Analyzer, draw
from .bundle import default_bundle_path


def pan_frames(img, n_frames: int = 60, shift: float = 6.0):
    """Pan across a picture so that everything in it moves."""
    h, w = img.shape[:2]
    out = []
    for i in range(n_frames):
        dx = int(shift * i)
        frame = np.zeros_like(img)
        x0 = max(0, min(w - 1, dx))
        x1 = max(0, min(w - 1, dx + w - 40))
        src = img[:, x0:x1]
        dst = img.shape[1] - (x1 - x0)
        frame[:, dst:dst + (x1 - x0)] = src
        out.append(frame)
    return out


def synthetic_walk(n_frames: int = 60, size=(640, 360), speed: float = 4.0):
    """Synthetic detections of a body walking across the frame.

    The detector is skipped on purpose: these are made up people, so
    the answer is known exactly and the tracking can be checked against
    the truth.
    """
    from . import posefit
    from . import synth
    from .bundle import load_bundle
    from .stance import StanceClassifier
    from .view import ViewClassifier
    bundle = load_bundle()
    view_clf = ViewClassifier(bundle)
    stance_clf = StanceClassifier(bundle)
    rng = np.random.default_rng(7)
    frames = []
    dets = []
    # one body, seen from one angle, walking across the frame
    base = synth.sample(rng, stance="walk", image_size=size)
    base_kps = base["kps"].copy()
    for i in range(n_frames):
        kps = base_kps.copy()
        x = 40.0 + speed * i
        bob = 2.0 * math.sin(i * 0.5)
        kps[:, 0] += x - base["box"][0]
        kps[:, 1] += size[0] * 0.92 - kps[:, 1].max() + bob
        box = (float(kps[:, 0].min()), float(kps[:, 1].min()),
               float(kps[:, 0].max()), float(kps[:, 1].max()))
        fit = posefit.fit(kps, focal_px=size[1] * 0.9)
        from .keypoints import Pose
        pose = Pose(kps, box, 0.95)
        from . import features
        feats = features.extract(pose, fit=fit)
        view = view_clf.predict(feats, fit)
        stance = stance_clf.predict(feats)
        dets.append({"box": box, "score": 0.95,
                     "view_probs": view.probs,
                     "stance_probs": stance.probs,
                     "fit": fit, "has_face": False})
        frame = np.full((size[0], size[1], 3), 28, dtype=np.uint8)
        frames.append(frame)
    return frames, dets


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hpose.demo")
    ap.add_argument("--image", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--bundle", default=None)
    ap.add_argument("--frames", type=int, default=60)
    ap.add_argument("--lead-ms", type=float, default=100.0)
    args = ap.parse_args(argv)

    analyzer = Analyzer(bundle_path=args.bundle or default_bundle_path())
    print(analyzer.describe())

    synthetic = not args.image
    if args.image:
        import cv2
        img = cv2.imread(args.image)
        if img is None:
            print("cannot read %r" % (args.image,))
            return 1
        frames = pan_frames(img, args.frames)
        dets = None
    else:
        frames, dets = synthetic_walk(args.frames)

    writer = None
    dt = 1.0 / 30.0
    lead = args.lead_ms / 1000.0
    print("")
    print("%-4s %-5s %-9s %-16s %-9s %-8s %s"
          % ("frm", "id", "state", "view", "stance", "speed", "lead"))
    for i, frame in enumerate(frames):
        t = i * dt
        if dets is None:
            tracks = analyzer.analyze(frame, t=t)
        else:
            tracks = analyzer.tracker.update([dets[i]], t)
        for tr in tracks:
            x, y, ox, oy = tr.lead(lead)
            print("%-4d %-5d %-9s %-16s %-9s %7.1f  (+%.0f,%+.0f)"
                  % (i, tr.id, tr.motion_state, tr.view, tr.stance,
                     tr.speed, ox, oy))
        if args.out:
            import cv2
            out = draw(frame, tracks)
            if writer is None:
                writer = cv2.VideoWriter(args.out,
                                         cv2.VideoWriter_fourcc(*"mp4v"),
                                         30.0, (frame.shape[1], frame.shape[0]))
            writer.write(out)
    if writer is not None:
        writer.release()
        print("")
        print("wrote %s" % (args.out,))
    return 0


if __name__ == "__main__":
    sys.exit(main())
