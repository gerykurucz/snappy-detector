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
from .synth import STANCE_CLASSES


class Track(object):
    """One tracked person."""

    __slots__ = ("id", "box", "score", "hits", "misses", "age", "kf",
                 "motion", "view_probs", "stance_probs", "view", "stance",
                 "view_history", "stance_history", "last_t", "fit",
                 "confirmed", "has_face")

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
        }


def _greedy_match(tracks, dets, iou_threshold: float):
    """Match tracks to detections by IoU, best pairs first."""
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

        matches, used = _greedy_match(self.tracks, high, self.iou_threshold)
        matched_det = set(di for _, di in matches)

        # second chance for the detections the detector was unsure about
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

        # age out the tracks that were not seen
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
        # A picture cannot tell a person who is standing still from one
        # who is walking past, because the difference is a stride.  The
        # tracker can: a body that is covering ground is walking.
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

    def reset(self) -> None:
        self.tracks = []
        self._next_id = 1
