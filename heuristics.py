"""Rule based fallbacks.

The trained heads are the best answer when they are available, but two
things make a set of rules worth having:

* they are the labels used to bring real photographs into the training
  set, so the classifier learns the shape of real people as well as
  the shape of generated ones;
* they keep the library useful before anything has been trained, and
  when a trained bundle does not fit the picture in front of you.

The rules read the same feature vector the heads read, so they can be
checked against the trained answers.
"""

from __future__ import annotations

import math

import numpy as np

from .model3d import VIEW_CLASSES

STANCE_NAMES = ("standing", "crouch", "prone", "jump", "walk")


class RuleResult(object):
    __slots__ = ("name", "confidence", "scores")

    def __init__(self, name: str, confidence: float, scores=None):
        self.name = name
        self.confidence = float(confidence)
        self.scores = dict(scores or {})

    def __repr__(self) -> str:
        return "RuleResult(%s, %.2f)" % (self.name, self.confidence)


def _feature_map(feats: np.ndarray, names=None) -> dict:
    from . import features
    names = names or features.feature_names()
    feats = np.asarray(feats, dtype=np.float64).reshape(-1)
    return {n: float(feats[i]) for i, n in enumerate(names[:len(feats)])}


def rule_stance(feats: np.ndarray, names=None) -> RuleResult:
    """Guess the body state from the geometry.

    The order of the tests matters.  A body that is lying down is
    recognised first, because its spine is horizontal and nothing else
    is.  Then the knees: a standing person has straight legs, a
    crouching or sitting one does not.  What is left is separated by
    the feet: a wide stance is a step, a narrow one is standing still.
    """
    f = _feature_map(feats, names)
    scores = {n: 0.0 for n in STANCE_NAMES}

    spine = f.get("spine_angle", 0.0)
    knee = 0.5 * (f.get("knee_angle_l", 1.0) + f.get("knee_angle_r", 1.0))
    elbow = 0.5 * (f.get("elbow_angle_l", 1.0) + f.get("elbow_angle_r", 1.0))
    ankle_sep = f.get("ankle_sep_over_sh", 0.4)
    body_axis = f.get("body_axis_angle", 0.0)
    aspect = f.get("box_aspect", 0.35)
    hip = f.get("hip_frac", 0.5)
    legs_seen = f.get("legs_seen", 1.0)

    # lying down: the spine is well away from vertical
    prone = max(0.0, (spine - 0.30) / 0.35)
    prone = max(prone, max(0.0, (body_axis - 0.45) / 0.35))
    prone = max(prone, max(0.0, (aspect - 0.85) / 0.6))
    scores["prone"] = min(1.0, prone)

    # knees: a bent knee means the body is low
    bend = max(0.0, (0.90 - knee) / 0.30)
    scores["crouch"] = min(1.0, bend * 1.1)

    # tucked legs with the body compact: in the air
    tuck = max(0.0, (0.75 - knee) / 0.25) * max(0.0, (0.55 - hip) / 0.2)
    scores["jump"] = min(1.0, tuck)

    # a step: the feet are apart and the knees are nearly straight
    stride = max(0.0, (ankle_sep - 0.45) / 0.45) * max(0.0, (knee - 0.80)
                                                         / 0.12)
    scores["walk"] = min(1.0, stride)

    # standing still: everything is straight and the feet are together
    still = min(1.0, max(0.0, (knee - 0.86) / 0.10)
                * max(0.0, (0.65 - ankle_sep) / 0.30))
    scores["standing"] = min(1.0, still)

    if legs_seen < 0.5:
        # without the legs only the lying-down case can be told
        scores["standing"] *= 0.4
        scores["walk"] *= 0.2
        scores["crouch"] *= 0.5

    name = max(scores, key=lambda k: scores[k])
    total = sum(scores.values()) or 1.0
    return RuleResult(name, scores[name] / total, scores)


def rule_view(yaw: float, face_conf: float = 0.5) -> RuleResult:
    """Guess the body view from a geometric yaw and the face evidence."""
    from .view import VIEW_CLASS_YAW
    scores = {}
    for name in VIEW_CLASSES:
        delta = (float(yaw) - VIEW_CLASS_YAW[name] + 180.0) % 360.0 - 180.0
        scores[name] = math.exp(-0.5 * (delta / 32.0) ** 2)
    total = sum(scores.values()) or 1.0
    name = max(scores, key=lambda k: scores[k])
    return RuleResult(name, scores[name] / total, scores)
