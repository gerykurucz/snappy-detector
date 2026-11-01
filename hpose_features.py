
from __future__ import annotations

import math

import numpy as np

from hpose_keypoints import FACE_IDX, N_KP, Pose, derive_joints
import hpose_posefit as posefit

FEATURE_VERSION = 6

ANCHOR_IDX = (5, 6, 11, 12)

def _angle_at(a, b, c) -> float:
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    c = np.asarray(c, dtype=np.float64)
    v1 = a - b
    v2 = c - b
    n1 = float(np.hypot(v1[0], v1[1]))
    n2 = float(np.hypot(v2[0], v2[1]))
    if n1 < 1e-6 or n2 < 1e-6:
        return 180.0
    cosv = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
    cosv = max(-1.0, min(1.0, cosv))
    return math.degrees(math.acos(cosv))

def fit_similarity(template: np.ndarray, observed: np.ndarray):
    template = np.asarray(template, dtype=np.float64)
    observed = np.asarray(observed, dtype=np.float64)
    n = template.shape[0]
    if n < 2:
        return 1.0, np.eye(2), np.zeros(2)
    mu_t = template.mean(axis=0)
    mu_o = observed.mean(axis=0)
    tc = template - mu_t
    oc = observed - mu_o
    var_t = float((tc * tc).sum() / n)
    if var_t < 1e-9:
        return 1.0, np.eye(2), mu_o - mu_t
    cov = (oc.T @ tc) / n
    try:
        u, s, vt = np.linalg.svd(cov)
    except np.linalg.LinAlgError:
        return 1.0, np.eye(2), mu_o - mu_t
    d = np.sign(np.linalg.det(u @ vt))
    if d == 0:
        d = 1.0
    rot = u @ np.diag([1.0, d]) @ vt
    scale = float((s * np.array([1.0, d])).sum() / var_t)
    if not math.isfinite(scale) or scale <= 1e-6:
        scale = 1.0
    trans = mu_o - scale * (rot @ mu_t)
    return scale, rot, trans

def apply_similarity(template: np.ndarray, scale: float, rot: np.ndarray,
                     trans: np.ndarray) -> np.ndarray:
    return (scale * (rot @ np.asarray(template, dtype=np.float64).T)).T + trans

def impute_missing(kps: np.ndarray, template: np.ndarray = None,
                   threshold: float = 0.2) -> np.ndarray:
    kps = np.array(kps, dtype=np.float32, copy=True)
    if kps.ndim != 2 or kps.shape[0] < N_KP:
        return kps
    conf = kps[:N_KP, 2]
    good = conf >= threshold
    if good.all() or template is None:
        return kps
    tpl = np.asarray(template, dtype=np.float32)
    if tpl.shape != (N_KP, 2):
        return kps

    idx = np.array([i for i in ANCHOR_IDX if good[i]], dtype=np.int64)
    if len(idx) < 2:
        idx = np.array([i for i in range(N_KP) if good[i]], dtype=np.int64)
    if len(idx) < 2:
        return kps
    scale, rot, trans = fit_similarity(tpl[idx], kps[:N_KP][idx][:, :2])
    filled = apply_similarity(tpl, scale, rot, trans)
    for i in range(N_KP):
        if not good[i]:
            kps[i, 0] = float(filled[i, 0])
            kps[i, 1] = float(filled[i, 1])
    return kps

class _Builder(object):

    def __init__(self):
        self.names = []
        self.values = []

    def add(self, name, value):
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = 0.0
        if not math.isfinite(v):
            v = 0.0
        self.names.append(name)
        self.values.append(v)
        return self

    def add_many(self, prefix, values):
        for i, v in enumerate(values):
            self.add("%s_%d" % (prefix, i), v)

    def vec(self) -> np.ndarray:
        return np.asarray(self.values, dtype=np.float32)

def _fill(b: _Builder, pose: Pose, fit: dict,
          conf_threshold: float) -> None:

    raw = np.asarray(pose.kps, dtype=np.float64)
    conf = raw[:N_KP, 2]
    if fit is not None:
        kps = posefit.repair(raw, fit, conf_threshold)
    else:
        kps = impute_missing(raw, None, conf_threshold)
    xy = kps[:N_KP, :2]
    d = derive_joints(kps[:N_KP])
    neck = d[17]
    pelvis = d[18]
    head = d[19]

    x1, y1, x2, y2 = pose.box
    bw = max(1e-3, x2 - x1)
    bh = max(1e-3, y2 - y1)

    scale = bh

    leg_ok = [conf[15] > conf_threshold, conf[16] > conf_threshold]
    span = 0.0
    if any(leg_ok):
        feet = [xy[15][1] if leg_ok[0] else xy[16][1],
                xy[16][1] if leg_ok[1] else xy[15][1]]
        span = abs(max(feet) - head[1])
    b.add("visible_span", span / bh)
    b.add("legs_seen", 1.0 if all(leg_ok) else 0.0)

    rel = (xy - pelvis[None, :2]) / scale
    b.add_many("rel", rel.reshape(-1))
    b.add_many("conf", conf)
    rel_d = (d[:, :2] - pelvis[None, :2]) / scale
    b.add_many("reld", rel_d[17:].reshape(-1))

    sh_w = abs(xy[6][0] - xy[5][0])
    hip_w = abs(xy[12][0] - xy[11][0])
    sh_mid = (xy[5] + xy[6]) * 0.5
    hip_mid = (xy[11] + xy[12]) * 0.5
    torso = max(1e-3, float(np.hypot(*(neck[:2] - pelvis[:2]))))

    b.add("sh_w", sh_w / scale)
    b.add("hip_w", hip_w / scale)
    b.add("sh_over_hip", sh_w / max(1e-3, hip_w))
    b.add("torso_len", torso / scale)
    b.add("head_to_neck", abs(float(head[1] - neck[1])) / scale)
    b.add("nose_off_x", float(xy[0][0] - sh_mid[0]) / max(1e-3, sh_w))
    b.add("nose_off_y", float(xy[0][1] - neck[1]) / torso)
    b.add("pelvis_off_x", float(pelvis[0] - sh_mid[0]) / max(1e-3, sh_w))
    b.add("torso_tilt",
          math.degrees(math.atan2(float(sh_mid[0] - hip_mid[0]),
                                  max(1e-3, abs(float(neck[1] - pelvis[1]))))))

    face_conf = conf[list(FACE_IDX)]
    body_conf = np.concatenate([conf[5:7], conf[11:13]])
    fc = float(np.mean(face_conf))
    b.add("face_conf_mean", fc)
    b.add("face_conf_max", float(np.max(face_conf)))
    b.add("face_conf_min", float(np.min(face_conf)))
    b.add("nose_conf", float(conf[0]))
    b.add("eye_conf", float(np.mean(conf[1:3])))
    b.add("ear_conf", float(np.mean(conf[3:5])))
    b.add("body_conf_mean", float(np.mean(body_conf)))
    b.add("face_over_body", fc / max(0.05, float(np.mean(body_conf))))
    b.add("vis_frac", float(np.mean(conf > conf_threshold)))
    b.add("vis_count", float(np.sum(conf > conf_threshold)))
    b.add("face_vis_count", float(np.sum(face_conf > conf_threshold)))

    left_idx = (5, 7, 9, 11, 13, 15)
    right_idx = (6, 8, 10, 12, 14, 16)
    lc = float(np.mean(conf[list(left_idx)]))
    rc = float(np.mean(conf[list(right_idx)]))
    b.add("limb_conf_left", lc)
    b.add("limb_conf_right", rc)
    b.add("limb_conf_balance", lc - rc)

    def pair_off(i_l, i_r):
        mid = (xy[i_l] + xy[i_r]) * 0.5
        return (abs(float(xy[i_l][0] - mid[0])),
                abs(float(xy[i_r][0] - mid[0])))

    for label, (i_l, i_r) in (("elbow", (7, 8)), ("wrist", (9, 10)),
                              ("knee", (13, 14)), ("ankle", (15, 16)),
                              ("hip", (11, 12))):
        l, r = pair_off(i_l, i_r)
        b.add("asym_%s_l" % label, l / scale)
        b.add("asym_%s_r" % label, r / scale)
        b.add("asym_%s_bal" % label, (l - r) / scale)

    b.add("ankle_sep", float(abs(xy[15][0] - xy[16][0])) / scale)
    b.add("ankle_dy", float(abs(xy[15][1] - xy[16][1])) / scale)
    b.add("knee_dy", float(abs(xy[13][1] - xy[14][1])) / scale)
    b.add("wrist_dy", float(abs(xy[9][1] - xy[10][1])) / scale)
    b.add("wrist_sep", float(abs(xy[9][0] - xy[10][0])) / scale)
    b.add("sh_hip_mid_dx", float(sh_mid[0] - hip_mid[0]) / scale)
    b.add("sh_hip_mid_dy", float(sh_mid[1] - hip_mid[1]) / scale)

    b.add("knee_angle_l", _angle_at(xy[11], xy[13], xy[15]) / 180.0)
    b.add("knee_angle_r", _angle_at(xy[12], xy[14], xy[16]) / 180.0)
    b.add("elbow_angle_l", _angle_at(xy[5], xy[7], xy[9]) / 180.0)
    b.add("elbow_angle_r", _angle_at(xy[6], xy[8], xy[10]) / 180.0)
    b.add("hip_angle_l", _angle_at(xy[5], xy[11], xy[13]) / 180.0)
    b.add("hip_angle_r", _angle_at(xy[6], xy[12], xy[14]) / 180.0)

    leg_len = 0.0
    if conf[15] > conf_threshold and conf[16] > conf_threshold:
        leg_len = (abs(float(xy[15][1] - pelvis[1]))
                   + abs(float(xy[16][1] - pelvis[1]))) * 0.5 / scale
    elif conf[13] > conf_threshold:
        leg_len = abs(float(xy[13][1] - pelvis[1])) / scale
    b.add("leg_len", leg_len)
    b.add("hip_height", float(pelvis[1] - y1) / bh)
    b.add("knee_height", float(xy[13][1] - y1) / bh)
    b.add("ankle_height", float(xy[15][1] - y1) / bh)
    b.add("head_height", float(head[1] - y1) / bh)
    b.add("box_aspect", bw / bh)
    b.add("shoulder_aspect", sh_w / bh)
    b.add("ankle_sep_over_sh",
          float(abs(xy[15][0] - xy[16][0])) / max(1e-3, sh_w))
    b.add("shoulder_pelvis_dy", float(sh_mid[1] - hip_mid[1]) / scale)
    b.add("shoulder_pelvis_dx", float(sh_mid[0] - hip_mid[0]) / scale)
    b.add("tilt_of_body",
          math.degrees(math.atan2(abs(float(hip_mid[1] - sh_mid[1])),
                                  max(1e-3, abs(float(hip_mid[0] - sh_mid[0]))))))

    spine_dx = float(neck[0] - pelvis[0])
    spine_dy = float(neck[1] - pelvis[1])
    b.add("spine_angle", math.degrees(math.atan2(abs(spine_dx),
                                                 max(1e-3, abs(spine_dy))))
          / 90.0)
    b.add("spine_angle_signed",
          math.degrees(math.atan2(spine_dx, max(1e-3, abs(spine_dy))))
          / 90.0)
    feet = []
    if conf[15] > conf_threshold:
        feet.append(xy[15])
    if conf[16] > conf_threshold:
        feet.append(xy[16])
    if feet:
        mid = np.mean(np.stack(feet), axis=0)
        axis_dx = float(head[0] - mid[0])
        axis_dy = float(head[1] - mid[1])
        b.add("body_axis_angle",
              math.degrees(math.atan2(abs(axis_dx), max(1e-3, abs(axis_dy))))
              / 90.0)
        b.add("body_axis_angle_signed",
              math.degrees(math.atan2(axis_dx, max(1e-3, abs(axis_dy))))
              / 90.0)
        b.add("body_axis_span", float(abs(axis_dy)) / scale)
        b.add("body_axis_run", float(abs(axis_dx)) / scale)
    else:
        b.add("body_axis_angle", 0.0)
        b.add("body_axis_angle_signed", 0.0)
        b.add("body_axis_span", 0.0)
        b.add("body_axis_run", 0.0)
        axis_dx = axis_dy = 0.0

    spine_axis = math.degrees(math.atan2(abs(spine_dx),
                                          max(1e-3, abs(spine_dy)))) / 90.0 \
        if (spine_dx or spine_dy) else 0.0
    axis = math.degrees(math.atan2(abs(axis_dx),
                                    max(1e-3, abs(axis_dy)))) / 90.0 \
        if (axis_dx or axis_dy) else 0.0
    b.add("spine_x_axis", spine_axis * axis)
    b.add("spine_minus_axis", spine_axis - axis)

    b.add("hip_frac", float(pelvis[1] - y1) / bh)
    b.add("shoulder_frac", float(sh_mid[1] - y1) / bh)
    b.add("neck_frac", float(neck[1] - y1) / bh)

    # --- Size and visibility features (crucial for 20-160 px targets) ----
    b.add("log_box_h", math.log(max(1.0, bh) / 100.0))
    b.add("log_box_w", math.log(max(1.0, bw) / 100.0))
    for i in range(N_KP):
        b.add("vis_%d" % i, 1.0 if conf[i] > conf_threshold else 0.0)
    vis_conf = np.where(conf > conf_threshold, conf, 0.0)
    if vis_conf.sum() > 1e-6:
        cx = float((xy[:, 0] * vis_conf).sum() / vis_conf.sum())
        cy = float((xy[:, 1] * vis_conf).sum() / vis_conf.sum())
    else:
        cx = cy = 0.0
    b.add("conf_centroid_dx", (cx - (sh_mid[0] + hip_mid[0]) * 0.5) / scale)
    b.add("conf_centroid_dy", (cy - (sh_mid[1] + hip_mid[1]) * 0.5) / scale)

    if fit is not None:
        yaw = float(fit.get("yaw", 0.0))
        b.add("fit_yaw_sin", math.sin(math.radians(yaw)))
        b.add("fit_yaw_cos", math.cos(math.radians(yaw)))
        b.add("fit_yaw_fold", posefit.fold_yaw(yaw) / 90.0)
        b.add("fit_yaw_folded", math.cos(math.radians(2.0 * yaw)))
        b.add("fit_quality", posefit.fit_quality(fit))
        b.add("fit_front_branch",
              1.0 if str(fit.get("branch", "")) == "front" else 0.0)
        b.add("fit_depth_rel", float(fit.get("depth", 0.0)) /
              max(1e-3, float(fit.get("scale", 1.0))))
        b.add("fit_scale_rel", float(fit.get("scale", 0.0)) / scale)
    else:
        for name in ("fit_yaw_sin", "fit_yaw_cos", "fit_yaw_fold",
                     "fit_yaw_folded", "fit_quality", "fit_front_branch",
                     "fit_depth_rel", "fit_scale_rel"):
            b.add(name, 0.0)

def extract(pose: Pose, fit: dict = None,
            conf_threshold: float = 0.2) -> np.ndarray:
    b = _Builder()
    _fill(b, pose, fit, conf_threshold)
    return b.vec()

_NAMES = None

def feature_names() -> list:

    global _NAMES
    if _NAMES is None:
        b = _Builder()
        # Dummy pose with some non-zero confidences to match real code paths
        kps = np.zeros((N_KP, 3), dtype=np.float32)
        # Set some confidences to trigger the same code paths as real poses
        kps[5, 2] = 0.8   # left shoulder
        kps[6, 2] = 0.8   # right shoulder
        kps[11, 2] = 0.8  # left hip
        kps[12, 2] = 0.8  # right hip
        kps[13, 2] = 0.7  # left knee
        kps[14, 2] = 0.7  # right knee
        kps[15, 2] = 0.6  # left ankle
        kps[16, 2] = 0.6  # right ankle
        dummy = Pose(kps, (0.0, 0.0, 10.0, 10.0), 1.0)
        _fill(b, dummy, None, 0.2)
        _NAMES = b.names
    return list(_NAMES)
