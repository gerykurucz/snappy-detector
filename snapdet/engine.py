"""The snapdet engine: every path a person can be found by, in one call.

Frame mode (the whole picture), per frame, within a hard budget:

1. **coarse**  - the whole frame letterboxed to 640 (finds >= ~100 px
   people at 1080p, cheap, always runs);
2. **sweep**   - a few tiles per frame of one or more round-robin zoom
   grids, so every pixel is re-examined at native resolution and
   beyond every few seconds (finds stationary small people);
3. **motion**  - camera-motion-compensated frame differencing yields
   candidate blobs (~9 ms), and every blob that is not already a known
   person gets a **zoom inspection** pass at the zoom its size calls
   for (finds moving 5-px people);
4. detections from all sources are fused, classified or size-gated,
   and handed to the tracker, which coasts identities through the
   frames where a tiny target was not re-found.

Circle mode (``capture_mode="circle"``) is for a capture that only
ever sees a disc of radius 50-100 px at the exact screen centre - an
aim region.  Close people are then FRAGMENTS: head+shoulders, a chest,
hips with no torso above.  A COCO-trained pose model scores a fragment
floating in a small masked square at ~0.0, because it has never seen
one; the same fragment flush with the BOTTOM edge of a 640 canvas, at
the right scale, scores 0.5-0.75, because that is exactly what a
person truncated by the frame border looks like.  Circle mode
therefore adds **context passes** at several scales, masks keypoints
outside the disc (they cannot be real), flags every detection that
touches the disc border as ``truncated``, and reports stance as
"unknown" for truncated bodies - a chest tells you which way a person
faces, never whether they are crouching.

``process(frame, t)`` is the streaming entry point; ``process_still``
trades time for recall on a single picture by sweeping *everything*.
"""

from __future__ import annotations

import math
import time

import numpy as np

from . import features
from . import posefit
from .bundle import load_bundle
from .config import Config
from .detector import FaceDetector, PoseDetector
from .inspector import Inspector
from .keypoints import Pose
from .motion import KalmanBox  # noqa: F401  (used by tracker)
from .motionroi import MotionAnalyzer
from .multiscale import SweepScheduler, merge_detections
from .stance import StanceClassifier
from .tracker import Tracker
from .view import ViewClassifier

MASK_GREY = 114


class Brain(object):
    """Pose -> view/stance, with an honest size gate.

    Below ``min_classify_height`` the 17 keypoints of a detected person
    fit in a handful of pixels; the pose model's joints there are noise,
    and a classifier fed noise answers noise.  Such detections are
    passed through as ``view="unknown", stance="unknown"`` with box,
    score, tracking and motion still fully alive - which is everything
    a 10-px target can truthfully be described by.

    A ``truncated`` body - one that runs out of the picture - is the
    other honest case: its stance cannot be known (the legs may be
    doing anything outside the disc), and its view can only be judged
    when the head is actually in the picture.
    """

    def __init__(self, cfg: Config, bundle_path: str = None,
                 face_detector=None):
        self.cfg = cfg
        self.bundle = load_bundle(bundle_path)
        self.view_clf = ViewClassifier(self.bundle)
        self.stance_clf = StanceClassifier(self.bundle)
        self.face = face_detector

    @property
    def ready(self) -> bool:
        return self.view_clf.ready and self.stance_clf.ready

    # -- face logic (from the hpose analyser, kept as-is) --------------
    @staticmethod
    def _head_region(box, frac=0.26):
        x1, y1, x2, y2 = box
        w = x2 - x1
        h = y2 - y1
        return (x1 - w * 0.12, y1 - h * 0.04, x2 + w * 0.12, y1 + h * frac)

    def _has_face(self, faces, box) -> bool:
        if not faces:
            return False
        rx1, ry1, rx2, ry2 = self._head_region(box, 0.26)
        for f in faces:
            fx = (f[0] + f[2]) * 0.5
            fy = (f[1] + f[3]) * 0.5
            if rx1 <= fx <= rx2 and ry1 <= fy <= ry2:
                return True
        return False

    def _head_crop(self, img, kps, box):
        import cv2
        h_img, w_img = img.shape[:2]
        pts = [(float(kps[i][0]), float(kps[i][1]))
               for i in (0, 1, 2, 3, 4) if kps[i][2] >= 0.25]
        if pts:
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            shoulder = abs(float(kps[6][0]) - float(kps[5][0]))
            radius = max(shoulder * 0.7, 10.0)
        else:
            x1, y1, x2, y2 = box
            radius = max((x2 - x1) * 0.20, (y2 - y1) * 0.12, 10.0)
            cx = (x1 + x2) * 0.5
            cy = y1 + radius * 0.7
        radius = min(radius, 0.45 * min(h_img, w_img))
        x1 = int(max(0, cx - radius)); y1 = int(max(0, cy - radius))
        x2 = int(min(w_img, cx + radius)); y2 = int(min(h_img, cy + radius))
        if x2 - x1 < 8 or y2 - y1 < 8:
            return None
        crop = img[y1:y2, x1:x2]
        size = 256
        scale = size / float(max(crop.shape[0], crop.shape[1]))
        return cv2.resize(crop, (max(8, int(crop.shape[1] * scale)),
                                 max(8, int(crop.shape[0] * scale))),
                          interpolation=cv2.INTER_CUBIC)

    def _face_at(self, img, kps, box, faces) -> bool:
        if self._has_face(faces, box):
            return True
        if self.face is None:
            return False
        crop = self._head_crop(img, kps, box)
        if crop is None:
            return False
        try:
            return bool(self.face.detect(crop))
        except Exception:
            return False

    # -- the gate -------------------------------------------------------
    def explain(self, work_img, det: dict, faces=None,
                focal_px: float = None) -> dict:
        """Add view/stance (or the honest 'unknown') to a detection.

        ``work_img`` is the image the detection's coordinates live in
        (the full frame, or the masked disc crop in circle mode).
        """
        x1, y1, x2, y2 = det["box"]
        h = float(y2 - y1)
        det["height"] = h
        det["width"] = float(x2 - x1)
        kps = det.get("kps")
        trunc = bool(det.get("truncated"))
        if kps is None or h < self.cfg.min_classify_height:
            det["size_gated"] = True
            det["view"] = "unknown"
            det["stance"] = "unknown"
            det["view_probs"] = {"unknown": 1.0}
            det["stance_probs"] = {"unknown": 1.0}
            det["fit"] = None
            det["has_face"] = False
            return det
        det["size_gated"] = False
        if focal_px is None:
            focal_px = max(1.0, work_img.shape[1] * self.cfg.focal_ratio)
        has_face = self._face_at(work_img, kps, det["box"], faces or [])
        fit = posefit.fit(kps, focal_px=focal_px, face_visible=has_face)
        pose = Pose(kps, det["box"], det["score"])
        feats = features.extract(pose, fit=fit)
        view = self.view_clf.predict(feats, fit, face_visible=has_face)
        stance = self.stance_clf.predict(feats)
        view_name, view_probs, yaw = view.name, view.probs, view.yaw
        stance_name, stance_probs = stance.name, stance.probs
        if trunc:
            # a fragment tells you where the body is and - when the
            # head is in the picture - which way it faces; it can
            # never tell you what the legs are doing
            stance_name = "unknown"
            stance_probs = {"unknown": 1.0}
            head_conf = float(np.mean(kps[0:5, 2]))
            if head_conf < 0.25 and not has_face:
                view_name = "unknown"
                view_probs = {"unknown": 1.0}
                yaw = 0.0
        det.update({
            "fit": fit,
            "has_face": has_face,
            "view": view_name,
            "view_probs": view_probs,
            "stance": stance_name,
            "stance_probs": stance_probs,
            "yaw": yaw,
        })
        return det


class CandidateMemory(object):
    """Temporal evidence accumulation for targets no single frame trusts.

    Every motion-gated inspection reports what it saw, including weak
    scores.  Evidence at a smoothly moving location accumulates with a
    per-frame decay; when the accumulated plausible score crosses
    ``memory_confirm`` with at least ``memory_min_hits`` sightings, the
    candidate is *promoted* to a real detection.  Noise blobs drift and
    flicker, so they rarely stack up at one predicted position; a
    walking 8-px person stacks up within a few frames.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cands = []

    def reset(self):
        self.cands = []

    def update(self, attempts: list, t: float) -> list:
        """Feed this frame's inspection attempts; return promotions.

        ``attempts``: dicts with cx, cy, score (plausible-best), est_h.
        """
        c = self.cfg
        promoted = []
        matched_ids = set()
        for a in sorted(attempts, key=lambda a: -a["score"]):
            if a["score"] <= 0.0:
                continue
            best_i, best_d = None, 1e18
            for i, cand in enumerate(self.cands):
                if i in matched_ids:
                    continue
                dt = max(0.0, t - cand["t"])
                px = cand["cx"] + cand["vx"] * dt
                py = cand["cy"] + cand["vy"] * dt
                gate = max(c.memory_gate,
                           min(2.0 * a["est_h"], c.memory_gate_max))
                d = math.hypot(px - a["cx"], py - a["cy"])
                if d < gate and d < best_d:
                    best_i, best_d = i, d
            if best_i is not None:
                cand = self.cands[best_i]
                matched_ids.add(best_i)
                dt = max(1e-3, t - cand["t"])
                nvx = (a["cx"] - cand["cx"]) / dt
                nvy = (a["cy"] - cand["cy"]) / dt
                cand["vx"] = 0.6 * cand["vx"] + 0.4 * nvx
                cand["vy"] = 0.6 * cand["vy"] + 0.4 * nvy
                cand["cx"], cand["cy"], cand["t"] = a["cx"], a["cy"], t
                cand["acc"] = cand["acc"] * c.memory_decay + a["score"]
                cand["hits"] += 1
                cand["misses"] = 0
                cand["est_h"] = 0.5 * cand["est_h"] + 0.5 * a["est_h"]
                cand["best_hit"] = max(cand["best_hit"], a["score"])
                if (cand["acc"] >= c.memory_confirm
                        and cand["hits"] >= c.memory_min_hits
                        and cand["best_hit"] >= c.memory_best_hit):
                    promoted.append(cand)
            else:
                self.cands.append({"cx": a["cx"], "cy": a["cy"],
                                   "vx": 0.0, "vy": 0.0, "t": t,
                                   "acc": a["score"], "hits": 1,
                                   "misses": 0, "best_hit": a["score"],
                                   "est_h": a["est_h"]})
        # a promoted candidate is NOT deleted: the target is still out
        # there and must be re-confirmed from a running start, not from
        # zero, or the track starves between promotions
        for cand in promoted:
            cand["acc"] = 0.5 * c.memory_confirm
        # age out everything not seen this frame
        alive = []
        for cand in self.cands:
            if cand["t"] < t - 1e-9:
                cand["misses"] += 1
                cand["acc"] *= c.memory_decay
            if cand["misses"] <= c.memory_max_age and \
                    cand["acc"] > 0.02:
                alive.append(cand)
        self.cands = alive
        return promoted[:c.memory_max_promote]

    def snapshot(self) -> list:
        return [(c["cx"], c["cy"], c["acc"], c["hits"]) for c in self.cands]


def _edge_touch(box, w, h, margin=3.0) -> bool:
    return (box[0] <= margin or box[1] <= margin
            or box[2] >= w - margin or box[3] >= h - margin)


class SnapEngine(object):
    """The whole detector.  ``process(frame, t)`` -> list of tracks."""

    def __init__(self, cfg: Config = None, bundle_path: str = None):
        self.cfg = cfg or Config()
        c = self.cfg
        self.circle = (str(c.capture_mode).lower() == "circle")
        self.pose = PoseDetector(model=c.pose_model, conf=c.conf_floor,
                                 imgsz=c.imgsz, download=c.download,
                                 threads=c.ort_threads, device=c.device)
        self.face = None
        if c.use_face:
            try:
                self.face = FaceDetector(
                    score_threshold=(c.circle_face_threshold
                                     if self.circle else 0.45),
                    download=c.download)
            except Exception:
                self.face = None
        self.brain = Brain(c, bundle_path, face_detector=self.face)
        self.tracker = Tracker(max_age=c.track_max_age,
                               low_score=c.track_low_score)
        self.sweep = SweepScheduler(c.circle_sweep_zooms if self.circle
                                    else c.sweep_zooms, c.sweep_overlap)
        if self.circle:
            # the disc is small: analyse motion at full resolution and
            # let blobs be big - a close fragment IS big
            self.motion = MotionAnalyzer(scale=1.0,
                                         threshold=c.motion_threshold,
                                         min_area=c.motion_min_area,
                                         max_area=c.circle_motion_max_area,
                                         max_rois=c.motion_max_rois)
        else:
            self.motion = MotionAnalyzer(scale=c.motion_scale,
                                         threshold=c.motion_threshold,
                                         min_area=c.motion_min_area,
                                         max_area=c.motion_max_area,
                                         max_rois=c.motion_max_rois)
        self.inspector = Inspector(self.pose, c)
        self.memory = CandidateMemory(c) if c.memory_enabled else None
        self.last_debug = {}

    def describe(self) -> str:
        cap = ("circle R=%d (aim region)" % self.cfg.circle_radius
               if self.circle else "frame")
        return ("snapdet engine\n"
                "  capture    : %s\n"
                "  pose model : %s\n"
                "  face model : %s\n"
                "  view head  : %s\n"
                "  stance head: %s\n"
                "  sweep grids: %s\n"
                "  budget     : %d passes/frame (%d inspect)"
                % (cap, self.pose.model_name,
                   "yes" if self.face else "no",
                   "ready" if self.brain.view_clf.ready else "not trained",
                   "ready" if self.brain.stance_clf.ready else "not trained",
                   ", ".join("z%d x%d" % zk for zk in self.cfg.sweep_zooms),
                   self.pass_budget(), self.cfg.inspect_budget))

    def pass_budget(self) -> int:
        return (self.cfg.pass_budget_circle if self.circle
                else self.cfg.pass_budget)

    # -- circle helpers ---------------------------------------------------
    def _crop_circle(self, frame):
        """Cut the aim disc out of the screen and mask the rest.

        Accepts either a full screen frame (the usual case: the engine
        crops the exact centre itself) or an already-cropped square of
        exactly 2R x 2R.  Returns ``(crop, (ox, oy), R)`` where the
        offset maps disc coordinates back to screen coordinates.
        """
        H, W = frame.shape[:2]
        R = int(round(self.cfg.circle_radius))
        R = max(20, min(R, min(H, W) // 2))
        if W == H == 2 * R:
            crop_in = frame
            ox, oy = 0, 0
        else:
            cx, cy = W // 2, H // 2
            ox, oy = cx - R, cy - R
            crop_in = frame[oy:oy + 2 * R, ox:ox + 2 * R]
        crop = crop_in.copy()
        yy, xx = np.mgrid[0:crop.shape[0], 0:crop.shape[1]]
        rc = (crop.shape[0] / 2.0, crop.shape[1] / 2.0)
        outside = ((xx - rc[1] + 0.5) ** 2
                   + (yy - rc[0] + 0.5) ** 2) > (R - 0.5) ** 2
        crop[outside] = MASK_GREY
        return crop, (ox, oy), R

    @staticmethod
    def _mask_kps_disc(kps, R):
        """Zero the confidence of joints outside the visible disc.

        The pose model happily invents positions for joints it cannot
        see; inside a disc, anything beyond the radius is definitionally
        unseen, so its confidence must not survive into the features.
        """
        if kps is None:
            return kps
        xx = kps[:, 0] - R
        yy = kps[:, 1] - R
        kps[xx * xx + yy * yy > R * R, 2] = 0.0
        return kps

    def _context_dets(self, crop, R):
        """Bottom-flush context passes: fragments as border truncations."""
        import cv2
        out = []
        side = crop.shape[0]
        for s in self.cfg.circle_context_scales:
            w2 = max(8, int(round(side * s)))
            img = cv2.resize(crop, (w2, w2),
                             interpolation=(cv2.INTER_AREA if s < 1.0
                                            else cv2.INTER_CUBIC))
            canvas = np.full((640, 640, 3), MASK_GREY, np.uint8)
            x0 = (640 - w2) // 2
            y0 = 640 - w2
            canvas[y0:y0 + w2, x0:x0 + w2] = img
            for p in self.pose.detect(canvas):
                if p.score < self.cfg.conf_context:
                    continue
                b = p.box
                kps = p.kps.copy()
                box = ((b[0] - x0) / s, (b[1] - y0) / s,
                       (b[2] - x0) / s, (b[3] - y0) / s)
                kps[:, 0] = (kps[:, 0] - x0) / s
                kps[:, 1] = (kps[:, 1] - y0) / s
                out.append({"box": box, "score": float(p.score),
                            "kps": kps, "source": "context",
                            "zoom": 1.0 / s,
                            "strong": bool(p.score
                                           >= self.cfg.conf_context_strong),
                            "truncated": _edge_touch(box, side, side, 4.0)})
        return out

    def _is_truncated(self, d, W, H) -> bool:
        """Does this detection's body run outside the visible region?"""
        if d.get("kps") is None:
            return True
        if _edge_touch(d["box"], W, H, 6.0 if self.circle else 3.0):
            return True
        if self.circle:
            kps = d["kps"]
            ankles = max(float(kps[15][2]), float(kps[16][2]))
            return ankles < 0.25
        return False

    def _prune_runaways(self, frame) -> None:
        """Drop tracks whose coasted centre left the picture for good.

        Velocity injection makes sparse-measurement coasting follow the
        target; it can also, on a bad measurement pair, fling a track
        off the screen.  Anything that coasts beyond the margin is
        gone rather than immortal.
        """
        H, W = frame.shape[:2]
        m = 120.0
        keep = []
        for tr in self.tracker.tracks:
            cx, cy = tr.center
            if -m <= cx <= W + m and -m <= cy <= H + m:
                keep.append(tr)
        self.tracker.tracks = keep

    def _face_fallback(self, merged, faces, R, W, H):
        """A face is a person, even when the pose model could make
        nothing of the fragment around it."""
        c = self.cfg
        if not faces:
            return
        for f in faces:
            if f[4] < c.circle_face_threshold:
                continue
            fcx, fcy = (f[0] + f[2]) * 0.5, (f[1] + f[3]) * 0.5
            # a 'face' out on the grey mask ring is texture, not a
            # person: the disc content ends at the radius
            if math.hypot(fcx - R, fcy - R) > 0.85 * R:
                continue
            covered = any(b[0] - 2 <= fcx <= b[2] + 2
                          and b[1] - 2 <= fcy <= b[3] + 2
                          for b in (d["box"] for d in merged))
            if covered:
                continue
            fw, fh = f[2] - f[0], f[3] - f[1]
            merged.append({
                "box": (max(0.0, f[0] - 0.8 * fw),
                        max(0.0, f[1] - 0.6 * fh),
                        min(float(W), f[2] + 0.8 * fw),
                        min(float(H), f[1] + 3.2 * fh)),
                "score": float(f[4]), "kps": None, "source": "face",
                "zoom": 0.0, "truncated": True, "seed_ok": True,
            })

    def _classify_all(self, work, frame, merged, faces):
        c = self.cfg
        focal = (max(1.0, frame.shape[1] * c.focal_ratio)
                 if self.circle else None)
        for d in merged:
            self.brain.explain(work, d, faces, focal_px=focal)
            if d.get("source") == "face":
                d["has_face"] = True

    # -- helpers ---------------------------------------------------------
    def _coarse(self, work, passes_used):
        out = []
        for p in self.pose.detect(work):
            if p.score >= self.cfg.conf_coarse:
                out.append({"box": p.box, "score": float(p.score),
                            "kps": p.kps.copy(), "source": "coarse",
                            "zoom": 1.0})
        return out

    @staticmethod
    def _near_any(cx, cy, boxes, radius_fn):
        for b in boxes:
            bx = (b[0] + b[2]) * 0.5
            by = (b[1] + b[3]) * 0.5
            span = math.sqrt(max(4.0, (b[2] - b[0]) * (b[3] - b[1])))
            if math.hypot(cx - bx, cy - by) <= radius_fn(span):
                return True
        return False

    # -- the streaming entry point ---------------------------------------
    def process(self, frame, t: float = None, reset: bool = False):
        t_wall0 = time.perf_counter()
        if t is None:
            t = t_wall0
        if frame is None or getattr(frame, "size", 0) == 0:
            return []
        if reset:
            self.tracker.reset()
            self.motion.reset()
            if self.memory is not None:
                self.memory.reset()
        c = self.cfg

        # 0. the working image: the whole frame, or the masked disc ----
        if self.circle:
            work, off, R = self._crop_circle(frame)
        else:
            work, off, R = frame, (0, 0), None
        H, W = work.shape[:2]
        passes = 0
        budget = self.pass_budget()
        debug = {"rois": [], "rejected": [], "sweep_windows": [],
                 "inspected": [], "memory": [],
                 "circle": (off[0] + R, off[1] + R, R) if self.circle
                 else None}

        # 1. coarse ------------------------------------------------------
        dets = self._coarse(work, passes)
        passes += 1

        # 1b. circle context passes ---------------------------------------
        # A fragment the model scores highly seeds a track immediately;
        # a weak one becomes evidence in the temporal memory, which is
        # what separates a real body from texture that briefly looked
        # like one.
        attempts = []
        if self.circle:
            for d in self._context_dets(work, R):
                if d.pop("strong"):
                    d["seed_ok"] = True
                    dets.append(d)
                else:
                    x1, y1, x2, y2 = d["box"]
                    attempts.append({"cx": (x1 + x2) * 0.5,
                                     "cy": (y1 + y2) * 0.5,
                                     "score": float(d["score"]),
                                     "est_h": max(6.0, (y2 - y1) * 0.9)})
            passes += len(c.circle_context_scales)

        # faces in circle mode are cheap (the disc is small) and double
        # as a person detector in their own right
        faces = []
        if self.face is not None and self.circle:
            try:
                faces = self.face.detect(work)
            except Exception:
                faces = []

        # 2. motion candidates -------------------------------------------
        rois = self.motion.analyze(work) if c.motion_enabled else []
        debug["rois"] = rois

        # 3. amortised sweep ---------------------------------------------
        # Detections below the sweep floor are not waste: a 0.09 that
        # repeats on the same spot every sweep cycle is a stationary
        # tiny target doing exactly what the temporal memory exists
        # for, so sub-threshold hits feed it as weak evidence.
        if c.sweep_enabled and H >= 64 and W >= 64:
            self.sweep.configure(W, H)
            for (x, y, win, zoom) in self.sweep.next_tiles():
                if passes >= budget:
                    break
                thr = c.conf_sweep if zoom <= 8 else c.conf_sweep_far
                found = self.inspector.run_window(work, x, y, win, zoom,
                                                  c.conf_floor, "sweep")
                strong = [d for d in found if d["score"] >= thr]
                debug["sweep_windows"].append((x, y, win, zoom,
                                               len(strong)))
                dets.extend(strong)
                for d in found:
                    if len(attempts) >= 4:
                        break
                    if c.conf_floor <= d["score"] < thr:
                        b = d["box"]
                        attempts.append({
                            "cx": (b[0] + b[2]) * 0.5,
                            "cy": (b[1] + b[3]) * 0.5,
                            "score": float(d["score"]),
                            "est_h": max(4.0, (b[3] - b[1]) * 0.9)})
                passes += 1

        # 4. motion-gated inspections ---------------------------------------
        # A blob next to an already-classified person is that person; a
        # blob next to a *gated* (tiny) track is the only way that track
        # ever gets refreshed, so those still get inspected.
        if rois:
            known = [d["box"] for d in dets]
            known += [tr.box for tr in self.tracker.tracks if not tr.gated]
            if self.circle and off != (0, 0):
                known += [(b[0] - off[0], b[1] - off[1],
                           b[2] - off[0], b[3] - off[1])
                          for tr in self.tracker.tracks if tr.gated
                          for b in (tr.box,)]
            queue = []
            for r in rois:
                cx, cy = r["center"]
                if self._near_any(cx, cy, known,
                                  lambda s: max(c.motion_dedup_radius,
                                                0.9 * s)):
                    continue
                queue.append(r)
            n_inspected = 0
            for r in queue:
                if passes >= budget or n_inspected >= c.inspect_budget:
                    break
                cx, cy = r["center"]
                est_h = max(4.0, float(r["height"]))
                if self.circle and est_h > c.circle_inspect_max_h:
                    # a blob this big inside the disc is a fragment of a
                    # close body: the context passes own that case, and a
                    # "zoom window" around it is the whole disc anyway
                    continue
                payload, info = self.inspector.inspect_point(work, cx, cy,
                                                             est_h)
                passes += len(info["zoom_tried"])
                n_inspected += 1
                debug["inspected"].append((cx, cy, info))
                attempts.append({"cx": cx, "cy": cy,
                                 "score": info["plausible_best"],
                                 "est_h": est_h})
                if payload is not None:
                    payload["seed_ok"] = True
                    dets.append(payload)
                else:
                    debug["rejected"].append((r["box"], info["best"]))

        # 4b. temporal evidence: promote what keeps being almost seen ----
        if self.memory is not None:
            for cand in self.memory.update(attempts, t):
                h = max(6.0, cand["est_h"])
                w = max(4.0, h * 0.45)
                cx, cy = cand["cx"], cand["cy"]
                dets.append({
                    "box": (cx - w / 2.0, cy - h / 2.0,
                            cx + w / 2.0, cy + h / 2.0),
                    "score": min(0.45, cand["acc"]),
                    "kps": None, "source": "memory", "zoom": 0.0,
                    "seed_ok": True,
                })
            debug["memory"] = self.memory.snapshot()

        # 5. fuse -----------------------------------------------------------
        merged = merge_detections(dets, iou_thr=0.35)

        # 5b. face fallback: a face is a person, even when the pose
        #     model could make nothing of the fragment around it
        if self.circle:
            self._face_fallback(merged, faces, R, W, H)

        # 6. circle bookkeeping: disc-mask the joints, decide what is
        #    truncated, then classify while still in work coordinates.
        #    Inside a disc only a body whose FEET were actually seen -
        #    the mask zeroes everything outside - counts as whole.
        for d in merged:
            if self.circle:
                self._mask_kps_disc(d.get("kps"), R)
            d["truncated"] = self._is_truncated(d, W, H)

        if not faces and self.face is not None and \
                any((d["box"][3] - d["box"][1]) >= c.min_classify_height
                    or d.get("truncated") for d in merged):
            try:
                faces = self.face.detect(work)
            except Exception:
                faces = []
        self._classify_all(work, frame, merged, faces)

        # 7. map back to screen coordinates and track --------------------------
        if self.circle and off != (0, 0):
            for d in merged:
                x1, y1, x2, y2 = d["box"]
                d["box"] = (x1 + off[0], y1 + off[1],
                            x2 + off[0], y2 + off[1])
                if d.get("kps") is not None:
                    d["kps"][:, 0] += off[0]
                    d["kps"][:, 1] += off[1]
            debug["rois"] = [dict(r, box=(r["box"][0] + off[0],
                                          r["box"][1] + off[1],
                                          r["box"][2] + off[0],
                                          r["box"][3] + off[1]),
                                  center=(r["center"][0] + off[0],
                                          r["center"][1] + off[1]))
                             for r in debug["rois"]]
            debug["rejected"] = [((b[0] + off[0], b[1] + off[1],
                                   b[2] + off[0], b[3] + off[1]), s)
                                 for b, s in debug["rejected"]]
            debug["sweep_windows"] = [(x + off[0], y + off[1], w, z, n)
                                      for (x, y, w, z, n)
                                      in debug["sweep_windows"]]
            debug["inspected"] = [(cx + off[0], cy + off[1], i)
                                  for (cx, cy, i) in debug["inspected"]]
            debug["memory"] = [(cx + off[0], cy + off[1], a, hh)
                               for (cx, cy, a, hh) in debug["memory"]]

        tracks = self.tracker.update(merged, t)
        self.tracker.consolidate()
        if self.circle and len(self.tracker.tracks) > c.circle_max_tracks:
            # the disc is an engagement area, not a crowd scene: keep the
            # strongest identities instead of every flicker that ever
            # confirmed once
            self.tracker.tracks.sort(
                key=lambda tr: (-int(tr.confirmed), -tr.hits, tr.misses,
                                tr.id))
            self.tracker.tracks = \
                sorted(self.tracker.tracks[:c.circle_max_tracks],
                       key=lambda tr: tr.id)
        self._prune_runaways(frame)
        tracks = self.tracker.tracks

        debug.update({
            "dets": merged,
            "passes": passes,
            "ms": (time.perf_counter() - t_wall0) * 1000.0,
            "sweep_progress": self.sweep.progress() if c.sweep_enabled else "-",
            "tracks": tracks,
        })
        self.last_debug = debug
        return tracks

    # -- the still-image entry point ----------------------------------------
    def process_still(self, frame, t: float = 0.0, full_sweep: bool = True):
        """Maximum-recall single frame: sweep every tile of every grid."""
        t_wall0 = time.perf_counter()
        if frame is None or getattr(frame, "size", 0) == 0:
            return []
        self.tracker.reset()
        if self.memory is not None:
            self.memory.reset()
        c = self.cfg
        if self.circle:
            work, off, R = self._crop_circle(frame)
        else:
            work, off, R = frame, (0, 0), None
        H, W = work.shape[:2]
        dets = self._coarse(work, 0)
        n_passes = 1
        if self.circle:
            dets.extend(self._context_dets(work, R))
            n_passes += len(c.circle_context_scales)
        debug = {"rois": [], "rejected": [], "sweep_windows": [],
                 "inspected": [], "memory": [], "dets": [],
                 "circle": (off[0] + R, off[1] + R, R) if self.circle
                 else None}
        if full_sweep and c.sweep_enabled:
            self.sweep.configure(W, H)
            for (x, y, win, zoom) in self.sweep.sweep_once():
                thr = c.conf_sweep if zoom <= 8 else c.conf_sweep_far
                found = self.inspector.run_window(work, x, y, win, zoom,
                                                  thr, "sweep")
                debug["sweep_windows"].append((x, y, win, zoom, len(found)))
                dets.extend(found)
                n_passes += 1
        merged = merge_detections(dets, 0.35)
        faces = []
        if self.face is not None and self.circle:
            try:
                faces = self.face.detect(work)
            except Exception:
                faces = []
        if self.circle:
            self._face_fallback(merged, faces, R, W, H)
        for d in merged:
            if self.circle:
                self._mask_kps_disc(d.get("kps"), R)
            d["truncated"] = self._is_truncated(d, W, H)
        if not faces and self.face is not None and \
                any((d["box"][3] - d["box"][1]) >= c.min_classify_height
                    or d.get("truncated") for d in merged):
            try:
                faces = self.face.detect(work)
            except Exception:
                faces = []
        self._classify_all(work, frame, merged, faces)
        if self.circle and off != (0, 0):
            for d in merged:
                x1, y1, x2, y2 = d["box"]
                d["box"] = (x1 + off[0], y1 + off[1],
                            x2 + off[0], y2 + off[1])
                if d.get("kps") is not None:
                    d["kps"][:, 0] += off[0]
                    d["kps"][:, 1] += off[1]
            debug["sweep_windows"] = [(x + off[0], y + off[1], w, z, n)
                                      for (x, y, w, z, n)
                                      in debug["sweep_windows"]]
        tracks = self.tracker.update(merged, t)
        self.tracker.consolidate()
        self._prune_runaways(frame)
        tracks = self.tracker.tracks
        debug.update({"dets": merged, "passes": n_passes,
                      "ms": (time.perf_counter() - t_wall0) * 1000.0,
                      "sweep_progress": "full", "tracks": tracks})
        self.last_debug = debug
        return tracks
