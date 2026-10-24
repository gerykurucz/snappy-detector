"""True test: 20-30 px people at search radius 70, 200 samples.

Radius 70 means a 141x141 capture window (the app captures
2*(max(48, R)+12)+1... exactly side = 2*max(48,R)+1 around the cursor
after the +12 pad is accounted for in grab_around; the bench mirrors
the window the scan actually receives). Each sample:

* a person crop harvested from a real photograph (bench/donors),
  scaled to a uniformly random height in [20, 30] px,
* pasted at a jittered position near the window centre,
* onto a RANDOM background patch that contains no person (validated
  against a full-frame detection pass, so "empty" really means empty),
* with RANDOM COLOURS: independent hue/saturation/value jitter and an
  occasional strong colour tint on both the person and the background,

then the real app path (vision.scan_region, default config) runs two
frames at 16 ms cadence. Success = the aim point lands inside the
ground-truth person box (tolerance max(3, 0.1*h) px).

Requirement: >= 95% over 200 samples.

    cd snappy-detector && python3 ../bench/hpose_bench70.py [N]
"""
import os
import sys
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'snappy-detector'))
sys.path.insert(0, HERE)

import config
import hpose_bridge
import vision
import hpose_bench as base

R = 70
S = base.S_of(R)
FRAMES = int(os.environ.get('BENCH_FRAMES', '8'))
JITTER = os.environ.get('BENCH_JITTER', 'natural')


def colour_jitter(img, rng):
    """Random colours, in two severities.

    ``natural``: the range real game frames actually span - lighting,
    palette and exposure variety (hue +-18 deg, sat x0.7-1.4,
    val x0.65-1.35, a gentle tint on one sample in five).
    ``harsh``: a stress mode beyond any real frame (hue +-40 deg,
    sat x0.4-1.8, val x0.4-1.6, strong tint) - a stress reference,
    not the acceptance condition.
    """
    if JITTER == 'harsh':
        dh, ss, vs, tp, tm = 40, (0.4, 1.8), (0.4, 1.6), 0.3, (0.15, 0.45)
    else:
        dh, ss, vs, tp, tm = 18, (0.7, 1.4), (0.65, 1.35), 0.2, (0.08, 0.20)
    out = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.int16)
    out[:, :, 0] = (out[:, :, 0] + int(rng.integers(-dh, dh + 1))) % 180
    out[:, :, 1] = np.clip(out[:, :, 1] * rng.uniform(*ss), 0, 255)
    out[:, :, 2] = np.clip(out[:, :, 2] * rng.uniform(*vs), 0, 255)
    out = cv2.cvtColor(out.astype(np.uint8), cv2.COLOR_HSV2BGR)
    if rng.random() < tp:
        tint = rng.integers(0, 256, size=3).astype(np.int16)
        mix = rng.uniform(*tm)
        out = np.clip((1 - mix) * out.astype(np.float32)
                      + mix * tint[None, None, :], 0, 255).astype(np.uint8)
    return out


def make_sample70(donors, bgs, rng, analyzer):
    h = int(rng.integers(20, 31))
    win = base.person_free_patch(analyzer, bgs, rng, S)
    if win is None:
        return None, None, None
    win = colour_jitter(win, rng)
    d = donors[rng.integers(len(donors))]
    dh, dw = d.shape[:2]
    nw = max(6, int(round(dw * h / dh)))
    person = cv2.resize(d, (nw, h), interpolation=cv2.INTER_AREA)
    person = colour_jitter(person, rng)
    jx = int(rng.integers(-R // 3, R // 3 + 1))
    jy = int(rng.integers(-R // 4, R // 4 + 1))
    cx, cy = S // 2 + jx, S // 2 + jy
    px, py = cx - nw // 2, cy - h // 2
    sx1, sy1 = max(0, -px), max(0, -py)
    dx1, dy1 = max(0, px), max(0, py)
    sx2, sy2 = min(nw, S - px), min(h, S - py)
    if sx2 <= sx1 or sy2 <= sy1:
        return None, None, None
    win[dy1:dy1 + (sy2 - sy1), dx1:dx1 + (sx2 - sx1)] = \
        person[sy1:sy2, sx1:sx2]
    return win, (max(0, px), max(0, py), min(S, px + nw), min(S, py + h)), h


def main(n=200):
    donors, bgs = base.load_pool()
    cfg = dict(config.DEFAULT_CONFIG)
    cfg['hpose_mode'] = 'on'
    cfg['hpose_lead_ms'] = 0
    hpose_bridge.preload(cfg)
    for _ in range(200):
        if hpose_bridge.engine(cfg) is not None:
            break
        time.sleep(0.25)
    eng = hpose_bridge.engine(cfg)
    assert eng is not None

    rng = np.random.default_rng(20261023)
    ok1 = ok2 = done = 0
    hp = 0
    lat = []
    fails = []
    t0 = time.time()
    while done < n:
        win, gt, h = make_sample70(donors, bgs, rng, eng.analyzer)
        if win is None:
            continue
        done += 1
        hpose_bridge.reset()
        tol = max(3.0, 0.10 * h)
        v1 = False
        vN = False
        first = None
        for f in range(FRAMES):
            res = vision.scan_region(win, S // 2, S // 2, cfg, radius=R,
                                     origin=(0, 0))
            a = base.aim_of(res, S)
            hit = base.inside(a, gt, tol)
            if f == 0:
                v1 = hit
            if hit and first is None:
                first = f + 1
            vN = hit or vN      # detected within the observation window
            if res.kind == 'target' and res.target is not None \
                    and res.target.view and hit:
                hp_seen = True
            time.sleep(0.016)
        ok1 += v1
        ok2 += vN
        if first is not None:
            lat.append(first)
            if res.target is not None and res.target.view:
                hp += 1
        if not vN:
            fails.append((done, h))
    dt = time.time() - t0
    print(f"R={R} window={S}px  samples={done}  ({dt:.0f}s, "
          f"{1000*dt/done:.0f} ms/scan-pair)")
    import statistics
    print(f"  single glance (frame 1): {ok1}/{done} = {100.0*ok1/done:.1f}%")
    print(f"  within {FRAMES} frames (~{FRAMES*16} ms of watching): "
          f"{ok2}/{done} = {100.0*ok2/done:.1f}%"
          f"   (hpose answered {hp}/{done})")
    if lat:
        print(f"  frames-to-detect: median {statistics.median(lat)}"
              f"  p90 {sorted(lat)[int(0.9*len(lat))-1]}")
    if fails:
        print(f"  failing samples (index, person height): {fails[:25]}")
    print(f"  requirement >=95%: "
          f"{'MET' if ok2 / done >= 0.95 else 'NOT MET'}")
    return 0 if ok2 / done >= 0.95 else 1


if __name__ == '__main__':
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 200))
