"""Unit tests for config parsing and coercion."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import DEFAULT_CONFIG, _coerce_value


def test_coerce_int():
    ok, val = _coerce_value(5, 0)
    assert ok and val == 5
    ok, val = _coerce_value("42", 0)
    assert ok and val == 42


def test_coerce_bool():
    ok, val = _coerce_value(True, False)
    assert ok and val is True
    ok, val = _coerce_value(1, False)
    assert ok and val is True
    ok, val = _coerce_value(0, False)
    assert ok and val is False


def test_coerce_map_string_passthrough():
    ok, val = _coerce_value("cross", "cross")
    assert ok and val == "cross"


def test_defaults_match_editable_gui_controls():
    import gui

    # META covers the normal tab renderer; theme and timer resolution are
    # deliberately special controls mounted outside it.
    expected = set(gui.META) | {"theme", "timer_resolution_us"}
    assert set(DEFAULT_CONFIG) == expected

    retired = {
        "aim_stick_sensitivity", "blob_watch", "center_follow",
        "config_version", "contrast_threshold", "controller_input",
        "glide_ms", "hpose_lead_ms", "hpose_model", "hpose_stance",
        "hpose_view", "language", "max_area", "max_dim", "min_area",
        "min_contrast", "mode", "motion_lead_ms", "motion_priority",
        "on_target_dist", "pad_deadzone", "pad_drive", "pad_emulate",
        "pad_index", "pad_poll_hz", "pad_rumble", "pad_sensitivity",
        "pad_trigger_threshold", "reticle_mode", "reticle_x",
        "reticle_y", "snap_offset_x", "snap_offset_y",
        "stall_pause_seconds", "watch_center_only", "yolo26n_conf",
        "yolo26n_enabled", "yolo26n_fallback_to_hpose",
        "yolo26n_input_size", "yolo26n_model",
    }
    assert retired.isdisjoint(DEFAULT_CONFIG)


def test_bundled_profiles_only_contain_editable_settings():
    root = os.path.join(os.path.dirname(__file__), "..", "configs")
    for filename in ("config.json", "fortnite.json"):
        with open(os.path.join(root, filename), "r", encoding="utf-8") as fh:
            profile = json.load(fh)
        assert set(profile).issubset(DEFAULT_CONFIG), filename
