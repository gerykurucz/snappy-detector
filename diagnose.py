"""Feature diagnostics: compare real detections with synthetic samples.

Run it with a picture and the features of every person in it are
printed next to the same features for synthetic bodies, with the
features that differ the most called out.  It is the quickest way to
find out *why* a classifier behaves differently on real photographs
than on the data it was trained on.
"""

from __future__ import annotations

import argparse
import sys

import numpy as np

from . import features
from . import posefit
from . import synth
from .analyzer import Analyzer
from .keypoints import Pose


def _feature_row(pose: Pose, fit: dict) -> np.ndarray:
    return features.extract(pose, fit=fit)


def real_rows(image: str, bundle: str = None, conf: float = 0.25):
    import cv2
    analyzer = Analyzer(bundle_path=bundle, conf=conf)
    img = cv2.imread(image)
    if img is None:
        raise RuntimeError("cannot read %r" % (image,))
    out = []
    for pose in analyzer.pose_detector.detect(img):
        fit = posefit.fit(pose.kps, focal_px=img.shape[1] * 0.9)
        out.append((pose, fit, _feature_row(pose, fit)))
    return out


def synthetic_rows(stance: str, count: int = 40, seed: int = 5):
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(count):
        s = synth.sample(rng, stance=stance)
        pose = Pose(s["kps"], s["box"], 1.0)
        fit = posefit.fit(s["kps"], focal_px=s["focal"])
        out.append((pose, fit, _feature_row(pose, fit)))
    return out


def compare(real, synth, top: int = 18):
    names = features.feature_names()
    r = np.mean([row[2] for row in real], axis=0)
    s = np.mean([row[2] for row in synth], axis=0)
    rs = np.std([row[2] for row in real], axis=0)
    ss = np.std([row[2] for row in synth], axis=0)
    score = np.abs(r - s) / np.maximum(0.02 * np.maximum(np.abs(s), 1e-3),
                                        1e-3)
    order = np.argsort(score)[::-1][:top]
    print("%-24s %10s %10s %10s %8s" % ("feature", "real", "synth",
                                        "real sd", "gap"))
    for i in order:
        print("%-24s %10.3f %10.3f %10.3f %8.2f"
              % (names[i], r[i], s[i], rs[i], score[i]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hpose.diagnose")
    ap.add_argument("--image", required=True)
    ap.add_argument("--bundle", default=None)
    ap.add_argument("--stance", default="standing")
    ap.add_argument("--top", type=int, default=18)
    args = ap.parse_args(argv)
    real = real_rows(args.image, args.bundle)
    syn = synthetic_rows(args.stance)
    if not real:
        print("no people found")
        return 1
    print("real people: %d   synthetic %s: %d"
          % (len(real), args.stance, len(syn)))
    print("")
    compare(real, syn, args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
