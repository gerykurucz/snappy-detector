"""The development overlay: show every decision the engine made.

Everything the engine touched in a frame gets drawn, colour-coded by
the path that found it:

    grey thin box    a sweep window inspected this frame (label = zoom)
    yellow box       a motion-compensated difference blob (candidate)
    red box + score  a candidate the zoom inspection REJECTED
    magenta box      a detection born from motion-gated inspection
    blue box         a detection from the zoom sweep
    green box        a detection from the coarse full-frame pass
    white box + ID   a live track: view/stance/height/speed, velocity
                     arrow and the lead point 150 ms ahead
    skeleton         keypoints, only when the body passed the size gate

The HUD top-left carries the frame cost, passes used, sweep progress
and track counts.  With ``--json`` on the CLI the same information goes
to a file; this overlay is the human-readable twin.
"""

from __future__ import annotations

import numpy as np

SOURCE_COLOUR = {
    "coarse": (80, 220, 80),     # green
    "sweep": (255, 150, 40),     # blue-orange
    "inspect": (255, 60, 255),   # magenta
    "memory": (255, 255, 0),     # cyan - promoted by accumulated evidence
    "context": (40, 160, 255),   # orange - circle-mode border-context pass
}
ROI_COLOUR = (0, 220, 255)       # yellow
REJECT_COLOUR = (60, 60, 220)    # red
TRACK_COLOUR = (255, 255, 255)   # white
SWEEP_COLOUR = (140, 140, 140)   # grey

VIEW_COLOURS = {
    "front": (80, 220, 80),
    "front_left_45": (60, 200, 160),
    "front_right_45": (60, 160, 220),
    "side_left": (60, 200, 240),
    "side_right": (240, 200, 60),
    "back_left_45": (200, 120, 240),
    "back_right_45": (240, 120, 200),
    "back": (80, 80, 240),
    "unknown": (200, 200, 200),
}


def _label(img, text, pos, colour, scale=0.42, thickness=1):
    import cv2
    x, y = int(pos[0]), int(pos[1])
    cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale,
                (0, 0, 0), thickness + 2, cv2.LINE_AA)
    cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale,
                colour, thickness, cv2.LINE_AA)


def _box(img, box, colour, thickness=1):
    import cv2
    x1, y1, x2, y2 = [int(v) for v in box]
    cv2.rectangle(img, (x1, y1), (x2, y2), colour, thickness)


def _skeleton(img, kps, colour, thr=0.25):
    import cv2
    from .keypoints import SKELETON
    for a, b in SKELETON:
        if kps[a][2] < thr or kps[b][2] < thr:
            continue
        cv2.line(img, (int(kps[a][0]), int(kps[a][1])),
                 (int(kps[b][0]), int(kps[b][1])), colour, 1, cv2.LINE_AA)


def draw_debug(frame, debug: dict, tracks, lead_s: float = 0.15,
               show: str = "all"):
    """Render one frame of engine state.  Returns a copy."""
    import cv2
    out = frame.copy()
    what = set(show.split(",")) if show != "all" else {
        "sweep", "roi", "reject", "dets", "tracks", "hud", "circle"}

    # 0. the aim disc, in circle mode
    circ = debug.get("circle")
    if circ and "circle" in what:
        cx, cy, R = circ
        cv2.circle(out, (int(cx), int(cy)), int(R), (0, 255, 255), 1)
        cv2.drawMarker(out, (int(cx), int(cy)), (0, 255, 255),
                       cv2.MARKER_CROSS, 8, 1)

    # 1. sweep windows looked at this frame
    if "sweep" in what:
        for (x, y, win, zoom, nfound) in debug.get("sweep_windows", []):
            _box(out, (x, y, x + win, y + win), SWEEP_COLOUR, 1)
            _label(out, "z%d" % zoom, (x + 2, y + 11), SWEEP_COLOUR, 0.35)

    # 2. motion blobs (candidates) + evidence memory
    if "roi" in what:
        for r in debug.get("rois", []):
            _box(out, r["box"], ROI_COLOUR, 1)
        for (cx, cy, acc, hits) in debug.get("memory", []):
            cv2.circle(out, (int(cx), int(cy)), 3, (255, 255, 0), 1)
            _label(out, "%.2f/%d" % (acc, hits), (int(cx) + 4, int(cy) - 4),
                   (255, 255, 0), 0.33)

    # 3. rejected inspections
    if "reject" in what:
        for (box, best) in debug.get("rejected", []):
            _box(out, box, REJECT_COLOUR, 1)
            _label(out, "rej %.2f" % best,
                   (box[0], max(10, box[1] - 3)), REJECT_COLOUR, 0.35)

    # 4. merged detections, colour-coded by source
    if "dets" in what:
        for d in debug.get("dets", []):
            colour = SOURCE_COLOUR.get(d.get("source"), (200, 200, 200))
            _box(out, d["box"], colour, 1)
            h = d["box"][3] - d["box"][1]
            _label(out, "%s %.2f h%d" % (d.get("source", "?")[:4],
                                         d["score"], round(h)),
                   (d["box"][0], max(10, d["box"][3] + 11)), colour, 0.35)
            if not d.get("size_gated") and d.get("kps") is not None:
                _skeleton(out, d["kps"], colour)

    # 5. tracks
    if "tracks" in what:
        for tr in tracks:
            colour = VIEW_COLOURS.get(tr.view, TRACK_COLOUR)
            _box(out, tr.box, TRACK_COLOUR, 2)
            cx, cy = tr.center
            vx, vy = tr.velocity
            if abs(vx) > 1.0 or abs(vy) > 1.0:
                cv2.arrowedLine(out, (int(cx), int(cy)),
                                (int(cx + vx * 0.25), int(cy + vy * 0.25)),
                                colour, 1, tipLength=0.3)
                lx, ly, _, _ = tr.lead(lead_s)
                cv2.drawMarker(out, (int(lx), int(ly)), (0, 255, 255),
                               cv2.MARKER_CROSS, 10, 1)
            label = "#%d %s/%s h%d" % (tr.id, tr.view, tr.stance,
                                       round(tr.box[3] - tr.box[1]))
            if tr.truncated:
                label += " [trunc]"
            if tr.gated:
                label += " [gated:%s]" % (tr.source or "?")
            if tr.misses:
                label += " coast%d" % tr.misses
            _label(out, label, (tr.box[0], max(11, tr.box[1] - 5)), colour)

    # 6. HUD
    assist = debug.get("assist")
    if assist:
        ax, ay, tid, held = assist
        col = (80, 80, 255) if held else (80, 200, 80)
        cv2.drawMarker(out, (int(ax), int(ay)), col, cv2.MARKER_CROSS,
                       18, 2)
        _label(out, "SEL #%d%s" % (tid, " HOLD" if held else ""),
               (int(ax) + 10, int(ay) - 8), col)
    if "hud" in what:
        n_conf = sum(1 for tr in tracks if tr.confirmed)
        lines = [
            "frame %.0f ms  passes %d" % (debug.get("ms", 0.0),
                                          debug.get("passes", 0)),
            "tracks %d (%d confirmed)  dets %d" % (len(tracks), n_conf,
                                                   len(debug.get("dets", []))),
            "motion ROIs %d  rejected %d  memory %d"
            % (len(debug.get("rois", [])), len(debug.get("rejected", [])),
               len(debug.get("memory", []))),
            "sweep %s" % debug.get("sweep_progress", "-"),
        ]
        y = 16
        for ln in lines:
            _label(out, ln, (6, y), (255, 255, 255), 0.45)
            y += 16
    return out
