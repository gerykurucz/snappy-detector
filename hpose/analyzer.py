"""The high level API: give it a frame, get back everything you know.

    from hpose.analyzer import Analyzer

    a = Analyzer()
    for track in a.analyze(frame, t):
        print(track.id, track.view, track.stance, track.motion_state)

One call does the whole job: people are detected, faces are looked for,
the body is fitted to recover how far it is turned, the view and the
stance are classified, and everything is tracked from frame to frame so
that the answers are steady instead of flickering.
"""

from __future__ import annotations

import math
import time

import numpy as np

from . import features
from . import posefit
from .bundle import load_bundle
from .detector import FaceDetector, PoseDetector
from .keypoints import Pose
from .stance import StanceClassifier
from .tracker import Tracker
from .view import ViewClassifier


class Analyzer(object):
    """Detects people and explains what they are doing."""

    def __init__(self, bundle_path: str = None, pose_model: str = None,
                 face_model: str = None, conf: float = 0.25,
                 iou: float = 0.45, use_face: bool = True,
                 download: bool = True, tracker: Tracker = None,
                 focal_ratio: float = 0.9, face_region: float = 0.26):
        self.bundle = load_bundle(bundle_path)
        self.pose_detector = PoseDetector(model=pose_model, conf=conf,
                                          iou=iou, download=download)
        self.face_detector = None
        if use_face:
            try:
                self.face_detector = FaceDetector(model=face_model,
                                                  download=download)
            except Exception:
                self.face_detector = None
        self.view_clf = ViewClassifier(self.bundle)
        self.stance_clf = StanceClassifier(self.bundle)
        self.tracker = tracker or Tracker()
        self.focal_ratio = float(focal_ratio)
        self.face_region = float(face_region)
        self.last_t = 0.0

    # -- helpers -------------------------------------------------------
    def _focal_px(self, img) -> float:
        return max(1.0, img.shape[1] * self.focal_ratio)

    def _face_boxes(self, img):
        if self.face_detector is None:
            return []
        try:
            return self.face_detector.detect(img)
        except Exception:
            return []

    @staticmethod
    def _head_region(box, frac: float):
        """The part of the box where a face would be."""
        x1, y1, x2, y2 = box
        w = x2 - x1
        h = y2 - y1
        return (x1 - w * 0.12, y1 - h * 0.04, x2 + w * 0.12,
                y1 + h * frac)

    def _head_crop(self, img, pose):
        """A square crop around the head, large enough to show a face.

        A face in a wide shot is a handful of pixels across, and a
        face detector that cannot see it will say "no face" for a
        person who is looking straight at the camera.  Cutting the head
        out and making it big again is the difference between knowing
        which way somebody is facing and guessing.
        """
        import cv2
        h_img, w_img = img.shape[:2]
        kps = pose.kps
        pts = [(float(kps[i][0]), float(kps[i][1]))
               for i in (0, 1, 2, 3, 4) if kps[i][2] >= 0.25]
        if pts:
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            shoulder = abs(float(kps[6][0]) - float(kps[5][0]))
            radius = max(shoulder * 0.7, 10.0)
        else:
            x1, y1, x2, y2 = pose.box
            radius = max((x2 - x1) * 0.20, (y2 - y1) * 0.12, 10.0)
            cx = (x1 + x2) * 0.5
            cy = y1 + radius * 0.7
        radius = min(radius, 0.45 * min(h_img, w_img))
        x1 = int(max(0, cx - radius))
        y1 = int(max(0, cy - radius))
        x2 = int(min(w_img, cx + radius))
        y2 = int(min(h_img, cy + radius))
        if x2 - x1 < 8 or y2 - y1 < 8:
            return None
        crop = img[y1:y2, x1:x2]
        size = 256
        scale = size / float(max(crop.shape[0], crop.shape[1]))
        return cv2.resize(crop, (max(8, int(crop.shape[1] * scale)),
                                  max(8, int(crop.shape[0] * scale))),
                           interpolation=cv2.INTER_CUBIC)

    def _has_face(self, faces, box) -> bool:
        if not faces:
            return False
        rx1, ry1, rx2, ry2 = self._head_region(box, self.face_region)
        for f in faces:
            fx = (f[0] + f[2]) * 0.5
            fy = (f[1] + f[3]) * 0.5
            if rx1 <= fx <= rx2 and ry1 <= fy <= ry2:
                return True
        return False

    def _face_at(self, img, pose, faces) -> bool:
        """Is there a face on this person?

        The whole picture is searched first, because a face detector
        is good at small faces it can see.  When it finds nothing, the
        head is cut out and enlarged and looked at again, which is
        what makes the difference between a person six metres away and
        a person three metres away.
        """
        if self._has_face(faces, pose.box):
            return True
        if self.face_detector is None:
            return False
        crop = self._head_crop(img, pose)
        if crop is None:
            return False
        try:
            found = self.face_detector.detect(crop)
        except Exception:
            return False
        return bool(found)

    # -- the main entry point ------------------------------------------
    def analyze(self, img, t: float = None, reset: bool = False) -> list:
        """Analyse one frame.  Returns the list of live tracks.

        ``reset`` starts a fresh stream, which is what you want when
        the frames do not belong to the same scene.
        """
        if t is None:
            t = time.perf_counter()
        if img is None or getattr(img, "size", 0) == 0:
            return []
        if reset:
            self.tracker.reset()
        self.last_t = t
        faces = self._face_boxes(img)
        focal = self._focal_px(img)
        poses = self.pose_detector.detect(img)
        dets = []
        for pose in poses:
            det = self.explain(pose, focal, faces, img)
            dets.append(det)
        return self.tracker.update(dets, t)

    def explain(self, pose: Pose, focal_px: float, faces=None,
                img=None) -> dict:
        """Classify a single pose without touching the tracker."""
        has_face = False
        if img is not None:
            has_face = self._face_at(img, pose, faces or [])
        else:
            has_face = self._has_face(faces or [], pose.box)
        fit = posefit.fit(pose.kps, focal_px=focal_px, face_visible=has_face
                          if (faces is not None or img is not None) else None)
        feats = features.extract(pose, fit=fit)
        view = self.view_clf.predict(feats, fit, face_visible=has_face)
        stance = self.stance_clf.predict(feats)
        return {
            "box": pose.box,
            "score": pose.score,
            "fit": fit,
            "feats": feats,
            "view": view.name,
            "view_probs": view.probs,
            "stance": stance.name,
            "stance_probs": stance.probs,
            "has_face": has_face,
            "yaw": view.yaw,
        }

    # -- convenience ----------------------------------------------------
    def detect_only(self, img) -> list:
        """Just the person boxes, with no classification."""
        return self.pose_detector.detect(img)

    def reset(self) -> None:
        self.tracker.reset()

    def describe(self) -> str:
        lines = ["hpose analyzer"]
        lines.append("  pose model : %s" % (self.pose_detector.model_name,))
        lines.append("  face model : %s"
                     % ("yes" if self.face_detector else "no"))
        lines.append("  view head  : %s"
                     % ("ready" if self.view_clf.ready else "not trained"))
        lines.append("  stance head: %s"
                     % ("ready" if self.stance_clf.ready else "not trained"))
        return "\n".join(lines)


def draw(img, tracks, show_labels: bool = True):
    """Draw an overlay of the tracks onto a copy of the frame."""
    import cv2
    out = img.copy()
    palette = {
        "front": (80, 220, 80),
        "front_left_45": (60, 200, 160),
        "front_right_45": (60, 160, 220),
        "side_left": (60, 200, 240),
        "side_right": (240, 200, 60),
        "back_left_45": (200, 120, 240),
        "back_right_45": (240, 120, 200),
        "back": (80, 80, 240),
    }
    for tr in tracks:
        x1, y1, x2, y2 = [int(v) for v in tr.box]
        colour = palette.get(tr.view, (200, 200, 200))
        cv2.rectangle(out, (x1, y1), (x2, y2), colour, 2)
        # the centre of the box, where a target is
        cx = int((x1 + x2) * 0.5)
        cy = int((y1 + y2) * 0.5)
        cv2.drawMarker(out, (cx, cy), colour, cv2.MARKER_CROSS, 14, 2)
        # the direction of travel
        vx, vy = tr.velocity
        if abs(vx) > 1.0 or abs(vy) > 1.0:
            tip = (int(cx + vx * 0.25), int(cy + vy * 0.25))
            cv2.arrowedLine(out, (cx, cy), tip, colour, 2,
                            tipLength=0.3)
        if show_labels:
            text = "#%d %s %s %s" % (tr.id, tr.view, tr.stance,
                                     tr.motion_state)
            cv2.putText(out, text, (x1, max(12, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, 1,
                        cv2.LINE_AA)
    return out
