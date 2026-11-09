"""Detection pipeline (single flat module).

Merged from yolo.py, hpose.py, vision.py:
YOLO26n capture+inference, human pose estimation, and vision scanning/targeting.
"""

from __future__ import annotations

# ======================================================================
# merged from yolo.py
# ======================================================================

"""
YOLO26n Detection Backend for Real-time Human Figure Detection

High-performance capture + detection pipeline targeting <10ms per frame.
- Uses YOLO26n ONNX model (FP16) for person detection (COCO class 0)
- 40-50px radius centered on screen center
- Threaded capture + inference pipeline using win32rt CaptureBackend (WGC/dxcam/mss)
- Zero-copy frame passing where possible
"""

import threading
import time
import queue
import cv2
import numpy as np
from dataclasses import dataclass
import dataclasses
from typing import Optional, Tuple, List
from pathlib import Path

# Import win32rt capture backend
try:
    from input import CaptureBackend
except ImportError:
    CaptureBackend = None


@dataclass
class Detection:
    """A detected human figure."""
    x: int           # Center X in screen coordinates
    y: int           # Center Y in screen coordinates
    w: int           # Box width
    h: int           # Box height
    conf: float      # Confidence score
    aspect_ratio: float = 0.0  # h/w ratio (target ~2.0 for upright human)
    seq: int = 0               # Monotonic inference-frame number
    t: float = 0.0             # time.perf_counter() when inferred


@dataclass
class YOLOConfig:
    """Configuration for YOLO26n backend."""
    model_path: str = "models/yolo26n.onnx"
    capture_radius: int = 45           # 40-50px radius from center
    conf_threshold: float = 0.35       # Detection confidence
    iou_threshold: float = 0.45        # NMS IoU
    target_fps: int = 120              # Capture FPS cap
    input_size: int = 224              # Model input (224x224 for speed)
    use_half: bool = True              # FP16 inference
    providers: Tuple[str, ...] = ("CPUExecutionProvider",)  # ONNX Runtime providers
    person_class_id: int = 0           # COCO class ID for person
    min_aspect_ratio: float = 1.1      # Min h/w for upright human
    max_aspect_ratio: float = 3.6      # Max h/w (close-ups clip the sides)
    capture_backend: str = "auto"      # win32rt capture engine


class YOLO26nBackend:
    """
    Real-time YOLO26n human detection backend.
    
    Pipeline:
    1. Capture thread: mss grabs screen region (centered, 2*radius square)
    2. Inference thread: YOLO26n detects persons, filters by aspect ratio
    3. Main thread: get_latest_detection() returns best match
    
    Target: <10ms total latency per frame
    """
    
    def __init__(self, cfg: Optional[YOLOConfig] = None):
        self.cfg = cfg or YOLOConfig()
        self._stop = threading.Event()
        self._capture_thread: Optional[threading.Thread] = None
        self._infer_thread: Optional[threading.Thread] = None
        self._frame_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=2)
        self._result_queue: queue.Queue[List[Detection]] = queue.Queue(maxsize=2)
        self._latest_detection: Optional[Detection] = None
        self._frame_seq = 0
        self._lock = threading.Lock()
        self._session = None
        self._input_name = None
        self._output_names = None
        self._monitor = None
        self._sct = None
        self._stats = {
            "frames_captured": 0,
            "frames_inferred": 0,
            "detections_found": 0,
            "avg_capture_ms": 0.0,
            "avg_infer_ms": 0.0,
        }
        self._warmup_done = False
        
    def start(self) -> bool:
        """Initialize model, capture, and start threads."""
        if not self._init_model():
            return False
        if not self._init_capture():
            return False
        self._start_threads()
        self._warmup()
        return True
    
    def _init_model(self) -> bool:
        """Load ONNX model with ONNX Runtime."""
        try:
            import onnxruntime as ort
        except ImportError:
            print("ERROR: onnxruntime not installed. pip install onnxruntime")
            return False
            
        model_path = Path(self.cfg.model_path)
        if not model_path.exists():
            print(f"ERROR: Model not found: {model_path}")
            return False
            
        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_options.intra_op_num_threads = 2
        sess_options.inter_op_num_threads = 2
        
        self._session = ort.InferenceSession(
            str(model_path),
            sess_options=sess_options,
            providers=self.cfg.providers
        )
        
        self._input_name = self._session.get_inputs()[0].name
        self._output_names = [o.name for o in self._session.get_outputs()]
        
        print(f"YOLO26n loaded: {model_path} | providers: {self.cfg.providers}")
        print(f"  Input: {self._session.get_inputs()[0].shape}")
        print(f"  Output: {[o.shape for o in self._session.get_outputs()]}")
        return True
    
    def _init_capture(self) -> bool:
        """Initialize win32rt CaptureBackend (WGC/dxcam/mss auto-select)."""
        if CaptureBackend is None:
            print("ERROR: win32rt.CaptureBackend not available")
            return False
            
        try:
            # Honor the app's capture choice.  "auto" picks mss, which is
            # the reliable engine on machines where dxcam's COM init fails.
            self._capture = CaptureBackend(prefer=self.cfg.capture_backend)
            # Get screen center
            import input as win32rt
            vx, vy, vw, vh = win32rt.virtual_screen()
            cx = vx + vw // 2
            cy = vy + vh // 2
            r = self.cfg.capture_radius
            self._capture_region = (cx - r, cy - r, cx + r, cy + r)
            print(f"Capture region: {2*r}x{2*r} @ ({cx-r},{cy-r}) using win32rt CaptureBackend (prefer={self.cfg.capture_backend})")
            return True
        except Exception as e:
            print(f"Capture init failed: {e}")
            return False
    
    def _start_threads(self):
        """Start capture and inference threads."""
        self._stop.clear()
        self._capture_thread = threading.Thread(
            target=self._capture_loop, name="yolo26n-capture", daemon=True
        )
        self._infer_thread = threading.Thread(
            target=self._inference_loop, name="yolo26n-infer", daemon=True
        )
        self._capture_thread.start()
        self._infer_thread.start()
        print("YOLO26n threads started")
    
    def _warmup(self, runs: int = 5):
        """Warm up the model with dummy input."""
        dummy = np.random.randint(0, 255, (self.cfg.input_size, self.cfg.input_size, 3), dtype=np.uint8)
        blob = self._preprocess(dummy)
        for _ in range(runs):
            self._session.run(self._output_names, {self._input_name: blob})
        self._warmup_done = True
        print("Warmup complete")
    
    def _capture_loop(self):
        """Continuous screen capture thread using win32rt CaptureBackend."""
        frame_delay = 1.0 / self.cfg.target_fps
        last_geom = 0.0
        while not self._stop.is_set():
            t0 = time.perf_counter()
            try:
                # Re-centre periodically: the game may switch resolution
                # or fullscreen mode after startup.
                if t0 - last_geom > 2.0:
                    last_geom = t0
                    try:
                        import input as win32rt
                        vx, vy, vw, vh = win32rt.virtual_screen()
                        cx = vx + vw // 2
                        cy = vy + vh // 2
                        r = self.cfg.capture_radius
                        region = (cx - r, cy - r, cx + r, cy + r)
                        if region != self._capture_region:
                            with self._lock:
                                self._capture_region = region
                            print(f"Capture region updated: {2*r}x{2*r} "
                                  f"@ ({cx-r},{cy-r})")
                    except Exception:
                        pass
                l, t, r, b = self._capture_region
                frame = self._capture.grab(l, t, r, b)
                if frame is None:
                    time.sleep(0.001)
                    continue
                    
                # Frame is already BGR from win32rt
                try:
                    self._frame_queue.put_nowait(frame)
                    self._stats["frames_captured"] += 1
                except queue.Full:
                    pass
                    
            except Exception as e:
                print(f"Capture error: {e}")
                time.sleep(0.01)
            
            elapsed = time.perf_counter() - t0
            self._stats["avg_capture_ms"] = self._stats["avg_capture_ms"] * 0.99 + elapsed * 1000 * 0.01
            sleep_time = frame_delay - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
    
    def _inference_loop(self):
        """Continuous inference thread."""
        while not self._stop.is_set():
            try:
                frame = self._frame_queue.get(timeout=0.1)
            except queue.Empty:
                continue
                
            t0 = time.perf_counter()
            try:
                detections = self._detect(frame)
                self._stats["frames_inferred"] += 1
                self._stats["detections_found"] += len(detections)

                # Stamp every result with a monotonically increasing frame
                # number so consumers can tell fresh sightings from repeats.
                with self._lock:
                    self._frame_seq += 1
                    seq = self._frame_seq
                stamp = time.perf_counter()
                for det in detections:
                    det.seq = seq
                    det.t = stamp

                try:
                    self._result_queue.put_nowait(detections)
                except queue.Full:
                    pass

                # Update latest detection (thread-safe)
                with self._lock:
                    if detections:
                        self._latest_detection = max(detections, key=lambda d: d.conf)
                    else:
                        self._latest_detection = None
                        
            except Exception as e:
                print(f"Inference error: {e}")
            
            elapsed = time.perf_counter() - t0
            self._stats["avg_infer_ms"] = self._stats["avg_infer_ms"] * 0.99 + elapsed * 1000 * 0.01
    
    def _preprocess(self, frame: np.ndarray) -> np.ndarray:
        """Preprocess frame for YOLO input: resize, normalize, CHW, batch.
        
        Optimized for square capture region (90x90) -> direct resize to input_size.
        No letterbox padding needed since capture is square.
        """
        # Direct resize - capture region is square (90x90)
        resized = cv2.resize(frame, (self.cfg.input_size, self.cfg.input_size), 
                            interpolation=cv2.INTER_LINEAR)
        
        # HWC -> CHW, normalize, batch
        blob = resized.astype(np.float32) / 255.0
        blob = np.transpose(blob, (2, 0, 1))[None]  # (1, 3, H, W)
        return blob
    
    def _detect(self, frame: np.ndarray) -> List[Detection]:
        """Run YOLO inference and return filtered human detections."""
        # Preprocess
        blob = self._preprocess(frame)
        
        # Inference
        outputs = self._session.run(self._output_names, {self._input_name: blob})
        
        # Parse YOLOv8/YOLO11 output: (1, 84, 8400) -> (8400, 84)
        # 84 = 4 box + 80 classes
        # IMPORTANT: Ultralytics ONNX export outputs boxes as [x1, y1, x2, y2] in input_size pixels
        preds = outputs[0][0].T  # (8400, 84)
        
        # Filter by person class confidence
        person_scores = preds[:, 4 + self.cfg.person_class_id]
        mask = person_scores > self.cfg.conf_threshold
        if not np.any(mask):
            return []
            
        preds = preds[mask]
        scores = person_scores[mask]
        
        # Ultralytics ONNX export (YOLO26n) outputs boxes as
        # [cx, cy, w, h] in input_size pixels - NOT corners, and NOT
        # normalised.  Validate with the raw scores before changing:
        # a person at the crop edge reads e.g. [37, 71, 69, 139].
        boxes = preds[:, :4]
        bcx = boxes[:, 0]
        bcy = boxes[:, 1]
        bw = boxes[:, 2]
        bh = boxes[:, 3]

        # NMSBoxes wants [left, top, width, height].
        nms_boxes = np.stack([bcx - bw / 2.0, bcy - bh / 2.0, bw, bh],
                             axis=1)

        # NMS
        indices = cv2.dnn.NMSBoxes(
            nms_boxes.tolist(), scores.tolist(),
            self.cfg.conf_threshold, self.cfg.iou_threshold
        )

        if len(indices) == 0:
            return []

        indices = indices.flatten() if isinstance(indices, np.ndarray) else indices
        nms_boxes = nms_boxes[indices]
        bcx = bcx[indices]
        bcy = bcy[indices]
        bw = bw[indices]
        bh = bh[indices]
        scores = scores[indices]

        # Map back to screen coordinates
        r = self.cfg.capture_radius
        l, t, _r2, _b2 = self._capture_region
        scale_x = (2 * r) / self.cfg.input_size
        scale_y = (2 * r) / self.cfg.input_size

        detections = []
        for _box, center_x, center_y, box_w, box_h, conf in zip(
                nms_boxes, bcx, bcy, bw, bh, scores):
            aspect = box_h / box_w if box_w > 0 else 0

            # Filter by aspect ratio (upright human ~ 2:1)
            if aspect < self.cfg.min_aspect_ratio or aspect > self.cfg.max_aspect_ratio:
                continue

            # Centre in screen coordinates.  The crop is 2*r wide and is
            # resized to input_size, so one input pixel is 2*r/input_size
            # screen pixels.  Map the centre directly from the crop's
            # top-left origin - do NOT subtract the radius here: that
            # would subtract r *input* pixels (r * scale screen px)
            # instead of r screen px and bias every detection.
            x = int(l + center_x * scale_x)
            y = int(t + center_y * scale_y)
            sw = int(box_w * scale_x)
            sh = int(box_h * scale_y)

            detections.append(Detection(x=x, y=y, w=sw, h=sh,
                                        conf=float(conf),
                                        aspect_ratio=float(aspect)))

        return detections

    def get_latest_detection(self, after_seq: Optional[int] = None,
                             max_age: Optional[float] = None) -> Optional[Detection]:
        """Get the most recent detection (thread-safe).

        after_seq: only return a detection from a frame newer than this
        sequence number (prevents re-using one sighting for several
        corrections).  max_age: reject results older than this many
        seconds, so a stalled capture thread cannot keep steering.
        """
        import dataclasses
        with self._lock:
            det = self._latest_detection
        if det is None:
            return None
        det = dataclasses.replace(det)
        if after_seq is not None and det.seq <= after_seq:
            return None
        if max_age is not None and (time.perf_counter() - det.t) > max_age:
            return None
        return det
    
    def get_detections(self) -> List[Detection]:
        """Get all detections from latest frame (non-blocking)."""
        try:
            return self._result_queue.get_nowait()
        except queue.Empty:
            return []
    
    def get_stats(self) -> dict:
        """Get performance statistics."""
        return self._stats.copy()
    
    def stop(self):
        """Stop all threads and cleanup."""
        self._stop.set()
        if self._capture_thread:
            self._capture_thread.join(timeout=1.0)
        if self._infer_thread:
            self._infer_thread.join(timeout=1.0)
        if self._capture:
            # win32rt CaptureBackend doesn't have explicit close, but we can dereference
            self._capture = None
        print("YOLO26n backend stopped")
    
    def __enter__(self):
        self.start()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


def test_on_video(video_path: str, cfg: Optional[YOLOConfig] = None):
    """Test YOLO26n on a recorded video file."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Cannot open video: {video_path}")
        return
        
    backend = YOLO26nBackend(cfg)
    if not backend.start():
        return
        
    frame_count = 0
    total_time = 0.0
    
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
                
            frame_count += 1
            t0 = time.perf_counter()
            
            # Run detection directly (bypass capture thread for video test)
            detections = backend._detect(frame)
            
            elapsed = time.perf_counter() - t0
            total_time += elapsed
            
            # Draw detections
            for det in detections:
                x1, y1 = det.x - det.w//2, det.y - det.h//2
                x2, y2 = det.x + det.w//2, det.y + det.h//2
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.putText(frame, f"{det.conf:.2f} ar={det.aspect_ratio:.1f}", 
                           (x1, y1-5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
            
            cv2.putText(frame, f"Frame: {frame_count} | {elapsed*1000:.1f}ms | Dets: {len(detections)}",
                       (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            
            cv2.imshow("YOLO26n Video Test", frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
                
    finally:
        cap.release()
        backend.stop()
        cv2.destroyAllWindows()
        
    if frame_count > 0:
        avg_ms = (total_time / frame_count) * 1000
        print(f"\nVideo test complete: {frame_count} frames, avg {avg_ms:.1f}ms/frame ({1000/avg_ms:.1f} FPS)")


if __name__ == "__main__":
    import sys
    
    video_path = r"C:\Users\root\Documents\GitHub\snappy-detector\testdata\recording\recording.mkv"
    
    cfg = YOLOConfig(
        model_path="models/yolo26n.onnx",
        capture_radius=45,
        conf_threshold=0.35,
        input_size=640,
        target_fps=120,
    )
    
    if len(sys.argv) > 1 and sys.argv[1] == "--live":
        # Live screen detection mode
        backend = YOLO26nBackend(cfg)
        if backend.start():
            print("Live detection running. Press Ctrl+C to stop.")
            try:
                while True:
                    det = backend.get_latest_detection()
                    if det:
                        print(f"Detected: x={det.x}, y={det.y}, w={det.w}, h={det.h}, conf={det.conf:.2f}, ar={det.aspect_ratio:.2f}")
                    time.sleep(0.1)
            except KeyboardInterrupt:
                pass
            finally:
                backend.stop()
    else:
        # Video file test mode
        test_on_video(video_path, cfg)

# ======================================================================
# merged from hpose.py
# ======================================================================

"""Human pose detection and analysis (single flat module).

Merged from the former hpose_* modules: keypoint model, motion tracking,
3D skeleton fitting, feature extraction, stance/view classifiers, ONNX
detectors, model bundle loading, per-subject analysis, tracking, and the
bridge used by the vision/assist layers.
"""

# ======================================================================
# merged from hpose_keypoints.py
# ======================================================================

import numpy as np

N_KP = 17

FACE_IDX = (0, 1, 2, 3, 4)

KEYPOINT_NAMES = (
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
)


def derive_joints(kps: np.ndarray) -> np.ndarray:
    kps = np.asarray(kps, dtype=np.float32)
    if kps.ndim != 2 or kps.shape[0] < N_KP or kps.shape[1] < 3:
        raise ValueError("expected a (17, 3) keypoint array")
    ls = kps[5]
    rs = kps[6]
    lh = kps[11]
    rh = kps[12]
    neck = np.array([(ls[0] + rs[0]) * 0.5,
                     (ls[1] + rs[1]) * 0.5,
                     min(ls[2], rs[2])], dtype=np.float32)
    pelvis = np.array([(lh[0] + rh[0]) * 0.5,
                       (lh[1] + rh[1]) * 0.5,
                       min(lh[2], rh[2])], dtype=np.float32)
    nose = kps[0]
    head = np.array([nose[0] * 0.45 + neck[0] * 0.55,
                     nose[1] * 0.45 + neck[1] * 0.55,
                     min(nose[2], neck[2])], dtype=np.float32)
    return np.concatenate([kps[:N_KP], neck[None], pelvis[None],
                           head[None]], axis=0)

class Pose(object):

    __slots__ = ("kps", "box", "score", "meta")

    def __init__(self, kps, box, score=1.0, meta=None):
        self.kps = np.asarray(kps, dtype=np.float32)
        if self.kps.shape[0] < N_KP:
            raise ValueError("kps must have at least 17 rows")
        self.box = tuple(float(v) for v in box)
        self.score = float(score)
        self.meta = dict(meta or {})

    @property
    def derived(self) -> np.ndarray:
        return derive_joints(self.kps)

    @property
    def xy(self) -> np.ndarray:
        return self.kps[:N_KP, :2]

    @property
    def conf(self) -> np.ndarray:
        return self.kps[:N_KP, 2]

    @property
    def width(self) -> float:
        return max(0.0, self.box[2] - self.box[0])

    @property
    def height(self) -> float:
        return max(0.0, self.box[3] - self.box[1])

    @property
    def center(self):
        return ((self.box[0] + self.box[2]) * 0.5,
                (self.box[1] + self.box[3]) * 0.5)

    @property
    def area(self) -> float:
        return self.width * self.height

    def copy(self) -> "Pose":
        return Pose(self.kps.copy(), self.box, self.score, dict(self.meta))

    def __repr__(self) -> str:
        return "Pose(box=%s, score=%.3f)" % (
            tuple(round(v, 1) for v in self.box), self.score)

def box_iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = ix2 - ix1
    ih = iy2 - iy1
    if iw <= 0.0 or ih <= 0.0:
        return 0.0
    inter = iw * ih
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    if union <= 0.0:
        return 0.0
    return float(inter / union)

def clip_box(box, width, height):
    x1 = min(max(0.0, float(box[0])), float(width))
    y1 = min(max(0.0, float(box[1])), float(height))
    x2 = min(max(0.0, float(box[2])), float(width))
    y2 = min(max(0.0, float(box[3])), float(height))
    if x2 <= x1:
        x2 = min(float(width), x1 + 1.0)
    if y2 <= y1:
        y2 = min(float(height), y1 + 1.0)
    return (x1, y1, x2, y2)


# ======================================================================
# merged from hpose_motion.py
# ======================================================================

import math

import numpy as np

STATIC = "static"
SLOW = "slow"
FAST = "fast"

FAST_SPEED = 260.0
SLOW_SPEED = 60.0

class KalmanBox(object):

    __slots__ = ("x", "p", "last_t", "accel", "_h0", "_r_var")

    def __init__(self, box, t: float = 0.0, accel: float = 1.0,
                 meas_var: float = None):
        h = max(1.0, float(box[3]) - float(box[1]))
        if meas_var is None:
            meas_var = max(1.0, (0.05 * h) ** 2)
        vel_var = max(4.0, (2.0 * h) ** 2)
        self.x = np.zeros(6, dtype=np.float64)
        self.x[0] = (box[0] + box[2]) * 0.5
        self.x[1] = (box[1] + box[3]) * 0.5
        self.x[2] = math.log(max(1.0, box[2] - box[0]))
        self.x[3] = math.log(max(1.0, box[3] - box[1]))
        self.p = np.diag([meas_var, meas_var, 0.25, 0.25, vel_var, vel_var])
        self.last_t = float(t)
        self.accel = float(accel)
        self._h0 = h
        self._r_var = float(meas_var)

    def predict(self, t: float) -> np.ndarray:
        t = max(float(t), self.last_t)             # monotonic guard
        dt = max(1e-3, min(0.5, t - self.last_t))
        self.last_t = t
        h = self._h0
        vel_noise = max(2.0, 0.05 * h) ** 2        # px^2/s^2, size scaled
        f = np.eye(6)
        f[0, 4] = dt
        f[1, 5] = dt
        self.x = f @ self.x
        q = np.diag([0.35, 0.35, 0.02, 0.02,
                     vel_noise * self.accel,
                     vel_noise * self.accel]) * dt
        self.p = f @ self.p @ f.T + q
        return self.x.copy()

    def update(self, box, t: float = None) -> None:
        if t is not None and float(t) > self.last_t + 1e-9:
            self.predict(float(t))
        cx = (box[0] + box[2]) * 0.5
        cy = (box[1] + box[3]) * 0.5
        w = math.log(max(1.0, box[2] - box[0]))
        h = math.log(max(1.0, box[3] - box[1]))
        z = np.array([cx, cy, w, h], dtype=np.float64)
        hmat = np.zeros((4, 6), dtype=np.float64)
        hmat[0, 0] = 1.0
        hmat[1, 1] = 1.0
        hmat[2, 2] = 1.0
        hmat[3, 3] = 1.0
        h_box = max(1.0, box[3] - box[1])
        r_var = max(1.0, (0.05 * h_box) ** 2)
        r_var = 0.8 * self._r_var + 0.2 * r_var
        self._r_var = r_var
        r = np.eye(4) * r_var
        y = z - hmat @ self.x
        s = hmat @ self.p @ hmat.T + r
        try:
            k = np.linalg.solve(s.T, (self.p @ hmat.T).T).T
        except np.linalg.LinAlgError:
            return
        self.x = self.x + k @ y
        eye = np.eye(6)
        I_kh = eye - k @ hmat
        self.p = I_kh @ self.p @ I_kh.T + k @ r @ k.T

    @property
    def velocity(self):
        return float(self.x[4]), float(self.x[5])

    @property
    def speed(self) -> float:
        vx, vy = self.velocity
        return math.hypot(vx, vy)

    @property
    def size(self):
        return float(math.exp(self.x[2])), float(math.exp(self.x[3]))

    def box(self, offset=(0.0, 0.0)):
        w, h = self.size
        cx = self.x[0] + offset[0]
        cy = self.x[1] + offset[1]
        return (cx - w * 0.5, cy - h * 0.5, cx + w * 0.5, cy + h * 0.5)

class MotionModel(object):

    def __init__(self, smoothing: float = 0.45, max_history: int = 90,
                 body_h_px: float = None):
        self.smoothing = float(smoothing)
        self.max_history = int(max_history)
        self.samples = []
        self.vel = (0.0, 0.0)
        self.accel = (0.0, 0.0)
        self.vel_raw = (0.0, 0.0)
        self.body_h_px = float(body_h_px) if body_h_px else None

    def reset(self) -> None:
        self.samples = []
        self.vel = (0.0, 0.0)
        self.accel = (0.0, 0.0)
        self.vel_raw = (0.0, 0.0)

    def add(self, t: float, x: float, y: float) -> None:
        if self.samples:
            t_prev, x_prev, y_prev = self.samples[-1]
            dt = float(t) - t_prev
            if dt > 1e-6:
                self.vel_raw = ((x - x_prev) / dt, (y - y_prev) / dt)
        self.samples.append((float(t), float(x), float(y)))
        if len(self.samples) > self.max_history:
            del self.samples[0]
        self._recompute()

    def _recompute(self) -> None:
        if len(self.samples) < 2:
            self.vel = (0.0, 0.0)
            self.accel = (0.0, 0.0)
            return
        t0, x0, y0 = self.samples[0]
        t1, x1, y1 = self.samples[-1]
        dt = t1 - t0
        if dt < 1e-6:
            self.vel = (0.0, 0.0)
            return
        vx = (x1 - x0) / dt
        vy = (y1 - y0) / dt
        a = self.smoothing
        self.vel = (a * vx + (1.0 - a) * self.vel[0],
                    a * vy + (1.0 - a) * self.vel[1])
        if len(self.samples) >= 3:
            tm, xm, ym = self.samples[len(self.samples) // 2]
            dtm = tm - t0
            if dtm > 1e-6:
                vxm = (xm - x0) / dtm
                vym = (ym - y0) / dtm
                ax = (vx - vxm) / max(1e-6, (t1 - tm))
                ay = (vy - vym) / max(1e-6, (t1 - tm))
                self.accel = (0.6 * ax + 0.4 * self.accel[0],
                              0.6 * ay + 0.4 * self.accel[1])

    @property
    def speed(self) -> float:
        return math.hypot(self.vel[0], self.vel[1])

    def state(self) -> str:
        # Thresholds are scaled by the target's body height, so the state
        # depends on "how many body-lengths per second", not absolute px/s.
        h = self.body_h_px or 100.0
        fast = 2.0 * h          # adults: 200 px/s approx walk
        slow = 0.5 * h          # adults: 50 px/s approx sway
        s = self.speed
        if s >= fast:
            return FAST
        if s >= slow:
            return SLOW
        return STATIC

    def direction(self):
        s = self.speed
        if s < 1e-6:
            return None
        return (self.vel[0] / s, self.vel[1] / s)

    def predict(self, seconds: float, use_acceleration: bool = True):
        t = max(0.0, float(seconds))
        ox = self.vel[0] * t
        oy = self.vel[1] * t
        # Acceleration is only meaningful after 6+ samples; otherwise it
        # is measurement jitter and leads to over-extrapolation.
        if (use_acceleration and t > 0.0 and len(self.samples) >= 6):
            ox += 0.5 * self.accel[0] * t * t
            oy += 0.5 * self.accel[1] * t * t
        if self.samples:
            x = self.samples[-1][1] + ox
            y = self.samples[-1][2] + oy
        else:
            x = ox
            y = oy
        return x, y, ox, oy


# ======================================================================
# merged from hpose_model3d.py
# ======================================================================

import math

import numpy as np


DEFAULT_PROPORTIONS = {
    "hip_hw": 0.090,
    "shoulder_hw": 0.190,
    "pelvis_y": 0.940,
    "spine_len": 0.500,
    "neck_len": 0.115,
    "upper_arm": 0.300,
    "forearm": 0.265,
    "thigh": 0.430,
    "shin": 0.435,
    "foot": 0.170,
}

FACE_OFFSETS = {
    "nose": (0.000, -0.012, 0.108),
    "left_eye": (-0.032, 0.040, 0.092),
    "right_eye": (0.032, 0.040, 0.092),
    "left_ear": (-0.078, 0.015, 0.010),
    "right_ear": (0.078, 0.015, 0.010),
}

def _rot_x(v: np.ndarray, ang: float) -> np.ndarray:
    c = math.cos(ang)
    s = math.sin(ang)
    return np.array([v[0], v[1] * c - v[2] * s, v[1] * s + v[2] * c])

def _rot_z(v: np.ndarray, ang: float) -> np.ndarray:
    c = math.cos(ang)
    s = math.sin(ang)
    return np.array([v[0] * c - v[1] * s, v[0] * s + v[1] * c, v[2]])

def random_person(rng: np.random.Generator, scale: float = 1.0) -> dict:
    base = DEFAULT_PROPORTIONS
    p = {}
    p["hip_hw"] = base["hip_hw"] * scale * rng.uniform(0.85, 1.18)
    p["shoulder_hw"] = base["shoulder_hw"] * scale * rng.uniform(0.85, 1.18)
    p["pelvis_y"] = base["pelvis_y"] * scale * rng.uniform(0.88, 1.12)
    p["spine_len"] = base["spine_len"] * scale * rng.uniform(0.92, 1.08)
    p["neck_len"] = base["neck_len"] * scale * rng.uniform(0.9, 1.15)
    p["upper_arm"] = base["upper_arm"] * scale * rng.uniform(0.9, 1.1)
    p["forearm"] = base["forearm"] * scale * rng.uniform(0.9, 1.1)
    p["thigh"] = base["thigh"] * scale * rng.uniform(0.9, 1.12)
    p["shin"] = base["shin"] * scale * rng.uniform(0.9, 1.12)
    p["foot"] = base["foot"] * scale * rng.uniform(0.9, 1.1)
    return p

def build_skeleton(p: dict, stance: str = "stand", amount: float = 0.0,
                    rng: np.random.Generator = None) -> np.ndarray:
    rng = rng or np.random.default_rng(0)
    j = np.zeros((N_KP, 3), dtype=np.float64)

    torso_pitch = 0.0
    hip_pitch = 0.0
    knee_flex = 0.0
    pelvis_drop = 0.0
    arm_pitch = 0.0
    arm_pitch_b = 0.0
    arm_abduct = 0.0
    elbow_flex = 0.0
    elbow_flex_b = 0.0
    body_pitch = 0.0
    stride = 0.0

    if stance == "crouch":
        amount = float(np.clip(amount, 0.0, 1.0))
        pelvis_drop = 0.40 * amount
        hip_pitch = math.radians(-68.0 * amount)
        knee_flex = math.radians(74.0 * amount)
        torso_pitch = math.radians(22.0 * amount)
        arm_pitch = math.radians(18.0 * amount)
    elif stance == "prone":
        amount = float(np.clip(amount, 0.0, 1.0))
        body_pitch = math.radians(70.0 + 20.0 * amount)
        pelvis_drop = 0.80 * amount
        knee_flex = math.radians(70.0 * amount)
        hip_pitch = math.radians(-18.0 * amount)
        elbow_flex = math.radians(60.0 * amount)
        arm_pitch = math.radians(35.0 * amount)
    elif stance == "jump":
        amount = float(np.clip(amount, 0.0, 1.0))
        hip_pitch = math.radians(-30.0 * amount)
        knee_flex = math.radians(55.0 * amount)
        pelvis_drop = -0.10 * amount
        arm_pitch = math.radians(135.0 * amount)
        elbow_flex = math.radians(25.0 * amount)
    elif stance == "sit":
        amount = float(np.clip(amount, 0.0, 1.0))
        pelvis_drop = 0.50 * amount
        hip_pitch = math.radians(-88.0 * amount)
        knee_flex = math.radians(86.0 * amount)
        torso_pitch = math.radians(7.0 * amount)
        arm_pitch = math.radians(24.0 * amount)
        elbow_flex = math.radians(72.0 * amount)
    elif stance == "walk":
        amount = float(np.clip(amount, 0.0, 1.0))
        stride = math.radians(24.0 * amount)
        hip_pitch = stride
        knee_flex = math.radians(max(4.0, 26.0 * amount))
        arm_pitch = -stride * 0.8
        elbow_flex = math.radians(14.0 * amount)

    torso_pitch += math.radians(rng.uniform(-12.0, 16.0))
    arm_pitch += math.radians(rng.uniform(-35.0, 35.0))
    arm_pitch_b = arm_pitch + math.radians(rng.uniform(-40.0, 40.0))
    elbow_flex += math.radians(rng.uniform(0.0, 80.0))
    elbow_flex_b = elbow_flex + math.radians(rng.uniform(-35.0, 45.0))
    arm_abduct += math.radians(rng.uniform(0.0, 25.0))

    pelvis = np.array([0.0, p["pelvis_y"] - pelvis_drop, 0.0])
    neck_local = np.array([0.0, p["spine_len"], 0.0])
    neck = pelvis + _rot_x(neck_local, torso_pitch)
    head = neck + _rot_x(np.array([0.0, p["neck_len"], 0.0]), torso_pitch)

    def place_face(idx, off):
        ox, oy, oz = off
        local = np.array([ox, oy, oz])
        pos = head + _rot_x(local, torso_pitch)
        j[idx] = pos

    place_face(0, FACE_OFFSETS["nose"])
    place_face(1, FACE_OFFSETS["left_eye"])
    place_face(2, FACE_OFFSETS["right_eye"])
    place_face(3, FACE_OFFSETS["left_ear"])
    place_face(4, FACE_OFFSETS["right_ear"])

    sh_off = _rot_x(np.array([p["shoulder_hw"], -0.055 * p["spine_len"],
                              0.0]), torso_pitch)
    sh_r = neck + np.array([sh_off[0], sh_off[1], sh_off[2]])
    sh_l = neck + np.array([-sh_off[0], sh_off[1], sh_off[2]])
    j[5] = sh_l
    j[6] = sh_r

    for sign, (sidx, eidx, widx) in ((-1.0, (5, 7, 9)), (1.0, (6, 8, 10))):
        use_pitch = arm_pitch if sign < 0 else arm_pitch_b
        use_elbow = elbow_flex if sign < 0 else elbow_flex_b
        upper = np.array([0.0, -p["upper_arm"], 0.0])
        upper = _rot_x(upper, use_pitch + stride * (1.0 if sign > 0 else -1.0))
        upper = _rot_z(upper, -sign * arm_abduct)
        elbow = j[sidx] + upper
        fore = np.array([0.0, -p["forearm"], 0.0])
        fore = _rot_x(fore, use_pitch + use_elbow
                      + stride * (1.0 if sign > 0 else -1.0))
        fore = _rot_z(fore, -sign * arm_abduct)
        j[eidx] = elbow
        j[widx] = elbow + fore

    hip_r = pelvis + np.array([p["hip_hw"], 0.0, 0.0])
    hip_l = pelvis + np.array([-p["hip_hw"], 0.0, 0.0])
    j[11] = hip_l
    j[12] = hip_r
    for sign, (hidx, kidx, aidx) in ((-1.0, (11, 13, 15)), (1.0, (12, 14, 16))):
        hp = hip_pitch + stride * (1.0 if sign > 0 else -1.0)
        thigh = _rot_x(np.array([0.0, -p["thigh"], 0.0]), hp)
        knee = j[hidx] + thigh
        shin = _rot_x(np.array([0.0, -p["shin"], 0.0]), hp + knee_flex)
        ankle = knee + shin
        j[kidx] = knee
        j[aidx] = ankle

    if body_pitch:
        pivot = j[11].copy() * 0.5 + j[12].copy() * 0.5
        j = np.array([_rot_x(v - pivot, body_pitch) + pivot for v in j])
        j[:, 1] -= 0.02
    return j

def body_to_camera(points: np.ndarray, yaw_deg: float) -> np.ndarray:
    t = math.radians(float(yaw_deg))
    c = math.cos(t)
    s = math.sin(t)
    out = np.empty_like(points)
    px = points[:, 0]
    py = points[:, 1]
    pz = points[:, 2]
    out[:, 0] = -px * c + pz * s
    out[:, 1] = -py
    out[:, 2] = -px * s - pz * c
    return out

def body_to_camera_batch(points: np.ndarray,
                         yaws: np.ndarray) -> np.ndarray:
    yaws = np.asarray(yaws, dtype=np.float64).reshape(-1)
    t = np.radians(yaws)
    c = np.cos(t)[:, None]
    s = np.sin(t)[:, None]
    px = points[None, :, 0]
    py = points[None, :, 1]
    pz = points[None, :, 2]
    out = np.empty((len(yaws), points.shape[0], 3), dtype=np.float64)
    out[:, :, 0] = -px * c + pz * s
    out[:, :, 1] = -py
    out[:, :, 2] = -px * s - pz * c
    return out

def project(points_cam: np.ndarray, focal: float, depth: float,
            cx: float, cy: float) -> np.ndarray:
    z = points_cam[:, 2] + depth
    z = np.where(np.abs(z) < 1e-3, 1e-3, z)
    u = cx + focal * points_cam[:, 0] / z
    v = cy + focal * points_cam[:, 1] / z
    return np.stack([u, v], axis=1)

def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)

def face_visibility(yaw_deg: float) -> dict:
    t = math.radians(float(yaw_deg))
    c = math.cos(t)
    s = math.sin(t)
    facing = c
    nose = _clamp01((facing + 0.28) / 0.55)
    eye = _clamp01((facing - 0.05) / 0.45)
    near_ear = 0.30 + 0.60 * abs(s)
    far_ear = 0.30 * (1.0 - abs(s)) + 0.45 * max(0.0, -facing)
    if abs(s) < 0.20:
        ear_l = ear_r = max(0.25, 0.30 + 0.45 * max(0.0, -facing))
    elif s > 0:
        ear_r = near_ear
        ear_l = far_ear
    else:
        ear_l = near_ear
        ear_r = far_ear
    nose = max(nose, 0.28)
    eye = max(eye, 0.22)
    return {
        "nose": nose,
        "left_eye": eye,
        "right_eye": eye,
        "left_ear": _clamp01(ear_l),
        "right_ear": _clamp01(ear_r),
    }

def _body_extremes(joints: np.ndarray) -> np.ndarray:
    neck = (joints[5] + joints[6]) * 0.5
    head_top = neck + np.array([0.0, 0.175, -0.005])
    toe_r = joints[16] + np.array([0.0, -0.045, 0.135])
    toe_l = joints[15] + np.array([0.0, -0.045, 0.135])
    hand_r = joints[10] + np.array([0.02, -0.05, 0.0])
    hand_l = joints[9] + np.array([-0.02, -0.05, 0.0])
    return np.stack([head_top, toe_r, toe_l, hand_r, hand_l])

def make_sample(rng: np.random.Generator, stance: str = "stand",
                amount: float = 0.0, image_size=(640, 640)):
    H, W = image_size
    p = random_person(rng, scale=rng.uniform(0.92, 1.06))
    joints = build_skeleton(p, stance, amount, rng)

    yaw = float(rng.uniform(-180.0, 180.0))

    px_sign = np.array([-1.0 if name.startswith("left") else
                        (1.0 if name.startswith("right") else 0.0)
                        for name in KEYPOINT_NAMES])
    turned = math.sin(math.radians(yaw))
    if abs(turned) > 0.2:
        hide = 0.35 + 0.5 * min(1.0, abs(turned))
        for i in range(N_KP):
            if px_sign[i] * turned < -0.25:
                joints[i, 0] *= hide

    cam = body_to_camera(joints, yaw)

    body_h = float(joints[:, 1].max() - joints[:, 1].min())
    focal = float(W * rng.uniform(0.85, 1.6))
    target_frac = rng.uniform(0.45, 0.95)
    depth = focal * body_h / max(1e-3, (target_frac * H))

    px = project(cam, focal, depth, W * 0.5, H * 0.5)
    extremes = project(body_to_camera(_body_extremes(joints), yaw), focal,
                       depth, W * 0.5, H * 0.5)
    allpts = np.concatenate([px, extremes], axis=0)
    centre = (allpts.min(axis=0) + allpts.max(axis=0)) * 0.5
    shift = np.array([W * 0.5, H * 0.5]) - centre
    px = px + shift
    extremes = extremes + shift
    vis = face_visibility(yaw)
    conf = np.ones(N_KP, dtype=np.float64)
    for name, idx in ((k, i) for i, k in enumerate(KEYPOINT_NAMES)):
        if name in vis:
            conf[idx] = vis[name]

    far_penalty = np.where(px_sign * turned < -0.30, 0.28, 0.0)
    conf = np.clip(conf - far_penalty, 0.02, 1.0)

    x1 = float(min(px[:, 0].min(), extremes[:, 0].min()))
    x2 = float(max(px[:, 0].max(), extremes[:, 0].max()))
    y1 = float(min(px[:, 1].min(), extremes[:, 1].min()))
    y2 = float(max(px[:, 1].max(), extremes[:, 1].max()))
    box = (x1, y1, x2, y2)
    kps = np.concatenate([px, conf[:, None]], axis=1)
    return {"kps": kps, "box": box, "yaw": yaw, "stance": stance,
            "visible": conf, "props": p, "focal": focal, "depth": depth,
            "body_height": body_h}

def yaw_to_class(yaw_deg: float) -> str:
    y = float(yaw_deg) % 360.0
    if y < 22.5 or y >= 337.5:
        return "front"
    if y < 67.5:
        return "front_right_45"
    if y < 112.5:
        return "side_right"
    if y < 157.5:
        return "back_right_45"
    if y < 202.5:
        return "back"
    if y < 247.5:
        return "back_left_45"
    if y < 292.5:
        return "side_left"
    return "front_left_45"

VIEW_CLASSES = (
    "front",
    "front_right_45",
    "side_right",
    "back_right_45",
    "back",
    "back_left_45",
    "side_left",
    "front_left_45",
)

VIEW_CLASS_YAW = {
    "front": 0.0,
    "front_right_45": 45.0,
    "side_right": 90.0,
    "back_right_45": 135.0,
    "back": 180.0,
    "back_left_45": 225.0,
    "side_left": 270.0,
    "front_left_45": 315.0,
}


# ======================================================================
# merged from hpose_posefit.py
# ======================================================================

import math

import numpy as np


CANON = build_skeleton(DEFAULT_PROPORTIONS, "stand")

FACE_JOINTS = (0, 1, 2, 3, 4)

_COARSE_STEPS = 24
_FINE_STEPS = 13
_FINE_SPAN = 14.0

UPPER_JOINTS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10)

LEAN_VALUES = (-26.0, -17.0, -9.0, 0.0, 9.0, 17.0, 26.0)

_DEPTH_FACTORS = (0.4, 0.65, 1.0, 1.6, 2.6)

BODY_HEIGHT_M = 1.72

def _normalise(tpl: np.ndarray) -> np.ndarray:
    tpl = np.asarray(tpl, dtype=np.float64)
    h = float(tpl[:, 1].max() - tpl[:, 1].min())
    if h < 1e-6:
        return tpl
    return tpl / h

CANON_N = _normalise(CANON)

def _batch_solve(fx: np.ndarray, fy: np.ndarray, obs: np.ndarray,
                 w: np.ndarray):
    k = fx.shape[0]
    sw = np.sqrt(np.maximum(w, 0.0))[None, :]
    a = np.zeros((k, 2 * N_KP, 3), dtype=np.float64)
    a[:, 0::2, 0] = fx * sw
    a[:, 0::2, 1] = sw
    a[:, 1::2, 0] = fy * sw
    a[:, 1::2, 2] = sw
    b = np.empty((k, 2 * N_KP), dtype=np.float64)
    b[:, 0::2] = obs[:, 0][None, :] * sw
    b[:, 1::2] = obs[:, 1][None, :] * sw
    ata = np.matmul(a.transpose(0, 2, 1), a)
    atb = np.matmul(a.transpose(0, 2, 1), b[:, :, None])[:, :, 0]
    try:
        theta = np.linalg.solve(ata, atb[:, :, None])[:, :, 0]
    except np.linalg.LinAlgError:
        z = np.zeros((k, 3))
        z[:, 0] = 1.0
        return z[:, 0], z[:, 1], z[:, 2], np.full(k, np.inf)
    diff = np.matmul(a, theta[:, :, None])[:, :, 0] - b
    resid = np.sum(diff * diff, axis=1)
    return theta[:, 0], theta[:, 1], theta[:, 2], resid

def _projectors(yaws: np.ndarray, inv_depth: float,
                 template: np.ndarray):
    pts = body_to_camera_batch(template, yaws)
    den = 1.0 + inv_depth * pts[:, :, 2]

    den = np.where(np.abs(den) < 1e-3, 1e-3, den)
    return pts[:, :, 0] / den, pts[:, :, 1] / den

def personalise(obs: np.ndarray, w: np.ndarray, template: np.ndarray):
    tpl = np.asarray(template, dtype=np.float64)
    if w[5] + w[6] < 0.5 or w[11] + w[12] < 0.5:
        return tpl
    torso_obs = float(np.hypot(*((((obs[5] + obs[6]) * 0.5)
                                  - ((obs[11] + obs[12]) * 0.5))[:2])))
    if torso_obs < 1e-3:
        return tpl
    t_torso = float(np.hypot(*((((tpl[5] + tpl[6]) * 0.5)
                                 - ((tpl[11] + tpl[12]) * 0.5))[:2])))
    t_sh = float(abs(tpl[6][0] - tpl[5][0]))
    if t_torso < 1e-6 or t_sh < 1e-6:
        return tpl
    f_y = torso_obs / t_torso
    f_x = (0.76 * torso_obs) / t_sh
    out = tpl.copy()
    out[:, 0] *= f_x
    out[:, 1] *= f_y
    return out

def _leaned(template: np.ndarray, lean_deg: float) -> np.ndarray:
    lean_deg = float(lean_deg)
    if abs(lean_deg) < 1e-6:
        return template
    t = np.asarray(template, dtype=np.float64).copy()
    a = math.radians(lean_deg)
    c = math.cos(a)
    s = math.sin(a)
    pivot = (t[11] + t[12]) * 0.5
    for i in UPPER_JOINTS:
        v = t[i] - pivot
        t[i] = np.array([v[0], v[1] * c - v[2] * s, v[1] * s + v[2] * c]) + pivot
    return t

def _search(obs, weights, yaws, mask, depths, template, leans=LEAN_VALUES):
    best = None
    lean_list = list(leans)
    for depth in depths:
        inv = 1.0 / max(1e-3, depth)
        for lean in lean_list:
            tpl = _leaned(template, lean)
            fx, fy = _projectors(yaws, inv, tpl)
            scale, u0, v0, resid = _batch_solve(fx, fy, obs, weights)
            valid = np.where(mask, resid, np.inf)
            if not np.isfinite(valid).any():
                continue
            i = int(np.argmin(valid))
            if best is None or resid[i] < best[0]:
                best = (float(resid[i]), float(yaws[i]), float(depth),
                        float(scale[i]), float(u0[i]), float(v0[i]),
                        float(lean))
    return best

def fit(kps: np.ndarray, conf_threshold: float = 0.25, face_visible=None,
        personalise_shape: bool = True, focal_px: float = 960.0,
        body_height_m: float = BODY_HEIGHT_M, depths=None,
        box_h_px: float = None):
    kps = np.asarray(kps, dtype=np.float64)
    if box_h_px is not None and box_h_px < 60.0:
        conf_threshold = max(0.10,
                             conf_threshold * max(float(box_h_px), 20.0)
                             / 60.0)
    obs = kps[:N_KP, :2]
    conf = kps[:N_KP, 2]
    base_w = np.where(conf >= conf_threshold, 1.0, 0.0)
    body_w = base_w.copy()
    for i in FACE_JOINTS:
        body_w[i] = 0.0

    face_conf = float(np.mean(conf[list(FACE_JOINTS)]))
    out = {"yaw": 0.0, "yaw_body": 0.0, "residual": float("inf"),
           "face_conf": face_conf,
           "ambiguity": 0.0, "scale": 0.0, "u0": 0.0, "v0": 0.0,
           "depth": 0.0, "filled": obs.copy(), "branch": "none",
           "stage": 0, "n_active": 0.0}

    torso_ok = (base_w[5] + base_w[6] >= 1) and (base_w[11] + base_w[12] >= 1)
    if base_w.sum() < 3 or not torso_ok:
        return out

    if face_visible is None:
        body_conf = float(np.mean(conf[[5, 6, 7, 8, 9, 10, 11, 12, 13,
                                        14, 15, 16]]))
        if body_conf > 1e-6:
            ratio = face_conf / body_conf
        else:
            ratio = 1.0
        face_visible = bool(face_conf >= 0.15 and ratio > 0.6)

    good = base_w > 0
    if good.any():
        h_px = float(obs[good][:, 1].max() - obs[good][:, 1].min())
    else:
        h_px = 100.0
    h_px = max(4.0, h_px)
    d0 = focal_px * body_height_m / h_px
    if depths is None:
        depths = [d0 * f for f in _DEPTH_FACTORS]

    weights = base_w if face_visible else body_w
    branch = "front" if face_visible else "back"
    n_active = float(np.sum(weights > 0))
    h_px_for_shape = float(obs[weights > 0][:, 1].max()
                            - obs[weights > 0][:, 1].min()) \
        if (weights > 0).any() else 0.0

    # Halve the coarse resolution for small targets: the fit's angular
    # error is dominated by keypoint noise, not by the search step, so
    # 12 coarse steps (~30°) is as good as 24 (~15°) for a 20 px target
    # — and halves the search cost.
    _box_h = box_h_px if box_h_px else h_px_for_shape
    coarse_steps = _COARSE_STEPS if _box_h >= 40.0 \
        else max(8, _COARSE_STEPS // 2)
    coarse = np.linspace(0.0, 360.0, coarse_steps, endpoint=False)
    if face_visible:
        mask = np.ones_like(coarse, dtype=bool)
    else:
        mask = np.array([math.cos(math.radians(y)) <= 0.4 for y in coarse])
        if not mask.any():
            mask = np.ones_like(coarse, dtype=bool)

    best = _search(obs, weights, coarse, mask, depths, CANON_N)
    if best is None:
        return out
    out["stage"] = 1

    # Fine refinement is only meaningful when the target's keypoint
    # jitter is small compared to the fine step (~2.3°). A 20 px target
    # has ±10–20° of angular noise per keypoint — the coarse pass is
    # already as accurate as the fit can be, and the fine pass costs
    # another ~5 ms with no accuracy benefit.
    fine = None
    fmask = None
    if _box_h >= 40.0:
        fine = np.linspace(best[1] - _FINE_SPAN, best[1] + _FINE_SPAN,
                           _FINE_STEPS)
        if face_visible:
            fmask = np.ones_like(fine, dtype=bool)
        else:
            fmask = np.array([math.cos(math.radians(y)) <= 0.4
                              for y in fine])
            if not fmask.any():
                fmask = np.ones_like(fine, dtype=bool)
        refined = _search(obs, weights, fine, fmask, depths, CANON_N)
        if refined is not None and refined[0] <= best[0]:
            best = refined
            out["stage"] = 2

    template = CANON_N
    lean = best[6]
    if personalise_shape and _box_h >= 40.0:
        raw = personalise(obs, weights, CANON)
        tpl = _normalise(raw)
        if np.all(np.isfinite(tpl)):
            near = [lean + d for d in (-9.0, 0.0, 9.0)]
            second = _search(obs, weights, fine, fmask, depths, tpl, near)
            if second is not None and second[0] <= best[0] * 1.05:
                best = second
                template = tpl
                lean = second[6]
                out["stage"] = 3

    resid, yaw, depth, scale, u0, v0, lean = best[0], best[1], best[2], \
        best[3], best[4], best[5], best[6]
    yaw = float(yaw) % 360.0
    inv = 1.0 / max(1e-3, depth)
    tpl = _leaned(template, lean)
    fx, fy = _projectors(np.array([yaw]), inv, tpl)
    filled = np.stack([u0 + scale * fx[0], v0 + scale * fy[0]], axis=1)
    out.update({"yaw": yaw, "yaw_body": yaw, "residual": float(resid),
                "scale": float(scale), "u0": float(u0), "v0": float(v0),
                "depth": float(depth), "filled": filled,
                "lean": float(lean), "branch": branch,
                "n_active": n_active})
    out["ambiguity"] = float(resid)
    return out

def repair(kps: np.ndarray, result: dict, conf_threshold: float = 0.25):
    kps = np.array(kps, dtype=np.float32, copy=True)
    filled = result.get("filled")
    if filled is None:
        return kps
    conf = kps[:N_KP, 2]
    for i in range(N_KP):
        if conf[i] < conf_threshold:
            kps[i, 0] = float(filled[i, 0])
            kps[i, 1] = float(filled[i, 1])
    return kps

def fold_yaw(yaw: float) -> float:
    y = abs(float(yaw)) % 360.0
    if y > 180.0:
        y = 360.0 - y
    if y > 90.0:
        y = 180.0 - y
    return y

def fit_quality(result: dict) -> float:
    scale = float(result.get("scale") or 0.0)
    if scale <= 1e-6:
        return 0.0
    resid = float(result.get("residual") or 0.0)
    n_active = max(1.0, float(result.get("n_active") or N_KP))
    rms = math.sqrt(max(0.0, resid) / (2.0 * n_active))
    rel = rms / max(1e-6, scale)
    # Soft size-aware falloff: quality 0.5 at rel = 0.12, floor at rel = 0.5.
    return float(max(0.0, min(1.0, 1.0 - rel / (0.12 + 0.3 * rel))))


# ======================================================================
# merged from hpose_features.py
# ======================================================================

import math

import numpy as np


FEATURE_VERSION = 6

ANCHOR_IDX = (5, 6, 11, 12)

def _angle_at(a, b, c) -> float:
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    c = np.asarray(c, dtype=np.float64)
    v1 = a - b
    v2 = c - b
    n1 = float(np.hypot(v1[0], v1[1]))
    n2 = float(np.hypot(v2[0], v2[1]))
    if n1 < 1e-6 or n2 < 1e-6:
        return 180.0
    cosv = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
    cosv = max(-1.0, min(1.0, cosv))
    return math.degrees(math.acos(cosv))

def fit_similarity(template: np.ndarray, observed: np.ndarray):
    template = np.asarray(template, dtype=np.float64)
    observed = np.asarray(observed, dtype=np.float64)
    n = template.shape[0]
    if n < 2:
        return 1.0, np.eye(2), np.zeros(2)
    mu_t = template.mean(axis=0)
    mu_o = observed.mean(axis=0)
    tc = template - mu_t
    oc = observed - mu_o
    var_t = float((tc * tc).sum() / n)
    if var_t < 1e-9:
        return 1.0, np.eye(2), mu_o - mu_t
    cov = (oc.T @ tc) / n
    try:
        u, s, vt = np.linalg.svd(cov)
    except np.linalg.LinAlgError:
        return 1.0, np.eye(2), mu_o - mu_t
    d = np.sign(np.linalg.det(u @ vt))
    if d == 0:
        d = 1.0
    rot = u @ np.diag([1.0, d]) @ vt
    scale = float((s * np.array([1.0, d])).sum() / var_t)
    if not math.isfinite(scale) or scale <= 1e-6:
        scale = 1.0
    trans = mu_o - scale * (rot @ mu_t)
    return scale, rot, trans

def apply_similarity(template: np.ndarray, scale: float, rot: np.ndarray,
                     trans: np.ndarray) -> np.ndarray:
    return (scale * (rot @ np.asarray(template, dtype=np.float64).T)).T + trans

def impute_missing(kps: np.ndarray, template: np.ndarray = None,
                   threshold: float = 0.2) -> np.ndarray:
    kps = np.array(kps, dtype=np.float32, copy=True)
    if kps.ndim != 2 or kps.shape[0] < N_KP:
        return kps
    conf = kps[:N_KP, 2]
    good = conf >= threshold
    if good.all() or template is None:
        return kps
    tpl = np.asarray(template, dtype=np.float32)
    if tpl.shape != (N_KP, 2):
        return kps

    idx = np.array([i for i in ANCHOR_IDX if good[i]], dtype=np.int64)
    if len(idx) < 2:
        idx = np.array([i for i in range(N_KP) if good[i]], dtype=np.int64)
    if len(idx) < 2:
        return kps
    scale, rot, trans = fit_similarity(tpl[idx], kps[:N_KP][idx][:, :2])
    filled = apply_similarity(tpl, scale, rot, trans)
    for i in range(N_KP):
        if not good[i]:
            kps[i, 0] = float(filled[i, 0])
            kps[i, 1] = float(filled[i, 1])
    return kps

class _Builder(object):

    def __init__(self):
        self.names = []
        self.values = []

    def add(self, name, value):
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = 0.0
        if not math.isfinite(v):
            v = 0.0
        self.names.append(name)
        self.values.append(v)
        return self

    def add_many(self, prefix, values):
        for i, v in enumerate(values):
            self.add("%s_%d" % (prefix, i), v)

    def vec(self) -> np.ndarray:
        return np.asarray(self.values, dtype=np.float32)

def _fill(b: _Builder, pose: Pose, fit: dict,
          conf_threshold: float) -> None:

    raw = np.asarray(pose.kps, dtype=np.float64)
    conf = raw[:N_KP, 2]
    if fit is not None:
        kps = repair(raw, fit, conf_threshold)
    else:
        kps = impute_missing(raw, None, conf_threshold)
    xy = kps[:N_KP, :2]
    d = derive_joints(kps[:N_KP])
    neck = d[17]
    pelvis = d[18]
    head = d[19]

    x1, y1, x2, y2 = pose.box
    bw = max(1e-3, x2 - x1)
    bh = max(1e-3, y2 - y1)

    scale = bh

    leg_ok = [conf[15] > conf_threshold, conf[16] > conf_threshold]
    span = 0.0
    if any(leg_ok):
        feet = [xy[15][1] if leg_ok[0] else xy[16][1],
                xy[16][1] if leg_ok[1] else xy[15][1]]
        span = abs(max(feet) - head[1])
    b.add("visible_span", span / bh)
    b.add("legs_seen", 1.0 if all(leg_ok) else 0.0)

    rel = (xy - pelvis[None, :2]) / scale
    b.add_many("rel", rel.reshape(-1))
    b.add_many("conf", conf)
    rel_d = (d[:, :2] - pelvis[None, :2]) / scale
    b.add_many("reld", rel_d[17:].reshape(-1))

    sh_w = abs(xy[6][0] - xy[5][0])
    hip_w = abs(xy[12][0] - xy[11][0])
    sh_mid = (xy[5] + xy[6]) * 0.5
    hip_mid = (xy[11] + xy[12]) * 0.5
    torso = max(1e-3, float(np.hypot(*(neck[:2] - pelvis[:2]))))

    b.add("sh_w", sh_w / scale)
    b.add("hip_w", hip_w / scale)
    b.add("sh_over_hip", sh_w / max(1e-3, hip_w))
    b.add("torso_len", torso / scale)
    b.add("head_to_neck", abs(float(head[1] - neck[1])) / scale)
    b.add("nose_off_x", float(xy[0][0] - sh_mid[0]) / max(1e-3, sh_w))
    b.add("nose_off_y", float(xy[0][1] - neck[1]) / torso)
    b.add("pelvis_off_x", float(pelvis[0] - sh_mid[0]) / max(1e-3, sh_w))
    b.add("torso_tilt",
          math.degrees(math.atan2(float(sh_mid[0] - hip_mid[0]),
                                  max(1e-3, abs(float(neck[1] - pelvis[1]))))))

    face_conf = conf[list(FACE_IDX)]
    body_conf = np.concatenate([conf[5:7], conf[11:13]])
    fc = float(np.mean(face_conf))
    b.add("face_conf_mean", fc)
    b.add("face_conf_max", float(np.max(face_conf)))
    b.add("face_conf_min", float(np.min(face_conf)))
    b.add("nose_conf", float(conf[0]))
    b.add("eye_conf", float(np.mean(conf[1:3])))
    b.add("ear_conf", float(np.mean(conf[3:5])))
    b.add("body_conf_mean", float(np.mean(body_conf)))
    b.add("face_over_body", fc / max(0.05, float(np.mean(body_conf))))
    b.add("vis_frac", float(np.mean(conf > conf_threshold)))
    b.add("vis_count", float(np.sum(conf > conf_threshold)))
    b.add("face_vis_count", float(np.sum(face_conf > conf_threshold)))

    left_idx = (5, 7, 9, 11, 13, 15)
    right_idx = (6, 8, 10, 12, 14, 16)
    lc = float(np.mean(conf[list(left_idx)]))
    rc = float(np.mean(conf[list(right_idx)]))
    b.add("limb_conf_left", lc)
    b.add("limb_conf_right", rc)
    b.add("limb_conf_balance", lc - rc)

    def pair_off(i_l, i_r):
        mid = (xy[i_l] + xy[i_r]) * 0.5
        return (abs(float(xy[i_l][0] - mid[0])),
                abs(float(xy[i_r][0] - mid[0])))

    for label, (i_l, i_r) in (("elbow", (7, 8)), ("wrist", (9, 10)),
                              ("knee", (13, 14)), ("ankle", (15, 16)),
                              ("hip", (11, 12))):
        l, r = pair_off(i_l, i_r)
        b.add("asym_%s_l" % label, l / scale)
        b.add("asym_%s_r" % label, r / scale)
        b.add("asym_%s_bal" % label, (l - r) / scale)

    b.add("ankle_sep", float(abs(xy[15][0] - xy[16][0])) / scale)
    b.add("ankle_dy", float(abs(xy[15][1] - xy[16][1])) / scale)
    b.add("knee_dy", float(abs(xy[13][1] - xy[14][1])) / scale)
    b.add("wrist_dy", float(abs(xy[9][1] - xy[10][1])) / scale)
    b.add("wrist_sep", float(abs(xy[9][0] - xy[10][0])) / scale)
    b.add("sh_hip_mid_dx", float(sh_mid[0] - hip_mid[0]) / scale)
    b.add("sh_hip_mid_dy", float(sh_mid[1] - hip_mid[1]) / scale)

    b.add("knee_angle_l", _angle_at(xy[11], xy[13], xy[15]) / 180.0)
    b.add("knee_angle_r", _angle_at(xy[12], xy[14], xy[16]) / 180.0)
    b.add("elbow_angle_l", _angle_at(xy[5], xy[7], xy[9]) / 180.0)
    b.add("elbow_angle_r", _angle_at(xy[6], xy[8], xy[10]) / 180.0)
    b.add("hip_angle_l", _angle_at(xy[5], xy[11], xy[13]) / 180.0)
    b.add("hip_angle_r", _angle_at(xy[6], xy[12], xy[14]) / 180.0)

    leg_len = 0.0
    if conf[15] > conf_threshold and conf[16] > conf_threshold:
        leg_len = (abs(float(xy[15][1] - pelvis[1]))
                   + abs(float(xy[16][1] - pelvis[1]))) * 0.5 / scale
    elif conf[13] > conf_threshold:
        leg_len = abs(float(xy[13][1] - pelvis[1])) / scale
    b.add("leg_len", leg_len)
    b.add("hip_height", float(pelvis[1] - y1) / bh)
    b.add("knee_height", float(xy[13][1] - y1) / bh)
    b.add("ankle_height", float(xy[15][1] - y1) / bh)
    b.add("head_height", float(head[1] - y1) / bh)
    b.add("box_aspect", bw / bh)
    b.add("shoulder_aspect", sh_w / bh)
    b.add("ankle_sep_over_sh",
          float(abs(xy[15][0] - xy[16][0])) / max(1e-3, sh_w))
    b.add("shoulder_pelvis_dy", float(sh_mid[1] - hip_mid[1]) / scale)
    b.add("shoulder_pelvis_dx", float(sh_mid[0] - hip_mid[0]) / scale)
    b.add("tilt_of_body",
          math.degrees(math.atan2(abs(float(hip_mid[1] - sh_mid[1])),
                                  max(1e-3, abs(float(hip_mid[0] - sh_mid[0]))))))

    spine_dx = float(neck[0] - pelvis[0])
    spine_dy = float(neck[1] - pelvis[1])
    b.add("spine_angle", math.degrees(math.atan2(abs(spine_dx),
                                                 max(1e-3, abs(spine_dy))))
          / 90.0)
    b.add("spine_angle_signed",
          math.degrees(math.atan2(spine_dx, max(1e-3, abs(spine_dy))))
          / 90.0)
    feet = []
    if conf[15] > conf_threshold:
        feet.append(xy[15])
    if conf[16] > conf_threshold:
        feet.append(xy[16])
    if feet:
        mid = np.mean(np.stack(feet), axis=0)
        axis_dx = float(head[0] - mid[0])
        axis_dy = float(head[1] - mid[1])
        b.add("body_axis_angle",
              math.degrees(math.atan2(abs(axis_dx), max(1e-3, abs(axis_dy))))
              / 90.0)
        b.add("body_axis_angle_signed",
              math.degrees(math.atan2(axis_dx, max(1e-3, abs(axis_dy))))
              / 90.0)
        b.add("body_axis_span", float(abs(axis_dy)) / scale)
        b.add("body_axis_run", float(abs(axis_dx)) / scale)
    else:
        b.add("body_axis_angle", 0.0)
        b.add("body_axis_angle_signed", 0.0)
        b.add("body_axis_span", 0.0)
        b.add("body_axis_run", 0.0)
        axis_dx = axis_dy = 0.0

    spine_axis = math.degrees(math.atan2(abs(spine_dx),
                                          max(1e-3, abs(spine_dy)))) / 90.0 \
        if (spine_dx or spine_dy) else 0.0
    axis = math.degrees(math.atan2(abs(axis_dx),
                                    max(1e-3, abs(axis_dy)))) / 90.0 \
        if (axis_dx or axis_dy) else 0.0
    b.add("spine_x_axis", spine_axis * axis)
    b.add("spine_minus_axis", spine_axis - axis)

    b.add("hip_frac", float(pelvis[1] - y1) / bh)
    b.add("shoulder_frac", float(sh_mid[1] - y1) / bh)
    b.add("neck_frac", float(neck[1] - y1) / bh)

    # --- Size and visibility features (crucial for 20-160 px targets) ----
    b.add("log_box_h", math.log(max(1.0, bh) / 100.0))
    b.add("log_box_w", math.log(max(1.0, bw) / 100.0))
    for i in range(N_KP):
        b.add("vis_%d" % i, 1.0 if conf[i] > conf_threshold else 0.0)
    vis_conf = np.where(conf > conf_threshold, conf, 0.0)
    if vis_conf.sum() > 1e-6:
        cx = float((xy[:, 0] * vis_conf).sum() / vis_conf.sum())
        cy = float((xy[:, 1] * vis_conf).sum() / vis_conf.sum())
    else:
        cx = cy = 0.0
    b.add("conf_centroid_dx", (cx - (sh_mid[0] + hip_mid[0]) * 0.5) / scale)
    b.add("conf_centroid_dy", (cy - (sh_mid[1] + hip_mid[1]) * 0.5) / scale)

    if fit is not None:
        yaw = float(fit.get("yaw", 0.0))
        b.add("fit_yaw_sin", math.sin(math.radians(yaw)))
        b.add("fit_yaw_cos", math.cos(math.radians(yaw)))
        b.add("fit_yaw_fold", fold_yaw(yaw) / 90.0)
        b.add("fit_yaw_folded", math.cos(math.radians(2.0 * yaw)))
        b.add("fit_quality", fit_quality(fit))
        b.add("fit_front_branch",
              1.0 if str(fit.get("branch", "")) == "front" else 0.0)
        b.add("fit_depth_rel", float(fit.get("depth", 0.0)) /
              max(1e-3, float(fit.get("scale", 1.0))))
        b.add("fit_scale_rel", float(fit.get("scale", 0.0)) / scale)
    else:
        for name in ("fit_yaw_sin", "fit_yaw_cos", "fit_yaw_fold",
                     "fit_yaw_folded", "fit_quality", "fit_front_branch",
                     "fit_depth_rel", "fit_scale_rel"):
            b.add(name, 0.0)

def extract(pose: Pose, fit: dict = None,
            conf_threshold: float = 0.2) -> np.ndarray:
    b = _Builder()
    _fill(b, pose, fit, conf_threshold)
    return b.vec()

_NAMES = None

def feature_names() -> list:

    global _NAMES
    if _NAMES is None:
        b = _Builder()
        # Dummy pose with some non-zero confidences to match real code paths
        kps = np.zeros((N_KP, 3), dtype=np.float32)
        # Set some confidences to trigger the same code paths as real poses
        kps[5, 2] = 0.8   # left shoulder
        kps[6, 2] = 0.8   # right shoulder
        kps[11, 2] = 0.8  # left hip
        kps[12, 2] = 0.8  # right hip
        kps[13, 2] = 0.7  # left knee
        kps[14, 2] = 0.7  # right knee
        kps[15, 2] = 0.6  # left ankle
        kps[16, 2] = 0.6  # right ankle
        dummy = Pose(kps, (0.0, 0.0, 10.0, 10.0), 1.0)
        _fill(b, dummy, None, 0.2)
        _NAMES = b.names
    return list(_NAMES)


# ======================================================================
# merged from hpose_stance.py
# ======================================================================

import numpy as np

STANCE_CLASSES = ("standing", "crouch", "prone", "jump", "walk")

class StanceResult(object):

    __slots__ = ("name", "confidence", "probs", "secondary", "crouched",
                 "prone", "airborne")

    def __init__(self, name="standing", confidence=0.0, probs=None):
        self.name = name
        self.confidence = float(confidence)
        self.probs = dict(probs or {})
        self.secondary = ""
        self.crouched = name == "crouch"
        self.prone = name == "prone"
        self.airborne = name == "jump"

    def as_dict(self) -> dict:
        return {"stance": self.name, "confidence": round(self.confidence, 4),
                "crouched": self.crouched, "prone": self.prone,
                "airborne": self.airborne,
                "probs": {k: round(v, 4) for k, v in self.probs.items()}}

    def __repr__(self) -> str:
        return "StanceResult(%s, %.2f)" % (self.name, self.confidence)

class StanceClassifier(object):

    def __init__(self, bundle):
        self.bundle = bundle
        self.head = getattr(bundle, "stance_head", None)
        self.classes = list(getattr(bundle, "stance_classes", None)
                            or STANCE_CLASSES)

    @property
    def ready(self) -> bool:
        return self.head is not None

    def probs(self, feats: np.ndarray) -> np.ndarray:
        if self.head is None:
            n = len(STANCE_CLASSES)
            return np.full(n, 1.0 / n)
        p = np.asarray(self.head.proba(feats)[0], dtype=np.float64)
        classes = self.classes or list(STANCE_CLASSES)
        if list(classes) != list(STANCE_CLASSES):
            out = np.zeros(len(STANCE_CLASSES))
            for i, name in enumerate(classes):
                if name in STANCE_CLASSES:
                    out[STANCE_CLASSES.index(name)] = p[i]
            p = out / max(1e-9, out.sum())
        return p

    def predict(self, feats: np.ndarray) -> StanceResult:
        p = self.probs(feats)
        p = self._apply_knee_rule(p, feats)
        idx = int(np.argmax(p))
        name = STANCE_CLASSES[idx]
        order = np.argsort(p)[::-1]
        res = StanceResult(name, float(p[idx]),
                           {STANCE_CLASSES[i]: float(p[i])
                            for i in range(len(p))})
        res.secondary = STANCE_CLASSES[int(order[1])] if len(order) > 1 else ""
        return res

    @staticmethod
    def _apply_knee_rule(p: np.ndarray, feats: np.ndarray) -> np.ndarray:
        names = feature_names()
        name_idx = {name: i for i, name in enumerate(names)}
        feats = np.asarray(feats, dtype=np.float64).reshape(-1)

        def value(name):
            try:
                return float(feats[name_idx[name]])
            except (ValueError, IndexError, KeyError):
                return 0.0

        # Require both knees to be visible with decent confidence. At 20 px
        # the leg joints are often below 0.2 confidence, and _angle_at
        # returns 180 on degenerate vectors, which we would otherwise read
        # as "perfectly straight legs".
        knee_conf = 0.5 * (value("vis_13") + value("vis_14"))
        knee = 0.5 * (value("knee_angle_l") + value("knee_angle_r"))
        spine = value("spine_angle")
        out = np.array(p, dtype=np.float64, copy=True)
        idx = {name: i for i, name in enumerate(STANCE_CLASSES)}

        if spine > 0.42:
            out[idx["standing"]] *= 0.02
            out[idx["walk"]] *= 0.02
            out[idx["jump"]] *= 0.25
        elif knee_conf < 0.5:
            # No reliable knee signal: fall back to the 
            return p
        elif knee > 0.86:
            out[idx["crouch"]] *= 0.02
            out[idx["prone"]] *= 0.02
            out[idx["jump"]] *= 0.02
            stride = value("ankle_dy") if value("legs_seen") > 0.5 else 0.0
            if stride <= 0.09:
                out[idx["walk"]] *= 0.15
                out[idx["standing"]] *= 3.0
        elif knee < 0.72:
            out[idx["standing"]] *= 0.15
        total = out.sum()
        return out / total if total > 1e-9 else out


# ======================================================================
# merged from hpose_view.py
# ======================================================================

import math

import numpy as np


class ViewResult(object):

    __slots__ = ("name", "confidence", "probs", "yaw", "facing_camera",
                 "back_turned", "sideways", "secondary")

    def __init__(self, name="front", confidence=0.0, probs=None, yaw=0.0):
        self.name = name
        self.confidence = float(confidence)
        self.probs = dict(probs or {})
        self.yaw = float(yaw)
        self.facing_camera = name in ("front", "front_left_45",
                                       "front_right_45")
        self.back_turned = name in ("back", "back_left_45", "back_right_45")
        self.sideways = name in ("side_left", "side_right")
        self.secondary = ""

    def as_dict(self) -> dict:
        return {"view": self.name, "confidence": round(self.confidence, 4),
                "yaw": round(self.yaw, 1),
                "facing_camera": self.facing_camera,
                "back_turned": self.back_turned,
                "sideways": self.sideways,
                "probs": {k: round(v, 4) for k, v in self.probs.items()}}

    def __repr__(self) -> str:
        return "ViewResult(%s, %.2f, yaw %.0f)" % (self.name,
                                                   self.confidence, self.yaw)

def _circular_delta(a: float, b: float) -> float:
    d = (float(a) - float(b) + 180.0) % 360.0 - 180.0
    return d

def geometric_prior(yaw: float, sigma: float = 32.0) -> np.ndarray:
    out = np.zeros(len(VIEW_CLASSES), dtype=np.float64)
    for i, name in enumerate(VIEW_CLASSES):
        d = _circular_delta(yaw, VIEW_CLASS_YAW[name])
        out[i] = math.exp(-0.5 * (d / sigma) ** 2)
    total = out.sum()
    if total <= 1e-9:
        out[:] = 1.0 / len(VIEW_CLASSES)
    else:
        out /= total
    return out

class ViewClassifier(object):

    def __init__(self, bundle, geometric_weight: float = None,
                 sigma: float = 32.0, back_weight: float = None):
        self.bundle = bundle
        self.head = getattr(bundle, "view_head", None)
        self.classes = list(getattr(bundle, "view_classes", None)
                            or VIEW_CLASSES)
        meta = getattr(bundle, "meta", None) or {}
        if geometric_weight is None:
            geometric_weight = meta.get("geometric_weight", 0.2)
        if back_weight is None:
            back_weight = meta.get("back_weight",
                                   float(geometric_weight) * 0.4)
        self.geometric_weight = float(geometric_weight)
        self.back_weight = float(back_weight)
        self.sigma = float(sigma)

    @property
    def ready(self) -> bool:
        return self.head is not None

    def probs(self, feats: np.ndarray, fit: dict = None) -> np.ndarray:
        if self.head is None:
            prior = geometric_prior(float((fit or {}).get("yaw", 0.0)),
                                    self.sigma)
            return prior
        p = np.asarray(self.head.proba(feats)[0], dtype=np.float64)
        classes = self.classes or list(VIEW_CLASSES)
        if list(classes) != list(VIEW_CLASSES):
            out = np.zeros(len(VIEW_CLASSES))
            for i, name in enumerate(classes):
                if name in VIEW_CLASSES:
                    out[VIEW_CLASSES.index(name)] = p[i]
            p = out / max(1e-9, out.sum())
        if fit is not None and self.geometric_weight > 0.0:
            g = geometric_prior(float(fit.get("yaw", 0.0)), self.sigma)
            if str(fit.get("branch", "")) == "back":
                g = _symmetrise(g)
                weight = self.back_weight
            else:
                weight = self.geometric_weight
            p = p * np.power(np.maximum(g, 1e-9), weight)
            p = p / max(1e-9, p.sum())
        return p

    def predict(self, feats: np.ndarray, fit: dict = None,
                face_visible=None) -> ViewResult:
        p = self.probs(feats, fit)
        if face_visible is None and fit is not None:
            face_visible = (str(fit.get("branch", "")) == "front")
        if face_visible is not None:
            p = self._apply_face_rule(p, bool(face_visible))
        idx = int(np.argmax(p))
        name = VIEW_CLASSES[idx]
        probs = {VIEW_CLASSES[i]: float(p[i]) for i in range(len(p))}
        order = np.argsort(p)[::-1]
        second = VIEW_CLASSES[int(order[1])] if len(order) > 1 else ""
        yaw = circular_mean_yaw(p)
        res = ViewResult(name, float(p[idx]), probs, yaw)
        res.secondary = second
        return res

    @staticmethod
    def _apply_face_rule(p: np.ndarray, face_visible: bool) -> np.ndarray:
        p = np.array(p, dtype=np.float64, copy=True)
        for i, name in enumerate(VIEW_CLASSES):
            if name == "back":
                p[i] *= 1e-4 if face_visible else 1.0
            elif name == "front":
                p[i] *= 1e-4 if not face_visible else 1.0
            elif name.startswith("back"):
                p[i] *= 0.15 if face_visible else 1.0
            elif name.startswith("front"):
                p[i] *= 1.0 if face_visible else 0.5
        total = p.sum()
        return p / total if total > 1e-9 else p

MIRROR = {
    "front": "front",
    "front_left_45": "front_right_45",
    "front_right_45": "front_left_45",
    "side_left": "side_right",
    "side_right": "side_left",
    "back_left_45": "back_right_45",
    "back_right_45": "back_left_45",
    "back": "back",
}

def _symmetrise(p: np.ndarray) -> np.ndarray:
    out = np.array(p, dtype=np.float64, copy=True)
    for i, name in enumerate(VIEW_CLASSES):
        other = MIRROR.get(name)
        if other and other in VIEW_CLASSES:
            j = VIEW_CLASSES.index(other)
            avg = 0.5 * (out[i] + out[j])
            out[i] = avg
            out[j] = avg
    total = out.sum()
    return out / total if total > 1e-9 else out

def circular_mean_yaw(p: np.ndarray) -> float:
    s = 0.0
    c = 0.0
    for i, name in enumerate(VIEW_CLASSES):
        w = float(p[i]) if i < len(p) else 0.0
        a = math.radians(VIEW_CLASS_YAW[name])
        s += w * math.sin(a)
        c += w * math.cos(a)
    if abs(s) < 1e-9 and abs(c) < 1e-9:
        return 0.0
    return math.degrees(math.atan2(s, c)) % 360.0


# ======================================================================
# merged from hpose_classifier.py
# ======================================================================

import numpy as np

class LinearHead(object):

    kind = "linear"

    def __init__(self, w, b, mean=None, std=None, classes=None,
                 feature_version=None):
        self.w = np.asarray(w, dtype=np.float32)
        self.b = np.asarray(b, dtype=np.float32)
        dim = self.w.shape[0]
        self.mean = (np.zeros(dim, dtype=np.float32)
                     if mean is None else np.asarray(mean, dtype=np.float32))
        self.std = (np.ones(dim, dtype=np.float32)
                    if std is None else np.asarray(std, dtype=np.float32))
        if self.mean.shape[0] != dim or self.std.shape[0] != dim:
            raise ValueError("head dim %d != mean/std dim %s"
                             % (dim, self.mean.shape))
        self.classes = list(classes or [])
        self.feature_version = feature_version

    def _check(self, x: np.ndarray) -> None:
        if x.shape[-1] != self.w.shape[0]:
            raise ValueError(
                "feature dim %d != head dim %d (feature_version %r)"
                % (x.shape[-1], self.w.shape[0], self.feature_version))

    def _z(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if x.ndim == 1:
            x = x[None, :]
        self._check(x)
        x = (x - self.mean) / np.maximum(self.std, 1e-6)
        return x @ self.w + self.b

    def proba(self, x: np.ndarray) -> np.ndarray:
        z = self._z(x)
        z = z - z.max(axis=1, keepdims=True)
        e = np.exp(z)
        return e / np.maximum(e.sum(axis=1, keepdims=True), 1e-12)

    def predict(self, x: np.ndarray) -> np.ndarray:
        return self.proba(x).argmax(axis=1)

    def to_arrays(self):
        return {"head_w": self.w, "head_b": self.b,
                "feat_mean": self.mean, "feat_std": self.std}

class MLPHead(object):

    kind = "mlp"

    def __init__(self, w1, b1, w2, b2, mean=None, std=None, classes=None,
                 feature_version=None):
        self.w1 = np.asarray(w1, dtype=np.float32)
        self.b1 = np.asarray(b1, dtype=np.float32)
        self.w2 = np.asarray(w2, dtype=np.float32)
        self.b2 = np.asarray(b2, dtype=np.float32)
        dim = self.w1.shape[0]
        self.mean = (np.zeros(dim, dtype=np.float32)
                     if mean is None else np.asarray(mean, dtype=np.float32))
        self.std = (np.ones(dim, dtype=np.float32)
                    if std is None else np.asarray(std, dtype=np.float32))
        if self.mean.shape[0] != dim or self.std.shape[0] != dim:
            raise ValueError("head dim %d != mean/std dim %s"
                             % (dim, self.mean.shape))
        self.classes = list(classes or [])
        self.feature_version = feature_version

    def _check(self, x: np.ndarray) -> None:
        if x.shape[-1] != self.w1.shape[0]:
            raise ValueError(
                "feature dim %d != head dim %d (feature_version %r)"
                % (x.shape[-1], self.w1.shape[0], self.feature_version))

    def _h(self, x):
        x = np.asarray(x, dtype=np.float32)
        if x.ndim == 1:
            x = x[None, :]
        self._check(x)
        x = (x - self.mean) / np.maximum(self.std, 1e-6)
        return np.maximum(x @ self.w1 + self.b1, 0.0)

    def proba(self, x: np.ndarray) -> np.ndarray:
        h = self._h(x)
        z = h @ self.w2 + self.b2
        z = z - z.max(axis=1, keepdims=True)
        e = np.exp(z)
        return e / np.maximum(e.sum(axis=1, keepdims=True), 1e-12)

    def predict(self, x: np.ndarray) -> np.ndarray:
        return self.proba(x).argmax(axis=1)

    def to_arrays(self):
        return {"head_w1": self.w1, "head_b1": self.b1,
                "head_w2": self.w2, "head_b2": self.b2,
                "feat_mean": self.mean, "feat_std": self.std}

def head_from_arrays(arrays: dict, classes=None, feature_version=None):
    if "head_w" in arrays:
        return LinearHead(arrays["head_w"], arrays["head_b"],
                          arrays.get("feat_mean"), arrays.get("feat_std"),
                          classes, feature_version)
    return MLPHead(arrays["head_w1"], arrays["head_b1"],
                   arrays["head_w2"], arrays["head_b2"],
                   arrays.get("feat_mean"), arrays.get("feat_std"),
                   classes, feature_version)


# ======================================================================
# merged from hpose_detector.py
# ======================================================================

import os
import sys
import threading
import urllib.request

import numpy as np

import utils

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
    """Model directory. Downloaded models stay inside the app folder."""
    env = os.environ.get("HPOSE_CACHE")
    if env:
        return env
    path = utils.models_dir()
    if not _writable(path):
        path = utils.cache_dir("models")
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


def _export_small_pose(target_path: str) -> bool:
    """Export yolo11n-pose.pt to a 320x320 ONNX, once.

    The .pt file auto-downloads from Ultralytics' model hub on first use.
    Returns True on success.
    """
    try:
        from ultralytics import YOLO
    except ImportError:
        sys.stderr.write(
            "hpose: 'ultralytics' is not installed; the 320 ONNX "
            "variant cannot be created. Install with "
            "'pip install ultralytics' to enable the 3x ROI speedup.\n")
        return False
    try:
        base = "yolo11n-pose.pt"  # Ultralytics auto-downloads from hub
        m = YOLO(base)
        exported = m.export(format="onnx", imgsz=320, simplify=True,
                             dynamic=False)
        os.replace(exported, target_path)
        sys.stderr.write("hpose: exported %s (%d bytes)\n"
                         % (target_path, os.path.getsize(target_path)))
        return True
    except Exception as exc:
        sys.stderr.write("hpose: 320-model export failed (%r)\n" % (exc,))
        return False


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
                 device: str = "auto", download: bool = True):
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

        # Provider selection: env override > first available GPU > CPU.
        # The env var lets CI and bench scripts force CPU for reproducibility.
        device = (device or "auto").strip().lower()
        want = os.environ.get("HPOSE_ORT_DEVICE", device)

        available = ort.get_available_providers()
        providers = []
        provider_options = []
        if want in ("auto", "gpu"):
            for ep in ("TensorrtExecutionProvider",
                       "CUDAExecutionProvider",
                       "DmlExecutionProvider"):
                if ep in available:
                    providers.append(ep)
                    break
        elif want == "cuda":
            if "CUDAExecutionProvider" in available:
                providers.append("CUDAExecutionProvider")
        elif want == "dml":
            if "DmlExecutionProvider" in available:
                providers.append("DmlExecutionProvider")
        elif want == "trt":
            if "TensorrtExecutionProvider" in available:
                providers.append("TensorrtExecutionProvider")
        providers.append("CPUExecutionProvider")

        # Provider-specific session tuning.
        for ep in providers:
            if ep == "CUDAExecutionProvider":
                provider_options.append({
                    "device_id": 0,
                    "arena_extend_strategy": "kSameAsRequested",
                    "cudnn_conv_algo_search": "EXHAUSTIVE",
                    "do_copy_in_default_stream": True,
                })
            elif ep == "TensorrtExecutionProvider":
                provider_options.append({
                    "device_id": 0,
                    "trt_fp16_enable": "1",
                    "trt_engine_cache_enable": "1",
                    "trt_engine_cache_path": utils.cache_dir("trt"),
                    "trt_timing_cache_enable": "1",
                })
            elif ep == "DmlExecutionProvider":
                provider_options.append({"device_id": 0})
            else:
                provider_options.append({})

        try:
            self.session = ort.InferenceSession(
                self.path, opts,
                providers=list(zip(providers, provider_options)))
        except Exception as exc:
            if "TensorrtExecutionProvider" in providers:
                sys.stderr.write(
                    "hpose: TensorRT failed (%r); falling back "
                    "to CUDA/CPU\n" % (exc,))
                keep = [p != "TensorrtExecutionProvider" for p in providers]
                providers = [p for p, k in zip(providers, keep) if k]
                provider_options = [po for po, k in zip(provider_options, keep) if k]
                self.session = ort.InferenceSession(
                    self.path, opts,
                    providers=list(zip(providers, provider_options)))
            else:
                raise

        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        self._in_h, self._in_w = self._input_shape()
        self.active_provider = self.session.get_providers()[0]
        self.device = str(self.active_provider).lower()

        # First inference on GPU pays CUDA-context init + kernel
        # compilation (2-5 s). Do that once, here, so the first real
        # click is fast. _load_in_background already runs in a daemon
        # thread so this doesn't block startup.
        try:
            warm = np.zeros((1, 3, self._in_h, self._in_w),
                            dtype=np.float32)
            self.session.run([self.output_name],
                             {self.input_name: warm})
            self.session.run([self.output_name],
                             {self.input_name: warm})
        except Exception as exc:
            sys.stderr.write("hpose: warmup failed (%r)\n" % (exc,))

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
        if out.ndim != 2:
            return []
        expected = 5 + N_KP * 3
        # Accept either layout: (N, expected) or (expected, N).
        # Also handle 57-column outputs (with objectness column at index 4).
        if out.shape[1] == expected:
            pass
        elif out.shape[0] == expected:
            out = out.T
        elif out.shape[1] == expected + 1:  # objectness variant
            out = np.delete(out, 4, axis=1)  # drop objectness
        elif out.shape[0] == expected + 1:
            out = np.delete(out, 4, axis=0).T
        else:
            return []
        if out.shape[1] != expected:
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
        self._lock = threading.Lock()
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
        with self._lock:
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


# ======================================================================
# merged from hpose_bundle.py
# ======================================================================

import json
import os
import time

import numpy as np

import utils

try:
    from system import log
except Exception:
    def log(_text):
        pass

BUNDLE_VERSION = 3
DEFAULT_BUNDLE_NAME = "hpose_bundle.npz"

class Bundle(object):

    def __init__(self, template=None, view_head=None, stance_head=None,
                 view_classes=None, stance_classes=None, meta=None,
                 feature_version=None):
        self.template = (np.zeros((17, 2), dtype=np.float32)
                         if template is None
                         else np.asarray(template, dtype=np.float32))
        self.view_head = view_head
        self.stance_head = stance_head
        self.view_classes = list(view_classes or [])
        self.stance_classes = list(stance_classes or [])
        self.meta = dict(meta or {})
        self.feature_version = feature_version

    def save(self, path: str) -> str:
        arrays = {"template": self.template}
        if self.view_head is not None:
            for k, v in self.view_head.to_arrays().items():
                arrays["view_" + k] = v
        if self.stance_head is not None:
            for k, v in self.stance_head.to_arrays().items():
                arrays["stance_" + k] = v
        arrays["meta_json"] = np.asarray(json.dumps({
            "version": BUNDLE_VERSION,
            "feature_version": FEATURE_VERSION,
            "view_classes": self.view_classes,
            "stance_classes": self.stance_classes,
            "meta": self.meta,
            "saved_at": time.time(),
        }))
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".",
                    exist_ok=True)
        np.savez_compressed(path, **arrays)
        return path

    @classmethod
    def load(cls, path: str) -> "Bundle":
        with np.load(path, allow_pickle=False) as z:
            arrays = {k: z[k] for k in z.files}
        meta_blob = json.loads(arrays.pop("meta_json").item())
        fv = meta_blob.get("feature_version")
        template = arrays.pop("template", None)
        view_arrays = {k[5:]: v for k, v in arrays.items()
                       if k.startswith("view_")}
        stance_arrays = {k[7:]: v for k, v in arrays.items()
                         if k.startswith("stance_")}
        view_head = (head_from_arrays(
            view_arrays, meta_blob.get("view_classes"), fv)
            if view_arrays else None)
        stance_head = (head_from_arrays(
            stance_arrays, meta_blob.get("stance_classes"), fv)
            if stance_arrays else None)
        return cls(template=template, view_head=view_head,
                   stance_head=stance_head,
                   view_classes=meta_blob.get("view_classes") or [],
                   stance_classes=meta_blob.get("stance_classes") or [],
                   meta=meta_blob.get("meta") or {},
                   feature_version=fv)

    def has_view(self) -> bool:
        return self.view_head is not None

    def has_stance(self) -> bool:
        return self.stance_head is not None

    def describe(self) -> str:
        lines = ["hpose bundle v%d" % (BUNDLE_VERSION,)]
        if self.meta.get("trained_at"):
            lines.append("  trained: %s" % (self.meta["trained_at"],))
        if self.meta.get("view_acc") is not None:
            lines.append("  view accuracy:  %.3f" % (self.meta["view_acc"],))
        if self.meta.get("stance_acc") is not None:
            lines.append("  stance accuracy: %.3f" % (self.meta["stance_acc"],))
        lines.append("  view classes:  %s" % (", ".join(self.view_classes),))
        lines.append("  stance classes: %s" % (", ".join(self.stance_classes),))
        return "\n".join(lines)

def default_bundle_path() -> str:
    env = os.environ.get("HPOSE_BUNDLE")
    if env:
        return env
    return os.path.join(utils.models_dir(), DEFAULT_BUNDLE_NAME)

def load_bundle(path: str = None) -> Bundle:
    path = path or default_bundle_path()
    if not os.path.isfile(path):
        return Bundle()
    try:
        bundle = Bundle.load(path)
    except (OSError, ValueError, EOFError) as exc:
        log("hpose: bundle %s corrupt (%r)" % (path, exc))
        return Bundle()
    fv = bundle.feature_version
    if fv is not None and fv != FEATURE_VERSION:
        log("hpose: bundle %s trained on features v%s, runtime v%s - "
            "retrain the bundle" % (path, fv, FEATURE_VERSION))
    return bundle


# ======================================================================
# merged from hpose_analyzer.py
# ======================================================================

# log already imported at top of module (guarded fallback at 2627)

class Analyzer(object):

    def __init__(self, bundle_path: str = None, pose_model: str = None,
                 face_model: str = None, conf: float = 0.25,
                 iou: float = 0.45, use_face: bool = True,
                 download: bool = True, face_region: float = 0.26,
                 min_face_person_px: float = 100.0):
        self.bundle = load_bundle(bundle_path)
        self.pose_detector = PoseDetector(model=pose_model, conf=conf,
                                          iou=iou, download=download)
        self.face_detector = None
        if use_face:
            try:
                self.face_detector = FaceDetector(model=face_model,
                                                  download=download)
            except Exception:
                self.face_detector = None
        self.view_clf = ViewClassifier(self.bundle)
        self.stance_clf = StanceClassifier(self.bundle)
        self.face_region = float(face_region)
        self.min_face_person_px = float(min_face_person_px)
        self._view_warned = False
        self._stance_warned = False

    def _face_boxes(self, img):
        if self.face_detector is None:
            return []
        try:
            return self.face_detector.detect(img)
        except Exception:
            return []

    @staticmethod
    def _head_region(box, frac: float):
        x1, y1, x2, y2 = box
        w = x2 - x1
        h = y2 - y1
        return (x1 - w * 0.12, y1 - h * 0.04, x2 + w * 0.12,
                y1 + h * frac)

    def _head_crop(self, img, pose):
        import cv2
        h_img, w_img = img.shape[:2]
        kps = pose.kps
        pts = [(float(kps[i][0]), float(kps[i][1]))
               for i in (0, 1, 2, 3, 4) if kps[i][2] >= 0.25]
        if pts:
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            shoulder = abs(float(kps[6][0]) - float(kps[5][0]))
            radius = max(shoulder * 0.7, 10.0)
        else:
            x1, y1, x2, y2 = pose.box
            radius = max((x2 - x1) * 0.20, (y2 - y1) * 0.12, 10.0)
            cx = (x1 + x2) * 0.5
            cy = y1 + radius * 0.7
        radius = min(radius, 0.45 * min(h_img, w_img))
        x1 = int(max(0, cx - radius))
        y1 = int(max(0, cy - radius))
        x2 = int(min(w_img, cx + radius))
        y2 = int(min(h_img, cy + radius))
        if x2 - x1 < 8 or y2 - y1 < 8:
            return None
        crop = img[y1:y2, x1:x2]
        size = 256
        if max(crop.shape[:2]) == size:
            return crop
        scale = size / float(max(crop.shape[0], crop.shape[1]))
        return cv2.resize(crop, (max(8, int(crop.shape[1] * scale)),
                                  max(8, int(crop.shape[0] * scale))),
                           interpolation=cv2.INTER_CUBIC)

    def _has_face(self, faces, box) -> bool:
        if not faces:
            return False
        rx1, ry1, rx2, ry2 = self._head_region(box, self.face_region)
        for f in faces:
            fx = (f[0] + f[2]) * 0.5
            fy = (f[1] + f[3]) * 0.5
            if rx1 <= fx <= rx2 and ry1 <= fy <= ry2:
                return True
        return False

    def _face_at(self, img, pose, faces) -> bool:
        if self._has_face(faces, pose.box):
            return True
        if self.face_detector is None:
            return False
        h = pose.box[3] - pose.box[1]
        if h < self.min_face_person_px:
            # FaceDetectorYN's minimum reliable face width is ~20 px,
            # which requires a >= 100 px person. Below that, crops are
            # wasted compute.
            return False
        crop = self._head_crop(img, pose)
        if crop is None:
            return False
        try:
            found = self.face_detector.detect(crop)
        except Exception:
            return False
        return bool(found)

    def explain(self, pose: Pose, focal_px: float, faces=None,
                img=None) -> dict:
        has_face = False
        if img is not None:
            has_face = self._face_at(img, pose, faces or [])
        else:
            has_face = self._has_face(faces or [], pose.box)
        # A face detector returning nothing is not evidence of a back view.
        # Only pass an explicit False when we actually have image data and
        # a positive signal of "no face" (i.e. face detector ran and found
        # faces elsewhere but none here). Otherwise let posefit decide from
        # keypoint confidences.
        fit_face_visible = None
        if has_face:
            fit_face_visible = True
        elif faces:  # detector ran, found faces, none here -> real "back"
            fit_face_visible = False
        h_box = float(pose.box[3] - pose.box[1])
        fit_res = fit(pose.kps, focal_px=focal_px,
                      face_visible=fit_face_visible, box_h_px=h_box)
        feats = extract(pose, fit=fit_res)

        face_visible_for_view = has_face if has_face else None
        try:
            view = self.view_clf.predict(feats, fit_res,
                                          face_visible=face_visible_for_view)
        except ValueError as exc:
            if "feature dim" not in str(exc):
                raise
            self._view_warned = getattr(self, "_view_warned", False)
            if not self._view_warned:
                self._view_warned = True
                log("hpose: bundle cannot classify view (%r) - the shipped "
                    "bundle must be retrained on current  View "
                    "filters will not match until then." % (exc,))
            view = ViewResult(name="unknown", confidence=0.0, probs={},
                              yaw=0.0)
        try:
            stance = self.stance_clf.predict(feats)
        except ValueError as exc:
            if "feature dim" not in str(exc):
                raise
            self._stance_warned = getattr(self, "_stance_warned", False)
            if not self._stance_warned:
                self._stance_warned = True
                log("hpose: bundle cannot classify stance (%r) - the shipped "
                    "bundle must be retrained on current  Stance "
                    "filters will not match until then." % (exc,))
            stance = StanceResult(name="unknown", confidence=0.0, probs={})
        return {
            "box": pose.box,
            "score": pose.score,
            "fit": fit_res,
            "feats": feats,
            "view": view.name,
            "view_probs": view.probs,
            "stance": stance.name,
            "stance_probs": stance.probs,
            "has_face": has_face,
            "yaw": view.yaw,
        }


# ======================================================================
# merged from hpose_tracker.py
# ======================================================================

class Track(object):

    __slots__ = ("id", "box", "score", "hits", "misses", "age", "kf",
                 "motion", "view_probs", "stance_probs", "view", "stance",
                 "view_history", "stance_history", "last_t", "fit",
                 "confirmed", "has_face", "kp_conf", "raw_score", "head_kp",
                 "kps")

    def __init__(self, tid: int, box, score: float, t: float):
        self.id = int(tid)
        self.box = tuple(float(v) for v in box)
        self.score = float(score)
        self.kp_conf = 1.0
        self.raw_score = float(score)
        self.head_kp = None
        self.hits = 0
        self.misses = 0
        self.age = 0
        self.kf = KalmanBox(box, t)
        self.motion = MotionModel(body_h_px=max(1.0, box[3] - box[1]))
        cx = (box[0] + box[2]) * 0.5
        cy = (box[1] + box[3]) * 0.5
        self.motion.add(t, cx, cy)
        self.view_probs = {c: 0.0 for c in VIEW_CLASSES}
        self.stance_probs = {c: 0.0 for c in STANCE_CLASSES}
        self.view = ""
        self.stance = ""
        self.view_history = []
        self.stance_history = []
        self.last_t = float(t)
        self.fit = None
        self.confirmed = False
        self.has_face = False
        self.kps = None

    @property
    def center(self):
        return ((self.box[0] + self.box[2]) * 0.5,
                (self.box[1] + self.box[3]) * 0.5)

    @property
    def velocity(self):
        return self.motion.vel

    @property
    def velocity_raw(self):
        return self.motion.vel_raw

    @property
    def speed(self) -> float:
        return self.motion.speed

    @property
    def motion_state(self) -> str:
        return self.motion.state()

    def lead(self, seconds: float):
        if self.hits >= 3 and self.motion.samples:
            t = max(0.0, float(seconds))
            # Use Kalman-smoothed velocity instead of noisy instantaneous vel_raw
            vx, vy = self.motion.vel
            ox = vx * t
            oy = vy * t
            x = self.motion.samples[-1][1] + ox
            y = self.motion.samples[-1][2] + oy
            return x, y, ox, oy
        return self.motion.predict(seconds)

    def as_dict(self) -> dict:
        vx, vy = self.motion.vel
        return {
            "id": self.id,
            "box": [round(v, 1) for v in self.box],
            "score": round(self.score, 4),
            "center": [round(self.center[0], 1), round(self.center[1], 1)],
            "velocity": [round(vx, 1), round(vy, 1)],
            "speed": round(self.motion.speed, 1),
            "motion": self.motion_state,
            "view": self.view,
            "stance": self.stance,
            "view_confidence": round(self.view_probs.get(self.view, 0.0), 4),
            "stance_confidence": round(self.stance_probs.get(self.stance, 0.0),
                                       4),
            "hits": self.hits,
            "age": self.age,
            "confirmed": self.confirmed,
        }

def _size_iou_threshold(box_a, box_b, base_thr: float) -> float:
    """Relax the IoU threshold for small targets.

    A 20 px person moving 5 px per frame has IoU 0.6; moving 10 px, 0.33.
    At 0.30 threshold, that 20 px target loses identity after two frames.
    For 160 px targets, the original 0.30 is fine.
    """
    h = max(box_a[3] - box_a[1], box_b[3] - box_b[1])
    h = max(1.0, min(400.0, h))
    scaled = 0.05 + 0.003 * h            # 20 px -> 0.11, 160 px -> 0.53
    return min(base_thr, max(0.08, scaled))


def _greedy_match(tracks, dets, iou_threshold: float, relax: float = 1.0):
    pairs = []
    for ti, tr in enumerate(tracks):
        for di, det in enumerate(dets):
            iou = box_iou(tr.box, det["box"])
            thr = _size_iou_threshold(tr.box, det["box"],
                                      iou_threshold) * relax
            if iou >= thr:
                pairs.append((iou, ti, di))
    pairs.sort(key=lambda item: (-item[0], item[1], item[2]))
    used_t = set()
    used_d = set()
    matches = []
    for iou, ti, di in pairs:
        if ti in used_t or di in used_d:
            continue
        used_t.add(ti)
        used_d.add(di)
        matches.append((ti, di))
    return matches, used_d

class Tracker(object):

    def __init__(self, max_age: int = 20, min_hits: int = 2,
                 iou_threshold: float = 0.30, low_iou_threshold: float = 0.15,
                 low_score: float = 0.35, view_smoothing: float = 0.35,
                 stance_smoothing: float = 0.25, switch_margin: float = 0.12):
        self.max_age = int(max_age)
        self.min_hits = int(min_hits)
        self.iou_threshold = float(iou_threshold)
        self.low_iou_threshold = float(low_iou_threshold)
        self.low_score = float(low_score)
        self.view_smoothing = float(view_smoothing)
        self.stance_smoothing = float(stance_smoothing)
        self.switch_margin = float(switch_margin)
        self.tracks = []
        self._next_id = 1

    def _smooth(self, store: dict, incoming: dict, alpha: float,
                current: str, margin: float) -> str:
        if not incoming:
            return current
        total = sum(store.values())
        if total <= 1e-9:
            for key in store:
                store[key] = float(incoming.get(key, 0.0))
            best = max(store, key=lambda k: store[k])
            return best
        for key, value in store.items():
            store[key] = (1.0 - alpha) * value + alpha * float(
                incoming.get(key, 0.0))
        if not current:
            best = max(store, key=lambda k: store[k])
            return best
        best = max(store, key=lambda k: store[k])
        if best != current and store[best] > store[current] + margin:
            return best
        return current

    def update(self, detections, t: float) -> list:
        dets = list(detections or [])
        for tr in self.tracks:
            tr.kf.predict(t)

        high = [d for d in dets if d.get("score", 1.0) >= self.low_score]

        matches, used = _greedy_match(self.tracks, high, self.iou_threshold)
        matched_det = set(di for _, di in matches)

        rest = [i for i in range(len(high)) if i not in matched_det]
        if rest and self.tracks:
            unmatched = [i for i, tr in enumerate(self.tracks)
                         if i not in {ti for ti, _ in matches}]
            sub_tracks = [self.tracks[i] for i in unmatched]
            sub_dets = [high[i] for i in rest]
            sub_matches, sub_used = _greedy_match(sub_tracks, sub_dets,
                                                  self.low_iou_threshold,
                                                  relax=0.6)
            for sti, sdi in sub_matches:
                ti = unmatched[sti]
                di = rest[sdi]
                matches.append((ti, di))
                used.add(di)

        matched_tracks = set(ti for ti, _ in matches)
        for ti, tr in enumerate(self.tracks):
            if ti not in matched_tracks:
                tr.misses += 1

        for ti, di in matches:
            det = high[di]
            self._apply(self.tracks[ti], det, t)

        for di, det in enumerate(high):
            if di in used:
                continue
            track = Track(self._next_id, det["box"], det.get("score", 1.0), t)
            self._next_id += 1
            self._apply(track, det, t)
            self.tracks.append(track)

        alive = []
        for tr in self.tracks:
            if tr.misses > self.max_age:
                continue
            alive.append(tr)
        self.tracks = alive
        return self.tracks

    def _apply(self, track: Track, det: dict, t: float) -> None:
        track.hits += 1
        track.misses = 0
        track.age += 1
        track.box = tuple(float(v) for v in det["box"])
        track.score = float(det.get("score", track.score))
        if "kp_conf" in det:
            track.kp_conf = float(det["kp_conf"])
        if "raw_score" in det:
            track.raw_score = float(det["raw_score"])
        hk = det.get("head_kp")
        if hk:
            track.head_kp = hk
        if det.get("kps") is not None:
            track.kps = det["kps"]
        track.last_t = float(t)
        track.kf.update(track.box, t)
        cx = (track.box[0] + track.box[2]) * 0.5
        cy = (track.box[1] + track.box[3]) * 0.5
        track.motion.add(t, cx, cy)
        h_new = max(1.0, track.box[3] - track.box[1])
        if track.motion.body_h_px is not None:
            track.motion.body_h_px = (0.85 * track.motion.body_h_px
                                      + 0.15 * h_new)
        else:
            track.motion.body_h_px = h_new
        track.fit = det.get("fit", track.fit)
        if det.get("has_face"):
            track.has_face = True
        track.view = self._smooth(track.view_probs, det.get("view_probs"),
                                  self.view_smoothing, track.view,
                                  self.switch_margin)
        track.stance = self._smooth(track.stance_probs,
                                    det.get("stance_probs"),
                                    self.stance_smoothing, track.stance,
                                    self.switch_margin)
        override = None
        if track.stance == "standing" and track.motion_state == "fast":
            override = "walk"
        elif track.stance == "walk" and track.motion_state == "static" \
                and track.hits > 8:
            override = "standing"
        if override is not None:
            track.stance = override
            # Keep stance_probs consistent so stance_confidence is meaningful.
            track.stance_probs[override] = max(
                0.55, track.stance_probs.get(override, 0.0))
            total = sum(track.stance_probs.values())
            if total > 1e-9:
                for k in track.stance_probs:
                    track.stance_probs[k] /= total

        track.view_history.append(track.view)
        track.stance_history.append(track.stance)
        if len(track.view_history) > 60:
            del track.view_history[0]
        if len(track.stance_history) > 60:
            del track.stance_history[0]
        if not track.confirmed and track.hits >= self.min_hits:
            track.confirmed = True

    def best(self, score_fn=None):
        if not self.tracks:
            return None
        if score_fn is not None:
            return max(self.tracks, key=score_fn)
        return max(self.tracks, key=lambda tr: tr.score)

    def by_id(self, tid: int):
        for tr in self.tracks:
            if tr.id == tid:
                return tr
        return None

    def reset(self) -> None:
        self.tracks = []
        self._next_id = 1


# ======================================================================
# merged from hpose_bridge.py
# ======================================================================

import math
import os
import threading
import time

try:
    from system import log
except Exception:
    def log(_text):
        pass

DEFAULT_MODEL = "yolo11n-pose.onnx"

DETECTOR_FLOOR = 0.10

def _conf_of(cfgv) -> float:
    try:
        pct = float(cfgv.get("hpose_min_conf", 35) or 35)
    except (TypeError, ValueError):
        pct = 35.0
    return max(0.05, min(0.95, pct / 100.0))

CROP_RESCUE_SCORE = 0.05
CROP_RESCUE_KP = 0.80

ROI_RADIUS_MAX = 150.0
ROI_MARGIN = 1.5
ROI_MIN_SIDE = 160
ROI_RESCUE_SCORE = 0.15
ROI_RESCUE_KP = 0.50

MAX_EXPLAIN_PER_FRAME = int(os.environ.get("HPOSE_MAX_EXPLAIN", "3"))

# A 20 px person typically produces YOLO scores in 0.10-0.30.
# Rejecting at 0.35 guarantees zero recall for the small-target case.
# The Tracker's hit-count confirmation rejects false positives for us.
PREFILTER_SCORE_FLOOR = float(os.environ.get("HPOSE_PREFILTER_FLOOR", "0.12"))

def _prefilter(poses):
    out = []
    for pose in poses:
        score = float(pose.score)
        if score < PREFILTER_SCORE_FLOOR:
            continue
        kp = _kp_conf(pose)
        h = max(1.0, pose.box[3] - pose.box[1])
        rank = (score + 0.3 * kp) * _size_rank(h)
        out.append((pose, rank, kp, score))
    out.sort(key=lambda item: -item[1])
    return out

def _size_rank(h: float) -> float:
    """Return a multiplicative priority in [0.8, 1.4] favouring smaller targets."""
    h = max(1.0, float(h))
    return max(0.8, min(1.4, 1.0 + (100.0 - h) / 500.0))

def _contained(inner, outer):
    ix1, iy1 = max(inner[0], outer[0]), max(inner[1], outer[1])
    ix2, iy2 = min(inner[2], outer[2]), min(inner[3], outer[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    area = max(1.0, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    return iw * ih / area

def _suppress_contained(dets):
    if len(dets) < 2:
        return dets
    keep = []
    for i, d in enumerate(dets):
        dropped = False
        for j, o in enumerate(dets):
            if i == j or o[1] <= d[1]:
                continue
            if _contained(d[0].box, o[0].box) >= 0.7:
                dropped = True
                break
        if not dropped:
            keep.append(d)
    return keep

def _kp_conf(pose) -> float:
    try:
        k = pose.kps
        return float(sum(float(k[i][2]) for i in (5, 6, 11, 12)) / 4.0)
    except Exception:
        return 0.0

def _suppress_dets(dets):
    if len(dets) < 2:
        return list(dets)
    boxes = [d["box"] for d in dets]
    scores = [float(d.get("score", 0.0)) for d in dets]
    keep = []
    for i, d in enumerate(dets):
        dropped = False
        for j, o in enumerate(dets):
            if i == j or scores[j] <= scores[i]:
                continue
            if _contained(boxes[i], boxes[j]) >= 0.7:
                dropped = True
                break
        if not dropped:
            keep.append(i)
    return [dets[i] for i in keep]

class Subject(object):

    __slots__ = ("box", "view", "stance", "motion", "speed", "score",
                 "view_confidence", "stance_confidence", "lead",
                 "track_id", "facing_camera", "back_turned", "kp_conf",
                 "raw_score", "hits", "head_kp", "kps")

    def __init__(self, box, view="", stance="", motion="static",
                 speed=0.0, score=0.0, view_confidence=0.0,
                 stance_confidence=0.0, lead=(0.0, 0.0), track_id=0,
                 kp_conf=1.0, raw_score=1.0, head_kp=None):
        self.box = tuple(float(v) for v in box)
        self.view = view
        self.stance = stance
        self.motion = motion
        self.speed = float(speed)
        self.score = float(score)
        self.view_confidence = float(view_confidence)
        self.stance_confidence = float(stance_confidence)
        self.lead = (float(lead[0]), float(lead[1]))
        self.track_id = int(track_id)
        self.facing_camera = False
        self.back_turned = False
        self.kp_conf = float(kp_conf)
        self.raw_score = float(raw_score)
        self.hits = 0
        self.head_kp = head_kp
        self.kps = None

    @property
    def center(self):
        return ((self.box[0] + self.box[2]) * 0.5,
                (self.box[1] + self.box[3]) * 0.5)

    @property
    def width(self) -> float:
        return max(0.0, self.box[2] - self.box[0])

    @property
    def height(self) -> float:
        return max(0.0, self.box[3] - self.box[1])

    def as_dict(self) -> dict:
        return {"box": [round(v, 1) for v in self.box],
                "view": self.view, "stance": self.stance,
                "motion": self.motion, "speed": round(self.speed, 1),
                "score": round(self.score, 4),
                "view_confidence": round(self.view_confidence, 4),
                "stance_confidence": round(self.stance_confidence, 4),
                "lead": [round(self.lead[0], 1), round(self.lead[1], 1)],
                "kp_conf": round(self.kp_conf, 2),
                "id": self.track_id}

class Engine(object):

    def __init__(self, cfgv=None):
        cfgv = cfgv or {}
        self.cfgv = dict(cfgv)
        bundle = cfgv.get("hpose_bundle") or None
        model = str(cfgv.get("hpose_model") or DEFAULT_MODEL)
        self.analyzer = Analyzer(bundle_path=bundle, pose_model=model,
                                 conf=DETECTOR_FLOOR,
                                 use_face=bool(cfgv.get("hpose_use_face", True)),
                                 min_face_person_px=float(cfgv.get(
                                     "hpose_min_face_person_px", 100.0)))
        self.tracker = Tracker()
        self.last_error = ""
        self._box_stats_cache = {}
        self._small_detector = None
        self._small_model = str(cfgv.get("hpose_small_model")
                                or "yolo11n-pose-320.onnx")
        self._stats = {"frames": 0, "hits": 0, "misses": 0,
                       "ms_total": 0.0, "ms_hit": 0.0, "ms_miss": 0.0}
        self._stop_small_thread = False
        # Warn up front if the user requested view/stance filters but the
        # shipped bundle cannot classify (feature_version mismatch). Fail-open
        # in matches_view/matches_stance means the filter silently does
        # nothing - better to tell the user at startup and force "any".
        _view_req = str(cfgv.get("hpose_view", "any")).lower()
        _stance_req = str(cfgv.get("hpose_stance", "any")).lower()
        if _view_req not in ("", "any") or _stance_req not in ("", "any"):
            try:
                bundle = self.analyzer.bundle
                if not getattr(bundle, "has_view", lambda: False)():
                    log("hpose: view filter requested (%r) but the shipped "
                        "bundle cannot classify view. Forcing hpose_view=any. "
                        "Retrain the bundle to restore view filtering."
                        % (_view_req,))
                    self.cfgv["hpose_view"] = "any"
                if not getattr(bundle, "has_stance", lambda: False)():
                    log("hpose: stance filter requested (%r) but the "
                        "shipped bundle cannot classify stance. Forcing "
                        "hpose_stance=any. Retrain the bundle to restore "
                        "stance filtering." % (_stance_req,))
                    self.cfgv["hpose_stance"] = "any"
            except Exception:
                pass
        # Kick off the small-model export/build in the background so the
        # first scan does not stall on a 30-60 s Ultralytics export.
        threading.Thread(target=self._prepare_small_detector,
                          name="hpose-small-model", daemon=True).start()

    def _tta_flag(self, img, n_poses, max_score):
        cfgv = getattr(self, "cfgv", None) or {}
        try:
            tta = str(cfgv.get("hpose_tta", "auto") or "auto").strip().lower()
        except Exception:
            tta = "auto"
        if tta == "off":
            return False
        if tta == "on":
            return True
        return n_poses == 0 or max_score < 0.20

    def _get_box_stats(self, img, lb, track_id, cfgv=None):
        """Cached version of _hpose_box_stats keyed by (track_id, img.shape, box).
        Only recomputes when the subject's box or image changes."""
        key = (int(track_id), tuple(img.shape),
               tuple(round(float(v), 1) for v in lb))
        cached = self._box_stats_cache.get(key)
        if cached is not None:
            return cached
        cfgv = cfgv or getattr(self, "cfgv", {}) or {}
        _want_sig = bool(cfgv.get("follow_enabled")) or \
                    bool(cfgv.get("hold_follow", True))
        if not _want_sig:
            area = float((lb[2] - lb[0]) * (lb[3] - lb[1]))
            result = (area, 0.0, None)
        else:
            result = _hpose_box_stats(img, lb)
        # Bound the cache to avoid unbounded growth: evict oldest entry
        if len(self._box_stats_cache) >= 64:
            self._box_stats_cache.popitem(last=False)
        self._box_stats_cache[key] = result
        return result

    def _get_small_detector(self):
        # Non-blocking: returns None until the background thread has
        # finished. First scan uses the big model for ROI; subsequent
        # scans get the 3x ROI win.
        return self._small_detector

    def _prepare_small_detector(self):
        try:
            import os
            path = os.path.join(cache_dir(), self._small_model)
            if not (os.path.isfile(path) and os.path.getsize(path) > 1024):
                if self._stop_small_thread:
                    return
                if not _export_small_pose(path):
                    log("hpose: 320 ROI model unavailable; ROI runs at "
                        "the 640 model speed (~50 ms). Install "
                        "'ultralytics' or drop %s into %s."
                        % (self._small_model, cache_dir()))
                    return
            if self._stop_small_thread:
                return
            self._small_detector = PoseDetector(
                model=self._small_model, imgsz=320, conf=0.05,
                download=False)
            log("hpose: 320 ROI model ready")
        except Exception as exc:
            log("hpose: 320 ROI model failed (%r); ROI runs at 640 speed"
                % (exc,))

    def _pick_roi_detector(self, expected_h: float, roi_side: float):
        """Small model is fine if the target ends up >= 32 px in the
        320-tensor after letterbox. Below that, use the big model (which
        gets twice the tensor resolution for the same upscale).

        For expected_h = 24 and roi_side = 104: target_in_small =
        24 * 320/104 = 73.8 px → small model, ~15 ms.
        For expected_h = 24 and roi_side = 192: target_in_small =
        24 * 320/192 = 40 px → small model but marginal.
        For expected_h = 24 and roi_side = 640: target_in_small =
        24 * 320/640 = 12 px → big model, ~50 ms.
        """
        if expected_h is None or roi_side <= 0:
            return self._get_small_detector() or self.analyzer.pose_detector
        target_in_small = expected_h * (320.0 / roi_side)
        if target_in_small >= 32.0:
            return self._get_small_detector() or self.analyzer.pose_detector
        return self.analyzer.pose_detector

    @staticmethod
    def _expected_h_from_crop(cs):
        # A crop sized `cs` was chosen to hold a person of roughly
        # `cs / 8` px (the 8x upscale heuristic from _roi_pass).
        return max(1.0, cs / 8.0)

    def _record_stats(self, hit: bool, t_start: float) -> None:
        """Record per-frame hit/miss and latency for instrumentation."""
        elapsed_ms = (time.perf_counter() - t_start) * 1000.0
        self._stats["frames"] += 1
        if hit:
            self._stats["hits"] += 1
            self._stats["ms_hit"] += elapsed_ms
        else:
            self._stats["misses"] += 1
            self._stats["ms_miss"] += elapsed_ms
        self._stats["ms_total"] += elapsed_ms

    def _finish_scan(self, dets, t):
        dets = _suppress_dets(dets)[:MAX_EXPLAIN_PER_FRAME]
        tracks = self.tracker.update(dets, t)
        out = []
        for tr in tracks:
            sub = Subject(tr.box,
                          view=tr.view,
                          stance=tr.stance,
                          motion=tr.motion_state,
                          speed=tr.speed,
                          score=tr.score,
                          view_confidence=tr.view_probs.get(tr.view, 0.0),
                          stance_confidence=tr.stance_probs.get(tr.stance,
                                                                0.0),
                          track_id=tr.id,
                          kp_conf=getattr(tr, "kp_conf", 1.0),
                          raw_score=getattr(tr, "raw_score", 1.0))
            sub.hits = int(getattr(tr, "hits", 0))
            sub.head_kp = getattr(tr, "head_kp", None)
            sub.kps = getattr(tr, "kps", None)
            sub.facing_camera = tr.view in ("front", "front_left_45",
                                            "front_right_45")
            sub.back_turned = tr.view in ("back", "back_left_45",
                                          "back_right_45")
            out.append(sub)
        return out

    def scan(self, img, origin=(0, 0), t=None, deadline=None,
             zoom_deadline=None, aim=None, aim_radius=0.0, tile_hints=None):
        if img is None or getattr(img, "size", 0) == 0:
            return []
        if t is None:
            t = time.perf_counter()
        ox, oy = int(origin[0]), int(origin[1])
        analyzer = self.analyzer
        focal = max(1.0, img.shape[1] * 0.9)
        min_side = min(img.shape[:2])
        radius = float(aim_radius or 0.0)
        cfgv = self.cfgv or {}
        zdl = zoom_deadline if zoom_deadline is not None else deadline
        now = time.perf_counter()

        # Aim-first: when the search region is small enough that a single
        # ROI crop covers it, skip the full-frame detect entirely. The
        # ONNX model input tensor is fixed at 640x640 regardless of crop
        # size, so the savings is running ONE inference instead of TWO,
        # not an 8x per-inference speedup.
        ROI_FIRST_MAX_RADIUS = float(
            cfgv.get("hpose_roi_first_max_radius", 150.0))
        if aim is not None and 4.0 < radius <= ROI_FIRST_MAX_RADIUS \
                and min_side >= 60 \
                and (deadline is None or now < deadline):
            roi = self._roi_pass(analyzer, img, ox, oy, aim, radius,
                                 zdl, expected_h=radius * 0.6)
            if roi:
                self._record_stats(True, t)
                return self._finish_scan(roi, t)
            # ROI missed. Try blob_watch as a fallback for small targets
            # that YOLO missed (e.g. synthetic silhouettes, low-contrast
            # figures). Runs before the crop_covers_search early return so
            # production-sized crops also get the blob fallback.
            if bool(cfgv.get("blob_watch", True)) \
                    and (deadline is None or time.perf_counter() < deadline):
                try:
                    aim_x = ox + aim[0] if aim else img.shape[1] // 2
                    aim_y = oy + aim[1] if aim else img.shape[0] // 2
                    _bt = _blob_watch(img, aim_x, aim_y,
                                              max(radius, 40.0),
                                              deadline=zdl)
                except Exception:
                    _bt = None
                if _bt is not None:
                    cx_abs = aim_x + _bt.dx
                    cy_abs = aim_y + _bt.dy
                    # blob_watch already validated shape/contrast/aspect,
                    # so trust it with a score that passes the tracker's
                    # low_score filter (default 0.35).
                    blob_score = min(1.0, float(_bt.contrast) / 40.0)
                    dets = [{
                        "box": (cx_abs - _bt.bw / 2.0,
                                cy_abs - _bt.bh / 2.0,
                                cx_abs + _bt.bw / 2.0,
                                cy_abs + _bt.bh / 2.0),
                        "score": blob_score,
                        "raw_score": blob_score,
                        "kp_conf": 0.0,
                        "head_kp": None,
                        "view_probs": {},
                        "stance_probs": {},
                    }]
                    self._record_stats(True, t)
                    return self._finish_scan(dets, t)
            # ROI missed. Fall through ONLY when the crop is substantially
            # larger than the search region — that is the only case where
            # the full-frame pass can find anything the ROI didn't already
            # see. assist.py always passes a crop sized to radius+12, so
            # this branch is dead in production.
            crop_covers_search = (min_side <= int(2.0 * (radius + 30.0)))
            if crop_covers_search:
                # We searched the entire region. Nothing here.
                self._record_stats(False, t)
                return []
            # Else: caller passed a much larger image; fall through.

        # Full-frame path: detect on the whole image, then optionally
        # supplement with ROI / crop / tile-hint passes.
        want_face = min_side >= 400 and (
            aim is None or radius >= 100.0)
        faces = analyzer._face_boxes(img) if want_face else []

        poses = analyzer.pose_detector.detect(img)
        max_score = max((p.score for p in poses), default=0.0)
        if self._tta_flag(img, len(poses), max_score):
            poses = analyzer.pose_detector.detect(img, tta=True)
        full = [self._explain_det(analyzer, pose, score, kp, raw_score,
                                  focal, faces, img, ox, oy)
                for pose, score, kp, raw_score in _prefilter(poses)]
        dets = full[:MAX_EXPLAIN_PER_FRAME]
        side_cap = min(img.shape[:2])

        if not full:
            if side_cap >= 120 and (zdl is None or now < zdl):
                # Small-target pass: upscale a tile sized to the expected person.
                expected = max(24.0, float(aim_radius or 40.0) * 0.6)
                tile_cs = int(min(side_cap - 4,
                                  max(48, int(8.0 * expected))))
                extra = self._crop_pass(analyzer, img, ox, oy, tile_cs,
                                        deadline=zdl)
                dets = dets + extra

        # Tile-hint pass: upscale classical candidate boxes and run pose detection
        if tile_hints and zdl is not None and now < zdl:
            extra = self._tile_hint_pass(analyzer, img, ox, oy, tile_hints, zdl)
            dets = dets + extra

        dets = _suppress_dets(dets)[:MAX_EXPLAIN_PER_FRAME]

        # Blob-watch fallback: when hpose finds nothing, try the blob
        # watcher as a last resort for small high-contrast people.
        # Route through a det dict so the tracker sees it (track
        # continuity, Kalman, motion history) rather than a raw Subject.
        if not dets and aim is not None and 4.0 < radius <= ROI_FIRST_MAX_RADIUS \
                and (deadline is None or time.perf_counter() < deadline):
            try:
                aim_x = ox + aim[0] if aim else img.shape[1] // 2
                aim_y = oy + aim[1] if aim else img.shape[0] // 2
                _bt = _blob_watch(img, aim_x, aim_y,
                                  max(radius, 40.0), deadline=zdl)
            except Exception:
                _bt = None
            if _bt is not None:
                cx_abs = aim_x + _bt.dx
                cy_abs = aim_y + _bt.dy
                blob_score = min(1.0, float(_bt.contrast) / 40.0)
                dets.append({
                    "box": (cx_abs - _bt.bw / 2.0, cy_abs - _bt.bh / 2.0,
                            cx_abs + _bt.bw / 2.0, cy_abs + _bt.bh / 2.0),
                    "score": blob_score,
                    "raw_score": blob_score,
                    "kp_conf": 0.0,
                    "head_kp": None,
                    "view_probs": {},
                    "stance_probs": {},
                })
                self._record_stats(True, t)

        if dets:
            self._record_stats(True, t)
            return self._finish_scan(dets, t)

        self._record_stats(False, t)
        return []


    def _explain_det(self, analyzer, pose, score, kp, raw, focal, faces,
                     img, ox, oy):
        det = analyzer.explain(pose, focal, faces, img)
        box = det.get("box") or (0.0, 0.0, 0.0, 0.0)
        head = None
        try:
            k0 = pose.kps[0]
            if float(k0[2]) >= 0.5:
                head = (float(k0[0]) + ox, float(k0[1]) + oy)
        except Exception:
            head = None
        kps_screen = None
        try:
            kps_screen = [(float(k[0]) + ox, float(k[1]) + oy, float(k[2]))
                          for k in pose.kps[:17]]
        except Exception:
            kps_screen = None
        return {
            "box": (box[0] + ox, box[1] + oy, box[2] + ox, box[3] + oy),
            "score": score,
            "raw_score": raw,
            "kp_conf": kp,
            "head_kp": head,
            "kps": kps_screen,
            "view_probs": det.get("view_probs") or {},
            "stance_probs": det.get("stance_probs") or {},
        }

    def _crop_pass(self, analyzer, img, ox, oy, cs, deadline=None):
        h, w = img.shape[:2]
        cx0, cy0 = (w - cs) // 2, (h - cs) // 2
        crop = img[cy0:cy0 + cs, cx0:cx0 + cs]
        focal = max(1.0, cs * 0.9)
        det = self._pick_roi_detector(self._expected_h_from_crop(cs), float(cs))
        raw = _prefilter(det.detect(crop))
        raw = _suppress_contained(raw)[:MAX_EXPLAIN_PER_FRAME]
        out = []
        for pose, score, kp, raw_score in raw:
            if deadline is not None and time.perf_counter() > deadline:
                break
            out.append(self._explain_det(analyzer, pose, score, kp,
                                         raw_score, focal, [], crop,
                                         cx0 + ox, cy0 + oy))
        return out

    def _tile_hint_pass(self, analyzer, img, ox, oy, tile_hints, deadline=None):
        """Upscale classical candidate boxes, run the pose detector on each
        tile, and map detections back to image coordinates."""
        h, w = img.shape[:2]
        out = []
        for bx0, by0, bx1, by1 in tile_hints:
            if deadline is not None and time.perf_counter() > deadline:
                break
            bh = max(1.0, float(by1 - by0))
            # Upscale tile so the expected target fills ~256-640 px input
            up_scale = max(1.0, min(12.0, 256.0 / bh))
            tw = max(64, int((bx1 - bx0) * up_scale))
            th = max(64, int(bh * up_scale))
            tx0 = int(max(0, bx0 - (tw - (bx1 - bx0)) // 2))
            ty0 = int(max(0, by0 - (th - bh) // 2))
            tx1 = min(w, tx0 + tw)
            ty1 = min(h, ty0 + th)
            tx0 = max(0, tx1 - tw)
            ty0 = max(0, ty1 - th)
            crop = img[ty0:ty1, tx0:tx1]
            if min(crop.shape[:2]) < 40:
                continue
            if deadline is not None and time.perf_counter() >= deadline:
                break
            focal = max(1.0, max(crop.shape[:2]) * 0.9)
            det = self._pick_roi_detector(max(1.0, bh), float(bh))
            raw = _prefilter(det.detect(crop))
            chosen = raw
            if len(raw) >= 2:
                chosen = _suppress_contained(raw)[:MAX_EXPLAIN_PER_FRAME]
            for pose, score, kp, raw_score in chosen:
                if deadline is not None and time.perf_counter() > deadline:
                    break
                # Map tile offset into screen-space ox/oy for _explain_det
                out.append(self._explain_det(analyzer, pose, score, kp,
                                             raw_score, focal, [], crop,
                                             ox + tx0, oy + ty0))
        return out

    def _roi_pass(self, analyzer, img, ox, oy, aim, radius, deadline=None,
                  expected_h: float = None):
        h, w = img.shape[:2]
        acx, acy = int(aim[0]), int(aim[1])
        if not (0 <= acx < w and 0 <= acy < h):
            return []
        # Side scaled by expected target height so 20 px targets don't get
        # 160 px tiles (which are 90 % background) and 160 px targets don't
        # get cropped below their body. Size the ROI so the upscaled target
        # is ~64 px in the 320 tensor (YOLO's reliable threshold).
        if expected_h:
            roi_side = min(5.0 * expected_h, 2.0 * radius + expected_h)
            side = int(min(float(min(h, w) - 4), roi_side))
        else:
            side = int(max(ROI_MIN_SIDE, ROI_MARGIN * float(radius)))
            side = min(side, min(h, w) - 4)
        if side < 60 or side >= min(h, w) - 2:
            return []
        x1 = int(max(0, acx - side // 2))
        y1 = int(max(0, acy - side // 2))
        x2 = min(w, x1 + side)
        y2 = min(h, y1 + side)
        x1 = max(0, x2 - side)
        y1 = max(0, y2 - side)
        crop = img[y1:y2, x1:x2]
        ch, cw = crop.shape[:2]
        if min(crop.shape[:2]) < 60:
            return []
        if deadline is not None and time.perf_counter() >= deadline:
            return []
        focal = max(1.0, max(ch, cw) * 0.9)
        det = self._pick_roi_detector(expected_h, float(side))
        raw = _prefilter(det.detect(crop))
        chosen = raw
        if len(raw) >= 2:
            chosen = _suppress_contained(raw)[:MAX_EXPLAIN_PER_FRAME]
        out = []
        for pose, score, kp, raw_score in chosen:
            if deadline is not None and time.perf_counter() > deadline:
                break
            out.append(self._explain_det(analyzer, pose, score, kp,
                                         raw_score, focal, [], crop,
                                         x1 + ox, y1 + oy))
        return out

    def lead_of(self, subject, seconds: float):
        if subject is None:
            return (0.0, 0.0)
        track = self.tracker.by_id(subject.track_id)
        if track is None:
            return (0.0, 0.0)
        _x, _y, ox, oy = track.lead(seconds)
        return (ox, oy)

    def reset(self):
        try:
            self.tracker.reset()
        except Exception as exc:
            log("hpose: engine reset failed (%r)" % (exc,))

_ENGINE = None
_LOCK = threading.Lock()
_LOADING = False
_TRIED = False
_TRIED_AT = 0.0
_RETRY_AFTER_S = 30.0

def _load_in_background(cfgv):
    global _LOADING, _TRIED, _TRIED_AT
    now = time.monotonic()
    with _LOCK:
        if _ENGINE is not None:
            return _ENGINE
        if _LOADING:
            return None
        if _TRIED and (now - _TRIED_AT) < _RETRY_AFTER_S:
            return None
        _LOADING = True
        _TRIED = True
        _TRIED_AT = now

    def worker():
        global _ENGINE, _LOADING, _TRIED_AT
        try:
            eng = Engine(cfgv)
            with _LOCK:
                _ENGINE = eng
                _TRIED_AT = time.monotonic()
            log("hpose: ready (%s on %s)"
                % (getattr(eng.analyzer.pose_detector, "model_name", "?"),
                   getattr(eng.analyzer.pose_detector,
                           "active_provider", "CPUExecutionProvider")))
        except Exception as exc:
            log("hpose: could not start (%r) - the classical scanner "
                "handles everything." % (exc,))
            with _LOCK:
                # Keep _TRIED set but back off so a broken model does not
                # spawn a fresh load thread on every scan.
                _TRIED_AT = time.monotonic()
        finally:
            with _LOCK:
                _LOADING = False

    threading.Thread(target=worker, name="hpose-load", daemon=True).start()
    return None

def engine(cfgv=None, force: bool = False):
    global _TRIED, _ENGINE, _LOADING
    if force:
        with _LOCK:
            old = _ENGINE
            _ENGINE = None
            _LOADING = False
            _TRIED = False
        if old is not None:
            old._stop_small_thread = True
    with _LOCK:
        if _ENGINE is not None:
            return _ENGINE
    try:
        import importlib
        importlib.import_module("cv2")
        importlib.import_module("onnxruntime")
    except Exception as exc:
        with _LOCK:
            if not _TRIED:
                _TRIED = True
                log("hpose: not available (%r) - the classical scanner "
                    "handles everything." % (exc,))
        return None
    return _load_in_background(cfgv)

def preload(cfgv=None):
    return engine(cfgv)

def reset():
    with _LOCK:
        if _ENGINE is not None:
            _ENGINE.reset()

def available() -> bool:
    return engine() is not None


def stats() -> dict:
    """Return per-frame hit/miss and latency instrumentation, or None."""
    with _LOCK:
        if _ENGINE is None or not hasattr(_ENGINE, "_stats"):
            return None
        s = _ENGINE._stats
        n = max(1, s["frames"])
        hits = s["hits"]
        miss = s["misses"]
        return {
            "frames": s["frames"],
            "hit_rate": hits / n,
            "avg_ms": s["ms_total"] / n,
            "avg_hit_ms": (s["ms_hit"] / hits) if hits else 0.0,
            "avg_miss_ms": (s["ms_miss"] / miss) if miss else 0.0,
        }

def mode_of(cfgv) -> str:
    try:
        mode = str(cfgv.get("hpose_mode", "off") or "off").strip().lower()
    except Exception:
        mode = "off"
    return "on" if mode in ("on", "true", "1", "yes", "enabled") else "off"

def wanted(cfgv) -> bool:
    if mode_of(cfgv) != "on":
        return False
    return True

def matches_view(subject, want: str) -> bool:
    want = (want or "any").strip().lower()
    if want in ("", "any"):
        return True
    view = (subject.view or "").lower()
    if view == "unknown" or not view:
        return True                     # fail-open, not fail-closed
    if want == "facing":
        return subject.facing_camera
    if want == "away":
        return subject.back_turned
    if want == "side":
        return view.startswith("side")
    if want == "front":
        return view.startswith("front")
    if want == "back":
        return view.startswith("back")
    return True

def matches_stance(subject, want: str) -> bool:
    want = (want or "any").strip().lower()
    if want in ("", "any"):
        return True
    stance = (subject.stance or "").lower()
    if stance == "unknown" or not stance:
        return True                     # fail-open, not fail-closed
    if want == "low":
        return stance in ("crouch", "prone")
    return stance == want

def pick(subjects, cfgv, cx, cy, radius):
    want_view = str(cfgv.get("hpose_view", "any") or "any")
    want_stance = str(cfgv.get("hpose_stance", "any") or "any")
    min_conf = 0.0
    try:
        min_conf = float(cfgv.get("hpose_min_conf", 35) or 0.0) / 100.0
    except (TypeError, ValueError):
        min_conf = 0.0
    motion_priority = bool(cfgv.get("motion_priority", True))
    motion_rank = {"fast": 0, "slow": 1, "static": 2}
    best = None
    best_key = None
    for sub in subjects:
        h = max(1.0, sub.box[3] - sub.box[1])
        size_scale = max(0.4, min(1.0, h / 120.0))
        raw_floor = 0.10 + 0.10 * size_scale
        kp_floor = 0.08 + 0.17 * size_scale
        score_floor = max(0.10, min_conf * size_scale)
        hits_needed = 1 if size_scale <= 0.5 else 2
        if getattr(sub, "raw_score", 1.0) < raw_floor and \
                getattr(sub, "hits", 2) < hits_needed:
            continue
        kp = getattr(sub, "kp_conf", 1.0)
        if kp < kp_floor and sub.score < score_floor * 1.4:
            continue
        if sub.score < score_floor:
            continue
        if not matches_view(sub, want_view):
            continue
        if not matches_stance(sub, want_stance):
            continue
        sxc, syc = sub.center
        dist = math.hypot(sxc - cx, syc - cy)
        if radius and dist > radius:
            continue
        if motion_priority:
            mrank = motion_rank.get(getattr(sub, "motion", "static"), 2)
            key = (mrank, round(dist, 1), -sub.score)
        else:
            key = (round(dist, 1), -sub.score)
        if best_key is None or key < best_key:
            best_key = key
            best = sub
    return best

# ======================================================================
# merged from vision.py
# ======================================================================

import functools
import math
import threading
import time

import numpy as np

from system import (ACCENT_PRESETS,
                        hsv_to_rgb,
                        parse_accent as _parse_accent,
                        parse_accents as _parse_accents)

_LOG_SINK = None

try:

    from input import get_dpi_scale as _get_dpi_scale
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

def _grow_cells(strong, weak, max_iter: int = 256):
    keep = strong.copy()
    for i in range(max_iter):
        grown = _dilate8(keep) & weak
        grown |= keep
        if np.array_equal(grown, keep):
            return keep
        keep = grown
        if (i & 31) == 31 and time.perf_counter() > getattr(
                _SCAN_DEADLINE, "deadline", float("inf")):
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
SHAPE_MIN_RATIO = 0.6
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
    if SHAPE_PLATEAU_HI < r < SHAPE_HI:
        return max(0.0, (SHAPE_HI - r) / (SHAPE_HI - SHAPE_PLATEAU_HI))
    # Below the person-shaped plateau, score falls smoothly to zero as the
    # box becomes more square-ish or more needle-like.
    if SHAPE_MIN_RATIO < r < SHAPE_LO:
        return (r - SHAPE_MIN_RATIO) / (SHAPE_LO - SHAPE_MIN_RATIO)
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
                        and 1.4 <= r <= SHAPE_HI)
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
    except (TypeError, ValueError):
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
        except (TypeError, ValueError):
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

    if matcher == "oklab":
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

    if matcher == "oklab":

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
        while True:
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
                continue
            if self.dim == 3 and abs(z[2] - self.x[2]) > self.SCALE_JUMP:
                self.reset()
                continue
            break

        n = self.dim
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
        if self.x is None or len(self.x) < self.dim + 2:
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
                or len(self.x) < self.dim + 2 or self.updates < 2:
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

    px = a[..., :3].reshape(-1, 3).astype(np.float32) / 255.0
    wt = weight.reshape(-1)
    keep = wt >= CALIB_MIN_WEIGHT
    if int(keep.sum()) >= 64:
        px, wt = px[keep], wt[keep]
    if px.shape[0] > 20000:

        step = px.shape[0] // 20000 + 1
        px, wt = px[::step], wt[::step]

    if matcher == "oklab":

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
        return bool(wanted(cfgv))
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

def _subject_silhouette(subject, lb, img_shape, pad=2):
    """Thin 17-kp silhouette: a filled hull of the visible keypoints.

    Returns (x1, y1, x2, y2, mask) or None. Uses only kp confidence >= 0.30
    and marks, per scan-row, the pixels between the leftmost and rightmost
    keypoint on that row.
    """
    kps = getattr(subject, "kps", None)
    if kps is None:
        return None
    try:
        pts = []
        for i in range(17):
            x = float(kps[i][0]) - lb[0]
            y = float(kps[i][1]) - lb[1]
            conf = float(kps[i][2]) if len(kps[i]) > 2 else 1.0
            if conf >= 0.30 and math.isfinite(x) and math.isfinite(y):
                pts.append((int(round(x)), int(round(y))))
        if len(pts) < 5:
            return None
        x_lo = max(0, min(p[0] for p in pts) - pad)
        y_lo = max(0, min(p[1] for p in pts) - pad)
        x_hi = min(img_shape[1] - lb[0], max(p[0] for p in pts) + pad)
        y_hi = min(img_shape[0] - lb[1], max(p[1] for p in pts) + pad)
        if x_hi <= x_lo or y_hi <= y_lo:
            return None
        mask = np.zeros((y_hi - y_lo, x_hi - x_lo), dtype=bool)
        rows = {}
        for px, py in pts:
            rows.setdefault(py - y_lo, []).append(px - x_lo)
        for r, xs in rows.items():
            if 0 <= r < mask.shape[0]:
                lo = max(0, min(xs) - 1)
                hi = min(mask.shape[1], max(xs) + 2)
                mask[r, lo:hi] = True
        if int(mask.sum()) < 12:
            return None
        return (x_lo, y_lo, x_hi, y_hi, mask)
    except Exception:
        return None


def _fast_classical_boxes(img, cx, cy, radius, deadline=None, cfgv=None):
    """Fast classical pre-scan: returns list of candidate boxes (x1, y1, x2, y2)
    for 20-160px person-like objects. Lightweight version of the classical
    pipeline that stops at connected-component bounding boxes."""
    if deadline is not None and time.perf_counter() > deadline:
        return []
    try:
        a = img.astype(np.float32)
        if a.ndim == 3 and a.shape[2] > 3:
            a = a[..., :3]
        h, w = a.shape[:2]
        bg = _box_blur(a, BLUR_WIN)
        diff = np.sqrt(((a - bg) ** 2 * _CH_WEIGHTS).sum(axis=2))
        if _bool_of(cfgv or {}, "luminance_norm", True):
            lum = (bg * _CH_WEIGHTS).sum(axis=2)
            diff = diff * np.clip(NORM_K / (lum + NORM_C), NORM_MIN, NORM_MAX)
        flat = diff.ravel()
        if flat.size > 20000:
            flat = flat[::flat.size // 20000 + 1]
        energy = float(np.percentile(flat, ADAPTIVE_PERCENTILE))
        thr = max(38.0, min(200.0, energy * 0.4))
        ink = diff > thr
        ink_w = diff > thr * 0.55
        for m in (ink, ink_w):
            m[:2, :] = False; m[-2:, :] = False; m[:, :2] = False; m[:, -2:] = False
        ink = _erode(_dilate(ink, 1), 1)
        ink_w = _erode(_dilate(ink_w, 1), 1)
        strong = ink
        weak = ink_w
        cs = 4
        gh, gw = a.shape[0] // cs, a.shape[1] // cs
        block = lambda m: m[:gh * cs, :gw * cs].reshape(gh, cs, gw, cs).mean(axis=(1, 3))
        grown = _grow_cells(block(strong) > 0.25, block(weak) > 0.25)
        try:
            import cv2
            n, lbl, stats, _ = cv2.connectedComponentsWithStats(grown.astype(np.uint8), connectivity=8)
            boxes = []
            for i in range(1, n):
                x, y, bw_c, bh_c, _ = stats[i]
                if bh_c < 4 or bw_c < 2:
                    continue
                bx0, by0 = x * cs, y * cs
                bx1, by1 = (x + bw_c) * cs, (y + bh_c) * cs
                bw, bh = bx1 - bx0, by1 - by0
                if not (14 <= bh <= 180):
                    continue
                asp = bw / bh if bh > 0 else 0
                if not (0.3 <= asp <= 0.85):
                    continue
                bxc = (bx0 + bx1) * 0.5
                byc = (by0 + by1) * 0.5
                if radius and math.hypot(bxc - cx, byc - cy) > radius + 20:
                    continue
                boxes.append((bx0, by0, bx1, by1))
            return boxes[:6]
        except Exception:
            return []
    except Exception:
        return []


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
        eng = engine(cfgv)
    except Exception:
        return None
    if eng is None:
        return None
    if time.perf_counter() > deadline:
        return None
    ox, oy = (int(origin[0]), int(origin[1])) if origin else (0, 0)

    # Fast classical pre-scan to get candidate boxes for tiled inference.
    # Only needed when the ROI-first pass might miss: for small radii the
    # ROI crop (expected_h = radius * 0.6) already covers every pixel that
    # could contain the target, so tile hints are pure overhead.
    if radius > 80.0:
        tile_hints = _fast_classical_boxes(img, cx, cy, radius, deadline, cfgv)
    else:
        tile_hints = []

    try:
        try:
            budget_s = min(max(float(cfgv.get("scan_budget_ms", 350)),
                               60.0), 2000.0) / 1000.0
        except (TypeError, ValueError):
            budget_s = 0.1
        subjects = eng.scan(img, (ox, oy), deadline=deadline,
                               zoom_deadline=deadline + 3.0 * budget_s,
                               aim=(int(cx), int(cy)),
                               aim_radius=float(radius or 0.0),
                               tile_hints=tile_hints)
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
    subject = pick(subjects, cfgv, screen_cx, screen_cy,
                                radius)
    if subject is None:
        return None
    aim = str(cfgv.get("hpose_aim", "center") or "center").lower()
    if aim == "head":
        head_kp = getattr(subject, "head_kp", None)
        if head_kp is not None:
            tx = float(head_kp[0])
            ty = float(head_kp[1])
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
            lx, ly = eng.lead_of(subject, lead_s)
        except Exception:
            lx = ly = 0.0
    ddx = int(round(tx + lx - screen_cx))
    ddy = int(round(ty + ly - screen_cy))
    move = math.hypot(ddx, ddy)
    lb = (subject.box[0] - ox, subject.box[1] - oy,
          subject.box[2] - ox, subject.box[3] - oy)
    area = float((subject.box[2] - subject.box[0])
                 * (subject.box[3] - subject.box[1]))
    contrast = 0.0
    sig = None
    bw = int(round(subject.width))
    bh = int(round(subject.height))
    try:
        area, contrast, sig = eng._get_box_stats(img, lb, subject.track_id, cfgv)
    except Exception:
        pass
    fig_score = 0.0
    try:
        sil = _subject_silhouette(subject, lb, img.shape, pad=2)
        if sil is not None:
            sx1, sy1, sx2, sy2, smask = sil
            fig_score = _figure_score(sx2 - sx1, sy2 - sy1,
                                      float(smask.sum()), smask)
        elif bw > 0 and bh > 0:
            tier = _shape_tier(bw, bh, float(bw * bh))
            fig_score = 0.55 if tier == 2 else (0.32 if tier == 1 else 0.0)
    except Exception:
        pass
    target = SnapTarget(ddx, ddy, move, area, contrast, bw, bh, sig,
                         max(float(subject.score), fig_score))
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
BLOB_MAX_H = 180.0
BLOB_ASPECT_LO = 0.30
BLOB_ASPECT_HI = 0.85
BLOB_STICK_LO = 0.15
BLOB_STICK_HI = 0.30
BLOB_FILL_MIN = 0.30
BLOB_STICK_FILL_MIN = 0.20
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

def _blob_watch(img, cx, cy, radius, deadline=None):
    if deadline is not None and time.perf_counter() > deadline:
        return None
    if img is None:
        return None
    a = np.asarray(img, dtype=np.float32)
    if a.ndim == 2:
        a = np.stack([a, a, a], axis=2)
    elif a.ndim == 3 and a.shape[2] > 3:
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
    max_iter = 8 if radius and radius <= 60 else 32
    grown = _grow_cells(block(strong) > 0.25, block(weak) > 0.25,
                        max_iter=max_iter)
    components = None
    try:
        import cv2
        n, lbl, stats, _ = cv2.connectedComponentsWithStats(
            grown.astype(np.uint8), connectivity=8)
        components = []
        for i in range(1, n):
            x, y, w, h, area = stats[i]
            components.append((int(x), int(y),
                               int(x + w), int(y + h), int(area)))
    except Exception:
        components = None
    if components is None:
        components = _blob_components_cells(grown)
    best = None
    best_key = None
    for bx0, by0, bx1, by1, ncell in components:
        if deadline is not None and time.perf_counter() > deadline:
            return None
        bw = (bx1 - bx0) * cs / float(zoom)
        bh = (by1 - by0) * cs / float(zoom)
        if not (BLOB_MIN_H <= bh <= BLOB_MAX_H):
            continue
        asp = bw / bh if bh > 0 else 0.0
        is_normal = BLOB_ASPECT_LO <= asp <= BLOB_ASPECT_HI
        is_stick = BLOB_STICK_LO <= asp <= BLOB_STICK_HI and bh >= BLOB_MIN_H
        if not (is_normal or is_stick):
            continue
        fill = ncell / float((bx1 - bx0) * (by1 - by0))
        fill_min = BLOB_FILL_MIN if is_normal else BLOB_STICK_FILL_MIN
        if fill < fill_min:
            continue
        y0, y1 = by0 * cs, by1 * cs
        x0, x1 = bx0 * cs, bx1 * cs
        m = grown[by0:by1, bx0:bx1]
        m_px = np.repeat(np.repeat(m, cs, axis=0), cs, axis=1)
        mean_c = float(c[y0:y1, x0:x1][m_px].mean()) if m.any() else 0.0
        if mean_c < thr * 0.8:
            continue
        bxc = (x0 + x1) / (2.0 * zoom)
        byc = (y0 + y1) / (2.0 * zoom)
        dist = math.hypot(bxc - cx, byc - cy)
        if radius and dist > radius + 10.0:
            continue
        score = mean_c * fill * (1.0 - abs(asp - 0.5) / 0.5)
        if is_stick:
            score *= 0.50
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

    if img is None:
        return ScanResult("none", None, 0.0)

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
    # Classical fallback is for UI elements (buttons, checkboxes, high-contrast
    # rectangles), not people. When the caller has explicitly opted into
    # subject/figure mode, they want the small-person path and the classical
    # scanner is dead weight on the miss path (~50 ms per frame). Otherwise
    # keep it — a desktop user with radius=40 for a checkbox still needs it.
    if _hpose_enabled(cfgv):
        _subject_mode = (bool(cfgv.get("subject_priority", False))
                         or bool(cfgv.get("subject_only", False)))
        if _subject_mode:
            if bool(cfgv.get("blob_watch", True)) \
                    and time.perf_counter() < deadline:
                try:
                    _bt = _blob_watch(img, cx, cy, radius,
                                      deadline=deadline)
                except Exception as exc:
                    _log("blob watch failed: %r" % (exc,))
                    _bt = None
                if _bt is not None:
                    return ScanResult("target", _bt,
                                      (time.perf_counter() - t0) * 1000.0)
            return ScanResult("none", None,
                              (time.perf_counter() - t0) * 1000.0)
    if time.perf_counter() > deadline:
        return ScanResult("none", None, (time.perf_counter() - t0) * 1000.0)

    a = np.asarray(img, dtype=np.float32)
    if a.ndim == 2:
        a = np.stack([a, a, a], axis=2)
    elif a.ndim == 3 and a.shape[2] > 3:
        a = a[..., :3]
    h, w = a.shape[:2]

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
    if bool(cfgv.get("blob_watch", True)) \
            and time.perf_counter() < deadline:
        try:
            _bt = _blob_watch(img, cx, cy, radius, deadline=deadline)
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
