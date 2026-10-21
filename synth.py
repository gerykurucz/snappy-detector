"""Synthetic training data.

A pose classifier needs labels, and labels for "which way is this
person facing" are expensive: every photograph has to be annotated by
hand.  The generator here sidesteps that.  A parametric 3D body is
posed, placed in a virtual camera and degraded the way a real detector
degrades its output.  Because the pose parameters *are* the label, a
hundred thousand labelled bodies cost a few seconds.

The degradation model matters as much as the geometry.  A real
detector

* jitters every joint by a couple of pixels,
* is unsure about joints that are hidden behind the body,
* sometimes reports a plausible-looking position for a joint it never
  really saw,
* occasionally loses the legs entirely when a body is cut off.

Training on clean geometry teaches a classifier to expect perfection,
which does not survive the first real frame.
"""

from __future__ import annotations

import numpy as np

from . import model3d
from .keypoints import N_KP, Pose

#: The five body states the stance classifier distinguishes.
STANCE_CLASSES = ("standing", "crouch", "prone", "jump", "walk")

#: How often each stance is drawn when the sampler is left alone.
DEFAULT_STANCE_MIX = {
    "standing": 0.34,
    "walk": 0.20,
    "crouch": 0.26,
    "jump": 0.10,
    "prone": 0.10,
}

#: Poses that are drawn to fill a label.  A person sitting down is a
#: different picture from a person crouching, but it is the same
#: answer, so both are drawn for the ``crouch`` label.
POSE_FOR_LABEL = {
    "standing": ("standing",),
    "walk": ("walk",),
    "crouch": ("crouch", "sit"),
    "jump": ("jump",),
    "prone": ("prone",),
}

#: How much of the body is hidden, as a chance per joint.
_OCCLUSION_CHANCE = 0.10


def _pick_stance(rng: np.random.Generator, mix=None) -> str:
    mix = mix or DEFAULT_STANCE_MIX
    names = sorted(mix)
    weights = np.array([mix[n] for n in names], dtype=np.float64)
    weights = weights / weights.sum()
    return names[int(rng.choice(len(names), p=weights))]


def _pick_pose(label: str, rng: np.random.Generator) -> str:
    options = POSE_FOR_LABEL.get(label, (label,))
    return options[int(rng.integers(0, len(options)))]


def degrade(kps: np.ndarray, body_height: float, rng: np.random.Generator,
            noise: float = 1.0, hallucinate: bool = True,
            cut_legs: float = 0.12) -> np.ndarray:
    """Make a clean set of joints look like a detector's output."""
    kps = np.array(kps, dtype=np.float32, copy=True)
    if body_height <= 0:
        return kps
    sigma = noise * 0.011 * body_height
    kps[:, :2] += rng.normal(0.0, sigma, size=(N_KP, 2)).astype(np.float32)
    conf = kps[:, 2]
    conf = np.clip(conf * rng.uniform(0.85, 1.15, N_KP)
                   + rng.normal(0.0, 0.05, N_KP), 0.02, 1.0)
    if cut_legs > 0 and rng.random() < cut_legs:
        # the body runs off the bottom of the frame
        conf[11:] = np.minimum(conf[11:], rng.uniform(0.02, 0.18, 6))
    for i in range(N_KP):
        if conf[i] < 0.32 and hallucinate and rng.random() < 0.45:
            # the detector reported a position it really never saw
            jitter = rng.normal(0.0, 0.06 * body_height, 2)
            kps[i, 0] += float(jitter[0])
            kps[i, 1] += float(jitter[1])
            conf[i] = float(rng.uniform(0.18, 0.5))
    kps[:, 2] = np.clip(conf, 0.02, 1.0)
    return kps


def sample(rng: np.random.Generator, stance: str = None, amount: float = None,
           image_size=(640, 640), noise: float = 1.0,
           hallucinate: bool = True, cut_legs: float = 0.12) -> dict:
    """One labelled, degraded person.

    ``stance`` is a *label* from :data:`STANCE_CLASSES`; the pose that
    is actually drawn for it is chosen at random.
    """
    if stance is None:
        stance = _pick_stance(rng)
    pose_name = _pick_pose(stance, rng)
    if amount is None:
        amount = float(rng.uniform(0.45, 1.0))
    base = model3d.make_sample(rng, pose_name, amount, image_size)
    kps = degrade(base["kps"], base["body_height"], rng, noise,
                  hallucinate, cut_legs)
    # the box stays as the generator made it: a detector's box reaches
    # past the joints to the top of the head and the tips of the toes
    box = base["box"]
    height = image_size[0]
    if cut_legs > 0 and rng.random() < cut_legs:
        # the body runs off the bottom of the picture: the legs are
        # outside the frame, so the detector cannot see them and the
        # box stops at the edge
        cut = float(rng.uniform(0.15, 0.55)) * base["body_height"]
        shift = max(0.0, (kps[:, 1].max()) - (height - cut))
        kps[:, 1] += shift
        box = (box[0], box[1] + shift, box[2], min(float(height),
                                                   box[3] + shift))
        for i in (13, 14, 15, 16):
            kps[i, 2] = float(min(kps[i, 2], rng.uniform(0.02, 0.16)))
    kps, box, cut = truncate(kps, box, rng)
    # A detector's box is not always tight on the person.  A bag in
    # the hand, a chair, a loose box from a tracker, a shadow - all of
    # them make the box wider than the body.  If the training data
    # never shows that, the classifier learns to read the width of the
    # box as if it meant something, and then calls a person carrying a
    # bag "prone".
    if rng.random() < 0.12:
        widen = float(rng.uniform(0.05, 0.25)) * (box[3] - box[1])
        if rng.random() < 0.5:
            box = (box[0] - widen, box[1], box[2], box[3])
        else:
            box = (box[0], box[1], box[2] + widen, box[3])
    view = model3d.yaw_to_class(base["yaw"])
    return {
        "kps": kps,
        "box": box,
        "yaw": float(base["yaw"]),
        "view": view,
        "stance": stance,
        "pose": pose_name,
        "focal": float(base["focal"]),
        "depth": float(base["depth"]),
        "body_height": float(base["body_height"]),
    }


#: Where a detector's box can stop, and how often.  A box usually
#: reaches the feet, but often it stops at the ankles, and now and then
#: the legs are missing altogether.  The weights match what real
#: detectors do: most boxes stop at the ankles or the knees.
_CUTS = ("toes", "ankles", "knees", "hips")
_CUT_WEIGHTS = (0.15, 0.45, 0.30, 0.10)


def truncate(kps: np.ndarray, box, rng: np.random.Generator):
    """Cut the body off at some height and drop the joints below it.

    Returns the keypoints, the new box and the name of the cut.
    """
    kps = np.array(kps, dtype=np.float32, copy=True)
    x1, y1, x2, y2 = box
    ankle_y = float(np.mean(kps[[15, 16]][:, 1]))
    knee_y = float(np.mean(kps[[13, 14]][:, 1]))
    hip_y = float(np.mean(kps[[11, 12]][:, 1]))
    cut = str(_CUTS[int(rng.choice(len(_CUTS), p=_CUT_WEIGHTS))])
    if cut == "toes":
        return kps, box, cut
    if cut == "ankles":
        level = ankle_y + 4.0
        below = (15, 16)
    elif cut == "knees":
        level = knee_y + 6.0
        below = (13, 14, 15, 16)
    else:
        level = hip_y + 6.0
        below = (11, 12, 13, 14, 15, 16)
    for i in below:
        kps[i, 2] = float(min(kps[i, 2], rng.uniform(0.02, 0.15)))
    return kps, (x1, y1, x2, min(y2, level)), cut


def dataset(n: int, seed: int = 0, image_size=(640, 640), noise: float = 1.0,
            stance_mix=None, hallu_rate: float = 1.0,
            cut_legs: float = 0.12) -> list:
    """A list of labelled samples."""
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(int(n)):
        out.append(sample(rng, image_size=image_size, noise=noise,
                          hallucinate=rng.random() < hallu_rate,
                          cut_legs=cut_legs))
    return out


def to_pose(sample_dict: dict) -> Pose:
    """Turn a sample dictionary into a :class:`Pose`."""
    return Pose(sample_dict["kps"], sample_dict["box"], 1.0)


def balance(samples: list, classes) -> list:
    """Subsample so every class is equally represented."""
    buckets = {c: [] for c in classes}
    for s in samples:
        key = s.get("view") if "view" in s else s.get("stance")
        if key in buckets:
            buckets[key].append(s)
    smallest = min((len(v) for v in buckets.values()), default=0)
    if smallest == 0:
        return samples
    out = []
    for c in classes:
        out.extend(buckets[c][:smallest])
    return out
