"""Fitting a rigid body template to a set of observed joints.

The view of a body is written all over its silhouette.  When a person
turns, the shoulders and hips foreshorten by the cosine of the angle,
the nose swings sideways, and the far limbs tuck behind the near ones.
This module fits the canonical 3D skeleton to the observed 2D joints
and recovers

* **yaw** - how far the body is turned, in degrees,
* **residuals** - how well a rigid standing body explains the pose,
  which is large for a crouch or a prone figure,
* **repaired joints** - positions for the joints the detector missed.

Camera model
------------
A person standing ``D`` metres from a camera of focal length ``f``
projects to

    u = u0 + f * X / (D + Z)
    v = v0 + f * Y / (D + Z)

The depth ``D`` is not free: the apparent size of a body and its
distance from the camera are the same fact seen twice, so ``D`` is
searched around the value implied by the observed body height and the
known focal length.  Leaving it free would let a wildly wrong pose be
"explained" by a wildly wrong perspective, which is exactly the kind
of mistake that makes a naive fitter useless.

For a fixed yaw and depth the model is linear in the three remaining
unknowns (``f/D``, ``u0`` and ``v0``), so the search is a handful of
batched linear solves rather than a general optimiser.

A mirrored body is symmetric, so the silhouette alone cannot tell a
person seen from the front from one seen from the back.  That
ambiguity is handled explicitly: the face landmarks, or a face
detector, decide which branch of the mirror is the real one.
"""

from __future__ import annotations

import math

import numpy as np

from . import model3d
from .keypoints import N_KP

#: The neutral skeleton every pose is measured against.
CANON = model3d.build_skeleton(model3d.DEFAULT_PROPORTIONS, "stand")

#: Face joints - the only ones that break the front/back symmetry.
FACE_JOINTS = (0, 1, 2, 3, 4)

_COARSE_STEPS = 24
_FINE_STEPS = 13
_FINE_SPAN = 14.0

#: Joints that follow the torso when it leans.
UPPER_JOINTS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10)

#: How far the torso may lean, in degrees.  People lean; a fit that
#: assumes a rigid upright body is wrong by as much as the lean.
LEAN_VALUES = (-26.0, -17.0, -9.0, 0.0, 9.0, 17.0, 26.0)

#: Multipliers applied to the depth implied by the observed body size.
_DEPTH_FACTORS = (0.4, 0.65, 1.0, 1.6, 2.6)

#: Assumed height of a person in metres, used to turn an apparent size
#: into a distance.
BODY_HEIGHT_M = 1.72


def _normalise(tpl: np.ndarray) -> np.ndarray:
    """Scale a template so the body is one unit tall."""
    tpl = np.asarray(tpl, dtype=np.float64)
    h = float(tpl[:, 1].max() - tpl[:, 1].min())
    if h < 1e-6:
        return tpl
    return tpl / h


#: The neutral skeleton, normalised so the body is one unit tall.
CANON_N = _normalise(CANON)


def _batch_solve(fx: np.ndarray, fy: np.ndarray, obs: np.ndarray,
                 w: np.ndarray):
    """Weighted least squares for a whole batch of yaws at once.

    ``fx`` and ``fy`` are ``(K, 17)`` design columns, ``obs`` is
    ``(17, 2)`` and ``w`` is ``(17,)``.  Returns ``(scale, u0, v0,
    residual)`` for each of the ``K`` candidates.
    """
    k = fx.shape[0]
    sw = np.sqrt(np.maximum(w, 0.0))[None, :]
    a = np.zeros((k, 2 * N_KP, 3), dtype=np.float64)
    a[:, 0::2, 0] = fx * sw
    a[:, 0::2, 1] = sw
    a[:, 1::2, 0] = fy * sw
    a[:, 1::2, 2] = sw
    b = np.empty((k, 2 * N_KP), dtype=np.float64)
    b[:, 0::2] = obs[:, 0][None, :] * sw
    b[:, 1::2] = obs[:, 1][None, :] * sw
    ata = np.matmul(a.transpose(0, 2, 1), a)
    atb = np.matmul(a.transpose(0, 2, 1), b[:, :, None])[:, :, 0]
    try:
        theta = np.linalg.solve(ata, atb[:, :, None])[:, :, 0]
    except np.linalg.LinAlgError:
        z = np.zeros((k, 3))
        z[:, 0] = 1.0
        return z[:, 0], z[:, 1], z[:, 2], np.full(k, np.inf)
    diff = np.matmul(a, theta[:, :, None])[:, :, 0] - b
    resid = np.sum(diff * diff, axis=1)
    return theta[:, 0], theta[:, 1], theta[:, 2], resid


def _projectors(yaws: np.ndarray, inv_depth: float,
                 template: np.ndarray):
    """Design columns for a batch of yaws at one depth."""
    pts = model3d.body_to_camera_batch(template, yaws)
    den = 1.0 + inv_depth * pts[:, :, 2]
    den = np.where(np.abs(den) < 1e-3, 1e-3, den)
    return pts[:, :, 0] / den, pts[:, :, 1] / den


def personalise(obs: np.ndarray, w: np.ndarray, template: np.ndarray):
    """Reshape the template to match the body's proportions.

    A generic template and a real body rarely share the same
    measurements, and the mismatch is the single biggest source of
    error in the fit.  Two numbers are enough to fix it: the torso
    length (shoulder line to hip line) and the shoulder width, both
    read straight off the observed joints.  Anthropometric ratios turn
    the torso length into an expected shoulder width, and the template
    is then stretched to match.

    The shoulder width is *not* taken from the image on purpose: in the
    image it is foreshortened by the very angle we are trying to
    measure, and copying it back would throw that cue away.
    """
    tpl = np.asarray(template, dtype=np.float64)
    if w[5] + w[6] < 0.5 or w[11] + w[12] < 0.5:
        return tpl
    torso_obs = float(np.hypot(*((((obs[5] + obs[6]) * 0.5)
                                  - ((obs[11] + obs[12]) * 0.5))[:2])))
    if torso_obs < 1e-3:
        return tpl
    t_torso = float(np.hypot(*((((tpl[5] + tpl[6]) * 0.5)
                                 - ((tpl[11] + tpl[12]) * 0.5))[:2])))
    t_sh = float(abs(tpl[6][0] - tpl[5][0]))
    if t_torso < 1e-6 or t_sh < 1e-6:
        return tpl
    f_y = torso_obs / t_torso
    # anthropometry: the shoulder width is about 0.76 of the torso
    # length.
    f_x = (0.76 * torso_obs) / t_sh
    out = tpl.copy()
    out[:, 0] *= f_x
    out[:, 1] *= f_y
    return out


def _leaned(template: np.ndarray, lean_deg: float) -> np.ndarray:
    """Tip the upper body forward or back about the hips."""
    lean_deg = float(lean_deg)
    if abs(lean_deg) < 1e-6:
        return template
    t = np.asarray(template, dtype=np.float64).copy()
    a = math.radians(lean_deg)
    c = math.cos(a)
    s = math.sin(a)
    pivot = (t[11] + t[12]) * 0.5
    for i in UPPER_JOINTS:
        v = t[i] - pivot
        t[i] = np.array([v[0], v[1] * c - v[2] * s, v[1] * s + v[2] * c]) + pivot
    return t


def _search(obs, weights, yaws, mask, depths, template, leans=LEAN_VALUES):
    """Search yaws, leans and depths together.

    The three parameters are searched as one batch: for every depth and
    every lean the whole yaw grid is solved at once, and the best of
    all of them wins.
    """
    best = None
    lean_list = list(leans)
    for depth in depths:
        inv = 1.0 / max(1e-3, depth)
        for lean in lean_list:
            tpl = _leaned(template, lean)
            fx, fy = _projectors(yaws, inv, tpl)
            scale, u0, v0, resid = _batch_solve(fx, fy, obs, weights)
            valid = np.where(mask, resid, np.inf)
            if not np.isfinite(valid).any():
                continue
            i = int(np.argmin(valid))
            if best is None or resid[i] < best[0]:
                best = (float(resid[i]), float(yaws[i]), float(depth),
                        float(scale[i]), float(u0[i]), float(v0[i]),
                        float(lean))
    return best


def fit(kps: np.ndarray, conf_threshold: float = 0.25, face_visible=None,
        personalise_shape: bool = True, focal_px: float = 960.0,
        body_height_m: float = BODY_HEIGHT_M, depths=None):
    """Fit the canonical body to observed joints.

    ``kps`` is a ``(17, 3)`` array of ``(x, y, confidence)``.  When
    ``face_visible`` is given it overrides the keypoint evidence for
    the front/back decision (``True`` = a face was detected).
    ``focal_px`` is the focal length of the camera in pixels, which
    turns the observed body height into a distance.

    The fit runs in two stages.  The first uses the generic template
    and gives a rough angle; the body is then reshaped to match the
    observed proportions and the fit is repeated, which sharpens the
    angle considerably.

    The result is a dict with the recovered yaw, the residual and the
    repaired joint positions.
    """
    kps = np.asarray(kps, dtype=np.float64)
    obs = kps[:N_KP, :2]
    conf = kps[:N_KP, 2]
    base_w = np.where(conf >= conf_threshold, 1.0, 0.0)
    body_w = base_w.copy()
    for i in FACE_JOINTS:
        body_w[i] = 0.0

    face_conf = float(np.mean(conf[list(FACE_JOINTS)]))
    out = {"yaw": 0.0, "yaw_body": 0.0, "residual": float("inf"),
           "residual_body": float("inf"), "face_conf": face_conf,
           "ambiguity": 0.0, "scale": 0.0, "u0": 0.0, "v0": 0.0,
           "depth": 0.0, "filled": obs.copy(), "branch": "none",
           "stage": 0}

    if base_w.sum() < 4:
        return out

    if face_visible is None:
        # Without a face detector the keypoint confidences have to
        # answer the question, and the useful signal is not the face
        # confidence on its own - a pose detector keeps reporting the
        # face joints even when the face is hidden.  What matters is
        # how the face compares with the rest of the body.
        body_conf = float(np.mean(conf[[5, 6, 7, 8, 9, 10, 11, 12, 13,
                                        14, 15, 16]]))
        if body_conf > 1e-6:
            ratio = face_conf / body_conf
        else:
            ratio = 1.0
        face_visible = bool(face_conf >= 0.15 and ratio > 0.6)

    # how far away is this body?  the apparent size gives it away.
    good = base_w > 0
    if good.any():
        h_px = float(obs[good][:, 1].max() - obs[good][:, 1].min())
    else:
        h_px = 100.0
    h_px = max(4.0, h_px)
    d0 = focal_px * body_height_m / h_px
    if depths is None:
        depths = [d0 * f for f in _DEPTH_FACTORS]

    weights = base_w if face_visible else body_w
    branch = "front" if face_visible else "back"

    coarse = np.linspace(0.0, 360.0, _COARSE_STEPS, endpoint=False)
    if face_visible:
        mask = np.ones_like(coarse, dtype=bool)
    else:
        # The silhouette is mirror symmetric, so only the back-facing
        # branch can be the truth when no face is on show.
        mask = np.array([math.cos(math.radians(y)) < 0.0 for y in coarse])
        if not mask.any():
            mask = np.ones_like(coarse, dtype=bool)

    best = _search(obs, weights, coarse, mask, depths, CANON_N)
    if best is None:
        return out
    out["stage"] = 1

    # --- refine around the winner ---------------------------------
    fine = np.linspace(best[1] - _FINE_SPAN, best[1] + _FINE_SPAN,
                       _FINE_STEPS)
    if face_visible:
        fmask = np.ones_like(fine, dtype=bool)
    else:
        fmask = np.array([math.cos(math.radians(y)) < 0.0 for y in fine])
        if not fmask.any():
            fmask = np.ones_like(fine, dtype=bool)
    refined = _search(obs, weights, fine, fmask, depths, CANON_N)
    if refined is not None and refined[0] <= best[0]:
        best = refined
        out["stage"] = 2

    # --- reshape the body and try again ----------------------------
    template = CANON_N
    lean = best[6] if len(best) > 6 else 0.0
    if personalise_shape:
        raw = personalise(obs, weights, CANON)
        tpl = _normalise(raw)
        if np.all(np.isfinite(tpl)):
            near = [lean + d for d in (-9.0, 0.0, 9.0)]
            second = _search(obs, weights, fine, fmask, depths, tpl, near)
            if second is not None and second[0] <= best[0] * 1.05:
                best = second
                template = tpl
                lean = second[6]
                out["stage"] = 3

    resid, yaw, depth, scale, u0, v0 = best[0], best[1], best[2], \
        best[3], best[4], best[5]
    if len(best) > 6:
        lean = best[6]
    yaw = float(yaw) % 360.0
    inv = 1.0 / max(1e-3, depth)
    tpl = _leaned(template, lean)
    fx, fy = _projectors(np.array([yaw]), inv, tpl)
    filled = np.stack([u0 + scale * fx[0], v0 + scale * fy[0]], axis=1)
    out.update({"yaw": yaw, "yaw_body": yaw, "residual": float(resid),
                "scale": float(scale), "u0": float(u0), "v0": float(v0),
                "depth": float(depth), "filled": filled,
                "lean": float(lean), "branch": branch})
    out["ambiguity"] = float(resid)
    return out


def repair(kps: np.ndarray, result: dict, conf_threshold: float = 0.25):
    """Replace uncertain joints with the fitted template positions."""
    kps = np.array(kps, dtype=np.float32, copy=True)
    filled = result.get("filled")
    if filled is None:
        return kps
    conf = kps[:N_KP, 2]
    for i in range(N_KP):
        if conf[i] < conf_threshold:
            kps[i, 0] = float(filled[i, 0])
            kps[i, 1] = float(filled[i, 1])
    return kps


def fold_yaw(yaw: float) -> float:
    """Fold a yaw into ``[0, 90]`` - the part a silhouette can see."""
    y = abs(float(yaw)) % 360.0
    if y > 180.0:
        y = 360.0 - y
    if y > 90.0:
        y = 180.0 - y
    return y


def yaw_to_class(yaw: float) -> str:
    """The eight-way view class for a yaw angle."""
    return model3d.yaw_to_class(yaw)


def fit_quality(result: dict) -> float:
    """How well the rigid body explains the pose, 0 (bad) to 1 (good).

    The residual is a squared pixel error, so it is divided by the
    number of joints and by the square of the apparent body size.
    """
    scale = float(result.get("scale") or 0.0)
    if scale <= 1e-6:
        return 0.0
    resid = float(result.get("residual") or 0.0)
    rms = math.sqrt(max(0.0, resid) / max(1.0, 2 * N_KP))
    rel = rms / max(1e-6, scale)
    return float(max(0.0, 1.0 - rel / 0.12))
