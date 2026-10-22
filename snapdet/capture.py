"""Frame sources: video files, live screens (dxcam / mss), synthetic.

    open_source("screen", backend="dxcam", radius=80)   # GPU duplication
    open_source("screen", backend="mss")                # portable fallback
    open_source("synth", circle=True)                   # built-in test clip
    open_source("game.mp4")                             # a file

Backend notes
-------------
``dxcam``  - Windows Desktop Duplication API; ~0.1-1 ms per grab, the
             right choice for a real-time aim region.  Reads RGB or
             BGR straight from the GPU, no window handling.
``mss``    - cross-platform (Win/mac/X11); ~2-6 ms for a small region.
             The fallback everywhere dxcam is unavailable.

When ``circle`` capture is used together with a screen source, the
source grabs only the centre square of side ``2 * radius``: the engine
then sees an already-cropped disc (coordinates are disc-local), and
the capture cost stays proportional to the aim region, not the screen.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.request

import numpy as np

#: Cached scene assets for the synthetic source (downloaded once).
PERSON_URL = ("http://images.cocodataset.org/val2017/000000186042.jpg")
FRONT_URL = ("http://images.cocodataset.org/val2017/000000397351.jpg")


def data_dir() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "tests", "data")
    os.makedirs(path, exist_ok=True)
    return path


def synth_assets():
    """Scene assets of the synthetic source, cached on disk.

    Returns ``(person, background, photo, front_person)``: a real
    photographed person, the same photo with the person inpainted out
    (a clean background), the source photo, and the largest person of
    a front-facing photograph.  The acceptance tests share these so
    there is exactly one copy of the scene builder.
    """
    import cv2
    d = data_dir()
    pp, fp = os.path.join(d, "person.jpg"), os.path.join(d, "front.jpg")
    if not os.path.isfile(pp):
        urllib.request.urlretrieve(PERSON_URL, pp)
    if not os.path.isfile(fp):
        urllib.request.urlretrieve(FRONT_URL, fp)
    src = cv2.imread(pp)
    box = (119, 70, 201, 252)
    person = src[box[1]:box[3], box[0]:box[2]].copy()
    mask = np.zeros(src.shape[:2], np.uint8)
    mask[box[1] - 6:box[3] + 6, box[0] - 6:box[2] + 6] = 255
    bg = cv2.inpaint(src, mask, 7, cv2.INPAINT_TELEA)
    fimg = cv2.imread(fp)
    front = None
    from .detector import PoseDetector
    poses = PoseDetector(conf=0.25).detect(fimg)
    if poses:
        p = max(poses, key=lambda p: p.box[3] - p.box[1])
        x1, y1, x2, y2 = [int(v) for v in p.box]
        mh = int((y2 - y1) * 0.12) + 4
        mw = int((x2 - x1) * 0.15) + 4
        H, W = fimg.shape[:2]
        front = fimg[max(0, y1 - mh):min(H, y2 + mh // 2),
                     max(0, x1 - mw):min(W, x2 + mw)]
    return person, bg, src, front


def _paste_clip(canvas, spr, top, left):
    H, W = canvas.shape[:2]
    sh, sw = spr.shape[:2]
    y1, y2 = max(0, top), min(H, top + sh)
    x1, x2 = max(0, left), min(W, left + sw)
    if y2 <= y1 or x2 <= x1:
        return
    canvas[y1:y2, x1:x2] = spr[y1 - top:y2 - top, x1 - left:x2 - left]


def build_synth_frames(n=60, circle=False):
    """The built-in clip: a panning real photo with people pasted in.

    ``circle=False``: an 182-px, a 48-px and an 8-px person (the three
    detection regimes).  ``circle=True``: a 520-px body whose head and
    shoulders sweep the exact screen centre plus a far person crossing
    the disc - the truncation regimes.
    """
    import cv2
    person, bg640, src, _ = synth_assets()
    H, W = bg640.shape[:2]
    wide = np.hstack([bg640, cv2.flip(bg640, 1), bg640])

    def sprite(h, img=None):
        img = person if img is None else img
        w = max(1, int(round(img.shape[1] * h / img.shape[0])))
        return cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)

    big = src[70:252, 119:201]
    med, small = sprite(48), sprite(8)
    giant = sprite(520, img=big)
    frames = []
    for i in range(n):
        x0 = int(2.0 * i)
        f = wide[:, x0:x0 + W].copy()
        if circle:
            _paste_clip(f, giant, int(180 - 0.13 * 520),
                        int(140 + 4.8 * i - giant.shape[1] / 2))
            _paste_clip(f, sprite(60), 170, int(210 + 2.2 * i))
        else:
            f[120:120 + big.shape[0], 470:470 + big.shape[1]] = big
            _paste_clip(f, med, 250, int(240 + 1.0 * i))
            _paste_clip(f, small, 300, int(60 + 2.0 * i))
        frames.append(f)
    return frames


class FrameSource(object):
    name = "base"
    fps_hint = 30.0
    pacing = 0.0     # seconds between reads; 0 = as fast as the source gives
    alive = True     # False once a finite source is exhausted

    def read(self):
        raise NotImplementedError

    def release(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class VideoSource(FrameSource):
    name = "video"

    def __init__(self, path, loop=False):
        import cv2
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise RuntimeError("could not open %r" % (path,))
        self.fps_hint = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        if not (1.0 < self.fps_hint <= 240.0):
            self.fps_hint = 30.0
        self.pacing = 1.0 / self.fps_hint
        self.loop = loop
        self.path = path
        self.origin = None
        self.shape = (int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                      int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)))

    def read(self):
        ok, frame = self.cap.read()
        if not ok and self.loop:
            self.cap.set(0, 0)
            ok, frame = self.cap.read()
        if not ok:
            self.alive = False
            return None
        return frame

    def release(self):
        self.cap.release()


class SynthSource(FrameSource):
    """The built-in clip, generated in process (no file I/O).

    With ``circle=True`` only the centre square of side ``2*radius``
    is delivered, exactly like a screen region capture would.
    """
    name = "synth"

    def __init__(self, frames=120, circle=False, radius=80, loop=True):
        self._frames = build_synth_frames(n=frames, circle=circle)
        if circle:
            side = 2 * int(radius)
            H, W = self._frames[0].shape[:2]
            cy, cx = H // 2, W // 2
            self._frames = [f[cy - radius:cy - radius + side,
                              cx - radius:cx - radius + side]
                            for f in self._frames]
        self.i = 0
        self.loop = loop
        self.pacing = 1.0 / self.fps_hint
        self.origin = None
        self.shape = self._frames[0].shape

    def read(self):
        if self.i >= len(self._frames):
            if not self.loop:
                self.alive = False
                return None
            self.i = 0
        f = self._frames[self.i]
        self.i += 1
        return f


class MSSSource(FrameSource):
    """Portable screen capture via ``mss``."""
    name = "mss"

    def __init__(self, region=None, radius=0, monitor=1):
        try:
            import mss
        except ImportError as exc:
            raise RuntimeError(
                "mss is not installed: pip install mss (%r)" % (exc,))
        self._mss = mss
        self.sct = mss.mss()
        mon = self.sct.monitors[monitor]
        self.screen_w, self.screen_h = mon["width"], mon["height"]
        if region is None and radius:
            cx, cy = self.screen_w // 2, self.screen_h // 2
            region = (cx - radius, cy - radius, 2 * radius, 2 * radius)
        if region is None:
            region = (mon["left"], mon["top"],
                      self.screen_w, self.screen_h)
        self.region = {"left": int(region[0]), "top": int(region[1]),
                       "width": int(region[2]), "height": int(region[3])}
        self.origin = (self.region["left"], self.region["top"])
        self.shape = (self.region["height"], self.region["width"])

    def read(self):
        shot = self.sct.grab(self.region)
        arr = np.frombuffer(shot.raw, dtype=np.uint8)
        arr = arr.reshape(shot.height, shot.width, 4)
        return np.ascontiguousarray(arr[:, :, :3])   # BGRA -> BGR

    def release(self):
        try:
            self.sct.close()
        except Exception:
            pass


class DXCamSource(FrameSource):
    """Windows Desktop Duplication capture via ``dxcam``.

    Asynchronous by design: ``get_latest_frame`` returns the newest
    presented frame, so a slow consumer never queues up latency - the
    property a real-time loop needs most.
    """
    name = "dxcam"

    def __init__(self, region=None, radius=0, device_idx=0, output_idx=0):
        if sys.platform not in ("win32", "cygwin"):
            raise RuntimeError(
                "dxcam needs Windows (Desktop Duplication API); "
                "use backend=mss here")
        try:
            import dxcam
        except ImportError as exc:
            raise RuntimeError(
                "dxcam is not installed: pip install dxcam (%r)" % (exc,))
        self.cam = dxcam.create(device_idx=device_idx,
                                output_idx=output_idx,
                                output_color="BGR",
                                region=tuple(region) if region else None)
        info = self.cam.get_device_info() if region is None else None
        self._last = None
        self.cam.start(start_async=True, target_fps=0)
        # one blocking grab to learn the surface size / prime the pipe
        for _ in range(50):
            f = self.cam.get_latest_frame()
            if f is not None:
                self._last = f
                break
            time.sleep(0.01)
        if self._last is None:
            raise RuntimeError("dxcam produced no frame; is a desktop "
                               "session active (no service/RDP headless)?")
        self.screen_h, self.screen_w = self._last.shape[:2]
        self.origin = (int(region[0]), int(region[1])) if region else (0, 0)
        self.shape = self._last.shape

    def read(self):
        f = self.cam.get_latest_frame()
        if f is None:
            return self._last          # no new present: reuse, do not stall
        self._last = f
        return f

    def release(self):
        try:
            self.cam.stop()
            del self.cam
        except Exception:
            pass


def centre_region(screen_w, screen_h, radius):
    cx, cy = screen_w // 2, screen_h // 2
    return (cx - radius, cy - radius, 2 * radius, 2 * radius)


def open_source(spec, backend="auto", region=None, radius=0,
                loop=False, **kw):
    """Factory.  ``spec``: 'screen', 'synth' or a video path."""
    if spec == "synth":
        return SynthSource(circle=bool(kw.pop("circle", False)),
                           radius=radius or 80, loop=loop,
                           **kw)
    if spec == "screen":
        if backend == "auto":
            backend = "dxcam" if sys.platform in ("win32", "cygwin") \
                else "mss"
        if backend == "dxcam":
            return DXCamSource(region=region, radius=radius, **kw)
        if backend == "mss":
            return MSSSource(region=region, radius=radius, **kw)
        raise RuntimeError("unknown screen backend %r" % (backend,))
    return VideoSource(spec, loop=loop)
