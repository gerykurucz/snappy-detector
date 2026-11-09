
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
        def on_timer_persist(us):
            try:
                cfg.set("timer_resolution_us", int(us))
            except Exception:
                pass
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
F_TAB_OFF = ("Segoe UI", 10)

TOPBAR_H = 74
TOPBAR_LOGO_PX = 60

TOPBAR_TEXT_RAISE = 4

TOPBAR_TEXT_TOP = max(0, (TOPBAR_H - TOPBAR_LOGO_PX) // 2
                      - TOPBAR_TEXT_RAISE)

F_TOP_H1 = ("Segoe UI", 20, "bold")
F_TOP_LINE = ("Segoe UI", 10)

TOPBAR_BUILD_INDENT = 2

ENTITLEMENT_CHECK_MINUTES = 30

META = {

    "controller_input": ("Enable controller input",
        "Read a gamepad through Windows' own XInput so it can launch, pause, "
        "click and steer like the mouse, with the same bindings and macros. "
        "Controller OUTPUT (replaying recordings as a virtual gamepad) needs "
        "the optional ViGEmBus driver.",
        "bool", None),
    "pad_drive": ("Controller steers and clicks",
        "The left stick moves the cursor and the trigger is the click, so all "
        "assistance works from a controller alone. The right stick steers the "
        "view while the click is held.",
        "bool", None),
    "pad_output": ("Controller output method",
        "How controller recordings and 'pad:' macros leave the program: "
        "Automatic (ViGEmBus if installed, else keyboard/mouse), Virtual pad "
        "(ViGEmBus only) or Keyboard/mouse emulation. Applies on restart.",
        "map", (('Automatic', 'auto'),
                 ('Virtual pad (ViGEmBus)', 'vigem'),
                 ('Keyboard/mouse', 'emulate'))),
    "pad_launch": ("Controller: launch",
        "Controller button that starts assistance - one button ('start') or a "
        "combination ('rb+start'). Empty = unbound.",
        "pad", None),
    "pad_pause": ("Controller: pause",
        "Controller button that pauses assistance and hands all input back to "
        "you. Empty = unbound.",
        "pad", None),
    "pad_mode1": ("Controller: classic mode",
        "Controller button that switches to mode 1 (classic): a mis-click is "
        "caught, snapped and pressed for you. Empty = unbound.",
        "pad", None),
    "pad_mode2": ("Controller: guide mode",
        "Controller button that switches to mode 2 (guide): your click always "
        "goes through. Empty = unbound.",
        "pad", None),
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
                          "handling go straight to Windows.",
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
                     "Only detected people can be assisted; all other clicks "
                     "pass straight through untouched.",
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
    "reticle_mode": ("Reticle position",
                       "Centred on the screen, or a fixed custom spot "
                       "you choose with the button below.",
                       "map", (("Centred", "center"),
                               ("Custom position", "custom"))),
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
    "reticle_x": ("Custom X",
                    "Horizontal screen position for custom mode.",
                    "int", None),
    "reticle_y": ("Custom Y",
                    "Vertical screen position for custom mode.",
                    "int", None),
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
               "How far around the anchor assistance looks (40-400). The "
               "anchor is the screen centre in centre-watch mode, the cursor "
               "otherwise.", "int", None),
    "center_blind_area": ("Centre blindness threshold",
               "Centre-watch only: ignore tiny items sitting right on the "
               "centre anchor, e.g. a drawn reticle (900 ~= 30x30 px). "
               "0 disables.", "int", None),
    "center_max_step": ("Steering step cap (px)",
               "Largest single correction of the classic steerer (0 = "
               "uncapped). The Unibot steering loop is exempt; click snaps "
               "stay capped.", "int", None),
    "subject_focus_bias": ("Subject aim bias (0-100)",
               "For tall 1:2 body-shape targets, move the snap point from "
               "the middle toward the box top by this share. 0 (default) "
               "off.", "int", None),
    "motion_sense": ("Motion sense (0-100)",
               "Centre-watch only: moving targets outrank static ones in "
               "close calls. 0 (default) off; brief flashes are ignored.",
               "int", None),
    "on_target_dist": ("On-target distance (px)",
                        "Clicks landing this close to a target's middle "
                        "are left alone - you already hit it.",
                        "int", None),
    "glide_ms": ("Animate cursor moves",
                  "0 (default) jumps instantly; 40-120 glides so you can "
                  "watch. Moving the mouse yourself cancels a glide.",
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
                    "Only items containing your colour can be assisted; all "
                    "other clicks pass through untouched. Needs colours set "
                    "first.", "bool", None),
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
                   "Follow moving targets by position AND size: single bad "
                   "scans are ignored, turns are followed, and a sudden size "
                   "jump restarts the track.",
                   "bool", None),
    "motion_predict": ("Lead moving items",
                       "For items that move while you click (scrolling "
                       "lists, video seek bars), move slightly ahead so "
                       "the click lands on the item, not where it was. "
                       "Still items are unaffected.", "bool", None),
    "motion_lead_ms": ("Lead time (ms)",
                       "How far ahead to lead a moving item (0-300); the "
                       "lead is capped at 80 px and switches off for slow or "
                       "turning movement.",
                       "int", None),
    "hpose_mode": ("Person detection (hpose)",
                   "On: an ONNX person detector answers first - it knows "
                   "facing and posture - and the edge scanner takes over "
                   "when it finds nobody. Needs onnxruntime; models sit in "
                   "the models folder.",
                   "map", (('Off', 'off'), ('On', 'on'))),
    "hpose_view": ("Required facing",
                   "Only people facing this way are aimed at: facing the "
                   "screen, turned away, or side profile. Any takes "
                   "everyone.",
                   "map", (('Any', 'any'), ('Facing the screen', 'facing'),
                              ('Turned away', 'away'),
                              ('Side profile', 'side'))),
    "hpose_stance": ("Required posture",
                      "Only people in this posture are aimed at; Low covers "
                      "crouching and prone. Any takes everyone.",
                      "map", (('Any', 'any'), ('Standing', 'standing'),
                                 ('Crouching', 'crouch'), ('Prone', 'prone'),
                                 ('Walking', 'walk'),
                                 ('Low (crouch or prone)', 'low'))),
    "hpose_aim": ("Aim point on the body",
                  "The middle of the body or the head; the body middle "
                  "moves less and is steadier.",
                  "map", (('Middle of the body', 'center'),
                             ('Head', 'head'))),
    "hpose_lead_ms": ("Person lead (ms)",
                      "How far ahead to lead a moving person (0-300); 40-80 "
                      "suits walking. Only applied to a target that is "
                      "actually moving, capped at 120 px.",
                      "int", None),
    "hpose_min_conf": ("Minimum confidence",
                       "Detector confidence required before a person is "
                       "aimed at (10-90). Higher is pickier, lower finds "
                       "people sooner.",
                       "int", None),
    "hpose_model": ("Detection model",
                    "Which ONNX pose model to use, from the models folder.",
                    "map", (('Fast (yolo26 nano)', 'yolo26n-pose.onnx'),
                               ('Accurate (yolo26 large)',
                                'yolo26l-pose.onnx'),
                               ('Legacy (yolo11 nano)',
                                'yolo11n-pose.onnx'))),
    "blob_watch": ("Blob watcher fallback",
                   "While person detection is on and finds nobody, also aim "
                   "at person-shaped high-contrast blobs (about twice as "
                   "tall as wide, 14-64 px).",
                   "bool", None),

    # YOLO26n 2D person detector (fast, no pose - pure YOLO)
    "yolo26n_enabled": ("YOLO26n 2D person detector",
                         "Fast 2D person bounding-box detector (YOLO26n). "
                         "Runs at ~100+ FPS on CPU. No pose/3D, just boxes. "
                         "Recommended as primary detector for speed.",
                         "bool", None),
    "yolo26n_input_size": ("YOLO26n input resolution",
                            "Input image size for YOLO26n. Lower = faster. "
                            "224 = fastest (~130 FPS), 320 = balanced (~50 FPS), "
                            "640 = most accurate (~12 FPS). Requires model export.",
                            "map", (("Fastest (224)", "224"),
                                     ("Balanced (320)", "320"),
                                     ("Accurate (640)", "640"))),
    "yolo26n_conf": ("YOLO26n confidence threshold",
                      "Minimum confidence for person detection (0.05-0.95). "
                      "Lower finds more people, higher is stricter.",
                      "float", None),
    "yolo26n_fallback_to_hpose": ("Fallback to hpose when YOLO misses",
                                     "If YOLO26n finds no person, try hpose "
                                     "(pose/3D) as a backup. Slower but smarter.",
                                     "bool", None),
    "yolo26n_model": ("YOLO26n model file",
                       "ONNX model file in the models folder.",
                       "map", (("Standard (yolo26n.onnx)", "yolo26n.onnx"),
                                  ("INT8 Quantized (yolo26n_int8.onnx)", "yolo26n_int8.onnx"))),
    "follow_enabled": ("Remember the last clicked item",
                       "After a successful snap, similar-looking items or "
                       "items in the same place win ties for a while - "
                       "handy for scrollbars and repeated clicks.",
                       "bool", None),
    "watch_center_only": ("Centre-watch mode",
                          "Search only inside a square at the screen centre "
                          "(its width = search distance) instead of around "
                          "the cursor: clicks inside are assisted, clicks "
                          "outside pass through untouched. The game mode "
                          "for a fixed centre crosshair.",
                          "bool", None),
    "center_follow": ("Keep the crosshair on it",
                      "Centre-watch only: once a target inside the square is "
                      "picked, assistance steers to keep it centred - also "
                      "while a mouse button is held (steered by mouse deltas "
                      "when 'Unibot aim steering' is on, by a virtual stick "
                      "otherwise).",
                      "bool", None),
    "unibot_aim": ("Unibot aim steering",
                   "Centre-watch only: steer the view with exact "
                   "relative-mouse deltas each frame, pulling the target "
                   "onto the screen centre while a mouse button is held "
                   "(needs the driver input backend in games). Off: steering "
                   "goes through a virtual gamepad stick.",
                   "bool", None),
    "aim_speed": ("Steering gain",
                  "Multiplier on each Unibot steering delta (0.1-2.0); "
                  "above 1 steers faster, below 1 softer.",
                  "float", None),
    "aim_y_speed_multiplier": ("Vertical steering gain",
                  "Extra multiplier on the Y axis only (0.1-2.0); "
                  "allows separate X/Y tuning like Unibot's y_speed_multiplier.",
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
                 "Replays the keyboard recording selected in the Macros tab, "
                 "or the newest one when nothing is selected. Pressed on "
                 "a repeating recording, it stops it.", "str", None),
    "key_mode1": ("Shortcut: classic mode (up to 4 keys)",
                  "Comma-separated, e.g. '1, f1, ctrl+f1, v'. See the "
                  "Keybindings tab for what classic mode does.",
                  "str", None),
    "key_mode2": ("Shortcut: guide mode (up to 4 keys)",
                  "Comma-separated, e.g. '2, f2, ctrl+f2, b'. See the "
                  "Keybindings tab for what guide mode does.",
                  "str", None),
    "key_assist_off": ("Shortcut: turn assistance off (up to 4 keys)",
                       "Comma-separated, e.g. 'ctrl+alt+o, end'. Pressing any "
                       "of these stops assisting without closing the program - "
                       "your input is yours again. Press launch or a mode key "
                       "to assist again. Up to 4 keys.", "str", None),
    "pad_assist_off": ("Controller: turn assistance off (up to 4 buttons)",
                       "Comma-separated controller buttons, e.g. 'back, "
                       "lb+rb'. Pressing any of these stops assisting without "
                       "closing the program. Up to 4 buttons.", "pad", None),
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

    "contrast_threshold": ("Edge threshold",
                           "Lower finds fainter items (and more noise); "
                           "higher needs crisper edges. Raising above the 38 "
                           "default turns off the auto-adjust for this "
                           "control.",
                           "int", None),
    "min_contrast": ("Minimum edge strength",
                     "An item must be at least this crisp to be clickable; "
                     "below 28 counts as 28. Raising above 48 turns off the "
                     "auto-adjust for this control.",
                     "int", None),
    "adaptive_threshold": ("Auto-adjust for dim screens",
                           "The detection bar lowers itself for dark themes "
                           "and washed-out content, so faint items are "
                           "still found. Recommended on; stands aside when "
                           "you deliberately raise either contrast control.",
                           "bool", None),
    "luminance_norm": ("Judge edges by relative brightness",
                       "Edges are scored against how bright their "
                       "surroundings are, so dark-mode items rank like "
                       "light-mode ones. Recommended on.",
                       "bool", None),
    "min_area": ("Smallest item size",
                 "Smaller blobs are ignored as specks, unless they are "
                 "glued to a bigger item.", "int", None),
    "max_area": ("Largest item size",
                 "Bigger blobs are ignored - usually whole windows or "
                 "background, not clickable items.", "int", None),
    "max_dim": ("Largest item width or height",
                "Items wider or taller than this are ignored; items at "
                "a screen edge, like scrollbars, get double.",
                "int", None),
    "stall_pause_seconds": ("Pause after a hiccup (s)",
                             "If a scan ever stalls, assistance pauses this "
                             "long and any held click is given back to you.",
                             "float", None),

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

    "snap_offset_x": ("Click offset X (px)",
                      "Moves the click point sideways from the middle of "
                      "the item (-200 to 200, negative goes left); for "
                      "controls whose middle is dead.",
                      "int", None),
    "snap_offset_y": ("Click offset Y (px)",
                      "Moves the click point vertically from the middle of "
                      "the item (-200 to 200, negative goes up); for "
                      "controls whose middle is dead.",
                      "int", None),
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
        "Recordings list in the Macros tab from a "
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
        "through the pad_emulate keyboard/mouse table without it. Empty = "
        "macro off.",
        "str", None)

for _i in range(1, REC_PLAY_SLOTS + 1):
    META["key_play%d" % _i] = (
        "Shortcut: play recording #%d" % _i,
        "Replays keyboard recording #%d of the numbered Recordings list in "
        "the Macros tab, "
        "by its position in that list. If the list is shorter than "
        "that, nothing happens." % _i,
        "str", None)

TAB_ASSIST = ["controller_input", "pad_drive",
              "shift_bypass", "subject_priority", "subject_only",
              "debug_visualize",
              "hold_follow", "rapid_click_snap",
              "follow_enabled", "motion_predict", "motion_3d",
              "watch_center_only",
              "center_follow", "unibot_aim",
              "accent_only",
              "adaptive_threshold", "luminance_norm",
              "color_adapt", "accent_variants", "accent_coherent"]
TAB_DETECT = ["accent_color", "exclude_color"]
TAB_RETICLE = ["reticle_enabled", "reticle_shape", "reticle_mode",
                 "reticle_size", "reticle_gap", "reticle_thickness",
                 "reticle_rotation", "reticle_opacity", "reticle_color",
                 "reticle_x", "reticle_y"]
TAB_KEYS = ["key_launch", "key_pause", "key_mode1", "key_mode2",
            "key_assist_off",
            "key_record", "key_record_stop", "key_play", "key_play1",
            "key_play2", "key_play3", "key_play4", "key_play5"]
TAB_NAMES = ["assist", "handler", "advanced", "experimental", "keyboard",
             "controller", "macros", "spoofer", "reticle", "latency_fix",
             "latency_live", "latency_tools", "help"]

TAB_PAD = ["pad_launch", "pad_pause", "pad_mode1", "pad_mode2",
           "pad_assist_off",
           "pad_record", "pad_record_stop", "pad_play",
           "pad_play1", "pad_play2", "pad_play3", "pad_play4", "pad_play5"]
TAB_MOUSE = ["trigger_button", "options_button"]
TAB_KEYBOARD = ["trigger_button", "options_button", "bypass_key",
                "key_launch", "key_pause", "key_mode1", "key_mode2",
                "key_assist_off",
                "key_record", "key_record_stop", "key_play", "key_play1",
                "key_play2", "key_play3", "key_play4", "key_play5"]
TAB_CONTROLLER = ["pad_click_button", "pad_right_button", "bypass_pad",
                  "pad_launch", "pad_pause", "pad_mode1", "pad_mode2",
                  "pad_assist_off",
                  "pad_record", "pad_record_stop", "pad_play",
                  "pad_play1", "pad_play2", "pad_play3", "pad_play4",
                  "pad_play5"]
TAB_COLOR = ["contrast_threshold", "min_contrast",
             "min_area", "max_area", "max_dim",
             "accent_tolerance", "accent_soft", "accent_boost"]
TAB_ADV = ["radius", "center_blind_area", "center_max_step", "aim_speed", "aim_y_speed_multiplier", "subject_focus_bias", "motion_sense", "motion_camera_comp", "on_target_dist", "glide_ms", "watcher_seconds",
           "stall_pause_seconds", "merge_distance",
           "motion_lead_ms", "follow_seconds", "scan_budget_ms",
           "hpose_lead_ms", "hpose_min_conf",
           "snap_offset_x", "snap_offset_y"]

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
_CAPTURE_KIND["key_assist_off"] = "combo"
_CAPTURE_KIND["pad_assist_off"] = "pad"
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
        "item and Snappy AI clicks it for you. 'Search distance (px)' "
        "decides the search area, anchored on the screen centre in "
        "centre-watch mode - raise it when the item you want is just "
        "out of reach, lower it when the wrong neighbour wins.Holding the bypass key (Keyboard & Mouse tab, Shift by "
        "default) lets one click through untouched in both modes, and which "
        "button assistance listens to at all is yours to move on the "
        "Keyboard & Mouse tab.",
    "help.3.t": "Nothing is found - or too much is",
    "help.3.b":
        "On the Detection tab, 'Edge threshold' and 'Minimum edge "
        "strength' decide which edges count at all: lower them to "
        "find fainter items, raise them to ignore noise. 'Smallest "
        "item size', 'Largest item size' and 'Largest item width or "
        "height' drop specks and whole windows. 'Merge nearby blobs "
        "(px)' joins a broken outline, or an icon beside its label, "
        "into one item. On the Assistance tab, 'Auto-adjust for dim "
        "screens' and 'Judge edges by relative brightness' make dark "
        "themes behave like light ones - keep both on.",
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
        "and moves ahead by the 'Lead time (ms)' setting, so the click "
        "lands on a scrolling list or a video seek bar instead of "
        "behind it. The lead caps at 80 pixels and switches itself off "
        "for still or turning items.",
    "help.7.t": "How clicks feel",
    "help.7.b":
        "'Animate cursor moves' = 0 jumps instantly; 40-120 glides so "
        "you can watch, and moving the mouse yourself cancels the "
        "glide. 'Double-click window (s)' turns a second snap near the "
        "same spot into a double-click in classic mode. 'Assist every "
        "click in a fast burst' assists every click of a rapid series "
        "instead of only the first. 'Pause after a hiccup' is the "
        "safety valve that gives a held click back if a scan ever "
        "stalls.",
    "help.8.t": "Keys, macros and recordings",
    "help.8.b":
        "The Keybindings tab explains the two modes and the recording "
        "workflow. In short: shortcuts with Ctrl, Alt, Shift or Win "
        "are reserved system-wide; bare single keys fire everywhere "
        "and still type. A macro turns one blocked key press into one "
        "whole combo. Recordings capture your real input - every key, "
        "mouse click and cursor move, or every controller button and "
        "stick move - and replay it from the Macros tab, which keeps a "
        "keyboard library and an independent controller library side by "
        "side, each with its own play-once or repeat-until-stopped "
        "switch.",
    "help.9.t": "Interface language",
    "help.9.b":
        "The interface is English.",
    "help.10.t": "When it misbehaves, check in this order",
    "help.10.b":
        "1. Is it launched? Paused means nothing is intercepted - "
        "'Shortcut: start assisting' (Ctrl+L by default) is the only "
        "on/off.  2. Hold Shift for one click to confirm the "
        "assistance was the problem.  3. Wrong item wins: lower "
        "'Search distance (px)', then set 'My colour(s)' or "
        "raise 'Colour tie-break strength'.  4. Nothing is found: "
        "lower 'Edge threshold' and 'Minimum edge strength', raise "
        "'Largest item width or height', or raise 'Merge nearby blobs "
        "(px)'.  5. One thing must never be assisted: put "
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
    "mac.row": "Macro %d",
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
                "a combination ('start', 'rb+a', 'ls+rs'), or press the Press "
                "button and then press the controller. A combination that "
                "contains a shorter bound combination wins, so binding "
                "'rb+a' never also fires 'a'. Leave a box empty to unbind "
                "that action. These need 'Enable controller input' on the "
                "Assistance tab, and they work while launched - paused means "
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
    "mac.pad_row": "Pad %d",
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
    "tab.handler": "Detection",
    "tab.advanced": "Advanced",
    "tab.experimental": "Experimental",
    "tab.help": "Help",
    "mac.keys_head": "Keyboard Macros",
    "mac.recordings": "Recordings",
    "mac.pad_recordings": "Controller Recordings",
    "mac.sends": "sends",
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
        "Switch on 'Enable controller input' and a gamepad becomes a full "
        "second input device, read through Windows' own XInput - no driver, no "
        "extra software. The left stick steers the cursor, the right trigger "
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
        "to the keyboard and mouse using the pad_emulate table in config.ini. "
        "Which button clicks and which one carries the extra actions is on "
        "this tab, together with the controller "
        "bypass. Tuning lives in config.ini: pad_sensitivity, pad_deadzone, "
        "pad_trigger_threshold, pad_poll_hz, pad_index, pad_rumble, "
        "pad_drive and pad_output.",
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
        content_span = box[6]
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
        sb.pack(side="right", fill="y")
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
                                             "Listbox"):
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

def capture_button(parent, kit, root, app, key, var, kind, status_msg, R):
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

def _widget_class(root, widget):
    try:
        if isinstance(widget, str):
            return str(root.tk.call("winfo", "class", widget))
        return widget.winfo_class()
    except Exception:
        return ""

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

    gw, gh = min(980, max(640, sw - 24)), min(770, max(480, sh - 24))
    root.geometry("%dx%d" % (gw, gh))
    root.minsize(min(860, gw), min(640, gh))
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

    topbar = tk.Frame(root, bg=pal["card"])
    kit.reg(topbar, "card")
    topbar.pack(fill="x", padx=10, pady=(0, 6))

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

    _mode_lbl = tk.Label(right, text="", font=F_BOLD, bg=pal["card"],
                         fg=pal["text"])
    kit.reg(_mode_lbl, "card_bold")

    def _mode_tick():
        if not _live(_mode_lbl):
            return
        try:
            m = int(cfg.get("mode", 2))
        except (TypeError, ValueError):
            m = 2
        want = i18n.L("top.mode1" if m == 1 else "top.mode2")
        try:
            if _mode_lbl.cget("text") != want:
                _mode_lbl.configure(text=want)
        except tk.TclError:
            return
        root.after(500, _mode_tick)

    _mode_lbl.pack(side="left", padx=(0, 14))
    _mode_tick()
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

    bar = tk.Frame(root, bg=pal["tab_bg"])
    kit.reg(bar, "tab_bg")
    bar.pack(fill="x", padx=10)

    tab_names = list(TAB_NAMES)
    tab_btns = {}
    pages = {}
    current = {"tab": tab_names[0]}

    content_host = tk.Frame(root, bg=pal["card"])
    kit.reg(content_host, "card")
    content_host.pack(fill="both", expand=True, padx=10, pady=(0, 4))

    def show_tab(name):
        current["tab"] = name
        for tn, b in tab_btns.items():
            sel = (tn == name)
            fill = pal["card"] if sel else pal["tab_bg"]
            b.configure(font=(F_TAB if sel else F_TAB_OFF),
                        fg=(pal["accent"] if sel else pal["muted"]),
                        bg=pal["tab_bg"], activebackground=pal["tab_bg"],
                        activeforeground=(pal["accent"] if sel
                                          else pal["muted"]),
                        bd=0)
            meta = getattr(b, "_tmeta", None)
            if meta and HAVE_PIL:
                try:
                    img = kit.rounded(meta[0], 34 if sel else 28, fill, 6)
                    b.configure(image=img, compound="center")
                    b._img = img
                except Exception:
                    pass
        for tn, pg in pages.items():
            if tn == name:
                pg.pack(fill="both", expand=True)
            else:
                pg.pack_forget()
    kit.repaint(lambda: show_tab(current["tab"]))

    tab_rows = []
    for _i in range(len(tab_names)):
        fr = tk.Frame(bar, bg=pal["tab_bg"])
        kit.reg(fr, "bg")
        tab_rows.append(fr)

    def _tab_row(i):
        return tab_rows[i]

    for tn in tab_names:
        text = "  %s  " % i18n.L("tab." + tn)
        b = tk.Button(bar, text=text, takefocus=0, bd=0,
                      highlightthickness=0, relief="flat",
                      cursor="hand2",
                      command=lambda n=tn: show_tab(n))
        f = tkfont.Font(font=F_TAB)
        b._tmeta = (int(f.measure(text)) + 12,)
        b._tabrow = None
        b._tabside = None
        tab_btns[tn] = b
        pages[tn] = tk.Frame(content_host, bg=pal["card"])
        kit.reg(pages[tn], "card")

        def _retab(b=b, tn=tn):
            if not _live(b):
                return False
            t = "  %s  " % i18n.L("tab." + tn)
            b.configure(text=t)
            b._tmeta = (int(tkfont.Font(font=F_TAB).measure(t)) + 12,)
            layout_tabs()
        R(_retab)

    def layout_tabs(_event=None):
        try:
            avail = bar.winfo_width()
            if avail < 200:
                avail = root.winfo_width() - 24
            if avail < 200:
                return
        except Exception:
            return
        row = 0
        used = 0
        for tn in tab_names:
            b = tab_btns[tn]
            w = b._tmeta[0] + 8
            if used and used + w > avail:
                row += 1
                row = min(row, len(tab_rows) - 1)
                used = 0
            used += w
            side = "left"
            gap = (3, 0)
            if b._tabrow != row or b._tabside != side:
                b.pack_forget()
                b.pack(in_=_tab_row(row), side=side, padx=gap)
                b._tabrow = row
                b._tabside = side
        for i, fr in enumerate(tab_rows):
            if i <= row:
                fr.pack(fill="x")
            else:
                fr.pack_forget()

    bar.bind("<Configure>", layout_tabs)
    try:
        root.after(60, layout_tabs)
    except Exception:
        pass

    desc_row = tk.Frame(root, bg=pal["desc_bg"])
    kit.reg(desc_row, "desc_frame")
    desc_row.pack(fill="x", padx=10)

    desc_var = tk.StringVar(value=i18n.L("desc.default"))

    desc_bar = tk.Label(desc_row, textvariable=desc_var, font=F_DESC,
                        bg=pal["desc_bg"], fg=pal["muted"], anchor="w",
                        justify="left", padx=12, pady=6, wraplength=660)
    kit.reg(desc_bar, "desc")
    desc_bar.pack(side="left", fill="both", expand=True)

    DESC_MAX_LINES = 5

    def _desc_height(_event=None):
        try:
            w = desc_row.winfo_width()
            if w > 80:
                want = max(160, w - 24)
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

    footer = tk.Frame(root, bg=pal["bg"])
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

    status_row = tk.Frame(root, bg=pal["status_bg"])
    kit.reg(status_row, "status_frame")
    status_row.pack(fill="x", side="bottom")

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

    def make_row(parent, key, row):
        label, desc, kind, choices = META[key]
        if key not in vars_:
            cur = cfg.get(key)
            var = (tk.BooleanVar(value=bool(cur)) if kind == "bool"
                       else tk.StringVar(value=map_display(key, cur) if kind == "map" else str(cur)))
            vars_[key] = (kind, var)
            var.trace_add("write", mark_dirty)
        else:
            kind, var = vars_[key]

        cell = tk.Frame(parent, bg=pal["card"])
        kit.reg(cell, "card")
        cell.grid(row=row, column=0, sticky="we", padx=10, pady=4)
        cell.columnconfigure(0, weight=1)

        lbl = tk.Label(cell, text=tr_label(key), font=F_BOLD,
                       bg=pal["card"], fg=pal["text"], anchor="w")
        kit.reg(lbl, "card_bold")
        lbl.grid(row=0, column=0, sticky="w", padx=(0, 10))
        lbl.bind("<Enter>", lambda e, k=key: set_desc(
            lambda: tr_desc(k)))
        lbl.bind("<Leave>", lambda e: set_desc(None))
        R(lambda l=lbl, k=key: l.configure(text=tr_label(k))
          if _live(l) else False)

        if kind == "bool":

            w = kit.bool_switch(cell, var)
        elif kind == "map":

            w = _ChoiceDropdown(cell, kit, var=var)
            w.set_pairs(map_pairs_live(key))

            def _revals(w=w, k=key):
                if not _live(w):
                    return False
                w.set_pairs(map_pairs_live(k))
                return True

            R(_revals)

            if key == "input_backend":

                note = tk.Label(cell, text="", font=F_DESC,
                                bg=pal["card"], fg=pal["muted"],
                                anchor="e", justify="right")
                kit.reg(note, "card_muted")
                note.grid(row=1, column=0, columnspan=2, sticky="e")

                def _avail_probe(txt=note):
                    try:
                        from input import InterceptionBackend
                        driver_ok = InterceptionBackend.available()
                    except Exception:
                        driver_ok = False

                    def _paint(driver_ok=driver_ok, txt=txt):
                        if not _live(txt):
                            return False
                        txt.configure(text=(
                            "Driver ready."
                            if driver_ok else
                            "No driver found here: that choice keeps "
                            "the standard Windows method."))
                        return True
                    try:
                        root.after(0, _paint)
                    except Exception:
                        pass

                threading.Thread(target=_avail_probe, daemon=True).start()
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
            capture_button(cell, kit, root, app, key, var, kind,
                           status_msg, R).grid(row=0, column=2, sticky="ns",
                                               padx=(6, 0))
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

    def _range_widget(cell, key, var, rng, dflt):
        lo, hi = rng
        integer = not isinstance(dflt, float)
        host = tk.Frame(cell, bg=pal["card"])
        kit.reg(host, "card")
        sl = _RangeSlider(host, kit, var=var, lo=lo, hi=hi, integer=integer)
        sl.pack(side="right")
        sl.bind("<Enter>", lambda e, k=key: set_desc(lambda: tr_desc(k)))
        sl.bind("<Leave>", lambda e: set_desc(None))
        val = tk.Label(host, width=VAL_W, font=F_BASE, anchor="e",
                       bg=pal["card"], fg=pal["text"])
        kit.reg(val, "card_text")
        val.pack(side="right", padx=(0, 10))
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
        grid.pack(fill="x", padx=6, pady=8)
        grid.columnconfigure(0, weight=1)
        for i, key in enumerate(keys):
            make_row(grid, key, i)

    assist_page = pages["assist"]
    fill_list(assist_page, TAB_ASSIST)

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

    ch_page = pages["reticle"]
    ch_host, ch_bind = _scrollable(ch_page, kit, pal)
    _FIT.append(ch_host)
    ch_grid = tk.Frame(ch_host, bg=pal["card"])
    kit.reg(ch_grid, "card")
    ch_grid.pack(fill="x", padx=6, pady=8)
    ch_grid.columnconfigure(0, weight=1)

    _ch_pv = tk.Canvas(ch_grid, width=240, height=140, bg=pal["entry"],
                       highlightthickness=1,
                       highlightbackground=pal["entry_bd"], takefocus=0)
    _ch_pv.grid(row=0, column=0, pady=(0, 4))

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
            reticle.render(
                _ch_pv, w / 2, h / 2, _ch_val("reticle_shape", "cross"),
                size * sc, gap * sc,
                int(float(_ch_val("reticle_thickness", 2) or 2)),
                float(_ch_val("reticle_rotation", 0) or 0),
                reticle.to_hex(reticle.parse_color(
                    _ch_val("reticle_color", "0,255,0"))))

            # Show where the reticle will land on screen: centred mode draws at
            # the screen centre (canvas centre here); custom mode shows the
            # configured offset from screen centre so the preview matches the
            # on-screen position instead of always drawing in the middle.
            try:
                mode = str(_ch_val("reticle_mode", "center") or "center")
            except (TypeError, ValueError):
                mode = "center"
            if mode.strip().lower() == "custom":
                try:
                    px = int(_ch_val("reticle_x", 0) or 0)
                    py = int(_ch_val("reticle_y", 0) or 0)
                except (TypeError, ValueError):
                    px = py = 0
                sw = _ch_pv.winfo_screenwidth()
                sh = _ch_pv.winfo_screenheight()
                if sw > 0 and sh > 0:
                    scale = min(1.0, (min(w, h) - 16) / max(1.0, float(max(sw, sh))))
                    ox = w / 2 + (px - sw / 2) * scale
                    oy = h / 2 + (py - sh / 2) * scale
                    r0 = max(2.0, 4.0 * sc)
                    _ch_pv.create_oval(ox - r0, oy - r0, ox + r0, oy + r0,
                                       fill=pal["accent"], outline="")
                    _ch_pv.create_text(ox, oy - r0 - 6, anchor="s",
                                       text="custom", font=F_DESC,
                                       fill=pal["accent"])
        except Exception:
            pass
        return True
    kit.repaint(ch_preview)

    for _i, _key in enumerate(TAB_RETICLE):
        make_row(ch_grid, _key, _i + 1)
    for _key in TAB_RETICLE:
        try:
            vars_[_key][1].trace_add(
                "write", lambda *_a: ch_preview())
        except (KeyError, tk.TclError):
            pass
    ch_preview()

    _ch_sw = tk.Frame(ch_grid, bg=pal["card"])
    kit.reg(_ch_sw, "card")
    _ch_sw.grid(row=len(TAB_RETICLE) + 1, column=0, sticky="we",
                padx=10, pady=4)
    _ch_sw_lbl = tk.Label(_ch_sw, text=i18n.L("xh.swatches"), font=F_BOLD,
                          bg=pal["card"], fg=pal["text"], anchor="w")
    kit.reg(_ch_sw_lbl, "card_bold")
    _ch_sw_lbl.pack(side="left", padx=(0, 8))
    R(lambda l=_ch_sw_lbl: l.configure(text=i18n.L("xh.swatches"))
      if _live(l) else False)
    for _tok in ("255,0,0", "0,255,0", "0,128,255", "255,255,0",
                 "255,0,255", "0,255,255", "255,255,255"):
        try:
            _rgb = tuple(int(p) for p in _tok.split(","))
        except ValueError:
            continue
        _b = tk.Button(_ch_sw,
                       bg="#%02x%02x%02x" % _rgb, activebackground="#%02x%02x%02x" % _rgb,
                       width=3, relief="solid", borderwidth=1,
                       cursor="hand2", takefocus=0,
                       command=lambda t=_tok: vars_["reticle_color"][1].set(t)
                       if "reticle_color" in vars_ else None)
        try:
            kit.reg(_b, "hl")
        except Exception:
            pass
        _b.pack(side="left", padx=2)

    _ch_pos = tk.Frame(ch_grid, bg=pal["card"])
    kit.reg(_ch_pos, "card")
    _ch_pos.grid(row=len(TAB_RETICLE) + 2, column=0, sticky="we",
                 padx=10, pady=4)

    def _ch_to_cursor():
        if get_cursor_pos is None:
            status_msg("Cursor position is unavailable on this platform.")
            return
        try:
            x, y = get_cursor_pos()
        except Exception as exc:
            status_msg("Could not read the cursor (%r)." % (exc,))
            return
        try:
            vars_["reticle_x"][1].set(str(int(x)))
            vars_["reticle_y"][1].set(str(int(y)))
            vars_["reticle_mode"][1].set(
                map_display("reticle_mode", "custom"))
        except (KeyError, tk.TclError, ValueError):
            pass

    _b_cur = kit.button(_ch_pos, i18n.L("xh.set_cursor"), _ch_to_cursor)
    _b_cur.pack(side="left", padx=(0, 6))
    R(lambda: kit.set_text(_b_cur, i18n.L("xh.set_cursor"))
      if _live(_b_cur) else False)

    _ch_pre_lab = tk.Label(_ch_pos, text=i18n.L("xh.preset"), font=F_BOLD,
                           bg=pal["card"], fg=pal["text"])
    kit.reg(_ch_pre_lab, "card_bold")
    _ch_pre_lab.pack(side="left", padx=(6, 4))
    R(lambda l=_ch_pre_lab: l.configure(text=i18n.L("xh.preset"))
      if _live(l) else False)
    _ch_pre_var = tk.StringVar(value="")
    _ch_pre_box = _ChoiceDropdown(_ch_pos, kit, var=_ch_pre_var,
                                  width=176,
                                  on_select=lambda: _ch_pre_load())
    _ch_pre_box.pack(side="left")

    def _ch_pre_names():
        try:
            return sorted(reticle.list_presets())
        except Exception:
            return []

    def _ch_pre_sync():
        if not _live(_ch_pre_box):
            return False
        _ch_pre_box.set_pairs([(n, n) for n in _ch_pre_names()])
        return True
    R(_ch_pre_sync)

    def _ch_pre_load(_event=None):
        name = _ch_pre_var.get()
        try:
            data = reticle.load_preset(name)
        except Exception:
            data = None
        if not data:
            return
        for k, v in data.items():
            if k not in TAB_RETICLE or k not in vars_:
                continue
            try:
                kind, var = vars_[k]
                if kind == "map":
                    var.set(map_display(k, v))
                elif kind == "bool":
                    var.set(bool(v))
                else:
                    var.set(str(v))
            except (tk.TclError, ValueError):
                pass
        _ch_name_var.set(name)

    _ch_name_var = tk.StringVar(value="")
    _ch_name = tk.Entry(_ch_pos, textvariable=_ch_name_var, width=14,
                        font=F_ENTRY, relief="solid", borderwidth=1,
                        bg=pal["entry"], fg=pal["text"],
                        insertbackground=pal["text"],
                        highlightbackground=pal["entry_bd"],
                        highlightthickness=1)
    kit.reg(_ch_name, "entry")
    _ch_name.pack(side="left", padx=(6, 4))

    def _ch_pre_save():
        name = _ch_name_var.get().strip()
        if not name:
            status_msg(i18n.L("xh.preset_name"))
            return
        vals = {}
        for k in TAB_RETICLE:
            if k in vars_:
                try:
                    kind, var = vars_[k]
                    vals[k] = map_value(k, var.get()) \
                        if kind == "map" else var.get()
                except (tk.TclError, ValueError):
                    pass
            else:
                vals[k] = cfg.get(k)
        try:
            reticle.save_preset(name, vals)
        except (OSError, ValueError) as exc:
            status_msg("Preset not saved (%r)." % (exc,))
            return
        _ch_pre_var.set(name)
        _ch_pre_sync()

    def _ch_pre_delete():
        name = _ch_pre_var.get().strip() or _ch_name_var.get().strip()
        if not name:
            return
        if not messagebox.askyesno(
                APP_NAME, i18n.L("st.delete_confirm", name), parent=root):
            return
        try:
            if reticle.delete_preset(name):
                _ch_pre_var.set("")
                _ch_pre_sync()
        except OSError:
            pass

    _b_save = kit.button(_ch_pos, i18n.L("xh.save"), _ch_pre_save)
    _b_save.pack(side="left", padx=(0, 6))
    R(lambda: kit.set_text(_b_save, i18n.L("xh.save"))
      if _live(_b_save) else False)
    _b_del = kit.button(_ch_pos, i18n.L("xh.delete"), _ch_pre_delete)
    _b_del.pack(side="left")
    R(lambda: kit.set_text(_b_del, i18n.L("xh.delete"))
      if _live(_b_del) else False)
    _ch_pre_sync()

    chp = pages["handler"]
    chinner, chbind = _scrollable(chp, kit, pal)
    _FIT.append(chinner)
    cp = chinner

    cgrid = tk.Frame(cp, bg=pal["card"])
    kit.reg(cgrid, "card")
    cgrid.pack(fill="x", padx=6, pady=(8, 2))
    cgrid.columnconfigure(0, weight=1)
    for _ri, _ckey in enumerate(TAB_DETECT):
        make_row(cgrid, _ckey, _ri)

    rgb_vars = {}
    for _ch, _dflt in (("r", "198"), ("g", "40"), ("b", "40")):
        rgb_vars[_ch] = tk.StringVar(value=_dflt)

    rgb_cell = tk.Frame(cgrid, bg=pal["card"])
    kit.reg(rgb_cell, "card")
    rgb_cell.grid(row=len(TAB_DETECT), column=0, sticky="we", padx=10, pady=4)
    rgb_cell.columnconfigure(0, weight=1)
    rgb_lbl = tk.Label(rgb_cell, text=i18n.L("col.rgb.name"),
                       font=F_BOLD, bg=pal["card"], fg=pal["text"],
                       anchor="w")
    kit.reg(rgb_lbl, "card_bold")
    rgb_lbl.grid(row=0, column=0, sticky="w", padx=(0, 10))
    rgb_lbl.bind("<Enter>", lambda e: set_desc(
        lambda: i18n.L("col.rgb.desc")))
    rgb_lbl.bind("<Leave>", lambda e: set_desc(None))
    R(lambda l=rgb_lbl: l.configure(text=i18n.L("col.rgb.name"))
      if _live(l) else False)

    RGB_SLOT = 5 * (CHIP_SW + 2 + 2 * CHIP_PAD)
    RGB_BW, RGB_BH = RGB_SLOT - 2 * CHIP_PAD - 2, 22
    rgb_box = tk.Frame(rgb_cell, bg=pal["card"])
    kit.reg(rgb_box, "card")
    rgb_box.grid(row=0, column=1, sticky="e")
    rgb_chips = {}
    for _ch in ("r", "g", "b"):
        _bx = tk.Canvas(rgb_box, width=RGB_BW, height=RGB_BH,
                        bg="#000000", highlightthickness=1,
                        highlightbackground=pal["entry_bd"], takefocus=0)
        kit.reg(_bx, "hl")
        _bx.pack(side="left", padx=CHIP_PAD)
        rgb_chips[_ch] = _bx

    def paint_rgb_chips():
        try:
            vals = {"r": max(0, min(255, int(rgb_vars["r"].get()))),
                    "g": max(0, min(255, int(rgb_vars["g"].get()))),
                    "b": max(0, min(255, int(rgb_vars["b"].get())))}
        except (ValueError, tk.TclError):
            return
        for ch, val in vals.items():
            box = rgb_chips.get(ch)
            if box is None or not _live(box):
                continue
            try:
                if ch == "r":
                    box.configure(bg="#%02x0000" % val)
                elif ch == "g":
                    box.configure(bg="#00%02x00" % val)
                else:
                    box.configure(bg="#0000%02x" % val)
            except tk.TclError:
                pass

    host = tk.Frame(cp, bg=pal["card"])
    kit.reg(host, "card")
    host.pack(fill="x", padx=6, pady=(0, 8))

    host.columnconfigure(0, weight=1)

    left = tk.Frame(host, bg=pal["card"])
    kit.reg(left, "card")
    left.grid(row=0, column=0, sticky="nw")

    SV = 200
    sv_canvas = tk.Canvas(left, width=SV, height=SV, cursor="crosshair",
                          highlightthickness=1,
                          highlightbackground=pal["entry_bd"], takefocus=0)
    sv_canvas.pack(anchor="w", pady=(6, 0), padx=(10, 0))
    kit.reg(sv_canvas, "hl")
    hue_canvas = tk.Canvas(left, width=SV, height=22, cursor="crosshair",
                           highlightthickness=1,
                           highlightbackground=pal["entry_bd"], takefocus=0)
    hue_canvas.pack(anchor="w", pady=(6, 0), padx=(10, 0))
    kit.reg(hue_canvas, "hl")

    right = tk.Frame(host, bg=pal["card"])
    kit.reg(right, "card")
    right.grid(row=0, column=1, sticky="ne", padx=(18, 10))

    PRESET_SW, PRESET_PAD = 24, 2
    PRESET_SPAN = (len(COLOR_PRESETS) * (PRESET_SW + 2 + 2 * PRESET_PAD)
                   - 2 * PRESET_PAD)
    SHOW_W = PRESET_SPAN - 2
    BTN_GAP = 6
    BTN_W = (PRESET_SPAN - BTN_GAP) // 2
    LEFT_H = 6 + (SV + 2) + 6 + (22 + 2)
    SHOW_H = (LEFT_H - 8
              - (8 + PRESET_SW + 2 + 8)
              - 2 * (kit.BTN_H + 6)
              - (4 + PRESET_SW + 2))
    prev = tk.Canvas(right, width=SHOW_W, height=SHOW_H, bg="#c62828",
                     highlightthickness=1,
                     highlightbackground=pal["entry_bd"])
    prev.pack(anchor="e", padx=(0, PRESET_PAD), pady=(6, 0))
    kit.reg(prev, "hl")

    sw_row = tk.Frame(right, bg=pal["card"])
    kit.reg(sw_row, "card")
    sw_row.pack(anchor="e", pady=(8, 8))

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
        paint_rgb_chips()
        try:
            r = max(0, min(255, int(rgb_vars["r"].get())))
            g = max(0, min(255, int(rgb_vars["g"].get())))
            b = max(0, min(255, int(rgb_vars["b"].get())))
        except (ValueError, tk.TclError):
            return
        hx = "#%02x%02x%02x" % (r, g, b)
        try:
            prev.configure(bg=hx)
            prev.delete("all")
            lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0

            prev.create_text(int(prev.cget("width")) // 2,
                             int(prev.cget("height")) // 2,
                             text=hx, font=F_BTN,
                             fill="#ffffff" if lum < 0.55 else "#1b1b1b")
        except tk.TclError:
            pass

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
    add_row.pack(anchor="e", pady=(0, 6))
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
    auto_row.pack(anchor="e", pady=(0, 6))

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
    sug_row = tk.Frame(right, bg=pal["card"])
    kit.reg(sug_row, "card")
    sug_row.pack(anchor="e", pady=(4, 0))

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
        host = parent
        if kind in CAPTURE_KINDS:
            host = tk.Frame(parent, bg=pal["card"])
            kit.reg(host, "card")
        en = tk.Entry(host, textvariable=var, width=width,
                      font=F_ENTRY, relief="solid", borderwidth=1,
                      bg=pal["entry"], fg=pal["text"],
                      insertbackground=pal["text"],
                      highlightbackground=pal["entry_bd"],
                      highlightthickness=1)
        kit.reg(en, "entry")
        en.bind("<Enter>", lambda e, k=desc_key: set_desc(
            lambda: tr_desc(k)))
        en.bind("<Leave>", lambda e: set_desc(None))
        if kind not in CAPTURE_KINDS:
            return en
        en.pack(side="left")
        capture_button(host, kit, root, app, key, var, kind,
                       status_msg, R).pack(side="left", fill="y", padx=(4, 0))
        return host

    def macro_word(parent, key, desc_key):
        w = tk.Label(parent, text=i18n.L(key), font=F_DESC,
                     bg=pal["card"], fg=pal["muted"])
        kit.reg(w, "card_muted")
        w.pack(side="left", padx=(0, 6))
        R(lambda: w.configure(text=i18n.L(key)) if _live(w) else False)
        w.bind("<Enter>", lambda e, k=desc_key: set_desc(
            lambda: tr_desc(k)))
        w.bind("<Leave>", lambda e: set_desc(None))
        return w

    kbp = pages["keyboard"]
    kinner, kbind = _scrollable(kbp, kit, pal)
    _FIT.append(kinner)
    kgrid = tk.Frame(kinner, bg=pal["card"])
    kit.reg(kgrid, "card")
    kgrid.pack(fill="x", padx=6, pady=8)
    kgrid.columnconfigure(0, weight=1)
    for _ki, _kkey in enumerate(TAB_KEYBOARD):
        make_row(kgrid, _kkey, _ki)



    ctrlp = pages["controller"]
    cinner, cbind = _scrollable(ctrlp, kit, pal)
    _FIT.append(cinner)
    cgrid = tk.Frame(cinner, bg=pal["card"])
    kit.reg(cgrid, "card")
    cgrid.pack(fill="x", padx=6, pady=8)
    cgrid.columnconfigure(0, weight=1)
    for _pi, _pkey in enumerate(TAB_CONTROLLER):
        make_row(cgrid, _pkey, _pi)
    macp = pages["macros"]
    macinner, macbind = _scrollable(macp, kit, pal)
    _FIT.append(macinner)
    mbody = tk.Frame(macinner, bg=pal["card"])
    kit.reg(mbody, "card")
    mbody.pack(fill="both", expand=True, padx=14, pady=8)

    mtop = tk.Frame(mbody, bg=pal["card"])
    kit.reg(mtop, "card")
    mtop.pack(fill="x")
    mtop.columnconfigure(0, weight=1, uniform="macrow")
    mtop.columnconfigure(1, weight=1, uniform="macrow")
    mbot = tk.Frame(mbody, bg=pal["card"])
    kit.reg(mbot, "card")
    mbot.pack(fill="x", pady=(16, 0))
    mbot.columnconfigure(0, weight=1, uniform="macrow")
    mbot.columnconfigure(1, weight=1, uniform="macrow")

    mleft = tk.Frame(mtop, bg=pal["card"])
    kit.reg(mleft, "card")
    mleft.grid(row=0, column=0, sticky="nw")
    mac_head(mleft, "mac.keys_head")
    for i in range(1, MACRO_SLOTS + 1):
        row = tk.Frame(mleft, bg=pal["card"])
        kit.reg(row, "card")
        row.pack(fill="x", pady=6)

        lb = tk.Label(row, text=i18n.L("mac.row", i), font=F_BOLD,
                      width=8, bg=pal["card"], fg=pal["text"],
                      anchor="w")
        kit.reg(lb, "card_bold")
        lb.pack(side="left")
        R(lambda l=lb, n=i: l.configure(text=i18n.L("mac.row", n))
          if _live(l) else False)
        mkey = "macro%d_key" % i
        mact = "macro%d_action" % i
        lb.bind("<Enter>", lambda e, k=mkey: set_desc(
            lambda: tr_desc(k)))
        lb.bind("<Leave>", lambda e: set_desc(None))

        macro_entry(row, mkey, 10, mkey, "combo").pack(side="left",
                                                       padx=(0, 8))
        macro_word(row, "mac.sends", mact)
        macro_entry(row, mact, 13, mact, "combo").pack(side="left")

    cleft = tk.Frame(mbot, bg=pal["card"])
    kit.reg(cleft, "card")
    cleft.grid(row=0, column=0, sticky="nw")
    mac_head(cleft, "mac.pad_head")
    for i in range(1, MACRO_SLOTS + 1):
        prow = tk.Frame(cleft, bg=pal["card"])
        kit.reg(prow, "card")
        prow.pack(fill="x", pady=6)

        plb = tk.Label(prow, text=i18n.L("mac.pad_row", i), font=F_BOLD,
                       width=8, bg=pal["card"], fg=pal["text"], anchor="w")
        kit.reg(plb, "card_bold")
        plb.pack(side="left")
        R(lambda l=plb, n=i: l.configure(text=i18n.L("mac.pad_row", n))
          if _live(l) else False)
        pkey = "pad_macro%d_key" % i
        pact = "pad_macro%d_action" % i
        plb.bind("<Enter>", lambda e, k=pkey: set_desc(lambda: tr_desc(k)))
        plb.bind("<Leave>", lambda e: set_desc(None))

        macro_entry(prow, pkey, 10, pkey, "pad").pack(side="left",
                                                     padx=(0, 8))
        macro_word(prow, "mac.sends", pact)
        macro_entry(prow, pact, 13, pact, "padkey").pack(side="left")

    mright = tk.Frame(mtop, bg=pal["card"])
    kit.reg(mright, "card")
    mright.grid(row=0, column=1, sticky="nsew", padx=(24, 0))
    mac_head(mright, "mac.recordings")

    pright = tk.Frame(mbot, bg=pal["card"])
    kit.reg(pright, "card")
    pright.grid(row=0, column=1, sticky="nsew", padx=(24, 0))

    REC_ROW_H = 26
    rec_box = tk.Frame(mright, bg=pal["card"], highlightthickness=1,
                       highlightbackground=pal["entry_bd"],
                       highlightcolor=pal["entry_bd"])
    kit.reg(rec_box, "panel")
    rec_box.pack(fill="both", expand=True, pady=(6, 6))

    btn_row = tk.Frame(rec_box, bg=pal["card"])
    kit.reg(btn_row, "card")
    btn_row.pack(side="bottom", fill="x", padx=8, pady=(4, 8))

    loop_head = tk.Label(rec_box, text=i18n.L("mac.loop"),
                         font=F_DESC, bg=pal["card"], fg=pal["muted"],
                         anchor="e")
    kit.reg(loop_head, "card_muted")
    R(lambda: loop_head.configure(text=i18n.L("mac.loop"))
      if _live(loop_head) else False)
    loop_head.bind("<Enter>", lambda e: set_desc(
        lambda: i18n.L("st.loop_desc")))
    loop_head.bind("<Leave>", lambda e: set_desc(None))

    rec_rows = tk.Frame(rec_box, bg=pal["card"])
    kit.reg(rec_rows, "card")
    rec_rows.pack(side="top", fill="both", expand=True,
                  padx=6, pady=(0, 2))
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
            bg = pal["light_bg"] if on else pal["card"]
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
        bg = pal["light_bg"] if rid == selected_rid() else pal["card"]
        fr = tk.Frame(rec_rows, bg=bg, cursor="hand2")
        fr.pack(fill="x", pady=1)

        var = tk.BooleanVar(value=bool(rec["loop"]))
        sw = kit.bool_switch(fr, var, word=True)
        sw.pack(side="right", padx=(6, 6))

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
        rec_row_w.append((rid, fr, [fr, num, name, st_lbl] + sw_widgets))

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

    for _rk, _rf, _kd in (("mac.delete", do_delete_rec, "amber"),
                          ("mac.import", do_import_rec, "plain"),
                          ("mac.export", do_export_rec, "plain"),
                          ("mac.edit", do_edit_rec, "plain"),
                          ("mac.rename", do_rename_rec, "plain")):
        _b = kit.button(btn_row, i18n.L(_rk), _rf, kind=_kd)
        _b.pack(side="right", padx=(6, 0))
        R(lambda b=_b, k=_rk: kit.set_text(b, i18n.L(k))
          if _live(b) else False)

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

            loop_head.pack(side="top", fill="x", padx=8, pady=(6, 2),
                           before=rec_rows)
            for n, r in enumerate(app.rec_store.recordings, 1):
                make_rec_row(n, r)
        else:
            loop_head.pack_forget()
            ph = tk.Label(rec_rows, text=i18n.L("mac.empty"),
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

    mac_head(pright, "mac.pad_recordings")

    prec_box = tk.Frame(pright, bg=pal["card"], highlightthickness=1,
                        highlightbackground=pal["entry_bd"],
                        highlightcolor=pal["entry_bd"])
    kit.reg(prec_box, "panel")
    prec_box.pack(fill="both", expand=True, pady=(6, 6))

    pbtn_row = tk.Frame(prec_box, bg=pal["card"])
    kit.reg(pbtn_row, "card")
    pbtn_row.pack(side="bottom", fill="x", padx=8, pady=(4, 8))

    ploop_head = tk.Label(prec_box, text=i18n.L("mac.loop"),
                          font=F_DESC, bg=pal["card"], fg=pal["muted"],
                          anchor="e")
    kit.reg(ploop_head, "card_muted")
    R(lambda: ploop_head.configure(text=i18n.L("mac.loop"))
      if _live(ploop_head) else False)
    ploop_head.bind("<Enter>", lambda e: set_desc(
        lambda: i18n.L("st.loop_desc")))
    ploop_head.bind("<Leave>", lambda e: set_desc(None))

    prec_rows = tk.Frame(prec_box, bg=pal["card"])
    kit.reg(prec_rows, "card")
    prec_rows.pack(side="top", fill="both", expand=True,
                   padx=6, pady=(0, 2))
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
            bg = pal["light_bg"] if on else pal["card"]
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
        bg = pal["light_bg"] if rid == pselected_rid() else pal["card"]
        fr = tk.Frame(prec_rows, bg=bg, cursor="hand2")
        fr.pack(fill="x", pady=1)

        var = tk.BooleanVar(value=bool(rec["loop"]))
        sw = kit.bool_switch(fr, var, word=True)
        sw.pack(side="right", padx=(6, 6))

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
        prec_row_w.append((rid, fr, [fr, num, name, st_lbl] + sw_widgets))

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

    for _rk, _rf, _kd in (("mac.delete", pdo_delete_rec, "amber"),
                          ("mac.import", pdo_import_rec, "plain"),
                          ("mac.export", pdo_export_rec, "plain"),
                          ("mac.edit", pdo_edit_rec, "plain"),
                          ("mac.rename", pdo_rename_rec, "plain")):
        _b = kit.button(pbtn_row, i18n.L(_rk), _rf, kind=_kd)
        _b.pack(side="right", padx=(6, 0))
        R(lambda b=_b, k=_rk: kit.set_text(b, i18n.L(k))
          if _live(b) else False)

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
            ploop_head.pack(side="top", fill="x", padx=8, pady=(6, 2),
                            before=prec_rows)
            for n, r in enumerate(app.rec_store_pad.recordings, 1):
                pmake_rec_row(n, r)
        else:
            ploop_head.pack_forget()
            ph = tk.Label(prec_rows, text=i18n.L("mac.empty"),
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


    adv = pages["advanced"]
    fill_list(adv, TAB_ADV)

    fill_list(chinner, TAB_COLOR, scroll=False)

    exp = pages["experimental"]
    fill_list(exp, TAB_EXPERIMENTAL)

    lat_handle = mount(
        root, pages, kit, pal,
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
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            root.update_idletasks()
            need_w = 0
            need_h = 0
            for frame in _FIT_DOC:
                if _live(frame):
                    need_w = max(need_w, frame.winfo_reqwidth())
            scrolled = set()
            for frame in _FIT:
                if not _live(frame):
                    continue
                need_w = max(need_w, frame.winfo_reqwidth())
                need_h = max(need_h, frame.winfo_reqheight())
                parent = frame.master
                while parent is not None and parent is not root:
                    scrolled.add(parent)
                    parent = getattr(parent, "master", None)
            for pg in pages.values():
                if not _live(pg) or pg in scrolled:
                    continue
                need_w = max(need_w, pg.winfo_reqwidth())
                need_h = max(need_h, pg.winfo_reqheight())
            chrome = 0
            for child in root.winfo_children():
                if child is content_host or not _live(child):
                    continue
                chrome += child.winfo_reqheight()
            want_w = min(sw - 24, max(need_w + 48, 900))
            want_h = min(sh - 48, max(need_h + chrome + 32, 600))
            root.minsize(min(880, want_w), min(560, want_h))
            root.geometry("%dx%d+%d+%d" % (want_w, want_h,
                                           max(0, (sw - want_w) // 2),
                                           max(0, (sh - want_h) // 3)))
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
    chain_confirm_text,
    classify_scan,
    issues_message,
    manual_instructions,
    run_confirm_text,
    undo_confirm_text,
)
from input import DEFAULT_THEME, ButtonFactory, Theme

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
        self.bg       = pal["bg"]
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
            status = embed.get("status")
            if status is None:
                status = ttk.Label(self.tab_home, style="CardDim.TLabel",
                                   anchor="w")
                status.pack(side="bottom", fill="x", padx=14, pady=(0, 8))
            self.status = status
            self._build_home()
            self._build_monitor()
            self._build_tools()
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
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.results_canvas.yview)
        self.results_body = ttk.Frame(self.results_canvas)
        self.results_body.bind(
            "<Configure>",
            lambda e: self.results_canvas.configure(
                scrollregion=self.results_canvas.bbox("all")
            ),
        )
        self.results_canvas.create_window((0, 0), window=self.results_body, anchor="nw")
        self.results_canvas.configure(yscrollcommand=vsb.set)
        self.results_canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
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
        t = self.theme
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
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=vsb.set)
        txt.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
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

import asyncio
import json
import queue
import threading
import tkinter as tk
from tkinter import messagebox
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

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
    F_H1 = fonts["h1"]
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
        wrap.pack(fill="x", padx=14, pady=(0, 10))
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
        line = card(parent)
        line.pack(fill="x", pady=3)
        widget = label(line, text, F_DESC, width=24)
        widget.pack(side="left")
        if hint:
            hint_label = label(line, hint, F_DESC, muted=True)
            hint_label.pack(side="left", padx=(8, 0))
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
        widget.pack(side="left", padx=(0, 8))
        return var, widget

    def button(parent, text, command, kind="plain"):
        widget = kit.button(parent, text, command, kind=kind)
        widget.pack(side="left", padx=(0, 6))
        return widget

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
                driver_text = "Kernel driver: found but not active"
            else:
                driver_text = "Kernel driver: not loaded (registry fallback)"
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
    title = label(header, "HWID Spoofer", F_H1)
    title.pack(fill="x")
    subtitle = label(
        header,
        "Change disk, network, SMBIOS and hardware identifiers. A registry "
        "snapshot is created before the first write, and every operation is "
        "recorded in the audit log.",
        F_DESC, muted=True,
    )
    subtitle.pack(fill="x", pady=(2, 0))

    status_bar = card(inner)
    status_bar.pack(fill="x", padx=14, pady=(0, 10))
    status_widgets = {}
    for key, text in (
        ("admin", "Access: checking..."),
        ("driver", "Kernel driver: checking..."),
        ("audit", "Audit events: 0"),
    ):
        widget = tk.Label(
            status_bar, text=text, font=F_DESC, anchor="w",
            bg=pal["card"], fg=pal["muted"],
        )
        kit.reg(widget, "card_muted")
        widget.pack(side="left", padx=(0, 18))
        status_widgets[key] = widget
    refresh_button = button(status_bar, "Refresh", refresh_status)

    # ----------------------------------------------------------- quick action
    quick = section(
        "Quick actions",
        "Safe spoof runs the three identifiers that have registry fallbacks: "
        "disk serial, NIC MAC and SMBIOS type 1. Kernel-only identifiers are "
        "left for the sections below.",
    )
    quick_row = card(quick)
    quick_row.pack(fill="x")

    def run_safe_spoof():
        def work():
            output = {}
            output["disk"] = asyncio.run(get_disk_spoofer().spoof(disk_var.get() or "C"))
            output["nic"] = asyncio.run(
                get_nic_spoofer().spoof(nic_var.get().strip() or None)
            )
            try:
                smbios_type = int(smbios_var.get() or "1")
            except ValueError:
                smbios_type = 1
            output["smbios"] = asyncio.run(
                get_smbios_spoofer().spoof(smbios_type)
            )
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

    # ------------------------------------------------------------------ disk
    disk = section(
        "Disk serial",
        "The kernel driver changes the serial at sector level. Without it the "
        "registry fallback returns a new identifier and marks the result as "
        "simulated.",
    )
    disk_row = row(disk, "Volume letter")
    disk_var, _disk_entry = entry(disk_row, "C", width=6)
    button(disk_row, "Spoof disk serial", lambda: run_operation(
        "Disk serial",
        lambda: asyncio.run(get_disk_spoofer().spoof(disk_var.get() or "C")),
    ), kind="accent")

    # --------------------------------------------------------------- network
    nic = section(
        "Network adapter (MAC)",
        "Writes NetworkAddress in the adapter's class key. Leave the filter "
        "empty to use the first physical adapter; restart the adapter for the "
        "new MAC to take effect.",
    )
    nic_row = row(nic, "Adapter filter")
    nic_var, _nic_entry = entry(nic_row, "", width=22)
    button(nic_row, "Spoof MAC", lambda: run_operation(
        "NIC MAC",
        lambda: asyncio.run(
            get_nic_spoofer().spoof(nic_var.get().strip() or None)
        ),
    ), kind="accent")

    # ---------------------------------------------------------------- smbios
    smbios = section(
        "SMBIOS",
        "Table type: 0 = BIOS, 1 = System, 2 = Baseboard, 3 = Chassis, "
        "4 = Processor, 17 = Memory device. Firmware-backed values need the "
        "kernel driver.",
    )
    smbios_row = row(smbios, "Table type")
    smbios_var, _smbios_entry = entry(smbios_row, "1", width=6)
    button(smbios_row, "Spoof SMBIOS", lambda: run_operation(
        "SMBIOS",
        lambda: asyncio.run(
            get_smbios_spoofer().spoof(int(smbios_var.get() or "1"))
        ),
    ), kind="accent")

    # ---------------------------------------------------------- kernel-only
    kernel = section(
        "Kernel-level identifiers",
        "These values live in kernel memory or device firmware. The buttons "
        "report requires_driver when the signed HWID driver is not loaded.",
    )
    kernel_row = card(kernel)
    kernel_row.pack(fill="x")

    def kernel_action(title, coro_factory):
        run_operation(
            title,
            lambda: asyncio.run(coro_factory()),
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
        lambda: {"success": bool(reload_driver().driver),
                 "status": get_loading_status()},
    ), kind="plain")

    # --------------------------------------------------------------- cleanup
    clean = section(
        "Cleanup",
        "A registry snapshot is created before any cleaning. File cleaning "
        "removes caches and temporary files; review the targets before "
        "running a full clean.",
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
            engine = get_registry_engine()
            engine.create_snapshot(
                "pre_clean_%s_%s" % (
                    target, datetime.now().strftime("%Y%m%d_%H%M%S"))
            )
            cleaner = CleanerEngine()
            if target == "registry":
                return asyncio.run(cleaner._clean_registry())
            if target == "files":
                return asyncio.run(cleaner._clean_files())
            if target == "temp":
                return {"success": True,
                        "cleaned": asyncio.run(cleaner._clean_temp_files())}
            if target == "cache":
                return asyncio.run(cleaner._clean_cache())
            return asyncio.run(cleaner.full_clean())
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

    # ------------------------------------------------------------------ init
    refresh_status()
    refresh_snapshots()
    append_log(
        "Spoofer ready. %s | %s" % (
            "Administrator" if is_admin() else "Not elevated",
            "kernel driver active" if get_loading_status().get("driver_active")
            else "kernel driver not loaded (registry fallbacks available)",
        ),
        "info",
    )
    if not restore_point_supported():
        append_log("System Restore API is not available on this system.", "warn")
    bind_wheel(inner)
    page.after(150, poll_results)
    return page
