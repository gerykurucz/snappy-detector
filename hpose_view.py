
from __future__ import annotations

import math

import numpy as np

from hpose_model3d import VIEW_CLASSES, VIEW_CLASS_YAW


class ViewResult(object):

    __slots__ = ("name", "confidence", "probs", "yaw", "facing_camera",
                 "back_turned", "sideways", "secondary")

    def __init__(self, name="front", confidence=0.0, probs=None, yaw=0.0):
        self.name = name
        self.confidence = float(confidence)
        self.probs = dict(probs or {})
        self.yaw = float(yaw)
        self.facing_camera = name in ("front", "front_left_45",
                                       "front_right_45")
        self.back_turned = name in ("back", "back_left_45", "back_right_45")
        self.sideways = name in ("side_left", "side_right")
        self.secondary = ""

    def as_dict(self) -> dict:
        return {"view": self.name, "confidence": round(self.confidence, 4),
                "yaw": round(self.yaw, 1),
                "facing_camera": self.facing_camera,
                "back_turned": self.back_turned,
                "sideways": self.sideways,
                "probs": {k: round(v, 4) for k, v in self.probs.items()}}

    def __repr__(self) -> str:
        return "ViewResult(%s, %.2f, yaw %.0f)" % (self.name,
                                                   self.confidence, self.yaw)


def _circular_delta(a: float, b: float) -> float:
    d = (float(a) - float(b) + 180.0) % 360.0 - 180.0
    return d


def geometric_prior(yaw: float, sigma: float = 32.0) -> np.ndarray:
    out = np.zeros(len(VIEW_CLASSES), dtype=np.float64)
    for i, name in enumerate(VIEW_CLASSES):
        d = _circular_delta(yaw, VIEW_CLASS_YAW[name])
        out[i] = math.exp(-0.5 * (d / sigma) ** 2)
    total = out.sum()
    if total <= 1e-9:
        out[:] = 1.0 / len(VIEW_CLASSES)
    else:
        out /= total
    return out


class ViewClassifier(object):

    def __init__(self, bundle, geometric_weight: float = None,
                 sigma: float = 32.0, back_weight: float = None):
        self.bundle = bundle
        self.head = getattr(bundle, "view_head", None)
        self.classes = list(getattr(bundle, "view_classes", None)
                            or VIEW_CLASSES)
        meta = getattr(bundle, "meta", None) or {}
        if geometric_weight is None:
            geometric_weight = meta.get("geometric_weight", 0.2)
        if back_weight is None:
            back_weight = meta.get("back_weight",
                                   float(geometric_weight) * 0.4)
        self.geometric_weight = float(geometric_weight)
        self.back_weight = float(back_weight)
        self.sigma = float(sigma)

    @property
    def ready(self) -> bool:
        return self.head is not None

    def probs(self, feats: np.ndarray, fit: dict = None) -> np.ndarray:
        if self.head is None:
            prior = geometric_prior(float((fit or {}).get("yaw", 0.0)),
                                    self.sigma)
            return prior
        p = np.asarray(self.head.proba(feats)[0], dtype=np.float64)
        classes = self.classes or list(VIEW_CLASSES)
        if len(p) != len(VIEW_CLASSES):
            out = np.zeros(len(VIEW_CLASSES))
            for i, name in enumerate(classes):
                if name in VIEW_CLASSES:
                    out[VIEW_CLASSES.index(name)] = p[i]
            p = out / max(1e-9, out.sum())
        if fit is not None and self.geometric_weight > 0.0:
            g = geometric_prior(float(fit.get("yaw", 0.0)), self.sigma)
            if str(fit.get("branch", "")) == "back":
                g = _symmetrise(g)
                weight = self.back_weight
            else:
                weight = self.geometric_weight
            p = p * np.power(np.maximum(g, 1e-9), weight)
            p = p / max(1e-9, p.sum())
        return p

    def predict(self, feats: np.ndarray, fit: dict = None,
                face_visible=None) -> ViewResult:
        p = self.probs(feats, fit)
        if face_visible is None and fit is not None:
            face_visible = (str(fit.get("branch", "")) == "front")
        if face_visible is not None:
            p = self._apply_face_rule(p, bool(face_visible))
        idx = int(np.argmax(p))
        name = VIEW_CLASSES[idx]
        probs = {VIEW_CLASSES[i]: float(p[i]) for i in range(len(p))}
        order = np.argsort(p)[::-1]
        second = VIEW_CLASSES[int(order[1])] if len(order) > 1 else ""
        yaw = circular_mean_yaw(p)
        res = ViewResult(name, float(p[idx]), probs, yaw)
        res.secondary = second
        return res

    @staticmethod
    def _apply_face_rule(p: np.ndarray, face_visible: bool) -> np.ndarray:
        p = np.array(p, dtype=np.float64, copy=True)
        for i, name in enumerate(VIEW_CLASSES):
            if name == "back":
                p[i] *= 1e-4 if face_visible else 1.0
            elif name == "front":
                p[i] *= 1e-4 if not face_visible else 1.0
            elif name.startswith("back"):
                p[i] *= 0.15 if face_visible else 1.0
            elif name.startswith("front"):
                p[i] *= 1.0 if face_visible else 0.15
        total = p.sum()
        return p / total if total > 1e-9 else p


MIRROR = {
    "front": "front",
    "front_left_45": "front_right_45",
    "front_right_45": "front_left_45",
    "side_left": "side_right",
    "side_right": "side_left",
    "back_left_45": "back_right_45",
    "back_right_45": "back_left_45",
    "back": "back",
}


def _symmetrise(p: np.ndarray) -> np.ndarray:
    out = np.array(p, dtype=np.float64, copy=True)
    for i, name in enumerate(VIEW_CLASSES):
        other = MIRROR.get(name)
        if other and other in VIEW_CLASSES:
            j = VIEW_CLASSES.index(other)
            avg = 0.5 * (out[i] + out[j])
            out[i] = avg
            out[j] = avg
    total = out.sum()
    return out / total if total > 1e-9 else out


def circular_mean_yaw(p: np.ndarray) -> float:
    s = 0.0
    c = 0.0
    for i, name in enumerate(VIEW_CLASSES):
        w = float(p[i]) if i < len(p) else 0.0
        a = math.radians(VIEW_CLASS_YAW[name])
        s += w * math.sin(a)
        c += w * math.cos(a)
    if abs(s) < 1e-9 and abs(c) < 1e-9:
        return 0.0
    return math.degrees(math.atan2(s, c)) % 360.0


