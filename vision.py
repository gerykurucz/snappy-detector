
import functools
import math
import threading
import time

import numpy as np

from system_core import (ACCENT_PRESETS,
                        hsv_to_rgb,
                        parse_accent as _parse_accent,
                        parse_accents as _parse_accents)







_LOG_SINK = None

try:



    from win32rt import get_dpi_scale as _get_dpi_scale
except Exception:
    _get_dpi_scale = None


def set_log_sink(fn):
    global _LOG_SINK
    _LOG_SINK = fn if callable(fn) else None


def _log(text):
    fn = _LOG_SINK
    if fn is None:
        return
    try:
        fn(text)
    except Exception:
        pass

_CH_WEIGHTS = np.array([0.114, 0.587, 0.299], dtype=np.float32)
_SCAN_DEADLINE = threading.local()

CS = 4
CELL_SOLID = 0.28
CELL_WEAK = 0.18
BLUR_WIN = 9
MIN_FILL = 0.45










NORM_C = 24.0
NORM_K = 152.0
NORM_MIN = 0.5
NORM_MAX = 3.0









ADAPTIVE_PERCENTILE = 98.0
ADAPTIVE_K = 0.5
ADAPTIVE_FLOOR = 14.0
ADAPTIVE_BASE = 38.0



MIN_CONTRAST_FLOOR = 28.0







MIN_CONTRAST_BASE = 48.0








CONTRAST_CORE_FRAC = 0.35
CONTRAST_METRIC_SCALE = 0.83


class SnapTarget(object):
    __slots__ = ("dx", "dy", "dist", "area", "contrast", "bw", "bh", "sig",
                 "hscore", "view", "stance", "motion", "lead_x", "lead_y",
                 "speed", "track_id")

    def __init__(self, dx, dy, dist, area, contrast, bw, bh, sig=None,
                 hscore=0.0):
        self.dx = dx
        self.dy = dy
        self.dist = dist
        self.area = area
        self.contrast = contrast
        self.bw = bw
        self.bh = bh
        self.sig = sig
        try:
            self.hscore = float(hscore)
        except (TypeError, ValueError):
            self.hscore = 0.0
        self.view = ""
        self.stance = ""
        self.motion = ""
        self.lead_x = 0.0
        self.lead_y = 0.0
        self.speed = 0.0
        self.track_id = 0


class ScanResult(object):
    __slots__ = ("kind", "target", "ms")

    def __init__(self, kind, target=None, ms=0.0):
        self.kind = kind
        self.target = target
        self.ms = ms


def _box_blur(a, win):
    try:
        import cv2
        return cv2.blur(a, (int(win), int(win)),
                        borderType=cv2.BORDER_REPLICATE)
    except Exception:
        pass
    r = win // 2
    h, w, _ = a.shape
    p = np.pad(a, ((r, r), (r, r), (0, 0)), mode="edge")
    c = p.cumsum(axis=0).cumsum(axis=1)
    c = np.pad(c, ((1, 0), (1, 0), (0, 0)))
    total = (
        c[win:win + h, win:win + w]
        - c[:h, win:win + w]
        - c[win:win + h, :w]
        + c[:h, :w]
    )
    return total / float(win * win)


def _dilate(m, n=1):
    for _ in range(n):
        o = m.copy()
        o[1:, :] |= m[:-1, :]
        o[:-1, :] |= m[1:, :]
        o[:, 1:] |= m[:, :-1]
        o[:, :-1] |= m[:, 1:]
        m = o
    return m


def _erode(m, n=1):






    for _ in range(n):
        o = m.copy()
        o[1:, :] &= m[:-1, :]
        o[:-1, :] &= m[1:, :]
        o[:, 1:] &= m[:, :-1]
        o[:, :-1] &= m[:, 1:]
        o[0, :] = False
        o[-1, :] = False
        o[:, 0] = False
        o[:, -1] = False
        m = o
    return m


def _dilate8(m):
    o = m.copy()
    o[1:, :] |= m[:-1, :]
    o[:-1, :] |= m[1:, :]
    o[:, 1:] |= m[:, :-1]
    o[:, :-1] |= m[:, 1:]
    o[1:, 1:] |= m[:-1, :-1]
    o[1:, :-1] |= m[:-1, 1:]
    o[:-1, 1:] |= m[1:, :-1]
    o[:-1, :-1] |= m[1:, 1:]
    return o


def _close_gaps(m, gap):
    try:
        g = int(float(gap))
    except (TypeError, ValueError):
        return m
    if g <= 0:
        return m
    g = min(g, min(m.shape) // 2 - 1)
    if g <= 0:
        return m
    try:
        import cv2
        se = np.ones((2 * g + 1, 2 * g + 1), np.uint8)
        e = cv2.morphologyEx(m.astype(np.uint8),
                             cv2.MORPH_CLOSE, se).astype(bool)
    except Exception:



        return m



    e[:g, :] = False
    e[-g:, :] = False
    e[:, :g] = False
    e[:, -g:] = False
    return e


def _meanshift(xs, ys, sx, sy, sigma=25.0, iters=8):
    px, py = float(sx), float(sy)
    inv2s2 = 1.0 / (2.0 * sigma * sigma)

    for _ in range(iters):
        w = np.exp(-(((xs - px) ** 2 + (ys - py) ** 2) * inv2s2))
        sw = float(w.sum())
        if sw < 1e-3:
            return None

        nx = float((xs * w).sum() / sw)
        ny = float((ys * w).sum() / sw)
        moved = abs(nx - px) + abs(ny - py)
        px, py = nx, ny

        if moved < 0.4:
            break

    return px, py


def _grow_cells(strong, weak):
    keep = strong.copy()
    for i in range(256):
        grown = _dilate8(keep) & weak
        grown |= keep
        if np.array_equal(grown, keep):
            return keep
        keep = grown
        if (i & 31) == 31 and time.perf_counter() > getattr(_SCAN_DEADLINE, 'deadline', float('inf')):
            return keep
    return keep


def _flood_bg(solids, mask):
    gh, gw = solids.shape
    free = mask & ~solids
    bg = np.zeros((gh, gw), dtype=bool)
    if not free.any():
        return bg

    border = np.zeros((gh, gw), dtype=bool)
    border[0, :] = True
    border[gh - 1, :] = True
    border[:, 0] = True
    border[:, gw - 1] = True
    seed = free & (border | _dilate8(~mask))
    if not seed.any():
        return bg
    bg = seed.copy()

    for i in range(256):
        g = bg.copy()
        g[1:, :] |= bg[:-1, :]
        g[:-1, :] |= bg[1:, :]
        g[:, 1:] |= bg[:, :-1]
        g[:, :-1] |= bg[:, 1:]
        g &= free
        if np.array_equal(g, bg):
            return bg
        bg = g
        if (i & 31) == 31 and time.perf_counter() > getattr(_SCAN_DEADLINE, 'deadline', float('inf')):
            return bg
    return bg


def _label_cells(cells):
    gh, gw = cells.shape
    labels = np.zeros((gh, gw), dtype=np.int32)
    runs_y = []
    runs_x0 = []
    runs_x1 = []
    parent = []

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    prev = []
    for y in range(gh):
        xs = np.flatnonzero(cells[y])
        if xs.size == 0:
            prev = []
            continue
        jumps = np.flatnonzero(np.diff(xs) > 1)
        starts = np.concatenate(([xs[0]], xs[jumps + 1]))
        ends = np.concatenate((xs[jumps] + 1, [xs[-1] + 1]))
        cur = []
        for x0, x1 in zip(starts, ends):
            rid = len(parent)
            parent.append(rid)
            runs_y.append(y)
            runs_x0.append(int(x0))
            runs_x1.append(int(x1))
            cur.append((int(x0), int(x1), rid))
            for px0, px1, prid in prev:
                if px0 <= x1 and x0 <= px1:
                    union(rid, prid)
        prev = cur

    nruns = len(parent)
    if nruns == 0:
        return labels, []
    roots = np.empty(nruns, dtype=np.int32)
    for r in range(nruns):
        roots[r] = find(r)

    order = {}
    for r in range(nruns):
        rt = roots[r]
        if rt not in order:
            order[rt] = len(order) + 1
    comp = np.empty(nruns, dtype=np.int32)
    for r in range(nruns):
        comp[r] = order[roots[r]]
    ncomp = len(order)

    ry = np.asarray(runs_y, dtype=np.int32)
    rx0 = np.asarray(runs_x0, dtype=np.int32)
    rx1 = np.asarray(runs_x1, dtype=np.int32)
    for k in range(nruns):
        labels[ry[k], rx0[k]:rx1[k]] = comp[k]

    lengths = (rx1 - rx0).astype(np.int64)
    counts = np.zeros(ncomp + 1, dtype=np.int64)
    np.add.at(counts, comp, lengths)
    y0 = np.full(ncomp + 1, gh, dtype=np.int32)
    y1 = np.zeros(ncomp + 1, dtype=np.int32)
    x0 = np.full(ncomp + 1, gw, dtype=np.int32)
    x1 = np.zeros(ncomp + 1, dtype=np.int32)
    np.minimum.at(y0, comp, ry)
    np.maximum.at(y1, comp, ry)
    np.minimum.at(x0, comp, rx0)
    np.maximum.at(x1, comp, rx1 - 1)
    info = [(i, int(counts[i]), int(y0[i]), int(x0[i]), int(y1[i]) + 1,
             int(x1[i]) + 1) for i in range(1, ncomp + 1)]
    return labels, info


MIN_FRAG_AREA = 64


















AREA_SCORE_CAP = 6000.0
DIST_FALLOFF = 2.0







PROX_CORE_PX = 24.0
PROX_CORE_BONUS = 0.5


def _prox_mult(dist):
    if dist >= PROX_CORE_PX:
        return 1.0
    return 1.0 + PROX_CORE_BONUS * (1.0 - dist / PROX_CORE_PX)

















SHAPE_LO = 1.2
SHAPE_PLATEAU_LO = 1.6
SHAPE_PLATEAU_HI = 2.4
SHAPE_HI = 3.0
SHAPE_BONUS = 0.6
SHAPE_SQ_LO = 0.75
SHAPE_SQ_HI = 1.33
SHAPE_SQ_BONUS = 0.3
SHAPE_MIN_H = 24
SHAPE_MIN_AREA = 200



SHAPE_TIER_MIN_F = 0.25


FIGURE_TIER2_THRESH = 0.50
FIGURE_TIER1_THRESH = 0.30
FIGURE_BOOST = 1.1
FIGURE_SMALL_H = 16
FIGURE_SMALL_AREA = 120
FIGURE_SYM_LO = 0.45
FIGURE_SYM_HI = 0.70
FIGURE_COVER_LO = 0.55
FIGURE_COVER_HI = 0.78
FIGURE_W_HEAD = 0.34
FIGURE_W_TAPER = 0.20
FIGURE_W_SYM = 0.12
FIGURE_W_COV = 0.17
FIGURE_W_FILL = 0.17
FIGURE_HEAD_OK = 0.62
FIGURE_HEAD_STRONG = 0.68
FIGURE_HEAD_MIN = 0.40
FIGURE_STEP_MIN = 0.10
FIGURE_DIP_MIN = 0.07
FIGURE_RESCUE_RS = 0.55


def _ratio_score(r):
    try:
        r = float(r)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(r) or r <= 0.0:
        return 0.0
    if SHAPE_PLATEAU_LO <= r <= SHAPE_PLATEAU_HI:
        return 1.0
    if SHAPE_LO < r < SHAPE_PLATEAU_LO:
        return (r - SHAPE_LO) / (SHAPE_PLATEAU_LO - SHAPE_LO)
    if SHAPE_PLATEAU_HI < r < 3.2:
        return max(0.0, (3.2 - r) / (3.2 - SHAPE_PLATEAU_HI))
    if 0.85 <= r <= 1.2:
        if r <= 1.0:
            return 0.35 * (r - 0.85) / 0.15
        return 0.35 * (1.2 - r) / 0.2
    return 0.0


def _figure_features(mask):
    try:
        h, w = mask.shape
    except (AttributeError, ValueError):
        return None
    if h < 4 or w < 2:
        return None
    m = mask
    step = max(1, max(h, w) // 120)
    if step > 1:
        m = mask[::step, ::step]
        h, w = m.shape
        if h < 4 or w < 2:
            return None
    try:
        area = int(m.sum())
    except Exception:
        return None
    fill = area / float(h * w) if h * w else 0.0
    row_fill = m.mean(axis=1)
    coverage = float((row_fill > 0.15).mean()) if h else 0.0
    w2 = w // 2
    sym = 0.0
    try:
        if w2 >= 1:
            left = m[:, :w2]
            right = m[:, -w2:][:, ::-1]
            union = int((left | right).sum())
            inter = int((left & right).sum())
            sym = (inter / float(union)) if union > 0 else 0.0
    except Exception:
        sym = 0.0
    def _band_width(y0f, y1f):
        y0 = int(h * y0f)
        y1 = max(y0 + 1, int(h * y1f))
        band = m[y0:y1]
        if band.size == 0:
            return 0
        cols = (band.mean(axis=0) > 0.20).sum()
        return int(cols)
    top_w = _band_width(0.0, 0.20)
    mid_w = _band_width(0.35, 0.65)
    bot_w = _band_width(0.80, 1.0)
    top_narrow = bool(mid_w >= 3 and top_w >= 1
                      and mid_w > top_w * 1.12)
    taper = 0.0
    try:
        if top_narrow and bot_w <= max(2, mid_w * 1.25):
            taper = 1.0
        elif top_narrow:
            taper = 0.6
        elif mid_w >= max(3, w * 0.35) and top_w <= max(1, mid_w * 0.95):
            taper = 0.4
    except Exception:
        taper = 0.0
    leg_split = False
    try:
        probe_y = int(h * 0.88)
        probe_y = min(max(probe_y, 0), h - 1)
        row = m[probe_y]
        if w >= 7 and int(row.sum()) >= 2:
            q = w // 4
            try:
                _mid_gate = mid_w >= max(6, int(w * 0.45))
            except Exception:
                _mid_gate = True
            if _mid_gate:
                leg_split = bool(row[:q].any() and row[-q:].any()
                                 and not row[q:-q].all())
    except Exception:
        leg_split = False
    def _cut_run(col):
        try:
            v = np.asarray(col).reshape(-1).astype(bool)
        except Exception:
            return 0.0
        if v.size == 0 or not v.any():
            return 0.0
        idx = np.flatnonzero(v)
        if idx.size == 0:
            return 0.0
        jumps = np.flatnonzero(np.diff(idx) > 1)
        if jumps.size == 0:
            return float(idx.size) / float(v.size)
        bounds = np.concatenate(([0], jumps + 1, [idx.size]))
        longest = 0
        for a, b in zip(bounds[:-1], bounds[1:]):
            longest = max(longest, int(idx[b - 1] - idx[a] + 1))
        return float(longest) / float(v.size)
    try:
        cut_left = bool(_cut_run(m[:, 0]) > 0.55) if w >= 1 else False
        cut_right = bool(_cut_run(m[:, -1]) > 0.55) if w >= 1 else False
        cut_top = bool(m[0, :].mean() > 0.60) if h >= 1 else False
        cut_bottom = bool(m[-1, :].mean() > 0.60) if h >= 1 else False
    except Exception:
        cut_left = cut_right = cut_top = cut_bottom = False
    try:
        covx = float((m.mean(axis=0) > 0.15).mean()) if w else 0.0
    except Exception:
        covx = 0.0
    try:
        cues = _profile_cues(m)
        head_s = _head_cue(cues)
    except Exception:
        cues, head_s = None, 0.0
    prone_hs, prone_cues = 0.0, None
    try:
        if cues is not None and w > h * 1.4 and w >= 16 and h >= 8:
            best_hs, best_c = -1.0, None
            for mm in (np.ascontiguousarray(m.T),
                       np.ascontiguousarray(m.T[::-1])):
                pc = _profile_cues(mm)
                hs2 = _head_cue(pc)
                if hs2 > best_hs:
                    best_hs, best_c = hs2, pc
            prone_hs, prone_cues = max(0.0, best_hs), best_c
    except Exception:
        prone_hs, prone_cues = 0.0, None
    return {"fill": fill, "sym": float(sym), "coverage": float(coverage),
            "top_w": top_w, "mid_w": mid_w, "bot_w": bot_w,
            "top_narrow": top_narrow, "taper": float(taper),
            "leg_split": bool(leg_split), "h": h, "w": w,
            "cut_left": cut_left, "cut_right": cut_right,
            "cut_top": cut_top, "cut_bottom": cut_bottom,
            "coverage_x": covx, "head_s": float(head_s),
            "shoulder_step": float(cues["step"]) if cues else 0.0,
            "waist_dip": float(cues["dip"]) if cues else 0.0,
            "neck_pinch": float(cues["pinch"]) if cues else 0.0,
            "prone_head_s": float(prone_hs), "prone_cues": prone_cues}


def _figure_score(bw, bh, area, mask=None):
    try:
        bw = int(bw)
        bh = int(bh)
        area = float(area)
    except (TypeError, ValueError):
        return 0.0
    if bw <= 0 or bh <= 0 or area <= 0:
        return 0.0
    r = float(bh) / float(bw)
    rs = _ratio_score(r)
    if mask is None:
        small_ok = (bh >= SHAPE_MIN_H and area >= SHAPE_MIN_AREA)
        if not small_ok:
            return 0.0
        return max(0.0, min(1.0, rs))
    try:
        s, _tier = _figure_detail(bw, bh, area, mask)
    except (TypeError, ValueError):
        return 0.0
    return s
def _trap(x, a, b, c, d):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(x) or x <= a or x >= d:
        return 0.0
    if x < b:
        return (x - a) / max(1e-9, float(b - a))
    if x <= c:
        return 1.0
    return (d - x) / max(1e-9, float(d - c))


def _profile_cues(m):
    out = {"rw": 0.0, "rh": 0.0, "ha": 0.0, "pinch": 0.0, "step": 0.0,
           "dip": 0.0, "cent": 0.0, "maxpos": 0.0, "w_sh": 0.0,
           "y_sh": -1}
    try:
        h, w = m.shape
        if h < 6 or w < 2:
            return out
        has = m.any(axis=1)
        if not bool(has.any()):
            return out
        first = np.argmax(m, axis=1)
        last = w - 1 - np.argmax(m[:, ::-1], axis=1)
        span_n = last - first + 1
        span = [s if hm else 0
                for s, hm in zip(span_n.tolist(), has.tolist())]
        first_l = first.tolist()
        last_l = last.tolist()
        ys = [i for i, s in enumerate(span) if s > 0]
        y0 = ys[0]
        extent = max(1, ys[-1] - y0 + 1)
        b0 = min(h - 1, max(y0 + 1, int(h * 0.10)))
        b1 = min(h, max(b0 + 2, int(h * 0.62)))
        band = sorted(s for s in span[b0:b1] if s > 0)
        if not band:
            band = sorted(s for s in span if s > 0)
            if not band:
                return out
        w_sh = float(band[int(0.60 * (len(band) - 1))])
        if w_sh <= 0:
            return out
        thr = 0.80 * w_sh
        y_sh = -1
        for i in range(b0, b1):
            if span[i] >= thr:
                y_sh = i
                break
        if y_sh < 0:
            y_sh = b0
            best = -1
            for i in range(b0, b1):
                if span[i] > best:
                    best = span[i]
                    y_sh = i
        if (y_sh - y0) * 100 < 9 * extent:
            lim = min(h - 1, y0 + max(3, int(0.35 * extent)))
            dthr = 0.72 * w_sh
            z_last = -1
            i = y_sh
            while i <= lim and z_last < 0:
                if span[i] <= dthr:
                    j = i
                    while j <= lim and span[j] <= dthr:
                        j += 1
                    z_last = j - 1
                i += 1
            if z_last >= 0:
                cand = -1
                for k in range(z_last + 1, lim + 1):
                    if span[k] >= thr:
                        cand = k
                        break
                if cand > 0:
                    hh2 = float(cand - y0)
                    if 0.05 * extent <= hh2 <= 0.30 * extent:
                        y_sh = cand
        hb = [s for s in span[y0:max(y0 + 1, y_sh)] if s > 0]
        if not hb:
            k = max(1, int(extent * 0.15))
            hb = [s for s in span[y0:y0 + k] if s > 0]
            if not hb:
                return out
            y_sh = min(h - 1, y0 + k)
        hbs = sorted(hb)
        n = len(hbs)
        w_hd = float(hbs[n // 2] if n % 2
                     else (hbs[n // 2 - 1] + hbs[n // 2]) * 0.5)
        hh = float(max(1, y_sh - y0))
        out["w_sh"] = w_sh
        out["y_sh"] = y_sh
        out["rw"] = w_hd / w_sh
        out["rh"] = hh / float(extent)
        out["ha"] = hh / max(1.0, w_hd)
        z0 = min(y_sh, y0 + max(1, int(0.35 * hh)))
        zone = [s for s in span[z0:y_sh + 1] if s > 0]
        if len(zone) >= 2:
            neck = float(min(zone)) / max(1.0, w_hd)
            out["pinch"] = max(0.0, min(1.0, (0.95 - neck) / 0.20))
        d0 = max(y0 + int(0.5 * hh), int(h * 0.03))
        d1 = min(h - 1, y_sh + 3, int(h * 0.65))
        if d1 > d0:
            seg = span[d0:d1 + 1]
            out["step"] = float(max(b - a for a, b in zip(seg, seg[1:]))) \
                / w_sh
        e0 = y_sh + max(1, int(h * 0.05))
        wlen = max(2, int(h * 0.15))
        full95 = 0.95 * w_sh
        for i in range(y_sh, min(h, y_sh + wlen)):
            if span[i] >= full95:
                e0 = i + 2
                break
        e0 = min(h - 1, e0)
        e1 = min(h, max(e0 + 1, int(h * 0.65)))
        low = [s for s in span[e0:e1] if s > 0]
        if low:
            out["dip"] = max(0.0, (w_sh - float(min(low))) / w_sh)
        seg2 = span[y0:y0 + extent]
        y_max = y0
        best = -1
        for i, s in enumerate(seg2):
            if s > best:
                best = s
                y_max = y0 + i
        out["maxpos"] = float(y_max - y0) / float(extent)
        t0c = min(h - 1, max(y0, y_sh))
        t1c = min(h, max(t0c + 1, y0 + int(extent * 0.65)))
        hw = [(first_l[i] + last_l[i]) * 0.5
              for i in range(y0, max(y0 + 1, min(y_sh, h)))
              if span[i] > 0]
        tw = [(first_l[i] + last_l[i]) * 0.5
              for i in range(t0c, t1c) if span[i] > 0]
        if hw and tw:
            hx = sum(hw) / float(len(hw))
            tx = sum(tw) / float(len(tw))
            den = max(w_sh, 0.35 * float(extent))
            off = abs(hx - tx) / max(1.0, den)
            out["cent"] = max(0.0, min(1.0, (0.52 - off) / 0.30))
    except Exception:
        pass
    return out


def _head_cue(c):
    rw_s = _trap(c.get("rw", 0.0), 0.20, 0.30, 0.95, 1.10)
    rh_s = _trap(c.get("rh", 0.0), 0.045, 0.08, 0.24, 0.32)
    ha_s = _trap(c.get("ha", 0.0), 0.35, 0.55, 1.90, 2.80)
    mp_s = _trap(c.get("maxpos", 0.0), 0.05, 0.12, 0.45, 0.62)
    s = (0.34 * rw_s + 0.20 * rh_s + 0.14 * ha_s
         + 0.08 * float(c.get("pinch", 0.0))
         + 0.14 * float(c.get("cent", 0.0))
         + 0.10 * mp_s)
    if float(c.get("rw", 0.0)) > 1.15:
        s = min(s, 0.30)
    if ha_s < 0.15:
        s = min(s, 0.55)
    if rh_s < 0.15 and ha_s < 0.15:
        s = min(s, 0.35)
    return s


def _body_combine(head_s, taper_s, sym_s, cov_s, fill_s):
    return max(0.0, min(1.0,
                        FIGURE_W_HEAD * head_s + FIGURE_W_TAPER * taper_s
                        + FIGURE_W_SYM * sym_s + FIGURE_W_COV * cov_s
                        + FIGURE_W_FILL * fill_s))


def _figure_body_score(feats):
    sym = feats["sym"]
    if sym <= FIGURE_SYM_LO:
        sym_s = 0.0
    elif sym >= FIGURE_SYM_HI:
        sym_s = 1.0
    else:
        sym_s = (sym - FIGURE_SYM_LO) / (FIGURE_SYM_HI - FIGURE_SYM_LO)
    cov = feats["coverage"]
    if cov <= FIGURE_COVER_LO:
        cov_s = 0.0
    elif cov >= FIGURE_COVER_HI:
        cov_s = 1.0
    else:
        cov_s = (cov - FIGURE_COVER_LO) / (FIGURE_COVER_HI - FIGURE_COVER_LO)
    fill = feats["fill"]
    if 0.30 <= fill <= 0.78:
        fill_s = 1.0
    elif 0.20 <= fill < 0.30:
        fill_s = (fill - 0.20) / 0.10
    elif 0.78 < fill <= 0.92:
        fill_s = max(0.0, (0.92 - fill) / 0.14)
    else:
        fill_s = 0.0
    taper_s = feats["taper"]
    if feats["leg_split"]:
        taper_s = max(taper_s, 0.8)
    head_s = float(feats.get("head_s", 0.0) or 0.0)
    return (_body_combine(head_s, taper_s, sym_s, cov_s, fill_s),
            sym_s, cov_s, fill_s, taper_s)


def _figure_prone_detail(feats, bw, bh):
    try:
        c = feats.get("prone_cues")
        hs = float(feats.get("prone_head_s", 0.0) or 0.0)
        if c is None or hs < FIGURE_HEAD_OK:
            return 0.0, 0
        if (float(c.get("step", 0.0)) < FIGURE_STEP_MIN
                and float(c.get("dip", 0.0)) < FIGURE_DIP_MIN
                and float(c.get("pinch", 0.0)) < 0.30):
            return 0.0, 0
        rt = float(bw) / float(max(1, int(bh)))
        if rt < 1.3 or rt > 6.5:
            return 0.0, 0
        fill = float(feats.get("fill", 0.0))
        if fill < 0.22 or fill > 0.92:
            return 0.0, 0
        covx = float(feats.get("coverage_x", 0.0))
        if covx < 0.70:
            return 0.0, 0
        score = (0.55 * hs + 0.20 * _ratio_score(rt) + 0.15 * covx
                 + 0.10 * (1.0 if 0.30 <= fill <= 0.85 else 0.5))
        score = max(0.0, min(0.85, score))
        if score >= 0.55:
            return score, 2
        return min(score, 0.45), 0
    except Exception:
        return 0.0, 0


def _figure_detail(bw, bh, area, mask):
    try:
        bw = int(bw)
        bh = int(bh)
        area = float(area)
    except (TypeError, ValueError):
        return 0.0, 0
    if bw <= 0 or bh <= 0 or area <= 0:
        return 0.0, 0
    feats = _figure_features(mask)
    if feats is None:
        if bh < SHAPE_MIN_H or area < SHAPE_MIN_AREA:
            return 0.0, 0
        s = max(0.0, min(1.0, _ratio_score(float(bh) / float(bw))))
        return s, (2 if s >= FIGURE_TIER2_THRESH else 0)
    tiny = (bh < SHAPE_MIN_H or area < SHAPE_MIN_AREA)
    if tiny:
        if bh < FIGURE_SMALL_H or area < FIGURE_SMALL_AREA:
            if not (bh >= FIGURE_SMALL_H
                    and area >= 0.6 * FIGURE_SMALL_AREA
                    and float(feats.get("head_s", 0.0) or 0.0) >= 0.80):
                return 0.0, 0
        if feats["coverage"] < 0.65:
            return 0.0, 0
        if feats["sym"] < 0.60 and feats.get("head_s", 0.0) < 0.55:
            return 0.0, 0
    r = float(bh) / float(bw)
    if r < 0.72 and bw >= 16 and bh >= 8:
        try:
            p_s, p_t = _figure_prone_detail(feats, bw, bh)
        except Exception:
            p_s, p_t = 0.0, 0
        if p_t == 2:
            return p_s, 2
    rs = _ratio_score(r)
    body, sym_s, cov_s, fill_s, taper_s = _figure_body_score(feats)
    head_s = float(feats.get("head_s", 0.0) or 0.0)
    fill = feats["fill"]
    occ_side = bool(feats.get("cut_left", False)) \
        ^ bool(feats.get("cut_right", False))
    if occ_side and (taper_s >= 0.6 or feats["leg_split"]):
        sym_s = max(sym_s, 0.5)
        rs = max(rs, 0.5)
        body = max(body, _body_combine(head_s, taper_s, sym_s, cov_s,
                                       fill_s))
    if bool(feats.get("cut_top", False)) and feats["leg_split"] \
            and feats["coverage"] >= 0.70:
        taper_s = max(taper_s, 0.6)
        rs = max(rs, 0.5)
        body = max(body, _body_combine(head_s, taper_s, sym_s, cov_s,
                                       fill_s))
    struct = bool(taper_s >= 0.6 or feats["leg_split"]
                  or head_s >= FIGURE_HEAD_OK)
    if rs < FIGURE_RESCUE_RS and head_s >= FIGURE_HEAD_STRONG \
            and (feats["leg_split"] or taper_s >= 0.6
                 or float(feats.get("waist_dip", 0.0) or 0.0) >= 0.10
                 or float(feats.get("neck_pinch", 0.0) or 0.0) >= 0.50
                 or float(feats.get("shoulder_step", 0.0) or 0.0)
                 >= FIGURE_STEP_MIN):
        rs = FIGURE_RESCUE_RS
    score = 0.5 * body + 0.5 * rs
    try:
        if (0.75 <= r <= 1.40 or r > 3.0 or r < 0.70):
            if not struct:
                score = min(score, 0.48)
        if fill > 0.94:
            score = min(score, 0.49)
        elif fill < 0.28 and not (head_s >= FIGURE_HEAD_STRONG and struct):
            score = min(score, 0.49)
        try:
            _top = float(feats["top_w"])
            _mid = float(feats["mid_w"])
        except Exception:
            _top = _mid = 0.0
        if taper_s < 0.6 and not feats["leg_split"] \
                and head_s < FIGURE_HEAD_OK:
            if fill < 0.32 or (_mid >= 1 and _top > _mid * 1.4):
                score = min(score, 0.45)
        if not feats["leg_split"] and not occ_side \
                and head_s < FIGURE_HEAD_STRONG \
                and not bool(feats.get("cut_top", False)) \
                and float(feats.get("shoulder_step", 0.0)) < FIGURE_STEP_MIN \
                and float(feats.get("waist_dip", 0.0)) < FIGURE_DIP_MIN:
            score = min(score, 0.45)
        if head_s < FIGURE_HEAD_MIN and not occ_side \
                and not bool(feats.get("cut_top", False)):
            torso_ok = (float(feats.get("shoulder_step", 0.0)) >= 0.15
                        and float(feats.get("waist_dip", 0.0)) >= 0.10
                        and 1.4 <= r <= 3.2)
            score = min(score, 0.52 if torso_ok else 0.48)
        r_cap = 5.0 if occ_side else 3.5
        if head_s >= FIGURE_HEAD_STRONG:
            r_cap = max(r_cap, 7.5)
        if r > r_cap or r < 0.60:
            score = min(score, 0.28)
    except Exception:
        pass
    score = max(0.0, min(1.0, score))
    stage2 = ((body >= 0.55 and rs >= 0.25) or (body >= 0.70)
              or (rs >= 0.80 and feats["coverage"] >= 0.55
                  and 0.25 <= fill <= 0.85))
    if stage2 and score >= FIGURE_TIER2_THRESH:
        return score, 2
    return score, 0


def _figure_tier(bw, bh, area, mask=None):
    if mask is not None:
        try:
            _s, tier = _figure_detail(int(bw), int(bh), float(area), mask)
        except (TypeError, ValueError):
            return 0
        if tier == 2:
            return 2
        s = _s
    else:
        s = _figure_score(bw, bh, area, mask)
    try:
        r = float(bh) / float(max(1, int(bw)))
    except (TypeError, ValueError, ZeroDivisionError):
        r = 0.0
    if SHAPE_SQ_LO < r < SHAPE_SQ_HI:
        if r <= 1.0:
            f = (r - SHAPE_SQ_LO) / (1.0 - SHAPE_SQ_LO)
        else:
            f = (SHAPE_SQ_HI - r) / (SHAPE_SQ_HI - 1.0)
        if f >= SHAPE_TIER_MIN_F:
            return 1
    if s >= FIGURE_TIER1_THRESH:
        return 1
    return 0


def _shape_mult(bw, bh, area, mask=None):
    if mask is not None:
        s = _figure_score(bw, bh, area, mask)
        if s >= FIGURE_TIER2_THRESH:
            q = min(1.0, (s - FIGURE_TIER2_THRESH)
                    / max(1e-6, 1.0 - FIGURE_TIER2_THRESH))
            return 1.0 + FIGURE_BOOST * (0.55 + 0.45 * q)
        if s >= FIGURE_TIER1_THRESH:
            q = (s - FIGURE_TIER1_THRESH) / max(
                1e-6, FIGURE_TIER2_THRESH - FIGURE_TIER1_THRESH)
            return 1.0 + SHAPE_SQ_BONUS * (0.5 + 0.5 * q)
        try:
            r = float(bh) / float(max(1, int(bw)))
        except (TypeError, ValueError, ZeroDivisionError):
            return 1.0
        if SHAPE_SQ_LO < r < SHAPE_SQ_HI and bh >= 8:
            if r <= 1.0:
                f = (r - SHAPE_SQ_LO) / (1.0 - SHAPE_SQ_LO)
            else:
                f = (SHAPE_SQ_HI - r) / (SHAPE_SQ_HI - 1.0)
            return 1.0 + SHAPE_SQ_BONUS * max(0.0, min(1.0, f))
        return 1.0
    if bw <= 0 or bh < SHAPE_MIN_H or area < SHAPE_MIN_AREA:
        return 1.0
    r = float(bh) / float(bw)
    best = 1.0
    if SHAPE_LO < r < SHAPE_HI:
        if r < SHAPE_PLATEAU_LO:
            f = (r - SHAPE_LO) / (SHAPE_PLATEAU_LO - SHAPE_LO)
        elif r <= SHAPE_PLATEAU_HI:
            f = 1.0
        else:
            f = (SHAPE_HI - r) / (SHAPE_HI - SHAPE_PLATEAU_HI)
        best = max(best, 1.0 + SHAPE_BONUS * f)
    if SHAPE_SQ_LO < r < SHAPE_SQ_HI:
        if r <= 1.0:
            f = (r - SHAPE_SQ_LO) / (1.0 - SHAPE_SQ_LO)
        else:
            f = (SHAPE_SQ_HI - r) / (SHAPE_SQ_HI - 1.0)
        best = max(best, 1.0 + SHAPE_SQ_BONUS * f)
    return best


def _shape_tier(bw, bh, area, mask=None):
    if mask is not None:
        return _figure_tier(bw, bh, area, mask)
    if bw <= 0 or bh < SHAPE_MIN_H or area < SHAPE_MIN_AREA:
        return 0
    r = float(bh) / float(bw)
    if SHAPE_LO < r < SHAPE_HI:
        if r < SHAPE_PLATEAU_LO:
            f = (r - SHAPE_LO) / (SHAPE_PLATEAU_LO - SHAPE_LO)
        elif r <= SHAPE_PLATEAU_HI:
            f = 1.0
        else:
            f = (SHAPE_HI - r) / (SHAPE_HI - SHAPE_PLATEAU_HI)



        if f >= SHAPE_TIER_MIN_F:
            return 2
    if SHAPE_SQ_LO < r < SHAPE_SQ_HI:
        if r <= 1.0:
            f = (r - SHAPE_SQ_LO) / (1.0 - SHAPE_SQ_LO)
        else:
            f = (SHAPE_SQ_HI - r) / (SHAPE_SQ_HI - 1.0)
        if f >= SHAPE_TIER_MIN_F:
            return 1
    return 0






EXCLUDE_ERODE_PX = 5
EXCLUDE_CORE_MIN = 8








ACCENT_RANK_CAP = 1.2


def _base_score(contrast, area, fill, dist, shape=1.0):
    return (contrast * math.sqrt(min(area, AREA_SCORE_CAP))
            * (0.4 + 0.6 * fill) * _prox_mult(dist) * shape
            / (12.0 + dist) ** DIST_FALLOFF)




ACCENT_MIN_FRACTION = 0.05











ACCENT_STRONG_SIM = 0.55
ACCENT_TONE_BONUS = 0.25


FOLLOW_COLOR_TOL = 18
FOLLOW_COLOR_FRAC = 0.25
FOLLOW_POS_SIM = 0.8

FOLLOW_POS_PAD = 48


class FollowMemory(object):

    def __init__(self):
        import threading as _th
        self._lock = _th.Lock()
        self.when = 0.0
        self.x = 0
        self.y = 0
        self.sig = None
        self.area = 0
        self.bw = 0
        self.bh = 0
        self.hits = 0

    def store(self, x, y, target):




        with self._lock:
            self.when = time.perf_counter()
            self.x, self.y = int(x), int(y)
            self.sig = getattr(target, "sig", None)
            self.area = getattr(target, "area", 0)
            self.bw = getattr(target, "bw", 0)
            self.bh = getattr(target, "bh", 0)
            self.hits += 1

    def context(self, origin_x, origin_y, follow_seconds):
        with self._lock:
            when, x, y = self.when, self.x, self.y
            sig = self.sig
        if when <= 0.0:
            return None

        age = time.perf_counter() - when
        if follow_seconds <= 0 or age > follow_seconds:
            return None

        decay = max(0.0, 1.0 - age / float(follow_seconds))
        return (sig, x - int(origin_x), y - int(origin_y), decay)

    def snapshot(self):
        with self._lock:
            return self.x, self.y, self.sig, self.when

    def clear(self):
        with self._lock:
            self.when = 0.0
            self.x = 0
            self.y = 0
            self.sig = None
            self.area = 0
            self.bw = 0
            self.bh = 0
            self.hits = 0


FIGURE_STICKY_S = 0.9
FIGURE_POS_PAD = 96
FIGURE_STICKY_BOOST = 0.85
FIGURE_SWITCH_MARGIN = 1.0
FIGURE_KEEP_MIN = 0.38
FIGURE_SOLID_MIN = 0.55


class FigureMemory(object):

    def __init__(self):
        import threading as _th
        self._lock = _th.Lock()
        self.when = 0.0
        self.x = 0
        self.y = 0
        self.bw = 0
        self.bh = 0
        self.hscore = 0.0
        self.hits = 0

    def store(self, x, y, bw, bh, hscore):
        with self._lock:
            self.when = time.perf_counter()
            self.x, self.y = int(x), int(y)
            try:
                self.bw, self.bh = int(bw), int(bh)
            except (TypeError, ValueError):
                self.bw, self.bh = 0, 0
            try:
                self.hscore = float(hscore)
            except (TypeError, ValueError):
                self.hscore = 0.0
            self.hits += 1

    def clear(self):
        with self._lock:
            self.when = 0.0
            self.hscore = 0.0
            self.hits = 0

    def context(self, origin_x, origin_y, sticky_s=FIGURE_STICKY_S):
        with self._lock:
            when, x, y = self.when, self.x, self.y
            bw, bh, hs = self.bw, self.bh, self.hscore
        if when <= 0.0 or hs < FIGURE_TIER2_THRESH:
            return None
        try:
            sticky_s = float(sticky_s)
        except (TypeError, ValueError):
            sticky_s = FIGURE_STICKY_S
        if sticky_s <= 0:
            return None
        age = time.perf_counter() - when
        if age > sticky_s:
            return None
        decay = max(0.0, 1.0 - age / sticky_s)
        return (x - int(origin_x), y - int(origin_y), int(bw), int(bh),
                float(hs), float(decay))


def _merge_nearby(candidates, fragments, merge_distance,
                  min_area, max_area, max_dim, max_aspect, scale=1.0,
                  cx=None, cy=None, w=None, h=None, edge_flags=None,
                  deadline=None, subject_mode=False):


    pool = list(candidates) + list(fragments)
    n_full = len(candidates)
    n = len(pool)

    if n == 0:
        return candidates
    if n == 1:

        return candidates




    boxes = [(c[2], c[1], c[2] + c[7], c[1] + c[8]) for c in pool]

    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(n):



        if deadline is not None and i > 0 \
                and time.perf_counter() > deadline:
            break
        ax0, ay0, ax1, ay1 = boxes[i]
        for j in range(i + 1, n):
            bx0, by0, bx1, by1 = boxes[j]
            gap_x = max(ax0 - bx1, bx0 - ax1, 0)
            gap_y = max(ay0 - by1, by0 - ay1, 0)


            if subject_mode:
                try:
                    x_overlap = max(0, min(ax1, bx1) - max(ax0, bx0))
                    min_w = max(1, min(ax1 - ax0, bx1 - bx0))
                except Exception:
                    x_overlap, min_w = 0, 1
                if x_overlap >= 0.35 * min_w:
                    eff = math.hypot(gap_x, gap_y / 2.0)
                else:
                    eff = math.hypot(gap_x, gap_y)
                if eff <= merge_distance:
                    union(i, j)
            elif math.hypot(gap_x, gap_y) <= merge_distance:
                union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    out = []

    for members in groups.values():
        full_members = [pool[m] for m in members if m < n_full]

        if len(members) == 1:
            if members[0] < n_full:
                out.append(pool[members[0]])
            continue




        if all(m < n_full for m in members):
            out.extend(full_members)
            continue

        X0 = min(boxes[m][0] for m in members)
        Y0 = min(boxes[m][1] for m in members)
        X1 = max(boxes[m][2] for m in members)
        Y1 = max(boxes[m][3] for m in members)
        bw, bh = X1 - X0, Y1 - Y0




        if w is not None and h is not None \
                and _touches_screen_edge(Y0, X0, Y1, X1, w, h,
                                         edge_flags):
            dim_cap = max_dim * 2
            aspect_cap = max_aspect * 2.5
        else:
            dim_cap = max_dim
            aspect_cap = max_aspect
        shape_ok = (
            max(bw, bh) <= dim_cap
            and max(bw, bh) <= aspect_cap * max(1, min(bw, bh))
        )

        if not shape_ok:
            out.extend(full_members)
            continue

        canvas = np.zeros((bh, bw), dtype=bool)
        contrast = 0.0

        for m in members:
            _s, mY0, mX0, cl, _d, _a, ct, mbw, mbh = pool[m]
            oy, ox = mY0 - Y0, mX0 - X0
            dst = canvas[oy:oy + mbh, ox:ox + mbw]
            np.logical_or(dst, cl[:dst.shape[0], :dst.shape[1]], out=dst)
            contrast = max(contrast, ct)

        area = int(canvas.sum())

        if area < min_area or area > max_area:
            out.extend(full_members)
            continue

        fill = area / float(bw * bh)




        if fill < MIN_FILL:
            out.extend(full_members)
            continue

        if cx is not None and cy is not None:



            dist = math.hypot(max(X0 - cx, 0, cx - (X1 - 1)),
                              max(Y0 - cy, 0, cy - (Y1 - 1)))
        else:
            dist = min(pool[m][4] for m in members)
        score = _base_score(contrast, area, fill, dist,
                            shape=_shape_mult(bw, bh, area))
        out.append((score, Y0, X0, canvas, dist, area, contrast, bw, bh))

    return out










__all__ = [
    "ACCENT_PRESETS", "AREA_SCORE_CAP", "CONTRAST_METRIC_SCALE",
    "DIST_FALLOFF", "FollowMemory", "MotionTracker", "ScanResult",
    "SnapTarget", "FigureMemory", "_accent_fraction", "_accent_stats",
    "_figure_body_score", "_figure_detail", "_figure_score", "_figure_tier", "_parse_accent",
    "_parse_accents", "_sig_matches_accents", "accent_coherent_of",
    "accent_matcher_of", "accent_soft_of", "accent_variants_of",
    "blend_similarity", "calibrate_accent", "ciede2000",
    "color_adapt_of", "draw_debug_overlay", "hsv_to_rgb", "motion_sense_of",
    "predict_motion", "scan_region", "set_log_sink",
    "suggest_accents",
]


def _soft_band(d, half, falloff):
    if falloff <= 0:
        return np.where(d <= half, 1.0, 0.0)
    return np.clip(1.0 - (d - half) / falloff, 0.0, 1.0)


def _accent_similarity(h, s, v, ah, asat, av, tol, soft=0.0):
    dh = np.abs(h - ah)
    dh = np.minimum(dh, 180.0 - dh)

    sv_band = tol * 2.2
    h_sim = _soft_band(dh, tol, tol * 2.0 * soft)
    s_sim = _soft_band(np.abs(s - asat), sv_band, sv_band * 2.0 * soft)
    v_sim = _soft_band(np.abs(v - av), sv_band, sv_band * 2.0 * soft)


    rel = min(1.0, float(asat) / 50.0)
    h_sim = h_sim * rel + (1.0 - rel)

    sim = (h_sim ** 0.50) * (s_sim ** 0.20) * (v_sim ** 0.30)

    if soft > 0:
        sim = sim + ACCENT_TONE_BONUS * soft * s_sim * v_sim \
            * (1.0 - h_sim)

    return np.minimum(sim, 1.0)












OKLAB_BLEND_MIN_A = 0.25
OKLAB_BLEND_EPS = 0.05


def _rgb01_to_oklab(px):
    def lin(c):
        return np.where(c <= 0.04045, c / 12.92,
                        ((c + 0.055) / 1.055) ** 2.4)

    r, g, b = lin(px[..., 0]), lin(px[..., 1]), lin(px[..., 2])
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = np.cbrt(l), np.cbrt(m), np.cbrt(s)
    L = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    A = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    B = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return np.stack([L, A, B], axis=-1)


def blend_similarity(px_bgr01, acc_rgb01, bg_bgr01):
    acc = np.asarray(acc_rgb01, dtype=np.float32)[::-1]
    bg = np.asarray(bg_bgr01, dtype=np.float32)
    d = acc - bg
    denom = float(np.dot(d, d))
    if denom < 1e-4:
        return np.zeros(px_bgr01.shape[:-1], dtype=np.float32)
    t = np.sum((px_bgr01 - bg) * d, axis=-1) / denom
    proj = bg + t[..., None] * d
    res = np.sqrt(np.sum((px_bgr01 - proj) ** 2, axis=-1))


    return np.where((t >= OKLAB_BLEND_MIN_A) & (res <= OKLAB_BLEND_EPS),
                    np.minimum(t, 1.0), 0.0).astype(np.float32)
































DE_TOL_BASE = 4.0
DE_TOL_SOFT = 32.0










ACCENT_L_VARIANTS = (0.0, 7.0, -7.0, 14.0, -14.0)
HSV_V_VARIANTS = (0.0, 18.0, -18.0, 36.0, -36.0)








ADAPT_MAX_AB = 10.0
ADAPT_STRENGTH = 0.6






ACCENT_COHERENT_FRAC = 0.5
ACCENT_COHERENT_MIN = 8






ACCENT_MAX_PIXELS = 3000

_lab_cache = {}





_accent_parse_cache = {}
_exclude_parse_cache = {}


def _cached_parse_accents(raw):
    key = ("s", raw) if isinstance(raw, str) else ("r", repr(raw))
    hit = _accent_parse_cache.get(key)
    if hit is None:
        if len(_accent_parse_cache) > 32:
            _accent_parse_cache.clear()
        hit = _parse_accents(raw)
        _accent_parse_cache[key] = hit
    return hit


def _rgb01_to_lab(px):
    def lin(c):
        return np.where(c <= 0.04045, c / 12.92,
                        ((c + 0.055) / 1.055) ** 2.4)

    r, g, b = lin(px[..., 0]), lin(px[..., 1]), lin(px[..., 2])
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = (0.2126729 * r + 0.7151522 * g + 0.0721750 * b)
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883

    def f(t):
        return np.where(t > 0.008856, np.cbrt(np.maximum(t, 1e-12)),
                        7.787 * t + 16.0 / 116.0)

    fx, fy, fz = f(x), f(y), f(z)
    L = 116.0 * fy - 16.0
    A = 500.0 * (fx - fy)
    B = 200.0 * (fy - fz)
    return np.stack([L, A, B], axis=-1)


@functools.lru_cache(maxsize=128)
def _accent_lab_quantised(ah, asat, av):
    rgb = np.array(hsv_to_rgb(ah, asat, av), dtype=np.float32) / 255.0
    lab = _rgb01_to_lab(rgb)
    hit = (lab, float(np.hypot(lab[1], lab[2])), rgb)
    return hit


def _accent_lab(ah, asat, av):
    return _accent_lab_quantised(round(float(ah), 2), round(float(asat), 2),
                                 round(float(av), 2))


def ciede2000(lab1, lab2, kL=1.0, kC=1.0, kH=1.0):
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]

    C1 = np.hypot(a1, b1)
    C2 = np.hypot(a2, b2)
    Cb = 0.5 * (C1 + C2)
    c7 = Cb ** 7
    G = 0.5 * (1.0 - np.sqrt(c7 / (c7 + 25.0 ** 7)))

    a1p = a1 * (1.0 + G)
    a2p = a2 * (1.0 + G)
    C1p = np.hypot(a1p, b1)
    C2p = np.hypot(a2p, b2)

    h1p = np.arctan2(b1, a1p) % (2.0 * np.pi)
    h2p = np.arctan2(b2, a2p) % (2.0 * np.pi)

    dLp = L2 - L1
    dCp = C2p - C1p




    h_diff = h2p - h1p
    dhp = np.where(h_diff > np.pi, h_diff - 2.0 * np.pi, h_diff)
    dhp = np.where(h_diff < -np.pi, dhp + 2.0 * np.pi, dhp)
    cc = C1p * C2p
    dhp = np.where(cc == 0.0, 0.0, dhp)
    dHp = 2.0 * np.sqrt(np.maximum(cc, 0.0)) * np.sin(dhp / 2.0)

    Lbp = 0.5 * (L1 + L2)
    Cbp = 0.5 * (C1p + C2p)

    hsum = h1p + h2p
    hbp = np.where(np.logical_and(cc != 0.0, np.abs(h_diff) > np.pi),
                   np.where(hsum < 2.0 * np.pi, hsum + 2.0 * np.pi,
                            hsum - 2.0 * np.pi),
                   hsum) * 0.5

    T = (1.0
         - 0.17 * np.cos(hbp - np.deg2rad(30.0))
         + 0.24 * np.cos(2.0 * hbp)
         + 0.32 * np.cos(3.0 * hbp + np.deg2rad(6.0))
         - 0.20 * np.cos(4.0 * hbp - np.deg2rad(63.0)))
    tmp = (Lbp - 50.0) ** 2
    SL = 1.0 + 0.015 * tmp / np.sqrt(20.0 + tmp)
    SC = 1.0 + 0.045 * Cbp
    SH = 1.0 + 0.015 * Cbp * T

    dRo = 30.0 * np.exp(-((np.rad2deg(hbp) - 275.0) / 25.0) ** 2)
    c7b = Cbp ** 7
    Rc = 2.0 * np.sqrt(c7b / (c7b + 25.0 ** 7))
    RT = -np.sin(2.0 * np.deg2rad(dRo)) * Rc

    dE2 = ((dLp / (kL * SL)) ** 2 + (dCp / (kC * SC)) ** 2
           + (dHp / (kH * SH)) ** 2
           + RT * (dCp / (kC * SC)) * (dHp / (kH * SH)))
    return np.sqrt(np.maximum(dE2, 0.0)).astype(np.float32)


def _de_terms(p_lab, acc_lab, neutral_chroma=None):
    d = ciede2000(p_lab, acc_lab)
    if neutral_chroma is not None and neutral_chroma < 8.0:
        cpx = np.hypot(p_lab[..., 1], p_lab[..., 2])
        extra = 2.0 * np.clip(cpx - (neutral_chroma + 1.5), 0.0, None)
        d = np.sqrt(d * d + extra * extra)
    return d


def _coherent_enough(mask, min_n=ACCENT_COHERENT_MIN):
    n = int(mask.sum())
    if n < min_n:
        return True
    _labels, info = _label_cells(mask)
    if not info:
        return False
    biggest = max(entry[1] for entry in info)
    return biggest >= ACCENT_COHERENT_FRAC * n


def _subsample_patch(sub_f32, mask):
    while int(mask.sum()) > ACCENT_MAX_PIXELS:
        h, w = mask.shape
        h2, w2 = (h // 2) * 2, (w // 2) * 2
        if h2 < 2 or w2 < 2:
            break
        s = sub_f32[:h2, :w2].reshape(h2 // 2, 2, w2 // 2, 2, 3)
        s = s.mean(axis=(1, 3))
        m = mask[:h2, :w2].reshape(h2 // 2, 2, w2 // 2, 2)
        m = m.mean(axis=(1, 3)) >= 0.5
        if not m.any():
            break
        sub_f32 = s.astype(np.float32)
        mask = m
    return sub_f32, mask

def accent_soft_of(cfgv):
    try:
        return min(1.0, max(0.0, float(cfgv.get("accent_soft", 60)) / 100.0))
    except (TypeError, ValueError):
        return 0.6


def accent_matcher_of(cfgv):
    try:
        m = str(cfgv.get("accent_matcher", "oklab") or "oklab")
    except (TypeError, ValueError):
        return "oklab"
    m = m.strip().lower()
    return m if m in ("hsv", "oklab") else "oklab"


def _bool_of(cfgv, key, default):
    try:
        v = cfgv.get(key, default)
    except (TypeError, ValueError, AttributeError):
        return bool(default)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def color_adapt_of(cfgv):
    return _bool_of(cfgv, "color_adapt", True)


def accent_variants_of(cfgv):
    return _bool_of(cfgv, "accent_variants", True)


def accent_coherent_of(cfgv):
    return _bool_of(cfgv, "accent_coherent", True)


def suggest_accents(img, max_suggestions=6):
    a = np.asarray(img)
    if a.ndim != 3 or a.shape[2] < 3 or a.size == 0:
        return []

    h, w = a.shape[:2]
    step = max(1, int(np.sqrt(float(h * w) / 24000.0)))
    a = a[::step, ::step, :3]
    px01 = a.astype(np.float32)[..., ::-1] / 255.0
    lab = _rgb01_to_oklab(px01).reshape(-1, 3)
    rgb = px01.reshape(-1, 3)

    q = np.stack([np.round(lab[:, 0] / 0.05),
                  np.round(lab[:, 1] / 0.04),
                  np.round(lab[:, 2] / 0.04)], axis=1)
    keys, inv, counts = np.unique(q, axis=0, return_inverse=True,
                                  return_counts=True)
    inv = inv.ravel()
    total = max(float(len(inv)), 1.0)

    sums = np.stack([np.bincount(inv, weights=rgb[:, c],
                                 minlength=len(counts))
                     for c in range(3)], axis=1)
    means = sums / np.maximum(counts, 1)[:, None]

    chroma = np.hypot(keys[:, 1] * 0.04, keys[:, 2] * 0.04)
    rank = (counts / total) * (0.25 + np.minimum(chroma, 0.25) * 3.0)
    order = np.argsort(-rank)




    out = []
    fallback = []
    for idx in order:
        m = means[idx]
        item = ((int(round(float(m[0]) * 255.0)),
                 int(round(float(m[1]) * 255.0)),
                 int(round(float(m[2]) * 255.0))),
                float(counts[idx]) / total)
        (out if chroma[idx] >= 0.03 else fallback).append(item)
        if len(out) >= max(1, int(max_suggestions)):
            break
    while len(out) < max(1, int(max_suggestions)) and fallback:
        out.append(fallback.pop(0))
    return out[:max(1, int(max_suggestions))]


def _sig_matches_accents(sig, accents, tol, soft=0.0, matcher="hsv",
                         variants=True):
    if sig is None or not accents:
        return False

    sh, ss, sv = float(sig[0]), float(sig[1]), float(sig[2])

    if matcher == "oklab" and soft > 0:
        rgb01 = (np.array(hsv_to_rgb(sh, ss, sv), dtype=np.float32)
                 / 255.0)
        sig_lab = _rgb01_to_lab(rgb01)
        tol_e = (DE_TOL_BASE
                 + DE_TOL_SOFT * max(0.0, min(1.0, float(soft)))) \
            * max(0.15, min(3.5, float(tol) / 26.0))
        for (ah, asat, av) in accents:
            lab, chroma, _ = _accent_lab(ah, asat, av)
            best = None
            for dL in (ACCENT_L_VARIANTS if variants else (0.0,)):
                d = float(_de_terms(sig_lab,
                                    lab + np.array(
                                        [dL, 0.0, 0.0], dtype=np.float32),
                                    chroma))
                best = d if best is None else min(best, d)



            if best <= (1.0 - ACCENT_STRONG_SIM) * tol_e:
                return True
        return False







    for (ah, asat, av) in accents:
        for dV in (HSV_V_VARIANTS if variants else (0.0,)):
            vv = min(255.0, max(0.0, av + dV))
            sim = float(_accent_similarity(sh, ss, sv, ah, asat, vv, tol,
                                           soft))
            if sim >= ACCENT_STRONG_SIM:
                return True

    return False


def _bgr01_to_hsv(px):
    b, g, r = px[:, 0], px[:, 1], px[:, 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    d = mx - mn
    safe_d = np.where(d > 0, d, 1.0)
    h_r = ((g - b) / safe_d) % 6.0
    h_g = ((b - r) / safe_d) + 2.0
    h_b = ((r - g) / safe_d) + 4.0
    h = np.where(d == 0, 0.0,
                 np.where(mx == r, h_r, np.where(mx == g, h_g, h_b))) * 30.0
    h %= 180.0
    s = np.where(mx > 0, d / np.maximum(mx, 1e-6), 0.0) * 255.0
    v = mx * 255.0
    return h, s, v


def _accent_stats(img, cl_px, X0, Y0, accents, tol, soft=0.0,
                  matcher="hsv", coherent=False, adapt=False,
                  variants=False, bg_from=None):
    ph, pw = cl_px.shape
    sub = img[Y0:Y0 + ph, X0:X0 + pw].astype(np.float32) / 255.0
    sub0 = sub
    sub, m = _subsample_patch(sub, cl_px)
    px = sub[m]

    if px.shape[0] == 0 or not accents:
        return 0.0, 0.0





    inner = np.zeros_like(m)
    if m.shape[0] > 2 and m.shape[1] > 2:
        inner[1:-1, 1:-1] = (m[1:-1, 1:-1] & m[:-2, 1:-1] & m[2:, 1:-1]
                              & m[1:-1, :-2] & m[1:-1, 2:])
    edge = (m & ~inner)[m]
    use_edge = bool(edge.any() and int(edge.sum()) >= 4)






    if bg_from is not None and bg_from.shape == cl_px.shape:
        _sub_bg, m_bg = _subsample_patch(sub0, bg_from)
        bgm = _sub_bg[~m_bg]
    else:
        bgm = sub[~m]
    bg = None
    if bgm.shape[0] >= 8:
        bg = np.median(bgm.reshape(-1, 3), axis=0)




    coh_min = ACCENT_COHERENT_MIN if m.shape == cl_px.shape \
        else max(2, ACCENT_COHERENT_MIN // 4)




    tol_scale = max(0.15, min(3.5, float(tol) / 26.0))

    if matcher == "oklab" and soft > 0:




        p_lab = _rgb01_to_lab(px[:, ::-1])
        adapt_ab = None
        if adapt and bg is not None:
            blab = _rgb01_to_lab(bg[::-1])
            shift = blab[1:3].astype(np.float32)
            n = float(np.hypot(shift[0], shift[1]))
            if n > 1e-6:
                adapt_ab = shift * min(1.0, ADAPT_MAX_AB / n) \
                    * ADAPT_STRENGTH
                p_lab = p_lab.copy()
                p_lab[..., 1:3] -= adapt_ab

        tol_e = (DE_TOL_BASE
                 + DE_TOL_SOFT * max(0.0, min(1.0, float(soft)))) \
            * tol_scale
        best_mean = 0.0
        best_strong = 0.0
        for (ah, asat, av) in accents:
            lab, chroma, rgb01 = _accent_lab(ah, asat, av)
            best_d = None
            for dL in ACCENT_L_VARIANTS if variants else (0.0,):
                var = lab + np.array([dL, 0.0, 0.0], dtype=np.float32)
                d = _de_terms(p_lab, var, chroma)
                best_d = d if best_d is None else np.minimum(best_d, d)
            sim = np.clip(1.0 - best_d / tol_e, 0.0, 1.0)
            if bg is not None:
                sim = np.maximum(sim, blend_similarity(px, rgb01, bg))
            mean = float(sim.mean())
            if use_edge:
                mean = max(mean, float(sim[edge].mean()))
            strong_mask = sim >= ACCENT_STRONG_SIM
            strong = float(strong_mask.mean())
            if coherent and strong > 0.0:
                grid = np.zeros(m.shape, dtype=bool)
                grid[m] = strong_mask
                if not _coherent_enough(grid, coh_min):
                    continue








            if mean > best_mean:
                best_mean = mean
            if strong > best_strong:
                best_strong = strong
        return best_mean, best_strong







    pxc = px
    if adapt and bg is not None:
        ref = float(bg.mean())
        gain = ref / np.maximum(bg, 1e-3)
        gain = np.clip(gain, 0.5, 2.0).astype(np.float32)
        pxc = np.clip(px * gain, 0.0, 1.0)

    h, s, v = _bgr01_to_hsv(pxc)

    best_mean = 0.0
    best_strong = 0.0

    for (ah, asat, av) in accents:
        best_sim = None
        for dV in HSV_V_VARIANTS if variants else (0.0,):
            vv = min(255.0, max(0.0, av + dV))
            sim = _accent_similarity(h, s, v, ah, asat, vv, tol, soft)
            best_sim = sim if best_sim is None \
                else np.maximum(best_sim, sim)
        sim = best_sim
        mean = float(sim.mean())
        if use_edge:
            mean = max(mean, float(sim[edge].mean()))
        strong_mask = sim >= ACCENT_STRONG_SIM
        strong = float(strong_mask.mean())
        if coherent and strong > 0.0:
            grid = np.zeros(m.shape, dtype=bool)
            grid[m] = strong_mask
            if not _coherent_enough(grid, coh_min):
                continue

        if mean > best_mean:
            best_mean = mean
        if strong > best_strong:
            best_strong = strong

    return best_mean, best_strong







MOTION_MIN_SPEED = 20.0
MOTION_MAX_DEVIATION = 12.0
MOTION_MIN_SPAN = 0.03


def predict_motion(obs, lead_ms, max_offset):
    if lead_ms <= 0 or len(obs) < 2 or max_offset <= 0:
        return 0.0, 0.0

    (t0, x0, y0), (t1, x1, y1) = obs[0], obs[-1]
    span = t1 - t0
    if span < MOTION_MIN_SPAN:
        return 0.0, 0.0

    vx = (x1 - x0) / span
    vy = (y1 - y0) / span
    if math.hypot(vx, vy) < MOTION_MIN_SPEED:
        return 0.0, 0.0

    for (t, x, y) in obs[1:-1]:
        dx = x - (x0 + vx * (t - t0))
        dy = y - (y0 + vy * (t - t0))
        if math.hypot(dx, dy) > MOTION_MAX_DEVIATION:
            return 0.0, 0.0

    lead = float(lead_ms) / 1000.0
    ox, oy = vx * lead, vy * lead
    mag = math.hypot(ox, oy)
    if mag > max_offset:
        k = max_offset / mag
        ox, oy = ox * k, oy * k
    return ox, oy





































class MotionTracker(object):



    POS_R = 9.0
    SCALE_R = 0.0025


    POS_Q = 2500.0
    SCALE_Q = 0.02

    MISS_RESET = 3


    STALE_S = 0.6


    SCALE_JUMP = 0.69


    POS_GATE_FLOOR = 4.0
    SCALE_GATE_FLOOR = 0.10

    def __init__(self, dim=2):
        if dim not in (2, 3):
            raise ValueError("MotionTracker dim is 2 or 3")
        self.dim = dim
        self.n = 2 * dim







        self._prev_obs = None
        self.reset()

    def reset(self):
        self.x = None
        self.P = None
        self.last_t = None
        self.updates = 0
        self.misses = 0
        self.q_mult = 1.0

    def _init(self, t, z):
        n = self.dim
        self.x = np.zeros(2 * n, dtype=np.float64)
        self.x[:n] = z


        self.P = np.diag(np.concatenate(
            [self._r_vec(), np.full(n, 40000.0)]))
        self.updates = 1
        prev = self._prev_obs
        if prev is not None:
            pt, pz = prev
            if pz.shape == z.shape and 0.0 < t - pt <= self.STALE_S:
                self.x[n:] = (z - pz) / (t - pt)
                self.updates = 2
        self.last_t = t
        self.misses = 0
        self.q_mult = 1.0
        return True

    def _r_vec(self):
        if self.dim == 2:
            return np.array([self.POS_R, self.POS_R], dtype=np.float64)
        return np.array([self.POS_R, self.POS_R, self.SCALE_R],
                        dtype=np.float64)

    def _q_base(self):
        if self.dim == 2:
            return np.array([self.POS_Q, self.POS_Q], dtype=np.float64)
        return np.array([self.POS_Q, self.POS_Q, self.SCALE_Q],
                        dtype=np.float64)

    def _to_state_pos(self, pos):
        if self.dim == 2:
            return np.array([float(pos[0]), float(pos[1])],
                            dtype=np.float64)
        s = float(pos[2])
        if not math.isfinite(s) or s <= 0.0:
            return None
        return np.array([float(pos[0]), float(pos[1]), math.log(s)],
                        dtype=np.float64)

    def update(self, t, pos):
        try:
            t = float(t)
            z = self._to_state_pos(pos)
        except (TypeError, ValueError, IndexError):
            return False
        if z is None or not math.isfinite(t) or not np.all(np.isfinite(z)):
            return False




        prev = self._prev_obs
        if prev is None or t > prev[0]:
            self._prev_obs = (t, z.copy())

        if self.last_t is None:
            return self._init(t, z)

        dt = t - self.last_t
        if dt <= 0.0:
            return False
        if dt > self.STALE_S:
            self.reset()
            return self.update(t, pos)

        n = self.dim
        if n == 3 and abs(z[2] - self.x[2]) > self.SCALE_JUMP:





            self.reset()
            return self.update(t, pos)



        F = np.eye(2 * n, dtype=np.float64)
        F[:n, n:] = np.eye(n, dtype=np.float64) * dt
        qb = self._q_base() * self.q_mult
        Q = np.zeros((2 * n, 2 * n), dtype=np.float64)
        Q[:n, :n] = np.diag(qb * dt ** 3 / 3.0)
        Q[:n, n:] = np.diag(qb * dt ** 2 / 2.0)
        Q[n:, :n] = np.diag(qb * dt ** 2 / 2.0)
        Q[n:, n:] = np.diag(qb * dt)
        xp = F.dot(self.x)
        Pp = F.dot(self.P).dot(F.T) + Q



        R = self._r_vec()
        S_diag = np.diag(Pp)[:n] + R
        S_diag = np.maximum(S_diag, 1e-9)
        y = z - xp[:n]
        floors = np.empty(n, dtype=np.float64)
        floors[:] = self.POS_GATE_FLOOR
        if n == 3:
            floors[2] = self.SCALE_GATE_FLOOR
        gate = np.maximum(3.0 * np.sqrt(S_diag), floors)
        if np.any(np.abs(y) > gate):
            self.misses += 1
            if self.misses >= self.MISS_RESET:









                self.reset()
                if prev is not None:
                    self._prev_obs = prev
                return self._init(t, z)
            return False



        H = np.zeros((n, 2 * n), dtype=np.float64)
        H[:, :n] = np.eye(n, dtype=np.float64)
        S = H.dot(Pp).dot(H.T) + np.diag(R)
        try:
            K = Pp.dot(H.T).dot(np.linalg.inv(S))
        except np.linalg.LinAlgError:
            return False
        self.x = xp + K.dot(y)
        I_KH = np.eye(2 * n, dtype=np.float64) - K.dot(H)
        self.P = I_KH.dot(Pp).dot(I_KH.T) \
            + K.dot(np.diag(R)).dot(K.T)
        normed = float(np.max(np.abs(y) / gate))
        self.q_mult = 16.0 if normed > 0.5 else max(1.0, self.q_mult * 0.5)
        self.last_t = t
        self.updates += 1
        self.misses = 0
        return True

    def speed(self):
        if self.x is None:
            return 0.0
        n = self.dim
        return float(math.hypot(self.x[n], self.x[n + 1]))

    def predict(self, lead_ms, max_offset, min_speed=MOTION_MIN_SPEED):
        zero3 = (0.0, 0.0, 1.0)
        zero2 = (0.0, 0.0)
        try:
            lead = float(lead_ms) / 1000.0
        except (TypeError, ValueError):
            return zero3 if self.dim == 3 else zero2
        if lead <= 0 or max_offset <= 0 or self.x is None \
                or self.updates < 2:
            return zero3 if self.dim == 3 else zero2

        n = self.dim
        vx, vy = float(self.x[n]), float(self.x[n + 1])
        if math.hypot(vx, vy) < min_speed:
            return zero3 if self.dim == 3 else zero2

        damp = 1.0 if self.misses == 0 else (
            0.5 if self.misses == 1 else 0.25)
        ox, oy = vx * lead * damp, vy * lead * damp
        mag = math.hypot(ox, oy)
        if mag > max_offset:
            k = max_offset / mag
            ox, oy = ox * k, oy * k

        if self.dim == 3:
            try:
                lr = max(-math.log(4.0),
                         min(math.log(4.0),
                             float(self.x[5]) * lead * damp))
                ratio = math.exp(lr)
            except (ValueError, OverflowError):
                ratio = 1.0
            return ox, oy, ratio
        return ox, oy












MOTION_DIFF_LO = 18.0
MOTION_DIFF_HI = 90.0
MOTION_WHITE_CUT = 210.0


def _frame_movers(prev_gray, curr_gray):
    try:
        if prev_gray is None or curr_gray is None:
            raise ValueError("no baseline")
        p = np.asarray(prev_gray, dtype=np.float32)
        c = np.asarray(curr_gray, dtype=np.float32)
        if p.shape != c.shape or p.size == 0:
            raise ValueError("shape")
        d = np.abs(c - p)
        lo = float(np.percentile(d, 10)) + 3.0
        hi = float(np.percentile(d, 90))
        lo = min(max(lo, MOTION_DIFF_LO), MOTION_DIFF_HI)
        hi = max(hi, lo + 1.0)
        m = (d >= lo) & ((d <= hi) | (c < MOTION_WHITE_CUT))
        return _erode(m, 1)
    except Exception:
        try:
            return np.zeros(np.asarray(curr_gray).shape[:2], dtype=bool)
        except Exception:
            return np.zeros((0, 0), dtype=bool)


def _estimate_cam_shift(prev_gray, curr_gray):
    try:
        import cv2
        if prev_gray is None or curr_gray is None:
            return 0.0, 0.0
        p = np.asarray(prev_gray, dtype=np.float32)
        c = np.asarray(curr_gray, dtype=np.float32)
        if p.shape != c.shape or p.size == 0:
            return 0.0, 0.0
        if p.shape[0] < 16 or p.shape[1] < 16:
            return 0.0, 0.0
        r = cv2.phaseCorrelate(p, c)
        if not r:
            return 0.0, 0.0
        s = r[0]
        return float(s[0]), float(s[1])
    except Exception:
        return 0.0, 0.0


def motion_sense_of(cfgv):
    try:
        return min(1.0, max(0.0, float(cfgv.get("motion_sense", 0)) / 100.0))
    except (TypeError, ValueError):
        return 0.0


def _accent_fraction(img, cl_px, X0, Y0, accents, tol, soft=0.0,
                      matcher="hsv", coherent=False, adapt=False,
                      variants=False):
    return _accent_stats(img, cl_px, X0, Y0, accents, tol, soft, matcher,
                         coherent, adapt, variants)[0]





CALIB_HALF_WEIGHT_PX = 60.0
CALIB_MIN_WEIGHT = 0.02


def _weighted_mean(values, weights):
    w = float(np.sum(weights))
    if w <= 0.0:
        return float(np.mean(values)) if len(values) else 0.0
    return float(np.sum(values * weights) / w)


def _weighted_percentile(values, weights, pct):
    values = np.asarray(values, dtype=np.float64).reshape(-1)
    weights = np.asarray(weights, dtype=np.float64).reshape(-1)
    if values.size == 0:
        return 0.0
    if values.size == 1 or float(np.sum(weights)) <= 0.0:
        return float(np.percentile(values, pct))
    order = np.argsort(values)
    v, w = values[order], weights[order]
    cum = np.cumsum(w)
    total = cum[-1]
    target = total * (float(pct) / 100.0)
    idx = int(np.searchsorted(cum, target))
    return float(v[min(idx, v.size - 1)])


def calibrate_accent(img, cx, cy, accents, tol, soft, matcher="hsv"):
    if not accents or img is None:
        return None
    a = np.asarray(img)
    if a.ndim != 3 or a.shape[2] < 3 or a.size == 0:
        return None













    h, w = a.shape[:2]
    cx = min(max(int(cx), 0), max(0, w - 1))
    cy = min(max(int(cy), 0), max(0, h - 1))
    yy, xx = np.mgrid[0:h, 0:w]
    spatial = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2).astype(np.float32)



    weight = 1.0 / (1.0 + (spatial / CALIB_HALF_WEIGHT_PX) ** 2)

    px = a.reshape(-1, 3).astype(np.float32) / 255.0
    wt = weight.reshape(-1)
    keep = wt >= CALIB_MIN_WEIGHT
    if int(keep.sum()) >= 64:
        px, wt = px[keep], wt[keep]
    if px.shape[0] > 20000:


        step = px.shape[0] // 20000 + 1
        px, wt = px[::step], wt[::step]

    if matcher == "oklab" and soft > 0:








        p_lab = _rgb01_to_lab(px[:, ::-1])
        best = None
        for (ah, asat, av) in accents:
            lab, chroma, _ = _accent_lab(ah, asat, av)
            best_d = None
            for dL in ACCENT_L_VARIANTS:
                d = _de_terms(p_lab,
                              lab + np.array([dL, 0.0, 0.0],
                                             dtype=np.float32),
                              chroma)
                best_d = d if best_d is None else np.minimum(best_d, d)
            best = best_d if best is None else np.minimum(best, best_d)
        color_d = best
        tol_now = (DE_TOL_BASE
                   + DE_TOL_SOFT * max(0.0, min(1.0, float(soft)))) \
            * max(0.15, min(3.5, float(tol) / 26.0))
        strong = float(_weighted_mean(
            (color_d <= (1.0 - ACCENT_STRONG_SIM) * tol_now).astype(np.float32),
            wt))
        unit = "dE"
    else:
        h, s, v = _bgr01_to_hsv(px)
        best = None
        for (ah, asat, av) in accents:
            sim = _accent_similarity(h, s, v, ah, asat, av, tol, soft)
            color_d = 1.0 - sim
            best = color_d if best is None else np.minimum(best, color_d)
        color_d = best
        strong = float(_weighted_mean(
            (color_d <= 1.0 - ACCENT_STRONG_SIM).astype(np.float32), wt))
        unit = "1-sim"

    if color_d.size == 0:
        return None
    p50 = float(_weighted_percentile(color_d, wt, 50.0))
    p95 = float(_weighted_percentile(color_d, wt, 95.0))


    if unit == "dE":



        scale = max(0.15, min(3.5, float(tol) / 26.0))
        suggested = int(round(min(1.0, max(0.0, (p95 / scale - DE_TOL_BASE)
                                             / DE_TOL_SOFT)) * 100.0))
    else:







        gate = 1.0 - ACCENT_STRONG_SIM
        cur = min(1.0, max(0.0, float(soft))) * 100.0
        if p95 <= gate:
            suggested = int(round(cur))
        else:
            frac = min(1.0, (p95 - gate) / max(1e-6, 1.0 - gate))
            suggested = int(round(cur + frac * (100.0 - cur)))
    return {"pixels": int(px.shape[0]), "p50": p50, "p95": p95,
            "strong_frac": strong, "suggested_soft": suggested,
            "unit": unit, "cursor": (cx, cy)}












EDGE_TOUCH_PX = 12


def _touches_screen_edge(Y0, X0, Y1, X1, w, h, edge_flags):
    if not edge_flags:
        return False
    try:
        el, et, er, eb = edge_flags
    except (TypeError, ValueError):
        return False


    return ((el and X0 <= EDGE_TOUCH_PX)
            or (et and Y0 <= EDGE_TOUCH_PX)
            or (er and X1 - 1 >= w - EDGE_TOUCH_PX)
            or (eb and Y1 - 1 >= h - EDGE_TOUCH_PX))


def _is_clipped(Y0, X0, bw, bh, w, h, edge_flags):
    el = et = er = eb = False
    if edge_flags is not None:
        try:
            el, et, er, eb = edge_flags
        except (TypeError, ValueError):
            pass

    if Y0 <= 2 and not et:
        return True
    if X0 <= 2 and not el:
        return True
    if Y0 + bh >= h - 2 and not eb:
        return True
    if X0 + bw >= w - 2 and not er:
        return True
    return False



def _hpose_enabled(cfgv) -> bool:
    try:
        import hpose_bridge
    except Exception:
        return False
    try:
        return bool(hpose_bridge.wanted(cfgv))
    except Exception:
        return False


def _hpose_box_stats(img, box):
    area = 0.0
    contrast = 0.0
    sig = None
    try:
        h, w = img.shape[:2]
        x1 = int(max(0, min(w - 1, box[0])))
        y1 = int(max(0, min(h - 1, box[1])))
        x2 = int(max(x1 + 1, min(w, box[2])))
        y2 = int(max(y1 + 1, min(h, box[3])))
        patch = img[y1:y2, x1:x2]
        if patch.size == 0:
            return area, contrast, sig
        area = float((x2 - x1) * (y2 - y1))
        lum = patch.astype(np.float32).mean(axis=2)
        inner = float(lum.mean())
        pad = max(2, min(x2 - x1, y2 - y1) // 6)
        ring = img[max(0, y1 - pad):min(h, y2 + pad),
                   max(0, x1 - pad):min(w, x2 + pad)]
        if ring.size:
            contrast = abs(inner - float(ring.astype(np.float32)
                                           .mean(axis=2).mean()))
        band = patch[int(patch.shape[0] * 0.25):int(patch.shape[0] * 0.75)]
        if band.size:
            flat = band.reshape(-1, band.shape[-1])
            if flat.shape[0] > 4000:
                flat = flat[::flat.shape[0] // 4000 + 1]
            med = np.median(flat.astype(np.float32), axis=0)
            sig = _parse_accent("%d,%d,%d" % (int(med[2]), int(med[1]),
                                              int(med[0])))
    except Exception:
        pass
    return area, contrast, sig


HINT_SIZE_LO = 0.30
HINT_SIZE_HI = 3.40


def _hpose_hint_match(img, subject, ox, oy, hint, cfgv):
    if hint is None:
        return True
    want_track = int(hint.get("track", 0) or 0)
    track = int(getattr(subject, "track_id", 0) or 0)
    if want_track and track and track == want_track:
        return True
    want_sig = hint.get("sig")
    if want_sig is None:
        return False
    box = getattr(subject, "box", None)
    if box is None:
        return False
    lb = (box[0] - ox, box[1] - oy, box[2] - ox, box[3] - oy)
    try:
        area, _contrast, sig = _hpose_box_stats(img, lb)
    except Exception:
        return False
    if sig is None:
        return False
    try:
        tol = float(cfgv.get("accent_tolerance", 26) or 26)
    except (TypeError, ValueError):
        tol = 26.0
    if not _sig_matches_accents(sig, [want_sig], tol,
                                accent_soft_of(cfgv),
                                accent_matcher_of(cfgv),
                                variants=accent_variants_of(cfgv)):
        return False
    want_area = float(hint.get("area", 0) or 0)
    if want_area > 0.0 and float(area) > 0.0:
        ratio = float(area) / want_area
        if ratio < HINT_SIZE_LO or ratio > HINT_SIZE_HI:
            return False
    return True


def _hpose_target(img, cx, cy, radius, cfgv, origin, deadline,
                collect=None, hint=None):
    try:
        import hpose_bridge
    except Exception:
        return None
    try:
        engine = hpose_bridge.engine(cfgv)
    except Exception:
        return None
    if engine is None:
        return None
    if time.perf_counter() > deadline:
        return None
    ox, oy = (int(origin[0]), int(origin[1])) if origin else (0, 0)
    try:
        try:
            budget_s = min(max(float(cfgv.get("scan_budget_ms", 100)),
                               60.0), 2000.0) / 1000.0
        except (TypeError, ValueError):
            budget_s = 0.1
        subjects = engine.scan(img, (ox, oy), deadline=deadline,
                               zoom_deadline=deadline + 3.0 * budget_s,
                               aim=(int(cx), int(cy)),
                               aim_radius=float(radius or 0.0))
    except Exception as exc:
        _log("hpose scan failed: %r" % (exc,))
        return None
    if not subjects:
        return None
    if hint is not None:
        subjects = [sb for sb in subjects
                    if _hpose_hint_match(img, sb, ox, oy, hint, cfgv)]
        if not subjects:
            return None
    screen_cx = ox + cx
    screen_cy = oy + cy
    if collect is not None:
        for _sb in subjects:
            try:
                _scx, _scy = _sb.center
                _ddx = _scx - screen_cx
                _ddy = _scy - screen_cy
                _bw = _sb.width
                _bh = _sb.height
                collect.append((_sb.score, _ddx, _ddy,
                                math.hypot(_ddx, _ddy),
                                _bw * _bh, 0.0, _bw, _bh))
            except Exception:
                continue
    subject = hpose_bridge.pick(subjects, cfgv, screen_cx, screen_cy,
                                radius)
    if subject is None:
        return None
    aim = str(cfgv.get("hpose_aim", "center") or "center").lower()
    if aim == "head":
        kps = getattr(subject, "kps", None)
        if kps and len(kps) > 0:
            tx = float(kps[0])
            ty = float(kps[1])
        else:
            tx = subject.center[0]
            ty = subject.box[1] + subject.height * 0.15
    else:
        tx, ty = subject.center
    lead_s = 0.0
    try:
        lead_s = float(cfgv.get("hpose_lead_ms", 0) or 0) / 1000.0
    except (TypeError, ValueError):
        lead_s = 0.0
    lx = ly = 0.0
    if lead_s > 0.0:
        try:
            lx, ly = engine.lead_of(subject, lead_s)
        except Exception:
            lx = ly = 0.0
    ddx = int(round(tx + lx - screen_cx))
    ddy = int(round(ty + ly - screen_cy))
    move = math.hypot(ddx, ddy)
    lb = (subject.box[0] - ox, subject.box[1] - oy,
          subject.box[2] - ox, subject.box[3] - oy)
    area, contrast, sig = _hpose_box_stats(img, lb)
    bw = int(round(subject.width))
    bh = int(round(subject.height))
    target = SnapTarget(ddx, ddy, move, area, contrast, bw, bh, sig,
                         float(subject.score))
    target.view = subject.view
    target.stance = subject.stance
    target.motion = subject.motion
    target.lead_x = float(lx)
    target.lead_y = float(ly)
    target.speed = float(subject.speed)
    target.track_id = int(subject.track_id)
    return target

"""Last-resort person watcher: high-contrast, roughly 1:2 ink blobs.

When the neural path and the classical scanner both come up empty, a
20-30 px person is still a compact, high-contrast blob about twice as
tall as it is wide.  That shape survives colour transformations and
background clutter that defeat both a 640 px-input pose network (the
person is a smear of upscaled blur down there) and the classical
scanner (whose luma-weighted diff goes blind on isoluminant colour
edges, and whose item priors are not person priors).

The watcher looks for exactly that, on a 2x magnified copy of the
window so a 20 px body spans enough cells to measure an aspect ratio,
with a colour-aware contrast metric (the per-channel maximum of the
difference from the local background, not a luma weighting).  It only
ever runs as the last stage, only when human detection is switched on,
and only inside the configured search radius.
"""

BLOB_MIN_H = 14.0
BLOB_MAX_H = 64.0
BLOB_ASPECT_LO = 0.30
BLOB_ASPECT_HI = 0.85
BLOB_FILL_MIN = 0.30
BLOB_CONTRAST_MIN = 26.0


def _blob_components_cells(cells):
    gh, gw = cells.shape
    if gh < 2 or gw < 2:
        return []
    parent = list(range(gh * gw + 1))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    boxes = {}
    for y in range(gh):
        for x in np.flatnonzero(cells[y]):
            i = y * gw + x
            boxes[i] = [x, y, x, y]
            if x and cells[y, x - 1]:
                union(i, i - 1)
            if y and cells[y - 1, x]:
                union(i, i - gw + x)
            if y and x and cells[y - 1, x - 1]:
                union(i, i - gw + x - 1)
            if y and x + 1 < gw and cells[y - 1, x + 1]:
                union(i, i - gw + x + 1)
    out = {}
    for i in list(boxes):
        r = find(i)
        b = out.setdefault(r, [boxes[i][0], boxes[i][1],
                               boxes[i][2], boxes[i][3], 0])
        b[0] = min(b[0], boxes[i][0]); b[1] = min(b[1], boxes[i][1])
        b[2] = max(b[2], boxes[i][2]); b[3] = max(b[3], boxes[i][3])
        b[4] += 1
    return [(b[0], b[1], b[2] + 1, b[3] + 1, b[4]) for b in out.values()]


def _blob_watch(img, cx, cy, radius):
    a = img.astype(np.float32)
    if a.ndim == 3 and a.shape[2] > 3:
        a = a[..., :3]
    zoom = 2
    a = np.repeat(np.repeat(a, zoom, axis=0), zoom, axis=1)
    bg = _box_blur(a, BLUR_WIN)
    c = np.abs(a - bg).max(axis=2)
    flat = c.ravel()
    if flat.size > 20000:
        flat = flat[::flat.size // 20000 + 1]
    energy = float(np.percentile(flat, ADAPTIVE_PERCENTILE))
    thr = max(BLOB_CONTRAST_MIN, min(80.0, energy * 0.40))
    strong = c > thr
    weak = c > thr * 0.5
    for m in (strong, weak):
        m[:4, :] = False
        m[-4:, :] = False
        m[:, :4] = False
        m[:, -4:] = False
    strong = _erode(_dilate(strong, 1), 1)
    weak = _dilate(weak, 1)
    cs = 4
    gh, gw = a.shape[0] // cs, a.shape[1] // cs
    block = lambda m: m[:gh * cs, :gw * cs].reshape(gh, cs, gw, cs).mean(
        axis=(1, 3))
    grown = _grow_cells(block(strong) > 0.25, block(weak) > 0.25)
    best = None
    best_key = None
    for bx0, by0, bx1, by1, ncell in _blob_components_cells(grown):
        bw = (bx1 - bx0) * cs / float(zoom)
        bh = (by1 - by0) * cs / float(zoom)
        if not (BLOB_MIN_H <= bh <= BLOB_MAX_H):
            continue
        asp = bw / bh if bh > 0 else 0.0
        if not (BLOB_ASPECT_LO <= asp <= BLOB_ASPECT_HI):
            continue
        fill = ncell / float((bx1 - bx0) * (by1 - by0))
        if fill < BLOB_FILL_MIN:
            continue
        y0, y1 = by0 * cs, by1 * cs
        x0, x1 = bx0 * cs, bx1 * cs
        m = grown[by0:by1, bx0:bx1]
        mean_c = float(c[y0:y1, x0:x1][m].mean()) if m.any() else 0.0
        if mean_c < thr * 0.8:
            continue
        bxc = (x0 + x1) / (2.0 * zoom)
        byc = (y0 + y1) / (2.0 * zoom)
        dist = math.hypot(bxc - cx, byc - cy)
        if radius and dist > radius + 10.0:
            continue
        score = mean_c * fill * (1.0 - abs(asp - 0.5) / 0.5)
        key = (round(dist, 1), -score)
        if best_key is None or key < best_key:
            best_key = key
            best = (bxc, byc, bw, bh, mean_c,
                    ncell * (cs / float(zoom)) ** 2)
    if best is None:
        return None
    bxc, byc, bw, bh, mean_c, area = best
    return SnapTarget(int(round(bxc - cx)), int(round(byc - cy)),
                      math.hypot(bxc - cx, byc - cy), float(area),
                      mean_c, int(round(bw)), int(round(bh)))


def scan_region(img, cx, cy, cfgv, radius, follow_ctx=None, edge_flags=None,
                deadline_ms=None, collect=None, anchor_blind_area=0,
                motion_ctx=None, focus_bias=0.0, require_tier=0,
                subject_ctx=None, origin=None, follow_hint=None):
    if deadline_ms is None:
        try:
            deadline_ms = float(cfgv.get("scan_budget_ms", 350))
        except Exception:
            deadline_ms = 350.0


        deadline_ms = min(max(deadline_ms, 60.0), 2000.0)
    t0 = time.perf_counter()
    deadline = t0 + deadline_ms / 1000.0
    _SCAN_DEADLINE.deadline = deadline

    if _hpose_enabled(cfgv):
        try:
            _ht = _hpose_target(img, cx, cy, radius, cfgv, origin, deadline,
                                collect=collect, hint=follow_hint)
        except Exception as exc:
            _log("hpose stage failed: %r" % (exc,))
            _ht = None
        if _ht is not None:
            return ScanResult("target", _ht,
                              (time.perf_counter() - t0) * 1000.0)
        if follow_hint is not None:
            return ScanResult("none", None,
                              (time.perf_counter() - t0) * 1000.0)

    a = img.astype(np.float32)
    h, w = a.shape[:2]
    if a.shape[2] > 3:
        a = a[..., :3]

    bg = _box_blur(a, BLUR_WIN)


    diff = np.sqrt(((a - bg) ** 2 * _CH_WEIGHTS).sum(axis=2))





    if bool(cfgv.get("luminance_norm", True)):
        lum = (bg * _CH_WEIGHTS).sum(axis=2)
        diff = diff * np.clip(NORM_K / (lum + NORM_C), NORM_MIN, NORM_MAX)

    try:
        thr = float(cfgv.get("contrast_threshold", 38))
    except (TypeError, ValueError):
        thr = 38.0
    adapt_scale = 1.0









    if bool(cfgv.get("adaptive_threshold", True)):




        flat = diff.ravel()
        if flat.size > 20000:
            flat = flat[::flat.size // 20000 + 1]
        energy = float(np.percentile(flat, ADAPTIVE_PERCENTILE))
        adapted = max(ADAPTIVE_FLOOR, min(thr, energy * ADAPTIVE_K))


        adapt_scale = min(1.0, adapted / thr) if thr > 0 else 1.0
        thr = adapted

    ink = diff > thr
    ink_w = diff > thr * 0.55

    for m in (ink, ink_w):
        m[:2, :] = False
        m[-2:, :] = False
        m[:, :2] = False
        m[:, -2:] = False

    ink = _erode(_dilate(ink, 1), 1)
    ink_w = _erode(_dilate(ink_w, 1), 1)
















    merge_distance = float(cfgv.get("merge_distance", 0) or 0)
    scale = 1.0
    if merge_distance > 0:


        if _get_dpi_scale is not None:
            try:
                scale = _get_dpi_scale()
            except Exception:
                scale = 1.0



        gap = int(min(3, max(1, round(0.5 * merge_distance * scale))))
        ink = _close_gaps(ink, gap)
        ink_w = _close_gaps(ink_w, gap)


    ph = (CS - h % CS) % CS
    pw = (CS - w % CS) % CS

    ink_p = np.pad(ink, ((0, ph), (0, pw))) if (ph or pw) else ink
    inkw_p = np.pad(ink_w, ((0, ph), (0, pw))) if (pw or ph) else ink_w

    gh, gw = ink_p.shape[0] // CS, ink_p.shape[1] // CS

    ratio = ink_p.reshape(gh, CS, gw, CS).mean(axis=(1, 3))
    ratio_w = inkw_p.reshape(gh, CS, gw, CS).mean(axis=(1, 3))

    strong = ratio >= CELL_SOLID
    weak = ratio_w >= CELL_WEAK

    solids = _grow_cells(strong, weak)
    solids = _erode(_dilate(solids, 1), 1)


    full = np.ones((gh, gw), dtype=bool)
    obj_cells = ~_flood_bg(solids, full)

    try:
        min_area = int(cfgv.get("min_area", 220))
    except (TypeError, ValueError):
        min_area = 220
    try:
        max_area = int(cfgv.get("max_area", 45000))
    except (TypeError, ValueError):
        max_area = 45000
    try:
        max_dim = int(cfgv.get("max_dim", 300))
    except (TypeError, ValueError):
        max_dim = 300









    try:
        user_min_contrast = float(cfgv.get("min_contrast", 48))
    except (TypeError, ValueError):
        user_min_contrast = 48.0
    if user_min_contrast > MIN_CONTRAST_BASE:
        min_contrast = user_min_contrast * adapt_scale * CONTRAST_METRIC_SCALE
    else:
        min_contrast = max(MIN_CONTRAST_FLOOR,
                           user_min_contrast * adapt_scale) \
            * CONTRAST_METRIC_SCALE
    max_aspect = 10.0





    frag_floor_split = 64

    finals = []
    labels, info = _label_cells(obj_cells)






    def _box_edge_dist(e):
        _cid, _count, _gy0, _gx0, _gy1, _gx1 = e
        return math.hypot(max(_gx0 * CS - cx, 0, cx - (_gx1 * CS - 1)),
                          max(_gy0 * CS - cy, 0, cy - (_gy1 * CS - 1)))
    try:
        info = sorted(info, key=_box_edge_dist)
    except Exception:
        pass
    pending = [(labels, info, 0)]
    guard = 0






    split_tries = 0
    SPLIT_TRY_CAP = 8





    detection_dropped = 0


    while pending and guard < 10:
        guard += 1

        if time.perf_counter() > deadline:
            detection_dropped += sum(len(inf) for _lab, inf, _d in pending)
            break

        lab, inf, depth = pending.pop()

        for cid, count, gy0, gx0, gy1, gx1 in inf:
            bw = (gx1 - gx0) * CS
            bh = (gy1 - gy0) * CS

            if depth < 2 and (bw > 200 or bh > 200 or count * CS * CS > 25000):
                if split_tries >= SPLIT_TRY_CAP:


                    finals.append((lab, cid, gy0, gx0, gy1, gx1))
                    continue
                split_tries += 1
                cmask = (lab == cid)


                sub_obj = cmask & ~_flood_bg(solids, cmask)
                slab, sinfo = _label_cells(sub_obj)

                if sinfo and not (len(sinfo) == 1 and sinfo[0][1] >= count * 0.9):
                    pending.append((slab, sinfo, depth + 1))
                    continue



                free = cmask & ~solids
                lf, lfi = _label_cells(free)
                big = np.zeros_like(cmask)
                big_min = max(30, int(0.40 * count))

                for fid, fcount, _a, _b, _c2, _d in lfi:
                    if fcount >= big_min:
                        big |= (lf == fid)

                sub_b = cmask & ~big
                lbb, ibi = _label_cells(sub_b)

                if ibi and not (len(ibi) == 1 and ibi[0][1] >= count * 0.9):
                    pending.append((lbb, ibi, depth + 1))
                    continue













                if bool(cfgv.get("subject_priority", False)):
                    core = _dilate(strong, 1) & cmask
                    slab2, sinfo2 = _label_cells(core)
                    kept = [e for e in sinfo2
                            if e[1] * CS * CS >= frag_floor_split]
                    if len(kept) > 1:
                        pending.append((slab2, kept, depth + 1))
                        continue

            finals.append((lab, cid, gy0, gx0, gy1, gx1))





    try:
        finals.sort(key=lambda e: math.hypot(
            max(e[3] * CS - cx, 0, cx - (e[5] * CS - 1)),
            max(e[2] * CS - cy, 0, cy - (e[4] * CS - 1))))
    except Exception:
        pass

    candidates = []
    fragments = []

    for _fi, (lab, cid, gy0, gx0, gy1, gx1) in enumerate(finals):
        if time.perf_counter() > deadline:
            detection_dropped += len(finals) - _fi
            break

        Y0, X0 = gy0 * CS, gx0 * CS
        Y1, X1 = min(gy1 * CS, h), min(gx1 * CS, w)

        if Y1 <= Y0 or X1 <= X0:
            continue

        bw, bh = X1 - X0, Y1 - Y0





        at_edge = _touches_screen_edge(Y0, X0, Y1, X1, w, h, edge_flags)

        dim_cap = max_dim * 2 if at_edge else max_dim
        aspect_cap = max_aspect * 2.5 if at_edge else max_aspect

        if max(bw, bh) > dim_cap:
            continue

        if max(bw, bh) > aspect_cap * max(1, min(bw, bh)):
            continue










        ddx = max(X0 - cx, 0, cx - (X1 - 1))
        ddy = max(Y0 - cy, 0, cy - (Y1 - 1))
        dist = math.hypot(ddx, ddy)

        if dist > radius:
            continue

        cl_cells = (lab[gy0:gy1, gx0:gx1] == cid)
        cl_px = np.repeat(np.repeat(cl_cells, CS, axis=0), CS, axis=1)
        cl_px = cl_px[:Y1 - Y0, :X1 - X0]

        area = int(cl_px.sum())

        if area > max_area:
            continue

        too_small = area < min_area







        frag_floor = min(MIN_FRAG_AREA, max(8, min_area * 3 // 4))
        if too_small and area < frag_floor:
            continue

        fill = area / float(bw * bh)

        if fill < MIN_FILL:










            if _touches_screen_edge(Y0, X0, Y1, X1, w, h, edge_flags):
                cl_px = np.ones((Y1 - Y0, X1 - X0), dtype=bool)
                area = int(cl_px.sum())
                if area > max_area:
                    continue
                fill = 1.0
            else:
                continue







        sub_d = diff[Y0:Y1, X0:X1]
        d_vals = sub_d[cl_px]

        if d_vals.size == 0:
            continue

        d_max = float(d_vals.max())
        core = d_vals >= (d_max * CONTRAST_CORE_FRAC)




        if d_vals.size > 4000:
            _dv = d_vals[::d_vals.size // 4000 + 1]
            _core = _dv >= (d_max * CONTRAST_CORE_FRAC)
        else:
            _dv, _core = d_vals, core
        if int(_core.sum()) >= 8:
            contrast = float(np.percentile(_dv[_core], 90))
        else:
            contrast = d_max * 0.7

        if contrast < min_contrast:
            continue

        score = _base_score(contrast, area, fill, dist,
                            shape=_shape_mult(bw, bh, area))
        entry = (score, Y0, X0, cl_px, dist, area, contrast, bw, bh)

        if too_small:
            fragments.append(entry)
        else:
            candidates.append(entry)







    if merge_distance > 0 and (fragments or len(candidates) > 1):
        candidates = _merge_nearby(
            candidates, fragments, merge_distance,
            min_area, max_area, max_dim, max_aspect, scale,
            cx=cx, cy=cy, w=w, h=h, edge_flags=edge_flags,
            deadline=deadline,
            subject_mode=bool(cfgv.get("subject_priority", False)),
        )












    candidates.sort(key=lambda c: c[4])












    if anchor_blind_area and anchor_blind_area > 0:


        blind_idx = {i for i, c in enumerate(candidates)
                     if c[2] <= cx < c[2] + c[7]
                     and c[1] <= cy < c[1] + c[8]
                     and c[5] <= anchor_blind_area}
        if blind_idx and len(blind_idx) < len(candidates):
            candidates = [c for i, c in enumerate(candidates)
                          if i not in blind_idx]














    subj_active = (bool(cfgv.get("subject_priority", False))
                   or bool(cfgv.get("subject_only", False)))
    if subj_active and candidates:
        hscores = []
        for c in candidates:
            try:
                hscores.append(_figure_score(c[7], c[8], c[5], c[3]))
            except Exception:
                hscores.append(0.0)
        tiers = [2 if s >= FIGURE_TIER2_THRESH
                 else (1 if (s >= FIGURE_TIER1_THRESH
                             or _shape_tier(c[7], c[8], c[5]) == 1)
                       else 0)
                 for c, s in zip(candidates, hscores)]
        rescored = []
        for c, s in zip(candidates, hscores):
            try:
                old_m = _shape_mult(c[7], c[8], c[5])
                new_m = _shape_mult(c[7], c[8], c[5], c[3])
                boost = (new_m / old_m) if old_m > 0 else new_m
                boost = min(1.6, max(1.0, boost)) if s >= 0.3 else 1.0
                if s >= FIGURE_TIER2_THRESH:
                    boost *= 1.0 + 0.35 * min(
                        1.0, (s - FIGURE_TIER2_THRESH)
                        / max(1e-6, 1.0 - FIGURE_TIER2_THRESH))
            except Exception:
                boost = 1.0
            rescored.append((c[0] * boost,) + c[1:])
        candidates = rescored
        sticky = None
        try:
            if subject_ctx is not None:
                (pmx, pmy, pbw, pbh, phs, pdecay) = subject_ctx
                if phs >= FIGURE_TIER2_THRESH and pdecay > 0:
                    sticky = (float(pmx), float(pmy), int(pbw), int(pbh),
                              float(phs), float(pdecay))
        except (TypeError, ValueError):
            sticky = None
        if sticky is not None:
            pmx, pmy, pbw, pbh, _phs, pdecay = sticky
            stuck = []
            for c in candidates:
                try:
                    ccx = c[2] + c[7] / 2.0
                    ccy = c[1] + c[8] / 2.0
                    d = math.hypot(ccx - pmx, ccy - pmy)
                    if d <= FIGURE_POS_PAD and pbw > 0 and pbh > 0:
                        sw = min(c[7], pbw) / float(max(c[7], pbw))
                        sh = min(c[8], pbh) / float(max(c[8], pbh))
                        size_sim = math.sqrt(max(0.0, sw * sh))
                    else:
                        size_sim = 0.0
                except Exception:
                    d, size_sim = 1e9, 0.0
                stuck.append((d, size_sim))
            boosted = []
            for c, s, tr, (d, size_sim) in zip(
                    candidates, hscores, tiers, stuck):
                if tr == 2 and d <= FIGURE_POS_PAD and size_sim >= 0.30:
                    prox = max(0.0, 1.0 - d / float(FIGURE_POS_PAD))
                    mult = 1.0 + FIGURE_STICKY_BOOST * pdecay \
                        * (0.4 + 0.6 * prox) * (0.5 + 0.5 * size_sim)
                    c = (c[0] * mult,) + c[1:]
                boosted.append(c)
            candidates = boosted
        best_tier = max(tiers) if tiers else 0
        best_h = max(hscores) if hscores else 0.0
        if best_tier == 2 and best_h >= FIGURE_SOLID_MIN:
            keep = []
            for c, tr, s in zip(candidates, tiers, hscores):
                if tr == 2:
                    keep.append(c)
                    continue
                if (sticky is not None and best_h < 0.65
                        and s >= FIGURE_KEEP_MIN - 0.10
                        and tr >= 1):
                    try:
                        ccx = c[2] + c[7] / 2.0
                        ccy = c[1] + c[8] / 2.0
                        d = math.hypot(ccx - sticky[0], ccy - sticky[1])
                    except Exception:
                        d = 1e9
                    if d <= FIGURE_POS_PAD:
                        keep.append(c)
            if keep:
                candidates = keep
        elif best_tier == 1:
            candidates = [c for c, tr in zip(candidates, tiers)
                          if tr == best_tier]






    try:
        _need = int(require_tier or 0)
    except (TypeError, ValueError):
        _need = 0
    if _need > 0 and candidates:
        kept = []
        for c in candidates:
            try:
                tr = _shape_tier(c[7], c[8], c[5], c[3])
            except Exception:
                tr = 0
            if tr >= _need:
                kept.append(c)
        candidates = kept

    if bool(cfgv.get("subject_only", False)) and candidates:
        kept = []
        for c in candidates:
            try:
                tr = _shape_tier(c[7], c[8], c[5], c[3])
            except Exception:
                tr = 0
            if tr == 2:
                kept.append(c)
        candidates = kept








    unscored = 0
    follow_skipped = 0

    accents = _cached_parse_accents(cfgv.get("accent_color", "off"))
    try:
        accent_boost = max(0.0, float(cfgv.get("accent_boost", 0.0) or 0.0))
    except (TypeError, ValueError):
        accent_boost = 0.0





    accent_only = bool(cfgv.get("accent_only", False))
    soft = accent_soft_of(cfgv)
    matcher = accent_matcher_of(cfgv)



    adapt = color_adapt_of(cfgv)
    variants = accent_variants_of(cfgv)
    coherent = accent_coherent_of(cfgv)
    try:
        tol = float(cfgv.get("accent_tolerance", 26))
    except (TypeError, ValueError):
        tol = 26.0

    if candidates and (accent_boost > 0 or accent_only):
        accented = []

        for cand in candidates:
            frac = 0.0
            strong = 0.0
            scored = time.perf_counter() <= deadline
            if scored and accents:
                frac, strong = _accent_stats(
                    img, cand[3], cand[2], cand[1], accents, tol, soft,
                    matcher, coherent, adapt, variants)
            else:
                unscored += 1







            if accent_only and (not scored or strong < ACCENT_MIN_FRACTION):
                continue

            mult = 1.0
            if frac > 0 and accent_boost > 0:
                mult = min(1.0 + accent_boost * frac, ACCENT_RANK_CAP)
            if mult != 1.0:
                cand = (cand[0] * mult,) + cand[1:]
            accented.append(cand)

        candidates = accented














    excludes = _cached_parse_accents(cfgv.get("exclude_color", "off"))
    if excludes and candidates:
        kept = []

        for cand in candidates:
            strong = 0.0
            scored = time.perf_counter() <= deadline
            if scored:




                if int(cand[3].sum()) < EXCLUDE_CORE_MIN:
                    core = cand[3]
                    use_core = False
                else:
                    core = _erode(cand[3], EXCLUDE_ERODE_PX)
                    use_core = int(core.sum()) >= EXCLUDE_CORE_MIN
                    if not use_core:
                        core = cand[3]
                _mean, strong = _accent_stats(
                    img, core, cand[2], cand[1], excludes, tol, soft,
                    matcher, coherent, adapt, variants,
                    bg_from=cand[3] if use_core else None)
            else:
                unscored += 1















            if not scored or strong >= ACCENT_MIN_FRACTION:
                continue
            kept.append(cand)

        candidates = kept



    if follow_ctx is not None and candidates:
        f_boost = float(cfgv.get("follow_boost", 1.0) or 0.0)
        try:
            sig, mem_x, mem_y, decay = follow_ctx
        except (TypeError, ValueError):
            sig, mem_x, mem_y, decay = None, 0, 0, 0.0

        if f_boost > 0 and decay > 0:
            followed = []

            for cand in candidates:
                sim = 0.0

                if sig is not None:
                    if time.perf_counter() <= deadline:







                        frac = _accent_fraction(img, cand[3], cand[2],
                                                cand[1], [sig],
                                                FOLLOW_COLOR_TOL,
                                                soft=soft, matcher=matcher,
                                                coherent=coherent,
                                                adapt=adapt,
                                                variants=variants)
                        if frac >= FOLLOW_COLOR_FRAC:
                            sim = frac
                    else:




                        follow_skipped += 1






                bx0, by0 = cand[2], cand[1]
                bx1, by1 = bx0 + cand[7], by0 + cand[8]
                if (bx0 - FOLLOW_POS_PAD <= mem_x <= bx1 + FOLLOW_POS_PAD
                        and by0 - FOLLOW_POS_PAD <= mem_y <= by1 + FOLLOW_POS_PAD):
                    sim = max(sim, FOLLOW_POS_SIM)

                if sim > 0:
                    cand = (cand[0] * (1.0 + f_boost * decay * sim),) + cand[1:]
                followed.append(cand)

            candidates = followed








    if motion_ctx is not None and candidates:
        try:
            _mmask, _mboost = motion_ctx
        except (TypeError, ValueError):
            _mmask, _mboost = None, 0.0
        if _mmask is not None and _mboost > 0:
            _moved = []
            for _cand in candidates:
                _frac = 0.0
                try:
                    _mY0, _mX0 = _cand[1], _cand[2]
                    _mh, _mw = _cand[3].shape
                    _win = _mmask[_mY0:_mY0 + _mh, _mX0:_mX0 + _mw]
                    if _win.shape == _cand[3].shape and _win.size:
                        _npx = int(_cand[3].sum())
                        if _npx > 0:
                            _frac = float(np.logical_and(
                                _cand[3], _win).sum()) / _npx
                except Exception:
                    _frac = 0.0
                if _frac > 0:
                    _cand = (_cand[0] * min(1.0 + _mboost * _frac,
                                            1.0 + _mboost),) + _cand[1:]
                _moved.append(_cand)
            candidates = _moved

    def _report_drops():











        if not (unscored or detection_dropped or follow_skipped):
            return
        tail = ""
        if follow_skipped:
            tail += " (%d follow nudge(s) skipped)" % follow_skipped
        if (accents and accent_only) or excludes:
            tail += (" - accent_only and exclude_color fail closed for "
                     "unscored items")
        else:
            tail += (" - unscored items were kept (no fail-closed "
                     "colour gate configured)")
        _log("Colour stage cut short: %d item(s) went unscored and %d "
             "unmeasured in detection before the %.0f ms scan budget "
             "ran out%s. Lower the search radius, or raise the scan "
             "budget, if this repeats."
             % (unscored, detection_dropped,
                (deadline - t0) * 1000.0, tail))

    ms = (time.perf_counter() - t0) * 1000.0

    if not candidates:
        _report_drops()
        return ScanResult("none", None, ms)






    region_area = float(h * w)
    boosted = []

    for score, Y0, X0, cl_px, dist, area, contrast, bw, bh in candidates:
        clipped = _is_clipped(Y0, X0, bw, bh, w, h, edge_flags)
        vis = min(0.5, area / region_area)
        score *= (1.0 + vis) * (0.6 if clipped else 1.0)
        boosted.append((score, Y0, X0, cl_px, dist, area, contrast, bw, bh))

    candidates = boosted
    candidates.sort(key=lambda c: -c[0])

    if collect is not None:




        for _s, _Y0, _X0, _cl, _d, _a, _ct, _bw, _bh in candidates:
            _ys, _xs = np.nonzero(_cl)
            if _xs.size == 0:
                continue
            _pt = _meanshift(_xs.astype(np.float32) + _X0,
                             _ys.astype(np.float32) + _Y0, cx, cy)
            if _pt is None:
                _ddx = _X0 + _bw / 2.0 - cx
                _ddy = _Y0 + _bh / 2.0 - cy
            else:
                _ddx, _ddy = _pt[0] - cx, _pt[1] - cy
            collect.append((_s, _ddx, _ddy, _d, _a, _ct, _bw, _bh))








    for _s, Y0, X0, cl_px, dist, area, contrast, bw, bh in candidates:
        if time.perf_counter() > deadline:
            detection_dropped += 1
            break
        try:
            _hs = _figure_score(bw, bh, area, cl_px)
        except Exception:
            _hs = 0.0
        ys, xs = np.nonzero(cl_px)
        if xs.size > 4000:
            step = xs.size // 4000 + 1
            xs, ys = xs[::step], ys[::step]
        xs = xs.astype(np.float32) + X0
        ys = ys.astype(np.float32) + Y0

        pt = _meanshift(xs, ys, cx, cy)

        if pt is None:




            tcx, tcy = X0 + bw / 2.0, Y0 + bh / 2.0
        else:
            tcx, tcy = pt
        try:
            _tier_now = 2 if _hs >= FIGURE_TIER2_THRESH else _shape_tier(bw, bh, area, cl_px)
        except Exception:
            _tier_now = 0
        if focus_bias > 0.0 and _tier_now == 2:





            tcy -= max(0.0, tcy - Y0) * min(1.0, focus_bias)
        ddx = tcx - cx
        ddy = tcy - cy
        move = math.hypot(ddx, ddy)

        if move > radius + 10:
            continue

        ms = (time.perf_counter() - t0) * 1000.0




        sig = None
        try:
            body = img[Y0:Y0 + cl_px.shape[0], X0:X0 + cl_px.shape[1]][cl_px]
            if body.shape[0] > 0:
                med = np.median(body.astype(np.float32), axis=0)
                sig = _parse_accent("%d,%d,%d"
                                    % (int(med[2]), int(med[1]), int(med[0])))
        except Exception:
            sig = None

        target = SnapTarget(
            int(round(ddx)),
            int(round(ddy)),
            move,
            area,
            contrast,
            bw,
            bh,
            sig,
            _hs,
        )

        return ScanResult("target", target, ms)

    ms = (time.perf_counter() - t0) * 1000.0
    _report_drops()
    if _hpose_enabled(cfgv) and bool(cfgv.get("blob_watch", True)):
        try:
            _bt = _blob_watch(img, cx, cy, radius)
        except Exception as exc:
            _log("blob watch failed: %r" % (exc,))
            _bt = None
        if _bt is not None:
            return ScanResult("target", _bt,
                              (time.perf_counter() - t0) * 1000.0)


    return ScanResult("none", None, ms)


def draw_debug_overlay(img, cx, cy, radius, candidates, chosen_idx=None):
    vis = img.copy()
    h, w = vis.shape[:2]

    try:
        import cv2
        cv2.circle(vis, (int(cx), int(cy)), int(radius), (0, 255, 255), 2, cv2.LINE_AA)
    except Exception:
        y, x = np.ogrid[:h, :w]
        mask = (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2
        edge = mask & ~(
            ((np.roll(mask, 1, axis=0) | np.roll(mask, -1, axis=0) |
              np.roll(mask, 1, axis=1) | np.roll(mask, -1, axis=1)) & mask)
        )
        vis[edge] = [0, 255, 255]

    for i, c in enumerate(candidates):
        _s, Y0, X0, cl_px, dist, area, contrast, bw, bh = c
        x1, y1 = int(X0), int(Y0)
        x2, y2 = int(X0 + bw), int(Y0 + bh)
        color = (0, 255, 0) if i == chosen_idx else (0, 255, 255)
        try:
            import cv2
            cv2.rectangle(vis, (x1, y1), (x2, y2), color, 2)
            cv2.putText(vis, f"{i}:{_s:.2f}", (x1, max(0, y1 - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA)
        except Exception:
            vis[y1:y1+2, x1:x2] = color
            vis[y2-2:y2, x1:x2] = color
            vis[y1:y2, x1:x1+2] = color
            vis[y1:y2, x2-2:x2] = color

    ch = 15
    try:
        import cv2
        cv2.line(vis, (int(cx) - ch, int(cy)), (int(cx) + ch, int(cy)), (255, 0, 255), 2)
        cv2.line(vis, (int(cx), int(cy) - ch), (int(cx), int(cy) + ch), (255, 0, 255), 2)
    except Exception:
        vis[max(0, int(cy)-1):int(cy)+2, max(0, int(cx)-ch):int(cx)+ch] = [255, 0, 255]
        vis[max(0, int(cy)-ch):int(cy)+ch, max(0, int(cx)-1):int(cx)+2] = [255, 0, 255]

    return vis


