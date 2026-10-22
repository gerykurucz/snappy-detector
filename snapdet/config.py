"""Every knob the engine has, in one place.

The defaults are tuned for 1080p game/screen capture on a CPU with a
"few hundred milliseconds per frame" budget.  ``bench`` in the CLI
measures the actual per-pass cost on your machine and prints a
suggested budget.
"""

from __future__ import annotations


class Config(object):
    DEFAULTS = {
        # --- confidence floors, one per discovery path --------------
        # A full-frame pass sees the whole picture, so it can afford a
        # strict floor.  A zoomed window sees 1/500th of it, and the
        # measurement that found the window (a motion blob, a sweep
        # tile) is already evidence - so the floor drops.  The numbers
        # come from the false-positive baseline: empty windows never
        # scored above ~0.09 at any zoom on real scenery.
        "conf_coarse": 0.25,      # whole-frame letterbox pass
        "conf_sweep": 0.20,       # amortised zoom-tile sweep, zoom <= 8
        "conf_sweep_far": 0.12,   # sweep tiles at zoom > 8 (tiny game)
        "conf_inspect": 0.12,     # motion-gated window inspection
        "conf_floor": 0.05,       # what the network itself reports

        # --- temporal evidence memory ----------------------------------
        # A 5-10 px person scores 0.05-0.45 depending on the frame and
        # what stands behind them; no single-frame threshold survives
        # that.  Weak but *repeated* plausible evidence at a smoothly
        # moving location accumulates until it confirms - the same
        # target seen five frames running is real even when no single
        # frame believes it.
        "memory_enabled": True,
        "memory_confirm": 0.45,   # accumulated score that promotes
        "memory_min_hits": 3,     # plausible sightings needed
        "memory_best_hit": 0.10,  # at least one decent single sighting
        "memory_decay": 0.97,     # per-frame fade of old evidence
        "memory_max_age": 12,     # frames without a sighting
        "memory_gate": 10.0,      # px + 2x target size matching gate
        "memory_gate_max": 24.0,  # ...capped, or giant blobs match everything
        "memory_max_promote": 2,  # promotions per frame, cap on FPs

        # --- per-frame budget ---------------------------------------
        "pass_budget": 10,        # pose-model inferences per frame, max
        "inspect_budget": 4,      # of those, how many may be inspections

        # --- amortised zoom sweep ------------------------------------
        # Several zoom grids run round-robin, a few tiles per frame, so
        # that every pixel of the scene is re-examined at high zoom
        # every few seconds - the guarantee behind "even a stationary
        # 5-pixel person is eventually found".
        "sweep_enabled": True,
        "sweep_zooms": ((8, 3), (12, 1)),   # (zoom, tiles per frame)
        # circle mode adds a 16x grid: a 12-px body wants ~192 px in
        # the network input, and on a 140-px disc that means 16x
        "circle_sweep_zooms": ((8, 3), (12, 1), (16, 1)),
        "sweep_overlap": 0.25,

        # --- motion-gated discovery -----------------------------------
        "motion_enabled": True,
        "motion_scale": 0.5,       # analysis downscale
        "motion_threshold": 18,    # grey-level difference
        "motion_min_area": 2,      # px^2 at analysis scale
        "motion_max_area": 400,
        "motion_max_rois": 24,     # strongest N per frame
        "motion_dedup_radius": 16.0,

        # --- zoom inspection -----------------------------------------
        "inspect_target_px": 192.0,  # wanted person height in network input
        "inspect_zoom_min": 3,
        "inspect_zoom_max": 16,
        "inspect_escalate": True,    # retry at 1.5x zoom when weak

        # --- the brain ------------------------------------------------
        # Below this body height (px) the 17 keypoints live in a handful
        # of pixels and view/stance are noise; tracks then honestly say
        # "unknown" and rely on box + motion.
        "min_classify_height": 48.0,
        "use_face": True,
        "focal_ratio": 0.9,

        # --- centre-circle capture mode ---------------------------------
        # The caller captures only a disc at the exact screen centre
        # (the aim region).  Close people then arrive as FRAGMENTS -
        # head+shoulders, chest-only - with most of the body outside
        # the disc.  In this mode the engine crops the disc itself,
        # masks everything outside it, and adds "context passes" that
        # re-present the disc content flush against a canvas edge at
        # several scales, which is what makes border-truncated bodies
        # recognisable to a COCO-trained detector.
        "capture_mode": "frame",          # "frame" or "circle"
        "circle_radius": 80,              # px, intended range 50-100
        "circle_context_scales": (1.0, 0.6, 0.45),
        "conf_context": 0.28,             # context-pass candidate floor
        "conf_context_strong": 0.55,      # seeds a track in one frame
        "circle_face_threshold": 0.55,    # YuNet floor for face fallback
        "circle_motion_max_area": 12000,  # fragments make BIG blobs
        "circle_inspect_max_h": 48,       # blobs taller than this are
                                          # fragments: the context passes
                                          # own them, not blob inspection
        "circle_max_tracks": 2,           # the aim disc is an engagement
                                          # area: the strongest identities
        "pass_budget_circle": 14,

        # --- models ----------------------------------------------------
        "pose_model": None,        # defaults to yolo11n-pose.onnx
        "imgsz": 640,
        "download": True,
        "ort_threads": 0,          # 0 = onnxruntime default (all cores)
        "device": "cpu",           # cpu | cuda | directml

        # --- tracker ----------------------------------------------------
        "track_max_age": 20,       # frames a lost track coasts
        "track_low_score": 0.35,
    }

    def __init__(self, **overrides):
        for k, v in self.DEFAULTS.items():
            setattr(self, k, v)
        for k, v in overrides.items():
            if k not in self.DEFAULTS:
                raise KeyError("unknown config key %r" % (k,))
            setattr(self, k, v)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.DEFAULTS}

    def __repr__(self) -> str:
        return "Config(%s)" % (", ".join("%s=%r" % kv
                                         for kv in sorted(self.as_dict().items())))
