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


def random_person(rng: np.random.Generator, scale: float = 1.0) -> dict:
    """Random but plausible body proportions."""
    base = DEFAULT_PROPORTIONS
    p = {}
    p["hip_hw"] = base["hip_hw"] * scale * rng.uniform(0.85, 1.18)
    p["shoulder_hw"] = base["shoulder_hw"] * scale * rng.uniform(0.85, 1.18)
    p["pelvis_y"] = base["pelvis_y"] * scale * rng.uniform(0.88, 1.12)
    p["spine_len"] = base["spine_len"] * scale * rng.uniform(0.92, 1.08)
    p["neck_len"] = base["neck_len"] * scale * rng.uniform(0.9, 1.15)
    p["upper_arm"] = base["upper_arm"] * scale * rng.uniform(0.9, 1.1)
    p["forearm"] = base["forearm"] * scale * rng.uniform(0.9, 1.1)
    p["thigh"] = base["thigh"] * scale * rng.uniform(0.9, 1.12)
    p["shin"] = base["shin"] * scale * rng.uniform(0.9, 1.12)
    p["foot"] = base["foot"] * scale * rng.uniform(0.9, 1.1)
    return p


def build_skeleton(p: dict, stance: str = "stand", amount: float = 0.0,
                    rng: np.random.Generator = None) -> np.ndarray:
    """Build the 17 COCO joints of a body in the body frame.

    ``stance`` is one of ``stand``, ``crouch``, ``prone``, ``jump`` or
    ``walk``.  ``amount`` is the intensity of that stance in ``[0, 1]``.
    """
    rng = rng or np.random.default_rng(0)
    j = np.zeros((N_KP, 3), dtype=np.float64)

    torso_pitch = 0.0        # forward lean of the spine
    hip_pitch = 0.0          # swing of the thigh away from vertical
    knee_flex = 0.0          # extra bend of the knee
    pelvis_drop = 0.0
    arm_pitch = 0.0
    arm_pitch_b = 0.0        # the other arm
    arm_abduct = 0.0
    elbow_flex = 0.0
    elbow_flex_b = 0.0
    body_pitch = 0.0         # global pitch (prone)
    stride = 0.0             # opposite phase for the two legs

    if stance == "crouch":
        amount = float(np.clip(amount, 0.0, 1.0))
        pelvis_drop = 0.40 * amount
        # a negative hip angle swings the thigh forward, which is what
        # a crouching or sitting person does
        hip_pitch = math.radians(-68.0 * amount)
        knee_flex = math.radians(74.0 * amount)
        torso_pitch = math.radians(22.0 * amount)
        arm_pitch = math.radians(18.0 * amount)
    elif stance == "prone":
        amount = float(np.clip(amount, 0.0, 1.0))
        # lying down: the body is close to horizontal whatever the
        # amount does
        body_pitch = math.radians(70.0 + 20.0 * amount)
        pelvis_drop = 0.80 * amount
        knee_flex = math.radians(70.0 * amount)
        hip_pitch = math.radians(-18.0 * amount)
        elbow_flex = math.radians(60.0 * amount)
        arm_pitch = math.radians(35.0 * amount)
    elif stance == "jump":
        amount = float(np.clip(amount, 0.0, 1.0))
        hip_pitch = math.radians(-30.0 * amount)
        knee_flex = math.radians(55.0 * amount)
        pelvis_drop = -0.10 * amount
        arm_pitch = math.radians(135.0 * amount)
        elbow_flex = math.radians(25.0 * amount)
    elif stance == "sit":
        # sitting down: thighs forward, shins down, hips at seat height
        amount = float(np.clip(amount, 0.0, 1.0))
        pelvis_drop = 0.50 * amount
        hip_pitch = math.radians(-88.0 * amount)
        knee_flex = math.radians(86.0 * amount)
        torso_pitch = math.radians(7.0 * amount)
        arm_pitch = math.radians(24.0 * amount)
        elbow_flex = math.radians(72.0 * amount)
    elif stance == "walk":
        amount = float(np.clip(amount, 0.0, 1.0))
        stride = math.radians(24.0 * amount)
        hip_pitch = stride
        knee_flex = math.radians(max(4.0, 26.0 * amount))
        arm_pitch = -stride * 0.8
        elbow_flex = math.radians(14.0 * amount)

    # Nobody stands like a mannequin.  A little lean, a little
    # asymmetry in the arms and a bit of elbow bend make the training
    # data look like people rather than diagrams.
    torso_pitch += math.radians(rng.uniform(-12.0, 16.0))
    arm_pitch += math.radians(rng.uniform(-35.0, 35.0))
    arm_pitch_b = arm_pitch + math.radians(rng.uniform(-40.0, 40.0))
    elbow_flex += math.radians(rng.uniform(0.0, 80.0))
    elbow_flex_b = elbow_flex + math.radians(rng.uniform(-35.0, 45.0))
    arm_abduct += math.radians(rng.uniform(0.0, 25.0))

    # --- torso chain ------------------------------------------------
    pelvis = np.array([0.0, p["pelvis_y"] - pelvis_drop, 0.0])
    neck_local = np.array([0.0, p["spine_len"], 0.0])
    neck = pelvis + _rot_x(neck_local, torso_pitch)
    head = neck + _rot_x(np.array([0.0, p["neck_len"], 0.0]), torso_pitch)
    head_fwd = _rot_x(np.array([0.0, 0.0, 1.0]), torso_pitch)

    def place_face(idx, off):
        ox, oy, oz = off
        local = np.array([ox, oy, oz])
        # face offsets ride along with the head orientation
        pos = head + _rot_x(local, torso_pitch)
        j[idx] = pos

    place_face(0, FACE_OFFSETS["nose"])
    place_face(1, FACE_OFFSETS["left_eye"])
    place_face(2, FACE_OFFSETS["right_eye"])
    place_face(3, FACE_OFFSETS["left_ear"])
    place_face(4, FACE_OFFSETS["right_ear"])

    # --- shoulders --------------------------------------------------
    # the shoulders ride with the torso, so a leaning person has
    # leaning shoulders
    sh_off = _rot_x(np.array([p["shoulder_hw"], -0.055 * p["spine_len"],
                              0.0]), torso_pitch)
    sh_r = neck + np.array([sh_off[0], sh_off[1], sh_off[2]])
    sh_l = neck + np.array([-sh_off[0], sh_off[1], sh_off[2]])
    j[5] = sh_l
    j[6] = sh_r

    # --- arms -------------------------------------------------------
    for sign, (sidx, eidx, widx) in ((-1.0, (5, 7, 9)), (1.0, (6, 8, 10))):
        use_pitch = arm_pitch if sign < 0 else arm_pitch_b
        use_elbow = elbow_flex if sign < 0 else elbow_flex_b
        upper = np.array([0.0, -p["upper_arm"], 0.0])
        upper = _rot_x(upper, use_pitch + stride * (1.0 if sign > 0 else -1.0))
        upper = _rot_z(upper, -sign * arm_abduct)
        elbow = j[sidx] + upper
        fore = np.array([0.0, -p["forearm"], 0.0])
        fore = _rot_x(fore, use_pitch + use_elbow
                      + stride * (1.0 if sign > 0 else -1.0))
        fore = _rot_z(fore, -sign * arm_abduct)
        j[eidx] = elbow
        j[widx] = elbow + fore

    # --- hips, legs -------------------------------------------------
    hip_r = pelvis + np.array([p["hip_hw"], 0.0, 0.0])
    hip_l = pelvis + np.array([-p["hip_hw"], 0.0, 0.0])
    j[11] = hip_l
    j[12] = hip_r
    for sign, (hidx, kidx, aidx) in ((-1.0, (11, 13, 15)), (1.0, (12, 14, 16))):
        hp = hip_pitch + stride * (1.0 if sign > 0 else -1.0)
        thigh = _rot_x(np.array([0.0, -p["thigh"], 0.0]), hp)
        knee = j[hidx] + thigh
        shin = _rot_x(np.array([0.0, -p["shin"], 0.0]), hp + knee_flex)
        ankle = knee + shin
        j[kidx] = knee
        j[aidx] = ankle

    if body_pitch:
        # rotate the whole body about the hips so it lies down
        pivot = j[11].copy() * 0.5 + j[12].copy() * 0.5
        j = np.array([_rot_x(v - pivot, body_pitch) + pivot for v in j])
        j[:, 1] -= 0.02
    return j


def body_to_camera(points: np.ndarray, yaw_deg: float) -> np.ndarray:
    """Map body-frame joints into the camera frame."""
    t = math.radians(float(yaw_deg))
    c = math.cos(t)
    s = math.sin(t)
    out = np.empty_like(points)
    px = points[:, 0]
    py = points[:, 1]
    pz = points[:, 2]
    out[:, 0] = -px * c + pz * s
    out[:, 1] = -py
    out[:, 2] = -px * s - pz * c
    return out


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


def project(points_cam: np.ndarray, focal: float, depth: float,
            cx: float, cy: float) -> np.ndarray:
    """Pinhole projection of camera-frame points to pixels."""
    z = points_cam[:, 2] + depth
    z = np.where(np.abs(z) < 1e-3, 1e-3, z)
    u = cx + focal * points_cam[:, 0] / z
    v = cy + focal * points_cam[:, 1] / z
    return np.stack([u, v], axis=1)


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def face_visibility(yaw_deg: float) -> dict:
    """Which face landmarks a camera at this yaw can actually see.

    Returns a mapping of joint name to a 0..1 visibility factor.  A
    nose pointing straight away from the camera is hidden by the
    skull, which is the single strongest cue that separates a person
    seen from behind from one seen from the front.
    """
    t = math.radians(float(yaw_deg))
    c = math.cos(t)
    s = math.sin(t)
    facing = c
    nose = _clamp01((facing + 0.28) / 0.55)
    eye = _clamp01((facing - 0.05) / 0.45)
    near_ear = 0.30 + 0.60 * abs(s)
    far_ear = 0.30 * (1.0 - abs(s)) + 0.45 * max(0.0, -facing)
    if abs(s) < 0.20:
        # nearly square on: both ears sit on the edge of the head
        ear_l = ear_r = max(0.25, 0.30 + 0.45 * max(0.0, -facing))
    elif s > 0:
        # the body faces towards the right of the image, so the
        # person's right ear is the one turned towards the camera
        ear_r = near_ear
        ear_l = far_ear
    else:
        ear_l = near_ear
        ear_r = far_ear
    # A pose detector is not a careful annotator: it keeps reporting
    # the face joints even when the face is mostly hidden, it just
    # becomes less sure about them.  The floor below keeps that
    # behaviour, so the classifier learns to read the *fall* of the
    # confidence rather than a clean cut.
    nose = max(nose, 0.28)
    eye = max(eye, 0.22)
    return {
        "nose": nose,
        "left_eye": eye,
        "right_eye": eye,
        "left_ear": _clamp01(ear_l),
        "right_ear": _clamp01(ear_r),
    }


def _body_extremes(joints: np.ndarray) -> np.ndarray:
    """Extra points that bound the body but are not COCO joints.

    A person detector's box reaches from the top of the head to the
    tips of the toes, which is wider and taller than the spread of the
    seventeen joints.  Without these the synthetic boxes would be
    tighter than real ones and every measurement taken from the box
    would be biased.
    """
    neck = (joints[5] + joints[6]) * 0.5
    head_top = neck + np.array([0.0, 0.175, -0.005])
    toe_r = joints[16] + np.array([0.0, -0.045, 0.135])
    toe_l = joints[15] + np.array([0.0, -0.045, 0.135])
    hand_r = joints[10] + np.array([0.02, -0.05, 0.0])
    hand_l = joints[9] + np.array([-0.02, -0.05, 0.0])
    return np.stack([head_top, toe_r, toe_l, hand_r, hand_l])


def make_sample(rng: np.random.Generator, stance: str = "stand",
                amount: float = 0.0, image_size=(640, 640)):
    """Render one labelled person.

    Returns a dict with ``kps`` (``(17, 3)`` image pixels), ``box``,
    ``yaw``, ``stance`` and ``visible`` (per-joint visibility before
    noise is applied).
    """
    H, W = image_size
    p = random_person(rng, scale=rng.uniform(0.92, 1.06))
    joints = build_skeleton(p, stance, amount, rng)

    yaw = float(rng.uniform(-180.0, 180.0))

    # A limb that is behind the body is not seen where it really is:
    # it is seen against the body, or not at all.  Pulling the far
    # limbs towards the middle line is what makes a turned body
    # lopsided, and that lopsidedness is the only thing that says
    # which way a person is turned once the face has gone.
    px_sign = np.array([-1.0 if name.startswith("left") else
                        (1.0 if name.startswith("right") else 0.0)
                        for name in KEYPOINT_NAMES])
    turned = math.sin(math.radians(yaw))
    if abs(turned) > 0.2:
        hide = 0.35 + 0.5 * min(1.0, abs(turned))
        for i in range(N_KP):
            if px_sign[i] * turned < -0.25:
                joints[i, 0] *= hide

    cam = body_to_camera(joints, yaw)

    # pick a focal length and a distance so the person fills a
    # reasonable part of the frame
    body_h = float(joints[:, 1].max() - joints[:, 1].min())
    focal = float(W * rng.uniform(0.85, 1.6))
    target_frac = rng.uniform(0.45, 0.95)
    depth = focal * body_h / max(1e-3, (target_frac * H))

    px = project(cam, focal, depth, W * 0.5, H * 0.5)
    extremes = project(body_to_camera(_body_extremes(joints), yaw), focal,
                       depth, W * 0.5, H * 0.5)
    # put the body in the middle of the frame.  Projecting the pelvis
    # to the centre of the frame would leave the head off the top and
    # the feet off the bottom, and a body that is only half in the
    # picture teaches the wrong thing about every measurement taken
    # from the box.
    allpts = np.concatenate([px, extremes], axis=0)
    centre = (allpts.min(axis=0) + allpts.max(axis=0)) * 0.5
    shift = np.array([W * 0.5, H * 0.5]) - centre
    px = px + shift
    extremes = extremes + shift
    vis = face_visibility(yaw)
    conf = np.ones(N_KP, dtype=np.float64)
    for name, idx in ((k, i) for i, k in enumerate(KEYPOINT_NAMES)):
        if name in vis:
            conf[idx] = vis[name]

    # the far side of the body is partially hidden by the torso.  The
    # side that faces away is the one opposite the direction the body
    # is turned towards.
    far_penalty = np.where(px_sign * turned < -0.30, 0.28, 0.0)
    conf = np.clip(conf - far_penalty, 0.02, 1.0)

    x1 = float(min(px[:, 0].min(), extremes[:, 0].min()))
    x2 = float(max(px[:, 0].max(), extremes[:, 0].max()))
    y1 = float(min(px[:, 1].min(), extremes[:, 1].min()))
    y2 = float(max(px[:, 1].max(), extremes[:, 1].max()))
    box = (x1, y1, x2, y2)
    kps = np.concatenate([px, conf[:, None]], axis=1)
    return {"kps": kps, "box": box, "yaw": yaw, "stance": stance,
            "visible": conf, "props": p, "focal": focal, "depth": depth,
            "body_height": body_h}


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

STANCE_CLASSES = ("standing", "crouch", "prone", "jump")
