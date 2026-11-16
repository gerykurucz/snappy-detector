"""Assistance engine: the low-level mouse hook, the click workers for
mode 1 (classic) and mode 2 (guide), hotkey actions, cursor glide,
follow-memory gating, and start_app() which wires the threads up.

Only the left mouse button is ever assisted; every other button passes
straight through to Windows. The right button can additionally fire the
programmable mouse2 side actions - a short press fires mouse2_action, a
hold of at least MOUSE2_HOLD_SECONDS fires mouse2_hold_action instead -
always alongside, never suppressed."""

import math
import os
import queue
import secrets
import threading
import time
from collections import deque

import numpy as np

import input as gamepad
import input as hotkeys
import detection
import online
import targeting
from system import IS_WINDOWS, log
from input import (ACTION_IDS, CONTROL_ACTION_NAMES as _CONTROL_NAMES,
                     build_hotkey_table, build_macro_table,
                     effective_mode_key)
from input import make_input_backend, make_pad_backend
from input import (RecordingStore, PAD_REC_FILE,
                        MAX_STEPS as REC_MAX_STEPS,
                        MAX_RECORDINGS as REC_MAX, STEP_KEY, STEP_MOVE,
                        STEP_BUTTON, STEP_PAD_BUTTON, STEP_PAD_AXIS,
                        BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_X1, BTN_X2,
                        PHASE_DOWN, PHASE_UP)
from detection import (FollowMemory, FigureMemory, scan_region, accent_soft_of,
                    accent_matcher_of, accent_variants_of, _parse_accents,
                    _sig_matches_accents, _shape_tier)

if IS_WINDOWS:

    import ctypes
    import ctypes.wintypes as wt
    from input import (
        CaptureBackend, GetMessage, HC_ACTION, HOOKPROC,
        INTERCEPTION_EXTRA_INFO, KBDLLHOOKSTRUCT,
        LLKHF_INJECTED, LLMHF_INJECTED,
        LLMHF_LOWER_IL_INJECTED, MSG, MSLLHOOKSTRUCT, PostThreadMessage,
        RegisterHotKey, XBUTTON1,
        SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN, SM_XVIRTUALSCREEN,
        SM_YVIRTUALSCREEN, UnregisterHotKey, VK_CONTROL, VK_LWIN, VK_MENU,
        VK_SHIFT, WH_KEYBOARD_LL, WH_MOUSE_LL, WM_HOTKEY,
        WM_KEYDOWN, WM_KEYUP, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MBUTTONDOWN,
        WM_MBUTTONUP, WM_MOUSEMOVE, WM_QUIT, WM_RBUTTONDOWN, WM_RBUTTONUP,
         WM_XBUTTONDOWN, WM_XBUTTONUP, cursor_visible,
         get_cursor_pos,
         inject_combo, inject_mouse, kernel32, move_cursor,
         setup_dpi_awareness, user32,
    )

else:
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    INTERCEPTION_EXTRA_INFO = 0x534E4150
    WM_RBUTTONDOWN = 0x0204
    WM_RBUTTONUP = 0x0205
    WM_MBUTTONDOWN = 0x0207
    WM_MBUTTONUP = 0x0208
    WM_MOUSEMOVE = 0x0200
    WM_XBUTTONDOWN = 0x020B
    WM_XBUTTONUP = 0x020C
    WM_HOTKEY = 0x0312
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_QUIT = 0x0012
    XBUTTON1 = 0x0001

MOUSE2_HOLD_SECONDS = 0.45

TRIGGER_HOLD_SECONDS = 0.20

# A classic click owns the trigger only long enough to close the camera loop.
# The first post-nudge frame can still show the pre-input view in a streamed
# game, so corrections are response-gated rather than replayed every detector
# tick. A full second leaves room for several rendered response frames while
# keeping a broken input/capture backend bounded.
CLASSIC_LOCK_TIMEOUT_SECONDS = 1.00
CLASSIC_LOCK_POLL_SECONDS = 1.0 / 240.0
CLASSIC_LOCK_RESPONSE_MIN_SECONDS = 0.018
CLASSIC_LOCK_RESPONSE_RETRY_SECONDS = 0.080
CLASSIC_LOCK_MAX_CORRECTIONS = 16
# A Mode-1 shot only needs the game to process the nudge before the click.
# This is far shorter than a detector frame and keeps the assist instant.
MODE1_FIRE_SETTLE_S = 0.003
# ADS/right-button zoom reduces the game's view rotation per mouse count.
# Compensate it for both Mode 1 snaps and Mode 2 steering.

# Normal held steering may stop within five rendered pixels.  Mode 1's click
# gate does not use this deadband: it still requires the measured target centre
# to occupy the exact centre pixel.
HOLD_CENTER_DEADBAND_PX = 5
HOLD_CENTER_RELEASE_PX = 5
HOLD_FINE_STEER_RADIUS_PX = 8

# Hold gain is a speed limit, never a fraction of the measured error.  At 120
# frames per second gain 1.0 can move at most ~50 screen pixels per frame; a
# larger error is closed over successive frames instead of being multiplied
# into an overshoot.  Track target is deliberately more aggressive, still with
# a hard safety clamp and one command per fresh measurement.
HOLD_STEER_SPEED_PX_S = 6000.0
TRACK_TARGET_SPEED_PX_S = 12000.0
TRACK_TARGET_SAFETY_MAX_PX = 360.0
ASSUMED_FRAME_SECONDS = 1.0 / 120.0
# Track target aims this many screen pixels above the measured body centre
# (screen Y grows downward).  X is an exact zero-pixel lock.
TRACK_TARGET_AIM_OFFSET_Y = -5.0
TRACK_TARGET_INTERVAL_S = 1.0 / 120.0

# Ledger marker: this Mode-1 trigger DOWN was deliberately passed through
# (rapid_click_snap off, an earlier click still being handled). Its UP must
# pass through too instead of being mistaken for a suppressed click.
_MODE1_PASS_THROUGH = object()

# Controller tuning used to masquerade as editable configuration despite
# having no GUI controls. Keep the fixed product behaviour explicit here.
PAD_POLL_HZ = 125.0
PAD_DEADZONE_PERCENT = 20.0
PAD_SENSITIVITY = 1.0
PAD_TRIGGER_THRESHOLD = gamepad.DEFAULT_TRIGGER_THRESHOLD
PAD_RUMBLE_ENABLED = True
PAD_DRIVE_ENABLED = True
PAD_INDEX = -1
AIM_STICK_UNITS_PER_PIXEL = 100.0

MOTION_MAX_OFFSET = 80.0
MOTION_STATIONARY_RADIUS_PX = 3.0
MOTION_MIN_SPEED_PX_S = 24.0
MOTION_MIN_SPAN_S = 0.024
MOTION_OUTPUT_DELAY_S = 0.018
MOTION_MAX_HORIZON_S = 0.14

REC_MOVE_MERGE_S = 0.05

MOUSE_BUTTON_INDEX = {"mouse1": BTN_LEFT, "mouse2": BTN_RIGHT,
                      "mouse3": BTN_MIDDLE, "mouse4": BTN_X1,
                      "mouse5": BTN_X2}

ROLE_TRIGGER = 1
ROLE_OPTIONS = 2
_MOUSE_BUTTON_MSG = {
    WM_LBUTTONDOWN: (BTN_LEFT, True), WM_LBUTTONUP: (BTN_LEFT, False),
    WM_RBUTTONDOWN: (BTN_RIGHT, True), WM_RBUTTONUP: (BTN_RIGHT, False),
    WM_MBUTTONDOWN: (BTN_MIDDLE, True), WM_MBUTTONUP: (BTN_MIDDLE, False),
    WM_XBUTTONDOWN: (None, True), WM_XBUTTONUP: (None, False),
}

def mouse_message_button(msg, mouse_data=0):
    """A mouse message -> (button index, down), or None when it is not one.

    The button indices are recordings' BTN_* values, which are the same
    numbering win32rt.inject_mouse takes - so the button a role is bound to is
    also the button that gets injected, with no second translation table.
    """
    pair = _MOUSE_BUTTON_MSG.get(int(msg))
    if pair is None:
        return None
    button, down = pair
    if button is None:
        button = BTN_X1 if (int(mouse_data) >> 16) == XBUTTON1 else BTN_X2
    return button, down

_MOUSE_REC_MAP = {
    0x0201: (BTN_LEFT, PHASE_DOWN),
    0x0202: (BTN_LEFT, PHASE_UP),
    0x0203: (BTN_LEFT, PHASE_DOWN),
    0x0204: (BTN_RIGHT, PHASE_DOWN),
    0x0205: (BTN_RIGHT, PHASE_UP),
    0x0206: (BTN_RIGHT, PHASE_DOWN),
    0x0207: (BTN_MIDDLE, PHASE_DOWN),
    0x0208: (BTN_MIDDLE, PHASE_UP),
    0x0209: (BTN_MIDDLE, PHASE_DOWN),
    0x020B: (BTN_X1, PHASE_DOWN),
    0x020C: (BTN_X1, PHASE_UP),
    0x020D: (None, PHASE_DOWN),
}
_MOUSE_REC_XBUTTON = (0x020B, 0x020C, 0x020D)

PAD_HID_OFFSET = 1000
_PAD_REC_ACTIONS = frozenset((
    "record_macro", "stop_macro", "play_macro",
    "play_macro1", "play_macro2", "play_macro3", "play_macro4",
    "play_macro5"))

def _debug_boxes(collect, origin, cx, cy):
    """Absolute screen boxes for the scan candidates the collector saw.

    ``collect`` entries are (score, ddx, ddy, dist, area, contrast,
    bw, bh) with ddx/ddy relative to the scan anchor; the debug
    overlay draws in screen coordinates, so the anchor is planted
    back through the capture origin.
    """
    out = []
    for item in collect or ():
        try:
            _s, ddx, ddy, _d, _a, _ct, bw, bh = item
            ax = origin[0] + cx + ddx
            ay = origin[1] + cy + ddy
            out.append((int(ax - bw / 2.0), int(ay - bh / 2.0),
                        int(ax + bw / 2.0), int(ay + bh / 2.0)))
        except Exception:
            continue
    return out


class _YOLOScanTarget(object):
    """Small adapter from detection.Detection to scan-engine target fields."""

    __slots__ = ("dx", "dy", "bw", "bh", "hscore", "sig", "area",
                 "dist", "contrast", "source", "predicted", "seq",
                 "observed_at", "track_id")

    def __init__(self, det, dx, dy):
        self.dx = dx
        self.dy = dy
        self.bw = det.w
        self.bh = det.h
        self.hscore = det.conf
        self.sig = None
        self.area = max(1, int(det.w) * int(det.h))
        self.dist = math.hypot(dx, dy)
        self.contrast = 0.0
        self.source = getattr(det, "source", "detect")
        self.predicted = bool(getattr(det, "predicted", False))
        self.seq = int(getattr(det, "seq", 0) or 0)
        self.observed_at = float(
            getattr(det, "captured_at", 0.0)
            or getattr(det, "t", 0.0) or 0.0)
        self.track_id = int(getattr(det, "track_id", 0) or 0)


class _ColorScanTarget(object):
    """Screen-pixel adapter for a persistent colour-lock measurement."""

    __slots__ = ("dx", "dy", "bw", "bh", "hscore", "sig", "area",
                 "dist", "contrast", "source", "predicted", "seq",
                 "observed_at", "track_id", "quality", "human_confirmed")

    def __init__(self, sample, mx, my):
        self.dx = int(round(float(sample.x) - float(mx)))
        self.dy = int(round(float(sample.y) - float(my)))
        self.bw = max(1, int(round(float(sample.w))))
        self.bh = max(1, int(round(float(sample.h))))
        self.hscore = float(getattr(sample, "conf", 0.0) or 0.0)
        self.sig = None
        self.area = max(1, int(self.bw * self.bh))
        self.dist = math.hypot(self.dx, self.dy)
        self.contrast = 0.0
        self.source = "color"
        self.predicted = False
        self.seq = int(getattr(sample, "seq", 0) or 0)
        self.observed_at = float(
            getattr(sample, "observed_at", 0.0) or 0.0)
        self.track_id = int(getattr(sample, "track_id", 0) or 0)
        self.quality = float(getattr(sample, "quality", 0.0) or 0.0)
        self.human_confirmed = bool(
            getattr(sample, "human_confirmed", False))


class App(object):
    def __init__(self, cfg, identity=None):
        self.cfg = cfg
        self.identity = identity

        # Mode is live operational state, not a persisted setting.  It changes
        # through the visible keyboard/controller mode bindings and starts in
        # guide mode unless a command-line launch override supplied one.
        # Mode 2 is the only user-facing mode.  Mode 1 remains only as
        # the internal single-press classic transaction.
        self._mode = 2

        self.running = False

        self.stop = threading.Event()
        self.ready = threading.Event()

        self.click_q = queue.Queue()

        self.pending_classics = []

        self._pending_lock = threading.Lock()

        self.worker_active = False
        self.scanner_dead = False
        self.fail_count = 0

        self.pause_until = 0.0
        self._slow_log_at = 0.0
        self._stats_log_at = 0.0
        self._edge_log_at = 0.0

        self._pass_log_at = {}
        self._mouse2_log_at = 0.0
        self._lead_log_at = 0.0
        self._nudge_fail_log_at = 0.0
        self._yolo_log_at = 0.0
        self._fallback_next_at = 0.0
        # Recent target positions per detection type, used to detect motion
        # and prioritise moving targets over stationary clutter across ALL
        # detection types (YOLO, pose, colour, contrast).
        self._motion_history = deque(maxlen=8)
        self.rbutton_down_time = 0.0
        self.snap_count = 0
        self.pass_count = 0

        self.last_snap_time = 0.0
        self.last_snap_pos = None

        self._continuous_lock = threading.Lock()
        self._continuous_active = False

        self._debug_vis = None

        self._vs_cache = {"t": 0.0, "v": None}

        self.hook = None
        self.proc_ref = None
        self.thread_id = None

        self._capture = CaptureBackend(cfg.get("capture_backend", "auto"))

        # YOLO26n 2D person detector (fast, no pose)
        self._yolo26n = None
        self._yolo_search_radius = None
        # Per-thread sequence of the last YOLO frame consumed: each worker
        # gets every sighting at most once, so one detection can never be
        # replayed as several identical nudges.
        self._yolo_local = threading.local()
        self._steer_local = threading.local()
        self._steer_response = 1.0
        self._steer_response_n = 0
        self._steer_response_ads = 1.75
        self._steer_response_ads_n = 0
        if bool(cfg.get("game_mode", False)):
            try:
                from detection import YOLO26nBackend, YOLOConfig
                yolo_cfg = YOLOConfig(
                    target_fps=120,
                    capture_backend=str(cfg.get("capture_backend", "auto")),
                )
                self._yolo26n = YOLO26nBackend(yolo_cfg)
                self._configure_yolo_backend(self._yolo26n)
                if not self._yolo26n.start():
                    log("YOLO26n backend failed to start, disabling")
                    self._yolo26n = None
                else:
                    log("YOLO26n backend started (input_size=%d)" % yolo_cfg.input_size)
            except Exception as exc:
                log("YOLO26n backend init failed: %r" % (exc,))
                self._yolo26n = None

        self._input = make_input_backend(cfg)
        self.follow = FollowMemory()
        self.subject = FigureMemory()

        self._lbtn_down = False

        self._rbtn_down = False
        self._rbtn_armed = False
        self._rbtn_hold_done = False
        self._rbtn_press_id = 0
        self._rbtn_lock = threading.Lock()

        # Mode 2 trigger button single-click vs hold detection
        self._trigger_press_time = 0.0
        self._trigger_press_id = 0
        self._trigger_hold_done = False
        self._trigger_hold_timer = None
        self._trigger_suppressed_down = None  # (x, y, now, entry) for reinjection
        self._trigger_lock = threading.Lock()
        # One FIFO slot per suppressed Mode-1 DOWN: the matching entry, or
        # None when the click was deliberately blocked without queuing a job.
        # The UP is swallowed from this ledger, never inferred from whichever
        # pending classic happens to be first.
        self._blocked_trigger_ups = []

        self._hk_always = []
        self.local_binds = []
        self._local_map = {}
        self._local_combos = set()
        self._key_held = set()
        self.input_focused = False
        self.key_hook = None
        self.key_proc_ref = None

        self.macro_q = queue.Queue()
        self._macros = []
        self._macro_map = {}
        self._macro_trigger_vks = set()

        self.rec_store = RecordingStore()
        try:
            from utils import recordings_dir as _recordings_dir
            _pad_path = os.path.join(_recordings_dir(), PAD_REC_FILE)
        except Exception:
            _pad_path = None
        self.rec_store_pad = RecordingStore(path=_pad_path) \
            if _pad_path else RecordingStore(path="pad_macros.json")
        self._rec_buffer = None
        self._rec_target = None
        self._rec_last_t = 0.0
        self._rec_last_move = 0.0
        self._rec_combos = set()
        self._rec_full = False
        self._rec_lock = threading.Lock()
        self.play_target_rid = None
        self.play_target_pad_rid = None

        self._play_cancel = threading.Event()
        self._play_busy = False
        self._play_looping = False
        self._play_rid = None
        self._play_source = None

        self._auto_next = 0.0
        self._auto_lock = threading.Lock()
        self._auto_armed = False

        self._bypass_available = True
        try:
            hotkeys.parse_bypass_spec(self.cfg.get("bypass_key", "shift"))
        except Exception as exc:
            self._bypass_available = False
            log("bypass spec parse failed: %r" % (exc,))

        self._track = deque(maxlen=32)
        self._tracker = detection.MotionTracker(dim=3)
        self._tracker_lock = threading.Lock()
        self._last_motion_seq = 0
        self._motion_track_id = 0
        self._motion_observed_at = 0.0
        self._motion_latency_s = 0.0
        self._motion_sample_seq = 0
        self._motion_source = ""

        self.follow_lock = None
        self._follow_stable = 0
        self._chase_stable = 0

        self._mode_epoch = 0

        self._state_lock = threading.RLock()

        self._center_follow = None
        self._pick_at = None
        self._pick_at_time = 0.0

        self._chase_ref = None

        self._center_hold = None

        self._center_hold_tomb = 0.0

        # One human-seeded colour identity for the current hold transaction.
        # It is reset on a new press/release, never while its colour remains
        # visible, and is shared by Mode 1 and the normal held Mode 2 path.
        self._target_lock = None
        self._target_lock_seq = 0
        self._target_lock_seeded_at = 0.0
        self._target_lock_misses = 0
        self._target_lock_source = ""

        # Dedicated Track target lock is independent of normal assist state.
        self._track_target_active = False
        self._track_target_thread = None
        self._track_target_vk = None
        self._track_target_mods = 0
        self._track_target_color = None
        self._track_target_misses = 0
        self._track_target_last_log = 0.0

        self._center_prev = None
        self._view_moved = False
        self._view_epoch = 0

        self._own_pid = os.getpid()

        self._pad_out = None
        self._pad_bindings = []
        self._pad_macros = []
        self._pad_index = None
        self._pad_connected = False
        self._pad_misses = 0
        self._pad_cursor = None
        self._pad_target = None
        self._pad_last_drive = 0.0
        self._pad_click_down = False
        self._pad_right_down = False
        self._pad_entry = None
        self._pad_raw_down = False
        self._pad_state_lock = threading.Lock()
        self._pad_prev_buttons = 0
        self._pad_prev_axes = {}
        self._pad_bypass = False
        self._pad_bypass_tracker = None
        self._pad_emu_warned = False

        # Virtual-stick aim is a pulse, not a persistent deflection.  A
        # generation token prevents an older timer from centring a newer
        # correction.
        self._aim_stick_lock = threading.Lock()
        self._aim_stick_generation = 0
        self._aim_stick_timer = None
        self._aim_stick_backend = None

        self._presence_nonce = secrets.token_hex(8)
        self._hb_sent = False
        self._bye_sent = False

    def is_recording(self):
        """True while a macro recording is armed (public read-only view of
        the capture buffer, so the GUI poller no longer reaches into
        app._rec_buffer)."""
        with self._rec_lock:
            return self._rec_buffer is not None

    def presence_hash(self):
        """Row hash of the entitlement key (the e-mail): two machines
        signed into the same Discord account beat the same key, which
        is exactly the sharing we detect."""
        if not self.identity:
            return None
        return online.row_hash(self.identity)

    def run_presence_worker(self):
        """Beat the Discord presence webhook while LAUNCHED.

        Paused = a goodbye beat, so a closed/paused session frees the
        account immediately instead of after the window expires.
        Best-effort: missed beats just look like gaps.

        The cadence is a build-time constant (online.presence_interval_
        seconds), NOT a setting: a user-writable 0 used to turn the whole
        account-sharing detector off from config.ini."""
        idh = self.presence_hash()
        was_running = None
        while not self.stop.is_set():
            secs = online.presence_interval_seconds()
            if not idh or not online.presence_webhook():
                if self.stop.wait(30):
                    break
                continue

            if self.running and was_running is not True:
                if online.post_presence(self.cfg, idh,
                                        self._presence_nonce):
                    self._hb_sent = True
                    self._bye_sent = False
                was_running = True
                continue
            if not self.running and was_running is not False:
                if self._hb_sent and not self._bye_sent:
                    if online.post_presence(self.cfg, idh,
                                            self._presence_nonce,
                                            bye=True):
                        self._bye_sent = True
                was_running = False
                continue
            was_running = bool(self.running)
            if self.stop.wait(max(1, secs)):
                break
            if self.running:
                if online.post_presence(self.cfg, idh,
                                        self._presence_nonce):
                    self._hb_sent = True
                    self._bye_sent = False
            elif self._hb_sent and not self._bye_sent:
                if online.post_presence(self.cfg, idh,
                                        self._presence_nonce, bye=True):
                    self._bye_sent = True
        if idh and self._hb_sent and not self._bye_sent:
            online.post_presence(self.cfg, idh, self._presence_nonce,
                                 bye=True)

    def _fg_is_own(self):
        """True when the foreground window belongs to Snappy AI itself
        (main GUI, popups, file dialogs). Assistance must stay out of
        the tool's own interface."""
        try:
            fg = user32.GetForegroundWindow()
            if not fg:
                return False
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
            return pid.value == self._own_pid
        except Exception:
            return False

    def _pass_note(self, reason):
        """Throttled "click passed through: <reason>" (5 s per REASON).

        Buckets are per reason so concurrent causes never mask each
        other: an "outside box" line must not silence a "no target"
        line five seconds of evidence later.
        """
        now = time.perf_counter()
        should_log = False
        with self._state_lock:
            if now - self._pass_log_at.get(reason, 0.0) > 5.0:
                self._pass_log_at[reason] = now
                should_log = True
        if should_log:
            log("Click passed through: %s." % reason)

    def radius_px(self):
        """Configured search radius in capture/screen pixels.

        Win32 cursor, capture, and detector coordinates are already physical
        pixels after DPI awareness is enabled. Scaling this value a second
        time turned a visible 40 px setting into 54 px at 135% display scale.
        """
        try:
            return max(1, int(round(float(self.cfg.get("radius", 150)))))
        except (TypeError, ValueError, OverflowError):
            return 150

    def current_mode(self):
        """Current live assistance mode (1 classic, 2 guide)."""
        return self._mode

    def _motion_enabled(self):
        """Whether screen-space motion history may affect an aim point."""
        return (bool(self.cfg.get("motion_predict", False))
                or bool(self.cfg.get("motion_3d", False)))

    def _reset_motion_history(self):
        """Drop motion learned in a different camera/target coordinate frame.

        A successful view nudge moves the entire 3D scene.  Retaining those
        samples makes camera motion look like target velocity and causes the
        next lead to overshoot in the opposite direction.
        """
        with self._state_lock:
            self._track.clear()
            self._last_motion_seq = 0
            self._motion_track_id = 0
            self._motion_observed_at = 0.0
            self._motion_latency_s = 0.0
            self._motion_source = ""
        with self._tracker_lock:
            try:
                self._tracker.reset(clear_history=True)
            except TypeError:
                # Compatibility with an older MotionTracker implementation.
                self._tracker.reset()
                if hasattr(self._tracker, "_prev_obs"):
                    self._tracker._prev_obs = None

    def _record_motion_observation(self, target, ax, ay):
        """Record one fresh, measured target sample for motion prediction.

        YOLO predictions are deliberately excluded: estimating velocity from
        positions that were themselves extrapolated compounds prediction and
        produces large overshoots.  Sequence and track IDs also prevent two
        assist workers from feeding the same backend result twice or blending
        two different people into one velocity.
        """
        source = getattr(target, "source", "")
        if bool(getattr(target, "predicted", False)) \
                or source == "predicted" \
                or (source and source not in ("detect", "pose", "color")):
            return False
        if source in ("detect", "pose", "color") \
                and source != str(getattr(self, "_motion_source", "") or ""):
            # Do not blend a pose/colour replacement into the fast
            # detector's velocity history; that manufactures a jump.
            with self._state_lock:
                self._track.clear()
                self._last_motion_seq = 0
                self._motion_track_id = 0
            self._motion_source = source

        try:
            seq = int(getattr(target, "seq", 0) or 0)
        except (TypeError, ValueError):
            seq = 0
        try:
            track_id = int(getattr(target, "track_id", 0) or 0)
        except (TypeError, ValueError):
            track_id = 0
        try:
            observed_at = float(getattr(target, "observed_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            observed_at = 0.0
        now = time.perf_counter()
        if not math.isfinite(observed_at) or observed_at <= 0.0 \
                or observed_at > now + 0.05:
            observed_at = now
        sample_latency = max(0.0, min(0.25, now - observed_at))

        reset_tracker = False
        with self._state_lock:
            if seq > 0 and seq <= self._last_motion_seq:
                return False
            if track_id > 0 and self._motion_track_id > 0 \
                    and track_id != self._motion_track_id:
                self._track.clear()
                reset_tracker = True
            if self._track and observed_at <= self._track[-1][0]:
                # Never turn an out-of-order asynchronous result into a
                # current sample; that fabricates velocity and lead.
                return False
            if seq > 0:
                self._last_motion_seq = seq
            if track_id > 0:
                self._motion_track_id = track_id
            self._motion_observed_at = observed_at
            old_latency = float(getattr(self, "_motion_latency_s", 0.0) or 0.0)
            self._motion_latency_s = (sample_latency if old_latency <= 0.0
                                      else 0.80 * old_latency
                                      + 0.20 * sample_latency)
            self._track.append((observed_at, float(ax), float(ay)))
            while self._track and observed_at - self._track[0][0] > 0.6:
                self._track.popleft()

        try:
            area = float(getattr(target, "area", 0) or 0)
        except (TypeError, ValueError):
            area = 0.0
        with self._tracker_lock:
            if reset_tracker:
                try:
                    self._tracker.reset(clear_history=True)
                except TypeError:
                    self._tracker.reset()
                    if hasattr(self._tracker, "_prev_obs"):
                        self._tracker._prev_obs = None
            if area > 0.0:
                try:
                    self._tracker.update(
                        observed_at, (float(ax), float(ay), math.sqrt(area)))
                except Exception:
                    return False
        return True

    def _sample_yolo_motion(self):
        """Passively record one fresh fast-detector centre without steering.

        The YOLO capture thread is already running, so this does not launch an
        inference or pose pass. It simply gives click-time leading the last few
        measured frames that an accurate direction/speed estimate requires,
        while remaining completely separate from the removed background chase.
        """
        backend = getattr(self, "_yolo26n", None)
        if backend is None or not self._motion_enabled():
            return False
        try:
            self._sync_yolo_search_radius()
            last_seq = int(getattr(self, "_motion_sample_seq", 0) or 0)
            det = backend.get_latest_detection(
                after_seq=last_seq,
                max_age=self._detection_max_age_seconds())
            if det is None:
                return False
            seq = int(getattr(det, "seq", 0) or 0)
            if seq > 0:
                self._motion_sample_seq = max(last_seq, seq)
            # Prediction is derived from the fast measured boxes only. Pose,
            # colour and detector bridge results may validate identity but
            # must not become velocity evidence.  Match by proximity to the
            # latest seq rather than exact equality - the pose worker and fast
            # detector may number frames differently, and an exact match can
            # silently drop valid motion samples.
            if getattr(det, "source", "") != "detect":
                raw_getter = getattr(backend, "get_last_measurements", None)
                raw = raw_getter() if callable(raw_getter) else ()
                fast = [item for item in raw
                        if getattr(item, "source", "") == "detect"
                        and abs(int(getattr(item, "seq", 0) or 0) - seq) <= 1]
                det = max(
                    fast,
                    key=lambda item: (float(getattr(
                        item, "quality", 0.0) or 0.0),
                                      float(getattr(
                                          item, "conf", 0.0) or 0.0)),
                    default=None,
                )
            if det is None or bool(getattr(det, "predicted", False)):
                return False
            box = self._center_box()
            if box is None:
                return False
            result = self._yolo26n_to_scan_result(det, box[0], box[1])
            if result is None or result.target is None:
                return False
            target = result.target
            return self._record_motion_observation(
                target, box[0] + target.dx, box[1] + target.dy)
        except Exception as exc:
            if time.perf_counter() - self._lead_log_at > 5.0:
                self._lead_log_at = time.perf_counter()
                log("Motion sampling skipped: %r" % (exc,))
            return False

    def _bypass_enabled(self):
        """True when the user's bypass input must stop assistance.

        The shift_bypass setting (default on) preserves the long-standing
        desktop precision override; games that hold its key during play
        (sprint) turn it off, or sprinting silently disables every correction
        with no log line to show for it. Which input that is lives in
        bypass_key / bypass_pad - the switch only says whether it counts.
        """
        try:
            return bool(self.cfg.get("shift_bypass", True))
        except (TypeError, ValueError, AttributeError):
            return True

    @staticmethod
    def _cap_step(dx, dy, max_step):
        """Clamp a relative view-rotation step to max_step px magnitude,
        preserving direction. One far transient pick must never spin
        the whole view in a single shot: capped steps still converge
        onto a persisting target over 2-3 tracking shots, while a
        one-frame phantom causes a small jerk instead of a 300px spin.
        max_step <= 0 disables (uncapped legacy behaviour).
        """
        try:
            cap = float(max_step)
        except (TypeError, ValueError):
            return int(dx), int(dy)
        if cap <= 0:
            return int(dx), int(dy)
        mag = math.hypot(dx, dy)
        if mag <= cap:
            return int(dx), int(dy)
        k = cap / mag
        return int(round(dx * k)), int(round(dy * k))

    def _max_step_cfg(self):
        """Per-shot reticle rotation cap, px (0 = off)."""
        try:
            return max(0.0, float(self.cfg.get("center_max_step", 160)))
        except (TypeError, ValueError):
            return 160.0

    def _steer_response_now(self):
        """Current view-pixels-per-count response, including ADS zoom."""
        if bool(getattr(self, "_rbtn_down", False)):
            return min(4.0, max(0.25, float(
                getattr(self, "_steer_response_ads", 1.75) or 1.75)))
        return min(4.0, max(0.25, float(
            getattr(self, "_steer_response", 1.0) or 1.0)))

    def _one_shot_view_delta(self, dx, dy):
        """Scale a single correction by the height/ADS base.

        Single clicks do not use the hold X/Y gain sliders, but they do use
        the target-size and ADS compensation so the view reaches the target.
        """
        # The initial snap is a full correction to the measured target
        # centre.  The ADS/hipfire height factor is for held follow speed,
        # not for the first snap.
        response = self._steer_response_now()
        sx = float(dx) / response
        sy = float(dy) / response
        sx = max(-abs(float(dx)) * 4.0, min(abs(float(dx)) * 4.0, sx))
        sy = max(-abs(float(dy)) * 4.0, min(abs(float(dy)) * 4.0, sy))
        cap = self._max_step_cfg()
        area_cap = 2.0 * max(1, self.radius_px())
        # cap <= 0 means "off" (no cap).  Only apply the radius area cap when
        # a user cap is actually set and it exceeds the radius-derived limit.
        if cap > 0 and cap > area_cap:
            cap = area_cap
        return self._cap_step(sx, sy, cap)

    def _scaled_view_delta(self, dx, dy):
        """Speed-limit a hold correction; never scale it away from the target.

        The X/Y sliders are maximum camera *turn speed*, expressed as a
        screen-pixel budget per 60 Hz frame.  They do not multiply the error,
        so no setting can turn a stationary target into a larger view jump.
        ADS only affects the learned view-pixels-per-count response; target
        height is telemetry and no longer changes the correction fraction.
        """
        try:
            gain = min(2.0, max(0.1, float(
                self.cfg.get("aim_speed", 1.0))))
        except (TypeError, ValueError):
            gain = 1.0
        try:
            y_gain = min(2.0, max(0.1, float(
                self.cfg.get("aim_y_speed_multiplier", 1.0))))
        except (TypeError, ValueError):
            y_gain = 1.0

        response = self._steer_response_now()
        max_x = HOLD_STEER_SPEED_PX_S * ASSUMED_FRAME_SECONDS * gain
        max_y = HOLD_STEER_SPEED_PX_S * ASSUMED_FRAME_SECONDS \
            * gain * y_gain
        step_x = math.copysign(min(abs(float(dx)), max_x), float(dx))
        step_y = math.copysign(min(abs(float(dy)), max_y), float(dy))
        sx = step_x / response
        sy = step_y / response
        # A bad response estimate may not create more than the measured
        # error's worth of view movement; the cap is safety, not gain.
        sx = max(-abs(float(dx)) * 4.0, min(abs(float(dx)) * 4.0, sx))
        sy = max(-abs(float(dy)) * 4.0, min(abs(float(dy)) * 4.0, sy))

        # Hold steering: the speed budget is already the safety limit.  Only
        # apply the user's center_max_step cap - do not shrink it further with
        # the search-radius area cap, or a fast-moving target can outrun the
        # correction and get lost inside the radius.
        cap = self._max_step_cfg()
        return self._cap_step(sx, sy, cap)

    def _bypass_spec(self):
        """(mods, vk) of bypass_key, or None when it is unbound/invalid.

        Read per call rather than cached: the hook and the two worker loops
        call this at most a few hundred times a second, config.get is a dict
        read, and a cached spec would go stale the moment the GUI writes a new
        one - the whole point of the setting is that it can change live.
        """
        return hotkeys.parse_bypass_spec(self.cfg.get("bypass_key", "shift"))

    def _bypass_held_kb(self):
        """True while the keyboard bypass input is physically down.

        A spec of modifiers only ("shift") asks whether every one of them is
        down; a combination asks for its modifiers AND its key. Either side of
        a modifier counts, because GetAsyncKeyState on a generic VK reports
        both and Win has no generic VK at all. A failed read answers False -
        keep assisting rather than silently switch the program off.
        """
        if not self._bypass_available:
            return False
        parsed = self._bypass_spec()
        if parsed is None:
            return False
        mods, vk = parsed
        try:
            for group in hotkeys.bypass_mod_vks(mods):
                if not any(user32.GetAsyncKeyState(code) & 0x8000
                           for code in group):
                    return False
            if vk is not None and not (user32.GetAsyncKeyState(vk) & 0x8000):
                return False
            return True
        except Exception:
            return False

    def _bypass_held(self):
        """True while EITHER device's bypass input is down.

        The hook reads this per event; the follow and auto-click loops (which
        the hook's `armed` gate does not cover) poll it per cycle, so the
        bypass holds for steering too - unless the switch is off, which those
        loops check first. The controller half is a flag the pad poller sets
        each sample: XInput state is only readable on that thread's cadence,
        and re-reading the pad from the hook would put a device call in the
        one place that must stay fast.
        """
        if self._pad_bypass:
            return True
        return self._bypass_held_kb()

    @staticmethod
    def _hit_test(ax, ay, x, y, bw, bh, on_target):
        """True when a press/release at (x, y) already landed on the
        item sighted at (ax, ay): within on_target px of its centre AND
        really standing on its box. One definition for every mode: the
        box half keeps tiny controls assistable - a press 8 px from the
        middle of a 6 px checkbox is inside the window yet has hit
        nothing, and that is exactly the case snapping exists for.
        """
        if bw <= 0 or bh <= 0:
            return False
        return (math.hypot(ax - x, ay - y) <= on_target
                and abs(ax - x) * 2.0 <= bw
                and abs(ay - y) * 2.0 <= bh)

    def _virtual_screen(self):
        """Virtual-screen rect, cached 2 s: four Win32 syscalls per
        grab added up across every scan of every session for nothing -
        display topology changes far slower than that."""
        now = time.perf_counter()
        if self._vs_cache["v"] is not None and \
                now - self._vs_cache["t"] < 2.0:
            return self._vs_cache["v"]
        v = (user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
             user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
             user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
             user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
        with self._state_lock:
            self._vs_cache["t"] = now
            self._vs_cache["v"] = v
        return v

    def grab_around(self, x, y, radius):
        """
        Capture a square around (x, y).
        Returns (img, cx, cy, origin, edges) or (None, None, None,
        None, None). img is BGR uint8; origin is the region's (left,
        top) on the virtual screen; edges is (left, top, right, bottom)
        - which region borders are true screen edges. Per-call values:
        the click workers and the follow worker run concurrently and
        must never share this state through self.
        """
        if self._capture is None:
            return None, None, None, None, None
        pad = radius + 12
        left = x - pad
        top = y - pad
        side = 2 * pad + 1

        vx, vy, vw, vh = self._virtual_screen()

        l = max(left, vx)
        t = max(top, vy)
        r = min(left + side, vx + vw)
        b = min(top + side, vy + vh)

        gw = r - l
        gh = b - t

        if gw < 40 or gh < 40:

            if time.perf_counter() - self._edge_log_at > 30.0:
                self._edge_log_at = time.perf_counter()
                log("Scan skipped: capture region too small at the "
                    "screen edge - nothing to analyse there.")
            return None, None, None, None, None

        origin = (int(l), int(t))

        edges = (l == vx, t == vy, r == vx + vw, b == vy + vh)

        img = self._capture.grab(l, t, r, b)

        if img is None:
            return None, None, None, None, None

        return img, x - l, y - t, origin, edges

    def _reticle_active(self):
        """True when centre-watch must ignore the OS cursor position.

        A raw-input game hides the OS cursor (and parks it, typically
        in a corner) while the on-screen reticle does the aiming -
        every real fire press then arrives from FAR outside the watched
        square, and any cursor-position gate turns the whole feature
        off exactly when the game starts. Menus show the cursor again,
        so this flips back automatically: visible cursor = normal
        desktop gating, hidden cursor = the reticle is the pointer.
        A failed visibility read answers False (keep gating).
        """
        try:
            if self.cfg.get("game_mode", False) \
                    and not cursor_visible():
                return True
        except Exception:
            pass
        return False

    def _center_box(self):
        """(mx, my, half) of the watched centre square, or None when
        "Watch the screen centre only" is off.

        The GATE square is fixed at the middle of the virtual screen,
        `half` = the configured search distance each way (a 40 px
        setting means +/-40 physical screen pixels, an 80x80 gate). The SCAN
        is wider: the centre scan analyses
        cap = half * 2 around the middle, so items straddling the gate
        boundary are measured whole instead of clipped. A press outside
        the gate is not assistance territory (it passes through) even
        though an item there is within scan reach - territory is the
        gate, reach is the scan. The cursor position is irrelevant to
        the geometry: with the switch on, the product treats the screen
        centre as the reticle even when the pointer sits elsewhere.
        """
        try:
            if not bool(self.cfg.get("game_mode", False)):
                return None
        except Exception:
            return None
        vx, vy, vw, vh = self._virtual_screen()
        try:
            half = max(1, int(round(float(self.cfg.get("radius", 150)))))
        except (TypeError, ValueError, OverflowError):
            half = 150
        return (vx + vw // 2, vy + vh // 2, half)

    def _subject_context(self, origin):
        try:
            if not (bool(self.cfg.get("subject_priority", False))
                    or bool(self.cfg.get("subject_only", False))):
                return None
        except Exception:
            return None
        try:
            return self.subject.context(origin[0], origin[1])
        except Exception:
            return None

    def _subject_store(self, ax, ay, target):
        try:
            if not (bool(self.cfg.get("subject_priority", False))
                    or bool(self.cfg.get("subject_only", False))):
                return
        except Exception:
            return
        try:
            hs = float(getattr(target, "hscore", 0.0) or 0.0)
        except (TypeError, ValueError):
            hs = 0.0
        if hs < 0.50:
            return
        try:
            self.subject.store(ax, ay, target.bw, target.bh, hs)
        except Exception:
            pass

    def _configured_search_radius_px(self):
        """Live GUI search radius in physical screen pixels."""
        return self.radius_px()

    def _detection_max_age_seconds(self):
        """Freshness gate derived from the visible scan-time budget."""
        try:
            budget = float(self.cfg.get("scan_budget_ms", 350)) / 1000.0
        except (TypeError, ValueError, OverflowError):
            budget = 0.12
        return min(0.12, max(0.02, budget))

    def _sync_yolo_search_radius(self):
        """Apply the GUI radius to the validated YOLO inner search gate.

        The reviewed detector has a 54 px validated inner field. A smaller GUI
        radius narrows that field exactly; a larger GUI radius is completed by
        the classical outer fallback in ``_scan_center`` rather than silently
        pretending YOLO can see beyond its validated context.
        """
        configured = self._configured_search_radius_px()
        validated = int(getattr(detection, "YOLO26_SEARCH_RADIUS", 54))
        active = min(configured, max(1, validated))
        backend = self._yolo26n
        if backend is not None \
                and active != getattr(self, "_yolo_search_radius", None):
            try:
                backend.search_radius = active
                backend_cfg = getattr(backend, "cfg", None)
                if backend_cfg is not None:
                    backend_cfg.capture_radius = active
                appearance = getattr(backend, "_appearance", None)
                if appearance is not None:
                    appearance.search_radius = active
            except Exception as exc:
                log("YOLO search-radius update failed: %r" % (exc,))
            self._yolo_search_radius = active
        return active

    def _configure_yolo_backend(self, backend):
        """Bind visible settings to the asynchronous detector orchestration."""
        self._yolo26n = backend
        active = self._sync_yolo_search_radius()

        # Runtime pose work is validation, never the primary recovery path.
        # It is submitted only after a fresh fast-model detection completed
        # inside the visible scan budget. This prevents the 640 px pose model
        # from consuming a core continuously while the detector is missing.
        def conditional_pose_interval(detections):
            if not bool(self.cfg.get("hpose_mode", True)):
                return float("inf")
            confirmed = any(
                getattr(item, "source", "") == "detect"
                and not bool(getattr(item, "predicted", False))
                for item in (detections or ()))
            if not confirmed:
                return float("inf")
            try:
                budget_ms = max(1.0, float(
                    self.cfg.get("scan_budget_ms", 350)))
                infer_ms = float(getattr(backend, "_stats", {}).get(
                    "avg_infer_ms", 0.0) or 0.0)
            except (TypeError, ValueError):
                budget_ms, infer_ms = 350.0, 0.0
            if infer_ms > budget_ms:
                return float("inf")
            return 1.0

        backend._pose_submission_interval = conditional_pose_interval
        log("Detection gate follows GUI search radius: %d px%s."
            % (self._configured_search_radius_px(),
               " (YOLO inner gate %d px; outer fallback enabled)" % active
               if self._configured_search_radius_px() > active else ""))

    def _yolo26n_to_scan_result(self, yolo_det, mx, my):
        """Convert YOLO26n detection to ScanResult format."""
        try:
            # YOLO26n gives absolute screen coordinates
            # Convert to center-relative (dx, dy from center)
            source = getattr(yolo_det, "source", "detect")
            # Colour fallback detections from the YOLO backend's appearance
            # process are never direct steering targets.  They are only used
            # through the persistent colour lock seeded from a confirmed
            # human (YOLO detect or pose).
            if source == "color":
                return None
            aim_x = float(yolo_det.x)
            aim_y = float(yolo_det.y)
            if source == "pose":
                if not bool(self.cfg.get("hpose_mode", True)):
                    return None
                min_conf = max(0.0, min(1.0, float(
                    self.cfg.get("hpose_min_conf", 12)) / 100.0))
                confidence = float(getattr(
                    yolo_det, "pose_conf", 0.0)
                    or getattr(yolo_det, "conf", 0.0) or 0.0)
                if confidence < min_conf:
                    return None
                if str(self.cfg.get("hpose_aim", "center")
                       or "center").strip().lower() == "head":
                    # The async detector intentionally publishes compact box
                    # geometry rather than all 17 keypoints. Use the stable
                    # upper-body point for the visible head choice; centre/any
                    # retain the measured box centre.
                    aim_y -= 0.32 * float(getattr(yolo_det, "h", 0.0) or 0.0)
            dx = aim_x - mx
            dy = aim_y - my
            # Recheck predictions against the visible GUI setting so a short
            # velocity extrapolation cannot escape the requested area.
            limit = self._configured_search_radius_px()
            if math.hypot(dx, dy) > limit:
                return None

            return detection.ScanResult(
                "target", _YOLOScanTarget(yolo_det, dx, dy), ms=0.0)
        except Exception:
            return None

    def _track_target_key_matches(self, vk):
        """Whether this key is the bound Track target key."""
        if self._track_target_vk is None or int(vk) != int(self._track_target_vk):
            return False
        mods = int(self._track_target_mods or 0)
        if not mods:
            return True
        for mod_vk in hotkeys.bypass_mod_vks(mods):
            try:
                if not (user32.GetAsyncKeyState(mod_vk) & 0x8000):
                    return False
            except Exception:
                return False
        return True

    def _start_track_target(self):
        """Start the dedicated full-screen target tracking loop."""
        with self._state_lock:
            if self._track_target_active:
                return
            self._track_target_active = True
            self._track_target_color = None
            self._track_target_misses = 0
        t = threading.Thread(
            target=self._track_target_loop,
            name="track-target", daemon=True)
        self._track_target_thread = t
        t.start()

    def _stop_track_target(self):
        """Stop the dedicated target tracking loop immediately."""
        with self._state_lock:
            self._track_target_active = False
            self._track_target_color = None
            self._track_target_misses = 0

    def _reset_target_lock(self):
        """Forget the human-seeded colour identity for a new transaction."""
        with self._state_lock:
            self._target_lock = None
            self._target_lock_misses = 0
            self._target_lock_source = ""

    def _seed_target_lock(self, target, frame, origin, mx, my,
                          model_confirmed, track_id=0):
        """Seed the persistent colour lock from an absolute scan target."""
        if target is None or frame is None or origin is None:
            return False
        try:
            ax = float(mx) + float(target.dx)
            ay = float(my) + float(target.dy)
            bw = max(1.0, float(target.bw))
            bh = max(1.0, float(target.bh))
        except (AttributeError, TypeError, ValueError, OverflowError):
            return False
        tracker = targeting.ColorTracker()
        if not tracker.seed(
                frame, origin,
                (ax - bw * 0.5, ay - bh * 0.5, bw, bh),
                model_confirmed=bool(model_confirmed),
                track_id=int(track_id or 0)):
            return False
        with self._state_lock:
            # Atomic check-and-set: another worker may have seeded between
            # our lock-free check and now. Only install if still empty.
            if self._target_lock is not None:
                return False
            self._target_lock = tracker
            self._target_lock_seq = 0
            self._target_lock_misses = 0
            self._target_lock_seeded_at = time.perf_counter()
            self._target_lock_source = "model" if model_confirmed else "blob"
        return True

    @staticmethod
    def _track_target_reset_color(self):
        with self._state_lock:
            self._track_target_color = None
            self._track_target_misses = 0

    def _track_target_seed(self, frame, origin, box, model_confirmed,
                           track_id=0):
        tracker = targeting.ColorTracker(max_misses=12)
        if not tracker.seed(frame, origin, box,
                            model_confirmed=model_confirmed,
                            track_id=track_id):
            return False
        with self._state_lock:
            self._track_target_color = tracker
            self._track_target_misses = 0
        return True

    def _track_target_color_measurement(self, now):
        """Update the dedicated colour lock in its own local capture."""
        with self._state_lock:
            tracker = self._track_target_color
        if tracker is None:
            return None, False
        box = tracker.search_box()
        if box is None:
            return None, False
        vx, vy, vw, vh = self._virtual_screen()
        try:
            x, y, w, h = box
            l = max(vx, int(x))
            t = max(vy, int(y))
            r = min(vx + vw, int(x + w))
            b = min(vy + vh, int(y + h))
            if r - l < 8 or b - t < 8:
                return None, False
            img = self._capture.grab(l, t, r, b)
            if img is None:
                return None, False
            sample = tracker.update(img, (l, t), now=now)
        except Exception as exc:
            if time.perf_counter() - self._track_target_last_log > 5.0:
                self._track_target_last_log = time.perf_counter()
                log("Track target colour error: %r" % (exc,))
            sample = None
        if sample is not None:
            with self._state_lock:
                self._track_target_misses = 0
            return sample, True
        with self._state_lock:
            self._track_target_misses += 1
            misses = self._track_target_misses
        if misses >= 12:
            self._track_target_reset_color()
        return None, False

    def _track_target_acquire(self, now):
        """Human-first acquisition: fast person, then pose/full-screen scan."""
        vx, vy, vw, vh = self._virtual_screen()
        cx = vx + vw // 2
        cy = vy + vh // 2

        # Fast YOLO26n is the preferred first link in the chain.  Its backend
        # watches the screen centre, which is exactly where a held Track
        # target starts.
        backend = self._yolo26n
        if backend is not None:
            try:
                det = backend.get_latest_detection(max_age=0.16)
            except Exception:
                det = None
            if det is not None and not bool(
                    getattr(det, "predicted", False)) \
                    and getattr(det, "source", "detect") == "detect":
                pad = max(48, int(1.4 * max(float(det.w), float(det.h))) + 16)
                img, _ix, _iy, origin, _edges = self.grab_around(
                    float(det.x), float(det.y), pad)
                if img is not None:
                    box = (float(det.x) - float(det.w) * 0.5,
                           float(det.y) - float(det.h) * 0.5,
                           float(det.w), float(det.h))
                    if self._track_target_seed(
                            img, origin, box, True,
                            int(getattr(det, "track_id", 0) or 0)):
                        return det

        if self._capture is None:
            return None
        try:
            img = self._capture.grab(vx, vy, vx + vw, vy + vh)
        except Exception:
            img = None
        if img is None:
            return None

        cfgv = dict(self.cfg.values)
        # Pose is the explicit fallback when the fast detector misses or the
        # body is only partly visible.  The blob stage is human-ratio gated in
        # detection.scan_region.
        cfgv["hpose_mode"] = True
        cfgv["subject_only"] = True
        cfgv["subject_priority"] = True
        cfgv["accent_only"] = False
        cfgv["blob_watch"] = True
        cfgv["scan_budget_ms"] = max(80, min(180, int(
            self.cfg.get("scan_budget_ms", 350) or 350)))
        try:
            res = scan_region(
                img, vw // 2, vh // 2, cfgv,
                max(1, max(vw, vh) // 2), origin=(vx, vy))
        except Exception as exc:
            if now - self._track_target_last_log > 5.0:
                self._track_target_last_log = now
                log("Track target scan error: %r" % (exc,))
            return None
        if res is None or res.kind != "target" or res.target is None:
            return None
        target = res.target
        source = getattr(target, "source", "classical")
        # Track target acquires only from confirmed humans (YOLO detect or
        # pose fallback).  Classical / high-contrast blobs are never a track
        # source, so the view cannot be rotated onto a crosshair or bullet.
        if source not in ("detect", "pose"):
            return None
        ax = cx + float(target.dx)
        ay = cy + float(target.dy)
        bw = max(1.0, float(target.bw))
        bh = max(1.0, float(target.bh))
        self._track_target_seed(
                img, (vx, vy),
                (ax - bw * 0.5, ay - bh * 0.5, bw, bh),
                True,
                int(getattr(target, "track_id", 0) or 0))
        return target

    def _track_target_view_delta(self, dx, dy):
        """Speed-limited raw counts for Track target (no user settings)."""
        response = max(0.25, min(4.0, float(
            getattr(self, "_steer_response", 1.0) or 1.0)))
        max_px = TRACK_TARGET_SPEED_PX_S * ASSUMED_FRAME_SECONDS
        sx = math.copysign(min(abs(float(dx)), max_px), float(dx))
        sy = math.copysign(min(abs(float(dy)), max_px), float(dy))
        sx /= response
        sy /= response
        cap = TRACK_TARGET_SAFETY_MAX_PX / response
        return self._cap_step(sx, sy, cap)

    def _track_target_steer(self, dx, dy):
        counts = self._track_target_view_delta(dx, dy)
        if counts == (0, 0):
            return False
        if bool(self.cfg.get("unibot_aim", True)):
            return self._nudge(counts[0], counts[1], view=True)
        if self._right_stick_aim(counts[0], counts[1], hold_gain=False):
            self._mark_view_moved()
            return True
        return self._nudge(counts[0], counts[1], view=True)

    def _track_target_loop(self):
        """Keep the selected human's origin at the exact screen centre.

        X is a zero-pixel lock.  Y is the requested five-pixel upward aim
        offset.  The loop runs at 60 Hz, sends at most one command per fresh
        colour/detector measurement, and ignores every normal assist setting
        (radius, deadband, gain, mode, pause) except the physical input
        backend.  It never runs a new person detection while the acquired
        colour is visible.
        """
        # Response-learning ledger for the Track target loop, mirroring the
        # hold-steering ledger so that _steer_response stays calibrated even
        # though Track target ignores normal assist settings.
        ledger = {"cmd_pending": False, "last_cmd_at": 0.0,
                  "last_cmd_offset": (0.0, 0.0), "last_cmd_step": (0.0, 0.0),
                  "last_cmd_seq": 0}
        try:
            while not self.stop.is_set():
                with self._state_lock:
                    active = self._track_target_active
                if not active:
                    return
                # Bypass stops Track target just like any other assistance:
                # the user's physical override takes precedence.
                if self._bypass_enabled() and self._bypass_held():
                    with self._state_lock:
                        self._track_target_active = False
                        self._track_target_color = None
                    return
                now = time.perf_counter()
                vx, vy, vw, vh = self._virtual_screen()
                cx = vx + vw // 2
                cy = vy + vh // 2
                sample, tracked = self._track_target_color_measurement(now)
                with self._state_lock:
                    tracker = self._track_target_color
                if sample is not None:
                    target_x = float(sample.x)
                    target_y = float(sample.y)
                    if tracker is not None:
                        try:
                            lx, ly = tracker.lead(now)
                            target_x += lx
                            target_y += ly
                        except Exception:
                            pass
                    # Learn view-pixels-per-count from the last command using
                    # the fresh colour measurement.  This keeps Track target
                    # calibrated even though it runs outside normal settings.
                    if ledger.get("cmd_pending"):
                        self._observe_steer_response(ledger, sample, now)
                elif tracker is not None:
                    # Brief colour loss: keep steering toward the last known
                    # centre so a momentary occlusion or colour shift doesn't
                    # break the lock.  Re-acquisition only happens after the
                    # tracker has cleared (12 consecutive misses).
                    px, py = tracker.last_center
                    target_x = px
                    target_y = py
                else:
                    # Colour lock cleared: start a new human-first chain.
                    acquired = self._track_target_acquire(now)
                    if acquired is not None:
                        if hasattr(acquired, "x") and hasattr(acquired, "w"):
                            sx = float(acquired.x)
                            sy = float(acquired.y)
                        else:
                            # scan_region SnapTarget is centre-relative.
                            sx = cx + float(acquired.dx)
                            sy = cy + float(acquired.dy)
                        target_x = sx
                        target_y = sy
                    else:
                        target_x = target_y = None
                if target_x is not None:
                    dx = int(round(target_x - cx))
                    dy = int(round(target_y - cy + TRACK_TARGET_AIM_OFFSET_Y))
                    if dx or dy:
                        counts = self._track_target_view_delta(dx, dy)
                        if counts != (0, 0):
                            # Store command info for response learning on the
                            # next fresh measurement.
                            try:
                                seq = int(getattr(sample, "seq", 0) or 0)
                            except (TypeError, ValueError):
                                seq = 0
                            ledger["last_cmd_seq"] = seq
                            ledger["last_cmd_at"] = now
                            ledger["last_cmd_offset"] = (float(dx), float(dy))
                            ledger["last_cmd_step"] = (float(counts[0]),
                                                         float(counts[1]))
                            ledger["cmd_pending"] = True
                            if bool(self.cfg.get("unibot_aim", True)):
                                self._nudge(counts[0], counts[1], view=True)
                            elif self._right_stick_aim(counts[0], counts[1],
                                                       hold_gain=False):
                                self._mark_view_moved()
                            else:
                                self._nudge(counts[0], counts[1], view=True)
                if self.stop.wait(TRACK_TARGET_INTERVAL_S):
                    break
        except Exception as exc:
            if time.perf_counter() - self._track_target_last_log > 5.0:
                self._track_target_last_log = time.perf_counter()
                log("Track target loop error: %r" % (exc,))
        finally:
            with self._state_lock:
                self._track_target_active = False
                self._track_target_color = None
                self._track_target_misses = 0

    def _scan_center(self, state=None, expected_track_id=None):
        """Scan the watched centre square; return (target, mx, my).

        target.dx/dy are CENTRE-relative (the scan anchor is the screen
        middle, so the absolute sighting is mx + dx, my + dy) - every
        caller must convert through the anchor, never through the
        capture origin. Ranking is the engine default: high-contrast
        items first, the accent colour only boosts (or, with
        accent_only, filters). None when the switch is off, the frame
        is stale, or nothing is inside.
        """
        res = None
        box = self._center_box()
        if box is None:
            return None
        mx, my, half = box
        if state is not None and state.get("abandoned"):
            return None

        cap = max(48, half * 2)
        yolo_on = self._yolo26n is not None
        yolo_limit = self._sync_yolo_search_radius() if yolo_on else 0
        # With YOLO active inside its validated inner gate, YOLO handles
        # detection and its asynchronous pose worker handles pose fallback.
        # A wider GUI radius gets the classical outer fallback.
        fallback_on = not yolo_on or half > yolo_limit

        colour_hit = False

        with self._state_lock:
            colour_lock = getattr(self, "_target_lock", None)

        # Consume at most one fresh YOLO sighting per worker thread.
        yolo_det = None
        if yolo_on:
            try:
                last_seq = getattr(self._yolo_local, "seq", 0)
                yolo_det = self._yolo26n.get_latest_detection(
                    after_seq=last_seq,
                    max_age=self._detection_max_age_seconds())
                if yolo_det is not None:
                    self._yolo_local.seq = yolo_det.seq
                    yolo_detected = True
                    if not bool(self.cfg.get("hpose_mode", True)) \
                            and getattr(yolo_det, "source", "") == "pose":
                        raw_getter = getattr(
                            self._yolo26n, "get_last_measurements", None)
                        raw = raw_getter() if callable(raw_getter) else ()
                        fast = [item for item in raw
                                if getattr(item, "source", "") == "detect"
                                and int(getattr(item, "seq", 0) or 0)
                                == int(yolo_det.seq)]
                        yolo_det = max(
                            fast,
                            key=lambda item: (float(getattr(
                                item, "quality", 0.0) or 0.0),
                                              float(getattr(
                                                  item, "conf", 0.0) or 0.0)),
                            default=None,
                        )
            except Exception as exc:
                log("YOLO26n read error: %r" % (exc,))
                yolo_det = None

        res = None
        img = None
        cx = cy = 0
        origin = None
        edges = None

        # --- PATH 1: YOLO has a fresh detection ---------------------------------
        # YOLO is the source of truth.  When it sees the person we use it
        # directly - no capture needed for the result.  We capture only to
        # seed/update the colour tracker so it stays fresh for when YOLO
        # misses.
        if yolo_det is not None:
            try:
                res = self._yolo26n_to_scan_result(yolo_det, mx, my)
                if res is not None and res.kind == "target":
                    now_l = time.perf_counter()
                    if now_l - self._yolo_log_at >= 1.0:
                        self._yolo_log_at = now_l
                        log("YOLO26n: found person at dx=%d, dy=%d, "
                            "conf=%.2f" % (res.target.dx, res.target.dy,
                                           res.target.hscore))
                else:
                    res = None
            except Exception as exc:
                log("YOLO26n scan error: %r" % (exc,))
                res = None

            # Capture for colour tracker seed/update when YOLO detects.
            # Capture failure is non-fatal - YOLO result is still returned.
            img, cx, cy, origin, edges = self.grab_around(mx, my, cap)
            if img is not None and not (state is not None and state.get("abandoned")):
                # Update the colour tracker with the captured frame when YOLO
                # has a detection.  This keeps the HSV model fresh so it can
                # take over immediately when YOLO misses.
                if (res is not None and res.kind == "target"
                        and colour_lock is not None):
                    try:
                        colour_lock.update(img, origin,
                                           now=time.perf_counter())
                    except Exception:
                        pass

                # Seed colour lock from YOLO if we don't have one yet.
                if (res is not None and res.kind == "target"
                        and getattr(self, "_target_lock", None) is None):
                    self._seed_target_lock(
                        res.target, img, origin, mx, my, True,
                        int(getattr(res.target, "track_id", 0) or 0))

                # Record YOLO target position in shared motion history so
                # contrast fallback can compare movement across detection types.
                if res is not None and res.kind == "target" \
                        and res.target is not None:
                    now_m = time.perf_counter()
                    self._motion_history.append((
                        mx + float(res.target.dx),
                        my + float(res.target.dy),
                        now_m))

        # --- PATH 2: YOLO missed, colour lock exists ---------------------------
        # Update the colour tracker so it stays fresh.  The colour lock is the
        # ONLY following mechanism after a human has been acquired - it must
        # not be replaced by random contrast blobs.
        if res is None and colour_lock is not None:
            img2, cx2, cy2, origin2, edges2 = self.grab_around(mx, my, cap)
            if img2 is not None and not (state is not None and state.get("abandoned")):
                img, cx, cy, origin, edges = img2, cx2, cy2, origin2, edges2
                try:
                    sample = colour_lock.update(img, origin,
                                                now=time.perf_counter())
                except Exception as exc:
                    sample = None
                    if time.perf_counter() - self._yolo_log_at >= 5.0:
                        self._yolo_log_at = time.perf_counter()
                        log("Colour lock update failed: %r" % (exc,))
                if sample is not None:
                    res = detection.ScanResult(
                        "target", _ColorScanTarget(sample, mx, my))
                    colour_hit = True
                    with self._state_lock:
                        self._target_lock_misses = 0
                    # Record colour-lock target position in shared motion
                    # history so YOLO/contrast can compare movement across
                    # detection types.
                    now_m = time.perf_counter()
                    self._motion_history.append((
                        float(sample.x), float(sample.y), now_m))
                else:
                    with self._state_lock:
                        self._target_lock_misses += 1
                        if self._target_lock_misses > 12:
                            self._target_lock = None
                            colour_lock = None

        # --- PATH 3: YOLO missed, no colour lock (or tracker lost) ------------
        # High-contrast / classical fallback is ONLY for initial acquisition
        # when no human has been detected yet.  It must never fire while a
        # colour lock exists, and it runs on a long cooldown so it does not
        # spam false positives every frame.
        #
        # Contrast detection prioritises MOVING targets: a target that is
        # walking across the screen is far more likely to be a human than a
        # stationary crosshair or HUD element.  Once a moving target is
        # acquired, the hold-steering loop constantly rotates the view so the
        # target becomes and stays at the exact screen centre - same as human
        # YOLO detections.
        if res is None and colour_lock is None and fallback_on:
            run_fallback = time.perf_counter() >= float(
                getattr(self, "_fallback_next_at", 0.0) or 0.0)
            if run_fallback:
                if img is None:
                    img, cx, cy, origin, edges = self.grab_around(mx, my, cap)
                    if img is None:
                        return None
                contrast_collect = []
                try:
                    fallback_cfg = dict(self.cfg.values)
                    # Pose is handled by YOLO's async worker - never re-run
                    # the legacy synchronous pose stage here.
                    if yolo_on and bool(self.cfg.get("hpose_mode", True)):
                        fallback_cfg["hpose_mode"] = False
                    fallback_cfg["subject_only"] = True
                    fallback_cfg["subject_priority"] = True
                    fallback_cfg["blob_watch"] = True
                    fallback_cfg["accent_only"] = False
                    # Short budget: classical scan must not block the loop.
                    fallback_cfg["scan_budget_ms"] = min(
                        float(fallback_cfg.get("scan_budget_ms", 350) or 350),
                        25.0)
                    res = scan_region(img, cx, cy, fallback_cfg, cap,
                                      follow_ctx=None,
                                      edge_flags=edges,
                                      anchor_blind_area=0,
                                      motion_ctx=None,
                                      focus_bias=0.0,
                                      subject_ctx=None,
                                      origin=origin,
                                      collect=contrast_collect)
                except Exception as exc:
                    with self._state_lock:
                        self.fail_count += 1
                        if self.fail_count == 1:
                            log("centre scan error: %r" % (exc,))
                        if self.fail_count >= 5:
                            self.scanner_dead = True
                            log("Scanner disabled after repeated errors. "
                                "Clicks pass through.")
                    return None
                with self._state_lock:
                    self.fail_count = 0
                    self.scanner_dead = False

                # Pick the best MOVING target from the contrast candidates.
                # Stationary clutter (crosshair, HUD) is deprioritised in
                # favour of targets that are actually walking across the
                # screen - those are the humans we want to track.
                now = time.perf_counter()
                best_target = None
                best_motion_key = None
                for _s, _ddx, _ddy, _d, _a, _ct, _bw, _bh in contrast_collect:
                    try:
                        if not targeting.humanoid_ok(_bw, _bh):
                            continue
                        bh_float = float(_bh)
                        if bh_float < 5.0 or bh_float > 20.0:
                            continue
                    except Exception:
                        continue
                    # Motion score: distance from the last tracked target
                    # position (any detection type).  Moving targets get
                    # priority over stationary clutter.
                    abs_x = cx + _ddx
                    abs_y = cy + _ddy
                    motion = self._motion_score(abs_x, abs_y, now)
                    # Key: moving targets first (higher motion = better), then
                    # closer to centre (smaller dist = better).
                    motion_key = (-motion, float(_d))
                    if best_motion_key is None or motion_key < best_motion_key:
                        best_motion_key = motion_key
                        best_target = (_ddx, _ddy, _bw, _bh, _ct, _a, abs_x,
                                       abs_y, motion)

                if best_target is not None:
                    _ddx, _ddy, _bw, _bh, _ct, _a, abs_x, abs_y, motion = best_target
                    # Record position for motion tracking on next frame.
                    self._motion_history.append((abs_x, abs_y, now))
                    # Build a ScanResult from the chosen moving target.
                    target = SnapTarget(
                        int(round(_ddx)), int(round(_ddy)),
                        math.hypot(_ddx, _ddy), float(_a),
                        float(_ct), int(round(_bw)), int(round(_bh)))
                    target.source = "classical"
                    target.observed_at = now
                    res = ScanResult("target", target, 0.0)
                    # Shorter cooldown when we found a moving target - keep
                    # tracking it actively instead of waiting a full second.
                    self._fallback_next_at = now + 0.050
                elif res is None or res.kind != "target":
                    # Long cooldown: contrast fallback is for initial
                    # acquisition only, not continuous detection.  One try
                    # per second keeps false positives from spamming.
                    self._fallback_next_at = now + 1.0

        # --- Human-only gate ----------------------------------------------------
        # Model person and pose results are human evidence.  Blob/classical
        # targets must pass the upright 1:2-1:5 silhouette gate in real
        # screen pixels - crosshair, bullet, muzzle flash, HUD dots never
        # pass this point.
        #
        # Size-based gating: targets >=20px tall are reliably detected by YOLO,
        # so contrast fallback should not override them.  Small targets between
        # 5px and 20px tall are where YOLO struggles and color/contrast
        # tracking is essential.  Anything under 5px is too small to track
        # reliably and is rejected.
        if res is not None and res.kind == "target" \
                and res.target is not None:
            target = res.target
            source = str(getattr(target, "source", "") or "")
            model_confirmed = source in ("detect", "pose")
            if not model_confirmed:
                try:
                    if not targeting.humanoid_ok(target.bw, target.bh):
                        res = None
                        target = None
                except Exception:
                    res = None
                    target = None
            if target is not None:
                th = float(target.bh)
                if model_confirmed:
                    # YOLO/pose detections are trusted at any size.
                    pass
                elif th < 5.0:
                    # Too small to track reliably - reject regardless of
                    # contrast score.
                    res = None
                    target = None
                elif th > 20.0:
                    # Large targets should be caught by YOLO; contrast
                    # fallback on them would introduce false positives.
                    res = None
                    target = None
                else:
                    # Small human (5-20px): acceptable via color/contrast.
                    pass
            if target is not None and model_confirmed:
                # Only confirmed humans seed the colour lock.
                if getattr(self, "_target_lock", None) is None:
                    self._seed_target_lock(
                        target, img, origin, mx, my, True,
                        int(getattr(target, "track_id", 0) or 0))

        # --- Radius gate --------------------------------------------------------
        if res is not None and res.kind == "target" \
                and res.target is not None:
            limit = self._configured_search_radius_px()
            if math.hypot(float(res.target.dx),
                          float(res.target.dy)) > limit:
                res = None

        # --- Track-ID gate ------------------------------------------------------
        if res is not None and res.kind == "target" \
                and res.target is not None and expected_track_id:
            candidate_id = int(getattr(res.target, "track_id", 0) or 0)
            if candidate_id > 0 and candidate_id != int(expected_track_id):
                if state is not None:
                    state["track_mismatch"] = True
                res = None

        # --- Debug visualisation ------------------------------------------------
        if bool(self.cfg.get("debug_visualize", False)):
            try:
                vx, vy, vw, vh = self._virtual_screen()
                dbg_cx, dbg_cy = vx + vw // 2, vy + vh // 2
                target_center = None
                if res is not None and res.kind == "target" \
                        and res.target is not None:
                    if origin is None and yolo_det is not None:
                        target_center = (int(yolo_det.x), int(yolo_det.y))
                    elif origin is not None:
                        acx = origin[0] + cx + res.target.dx
                        acy = origin[1] + cy + res.target.dy
                        target_center = (int(acx), int(acy))
                candidates = []  # debug collection disabled in fast path
                self._debug_vis = (
                    dbg_cx, dbg_cy,
                    half,
                    target_center,
                    candidates,
                )
            except Exception:
                pass
        else:
            self._debug_vis = None

        # --- Return -------------------------------------------------------------
        if res is not None and res.kind == "target" \
                and res.target is not None:
            ax = mx + res.target.dx
            ay = my + res.target.dy
            self._record_motion_observation(res.target, ax, ay)
            return (res.target, mx, my)
        return None

    def scan_near(self, x, y, state=None, track=None):
        """
        Scan around cursor and return SnapTarget or None.

        track: optional deque fed with (t, abs_x, abs_y) observations of
        the chosen target - the motion-lead history. Only the click
        workers pass it; the follow worker's scans stay out of it. The
        centre scan itself (_scan_center) still feeds the SHARED
        self._track / Kalman history from every centre sighting -
        including follow/hold cycles - by design, so "Lead moving
        items" stays fresh mid-chase; the `track` argument only controls
        the per-call deque, not the shared history.
        With "Watch the screen centre only" the returned target's
        dx/dy are CENTRE-relative (plant them as mx + dx, my + dy with
        the box from _center_box()); otherwise they are
        cursor-relative as always.
        """
        if state is not None and state.get("abandoned"):
            return None

        box = self._center_box()
        if box is not None:
            mx, my, half = box

            if (abs(x - mx) > half or abs(y - my) > half) \
                    and not self._reticle_active() \
                    and not bool(state and state.get("block_unconfirmed")):
                if state is not None:
                    state["center_pass"] = True
                return None

            expected_track_id = None
            if state is not None:
                expected_track_id = state.get("track_id")
            got = self._scan_center(
                state,
                expected_track_id=expected_track_id,
            )
            if got is None:
                return None
            if state is not None:
                track_id = int(getattr(got[0], "track_id", 0) or 0)
                if track_id > 0:
                    state["track_id"] = track_id
            return got[0]

        radius = self.radius_px()
        cap = max(48, int(radius))

        img, cx, cy, origin, edges = self.grab_around(x, y, cap)

        if state is not None and state.get("abandoned"):
            return None

        if img is None:
            return None

        follow_ctx = None
        if bool(self.cfg.get("follow_enabled")):
            try:
                follow_secs = float(self.cfg.get("follow_seconds", 20) or 20)
            except (TypeError, ValueError):
                follow_secs = 20.0
            follow_ctx = self.follow.context(
                origin[0], origin[1],
                follow_secs
            )

        try:
            subject_ctx = self._subject_context(origin)
        except Exception:
            subject_ctx = None
        dbg_collect = [] if bool(self.cfg.get("debug_visualize", False)) \
            else None
        try:
            res = scan_region(img, cx, cy, self.cfg.values, cap,
                              follow_ctx=follow_ctx,
                              edge_flags=edges,
                              subject_ctx=subject_ctx,
                              origin=origin,
                              collect=dbg_collect)
        except Exception as exc:
            with self._state_lock:
                self.fail_count += 1
                dead = self.fail_count >= 5
                if dead:
                    self.scanner_dead = True
            log("scan error: %r" % (exc,))
            if dead:
                log("Scanner disabled after repeated errors. Clicks pass through.")
            return None

        with self._state_lock:
            self.fail_count = 0
            self.scanner_dead = False

        if dbg_collect is not None:
            try:
                target_box = None
                if res is not None and res.kind == "target" and res.target is not None:
                    acx = origin[0] + cx + res.target.dx
                    acy = origin[1] + cy + res.target.dy
                    target_box = (
                        int(acx - res.target.bw / 2),
                        int(acy - res.target.bh / 2),
                        int(acx + res.target.bw / 2),
                        int(acy + res.target.bh / 2),
                    )
                # target_box is a box (x1,y1,x2,y2); overlay expects center point
                target_center = None
                if target_box:
                    target_center = (
                        (target_box[0] + target_box[2]) // 2,
                        (target_box[1] + target_box[3]) // 2,
                    )
                self._debug_vis = (x, y, self.cfg.get("radius", 150), target_center,
                                   _debug_boxes(dbg_collect, origin, cx, cy))
            except Exception:
                pass
        else:
            self._debug_vis = None

        if res.ms > 90 and time.perf_counter() - self._slow_log_at > 30.0:

            self._slow_log_at = time.perf_counter()
            log("Slow scan: %.0f ms at radius %d px. The full search "
                "distance is always scanned - lower the radius if "
                "clicks feel late." % (res.ms, cap))

        if state is not None and state.get("abandoned"):
            return None

        if res is not None and res.kind == "target" and res.target is not None:
            if track is not None and track is not False:
                ax, ay = x + res.target.dx, y + res.target.dy
                self._record_motion_observation(res.target, ax, ay)
            try:
                ax0, ay0 = x + res.target.dx, y + res.target.dy
                self._subject_store(ax0, ay0, res.target)
            except Exception:
                pass
            return res.target

        return None

    def _role_button(self, key, default):
        """The button index a mouse role is bound to, with its fallback.

        Read per event rather than cached: a role can be rebound in the GUI
        while the hook is installed, and a cached index would keep snapping on
        the button the user just moved away from.
        """
        name = str(self.cfg.get(key, default) or "").strip().lower()
        index = MOUSE_BUTTON_INDEX.get(name)
        if index is None:
            return MOUSE_BUTTON_INDEX[default]
        return index

    def _trigger_button(self):
        """The button assistance listens to (default left)."""
        return self._role_button("trigger_button", "mouse1")

    def _options_button(self):
        """The extra-options button index, or None when it cannot have one.

        None on a collision with the trigger: one press cannot be both roles,
        and the trigger is the one that must keep working. config._sanitize
        repairs the collision at load; this covers a write made at runtime.
        """
        index = self._role_button("options_button", "mouse2")
        return None if index == self._trigger_button() else index

    def _options_vk(self):
        """VK code for the physical button currently bound to the options role.

        Returns 0 when the options role collides with the trigger (no
        separate options key exists), so consumers can distinguish "no
        options key" from "right button is the options key."
        """
        index = self._options_button()
        if index is None:
            return 0
        return {
            BTN_LEFT: 0x01,    # VK_LBUTTON
            BTN_RIGHT: 0x02,   # VK_RBUTTON
            BTN_MIDDLE: 0x04,  # VK_MBUTTON
            BTN_X1: 0x05,      # VK_XBUTTON1
            BTN_X2: 0x06,      # VK_XBUTTON2
        }[index]

    def _button(self, down, index=None):
        """One transition of the assist button through the active backend.

        Which button that is comes from trigger_button, so a role moved onto
        the middle or a side button is delivered as the button the user really
        pressed: a suppressed middle click must come back as a middle click,
        never as a left one.

        The backend's button() reports whether the stroke really went
        out (False = it degraded mid-call). A lost transition would
        leave the logical button stuck (down never followed by up), so
        on any refusal we hand THIS event to Win32 SendInput right away.

        Driver-level backends ledger the transition themselves, so when
        it arrives back at our own hook (no injected mark) it is
        consumed there instead of corrupting the tracked button state
        (a synthetic UP ending a real hold) or queueing phantom jobs.
        """
        if index is None:
            index = self._trigger_button()
        if not self._input.button(down, index):
            inject_mouse(index, down)

    def _warp(self, x, y):
        """Move the cursor through the active input backend."""
        self._input.move(x, y)

    def _mark_view_moved(self):
        """Invalidate 2D histories after this process rotates the camera."""
        compensate = bool(self.cfg.get("motion_camera_comp", True))
        with self._state_lock:
            self._view_moved = True
            if compensate:
                self._view_epoch += 1
        if not compensate:
            return
        self._reset_motion_history()
        try:
            notify = getattr(self._yolo26n, "notify_view_moved", None)
            if callable(notify):
                notify()
        except Exception:
            pass

    def _steer_view(self, dx, dy, hold=False):
        """One view-steering step through the configured driver.

        unibot_aim=True (the default) uses the active mouse input
        backend - Win32 or Interception - exactly like every other
        assistance path.  unibot_aim=False uses the ViGEm virtual right
        stick when it is available, and falls back to the same mouse
        backend otherwise. ``hold`` is true for held steering; the X/Y gain
        controls apply there.  Single clicks use the same target-height and
        ADS/hipfire base, but not the hold X/Y gain.
        """
        if not hold:
            dx, dy = self._one_shot_view_delta(dx, dy)
        if bool(self.cfg.get("unibot_aim", True)):
            return self._nudge(dx, dy, view=True, hold_gain=hold)
        if self._right_stick_aim(dx, dy, hold_gain=hold):
            if dx or dy:
                self._mark_view_moved()
            return True
        return self._nudge(dx, dy, view=True, hold_gain=hold)

    def _nudge(self, dx, dy, view=False, hold_gain=False):
        """One RELATIVE steering step through the active backend.

        This is the hold-steering primitive (the standalone addon's
        mouse.move): the delta rotates the view in raw-input games
        and walks the pointer on the desktop, without teleporting.
        A failed step is just a skipped frame - the next cycle
        retries from a fresh sighting.

        view=True marks that the GAME VIEW rotated (reticle-flight
        nudges only): the motion-sense baseline is stale afterwards,
        so the next centre scan refreshes it instead of comparing.
        Desktop cursor walks never set it (the world didn't move).

        hold_gain is deliberately independent of ``view``. Camera movement
        does not imply gain scaling: click snaps and manual pad movement are
        immediate raw deltas; only Mode-2 held-target steering is scaled.
        """
        if hold_gain:
            dx, dy = self._scaled_view_delta(dx, dy)
        self._steer_local.delta = (int(dx), int(dy))
        sent = False
        try:
            sent = bool(self._input.nudge(dx, dy))
        except Exception as exc:
            log("nudge error: %r" % (exc,))
        if not sent and (dx or dy):
            # Backend refused - do not fabricate a relative move through
            # win32rt (inject_mouse takes button/down, not dx/dy).
            # Log once per 5 seconds so the user sees why steering
            # silently doesn't rotate.
            if time.perf_counter() - self._nudge_fail_log_at > 5.0:
                self._nudge_fail_log_at = time.perf_counter()
                log("nudge failed: backend refused (dx=%d dy=%d). "
                    "Check input_backend configuration." % (dx, dy))
        if view and sent and (dx or dy):
            self._mark_view_moved()
        return sent

    def _atomic_click(self, index=0):
        """Try to deliver a whole down+up click as one packet through the
        active backend (Interception sends both strokes in a single
        kernel transaction - borrowed from connorabs' osu!mania engine).
        Returns True if the complete click went out. Win32 has no such
        packet API and returns False, so callers use the classic two
        transitions. On ANY False nothing was delivered as a pair, so
        the caller's per-event _button path still guards each half."""
        fn = getattr(self._input, "send_click", None)
        if fn is None:
            return False
        try:
            return bool(fn(index))
        except Exception:
            return False

    def send_click(self, x, y, double=False, index=None, move=True):
        if index is None:
            index = self._trigger_button()

        # For game mode (reticle active): use relative nudge to rotate view
        # For desktop: absolute warp.  move=False is the reinjection path:
        # a suppressed click must be returned where the user aimed, never
        # steered toward a stale desktop cursor position.
        if self._reticle_active():
            if move:
                vx, vy, vw, vh = self._virtual_screen()
                cx = vx + vw // 2
                cy = vy + vh // 2
                dx = x - cx
                dy = y - cy
                # Only steer for a target inside the watched area.  The
                # hidden OS cursor can sit anywhere; using its position as
                # a delta is how a single click used to spin the view.
                # Route through _one_shot_view_delta so ADS compensation and
                # response scaling apply instead of sending raw screen pixels
                # as mouse counts (which under-rotates under ADS zoom).
                ndx, ndy = self._one_shot_view_delta(dx, dy)
                if ndx or ndy:
                    self._nudge(ndx, ndy, view=True)
        else:
            if move:
                self._warp(x, y)
        time.sleep(0.012)

        if not self._atomic_click(index):
            self._button(True, index)
            time.sleep(0.015)
            self._button(False, index)

        if double:
            time.sleep(0.055)
            if not self._atomic_click(index):
                self._button(True, index)
                time.sleep(0.015)
                self._button(False, index)

    def _click_here(self):
        """One down+up click at the CURRENT cursor, no warp.

        The glide-abort path's delivery: the user already moved the
        cursor by hand, so warping back to the snap point would fight
        them. Same backend accounting as send_click (atomic packet
        first, split transitions after) - the two delivery paths must
        never diverge.
        """
        time.sleep(0.012)
        index = self._trigger_button()
        if not self._atomic_click(index):
            self._button(True, index)
            time.sleep(0.015)
            self._button(False, index)

    def _click_at_reticle(self, index=None):
        """One down+up click at the current reticle position, no warp.

        The flight-mode click: the view rotated to the target, the click
        goes where the reticle now points.
        """
        if index is None:
            index = self._trigger_button()
        if not self._atomic_click(index):
            self._button(True, index)
            time.sleep(0.015)
            self._button(False, index)

    def _follow_may_learn(self, target):
        """Guarded entry: a raise here must never cost the user's click.
        Callers may have claimed the delivery already, and the outer error
        handler would then skip the reinject (the claim is taken) - so
        every failure degrades to "do not learn", never to a lost click."""
        try:
            return self._follow_may_learn_inner(target)
        except Exception as exc:
            log("follow learn check error: %r" % (exc,))
            return False

    def _follow_may_learn_inner(self, target):
        """
        Decide whether a successful snap may be remembered.

        With follow_learn = "accent" (default) and an accent colour
        configured, only accent-matching items are learned - so a
        misclick that snapped to some other item never poisons the
        memory: it keeps following the colour you configured.

        subject_only (the strict game mode) adds a SHAPE gate on top: in a
        shooter the memory must never learn the gun sight's grey or the
        foliage green it just snapped to instead of an enemy - only figure
        tier-2 picks teach the memory, everything else is forgotten.
        subject_priority alone is a soft RANKING preference and does not
        block learning, so the desktop follow memory (scrollbars and
        repeated clicks) works with the default settings.
        (With accent colours configured the colour gate still applies
        as well; the two combine.)
        """
        if bool(self.cfg.get("subject_only", False)):
            try:
                hs = float(getattr(target, "hscore", 0.0) or 0.0)
            except (TypeError, ValueError):
                hs = 0.0
            if hs >= 0.50:
                pass
            else:
                try:
                    tier = _shape_tier(target.bw, target.bh,
                                       getattr(target, "area", 0) or 0)
                except Exception:
                    return False
                if tier != 2:
                    return False
        learn = str(self.cfg.get("follow_learn", "accent")).lower()
        if learn == "any":
            return True

        accents = _parse_accents(self.cfg.get("accent_color", "off"))
        if not accents:
            return True

        # A colour-lock target is human-seeded: its HSV is the target's
        # colour, so it always passes the accent gate (the follow memory
        # learns exactly the colour the user is tracking).
        if str(getattr(target, "source", "") or "") == "color":
            return True

        try:
            tol = float(self.cfg.get("accent_tolerance", 26))
        except Exception:
            tol = 26.0
        return _sig_matches_accents(
            getattr(target, "sig", None), accents,
            tol,
            accent_soft_of(self.cfg),
            accent_matcher_of(self.cfg),
            variants=accent_variants_of(self.cfg),
        )

    def glide_cursor(self, nx, ny):
        """
        Move the cursor to (nx, ny).
        Gliding has been removed; the cursor jumps instantly to (nx, ny)
        for lowest latency.

        Returns True if the cursor reached (nx, ny).
        """
        self._warp(nx, ny)
        return True

    def reinject_click(self, x, y):
        """
        Give back a click that classic mode suppressed but did not assist.
        """
        try:
            self.send_click(x, y, double=False, move=False)
        except Exception as exc:
            log("reinject error: %r" % (exc,))

    def _claim_delivery(self, state):
        """
        Exactly-once resolution guard for a suppressed classic click.

        The disposable worker and the scanner's stall path race: without
        this, a worker that finishes just after the 1 s join timeout
        could deliver its snap click ON TOP of the click the scanner
        already returned (a double click from one physical press). A
        resolution may deliver a click, return it raw, or deliberately keep
        an unconfirmed in-game Mode-1 trigger blocked. Only one path may make
        that decision.
        """
        lock = state.get("lock")
        if lock is None:
            return True
        with lock:
            if state.get("abandoned") or state.get("delivered"):
                return False
            state["delivered"] = True
            return True

    def _epoch_alive(self, state):
        """True while the job's mode is still current.

        state["epoch"] is stamped at dequeue; _mode_epoch bumps on
        every mode switch. A mismatch means the mode died mid-job:
        the worker must return the press raw (classic) or stand down
        (guide) instead of delivering stale-mode behaviour. An
        UNSTAMPED state (direct worker tests, legacy callers) fails
        open - no epoch, no opinion.
        """
        try:
            ep = state.get("epoch", None)
            if ep is None:
                return True
            return ep == self._mode_epoch
        except Exception:
            return True

    def _emergency_release(self):
        """
        Resolve classic clicks that were suppressed but never completed.

        Desktop/Mode-2 clicks are given back exactly once. An in-game Mode-1
        fire transaction remains blocked because it never reached confirmed
        exact centre.

        Queued (not yet started) jobs are returned by _drain_queue FIRST,
        so whatever is still in pending_classics afterwards belongs to the
        job currently in flight. That is precisely why this must not simply
        re-inject: before v8.49 it did, and a worker still inside
        scan_near() went on to deliver its own snap click on top of the
        returned one - TWO clicks from one physical press, which is the
        exact failure _claim_delivery() exists to prevent. Reachable by
        tapping a bare mode key ("1"/"2") during a drag, or by pausing
        while a click is being assisted.

        Each entry now carries its job's state dict, so this path claims
        through the same latch every other delivery path uses. An entry
        with no state (queued but never picked up) is only returned when no
        worker is active - if one is, the job is in flight and its state
        has not been attached yet, so returning it would duplicate.
        """
        with self._pending_lock:
            pending = self.pending_classics
            self.pending_classics = []
        for entry in pending:
            if entry.get("up") or entry.get("worker_done"):
                continue
            state = entry.get("state")
            if state is not None:
                if not self._claim_delivery(state):
                    continue
            elif self.worker_active:
                continue
            if self._entry_must_not_fire(entry):
                log("Cancelled Mode-1 trigger stayed blocked safely.")
                continue
            if entry.get("pad"):
                continue
            self.reinject_click(entry["x"], entry["y"])
            log("Suppressed click returned safely.")

        with self._state_lock:
            had_hold = self._center_hold is not None
            self._center_hold = None
            if had_hold:
                self._center_hold_tomb = time.perf_counter()

    def _drain_queue(self):
        """
        Discard queued jobs. A discarded classic job will never be delivered
        by a worker, so its suppression is resolved here: desktop/Mode-2
        clicks are returned, while unconfirmed in-game Mode-1 triggers remain
        blocked. The pending entry is removed too, so _emergency_release
        cannot resolve it a second time.
        """
        while True:
            try:
                item = self.click_q.get_nowait()
            except queue.Empty:
                break
            if item[0] == "classic":
                entry = item[4] if len(item) > 4 else None
                if entry is not None:
                    with self._pending_lock:
                        try:
                            self.pending_classics.remove(entry)
                        except ValueError:
                            pass
                    if self._entry_must_not_fire(entry):
                        log("Queued Mode-1 trigger stayed blocked safely.")
                        continue
                    if entry.get("pad"):
                        continue
                self.reinject_click(item[1], item[2])
                log("Queued click returned safely.")
            elif item[0] == "guide":
                log("Queued guide job dropped.")

    def _classic_lock_alive(self, state):
        """Whether a mode-1 feedback lock still owns an undelivered click."""
        if not self._epoch_alive(state):
            return False
        stop = getattr(self, "stop", None)
        if stop is not None and stop.is_set():
            return False
        if hasattr(self, "running") and not self.running:
            return False
        lock = state.get("lock")
        if lock is None:
            return not (state.get("abandoned") or state.get("delivered"))
        with lock:
            return not (state.get("abandoned") or state.get("delivered"))

    def _classic_mode1_job(self, state):
        """True only for a click that was captured while mode 1 was active.

        ``do_classic`` is also the short-click delivery primitive used by mode
        2.  Looking at the stamped job mode keeps mode 2's gradual steering
        semantics separate from mode 1's detect/lock/fire transaction.
        """
        mode = state.get("mode")
        if mode is None:
            mode = self.current_mode()
        return mode == 1

    def _mode1_trigger_must_not_fire(self, state):
        """Whether this Mode-1 job owns a trigger that must stay blocked.

        Only the in-game reticle path is a fire transaction.  Desktop Mode 1
        still warps the visible cursor and keeps its historical no-target
        pass-through; a game trigger, by contrast, must never produce an
        uncentred shot merely because the lock timed out.
        """
        if "block_unconfirmed" in state:
            return (self._classic_mode1_job(state)
                    and bool(state.get("block_unconfirmed")))
        return (self._classic_mode1_job(state)
                and self._center_box() is not None
                and self._reticle_active())

    @staticmethod
    def _entry_must_not_fire(entry):
        """True for a queued in-game Mode-1 fire transaction.

        This is stamped when the physical DOWN is captured.  Teardown may
        happen after the cursor becomes visible or the mode changes, so those
        later paths must not re-evaluate live UI state and accidentally return
        an uncentred shot.
        """
        return bool(entry and entry.get("mode") == 1
                    and entry.get("block_unconfirmed"))

    @staticmethod
    def _target_center_delta(target):
        """Integer correction from the reticle to the target's centre pixel.

        ``None`` means the target has no usable centre.  Never turn a missing
        measurement into ``(0, 0)``: that would release a Mode-1 trigger on a
        malformed or predicted result.
        """
        try:
            return (int(round(float(target.dx))),
                    int(round(float(target.dy))))
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None

    @classmethod
    def _target_measured(cls, target):
        """True only for current detector geometry, never a bridge frame."""
        return not (target is None
                    or bool(getattr(target, "predicted", False))
                    or getattr(target, "source", "") == "predicted")

    @classmethod
    def _human_target_ok(cls, target):
        """True only for a confirmed human measurement.

        During an assisted hold / Track target session the view may only be
        steered from a fast person detection, the pose fallback, or the
        persistent colour lock (seeded from a confirmed human).  Colour
        fallback detections from the YOLO backend are rejected at
        ``_yolo26n_to_scan_result`` so they never reach here as direct
        targets.  Classical/high-contrast blobs (crosshair, bullet, muzzle
        flash, HUD dot) must never rotate the view.
        """
        if target is None:
            return False
        source = str(getattr(target, "source", "") or "")
        return source in ("detect", "pose", "color")

    @classmethod
    def _target_center_confirmed(cls, target):
        """A measured target occupies the exact centre pixel.

        Predicted bridge frames are useful for steering but can never release
        a Mode-1 trigger: the click needs a detector measurement, not an
        extrapolation that merely happens to cross zero.
        """
        if not cls._target_measured(target):
            return False
        return cls._target_center_delta(target) == (0, 0)

    @staticmethod
    def _target_observed_after(target, moment):
        """Whether asynchronous geometry was captured after ``moment``.

        Classical targets have no capture stamp because they are produced by
        the synchronous grab that just ran, so they are fresh by construction.
        """
        try:
            observed = float(getattr(target, "observed_at", 0.0) or 0.0)
            moment = float(moment or 0.0)
        except (TypeError, ValueError):
            return False
        if moment <= 0.0 or observed <= 0.0:
            return True
        now = time.perf_counter()
        # A future-skewed clock (or synthetic observation) must not pass as
        # "fresh" - clamp to the same now + 0.05 guard used elsewhere.
        if observed > now + 0.05:
            return False
        return math.isfinite(observed) and observed >= moment

    @staticmethod
    def _hold_command_ready(ledger, target, now):
        """Whether a held-steer loop may send its next correction.

        A command gets one chance to appear in a newer detector frame.  If the
        frame is identical, replaying the same offset would double the turn;
        if the target has visibly moved (or a response deadline passed), the
        next measured offset is safe to use.
        """
        if not ledger.get("cmd_pending"):
            return True
        try:
            seq = int(getattr(target, "seq", 0) or 0)
        except (TypeError, ValueError):
            seq = 0
        if seq > 0 and seq <= int(ledger.get("last_seq", 0) or 0):
            return False
        elapsed = now - float(ledger.get("last_cmd_at", 0.0) or 0.0)
        if elapsed < CLASSIC_LOCK_RESPONSE_MIN_SECONDS:
            return False
        old_x, old_y = ledger.get("last_cmd_offset", (0.0, 0.0))
        response = math.hypot(float(target.dx) - old_x,
                              float(target.dy) - old_y)
        gate = max(2.0, 0.08 * math.hypot(old_x, old_y))
        return not (elapsed < CLASSIC_LOCK_RESPONSE_RETRY_SECONDS
                    and response < gate)

    def _hold_command_sent(self, ledger, target, now, step=None):
        """Remember the frame, offset, and actual scaled command."""
        try:
            seq = int(getattr(target, "seq", 0) or 0)
        except (TypeError, ValueError):
            seq = 0
        ledger["last_seq"] = seq
        ledger["last_cmd_seq"] = seq
        ledger["last_cmd_at"] = now
        ledger["last_cmd_offset"] = (float(target.dx), float(target.dy))
        if step is None:
            step = getattr(self._steer_local, "delta", (0.0, 0.0))
        try:
            ledger["last_cmd_step"] = (float(step[0]), float(step[1]))
        except (TypeError, ValueError, IndexError):
            ledger["last_cmd_step"] = (0.0, 0.0)
        ledger["cmd_pending"] = True

    def _observe_steer_response(self, ledger, target, now):
        """Learn view-pixels-per-count from the last held command."""
        if not ledger or not ledger.get("cmd_pending"):
            return False
        step = ledger.get("last_cmd_step")
        old = ledger.get("last_cmd_offset")
        if not step or not old:
            return False
        try:
            old_x, old_y = old
            sx, sy = step
            new_x = float(target.dx)
            new_y = float(target.dy)
            seq = int(getattr(target, "seq", 0) or 0)
            last_seq = int(ledger.get("last_cmd_seq", 0) or 0)
            elapsed = float(now) - float(
                ledger.get("last_cmd_at", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            return False
        if seq <= last_seq or elapsed < 0.016 or elapsed > 0.12:
            return False
        mag = math.hypot(sx, sy)
        if mag < 2.0 or mag > 28.0:
            return False
        observed = -((new_x - old_x) * sx + (new_y - old_y) * sy) / mag
        if not math.isfinite(observed) or observed < 0.4 \
                or observed > mag * 3.2:
            return False
        ratio = observed / mag
        if not (0.2 <= ratio <= 3.5):
            return False
        ads = bool(getattr(self, "_rbtn_down", False))
        if ads:
            old_response = float(
                getattr(self, "_steer_response_ads", 2.0) or 2.0)
            samples = int(getattr(self, "_steer_response_ads_n", 0) or 0)
            if samples < 4:
                new_response = ratio
            else:
                new_response = 0.8 * old_response + 0.2 * ratio
            self._steer_response_ads = min(4.0, max(0.25, new_response))
            self._steer_response_ads_n = samples + 1
        else:
            old_response = float(
                getattr(self, "_steer_response", 2.0) or 2.0)
            samples = int(getattr(self, "_steer_response_n", 0) or 0)
            if samples < 4:
                new_response = ratio
            else:
                new_response = 0.8 * old_response + 0.2 * ratio
            self._steer_response = min(4.0, max(0.25, new_response))
            self._steer_response_n = samples + 1
        ledger["last_obs_seq"] = seq
        return True

    @staticmethod
    def _fresh_target_sample(candidate, previous, command_at):
        """Whether ``candidate`` is newer than the frame a command used."""
        if candidate is None:
            return False
        try:
            seq = int(getattr(candidate, "seq", 0) or 0)
            old_seq = int(getattr(previous, "seq", 0) or 0)
        except (TypeError, ValueError):
            seq = old_seq = 0
        try:
            observed = float(getattr(candidate, "observed_at", 0.0) or 0.0)
            old_observed = float(
                getattr(previous, "observed_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            observed = old_observed = 0.0
        if observed > 0.0:
            # Sequence numbers describe backend order, not necessarily when a
            # buffered frame was captured. A higher sequence captured before
            # the steering command cannot confirm its result.
            if not math.isfinite(observed) or observed < command_at:
                return False
            if old_observed > 0.0 and observed <= old_observed:
                return False
            if seq > 0 and old_seq > 0 and seq <= old_seq:
                return False
            return True
        if seq > 0 and old_seq > 0:
            return seq > old_seq
        # Classical scans create a new target object for each captured frame.
        return candidate is not previous

    def _classic_reticle_lock(self, cbox, target, state):
        """Close the live 3D camera loop before a mode-1 click is delivered.

        A relative mouse delta is not a screen-pixel teleport: game
        sensitivity, render/input latency, and streaming delay decide how far
        the target actually moves.  Therefore every correction is followed by
        a *fresh* detector result.  A second correction is withheld until the
        rendered target has visibly responded, preventing delayed streamed
        frames from applying the same turn more than once.  The transaction's
        overall deadline remains the safety bound.

        Returns a small result dictionary.  It never clicks and never claims
        delivery; that remains the caller's exactly-once responsibility.
        """
        mx, my = cbox[0], cbox[1]
        deadline = time.perf_counter() + CLASSIC_LOCK_TIMEOUT_SECONDS
        corrections = 0
        last = target
        confirm_after = state.get("pressed_at", 0.0)
        last_ax = mx + target.dx
        last_ay = my + target.dy
        centered = False
        reason = "timeout"

        while self._classic_lock_alive(state):
            now = time.perf_counter()
            last_ax = mx + last.dx
            last_ay = my + last.dy
            if now >= deadline:
                reason = "timeout"
                break

            if not self._target_observed_after(last, confirm_after):
                # The async backend may still hold the frame that was latest
                # just before the physical press. Do not steer from it: wait
                # for the first measured post-press frame, then begin the
                # transaction from where the target actually is now.
                prior = last
                try:
                    fresh = self.scan_near(mx, my, state, False)
                except Exception as exc:
                    log("Classic lock scan error: %r" % (exc,))
                    fresh = None
                if self._fresh_target_sample(
                        fresh, prior, float(confirm_after or now)) \
                        and self._target_measured(fresh):
                    last = fresh
                delay = min(CLASSIC_LOCK_POLL_SECONDS,
                            max(0.0, deadline - time.perf_counter()))
                if delay > 0.0:
                    time.sleep(delay)
                continue

            # Mode 1 centres the detector's target itself. Motion lead and
            # configurable aim offsets belong to tracking/ranking paths; using
            # either here would deliberately steer away from exact centre.
            if self._target_center_confirmed(last) \
                    and self._target_observed_after(last, confirm_after):
                centered = True
                reason = "centered"
                break
            if corrections >= CLASSIC_LOCK_MAX_CORRECTIONS:
                reason = "correction limit"
                break

            if not self._target_measured(last):
                # Predicted/color bridge points may keep identity alive but
                # must neither steer nor release a Mode-1 trigger. Wait for a
                # fresh fast-model/pose measurement.
                prior = last
                try:
                    fresh = self.scan_near(mx, my, state, False)
                except Exception as exc:
                    log("Classic lock scan error: %r" % (exc,))
                    fresh = None
                if self._fresh_target_sample(fresh, prior, now) \
                        and self._target_measured(fresh):
                    last = fresh
                delay = min(CLASSIC_LOCK_POLL_SECONDS,
                            max(0.0, deadline - time.perf_counter()))
                if delay > 0.0:
                    time.sleep(delay)
                continue

            step_cap = self._max_step_cfg()
            area_cap = 2.0 * max(1, self.radius_px())
            if step_cap <= 0 or step_cap > area_cap:
                step_cap = area_cap
            delta = self._target_center_delta(last)
            if delta is None:
                reason = "target lost"
                break
            raw_dx, raw_dy = delta
            ndx, ndy = self._cap_step(raw_dx, raw_dy, step_cap)
            if ndx == 0 and ndy == 0:
                # A predicted/sub-pixel-zero result cannot confirm a shot.
                # Wait for another measured frame without injecting motion.
                prior = last
                try:
                    fresh = self.scan_near(mx, my, state, False)
                except Exception as exc:
                    log("Classic lock scan error: %r" % (exc,))
                    fresh = None
                if self._fresh_target_sample(fresh, prior, now):
                    last = fresh
                delay = min(CLASSIC_LOCK_POLL_SECONDS,
                            max(0.0, deadline - time.perf_counter()))
                if delay > 0.0:
                    time.sleep(delay)
                continue

            command_offset = (float(last.dx), float(last.dy))
            command_target = last
            self._steer_target_h = float(getattr(last, "bh", 0.0) or 0.0)
            if not self._steer_view(ndx, ndy):
                reason = "steering refused"
                centered = False
                break
            corrections += 1
            command_at = time.perf_counter()

            # Camera invalidation deliberately gives the post-turn detector a
            # new track identity.  Retaining the pre-turn ID here would reject
            # the very frame needed to verify the correction.
            state.pop("track_id", None)
            state.pop("track_mismatch", None)

            newest = None
            while self._classic_lock_alive(state):
                now = time.perf_counter()
                if now >= deadline:
                    break
                try:
                    fresh = self.scan_near(mx, my, state, False)
                except Exception as exc:
                    log("Classic lock scan error: %r" % (exc,))
                    fresh = None
                if self._fresh_target_sample(
                        fresh, command_target, command_at):
                    if not self._target_measured(fresh):
                        delay = min(
                            CLASSIC_LOCK_POLL_SECONDS,
                            max(0.0, deadline - time.perf_counter()))
                        if delay > 0.0:
                            time.sleep(delay)
                        continue
                    newest = fresh
                    # A confirmed centre can fire immediately.  Otherwise do
                    # not stack another turn onto a frame that still depicts
                    # the pre-input view (common with remote/streamed games).
                    if self._target_center_confirmed(fresh):
                        last = fresh
                        break
                    elapsed = now - command_at
                    response = math.hypot(
                        float(fresh.dx) - command_offset[0],
                        float(fresh.dy) - command_offset[1])
                    response_gate = max(
                        2.0, 0.08 * math.hypot(*command_offset))
                    if elapsed >= CLASSIC_LOCK_RESPONSE_MIN_SECONDS \
                            and (response >= response_gate
                                 or elapsed >=
                                 CLASSIC_LOCK_RESPONSE_RETRY_SECONDS):
                        last = fresh
                        break

                delay = min(CLASSIC_LOCK_POLL_SECONDS,
                            max(0.0, deadline - time.perf_counter()))
                if delay > 0.0:
                    time.sleep(delay)
            else:
                reason = "cancelled"
                centered = False
                break

            if not self._classic_lock_alive(state):
                reason = "cancelled"
                centered = False
                break
            if newest is None and time.perf_counter() >= deadline:
                reason = "target lost"
                centered = False
                break
            if newest is not None:
                last = newest

        else:
            centered = False
            reason = "cancelled"

        return {
            "target": last,
            "ax": last_ax,
            "ay": last_ay,
            "nx": last_nx,
            "ny": last_ny,
            "centered": bool(centered),
            "reason": reason,
            "corrections": corrections,
        }

    def _mode1_fresh_target(self, target, state, x, y):
        """Briefly acquire a usable target for an instant Mode-1 shot.

        The press itself does not move the view, so a target captured a few
        milliseconds before the press is still valid if the detector has not
        produced a newer frame yet.  Retry for a short, bounded window (not
        the old one-second lock), but never discard the last good target and
        leave the trigger with nothing to steer.
        """
        try:
            pressed_at = float(state.get("pressed_at", 0.0) or 0.0)
        except (TypeError, ValueError, AttributeError):
            pressed_at = 0.0
        if not self._classic_mode1_job(state):
            return target
        if pressed_at <= 0.0:
            return target
        best_measured = target if (target is not None
                                   and self._target_measured(target)) else None
        deadline = time.perf_counter() + 0.080
        while time.perf_counter() < deadline:
            if target is not None:
                measured = self._target_measured(target)
                fresh = self._target_observed_after(target, pressed_at)
                if measured and fresh:
                    return target
                if measured and best_measured is None:
                    best_measured = target
            time.sleep(0.002)
            try:
                target = self.scan_near(x, y, state, False)
            except Exception:
                break
        if target is not None and self._target_measured(target) \
                and self._target_observed_after(target, pressed_at):
            return target
        if best_measured is not None:
            return best_measured
        # If no measured frame arrived, do not steer from a colour/predicted
        # bridge; the caller keeps the trigger blocked.
        return None

    def do_classic(self, x, y, out, state):
        """
        Mode 1:
        The physical down was suppressed.
        Find target, move, click. Desktop failures return the click; an
        in-game fire transaction stays blocked unless exact centre is freshly
        confirmed.
        """
        try:
            # Every suppressed click is a new acquisition transaction.  It
            # must never inherit the previous shot's colour identity.
            self._reset_target_lock()
            if not self._epoch_alive(state):

                if self._claim_delivery(state):
                    if self._mode1_trigger_must_not_fire(state):
                        log("Classic assist: Mode-1 job cancelled; trigger "
                            "stayed blocked.")
                    else:
                        self.reinject_click(x, y)
                out["handled"] = True
                return
            if state.get("abandoned"):
                out["handled"] = True
                return

            if "block_unconfirmed" in state:
                game_transaction = bool(state.get("block_unconfirmed"))
            else:
                game_transaction = self._center_box() is not None
            mode1_game = (self._classic_mode1_job(state)
                          and game_transaction)
            target = self.scan_near(
                x, y, state, False if mode1_game else self._track)

            if not self._epoch_alive(state):

                if self._claim_delivery(state):
                    if self._mode1_trigger_must_not_fire(state):
                        log("Classic assist: Mode-1 job cancelled; trigger "
                            "stayed blocked.")
                    else:
                        self.reinject_click(x, y)
                out["handled"] = True
                return
            if state.get("abandoned"):
                out["handled"] = True
                return

            target = self._mode1_fresh_target(target, state, x, y)
            if target is None:
                self.pass_count += 1
                self._pass_note("no measured post-press target")
                if self._claim_delivery(state):
                    if self._mode1_trigger_must_not_fire(state):
                        log("Classic assist: no measured post-press target; "
                            "Mode-1 trigger stayed blocked.")
                    else:
                        self.reinject_click(x, y)
                out["handled"] = True
                return

            cbox = self._center_box()
            if cbox is not None:
                ax, ay = (cbox[0] + target.dx, cbox[1] + target.dy)
            else:
                ax, ay = (x + target.dx, y + target.dy)
            on_target_cfg = 0.0
            hit = self._hit_test(ax, ay, x, y, target.bw, target.bh,
                                 on_target_cfg)

            flight = (cbox is not None
                      and (self._reticle_active()
                           or bool(state.get("block_unconfirmed"))))
            if self._mode1_trigger_must_not_fire(state) and not flight:
                # The press began as an in-game fire transaction, but the
                # game/reticle context disappeared while it was queued (for
                # example a menu opened). Never reinterpret it as a desktop
                # click and fire without exact-centre confirmation.
                if self._claim_delivery(state):
                    log("Classic assist: reticle context changed; Mode-1 "
                        "trigger stayed blocked.")
                out["handled"] = True
                return

            if flight:

                if not self._classic_mode1_job(state) \
                        and not self._target_measured(target):
                    # Mode 2 still owes the user the click whose DOWN was
                    # suppressed, but bridge/predicted geometry is not allowed
                    # to rotate the camera. Deliver once at the current
                    # reticle and wait for a measured frame next time.
                    if self._claim_delivery(state):
                        self._click_at_reticle()
                        self.pass_count += 1
                    out["handled"] = True
                    return

                if self._classic_mode1_job(state):
                    # Mode 1 is a snap-and-fire transaction.  Do not wait for
                    # a second exact-centre frame: scan once, nudge to the
                    # measured target centre, then click immediately.
                    if not self._target_measured(target):
                        self.pass_count += 1
                        log("Classic assist: Mode-1 trigger stayed blocked "
                            "(no measured target).")
                        out["handled"] = True
                        return

                    log("Classic assist: instant target dx=%+d, dy=%+d%s."
                        % (int(round(float(target.dx))),
                           int(round(float(target.dy))),
                           " (ADS)" if bool(
                               getattr(self, "_rbtn_down", False)) else ""))
                    ax = cbox[0] + target.dx
                    ay = cbox[1] + target.dy
                    nx, ny = ax, ay
                    if bool(self.cfg.get("motion_predict", False)) \
                            or bool(self.cfg.get("motion_3d", False)):
                        nx, ny = self._apply_motion_lead(nx, ny, target)

                    step_cap = self._max_step_cfg()
                    area_cap = 2.0 * max(1, self.radius_px())
                    if step_cap <= 0 or step_cap > area_cap:
                        step_cap = area_cap
                    ndx, ndy = self._cap_step(nx - cbox[0], ny - cbox[1],
                                              step_cap)
                    self._steer_target_h = float(
                        getattr(target, "bh", 0.0) or 0.0)
                    sent = True
                    if ndx or ndy:
                        sent = self._steer_view(ndx, ndy)
                    if not sent:
                        self.pass_count += 1
                        log("Classic assist: Mode-1 trigger stayed blocked "
                            "(steering refused).")
                        out["handled"] = True
                        return

                    if not self._claim_delivery(state):
                        out["handled"] = True
                        return
                    if not self._epoch_alive(state):
                        if self._mode1_trigger_must_not_fire(state):
                            log("Classic assist: Mode-1 job cancelled; "
                                "trigger stayed blocked.")
                        else:
                            self.reinject_click(x, y)
                        out["handled"] = True
                        return

                    if MODE1_FIRE_SETTLE_S > 0.0:
                        time.sleep(MODE1_FIRE_SETTLE_S)
                    self._click_at_reticle()
                    self.snap_count += 1
                    self.last_snap_time = time.perf_counter()
                    self.last_snap_pos = (int(cbox[0]), int(cbox[1]))
                    self._clear_mode1_follow_state()
                    log("Classic assist: instant snap (%+d,%+d) and click "
                        "after %d correction(s)."
                        % (ndx, ndy, 1 if (ndx or ndy) else 0))
                    out["handled"] = True
                    return

                if not self._claim_delivery(state):
                    out["handled"] = True
                    return
                if not self._epoch_alive(state):

                    self.reinject_click(x, y)
                    out["handled"] = True
                    return

                _ot = 0.0
                if self._hit_test(cbox[0] + target.dx, cbox[1] + target.dy,
                                  cbox[0], cbox[1], target.bw, target.bh,
                                  _ot):
                    # Already on target: click at reticle position
                    # (no warp - cursor is hidden in flight mode).
                    self._click_at_reticle()
                    self.snap_count += 1
                    if self._lbtn_down and self._mode_allows_mouse2_follow():
                        with self._state_lock:
                            self._center_hold = self._new_hold_session()
                    if self._follow_may_learn(target):
                        self.follow.store(ax, ay, target)
                    self.last_snap_time = time.perf_counter()
                    self.last_snap_pos = (int(ax), int(ay))
                    log("Classic assist: already on it - clicked, "
                        "view untouched.")
                    out["handled"] = True
                    return
                nx, ny = ax, ay
                # Mode 1's instant snap above handles its own forced lead.
                # Mode 2 short clicks use the configured motion lead only.
                nx, ny = self._apply_motion_lead(
                    nx, ny, target, force=False)
                nx, ny = self._apply_snap_offset(nx, ny)

                step_cap = self._max_step_cfg()
                area_cap = 2.0 * max(1, self.radius_px())
                if step_cap <= 0 or step_cap > area_cap:
                    step_cap = area_cap
                ndx, ndy = self._cap_step(nx - cbox[0], ny - cbox[1],
                                          step_cap)
                if abs(target.dx) <= 2 and abs(target.dy) <= 2:
                    ndx = ndy = 0
                self._steer_target_h = float(getattr(target, "bh", 0.0) or 0.0)
                sent = True if (ndx == 0 and ndy == 0) \
                    else self._steer_view(ndx, ndy)
                if not sent:
                    # Nudge refused: the reticle did not move, so click
                    # where it still points.  Never warp in flight.
                    log("Classic assist: nudge refused (%+d, %+d); "
                        "clicking at the reticle." % (int(ndx), int(ndy)))
                    self._click_at_reticle()
                    self.pass_count += 1
                    out["handled"] = True
                    return
                # The game needs ONE frame to consume the move before the
                # click traces from the new reticle. 20 ms ~= one frame at 50 Hz.
                time.sleep(0.020)
                self._click_at_reticle()
                self.snap_count += 1
                if self._lbtn_down and self._mode_allows_mouse2_follow():
                    with self._state_lock:
                        self._center_hold = self._new_hold_session()
                if self._follow_may_learn(target):

                    self.follow.store(ax, ay, target)
                self.last_snap_time = time.perf_counter()
                self.last_snap_pos = (int(nx), int(ny))
                log("Classic assist: view rotated (%+d, %+d) by nudge, "
                    "clicked at pointer."
                    % (int(ndx), int(ndy)))
                out["handled"] = True
                return

            if hit:

                nx, ny = x, y
            else:
                nx, ny = ax, ay

                if bool(self.cfg.get("motion_predict", False)) \
                        or bool(self.cfg.get("motion_3d", False)):
                    nx, ny = self._apply_motion_lead(nx, ny, target)

                nx, ny = self._apply_snap_offset(nx, ny)

            now = time.perf_counter()
            watcher = float(self.cfg.get("watcher_seconds", 0.45))

            double = False
            if (
                self.last_snap_pos is not None
                and now - self.last_snap_time < watcher
            ):
                lx, ly = self.last_snap_pos
                h = max(1.0, target.bh)
                double_radius = max(6.0, min(35.0, 1.5 * h))
                if math.hypot(nx - lx, ny - ly) <= double_radius:
                    double = True

            if not self._claim_delivery(state):
                out["handled"] = True
                return

            if not self._epoch_alive(state):

                self.reinject_click(x, y)
                out["handled"] = True
                return

            self.snap_count += 1
            if cbox is not None and self._lbtn_down \
                    and self._mode_allows_mouse2_follow():
                with self._state_lock:
                    self._center_hold = self._new_hold_session()
            if not self._classic_mode1_job(state) \
                    and self._follow_may_learn(target):

                self.follow.store(ax, ay, target)

            with self._state_lock:
                hold_now = self._lbtn_down
                locked = bool(self.follow_lock
                              and self.follow_lock.get("active"))
            if (bool(self.cfg.get("hold_follow", True))
                    and not self._classic_mode1_job(state)
                    and hold_now
                    and target.sig is not None
                    and not locked):
                rgb = detection.hsv_to_rgb(*target.sig)
                with self._state_lock:
                    self.follow_lock = {
                        "active": True,
                        "accent": "%d,%d,%d" % rgb,
                        "source": "hold",
                        "misses": 0,
                    }

            if hit or self.glide_cursor(nx, ny):
                self.send_click(nx, ny, double=double)
            else:

                log("Glide aborted by user movement; clicking at current position.")
                self._click_here()

            self.last_snap_time = time.perf_counter()
            self.last_snap_pos = (int(nx), int(ny))
            if self._classic_mode1_job(state):
                self._clear_mode1_follow_state()

            log(
                "Classic assist: moved %.0f px to (%d, %d)%s%s"
                % (
                    math.hypot(nx - x, ny - y),
                    int(nx),
                    int(ny),
                    " (already on it - nothing moved)" if hit else "",
                    " and double-clicked" if double else "",
                )
            )

            out["handled"] = True

        except Exception as exc:
            log("classic worker error: %r" % (exc,))
            if self._claim_delivery(state):
                if self._mode1_trigger_must_not_fire(state):
                    log("Classic assist: Mode-1 worker failed; trigger "
                        "stayed blocked.")
                else:
                    try:
                        self.reinject_click(x, y)
                    except Exception:
                        pass
            out["handled"] = True

    def do_guide(self, x, y, out, state):
        """
        Mode 2:
        The user's click already passed through.
        After release, move cursor onto the nearest clear item.
        """
        try:
            if not self._epoch_alive(state):

                out["handled"] = True
                return
            if state.get("abandoned"):
                out["handled"] = True
                return

            target = self.scan_near(x, y, state, self._track)

            if not self._epoch_alive(state):
                out["handled"] = True
                return
            if state.get("abandoned"):
                out["handled"] = True
                return

            if target is None:
                if not state.get("continuous"):
                    self.pass_count += 1

                out["handled"] = True
                return

            hold = bool(state.get("continuous"))
            if hold and not self._human_target_ok(target):
                # During a held assist only a confirmed human (YOLO detect,
                # pose, or the seeded colour lock) may steer.  Classical /
                # high-contrast blobs (crosshair, bullet, muzzle flash, HUD
                # dot) are rejected so the view never rotates away from the
                # acquired human.
                target = None
            ledger = state.get("guidance") if hold else None
            cbox = self._center_box()
            flight = self._reticle_active()
            if cbox is not None and flight \
                    and not self._target_measured(target):
                if ledger is not None:
                    ledger["cmd_pending"] = False
                out["handled"] = True
                return
            if cbox is not None:
                ax, ay = (cbox[0] + target.dx, cbox[1] + target.dy)
            else:
                ax, ay = (x + target.dx, y + target.dy)
            on_target = 0.0

            # In reticle flight the aim point is the screen centre, not
            # the hidden OS cursor: hit-testing against the cursor would
            # either skip a needed correction or repeat one forever.
            if cbox is not None and flight:
                aim_x, aim_y = cbox[0], cbox[1]
            else:
                aim_x, aim_y = x, y

            already_on_target = self._hit_test(
                ax, ay, aim_x, aim_y, target.bw, target.bh, on_target)
            may_lead_hold = bool(hold and cbox is not None and flight)
            if already_on_target and not may_lead_hold:
                if ledger is not None:
                    ledger["cmd_pending"] = False
                    ledger["center_latched"] = True
                if not hold:
                    self.pass_count += 1
                out["handled"] = True
                return

            nx, ny = ax, ay

            if cbox is not None and flight:
                nx, ny = self._apply_motion_lead(
                    nx, ny, target, force=bool(hold))
                nx, ny = self._apply_snap_offset(nx, ny)
                # A correction can never be larger than the watched area:
                # the sighting is inside it by definition, and the old
                # 500 px cap let one bad reading spin the whole view.
                step_cap = self._max_step_cfg()
                area_cap = 2.0 * max(1, self.radius_px())
                if step_cap <= 0 or step_cap > area_cap:
                    step_cap = area_cap
                ndx, ndy = self._cap_step(nx - cbox[0], ny - cbox[1],
                                          step_cap)

                try:
                    centre_x = int(round(float(target.dx)))
                    centre_y = int(round(float(target.dy)))
                except (TypeError, ValueError, OverflowError):
                    centre_x = centre_y = 0
                has_lead = (int(round(nx - ax)) != 0
                            or int(round(ny - ay)) != 0)
                center_distance = math.hypot(centre_x, centre_y)
                if ledger is not None:
                    if center_distance <= HOLD_CENTER_DEADBAND_PX \
                            and not has_lead:
                        ledger["center_latched"] = True
                    elif center_distance > HOLD_CENTER_RELEASE_PX or has_lead:
                        ledger["center_latched"] = False
                    center_quiet = bool(ledger.get("center_latched")) \
                        and center_distance <= HOLD_CENTER_RELEASE_PX \
                        and not has_lead
                else:
                    center_quiet = (center_distance
                                    <= HOLD_CENTER_DEADBAND_PX
                                    and not has_lead)
                if center_quiet:
                    ndx = ndy = 0
                elif not has_lead and self._hit_test(
                        cbox[0] + target.dx,
                        cbox[1] + target.dy,
                        cbox[0], cbox[1], target.bw, target.bh,
                        on_target):
                    ndx = ndy = 0
                if not (ndx or ndy) and ledger is not None:
                    ledger["cmd_pending"] = False
                if ndx or ndy:
                    # Send one correction per fresh measurement.  The
                    # speed-limited servo already prevents overshoot;
                    # waiting for a camera-response confirmation just
                    # makes fast targets stutter and get lost.
                    if ledger is not None:
                        new_seq = int(getattr(target, "seq", 0) or 0)
                        old_seq = int(ledger.get("last_seq", 0) or 0)
                        if new_seq > 0 and new_seq <= old_seq:
                            out["handled"] = True
                            return
                    self._steer_target_h = float(
                        getattr(target, "bh", 0.0) or 0.0)
                    completed = self._steer_view(ndx, ndy, hold=hold)
                    if completed and ledger is not None:
                        ledger["last_seq"] = int(
                            getattr(target, "seq", 0) or 0)
                        ledger["last_cmd_at"] = time.perf_counter()
                        ledger["last_cmd_offset"] = (float(target.dx),
                                                     float(target.dy))
                else:
                    # One-shot guide still reports "already on it"; the
                    # continuous hold loop must not count a no-op every tick.
                    completed = not hold
                if completed:
                    # The continuous loop feeds last_snap_pos back as its
                    # next click point; in flight that point is the reticle.
                    self.last_snap_pos = (int(cbox[0]), int(cbox[1]))
            else:

                if bool(self.cfg.get("motion_predict", False)) \
                        or bool(self.cfg.get("motion_3d", False)):
                    nx, ny = self._apply_motion_lead(nx, ny, target)

                nx, ny = self._apply_snap_offset(nx, ny)

                completed = self.glide_cursor(nx, ny)
                ndx = ndy = None

            if completed:
                self.snap_count += 1
            if completed and self._follow_may_learn(target):

                self.follow.store(ax, ay, target)
            if completed:
                self.last_snap_time = time.perf_counter()
            if completed and not (cbox is not None and flight):
                self.last_snap_pos = (int(nx), int(ny))

            if ndx is None:
                log(
                    "Guide assist: cursor moved %.0f px to (%d, %d)%s"
                    % (math.hypot(nx - x, ny - y), int(nx), int(ny),
                       "" if completed else " (glide aborted: you took over)")
                )
            elif not (hold and not completed and not (ndx or ndy)):
                if not completed:
                    log("Guide assist: steering backend refused the nudge; "
                        "view untouched.")
                elif ndx == 0 and ndy == 0:
                    log("Guide assist: already on it - view untouched.")
                else:
                    tag = (" (ADS response %.2f)"
                           % self._steer_response_now()
                           if bool(getattr(self, "_rbtn_down", False)) else "")
                    log("Guide assist: view rotated (%+d, %+d) by nudge%s."
                        % (int(ndx), int(ndy), tag))

            out["handled"] = True

        except Exception as exc:
            log("guide worker error: %r" % (exc,))
            out["handled"] = True

    def run_scanner(self):
        """
        Process queued click assistance jobs.
        Long operations run in disposable threads so the hook never freezes.
        Also expires deferred jobs from the double-click guard.
        """
        while not self.stop.is_set():
            try:
                item = self.click_q.get(timeout=0.1)
            except queue.Empty:
                continue

            kind, x, y = item[0], item[1], item[2]
            entry = item[4] if len(item) > 4 else None

            out = {}
            state = {"abandoned": False, "delivered": False,
                     "lock": threading.Lock(),
                     "epoch": self._mode_epoch,
                     "pressed_at": item[3]}

            if entry is not None and entry.get("mode") in (1, 2):
                state["mode"] = entry["mode"]
            else:
                state["mode"] = self.current_mode()
            if entry is not None:
                state["block_unconfirmed"] = bool(
                    entry.get("block_unconfirmed", False))

            if entry is not None:
                with state["lock"]:
                    entry["state"] = state

            if kind == "classic":
                func = self.do_classic
            elif kind == "guide":
                func = self.do_guide
            else:
                continue

            self.worker_active = True

            th = threading.Thread(
                target=func,
                args=(x, y, out, state),
                name="assist-worker",
                daemon=True,
            )
            th.start()

            _glide_s = 0.0
            th.join(timeout=1.0 + max(0.0, _glide_s) + 0.5)

            if not th.is_alive():
                self.worker_active = False

            if th.is_alive():
                if kind == "classic":
                    # Claim FIRST, then mark abandoned. _claim_delivery refuses
                    # an already-abandoned state, so reversing this order would
                    # lose the suppressed physical click.
                    if self._claim_delivery(state):
                        with state["lock"]:
                            state["abandoned"] = True
                        if self._mode1_trigger_must_not_fire(state):
                            log("Mode-1 assist stalled; trigger stayed "
                                "blocked without a confirmed centre.")
                        else:
                            self.reinject_click(x, y)
                            if entry and entry.get("pad"):
                                log("Pad assist stalled. Click delivered raw.")
                            else:
                                log("Assist worker stalled. Suppressed click "
                                    "returned.")
                    else:
                        with state["lock"]:
                            state["abandoned"] = True
                else:
                    with state["lock"]:
                        state["abandoned"] = True
                # The job is abandoned; do not keep the queue blocked forever.
                self.worker_active = False

                _stall = 0.0
                self.pause_until = time.perf_counter() + _stall

            elif kind == "classic" and not out.get("handled"):

                if self._claim_delivery(state):
                    if self._mode1_trigger_must_not_fire(state):
                        log("Mode-1 worker ended without confirmation; "
                            "trigger stayed blocked.")
                    else:
                        self.reinject_click(x, y)

            if kind == "classic" and entry is not None:

                with self._pending_lock:
                    entry["worker_done"] = True
                    if entry.get("up"):
                        try:
                            self.pending_classics.remove(entry)
                        except ValueError:
                            pass

    def _arm_hold_follow(self, x, y, now, armed):
        """Lock the follow memory for the duration of a held press.

        One implementation for both input devices: the mouse hook calls it on
        WM_LBUTTONDOWN and the controller drive calls it when the trigger goes
        down, so "keep following while the button is held" behaves identically
        whether the user holds a mouse button or a pad trigger.
        """
        pad = detection.FOLLOW_POS_PAD

        _mem_x, _mem_y, _mem_sig, _mem_when = self.follow.snapshot()
        with self._state_lock:
            _locked = bool(self.follow_lock
                           and self.follow_lock.get("active"))
            _mouse2 = bool(self.follow_lock
                           and self.follow_lock.get("active")
                           and self.follow_lock.get("source") == "mouse2")
        near_memory = (abs(x - _mem_x) <= pad and abs(y - _mem_y) <= pad)
        if (bool(self.cfg.get("hold_follow", True))
                and armed
                and _mem_sig is not None
                and _mem_when > 0
                and (now - _mem_when)
                <= float(self.cfg.get("follow_seconds", 20) or 20)
                and near_memory
                and not _locked):
            rgb = detection.hsv_to_rgb(*_mem_sig)
            with self._state_lock:
                self.follow_lock = {
                    "active": True,
                    "accent": "%d,%d,%d" % rgb,
                    "source": "hold",
                    "misses": 0,
                }
        elif _mouse2:
            with self._state_lock:
                self.follow_lock = None

    def _clear_mode1_follow_state(self):
        """Mode 1 is one block/snap/click transaction, never a follow mode."""
        with self._state_lock:
            self.follow_lock = None
            self._center_hold = None
            self._center_hold_tomb = 0.0
            self._center_follow = None
            self._pick_at = None
            self._chase_ref = None

    def _arm_center_hold(self, x, y, mode, armed):
        """Start a centre-hold steering session for a held press in mode 2.

        Shared by the mouse hook and the controller drive, for the same reason
        as _arm_hold_follow: one definition of "the press is inside the
        watched centre square".
        """
        if mode != 2 or not armed:
            return
        # In reticle flight the held-trigger continuous loop owns steering.
        # Arming here as well would steer during the 200 ms short-press
        # window and then let the snap steer again: two corrections for
        # one sighting.  A snap that lands while the button is still down
        # arms its own hold session directly in do_classic.
        if self._reticle_active():
            return
        cbox = self._center_box()
        if cbox is not None:
            if not (self._reticle_active()
                    or (abs(x - cbox[0]) <= cbox[2]
                        and abs(y - cbox[1]) <= cbox[2])):
                return
        with self._state_lock:
            self._center_hold = self._new_hold_session()

    def _release_holds(self, now):
        """Tear down everything a held press armed. Returns was_center.

        One implementation for both input devices, and deliberately WITHOUT
        the pending-classics bookkeeping: that ledger belongs to the mouse
        hook, whose physical UP has to be swallowed exactly once. A controller
        release has no physical event to swallow, and marking a mouse entry
        from the pad path would let that mouse click through un-swallowed.
        """
        with self._state_lock:
            was_center = self._center_hold is not None
            if not was_center and self._center_hold_tomb \
                    and now - self._center_hold_tomb < 2.0:
                was_center = True
            self._center_hold = None
            self._center_hold_tomb = 0.0
            hold_lock = bool(self.follow_lock
                             and self.follow_lock.get("source") == "hold")
            if hold_lock:
                self.follow_lock = None
            if was_center:
                self._chase_ref = None
                self._center_prev = None
        if was_center:
            self._reset_motion_history()
        return was_center

    def _release_bookkeeping(self, role, armed, mode, now, pt):
        """Shared release-side bookkeeping for button-UP events.

        Runs for releases ANYWHERE - including over our own windows -
        so a drag ending over the GUI can never strand a session, a
        synthetic DOWN, a hold lock or a swallowed UP. Returns 1 when
        the caller must swallow the event (the UP of a suppressed
        classic DOWN), else None.

        armed/mode gate only NEW side effects (extra-options actions, guide
        jobs): teardown always runs, armed or not.

        `role` is ROLE_TRIGGER or ROLE_OPTIONS: which physical button plays
        which part is the user's choice (trigger_button / options_button), so
        nothing below may assume left or right. The _rbtn_*/mouse2 names in
        this class are historical and mean "the options role".
        """
        if role == ROLE_OPTIONS:
            self._finish_mouse2(now)
            return None

        was_center = self._release_holds(now)

        with self._pending_lock:
            for entry in self.pending_classics:
                if entry.get("pad"):
                    continue
                if not entry.get("up"):
                    entry["up"] = True
                    if entry.get("worker_done"):
                        try:
                            self.pending_classics.remove(entry)
                        except ValueError:
                            pass
                    return 1

        if mode == 2 and armed and not was_center:
            self.click_q.put(("guide", pt.x, pt.y, now, None))
        return None

    def make_hook_proc(self):

        def proc(nCode, wParam, lParam):
            swallow = False
            try:
                if nCode == HC_ACTION and not self.stop.is_set():
                    info = ctypes.cast(
                        lParam, ctypes.POINTER(MSLLHOOKSTRUCT)
                    ).contents

                    # Our own injected events must always pass.  Win32
                    # SendInput sets LLMHF_INJECTED; Interception strokes are
                    # driver-level and arrive unflagged, but we tag their
                    # extra-info word.  Never identify an injected event by
                    # position/time: a fast physical click at the same pixel
                    # would otherwise be mistaken for our synthetic click and
                    # leak through the Mode-1 block.
                    try:
                        extra = (int(info.dwExtraInfo)
                                 if info.dwExtraInfo else 0)
                    except (TypeError, ValueError):
                        extra = 0
                    if (info.flags & (LLMHF_INJECTED
                                      | LLMHF_LOWER_IL_INJECTED)) \
                            or extra == INTERCEPTION_EXTRA_INFO:
                        return user32.CallNextHookEx(
                            self.hook, nCode, wParam, lParam)

                    own = None
                    if self._rec_buffer is not None and self.running \
                            and not (info.flags
                                     & (LLMHF_INJECTED
                                        | LLMHF_LOWER_IL_INJECTED)):
                        rmsg = int(wParam)
                        if rmsg == WM_MOUSEMOVE:
                            self._record_move(info.pt.x, info.pt.y)
                            return user32.CallNextHookEx(
                                self.hook, nCode, wParam, lParam)
                        if rmsg in _MOUSE_REC_MAP:
                            own = self._fg_is_own()
                            if not own:
                                self._record_button(rmsg, info.pt.x,
                                                    info.pt.y,
                                                    info.mouseData)

                    pair = mouse_message_button(wParam, info.mouseData)
                    if pair is None:
                        return user32.CallNextHookEx(
                            self.hook, nCode, wParam, lParam)
                    btn, btn_down = pair

                    if not (info.flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED)):
                        now = time.perf_counter()

                        if btn == self._trigger_button():
                            role = ROLE_TRIGGER
                        elif btn == self._options_button():
                            role = ROLE_OPTIONS
                        else:
                            return user32.CallNextHookEx(
                                self.hook, nCode, wParam, lParam)

                        if role == ROLE_TRIGGER:
                            self._lbtn_down = btn_down
                        else:
                            self._rbtn_down = btn_down
                            if btn_down:
                                self.rbutton_down_time = now

                        if own is None:
                            own = self._fg_is_own()
                        if own:
                            if not btn_down:
                                if role == ROLE_TRIGGER:
                                    # A press that started outside our window
                                    # can end over it. Consume its ledger slot
                                    # so a later physical UP is not mistaken
                                    # for this one.
                                    with self._trigger_lock:
                                        if self._blocked_trigger_ups:
                                            self._blocked_trigger_ups.pop(0)
                                self._release_bookkeeping(role, armed=False,
                                                          mode=2, now=now,
                                                          pt=info.pt)
                            return user32.CallNextHookEx(
                                self.hook, nCode, wParam, lParam)

                        bypass = self._bypass_held()

                        armed = (
                            self.running
                            and not self.scanner_dead
                            and now >= self.pause_until
                            and (not bypass or not self._bypass_enabled())
                        )

                        mode = self.current_mode()

                        if role == ROLE_OPTIONS and btn_down:

                            if armed:
                                if self._mouse2_hold_action() == 4:

                                    self._handle_mouse2()
                                else:

                                    self._rbtn_armed = True
                                    self._start_mouse2_hold_timer()

                        elif role == ROLE_OPTIONS:
                            self._finish_mouse2(now)

                        if role == ROLE_TRIGGER and btn_down:

                            # Once the trigger is armed for Mode 1/2 the
                            # physical press is claimed.  Set this before the
                            # follow/centre bookkeeping so an unexpected error
                            # cannot fall through to CallNextHookEx and fire.
                            if armed and mode in (1, 2):
                                swallow = True
                            if mode == 1:
                                self._clear_mode1_follow_state()
                            else:
                                self._arm_hold_follow(info.pt.x, info.pt.y,
                                                      now, armed)
                                self._arm_center_hold(info.pt.x, info.pt.y,
                                                      mode, armed)

                            rapid = bool(self.cfg.get("rapid_click_snap",
                                                      False))
                            if mode == 1 and armed:
                                block_unconfirmed = bool(
                                    self._center_box() is not None)
                                pass_through = False
                                with self._pending_lock:
                                    un_upped = any(
                                        (not e.get("up"))
                                        and (not e.get("worker_done"))
                                        for e in self.pending_classics
                                    )
                                    if rapid or (
                                            not un_upped
                                            and self.click_q.qsize() == 0
                                            and not self.worker_active):
                                        entry = {"x": info.pt.x,
                                                 "y": info.pt.y,
                                                 "up": False, "state": None,
                                                 "mode": 1,
                                                 "block_unconfirmed":
                                                     block_unconfirmed}
                                        self.pending_classics.append(entry)
                                    else:
                                        entry = None
                                        # In-game Mode 1 never releases an
                                        # unassisted shot. On the desktop the
                                        # visible rapid-click off behaviour
                                        # still passes the extra click
                                        # through to Windows.
                                        pass_through = not block_unconfirmed
                                with self._trigger_lock:
                                    self._blocked_trigger_ups.append(
                                        _MODE1_PASS_THROUGH
                                        if pass_through else entry)
                                if entry is not None:
                                    self.click_q.put(
                                        ("classic", info.pt.x, info.pt.y,
                                         now, entry)
                                    )
                                    return 1
                                if pass_through:
                                    self._pass_note(
                                        "previous click still assisted "
                                        "(rapid off); this click passed "
                                        "through")
                                else:
                                    self._pass_note(
                                        "previous click still assisted "
                                        "(rapid off); this trigger stayed "
                                        "blocked")
                                    return 1

                            # Mode 2: single-click = classic behavior (suppress, detect, snap, click)
                            #         hold       = pass clicks through, continuous assist while held
                            elif mode == 2 and armed:
                                # Tentatively suppress the click; we'll decide on UP or hold timer
                                with self._trigger_lock:
                                    self._trigger_press_time = now
                                    self._trigger_press_id += 1
                                    pid = self._trigger_press_id
                                    self._trigger_hold_done = False
                                    self._trigger_suppressed_down = {
                                        "x": info.pt.x, "y": info.pt.y, "now": now,
                                         "entry": {"x": info.pt.x, "y": info.pt.y,
                                                  "up": False, "state": None,
                                                  "mode": 2}
                                    }
                                # Start hold timer - if it fires, this was a hold
                                t = threading.Timer(TRIGGER_HOLD_SECONDS,
                                                    self._trigger_hold_check, args=(pid,))
                                t.daemon = True
                                t.start()
                                with self._trigger_lock:
                                    self._trigger_hold_timer = t
                                # Queue the classic-style entry but DON'T put in click_q yet
                                with self._pending_lock:
                                    self.pending_classics.append(
                                        self._trigger_suppressed_down["entry"])
                                # Suppress the DOWN (will reinject if hold, or worker will click if single)
                                return 1

                        elif role == ROLE_TRIGGER:
                            # Button UP - check if it was a single click or hold.
                            # Always tear down anything the press armed (hold
                            # follow, centre-hold session) so assistance never
                            # outlives the physical button.
                            self._release_holds(now)
                            with self._trigger_lock:
                                pid = self._trigger_press_id
                                was_hold = self._trigger_hold_done
                                suppressed = self._trigger_suppressed_down
                                self._trigger_suppressed_down = None
                                self._trigger_hold_done = False
                                if self._trigger_hold_timer is not None:
                                    self._trigger_hold_timer.cancel()
                                    self._trigger_hold_timer = None
                                mode1_ledger = bool(self._blocked_trigger_ups)
                                mode1_entry = (
                                    self._blocked_trigger_ups.pop(0)
                                    if mode1_ledger else None)
                            if mode1_ledger:
                                if mode1_entry is _MODE1_PASS_THROUGH:
                                    # The matching DOWN went to the game, so
                                    # its UP must go there too. Do not run the
                                    # pending-classic bookkeeping: it belongs
                                    # to a different physical press.
                                    return user32.CallNextHookEx(
                                        self.hook, nCode, wParam, lParam)
                                # This UP belongs to a Mode-1 DOWN that never
                                # reached the game. Mark only that press's
                                # worker entry and consume the release.
                                if mode1_entry is not None:
                                    with self._pending_lock:
                                        mode1_entry["up"] = True
                                        if mode1_entry.get("worker_done"):
                                            try:
                                                self.pending_classics.remove(
                                                    mode1_entry)
                                            except ValueError:
                                                pass
                                return 1
                            if suppressed is not None and mode == 2 and armed:
                                if was_hold:
                                    # Was a hold: the hold timer reinjected a
                                    # DOWN and the continuous loop steered
                                    # until release.  Send the matching UP
                                    # now so the button isn't left stuck down.
                                    self._button(False)
                                else:
                                    # Single click: queue the classic job to
                                    # detect+snap+click exactly once.
                                    with self._pending_lock:
                                        suppressed["entry"]["up"] = True
                                        if suppressed["entry"].get("worker_done"):
                                            try:
                                                self.pending_classics.remove(
                                                    suppressed["entry"])
                                            except ValueError:
                                                pass
                                    self.click_q.put(
                                        ("classic", suppressed["x"], suppressed["y"],
                                         suppressed["now"], suppressed["entry"])
                                    )
                                return 1
                            else:
                                # No suppressed click (wasn't armed or not mode 2)
                                if self._release_bookkeeping(role, armed, mode, now, info.pt) == 1:
                                    return 1

            except Exception as exc:
                log("hook error: %r" % (exc,))
                if swallow:
                    # The press was already claimed for Mode 1/2 assistance.
                    # Do not let a bookkeeping exception turn it into a
                    # pass-through shot.
                    return 1

            return user32.CallNextHookEx(self.hook, nCode, wParam, lParam)

        return HOOKPROC(proc)

    def make_key_hook_proc(self):
        """
        Pass-through keyboard hook for single-key bindings.

        Never blocks anything: every key event is handed straight to
        Windows. When a bound single key is pressed (not a repeat, not
        injected, no modifier held, not while typing in our own text
        fields), the bound action ALSO fires - system-wide, in whatever
        app is focused.
        """
        def proc(nCode, wParam, lParam):
            try:
                if nCode == HC_ACTION and not self.stop.is_set():
                    info = ctypes.cast(
                        lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)
                    ).contents
                    vk = int(info.vkCode)

                    if wParam == WM_KEYUP:
                        self._key_held.discard(vk)
                        if self._track_target_vk is not None \
                                and int(vk) == int(self._track_target_vk):
                            self._stop_track_target()
                    elif wParam == WM_KEYDOWN:
                        if vk in self._key_held:
                            pass
                        else:
                            self._key_held.add(vk)
                            if not self.input_focused \
                                    and self._track_target_key_matches(vk):
                                self._start_track_target()
                            if info.flags & LLKHF_INJECTED:

                                # Injected events are our own macro output.
                                # They must pass through even when the key is
                                # also a macro trigger: a macro action may
                                # legitimately contain its own trigger key.
                                pass
                            elif not self.input_focused:

                                fired, swallow = self._macro_press(vk)
                                if not fired:

                                    self._record_press(vk)
                                if swallow:
                                    return 1
                                if not fired:
                                    self._maybe_fire_single_key(vk)
            except Exception as exc:
                log("key hook error: %r" % (exc,))

            return user32.CallNextHookEx(
                self.key_hook, nCode, wParam, lParam)

        return HOOKPROC(proc)

    def _maybe_fire_single_key(self, vk):

        hid = self._local_map.get(vk)
        if hid is None:
            return
        for mv in (VK_SHIFT, VK_CONTROL, VK_MENU):
            if user32.GetAsyncKeyState(mv) & 0x8000:
                return
        self.handle_hotkey(hid)

    def _mark_rec_full(self):
        if not self._rec_full:
            self._rec_full = True
            log("Recording capped at %d steps - further input is not "
                "captured." % REC_MAX_STEPS)

    def _record_press(self, vk):
        """
        Passive capture for the macro recorder: while a recording is
        armed (launched only) every deliberate physical press becomes a
        step (held modifiers, vk, gap since the previous step). Keys
        still reach the focused app - recording never blocks anything.
        The record-toggle combo itself is never captured, and injected
        events (macro playback) are filtered by the hook already.
        """
        with self._rec_lock:
            return self._record_press_inner(vk)

    def _record_press_inner(self, vk):
        if self._rec_buffer is None or not self.running:
            return

        held = 0
        for mask, mv in ((0x0002, VK_CONTROL), (0x0001, VK_MENU),
                         (0x0004, VK_SHIFT), (0x0008, VK_LWIN)):
            try:
                if user32.GetAsyncKeyState(mv) & 0x8000:
                    held |= mask
            except Exception:
                pass

        if (held, vk) in self._rec_combos:
            return
        if vk in (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, 0x5C):
            return

        now = time.perf_counter()
        delay = 0 if not self._rec_buffer else \
            int(round((now - self._rec_last_t) * 1000.0))
        self._rec_last_t = now
        if len(self._rec_buffer) >= REC_MAX_STEPS:
            self._mark_rec_full()
            return
        self._rec_buffer.append(
            (STEP_KEY, held, vk, max(0, min(5000, delay))))

    def _record_move(self, x, y):
        """
        Passive capture of cursor motion (v8.46): consecutive moves
        inside REC_MOVE_MERGE_S merge into ONE step whose position
        updates to the newest point, while the step keeps its original
        gap - so the recorded timeline stays exact (the next event's
        delay is measured from the merged step's first sample) and a
        smooth drag costs ~20 steps per second instead of one step per
        hardware event.
        """
        with self._rec_lock:
            return self._record_move_inner(x, y)

    def _record_move_inner(self, x, y):
        if self._rec_buffer is None or not self.running:
            return
        now = time.perf_counter()
        buf = self._rec_buffer
        if buf and buf[-1][0] == STEP_MOVE \
                and (now - self._rec_last_move) <= REC_MOVE_MERGE_S:
            buf[-1] = (STEP_MOVE, int(x), int(y), buf[-1][3])
            self._rec_last_move = now
            return
        if len(buf) >= REC_MAX_STEPS:
            self._mark_rec_full()
            return
        delay = 0 if not buf else \
            int(round((now - self._rec_last_t) * 1000.0))
        self._rec_last_t = now
        self._rec_last_move = now
        buf.append((STEP_MOVE, int(x), int(y),
                    max(0, min(5000, delay))))

    def _record_button(self, msg, x, y, mouse_data=0):
        """Passive capture of one physical mouse-button transition
        (any of the five buttons, down/up, double-click as a second down).
        The click still reaches the focused app untouched."""
        with self._rec_lock:
            return self._record_button_inner(msg, x, y, mouse_data)

    def _record_button_inner(self, msg, x, y, mouse_data=0):
        if self._rec_buffer is None or not self.running:
            return
        pair = _MOUSE_REC_MAP.get(int(msg))
        if pair is None:
            return
        btn, phase = pair
        if int(msg) in _MOUSE_REC_XBUTTON:
            btn = BTN_X1 if (int(mouse_data) >> 16) == XBUTTON1 else BTN_X2
        now = time.perf_counter()
        buf = self._rec_buffer
        if len(buf) >= REC_MAX_STEPS:
            self._mark_rec_full()
            return
        delay = 0 if not buf else \
            int(round((now - self._rec_last_t) * 1000.0))
        self._rec_last_t = now
        buf.append((STEP_BUTTON, btn, phase, int(x), int(y),
                    max(0, min(5000, delay))))

    def _rec_store_for(self, source):
        return self.rec_store_pad if source == "pad" else self.rec_store

    def rec_source(self):
        return self._rec_target if self._rec_buffer is not None else None

    def _start_recording(self, source="key"):
        if self._rec_buffer is not None:
            log("Already recording - press %s to stop and save."
                % str(self.cfg.get("key_record_stop", "ctrl+alt+s"))
                .lower())
            return
        if not self.running:
            log("Recording arms while launched - press Launch first "
                "(paused means the keyboard is entirely yours).")
            return
        with self._rec_lock:
            self._rec_buffer = []
            self._rec_target = source if source == "pad" else "key"
            self._rec_last_t = time.perf_counter()
            self._rec_last_move = 0.0
            self._rec_full = False
        self._pad_rumble(0.12, 0.7)
        if self._rec_target == "pad":
            log("RECORDING (controller) - every controller button and stick "
                "move is captured and still reaches your apps. Press the "
                "controller stop button to stop and save.")
        else:
            log("RECORDING - every key, mouse click and cursor move is "
                "captured and still reaches your apps. Press %s to stop "
                "and save."
                % str(self.cfg.get("key_record_stop", "ctrl+alt+s")).lower())

    def _stop_recording(self):
        self.stop_playback()
        if self._rec_buffer is None:
            log("Not recording - nothing to stop.")
            return
        with self._rec_lock:
            steps = self._rec_buffer
            self._rec_buffer = None
        source = self._rec_target if self._rec_target == "pad" else "key"
        self._rec_target = None
        if not steps:
            log("Recording stopped - no keys captured, nothing saved.")
            return
        store = self._rec_store_for(source)
        name = "Recording %d" % (len(store.recordings) + 1)
        rec = store.add(name, steps)
        if rec is None:
            if len(store.recordings) >= REC_MAX:
                log("Recording NOT saved: the %s library holds its cap of "
                    "%d recordings - delete one in the Macros tab "
                    "first." % ("controller" if source == "pad"
                                else "keyboard", REC_MAX))
            else:
                log("Recording not saved (no valid steps captured).")
            return
        if source == "pad":
            self.play_target_pad_rid = rec["id"]
        else:
            self.play_target_rid = rec["id"]
        self._pad_rumble(0.25, 1.0)
        if source == "pad":
            log("Controller recording saved as #%d: %r (%d steps). Rename it "
                "in the Macros tab; replay it with the controller play "
                "button."
                % (len(store.recordings), rec["name"], len(steps)))
        else:
            log("Recording saved as #%d: %r (%d steps). Rename it in the "
                "Macros tab; replay with %s, or Ctrl+1..5 by list position."
                % (len(store.recordings), rec["name"], len(steps),
                   str(self.cfg.get("key_play", "ctrl+alt+m")).lower()))

    def set_play_target(self, rid):
        """GUI selection: what the plain play hotkey replays."""
        self.play_target_rid = rid

    def set_play_target_pad(self, rid):
        self.play_target_pad_rid = rid

    def _play_recording(self, rid=None, source="key"):
        store = self._rec_store_for(source)
        rec = store.get(rid) if rid else None
        if rec is None:
            rec = store.most_recent()
        if rec is None:
            if source == "pad":
                log("Nothing recorded yet on the controller side - press the "
                    "controller record button to capture a sequence.")
            else:
                log("Nothing recorded yet - press %s to record a sequence."
                    % str(self.cfg.get("key_record", "ctrl+alt+r")).lower())
            return
        self._start_playback(rec, source)

    def _play_slot(self, position, source="key"):
        """Play the recording at that LIST POSITION (Ctrl+1..5, or the
        matching controller buttons for the pad library)."""
        store = self._rec_store_for(source)
        rec = store.by_position(position)
        if rec is None:
            if source == "pad":
                log("No controller recording at position %d (the controller "
                    "library has %d)."
                    % (position, len(store.recordings)))
            else:
                log("No recording at position %d (the library has %d). "
                    "Ctrl+1..5 play recordings by list order."
                    % (position, len(store.recordings)))
            return
        self._start_playback(rec, source)

    def _start_playback(self, rec, source="key"):
        """Queue one playback job in the recording's OWN saved style
        (v8.27: the "loop" flag on the recording itself - play once or
        repeat until stopped; the global play_mode/play_repeat settings
        are gone).

        Pressing play for the recording that is currently LOOPING
        stops it instead; starting a different job replaces the
        running one.

        Launched-only, like recording and macros: paused means the
        keyboard is entirely yours, so a play press while paused is
        refused instead of queued (the worker would otherwise inject
        it anyway - it only watches the stop event, not the state).
        """
        if not self.running:
            log("Playback arms while launched - press Launch first "
                "(paused means the keyboard is entirely yours).")
            return
        loop = bool(rec.get("loop"))

        if self._play_busy and self._play_rid == rec["id"] \
                and self._play_source == source \
                and (self._play_looping or loop):
            self.stop_playback()
            return

        if self._play_busy or self._play_looping \
                or self._play_rid is not None:
            self._play_cancel.set()

        if self._play_cancel.is_set():
            self.macro_q.put(("flush-cancel", None, None))
        self.macro_q.put(("rec", rec["steps"], "loop" if loop else "once"))
        self._play_rid = rec["id"]
        self._play_source = source
        self._play_looping = loop
        if source == "pad":
            self.play_target_pad_rid = rec["id"]
        else:
            self.play_target_rid = rec["id"]
        log("Playing recording %r (%d steps, %s)."
            % (rec["name"], len(rec["steps"]),
               "until stopped" if loop else "once"))

    def stop_playback(self):
        """Cancel a running OR QUEUED playback (responsive mid-gap).

        v8.49: this used to bail out unless `_play_busy or _play_looping`,
        and _start_playback sets neither for a play-once job until the
        worker picks it up - so pressing stop immediately after play did
        nothing at all and the recording injected anyway. _play_rid is set
        at queue time for every job, which makes it the reliable test."""
        if not (self._play_busy or self._play_looping
                or self._play_rid is not None):
            return False
        self._play_cancel.set()
        self._play_looping = False
        self._play_source = None
        log("Playback stopped.")
        return True

    def _macro_press(self, vk):
        """
        Fire the macro bound to (held modifiers, vk), if any.

        Returns (fired, swallow):
          fired  - a combo was queued for the worker;
          swallow - the physical press must be blocked (return 1 from
                    the hook) so the focused app only sees the injected
                    combo - "D = D types one D".

        v8.27: every macro is blocked and single-shot - one trigger
        press injects exactly one combo press, and only while launched
        (paused, the keyboard is entirely yours). The trigger fires
        with EXACTLY its own modifiers held, so Ctrl+<trigger> etc.
        stay with the focused app. The hook only queues; the worker
        injects.
        """
        held = self._held_mods()
        spec = self._macro_map.get((held, vk))
        if spec is None:
            return False, False
        if not self.running:
            return False, False
        _tm, _tv, avks = spec
        self.macro_q.put(("macro", avks))
        log("Macro: trigger fired - sending %d key tap(s)." % len(avks))
        return True, True

    def _held_mods(self):
        held = 0
        for mask, mv in ((0x0002, VK_CONTROL), (0x0001, VK_MENU),
                         (0x0004, VK_SHIFT), (0x0008, VK_LWIN)):
            try:
                if user32.GetAsyncKeyState(mv) & 0x8000:
                    held |= mask
            except Exception:
                pass
        return held

    def _play_macro_sequence(self, avks):
        """Tap each key in an action sequence, 10 ms apart.

        This is intentionally independent of the macro worker: several
        macros can play at the same time, and the keyboard hook never
        waits for one.  inject_combo(0, vk) sends one down+up tap; the
        sleep belongs to this thread only.
        """
        try:
            for vk in tuple(avks or ()):
                inject_combo(0, int(vk))
                time.sleep(0.010)
        except Exception as exc:
            log("macro sequence error: %r" % (exc,))

    def run_macro_worker(self):
        """Inject queued macro combos and recordings (the hook thread
        never sleeps). Jobs: ("combo", mods, vk) - one press - and
        ("rec", steps, mode) with mode "once"/"loop".

        v8.46: recording steps are TAGGED (see recordings.py): keys
        inject as before, moves teleport the cursor, and button steps
        aim the cursor at their recorded point first, then press or
        release left/right/middle. Everything injects with the
        LLMHF_INJECTED/LLKHF_INJECTED marks, so neither the assistance
        hooks nor a running recorder ever react to playback."""
        while not self.stop.is_set():
            try:
                job = self.macro_q.get(timeout=0.2)
            except queue.Empty:
                continue

            if job[0] == "macro":
                _kind, avks = job
                # Run every macro in its own thread.  The worker stays
                # free to start the next macro immediately, and the
                # 10 ms gaps inside one sequence never delay another
                # macro or the keyboard hook.
                threading.Thread(
                    target=self._play_macro_sequence, args=(avks,),
                    name="macro-sequence", daemon=True).start()
                continue

            if job[0] == "combo":
                _kind, amods, avk = job
                try:
                    inject_combo(amods, avk)
                except Exception as exc:
                    log("macro inject error: %r" % (exc,))
                continue

            if job[0] == "padmask":
                _kind, mask = job
                try:
                    self._pad_emit_mask(mask)
                except Exception as exc:
                    log("controller macro error: %r" % (exc,))
                continue

            if job[0] == "flush-cancel":

                self._play_cancel.clear()
                continue

            if job[0] == "rec":
                _kind, steps, mode = job
                self._play_busy = True
                passes = 1 if mode != "loop" else None
                done = 0
                while not self.stop.is_set() \
                        and not self._play_cancel.is_set():
                    for step in steps:
                        if self.stop.is_set() \
                                or self._play_cancel.is_set():
                            break
                        delay = int(step[-1])
                        if delay > 0:
                            end = time.perf_counter() \
                                + min(5.0, delay / 1000.0)
                            while time.perf_counter() < end:
                                if self.stop.is_set() or \
                                        self._play_cancel.is_set():
                                    break
                                time.sleep(0.05)
                        if self.stop.is_set() \
                                or self._play_cancel.is_set():
                            break
                        try:
                            tag = int(step[0])
                            if tag == STEP_MOVE:
                                move_cursor(step[1], step[2])
                            elif tag == STEP_BUTTON:
                                move_cursor(step[3], step[4])
                                time.sleep(0.008)
                                inject_mouse(step[1],
                                             step[2] == PHASE_DOWN)
                            elif tag == STEP_PAD_BUTTON:
                                self._pad_emit_button(
                                    step[1], step[2] == PHASE_DOWN)
                            elif tag == STEP_PAD_AXIS:
                                self._pad_emit_axis(step[1], step[2])
                            elif tag == STEP_KEY:
                                inject_combo(step[1], step[2])
                            else:
                                log("recording step tag %d is unknown; "
                                    "step skipped." % tag)
                        except Exception as exc:
                            log("recording inject error: %r" % (exc,))
                            self._play_cancel.set()
                            break
                    done += 1
                    if passes is not None and done >= passes:
                        break
                self._play_busy = False
                self._play_looping = False

                self._play_rid = None
                self._play_source = None

                if self._play_cancel.is_set():
                    self._play_cancel.clear()

    def _mode_allows_mouse2_follow(self):
        """False in mode 1: there the right button is aim-down-sights,
        and any follow steering would yank the cursor mid-ADS. Mode 2
        keeps the follow features. Garbage mode values fail open
        (old behaviour) rather than silently dropping a feature."""
        return self.current_mode() != 1

    def _mouse2_hold_action(self):
        """Validated mouse2_hold_action value (1-4, fallback 4)."""
        try:
            action = int(self.cfg.get("mouse2_hold_action", 4))
        except (TypeError, ValueError):
            return 4
        return action if action in (1, 2, 3, 4) else 4

    def _armed_now(self):
        """Live version of the hook's per-event 'armed' gate, checked
        again when a deferred right-button action actually fires (hold
        timer / button release) - pausing mid-press cancels it."""
        return (self.running
                and not self.scanner_dead
                and time.perf_counter() >= self.pause_until
                and (not self._bypass_held() or not self._bypass_enabled()))

    def _start_mouse2_hold_timer(self):
        """Arm the hold detector for the current right-button press.
        A short timer thread (never the hook itself, which must stay
        fast) fires the hold action if the button is still down."""
        with self._rbtn_lock:
            self._rbtn_hold_done = False
            self._rbtn_press_id += 1
            pid = self._rbtn_press_id
        t = threading.Timer(MOUSE2_HOLD_SECONDS,
                            self._mouse2_hold_check, args=(pid,))
        t.daemon = True
        t.start()

    def _mouse2_hold_check(self, pid):
        """Timer callback: still holding the same press? -> run the
        HOLD action (mouse2_hold_action)."""
        try:
            with self._rbtn_lock:
                if (pid != self._rbtn_press_id
                        or not self._rbtn_down
                        or self._rbtn_hold_done):
                    return

            try:
                still = bool(user32.GetAsyncKeyState(self._options_vk()) & 0x8000)
            except Exception:
                still = False
            if not (still and self._armed_now()):

                return
            with self._rbtn_lock:
                if pid != self._rbtn_press_id or self._rbtn_hold_done:
                    return
                self._rbtn_hold_done = True
            try:
                self._handle_mouse2("mouse2_hold_action",
                                    "right-button hold")
            except Exception:
                with self._rbtn_lock:
                    if pid == self._rbtn_press_id:
                        self._rbtn_hold_done = False
                raise
        except Exception as exc:
            log("mouse2 hold error: %r" % (exc,))

    def _trigger_hold_check(self, pid):
        """Timer callback: still holding the trigger? -> this was a hold.
        Reinject the suppressed click and start continuous assist."""
        try:
            with self._trigger_lock:
                if (pid != self._trigger_press_id
                        or not self._lbtn_down
                        or self._trigger_hold_done):
                    return
                suppressed = self._trigger_suppressed_down
                if suppressed is None:
                    return
                self._trigger_hold_done = True
            # Reinject the suppressed DOWN so the game sees the click
            try:
                self._button(True)
            except Exception:
                pass
            # Remove the pending entry since we're not doing single-click
            with self._pending_lock:
                try:
                    self.pending_classics.remove(suppressed["entry"])
                except ValueError:
                    pass
            # Hand steering over to the continuous loop: the centre-hold
            # EMA loop would otherwise nudge for the same sighting too.
            with self._state_lock:
                self._center_hold = None
                self._center_hold_tomb = 0.0
            # Start continuous assist loop in background
            self._start_continuous_assist(suppressed["x"], suppressed["y"])
        except Exception as exc:
            log("trigger hold error: %r" % (exc,))

    def _start_continuous_assist(self, start_x, start_y):
        """Start a background loop that continuously detects and nudges
        while the trigger is held. Runs in a daemon thread."""
        # One loop at a time: a second hold timer must not double-steer.
        with self._continuous_lock:
            if self._continuous_active:
                return
            self._continuous_active = True
        # A held trigger is a fresh detection chain.  The color lock acquired
        # here is reused frame-by-frame until it is lost or the trigger is
        # released; a second press starts over at the human detector.
        self._reset_target_lock()
        # Use a list to hold mutable coordinates (avoids closure capture issues)
        coords = [start_x, start_y]
        def loop():
            epoch = self._mode_epoch
            with self._state_lock:
                view_epoch = self._view_epoch
            track_id = 0
            mismatch_misses = 0
            guidance = {}
            try:
                while not self.stop.is_set():
                    # Check if trigger is still held and we're still armed
                    if not self._lbtn_down or not self._armed_now() \
                            or epoch != self._mode_epoch:
                        break
                    with self._state_lock:
                        current_view_epoch = self._view_epoch
                    if current_view_epoch != view_epoch:
                        # Backend tracking intentionally restarts after our
                        # own camera move; let the next post-move frame adopt
                        # its replacement ID rather than rejecting it as a
                        # different person.
                        view_epoch = current_view_epoch
                        track_id = 0
                        mismatch_misses = 0
                    # Do a guide-style detection and nudge (no click)
                    # Use a lightweight state for this iteration
                    state = {"abandoned": False, "lock": threading.Lock(),
                             "epoch": epoch, "continuous": True,
                             "guidance": guidance}
                    if track_id > 0:
                        state["track_id"] = track_id
                    out = {}
                    self.do_guide(coords[0], coords[1], out, state)
                    try:
                        track_id = int(state.get("track_id", track_id) or 0)
                    except (TypeError, ValueError):
                        track_id = 0
                    if state.get("track_mismatch"):
                        mismatch_misses += 1
                        if mismatch_misses >= 3:
                            track_id = 0
                            mismatch_misses = 0
                    elif state.get("track_id"):
                        mismatch_misses = 0
                    # Update coords for next iteration (cursor moved)
                    if self.last_snap_pos:
                        coords[0], coords[1] = self.last_snap_pos
                    # Poll near the detector cadence. _scan_center consumes a
                    # sequence only once, so this cannot replay a sighting;
                    # the old 30 Hz loop added up to 33 ms of avoidable
                    # response latency on top of an 8-10 ms detector.
                    if self.stop.wait(TRACK_TARGET_INTERVAL_S):
                        break
            except Exception as exc:
                log("continuous assist error: %r" % (exc,))
            finally:
                with self._continuous_lock:
                    self._continuous_active = False
        t = threading.Thread(target=loop, name="continuous-assist", daemon=True)
        t.start()

    def _finish_mouse2(self, now):
        """Right button released: fire the CLICK action unless the
        press was (or just became) a hold - a press is never both."""
        try:
            with self._rbtn_lock:
                armed_down = self._rbtn_armed
                self._rbtn_armed = False
                held = self._rbtn_hold_done
                self._rbtn_hold_done = False
                self._rbtn_press_id += 1
                dur = now - self.rbutton_down_time
            if held or not armed_down:
                return
            if not self._armed_now():
                return
            if dur >= MOUSE2_HOLD_SECONDS:

                self._handle_mouse2("mouse2_hold_action",
                                    "right-button hold")
            else:
                self._handle_mouse2("mouse2_action", "right-click")
        except Exception as exc:
            log("mouse2 release error: %r" % (exc,))

    def _handle_mouse2(self, key="mouse2_action", label="right-click"):
        """Programmable right-button side action, fired by a short
        press (key = mouse2_action) or a hold (key =
        mouse2_hold_action). The right button itself always keeps its
        normal Windows function - we only react alongside, never
        suppress it."""
        try:
            action = int(self.cfg.get(key, 4))
        except (TypeError, ValueError):
            action = 4

        if action in (2, 3) and not self._mode_allows_mouse2_follow():

            if time.perf_counter() - self._mouse2_log_at > 5.0:
                self._mouse2_log_at = time.perf_counter()
                log("Right-button follow is a mode-2 feature - "
                    "ignored in mode 1.")
            return

        if action == 1:
            with self._state_lock:
                locked = self.follow_lock is not None
                self.follow_lock = None
            if locked:
                log("Current follow task cleared.")

        elif action == 2:
            with self._state_lock:
                if self.follow_lock and self.follow_lock.get("active"):
                    self.follow_lock = None
                    toggled = True
                else:
                    toggled = False
            if toggled:
                log("Colour follow toggled off.")
                return
            _sig, _when = self.follow.snapshot()[2:]
            if _sig is not None and _when > 0:
                rgb = detection.hsv_to_rgb(*_sig)
                with self._state_lock:
                    self.follow_lock = {
                        "active": True,
                        "accent": "%d,%d,%d" % rgb,
                        "source": "mouse2",
                        "misses": 0,
                    }
                log("Following snapped colour rgb%s (%s or "
                    "clear to stop)." % (rgb, label))
            else:
                log("Nothing snapped yet - nothing to follow.")

        elif action == 3:
            with self._state_lock:
                self.follow_lock = {
                    "active": True,
                    "accent": None,
                    "pick_until": time.perf_counter() + 2.0,
                    "source": "mouse2",
                    "misses": 0,
                }
            log("Pick window open: place the cursor on the colour "
                "(2 seconds)...")

    def _sample_cursor_color(self):
        """RGB tuple of the pixel under the cursor, or None."""
        try:
            x, y = get_cursor_pos()
            img = self._capture.grab(x, y, x + 1, y + 1)
            if img is None:
                return None
            b, g, r = (int(v) for v in img[0, 0])
            return (r, g, b)
        except Exception:
            return None

    def _center_follow_cycle(self):
        """Retired: Game Mode never starts an unsolicited centre chase."""
        return False

    @staticmethod
    def _new_hold_session():
        """Fresh centre-hold state.

        No EMA remains from older frames: a target that has stopped must stop
        receiving corrections immediately. The command stamps are used only
        to withhold a repeated turn until a newer rendered frame can show the
        previous one's effect.
        """
        return {"last_t": time.perf_counter(), "last_tap": 0.0,
                "track_id": 0, "view_epoch": 0,
                "last_seq": 0, "last_cmd_at": 0.0,
                "last_cmd_offset": (0.0, 0.0), "cmd_pending": False,
                "center_latched": False}

    def _center_hold_cycle(self):
        """One pass of a centre-watch hold session (mouse1 still down).

        Re-scan the watched square and steer only by the *current* measured
        offset. Once the centre is inside the three-pixel deadband there is
        nothing left to correct, and the quiet state stays latched through
        five pixels of box/body wobble. This stops a stationary target from
        being pushed past centre and back again.

        Returns True when the session continues (caller sleeps ~one
        frame gap), False when it just ended (caller falls through
        to the normal follow logic). Ends on button-up, pause, stop,
        mode switch, reload or quit - release frees the mouse
        completely.
        """
        with self._state_lock:
            sess = self._center_hold
        if sess is None:
            return False
        if self.stop.is_set() or not self.running \
                or time.perf_counter() < self.pause_until \
                or (not self._lbtn_down and not self._pad_click_down
                    and not self._pad_raw_down):
            with self._state_lock:
                if self._center_hold is sess:
                    self._center_hold = None
            return False
        now = time.perf_counter()
        sess["last_t"] = now
        reticle = self._reticle_active()
        with self._state_lock:
            view_epoch = self._view_epoch
        if sess.get("view_epoch") != view_epoch:
            sess["view_epoch"] = view_epoch
            sess["track_id"] = 0
        cbox = self._center_box()
        if cbox is not None:
            try:
                got = self._scan_center(
                    None,
                    expected_track_id=sess.get("track_id"),
                )
            except Exception as exc:
                log("centre-hold scan error: %r" % (exc,))
                got = None
            anchor = (cbox[0], cbox[1])
        else:
            try:
                px, py = get_cursor_pos()
            except RuntimeError:
                return False
            try:
                t = self.scan_near(px, py, None, False)
            except Exception as exc:
                log("centre-hold scan error: %r" % (exc,))
                t = None
            got = (t, px, py) if t is not None else None
            anchor = (px, py)
        with self._state_lock:
            if self._center_hold is not sess:
                return False
        if got is None:
            steer = False
        elif not self._human_target_ok(got[0]):
            # Classical/high-contrast blobs (crosshair, bullet, muzzle flash,
            # HUD dot) are rejected during an assisted hold: only a confirmed
            # human (YOLO detect, pose, or the seeded colour lock) may steer.
            steer = False
        elif not self._target_measured(got[0]):
            steer = False
        elif bool(self.cfg.get("subject_priority", False)):
            try:
                _hs0 = float(getattr(got[0], "hscore", 0.0) or 0.0)
            except (TypeError, ValueError):
                _hs0 = 0.0
            try:
                if _hs0 >= 0.30:
                    steer = True
                else:
                    steer = _shape_tier(
                        got[0].bw, got[0].bh,
                        getattr(got[0], "area", 0) or 0) > 0
            except Exception:
                steer = False
        else:
            steer = True

        # No fresh sighting means no steering. Keep the session alive so the
        # target may return while the button is still held.
        if not steer:
            sess["cmd_pending"] = False
            return True

        target, _mx, _my = got
        target_track_id = int(getattr(target, "track_id", 0) or 0)
        if target_track_id > 0 and not sess.get("track_id"):
            sess["track_id"] = target_track_id
        if reticle:
            tx, ty = float(target.dx), float(target.dy)
        else:
            try:
                _pcx, _pcy = get_cursor_pos()
                tx = (anchor[0] + float(target.dx)) - _pcx
                ty = (anchor[1] + float(target.dy)) - _pcy
            except Exception:
                tx, ty = float(target.dx), float(target.dy)

        # The detector reports the target's integer centre. A target already
        # occupying the centre pixel (or within the Mode-2 deadband) receives
        # exactly zero correction; there is no residual motion to chase.
        ix, iy = int(round(tx)), int(round(ty))
        center_distance = math.hypot(ix, iy)
        if center_distance <= HOLD_CENTER_DEADBAND_PX:
            sess["center_latched"] = True
        elif center_distance > HOLD_CENTER_RELEASE_PX:
            sess["center_latched"] = False
        if bool(sess.get("center_latched")) \
                and center_distance <= HOLD_CENTER_RELEASE_PX:
            sess["cmd_pending"] = False
            sess["last_cmd_offset"] = (tx, ty)
            return True

        cap = self._max_step_cfg()
        if reticle:
            area_cap = 2.0 * max(1, self.radius_px())
            if cap <= 0 or cap > area_cap:
                cap = area_cap
        jx, jy = self._cap_step(ix, iy, cap)
        if jx == 0 and jy == 0:
            sess["cmd_pending"] = False
            return True

        # Send one correction per fresh measurement.  The speed-limited
        # servo already prevents overshoot; waiting for a camera-response
        # confirmation just makes fast targets stutter and get lost.
        with self._state_lock:
            if self._center_hold is not sess \
                    or (not self._lbtn_down
                        and not self._pad_click_down
                        and not self._pad_raw_down):
                return False
        old_seq = int(sess.get("last_seq", 0) or 0)
        new_seq = int(getattr(target, "seq", 0) or 0)
        if new_seq > 0 and new_seq <= old_seq:
            # Same frame as the last command - don't replay it.
            return True

        if reticle:
            self._steer_target_h = float(getattr(target, "bh", 0.0) or 0.0)
            sent = self._steer_view(jx, jy, hold=True)
        else:
            sent = self._nudge(jx, jy, view=False)
        if sent:
            sess["last_seq"] = new_seq
            sess["last_cmd_at"] = now
            sess["last_cmd_offset"] = (float(tx), float(ty))
        else:
            sess["cmd_pending"] = False
        if now - sess.get("last_beat", 0.0) > 2.0:
            sess["last_beat"] = now
            log("Centre hold: steering (%+d,%+d) toward a measured "
                "offset of (%+.0f,%+.0f)." % (jx, jy, tx, ty))
        interval = float(getattr(self._input, "tap_interval", 0) or 0)
        if interval > 0:
            if now - sess.get("last_tap", 0.0) >= interval:
                sess["last_tap"] = now
                self._button(True)
                self._button(False)
        return True

    def run_follow_worker(self):
        """
        While a follow-lock is active, keep the cursor on the locked
        colour's nearest item (stabilises drags, e.g. scrollbar thumbs).

        And centre-watch holds: while mouse1 is held after a centre
        snap/press, re-scan the watched square about every 2-3 frames
        and steer onto each fresh sighting with relative nudges (the
        addon model - single clicks snap exactly once, only a HELD
        button keeps steering alive).
        """
        while not self.stop.is_set():
            if self.scanner_dead and self.running:
                try:
                    warm = np.full((48, 48, 3), 128, dtype=np.uint8)
                    detection.scan_region(warm, 24, 24, self.cfg.values, 20)
                    self.scanner_dead = False
                    self.fail_count = 0
                    log("Scanner recovered.")
                except Exception:
                    pass
                time.sleep(2.0)
                continue
            if not self.running \
                    or time.perf_counter() < self.pause_until:
                time.sleep(0.05)
                continue

            if self._bypass_enabled() and self._bypass_held():
                time.sleep(0.05)
                continue

            passive_motion = self._sample_yolo_motion()

            # Log hpose hit-rate instrumentation once a minute.
            if self.running and time.perf_counter() - self._stats_log_at > 60.0:
                self._stats_log_at = time.perf_counter()
                try:
                    import detection as hpose_bridge
                    st = hpose_bridge.stats()
                    if st and st["frames"] > 20:
                        log("hpose: %d frames | hit %.0f%% | avg %.0f ms "
                            "(hit %.0f / miss %.0f)"
                            % (st["frames"], 100 * st["hit_rate"],
                               st["avg_ms"], st["avg_hit_ms"],
                               st["avg_miss_ms"]))
                except Exception:
                    pass

            with self._state_lock:
                _hold = self._center_hold
            if _hold is not None:
                try:
                    if self._center_hold_cycle():
                        time.sleep(TRACK_TARGET_INTERVAL_S)
                        continue
                    time.sleep(0.05)
                    continue
                except Exception as exc:
                    log("centre-hold cycle error: %r" % (exc,))
                    time.sleep(0.05)
                    continue

            with self._state_lock:
                lock = self.follow_lock
            # Game Mode is a centre-anchored scan, not an always-on chase.
            # Steering exists only for an explicit click/hold transaction.
            # The former background centre-follow silently picked a target
            # whenever the cursor happened to be visible and kept moving after
            # Mode 1 completed, which was neither requested nor controllable.
            with self._state_lock:
                self._center_follow = None
                self._pick_at = None
                self._chase_ref = None
            if not lock or not lock.get("active"):
                # Passive history is a cheap read from the already-running
                # detector. Sample near its cadence so four measured frames
                # establish motion before the next explicit click/hold.
                time.sleep(0.01 if (passive_motion
                                    or (self._motion_enabled()
                                        and self._yolo26n is not None))
                           else 0.05)
                continue

            with self._state_lock:
                lbtn_down = (self._lbtn_down or self._pad_click_down
                             or self._pad_raw_down)
            if lock.get("source") == "hold" and not lbtn_down:
                with self._state_lock:
                    if self.follow_lock is lock:
                        self.follow_lock = None
                continue

            if lock.get("source") == "mouse2" \
                    and not self._mode_allows_mouse2_follow():
                time.sleep(0.05)
                continue

            try:
                if lock.get("pick_until"):
                    if time.perf_counter() >= lock["pick_until"]:
                        rgb = self._sample_cursor_color()
                        with self._state_lock:
                            live = self.follow_lock is lock
                            if live:
                                if rgb:
                                    lock["accent"] = "%d,%d,%d" % rgb
                                    lock["pick_until"] = None
                                else:
                                    self.follow_lock = None
                        if live:
                            if rgb:
                                log("Locked onto colour rgb%s - following."
                                    % (rgb,))
                            else:
                                log("Colour pick failed - follow cancelled.")
                    time.sleep(0.05)
                    continue

                accent = lock.get("accent")
                follow_hint = None
                if accent:
                    try:
                        _parsed = _parse_accents(accent)
                        if _parsed:
                            ah, asat, av = _parsed[0]
                            follow_hint = {"sig": (ah, asat, av),
                                           "area": float(
                                               lock.get("last_area", 0) or 0)}
                    except Exception:
                        follow_hint = None

                x, y = get_cursor_pos()
                radius = self.radius_px()
                img, cx, cy, origin, _edges = self.grab_around(x, y,
                                                               radius)
                if img is not None:
                    cfgv = dict(self.cfg.values)
                    if accent:
                        cfgv["accent_color"] = accent
                        cfgv["accent_only"] = True
                    else:

                        cfgv["accent_color"] = "off"
                        cfgv["accent_only"] = False
                    try:
                        _sctx = self._subject_context(origin)
                    except Exception:
                        _sctx = None
                    res = detection.scan_region(img, cx, cy, cfgv, radius,
                                             subject_ctx=_sctx,
                                             follow_hint=follow_hint)
                    with self._state_lock:
                        live = self.follow_lock is lock
                    if not live:
                        continue
                    if res is not None and res.kind == "target" and res.target is not None:
                        lock["misses"] = 0

                        ax = origin[0] + cx + res.target.dx
                        ay = origin[1] + cy + res.target.dy
                        try:
                            self._subject_store(ax, ay, res.target)
                        except Exception:
                            pass
                        on_td = 0.0
                        fx, fy = get_cursor_pos()
                        if math.hypot(ax - fx, ay - fy) > on_td:
                            self._warp(ax, ay)
                            with self._state_lock:
                                self._follow_stable = 0
                        else:

                            with self._state_lock:
                                self._follow_stable += 1
                    else:

                        lock["misses"] = lock.get("misses", 0) + 1
                        with self._state_lock:
                            self._follow_stable = 0
                            if lock["misses"] >= 12 \
                                    and self.follow_lock is lock:
                                self.follow_lock = None
            except Exception as exc:
                log("follow worker error: %r" % (exc,))
                with self._state_lock:
                    self._follow_stable = 0

            with self._state_lock:
                _stable = self._follow_stable
            time.sleep(0.08 if _stable >= 4 else 0.04)

    AUTO_CLICK_INTERVAL = 0.05
    AUTO_CLICK_MIN_CONTRAST = 60.0

    AUTO_CLICK_ENABLED = False

    def _auto_can_arm(self):
        """Auto-click has no editable control, so it is permanently retired.

        The trigger-click paths own all assisted clicking now. Keeping the
        former switch out of the runtime also means an old config.ini entry
        cannot resurrect an invisible behaviour.
        """
        return self.AUTO_CLICK_ENABLED

    def _auto_reset_session(self):
        """Re-arm the announcement (on launch).

        The first scan also waits one full interval, so arming never
        clicks before the user has seen that auto-click is on.
        """
        with self._auto_lock:
            self._auto_armed = False
            self._auto_next = time.perf_counter() \
                + max(0.05, self.AUTO_CLICK_INTERVAL)

    def _auto_scan_config(self):
        """Config snapshot for the auto scan.

        accent_only is forced on ONLY when the user actually set a colour
        (then it is their explicit statement of intent and nothing else
        may qualify). With no accent colour configured the scan degrades
        to plain contrast-first ranking - the same engine every manual
        path uses - held to the fixed 60 crispness gate below. Every
        other setting (accent colour, exclude list, tolerances, size
        gates) is the user's own, untouched.
        """
        cfgv = dict(self.cfg.values)
        accents = _parse_accents(cfgv.get("accent_color"))
        cfgv["accent_only"] = bool(accents)
        cfgv["min_contrast"] = self.AUTO_CLICK_MIN_CONTRAST
        return cfgv

    def _auto_pick(self):
        """Find the best auto-click target: (abs_x, abs_y) or None.

        Scans the CENTRE of the screen - the place a person means by "the
        button in the middle" - not wherever the cursor happens to rest,
        so a click is decided by the target and not by mouse drift.
        subject_only (the strict game mode) additionally requires a figure
        silhouette: without one in the field there is no shot (never a
        fallback click). subject_priority only steers RANKING (soft), so
        desktop auto-click keeps working with the default settings.
        """
        vx, vy, vw, vh = self._virtual_screen()
        cx = vx + vw // 2
        cy = vy + vh // 2

        radius = self.radius_px()

        img, ix, iy, origin, edges = self.grab_around(cx, cy, radius)
        if img is None:
            return None

        cfgv = self._auto_scan_config()

        strict = bool(self.cfg.get("subject_only", False))
        ranking = strict or bool(self.cfg.get("subject_priority", False))
        try:
            subject_ctx = self._subject_context(origin) if ranking else None
        except Exception:
            subject_ctx = None
        try:
            res = scan_region(img, ix, iy, cfgv, radius, edge_flags=edges,
                              require_tier=(2 if strict else 0),
                              subject_ctx=subject_ctx)
        except Exception as exc:
            log("auto-click scan error: %r" % (exc,))
            return None

        if res is None or res.kind != "target" or res.target is None:
            return None

        try:
            self._subject_store(cx + res.target.dx, cy + res.target.dy, res.target)
        except Exception:
            pass

        return cx + res.target.dx, cy + res.target.dy

    def _motion_score(self, x, y, now):
        """Return how much a target at (x, y) has moved since its last seen
        position.

        Uses the shared motion history across ALL detection types (YOLO,
        pose, colour, contrast).  A stationary target gets ~0; a moving
        target gets pixels-per-frame displacement.  This is used to prefer
        moving humans over stationary false positives (crosshair, HUD,
        bullets).
        """
        if not self._motion_history:
            return 0.0
        last_x, last_y, last_t = self._motion_history[-1]
        dt = float(now) - float(last_t)
        if dt <= 0.0 or dt > 0.5:
            return 0.0
        return math.hypot(float(x) - float(last_x),
                          float(y) - float(last_y)) / max(dt, 0.001)

    @staticmethod
    def _fit_screen_velocity(points):
        """Weighted screen velocity and residual for timestamped points."""
        if len(points) < 2:
            return 0.0, 0.0, float("inf")
        t_ref = float(points[-1][0])
        ts = np.asarray([float(p[0]) - t_ref for p in points],
                        dtype=np.float64)
        xs = np.asarray([float(p[1]) for p in points], dtype=np.float64)
        ys = np.asarray([float(p[2]) for p in points], dtype=np.float64)
        weights = np.linspace(0.55, 1.0, len(points), dtype=np.float64)
        wsum = float(weights.sum())
        mt = float(np.dot(weights, ts) / wsum)
        denom = float(np.dot(weights, (ts - mt) ** 2))
        if denom <= 1e-9:
            return 0.0, 0.0, float("inf")
        mx = float(np.dot(weights, xs) / wsum)
        my = float(np.dot(weights, ys) / wsum)
        vx = float(np.dot(weights, (ts - mt) * (xs - mx)) / denom)
        vy = float(np.dot(weights, (ts - mt) * (ys - my)) / denom)
        fit_x = mx + vx * (ts - mt)
        fit_y = my + vy * (ts - mt)
        residual = float(np.sqrt(np.dot(
            weights, (xs - fit_x) ** 2 + (ys - fit_y) ** 2) / wsum))
        return vx, vy, residual

    def _motion_prediction(self, now=None):
        """Latency-aware lead from recent measured detector centres.

        The estimate deliberately answers the movement questions from screen
        observations rather than continuously invoking pose: recent direction,
        speed, a bounded speed change, a stop/reversal, and detector age all
        affect the result. Zig-zag/body-animation jitter has low path
        efficiency and is rejected instead of being magnified into camera
        movement.
        """
        now = time.perf_counter() if now is None else float(now)
        with self._state_lock:
            observations = list(self._track)
            measured_latency = float(
                getattr(self, "_motion_latency_s", 0.0) or 0.0)
        if len(observations) < 4:
            return 0.0, 0.0, ""

        latest_t = float(observations[-1][0])
        age = now - latest_t
        if not math.isfinite(age) or age < 0.0 or age > 0.18:
            return 0.0, 0.0, ""

        # About a quarter second is enough to establish intent while still
        # forgetting the old direction quickly after a turn or landing.
        points = [p for p in observations
                  if latest_t - 0.24 <= float(p[0]) <= latest_t]
        points = points[-10:]
        if len(points) < 4 \
                or latest_t - float(points[0][0]) < MOTION_MIN_SPAN_S:
            return 0.0, 0.0, ""

        path = 0.0
        useful_segments = []
        for first, second in zip(points, points[1:]):
            dt = float(second[0]) - float(first[0])
            if dt <= 0.0 or dt > 0.16:
                continue
            dx = float(second[1]) - float(first[1])
            dy = float(second[2]) - float(first[2])
            distance = math.hypot(dx, dy)
            path += distance
            if distance > 0.0:
                useful_segments.append((dx / dt, dy / dt))
        displacement = math.hypot(float(points[-1][1]) - float(points[0][1]),
                                  float(points[-1][2]) - float(points[0][2]))
        if path <= MOTION_STATIONARY_RADIUS_PX \
                or displacement <= MOTION_STATIONARY_RADIUS_PX \
                or not useful_segments:
            return 0.0, 0.0, ""
        tail = points[-3:]
        tail_path = sum(math.hypot(float(second[1]) - float(first[1]),
                                   float(second[2]) - float(first[2]))
                        for first, second in zip(tail, tail[1:]))
        tail_displacement = math.hypot(
            float(tail[-1][1]) - float(tail[0][1]),
            float(tail[-1][2]) - float(tail[0][2]))
        if tail_path <= 2.5 and tail_displacement <= 1.5:
            # Two quiet/reversing intervals are enough to recognize a stop;
            # do not carry the earlier walking velocity through it.
            return 0.0, 0.0, ""
        efficiency = displacement / max(path, 1e-6)
        if efficiency < 0.55:
            return 0.0, 0.0, ""

        # Fit the latest few frames so a changed direction replaces the old
        # one promptly. The residual gate rejects pose/box-centre wobble.
        recent = points[-6:]
        while len(recent) > 4 \
                and float(recent[-1][0]) - float(recent[0][0]) > 0.14:
            recent.pop(0)
        vx, vy, residual = self._fit_screen_velocity(recent)
        speed = math.hypot(vx, vy)
        if speed < MOTION_MIN_SPEED_PX_S \
                or residual > max(2.5, 0.30 * displacement):
            return 0.0, 0.0, ""

        agreeing = sum(1 for sx, sy in useful_segments[-5:]
                       if sx * vx + sy * vy > 0.0)
        if agreeing < max(2, int(math.ceil(
                0.60 * min(5, len(useful_segments))))):
            return 0.0, 0.0, ""

        ax = ay = 0.0
        older = points[:-2]
        if len(older) >= 3:
            ovx, ovy, _old_residual = self._fit_screen_velocity(older[-6:])
            old_speed = math.hypot(ovx, ovy)
            old_mid = 0.5 * (float(older[-1][0]) + float(older[0][0]))
            new_mid = 0.5 * (float(recent[-1][0]) + float(recent[0][0]))
            accel_dt = new_mid - old_mid
            # A reversal is a new intent, not enormous acceleration through
            # zero. Use only the recent direction until more frames arrive.
            if accel_dt > 0.02 and (old_speed < MOTION_MIN_SPEED_PX_S
                                    or ovx * vx + ovy * vy > 0.0):
                ax = (vx - ovx) / accel_dt
                ay = (vy - ovy) / accel_dt

        horizon = max(age, measured_latency) + MOTION_OUTPUT_DELAY_S
        horizon = min(MOTION_MAX_HORIZON_S, max(0.0, horizon))
        if horizon <= 0.0:
            return 0.0, 0.0, ""
        ox = vx * horizon
        oy = vy * horizon

        # Acceleration can react to starting/stopping, but may alter at most
        # half of the constant-velocity lead. This keeps one uneven detector
        # interval from creating a wild fling.
        adx = 0.5 * ax * horizon * horizon
        ady = 0.5 * ay * horizon * horizon
        accel_mag = math.hypot(adx, ady)
        accel_cap = 0.5 * max(1.0, math.hypot(ox, oy))
        if accel_mag > accel_cap:
            scale = accel_cap / accel_mag
            adx *= scale
            ady *= scale
        ox += adx
        oy += ady

        max_offset = min(MOTION_MAX_OFFSET,
                         float(max(1, self.radius_px())))
        lead_mag = math.hypot(ox, oy)
        if lead_mag > max_offset:
            scale = max_offset / lead_mag
            ox *= scale
            oy *= scale

        horizontal = "right" if vx > MOTION_MIN_SPEED_PX_S else (
            "left" if vx < -MOTION_MIN_SPEED_PX_S else "")
        vertical = "falling" if vy > MOTION_MIN_SPEED_PX_S else (
            "jumping" if vy < -MOTION_MIN_SPEED_PX_S else "")
        direction = "+".join(part for part in (horizontal, vertical) if part)
        return ox, oy, direction

    def _apply_motion_lead(self, nx, ny, target=None, force=False):
        """Project a measured target through capture and output latency."""
        if not (force or self._motion_enabled()) \
                or not self._target_measured(target):
            return int(nx), int(ny)

        # The colour lock has the freshest translation history (2-3 frames).
        # Prefer its 20-30 ms lead over the slower detector-centre fit; keep
        # the two paths separate so a pose/colour handoff cannot double-lead.
        if getattr(target, "source", "") == "color":
            with self._state_lock:
                tracker = self._target_lock
            if tracker is not None:
                try:
                    lx, ly = tracker.lead(time.perf_counter())
                except Exception:
                    lx = ly = 0.0
                if abs(lx) + abs(ly) >= 0.75:
                    if time.perf_counter() - self._lead_log_at > 5.0:
                        self._lead_log_at = time.perf_counter()
                        log("Motion lead: colour %+.1f,%+.1f px"
                            % (lx, ly))
                    return (int(round(float(nx) + lx)),
                            int(round(float(ny) + ly)))
            return int(nx), int(ny)

        ox, oy, direction = self._motion_prediction()
        if abs(ox) + abs(oy) < 1.5:
            return int(nx), int(ny)

        tag = (", %s" % direction) if direction else ""
        if bool(self.cfg.get("motion_3d", False)):
            try:
                horizon_ms = 1000.0 * min(
                    MOTION_MAX_HORIZON_S,
                    max(MOTION_OUTPUT_DELAY_S,
                        time.perf_counter() - self._motion_observed_at
                        + MOTION_OUTPUT_DELAY_S))
                with self._tracker_lock:
                    pred = self._tracker.predict(
                        horizon_ms, MOTION_MAX_OFFSET,
                        min_speed=MOTION_MIN_SPEED_PX_S)
                if len(pred) > 2:
                    ratio = float(pred[2])
                    if ratio >= 1.02:
                        tag += ", approaching"
                    elif ratio <= 0.98:
                        tag += ", moving away"
            except (AttributeError, TypeError, ValueError, OverflowError):
                pass

        nx = int(round(float(nx) + ox))
        ny = int(round(float(ny) + oy))
        if time.perf_counter() - self._lead_log_at > 5.0:
            self._lead_log_at = time.perf_counter()
            log("Motion lead: %+.1f,%+.1f px%s" % (ox, oy, tag))
        return nx, ny

    def _apply_snap_offset(self, nx, ny):
        """Legacy compatibility hook; retired snap offsets are always zero."""
        return int(nx), int(ny)

    def _auto_fire(self, x, y):
        """Move onto (x, y) and click once. No settle wait (retired):
        the point already carries the snap offset.
        """
        if not self.glide_cursor(x, y):

            log("Auto-click cancelled: you moved the mouse yourself.")
            return False

        self.send_click(x, y, double=False)

        log("Auto-click: clicked (%d, %d)." % (int(x), int(y)))
        return True

    def run_auto_click_worker(self):
        """The auto-click loop. Own thread, daemon, never blocks a click."""

        def _gap():
            return max(0.05, self.AUTO_CLICK_INTERVAL)

        idle = 0.05

        while not self.stop.is_set():
            try:
                if not self._auto_can_arm():

                    time.sleep(idle)
                    continue

                if not self.running \
                        or time.perf_counter() < self.pause_until:
                    time.sleep(idle)
                    continue

                if self._bypass_enabled() and self._bypass_held():
                    time.sleep(idle)
                    continue

                if self.stop.wait(max(0.0, self._auto_next - time.perf_counter())):
                    break

                if self._fg_is_own():
                    time.sleep(idle)
                    continue

                target = self._auto_pick()
                if target is None:
                    time.sleep(idle)
                    continue

                x, y = self._apply_snap_offset(*target)

                with self._auto_lock:
                    announced = self._auto_armed
                    self._auto_armed = True
                if not announced:

                    if bool(self.cfg.get("subject_priority", False)):
                        what = "figure targets"
                    else:
                        acc = _parse_accents(
                            self.cfg.get("accent_color", "off"))
                        what = ("colour %s" % self.cfg.get("accent_color")
                                if acc else "high-contrast items")
                    log("Auto-click is ON: scanning for %s every %.3gs."
                        % (what, _gap()))

                self._auto_fire(x, y)
                self._auto_next = time.perf_counter() + _gap()
            except Exception as exc:
                log("auto-click worker error: %r" % (exc,))
                time.sleep(idle)

    def _rebind_hotkeys(self):
        """Rebuild the hotkey tables from config (hook thread only)."""
        for hid, _m, _v in self._hk_always:
            try:
                UnregisterHotKey(None, hid)
            except Exception:
                pass

        always, local = build_hotkey_table(self.cfg)
        self._hk_always = always
        self.local_binds = local
        self._local_map = {vk: hid for hid, vk in local}
        track_spec = str(self.cfg.get("key_track_target", "") or "").strip()
        track_parsed = hotkeys._parse_hotkey(track_spec) if track_spec else None
        if track_parsed is not None:
            self._track_target_mods, self._track_target_vk = track_parsed
        else:
            self._track_target_mods = 0
            self._track_target_vk = None

        self._local_combos = {(0, vk) for _hid, vk in local}

        self._macros, self._macro_map = build_macro_table(self.cfg)
        self._macro_trigger_vks = {vk for _held, vk in self._macro_map}
        self._rec_combos = set()

        _CONTROL_IDS = tuple(hid for hid, name in ACTION_IDS.items()
                             if name in _CONTROL_NAMES)
        for hid, mods, vk in always:
            if hid in _CONTROL_IDS:
                self._rec_combos.add((mods & 0x000F, vk))
        for hid, vk in local:
            if hid in _CONTROL_IDS:
                self._rec_combos.add((0, vk))
        for m in self._macros:
            if not m[1]:
                continue
            k = (m[0] & 0x000F, m[1][0])
            if k in self._local_combos or k in self._rec_combos or any(
                    (mm & 0x000F, vv) == k for _h, mm, vv in always):
                log("Macro trigger shares a key with an action or "
                    "recorder hotkey; while launched, the macro wins "
                    "that key.")
        for hid, mods, vk in always:
            if not RegisterHotKey(None, hid, mods, vk):
                log("Hotkey id %d could not be registered." % hid)

        try:
            bound, macros = self._pad_rebind()
        except Exception as exc:

            log("Controller: the binding tables could not be built (%r); "
                "controller actions stay as they were." % (exc,))
        else:
            if self._pad_connected:
                log("Controller: %d action binding(s) and %d macro(s) "
                    "reloaded." % (bound, macros))

    def _clear_trigger_state(self):
        """Cancel a pending Mode-2 hold timer and forget its press.

        Called whenever assistance is torn down so a suppressed DOWN
        can never leave a timer behind that later reinjects a click.  The
        Mode-1 UP ledger deliberately survives: a physical DOWN that reached
        the game still needs its matching UP passed through, while an eaten
        DOWN still needs its matching UP eaten, even across pause/mode switch.
        """
        with self._trigger_lock:
            timer = self._trigger_hold_timer
            self._trigger_hold_timer = None
            self._trigger_suppressed_down = None
            self._trigger_hold_done = False
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _ensure_running(self):
        """Make assistance active without touching the hook threads."""
        if self.running:
            return False
        self.running = True
        self.scanner_dead = False
        self.fail_count = 0
        self._auto_reset_session()
        return True

    def _assist_off(self, why="ASSIST OFF"):
        """Stop assisting only - keep the program and its hotkeys alive."""
        if not self.running:
            return False
        self.running = False
        self._drain_queue()
        self._emergency_release()
        self._pad_release_all()
        self._clear_trigger_state()
        self._stop_track_target()
        self._debug_vis = None
        with self._state_lock:
            self.follow_lock = None
            self._center_follow = None
            self._center_hold = None
            self._center_hold_tomb = 0.0
            self._pick_at = None
            self._chase_ref = None
        self._reset_target_lock()
        self._reset_motion_history()
        log("%s - assistance is off; your input is untouched. "
            "Press launch or a mode key to assist again." % why)
        return True

    def _force_mode(self, mode):
        """Switch modes in real time with a full teardown of the old.

        Force-kill semantics: queued jobs are returned, suppressed
        presses reclaimed exactly once, hold sessions/follow locks die
        (synthetic buttons released first - never a stuck trigger),
        motion history is dropped (no stale lead flings the new mode's
        first snap), looping playback stops, the stall backoff clears,
        and the epoch bumps so in-flight workers abandon themselves
        instead of delivering stale-mode clicks. Threads can't be
        killed in Python - generation-skipping invalidates them, which
        is the same guarantee from the click's point of view: nothing
        from the old mode acts after this returns (modulo a worker
        already inside its delivery call, bounded by the exactly-once
        latch). Applies mid-hold too: a held button's press belonged
        to the dead mode, and the new mode starts clean on next press.
        """
        new_mode = 1 if int(mode) == 1 else 2
        # Invalidate in-flight work before any teardown operation can block.
        # Otherwise a worker can finish and click during the drain window,
        # while the old epoch still appears live.
        with self._state_lock:
            self._mode_epoch += 1
            self._mode = new_mode

        self._drain_queue()
        self._emergency_release()
        self._pad_release_all()
        self._clear_trigger_state()
        self._stop_track_target()

        self.stop_playback()
        with self._state_lock:
            self.follow_lock = None
            self._center_follow = None
            self._center_hold_tomb = 0.0
            self._pick_at = None
            self._chase_ref = None
            try:
                self.subject.clear()
            except Exception:
                pass
        self._reset_target_lock()
        self._reset_motion_history()
        self.pause_until = 0.0

    def toggle_running(self):
        if self.running:
            self.handle_hotkey(11)
        else:
            self.handle_hotkey(10)

    def handle_hotkey(self, hid):
        try:
            hid = int(hid)
        except (TypeError, ValueError):
            return
        source = "key"
        if hid >= PAD_HID_OFFSET:
            source = "pad"
            hid -= PAD_HID_OFFSET
        action = ACTION_IDS.get(hid)
        if action is None:
            return

        if action == "launch":
            if self._ensure_running():
                log(
                    "LAUNCHED - assist active, mode 2 (guide). %s pauses."
                    % self.cfg.get("key_pause", "ctrl+p")
                )

        elif action == "pause":
            if self.running:

                self.running = False
                self._drain_queue()
                self._emergency_release()
                self._pad_release_all()
                self._clear_trigger_state()
                self._stop_track_target()
                self._debug_vis = None
                with self._state_lock:
                    self.follow_lock = None
                    self._center_follow = None
                    self._center_hold = None
                    self._center_hold_tomb = 0.0
                    self._pick_at = None
                    self._chase_ref = None

                self._reset_target_lock()
                self._reset_motion_history()
                self.stop_playback()
                log("PAUSED - your mouse and keyboard are entirely yours.")

        elif action == "track_target":
            # The physical Track target key is handled directly by the key
            # hook; this arm is reached when the controller binding posts the
            # same action id to the hook thread. Release is dispatched by the
            # pad poll loop.
            self._start_track_target()

        elif action == "assist_off":
            self._assist_off()

        elif action == "reload":
            with self._state_lock:
                self._mode_epoch += 1
            self._drain_queue()
            self._emergency_release()
            self._pad_release_all()
            self._stop_track_target()
            self._clear_trigger_state()
            with self._state_lock:
                self.follow_lock = None
                self._center_follow = None
                self._center_hold_tomb = 0.0
                self._pick_at = None
                self._chase_ref = None
            self._reset_target_lock()
            self._reset_motion_history()
            self.cfg.reload()
            self._rebind_hotkeys()

            notes = []
            backend = str(self.cfg.get("capture_backend", "auto"))
            if backend != self._capture.prefer:
                notes.append("capture_backend change needs a restart")

            yolo_requested = bool(self.cfg.get("game_mode", False))
            if self._yolo26n is not None and not yolo_requested:
                yolo = self._yolo26n
                self._yolo26n = None
                try:
                    yolo.stop()
                    notes.append("YOLO26n stopped")
                except Exception as exc:
                    log("YOLO26n shutdown error: %r" % (exc,))
            elif self._yolo26n is None and yolo_requested:
                notes.append("enabling YOLO26n needs a restart")

            suffix = " (%s)" % "; ".join(notes) if notes else ""
            log("CONFIG RELOADED from disk%s." % suffix)

        elif action == "mode1":
            self._ensure_running()
            self._force_mode(1)
            log(
                "MODE 1 classic: mis-click suppressed, snap, auto-click. "
                "Shift bypasses."
            )

        elif action == "mode2":
            self._ensure_running()
            self._force_mode(2)
            log(
                "MODE 2 guide: your clicks always pass through; "
                "cursor moves onto items after release."
            )

        elif action == "record_macro":
            self._start_recording(source=source)

        elif action == "stop_macro":
            self._stop_recording()

        elif action == "play_macro":
            if source == "pad":
                self._play_recording(self.play_target_pad_rid, source="pad")
            else:
                self._play_recording(self.play_target_rid)

        elif action.startswith("play_macro") and action[-1].isdigit():

            digits = action[len("play_macro"):]
            self._play_slot(int(digits) if digits else 0, source=source)

    def request_quit(self):

        self.running = False
        self._drain_queue()
        self._emergency_release()
        self._pad_release_all()
        self._clear_trigger_state()
        self._stop_track_target()
        self._debug_vis = None
        if self._pad_out is not None:
            try:
                self._pad_out.close()
            except Exception:
                pass
            self._pad_out = None
        with self._state_lock:
            self.follow_lock = None
            self._center_follow = None
            self._pick_at = None
            self._chase_ref = None
        self._reset_target_lock()
        self._reset_motion_history()
        self._play_cancel.set()
        while True:
            try:
                self.macro_q.get_nowait()
            except queue.Empty:
                break
        closer = getattr(self._input, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
        capture_closer = getattr(self._capture, "close", None)
        if callable(capture_closer):
            try:
                capture_closer()
            except Exception:
                pass
        yolo = self._yolo26n
        self._yolo26n = None
        if yolo is not None:
            try:
                yolo.stop()
            except Exception as exc:
                log("YOLO26n shutdown error: %r" % (exc,))
        self.stop.set()

        if self.thread_id:
            try:
                PostThreadMessage(self.thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass

    def run_hook_thread(self):
        """Hook-thread entry. A raise here used to leave the app "ready"
        with no hooks installed and start_app none the wiser: any failure
        marks the app stopped and ready so startup reports it and exits."""
        try:
            self._run_hook_thread_inner()
        except Exception as exc:
            log("FATAL: the hook thread died: %r" % (exc,))
            self.stop.set()
            self.ready.set()

    def _run_hook_thread_inner(self):
        self.thread_id = kernel32.GetCurrentThreadId()

        try:
            warm = np.full((64, 64, 3), 128, dtype=np.uint8)
            scan_region(warm, 32, 32, self.cfg.values, 50)

            x, y = get_cursor_pos()
            self.grab_around(x, y, 40)
        except Exception as exc:
            log("warmup note: %r" % (exc,))

        self.proc_ref = self.make_hook_proc()

        self.hook = user32.SetWindowsHookExW(
            WH_MOUSE_LL,
            self.proc_ref,
            kernel32.GetModuleHandleW(None),
            0,
        )

        if not self.hook:
            log("FATAL: could not install the mouse hook.")
            log("Another program may be blocking it. Try restarting Windows.")
            self.stop.set()
            self.ready.set()
            return

        self.key_proc_ref = self.make_key_hook_proc()
        self.key_hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL,
            self.key_proc_ref,
            kernel32.GetModuleHandleW(None),
            0,
        )
        if not self.key_hook:
            log("WARNING: keyboard hook unavailable; single-key binds "
                "fall back to registered hotkeys only.")

        self._rebind_hotkeys()

        self.ready.set()

        msg = MSG()

        while not self.stop.is_set():
            ret = GetMessage(ctypes.byref(msg), None, 0, 0)

            if ret == 0 or ret == -1:
                break

            if msg.message == WM_HOTKEY:
                try:
                    hid = int(msg.wParam)
                    self.handle_hotkey(hid)
                except Exception as exc:
                    log("hotkey error: %r" % (exc,))

        for hid, _m, _v in self._hk_always:
            try:
                UnregisterHotKey(None, hid)
            except Exception:
                pass

        if self.hook:
            try:
                user32.UnhookWindowsHookEx(self.hook)
            except Exception:
                pass

            self.hook = None

        if self.key_hook:
            try:
                user32.UnhookWindowsHookEx(self.key_hook)
            except Exception:
                pass

            self.key_hook = None

    PAD_SPEED = 900.0
    PAD_MISS_LIMIT = 3

    def _pad_enabled(self):
        """Controller input is always available when XInput can read a pad.

        The Controller tab's visible bindings are the switch: an empty table
        still polls for recordings/playback, but no action is claimed.
        """
        return bool(gamepad.available())

    def _pad_settings(self):
        """Fixed controller tuning used by every installation."""
        return {
            "interval": 1.0 / PAD_POLL_HZ,
            "threshold": int(PAD_TRIGGER_THRESHOLD),
            "deadzone": int(gamepad.AXIS_FULL * PAD_DEADZONE_PERCENT / 100.0),
            "speed": self.PAD_SPEED * PAD_SENSITIVITY,
            "drive": PAD_DRIVE_ENABLED,
            "rumble": PAD_RUMBLE_ENABLED,
            "index": PAD_INDEX,
            "click": self._pad_token("pad_click_button", "rt"),
            "right": self._pad_token("pad_right_button", "lt"),
        }

    def _pad_token(self, key, fallback):
        """A single button name, falling back instead of disabling the drive.

        config._sanitize already refuses a bad value; this is the second line
        of defence, because "no click button" would leave a controller user
        able to steer but unable to click, with nothing on screen to say why.
        """
        index = gamepad.parse_token(self.cfg.get(key, fallback))
        if index is None:
            index = gamepad.parse_token(fallback)
        return index

    def _neutralize_aim_stick(self, generation=None):
        """Centre a virtual aim stick unless a newer pulse superseded it."""
        with self._aim_stick_lock:
            if generation is not None \
                    and generation != self._aim_stick_generation:
                return False
            timer = self._aim_stick_timer
            self._aim_stick_timer = None
            backend = getattr(self, "_aim_stick_backend", None)
            if generation is None:
                self._aim_stick_generation += 1
            if backend is None or getattr(backend, "name", "") != "vigem":
                sent = False
            else:
                try:
                    ok_x = bool(backend.axis(4, 0))
                    ok_y = bool(backend.axis(5, 0))
                    sent = bool(ok_x and ok_y and backend.flush())
                except Exception:
                    sent = False
        if timer is not None and timer is not threading.current_thread():
            try:
                timer.cancel()
            except Exception:
                pass
        return sent

    def _right_stick_backend(self):
        """ViGEm backend for aim steering, independent of macro output.

        ``pad_output`` controls recordings and ``pad:`` macros. It must not
        silently override the separate visible ``unibot_aim`` switch. Reuse a
        live ViGEm macro backend when one exists; otherwise ask input.py for
        its dedicated cached steering pad.
        """
        backend = getattr(self, "_pad_out", None)
        if backend is not None and getattr(backend, "name", "") == "vigem" \
                and getattr(backend, "ok_for_pad", lambda: False)():
            return backend
        try:
            backend = gamepad._get_vigem_steer(None)
        except Exception:
            backend = False
        if backend is False or backend is None \
                or not getattr(backend, "ok_for_pad", lambda: False)():
            return None
        return backend

    def _right_stick_aim(self, target_dx, target_dy, max_radius=None,
                         hold_gain=False):
        """Move the virtual right stick to aim at a target offset.

        Args:
            target_dx: Horizontal offset from screen center to target (pixels)
            target_dy: Vertical offset from screen center to target (pixels)
            max_radius: Maximum radius to consider (optional, for sensitivity scaling)

        Returns True if the right stick was moved, False if ViGEm not available.
        """
        backend = self._right_stick_backend()
        if backend is None:
            return False

        # Screen center is the reticle position in flight mode
        # Map pixel offset to right stick range (-32768 to 32767)
        # Typical sensitivity: 1 pixel ~= 100 stick units.
        sensitivity = AIM_STICK_UNITS_PER_PIXEL
        if hold_gain:
            scaled_dx, scaled_dy = self._scaled_view_delta(
                target_dx, target_dy)
        else:
            scaled_dx, scaled_dy = int(target_dx), int(target_dy)
        self._steer_local.delta = (int(scaled_dx), int(scaled_dy))

        # Calculate stick values
        rx = int(round(scaled_dx * sensitivity))
        ry = int(round(-scaled_dy * sensitivity))  # Invert Y (screen coords)

        # Clamp to valid range
        rx = max(-32768, min(32767, rx))
        ry = max(-32768, min(32767, ry))

        # Optional: limit max deflection if max_radius specified
        if max_radius is not None:
            mag = math.hypot(scaled_dx, scaled_dy)
            if mag > max_radius:
                scale = max_radius / mag
                rx = int(round(rx * scale))
                ry = int(round(ry * scale))

        try:
            with self._aim_stick_lock:
                self._aim_stick_generation += 1
                generation = self._aim_stick_generation
                old_timer = self._aim_stick_timer
                self._aim_stick_timer = None
                self._aim_stick_backend = backend
                ok_x = bool(backend.axis(4, rx))
                ok_y = bool(backend.axis(5, ry))
                sent = bool(ok_x and ok_y and backend.flush())
                if sent and (rx or ry):
                    timer = threading.Timer(
                        0.020, self._neutralize_aim_stick,
                        args=(generation,))
                    timer.daemon = True
                    self._aim_stick_timer = timer
                else:
                    timer = None
            if old_timer is not None:
                try:
                    old_timer.cancel()
                except Exception:
                    pass
            if timer is not None:
                timer.start()
            return sent
        except Exception as exc:
            log("Right stick aim error: %r" % (exc,))
            self._neutralize_aim_stick()
            return False

    def _pad_output(self):
        """The controller OUTPUT door, built on first use and kept.

        Output is independent of the input switch: a controller recording can
        be played back on a machine where live controller input is off, and
        it still has to leave the program somehow.
        """
        if self._pad_out is not None:
            return self._pad_out
        try:
            self._pad_out = make_pad_backend(self.cfg)
        except Exception as exc:
            log("Controller: no output backend could be built (%r); "
                "controller macros and playback are silent." % (exc,))
            self._pad_out = None
        return self._pad_out

    def _pad_rebind(self):
        """Rebuild the controller binding and macro tables from config."""
        self._pad_bindings = gamepad.build_action_bindings(self.cfg)
        self._pad_macros = []
        for slot, spec, indices, action in gamepad.build_pad_macro_table(self.cfg):
            binding = gamepad.PadBinding("pad_macro%d_key" % slot, spec,
                                         action)
            binding.indices = indices
            binding.tracker = gamepad.PadTracker(indices)
            self._pad_macros.append(binding)
        spec = gamepad.parse_spec(self.cfg.get("bypass_pad", ""))
        self._pad_bypass_tracker = (gamepad.PadTracker(spec)
                                    if spec else None)
        if self._pad_bypass_tracker is None:
            self._pad_bypass = False
        macro_slots = {}
        for binding in self._pad_macros:
            macro_slots[binding.indices] = binding.key
        kept = []
        for binding in self._pad_bindings:
            if binding.indices in macro_slots:
                log("Controller: %s is bound to %s, which %s also uses; "
                    "while launched, the macro wins that button."
                    % (binding.key, binding.spec, macro_slots[binding.indices]))
                continue
            kept.append(binding)
        self._pad_bindings = kept
        self._pad_warn_drive_conflicts()
        return len(self._pad_bindings), len(self._pad_macros)

    def _pad_warn_drive_conflicts(self):
        """Say so when a binding sits on the button the drive clicks with.

        Both still happen - the binding fires and the trigger still presses -
        which is what the user asked for by binding it, but it reads like a
        double click unless the log says why. Told once per rebind rather than
        silently re-routed: taking the click away from a trigger the user is
        aiming with would be the worse surprise.
        """
        if not PAD_DRIVE_ENABLED:
            return
        click = self._pad_token("pad_click_button", "rt")
        right = self._pad_token("pad_right_button", "lt")
        for binding in self._pad_bindings + self._pad_macros:
            if not binding.indices or len(binding.indices) != 1:
                continue
            index = next(iter(binding.indices))
            for role_index, role_name in (
                    (click, "the controller's press"),
                    (right, "the controller's right click")):
                if role_index is not None and index == role_index:
                    log("Controller: %s is bound to %s, which is also %s - "
                        "both will happen on that button. Rebind one of them, "
                        "or change pad_click_button / pad_right_button in "
                        "config.ini."
                        % (binding.key, binding.spec, role_name))

    def _pad_resolve_index(self, settings):
        """Which pad to read: the pinned one, or the first that answers."""
        index = gamepad.first_connected(settings["index"])
        if index is None:
            return None
        if self._pad_index != index:
            self._pad_index = index
        return index

    def run_pad_worker(self):
        """Poll the controller and turn it into the same events the mouse and
        keyboard hooks produce.

        One thread, one cadence, and every path ends in code the mouse and
        keyboard already use: an action binding calls handle_hotkey with the
        SAME id a keyboard binding would, except record/play actions, which
        carry PAD_HID_OFFSET so they land in the controller recording
        library instead of the keyboard one. A macro goes through the same
        macro queue, a recording lands in the same buffer, and the drive
        queues the same classic/guide jobs. That is what makes the
        assistance identical for the two devices rather than a parallel
        implementation.

        XInput has no events, so this is a poll. Nothing here may block: the
        frame budget is the poll interval, and output is queued, never sent
        inline, except for the cursor itself.
        """
        while not self.stop.is_set():
            try:
                if not self._pad_enabled():
                    if self._pad_connected or self._pad_out is not None:
                        self._pad_teardown("controller input is off")
                    if self.stop.wait(0.5):
                        return
                    continue
                settings = self._pad_settings()
                index = self._pad_resolve_index(settings)
                if index is None:
                    if self._pad_connected:
                        self._pad_teardown("the controller was disconnected")
                    if self.stop.wait(0.5):
                        return
                    continue
                state = gamepad.read(index, settings["threshold"])
                if not state.connected:

                    self._pad_misses += 1
                    if self._pad_connected:
                        if self._pad_misses >= self.PAD_MISS_LIMIT:
                            self._pad_teardown(
                                "the controller stopped answering")
                        if self.stop.wait(0.02):
                            return
                        continue
                    if self.stop.wait(0.5):
                        return
                    continue
                self._pad_misses = 0
                now = time.perf_counter()
                if not self._pad_connected:
                    self._pad_connected = True
                    caps = gamepad.capabilities(index)
                    log("Controller: pad %d connected%s - %d action "
                        "binding(s), %d macro(s), drive %s."
                        % (index,
                           (" (type %d, subtype %d)" % (caps[0], caps[1]))
                           if caps else "",
                           len(self._pad_bindings), len(self._pad_macros),
                           "on" if settings["drive"] else "off"))
                self._pad_poll(state, index, now, settings)
                elapsed = time.perf_counter() - now
                if self.stop.wait(max(0.001, settings["interval"] - elapsed)):
                    return
            except Exception as exc:
                log("controller worker error: %r" % (exc,))
                if self.stop.wait(0.25):
                    return

    def _pad_teardown(self, why):
        """The pad went away or was switched off: let go of everything it was
        holding, so nothing can stay stuck down. The output backend is
        closed and dropped too - leaving it built made the pad worker's
        `connected or output` guard tear down again every 0.5 s forever."""
        self._pad_connected = False
        self._pad_index = None
        self._pad_cursor = None
        self._pad_target = None
        self._pad_bypass = False
        self._pad_emu_warned = False
        self._pad_release_all()
        backend = self._pad_out
        self._pad_out = None
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
        log("Controller: %s - controller input is idle." % (why,))

    def _pad_poll(self, state, index, now, settings):
        buttons = int(state.buttons)
        self._pad_bypass = bool(self._pad_bypass_tracker
                                and self._pad_bypass_tracker.matches(buttons))
        if self._rec_buffer is not None and self.running:
            self._record_pad_edges(state, buttons, now)
        else:
            self._pad_prev_buttons = buttons
            self._pad_sync_axes(state)

        armed = self._armed_now()
        if armed:
            self._pad_dispatch(state, buttons, now)
        else:
            for binding in self._pad_bindings:
                binding.tracker.update(buttons, now)
            for binding in self._pad_macros:
                binding.tracker.update(buttons, now)

        if settings["drive"]:
            self._pad_drive(state, now, settings)
        elif self._pad_click_down or self._pad_right_down:
            self._pad_release_all()

    def _pad_dispatch(self, state, buttons, now):
        """Fire action bindings and macros, longest combination first.

        A combination swallows its own subsets for this sample, so with
        A=launch and RB+A=record, pressing RB+A records and does not also
        launch. The shadowed tracker is still advanced - only its event is
        dropped - so releasing RB while A stays held cannot fire launch late.
        """
        every = self._pad_bindings + self._pad_macros
        shadow = gamepad.shadowed_indices(every, buttons)
        for binding in self._pad_bindings:
            event = binding.tracker.update(buttons, now)
            if event not in ("press", "release") or id(binding) in shadow:
                continue
            if binding.action == "track_target":
                if event == "press":
                    self._start_track_target()
                else:
                    self._stop_track_target()
                continue
            if event == "press":
                self._pad_action(binding.action)
        for binding in self._pad_macros:
            event = binding.tracker.update(buttons, now)
            if event != "press" or id(binding) in shadow:
                continue
            self._pad_fire_macro(binding.action)

    def _pad_action(self, hid):
        """Route a controller action to the hook thread.

        RegisterHotKey/UnregisterHotKey belong to the thread that owns the
        message loop, and handle_hotkey(6) re-registers them - so an action
        must not run on the controller thread. The GUI posts WM_HOTKEY to the
        hook thread for exactly this reason; the controller does the same, and
        a controller binding ends up in the very same handle_hotkey call a
        keyboard binding reaches. With no hook thread (unit tests, a failed
        hook install) the action runs here instead of being dropped.

        Record/play actions carry PAD_HID_OFFSET so the hook thread routes
        them to the controller recording library, not the keyboard one.
        """
        try:
            hid = int(hid)
        except (TypeError, ValueError):
            return False
        try:
            if ACTION_IDS.get(hid) in _PAD_REC_ACTIONS:
                hid += PAD_HID_OFFSET
        except Exception:
            pass
        tid = self.thread_id
        if tid:
            try:
                PostThreadMessage(tid, WM_HOTKEY, int(hid), 0)
                return True
            except Exception as exc:
                log("controller action could not be posted (%r); dropped "
                    "rather than run on the wrong thread." % (exc,))
                return False
        self.handle_hotkey(hid)
        return True

    def _pad_fire_macro(self, action):
        """Queue a pad macro. Output goes through the macro worker, never
        inline: the poll loop must not block on an injection."""
        if not self.running:
            return
        kind = action[0]
        if kind == "key":
            self.macro_q.put(("macro", action[1]))
            return
        backend = self._pad_output()
        if backend is None:
            return
        if backend.name != "vigem" and not self._pad_emu_warned:
            self._pad_emu_warned = True
            log("Controller: 'pad:' macro actions need the ViGEmBus virtual "
                "pad. Without it they are delivered through the pad_emulate "
                "keyboard/mouse table instead.")
        self.macro_q.put(("padmask", action[1]))

    def _pad_emit_button(self, index, down):
        backend = self._pad_output()
        if backend is None:
            return False
        try:
            sent = bool(backend.button(int(index), bool(down)))
        except Exception as exc:
            backend.note_failure("button(%r)" % (exc,))
            return False
        if sent:
            try:
                backend.flush()
            except Exception:
                pass
        return sent

    def _pad_emit_axis(self, index, value):
        backend = self._pad_output()
        if backend is None:
            return False
        try:
            sent = bool(backend.axis(int(index), int(value)))
        except Exception as exc:
            backend.note_failure("axis(%r)" % (exc,))
            return False
        if sent:
            try:
                backend.flush()
            except Exception:
                pass
        return sent

    def _pad_emit_mask(self, mask):
        """Press every button in a mask, hold briefly, release. One frame of
        a virtual pad is not enough for most games to notice a press."""
        backend = self._pad_output()
        if backend is None:
            return False
        indices = [i for i in range(len(gamepad.PAD_BUTTONS))
                   if mask & (1 << i)]
        if not indices:
            return False

        def _emit():
            try:
                for index in indices:
                    backend.button(index, True)
                backend.flush()
                time.sleep(0.045)
                for index in indices:
                    backend.button(index, False)
                backend.flush()
            except Exception as exc:
                backend.note_failure("padmask(%r)" % (exc,))

        threading.Thread(target=_emit, daemon=True, name="pad-mask").start()
        return True

    def _pad_release_all(self):
        """Let go of everything the controller path could be holding.

        The flags are snapshot and cleared under _pad_state_lock; the
        releases themselves run OUTSIDE it - _pad_release takes that same
        lock, and it is not reentrant."""
        self._neutralize_aim_stick()
        with self._pad_state_lock:
            click_down = self._pad_click_down
            self._pad_click_down = False
            right_down = self._pad_right_down
            self._pad_right_down = False
        if click_down:
            try:
                x, y = get_cursor_pos()
                self._pad_release(int(x), int(y), time.perf_counter(), queue_guide=False)
            except Exception as exc:
                log("controller release error: %r" % (exc,))
        if right_down:
            try:
                inject_mouse(BTN_RIGHT, False)
            except Exception:
                pass
        backend = self._pad_out
        if backend is not None:
            try:
                backend.release_all()
                backend.flush()
            except Exception:
                pass

    def _pad_drive(self, state, now, settings):
        """The controller as a pointer: left stick steers the cursor, the
        configured trigger is the press, so a controller-only user gets the
        same snap, guide, follow and auto-click assistance.

        The cursor is integrated here rather than read from the stick each
        frame, and re-synced to the real cursor whenever the stick is idle -
        so a user with both a mouse and a pad never has the two fight.
        """
        vx, vy, vw, vh = self._virtual_screen()
        previous = self._pad_last_drive
        self._pad_last_drive = now
        dt = 0.0 if previous <= 0 else max(0.0, min(0.1, now - previous))
        fx, fy = gamepad.apply_deadzone(state.lx, state.ly,
                                        settings["deadzone"])
        if fx or fy:
            if self._pad_cursor is None:
                self._pad_cursor = get_cursor_pos()
            elif self._pad_target is not None:
                real = get_cursor_pos()
                if (abs(real[0] - self._pad_target[0]) > 3
                        or abs(real[1] - self._pad_target[1]) > 3):
                    self._pad_cursor = real
            cx = self._pad_cursor[0] + fx * settings["speed"] * dt
            cy = self._pad_cursor[1] - fy * settings["speed"] * dt
            cx = min(vx + vw - 1, max(vx, cx))
            cy = min(vy + vh - 1, max(vy, cy))
            self._pad_cursor = (cx, cy)
            target = (int(round(cx)), int(round(cy)))
            self._pad_target = target
            try:
                self._input.move(target[0], target[1])
            except Exception as exc:
                log("controller cursor error: %r" % (exc,))
        else:
            self._pad_cursor = None
            self._pad_target = None

        rx, ry = gamepad.apply_deadzone(state.rx, state.ry,
                                        settings["deadzone"])
        if (rx or ry) and self._pad_click_down:
            if not self._nudge(int(round(rx * 24.0)), int(round(-ry * 24.0)),
                               view=True):
                # Nudge refused: fall back to absolute cursor move.
                try:
                    x, y = get_cursor_pos()
                    self._input.move(int(x + rx * 24.0), int(y - ry * 24.0))
                except Exception:
                    pass

        click_index = settings["click"]
        right_index = settings["right"]
        if click_index is None:
            click_index = gamepad.parse_token("rt")
        if right_index is None:
            right_index = gamepad.parse_token("lt")

        click_down = state.down(click_index) if click_index is not None else False
        with self._pad_state_lock:
            edge = click_down != self._pad_click_down
            if edge:
                self._pad_click_down = click_down
        if edge:
            if self._pad_target is not None:
                x, y = self._pad_target
            else:
                x, y = get_cursor_pos()
            if click_down:
                self._pad_press(int(x), int(y), now)
            else:
                self._pad_release(int(x), int(y), now)

        if right_index is not None and right_index != click_index:
            right_down = state.down(right_index)
            with self._pad_state_lock:
                edge = right_down != self._pad_right_down
                if edge:
                    self._pad_right_down = right_down
            if edge:
                try:
                    inject_mouse(BTN_RIGHT, right_down)
                except Exception:
                    pass

    def _pad_press(self, x, y, now, armed=None):
        """A controller press, routed through the same primitives the mouse
        hook uses: mode 1 queues a classic job and the worker delivers the
        click, mode 2 delivers the press and queues the guide job on release.
        Nothing here is a copy of the hook - it calls the hook's own methods.
        """
        if armed is None:
            armed = self._armed_now()
        if not armed:
            return
        own = self._fg_is_own()
        mode = self.current_mode()
        if not own:
            if mode == 1:
                self._clear_mode1_follow_state()
            else:
                self._arm_hold_follow(x, y, now, armed)
                self._arm_center_hold(x, y, mode, armed)
        if mode == 1 and not own:
            rapid = bool(self.cfg.get("rapid_click_snap", False))
            block_unconfirmed = bool(
                self._center_box() is not None)
            with self._pending_lock:
                un_upped = any(
                    (not e.get("up")) and (not e.get("worker_done"))
                    for e in self.pending_classics
                )
                if not un_upped and (rapid or (self.click_q.qsize() == 0
                                               and not self.worker_active)):
                    entry = {"x": x, "y": y, "up": False, "state": None,
                             "pad": True, "mode": 1,
                             "block_unconfirmed": block_unconfirmed}
                    self.pending_classics.append(entry)
                else:
                    entry = None
            if entry is not None:
                with self._pad_state_lock:
                    self._pad_entry = entry
                self.click_q.put(("classic", x, y, now, entry))
                return
            self._pass_note("previous click still assisted (rapid off)")
            if block_unconfirmed:
                # In-game Mode 1 blocks the virtual trigger as well: no
                # unassisted shot may slip out while a click is centring.
                return
        with self._pad_state_lock:
            self._pad_raw_down = True
        self._button(True)

    def _pad_release(self, x, y, now, queue_guide=True):
        with self._pad_state_lock:
            entry = self._pad_entry
            self._pad_entry = None
            raw = self._pad_raw_down
            if raw:
                self._pad_raw_down = False
        if entry is not None:
            with self._pending_lock:
                entry["up"] = True
            return
        if raw:
            self._button(False)
        armed = self._armed_now()
        mode = self.current_mode()
        was_center = self._release_holds(now)
        if queue_guide and mode == 2 and armed and not was_center and not self._fg_is_own():
            self.click_q.put(("guide", x, y, now, None))

    def _pad_sync_axes(self, state):
        for index, name in enumerate(gamepad.PAD_AXES):
            self._pad_prev_axes[index] = self._pad_quantise(name,
                                                            getattr(state, name))

    @staticmethod
    def _pad_quantise(name, value):
        """Coarse quantisation for the recorder: a stick sweep costs ~128
        steps instead of one per poll, which is what keeps a two-minute
        recording inside MAX_STEPS."""
        try:
            value = int(value)
        except (TypeError, ValueError):
            return 0
        if name in ("lt", "rt"):
            return value // 16
        return value // 512

    def _record_pad_edges(self, state, buttons, now):
        """Capture every controller transition into the SAME buffer and the
        SAME timeline as the keyboard and mouse, so one recording can hold a
        mixed input and plays back in order."""
        previous = self._pad_prev_buttons
        self._pad_prev_buttons = buttons
        for index in range(len(gamepad.PAD_BUTTONS)):
            bit = 1 << index
            was = bool(previous & bit)
            is_down = bool(buttons & bit)
            if is_down and not was:
                self._record_pad_step(STEP_PAD_BUTTON, index, PHASE_DOWN)
            elif was and not is_down:
                self._record_pad_step(STEP_PAD_BUTTON, index, PHASE_UP)
        for index, name in enumerate(gamepad.PAD_AXES):
            value = int(getattr(state, name))
            quantised = self._pad_quantise(name, value)
            previous = self._pad_prev_axes.get(index)
            if previous is None:
                self._pad_prev_axes[index] = quantised
                continue
            if previous == quantised:
                continue
            self._pad_prev_axes[index] = quantised
            self._record_pad_step(STEP_PAD_AXIS, index,
                                  gamepad.normalise_axis(index, value))

    def _record_pad_step(self, tag, index, value):
        with self._rec_lock:
            return self._record_pad_step_inner(tag, index, value)

    def _record_pad_step_inner(self, tag, index, value):
        if self._rec_buffer is None or not self.running:
            return
        now = time.perf_counter()
        delay = 0 if not self._rec_last_t else \
            int(round((now - self._rec_last_t) * 1000.0))
        self._rec_last_t = now
        if len(self._rec_buffer) >= REC_MAX_STEPS:
            self._mark_rec_full()
            return
        self._rec_buffer.append(
            (tag, index, value, max(0, min(5000, delay))))

    def _pad_rumble(self, seconds=0.12, strong=0.6):
        """The one piece of feedback a controller user gets without looking
        at the screen: recording started, recording saved."""
        if not PAD_RUMBLE_ENABLED:
            return False
        if not self._pad_connected:
            return False
        index = self._pad_index if self._pad_index is not None else 0
        threading.Thread(target=gamepad.set_rumble, daemon=True,
                         name="pad-rumble",
                         args=(index, strong, strong * 0.5, seconds)).start()
        return True

    def pad_status(self):
        """One-paragraph status for the console `gamepad` command."""
        lines = [gamepad.describe(self._pad_index)]
        backend = self._pad_out
        lines.append("  output: %s" % (backend.name if backend else
                                       "not built yet"))
        lines.append("  enabled: %s | connected: %s | drive: %s"
                     % (self._pad_enabled(), self._pad_connected,
                        PAD_DRIVE_ENABLED))
        bindings = ", ".join("%s=%s" % (b.key, b.spec)
                             for b in self._pad_bindings) or "none"
        lines.append("  bindings: %s" % bindings)
        macros = ", ".join("%s->%s" % (b.spec, b.action)
                           for b in self._pad_macros) or "none"
        lines.append("  macros: %s" % macros)
        return "\n".join(lines)

def start_app(cfg, identity=None):
    """Create the App and start its threads. Returns (app, hook_thread)."""
    try:
        import mss
        if not (getattr(mss, "MSS", None) or getattr(mss, "mss", None)):
            raise ImportError("the mss package exposes no mss()/MSS() factory")
    except ImportError:
        print("Snappy AI needs mss on Windows.")
        print("Install with:")
        print("    python -m pip install numpy mss")
        print("Optional extras (faster capture, new capture engine):")
        print("    python -m pip install dxcam")
        print("    python -m pip install windows-capture   # capture_backend = wgc")
        return None, None

    setup_dpi_awareness()

    app = App(cfg, identity)

    try:
        import detection as hpose_bridge
        # YOLO26 already owns detector + pose. Preloading the legacy hpose
        # stack here consumed memory and CPU even though the active centre path
        # could never call it.
        if app._yolo26n is None \
                and hpose_bridge.mode_of(cfg.values) == "on":
            hpose_bridge.preload(cfg.values)
    except Exception:
        pass

    hook_thread = threading.Thread(
        target=app.run_hook_thread,
        name="hook",
        daemon=True,
    )
    hook_thread.start()

    threading.Thread(
        target=app.run_scanner,
        name="scanner",
        daemon=True,
    ).start()

    threading.Thread(
        target=app.run_follow_worker,
        name="follow",
        daemon=True,
    ).start()

    threading.Thread(
        target=app.run_macro_worker,
        name="macros",
        daemon=True,
    ).start()

    threading.Thread(
        target=app.run_presence_worker,
        name="presence",
        daemon=True,
    ).start()

    threading.Thread(
        target=app.run_pad_worker,
        name="controller",
        daemon=True,
    ).start()

    app.ready.wait(timeout=10)

    if app.stop.is_set() or not hook_thread.is_alive():
        print("")
        print("Snappy AI could not start. See messages above.")
        app.request_quit()
        return None, None

    return app, hook_thread
