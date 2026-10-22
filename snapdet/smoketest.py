"""Acceptance tests: the engine must find people the old pipeline misses.

    python -m snapdet.smoketest

Uses one real photograph (downloaded once into ``tests/data``) as scene
background and as the person sprite, so the tests measure the same
thing the benchmarks did: real pixels, real detector behaviour.
Scenarios:

  unit      merge deduplication, sweep coverage, motion compensation
  inspector a 24-px person must be confirmed through a zoom window
  still     full sweep of a 640x360 scene must find a 24-px person
  video     a panning 'game capture' with an 8-px strafing person must
            hold a track on them for most frames, gated to 'unknown'
  brain     a full-size person must still get a real view/stance
"""

from __future__ import annotations

import sys
import time

import numpy as np

from .capture import synth_assets

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print("  %s  %s%s" % ("pass" if cond else "FAIL", name,
                          ("   [%s]" % detail) if (detail and not cond)
                          else ""))


def scene_parts():
    """(person sprite, clean background, source photo)."""
    person, bg, src, _ = synth_assets()
    return person, bg, src


def front_person_crop():
    """The largest person in the front-facing photo, as a sprite."""
    return synth_assets()[3]


def paste(bg, person, h, y0, x0):
    import cv2
    s = h / person.shape[0]
    w = max(1, int(round(person.shape[1] * s)))
    small = cv2.resize(person, (w, h), interpolation=cv2.INTER_AREA)
    c = bg.copy()
    c[y0:y0 + h, x0:x0 + w] = small
    return c, (x0 + w / 2.0, y0 + h / 2.0, w, h)


def test_units():
    print("unit tests")
    from .multiscale import SweepScheduler, merge_detections
    from .config import Config

    d1 = {"box": (100, 100, 106, 110), "score": 0.5}
    d2 = {"box": (102, 101, 108, 111), "score": 0.4}   # 2px off, tiny boxes
    d3 = {"box": (400, 300, 420, 350), "score": 0.3}
    merged = merge_detections([d1, d2, d3], 0.35)
    check("tiny near-duplicates merge", len(merged) == 2,
          str([m["box"] for m in merged]))

    s = SweepScheduler(((8, 3),), overlap=0.25)
    s.configure(640, 360)
    tiles = s.sweep_once()
    check("sweep covers grid", len(tiles) >= 60, str(len(tiles)))
    seen = set((x, y) for x, y, w, z in tiles)
    check("sweep tiles unique", len(seen) == len(tiles))

    from .motionroi import MotionAnalyzer
    m = MotionAnalyzer()
    a = np.zeros((240, 320, 3), np.uint8)
    a[:] = 60
    rng = np.random.default_rng(0)
    a = a + rng.integers(0, 40, a.shape, dtype=np.uint8)
    b = np.roll(a, 4, axis=1)          # camera pans 4px
    b[100:106, 204:208] = 250          # tiny blob moves with content+own
    m.analyze(a)
    rois = m.analyze(b)
    # the blob is at ~x=204+4; allow the pan-compensated position ±12
    hit = any(abs(r["center"][0] - 206) < 14 and abs(r["center"][1] - 103) < 14
              for r in rois)
    check("motion finds a 4x6 blob under a 4px pan", hit,
          str([tuple(round(v) for v in r["center"]) for r in rois]))


def test_inspector():
    print("inspector")
    from .config import Config
    from .detector import PoseDetector
    from .inspector import Inspector
    person, bg, _ = scene_parts()
    det = PoseDetector(conf=0.08)
    insp = Inspector(det, Config())
    scene, gt = paste(bg, person, 24, 120, 150)   # 24px person, 640x360
    payload, info = insp.inspect_point(scene, gt[0], gt[1], est_h=24)
    check("24px person confirmed by inspection (score %.2f)"
          % info["best"], payload is not None and info["best"] >= 0.25,
          str(info))


def test_still():
    print("still mode (full sweep)")
    from .config import Config
    from .engine import SnapEngine
    person, bg, _ = scene_parts()
    scene, gt = paste(bg, person, 24, 120, 150)
    eng = SnapEngine(Config(sweep_zooms=((8, 3),), motion_enabled=False,
                            use_face=False))
    t0 = time.perf_counter()
    tracks = eng.process_still(scene)
    dt = time.perf_counter() - t0
    near = [tr for tr in tracks
            if abs(tr.center[0] - gt[0]) < 20 and abs(tr.center[1] - gt[1]) < 20]
    check("full sweep finds the 24px person (%d tracks, %.1fs)"
          % (len(tracks), dt), len(near) == 1,
          str([(tr.id, [round(v) for v in tr.box], tr.view) for tr in tracks]))
    if near:
        check("24px track is size-gated to unknown",
              near[0].view == "unknown" and near[0].gated)


def test_video():
    print("video mode (panning capture, 8px strafing person)")
    import cv2
    from .config import Config
    from .engine import SnapEngine
    person, bg640, _ = scene_parts()
    # native 640x360 - sharp, like a real capture (no upscale blur)
    wide = np.hstack([bg640, cv2.flip(bg640, 1), bg640])
    ph = 8
    pw = max(1, int(round(person.shape[1] * ph / person.shape[0])))
    sprite = cv2.resize(person, (pw, ph), interpolation=cv2.INTER_AREA)
    N, PAN, WALK = 40, 2.0, 4.0
    W = bg640.shape[1]
    eng = SnapEngine(Config(sweep_zooms=((8, 2),), pass_budget=8,
                            inspect_budget=4, use_face=False))
    hits = 0
    judged = 0
    person_ids = set()
    for i in range(N):
        x0 = int(PAN * i)
        f = wide[:, x0:x0 + W].copy()
        px = int(60 + (WALK - PAN) * i)
        py = 300
        f[py:py + ph, px:px + pw] = sprite
        tracks = eng.process(f, t=i / 30.0)
        if i >= 5:
            judged += 1
            gx, gy = px + pw / 2.0, py + ph / 2.0
            near = [tr for tr in tracks
                    if abs(tr.center[0] - gx) < 25
                    and abs(tr.center[1] - gy) < 25]
            if near:
                hits += 1
                person_ids.update(tr.id for tr in near)
    rate = hits / max(1, judged)
    check("8px person tracked in %.0f%% of frames (>= 60%%)" % (100 * rate),
          rate >= 0.60, "%d/%d" % (hits, judged))
    check("person keeps a stable identity (<= 2 ids)", len(person_ids) <= 2,
          str(person_ids))
    check("fewer than 4 spurious tracks alive at the end",
          len(eng.tracker.tracks) <= 4,
          str([(tr.id, [round(v) for v in tr.center])
               for tr in eng.tracker.tracks]))


def test_brain():
    print("brain on a full-size person")
    from .config import Config
    from .engine import SnapEngine
    _, _, src = scene_parts()
    eng = SnapEngine(Config(motion_enabled=False, sweep_enabled=False,
                            use_face=True))
    tracks = eng.process(src, t=0.0, reset=True)
    check("full-size person detected by coarse pass", len(tracks) >= 1,
          str(len(tracks)))
    if tracks:
        tr = max(tracks, key=lambda t: t.box[3] - t.box[1])
        h = tr.box[3] - tr.box[1]
        check("classified, not gated (view=%s stance=%s h=%.0f)"
              % (tr.view, tr.stance, h),
              (not tr.gated) and tr.view != "unknown" and h >= 48,
              "%s %s" % (tr.view, tr.stance))


def _sharp_screen(bg640):
    """A 1280x720 virtual screen tiled from the real photo - SHARP.

    (An upscaled blurry background makes the pose model hallucinate
    people out of texture; a native-pixel screen is what a real game
    capture delivers, and what the engine is judged on.)
    """
    import cv2
    top = np.hstack([bg640, cv2.flip(bg640, 1)])
    return np.vstack([top, cv2.flip(top, 0)])


def _circle_scene(screen, sprite_img, screen_h=None, anchor=0.35,
                  cx=640, cy=360):
    """The virtual screen with a person (or nobody) placed on it."""
    import cv2
    canvas = screen.copy()
    if screen_h is None or sprite_img is None:
        return canvas
    w = max(1, int(round(sprite_img.shape[1] * screen_h
                         / sprite_img.shape[0])))
    spr = cv2.resize(sprite_img, (w, screen_h),
                     interpolation=cv2.INTER_AREA)
    top = int(cy - anchor * screen_h)
    left = int(cx - w / 2)
    H, W = canvas.shape[:2]
    y1, y2 = max(0, top), min(H, top + screen_h)
    x1, x2 = max(0, left), min(W, left + w)
    canvas[y1:y2, x1:x2] = spr[y1 - top:y2 - top, x1 - left:x2 - left]
    return canvas


def test_circle():
    print("circle capture mode (aim-disc, radius 50-100)")
    from .config import Config
    from .engine import SnapEngine
    person, bg640, src = scene_parts()
    fullp = src[70:252, 119:201]
    screen = _sharp_screen(bg640)
    C, CY = 640, 360

    # 1. a far person standing at the exact centre of the disc
    scene = _circle_scene(screen, fullp, screen_h=60, anchor=0.5,
                          cx=C, cy=CY)
    eng = SnapEngine(Config(capture_mode="circle", circle_radius=80,
                            use_face=False, motion_enabled=False))
    tracks = eng.process(scene, t=0.0)
    near = [tr for tr in tracks
            if abs(tr.center[0] - C) < 25 and abs(tr.center[1] - CY) < 25]
    check("circle: far 60px person at centre found", len(near) >= 1,
          str([(tr.id, [round(v) for v in tr.box]) for tr in tracks]))
    if near:
        check("circle: far person is whole, not truncated",
              not near[0].truncated)

    # 2. a close person: chest fragment fills the R=50 disc, no head
    scene = _circle_scene(screen, fullp, screen_h=700, anchor=0.35,
                          cx=C, cy=CY)
    eng = SnapEngine(Config(capture_mode="circle", circle_radius=50,
                            use_face=False, motion_enabled=False))
    tracks = eng.process(scene, t=0.0)
    near = [tr for tr in tracks
            if abs(tr.center[0] - C) < 40 and abs(tr.center[1] - CY) < 40]
    check("circle: chest fragment (R=50) found by context passes",
          len(near) >= 1,
          str([(tr.id, [round(v) for v in tr.box], tr.source)
               for tr in tracks]))
    if near:
        tr = near[0]
        check("circle: fragment flagged truncated, stance unknown",
              tr.truncated and tr.stance == "unknown",
              "trunc=%s stance=%s" % (tr.truncated, tr.stance))

    # 3. an empty disc must not hallucinate people
    scene = _circle_scene(screen, None)
    eng = SnapEngine(Config(capture_mode="circle", circle_radius=80,
                            use_face=False, motion_enabled=False))
    tracks = eng.process(scene, t=0.0)
    check("circle: empty disc produces no tracks", len(tracks) == 0,
          str([(tr.id, [round(v) for v in tr.box], tr.source,
                round(tr.score, 2)) for tr in tracks]))

    # 4. a FRONT-facing head+shoulders fragment: the face is the hook
    face_sprite = front_person_crop()
    if face_sprite is None:
        print("  SKIP  circle: front head fragment (no person in photo)")
    else:
        scene = _circle_scene(screen, face_sprite, screen_h=460,
                              anchor=0.10, cx=C, cy=CY)
        eng = SnapEngine(Config(capture_mode="circle", circle_radius=80,
                                motion_enabled=False))
        tracks = eng.process(scene, t=0.0)

        def near_disc(tr, pad=40):
            x1, y1, x2, y2 = tr.box
            dx = max(x1 - C, 0.0, C - x2)
            dy = max(y1 - CY, 0.0, CY - y2)
            return (dx * dx + dy * dy) ** 0.5 <= pad

        near = [tr for tr in tracks if near_disc(tr)]
        check("circle: front head fragment (R=80) found "
              "(sources: %s)" % ([tr.source for tr in near],),
              len(near) >= 1,
              str([(tr.id, [round(v) for v in tr.box], tr.source)
                   for tr in tracks]))

    # 5. a close fragment sliding across the disc keeps one identity
    eng = SnapEngine(Config(capture_mode="circle", circle_radius=80,
                            use_face=False))
    hits = 0
    ids = set()
    N = 12
    for i in range(N):
        cx = C + (i - 6) * 6
        scene = _circle_scene(screen, fullp, screen_h=600, anchor=0.35,
                              cx=cx, cy=CY)
        tracks = eng.process(scene, t=i / 30.0)
        if i >= 2:
            near = [tr for tr in tracks
                    if abs(tr.center[0] - cx) < 35
                    and abs(tr.center[1] - CY) < 35]
            if near:
                hits += 1
                ids.update(tr.id for tr in near)
    check("circle: moving fragment tracked in %d/%d frames (>= 8)"
          % (hits, N - 2), hits >= 8, "%d" % hits)
    check("circle: moving fragment keeps <= 2 identities", len(ids) <= 2,
          str(ids))


def test_capture_realtime():
    print("capture sources + real-time loop")
    import sys
    from .capture import open_source
    from .config import Config
    from .engine import SnapEngine
    from .live import RealtimeLoop

    if sys.platform not in ("win32", "cygwin"):
        try:
            open_source("screen", backend="dxcam")
            err = None
        except RuntimeError as exc:
            err = str(exc)
        check("dxcam refuses non-Windows with a helpful error",
              bool(err) and "Windows" in err, str(err))

    src = open_source("synth", circle=True, radius=80, frames=48)
    f = src.read()
    check("synth circle source delivers the centre square (160x160)",
          f is not None and f.shape == (160, 160, 3),
          str(None if f is None else f.shape))
    src.release()

    eng = SnapEngine(Config(capture_mode="circle", circle_radius=80,
                            use_face=False))
    src = open_source("synth", circle=True, radius=80, frames=90, loop=True)
    loop = RealtimeLoop(src, eng, target_fps=25.0, max_seconds=20.0)
    stats = loop.run()
    src.release()
    check("live loop processed >= 15 frames in 20 s (%d)"
          % stats["processed"], stats["processed"] >= 15)
    check("budget controller stayed inside [3, 12]",
          3 <= stats["final_budget"] <= 12, str(stats["final_budget"]))
    check("budget controller adapted to this machine",
          len(stats["budget_history"]) >= 1 or stats["final_budget"] < 12,
          str(stats["budget_history"]))
    check("live loop saw tracks (fragment or far person)",
          stats["tracks_seen"] >= 1, str(stats["tracks_seen"]))
    print("        live stats: %.1f fps, p50 %.0f ms, budget %d"
          % (stats["fps"], stats["ms_p50"], stats["final_budget"]))


def test_events():
    print("operator selection events")
    import socket
    from .events import EventBus, SelectEvent, pick_track

    class T(object):
        def __init__(self, tid, box):
            self.id = tid
            self.box = box

        @property
        def center(self):
            return ((self.box[0] + self.box[2]) / 2.0,
                    (self.box[1] + self.box[3]) / 2.0)

    trs = [T(1, (100, 100, 140, 200)), T(2, (300, 300, 320, 360))]
    check("click inside a box picks that track",
          pick_track(trs, 120, 150, 10) is trs[0])
    check("click 10px off the box edge picks within radius 40",
          pick_track(trs, 90, 150, 40) is trs[0])
    check("click beyond the magnet radius picks nothing",
          pick_track(trs, 80, 150, 15) is None)
    check("nearest track wins", pick_track(trs, 280, 280, 200) is trs[1])

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2.0)
    port = rx.getsockname()[1]
    bus = EventBus(publish="udp://127.0.0.1:%d" % port)
    got = []
    bus.subscribe(lambda ev: got.append(ev))
    bus.emit(SelectEvent("select", 1.0, 120, 150, 118, 152, 1,
                         "front", "standing", 100.0, "coarse"))
    blob, _ = rx.recvfrom(65535)
    ev = SelectEvent.from_json(blob.decode("utf-8"))
    check("event survives the UDP JSON round trip",
          ev.kind == "select" and ev.track_id == 1 and ev.x == 120
          and ev.view == "front", str(ev))
    check("in-process subscriber received the event", len(got) == 1)
    bus.close()
    rx.close()


def test_overlay():
    print("on-screen overlay primitives")
    import numpy as np
    from .overlay import overlay_primitives, render_cv2

    class Tr(object):
        id, view, stance = 1, "front", "standing"
        box = (20, 20, 40, 60)
        misses, truncated, gated, source = 0, False, False, "coarse"

        @property
        def center(self):
            return (30.0, 40.0)

        @property
        def velocity(self):
            return (12.0, 0.0)

    debug = {"dets": [{"box": (10, 10, 30, 50), "score": 0.5,
                      "source": "coarse"}],
             "rois": [{"box": (5, 5, 15, 15)}],
             "rejected": [((40, 40, 50, 50), 0.10)],
             "memory": [(60, 60, 0.40, 3)],
             "circle": (80, 80, 80),
             "assist": (80, 80, 1, True),
             "ms": 12.0, "passes": 5, "sweep_progress": "z8:1/9"}
    prims = overlay_primitives(debug, [Tr()])
    kinds = set(p["kind"] for p in prims)
    check("overlay primitives cover rect/circle/cross/text/line",
          {"rect", "circle", "cross", "text", "line"} <= kinds,
          str(sorted(kinds)))
    labels = " ".join(p.get("s", "") for p in prims if p["kind"] == "text")
    check("overlay labels carry id/view/stance and HOLD state",
          "#1 front/standing" in labels and "HOLD" in labels, labels)
    img = render_cv2(np.zeros((160, 160, 3), np.uint8), prims)
    check("overlay raster twin renders pixels", int(img.sum()) > 0)


def main():
    print("snapdet smoketest")
    t0 = time.perf_counter()
    test_units()
    test_inspector()
    test_still()
    test_video()
    test_circle()
    test_capture_realtime()
    test_events()
    test_overlay()
    test_brain()
    npass = sum(1 for _, ok in RESULTS if ok)
    print()
    print("%d passed, %d failed  (%.1f s)"
          % (npass, len(RESULTS) - npass, time.perf_counter() - t0))
    return 0 if npass == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
