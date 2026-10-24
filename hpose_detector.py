"""Pose detection: YOLO-pose ONNX inference and a face detector.

Two models work together here:

* **YOLO-pose** finds people and their 17 joints.
* **YuNet** finds faces.  It is small (a quarter of a megabyte) and
  fast, and it answers a question the keypoints answer only weakly:
  *is that head looking at the camera?*  A face detector that fires
  means the person is facing us; a head with no face on it means they
  are turned away.

Both models download automatically into a cache folder and are then
used offline.  Nothing here is game specific - the detectors read
pixels and return geometry.
"""

from __future__ import annotations

import os
import sys
import urllib.request

import numpy as np

from hpose_keypoints import N_KP, Pose, clip_box

#: Where the downloaded weights are kept.
#: Only models with a live upstream URL are listed: these two
#: self-heal when missing.  Anything else (yolo11s-pose.onnx, the
#: sharper option in the GUI) must be placed in models/ by hand -
#: export it with ultralytics, see models/README.md.  Stale URLs here
#: used to 404, turning a missing file into a confusing download error.
MODEL_URLS = {
    "yolo11n-pose.onnx":
        "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n-pose.onnx",
    "face_detection_yunet_2023mar.onnx":
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
}

DEFAULT_POSE_MODEL = "yolo11n-pose.onnx"
DEFAULT_FACE_MODEL = "face_detection_yunet_2023mar.onnx"



def _writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_test")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def cache_dir() -> str:
    """The folder that holds the model weights.

    The weights live in ``models/`` beside the code, so the whole
    project can be copied to another machine and still work.  The
    order of preference is

    1. ``HPOSE_CACHE`` when it is set,
    2. ``models`` beside the executable, when a frozen build finds the
       pose model there (the portable layout),
    3. ``models`` beside the code (source tree, or bundled copy),
    4. the per-user application data folder, when neither can be
       written to.

    Nothing is ever loaded from somewhere unrelated: the models are
    either in the project or downloaded into it.
    """
    env = os.environ.get("HPOSE_CACHE")
    if env:
        path = env
    else:
        path = ""
        if getattr(sys, "frozen", False):
            beside_exe = os.path.join(
                os.path.dirname(os.path.abspath(sys.executable)), "models")
            if os.path.isfile(os.path.join(beside_exe, DEFAULT_POSE_MODEL)):
                path = beside_exe
        if not path:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "models")
        if not _writable(path):
            base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
            path = os.path.join(base, "SnappyAI", "models")
    os.makedirs(path, exist_ok=True)
    return path


def model_path(name: str, download: bool = True) -> str:
    """Absolute path of a model, downloading it when needed."""
    path = os.path.join(cache_dir(), name)
    if os.path.isfile(path) and os.path.getsize(path) > 1024:
        return path
    url = MODEL_URLS.get(name)
    if not url:
        raise FileNotFoundError("unknown model %r" % (name,))
    if not download:
        raise FileNotFoundError("model %r is not cached in %s"
                                % (name, cache_dir()))
    sys.stderr.write("hpose: downloading %s ...\n" % (name,))
    tmp = path + ".part"
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            data = r.read()
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except Exception as exc:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise RuntimeError("could not download %s (%s): %r"
                           % (name, url, exc))
    sys.stderr.write("hpose: cached %s (%d bytes)\n"
                     % (name, os.path.getsize(path)))
    return path


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float) -> list:
    """Plain greedy NMS over ``(x1, y1, x2, y2)`` boxes."""
    if len(boxes) == 0:
        return []
    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]
    areas = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.clip(xx2 - xx1, 0, None)
        h = np.clip(yy2 - yy1, 0, None)
        inter = w * h
        union = areas[i] + areas[order[1:]] - inter
        iou = inter / np.maximum(union, 1e-9)
        order = order[1:][iou <= iou_thr]
    return keep


class PoseDetector(object):
    """YOLO-pose person detector."""

    def __init__(self, model: str = None, imgsz: int = 640,
                 conf: float = 0.25, iou: float = 0.45,
                 device: str = "cpu", download: bool = True):
        self.model_name = model or DEFAULT_POSE_MODEL
        model = self.model_name
        self.imgsz = int(imgsz)
        self.conf = float(conf)
        self.iou = float(iou)
        self.path = model_path(model, download=download)
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError(
                "onnxruntime is required for pose detection; install it with "
                "'pip install onnxruntime' (%r)" % (exc,))
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        providers = ["CPUExecutionProvider"]
        if device == "cuda" and "CUDAExecutionProvider" in \
                ort.get_available_providers():
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        self.session = ort.InferenceSession(self.path, opts, providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        self._in_h, self._in_w = self._input_shape()

    def _input_shape(self):
        shape = self.session.get_inputs()[0].shape
        h = shape[2] if len(shape) > 2 and isinstance(shape[2], int) \
            else self.imgsz
        w = shape[3] if len(shape) > 3 and isinstance(shape[3], int) \
            else self.imgsz
        return int(h), int(w)

    # -- preprocessing ------------------------------------------------
    def _letterbox(self, img: np.ndarray):
        import cv2
        h, w = img.shape[:2]
        ih, iw = self._in_h, self._in_w
        r = min(iw / float(w), ih / float(h))
        nw = max(1, int(round(w * r)))
        nh = max(1, int(round(h * r)))
        if (nw, nh) != (w, h):
            img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        pad_x = (iw - nw) / 2.0
        pad_y = (ih - nh) / 2.0
        left = int(round(pad_x - 0.1))
        top = int(round(pad_y - 0.1))
        out = np.full((ih, iw, 3), 114, dtype=np.uint8)
        out[top:top + nh, left:left + nw] = img
        return out, r, left, top

    # -- inference ----------------------------------------------------
    def detect(self, img: np.ndarray, max_people: int = 64) -> list:
        """Detect people.  Returns a list of :class:`Pose`."""
        import cv2
        if img is None or img.size == 0:
            return []
        h, w = img.shape[:2]
        padded, r, left, top = self._letterbox(img)
        blob = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB)
        blob = blob.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        raw = self.session.run([self.output_name],
                               {self.input_name: blob})[0]
        return self.decode(raw, r, left, top, w, h, max_people)

    def decode(self, raw: np.ndarray, r: float, left: float, top: float,
               w: int, h: int, max_people: int = 64) -> list:
        """Turn a raw network output into poses."""
        out = np.asarray(raw, dtype=np.float32)
        if out.ndim == 3:
            out = out[0]
        if out.shape[0] < out.shape[1]:
            out = out.T
        # columns: cx, cy, w, h, score, then 17 * (x, y, conf)
        if out.shape[1] < 5 + N_KP * 3:
            return []
        boxes_c = out[:, :4]
        scores = out[:, 4]
        kps_raw = out[:, 5:5 + N_KP * 3].reshape(-1, N_KP, 3)

        keep_mask = scores >= self.conf
        if not np.any(keep_mask):
            return []
        boxes_c = boxes_c[keep_mask]
        scores = scores[keep_mask]
        kps_raw = kps_raw[keep_mask]

        cx = boxes_c[:, 0]
        cy = boxes_c[:, 1]
        bw = boxes_c[:, 2]
        bh = boxes_c[:, 3]
        boxes = np.stack([cx - bw / 2, cy - bh / 2,
                          cx + bw / 2, cy + bh / 2], axis=1)
        idx = _nms(boxes, scores, self.iou)[:max_people]
        if not idx:
            return []

        poses = []
        for i in idx:
            kp = kps_raw[i].copy()
            # un-letterbox the keypoints
            kp[:, 0] = (kp[:, 0] - left) / r
            kp[:, 1] = (kp[:, 1] - top) / r
            kp[:, 2] = np.clip(kp[:, 2], 0.0, 1.0)
            b = ((boxes[i][0] - left) / r, (boxes[i][1] - top) / r,
                 (boxes[i][2] - left) / r, (boxes[i][3] - top) / r)
            b = clip_box(b, w, h)
            poses.append(Pose(kp, b, float(scores[i])))
        return poses


class FaceDetector(object):
    """YuNet face detector - the 'is the head looking at us' sensor."""

    def __init__(self, model: str = None,
                 score_threshold: float = 0.45, nms_threshold: float = 0.3,
                 download: bool = True):
        model = model or DEFAULT_FACE_MODEL
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("opencv is required for face detection (%r)"
                               % (exc,))
        self.path = model_path(model, download=download)
        self.model = self._create(cv2, self.path, score_threshold,
                                  nms_threshold)
        self.score_threshold = float(score_threshold)

    @staticmethod
    def _create(cv2, path, score_threshold, nms_threshold):
        """Build the detector without OpenCV's noisy warning.

        FaceDetectorYN asks the network which backend it should use.
        OpenCV 5 runs on a new graph engine where that request is not
        supported, so it prints a warning every time - even though
        nothing is wrong and the detector runs on the CPU either way.

        The log level is dropped just for the call that triggers it
        and put straight back, so every other OpenCV message still
        gets through.
        """
        level = None
        try:
            level = cv2.utils.logging.getLogLevel()
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
        except Exception:
            level = None
        try:
            return cv2.FaceDetectorYN.create(
                path, "", (320, 320), float(score_threshold),
                float(nms_threshold), 5000)
        finally:
            if level is not None:
                try:
                    cv2.utils.logging.setLogLevel(level)
                except Exception:
                    pass

    def detect(self, img: np.ndarray) -> list:
        """Return a list of ``(x1, y1, x2, y2, score)`` face boxes."""
        if img is None or img.size == 0:
            return []
        h, w = img.shape[:2]
        self.model.setInputSize((w, h))
        try:
            ok, faces = self.model.detect(img)
        except Exception:
            return []
        if not ok or faces is None:
            return []
        out = []
        for f in np.asarray(faces, dtype=np.float32):
            x, y, fw, fh = float(f[0]), float(f[1]), float(f[2]), float(f[3])
            out.append((x, y, x + fw, y + fh, float(f[-1])))
        return out

