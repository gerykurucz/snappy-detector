"""Screen-space target geometry and the persistent colour lock.

This module deliberately has no game or detector dependencies.  Everything
that reaches it is already in *real screen pixels*: a detector box, a captured
frame and the frame's virtual-screen origin.  Keeping the conversion here means
the assisted path can never accidentally feed a rendered/upscaled detector
coordinate back into steering or target sizing.

The colour lock is not a general-purpose tracker.  It is a short-lived,
human-seeded identity lock:

* a fast person detector (or the pose fallback) seeds a torso HSV histogram;
* a connected component must match that histogram and keep a plausible body
  size/ratio;
* the lock is used only while it is visible, so no new model detection is run
  for that target;
* when it is lost, the caller starts a new human-first acquisition.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass

import cv2
import numpy as np


# A human is roughly twice as tall as wide; allow perspective/pose tolerance
# up to 1:5.5 while keeping random square/wide blobs out.
HUMANOID_MIN_RATIO = 1.8
HUMANOID_MAX_RATIO = 5.5
HUMANOID_MIN_HEIGHT_PX = 6.0

# Colour tracking is a *supporting* measurement, not a replacement for the
# person class.  It is intentionally conservative around tiny 7-10 px bodies.
COLOR_MIN_COMPONENT_H = 4
COLOR_MIN_COMPONENT_W = 1
COLOR_MIN_FILL = 0.16
COLOR_HIST_CORR_MIN = 0.22
COLOR_HIST_CORR_FALLBACK_MIN = 0.32
COLOR_MAX_SEARCH_PAD = 220
COLOR_LOST_GRACE_S = 0.25

COLOUR_LEAD_MIN_SPEED_PX_S = 18.0
COLOUR_LEAD_MAX_PX = 28.0
COLOUR_LEAD_HORIZON_S = 0.030
COLOUR_LEAD_OUTPUT_DELAY_S = 0.018


def humanoid_ratio(width, height):
    """Return height/width, or ``0.0`` for unusable geometry."""
    try:
        width = float(width)
        height = float(height)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not (math.isfinite(width) and math.isfinite(height)):
        return 0.0
    if width <= 0.0 or height <= 0.0:
        return 0.0
    return height / width


def humanoid_ok(width, height, min_height=HUMANOID_MIN_HEIGHT_PX):
    """Whether a non-model blob can plausibly be an upright human.

    This is the single gate used everywhere a classical/colour candidate can
    become a target.  Model person boxes and pose results are exempt because
    their class/skeleton is the evidence.
    """
    ratio = humanoid_ratio(width, height)
    return (ratio >= HUMANOID_MIN_RATIO
            and ratio <= HUMANOID_MAX_RATIO
            and float(height) >= float(min_height))


@dataclass
class ColorSample:
    """A colour-lock measurement in absolute screen coordinates."""

    x: float
    y: float
    w: float
    h: float
    conf: float
    seq: int
    observed_at: float
    track_id: int = 0
    source: str = "color"
    predicted: bool = False
    quality: float = 0.0
    human_confirmed: bool = True

    @property
    def area(self):
        return max(1.0, float(self.w) * float(self.h))

    @property
    def box(self):
        return (self.x - self.w * 0.5, self.y - self.h * 0.5,
                float(self.w), float(self.h))


class ColorTracker:
    """Persistent HSV lock for one already-detected person."""

    def __init__(self, max_misses=12, min_hits=2):
        self.max_misses = max(1, int(max_misses))
        self.min_hits = max(1, int(min_hits))
        self.hist = None
        self.hue_center = 0.0
        self.sat_center = 0.0
        self.val_center = 0.0
        self.human_confirmed = False
        self.track_id = 0
        self.last_center = (0.0, 0.0)
        self.last_size = (1.0, 1.0)
        self.last_seen = 0.0
        self.misses = 0
        self.seq = 0
        self._pending = None
        self._pending_hits = 0
        self._history = deque(maxlen=6)

    @property
    def seeded(self):
        return self.hist is not None

    @property
    def visible(self):
        return (self.seeded
                and (time.perf_counter() - self.last_seen) <= COLOR_LOST_GRACE_S)

    def reset(self):
        self.__init__(max_misses=self.max_misses, min_hits=self.min_hits)

    def _clamp_box(self, frame, box):
        h, w = frame.shape[:2]
        try:
            x, y, bw, bh = (float(v) for v in box)
        except (TypeError, ValueError, OverflowError):
            return None
        if bw <= 0.0 or bh <= 0.0:
            return None
        x0 = max(0, min(w - 1, int(math.floor(x))))
        y0 = max(0, min(h - 1, int(math.floor(y))))
        x1 = max(x0 + 1, min(w, int(math.ceil(x + bw))))
        y1 = max(y0 + 1, min(h, int(math.ceil(y + bh))))
        if x1 - x0 < 1 or y1 - y0 < 2:
            return None
        return x0, y0, x1, y1

    @staticmethod
    def _torso_rect(x0, y0, x1, y1):
        bw = x1 - x0
        bh = y1 - y0
        if bw < 1 or bh < 2:
            return None
        tx0 = x0 + int(round(0.18 * bw))
        tx1 = x1 - int(round(0.18 * bw))
        ty0 = y0 + int(round(0.12 * bh))
        ty1 = y0 + int(round(0.70 * bh))
        tx1 = max(tx0 + 1, min(x1, tx1))
        ty1 = max(ty0 + 1, min(y1, ty1))
        return tx0, ty0, tx1, ty1

    def _histogram(self, frame, box):
        local = self._clamp_box(frame, box)
        if local is None:
            return None
        x0, y0, x1, y1 = local
        bw = x1 - x0
        bh = y1 - y0
        # Tiny targets (roughly the 7-10 px human floor) have no meaningful
        # torso sub-patch; sample the whole box so the seed histogram survives
        # at small size instead of failing and pushing the caller back to
        # classical blobs.
        use_whole = min(bw, bh) < 14
        if use_whole:
            tx0, ty0, tx1, ty1 = x0, y0, x1, y1
        else:
            torso = self._torso_rect(x0, y0, x1, y1)
            if torso is None:
                return None
            tx0, ty0, tx1, ty1 = torso
        patch = frame[ty0:ty1, tx0:tx1]
        if patch.size == 0:
            return None
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        mask = ((hsv[..., 1] >= 28) & (hsv[..., 2] >= 24)).astype(np.uint8)
        # A tiny patch has few saturated pixels; don't throw the whole seed
        # away when only a handful of pixels qualify.
        need = max(3, (patch.shape[0] * patch.shape[1]) // 30)
        if int(mask.sum()) < need:
            mask = np.ones(hsv.shape[:2], dtype=np.uint8)
        hist = cv2.calcHist([hsv], [0, 1], mask, [24, 12],
                            [0, 180, 0, 256])
        cv2.normalize(hist, hist, alpha=1.0, beta=0.0,
                      norm_type=cv2.NORM_L1)
        pixels = hsv[mask.astype(bool)]
        if pixels.size == 0:
            return None
        self.hue_center = float(np.median(pixels[:, 0]))
        self.sat_center = float(np.median(pixels[:, 1]))
        self.val_center = float(np.median(pixels[:, 2]))
        return hist

    def seed(self, frame, origin, box, now=None, model_confirmed=True,
             track_id=0):
        """Seed at an absolute screen box.

        ``model_confirmed=False`` applies the 1:2-1:5 humanoid gate to the
        seed itself, so a colour-only fallback cannot start on a bullet hole,
        muzzle flash or HUD dot.
        """
        if frame is None:
            return False
        try:
            ox, oy = int(origin[0]), int(origin[1])
        except (TypeError, ValueError, IndexError):
            return False
        try:
            ax, ay, aw, ah = (float(v) for v in box)
        except (TypeError, ValueError, OverflowError):
            return False
        if aw <= 0.0 or ah <= 0.0:
            return False
        if not model_confirmed and not humanoid_ok(aw, ah):
            return False
        local_box = (ax - ox, ay - oy, aw, ah)
        hist = self._histogram(frame, local_box)
        if hist is None:
            return False
        self.hist = hist
        self.human_confirmed = bool(model_confirmed)
        self.track_id = int(track_id or 0)
        self.misses = 0
        self._pending = None
        self._pending_hits = 0
        self.seq = 0
        now = time.perf_counter() if now is None else float(now)
        self.last_center = (ax + aw * 0.5, ay + ah * 0.5)
        self.last_size = (aw, ah)
        self.last_seen = now
        self._history.clear()
        self._history.append((now, self.last_center[0],
                              self.last_center[1], aw, ah))
        return True

    def _predicted_center(self, now):
        if len(self._history) < 2:
            return self.last_center
        t1, x1, y1, _w1, _h1 = self._history[-1]
        t0, x0, y0, _w0, _h0 = self._history[-2]
        dt = float(t1) - float(t0)
        if dt <= 0.0 or dt > 0.12:
            return self.last_center
        lead_t = min(0.025, max(0.0, float(now) - float(t1)))
        return (float(x1) + (float(x1) - float(x0)) / dt * lead_t,
                float(y1) + (float(y1) - float(y0)) / dt * lead_t)

    def _candidate_ok(self, frame, origin, comp, last_size,
                      model_confirmed):
        x, y, w, h, area = comp
        if w < COLOR_MIN_COMPONENT_W or h < COLOR_MIN_COMPONENT_H:
            return None
        if not model_confirmed and not humanoid_ok(w, h):
            return None
        fill = float(area) / float(max(1, w * h))
        if fill < COLOR_MIN_FILL:
            return None
        if last_size is not None:
            lw, lh = last_size
            if lw > 0 and lh > 0:
                wr = float(w) / float(lw)
                hr = float(h) / float(lh)
                if not (0.30 <= wr <= 2.60 and 0.40 <= hr <= 2.40):
                    return None
        # Compare the candidate's torso-ish patch with the seed histogram.
        ox, oy = origin
        candidate_box = (ox + x, oy + y, w, h)
        cand_hist = self._histogram(frame, candidate_box)
        if cand_hist is None:
            return None
        corr = float(cv2.compareHist(self.hist, cand_hist,
                                     cv2.HISTCMP_CORREL))
        floor = (COLOR_HIST_CORR_MIN if model_confirmed
                 else COLOR_HIST_CORR_FALLBACK_MIN)
        if not math.isfinite(corr) or corr < floor:
            return None
        return float(corr), fill

    def _miss(self):
        """Record a missed frame and clear the lock after too many misses."""
        self.misses += 1
        self._pending = None
        self._pending_hits = 0
        if self.misses > self.max_misses:
            self.hist = None
            self.last_seen = 0.0
        return None

    def update(self, frame, origin, now=None, search_pad=None):
        """Return the current absolute colour measurement, or ``None``.

        A miss never returns the previous box: steering must not chase a stale
        measurement.  The caller decides whether to re-run human detection.
        """
        if self.hist is None or frame is None:
            return None
        now = time.perf_counter() if now is None else float(now)
        h, w = frame.shape[:2]
        pcx, pcy = self._predicted_center(now)
        ox, oy = int(origin[0]), int(origin[1])
        lcx = int(round(pcx - ox))
        lcy = int(round(pcy - oy))
        try:
            pad = int(search_pad) if search_pad is not None else int(
                max(40, min(COLOR_MAX_SEARCH_PAD,
                            2.5 * max(self.last_size) + 20)))
        except (TypeError, ValueError, OverflowError):
            pad = 48
        pad = max(8, min(COLOR_MAX_SEARCH_PAD, pad))
        x0 = max(0, lcx - pad)
        y0 = max(0, lcy - pad)
        x1 = min(w, lcx + pad + 1)
        y1 = min(h, lcy + pad + 1)
        if x1 - x0 < 3 or y1 - y0 < 4:
            return self._miss()
        roi = frame[y0:y1, x0:x1]
        if roi.size == 0:
            return self._miss()
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        back = cv2.calcBackProject([hsv], [0, 1], self.hist,
                                   [0, 180, 0, 256], 1.0)
        peak = float(back.max()) if back.size else 0.0
        if peak <= 0.0:
            return self._miss()
        # Back-projection values are histogram densities, not fixed 0-255
        # scores; threshold relative to the peak so the same seed works across
        # colours and lighting.
        mask = ((back > peak * 0.25).astype(np.uint8)) * 255
        # Close one-pixel holes from compression; keep the original for
        # very small bodies (opening would erase them).
        if min(mask.shape) >= 8:
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                                    np.ones((3, 3), np.uint8))
        if int(np.count_nonzero(mask)) < 4:
            return self._miss()
        n, _labels, stats, cents = cv2.connectedComponentsWithStats(
            mask, connectivity=8)
        best = None
        best_key = None
        last_size = self.last_size if self.human_confirmed else None
        for i in range(1, n):
            x, y, bw, bh, area = (int(v) for v in stats[i])
            if area <= 0:
                continue
            comp = (x, y, bw, bh, area)
            checked = self._candidate_ok(roi, (ox + x0, oy + y0),
                                         comp, last_size,
                                         self.human_confirmed)
            if checked is None:
                continue
            corr, fill = checked
            ccx, ccy = cents[i]
            gx = ox + x0 + float(ccx)
            gy = oy + y0 + float(ccy)
            dist = math.hypot(gx - pcx, gy - pcy)
            if dist > pad + 2.0:
                continue
            # Prefer a strong colour match, solid ink and proximity; the last
            # term prevents a same-coloured distant object winning.
            score = (corr * 0.62 + fill * 0.23
                     + 0.15 * (1.0 - min(1.0, dist / max(1.0, pad))))
            key = (-score, dist)
            if best_key is None or key < best_key:
                best_key = key
                best = (gx, gy, float(bw), float(bh), corr, i)
        if best is None:
            return self._miss()

        gx, gy, bw, bh, corr, _idx = best
        if not self.human_confirmed:
            # A colour-only candidate must prove temporal persistence before
            # it can become a target.  A single bullet/muzzle-flash frame is
            # never enough.
            if self._pending is not None:
                px, py, pw, ph = self._pending
                if (math.hypot(gx - px, gy - py)
                        <= max(6.0, 0.35 * max(pw, ph))) \
                        and 0.45 <= bh / max(1.0, ph) <= 2.2:
                    self._pending_hits += 1
                else:
                    self._pending_hits = 1
            else:
                self._pending_hits = 1
            self._pending = (gx, gy, bw, bh)
            if self._pending_hits < self.min_hits:
                self.misses = 0
                return None

        self.last_center = (gx, gy)
        self.last_size = (max(1.0, bw), max(1.0, bh))
        self.last_seen = now
        self.misses = 0
        self._pending = None
        self._pending_hits = 0
        self.seq += 1
        self._history.append((now, gx, gy, self.last_size[0],
                              self.last_size[1]))
        return ColorSample(
            x=gx, y=gy, w=self.last_size[0], h=self.last_size[1],
            conf=min(1.0, max(0.0, corr)), seq=self.seq,
            observed_at=now, track_id=self.track_id,
            source="color", predicted=False, quality=corr,
            human_confirmed=self.human_confirmed)

    def search_box(self, pad=None):
        """Absolute capture box around the lock, or ``None``."""
        if self.hist is None:
            return None
        try:
            pad = int(pad) if pad is not None else int(
                max(24, min(COLOR_MAX_SEARCH_PAD,
                            1.9 * max(self.last_size) + 12)))
        except (TypeError, ValueError, OverflowError):
            pad = 48
        cx, cy = self.last_center
        return (int(round(cx - pad)), int(round(cy - pad)),
                int(round(2 * pad + 1)), int(round(2 * pad + 1)))

    def lead(self, now=None, latency_s=None):
        """Translation-only lead from the last 2-3 colour centres.

        Stationary, reversing, body-animation-jitter and size-changing
        (approach/turn) tracks return zero.  The caller may add measured
        detector latency; the colour path itself is current.
        """
        if len(self._history) < 3:
            return 0.0, 0.0
        now = time.perf_counter() if now is None else float(now)
        points = [p for p in self._history
                  if now - 0.16 <= float(p[0]) <= now + 0.02]
        if len(points) < 3:
            return 0.0, 0.0
        # Fit the latest 3 samples; short history avoids carrying a body turn
        # through a direction change.
        pts = points[-3:]
        t0, x0, y0, w0, h0 = pts[0]
        t2, x2, y2, w2, h2 = pts[-1]
        dt = float(t2) - float(t0)
        if dt <= 0.008 or dt > 0.14:
            return 0.0, 0.0
        vx = (float(x2) - float(x0)) / dt
        vy = (float(y2) - float(y0)) / dt
        speed = math.hypot(vx, vy)
        if speed < COLOUR_LEAD_MIN_SPEED_PX_S:
            return 0.0, 0.0
        path = 0.0
        for first, second in zip(points, points[1:]):
            path += math.hypot(float(second[1]) - float(first[1]),
                               float(second[2]) - float(first[2]))
        displacement = math.hypot(float(points[-1][1]) - float(points[0][1]),
                                  float(points[-1][2]) - float(points[0][2]))
        if path <= 2.0 or displacement / max(path, 1e-6) < 0.55:
            return 0.0, 0.0
        # A large width/height change is the body turning, crouching or
        # approaching faster than lateral translation; leading that is unsafe.
        if h0 > 0 and not (0.72 <= float(h2) / float(h0) <= 1.38):
            return 0.0, 0.0
        if w0 > 0 and not (0.60 <= float(w2) / float(w0) <= 1.65):
            return 0.0, 0.0
        horizon = COLOUR_LEAD_HORIZON_S + COLOUR_LEAD_OUTPUT_DELAY_S
        if latency_s is not None:
            try:
                horizon += max(0.0, min(0.06, float(latency_s)))
            except (TypeError, ValueError, OverflowError):
                pass
        ox = vx * horizon
        oy = vy * horizon
        # Multiply the leading end result by 1.5 so the aim runs slightly
        # ahead of the measured translation.
        ox *= 1.5
        oy *= 1.5
        # Height-based vertical boost: a taller target needs more lead.
        # 20 px height -> 10 px of additional Y lead, i.e. height / 2.
        try:
            height = float(self.last_size[1])
        except (TypeError, ValueError, OverflowError):
            height = 0.0
        oy += height * 0.5
        mag = math.hypot(ox, oy)
        if mag > COLOUR_LEAD_MAX_PX:
            scale = COLOUR_LEAD_MAX_PX / mag
            ox *= scale
            oy *= scale
        if abs(ox) + abs(oy) < 0.75:
            return 0.0, 0.0
        return ox, oy
