
from __future__ import annotations

import hpose_features as features
import hpose_posefit as posefit
from hpose_bundle import load_bundle
from hpose_detector import FaceDetector, PoseDetector
from hpose_keypoints import Pose
from hpose_stance import StanceClassifier
from hpose_view import ViewClassifier
from hpose_stance import StanceResult
from hpose_view import ViewResult

from system_core import log

class Analyzer(object):

    def __init__(self, bundle_path: str = None, pose_model: str = None,
                 face_model: str = None, conf: float = 0.25,
                 iou: float = 0.45, use_face: bool = True,
                 download: bool = True, face_region: float = 0.26,
                 min_face_person_px: float = 100.0):
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
        self.face_region = float(face_region)
        self.min_face_person_px = float(min_face_person_px)
        self._face_skip_streak = 0
        self._face_disable_after = 30  # frames with no positive -> off

    def _face_boxes(self, img):
        if self.face_detector is None:
            return []
        try:
            return self.face_detector.detect(img)
        except Exception:
            return []

    @staticmethod
    def _head_region(box, frac: float):
        x1, y1, x2, y2 = box
        w = x2 - x1
        h = y2 - y1
        return (x1 - w * 0.12, y1 - h * 0.04, x2 + w * 0.12,
                y1 + h * frac)

    def _head_crop(self, img, pose):
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
        if max(crop.shape[:2]) == size:
            return crop
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
        if self._has_face(faces, pose.box):
            return True
        if self.face_detector is None:
            return False
        h = pose.box[3] - pose.box[1]
        if h < self.min_face_person_px:
            # FaceDetectorYN's minimum reliable face width is ~20 px,
            # which requires a >= 100 px person. Below that, crops are
            # wasted compute.
            return False
        crop = self._head_crop(img, pose)
        if crop is None:
            return False
        try:
            found = self.face_detector.detect(crop)
        except Exception:
            return False
        return bool(found)

    def explain(self, pose: Pose, focal_px: float, faces=None,
                img=None) -> dict:
        has_face = False
        if img is not None:
            has_face = self._face_at(img, pose, faces or [])
        else:
            has_face = self._has_face(faces or [], pose.box)
        # A face detector returning nothing is not evidence of a back view.
        # Only pass an explicit False when we actually have image data and
        # a positive signal of "no face" (i.e. face detector ran and found
        # faces elsewhere but none here). Otherwise let posefit decide from
        # keypoint confidences.
        fit_face_visible = None
        if has_face:
            fit_face_visible = True
        elif faces:  # detector ran, found faces, none here -> real "back"
            fit_face_visible = False
        h_box = float(pose.box[3] - pose.box[1])
        fit = posefit.fit(pose.kps, focal_px=focal_px,
                          face_visible=fit_face_visible, box_h_px=h_box)
        feats = features.extract(pose, fit=fit)

        face_visible_for_view = has_face if has_face else None
        try:
            view = self.view_clf.predict(feats, fit,
                                          face_visible=face_visible_for_view)
        except ValueError as exc:
            if "feature dim" not in str(exc):
                raise
            self._bundle_warned = getattr(self, "_bundle_warned", False)
            if not self._bundle_warned:
                self._bundle_warned = True
                log("hpose: bundle cannot classify view (%r) - the shipped "
                    "bundle must be retrained on current features. View "
                    "filters will not match until then." % (exc,))
            view = ViewResult(name="unknown", confidence=0.0, probs={},
                              yaw=0.0)
        try:
            stance = self.stance_clf.predict(feats)
        except ValueError as exc:
            if "feature dim" not in str(exc):
                raise
            self._bundle_warned = getattr(self, "_bundle_warned", False)
            if not self._bundle_warned:
                self._bundle_warned = True
                log("hpose: bundle cannot classify stance (%r) - the shipped "
                    "bundle must be retrained on current features. Stance "
                    "filters will not match until then." % (exc,))
            stance = StanceResult(name="unknown", confidence=0.0, probs={})
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
