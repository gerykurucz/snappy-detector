
import colorsys
import io
import os
import base64
import threading
import time

import numpy as np

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox, simpledialog

import system as i18n
import logo
import online
import detection

import reticle
from assist import start_app
from config import DEFAULT_CONFIG, _coerce_value
from system import APP_NAME, IS_WINDOWS, log
try:
    from system import APP_VERSION
except ImportError:
    APP_VERSION = "?"
from input import MACRO_SLOTS, REC_PLAY_SLOTS
if IS_WINDOWS:
    from input import (PostThreadMessage, WM_HOTKEY, get_cursor_pos,
                         virtual_screen)
else:
    PostThreadMessage = WM_HOTKEY = get_cursor_pos = virtual_screen = None

try:
    from input import apply_window_theme
except ImportError:

    def apply_window_theme(*_args, **_kw):
        pass
    if IS_WINDOWS:
        log("win32rt.py is older than gui.py - title-bar theming disabled. "
            "Copy ALL project files, then rebuild.")

try:
    from PIL import Image as _PILImage
    from PIL import ImageDraw as _PILDraw
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

# ---- Latency tab mounting (merged from input_utils.mount/pump/shutdown) ----
# gui.py owns the latency tab lifecycle so input_utils stays free of GUI code.
POLL_MS = 80
TAB_FIX = "latency_fix"
TAB_LIVE = "latency_live"
TAB_TOOLS = "latency_tools"
LATENCY_TABS = (TAB_FIX, TAB_LIVE, TAB_TOOLS)
BRAND = "SnappyAI"


def default_out_root():
    try:
        from utils import reports_dir
        return reports_dir()
    except Exception:
        import os
        return os.path.join(os.getcwd(), "reports")


def snappy_theme(pal, fonts):
    from input import Theme
    return Theme(
        bg=pal["card"],
        raised=pal["desc_bg"],
        inset=pal["entry"],
        fg=pal["text"],
        muted=pal["muted"],
        accent=pal["accent"],
        good=pal["green"],
        warn=pal["amber"],
        bad=pal["accent"],
        border=pal["entry_bd"],
        entry_bg=pal["entry"],
        entry_fg=pal["text"],
        font=fonts["base"],
        font_bold=fonts["bold"],
        font_mono=("Consolas", 9),
        font_h1=fonts["h1"],
        font_h2=fonts["bold"],
        font_big=(fonts["base"][0], 30),
        font_entry=fonts["entry"],
    )


def mount(root, pages, kit, pal, fonts, out_root=None, backend="auto",
          brand=BRAND, cfg=None):
    from scanner import GuiController
    
    ctl = GuiController(
        backend_name=backend, out_root=out_root or default_out_root(),
        brand=brand,
    )

    on_timer_persist = None
    startup_timer_us = 0
    if cfg is not None:
        def _persist_timer(us):
            try:
                cfg.set("timer_resolution_us", int(us))
            except Exception:
                pass
        on_timer_persist = _persist_timer
        try:
            startup_timer_us = int(cfg.get("timer_resolution_us") or 0)
        except (TypeError, ValueError):
            startup_timer_us = 0

    app = SnappyAIApp(
        root,
        ctl,
        embed={
            "frames": {
                "home": pages[TAB_FIX],
                "monitor": pages[TAB_LIVE],
                "tools": pages[TAB_TOOLS],
            },
            "kit": kit,
            "pal": pal,
            "fonts": fonts,
            "on_timer_persist": on_timer_persist,
        },
    )

    if startup_timer_us > 0:
        app._timer_trace_guard = True
        try:
            app.var_timer.set(True)
        finally:
            app._timer_trace_guard = False
        ctl.start_timer_request(float(startup_timer_us))

    def _restyle():
        app.retheme(snappy_theme(kit.pal, fonts))

    kit.repaint(_restyle)
    return {"app": app, "ctl": ctl}


def pump(root, handle):
    import tkinter as tk
    try:
        if not root.winfo_exists():
            return
        handle["app"].pump_once()
    except tk.TclError:
        return
    try:
        root.after(POLL_MS, lambda: pump(root, handle))
    except tk.TclError:
        pass


def shutdown(handle):
    try:
        handle["ctl"].stop_monitor()
    except Exception:
        pass
    try:
        from system import stop_timer_request
        stop_timer_request()
    except Exception:
        pass


def hex2rgb(value):
    value = str(value).lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))

RETEXT = []

def R(fn):
    RETEXT.append(fn)
    return fn

def dispatch_ret():
    for fn in list(RETEXT):
        try:
            fn()
        except Exception:
            try:
                RETEXT.remove(fn)
            except ValueError:
                pass

def _live(w):
    try:
        return bool(w.winfo_exists())
    except (tk.TclError, RuntimeError):
        return False

def label_key(key):
    if key.startswith("macro") and key.endswith("_key"):
        return "lbl.macro_key"
    if key.startswith("macro") and key.endswith("_action"):
        return "lbl.macro_action"
    if key.startswith("key_play") and key[len("key_play"):].isdigit():
        return "lbl.key_play_n"
    return "lbl." + key

def tr_label(key):
    lk = label_key(key)
    t = i18n.L(lk)
    if t == lk:
        return META[key][0]
    if "%d" in t:
        num = key[len("key_play"):] if lk == "lbl.key_play_n" \
            else key[len("macro"):-len("_action") if lk == "lbl.macro_action"
                     else -len("_key")] if lk != "lbl.key_play_n" else ""
        try:
            return t % int(num)
        except (TypeError, ValueError):
            return t
    return t

def desc_key(key):
    if key.startswith("macro") and key.endswith(("_key", "_action")):
        return "desc.macro_" + ("key" if key.endswith("_key") else "action")
    if key.startswith("key_play") and key[len("key_play"):].isdigit():
        return "desc.key_play_n"
    return "desc." + key

def tr_desc(key):
    dk = desc_key(key)
    t = i18n.L(dk)
    if t == dk:
        try:
            return META[key][1]
        except (KeyError, IndexError):
            return dk
    if dk == "desc.key_play_n" and "%d" in t:
        t = t % int(key[len("key_play"):])
    return t

THEMES = {
    "light": dict(
        bg="#f4f5f7", card="#ffffff", text="#1b1b1b", muted="#5f6368",
        entry="#ffffff", entry_bd="#c8ccd0", accent="#c62828",
        green="#2e7d32", amber="#b26a00",
        tab_bg="#eceef1", desc_bg="#e8eaed", status_bg="#e8eaed",
        light_bg="#dfe2e6",

        switch_on="#cc2f38",
    ),
    "dark": dict(
        bg="#1e2026", card="#272a33", text="#e9eaee", muted="#a2a7b0",
        entry="#1b1d23", entry_bd="#3a3f4a", accent="#ef5350",
        green="#66bb6a", amber="#ffb74d",
        tab_bg="#17181d", desc_bg="#17181d", status_bg="#17181d",
        light_bg="#343845",

        switch_on="#e04a52",
    ),
}

SW_W, SW_H = 46, 22
SW_KNOB, SW_PAD, SW_RADIUS = 16, 3, 11

F_BASE = ("Segoe UI", 11)
F_BOLD = ("Segoe UI", 11, "bold")
F_DESC = ("Segoe UI", 10)
F_H1 = ("Segoe UI", 19, "bold")
F_BTN = ("Segoe UI", 11, "bold")
F_BTN_BIG = ("Segoe UI", 13, "bold")
F_ENTRY = ("Segoe UI", 11)
F_TAB = ("Segoe UI", 12, "bold")
F_TAB_OFF = ("Segoe UI", 11)

TOPBAR_H = 92
TOPBAR_LOGO_PX = 56

TOPBAR_TEXT_RAISE = 4

TOPBAR_TEXT_TOP = max(0, (TOPBAR_H - TOPBAR_LOGO_PX) // 2
                      - TOPBAR_TEXT_RAISE)

F_TOP_H1 = ("Segoe UI", 20, "bold")
F_TOP_LINE = ("Segoe UI", 10)

TOPBAR_BUILD_INDENT = 2

ENTITLEMENT_CHECK_MINUTES = 30

META = {

    "pad_output": ("Input device",
        "How controller recordings and 'pad:' macros leave the program: "
        "Keyboard & mouse emulation (default, works everywhere) or "
        "Controller (ViGEmBus virtual pad, needs the ViGEmBus driver). "
        "Applies on restart.",
        "map", (('Keyboard & mouse', 'emulate'),
                 ('Controller (ViGEmBus)', 'vigem'))),
    "pad_launch": ("Controller: launch",
        "Controller button that starts assistance - one button ('start') or a "
        "combination ('rb+start'). Empty = unbound.",
        "pad", None),
    "pad_pause": ("Controller: pause",
        "Controller button that pauses assistance and hands all input back to "
        "you. Empty = unbound.",
        "pad", None),
    "pad_track_target": ("Controller: track target (hold)",
        "While this button is held, scan for a person, lock on, and keep them "
        "at the exact screen centre. Ignores assist settings and modes. "
        "Release to stop.", "pad", None),
    "pad_record": ("Controller: record",
        "Starts a controller recording; also stops and saves one when "
        "'Controller: stop recording' is empty. Empty = unbound.",
        "pad", None),
    "pad_record_stop": ("Controller: stop recording",
        "Stops and saves a controller recording. Leave empty to let the "
        "record button toggle instead. Empty = unbound.",
        "pad", None),
    "pad_play": ("Controller: play recording",
        "Replays the selected controller recording, or the newest one when "
        "nothing is selected; pressing again stops a loop. Empty = unbound.",
        "pad", None),
    "hold_follow": ("Keep following while the button is held",
                    "After a snap, holding the trigger keeps the cursor on "
                    "that item's colour - steadies drags like scrollbar "
                    "thumbs. Clicks elsewhere are untouched.",
                    "bool", None),
    "rapid_click_snap": ("Assist every click in a fast burst",
                           "On: every left click is assisted, so rapid clicks "
                           "keep snapping. Off: extra clicks during one "
                           "handling pass through on the desktop; in game "
                           "Mode 1 they stay blocked so no uncentred shot "
                           "escapes.",
                           "bool", None),
    "trigger_button": ("Assist trigger button",
                       "The mouse button assistance listens to; hold it to be "
                       "guided onto the nearest clear target. In classic mode "
                       "the click is caught, snapped and pressed for you. "
                       "Default mouse1 (left).", "mouse", None),
    "options_button": ("Extra-options button",
                       "The button behind the two extra-action settings "
                       "(short press and hold); the click itself keeps its "
                       "normal function. Default mouse2 (right). It cannot "
                       "match the assist trigger.", "mouse", None),
    "shift_bypass": ("Bypass input stops assistance",
                     "While the bypass key (its controller equivalent too) is "
                     "held, every click passes through untouched - a "
                     "precision override.",
                     "bool", None),
    "bypass_key": ("Bypass key (held)",
                   "Key that stops assistance while held, when 'Bypass input "
                   "stops assistance' is on. Default Shift.",
                   "keymod", None),
    "bypass_pad": ("Controller bypass button",
                   "Controller button that stops assistance while held (needs "
                   "'Bypass input stops assistance'). Empty = unbound.",
                   "pad", None),
    "pad_click_button": ("Controller click button",
                         "Controller button that acts as the click: hold it "
                         "to be steered onto the target. Default "
                         "rt (right trigger). One button only.",
                         "padsingle", None),
    "pad_right_button": ("Controller extra-options button",
                         "Controller button behind the two extra actions. "
                         "Default lt (left trigger). One button only.",
                         "padsingle", None),
    "subject_priority": ("Subject-first targets",
                     "In a close call, prefer detected people over other "
                     "targets (scored by head-shoulder shape, taper and "
                     "motion). Off: nearest target wins.",
                     "bool", None),
    "subject_only": ("Only aim at people",
                     "Only detected people can be assisted. Non-matches pass "
                     "through on the desktop and in Mode 2; in game Mode 1 "
                     "the trigger stays blocked rather than firing off-target.",
                     "bool", None),
    "debug_visualize": ("Debug: show detections",
                        "Draw a box around every candidate the scanner "
                        "sees and a circle at the configured search "
                        "radius. Purely visual - does not affect clicks.",
                        "bool", None),
    "reticle_enabled": ("Show custom reticle",
                          "Draw your own reticle on top of everything, "
                          "games included; the overlay is click-through. "
                          "Keep it small - a big marker near the centre can "
                          "be picked up as a target itself.",
                          "bool", None),
    "reticle_shape": ("Reticle shape",
                        "Which shape to draw: cross, plus, dot, circle "
                        "or X.",
                        "map", (("Cross", "cross"), ("Plus", "plus"),
                                ("Dot", "dot"), ("Circle", "circle"),
                                ("X", "x"))),
    "reticle_size": ("Reticle size",
                       "Overall size in pixels (8-96).",
                       "int", None),
    "reticle_gap": ("Reticle gap",
                      "Empty space in the middle in pixels (0-48) - the "
                      "part you aim with stays clear.",
                      "int", None),
    "reticle_thickness": ("Reticle thickness",
                            "Line width in pixels (1-10).",
                            "int", None),
    "reticle_rotation": ("Reticle rotation",
                           "Turn the lines 0-90 degrees.",
                           "int", None),
    "reticle_opacity": ("Reticle opacity",
                          "10-100: how solid the marker looks.",
                          "int", None),
    "reticle_color": ("Reticle color",
                        "One colour as r,g,b (for example 0,255,0) or "
                        "#rrggbb. The swatches below fill this in.",
                        "str", None),
    "mouse2_action": ("Extra-options button: short press",
                      "What a short press of the extra-options button does on "
                      "top of its normal click. The follow options only steer "
                      "in mode 2; default: do nothing.",
                      "map", (('Do nothing', 4),
                               ('Clear follow task', 1),
                               ('Follow last colour', 2),
                               ('Pick colour, then follow', 3))),
    "mouse2_hold_action": ("Extra-options button: hold",
                           "The same options, fired by holding the "
                           "extra-options button for about half a second. A "
                           "press is one or the other, never both.",
                           "map", (('Do nothing', 4),
                                    ('Clear follow task', 1),
                                    ('Follow last colour', 2),
                                    ('Pick colour, then follow', 3))),
    "radius": ("Search distance (px)",
               "How many physical screen pixels around the anchor assistance "
               "looks (40-400). The anchor is the screen centre in Game Mode, "
               "the cursor otherwise. Windows display scaling does not change "
               "this value.", "int", None),
    "center_blind_area": ("Centre blindness threshold",
               "Centre-watch only: ignore tiny items sitting right on the "
               "centre anchor, e.g. a drawn reticle (900 ~= 30x30 px). "
               "0 disables.", "int", None),
    "center_max_step": ("Steering step cap (px)",
               "Largest single steering correction in pixels (0 = uncapped). "
               "Applies to Mode 1 centring and to Mode 2 while the trigger is "
               "held. Raw click warps are not capped.", "int", None),
    "subject_focus_bias": ("Subject aim bias (0-100)",
               "For tall 1:2 body-shape targets, move the snap point from "
               "the middle toward the box top by this share. 0 (default) "
               "off.", "int", None),
    "motion_sense": ("Motion sense (0-100)",
               "Centre-watch only: moving targets outrank static ones in "
               "close calls. 0 (default) off; brief flashes are ignored.",
               "int", None),
    "watcher_seconds": ("Double-click window (s)",
                        "Classic mode: a second snap near the same spot "
                        "within this time becomes a double-click on the "
                        "item.", "float", None),

    "accent_color": ("My colour(s)",
                     "The colours assistance prefers; items containing one "
                     "win ties. Pick a colour below and press 'Add to "
                     "preferred colours'; click a box to remove it.",
                     "colors", None),
    "accent_only": ("Only assist my colour",
                    "Only items containing your colour can be assisted. "
                    "Non-matches pass through on the desktop and in Mode 2; "
                    "in game Mode 1 the trigger stays blocked. Needs colours "
                    "set first.", "bool", None),
    "accent_tolerance": ("Colour match strictness",
                         "How close a colour must be to count as yours "
                         "(4-90). Lower = stricter.", "int", None),

    "accent_soft": ("Accept near-miss shades",
                    "0-100: how much lighter, darker or washed-out "
                    "versions of your colour still count in its "
                    "favour. 0 = exact shades only, and at 0 the Colour "
                    "matching engine below makes no difference - raise "
                    "this first.", "int", None),
    "accent_matcher": ("Colour matching engine",
                       "Perceptual (recommended) ranks colours the way they "
                       "look; Classic is the older engine, kept for "
                       "comparison. Only applies when 'Accept near-miss "
                       "shades' is above 0.",
                       "map", (('Perceptual (recommended)', 'oklab'),
                                ('Classic (older engine)', 'hsv'))),
    "accent_boost": ("Colour tie-break strength",
                     "How much your colour lifts an item's score "
                     "(0-3). Raise toward 1.5-2 when the wrong item "
                     "still wins.", "float", None),

    "exclude_color": ("Never-click colour(s)",
                      "Items containing these colours are never assisted, in "
                      "both modes. Pick a colour below and press 'Add to "
                      "refused colours'; click a box to remove it.",
                      "colors", None),
    "motion_3d": ("Track moving items robustly",
                   "Track measured target position and size over recent frames. "
                   "Direction changes, starts/stops and size jumps reset stale "
                   "motion instead of carrying it into the next aim.",
                   "bool", None),
    "motion_predict": ("Lead moving items",
                        "Project measured direction and speed through the "
                        "actual capture age and output delay. Recent speed "
                        "changes adjust the lead; stops, reversals and body "
                        "wobble cancel it. Still items are unaffected.",
                        "bool", None),
    "hpose_mode": ("Person detection (hpose)",
                    "On: run the heavier pose model as occasional validation "
                    "only after the fast person detector succeeds within the "
                    "scan budget. Misses do not continuously run pose. Needs "
                    "onnxruntime; models sit in the models folder.",
                   "bool", None),
    "hpose_aim": ("Aim point on the body",
                   "The middle of the body or the head; the body middle "
                   "moves less and is steadier. Any picks the centre of "
                   "whatever body part is visible.",
                   "map", (('Middle of the body', 'center'),
                              ('Head', 'head'),
                              ('Any visible part', 'any'))),
    "hpose_min_conf": ("Minimum confidence",
                       "Detector confidence required before a person is "
                       "aimed at (10-90). Higher is pickier, lower finds "
                       "people sooner.",
                       "int", None),
    "follow_enabled": ("Remember the last clicked item",
                       "After a successful snap, similar-looking items or "
                       "items in the same place win ties for a while - "
                       "handy for scrollbars and repeated clicks.",
                       "bool", None),
    "game_mode": ("Game mode",
                   "Anchor detection and explicit click/hold steering at the "
                   "screen centre. It never starts a background chase by "
                   "itself. In Mode 1 one trigger is blocked, centred from "
                   "fresh measured frames, clicked once, then finished. "
                   "Mode 2 hold rests inside a 3 px centre radius and ignores "
                   "small body-box wobble until movement exceeds 5 px.",
                   "bool", None),
    "unibot_aim": ("Unibot aim steering",
                   "Centre-watch only: steer the view with exact "
                   "relative-mouse deltas, pulling the target onto the "
                   "screen centre for click snaps and held steering (needs "
                   "the driver input backend in games). Off: steering goes "
                   "through a virtual gamepad stick when available.",
                   "bool", None),
    "aim_speed": ("Steering gain (X)",
                  "Mode 2 hold only: maximum camera turn speed on the X axis "
                  "as screen pixels per 60 Hz frame at gain 1.0 (0.1-2.0). "
                  "It limits speed, it never multiplies the measured error, "
                  "so a setting cannot overshoot a stationary target. Single "
                  "clicks and Mode 1 centring are unchanged.",
                  "float", None),
    "aim_y_speed_multiplier": ("Steering gain (Y)",
                  "Mode 2 hold only: extra X-axis multiplier applied to the Y "
                  "speed budget (0.1-2.0). Single clicks are unaffected.",
                  "float", None),
    "ads_steer_multiplier": ("ADS response compensation",
                  "ADS zoom changes learned view-pixels-per-count; this is "
                  "used only to scale the response estimate, never to multiply "
                  "the held-target correction fraction (0-500%).",
                  "float", None),
    "motion_camera_comp": ("Camera motion compensation",
                  "Subtract estimated camera pan from motion lead so steering "
                  "stays relative to the scene. Requires motion_predict or "
                  "motion_3d and a reticle. Default on.",
                  "bool", None),
    "follow_learn": ("What the memory may learn",
                     "Only items in my colour: a misclick that snapped "
                     "somewhere else cannot retarget the memory. Every "
                     "successful snap: the old, trusting behaviour.",
                     "map", (("Only items in my colour", "accent"),
                             ("Every successful snap", "any"))),
    "follow_boost": ("Memory strength",
                     "How strongly the remembered item wins ties: "
                     "100% can double its score while fresh, 25% is a "
                     "light nudge.",
                     "map", (("Off", 0.0), ("25%", 0.25), ("50%", 0.5),
                             ("75%", 0.75), ("100%", 1.0), ("150%", 1.5),
                             ("200%", 2.0), ("300%", 3.0))),
    "follow_seconds": ("Memory retention (s)",
                       "Seconds the remembered item stays preferred after "
                       "your last successful snap (1-120).",
                       "int", None),
    "merge_distance": ("Merge nearby blobs (px)",
                       "Pieces closer than this many pixels (a broken "
                       "outline, an icon beside its label) become one item; "
                       "two complete items are never merged.",
                       "int", None),
    "key_launch": ("Shortcut: start assisting",
                   "Format: ctrl+l, f5, space, ctrl+alt+1 ... "
                   "Modifiers: ctrl, alt, shift, win. Applied on "
                   "Apply.", "str", None),
    "key_pause": ("Shortcut: pause",
                  "Your mouse and keyboard become entirely yours "
                  "again until you launch.", "str", None),
    "key_track_target": ("Shortcut: track target (hold)",
                  "While this key is held, scan for a person, lock on, and "
                  "keep them at the exact screen centre. Ignores assist "
                  "settings and modes. Release to stop.", "str", None),
    "key_record": ("Shortcut: start a recording",
                   "Captures everything you do - every key, every "
                   "mouse click and every cursor move - while your "
                   "input still reaches your programs, until the stop "
                   "shortcut saves it into the keyboard Recordings list. "
                   "Arms only while launched.", "str", None),
    "key_record_stop": ("Shortcut: stop and save the recording",
                        "Stops capturing, saves the sequence into the "
                        "keyboard Recordings list, and halts a running "
                        "playback.",
                        "str", None),
    "key_play": ("Shortcut: play the selected recording",
                 "Replays the keyboard recording selected on Keyboard & Mouse, "
                 "or the newest one when nothing is selected. Pressed on "
                 "a repeating recording, it stops it.", "str", None),
    "capture_backend": ("Screen capture engine",
                        "How the screen is photographed. Automatic always "
                        "works (RDP, VMs); Desktop Duplication and Windows "
                        "Graphics Capture are faster opt-in picks - some "
                        "drivers and protected-content apps answer them "
                        "with black frames. Applies on restart.",
                        "map", (('Automatic (compatible)', 'auto'),
                                 ('Desktop Duplication', 'dxcam'),
                                 ('Compatible (RDP, VMs)', 'mss'),
                                 ('Windows Graphics Capture', 'wgc'))),

    "input_backend": ("Click delivery method",
                      "Standard Windows clicks work everywhere. Driver "
                      "level (Interception) sends real kernel-driver input - "
                      "what in-game view steering needs and what apps that "
                      "ignore synthetic clicks respond to. Install the "
                      "driver once with interception.exe install, then "
                      "reboot. Applies on restart.",
                      "map", (('Standard Windows', 'win32'),
                               ('Driver level (kernel)', 'driver'))),

    "adaptive_threshold": ("Auto-adjust for dim screens",
                           "The detection bar lowers itself for dark themes "
                           "and washed-out content, so faint items are "
                           "still found. Recommended on.",
                           "bool", None),
    "luminance_norm": ("Judge edges by relative brightness",
                       "Edges are scored against how bright their "
                       "surroundings are, so dark-mode items rank like "
                       "light-mode ones. Recommended on.",
                       "bool", None),
    "color_adapt": ("Adapt to screen lighting",
                    "Each item is white-balanced against its own "
                    "background before matching, so night light and "
                    "theme casts stop shifting your colour out of "
                    "tolerance. Turn off only to compare against the "
                    "unadapted matcher.",
                    "bool", None),
    "accent_variants": ("Catch bright and dim states of my colour",
                        "Brighter and dimmer copies of your colour match "
                        "too, so hover, pressed and dimmed states of a "
                        "control count without widening the hue "
                        "tolerance. Lightness only - a wrong hue can "
                        "never ride it.",
                        "bool", None),
    "accent_coherent": ("Ignore scattered colour specks",
                        "Your colour must form a connected blob to count "
                        "- scattered dither or compression specks never "
                        "qualify an item, boost it, or put it on the "
                        "never-click list.",
                        "bool", None),

    "scan_budget_ms": ("Scan budget (ms)",
                       "Milliseconds one scan of the search area may take "
                       "before it gives up and lets the click through "
                       "untouched (60-2000). The budget is checked between "
                       "stages, so a very busy frame can overrun slightly.",
                       "int", None),

}

for _i in range(1, MACRO_SLOTS + 1):
    META["macro%d_key" % _i] = (
        "Macro %d: press this key" % _i,
        "The key that MEANS the macro instead of typing: 'b', 'f7', "
        "'ctrl+b' ... While launched, pressing it sends the combo "
        "beside it exactly once and the key itself never reaches your "
        "programs. Paused, it types normally. Empty = macro off.",
        "str", None)
    META["macro%d_action" % _i] = (
        "Macro %d: sends this instead" % _i,
        "The key combo pressed for you on each macro-key press (one "
        "press = one combo press): 'ctrl+c', 'ctrl+shift+s', 'space', "
        "'f5' ... Empty = macro off.",
        "str", None)

for _i in range(1, REC_PLAY_SLOTS + 1):
    META["pad_play%d" % _i] = (
        "Controller: play recording #%d" % _i,
        "Replays controller recording #%d of the numbered Controller "
        "Recordings list on the Controller tab from a "
        "controller button, by its position in that list. If the list is "
        "shorter than that, nothing happens. Empty = unbound." % _i,
        "pad", None)

for _i in range(1, MACRO_SLOTS + 1):
    META["pad_macro%d_key" % _i] = (
        "Pad macro %d: press this" % _i,
        "The controller button or combination that MEANS the macro: 'a', "
        "'rb+y', 'ls+rs' ... While launched, pressing it fires the output "
        "beside it exactly once and nothing reaches your programs. A "
        "combination that contains a shorter bound combination wins, so "
        "'rb+a' never also fires 'a'. Empty = macro off.",
        "pad", None)
    META["pad_macro%d_action" % _i] = (
        "Pad macro %d: sends this" % _i,
        "What the controller macro fires: a key combo ('ctrl+c', 'f5', "
        "'shift+tab') which works everywhere, or a controller combo written "
        "as 'pad:a+b', which needs the ViGEmBus virtual pad and is delivered "
        "through the built-in keyboard/mouse mapping without it. Empty = "
        "macro off.",
        "str", None)

for _i in range(1, REC_PLAY_SLOTS + 1):
    META["key_play%d" % _i] = (
        "Shortcut: play recording #%d" % _i,
        "Replays keyboard recording #%d of the numbered Recordings list in "
        "Keyboard & Mouse, "
        "by its position in that list. If the list is shorter than "
        "that, nothing happens." % _i,
        "str", None)

TAB_ASSIST = ["shift_bypass", "subject_priority", "subject_only",
              "hold_follow", "rapid_click_snap",
              "follow_enabled", "motion_predict", "motion_3d",
              "unibot_aim", "accent_only",
              "hpose_mode", "game_mode", "motion_camera_comp"]
TAB_DETECT = ["accent_color", "exclude_color",
              "adaptive_threshold", "luminance_norm",
              "color_adapt", "accent_variants", "accent_coherent",
              "debug_visualize", "merge_distance"]
TAB_RETICLE = ["reticle_enabled", "reticle_shape",
                  "reticle_size", "reticle_gap", "reticle_thickness",
                  "reticle_rotation", "reticle_opacity", "reticle_color"]
TAB_KEYS = ["key_launch", "key_pause", "key_track_target",
            "key_record", "key_record_stop", "key_play", "key_play1",
            "key_play2", "key_play3", "key_play4", "key_play5"]
TAB_NAMES = ["assist", "handler", "keyboard",
             "controller", "spoofer", "latency_fix",
             "latency_live", "latency_tools", "help"]

TAB_PAD = ["pad_launch", "pad_pause", "pad_track_target",
           "pad_record", "pad_record_stop", "pad_play",
           "pad_play1", "pad_play2", "pad_play3", "pad_play4", "pad_play5"]
TAB_MOUSE = ["trigger_button", "options_button"]
TAB_KEYBOARD = ["trigger_button", "options_button", "bypass_key",
                "key_launch", "key_pause", "key_track_target",
                "key_record", "key_record_stop", "key_play", "key_play1",
                "key_play2", "key_play3", "key_play4", "key_play5"]
TAB_CONTROLLER = ["pad_click_button", "pad_right_button", "bypass_pad",
                  "pad_launch", "pad_pause", "pad_track_target",
                  "pad_record", "pad_record_stop", "pad_play",
                  "pad_play1", "pad_play2", "pad_play3", "pad_play4",
                  "pad_play5"]
TAB_COLOR = ["accent_tolerance", "accent_soft", "accent_boost"]
TAB_ADV = ["radius", "center_blind_area", "center_max_step", "aim_speed", "aim_y_speed_multiplier", "ads_steer_multiplier", "subject_focus_bias", "motion_sense", "watcher_seconds",
           "follow_seconds", "scan_budget_ms",
           "hpose_min_conf",
]

TAB_EXPERIMENTAL = [k for k in META if META[k][2] == "map" and k not in TAB_RETICLE]

_CAPTURE_KIND = {}
for _k in TAB_KEYS:
    _CAPTURE_KIND[_k] = "key"
for _k in TAB_MOUSE:
    _CAPTURE_KIND[_k] = "mouse"
for _k in TAB_PAD:
    _CAPTURE_KIND[_k] = "pad"
_CAPTURE_KIND["bypass_key"] = "keymod"
_CAPTURE_KIND["bypass_pad"] = "pad"
_CAPTURE_KIND["pad_click_button"] = "padsingle"
_CAPTURE_KIND["pad_right_button"] = "padsingle"
for _i in range(1, MACRO_SLOTS + 1):
    _CAPTURE_KIND["macro%d_key" % _i] = "combo"
    _CAPTURE_KIND["macro%d_action" % _i] = "seq"
    _CAPTURE_KIND["pad_macro%d_key" % _i] = "pad"
    _CAPTURE_KIND["pad_macro%d_action" % _i] = "padkey"
for _k, _kind in _CAPTURE_KIND.items():
    if _k in META:
        META[_k] = (META[_k][0], META[_k][1], _kind, META[_k][3])

def val_slug(value):
    return str(value).replace(".", "_")

def val_key(key, value):
    return "val.%s.%s" % (key, val_slug(value))

def map_pairs(key):
    return list(META[key][3])

def map_pairs_live(key):
    out = []
    for disp, val in META[key][3]:
        t = i18n.L(val_key(key, val))
        out.append((t if t != val_key(key, val) else disp, val))
    return out

def map_display(key, value):
    pairs = map_pairs_live(key)
    for disp, val in pairs:
        if str(val) == str(value):
            return disp
    try:
        num = float(value)
    except (TypeError, ValueError):
        return pairs[0][0]
    try:
        return min(pairs, key=lambda p: abs(float(p[1]) - num))[0]
    except (TypeError, ValueError):
        return pairs[0][0]

def map_value(key, display):
    for pairs in (map_pairs_live(key), map_pairs(key)):
        for disp, val in pairs:
            if disp == display:
                return val
    return None

PROSE = {
    "top.mode1": "MODE 1 - CLASSIC (clicks assisted)",
    "top.mode2": "MODE 2 - GUIDE (clicks pass through)",
    "desc.default": "Hover over a setting name to see what it does.",

    "col.rgb.name": "Current RGB Value",
    "col.rgb.desc":
        "Three boxes showing the colour sitting in the picker right "
        "now: the first glows with how much red it holds, the second "
        "with green, the third with blue - brighter means more. The "
        "row follows the picker, the preset swatches and the screen "
        "buttons; it cannot be typed into.",
    "help.title": "Tuning Snappy AI in plain words",
    "help.sub":
        "Every setting by the name you see on its tab, in the order "
        "you will want it. Hover a setting name for its one-line "
        "description; this page is the long form.",
    "help.tail":
        "Settings apply when you press Apply. A config.ini beside the "
        "program overrides config.json. This page explains; it does "
        "not configure.",
    "help.1.t": "What happens when you click",
    "help.1.b":
        "Snappy AI looks around the cursor (or the screen centre in "
        "centre-watch mode), finds the "
        "solid item-shaped things near it - buttons, icons, links - "
        "and picks the best one: crisp edges, a decent size and close "
        "to your cursor win. Your colour, the never-click list and the "
        "memory of your last click can tilt the choice. The cursor is "
        "then placed at the middle of the item, not at the corner of "
        "its box.",
    "help.2.t": "Start here: mode and search distance",
    "help.2.b":
        "Guide mode (the default) never eats a click: your click "
        "reaches the program and, after you release, the cursor moves "
        "onto the nearest clear item. Classic mode is stronger: a "
        "mis-click near an item is caught, the cursor snaps onto the "
        "item and Snappy AI clicks it for you. In game Mode 1, the shot "
        "fires only after a fresh frame confirms the target on the exact "
        "centre pixel; otherwise the trigger stays blocked. 'Search distance "
        "(px)' "
        "decides the search area, anchored on the screen centre in "
        "centre-watch mode - raise it when the item you want is just "
        "out of reach, lower it when the wrong neighbour wins. Holding the "
        "bypass key (Keyboard & Mouse tab, Shift by "
        "default) lets one click through untouched in both modes, and which "
        "button assistance listens to at all is yours to move on the "
        "Keyboard & Mouse tab.",
    "help.3.t": "Nothing is found - or too much is",
    "help.3.b":
        "Raise 'Search distance (px)' when the target is just out of reach, "
        "or lower it when too many neighbours compete. 'Merge nearby blobs "
        "(px)' joins a broken outline, or an icon beside its label, into one "
        "item. 'Auto-adjust for dim screens' and 'Judge edges by relative "
        "brightness' make dark themes behave like light ones; 'Scan budget "
        "(ms)' gives difficult frames more time. Use the colour and "
        "people-only controls when you need stricter filtering.",
    "help.4.t": "Working with colours",
    "help.4.b":
        "On the Colors tab, 'My colour(s)' is the colour Snappy AI "
        "prefers and 'Never-click colour(s)' is absolute: items with "
        "that colour are never assisted. 'Only assist my colour' "
        "restricts assistance to matching items. On the Detection tab, "
        "'Colour match strictness' says how close a shade must be, "
        "'Accept near-miss shades' lets lighter or washed-out versions "
        "count partly, and 'Colour tie-break strength' says how much "
        "your colour lifts an item - raise it when the wrong item "
        "still wins.",
    "help.5.t": "Repeated clicks on one control",
    "help.5.b":
        "'Remember the last clicked item' keeps preferring the item "
        "your last successful click landed on, for 'How long to "
        "remember' seconds - this is what makes scrollbars and dialog "
        "buttons pleasant. 'Memory strength' says how strongly it "
        "wins, and 'What the memory may learn' protects it: with 'Only "
        "items in my colour', a misclick cannot retarget it. 'Keep "
        "following while the button is held' steadies drags, and the "
        "right-button actions on the Experimental tab can follow or "
        "clear the memory by mouse.",
    "help.6.t": "Clicking moving things",
    "help.6.b":
        "'Lead moving items' measures how fast the chosen item travels "
        "and moves ahead automatically based on the item's speed - no "
        "user setting needed. The lead caps at 80 pixels and switches "
        "itself off for still or turning items.",
    "help.7.t": "How clicks feel",
    "help.7.b":
        "'Double-click window (s)' turns a second snap near the "
        "same spot into a double-click in classic mode. 'Assist every "
        "click in a fast burst' assists every click of a rapid series "
        "instead of only the first.",
    "help.8.t": "Keys, macros and recordings",
    "help.8.b":
        "The Keybindings tab explains the two modes and the recording "
        "workflow. In short: shortcuts with Ctrl, Alt, Shift or Win "
        "are reserved system-wide; bare single keys fire everywhere "
        "and still type. A macro turns one blocked key press into one "
        "whole combo. Recordings capture your real input - every key, "
        "mouse click and cursor move, or every controller button and "
        "stick move - and replay it on the corresponding input tabs, which keep a "
        "keyboard library and an independent controller library side by "
        "side, each with its own play-once or repeat-until-stopped "
        "switch.",
    "help.9.t": "Interface",
    "help.9.b":
        "Use the light/dark switch in the title bar to change the interface "
        "appearance. All editable assistance settings are shown in the tabs; "
        "there are no additional hidden tuning settings in config.ini.",
    "help.10.t": "When it misbehaves, check in this order",
    "help.10.b":
        "1. Is it launched? Paused means nothing is intercepted - "
        "'Shortcut: start assisting' (Ctrl+L by default) is the only "
        "on/off.  2. Hold Shift for one click to confirm the "
        "assistance was the problem.  3. Wrong item wins: lower "
        "'Search distance (px)', then set 'My colour(s)' or "
        "raise 'Colour tie-break strength'.  4. Nothing is found: "
        "raise 'Merge nearby blobs (px)' to join broken outlines, "
        "or lower 'Search distance (px)'.  5. One thing must never "
        "be assisted: put "
        "its colour in 'Never-click colour(s)'.  6. Still wrong: tell "
        "support what you clicked and what you expected instead, and "
        "include the log file Snappy AI writes to its data folder.",

    "login.title": "%s - Sign in",
    "login.sub": "Sign in with Discord - your subscription is bound to "
                 "this account, and payments must use its e-mail "
                 "address. No password here.",
    "login.note": "The sign-in opens your browser and comes back to "
                  "this window on its own.",
    "login.provider": "Sign in with %s",
    "login.waiting": "Waiting for the browser - finish the %s sign-in...",
    "login.signedin": "Signed in as %s.",
    "login.failed": "%s sign-in did not complete - try again.",
    "login.unreachable": "Auth server unreachable: %s",

    "gate.title": "%s - Subscription",
    "gate.prices": "2-day trial: 1.99\u20ac (one time)  -  subscription: "
                   "9.99\u20ac / month.\nActivation lands up to ~10 "
                   "minutes after payment; renewals re-enable this same "
                   "login automatically.",
    "gate.btn.monthly": "Monthly Subscription for 9.99\u20ac",
    "gate.btn.trial": "Trial Purchase for 1.99\u20ac",

    "gate.note": "The engine starts as soon as the subscription is "
                 "active - this window re-checks by itself every 30 "
                 "seconds. Closing it returns to sign-in.",
    "gate.title.shared":
        "This account is in use on ANOTHER computer right now. Snappy "
        "AI stays off while the same Discord account runs it in two "
        "places at once - close the other session, then re-check. "
        "Sharing subscriptions is what keeps prices up; please don't.",
    "gate.title.refunded":
        "This purchase was refunded or charged back, so the entitlement "
        "was revoked. A new subscription on this Discord e-mail "
        "re-activates it.",
    "gate.title.canceling":
        "Your subscription is set to cancel - it stays active until "
        "%s, then this login locks again unless you renew.",
    "gate.title.expired":
        "Your subscription for this login expired - renew it on Stripe "
        "to continue.",
    "gate.title.past_due":
        "The last subscription payment failed - fix it on Stripe to "
        "re-enable this login.",
    "gate.title.canceled":
        "This subscription was canceled - start a new one on Stripe to "
        "continue.",
    "gate.title.none":
        "You are signed in, but this login has no active subscription. "
        "Pay with THIS Discord e-mail - a purchase on any other address "
        "cannot activate it.",
    "gate.msg.checking": "Checking with the server...",
    "gate.msg.open_ok": "Stripe is open in your browser - pay there. "
                        "This window notices the payment by itself and "
                        "starts the engine.",
    "gate.msg.open_fail": "Could not open a browser. Open this link by "
                          "hand:\n%s",
    "gate.msg.no_link": "That payment link is not configured on this "
                        "build.",

    "gate.msg.unreachable": "Auth server unreachable: %s",
    "gate.msg.session_gone": "Session gone - sign in again.",
    "gate.msg.active": "Subscription active - starting Snappy AI...",
    "gate.msg.still_shared": "Still in use on another computer - close "
                             "that session, then re-check.",
    "gate.msg.still_none": "Still no active subscription for this "
                           "login.",

    "shared.title": "Account in use elsewhere",
    "shared.body":
        "This Discord account is running Snappy AI on another computer "
        "right now.\nAssistance is paused here until only one session "
        "remains.\n\nOne subscription, one machine at a time - that "
        "is what keeps\nthe price down and the development going.",
    "shared.recheck": "Re-check now",
    "shared.close": "Close",
    "shared.back": "Welcome back - press Launch when ready.",
    "shared.still": "Still in use elsewhere - staying paused.",
    "shared.unreachable": "Server unreachable - will re-check.",
    "theme.light": "Light",
    "theme.dark": "Dark",
    "hdr.build": "build %s",
    "st.registered": "Registered to: %s    Status: %s",
    "mac.row": "Macro key #%d",
    "err.auth_unreachable": "The Snappy AI auth server is "
                            "unreachable:\n%s\n\nSnappy AI only runs "
                            "with a live answer from it.\nRetry?",
    "err.start_fail": "Snappy AI could not start.\nRun with --console "
                      "to see details.",

    "st.engine_dead": "Engine thread unavailable - hotkeys will not "
                      "respond.",
    "st.ini_over": "Applied, but config.ini overrides: %s",
    "st.saved_bad": "Saved, but invalid (old kept): %s",
    "st.saved": "Saved and applied.",
    "st.defaults_restored": "Defaults restored.",

    "st.exported": "Settings exported to %s",
    "st.export_fail": "Export failed: %r",
    "st.select_rec": "Select a recording first.",
    "st.name_empty": "The name must not be empty.",
    "st.renamed": "Recording renamed.",
    "st.deleted": "Recording deleted.",
    "st.lib_empty": "Library is empty - nothing to export.",
    "st.rec_now": "%r now %s.",
    "st.rec_repeats": "repeats until stopped",
    "st.rec_once": "plays once",
    "st.rec_recording": "RECORDING - press %s to stop and save",
    "st.rec_playing_loop": "Playing %r until stopped",
    "st.rec_playing": "Playing %r...",
    "st.rec_count": "%d of 5 recordings",
    "st.pick_wait": "Move the cursor onto the colour - capturing in "
                    "3 s...",
    "st.pick_ok": "Picked rgb(%d, %d, %d) at (%d, %d).",
    "st.pick_fail": "Capture failed - try again.",
    "st.pick_err": "Pick failed: %r",
    "st.sug_wait": "Point at the app you work in - sampling in 3 s...",
    "st.sug_none": "No colour suggestions from this region.",
    "st.sug_ok": "%d suggestions - click one to load it into the "
                 "picker.",
    "st.sug_fail": "Suggest failed: %r",
    "st.rgb_bad": "Enter valid R/G/B values first.",
    "st.sug_loaded": "Suggestion rgb(%d, %d, %d) is in the picker - "
                     "add it to a list with the buttons above.",
    "st.sug_loaded_desc": "Suggested UI colour rgb(%d, %d, %d) - %d%% "
                          "of the sampled region. Click to load it "
                          "into the picker.",
    "st.preset_desc": "Preset swatch: %s - click to load it into the "
                      "picker.",
    "st.loop_desc": "This recording's playback style: off = play once; "
                    "on = repeat until you press the play shortcut "
                    "again. Each recording has its own switch, saved "
                    "with the recording itself.",
    "st.delete_confirm": "Delete recording %r?\nThis cannot be undone.",
    "st.rename_prompt": "New name for recording #%d:",
    "st.reset_confirm": "Reset every setting to its default?\nAccounts "
                        "are kept; this only affects settings.",
    "st.export_title": "Export settings",
    "st.import_title": "Import settings",
    "st.export_rec_title": "Export recording",
    "st.import_rec_title": "Import recording(s)",
    "st.file_settings": "Snappy AI settings",
    "st.file_rec": "Snappy AI recording",
    "st.all_files": "All files",
    "st.shared_paused": "ACCOUNT IN USE ELSEWHERE - assistance paused; "
                        "one subscription = one machine at a time.",
    "st.ent_paused": "Subscription %s - assistance paused. Re-subscribe "
                     "on Stripe, then restart.",
    "st.ent_unreachable": "Entitlement re-check: server unreachable "
                          "(session keeps running).",
    "login.not_configured": "The subscription file is not configured on "
                            "this build yet.",
    "login.no_provider": "No sign-in provider is built into this copy.",

    "btn.launch": "Launch",
    "btn.pause": "Pause",
    "btn.logout": "Log out",
    "btn.apply": "Apply",
    "btn.restore": "Restore defaults",
    "btn.import": "Import",
    "btn.export": "Export",
    "word.on": "On",
    "word.off": "Off",
    "col.accent": "My colour(s)",
    "col.exclude": "Never-click colour(s)",
    "col.chip_off": "%s is off - nothing is filtered by colour.",
    "col.chip_desc": "rgb(%d, %d, %d) in %s - click the chip to remove it.",
    "col.pick": "Pick from screen",
    "col.suggest": "Suggest colours",
    "col.add_pref": "Add to my colours",
    "col.add_ref": "Add to never-click",
    "mac.empty": "No recordings yet - press the record shortcut to capture "
                 "one, or import a library.",
    "mac.loop": "Repeat",
    "mac.steps": "%d steps",
    "st.running": "Running",
    "st.standby": "Standby",
    "st.follow": " - following for %ds",
    "st.line": "Snapped %d clicks, passed %d through%s  |  %s theme",
    "st.color_added": "Added rgb(%d, %d, %d) to %s.",
    "st.color_already": "rgb(%d, %d, %d) is already in %s.",
    "st.color_full": "%s is full - remove a colour before adding another.",
    "st.color_removed": "Removed rgb(%d, %d, %d) from %s.",

    "pad.head": "Controller",
    "pad.note": "Controller bindings, one per action. Type a button name or "
                "a combination ('start', 'rb+a', 'ls+rs'), or click the "
                "value box and press the controller button. A combination that "
                "contains a shorter bound combination wins, so binding "
                "'rb+a' never also fires 'a'. Leave a box empty to unbind "
                "that action. A connected XInput controller is detected "
                "automatically. Bindings work while launched; paused means "
                "the controller is entirely yours.",
    "cap.btn": "Press",
    "pad.cap_title": "Controller button",
    "pad.cap_head": "Press the controller button - or hold several at once - "
                    "then let go.",
    "pad.cap_waiting": "Waiting for a controller button...",
    "pad.cap_now": "Holding %s - release to set it.",
    "pad.cap_nopad": "No controller detected. Plug one in and press a button, "
                     "or close this and type the name into the box instead.",
    "pad.cap_noxinput": "This PC has no XInput runtime, so no controller can "
                        "be read here. Type the button name into the box "
                        "instead, or install the DirectX end-user runtime.",
    "pad.cap_timeout": "Nothing was pressed in time.",
    "cap.cancel": "Cancel",
    "pad.cap_names": "Names: a b x y - lb rb - lt rt - back start - ls rs - "
                      "up down left right. Combine them with +.",
    "cap.set": "%s is now %s. Press Apply to make it live.",
    "cap.kept": "%s keeps its previous binding.",
    "cap.none": "unbound",
    "mac.pad_head": "Controller Macros",
    "mac.pad_row": "Macro button #%d",
    "tab.assist": "Assistance",
    "tab.reticle": "Reticle",
    "tab.spoofer": "Spoofer",
    "xh.set_cursor": "Set to cursor",
    "xh.save": "Save",
    "xh.delete": "Delete",
    "xh.preset_name": "Preset name",
    "xh.preset": "Preset",
    "xh.swatches": "Quick colours",
    "tab.macros": "Macros",
    "tab.handler": "Screen",
    "tab.advanced": "Advanced",
    "tab.experimental": "Experimental",
    "tab.help": "Help",
    "mac.keys_head": "Keyboard Macros",
    "mac.recordings": "Recordings",
    "mac.pad_recordings": "Controller Recordings",
    "mac.sends": "presses",
    "mac.rename": "Rename",
    "mac.edit": "Edit",
    "mac.export": "Export",
    "mac.import": "Import",
    "mac.delete": "Delete",
    "st.rec_reloaded": "Recording file changed on disk - list reloaded.",
    "st.rec_bad": "Recording file edit could not be loaded (invalid JSON?).",
    "st.rec_edit_none": "No recording library file to open.",
    "st.rec_edit_fail": "Could not open the recording file (%s).",
    "tab.keyboard": "Keyboard & Mouse",
    "tab.controller": "Controller",
    "tab.latency_fix": "Fix my PC",
    "tab.latency_live": "Live latency",
    "tab.latency_tools": "Latency tools",
    "tab.recordings": "Recordings",
    "key.cap_title": "Keyboard shortcut",
    "key.cap_head": "Press the key you want - or hold Ctrl / Alt / Shift and "
                    "press a key for a combination.",
    "key.cap_waiting": "Waiting for a key...",
    "key.cap_mod_note": "Escape cancels. Release the key or combination to "
                        "bind it. A modifier on its own works here - this is "
                        "a hold, not a shortcut.",
    "mouse.cap_title": "Choose a mouse button",
    "mouse.cap_head": "Press the mouse button this role should use.",
    "mouse.cap_current": "Now: %s",
    "mouse.cap_timeout": "Nothing was pressed in time.",
    "mouse.cap_note": "Escape cancels. mouse1 is the left button, mouse2 the "
                      "right, mouse3 the middle (wheel) button, mouse4 and "
                      "mouse5 the two side buttons. A role cannot be "
                      "unbound - something has to click.",
    "pad.cap_single_head": "Press ONE controller button.",
    "head.mouse_roles": "Mouse buttons: what assistance listens to",
    "head.key_shortcuts": "Keyboard shortcuts",
    "head.pad_actions": "Controller actions",
    "head.pad_roles": "Controller buttons: click, extra options, bypass",
    "key.cap_holding": "Holding %s - now press the key.",
    "key.cap_unbound": "'%s' cannot be bound here.",
    "key.cap_invalid": "'%s' is not a valid shortcut.",
    "key.cap_timeout": "Nothing was pressed in time.",
    "key.cap_head_multi": "Press up to 5 keys together - modifiers and keys, "
                          "in any order. Release everything to set the "
                          "combination. Escape cancels.",
    "key.cap_holding_multi": "Holding %s - release everything to set the "
                             "combination.",
    "key.cap_max": "A combination can hold at most %d keys.",
    "key.cap_note": "Escape cancels. Mouse buttons cannot be bound - they are "
                    "what the assistance works on.",
    "key.cap_pad": "Escape cancels. Press a key for a keyboard combo, or a "
                   "controller button for a controller combo (stored as "
                   "'pad:a+b').",
    "key.cap_pad_now": "Controller: holding %s - release to set it.",
    "help.11.t": "Using a controller",
    "help.11.b":
        "A connected gamepad automatically becomes a full second input "
        "device, read through Windows' own XInput - no driver, no extra "
        "software. The left stick steers the cursor, the right trigger "
        "is the press that assistance snaps from, the left trigger is a right "
        "click, and the right stick steers the view while that press is held, "
        "so the snap, guide and follow behaviour is the same code "
        "a mouse user gets. Start launches and Back pauses out of the box; "
        "every action can be re-bound on the Controller tab, including to a "
        "combination, and a combination always wins over the shorter one it "
        "contains - binding 'rb+a' never also fires 'a'. Keyboard recordings "
        "save into the Recordings list and controller recordings into the "
        "independent Controller Recordings list, and each side plays back "
        "from its own list in order. Replaying controller output as a REAL "
        "virtual gamepad needs the optional ViGEmBus driver plus its "
        "ViGEmClient.dll next to the program; without them, output falls back "
        "to the built-in keyboard and mouse mapping. "
        "Which button clicks and which one carries the extra actions is on "
        "this tab, together with the controller bypass and output choice.",
    "help.12.t": "Telling a person from a picture of a person",
    "help.12.b":
        "The scanner normally works from edges and shapes: it finds "
        "things that look like buttons, icons and people. Switch on "
        "'Person detection (hpose)' and a neural detector takes over "
        "for people. "
        "It draws a real body, fits it to the picture and works out "
        "which way the person is facing - towards the screen, turned "
        "away, or in profile - and what they are doing: standing, "
        "walking, crouching or lying down. Those answers can be used as "
        "conditions: 'only aim at people facing the screen', 'only aim "
        "at crouching people'. A moving person is measured and led, so "
        "the aim lands where they are going rather than where they "
        "were. When the detector finds nobody, the ordinary scanner "
        "takes over, so nothing is lost by leaving it on. Models live "
        "in the models folder and onnxruntime is required; without it "
        "the ordinary scanner runs as before.",
}

i18n.publish("en", PROSE)

i18n.publish("en", {"desc." + k: META[k][1] for k in META
                    if desc_key(k) == "desc." + k})

_VALS = {}
for _k in META:
    if META[_k][2] == "map":
        for _d, _v in META[_k][3]:
            _VALS[val_key(_k, _v)] = _d
i18n.publish("en", _VALS)

i18n.publish("en", {"lbl.macro_key": "Macro %d: press this key",
                     "lbl.macro_action": "Macro %d: sends this instead",
                     "lbl.key_play_n": "Shortcut: play recording #%d",
                     "desc.macro_key": META["macro1_key"][1],
                     "desc.macro_action": META["macro1_action"][1],
                     "desc.key_play_n": META["key_play1"][1]
                     .replace("#1", "#%d")})

DESC_DEFAULT = "Hover over a setting name to see what it does."

COLOR_PRESETS = [
    ("red", (229, 57, 53)), ("orange", (255, 152, 0)),
    ("yellow", (253, 216, 53)), ("lime", (192, 202, 51)),
    ("green", (67, 160, 71)), ("teal", (0, 137, 123)),
    ("cyan", (0, 183, 199)), ("blue", (30, 136, 229)),
    ("indigo", (57, 73, 171)), ("purple", (142, 36, 170)),
    ("pink", (216, 27, 96)), ("brown", (121, 85, 72)),
    ("grey", (158, 158, 158)), ("white", (245, 245, 245)),
    ("black", (33, 33, 33)),
]

def _surf_of_global(widget, fallback="#ffffff"):
    try:
        parent = widget.nametowidget(widget.winfo_parent())
        return str(parent.cget("bg"))
    except Exception:
        return fallback

def _surf_of(widget, fallback="#ffffff"):
    return _surf_of_global(widget, fallback)

HELP_KEYS = [("help.%d.t" % n, "help.%d.b" % n) for n in range(1, 12)]

CHOICE_WIDTH = 26

PAD_CAPTURE_SECONDS = 8.0
PAD_CAPTURE_W = 84

SLIM_SCB_W = 10
DD_H = 26
DD_ROW_H = 30
SLIDE_W = 176
SLIDE_H = 24
VAL_W = 8

def _thumb_hex(kit, active):
    pal = kit.pal
    return pal["accent"] if active else pal["muted"]

class _SlimScrollbar(tk.Canvas):

    def __init__(self, master, kit, cvs, wheel=None):
        super().__init__(master, width=SLIM_SCB_W, highlightthickness=0,
                         takefocus=0, bd=0, bg=kit.pal["card"])
        self._kit = kit
        self._cvs = cvs
        self._wheel_cb = wheel
        self._first = 0.0
        self._last = 1.0
        self._hot = False
        self._drag = None
        self._imgs = []
        kit.reg(self, "canvas_card")
        kit.repaint(self._paint)
        self.bind("<Enter>", lambda e: self._sethot(True))
        self.bind("<Leave>", lambda e: self._sethot(False))
        self.bind("<Configure>", lambda e: self._paint())
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag_to)
        self.bind("<ButtonRelease-1>", self._drop)
        self.bind("<MouseWheel>", lambda e: self._step(-int(e.delta) // 120))
        self.bind("<Button-4>", lambda e: self._step(-1))
        self.bind("<Button-5>", lambda e: self._step(1))

    def _sethot(self, on):
        self._hot = bool(on)
        self._paint()

    def set(self, first, last):
        try:
            self._first = min(1.0, max(0.0, float(first)))
            self._last = min(1.0, max(0.0, float(last)))
        except (TypeError, ValueError):
            return
        self._paint()

    def _box(self):
        h = self.winfo_height()
        w = self.winfo_width()
        if h < 3 or w < 3:
            return None
        span = self._last - self._first
        if span <= 0.0005 or span >= 0.9995:
            return None
        th = max(30, int(round(span * h)))
        travel = h - th
        frac = self._first / (1.0 - span) if span < 1.0 else 0.0
        ty = min(travel, max(0, int(round(frac * travel))))
        return (0, ty, w, ty + th, th, h, travel, span)

    def _paint(self):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        pal = self._kit.pal
        self.configure(bg=pal["card"])
        self.delete("all")
        self._imgs = []
        box = self._box()
        h = self.winfo_height()
        if h < 3:
            return
        try:
            rail = self._kit.rounded(SLIM_SCB_W, h, pal["light_bg"], 5)
            self.create_image(0, 0, anchor="nw", image=rail)
            self._imgs.append(rail)
        except Exception:
            self.create_rectangle(0, 0, SLIM_SCB_W, h, fill=pal["light_bg"], outline="")
        if box is None:
            return
        fill = _thumb_hex(self._kit, self._hot or self._drag is not None)
        try:
            img = self._kit.rounded(SLIM_SCB_W, box[4], fill, 5)
            self.create_image(0, box[1], anchor="nw", image=img)
            self._imgs.append(img)
        except Exception:
            self.create_rectangle(0, box[1], SLIM_SCB_W - 1, box[3],
                                  fill=fill, outline="")

    def _to(self, frac):
        try:
            self._cvs.yview_moveto(min(1.0, max(0.0, frac)))
        except tk.TclError:
            pass

    def _step(self, n):
        if self._wheel_cb is not None:
            try:
                self._wheel_cb(n)
            except Exception:
                pass
        return "break"

    def _press(self, event):
        box = self._box()
        if box is None:
            return
        ty = box[1]
        by = box[3]
        travel = box[5]
        if event.y < ty:
            try:
                self._cvs.yview_scroll(-1, "pages")
            except tk.TclError:
                pass
            return
        if event.y > by:
            try:
                self._cvs.yview_scroll(1, "pages")
            except tk.TclError:
                pass
            return
        self._drag = (event.y, ty, travel, self._last - self._first)

    def _drag_to(self, event):
        if self._drag is None:
            return
        y0, ty0, travel, content_span = self._drag
        if travel <= 0 or content_span >= 1.0:
            return
        dy = event.y - ty0
        new_ty = max(0, min(travel, ty0 + dy))
        frac = new_ty / float(travel)
        self._to(frac * (1.0 - content_span))

    def _drop(self, _event):
        self._drag = None
        self._paint()

class _ChoiceDropdown(tk.Canvas):

    def __init__(self, master, kit, var=None, width=DD_H + 150,
                 on_select=None):
        super().__init__(master, width=width, height=DD_H,
                         highlightthickness=0, takefocus=1, bd=0,
                         cursor="hand2", bg=kit.pal["card"])
        self._kit = kit
        self._var = var
        self._on_select = on_select
        self._pairs = []
        self._pop = None
        self._imgs = []
        kit.reg(self, "canvas_card")
        kit.repaint(self._paint)
        self.bind("<Button-1>", lambda _e: self.toggle())
        self.bind("<Return>", lambda _e: self.toggle())
        self.bind("<Escape>", lambda _e: self.close())
        self.bind("<Button-2>", lambda e: "break")
        self.bind("<Configure>", lambda _e: self._paint())
        self._owns_wheel = True

    def set_pairs(self, pairs):
        try:
            self._pairs = [(str(d), v) for d, v in list(pairs or [])]
        except (TypeError, ValueError):
            self._pairs = []
        self._paint()

    def _text(self):
        try:
            want = str(self._var.get())
        except (tk.TclError, ValueError):
            want = ""
        for d, _v in self._pairs:
            if d == want:
                return d
        try:
            nums = [abs(float(v) - float(want)) for _d, v in self._pairs]
            return self._pairs[nums.index(min(nums))][0]
        except (TypeError, ValueError):
            pass
        return want

    def _paint(self):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        pal = self._kit.pal
        self.configure(bg=pal["card"])
        w = self.winfo_width()
        h = DD_H
        self.delete("all")
        self._imgs = []
        if w < 10:
            return
        try:
            img = self._kit.rounded(w, h, pal["entry"], 6)
            self.create_image(0, 0, anchor="nw", image=img)
            self._imgs.append(img)
            rim = self._kit.rounded(w, h, pal["entry_bd"], 6)
            self.create_image(0, 0, anchor="nw", image=rim)
            self._imgs.append(rim)
            img2 = self._kit.rounded(w - 2, h - 2, pal["entry"], 6)
            self.create_image(1, 1, anchor="nw", image=img2)
            self._imgs.append(img2)
        except Exception:
            self.create_rectangle(0, 0, w, h, fill=pal["entry"],
                                  outline=pal["entry_bd"])
        f = tkfont.Font(font=F_BASE)
        txt = self._text()
        ellipsis = "…"
        limit = max(max(10, w - 30), f.measure(ellipsis) + 4)
        iter_cap = 50
        while txt and f.measure(txt) > limit and iter_cap:
            iter_cap -= 1
            if txt == ellipsis:
                txt = ""
                break
            txt = txt[:-2]
            if not txt.endswith(ellipsis):
                txt = txt + ellipsis
            elif len(txt) > 1:
                txt = txt[:-1]
            else:
                txt = ""
                break
        self.create_text(10, h // 2, anchor="w", text=txt, font=F_BASE,
                         fill=pal["text"])
        cx = w - 9
        cy = h // 2
        hover = pal["muted"] if self._pop is None else pal["accent"]
        self.create_polygon(cx - 5, cy - 2, cx + 5, cy - 2, cx, cy + 4,
                            fill=hover, outline="")

    def toggle(self):
        try:
            self.focus_set()
        except tk.TclError:
            pass
        if self._pop is not None:
            self.close()
            return
        self._open_popup()

    def close(self):
        if self._pop is None:
            return
        pop = self._pop
        self._pop = None
        self._paint()
        if pop is None:
            return
        try:
            pop.withdraw()
        except Exception:
            pass
        try:
            pop.after(1, lambda p=pop: self._destroy_popup(p))
        except Exception:
            self._destroy_popup(pop)

    def _destroy_popup(self, pop):
        try:
            pop.grab_release()
        except Exception:
            pass
        try:
            pop.destroy()
        except Exception:
            pass

    def _open_popup(self):
        pairs = list(self._pairs)
        if not pairs:
            return
        pal = self._kit.pal
        pop = tk.Toplevel(self.winfo_toplevel())
        self._pop = pop
        pop.wm_overrideredirect(True)
        try:
            pop.attributes("-topmost", True)
        except tk.TclError:
            pass
        pop.configure(background=pal["card"], takefocus=1)

        pop_w = max(self.winfo_width(), 176)
        row_h = DD_ROW_H
        ph = len(pairs) * DD_ROW_H + 2

        pop_cvs = tk.Canvas(pop, highlightthickness=0, bg=pal["card"],
                            width=pop_w, height=ph)
        pop_cvs.pack(fill="both", expand=True)
        self._pop_cvs = pop_cvs

        ff = tkfont.Font(font=F_BASE)
        cur = self._text()
        text_limit = max(10, pop_w - 20)

        def _trunc(s):
            if not s or ff.measure(s) <= text_limit:
                return s
            ell = "…"
            t = s
            while t and ff.measure(t + ell) > text_limit:
                t = t[:-1]
            return (t + ell) if t else ell

        row_h = DD_ROW_H
        for i, (disp, val) in enumerate(pairs):
            y = i * row_h
            bg = pal["light_bg"] if disp == cur else pal["card"]
            fg = pal["accent"] if disp == cur else pal["text"]
            pop_cvs.create_rectangle(0, y, pop_w, y + row_h, fill=bg, outline="",
                                      tags=("bg%d" % i, "row%d" % i))
            pop_cvs.create_text(10, y + row_h // 2, anchor="w",
                                text=_trunc(str(disp)),
                                font=F_BASE, fill=fg,
                                tags=("tx%d" % i, "row%d" % i))
            pop_cvs.tag_bind("row%d" % i, "<Enter>",
                             lambda e, i=i: self._pop_hover(i, True))
            pop_cvs.tag_bind("row%d" % i, "<Leave>",
                             lambda e, i=i: self._pop_hover(i, False))
            pop_cvs.tag_bind("row%d" % i, "<Button-1>",
                             lambda e, i=i: self._pop_pick(i))

        pop.bind("<Escape>", lambda _e: self.close())
        pop.bind("<Button-1>", lambda _e: self.close())

        pop.update_idletasks()
        pop_cvs.configure(width=pop_w, height=ph)
        sx = self.winfo_rootx()
        sy = self.winfo_rooty() + DD_H + 3
        sh = pop.winfo_screenheight()
        if sy + ph > sh - 6:
            sy = max(6, self.winfo_rooty() - ph - 4)
        pop.geometry("%dx%d+%d+%d" % (pop_w, ph, sx, sy))
        pop.deiconify()
        try:
            pop.grab_set()
        except tk.TclError:
            pass
        pop.bind("<Destroy>", lambda _e: self._popped(pop), add="+")
        self._paint()

    def _pop_hover(self, idx, on):
        if self._pop is None:
            return
        pal = self._kit.pal
        try:
            cur = self._text()
            for i, (disp, _v) in enumerate(self._pairs):
                if on and i == idx:
                    bg = pal["light_bg"]
                elif disp == cur:
                    bg = pal["light_bg"]
                else:
                    bg = pal["card"]
                self._pop_cvs.itemconfigure("bg%d" % i, fill=bg)
        except tk.TclError:
            pass

    def _pop_pick(self, idx):
        if idx >= len(self._pairs):
            return
        disp, val = self._pairs[idx]
        try:
            self._var.set(disp)
        except (tk.TclError, ValueError):
            pass
        if self._on_select is not None:
            try:
                self._on_select()
            except Exception:
                pass
        self.close()

    def _popped(self, pop):
        if self._pop is pop:
            self._pop = None
            self._paint()

class _RangeSlider(tk.Canvas):

    def __init__(self, master, kit, var=None, lo=0.0, hi=1.0,
                 integer=True, width=SLIDE_W, height=SLIDE_H,
                 wheel_step=None):
        super().__init__(master, width=width, height=height,
                         highlightthickness=0, takefocus=0, bd=0,
                         cursor="hand2", bg=kit.pal["card"])
        self._kit = kit
        self._var = var
        self._lo = float(lo)
        self._hi = float(hi)
        self._integer = bool(integer)
        self._step = wheel_step
        self._drag = False
        self._hover = False
        self._imgs = []
        kit.reg(self, "canvas_card")
        kit.repaint(self._paint)
        self._owns_wheel = True
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._move)
        self.bind("<ButtonRelease-1>", self._drop)
        self.bind("<MouseWheel>", self._wheel_abs)
        self.bind("<Button-4>", self._wheel_step)
        self.bind("<Button-5>", self._wheel_step)
        self.bind("<Configure>", lambda _e: self._paint())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        if var is not None:
            var.trace_add("write", lambda *_a: self._paint())

    def _num(self):
        raw = ""
        if self._var is not None:
            try:
                raw = str(self._var.get())
            except (tk.TclError, ValueError):
                raw = ""
        try:
            val = float(raw)
        except (TypeError, ValueError):
            val = (self._lo + self._hi) / 2.0
        return self._clamp(val)

    def _clamp(self, val):
        if self._integer:
            val = int(round(val))
            val = int(max(self._lo, min(self._hi, val)))
            return float(val)
        val = max(self._lo, min(self._hi, val))
        return round(val, 2) + 0.0

    def _fmt(self, val):
        if self._integer:
            return "%d" % int(round(val))
        s = "%.2f" % val
        if s.endswith("00"):
            s = s[:-3]
        else:
            s = s.rstrip("0").rstrip(".")
        return s or "0"

    def _snap_step(self):
        if not self._integer:
            return 0
        span = self._hi - self._lo
        if span <= 20:
            return 1
        if span <= 100:
            return 1
        if span <= 500:
            return 5
        if span <= 2000:
            return 10
        if span <= 10000:
            return 100
        if span <= 50000:
            return 500
        return 1000

    def _set(self, val):
        val = self._clamp(val)
        step = self._snap_step()
        if step > 0 and val % step:
            val = round(val / step) * step
            val = self._clamp(val)
        text = self._fmt(val)
        if self._var is not None:
            try:
                if str(self._var.get()) != text:
                    self._var.set(text)
            except (tk.TclError, ValueError):
                pass
        self._paint()

    def _paint(self):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        pal = self._kit.pal
        self.configure(bg=pal["card"])
        w = self.winfo_width()
        h = SLIDE_H
        self.delete("all")
        self._imgs = []
        if w < 20:
            return
        cy = h // 2
        kw = 16
        span = max(1.0, self._hi - self._lo)
        frac = (self._num() - self._lo) / span
        frac = min(1.0, max(0.0, frac))
        x0 = kw // 2
        x1 = w - kw // 2
        pos = int(round(x0 + frac * (x1 - x0)))
        try:
            track = self._kit.rounded(w, 6, pal["light_bg"], 3)
            self.create_image(0, cy - 3, anchor="nw", image=track)
            self._imgs.append(track)
        except Exception:
            self.create_rectangle(0, cy - 3, w, cy + 3, fill=pal["light_bg"],
                                  outline="")
        fw = max(6, pos)
        try:
            fill_img = self._kit.rounded(max(fw, 4), 6, pal["accent"], 3)
            self.create_image(0, cy - 3, anchor="nw", image=fill_img)
            self._imgs.append(fill_img)
        except Exception:
            self.create_rectangle(0, cy - 3, max(4, pos), cy + 3,
                                  fill=pal["accent"], outline="")
        knob_fill = pal["accent"] if self._hover or self._drag else pal["accent"]
        self.create_oval(pos - kw // 2, cy - kw // 2, pos + kw // 2,
                         cy + kw // 2, fill=knob_fill,
                         outline=pal["entry_bd"])
        self._pos = pos

    def _val_at(self, x):
        w = self.winfo_width()
        kw = 16
        x0 = kw / 2.0
        x1 = max(x0 + 1, w - kw / 2.0)
        frac = (x - x0) / (x1 - x0)
        frac = min(1.0, max(0.0, frac))
        return self._lo + frac * (self._hi - self._lo)

    def _press(self, event):
        self._drag = True
        self._set(self._val_at(event.x))
        return "break"

    def _move(self, event):
        if not self._drag:
            return
        self._set(self._val_at(event.x))
        return "break"

    def _drop(self, _event):
        self._drag = False
        self._paint()

    def _wheel_abs(self, event):
        return self._wheel_step(event)

    def _set_hover(self, on):
        self._hover = bool(on)
        self._paint()

    def _wheel_step(self, event):
        if self._integer:
            step = self._snap_step() or 1
        else:
            step = (self._hi - self._lo) / 80.0
        delta = -1 if (getattr(event, "delta", 0) > 0
                       or getattr(event, "num", 0) == 4) else 1
        self._set(self._num() + delta * step)
        return "break"

# Short UI labels only.  META, descriptions, config keys and stored map values
# stay unchanged, so existing settings remain compatible with older releases.
_COMPACT_UI_LABELS = {
    "shift_bypass": "Bypass input", "subject_priority": "Subject priority",
    "subject_only": "People only", "hold_follow": "Hold to follow",
    "rapid_click_snap": "Rapid click assist", "follow_enabled": "Remember target",
    "motion_predict": "Motion lead", "motion_3d": "Robust tracking",
    "unibot_aim": "Aim steering", "accent_only": "My colors only",
    "hpose_mode": "Pose detector", "game_mode": "Game mode",
    "motion_camera_comp": "Camera compensation",
    "radius": "Search radius", "center_blind_area": "Center blind zone",
    "center_max_step": "Max steering step", "aim_speed": "Steering gain X",
    "aim_y_speed_multiplier": "Steering gain Y",
    "ads_steer_multiplier": "ADS response",
    "subject_focus_bias": "Subject aim bias", "motion_sense": "Motion sense",
    "watcher_seconds": "Double-click time", "follow_seconds": "Memory time",
    "scan_budget_ms": "Scan budget", "hpose_min_conf": "Min confidence",
    "pad_output": "Pad output", "mouse2_action": "Extra: tap",
    "mouse2_hold_action": "Extra: hold", "hpose_aim": "Aim point",
    "follow_learn": "Memory learns", "follow_boost": "Memory strength",
    "input_backend": "Input backend", "accent_matcher": "Color matcher",
    "capture_backend": "Screen capture",
    "reticle_enabled": "Show reticle", "reticle_shape": "Shape",
    "reticle_size": "Size",
    "reticle_gap": "Gap", "reticle_thickness": "Thickness",
    "reticle_rotation": "Rotation", "reticle_opacity": "Opacity",
    "reticle_color": "Color",
    "accent_color": "My colors", "exclude_color": "Avoid colors",
    "adaptive_threshold": "Auto threshold", "luminance_norm": "Relative brightness",
    "color_adapt": "Color adaptation", "accent_variants": "Shade variants",
    "accent_coherent": "Ignore color specks", "debug_visualize": "Show detections",
    "merge_distance": "Merge distance", "accent_tolerance": "Color tolerance",
    "accent_soft": "Near shades", "accent_boost": "Color priority",
    "trigger_button": "Assist button", "options_button": "Extra button",
    "bypass_key": "Bypass key", "bypass_pad": "Pad bypass",
    "pad_click_button": "Pad click", "pad_right_button": "Pad extra",
    "key_launch": "Launch key", "key_pause": "Pause key",
    "key_track_target": "Track target",
    "key_record": "Record key",
    "key_record_stop": "Stop record key", "key_play": "Play key",
    "pad_launch": "Pad launch", "pad_pause": "Pad pause",
    "pad_record": "Pad record",
    "pad_record_stop": "Pad stop record", "pad_play": "Pad playback",
}
_COMPACT_UI_CHOICES = {
    "pad_output": {"emulate": "Keyboard/Mouse", "vigem": "Controller"},
    "mouse2_action": {"4": "None", "1": "Clear", "2": "Follow last", "3": "Pick & follow"},
    "mouse2_hold_action": {"4": "None", "1": "Clear", "2": "Follow last", "3": "Pick & follow"},
    "accent_matcher": {"oklab": "Perceptual", "hsv": "Classic"},
    "hpose_aim": {"center": "Body", "head": "Head", "any": "Visible"},
    "follow_learn": {"accent": "My color", "any": "Any target"},
    "capture_backend": {"auto": "Auto", "dxcam": "Duplication", "mss": "Compatible", "wgc": "Graphics Capture"},
    "input_backend": {"win32": "Windows", "driver": "Driver"},
}


def _compact_ui_label(key):
    if key in _COMPACT_UI_LABELS:
        return _COMPACT_UI_LABELS[key]
    # Dynamic numbered keybindings use the established original numbering.
    if key.startswith("key_play") and key[8:].isdigit():
        return "Play #%s" % key[8:]
    if key.startswith("pad_play") and key[8:].isdigit():
        return "Pad play #%s" % key[8:]
    label = tr_label(key)
    for prefix, short in (("Controller: ", "Pad: "),
                          ("Shortcut: ", ""),
                          ("Extra-options button: ", "Extra: ")):
        if label.startswith(prefix):
            label = short + label[len(prefix):]
    return label


def _compact_ui_choice(key, pair):
    display, actual = pair
    return _COMPACT_UI_CHOICES.get(key, {}).get(str(actual), display)


class _MapChoiceSwitch(tk.Frame):
    """Two-choice configuration field.  Keep the old display StringVar intact.

    Its BooleanVar is presentation-only, so save/apply still uses the exact
    original mapped values and existing validation path.
    """
    def __init__(self, master, kit, var, pairs, key=None):
        super().__init__(master, bg=kit.pal["card"])
        self._kit = kit
        self._key = key
        self._var = var
        self._pairs = list(pairs)
        self._updating = False
        kit.reg(self, "card")
        self._state = tk.BooleanVar(master=self, value=False)
        self._toggle = kit.bool_switch(self, self._state, word=False)
        self._toggle.pack(side="right", padx=(6, 0))
        self._caption = tk.Label(self, font=F_DESC, anchor="e", justify="right", wraplength=118,
                                 bg=kit.pal["card"], fg=kit.pal["text"])
        kit.reg(self._caption, "card_text")
        self._caption.pack(side="right")
        self._caption.bind("<Button-1>", lambda _e: self._state.set(not self._state.get()))
        self._var.trace_add("write", self._from_var)
        self._state.trace_add("write", self._from_toggle)
        self._from_var()

    def set_pairs(self, pairs):
        self._pairs = list(pairs)
        self._from_var()

    def _from_var(self, *_a):
        if not self._pairs or self._updating:
            return
        self._updating = True
        try:
            selected = str(self._var.get())
            on = len(self._pairs) > 1 and selected == self._pairs[1][0]
            self._state.set(on)
            self._caption.configure(text=_compact_ui_choice(self._key, self._pairs[int(on)]))
        finally:
            self._updating = False

    def _from_toggle(self, *_a):
        if not self._pairs or self._updating:
            return
        self._updating = True
        try:
            index = min(int(self._state.get()), len(self._pairs) - 1)
            want = self._pairs[index][0]
            if str(self._var.get()) != want:
                self._var.set(want)
            self._caption.configure(text=_compact_ui_choice(self._key, self._pairs[index]))
        finally:
            self._updating = False


class _MapChoiceSlider(tk.Frame):
    """Discrete slider showing option NAMES, not their numeric indices."""
    def __init__(self, master, kit, var, pairs, key=None):
        super().__init__(master, bg=kit.pal["card"])
        self._kit = kit
        self._key = key
        self._var = var
        self._pairs = list(pairs)
        self._updating = False
        kit.reg(self, "card")
        self._index = tk.StringVar(master=self, value="0")
        self._slider = _RangeSlider(self, kit, var=self._index, lo=0,
                                    hi=max(1, len(self._pairs) - 1),
                                    integer=True, width=112)
        self._slider.configure(takefocus=1)
        self._slider.pack(side="right")
        self._caption = tk.Label(self, text="", font=F_DESC, anchor="e", justify="right", wraplength=116,
                                 bg=kit.pal["card"], fg=kit.pal["text"])
        kit.reg(self._caption, "card_text")
        self._caption.pack(side="right", padx=(0, 8))
        self._var.trace_add("write", self._from_var)
        self._index.trace_add("write", self._from_index)
        self._slider.bind("<Left>", lambda _e: self._nudge(-1))
        self._slider.bind("<Right>", lambda _e: self._nudge(1))
        self._slider.bind("<Home>", lambda _e: self._go(0))
        self._slider.bind("<End>", lambda _e: self._go(len(self._pairs) - 1))
        self._from_var()

    def set_pairs(self, pairs):
        self._pairs = list(pairs)
        self._slider._hi = float(max(1, len(self._pairs) - 1))
        self._from_var()

    def _go(self, target):
        self._index.set(str(max(0, min(len(self._pairs) - 1, target))))
        return "break"

    def _nudge(self, delta):
        try:
            i = int(float(self._index.get()))
        except (TypeError, ValueError):
            i = 0
        return self._go(i + delta)

    def _from_var(self, *_a):
        if not self._pairs or self._updating:
            return
        self._updating = True
        try:
            selected = str(self._var.get())
            index = next((i for i, p in enumerate(self._pairs)
                          if p[0] == selected), 0)
            self._index.set(str(index))
            self._caption.configure(text=_compact_ui_choice(self._key, self._pairs[index]))
        finally:
            self._updating = False

    def _from_index(self, *_a):
        if not self._pairs or self._updating:
            return
        self._updating = True
        try:
            try:
                index = int(round(float(self._index.get())))
            except (TypeError, ValueError):
                index = 0
            index = max(0, min(len(self._pairs) - 1, index))
            want = self._pairs[index][0]
            if str(self._var.get()) != want:
                self._var.set(want)
            self._caption.configure(text=_compact_ui_choice(self._key, self._pairs[index]))
        finally:
            self._updating = False


def _scrollable(page, kit, pal, bar=True):
    host = tk.Frame(page, bg=pal["card"])
    kit.reg(host, "card")
    host.pack(fill="both", expand=True)

    cvs = tk.Canvas(host, highlightthickness=0, takefocus=0, bd=0,
                    bg=pal["card"])
    kit.reg(cvs, "canvas_card")

    inner = tk.Frame(cvs, bg=pal["card"])
    kit.reg(inner, "card")
    win = cvs.create_window(0, 0, anchor="nw", window=inner)

    def _fit(_event=None):
        try:
            cvs.itemconfigure(win, width=max(120, cvs.winfo_width()))
            cvs.configure(scrollregion=cvs.bbox("all"))
            bind_wheel(inner)
        except tk.TclError:
            pass

    inner.bind("<Configure>", _fit)
    cvs.bind("<Configure>", _fit)

    def _wheel(step):
        try:
            total = max(1, inner.winfo_height())
            low, _high = cvs.yview()
            frac = low + (step * 60.0 / total)
            cvs.yview_moveto(min(1.0, max(0.0, frac)))
        except tk.TclError:
            pass

    if bar:
        sb = _SlimScrollbar(host, kit, cvs, wheel=_wheel)
        # Keep equal clearance above and below the scroll rail on every tab.
        sb.pack(side="right", fill="y", pady=(7, 7))
        cvs.configure(yscrollcommand=sb.set)
        sb.set(0.0, 1.0)
    cvs.pack(side="left", fill="both", expand=True)

    def bind_wheel(widget):
        seen = set()

        def _tree(w):
            if id(w) in seen:
                return
            seen.add(id(w))
            try:
                if getattr(w, "_owns_wheel", False):
                    pass
                elif w.winfo_class() not in ("TCombobox", "TSpinbox",
                                             "Spinbox", "TScrollbar",
                                             "Listbox", "Text"):
                    w.bind("<MouseWheel>",
                           lambda e: _wheel(-int(e.delta) // 120))
                    w.bind("<Button-4>", lambda e: _wheel(-1))
                    w.bind("<Button-5>", lambda e: _wheel(1))
                for c in w.winfo_children():
                    _tree(c)
            except tk.TclError:
                pass

        _tree(widget)

    bind_wheel(cvs)
    return inner, bind_wheel

def _fill_help(page, kit, pal):
    inner, bind_wheel = _scrollable(page, kit, pal, bar=True)
    bodies = []

    def para(parent, key, font, role, pady, wheel=True):
        lb = tk.Label(parent, text=i18n.L(key), font=font,
                      bg=pal["card"],
                      fg=pal["text"] if role != "card_muted"
                      else pal["muted"],
                      anchor="w", justify="left")
        kit.reg(lb, role)
        lb.pack(fill="x", padx=14, pady=pady)
        if wheel:
            bind_wheel(lb)
        R(lambda: lb.configure(text=i18n.L(key)) if _live(lb) else False)
        bodies.append(lb)
        return lb

    title_lbl = tk.Label(inner, text=i18n.L("help.title"), font=F_H1,
                    bg=pal["card"], fg=pal["text"], anchor="w")
    kit.reg(title_lbl, "card_text")
    title_lbl.pack(fill="x", padx=14, pady=(12, 0))
    bind_wheel(title_lbl)
    R(lambda: title_lbl.configure(text=i18n.L("help.title")) if _live(title_lbl)
      else False)

    para(inner, "help.sub", F_DESC, "card_muted", (2, 4))
    for tk_, bk_ in HELP_KEYS:
        h = tk.Label(inner, text=i18n.L(tk_), font=F_BOLD, bg=pal["card"],
                     fg=pal["text"], anchor="w")
        kit.reg(h, "card_bold")
        h.pack(fill="x", padx=14, pady=(12, 2))
        bind_wheel(h)
        R(lambda h=h, k=tk_: h.configure(text=i18n.L(k)) if _live(h)
          else False)
        para(inner, bk_, F_DESC, "card_muted", (0, 2))
    para(inner, "help.tail", F_DESC, "card_muted", (14, 12))

    def _rewrap(_event=None):
        try:
            want = max(200, inner.winfo_width() - 32)
            for lb in bodies:
                if str(lb.cget("wraplength")) != str(want):
                    lb.configure(wraplength=want)
        except tk.TclError:
            pass

    inner.bind("<Configure>", _rewrap, add="+")
    _rewrap()
    return inner

KEY_CAPTURE_SECONDS = 12.0
MOUSE_CAPTURE_SECONDS = 12.0

_MOD_ORDER = ("ctrl", "alt", "shift", "win")

_KEYSYM_TO_SPEC = {
    "return": "enter", "kp_enter": "enter", "escape": "esc", "space": "space",
    "tab": "tab", "kp_tab": "tab", "backspace": "backspace",
    "insert": "insert", "delete": "delete", "home": "home", "end": "end",
    "prior": "pgup", "next": "pgdn", "up": "up", "down": "down",
    "left": "left", "right": "right", "comma": "comma", "period": "period",
    "minus": "minus", "plus": "plus", "equal": "plus",
}

_CHAR_TO_SPEC = {" ": "space", ",": "comma", ".": "period", "-": "minus",
                 "+": "plus"}

_TK_MODS = {
    "shift_l": "shift", "shift_r": "shift",
    "control_l": "ctrl", "control_r": "ctrl",
    "alt_l": "alt", "alt_r": "alt", "mode_switch": "alt",
    "super_l": "win", "super_r": "win", "win_l": "win", "win_r": "win",
    "meta_l": "win", "meta_r": "win",
}

def keysym_to_spec(keysym):
    ks = str(keysym or "").strip()
    low = ks.lower()
    if low in _KEYSYM_TO_SPEC:
        return _KEYSYM_TO_SPEC[low]
    if len(low) > 1 and low[0] == "f" and low[1:].isdigit():
        n = int(low[1:])
        if 1 <= n <= 24:
            return "f%d" % n
    if len(ks) == 1:
        c = ks.lower()
        if ("a" <= c <= "z") or ("0" <= c <= "9") or c in _CHAR_TO_SPEC:
            return _CHAR_TO_SPEC.get(c, c)
    return None

def _key_capture_dialog(parent, kit, app=None, initial="", allow_pad=False,
                        allow_modifier=False, multi=False, seq=False):
    import input as gamepad
    import input as hotkeys
    max_tokens = 5
    win = tk.Toplevel(parent)
    kit.attach(win)
    kit.reg(win, "card")
    pal = kit.pal
    win.title(i18n.L("key.cap_title"))
    win.configure(background=pal["card"])
    win.resizable(False, False)
    win.transient(parent)
    try:
        win.grab_set()
    except tk.TclError:
        pass

    box = tk.Frame(win, bg=pal["card"])
    kit.reg(box, "card")
    box.pack(fill="both", expand=True, padx=18, pady=16)
    head = tk.Label(box, text=i18n.L(
        "key.cap_head_multi" if multi else "key.cap_head"), font=F_BOLD,
        bg=pal["card"], fg=pal["text"], wraplength=380,
        justify="left", anchor="w")
    kit.reg(head, "card_bold")
    head.pack(fill="x", pady=(0, 8))
    hint = tk.Label(box, text=i18n.L("key.cap_waiting"), font=F_DESC,
                    bg=pal["card"], fg=pal["accent"], wraplength=380,
                    justify="left", anchor="w")
    kit.reg(hint, "card_muted")
    hint.pack(fill="x", pady=(0, 10))
    note = tk.Label(box, text=i18n.L(
                        "key.cap_mod_note" if allow_modifier
                        else ("key.cap_pad" if allow_pad else "key.cap_note")),
                    font=F_DESC, bg=pal["card"], fg=pal["muted"],
                    wraplength=380, justify="left", anchor="w")
    kit.reg(note, "card_muted")
    note.pack(fill="x", pady=(0, 12))

    result = {"spec": None}
    done = {"v": False}
    held = set()
    track = {"peak": 0, "seen": False, "index": None, "rescan": 0.0,
             "sawkey": False, "keys": [], "mods": [], "down": set(),
             "expire": None}
    aid = []

    def _seq_current():
        return "+".join([m for m in _MOD_ORDER if m in track["mods"]]
                        + list(track["keys"]))

    def _hint_hold():
        names = "+".join(m for m in _MOD_ORDER if m in held)
        if multi:
            if track["mods"] or track["keys"]:
                combo = "+".join(track["mods"] + track["keys"])
                hint.configure(text=i18n.L("key.cap_holding_multi", combo))
            elif names:
                hint.configure(text=i18n.L("key.cap_holding", names))
            else:
                hint.configure(text=i18n.L("key.cap_waiting"))
            return
        if names:
            hint.configure(text=i18n.L("key.cap_holding", names))

    def _rearm():
        if track["expire"] is not None:
            try:
                win.after_cancel(track["expire"])
            except (tk.TclError, ValueError):
                pass
        track["expire"] = win.after(
            int(KEY_CAPTURE_SECONDS * 1000), expire)

    def finish(spec):
        if done["v"] or not _live(win):
            return
        if multi and spec is not None:
            if len(track["mods"]) + len(track["keys"]) > max_tokens:
                return
            canon = "+".join([m for m in _MOD_ORDER
                              if m in track["mods"]] + list(track["keys"]))
            if seq:
                if hotkeys._parse_macro_sequence(canon) is None:
                    hint.configure(text=i18n.L("key.cap_invalid", canon))
                    return
            else:
                if hotkeys._parse_macro_spec(canon) is None:
                    hint.configure(text=i18n.L("key.cap_invalid", canon))
                    return
            spec = canon
        done["v"] = True
        result["spec"] = spec
        for handle in aid:
            try:
                win.after_cancel(handle)
            except (tk.TclError, ValueError):
                pass
        for event_name in ("<KeyPress>", "<KeyRelease>", "<Escape>"):
            try:
                win.unbind_all(event_name)
            except tk.TclError:
                pass
        try:
            win.destroy()
        except tk.TclError:
            pass

    def on_press(event):
        if not _live(win) or done["v"]:
            return
        low = str(getattr(event, "keysym", "") or "").lower()
        mod = _TK_MODS.get(low)
        if mod is not None:
            held.add(mod)
            if multi and mod not in track["mods"]:
                track["mods"].append(mod)
            _hint_hold()
            _rearm()
            return
        token = keysym_to_spec(getattr(event, "keysym", ""))
        if token is not None:
            track["sawkey"] = True
        if token is None:
            hint.configure(text=i18n.L("key.cap_unbound",
                                       str(getattr(event, "keysym", "?"))))
            return
        if multi:
            total = len(track["mods"]) + len(track["keys"])
            if token in track["keys"]:
                if seq:
                    track["keys"].append(token)
                    track["down"].add(token)
                else:
                    track["down"].add(token)
                _hint_hold()
                _rearm()
                return
            if total >= max_tokens:
                hint.configure(text=i18n.L("key.cap_max", max_tokens))
                return
            track["keys"].append(token)
            track["down"].add(token)
            _hint_hold()
            _rearm()
            return
        spec = "+".join([m for m in _MOD_ORDER if m in held] + [token])
        if hotkeys._parse_hotkey(spec) is None:
            hint.configure(text=i18n.L("key.cap_invalid", spec))
            return
        finish(spec)

    def on_release(event):
        low = str(getattr(event, "keysym", "") or "").lower()
        mod = _TK_MODS.get(low)
        if mod is None:
            token = keysym_to_spec(getattr(event, "keysym", ""))
            if multi and token in track["down"]:
                track["down"].discard(token)
                if not seq and track["keys"] and not track["down"]:
                    finish("+".join([m for m in _MOD_ORDER
                                     if m in track["mods"]]
                                    + list(track["keys"])))
            return
        held.discard(mod)
        if multi:
            if (allow_modifier or not track["keys"]) and track["mods"] \
                    and not held and not track["keys"]:
                finish("+".join(m for m in _MOD_ORDER if m in track["mods"]))
            return
        if allow_modifier and not track["sawkey"] and held:
            spec = "+".join(m for m in _MOD_ORDER if m in held)
            held.discard(mod)
            finish(spec)
            return
        held.discard(mod)

    def poll_pad():
        if not _live(win) or not allow_pad:
            return
        reader = getattr(app, "_pad_reader", None) if app is not None else None
        now = time.time()
        index = getattr(app, "_pad_index", None) if app is not None else None
        if index is None and now >= track["rescan"]:
            track["rescan"] = now + 0.5
            index = gamepad.first_connected(-1, reader=reader)
            track["index"] = index
        if index is None:
            index = track["index"]
        state = None if index is None else gamepad.read(index, reader=reader)
        if state is not None and state.connected:
            buttons = int(state.buttons)
            if buttons:
                track["seen"] = True
                track["peak"] |= buttons
                hint.configure(text=i18n.L(
                    "key.cap_pad_now",
                    gamepad.format_buttons(track["peak"])))
            elif track["seen"]:
                finish("pad:" + gamepad.format_buttons(track["peak"]))
                return
        aid.append(win.after(16, poll_pad))

    def expire():
        if not _live(win):
            return
        hint.configure(text=i18n.L("key.cap_timeout"))
        aid.append(win.after(1100, lambda: finish(_seq_current() if seq else None)))

    cancel = kit.button(box, i18n.L("cap.cancel"),
                        lambda: finish(
                            _seq_current() if seq else None))
    cancel.pack(anchor="e")
    win.bind("<KeyPress>", on_press)
    win.bind("<KeyRelease>", on_release)
    win.bind("<Escape>", lambda _e: finish(
        _seq_current() if seq else None))
    win.bind_all("<KeyPress>", on_press, add="+")
    win.bind_all("<KeyRelease>", on_release, add="+")
    win.bind_all("<Escape>", lambda _e: finish(
        _seq_current() if seq else None), add="+")
    win.protocol("WM_DELETE_WINDOW", lambda: finish(
        _seq_current() if seq else None))
    try:
        _rearm()
    except Exception:
        aid.append(win.after(int(KEY_CAPTURE_SECONDS * 1000), expire))
    try:
        win.update_idletasks()
        win.deiconify()
        win.lift()
        win.focus_force()
    except tk.TclError:
        pass
    if allow_pad:
        poll_pad()
    try:
        parent.wait_window(win)
    except tk.TclError:
        pass
    return result["spec"]

def _pad_capture_dialog(parent, kit, app, initial="", single=False):
    import input as gamepad
    win = tk.Toplevel(parent)
    kit.attach(win)
    kit.reg(win, "card")
    pal = kit.pal
    win.title(i18n.L("pad.cap_title"))
    win.configure(background=pal["card"])
    win.resizable(False, False)
    win.transient(parent)
    try:
        win.grab_set()
    except tk.TclError:
        pass

    box = tk.Frame(win, bg=pal["card"])
    kit.reg(box, "card")
    box.pack(fill="both", expand=True, padx=18, pady=16)
    head = tk.Label(box, text=i18n.L("pad.cap_single_head" if single
                                     else "pad.cap_head"), font=F_BOLD,
                    bg=pal["card"], fg=pal["text"], wraplength=380,
                    justify="left", anchor="w")
    kit.reg(head, "card_bold")
    head.pack(fill="x", pady=(0, 8))
    hint = tk.Label(box, text="", font=F_DESC, bg=pal["card"],
                    fg=pal["accent"], wraplength=380, justify="left",
                    anchor="w")
    kit.reg(hint, "card_muted")
    hint.pack(fill="x", pady=(0, 12))
    names = tk.Label(box, text=i18n.L("pad.cap_names"), font=F_DESC,
                     bg=pal["card"], fg=pal["muted"], wraplength=380,
                     justify="left", anchor="w")
    kit.reg(names, "card_muted")
    names.pack(fill="x", pady=(0, 12))

    result = {"spec": None}
    done = {"v": False}
    track = {"peak": 0, "seen": False, "index": None, "rescan": 0.0,
             "deadline": time.time() + PAD_CAPTURE_SECONDS}
    aid = []

    def finish(spec):
        if done["v"] or not _live(win):
            return
        done["v"] = True
        result["spec"] = spec
        for handle in aid:
            try:
                win.after_cancel(handle)
            except (tk.TclError, ValueError):
                pass
        for seq in ("<Escape>", "<KeyPress>"):
            try:
                win.unbind(seq)
                win.unbind_all(seq)
            except tk.TclError:
                pass
        try:
            win.destroy()
        except tk.TclError:
            pass

    def on_key(event):
        if str(getattr(event, "keysym", "") or "").lower() in ("escape", "esc"):
            finish(None)

    def poll():
        if not _live(win):
            return
        if time.time() > track["deadline"]:
            hint.configure(text=i18n.L("pad.cap_timeout"))
            aid.append(win.after(1100, lambda: finish(None)))
            return
        reader = getattr(app, "_pad_reader", None)
        now = time.time()
        index = getattr(app, "_pad_index", None)
        if index is None and now >= track["rescan"]:
            track["rescan"] = now + 0.5
            index = gamepad.first_connected(-1, reader=reader)
            track["index"] = index
        if index is None:
            index = track["index"]
        state = None if index is None else gamepad.read(index, reader=reader)
        if state is None or not state.connected:
            if reader is None and not gamepad.available():
                hint.configure(text=i18n.L("pad.cap_noxinput"))
                aid.append(win.after(1500, lambda: finish(None)))
                return
            hint.configure(text=i18n.L("pad.cap_nopad"))
            aid.append(win.after(120, poll))
            return
        buttons = int(state.buttons)
        if single:
            if buttons:
                finish(gamepad.PAD_BUTTONS[
                    (buttons & -buttons).bit_length() - 1])
                return
            hint.configure(text=i18n.L("pad.cap_waiting"))
        elif buttons:
            track["seen"] = True
            track["peak"] |= buttons
            hint.configure(text=i18n.L(
                "pad.cap_now", gamepad.format_buttons(track["peak"])))
        elif track["seen"]:
            finish(gamepad.format_buttons(track["peak"]))
            return
        else:
            hint.configure(text=i18n.L("pad.cap_waiting"))
        aid.append(win.after(16, poll))

    cancel = kit.button(box, i18n.L("cap.cancel"), lambda: finish(None))
    cancel.pack(anchor="e")
    win.bind("<Escape>", lambda _e: finish(None))
    win.bind("<KeyPress>", on_key)
    win.bind_all("<Escape>", lambda _e: finish(None), add="+")
    win.bind_all("<KeyPress>", on_key, add="+")
    win.protocol("WM_DELETE_WINDOW", lambda: finish(None))
    if initial:
        track["deadline"] = time.time() + PAD_CAPTURE_SECONDS
    poll()
    try:
        parent.wait_window(win)
    except tk.TclError:
        pass
    return result["spec"]

_TK_MOUSE_BUTTONS = {1: "mouse1", 2: "mouse3", 3: "mouse2",
                     4: "mouse4", 5: "mouse5"}

MOUSE_BUTTON_NAMES = tuple(_TK_MOUSE_BUTTONS[n]
                           for n in sorted(_TK_MOUSE_BUTTONS))

def _mouse_capture_dialog(parent, kit, initial=""):
    win = tk.Toplevel(parent)
    kit.attach(win)
    kit.reg(win, "card")
    pal = kit.pal
    win.title(i18n.L("mouse.cap_title"))
    win.configure(background=pal["card"])
    win.resizable(False, False)
    win.transient(parent)
    try:
        win.grab_set()
    except tk.TclError:
        pass

    box = tk.Frame(win, bg=pal["card"])
    kit.reg(box, "card")
    box.pack(fill="both", expand=True, padx=18, pady=16)
    head = tk.Label(box, text=i18n.L("mouse.cap_head"), font=F_BOLD,
                    bg=pal["card"], fg=pal["text"], wraplength=380,
                    justify="left", anchor="w")
    kit.reg(head, "card_bold")
    head.pack(fill="x", pady=(0, 8))
    hint = tk.Label(box, text=i18n.L("mouse.cap_current",
                                     initial or i18n.L("cap.none")),
                    font=F_DESC, bg=pal["card"], fg=pal["accent"],
                    wraplength=380, justify="left", anchor="w")
    kit.reg(hint, "card_muted")
    hint.pack(fill="x", pady=(0, 10))
    note = tk.Label(box, text=i18n.L("mouse.cap_note"), font=F_DESC,
                    bg=pal["card"], fg=pal["muted"], wraplength=380,
                    justify="left", anchor="w")
    kit.reg(note, "card_muted")
    note.pack(fill="x", pady=(0, 12))

    result = {"spec": None}
    done = {"v": False}
    seqs = []
    aid = []

    def finish(spec):
        if done["v"] or not _live(win):
            return
        done["v"] = True
        result["spec"] = spec
        for handle in aid:
            try:
                win.after_cancel(handle)
            except (tk.TclError, ValueError):
                pass
        for seq in seqs:
            try:
                win.unbind(seq)
                win.unbind_all(seq)
            except tk.TclError:
                pass
        try:
            win.destroy()
        except tk.TclError:
            pass

    for num in sorted(_TK_MOUSE_BUTTONS):
        seq = "<ButtonPress-%d>" % num
        seqs.append(seq)
        win.bind(seq, lambda e, n=_TK_MOUSE_BUTTONS[num]: (e.widget.winfo_class() != "Button" and finish(n)))
    def on_key(event):
        if str(getattr(event, "keysym", "") or "").lower() in ("escape", "esc"):
            finish(None)

    for seq in ("<Escape>", "<KeyPress>"):
        seqs.append(seq)
        win.bind(seq, on_key if seq == "<KeyPress>"
                 else (lambda _e: finish(None)))
        win.bind_all(seq, on_key if seq == "<KeyPress>"
                     else (lambda _e: finish(None)), add="+")

    def expire():
        if done["v"] or not _live(win):
            return
        hint.configure(text=i18n.L("mouse.cap_timeout"))
        aid.append(win.after(1100, lambda: finish(None)))

    cancel = kit.button(box, i18n.L("cap.cancel"), lambda: finish(None))
    cancel.pack(anchor="e")
    win.protocol("WM_DELETE_WINDOW", lambda: finish(None))
    aid.append(win.after(int(MOUSE_CAPTURE_SECONDS * 1000), expire))
    try:
        parent.wait_window(win)
    except tk.TclError:
        pass
    return result["spec"]

CAPTURE_KINDS = ("key", "keymod", "pad", "padsingle", "padkey", "mouse",
                 "combo", "seq")

def capture_button(parent, kit, root, app, key, var, kind, status_msg, R, action_only=False):
    def _capture(k=key, v=var, kd=kind):
        if kd == "pad":
            spec = _pad_capture_dialog(root, kit, app, v.get())
        elif kd == "padsingle":
            spec = _pad_capture_dialog(root, kit, app, v.get(), single=True)
        elif kd == "mouse":
            spec = _mouse_capture_dialog(root, kit, v.get())
        elif kd == "combo":
            spec = _key_capture_dialog(root, kit, app, v.get(),
                                       allow_modifier=True, multi=True)
        elif kd == "seq":
            spec = _key_capture_dialog(root, kit, app, v.get(),
                                       allow_modifier=True, multi=True,
                                       seq=True)
        else:
            spec = _key_capture_dialog(root, kit, app, v.get(),
                                       allow_pad=(kd == "padkey"),
                                       allow_modifier=(kd == "keymod"))
        label = tr_label(k)
        if spec is None:
            status_msg(i18n.L("cap.kept", label))
            return
        v.set(spec)
        status_msg(i18n.L("cap.set", label, spec or i18n.L("cap.none")))

    if action_only:
        return _capture

    entry_h = getattr(kit, "_entry_h", None)
    if entry_h is None:
        try:
            probe = tk.Entry(parent, font=F_ENTRY, relief="solid",
                             borderwidth=1, highlightthickness=1)
            probe.update_idletasks()
            entry_h = max(1, int(probe.winfo_reqheight()))
            probe.destroy()
        except Exception:
            entry_h = kit.BTN_H
        kit._entry_h = entry_h
    btn = kit.button(parent, i18n.L("cap.btn"), _capture,
                     fixed_w=PAD_CAPTURE_W, height=entry_h)
    R(lambda b=btn: kit.set_text(b, i18n.L("cap.btn")) if _live(b) else False)
    return btn

def _bind_capture_entry(entry, capture):
    """Click/Return captures a binding; Tab still allows keyboard editing.

    Keep the existing Entry and StringVar: settings can still be pasted or
    edited after tabbing into the field, and Apply works as before.
    """
    entry.configure(cursor="hand2", takefocus=1)
    entry.bind("<Button-1>", lambda _e: (capture(), "break")[1])
    entry.bind("<Return>", lambda _e: (capture(), "break")[1])
    entry.bind("<KP_Enter>", lambda _e: (capture(), "break")[1])


def _widget_class(root, widget):
    try:
        if isinstance(widget, str):
            return str(root.tk.call("winfo", "class", widget))
        return widget.winfo_class()
    except Exception:
        return ""



def _snappy_nav_icon_vector_fallback(canvas, name, stroke):
    """Draw crisp, uniformly aligned navigation icons in 24x24 logical px.

    Uses native Tk vector strokes rather than platform-dependent Unicode glyphs.
    The existing labels and tab IDs are unchanged.
    """
    canvas.delete("all")
    line = lambda *p, **kw: canvas.create_line(
        *p, fill=stroke, width=1.8, capstyle="round", joinstyle="round", **kw)
    oval = lambda *p, **kw: canvas.create_oval(
        *p, outline=stroke, width=1.8, **kw)
    rect = lambda *p, **kw: canvas.create_rectangle(
        *p, outline=stroke, width=1.8, **kw)
    poly = lambda *p, **kw: canvas.create_polygon(
        *p, outline=stroke, fill="", width=1.8, **kw)
    if name == "assist":
        oval(5, 5, 19, 19)
        oval(10.5, 10.5, 13.5, 13.5)
        line(12, 1.5, 12, 6.5); line(12, 17.5, 12, 22.5)
        line(1.5, 12, 6.5, 12); line(17.5, 12, 22.5, 12)
    elif name == "handler":
        rect(3, 4, 21, 18); line(8, 21, 16, 21); line(12, 18, 12, 21)
        line(8, 11, 10.4, 13.4, 16, 8)
    elif name == "advanced":
        oval(7, 7, 17, 17); oval(10.5, 10.5, 13.5, 13.5)
        import math
        for i in range(8):
            a = i * math.pi / 4
            line(12 + 6.8 * math.cos(a), 12 + 6.8 * math.sin(a),
                 12 + 10.1 * math.cos(a), 12 + 10.1 * math.sin(a))
    elif name == "experimental":
        line(9, 3, 15, 3); line(10, 3, 10, 10); line(14, 3, 14, 10)
        poly(10, 10, 5.5, 19.5, 6.5, 21, 17.5, 21, 18.5, 19.5, 14, 10)
        line(8, 16, 16, 16)
    elif name == "keyboard":
        rect(2.5, 6, 21.5, 18)
        for x in (6.5, 11, 15.5):
            line(x, 9, x + 1.3, 9)
            line(x, 12, x + 1.3, 12)
        line(8, 15.3, 16, 15.3)
    elif name == "controller":
        oval(3, 7, 21, 18)
        line(7, 11, 11, 11); line(9, 9, 9, 13)
        oval(15.5, 10, 17, 11.5); oval(18, 13, 19.5, 14.5)
    elif name == "macros":
        for y in (6, 12, 18):
            oval(3, y - 1.2, 5.4, y + 1.2)
            line(9, y, 21, y)
    elif name == "spoofer":
        poly(12, 2.5, 20, 6, 20, 13, 17.5, 18, 12, 21.5,
             6.5, 18, 4, 13, 4, 6)
        oval(9.5, 9.5, 14.5, 14.5)
    elif name == "latency_fix":
        oval(4, 4, 19, 19)
        line(12, 6, 12, 12, 16, 14); line(19, 4.5, 19, 9.5)
    elif name == "latency_live":
        line(2, 13, 6, 13, 9, 6, 12, 19, 15, 10, 18, 13, 22, 13)
    elif name == "latency_tools":
        for x, y in ((6, 9), (12, 15), (18, 8)):
            line(x, 3, x, 21)
            rect(x - 2.6, y - 2.1, x + 2.6, y + 2.1)
    elif name == "help":
        oval(3.5, 3.5, 20.5, 20.5)
        canvas.create_text(12, 12, text="?", fill=stroke,
                           font=("Segoe UI", 12, "bold"))
    # Strokes and font metrics vary by Tk/Windows DPI. Normalize the *actual*
    # drawn bounds into the same 20x20 visual box for every icon.
    bounds = canvas.bbox("all")
    if bounds:
        span = max(bounds[2] - bounds[0], bounds[3] - bounds[1], 1)
        canvas.scale("all", 12, 12, min(1.0, 19.0 / span),
                     min(1.0, 19.0 / span))
        bounds = canvas.bbox("all")
        canvas.move("all", 12 - (bounds[0] + bounds[2]) / 2.0,
                    12 - (bounds[1] + bounds[3]) / 2.0)

# Pixel-accurate, anti-aliased PNGs created from one consistent outline icon set.
# These are embedded so gui.py remains a one-file replacement, and Windows
# users do not need CairoSVG, Pillow, extra PNG files, or any icon fonts.
_NAV_ICON_PNG = {
    'assist': {
        'light': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAADPklEQVRIia2W0WvbVRTHP+eXpKEgZoVpfr/ctMniw8DfixAnCuuL+rKKoJ0bWPekIPMPEASHL/og+LJ/QoWxrsjs3vbUokKNA6Uvyqq1vUnaQpeNlhCT/I4PvT+NSZsl4IEL957zvd/zO/ecc+8PxhBjzIoxZmWcPclxwMD5MfEkRgFls9kzU1NTc8BFgEwm89vk5OTB4eFh43F7ZZgtn8/Pq+o14LkTMPdE5LPt7e0lQEd2UCqVMq1W62vgglM9AO4Cb7n1TeBV4JRb30mn0wsbGxsP+7m8E8hXHfmuiLzv+37WWnspxlhrL/m+/zRwFdgD5lqt1kqpVMo8LgIxxiwDF1R1HZirVqt/xsa4gqy1s7EuCIKCiCyLSAh8a619/UQH+Xz+oqreBHZV9Vwv+TAJgqDged4a8JSIzLucAH1H5BKKiFzrJc/lcmeNMYvGmF03FnO53NnYXqvVNoFPejkGIpiZmSl1u937wAPf97OVSqUNMD09/Yyq/qSqT/Z9+AFQttb+ChCG4USj0dgFMt1u90y9Xv/jPxFEUfSim96NyQG63e4XjvxGKpUKUqlUANwAnlDVz2Pc+vr6XxxVGp7nxVyIS9xxHbpqrZ01xlSBIJlM5jY3N2sAhUIh6HQ6VaBmrc0N4xgo0/9bPGvtrLVWROQdp1u01kpciqr6A0Cn07leLBb9YrHodzqd6722mENVl5z+7Zjjnwja7fb3bvpKuVxOxfpEIvGhiDwCLrfb7Vq73a4Bl4EDEfkoxoVhOCEiLzsHMde/Sd7Z2fkduAecqtfr78b6ra2t+1EUvQDc4qhr94Bbqvp8XEEAjUbjPSAjIhVXtsBgo82r6iKwF0XRuV7gMPF9v5hIJNaA06r6RrVa/WYgAgDXgXc46sjlIAgKvfbjHhzf94vJZHIZOA3c7iUfcABoOp1eAH4RkdDzvB+NMVfDMJxw9vNuEIbhhDHmg0QisaaqzwI/N5vNK/3RDbuuvwRec6qHHDXRPICqLrmExrfn7WazeWV/f//RSA5iyefzbwIfq2r5OLuIVKIo+rT/WEZ2EIvv+0XP814Ska9cBAuq+t2oRTCyGGPUGHPs03iSjPtXsTomnr8BlFdzvk1+P0UAAAAASUVORK5CYII=',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAAElUlEQVRIia2XXWxURRTHf2fZ3d4qLpBQE+dOIja8aKM1fGmoJFok8mlCgCcj8QUiikG0Eh+Ivkhi8KVKiQk8KTyZ+PFACiYIPoDRgMiHEGOwNPTea2oN4m5Jt9lljw87i5fb3UI3nGRyz9xz5v+fMzPnzrlCE9Le3j6jWCyeBPA8r2tgYODfqWKkmiEulUqdItIhIh2lUqmzGYymiFU1VU+fisjdOs6ZM8crlUpLgdXAfGChM50CTovIoXQ6fWxwcLB4T4iNMfeJyDZgBzDzDu7XReRD4JMgCMaaJvZ9/0ngG+Dh2OsxERlQ1Q4AEbmoqu1A6y1QkSuqujYMw3ONsBvuj7V2JXAiRnpWRNar6mxga8x1q3u3ATgLoKqPACestSsa4deN2EV6ArgfKItITxAEe4AKgDFmsYicdCRdURT9UAvE9/1twG4gDYwCz9SLfAKx29NLLtIysDYMw0MJt7S1thcgCII3nV8cY42IfAWkReQK0JHc82lJ4lwu1wOsc923wzD8vM6iVPL5fH8+n++vrUJcCoXC77lc7gbwAjBLVfOFQuFkw4hdyvxJ9fSeDcNwfhLYWrtcVXuAJ9yr86lU6qOhoaFvE/wp3/fPAJ3AP62trQ9dvnx5/JYx7unydCaAiHyQIBVjzD5VPQwsBdpcW1qpVI4YY/YlAqkAu5w+q1gsdt82q8QsV7nnWKVSORw3+L7/sohsct0R4FPXRtxEN1lrX4qPKZfL/UBtb1fGben29vYZ4+Pj81R1mogsUlVU9Qqw2BhTjKLoR6qH51U3ZkhV50VR9DeAMeY9EfkFsKq6BTgIpI0xTwOeqg6KyKOq+pQx5nkRudnS0nJGjDG/ikgHDURE9gZBsNX3/QIwHegNw3B7YjV6gW3AaBiGD1hr+1T19UaYqnqxqQ/8vZCU53ldQLeqLhORn92MLqnqMlVd4vIU4IJ7rjPGzK4BOL2WfuehmtuqusRh/OYwT6vqMqDb87yu29LJ9/29wGvAWLlcbhseHr4Rs20EPnPdv4Avnb6e6ukG2BiG4YHamLa2tunZbHYE8ESkLwiCN25FnFiBfvdsTafTt31nwzA8oKr7XfdBYItrbS6i/WEYHoyPyWazKwEvgT2ROJPJfAdcd92dCbtGUbRZRFYAR6mm0QhwNJVKLY+iaDOgCeydTr/med4xJhNr7bu+76tr2yd1nkSMMT01HGPMO0l7vVP9sfuwA+w2xqyp45O21vZZa/uo3kJJ0hddQQAwkM1m9yR9Gl2LnVSvxenATWBHGIa93MW1aIx5y5FOA0ZFZHEQBBeSHA0rEGvtClX9wpEDnAN2lcvl/kwms1BVjwOIyHOlUulUOp1eRXVPH3f+oyKyIQiCI/Xw71T6dIrI166iqEnRlT6POeJLrvTxYj5/iMjaepHWZMJ9HJdCoTCcy+X2qWpeRBZQravS/J+3OL22z9dU9f1sNvvK1atXo8mw77q8nTt3bkuxWOxW1dXAAmCRM/0kIqeBQ57nHY/fufeEOC7W2mfjexwEwfdTxWjuL0CkUk+fikzIwbuRTCZzrlgsXqzpzWD8BwgL4vfP8llXAAAAAElFTkSuQmCC',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAAFm0lEQVRYhcVYbYhUVRh+3nvPOrOre12W0WHnnNnwKxAr+1NYVJZQ9iGaqwj5RWIgZpQYRD+KlAR/aGYf/gnJzdWFjD4sXKUgzDL9p1l/JD9q555BbSGccdWtueftx5zRO3fvrKuj9MKF9+uc95nz8b7vGUKdpJSaCOBbKz7h+/7JeuZz6gXEzAuYeZz9FtQ7X92AiGhEHH+zVC8gl5mbK4Ll3XompBsd0N7ePr5UKs0jojkApsUACAAcYeY9QojPe3t7T98WQJlMJktE6wAsjQFRiwIi6mTmtVpr/5YBymQyy4noAwCNEVMJwGUAlW0rAmiKAXwZwIta687rxbreGXKllO8S0bYImB4iWtTQ0DCGiDZXlES02XXdMUS0BMC+kH8jgO1SyndiwFaRGMoopdwEYHVIdZSZX87n8z9VFEqpqjG9vb1/A9gJYGc2m33EGPM+gKnWvEZKCa31q7Vi1lwhKeXzETCfMfNDYTAAwMyn4ngAyOVyBxsaGqYx866Qeo1S6oVacWPPkD3AJ3Btmz7VWj8HgOPmUEotAgDf93fV8pFS7gYw38qXiOhO3/d11DF2Pz3P2wLgPiseJaLZhULh31q/qlAoHC8UCsdr2QGgqampx3GcWQDSABqY2SsWi98MQh5VZLPZCcaYExWwzPxwdJsAIJVKNSeTyVXM/DiAu636VyL67sqVK1v7+vqK0TFKqenMfMCKQalUmnTu3LkzYZ9BZygIgg5cW7meODBtbW2TE4nEMWbeAGAGgDH2m8HMGxKJxLG2trbJ0XG+7//AzPut6AohOqI+gwAR0dwQvytqT6VSzY7j7AcwPmoL0XjHcfalUqnmqIGIukP8nOsBcgHcb/lACNETHZBIJF4B0G7FCwCWBUEwNgiCsQCWWR0A3GF9o4D2AjAAwMwPIHKOhVJqojFmruM4DhF5xhjXDuwvlUorbJ7RoRs0LzT5KquvUKdSqsTMXVbuALAe126itPp+lLO7yGaz65i5aIwxjuN8SVLKUxh6+Su0VGvdJaUsAhgFAAMDA1708La2tnqNjY2VVSpqrT0p5RIAO4YR43Td/VCUHMeJy0PDJkFEM40xC1EuiqOIaBUAMPNFAFttkN9CW3MSwL0AkEwmZwOoOvjJZHIO81VMJwFAa71TKcXGmLus/iUiGmnjfIjyFl5yHKc7modcKeUVlGtc4LruGFubrpKU8g0Ab1vxAoDVQRDsBQDXdZ8BsAXAaGt/U2u9PjxeKdXKzH+hfKFKWuskyj0UgMG3LABwpALOGPN0xI6BgYH3APRacTSA7a7rnndd9zyA7SEwf1rfKmLmWaG4h8Jg4gCBmfeE+EVRe19fX9EY8ySAoTrB08aYp+KyNTMvDIlfR+2DSkc6nR4nhPgdNj84jjM9l8sdjPrdTOmQUj4G4HsrloIgmHT27Nk/hgQEAEqpbcy83Iq/MPOD+Xz+UpzvcCmdTo8UQhwBUDnYH2mtV0T9al37t1BuOwFgKhF1ona7S0qpxUqpxUP5CCE+CYHpF0KsjXOMbT8KhULR87wcgEpdm+J53pSmpqae/v7+qjbEJr0uAB2e550qFotVbUg6nR7Z0tLSjVCGZ+aVvu//GBe7ZmLUWu8AsDmkmi+E+FkpNT3sR0QT4ngAUEo9KoQ4jOpyszGfz39cK+6QPbXW+jUpJQCssap7mPlAJpPZT0TdtlBWkc0zs5h5ITPPjIDf6Pv+60PFHNYzyPbXW1HO5mEyAC7B1jYAF61PdOX7AazUWnfhOjSsB1+xWDzW0tLSaYwZTURTQwEJQPg9PwLVP7IEYJsQYl4ulzs0nFg3/JS2eaoDwLMoP6Wj214iosPM/FUQBF9E88ztJkdKuUlKyfbbhDr/wKi3/TBEdLEiWN78n4DAzP/E8TdLt+IPq91EdMZ+u+ud7z/FMzYCP4IFTgAAAABJRU5ErkJggg==',
        },
        'dark': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAADIklEQVRIibWWz4scRRTHP6+nd0+SnZ3dnh97MBs9BEwCQowkkFzUi3iQrBJkySkixD9ACJhbPAj+BeLFgz9ISDao6G1PWdyDrgvGuQhBvUxPvc5Ub0JOmel+HqZnMzvObHYCPijo/va33rfqW6+qGqYIp/6OU39nmj7hNGTg7JT8gwm02ztHKOVnsOI98atkwWa9Xv7raX1l0gczE1W/gshV4OUJtG1MPqlWy7dFxA4s4L2f6/b4FnizgFKQdbB3i243wd4AygAGP82GrFYqlQdPFSiSbwDHwdSEq7XFypci0nXqDaBWrYiZzej99BLGNSAC7s6EnBsVCUZtKUZ+HKGZhXKqHi18ISLdgrJRNESkW4sqn2c9OYXQBE70enw1zpHdcK7zjlNvTjuu5f3z+5KHotVKDzv16tSbc+n5yQLqt516ayedD4bxJEmOOu3cKpKo086tJEmO7h2cv9wfnN8axuUJYecFJL8HpNVovjawxbmdF5H8N+DQyHgeBZKdjKLoT4Cm2exikiowl2dypNGY/xuG1sCC/HShuT7kOUj2GXDI4IbQawi9hsEN4Lncgk8HtGMij8HWASS007vdi60/bodu1KqVc059C2gEki1FURQXljVyK7WAuFatLO2XIxgD/j/RTvxqf5HSm8O4086aU29t9ddVta6q9bb660W1rY3lJv69AfZkBlmw2X+w181sZhe30kfAQ4ELRhgbYSxwAXgUSH5lQGuazYK8BpB3ZfM/AsXBtQ2U9X56aYDXauV7gWSvgq0BSb/ZWiDZK4MKAljU9H1gDthaWpr/Z6xNzqUrRS1rq5Ue3sfRPRHH6bJTnzj11m6nb08kmpm01f/o1JtL/B+jIuMunDhOl53zTafeVP33ozn3VJGI2GzIKnAX41gptF+d85f7/gL9UjwLfc+d8x8GJfsF4SXg91LJLo4KTDyuH/f4WuCtAnrQ30SyUsz1drGgc0WSH0olu7iwsPDwQAK7lrj0PGIfAycnULYsl2v1+vx3k3LsKzCIOE6XJbQzYnwDYMJq3pWfJ1bLs0ZRYWOvxkkx7V/FxpR8/gVYLrNRlz2qZAAAAABJRU5ErkJggg==',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAAE1ElEQVRIia2WTWxUVRTHf2dmKgWqw0w785BItCQstCoEBJVKgkUSPk0IsDIuIRJJ+AiiJsSVJAQ3KBgMLGVl4seCACYIm6IQEPkQYkJkTEwG3hv6hqktdOjM+7uYN2WmtENpOKtz7/m///+e++459xrjMN/344NlTgM0RelMJpOFx+WIjEe4VLJZiA5ER6lks8bDMS7hsj34rtZ/HLOxAjOZTHNLS3xxACuFzTVpHoDMzhk6b+JIf3/hZHt7+8ATEc5ms5NisebNgh3AlEfA7xjsLhb7v5o+ffq9cQt7Xn62TD8hnq+ZvofZDaSOCoNdRZoBTKzBZJCtdpzEpdG4R/0/nucvF+quEb2ItLZcGmgLYFMVF8CmcmmgzdA64GI43Y6p2/P8ZaPxj5ix5+VnC3UDk4GSsO1Oaso+MwsAXLdnAWanAZA6Haf114qriHvb32yyPUAM6EP21kiZPySczWYnRZuar4WZlmSsnppKHqnFSIp5ufxegHQqscXMSrXxWzl/lYkfQvHM/WJ/x6P+OZ7nf+p6vlzP161cfktDcANzcz3bqjyu6+8YHq/LOJPJNE+aHL9J5fReTKcSc6vb+2BhPUtlbEf2aoVBlwn4wnFaf67FSYp4ufwFYBaQ7y0knp0504rVeN3hammJL6ZaMtLntaKSzHX9g8KOIVsMpIAUssWYHXdd/6CkoUTMLDC0Kxwm4vF8V61WnXAAK0L3XrlcPFaf6Z33MdaHwxzGAYwDQC7cu/Wel3+vji8YPArcC7mX18Zivu/Hi2WbEwnKUcT8SnpkotGnFnieP5BKJc6YWQnTB5UQ/wal2Jxp0565DZDN9n4WjZX+AJ7D2AgclhTL5fJvSEGz0D+GvQh63XVvvxNEouUJUV0wN+f/iehgdPvaSSc3uZ7/H9BiaG863bq1fjd69grbDPQ56eTTrufvBz4cldG4Oq4G/yQs0hSlMzDrQsES4HcAxDUULDFYmE4lqiV1BSDA1mSzvW1Vgmy2t03YmnB4GcLahoUoWCL0V4VS51GwJDDraorSGQsv8VMAruefBeZitCso/eY4U/uHlij7BtObBtOjscGrbs7/vjJfWkvlhFcwQNhQuj3Pa4HYCxUCO+M4rSeGMq5L3zgauhMjkaa6PptOT/kWcSgkSSM2IjY+EOVQOj3lcP2GRpcDzaHQ0dpInXBfX+EX4E6Fx3ZKGoqbmRwnucHQMsxOUCmjHGYnkJY6TnKDmWlog6SIsJ3hovxCIXGSRuZ5/icPWmbP1obgBua6PdtrWuZHw+MPnepisf9LIANgsj23cv6q4RhJMdfz97uev19SbHj8Vi7/Lma7w+GNu3cL+4ZjRrwWXTc/C1M30AKUMe1ItyX3juVa9Dx/WygaBfoUsGDq1OSVMQkDeJ6/TPBdKA5wydCuIBg8quiEeRHpFEBg9raVi+cikaYV4T99JcT3GVqXTrceH4m/4dMnzPxHoL1mekDGDRMvAci4ZmIG4ekN7W8FrB4p06o17FyOk7h0v9jfgfgYyIfTzVVRgNCviAofseNuf+HlRqLwGM/b69c1IR7PdwlWyuw1k+YDyDhr4ryhI4VC8lTtnfvE7WYuv6haKjdz+UXj4RjXJREVwUj+49hDNTimj2K6NFjmKkAsqlHfzo3sf13HVqXHBabAAAAAAElFTkSuQmCC',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAAFwklEQVRYhcWXa4xdVRXHf+uc6e2Laefezjmn1KBpoSYkCsYPBo1aJVF8ECoFSWwHQoMJIgZITYzxEUt88IGKovYLIbbSaRNLfEDS0mBCkKj0W/HxhVhaFYbes0/vy+kMM+09e/nh7jtz7pkzt+NcievTeq//WXvvtfcRBiRjWtco6fMAgv/JMFx/epB83qCALOkdwGZgs+MHooEBiWqpiF8uDQRIVX1gOKMadrplk/y3AXHc3AL2NoTtwA1AHkAKnER5BrxfRdHImbcF0EStdpWf8rAgdxWAWIxSQQ76vt27YcOGN/5ngKqmdo8gPwVW50xt4C3ml20SWFMA+C1BvhyG5YOXq9V3D6mqb0ztR4I8mQWjcFyFXaUVEqD6WCbgsZUlL0C5E3guk2q1ogfiuPHDy+2xoX7GJKnvU+ShjOqUwANRWPlDVxHHtZ6YkZGRBjAOjBvT+KiiPwGuB0B0jzFNgK8uVnPRDhnTuLsXjDydtmc+HGbAuBSvFfMQhuWXpqdaN6Acnk+je6pJ7YuL1S3cQxO12lVDqbyKWyaBXwZB+QsionlfVRVjGrscgMOL+iTNo6C3O9W079l3j46OTuR9CzvkpzzM/J45NTs7tbuoEICIaBRVxqOoMt7PR+3s3cBfnGqNtf7eQt+8Io6bVyP2VdxJEfjIwmWCJEmGVf37VfUTiLwXANW/isjvRNL9QRBMLoxpbLOqLzoxVett3bhx5GzWp6BDdkcXjMLxIjBvnj9/rVX/FYVHELkRCIAAkRsVHrHqv/Lm+fPX5uOCoPx74IQTffHTHXmfhYCEWzP84bw5SZJhP/VOAFsWfswcbfHVey5JkuEFFpUjGX57X0BuRnzAienKITmeD0h16EGEdzqxJchum64IbboiFGQ30Ook412pDj2Yj7+4yjsGWCd+MD+XxJjWNarprSJ4wDqFb7iE/xbhBx2gTHRPUGzqp4D3daIZi4JKTxfjuD6GcMiJp6Kw8v7uSRThHZ3UfBM33QW+D0yqYkX830hs6q/Rv/2ufXJXFJUPxaY+CVwB4Em6Lr95a7XaunYqLSdORmFlXRw37kT0qcvWgDMDv4fyZK0tPPpLpSHBv0nF7kR1DZ0vv9/ZLgD7AVD+FoYj3aU5jVsyi38L9G58a/3tmWFyGiAMR8aNaSjCezrp+IrAWsf/TGAKkWlR70jPHFJV3ySNGTp3XLqy5AXubpqjqml8S9DvOrElyENpOnQMwPfbn1X0x8D6TjH59saw/L1s/OutVqU0myZ0DlQ7DMqrRCTt2nuWzBlOOtGfvWQ/k2+pL+3HUf7lxPWKHvD8S8bzLxlFD3TBIPzTl/bj+fjSjL25W1fhj1kwCwC5Nj0zz7Mrbw6CYDL17aeAfi/BM6nYTxdNa0R3zrGizy4w5xXVanOzePbvzF0dsi0Myy/l/ZZzdZxLGh/3VF9wYtumsvXKK8v/6AsIwJjGk4re48Q/p+2ZD23atGm6yHepVK1W14pXOgmdjY3yRBRV7s37FR57z0u/Q+dpCnC9P7T6oKoWgldVieP6WBzXx/r5iLfyF3NgYMrz0r2FtYuUo6OjE6h8KZPy8yZpHq1Wq2vzvsY0O5NZOGRMcyxvr1ara03SfBr0tq5O0AeCIDi3ZEAAUVR+CpX59zJ6u3ilPyVJY1uvp726mIdzSeNj4pVezoJBeDQMN/x8sbp939RhOPI1Y5oguseprrOqL8amfgKVIxdXeceYaffEvN5qVUoz9mZEd6J6U49ReDQcLX+9X80l/QZ13te6n84vTpYsqtOIXAGA6gVE1rCw81Oo3BdF5UNchpZ0l4Vh+aAn6Vb3O5QdZN4cGMDx2ZxtlCc8SbcuBQws41e6Wm1uFj/dgcrn6PxK55e9DbyM6G9t2/t1fs68raSqXhzX9sWmrrGpaxzX9qnqQC+IgYJFxNJ5FXTpgtP9fwABqMjFIn65NDAgD/8ocBY46/iB6D9F+XucQCY6FwAAAABJRU5ErkJggg==',
        },
        'active': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAABpElEQVRIia2US04CQRCGq42wMsxWNjw0kSt4CwNshD0Bb2LiSQBlgY8dK1wixtcVSMRENO508bmghnSamWE6oZIJ9F9//VVd1d0iHgaMgbFPjJeh5hOzk1K4DDStdRMo+xboihqgDjwSb1OgBhhf8QC4s4Q+gb617gMLa30LBD7iLxr4DrSAjPpWMwAyQBuYK/y8MYm2Jaz8FSg4/rVTBBSVC3C9KUHdqryQSF5PEu6kmkQMB9py8ApwpSJz/V9xOB2NfYgTP7AGmrHwQ+CbdfsBjixeFvhSXynE7XtwrL8jY8yfhV+ISE5EeiKS168nInsich6SjDG/IjJytFaDi7Kx+me6zlsxecVmmzRS3eStGMvrD3Dp4APFu8C+fl3FBjHc06gEZXUutjDkYtwupkpoO3iaY3qmsZOkNtWUNI+tIjquBHxo7EkS0bB8uGB5/YuOP+qpKAFvGjNMU03A8uFCq+oAWfUBq8cuq20JK38Ccmm3HAA31kC/tO+hDayBAgxTizuJqsCEeJsk9twjUQloWMINn0Pgk2g1g7S265nj3pMv/yJHNHDc3txvAAAAAElFTkSuQmCC',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAACYklEQVRIib2Xz2oUQRDGa4aF7GaFveQ1JB7EQ5CQSMD89ZLEk/oKGkmiPoWCa4Q8h7cEPClkDSSEjV50CXoyEHMYSRCDPw9Tw9Q082/HjR8MTHdVfV93T1X1rkgFAC2gq0+rCkclAOPEGK/C4VfU9jPeBw+gDswBr4GO2XEH2ABmgfogBYeBZ8ApxTgFngCNfxW9Bhw55GeaVBG6OmfRA0aris4CPw3ZHrCoJzBh5id0bkl9IgTATJWdRqK/gYeAb+xjRmDMzPvAisZE4uV2rqs/MqLzKT41oK1PLcW+YMR7pb45YSJFeFRqtek8jw3PepFznTh79+zxGp9pYBs41mcbuJ3i5wP7yvUDGMoTnjOrXHRsHrBJNjYBz4lZNvbsRCNsBBCWx7Bje2BIjtV3Q98j3HNimsSl9tIVawGTwBSwq06HOr6JJg/wXm1fgRETPwJ8U9s7natp7BTwUW0dHU8CLSHZDNLQVrJAx89TTuqF2gIdtws4u5fb4PPwH476k9o+JI7aWcQrdToDmo7NJtd30pPrvhNzBThXWzK5HEdbTkuOrUo53TX23HKyDWSf7AayRdxAtshuIAfKdUJeA9GAp2aVK7nO+TyrhmetTECDsLFD2OgXUnyKLok7wIVyfKHsLxNglLhuLwgbftlrcdWIBsDVUqKGZMaIR998mbANuj8EmppIB2Y+AKb7EnV23iOJc8I6j3BIXDIRPve90xTxBrBOeLUV4QRYo8Q39YoczAKGROSWiMyLyHURuaGmHRHZFZE3IvLW87xf/W+v/CIS37gKR9VL4k/G++WCAfxp+wuK32wv1FRk9gAAAABJRU5ErkJggg==',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAADKUlEQVRYhcWYwUtUURTGv6cSpY2Y0KJMSHMjtOgPEK1VtSkqcKFOSAUtbJUILVwYtBKlTS1EkSCNsCAKrDZCWLQMrZYqtWjTKnTGRTP6a/Hu4J03943z5o35wcX7zj3ne99999x7zyjFBNAGrJnWFpevKi6BpG5JLaZ1xyWrhKADIf2yEEsQUC0pYZkSxvb/ALQCQ8AnIEshsmZsCGjdSyHNwHSIiDBkgSngRKXF3AQ2HS/MAOvW83qI4E2gvxJCqoGHjhfMAz1AAzBi2UeAI0Af8NYRN06cHHOI+QJ0BHzyBAXGOoGloKhyxfQHiOaAWodf0vJJOsYPAjMBrltRxTSTnzPPAS/E1zNL1LeLzwuLLw00RRE0HVimQ5Fm5OasA5Yt3slSA0+Rv1M6QvwSwD1gAfht2oKxJUJiuizeLNBSiqAhK2g+xKcdWCUcq0B7SOw7y2+wFEGfrYAex3gC+FlETA4/XF+K/E2wuJuYanaWKws0OHyGLcI/+LvxqGn9xpbDsCO+Edgy4xkC51INfg1zRf5FWy8p55CWdBuQpF+SZj3PQ9I1K37A87xZ6/kJkJX01DxflfQAf/f1SsrtrLT8S7lG0n1gQ9K2pFeieC7YSJoZblg215LUW+PrxpakNKxWoh4q0BQnuEbSeUk9kmolHZY0YMZSkh6b/ndJuaVZkXTG9C9Z9hwuW/0V83fGCD1tnu9IqjP9R/KXcFPSszwm/KTOmM+XBY4EZ8AeJ3UBgI8WYa9jPO62v275fAiOu3LotdUvEOR53oakC5LWisxrTdJF4xuEfba9KcLhA2gh/+roDPEr5+o4Z/FmgJO7CjKBU1bgEo6yIyrwL9dvFu9ElOAm8suPOeKXHy8tvhRwLOqM7OQDv56pc/jtVqDVBcQA3IgkxiIbDxAtA10Bn2Il7Fnga4BjtCwxhrDaIQr8EiKJf6YEi/xG83XfO+JGgfi3A/6Bl3a8YIv8u22DnUPPRgrHcsYVdRyYJNoPxQwwQdQEjiisBRjEP9EzISIWgbuUes5UUFwVMGaJGSNmjsQK9jxvW35VkEPK2PZHkMHfkP7+gAr/S+8fecSGbIeDO7AAAAAASUVORK5CYII=',
        },
    },
    'handler': {
        'light': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAAB7ElEQVRIieWUMWgTURjHf9/lUNJJC0J895osAR2cBHGoIFURVDoogk66iSA46OCgm5PgoA4duzp2SJGia+li1w5ChJBcHsEMLtrQ4LvPwUtI6ZlrTxfxP717373f773H3Qf/emTyoVarHffeLwJHi8CSJPmSJMlqr9fr7xFYa8+q6hpwpOhuAVS1F4bhpXa7vQUQThRepfBVVd0qAheRMyJywXv/Bri4SwCcBiiXyzebzeZOEYExZkZEvgPzo7lgon4IoCgcwDm3nQ4PZwkOGjHGzFtry9NeKiqQKIqWRGRdVZ/+dUEURS+B+8BAVd8VFhhjZqy1D40x1Qn4c+ARMBSRG865jWmMcFoRWFTV1yLyuFqtLnjvbwHPgB8icjuO47Wc9bmCBrAOnPPefwRmAa+qd7rd7koeHHKuyDm3PRwOr6SSWUBV9Z5z7u1+4LkCgH6//y2VLAF3nXPL+4VD/hWNJcCDg4BHmTzBDvz6coqAAOr1+ugPHneDyRNsAAsi0rDWvs8CqOoJABH5lFUfDAbn0+HmaG7crufm5k557z+ISKXY/sf5GgTB5U6ns7lLAFCpVI6VSqVrqnoya6WIPAFQ1RdZ9SAIPodh2Gi1Wr09J8iKtfaqqq6QdtqMDEXkehzHv20Xf9JN/5P8BCuSrg5V9BjEAAAAAElFTkSuQmCC',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAAChUlEQVRIie2WPWgUQRTH/292wyaEQCKLxNk1jYeKiBoIWKhEEISAGIwQFEHsrKzstZCUQRAsxEYEC0GDphGs1E7k/EASTQgK8WabnKTw47L3Mc8is8dx3nmzuTON/puZefPm/ebN8nYG+K9NEtUbgiC4QERnmXlHo/mUYgDzAO4qpR40BUspbxDRpTZhzXRFKXXtN7CUcpiIssb2iYieMXO5HRIRucw8BmAbgDIz742iaAEA3Bq/QzUbGc3lcrl2oImklLuI6KNhHQOwAAAicRBC+ElfKdURKAAkGRrG1mq/U4C06hjY9/2+MAwnfN/v2zTw0NDQgOd5z5n5oed5NzcF7Pt+X6VSeQJg2Ji+2KxzW7s0VxiGPcw8C+CgMc0opa7arLXKWEq5W0r5KgiC68maTCbjAXgE4Khxmx0cHDwDwKr2rTImojEAIwBGgiDY0tXVdbFQKNwHcNzMP+3u7p7MZrMlm3iAZcaO49wB8M4Mz5dKpUUAJwGAmV9orU8tLS3FtlBr8PLy8irW/zoJfLtpXxaLxRNRFP1MA7UGA4BS6msd/I3jOGP5fP5bWmgqcAKP4/gIEZ2O43jUnMSGlLqcTIYzGwUmqmZcdwW2Vd91qsaqZdQa3xOt34phGE4BuC2E0H8I2GPaQjMHZiat9WQyJqL5aj/pZDIZb21t7TUz77FLJJ2I6HMcx/tWVla+AzVHbepwHMDbv8BdYOaJBAo0fsw5Usr9AA4IIRp+a2beCeCyGU4T0WIjP611mYg+9Pf3Z+fm5oq1c1avSCnlOBHdA9DbwvUHM5+Louhxq5hWdSyEOGwBBYBe49tSVmXjuu5UuVzOa60HWmxw1XXdWzYx/z39Akfd33OwH2Z3AAAAAElFTkSuQmCC',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAAC0UlEQVRYhe2XvWsUQRiHn3f3kCRFhCsEZ2dB0IgIKiKiWAiKEAQVRRARvwob8S+wsPQPsJU0KhYKih8g4kchNoKEQFAIUZu7vWkMKhFOjJd5LW4De0tibpNbP8BfNfN+zPswO8u8A//1j0kWclSr1cG+vr5NQH+PazaDIBhLkuRbV0DGmBi4KiLD8/l7pB/A3TAMz9dqtc8LAllrq6r6BlhdEkheE6q6zTnXnDNUsl5VvZyBmQFeA/Nu7TI0AOwAQmADcAm4OC8QsDcDd8A597THMABYa0+o6k2AIAgOZYE6PlkURbNAANBoNELAlwEEBGktAJ/WajvygZlxWTD5tTsY8kB/XPkz1DNZa4e897uB+865qW7zStmhOI63q+prERkRkVtFcnsOZK3d7L1/DKxMTQN/DMgYs0FVnwLV1PQxDMNzpQJZa9fFcWzy9jiO14rIM2BVavqiqsO1Wu1taUBRFB1T1Unv/fsoig7M2Y0xsff+GRClpq/AfufcWJH1oeBfJiLbVFVodwB3oig6WqlURlut1nNgTRrWFJGDSZK8KgoDBXdIRK4AH9LpCuBOq9V6CQyltu+qeiRJkhdLgSkMVK/XnaruyUGtTcc/VPWYc+7JUmEKAwE45+o5KIBZ4KRz7sFyYJYElIMap31mzjYajdvLhYFlXB3OuTqwpRcQWf11l2seaMG2oMS6fiEHIjIxN7bWHi+LxhizLzN9l/V1nCHv/UMR2QigqtejKLoANPm1KiKyPs2ZBFqLxPcD2zPz51lnRwtrjBkQkVHazffvkBORTUmSfJozdHwy51wzDMNdwC3ar46y5IFHqrozCwO/eAhaa/u991tZpJ8RkdPAqXR6Q1WvLwLzbWZmZnxqaurrvOstktwhY8zhIAiuqepgN/EiMu29P+Ocu9dtjaKX67luYQBUdVBEymvQVHVERKa7jReRaVUdKVLjv/45/QROlQlacA2i5AAAAABJRU5ErkJggg==',
        },
        'dark': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAAB/klEQVRIieWUP2gTURzHP793RxMrQnIhL38Gt6KDqzgoSFUElQ666KSbCIKDDg66OQkO6tDR1bFDihRdQxe7dpDiFLjmLvVS/1VSk/wcmksizZ96uIjf5d693+99vu897r7wr0uGXxqNRqmLsyBKNiEu7HTc5VLpSGOfQb2+dUqMWQEyCTfbR3U7eqFUyq0DuH0nY54DGYFlhfVEaNWTiJwzhpfAeRg6QRBELYSZL5+z6bk5aSXh+74/67jp70CrYL00gOlXhRmApHCAcrm80xum4jkzpneqVFWCIDpdq9UOTepLZKCqEm41FxGqqdThR3/dIAy3n6HcAX6o6pvEBr7vz4Zh854fRUcH8E9PEL2PsivotUIhtzqJ4U4qGje1oOgL5ycPgmB7XqR7XeEx0Aa5Ya23Mmn9VINuu1Vx3HQV4Qzafa/gAR0VbhZtdmkaHKZcUblc3hHal4AqggeooLeLee/1QeBTDQCstd96Jouo3LI29+qgcJhyRcMmwN0/AccaPkEL9r6cJCCAjQ2N/+B+GgzCDlYV5h0nVQnD6O1ohB7rdX8YWaV5tjdcG+LuqV6PTojhHVBMtP2BmkbMxXw+s/abAcDm5te847SvKHp8zOKHvefTUUUV/Wi0U7HW1uM5GdUYKwyjy6osxUm7n8iuCFet9cbGReI0/X/0C623qWdjww8vAAAAAElFTkSuQmCC',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAACmUlEQVRIie2WS2gTURSG/zMzbWxDax7OtJa6kqioqIWCC5UKBaEgFisURRB3rrpyrwvpUgTBhbgRwYWgRbsRXKk7kfpAqLYBs4iLdO7UaeuzTTK/i2TSqEkz6SMb/Vfn3vtzvnNm5jAX+K8GSf7csG33PMEzALZXOq9TFGAS4B3Lit+vCs7Yn68LMLJGWGWJXOowo1f+Atu220Nworj3kZCnQubWwqKIIeAAgK0Acprk95qmOQUARpntkF+IobMvHo99WgvUl1Jqp0f9AwDD8/R+AFMAoJWw5BY/jsfj6wIFAL/DIsXyI62SuRFaN7BSqm1mxh1SSrU1DDw3Nxf1qD+D8IEH/UZDwEqptsWs9xhADwAIkd5wcDqdbiH1cRAHCzscM83o5XUDO46zK2PPvrTt2WskNQBIJhlqDoUfEjhaYGLcMmOnRSTQ7Bu1LUCOMiCQXgK9tnJjqVTqQmurew/AsaLlycJCdLijQ7JB8gEBO97UpN8G8La4PNcS3jwNwYni+nk+9/NkIiGLQaGBwZFIxM0uGf0+XIBtKAQvNMkf7+rq+l4PNDAYALq722fL4QBeh5q0AdM0v9QLrQvswzXJHwHllCb5vkgk4q4GCgT8uMpV7HBstUBfyz8J0UpjQLLugqqpPFc5Yxng4Z1fhu24o7Y9f4ukVy2h5+VbAEDT9B8rcEUpd7i0ICZLsR8kkwy1t7uvINgdtJs6lRLk9lmW9RUoe9SJhCyK6IMA3mwAdEogQz4UqHCZI6k7zvx+Mn+AIlXetewAebEQylWA0xVdZI7Ee8eKTewRWfrtLEi5mYw7KBrvAgjXsH6jJ2c7O6OPauUMNMei83AAKACEi96aCjQ2zYaMZrN0CERXLBBwmwy5GSTnv6df+ZjwLy4YcboAAAAASUVORK5CYII=',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAADCUlEQVRYhe2Xz4sTZxjHP89MTHf3sLvZMIk7S6BgLYugUpZF6UFQBBG0tBRExFYPXkr/gh48+gd4FS9aPCgobYUi/jhILwURQRRE21Mw3Zn8WHeFWLOZ9+nByTITYsxkM7QVv6d5n1/vh+d9Z+Z94YP+Z5K3Oer1+mS7LdvBjI94ymar1XxQKpVeDQT0vF4vZQI5Bxzo5R+R1gSuZbPWd9PT08tvBSqvrMxkXwePgNmUQGJSeGLafy+4rtvs2DLRgGwrOLMOo7QQ7iHSs7XDU+gEsAuwBeatzNhp4IeeQKjsA+0MDhUL+VsjhQm1VG0cE+USgChfRIFiS+b5jQCwAApOzhYRkwaQqlp+dTkIh6ZYmLE7Pqsrdn2cFkyP2jGGbqB/XZl3hwwn31/ZamjvMe1NP7vuZG3QvFQ69FettqgE9wQ5b9trl5PkjrxDntfYQcANhCkARCaS5I+0Q7VabR7RWwgzoalqAj2VKpDvr3xSrVbdbrvnvdgSGOs2SCE0vRDkwOxs/nFqQL5fP6IET43afyxVG4c69uf1egkxt4G50PRSjR4sFHIPktSHhHtIRRZQBBgXw9WlauNrm+C+CbgDfByGNS2Rw87mmd+TwkDCDlkEZ4E/ARCyYrhq1P4NZGsY8hrVrxwnd3cYmMRAjuNU2rbujUIBW0L3moocKRbzN4eFSQwEMJfPl2NQbxQIenyzk/tlIzBDAXVBPQSaKCcLhfyVjcLABj6Mc/l8Gdg5Coio/nM/126g9WOBqqYG21U7dsyJT6o86Tx6teWjaQH5fmN/ZPgs6ovvIeE6sA1AlIue3/gekSZ9JGhGlU8BRHiqSLsvjeo4ymLEcideL6JKpTJhZcbuC8z3LTo6VVof2dtLU1ONjiG2ZK7rNsey1ucCl1FaKYIYhV/btu6OwkCfi2C5XB7PZic+A33XeeZbxPoGADU/Ahf7h1uvLCt46DjOy17eRDdTz1v+EtELwOSAKauonCgWcz8NOkeiV1tFTyWAAZgMcwZWIiBROQ+sJkhZDXM+6P3VPzhzCH3Btl9PAAAAAElFTkSuQmCC',
        },
        'active': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAABQ0lEQVRIieWVMS8FURCFzxWJeNEoFBqFhoSSEApBI08UKlFo/ACtqGkUSvFD3qsUGgmNVqFQqMh7QSc2xKcwGze7e19271Zimsmdc/c7M9nsrPTXw/kHYFTShqThSF5HUts5180pwBzwSv14BKZyEwDXkuYltSXdRk4wK2lF0oVzbjU7QWIdDETCBTSM8V4kAhALD3H6aoAcsAgMRjmXgJ/ZI0elOBUNTuz6G7AQbWAvbQ8Y82qHdjUB1ko3GjDYsvIDMA4c2PkD2AxMV3mCS5OeLX8C20XwygZWH/JMvoDdEDzKwDM5BXZ6waMNqkSW439oiV1o1ICnayZJa/2efiVpWVILOA8wJizfBfQlyzdF7tP8rNq68QLMpNzsD2dE0rqkyUCH+5aPA/q9pJZz7img56Zq8rvGiyIBmr0Y0dv0/8Q3+Mscio8HJUIAAAAASUVORK5CYII=',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAAB6UlEQVRIie2WP2gUQRjFf18MhBgDnoggaKWohWACgo2CIAoBIWARtLMLiJ21mMZSBMFC7OwEFUkjaGNKkRBFjH+KFGorERIUNPizyNy5LLe5udyfRh8sO/vm8d58szPDwH/0CVEm1IvABWBfs/42IbAI3IuIB9UqvWXvcLVpxeo4MJ+4JeA5sNZhxYPABLA7eR2OiA/lai8XRrenw8Ci78GC76U6P1DQ7Kw3IuJLt4JLFe5qFtxXdC1YHVXPqaN9C1ZrwBzwELjdl+BU4RNgPFGfex6sDgOzwLFEPQKudS1YPaS+VG+qA4kbAh4DJ5NsFjgfEVl7fzBHxPohcDQ9O9Rp4D5wJvU/BaYi4lem31+oM/Vd3qSvpr4qHASfCu05dWsL7zpm6lzWVEfEMnAKeJ2oven9AjgbEd9zfIrIXlwR8bUUvgBMRMRKu6GQ/48b4eoJ4DTwbLOhbQen8BXWt01HKE51YxuobQ+oCiWvRkaRfFNoX1fvAr838BxO7x8baAKYKnwvNhvZkPp2c5eLLCyp25oOT92vLvQg9L06Vp6KcvgW4AgwRvXiOwBcSe0bwMcK3RrwDpiPiJ8Vmmqok+pqRmWr6mSOZ+4BchwYydCNJG1LZN2b1e3ANFBrIV0G7kTEtxzffwt/AJ4VGkAXNbVSAAAAAElFTkSuQmCC',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAACLklEQVRYhe2XP2sUQRiHn0nSJIWi2CSQyj8EJUKQYLCxUasgqYKI2lj6CSwsLfwOVrHR2qA2fgIRiSjEKNiIoIWaiBcNyT0Wt3fMDXe3e96u/8ivmnnn3ff3MLvsvAO7+scUui2oe4BpYLRkzxrwLISwWShbnVQfqnWr05Z6V92X+ocEZj/wAhgvYysKaBU4EUKoNQMjScLNCGYLeAIU29riGgNOAsPAFHADuN4xU30VbevZkkFin4uRz8t4LX1lO8BQNh0OIdQrAhoCdrJpPYQw3FwbSnJb86pgOtRuY0iB/rgqA1IPq1fVA4MUaWlAmFn1S1bqcT9epe+Qehx4BOzNQmODFBtoh9Qp9UNU5qN67Je9iiSph9SJDvGD6ruoxGd1ZhCv3CR10cYZV1Pno/ik+jZ6fEOd62pUItCtKOWHOq+Oq2tR/Jt6uhdMmUAT6psEKp5/V8/lwZQGlOVMJhBNbanni8CUCtQFaltdLApTOlAEtWLjm7nUD0wvr/S0by2GELq2t2Wom9dff7i22gIbPUslSmq3tTmp6Wo0vlAVEHAmGr+OF9Ke+j5wNBsvqddoXFt6aQQ4ko3XgO2c/FFgNpq3dQPpRz0GPKXRfP8OvQemQwifmoG2V5ZdR04B92jcOqpSHXgAzMUw0PvmOgrMkN/PXAEuZ+M7wFJO/ibwPITwNScvX+qCut7h2OimdXVhYOMeQMt9wDS13I9Hv/+a28BGH/kb2TO7+n/1E+rxI5fdf+XQAAAAAElFTkSuQmCC',
        },
    },
    'advanced': {
        'light': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAADzUlEQVRIicWVXYhVVRTHf2vfe8GPg3dCkbnnHO89GWSFYHJBglIa+wKlMAJFSNOH8NXXoMcewkBEQ4he+ngIBDFMwwKZ0ILoYcZErCTqej333Dui43DO4Ahz7l49uO90HBQnKFqwOez1/6//3mvts/aG/9hkoUTf958TkRqAqnaTJPn+X1ugVqttNMacK/qstZu63e75h8Wa+zkbjUYtCILtzWazAmCMecVBF92Y8zWbzUoQBNsbjUZtQRnU6/XV/X5/FKgDv6rqt8aYPaq6DHjV0b4SkdRa+4mIvAw8AbRLpdJIu93+44EL+L5fF5HzTvwOsKgAn1XV1wBE5CTwQgEbcNuqujFJkvYAKBcXMMZsU9U6cF1EngR2WWvXGmM+juP4pwL1xTAMN1hr3zbGXAI+V9VfgLoxZhtw+EEZrBCRK8AjIrI1juOv55fwfhaG4RZVPQ3cUtXHkyS5MbfpIjFJkhsicgbAWruxsPCaIAiOB0Fw3Y3jvu+vGeADroicKYrfk0EQBE8DI8B7wOJ+v7+21+tdXrVq1WOqOuYOuWjTQLPT6VwZHh5+qlQqXQJmgHeB0U6nc2EugzAMtwDjwEFgCXCq1+tdBuj3+x848WOVSqVWqVRqwDHAU9X3ARz3lIs9CIw7zbkShe77M7BnZmbmzbkURZ4BKJfL+1utVq/VavXK5fL+IgbgYvY4jTnN+Y12U0TGJicnU/6hTU5OpiIyBtws+g2AtTZz882qejEMwyMDgqr+CJDn+aEoioajKBrO8/xQEQMIw/CIql4ENhc1B4dc9n1/t4iMADuBvFwuP3r16tXuQg650WjU8jz/k7t99YWqjiZJ8hmQlxzZZlk2nmXZiWq1uh5Ya62dyrLsXJqmtzzPOyEiPrACuA18o6o7kiS5AuB53n7gJRE52el0dmRZNg5YmNfJURQtyvN8nUu/O/AnSfIb8MaD6q+qXREBWBdF0aJWq3VngJWKRM/z9qrqbuB6rVZ7C3i2Wq1uXb58+e9TU1N3itwoioaWLl261/O8xb7vn56ent4HhKraTtN0bMC75y8SkXFXgpUTExMX3BtwdHZ29loYhgfchkphGB6YnZ29Bhw1xpybmJi4AKwEbjuNvzXnpxuG4fPuXlkCTIvIuKoOroIRV5JRNz+vqusBz4lvjeP4u6LePSUCSNO0Va1Wz6rqzVKptCuO48PVanUFsIG7zbMJWC0iH8ZxvH1oaOhTa21ujHknjuMf5ust6Mn0fX+biJwo+lT19SRJvnxY7EIffRMEwT4RWebE006n8xHuV/xf7S8lfLLbucHGDAAAAABJRU5ErkJggg==',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAAEVElEQVRIieWXX4hUVRzHP7/Ze93ZgQazLfP87soqlpvSFvkQopIQlSDogw9WouGDmD0pvvrqiwjtg5ghERphEQo9aWiQEOZbadhqWgy7e67NOOnCgrPbzHh6mDN69+7dwQ2ih35wuOf8/ny/93fPv9+F/5vIXAOMMR+IyF6gy6uazrmhOI6P/pvEOVW9AyxI6e9aa58GHjw2UCebMWanqm5sK6IoWpkgPe8bwAJvA0BVNxpjdnbCny3jnKp+Cuzw44vAl8D7wCCAc24AQESue5+rwDFgK/Ca15201u4k40tkEadJs2TYWrsSQFWvAS908M0kn/EpjDH7E6RXgCGgDuCc+wM40NXVtQZwgPP9A96G9x3ysQA7POb07GYocrn2akVEjlhr94nICufcpkKh0G+tPTgyMnKv7TMyMnLPWnuwUCj0O+c2icgKH3MkC/MhdlrR29v7RHd39w1gEWCdc8/HcXw/7ddJjDEFEfkVUOD21NTU8mq1OtEx42q1OuGcO+GHCgwk7VEUbVDVC6pa8e1CX1/fWymYAR+Lc+5EmhQyMl64cOEzQRD8ACwFblprB2gtDDHGfCwiu7KydM4dj+N4N625z6nqdeA54PdGo7G6XC5XMjNW1d2qOhwEQdmT4pwb8qSo6vYE6R3gI9/uAIjIriiKtnn7Ax8LsDQIgrKqDqvq7hkZq+oUMC/xUjedcy+351dVLwGrgVHn3CtxHFcBjDG9IvIjEAGXrLVrvL4gIj/5rNvyl7W2e1rGCdKzIrI+DMPB1KJ60T9Pt0kBfP+0Hw4m9PfDMBwUkfXA2RRH5pFWyufzl0ul0mSGbU5SKpUm8/n8ZaCUtiWJ2yfLnlqtNq6qJ1P2n/1zizGmt630/S1+eDWJraona7XaOLAnxTEN+PNEPw9sN8ZsTOiO+WefiFxT1aOqelREfqE1v0kffOx2jzWDY9p26u/vf7Zer68FjgPzgYvW2vVt3zlsJ1T1O1qXxTiwKwzD70ulUvtYzb6dVPVDYK8HHIjj+EbbFkXRBufcfuAlr7qSy+UOj46OfpPIdnni1hqy1u5LcwQZvDkRWeecA6g1Go3xpHFsbOwccC7rhdvSaDTGwzCsAT0iso7WlHa+nVR1m3NuFYCIHKpUKmUgWLx48ZOdyAC8T1CpVMoicgjAObdKVbelfWcQi8jDveicW2KM2ayqw81ms2qMOWOMWZuOMcasNcacaTabVVUdNsZsds4tycJ8qMvI+CngWx7NYZZ8Ya19x/ufAt7u4HsFeN1a+2dH4lnI79Kqrzbjt0cYhosA6vX6be8zCXwNvMGjuiyTFB6VqNNkYmKiViwWvwIWich5YKu19rNisXgJeA+g2WzOazab60TkVR/2prX2cLFY/EREcsBvwLtZpLNmPJssW7as259E+ZRpsqenZ/6tW7emHherU3k7QzzwqQzTqbmQwj/4k6BVb0dBEAQAjUajEcfxGHMo5v9T+Rvr/7QrPfgW4gAAAABJRU5ErkJggg==',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAAFgUlEQVRYhe2XbWhcVRrHf8+dSWZKWmNr6zBzrrVqde1iVQzVBRVL2VYkrYIW0WWF1f0iVqoiClrcBQtWuiys0oqIaD7IUqRVKYpiFawIVmhEau1iN5SazJmZNAVfJmnzdufxQ86NJ5N7JyWCn/zDMOc8r//z9txz4He0hvwaZ2NMt4hc5ctU9ai19r3fnJAxpht4N0W9cb6kgvkSAm6fp64lsnMZlEqlm0RkM7DHWnvIU93stV93//cn6DDG/Am4R1X3ViqVz1rla7lkxpiNwD6gHVBV/a+IvAb8Dfir8x8XkfOZMvghtgXeAHpU9QER+UtsC9xlrU1b6nRCTWRa4aC1dq3z+QS4ZQ77lqQS95AxZl0Tme/TyERR9FDcce2DKbZxjHZgn8txboSAxzwy/ap6TRAEt6jqYaZGuCcIgjXW2rW1Wu1Y7FSr1Y5Za9cGQbAG2AOMq+ph53sN0O+ReiwpceKmVtWDIrLRdRcGQTAyMDDwKbAmZQAzMDAwcBi415eFYbhEVRf6OZJ8E2do8eLFLwJ9rruk0Wj881yItIKLscR1+1yOWUjd1KVS6SER2e26/7fWXuHrly5duiifz29R1fXAaif+WkQOjI6O7j59+nTdtzfGHAcuB1DVLZVK5aWkvK0K46a4oaof+Ipisbgql8t9pao7gHXAMvdbp6o7crncV8VicZXv0xRjEymYMUNhGN6gqutV9SYR2eD0USaTuaK/v/8ETM1MLpc7CixvMRiA78bGxlbHM7V8+fJLoyg6DmSm+OmHIvKZiBwol8tfxE7TM2SMuU9VDwHbReRWj+zbMRmAXC73iEfmR+D+KIoujKLoQqYq9Y9Od7GzBcDFeDueCJdju6oeMsbcN4sQcGPCKL8FtjXJ7oobIrLFWttTq9WGarXakLW2R0Qe9mzvbPLd5mI2Yzq3T8hfvpejKLrEWnultfZ4k/PKuDE6Orq/OfKZM2f2J9kCWGuPW2uvjKLoEuDlpNxpm/pIrVY7maJriSAIdC4bF/tIon+Kzy5jzJEwDHcWCoWOJl1cn8jn87OuGfl8/o4kW4BCodARhuFOY8wRYFdS4ulKLSJV1enBBcBqVV2dzWYngac9n33AtQCqutsY0xZF0XsAmUymW1X/49m+NSNZNrtNVZ9oJiEi1em2zz6bzT4N/Bm4ziP7w/j4+EVDQ0PDMP9jv2zZsoXt7e0DwPlOPwl8CXw0OTn53ODg4MgMQj4cuV7gD24Ej5bL5RdifbFYXBUEwbvApSlkTjQajY3VavV/sSAMw0e82ft2cnKyKybhI5MUbWRkZKKzszMAbnOiUr1enz4Vw8PDp3O53OttbW0/MbW8C4EzwBci8srY2NjfT506Zf2YixYtegUouAFur1ariTfH1Cusqm7wuuVmvVuK593vXFAGrvZiJ35cE09ZGIa3Ad2uGzG7OM4H21wsgG6XYxbS7kMPet1vrLVHC4VCRyaTeVxEulR1T7FY3Nvb2zuR5N/V1dVWrVY3i8g9qtobRdG/rbVHjTHf8MssPQi83+ybuKlLpdITIrLTEx0A/ggYT9YPvNjW1rb75MmTowArVqzIT0xMbAG2MvMUWuAYsN4b9JOVSuVfzbkTN3W9Xv+8s7PzAuB6J7oMOK/JrBPY0Gg0LqrX6+8AdHR0vAo86XQ+znMxABCRXdbaZ5Jyp1VqLZfLW0WkuZpWgBeAQU+2ycUJmHnPGXS2FT+AiOwql8tbmXoqzcJcT2kxxjwL3A3sHR8f3zE0NDS8cuXK3NmzZ7/DHWOm3mkAPTGZBQsWXNzX1zfmCuJTwGbgTWvtP9LInAuhVBhj9uJdRZqwz1q7eT5xf83b/uN56lpizrd9GkSkR1WvZfbn44SI9Mw37u+YCz8DZtg1hQG902MAAAAASUVORK5CYII=',
        },
        'dark': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAADn0lEQVRIibWVQWgcZRTHf29m18RQQnYnM9sNEoiC0VKooogHE00UhRYlIhSEVnoJkluv0hwFLUIptiDiRfQgFEpFWymCpBoPIpLYUKL24EIP3Z1vsjMhhtqYne952E2c3awkgn3wMcx7//f+7z/f972Be2yyV6Ax8TOg5VZaNQiK3/9vBMbEYwrfdSSOB0Fxfrdcp5sziqKyMfWjqppvuV5qPZdaa9unqnlj6kejKCrvSUEYrj4Idg5hWOFX0K8FOQH0q/AygChfAmuKfgzyosAjKLfAmSiVBn7/V4LbcTzsbjKPMAzcBXr/Qeo36ebGKwBuvucLVJ7PpDaxyq00z9hQsXhrK5DLEuQaMqWiw6Dmr57co/kNe9yBg6mTflQeHPwxA32hurLylGvdaQs3NnucT+/baPyCyHCuIVPA+90V3F4bdHONm0BB4EgQFL/q9l07zZj4sMIVIEkbuYeHhvpXtmJtmzw01L+CchXAwtiWP4qi0dDUL4YmNs1VvxhF0ehWfBurXM0Wb1NgTPKYFTshKm8D91tXD5Y9bzkMVx9C7ALQ39H4uiPpE77v36zW6wecVG4Af6rorKPOXBAUft5W0JSoi6JyBugTuFz2vOVmC+l7QL/CBaFRFhplhQvAPqvOuwBlz1sWuAz0icoZRReNiQ9vE6jwQKur64KccF09lhH5NIAr6ckgCGpBENRcSU9mYwCuq8dax/l6tmb7RVOtW6sLnuet8R/N87w1a3UB1XrW31QAfzQbkklxWKqZ+FyG9QeAVN2zxpj9xpj9qbpnszGAmonPicMSIpPZmtJsXHNRFL+hIhMorwMNR9IR3/ere9nkKIrKVt0KkEP4TFTnfL/4iYg0dkgNo/hSaGKtmWR2y7fbMa2ZZDY0sYZRfKmzXttNrlQqvSiHmm+2uuX3ff834LUd3WybrYKAcqhSqfSOjIzc7QqrRfXp0MQamnrYnJLxWBjGM0mSDHRikyQZCMN4xph4TFXzoamHoYm1FtWns7i2URFFq09atd8CfSjLCAdaoXWED4LBwlsAZiV5B2UG2AeQwd5xxHnW9wd+6koAUI2S5xzVK0AfsA4s0hoFVmQCwFGda8HngcdbRHesyJGyX7iWrbfjh1P2C9fU2kngtCPpaCkojiucb4FPOXCq2TTnS0Fx3JF0FDit1k52Fu+qoJuFYTKFaPsJUXm1VCp8vlvunghU1TEmeVOkeRdUWQuCwociYveSf0/tb0z4xVDZMjiLAAAAAElFTkSuQmCC',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAAEsUlEQVRIieWXT2gUVxzHP7/ZTWIF4+7GnU3WFIyQapXGUk+iUttSKgh68NAWUfAg1h6KwUsPXoUiBUMRtXgoCsX2oNCTFu0foVhvbSw22kARajc7b7Iz44oxyW7m18PMxs1mN00KpYf+YGHf79/nfd/Oe28W/m8mSy1wHO99RI+CJCKPzqAylMtlzvxrYFW1jPFdhMzcAJ5tp7MiEi62l/U3kIOu6+2q+RzH3/gMKtejDyBkHMffWMtzXW+XMf5BVW3Zv6liVbWM638GHIgEcVOULxHeAwYALJlZDxBq4l5cdgflnApvC7wa+y7a2fTBZisxD9wIbTHbkWw2vTFS599VeLFVbiv4vKUwxj9WBx0WdAioxOOiwvH2dmuriKiIaHu7tVXhOFCMcypxzXA8PhD3nGPzwCIkat9V9LRtdw0KiQ0q7C4/Sq/ptjMnUqmUX8tJpVJ+t505UX6UXqPCbiGxwba7BlX0dLOes75Gh+u6K0JN3Ad6gD9nqpMv5PP5ica8haxQKCxPJJf9BqwGxiyZWZfNZh8vqDibzT5WuBAPV7e1LV9fHzemtNNxSzcc4xnHeMZxSzccp/RWfU5csxpA4UIjtKniYvGxLVblR2At6KidzawXkVBVxRj/U4RDTWUq5207fVhENHpAvXsg/cDvGrZt6e5eYZoqdlzvcNF4I2JVnAgKqAzVnkZjgv11UBfhLMJZwI0lHDLG3wcgIiEqQ3HuWrEqTtF4I47rHZ6n2HG8KYT2OgmjM9Wpl2u/r2O8W8AWhT/CavKVfL5zHKBQKK9KJKs/Ab3ArZyd2Rr5C8sTyY6fY9W1VZnO5TIdcxTXQa9aIjsmnpQHGh6ql6ICvVyDAuTzneOCXo6HA8/8+YmJJ+UBS2QHcLWB0eTIFB4EQep2X1/f5LzYEq2vr28yCFK3ER40xurB0cmiHOlc6QeO611sOGt/iZJkb6FQXlVzFgrlVYrsjYd3an5VtRzXu9i50g9QjsxhMHepP6+DLEPZ74z7sxcEKueiNJ5PJCt3Hdc747jemUSy+ivR7zubA+CM+7tQ9gPLmjHmbCdjTDcktilyHkgp3Oy2MztiBYveTgBF430fXxaBoIdg5gfbtmvHavPbyZjSKUWOQnQLZbPZ+3WxnSrWMVQ3RR1kmDD8OJfr+rqW47ruutqtJeiQbXcNNjKS8yYe3U7b4+HTMAyD+rhtd10DrjVVHVsYhgGSeAo8p8h2VbUWcTsF+4DN8SxO5nI5R1WTQRCkF4IBBEGQVtVkLpdzUD0ZuzfHPefY/O1k6exeRKSvWPT3GNcfmZoOxx1TumKMt23+ZL1tjildmZoOx43rjxSL/h5E+pr2rLVudDx8WO5qa69+A2xqpUzgC9vOvBtDLym80yoXGK5MJ9/o7e0sLQhuClc8Ea4r7CHeHkK1Jwolx+KySYGvVHmz7mWwKbQleBbeUT0FOJWp5Ee9vZ2lMdd/zVL9NgLqJ1ED+QAgFHm9J5v+Lq77EMhVppKDzaALgpvZ6Kh2dK70A+oPhVht+VE61d8vU4vt1fL1s5n198uUopca/YpeWgoU/sE/CVW1xny/N1GxkgAzbWG1J51+uJSX+f/U/gKsszhGTeEf0QAAAABJRU5ErkJggg==',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAAFl0lEQVRYhe2Xb4hc1RnGf++5s2PpbnRndu+dGaXKSrWmNjUoQcFIQjBK2GihhqJSafSTmJJUioUYUKjQSESMIRE/6QpFgiQVRKk0VZrSDxaiSIyRxJBotOv9Mzuza7OlGeee1w/z787snZ24gp98YOCe+z7v8z7nPeeeewe+x+KQb5McRZVJVX7WJSgcc938G9+5oSiqTFrl9bSYETYu1ZRZqiFruXMpsUEYaCgMK6vDcGa378/clLwvwi2ta0VfVPTFtBiA78/cFIYzu8OwsnpQvUWXzI8qG8VyECHbqMvL6pgXRO1mlF8DglKr1eZHAbLZ4dk2V/izipmS2D6AcG+Lq4a7im4+dakXNdRjpi8UDhe9/FoAP6z8Q2DNYvxBplKXzC/PrusxU+1nRh19qD129CGFw32sNDSErFgO+uXZdRdsyFj7cNuMcrbu6HWCrFH0CEpNYL8Rs6ro5deWxsaOt/JKY2PHi15+rRGzSmA/Sk3RI4KsqTt6HcrZlilj7cNptTOpM1c9jMjG5nDEZjLz3tgl/wRW9Zl9F1x39AhwT/LeZ3NzeerxSFeNtGak3Sx7+T3AKQCE/ND5+uMXYmQxDJ2vP46Qbw5PNWssQGqHrhWpBUHlGYR9DU9sALYlOVEULVN1tqjqekRWAKD6gYgcEon3ua773yS/qdGA8sy1IrW02v3PIeGOTr68mQxNl8vLrTrvK+xEZB3gAi4i6xR2WnXeny6XlydzujQS2gvLJuD75RsxznpBVwO3NeMxaq4uFEZPQ6Mz1jrHEC7vO5mG8qeGeEWrU0EweyViTwIOoMDfFPkXNj5ULI7/u5XW7lAQVO8TY94R9Ang9o5ZebVlBiDWzLaEmTlB7rfxkGfjIU+Q+4G5ZkuuiDXTXuaGhryaaMTtgj4hxrwTBNX7FhgCvTllnieM1Hd0T1zvSgy2eF5uqlRaFpVKyyLPy02h/DbB/WUyt6l1YmGZTu3kHpLE1fM2lomCl7/Gdd2TPdk/7iTHr/VKZzL6WhoXwHXdkwUvf42NZQLh+bTa6ZvacrRUyn2SGhsAa60O4pRKuU+wHE2LpRsS9gZh5WgQVXb5vj/cEz3VLo6z4DPDWucXaVwA3/eHg6iyKwgrRxH2ppVOnEP6RU/nVqCswGTrwKNtFnJQ0JXNwb4wrA7FceYNAMepTyq6O8H9S1c1k92B8shCG/pF66rtwPf9YWOyj6rKrYhenzA7K9R/5HneOVj6Yx+G4YiS+QwYbTLqqLwnon+3tvanYrE432UoCd/3h8Vk3wV+0pzp74pe7tlWfLpcXu5Y8zpwZR87p2NjN146Pv5RWzOsbpNO906ord3QMpFE6h4qFovzgjzXmaw+kIxfOj7+kZF4pcB2VN8GIiBC9W2B7UbilUkzvRqCPJdmBvq8ywAselurfQqf98abS/Fk8zcQCp8L/LylDaS+XFM7FIaVDQKTzWFskB1pvG+CpkYMIDAZhpUNabw+30M8mNhdH7ru6DHf94fFuej3WL1BDfsL47kDIvJVer4OBeXqJrHcjZF3NT7/tOuOHguj6oc0u6TKg8Bfe3NTN3UQVB5B2JWgHQL9KXBZpypnMbrnf+e+3DcxMfF/gDNnzvzghyMXb8HK1p6n8D8gx0HXJ/L/UCjkn7ogQ6oqQVTdI3TeS/2hLxW8sc0AQTgzBfKbgRmwt+DmtorIglM9dQ+JiBbc3FZlwWk6LeizIEFHXe5QVaOqBpXEd44EDS7TF2oGBvwvU1WJosofFfMrQQ9Afafneec+/lgvuviS2U9BCw0R2dwoplMtM1/OjV5x1VVyPgzDEchsV2STYF9x3fxj/cwMNLQYgrB6gOSnSLfswYKX27QU3SX/t0f1rSXFBqDvwTgItdr8VDY7vBIj3a8Pq6drtfmppep+j0H4Go2rVXafVZUSAAAAAElFTkSuQmCC',
        },
        'active': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAACDUlEQVRIibWVv0scURDH5/kDJIVIsMhaWCgo2Ggh2mThOuFIIQQCVoKgIY2S3j8g2ImVWKayUMFKiJVYiwRCSGmjXEiEKEcsxE+K+26Ye7e7WcQMDMeb+X6/M2/e23dm/9lCVSDw0swSLa9CCKdP1gWQ0mlpFW5XgWACvAF6FZrT72f53xjQK2wS6xR1OwJcqMuvwCbwS+tXchTbFAZxRv4lPuzEf0cjOQaeyY+jXIa9AIbLCqwK2ACeA2vADjCTg51Rbk3YhrirZQUGgWsB65Vm2uLVxbkGBn2u7ZBDCD/M7EjL1AmMA3vAd/keMO6oGfZIGrldTAHvgSbwAEwoPuoO2dstMCbMhDhNaUwVbTGzQ5fbV2wXeCHfVWzf4Q4jjbovsKLgObAI9LvcpXKJiyWKXbpYv7jnyq10nIGZ/TSzsxDCTe4cS0ycM2m0G7AQbW/rESPaijQWfIEeYAn4CNwDd9lIqHbIiTj30lgCenK3ChxIYN3FSq8psC7OQazXVgXoM7NJLa/cfL+Z2ev86bdhJ4G+EMJdUffL6qRB65VMgXfAQA52QLlU2OypWC5sA5im9bEAfInmvQF0yzcUI8I2gemSnZoBNVfkFjhxQjV5ZieuUBOolYq7IrPAB2BI6+wKfpKDrjIwJOxsJfGCgvN02nwVbqU/faDLzN6aWfaE3JjZdgjh4XEtP6H9AR4ujlyhwzJbAAAAAElFTkSuQmCC',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAACjUlEQVRIie2Wv0tbURTHzw0BRXAQRYV2EQRd7F8gFKcKIl061kJBkGYXhC7uDrZLB7dkSSdHh+rg0uLQIS4GhEJppVJRHAJNI7WfDn5fe/Jyk7yUli79woW8c74/7s17795n9h9dABSAY+CdxjFQ+NuhOeCCVlwAuT8Z8hhYcLUZF/ZKI8GM4y1I29tkFFp0pvvAE+DQ1aY0EhyKs+9qxczhkdAYjoCgcdSFmy0cWHWiCrAJXOn6FHgKDDn+kGqn4lxJU3E+q1mC15xgWbVJYBHo66DrE2dS18vOZy1L8CDwSYITYKCrqNVjQFrkNZjmtPz3IYSamRV1ecvMplOm88AecKaxB9xL2UxLa2ZWlGfX2Y5yszHAzeaQUz0AWx0eoi0giJuTFnmNdgpcAaops4LrP3L1M+CFxpmrP3T8QsqrCqzEghsp4rG/v8Ab1T8AI64+AnxU77WrD7hVJ2jEghPsAHeB/lS/pv5mRPtMvVqq3i+vncQ86cVe7PdmdhBC+Brp9QR5HMgzDuDarboOlHA7zm/81Tl51J3vdSy4RCsWXd8/XJ+JP1xLjr8Y8Su1W/U48AC4FHHf9TK/TuInh8WlPMc73JWfok1nOJXqzQO7/NpAdkltIDSfWi0PY7vQHPBWoi/AWCZhs8eYtMgr0+m05Ga7rloedyJ10A4Bef1ej937BLGZ3HG/J4D7ZlY1s3NgG5iNBM4C22Z2bmZVaSbaeLad9TDNZ2kMZccvd+FWgOGuwW3CL4CXNL+T4xoJ6uL4j8HsoanwErCRiIE5Z/pcI8Gc021I21toh8n0pVbtV9v26ySGnj4/QwgNMytHWmX1snv1Qja7ec/N7LaZ5VX6ZmYnIYTvvXr9E/wA/z1V5MTKX1YAAAAASUVORK5CYII=',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAAC8ElEQVRYhe2YTUgVURiGz4h/4bWF0DIQF4JS4ELQhbhwJ0qtoyTXLnSRQpS0CTet/It0EyJCCy0QFSUQ3EZQeAnxJ0NBUQIraiVKT4v7DX6czpl7Z8Z2vXBg/N7vfc9758yZH435j2gEacRAhzHmhlX+FATBUhrfxGHwoyOpb1GKTLcScukAtADDQLNV31Bn5KWMEBtWb7N4tKQN0wmcyiS/gRmgDZiWvxH+igzdOy29M1Zv52WEicKa0qwV0B8/lPwyHeabLwxQr3T1EaG0xynQFifQghLvA9eBVuC9mL0CGiP0jdJzKppW8dhXvgtxAvUr4QlQVbDY71klXiH644hLgR0lHrmEQCPKbwcojWvQowy2HXwl8BBYBb7KWJVapaN/W/n1JPlFy8pg1OLqgF382AXqLM2o4pcLDdEEDAIrXNw7zoEa68zoi9OHPX2mgBrxQrxXZK4mX5guj/Gs1TeouB9AN3BNRrfUQgxa2lnPHF2uQBOOxk2g1ur7qPi7Dp97iv9gcbXiaWPCFWhSNbwAqj1n8pfqc128VxX/0+NRLXOEmAw539M+GwTBnofLB/I1iHfWxfkCjQNZ4BlQYXGf1bHrNeO2p9cAFeKZNcaMuyYuVsdHVtCbMs6NMY8U99oY0yDHz4ESY0z4hthhjBlWvW+s+R4bYwYcOY7+qkj6IeAdcKbW9zuQUX1Jt31GvEKcyVxDjlVwhtO7oc/ik9wY+xS/mTeEI1SvMlh38HEfHevKrzdWGDFYVAapvyKAJeW3GFfcrsTnQEN+VV7PBi4eHQDtccTzermAYnLX1RPh7pDbXT59ifTMi6ZCPPSyzccJNGBdpG+BA6u2DzwAypWuXGr2LjwQDw3X9vcGCoAxCsOU0k0VqBkD4n01e0Idkvu+Ola1E6BIhn5FPZbew9RhrFBPgS1yN7CM1MusUPdl6DBl0psR7ZZ4pfp/QlTYuYglmUvqm+bbfjUhF4ni/C1eTJncQ7bGqn8R7j/+Cf4AyqLbBqyFwQQAAAAASUVORK5CYII=',
        },
    },
    'experimental': {
        'light': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAAB6ElEQVRIieWVP4sTURTFf3dmUsTYrZDk3RddWxXUTyAo2Fm4Lqx+ALHdQkX8g2AX1kXsVOxE7LSwl/0AFgHFRhAM81KtbhWFBefazCxJSDL5g5WnmXfvefece2eG9+AfQ8o2OOcuRVF0YhyXZdmXXq/3flp9UtqByBszq03g+sDhafVRmYGZXRtYt82sPY5bCqpqqmqT4mkonWBZ/AcGrVbrVL7cH0jvj3CLG5jZdv58NpB7DpBl2dOlDFR13cwuAj9F5FGRT5LkIbALnPfeX1nIwHtfBbby8G4I4UfBdbvdPeABgJk9cc4dmtsgy7LbwCrQCSG8HOVDCC/M7CPQAm7OZaCqXkRuAYjIJvBnXA/AJmAicqfZbB4bpzXpLHoM1ADMbEdVJzVYoBpFURu4WjqB9/4csAH0gU6Zcr6nD2zktVMRq2onP2vuzSAOgKreV1Vzzn1m5K0MTeCcuwGcBr5VKpXtWQ2q1eoW8FVETqrq9UHu4MKp1+u1JEm+Aysispam6btZDQC895fN7C2wKyJH0zT9PTRBHMdngRWgM684QF7zCThiZmeK/IFBkiR7+VIbjcbqvAb1ev040AQws0Jr+E5W1R2g9E8owYcQwoUiGPrIIrJmZq+BXwsI94FXcRyvL9ngfPgLVxGcoemkapsAAAAASUVORK5CYII=',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAACuklEQVRIie2WT2sTQRyGn9+atMWixR4au9sNFYIVq0UpIip6UxD0IB686cFDjxapJ/HPRUEr7UGEUvQrFPwOKlSp/5DSFkSa7I5bKAlVa4ImGQ+dQNqmyaYN6MH3svPOvPt7dmZ3hoW/JKkjG4nH4/FqgWQymQTyDQU7jvMSOF4j9sr3/RNh6llhwcCeBmXqA1uWdRKYMXZORAZEZACYM30zJtNYcCqV+gwExirP88Y9zxsHlOkLTKax4EbrP/jfA/f29jYB3cYWy4ZK7W6TaSw4k8lcK4FF5Hmpv6zdbTKhFOrk6ujoiEWj0VmgTUSmY7HYoampqd9mOOI4zlvgIPA9Eon0zM/Pf61VM9SMo9HoA6ANQESul0Fh5WweNO0d+Xz+fpiaNWfc2dnZb1nWa/OQE77vX6yUs217QkQuAFpEjnmeN1mtbq0ZW5ZlPTG5XD6fH6qSHQR+sjKZ0j2bA9u2fQU4auzwwsLCl42ySqkkMAKgte53HOdytdobLnV7e/vOlpaWWRHZzcqWuSciy9WKaa1bgZuApbUOcrlcTzqd/lYXuKura1hrXW1pa0pEHnmedyM02LbtHhH5CDQBk8AuYG9I3hyQYeUV/dJa9ymlZteGIhs86aiBFoAB3/c/hIQC4LrugWKx+A5oEpHHwJm1mXUfl23b54Gzxo7XCwVIpVKfgKfGnnYc51xVcCKRaBaREWPTWuvb9UJL0lrfAtLGjiYSieYNwdls9hKQMDfeUUotbhaslFoUkbvGJnK53KqDZ+1SHzbXH0qpsc1CS4rFYmNAdk3t9WARWTLN7a7r7tsqOAiC/UALgNZ6qXxs1XYyX+N7YBuwDLwh5A96BUWAI0ArUCgUCn1BEExXBAPYtj0kIg8rjW1SGhjyfX+kvLNicdd1TxWLxauAvUWop7V+ppR6scU6jdMfZivnhCVDJ4YAAAAASUVORK5CYII=',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAADN0lEQVRYhe2Xz2tUVxTHv+e+MfZVYjaVCfdNgnSh0EiJZGFLqS40uOiiCmJL0YX1x0Yi1JW4EUEQKcVauikBEXUjdNk22PgHKBp/lQri4GLee1cCGmISAknhfl14nzwmM/PejPPoJt/VOed+7z2fuffNvDvAqlpL2p0QBME+ETlKUrVcWMSSHI/j+PeigeYA9Oa0z8dxvL6d9Vt+yia6X5AXQAdAvu/vFpHLqdI1kqMkRwFcS4oictn3/d2FA1Wr1SUAYarxc2PMLWPMLRF5nrKGzlssUNFaBcrSKlCWOgIiuTGJrbVsFKc9hQIFQTAM4ECSe553u1EM4IDzFgsE4GcAnosnwzC8mQy4eCLhA/Ar2nw9tQVUqVS+A7DDpcskx+o9InICQPKD+IXW+ttCgLTWH5I8n2r8izHmab0viqIq3u5M4vuxXC6v6zqQiJwGMOjS6Z6ennPNvEtLS2cBvHBpUCqVTuXt42VbgMHBwY9JXgdQcqWxWq12p5l/cXFxube3d0ZEvnalbX19fTfm5uZmsnrl2iFr7U8APgAAEZmK4/hq1hxjzBUACfRaa+2FPL0ygYIg2Elyj0sJ4DgAm2NtWmvfeUVk78DAQOZ1pOVXcmRkZM309PRDkp/kAMiUiDwpl8vDU1NT/zXztHyGlFInABzsBozThoWFhZn5+fnbzQxNj6y/v38DgDOp0m8Aah1A1NzcRGfc2g3V9Mi01uMicsSlVd/3t3RyAwSAoaGhntnZ2ccANgMAyXFjzLHcQFrrrSJyD24HReSrKIr+6gQmteaoiPztUquU+iwMw7v1vkZHJkqpS6mxP94XBgCMMZMA/kz6WmsvocGGrADSWn9D8kuXLovIyfeFSSQiPwBYdunnWuv9mUAi8n0Sk7wYRdGzbgFFUfSM5MVU6VAmEIBNSeB53ni3YBKl/9MppTbVjzcCep0EJNu+YOXQuzVJztYPluoLJCdE5FMXX9Fab1dKmW6QWGs1gMOp0kS9Z8VTrrX+SEQeAdDdgGihCMBwHMev0sUVR2aMeWmt3UXy3wJh/iG5qx4GaP1y9SqVyijJrSTXZ3jziEqp1yLyIAzDSeS7Mfz/egNSzSf/lfftjAAAAABJRU5ErkJggg==',
        },
        'dark': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAAB9klEQVRIieWUz2sTQRTHP29XEtqKpI02igH1qoL6F/Tg2UO1UP0DxGsvgvjzLKWINz2LVE8evIt3DwYUL4KXLenOrDuTKlFQ0uchm2DaZDdp8eQXBmbm+973+x7zGPjHkKKAOHGXAuX0MG5b+HT0yNzrvPwDhRUo6wozI7g2cDAvPygyUOHaX8eH2RrG7R3GOjXW6ahzHgo72C/+A4M4dmcBUH71L7N9n9uPgQSsAajok76+8BRARB8X5efC2nTJWKfGuHRj41u1d99qtWaNdUmXS6/kaYzsIIqiKUVWs6jb9fqhtMdVKhWPcq/bjTxqNpvTE1dvEv8gm/f3qhru5FU1iG36zlinJvH3J+ogTdM6qjcBApEVEensjBGRbVFZARTVW82mPzFMa+hnZ617obA8qqoRQi/n5+euFnaQJH4hE28DjTG0G0BbYTlJ/EJupKqGxrqGsU5j6+6MWTyx9Xe7b+E+qurADz3QgbX+BnAO+PKzvbU2rsH3rcoq6GeUM9b668OriOMZY93X7mz7xXHFezDGL2ZTl0RRNLWrgyAoXQCqQKNWm301qUGW8wE4XCpNn99l0Omoz7bHNzf9yUkN4rh1CjgGEIZ9rcExja17K5A/CUVQfVOrVS/2jgOP/LscXkZ5DvzYg3Qb4Vm5HC7tq8BJ8Qf2kPXY6WZnGAAAAABJRU5ErkJggg==',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAAC40lEQVRIie2Wy2tcZRjGf8+Z47RYOpnM9MwZZyERihHR0lIkUKk7BUFXLtzpwkWWFokrSXWjYAvtohRK0D+hBf+EQimUgjdsaRKKUKSeS+YStbaYnPO6OGcgiXM5SQZ04bP63vd7eH7fC9+5wL8kFTWamRtF68+O8jQaUw8kbRbJc4uCo7h7HXFqjOcm8GqRPKcoGHhuQp5dgs05bXAvK7SCmEfMg1YADO5hzumJg32/et+BAEDYQ9+rLflebUnYwzwo8P3q/YmDJ63/wf898B2zssEMgJml/X5/bTBzx6w8cfCRqPshOVhyvun3t6xnck8hFXplhmHoo6eWgSmMu43G9HFJG5C/SuPut8DLwO+OklnP834dl1lsYrlfAlNZYR/1oQCSNs1xzuTl4dScLwpljlMc906GUScJo46FUfvqMF8Yta9lnk4aBGtz43JHTmxmTkp6Ofc9sbS0MMybuDoD/AlIjnPZzEZmj9yM4977GHP5Kc43m9Wfh3lbtdoDYRfy8mQc994blT30crXb7cpmomWgCaSYfS7p0agwMzuE9AnZQIFbstl6vf7bIO/Q7/FmqsUcCuAgLdooKoC2zdHMMz4eaB3UjON4Nk1LPyLKiFuYpsGeH8fNI1eQdTHmMP5ynOSY53nLO10DJ06tdBFRBhJSzfv+9A/FoJmCoPOSHL5DlFNzLwFv7PT843IFcedt4M3s8CztFgrQbNZ+QnyVVfZ6EHfeGgleXbUDMrKbaXSSDffsbqF9JRvuIkYHQMbF1VU7MBRcqfTeBY4CSHzaalXW9gputSprkj7Ly6OHq913hoKl9ES+/MPzpq/sFdqX51WvAI8BBCe27m0Dm7GeL58Ow+4L+wVHUe9F4GCWrfWte9sep/w2fg+UgEeC21bwB32nZOYavAIcApK0ZMeeqdfvDgQDhGF7AencoL09ypAt+F79wtbmwPAo6r5m2AdIrf0h01+Evm40ajf2lTNJ/Q3S5hOwBhlk+AAAAABJRU5ErkJggg==',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAADQ0lEQVRYhe2Yz2tcVRTHP+e9iS2KSZPMew9jERHSRcGS7pTSurChCzcKoiLdRHEVFHQlboogFBGplW7aQAh261Itmv4Bir+RiDLSRSsz817mVzqrpMk9LvLmzXRm8n6k88BFvjBwz73fc85n3h3evQwcKF6SNSEI6i8r8hYiVqxR1Qi65LrTX+YK5AeNu8CjKe1tz50az1I//lsOkcIveXg7ygzU3pg8p8hyNCFcR808auYRrndhZLm9MXkud6DZWdkUNXeiCaO3PK940/OKNzF6K+JUc2d2VjZzB8pbB0BJOgBK0v6AhCe7FSwdOu715AkUBM05kPPRhDHfDx0j53e9OQMZ9DPADpuuet70t521cHwjDG1Fr6hqpuMpE1B1vfG6wHMAKFuWbL/d7xHsd4DOC/GUX2u+lgtQuVx+WAwXezI/dxzn736f6078g8qVCFD5pFqtPjJyoELh8AcIT4Rt/DGbj/Ysam1/CFTC8HHLGns/bZ9U++v7racQswYc3k2SBdedXInLCYLmgqKdQ3hTsJ923YlSUq90T8gyn3ZggJ8d58gXSSmOc2QF4YcwPKRsf5yqVZKhWm09j/JiGKoasygiJilPRNTCWgRCr7zk+/XE60jslqnqWBA0f0M4nlQolZQ/XXdyTkTu7WWJfUJ+rbU4MhgA4bhfay3GWfYEqlTajqheiCaUqyi3M0Mot1GuRkyqFyqVtpO5ThA0l/ygoeGnVCrpocxFQq2pPuQHjb+ien7j2l7eob+hIGieVPQnwico8ILrTn2zXyAA36/PI/JdGBpjmWceKxZ/7PcNbJmqiqKXe2C+elAYAM+bXlX4utPXMtblYefcAJBfa74KnN6lYwvs9x4UptvMfne3JgDP+uuNVxKBROWNbsClNG/XtHLdiRLCpW55WUgEQvRYNFZraVQwUUPZWe4Jjw2sD2QoG91gJ/MFK0mqVm/NVv96YUjODeAEACIr1aB+xkLKo4Ax6Iwib3Zi6V7m6Jm7X+Xy3aJd2P4dmBkFRIz+vbdVmDt6dLzeOzmwZTMz47Udy5xFWMsR5g9Lds72w0DM4aqq9vp6Yx7kpIFx2cdfN/fVA7VgQ1V/dd2p1TQ3hv+F/gP7+TSpoXl4zQAAAABJRU5ErkJggg==',
        },
        'active': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAABQUlEQVRIieWUvUoDQRSF7ySCxthEK1FQW/EFRCFPYBG08QmivoKitaU+glikt7IQ7C0iIhaCCLZRbDYWKfxszsIQ9292sfLAsrP3nPtdZncZsz+WywsAW2a2mmI/Oeeuqg6IzKyZYg+dczNZ/bW8AWa2661PdSV55YWU9pylIjuopH8wAFjTcuSVR2NeeQHX+qZnXu1ctZuq8B2BPoA5r94CBvK2y8IbwKsg3QR/T94bMF1mwIkAfaCe4NeAO2WOQ+GLQKTmdkZuA/gGvoClkAE9wtUrCm+rIdLryVO/yG5jeB24V/gwYMdH6nkEJrKCBwq+AFMBAyaBZ/Xup4WawLtCnaJwr7+j3gHQSApsxu80FO4xHsRYj2v+WfSp+wKwXAK+YmbzY6xfodsCf02e0s8nYBa4BIYlwBFwAbRCd19JP0N/IbiaiAMVAAAAAElFTkSuQmCC',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAAB5ElEQVRIie2WPUscURiFzzXrBwqphEQLWbFYEUmKENKlSGMT/0AKUQQrJSB2dkoqi3SxCskfSCBNijQ2CkIsJKCypBMLGyEqKhLdx2LexevEnY+dC6bwwLIz9557nneGue+MdEdyWY1ASVJfim3XOXdRrKR/wWukay1rXksOdn8gTz4BA8COXVkVmLJf1cZ2gIHgYIOvGGQlaSyL8tzqoLoH/39goE1S2U5r3lT9uGyesGBJbz3wN2+8flw2TzgBj4A/tm22gFZvrgT8srkjoCck+LPXFkdumX/lzX8KBX0GXFrolwTfV/PUgBdFoS3AugWeAQ17MdAHnJh3A2h+xwAT3i1cyOBf9PzjSd6G72PgoaSqpMeKtsw7SScp7C5J84p2y76kinPuKK3gOHiJ4lrKC60A57Z4netXXxZVuX4uzoFKHvB3W3gBPM1VdbR+GPhrGT+yLhr1qv+QF+rlLHs5r9PM7cBvMx8A3QXA3ZaBZbYnmce8KqebhXp5M17emyTjezMdE33OFgW3AqeWeeMJj3eXQ/vvlDRYFCxpSFJHLFtSrIEAw5I2JT1Q1Cx+Smr2A70k6bmipnIp6YlzbruhG5gjavShVANm45xbWybwUtKkpN4mr7auPUkfnXOrBXPC6QqP/7FThy5MDAAAAABJRU5ErkJggg==',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAACLklEQVRYhe2YvWsUURTFz1VjQElsTGMlahRSyAYRG7Fxw4K14EeliJV/hhYB/QPEEJDUooUSJVqIiFiIIrIgCMGQECyU+FGYhWR/Fk7w7macmTc7L9UeWHjcd8655+28fTOzUh/ZsFABcFbSVUnbcqhtSVNmdi92oJ+ShgrSf5nZcIh/3irT8DYStxyAQWCaf5gB6slnxtWngcFQ/x2hAjNrAYuuNG9mz5KwJ1190cxaof5lLllU9APloR8oD2UD7Xdj/jP2nHgAasCaO28abq7h6mtAbSsCPXdN51LmZ938SyD49hQS5qJr1gKOpHAOAauOdyFWmF3Agmt0M4N7y/GWgN0xAt1wTb4AezK4Q8Cy41+vOswB4LdrcKmA5rLjrwKjVQZ64MzfALnHBWDAa6e7X1WY0860DZwI0B4D1p2+ka/KNhwAmlSHJjCQ1TPvq78maaynVXViLPEMBzACrLjV3abzZ18UC4l2AyvASJlAU87kEyUeR53XTuCj87sTajBO52Y8UzaM85xwfuvA8aJCA1448cNewzjvR873FUXuc8B5J2pR4YEGjCaeGzhXRDTnBJNVhXH+k87/SRHBZyc4GCHQYec/3z2fdg79cOMYD1je83v3ZNqL4mNJR5PxXeCUpOWKwuyTdKWrVwc27XJgr6T3iTgmliTVzOybL266ZGb2VVJdUjNimA+S6t1hpIy/Y4DtkiYkjUsazuIWBPq7P99Jempm7R79tgZ/AALVBv0YNYqOAAAAAElFTkSuQmCC',
        },
    },
    'keyboard': {
        'light': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAABQ0lEQVRIie2UMUtDMRSFz80rrYj+gJK8n+DiKtWx4Obo4iTioGMXJ/+DTi4OgqubCG4+BKE/o3nt5CIoWPvucYm1xQfatChCvyW5ueGckzsEmPPXyGiRpmmzKIoVY0wSI0ZyYIxpdzqduy8GzrlLktvxWT8RkVPv/eHQIE3TpqreAHgUkXNVLSKFqwB2ASyr6nqv18sqAKCqq+HOmff+aJr0zrkByVaSJGsAMhOca2HtA4C1NrPW0lqbRdQvwWsBAMw0aX9Cpewwz/NGbO2cOx7tlb5gyhGN8f9HVGoQLm6SvAJQ/SZkX0S2vPfXZc3fGRHJ11AvfjRCotqkgqpaE5GhZgUAjDH3qgqS+9baNxF5iklLcgnAXti3gfHP7oTkQYxwCRd5nu+MGQBAvV5vGGM2QpKJEZFnkg/dbvd2FinnzIZ3g12kr2Ss4FwAAAAASUVORK5CYII=',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAACGklEQVRIie2WvWsUQRiHn/d2c55C1g+w2XktBbFR8CNFtNDC/8DSQggEUgVSpbazUbAQFUER/4HUfkCMaSwUOwurux0QFM9c4Xne7muR2yR3uZwXdtPdU+3+dmaemXeW2YUJEw4IGQxU9XSWZRcqlUpUhsDMmsB6kiSNoWJVPWxmj4BbZQh3++2B934J6AIE+ZPp6em7wPwBSAFERGaiKPrbarVWYXvFFVX9aWaRma2GYbgA/C7DmKZpBDwDzgE/kiQ5CZgAOOcUqAOY2Zz3/mkZ0hxVnTOzJ73bU0mSNCq9WYV5IxFJAeI4XnDO/XHOZXEcvyySmVl3RwVCgMpesxSRa0B181JuFM0GCYeFvc6LZvYZOCQiK0UyMzs7OP6eK240Gt/M7C3wulqtfimajS1W1fsi8g5402633xfNxhYfNKNKvWhmV4HrtVpttmg2yJ4vF9D13q/FcXym3W7Pq2rfud7pdC6rKoNZHMcGrHjv1/LcObcvcc4LEbloZmM0BRHBzG4Cl0a1G2ePX9E72Mek2+szkhAgCIKtgUWkrwre+2VgeR/iXZhZILK5U7kr3zdxzn0HTgCfgNtBEGwUke2QHsmy7CFwBfiVJMlxwPLVGXAPuAOcBz6maVqGtw8RedxzbX+PW63WehRFR4EZhvyZlMDzqamppWaz2VfqLVTVmdmsiBwrw5Zl2UYQBB/q9frXMsabMOG//AONmiK8KBv9vQAAAABJRU5ErkJggg==',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAACTklEQVRYhe2Wv2sUQRTHP28SEoTkiJDC3E4Tf4CQBMXWJhgtDAh2KYyVECWFf4C2kkaw0MY6BIlVxICEYP6EIBKCIILNztnoEfZA0MR5Fu4dc2bvLvF2sblv9d533775MDMwD3rqqadiJVnmxMTEQLVanRKRk4UsKvLVObcD/GoLZK09oapLwCIwUARMAJV4759UKpVHIVhfWDM0NLQuIvN/+UVpUESmS6XSeK1WW2tA1INyuTwnIqvBD++AbwXBnAImGxAi1+M43gDoD8wrwQ9LzrmHBcEASBRFz4EFAFW9AWwAmKDoXBC/LRAGQIGXQX6+HvQHZuP4REQBrLVngU1VHQcOgMfOuQd5+CKyqar1Jc2hIEve+5tpkzr8/Tz9LLUFMsa8EpHPaXoAPM3Tz1J/qw8AcRx/Ak4X4Vtrp48NBIi19hYQee+9MWYtbd6132rBtkBRFM2r6jKAiKCq94AzefgicidrzbZ36H+o7Q4551asteq9nwS+G2Ne5OjbYwMBGsfxShG+tfafgIA/48je3t4bYOYo9RnaGhkZmd3d3f3ZqfBIdyhJkgtdwADMpD066khApVLpPbDVBdBW2qOjwiNrPCyq2gSabvXVLoBwzjXlqhoOh4cfNeBDEM/RYrzNUdeC+GM9COehdVVdTNOFcrl8WUS+FAQzClwM8sZ1aNqFKIqWgdsFQWRKVTcqlcos6bE1zc61Wu318PCwN8ZcAgYLZvkBPDPG3E2SZL9utronfVEUTanqaEEw1bGxsZ3t7e39zqU99dRTvvoNSs1Kboy7U68AAAAASUVORK5CYII=',
        },
        'dark': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAABQElEQVRIie2UsUoDURBFz40hEYmFK9nNT9jYSrQM2FnaWIlYaGlj5T9oZWMh2NqJYGcQhPyI70leISgY446FMcQYxGyCIuR0swPn3jfFwoS/Rr2Dc80asCBpKovMZG1ZrhHHc9dfApwLZ4j1zFV7g+CoEke73QDnmjWkS6CJOMF4zWZWAdkmMCtYjuOongeQtGjvyceVcrQ/Snt3H9oYe8ASUM8BmFkRQGYtAOdD3flgzof60HNqTx3nNEBulLY/IT/oYxJH1ayzc82D3t3AF4x0oj7+/4kGBgB4H1bNOEcUvq1otCTW4ji6GLT+nRNJejaAnGY+Fp1GxWGFJhXVcXYDQDdgYGx7H16Ah2x9rWTGFgKztAE9P7s7Hw4FO9nEfYjTpBxtfAoA8D5UU7QirJRNrEfS9DZJ5q/GUHPCmHgDUq28i2DTy9MAAAAASUVORK5CYII=',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAACNElEQVRIie2WO2/UQBRGzx0vDi9F2ZjdJaRGQjQg8WgCBRT8A0oKKqQUKBINaWgQEh0gkBCk4SH+QGoeEgQaChASBQVVUsSzu3YSIRCJPZdi4w04r0V2uj3l5+t7xnMtj6FPnx1C8oG1i4edpCcEBksRqC54Hh+CIJjbUDw7O7vH373vEcqlMoQ5VNH7jdrwNRFJAEx2xff339ohKYAIcjVsLlzvBp3lqLHNOKazvW9dquPGeL/KMeqgok+AY0C7XqvWREQrAFEUHQIZBFD02chI8LUMacZ8s/1AVKaAIIqiUWDOACwvm0pWZDApQBhG42EY/Q5t5MIwelEkM2qSrH/m6s54HSLnEHxAEC4UznJUNgoBPJNOpOp9EdWB1LjpIpnnvKP5/ps+cRAEoai+SUVeDRjzrWjWs9g247sK74zq65WU90WznsU7zabieq06IXDWiZzf5TFWNMuz6cu1+mmbabVaR1ZWzBVrI2k2YwA8VZKE09ZG5LMwjLTdbk/X6wdmsl7Wxr2LMxInz0U4qdsVdlcMiZOLwKmtynqYsbwEkm3L1khW79mSCoDvuyRJOweVE/fPLhysD08Ck/8hXofDebJ6EPq+S2DtkBBr4xbCMPBZkMuqslRElqHq9orhIXAGWKzXqtXuISEiOm/jO4LeBI4r+gnpeapbIn//agiPRTqNuzNu1IZuC3oPKMe4Dn3688fijbU15Gi1WqPOyZiKDJWigyVx5mOjMfS9jH59+mzLH97fWgE2FaNtAAAAAElFTkSuQmCC',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAACXUlEQVRYhe2XzUsUYRzHP9/dRamLujv7JNSlNwhS7E+ILCohiA556AUiiOjQH1CXDuElSOjU2SSKDnVKkfwbQkSCEDp4sGbfVCjQdvbXod1xxHFdZacu+zk9853fzO/DM8+8QYcOHZJFceGCWZcrrg7WatW+RJoqXczne+clBU2FlpaWDnR3Hxwz0wNEVxIyEdaQnjuv92lULBQyM/mFyhRwMWGRrYhXh/LZ29uEvvulUaE3kdLPSKVEJMz6gYFNCbvsXG4aIBOGpnMNPcGYc9nHichQvxp+5SXiHoChK8A0QCoUEicb40D6lJTM314yS6feRgxPNYaZaF1kbAC+v3rCCGaAo0DV4Fm/yz5qR25mM42GksKJSdEEs+rV+kkAMoKH7czjaCokZT4A3+qbVYMX7czjyOy0A8C5nkXgWBL5cqFyds9C9bvhhsRhM2pS+r1zPYvtyANqsT2bCvn+yk3EhAEIjOA+cLw9ue7G9Wy6hv4Hu6yh3knfrxhiAOmXLPW6fXntyJ6FJBkwmUS+XKjsXajBglmXVyx/xDTcSv02ZLNFLztyWtrYrbSlNZQtlYb2LQNgGs6WSkOtlLYkVM7l5pDN7ltINlvO5eZaKY1eMguPj7xbAOpTfX7fQvFse3dCZIYMvoSVQW3UzGI/b9tmY3Yh7G18DfPGwPdLlwxNbR7BAmg5ERszDziz2cquO5d7t0UI4EehPIFxKxGJnZl2+b6R+qNh66J2Xt8dpCfA2j8QWcc0vrH+81pDBnb4DTKzdKGwMmgWeEmYpFKZsuf1zEv6ncT5O3To0Iw/R8hKUdng9s0AAAAASUVORK5CYII=',
        },
        'active': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAAA9ElEQVRIie2UT0rDUBCHv9FSRexReoS0S6HnEOkRdNdVF93mAIUuPEIR3BkEoWdxIyikKr9uJm3SRkhfjULJt5nMm3m/+QMv0PDfWN6RdAV0gdNAvS9gYWZPOxFJ9/o94sIE3vkD8ApMge/ACdrANdABemaWZN3feeVxoPAaSRPXugU48fMzt0tPSjwpCfA/XOs8X6A2WmWHZhaF+pJG+VjpBAeuqMCRrsgTB5LSCo8qlTT4SefPVpS6vcgCZjZn8z72IbuT5gs8ux1K+gTeAoQBLoEb/14UIpLiCvuuyizT3f5dR0DfOwnhHXgxs8fA+w01sALPl2c4VAmL2AAAAABJRU5ErkJggg==',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAABjElEQVRIie2WvUoDQRSFvxvjb6EiWGlhI4iNhZpGmzS+gX1AQdDGVvAJLETBBwiKL2AtCCI2WomNhZ0WFv42IkSPRWaTSXazGna3y1ftnpm5Z+69s8NChw4ZYc2CpElgFhhMyeMNuDSzh8hRSf2SDpUNP5L2JeWjjHczMvXZbii1pBzw6sp7DqwDnymVehAoAzPAMzBqZgqyHfd2tZKSYQ1Jq178cYCcG/Nr/+0mr0v6cv05TqhVvPh53ziKItBDtR1LKWgNhE9ZnU3gBugFThJq0+0YPwFnQDdwl1ALGQMgacJrfslpB552m0STVPLeJyC+x9nTIuO8pEVJRUlDSbSojFv22MwqwIWkKWBNUvO9XpDUvKygqnhiZhdeYqH4cYcr4AiY+8c8n2VgPm7Cf3p8SuMF8BcVtyaWIOPQzRJgZlvAVhvGUXQ1baxm8gi8ACPAhqRr4COhWcAAUHLP786rjqRtZc9OaFvuE9hT9WLPgrKkvsAv6tdnDFgAhlMq9QdwZWb3KcXr0CGeX0d46gyx38OgAAAAAElFTkSuQmCC',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAABvklEQVRYhe2Xv0ocURSHv5MNgqWwhRBLAykUfYSFNRBfIBaSt0ifKikVAhZpFxHFQjstNvgIQUIQJH/KNDGwEELU6C/FnYWz48zOMM5dUsxXnXvmzJzf3HPmzr3Q0NAQF8tySpoCFoGZSHl/AB/N7GZslKRpSZuSLhWfgaRXklp5YkzS8QSEpOl5HeYErQG77toH4KJqTQqYBRbceNXMjkciJL1zql9HEjLMZal8W8NrD1zcY2f3YwoyMwF7zvUkS5D/4gQgaV7S1+QtriW9qdF/6/J5HYkC6cRNYSfxvUw14K+6/JI6bnySr2yUQ+BbYv8F3tbszydrhmKSN0MPC24yYB14RKj5gZl9rsNfRnFWD71I1f5LXf6qPTRxxpYM2CYsAQvAb2CnRv/c2Mz/S1OXKpmkKUl9VaevsKUppGwPLQHdkrFZdJNnFFJW0CnwvrKccO9pmUDf1HL2iFAzuwJW7iEoizv/znTiM2evKSxmMXnq7POh4Tdoz4AjF/QJ+B5JTBtYduPnZrZ/J0pS7x5fUlWOcqshqaWw8R5MQMgfSRuSpr2GvGNQi3AMaletSQE/Cceg60jPb2hoyOUfjsT7i+ku2X0AAAAASUVORK5CYII=',
        },
    },
    'controller': {
        'light': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAACbklEQVRIie2TMWhTcRDGf/eaZ5uhmFJKw7vXErt2KC1CdSxCsYKCOijo1lVEVNTdQQeHCoJTHUQEETpZCwqWTt0MDtmKhua9EC0txQ5pEvvOIS8aU01bXPtN7/5377u77+7gEIf4X0jrg+d5Z0XkFjAOdO2TZxtYFpFHQRC8bXZ0NBuqel9EngIZIHGAQhPAMeBKd3c3W1tbS7s68H3/tJktAFXgbrlcfraxsfF9P+xDQ0NHK5XKNPAQcM1sslgsvv8jyPO8BVU1z/NuxwmnfN+fOkAX+L5/T1VNVecbb06jExEZB+jo6HgJYGZzZjbXjjCdTvel0+m+hi0iz+PPk8TqOAD9/f0ZoAf4WigUinFQF3sM2XGcGcdxZhp2oVAomlkJ6Ik564N0XXfMzABWVbUCHGn8pKoGVEXkfOuGRFF0ozWpiGSBKdd1R4EvTizHaOz/3K7iVpRKpbVSqbSmqr2q+kpVe4GPzZyNVRyNs78OguByU+WEYbjrVtpBRLKxGmO7EjiOk22K3d4vaRiG68AlgMHBwezOzs7vojOZTFetVisDlTAMk4BBfU3NrA+4CYz8g/uTmZ0rFourzU2oahVIpFKpTiefz28D60DnwMDA8UZUFEUl4EEbcoAREVn2PK8xQ1T1BHVlvuVyuWoCQERmzexOFEVvVPUJ0AlcB7qBRdd1L+Tz+c1m5kwmk6rVanPAhIgsqepjM6sB1+KQWYiPIZZpDmi93BepVGo6l8tV/1b+8PDwkc3NzVngaotrPplMXlxZWak0b4io6hlgwsx+iMi7MAw/tJHnF1T1lIhMUj/cxSAIFohneYg98RO8UN0LP2A5lwAAAABJRU5ErkJggg==',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAADf0lEQVRIie2V32scVRTHP2c22WSNYkoNCXMn2zXmQWwDTVYQG60/akBq3/rmU30URFAQfxXNk/ijKNYHX0TwD/CpVIUuFIkYEJIKNnla02VhJplF1hhTxll37vUhd7bjmrRJ9UXI9+Xee86Z+znnDGcG9rWvff1Hkp0cIyMjQ7lc7mkRcW/zbl9r/U0QBL/sCjw+Pt4XRdE7wAtA/jahqWIR+aS/v/9stVqNdwRb6NfAE/8S2K3K4ODgM0tLS63U0JP1RlH0dgY6r7V+dXV1dR5o74VSLpd7G43Gw1rr94GHgKfW19ffAs6mMZ2Kx8bG7o7jOADuAH4oFArHq9VqXCwWD9Tr9d/3CgcolUr9rVZrTkQeBK5HUeQ2m80NACcNiuP4UQtFRGar1Wrsuu5MkiQNpVRlr1CAWq32h4jM2uNAoVA4nvo6YGPMVLrN5/Pf2/0xtl7HY7sBeZ532vO801lbFEVzgLHHyX+AHcdJjT+vrKz8Zm07jtt2MsacM8acy9psa1esvwPuyTw0ZddQKbUJDBhjOhcopdLDt77vnwCSbnC73X5yh5wWgftEJO3qVsVKqYNAEcBxHA0M3KSw6VKpdNd2jjAMr4VheA2gWCwedl13xrqu2PWQ67r3dCo2xkyJdLr6sYhc1FofEJFHgGkb856IaGPM5Vqttn6TxADQWn8qIsfK5XIhDMMrme4dBSppqzu9b7Va3zUajS8BPM+bNcZMAwRB8NqtYFk5jvN8u912FxYW/hweHl7s6em81Umg4tigo9boNxqNMJP1pt1e3wsUoF6vLwVBcAkgDMMG4FvWJNxo9bhdr2Yfzufzn7Xb7WGt9YJS6iJw8ha8r/r6+p5Np6JLy4AyxtwLN8Zpw65eNrJWq61rrc+LyBu7gAKcjON4znXd0S67AKMAIrIBtmIRqRhjTojIYaXU677vvwsY13UnReQCoOwFl4wxi9sR7ajMABMiMq+UOuX7/o+Ao5R6E7gfQGtdSTNJv9NXMxXXgV+BI0AOwBhzPgiCl9lmfq1yrut+KCIv2nMC/AQcTKsF6lEUTTSbzY3ODHmeN2GMuQAc6rowAV7xff+jHYB/k1LqJeCDNOGMarlc7lS9Xl/qVJxqaGjozt7e3jMi8jhbP4zlJEk+X1tbW94NNNXo6OgRrfVzxpgHRGTTGHM5SZIvwjDc83Ts6/+rvwBT23CDd9AjLgAAAABJRU5ErkJggg==',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAAD4UlEQVRYhe2WXWgcVRTHf2cmH92N3WAMuswdlmEtKIJYK1UQHwQhUEWJoBaF1gpG++SD4JtIEQR98EEo+FCxpbQVK2gtfjwJWhHJQ5KqNSCNJMSZiTGU2o2YNMPM8SEz6+xmu01dfRD2/zLn3nPuOb+99+69F7rqqquuumqQtPFZjuPsFpE9wN3ADR3WugCMq+rRMAw/AJJNA5XLZc+27ZPAzg4hrqRxVX0iDMP5qwKlMN8Azn8Ek8lX1XvDMPwl39nTFCS2bR/PwVwWkTeiKDqyuLg420n1SqVSjeP4GeAloB9wgWPA/YDWAfKDHMd5REQ+TptrIjLi+/5XnYA0yxjzAPA50AsgIg/5vv9Z5rea4nfXSUXeymAcxxk1xnzqOM5op0BBEHwBHMzaSZI8lfc3AInIzpx9MmcfAR5Mvx3LsqwTrWo2AA0NDZWAm9Nm1N/f/0MubrDpuxn1GGNOG2NOA3bekeaO0ua24eHhrRuABgYGtufaP87MzFy+huIb5HnedcAuYJfneVvzvjT3dMbQ29u7vf4rMiNJkh25MRdc172kqqXmQsYYBRCRWpIkT4dheKoV0Nzc3O+u696X2S1CpoA7AGzb3gF8DY176M7MUNVyK5i8VLUkIs+2i/F9f9z3/fGsXalUriddPhGZyuWq184D5WfomIjU2hUTkZqqvtMuJi/Xde+J4/g3Y8xHAEmSTObcdaAeAM/ztkRRdEval0RRdHBpaen1LChbJoAgCNrdf5tWFEVn+/r6EtYn5TbP87bMzc2tWgBxHN9OelAB55eWlv74N4rm5fv+uG3bNwZB8ChAWuN86u5JGdaXrGlDT7FRl5q+/0jz8/MXgbhVrWwf9QCIyDbV+qp835xIVfeJyJiIvGuMeRsYo+lsaaEYOFQul1+YmJiIrhBzLjOSJKlCOkOqmh9QbB4VhuGplZWVJ4HngP2bgCGN2b+4uPhJeui20kBmiMhaHYjGZdrbnMB1XVMoFM6o6sgmQBqkqiOFQuGM67om31+tVgeBPTmgs5De9q7rFlR1FrgpAxSRl0XkpyRJ7gLeZP25kNbQA2EYvtoOxHGcV0TkAH+/KHzgRcuyJpMkuRV4jfRgVNVfLcuq+r6/Uv8LG2MeB96n/bN2DRgLguBoO5hczr3AIaCvTZiKyGO+738Iub2wvLw8PTg4GAAjbHy4AVwERoMgaHlVtNLy8vJ3pVLpW+BhoNAiZFVVnw+C4L2so2Fz1mq1yWKxeMK27UhV+0RkFZgWkcNxHO9dWFg4tyHl1aFmi8XiYdu2/2R9piJV/dmyrONxHO9bWFj48lpzdtXV/0p/AVVNbmulpl4iAAAAAElFTkSuQmCC',
        },
        'dark': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAACa0lEQVRIie2TO0wUURSGvzMsDzU8ZmHANRRSUFGQJSZqSYxGTDRRCy3sbI0haIyFhcbCxgITWxpjTIwJlUhiIbGik1jYSkI2MnN35w6PQnYX5ljM7rryWFBb/u6ee+73n3PuvXCoQ/2vZHvAz9vLKPcETgNtB+RsCMwDz3t70x/2NDAmfKrIo38uF0DkcZ/nPtkRNya8GBirQWCLfj4aD8Ow46BMa21nkA8nAmNLCSM8vyMpMHa2snk/MbRjxtixvyneGPswMFZ9Y2eqMQdAVYVk5jhO/AZAYVphuhFweXndW15e96prka1XAAJnK8zEIAhWTwIuSOB53o9Kfhv7XLLjlCcdpzxZXVfO+oBbYZKqOI+AIMJSENgiQkv1UGCsopREuLr9hcRx8/guvgvAmIhmgUUnMZAsABp/b1TxdmUy7flMpj2fy611+8a+zeXWugW+JMw4W+sghqwAqrzr60vfrFUO9PWmd/yVRlLVBUSIkZGagUDSAU0LdbkbB4X293eEwA2AIFhZgLjGlMXFxbajxzp/AsVezz0iIgrJM1VHPGKdAIb3YH/dSnHlRDq9VNeBmHxUAlIFz211BgYGNoAQaPXD8NTvs+IT67MGcIDhpk3mjYmy1YAx9gyQAjVDIqVUwmIK5YETy/sgH70U1VZF7wLtAnPNzXLNdd2VenIURV3lsk4rjCr62ZjwhYpTRvUOgCJTCRqojGka+PPnCq8LPe7tIZHSbuV/U23pKURTKLfq4woz66vu9cFBKdZeiKpKoRBdimMZRXRTHefj8Z6uTw3GU5Pvr5yTpvgC4Igy53nubPUuD7WvfgH3sxgQ0Gx29gAAAABJRU5ErkJggg==',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAADhUlEQVRIie2Vz2scZRjHP8/sJLWJzWZ2utlNai1BF8Q00ARPjY0/SkBqb715skdBBAXBX4ccpCgWxXrwIoJ/gKdSFQwUiVQRTAXbXIJGpGl23s3OjpsUzO7OPB52djMbszapXoR8YZnZ55n3+XyfZ993Fva1r339R5JuidXV9axlNZ4R0ZF7KazKShjaX42MDKztCry0pAcGBoILoC8i9N4LNKFNhI+qgfNWoSCbXcFLS3ogna58qfDUvwR2SmRu7fDgs2MitR3zReNf8Iyv8eeaMf4pVbX3ylHVHmMq017J/75Vzxj/7Q4vrRvf99P1BreBPlR+qFYHpwsF2QyCwEmn0+si0tirgeXl5fsO9g/MC/IYcMdO6YjrulUAq/VQLeQU0AcgorOFgmx6XnlmsxYZr1SZ2ysUYHR09M+UyGz8tb8eyXQr1wYLMhnfqm1zrZm1TgK2wBO7AXle+Zznlc8lY5al84DGjIm/gUFbwV8ymcwfAERR1+O2o0QuInIxGYpH+2sTEbXBWxsnYhIBVTzP+BtAf0c3xlea1r/JZZ3TIhJu52pkPd3F0QLoQ0h7qs2Ob92quggPNk0TbYd2lICpIAgO7ZTL5weX8/nBZYDV1fKY55Vnmmv0etMZx27frh5ud9zTU59sb3DVD0XkioIDPA5MxXXfVYhE9arjZIJuxtodpeRj4KSqHiyV/Out+qlU7QQwF49660eHxrdDQ7nPATyvPIvIFEBuKPPa3WBJRaG+YFmMiEi9WFxfEKvesjSRAHMivq7kcjkvsX4jvt7ZCxRgeNi9CdwEyOcPGc/4K8ARYALiUYvIw9rc8TeSi3t7rU9qDc1ZqdSPReNfETjzTzCFL3ptnmufig7JIugRhFGIN5dKVG3meCD5qOM4QcPSS1EjfONu0OZyztQbzK+Uy0c7DKmKoq1YdatjlTmF0yhjxvivZ7POOyKixlQmNNTL8YgA+Rp0oQt2EnQGGLdD+c6YytmhIecnVbXMWvCmwCPxWOZik+339A2IO1Z+R6gAx4FUM6SXctnMKzud37irlFfy3xfkpTgUAj8ruAJHW3VtW8dd162230zFoj8uKS6jHNtWM1TRV/NZ94NuI06qWCq/LCrvtQwn9FsU6tl403X+Hxtj7gf7eUWeBO1DZTGyo0+HXXdxN9BEE8dF5Dyij4JsoHpVtfZZPp/f8+nY1/9XfwFzYnLgqs4iMQAAAABJRU5ErkJggg==',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAAD6klEQVRYhe2WTWgdVRTHf+dOmkRtPibJm3lpDDSxRRGkbUSF4qIgBhpBKqhFoVXRlNKFC8GdSBEEXbgQqi4qEoIItqC1+LFRxKiLbJqiVpAunphC3vdMo2LykjfHxZt5b/Ly8mGfLoT338zcc84958c9994ZaKmlllpqaY1kI4eqmkzeOyrKMeBeoL/JWgWFWdBpN9F3XkSCbQMtLHi7LYtzit7TJERjCbNli8d39fX9tiXQwoK321j6PbDrP4Gp6dqqpQeH+vvnNwRSVcnmvO+Ag6FpGdXXVa2pZLI31Uz1TMYfFSk/o8iLQEdonnES9iER0YZA6Zz3sKh+UqGjZIyMJxL2N82A1Cud9h8QE3wB7AgBHnKcvs8jv4kHS6BHqXnejGAyGe9IOlv8LJPxjjQLlEz2foXKmWisypNx/xogpLaJDeZczD4lMIHoVLNAAMbIB7XcsubgVIEKhUI3yG3hcMX3e36MxfXUPbeUqrZls8WL2WzxoqpacV+YeyWM3JPL5brWAZXLZn9sfGXvXlnebvFG8n1/p8JhhcO+73fFfWHunyMGVWt/5GuLXgJhLNrrqlLIZIvXge76QplsMToRi6g85br2hUZAtm376XT+/sr7gL8+QudA9gEEyBjwLcRWSFQPxIKTjWDq1K2iz20WkEwOzCaTA7PR2Pd9O2qfYOaqtQmqteObeqxqFPM+sLgF0KKovLtFTFXpdP6+5VKQzeW8jysWvVTzShWoDSCVSnUCt4e2AEpnXMd5LQqKtQnX6dvw+/fPtHoZ2gIqi3JnKpXqHBkZWTIAnV1ddxFeVMBVx3H++HeK1pRMDsx2tBsnkbAfAQhrXA3dbSFDpWVGTbVdAnP1yYDrdc8bUm9vryci5Ua1jJoDUDtle2LzfliXSeVpFZ0UNe9lcsV3UCYBa13cWpURzjoD9vMistIoIICfqv1XRiFcIVViE/Tm+omua1/YYekTSHAC5eQ2YAAslJPZnPdp5dJtqFtqZbVUBTLoXM0ux+sT5PP5odWyzADj2wCp1/hqWWby+fxQ3FgsFnsEjtUs5jKEX/v5+fmb2jt2pkDd0Dsn8JKq+UWkfLcibwC3RsyInHYT9iubUWRy3suonqb2R3FN0BdUrUvGBHcEyqvAvtCXLi3/OTo8PPxXtYXZbOExRT5kk99alBLIpOva05vBVKEy3nHQswjtm4QpKo+6rv0RxC5Gx+k/r6IngKUNJnqqZmK7MACua0+rmgnA2yBkSZBnIxhosBrptD8iVnBKVQ8J0gcsoPplELS/NTjYldsuzNqcvzsipVOIPAgkFS2KyNfBqrw9OGj/eiM5W2rpf6O/Aeb0ew75ka7NAAAAAElFTkSuQmCC',
        },
        'active': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAABeklEQVRIie2TvUpDQRCFz4jEVCqENCmE5A0iWFiKKBqw0cbKZxCxsFELRfA5xEJs1YBFtLZQn8Au8SeSwib+wWfh3LhcrrkGLHOa5X4ze2ZnZ6/UV1//LmABuATa/F1toAZU0sx3ejD9Tdu/mc95whuwCgz30PUIsAa8u8dMUlLVg+v+PQ/M/7WI79lwj9N4wICWBwvO2kA7xTAP5IPvgnu0AAsTix54CBgAKQUOgcMYu/etxRAuObzyGcT1RsILiXfg7Mz3LErSgPOyr3fdThyXmTXNrAnkgCMgJ+k69ByMFTg2s2U/CW5i6k03vo53CNDwtkoBSx1ykoCSezUikHXwGk7en+kKcJswk0i3wFisgAEfHs9E8NnBRJBYBupdzCPVgXKwb9L5Y1h1P4LAJrALvDirAaMJVzHqMTx3B9gCnpzthclZfp5XqINOm8n3nfGcuE6AIUkK79wkVSRNSfqUdG5mtfSxSsC0pFl9P/sLSVUz6/qT9tXRFzzIj068nfOjAAAAAElFTkSuQmCC',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAACQUlEQVRIie2UzYuOURjGfzfDIBmTryILZSFDjVmOz9IUmZ1/gPIPWFCysZAIi1nZSNnaaopmMkKDlJkmxsp3FmSBZopMfhbveczx9D7mfYeNmquezuk+131d9znnPg/MYQ5z+EeIqgV1FbAPWDtL7XfAjYj42BBbbVUvqt/8e3xVz6utjZje+geGZQyoC/9kfCYjD6s71ZZmzjfpLFB3qQ8yvdNV5DZ1MpEeFsejts/GPOUuUh8lzQl1WT1Sb1bd/hTrUb+rt2djnDQOZLq9RXxexukquMBwmncDLcDuBk0OqgdL4btJE2BbPeMi+DwiPqd55XOrwIX0/UJEfAFelI3zal+n47iX7qMKt9X59VzVDeqGOvFrKfdVeWFFJnznD6amO18+09bVDrUnzU9k+Suhdn8wfb8AfUA/0A7sALan+DngBzAUEZ9mMgYuAd3qYmAki3cCg0V1x7OK1mRVnyqCDRj9htKOV2f6x2C6uTrT+C4i3mf5E2mcbNY4Ip5GxECaf6D274bUYIXxxjQ+KeVfptalR9T+Ge7exGmrqGU8jdPNpw6mxLIx6np1rAHTAmPq+pJGqM/S+k2Ybq5BYC/QoZ4AzkaE6jbgOrAu8QaAxxU76gJ6gK3AfbU3IkbVecBJYFPm9auiNvVtVvVrdVSdymJ9VrzfpDE/cQpMqSPqm5LusnLiVvVVnaObUo9WGdYp4Gip4AIv1Y6CF6WkpcAhYA+whFpDXImIcZqAugU4DGym9jKGgKsR0fTrmMP/i5/Y7jt8yCuSawAAAABJRU5ErkJggg==',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAACe0lEQVRYhe2WzUtVURTF11ZDLTNrEIEUpf0DhtLEPuBRA4OQwKKJFDmI/gghmjVoVk0a2qhBIeQoigZBNjCJBtaokDDKTJ2YPvXX4J372u92331etUFwF1zeuWevvfY6X/cdKUeOHDlybAxAHXAZGANm2TpmgafAJaAuq5nDwJttMFENr4FDWcx8+YdmIkwDB2uZMeCVS/oF3ASOZJriZO0O4FbQjPASsLSk8468DJzaqpGEGgVgxdXpSyM/dMTbrr+f0obs3yZTd1ydkTTiR0fsdv3zoW9+mwx1uzofqpFagbVAWgEaXayMDEUbgNHw1MdijW7Z1oDdSQInXd23sdhmDLUBxfC0JcQnneyJqN9/oI659g9gIcmEE1lI21NmNi+pV1JvaMfhB12u7Q11ufYBSa3VigW0ShpKI5jZuJmNR+/AXrd83lC5drUZGpG0WMPQoqQHNThlAMclfZP0OHRNJBmKyE2xTdYSi2feQ0mGwn4aDe8t7hAVgSZP7nE1pxLEtmwo6PglEzDlpHukP0vml6vihAUsxH43BTP7aWZrVWp1eUNHXeBdgtYVSWOSrgH3gVVqYzVwd6R4fO/aHZLUEF6KLrAzYWRPgOeSHkk6m1LAo17SdUkdwICZJR2SXa69Um4BA25kn4GKIw+0U/khy4pJoD2muYfSFSTCBR9sBr664ATQB3QCF2OJ68BwrekBhgM3wnQYeCdwLjbAGaA5LjAQE0jCMjBYy4zTHAw5aVivmJ2YwBCwVCVxDihs1IzTLITcJCwBVz3/r9sapdvhDUmnJe2TNCPpmaS7ZvY9q6GguT9onlHpb2lO0gtJ98zs02Y0c+T4b/Ab4ASjtCxiEJMAAAAASUVORK5CYII=',
        },
    },
    'macros': {
        'light': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAAAp0lEQVRIiWNgGAUEADM2QVlZWVNeXt7TvLy88p8/f95BiQUsMIa0tPRhRkZGzn///vX8+/dvNgMDAw8jIyMrJYajWMDAwMD+//9/Y0ZGxuVQ/sKnT5/mSUtLH2ZgYLAh0dwjT58+tWVgYGBggon8+fPHh4GB4QqU2/f06dNEBgaGP2Q6HDsQEhLik5GRMaeqobQGo6kIFxhNRcSD0VSEC4ymolFARQAAn/JraCH5bvgAAAAASUVORK5CYII=',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAABoUlEQVRIie2VMYsTQRSAvzfuZRuJIVaZmSqksztBy6vUSo9TLCwsRLSx0MrCf6AgpLA9wc5CDhsbO+UED+4a8QdEmF2D1ZEiGJbdZ2EMK3eX4naLFPt1M+8xH+8NMw8aVoSo1+tdBKK6DzYnBbrdbts599EYs++9H9YtXlTivb+kqh9E5Eue54+NMTvAOoCqSt3ixYHW2kci8mq+zIA1ABF5H0XRnSzL7gE3K7gOReRJCCGBUsWtVms7y7JN4EpJ+jqE8BDInXMvgLMVxKjqV+Dlf+LRaPTbe79ZFMWOiFxT1edJkjwDdJ7ylOoVv12WYJxz5ysIVpszy4LW2qvtdnvY6XSSyWTyo07x4o4Hg0E8nU7vG2MOQgh7zrm7wDawVhQFwOc6xcc9pxnwBngwj0+BG3Ec789ms3VVXdqlkzDG/AohfDsi9t5fVtVPQFzKP1TV62ma7lprv4vIhdNIS9xOkuQdlL7MEMIecIu/FQP8FJGNNE13K8qO5chX6L3fUNWtPM+H4/F49G+/3++fq7PVK0szFmujGYvNWGxoODV/ADaKzO+pMHD2AAAAAElFTkSuQmCC',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAABIElEQVRYhe2UsUoEMRCG/4nCsvoCkunUexVttZPDVrARrrY+LQ8sbCzOzkqwtvBNUibpBTm5LSYW3snuNrvs7iHifN0kk8w/k/ADitIPapNkrT0CMALwGGNcbFLQdlMCM18DmOJb/AjAZJOCTK34LTN/WmvvAGwx8wzADVpOcggqhZj5A8DuKnQADkvbb0VRnGRZticip8aYSjNdEBExxrx47916rfJkKaU5EV2twrKY5zzPz0MIS2Z+JaL9lFJfPSAipJQuARys1ypdxhgnRDSvnXsIIZw555a9FbSg/qnFe3/BzO8AxgDuQwhTAD/jIKJjERkD2Bmg/sIY8zTAPf8INcYm1BjLqDF2QY3xz6HG2IQaYxk1xi6oMSqK8tt8Adl32jLeeS25AAAAAElFTkSuQmCC',
        },
        'dark': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAAAmklEQVRIiWNgGAXkgOdv3pi+fPXu6YtXbydSahYLjPHy1bvDDAwMnP8ZGXoY/zHMZmBg4GFkZGSlmgWMDIzs/xn+GzP+Z1gOEfm/UExEKA9qsQ2J5h4RFxOyZWBgYGCCifz7x+LDwMBwBWI2Y5+YqFAiIyPjH8rcjwbevn3L9+LFG3OqGjogYDQVMYymIorBaCpiGE1Fo4CqAACw1XfsY5IY+gAAAABJRU5ErkJggg==',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAABpElEQVRIie2Vvy9DURTHv+eipaSlfX2v/gYbCZtOmEgQg8EgQiIGBpP/wCAxWEns0gix2AiDpBaxdaikpH33te/Vj0ga3juWp34vzxtI3me795yczz03ufcAAX8BZm40jGoPMzf6XVv8FKhUKlHDsA4ddrLSsNb9Ftc7KZbLvcIRByCc2k+0+GxzBkC3Gya/xfWCUpoLDGy4yycATW7G7uPD7WQkEpuGoDHPJka1QdhLiqLcfBDn8/nmSGv7HsADb7m0pSXb54jI1qV5D6DNsxgAiJe1ZGLtgxgACoVCSyjcmgEwxMCqluxYISIGAF035/3s+GucWVxf3yU8C/41ul4ZlNLcl9Lq97t2/TnlchyORq0ZZuc8lVLOdN2aAvEmA00EBoBjP8XfPacaGNsgzLrxR3bESCjkZGs2dQvHbvCmElLT4hdfxKVSuY+EOAIQfpddJWBYVeMnumFegtHlTfoq4wlVTewA777MVEo5E4RxADV3qwhGWlXjJ7+R/XyITxiGlWZ2Rm1brHd2dly97pumGfPzqv8mwVj0k2AsBmMxIMAzL47O+m3JaSu3AAAAAElFTkSuQmCC',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAABJUlEQVRYhe3UP0vDUBQF8HOaQEEHo5BUMqr5KrrqJsXVUejcuToKCi4OujkJfgK/SXXOe9BoRaqFNtfFP4lUUpIWEe9ve3DJPe8RDqBUNZxmyJjeJsloNHq9DMNwMM9AbtFAbJM2gI4AdN16BKA1z0C1b8uPjE1eYts7ERHHmIdjAoeY8iVnIbfI2OQZwOL7sQtg42tSbinjbaC+KjLeIfOXKUMEKencBMFSd2Kg2CanBA4m5L5+6nt7UcShsckdgLWqYTLuG8HK+schd8uGv9wS8CI3LjgPfG83ijicYYgf5X5qkqmI7Fv72AfRBHEW+F6HpHzOwNkSpk2ILFTeTg4otavK3/lXtBiLaDFmaTGWocX452gxFtFizNJiLEOLUSn1294Ak4L9MGY3IxsAAAAASUVORK5CYII=',
        },
        'active': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAAAeUlEQVRIie3SsQnCUBSF4f8F0zpA9hCXcIC0LiRkAydJky6kdAW3EH6bR4hYxffA5n7Vrc7lwIHwC/WkPtWhNKvZhE7qovbACHRAW/pgpc5+uquH/Hiv6asBcAEe+b4B15TSq1qD3OKonquG/kWsKFZULlYUKwp1vQErLEgVqBtoIgAAAABJRU5ErkJggg==',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAABK0lEQVRIie2VvS5EURCAv1kEDYUH8A4kthEbDbWfRqGQUKCR6LyBTqFVeAHZRKeg0WjFA2hEQkEiEUvxKfbs5q5NFLt3ZSX3q+6dSc6XOZkzAwX9gDqoTquDfykdUy+tc5z3+aWMaEZ9VqvqJHAFzKd05C1uHqjuAo3KvoCh9F0F1oANYLkL1yuwFxEPLVF1RL2wlRN1IOXf7J79torT4aPAGbAIHAIHEWHKbfek4oy8pE50IfjHqAvquTrXS8mwuqOW0/+6+pma4jxvX3YibVJ/TjX1FNii3nzvwJE6DkwBAx26niLiti2qltWPH+3/os6m/F0Oz2m14WtOroi4AVaAWgo9ApWIuO6wwl9pG4VqBVgCjiLiPhPvzVX3FRZrMUeKtVisxYKCjvkGjEPiU9wCLLYAAAAASUVORK5CYII=',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAAA3ElEQVRYhe2UMU4CURRFzwMSEtiAC3At2EpLT8UOqNXSqImFBa0NhStgJ7AIEiPdHAvHMEyggDCSmHe6l3f/v/cX/0KS/AHqQJ2ovUtnQZ2qhT88Ne3Xqpk/qBv1WW2rj8A9EE0H+WXHSP0E+uW4BK4r6wVwC1wBQ2qPOZEC+IiI5d6t+uJ+5mq31KwOaE5ldTCu2lJntQNvaruiaTRQpzpERKGOgTUwAl6Bu4iwIrspd+f4cV/A+xnuSXYxi3FrnsVIFuORgbIY/ydmMW7NsxjJYjwyUBZjkiSX5hvmilQKJMXJsQAAAABJRU5ErkJggg==',
        },
    },
    'spoofer': {
        'light': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAACmklEQVRIibWVT0hUURTGf+c6TYo0gsiI984olARhO2mTWOugBEsjIqKIIiKiZQS2iKDAdVEEJS0sIl0IbcRFf0goiiDaBsF791FqZWhhNs5p0RvSccYmyW/z3jnnnvN955z7eLDGkEoOZbPZTap6WFUPAojIoIjcCYLg3aoJGhoaNiSTyR7giIh0ljirqvoUGJifn38wNTU1UwmBcc7tBI4A+4Da2P8NGAIGYrts3Hv/GMiXJHDOPQO2V6JwhQ7Hvfcd5QgUQEQuVjpjWLKjCwDe+9Kjd85pgWQ1KJVvVlusUqyaoK2tLWmtPeWc27wWBInp6elBEbkK9P1vAuOcu83va/pFRPr/hWAhflYBWGuPZjKZr865ntgv1trrwCFgRkR2hWH4ZnHOohoAJIoIPgHpdDrdMDEx8dEYk1DVFHDXOYeqdorIceC7MWZ3EATPC4ktLS3pXC4HMFG2AxEJAJLJ5BaAMAxvApdiIfdF5AzwQ1W7gyB4sjg3l8ttiV/DsgT5fH4MQFW7Cj7vfV9MIsBPVd0fRdEoy1HIGVsierHR1NTUbox5Ccyo6sYoiqYKMWvtAWPMhzAMHxVXbmxsTCcSiXdAraq2R1H0uoSA33DOjcRf5EixgDIQa+2wc06ttcPFwWXXdGFh4QzwBdiTyWSu/K26c65fRLqBz8DZ4nhVsWN2dna6rq7uDdAL7EilUk3Nzc2jk5OTS65fa2vr+urq6usichqYF5Fe7/2rZe2VU2at7RKRe0AN8MIYcywIgrcA2Wx2az6fvwVsA74D+733D0vVWXHG8dKHgJZY5RVVFeAcsA54r6p7V1rqX5dYX1+fqqmpuQyc5M/O8iJybW5u7ny5X2XFBAVYazuAG7F5Ioqi8Upz1xS/AOee/W5tFI5kAAAAAElFTkSuQmCC',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAADvklEQVRIic2XTWhcVRTHf+dNJ2GUZMKAmeTOS4QhFjTL4MKKijSJHxTbKtqFNOnKLFwq0oVKd24UXdiNuInJwopUW4hCW/CTCqIgCBqiZJHMu0xiSHAiM5Nk8o6LuaOTl5lkom31bO7HOef/e+++Oe+dgf/I5B/keMaYYc/zngEIw/ADa+01ILwp4EwmcxgYB8YAP+LOAe8Bk0EQzP1rcDabTW5ubp5S1XHgSMRdcmMisn9dRCbb2touzM/P/34QsGeMOSIip4Fngdt3JIh8D0yp6nSpVNpKJBIngNPA0YjeBnBZVaestZ8ClabgbDab3NjY+Aa4O3Ix+x7lPo/i5/b29vvqT2AHuK+v78EwDL9wy5KqfgRMHvDH4xljhoFxETmJexSe5z20uLj4ZS3oUH2Gqnp18ycc8KAWWmuvAFeMMcMicjWqDeA1TK1eYaWZr1XbS6Mp+GbbDQMPDg62+b5/Mp1Od98y8MDAQPva2tplVb0Yj8c/vCXgoaGheLlcviAijwCEYbhwI8GHent7h4hUARDL5/NTqnocQES+Ap47MDgMw626+V8Q3/ff8jzvO9/3Z3zfr70ivUwm8y5wyq2/LRaLx6y1xUYa9dq7wJ7nLdct76hNVFXcOKqql4wxt2UymbeBMy7kh1gs9ujq6mohopeumy/V+3YcXbFYXEokErXAO2v7IvKiqt4FjAAjIjIL9DnfT5VKZTQIgjUipqr9tXm5XN4B3nHH7ornXNJIbT+Xy5VE5Dhw1W31ufFXERnJ5/O/RaHORt04u7Kyst4U7GzGjQ8YY2qARvAFVT26uLhoGxGNMf3A/W75SdS/C6yq04ACcRF5ud6Xy+VKXV1dx0TkyUqlcq+1dq/SeQWIVyV1Oups2Aj4vv+xK5FQVR9zL/2WLZPJPEz1ZGLAxSAInorGNKxjVX0J+APwRGTKfWtbhR4G3nfQdRE52yiuITgIgjkRmXDLbuCz3t7eaHOwy3p6eu4BPnc5qOpELpf7pVFsrJlIoVD4saOjoygiI0CHiIwlk8nZQqEw2yje9/3HRWSGv+v/VWvt+Wb6TcEA6+vr15PJZJlqP9UOPN3Z2ZmOx+Nfl0qlDYBUKtWZSqXeBN6g2m2oqp611r62l3ZL7a0x5oTneZOq2um2cqr6PICInMf1WCJSCMNwzFp7aT/Nlvvq/v7+7Pb29jtU776RXYvFYhMLCwvzregd9J+EGGPOiMjrQMrtrQIvBEEwSbX+WxM6IBiA7u7udDwePwewtbV1bnl5eWmflP+P/QkHDnd/Jn4R3AAAAABJRU5ErkJggg==',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAAEYUlEQVRYheWYXWgcVRTH/+d2ko1fkxKimZ2ZjUt8UFz8SIOQKopaKKIoqFhT0KaCPhTzoEgeBBHBl76UWmhQEUwqYggNWC0pEvMggrZQU7Si4kckYefsbo1I2AFl2e0cHzLTzG5m9qOxTcH/y9x7zplzf3Pv3I8Z4AoTbTSBbdtdAB72qyccx/lrM4CUaZoPEtEeAE8CuNq3lwB8DuADwzCOzc/Ply8pkGmatyilhkRkL4AbG4TnReQoEY0z87f/GZBt212e5+0momEAd8WEnfavsX4ROaKUmmw0pHFAcUMS1roeaKIHGw7pOqBUKnWT53mzAPpiEn4K4AgzfwbgfMwDbbEs6yEAwwAeA5CIiPldKbUzm80uhI2qNsrzvKcjYE6LyAgRmcy8i5ln6sAAwHlmnmHmXURkisgI1oY1UJ/fVpW0WgMRtYtIUJ6tVCovFwqFH+s0Xlf+OzMGYMwwjFs1TTsoIjuDtmrj1/VQjU5uBKZWfq6T9WIaAV12/X+ABgYG2kzT7E+n0x2bDtTT03NNPp+fI6Iz5XL5i00FSqfTHZqmfUJE9/mmOxAxmy8LUCaTaS+Xy9MAdgQ2ERkFUGk2R9PkAJBKpUzP84aIaM5xnLO1uVZWVj4C8EhgIKJXmflwK22s6yERKYXKVQuX53lTAA6IyCnLsnaEXMqyrHGs7nuB3nQcZ39E/vZQuVTrjwL6IygT0Q0xD3IVgOM+FJmm+Q6AZ0L+A8z8esy9RqitQkMgIjoXuiFZFazUXgDZGqhpInohFPY2M4/GwFQBKaXO1TqjNtfw7juI0HuWzWYXlFIP1EA9EYqfYOYRABIDowHYHlRE5LeGQPl8/icAi361K5lMbg/7I6ACTTHz8wC8GBjYtn0PgK1+dYGZf2kI5OvEhQClXqx1hqCWAEBEPjYM41nUP5LAP4YE5ZmomMgTo2VZdwI44/s9IuqPmObo7u6+LpFI9DHz96jTMwBgmuY2IvomyAlgGzN/1xSQDzWNtWl8logGHcf5p16jcUqn0x3lcvlrAP2+aYqZh6JiY1dqEXkNQABwu+d5hy4GBgBVKpXDIZi/AcQtCdgS53Bd98/Ozs5lAI8CABEN6Lq+1XXd2VZgLMs6CGBfYBCRfblcbq5lIAAoFovzuq73YXWDBIBBXdeTvb29s8vLy3Vf4Ewm055IJN4NwwAYz+Vyb9R9gnpOX5plWR8CCB/Iv9I07amlpaV81A3+nncUwN0XGiKadBxnDxpstHV7yJfnuu4xXdfTWOupXs/zhnVdX3Jd94dwsGmaQwCOA7g5ZJ5g5ufQYFkAWvyUtizrFQD7UX1KmCail0SEABxC9cpdATDKzG8120bLPxts275fRCZQ/WXq+rmuDdkWlVLD2Wz2y1byNzNkVSoWi4ttbW3va5p2PRH1+yAJAMGxQgC8VyqVHi8UCr+2mn9D/4eSyeS9SqkxALf5pp9FZKTetL6kQMDq10WhUNgNAIZhTF7MP6ErWv8Cz8y8bo4qKhQAAAAASUVORK5CYII=',
        },
        'dark': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAACjElEQVRIibWVP2gUURDGf7M5YxEM5s/txjSCgiCkS6cEW0UNREVERLRQxCJYihALERSsFcVCsYggJkVKsfAPBiKCIKQKAUGLfW9v356ECDHZNxa5i+ZM7o7DTLdvvvm++WZ238IWhzQDMqa8F/LziJwFQHUc2p5F0c75lgWSJNmhWjil6AVgaAOsAu8FeSqy8rJYLC40FFDVIC6VDwWqF4CTQEcltQhMeJGnAPXyfb0734qI31DAWPcBONBMh3UcTkdh98HNBLRi5VazM4Z1O7oJEIXdG4/eWKdrIi3ERvVBq2TNRssCs6rtxrirSZLs++8CqlroTcrjCPc9bWP1sIUWyANbyp6w+ppmeO7Vw9c6yCskbQDWZheNdT+sTU9VzsXa7CHKOWBBvT8SRd1f/q6pcmziQFOQ0FrbCxgvviAqnYo8tzbFJG5IRC4BPwU5FvX1zlQrS6VSCKt91XEg3wB80L4foK/Y81jQ20BBkReCjAJLqI6EYde7ddZl234AQb7XG9FrgMAzXD0Iw56xiogAyypyOop6XtXUEfh8GMCjr9e1/PdDkpQHvfpPwEK+UtjT399ZqubixJ1RJN5V7HpTSx7HC6EEy/NAhyCDYdj1uRazFta6KWOdWuumVLXhda6qYmw6ufoVp5P/OKs9yHMZBTKF47aU3W0kYG35HsgIissLcq0RvuriiDFuyVinxriHs6rttZi5Od1ubfa4glmyNj3cFHk14iQbNtb9XCXIZuLYDazlYjdgbfaxcrktJok7uhlP3RknSXnQ4ydQdqP8Ar0LCCLXgW3AV0FO1FtqwyWmadq5kssd4Ap/duYVHrRJfmOzX2XTAtUwxh0k4FGF/nIU9Uw3W7ul8Ruwk2SFGzGIugAAAABJRU5ErkJggg==',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAADzElEQVRIic2XTWgcZRjH/887s4nRttlsMjtJcCsIBvQqHqyoSJooUmzjwR6kTU/m4FGpFVR6U8SDBwURLzFBrfjVQhXagF+oKApe1JIUD03d9p03OxObakx2Zv4eZrbdnewmG22rz2X2fT7+v+d554N3gf/IZLMFJJXn+TtF1CPJOn63WCzMiEh8VcDGmCHSGiewH8CNmfBZAd4UiSYdx5n912Df97urEfaCGAewIxNeTq9dGf/XEEzmLBwpFAq/tw0mqYwJdpDYB8GjAG7IpPxAyFS4ak13dVWrYaj2QGEfyOGM3gogxyiccvt6PhGRsCXY9/3uMMQ3BG7NwDbcyvVuhQC/2DburN+BBrDnBfcQ/DxdLoP4EODkZh6e2sMHyDgEY0hvhUDuLRZ7vqjl2fVFkUAp1hTih1y3b6YdWMN0SYMnAJzQemEnRJ2sadfnqWbFABArK2wVa9fW02gJvtp2xcA/kR1aB2Pnzy8Vrxl4bo6dfSY4BuEHoqrvXRMwydy2fHAEwP2JA2euGJikbczi7STtjN8yJpgCsTt1fRlFfz22abCKWb38O7oE8Uzwcsz4e88Ex+fn57tSqPKM/waBvUkX8p1tcdfg4OCfzTTqtdeARSzv0jRKOfWh9Dra0bnlaLlcvt5bCF4B5EDq/7GzUx7o7e290DiWcuu0dX2oYessK9RhlDAUcVPNv7ryx5MdnVtuATgCcETZ150CUUo6xM9xnBvN57cGyJgQ22vfI5HVBnDDxEnHMgsAJEdq/lKptLy6cnE3ICfT8Utp6LRS0cjAwFaThaY2mvSGU47jLLUEpxMcT1u8+7dKpQZYAwdxJrIx7DhOuRmx7PvbCdyVTC4fZ+NrwErJdNIkcnYoz9THSqXS8oKT3wXKw2TujsFCoeWrY4fyLIAcAIpgulVeg2njf6Q9n9rzI60ro20V1dk5E9ynPT9MNCrvN8tp+h4rRAcBXASgIJgyxgy1CzXGDCnG7wCwACwJ7ENtgx3HmaVgIllJMab1aXlhIXs4WGPnKpXbYlqfAVIEAAomisXuubbBANDvFN4CcTBdDlqx+lbrYKxVvuf5D6pIvgIwkPQrz/U7hbdb5W94yvQ8/ykCz6e5MYDXbItP1z4WlUplWxjLCyAm0kEI4pDrFl5cT7et463WwR4IJwFsS11nKfI4AAj5Ki6fsS4wlv39/T1HN9Js+1yt9eLNUNHroAw3V5IZxDLhuvlf29Hb1D8JkmLM4gGSL0FQSJzwReQJx8lPigg3kPhn4JpprV2o3GEAQFw97LquXr/if2R/AyShooSp2KpGAAAAAElFTkSuQmCC',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAAEVklEQVRYheWYW2gcZRTH/2c23aRJS/Y23zSBIMQXEYymILQWRS0UUfDBokmhN9A+GaQieRDEF0H6UmKhxQvFJkUMoQFvpEjNQxG0glI0JfWCcSt5yO43ewviJTUzfx8yu5nszt4SbQoeCMyec75zft/58l2B20xkowHmFxdj4b+cxwHgZlvoYk9nZ+6WA5E0tM4+CsM4DGI/gHbPtCTAZwDPm2bsQxH5+z8FymQydzmODMKQoyDuqOO+QPCCAeOcUtFv/zWg+cXFWOuScwCQIwTvDw4iXwNAbTvHllpD4/WGNBCoxpD4paICDVSw7pBWAKXThTsh7iUAvUEBAfnYEI4lEtFPRcSp0qFQJpN/zKUcAfgkgNYAt19AY59lReb8ypaKYOIOSBlMMyUHAA90CsBUjSHvpbgDAF6vCSRkGFIq3CU3xBe74rHr9SCqideBMwDOLGSzdxuOjADYV8pVJkbNaOSVrnh83TDl0hWPXwd5pZZPbaBNkP8PEMktWuf7k8lk26YDpVKpDm3npwlebd/WeXlTgZLJZJuEtnwE4CEAAHEvyYrZfEuAZsnw1o7OSVD2FnUCDIvIcqMxGiYHANu2u10Yg3Bl2rJiM34byRZtF94H8IQP5mWlYqebyVFRIRFZKiURWbNwuQxNgHISgq9SqUKpCiQNncmfA7jfR/iaUrET5fH9Mf25qgK5oF41iqrSka1iuJ+kUoW9JEXr/FsgDvqynrSs+KvBTblj9VtSdYEgkvbBda2NZRwlMO+H0nZuEoJjq+3xplKR4SodgUB8QEyX2ysrJG5p9xVil3+GWFZkTmg84ocC5ClfglGViA6JCINgvFi7S/HF+bkuUHci8T2AG153Yrad3+23B0B5rphQZuw5EXGDYAAgkynsARDxfs6ZpvlTXSBPLvq+ny83FqEg+HVFww9MM3qo2vmoKC4xVPwmOBXkE3hi1Dp/H8Grnt0F0V8+zQHAtu3trtvSq1TkWq3KrPgWdrp0v1mNKTstK/pdQ0AAkNb5Sd80nrm59Puunp6eP2slrSbJZLKtvaPzSwD9XtIJpWKDQb5VV+qQ4bwCoAjQFw53nFoPDEnp6IicLsIA+EPEqbIk1ABKJBI/QHC8pBAc0zo7QrLhqxNJse3cCMFnSzrwhaB/5oYlrXNjaZ1j6S+de3s24OhZLrNkWOv8WX/blM6+W69d3d6SbLHt/HsEBnzqLwxxnjZNcyGojW3b3S5DFwA8sBoI40pFD9fbaOvu9iKybJrRgwDO+9R7XIZmtM4+U+6fsnODLkMza2DAUaWihxrZ9Zu6Sqft7EugnMCaU4JMhgznuIjIsoNTa1duLFNkeIcZfaPRHE0/NizY+YcNcLTsZvqbF2ubT3dDIEeUin7eTPymD2hdZvRyi8E+gZwFUNyztvtgCOIdQ5y+ZmGADb4PaZ17kCuXwHs81Y+gO2RZien1xtzwg9XK7aJwAACUioyv503otpZ/AE/X7nPsWtfrAAAAAElFTkSuQmCC',
        },
        'active': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAABoUlEQVRIic3Vv0uVYRQH8OfRsMmmSFoiEJokCIfAQLIhisBJovUOLf0PDbdchJaIIHQK13BwDJcGh6SxraGmoKGgssj88Wk5Vx9u1/c+Nx068PJyzvn+OM+Plzel/yEwjjbexdPG+FFFR9HCK+z5O/ai18JoregQZvAcm4XYZtRmKvpDTQbrtRM2rHC9yaATA+1xcUagr0GtcA3/8P06pvhnA4zgHi4MQqraIpzAi4AvV/NrDOIqLwf0Cy4OYrATmOHIW/iKucgzFgPzDZcL7nDUd5oMPgVoLPK7kW9jDo8j/4HpLu7Z6H1sMngToKtF7WHUOh/TL1zvwb0W/Y2y3n2L1uI92ynknO+nlOZTSjmltJ1Sup1zftljvg5nrUdvf4rJYn9Pd/XulCvr6p3B91jlpUMNArwaJqvIjeC0f/ArwVnph084H9cPFirwjwL7Gef6GgTpJraC+AwjPTAnsRSYLdyoEi8EZvEzBF5jouhNYKO4srcGEi+EJvGhmLKNB/gdtfd9D7XC5BSeYtdB7OKJ2l9lpdEVvI1n6tiEjxp/ACcpuLq+r+hAAAAAAElFTkSuQmCC',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAACQ0lEQVRIic2XP2gUQRTGvzmjEsFLIfFAMEXAA+3UVkKE4xRRYxpFC4lNWiv/FZaWioV/CNqJhUFFBRsTsZA0ksLGiFiZQ+GEIDkliCb3s/AdPjd3e7uXJfpgYHjzvu83MzvM7kr/KEJaAZCTVJJ01FLjkiZDCPUsJ+aBReASUGF5VGysmBWsBxgFpprAFqxFY8o0PWlhOWAPMAZ8a2I8DZwGNgF54CQwAdQjdd+BceAQ0JVkhTOdbGWbRzETuwPAQGQr7wJlfh+oNDtWNq1/FANxokFXWEoKi/ErOb9BPxa3ksWVguM8Em9h1pEZGFgHDAObVw0MrJf0RNJDSfdXBQyslXRP0j5LzWYGBrqA3dGLAFgj6Y6kIUu9lDTaCfin63vIVUnTkp4C3QbNSbot6ZjVvJJ0MISw0MLDey8Df3b9XtdvvMXKkh4DGyRdkzRi+deS9ocQahG/gutX1Srs7m3EeZfvBp65sVnXfwP0tvC74Oo2tgRb8TsrfB7JR+EA74EtMV4vrO5tLNSKr1jxD2BrDPwD0Bfj02ceAJeTgHfx5xU31mQ80UUB3DKPOrCzLdhEj0y0BJQTif7W7wUWzeNBGmER+GrCKik+aUxbNW0N2JZ21ifcIfoIbE+g2QF8crrjqaDO6IwzqQHDMbUHgC+u/mJHUGd4zh22JeA6kHfjeeCGjTUO09kVQZ35EWDeraYCHLbmv7HmgaH2jung/cAkrWMC6M8U6uABOAXMOeAcMAKk/ivpZAIF4Ka1QnvFfxS/AJVve3+12N5bAAAAAElFTkSuQmCC',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAACuElEQVRYheWYO2hUQRSG/9nshgSJRQrRaLcgMYUbrU0UkirRShQFO8EmjSgWppAFGxvRgIovDFpETaIgwYCgYKEEUgS0EAtDxELEJyhRQzSfxU7YcXJf+3AT8FR35/znn2/P3Llzd6UVFqZSA6BZUo/9OG6M+VypZzkQKaAbuAHMUoyfwBiwB8jUAqQVyAOviY+3wADQXm2IZqAPmIyYfDJBvs8ub1kQYUsS2YEEHSx9SYEsMB1hOAz0AnURHnVWM2xrgmIayCYB6q9my4le8v4kBnmn4AHQVipEhHeb9VyMvK9JxXhMGGNeVAvIek1EaeKAah7/DxCQAbYADcsOBKyS9FDSlKTHywpkO3JPUqcdygHpZQEC6iWNSupyho8ZY379EyCgBTgCbA7IpSUNSep1ho8bY86VMkdQK+ec63ovd1vSNkk/gF3GmEcWJiVpUNJuR3vSGHMqwN/1nPOTQR1671yvCchLUqOkMaALMJIuSjrg5E8bY06E1K51rt+FaIoB7HQe7fe9XBZ44+S/A3e88+mChQzzH3e0PWE6t2CTU/DJ3yEBUG4M2uUL804DXxz9xlggWzjjFHUE5IOgbhHxSmLrtjv6V4lgbOF5d6IQTZbii9hdErxwASOO70ApQO3Agi38TcA2t7omIBe1TI52q+eZSwxkDUadb/MMaCzJ4G+vBmAqrutxJq0UdtFiXC4TxgBXHZ9Zkt7MAWaHvBv3DBFbOgTmrOdxsCwYx/S6Z3iJwpkVV1fvdQbgWkUw1jhNYUu78QRYF1HTAjz1aoYo4dRPAuV36gOwN0C7D/joaQeJeUaVC3YUmPcmGwHWAxtYeozMA4erDuJB7WDpL9OvwDdvbAbojHesDtRq4ArFB50bCxRu/KaawHhgHcBzB+Yl0F2JZzX+sMpI2m8/3jTGzFfquaLiD5lYyViHMm3QAAAAAElFTkSuQmCC',
        },
    },
    'latency_fix': {
        'light': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAACeUlEQVRIie2VTUgVURTHf2dmCNGMUuqBd3gJ2cI+QKLsC4Ry16IWBYm5kdq4itq0ateqVS3EMgQ3UYuiFkFUGNEi+oQIs00KvXn3mYFJGUZP7zstmhfP5+jL1v4393A+/v97zpyZgRVUgFRKSKVSNb7vt4vITiAFICKfC4XCa2Aol8vN/JdAGIZ1qnoeOAWsXiRtGrjm+/6FTCYz9c8CxpgDwI34xgo8Bx6LSEZVBUgDB4E9ccm453kdURQ9rSgQhuEhVb0DrAKeiMjpbDb7LukiDQ0N+0XkOrAR+OV53pEoih4sKhCG4WZVfQPUApettWeBQhJ5KpXaEATBELAFuAccFpHvIrIjiqLRYp5XWqSqvTH5TWvtmX8g3waM5PP5E8AtVV3jnOtN7MAYsxd4BkyJSFM2m/1amphOp9c5546raq2IdAPNwPDc3Fz7xMTEF2NMPfARWOt5XmsURa/KO+iKu+gvJzfG1Dvn3gJ9InIRaFbVD0VyAGvtJDAA4JzrShpRG4CI3C0fiYgc5c/mzJT4BorkRcTLgYi0JQk0xUmJG1PkKIqIyHR5sKqqajg2NyUJVAGFXC73cwGr6m0gA9QA1UAm9s3D2NjYj9isThL4BnjGmLryQmvtpO/7LUAP0OP7fks883lobGxcH5t/3+qgJP4e2CcircD98uL4U3Cl3F+KfD6/S0RQ1ZEFHajqw/jsXIpkKYhIZ3w+ShIYBOaAjnQ6vXW55GEYbgeOAbPA4AKB8fHxTyJyFQiccyeXK6Cq3UCgqn3W2mzRX/oMmJ2dPRcEwWhxQ4wxL4DWCtwvrbW7VfWSiETOuf7S4JI/nOUIVMhZweL4DdrcBvkiMafaAAAAAElFTkSuQmCC',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAADaElEQVRIie2WT2gcVRzHP7+Z2Y2J7hrWZifObKC1OYggnkSLi0oRQZJ68aJ47CUFBZFee+hFQVCM4kFLBSmeRJAasSeL3vQkxGhBgkvmj7saFDao2TUzPw95s84ms+nWCIL0e/q9937v93m/9+b93sBN/d8l4zrOz89P9Hq9R9M0fURE7lHVIwAisqmq31qW9Xm1Wr26trbW/1fAtVqtOjk5eRY4Axy5jvvPqvpWv99/bXNzc+sfg33fXwAuAHfmuneA74HItBvAPODkfGIROR2G4ZUbBnued1ZEXsn5fKmqy/1+f2VvNrVarTo1NbWoqi8A95vuVEReDMNweWyw53nPicibACLSVdXnoyi6BOiohZp5D1mWtayq9/H3DixFUfT2dcG+758AvgAcVW07jvPYxsbG2kFAM28B+BCYEJF3VXURqAN/ikgzDMOv8v7Wnvk28I5Z7TawcKNQoJem6UUROQX0gJKqXjCxh0ADNRqNZ4ElABE5F0XRB6NgMzMzt1UqldL09PTjeSjwVBzHn3W73aharSpwEnArlcq1ra2tbwozVtUzxvzRcZzXi4Cu69Z93/+4XC53HcfZAi7noVEUfZL5lkqlV4GOaS7l4wzA9XrdBU6YBbzXarW2i8CO41wEFtn9PsTESPdCAVqt1raIXAIQkabv+3fsAzuO84AJhGVZhffP87wp4AnTTHNDqqpXi+ao6qc51oP7wCJyPOe/WhSkQBl85DXb2dkZnKuI3FUEvj2zwzD8tShIHMe/A/kMsvlXzNg+dTqdXzJbVQcMK9fZy+xGo3HLARmcBlbYzVKBFdNXKNd1J3KMwQMyVF8zI0mSo8B3IzL4CTjluu6tpv3bKChAuVw+lqa7J2JZ1oCRB3+dGbZtN0eBcws4EJgpTdNmZovIgDHY6iiKVlW1DaCqT48TdByp6jPGjIMgGFTBfAFJReR9Y580NftQ8jyvKSIPmwUMPTJ7K9cyuzWa7HU6jEQkexL/sG37jfxY/oyJ4zjwPO9lETmvql7W77ruMdu28/d8pJIkWe90Oj+YZvYD8VIQBHHer+g9tjzPe9K27dUgCNZnZ2eP2rZ9jd16PI56SZLc3W63W3Nzc8eTJLk3juPLDFe64YyN0jiOPxoTcqCCIFgH1ovGxvrLPMRW39R/r78AYvNbn2tnDHcAAAAASUVORK5CYII=',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAAD8ElEQVRYhe2WT2gcVRzHP7+3m02UuiUlNXbeJC42giUHTz2JKCo1h4IWDwpSbGtrqUg91IsggkejIAjmkJYW60Gh1D+IYiMtqO1F6ElSb3bZnTc2TUyjBWPT3f156EycTHYmbhMvku/p/d73/X7zmffebxhY17r+Z5LbyDG+7z+sqjuA7UAF6Iu834DLwI/GmIl6vX4eaP0nQJ7n3Qm8LCKHgYF/mVYD3heRsSAI5tcMyFq7ExjrAGQZmIgcCoLg69UCGWvtKHAkNT8FnBaRc81m81KhUJgCaDab/caYYeAx4Bng7kSOisg7QRC8Ts4x5gEZz/NOisjzixVVrxhj3ty4ceOHk5OTC3lvMjQ01D0/P78HeAvoT1gnnXN7s6AKWQWtte+JyIFFcpHPC4XCjnq9fmF6erqZBwMwOzur5XJ5l4jcCxSBOyLrwXK5vOH69esT7fLa7pDv+7tU9dPE1Jhz7hVAVwKJZHzfP6qq+6L4PHAJeCleoKpPh2H4xbLE9ERfX99dqvpBYur0KmEAfnDOHVLVz+IJERnbvHnzhhWBuru7XwW2RKHr6uravxoYETnunHsDaBWLxReBXyPLK5VKh9MFlhzZ8PBwaW5urs4/3fGCc+5kDkDR9/19qvpEVGsL8FASJgiCAyQusLV2D3ACbjVJGIYDQKMtUPS9+TIKa865+4CsC1yw1p4BHm9ntoOJX8JaWwUsgDFmpF6vn4nNJUcmIk/GY1X9OAcGa+3+LBjgQgYM3NqNT+Kg1WqNJM0lQKq6PRGezYIBEJFHsjxVrWXAxLnnEmHymcsu9dZ4UCqVfsoDypMxy3pliVqt1mJtVd2a9NKZm+J11Wp1Oq+oqn6f432Xl9vb2zsVj0VkU9JLAyUveW6rO+eO0v5YzzrnjuXl9vT0JGsvYUgDzcVg/f39feSr6ZwbEZGDwCnglIgcdM6NkNMMADMzM8nac0mvmFp7GegF6Orq2gZcXQGqEQTBODC+wrolunnz5rDI4mH8kvTSXXYxHrdarUc7eUgnEpFk7YtJL/0d+jYxfo7b+8VdSQZ4Ng5UdSJtkjC/An6Pwgc8z9u51jS+7z8FDEXhtVKp9E0mUBiGfwKLHSIio5VKpWetYCqVSo+qvh3HqjperVb/ygQCaDQaoyR2aWFhYddaAUW17o/Ca8C76TXpLmNqauqq53lHROQYcMMYMxlZxlq7W0Tu6QRCVa845z4CWsaYSVW9AXSr6mthGM6k12de2sHBweFGo/FHGIZ1AM/z9orI8U5gElD7wjA8EdUZKBaL5VqtNtlu7bIdipWVsFrFL5ilTtraWGt3q+q2TgBE5Of4yDrJW9e6bld/A00neZrniGI7AAAAAElFTkSuQmCC',
        },
        'dark': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAACn0lEQVRIie2UO2hTYRTHf+fmphG1bR6m14So4GOoKIhofYGg3Rx0cFDUpeguuji5OTnpUOoDR1HBUgdBVBRxEN+IVF3awTZqkpImbUW9TfIdhzxM0jRtnXumw/nO//c/3+NeWIw5QuZqSCQSy/D4usWYbSBOsaoJteStyf15Eo1Gf/2XwejERNDn5s8rcgpYPkvbFCrXfT654Pf7M/M2+DGW2Wcpt0AdQIGXAk9VGAFElNUK+4GdZYkgRzs6As/nNEilxg+oMoDQIvBMldOOE/zYaJBkcnwPFjdR1gAuqoccJ/RwVoNUamKDUngHtAp6ORwOnhUR0wieSEx1iJV7AmxEuY9wEJhEra2O4x8u91nVIsX0FuHcDoeDZ+YB3wR8FskfB7kLtCGmt+EOksn0LkReAJlpn2f9qvb28erGbDYbcF1zBGgVix5VOoFBNd7ulStbU/H4ZMjbkh8C/MYyXZEVK97U7kDkRCm7Vg+PxydDrms+IPQhXFSlU4QvZThALNaWRuUGgJgKq+aI9gKo0Xv1R+L15Q8jrAYqb16RG2X4v9CB4rHI3kYG6wGMcRu+mAq3YqJTMwbxMlhK1zUyWAKYSCTyu16Yc+1+lBFgGbAUZSTn2v31fYFA4GcpXVqu2VXrE0D7t29TQSBdLYzF2tLZbHZL6ZLx+aw7jtM248sdGxsLl5CVtWqDT8DulpZ8F/CgXlz6FVypr1eHEXu7KACfy7XqV/QIQIVjzSDNwtKiVpHHFWw5+f49s8Zj6xCAKeiWSCT0aSHwRGJ8s1i8B9T26NpQKBSv2UE0GvgKXAVsy7JOLnR6EekBbEX7ynCovQPUTJ/D4x02Nv0AyWTmFaJdTckqrx0nsKPg1UtWQUcp5K7VGDfTLsSgac9iNIu/KBT+Xctcx9cAAAAASUVORK5CYII=',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAADbUlEQVRIie2Wz2scZRjHP8+7+WFtXTfZzMzu2kPX9CD+PIkGg4p4kaZeBLHnXFpQEOk/UAQFQTEWD1oqSPEoSI3YW9GDYA8ixF8QgoLrZmaS3U0T0m66m/fxsDObSXa23RpBkH5Pz/PO8zyf932Ged6BO/q/SwYNXFzU0Wy2/qyKPCPCgygTUYVVVX4R1W9W3fHLD4nc+FfAtVot225zGpFTEMH6awWRDw3t9xzH2fjH4JWV+jGrnAOKieW2wKKK/AWgqocFjgJDiZiqoLOum7902+AgqJ1G5J1ujPC9wlyG7fm9p6nVatmWlRmj8rqij0fLVpE3Cu7Y3MDgMKy/qnA2ctdRec11cxdERPtttLPZ+lMIc8BjxB0QTnrO+Ee3BAdBbQqRb6NE327r88Vi/uebAaH7Wj4HRhX5RLAzIC7QssZOFycmriTjTdJR1QwiH0fQphFz7HahwJaoPW+NHge2gGFjzTlVzfQFh2HjBPBw1IozjpP7oR8sDMNDvu8f3As1wkuel/+uODFxReHNKPzRYLXxcl8wwqnIWt7cvPp+GtD3N9wwrH+pDK2LGdlQ5WIS6jjjX8Wx1zevvgsSAIhyMhUcBIEHTAEofFoul5tpYGNa5xVmOk1BFIyA3QsFKJfLTUQvRO50pbKe7wGrGXkiKkZGJPX7q1ardyu80OmO2m4uaKvVvJyWo2K+jlmjo+0ne8BgJ2OrOWIW0or0aAfe/zNrZ37qbkLl/h6wWO6N7cPZbCOtRqlUuibQOYGKQcUACFwqlUrX0nI871A9ti07jB2wyFZsVyqVu/odwNrhWYF5OqdUgXlrh2f7xQdBMNplqHYvkMR8lWrcscyBA0eAX9MKFQr3hMBx3/cPAniFwmY/aEcj5YRT7QGr8mM8x4zKdD/wzgZuBexIhOldjEjdVrtubgHwAcTyyiBFB5JwIrKqnjfWnYLJd2xR+SxynguC2tR+mWFYnwaejtxdl8yuydUesnNAM4KfZZ9SiK/E60a2P0g+S17e3JfP/xmsNN5G9QxQitd9f60s0p5kAKkOLRUKud8jt9jZgLzlOE41GddzLaqqCcO1F0EWPC+3tLzcOGIy+hudeTyItuy2PFAsjv0RBGuToI+4bu6iiNhk0NDerCjgiwEhN5Xn5ZaApbRnA/1l7qPVd/Tf629RmFPqkqIBwwAAAABJRU5ErkJggg==',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAAD60lEQVRYhe2WT2gcZRjGn2d2k1Rp485mZzbZsmJsBGvOOYkoKppDQIsHBVHbWIWK1EN7EUTwaBQPgjm0JcV40Ev9gyi22oJaL0JPknoouJLSZHdmN7OKxCbZncdDZncn251J1sSL5IFl93uf732/3zffvLMD7GpX/zOx2wRJRrlcfUDSYwLHAP8ugJnArgAsEPoZ4AXLSl0m6f8nQAsLC7cnEn2viDxOIL81eszD0PurN5en8/n83zsGVHSXJiBMbxmkAxiJY7ad/npbQJIMx6lOgTrRllYCdA7SpXpCV+s9PSUASKytZZM+R0U+DOkpgPYGLOIdO2O+HneMkUDrMN4siGdD4aKoNyuZ9Iej5GrcZq5dU19/yjsM8S1A2ZA1a1vmkSioZFRB1116D2QLhvi8r8eYTKVSXhxIQyMjWHNd3CmpAqIXgBlYzztO1QVwslNexytUKnmHQH0aCk3blvkqSW0FRpJRcqunCU0GocsQroJ4uTnH55ODg+YX7blGe8B13X2gPggxn9smDAT8aNvmMUCfNasamnYcZ++mQHUlXwMwFAxv9Pbg6PZgOJO1zDdI+n29iRcBLAZWzkfyeHuNDUc2J/Vm3KXrze4QX8hmzdkYgKRT9iYhPgqAgoYI3L8RJvVS+AZ2HO+woLPBsGhbZp5krSNQ0V2aoPBlUG3ets27SdYjYBJOeek8xEc6+h1gmptwvd8B7A8C49nswPmGb7RVebz5m/g4CgYAnLJ3NAoGwE+dYACAZA3iJ60xxsO+sXEyxkJbuRgFAwDw8WCkJ8zHPvyoS62pHAt77VfoQCup/kss0Da0llC49oGw195l6eBblmW5m9T9IdIx8H1cYjWdLjUHaq7ZESh8k8e2um2bp8EOx0pdtDPmmbjc+8K1uZGh/a+jivVHPEulvzIAnKiiJOuSxkNtD1Df2Zn0TFwzAEC5XM4AifCakUCFAAiJRO1gHFAAVQNwKvhsWb7P0eZZiL+Fvfab+kozCXiom0W6EWm0ahu6Evba2l7fNtmkZyR1/Yq7mSQZAp5uBnxeiASq1Va+AvAHABC4t1T2JnYayHGqTwAYCYbe8nL1m0igXC63DLHVIcJUoVDYs1MwhUJhD+i/HQqdGh4evhkJBABScgqhq3Tb3jsO7RTQei3eEwy9ei35bvucW94YBwf3OUWncoLgGQAr9DG3DirDdavPARrsDoNFy0p9RNKnjzkQKwD6CJ3M5frLt8yOKrO4WBn1e/Hn/oGB6wDgON4RQTPdwTQW4aRtm2cB4EalkjdW0T80NDDXaW7kO3VUwnbV2GCUttzWjSMTdLAbAIK/No6sm7xd7erf6h+hVJstKeMrrwAAAABJRU5ErkJggg==',
        },
        'active': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAABk0lEQVRIie2TP0scURTFz8RybdYFtZBNEVOJqRQMVsZCFOzFDyC2IeQ7WPgXks9gkcpOVkyIILggCBqsNU2EgCaRBP9EfjZn4OG+2dmdei8Ml/fOOfe8O/c9qRM5keQRgJKkSUkjkvq8fSHpUNJukiT/CjkDPcAacE12/AFWgHJbHQATkjZ9YiQdSPos6bs1VUlvJI1Z8kPSXJIke62cfAa49Qm/AK+acMeBM3NvgKm84i/dNsA68KwJtxc4AR6ALWt+Ay+aGdRM3AQyL0BQHOdu4JPX21mi1yZcAj0RvAwsAu+B06B4r/EKcOX90ZjBR4NLEawCnD+5Qadp8YC3bGwjZpC2PBbBFoz9DQzeRXjjxo7SvXCIg87HDe5BDUnpw7qO4N+cGwdt54fYcCO/6ByoRHhdxv/HDH4ZbBAaT4e8SMbLBfpd42cM3Dc4HRO3EsCsa3xN98IZ1JznixoE2p2Y+3Pg3t9Qu5WBYWvvgIEs0ge3uFrAYDXzDQSkEvAWqHpdJz/q5latLbVzqpYNOlE4HgEJFErafOu9rwAAAABJRU5ErkJggg==',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAACKElEQVRIie2WvWtTURjGnzcVWlo/BkWxIKRm082hUC1UnISguFbEpYOL4OIgOIgOKui/4F9RQUFEMggiFSqVdhIHadQqikNrrG1+DnnTvib33lzTgCB9IOS95zwf5xzenBtpG/87LC8R6Jd0UtKEpCOS9vnUF0nzkiqSnpnZak9WBuwGbgGf6Ywl4Aawa6uhZaDaYv4LmAee+GfBxyIWgdPdhl4F6sHsBTCZtBs/lfPAy8BfB678bejlYPAduAh07AfgBDDTcgKX8oaOBeEH4GhOXRmoue4B8MnrVWC0k7gPmHPBD+BYF6E14DgwGsZeA31ZBhfCEV3rELYTGEoILQfO9eA3mWX23ElVYCCFsx+YptF4dW+itlDnDgAffb6SFnqAzS6+k7G4adqx3hoa+PcCZ28S4WwwmkgxGQTWglETa8BgiuZU4G0srhA4pVDPpe24BfWmfwbnTagPJwXvCfW3JAczW5H0KGib+sc+l4SvSRkx+GeoExvLMSXpoRq7xOupDH5/qDdeIDvCYDXURUkLSS5mtiTpDDDkz8sZoZI0kpQRdzwb6vEOZjKz5RyhrV6zbbNAgcYVCfA0h2EuABX3XCTtvgfuh9Yf60HoePC7m0U8ROOOBpjpQfAr91oBhuNcbC6Z2Xu/tW5KGg4GI/rzd56Ft2b2zuuD/n3bzKppgmZIATgHlPy5yOZLIA9qQNG1JfcqZIamLKTr4Czk+pe5haPexr/Hb8omJxkzhlSvAAAAAElFTkSuQmCC',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAACfElEQVRYhe2Wz0tUURzFz1VTk4IUkVoIQavBtcsoKCrCqKgWLoqErD9AV9G2KHDRRiHIhRgUFrUPCypbFMwqBqEWoVlqP6RtVH5aeKFvd+bd997MuIk5MLw79/s953vufffHkxpo4D+Dy0sAmiTtlXRIUr+k3ZK6ffibpPeSXkt6LGnOObdeF6cVjHQAo8Ai2bEAjABb621mIKeRSsaO1sNIEzBWocAKMA6cAgpAl/8VgNPABLAacNaBG2y88qrN3AlEl4FhoDUDvw245M1bTFVlCrgZCD0COnMO6CpQAtYCrbG8Zk4GAuNA5h3pzUwa/gvgVqB5PKvYduCTIT6o0QzANd//0PR9BLZlEbxiSEvAjhrNTOLXDNAZDPZymmAr/+6Ocyn5LcBFYAa4D8wlmTGc8ya+DLTECgyY5AWgOZLbDMySjDIzZhBLJu+wjYcEG7zrnPsdmaALkg4kxF5KGq50bTjnfkm6Z7qOxAz1m/aTiBlJ2heJLabcYU8TapYZ2mPab1IM1QKrbWuWGeryTyR9SRF9Hok9S+GuVqhZDjbuG/wzerxHFvVsbDN47haT/zOWaI/4npioz7fbfsa3k7fxX94uUyf5TQBFkxhbtDUBOGjqvLKx8LUUTXv/ZhkKtItJSQLOGOfz5LjDsoKN6+WdqXMiltwBfDfJxzbBkP2SWAPa0wj2C3E+lZDPTDvw1uhfz0LqCWZpsI6GBoPZ6Q5zyraoc+4zMCLptqQfkkperEnSWUk7c/pYkTTtr5KS12yTNOqc+5pnNH1Ar/k/RPUYMjq9QF9S3cRDzDlXyuw+B5xzH2Lx1FPVYErSuqRCTg/zkqZzchpooGr8AW5AJOm8o3ilAAAAAElFTkSuQmCC',
        },
    },
    'latency_live': {
        'light': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAABfklEQVRIie2TsWobQRRFz9silQtVEbtvRISDiiBS2oWR3LgMDsh1UuQDkn/IBxhDigRSWbh3wHIghcEIJSQ/INzYYNDuKgEVAhEsLLKTQhMMsrxCYsEpdJuZN8O753GZgaX+d3mq+k1VW4AsZJB2GQTBM2ADqBhj1jIHiMibf/skSXYyBfi+/wTYAhIHyxbged5rxrl/tNb+BErGmKeZAIrFYg54CdgkSd4BDVgspqmA0Wj0ClgBTrrd7pnneYdwO6YgCLZVdaCq7/P5/MNpXgLgnmFl8tJa+zyO40a5XH7Q7/d/ATkRKYVheO4ATRHZvGP4r1EUVdNe0fc4jj8DtNvta+AYbmIqFAqPRaQK/GYc4Z8Ur9kyxtRU1arqD1e/dfV+Wl/qP5jQF8bTrvu+/8ha+8Kd1zMBhGF45SAiInvAKnAZRVEzEwBj509urbm1jvuImQCGw+ERMHSlFZGDWT1zAXq93gA4BbDWtjqdzkWmAGf8ARgAu/P2LnU/+gtqWXfoILpK8wAAAABJRU5ErkJggg==',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAACH0lEQVRIie2UvWsUQRjGf+/eEhQJZxG4jxm9BbE6QVSIAa2CrWBOQaysJIjBK/JfiGijgoWFEWIhUUHRSlCwiEWsPNKIcLB7HwTh7OTYnbFwF5cj8Y5LVpt7mpl52ff5zfswLEw00f+W1npVKbWltZ7dCz9nROhpa+0VYMYYU/9nYGPMzWQvIuc9z9uXObhSqZRE5FKqNB2G4Xzm4CiKFoGp5AhgjFnIFFytVqeMMYsAIrIBvIn3FwA3M3Cv17ssIsX4eBd4Hu9ntNZnMwMDSwDW2k4+n38GvAJCAGNMbfBjz/MOaq3r5XL5xDBwEpdbLpfngPRrPQzMAjiO87DRaPSB70qp98A5EVkA6oBNGvr9/i0RuSYiRim1aq19ApiU589Wq7UOhAKgtb5nrb2xw+X6rut6zWazDaCUug48ABCROd/3PwEUCoUDruu2gem/TSoi933fXxr6qq21KwkUwHXdl6kpaqn6xRR0nVQS214g6dsmakhFky4qpT4CZ4CvQRAcjWvvgHmgWywWD3W7Xc8YU9nJTxhDSqll4DaAiByPouiH4zjf+P1Y7wRBsDzMY6Rf5qCiKFrjT5Q1Ebma8loZxWOsiQGUUhvASeALsB84AnwOguDUKP1jTRzrRbwei6GIyONRm8cGG2PWBkr9MAyfZg5ut9ubwGaq9LrT6WxlDo6VxI21duSYdw3O5XKPAB/4UCqV3u7Ga6KJ9ky/AIvGsVja7d8XAAAAAElFTkSuQmCC',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAACjUlEQVRYhe2Wv2sUQRTHv2+ThZzKHRYxuPPuGoOKCLaihRDBEPyB0fJQYx2sxb/ARrASbILRKFhFRWMhJo2orUWKmGyj7qxE0uSCQvBunkVmyXBuyK1u7pr7Njvv7XtvPrtvZ2aBrrrqamfVkzUhCILbxWLxVbFY3LW2tjaXNxBlCWbmQRH5DMAD0KjX68Hy8vKPPIG8LMEiMu7k9Pi+fzFPmExA/f39ewBcd33GmEsdA/J9fwxAyfUR0VClUtnbCSAionHH/m2vfqPRONd2oHK5fAbAYUtWA3AnuUdEubatJSBjzI1kLCITRPTAsYcHBgZ2tw2ImQcBjCTzA7gfRdESgHnrK/T29o6kJu8EUNNSn9FaLwIAEU0nMVu1jZlHmXlVKfWBmU+1ApRsjMTMVQAqBegW7OoSkeE4jt8AgFLqGIBPFqjW19e3LwzDdbe2UmoBwEEH/AWAjykcOoqiJwCEbPErAB5tA7+gtT6CjbbB5oUADtjJzkZR9Dq5FwTBCSJ6v01NV1e11lMt70NEdNeFsb5px7zclDLmjL8CMC3Nk1yZuWqMOZoW5HnefPJKXb9S6jg2W7Citd4PoM7MBRH5js1WnySinwAuiMhfK9Ktn+lwTXsgpdQXAGVrn9ZazzFzVUQeW9+S1vpQ88NspUyHa4rEfqgbdESjACAi15yYyVZh8gCCiEw749EgCMoAhqzLiMhUlnr/DaS1fgdgxZqKiO5h88dvNo7jb20FAlB32wbgfDIgosmsxfIAgog8a/YRUc0Y87wjQIVC4S2AVdcnIk/jOP7VEaAwDNdFZKYJ6OG/1MoFCAA8z3N37cU4jtPOrO3r5MSDUqn0EsAsEdVE5CYy7D1dddVVO/UH0U3j3msgHFYAAAAASUVORK5CYII=',
        },
        'dark': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAABm0lEQVRIie2TMWsUQRiGn3c3WAm5hN29EDgCSgoRSy3EpEkpBpI6KfID9D/4A0SwULAy2Ct4CikECScY7EMaA6mOnd273cAZ1MPbz+IOUYkbFhbS5K3mm3fe75kZZuBC5y2VmWbmJWneAYoonFmWZFUBU2Wm6+V3BbcB4n7/JvC5KsArM4Ue/F5YeOtVm5cCur3eNcxWgGI8Y/UCfPPuA0I8B2LQYhxnN2oB5HnewNgEbKTiCUYbQL4qn+JUwI+fxRZwGfR+PggOJHsFgP19TXGa3XNJNnBp9jSOB9FpvQTgkqwD3PnXNLE6F862980uBWnugIbwF6No+ssktwss/2fzH5vR7FLZK/rUDGbeAVyXhoi340OM1gGcO74KLAEngjYwKul1tpzL11ySmUuzvXHdf+iSzFzSf1GWK/0Hf2o4/LoDnGDc6nbzBaQNgELedi2AVqv1DbQDyJ8qHgNXgKO5oLFbCwAA2evJYA0As21JRW0Aj9Eb4PukNPBfnp2poDAMB8CHSdlpNhuHtQIATDwDBiY9qpq90PnoFx6plNyGhZXfAAAAAElFTkSuQmCC',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAACOklEQVRIie2Uv2sUURDHP/NyicpxmIS7SyKowR+VIqiggVhIsBXMKYiVlQQxmP/DxkYFSyPGQmIExU5IkSIWsVJSKFxhcft2L7vRSja3OxbZyx3hzB3Rjc19YXk7b2fmMzO8t9BVV/9b1vpz1vW9SrV64V/kM504OU71IsItIG8iM7NnYBFzv2FwtVwu708d7HneCHCjaSuXzR6cSB0cx2YKoS8xo81FJlMFf1HtQ2QqMVcE3gMoek1VM6mB8+76TWAYAOEhyOv6J6e6fik1MDCdrE41P/AqDHveAjUAUS1tdw6CoN9xgxnXDc62A2cAVDXjecGYarx1WkXkiKKbd1b16SmREFizXrCI6hWBSVWdERGtx4ShPhDhjkJsPX+OWJ+Dxo2c5lehMLAsIjUBsK7/CLjXsjQlNCYaLRQKFQBr/bsITwA0jseGh/MfARzHyYrpqwC5Ns0+HioOTrc91SIyW4cCGBO9AWIA6TGlht++603QZUDZQQKtR72ZrDGa5n3r+kvAOPBtqDh4EsDatQ+ITIDYYqH/sOf9HFXdOPqnfJmkqxqw1K77RgZdQGUcOGGtfyaK5AeilwFQXojIBvA1eVqqo1/mdsU1M8/WKLXU08vtplyzHdW+GzCAdf0V4BzwGTgAHAc+DRUHz3cSv6uOARRZSF5PJ1AEedZp/K7BsYnmt1USRlHmZergQ/n8qsDq1obou5GRnJc6OFF93KiYjsf817J2/Zh1/e+O6y+qau+egbvqaif9Bgh93VXWOQcYAAAAAElFTkSuQmCC',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAACpElEQVRYhe2VzWsTURTFz5kYpVRpkzaTmNKVjYoIbkUXQgVL8QOry6LWdXEt/gHiRnAluBGrVXBVLVIXYt2IunXRRW3daChmXprJB/gRmuS6yGRm0rQ6Y8foImeTeTf3nfvLvXnzgI466ug/U0aZNwxlflfKvP43/OknWanikKD6AYAGoCq1cDKR2KWCBNL8pVcmXXtCDK2dDRLGF5BSaqeAl5uCgnP/DAjYNgGgZ11wuFAoRALk8QYkIqwBk67QmvUZLpflVNuBlDJPENhvLUsC3HQcJNCxeesQecW1uKshdM9ZYySTyXS3DUip4hCB0UZ5Tavc0fWeZQALVqxL08Kjm2wPHsh91AnMxWKxpTqazDQyRLjh2AwjP2Yos2go8202mz/mBYh1Q6FS+XESA+sTBLiGxukSGYnH+15YxQ6B8t5KK5WKET2VYtmBFKpsYRGQva5qsxS8a6khWNH1yCOSQsv8AigPfkUuwGI8FjlAUuwOKPMjgD3WLzup69Hn9ndG7gjIN79tiV2AF+PxyLTn9xCJW26YehD22Grg+XU7Jpxi+Ayg5qkO4IwMxMENswQLjZa6w5lM7jA1Nkawqsciu0lW0ul01/Yd3V/gjPoooH2FhjMQaT2RLn9fl2uLjwiNbP4TgUEAEE07nujvfWVkzXEIHlpZy3osuq+lu5vI5+XaLKvIrB2o1cbqDLxkQ4NTXmG2DFSvTft/RGBsJZcbBGS4gVgNybQfvy0DJfp7XwNYtZYD4SpvAwhZiPMDfX3ptgKRrBC0xybAafuZMuXXb8tAdSh5skG4VFv78dSvVyBAhULkJYBiU1DwOJlMfvsnQKkUyxDMNUfl/p94BQJkAcw4z1zS9WjLneVFgQGt6tFnoMwDKEFw1c+7p6OOOmqnfgKtmvi/gPGlOQAAAABJRU5ErkJggg==',
        },
        'active': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAABFElEQVRIie2UvUoDQRSFz6QQLAQrWwttUtjaKdjZrGBa3yHgExhfIkIqH0Bs/Cnsli20tRXE0lKIihDIfhYecAnJivFKmpxm5s7dOd/cuctIc81aqS4JNCQVkkpJ2yklQulAxrc2p/Fo/JBvV+ataQATBTSBEhi6godoQNfGJ8Cz5xtR5svAqytoAj0DjqIAhza8cbzr+H7ku8wH6QIrdYYF45U5vwC8eG29si+fsA+gkOr/oltJV5KUUhpIuvR6y+ZrkrYkvUu6kDT81TWNqXLfJ7tzfOz49E/GFcAi8ObGrwKPBuyEAAw5s+m5xye+npMwwMFIIzth5gYsAR82L93oWAHXBuTh5gZkQB/Y+xfAXOH6BLlpTclTkbB8AAAAAElFTkSuQmCC',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAABj0lEQVRIie2Tv0vVURiHnxM3CUIQwsGhFMVBiKAlg5YIpzZ1FMlFxMnB/yJwCZfGFN2kaA8cGhRyCprcGi4hLU5q6uPyXu4Jb3r0cr8t91nOe3687+e8H86BLl3+N+qmeqA+q1J03CYbVQpvZMKH6r0qRAfUY//mdbt17xScWQB6Ij6LcbJd4StRe9R6dPlN/RzxgVrrpPBsZu+MOpfNX3ZSeDdE6tH9A/VPrL1rcb5PXVKfXle7Fgk14DmQv9ZHQOPPvk8pnQC/1W1gAphUl1JKZjlvgXngXN0E1oHzbP8I2EkpnTZuuuq/OVYHsq4Ws73xbP1+fLXrWIWyV72WUqpn809ZF1PZ+jTQG/EOkDtxiRS3bWX1ZWua3X0FXgD7KaXRWPsCvAJ+AQ+BIWCwpF4x6nJm3RN1UD2L+UpJjRKrW7FF08op4E1Wa+2WNctQ96LD7+p+xHul+bftGOBjjI+BkYg/tFGvDHWsxbfr77hwiP/IhLduktuO1dC0G6qwuYE6rP5Ut9W7lQl36XIVF5ieoD11wHP8AAAAAElFTkSuQmCC',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAAB0UlEQVRYhe2Wv0scQRTHvy9KgjEgaYQgdomVYBu0CNoICkG9VMqR9PoP+AckdloLKdLYKoqCiLVpAxH8gdWRYKGFBpKQoH4sbvSe66q35LxJsd9mZ9/Mm/eZeftmR8qVK9d/JmAK+A28j80i4DlwSlknQGutYzzIOH7c+TRIGqotTgYBT4Ajrmo1JtAE1/UXeBoDxoCtBMiFijGA+h3AMfDBvS/EAFp2ANPAC/f+C2iuJ4wv9TOgI9i/Oqg3tYpXTdn7Ul8xs93QnndjRtIcgeGQ4g3gVTVAFhxN0piktpQxk5JaQrvfzNaCT5ekL8H+Q1Krmf1xMCZpW1KHm2tR0ueUGN8lzZkZF87FlJJOaisE8Tuw5/oHEn3dVczpVZSyndQzlyuoyKetkOh759olSWfVBEmmrPOGcZvyWxoEvFQlBYeSnpnZCdAkaV+VVPdI+inptaS0ikydP7MoH5glt+19wT7mbLvJVN+mrD/XKworWnSm4fB862yf/nnlWQT0ut34BrRTvppA+fxqrxtMAGoEDhzUkmuv1RXGQX28oZRHYwENpsAcA49jAT3i+uVtNgqMg5pLAHXHBio4mJ0sZ899AT0E1sO3E+/ynytXrjt0Dj/aXK4uTkPqAAAAAElFTkSuQmCC',
        },
    },
    'latency_tools': {
        'light': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAAAtElEQVRIid2UwQkCQQxF/7eNDGgT3rQBu/BkBxZhB2s1gp5sQtFJHfGisAxkIMug677bJPCHn3wC/Dv0GimljZkdAci7dCe5zTmfIh/MvIaZdT1xAFiY2SEiDhQOROQMYBUVKbio6vrzcB20wt2BiFwBLIvyQ1XnkQ9cByT3JG+90pPkLiL+FdwR1YhEeNCSIxGecExrRCI8yMGoIlwdUYuLWh1Ri4s68Zi2uKhVB6OK4894AYMsVxwE77V4AAAAAElFTkSuQmCC',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAACVElEQVRIie2VzWsTQRjGnyc2pOJHIvS0O0tjFItU6UVR9CBePHoxKpQeBVEv/gMi3j2Klx6VemjBP0AQBK0IovjRQ6v20MyuFw9tUrS4MY8Ht5LEZLNJExD0uczOPO/Mb94Z5l3gXxM7BeTz+Vy1Wr0AYA8ASKqSfGqtfTEwcAR9JWlvkyWSl621072CU3FmGIYXW0ABgJJu9AoFmjIuFArZjY2NZyTHt7JosyQtDA8Pn1xeXl7bHGvIOAxDr99QACA5Hoah1zDWHOS6blHSkWjCcQCnIutJ1I4CyEv6AeB2QvBL3/fnYsH1MsYck/S8Tdwj3/fPJAG30rY4s1wu+9ls9jOACQDZaPg7gMeSLlUqlbX2s/9SdSwgSWSMmQJQlLQDACRVSM4032tfwcaYKUn32tjn28GHmgccx/EAjCUFS7oateskl6LvAyR3ArjuOM4qgMUgCEr18xoydhzHI7kIYHtScEJ9kzRWD48tmYPUH3fc7VGTvAXgRJujnpd0E52Ouhe5rlsEMNvKkzQZBMGDlhveKhgAjDHnJE0C2A0AJNcBzFprZ/qxfl+VKGPXdU+TvCPpYDSnBuCtpGtBEMwPBDwyMrIrk8msAMi1sL9IGg2C4OuWwa7rFknu2+xLGgVwBQBIvpG0SjInaSIKuUtyJQ4i6VPsb9HzvEO1Wu1dt7tPolQqdbhUKr3/3a830+l0SdJCv6GSFtLpdHfv2BizX9KHmJAx3/eXut1Mx5Jprf0I4H4be64XKJC8gNBxnLMkj0oaIlkl+dpa+xC/ntZ/ddRPAJYBeS3nCdwAAAAASUVORK5CYII=',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAACZklEQVRYhe2WPWhTURSAv3MDCsU0Flyad0OtZBKdFHSqrUPronQVf7bq6OTsoOAiuDlUQajUuYODf2gLBTs4iW5CG7nvGUhBSB2qJPc4NMXYNs+XX6z0m17uOXnn4557Dw/22GVIkqR8Pr9/fX39tqpeBgbrQl9E5LFz7g5Q6YRQKklSX1/fXVW9CaS3hDLAaCaT0XK5PN8JIZMkqbYzcfGpTsjADi2z1uaBl6o63KkiOxYWWQbGnXOf69e37ZD3frLbMgCqOuy9n9y6vk3IGDNXs+8qIrJsjJnbtp7kz0EQOCCISQnDMLStytWT6FCLyKO4uKo+6YQMJLz25XJ5MZ1OexE5wsZV3yRS1QcDAwO3SqVStVNS/xSJzlCz5HK5iWq1etwYkwJQ1YqIvHfOLfRcyFr7VFUvNghPh2F4vWdC2Wx2XERexBYUGY3bqZ2ExFp7ifhr3ogxVZ2oPReAldrzYWCoJvQcmAdC59wsoLFCQRBcAWZakGmFq2EY/jEyEs2hXtKwZd77Y02/TOQ0cKb2s75lQ2y0DWBBVZeMMR8TtawdgiAYA97E5ajqeBRFrxrFE03qpKytra309/cPAicbyMxGUXQv7h3dGowj3vsRVT0AICLfVXUpiqLX3ajXVZraIWvtKVW9D5wA9tWWfwDvROSGc+5Dz4RyuVzWe/8JONggZbVarR4tFouldoQSzyHv/fkYGYBDxpgL7cjA3kf+b/6vj3wAa+2Uqk7/pcg159zDVgQ3SXyoK5XKHLAak/ItlUo9a0emKaFisVgSkbPAWzZmzyY/gUUROVcoFL62K7THruMX8UH2doWhTOgAAAAASUVORK5CYII=',
        },
        'dark': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAAAvElEQVRIiWNgGOqAEZfEq1fvvP4zMMxiYGCQhgo9+MfImCgpKniAFAuYcEn8Z2CYiWQ4AwMDgwLzf4YuUgxnYEDzwctX7w4zMDDYkGoIGjgiLiZkC+Pg9AG1AJ44eH/qP8N/UxTB/wyPxMWF5EmxAKcP/jIylDEwMNxHmM3wmJHxfzophtMF4AwifICUJExWJJOShIdxMsUHSEnCZPlgUCVhvEFEjRIVbxBRo0Qd5smUGiUqXh8MquQ4YAAA/cdaEG8yvvoAAAAASUVORK5CYII=',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAACZklEQVRIieWVS2sTURSAv3MbA2K1adJMh0qporRIFTeKogtx49KNVaF0Kfja+AdE3LsUNy6VumjBH9CVoJWCKD66aNVNi5hJmkyLRWU6M8dFU0niNJmkKS767e49d8537mPuhZ2GNBrgum7K8/WKKN0AKuproC9tu2dm28Su66a8NX0LHKwJqYpet7OZx62KTb2g5+vVCCmAoHK3Vel6ggpKpVLXWsArlOGtJI2wzO7q4Gw6nV7Z6KqasefR33YpgDLsefRX11JDPl8cUeQEgIicVtVz5YEvQkCQAdADoAHIgzheQd9YVmayrriSXG7plBjzOnqcTPVa3RfiiKOoe7hsu2cG4QbKwt9OxQOZChJ6rVXpf6XhBRIHxymNYRgB2QOAhj8Exmv3ta1ixymNITyJTq6XN5Mnaju+FYv9CV+H4qv1FgiqumqMmQdQ1UGgU5E7jrO07Cdkbn8ms1hdVK00kDlgd3xxLH75HTpUKa97qreTf/a46aUWcx84A6yKyDxAGIaDItIJTKPhvYZL3Qrlm24iKqbCqJ1NP4usd6tiAMcpXkIYRcy+snJVYcLOpsfbkb+txJrx94J73oT6EOFI+ZsQ+IDq7d7ezPS2iAuFwt5QOxaAVER4KfB/D/T19f3csjifL46AHNpoqzKAcHO9wXsVlkVJIRwvJ3gEFY9IJPq17rOYy5WOiuFjs9XHQUOO2Xb600a76gJJJllEmG27VZhNJmnuP87nVw4rwefN4kaCoWw2O99sLQ2vTMvq+oLwNDoqk61IIebvpKriOMsXjdGTCgkBX1XfWVb6uYiErYh3Hn8Aoz7jhwChCLwAAAAASUVORK5CYII=',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAACYElEQVRYhe2WPUwTYRjHf88VcRGlhetZNQKJkx+Tg04oDuCicTV+DCbo6ORkYjQ6uTiLJiYSnBkc/IpIYiKDk9EJE8CIcNdCNXGBlPs7tBIa6OVKSyOG33S95997fnne994cbLHJsDih8XFt39mav4u4CGSWC+Ib6Ek6nbpnZoWGCfnZ+fuIGxUD0h3Pa7tdDyEnVqo4mcqY9ddDBtaYUBD8OiCWXgFd9WpSgQkj0ZtO7/q68uaqCUmFcw2QAegq9SpjlZBZ0zAw0QChiVKv8v5x/ukH89+BvRGRaS+d2rdes5XE3NR6HFmGwXrIQMwJSWoKcj9vEuoKxv4VpR/AYM5N3jpktlgvqX+KWBOqFt+f6wOOmFkCQKaCg/PRdZOjDRfy/flnGOfXLIoBz0tda5iQ78/1YvYyKuOYnYya1CohSRYE+Qtmka/5mkjqwayveM0UxmSxQKcZHaWGL4B3EtPpdHLIzBQp5Pv5S5ieViuzLmSXPS9ZdmTEO4caSMUlwzhc9cPMjks6UfyhKckmi02sA9RZyoxKGkN8jrVktTCTzfc40tvIkNTreW2vK5XrumQZNzmCeFhZhqEoGdiggzEI8t0hdBvaUfSw36alMc9rf7MR/TaUqiY0O5s7Zo7zAHEUo7l0e8Hgg8R1z0t9aphQNpvdEyrxBWitEMmFS9sOZjIt2VqEYm/qkMSZCBmAdkssnq1FBrY+8sv4jz7yAWazc/0mG4jKyHR1t9v2aD12f4m9qVVoHgZyEZF8gvB5LTJVCWUyLVmFnDIYARaWC2IReB864WnXdWdqFdpi0/EHT6PfphsnfAUAAAAASUVORK5CYII=',
        },
        'active': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAAAZElEQVRIie2SywmAMBAFg5XlGGw7txRh8NPHeNOAISC7aFgyBcx7h3HOLMAMHNxkwGsO7DxJUmmsSN8SS+ckeiQBSJV3q+aAB5ZCvgFBbeBX+k3YVqYtRsIX/ebYEBrLdOT4CScUtOih0pnzhwAAAABJRU5ErkJggg==',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAABsElEQVRIieWWPWsVQRRAzxUbP/ME23yAISkSsFGUNOIfSOMiRGyNYJV/EKz1XwhpItinE0QQhCDkFRES0JQWBgyIkORYOMb1Zd/uvs3bNDnNzgw798zO3LksnDWi6gW1AzwErqWhfeBdRHxobVVqR932OIfqkzbFTwukf/k6TNGIulEia8qGOpJ3netxjwIzQ/uSf8yk2EccSy41A26l7l3gXmq/Tc9xYAI4AF7WFH+MiNe1l6neSYlUxFrtQE1QF9UvOeEvdU0da1XcFpUFpA7qYyADLqWhH8DKQOfaRFpyjbJ+884XBBoFpgdwP0vPPeBzak8Bl4EldRfYjIid/KT/tjpJN4ELA4jr8BOYzst7C8ipUVRABt3q58AcxVv9HlimYKtPjJqVJNdCv3nDuk4PgEfA1TS0B6xGxMow4p8+6n21m6vbB+q6Otem9Ir6vc8ZflMvNolbVEAy4EZuaBzopPYnYDf1bwLXgRdW/41slZZPdbYkQ0/KbN7VW0B2gG7F6pvQTbHro05WfMlUCws9kr/qI11tGrNWAVEDmAdu8ych94F14E1EHDaVny1+A18+M5El6SIVAAAAAElFTkSuQmCC',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAABp0lEQVRYhe2Wv04UURhHz9UNNhYWbKEWSEIplEi1hAKpMPaGknegg9LwBJLQ8afeByAYQmIoKKlXSVgaE20sJDE5FjPE3XWcGXZ3blayp7qZydzfmfvd+ebCmPuI+kjdVq/t5lLdUmuxhbbNZyu2UO/K9NKuMnxGbRUIDIOWOtOb/yDD6S0wXdkb/2E6zeoiS6gJfK5cJ8lo9vWkelWw/FfDssxaoSx2C+7vDSpyJ9SaumnSdzppq+/ViahCMQlVTKquALPAw/TSL+A8hHBSRV6RzGHO5t+JLfO6RENczJvjr5+iGoB3wPM+nJY6xpfAl3T8AphKxxvqAtAGDkII5s6orpV4y2Gx1ptftg9FI+scsw8IvOxjvgXgdo90lmyKpGwAJ8AZcAEc9JFRHnWpRJmWK5XIkPqQI7Nf9HxVjbEBNIDH6aUfwFkI4aiKvNFBfaV+Um86yvBT/ajOxZZ5pn7P2R9f1fqgOXfpQ6vAk5z7k8CbwXQycHzI7+b/POQDqOslyrA+PN9iobrJl/QvvqlPowmlUrPqsUnvueVGPVXno8qMGRV+A/izV4qRKKWqAAAAAElFTkSuQmCC',
        },
    },
    'help': {
        'light': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAADI0lEQVRIidWVz2sdVRTHP2de3kswfRhwkTD3UjSKCCMu+sSFoJsQQUF8NeAf4C+otVApVRS3gia4VtAuBRdNSbooQltBEMEfz0XbuNCKts4dEwgSmUjea+bNcdE75SbvpdFlDwzcOd9zzvd8D3PPwJ1ucjswSZLGxsZGW0TaqnoIsB5KReRHVV2amJhYWllZufG/Cay1c6o6D0zv0+SvIvJmmqZnhoHRMJ+1dkFVT/vil0XkeL/fT4qiOFAUxYFarfawiBwHrgD3q+qitXZ+j3oDnS8YY9QY0zPGHNknKYrj+DUfq9baD3YH7BiRH8tp4AbwtHPuywqbnp6+u9vtPhlFUR34Nk1TV2HGmBngHNBQ1cNZli0NjChJkoafOar6RljcGPNCr9e7JiJnVXVRVX8zxrxb4c65i6p6AkBEFlqtVr3CatWh0WjMAa8Cl7MsewVQr+oR4AvgLuBr4BLwIDDTbDZ/zvP8CkCe551mszknIg9tbm5eyvP8px0KgMO+g0+BfuVU1VmgDnzknHvCOfcscBQgiqIjQX5fRE75nHblHAkCDgEURXEh8FEUxcf1en11e3v71lxV9SsRQVUPhrFRFJ0vyxIReXQYQQzQ7/evh0lra2v/AJ9V78aYe4BPvNpvwtitra1ro6OjAGYYwb42OTl5H3CBm/fjelmWb4V4WZbiictbqgI8A6jVajtkhzYyMvK5L/69iDyeZdkfIT4+Pn4QQFX/HCBQ1R98kdlhxf1oHgP+7vV6M+E9CBQ85Y/fDRCIyLInepng863MOfcX8KGqvrO+vp4P6aGmqi/689kBNEmShjHmqjFG4zg+OkzF7cxae8yvmF/Ci7Z7VTyvqovcXBXPOOcuhri/veqce2+XP1wV7SzLlitsxyJL0/SMiCwADeBcHMevs3Nc6p/KatbaY1VxEZkPiw8oqEitte+r6kkAVV0RkVNRFJ3vdru/A4yNjd1bluWsqr4kIgmAiMynafo2UIbF9vzhxHHc9moe2CvG21VVPRlu0P9EANBqteqrq6vPqWpbRFoEv0xV7YjI0tTU1HKn09nep4k72P4F25dWcMBRrK4AAAAASUVORK5CYII=',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAAEHklEQVRIie2Wz2sdVRTHP2duklbhvbQWNZmZF19rI+KvqrUVLLoQFcVCiuhG0Z24cWM07dJdsf7oH6AudKFgqaaJDYLuRKvUpmL9AUIbNW/utEoUmkEweZk5Lt6d52TyXppSd/pd3Tnn3PM9d+ac7x34r0EuMr4nCIIdIrJFVa8CEJHfVHXGWvsVsPSvEgdBcCswCjwMXNEl7A9gCnjNWvvNJREPDAxcaYw5CDwOeGspEsiAd1R1NI7juYsmrtVqN6nqpKpudqZFYFxVJ4Cv0zRtABhjap7n3a6qI8AeoM/Fz3ieN9JoNL5bM3EYhjcDn6lq1ZkOG2P2zc7OzqxyUmq12rVZlr0MPOJM540xu2ZnZ7+/ILF7vceBOpACe621B1cjLMP3/RdE5ACtzzMD7LTW/l6MMeVN1Wr1dRG5xz2uIPV9//JKpfJ0f3//c9Vq9clKpTK8YcOG0/Pz80kekyTJsUql8peI3A9sBK5OkuRI1xO77p12lR621j5W8l8HfARsKdWbqOoTcRx/WDSGYTiuqnuATERui6LoVO4rd+qosy0aY/YVHfV6fT1wtEAaAT+6dUVE3nOFFTFGqyk9VR0tOorEPbTmFGC83EhLS0sPAcPucb+19hpr7fWum1PgMuD54p4oik4DE+5xt+NYTuz7/k6cOLiRWQYROQWcBb6w1r5Ia16J43hSVT9x+3Z12Jfn2hQEwR3FU7Yq8LzNqpqvT5QTNBqNM4BftgOeiGx0JGnZmWXZCZFWK6lqHfhyGXGWZYN5wMLCwtkOBCtQr9fXLy4uvgnc6UxHyzHNZtP29bU0RUTahbeJRUTztapeUMODINjUbDanRCQnPbWwsPBSOS5N045S2zaKSPuUfX19nV5pEaKqH+BOqqqfAvfOzc0l5cB169YF+VpV7QriLMt+bhs9b/tqrLVa7e5cZFT13cHBwfvKypTDGNPOJSK/rCCO4/g4rasNNyJdkWXZQKHIvdPT081usYVcc+7OXk5M6xKfcus9Q0NDZXVqo7e3dxLYLyLPRFFku8WFYTgM5MRTtOYdWCmZ24CTrqD3rbWPdku6FoRheCQXGCeZ3+a+ZZdEkiS/VqvVrcA24IZKpfJnkiTHuiR9sFqtbp2fnz/dxb9PVZ8FEJG3oih6o+hf0epOU2fchgO+7491Sqyqh1T1UCef7/t7VXW/ezxDS7OXoeO8Dg0N3Zim6edAvyMZN8aMOfXKkz8AEMfxx4VTDgOvFBrqfJqmd507d+6HNREXyCf55zZaBCZEZCJN05NLS0sNgJ6enpoxZrsjGwF685MaY0Y6/X2sSgwtdQJeBZ7i4n723gbGus32BYlzhGF4i/v2u4FNXcLmgCkRea3YvZdEXIAJgmCHqm4WkQEAVT3red5PURSdoDCn/6OMvwFiK7Dy38VWpgAAAABJRU5ErkJggg==',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAAFJklEQVRYhe1Yb4hUVRT/nTczO7vl7sK47qz3vqnBwKKgwBVC7KNfsnQNaSnS0BD6UGEmVFZoVoYY+SeEPkgEka1Ksa4hfgiKoD+QRBGSBFnTet/bmVYk5uVs4/Pd0wffzNx5O8+ZrD4Unk/33PM79/7uOfeee98Drsl/TOgqfBK5XG6p1nqEmRcTURaAHdrOMvOvRHRSaz0xNTX1BYDgXyFk23YPgI3MvBnAQIdu00T0OoA3lFIz/xghKeUogN0AZIdEoqIAbHIc5/2/S8gSQuwgomcj/ZPMPJFIJI4HQVAIgkABQCKRsBOJRJ6Z72XmEQA5w4cB7HQc5wUA+moIWVLKQwDuN/oUM291XfcdtN8bCSHEOiLajubIHnEc58E4Uom40YQQrxLRozWdmcd9319WKpW+wuXVthP2PO+bdDp9IJVK3QrglrD/tr6+vpTneR93TEhKOUpE+4yuva7rbqhUKtVW+OHh4RQRdXue50dtlUrlYrlcPtLb25shojvD7rv6+/tPlcvl01H8rJTZtt3DzD8gzD8RHVVKrcbsEFthSh4DcEe4uAKAsVQqtatQKPwWxUspxwGsDHWHmRe6rltpArVY8EY0NqOqVqtro2Ty+Xy3lPJDInoLwCI0Ip0HsMX3/W+llAsj4+pqtboGgBvqEsDj0cmjhBLM/FRNYeat09PTv0edfN/fC2C50RUAMNN5I4Bj+Xy+2/Q7d+6cR0TbajoRbUZk2zQRyuVySwHMC9XJ8DQ1iRDiBgAbjEF3zczMZBzHuZ6ZHwBwITTd7Pv++qi/UuptXK5LADAohFgSS0hrvarWZuajaHG0tdZzjFWdUEo9c/78+TKAwHXdw0T0vDHGSNQfQEBEE3UCltWEiaZskQE80WIwFIvF74loDYDdlmVtaAH5xBgj18IOAMcN0sOmIRkBCqP9Y8xgUEodBHCwlY2ZR432VCuM1vonovoBN+ecFaH5tUa1Wi3GEYoRS0r5MoB6yojoUCug7/uOoTbdj9EIdVKBZ4kQ4jrLst6L7JmPwg08S4IgqAeCiJpKSjRC9RCn0+n56EyIiMYiZE5cunTpPsTcd93d3fU0MbNr2qKETONNnbARQqxEo/qCiF5zHGdFqVS6EOdjWdYCQ23aZ1FCX9caWuvl6ECIaLnR3q+UehptXgLMfI/hczKWkNZ6wgCuwhVeAwaubKhtH2C4vG/rEdVaH4slFL6Bp0M1J4RY1270arW6HcBzAB5WSn3aDm/b9no03uAl13W/NO3RCHB/f78FYBkAENFwOp0+UKlULsZNUKlULnqe95nned+1I5PJZPqSyeQHAHoBgJm3e573uYmZddsnk8l9ACZDVabT6cMtiDfJ4OBgNpvNDrbhY/X09LyLRq37paura/8sULSjUCj8AWAzGjXpbiHEnlZYAMhms4OpVOpMMpk8MzQ0NK8VBoBl2/Y+ACtCnYloUzjXlQkBQPh1sLOmE9ETUsrxgYGB3pgJYwtqJpPpk1JOMLP59nlFKTXeCt/ukT8GYNToc4loW1iB60c7m80OEhEXi8VpA5u0bfsRZn4RxpVERGNKqYfiFtH2Myi8n7ZEsGeJ6BiA41rrn33fVwCQSqVsy7IWhHVmJRqnCSGBHY7jbI0j0wkhAIBt26uZeQ+av7P+ikwS0ZNxaTKlbeEDgHK5fHru3LlvBkHgEdHtAOZ0SKTEzC91dXWtnZycPNWJw1X9bBBCLLEsa4SZFwMYQiM1CkCx9rMhLHqxX6nX5H8hfwKFGfqfbzY0hQAAAABJRU5ErkJggg==',
        },
        'dark': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAADIklEQVRIid2VzYscVRTFf7dqulcR7enpDxwRHd2NCEZxIehmGCELsYOYhUt1ky/IwkTU/AE6o24VNEtRYQwzLrJJEAQR1ChInJUJaGy16nXmdRtxkZ6uOi66ulM91XZwqRcK3rvn3nvuPcV7D/7rZrPAbalc6/iWZC2M/cBdGdRGfGemzU5tfnPZrP+vCeJ45xnM1oClWzR5BdmpRqNydhoY7HVICuKOX8dsIyt+SdiJNNSy0v4+pf19aaIHhJ0AfgDuw/RJ3PFrkgr1ip13/HrsvOLY34hjf3hWkqQgjv2ROPY3YucVd/wbe2MmJMpk2UD0FQYHmgt3fDbCvPe39xOesNRKYZh8tbCw8OsIi6Leill6DqOM7GCjUdksdLMtlWPnr2TdH8ljzu0cip3vxc4r+/qR656ejPHHMuxHSaXCBM7tHBL2MXCpXqs8ZGbJcCr/IMZFoAR8YdATHABCGc81a/MfZnKF7lr3e8SyoWfr9eoGTPxkOzhktPdHxQEItAqUMN5p1Ocfr9fnn8I4CmDi8DjbLBE6MySz1sg/N1oI2w8iCdML+dGV7L5rYTlS0h/rGlr6eaIAjLsndE7sPAFgPFIgAN0JECbJ1XxOs9n8C/hgtG+3r1eTdPAeAClf5mPDMPk5VQiwOIXg1hZFvXstGFwAlhBXB3N6OY8HQWBpklGPfDdh+w0gCcPJsXNmpo+AJcO+CcP0scVq9Zc8vrtro9zfiwTSRQBLgtVpxdvt61VMjwJ/mA1W8udg3ECoJ4cLvi4QmGlriOlFaShk3hYXb/PI3kK8WqvV/tyLD3PseQCTPi10mB20y7Hzcs4fnTbFLHOue3zaQRtPsGzWR3Zq2A1vR1FvZW+RyHVPR86/VvBHvRVJbwIotZfMbLdAANBoVM5irGOUzdJzzvljk3JJgG7uFDrXPT6+h4y1ZrOyla9ZeA8kBe5a93XEySxiW+gMiZ0PgsFPAGk6dw+hVg17AbGcxa3VFyqvmFmarzfjwem2MK0D9/9TTGaXkZ2ceoPOIsimKXU6/mnJWjIetuzJFLRNfGumzVptfiuv+f/P/gaDC5hAWJxITQAAAABJRU5ErkJggg==',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAAEN0lEQVRIie2W3W8UVRjGf+/MtNQmArvL7GwRImAgJoqiCCaiXnhhMJCUmHijieHO+0rbS++IKOBfwIUkmkiItZXGRO+MRsOHRvxITKSrAbo7s91ZSsMFuzPn9WJntst+0DZ6p+/VmXOe8zzvcz7eOfBfC1kLWFWdIAj3YckORfNNAgkwOpfPZy+JSPSvCgdBbY+KjmE4hJDtnRUhFrMYOeV5mZ/+kXCptORaTuM0yuuAtZokAYPwUdxwxjZvXr+wZuFyOXxcLGaA7QAodRGmjDDtiPkxiqLrAI7jbI3UehplVJQjCIMJxZwaRguF7C+rFi6Xw91i8Q2wPoGdR2XS8zbO3c+q7996BInfA3k16Vo0sR4YGcn9uqJwqbTkWnbjIrANiBGd8Nzc6fsJdidQPYbICZrbM9eoO/u3bFlfbcc4nZMsq/FBIgqik52i8/Pzw7Y9dBR4HothkCsW0RnXdedTjOflTvp+aCGcAHYMDDZOAUf7Og6C2h5FrzQzlfNePvNa+3ilUtll1P4C2NGR75IKbxTc7Of3OK+EUyhHAIPylOdlr7YMtgNVdAywUOqoTLaPFYvFIaPWhTbRG8DvSftBUT6pVCq77nGl9jhKHbBocreiJayqDoZDACJMdR6k4eGNr4DsTJbpeN7NPOzls4+qyCgQAw8YY7/dPief3/AHItNJGodVtbW1LeEgCPenxcEI03SFXAVKwHeum3lHRAxAwc3MAF8lbAe6p2nKlfP98Jm0e/lwWbIdTZpqX+6c73kbrwGbO/tV1QoWahkUUOLOcYv4ssFuftiyDfj+HscYHVmG3y11Zd4jisXiUBDUzqI8CyBwoUdiN9O2oK3E2xyjqWMRWbGG37hxOzewLppNRYGrIvG7nTjbtq2oax3aDxfS7rJrSdtDVWVgMPq0TfTrRt15yXXdpU7sXdWHlufRct9yLEb/JDEaY+9l+ap0RaVy6wXgxSTjj/P5zFERafTC2rG1t1UtDH+l/S3H+Xz2IkqYkI32tdtkKLSIbTPRT7TpSFKuBc/LXuoSFpEIi1kAUY74/q3O6tSKO3duzwgcR3hr06ZNN/vhgmBxJ2girLMi0mO3Ad+vPekHYewHofpB7XxfF6sMvxJ+1uQKo3I53L0S+GwCVt+vHuuHC4LqwSCoHuw/Hk6mPOWgdqZzvOtVETecMaBZLkVO+H443otYkXOKnOuZvB9OKBxPPq811lldHD3va6lUfcyy5VtgQyIzhdrjSfVKyKsvA3he7stll4s7VeL32w7norH1uZFc7rdVCbeJz5D+jZQ6ItOITsdifnCMuQ4QWdbW5pWR0eQgDaROTayjvV4f9xWGpDoNNk6CvMkaHnuKfhjVB8Y7Xx2rFk7D98Mnmv9TOQzk+sAWQGfVyKlCIfvzSpxrfdDbvh/uw5btoEkRkRKxKXpe7nLfe/p/AH8DWwTKnjT0U1AAAAAASUVORK5CYII=',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAAFGUlEQVRYhe2Y3W8UVRjGn3emTUsrH7PbmWkLVYoJxCsT5IbwF/gBxRAbjUjAcKcGkQiiBiSKElQQwq0hQbFgMKUYwoUXxhu4ICReGU3EYluWnTnsbqG2UN2Zx4vu7pyd7nQXPy40vMkmc8553vf85ny8c84C9+0/ZnKvDiRNpcbXkOiDcBUAF8ASACAwKoAPymURDtm2dVFEgn8FaHR0dF5LS/s2AjsAdDTopgT4eHp68mhPT8+dfwzI93P9hBwCsLhBkLiNCbjdcdJn/hYQScNThf0CvFHdgBEKhoQ8bxjhtSAIxgDANM0lYWgspchTAPoE6KkKBxxwbettEQnvGYik4avxUwCf0arHBLLHthedqLc2SJqeym8WyD5oI0vgS9e2nkuCSgTK+vkPqkeGg4Jgk+M4v80FEjel1PwQ5mcg+rRO33ec1FsNB/H9XL/n51n++X7uMEkjSU+yOZPJtM3RbmT9/FEtZuh5uQ21tLNGaHR0dF5zS/tPlfkXnHU6rA3xISZpKJXfTMhLAB4FYAK4RmCgpVkOWpY1Htf7fmEQgnWlqutB8e7y7u7uKV03661bWtq3aYtxTFh8IQ4zPDzc6qnC14R8CmBlCQYAlgqw+/civ1dKLa96c5HQMIKNADKlqsWm2fpyvP8qIJImgdcqQSB7aq2ZtraFnwjwhFYVAJiOAuGhkOa54eHhVt3Ptu0JCvdGlNxB0tQ1VUBKja8BYJeCjtj2ohNxmEw+/yAEW6OgONhkMuXYVjvBZwFMllpWtLUt3BL3dztSxwGMlZwdpQqrE4FIrK88C8/W2tpCPoBoii64dmpXOp2+LSJBp5M+TUi0eyTaWZWqmZhDUT/Vmmog4Uqt4UI8GAB0pdM/QLARlEOGBFvj7UJ+GwWsSowaJ85HejymtzVVC9kdbbymn2sFAwDXTp0EcLJmZ4J+lgsGb9TWBL+Ulw6B7kQgQLqi5+lsElAtK6WBfQQqU0bgVIL2ulas+j42xbX3AlG2TCbT5t8sfAGIth7kG7fDOl5Lb5qmUYxWZ1VKieUh0Ya4pQsNGEkxzdYB/dMA4ALD6aeTvnckK9PEKC/NBhJQayw+3AiQ542v07IvIPjQsa21nZ2dk0k+YWgs0wCq1lls28uVihNET3yJJsKKjsAx107trHsSAJ6sPAsuJwKJcEgrro9n0Zpm4Hb50RSpewAj2QRoIxriXCKQbVsXASgAEKDHU/nN9ToQFvcJ8CYom2zb+q6e3ruZ34LSGRwQz3GsS1Xx4g6+n99F4ECpeN2Q4BHbtifqddSI5XK5BcVAfgQws2HI1103/ZGumfW1n5y8dQTESKm4OKR5ut7UeZ7nZrMTzlwakkYQyOcVGMGvU1O3j8V1s4B6e3vvinAHopz0uKcKiQe0bHbCgTRfFeOPqzduTNhJMJ4qHCGwtlyFULb39vberQsEAI6TPqNNGwR4xfcLg0qp+bX0mCOh5nK5BUoVhgSIzj7ke65rDdbSz3nI91RhQIB+rTpD4V63I3Vc39rZ7IRDgl1d85Xm3+TfLLwI4h2Up2kGfcBxrOdFpOZL1L0GKVV4l8BuXVu6oZ4T4LxhhMNhGI4BgGEYS8LQWFbKM+tQ2U0zbgLut+3UniSYukBl87zcBoocjt2zGjdiBJBXk6ZJt8SbhG6um/7qzuSt5SB2AeI1TiIeiJ1TU7dWNAID/OU/GwqrKegTYlUIdEr0Z8OYAWQpuIwQQ45jXZrrlnrf/hf2J/dXORT6PFLWAAAAAElFTkSuQmCC',
        },
        'active': {
            24: 'iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAYAAADgdz34AAAABmJLR0QA/wD/AP+gvaeTAAABzklEQVRIidWVsWqUQRSFz+ySbTZqkVIt7CQGCwUfwEU7ybJKQHwAMUYl2Jm8QKK1TyDaRnwDrRI0RZJCiYKC9oZUicJnsWfh7mR2/z92uTD8/OecuWe43LkjnfRI40igJanrdUXSOVM/JW1KWpO0llI6PLYzcBv4RnV8BXrHSdwAnocEW8ATYBpoe10yth10q0CjjsEg+QHwYNwmH2beWoCVOmUZJL+ecWeAW0APOJtxnWDSHZW8FWo+n3FzwO9QjkNgOdMsmNsFJkoGc6HmzYBfdkKAD8A74K//7wZdE9gxfqdk8Mbk4wx/avxlwO4be59pF42/Khl8MTmd4W3gHtAO2EVrv2faGeOfSwb7JiePkMO6KZcK4HXGnTK+/18GwIXQCD+A8xl/2txeaXOxRJlm3ZoNslY1f6RE8RJ99PfGiORTkq5J2pPUSSn9Kshu+rtRSjBo021CmwY+AS/I7kjgK9u0RX9wATwsJRkXwCPvLV80i3oWHQCdAr8MLBXwOCpmq06yGkwWGL7VS8Cz8N/0yesNO29qBBNc10V3yKTXjLGdoFuhzrgORl3Xsyp2GTVBVf1kTkiaVf/JvKrhJ/OT+k/m25TSn9onP3HxDyxP9wedq+EdAAAAAElFTkSuQmCC',
            30: 'iVBORw0KGgoAAAANSUhEUgAAAB4AAAAeCAYAAAA7MK6iAAAABmJLR0QA/wD/AP+gvaeTAAACmElEQVRIie2Wz0tVQRTHv/N0IdWm1KjcpKZo/oSIIqhFhLgpaBOYrVu0LLNVS6F/oUUlFqJZ1qJo3yKCXEWLCH8UUUL2oAwCxcenxZ3nO9439777wl0duDD3nO/5fs/cO3NmpH/NXDVgoFbSUUktkvZ69zdJi5LeOOc2trU6oB+YAPIkW95j+rZDsBG4DxRSBONW8AU0/K1oN7BoCNeAKWAI6AB2+qcDuAhMe0zRFoDuakV7gJ+GZAZoyZDXCjw2eT+ArqyijcCST9wArlZVdcQxYn7PAlCfJemBqfhaIL4DuAJMAk+Bm8CBAG7U8IxXEu03lc4E4u1+BnFbBc4G8E98vAD0pglPmIXUEovVAR+M2GfgvXn/DbTHcg5RWnDhWQO1lPbpVCB+3oiMATnvP+fXAsDtQN5DH/tO1IDKACcM8VAg3gp8BV7FCYAXPu9dIG/Y8B4v+i1BsxnPxQmccwuSQosoJ2m3fy2UzWgr10FJryUpZ5z7zXg5QFBmQJ2kCUnHvOtZAPbFjDcLtzPGjCseHkR787kRfSvpVgCaC/i2OO0syz5pTNRJmjWiLyWdds79CsCbzHhz9lb4oxkfSROWdFLSKT+elHTGOZdPwFquT2XR2HaaTlMFLpiV2lQBO+NxK0BNEiixgcRwdX4vX64g2gaspzYQD+yj1DIfpZFmMaJejm8wPZXAxVkDjKTgBoHBlPgNw3MnS5UNlA6CAnA9AbcKrCbERs2Xmwf2VBT2iV1Eh3jRZoHWGGYAGIj52sznxXMcziQaE7dH4BpRwx8GOoFd/ukELvnVu27w82S9fQTE64F7VH/Zu0uWW0eGAnqBcaKjLclWPCZ99Xqr9kJfo+hC3yxpn3cvS1qSNOecC51O/02S9Acn8e2oM5RhjgAAAABJRU5ErkJggg==',
            36: 'iVBORw0KGgoAAAANSUhEUgAAACQAAAAkCAYAAADhAJiYAAAABmJLR0QA/wD/AP+gvaeTAAADCElEQVRYhe2YvU9UQRTF79vVZIGF2AjGGAu/IBZWNgY/ShWjxkCwMBYImljZKGIrgjFEjYCJf4EJFioaiTHGWGlBKI1oIx8LIlaKJhbIz+K9zd4d3szOW2lIOMkmk51zz5x5d+7MvCeyhlWGIGkAkBaRRhE5KSJ7RaRORLZE3dMiMi8ioyIyLCLvgiD4uzJWlxupALqA7/hjHrgKVKy0mVYgl8CIiWmgZSWMpICbMQNMAv3AYaAeqIp+9dF/A8CUEbME9AKp/zHzKGambYTrqFR8GmiPebJDZZmKeTKPgWwZOtXAU0OrJ6lIqyFw1zUrYD1Q6ehPRSnW6Wv2NVNh5P9JnJlokHPAGLAYcb9E62SDhT+sdHOuSejALmPNLEsTkAFeYMcEsCsmrhqYUbzOUmbShHtHHm0W3gPDwCLwx/hvHMjExHYozjdcBQIcVOTJODKwlUKKAG4BNdFkTgO/VN9Fy6SnFWe/y9AdRbxn4exWnJGY/kuq/6VFY1Bx+lyG3iriEQfvDHAb2BzTt0dpfLDEH1WcNy5DnxVxh5XoAHBDaby2cOoVZ9wltqCIiTZBwrLuphgdFm5WcRZcoj/LMQRUsnw3foWlggiLII8fLuFPirjT00xA8YYHMAJUOWIaFPej7jN34FnV3u5jSERORL88+kTkeBAEvx0x21T7q8vQmGo3eRrSvMEgCDo9bonHVHvUygIOqEc5ZVsDRkyfijnkwV9H8cbY6CKbR0e7xwBZ4BpwthQ34p9X+nOUuhsR3oHzyAHVPgN5mqkBZpX+ZZ+gDOE5pivGmTqgDqgtwUkBz5TuBDGHry24hfASlUe/7dECtYQH6gKw0WFmQOktAae8zCiRXooxHJe+yNCCzVCUpueG1vVEZtSshgyhGcL7TNrg1ppmCKvpgrFmAB4CiV9QtakeI30QbgmDhKd2A2GlZaN2E3Cf4tLOp6m7bDOGsWaWv2clwSRJ14yHqQzQSbh3+GIOuIJvNUn5Hxv2SeFjwyYpfGzIicicFD42vA+CYCnpGGtYVfgHq/8zJZB6jwYAAAAASUVORK5CYII=',
        },
    },
}


def _snappy_nav_icon(canvas, name, stroke):
    """Render a crisp, exact-size sidebar icon with theme-aware coloring.

    Each canvas owns its cached PhotoImage references; they must stay alive
    or Tk displays an empty icon.  Fall back to native vectors on an old Tk.
    """
    try:
        px = int(getattr(canvas, "_snappy_icon_size", 24))
        px = px if px in (24, 30, 36) else 24
        color = str(stroke).lower()
        if color == "#ffffff":
            role = "active"
        elif color == THEMES["dark"]["text"].lower():
            role = "dark"
        else:
            role = "light"
        token = (name, role, px)
        cache = getattr(canvas, "_snappy_png_cache", None)
        if cache is None:
            cache = {}
            canvas._snappy_png_cache = cache
        photo = cache.get(token)
        if photo is None:
            encoded = _NAV_ICON_PNG[name][role][px]
            photo = tk.PhotoImage(master=canvas, data=encoded, format="png")
            cache[token] = photo
        canvas.delete("all")
        canvas.create_image(px / 2, px / 2, image=photo, anchor="center")
    except (KeyError, ValueError, tk.TclError):
        _snappy_nav_icon_vector_fallback(canvas, name, stroke)

class ThemeKit:

    BTN_H = 31

    def __init__(self, cfg):
        self.cfg = cfg
        self.pal = dict(THEMES[str(cfg.get("theme", "light"))])
        self.roots = []
        self.repaints = []
        self._img_cache = {}

    def attach(self, root):
        self.roots.append(root)
        return root

    def reg(self, widget, role):
        try:
            widget._tkrole = role
        except Exception:
            pass
        return widget

    def repaint(self, fn):
        self.repaints.append(fn)
        return fn

    def current(self):
        return "dark" if self.pal.get("bg") == THEMES["dark"]["bg"] \
            else "light"

    def switch_to(self, name):
        self.pal.clear()
        self.pal.update(THEMES[name])
        self.cfg.set("theme", name, save=True)
        self.apply()

    def toggle(self):
        self.switch_to("dark" if self.current() == "light" else "light")

    def apply(self):
        for root in list(self.roots):
            try:
                if not root.winfo_exists():
                    self.roots.remove(root)
                    continue
                self._walk(root)
            except tk.TclError:
                pass

        for fn in list(self.repaints):
            try:
                fn()
            except Exception:
                pass

    def _walk(self, w):

        try:
            if not w.winfo_exists():
                return
        except tk.TclError:
            return
        role = getattr(w, "_tkrole", None)
        if role:
            self._style_one(w, role)
        for c in w.winfo_children():
            self._walk(c)

    def _style_one(self, w, role):
        pal = self.pal
        if role == "bg":
            w.configure(bg=pal["bg"])
        elif role == "tab_bg":
            w.configure(bg=pal["tab_bg"])
        elif role == "sidebar_heading":
            w.configure(bg=pal["tab_bg"], fg=pal["text"])
        elif role == "sidebar_canvas":
            w.configure(bg=pal["tab_bg"])
        elif role == "bg_text":
            w.configure(bg=pal["bg"], fg=pal["text"])
        elif role == "canvas_bg":
            w.configure(bg=pal["bg"])
        elif role == "card":
            w.configure(bg=pal["card"])
        elif role == "card_text":
            w.configure(bg=pal["card"], fg=pal["text"])
        elif role == "card_muted":
            w.configure(bg=pal["card"], fg=pal["muted"])
        elif role == "card_bold":
            w.configure(bg=pal["card"], fg=pal["text"])
        elif role == "entry":

            w.configure(bg=pal["entry"], fg=pal["text"],
                        insertbackground=pal["text"],
                        readonlybackground=pal["entry"],
                        highlightbackground=pal["entry_bd"])
        elif role == "check":
            w.configure(bg=pal["card"], fg=pal["muted"],
                        selectcolor=pal["entry"],
                        activebackground=pal["card"])
        elif role == "listbox":
            w.configure(bg=pal["entry"], fg=pal["text"],
                        highlightbackground=pal["entry_bd"],
                        selectbackground=pal["accent"],
                        selectforeground="#ffffff")
        elif role == "desc":
            w.configure(bg=pal["desc_bg"], fg=pal["muted"])
        elif role == "desc_frame":

            w.configure(bg=pal["desc_bg"])
        elif role == "status":
            w.configure(bg=pal["status_bg"], fg=pal["muted"])
        elif role == "status_frame":

            w.configure(bg=pal["status_bg"])
        elif role == "canvas_card":
            w.configure(bg=pal["card"])
        elif role == "panel":

            w.configure(bg=pal["card"],
                        highlightbackground=pal["entry_bd"])
        elif role == "hl":
            w.configure(highlightbackground=pal["entry_bd"])
        elif role in ("btn", "btn_accent", "btn_green", "btn_amber"):
            meta = getattr(w, "_rmeta", None)
            bg, fg = self._btn_colors(role)
            surface = pal[meta[2]] if meta else pal["card"]
            w.configure(fg=fg, bg=surface, activebackground=surface,
                        activeforeground=fg, bd=0)
            if meta and HAVE_PIL:
                try:
                    img = self.rounded(meta[3], meta[4], bg, 7)
                    w.configure(image=img)
                    w._img = img
                except Exception:
                    pass

    def _btn_colors(self, role):
        pal = self.pal
        if role == "btn_accent":
            return pal["accent"], "#ffffff"
        if role == "btn_green":
            return pal["green"], "#ffffff"
        if role == "btn_amber":
            return pal["amber"], "#ffffff"
        return pal["light_bg"], pal["text"]

    def rounded(self, width, height, fill_hex, radius=7):
        key = (width, height, fill_hex, radius)
        if key not in self._img_cache:
            img = _PILImage.new("RGBA", (width, height), (0, 0, 0, 0))
            d = _PILDraw.Draw(img)
            d.rounded_rectangle((0, 0, width - 1, height - 1),
                                radius=radius,
                                fill=hex2rgb(fill_hex) + (255,))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            self._img_cache[key] = buf.getvalue()
        return tk.PhotoImage(data=base64.b64encode(self._img_cache[key]).decode("ascii"))

    def button(self, parent, text, command, kind="plain", big=False,
               surface="card", fixed_w=None, height=None):
        role = {"plain": "btn", "accent": "btn_accent",
                "green": "btn_green", "amber": "btn_amber"}[kind]
        bg, fg = self._btn_colors(role)
        surf = self.pal[surface]
        b = tk.Button(parent, text=text, relief="flat", bd=0,
                      font=(F_BTN_BIG if big else F_BTN),
                      bg=surf, fg=fg, activebackground=surf,
                      activeforeground=fg, highlightthickness=0,
                      takefocus=0, cursor="hand2", compound="center",
                      padx=0, pady=0,
                      disabledforeground="#9aa0a6")
        f = tkfont.Font(font=(F_BTN_BIG if big else F_BTN))
        w = int(fixed_w) if fixed_w else \
            int(f.measure(text)) + (44 if big else 30)
        h = int(height) if height else (40 if big else self.BTN_H)
        b._rmeta = (text, big, surface, w, h, role)
        if fixed_w:
            b._fixed_w = int(fixed_w)
        if HAVE_PIL:
            try:
                img = self.rounded(w, h, bg, 7)
                b.configure(image=img)
                b._img = img
            except Exception:
                pass
        b.configure(command=command)
        return self.reg(b, role)

    def _draw_pill(self, cvs, right, track_hex, knob_hex):
        cvs.delete("all")
        if HAVE_PIL:
            img = self.rounded(SW_W, SW_H, track_hex, SW_RADIUS)
            cvs.create_image(0, 0, anchor="nw", image=img)
            cvs._track = img
        else:
            cvs.create_rectangle(0, 0, SW_W - 1, SW_H - 1,
                                 fill=track_hex, outline="")
        x = (SW_W - SW_PAD - SW_KNOB) if right else SW_PAD
        cvs.create_oval(x, SW_PAD, x + SW_KNOB, SW_PAD + SW_KNOB,
                        fill=knob_hex, outline="")

    def set_text(self, b, text):
        meta = getattr(b, "_rmeta", None)
        b.configure(text=text)
        if meta is None:
            return
        _t, big, surface, _w, h, role = meta
        bg, _fg = self._btn_colors(role)
        f = tkfont.Font(font=(F_BTN_BIG if big else F_BTN))

        w = getattr(b, "_fixed_w", None) or \
            int(f.measure(text)) + (44 if big else 30)
        if HAVE_PIL:
            try:
                img = self.rounded(w, h, bg, 7)
                b.configure(image=img)
                b._img = img
            except Exception:
                pass
        b._rmeta = (text, big, surface, w, h, role)

    def switch_widget(self, parent, on_toggle):
        pal = self.pal
        f = tk.Frame(parent, bg=pal["card"])
        self.reg(f, "card")
        left = tk.Label(f, text=i18n.L("theme.light"), font=F_DESC,
                        bg=pal["card"], fg=pal["text"])
        self.reg(left, "card_text")
        R(lambda: left.configure(text=i18n.L("theme.light"))
          if _live(left) else False)
        cvs = tk.Canvas(f, width=SW_W, height=SW_H, highlightthickness=0,
                        takefocus=0, bg=pal["card"])
        self.reg(cvs, "canvas_card")
        right = tk.Label(f, text=i18n.L("theme.dark"), font=F_DESC,
                         bg=pal["card"], fg=pal["text"])
        self.reg(right, "card_text")
        R(lambda: right.configure(text=i18n.L("theme.dark"))
          if _live(right) else False)
        left.pack(side="left", padx=(0, 6))
        cvs.pack(side="left")
        right.pack(side="left", padx=(6, 0))

        def paint():
            dark = self.current() == "dark"
            self._draw_pill(cvs, dark, self.pal["light_bg"],
                            self.pal["text"] if dark else "#ffffff")

        self.repaint(paint)
        for w in (cvs, left, right):
            w.bind("<Button-1>", lambda e: (on_toggle(), paint()))
        paint()
        return f

    def bool_switch(self, parent, var, word=True):
        pal = self.pal
        f = tk.Frame(parent, bg=pal["card"])
        self.reg(f, "card")

        lbl = None
        if word:
            lbl = tk.Label(f, text=i18n.L("word.off"), width=3, anchor="e",
                           font=F_DESC, bg=pal["card"], fg=pal["muted"])
            self.reg(lbl, "card_muted")
            lbl.pack(side="left", padx=(0, 6))

        cvs = tk.Canvas(f, width=SW_W, height=SW_H, highlightthickness=0,
                        takefocus=1, bd=0, bg=pal["card"], cursor="hand2")
        self.reg(cvs, "canvas_card")
        cvs.pack(side="left")

        def paint(*_a):
            try:
                if not cvs.winfo_exists():
                    return
            except tk.TclError:
                return
            try:
                on = bool(var.get())
            except (tk.TclError, ValueError):
                on = False
            pal = self.pal
            self._draw_pill(
                cvs, on,
                pal["switch_on"] if on else pal["light_bg"],
                "#ffffff" if (on or self.current() == "light")
                else pal["text"])
            if lbl is not None:
                try:
                    lbl.configure(text=i18n.L("word.on") if on
                                  else i18n.L("word.off"),
                                  fg=pal["switch_on"] if on
                                  else pal["muted"])
                except tk.TclError:
                    pass

        def toggle(_event=None):
            try:
                var.set(not bool(var.get()))
            except (tk.TclError, ValueError):
                var.set(True)
            paint()

        for w in ([cvs, lbl] if lbl is not None else [cvs]):
            w.bind("<Button-1>", toggle)

        cvs.bind("<space>", toggle)
        cvs.bind("<Return>", toggle)

        var.trace_add("write", paint)
        self.repaint(paint)
        paint()
        return f

    def titlebar_hook(self, root):
        def _apply():
            try:
                if not root.winfo_exists():
                    return
                hwnd = int(root.frame(), 16)
                apply_window_theme(hwnd, self.current() == "dark",
                                   hex2rgb(self.pal["bg"]),
                                   hex2rgb(self.pal["text"]))
            except Exception:
                pass
        self.repaint(_apply)
        try:
            root.update_idletasks()
        except Exception:
            pass
        _apply()
        try:
            try:
                root.after(150, _apply)
            except Exception:
                pass
        except Exception:
            pass

    def defocus_hook(self, root):
        def _defocus(event):
            try:
                if _widget_class(root, event.widget) in ("Button",
                                                         "Checkbutton"):
                    root.winfo_toplevel().focus_set()
            except Exception:
                pass
        root.bind("<ButtonRelease-1>", _defocus)

def registration_line(identity, ent):
    try:
        who = online.identity_label(identity) if identity else ""
    except Exception:
        who = ""
    return i18n.L("st.registered",
                  (who or "").strip() or "unknown",
                  online.entitlement_summary(ent))

def _login_window(cfg, kit):
    pal = kit.pal
    result = {}
    W, H = 560, 640

    root = tk.Tk()
    kit.attach(root)
    kit.reg(root, "card")
    root.title(i18n.L("login.title", APP_NAME))
    root.configure(background=pal["card"])
    root.resizable(False, False)
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry("%dx%d+%d+%d" % (W, H, max(0, (sw - W) // 2),
                                   max(0, (sh - H) // 3)))
    try:
        root.iconbitmap(logo.ico_path())
    except Exception:
        try:
            root.iconphoto(True, logo.photoimage(tk, 64))
        except Exception:
            pass
    kit.defocus_hook(root)
    kit.titlebar_hook(root)

    top = tk.Frame(root, bg=pal["card"])
    kit.reg(top, "card")
    top.pack(fill="x", padx=14, pady=(10, 0))
    kit.switch_widget(top, kit.toggle).pack(side="right")

    def _surf_of(widget):
        return _surf_of_global(widget, pal["card"])

    try:
        lb = tk.Label(root, bg=pal["card"])
        kit.reg(lb, "card")
        im = logo.surface_image(tk, 112, _surf_of(lb))
        lb.configure(image=im, bg=_surf_of(lb))
        lb._img = im
        lb.pack(pady=(10, 2))

        def _relogo():
            surf = _surf_of(lb)
            im2 = logo.surface_image(tk, 112, surf)
            lb.configure(image=im2, bg=surf)
            lb._img = im2
        kit.repaint(_relogo)
    except Exception:
        pass

    t = tk.Label(root, text=APP_NAME, font=F_H1, bg=pal["card"],
                 fg=pal["text"])
    kit.reg(t, "card_text")
    t.pack()

    sub = tk.Label(root, font=F_DESC, bg=pal["card"], fg=pal["muted"],
                   text=i18n.L("login.sub"),
                   wraplength=460, justify="center")
    kit.reg(sub, "card_muted")
    sub.pack(pady=(2, 12))

    msg = tk.StringVar(value="")
    msg_lbl = tk.Label(root, textvariable=msg, font=F_DESC, bg=pal["card"],
                       fg=pal["accent"], wraplength=460, justify="center")
    kit.reg(msg_lbl, "card")
    msg_lbl.pack(side="bottom", pady=(0, 12))
    msg_kind = ["err"]

    def _remsg():

        msg_lbl.configure(fg=(pal["green"] if msg_kind[0] == "ok"
                              else pal["accent"]))
    kit.repaint(_remsg)

    def set_msg(text, ok=False):
        msg_kind[0] = "ok" if ok else "err"
        msg_lbl.configure(fg=(pal["green"] if ok else pal["accent"]))
        msg.set(text)

    body = tk.Frame(root, bg=pal["card"])
    kit.reg(body, "card")
    body.pack(pady=(0, 6))

    btns = {}
    waiting = {"on": False}

    def done(identity, err, label):
        try:
            if not _live(root):
                return
            waiting["on"] = False
            for b in btns.values():
                b.configure(state="normal")
            if identity:
                result["identity"] = identity
                set_msg(i18n.L("login.signedin",
                             online.identity_label(identity)), ok=True)
                root.after(350, root.destroy)
                return
            set_msg(err or i18n.L("login.failed", label))
        except tk.TclError:
            pass

    def choose(provider, label):
        if waiting["on"]:
            return
        if not online.configured(cfg):
            set_msg(i18n.L("login.not_configured"))
            return
        waiting["on"] = True
        set_msg(i18n.L("login.waiting", label))
        for b in btns.values():
            b.configure(state="disabled")

        def worker():
            try:
                identity, msg = online.login_with_provider(cfg,
                                                           provider)
            except online.OnlineError as exc:
                identity, msg = None, i18n.L("login.unreachable", exc)
            try:
                root.after(0, lambda: done(identity, msg, label))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True,
                         name="login-%s" % provider).start()

    def provider_pill(pid, w, h, bg, dark):
        th = max(8, h - 16)
        img = _PILImage.new("RGBA", (w, h), (0, 0, 0, 0))
        dr = _PILDraw.Draw(img)
        dr.rounded_rectangle((0, 0, w - 1, h - 1), radius=7,
                             fill=hex2rgb(bg) + (255,))
        name = pid + "_dark" if (dark and pid == "github") else pid
        mark = _PILImage.open(io.BytesIO(base64.b64decode(
            logo.PROVIDER_LOGO_B64.get(name, logo.PROVIDER_LOGO_B64.get("discord", "")))))
        if mark.height != th:
            mark = mark.resize(
                (max(1, int(round(mark.width * th / mark.height))), th),
                _PILImage.LANCZOS)
        img.paste(mark, (16, (h - th) // 2), mark)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"))

    def paint_provider_buttons():
        dark = kit.current() == "dark"
        bg, fg = kit._btn_colors("btn")
        for pid, b in btns.items():
            b.configure(fg=fg, bg=pal["card"], activebackground=pal["card"],
                        activeforeground=fg)
            if not HAVE_PIL:
                continue
            text, w, h = b._pmeta
            try:
                img = provider_pill(pid, w, h, bg, dark)
                b.configure(image=img)
                b._img = img
            except Exception:
                pass

    for pid, label in online.PROVIDERS:
        if not online.PROVIDER_TABLE[pid]["client_id"]:
            continue
        text = i18n.L("login.provider", label)
        f = tkfont.Font(font=F_BTN_BIG)
        w = int(f.measure(text)) + 96
        h = 40
        b = tk.Button(body, text=text, relief="flat", bd=0, font=F_BTN_BIG,
                      highlightthickness=0, takefocus=0, cursor="hand2",
                      compound="center",
                      disabledforeground="#9aa0a6",
                      command=lambda p=pid, l=label: choose(p, l))
        b._pmeta = (text, w, h)
        btns[pid] = b

    wmax = max([b._pmeta[1] for b in btns.values()] or [0])
    for b in btns.values():
        text, _w, h = b._pmeta
        b._pmeta = (text, wmax, h)
        b.pack(fill="x", padx=60, pady=(0, 8))
    if not btns:

        set_msg(i18n.L("login.no_provider"))
    paint_provider_buttons()
    kit.repaint(paint_provider_buttons)

    note = tk.Label(body, font=F_DESC, bg=pal["card"], fg=pal["muted"],
                    text=i18n.L("login.note"),
                    wraplength=440, justify="center")
    kit.reg(note, "card_muted")
    note.pack(pady=(6, 4))

    R(lambda: sub.configure(text=i18n.L("login.sub")) if _live(sub)
      else False)
    R(lambda: note.configure(text=i18n.L("login.note")) if _live(note)
      else False)

    root.update_idletasks()
    need = root.winfo_reqheight() + 8
    if need > H:
        H = need
        root.geometry("%dx%d+%d+%d" % (W, H, max(0, (sw - W) // 2),
                                       max(0, (sh - H) // 3)))

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return result.get("identity")

def _run_main(cfg, identity, app, hook_thread, kit, ent=None):
    action = {"now": "quit"}
    pal = kit.pal

    aids = []
    AIDS_CAP = 64

    def after_ms(ms, fn):
        aid = root.after(ms, fn)
        aids.append(aid)
        if len(aids) > AIDS_CAP:
            del aids[:len(aids) - AIDS_CAP]
        return aid

    def post(key_id):
        tid = app.thread_id
        if tid:
            try:
                PostThreadMessage(tid, WM_HOTKEY, key_id, 0)
            except Exception:
                pass
        else:
            status_msg(i18n.L("st.engine_dead"))

    vars_ = {}
    dirty = {"on": False}
    suppress = [False]

    root = tk.Tk()
    root.withdraw()
    kit.attach(root)
    kit.reg(root, "bg")
    root.title(APP_NAME)
    root.configure(background=pal["bg"])
    root.update_idletasks()

    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()

    # An ordinary, non-maximizable window.  Size is constrained by the
    # screen's work area, so low-resolution displays are not cut off.
    UI_MAX_WIDTH = 1340
    UI_SIDEBAR_WIDTH = 232
    gw = min(1260, max(1, sw - 40))
    gh = min(884, max(1, sh - 75))
    root.geometry("%dx%d" % (gw, gh))
    root.minsize(gw, gh)
    root.maxsize(gw, gh)
    root.resizable(False, False)
    try:
        root.iconbitmap(logo.ico_path())
    except Exception:
        try:
            root.iconphoto(True, logo.photoimage(tk, 64))
        except Exception:
            pass
    kit.defocus_hook(root)
    kit.titlebar_hook(root)

    def _focus_watch(event):
        try:
            app.input_focused = _widget_class(root, event.widget) in (
                "Entry", "TEntry", "Combobox", "TCombobox", "Listbox",
                "Spinbox", "Text")
        except Exception:
            app.input_focused = False
    root.bind_all("<FocusIn>", _focus_watch, add="+")
    root.bind_all("<FocusOut>",
                  lambda e: setattr(app, "input_focused", False),
                  add="+")

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    def restyle_tt():
        style.configure("TCombobox", fieldbackground=pal["entry"],
                        background=pal["light_bg"], foreground=pal["text"],
                        arrowcolor=pal["text"], font=F_BASE, padding=4,
                        borderwidth=1)
        style.map("TCombobox",
                  fieldbackground=[("readonly", pal["entry"])],
                  foreground=[("readonly", pal["text"])])
        style.configure("TScrollbar", background=pal["light_bg"],
                        troughcolor=pal["bg"], arrowcolor=pal["text"],
                        borderwidth=0)
        try:
            root.option_add("*TCombobox*Listbox.background", pal["entry"])
            root.option_add("*TCombobox*Listbox.foreground", pal["text"])
            root.option_add("*TCombobox*Listbox.selectBackground", pal["accent"])
            root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        except Exception:
            pass
    kit.repaint(restyle_tt)
    restyle_tt()

    # Bounded, horizontally centered workspace: full screen adds neutral
    # margins, not massive gaps between settings and their controls.
    shell = tk.Frame(root, bg=pal["bg"])
    kit.reg(shell, "bg")
    shell.place(relx=0.5, y=0, anchor="n",
                width=min(UI_MAX_WIDTH, gw), relheight=1.0)

    def _size_shell(event):
        if event.widget is not root:
            return
        try:
            shell.place_configure(width=max(1, min(UI_MAX_WIDTH, event.width)))
        except tk.TclError:
            pass

    root.bind("<Configure>", _size_shell, add="+")

    topbar = tk.Frame(shell, bg=pal["card"])
    kit.reg(topbar, "card")
    topbar.pack(fill="x", padx=10, pady=(9, 7))

    topbar.configure(height=TOPBAR_H)
    topbar.pack_propagate(False)
    try:
        hl = tk.Label(topbar, bg=pal["card"])
        kit.reg(hl, "card")
        himg = logo.surface_image(tk, TOPBAR_LOGO_PX,
                                  _surf_of(hl, pal["card"]))
        hl.configure(image=himg, bg=_surf_of(hl, pal["card"]),
                     padx=0, pady=0, borderwidth=0)
        hl._img = himg
        hl.pack(side="left", padx=(14, 10))

        def _relogo_topbar():
            surf = _surf_of(hl, pal["card"])
            im2 = logo.surface_image(tk, TOPBAR_LOGO_PX, surf)
            hl.configure(image=im2, bg=surf)
            hl._img = im2
        kit.repaint(_relogo_topbar)
    except Exception:
        pass

    tt = tk.Frame(topbar, bg=pal["card"])
    kit.reg(tt, "card")
    tt.pack(side="left", fill="y")
    h1 = tk.Label(tt, text=APP_NAME, font=F_TOP_H1, bg=pal["card"],
                  fg=pal["text"])
    kit.reg(h1, "card_text")
    h1.pack(anchor="w", pady=(TOPBAR_TEXT_TOP, 0))

    h2 = tk.Label(tt, text=i18n.L("hdr.build", APP_VERSION),
                  font=F_TOP_LINE, bg=pal["card"], fg=pal["muted"])
    kit.reg(h2, "card_muted")
    h2.pack(anchor="w", padx=(TOPBAR_BUILD_INDENT, 0), pady=(1, 0))
    R(lambda: h2.configure(text=i18n.L("hdr.build", APP_VERSION))
      if _live(h2) else False)

    right = tk.Frame(topbar, bg=pal["card"])
    kit.reg(right, "card")
    right.pack(side="right", padx=12)

    def close_all(code):
        action["now"] = code
        try:
            ov = _ch_overlay.get("obj")
        except (NameError, AttributeError):
            ov = None
        if ov is not None:
            try:
                ov.stop()
            except Exception:
                pass
        for aid in aids:
            try:
                root.after_cancel(aid)
            except (tk.TclError, ValueError):
                pass
        try:
            root.destroy()
        except tk.TclError:
            pass

    def do_logout():
        app.request_quit()
        try:
            hook_thread.join(timeout=2.0)
        except Exception:
            pass
        close_all("logout")

    kit.switch_widget(right, kit.toggle).pack(side="left", padx=(0, 10))
    _b_logout = kit.button(right, i18n.L("btn.logout"), do_logout)
    _b_logout.pack(side="left", padx=(0, 18))
    _b_pause = kit.button(right, i18n.L("btn.pause"), lambda: post(11),
                          kind="amber")
    _b_pause.pack(side="left", padx=(0, 6))
    _b_launch = kit.button(right, i18n.L("btn.launch"), lambda: post(10),
                           kind="green")
    _b_launch.pack(side="left")
    for _b, _k in ((_b_logout, "btn.logout"), (_b_pause, "btn.pause"),
                   (_b_launch, "btn.launch")):
        R(lambda b=_b, k=_k: kit.set_text(b, i18n.L(k)) if _live(b)
          else False)

    # Fixed-width tab sidebar, with its own vertical scrolling on short
    # screens. All names come from the existing TAB_NAMES/i18n catalogue.
    workspace = tk.Frame(shell, bg=pal["bg"])
    kit.reg(workspace, "bg")
    workspace.pack(fill="both", expand=True, padx=10, pady=(0, 6))

    sidebar = tk.Frame(workspace, bg=pal["tab_bg"],
                       width=UI_SIDEBAR_WIDTH)
    kit.reg(sidebar, "tab_bg")
    sidebar.pack(side="left", fill="y", padx=(0, 9))
    sidebar.pack_propagate(False)

    sidebar_cap = tk.Label(sidebar, text=APP_NAME, font=F_TAB,
                           anchor="w", padx=13, pady=14,
                           bg=pal["tab_bg"], fg=pal["text"])
    kit.reg(sidebar_cap, "sidebar_heading")
    sidebar_cap.pack(fill="x")

    nav_host = tk.Frame(sidebar, bg=pal["tab_bg"])
    kit.reg(nav_host, "tab_bg")
    nav_host.pack(fill="both", expand=True, pady=(0, 7))
    nav_canvas = tk.Canvas(nav_host, bg=pal["tab_bg"], bd=0,
                           highlightthickness=0, takefocus=0)
    kit.reg(nav_canvas, "sidebar_canvas")
    nav_canvas.pack(side="left", fill="both", expand=True)
    # The scrollbar calls the same guarded wheel handler as the menu:
    # yview_scroll() alone can shift a short list into blank canvas space.
    nav_scroll = _SlimScrollbar(nav_host, kit, nav_canvas,
                                wheel=lambda step: _nav_scroll(step))
    nav_scroll.pack(side="right", fill="y")
    nav_canvas.configure(yscrollcommand=nav_scroll.set)
    nav_content = tk.Frame(nav_canvas, bg=pal["tab_bg"])
    kit.reg(nav_content, "tab_bg")
    nav_win = nav_canvas.create_window(0, 0, window=nav_content, anchor="nw")

    def _nav_size():
        # Requested content height is stable even when the canvas view was
        # previously scrolled away.  Do not derive it from bbox("all").
        inner_h = max(1, nav_content.winfo_reqheight())
        viewport_h = max(1, nav_canvas.winfo_height())
        return inner_h, viewport_h, max(0, inner_h - viewport_h)

    def _nav_scroll(steps):
        try:
            inner_h, viewport_h, travel = _nav_size()
            if not travel:
                nav_canvas.yview_moveto(0.0)
                return
            y = max(0.0, min(float(travel), nav_canvas.canvasy(0)))
            y = max(0.0, min(float(travel), y + int(steps) * 45))
            nav_canvas.yview_moveto(y / inner_h)
        except (ValueError, tk.TclError):
            pass

    def _nav_fit(_event=None):
        try:
            inner_h, viewport_h, travel = _nav_size()
            viewport_w = max(1, nav_canvas.winfo_width())
            nav_canvas.itemconfigure(nav_win, width=viewport_w)
            # A non-overflowing list has a region equal to its viewport:
            # no scrollable whitespace exists above or below the menu.
            nav_canvas.configure(scrollregion=(
                0, 0, viewport_w, max(inner_h, viewport_h)))
            if not travel:
                nav_canvas.yview_moveto(0.0)
                if nav_scroll.winfo_manager():
                    nav_scroll.pack_forget()
            else:
                if not nav_scroll.winfo_manager():
                    nav_scroll.pack(side="right", fill="y")
                y = max(0.0, min(float(travel), nav_canvas.canvasy(0)))
                nav_canvas.yview_moveto(y / inner_h)
        except tk.TclError:
            pass

    nav_canvas.bind("<Configure>", _nav_fit)
    nav_content.bind("<Configure>", _nav_fit)

    def _nav_wheel(event):
        d = getattr(event, "delta", 0)
        count = (-int(d / 120) if d else
                 (-1 if getattr(event, "num", 0) == 4 else 1))
        _nav_scroll(count or (-1 if d > 0 else 1))
        return "break"

    for widget in (nav_canvas, nav_content, sidebar_cap):
        widget.bind("<MouseWheel>", _nav_wheel)
        widget.bind("<Button-4>", _nav_wheel)
        widget.bind("<Button-5>", _nav_wheel)

    main_column = tk.Frame(workspace, bg=pal["card"],
                           highlightbackground=pal["entry_bd"],
                           highlightthickness=1)
    kit.reg(main_column, "panel")
    main_column.pack(side="left", fill="both", expand=True)

    tab_names = list(TAB_NAMES)
    tab_btns = {}
    pages = {}
    current = {"tab": tab_names[0]}

    content_host = tk.Frame(main_column, bg=pal["card"])
    kit.reg(content_host, "card")
    content_host.pack(fill="both", expand=True, padx=7, pady=(0, 7))

    # Each tab uses an identical-size, high-quality outline icon. Choose a
    # premade asset at the display's DPI instead of resizing one small bitmap.
    try:
        _icon_dpi = float(root.tk.call("tk", "scaling")) / (96.0 / 72.0)
    except (ValueError, tk.TclError):
        _icon_dpi = 1.0
    nav_icon_px = min((24, 30, 36), key=lambda n: abs(n - 24 * _icon_dpi))
    nav_row_px = max(42, nav_icon_px + 16)

    def show_tab(name):
        if name not in pages:
            return
        current["tab"] = name
        for tn, (surface, ico, label) in tab_btns.items():
            selected = (tn == name)
            bg = pal["accent"] if selected else pal["tab_bg"]
            fg = "#ffffff" if selected else pal["text"]
            surface.configure(bg=bg)
            ico.configure(bg=bg)
            _snappy_nav_icon(ico, tn, fg)
            label.configure(bg=bg, fg=fg, activebackground=bg,
                            activeforeground=fg,
                            font=F_TAB if selected else F_TAB_OFF)
        for tn, pg in pages.items():
            if tn == name:
                pg.pack(fill="both", expand=True)
            else:
                pg.pack_forget()

    kit.repaint(lambda: show_tab(current["tab"]))

    for tn in tab_names:
        surface = tk.Frame(nav_content, bg=pal["tab_bg"], height=nav_row_px,
                           cursor="hand2")
        surface.pack(fill="x", padx=(8, 7), pady=(2, 3))
        surface.pack_propagate(False)
        ico = tk.Canvas(surface, width=nav_icon_px, height=nav_icon_px, bd=0,
                        bg=pal["tab_bg"], highlightthickness=0,
                        takefocus=0, cursor="hand2")
        ico._snappy_icon_size = nav_icon_px
        ico.pack(side="left", padx=(12, 10))
        b = tk.Button(surface, text=i18n.L("tab." + tn), takefocus=1,
                      bd=0, font=F_TAB_OFF, anchor="w", padx=0, pady=0,
                      relief="flat", highlightthickness=0, cursor="hand2",
                      bg=pal["tab_bg"], fg=pal["text"],
                      command=lambda n=tn: show_tab(n))
        b.pack(side="left", fill="both", expand=True)
        for w in (surface, ico):
            w.bind("<Button-1>", lambda e, n=tn: show_tab(n))
        for w in (surface, ico, b):
            w.bind("<MouseWheel>", _nav_wheel)
            w.bind("<Button-4>", _nav_wheel)
            w.bind("<Button-5>", _nav_wheel)
        tab_btns[tn] = (surface, ico, b)
        pages[tn] = tk.Frame(content_host, bg=pal["card"])
        kit.reg(pages[tn], "card")
        R(lambda b=b, tn=tn: b.configure(text=i18n.L("tab." + tn))
          if _live(b) else False)

    root.after_idle(_nav_fit)

    desc_row = tk.Frame(shell, bg=pal["desc_bg"])
    kit.reg(desc_row, "desc_frame")
    desc_row.pack(fill="x", padx=(10, 12), pady=(0, 9))

    desc_var = tk.StringVar(value=i18n.L("desc.default"))

    desc_bar = tk.Label(desc_row, textvariable=desc_var, font=F_DESC,
                        bg=pal["desc_bg"], fg=pal["muted"], anchor="w",
                        justify="left", width=1, padx=9, pady=5,
                        wraplength=500)
    kit.reg(desc_bar, "desc")
    desc_bar.pack(side="left", fill="both", expand=True)

    DESC_MAX_LINES = 5

    def _desc_height(_event=None):
        try:
            w = desc_bar.winfo_width()
            if w > 80:
                want = max(70, w - 22)
                if str(desc_bar.cget("wraplength")) != str(want):
                    desc_bar.configure(wraplength=want)
                txt = desc_var.get()
                try:
                    avg = tkfont.Font(font=F_DESC).measure("0123456789") / 10.0
                    per_line = max(20, int(want / max(4.0, avg)))
                except Exception:
                    per_line = max(20, int(want * 0.13))
                lines = 0
                for para in txt.split("\n"):
                    lines += max(1, len(para) // per_line + 1)
                want_h = min(DESC_MAX_LINES, max(2, lines))
                if int(desc_bar.cget("height")) != want_h:
                    desc_bar.configure(height=want_h)
        except tk.TclError:
            pass

    desc_row.bind("<Configure>", _desc_height)
    desc_bar.bind("<Configure>", _desc_height, add="+")
    _desc_height()

    desc_src = [None]

    def set_desc(fn):
        desc_src[0] = fn
        desc_var.set(fn() if fn else i18n.L("desc.default"))
        _desc_height()

    def _redesc():
        if not _live(desc_bar):
            return False
        desc_var.set(desc_src[0]() if desc_src[0]
                     else i18n.L("desc.default"))
        _desc_height()
        return True

    R(_redesc)

    apply_btn = kit.button(desc_row, i18n.L("btn.apply"), lambda: None,
                           kind="accent", surface="desc_bg")
    R(lambda: kit.set_text(apply_btn, i18n.L("btn.apply"))
      if _live(apply_btn) else False)

    footer = tk.Frame(shell, bg=pal["bg"])
    kit.reg(footer, "bg")
    footer.pack(fill="x", padx=10, pady=(2, 4))

    status_until = [0.0]
    status_var = tk.StringVar(value="")

    def status_msg(text):
        status_var.set(text)
        status_until[0] = time.perf_counter() + 4.0

    def mark_dirty(_a=None, _b=None, _c=None):
        if suppress[0] or dirty["on"]:
            return
        dirty["on"] = True
        apply_btn.pack(side="right", padx=(6, 6), pady=3)

    def refresh_fields():
        suppress[0] = True
        try:
            for key, (kind, var) in vars_.items():
                value = cfg.get(key)
                if kind == "bool":
                    want = bool(value)
                    if bool(var.get()) != want:
                        var.set(want)
                elif kind == "map":
                    want = map_display(key, value)
                    if var.get() != want:
                        var.set(want)
                else:
                    want = str(value)
                    if var.get() != want:
                        var.set(want)
        finally:
            suppress[0] = False

    def widget_values():
        values = {}
        bad = []
        for key, (kind, var) in vars_.items():
            raw = var.get()
            if kind == "bool":
                values[key] = bool(raw)
                continue
            if kind == "map":
                value = map_value(key, raw)
                if value is None:
                    bad.append(key)
                else:
                    values[key] = value
                continue
            ok, value = _coerce_value(raw, DEFAULT_CONFIG.get(key))
            if ok:
                values[key] = value
            else:
                bad.append(key)
        return values, bad

    def _sync_from_cfg(snapshot=None):
        if dirty.get("on"):
            return
        dirty["on"] = False
        apply_btn.pack_forget()
        theme = str(cfg.get("theme", "light"))
        if theme != kit.current():
            kit.switch_to(theme)
            refresh_fields()
            dispatch_ret()
        else:
            refresh_fields()
        if snapshot:
            over = [k for k in snapshot if cfg.get(k) != snapshot[k]]
            if over:
                status_msg(i18n.L("st.ini_over", ", ".join(over)))

    def save_and_apply():
        values, bad = widget_values()
        for key, value in values.items():
            cfg.set(key, value, save=False)
        cfg.sanitize()
        snapshot = {k: cfg.get(k) for k in vars_}
        cfg.save()
        post(6)
        dirty["on"] = False
        apply_btn.pack_forget()
        try:
            root.after(350, lambda: _sync_from_cfg(snapshot))
        except Exception:
            pass
        if bad:
            status_msg(i18n.L("st.saved_bad", ", ".join(bad)))
        else:
            status_msg(i18n.L("st.saved"))

    apply_btn.configure(command=save_and_apply)

    def do_export():
        path = filedialog.asksaveasfilename(
            parent=root,
            title=i18n.L("st.export_title"),
            initialfile="snappyai-settings.json",
            defaultextension=".json",
            filetypes=[(i18n.L("st.file_settings"), "*.json")])
        if path:
            values, _bad = widget_values()
            merged = dict(cfg.values)
            merged.update(values)
            try:
                ok, text = cfg.export_to(path, values=merged)
                status_msg(text)
            except OSError as exc:
                status_msg(i18n.L("st.export_fail", exc))

    def do_import():
        path = filedialog.askopenfilename(
            parent=root,
            title=i18n.L("st.import_title"),
            filetypes=[(i18n.L("st.file_settings"), "*.json"),
                       (i18n.L("st.all_files"), "*.*")])
        if path:
            ok, text = cfg.import_from(path)
            if ok:
                cfg.save()
                post(6)
                try:
                    _sync_from_cfg()
                except Exception:
                    pass
            status_msg(text)

    def restore_defaults():
        if not messagebox.askyesno(APP_NAME, i18n.L("st.reset_confirm")):
            return
        theme = cfg.get("theme")

        cfg.values = dict(DEFAULT_CONFIG)
        cfg.values["theme"] = theme
        cfg.save()
        refresh_fields()
        post(6)
        dirty["on"] = False
        apply_btn.pack_forget()
        status_msg(i18n.L("st.defaults_restored"))

    _b_rest = kit.button(footer, i18n.L("btn.restore"), restore_defaults,
                         surface="bg")
    _b_rest.pack(side="left", padx=(4, 16))
    _b_imp = kit.button(footer, i18n.L("btn.import"), do_import,
                        surface="bg")
    _b_imp.pack(side="left", padx=(0, 6))
    _b_exp = kit.button(footer, i18n.L("btn.export"), do_export,
                        surface="bg")
    _b_exp.pack(side="left", padx=(0, 6))

    for _b, _k in ((_b_rest, "btn.restore"), (_b_imp, "btn.import"),
                   (_b_exp, "btn.export")):
        R(lambda b=_b, k=_k: kit.set_text(b, i18n.L(k)) if _live(b)
          else False)

    R(lambda: (kit.apply(), True)[1] if _live(root) else False)

    R(lambda: (refresh_fields(), True)[1] if _live(root) else False)

    light_box = tk.Frame(footer, bg=pal["bg"])
    kit.reg(light_box, "bg")
    light_box.pack(side="right", padx=(8, 4))
    run_state = tk.StringVar(value="Standby..")
    run_lbl = tk.Label(light_box, textvariable=run_state, font=F_BOLD,
                       bg=pal["bg"], fg=pal["text"])
    kit.reg(run_lbl, "bg_text")
    run_lbl.pack(side="left", padx=(0, 6))
    light = tk.Canvas(light_box, width=16, height=16,
                      highlightthickness=0, bg=pal["bg"])
    kit.reg(light, "canvas_bg")
    light.pack(side="left", pady=2)
    light_dot = light.create_oval(3, 3, 13, 13, fill=pal["amber"],
                                  outline="")

    status_row = tk.Frame(shell, bg=pal["status_bg"])
    kit.reg(status_row, "status_frame")
    status_row.pack(fill="x", side="bottom", padx=(10, 12), pady=(0, 9))

    sb = tk.Label(status_row, textvariable=status_var, font=F_DESC,
                  anchor="w", bg=pal["status_bg"], fg=pal["muted"],
                  padx=12, pady=5)
    kit.reg(sb, "status")
    sb.pack(side="left")

    reg_var = tk.StringVar(value=registration_line(identity, ent))
    reg_lbl = tk.Label(status_row, textvariable=reg_var, font=F_DESC,
                       anchor="e", bg=pal["status_bg"], fg=pal["muted"],
                       padx=12, pady=5)
    kit.reg(reg_lbl, "status")
    reg_lbl.pack(side="right")

    def _status_fit(_event=None):
        try:
            w = status_row.winfo_width()
            if w < 200:
                return
            rw = reg_lbl.winfo_reqwidth()
            if rw > int(w * 0.62):
                reg_lbl.configure(wraplength=int(w * 0.62))
                rw = int(w * 0.62)
            want = max(140, w - rw - 24)
            if str(sb.cget("wraplength")) != str(want):
                sb.configure(wraplength=want)
        except tk.TclError:
            pass

    status_row.bind("<Configure>", _status_fit)
    _status_fit()

    ranges = type(cfg)._RANGES

    def make_row(parent, key, row, column=0, card_layout=False):
        label, desc, kind, choices = META[key]
        if key not in vars_:
            cur = cfg.get(key)
            var = (tk.BooleanVar(value=bool(cur)) if kind == "bool"
                   else tk.StringVar(value=map_display(key, cur)
                                     if kind == "map" else str(cur)))
            vars_[key] = (kind, var)
            var.trace_add("write", mark_dirty)
        else:
            kind, var = vars_[key]

        cell = tk.Frame(parent, bg=pal["card"],
                        highlightbackground=pal["entry_bd"],
                        highlightthickness=1, padx=9, pady=6)
        kit.reg(cell, "panel")
        cell.grid(row=row, column=column, sticky="nsew", padx=4, pady=3)
        cell.columnconfigure(0, weight=1)

        lbl = tk.Label(cell, text=_compact_ui_label(key), font=F_BOLD,
                       bg=pal["card"], fg=pal["text"], anchor="w",
                       justify="left", wraplength=190)
        kit.reg(lbl, "card_bold")
        lbl.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        lbl.bind("<Enter>", lambda e, k=key: set_desc(
            lambda: tr_desc(k)))
        lbl.bind("<Leave>", lambda e: set_desc(None))
        R(lambda l=lbl, k=key: l.configure(text=_compact_ui_label(k))
          if _live(l) else False)

        if kind == "bool":
            w = kit.bool_switch(cell, var, word=False)
        elif kind == "map":
            pairs = map_pairs_live(key)
            if len(pairs) == 2:
                w = _MapChoiceSwitch(cell, kit, var, pairs, key=key)
            elif len(pairs) >= 3:
                w = _MapChoiceSlider(cell, kit, var, pairs, key=key)
            else:
                w = _ChoiceDropdown(cell, kit, var=var)
                w.set_pairs(pairs)
            def _revals(w=w, k=key):
                if not _live(w):
                    return False
                w.set_pairs(map_pairs_live(k))
                return True
            R(_revals)
        elif kind == "colors":
            w = tk.Frame(cell, bg=pal["card"])
            kit.reg(w, "card")
            _chip_strips[key] = (w, var)
            var.trace_add("write", lambda *a, k=key: rebuild_chips(k))
            rebuild_chips(key)
        elif kind in CAPTURE_KINDS:
            w = tk.Entry(cell, textvariable=var, width=13, font=F_ENTRY,
                         relief="solid", borderwidth=1, bg=pal["entry"],
                         fg=pal["text"], insertbackground=pal["text"],
                         highlightbackground=pal["entry_bd"],
                         highlightthickness=1)
            kit.reg(w, "entry")
            capture = capture_button(cell, kit, root, app, key, var,
                                     kind, status_msg, R, action_only=True)
            _bind_capture_entry(w, capture)
        else:
            rng = ranges.get(key)
            dflt = DEFAULT_CONFIG.get(key)
            if rng is not None and isinstance(dflt, (int, float)):
                w = _range_widget(cell, key, var, rng, dflt)
            else:
                w = tk.Entry(cell, textvariable=var, width=16, font=F_ENTRY,
                             relief="solid", borderwidth=1, bg=pal["entry"],
                             fg=pal["text"], insertbackground=pal["text"],
                             highlightbackground=pal["entry_bd"],
                             highlightthickness=1)
                kit.reg(w, "entry")
        w.grid(row=0, column=1, sticky="e")
        return cell

    def _range_widget(cell, key, var, rng, dflt):
        lo, hi = rng
        integer = not isinstance(dflt, float)
        host = tk.Frame(cell, bg=pal["card"])
        kit.reg(host, "card")
        sl = _RangeSlider(host, kit, var=var, lo=lo, hi=hi,
                          integer=integer, width=124)
        sl.pack(side="right")
        sl.bind("<Enter>", lambda e, k=key: set_desc(lambda: tr_desc(k)))
        sl.bind("<Leave>", lambda e: set_desc(None))
        val = tk.Label(host, width=6, font=F_BASE, anchor="e",
                       bg=pal["card"], fg=pal["text"])
        kit.reg(val, "card_text")
        val.pack(side="right", padx=(0, 7))
        val.bind("<Enter>", lambda e, k=key: set_desc(lambda: tr_desc(k)))
        val.bind("<Leave>", lambda e: set_desc(None))

        def _valtext(_a=None, _b=None, _c=None):
            try:
                if _live(val):
                    val.configure(text=str(var.get()))
            except tk.TclError:
                pass

        var.trace_add("write", _valtext)
        _valtext()
        return host

    _chip_strips = {}
    _OFF_WORDS = ("off", "none", "disabled", "false")
    _MAX_CHIPS = 15
    CHIP_SW, CHIP_PAD = 24, 2
    CHIP_OFF_X = "#cc2f38"

    def _token_rgb(tok):
        preset = detection.ACCENT_PRESETS.get(tok.strip().lower())
        if preset is not None:
            return tuple(int(v) for v in detection.hsv_to_rgb(*preset))
        try:
            parts = [int(p.strip()) for p in tok.split(",")]
        except ValueError:
            return None
        if len(parts) != 3:
            return None
        return tuple(min(255, max(0, p)) for p in parts)

    def _color_tokens(value):
        s = str(value or "").strip()
        if not s or s.lower() in _OFF_WORDS:
            return []
        return [t.strip() for t in s.split(";") if t.strip()]

    def rebuild_chips(key):
        entry = _chip_strips.get(key)
        if entry is None:
            return
        strip, var = entry
        if not _live(strip):
            return
        for c in strip.winfo_children():
            c.destroy()
        toks = _color_tokens(var.get())
        shown = [(i, _token_rgb(t)) for i, t in enumerate(toks)]
        shown = [(i, rgb) for i, rgb in shown if rgb is not None]
        if not shown:

            off = tk.Canvas(strip, width=CHIP_SW, height=CHIP_SW,
                            bg=pal["light_bg"], highlightthickness=1,
                            highlightbackground=pal["entry_bd"],
                            takefocus=0)
            kit.reg(off, "hl")
            off.pack(side="left", padx=CHIP_PAD)
            off.create_line(6, 6, CHIP_SW - 6, CHIP_SW - 6,
                            fill=CHIP_OFF_X, width=2)
            off.create_line(CHIP_SW - 6, 6, 6, CHIP_SW - 6,
                            fill=CHIP_OFF_X, width=2)
            off.bind("<Enter>", lambda e, k=key: set_desc(
                lambda: i18n.L("col.chip_off", tr_label(k))))
            off.bind("<Leave>", lambda e: set_desc(None))
            return
        for i, rgb in shown[:_MAX_CHIPS]:
            hx = "#%02x%02x%02x" % rgb
            chip = tk.Canvas(strip, width=CHIP_SW, height=CHIP_SW,
                             bg=hx, highlightthickness=1,
                             highlightbackground=pal["entry_bd"],
                             cursor="hand2", takefocus=0)
            kit.reg(chip, "hl")
            chip.pack(side="left", padx=CHIP_PAD)
            chip.bind("<Button-1>",
                      lambda e, k=key, idx=i: remove_chip(k, idx))
            chip.bind("<Enter>", lambda e, k=key, rr=rgb: set_desc(
                lambda: i18n.L("col.chip_desc", rr[0], rr[1], rr[2],
                               tr_label(k))))
            chip.bind("<Leave>", lambda e: set_desc(None))
        if len(shown) > _MAX_CHIPS:
            more = tk.Label(strip, text="+%d" % (len(shown) - _MAX_CHIPS),
                            font=F_DESC, bg=pal["card"], fg=pal["muted"])
            kit.reg(more, "card_muted")
            more.pack(side="left", padx=CHIP_PAD)

    def remove_chip(key, idx):
        entry = _chip_strips.get(key)
        if entry is None:
            return
        _strip, var = entry
        toks = _color_tokens(var.get())
        if not (0 <= idx < len(toks)):
            return
        rgb = _token_rgb(toks[idx]) or (0, 0, 0)
        del toks[idx]
        var.set("; ".join(toks) if toks else "off")
        status_msg(i18n.L("st.color_removed", rgb[0], rgb[1], rgb[2],
                          tr_label(key)))

    def _reword_chips():
        alive = False
        for k in list(_chip_strips):
            if _live(_chip_strips[k][0]):
                rebuild_chips(k)
                alive = True
        return alive

    R(_reword_chips)

    _FIT = []
    _FIT_DOC = []

    def fill_list(page, keys, scroll=True):
        host = page
        if scroll:
            host, bind_wheel = _scrollable(page, kit, pal)
            _FIT.append(host)
        grid = tk.Frame(host, bg=pal["card"])
        if scroll:
            bind_wheel(grid)
        kit.reg(grid, "card")
        grid.pack(fill="x", padx=5, pady=5)
        grid.columnconfigure(0, weight=1, uniform="settings")
        grid.columnconfigure(1, weight=0)
        cells = [make_row(grid, key, i, card_layout=True)
                 for i, key in enumerate(keys)]
        layout = {"columns": 1}

        def arrange_cards(event=None):
            width = event.width if event is not None else grid.winfo_width()
            cols = 2 if width >= 940 else 1
            if cols == layout["columns"]:
                return
            layout["columns"] = cols
            grid.columnconfigure(1, weight=1 if cols == 2 else 0,
                                 uniform="settings" if cols == 2 else "")
            for i, cell in enumerate(cells):
                cell.grid_configure(row=i // cols, column=i % cols)

        grid.bind("<Configure>", arrange_cards, add="+")
        return grid

    # Assistance includes the former Advanced and Experimental settings.
    # Switches (bool + two-choice maps) on the left; numeric and named
    # sliders (three+ choices) on the right. No duplicate section headings.
    _exp_screen_keys = ("accent_matcher", "capture_backend")
    _exp_assist_keys = [k for k in TAB_EXPERIMENTAL
                        if k not in _exp_screen_keys]
    _assist_all_keys = list(dict.fromkeys(TAB_ASSIST + TAB_ADV +
                                          _exp_assist_keys))
    _assist_switch_keys = [k for k in _assist_all_keys
                           if META[k][2] == "bool" or
                           (META[k][2] == "map" and len(META[k][3]) == 2)]
    _assist_slider_keys = [k for k in _assist_all_keys
                           if k not in _assist_switch_keys]
    assist_page = pages["assist"]
    assist_host, assist_wheel = _scrollable(assist_page, kit, pal)
    _FIT.append(assist_host)
    assist_grid = tk.Frame(assist_host, bg=pal["card"])
    kit.reg(assist_grid, "card")
    assist_grid.pack(fill="x", padx=6, pady=(6, 8))
    assist_wheel(assist_grid)
    assist_grid.columnconfigure(0, weight=1, uniform="assist_columns")
    assist_grid.columnconfigure(1, weight=1, uniform="assist_columns")

    assist_switch_cells = [make_row(assist_grid, key, i, column=0,
                                    card_layout=True)
                           for i, key in enumerate(_assist_switch_keys)]
    assist_slider_cells = [make_row(assist_grid, key, i, column=1,
                                    card_layout=True)
                           for i, key in enumerate(_assist_slider_keys)]

    def _fit_assist(event=None):
        width = event.width if event is not None else assist_grid.winfo_width()
        wide = width >= 820
        if wide:
            assist_grid.columnconfigure(0, weight=1,
                                        uniform="assist_columns")
            assist_grid.columnconfigure(1, weight=1,
                                        uniform="assist_columns")
            for i, cell in enumerate(assist_switch_cells):
                cell.grid_configure(row=i, column=0)
            for i, cell in enumerate(assist_slider_cells):
                cell.grid_configure(row=i, column=1)
        else:
            assist_grid.columnconfigure(0, weight=1, uniform="")
            assist_grid.columnconfigure(1, weight=0, uniform="")
            for i, cell in enumerate(assist_switch_cells):
                cell.grid_configure(row=i, column=0)
            for i, cell in enumerate(assist_slider_cells):
                cell.grid_configure(row=len(assist_switch_cells) + i, column=0)

    _assist_wide = {"value": None}
    def _fit_assist_once(event=None):
        width = event.width if event is not None else assist_grid.winfo_width()
        wide = width >= 820
        if _assist_wide["value"] != wide:
            _assist_wide["value"] = wide
            _fit_assist(event)
    assist_grid.bind("<Configure>", _fit_assist_once, add="+")
    assist_grid.after_idle(_fit_assist_once)

    try:
        build_spoofer_page(
            pages["spoofer"], kit, pal, status_msg, _scrollable,
            {"h1": F_H1, "bold": F_BOLD, "desc": F_DESC, "entry": F_ENTRY},
        )
    except Exception as _spoofer_exc:
        log("Spoofer tab failed to load: %s" % _spoofer_exc)
        _sp_error = tk.Label(
            pages["spoofer"],
            text=("The spoofing tab could not be loaded.\n\n%s" % _spoofer_exc),
            font=F_DESC, bg=pal["card"], fg=pal["accent"],
            justify="left", anchor="nw", padx=16, pady=16,
        )
        kit.reg(_sp_error, "card")
        _sp_error.pack(fill="both", expand=True)

    # Screen: compact, left-aligned preview + picker + colour actions.
    # The original picker functions and config variables remain in use.
    chp = pages["handler"]
    chinner, chbind = _scrollable(chp, kit, pal)
    _FIT.append(chinner)
    cp = tk.Frame(chinner, bg=pal["card"], highlightthickness=1,
                  highlightbackground=pal["entry_bd"])
    kit.reg(cp, "panel")
    cp.pack(fill="x", padx=10, pady=(7, 10))

    host = tk.Frame(cp, bg=pal["card"])
    kit.reg(host, "card")
    # Match the left and right edges of the setting cards below (7+4 px).
    host.pack(fill="x", padx=11, pady=(9, 4))
    host.grid_anchor("w")
    host.columnconfigure(0, weight=1)

    top_pair = tk.Frame(host, bg=pal["card"])
    kit.reg(top_pair, "card")
    top_pair.grid(row=0, column=0, sticky="ew", padx=(0, 10))
    top_pair.columnconfigure(0, weight=1)
    preview_frame = tk.Frame(top_pair, bg=pal["card"])
    kit.reg(preview_frame, "card")
    preview_frame.grid(row=0, column=0, sticky="new", padx=(0, 10))
    # The preview canvas is the same total height as the square picker, its
    # 6px separation, and the hue slider (including 1px canvas borders).
    # Internal canvas height: 200 + 6 + 22 + 2 = 230px.
    _ch_pv = tk.Canvas(preview_frame, width=230, height=230, bg=pal["entry"],
                       highlightthickness=1,
                       highlightbackground=pal["entry_bd"],
                       highlightcolor=pal["entry_bd"],
                       bd=0, relief="flat", takefocus=0)
    _ch_pv.pack(fill="x", anchor="nw")

    # The existing HSV picker appears immediately to the right of the preview.
    left = tk.Frame(top_pair, bg=pal["card"])
    kit.reg(left, "card")
    left.grid(row=0, column=1, sticky="nw")
    right = tk.Frame(host, bg=pal["card"])
    kit.reg(right, "card")
    right.grid(row=0, column=1, sticky="ne")

    SV = 200
    sv_canvas = tk.Canvas(left, width=SV, height=SV, cursor="crosshair",
                          highlightthickness=1,
                          highlightbackground=pal["entry_bd"],
                          highlightcolor=pal["entry_bd"],
                          bd=0, relief="flat",
                          bg=pal["entry"], takefocus=0)
    sv_canvas.pack(anchor="w", pady=(0, 6))
    kit.reg(sv_canvas, "hl")
    hue_canvas = tk.Canvas(left, width=SV, height=22, cursor="crosshair",
                           highlightthickness=1,
                           highlightbackground=pal["entry_bd"],
                           highlightcolor=pal["entry_bd"],
                           bd=0, relief="flat",
                           bg=pal["entry"], takefocus=0)
    hue_canvas.pack(anchor="w")
    kit.reg(hue_canvas, "hl")

    def _refresh_screen_outlines():
        # One identical outline treatment for reticle, square HSV selector,
        # and hue strip, including after light/dark theme changes.
        for canvas in (_ch_pv, sv_canvas, hue_canvas):
            if _live(canvas):
                canvas.configure(bd=0, relief="flat",
                                 highlightthickness=1,
                                 highlightbackground=pal["entry_bd"],
                                 highlightcolor=pal["entry_bd"])
        return True

    kit.repaint(_refresh_screen_outlines)

    def _match_reticle_preview_height(_event=None):
        try:
            # The picker's outer height includes both canvases and their gap.
            desired = max(80, left.winfo_reqheight() - 2)
            if int(_ch_pv.cget("height")) != desired:
                _ch_pv.configure(height=desired)
        except (tk.TclError, ValueError):
            pass

    left.bind("<Configure>", _match_reticle_preview_height, add="+")
    left.after_idle(_match_reticle_preview_height)

    def _fit_screen_picker(event=None):
        width = event.width if event is not None else host.winfo_width()
        # A three-piece row needs: right-side palette ~450, picker ~202,
        # preview >= 210, and two 10px gaps. Reflow rather than clip.
        layout = 3 if width >= 885 else (2 if width >= 450 else 1)
        if layout != getattr(host, "_screen_columns", None):
            host._screen_columns = layout
            if layout == 3:
                host.columnconfigure(0, weight=1)
                host.columnconfigure(1, weight=0)
                top_pair.grid_configure(row=0, column=0, sticky="ew",
                                        padx=(0, 10))
                top_pair.columnconfigure(0, weight=1)
                preview_frame.grid_configure(row=0, column=0,
                                             sticky="new", padx=(0, 10))
                left.grid_configure(row=0, column=1, sticky="nw", pady=0)
                right.grid_configure(row=0, column=1, sticky="ne",
                                     padx=0, pady=0)
            elif layout == 2:
                host.columnconfigure(0, weight=1)
                host.columnconfigure(1, weight=0)
                top_pair.grid_configure(row=0, column=0, sticky="ew", padx=0)
                top_pair.columnconfigure(0, weight=1)
                preview_frame.grid_configure(row=0, column=0,
                                             sticky="new", padx=(0, 10))
                left.grid_configure(row=0, column=1, sticky="nw", pady=0)
                right.grid_configure(row=1, column=0, sticky="w",
                                     padx=0, pady=(8, 0))
            else:
                host.columnconfigure(0, weight=1)
                host.columnconfigure(1, weight=0)
                top_pair.grid_configure(row=0, column=0, sticky="ew", padx=0)
                top_pair.columnconfigure(0, weight=1)
                preview_frame.grid_configure(row=0, column=0,
                                             sticky="ew", padx=0)
                left.grid_configure(row=1, column=0, sticky="nw", pady=(8, 0))
                right.grid_configure(row=1, column=0, sticky="w",
                                     padx=0, pady=(8, 0))
    host.bind("<Configure>", _fit_screen_picker, add="+")

    rgb_vars = {channel: tk.StringVar(value=initial)
                for channel, initial in (("r", "198"), ("g", "40"), ("b", "40"))}

    # The HSV picker still maintains rgb_vars for the showcase, preset swatches,
    # colour lists, and reticle colour button. RGB Feedback's redundant visual
    # row has been removed; it never supplied configuration values.
    color_rows = tk.Frame(cp, bg=pal["card"])
    kit.reg(color_rows, "card")
    color_rows.pack(fill="x", padx=7, pady=(0, 7))
    color_rows.columnconfigure(0, weight=1)

    # Preserve the original chip-list add/remove handlers and setting keys.
    make_row(color_rows, "accent_color", 0)
    make_row(color_rows, "exclude_color", 1)

    # The reticle colour is a single swatch like the other colour controls.
    # Clicking it copies the HSV picker's currently selected RGB value.
    reticle_color_var = vars_.get("reticle_color", (None, None))[1]
    if reticle_color_var is None:
        reticle_color_var = tk.StringVar(value=str(cfg.get("reticle_color") or "0,255,0"))
        vars_["reticle_color"] = ("str", reticle_color_var)
        reticle_color_var.trace_add("write", mark_dirty)
    reticle_color_cell = tk.Frame(color_rows, bg=pal["card"],
                                  highlightbackground=pal["entry_bd"],
                                  highlightthickness=1, padx=9, pady=6)
    kit.reg(reticle_color_cell, "panel")
    reticle_color_cell.grid(row=2, column=0, sticky="ew", padx=4, pady=3)
    reticle_color_cell.columnconfigure(0, weight=1)
    reticle_color_lbl = tk.Label(reticle_color_cell, text="Reticle color",
                                 font=F_BOLD, bg=pal["card"], fg=pal["text"],
                                 anchor="w")
    kit.reg(reticle_color_lbl, "card_bold")
    reticle_color_lbl.grid(row=0, column=0, sticky="w")
    reticle_color_lbl.bind("<Enter>",
                            lambda e: set_desc(lambda: tr_desc("reticle_color")))
    reticle_color_lbl.bind("<Leave>", lambda e: set_desc(None))
    reticle_color_chip_host = tk.Frame(reticle_color_cell, bg=pal["card"])
    kit.reg(reticle_color_chip_host, "card")
    reticle_color_chip_host.grid(row=0, column=1, sticky="e")
    reticle_color_chip = tk.Canvas(reticle_color_chip_host, width=CHIP_SW,
                                   height=CHIP_SW, takefocus=0,
                                   highlightthickness=1,
                                   highlightbackground=pal["entry_bd"])
    reticle_color_chip.pack(side="left", padx=CHIP_PAD)

    def paint_reticle_color(*_a):
        try:
            hx = reticle.to_hex(reticle.parse_color(reticle_color_var.get()))
        except (TypeError, ValueError):
            hx = "#00ff00"
        if _live(reticle_color_chip):
            reticle_color_chip.configure(bg=hx,
                                         highlightbackground=pal["entry_bd"])
        return True

    def set_reticle_from_picker(_event=None):
        rgb = read_rgb()
        if rgb is not None:
            reticle_color_var.set("%d,%d,%d" % rgb)
        return "break"

    reticle_color_var.trace_add("write", paint_reticle_color)
    kit.repaint(paint_reticle_color)
    paint_reticle_color()

    PRESET_SW, PRESET_PAD = 24, 2
    PRESET_SPAN = (len(COLOR_PRESETS) * (PRESET_SW + 2 + 2 * PRESET_PAD)
                   - 2 * PRESET_PAD)
    SHOW_W = PRESET_SPAN - 2
    BTN_GAP = 6
    BTN_W = (PRESET_SPAN - BTN_GAP) // 2
    # Budget the entire right-hand stack against the HSV picker height.
    # Crucially the suggested colours have a PERMANENT 26px slot, even
    # before any suggestions have been loaded. The top row must never grow
    # when that slot is populated, or the preview/picker edges will shift.
    PICKER_TOTAL_H = (SV + 2) + 6 + (22 + 2)
    SWATCH_TOTAL_H = (PRESET_SW + 2) + 8 + 8
    # Use the normal button height as the initial layout budget. Some Tk
    # builds add a further 1-2px border on top of that, so the showcase is
    # also fitted against the REAL swatch row geometry below.
    ACTION_TOTAL_H = kit.BTN_H + 6
    SUG_SLOT_H = PRESET_SW + 2
    SUG_TOTAL_H = SUG_SLOT_H + 4
    SHOW_INNER_H = max(60, PICKER_TOTAL_H - SWATCH_TOTAL_H
                       - 2 * ACTION_TOTAL_H - SUG_TOTAL_H - 2)
    prev = tk.Canvas(right, width=SHOW_W, height=SHOW_INNER_H, bg="#c62828",
                     highlightthickness=1,
                     highlightbackground=pal["entry_bd"])
    prev.pack(anchor="w", fill="x", padx=(0, PRESET_PAD), pady=0)
    kit.reg(prev, "hl")

    # Keep the original picker action inside the colour showcase. Its button
    # is now as tall as the other UI buttons and only as wide as its caption
    # needs, with the same 7px rounded corners. A canvas window lets us
    # centre it horizontally while preserving a bottom inset on resize.
    _reticle_picker_text = "Set as reticle color"
    _reticle_picker_font = tkfont.Font(font=F_BTN)
    _reticle_picker_ideal_width = _reticle_picker_font.measure(
        _reticle_picker_text) + 40  # 5px more breathing room on each side
    _b_reticle_picker = tk.Button(
        prev, text=_reticle_picker_text, command=set_reticle_from_picker,
        font=F_BTN, relief="flat", bd=0, highlightthickness=0,
        compound="center", padx=0, pady=0, takefocus=1,
        cursor="hand2")
    _reticle_picker_window = prev.create_window(
        1, 1, window=_b_reticle_picker, anchor="s", tags="reticle_action")

    def _picked_button_colors(rgb):
        # WCAG relative luminance: invert the colour's brightness to obtain
        # a neutral grayscale button, then choose readable black/white text.
        def linear(channel):
            x = channel / 255.0
            return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4

        r, g, b = rgb
        luminance = (0.2126 * linear(r) +
                     0.7152 * linear(g) + 0.0722 * linear(b))
        shade = int(round(32 + (1.0 - luminance) * 205))
        bg = "#%02x%02x%02x" % (shade, shade, shade)
        fg = "#000000" if shade >= 118 else "#ffffff"
        return bg, fg

    sw_row = tk.Frame(right, bg=pal["card"])
    kit.reg(sw_row, "card")
    sw_row.pack(anchor="w", pady=(8, 8))

    picker = {"h": 0.02, "s": 0.8, "v": 0.8, "lock": False}
    sv_photo = [None]
    hue_photo = [None]
    sv_mark = [None]
    hue_mark = [None]

    def _hsv_grid(h, size=SV):
        ys, xs = np.mgrid[0:size, 0:size].astype(np.float64)
        ss = xs / (size - 1)
        vv = 1.0 - ys / (size - 1)
        h6 = h * 6.0
        i = (np.floor(h6) % 6).astype(np.int64)
        f = h6 - np.floor(h6)
        p = vv * (1 - ss)
        q = vv * (1 - ss * f)
        t = vv * (1 - ss * (1 - f))
        r = np.choose(i, [vv, q, p, p, t, vv])
        g = np.choose(i, [t, vv, vv, q, p, p])
        b = np.choose(i, [p, p, t, vv, vv, q])
        return (np.dstack([r, g, b]) * 255).astype(np.uint8)

    def render_sv():
        if not HAVE_PIL:
            return _render_sv_fallback()
        buf = io.BytesIO()
        _PILImage.fromarray(_hsv_grid(picker["h"]), "RGB").save(
            buf, format="PNG")
        sv_photo[0] = tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"))
        sv_canvas.delete("bg")
        sv_canvas.create_image(0, 0, anchor="nw", image=sv_photo[0],
                               tags="bg")
        place_sv_mark()

    def _render_sv_fallback():
        n = 10
        cell = SV // n
        sv_canvas.delete("bg")
        h = picker["h"]
        for iy in range(n):
            v = 1.0 - (iy + 0.5) / n
            for ix in range(n):
                s = (ix + 0.5) / n
                r, g, b = colorsys.hsv_to_rgb(h, s, v)
                sv_canvas.create_rectangle(
                    ix * cell, iy * cell,
                    (ix + 1) * cell - 1, (iy + 1) * cell - 1,
                    fill="#%02x%02x%02x" % (int(r * 255), int(g * 255),
                                            int(b * 255)),
                    outline="", tags="bg")
        place_sv_mark()

    def render_hue():
        if not HAVE_PIL:
            return _render_hue_fallback()
        arr = np.zeros((22, SV, 3), dtype=np.uint8)
        for x in range(SV):
            r, g, b = colorsys.hsv_to_rgb(x / (SV - 1.0), 1.0, 1.0)
            arr[:, x] = (int(r * 255), int(g * 255), int(b * 255))
        buf = io.BytesIO()
        _PILImage.fromarray(arr, "RGB").save(buf, format="PNG")
        hue_photo[0] = tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"))

        hue_canvas.delete("bg")
        hue_canvas.create_image(0, 0, anchor="nw", image=hue_photo[0],
                                tags="bg")
        place_hue_mark()

    def _render_hue_fallback():
        n = 20
        w = SV // n
        hue_canvas.delete("bg")
        for i in range(n):
            r, g, b = colorsys.hsv_to_rgb((i + 0.5) / n, 1.0, 1.0)
            hue_canvas.create_rectangle(
                i * w, 0, (i + 1) * w - 1, 21,
                fill="#%02x%02x%02x" % (int(r * 255), int(g * 255),
                                        int(b * 255)),
                outline="", tags="bg")
        place_hue_mark()

    def place_sv_mark():
        x = picker["s"] * (SV - 1)
        y = (1.0 - picker["v"]) * (SV - 1)

        sv_canvas.delete("smark")
        sv_mark[0] = sv_canvas.create_oval(x - 5, y - 5, x + 5, y + 5,
                                           outline="#ffffff", width=2,
                                           tags="smark")

    def place_hue_mark():
        x = picker["h"] * (SV - 1)

        hue_canvas.delete("hmark")
        hue_mark[0] = hue_canvas.create_rectangle(x - 2, 0, x + 2, 21,
                                                  outline="#ffffff",
                                                  width=2, tags="hmark")

    def paint_preview(*_a):
        try:
            r = max(0, min(255, int(rgb_vars["r"].get())))
            g = max(0, min(255, int(rgb_vars["g"].get())))
            b = max(0, min(255, int(rgb_vars["b"].get())))
        except (ValueError, tk.TclError):
            return
        hx = "#%02x%02x%02x" % (r, g, b)
        try:
            prev.configure(bg=hx)
            # Never delete the embedded button when the picker changes colour.
            prev.delete("hex_value")
            lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
            w = max(1, prev.winfo_width())
            h = max(1, prev.winfo_height())
            # Natural-width, centered button with a little margin to the
            # showcase edges and bottom. The height matches ThemeKit buttons.
            bottom_gap = 7
            action_h = kit.BTN_H
            action_w = min(_reticle_picker_ideal_width, max(1, w - 20))
            action_top = max(0, h - bottom_gap - action_h)
            prev.create_text(w / 2, action_top / 2, text=hx,
                             font=F_BTN,
                             fill="#ffffff" if lum < 0.55 else "#1b1b1b",
                             tags="hex_value")
            prev.coords(_reticle_picker_window, w / 2, h - bottom_gap)
            prev.itemconfigure(_reticle_picker_window,
                               width=action_w, height=action_h)
            bg, fg = _picked_button_colors((r, g, b))
            _b_reticle_picker.configure(bg=hx, fg=fg,
                                        activebackground=hx,
                                        activeforeground=fg,
                                        disabledforeground=fg)
            if HAVE_PIL:
                # Same rounded background as the application's other buttons.
                # Retain the photo reference so Tk does not discard it.
                image = kit.rounded(action_w, action_h, bg, 7)
                _b_reticle_picker.configure(image=image)
                _b_reticle_picker._img = image
            else:
                # Without Pillow, keep the text legible (Tk has no native
                # rounded button corners) instead of hiding the control.
                _b_reticle_picker.configure(bg=bg, activebackground=bg,
                                            image="")
        except tk.TclError:
            pass

    prev.bind("<Configure>", paint_preview, add="+")
    kit.repaint(paint_preview)

    def push_to_entries():
        picker["lock"] = True
        r, g, b = colorsys.hsv_to_rgb(picker["h"], picker["s"],
                                      picker["v"])
        rgb_vars["r"].set(str(int(round(r * 255))))
        rgb_vars["g"].set(str(int(round(g * 255))))
        rgb_vars["b"].set(str(int(round(b * 255))))
        paint_preview()
        picker["lock"] = False

    def entries_changed(*_a):
        if picker["lock"]:
            return
        try:
            r = max(0, min(255, int(rgb_vars["r"].get())))
            g = max(0, min(255, int(rgb_vars["g"].get())))
            b = max(0, min(255, int(rgb_vars["b"].get())))
        except (ValueError, tk.TclError):
            return
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        picker["h"], picker["s"], picker["v"] = h, s, v
        render_sv()
        place_hue_mark()
        paint_preview()

    for _v in rgb_vars.values():
        _v.trace_add("write", entries_changed)

    def on_sv(event):
        x = max(0, min(SV - 1, event.x))
        y = max(0, min(SV - 1, event.y))
        picker["s"] = x / (SV - 1.0)
        picker["v"] = 1.0 - y / (SV - 1.0)
        place_sv_mark()
        push_to_entries()

    def on_hue(event):
        x = max(0, min(SV - 1, event.x))
        picker["h"] = x / (SV - 1.0)
        render_sv()
        place_hue_mark()
        push_to_entries()

    sv_canvas.bind("<Button-1>", on_sv)
    sv_canvas.bind("<B1-Motion>", on_sv)
    hue_canvas.bind("<Button-1>", on_hue)
    hue_canvas.bind("<B1-Motion>", on_hue)

    def set_rgb(r, g, b):
        picker["lock"] = True
        rgb_vars["r"].set(str(r))
        rgb_vars["g"].set(str(g))
        rgb_vars["b"].set(str(b))
        picker["lock"] = False
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        picker["h"], picker["s"], picker["v"] = h, s, v
        render_sv()
        place_hue_mark()
        paint_preview()

    for name, rgb in COLOR_PRESETS:
        hx = "#%02x%02x%02x" % rgb
        sw = tk.Canvas(sw_row, width=PRESET_SW, height=PRESET_SW, bg=hx,
                       highlightthickness=1,
                       highlightbackground=pal["entry_bd"],
                       cursor="hand2", takefocus=0)
        sw.pack(side="left", padx=PRESET_PAD)
        sw.bind("<Button-1>", lambda e, rr=rgb: set_rgb(*rr))
        sw.bind("<Enter>", lambda e, n=name: set_desc(
            lambda: i18n.L("st.preset_desc", n)))
        sw.bind("<Leave>", lambda e: set_desc(None))

    add_row = tk.Frame(right, bg=pal["card"])
    kit.reg(add_row, "card")
    # Place action buttons closer to the bottom of the square HSV picker.
    # Transfer 6px of the existing space below the buttons to above them.
    # The suggestion strip's top/bottom position is unchanged.
    add_row.pack(anchor="w", pady=(0, 12))
    _TARGET_KEY = {"accent": "accent_color", "exclude": "exclude_color"}
    _TARGET_NAME = {"accent": i18n.L("col.accent"),
                    "exclude": i18n.L("col.exclude")}
    R(lambda: (_TARGET_NAME.update(
        {"accent": i18n.L("col.accent"),
         "exclude": i18n.L("col.exclude")}), True)[1])

    def read_rgb():
        try:
            r = max(0, min(255, int(rgb_vars["r"].get())))
            g = max(0, min(255, int(rgb_vars["g"].get())))
            b = max(0, min(255, int(rgb_vars["b"].get())))
            return r, g, b
        except (ValueError, tk.TclError):
            return None

    def add_color(which):
        rgb = read_rgb()
        if rgb is None:
            status_msg(i18n.L("st.rgb_bad"))
            return
        key = _TARGET_KEY[which]
        var = vars_[key][1]
        cur = str(var.get() or "").strip()
        parts = ([p.strip() for p in cur.split(";") if p.strip()]
                 if cur.lower() not in ("", "off") else [])
        if len(parts) >= _MAX_CHIPS:
            status_msg(i18n.L("st.color_full", _TARGET_NAME[which]))
            return
        spec = "%d,%d,%d" % rgb
        if spec in parts:
            status_msg(i18n.L("st.color_already", rgb[0], rgb[1],
                              rgb[2], _TARGET_NAME[which]))
            return
        parts.append(spec)
        var.set("; ".join(parts))
        status_msg(i18n.L("st.color_added", rgb[0], rgb[1], rgb[2],
                          _TARGET_NAME[which]))

    add_pref_btn = kit.button(add_row, i18n.L("col.add_pref"),
                              lambda: add_color("accent"), kind="accent",
                              fixed_w=BTN_W)
    add_pref_btn.pack(side="left", padx=(PRESET_PAD, BTN_GAP // 2))
    R(lambda: kit.set_text(add_pref_btn, i18n.L("col.add_pref"))
      if _live(add_pref_btn) else False)
    add_ref_btn = kit.button(add_row, i18n.L("col.add_ref"),
                             lambda: add_color("exclude"), fixed_w=BTN_W)
    add_ref_btn.pack(side="left", padx=(BTN_GAP // 2, PRESET_PAD))
    R(lambda: kit.set_text(add_ref_btn, i18n.L("col.add_ref"))
      if _live(add_ref_btn) else False)

    auto_row = tk.Frame(right, bg=pal["card"])
    kit.reg(auto_row, "card")
    auto_row.pack(anchor="w", pady=(0, 0))

    def pick_from_screen():
        status_msg(i18n.L("st.pick_wait"))

        def cap():
            try:
                x, y = get_cursor_pos()
                img = app._capture.grab(x, y, x + 1, y + 1)
                if img is not None:
                    bb, gg, rr = [int(vv) for vv in img[0, 0]]
                    set_rgb(rr, gg, bb)
                    status_msg(i18n.L("st.pick_ok", rr, gg, bb, x, y))
                else:
                    status_msg(i18n.L("st.pick_fail"))
            except Exception as exc:
                status_msg(i18n.L("st.pick_err", exc))
        try:
            root.after(3000, cap)
        except Exception:
            pass

    pick_btn = kit.button(auto_row, i18n.L("col.pick"), pick_from_screen,
                          fixed_w=BTN_W)
    pick_btn.pack(side="left", padx=(PRESET_PAD, BTN_GAP // 2))
    R(lambda: kit.set_text(pick_btn, i18n.L("col.pick"))
      if _live(pick_btn) else False)

    SUG_N = 15
    # Reserve the swatch strip's height in both its empty and populated
    # states. Disabling geometry propagation stops asynchronously returned
    # suggestions from expanding this right column and pushing Screen down.
    sug_row = tk.Frame(right, bg=pal["card"], height=SUG_SLOT_H)
    kit.reg(sug_row, "card")
    sug_row.pack(anchor="w", fill="x", pady=(4, 0))
    sug_row.pack_propagate(False)

    def _align_suggestion_row():
        """Fit the colour showcase to the picker at the current Tk/DPI scale.

        The suggestion strip has a permanent height, so the position stays
        fixed whether it has zero or fifteen colours. Native Tk buttons can
        request a slightly different height on Windows than on other systems;
        grow the showcase by that difference instead of using a fixed guess.
        """
        try:
            if not (_live(prev) and _live(left) and _live(right)):
                return False
            # Only these two columns share a top/bottom edge in the wide layout.
            if getattr(host, "_screen_columns", 3) != 3:
                return True
            delta = left.winfo_reqheight() - right.winfo_reqheight()
            if delta and abs(delta) <= 32:
                height = max(60, int(prev.cget("height")) + delta)
                if height != int(prev.cget("height")):
                    prev.configure(height=height)
        except (tk.TclError, ValueError):
            return False
        return True

    # Run once after geometry settles, and also after native size / DPI
    # changes. Matching requested sizes avoids disturbing the selected colour
    # or recreating the suggestion swatches on each canvas redraw.
    right.bind("<Configure>",
               lambda _e: right.after_idle(_align_suggestion_row), add="+")
    left.bind("<Configure>",
              lambda _e: left.after_idle(_align_suggestion_row), add="+")
    right.after_idle(_align_suggestion_row)
    kit.repaint(lambda: (right.after_idle(_align_suggestion_row), True)[1]
                if _live(right) else False)

    def _show_suggestions(sug):
        if not _live(sug_row):
            return
        for child in sug_row.winfo_children():
            child.destroy()
        sug = list(sug or [])[:SUG_N]
        if not sug:
            status_msg(i18n.L("st.sug_none"))
            return
        for rgb, frac in sug:
            hx = "#%02x%02x%02x" % rgb
            sw = tk.Canvas(sug_row, width=PRESET_SW, height=PRESET_SW,
                           bg=hx, highlightthickness=1,
                           highlightbackground=pal["entry_bd"],
                           cursor="hand2", takefocus=0)
            kit.reg(sw, "hl")
            sw.pack(side="left", padx=PRESET_PAD)
            sw.bind("<Button-1>", lambda e, rr=rgb: (
                set_rgb(*rr),
                status_msg(i18n.L("st.sug_loaded", rr[0], rr[1],
                                  rr[2]))))
            sw.bind("<Enter>",
                    lambda e, rr=rgb, ff=frac: set_desc(
                        lambda: i18n.L("st.sug_loaded_desc",
                                       rr[0], rr[1], rr[2],
                                       int(round(ff * 100)))))
            sw.bind("<Leave>", lambda e: set_desc(None))
        status_msg(i18n.L("st.sug_ok", len(sug)))

    def suggest_from_screen():
        status_msg(i18n.L("st.sug_wait"))

        def work():

            try:

                time.sleep(3.0)
                x, y = get_cursor_pos()
                vx, vy, vw, vh = virtual_screen()
                w, h = min(640, vw), min(400, vh)
                left = max(vx, min(x - w // 2, vx + vw - w))
                top = max(vy, min(y - h // 2, vy + vh - h))
                img = app._capture.grab(left, top, left + w, top + h)
                if img is None:

                    time.sleep(0.3)
                    img = app._capture.grab(left, top, left + w,
                                            top + h)
                if img is None:
                    root.after(0, lambda: status_msg(
                        i18n.L("st.pick_fail")))
                    return
                sug = detection.suggest_accents(img, SUG_N)
                try:
                    root.after(0, lambda: _show_suggestions(sug))
                except Exception:
                    pass
            except Exception as exc:
                try:
                    root.after(0, lambda exc=exc: status_msg(
                        i18n.L("st.sug_fail", exc)))
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True,
                         name="snappy-suggest").start()

    _b_sug = kit.button(auto_row, i18n.L("col.suggest"),
                        suggest_from_screen, fixed_w=BTN_W)
    _b_sug.pack(side="left", padx=(BTN_GAP // 2, PRESET_PAD))
    R(lambda: kit.set_text(_b_sug, i18n.L("col.suggest"))
      if _live(_b_sug) else False)

    render_hue()
    render_sv()
    entries_changed()

    def _ch_val(key, default=None):
        try:
            kind, var = vars_[key]
            val = var.get()
            if kind == "map":
                got = map_value(key, val)
                return got if got is not None else default
            if kind == "bool":
                return bool(val)
            return val
        except (KeyError, tk.TclError):
            pass
        return cfg.get(key, default)

    def ch_preview():
        if not _live(_ch_pv):
            return False
        try:
            _ch_pv.configure(bg=pal["entry"],
                             highlightbackground=pal["entry_bd"])
            _ch_pv.delete("all")
            w = _ch_pv.winfo_width()
            h = _ch_pv.winfo_height()
            if w < 40 or h < 40:
                return True
            try:
                size = max(1.0, float(_ch_val("reticle_size", 24) or 24))
            except (TypeError, ValueError):
                size = 24.0
            try:
                gap = max(0.0, float(_ch_val("reticle_gap", 6) or 0))
            except (TypeError, ValueError):
                gap = 0.0
            sc = min(1.0, (min(w, h) - 16) / max(1.0, size + gap * 2 + 16))
            _ch_pv.create_line(w / 2, 0, w / 2, h, fill=pal["entry_bd"], dash=(2, 3))
            _ch_pv.create_line(0, h / 2, w, h / 2, fill=pal["entry_bd"], dash=(2, 3))
            existing = set(_ch_pv.find_all())
            reticle.render(
                _ch_pv, w / 2, h / 2, _ch_val("reticle_shape", "cross"),
                size * sc, gap * sc,
                int(float(_ch_val("reticle_thickness", 2) or 2)),
                float(_ch_val("reticle_rotation", 0) or 0),
                reticle.to_hex(reticle.parse_color(
                    _ch_val("reticle_color", "0,255,0"))))
            drawn = [item for item in _ch_pv.find_all() if item not in existing]
            if drawn:
                bounds = _ch_pv.bbox(*drawn)
                if bounds:
                    dx = w / 2.0 - (bounds[0] + bounds[2]) / 2.0
                    dy = h / 2.0 - (bounds[1] + bounds[3]) / 2.0
                    for item in drawn:
                        _ch_pv.move(item, dx, dy)

        except Exception:
            pass
        return True
    kit.repaint(ch_preview)
    _ch_pv.bind("<Configure>", lambda _event: ch_preview(), add="+")


    # Other Screen settings: switches left, numeric / named sliders right.
    # The colour lists and reticle colour were already placed above.
    _screen_reticle_keys = [k for k in TAB_RETICLE
                            if k != "reticle_color"]
    _screen_option_keys = list(dict.fromkeys(
        _screen_reticle_keys
        + [k for k in TAB_DETECT if k not in ("accent_color", "exclude_color")]
        + list(TAB_COLOR)
        + list(_exp_screen_keys)
    ))
    _screen_switch_keys = [k for k in _screen_option_keys
                           if META[k][2] == "bool" or
                           (META[k][2] == "map" and len(META[k][3]) == 2)]
    _screen_slider_keys = [k for k in _screen_option_keys
                           if k not in _screen_switch_keys]
    screen_grid = tk.Frame(cp, bg=pal["card"])
    kit.reg(screen_grid, "card")
    screen_grid.pack(fill="x", padx=7, pady=(2, 8))
    screen_grid.columnconfigure(0, weight=1, uniform="screen_columns")
    screen_grid.columnconfigure(1, weight=1, uniform="screen_columns")
    screen_switch_cells = [make_row(screen_grid, k, i, column=0,
                                    card_layout=True)
                           for i, k in enumerate(_screen_switch_keys)]
    screen_slider_cells = [make_row(screen_grid, k, i, column=1,
                                    card_layout=True)
                           for i, k in enumerate(_screen_slider_keys)]

    _screen_wide = {"value": None}
    def _fit_screen_options(event=None):
        width = event.width if event is not None else screen_grid.winfo_width()
        wide = width >= 820
        if wide == _screen_wide["value"]:
            return
        _screen_wide["value"] = wide
        if wide:
            screen_grid.columnconfigure(0, weight=1, uniform="screen_columns")
            screen_grid.columnconfigure(1, weight=1, uniform="screen_columns")
            for i, cell in enumerate(screen_switch_cells):
                cell.grid_configure(row=i, column=0)
            for i, cell in enumerate(screen_slider_cells):
                cell.grid_configure(row=i, column=1)
        else:
            screen_grid.columnconfigure(0, weight=1, uniform="")
            screen_grid.columnconfigure(1, weight=0, uniform="")
            for i, cell in enumerate(screen_switch_cells):
                cell.grid_configure(row=i, column=0)
            for i, cell in enumerate(screen_slider_cells):
                cell.grid_configure(row=len(screen_switch_cells) + i, column=0)

    screen_grid.bind("<Configure>", _fit_screen_options, add="+")
    screen_grid.after_idle(_fit_screen_options)
    for _key in TAB_RETICLE:
        try:
            vars_[_key][1].trace_add("write", lambda *_a: ch_preview())
        except (KeyError, tk.TclError):
            pass
    ch_preview()


    def mac_head(parent, key):
        h = tk.Label(parent, text=i18n.L(key), font=F_BOLD,
                     bg=pal["card"], fg=pal["text"], anchor="w")
        kit.reg(h, "card_bold")
        h.pack(fill="x", pady=(10, 6))
        R(lambda h=h, k=key: h.configure(text=i18n.L(k))
          if _live(h) else False)
        return h

    def macro_entry(parent, key, width, desc_key, kind=None):
        if key not in vars_:
            var = tk.StringVar(value=str(cfg.get(key)))
            vars_[key] = ("str", var)
            var.trace_add("write", mark_dirty)
        else:
            var = vars_[key][1]
        en = tk.Entry(parent, textvariable=var, width=width,
                      font=F_ENTRY, relief="solid", borderwidth=1,
                      bg=pal["entry"], fg=pal["text"],
                      insertbackground=pal["text"],
                      highlightbackground=pal["entry_bd"],
                      highlightthickness=1)
        kit.reg(en, "entry")
        en.bind("<Enter>", lambda e, k=desc_key: set_desc(
            lambda: tr_desc(k)))
        en.bind("<Leave>", lambda e: set_desc(None))
        if kind in CAPTURE_KINDS:
            capture = capture_button(parent, kit, root, app, key, var,
                                     kind, status_msg, R, action_only=True)
            _bind_capture_entry(en, capture)
        return en

    def macro_word(parent, key, desc_key):
        w = tk.Label(parent, text=i18n.L(key), font=F_DESC,
                     bg=pal["card"], fg=pal["muted"])
        kit.reg(w, "card_muted")
        R(lambda w=w, k=key: w.configure(text=i18n.L(k))
          if _live(w) else False)
        w.bind("<Enter>", lambda e, k=desc_key: set_desc(
            lambda: tr_desc(k)))
        w.bind("<Leave>", lambda e: set_desc(None))
        return w

    # Both device pages share a 13-row grid.  Each row has the same height in
    # both columns, so the five-row recording library starts exactly at
    # Classic mode (row 5), and the Play binding ends alongside Play #5.
    # Nothing about the configuration variables, stores or callbacks changes.
    _RECORD_FIELDS = ("key_record", "key_record_stop", "key_play")
    _PAD_RECORD_FIELDS = ("pad_record", "pad_record_stop", "pad_play")
    _KEY_LEFT_FIELDS = [k for k in TAB_KEYBOARD if k not in _RECORD_FIELDS]
    _PAD_LEFT_FIELDS = [k for k in TAB_CONTROLLER if k not in _PAD_RECORD_FIELDS]
    assert len(_KEY_LEFT_FIELDS) == 11
    assert len(_PAD_LEFT_FIELDS) == 11
    assert _KEY_LEFT_FIELDS[-1] == "key_play5"
    assert _PAD_LEFT_FIELDS[-1] == "pad_play5"

    def _binding_workspace(tab_id):
        inner, bind_wheel = _scrollable(pages[tab_id], kit, pal)
        _FIT.append(inner)
        grid = tk.Frame(inner, bg=pal["card"])
        kit.reg(grid, "card")
        grid.pack(fill="x", padx=8, pady=8)
        grid.columnconfigure(0, weight=1, uniform="binding_columns")
        grid.columnconfigure(1, weight=1, uniform="binding_columns")
        return inner, bind_wheel, grid

    kinner, kbind, kgrid = _binding_workspace("keyboard")
    keyboard_cells = [make_row(kgrid, key, row, column=0)
                      for row, key in enumerate(_KEY_LEFT_FIELDS)]

    cinner, cbind, cgrid = _binding_workspace("controller")
    controller_cells = [make_row(cgrid, key, row, column=0)
                        for row, key in enumerate(_PAD_LEFT_FIELDS)]

    # The five macro cards occupy the first five rows of the right column.
    def _macro_card(parent, index, is_pad):
        row = tk.Frame(parent, bg=pal["card"],
                       highlightbackground=pal["entry_bd"],
                       highlightthickness=1, padx=8, pady=6)
        kit.reg(row, "panel")
        row.grid(row=index - 1, column=1, sticky="nsew", padx=4, pady=3)
        row.columnconfigure(1, weight=1)
        row.columnconfigure(3, weight=1)
        prefix = "pad_macro" if is_pad else "macro"
        key = "%s%d_key" % (prefix, index)
        action = "%s%d_action" % (prefix, index)
        title_key = "mac.pad_row" if is_pad else "mac.row"
        caption = tk.Label(row, text=i18n.L(title_key, index), font=F_BOLD,
                           bg=pal["card"], fg=pal["text"], anchor="w")
        kit.reg(caption, "card_bold")
        caption.grid(row=0, column=0, sticky="w", padx=(0, 7))
        R(lambda w=caption, k=title_key, n=index:
          w.configure(text=i18n.L(k, n)) if _live(w) else False)
        caption.bind("<Enter>", lambda e, k=key: set_desc(lambda: tr_desc(k)))
        caption.bind("<Leave>", lambda e: set_desc(None))

        # Grid only affects presentation: same vars_, capture handlers and
        # input/action configuration keys are used for both macro libraries.
        binding_kind = "pad" if is_pad else "combo"
        action_kind = "padkey" if is_pad else "combo"
        macro_entry(row, key, 8, key, binding_kind).grid(
            row=0, column=1, sticky="ew", padx=(0, 7))
        macro_word(row, "mac.sends", action).grid(
            row=0, column=2, sticky="w", padx=(0, 7))
        macro_entry(row, action, 10, action, action_kind).grid(
            row=0, column=3, sticky="ew")
        return row

    keyboard_macro_cards = [_macro_card(kgrid, i, False)
                            for i in range(1, MACRO_SLOTS + 1)]
    controller_macro_cards = [_macro_card(cgrid, i, True)
                              for i in range(1, MACRO_SLOTS + 1)]

    # Recordings occupy the next five shared grid rows; the three binding
    # settings are deliberately placed *below* the recording library.
    REC_ROW_H = 26

    def _record_selection_bg(selected):
        if not selected:
            return pal["card"]
        # An accent-tinted surface (instead of light_bg) leaves the grey
        # OFF switch track visible when a recording is selected.
        return "#fae8eb" if pal["bg"] == THEMES["light"]["bg"] else "#46313b"

    def _record_action_bar(host, actions):
        """Five equal-width recording buttons, responsive to content width."""
        buttons = []
        gap = 4
        # Previous right-packed order displayed Rename ... Delete from left
        # to right. Keep that visible order and preserve every callback.
        for i, (translation_key, callback, kind) in enumerate(reversed(actions)):
            host.grid_columnconfigure(i, weight=1, uniform="record_actions")
            b = kit.button(host, i18n.L(translation_key), callback,
                           kind=kind, fixed_w=78)
            b.grid(row=0, column=i, sticky="ew", padx=gap // 2)
            R(lambda w=b, k=translation_key:
              kit.set_text(w, i18n.L(k)) if _live(w) else False)
            buttons.append(b)

        width_cache = [0]

        def _fit(event=None):
            try:
                available = host.winfo_width()
                if available < 100:
                    return
                new_width = max(68, (available - gap * len(buttons))
                                // len(buttons))
                if new_width == width_cache[0]:
                    return
                width_cache[0] = new_width
                for b in buttons:
                    b._fixed_w = new_width
                    kit.set_text(b, b.cget("text"))
            except (tk.TclError, ValueError):
                pass

        host.bind("<Configure>", _fit, add="+")
        host.after_idle(_fit)
        return buttons

    rec_box = tk.Frame(kgrid, bg=pal["card"], highlightthickness=1,
                       highlightbackground=pal["entry_bd"],
                       highlightcolor=pal["entry_bd"])
    kit.reg(rec_box, "panel")
    rec_box.grid(row=5, column=1, rowspan=5, sticky="nsew", padx=4, pady=3)

    btn_row = tk.Frame(rec_box, bg=pal["card"])
    kit.reg(btn_row, "card")
    btn_row.pack(side="bottom", fill="x", padx=8, pady=(4, 8))

    rec_rows = tk.Frame(rec_box, bg=pal["card"])
    kit.reg(rec_rows, "card")
    # Match the recording list's top and bottom breathing room.
    # The action bar has a 4px top inset; give the list area 4px above.
    rec_rows.pack(side="top", fill="both", expand=True,
                  padx=6, pady=(4, 0))
    # Pack the actual five recording rows into a vertically centered child.
    # This prevents unused height from accumulating only below Recording #5.
    rec_list = tk.Frame(rec_rows, bg=pal["card"])
    kit.reg(rec_list, "card")
    rec_list.pack(fill="x", expand=True)
    rec_rows.configure(height=REC_ROW_H * REC_PLAY_SLOTS)

    rec_ids = []
    rec_row_w = []
    rec_row_paints = []
    sel_rid = [None]
    rec_ph = [None]

    def selected_rid():
        rid = sel_rid[0]
        return rid if rid in rec_ids else None

    def paint_rows():
        sel = selected_rid()
        for r_id, _fr, ws in rec_row_w:
            on = (r_id == sel)
            bg = _record_selection_bg(on)
            for w in ws:
                try:
                    w.configure(bg=bg)
                except tk.TclError:
                    pass
            try:
                ws[1].configure(fg=pal["accent"] if on
                                else pal["text"])
            except tk.TclError:
                pass

    def select_rec(rid, _event=None):
        sel_rid[0] = rid
        app.set_play_target(rid)
        paint_rows()

    def make_rec_row(n, rec):
        rid = rec["id"]
        bg = _record_selection_bg(rid == selected_rid())
        fr = tk.Frame(rec_list, bg=bg, cursor="hand2")
        fr.pack(fill="x", pady=1)

        var = tk.BooleanVar(value=bool(rec["loop"]))
        sw = kit.bool_switch(fr, var, word=True)
        sw.pack(side="right", padx=(2, 6))
        repeat_lbl = tk.Label(fr, text=i18n.L("mac.loop") + ":",
                              font=F_DESC, bg=bg, fg=pal["muted"])
        kit.reg(repeat_lbl, "card_muted")
        repeat_lbl.pack(side="right", padx=(4, 2))
        R(lambda w=repeat_lbl: w.configure(text=i18n.L("mac.loop") + ":")
          if _live(w) else False)
        repeat_lbl.bind("<Enter>", lambda e: set_desc(
            lambda: i18n.L("st.loop_desc")))
        repeat_lbl.bind("<Leave>", lambda e: set_desc(None))

        def on_toggle(*_a):
            try:
                on = bool(var.get())
            except (tk.TclError, ValueError):
                on = False
            if app.rec_store.set_loop(rid, on):
                status_msg(i18n.L(
                    "st.rec_now", rec["name"],
                    i18n.L("st.rec_repeats") if on
                    else i18n.L("st.rec_once")))
        var.trace_add("write", on_toggle)

        st_lbl = tk.Label(fr, text=i18n.L("mac.steps", len(rec["steps"])),
                          font=F_DESC, bg=bg, fg=pal["muted"])
        st_lbl.pack(side="right", padx=(8, 0))
        num = tk.Label(fr, text="%d." % n, font=F_BOLD, width=3,
                       bg=bg, fg=pal["text"], anchor="w")
        num.pack(side="left")
        name = tk.Label(fr, text=rec["name"], font=F_BASE, bg=bg,
                        fg=pal["text"], anchor="w")
        name.pack(side="left", fill="x", expand=True, padx=(2, 6))

        for w in (fr, num, name, st_lbl):
            w.bind("<Button-1>", lambda e, r=rid: select_rec(r))
        sw_widgets = [sw] + list(sw.winfo_children())
        for w in sw_widgets:
            try:
                w.configure(bg=bg)
            except tk.TclError:
                pass
            w.bind("<Enter>", lambda e: set_desc(
                lambda: i18n.L("st.loop_desc")))
            w.bind("<Leave>", lambda e: set_desc(None))
        rec_row_w.append((rid, fr, [fr, num, name, st_lbl, repeat_lbl] + sw_widgets))

    def _ph_fit(_event=None):
        ph = rec_ph[0]
        if ph is None or not _live(ph):
            return
        try:
            want = rec_rows.winfo_width() - 20
            if want > 120 and str(ph.cget("wraplength")) != str(want):
                ph.configure(wraplength=want)
        except tk.TclError:
            pass

    rec_rows.bind("<Configure>", _ph_fit)

    def do_rename_rec():
        rid = selected_rid()
        rec = app.rec_store.get(rid) if rid is not None else None
        if rec is None:
            status_msg(i18n.L("st.select_rec") if rec_ids
                       else i18n.L("st.lib_empty"))
            return
        position = rec_ids.index(rid) + 1
        new = simpledialog.askstring(
            APP_NAME, i18n.L("st.rename_prompt", position),
            initialvalue=rec["name"], parent=root)
        if new is None:
            return
        if app.rec_store.set_name(rid, new):
            rebuild_recs()
            status_msg(i18n.L("st.renamed"))
        else:
            status_msg(i18n.L("st.name_empty"))

    def do_delete_rec():
        rid = selected_rid()
        rec = app.rec_store.get(rid) if rid is not None else None
        if rec is None:
            status_msg(i18n.L("st.select_rec") if rec_ids
                       else i18n.L("st.lib_empty"))
            return
        if not messagebox.askyesno(
                APP_NAME, i18n.L("st.delete_confirm", rec["name"]),
                parent=root):
            return
        app.rec_store.remove(rid)
        if sel_rid[0] == rid:
            sel_rid[0] = None
        rebuild_recs()
        status_msg(i18n.L("st.deleted"))

    def _safe_filename(name):
        safe = "".join(c if (c.isalnum() or c in " -_") else "_"
                       for c in str(name)).strip(" -_")
        return (safe[:40] or "recording") + ".json"

    def do_export_rec():

        rid = selected_rid()
        rec = app.rec_store.get(rid) if rid is not None else None
        if rec is None:
            status_msg(i18n.L("st.select_rec") if rec_ids
                       else i18n.L("st.lib_empty"))
            return
        path = filedialog.asksaveasfilename(
            parent=root,
            title=i18n.L("st.export_rec_title"),
            initialfile=_safe_filename(rec["name"]),
            defaultextension=".json",
            filetypes=[(i18n.L("st.file_rec"), "*.json")])
        if not path:
            return
        _ok, text = app.rec_store.export_to(path, rid)
        status_msg(text)

    def do_import_rec():
        path = filedialog.askopenfilename(
            parent=root,
            title=i18n.L("st.import_rec_title"),
            filetypes=[(i18n.L("st.file_rec"), "*.json"),
                       (i18n.L("st.all_files"), "*.*")])
        if not path:
            return
        try:
            text = app.rec_store.import_from(path)[1]
        except Exception as exc:
            text = i18n.L("rec.import_fail", exc)
        rebuild_recs()
        status_msg(text)

    rec_watch = {}

    def _store_mtime(store):
        try:
            return os.path.getmtime(store.path)
        except OSError:
            return None

    def _watch_rec_file(store, rebuild, live_widget=None):
        anchor = live_widget if live_widget is not None else rec_rows
        if not _live(anchor):
            return
        key = id(store)
        rec_watch[key] = {"mtime": _store_mtime(store), "reload": None}

        def _flush():
            st = rec_watch.get(key)
            if st is None or not _live(anchor):
                rec_watch.pop(key, None)
                return
            ok = store.reload_from_disk()
            rebuild()
            status_msg(i18n.L("st.rec_reloaded" if ok else "st.rec_bad"))

        def _check():
            st = rec_watch.get(key)
            if st is None or not _live(anchor):
                rec_watch.pop(key, None)
                return
            cur = _store_mtime(store)
            if cur != st["mtime"]:
                st["mtime"] = cur
                if st["reload"] is not None:
                    try:
                        anchor.after_cancel(st["reload"])
                    except (tk.TclError, ValueError):
                        pass
                st["reload"] = anchor.after(900, _flush)
            try:
                anchor.after(1200, _check)
            except (tk.TclError, ValueError):
                rec_watch.pop(key, None)

        anchor.after(1200, _check)

    def do_edit_rec():
        rid = selected_rid()
        rec = app.rec_store.get(rid) if rid is not None else None
        if rec is None:
            status_msg(i18n.L("st.select_rec") if rec_ids
                       else i18n.L("st.lib_empty"))
            return
        path = getattr(app.rec_store, "path", None)
        if not path or not os.path.isfile(path):
            status_msg(i18n.L("st.rec_edit_none"))
            return
        try:
            os.startfile(path)
        except Exception as exc:
            status_msg(i18n.L("st.rec_edit_fail", exc))
            return
        log("Opened the recording library for editing: %r" % (path,))
        _watch_rec_file(app.rec_store, rebuild_recs, rec_rows)

    _record_action_bar(btn_row, (
        ("mac.delete", do_delete_rec, "amber"),
        ("mac.import", do_import_rec, "plain"),
        ("mac.export", do_export_rec, "plain"),
        ("mac.edit", do_edit_rec, "plain"),
        ("mac.rename", do_rename_rec, "plain"),
    ))

    def rebuild_recs():
        if not _live(rec_rows):
            return False
        for fn in rec_row_paints:
            try:
                kit.repaints.remove(fn)
            except ValueError:
                pass
        del rec_row_paints[:]
        for _rid, fr, _ws in rec_row_w:
            try:
                fr.destroy()
            except tk.TclError:
                pass
        del rec_row_w[:]
        ph = rec_ph[0]
        if ph is not None:
            try:
                ph.destroy()
            except tk.TclError:
                pass
            rec_ph[0] = None

        rec_ids[:] = [r["id"] for r in app.rec_store.recordings]
        if selected_rid() is None:
            sel_rid[0] = None

        n0 = len(kit.repaints)
        if rec_ids:

            for n, r in enumerate(app.rec_store.recordings, 1):
                make_rec_row(n, r)
        else:
            ph = tk.Label(rec_list, text=i18n.L("mac.empty"),
                          font=F_DESC, bg=pal["card"], fg=pal["muted"],
                          anchor="w", justify="left", wraplength=360)
            kit.reg(ph, "card_muted")
            ph.pack(fill="both", expand=True, padx=10, pady=6)
            rec_ph[0] = ph
            _ph_fit()
        rec_row_paints[:] = kit.repaints[n0:]
        paint_rows()
        return True

    R(lambda: rebuild_recs() if _live(rec_rows) else False)
    kit.repaint(rebuild_recs)
    rebuild_recs()
    kit.repaint(paint_rows)


    prec_box = tk.Frame(cgrid, bg=pal["card"], highlightthickness=1,
                        highlightbackground=pal["entry_bd"],
                        highlightcolor=pal["entry_bd"])
    kit.reg(prec_box, "panel")
    prec_box.grid(row=5, column=1, rowspan=5, sticky="nsew", padx=4, pady=3)

    pbtn_row = tk.Frame(prec_box, bg=pal["card"])
    kit.reg(pbtn_row, "card")
    pbtn_row.pack(side="bottom", fill="x", padx=8, pady=(4, 8))

    prec_rows = tk.Frame(prec_box, bg=pal["card"])
    kit.reg(prec_rows, "card")
    # Match the recording list's top and bottom breathing room.
    # The action bar has a 4px top inset; give the list area 4px above.
    prec_rows.pack(side="top", fill="both", expand=True,
                  padx=6, pady=(4, 0))
    # Pack the actual five recording rows into a vertically centered child.
    # This prevents unused height from accumulating only below Recording #5.
    prec_list = tk.Frame(prec_rows, bg=pal["card"])
    kit.reg(prec_list, "card")
    prec_list.pack(fill="x", expand=True)
    prec_rows.configure(height=REC_ROW_H * REC_PLAY_SLOTS)

    prec_ids = []
    prec_row_w = []
    prec_row_paints = []
    psel_rid = [None]
    prec_ph = [None]

    def pselected_rid():
        rid = psel_rid[0]
        return rid if rid in prec_ids else None

    def ppaint_rows():
        sel = pselected_rid()
        for r_id, _fr, ws in prec_row_w:
            on = (r_id == sel)
            bg = _record_selection_bg(on)
            for w in ws:
                try:
                    w.configure(bg=bg)
                except tk.TclError:
                    pass
            try:
                ws[1].configure(fg=pal["accent"] if on
                                else pal["text"])
            except tk.TclError:
                pass

    def pselect_rec(rid, _event=None):
        psel_rid[0] = rid
        app.set_play_target_pad(rid)
        ppaint_rows()

    def pmake_rec_row(n, rec):
        rid = rec["id"]
        bg = _record_selection_bg(rid == pselected_rid())
        fr = tk.Frame(prec_list, bg=bg, cursor="hand2")
        fr.pack(fill="x", pady=1)

        var = tk.BooleanVar(value=bool(rec["loop"]))
        sw = kit.bool_switch(fr, var, word=True)
        sw.pack(side="right", padx=(2, 6))
        repeat_lbl = tk.Label(fr, text=i18n.L("mac.loop") + ":",
                              font=F_DESC, bg=bg, fg=pal["muted"])
        kit.reg(repeat_lbl, "card_muted")
        repeat_lbl.pack(side="right", padx=(4, 2))
        R(lambda w=repeat_lbl: w.configure(text=i18n.L("mac.loop") + ":")
          if _live(w) else False)
        repeat_lbl.bind("<Enter>", lambda e: set_desc(
            lambda: i18n.L("st.loop_desc")))
        repeat_lbl.bind("<Leave>", lambda e: set_desc(None))

        def on_toggle(*_a):
            try:
                on = bool(var.get())
            except (tk.TclError, ValueError):
                on = False
            if app.rec_store_pad.set_loop(rid, on):
                status_msg(i18n.L(
                    "st.rec_now", rec["name"],
                    i18n.L("st.rec_repeats") if on
                    else i18n.L("st.rec_once")))
        var.trace_add("write", on_toggle)

        st_lbl = tk.Label(fr, text=i18n.L("mac.steps", len(rec["steps"])),
                          font=F_DESC, bg=bg, fg=pal["muted"])
        st_lbl.pack(side="right", padx=(8, 0))
        num = tk.Label(fr, text="%d." % n, font=F_BOLD, width=3,
                       bg=bg, fg=pal["text"], anchor="w")
        num.pack(side="left")
        name = tk.Label(fr, text=rec["name"], font=F_BASE, bg=bg,
                        fg=pal["text"], anchor="w")
        name.pack(side="left", fill="x", expand=True, padx=(2, 6))

        for w in (fr, num, name, st_lbl):
            w.bind("<Button-1>", lambda e, r=rid: pselect_rec(r))
        sw_widgets = [sw] + list(sw.winfo_children())
        for w in sw_widgets:
            try:
                w.configure(bg=bg)
            except tk.TclError:
                pass
            w.bind("<Enter>", lambda e: set_desc(
                lambda: i18n.L("st.loop_desc")))
            w.bind("<Leave>", lambda e: set_desc(None))
        prec_row_w.append((rid, fr, [fr, num, name, st_lbl, repeat_lbl] + sw_widgets))

    def _pph_fit(_event=None):
        ph = prec_ph[0]
        if ph is None or not _live(ph):
            return
        try:
            want = prec_rows.winfo_width() - 20
            if want > 120 and str(ph.cget("wraplength")) != str(want):
                ph.configure(wraplength=want)
        except tk.TclError:
            pass

    prec_rows.bind("<Configure>", _pph_fit)

    def pdo_rename_rec():
        rid = pselected_rid()
        rec = app.rec_store_pad.get(rid) if rid is not None else None
        if rec is None:
            status_msg(i18n.L("st.select_rec") if prec_ids
                       else i18n.L("st.lib_empty"))
            return
        position = prec_ids.index(rid) + 1
        new = simpledialog.askstring(
            APP_NAME, i18n.L("st.rename_prompt", position),
            initialvalue=rec["name"], parent=root)
        if new is None:
            return
        if app.rec_store_pad.set_name(rid, new):
            rebuild_precs()
            status_msg(i18n.L("st.renamed"))
        else:
            status_msg(i18n.L("st.name_empty"))

    def pdo_delete_rec():
        rid = pselected_rid()
        rec = app.rec_store_pad.get(rid) if rid is not None else None
        if rec is None:
            status_msg(i18n.L("st.select_rec") if prec_ids
                       else i18n.L("st.lib_empty"))
            return
        if not messagebox.askyesno(
                APP_NAME, i18n.L("st.delete_confirm", rec["name"]),
                parent=root):
            return
        app.rec_store_pad.remove(rid)
        if psel_rid[0] == rid:
            psel_rid[0] = None
        rebuild_precs()
        status_msg(i18n.L("st.deleted"))

    def pdo_export_rec():
        rid = pselected_rid()
        rec = app.rec_store_pad.get(rid) if rid is not None else None
        if rec is None:
            status_msg(i18n.L("st.select_rec") if prec_ids
                       else i18n.L("st.lib_empty"))
            return
        path = filedialog.asksaveasfilename(
            parent=root,
            title=i18n.L("st.export_rec_title"),
            initialfile=_safe_filename(rec["name"]),
            defaultextension=".json",
            filetypes=[(i18n.L("st.file_rec"), "*.json")])
        if not path:
            return
        _ok, text = app.rec_store_pad.export_to(path, rid)
        status_msg(text)

    def pdo_import_rec():
        path = filedialog.askopenfilename(
            parent=root,
            title=i18n.L("st.import_rec_title"),
            filetypes=[(i18n.L("st.file_rec"), "*.json"),
                       (i18n.L("st.all_files"), "*.*")])
        if not path:
            return
        try:
            text = app.rec_store_pad.import_from(path)[1]
        except Exception as exc:
            text = i18n.L("rec.import_fail", exc)
        rebuild_precs()
        status_msg(text)

    def pdo_edit_rec():
        rid = pselected_rid()
        rec = app.rec_store_pad.get(rid) if rid is not None else None
        if rec is None:
            status_msg(i18n.L("st.select_rec") if prec_ids
                       else i18n.L("st.lib_empty"))
            return
        path = getattr(app.rec_store_pad, "path", None)
        if not path or not os.path.isfile(path):
            status_msg(i18n.L("st.rec_edit_none"))
            return
        try:
            os.startfile(path)
        except Exception as exc:
            status_msg(i18n.L("st.rec_edit_fail", exc))
            return
        log("Opened the controller recording library for editing: %r"
            % (path,))
        _watch_rec_file(app.rec_store_pad, rebuild_precs, prec_rows)

    _record_action_bar(pbtn_row, (
        ("mac.delete", pdo_delete_rec, "amber"),
        ("mac.import", pdo_import_rec, "plain"),
        ("mac.export", pdo_export_rec, "plain"),
        ("mac.edit", pdo_edit_rec, "plain"),
        ("mac.rename", pdo_rename_rec, "plain"),
    ))

    def rebuild_precs():
        if not _live(prec_rows):
            return False
        for fn in prec_row_paints:
            try:
                kit.repaints.remove(fn)
            except ValueError:
                pass
        del prec_row_paints[:]
        for _rid, fr, _ws in prec_row_w:
            try:
                fr.destroy()
            except tk.TclError:
                pass
        del prec_row_w[:]
        ph = prec_ph[0]
        if ph is not None:
            try:
                ph.destroy()
            except tk.TclError:
                pass
            prec_ph[0] = None

        prec_ids[:] = [r["id"] for r in app.rec_store_pad.recordings]
        if pselected_rid() is None:
            psel_rid[0] = None

        n0 = len(kit.repaints)
        if prec_ids:
            for n, r in enumerate(app.rec_store_pad.recordings, 1):
                pmake_rec_row(n, r)
        else:
            ph = tk.Label(prec_list, text=i18n.L("mac.empty"),
                          font=F_DESC, bg=pal["card"], fg=pal["muted"],
                          anchor="w", justify="left", wraplength=360)
            kit.reg(ph, "card_muted")
            ph.pack(fill="both", expand=True, padx=10, pady=6)
            prec_ph[0] = ph
            _pph_fit()
        prec_row_paints[:] = kit.repaints[n0:]
        ppaint_rows()
        return True

    R(lambda: rebuild_precs() if _live(prec_rows) else False)
    kit.repaint(rebuild_precs)
    rebuild_precs()
    kit.repaint(ppaint_rows)

    keyboard_record_cells = [make_row(kgrid, key, 10 + pos, column=1)
                             for pos, key in enumerate(_RECORD_FIELDS)]
    controller_record_cells = [make_row(cgrid, key, 10 + pos, column=1)
                               for pos, key in enumerate(_PAD_RECORD_FIELDS)]

    def _fit_binding_columns(grid, left_cells, macro_cards, record_box,
                             record_cells):
        """Keep 1:1 row alignment on desktop; stack gracefully if cramped."""
        state = {"two": None}
        def fit(event=None):
            if not _live(grid):
                return
            width = event.width if event is not None else grid.winfo_width()
            two = width >= 950
            if state["two"] is two:
                return
            state["two"] = two
            # Move spanned widget out first, avoiding overlapping grid slaves.
            record_box.grid_remove()
            if two:
                grid.columnconfigure(0, weight=1, uniform="binding_columns")
                grid.columnconfigure(1, weight=1, uniform="binding_columns")
                for i, cell in enumerate(left_cells):
                    cell.grid_configure(row=i, column=0)
                for i, cell in enumerate(macro_cards):
                    cell.grid_configure(row=i, column=1)
                for i, cell in enumerate(record_cells):
                    cell.grid_configure(row=10 + i, column=1)
                record_box.grid(row=5, column=1, rowspan=5,
                                sticky="nsew", padx=4, pady=3)
            else:
                grid.columnconfigure(0, weight=1, uniform="")
                grid.columnconfigure(1, weight=0, uniform="")
                for i, cell in enumerate(left_cells):
                    cell.grid_configure(row=i, column=0)
                for i, cell in enumerate(macro_cards):
                    cell.grid_configure(row=13 + i, column=0)
                for i, cell in enumerate(record_cells):
                    cell.grid_configure(row=23 + i, column=0)
                record_box.grid(row=18, column=0, rowspan=5,
                                sticky="nsew", padx=4, pady=3)
        grid.bind("<Configure>", fit, add="+")
        grid.after_idle(fit)

    _fit_binding_columns(kgrid, keyboard_cells, keyboard_macro_cards,
                         rec_box, keyboard_record_cells)
    _fit_binding_columns(cgrid, controller_cells, controller_macro_cards,
                         prec_box, controller_record_cells)

    latency_pages = dict(pages)
    for _lat_name in LATENCY_TABS:
        _lat_panel = tk.Frame(pages[_lat_name], bg=pal["card"],
                              highlightbackground=pal["entry_bd"],
                              highlightthickness=1)
        kit.reg(_lat_panel, "panel")
        _lat_panel.pack(fill="both", expand=True, padx=6, pady=8)
        latency_pages[_lat_name] = _lat_panel

    lat_handle = mount(
        root, latency_pages, kit, pal,
        {"base": F_BASE, "bold": F_BOLD, "h1": F_H1, "entry": F_ENTRY},
        out_root=None,
        backend="auto",
        cfg=cfg,
    )
    root.after(POLL_MS,
               lambda: pump(root, lat_handle))
    def _on_root_destroy(e):
        if e.widget is root:
            shutdown(lat_handle)
    root.bind("<Destroy>", _on_root_destroy, add="+")

    _FIT_DOC.append(_fill_help(pages["help"], kit, pal))

    poll_tick = [0]
    rec_ui = [None]
    rec_ui[0] = (app.rec_store.revision,
                 getattr(app.rec_store_pad, "revision", 0),
                 app.is_recording(), app.rec_source(),
                 app._play_busy, app._play_looping, app._play_rid,
                 app._play_source)

    def poll_status():
        try:
            if not root.winfo_exists():
                return
            running = app.running
            run_state.set(i18n.L("st.running") if running
                          else i18n.L("st.standby"))
            light.itemconfigure(light_dot,
                                fill=(pal["green"] if running
                                      else pal["amber"]))
            try:
                _ov = _ch_overlay.get("obj")
            except (NameError, AttributeError):
                _ov = None
            if _ov is not None:
                _dv = getattr(app, "_debug_vis", None)
                if _dv is not None:
                    try:
                        _ov.set_debug(*_dv)
                    except Exception:
                        pass
                else:
                    try:
                        _ov.clear_debug()
                    except Exception:
                        pass
            poll_tick[0] += 1
            if poll_tick[0] % 4 == 0 and not dirty["on"]:
                _sync_from_cfg()
            state_now = (app.rec_store.revision,
                         getattr(app.rec_store_pad, "revision", 0),
                         app.is_recording(), app.rec_source(),
                         app._play_busy, app._play_looping, app._play_rid,
                         app._play_source)
            if rec_ui[0] != state_now:
                rec_ui[0] = state_now
                rebuild_recs()
                rebuild_precs()
            if time.perf_counter() > status_until[0]:
                mem = app.follow
                follow = ""
                if mem.when > 0:
                    follow = i18n.L("st.follow",
                                    int(time.perf_counter() - mem.when))
                _th = str(cfg.get("theme"))
                status_var.set(i18n.L(
                    "st.line", app.snap_count, app.pass_count, follow,
                    i18n.L("theme." + _th) if _th in ("light", "dark")
                    else _th))
        except Exception as exc:
            try:
                log("status poll error: %r" % (exc,))
            except Exception:
                pass
        finally:
            try:
                after_ms(500, poll_status)
            except Exception:
                pass

    shared_win = {"win": None}

    def close_shared_dialog():
        win = shared_win["win"]
        shared_win["win"] = None
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass

    def open_shared_dialog():
        if shared_win["win"] is not None:
            return
        win = tk.Toplevel(root)
        shared_win["win"] = win
        kit.reg(win, "card")
        win.title("%s - %s" % (APP_NAME, i18n.L("shared.title")))
        win.configure(background=pal["card"])
        win.resizable(False, False)
        win.transient(root)
        try:
            win.grab_set()
        except tk.TclError:
            pass
        W2, H2 = 560, 400
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry("%dx%d+%d+%d" % (W2, H2, max(0, (sw - W2) // 2),
                                      max(0, (sh - H2) // 3)))

        box = tk.Frame(win, bg=pal["card"])
        kit.reg(box, "card")
        box.place(relx=0.5, rely=0.5, anchor="center")
        t1 = tk.Label(box, text=i18n.L("shared.title"),
                      font=F_H1, bg=pal["card"], fg=pal["text"])
        kit.reg(t1, "card_text")
        t1.pack(pady=(14, 4))
        t2 = tk.Label(box, text=i18n.L("shared.body"),
                      font=F_DESC, bg=pal["card"], fg=pal["muted"],
                      justify="center", wraplength=440)
        kit.reg(t2, "card_muted")
        t2.pack(pady=(0, 12))

        def recheck():
            def worker():
                try:
                    rows, gen = online.fetch_entitlements(cfg)
                except online.OnlineError:
                    try:
                        root.after(0, lambda: status_msg(
                            i18n.L("shared.unreachable")))
                    except Exception:
                        pass
                    return
                idh = identity_hash[0]
                shared = online.row_shared(rows, idh, gen)
                try:
                    root.after(0, lambda: finish(not shared))
                except Exception:
                    pass
            threading.Thread(target=worker, daemon=True,
                             name="shared-recheck").start()

        def finish(clean):
            if clean:
                close_shared_dialog()
                status_msg(i18n.L("shared.back"))
            else:
                status_msg(i18n.L("shared.still"))
        _s_rc = kit.button(box, i18n.L("shared.recheck"), recheck,
                           kind="green", big=True)
        _s_rc.pack(pady=(0, 8))
        _s_cl = kit.button(box, i18n.L("shared.close"), close_shared_dialog)
        _s_cl.pack(pady=(0, 14))

        def _shared_retext():
            if not _live(win):
                return False
            win.title("%s - %s" % (APP_NAME, i18n.L("shared.title")))
            t1.configure(text=i18n.L("shared.title"))
            t2.configure(text=i18n.L("shared.body"))
            kit.set_text(_s_rc, i18n.L("shared.recheck"))
            kit.set_text(_s_cl, i18n.L("shared.close"))
            return True

        R(_shared_retext)

        win.update_idletasks()
        need2 = box.winfo_reqheight() + 40
        if need2 > H2:
            H2 = need2
            win.geometry("%dx%d+%d+%d"
                         % (W2, H2, max(0, (sw - W2) // 2),
                            max(0, (sh - H2) // 3)))

        win.protocol("WM_DELETE_WINDOW", close_shared_dialog)

    identity_hash = [None]
    if identity:
        identity_hash[0] = online.row_hash(identity)

    def presence_watch():
        try:
            alive = root.winfo_exists()
        except tk.TclError:
            alive = False
        if not alive:
            return
        def worker():
            if not identity_hash[0]:
                return
            try:
                rows, gen = online.fetch_entitlements(cfg)
            except online.OnlineError:
                return
            shared = online.row_shared(rows, identity_hash[0], gen)
            try:
                root.after(0, lambda: apply_shared(shared))
            except Exception:
                pass
        def apply_shared(shared):
            if not shared:
                return
            if app.running:
                post(11)
                log(i18n.L("st.shared_paused"))
            open_shared_dialog()
        threading.Thread(target=worker, daemon=True,
                         name="presence-watch").start()
        after_ms(max(1, online.presence_interval_seconds()) * 1000,
                 presence_watch)

    after_ms(max(1, online.presence_interval_seconds()) * 1000,
             presence_watch)

    def watchdog():
        try:
            alive = root.winfo_exists()
        except tk.TclError:
            alive = False
        if not alive:
            return
        if app.stop.is_set():
            try:
                ov = _ch_overlay.get("obj")
            except (NameError, AttributeError):
                ov = None
            if ov is not None:
                try:
                    ov.stop()
                except Exception:
                    pass
            if root.winfo_exists():
                try:
                    root.destroy()
                except tk.TclError:
                    pass
            return
        after_ms(250, watchdog)

    def entitlement_watch():
        def worker():

            try:
                ident, ent_now = online.me(cfg)
            except online.OnlineError:
                log(i18n.L("st.ent_unreachable"))
                return

            if ident:
                try:
                    root.after(0, lambda i=ident, e=ent_now:
                               reg_var.set(registration_line(i, e)))
                except tk.TclError:
                    return
            active = bool(ident) and online.entitlement_active(ent_now)
            if active:
                return
            state = str((ent_now or {}).get("status", "inactive"))
            if app.running:
                post(11)

            try:
                root.after(0, lambda s=state: status_msg(
                    i18n.L("st.ent_paused", s)))
            except tk.TclError:
                return
            log("Entitlement re-check: %s - paused assistance." % state)
        if root.winfo_exists():
            threading.Thread(target=worker, daemon=True,
                             name="entitlement-watch").start()
            after_ms(check_ms(), entitlement_watch)

    def check_ms():
        return ENTITLEMENT_CHECK_MINUTES * 60000

    after_ms(check_ms(), entitlement_watch)

    root.protocol("WM_DELETE_WINDOW",
                  lambda: (app.request_quit(), close_all("quit")))

    def _fit_window():
        try:
            root.update_idletasks()
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            w, h = root.winfo_width(), root.winfo_height()
            root.geometry("+%d+%d" % (max(0, (sw - w) // 2),
                                      max(0, (sh - h) // 2)))
        except tk.TclError:
            pass

    show_tab(tab_names[0])
    refresh_fields()
    _ch_overlay = {"obj": None}
    try:
        _ch_overlay["obj"] = reticle.ReticleOverlay(root, cfg)
        _ch_overlay["obj"].start()
    except Exception as exc:
        log("Reticle overlay unavailable (%r)." % (exc,))
    _fit_window()
    _fit_window()
    try:
        kit.apply()
    except Exception:
        pass
    poll_status()
    watchdog()
    log("GUI ready - signed in as %s."
        % online.identity_label(identity))
    root.deiconify()
    root.mainloop()

    return action["now"]

def _gate_window(cfg, kit, identity, ent):
    pal = kit.pal
    result = {}
    gaids = []
    W, H = 620, 500

    root = tk.Tk()
    kit.attach(root)
    kit.reg(root, "card")
    root.title(i18n.L("gate.title", APP_NAME))
    root.configure(background=pal["card"])
    root.resizable(False, False)
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry("%dx%d+%d+%d" % (W, H, max(0, (sw - W) // 2),
                                   max(0, (sh - H) // 3)))
    try:
        root.iconbitmap(logo.ico_path())
    except Exception:
        pass
    kit.defocus_hook(root)
    kit.titlebar_hook(root)

    box = tk.Frame(root, bg=pal["card"])
    kit.reg(box, "card")
    box.place(relx=0.5, rely=0.5, anchor="center")

    def refit():
        nonlocal W, H
        try:
            root.update_idletasks()
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            W = max(620, box.winfo_reqwidth() + 24)
            H = max(500, box.winfo_reqheight() + 16)
            root.geometry("%dx%d+%d+%d" % (W, H,
                                           max(0, (sw - W) // 2),
                                           max(0, (sh - H) // 3)))
        except Exception:
            pass

    lg = tk.Label(box, bg=pal["card"])
    kit.reg(lg, "card")
    lim = logo.surface_image(tk, 96, _surf_of(lg, pal["card"]))
    lg.configure(image=lim, bg=_surf_of(lg, pal["card"]))
    lg._img = lim
    lg.pack(pady=(14, 0))

    def _relogo_gate():
        surf = _surf_of(lg, pal["card"])
        im2 = logo.surface_image(tk, 96, surf)
        lg.configure(image=im2, bg=surf)
        lg._img = im2
    kit.repaint(_relogo_gate)

    t = tk.Label(box, text=APP_NAME, font=F_H1, bg=pal["card"],
                 fg=pal["text"])
    kit.reg(t, "card_text")
    t.pack(pady=(2, 2))

    def title_for(ent_now):
        ent_now = ent_now or {}
        state = str(ent_now.get("status", "unsubscribed"))
        try:
            until = float(ent_now.get("until") or 0)
        except (TypeError, ValueError):
            until = 0.0
        if ent_now.get("shared"):
            return i18n.L("gate.title.shared")
        if state == "refunded":
            return i18n.L("gate.title.refunded")
        if state == "canceling":
            return i18n.L("gate.title.canceling",
                          time.strftime("%Y-%m-%d", time.gmtime(until)))
        if state == "past_due":
            return i18n.L("gate.title.past_due")
        if state == "canceled":
            return i18n.L("gate.title.canceled")
        if until and until <= time.time():
            return i18n.L("gate.title.expired")
        return i18n.L("gate.title.none")

    h1 = tk.Label(box, text=title_for(ent), font=F_BOLD, bg=pal["card"],
                  fg=pal["text"], wraplength=460, justify="center")
    kit.reg(h1, "card_bold")
    h1.pack(pady=(6, 2))
    h2 = tk.Label(box, text=i18n.L("gate.prices"),
                  font=F_DESC, bg=pal["card"], fg=pal["muted"],
                  wraplength=460, justify="center")
    kit.reg(h2, "card_muted")
    h2.pack(pady=(0, 10))

    who = tk.Label(box, text=online.identity_label(identity),
                   font=F_DESC, bg=pal["card"], fg=pal["muted"])
    kit.reg(who, "card_muted")
    who.pack(pady=(0, 4))
    sum_lbl = tk.Label(box, text=online.entitlement_summary(ent),
                       font=F_DESC, bg=pal["card"], fg=pal["amber"],
                       wraplength=460, justify="center")
    kit.reg(sum_lbl, "card_muted")
    sum_lbl.pack(pady=(0, 10))

    msg = tk.StringVar(value="")
    msg_lbl = tk.Label(box, textvariable=msg, font=F_DESC,
                       bg=pal["card"], fg=pal["accent"], wraplength=460,
                       justify="center")
    msg_kind = ["err"]

    def _remsg():
        msg_lbl.configure(fg=(pal["green"] if msg_kind[0] == "ok"
                              else pal["accent"]))
    kit.repaint(_remsg)

    def set_msg(text, ok=False):
        msg_kind[0] = "ok" if ok else "err"
        msg_lbl.configure(fg=(pal["green"] if ok else pal["accent"]))
        msg.set(text)

    def do_subscribe(kind="monthly"):
        url = online.subscribe_url(cfg, identity, kind)
        if not url:
            set_msg(i18n.L("gate.msg.no_link"))
            return
        if online.open_subscribe(cfg, identity, kind):
            set_msg(i18n.L("gate.msg.open_ok"))
        else:
            set_msg(i18n.L("gate.msg.open_fail", url))

    def do_recheck(_event=None, silent=True):
        def worker():
            if not silent:
                online.nudge_publisher(cfg)
            try:

                ident, ent_now = online.me(cfg, fresh=False)
                err = None
            except online.OnlineError as exc:
                ident, ent_now, err = None, None, str(exc)
            try:
                root.after(0, lambda: checked(ident, ent_now, err))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True,
                         name="entitlement-recheck").start()

    def checked(ident, ent_now, err):
        if err:
            set_msg(i18n.L("gate.msg.unreachable", err))
            return
        if ident is None:
            set_msg(i18n.L("gate.msg.session_gone"))
            root.after(600, root.destroy)
            return
        ent_cur[0] = ent_now
        h1.configure(text=title_for(ent_now))
        sum_lbl.configure(text=online.entitlement_summary(ent_now))

        if online.entitlement_active(ent_now) and not ent_now.get("shared"):
            result["ok"] = True
            set_msg(i18n.L("gate.msg.active"), ok=True)
            for aid in gaids:
                try:
                    root.after_cancel(aid)
                except (tk.TclError, ValueError):
                    pass
            root.after(350, root.destroy)
        elif ent_now.get("shared"):
            set_msg(i18n.L("gate.msg.still_shared"))
        else:
            set_msg(i18n.L("gate.msg.still_none"))

    msg_lbl.pack(pady=(0, 10))
    ent_cur = [ent]

    def _gate_retext():
        if not _live(box):
            return False
        h1.configure(text=title_for(ent_cur[0]))
        h2.configure(text=i18n.L("gate.prices"))
        note.configure(text=i18n.L("gate.note"))
        kit.set_text(sub_btn, i18n.L("gate.btn.monthly"))
        kit.set_text(tri_btn, i18n.L("gate.btn.trial"))
        refit()
        return True

    pay_btns = []
    sub_btn = kit.button(box, i18n.L("gate.btn.monthly"),
                         lambda: do_subscribe("monthly"),
                         kind="accent", big=True)
    sub_btn.pack(pady=(0, 6))
    pay_btns.append(sub_btn)

    tri_btn = kit.button(box, i18n.L("gate.btn.trial"),
                         lambda: do_subscribe("trial"),
                         kind="green", big=True)
    tri_btn.pack(pady=(0, 6))
    pay_btns.append(tri_btn)

    root.update_idletasks()
    bw = max([b.winfo_width() for b in pay_btns] + [300])
    for b in pay_btns:
        meta = getattr(b, "_rmeta", None)
        if meta and HAVE_PIL and bw > 10:
            text, big, surface, _w, h, role = meta
            bg, _fg = kit._btn_colors(role)
            try:
                img = kit.rounded(bw, h, bg, 7)
                b.configure(image=img)
                b._img = img
            except Exception:
                pass

    note = tk.Label(box, text=i18n.L("gate.note"),
                    font=F_DESC, bg=pal["card"], fg=pal["muted"],
                    wraplength=460, justify="center")
    kit.reg(note, "card_muted")
    note.pack(pady=(4, 14))

    refit()

    R(_gate_retext)

    def auto_loop():
        if result.get("ok") or not root.winfo_exists():
            return
        do_recheck(silent=True)

        gaids.append(root.after(30000, auto_loop))
        if len(gaids) > 32:
            del gaids[:len(gaids) - 32]

    gaids.append(root.after(30000, auto_loop))
    root.bind("<Return>", lambda _e: do_recheck(silent=False))
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return bool(result.get("ok"))

def run_gui(cfg):
    i18n.set_language("en")
    kit = ThemeKit(cfg)
    force_login = False

    def server_error(exc):
        try:
            r = tk.Tk()
            r.withdraw()
            again = messagebox.askretrycancel(
                APP_NAME,
                i18n.L("err.auth_unreachable", exc))
            r.destroy()
            return bool(again)
        except Exception:
            return False

    while True:
        identity = None
        ent = None
        sess = online.load_session()

        if not force_login and sess and sess.get("provider"):
            try:
                identity, ent = online.me(cfg, fresh=False)
            except online.OnlineError as exc:
                log("Auth server unreachable at start: %s" % exc)
                if not server_error(exc):
                    return 1
                continue
            if identity is None:
                online.clear_session()

        if identity is None:
            identity = _login_window(cfg, kit)
            if identity is None:
                return 0

            try:
                identity, ent = online.me(cfg, fresh=False)
            except online.OnlineError as exc:
                log("Entitlement check after sign-in failed: %s" % exc)
                if not server_error(exc):
                    return 1
                continue
            if identity is None:
                continue
        force_login = False

        if not online.entitlement_active(ent) or ent.get("shared"):
            if not _gate_window(cfg, kit, identity, ent):
                force_login = True
                continue

        app, hook_thread = start_app(cfg, identity)
        if app is None:
            try:
                r = tk.Tk()
                r.withdraw()
                messagebox.showerror(APP_NAME,
                                     i18n.L("err.start_fail"))
                r.destroy()
            except Exception:
                pass
            return 1

        action = _run_main(cfg, identity, app, hook_thread, kit, ent)

        if not app.stop.is_set():
            app.request_quit()
        try:
            hook_thread.join(timeout=2.0)
        except Exception:
            pass

        if action == "logout":
            online.clear_session()
            force_login = True
            log("Signed out.")
            continue
        return 0


# ======================================================================
# merged from lattool_gui.py
# ======================================================================

import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from scanner import (
    CONSENT_TEXT,
    EventType,
    GuiController,
    PlainFinding,
    apply_confirm_text,
    classify_scan,
    issues_message,
    manual_instructions,
    run_confirm_text,
    undo_confirm_text,
)
from input import DEFAULT_THEME, Theme

APP_TITLE = "latency"
APP_SUBTITLE = "finds what is actually slowing your game, and fixes what it can"
WATCH_SECONDS = 20.0

CHAIN_TILE_LABELS = {
    "input": "INPUT (p50 one-way)",
    "render": "RENDER (p95)",
    "network": "NETWORK (p95)",
}

_FALLBACK_STYLE = {
    "plain": "TButton",
    "accent": "Accent.TButton",
    "big": "Big.TButton",
    "danger": "Danger.TButton",
}

_DEFAULT_FONTS = {
    "base":   ("Segoe UI", 11),
    "bold":   ("Segoe UI", 11, "bold"),
    "h1":     ("Segoe UI", 19, "bold"),
    "entry":  ("Segoe UI", 11),
}

class _PalTheme:
    """Attribute-style view over gui.py's pal dict, so lattool_gui.py's
    existing `t.raised` / `t.accent` etc. keep working unchanged."""
    def __init__(self, pal, fonts):
        # Embedded latency pages share the main setting-card surface,
        # rather than painting an unrelated full-page dark/light background.
        self.bg       = pal["card"]
        self.raised   = pal["card"]
        self.inset    = pal["entry"]
        self.fg       = pal["text"]
        self.muted    = pal["muted"]
        self.accent   = pal["accent"]
        self.good     = pal["green"]
        self.warn     = pal["amber"]
        self.bad      = pal["accent"]
        self.border   = pal["entry_bd"]
        self.entry_bg = pal["entry"]
        self.entry_fg = pal["text"]
        self.font        = fonts["base"]
        self.font_bold   = fonts["bold"]
        self.font_h1     = fonts["h1"]
        self.font_h2     = fonts["h1"]
        self.font_big    = fonts["h1"]
        self.font_entry  = fonts["entry"]
        self.font_mono   = ("Consolas", 10)

class DarkCheck(ttk.Frame):
    SIZE = 15

    def __init__(
        self, master, *, text: str, variable: tk.BooleanVar | None = None,
        command: Any = None, surface: str = "bg", theme: Theme | None = None,
    ) -> None:
        t = theme or DEFAULT_THEME
        super().__init__(master, style=("Card.TFrame" if surface == "card" else "TFrame"))
        self.var = variable if variable is not None else tk.BooleanVar(master=self)
        self.command = command
        self._t = t
        bg = t.raised if surface == "card" else t.bg
        self.canvas = tk.Canvas(
            self, width=self.SIZE, height=self.SIZE, bg=bg, highlightthickness=1,
            highlightbackground=t.border, highlightcolor=t.accent, bd=0,
            takefocus=1, cursor="hand2",
        )
        self.canvas.pack(side="left", padx=(0, 8), pady=2)
        self.label = tk.Label(
            self, text=text, bg=bg, cursor="hand2", font=t.font, fg=t.fg,
            anchor="w", justify="left", wraplength=860,
        )
        self.label.pack(side="left", fill="x", expand=True)
        for w in (self.canvas, self.label):
            w.bind("<Button-1>", lambda _e: self.toggle())
            w.bind("<space>", lambda _e: self.toggle())
            w.bind("<Return>", lambda _e: self.toggle())
        self.var.trace_add("write", lambda *_a: self._draw())
        self._draw()

    def toggle(self) -> None:
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def _draw(self) -> None:
        t = self._t
        c = self.canvas
        c.delete("all")
        s = self.SIZE
        if self.var.get():
            c.create_rectangle(1, 1, s - 1, s - 1, fill=t.accent, outline=t.accent)
            c.create_line(3.5, s / 2, s / 2 - 1, s - 4.5, fill="#ffffff", width=2)
            c.create_line(s / 2 - 1, s - 4.5, s - 3.5, 3.5, fill="#ffffff", width=2)
        else:
            c.create_rectangle(1, 1, s - 1, s - 1, fill=t.inset, outline=t.border)

class SnappyAIApp:
    def __init__(
        self, root: tk.Tk, controller: GuiController | None = None, *,
        embed: dict[str, Any] | None = None,
    ) -> None:
        self.root = root
        self.ctl = controller or GuiController()

        self._kit = (embed.get("kit") if embed else None)
        self._pal = ((embed.get("pal") if embed else None)
                     or (self._kit.pal if self._kit else None))
        self._fonts = (embed.get("fonts") if embed else None)

        if self._pal is not None:
            self.theme = _PalTheme(self._pal, self._fonts or _DEFAULT_FONTS)
        else:
            self.theme = (embed.get("theme") if embed else None) or DEFAULT_THEME

        self._button_factory = (
            (embed.get("button") if embed else None)
            or (self._kit.button if self._kit else None)
        )
        self._on_timer_persist = (embed.get("on_timer_persist") if embed else None)
        self.last_script_path = ""

        self._embed_timer_var_traced = False

        if embed is None:
            root.title(APP_TITLE)
            root.geometry("1080x760")
            root.minsize(900, 640)
            root.configure(bg=self.theme.bg)
            self._configure_style()
            self._build()
        else:
            frames = embed["frames"]
            self.tab_home = frames["home"]
            self.tab_monitor = frames["monitor"]
            self.tab_tools = frames["tools"]
            self._embedded_scroll_binders = []
            if self._kit is not None:
                self.tab_monitor, monitor_bind = _scrollable(
                    self.tab_monitor, self._kit, self._kit.pal)
                self.tab_tools, tools_bind = _scrollable(
                    self.tab_tools, self._kit, self._kit.pal)
                self._embedded_scroll_binders = [
                    (self.tab_monitor, monitor_bind),
                    (self.tab_tools, tools_bind),
                ]
            status = embed.get("status")
            if status is None:
                status = ttk.Label(self.tab_home, style="CardDim.TLabel",
                                   anchor="w")
                status.pack(side="bottom", fill="x", padx=14, pady=(0, 8))
            self.status = status
            self._build_home()
            self._build_monitor()
            self._build_tools()
            for frame, binder in self._embedded_scroll_binders:
                binder(frame)
            self._configure_style(safe=True)
            self.header_state = ttk.Label(self.tab_home)
            self.progress = ttk.Progressbar(self.tab_home, mode="indeterminate")
        self._last_series: list[float] = []
        self._show_screen("start")
        if embed is None:
            root.after(POLL_MS, self._pump)

    def _button(
        self, parent, text: str, command: Any = None, kind: str = "plain",
        big: bool = False,
    ):

        kit_kind = {"danger": "amber"}.get(kind, kind)
        if self._kit is not None:
            return self._kit.button(parent, text, command, kind=kit_kind, big=big)
        if self._button_factory is not None:
            return self._button_factory(
                parent, text, command, kind=kind, big=big
            )
        return ttk.Button(
            parent, text=text, command=command,
            style=_FALLBACK_STYLE["big" if big else kind],
        )

    def _check(self, parent, text, variable, surface="bg"):
        """Create a checkbox row using kit.bool_switch when available,
        otherwise fall back to DarkCheck."""
        if self._kit is not None:
            row = tk.Frame(parent, bg=self.theme.raised if surface == "card"
                                        else self.theme.bg)
            self._kit.bool_switch(row, variable, word=True).pack(side="left")
            lbl = tk.Label(
                row, text=text, anchor="w", justify="left",
                bg=row.cget("bg"), fg=self.theme.fg,
                font=self.theme.font, wraplength=800, cursor="hand2",
            )
            lbl.pack(side="left", padx=(8, 0))

            lbl.bind("<Button-1>", lambda _e: variable.set(not variable.get()))
            return row

        dc = DarkCheck(parent, text=text, variable=variable,
                       surface=surface, theme=self.theme)
        return dc

    def _configure_style(self, safe: bool = False) -> None:
        t = self.theme
        style = ttk.Style(self.root)
        if not safe:
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            style.configure(
                ".", background=t.bg, foreground=t.fg, font=t.font,
                bordercolor=t.border, lightcolor=t.bg, darkcolor=t.bg,
            )
        style.configure("TFrame", background=t.bg)
        style.configure("Raised.TFrame", background=t.raised)
        style.configure("Card.TFrame", background=t.raised)
        style.configure("TLabel", background=t.bg, foreground=t.fg, font=t.font)
        style.configure("Dim.TLabel", background=t.bg, foreground=t.muted, font=t.font)
        style.configure(
            "Card.TLabel", background=t.raised, foreground=t.fg, font=t.font
        )
        style.configure(
            "CardDim.TLabel", background=t.raised, foreground=t.muted, font=t.font
        )
        style.configure("H1.TLabel", background=t.bg, foreground=t.fg, font=t.font_h1)
        style.configure("H2.TLabel", background=t.bg, foreground=t.fg, font=t.font_h2)
        style.configure(
            "Big.TLabel", background=t.raised, foreground=t.accent,
            font=t.font_big,
        )
        style.configure(
            "TButton", background=t.raised, foreground=t.fg, padding=(14, 7),
            borderwidth=1, relief="flat", focuscolor=t.accent, font=t.font,
        )
        style.map(
            "TButton", background=[("active", t.border), ("disabled", t.bg)],
            foreground=[("disabled", t.muted)],
        )
        style.configure(
            "Accent.TButton", background=t.accent, foreground=t.bg,
            font=t.font_bold, padding=(20, 10),
        )
        style.map(
            "Accent.TButton", background=[("active", t.accent)],
            foreground=[("disabled", t.muted)],
        )
        style.configure(
            "Big.TButton", background=t.accent, foreground=t.bg,
            font=(t.font_h1[0], 13, "bold"), padding=(28, 16),
        )
        style.map("Big.TButton", background=[("active", t.accent)])
        style.configure("Danger.TButton", background=t.raised, foreground=t.warn)
        style.map("Danger.TButton", background=[("active", t.border)])
        style.configure(
            "TNotebook", background=t.bg, borderwidth=0, tabmargins=(6, 6, 6, 0)
        )
        style.configure(
            "TNotebook.Tab", background=t.bg, foreground=t.muted, padding=(20, 10),
            font=t.font, borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", t.raised), ("active", t.border)],
            foreground=[("selected", t.accent), ("active", t.fg)],
        )
        style.configure(
            "TEntry", fieldbackground=t.entry_bg, foreground=t.entry_fg,
            insertcolor=t.entry_fg, bordercolor=t.border, padding=5, font=t.font_entry,
        )
        style.configure(
            "TCombobox",
            fieldbackground=t.entry_bg, background=t.entry_bg,
            foreground=t.entry_fg, arrowcolor=t.entry_fg,
            bordercolor=t.border, padding=4, font=t.font,
        )
        style.map("TCombobox",
                  fieldbackground=[("readonly", t.entry_bg)],
                  foreground=[("readonly", t.entry_fg)])
        style.configure(
            "Horizontal.TProgressbar", background=t.accent, troughcolor=t.inset,
            bordercolor=t.inset, lightcolor=t.accent, darkcolor=t.accent,
            thickness=8,
        )
        style.configure(
            "TLabelframe", background=t.bg, foreground=t.muted,
            bordercolor=t.border,
        )
        style.configure(
            "TLabelframe.Label", background=t.bg, foreground=t.muted,
            font=t.font_bold,
        )
        style.configure(
            "Card.TLabelframe", background=t.raised, bordercolor=t.border
        )
        style.configure(
            "Card.TLabelframe.Label", background=t.raised, foreground=t.accent,
            font=t.font_bold,
        )
        for orient in ("Vertical", "Horizontal"):
            style.configure(
                f"{orient}.TScrollbar", background=t.raised,
                troughcolor=t.inset, arrowcolor=t.muted,
                bordercolor=t.inset, relief="flat", arrowsize=13, gripcount=0,
            )
            style.map(
                f"{orient}.TScrollbar",
                background=[("active", t.border), ("disabled", t.raised)],
            )

    def retheme(self, theme: Theme) -> None:
        """Live light/dark switch when embedded in a host app: restyle ttk,
        remap the raw tk widgets that captured colours at build time, and
        redraw the canvases."""
        if self._kit is not None:

            theme = _PalTheme(self._kit.pal, self._fonts or _DEFAULT_FONTS)
        old = self.theme
        self.theme = theme
        self._configure_style(safe=True)
        bg_map = {old.bg.lower(): theme.bg, old.raised.lower(): theme.raised,
                  old.inset.lower(): theme.inset}
        fg_map = {old.fg.lower(): theme.fg, old.muted.lower(): theme.muted,
                  old.accent.lower(): theme.accent, old.good.lower(): theme.good,
                  old.warn.lower(): theme.warn, old.bad.lower(): theme.bad}
        bd = old.border.lower()
        checks: list[Any] = []

        def walk(w: Any) -> None:
            try:
                if isinstance(w, DarkCheck):
                    w._t = theme
                    checks.append(w)
                elif isinstance(w, tk.Label):
                    bg = str(w.cget("bg")).lower()
                    if bg in bg_map:
                        w.configure(bg=bg_map[bg])
                    fg = str(w.cget("fg")).lower()
                    if fg in fg_map:
                        w.configure(fg=fg_map[fg])
                elif isinstance(w, tk.Canvas):
                    bg = str(w.cget("bg")).lower()
                    if bg in bg_map:
                        w.configure(bg=bg_map[bg])
                    if str(w.cget("highlightbackground")).lower() == bd:
                        w.configure(highlightbackground=theme.border)
                elif isinstance(w, tk.Entry):
                    bg = str(w.cget("bg")).lower()
                    if bg in bg_map:
                        w.configure(bg=bg_map[bg])
                    fg = str(w.cget("fg")).lower()
                    if fg in fg_map:
                        w.configure(fg=fg_map[fg], insertbackground=fg_map[fg])
                    if str(w.cget("highlightbackground")).lower() == bd:
                        w.configure(highlightbackground=theme.border)
                elif isinstance(w, tk.Text):
                    bg = str(w.cget("bg")).lower()
                    if bg in bg_map:
                        w.configure(bg=bg_map[bg])
                    fg = str(w.cget("fg")).lower()
                    if fg in fg_map:
                        w.configure(fg=fg_map[fg], insertbackground=fg_map[fg])
                    sel = str(w.cget("selectbackground")).lower()
                    if sel in fg_map:
                        w.configure(selectbackground=fg_map[sel],
                                    selectforeground=theme.bg)
                    if str(w.cget("highlightbackground")).lower() == bd:
                        w.configure(highlightbackground=theme.border)
                    for tag, colour in (("h", theme.accent),
                                        ("good", theme.good),
                                        ("warn", theme.warn),
                                        ("bad", theme.bad),
                                        ("dim", theme.muted),
                                        ("head", theme.fg)):
                        w.tag_configure(tag, foreground=colour)
                for child in w.winfo_children():
                    walk(child)
            except tk.TclError:
                pass

        for frame in (self.tab_home, self.tab_monitor, self.tab_tools):
            walk(frame)
        for dc in checks:
            try:
                dc._draw()
            except tk.TclError:
                pass
        try:
            self._draw_spark(list(self._last_series))
        except tk.TclError:
            pass

    def _build(self) -> None:
        t = self.theme
        brand = self.ctl.brand
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)
        head = ttk.Frame(outer, style="Raised.TFrame", padding=(18, 12))
        head.pack(fill="x", side="top")
        ttk.Label(
            head, text=brand, style="H2.TLabel", font=(t.font_h1[0], 15),
        ).pack(side="left")
        ttk.Label(head, text="  " + APP_SUBTITLE, style="CardDim.TLabel").pack(
            side="left", padx=(10, 0)
        )
        self.header_state = ttk.Label(head, text="", style="CardDim.TLabel")
        self.header_state.pack(side="right")
        bar = ttk.Frame(outer, style="Raised.TFrame", padding=(14, 7))
        bar.pack(fill="x", side="bottom")
        self.progress = ttk.Progressbar(
            bar, mode="indeterminate", length=150, style="Horizontal.TProgressbar"
        )
        self.progress.pack(side="right", padx=(10, 0))
        self.status = ttk.Label(
            bar,
            text=f"{brand} only reads your system until you say otherwise. "
            "Nothing is ever sent anywhere.",
            style="CardDim.TLabel",
        )
        self.status.pack(side="left", fill="x", expand=True)
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True, padx=10)
        self.tab_home = ttk.Frame(self.notebook, padding=14)
        self.tab_monitor = ttk.Frame(self.notebook, padding=14)
        self.tab_tools = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(self.tab_home, text="  Fix my PC  ")
        self.notebook.add(self.tab_monitor, text="  Live latency  ")
        self.notebook.add(self.tab_tools, text="  Tools  ")
        self._build_home()
        self._build_monitor()
        self._build_tools()

    def _build_home(self) -> None:
        self.screens: dict[str, ttk.Frame] = {}
        self._screen_name = "start"
        for name in ("start", "analyzing", "results", "fixing", "done"):
            f = ttk.Frame(self.tab_home)
            self.screens[name] = f
        f = self.screens["start"]
        f.columnconfigure(0, weight=1)
        inner = ttk.Frame(f)
        inner.grid(row=0, column=0, sticky="nsew")
        f.rowconfigure(0, weight=1)
        inner.columnconfigure(0, weight=1)
        ttk.Label(inner, text="Is your game lagging?", style="H1.TLabel").grid(
            row=0, column=0, pady=(60, 6)
        )
        ttk.Label(
            inner,
            text="Checks your network, your drivers, your display and your "
            "Windows settings,\nthen tells you in plain words what is wrong - and "
            "fixes the parts it safely can.",
            style="Dim.TLabel",
            justify="center",
        ).grid(row=1, column=0, pady=(0, 34))
        self.btn_review = self._button(
            inner, "I allow to review my PC", self.on_review, big=True
        )
        self.btn_review.grid(row=2, column=0)
        ttk.Label(
            inner,
            text="Read-only review. No settings are changed until you approve them "
            "explicitly.",
            style="Dim.TLabel",
        ).grid(row=3, column=0, pady=(18, 0))
        f = self.screens["analyzing"]
        ttk.Label(f, text="Reviewing your PC", style="H1.TLabel").pack(pady=(70, 18))
        self.analyzing_note = ttk.Label(f, text="", style="Dim.TLabel")
        self.analyzing_note.pack()
        self.analyzing_bar = ttk.Progressbar(f, mode="indeterminate", length=340)
        self.analyzing_bar.pack(pady=(26, 0))
        f = self.screens["results"]
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        self.results_title = ttk.Label(f, text="", style="H1.TLabel")
        self.results_title.grid(row=0, column=0, sticky="w", pady=(0, 8))
        wrap = ttk.Frame(f)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        t = self.theme
        self.results_canvas = tk.Canvas(wrap, bg=t.bg, highlightthickness=0, bd=0)
        if self._kit is not None:
            vsb = _SlimScrollbar(wrap, self._kit, self.results_canvas,
                                 wheel=lambda step: self.results_canvas.yview_scroll(
                                     step, "units"))
        else:
            vsb = ttk.Scrollbar(wrap, orient="vertical",
                                command=self.results_canvas.yview)
        self.results_body = ttk.Frame(self.results_canvas)
        self.results_body.bind(
            "<Configure>",
            lambda e: self.results_canvas.configure(
                scrollregion=self.results_canvas.bbox("all")
            ),
        )
        self._results_window = self.results_canvas.create_window(
            (0, 0), window=self.results_body, anchor="nw")
        self.results_canvas.bind(
            "<Configure>",
            lambda e: self.results_canvas.itemconfigure(
                self._results_window, width=max(1, e.width - 2)),
            add="+",
        )
        self.results_canvas.configure(yscrollcommand=vsb.set)
        self.results_canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns", pady=(7, 7))
        self.results_canvas.bind(
            "<MouseWheel>",
            lambda e: self.results_canvas.yview_scroll(
                int(-1 * (e.delta / 120)), "units"
            ),
        )
        btns = ttk.Frame(f)
        btns.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.btn_continue = self._button(
            btns, "Continue", self.on_continue_results, kind="accent"
        )
        self.btn_continue.pack(side="left")
        self._button(
            btns, "Not now", lambda: self._show_screen("start")
        ).pack(side="left", padx=(10, 0))
        f = self.screens["fixing"]
        ttk.Label(f, text="Applying the fixes", style="H1.TLabel").pack(pady=(70, 18))
        self.fixing_note = ttk.Label(
            f, text="", style="Dim.TLabel", justify="center", wraplength=800
        )
        self.fixing_note.pack()
        self.fixing_bar = ttk.Progressbar(f, mode="indeterminate", length=340)
        self.fixing_bar.pack(pady=(26, 0))
        self.fixing_bar.start(14)
        f = self.screens["done"]
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        self.done_title = ttk.Label(f, text="", style="H1.TLabel")
        self.done_title.grid(row=0, column=0, sticky="w", pady=(0, 8))
        wrap = ttk.Frame(f)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.done_text = self._make_text(wrap, height=24)
        btns = ttk.Frame(f)
        btns.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self._button(btns, "Copy this list", self.on_copy_manual).pack(side="left")
        self._button(
            btns, "Open the full report",
            lambda: self._open_file("report.html"),
        ).pack(side="left", padx=8)
        self._button(
            btns, "Open the apply log",
            lambda: self._open_file("apply-run.log"),
        ).pack(side="left")
        self._button(
            btns, "Undo applied changes", self.on_undo, kind="danger"
        ).pack(side="left", padx=8)
        self._button(
            btns, "Start over", lambda: self._show_screen("start"), kind="accent"
        ).pack(side="right")

    def _show_screen(self, name: str) -> None:
        for key, frame in self.screens.items():
            if key != name:
                frame.pack_forget()
        self.screens[name].pack(fill="both", expand=True)
        self.btn_continue.configure(state="normal")
        self._screen_name = name
        if name == "analyzing":
            self.analyzing_bar.start(14)
        else:
            try:
                self.analyzing_bar.stop()
            except tk.TclError:
                pass
        if name == "fixing":
            self.fixing_bar.start(14)
        else:
            try:
                self.fixing_bar.stop()
            except tk.TclError:
                pass

    def _entry(self, parent, textvariable, width: int) -> tk.Entry:
        t = self.theme
        return tk.Entry(
            parent, textvariable=textvariable, width=width, font=t.font_entry,
            relief="solid", borderwidth=1, bg=t.entry_bg, fg=t.entry_fg,
            insertbackground=t.entry_fg, highlightbackground=t.border,
            highlightthickness=1,
        )

    def _build_monitor(self) -> None:
        t = self.theme
        tab = self.tab_monitor
        tab.columnconfigure(0, weight=1)
        nums = ttk.Frame(tab, style="Raised.TFrame", padding=(16, 14))
        nums.grid(row=0, column=0, sticky="ew")
        for i in range(6):
            nums.columnconfigure(i, weight=1)
        self.mon_big = ttk.Label(nums, text="--", style="Big.TLabel")
        self.mon_big.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 26))
        self.mon_stats: dict[str, ttk.Label] = {}
        for i, key in enumerate(("p50", "p95", "max", "loss", "jitter")):
            col = i + 1
            tk.Label(
                nums, text=key.upper(), bg=t.raised, fg=t.muted, font=t.font_bold
            ).grid(row=0, column=col, sticky="w")
            val = tk.Label(nums, text="--", bg=t.raised, fg=t.fg, font=t.font_h2)
            val.grid(row=1, column=col, sticky="w")
            self.mon_stats[key] = val
        self.mon_now_lbl = ttk.Label(nums, text="ms now", style="CardDim.TLabel")
        self.mon_now_lbl.grid(
            row=2, column=0, sticky="w"
        )
        self.spark = tk.Canvas(
            tab, height=170, bg=t.inset, highlightthickness=1,
            highlightbackground=t.border, bd=0,
        )
        self.spark.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        tab.rowconfigure(1, weight=1)
        chain = ttk.LabelFrame(
            tab, text="FULL CHAIN - CLICK TO SERVER TO SCREEN",
            style="Card.TLabelframe", padding=14,
        )
        chain.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        tiles = ttk.Frame(chain, style="Raised.TFrame")
        tiles.pack(fill="x")
        for i in range(4):
            tiles.columnconfigure(i, weight=1)
        self.chain_vals: dict[str, Any] = {}
        for i, key in enumerate(("input", "render", "network")):
            cell = ttk.Frame(tiles, style="Raised.TFrame")
            cell.grid(row=0, column=i, sticky="w", padx=(0, 18))

            tk.Label(
                cell, text=CHAIN_TILE_LABELS[key], bg=t.raised, fg=t.muted,
                font=t.font_bold,
            ).pack(anchor="w")
            val = tk.Label(cell, text="--", bg=t.raised, fg=t.fg, font=t.font_h2)
            val.pack(anchor="w")
            self.chain_vals[key] = val
        total_cell = ttk.Frame(tiles, style="Raised.TFrame")
        total_cell.grid(row=0, column=3, sticky="e")
        tk.Label(
            total_cell, text="CHAIN TOTAL", bg=t.raised, fg=t.muted,
            font=t.font_bold,
        ).pack(anchor="e")
        self.chain_total = tk.Label(
            total_cell, text="--", bg=t.raised, fg=t.accent, font=t.font_big
        )
        self.chain_total.pack(anchor="e")
        self.chain_note = ttk.Label(
            chain, text="", style="CardDim.TLabel", justify="left",
            wraplength=980,
        )
        self.chain_note.pack(anchor="w", pady=(8, 0))
        watch = ttk.Frame(tab)
        watch.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(watch, text="Watch:").pack(side="left")
        self.var_mon_target = tk.StringVar(value="1.1.1.1")
        entry = self._entry(watch, self.var_mon_target, 28)
        entry.pack(side="left", padx=8)
        entry.bind("<Return>", lambda _e: self._restart_watch())
        entry.bind("<FocusOut>", lambda _e: self._restart_watch())
        self._watch_started = None
        self._watch_session = 0
        ttk.Label(
            watch,
            text="p95 and max describe lag. A steady average with a high max "
            "means spikes, not speed.",
            style="Dim.TLabel",
        ).pack(side="left", padx=(16, 0))
        brow = ttk.Frame(tab)
        brow.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self.btn_chain = self._button(
            brow, "Measure full chain", self.on_measure_chain, kind="accent"
        )
        self.btn_chain.pack(side="left")
        ttk.Label(
            brow,
            text="Input: synthetic click to hook dispatch. Render: present to "
            "vblank on a test surface. Network: ping p95. The total is an "
            "estimate - panel scan-out and USB wire time need a camera.",
            style="Dim.TLabel",
        ).pack(side="left", padx=14)

    def _build_tools(self) -> None:
        tab = self.tab_tools
        tab.columnconfigure(0, weight=1)
        mem = ttk.LabelFrame(tab, text="MEMORY", style="Card.TLabelframe", padding=14)
        mem.grid(row=0, column=0, sticky="ew")
        row = ttk.Frame(mem, style="Raised.TFrame")
        row.pack(fill="x")
        self.btn_memclean = self._button(
            row, "Free up memory now", self.on_memory_clean, kind="accent"
        )
        self.btn_memclean.pack(side="left")
        ttk.Label(
            row,
            text="One click. Empties the standby list, flushes modified pages and "
            "trims working sets - every area a RAM cleaner touches.",
            style="CardDim.TLabel",
        ).pack(side="left", padx=14)
        self.mem_result = ttk.Label(
            mem, text="", style="CardDim.TLabel", justify="left", wraplength=980
        )
        self.mem_result.pack(anchor="w", pady=(10, 0))
        timer = ttk.LabelFrame(
            tab, text="SYSTEM TIMER", style="Card.TLabelframe", padding=14,
        )
        timer.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        trow = ttk.Frame(timer, style="Raised.TFrame")
        trow.pack(fill="x")
        self.var_timer = tk.BooleanVar(value=False)
        self.chk_timer = self._check(
            trow, "Hold a 0.5 ms system tick while SnappyAI runs",
            self.var_timer, surface="card",
        )
        self.chk_timer.pack(side="left")

        # Programmatic writes to var_timer (config restore, status sync) must not
        # re-trigger requests; only genuine user toggles go through the trace.
        self._timer_trace_guard = False
        self.var_timer.trace_add(
            "write", lambda *_a: None if self._timer_trace_guard
            else self.on_timer_toggle()
        )
        ttk.Label(
            trow,
            text="Raises the clock interrupt frequency so sub-millisecond waits "
            "are not rounded up to 15.6 ms. Released automatically on exit; "
            "raises idle power draw.",
            style="CardDim.TLabel",
            wraplength=620,
            justify="left",
        ).pack(side="left", padx=14)
        self.timer_result = ttk.Label(
            timer, text="", style="CardDim.TLabel", justify="left", wraplength=980
        )
        self.timer_result.pack(anchor="w", pady=(10, 0))
        gen = ttk.LabelFrame(
            tab, text="SCRIPTS AND MAINTENANCE", style="Card.TLabelframe",
            padding=14,
        )
        gen.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        self.var_del = tk.BooleanVar(value=False)
        self._check(gen, "Cleanup scripts may delete files (otherwise they only report "
            "what could be deleted)",
            self.var_del, surface="card").pack(anchor="w")
        self.var_removals = tk.BooleanVar(value=False)
        self._check(gen, "Include irreversible removals (Edge, Xbox overlay, OneDrive, "
            "Outlook) in the debloat script",
            self.var_removals, surface="card").pack(anchor="w", pady=(6, 0))
        row = ttk.Frame(gen, style="Raised.TFrame")
        row.pack(fill="x", pady=(12, 0))
        self._button(row, "Cleanup script", self.on_gen_cleanup).pack(side="left")
        self._button(row, "Redistributables script", self.on_gen_redists).pack(
            side="left", padx=8
        )
        self._button(row, "Debloat script", self.on_gen_debloat).pack(side="left")
        self._button(row, "Memory script", self.on_gen_memory).pack(
            side="left", padx=8
        )
        self._button(row, "Power script", self.on_gen_power).pack(side="left")
        self._button(row, "Registry payload...", self.on_registry).pack(
            side="left", padx=(8, 0)
        )
        self.script_result = ttk.Label(
            gen, text="No script generated yet.", style="CardDim.TLabel",
            justify="left", wraplength=980,
        )
        self.script_result.pack(anchor="w", pady=(10, 0))
        runrow = ttk.Frame(gen, style="Raised.TFrame")
        runrow.pack(fill="x", pady=(8, 0))
        self.btn_run_script = self._button(
            runrow, "Run the generated script silently", self.on_run_script,
            kind="accent",
        )
        self.btn_run_script.pack(side="left")
        self.btn_run_script.configure(state="disabled")
        ttk.Label(
            runrow,
            text="Runs hidden: no terminal opens. A confirmation popup comes first.",
            style="CardDim.TLabel",
        ).pack(side="left", padx=14)
        diag = ttk.LabelFrame(
            tab, text="IF SOMETHING WENT WRONG", style="Card.TLabelframe",
            padding=14,
        )
        diag.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        row = ttk.Frame(diag, style="Raised.TFrame")
        row.pack(fill="x")
        self._button(
            row, "Create a diagnosis log", self.on_diagnose, kind="accent"
        ).pack(side="left")
        self._button(row, "Open output folder", self.on_open_outdir).pack(
            side="left", padx=10
        )
        ttk.Label(
            row,
            text="The log is a plain text file you can paste anywhere. It holds no "
            "passwords and no personal files.",
            style="CardDim.TLabel",
        ).pack(side="left", padx=10)

    def _make_text(self, parent, height: int = 12) -> tk.Text:
        t = self.theme
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        txt = tk.Text(
            wrap, height=height, wrap="word", bg=t.inset, fg=t.fg,
            insertbackground=t.fg, relief="flat", font=t.font, padx=12, pady=10,
            selectbackground=t.accent, selectforeground=t.bg, state="disabled",
            borderwidth=0, highlightthickness=1, highlightbackground=t.border,
            highlightcolor=t.accent, spacing3=4,
        )
        if self._kit is not None:
            vsb = _SlimScrollbar(wrap, self._kit, txt,
                                 wheel=lambda step: txt.yview_scroll(step, "units"))
        else:
            vsb = ttk.Scrollbar(wrap, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vsb.set)
        txt.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns", pady=(7, 7))
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        for tag, colour in (
            ("h", t.accent), ("good", t.good), ("warn", t.warn),
            ("bad", t.bad), ("dim", t.muted), ("head", t.fg),
        ):
            txt.tag_configure(tag, foreground=colour)
        txt.tag_configure("h", font=t.font_h2, spacing1=10, spacing3=4)
        txt.tag_configure("dim", font=(t.font[0], 9))
        txt.tag_configure("head", font=t.font_bold)
        return txt

    @staticmethod
    def _write(txt: tk.Text, blocks: list[tuple[str, str]]) -> None:
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        for tag, content in blocks:
            txt.insert("end", content + "\n", tag)
        txt.configure(state="disabled")

    def _pump(self) -> None:
        self.pump_once()
        self.root.after(POLL_MS, self._pump)

    def pump_once(self) -> None:
        events = self.ctl.poll()

        last_tick = -1
        for i, ev in enumerate(events):
            if ev.kind is EventType.MONITOR_TICK:
                last_tick = i
        for i, ev in enumerate(events):
            if ev.kind is EventType.MONITOR_TICK and i != last_tick:
                continue
            try:
                self._handle(ev)
            except Exception as exc:
                self.status.configure(
                    text=f"Internal error while updating the window: {exc}"
                )

    def _handle(self, ev) -> None:
        k = ev.kind
        if k is EventType.BUSY:
            self.header_state.configure(text=f"working: {ev.message}")
            self.progress.start(12)
        elif k is EventType.IDLE:
            self.header_state.configure(text="")
            self.progress.stop()
        elif k is EventType.PROGRESS:
            self.status.configure(text=ev.message)
            if self._current_screen() == "analyzing":
                self.analyzing_note.configure(text=ev.message)
            elif self._current_screen() == "fixing":
                self.fixing_note.configure(text=ev.message)
        elif k is EventType.LOG:
            self.status.configure(text=ev.message)
        elif k is EventType.SCAN_DONE:
            self._show_results()
        elif k is EventType.APPLY_DONE:
            self._show_done(ev.data or {})
        elif k is EventType.UNDO_DONE:
            self._show_undo(ev.data or {})
        elif k is EventType.MONITOR_TICK:
            self._render_tick(ev.data or {})
        elif k is EventType.CHAIN_DONE:
            self._render_chain(ev.data or {})
        elif k is EventType.MEMORY_CLEAN_DONE:
            self._render_memory(ev.data or {})
        elif k is EventType.TIMER_DONE:
            self.status.configure(text=ev.message)
            self._render_timer(ev.data or {})
        elif k is EventType.SCRIPT_DONE:
            d = ev.data or {}
            if "exit_code" in d:
                self._show_run_result(d)
            elif str(d.get("path", "")).lower().endswith(".ps1"):
                self.last_script_path = d.get("path", "")
                self.script_result.configure(
                    text=f"{ev.message}\n{d.get('path', '')}\n"
                    "Review the file, then run it with the button below."
                )
                self.btn_run_script.configure(state="normal")
                self.status.configure(text=f"{ev.message}  ->  {d.get('path', '')}")
            else:
                self.status.configure(text=f"{ev.message}  ->  {d.get('path', '')}")
        elif k is EventType.ERROR:
            self.status.configure(text=f"Error: {ev.message}")
            messagebox.showerror(self.ctl.brand, ev.message)

    def _current_screen(self) -> str:
        return self._screen_name

    def on_review(self) -> None:
        if not messagebox.askyesno(self.ctl.brand, CONSENT_TEXT):
            return
        self._show_screen("analyzing")
        self.analyzing_note.configure(text="reading your system")
        self.ctl.start_scan(experimental=False, with_maintenance=False)

    def _show_results(self) -> None:
        brand = self.ctl.brand
        t = self.theme
        small = (t.font[0], 9)
        small_b = (t.font[0], 9, "bold")
        scan = self.ctl.last_scan
        if scan is None:
            self._show_screen("start")
            return
        groups = classify_scan(scan, brand=brand)
        self._groups = groups
        n_auto, n_manual = len(groups["auto"]), len(groups["manual"])
        n_total = n_auto + n_manual
        for child in self.results_body.winfo_children():
            child.destroy()
        r = 0

        def heading(text: str, colour: str) -> None:
            nonlocal r
            tk.Label(
                self.results_body, text=text, bg=t.bg, fg=colour, font=t.font_h2,
                anchor="w",
            ).grid(row=r, column=0, sticky="ew", pady=(14 if r else 0, 6))
            r += 1

        def card(pf: PlainFinding) -> None:
            nonlocal r
            box = ttk.Frame(self.results_body, style="Card.TFrame", padding=(14, 10))
            box.grid(row=r, column=0, sticky="ew", pady=3)
            box.columnconfigure(0, weight=1)
            tk.Label(
                box, text=pf.headline, bg=t.raised, fg=t.fg, font=t.font_bold,
                anchor="w",
            ).grid(row=0, column=0, sticky="w")
            tk.Label(
                box, text=pf.detail, bg=t.raised, fg=t.muted, font=small,
                anchor="w", justify="left", wraplength=880,
            ).grid(row=1, column=0, sticky="w", pady=(3, 0))
            tk.Label(
                box, text=pf.fix_text, bg=t.raised,
                fg=t.good if pf.fix_kind == "auto" else t.warn,
                font=small_b, anchor="w",
            ).grid(row=2, column=0, sticky="w", pady=(4, 0))
            r += 1

        if n_total == 0:
            self.results_title.configure(
                text=f"No problems found that {brand} can act on."
            )
            heading(
                f"Everything checked looks fine ({len(groups['fine'])} checks)",
                t.good,
            )
        else:
            self.results_title.configure(
                text=f"{n_total} thing(s) are costing you performance"
            )
        if groups["auto"]:
            heading(f"{brand} can fix these for you ({n_auto})", t.accent)
            for pf in groups["auto"]:
                card(pf)
        if groups["manual"]:
            heading(f"These need your own hands ({n_manual})", t.warn)
            for pf in groups["manual"]:
                card(pf)
        if groups["unsure"]:
            heading("Could not be checked without more measurement", t.warn)
            for pf in groups["unsure"]:
                tk.Label(
                    self.results_body,
                    text="•  " + pf.headline + " - " + pf.detail, bg=t.bg,
                    fg=t.muted, font=small, anchor="w", justify="left",
                    wraplength=940,
                ).grid(row=r, column=0, sticky="ew", pady=2)
                r += 1
        if groups["fine"] and n_total:
            heading(f"Already fine ({len(groups['fine'])} checks)", t.good)
        self.results_canvas.configure(scrollregion=self.results_canvas.bbox("all"))
        self._show_screen("results")
        if n_total:
            if messagebox.askyesno(
                brand, issues_message(n_total, n_auto, n_manual, brand=brand)
            ):
                self._ask_apply()
            else:
                self._show_screen("start")
        else:
            self.btn_continue.configure(state="disabled")

    def _ask_apply(self) -> None:
        if not messagebox.askyesno(self.ctl.brand, apply_confirm_text(self.ctl.brand)):
            self._show_screen("start")
            return
        self._show_screen("fixing")
        self.fixing_note.configure(
            text="running the generated script silently, elevated - "
            "no terminal window opens"
        )
        self.ctl.start_apply()

    def on_continue_results(self) -> None:
        groups = getattr(self, "_groups", None)
        if not groups:
            return
        n_auto, n_manual = len(groups["auto"]), len(groups["manual"])
        if messagebox.askyesno(
            self.ctl.brand,
            issues_message(n_auto + n_manual, n_auto, n_manual,
                           brand=self.ctl.brand),
        ):
            self._ask_apply()
        else:
            self._show_screen("start")

    def _show_done(self, data: dict[str, Any]) -> None:
        brand = self.ctl.brand
        code = data.get("exit_code")
        ok = code == 0
        self.done_title.configure(
            text="Done - here is what still needs you"
            if ok
            else f"The automatic fixes stopped with exit code {code}"
        )
        blocks: list[tuple[str, str]] = []
        if not ok:
            blocks.append(("bad", "The apply script reported a failure. Its log:"))
            blocks.append(("dim", (data.get("log") or "(no output)")[-3000:]))
            blocks.append(("", ""))
        blocks.append(("h", f"What {brand} changed"))
        scan = self.ctl.last_scan
        groups = classify_scan(scan, brand=brand) if scan else {"auto": []}
        if groups["auto"]:
            for pf in groups["auto"]:
                blocks.append(("good", "•  " + pf.headline))
        else:
            blocks.append(("dim", "nothing was applied"))
        blocks.append(("h", "What you need to do yourself"))
        steps = manual_instructions(scan) if scan else []
        if steps:
            for i, st in enumerate(steps, 1):
                blocks.append(("head", f"{i}. {st['title']}"))
                for line in st["instruction"].split("\n"):
                    blocks.append(("", "   " + line))
                blocks.append(("", ""))
        else:
            blocks.append(("dim", "nothing - every finding was automatic"))
        blocks.append(("h", "After you are done"))
        blocks.append(
            (
                "",
                "Run Measure full chain again on the Live latency tab and compare "
                "the totals. If a change did not measurably help, undo it "
                "with the button below: undo.ps1 restores your previous values.",
            )
        )
        self._manual_blocks = blocks
        self._write(self.done_text, blocks)
        self._show_screen("done")

    def _show_undo(self, data: dict[str, Any]) -> None:
        brand = self.ctl.brand
        code = data.get("exit_code")
        if code == 0:
            messagebox.showinfo(
                brand,
                "Undo complete: every recorded change was restored from the snapshot.",
            )
            self.status.configure(text="undo complete: prior values restored")
        else:
            messagebox.showerror(
                brand,
                f"Undo stopped with exit code {code}.\n\n"
                f"{(data.get('log') or '')[-1500:]}\n\n"
                f"Full log: {data.get('log_path', '')}",
            )
            self.status.configure(text=f"undo failed with exit code {code}")

    def _show_run_result(self, data: dict[str, Any]) -> None:
        brand = self.ctl.brand
        code = data.get("exit_code")
        if code == 0:
            messagebox.showinfo(
                brand,
                f"Script finished successfully.\n\nLog: {data.get('log_path', '')}",
            )
        else:
            messagebox.showerror(
                brand,
                f"Script stopped with exit code {code}.\n\n"
                f"{(data.get('log') or '')[-1500:]}\n\n"
                f"Full log: {data.get('log_path', '')}",
            )
        self.status.configure(
            text=f"{data.get('script', 'script')} finished with exit code {code}"
        )

    def on_undo(self) -> None:
        if not messagebox.askyesno(
            self.ctl.brand, undo_confirm_text(self.ctl.brand)
        ):
            return
        self._show_screen("fixing")
        self.fixing_note.configure(
            text="running the undo script silently - no terminal window opens"
        )
        self.ctl.start_undo()

    def on_copy_manual(self) -> None:
        lines = []
        for _tag, text in getattr(self, "_manual_blocks", []):
            if text.strip():
                lines.append(text)
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.status.configure(text="Manual steps copied to the clipboard.")

    def _sync_chain_btn(self) -> None:
        try:
            want = "Stop" if self.ctl.monitoring else "Measure full chain"
            if str(self.btn_chain.cget("text")) != want:
                self.btn_chain.configure(text=want)
        except (AttributeError, tk.TclError):
            pass

    def _start_watch(self, target: str) -> None:
        self.ctl.start_monitor(target, interval=0.1)
        self._watch_started = time.monotonic()
        self._watch_session += 1
        sess = self._watch_session
        self.mon_big.configure(text="--")
        self.mon_now_lbl.configure(text="ms now")
        self._draw_spark([])
        self._sync_chain_btn()
        if hasattr(self, "status"):
            self.status.configure(
                text=f"measuring {target} for {WATCH_SECONDS:g}s (one sample "
                     f"every 0.1s) - press Stop to end early; the graph and "
                     f"stats stay on screen when it finishes")
        try:
            self.root.after(
                int(WATCH_SECONDS * 1000),
                lambda: self._finish_watch(sess, timed_out=True))
        except tk.TclError:
            pass

    def _finish_watch(self, sess: int, timed_out: bool) -> None:
        """End the timed run. A stale timer (superseded by a newer session)
        or an already-stopped monitor is ignored, so restarting mid-run
        cannot have the old timer kill the new measurement. The graph and
        the statistics deliberately STAY on screen: the run is over, its
        result is what the user came for."""
        if sess != self._watch_session or not self.ctl.monitoring:
            return
        self.ctl.stop_monitor()
        elapsed = 0.0
        if self._watch_started is not None:
            elapsed = time.monotonic() - self._watch_started
        self._watch_started = None
        self._sync_chain_btn()
        try:
            self.mon_now_lbl.configure(
                text="ms now - measured %.0fs (%s)"
                     % (elapsed, "complete" if timed_out else "stopped early"))
        except tk.TclError:
            pass
        if hasattr(self, "status"):
            if timed_out:
                self.status.configure(
                    text="measurement complete: %.1f s of samples - the graph "
                         "and stats above stay; press the button again for a "
                         "new run" % elapsed)
            else:
                self.status.configure(
                    text="watch stopped after %.1f s - the graph and stats "
                         "above stay" % elapsed)

    def _stop_watch(self) -> None:
        self._finish_watch(self._watch_session, timed_out=False)

    def _restart_watch(self) -> None:
        """Target-field commit (Enter / focus-out): (re)watch the new target
        without triggering a chain run."""
        target = self.var_mon_target.get().strip()
        if not target:
            if self.ctl.monitoring:
                self._stop_watch()
            if hasattr(self, 'status'):
                self.status.configure(text="Enter an IP to watch.")
            return
        self._start_watch(target)

    def _render_tick(self, d: dict[str, Any]) -> None:
        if d.get("error"):
            self.mon_big.configure(text="--")
            self.status.configure(text=f"monitor: {d['error']}")
            return
        cur = d.get("current")
        self.mon_big.configure(text=f"{cur:.1f}" if cur is not None else "--")
        if self._watch_started is not None:
            el = int(time.monotonic() - self._watch_started)
            try:
                self.mon_now_lbl.configure(
                    text="ms now - measuring %ds / %gs" % (el, WATCH_SECONDS))
            except tk.TclError:
                pass
        p50 = d.get("p50")
        p95 = d.get("p95")
        mx = d.get("max")
        jt = d.get("jitter")
        self.mon_stats["p50"].configure(text=f"{p50:.2f}" if p50 else "--")
        self.mon_stats["p95"].configure(text=f"{p95:.2f}" if p95 else "--")
        self.mon_stats["max"].configure(text=f"{mx:.2f}" if mx else "--")
        self.mon_stats["loss"].configure(text=f"{d.get('loss_pct', 0.0):.1f}%")
        self.mon_stats["jitter"].configure(text=f"{jt:.2f}" if jt else "--")
        series = d.get("series") or []
        self._last_series = list(series)
        self._draw_spark(series)

    def _draw_spark(self, series: list[float]) -> None:
        t = self.theme
        c = self.spark
        c.delete("all")
        c.update_idletasks()
        w = max(50, c.winfo_width())
        h = max(40, c.winfo_height())
        if len(series) < 2:
            c.create_text(
                w / 2, h / 2,
                text="waiting for a second sample..." if series
                else "no samples yet",
                fill=t.muted, font=t.font,
            )
            return
        top = max(series) * 1.15 or 1.0
        bottom = min(series) * 0.85
        span = max(0.5, top - bottom)
        n = len(series)
        pts = []
        for i, v in enumerate(series):
            x = (i / max(1, n - 1)) * (w - 12) + 6
            y = h - 10 - ((v - bottom) / span) * (h - 24)
            pts.extend((x, y))
        c.create_line(*pts, fill=t.accent, width=2, smooth=False)
        c.create_text(
            8, 8, anchor="nw", fill=t.muted, font=t.font_mono,
            text=f"min {min(series):.1f}  max {max(series):.1f}  "
            f"now {series[-1]:.1f} ms",
        )

    def on_measure_chain(self) -> None:
        """The ONE button: first press starts the live watch (top values +
        graph) AND the full-chain snapshot; while the watch runs the button
        is a Stop for the live measurement.

        The measurement starts immediately (no blocking confirmation popup),
        so the chain result is rendered straight into the Live latency tab
        instead of being hidden behind a popup that must be dismissed.
        """
        if self.ctl.monitoring:
            self._stop_watch()
            return
        target = self.var_mon_target.get().strip() or "1.1.1.1"
        self._start_watch(target)
        try:
            from utils import reports_dir
            path = Path(reports_dir()) / "chain.json"
        except Exception:
            path = self.ctl.out_root / "chain.json"
        self.chain_note.configure(
            text="measuring: input, then render, then network... "
                 "(the live graph keeps running while the chain measures)")
        self.ctl.start_chain(target=target, save_path=path)

    def _render_chain(self, d: dict[str, Any]) -> None:
        brand = self.ctl.brand
        iv = d.get("input") or {}
        rv = d.get("render") or {}
        nv = d.get("network") or {}
        one = iv.get("p50_one_way_ms")
        rp = rv.get("p95_ms")
        np_ = nv.get("p95")
        self.chain_vals["input"].configure(
            text=f"{one:.2f} ms" if one is not None else "--"
        )
        self.chain_vals["render"].configure(
            text=f"{rp:.1f} ms" if rp is not None else "--"
        )
        self.chain_vals["network"].configure(
            text=f"{np_:.1f} ms" if np_ is not None else "--"
        )
        total = (d.get("chain") or {}).get("total_ms")
        self.chain_total.configure(
            text=f"{total:.1f} ms" if total is not None else "--"
        )
        if total is None:
            notes = []
            for leg, payload in (("input", iv), ("render", rv), ("network", nv)):
                err = payload.get("error")
                if err:
                    notes.append(f"{leg}: {err}")
            self.chain_note.configure(
                text="Chain incomplete - " + "; ".join(notes)
                if notes else "Chain incomplete."
            )
            messagebox.showwarning(
                brand,
                "The full chain could not be measured:\n\n"
                + "\n".join(notes)
                + f"\n\nFull result: {d.get('saved_to', '')}",
            )
        else:
            dur = d.get("duration_s")
            dur_txt = f" Measured over {dur:.1f} s." if dur else ""
            self.chain_note.configure(
                text=f"chain {total:.1f} ms = input {one:.2f} + render {rp:.1f} + "
                f"network {np_:.1f}.{dur_txt} Saved to {d.get('saved_to', '')}. "
                "Estimate only - see the method notes."
            )

    def on_memory_clean(self) -> None:
        if not messagebox.askyesno(
            self.ctl.brand,
            "Free up memory now? Windows will page trimmed memory back on demand; "
            "expect a short burst of disk activity afterwards.",
        ):
            return
        self.mem_result.configure(text="working...")
        self.ctl.start_memory_clean()

    def _render_memory(self, d: dict[str, Any]) -> None:
        before, after = d.get("before") or {}, d.get("after") or {}
        areas = d.get("areas") or []
        avail_b = before.get("Available MBytes", next(iter(before.values()), None))
        avail_a = after.get("Available MBytes", next(iter(after.values()), None))
        lines = ["Freed memory report:"]
        if avail_b is not None and avail_a is not None:
            lines.append(f"  available: {avail_b} MB before -> {avail_a} MB after")
        for a in areas:
            lines.append(f"  {a.get('area')}: {a.get('status')}")
        lines.append("")
        lines.append(d.get("caveat", ""))
        self.mem_result.configure(text="\n".join(lines))

    def on_timer_toggle(self) -> None:
        want = self.var_timer.get()
        if want:
            self.timer_result.configure(text="requesting a finer tick...")
            started = self.ctl.start_timer_request(500.0)
        else:
            self.timer_result.configure(text="releasing...")
            started = self.ctl.start_timer_stop()
        if not started:
            # Busy: just tell the user. Never write var_timer back here - the
            # write trace would fire on_timer_toggle again and loop forever,
            # spamming "another operation is still running" over every task.
            self.timer_result.configure(
                text="busy - wait for the current operation, then try again"
            )
            return
        if self._on_timer_persist:
            self._on_timer_persist(500 if want else 0)

    def _render_timer(self, d: dict[str, Any]) -> None:
        lines: list[str] = []
        if d.get("error"):
            lines.append(f"  {d['error']}")
        actual = d.get("actual_us")
        if actual:
            lines.append(f"  request active: {actual / 1000.0:.4f} ms "
                         f"(for SnappyAI's own waits; process-scoped on "
                         f"Windows 10 2004+)")
            if d.get("clamped"):
                lines.append(
                    f"  (0.5 ms not reachable; platform floor is "
                    f"{(d.get('min_us') or 0) / 1000.0:.4f} ms)"
                )
        elif d.get("current_us") and not d.get("active"):
            lines.append(f"  released; tick now "
                         f"{d['current_us'] / 1000.0:.4f} ms")
        thr = d.get("throttling") or {}
        if thr.get("attempted") and not thr.get("ok"):
            lines.append(
                "  note: background-throttling opt-out unavailable, so Windows may "
                "drop the request while this window is minimized"
            )
        self.timer_result.configure(text="\n".join(lines))

        # Sync the checkbox silently (trace write guard): this runs after every
        # timer event, so a guarded write keeps it from re-requesting a tick.
        if self.var_timer.get() != bool(d.get("active")):
            self._timer_trace_guard = True
            try:
                self.var_timer.set(bool(d.get("active")))
            finally:
                self._timer_trace_guard = False

    def on_gen_cleanup(self) -> None:
        self.ctl.start_generate(kind="cleanup", execute=self.var_del.get())

    def on_gen_redists(self) -> None:
        self.ctl.start_generate(kind="redists")

    def on_gen_debloat(self) -> None:
        selected = None
        if not self.var_removals.get():
            selected = tuple(
                o["id"] for o in self.ctl.debloat_options() if o["reversible"]
            )
        self.ctl.start_generate(kind="debloat", selected=selected)

    def on_gen_memory(self) -> None:
        self.ctl.start_generate(kind="memory", selected=("memory.measure",))

    def on_gen_power(self) -> None:
        self.ctl.start_generate(kind="power")

    def on_registry(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a registry payload JSON",
            filetypes=[("JSON payload", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        self.status.configure(text=f"validating {path}...")
        self.ctl.start_registry(payload=path)

    def on_run_script(self) -> None:
        path = self.last_script_path
        if not path:
            messagebox.showwarning(self.ctl.brand, "Generate a script first.")
            return
        if not messagebox.askyesno(
            self.ctl.brand, run_confirm_text(path, self.ctl.brand)
        ):
            return
        self.status.configure(text=f"running {path} silently...")
        self.ctl.start_run_script(path)

    def on_diagnose(self) -> None:
        self.ctl.start_diagnose()

    def on_open_outdir(self) -> None:
        try:
            from utils import app_dir
            self._open_path(Path(app_dir()))
        except Exception:
            self._open_path(self.ctl.out_root)

    def _open_file(self, name: str) -> None:
        target = (
            self.ctl.last_scan.out_dir if self.ctl.last_scan else self.ctl.out_root
        )
        path = Path(target) / name
        if not path.exists():
            # Also check reports dir
            try:
                from utils import reports_dir
                alt = Path(reports_dir()) / name
                if alt.exists():
                    path = alt
            except Exception:
                pass
        if not path.exists():
            messagebox.showwarning(
                self.ctl.brand, f"{path.name} does not exist yet."
            )
            return
        self._open_path(path)

    def _open_path(self, path: Path) -> None:
        import os
        import subprocess
        import sys

        path = Path(path)
        if not path.exists():
            messagebox.showwarning(self.ctl.brand, f"{path} does not exist yet.")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror(self.ctl.brand, f"Could not open {path}: {exc}")

def build_app(
    root: tk.Tk, *, backend: str = "auto", out_root: str | Path | None = None
) -> SnappyAIApp:
    ctl = GuiController(backend_name=backend, out_root=out_root)
    return SnappyAIApp(root, ctl)

def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    p = argparse.ArgumentParser(
        prog="latency-gui", description="latency graphical interface"
    )
    p.add_argument(
        "--backend", choices=("auto", "fake"), default="auto",
        help="'fake' drives the whole UI from the built-in Windows fixture",
    )
    p.add_argument("--out", default=None, help="output root directory")
    args = p.parse_args(argv)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        import sys

        print(f"latency-gui: cannot open a display: {exc}", file=sys.stderr)
        return 1
    app = build_app(root, backend=args.backend, out_root=args.out)
    if args.backend == "fake":
        app.status.configure(
            text="DEMO MODE: built-in fixture, not this machine. "
            "Nothing here is your PC."
        )
    if os.environ.get("LATTOOL_GUI_SMOKE"):
        for _ in range(5):
            root.update()
        root.destroy()
        return 0
    root.mainloop()
    return 0


# ======================================================================
# merged from spoofer_tab.py
# ======================================================================

"""Spoofer tab for snappy-detector.

The tab reuses the HWID spoofer package that lives next to this file and
follows the host application's ThemeKit conventions: every widget is
registered with a palette role, buttons come from ``kit.button`` and the page
body is wrapped in the same slim scroll area as the other tabs.
"""

import json
import queue
import threading
import tkinter as tk
from tkinter import messagebox
from datetime import datetime
from pathlib import Path
from typing import Any

from spoofer import (
    AuditEventType, CleanerEngine, audit_log, get_audit_logger,
    get_loading_status, reload_driver,
    detect as detect_external_tools, is_admin, get_registry_engine,
    create_restore_point, is_supported as restore_point_supported,
    run_verification, get_boot_spoofer, get_gpu_spoofer,
    get_monitor_spoofer, get_peripheral_spoofer, get_disk_spoofer,
    get_nic_spoofer, get_smbios_spoofer,
)

SMBIOS_TYPES = {
    "0": "BIOS",
    "1": "System",
    "2": "Baseboard",
    "3": "Chassis",
    "4": "Processor",
    "17": "Memory device",
}


def _json_text(value: Any, limit: int = 1400) -> str:
    try:
        text = json.dumps(value, indent=2, default=str)
    except Exception:
        text = str(value)
    if len(text) > limit:
        text = text[:limit] + "\n... (truncated)"
    return text


def build_spoofer_page(page, kit, pal, status_msg, scrollable, fonts):
    """Build the Spoofer tab into *page*.

    Parameters are supplied by gui.py so this module does not import the host
    application (which would create a cycle):
      page       - the tk.Frame created for the tab
      kit        - ThemeKit instance
      pal        - current palette dict
      status_msg - footer status setter
      scrollable - gui._scrollable
      fonts      - dict with h1/bold/desc/entry font tuples
    """
    F_BOLD = fonts["bold"]
    F_DESC = fonts["desc"]
    F_ENTRY = fonts["entry"]
    F_MONO = ("Consolas", 9)

    inner, bind_wheel = scrollable(page, kit, pal)

    results = queue.Queue()
    busy = {"on": False}
    snapshot_names = []
    log_box = None

    # ------------------------------------------------------------------ utils
    def card(parent, role="card", **kwargs):
        widget = tk.Frame(parent, bg=pal["card"], **kwargs)
        kit.reg(widget, role)
        return widget

    def label(parent, text, font=F_DESC, muted=False, width=None):
        widget = tk.Label(
            parent, text=text, font=font, anchor="w", justify="left",
            bg=pal["card"], fg=pal["muted"] if muted else pal["text"],
        )
        if width is not None:
            widget.configure(width=width)
        kit.reg(widget, "card_muted" if muted else "card_text")
        return widget

    def section(title, note=""):
        wrap = tk.Frame(
            inner, bg=pal["card"], highlightthickness=1,
            highlightbackground=pal["entry_bd"],
        )
        kit.reg(wrap, "panel")
        wrap.pack(fill="x", padx=20, pady=(0, 14))
        head = card(wrap)
        head.pack(fill="x", padx=12, pady=(10, 0))
        head_title = label(head, title, F_BOLD)
        head_title.pack(side="left")
        if note:
            note_label = label(wrap, note, F_DESC, muted=True)
            note_label.pack(fill="x", padx=12, pady=(2, 0))

            def _wrap_note(event=None, widget=note_label, host=wrap):
                try:
                    value = max(220, host.winfo_width() - 26)
                    if str(widget.cget("wraplength")) != str(value):
                        widget.configure(wraplength=value)
                except tk.TclError:
                    pass

            wrap.bind("<Configure>", _wrap_note)
        body = card(wrap)
        body.pack(fill="x", padx=12, pady=(8, 10))
        return body

    def row(parent, text, hint=""):
        # Each original form stays intact, but is now a responsive label /
        # input / action grid rather than an uneven horizontal pack row.
        line = card(parent)
        line.pack(anchor="center", pady=5, padx=8)
        line._spoofer_form = True
        line._caption = label(line, text, F_BOLD)
        line._caption.grid(row=0, column=0, sticky="w", padx=(0, 12), pady=5)
        line._field = None
        line._action = None
        if hint:
            line._hint = label(line, hint, F_DESC, muted=True)
            line._hint.grid(row=2, column=0, columnspan=3, sticky="w")

        def fit(event=None):
            if line._field is None or line._action is None:
                return
            narrow = parent.winfo_width() < 615
            if narrow == getattr(line, "_narrow", None):
                return
            line._narrow = narrow
            line._caption.grid_configure(
                row=0, column=0, columnspan=2 if narrow else 1)
            line._field.grid_configure(
                row=1 if narrow else 0, column=0 if narrow else 1,
                sticky="w", padx=(0, 10), pady=5)
            line._action.grid_configure(
                row=1 if narrow else 0, column=1 if narrow else 2,
                sticky="e", padx=(0, 2), pady=5)
            line.columnconfigure(0, weight=1 if narrow else 0)
            line.columnconfigure(1, weight=1)
            line.columnconfigure(2, weight=0)
        line.bind("<Configure>", fit, add="+")
        line._fit_form = fit
        return line

    def entry(parent, initial="", width=18):
        var = tk.StringVar(value=str(initial))
        widget = tk.Entry(
            parent, textvariable=var, width=width, font=F_ENTRY,
            relief="solid", borderwidth=1, bg=pal["entry"], fg=pal["text"],
            insertbackground=pal["text"],
            highlightbackground=pal["entry_bd"], highlightthickness=1,
        )
        kit.reg(widget, "entry")
        if getattr(parent, "_spoofer_form", False):
            parent._field = widget
            widget.grid(row=0, column=1, sticky="w", padx=(0, 10), pady=5)
        else:
            widget.pack(side="left", padx=(0, 8))
        return var, widget

    def button(parent, text, command, kind="plain"):
        widget = kit.button(parent, text, command, kind=kind)
        if getattr(parent, "_spoofer_form", False):
            parent._action = widget
            widget.grid(row=0, column=2, sticky="e", padx=(0, 2), pady=5)
            parent.after_idle(parent._fit_form)
        else:
            widget.pack(side="left", padx=(0, 6), pady=3)
        return widget

    def tile_buttons(parent, columns=3):
        # Resizes every real kit.button background image together with the
        # column, so the drawn button and its hit area never disagree.
        items = [w for w in parent.winfo_children() if isinstance(w, tk.Button)]
        for w in items:
            w.pack_forget()
        state = {"layout": None, "pending": False}

        def _apply():
            state["pending"] = False
            if not _live(parent):
                return
            avail = max(200, parent.winfo_width())
            cols = max(1, min(columns, int(avail / 176)))
            cell = max(126, int(avail / cols) - 12)
            layout = (cols, cell)
            if layout == state["layout"]:
                return
            state["layout"] = layout
            for col in range(columns):
                parent.columnconfigure(col, weight=1 if col < cols else 0,
                                       uniform="spoofer_actions" if col < cols else "")
            for idx, w in enumerate(items):
                w.grid(row=idx // cols, column=idx % cols,
                       padx=5, pady=5, sticky="ew")
                w._fixed_w = cell
                kit.set_text(w, str(w.cget("text")))

        def schedule(_event=None):
            if not state["pending"]:
                state["pending"] = True
                parent.after_idle(_apply)
        parent.bind("<Configure>", schedule, add="+")
        schedule()

    def append_log(text, kind="info"):
        if log_box is None:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        try:
            log_box.configure(state="normal")
            log_box.insert("end", f"[{stamp}] {text}\n", kind)
            log_box.see("end")
            log_box.configure(state="disabled")
        except tk.TclError:
            pass

    def recolor_log():
        if log_box is None:
            return False
        try:
            log_box.tag_configure("ok", foreground=pal["green"])
            log_box.tag_configure("err", foreground=pal["accent"])
            log_box.tag_configure("warn", foreground=pal["amber"])
            log_box.tag_configure("info", foreground=pal["text"])
            log_box.configure(
                bg=pal["entry"], fg=pal["text"],
                insertbackground=pal["text"],
                highlightbackground=pal["entry_bd"],
            )
            return True
        except tk.TclError:
            return False

    def refresh_status():
        try:
            admin_text = "Administrator" if is_admin() else "Not elevated"
            status_widgets["admin"].configure(text="Access: " + admin_text)
            info = get_loading_status()
            if info.get("driver_active"):
                driver_text = "Kernel driver: active"
            elif info.get("driver_file"):
                state = info.get("service_state") or "not active"
                driver_text = "Kernel driver: bundled; service " + str(state)
            else:
                driver_text = "Kernel driver: bundled file missing"
            status_widgets["driver"].configure(text=driver_text)
            stats = get_audit_logger().get_stats()
            status_widgets["audit"].configure(
                text="Audit events: %d" % int(stats.get("total_events", 0))
            )
        except Exception as exc:
            append_log("Status refresh failed: %s" % exc, "warn")

    def refresh_snapshots():
        nonlocal snapshot_names
        try:
            snapshot_names = get_registry_engine().list_snapshots()
        except Exception as exc:
            snapshot_names = []
            append_log("Could not list snapshots: %s" % exc, "warn")
        if snapshot_list is None:
            return
        try:
            snapshot_list.delete(0, "end")
            for name in snapshot_names:
                snapshot_list.insert("end", name)
            if snapshot_names:
                snapshot_list.selection_set("end")
        except tk.TclError:
            pass

    def selected_snapshot():
        if snapshot_list is None:
            return None
        try:
            selection = snapshot_list.curselection()
            if not selection:
                return snapshot_names[-1] if snapshot_names else None
            index = int(selection[0])
            return snapshot_names[index] if index < len(snapshot_names) else None
        except (tk.TclError, ValueError):
            return None

    def run_operation(title, function, done=None, success_check=None):
        if busy["on"]:
            status_msg("Another spoofer operation is still running.")
            append_log("Ignored %s: busy." % title, "warn")
            return
        busy["on"] = True
        append_log("%s started..." % title, "info")
        status_msg("%s..." % title)

        def worker():
            try:
                value = function()
                results.put((title, value, None, done, success_check))
            except Exception as exc:
                results.put((title, None, exc, done, success_check))

        threading.Thread(
            target=worker, daemon=True,
            name="spoofer-%s" % title.lower().replace(" ", "-"),
        ).start()

    def poll_results():
        try:
            while True:
                title, value, error, done, success_check = results.get_nowait()
                busy["on"] = False
                if error is not None:
                    append_log("%s failed: %s" % (title, error), "err")
                    status_msg("%s failed." % title)
                else:
                    try:
                        ok = (success_check(value) if success_check
                              else (value.get("success", True)
                                    if isinstance(value, dict) else True))
                    except Exception:
                        ok = True
                    append_log("%s result:\n%s" % (title, _json_text(value)),
                               "ok" if ok else "err")
                    status_msg("%s finished." % title)
                    if done is not None:
                        try:
                            done(value)
                        except Exception as exc:
                            append_log("Post-action error: %s" % exc, "warn")
                refresh_status()
                refresh_snapshots()
        except queue.Empty:
            pass
        try:
            page.after(150, poll_results)
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- header
    header = card(inner)
    header.pack(fill="x", padx=14, pady=(12, 8))
    subtitle = label(
        header,
        "Change disk, network, SMBIOS and hardware identifiers. A registry "
        "snapshot is created before each registry write, and every operation "
        "is recorded in the audit log. Unsupported changes fail explicitly.",
        F_DESC, muted=True,
    )
    subtitle.pack(fill="x", pady=(2, 0))

    status_bar = card(inner)
    status_bar.pack(fill="x", padx=20, pady=(0, 14))
    status_widgets = {}
    for col, (key, text) in enumerate((
        ("admin", "Access: checking..."),
        ("driver", "Kernel driver: checking..."),
        ("audit", "Audit events: 0"),
    )):
        widget = tk.Label(
            status_bar, text=text, font=F_DESC, anchor="w",
            bg=pal["card"], fg=pal["muted"],
        )
        kit.reg(widget, "card_muted")
        widget.grid(row=col // 2, column=col % 2, sticky="w",
                    padx=(0, 16), pady=4)
        status_widgets[key] = widget
    refresh_button = kit.button(status_bar, "Refresh", refresh_status)
    refresh_button.grid(row=1, column=1, sticky="e", pady=4)
    status_bar.columnconfigure(0, weight=1)
    status_bar.columnconfigure(1, weight=1)

    # ----------------------------------------------------------- quick action
    quick = section(
        "Quick actions",
        "Safe spoof runs disk, NIC, and SMBIOS using only their available real "
        "methods. It reports a failure instead of claiming a simulated change.",
    )
    quick_row = card(quick)
    quick_row.pack(fill="x")

    def run_safe_spoof():
        disk_value = disk_var.get() or "C"
        nic_value = nic_var.get().strip() or None
        try:
            smbios_type = int(smbios_var.get() or "1")
        except ValueError:
            smbios_type = 1

        def work():
            if not get_loading_status().get("driver_active"):
                return {
                    "success": False,
                    "error": (
                        "quick spoof requires the kernel driver to be active; "
                        "no partial disk/NIC fallback changes were made"),
                }
            output = {}
            output["disk"] = get_disk_spoofer().spoof(disk_value)
            output["nic"] = get_nic_spoofer().spoof(nic_value)
            output["smbios"] = get_smbios_spoofer().spoof(smbios_type)
            output["success"] = all(
                bool(item.get("success")) for item in output.values()
                if isinstance(item, dict)
            )
            audit_log(
                AuditEventType.SPOOF_SUCCESS if output["success"]
                else AuditEventType.SPOOF_FAILURE,
                "spoof", "quick-actions", "safe-spoof", output,
                bool(output["success"]),
            )
            return output
        run_operation("Safe spoof", work)

    button(quick_row, "Run safe spoof", run_safe_spoof, kind="accent")
    button(quick_row, "Verify system", lambda: run_operation(
        "System verification", run_verification), kind="plain")

    def run_restore_point():
        run_operation(
            "System restore point",
            lambda: create_restore_point("Snappy Spoofer checkpoint"),
        )

    restore_button = button(
        quick_row, "Create restore point", run_restore_point, kind="amber")
    if not restore_point_supported():
        restore_button.configure(state="disabled")
    tile_buttons(quick_row, columns=3)

    # ------------------------------------------------------------------ disk
    disk = section(
        "Disk serial",
        "An active kernel driver applies its supported disk changes at load. "
        "Without it, the bundled Microsoft VolumeId tool changes the selected "
        "volume ID and requires administrator rights and a reboot.",
    )
    disk_row = row(disk, "Volume letter")
    disk_var, _disk_entry = entry(disk_row, "C", width=6)
    def run_disk_spoof():
        value = disk_var.get() or "C"
        run_operation("Disk serial", lambda: get_disk_spoofer().spoof(value))
    button(disk_row, "Spoof disk serial", run_disk_spoof, kind="accent")

    # --------------------------------------------------------------- network
    nic = section(
        "Network adapter (MAC)",
        "Writes NetworkAddress in the adapter's class key. Leave the filter "
        "empty to use the first physical adapter; restart the adapter for the "
        "new MAC to take effect.",
    )
    nic_row = row(nic, "Adapter filter")
    nic_var, _nic_entry = entry(nic_row, "", width=22)
    def run_nic_spoof():
        value = nic_var.get().strip() or None
        run_operation("NIC MAC", lambda: get_nic_spoofer().spoof(value))
    button(nic_row, "Spoof MAC", run_nic_spoof, kind="accent")

    # ---------------------------------------------------------------- smbios
    smbios = section(
        "SMBIOS",
        "Table type: 0 = BIOS, 1 = System, 2 = Baseboard, 3 = Chassis, "
        "4 = Processor, 17 = Memory device. Firmware-backed values need the "
        "kernel driver.",
    )
    smbios_row = row(smbios, "Table type")
    smbios_var, _smbios_entry = entry(smbios_row, "1", width=6)
    def run_smbios_spoof():
        value = smbios_var.get() or "1"
        run_operation("SMBIOS", lambda: get_smbios_spoofer().spoof(value))
    button(smbios_row, "Spoof SMBIOS", run_smbios_spoof, kind="accent")

    # ---------------------------------------------------------- kernel-only
    kernel = section(
        "Kernel-level identifiers",
        "These values live in kernel memory or device firmware. The buttons "
        "report requires_driver when the signed HWID driver is not loaded.",
    )
    kernel_row = card(kernel)
    kernel_row.pack(fill="x")

    def kernel_action(title, operation):
        run_operation(
            title,
            operation,
            success_check=lambda value: bool(value.get("success")),
        )

    button(kernel_row, "GPU UUID", lambda: kernel_action(
        "GPU UUID", lambda: get_gpu_spoofer().spoof()), kind="plain")
    button(kernel_row, "Boot ID", lambda: kernel_action(
        "Boot ID", lambda: get_boot_spoofer().spoof()), kind="plain")
    button(kernel_row, "Monitor EDID", lambda: kernel_action(
        "Monitor EDID", lambda: get_monitor_spoofer().spoof(0)), kind="plain")
    button(kernel_row, "Peripheral serial", lambda: kernel_action(
        "Peripheral serial", lambda: get_peripheral_spoofer().spoof("usb")),
        kind="plain")
    button(kernel_row, "Reload driver", lambda: run_operation(
        "Driver reload",
        lambda: {"success": bool(reload_driver().loaded),
                 "status": get_loading_status()},
    ), kind="plain")
    tile_buttons(kernel_row, columns=3)

    # --------------------------------------------------------------- cleanup
    clean = section(
        "Cleanup",
        "Registry keys and values are snapshotted before the app-owned tree is removed. "
        "File cleaning is limited to old app logs, app cache, and Snappy-owned "
        "temporary entries; it never sweeps the whole system temp folder.",
    )
    clean_row = card(clean)
    clean_row.pack(fill="x")

    def clean_operation(title, target):
        if not messagebox.askyesno(
            "Run cleanup",
            "Run '%s' now?\n\nA registry snapshot is created before the "
            "cleanup starts." % title,
            parent=page,
        ):
            return

        def work():
            cleaner = CleanerEngine()
            if target == "registry":
                return cleaner._clean_registry()
            if target == "files":
                return {"success": True, "cleaned": cleaner._clean_files()}
            if target == "temp":
                return {"success": True,
                        "cleaned": cleaner._clean_temp_files()}
            if target == "cache":
                return {"success": True, "cleaned": cleaner._clean_cache()}
            return cleaner.full_clean()
        run_operation(title, work)

    for text, target, kind in (
        ("Registry traces", "registry", "plain"),
        ("Files", "files", "plain"),
        ("Temp", "temp", "plain"),
        ("Caches", "cache", "plain"),
        ("Full clean", "full", "amber"),
    ):
        button(clean_row, text,
               lambda t=target, n=text: clean_operation(n, t), kind=kind)
    tile_buttons(clean_row, columns=3)

    # -------------------------------------------------------- backup/restore
    backup = section(
        "Backup & restore",
        "Snapshots are JSON files in registry_backups. Restoring writes the "
        "captured registry values back; restart affected services afterwards.",
    )
    backup_buttons = card(backup)
    backup_buttons.pack(fill="x", pady=(0, 6))

    def create_snapshot():
        def work():
            name = "manual_%s" % datetime.now().strftime("%Y%m%d_%H%M%S")
            return {"success": True,
                    "snapshot": get_registry_engine().create_snapshot(name)}
        run_operation("Create snapshot", work)

    def restore_snapshot():
        name = selected_snapshot()
        if not name:
            status_msg("Select a snapshot first.")
            append_log("No snapshot selected.", "warn")
            return
        if not messagebox.askyesno(
            "Restore registry values",
            "Restore the captured values from snapshot:\n\n%s\n\n"
            "This writes registry values back. Continue?" % name,
            parent=page,
        ):
            return
        run_operation(
            "Restore snapshot",
            lambda: {"success": get_registry_engine().restore_snapshot(name),
                     "snapshot": name},
        )

    def delete_snapshot():
        name = selected_snapshot()
        if not name:
            status_msg("Select a snapshot first.")
            return
        if not messagebox.askyesno(
            "Delete snapshot",
            "Delete snapshot '%s'?\n\nThis cannot be undone." % name,
            parent=page,
        ):
            return
        try:
            path = get_registry_engine().backup_dir / ("%s.json" % name)
            path.unlink(missing_ok=True)
            append_log("Deleted snapshot %s." % name, "warn")
            refresh_snapshots()
        except Exception as exc:
            append_log("Could not delete snapshot: %s" % exc, "err")

    button(backup_buttons, "Create snapshot", create_snapshot, kind="plain")
    button(backup_buttons, "Restore selected", restore_snapshot, kind="accent")
    button(backup_buttons, "Delete selected", delete_snapshot, kind="plain")
    button(backup_buttons, "Refresh list", refresh_snapshots, kind="plain")
    tile_buttons(backup_buttons, columns=2)

    snapshot_frame = card(backup)
    snapshot_frame.pack(fill="x")
    snapshot_list = tk.Listbox(
        snapshot_frame, height=5, font=F_ENTRY, activestyle="none",
        relief="solid", borderwidth=1, bg=pal["entry"], fg=pal["text"],
        highlightbackground=pal["entry_bd"], highlightthickness=1,
        selectbackground=pal["accent"], selectforeground="#ffffff",
        exportselection=False,
    )
    kit.reg(snapshot_list, "listbox")
    snapshot_list.pack(fill="x")

    # ------------------------------------------------------ audit/verify
    audit_section = section(
        "Audit & verification",
        "Verification is read-only: it reports the disk serial, MachineGuid, "
        "BIOS strings, SMBIOS hash, network overrides and display adapters.",
    )
    audit_row = card(audit_section)
    audit_row.pack(fill="x")

    def show_audit():
        try:
            logger = get_audit_logger()
            entries = logger.get_recent_entries(count=25)
            lines = []
            for item in entries:
                mark = "OK" if item.success else "FAIL"
                lines.append(
                    "%s %s %s %s" % (mark, item.timestamp[:19].replace("T", " "),
                                     item.event_type.value, item.target)
                )
            if not lines:
                lines = ["(no audit events yet)"]
            append_log("Recent audit events:\n" + "\n".join(lines), "info")
            status_msg("Showing %d audit events." % len(entries))
        except Exception as exc:
            append_log("Audit read failed: %s" % exc, "err")

    def export_audit():
        try:
            path = get_audit_logger().export_summary()
            append_log("Audit summary written to %s" % path, "ok")
            status_msg("Audit summary exported.")
        except Exception as exc:
            append_log("Audit export failed: %s" % exc, "err")

    button(audit_row, "Show recent audit", show_audit, kind="plain")
    button(audit_row, "Export summary", export_audit, kind="plain")
    button(audit_row, "Run verification", lambda: run_operation(
        "System verification", run_verification), kind="accent")
    tile_buttons(audit_row, columns=3)

    # ------------------------------------------------------- external tools
    tools = section(
        "Optional external tools",
        "Detected automatically in tools/, driver/, bin/, the current "
        "directory and PATH. They are not required for the integrated "
        "spoofers.",
    )
    tools_body = card(tools)
    tools_body.pack(fill="x")
    detected = {}
    try:
        detected = detect_external_tools()
    except Exception as exc:
        append_log("External tool detection failed: %s" % exc, "warn")
    tool_labels = {}
    for key, title_text in (("volumeid", "VolumeID64"),
                            ("amidewin", "AMIDEWINx64")):
        line = card(tools_body)
        line.pack(fill="x", pady=2)
        label(line, title_text, F_DESC, width=18).pack(side="left")
        value = detected.get(key)
        text = value if value else "not found"
        widget = label(line, text, F_DESC, muted=not bool(value))
        widget.pack(side="left")
        tool_labels[key] = widget

    # ----------------------------------------------------------- activity log
    log_section = section(
        "Activity log",
        "Every action and its JSON result appears here. The same events are "
        "written to the audit log.",
    )
    log_body = card(log_section)
    log_body.pack(fill="x")
    log_box = tk.Text(
        log_body, height=11, wrap="word", font=F_MONO,
        relief="solid", borderwidth=1, bg=pal["entry"], fg=pal["text"],
        insertbackground=pal["text"],
        highlightbackground=pal["entry_bd"], highlightthickness=1,
        state="disabled",
    )
    kit.reg(log_box, "listbox")
    log_box.pack(fill="x")
    recolor_log()
    kit.repaint(recolor_log)

    log_buttons = card(log_section)
    log_buttons.pack(fill="x", pady=(6, 0))

    def clear_log():
        try:
            log_box.configure(state="normal")
            log_box.delete("1.0", "end")
            log_box.configure(state="disabled")
        except tk.TclError:
            pass

    button(log_buttons, "Clear log", clear_log, kind="plain")
    tile_buttons(log_buttons, columns=1)

    # ------------------------------------------------------------------ init
    refresh_status()
    refresh_snapshots()
    append_log(
        "Spoofer ready. %s | %s" % (
            "Administrator" if is_admin() else "Not elevated",
            "kernel driver active" if get_loading_status().get("driver_active")
            else "kernel driver not active; only supported real fallbacks are available",
        ),
        "info",
    )
    if not restore_point_supported():
        append_log("System Restore API is not available on this system.", "warn")
    bind_wheel(inner)
    page.after(150, poll_results)
    return page
