
from __future__ import annotations

import numpy as np

N_KP = 17

KEYPOINT_NAMES = (
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
)

KP = {name: i for i, name in enumerate(KEYPOINT_NAMES)}

SKELETON = (
    (0, 1), (0, 2), (1, 3), (2, 4),
    (0, 5), (0, 6), (5, 6),
    (5, 7), (7, 9), (6, 8), (8, 10),
    (5, 11), (6, 12), (11, 12),
    (11, 13), (13, 15), (12, 14), (14, 16),
)

FACE_IDX = (0, 1, 2, 3, 4)
SHOULDER_IDX = (5, 6)
HIP_IDX = (11, 12)
ELBOW_IDX = (7, 8)
WRIST_IDX = (9, 10)
KNEE_IDX = (13, 14)
ANKLE_IDX = (15, 16)

DERIVED_NAMES = ("neck", "pelvis", "head_center")
N_KP_DERIVED = N_KP + len(DERIVED_NAMES)
DERIVED_OFFSET = N_KP



def derive_joints(kps: np.ndarray) -> np.ndarray:
    kps = np.asarray(kps, dtype=np.float32)
    if kps.ndim != 2 or kps.shape[0] < N_KP or kps.shape[1] < 3:
        raise ValueError("expected a (17, 3) keypoint array")
    ls = kps[5]
    rs = kps[6]
    lh = kps[11]
    rh = kps[12]
    neck = np.array([(ls[0] + rs[0]) * 0.5,
                     (ls[1] + rs[1]) * 0.5,
                     min(ls[2], rs[2])], dtype=np.float32)
    pelvis = np.array([(lh[0] + rh[0]) * 0.5,
                       (lh[1] + rh[1]) * 0.5,
                       min(lh[2], rh[2])], dtype=np.float32)
    nose = kps[0]
    head = np.array([nose[0] * 0.45 + neck[0] * 0.55,
                     nose[1] * 0.45 + neck[1] * 0.55,
                     min(nose[2], neck[2])], dtype=np.float32)
    return np.concatenate([kps[:N_KP], neck[None], pelvis[None],
                           head[None]], axis=0)


def midpoint(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    return np.array([(a[0] + b[0]) * 0.5,
                     (a[1] + b[1]) * 0.5,
                     min(a[2], b[2])], dtype=np.float32)


class Pose(object):

    __slots__ = ("kps", "box", "score", "meta")

    def __init__(self, kps, box, score=1.0, meta=None):
        self.kps = np.asarray(kps, dtype=np.float32)
        if self.kps.shape[0] < N_KP:
            raise ValueError("kps must have at least 17 rows")
        self.box = tuple(float(v) for v in box)
        self.score = float(score)
        self.meta = dict(meta or {})

    @property
    def derived(self) -> np.ndarray:
        return derive_joints(self.kps)

    @property
    def xy(self) -> np.ndarray:
        return self.kps[:N_KP, :2]

    @property
    def conf(self) -> np.ndarray:
        return self.kps[:N_KP, 2]

    @property
    def width(self) -> float:
        return max(0.0, self.box[2] - self.box[0])

    @property
    def height(self) -> float:
        return max(0.0, self.box[3] - self.box[1])

    @property
    def center(self):
        return ((self.box[0] + self.box[2]) * 0.5,
                (self.box[1] + self.box[3]) * 0.5)

    @property
    def area(self) -> float:
        return self.width * self.height

    def copy(self) -> "Pose":
        return Pose(self.kps.copy(), self.box, self.score, dict(self.meta))

    def __repr__(self) -> str:
        return "Pose(box=%s, score=%.3f)" % (
            tuple(round(v, 1) for v in self.box), self.score)


def box_iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = ix2 - ix1
    ih = iy2 - iy1
    if iw <= 0.0 or ih <= 0.0:
        return 0.0
    inter = iw * ih
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    if union <= 0.0:
        return 0.0
    return float(inter / union)




def clip_box(box, width, height):
    x1 = min(max(0.0, float(box[0])), float(width))
    y1 = min(max(0.0, float(box[1])), float(height))
    x2 = min(max(0.0, float(box[2])), float(width))
    y2 = min(max(0.0, float(box[3])), float(height))
    if x2 <= x1:
        x2 = min(float(width), x1 + 1.0)
    if y2 <= y1:
        y2 = min(float(height), y1 + 1.0)
    return (x1, y1, x2, y2)
