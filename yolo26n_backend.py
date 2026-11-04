"""
YOLO26n Detection Backend for Real-time Human Figure Detection

High-performance capture + detection pipeline targeting <10ms per frame.
- Uses YOLO26n ONNX model (FP16) for person detection (COCO class 0)
- 40-50px radius centered on screen center
- Threaded capture + inference pipeline using win32rt CaptureBackend (WGC/dxcam/mss)
- Zero-copy frame passing where possible
"""

from __future__ import annotations

import threading
import time
import queue
import cv2
import numpy as np
from dataclasses import dataclass
from typing import Optional, Tuple, List
from pathlib import Path

# Import win32rt capture backend
try:
    from win32rt import CaptureBackend
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
            import win32rt
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
        for _ in range(runs):
            self._preprocess(dummy)
            self._session.run(self._output_names, {self._input_name: self._preprocess(dummy)})
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
                        import win32rt
                        vx, vy, vw, vh = win32rt.virtual_screen()
                        cx = vx + vw // 2
                        cy = vy + vh // 2
                        r = self.cfg.capture_radius
                        region = (cx - r, cy - r, cx + r, cy + r)
                        if region != self._capture_region:
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
        with self._lock:
            det = self._latest_detection
        if det is None:
            return None
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