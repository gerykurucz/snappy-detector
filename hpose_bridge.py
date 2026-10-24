"""The app side of human detection: engine lifecycle and target choice.

The pipeline itself lives in the flat ``hpose_*`` modules beside this
one (detector, pose fit, view/stance heads, motion, tracker, analyzer)
and in the weights under ``models/``.  Human detection answers the
questions the classical scanner in ``vision.py`` can only guess at: is
that really a person, which way are they facing, are they crouching,
and are they moving.  This module loads the pipeline, keeps it out of
the way when it is not wanted, and translates its answers into the
shape the rest of the program already understands.

Two rules govern the whole file.

**It never breaks the program.**  Human detection needs opencv,
onnxruntime and the model files under ``models/``.  When they are
missing this module says so once in the log and switches itself off;
the classical scanner carries on exactly as before.  The heavy
libraries are not imported until they are actually needed, so the
program starts normally without them.

**It tracks in screen coordinates.**  The scan region is a square that
follows the cursor, so every frame is a different window onto the
screen.  A tracker that worked in the window's own coordinates would
lose every target the moment the cursor moved.  The detections are
therefore moved into screen coordinates before they reach the tracker,
and the velocity and the lead are measured against the screen, which
is where they mean something.
"""

import math
import threading
import time

try:
    from system_core import log
except Exception:                                    # pragma: no cover
    def log(_text):
        pass


#: The pose model to use.  The small one is fast; the bigger one is
#: better at small people.
DEFAULT_MODEL = "yolo11n-pose.onnx"

#: The detector's own keep-floor.  Deliberately far below the user's
#: "how sure a person must be" gate: small and half-hidden people score
#: 0.15-0.35 with a perfectly good skeleton behind them, and throwing
#: those away before the skeleton is even looked at would waste them.
#: The user gate is applied later, in pick(), on the fused confidence.
DETECTOR_FLOOR = 0.10

#: How the bridge reports that it cannot run.
_UNAVAILABLE_REASON = ""


def _conf_of(cfgv) -> float:
    """The detector confidence, from the percentage the user sets."""
    try:
        pct = float(cfgv.get("hpose_min_conf", 35) or 35)
    except (TypeError, ValueError):
        pct = 35.0
    return max(0.05, min(0.95, pct / 100.0))


#: Rescue band at crop scale, measured against the hallucination
#: distribution on person-free clutter (see HPOSE.md, "Confidence").
CROP_RESCUE_SCORE = 0.05
CROP_RESCUE_KP = 0.80

#: How many detections one frame may pay the classification stage for.
#: explain() runs the geometric fit and both heads; on a crowded frame
#: the raw detector can offer a dozen boxes and the assist only ever
#: picks one near the cursor.
MAX_EXPLAIN_PER_FRAME = 6


def _prefilter(poses, rescue_score=0.20, rescue_kp=0.6):
    """Score/skeleton gate + fused confidence, before any expensive work.

    A weak box with a confident skeleton is a small or partly hidden
    person; a confident box with no skeleton behind it is texture the
    network dressed up as a body.  Keep the union of "sure box" and
    "sure skeleton", drop the rest.  The fused confidence (box and
    skeleton are two independent witnesses) is the score everything
    downstream works with.

    The rescue band is per scale: at whole-window magnification a 20 px
    person still scores 0.2-0.35, but inside a magnified centre crop the
    same person can score 0.05-0.2 while carrying a perfect skeleton -
    there the skeleton alone is the witness worth trusting (callers
    pass a wider band for zoom scales).
    """
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
    """How much of ``inner``'s area sits inside ``outer``."""
    ix1, iy1 = max(inner[0], outer[0]), max(inner[1], outer[1])
    ix2, iy2 = min(inner[2], outer[2]), min(inner[3], outer[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    area = max(1.0, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    return iw * ih / area


def _suppress_contained(dets):
    """Drop part-of-person duplicates of a better whole-person det.

    At these magnifications the detector sometimes reports a head or a
    torso beside the full body it also found.  Two tracks for one
    person make the pick flip between them and reset the motion
    history, so a moving target reads as static.  A box that sits
    inside a better-scoring box is the same person twice.
    """
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
    """Mean confidence of the four torso joints (shoulders and hips).

    The torso is what a person cannot hide: at the sizes this app works
    at, a real body keeps its torso joints confident long after the box
    score has fallen into the noise, and a background the network
    hallucinates a body onto rarely sustains a skeleton.
    """
    try:
        k = pose.kps
        return float(sum(float(k[i][2]) for i in (5, 6, 11, 12)) / 4.0)
    except Exception:
        return 0.0


class Subject(object):
    """One person, in screen coordinates.

    ``box`` is ``(x1, y1, x2, y2)`` on the screen, ``lead`` is the
    offset the target will travel in the configured lead time and
    ``view`` / ``stance`` are the hpose answers.  ``score`` is the
    fused box+skeleton confidence (see ``Engine.scan``), and
    ``kp_conf`` the skeleton quality behind it.
    """

    __slots__ = ("box", "view", "stance", "motion", "speed", "score",
                 "view_confidence", "stance_confidence", "lead",
                 "track_id", "facing_camera", "back_turned", "kp_conf",
                 "raw_score", "hits")

    def __init__(self, box, view="", stance="", motion="static",
                 speed=0.0, score=0.0, view_confidence=0.0,
                 stance_confidence=0.0, lead=(0.0, 0.0), track_id=0,
                 kp_conf=1.0, raw_score=1.0):
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
    """The loaded hpose pipeline plus a screen space tracker."""

    def __init__(self, cfgv=None):
        from hpose_analyzer import Analyzer
        from hpose_tracker import Tracker
        cfgv = cfgv or {}
        bundle = cfgv.get("hpose_bundle") or None
        model = str(cfgv.get("hpose_model") or DEFAULT_MODEL)
        conf = _conf_of(cfgv)
        # the detector runs at its own low floor: every scale-specific
        # gate lives in _prefilter/pick, so nothing is thrown away
        # before its skeleton has been looked at
        self.analyzer = Analyzer(bundle_path=bundle, pose_model=model,
                                 conf=DETECTOR_FLOOR)
        self.tracker = Tracker()
        self.last_error = ""

    # -- scanning ------------------------------------------------------
    def scan(self, img, origin=(0, 0), t=None, deadline=None,
             zoom_deadline=None):
        """Find the people in a captured region.

        ``img`` is the captured region and ``origin`` is where that
        region sits on the screen.  Returns a list of
        :class:`Subject` in screen coordinates.

        When the whole-window pass finds nobody and there is still
        time in the scan budget (``deadline``, perf_counter seconds),
        a second pass looks at the centre half of the window at twice
        the magnification: a 20 px person in a 140 px window is below
        the detector's floor at window scale and comfortably above it
        at crop scale.  The centre is where the assist looks anyway -
        pick() only accepts subjects near the cursor.

        The zoom pass runs when the whole-window pass found nobody or
        nothing confident, and is bounded by ``zoom_deadline`` (the
        scan budget times four, by default): the extra time is spent
        only on scans that have nothing to click on yet, while scans
        that already see a person keep their single-pass latency.
        """
        if img is None or getattr(img, "size", 0) == 0:
            return []
        if t is None:
            t = time.perf_counter()
        ox, oy = int(origin[0]), int(origin[1])
        analyzer = self.analyzer
        focal = max(1.0, img.shape[1] * 0.9)
        faces = analyzer._face_boxes(img)
        poses = analyzer.pose_detector.detect(img)
        raw = _prefilter(poses)
        if min(img.shape[:2]) >= 120:
            # Magnified centre crops, but only when the whole-window
            # pass has nothing confident: a 20-30 px person is below
            # the detector's floor at window scale and comfortably
            # above it in the crop, while a crop consulted on every
            # scan would hallucinate centre clutter - exactly where
            # the target sits - into competing tracks.
            zdl = zoom_deadline if zoom_deadline is not None else deadline
            best = raw[0][1] if raw else 0.0
            now = time.perf_counter()
            if (not raw or best < 0.45) and (zdl is None or now < zdl):
                raw = raw + self._crop_pass(analyzer, img, ox, oy,
                                            max(48, min(img.shape[:2]) // 2),
                                            deadline=zdl)
                if not raw and (zdl is None or time.perf_counter() < zdl):
                    raw = raw + self._crop_pass(
                        analyzer, img, ox, oy,
                        max(40, min(img.shape[:2]) // 3), deadline=zdl)
        raw = _suppress_contained(raw)[:MAX_EXPLAIN_PER_FRAME]
        dets = [self._explain_det(analyzer, pose, score, kp, raw_score,
                                  focal, faces, img, ox, oy)
                for pose, score, kp, raw_score in raw]
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
            sub.facing_camera = tr.view in ("front", "front_left_45",
                                            "front_right_45")
            sub.back_turned = tr.view in ("back", "back_left_45",
                                          "back_right_45")
            out.append(sub)
        return out

    def _explain_det(self, analyzer, pose, score, kp, raw, focal, faces,
                     img, ox, oy):
        """Run the classification stage for one prefiltered pose."""
        det = analyzer.explain(pose, focal, faces, img)
        box = det.get("box") or (0.0, 0.0, 0.0, 0.0)
        return {
            "box": (box[0] + ox, box[1] + oy, box[2] + ox, box[3] + oy),
            "score": score,
            "raw_score": raw,
            "kp_conf": kp,
            "view_probs": det.get("view_probs") or {},
            "stance_probs": det.get("stance_probs") or {},
        }

    def _crop_pass(self, analyzer, img, ox, oy, cs, deadline=None):
        """One magnified look at the centre of the window.

        A magnified crop hallucinates more readily than the whole
        window, so the skeleton carries more of the burden here: the
        rescue band is score >= 0.05 with kp_conf >= 0.70 (measured:
        true 20-30 px people sit at 0.05-0.35 with kp 0.7-1.0 there,
        while window-scale texture blobs do not sustain a skeleton).
        """
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

    def lead_of(self, subject, seconds: float):
        """How far the subject travels in ``seconds``."""
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
    """Start loading the engine on its own thread.

    Loading the models takes a moment, and doing it inside a scan
    would eat the scan budget.  The first request therefore starts the
    load and returns nothing; the classical scanner covers that scan
    and the engine is ready for the next one.
    """
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
    """The shared engine, loaded on first use.

    Returns ``None`` when hpose cannot run or is not ready yet.  The
    failure is logged once so the log does not fill up with the same
    message every scan.
    """
    global _TRIED
    with _LOCK:
        if _ENGINE is not None and not force:
            return _ENGINE
    if force:
        with _LOCK:
            _TRIED = False
    try:
        # The pipeline modules are ordinary app code; what they cannot
        # do without these two is run the networks.  Probe them here so
        # a machine without them degrades to the classical scanner.
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
    """Ask for the engine to be ready before it is needed."""
    return engine(cfgv)


def reset():
    """Forget the tracks.  Used when the scan context changes."""
    with _LOCK:
        if _ENGINE is not None:
            _ENGINE.reset()


def available() -> bool:
    """True when hpose can be used."""
    return engine() is not None


def mode_of(cfgv) -> str:
    """The configured mode: ``off`` or ``on``."""
    try:
        mode = str(cfgv.get("hpose_mode", "off") or "off").strip().lower()
    except Exception:
        mode = "off"
    return "on" if mode in ("on", "true", "1", "yes", "enabled") else "off"


def wanted(cfgv) -> bool:
    """True when the hpose path should run for this scan."""
    if mode_of(cfgv) != "on":
        return False
    return True


def matches_view(subject, want: str) -> bool:
    """Does the subject's view match what was asked for?"""
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
    """Does the subject's stance match what was asked for?"""
    want = (want or "any").strip().lower()
    if want in ("", "any"):
        return True
    stance = (subject.stance or "").lower()
    if want == "low":
        return stance in ("crouch", "prone")
    return stance == want


def pick(subjects, cfgv, cx, cy, radius):
    """Choose the subject the assist should aim at.

    The nearest matching person wins, the same way the classical
    scanner picks the nearest item.  Ties go to the one the detector
    is surest of.
    """
    want_view = str(cfgv.get("hpose_view", "any") or "any")
    want_stance = str(cfgv.get("hpose_stance", "any") or "any")
    min_conf = 0.0
    try:
        min_conf = float(cfgv.get("hpose_min_conf", 35) or 0.0) / 100.0
    except (TypeError, ValueError):
        min_conf = 0.0
    best = None
    best_key = None
    for sub in subjects:
        # a track born from a weak box rescued by its skeleton is a
        # one-frame rumour until it is seen twice; confident boxes and
        # confirmed tracks need no such wait
        if getattr(sub, "raw_score", 1.0) < 0.35 and \
                getattr(sub, "hits", 2) < 2:
            continue
        kp = getattr(sub, "kp_conf", 1.0)
        # no skeleton and no confident box either: not a person
        if kp < 0.25 and sub.score < 0.5:
            continue
        # sub.score is the fused box+skeleton confidence computed in
        # scan(); the user's "how sure a person must be" gates on it
        if sub.score < min_conf:
            continue
        if not matches_view(sub, want_view):
            continue
        if not matches_stance(sub, want_stance):
            continue
        sxc, syc = sub.center
        dist = math.hypot(sxc - cx, syc - cy)
        if radius and dist > radius + 10.0:
            continue
        key = (round(dist, 1), -sub.score)
        if best_key is None or key < best_key:
            best_key = key
            best = sub
    return best
