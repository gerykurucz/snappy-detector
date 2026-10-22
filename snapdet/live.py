"""The real-time loop.  Live on screen, never to a file.

Three moving parts:

* **capture thread** - grabs frames as fast as the source presents
  them and keeps only the NEWEST one.  A queue would be wrong here:
  queued frames are latency, and a tracker wants the present, not the
  past.
* **engine loop** - processes the newest frame, never the same frame
  twice, and adapts its own workload: a small controller watches the
  median frame cost and moves the pass budget so the loop holds the
  requested rate on whatever machine it runs on.  ONNX Runtime
  releases the GIL during a pass, so capture and inference overlap.
* **view** - where the answer goes, instantly:
    ``overlay``  a transparent, borderless, always-on-top window over
                 the captured region (snapdet.overlay); the loop runs
                 in a worker thread and the overlay polls the latest
                 state at ~100 Hz, so drawing never waits on a pass;
    ``window``   an OpenCV viewer window (upscaled disc in circle
                 mode) - the fallback where transparency is unusable;
    ``none``     headless: callbacks / event bus / JSON log only.

Nothing is recorded: no VideoWriter, no frame files.  ``--json`` logs
track/event records for debugging and is the only file output left.
"""

from __future__ import annotations

import threading
import time

import numpy as np


class _Slot(object):
    """Single-frame mailbox: writer replaces, reader takes-if-new."""

    def __init__(self):
        self.lock = threading.Lock()
        self.frame = None
        self.seq = 0
        self.t = 0.0

    def put(self, frame):
        with self.lock:
            self.seq += 1
            self.frame = frame
            self.t = time.perf_counter()

    def take(self, last_seq):
        with self.lock:
            if self.seq == last_seq:
                return None, last_seq, 0.0
            return self.frame, self.seq, self.t


class BudgetController(object):
    """Keeps the loop at the requested rate by moving the pass budget.

    The engine's budget is its quality knob: fewer passes means the
    sweep and inspections cover less per frame (the tracker and the
    temporal memory bridge the gaps), more passes means deeper recall.
    The controller nudges it every ``window`` frames or every 3 s,
    whichever comes first, based on the median frame cost.
    """

    def __init__(self, engine, target_fps: float, min_budget: int = 3,
                 max_budget: int = None, window: int = 15):
        self.engine = engine
        self.target_ms = 1000.0 / max(1.0, float(target_fps))
        self.min_budget = int(min_budget)
        cfg = engine.cfg
        self.max_budget = int(max_budget or (cfg.pass_budget_circle
                                             if engine.circle
                                             else cfg.pass_budget))
        self.window = int(window)
        self.costs = []
        self.history = []
        self._last_adapt = time.perf_counter()
        self._apply(self.max_budget)

    def _apply(self, budget):
        budget = int(max(self.min_budget, min(self.max_budget, budget)))
        cfg = self.engine.cfg
        if self.engine.circle:
            cfg.pass_budget_circle = budget
        else:
            cfg.pass_budget = budget
        # inspections may never eat the whole budget: the sweep must
        # keep its tiles or stationary small targets starve
        cfg.inspect_budget = max(1, min(4, budget - 2))
        self.budget = budget
        return budget

    def observe(self, ms: float):
        self.costs.append(ms)
        now = time.perf_counter()
        by_frames = len(self.costs) >= self.window
        by_time = (now - self._last_adapt) >= 3.0 and len(self.costs) >= 3
        if not (by_frames or by_time):
            return self.budget
        med = float(np.median(self.costs))
        self.costs = []
        self._last_adapt = now
        if med > self.target_ms * 2.0:
            # badly over budget: cut proportionally, a slow machine
            # cannot wait out a linear descent
            self._apply(self.budget - max(2, self.budget // 3))
        elif med > self.target_ms * 1.08 and self.budget > self.min_budget:
            self._apply(self.budget - 1)
        elif med < self.target_ms * 0.72 and self.budget < self.max_budget:
            self._apply(self.budget + 1)
        self.history.append((self.budget, round(med, 1)))
        return self.budget


class RealtimeLoop(object):
    """Capture thread + engine loop + a live view (no recording)."""

    def __init__(self, source, engine, target_fps: float = 30.0,
                 view: str = "none", view_zoom: float = 4.0,
                 json_path: str = None, on_tracks=None,
                 max_seconds: float = 0.0, bus=None,
                 select_radius: float = 48.0):
        self.source = source
        self.engine = engine
        self.slot = _Slot()
        self.view = view
        self.view_zoom = float(view_zoom)
        self.json_path = json_path
        self.on_tracks = on_tracks
        self.max_seconds = float(max_seconds)
        self.ctrl = BudgetController(engine, target_fps)
        self.running = False
        self._thread = None          # capture thread
        self._worker = None          # engine thread (overlay view)
        self.stats_out = {"processed": 0, "costs": [], "tracks_seen": 0,
                          "started": 0.0, "ended": 0.0}
        # latest processed state, polled by the overlay
        self.last = {"frame": None, "tracks": [], "debug": None}
        # operator selection (click / hold mouse1), fed by whichever
        # view is on screen - see snapdet.events
        from .events import EventBus
        self.bus = bus if bus is not None else EventBus()
        self.select_radius = float(select_radius)
        self.mouse = {"x": 0, "y": 0, "lmb": False,
                      "click": None, "release": False}
        self.assist = None
        self.holding = False

    # -- operator mouse ---------------------------------------------------
    def _interact(self, tracks, t, zoom=1.0):
        from .events import SelectEvent, pick_track
        m = self.mouse
        mx, my = m["x"] / zoom, m["y"] / zoom
        click = None
        if m["click"] is not None:
            click = (m["click"][0] / zoom, m["click"][1] / zoom)
            m["click"] = None
        if click is not None:
            cx, cy = click
            tr = pick_track(tracks, cx, cy, self.select_radius)
            if tr is not None:
                px, py = tr.center
                self.assist = (px, py, tr.id)
                self.bus.emit(SelectEvent(
                    "select", t, int(px), int(py), int(cx), int(cy),
                    tr.id, tr.view, tr.stance, tr.box[3] - tr.box[1],
                    tr.source))
            else:
                self.assist = None
                self.bus.emit(SelectEvent("select", t, int(cx), int(cy),
                                          int(cx), int(cy), -1))
        if m["release"]:
            m["release"] = False
            if self.holding:
                self.holding = False
                ax, ay, tid = self.assist or (mx, my, -1)
                self.bus.emit(SelectEvent("release", t, int(ax), int(ay),
                                          int(mx), int(my), tid))
                self.assist = None
        if m["lmb"]:
            tr = pick_track(tracks, mx, my, self.select_radius)
            if tr is not None:
                self.holding = True
                px, py = tr.center
                self.assist = (px, py, tr.id)
                self.bus.emit(SelectEvent(
                    "hold", t, int(px), int(py), int(mx), int(my), tr.id,
                    tr.view, tr.stance, tr.box[3] - tr.box[1], tr.source))

    # -- capture side ------------------------------------------------------
    def _capture(self):
        src = self.source
        last = 0.0
        while self.running:
            f = src.read()
            if f is None:
                if not getattr(src, "alive", True):
                    break          # finite source exhausted: drain
                time.sleep(0.001)
                continue
            if src.pacing > 0.0:
                # file/synthetic sources would otherwise spin a core at
                # full tilt and starve the inference threads; live
                # screen sources pace themselves
                now = time.perf_counter()
                wait = src.pacing - (now - last)
                if wait > 0.0:
                    time.sleep(wait)
                last = time.perf_counter()
            else:
                last = time.perf_counter()
            self.slot.put(f)
        self.slot.put(self.slot.frame)

    # -- engine side --------------------------------------------------------
    def _step_body(self, frame, seq, t_now):
        tracks = self.engine.process(frame, t=t_now)
        cost = (time.perf_counter() - t_now) * 1000.0
        self.stats_out["costs"].append(cost)
        self.ctrl.observe(cost)
        self.stats_out["processed"] += 1
        self.stats_out["tracks_seen"] = max(self.stats_out["tracks_seen"],
                                            len(tracks))
        dbg = self.engine.last_debug
        dbg["assist"] = (self.assist[0], self.assist[1], self.assist[2],
                         self.holding) if self.assist else None
        self.last = {"frame": frame, "tracks": tracks, "debug": dbg}
        if self.on_tracks:
            self.on_tracks(tracks, dbg)
        return tracks, dbg, cost

    def run(self):
        """Blocking engine loop (views 'none' and 'window')."""
        import cv2
        from .debug import draw_debug
        self.running = True
        win_name = None
        if self.view == "window":
            win_name = "snapdet live"
            cv2.namedWindow(win_name)
            cv2.setMouseCallback(win_name, self._cv_mouse)
        self._thread = threading.Thread(target=self._capture, daemon=True)
        self._thread.start()
        t0 = time.perf_counter()
        self.stats_out["started"] = t0
        last_seq = -1
        records = []
        while self.running:
            if self.max_seconds and \
                    (time.perf_counter() - t0) >= self.max_seconds:
                break
            frame, seq, tcap = self.slot.take(last_seq)
            if frame is None:
                if not self._thread.is_alive():
                    break
                time.sleep(0.0005)
                continue
            last_seq = seq
            t_now = time.perf_counter()
            tracks, dbg, cost = self._step_body(frame, seq, t_now)
            if self.view == "window":
                zoom = self.view_zoom if self.engine.circle else 1.0
                self._interact(tracks, t_now, zoom=zoom)
                dbg["assist"] = (self.assist[0], self.assist[1],
                                 self.assist[2], self.holding) \
                    if self.assist else None
                out = draw_debug(frame, dbg, tracks)
                if zoom != 1.0:
                    out = cv2.resize(out, None, fx=zoom, fy=zoom,
                                     interpolation=cv2.INTER_NEAREST)
                cv2.imshow(win_name, out)
                if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                    break
            else:
                self._interact(tracks, t_now)   # mouse fed by overlay
            if self.json_path:
                records.append({"t": round(t_now - t0, 4),
                                "ms": round(cost, 1),
                                "budget": self.ctrl.budget,
                                "tracks": [tr.as_dict() for tr in tracks]})
        self.running = False
        self._thread.join(timeout=2.0)
        if win_name:
            cv2.destroyAllWindows()
        self.stats_out["ended"] = time.perf_counter()
        if self.json_path and records:
            import json
            with open(self.json_path, "w", encoding="utf-8") as fh:
                json.dump(records, fh, indent=1)
        return self.stats()

    def _cv_mouse(self, event, x, y, flags, param):
        import cv2
        m = self.mouse
        m["x"], m["y"] = int(x), int(y)
        if event == cv2.EVENT_LBUTTONDOWN:
            m["lmb"] = True
            m["click"] = (int(x), int(y))
        elif event == cv2.EVENT_LBUTTONUP:
            m["lmb"] = False
            m["release"] = True

    # -- worker mode (overlay view polls self.last) --------------------------
    def start(self):
        """Run the engine loop in a worker thread; return immediately."""
        self.running = True
        self._thread = threading.Thread(target=self._capture, daemon=True)
        self._thread.start()
        self.stats_out["started"] = time.perf_counter()
        self._worker = threading.Thread(target=self._worker_run,
                                        daemon=True)
        self._worker.start()
        return self._worker

    def _worker_run(self):
        last_seq = -1
        while self.running:
            frame, seq, tcap = self.slot.take(last_seq)
            if frame is None:
                if not self._thread.is_alive():
                    break
                time.sleep(0.0005)
                continue
            last_seq = seq
            self._step_body(frame, seq, time.perf_counter())
        self.stats_out["ended"] = time.perf_counter()

    def join(self, timeout=None):
        if self._worker is not None:
            self._worker.join(timeout=timeout)
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def stop(self):
        self.running = False

    # -- reporting ------------------------------------------------------
    def stats(self):
        s = self.stats_out
        if not s["ended"]:
            s["ended"] = time.perf_counter()
        wall = max(1e-6, s["ended"] - s["started"])
        costs = np.asarray(s["costs"], dtype=np.float64) if s["costs"] \
            else np.zeros(1)
        return {
            "processed": s["processed"],
            "tracks_seen": s["tracks_seen"],
            "fps": s["processed"] / wall,
            "ms_p50": float(np.percentile(costs, 50)),
            "ms_p95": float(np.percentile(costs, 95)),
            "budget_history": self.ctrl.history[-8:],
            "final_budget": self.ctrl.budget,
            "target_ms": self.ctrl.target_ms,
        }
