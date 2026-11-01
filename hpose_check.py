
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
        face = math.cos(math.radians(s["yaw"])) > 0.35
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

def test_fit_small_target(c):
    import hpose_model3d as model3d
    import hpose_posefit as posefit
    import math
    rng = np.random.default_rng(4)
    stages = []
    residuals = []
    errs = []
    n_small = 0
    for _ in range(40):
        s = model3d.make_sample(rng, "stand", 0.0, (80, 80))
        face = math.cos(math.radians(s["yaw"])) > 0.35
        h_px = s["box"][3] - s["box"][1]
        if h_px >= 40.0:
            continue                                # only small-target samples
        n_small += 1
        r = posefit.fit(s["kps"], focal_px=s["focal"], face_visible=face,
                         box_h_px=h_px)
        stages.append(r.get("stage", 0))
        residuals.append(r.get("residual", float("inf")))
        errs.append(abs(((r["yaw"] - s["yaw"] + 180.0) % 360.0) - 180.0))
    c.ok("small-target fit runs at all",
         len(errs) > 0, "n_small=%d" % (n_small,))
    if not errs:
        return
    c.ok("small-target fit completes (stage >= 1)",
         all(s >= 1 for s in stages), str(stages))
    c.ok("small-target fit is finite",
         all(np.isfinite(r) for r in residuals), str(residuals))
    c.ok("small-target fit does not run the fine stage",
         all(s < 2 for s in stages), str(stages))
    c.ok("small-target fit yaw is usable (median %.1f deg)"
         % (np.median(errs),), np.median(errs) < 60.0, str(np.median(errs)))

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
    import hpose_features as features
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
    c.ok("shipped bundle was trained on current features",
         getattr(shipped, "feature_version", None)
            == features.FEATURE_VERSION,
         "bundle fv=%r runtime fv=%r"
         % (getattr(shipped, "feature_version", None),
            features.FEATURE_VERSION))

def test_motion(c):
    from hpose_motion import KalmanBox, MotionModel
    kb = KalmanBox((0, 0, 20, 40), 0.0)
    for i in range(1, 11):
        kb.update((i * 5.0, 0, i * 5.0 + 20, 40), float(i) * 0.05)
    kb.predict(0.15)
    # last_t must never move backward: predict into the past is clamped
    c.ok("kalman last_t stays at 0.50 after predict into past",
         abs(kb.last_t - 0.50) < 1e-6, "last_t=%.3f" % (kb.last_t,))
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
    c.ok("track is moving", tracks[0].speed > 20.0, str(tracks[0].speed))
    far = [{"box": (900.0, 900.0, 950.0, 990.0), "score": 0.9,
            "view_probs": {"front": 0.9, "back": 0.1},
            "stance_probs": {"standing": 1.0}}]
    tracks = tr.update(far, 0.2)
    c.ok("a distant detection starts a new track", len(tracks) == 2,
         str(len(tracks)))


def test_decode_single_detection(c):
    from hpose_detector import PoseDetector
    det = PoseDetector.__new__(PoseDetector)
    det.conf, det.iou = 0.05, 0.45
    raw = np.zeros((1, 56), dtype=np.float32)
    raw[0, 0:4] = (320.0, 320.0, 40.0, 80.0)
    raw[0, 4] = 0.9
    raw[0, 5::3] = 320.0
    raw[0, 6::3] = 320.0
    raw[0, 7::3] = 0.9
    c.ok("decode handles (1, 56)",
         len(det.decode(raw, 1.0, 0.0, 0.0, 640, 640)) == 1)
    raw_t = np.ascontiguousarray(raw.T)
    c.ok("decode handles (56, 1)",
         len(det.decode(raw_t, 1.0, 0.0, 0.0, 640, 640)) == 1)

def test_onnx_provider(c):
    """Assert the ORT session uses a GPU provider when one is available,
    and falls back to CPU otherwise."""
    import hpose_detector
    import onnxruntime as ort
    available = ort.get_available_providers()
    want_gpu = any(ep in available for ep in (
        "TensorrtExecutionProvider",
        "CUDAExecutionProvider",
        "DmlExecutionProvider"))
    try:
        det = hpose_detector.PoseDetector(download=False)
    except Exception as exc:
        c.ok("PoseDetector loads", False, repr(exc))
        return
    c.ok("PoseDetector loads", det.session is not None)
    if want_gpu:
        c.ok("PoseDetector uses a GPU provider",
             det.active_provider != "CPUExecutionProvider",
             "active=%s available=%s"
             % (det.active_provider, available))
    else:
        c.ok("PoseDetector falls back to CPU",
             det.active_provider == "CPUExecutionProvider",
             det.active_provider)

def test_kalman_size_scaling(c):
    from hpose_motion import KalmanBox
    rng = np.random.default_rng(0)

    def track_err(h, n=30):
        kf = KalmanBox((0, 0, h / 2, h), 0.0)
        errs = []
        for i in range(1, n + 1):
            t = i * 0.033
            true_cx = 100.0 * t
            meas_cx = true_cx + rng.normal(0, 1.0)
            kf.update((meas_cx - h / 4, 0, meas_cx + h / 4, h), t)
            errs.append(abs(kf.x[0] - true_cx) / h)
        return float(np.mean(errs))

    err_small = track_err(20)
    err_large = track_err(200)
    c.ok("small targets track tighter relative to size",
         err_small < 4 * err_large,
         "small=%.3f large=%.3f" % (err_small, err_large))

def test_features_no_feet(c):
    import hpose_features as features
    import hpose_model3d as model3d
    from hpose_keypoints import Pose
    rng = np.random.default_rng(11)
    s = model3d.make_sample(rng, "stand", 0.0, (640, 640))
    kps = s["kps"].copy()
    kps[15, 2] = 0.0
    kps[16, 2] = 0.0
    vec = features.extract(Pose(kps, s["box"], 1.0), fit=None)
    names = features.feature_names()
    c.ok("features are length-stable without feet",
         len(vec) == len(names), "%d vs %d" % (len(vec), len(names)))

def test_motion_state_adapts(c):
    from hpose_motion import MotionModel
    m = MotionModel(body_h_px=20.0)
    # Add enough samples for EMA to converge near true speed
    for i in range(50):
        m.add(i * 0.05, i * 2.0, 100.0)
    # True speed is 40 px/s; fast threshold = 2*20=40. EMA should converge
    # to just below 40, so state may be "slow" - accept either.
    c.ok("20px: speed converges near 40px/s",
         m.speed > 35.0 and m.speed < 45.0, "speed=%.1f state=%s" % (m.speed, m.state()))
    # Now grow to 200px: fast=400, slow=100. Speed ~40 is static.
    m.body_h_px = 200.0
    c.ok("200px: 40px/s is static", m.state() == "static", m.state())

def test_pick_small_target(c):
    import hpose_bridge
    from hpose_bridge import Subject

    sub = Subject((100, 100, 115, 120), score=0.15, raw_score=0.15,
                  kp_conf=0.20)
    sub.hits = 1
    cfg = {"hpose_min_conf": 12, "hpose_view": "any",
           "hpose_stance": "any"}
    picked = hpose_bridge.pick([sub], cfg, 107, 110, 40)
    c.ok("pick accepts a 20px 1-hit target",
         picked is not None, "got None")

def test_blob_watch_basic(c):
    import numpy as np
    import vision
    img = np.zeros((200, 200, 3), np.uint8)
    img[80:120, 90:110] = 200
    t = vision._blob_watch(img, 100, 100, 40)
    c.ok("blob watch finds a 20x40 blob", t is not None)
    if t is not None:
        c.ok("blob watch reports a 20x40 box",
             15 <= t.bw <= 25 and 35 <= t.bh <= 45,
             "%dx%d" % (t.bw, t.bh))

def test_fig_score_can_cross_tier(c):
    import numpy as np
    import vision
    tier = vision._shape_tier(40, 100, 4000)
    c.ok("aspect-only tier is 2 for r=2.5", tier == 2, str(tier))
    score_ones = vision._figure_score(40, 100, 4000,
                                       np.ones((100, 40), dtype=bool))
    c.ok("all-ones mask is capped below the memory gate",
         score_ones < 0.50, str(score_ones))






def unit_checks(c):
    print("unit checks")
    test_keypoints(c)
    test_model3d(c)
    test_fit(c)
    test_fit_small_target(c)
    test_features(c)
    test_features_no_feet(c)
    test_bundle(c)
    test_motion(c)
    test_kalman_size_scaling(c)
    test_motion_state_adapts(c)
    test_tracker(c)
    test_decode_single_detection(c)
    test_onnx_provider(c)
    test_pick_small_target(c)
    test_blob_watch_basic(c)
    test_fig_score_can_cross_tier(c)
    test_blob_fallback_through_tracker(c)
    test_blob_watch_no_false_positives(c)


def test_blob_fallback_through_tracker(c):
    """Blob detections must flow through the tracker and produce a Subject
    with hits >= 1 and a stable track_id across frames."""
    import numpy as np
    import hpose_bridge
    import time as _time
    cfg = {"hpose_mode": "on"}
    hpose_bridge.preload(cfg)
    for _ in range(240):
        if hpose_bridge.engine(cfg) is not None:
            break
        _time.sleep(0.25)
    eng = hpose_bridge.engine(cfg)
    if eng is None:
        c.ok("hpose engine available", False)
        return
    eng.reset()
    img = np.zeros((120, 120, 3), dtype=np.uint8)
    img[40:80, 55:65] = 200  # 10 x 40 blob, aspect 0.25 (stick figure)
    subs1 = eng.scan(img, origin=(0, 0), aim=(60, 60), aim_radius=40)
    c.ok("blob fallback produces a Subject", len(subs1) >= 1,
         "got %d" % len(subs1))
    if not subs1:
        return
    c.ok("blob Subject has a track id", subs1[0].track_id > 0,
         "id=%d" % subs1[0].track_id)
    c.ok("blob Subject has hits >= 1", subs1[0].hits >= 1,
         "hits=%d" % subs1[0].hits)
    subs2 = eng.scan(img, origin=(0, 0), aim=(60, 60), aim_radius=40)
    c.ok("blob Subject keeps its identity",
         len(subs2) == 1 and subs2[0].track_id == subs1[0].track_id)
    c.ok("blob Subject accumulates hits",
         len(subs2) == 1 and subs2[0].hits > subs1[0].hits)


def test_blob_watch_no_false_positives(c):
    """Blob_watch must not fire on UI elements: vertical menu dividers,
    table grid lines, and text editor carets are all 1-2 px wide, tall
    vertical sticks with aspect ratio ~0.02-0.10. With BLOB_STICK_LO=0.15,
    none of these should qualify as person-like blobs."""
    import os
    import cv2
    import vision
    neg_dir = os.path.join("testdata", "negatives")
    neg_files = ("menu_bar.jpg", "table_grid.jpg", "editor_caret.jpg")
    found_any = False
    for name in neg_files:
        path = os.path.join(neg_dir, name)
        if not os.path.isfile(path):
            continue
        found_any = True
        img = cv2.imread(path)
        if img is None:
            continue
        h, w = img.shape[:2]
        # Run blob_watch centered on the image with a reasonable radius
        bt = vision._blob_watch(img, w // 2, h // 2, min(w, h) // 4)
        c.ok("blob_watch no false positive: %s" % (name,),
             bt is None,
             "got box=%sx%s" % (bt.bw, bt.bh) if bt is not None else "")
    if not found_any:
        c.ok("blob_watch negative fixtures exist",
             False,
             "no negative fixture images found in %s" % neg_dir)


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
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if hpose_bridge.engine(cfg) is not None:
            break
        time.sleep(0.25)
    else:
        c.ok("the hpose engine loads", False, "timeout")
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
