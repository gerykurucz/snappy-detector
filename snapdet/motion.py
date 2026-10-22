"""Motion: where a target is and where it is going.

Two things live here.

:class:`KalmanBox` is a small constant-velocity filter over a target's
box.  It smooths the jitter that comes with a detector and gives a
velocity that is worth acting on, rather than the difference between
two noisy boxes.

:class:`MotionModel` turns that history into the answers a caller
actually wants: how fast the target is moving, whether it is moving at
all, and where it will be after a given delay - which is what makes a
lead aim land on a moving target instead of where it used to be.
"""

from __future__ import annotations

import math

import numpy as np

#: Motion states a target can be in.
STATIC = "static"
SLOW = "slow"
FAST = "fast"

#: Pixel per second above which a target counts as moving fast.
FAST_SPEED = 260.0
SLOW_SPEED = 60.0


class KalmanBox(object):
    """Constant-velocity filter over ``(cx, cy, w, h)``.

    The state is ``[cx, cy, log w, log h, vx, vy]``: the centre and
    the size of the box, plus how fast both are moving.  The size is
    kept as a logarithm so that a box can never turn inside out.
    """

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
        """Correct the state with a measurement.

        When time has moved on since the last call the state is
        carried forward first, so the filter can be used on its own as
        well as under the tracker.
        """
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

    # -- accessors ----------------------------------------------------
    @property
    def velocity(self):
        """Velocity of the box centre in pixels per second."""
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

    def predict_box(self, seconds: float):
        """Where the box will be after ``seconds``."""
        w, h = self.size
        cx = self.x[0] + self.x[4] * seconds
        cy = self.x[1] + self.x[5] * seconds
        return (cx - w * 0.5, cy - h * 0.5, cx + w * 0.5, cy + h * 0.5)


class MotionModel(object):
    """Velocity, acceleration and lead prediction for one target."""

    def __init__(self, smoothing: float = 0.45, max_history: int = 90):
        self.smoothing = float(smoothing)
        self.max_history = int(max_history)
        self.samples = []          # (t, x, y)
        self.vel = (0.0, 0.0)
        self.accel = (0.0, 0.0)

    def reset(self) -> None:
        self.samples = []
        self.vel = (0.0, 0.0)
        self.accel = (0.0, 0.0)

    def add(self, t: float, x: float, y: float) -> None:
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
        # blend with the previous estimate so a single bad frame does
        # not swing the aim
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

    # -- queries -------------------------------------------------------
    @property
    def speed(self) -> float:
        return math.hypot(self.vel[0], self.vel[1])

    def state(self) -> str:
        """``static``, ``slow`` or ``fast``."""
        s = self.speed
        if s >= FAST_SPEED:
            return FAST
        if s >= SLOW_SPEED:
            return SLOW
        return STATIC

    def is_moving(self, threshold: float = SLOW_SPEED) -> bool:
        return self.speed >= threshold

    def direction(self):
        """Unit vector along the motion, or ``None`` when still."""
        s = self.speed
        if s < 1e-6:
            return None
        return (self.vel[0] / s, self.vel[1] / s)

    def predict(self, seconds: float, use_acceleration: bool = True):
        """Where the target will be after ``seconds``.

        Returns ``(x, y, offset_x, offset_y)`` where the offset is
        relative to where the target is now.
        """
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

    def lead_for(self, x: float, y: float, seconds: float,
                 use_acceleration: bool = True):
        """The point to aim at to meet a target moving from ``(x, y)``."""
        px, py, _, _ = self.predict(seconds, use_acceleration)
        return px, py
