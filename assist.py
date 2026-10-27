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

import input_utils as gamepad
import input_utils as hotkeys
import vision
import online
from system_core import IS_WINDOWS, log
from input_utils import (ACTION_IDS, CONTROL_ACTION_NAMES as _CONTROL_NAMES,
                     build_hotkey_table, build_macro_table,
                     effective_mode_key)
from input_backend import make_input_backend, make_pad_backend
from input_utils import (RecordingStore, PAD_REC_FILE,
                        MAX_STEPS as REC_MAX_STEPS,
                        MAX_RECORDINGS as REC_MAX, STEP_KEY, STEP_MOVE,
                        STEP_BUTTON, STEP_PAD_BUTTON, STEP_PAD_AXIS,
                        BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_X1, BTN_X2,
                        PHASE_DOWN, PHASE_UP)
from vision import (FollowMemory, FigureMemory, scan_region, accent_soft_of,
                    accent_matcher_of, accent_variants_of, _parse_accents,
                    _sig_matches_accents, _shape_tier)

if IS_WINDOWS:













    import ctypes
    import ctypes.wintypes as wt
    from win32rt import (
        CaptureBackend, GetMessage, HC_ACTION, HOOKPROC, KBDLLHOOKSTRUCT,
        LLKHF_INJECTED, LLMHF_INJECTED,
        LLMHF_LOWER_IL_INJECTED, MSG, MSLLHOOKSTRUCT, PostThreadMessage,
        RegisterHotKey, XBUTTON1,
        SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN, SM_XVIRTUALSCREEN,
        SM_YVIRTUALSCREEN, UnregisterHotKey, VK_CONTROL, VK_LWIN, VK_MENU,
        VK_RBUTTON, VK_SHIFT, WH_KEYBOARD_LL, WH_MOUSE_LL, WM_HOTKEY,
        WM_KEYDOWN, WM_KEYUP, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MBUTTONDOWN,
        WM_MBUTTONUP, WM_MOUSEMOVE, WM_QUIT, WM_RBUTTONDOWN, WM_RBUTTONUP,
        WM_XBUTTONDOWN, WM_XBUTTONUP, cursor_visible,
        get_cursor_pos,
        get_dpi_scale, inject_combo, inject_mouse, kernel32, move_cursor,
        setup_dpi_awareness, user32,
    )




else:
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
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



MOTION_MAX_OFFSET = 80.0




REC_MOVE_MERGE_S = 0.05






MOUSE_BUTTON_INDEX = {"mouse1": BTN_LEFT, "mouse2": BTN_RIGHT,
                      "mouse3": BTN_MIDDLE, "mouse4": BTN_X1,
                      "mouse5": BTN_X2}

# A press is classified into a ROLE, not matched against a fixed message: the
# trigger role is whichever button trigger_button names (left by default) and
# the extra-options role whichever options_button names (right by default).
# The two side buttons share one pair of messages and are told apart by
# mouseData's high word, the only place Windows puts that information.
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

# Controller-initiated record/play actions are posted to the hook thread with
# this offset added, so handle_hotkey can route them to the controller
# library instead of the keyboard library. Keyboard hotkeys keep their plain
# ids; the offset range collides with nothing (plain ids are < 100).
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


class App(object):
    def __init__(self, cfg, identity=None):
        self.cfg = cfg
        self.identity = identity



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
        self._edge_log_at = 0.0




        self._pass_log_at = {}
        self._mouse2_log_at = 0.0
        self._lead_log_at = 0.0
        self.rbutton_down_time = 0.0
        self.snap_count = 0
        self.pass_count = 0

        self.last_snap_time = 0.0
        self.last_snap_pos = None

        #: (centre_x, centre_y, radius, target_box, candidate_boxes)
        #: in screen coordinates, stashed per scan for the debug
        #: overlay; None unless debug_visualize is on.
        self._debug_vis = None

        self.dpi_scale = 1.0






        self._vs_cache = {"t": 0.0, "v": None}

        self.hook = None
        self.proc_ref = None
        self.thread_id = None

        self._capture = CaptureBackend(cfg.get("capture_backend", "auto"))





        self._input = make_input_backend(cfg)
        self.follow = FollowMemory()
        self.subject = FigureMemory()
        self._debug_vis = None



        self._lbtn_down = False




        self._rbtn_down = False
        self._rbtn_armed = False
        self._rbtn_hold_done = False
        self._rbtn_press_id = 0
        self._rbtn_lock = threading.Lock()

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


        self.rec_store = RecordingStore()
        try:
            from config import config_dir as _cfg_dir
            _pad_path = os.path.join(_cfg_dir(), PAD_REC_FILE)
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






        self._track = deque(maxlen=32)
        self._tracker = vision.MotionTracker(dim=3)


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






        self._center_prev = None
        self._view_moved = False









        self._center_syn_down = False



        self._own_pid = os.getpid()



        self._pad_out = None
        self._pad_bindings = []
        self._pad_macros = []
        self._pad_index = None
        self._pad_connected = False
        self._pad_misses = 0
        self._pad_reader = None
        self._pad_cursor = None
        self._pad_target = None
        self._pad_last_drive = 0.0
        self._pad_click_down = False
        self._pad_right_down = False
        self._pad_entry = None
        self._pad_raw_down = False
        self._pad_prev_buttons = 0
        self._pad_prev_axes = {}
        self._pad_bypass = False
        self._pad_bypass_tracker = None
        self._pad_note_at = 0.0
        self._pad_emu_warned = False

        self._presence_nonce = secrets.token_hex(8)
        self._hb_sent = False
        self._bye_sent = False

    def is_recording(self):
        """True while a macro recording is armed (public read-only view of
        the capture buffer, so the GUI poller no longer reaches into
        app._rec_buffer)."""
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
        if now - self._pass_log_at.get(reason, 0.0) > 5.0:
            self._pass_log_at[reason] = now
            log("Click passed through: %s." % reason)

    def radius_px(self):
        try:
            return int(round(float(self.cfg.get("radius", 150))
                             * self.dpi_scale))
        except (TypeError, ValueError):
            return 150

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
        try:
            self.dpi_scale = get_dpi_scale()
        except Exception:
            pass
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
            if self.cfg.get("watch_center_only", False) \
                    and not cursor_visible():
                return True
        except Exception:
            pass
        return False

    def _center_box(self):
        """(mx, my, half) of the watched centre square, or None when
        "Watch the screen centre only" is off.

        The GATE square is fixed at the middle of the virtual screen,
        `half` each way (75 px at defaults, so a 150x150 gate), DPI-
        scaled like every other distance here. The SCAN is wider: the
        centre scan analyses cap = half * 2 around the middle, so items
        straddling the gate boundary are measured whole instead of
        clipped. A press outside the gate is not assistance territory
        (it passes through) even though an item there is within scan
        reach - territory is the gate, reach is the scan. The cursor
        position is irrelevant to the geometry: with the switch on, the
        product treats the screen centre as the reticle even when the
        pointer sits elsewhere.
        """
        try:
            if not bool(self.cfg.get("watch_center_only", False)):
                return None
        except Exception:
            return None
        vx, vy, vw, vh = self._virtual_screen()
        try:
            half = max(24, int(round(
                float(self.cfg.get("radius", 150)) * self.dpi_scale))) // 2
        except (TypeError, ValueError):
            half = 75
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

    def _scan_center(self, state=None, arm_follow=True):
        """Scan the watched centre square; return (target, mx, my).

        target.dx/dy are CENTRE-relative (the scan anchor is the screen
        middle, so the absolute sighting is mx + dx, my + dy) - every
        caller must convert through the anchor, never through the
        capture origin. Ranking is the engine default: high-contrast
        items first, the accent colour only boosts (or, with
        accent_only, filters). None when the switch is off, the frame
        is stale, or nothing is inside.
        """
        box = self._center_box()
        if box is None:
            return None
        mx, my, half = box
        if state is not None and state.get("abandoned"):
            return None



        cap = max(48, half * 2)
        img, cx, cy, origin, edges = self.grab_around(mx, my, cap)
        if img is None:
            return None
        if state is not None and state.get("abandoned"):
            return None
        follow_ctx = None
        if bool(self.cfg.get("follow_enabled")):
            follow_ctx = self.follow.context(
                origin[0], origin[1],
                float(self.cfg.get("follow_seconds", 20))
            )




        blind = 0.0
        flight = self._reticle_active()
        if flight:
            try:
                blind = max(0.0, float(
                    self.cfg.get("center_blind_area", 10)))
            except (TypeError, ValueError):
                blind = 0.0






        motion_ctx = None
        curr_gray = None
        try:
            sense = vision.motion_sense_of(self.cfg.values)
        except Exception:
            sense = 0.0
        if flight and sense > 0 \
                and bool(self.cfg.get("subject_priority", False)):
            try:
                curr_gray = img.mean(axis=2).astype(np.float32)
            except Exception:
                curr_gray = None
            with self._state_lock:
                prev = self._center_prev
                dirty = self._view_moved
            if curr_gray is not None and prev is not None and not dirty:
                try:
                    pframe, pts, pcap = (prev["gray"], prev["t"],
                                         prev["cap"])
                    dt = time.perf_counter() - pts
                    if pcap == cap and dt <= 0.40 \
                            and pframe.shape == curr_gray.shape:
                        movers = vision._frame_movers(pframe, curr_gray)
                        if bool(movers.any()):
                            motion_ctx = (movers, sense)
                except Exception:
                    motion_ctx = None
        try:
            subject_priority_on = (bool(self.cfg.get("subject_priority", False))
                                  or bool(self.cfg.get("subject_only", False)))
            hb = float(self.cfg.get("subject_focus_bias", 0) or 0) / 100.0
        except (TypeError, ValueError):
            hb = 0.0
        if not subject_priority_on:
            hb = 0.0
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
                              anchor_blind_area=blind,
                              motion_ctx=motion_ctx,
                              focus_bias=min(1.0, max(0.0, hb)),
                              subject_ctx=subject_ctx,
                              origin=origin,
                              collect=dbg_collect)
        except Exception as exc:
            self.fail_count += 1
            log("scan error: %r" % (exc,))
            if self.fail_count >= 5:
                self.scanner_dead = True
                log("Scanner disabled after repeated errors. "
                    "Clicks pass through.")
            if curr_gray is not None:
                with self._state_lock:
                    self._center_prev = {"gray": curr_gray,
                                         "t": time.perf_counter(),
                                         "cap": cap}
                    self._view_moved = False
            return None
        self.fail_count = 0

        # Debug visualization - stash for the GUI to hand to the reticle
        # overlay (screen-space centre, scan radius and the absolute box
        # of the picked target). Never touches the scan-centre cx/cy.
        if bool(self.cfg.get("debug_visualize", False)):
            try:
                vx, vy, vw, vh = self._virtual_screen()
                dbg_cx, dbg_cy = vx + vw // 2, vy + vh // 2
                target_box = None
                if res.kind == "target" and res.target is not None:
                    acx = origin[0] + cx + res.target.dx
                    acy = origin[1] + cy + res.target.dy
                    target_box = (
                        int(acx - res.target.bw / 2),
                        int(acy - res.target.bh / 2),
                        int(acx + res.target.bw / 2),
                        int(acy + res.target.bh / 2),
                    )
                self._debug_vis = (dbg_cx, dbg_cy, cap, target_box,
                                   _debug_boxes(dbg_collect, origin, cx, cy))
            except Exception:
                pass
        else:
            self._debug_vis = None

        if curr_gray is not None:




            with self._state_lock:
                self._center_prev = {"gray": curr_gray,
                                     "t": time.perf_counter(),
                                     "cap": cap}
                self._view_moved = False
        if state is not None and state.get("abandoned"):
            return None
        if res.kind == "target" and res.target is not None:





            ax = mx + res.target.dx
            ay = my + res.target.dy
            if arm_follow and bool(self.cfg.get("center_follow", False)):
                with self._state_lock:
                    prev = self._center_follow
                    if (prev is not None
                            and math.hypot(ax - prev["x"],
                                           ay - prev["y"]) > 3.0):


                        self._chase_stable = 0
                    self._center_follow = {
                        "x": ax, "y": ay,
                        "sig": res.target.sig,
                        "when": time.perf_counter(),
                        "misses": 0,
                        "area": getattr(res.target, "area", 0) or 0,
                    }





            now = time.perf_counter()
            with self._state_lock:
                self._track.append((now, ax, ay))
                while self._track and now - self._track[0][0] > 0.6:
                    self._track.popleft()
            try:
                area = float(getattr(res.target, "area", 0) or 0)
                if area > 0.0:
                    with self._state_lock:
                        self._tracker.update(
                            now, (ax, ay, math.sqrt(area)))
            except Exception:
                pass
            try:
                self._subject_store(ax, ay, res.target)
            except Exception:
                pass
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
                    and not self._reticle_active():
                if state is not None:
                    state["center_pass"] = True
                return None



            got = self._scan_center(state, arm_follow=(track is not False))
            if got is None:
                return None
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
            follow_ctx = self.follow.context(
                origin[0], origin[1],
                float(self.cfg.get("follow_seconds", 20))
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
            self.fail_count += 1
            log("scan error: %r" % (exc,))

            if self.fail_count >= 5:
                self.scanner_dead = True
                log("Scanner disabled after repeated errors. Clicks pass through.")

            return None

        self.fail_count = 0

        # Debug visualization - the same stash _scan_center keeps, for
        # the cursor-anchored scan: circle at the cursor, box around
        # the picked target, boxes around every candidate.
        if dbg_collect is not None:
            try:
                target_box = None
                if res.kind == "target" and res.target is not None:
                    acx = origin[0] + cx + res.target.dx
                    acy = origin[1] + cy + res.target.dy
                    target_box = (
                        int(acx - res.target.bw / 2),
                        int(acy - res.target.bh / 2),
                        int(acx + res.target.bw / 2),
                        int(acy + res.target.bh / 2),
                    )
                self._debug_vis = (x, y, cap, target_box,
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

        if res.kind == "target" and res.target is not None:
            if track is not None and track is not False:
                now = time.perf_counter()
                ax, ay = x + res.target.dx, y + res.target.dy
                track.append((now, ax, ay))
                while track and now - track[0][0] > 0.6:
                    track.popleft()





                try:
                    area = float(getattr(res.target, "area", 0) or 0)
                    if area > 0.0:
                        with self._state_lock:
                            self._tracker.update(
                                now, (ax, ay, math.sqrt(area)))
                except Exception:
                    pass
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

    def _nudge(self, dx, dy, view=False):
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
        """
        try:
            self._input.nudge(dx, dy)
        except Exception as exc:
            log("nudge error: %r" % (exc,))
        if view:
            with self._state_lock:
                self._view_moved = True

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

    def send_click(self, x, y, double=False, index=None):
        if index is None:
            index = self._trigger_button()
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
        if not self._atomic_click():
            self._button(True)
            time.sleep(0.015)
            self._button(False)

    def _follow_may_learn(self, target):
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

        glide_ms = 0 (default): one instant jump, lowest latency.
        glide_ms > 0: animated ease-out move over that many
        milliseconds so the user can follow where the cursor went.
        Any physical mouse movement aborts the animation immediately
        and hands control back to the user.

        Returns True if the cursor reached (nx, ny).
        """
        ms = int(self.cfg.get("glide_ms", 0) or 0)

        if ms <= 0:
            self._warp(nx, ny)
            return True

        x0, y0 = get_cursor_pos()
        steps = max(3, ms // 10)

        for i in range(1, steps + 1):
            t = i / float(steps)
            ease = 1.0 - (1.0 - t) ** 3
            px = int(round(x0 + (nx - x0) * ease))
            py = int(round(y0 + (ny - y0) * ease))
            self._warp(px, py)
            time.sleep(0.010)

            ax, ay = get_cursor_pos()
            if abs(ax - px) > 2 or abs(ay - py) > 2:
                return False

        return True

    def reinject_click(self, x, y):
        """
        Give back a click that classic mode suppressed but did not assist.
        """
        try:
            self.send_click(x, y, double=False)
        except Exception as exc:
            log("reinject error: %r" % (exc,))

    def _claim_delivery(self, state):
        """
        Exactly-once guard for a suppressed classic click.

        The disposable worker and the scanner's stall path race: without
        this, a worker that finishes just after the 1 s join timeout
        could deliver its snap click ON TOP of the click the scanner
        already returned (a double click from one physical press). Only
        the caller that claims may deliver or reinject; a click is
        returned exactly once - never zero, never twice.
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
        If classic clicks were suppressed but never completed, give
        them back - EXACTLY ONCE.

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
            if entry.get("up"):
                continue
            state = entry.get("state")
            if state is not None:
                if not self._claim_delivery(state):
                    continue
            elif self.worker_active:
                continue
            if entry.get("pad"):
                continue
            self.reinject_click(entry["x"], entry["y"])
            log("Suppressed click returned safely.")









        with self._state_lock:
            had_hold = self._center_hold is not None
            self._center_hold = None
            self._guide_hold = None
            if had_hold:
                self._center_hold_tomb = time.perf_counter()
            need_up = self._center_syn_down
            if need_up:
                self._center_syn_down = False
        if need_up:
            try:
                self._button(False)
            except Exception as exc:
                log("synthetic release error: %r" % (exc,))

    def _drain_queue(self):
        """
        Discard queued jobs. A discarded classic job will never be
        delivered by a worker, so its suppressed click is returned
        here - exactly once: the job's pending entry is removed too,
        so _emergency_release cannot return it a second time.
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
                    if entry.get("pad"):
                        # Release ownership so a later _pad_release cannot
                        # mark an orphaned entry (identity check: a
                        # concurrent new press already owns a fresh one).
                        if self._pad_entry is entry:
                            self._pad_entry = None
                        continue
                self.reinject_click(item[1], item[2])
                log("Queued click returned safely.")

    def do_classic(self, x, y, out, state):
        """
        Mode 1:
        The physical down was suppressed.
        Find target, move, click. If no target, return the click.
        """
        try:
            if not self._epoch_alive(state):





                if self._claim_delivery(state):
                    self.reinject_click(x, y)
                out["handled"] = True
                return
            if state.get("abandoned"):
                out["handled"] = True
                return

            target = self.scan_near(x, y, state, self._track)

            if not self._epoch_alive(state):


                if self._claim_delivery(state):
                    self.reinject_click(x, y)
                out["handled"] = True
                return
            if state.get("abandoned"):
                out["handled"] = True
                return

            if target is None:
                self.pass_count += 1









                if state.get("center_pass"):
                    self._pass_note("outside the watched centre box")
                elif state.get("abandoned"):
                    self._pass_note("worker stalled")
                else:
                    self._pass_note("no target in range")
                if self._claim_delivery(state):
                    self.reinject_click(x, y)
                out["handled"] = True
                return

















            cbox = self._center_box()
            if cbox is not None:
                ax, ay = (cbox[0] + target.dx, cbox[1] + target.dy)
            else:
                ax, ay = (x + target.dx, y + target.dy)
            try:
                on_target_cfg = float(self.cfg.get("on_target_dist", 10))
            except (TypeError, ValueError):
                on_target_cfg = 10.0
            hit = self._hit_test(ax, ay, x, y, target.bw, target.bh,
                                 on_target_cfg)

            if cbox is not None and self._reticle_active():









                if not self._claim_delivery(state):
                    out["handled"] = True
                    return
                if not self._epoch_alive(state):


                    self.reinject_click(x, y)
                    out["handled"] = True
                    return







                try:
                    _ot = float(self.cfg.get("on_target_dist", 10))
                except (TypeError, ValueError):
                    _ot = 10.0
                if self._hit_test(cbox[0] + target.dx, cbox[1] + target.dy,
                                  cbox[0], cbox[1], target.bw, target.bh,
                                  _ot):
                    self.send_click(x, y)
                    self.snap_count += 1
                    if self._lbtn_down:
                        with self._state_lock:
                            hold_now = self._lbtn_down
                            claim = hold_now and not self._center_syn_down
                            if claim:
                                self._center_syn_down = True
                            if hold_now:
                                self._center_hold = self._new_hold_session()
                        if claim:
                            self._button(True)
                    if self._follow_may_learn(target):
                        self.follow.store(ax, ay, target)
                    self.last_snap_time = time.perf_counter()
                    self.last_snap_pos = (int(ax), int(ay))
                    log("Classic assist: already on it - clicked, "
                        "view untouched.")
                    out["handled"] = True
                    return
                nx, ny = ax, ay
                if bool(self.cfg.get("motion_predict", False)) \
                        or bool(self.cfg.get("motion_3d", False)):
                    nx, ny = self._apply_motion_lead(nx, ny)
                nx, ny = self._apply_snap_offset(nx, ny)




                ndx, ndy = self._cap_step(nx - cbox[0], ny - cbox[1],
                                          self._max_step_cfg())
                self._nudge(ndx, ndy, view=True)
                self.send_click(x, y)
                self.snap_count += 1
                if self._lbtn_down:







                    with self._state_lock:
                        hold_now = self._lbtn_down
                        claim = hold_now and not self._center_syn_down
                        if claim:
                            self._center_syn_down = True
                        if hold_now:
                            self._center_hold = self._new_hold_session()
                    if claim:
                        self._button(True)
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
                    nx, ny = self._apply_motion_lead(nx, ny)






                nx, ny = self._apply_snap_offset(nx, ny)

            now = time.perf_counter()
            watcher = float(self.cfg.get("watcher_seconds", 0.45))

            double = False
            if (
                self.last_snap_pos is not None
                and now - self.last_snap_time < watcher
            ):
                lx, ly = self.last_snap_pos
                if math.hypot(nx - lx, ny - ly) <= 35.0:
                    double = True




            if not self._claim_delivery(state):
                out["handled"] = True
                return

            if not self._epoch_alive(state):



                self.reinject_click(x, y)
                out["handled"] = True
                return

            self.snap_count += 1
            if cbox is not None and self._lbtn_down:













                with self._state_lock:
                    hold_now = self._lbtn_down
                    claim = hold_now and not self._center_syn_down
                    if claim:
                        self._center_syn_down = True
                    if hold_now:
                        self._center_hold = self._new_hold_session()
                if claim:
                    self._button(True)
            if self._follow_may_learn(target):



                self.follow.store(ax, ay, target)







            with self._state_lock:
                hold_now = self._lbtn_down
                locked = bool(self.follow_lock
                              and self.follow_lock.get("active"))
            if (bool(self.cfg.get("hold_follow", True))
                    and hold_now
                    and target.sig is not None
                    and not locked):
                rgb = vision.hsv_to_rgb(*target.sig)
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
                self.pass_count += 1



                out["handled"] = True
                return





            cbox = self._center_box()
            if cbox is not None:
                ax, ay = (cbox[0] + target.dx, cbox[1] + target.dy)
            else:
                ax, ay = (x + target.dx, y + target.dy)
            try:
                on_target = float(self.cfg.get("on_target_dist", 10))
            except (TypeError, ValueError):
                on_target = 10.0



            if self._hit_test(ax, ay, x, y, target.bw, target.bh,
                              on_target):
                self.pass_count += 1
                out["handled"] = True
                return

            nx, ny = ax, ay









            if cbox is not None and self._reticle_active():
                if bool(self.cfg.get("motion_predict", False)) \
                        or bool(self.cfg.get("motion_3d", False)):
                    nx, ny = self._apply_motion_lead(nx, ny)
                nx, ny = self._apply_snap_offset(nx, ny)
                ndx, ndy = self._cap_step(nx - cbox[0], ny - cbox[1],
                                          self._max_step_cfg())



                if self._hit_test(cbox[0] + target.dx, cbox[1] + target.dy,
                                  cbox[0], cbox[1], target.bw, target.bh,
                                  on_target):
                    ndx = ndy = 0
                else:
                    self._nudge(ndx, ndy, view=True)
                completed = True
            else:

                if bool(self.cfg.get("motion_predict", False)) \
                        or bool(self.cfg.get("motion_3d", False)):
                    nx, ny = self._apply_motion_lead(nx, ny)

                nx, ny = self._apply_snap_offset(nx, ny)

                completed = self.glide_cursor(nx, ny)
                ndx = ndy = None

            if completed or ndx is not None:
                self.snap_count += 1
            if self._follow_may_learn(target):


                self.follow.store(ax, ay, target)
            self.last_snap_time = time.perf_counter()
            self.last_snap_pos = (int(nx), int(ny))

            if ndx is None:
                log(
                    "Guide assist: cursor moved %.0f px to (%d, %d)%s"
                    % (math.hypot(nx - x, ny - y), int(nx), int(ny),
                       "" if completed else " (glide aborted: you took over)")
                )
            else:




                if ndx == 0 and ndy == 0:
                    log("Guide assist: already on it - view untouched.")
                else:
                    log("Guide assist: view rotated (%+d, %+d) by nudge."
                        % (int(ndx), int(ndy)))

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
                     "epoch": self._mode_epoch}






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





            try:
                _glide_s = float(self.cfg.get("glide_ms", 0) or 0) / 1000.0
            except (TypeError, ValueError):
                _glide_s = 0.0
            th.join(timeout=1.0 + max(0.0, _glide_s) + 0.5)

            self.worker_active = False

            if th.is_alive():
                with state["lock"]:
                    state["abandoned"] = True
                    already = state["delivered"]

                if kind == "classic" and not already:


                    self.reinject_click(x, y)
                    log("Assist worker stalled. Suppressed click returned.")

                self.pause_until = time.perf_counter() + float(
                    self.cfg.get("stall_pause_seconds", 0.25)
                )

            elif kind == "classic" and not out.get("handled"):

                if self._claim_delivery(state):
                    self.reinject_click(x, y)

            if kind == "classic" and entry is not None:


                with self._pending_lock:
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
        pad = vision.FOLLOW_POS_PAD
        # snapshot() reads under FollowMemory's OWN lock - self._state_lock
        # does not order these attributes against a concurrent store(), so
        # reading them under it could tear across two sightings.
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
                <= float(self.cfg.get("follow_seconds", 20))
                and near_memory
                and not _locked):
            rgb = vision.hsv_to_rgb(*_mem_sig)
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

    def _arm_center_hold(self, x, y, mode, armed):
        """Start a centre-hold steering session for a held press in mode 2.

        Shared by the mouse hook and the controller drive, for the same reason
        as _arm_hold_follow: one definition of "the press is inside the
        watched centre square".
        """
        if mode != 2 or not armed:
            return
        cbox = self._center_box()
        if cbox is None:
            return
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
            need_up = self._center_syn_down
            if need_up:
                self._center_syn_down = False
            hold_lock = bool(self.follow_lock
                             and self.follow_lock.get("source") == "hold")
            if hold_lock:
                self.follow_lock = None
            if was_center:
                self._track.clear()
                try:
                    self._tracker.reset()
                except Exception:
                    pass
                self._chase_ref = None
                self._center_prev = None
                self._cam_prev = None
        if need_up:
            try:
                self._button(False)
            except Exception as exc:
                log("synthetic release error: %r" % (exc,))
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
                    return 1




        if mode == 2 and armed and not was_center:
            self.click_q.put(("guide", pt.x, pt.y, now, None))
        return None

    def make_hook_proc(self):














        def proc(nCode, wParam, lParam):
            try:
                if nCode == HC_ACTION and not self.stop.is_set():
                    info = ctypes.cast(
                        lParam, ctypes.POINTER(MSLLHOOKSTRUCT)
                    ).contents










                    try:
                        consume = getattr(self._input,
                                          "consume_expected", None)
                        if consume is not None and consume(
                                int(wParam), info.pt.x, info.pt.y):
                            return user32.CallNextHookEx(
                                self.hook, nCode, wParam, lParam)
                    except Exception:
                        pass












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

                        # The role is decided per event, not when the hook is
                        # installed: rebinding a button in the GUI takes effect
                        # on the next press with the hook still in place. A
                        # button that holds neither role is none of our
                        # business and goes straight on to Windows.
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

                        try:
                            mode = int(self.cfg.get("mode", 2))
                        except (TypeError, ValueError):
                            mode = 2

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









                            self._arm_hold_follow(info.pt.x, info.pt.y,
                                                  now, armed)
                            self._arm_center_hold(info.pt.x, info.pt.y,
                                                  mode, armed)
















                            rapid = bool(self.cfg.get("rapid_click_snap",
                                                      False))
                            if mode == 1 and armed:
                                with self._pending_lock:
                                    un_upped = any(
                                        not e.get("up")
                                        for e in self.pending_classics
                                    )
                                    if not un_upped and (
                                            rapid
                                            or (self.click_q.qsize() == 0
                                                and not self.worker_active)):
                                        entry = {"x": info.pt.x,
                                                 "y": info.pt.y,
                                                 "up": False, "state": None}
                                        self.pending_classics.append(entry)
                                    else:
                                        entry = None
                                if entry is not None:
                                    self.click_q.put(
                                        ("classic", info.pt.x, info.pt.y,
                                         now, entry)
                                    )
                                    return 1




                                self._pass_note(
                                    "previous click still assisted "
                                    "(rapid off)")

                        elif role == ROLE_TRIGGER:
                            if self._release_bookkeeping(
                                    role, armed, mode, now, info.pt) == 1:
                                return 1

            except Exception as exc:
                log("hook error: %r" % (exc,))

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
                    elif wParam == WM_KEYDOWN:
                        if vk in self._key_held:
                            pass
                        else:
                            self._key_held.add(vk)
                            if not (info.flags & LLKHF_INJECTED) \
                                    and not self.input_focused:












                                self._record_press(vk)
                                fired, swallow = self._macro_press(vk)
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
        _tm, _tv, amods, avk = spec
        self.macro_q.put(("combo", amods, avk))
        log("Macro: trigger fired - sending the combo once.")
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
        try:
            return int(self.cfg.get("mode", 2)) != 1
        except (TypeError, ValueError):
            return True

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
                still = bool(user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000)                         or self._pad_right_down
            except Exception:
                still = True
            if not (still and self._armed_now()):




                return
            with self._rbtn_lock:
                if pid != self._rbtn_press_id or self._rbtn_hold_done:
                    return
                self._rbtn_hold_done = True
            self._handle_mouse2("mouse2_hold_action",
                                "right-button hold")
        except Exception as exc:
            log("mouse2 hold error: %r" % (exc,))

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
                rgb = vision.hsv_to_rgb(*_sig)
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
        """One pass of "Keep the cursor on it" (centre-watch follow)."""
        if not bool(self.cfg.get("watch_center_only", False)):
            return False

        motion_on = (bool(self.cfg.get("motion_predict", False))
                    or bool(self.cfg.get("motion_3d", False)))

        cf = self._center_follow
        now = time.perf_counter()

        if cf is None or (now - cf["when"]) > 1.0:

            stale = cf is not None
            if stale:
                with self._state_lock:
                    self._center_follow = None

            box = self._center_box()
            if box is None:
                return False
            mx, my, half = box
            try:
                x, y = get_cursor_pos()
            except RuntimeError:
                return False
            if (abs(x - mx) > half or abs(y - my) > half) \
                    and not self._reticle_active():
                self._pick_at = None
                return False
            if stale:
                self._pick_at = None







            target = self.scan_near(x, y, None, False)
            if target is not None:
                ax = mx + target.dx
                ay = my + target.dy
                if (self._pick_at is not None
                        and math.hypot(ax - self._pick_at[0],
                                    ay - self._pick_at[1]) <= 6.0
                        and now - self._pick_at_time >= 0.25):
                    with self._state_lock:
                        self._center_follow = {
                            "x": ax, "y": ay,
                            "sig": target.sig,
                            "when": time.perf_counter(),
                            "misses": 0,
                            "area": getattr(target, "area", 0) or 0,
                        }
                    self._pick_at = None
                    log("Centre follow: keeping the cursor on the "
                        "picked item.")
                else:
                    anchors = vision._parse_accents(
                        self.cfg.get("accent_color", "off"))
                    _sig_m, _when_m = self.follow.snapshot()[2:]
                    has_anchor = bool(anchors) \
                        or (_sig_m is not None and _when_m > 0.0)
                    if not has_anchor and self._pick_at is None:
                        with self._state_lock:
                            self._center_follow = {
                                "x": ax, "y": ay,
                                "sig": target.sig,
                                "when": time.perf_counter(),
                                "misses": 0,
                                "area": getattr(target, "area", 0) or 0,
                            }
                        log("Centre follow: keeping the cursor on the "
                            "item (contrast tracking - no accent "
                            "colour set).")
                    else:
                        self._pick_at = (ax, ay)
                        self._pick_at_time = now
            else:
                self._pick_at = None

            with self._state_lock:
                armed_now = self._center_follow is not None
            if motion_on and armed_now:
                self._feed_motion_track()
            return True

        self._pick_at = None


        if self._lbtn_down or self._rbtn_down or self._pad_click_down or self._pad_raw_down:
            return True
        with self._state_lock:
            lock = self.follow_lock
        if lock and lock.get("active"):
            return True




















        found = False
        try:
            got = self._scan_center(None, arm_follow=False)
        except Exception as exc:
            log("centre-follow scan error: %r" % (exc,))
            got = None
        with self._state_lock:
            live = self._center_follow is cf
        if got is not None and live:
            _target, _mx, _my = got
            with self._state_lock:
                cf["x"] = _mx + _target.dx
                cf["y"] = _my + _target.dy
                cf["sig"] = _target.sig
                cf["area"] = getattr(_target, "area", 0) or 0
                cf["misses"] = 0
            found = True

        if not found:





            with self._state_lock:
                cf["misses"] = cf.get("misses", 0) + 1
                misses = cf["misses"]
                if misses >= 12:
                    self._center_follow = None
                    self._chase_stable = 0
                    self._chase_ref = None
            return True

        with self._state_lock:
            cf["when"] = now

        if motion_on:
            self._feed_motion_track()

        cx, cy = cf["x"], cf["y"]
        nx, ny = self._apply_motion_lead(cx, cy)


        nx, ny = self._apply_snap_offset(nx, ny)
        px, py = get_cursor_pos()
        if math.hypot(nx - px, ny - py) <= 2.0:
            with self._state_lock:
                self._chase_stable += 1
            return True





        with self._state_lock:
            ref = self._chase_ref
        if ref is not None \
                and math.hypot(px - ref[0], py - ref[1]) > 12.0:
            with self._state_lock:
                self._chase_ref = (px, py)
            return True
        self._warp(nx, ny)
        with self._state_lock:
            self._chase_stable = 0
            self._chase_ref = (nx, ny)
        return True

    def _feed_motion_track(self):
        """Refresh the Kalman/linear track with the freshest centre
        sighting so the motion lead stays alive during a chase."""
        with self._state_lock:
            cf = self._center_follow
        if cf is None:
            return
        now = time.perf_counter()
        with self._state_lock:
            self._track.append((now, cf["x"], cf["y"]))
            while self._track and now - self._track[0][0] > 0.6:
                self._track.popleft()
        try:
            area = float(cf.get("area", 0) or 0)
            if area > 0.0:
                with self._state_lock:
                    self._tracker.update(now, (cf["x"], cf["y"],
                                               math.sqrt(area)))
        except Exception:
            pass

    @staticmethod
    def _new_hold_session():
        """Fresh centre-hold steering state (addon Snap parity).

        px/py: EMA smoothing state (previous_x/y). cx/cy: fractional
        carry so sub-pixel EMA output accumulates instead of rounding
        down to zero and stalling slow approaches. rem_x/rem_y: fractional
        carry for _nudge speed residuals. last_t: cycle
        stamp for the time-derived EMA weight. last_tap: last
        press-unit tap (repeat_hold backends only).
        """
        return {"px": 0.0, "py": 0.0, "cx": 0.0, "cy": 0.0,
                "rem_x": 0.0, "rem_y": 0.0,
                "last_t": time.perf_counter(), "last_tap": 0.0}

    def _center_hold_cycle(self):
        """One pass of a centre-watch hold session (mouse1 still down).

        The standalone addon's frame, ported: re-scan the watched
        square, ease toward the fresh sighting, emit ONE relative
        nudge - steering only, never a click. In reticle flight the
        view rotates by the nudge, so the same sighting re-scanned next
        cycle shrank toward zero and closes the loop. On the desktop
        the cursor itself walks: the target is measured against the
        live cursor position (steering by the raw centre offset there
        would drift past the item by the offset every frame). No
        target: decay the pending move toward zero (smooth stop, addon
        DECAY_RATE) and keep the session - the item may walk back
        into the square while the button is still held.

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
                or (not self._lbtn_down and not self._pad_click_down and not self._pad_raw_down):
            with self._state_lock:
                if self._center_hold is sess:
                    self._center_hold = None
            return False
        now = time.perf_counter()
        dt = min(max(now - sess.get("last_t", now), 0.001), 0.1)
        sess["last_t"] = now
        try:
            got = self._scan_center(None, arm_follow=False)
        except Exception as exc:
            log("centre-hold scan error: %r" % (exc,))
            got = None
        with self._state_lock:
            if self._center_hold is not sess:



                return False
        if got is None:
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
        if not steer:



            k = max(0.0, 1.0 - 8.0 * dt)
            sess["px"] *= k
            sess["py"] *= k
        else:
            target, _mx, _my = got
            if self._reticle_active():


                tx, ty = float(target.dx), float(target.dy)
            else:





                try:
                    _pcx, _pcy = get_cursor_pos()
                    tx = (_mx + float(target.dx)) - _pcx
                    ty = (_my + float(target.dy)) - _pcy
                except Exception:
                    tx, ty = float(target.dx), float(target.dy)


            alpha = 1.0 - math.exp(-dt / 0.05)
            alpha = min(max(alpha, 0.0), 1.0)
            sess["px"] += alpha * (tx - sess["px"])
            sess["py"] += alpha * (ty - sess["py"])



        out_x = sess["px"] + sess.get("cx", 0.0)
        out_y = sess["py"] + sess.get("cy", 0.0)
        ix, iy = int(round(out_x)), int(round(out_y))


        with self._state_lock:
            if self._center_hold is not sess \
                    or (not self._lbtn_down
                        and not self._pad_click_down
                        and not self._pad_raw_down):
                return False







        cap = self._max_step_cfg()
        jx, jy = self._cap_step(ix, iy, cap)
        if cap > 0:
            sess["cx"] = min(max(out_x - jx, -cap), cap)
            sess["cy"] = min(max(out_y - jy, -cap), cap)
        else:
            sess["cx"], sess["cy"] = (out_x - ix, out_y - iy)
        ix, iy = jx, jy
        if ix or iy:




            self._nudge(ix, iy, view=True)
        if now - sess.get("last_beat", 0.0) > 2.0:



            sess["last_beat"] = now
            log("Centre hold: steering (%+d,%+d)/frame toward "
                "(%+d,%+d) EMA."
                % (ix, iy, int(round(sess["px"])),
                   int(round(sess["py"]))))
        interval = float(getattr(self._input, "tap_interval", 0) or 0)
        if interval > 0:






            if now - sess.get("last_tap", 0.0) >= interval:
                sess["last_tap"] = now
                self._button(True)
        return True

    def run_follow_worker(self):
        """
        While a follow-lock is active, keep the cursor on the locked
        colour's nearest item (stabilises drags, e.g. scrollbar thumbs).

        Also drives "Keep the cursor on it": with the centre watch on,
        park the cursor on the picked item inside the watched square
        and chase it while it moves (scroll planes and similar).

        And centre-watch holds: while mouse1 is held after a centre
        snap/press, re-scan the watched square about every 2-3 frames
        and steer onto each fresh sighting with relative nudges (the
        addon model - single clicks snap exactly once, only a HELD
        button keeps steering alive).
        """
        while not self.stop.is_set():
            if not self.running \
                    or time.perf_counter() < self.pause_until:
                time.sleep(0.05)
                continue





            if self._bypass_enabled() and self._bypass_held():
                time.sleep(0.05)
                continue






            with self._state_lock:
                _hold = self._center_hold
            if _hold is not None:
                if self._center_hold_cycle():
                    time.sleep(0.04)
                    continue
                time.sleep(0.05)
                continue

            with self._state_lock:
                lock = self.follow_lock
            center_on = (bool(self.cfg.get("center_follow", False))
                         and bool(self.cfg.get("watch_center_only", False)))
            if not center_on:
                with self._state_lock:
                    self._center_follow = None
                    self._pick_at = None
                    self._chase_ref = None
            if not lock or not lock.get("active"):
                if center_on and self._center_follow_cycle():
                    with self._state_lock:
                        _stable = self._chase_stable
                    time.sleep(0.08 if _stable >= 4 else 0.04)
                    continue
                time.sleep(0.05)
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
                    res = vision.scan_region(img, cx, cy, cfgv, radius,
                                             subject_ctx=_sctx)
                    with self._state_lock:
                        live = self.follow_lock is lock
                    if not live:
                        continue
                    if res.kind == "target" and res.target is not None:
                        lock["misses"] = 0







                        ax = origin[0] + cx + res.target.dx
                        ay = origin[1] + cy + res.target.dy
                        try:
                            self._subject_store(ax, ay, res.target)
                        except Exception:
                            pass
                        try:
                            on_td = float(
                                self.cfg.get("on_target_dist", 10))
                        except (TypeError, ValueError):
                            on_td = 10.0
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

    def _auto_can_arm(self):
        """May the auto-click loop act at all right now?

        The on/off switch is the whole control now: caps, guards and
        waits are retired, so this is a single switch read. An accent
        colour is NO LONGER required. The user's stated intent
        ("pressed mouse1") is the gesture; with no colour set the scan
        falls back to the same contrast-first ranking every other feature
        uses, gated by the fixed 60 crispness gate so only a crisp,
        clearly drawn item can qualify. When a colour IS set it
        still wins (accent_only), exactly as before.
        """
        return bool(self.cfg.get("auto_click", False))

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

        if res.kind != "target" or res.target is None:
            return None

        try:
            self._subject_store(cx + res.target.dx, cy + res.target.dy, res.target)
        except Exception:
            pass

        return cx + res.target.dx, cy + res.target.dy

    def _apply_motion_lead(self, nx, ny):
        """Aim at where the tracked item WILL be, not where the scan
        saw it. Returns the (possibly unchanged) snap point.

        Two engines behind one switch: the legacy linear fit
        (predict_motion) by default, or the Kalman tracker - position
        plus scale - when motion_3d is on. Either switch alone enables
        leading (motion_3d does not need motion_predict); both share
        the same gates (too slow, too stale, capped), so the engine
        choice never changes what a click is allowed to do, only how
        well it follows. The scale reading is logged when there is a
        lead to report, so a growing dialog visibly explains itself.
        """
        try:
            lead_ms = float(self.cfg.get("motion_lead_ms", 50))
        except Exception:
            lead_ms = 50.0
        tag = ""




        if bool(self.cfg.get("motion_3d", False)):
            with self._state_lock:
                pred = self._tracker.predict(lead_ms, MOTION_MAX_OFFSET)
            ox, oy = float(pred[0]), float(pred[1])
            if len(pred) > 2:
                try:
                    ratio = float(pred[2])



                    tag = ", scale x%.2f" % ratio \
                        if abs(ratio - 1.0) >= 0.02 else ""
                except (TypeError, ValueError):
                    tag = ""
        else:
            with self._state_lock:
                _obs = list(self._track)
            ox, oy = vision.predict_motion(_obs, lead_ms,
                                           MOTION_MAX_OFFSET)
        if abs(ox) + abs(oy) >= 1.5:
            nx = int(round(nx + ox))
            ny = int(round(ny + oy))
            if time.perf_counter() - self._lead_log_at > 5.0:
                self._lead_log_at = time.perf_counter()
                log("Motion lead: %+d,%+d px%s" % (ox, oy, tag))
        return nx, ny

    def _apply_snap_offset(self, nx, ny):
        """Shift a snap point by the user's snap_offset_x/y.

        Returns (int, int). A zero offset is returned untouched, so the
        default configuration behaves bit-for-bit as before.
        """
        try:
            ox = int(self.cfg.get("snap_offset_x", 0) or 0)
        except (TypeError, ValueError):
            ox = 0
        try:
            oy = int(self.cfg.get("snap_offset_y", 0) or 0)
        except (TypeError, ValueError):
            oy = 0
        if ox or oy:
            return int(nx + ox), int(ny + oy)
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





        self._local_combos = {(0, vk) for _hid, vk in local}

        self._macros, self._macro_map = build_macro_table(self.cfg)
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
            k = (m[0] & 0x000F, m[1])
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
            # This runs on the hook thread, which owns the message loop and
            # therefore the low-level hooks: an exception here would kill the
            # engine outright. A bad controller table must never be able to do
            # that, so it is reported and the keyboard keeps working.
            log("Controller: the binding tables could not be built (%r); "
                "controller actions stay as they were." % (exc,))
        else:
            if self._pad_connected:
                log("Controller: %d action binding(s) and %d macro(s) "
                    "reloaded." % (bound, macros))

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
        self._drain_queue()
        self._emergency_release()
        self._pad_release_all()
        # _pad_release (inside _pad_release_all) may have queued one
        # last OLD-mode guide job while running was still True; drop it
        # so nothing from the dead mode moves the cursor in the new one.
        self._drain_queue()
        self.stop_playback()
        with self._state_lock:
            self.follow_lock = None
            self._center_follow = None
            self._guide_hold = None
            self._pick_at = None
            self._chase_ref = None
            self._track.clear()
            try:
                self._tracker.reset()
            except Exception:
                pass
            try:
                self.subject.clear()
            except Exception:
                pass
            self._mode_epoch += 1
        self.pause_until = 0.0
        self.cfg.set("mode", mode)

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
            if not self.running:
                self.running = True



                self._auto_reset_session()
                log(
                    "LAUNCHED - assist active, mode %s "
                    "(%s pauses, %s / %s switch modes)"
                    % (self.cfg.get("mode"),
                       self.cfg.get("key_pause", "ctrl+p"),
                       effective_mode_key(self.cfg, 1),
                       effective_mode_key(self.cfg, 2))
                )

        elif action == "pause":
            if self.running:
                # running goes down FIRST: the pad teardown below calls
                # _pad_release, whose mode-2 guide enqueue is gated on
                # _armed_now() - with running still True it could queue
                # one last cursor move that lands AFTER "PAUSED".
                self.running = False
                self._drain_queue()
                self._emergency_release()
                self._pad_release_all()
                with self._state_lock:
                    self.follow_lock = None
                    self._center_follow = None
                    self._pick_at = None
                    self._chase_ref = None



                self.stop_playback()
                log("PAUSED - your mouse and keyboard are entirely yours.")

        elif action == "reload":
            self._drain_queue()
            self._emergency_release()
            with self._state_lock:
                self.follow_lock = None
                self._center_follow = None
                self._pick_at = None
                self._chase_ref = None
            self.cfg.reload()
            self._rebind_hotkeys()

            note = ""
            backend = str(self.cfg.get("capture_backend", "auto"))
            if backend != self._capture.prefer:
                note = " (capture_backend change needs a restart)"

            log("CONFIG RELOADED from disk%s." % note)

        elif action == "mode1":
            self._force_mode(1)
            log(
                "MODE 1 classic: mis-click suppressed, snap, auto-click. "
                "Shift bypasses."
            )

        elif action == "mode2":
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
        # running down first: the pad release below must not queue a
        # final guide job into a scanner that is about to be stopped.
        self.running = False
        self._drain_queue()
        self._emergency_release()
        self._pad_release_all()
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
        self.stop.set()

        if self.thread_id:
            try:
                PostThreadMessage(self.thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass

    def run_hook_thread(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        self.dpi_scale = get_dpi_scale()


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


    # ---- controller (XInput) ------------------------------------------

    PAD_SPEED = 900.0
    PAD_MISS_LIMIT = 3

    def _pad_enabled(self):
        """True when the user switched controller input on AND this machine
        can read a pad. The second half matters: without it the switch would
        look broken on a PC with no XInput runtime, instead of saying so."""
        try:
            if not bool(self.cfg.get("controller_input", False)):
                return False
        except (TypeError, ValueError):
            return False
        if self._pad_reader is not None:
            return True
        if not gamepad.available():
            return False
        return True

    def _pad_settings(self):
        """The numeric knobs, each coerced with its own fallback so one bad
        value in config.ini can never stop the poller."""
        def num(key, default, lo, hi):
            try:
                value = float(self.cfg.get(key, default))
            except (TypeError, ValueError):
                return float(default)
            return max(lo, min(hi, value))
        hz = num("pad_poll_hz", gamepad.DEFAULT_POLL_HZ, 30.0, 500.0)
        deadzone_pct = num("pad_deadzone", 20, 0, 60)
        return {
            "interval": 1.0 / hz,
            "threshold": int(num("pad_trigger_threshold",
                                 gamepad.DEFAULT_TRIGGER_THRESHOLD, 1, 255)),
            "deadzone": int(gamepad.AXIS_FULL * deadzone_pct / 100.0),
            "speed": self.PAD_SPEED * num("pad_sensitivity", 1.0, 0.1, 4.0),
            "drive": bool(self.cfg.get("pad_drive", True)),
            "rumble": bool(self.cfg.get("pad_rumble", True)),
            "index": int(num("pad_index", -1, -1, gamepad.XUSER_MAX_COUNT - 1)),
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
        if not bool(self.cfg.get("pad_drive", True)):
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
        if self._pad_reader is not None:
            return settings["index"] if settings["index"] >= 0 else 0
        index = gamepad.first_connected(settings["index"],
                                        reader=self._pad_reader)
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
                state = gamepad.read(index, settings["threshold"],
                                     reader=self._pad_reader)
                if not state.connected:
                    # One failed read is a glitch, not an unplug: XInput
                    # hiccups, and a reader that raises is answered with the
                    # empty state. Declaring a disconnect on the first miss
                    # released every held button and logged a teardown/reconnect
                    # pair per hiccup, so it takes PAD_MISS_LIMIT in a row.
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
        holding, so nothing can stay stuck down."""
        self._pad_connected = False
        self._pad_index = None
        self._pad_cursor = None
        self._pad_target = None
        self._pad_bypass = False
        self._pad_release_all()
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
            if event != "press" or id(binding) in shadow:
                continue
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
            self.macro_q.put(("combo", action[1], action[2]))
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
            return False
        return True

    def _pad_release_all(self):
        """Let go of everything the controller path could be holding."""
        if self._pad_click_down:
            self._pad_click_down = False
            try:
                x, y = get_cursor_pos()
                self._pad_release(int(x), int(y), time.perf_counter())
            except Exception as exc:
                log("controller release error: %r" % (exc,))
        if self._pad_right_down:
            self._pad_right_down = False
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
            real = get_cursor_pos()
            if self._pad_cursor is None:
                self._pad_cursor = real
            elif self._pad_target is not None \
                    and (abs(real[0] - self._pad_target[0]) > 3
                         or abs(real[1] - self._pad_target[1]) > 3):
                # Somebody else moved the pointer since our last frame - the
                # assistance snapped it onto a target, or the user has a mouse
                # as well. Adopt where it actually is instead of yanking it
                # back: fighting a snap would undo the very thing the assist
                # just did, and fighting a mouse makes both unusable.
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
            self._nudge(int(round(rx * 24.0)), int(round(-ry * 24.0)),
                        view=True)

        click_index = settings["click"]
        right_index = settings["right"]
        if click_index is None:
            click_index = gamepad.parse_token("rt")
        if right_index is None:
            right_index = gamepad.parse_token("lt")

        click_down = state.down(click_index) if click_index is not None else False
        if click_down and not self._pad_click_down:
            self._pad_click_down = True
            x, y = get_cursor_pos()
            self._pad_press(int(x), int(y), now)
        elif not click_down and self._pad_click_down:
            self._pad_click_down = False
            x, y = get_cursor_pos()
            self._pad_release(int(x), int(y), now)

        if right_index is not None and right_index != click_index:
            right_down = state.down(right_index)
            if right_down and not self._pad_right_down:
                self._pad_right_down = True
                try:
                    inject_mouse(BTN_RIGHT, True)
                except Exception:
                    pass
            elif not right_down and self._pad_right_down:
                self._pad_right_down = False
                try:
                    inject_mouse(BTN_RIGHT, False)
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
        own = self._fg_is_own()
        try:
            mode = int(self.cfg.get("mode", 2))
        except (TypeError, ValueError):
            mode = 2
        if armed and not own:
            self._arm_hold_follow(x, y, now, armed)
            self._arm_center_hold(x, y, mode, armed)
        if mode == 1 and armed and not own:
            rapid = bool(self.cfg.get("rapid_click_snap", False))
            with self._pending_lock:
                un_upped = any(not e.get("up") for e in self.pending_classics)
                if not un_upped and (rapid or (self.click_q.qsize() == 0
                                               and not self.worker_active)):
                    entry = {"x": x, "y": y, "up": False, "state": None,
                             "pad": True}
                    self.pending_classics.append(entry)
                else:
                    entry = None
            if entry is not None:
                self._pad_entry = entry
                self.click_q.put(("classic", x, y, now, entry))
                return
            self._pass_note("previous click still assisted (rapid off)")
        self._pad_raw_down = True
        self._button(True)

    def _pad_release(self, x, y, now):
        entry = self._pad_entry
        self._pad_entry = None
        if entry is not None:
            with self._pending_lock:
                entry["up"] = True
            return
        if self._pad_raw_down:
            self._pad_raw_down = False
            self._button(False)
        armed = self._armed_now()
        try:
            mode = int(self.cfg.get("mode", 2))
        except (TypeError, ValueError):
            mode = 2
        was_center = self._release_holds(now)
        if mode == 2 and armed and not was_center and not self._fg_is_own():
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
        try:
            if not bool(self.cfg.get("pad_rumble", True)):
                return False
        except (TypeError, ValueError):
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
        lines = [gamepad.describe(self._pad_index, reader=self._pad_reader)]
        backend = self._pad_out
        lines.append("  output: %s" % (backend.name if backend else
                                       "not built yet"))
        lines.append("  enabled: %s | connected: %s | drive: %s"
                     % (self._pad_enabled(), self._pad_connected,
                        bool(self.cfg.get("pad_drive", True))))
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

    # Get the human detector ready in the background when it is
    # switched on, so the first scan does not pay for loading the
    # models and blow its budget.
    try:
        import hpose_bridge
        if hpose_bridge.mode_of(cfg.values) == "on":
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
        target=app.run_auto_click_worker,
        name="auto-click",
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

    if app.stop.is_set():
        print("")
        print("Snappy AI could not start. See messages above.")
        return None, None

    return app, hook_thread

