"""Synthetic small-target recall harness.

Generates 20 px person crops from the model3d generator, composites them
onto a real background at pixel heights in {20, 30, 40, 60, 80, 120, 160},
and measures end-to-end detection recall (any Subject returned whose
centre is within `radius` of ground-truth centre).

Usage:
  python hpose_small_test.py <background.jpg> [--n 200] [--radius 40]
"""
from __future__ import annotations
import argparse
import sys

import numpy as np

import hpose_bridge
import hpose_model3d as model3d


HEIGHTS = (20, 30, 40, 60, 80, 120, 160)


def _render_person(rng, height_px):
    # Render at a comfortable working size, then resize so the composite
    # is exactly `height_px` tall. This removes the ±40% spread from
    # make_sample's target_frac choice, so a 20 px bucket actually
    # contains 20 px people.
    work_h = max(64, height_px * 4)
    s = model3d.make_sample(rng, "stand", 0.0, (work_h, work_h))
    x1, y1, x2, y2 = s["box"]
    w = int(np.ceil(x2 - x1))
    h = int(np.ceil(y2 - y1))
    if h <= 0 or w <= 0:
        return s, max(4, height_px), max(4, height_px)
    sy = height_px / float(h)
    sx = sy  # preserve aspect ratio
    new_w = max(1, int(round(w * sx)))
    new_h = height_px
    # Scale keypoints into the new coordinate space (relative to box top-left).
    kps = s["kps"].copy()
    kps[:, 0] = (kps[:, 0] - x1) * sx
    kps[:, 1] = (kps[:, 1] - y1) * sy
    kps[:, 2] = np.clip(kps[:, 2], 0.0, 1.0)
    s["box"] = (0.0, 0.0, float(new_w), float(new_h))
    s["kps"] = kps
    return s, new_w, new_h


def _place(background, rng, target_h, radius, aim):
    h, w = background.shape[:2]
    s, pw, ph = _render_person(rng, target_h)
    ang = rng.uniform(0, 2 * np.pi)
    r = rng.uniform(0, radius)
    cx = int(round(aim[0] + r * np.cos(ang)))
    cy = int(round(aim[1] + r * np.sin(ang)))
    cx = max(pw // 2, min(w - pw // 2, cx))
    cy = max(ph // 2, min(h - ph // 2, cy))
    canvas = background.copy()
    patch = canvas[cy - ph // 2:cy - ph // 2 + ph,
                   cx - pw // 2:cx - pw // 2 + pw]
    if patch.size == 0:
        return None
    proj = s["kps"][:, :2] - np.array([[s["box"][0], s["box"][1]]])
    pts = np.round(proj).astype(np.int32)
    try:
        import cv2
        hull = cv2.convexHull(pts.reshape(-1, 1, 2))
        overlay = patch.copy()
        cv2.fillPoly(overlay, [hull], (40, 40, 40))
        patch[:] = cv2.addWeighted(overlay, 0.85, patch, 0.15, 0)
    except Exception:
        import cv2
        for a, b in ((0, 5), (0, 6), (5, 7), (7, 9), (6, 8), (8, 10),
                     (5, 11), (6, 12), (11, 13), (13, 15),
                     (12, 14), (14, 16), (5, 6), (11, 12)):
            cv2.line(patch, (int(pts[a][0]), int(pts[a][1])),
                     (int(pts[b][0]), int(pts[b][1])), (40, 40, 40), 1)
    return canvas, (cx, cy, ph)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("background")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--radius", type=float, default=40.0)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    import cv2
    bg = cv2.imread(args.background)
    if bg is None:
        print("could not read", args.background); return 1
    H, W = bg.shape[:2]
    aim = (W // 2, H // 2)
    cfg = {"hpose_mode": "on", "hpose_min_conf": 12,
           "hpose_use_face": False, "hpose_tta": "auto"}
    hpose_bridge.preload(cfg)
    timeout = 60.0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if hpose_bridge.engine(cfg) is not None:
            break
        time.sleep(0.25)
    else:
        print("engine did not load within %ds" % timeout); return 1
    eng = hpose_bridge.engine(cfg)
    eng.reset()

    rng = np.random.default_rng(args.seed)
    per_h = {h: [0, 0] for h in HEIGHTS}  # [hits, total]
    for _ in range(args.n):
        target_h = int(rng.choice(HEIGHTS))
        placed = _place(bg, rng, target_h, args.radius, aim)
        if placed is None:
            continue
        frame, (gx, gy, gh) = placed
        actual = min(HEIGHTS, key=lambda hh: abs(hh - gh))
        per_h[actual][1] += 1
        eng.reset()
        subs = eng.scan(frame, origin=(0, 0), aim=aim,
                        aim_radius=args.radius)
        ok = False
        for s in subs:
            cx, cy = s.center
            if abs(cx - gx) <= max(8, gh * 0.5) and \
                    abs(cy - gy) <= max(8, gh * 0.5):
                ok = True
                break
        if ok:
            per_h[actual][0] += 1
    print("%6s  %5s  %7s" % ("height", "n", "recall"))
    total_ok = 0
    total_n = 0
    for h in HEIGHTS:
        hits, n = per_h[h]
        if n == 0:
            continue
        print("%6d  %5d  %6.2f%%" % (h, n, 100.0 * hits / n))
        total_ok += hits
        total_n += n
    if total_n:
        print("overall %.2f%% (%d/%d)" % (100.0 * total_ok / total_n,
                                           total_ok, total_n))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))