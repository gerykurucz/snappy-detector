"""Calibrate score floors against a labelled small-target set.

Usage:
  python hpose_calibrate.py <folder-of-positive-crops>

Each file in the folder is a crop of a single person. The script reports,
per pixel-height bucket, the score distribution produced by the current
PoseDetector. It writes hpose_calibration.json with recommended floors.
"""
from __future__ import annotations
import json
import os
import sys

import numpy as np

import hpose_bridge
from hpose_detector import PoseDetector


BUCKETS = [(20, 40), (40, 80), (80, 160), (160, 400)]


def main(argv):
    folder = argv[0] if argv else "."
    detector = PoseDetector(model=os.environ.get(
        "HPOSE_POSE_MODEL", "yolo26n-pose.onnx"), conf=0.05)
    import cv2
    files = [os.path.join(folder, n) for n in sorted(os.listdir(folder))
             if n.lower().endswith((".jpg", ".png", ".jpeg"))]
    per_bucket = {b: [] for b in BUCKETS}
    for path in files:
        img = cv2.imread(path)
        if img is None:
            continue
        h, w = img.shape[:2]
        poses = detector.detect(img)
        if not poses:
            continue
        # pick the highest-score pose
        best = max(poses, key=lambda p: p.score)
        bh = best.box[3] - best.box[1]
        for lo, hi in BUCKETS:
            if lo <= bh < hi:
                per_bucket[(lo, hi)].append(float(best.score))
                break
    report = {}
    for (lo, hi), scores in per_bucket.items():
        if not scores:
            continue
        arr = np.asarray(scores)
        p5, p25, p50, p75, p95 = np.percentile(arr, [5, 25, 50, 75, 95])
        report["%d-%d" % (lo, hi)] = {
            "n": int(arr.size),
            "min": float(arr.min()),
            "p5": float(p5),
            "median": float(p50),
            "p95": float(p95),
            "max": float(arr.max()),
        }
    print(json.dumps(report, indent=2))
    out = "hpose_calibration.json"
    with open(out, "w") as fh:
        json.dump(report, fh, indent=2)
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))