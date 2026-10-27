
from __future__ import annotations

import math

import numpy as np

STATIC = "static"
SLOW = "slow"
FAST = "fast"

FAST_SPEED = 260.0
SLOW_SPEED = 60.0


class KalmanBox(object):

    __slots__ = ("x", "p", "last_t", "accel")

    def __init__(self, box, t: float = 0.0, accel: float = 1.0,
                 meas_var: float = 12.0):
        self.x = np.zeros(6, dtype=np.float64)
        self.x[0] = (box[0] + box[2]) * 0.5
        self.x[1] = (box[1] + box[3]) * 0.5
        self.x[2] = math.log(max(1.0, box[2] - box[0]))
        self.x[3] = math.log(max(1.0, box[3] - box[1]))
        self.p = np.diag([meas_var, meas_var, 0.25, 0.25, 400.0, 400.0])
        self.last_t = float(t)
        self.accel = float(accel)

    def predict(self, t: float) -> np.ndarray:
        dt = max(1e-3, float(t) - self.last_t)
        self.last_t = float(t)
        f = np.eye(6)
        f[0, 4] = dt
        f[1, 5] = dt
        self.x = f @ self.x
        q = np.diag([0.35, 0.35, 0.02, 0.02,
                     120.0 * self.accel, 120.0 * self.accel]) * dt
        self.p = f @ self.p @ f.T + q
        return self.x.copy()

    def update(self, box, t: float = None) -> None:
        if t is not None and float(t) > self.last_t + 1e-9:
            self.predict(float(t))
        cx = (box[0] + box[2]) * 0.5
        cy = (box[1] + box[3]) * 0.5
        w = math.log(max(1.0, box[2] - box[0]))
        h = math.log(max(1.0, box[3] - box[1]))
        z = np.array([cx, cy, w, h], dtype=np.float64)
        hmat = np.zeros((4, 6), dtype=np.float64)
        hmat[0, 0] = 1.0
        hmat[1, 1] = 1.0
        hmat[2, 2] = 1.0
        hmat[3, 3] = 1.0
        r = np.eye(4) * 9.0
        y = z - hmat @ self.x
        s = hmat @ self.p @ hmat.T + r
        try:
            k = np.linalg.solve(s.T, (self.p @ hmat.T).T).T
        except np.linalg.LinAlgError:
            return
        self.x = self.x + k @ y
        eye = np.eye(6)
        self.p = (eye - k @ hmat) @ self.p

    @property
    def velocity(self):
        return float(self.x[4]), float(self.x[5])

    @property
    def speed(self) -> float:
        vx, vy = self.velocity
        return math.hypot(vx, vy)

    @property
    def size(self):
        return float(math.exp(self.x[2])), float(math.exp(self.x[3]))

    def box(self, offset=(0.0, 0.0)):
        w, h = self.size
        cx = self.x[0] + offset[0]
        cy = self.x[1] + offset[1]
        return (cx - w * 0.5, cy - h * 0.5, cx + w * 0.5, cy + h * 0.5)


class MotionModel(object):

    def __init__(self, smoothing: float = 0.45, max_history: int = 90):
        self.smoothing = float(smoothing)
        self.max_history = int(max_history)
        self.samples = []
        self.vel = (0.0, 0.0)
        self.accel = (0.0, 0.0)
        self.vel_raw = (0.0, 0.0)

    def reset(self) -> None:
        self.samples = []
        self.vel = (0.0, 0.0)
        self.accel = (0.0, 0.0)
        self.vel_raw = (0.0, 0.0)

    def add(self, t: float, x: float, y: float) -> None:
        if self.samples:
            t_prev, x_prev, y_prev = self.samples[-1]
            dt = float(t) - t_prev
            if dt > 1e-6:
                self.vel_raw = ((x - x_prev) / dt, (y - y_prev) / dt)
        self.samples.append((float(t), float(x), float(y)))
        if len(self.samples) > self.max_history:
            del self.samples[0]
        self._recompute()

    def _recompute(self) -> None:
        if len(self.samples) < 2:
            self.vel = (0.0, 0.0)
            self.accel = (0.0, 0.0)
            return
        t0, x0, y0 = self.samples[0]
        t1, x1, y1 = self.samples[-1]
        dt = t1 - t0
        if dt < 1e-6:
            self.vel = (0.0, 0.0)
            return
        vx = (x1 - x0) / dt
        vy = (y1 - y0) / dt
        a = self.smoothing
        self.vel = (a * vx + (1.0 - a) * self.vel[0],
                    a * vy + (1.0 - a) * self.vel[1])
        if len(self.samples) >= 3:
            tm, xm, ym = self.samples[len(self.samples) // 2]
            dtm = tm - t0
            if dtm > 1e-6:
                vxm = (xm - x0) / dtm
                vym = (ym - y0) / dtm
                ax = (vx - vxm) / max(1e-6, (t1 - tm))
                ay = (vy - vym) / max(1e-6, (t1 - tm))
                self.accel = (0.6 * ax + 0.4 * self.accel[0],
                              0.6 * ay + 0.4 * self.accel[1])

    @property
    def speed(self) -> float:
        return math.hypot(self.vel[0], self.vel[1])

    def state(self) -> str:
        s = self.speed
        if s >= FAST_SPEED:
            return FAST
        if s >= SLOW_SPEED:
            return SLOW
        return STATIC

    def direction(self):
        s = self.speed
        if s < 1e-6:
            return None
        return (self.vel[0] / s, self.vel[1] / s)

    def predict(self, seconds: float, use_acceleration: bool = True):
        t = max(0.0, float(seconds))
        ox = self.vel[0] * t
        oy = self.vel[1] * t
        if use_acceleration and t > 0.0:
            ox += 0.5 * self.accel[0] * t * t
            oy += 0.5 * self.accel[1] * t * t
        if self.samples:
            x = self.samples[-1][1] + ox
            y = self.samples[-1][2] + oy
        else:
            x = ox
            y = oy
        return x, y, ox, oy

