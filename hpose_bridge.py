
import math
import threading
import time

try:
    from system_core import log
except Exception:
    def log(_text):
        pass


DEFAULT_MODEL = "yolo11n-pose.onnx"

DETECTOR_FLOOR = 0.10

_UNAVAILABLE_REASON = ""


def _conf_of(cfgv) -> float:
    try:
        pct = float(cfgv.get("hpose_min_conf", 35) or 35)
    except (TypeError, ValueError):
        pct = 35.0
    return max(0.05, min(0.95, pct / 100.0))


CROP_RESCUE_SCORE = 0.05
CROP_RESCUE_KP = 0.80

ROI_RADIUS_MAX = 150.0
ROI_MARGIN = 3.0
ROI_MIN_SIDE = 160
ROI_RESCUE_SCORE = 0.15
ROI_RESCUE_KP = 0.50

MAX_EXPLAIN_PER_FRAME = 6

TTA_MAX_SIDE = 280


def _prefilter(poses, rescue_score=0.20, rescue_kp=0.6):
    out = []
    for pose in poses:
        score = float(pose.score)
        kp = _kp_conf(pose)
        if not (score >= 0.35 or (score >= rescue_score and kp >= rescue_kp)):
            continue
        out.append((pose, max(score, 0.5 * (score + kp)), kp, score))
    out.sort(key=lambda item: -item[1])
    return out


def _contained(inner, outer):
    ix1, iy1 = max(inner[0], outer[0]), max(inner[1], outer[1])
    ix2, iy2 = min(inner[2], outer[2]), min(inner[3], outer[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    area = max(1.0, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    return iw * ih / area


def _suppress_contained(dets):
    if len(dets) < 2:
        return dets
    keep = []
    for i, d in enumerate(dets):
        dropped = False
        for j, o in enumerate(dets):
            if i == j or o[1] <= d[1]:
                continue
            if _contained(d[0].box, o[0].box) >= 0.7:
                dropped = True
                break
        if not dropped:
            keep.append(d)
    return keep


def _kp_conf(pose) -> float:
    try:
        k = pose.kps
        return float(sum(float(k[i][2]) for i in (5, 6, 11, 12)) / 4.0)
    except Exception:
        return 0.0


def _suppress_dets(dets):
    if len(dets) < 2:
        return list(dets)
    boxes = [d["box"] for d in dets]
    scores = [float(d.get("score", 0.0)) for d in dets]
    keep = []
    for i, d in enumerate(dets):
        dropped = False
        for j, o in enumerate(dets):
            if i == j or scores[j] <= scores[i]:
                continue
            if _contained(boxes[i], boxes[j]) >= 0.7:
                dropped = True
                break
        if not dropped:
            keep.append(i)
    return [dets[i] for i in keep]


class Subject(object):

    __slots__ = ("box", "view", "stance", "motion", "speed", "score",
                 "view_confidence", "stance_confidence", "lead",
                 "track_id", "facing_camera", "back_turned", "kp_conf",
                 "raw_score", "hits", "kps")

    def __init__(self, box, view="", stance="", motion="static",
                 speed=0.0, score=0.0, view_confidence=0.0,
                 stance_confidence=0.0, lead=(0.0, 0.0), track_id=0,
                 kp_conf=1.0, raw_score=1.0, kps=None):
        self.box = tuple(float(v) for v in box)
        self.view = view
        self.stance = stance
        self.motion = motion
        self.speed = float(speed)
        self.score = float(score)
        self.view_confidence = float(view_confidence)
        self.stance_confidence = float(stance_confidence)
        self.lead = (float(lead[0]), float(lead[1]))
        self.track_id = int(track_id)
        self.facing_camera = False
        self.back_turned = False
        self.kp_conf = float(kp_conf)
        self.raw_score = float(raw_score)
        self.hits = 0
        self.kps = kps

    @property
    def center(self):
        return ((self.box[0] + self.box[2]) * 0.5,
                (self.box[1] + self.box[3]) * 0.5)

    @property
    def width(self) -> float:
        return max(0.0, self.box[2] - self.box[0])

    @property
    def height(self) -> float:
        return max(0.0, self.box[3] - self.box[1])

    def as_dict(self) -> dict:
        return {"box": [round(v, 1) for v in self.box],
                "view": self.view, "stance": self.stance,
                "motion": self.motion, "speed": round(self.speed, 1),
                "score": round(self.score, 4),
                "view_confidence": round(self.view_confidence, 4),
                "stance_confidence": round(self.stance_confidence, 4),
                "lead": [round(self.lead[0], 1), round(self.lead[1], 1)],
                "kp_conf": round(self.kp_conf, 2),
                "id": self.track_id}


class Engine(object):

    def __init__(self, cfgv=None):
        from hpose_analyzer import Analyzer
        from hpose_tracker import Tracker
        cfgv = cfgv or {}
        self.cfgv = dict(cfgv)
        bundle = cfgv.get("hpose_bundle") or None
        model = str(cfgv.get("hpose_model") or DEFAULT_MODEL)
        conf = _conf_of(cfgv)
        self.analyzer = Analyzer(bundle_path=bundle, pose_model=model,
                                 conf=DETECTOR_FLOOR)
        self.tracker = Tracker()
        self.last_error = ""

    def _tta_flag(self, img):
        cfgv = getattr(self, "cfgv", None) or {}
        try:
            tta = str(cfgv.get("hpose_tta", "auto") or "auto").strip().lower()
        except Exception:
            tta = "auto"
        if tta == "off":
            return False
        if tta == "on":
            return True
        return False

    def scan(self, img, origin=(0, 0), t=None, deadline=None,
             zoom_deadline=None, aim=None, aim_radius=0.0):
        if img is None or getattr(img, "size", 0) == 0:
            return []
        if t is None:
            t = time.perf_counter()
        ox, oy = int(origin[0]), int(origin[1])
        analyzer = self.analyzer
        focal = max(1.0, img.shape[1] * 0.9)
        faces = analyzer._face_boxes(img)
        poses = analyzer.pose_detector.detect(img, tta=self._tta_flag(img))
        full = [self._explain_det(analyzer, pose, score, kp, raw_score,
                                  focal, faces, img, ox, oy)
                for pose, score, kp, raw_score in _prefilter(poses)]
        dets = full[:MAX_EXPLAIN_PER_FRAME]
        zdl = zoom_deadline if zoom_deadline is not None else deadline
        now = time.perf_counter()
        side_cap = min(img.shape[:2])
        radius = float(aim_radius or 0.0)
        roi = None
        if aim is not None and 4.0 < radius <= ROI_RADIUS_MAX \
                and side_cap >= 60 and (zdl is None or now < zdl):
            roi = self._roi_pass(analyzer, img, ox, oy, aim, radius, zdl)
            if roi:
                dets = full[:3] + roi[:4]
        if not roi:
            best = full[0].get("score", 0.0) if full else 0.0
            if side_cap >= 120 and (zdl is None or now < zdl) \
                    and (not full or best < 0.45):
                dets = dets + self._crop_pass(analyzer, img, ox, oy,
                                              max(48, side_cap // 2),
                                              deadline=zdl)
                if not dets and (zdl is None or time.perf_counter() < zdl):
                    dets = dets + self._crop_pass(
                        analyzer, img, ox, oy,
                        max(40, side_cap // 3), deadline=zdl)
        dets = _suppress_dets(dets)[:MAX_EXPLAIN_PER_FRAME]
        tracks = self.tracker.update(dets, t)
        out = []
        for tr in tracks:
            sub = Subject(tr.box,
                          view=tr.view,
                          stance=tr.stance,
                          motion=tr.motion_state,
                          speed=tr.speed,
                          score=tr.score,
                          view_confidence=tr.view_probs.get(tr.view, 0.0),
                          stance_confidence=tr.stance_probs.get(tr.stance,
                                                               0.0),
                          track_id=tr.id,
                          kp_conf=getattr(tr, "kp_conf", 1.0),
                          raw_score=getattr(tr, "raw_score", 1.0))
            sub.hits = int(getattr(tr, "hits", 0))
            sub.kps = getattr(tr, "head_kp", None)
            sub.facing_camera = tr.view in ("front", "front_left_45",
                                            "front_right_45")
            sub.back_turned = tr.view in ("back", "back_left_45",
                                          "back_right_45")
            out.append(sub)
        return out

    def _explain_det(self, analyzer, pose, score, kp, raw, focal, faces,
                     img, ox, oy):
        det = analyzer.explain(pose, focal, faces, img)
        box = det.get("box") or (0.0, 0.0, 0.0, 0.0)
        head = None
        try:
            k0 = pose.kps[0]
            if float(k0[2]) >= 0.5:
                head = (float(k0[0]) + ox, float(k0[1]) + oy)
        except Exception:
            head = None
        return {
            "box": (box[0] + ox, box[1] + oy, box[2] + ox, box[3] + oy),
            "score": score,
            "raw_score": raw,
            "kp_conf": kp,
            "head_kp": head,
            "view_probs": det.get("view_probs") or {},
            "stance_probs": det.get("stance_probs") or {},
        }

    def _crop_pass(self, analyzer, img, ox, oy, cs, deadline=None):
        h, w = img.shape[:2]
        cx0, cy0 = (w - cs) // 2, (h - cs) // 2
        crop = img[cy0:cy0 + cs, cx0:cx0 + cs]
        focal = max(1.0, cs * 0.9)
        raw = _prefilter(analyzer.pose_detector.detect(crop),
                         rescue_score=CROP_RESCUE_SCORE,
                         rescue_kp=CROP_RESCUE_KP)
        raw = _suppress_contained(raw)[:MAX_EXPLAIN_PER_FRAME]
        out = []
        for pose, score, kp, raw_score in raw:
            if deadline is not None and time.perf_counter() > deadline:
                break
            out.append(self._explain_det(analyzer, pose, score, kp,
                                         raw_score, focal, [], crop,
                                         cx0 + ox, cy0 + oy))
        return out

    def _roi_pass(self, analyzer, img, ox, oy, aim, radius, deadline=None):
        h, w = img.shape[:2]
        acx, acy = int(aim[0]), int(aim[1])
        if not (0 <= acx < w and 0 <= acy < h):
            return []
        side = int(max(ROI_MIN_SIDE, ROI_MARGIN * float(radius)))
        side = min(side, min(h, w))
        if side < 60 or side >= min(h, w) - 2:
            return []
        x1 = int(max(0, acx - side // 2))
        y1 = int(max(0, acy - side // 2))
        x2 = min(w, x1 + side)
        y2 = min(h, y1 + side)
        x1 = max(0, x2 - side)
        y1 = max(0, y2 - side)
        crop = img[y1:y2, x1:x2]
        ch, cw = crop.shape[:2]
        if min(crop.shape[:2]) < 60:
            return []
        if deadline is not None and time.perf_counter() >= deadline:
            return []
        focal = max(1.0, max(ch, cw) * 0.9)
        raw = _prefilter(analyzer.pose_detector.detect(crop),
                         rescue_score=ROI_RESCUE_SCORE,
                         rescue_kp=ROI_RESCUE_KP)
        chosen = raw
        if len(raw) >= 2:
            chosen = _suppress_contained(raw)[:MAX_EXPLAIN_PER_FRAME]
        out = []
        for pose, score, kp, raw_score in chosen:
            if deadline is not None and time.perf_counter() > deadline:
                break
            out.append(self._explain_det(analyzer, pose, score, kp,
                                         raw_score, focal, [], crop,
                                         x1 + ox, y1 + oy))
        return out

    def lead_of(self, subject, seconds: float):
        if subject is None:
            return (0.0, 0.0)
        track = self.tracker.by_id(subject.track_id)
        if track is None:
            return (0.0, 0.0)
        _x, _y, ox, oy = track.lead(seconds)
        return (ox, oy)

    def reset(self):
        try:
            self.tracker.reset()
        except Exception:
            pass


_ENGINE = None
_LOCK = threading.Lock()
_LOADING = False
_TRIED = False


def _load_in_background(cfgv):
    global _LOADING, _TRIED
    with _LOCK:
        if _ENGINE is not None:
            return _ENGINE
        if _LOADING:
            return None
        _LOADING = True
        _TRIED = True

    def worker():
        global _ENGINE, _LOADING
        try:
            eng = Engine(cfgv)
            with _LOCK:
                _ENGINE = eng
            log("hpose: ready (%s)"
                % (getattr(eng.analyzer.pose_detector, "model_name", "?"),))
        except Exception as exc:
            log("hpose: could not start (%r) - the classical scanner "
                "handles everything." % (exc,))
        finally:
            with _LOCK:
                _LOADING = False

    threading.Thread(target=worker, name="hpose-load", daemon=True).start()
    return None


def engine(cfgv=None, force: bool = False):
    global _TRIED
    with _LOCK:
        if _ENGINE is not None and not force:
            return _ENGINE
    if force:
        with _LOCK:
            _TRIED = False
    try:
        import importlib
        importlib.import_module("cv2")
        importlib.import_module("onnxruntime")
    except Exception as exc:
        with _LOCK:
            if not _TRIED:
                _TRIED = True
                log("hpose: not available (%r) - the classical scanner "
                    "handles everything." % (exc,))
        return None
    return _load_in_background(cfgv)


def preload(cfgv=None):
    return engine(cfgv)


def reset():
    with _LOCK:
        if _ENGINE is not None:
            _ENGINE.reset()


def available() -> bool:
    return engine() is not None


def mode_of(cfgv) -> str:
    try:
        mode = str(cfgv.get("hpose_mode", "off") or "off").strip().lower()
    except Exception:
        mode = "off"
    return "on" if mode in ("on", "true", "1", "yes", "enabled") else "off"


def wanted(cfgv) -> bool:
    if mode_of(cfgv) != "on":
        return False
    return True


def matches_view(subject, want: str) -> bool:
    want = (want or "any").strip().lower()
    if want in ("", "any"):
        return True
    view = (subject.view or "").lower()
    if want == "facing":
        return subject.facing_camera
    if want == "away":
        return subject.back_turned
    if want == "side":
        return view.startswith("side")
    if want == "front":
        return view.startswith("front")
    if want == "back":
        return view.startswith("back")
    return True


def matches_stance(subject, want: str) -> bool:
    want = (want or "any").strip().lower()
    if want in ("", "any"):
        return True
    stance = (subject.stance or "").lower()
    if want == "low":
        return stance in ("crouch", "prone")
    return stance == want


def pick(subjects, cfgv, cx, cy, radius):
    want_view = str(cfgv.get("hpose_view", "any") or "any")
    want_stance = str(cfgv.get("hpose_stance", "any") or "any")
    min_conf = 0.0
    try:
        min_conf = float(cfgv.get("hpose_min_conf", 35) or 0.0) / 100.0
    except (TypeError, ValueError):
        min_conf = 0.0
    motion_priority = bool(cfgv.get("motion_priority", True))
    motion_rank = {"fast": 0, "slow": 1, "static": 2}
    best = None
    best_key = None
    for sub in subjects:
        if getattr(sub, "raw_score", 1.0) < 0.35 and \
                getattr(sub, "hits", 2) < 2:
            continue
        kp = getattr(sub, "kp_conf", 1.0)
        if kp < 0.25 and sub.score < 0.5:
            continue
        if sub.score < min_conf:
            continue
        if not matches_view(sub, want_view):
            continue
        if not matches_stance(sub, want_stance):
            continue
        sxc, syc = sub.center
        dist = math.hypot(sxc - cx, syc - cy)
        if radius and dist > radius:
            continue
        if motion_priority:
            mrank = motion_rank.get(getattr(sub, "motion", "static"), 2)
            key = (mrank, round(dist, 1), -sub.score)
        else:
            key = (round(dist, 1), -sub.score)
        if best_key is None or key < best_key:
            best_key = key
            best = sub
    return best
