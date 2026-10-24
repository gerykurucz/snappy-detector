"""Benchmark: hpose detection accuracy at small search radii.

Composites real person crops (harvested from COCO-style photos in
bench/pics into bench/donors) at a target pixel height into photo-
realistic windows sized exactly like the app's capture
(side = 2*max(48, R) + 1), then runs the REAL app path:
vision.scan_region with default config (hpose on, min_conf 35, aim
center). Success = a target whose aim point lands inside the ground
truth person box (tolerance max(3, 0.1*h) px).

Matrix: R in {10..70 step 10} with h = 2R (the stated pairing), plus
off-diagonal cells; N samples per cell (BENCH_N). Two frames per
sample at 16 ms cadence (click + follow worker); verdict on frame 2,
frame 1 also reported.

    cd snappy-detector && python3 ../bench/hpose_bench.py
"""
import os
import sys
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'snappy-detector'))

import config
import hpose_bridge
import vision

DONORS = os.path.join(HERE, 'donors')
PICS = os.path.join(HERE, 'pics')
CELLS = [(10, 20), (20, 40), (30, 60), (40, 80), (50, 100), (60, 120),
         (70, 140)]
N_PER_CELL = int(os.environ.get('BENCH_N', '12'))


_PERSON_BOX_CACHE = {}


def person_boxes(analyzer, img, key):
    if key not in _PERSON_BOX_CACHE:
        boxes = []
        for pose in analyzer.pose_detector.detect(img):
            x1, y1, x2, y2 = pose.box
            boxes.append((x1 - 24, y1 - 24, x2 + 24, y2 + 24))
        _PERSON_BOX_CACHE[key] = boxes
    return _PERSON_BOX_CACHE[key]


def person_free_patch(analyzer, bgs, rng, S, margin=8, tries=40):
    """A photo patch of side S that contains no detected person.

    Random photo crops are NOT person-free: the source photographs are
    full of people, and a 'negative control' that contains a bystander
    measures nothing. Patches are rejected while they touch any person
    box found in the full frame at a permissive confidence.
    """
    for _ in range(tries):
        bi = int(rng.integers(len(bgs)))
        img = bgs[bi]
        bh, bw = img.shape[:2]
        if bh < S or bw < S:
            img = cv2.resize(img, (max(bw, S + 1), max(bh, S + 1)))
            bh, bw = img.shape[:2]
        y0 = int(rng.integers(0, bh - S + 1))
        x0 = int(rng.integers(0, bw - S + 1))
        ok = True
        for bx1, by1, bx2, by2 in person_boxes(analyzer, img, bi):
            if not (x0 + S + margin < bx1 or bx1 + margin > x0 + S or
                    y0 + S + margin < by1 or by1 + margin > y0 + S):
                # boxes overlap the patch?
                if not (bx2 < x0 - margin or bx1 > x0 + S + margin or
                        by2 < y0 - margin or by1 > y0 + S + margin):
                    ok = False
                    break
        if ok:
            return img[y0:y0 + S, x0:x0 + S].copy()
    return None


def load_pool():
    donors = []
    for n in sorted(os.listdir(DONORS)):
        im = cv2.imread(os.path.join(DONORS, n), cv2.IMREAD_COLOR)
        if im is not None and im.shape[0] >= 60:
            donors.append(im)
    bgs = []
    for n in sorted(os.listdir(PICS)):
        im = cv2.imread(os.path.join(PICS, n), cv2.IMREAD_COLOR)
        if im is not None:
            bgs.append(im)
    return donors, bgs


def S_of(R):
    """The window the app actually captures: grab_around pads the
    search radius by 12 px per side (pad = cap + 12, side = 2*pad+1,
    cap = max(48, radius))."""
    return 2 * (max(48, R) + 12) + 1


def make_sample(donors, bgs, R, h, rng, analyzer=None):
    S = S_of(R)
    win = None
    if analyzer is not None:
        win = person_free_patch(analyzer, bgs, rng, S)
    if win is None:
        bg = bgs[rng.integers(len(bgs))]
        bh, bw = bg.shape[:2]
        if bh < S or bw < S:
            bg = cv2.resize(bg, (max(bw, S + 1), max(bh, S + 1)))
            bh, bw = bg.shape[:2]
        y0 = int(rng.integers(0, bh - S + 1))
        x0 = int(rng.integers(0, bw - S + 1))
        win = bg[y0:y0 + S, x0:x0 + S].copy()

    d = donors[rng.integers(len(donors))]
    dh, dw = d.shape[:2]
    nw = max(6, int(round(dw * h / dh)))
    person = cv2.resize(d, (nw, h), interpolation=cv2.INTER_AREA)
    jx = int(rng.integers(-max(1, R // 3), max(1, R // 3) + 1))
    jy = int(rng.integers(-max(1, R // 4), max(1, R // 4) + 1))
    cx, cy = S // 2 + jx, S // 2 + jy
    px, py = cx - nw // 2, cy - h // 2
    sx1, sy1 = max(0, -px), max(0, -py)
    dx1, dy1 = max(0, px), max(0, py)
    sx2 = min(nw, S - px) if px < S else 0
    sy2 = min(h, S - py) if py < S else 0
    if sx2 > sx1 and sy2 > sy1:
        win[dy1:dy1 + (sy2 - sy1), dx1:dx1 + (sx2 - sx1)] = \
            person[sy1:sy2, sx1:sx2]
    gt = (max(0, px), max(0, py), min(S, px + nw), min(S, py + h))
    return win, gt


def aim_of(res, S):
    if res.kind != 'target' or res.target is None:
        return None
    return (S // 2 + res.target.dx, S // 2 + res.target.dy)


def inside(pt, box, tol):
    if pt is None:
        return False
    x1, y1, x2, y2 = box
    return (x1 - tol) <= pt[0] <= (x2 + tol) and \
           (y1 - tol) <= pt[1] <= (y2 + tol)


def main(cells=None):
    cells = cells or CELLS
    donors, bgs = load_pool()
    print(f"donors={len(donors)} backgrounds={len(bgs)}")
    cfg = dict(config.DEFAULT_CONFIG)
    cfg['hpose_mode'] = 'on'
    cfg['hpose_lead_ms'] = 0
    hpose_bridge.preload(cfg)
    for _ in range(200):
        if hpose_bridge.engine(cfg) is not None:
            break
        time.sleep(0.25)
    assert hpose_bridge.engine(cfg) is not None, 'engine failed to load'

    rng = np.random.default_rng(1234)
    total1 = total2 = n = 0
    hp_only = [0]
    worst = (1.0, None)
    for R, h in cells:
        S = S_of(R)
        ok1 = ok2 = 0
        fails = []
        for i in range(N_PER_CELL):
            win, gt = make_sample(donors, bgs, R, h, rng,
                                  analyzer=hpose_bridge.engine(cfg).analyzer)
            hpose_bridge.reset()
            tol = max(3.0, 0.10 * h)
            res = vision.scan_region(win, S // 2, S // 2, cfg, radius=R,
                                     origin=(0, 0))
            v1 = inside(aim_of(res, S), gt, tol)
            time.sleep(0.016)
            res = vision.scan_region(win, S // 2, S // 2, cfg, radius=R,
                                     origin=(0, 0))
            v2 = inside(aim_of(res, S), gt, tol)
            if v2 and res.target is not None and not res.target.view:
                hp_only[0] -= 1     # classical fallback, not hpose
            ok1 += v1
            ok2 += v2
            if not v2:
                fails.append(i)
        acc = ok2 / N_PER_CELL
        if acc < worst[0]:
            worst = (acc, (R, h))
        total1 += ok1
        total2 += ok2
        n += N_PER_CELL
        print(f"R={R:3d} h={h:3d} win={S:3d}: frame1 {ok1}/{N_PER_CELL}"
              f"  frame2 {ok2}/{N_PER_CELL}"
              + (f"  failing samples: {fails}" if fails else ""))
    print(f"\nhpose-answered successes: {total2 - (-hp_only[0]) - (total2 - total2) if False else total2 + hp_only[0]}/{n}")
    print(f"TOTAL frame1: {total1}/{n} = {100.0*total1/n:.1f}%")
    print(f"TOTAL frame2: {total2}/{n} = {100.0*total2/n:.1f}%"
          f"   worst cell {worst[1]}: {100.0*worst[0]:.1f}%")
    return 0 if total2 / n >= 0.95 else 1


if __name__ == '__main__':
    sys.exit(main())
