
import math
import os
import sys
import tempfile
import time

import numpy as np

import config
import hpose_bridge
import vision


class Check(object):
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
    from hpose_keypoints import box_iou, derive_joints
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
    import hpose_model3d as model3d
    rng = np.random.default_rng(0)
    for yaw in (0.0, 45.0, 90.0, 180.0, 270.0):
        s = model3d.make_sample(rng, "stand", 0.0, (640, 640))
        s["yaw"] = yaw
        name = model3d.yaw_to_class(yaw)
        expect = {0.0: "front", 45.0: "front_right_45", 90.0: "side_right",
                  180.0: "back", 270.0: "side_left"}[yaw]
        c.ok("yaw %.0f maps to %s" % (yaw, name), name == expect, name)
    front = model3d.face_visibility(0.0)
    back = model3d.face_visibility(180.0)
    c.ok("face is confident from the front", front["nose"] > 0.8,
         str(front["nose"]))
    c.ok("face is weak from behind", back["nose"] < front["nose"],
         str(back["nose"]))
    c.ok("nose is further out from the side",
         model3d.face_visibility(90.0)["nose"] < 1.0)


def test_fit(c):
    import hpose_model3d as model3d
    import hpose_posefit as posefit
    rng = np.random.default_rng(4)
    front_err = []
    back_err = []
    for _ in range(30):
        s = model3d.make_sample(rng, "stand", 0.0, (640, 640))
        face = math.cos(math.radians(s["yaw"])) > 0.34
        r = posefit.fit(s["kps"], focal_px=s["focal"], face_visible=face)
        if face:
            front_err.append(abs(((r["yaw"] - s["yaw"] + 180.0) % 360.0)
                                  - 180.0))
        else:
            back_err.append(abs(posefit.fold_yaw(r["yaw"])
                                - posefit.fold_yaw(s["yaw"])))
    fe = np.array(front_err) if front_err else np.array([0.0])
    be = np.array(back_err) if back_err else np.array([0.0])
    c.ok("geometric yaw is close with a face (median %.1f deg)"
         % (np.median(fe),), np.median(fe) < 30.0, str(fe))
    c.ok("geometric yaw is close without a face (median %.1f deg)"
         % (np.median(be),), np.median(be) < 20.0, str(be))
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
    import hpose_features as features
    import hpose_model3d as model3d
    import hpose_posefit as posefit
    from hpose_keypoints import Pose
    rng = np.random.default_rng(5)
    s = model3d.make_sample(rng, "stand", 0.0, (640, 640))
    pose = Pose(s["kps"], s["box"], 1.0)
    fit = posefit.fit(s["kps"], focal_px=s["focal"])
    vec = features.extract(pose, fit=fit)
    names = features.feature_names()
    c.ok("feature vector length matches the names",
         len(vec) == len(names), "%d vs %d" % (len(vec), len(names)))
    c.ok("feature vector is finite", bool(np.all(np.isfinite(vec))))
    vec2 = features.extract(Pose(s["kps"], s["box"], 1.0), fit=fit)
    c.ok("features are repeatable", bool(np.allclose(vec, vec2)))
    empty = Pose(np.zeros((17, 3), dtype=np.float32), (0, 0, 10, 10), 1.0)
    v3 = features.extract(empty, fit=None)
    c.ok("features survive an empty pose", bool(np.all(np.isfinite(v3))))


def test_bundle(c):
    from hpose_bundle import Bundle, load_bundle
    from hpose_classifier import LinearHead
    rng = np.random.default_rng(6)
    x = rng.normal(0, 1, size=(200, 6)).astype(np.float32)
    head = LinearHead(rng.normal(0, 0.1, (6, 2)).astype(np.float32),
                      np.zeros(2, dtype=np.float32),
                      classes=["a", "b"])
    p = head.proba(x)
    c.ok("probabilities add up", bool(np.allclose(p.sum(axis=1), 1.0)))
    c.ok("probabilities are positive", bool(np.all(p >= 0.0)))
    bundle = Bundle(view_head=head, view_classes=["a", "b"],
                    stance_classes=["standing"])
    path = os.path.join(tempfile.mkdtemp(prefix="hpose_check_"),
                        "bundle.npz")
    bundle.save(path)
    again = Bundle.load(path)
    c.ok("bundle round trips",
         again.view_head is not None
         and again.view_classes == ["a", "b"])
    try:
        os.remove(path)
    except OSError:
        pass
    shipped = load_bundle()
    c.ok("the shipped bundle is found and trained",
         shipped.has_view() and shipped.has_stance(),
         "expected models/hpose_bundle.npz with both heads")


def test_motion(c):
    from hpose_motion import KalmanBox, MotionModel
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
    from hpose_tracker import Tracker
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
    far = [{"box": (900.0, 900.0, 950.0, 990.0), "score": 0.9,
            "view_probs": {"front": 0.9, "back": 0.1},
            "stance_probs": {"standing": 1.0}}]
    tracks = tr.update(far, 0.2)
    c.ok("a distant detection starts a new track", len(tracks) == 2,
         str(len(tracks)))


def unit_checks(c):
    print("unit checks")
    test_keypoints(c)
    test_model3d(c)
    test_fit(c)
    test_features(c)
    test_bundle(c)
    test_motion(c)
    test_tracker(c)

def tempfile_dir():
    return os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp"


def _sample_images(extra_dir=None):
    out = []
    folders = []
    if extra_dir:
        folders.append(extra_dir)
    folders += [os.path.join(tempfile_dir(), "probe", "coco_eval2"),
                os.path.join(tempfile_dir(), "hpose_check")]
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.lower().endswith((".jpg", ".png", ".jpeg")):
                out.append(os.path.join(folder, name))
    return out


def integration_checks(c, images, cv2):
    print("integration checks (%d picture(s))" % (len(images),))
    cfg = dict(config.DEFAULT_CONFIG)

    cfg["hpose_mode"] = "off"
    worked = 0
    for path in images[:6]:
        img = cv2.imread(path)
        if img is None:
            continue
        h, w = img.shape[:2]
        res = vision.scan_region(img, w // 2, h // 2, cfg, radius=400,
                                 origin=(0, 0))
        if res.kind == "target":
            worked += 1
    c.ok("the classical scanner still answers with hpose off",
         worked > 0, "%d of 6" % (worked,))

    cfg["hpose_mode"] = "on"
    cfg["hpose_min_conf"] = 25
    hpose_bridge.preload(cfg)
    for _ in range(120):
        if hpose_bridge.engine(cfg) is not None:
            break
        time.sleep(0.25)
    c.ok("the hpose engine loads", hpose_bridge.engine(cfg) is not None)

    found = 0
    described = 0
    for path in images:
        img = cv2.imread(path)
        if img is None:
            continue
        h, w = img.shape[:2]
        res = vision.scan_region(img, w // 2, h // 2, cfg, radius=600,
                                 origin=(0, 0))
        if res.kind == "target" and res.target is not None:
            found += 1
            if res.target.view:
                described += 1
    c.ok("hpose finds people in the pictures", found > 0,
         "%d targets" % (found,))
    c.ok("the target says who it is", described > 0,
         "%d described" % (described,))

    img = cv2.imread(images[0])
    if img is not None:
        h, w = img.shape[:2]
        for want, field in (("facing", "facing_camera"),
                            ("away", "back_turned")):
            cfg["hpose_view"] = want
            ok = True
            seen = 0
            for path in images[:8]:
                pic = cv2.imread(path)
                if pic is None:
                    continue
                hh, ww = pic.shape[:2]
                res = vision.scan_region(pic, ww // 2, hh // 2, cfg,
                                         radius=600, origin=(0, 0))
                if res.kind == "target" and res.target.view:
                    seen += 1
                    if want == "facing" and not res.target.view.startswith(
                            "front"):
                        ok = False
                    if want == "away" and not res.target.view.startswith(
                            "back"):
                        ok = False
            c.ok("the view filter '%s' is obeyed" % (want,), ok,
                 "%d targets" % (seen,))
        cfg["hpose_view"] = "any"

    for path in images[:8]:
        pic = cv2.imread(path)
        if pic is None:
            continue
        hh, ww = pic.shape[:2]
        res = vision.scan_region(pic, ww // 2, hh // 2, cfg, radius=600,
                                 origin=(0, 0))
        if res.kind != "target" or res.target is None:
            continue
        t = res.target
        ax = ww // 2 + t.dx
        ay = hh // 2 + t.dy
        inside = (0 <= ax < ww and 0 <= ay < hh)
        c.ok("the aim point is inside the picture", inside,
             "%s" % ((ax, ay),))
        break

    if img is not None:
        h, w = img.shape[:2]
        hpose_bridge.reset()
        lead_seen = False
        moved = False
        for i in range(8):
            frame = np.roll(img, -i * 40, axis=1)
            res = vision.scan_region(frame, w // 2, h // 2, cfg,
                                     radius=600, origin=(0, 0))
            if res.kind == "target" and res.target is not None:
                if res.target.motion and res.target.motion != "static":
                    moved = True
                if abs(res.target.lead_x) > 1.0 or \
                        abs(res.target.lead_y) > 1.0:
                    lead_seen = True
        c.ok("a moving target is measured as moving", moved)
        c.ok("a moving target is led", lead_seen)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    print("hpose check")
    c = Check()

    unit_checks(c)

    extra = argv[0] if argv else None
    images = _sample_images(extra)
    if not images:
        print("")
        print("integration checks skipped - no pictures found")
        print("  put some in %s or pass a folder:"
              % (os.path.join(tempfile_dir(), "hpose_check"),))
        print("  python hpose_check.py <picture-folder>")
    else:
        try:
            import cv2
        except ImportError as exc:
            print("")
            print("integration checks skipped - opencv missing (%r)" % (exc,))
        else:
            integration_checks(c, images, cv2)

    print("")
    print("%d passed, %d failed" % (c.passed, len(c.failed)))
    if c.failed:
        for name in c.failed:
            print("  failed: %s" % (name,))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
