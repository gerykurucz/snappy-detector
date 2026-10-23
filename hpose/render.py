"""Drawing helpers - a stick figure of a pose, and an annotated frame.

The stick figure is the quickest way to see whether a pose is what it
was meant to be.  It is used by the development tools and by the
``python -m hpose.render`` command, which writes a contact sheet of
synthetic bodies so that the training data can be inspected by eye.
"""

from __future__ import annotations

import argparse
import sys

import numpy as np

from .keypoints import KEYPOINT_NAMES, SKELETON, derive_joints

#: Colour per body view, for the overlay.
VIEW_COLOURS = {
    "front": (80, 220, 80),
    "front_left_45": (60, 200, 160),
    "front_right_45": (60, 160, 220),
    "side_left": (60, 200, 240),
    "side_right": (240, 200, 60),
    "back_left_45": (200, 120, 240),
    "back_right_45": (240, 120, 200),
    "back": (80, 80, 240),
}

STANCE_COLOURS = {
    "standing": (200, 200, 200),
    "walk": (120, 200, 255),
    "crouch": (80, 255, 255),
    "prone": (255, 160, 60),
    "jump": (255, 120, 220),
}


def draw_pose(img, kps, colour=(0, 255, 0), thickness: int = 2,
              radius: int = 3, box=None, label: str = ""):
    """Draw one pose onto a frame."""
    import cv2
    out = img
    kps = np.asarray(kps, dtype=np.float64)
    for a, b in SKELETON:
        if kps[a][2] < 0.15 or kps[b][2] < 0.15:
            continue
        cv2.line(out, (int(kps[a][0]), int(kps[a][1])),
                 (int(kps[b][0]), int(kps[b][1])), colour, thickness,
                 cv2.LINE_AA)
    for i in range(len(kps)):
        if kps[i][2] < 0.15:
            continue
        cv2.circle(out, (int(kps[i][0]), int(kps[i][1])), radius, colour,
                   -1, cv2.LINE_AA)
    if box is not None:
        cv2.rectangle(out, (int(box[0]), int(box[1])),
                      (int(box[2]), int(box[3])), colour, 1)
    if label:
        cv2.putText(out, label, (int(box[0]) if box else 4,
                                 int(box[1]) - 6 if box else 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, 1, cv2.LINE_AA)
    return out


def contact_sheet(path: str, per_class: int = 4, size: int = 640,
                  seed: int = 3) -> str:
    """Write a grid of synthetic bodies, one row per view class."""
    import cv2
    from . import synth
    from .model3d import VIEW_CLASSES
    rng = np.random.default_rng(seed)
    rows = []
    for view in VIEW_CLASSES:
        row = []
        for _ in range(per_class):
            s = synth.sample(rng)
            while s["view"] != view:
                s = synth.sample(rng)
            canvas = np.full((size, size, 3), 30, dtype=np.uint8)
            draw_pose(canvas, s["kps"], VIEW_COLOURS.get(view, (0, 255, 0)),
                      box=s["box"], label="%s %s" % (view, s["stance"]))
            row.append(canvas)
        rows.append(np.concatenate(row, axis=1))
    sheet = np.concatenate(rows, axis=0)
    import cv2 as _cv2
    _cv2.imwrite(path, sheet)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hpose.render")
    ap.add_argument("--out", default="hpose_poses.png")
    ap.add_argument("--per-class", type=int, default=4)
    ap.add_argument("--seed", type=int, default=3)
    args = ap.parse_args(argv)
    path = contact_sheet(args.out, args.per_class, seed=args.seed)
    print("wrote %s" % (path,))
    return 0


if __name__ == "__main__":
    sys.exit(main())
