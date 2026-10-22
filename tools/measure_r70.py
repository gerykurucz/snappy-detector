"""Capability measurement at radius 70 (disc = 140 x 140 px).

For every hand-labelled COCO person (view bin + stance, labelled by a
human looking at the photograph) the person is cut out, scaled to a
target height and placed into a 140x140 disc over sharp real scenery,
in five visibility states:

    whole   the body sits inside the disc
    top     body centre on the top edge    -> lower half visible
    bottom  body centre on the bottom edge -> upper half visible
    left    body centre on the left edge   -> right half visible
    right   body centre on the right edge  -> left half visible

and the shipped engine runs in circle mode (process_still = full
sweep, i.e. what the live loop converges to).  A case counts as
DETECTED when a fused detection lands inside the visible part of the
body (person box intersected with the disc square, padded 8 px).

Outputs a markdown report: detection ratios by yaw bin, by stance, by
visibility, by body height, the height x visibility matrix, and - for
bodies tall enough to be classified at all - how often the brain's
view bin agrees with the human label.

    python -m tools.measure_r70            # ~20 min on a CPU box
    python -m tools.measure_r70 --quick    # 2 sizes, 3 vis states
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import urllib.request

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from snapdet.capture import synth_assets, data_dir          # noqa: E402
from snapdet.config import Config                           # noqa: E402
from snapdet.engine import SnapEngine                       # noqa: E402

COCO = "http://images.cocodataset.org/val2017/%s"

CASES = [
    # (image, view, stance)
    ("000000186042.jpg", "back", "standing"),
    ("000000172648.jpg", "back", "standing"),
    ("000000357742.jpg", "back", "standing"),
    ("000000039785.jpg", "back", "crouch"),
    ("000000482735.jpg", "back", "prone"),
    ("000000485071.jpg", "side_left", "prone"),
    ("000000049259.jpg", "side_right", "prone"),
    ("000000485027.jpg", "front", "jump"),
    ("000000068628.jpg", "front", "jump"),
    ("000000397351.jpg", "front", "standing"),
    ("000000573258.jpg", "front", "standing"),
    ("000000221291.jpg", "front", "standing"),
    ("000000162415.jpg", "front", "standing"),
    ("000000132703.jpg", "front", "crouch"),
    ("000000273132.jpg", "front", "crouch"),
    ("000000356505.jpg", "front", "crouch"),
]
SIZES = [140, 90, 50, 24, 12]
VIS = ["whole", "top", "bottom", "left", "right"]
R = 70
MASK = 114


def fetch(name):
    d = os.path.join(data_dir(), "coco")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    if not os.path.isfile(p):
        urllib.request.urlretrieve(COCO % name, p)
    return p


def person_box(img):
    from snapdet.detector import PoseDetector
    ps = PoseDetector(conf=0.25).detect(img)
    if not ps:
        return None
    p = max(ps, key=lambda p: p.score)
    return p.box


def disc_scene(bg, sprite, h_target, vis, jx=0, jy=0):
    """140x140 masked disc with the sprite placed per visibility.

    ``jx/jy`` shift the placement by a pixel or two - the micro-motion
    a real handheld or game camera produces, which the live protocol
    uses so the temporal paths get their natural input.
    """
    import cv2
    side = 2 * R
    w = max(1, int(round(sprite.shape[1] * h_target / sprite.shape[0])))
    spr = cv2.resize(sprite, (w, h_target), interpolation=cv2.INTER_AREA)
    canvas = bg.copy()
    if vis == "whole":
        cx, cy = R, R
    elif vis == "top":
        cx, cy = R, 0
    elif vis == "bottom":
        cx, cy = R, side
    elif vis == "left":
        cx, cy = 0, R
    else:
        cx, cy = side, R
    cx, cy = cx + jx, cy + jy
    top, left = int(cy - h_target / 2), int(cx - w / 2)
    y1, y2 = max(0, top), min(side, top + h_target)
    x1, x2 = max(0, left), min(side, left + w)
    if y2 > y1 and x2 > x1:
        canvas[y1:y2, x1:x2] = spr[y1 - top:y2 - top, x1 - left:x2 - left]
    yy, xx = np.mgrid[0:side, 0:side]
    canvas[((xx - R + .5) ** 2 + (yy - R + .5) ** 2) > (R - .5) ** 2] = MASK
    # visible part of the body, in disc coordinates
    vx1, vy1 = max(0, left), max(0, top)
    vx2, vy2 = min(side, left + w), min(side, top + h_target)
    return canvas, (vx1, vy1, vx2, vy2)


def detected(debug, visbox, pad=8.0):
    for d in debug.get("dets", []):
        cx = (d["box"][0] + d["box"][2]) * 0.5
        cy = (d["box"][1] + d["box"][3]) * 0.5
        if (visbox[0] - pad <= cx <= visbox[2] + pad
                and visbox[1] - pad <= cy <= visbox[3] + pad):
            return d
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--part", type=int, default=0)
    ap.add_argument("--parts", type=int, default=1)
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--sizes", default="",
                    help="comma list of heights to measure, e.g. 12,24")
    ap.add_argument("--live", type=int, default=0,
                    help="live-convergence protocol: N frames per case "
                         "with micro-motion, hit = seen in any frame")
    args = ap.parse_args(argv)
    if args.merge:
        return report(load_parts(args.parts))
    sizes = [140, 50] if args.quick else SIZES
    if args.sizes:
        sizes = [int(x) for x in args.sizes.split(",")]
    vis = ["whole", "top", "left"] if args.quick else VIS

    import cv2
    _, bg640, _, _ = synth_assets()
    bg = bg640[180:320, 420:560]            # sharp 140x140 scenery
    eng = SnapEngine(Config(capture_mode="circle", circle_radius=R))

    cases = CASES[args.part::args.parts]
    rows = []
    total = len(cases) * len(sizes) * len(vis)
    done = 0
    for name, view, stance in cases:
        img = cv2.imread(fetch(name))
        box = person_box(img)
        if box is None:
            print("  (no person in %s, skipped)" % name)
            continue
        x1, y1, x2, y2 = [int(v) for v in box]
        sprite = img[max(0, y1 - 2):y2 + 2, max(0, x1 - 2):x2 + 2]
        for h in sizes:
            for v in vis:
                if args.live:
                    hit, d = False, None
                    rng = np.random.default_rng(hash((name, h, v)) % 2**31)
                    for f in range(args.live):
                        jx, jy = (int(rng.integers(-2, 3)),
                                  int(rng.integers(-2, 3)))
                        scene, vb = disc_scene(bg, sprite, h, v, jx, jy)
                        tracks = eng.process(scene, t=f / 30.0,
                                             reset=(f == 0))
                        d = detected(eng.last_debug, vb) or d
                        if d is not None:
                            hit = True
                        for tr in tracks:
                            if (vb[0] - 8 <= tr.center[0] <= vb[2] + 8 and
                                    vb[1] - 8 <= tr.center[1] <= vb[3] + 8):
                                hit = True
                                d = d or {"score": tr.score,
                                          "source": tr.source,
                                          "view": tr.view,
                                          "truncated": tr.truncated}
                    mode = "live%d" % args.live
                else:
                    scene, vb = disc_scene(bg, sprite, h, v)
                    eng.process_still(scene, t=0.0)
                    d = detected(eng.last_debug, vb)
                    hit = d is not None
                    mode = "still"
                rows.append({"img": name, "view": view, "stance": stance,
                             "h": h, "vis": v, "mode": mode,
                             "hit": hit,
                             "score": round(d["score"], 2) if d else 0.0,
                             "src": d.get("source") if d else None,
                             "pred_view": d.get("view") if d else None,
                             "trunc": bool(d.get("truncated")) if d else False})
                done += 1
                if done % 25 == 0:
                    print("  ... %d/%d" % (done, total))

    out = os.path.join(HERE, "measure_r70_rows_part%d.json" % args.part)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=1)
    print("part %d wrote %d rows to %s" % (args.part, len(rows), out))
    return 0


def load_parts(parts):
    rows = []
    for i in range(parts):
        f = os.path.join(HERE, "measure_r70_rows_part%d.json" % i)
        if os.path.isfile(f):
            rows.extend(json.load(open(f)))
    return rows


def report(rows):
    sizes, vis = SIZES, VIS

    def rate(sel):
        sel = [r for r in rows if sel(r)]
        if not sel:
            return None, 0, 0
        hit = sum(1 for r in sel if r["hit"])
        return hit / len(sel), hit, len(sel)

    md = ["# Measured capability, radius 70 (disc 140x140 px)",
          "",
          "Real COCO photographs, human view/stance labels, shipped",
          "engine, circle mode, full sweep per case.",
          "",
          "overall: **%.0f%%** (%d/%d)" % ((rate(lambda r: True)[0] or 0)
                                           * 100, rate(lambda r: True)[1],
                                           rate(lambda r: True)[2]),
          ""]
    md += ["## by body height in the disc", "",
           "| height px | detection |", "|---|---|"]
    for h in sizes:
        r, a, b = rate(lambda r, h=h: r["h"] == h)
        md.append("| %d | %.0f%% (%d/%d) |" % (h, (r or 0) * 100, a, b))
    md += ["", "## by visibility", "", "| visibility | detection |",
           "|---|---|"]
    for v in vis:
        r, a, b = rate(lambda r, v=v: r["vis"] == v)
        md.append("| %s | %.0f%% (%d/%d) |" % (v, (r or 0) * 100, a, b))
    md += ["", "## by yaw (view bin)", "", "| yaw bin | detection |",
           "|---|---|"]
    for view in ("front", "back", "side_left", "side_right"):
        r, a, b = rate(lambda r, view=view: r["view"] == view)
        md.append("| %s | %.0f%% (%d/%d) |" % (view, (r or 0) * 100, a, b))
    md += ["", "## by stance", "", "| stance | detection |", "|---|---|"]
    for st in ("standing", "crouch", "prone", "jump"):
        r, a, b = rate(lambda r, st=st: r["stance"] == st)
        md.append("| %s | %.0f%% (%d/%d) |" % (st, (r or 0) * 100, a, b))
    md += ["", "## height x visibility", "",
           "| height | " + " | ".join(vis) + " |",
           "|" + "---|" * (len(vis) + 1)]
    for h in sizes:
        cells = []
        for v in vis:
            r, a, b = rate(lambda r, h=h, v=v: r["h"] == h and r["vis"] == v)
            cells.append("%.0f%%" % ((r or 0) * 100))
        md.append("| %d | %s |" % (h, " | ".join(cells)))
    md += ["", "## yaw x visibility (bodies >= 50 px)", "",
           "| yaw bin | " + " | ".join(vis) + " |",
           "|" + "---|" * (len(vis) + 1)]
    for view in ("front", "back", "side_left", "side_right"):
        cells = []
        for v in vis:
            r, a, b = rate(lambda r, view=view, v=v: r["view"] == view
                           and r["vis"] == v and r["h"] >= 50)
            cells.append("%.0f%% (%d)" % ((r or 0) * 100, b))
        md.append("| %s | %s |" % (view, " | ".join(cells)))
    md += ["", "## view classification of detected bodies >= 48 px", "",
           "| label | agreed | detected&classified |", "|---|---|---|"]
    for view in ("front", "back", "side_left", "side_right"):
        sel = [r for r in rows if r["view"] == view and r["hit"]
               and r["h"] >= 48 and r["pred_view"] not in (None, "unknown")]
        if not sel:
            continue
        ok = sum(1 for r in sel if r["pred_view"] == view
                 or (view == "back" and r["pred_view"].startswith("back"))
                 or (view == "front" and r["pred_view"].startswith("front"))
                 or (view.startswith("side")
                     and r["pred_view"].startswith("side")))
        md.append("| %s | %.0f%% | %d |" % (view, 100.0 * ok / len(sel),
                                            len(sel)))
    md += ["", "## notes", "",
           "- diagonal yaw bins (45/135/225/315 deg) have no hand-labelled"
           " real photographs in the label set; they are bracketed by the"
           " measured cardinals and covered in training by the synthetic"
           " 360 deg body model.",
           "- 'detection' = a fused detection inside the visible part of"
           " the body; full-sweep still mode = what the live loop"
           " converges to over a sweep cycle.",
           ""]
    rep = os.path.join(HERE, "measure_r70_results.md")
    out = os.path.join(HERE, "measure_r70_results.json")
    open(rep, "w", encoding="utf-8").write("\n".join(md))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=1)
    print("\n".join(md))
    print("\nwrote %s and %s" % (out, rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
