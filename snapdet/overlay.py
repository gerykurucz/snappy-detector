"""The on-screen overlay: vectors, not video.

A borderless, click-through-friendly, always-on-top window that sits
exactly over the captured region and draws the engine's state as
transparent-window vectors: the aim disc, per-source detection boxes,
motion blobs, rejections, memory candidates, track boxes with labels,
the assist crosshair and a one-line HUD.  No frames are stored,
encoded or written anywhere - everything is drawn, instantly, where
the pixels came from.

Implementation: tkinter (standard library) with a transparent
colorkey on Windows; on platforms without colorkey support the
window falls back to opaque black, still borderless and on top.
tkinter runs in the main thread; the engine runs in its worker
thread and publishes its latest state, which the overlay polls at
~100 Hz.  Mouse buttons on the overlay window are the operator
select/hold authorisation (see snapdet.events).

``overlay_primitives`` is a pure function (state -> draw list) so the
drawing rules are testable headless and reusable by any renderer;
``render_cv2`` is the raster twin used by the fallback window view
and the acceptance tests.
"""

from __future__ import annotations

from .debug import SOURCE_COLOUR, ROI_COLOUR, REJECT_COLOUR, \
    SWEEP_COLOUR, TRACK_COLOUR, VIEW_COLOURS


def _bgr_hex(c):
    b, g, r = c
    return "#%02x%02x%02x" % (r, g, b)


def overlay_primitives(debug: dict, tracks, sweep_windows: bool = False):
    """State -> list of draw primitives, in disc/region coordinates.

    Kinds: rect, line, cross, circle, text.  Colours are BGR tuples,
    shared with :mod:`snapdet.debug` so both views agree.
    """
    out = []
    circ = debug.get("circle")
    if circ:
        cx, cy, R = circ
        out.append({"kind": "circle", "c": (cx, cy), "r": R,
                    "color": (0, 255, 255), "w": 1})
        out.append({"kind": "cross", "p": (cx, cy),
                    "color": (0, 255, 255), "s": 6, "w": 1})
    if sweep_windows:
        for (x, y, w, z, n) in debug.get("sweep_windows", []):
            out.append({"kind": "rect", "box": (x, y, x + w, y + w),
                        "color": SWEEP_COLOUR, "w": 1})
    for r in debug.get("rois", []):
        out.append({"kind": "rect", "box": r["box"], "color": ROI_COLOUR,
                    "w": 1})
    for (box, best) in debug.get("rejected", []):
        out.append({"kind": "rect", "box": box, "color": REJECT_COLOUR,
                    "w": 1})
        out.append({"kind": "text", "p": (box[0], max(9, box[1] - 2)),
                    "s": "rej %.2f" % best, "color": REJECT_COLOUR,
                    "size": 8})
    for (cx, cy, acc, hits) in debug.get("memory", []):
        out.append({"kind": "circle", "c": (cx, cy), "r": 3,
                    "color": (255, 255, 0), "w": 1})
        out.append({"kind": "text", "p": (cx + 4, cy - 3),
                    "s": "%.2f/%d" % (acc, hits), "color": (255, 255, 0),
                    "size": 8})
    for d in debug.get("dets", []):
        colour = SOURCE_COLOUR.get(d.get("source"), (200, 200, 200))
        out.append({"kind": "rect", "box": d["box"], "color": colour,
                    "w": 1})
        out.append({"kind": "text",
                    "p": (d["box"][0], max(9, d["box"][3] + 10)),
                    "s": "%s %.2f h%d" % (d.get("source", "?")[:4],
                                          d["score"],
                                          round(d["box"][3] - d["box"][1])),
                    "color": colour, "size": 8})
    for tr in tracks:
        colour = VIEW_COLOURS.get(tr.view, TRACK_COLOUR)
        out.append({"kind": "rect", "box": tr.box, "color": TRACK_COLOUR,
                    "w": 2})
        cx, cy = tr.center
        vx, vy = tr.velocity
        if abs(vx) > 1.0 or abs(vy) > 1.0:
            out.append({"kind": "line",
                        "a": (cx, cy), "b": (cx + vx * 0.25, cy + vy * 0.25),
                        "color": colour, "w": 1})
        label = "#%d %s/%s h%d" % (tr.id, tr.view, tr.stance,
                                   round(tr.box[3] - tr.box[1]))
        if getattr(tr, "truncated", False):
            label += " [trunc]"
        if getattr(tr, "gated", False):
            label += " [gated]"
        if tr.misses:
            label += " coast%d" % tr.misses
        out.append({"kind": "text", "p": (tr.box[0], max(10, tr.box[1] - 4)),
                    "s": label, "color": colour, "size": 9})
    assist = debug.get("assist")
    if assist:
        ax, ay, tid, held = assist
        col = (80, 80, 255) if held else (80, 200, 80)
        out.append({"kind": "cross", "p": (ax, ay), "color": col,
                    "s": 9, "w": 2})
        out.append({"kind": "text", "p": (ax + 10, ay - 6),
                    "s": "SEL #%d%s" % (tid, " HOLD" if held else ""),
                    "color": col, "size": 9})
    hud = ["%.0f ms  passes %d  tracks %d"
           % (debug.get("ms", 0.0), debug.get("passes", 0), len(tracks)),
           "rois %d  rej %d  mem %d  %s"
           % (len(debug.get("rois", [])), len(debug.get("rejected", [])),
              len(debug.get("memory", [])),
              debug.get("sweep_progress", "-"))]
    y = 10
    for ln in hud:
        out.append({"kind": "text", "p": (4, y), "s": ln,
                    "color": (255, 255, 255), "size": 9})
        y += 12
    return out


def render_cv2(img, prims):
    """Raster twin of the primitives, for the window view and tests."""
    import cv2
    out = img
    for p in prims:
        c = p["color"]
        if p["kind"] == "rect":
            x1, y1, x2, y2 = [int(v) for v in p["box"]]
            cv2.rectangle(out, (x1, y1), (x2, y2), c, p["w"])
        elif p["kind"] == "line":
            cv2.line(out, (int(p["a"][0]), int(p["a"][1])),
                     (int(p["b"][0]), int(p["b"][1])), c, p["w"],
                     cv2.LINE_AA)
        elif p["kind"] == "cross":
            cv2.drawMarker(out, (int(p["p"][0]), int(p["p"][1])), c,
                           cv2.MARKER_CROSS, p["s"] * 2, p["w"])
        elif p["kind"] == "circle":
            cv2.circle(out, (int(p["c"][0]), int(p["c"][1])), int(p["r"]),
                       c, p["w"])
        elif p["kind"] == "text":
            x, y = int(p["p"][0]), int(p["p"][1])
            cv2.putText(out, p["s"], (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                        0.34, (0, 0, 0), p.get("size", 9) // 3 + 2,
                        cv2.LINE_AA)
            cv2.putText(out, p["s"], (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                        0.34, c, 1, cv2.LINE_AA)
    return out


class TkOverlay(object):
    """Transparent, borderless, always-on-top overlay window.

    Sits over the captured region at 1:1 screen scale and redraws the
    primitives at ~100 Hz from the engine loop's latest state.  Mouse
    left button on the window is the operator authorisation: press =
    select (click magnet), hold = assisted hold events, release =
    release.  Escape (or q) stops everything.
    """

    POLL_MS = 10

    def __init__(self, loop, x=None, y=None, w=None, h=None):
        import tkinter as tk
        self.tk = tk
        self.loop = loop
        self.root = tk.Tk()
        self.root.title("snapdet overlay")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        if w is None or h is None:
            w, h = 2 * 80, 2 * 80
        if x is None:
            x = (sw - w) // 2
        if y is None:
            y = (sh - h) // 2
        self.w, self.h = int(w), int(h)
        self.root.geometry("%dx%d+%d+%d" % (self.w, self.h, int(x), int(y)))
        self.transparent = False
        try:
            self.root.attributes("-transparentcolor", "magenta")
            bg = "magenta"
            self.transparent = True
        except Exception:
            bg = "black"
        self.canvas = tk.Canvas(self.root, width=self.w, height=self.h,
                                bg=bg, highlightthickness=0)
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self._down)
        self.canvas.bind("<ButtonRelease-1>", self._up)
        self.canvas.bind("<Motion>", self._move)
        self.root.bind("<Escape>", lambda e: self.stop())
        self.root.bind("<q>", lambda e: self.stop())
        self._stop = False

    # -- operator mouse -> the loop's interaction state ----------------
    def _down(self, ev):
        m = self.loop.mouse
        m["x"], m["y"] = ev.x, ev.y
        m["lmb"] = True
        m["click"] = (ev.x, ev.y)

    def _up(self, ev):
        m = self.loop.mouse
        m["lmb"] = False
        m["release"] = True

    def _move(self, ev):
        self.loop.mouse["x"], self.loop.mouse["y"] = ev.x, ev.y

    # -- redraw ----------------------------------------------------------
    def _poll(self):
        if self._stop:
            return
        st = self.loop.last
        self.canvas.delete("all")
        if st.get("debug") is not None:
            for p in overlay_primitives(st["debug"], st["tracks"]):
                self._draw(p)
        self.root.after(self.POLL_MS, self._poll)

    def _draw(self, p):
        c = _bgr_hex(p["color"])
        k = p["kind"]
        if k == "rect":
            x1, y1, x2, y2 = p["box"]
            self.canvas.create_rectangle(x1, y1, x2, y2, outline=c,
                                         width=p["w"])
        elif k == "line":
            self.canvas.create_line(p["a"][0], p["a"][1], p["b"][0],
                                    p["b"][1], fill=c, width=p["w"])
        elif k == "cross":
            x, y = p["p"]
            s = p["s"]
            self.canvas.create_line(x - s, y, x + s, y, fill=c,
                                    width=p["w"])
            self.canvas.create_line(x, y - s, x, y + s, fill=c,
                                    width=p["w"])
        elif k == "circle":
            x, y = p["c"]
            r = p["r"]
            self.canvas.create_oval(x - r, y - r, x + r, y + r, outline=c,
                                    width=p["w"])
        elif k == "text":
            self.canvas.create_text(p["p"][0], p["p"][1], text=p["s"],
                                    fill=c, anchor=self.tk.NW,
                                    font=("TkFixedFont", p.get("size", 9)))

    # -- lifecycle ---------------------------------------------------------
    def mainloop(self):
        self.root.after(self.POLL_MS, self._poll)
        self.root.mainloop()

    def stop(self):
        if self._stop:
            return
        self._stop = True
        self.loop.stop()
        try:
            self.root.destroy()
        except Exception:
            pass
