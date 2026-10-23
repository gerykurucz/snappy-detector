"""Evaluate against the hand labelled photographs.

    python -m hpose.eval_real --photos folder

The labels live in :mod:`hpose.real_labels` and were written by
looking at the pictures, so this measures the answer against a person
rather than against another model.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

from .analyzer import Analyzer
from .model3d import VIEW_CLASSES
from .real_labels import LABELS
from .synth import STANCE_CLASSES


def evaluate(photos: str, bundle: str = None, conf: float = 0.25,
             use_face: bool = True):
    import cv2
    analyzer = Analyzer(bundle_path=bundle, conf=conf, use_face=use_face)
    view_true = []
    view_pred = []
    stance_true = []
    stance_pred = []
    pictures = 0
    people = 0

    for name in sorted(LABELS):
        path = os.path.join(photos, name)
        img = cv2.imread(path)
        if img is None:
            continue
        pictures += 1
        tracks = analyzer.analyze(img, t=0.0, reset=True)
        truth = LABELS[name]
        for i, label in enumerate(truth):
            if label is None or i >= len(tracks):
                continue
            people += 1
            view, stance = label
            tr = tracks[i]
            if view:
                view_true.append(view)
                view_pred.append(tr.view)
            if stance:
                stance_true.append(stance)
                stance_pred.append(tr.stance)

    print("")
    print("pictures        : %d" % (pictures,))
    print("people judged   : %d" % (people,))
    print("")
    if view_true:
        hits = sum(1 for a, b in zip(view_true, view_pred) if a == b)
        near = 0
        for a, b in zip(view_true, view_pred):
            if a == b:
                near += 1
            elif _same_family(a, b):
                near += 1
        print("view exact      : %.3f  (%d people)"
              % (hits / max(1, len(view_true)), len(view_true)))
        print("view within 45  : %.3f"
              % (near / max(1, len(view_true))))
        _report(view_true, view_pred, VIEW_CLASSES)
    if stance_true:
        hits = sum(1 for a, b in zip(stance_true, stance_pred) if a == b)
        print("")
        print("stance exact    : %.3f  (%d people)"
              % (hits / max(1, len(stance_true)), len(stance_true)))
        _report(stance_true, stance_pred, STANCE_CLASSES)
    return 0


def _same_family(a: str, b: str) -> bool:
    """True when the two answers are in the same part of the circle."""
    order = ["front", "front_left_45", "side_left", "back_left_45", "back",
             "back_right_45", "side_right", "front_right_45"]
    try:
        d = abs(order.index(a) - order.index(b))
    except ValueError:
        return False
    return min(d, 8 - d) <= 1


def _report(truth, pred, classes):
    print("")
    width = max(len(c) for c in classes)
    print("      " + " ".join("%*s" % (width, c[:width]) for c in classes))
    for i, name in enumerate(classes):
        row = []
        for j in range(len(classes)):
            n = sum(1 for a, b in zip(truth, pred)
                    if a == name and b == classes[j])
            row.append("%*d" % (width, n))
        print("%*s  %s" % (width, name[:width], " ".join(row)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hpose.eval_real")
    ap.add_argument("--photos", required=True)
    ap.add_argument("--bundle", default=None)
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--no-face", action="store_true")
    args = ap.parse_args(argv)
    return evaluate(args.photos, args.bundle, args.conf, not args.no_face)


if __name__ == "__main__":
    sys.exit(main())
