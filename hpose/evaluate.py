"""Measuring how good the classifier actually is.

The evaluation set is a JSON file::

    {
      "items": [
        {"image": "photos/a.jpg",
         "people": [
           {"box": [x1, y1, x2, y2], "view": "front", "stance": "standing"}
         ]}
      ]
    }

Every person in the picture must be listed.  The report gives

* detection quality - average IoU and recall at IoU 0.5,
* view accuracy and stance accuracy over the matched people,
* the confusion matrices, so the classes that get confused are
  visible rather than hidden inside an average.

Run it with::

    python -m hpose.evaluate --labels labels.json --bundle hpose_bundle.npz
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

from . import model3d
from .analyzer import Analyzer
from .keypoints import box_iou
from .model3d import VIEW_CLASSES
from .synth import STANCE_CLASSES


def _match(pred_boxes, gt_boxes, iou_threshold: float = 0.5):
    """Greedy IoU matching between predictions and ground truth."""
    pairs = []
    for pi, pb in enumerate(pred_boxes):
        for gi, gb in enumerate(gt_boxes):
            iou = box_iou(pb, gb)
            if iou >= iou_threshold:
                pairs.append((iou, pi, gi))
    pairs.sort(reverse=True)
    used_p = set()
    used_g = set()
    out = []
    for iou, pi, gi in pairs:
        if pi in used_p or gi in used_g:
            continue
        used_p.add(pi)
        used_g.add(gi)
        out.append((pi, gi, iou))
    return out, used_p, used_g


def confusion(y_true, y_pred, n):
    m = np.zeros((n, n), dtype=np.int64)
    for a, b in zip(y_true, y_pred):
        m[a][b] += 1
    return m


def print_confusion(matrix, classes) -> None:
    width = max(len(c) for c in classes)
    print("      " + " ".join("%*s" % (width, c[:width]) for c in classes))
    for i, row in enumerate(matrix):
        print("%*s  %s" % (width, classes[i][:width],
                           " ".join("%*d" % (width, v) for v in row.tolist())))


def evaluate(labels_path: str, bundle_path: str = None, conf: float = 0.25,
             iou_threshold: float = 0.5, use_face: bool = True):
    import cv2
    with open(labels_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    items = data["items"] if isinstance(data, dict) else data

    analyzer = Analyzer(bundle_path=bundle_path, conf=conf,
                        use_face=use_face)
    view_true = []
    view_pred = []
    stance_true = []
    stance_pred = []
    ious = []
    detected = 0
    total = 0

    for item in items:
        path = item["image"]
        img = cv2.imread(path)
        if img is None:
            sys.stderr.write("hpose: cannot read %s, skipping\n" % (path,))
            continue
        people = item.get("people") or []
        gt_boxes = [p["box"] for p in people]
        tracks = analyzer.analyze(img, t=0.0, reset=True)
        pred_boxes = [tr.box for tr in tracks]
        matches, used_p, _ = _match(pred_boxes, gt_boxes, iou_threshold)
        detected += len(matches)
        total += len(gt_boxes)
        for pi, gi, iou in matches:
            ious.append(iou)
            pred = tracks[pi]
            gt = people[gi]
            if "view" in gt:
                view_true.append(gt["view"])
                view_pred.append(pred.view)
            if "stance" in gt:
                stance_true.append(gt["stance"])
                stance_pred.append(pred.stance)

    print("")
    print("people in the set      : %d" % (total,))
    print("found at IoU %.2f       : %d (%.1f%%)"
          % (iou_threshold, detected, 100.0 * detected / max(1, total)))
    if ious:
        print("mean IoU of the matches: %.3f" % (float(np.mean(ious)),))
    if view_true:
        acc = float(np.mean([a == b for a, b in zip(view_true, view_pred)]))
        print("")
        print("view accuracy           : %.4f  (%d people)"
              % (acc, len(view_true)))
        print_confusion(confusion(view_true, view_pred, len(VIEW_CLASSES)),
                        VIEW_CLASSES)
    if stance_true:
        acc = float(np.mean([a == b for a, b in zip(stance_true,
                                                    stance_pred)]))
        print("")
        print("stance accuracy         : %.4f  (%d people)"
              % (acc, len(stance_true)))
        print_confusion(confusion(stance_true, stance_pred,
                                   len(STANCE_CLASSES)), STANCE_CLASSES)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hpose.evaluate")
    ap.add_argument("--labels", required=True)
    ap.add_argument("--bundle", default=None)
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--iou", type=float, default=0.5)
    ap.add_argument("--no-face", action="store_true")
    args = ap.parse_args(argv)
    return evaluate(args.labels, args.bundle, args.conf, args.iou,
                    not args.no_face)


if __name__ == "__main__":
    sys.exit(main())
