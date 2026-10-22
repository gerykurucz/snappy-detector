"""Zoom inspection: looking hard at one small window.

The pose model wants a person to be roughly 100-200 px tall in its
640x640 input to produce a confident score - measurements on real
photographs put an 8-px person at ~0.4 through a 12-16x window and a
24-px person at ~0.8 through 8x, while empty windows never scored
above ~0.09 at any zoom.  So the useful zoom is a function of the
target size:

    zoom ~= target_px / estimated_height, clamped

Inspection is the expensive step (one network pass each), which is why
it is *gated*: only where a motion blob or a weak sweep tile said
"there might be something" does a window get looked at, and the budget
in :class:`~snapdet.config.Config` caps how many per frame.
"""

from __future__ import annotations

import numpy as np


def _payload(pose, x1: int, y1: int, zoom: float, source: str) -> dict:
    """Map a detection made on an upscaled window back to frame pixels."""
    b = pose.box
    kps = pose.kps.copy()
    box = (b[0] / zoom + x1, b[1] / zoom + y1,
           b[2] / zoom + x1, b[3] / zoom + y1)
    kps[:, 0] = kps[:, 0] / zoom + x1
    kps[:, 1] = kps[:, 1] / zoom + y1
    return {"box": box, "score": float(pose.score), "kps": kps,
            "source": source, "zoom": zoom}


class Inspector(object):
    """Runs the pose model on small, highly-zoomed windows."""

    def __init__(self, pose_detector, cfg):
        self.pose = pose_detector
        self.cfg = cfg

    # -- the primitive ------------------------------------------------
    def run_window(self, frame, x1: int, y1: int, win: int, zoom: float,
                   thr: float, source: str) -> list:
        """One network pass over a ``win``x``win`` window.

        Returns detection payloads above ``thr`` in frame coordinates.
        """
        import cv2
        H, W = frame.shape[:2]
        x1 = int(np.clip(x1, 0, max(0, W - win)))
        y1 = int(np.clip(y1, 0, max(0, H - win)))
        crop = frame[y1:y1 + win, x1:x1 + win]
        if crop.shape[0] < 8 or crop.shape[1] < 8:
            return []
        up = cv2.resize(crop, (640, 640), interpolation=cv2.INTER_CUBIC)
        out = []
        for p in self.pose.detect(up):
            if p.score >= thr:
                out.append(_payload(p, x1, y1, zoom, source))
        return out

    # -- adaptive inspection -------------------------------------------
    def inspect_point(self, frame, cx: float, cy: float,
                      est_h: float) -> tuple:
        """Inspect the neighbourhood of a point.

        ``est_h`` is the guess for the target's height in frame pixels
        (a motion blob gives one).  Returns ``(payload_or_None, info)``.
        ``info`` carries everything the temporal memory and the debug
        overlay need: what was tried, the best score, and the best
        *size-plausible* score - a detection whose box is five times
        the blob that summoned it is the model grabbing background,
        not the target, and must not be allowed to confirm anything.
        """
        cfg = self.cfg
        est_h = max(2.0, float(est_h or 8.0))
        zoom = int(round(cfg.inspect_target_px / est_h))
        zoom = int(np.clip(zoom, cfg.inspect_zoom_min, cfg.inspect_zoom_max))
        lo, hi = 0.3 * est_h, 3.0 * est_h + 4.0
        best, best_score = None, 0.0
        best_pl, best_pl_score = None, 0.0
        tries = []
        for attempt in (1, 2):
            win = int(round(640.0 / zoom))
            x1 = int(round(cx - win / 2.0))
            y1 = int(round(cy - win / 2.0))
            dets = self.run_window(frame, x1, y1, win, zoom,
                                   cfg.conf_floor, "inspect")
            near = []
            radius = max(10.0, est_h * 1.6, win * 0.35)
            radius = min(radius, win * 0.6 + 4.0)
            for d in dets:
                bx = (d["box"][0] + d["box"][2]) * 0.5
                by = (d["box"][1] + d["box"][3]) * 0.5
                if abs(bx - cx) <= radius and abs(by - cy) <= radius:
                    dh = d["box"][3] - d["box"][1]
                    d["plausible"] = bool(lo <= dh <= hi)
                    near.append(d)
            s = max((d["score"] for d in near), default=0.0)
            tries.append((zoom, round(s, 3)))
            for d in near:
                if d["plausible"] and d["score"] > best_pl_score:
                    best_pl, best_pl_score = d, d["score"]
                if d["score"] > best_score:
                    best, best_score = d, d["score"]
            if best_pl_score >= cfg.conf_inspect:
                break
            if not cfg.inspect_escalate or attempt == 2:
                break
            nxt = min(cfg.inspect_zoom_max, int(round(zoom * 1.5)))
            if nxt == zoom:
                nxt = max(cfg.inspect_zoom_min, int(round(zoom * 0.65)))
            if nxt == zoom:
                break
            zoom = nxt
        passed = best_pl is not None and best_pl_score >= cfg.conf_inspect
        info = {"zoom_tried": tries, "best": best_score,
                "plausible_best": best_pl_score, "passed": passed,
                "est_h": est_h}
        if passed:
            return best_pl, info
        return None, info
