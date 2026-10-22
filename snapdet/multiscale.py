"""The amortised zoom sweep and cross-source detection merging.

One full-frame pass downscales everything; a 1080p frame squeezed into
640x640 turns a 96-px person into 32 px and a 10-px person into
nothing.  The sweep looks at the scene at *native pixels* through a
moving window: a grid of small tiles, each upscaled to the network
input, walked round-robin a few tiles per frame.  Every pixel of the
frame is therefore re-examined at high zoom every few seconds, which is
what makes "all people, even stationary tiny ones" a promise the
system can keep - the tracker holds identities in between.
"""

from __future__ import annotations

import math

import numpy as np


def window_starts(n: int, win: int, stride: int) -> list:
    """Tile origins covering ``n`` pixels with window ``win``."""
    if n <= win:
        return [0]
    pts = list(range(0, n - win + 1, stride))
    if pts[-1] != n - win:
        pts.append(n - win)
    return pts


class _Grid(object):
    def __init__(self, zoom: int, tiles_per_frame: int, overlap: float,
                 w: int, h: int):
        self.zoom = int(zoom)
        self.k = max(1, int(tiles_per_frame))
        self.win = int(round(640.0 / self.zoom))
        stride = max(1, int(round(self.win * (1.0 - overlap))))
        xs = window_starts(w, self.win, stride)
        ys = window_starts(h, self.win, stride)
        self.tiles = [(x, y) for y in ys for x in xs]
        self.idx = 0

    def __len__(self):
        return len(self.tiles)

    def next(self):
        if not self.tiles:
            return None
        t = self.tiles[self.idx % len(self.tiles)]
        self.idx += 1
        return (t[0], t[1], self.win, self.zoom)


class SweepScheduler(object):
    """Round-robin walker over one or more zoom grids."""

    def __init__(self, zooms=((8, 3),), overlap: float = 0.25):
        self.zoom_budgets = [(int(z), int(k)) for z, k in zooms]
        self.overlap = float(overlap)
        self.size = None
        self.grids = []

    def configure(self, w: int, h: int) -> None:
        if self.size == (w, h) and self.grids:
            return
        self.size = (w, h)
        self.grids = [_Grid(z, k, self.overlap, w, h)
                      for z, k in self.zoom_budgets]

    def next_tiles(self) -> list:
        out = []
        for g in self.grids:
            for _ in range(g.k):
                t = g.next()
                if t is not None:
                    out.append(t)
        return out

    def sweep_once(self) -> list:
        """Every tile of every grid, for still-image mode."""
        out = []
        for g in self.grids:
            for i in range(len(g.tiles)):
                t = g.next()
                if t is not None:
                    out.append(t)
        return out

    def progress(self) -> str:
        parts = []
        for g in self.grids:
            n = max(1, len(g.tiles))
            parts.append("z%d:%d/%d" % (g.zoom, g.idx % n, n))
        return " ".join(parts)

    def tiles_per_frame(self) -> int:
        return sum(g.k for g in self.grids)


def merge_detections(dets: list, iou_thr: float = 0.35,
                     dedup_rel: float = 0.7, dedup_min: float = 8.0) -> list:
    """Fuse detections from every source into one list.

    The same tiny person is often seen by two overlapping tiles with a
    one-pixel difference; plain NMS at IoU 0.35 keeps both, because two
    6x10 boxes offset by 2 px barely overlap.  A centre-distance rule
    relative to the target's own size catches what IoU cannot - which
    matters exactly where the boxes are smallest.
    """
    order = sorted(range(len(dets)), key=lambda i: -float(dets[i]["score"]))
    kept = []
    for i in order:
        d = dets[i]
        x1, y1, x2, y2 = d["box"]
        cx, cy = (x1 + x2) * 0.5, (y1 + y2) * 0.5
        span = math.sqrt(max(1.0, (x2 - x1) * (y2 - y1)))
        dup = False
        for k in kept:
            kx1, ky1, kx2, ky2 = k["box"]
            # IoU test
            ix = max(0.0, min(x2, kx2) - max(x1, kx1))
            iy = max(0.0, min(y2, ky2) - max(y1, ky1))
            inter = ix * iy
            ua = ((x2 - x1) * (y2 - y1) + (kx2 - kx1) * (ky2 - ky1) - inter)
            if ua > 0 and inter / ua >= iou_thr:
                dup = True
                break
            # centre distance test
            kcx, kcy = (kx1 + kx2) * 0.5, (ky1 + ky2) * 0.5
            kspan = math.sqrt(max(1.0, (kx2 - kx1) * (ky2 - ky1)))
            if math.hypot(cx - kcx, cy - kcy) <= max(dedup_min,
                                                     dedup_rel * min(span, kspan)):
                dup = True
                break
        if not dup:
            kept.append(d)
    return kept
