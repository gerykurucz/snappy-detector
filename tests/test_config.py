"""Unit tests for config parsing and coercion."""

import sys
import os

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


def test_defaults_contain_reticle_keys():
    assert "reticle_x" in DEFAULT_CONFIG
    assert "reticle_y" in DEFAULT_CONFIG
    assert "timer_resolution_us" in DEFAULT_CONFIG
