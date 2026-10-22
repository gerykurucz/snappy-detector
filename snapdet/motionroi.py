"""Motion-gated discovery of targets too small to see.

A 5-px person is at the edge of what any appearance model can score.
But a 5-px person that *moves* is easy: after the camera's own motion
is compensated out, they are one of the few things in the frame that
changed.  This is the wide-area-motion-imagery trick, scaled down:

    downscale -> sparse LK flow -> RANSAC partial affine (pan/zoom/roll)
    -> warp previous frame onto current -> absdiff -> blur/threshold
    -> morphology -> connected components -> candidate ROIs

The ROIs are *candidates*, not detections: each one earns a cheap
zoom-inspection pass (see :mod:`snapdet.inspector`), and only what the
pose model confirms becomes a detection.  On a clean pan this costs
~9 ms per frame and yields a handful of candidates.
"""

from __future__ import annotations

import numpy as np


class MotionAnalyzer(object):
    """Frame-difference ROI finder with global motion compensation."""

    def __init__(self, scale: float = 0.5, threshold: int = 18,
                 min_area: int = 2, max_area: int = 400,
                 max_rois: int = 24, flow_points: int = 250):
        self.scale = float(scale)
        self.threshold = int(threshold)
        self.min_area = int(min_area)
        self.max_area = int(max_area)
        self.max_rois = int(max_rois)
        self.flow_points = int(flow_points)
        self.prev_gray = None
        self.last_mask = None
        self.last_M = None

    def reset(self) -> None:
        self.prev_gray = None
        self.last_mask = None
        self.last_M = None

    def analyze(self, frame_bgr: np.ndarray) -> list:
        """Return ROI dicts for everything that moved since last frame.

        Each ROI: ``{"box": (x1, y1, x2, y2) full-res, "area": px^2 at
        analysis scale, "strength": peak grey difference, "center":
        (cx, cy), "height": full-res px}``.  Sorted strongest first,
        capped at ``max_rois``.
        """
        import cv2
        g = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(g, None, fx=self.scale, fy=self.scale,
                           interpolation=cv2.INTER_AREA)
        out = []
        if self.prev_gray is not None and \
                self.prev_gray.shape == small.shape:
            p, c = self.prev_gray, small
            M = np.float32([[1, 0, 0], [0, 1, 0]])
            pts = cv2.goodFeaturesToTrack(p, self.flow_points, 0.01, 8)
            if pts is not None and len(pts) >= 8:
                p1, st, _ = cv2.calcOpticalFlowPyrLK(p, c, pts, None)
                ok = st.ravel() == 1
                if int(ok.sum()) >= 8:
                    est, _ = cv2.estimateAffinePartial2D(
                        pts[ok], p1[ok], method=cv2.RANSAC,
                        ransacReprojThreshold=2.0)
                    if est is not None:
                        M = est
            self.last_M = M
            h, w = c.shape
            warped = cv2.warpAffine(p, M, (w, h))
            border = np.zeros((h, w), np.uint8)
            b = 6
            border[b:h - b, b:w - b] = 255
            diff = cv2.absdiff(warped, c)
            diff = cv2.GaussianBlur(diff, (5, 5), 0)
            _, th = cv2.threshold(diff, self.threshold, 255,
                                  cv2.THRESH_BINARY)
            th = cv2.bitwise_and(th, border)
            th = cv2.morphologyEx(th, cv2.MORPH_OPEN,
                                  np.ones((2, 2), np.uint8))
            self.last_mask = th
            n, lab, stats, cent = cv2.connectedComponentsWithStats(th)
            for i in range(1, n):
                x, y, bw, bh, area = stats[i]
                if not (self.min_area <= area <= self.max_area):
                    continue
                ys, xs = np.where(lab == i)
                strength = int(diff[ys, xs].max()) if len(ys) else 0
                out.append({
                    "box": (x / self.scale, y / self.scale,
                            (x + bw) / self.scale, (y + bh) / self.scale),
                    "area": int(area),
                    "strength": strength,
                    "center": (cent[i][0] / self.scale,
                               cent[i][1] / self.scale),
                    "height": bh / self.scale,
                })
            out.sort(key=lambda r: -r["strength"])
            out = out[:self.max_rois]
        self.prev_gray = small
        return out
