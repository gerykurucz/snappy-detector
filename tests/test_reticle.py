"""Unit tests for the reticle module.

These run head-less (no display required) and cover colour parsing, geometry
bounds and the shape rendering helpers.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import reticle


def test_parse_color_defaults_to_green():
    assert reticle.parse_color(None) == (0, 255, 0)
    assert reticle.parse_color("") == (0, 255, 0)
    assert reticle.parse_color("garbage") == (0, 255, 0)


def test_parse_color_rgb():
    assert reticle.parse_color("0,255,0") == (0, 255, 0)
    assert reticle.parse_color("255, 0, 0") == (255, 0, 0)
    assert reticle.parse_color("300,-5,20") == (255, 0, 20)  # clamped


def test_parse_color_hex():
    assert reticle.parse_color("#ff0000") == (255, 0, 0)
    assert reticle.parse_color("#00ff00") == (0, 255, 0)
    assert reticle.parse_color("#0000ff") == (0, 0, 255)


def test_to_hex():
    assert reticle.to_hex((255, 0, 0)) == "#ff0000"
    assert reticle.to_hex((0, 255, 0)) == "#00ff00"
    assert reticle.to_hex((0, 0, 255)) == "#0000ff"


def test_shape_geometry_known_shapes():
    for shape in ("cross", "plus", "dot", "circle", "x"):
        geo = reticle.shape_geometry(shape, 24, 6, 2, 0)
        assert "lines" in geo and "ovals" in geo


def test_shape_bounds_positive():
    for shape in ("cross", "plus", "dot", "circle", "x"):
        b = reticle.shape_bounds(shape, 24, 6, 2, 0)
        assert isinstance(b, int) and b > 0


def test_rotation_range_clamped():
    geo = reticle.shape_geometry("cross", 24, 6, 2, 200)
    assert geo["lines"]


def test_invalid_shape_falls_back_to_lines():
    geo = reticle.shape_geometry("not-a-shape", 24, 6, 2, 0)
    # Unknown shape defaults to cross-like axes
    assert "lines" in geo
