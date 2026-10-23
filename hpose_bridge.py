"""Bridge between Snappy AI and the hpose human analysis package.

hpose answers the questions the classical scanner in ``vision.py`` can
only guess at: is that really a person, which way are they facing, are
they crouching, and are they moving.  This module loads it, keeps it
out of the way when it is not wanted, and translates its answers into
the shape the rest of the program already understands.

Two rules govern the whole file.

**It never breaks the program.**  hpose needs onnxruntime and two
model files.  When they are missing the bridge says so once in the log
and switches itself off; the classical scanner carries on exactly as
before.  Nothing here is imported until it is actually needed, so the
program starts normally on a machine that has never heard of hpose.

**It tracks in screen coordinates.**  The scan region is a square that
follows the cursor, so every frame is a different window onto the
screen.  A tracker that worked in the window's own coordinates would
lose every target the moment the cursor moved.  The detections are
therefore moved into screen coordinates before they reach the tracker,
and the velocity and the lead are measured against the screen, which
is where they mean something.
"""

import math
import os
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

#: How the bridge reports that it cannot run.
_UNAVAILABLE_REASON = ""


def _conf_of(cfgv) -> float:
    """The detector confidence, from the percentage the user sets."""
    try:
        pct = float(cfgv.get("hpose_min_conf", 35) or 35)
    except (TypeError, ValueError):
        pct = 35.0
    return max(0.05, min(0.95, pct / 100.0))


class Subject(object):
    """One person, in screen coordinates.

    ``box`` is ``(x1, y1, x2, y2)`` on the screen, ``lead`` is the
    offset the target will travel in the configured lead time and
    ``view`` / ``stance`` are the hpose answers.
    """

    __slots__ = ("box", "view", "stance", "motion", "speed", "score",
                 "view_confidence", "stance_confidence", "lead",
                 "track_id", "facing_camera", "back_turned")

    def __init__(self, box, view="", stance="", motion="static",
                 speed=0.0, score=0.0, view_confidence=0.0,
                 stance_confidence=0.0, lead=(0.0, 0.0), track_id=0):
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
                "id": self.track_id}


class Engine(object):
    """The loaded hpose pipeline plus a screen space tracker."""

    def __init__(self, cfgv=None):
        from hpose.analyzer import Analyzer
        from hpose.tracker import Tracker
        cfgv = cfgv or {}
        bundle = cfgv.get("hpose_bundle") or None
        model = str(cfgv.get("hpose_model") or DEFAULT_MODEL)
        conf = _conf_of(cfgv)
        try:
            self.analyzer = Analyzer(bundle_path=bundle, pose_model=model,
                                     conf=conf)
        except TypeError:
            # an older hpose without the pose_model argument
            self.analyzer = Analyzer(bundle_path=bundle, conf=conf)
        self.tracker = Tracker()
        self.last_error = ""

    # -- scanning ------------------------------------------------------
    def scan(self, img, origin=(0, 0), t=None):
        """Find the people in a captured region.

        ``img`` is the captured region and ``origin`` is where that
        region sits on the screen.  Returns a list of
        :class:`Subject` in screen coordinates.
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
        dets = []
        for pose in poses:
            det = analyzer.explain(pose, focal, faces, img)
            box = det.get("box") or (0.0, 0.0, 0.0, 0.0)
            screen_box = (box[0] + ox, box[1] + oy,
                          box[2] + ox, box[3] + oy)
            dets.append({
                "box": screen_box,
                "score": float(det.get("score", 1.0)),
                "view_probs": det.get("view_probs") or {},
                "stance_probs": det.get("stance_probs") or {},
            })
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
                          track_id=tr.id)
            sub.facing_camera = tr.view in ("front", "front_left_45",
                                            "front_right_45")
            sub.back_turned = tr.view in ("back", "back_left_45",
                                          "back_right_45")
            out.append(sub)
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
    global _ENGINE, _LOADING, _TRIED
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
    global _ENGINE, _TRIED
    with _LOCK:
        if _ENGINE is not None and not force:
            return _ENGINE
    if force:
        with _LOCK:
            _TRIED = False
    try:
        import hpose  # noqa: F401
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
    global _ENGINE
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
