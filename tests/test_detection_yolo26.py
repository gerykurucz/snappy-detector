"""Focused tests for the hardcoded YOLO26 detector and pose decoders."""

import os
import sys

import numpy as np


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import detection


class _FakeSession:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def run(self, output_names, feeds):
        self.calls.append((output_names, feeds))
        return [self.output]


def test_preprocess_reusable_blob_is_identical_to_rgb_reference():
    backend = detection.YOLO26nBackend()
    frame = np.arange(31 * 29 * 3, dtype=np.uint8).reshape(31, 29, 3)

    got = backend._preprocess(frame, 64).copy()
    resized = detection.cv2.resize(
        frame, (64, 64), interpolation=detection.cv2.INTER_LINEAR)
    rgb = detection.cv2.cvtColor(resized, detection.cv2.COLOR_BGR2RGB)
    expected = np.ascontiguousarray(
        rgb.transpose(2, 0, 1)[None], dtype=np.float32)
    expected *= (1.0 / 255.0)

    assert got.shape == (1, 3, 64, 64)
    assert got.dtype == np.float32
    np.testing.assert_array_equal(got, expected)


def test_warmup_uses_detector_and_pose_tensor_shapes():
    backend = detection.YOLO26nBackend()
    detector = _FakeSession(np.zeros((1, 84, 1), dtype=np.float32))
    pose = _FakeSession(np.zeros((1, 300, 57), dtype=np.float32))
    backend._session = detector
    backend._pose_session = pose
    backend._input_name = "images"
    backend._output_names = ["output0"]
    backend._pose_input_name = "images"
    backend._pose_output_name = "output0"

    backend._warmup(runs=2)

    assert len(detector.calls) == 2
    assert len(pose.calls) == 2
    assert detector.calls[0][1]["images"].shape == (1, 3, 256, 256)
    assert pose.calls[0][1]["images"].shape == (1, 3, 640, 640)


def test_backend_object_decoder_maps_center_crop_to_screen():
    backend = detection.YOLO26nBackend()
    raw = np.zeros((1, 84, 100), dtype=np.float32)
    scale = 176.0 / 256.0
    raw[0, 0, 0] = 98.0 / scale
    raw[0, 1, 0] = 83.0 / scale
    raw[0, 2, 0] = 20.0 / scale
    raw[0, 3, 0] = 40.0 / scale
    raw[0, 4, 0] = 0.5
    backend._session = _FakeSession(raw)
    backend._input_name = "images"
    backend._output_names = ["output0"]

    detections = backend._detect(
        np.zeros((224, 224, 3), dtype=np.uint8), origin=(100, 200)
    )

    assert len(detections) == 1
    det = detections[0]
    assert (det.x, det.y, det.w, det.h) == (222, 307, 20, 40)
    assert det.source == "detect"


def test_backend_pose_decoder_reads_post_nms_rows():
    backend = detection.YOLO26nBackend()
    raw = np.zeros((1, 300, 57), dtype=np.float32)
    # Coordinates are in the 640 tensor. This maps to a 40x80 box centred
    # 10 px right and 5 px above the centre of a 224px crop.
    raw[0, 0, :6] = [291.43, 191.43, 405.71, 420.0, 0.8, 0.0]
    keypoints = np.tile([348.57, 305.71, 0.9], (17, 1))
    raw[0, 0, 6:] = keypoints.reshape(-1)
    backend._pose_session = _FakeSession(raw)
    backend._pose_input_name = "images"
    backend._pose_output_name = "output0"

    detections = backend._detect_pose(
        np.zeros((224, 224, 3), dtype=np.uint8), origin=(100, 200)
    )

    assert len(detections) == 1
    det = detections[0]
    assert (det.x, det.y, det.w, det.h) == (222, 307, 40, 80)
    assert det.source == "pose"


def test_pose_cadence_stays_fast_for_misses_and_slows_for_confirmation():
    backend = detection.YOLO26nBackend()
    confirmed = detection.Detection(
        100, 100, 20, 40, 0.8, source="detect", predicted=False)
    appearance = detection.Detection(
        100, 100, 20, 40, 0.8, source="color", predicted=True)

    assert backend._pose_submission_interval([]) == 0.20
    assert backend._pose_submission_interval([appearance]) == 0.20
    assert backend._pose_submission_interval([confirmed]) == 1.0


def test_shared_pose_decoder_reads_yolo26_post_nms_layout():
    decoder = object.__new__(detection.PoseDetector)
    decoder.conf = 0.1
    decoder.iou = 0.45
    raw = np.zeros((1, 300, 57), dtype=np.float32)
    raw[0, 0, :6] = [30.0, 40.0, 110.0, 200.0, 0.75, 0.0]
    raw[0, 0, 6:] = np.tile([50.0, 60.0, 0.8], (17, 1)).reshape(-1)

    poses = decoder.decode(raw, r=2.0, left=10.0, top=20.0,
                           w=100, h=100)

    assert len(poses) == 1
    assert poses[0].box == (10.0, 10.0, 50.0, 90.0)
    np.testing.assert_allclose(poses[0].kps[0], [20.0, 20.0, 0.8])
    assert poses[0].score == 0.75


def test_center_tracker_predicts_only_for_short_gaps():
    tracker = detection._CenterTrack(max_age_s=0.10)
    first = detection.Detection(100, 100, 20, 40, 0.8, quality=10.0)
    second = detection.Detection(102, 100, 20, 40, 0.8, quality=10.0)

    a = tracker.update([first], now=1.0, seq=1)
    b = tracker.update([second], now=1.02, seq=2)
    predicted = tracker.update([], now=1.04, seq=3)

    assert a.track_id == b.track_id == predicted.track_id
    assert predicted.predicted
    assert predicted.x > b.x
    assert tracker.update([], now=1.13, seq=4) is None


def test_center_tracker_does_not_associate_a_far_false_positive():
    tracker = detection._CenterTrack(max_age_s=0.10)
    first = tracker.update([
        detection.Detection(100, 100, 10, 20, 0.8, quality=10.0)
    ], now=1.0, seq=1)
    far = tracker.update([
        detection.Detection(180, 100, 10, 20, 0.9, quality=11.0)
    ], now=1.01, seq=2)

    assert far.track_id != first.track_id
    assert tracker.vx == tracker.vy == 0.0


def test_color_contrast_fallback_bridges_a_short_model_miss():
    frame = np.full((224, 224, 3), (30, 120, 30), dtype=np.uint8)
    frame[85:115, 122:138] = (20, 20, 220)
    model = detection.Detection(
        130, 100, 16, 30, 0.8, source="detect", quality=10.0
    )
    fallback = detection._ColorContrastFallback()

    assert fallback.process(frame, (0, 0), [model], now=0.0, seq=1) == []
    changed = frame.copy()
    changed[85:115, 122:138] = (25, 25, 210)
    result = fallback.process(changed, (0, 0), [], now=0.10, seq=2)

    assert len(result) == 1
    assert result[0].source == "color"
    assert result[0].predicted
    assert (result[0].x, result[0].y) == (130, 100)


def test_color_contrast_fallback_rejects_changed_or_expired_appearance():
    frame = np.full((224, 224, 3), (30, 120, 30), dtype=np.uint8)
    frame[85:115, 122:138] = (20, 20, 220)
    model = detection.Detection(
        130, 100, 16, 30, 0.8, source="detect", quality=10.0
    )
    fallback = detection._ColorContrastFallback()
    fallback.process(frame, (0, 0), [model], now=0.0, seq=1)

    different = frame.copy()
    different[85:115, 122:138] = (220, 20, 20)
    assert fallback.process(different, (0, 0), [],
                            now=0.10, seq=2) == []
    assert fallback.process(frame, (0, 0), [],
                            now=0.25, seq=3) == []


def test_center_tracker_keeps_color_measurement_uncertain():
    tracker = detection._CenterTrack(max_age_s=0.10)
    color = detection.Detection(
        100, 100, 20, 40, 0.8, source="color",
        predicted=True, quality=2.0,
    )

    tracked = tracker.update([color], now=1.0, seq=1)

    assert tracked.source == "color"
    assert tracked.predicted


def test_view_move_clears_published_and_pose_results():
    backend = detection.YOLO26nBackend()
    backend._latest_detection = detection.Detection(
        100, 100, 20, 40, 0.8, seq=1, t=1.0)
    backend._last_measurements = [backend._latest_detection]
    backend._pose_result = (1, 1.0, [backend._latest_detection])

    backend.notify_view_moved(when=2.0)

    assert backend.get_latest_detection() is None
    assert backend.get_last_measurements() == []
    assert backend._pose_result is None
    assert backend._view_changed_at == 2.0


def test_async_pose_is_projected_instead_of_applied_at_old_position():
    backend = detection.YOLO26nBackend()
    backend._track.update([
        detection.Detection(100, 112, 20, 40, 0.8, quality=10.0)
    ], now=1.0, seq=1)
    backend._track.update([
        detection.Detection(110, 112, 20, 40, 0.8, quality=10.0)
    ], now=1.1, seq=2)
    old_pose = detection.Detection(
        100, 112, 20, 40, 0.8, source="pose", quality=10.0)

    aligned = backend._align_pose_detections(
        [old_pose], pose_captured_at=1.0, current_captured_at=1.2,
        frame=np.zeros((224, 224, 3), dtype=np.uint8), origin=(0, 0))

    assert len(aligned) == 1
    assert aligned[0].x > old_pose.x
    assert aligned[0].predicted
    assert aligned[0].captured_at == 1.2


def test_materially_stale_pose_without_track_is_rejected():
    backend = detection.YOLO26nBackend()
    pose = detection.Detection(
        112, 112, 20, 40, 0.8, source="pose", quality=10.0)

    assert backend._align_pose_detections(
        [pose], pose_captured_at=1.0, current_captured_at=1.2,
        frame=np.zeros((224, 224, 3), dtype=np.uint8),
        origin=(0, 0)) == []


def test_reviewed_label_sidecar_is_complete_and_compact():
    labels, meta = detection._load_reviewed_labels(
        detection.DEFAULT_BENCHMARK_LABELS
    )

    assert meta["sample_count"] == 202
    assert len(labels) == 191
    assert labels[0] is False
    assert labels[150] is True
    assert 81 * 150 not in labels
    assert labels[199 * 150] is False
