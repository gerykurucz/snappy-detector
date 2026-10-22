"""Tracking people across frames.

A detector answers "where is everybody in this picture".  A tracker
answers "where is the person I was following, and how have they
changed".  For a target that moves, that difference matters: the
detector's box jitters by a few pixels every frame, and its class
answers flicker between two labels when a body sits on the boundary
between them.

:class:`Tracker` keeps a Kalman filter on every box, a motion history
on every centre point and a running average on every class
probability, with a margin that stops a label changing on a single
uncertain frame.

Association is two-stage, the same idea ByteTrack uses: confident
detections are matched first, and the detections the detector was less
sure about are then given a chance to keep a track alive.  Without the
second stage a target that is briefly blurred loses its identity, its
velocity and its history.
"""

from __future__ import annotations

import math

import numpy as np

from .keypoints import box_iou
from .motion import KalmanBox, MotionModel
from .model3d import VIEW_CLASSES
from .stance import STANCE_CLASSES


class Track(object):
    """One tracked person."""

    __slots__ = ("id", "box", "score", "hits", "misses", "age", "kf",
                 "motion", "view_probs", "stance_probs", "view", "stance",
                 "view_history", "stance_history", "last_t", "fit",
                 "confirmed", "has_face", "source", "gated", "truncated",
                 "prev_cx", "prev_cy", "prev_ut")

    def __init__(self, tid: int, box, score: float, t: float):
        self.id = int(tid)
        self.box = tuple(float(v) for v in box)
        self.score = float(score)
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
        self.source = ""
        self.gated = False
        self.truncated = False
        self.prev_cx = None
        self.prev_cy = None
        self.prev_ut = None

    # -- geometry ------------------------------------------------------
    @property
    def center(self):
        return ((self.box[0] + self.box[2]) * 0.5,
                (self.box[1] + self.box[3]) * 0.5)

    @property
    def velocity(self):
        return self.motion.vel

    @property
    def speed(self) -> float:
        return self.motion.speed

    @property
    def motion_state(self) -> str:
        return self.motion.state()

    def lead(self, seconds: float):
        """Where this target will be after ``seconds``."""
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
            "source": self.source,
            "size_gated": self.gated,
            "truncated": self.truncated,
        }


def _gate_score(tr_box, det_box, slack: float = 0.0) -> float:
    """Association score that survives tiny boxes.

    IoU is brutal on small boxes: a one-pixel jitter halves it.  For a
    target a handful of pixels across, the centre distance says more
    than the overlap does, so the gate is the better of the two - IoU,
    or closeness of the centres relative to the target's own size.
    ``slack`` widens the distance gate for tracks that have been
    coasting: a tiny target is re-found every few frames, and by then
    it has moved further than a fresh track's gate would allow.
    """
    iou = box_iou(tr_box, det_box)
    tw = max(1.0, tr_box[2] - tr_box[0])
    th_ = max(1.0, tr_box[3] - tr_box[1])
    span = math.sqrt(tw * th_)
    dcx = (det_box[0] + det_box[2]) * 0.5 - (tr_box[0] + tr_box[2]) * 0.5
    dcy = (det_box[1] + det_box[3]) * 0.5 - (tr_box[1] + tr_box[3]) * 0.5
    dist = math.hypot(dcx, dcy)
    gate = max(10.0, 0.8 * span) + slack
    dist_score = max(0.0, 1.0 - dist / gate) if dist < gate else 0.0
    return max(iou, dist_score)


def _greedy_match(tracks, dets, iou_threshold: float):
    """Match tracks to detections by gate score, best pairs first."""
    pairs = []
    for ti, tr in enumerate(tracks):
        span = math.sqrt(max(4.0, (tr.box[2] - tr.box[0])
                             * (tr.box[3] - tr.box[1])))
        slack = min(36.0, tr.misses * max(3.0, 0.6 * span))
        for di, det in enumerate(dets):
            score = _gate_score(tr.box, det["box"], slack)
            if score >= iou_threshold:
                pairs.append((score, ti, di))
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
    """Multi-object tracker with smoothed classifications."""

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

    # -- helpers -------------------------------------------------------
    def _smooth(self, store: dict, incoming: dict, alpha: float,
                current: str, margin: float) -> str:
        """Blend class probabilities and pick a label with hysteresis."""
        if not incoming:
            return current
        total = sum(store.values())
        if total <= 1e-9:
            # first sighting: take the detector's answer as it is, so
            # that a single frame already carries a real confidence
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

    # -- main ----------------------------------------------------------
    def update(self, detections, t: float) -> list:
        """Feed one frame of detections.  Returns the live tracks."""
        dets = list(detections or [])
        for tr in self.tracks:
            tr.kf.predict(t)

        high = [d for d in dets if d.get("score", 1.0) >= self.low_score]
        low = [d for d in dets if d.get("score", 1.0) < self.low_score]

        pairs = []                 # (track index, detection)
        matched_tracks = set()
        used_high = set()

        # stage 1: confident detections against every track, strict gate
        m1, used1 = _greedy_match(self.tracks, high, self.iou_threshold)
        used_high |= used1
        for ti, di in m1:
            pairs.append((ti, high[di]))
            matched_tracks.add(ti)

        # stage 2: confident detections the strict gate missed get a
        # looser gate against the tracks that are still free
        free = [i for i in range(len(self.tracks)) if i not in matched_tracks]
        rest = [(i, high[i]) for i in range(len(high)) if i not in used1]
        if rest and free:
            m2, _ = _greedy_match([self.tracks[i] for i in free],
                                  [d for _, d in rest],
                                  self.low_iou_threshold)
            for si, di in m2:
                oi, det = rest[di]
                pairs.append((free[si], det))
                matched_tracks.add(free[si])
                used_high.add(oi)

        # stage 3: weak detections may *sustain* an existing track but
        # never start one - unless they carry ``seed_ok``: a detection
        # born from a motion-gated inspection or the temporal memory
        # has strong prior evidence behind it, and tiny targets simply
        # do not score above the "high" bar.
        free = [i for i in range(len(self.tracks)) if i not in matched_tracks]
        used_low = set()
        if low and free:
            m3, used3 = _greedy_match([self.tracks[i] for i in free], low,
                                      self.low_iou_threshold)
            used_low |= used3
            for si, di in m3:
                pairs.append((free[si], low[di]))
                matched_tracks.add(free[si])

        for ti, det in pairs:
            self._apply(self.tracks[ti], det, t)

        for di, det in enumerate(high):
            if di in used_high:
                continue
            track = Track(self._next_id, det["box"], det.get("score", 1.0), t)
            self._next_id += 1
            self._apply(track, det, t)
            self.tracks.append(track)
            matched_tracks.add(len(self.tracks) - 1)

        for di, det in enumerate(low):
            if di in used_low or not det.get("seed_ok"):
                continue
            track = Track(self._next_id, det["box"], det.get("score", 1.0), t)
            self._next_id += 1
            self._apply(track, det, t)
            self.tracks.append(track)
            matched_tracks.add(len(self.tracks) - 1)

        # count a miss on every track that was not seen, coast its box
        # on the Kalman filter, and retire the ones that stay unseen.
        # Gated (tiny) tracks coast twice as long: their re-finds are
        # intermittent by nature.
        alive = []
        for i, tr in enumerate(self.tracks):
            if i not in matched_tracks:
                tr.misses += 1
                tr.age += 1
                tr.box = tr.kf.box()
            limit = self.max_age * (2 if tr.gated else 1)
            if tr.misses <= limit:
                alive.append(tr)
        self.tracks = alive
        return self.tracks

    def _apply(self, track: Track, det: dict, t: float) -> None:
        cx = (det["box"][0] + det["box"][2]) * 0.5
        cy = (det["box"][1] + det["box"][3]) * 0.5
        # A Kalman filter learns velocity from dense measurements; a
        # tiny target is re-found every few frames, and by the time the
        # filter has converged the target has outrun its own gate.  The
        # measured hop between sightings is injected straight into the
        # state instead, so the coast between sightings follows.
        if track.prev_ut is not None and t > track.prev_ut + 1e-6:
            dt = t - track.prev_ut
            vx = (cx - track.prev_cx) / dt
            vy = (cy - track.prev_cy) / dt
            if abs(vx) < 4000.0 and abs(vy) < 4000.0:
                track.kf.x[4] = 0.6 * vx + 0.4 * track.kf.x[4]
                track.kf.x[5] = 0.6 * vy + 0.4 * track.kf.x[5]
        track.prev_cx, track.prev_cy, track.prev_ut = cx, cy, float(t)
        track.hits += 1
        track.misses = 0
        track.age += 1
        track.box = tuple(float(v) for v in det["box"])
        track.score = float(det.get("score", track.score))
        track.last_t = float(t)
        track.kf.update(track.box, t)
        track.motion.add(t, cx, cy)
        track.fit = det.get("fit", track.fit)
        track.source = det.get("source", track.source)
        track.gated = bool(det.get("size_gated", False))
        track.truncated = bool(det.get("truncated", track.truncated))
        if det.get("has_face"):
            track.has_face = True
        if track.gated:
            # too small for keypoints to mean anything: say so honestly
            # instead of letting a noise vector pick a label
            track.view = "unknown"
            track.stance = "unknown"
            track.view_probs = {"unknown": 1.0}
            track.stance_probs = {"unknown": 1.0}
        else:
            # a truncated body may carry an honest {"unknown": 1.0}
            # from the brain; the smoothing stores only know the real
            # classes, so let the override through untouched instead of
            # letting an all-zero row argmax to a random label
            vprobs = det.get("view_probs") or {}
            sprobs = det.get("stance_probs") or {}
            if len(vprobs) == 1 and "unknown" in vprobs:
                track.view = "unknown"
                track.view_probs = {"unknown": 1.0}
            else:
                if "unknown" in track.view_probs:
                    track.view_probs = {c: 0.0 for c in VIEW_CLASSES}
                track.view = self._smooth(track.view_probs, vprobs,
                                          self.view_smoothing, track.view,
                                          self.switch_margin)
            if len(sprobs) == 1 and "unknown" in sprobs:
                track.stance = "unknown"
                track.stance_probs = {"unknown": 1.0}
            else:
                if "unknown" in track.stance_probs:
                    track.stance_probs = {c: 0.0 for c in STANCE_CLASSES}
                track.stance = self._smooth(track.stance_probs, sprobs,
                                            self.stance_smoothing,
                                            track.stance,
                                            self.switch_margin)
        # A picture cannot tell a person who is standing still from one
        # who is walking past, because the difference is a stride.  The
        # tracker can: a body that is covering ground is walking - but
        # only a COMPLETE body: a fragment sweeping across an aim disc
        # is the camera moving, not the person walking.
        if not track.truncated and not track.gated:
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

    # -- queries -------------------------------------------------------
    def best(self, score_fn=None):
        """The most interesting track, by default the biggest one."""
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

    def consolidate(self) -> None:
        """Merge tracks that sit on top of each other.

        A multi-path detector sees the same fragment through several
        windows in one frame; fusion removes most duplicates, but
        across frames - when one path sees the target at one size and
        another at another - two tracks can still be born on one body.
        The older, more confirmed track absorbs the younger.
        """
        def span(tr):
            return math.sqrt(max(4.0, (tr.box[2] - tr.box[0])
                                 * (tr.box[3] - tr.box[1])))
        ordered = sorted(self.tracks,
                         key=lambda tr: (-int(tr.confirmed), -tr.hits, tr.id))
        keep = []
        for tr in ordered:
            dup = False
            for k in keep:
                s1, s2 = span(tr), span(k)
                # only bodies of similar size can be the same body seen
                # twice: a giant fragment and a far person crossing
                # paths must stay separate identities
                if max(s1, s2) > 2.2 * min(s1, s2):
                    continue
                gate = max(12.0, 0.5 * min(s1, s2))
                if math.hypot(tr.center[0] - k.center[0],
                              tr.center[1] - k.center[1]) <= gate:
                    k.hits = max(k.hits, tr.hits)
                    k.has_face = k.has_face or tr.has_face
                    dup = True
                    break
            if not dup:
                keep.append(tr)
        self.tracks = sorted(keep, key=lambda tr: tr.id)

    def reset(self) -> None:
        self.tracks = []
        self._next_id = 1
