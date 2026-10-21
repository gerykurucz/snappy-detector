"""Validate against the COCO keypoint annotations.

COCO ships human-annotated keypoints for thousands of people, and the
annotations record whether each joint was *visible* or merely *known*.
That gives an honest front/back label that nobody has to guess at:

* a person looking at the camera has a visible nose and eyes,
* a person turned away has a nose and eyes that are marked as hidden.

This script runs the whole pose pipeline on the annotated keypoints -
not on the detector's - and reports how often the right answer comes
back.  It is the check that says whether the classifier works on real
bodies or only on the ones it was trained on.

    python -m hpose.validate_coco --kp-json kp_val2017.json
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np

from . import features
from . import posefit
from .keypoints import N_KP, Pose
from .model3d import VIEW_CLASSES

#: COCO keypoint order.
COCO_ORDER = ("nose", "left_eye", "right_eye", "left_ear", "right_ear",
              "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
              "left_wrist", "right_wrist", "left_hip", "right_hip",
              "left_knee", "right_knee", "left_ankle", "right_ankle")


def load_annotations(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def pose_from_annotation(ann: dict) -> Pose:
    """Turn a COCO annotation into a pose.

    COCO stores ``[x, y, visibility]`` triples flattened into a list,
    with visibility 1 meaning "visible", 2 meaning "known but hidden"
    and 0 meaning "not annotated".
    """
    flat = ann.get("keypoints") or []
    kps = np.zeros((N_KP, 3), dtype=np.float32)
    for i in range(N_KP):
        x, y, v = flat[3 * i:3 * i + 3]
        if v == 0:
            kps[i] = (0.0, 0.0, 0.0)
        else:
            # a joint that is known but hidden is not something a
            # detector would have found either
            conf = 0.85 if v == 1 else 0.15
            kps[i] = (float(x), float(y), conf)
    x, y, w, h = ann["bbox"]
    box = (x, y, x + w, y + h)
    return Pose(kps, box, 1.0)


def facing_label(ann: dict) -> str:
    """``front``, ``back`` or ``ambiguous`` from the visibility flags.

    COCO annotates a face joint as 1 when it can be seen, 2 when the
    annotator knows where it is but it is hidden, and 0 when it was not
    annotated at all.  A visible nose means the person is looking at
    the camera; a nose that is hidden together with both eyes means
    they are turned away.  Everything else is left unlabelled rather
    than guessed at.
    """
    flat = ann.get("keypoints") or []

    def vis(i):
        return flat[3 * i + 2] if 3 * i + 2 < len(flat) else 0

    nose = vis(0)
    le = vis(1)
    re = vis(2)
    # only test people whose body was properly annotated, otherwise
    # the label says more about the size of the person in the picture
    # than about the way they are facing
    visible = sum(1 for i in range(N_KP) if vis(i) == 1)
    if visible < 8:
        return "ambiguous"
    if nose == 1:
        return "front"
    if nose == 2 and le == 2 and re == 2:
        return "back"
    return "ambiguous"


def run(kp_json: str, bundle: str = None, limit: int = 0,
        use_fit: bool = True):
    from .bundle import load_bundle
    from .stance import StanceClassifier
    from .view import ViewClassifier
    bundle_obj = load_bundle(bundle)
    view_clf = ViewClassifier(bundle_obj)
    stance_clf = StanceClassifier(bundle_obj)

    data = load_annotations(kp_json)
    anns = [a for a in data["annotations"] if not a.get("iscrowd", 0)]
    if limit:
        anns = anns[:limit]

    counts = {"front": [0, 0], "back": [0, 0], "ambiguous": [0, 0]}
    fit_counts = {"front": [0, 0], "back": [0, 0], "ambiguous": [0, 0]}
    view_hits = 0
    view_total = 0
    for ann in anns:
        pose = pose_from_annotation(ann)
        label = facing_label(ann)
        counts[label][1] += 1
        fit = posefit.fit(pose.kps, focal_px=640.0)
        feats = features.extract(pose, fit=fit)
        res = view_clf.predict(feats, fit)
        if label != "ambiguous":
            # the classifier must not call a person seen from behind
            # "front"
            called_back = res.name in ("back", "back_left_45",
                                       "back_right_45")
            called_front = res.name in ("front", "front_left_45",
                                        "front_right_45")
            if (label == "back" and called_back) or \
                    (label == "front" and called_front):
                counts[label][0] += 1
            # the geometric fit alone must land on the right side of
            # the circle
            yaw = float(fit.get("yaw", 0.0))
            cos = np.cos(np.radians(yaw))
            fit_counts[label][1] += 1
            if (label == "back" and cos < 0.0) or \
                    (label == "front" and cos > 0.0):
                fit_counts[label][0] += 1
    print("")
    print("COCO annotated people: %d" % (len(anns),))
    print("")
    print("%-12s %6s %10s %10s" % ("label", "count", "classifier",
                                   "fit alone"))
    for key in ("front", "back", "ambiguous"):
        n = counts[key][1]
        if not n:
            continue
        print("%-12s %6d %9.1f%% %9.1f%%"
              % (key, n, 100.0 * counts[key][0] / n,
                 100.0 * fit_counts[key][0] / max(1, fit_counts[key][1])))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hpose.validate_coco")
    ap.add_argument("--kp-json", required=True)
    ap.add_argument("--bundle", default=None)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)
    return run(args.kp_json, args.bundle, args.limit)


if __name__ == "__main__":
    sys.exit(main())
