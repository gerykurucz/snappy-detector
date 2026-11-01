
import math
import os
import threading
import time

try:
    from system_core import log
except Exception:
    def log(_text):
        pass

DEFAULT_MODEL = "yolo11n-pose.onnx"

DETECTOR_FLOOR = 0.10

def _conf_of(cfgv) -> float:
    try:
        pct = float(cfgv.get("hpose_min_conf", 35) or 35)
    except (TypeError, ValueError):
        pct = 35.0
    return max(0.05, min(0.95, pct / 100.0))

CROP_RESCUE_SCORE = 0.05
CROP_RESCUE_KP = 0.80

ROI_RADIUS_MAX = 150.0
ROI_MARGIN = 1.5
ROI_MIN_SIDE = 160
ROI_RESCUE_SCORE = 0.15
ROI_RESCUE_KP = 0.50

MAX_EXPLAIN_PER_FRAME = int(os.environ.get("HPOSE_MAX_EXPLAIN", "3"))

# A 20 px person typically produces YOLO scores in 0.10-0.30.
# Rejecting at 0.35 guarantees zero recall for the small-target case.
# The Tracker's hit-count confirmation rejects false positives for us.
PREFILTER_SCORE_FLOOR = float(os.environ.get("HPOSE_PREFILTER_FLOOR", "0.12"))

def _prefilter(poses):
    out = []
    for pose in poses:
        score = float(pose.score)
        if score < PREFILTER_SCORE_FLOOR:
            continue
        kp = _kp_conf(pose)
        h = max(1.0, pose.box[3] - pose.box[1])
        rank = (score + 0.3 * kp) * _size_rank(h)
        out.append((pose, rank, kp, score))
    out.sort(key=lambda item: -item[1])
    return out

def _size_rank(h: float) -> float:
    """Return a multiplicative priority in [0.8, 1.4] favouring smaller targets."""
    h = max(1.0, float(h))
    return max(0.8, min(1.4, 1.0 + (100.0 - h) / 500.0))

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
                 "raw_score", "hits", "head_kp", "kps")

    def __init__(self, box, view="", stance="", motion="static",
                 speed=0.0, score=0.0, view_confidence=0.0,
                 stance_confidence=0.0, lead=(0.0, 0.0), track_id=0,
                 kp_conf=1.0, raw_score=1.0, head_kp=None):
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
        self.head_kp = head_kp
        self.kps = None

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
        self.analyzer = Analyzer(bundle_path=bundle, pose_model=model,
                                 conf=DETECTOR_FLOOR,
                                 use_face=bool(cfgv.get("hpose_use_face", True)),
                                 min_face_person_px=float(cfgv.get(
                                     "hpose_min_face_person_px", 100.0)))
        self.tracker = Tracker()
        self.last_error = ""
        self._box_stats_cache = {}
        self._small_detector = None
        self._small_model = str(cfgv.get("hpose_small_model")
                                or "yolo11n-pose-320.onnx")
        self._stats = {"frames": 0, "hits": 0, "misses": 0,
                       "ms_total": 0.0, "ms_hit": 0.0, "ms_miss": 0.0}
        self._stop_small_thread = False
        # Warn up front if the user requested view/stance filters but the
        # shipped bundle cannot classify (feature_version mismatch). Fail-open
        # in matches_view/matches_stance means the filter silently does
        # nothing - better to tell the user at startup and force "any".
        _view_req = str(cfgv.get("hpose_view", "any")).lower()
        _stance_req = str(cfgv.get("hpose_stance", "any")).lower()
        if _view_req not in ("", "any") or _stance_req not in ("", "any"):
            try:
                bundle = self.analyzer.bundle
                if not getattr(bundle, "has_view", lambda: False)():
                    log("hpose: view filter requested (%r) but the shipped "
                        "bundle cannot classify view. Forcing hpose_view=any. "
                        "Retrain the bundle to restore view filtering."
                        % (_view_req,))
                    self.cfgv["hpose_view"] = "any"
                if not getattr(bundle, "has_stance", lambda: False)():
                    log("hpose: stance filter requested (%r) but the "
                        "shipped bundle cannot classify stance. Forcing "
                        "hpose_stance=any. Retrain the bundle to restore "
                        "stance filtering." % (_stance_req,))
                    self.cfgv["hpose_stance"] = "any"
            except Exception:
                pass
        # Kick off the small-model export/build in the background so the
        # first scan does not stall on a 30-60 s Ultralytics export.
        threading.Thread(target=self._prepare_small_detector,
                          name="hpose-small-model", daemon=True).start()

    def _tta_flag(self, img, n_poses, max_score):
        cfgv = getattr(self, "cfgv", None) or {}
        try:
            tta = str(cfgv.get("hpose_tta", "auto") or "auto").strip().lower()
        except Exception:
            tta = "auto"
        if tta == "off":
            return False
        if tta == "on":
            return True
        return n_poses == 0 or max_score < 0.20

    def _get_box_stats(self, img, lb, track_id, cfgv=None):
        """Cached version of _hpose_box_stats keyed by (track_id, img.shape, box).
        Only recomputes when the subject's box or image changes."""
        import vision as _vision
        key = (int(track_id), tuple(img.shape),
               tuple(round(float(v), 1) for v in lb))
        cached = self._box_stats_cache.get(key)
        if cached is not None:
            return cached
        cfgv = cfgv or getattr(self, "cfgv", {}) or {}
        _want_sig = bool(cfgv.get("follow_enabled")) or \
                    bool(cfgv.get("hold_follow", True))
        if not _want_sig:
            area = float((lb[2] - lb[0]) * (lb[3] - lb[1]))
            result = (area, 0.0, None)
        else:
            result = _vision._hpose_box_stats(img, lb)
        # Bound the cache to avoid unbounded growth
        if len(self._box_stats_cache) >= 64:
            self._box_stats_cache.clear()
        self._box_stats_cache[key] = result
        return result

    def _get_small_detector(self):
        # Non-blocking: returns None until the background thread has
        # finished. First scan uses the big model for ROI; subsequent
        # scans get the 3x ROI win.
        return self._small_detector

    def _prepare_small_detector(self):
        try:
            from hpose_detector import PoseDetector, cache_dir, \
                _export_small_pose
            import os
            path = os.path.join(cache_dir(), self._small_model)
            if not (os.path.isfile(path) and os.path.getsize(path) > 1024):
                if self._stop_small_thread:
                    return
                if not _export_small_pose(path):
                    log("hpose: 320 ROI model unavailable; ROI runs at "
                        "the 640 model speed (~50 ms). Install "
                        "'ultralytics' or drop %s into %s."
                        % (self._small_model, cache_dir()))
                    return
            if self._stop_small_thread:
                return
            self._small_detector = PoseDetector(
                model=self._small_model, imgsz=320, conf=0.05,
                download=False)
            log("hpose: 320 ROI model ready")
        except Exception as exc:
            log("hpose: 320 ROI model failed (%r); ROI runs at 640 speed"
                % (exc,))

    def _pick_roi_detector(self, expected_h: float, roi_side: float):
        """Small model is fine if the target ends up >= 32 px in the
        320-tensor after letterbox. Below that, use the big model (which
        gets twice the tensor resolution for the same upscale).

        For expected_h = 24 and roi_side = 104: target_in_small =
        24 * 320/104 = 73.8 px → small model, ~15 ms.
        For expected_h = 24 and roi_side = 192: target_in_small =
        24 * 320/192 = 40 px → small model but marginal.
        For expected_h = 24 and roi_side = 640: target_in_small =
        24 * 320/640 = 12 px → big model, ~50 ms.
        """
        if expected_h is None or roi_side <= 0:
            return self._get_small_detector() or self.analyzer.pose_detector
        target_in_small = expected_h * (320.0 / roi_side)
        if target_in_small >= 32.0:
            return self._get_small_detector() or self.analyzer.pose_detector
        return self.analyzer.pose_detector

    @staticmethod
    def _expected_h_from_crop(cs):
        # A crop sized `cs` was chosen to hold a person of roughly
        # `cs / 8` px (the 8x upscale heuristic from _roi_pass).
        return max(1.0, cs / 8.0)

    def _record_stats(self, hit: bool, t_start: float) -> None:
        """Record per-frame hit/miss and latency for instrumentation."""
        elapsed_ms = (time.perf_counter() - t_start) * 1000.0
        self._stats["frames"] += 1
        if hit:
            self._stats["hits"] += 1
            self._stats["ms_hit"] += elapsed_ms
        else:
            self._stats["misses"] += 1
            self._stats["ms_miss"] += elapsed_ms
        self._stats["ms_total"] += elapsed_ms

    def _finish_scan(self, dets, t):
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
            sub.head_kp = getattr(tr, "head_kp", None)
            sub.kps = getattr(tr, "kps", None)
            sub.facing_camera = tr.view in ("front", "front_left_45",
                                            "front_right_45")
            sub.back_turned = tr.view in ("back", "back_left_45",
                                          "back_right_45")
            out.append(sub)
        return out

    def scan(self, img, origin=(0, 0), t=None, deadline=None,
             zoom_deadline=None, aim=None, aim_radius=0.0, tile_hints=None):
        if img is None or getattr(img, "size", 0) == 0:
            return []
        if t is None:
            t = time.perf_counter()
        ox, oy = int(origin[0]), int(origin[1])
        analyzer = self.analyzer
        focal = max(1.0, img.shape[1] * 0.9)
        min_side = min(img.shape[:2])
        radius = float(aim_radius or 0.0)
        cfgv = self.cfgv or {}
        zdl = zoom_deadline if zoom_deadline is not None else deadline
        now = time.perf_counter()

        # Aim-first: when the search region is small enough that a single
        # ROI crop covers it, skip the full-frame detect entirely. The
        # ONNX model input tensor is fixed at 640x640 regardless of crop
        # size, so the savings is running ONE inference instead of TWO,
        # not an 8x per-inference speedup.
        ROI_FIRST_MAX_RADIUS = float(
            cfgv.get("hpose_roi_first_max_radius", 150.0))
        if aim is not None and 4.0 < radius <= ROI_FIRST_MAX_RADIUS \
                and min_side >= 60 \
                and (deadline is None or now < deadline):
            roi = self._roi_pass(analyzer, img, ox, oy, aim, radius,
                                 zdl, expected_h=radius * 0.6)
            if roi:
                self._record_stats(True, t)
                return self._finish_scan(roi, t)
            # ROI missed. Try blob_watch as a fallback for small targets
            # that YOLO missed (e.g. synthetic silhouettes, low-contrast
            # figures). Runs before the crop_covers_search early return so
            # production-sized crops also get the blob fallback.
            if bool(cfgv.get("blob_watch", True)) \
                    and (deadline is None or time.perf_counter() < deadline):
                try:
                    import vision as _vision
                    aim_x = ox + aim[0] if aim else img.shape[1] // 2
                    aim_y = oy + aim[1] if aim else img.shape[0] // 2
                    _bt = _vision._blob_watch(img, aim_x, aim_y,
                                              max(radius, 40.0),
                                              deadline=zdl)
                except Exception:
                    _bt = None
                if _bt is not None:
                    cx_abs = aim_x + _bt.dx
                    cy_abs = aim_y + _bt.dy
                    # blob_watch already validated shape/contrast/aspect,
                    # so trust it with a score that passes the tracker's
                    # low_score filter (default 0.35).
                    blob_score = min(1.0, float(_bt.contrast) / 40.0)
                    dets = [{
                        "box": (cx_abs - _bt.bw / 2.0,
                                cy_abs - _bt.bh / 2.0,
                                cx_abs + _bt.bw / 2.0,
                                cy_abs + _bt.bh / 2.0),
                        "score": blob_score,
                        "raw_score": blob_score,
                        "kp_conf": 0.0,
                        "head_kp": None,
                        "view_probs": {},
                        "stance_probs": {},
                    }]
                    self._record_stats(True, t)
                    return self._finish_scan(dets, t)
            # ROI missed. Fall through ONLY when the crop is substantially
            # larger than the search region — that is the only case where
            # the full-frame pass can find anything the ROI didn't already
            # see. assist.py always passes a crop sized to radius+12, so
            # this branch is dead in production.
            crop_covers_search = (min_side <= int(2.0 * (radius + 30.0)))
            if crop_covers_search:
                # We searched the entire region. Nothing here.
                self._record_stats(False, t)
                return []
            # Else: caller passed a much larger image; fall through.

        # Full-frame path: detect on the whole image, then optionally
        # supplement with ROI / crop / tile-hint passes.
        want_face = min_side >= 400 and (
            aim is None or radius >= 100.0)
        faces = analyzer._face_boxes(img) if want_face else []

        poses = analyzer.pose_detector.detect(img)
        max_score = max((p.score for p in poses), default=0.0)
        if self._tta_flag(img, len(poses), max_score):
            poses = analyzer.pose_detector.detect(img, tta=True)
        full = [self._explain_det(analyzer, pose, score, kp, raw_score,
                                  focal, faces, img, ox, oy)
                for pose, score, kp, raw_score in _prefilter(poses)]
        dets = full[:MAX_EXPLAIN_PER_FRAME]
        side_cap = min(img.shape[:2])

        if not full:
            if side_cap >= 120 and (zdl is None or now < zdl):
                # Small-target pass: upscale a tile sized to the expected person.
                expected = max(24.0, float(aim_radius or 40.0) * 0.6)
                tile_cs = int(min(side_cap - 4,
                                  max(48, int(8.0 * expected))))
                extra = self._crop_pass(analyzer, img, ox, oy, tile_cs,
                                        deadline=zdl)
                dets = dets + extra

        # Tile-hint pass: upscale classical candidate boxes and run pose detection
        if tile_hints and zdl is not None and now < zdl:
            extra = self._tile_hint_pass(analyzer, img, ox, oy, tile_hints, zdl)
            dets = dets + extra

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
            sub.head_kp = getattr(tr, "head_kp", None)
            sub.kps = getattr(tr, "kps", None)
            sub.facing_camera = tr.view in ("front", "front_left_45",
                                            "front_right_45")
            sub.back_turned = tr.view in ("back", "back_left_45",
                                          "back_right_45")
            out.append(sub)
        self._record_stats(bool(dets), t)
        if dets:
            return self._finish_scan(dets, t)
        # Blob-watch fallback: when hpose finds nothing, try the blob
        # watcher as a last resort for small high-contrast people.
        # Route through a det dict so the tracker sees it (track
        # continuity, Kalman, motion history) rather than a raw Subject.
        if aim is not None and 4.0 < radius <= ROI_FIRST_MAX_RADIUS \
                and (deadline is None or time.perf_counter() < deadline):
            try:
                import vision as _vision
                aim_x = ox + aim[0] if aim else img.shape[1] // 2
                aim_y = oy + aim[1] if aim else img.shape[0] // 2
                _bt = _vision._blob_watch(img, aim_x, aim_y,
                                          max(radius, 40.0), deadline=zdl)
            except Exception:
                _bt = None
            if _bt is not None:
                cx_abs = aim_x + _bt.dx
                cy_abs = aim_y + _bt.dy
                # blob_watch already validated shape/contrast/aspect, so
                # trust it with a high score that passes the tracker's
                # low_score filter (default 0.35).
                blob_score = min(1.0, float(_bt.contrast) / 40.0)
                dets.append({
                    "box": (cx_abs - _bt.bw / 2.0, cy_abs - _bt.bh / 2.0,
                            cx_abs + _bt.bw / 2.0, cy_abs + _bt.bh / 2.0),
                    "score": blob_score,
                    "raw_score": blob_score,
                    "kp_conf": 0.0,
                    "head_kp": None,
                    "view_probs": {},
                    "stance_probs": {},
                })
                self._record_stats(True, t)
        return self._finish_scan(dets, t)

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
        kps_screen = None
        try:
            kps_screen = [(float(k[0]) + ox, float(k[1]) + oy, float(k[2]))
                          for k in pose.kps[:17]]
        except Exception:
            kps_screen = None
        return {
            "box": (box[0] + ox, box[1] + oy, box[2] + ox, box[3] + oy),
            "score": score,
            "raw_score": raw,
            "kp_conf": kp,
            "head_kp": head,
            "kps": kps_screen,
            "view_probs": det.get("view_probs") or {},
            "stance_probs": det.get("stance_probs") or {},
        }

    def _crop_pass(self, analyzer, img, ox, oy, cs, deadline=None):
        h, w = img.shape[:2]
        cx0, cy0 = (w - cs) // 2, (h - cs) // 2
        crop = img[cy0:cy0 + cs, cx0:cx0 + cs]
        focal = max(1.0, cs * 0.9)
        det = self._pick_roi_detector(self._expected_h_from_crop(cs), float(cs))
        raw = _prefilter(det.detect(crop))
        raw = _suppress_contained(raw)[:MAX_EXPLAIN_PER_FRAME]
        out = []
        for pose, score, kp, raw_score in raw:
            if deadline is not None and time.perf_counter() > deadline:
                break
            out.append(self._explain_det(analyzer, pose, score, kp,
                                         raw_score, focal, [], crop,
                                         cx0 + ox, cy0 + oy))
        return out

    def _tile_hint_pass(self, analyzer, img, ox, oy, tile_hints, deadline=None):
        """Upscale classical candidate boxes, run the pose detector on each
        tile, and map detections back to image coordinates."""
        h, w = img.shape[:2]
        out = []
        for bx0, by0, bx1, by1 in tile_hints:
            if deadline is not None and time.perf_counter() > deadline:
                break
            bh = max(1.0, float(by1 - by0))
            # Upscale tile so the expected target fills ~256-640 px input
            up_scale = max(1.0, min(12.0, 256.0 / bh))
            tw = max(64, int((bx1 - bx0) * up_scale))
            th = max(64, int(bh * up_scale))
            tx0 = int(max(0, bx0 - (tw - (bx1 - bx0)) // 2))
            ty0 = int(max(0, by0 - (th - bh) // 2))
            tx1 = min(w, tx0 + tw)
            ty1 = min(h, ty0 + th)
            tx0 = max(0, tx1 - tw)
            ty0 = max(0, ty1 - th)
            crop = img[ty0:ty1, tx0:tx1]
            if min(crop.shape[:2]) < 40:
                continue
            if deadline is not None and time.perf_counter() >= deadline:
                break
            focal = max(1.0, max(crop.shape[:2]) * 0.9)
            det = self._pick_roi_detector(max(1.0, bh), float(bh))
            raw = _prefilter(det.detect(crop))
            chosen = raw
            if len(raw) >= 2:
                chosen = _suppress_contained(raw)[:MAX_EXPLAIN_PER_FRAME]
            for pose, score, kp, raw_score in chosen:
                if deadline is not None and time.perf_counter() > deadline:
                    break
                # Map tile offset into screen-space ox/oy for _explain_det
                out.append(self._explain_det(analyzer, pose, score, kp,
                                             raw_score, focal, [], crop,
                                             ox + tx0, oy + ty0))
        return out

    def _roi_pass(self, analyzer, img, ox, oy, aim, radius, deadline=None,
                  expected_h: float = None):
        h, w = img.shape[:2]
        acx, acy = int(aim[0]), int(aim[1])
        if not (0 <= acx < w and 0 <= acy < h):
            return []
        # Side scaled by expected target height so 20 px targets don't get
        # 160 px tiles (which are 90 % background) and 160 px targets don't
        # get cropped below their body. Size the ROI so the upscaled target
        # is ~64 px in the 320 tensor (YOLO's reliable threshold).
        if expected_h:
            roi_side = min(5.0 * expected_h, 2.0 * radius + expected_h)
            side = int(min(float(min(h, w) - 4), roi_side))
        else:
            side = int(max(ROI_MIN_SIDE, ROI_MARGIN * float(radius)))
            side = min(side, min(h, w) - 4)
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
        det = self._pick_roi_detector(expected_h, float(side))
        raw = _prefilter(det.detect(crop))
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
        if _LOADING or _TRIED:
            return None
        _LOADING = True
        _TRIED = True

    def worker():
        global _ENGINE, _LOADING
        try:
            eng = Engine(cfgv)
            with _LOCK:
                _ENGINE = eng
            log("hpose: ready (%s on %s)"
                % (getattr(eng.analyzer.pose_detector, "model_name", "?"),
                   getattr(eng.analyzer.pose_detector,
                           "active_provider", "CPUExecutionProvider")))
        except Exception as exc:
            log("hpose: could not start (%r) - the classical scanner "
                "handles everything." % (exc,))
        finally:
            with _LOCK:
                _LOADING = False

    threading.Thread(target=worker, name="hpose-load", daemon=True).start()
    return None

def engine(cfgv=None, force: bool = False):
    global _TRIED, _ENGINE, _LOADING
    if force:
        with _LOCK:
            old = _ENGINE
            _ENGINE = None
            _LOADING = False
            _TRIED = False
        if old is not None:
            old._stop_small_thread = True
    with _LOCK:
        if _ENGINE is not None:
            return _ENGINE
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


def stats() -> dict:
    """Return per-frame hit/miss and latency instrumentation, or None."""
    with _LOCK:
        if _ENGINE is None or not hasattr(_ENGINE, "_stats"):
            return None
        s = _ENGINE._stats
        n = max(1, s["frames"])
        hits = s["hits"]
        miss = s["misses"]
        return {
            "frames": s["frames"],
            "hit_rate": hits / n,
            "avg_ms": s["ms_total"] / n,
            "avg_hit_ms": (s["ms_hit"] / hits) if hits else 0.0,
            "avg_miss_ms": (s["ms_miss"] / miss) if miss else 0.0,
        }

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
    if view == "unknown" or not view:
        return True                     # fail-open, not fail-closed
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
    if stance == "unknown" or not stance:
        return True                     # fail-open, not fail-closed
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
        h = max(1.0, sub.box[3] - sub.box[1])
        size_scale = max(0.4, min(1.0, h / 120.0))
        raw_floor = 0.10 + 0.10 * size_scale
        kp_floor = 0.08 + 0.17 * size_scale
        score_floor = max(0.10, min_conf * size_scale)
        hits_needed = 1 if size_scale <= 0.5 else 2
        if getattr(sub, "raw_score", 1.0) < raw_floor and \
                getattr(sub, "hits", 2) < hits_needed:
            continue
        kp = getattr(sub, "kp_conf", 1.0)
        if kp < kp_floor and sub.score < score_floor * 1.4:
            continue
        if sub.score < score_floor:
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
