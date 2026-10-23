"""Self test.

    python -m hpose.selftest

Checks the pieces that can be checked without a photograph: the
geometry, the synthetic generator, the feature vector, the geometric
body fit, the classifiers and the tracker.  It is quick, and it is
what to run after changing anything.
"""

from __future__ import annotations

import math
import sys

import numpy as np

from . import features
from . import model3d
from . import posefit
from . import synth
from .bundle import Bundle
from .classifier import LinearHead, MLPHead
from .keypoints import Pose, box_iou, derive_joints
from .motion import KalmanBox, MotionModel
from .tracker import Tracker


class Check(object):
    """Collects the results so everything is reported, not just the
    first failure."""

    def __init__(self):
        self.passed = 0
        self.failed = []

    def ok(self, name, condition, detail=""):
        if condition:
            self.passed += 1
            print("  pass  %s" % (name,))
        else:
            self.failed.append(name)
            print("  FAIL  %s  %s" % (name, detail))

    def near(self, name, got, want, tol):
        self.ok(name, abs(float(got) - float(want)) <= tol,
                "got %.4f want %.4f" % (got, want))


def test_keypoints(c):
    kps = np.zeros((17, 3), dtype=np.float32)
    kps[5] = (10, 20, 0.9)
    kps[6] = (30, 20, 0.9)
    kps[11] = (12, 50, 0.8)
    kps[12] = (28, 50, 0.8)
    d = derive_joints(kps)
    c.near("derived neck x", d[17][0], 20.0, 0.01)
    c.near("derived neck y", d[17][1], 20.0, 0.01)
    c.near("derived pelvis x", d[18][0], 20.0, 0.01)
    c.near("derived pelvis confidence", d[17][2], 0.9, 0.01)
    c.near("iou of a box with itself", box_iou((0, 0, 10, 10),
                                               (0, 0, 10, 10)), 1.0, 1e-6)
    c.near("iou of disjoint boxes", box_iou((0, 0, 1, 1), (5, 5, 6, 6)),
           0.0, 1e-6)


def test_model3d(c):
    rng = np.random.default_rng(0)
    for yaw in (0.0, 45.0, 90.0, 180.0, 270.0):
        s = model3d.make_sample(rng, "stand", 0.0, (640, 640))
        s["yaw"] = yaw
        name = model3d.yaw_to_class(yaw)
        expect = {0.0: "front", 45.0: "front_right_45", 90.0: "side_right",
                  180.0: "back", 270.0: "side_left"}[yaw]
        c.ok("yaw %.0f maps to %s" % (yaw, name), name == expect, name)
    # a body turned away must not show a confident face
    front = model3d.face_visibility(0.0)
    back = model3d.face_visibility(180.0)
    c.ok("face is confident from the front", front["nose"] > 0.8,
         str(front["nose"]))
    c.ok("face is weak from behind", back["nose"] < front["nose"],
         str(back["nose"]))
    c.ok("nose is further out from the side",
         model3d.face_visibility(90.0)["nose"] < 1.0)


def test_synth(c):
    rng = np.random.default_rng(1)
    s = synth.sample(rng, stance="standing")
    c.ok("sample has 17 joints", s["kps"].shape == (17, 3))
    c.ok("sample is labelled", s["stance"] in synth.STANCE_CLASSES)
    c.ok("sample has a view label", s["view"] in model3d.VIEW_CLASSES)
    c.ok("sample sits in the frame",
         s["box"][1] < 640 and s["box"][3] > 0, str(s["box"]))
    c.ok("view label matches the yaw",
         s["view"] == model3d.yaw_to_class(s["yaw"]))
    batch = synth.dataset(200, seed=3)
    c.ok("dataset has the requested size", len(batch) == 200)
    views = set(s["view"] for s in batch)
    c.ok("dataset covers many views", len(views) >= 6, str(sorted(views)))


def test_fit(c):
    rng = np.random.default_rng(4)
    front_err = []
    back_err = []
    for _ in range(30):
        s = model3d.make_sample(rng, "stand", 0.0, (640, 640))
        # the analyser tells the fit what a face detector saw; the test
        # does the same, using the angle it knows the truth to be
        face = math.cos(math.radians(s["yaw"])) > 0.34
        r = posefit.fit(s["kps"], focal_px=s["focal"], face_visible=face)
        if face:
            front_err.append(abs(((r["yaw"] - s["yaw"] + 180.0) % 360.0)
                                  - 180.0))
        else:
            # without a face the silhouette is a mirror image, so only
            # the folded angle can be compared
            back_err.append(abs(posefit.fold_yaw(r["yaw"])
                                - posefit.fold_yaw(s["yaw"])))
    fe = np.array(front_err) if front_err else np.array([0.0])
    be = np.array(back_err) if back_err else np.array([0.0])
    # the tolerance is loose on purpose: the template has its arms
    # straight down, and a real person has them wherever they please,
    # which costs a few degrees of angle
    c.ok("geometric yaw is close with a face (median %.1f deg)"
         % (np.median(fe),), np.median(fe) < 30.0, str(fe))
    c.ok("geometric yaw is close without a face (median %.1f deg)"
         % (np.median(be),), np.median(be) < 20.0, str(be))
    # the repair must replace the uncertain joints and keep the rest
    kps = np.zeros((17, 3), dtype=np.float32)
    kps[:, :2] = np.random.default_rng(0).normal(50, 5, (17, 2))
    kps[:, 2] = 0.9
    kps[0, 2] = 0.0
    fit = posefit.fit(kps, focal_px=960.0)
    repaired = posefit.repair(kps, fit)
    c.ok("uncertain joints are repaired",
         repaired[0][0] != kps[0][0] or repaired[0][1] != kps[0][1])
    c.ok("confident joints are left alone",
         repaired[5][0] == kps[5][0] and repaired[5][1] == kps[5][1])


def test_features(c):
    rng = np.random.default_rng(5)
    s = synth.sample(rng, stance="standing")
    pose = Pose(s["kps"], s["box"], 1.0)
    fit = posefit.fit(s["kps"], focal_px=s["focal"])
    vec = features.extract(pose, fit=fit)
    names = features.feature_names()
    c.ok("feature vector length matches the names",
         len(vec) == len(names), "%d vs %d" % (len(vec), len(names)))
    c.ok("feature vector is finite", bool(np.all(np.isfinite(vec))))
    # the same sample must give the same vector
    vec2 = features.extract(Pose(s["kps"], s["box"], 1.0), fit=fit)
    c.ok("features are repeatable", bool(np.allclose(vec, vec2)))
    # a broken pose must not produce nonsense
    empty = Pose(np.zeros((17, 3), dtype=np.float32), (0, 0, 10, 10), 1.0)
    v3 = features.extract(empty, fit=None)
    c.ok("features survive an empty pose", bool(np.all(np.isfinite(v3))))


def test_classifier(c):
    rng = np.random.default_rng(6)
    x = rng.normal(0, 1, size=(200, 6)).astype(np.float32)
    y = (x[:, 0] > 0).astype(np.int64)
    head = LinearHead(rng.normal(0, 0.1, (6, 2)).astype(np.float32),
                      np.zeros(2, dtype=np.float32),
                      classes=["a", "b"])
    p = head.proba(x)
    c.ok("probabilities add up", bool(np.allclose(p.sum(axis=1), 1.0)))
    c.ok("probabilities are positive", bool(np.all(p >= 0.0)))
    bundle = Bundle(view_head=head, view_classes=["a", "b"],
                    stance_classes=["standing"])
    path = bundle.save("__hpose_selftest_bundle.npz")
    again = Bundle.load(path)
    c.ok("bundle round trips",
         again.view_head is not None
         and again.view_classes == ["a", "b"])
    try:
        import os
        os.remove(path)
    except OSError:
        pass


def test_motion(c):
    kb = KalmanBox((0, 0, 20, 40), 0.0)
    for i in range(1, 11):
        kb.update((i * 5.0, 0, i * 5.0 + 20, 40), float(i) * 0.05)
    kb.predict(0.15)
    vx, vy = kb.velocity
    c.ok("kalman follows a moving box", vx > 50.0, "vx=%.1f" % (vx,))
    c.ok("kalman ignores a vertical drift", abs(vy) < 30.0, "vy=%.1f" % (vy,))
    m = MotionModel()
    for i in range(20):
        m.add(i * 0.05, i * 20.0, 100.0)
    c.ok("motion speed is measured", m.speed > 200.0, str(m.speed))
    c.ok("motion state is fast", m.state() == "fast", m.state())
    x, y, ox, oy = m.predict(0.1)
    c.ok("lead moves the target forward", ox > 0.0, "ox=%.1f" % (ox,))
    c.near("lead is proportional to the delay", ox, m.speed * 0.1, 60.0)


def test_tracker(c):
    tr = Tracker(min_hits=1)
    det = {"box": (10.0, 10.0, 40.0, 90.0), "score": 0.9,
           "view_probs": {"front": 0.9, "back": 0.1},
           "stance_probs": {"standing": 0.8, "crouch": 0.2}}
    tracks = tr.update([det], 0.0)
    c.ok("tracker creates a track", len(tracks) == 1, str(len(tracks)))
    c.ok("track has an identity", tracks[0].id == 1)
    moved = dict(det)
    moved["box"] = (16.0, 10.0, 46.0, 90.0)
    tracks = tr.update([moved], 0.1)
    c.ok("tracker keeps the same identity", len(tracks) == 1
         and tracks[0].id == 1)
    c.ok("track counts its hits", tracks[0].hits == 2, str(tracks[0].hits))
    c.ok("track knows the view", tracks[0].view == "front", tracks[0].view)
    c.ok("track knows the stance", tracks[0].stance == "standing",
         tracks[0].stance)
    c.ok("track is moving", tracks[0].speed > 1.0, str(tracks[0].speed))
    # a completely different picture must not steal the track
    far = [{"box": (900.0, 900.0, 950.0, 990.0), "score": 0.9,
            "view_probs": {"front": 0.9, "back": 0.1},
            "stance_probs": {"standing": 1.0}}]
    tracks = tr.update(far, 0.2)
    c.ok("a distant detection starts a new track", len(tracks) == 2,
         str(len(tracks)))


def main() -> int:
    print("hpose self test")
    c = Check()
    test_keypoints(c)
    test_model3d(c)
    test_synth(c)
    test_fit(c)
    test_features(c)
    test_classifier(c)
    test_motion(c)
    test_tracker(c)
    print("")
    print("%d passed, %d failed" % (c.passed, len(c.failed)))
    if c.failed:
        for name in c.failed:
            print("  failed: %s" % (name,))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
