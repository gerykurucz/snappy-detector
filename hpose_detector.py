
from __future__ import annotations

import os
import sys
import urllib.request

import numpy as np

from hpose_keypoints import N_KP, Pose, clip_box

MODEL_URLS = {
    "yolo11n-pose.onnx":
        "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n-pose.onnx",
    "yolo11s-pose.onnx":
        "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11s-pose.onnx",
    "yolo11m-pose.onnx":
        "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11m-pose.onnx",
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

    def _letterbox(self, img: np.ndarray):
        import cv2
        h, w = img.shape[:2]
        ih, iw = self._in_h, self._in_w
        r = min(iw / float(w), ih / float(h))
        nw = max(1, int(round(w * r)))
        nh = max(1, int(round(h * r)))
        if (nw, nh) != (w, h):
            interp = cv2.INTER_AREA if r < 1.0 else cv2.INTER_CUBIC
            img = cv2.resize(img, (nw, nh), interpolation=interp)
        pad_x = (iw - nw) / 2.0
        pad_y = (ih - nh) / 2.0
        left = int(round(pad_x - 0.1))
        top = int(round(pad_y - 0.1))
        out = np.full((ih, iw, 3), 114, dtype=np.uint8)
        out[top:top + nh, left:left + nw] = img
        return out, r, left, top

    def detect(self, img: np.ndarray, max_people: int = 64,
               tta: bool = False) -> list:
        import cv2
        if img is None or img.size == 0:
            return []
        poses = self._detect_pass(img, max_people)
        if not tta:
            return poses
        h, w = img.shape[:2]
        if h < 8 or w < 8:
            return poses
        flipped = cv2.flip(img, 1)
        other = self._detect_pass(flipped, max_people)
        if not other:
            return poses
        left = (1, 3, 5, 7, 9, 11, 13, 15)
        right = (2, 4, 6, 8, 10, 12, 14, 16)
        merged = list(poses)
        for p in other:
            kp = p.kps.copy()
            kp[:, 0] = w - kp[:, 0]
            tmp = kp[list(left)].copy()
            kp[list(left)] = kp[list(right)]
            kp[list(right)] = tmp
            box = (w - p.box[2], p.box[1], w - p.box[0], p.box[3])
            merged.append(Pose(kp, box, p.score))
        boxes = np.asarray([p.box for p in merged], dtype=np.float32)
        scores = np.asarray([p.score for p in merged], dtype=np.float32)
        idx = _nms(boxes, scores, self.iou)
        return [merged[i] for i in idx]

    def _detect_pass(self, img: np.ndarray, max_people: int = 64) -> list:
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
        out = np.asarray(raw, dtype=np.float32)
        if out.ndim == 3:
            out = out[0]
        if out.shape[0] < out.shape[1]:
            out = out.T
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
            kp[:, 0] = (kp[:, 0] - left) / r
            kp[:, 1] = (kp[:, 1] - top) / r
            kp[:, 2] = np.clip(kp[:, 2], 0.0, 1.0)
            b = ((boxes[i][0] - left) / r, (boxes[i][1] - top) / r,
                 (boxes[i][2] - left) / r, (boxes[i][3] - top) / r)
            b = clip_box(b, w, h)
            poses.append(Pose(kp, b, float(scores[i])))
        return poses


class FaceDetector(object):

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

