"""Focused tests for live YOLO-to-assist tracking integration."""

import queue
import threading
import time
from collections import deque
from types import SimpleNamespace

import assist
import detection


class _Config:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


def _tracking_app(**config):
    app = assist.App.__new__(assist.App)
    app.cfg = _Config(**config)
    app._mode = 1
    app._mode_epoch = 1
    app._aim_stick_backend = None
    app._steer_local = threading.local()
    app._steer_response = 1.0
    app._steer_response_n = 0
    app._steer_response_ads = 1.0
    app._steer_response_ads_n = 0
    app._state_lock = threading.RLock()
    app._tracker_lock = threading.Lock()
    app._track = deque(maxlen=32)
    app._tracker = detection.MotionTracker(dim=3)
    app._last_motion_seq = 0
    app._motion_track_id = 0
    app._motion_observed_at = 0.0
    app._motion_latency_s = 0.0
    app._motion_sample_seq = 0
    app._lead_log_at = 0.0
    app.dpi_scale = 1.0
    app._view_epoch = 0
    app._capture = None
    app._target_lock = None
    app.follow_lock = None
    app._center_follow = None
    app._center_hold = None
    app._center_hold_tomb = 0.0
    app._pick_at = None
    app._chase_ref = None
    return app


def _target(dx=15, dy=5, track_id=7, seq=1, predicted=False):
    return SimpleNamespace(
        dx=dx,
        dy=dy,
        bw=20,
        bh=40,
        hscore=0.8,
        sig=None,
        area=800,
        source="predicted" if predicted else "detect",
        predicted=predicted,
        seq=seq,
        observed_at=time.perf_counter(),
        track_id=track_id,
    )


def test_motion_history_deduplicates_and_separates_tracks():
    app = _tracking_app(motion_predict=True, motion_3d=True)
    first = _target(track_id=1, seq=10)
    assert app._record_motion_observation(first, 100, 100)
    assert not app._record_motion_observation(first, 100, 100)
    assert len(app._track) == 1

    second = _target(track_id=2, seq=11)
    assert app._record_motion_observation(second, 120, 100)
    assert len(app._track) == 1
    assert app._motion_track_id == 2

    predicted = _target(track_id=2, seq=12, predicted=True)
    assert not app._record_motion_observation(predicted, 130, 100)
    assert len(app._track) == 1


def test_unsolicited_center_follow_is_retired():
    app = _tracking_app(game_mode=True)
    app._warp = lambda *_args: (_ for _ in ()).throw(
        AssertionError("background centre follow must never move"))
    assert not app._center_follow_cycle()


def test_passive_motion_sampling_records_fast_measurements_without_steering():
    app = _tracking_app(game_mode=True, motion_predict=True, radius=40)
    captured_at = time.perf_counter() - 0.010
    det = detection.Detection(
        520, 500, 20, 40, 0.8, source="detect", seq=3,
        captured_at=captured_at, track_id=7)
    app._yolo26n = SimpleNamespace(
        get_latest_detection=lambda **_kwargs: det)
    app._center_box = lambda: (500, 500, 40)
    app._steer_view = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("passive sampling must never steer"))

    assert app._sample_yolo_motion()
    assert app._motion_sample_seq == 3
    assert len(app._track) == 1
    assert app._track[0][1:] == (520.0, 500.0)


def test_camera_compensation_invalidates_motion_and_backend():
    app = _tracking_app(motion_predict=True, motion_3d=True,
                        motion_camera_comp=True)
    app._view_moved = False
    app._view_epoch = 0
    app._track.append((time.perf_counter(), 100.0, 100.0))
    notified = []
    app._yolo26n = SimpleNamespace(
        notify_view_moved=lambda: notified.append(True))

    app._mark_view_moved()

    assert app._view_moved
    assert app._view_epoch == 1
    assert list(app._track) == []
    assert notified == [True]


def test_steer_view_sends_raw_delta_to_virtual_stick():
    app = _tracking_app(unibot_aim=False)
    sent = []
    marked = []
    app._right_stick_aim = (
        lambda dx, dy, **_kwargs: sent.append((dx, dy)) or True)
    app._mark_view_moved = lambda: marked.append(True)
    app._nudge = lambda *_args, **_kwargs: False

    assert app._steer_view(7, -3)
    assert sent == [(7, -3)]
    assert marked == [True]


def test_hold_steering_applies_gain_but_single_click_does_not():
    app = _tracking_app(aim_speed=1.0, aim_y_speed_multiplier=1.0,
                        center_max_step=30, radius=54)
    sent = []
    app._input = SimpleNamespace(
        nudge=lambda dx, dy: sent.append((dx, dy)) or True)
    app._mark_view_moved = lambda: None
    app._nudge_fail_log_at = 0.0

    # Gain is now a speed budget: at 60 Hz gain 1.0 allows about 100 px per
    # frame per axis, so a 30 px error closes in one frame.
    assert app._nudge(30, 0, view=True, hold_gain=True)
    assert sent == [(30, 0)]

    sent.clear()
    assert app._nudge(10, 10, view=True)
    assert sent == [(10, 10)]


def test_hold_gain_above_one_is_capped_at_measured_error():
    app = _tracking_app(aim_speed=2.0, aim_y_speed_multiplier=2.0,
                        center_max_step=60, radius=54)
    assert app._scaled_view_delta(20, 10) == (20, 10)
    assert app._scaled_view_delta(10, 10) == (10, 10)


def test_ads_compensation_is_capped_at_measured_error():
    app = _tracking_app(aim_speed=1.0, aim_y_speed_multiplier=1.0,
                        center_max_step=80, radius=54)
    app._rbtn_down = True
    app.cfg.values["ads_steer_multiplier"] = 0.0
    # Gain is a speed limit: at 60 Hz gain 1.0 allows ~50 px, so a 20 px
    # error is closed in one frame regardless of ADS.  ADS only changes the
    # learned response.
    assert app._scaled_view_delta(20, 0) == (20, 0)
    app.cfg.values["aim_speed"] = 0.25
    # 6000 px/s budget * 0.25 / 120 Hz = 12.5 px -> int truncates to 12 counts.
    assert app._scaled_view_delta(20, 0) == (12, 0)
    app.cfg.values["aim_speed"] = 2.0
    # 6000 px/s * 2.0 / 120 Hz = 100 px > 20 px error -> closes in one frame.
    assert app._scaled_view_delta(20, 0) == (20, 0)


def test_failed_guide_nudge_is_not_counted_or_learned():
    app = _tracking_app(game_mode=True, motion_predict=False,
                        motion_3d=False)
    target = _target(dx=20, dy=0)
    app._mode_epoch = 1
    app.scan_near = lambda *_args, **_kwargs: target
    app._center_box = lambda: (100, 100, 54)
    app._reticle_active = lambda: True
    app._apply_snap_offset = lambda x, y: (x, y)
    app._max_step_cfg = lambda: 54
    app.radius_px = lambda: 54
    app._steer_view = lambda *_args, **_kwargs: False
    app._follow_may_learn = lambda _target: True
    stored = []
    app.follow = SimpleNamespace(store=lambda *args: stored.append(args))
    app.snap_count = 0
    app.pass_count = 0
    app.last_snap_time = 0.0
    app.last_snap_pos = None
    out = {}
    state = {"abandoned": False, "epoch": 1}

    app.do_guide(100, 100, out, state)

    assert out["handled"]
    assert app.snap_count == 0
    assert stored == []
    assert app.last_snap_time == 0.0


def _classic_test_app(mode):
    app = _tracking_app(mode=mode, game_mode=True, motion_predict=False,
                        motion_3d=False, center_max_step=54, radius=54)
    app._mode_epoch = 1
    app._center_box = lambda: (100, 100, 54)
    app._reticle_active = lambda: True
    app._follow_may_learn = lambda _target: False
    app.follow = SimpleNamespace(store=lambda *_args: None)
    app._lbtn_down = False
    app.snap_count = 0
    app.pass_count = 0
    app.last_snap_time = 0.0
    app.last_snap_pos = None
    app.reinject_click = lambda *_args: (_ for _ in ()).throw(
        AssertionError("a detected classic target must not be reinjected"))
    return app


def test_mode1_classic_snaps_from_first_measured_target():
    app = _classic_test_app(1)
    target = _target(dx=20, dy=0, seq=1)
    scans = []

    def scan(*_args, **_kwargs):
        target.observed_at = time.perf_counter()
        scans.append(target.dx)
        return target

    app.scan_near = scan
    steering = []
    app._steer_view = lambda dx, dy, *_args: steering.append((dx, dy)) or True
    clicks = []
    app._click_at_reticle = lambda: clicks.append(True)

    out = {}
    state = {"abandoned": False, "delivered": False,
             "lock": threading.Lock(), "epoch": 1, "mode": 1}
    app.do_classic(100, 100, out, state)

    assert out["handled"]
    assert scans == [20]
    assert steering == [(20, 0)]
    assert clicks == [True]
    assert state["delivered"]
    assert app.snap_count == 1
    assert app.pass_count == 0
    assert app.last_snap_pos == (100, 100)
    assert app.follow_lock is None
    assert app._center_follow is None


def test_mode1_classic_does_not_wait_for_camera_response():
    app = _classic_test_app(1)
    target = _target(dx=20, dy=0, seq=1)
    index = [0]

    def scan(*_args, **_kwargs):
        target.observed_at = time.perf_counter()
        index[0] += 1
        return target

    app.scan_near = scan
    steering = []
    app._steer_view = lambda dx, dy, *_args: steering.append((dx, dy)) or True
    clicks = []
    app._click_at_reticle = lambda: clicks.append(True)

    out = {}
    state = {"abandoned": False, "delivered": False,
             "lock": threading.Lock(), "epoch": 1, "mode": 1}
    app.do_classic(100, 100, out, state)

    # One scan, one nudge, one click: no exact-centre confirmation loop.
    assert steering == [(20, 0)]
    assert clicks == [True]
    assert index[0] == 1


def test_mode1_classic_single_correction_is_immediate():
    app = _classic_test_app(1)
    target = _target(dx=20, dy=0, seq=1)
    index = [0]

    def scan(*_args, **_kwargs):
        target.observed_at = time.perf_counter()
        index[0] += 1
        return target

    app.scan_near = scan
    steering = []
    app._steer_view = lambda dx, dy, *_args: steering.append((dx, dy)) or True
    clicks = []
    app._click_at_reticle = lambda: clicks.append(True)

    out = {}
    state = {"abandoned": False, "delivered": False,
             "lock": threading.Lock(), "epoch": 1, "mode": 1}
    app.do_classic(100, 100, out, state)

    assert steering == [(20, 0)]
    assert clicks == [True]
    assert index[0] == 1


def test_mode1_centered_initial_result_must_be_newer_than_press():
    app = _classic_test_app(1)
    pressed_at = time.perf_counter()
    stale = _target(dx=0, dy=0, seq=1)
    stale.observed_at = pressed_at - 0.010
    fresh = _target(dx=0, dy=0, seq=2)
    fresh.observed_at = pressed_at + 0.010
    targets = iter((stale, fresh))
    app.scan_near = lambda *_args, **_kwargs: next(targets)
    app._steer_view = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("centred geometry must not steer"))
    clicks = []
    app._click_at_reticle = lambda: clicks.append(True)

    out = {}
    state = {"abandoned": False, "delivered": False,
             "lock": threading.Lock(), "epoch": 1, "mode": 1,
             "pressed_at": pressed_at}
    app.do_classic(100, 100, out, state)

    assert out["handled"]
    assert clicks == [True]


def test_mode1_does_not_steer_from_pre_press_async_frame(monkeypatch):
    app = _classic_test_app(1)
    pressed_at = time.perf_counter()
    stale = _target(dx=20, dy=0, seq=1)
    stale.observed_at = pressed_at - 0.010
    fresh = _target(dx=8, dy=0, seq=2)
    fresh.observed_at = pressed_at + 0.010
    centered = _target(dx=0, dy=0, seq=3)
    centered.observed_at = pressed_at + 0.020
    targets = iter((stale, fresh, centered))
    app.scan_near = lambda *_args, **_kwargs: next(targets)
    steering = []
    app._steer_view = lambda dx, dy, *_args: steering.append((dx, dy)) or True
    clicks = []
    app._click_at_reticle = lambda: clicks.append(True)
    monkeypatch.setattr(assist, "CLASSIC_LOCK_RESPONSE_MIN_SECONDS", 0.0)
    monkeypatch.setattr(assist, "CLASSIC_LOCK_POLL_SECONDS", 0.0)

    out = {}
    state = {"abandoned": False, "delivered": False,
             "lock": threading.Lock(), "epoch": 1, "mode": 1,
             "pressed_at": pressed_at}
    app.do_classic(100, 100, out, state)

    assert steering == [(8, 0)]
    assert clicks == [True]


def test_mode1_fresh_sample_rejects_frame_captured_before_command():
    command_at = time.perf_counter()
    previous = _target(dx=20, seq=1)
    previous.observed_at = command_at - 0.020
    buffered = _target(dx=0, seq=2)
    buffered.observed_at = command_at - 0.001
    post_command = _target(dx=0, seq=3)
    post_command.observed_at = command_at + 0.001

    assert not assist.App._fresh_target_sample(
        buffered, previous, command_at)
    assert assist.App._fresh_target_sample(
        post_command, previous, command_at)


def test_mode1_ignores_steering_gain():
    app = _classic_test_app(1)
    app.cfg.values["aim_speed"] = 2.0
    app.cfg.values["aim_y_speed_multiplier"] = 2.0
    target = _target(dx=20, dy=0, seq=1)
    app.scan_near = lambda *_args, **_kwargs: target
    steering = []
    app._steer_view = lambda dx, dy, *_args: steering.append((dx, dy)) or True
    app._click_at_reticle = lambda: None

    out = {}
    state = {"abandoned": False, "delivered": False,
             "lock": threading.Lock(), "epoch": 1, "mode": 1}
    app.do_classic(100, 100, out, state)
    # Gain must not multiply a Mode-1 correction.
    assert steering == [(20, 0)]

    steering.clear()
    app._rbtn_down = True
    state = {"abandoned": False, "delivered": False,
             "lock": threading.Lock(), "epoch": 1, "mode": 1}
    app.do_classic(100, 100, {}, state)
    # The initial Mode-1 snap is a full correction.
    assert steering == [(20, 0)]


def test_mode1_classic_clicks_without_waiting_for_lock_timeout():
    app = _classic_test_app(1)
    app.scan_near = lambda *_args, **_kwargs: _target(dx=20, dy=0, seq=1)
    app._steer_view = lambda *_args, **_kwargs: True
    clicks = []
    app._click_at_reticle = lambda: clicks.append(True)

    out = {}
    state = {"abandoned": False, "delivered": False,
             "lock": threading.Lock(), "epoch": 1, "mode": 1}
    app.do_classic(100, 100, out, state)

    assert out["handled"]
    assert clicks == [True]
    assert state["delivered"]
    assert app.snap_count == 1
    assert app.pass_count == 0


def test_mode1_no_target_never_clicks_or_returns_the_trigger():
    app = _classic_test_app(1)
    app.scan_near = lambda *_args, **_kwargs: None
    clicks = []
    app._click_at_reticle = lambda: clicks.append(True)

    out = {}
    state = {"abandoned": False, "delivered": False,
             "lock": threading.Lock(), "epoch": 1, "mode": 1}
    app.do_classic(100, 100, out, state)

    assert out["handled"]
    assert clicks == []
    assert state["delivered"]


def test_mode1_block_stamp_survives_live_reticle_state_change():
    app = _classic_test_app(1)
    app.scan_near = lambda *_args, **_kwargs: _target(dx=12, dy=0)
    app._center_box = lambda: None
    app._reticle_active = lambda: False

    out = {}
    state = {"abandoned": False, "delivered": False,
             "lock": threading.Lock(), "epoch": 1, "mode": 1,
             "block_unconfirmed": True}
    app.do_classic(100, 100, out, state)

    # The click began as an in-game fire transaction. Opening a menu or
    # switching live UI state while it is queued must not return it raw.
    assert out["handled"]
    assert state["delivered"]


def test_teardown_drops_unconfirmed_mode1_job_instead_of_reinjecting():
    app = _tracking_app()
    app.click_q = queue.Queue()
    app._pending_lock = threading.Lock()
    app.pending_classics = []
    returned = []
    app.reinject_click = lambda x, y: returned.append((x, y))
    entry = {"x": 10, "y": 20, "up": False, "state": None,
             "mode": 1, "block_unconfirmed": True}
    app.pending_classics.append(entry)
    app.click_q.put(("classic", 10, 20, time.perf_counter(), entry))

    app._drain_queue()

    assert returned == []
    assert app.pending_classics == []
    assert app.click_q.empty()


def test_emergency_release_keeps_unconfirmed_mode1_trigger_blocked():
    app = _tracking_app()
    app._pending_lock = threading.Lock()
    app.pending_classics = []
    app.worker_active = False
    app._center_hold = None
    returned = []
    app.reinject_click = lambda x, y: returned.append((x, y))
    entry = {"x": 10, "y": 20, "up": False, "state": None,
             "mode": 1, "block_unconfirmed": True}
    app.pending_classics.append(entry)

    app._emergency_release()

    assert returned == []
    assert app.pending_classics == []


def test_clear_trigger_state_preserves_matching_mode1_up_ledger():
    app = _tracking_app()
    app._trigger_lock = threading.Lock()
    app._trigger_hold_timer = None
    app._trigger_suppressed_down = {"x": 1}
    app._trigger_hold_done = True
    marker = {"mode": 1, "block_unconfirmed": True}
    app._blocked_trigger_ups = [marker]

    app._clear_trigger_state()

    assert app._blocked_trigger_ups == [marker]
    assert app._trigger_suppressed_down is None
    assert not app._trigger_hold_done


def test_mode_switch_invalidates_worker_before_queue_teardown():
    app = _tracking_app()
    app._mode = 1
    app._mode_epoch = 7
    observed = []
    app._drain_queue = lambda: observed.append(
        (app._mode_epoch, app.current_mode()))
    app._emergency_release = lambda: None
    app._pad_release_all = lambda: None
    app._clear_trigger_state = lambda: None
    app.stop_playback = lambda: None
    app._reset_motion_history = lambda: None
    app.subject = SimpleNamespace(clear=lambda: None)
    app.follow_lock = None
    app._center_follow = None
    app._center_hold_tomb = 0.0
    app._pick_at = None
    app._chase_ref = None
    app.pause_until = 1.0

    app._force_mode(2)

    assert observed == [(8, 2)]
    assert app.pause_until == 0.0


def _hold_test_app():
    app = _tracking_app(game_mode=True, motion_predict=False,
                        motion_3d=False, center_max_step=54,
                        radius=54, subject_priority=False)
    app.stop = threading.Event()
    app.running = True
    app.pause_until = 0.0
    app._lbtn_down = True
    app._pad_click_down = False
    app._pad_raw_down = False
    app._center_box = lambda: (100, 100, 54)
    app._reticle_active = lambda: True
    app.radius_px = lambda: 54
    app._input = SimpleNamespace(tap_interval=0)
    app._center_hold = app._new_hold_session()
    return app


def test_center_hold_stops_after_centering_and_does_not_carry_ema():
    app = _hold_test_app()
    current = [_target(dx=15, dy=0, seq=1)]
    app._scan_center = lambda *_args, **_kwargs: (current[0], 100, 100)
    commands = []
    app._steer_view = (
        lambda dx, dy, **kwargs:
        commands.append((dx, dy, kwargs.get("hold"))) or True)

    assert app._center_hold_cycle()
    assert commands == [(15, 0, True)]

    current[0] = _target(dx=0, dy=0, seq=2)
    assert app._center_hold_cycle()
    assert commands == [(15, 0, True)]

    # A sub-pixel wobble rounds to the same centre pixel: no reversal.
    current[0] = _target(dx=-0.4, dy=0.3, seq=3)
    assert app._center_hold_cycle()
    assert commands == [(15, 0, True)]

    current[0] = _target(dx=4, dy=0, seq=4)
    assert app._center_hold_cycle()
    assert commands == [(15, 0, True)]

    current[0] = _target(dx=6, dy=0, seq=5)
    assert app._center_hold_cycle()
    assert commands == [(15, 0, True), (6, 0, True)]


def test_continuous_guide_does_not_replay_a_held_correction():
    app = _tracking_app(game_mode=True, motion_predict=False,
                        motion_3d=False, center_max_step=54,
                        radius=54, subject_priority=False)
    app._center_box = lambda: (100, 100, 54)
    app._reticle_active = lambda: True
    app.radius_px = lambda: 54
    app._apply_snap_offset = lambda x, y: (x, y)
    app._follow_may_learn = lambda _target: False
    app.follow = SimpleNamespace(store=lambda *_args: None)
    app.snap_count = 0
    app.pass_count = 0
    app.last_snap_time = 0.0
    app.last_snap_pos = None
    target = _target(dx=20, dy=0, seq=1)
    app.scan_near = lambda *_args, **_kwargs: target
    commands = []
    app._steer_view = (
        lambda dx, dy, **kwargs:
        commands.append((dx, dy, kwargs.get("hold"))) or True)
    guidance = {}
    state = {"abandoned": False, "epoch": 1, "continuous": True,
             "guidance": guidance}

    out = {}
    app.do_guide(100, 100, out, state)
    assert commands == [(20, 0, True)]

    # The same captured frame cannot prove the turn was applied.
    out = {}
    app.do_guide(100, 100, out, state)
    assert commands == [(20, 0, True)]
    assert out["handled"]
    assert guidance["last_seq"] == 1


def test_continuous_guide_sends_nothing_for_stationary_centered_target():
    app = _tracking_app(game_mode=True, motion_predict=False,
                        motion_3d=False)
    app.scan_near = lambda *_args, **_kwargs: _target(dx=2, dy=2, seq=2)
    app._center_box = lambda: (100, 100, 54)
    app._reticle_active = lambda: True
    app._steer_view = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("a centred held target must not steer"))
    app.pass_count = 0
    guidance = {"cmd_pending": True}
    state = {"abandoned": False, "epoch": 1, "continuous": True,
             "guidance": guidance}

    out = {}
    app.do_guide(100, 100, out, state)

    assert out["handled"]
    assert app.pass_count == 0
    assert not guidance["cmd_pending"]


def test_continuous_guide_uses_radial_five_pixel_deadband():
    app = _tracking_app(game_mode=True, motion_predict=False,
                        motion_3d=False)
    app.scan_near = lambda *_args, **_kwargs: _target(dx=3, dy=3, seq=2)
    app._center_box = lambda: (100, 100, 54)
    app._reticle_active = lambda: True
    commands = []
    app._steer_view = lambda dx, dy, **kwargs: (
        commands.append((dx, dy, kwargs.get("hold"))) or True)
    app.pass_count = 0
    app.snap_count = 0
    app.last_snap_pos = None
    app.last_snap_time = 0.0
    app._follow_may_learn = lambda _target: False
    state = {"abandoned": False, "epoch": 1, "continuous": True,
             "guidance": {}}

    out = {}
    app.do_guide(100, 100, out, state)

    assert out["handled"]
    assert commands == []

    # Beyond the deadband the held loop steers toward the target centre.
    app.scan_near = lambda *_args, **_kwargs: _target(dx=15, dy=0, seq=3)
    app.do_guide(100, 100, {}, state)
    assert commands == [(15, 0, True)]


def test_continuous_guide_center_hysteresis_ignores_body_wobble():
    app = _tracking_app(game_mode=True, motion_predict=False,
                        motion_3d=False)
    current = [_target(dx=2, dy=0, seq=1)]
    app.scan_near = lambda *_args, **_kwargs: current[0]
    app._center_box = lambda: (100, 100, 54)
    app._reticle_active = lambda: True
    commands = []
    app._steer_view = lambda dx, dy, **kwargs: (
        commands.append((dx, dy, kwargs.get("hold"))) or True)
    app.pass_count = 0
    app.snap_count = 0
    app.last_snap_pos = None
    app.last_snap_time = 0.0
    app._follow_may_learn = lambda _target: False
    guidance = {}
    state = {"abandoned": False, "epoch": 1, "continuous": True,
             "guidance": guidance}

    app.do_guide(100, 100, {}, state)
    assert guidance["center_latched"]
    assert commands == []

    current[0] = _target(dx=4, dy=0, seq=2)
    app.do_guide(100, 100, {}, state)
    assert commands == []

    current[0] = _target(dx=12, dy=0, seq=3)
    app.do_guide(100, 100, {}, state)
    assert commands == [(12, 0, True)]


def test_continuous_guide_never_steers_from_predicted_bridge_frame():
    app = _tracking_app(game_mode=True, motion_predict=True,
                        motion_3d=True)
    app.scan_near = lambda *_args, **_kwargs: _target(
        dx=12, dy=0, seq=2, predicted=True)
    app._center_box = lambda: (100, 100, 54)
    app._reticle_active = lambda: True
    app._steer_view = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("predicted bridge geometry must not steer"))
    state = {"abandoned": False, "epoch": 1, "continuous": True,
             "guidance": {"cmd_pending": True}}

    out = {}
    app.do_guide(100, 100, out, state)

    assert out["handled"]
    assert not state["guidance"]["cmd_pending"]


def test_mode2_short_click_keeps_single_nudge_behavior(monkeypatch):
    app = _classic_test_app(2)
    scans = []

    def scan(*_args, **_kwargs):
        if scans:
            raise AssertionError("mode 2 short click must not enter mode-1 lock")
        scans.append(True)
        return _target(dx=20, dy=0)

    app.scan_near = scan
    steering = []
    app._steer_view = lambda dx, dy, *_args: steering.append((dx, dy)) or True
    clicks = []
    app._click_at_reticle = lambda: clicks.append(True)
    monkeypatch.setattr(assist.time, "sleep", lambda _seconds: None)

    out = {}
    state = {"abandoned": False, "delivered": False,
             "lock": threading.Lock(), "epoch": 1, "mode": 2}
    app.do_classic(100, 100, out, state)

    assert out["handled"]
    assert scans == [True]
    assert steering == [(20, 0)]
    assert clicks == [True]
    assert app.snap_count == 1


def test_mode2_already_centered_click_does_not_send_zero_steering():
    app = _classic_test_app(2)
    app.scan_near = lambda *_args, **_kwargs: _target(dx=0, dy=0)
    app._steer_view = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("an already-centred click must not steer"))
    clicks = []
    app._click_at_reticle = lambda: clicks.append(True)

    out = {}
    state = {"abandoned": False, "delivered": False,
             "lock": threading.Lock(), "epoch": 1, "mode": 2}
    app.do_classic(100, 100, out, state)

    assert out["handled"]
    assert clicks == [True]


def test_emulated_pad_is_not_mistaken_for_right_stick_aim(monkeypatch):
    app = _tracking_app(game_mode=True)
    app._aim_stick_lock = threading.Lock()
    app._aim_stick_generation = 0
    app._aim_stick_timer = None
    app._pad_out = SimpleNamespace(
        name="emulated", ok_for_pad=lambda: True)
    monkeypatch.setattr(assist.gamepad, "_get_vigem_steer",
                        lambda *_args: False)

    assert not app._right_stick_aim(5, 2)


def test_yolo_gate_uses_gui_radius_exactly_without_dpi_scaling():
    app = _tracking_app(game_mode=True, radius=40,
                        debug_visualize=False, follow_enabled=False)
    app.dpi_scale = 1.35
    assert app.radius_px() == 40
    app._virtual_screen = lambda: (0, 0, 1000, 1000)
    assert app._center_box() == (500, 500, 40)

    center_x = center_y = 500
    outside = detection.Detection(
        center_x + 52, center_y, 20, 40, 0.8,
        seq=3, t=time.perf_counter(), source="detect",
        track_id=9, quality=10.0,
    )
    inside = detection.Detection(
        center_x + 39, center_y, 20, 40, 0.8,
        seq=4, t=time.perf_counter(), source="detect",
        track_id=9, quality=10.0,
    )
    latest = [outside]
    app._center_box = lambda: (center_x, center_y, 40)
    app._reticle_active = lambda: True
    app._yolo_local = threading.local()
    app._yolo_log_at = time.perf_counter()
    app._yolo26n = SimpleNamespace(
        get_latest_detection=lambda **_kwargs: latest[0])
    app._record_motion_observation = lambda *_args: False
    app._debug_vis = None

    assert app._scan_center(None) is None
    assert app._yolo26n.search_radius == 40

    latest[0] = inside
    got = app._scan_center(None)
    assert got is not None
    assert got[0].dx == 39


def test_pose_work_requires_fast_detection_and_budget():
    app = _tracking_app(hpose_mode=True, scan_budget_ms=20, radius=40)
    backend = SimpleNamespace(
        search_radius=54,
        cfg=SimpleNamespace(capture_radius=54),
        _appearance=SimpleNamespace(search_radius=54),
        _stats={"avg_infer_ms": 8.0},
    )

    app._configure_yolo_backend(backend)
    measured = _target()
    measured.source = "detect"

    assert backend._pose_submission_interval([]) == float("inf")
    assert backend._pose_submission_interval([measured]) == 1.0
    backend._stats["avg_infer_ms"] = 25.0
    assert backend._pose_submission_interval([measured]) == float("inf")
    app.cfg.values["hpose_mode"] = False
    assert backend._pose_submission_interval([measured]) == float("inf")


def test_pose_result_obeys_visible_confidence_and_aim_settings():
    app = _tracking_app(radius=54, hpose_mode=True,
                        hpose_min_conf=50, hpose_aim="head")
    pose = detection.Detection(
        100, 100, 20, 40, 0.40, source="pose", pose_conf=0.40)
    assert app._yolo26n_to_scan_result(pose, 100, 100) is None

    pose.conf = pose.pose_conf = 0.80
    result = app._yolo26n_to_scan_result(pose, 100, 100)
    assert result is not None
    assert result.target.dx == 0
    assert abs(result.target.dy + 12.8) < 1e-6

    app.cfg.values["hpose_mode"] = False
    assert app._yolo26n_to_scan_result(pose, 100, 100) is None


def test_motion_prediction_leads_consistent_motion_through_latency():
    app = _tracking_app(motion_predict=True, radius=40)
    app._motion_latency_s = 0.040
    app._track.extend([
        (9.94, 100.0, 100.0),
        (9.96, 102.0, 100.0),
        (9.98, 104.0, 100.0),
        (10.00, 106.0, 100.0),
    ])

    ox, oy, direction = app._motion_prediction(now=10.00)

    assert 5.0 <= ox <= 7.0
    assert abs(oy) < 0.01
    assert direction == "right"


def test_motion_prediction_rejects_stationary_body_wobble():
    app = _tracking_app(motion_predict=True, radius=40)
    app._track.extend([
        (9.94, 100.0, 100.0),
        (9.96, 102.0, 99.0),
        (9.98, 99.0, 101.0),
        (10.00, 101.0, 100.0),
    ])

    assert app._motion_prediction(now=10.00) == (0.0, 0.0, "")


def test_motion_prediction_drops_old_velocity_as_soon_as_target_stops():
    app = _tracking_app(motion_predict=True, radius=40)
    app._track.extend([
        (9.88, 100.0, 100.0),
        (9.90, 103.0, 100.0),
        (9.92, 106.0, 100.0),
        (9.94, 109.0, 100.0),
        (9.96, 109.0, 100.0),
        (9.98, 109.0, 100.0),
        (10.00, 109.0, 100.0),
    ])

    assert app._motion_prediction(now=10.00) == (0.0, 0.0, "")


def test_motion_prediction_does_not_carry_old_direction_through_reversal():
    app = _tracking_app(motion_predict=True, radius=40)
    app._track.extend([
        (9.88, 100.0, 100.0),
        (9.90, 103.0, 100.0),
        (9.92, 106.0, 100.0),
        (9.94, 109.0, 100.0),
        (9.96, 106.0, 100.0),
        (9.98, 103.0, 100.0),
        (10.00, 100.0, 100.0),
    ])

    ox, _oy, _direction = app._motion_prediction(now=10.00)
    assert ox <= 0.0


def test_motion_tracker_hard_reset_forgets_previous_velocity():
    tracker = detection.MotionTracker(dim=3)
    assert tracker.update(1.0, (0.0, 0.0, 10.0))
    assert tracker.update(1.1, (10.0, 0.0, 10.0))
    assert tracker.speed() > 0.0

    tracker.reset(clear_history=True)
    assert tracker.update(1.2, (20.0, 0.0, 10.0))
    assert tracker.updates == 1
    assert tracker.speed() == 0.0


def test_steering_response_calibrates_mouse_counts():
    app = _tracking_app(aim_speed=1.0, aim_y_speed_multiplier=1.0,
                        center_max_step=80, radius=54)
    app._steer_response = 2.0
    # Speed budget ~23 px at gain 1.0; response 2 view px per count -> 10
    # counts for a 20 px error.
    assert app._scaled_view_delta(20, 0) == (10, 0)


def test_steer_response_learns_from_measured_correction():
    app = _tracking_app(aim_speed=1.0, aim_y_speed_multiplier=1.0,
                        center_max_step=80, radius=54)
    app._steer_response = 2.0
    app._steer_response_n = 0
    now = time.perf_counter()
    ledger = {
        "cmd_pending": True,
        "last_cmd_offset": (20.0, 0.0),
        "last_cmd_step": (10.0, 0.0),
        "last_cmd_seq": 1,
        "last_cmd_at": now - 0.020,
    }
    target = _target(dx=0, dy=0, seq=2)
    target.observed_at = now
    assert app._observe_steer_response(ledger, target, now)
    assert app._steer_response == 2.0


def test_ads_steer_setting_adds_percentage():
    app = _tracking_app(aim_speed=0.1, aim_y_speed_multiplier=1.0,
                        center_max_step=80, radius=54)
    app._rbtn_down = True
    app._steer_target_h = 20.0
    # Gain 0.1 gives ~5 px per 120 Hz frame; response 1 -> 5 counts.  ADS
    # changes the learned response, not the correction fraction.
    assert app._scaled_view_delta(20, 0) == (5, 0)
