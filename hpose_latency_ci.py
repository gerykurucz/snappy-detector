"""Fail CI if latency or recall regresses beyond threshold.

Usage in CI:
  python hpose_latency_ci.py --image <fixture.jpg> --radius 40 \
      --max-median-ms 80 --max-p95-ms 120
"""
from __future__ import annotations
import argparse
import statistics
import sys
import time

import numpy as np

import hpose_bridge


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--radius", type=float, default=40.0)
    ap.add_argument("--max-median-ms", type=float, default=80.0)
    ap.add_argument("--max-p95-ms", type=float, default=120.0)
    ap.add_argument("--repeat", type=int, default=30)
    args = ap.parse_args(argv)
    import cv2
    img = cv2.imread(args.image)
    if img is None:
        print("could not read image"); return 1
    h, w = img.shape[:2]
    aim = (w // 2, h // 2)
    cfg = {"hpose_mode": "on", "hpose_min_conf": 12,
           "hpose_use_face": False, "hpose_tta": "auto"}
    hpose_bridge.preload(cfg)
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if hpose_bridge.engine(cfg) is not None:
            break
        time.sleep(0.25)
    else:
        print("engine not available within 60s"); return 1
    eng = hpose_bridge.engine(cfg)
    eng.reset()
    times = []
    for _ in range(args.repeat):
        t0 = time.perf_counter()
        eng.scan(img, origin=(0, 0), aim=aim, aim_radius=args.radius)
        times.append((time.perf_counter() - t0) * 1000)
    med = statistics.median(times)
    p95 = float(np.percentile(times, 95))
    print("median %.1f ms  p95 %.1f ms" % (med, p95))
    if med > args.max_median_ms or p95 > args.max_p95_ms:
        print("REGRESSION: budget exceeded"); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))