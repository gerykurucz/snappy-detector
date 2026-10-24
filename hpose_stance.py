"""What is this person doing?

Four body states are distinguished:

``standing`` - upright, legs together or nearly so,
``walk``     - upright, legs apart, one foot forward,
``crouch``   - down low, knees bent, the head well below its
               standing height,
``prone``    - down on the ground, the body more horizontal than
               vertical,
``jump``     - off the ground, knees tucked, arms up.

The classifier works on the geometry alone: where the hips sit inside
the bounding box, how far the knees are bent, how far the torso
leans, and how wide the box is compared with how tall it is.  A prone
figure is the easy case - the body is wider than it is tall - and a
crouch is the hard one, because a crouching person and a short person
standing still look alike until the knee angles are read.
"""

from __future__ import annotations

import numpy as np

#: The five body states, in the order the stance head was trained.
#: (Lived in the training generator; the generator is gone, this is the
#: canonical copy.)
STANCE_CLASSES = ("standing", "crouch", "prone", "jump", "walk")


class StanceResult(object):
    """The answer for one person."""

    __slots__ = ("name", "confidence", "probs", "secondary", "crouched",
                 "prone", "airborne")

    def __init__(self, name="standing", confidence=0.0, probs=None):
        self.name = name
        self.confidence = float(confidence)
        self.probs = dict(probs or {})
        self.secondary = ""
        self.crouched = name == "crouch"
        self.prone = name == "prone"
        self.airborne = name == "jump"

    def as_dict(self) -> dict:
        return {"stance": self.name, "confidence": round(self.confidence, 4),
                "crouched": self.crouched, "prone": self.prone,
                "airborne": self.airborne,
                "probs": {k: round(v, 4) for k, v in self.probs.items()}}

    def __repr__(self) -> str:
        return "StanceResult(%s, %.2f)" % (self.name, self.confidence)


class StanceClassifier(object):
    """Turns a feature vector into a body state."""

    def __init__(self, bundle):
        self.bundle = bundle
        self.head = getattr(bundle, "stance_head", None)
        self.classes = list(getattr(bundle, "stance_classes", None)
                            or STANCE_CLASSES)

    @property
    def ready(self) -> bool:
        return self.head is not None

    def probs(self, feats: np.ndarray) -> np.ndarray:
        if self.head is None:
            n = len(STANCE_CLASSES)
            return np.full(n, 1.0 / n)
        p = np.asarray(self.head.proba(feats)[0], dtype=np.float64)
        classes = self.classes or list(STANCE_CLASSES)
        if len(p) != len(STANCE_CLASSES):
            out = np.zeros(len(STANCE_CLASSES))
            for i, name in enumerate(classes):
                if name in STANCE_CLASSES:
                    out[STANCE_CLASSES.index(name)] = p[i]
            p = out / max(1e-9, out.sum())
        return p

    def predict(self, feats: np.ndarray) -> StanceResult:
        p = self.probs(feats)
        p = self._apply_knee_rule(p, feats)
        idx = int(np.argmax(p))
        name = STANCE_CLASSES[idx]
        order = np.argsort(p)[::-1]
        res = StanceResult(name, float(p[idx]),
                           {STANCE_CLASSES[i]: float(p[i])
                            for i in range(len(p))})
        res.secondary = STANCE_CLASSES[int(order[1])] if len(order) > 1 else ""
        return res

    @staticmethod
    def _apply_knee_rule(p: np.ndarray, feats: np.ndarray) -> np.ndarray:
        """Let the knees have the last word.

        A person whose knees are straight is not crouching and not
        lying down, whatever the box around them says.  A person whose
        knees are bent and whose body is upright is not standing still.
        The learned head sees all of this, but it also sees a hundred
        other things, and on a photograph it is often distracted by the
        shape of the box.
        """
        import hpose_features as _features
        names = _features.feature_names()
        feats = np.asarray(feats, dtype=np.float64).reshape(-1)

        def value(name):
            try:
                return float(feats[names.index(name)])
            except (ValueError, IndexError):
                return 0.0

        knee = 0.5 * (value("knee_angle_l") + value("knee_angle_r"))
        spine = value("spine_angle")
        if spine > 0.42:
            return p
        out = np.array(p, dtype=np.float64, copy=True)
        idx = {name: i for i, name in enumerate(STANCE_CLASSES)}
        if spine > 0.45:
            # the body is lying down
            out[idx["standing"]] *= 0.02
            out[idx["walk"]] *= 0.02
            out[idx["jump"]] *= 0.25
        elif knee > 0.86:
            # straight legs and an upright body: standing, or walking
            out[idx["crouch"]] *= 0.02
            out[idx["prone"]] *= 0.02
            out[idx["jump"]] *= 0.02
            # a stride shows up as one foot being lower than the
            # other, but only when the feet were actually seen
            if value("legs_seen") > 0.5:
                stride = value("ankle_dy")
            else:
                stride = 0.0
            if stride > 0.09:
                out[idx["walk"]] *= 1.0
            else:
                out[idx["walk"]] *= 0.15
                out[idx["standing"]] *= 3.0
        elif knee < 0.72:
            # bent knees
            out[idx["standing"]] *= 0.15
        total = out.sum()
        return out / total if total > 1e-9 else out
