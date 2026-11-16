"""Regression tests for screen-space targeting and the colour lock."""

import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import targeting


def _colour_frame(size=(120, 160), colour=(40, 90, 180), box=None):
    """BGR frame with one filled rectangle colour box in screen pixels."""
    h, w = size
    img = np.zeros((h, w, 3), dtype=np.uint8)
    if box is None:
        bx, by, bw, bh = (w // 4, h // 4, w // 4, h // 2)
    else:
        bx, by, bw, bh = box
    x0 = max(0, min(w - 1, int(bx)))
    y0 = max(0, min(h - 1, int(by)))
    x1 = max(x0 + 1, min(w, int(bx + bw)))
    y1 = max(y0 + 1, min(h, int(by + bh)))
    img[y0:y1, x0:x1] = colour
    return img


def test_humanoid_gate_accepts_humans_rejects_blobs():
    assert targeting.humanoid_ok(20, 40)   # 1:2 human
    assert targeting.humanoid_ok(8, 40)    # 1:5 distant human
    assert not targeting.humanoid_ok(20, 20)  # crosshair/bullet
    assert not targeting.humanoid_ok(20, 10)  # wide HUD bar
    assert not targeting.humanoid_ok(6, 40)   # below 7 px floor
    assert targeting.humanoid_ratio(20, 40) == 2.0


def test_color_tracker_seed_rejects_non_humanoid_colour_blob():
    tracker = targeting.ColorTracker()
    img = _colour_frame(colour=(40, 90, 180), box=(60, 40, 10, 10))
    origin = (0, 0)
    now = time.perf_counter()
    assert not tracker.seed(img, origin, (60, 40, 10, 10),
                            model_confirmed=False, track_id=1)
    assert not tracker.seeded


def test_color_tracker_seeds_from_model_person_and_tracks():
    tracker = targeting.ColorTracker()
    img = _colour_frame(colour=(40, 90, 180), box=(60, 40, 20, 40))
    origin = (0, 0)
    now = time.perf_counter()
    assert tracker.seed(img, origin, (60, 40, 20, 40),
                        model_confirmed=True, track_id=1)
    assert tracker.seeded
    sample = tracker.update(img, origin, now=now)
    assert sample is not None
    assert abs(sample.x - 70) <= 3
    assert abs(sample.y - 60) <= 3


def test_color_tracker_loses_colour_when_it_changes():
    tracker = targeting.ColorTracker(max_misses=2)
    img = _colour_frame(colour=(40, 90, 180), box=(60, 40, 20, 40))
    origin = (0, 0)
    now = time.perf_counter()
    assert tracker.seed(img, origin, (60, 40, 20, 40),
                        model_confirmed=True, track_id=1)
    other = _colour_frame(colour=(200, 50, 50), box=(60, 40, 20, 40))
    # First miss still within grace; subsequent misses clear the lock.
    for _ in range(3):
        sample = tracker.update(other, origin, now=now + 0.01)
        now += 0.01
    assert sample is None
    assert not tracker.seeded


def test_colour_lead_is_zero_when_stationary():
    tracker = targeting.ColorTracker()
    img = _colour_frame(colour=(40, 90, 180), box=(60, 40, 20, 40))
    origin = (0, 0)
    now = time.perf_counter()
    assert tracker.seed(img, origin, (60, 40, 20, 40),
                        model_confirmed=True, track_id=1)
    # Record repeated identical centres directly for the lead path.
    for _ in range(5):
        tracker._history.append((now, 70.0, 60.0, 20.0, 40.0))
        now += 0.016
    lx, ly = tracker.lead(now)
    assert lx == 0.0 and ly == 0.0


def test_colour_lead_moves_with_consistent_translation():
    tracker = targeting.ColorTracker()
    img = _colour_frame(colour=(40, 90, 180), box=(60, 40, 20, 40))
    origin = (0, 0)
    now = time.perf_counter()
    assert tracker.seed(img, origin, (60, 40, 20, 40),
                        model_confirmed=True, track_id=1)
    # Walking right: three samples 30 px/s for 0.1 s pass the lead speed floor.
    # Target height is 40 px -> height / 2 = 20 px vertical boost.
    for i in range(3):
        tracker._history.append((now + i * 0.05, 70.0 + i * 3.0, 60.0,
                                 20.0, 40.0))
    lx, ly = tracker.lead(now + 0.15)
    assert lx > 0.0
    assert abs(ly - 20.0) < 1.0
