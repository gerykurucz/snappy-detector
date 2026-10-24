"""Diagnostics for the small-window accuracy work.

A: per-cell detection stats straight from the detector (IoU-matched
   true dets, merged-box dets, misses) plus keypoint-quality stats and
   box-centre vs torso-midpoint aim error;
B: negative controls - person-free windows must not produce targets;
C: letterbox interpolation A/B (LINEAR / CUBIC / LANCZOS4).

    cd snappy-detector && python3 ../bench/hpose_diag.py
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
import hpose_detector

sys.path.insert(0, HERE)
import hpose_bench as bench

donors, bgs = bench.load_pool()
cfg = dict(config.DEFAULT_CONFIG)
cfg['hpose_mode'] = 'on'
cfg['hpose_lead_ms'] = 0
hpose_bridge.preload(cfg)
for _ in range(200):
    if hpose_bridge.engine(cfg) is not None:
        break
    time.sleep(0.25)
eng = hpose_bridge.engine(cfg)


def torso_stats(pose):
    k = pose.kps
    tor = [k[i] for i in (5, 6, 11, 12)]
    confs = [float(c[2]) for c in tor]
    good = [(c[0], c[1]) for c in tor if c[2] >= 0.25]
    mid = ((sum(p[0] for p in good) / len(good),
            sum(p[1] for p in good) / len(good)) if good else None)
    return sum(confs) / 4.0, float(k[0][2]), mid


def iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


CELLS = [(10, 20), (20, 40), (30, 60), (40, 80), (50, 100), (60, 120),
         (70, 140), (70, 40), (70, 60), (70, 100), (50, 20), (50, 40),
         (40, 20), (30, 20)]
N = int(os.environ.get('DIAG_N', '10'))
rng = np.random.default_rng(777)

print("=== A: detector stats per cell (single frame, no tracker) ===")
print("  R    h  det%  merged%  score  torsoConf  aimErrBox  aimErrTorso")
for R, h in CELLS:
    ndet = nmerged = 0
    sc, tc, eb, et = [], [], [], []
    for i in range(N):
        win, gt = bench.make_sample(donors, bgs, R, h, rng)
        an = eng.analyzer
        poses = an.pose_detector.detect(win)
        best, bestv = None, 0.0
        for p in poses:
            v = iou(p.box, gt)
            if v > bestv:
                best, bestv = p, v
        gtc = ((gt[0] + gt[2]) / 2, (gt[1] + gt[3]) / 2)
        if best is not None and bestv >= 0.2:
            ndet += 1
            tconf, _nconf, mid = torso_stats(best)
            sc.append(best.score)
            tc.append(tconf)
            bc = ((best.box[0] + best.box[2]) / 2,
                  (best.box[1] + best.box[3]) / 2)
            eb.append(abs(bc[0] - gtc[0]) + abs(bc[1] - gtc[1]))
            if mid:
                et.append(abs(mid[0] - gtc[0]) + abs(mid[1] - gtc[1]))
        elif any(p.box[0] <= gtc[0] <= p.box[2] and p.box[1] <= gtc[1] <= p.box[3]
                 for p in poses):
            nmerged += 1
    f = lambda xs: (sum(xs) / len(xs) if xs else float('nan'))
    print(f"{R:3d} {h:4d} {100*ndet/N:5.0f} {100*nmerged/N:7.0f}  "
          f"{f(sc):.2f}   {f(tc):.2f}      {f(eb):6.1f}     {f(et):6.1f}")

print()
print("=== B: negative controls (person-free windows) ===")
fp = tot = 0
for R, _h in [(10, 0), (20, 0), (30, 0), (40, 0), (50, 0), (60, 0), (70, 0)]:
    S = 2 * max(48, R) + 1
    nf = 0
    for i in range(12):
        win = bench.person_free_patch(eng.analyzer, bgs, rng, S)
        if win is None:
            continue
        hpose_bridge.reset()
        subs = eng.scan(win, (0, 0))
        if hpose_bridge.pick(subs, cfg, S // 2, S // 2, R) is not None:
            nf += 1
    fp += nf
    tot += 12
    print(f"  R={R:3d}: false targets {nf}/12")
print(f"  TOTAL false positives: {fp}/{tot} = {100.0*fp/tot:.1f}%")

print()
print("=== C: letterbox interpolation A/B ===")
orig = hpose_detector.PoseDetector._letterbox


def make_letterbox(interp):
    def _lb(self, img):
        h, w = img.shape[:2]
        ih, iw = self._in_h, self._in_w
        r = min(iw / float(w), ih / float(h))
        nw = max(1, int(round(w * r)))
        nh = max(1, int(round(h * r)))
        if (nw, nh) != (w, h):
            img = cv2.resize(img, (nw, nh), interpolation=interp)
        pad_x = (iw - nw) / 2.0
        pad_y = (ih - nh) / 2.0
        left = int(round(pad_x - 0.1))
        top = int(round(pad_y - 0.1))
        out = np.full((ih, iw, 3), 114, dtype=np.uint8)
        out[top:top + nh, left:left + nw] = img
        return out, r, left, top
    return _lb


rng2 = np.random.default_rng(4242)
samples = [(bench.make_sample(donors, bgs, R, h, rng2), R, h)
           for R, h in [(10, 20), (20, 40), (30, 60), (40, 80)]
           for _ in range(10)]
for name, interp in (("LINEAR", cv2.INTER_LINEAR),
                     ("CUBIC", cv2.INTER_CUBIC),
                     ("LANCZOS4", cv2.INTER_LANCZOS4)):
    hpose_detector.PoseDetector._letterbox = make_letterbox(interp)
    scores, hits = [], 0
    for (win, gt), R, h in samples:
        for p in eng.analyzer.pose_detector.detect(win):
            if iou(p.box, gt) >= 0.2:
                scores.append(p.score)
                hits += 1
    m = sum(scores) / max(1, len(scores))
    print(f"  {name:9s}: true-dets {hits}/{len(samples)}  mean score {m:.3f}")
hpose_detector.PoseDetector._letterbox = orig
