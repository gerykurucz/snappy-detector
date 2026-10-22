"""A parametric 3D human skeleton.

The skeleton is the ground truth generator for the view/stance
classifiers.  A body is described by a handful of joint angles plus a
set of segment lengths; the class labels (which way the body faces,
what it is doing) fall straight out of the parameters, so a very large
labelled set can be synthesised without collecting or annotating a
single photograph.

Body frame
-----------
``x`` points to the person's **right**, ``y`` points **up**, ``z``
points **forward** (the way the nose points).  Left/right joints
therefore carry a negative ``x``.

Camera frame
------------
``X`` right, ``Y`` down, ``Z`` into the screen, the usual OpenCV
convention.  A yaw of ``0`` means the person faces the camera; a yaw
of ``+90`` means they face towards the right of the image.
"""

from __future__ import annotations

import math

import numpy as np

from .keypoints import KEYPOINT_NAMES, N_KP

#: Segment lengths in metres for an average adult, used as the default
#: proportions.  ``random_person`` perturbs them.
DEFAULT_PROPORTIONS = {
    "hip_hw": 0.090,      # half distance between the hip joints
    "shoulder_hw": 0.190,  # half distance between the shoulder joints
    "pelvis_y": 0.940,     # hip height above the ground
    "spine_len": 0.500,    # pelvis -> neck
    "neck_len": 0.115,     # neck -> head centre
    "upper_arm": 0.300,
    "forearm": 0.265,
    "thigh": 0.430,
    "shin": 0.435,
    "foot": 0.170,
}

#: Extra lateral offsets of the face landmarks relative to the head
#: centre, in metres.
FACE_OFFSETS = {
    "nose": (0.000, -0.012, 0.108),
    "left_eye": (-0.032, 0.040, 0.092),
    "right_eye": (0.032, 0.040, 0.092),
    "left_ear": (-0.078, 0.015, 0.010),
    "right_ear": (0.078, 0.015, 0.010),
}


def _rot_x(v: np.ndarray, ang: float) -> np.ndarray:
    c = math.cos(ang)
    s = math.sin(ang)
    return np.array([v[0], v[1] * c - v[2] * s, v[1] * s + v[2] * c])


def _rot_y(v: np.ndarray, ang: float) -> np.ndarray:
    c = math.cos(ang)
    s = math.sin(ang)
    return np.array([v[0] * c + v[2] * s, v[1], -v[0] * s + v[2] * c])


def _rot_z(v: np.ndarray, ang: float) -> np.ndarray:
    c = math.cos(ang)
    s = math.sin(ang)
    return np.array([v[0] * c - v[1] * s, v[0] * s + v[1] * c, v[2]])


def build_skeleton(p: dict, rng: np.random.Generator = None) -> np.ndarray:
    """Build the 17 COCO joints of a neutral body, in body frame.

    The canonical template every measurement is taken against; a
    little random lean and arm asymmetry keep it from being a
    mannequin.
    """
    rng = rng or np.random.default_rng(0)
    j = np.zeros((N_KP, 3), dtype=np.float64)

    torso_pitch = 0.0        # forward lean of the spine
    hip_pitch = 0.0          # swing of the thigh away from vertical
    knee_flex = 0.0          # extra bend of the knee
    arm_pitch = 0.0
    arm_pitch_b = 0.0        # the other arm
    arm_abduct = 0.0
    elbow_flex = 0.0
    elbow_flex_b = 0.0

    # Nobody stands like a mannequin.
    torso_pitch += math.radians(rng.uniform(-12.0, 16.0))
    arm_pitch += math.radians(rng.uniform(-35.0, 35.0))
    arm_pitch_b = arm_pitch + math.radians(rng.uniform(-40.0, 40.0))
    elbow_flex += math.radians(rng.uniform(0.0, 80.0))
    elbow_flex_b = elbow_flex + math.radians(rng.uniform(-35.0, 45.0))
    arm_abduct += math.radians(rng.uniform(0.0, 25.0))

    # --- torso chain ------------------------------------------------
    pelvis = np.array([0.0, p["pelvis_y"], 0.0])
    neck = pelvis + _rot_x(np.array([0.0, p["spine_len"], 0.0]), torso_pitch)
    head = neck + _rot_x(np.array([0.0, p["neck_len"], 0.0]), torso_pitch)

    for i, name in enumerate(("nose", "left_eye", "right_eye",
                              "left_ear", "right_ear")):
        ox, oy, oz = FACE_OFFSETS[name]
        j[i] = head + _rot_x(np.array([ox, oy, oz]), torso_pitch)

    # --- shoulders: they ride with the torso -------------------------
    sh_off = _rot_x(np.array([p["shoulder_hw"], -0.055 * p["spine_len"],
                              0.0]), torso_pitch)
    j[5] = neck + np.array([-sh_off[0], sh_off[1], sh_off[2]])
    j[6] = neck + sh_off

    # --- arms -------------------------------------------------------
    for sign, (sidx, eidx, widx) in ((-1.0, (5, 7, 9)), (1.0, (6, 8, 10))):
        use_pitch = arm_pitch if sign < 0 else arm_pitch_b
        use_elbow = elbow_flex if sign < 0 else elbow_flex_b
        upper = _rot_x(np.array([0.0, -p["upper_arm"], 0.0]), use_pitch)
        upper = _rot_z(upper, -sign * arm_abduct)
        elbow = j[sidx] + upper
        fore = _rot_x(np.array([0.0, -p["forearm"], 0.0]),
                      use_pitch + use_elbow)
        fore = _rot_z(fore, -sign * arm_abduct)
        j[eidx] = elbow
        j[widx] = elbow + fore

    # --- hips, legs -------------------------------------------------
    j[11] = pelvis + np.array([-p["hip_hw"], 0.0, 0.0])
    j[12] = pelvis + np.array([p["hip_hw"], 0.0, 0.0])
    for sign, (hidx, kidx, aidx) in ((-1.0, (11, 13, 15)),
                                     (1.0, (12, 14, 16))):
        thigh = _rot_x(np.array([0.0, -p["thigh"], 0.0]), hip_pitch)
        knee = j[hidx] + thigh
        shin = _rot_x(np.array([0.0, -p["shin"], 0.0]),
                      hip_pitch + knee_flex)
        j[kidx] = knee
        j[aidx] = knee + shin
    return j


def body_to_camera_batch(points: np.ndarray,
                         yaws: np.ndarray) -> np.ndarray:
    """Map body-frame joints into the camera frame for many yaws.

    ``points`` is ``(17, 3)`` and ``yaws`` is ``(K,)``; the result is
    ``(K, 17, 3)``.  Doing it in one go matters: the body fit searches
    hundreds of angles at once.
    """
    yaws = np.asarray(yaws, dtype=np.float64).reshape(-1)
    t = np.radians(yaws)
    c = np.cos(t)[:, None]
    s = np.sin(t)[:, None]
    px = points[None, :, 0]
    py = points[None, :, 1]
    pz = points[None, :, 2]
    out = np.empty((len(yaws), points.shape[0], 3), dtype=np.float64)
    out[:, :, 0] = -px * c + pz * s
    out[:, :, 1] = -py
    out[:, :, 2] = -px * s - pz * c
    return out


def yaw_to_class(yaw_deg: float) -> str:
    """Map a yaw angle to one of the eight body-view classes.

    ``yaw`` is the direction the body faces relative to the camera:
    0 means facing the camera, 180 means facing away, +90 means
    facing towards the right of the image.
    """
    y = float(yaw_deg) % 360.0
    if y < 22.5 or y >= 337.5:
        return "front"
    if y < 67.5:
        return "front_right_45"
    if y < 112.5:
        return "side_right"
    if y < 157.5:
        return "back_right_45"
    if y < 202.5:
        return "back"
    if y < 247.5:
        return "back_left_45"
    if y < 292.5:
        return "side_left"
    return "front_left_45"


#: The eight view classes in a stable order.
VIEW_CLASSES = (
    "front",
    "front_right_45",
    "side_right",
    "back_right_45",
    "back",
    "back_left_45",
    "side_left",
    "front_left_45",
)

#: Representative yaw for each view class, used by the geometric
#: prior.
VIEW_CLASS_YAW = {
    "front": 0.0,
    "front_right_45": 45.0,
    "side_right": 90.0,
    "back_right_45": 135.0,
    "back": 180.0,
    "back_left_45": 225.0,
    "side_left": 270.0,
    "front_left_45": 315.0,
}

