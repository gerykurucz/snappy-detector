
from __future__ import annotations


from hpose_keypoints import box_iou
from hpose_motion import KalmanBox, MotionModel
from hpose_model3d import VIEW_CLASSES
from hpose_stance import STANCE_CLASSES


class Track(object):

    __slots__ = ("id", "box", "score", "hits", "misses", "age", "kf",
                 "motion", "view_probs", "stance_probs", "view", "stance",
                 "view_history", "stance_history", "last_t", "fit",
                 "confirmed", "has_face", "kp_conf", "raw_score", "head_kp")

    def __init__(self, tid: int, box, score: float, t: float):
        self.id = int(tid)
        self.box = tuple(float(v) for v in box)
        self.score = float(score)
        self.kp_conf = 1.0
        self.raw_score = float(score)
        self.head_kp = None
        self.hits = 0
        self.misses = 0
        self.age = 0
        self.kf = KalmanBox(box, t)
        self.motion = MotionModel()
        cx = (box[0] + box[2]) * 0.5
        cy = (box[1] + box[3]) * 0.5
        self.motion.add(t, cx, cy)
        self.view_probs = {c: 0.0 for c in VIEW_CLASSES}
        self.stance_probs = {c: 0.0 for c in STANCE_CLASSES}
        self.view = ""
        self.stance = ""
        self.view_history = []
        self.stance_history = []
        self.last_t = float(t)
        self.fit = None
        self.confirmed = False
        self.has_face = False

    @property
    def center(self):
        return ((self.box[0] + self.box[2]) * 0.5,
                (self.box[1] + self.box[3]) * 0.5)

    @property
    def velocity(self):
        return self.motion.vel

    @property
    def velocity_raw(self):
        return self.motion.vel_raw

    @property
    def speed(self) -> float:
        return self.motion.speed

    @property
    def motion_state(self) -> str:
        return self.motion.state()

    def lead(self, seconds: float):
        if self.hits >= 3 and self.motion.samples:
            t = max(0.0, float(seconds))
            vx, vy = self.motion.vel_raw
            ox = vx * t
            oy = vy * t
            x = self.motion.samples[-1][1] + ox
            y = self.motion.samples[-1][2] + oy
            return x, y, ox, oy
        return self.motion.predict(seconds)

    def as_dict(self) -> dict:
        vx, vy = self.motion.vel
        return {
            "id": self.id,
            "box": [round(v, 1) for v in self.box],
            "score": round(self.score, 4),
            "center": [round(self.center[0], 1), round(self.center[1], 1)],
            "velocity": [round(vx, 1), round(vy, 1)],
            "speed": round(self.motion.speed, 1),
            "motion": self.motion_state,
            "view": self.view,
            "stance": self.stance,
            "view_confidence": round(self.view_probs.get(self.view, 0.0), 4),
            "stance_confidence": round(self.stance_probs.get(self.stance, 0.0),
                                       4),
            "hits": self.hits,
            "age": self.age,
            "confirmed": self.confirmed,
        }


def _greedy_match(tracks, dets, iou_threshold: float):
    pairs = []
    for ti, tr in enumerate(tracks):
        for di, det in enumerate(dets):
            iou = box_iou(tr.box, det["box"])
            if iou >= iou_threshold:
                pairs.append((iou, ti, di))
    pairs.sort(reverse=True)
    used_t = set()
    used_d = set()
    matches = []
    for iou, ti, di in pairs:
        if ti in used_t or di in used_d:
            continue
        used_t.add(ti)
        used_d.add(di)
        matches.append((ti, di))
    return matches, used_d


class Tracker(object):

    def __init__(self, max_age: int = 20, min_hits: int = 2,
                 iou_threshold: float = 0.30, low_iou_threshold: float = 0.15,
                 low_score: float = 0.35, view_smoothing: float = 0.35,
                 stance_smoothing: float = 0.25, switch_margin: float = 0.12):
        self.max_age = int(max_age)
        self.min_hits = int(min_hits)
        self.iou_threshold = float(iou_threshold)
        self.low_iou_threshold = float(low_iou_threshold)
        self.low_score = float(low_score)
        self.view_smoothing = float(view_smoothing)
        self.stance_smoothing = float(stance_smoothing)
        self.switch_margin = float(switch_margin)
        self.tracks = []
        self._next_id = 1

    def _smooth(self, store: dict, incoming: dict, alpha: float,
                current: str, margin: float) -> str:
        if not incoming:
            return current
        total = sum(store.values())
        if total <= 1e-9:
            for key in store:
                store[key] = float(incoming.get(key, 0.0))
            best = max(store, key=lambda k: store[k])
            return best
        for key, value in store.items():
            store[key] = (1.0 - alpha) * value + alpha * float(
                incoming.get(key, 0.0))
        if not current:
            best = max(store, key=lambda k: store[k])
            return best
        best = max(store, key=lambda k: store[k])
        if best != current and store[best] > store[current] + margin:
            return best
        return current

    def update(self, detections, t: float) -> list:
        dets = list(detections or [])
        for tr in self.tracks:
            tr.kf.predict(t)

        high = [d for d in dets if d.get("score", 1.0) >= self.low_score]
        low = [d for d in dets if d.get("score", 1.0) < self.low_score]

        matches, used = _greedy_match(self.tracks, high, self.iou_threshold)
        matched_det = set(di for _, di in matches)

        rest = [i for i in range(len(high)) if i not in matched_det]
        if rest and self.tracks:
            unmatched = [i for i, tr in enumerate(self.tracks)
                         if i not in {ti for ti, _ in matches}]
            sub_tracks = [self.tracks[i] for i in unmatched]
            sub_dets = [high[i] for i in rest]
            sub_matches, sub_used = _greedy_match(sub_tracks, sub_dets,
                                                  self.low_iou_threshold)
            for sti, sdi in sub_matches:
                ti = unmatched[sti]
                di = rest[sdi]
                matches.append((ti, di))
                used.add(di)

        for ti, di in matches:
            det = high[di]
            self._apply(self.tracks[ti], det, t)

        for di, det in enumerate(high):
            if di in used:
                continue
            track = Track(self._next_id, det["box"], det.get("score", 1.0), t)
            self._next_id += 1
            self._apply(track, det, t)
            self.tracks.append(track)

        alive = []
        for tr in self.tracks:
            if tr.misses > self.max_age:
                continue
            alive.append(tr)
        self.tracks = alive
        return self.tracks

    def _apply(self, track: Track, det: dict, t: float) -> None:
        track.hits += 1
        track.misses = 0
        track.age += 1
        track.box = tuple(float(v) for v in det["box"])
        track.score = float(det.get("score", track.score))
        if "kp_conf" in det:
            track.kp_conf = float(det["kp_conf"])
        if "raw_score" in det:
            track.raw_score = float(det["raw_score"])
        hk = det.get("head_kp")
        if hk:
            track.head_kp = hk
        track.last_t = float(t)
        track.kf.update(track.box, t)
        cx = (track.box[0] + track.box[2]) * 0.5
        cy = (track.box[1] + track.box[3]) * 0.5
        track.motion.add(t, cx, cy)
        track.fit = det.get("fit", track.fit)
        if det.get("has_face"):
            track.has_face = True
        track.view = self._smooth(track.view_probs, det.get("view_probs"),
                                  self.view_smoothing, track.view,
                                  self.switch_margin)
        track.stance = self._smooth(track.stance_probs,
                                    det.get("stance_probs"),
                                    self.stance_smoothing, track.stance,
                                    self.switch_margin)
        if track.stance == "standing" and track.motion_state == "fast":
            track.stance = "walk"
        elif track.stance == "walk" and track.motion_state == "static" \
                and track.hits > 8:
            track.stance = "standing"
        track.view_history.append(track.view)
        track.stance_history.append(track.stance)
        if len(track.view_history) > 60:
            del track.view_history[0]
        if len(track.stance_history) > 60:
            del track.stance_history[0]
        if not track.confirmed and track.hits >= self.min_hits:
            track.confirmed = True

    def best(self, score_fn=None):
        if not self.tracks:
            return None
        if score_fn is not None:
            return max(self.tracks, key=score_fn)
        return max(self.tracks, key=lambda tr: tr.box[2] - tr.box[0])

    def by_id(self, tid: int):
        for tr in self.tracks:
            if tr.id == tid:
                return tr
        return None

    def reset(self) -> None:
        self.tracks = []
        self._next_id = 1
