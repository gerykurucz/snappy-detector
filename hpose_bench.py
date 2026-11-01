"""Per-stage latency benchmark.

Usage:
  python hpose_bench.py <image> [--repeat 100] [--aim 320,240 --radius 40]

Reports median / p95 / max milliseconds for each pipeline stage.
"""
from __future__ import annotations
import statistics
import sys
import time

import numpy as np

import hpose_bridge


def _pct(vals, p):
    return float(np.percentile(np.asarray(vals), p))


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    import cv2
    path = argv[0]
    repeat = 100
    aim = None
    radius = 0.0
    i = 1
    while i < len(argv):
        if argv[i] == "--repeat":
            repeat = int(argv[i + 1]); i += 2
        elif argv[i] == "--aim":
            aim = tuple(float(v) for v in argv[i + 1].split(",")); i += 2
        elif argv[i] == "--radius":
            radius = float(argv[i + 1]); i += 2
        else:
            i += 1
    img = cv2.imread(path)
    if img is None:
        print("could not read", path); return 1

    cfg = {"hpose_mode": "on", "hpose_min_conf": 12,
           "hpose_use_face": True, "hpose_tta": "auto",
           "hpose_model": "yolo26n-pose.onnx"}
    hpose_bridge.preload(cfg)
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if hpose_bridge.engine(cfg) is not None:
            break
        time.sleep(0.25)
    else:
        print("engine did not load within 60s"); return 1
    eng = hpose_bridge.engine(cfg)
    eng.reset()

    full, detect, roi, explain = [], [], [], []
    cap = max(48, int(radius))
    pad = cap + 12
    h, w = img.shape[:2]
    for _ in range(repeat):
        if aim is not None:
            ax, ay = int(aim[0]), int(aim[1])
            l = max(0, ax - pad)
            t = max(0, ay - pad)
            r = min(w, ax + pad + 1)
            b = min(h, ay + pad + 1)
            crop = img[t:b, l:r]
            # Mimic production: assist.py passes grab_around's crop (~105x105),
            # not the full frame. Benchmark on that crop so numbers are real.
            t0 = time.perf_counter()
            eng.scan(crop, origin=(l, t), aim=(ax - l, ay - t),
                     aim_radius=radius)
            full.append((time.perf_counter() - t0) * 1000)

            t0 = time.perf_counter()
            det = eng.analyzer.pose_detector.detect(crop)
            detect.append((time.perf_counter() - t0) * 1000)

            t0 = time.perf_counter()
            eng._roi_pass(eng.analyzer, crop, l, t,
                          aim=(ax - l, ay - t), radius=radius,
                          expected_h=radius * 0.6)
            roi.append((time.perf_counter() - t0) * 1000)
        else:
            t0 = time.perf_counter()
            eng.scan(img, origin=(0, 0), aim=None, aim_radius=0.0)
            full.append((time.perf_counter() - t0) * 1000)

    def report(name, vals):
        if not vals:
            return
        print("%-12s n=%d  median=%6.1f  p95=%6.1f  max=%6.1f  ms"
              % (name, len(vals), statistics.median(vals),
                 _pct(vals, 95), max(vals)))

    report("scan", full)
    report("detect", detect)
    report("roi", roi)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))