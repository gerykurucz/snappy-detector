
import colorsys
import io
import base64
import threading
import time

import numpy as np

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox, simpledialog

import system_core as i18n
import input_utils as latency_tabs
import logo
import online
import vision

import reticle
from assist import start_app
from config import DEFAULT_CONFIG, _coerce_value
from system_core import APP_NAME, IS_WINDOWS, log
try:



    from system_core import APP_VERSION
except ImportError:
    APP_VERSION = "?"
from input_utils import MACRO_SLOTS, REC_PLAY_SLOTS
if IS_WINDOWS:
    from win32rt import (PostThreadMessage, WM_HOTKEY, get_cursor_pos,
                         virtual_screen)
else:
    PostThreadMessage = WM_HOTKEY = get_cursor_pos = virtual_screen = None

try:
    from win32rt import apply_window_theme
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


def hex2rgb(value):
    value = str(value).lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))













RETEXT = []


def R(fn):
    RETEXT.append(fn)
    return fn



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
        "Read your controller through Windows' own XInput and let it do "
        "everything a keyboard and mouse do here: the same assistance, the "
        "same bindings, the same macros, the same recordings. The left stick "
        "steers the cursor, the right trigger is the press, and Start and "
        "Back launch and pause out of the box. Bind the rest on the "
        "Keybindings tab; controller macros sit on the Macros tab. "
        "Controller OUTPUT - replaying a controller recording, or a 'pad:' "
        "macro, as a genuine virtual gamepad - needs the optional ViGEmBus "
        "driver; without it that output falls back to keyboard and mouse. "
        "Takes effect at once, no restart.",
        "bool", None),
    "pad_drive": ("Controller steers and clicks",
        "With this on the left stick moves the cursor and the trigger is the "
        "press, so snapping, guide mode, follow memory and auto-click all "
        "work for somebody holding nothing but a controller. The right stick "
        "steers the view while that press is held, exactly as holding a mouse "
        "button does. Turn it off to keep the controller for bindings and "
        "macros only and leave the mouse as the one pointer.",
        "bool", None),
    "pad_output": ("Controller output method",
        "How a recorded controller step or a 'pad:' macro leaves the "
        "program. Automatic uses a ViGEmBus virtual pad when that driver is "
        "installed - the game then sees a real Xbox 360 controller - and "
        "keyboard/mouse emulation when it is not. Virtual pad insists on "
        "ViGEm and says so once when it is missing. Emulation never touches "
        "the driver: every button becomes the key or mouse button named in "
        "the pad_emulate table in config.ini. Applies on restart.",
        "map", (('Automatic', 'auto'),
                 ('Virtual pad (ViGEmBus)', 'vigem'),
                 ('Keyboard/mouse', 'emulate'))),
    "pad_launch": ("Controller: launch",
        "The controller button or combination that launches assistance - the "
        "same action the keyboard launch shortcut performs. One button "
        "('start') or several at once ('rb+start'). Empty = unbound.",
        "pad", None),
    "pad_pause": ("Controller: pause",
        "The controller button that pauses assistance and hands the "
        "controller and the mouse back to you entirely. Empty = unbound.",
        "pad", None),
    "pad_mode1": ("Controller: classic mode",
        "The controller button that switches to mode 1 (classic) - a "
        "mis-click is suppressed, the cursor snaps and the click is delivered "
        "for you. Empty = unbound.",
        "pad", None),
    "pad_mode2": ("Controller: guide mode",
        "The controller button that switches to mode 2 (guide) - your click "
        "always goes through and the cursor moves afterwards. Empty = "
        "unbound.",
        "pad", None),
    "pad_record": ("Controller: record",
        "The controller button that starts a controller recording. If "
        "'Controller: stop recording' is empty this same button also stops "
        "and saves it into the Controller Recordings list, so one "
        "button toggles. Empty = unbound.",
        "pad", None),
    "pad_record_stop": ("Controller: stop recording",
        "The controller button that stops and saves a controller recording "
        "into the Controller Recordings list. Leave it "
        "empty to let the record button toggle instead. Empty = unbound.",
        "pad", None),
    "pad_play": ("Controller: play recording",
        "The controller button that replays the selected controller "
        "recording, or the "
        "newest one when nothing is selected. Pressing it while that "
        "recording is looping stops the loop. Empty = unbound.",
        "pad", None),
    "hold_follow": ("Keep following while the button is held",
                    "While you keep the left button pressed right "
                    "after a snap - or press again on the remembered "
                    "item - the cursor stays on that item's colour, "
                    "which steadies drags like scrollbar thumbs. "
                    "Presses anywhere else are never touched.",
                    "bool", None),
    "rapid_click_snap": ("Assist every click in a fast burst",
                         "Off (default): while one click is still "
                         "being handled, extra left clicks go straight "
                         "to Windows. On: every left click is assisted, "
                         "so double-clicking keeps snapping to items "
                         "instead of leaking plain clicks. For rapid "
                         "click sequences, on is usually right.",
                         "bool", None),
    "trigger_button": ("Assist trigger button",
                       "The mouse button assistance listens to. Hold it and "
                       "the cursor is guided onto the nearest clear item; in "
                       "Classic mode the click is caught, snapped and pressed "
                       "for you. Default mouse1 (left). Move it when a game or "
                       "an application needs that button for itself - the "
                       "whole behaviour follows, including which button is "
                       "re-delivered after a snap.", "mouse", None),
    "options_button": ("Extra-options button",
                       "The button behind the two extra-action settings "
                       "(short press and hold). Default mouse2 (right): the "
                       "click itself always keeps its normal Windows "
                       "function, the action is added on top. It cannot be "
                       "the same button as the assist trigger - if the two "
                       "collide, this one returns to its default.",
                       "mouse", None),
    "shift_bypass": ("Bypass input stops assistance",
                     "On (default): holding the bypass input lets every click "
                     "pass through untouched - a precision override. Turn it "
                     "off when that input is held during regular use, or "
                     "holding it silently disables every correction. Which "
                     "input that is: 'Bypass key (held)' on the Keyboard & "
                     "Mouse tab, and its controller equivalent on the "
                     "Controller tab.",
                     "bool", None),
    "bypass_key": ("Bypass key (held)",
                   "The key or combination that, held down, stops assistance "
                   "while 'Bypass input stops assistance' on the Assistance "
                   "tab is on. Default Shift, the "
                   "long-standing desktop precision override. A modifier on "
                   "its own is accepted here - this is a hold, not a shortcut "
                   "- and so is any combination ('ctrl+alt', 'ctrl+alt+x'). "
                   "Press the button beside it, then press and release what "
                   "you want.", "keymod", None),
    "bypass_pad": ("Controller bypass button",
                   "The controller button or combination that does on a pad "
                   "what the bypass key does on a keyboard: while it is held, "
                   "assistance is off. The switch on the Assistance tab still "
                   "decides whether that counts. Empty = unbound, so a "
                   "controller-only setup keeps assisting until you choose "
                   "one.", "pad", None),
    "pad_click_button": ("Controller click button",
                         "The controller button that clicks: hold it and the "
                         "cursor is steered onto the target, exactly like "
                         "holding the assist trigger button with a mouse. "
                         "Default rt (right trigger). One button only - a "
                         "combination here could never be held and released "
                         "as a click.", "padsingle", None),
    "pad_right_button": ("Controller extra-options button",
                         "The controller button behind the same two extra "
                         "actions the mouse's extra-options button performs. "
                         "Default lt (left trigger). One button only.",
                         "padsingle", None),
    "subject_priority": ("Subject-first targets",
                     "On (default): figure figures "
                     "win close calls anywhere in range - scored by "
                     "head-shoulder structure, taper, coverage and "
                     "motion from any direction (front, back, side "
                     "profile, 45-degree), not just box ratio or "
                     "symmetry, so crouched (1:1), distant, split or "
                     "fast-moving figures hold the lock instead of "
                     "flickering. No figure present: square "
                     "shapes outrank unshaped items. Off: normal "
                     "ranking - nearest wins, shape and colour break "
                     "ties. "
                     "Combine with colour filtering for precise "
                     "selection.",
                     "bool", None),
    "subject_only": ("Only watch human shapes",
                     "Only figure figures can be assisted; clicks "
                     "anywhere else pass straight through to Windows. "
                     "A simpler, single-job watcher: no squares, no "
                     "clutter, just the figure score. Recognises the "
                     "body from any direction - front, back, side "
                     "profile, 45-degree, crouched, running or prone - "
                     "by head-shoulder structure, not mirror symmetry. "
                     "Works alone; combine with Subject-first for "
                     "ranked preference between several figures.",
                     "bool", None),
    "debug_visualize": ("Debug: show detections",
                        "Draw a box around every candidate the scanner "
                        "sees and a circle at the configured search "
                        "radius. Purely visual - does not affect clicks.",
                        "bool", None),
    "reticle_enabled": ("Show custom reticle",
                          "Draw your own reticle on top of everything, "
                          "games included. The overlay is click-through: "
                          "aim with it, click through it. Keep it small - "
                          "a big marker near the centre can be picked up "
                          "as a target itself. Feature idea inspired by "
                          "Reticle Y (MIT, YSSF8).",
                          "bool", None),
    "reticle_shape": ("Reticle shape",
                        "Which figure to draw: cross, plus, dot, circle "
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
                      "What a short press of the extra-options button ALSO "
                      "does - the click itself always keeps its normal "
                      "Windows function. Which button that is: "
                      "'Extra-options button' on the Keyboard & Mouse tab, or "
                      "its controller equivalent. The two follow choices only "
                      "steer in mode 2; in mode 1 that button never moves the "
                      "cursor. Default: nothing.",
                      "map", (('Do nothing', 4),
                               ('Clear follow task', 1),
                               ('Follow last colour', 2),
                               ('Pick colour, then follow', 3))),
    "mouse2_hold_action": ("Extra-options button: hold",
                           "The same choices, fired when you HOLD the "
                           "extra-options button for about half a second "
                           "instead of clicking it. A press is either "
                           "a click or a hold, never both. The follow "
                           "choices only steer in mode 2. Default: "
                           "nothing.",
                           "map", (('Do nothing', 4),
                                    ('Clear follow task', 1),
                                    ('Follow last colour', 2),
                                    ('Pick colour, then follow', 3))),
    "radius": ("How far from the cursor to look",
               "Search distance in pixels (40-400). Raise it when the "
               "item you want sits just out of reach; lower it when "
               "the wrong neighbour keeps winning.", "int", None),
    "center_blind_area": ("Centre self-target blindness (ink px)",
               "Assisted-centre only (hidden cursor): ignore items this small "
               "that sit exactly on the centre anchor - the overlay's own "
               "drawn marker and brief flashes, which otherwise "
               "outscore every real item so corrections land "
               "~zero. 900 ~= 30x30. 0 disables. Standard cursor "
               "clicks are never affected.", "int", None),
    "center_max_step": ("Centre steering cap (px)",
               "Assisted-centre steering steps are clamped to this size with "
               "direction exact: a far pick corrects toward the item "
               "without jumping the view in one step. 160 default. "
               "0 disables (uncapped).", "int", None),
    "subject_focus_bias": ("Subject focus bias (0-100)",
               "For tall 1:2 subject-shape winners only: pull the snap point "
               "from the mass centroid toward the box top by this "
               "share - 30 lands near the upper part. 0 (default) off.",
               "int", None),
    "motion_sense": ("Motion sense (0-100)",
               "Prefer items that moved since the previous scan - a "
               "moving item outranks static ones in close "
               "calls. Assisted-centre only. 0 (default) off; 35 is a sane "
               "first try. Brief flashes are ignored.",
               "int", None),
    "on_target_dist": ("Already-on-item distance",
                       "If your click lands within this many pixels of "
                       "an item's centre, nothing moves - you hit it.",
                       "int", None),
    "glide_ms": ("Animate cursor moves",
                 "0 (default) jumps instantly - fastest. 40-120 glides "
                 "so you can watch the cursor travel; moving the mouse "
                 "yourself cancels the glide and keeps control.",
                 "int", None),
    "watcher_seconds": ("Double-click window",
                        "In classic mode, a second snap near the same "
                        "spot within this time becomes a double-click "
                        "on the item.", "float", None),


    "accent_color": ("My colour(s)",
                     "The colour Snappy AI should prefer, shown as "
                     "colour boxes on the right. Pick a colour below "
                     "and press 'Add to preferred colours' to add it "
                     "- fifteen boxes at most; click a box to remove "
                     "that colour again. Items containing one of them "
                     "win ties. An empty list shows a crossed-out box "
                     "and disables the setting.",
                     "colors", None),
    "accent_only": ("Only assist my colour",
                    "Only items containing your colour can be "
                    "assisted; clicks anywhere else pass straight "
                    "through to Windows. Needs a colour in 'My "
                    "colour(s)' first - with none set this switch stays "
                    "on and simply does nothing, and the log says so "
                    "(your switch is never rewritten behind your "
                    "back).", "bool", None),
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
                       "How colours are compared. Perceptual ranks by "
                       "how colours actually look and is recommended; "
                       "Classic is the older engine, kept for "
                       "comparison. Only applies while 'Accept "
                       "near-miss shades' is above 0 - at 0 both use the "
                       "same exact colour box.",
                       "map", (('Perceptual (recommended)', 'oklab'),
                                ('Classic (older engine)', 'hsv'))),
    "accent_boost": ("Colour tie-break strength",
                     "How much your colour lifts an item's score "
                     "(0-3). Raise toward 1.5-2 when the wrong item "
                     "still wins.", "float", None),

    "exclude_color": ("Never-click colour(s)",
                      "Items containing these colours are never "
                      "assisted: clicks on them go straight to "
                      "Windows, in both modes. Shown as colour boxes "
                      "on the right - pick a colour below and press "
                      "'Add to refused colours' to add one (fifteen "
                      "at most), click a box to remove it. An empty "
                      "list shows a crossed-out box and disables the "
                      "setting.",
                      "colors", None),
    "motion_3d": ("Smarter following for moving items",
                   "Track moving items with position AND size instead of "
                   "a straight-line fit: single bad scans are ignored "
                   "instead of blinding the follower, turns are followed "
                   "through, and a sudden size jump restarts the track "
                   "(that means the scanner picked a different item). "
                   "Same speed gates and the same 80 px cap - only the "
                   "brains change. Runs on its own.",
                   "bool", None),
    "motion_predict": ("Lead moving items",
                       "For items that move while you click (scrolling "
                       "lists, video seek bars), move slightly ahead so "
                       "the click lands on the item, not where it was. "
                       "Still items are unaffected.", "bool", None),
    "motion_lead_ms": ("How far ahead to lead",
                       "Milliseconds of travel to lead a moving item "
                       "(0-300): 40-80 suits slow scrolling, 100-150 a "
                       "fast seek bar. The lead is capped at 80 px and "
                       "switches off for slow or turning movement.",
                       "int", None),
    "hpose_mode": ("Human detection (hpose)",
                   "Off: the classical scanner decides, as before. On: a "
                   "neural person detector answers first - it tells a "
                   "person from a picture of a person, and it knows which "
                   "way they are facing and what they are doing. When it "
                   "finds nobody the classical scanner takes over. Needs "
                   "onnxruntime; the models download themselves.",
                   "map", (('Off', 'off'), ('On', 'on'))),
    "hpose_view": ("Which way the person must face",
                   "Only people whose body faces this way are aimed at. "
                   "Any takes everyone. Facing means the person is "
                   "looking towards the screen - a face is visible. Away "
                   "means their back is turned. Side means a profile.",
                   "map", (('Any', 'any'), ('Facing the screen', 'facing'),
                              ('Turned away', 'away'),
                              ('Side profile', 'side'))),
    "hpose_stance": ("Which posture the person must have",
                     "Only people in this posture are aimed at. Any takes "
                     "everyone. Low covers crouching and prone bodies. "
                     "Walking and standing are told apart by the track, "
                     "not by one picture.",
                     "map", (('Any', 'any'), ('Standing', 'standing'),
                                ('Crouching', 'crouch'), ('Prone', 'prone'),
                                ('Walking', 'walk'), ('Low (crouch or prone)',
                                                     'low'))),
    "hpose_aim": ("Where to aim on the person",
                  "The middle of the body, or the head. The head is "
                  "where a person is usually meant to be hit, but it "
                  "moves more than the body does, so it is steadier to "
                  "take the middle.",
                  "map", (('Middle of the body', 'center'),
                             ('Head', 'head'))),
    "hpose_lead_ms": ("How far ahead to lead a person",
                      "Milliseconds of travel to lead a person who is "
                      "moving (0-300): 40-80 suits ordinary walking. The "
                      "lead is only applied to a target that is "
                      "actually moving, and is capped at 120 px.",
                      "int", None),
    "hpose_min_conf": ("How sure a person must be",
                       "The confidence the detector must have before a "
                       "person is aimed at (10-90): higher is pickier "
                       "and slower to find people, lower finds them "
                       "sooner but makes more mistakes.",
                       "int", None),
    "hpose_model": ("Person detector",
                    "Which neural detector to use. The small one is fast "
                    "and finds big people; the medium one is better at "
                    "small and distant people and costs more time.",
                    "map", (('Small and fast', 'yolo11n-pose.onnx'),
                               ('Medium and sharper', 'yolo11s-pose.onnx'))),
    "blob_watch": ("Blob watcher fallback",
                   "When human detection and the classical scanner both "
                   "find nothing, aim at a high-contrast blob shaped "
                   "like a person (about twice as tall as wide, 14-64 "
                   "px) - the last resort for 20-30 px people the "
                   "neural detector cannot see. Only active while "
                   "human detection is on.",
                   "bool", None),
    "follow_enabled": ("Remember the last clicked item",
                       "After a successful snap, similar-looking items "
                       "or items in the same place win ties for a "
                       "while - handy for scrollbars and repeated "
                       "clicks on one control.", "bool", None),
    "watch_center_only": ("Watch the screen centre only",
                          "For fixed layouts where the items never "
                          "move: assistance ignores where your cursor "
                          "is and always looks inside a square at the "
                          "middle of the screen - as wide as your "
                          "\"search distance\" setting (150 px by "
                          "default) - treating the centre itself as "
                          "the anchor. Highest-contrast items win, "
                          "your colour only breaks ties. A mouse1 "
                          "click inside the square snaps onto the "
                          "item once; KEEP holding and it keeps "
                          "re-snapping onto each fresh sighting about "
                          "every 2-3 frames until you let go (or the "
                          "item leaves the square). Clicks outside "
                          "that square pass through completely "
                          "untouched, like empty space. Off "
                          "(default): the usual cursor-anchored "
                          "search.",
                          "bool", None),
    "center_follow": ("Keep the cursor on it",
                      "Needs \"Watch the screen centre only\" on. Once "
                      "an item inside the watched square is picked - "
                      "by your click, or by resting the cursor on it - "
                      "the cursor stays parked on it and keeps following "
                      "it even while the item moves (a scrolling plane, "
                      "an animated panel). No colour needed: without "
                      "\"My colour(s)\" set it locks straight onto the "
                      "best high-contrast item; with a colour set it "
                      "waits for the brief dwell first. It never fights "
                      "you: holding any mouse button pauses it, and it "
                      "lets go about a second after the item "
                      "disappears. Turn on \"Lead moving items\" as "
                      "well for smooth fast chasing.",
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
    "follow_seconds": ("How long to remember",
                       "Seconds the remembered item stays preferred "
                       "after your last successful snap (1-120).",
                       "int", None),
    "merge_distance": ("Glue nearby pieces together",
                       "Pieces closer than this many pixels (a broken "
                       "outline, an icon beside its label) become one "
                       "item. Two complete items are never glued, so "
                       "the cursor cannot land between buttons.",
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
    "capture_backend": ("Screen capture engine",
                        "How the screen is photographed. Automatic "
                        "uses the compatible capture that always "
                        "works, including over Remote Desktop and in "
                        "virtual machines. Desktop Duplication is "
                        "faster and stays opt-in: a minority of GPU "
                        "drivers and protected-content apps answer it "
                        "with black frames, so it is never chosen for "
                        "you. Applies on restart.",
                        "map", (('Automatic (compatible)', 'auto'),
                                 ('Desktop Duplication', 'dxcam'),
                                 ('Compatible (RDP, VMs)', 'mss'),
                                 ('Windows Graphics Capture', 'wgc'))),




    "input_backend": ("Click delivery method",
                      "How your assisted clicks reach the screen. "
                      "Standard works everywhere with nothing to "
                      "install and is what almost everyone should "
                      "keep. Driver level sends clicks through a "
                      "kernel driver instead, for the "
                      "few apps that ignore normal synthetic clicks "
                      "- it needs only the driver installed (run "
                      "interception.exe install as administrator, "
                      "then reboot; no DLL needed, strokes go "
                      "straight to keyboard.sys/mouse.sys). "
                      "Server sends every move and click to a "
                      "companion server over websocket "
                      "- hardware-level input "
                      "for apps that ignore SendInput and lack the "
                      "driver; it needs that server running plus the "
                      "websocket-client package, and holds arrive as "
                      "repeated taps. If anything is missing, Snappy "
                      "AI says so once and keeps working the "
                      "standard way. Applies on restart.",
                      "map", (('Standard Windows', 'win32'),
                               ('Driver level (kernel)', 'driver'),
                               ('Server (websocket)', 'server'))),
    "server_url": ("Server address",
                   "WebSocket URL of the companion server "
                   "(ws://127.0.0.1:19561). Only used with the "
                   "Server delivery method. Applies on "
                   "restart.",
                   "str", None),





    "contrast_threshold": ("How faint an edge may be",
                           "Lower finds fainter items (and more "
                           "noise); higher needs crisper edges. Raising "
                           "this above 38 turns off the auto-adjust "
                           "below for this control - a value you set "
                           "deliberately is used exactly.",
                           "int", None),
    "min_contrast": ("Minimum edge strength",
                     "An item must be at least this crisp to be "
                     "clickable at all. Values below 28 count as 28: "
                     "a built-in quality floor keeps noise from "
                     "becoming clickable. Raising this above 48 turns "
                     "off the auto-adjust below for this control - your "
                     "number is then used exactly. 'Click items by "
                     "itself' holds its own stricter floor (60) so it "
                     "only ever fires on a crisp item, whatever this is "
                     "set to.", "int", None),
    "adaptive_threshold": ("Auto-adjust for dim screens",
                           "Dark themes and washed-out documents lower "
                           "their own detection bar automatically, so "
                           "faint items are still found. Recommended "
                           "on. It only ever LOWERS the bar, and it "
                           "stands aside when you have deliberately "
                           "raised either contrast control above its "
                           "stock value (38 / 48).", "bool", None),
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
    "stall_pause_seconds": ("Pause after a hiccup",
                            "If a scan ever stalls, assistance pauses "
                            "this long and any held click is given "
                            "back to you.", "float", None),




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



    "snap_offset_x": ("Click point: shift left / right",
                      "Moves the click point sideways from the middle of "
                      "the item (-200 to 200, negative goes left). For a "
                      "control whose middle is dead - a split button, a "
                      "slider track - dial the click onto the live spot "
                      "once instead of fighting every snap.",
                      "int", None),
    "snap_offset_y": ("Click point: shift up / down",
                      "Moves the click point vertically from the middle "
                      "of the item (-200 to 200, negative goes up). Same "
                      "idea as the sideways shift: land on the live part "
                      "of the control.",
                      "int", None),
    "scan_budget_ms": ("Scan time budget",
                       "Milliseconds one scan of the whole search area "
                       "may take before it gives up and lets your click "
                       "through untouched (60-2000). Raise it on busy "
                       "screens if detection seems to miss; lower it if "
                       "clicks feel late on a slow machine. The budget is "
                       "checked between stages, so a very busy frame can "
                       "overrun it slightly.",
                       "int", None),




    "auto_click": ("Click items by itself",
                   "Scans the screen centre on its own and clicks the "
                   "best item - no click from you needed; turning this "
                   "on IS the request. With 'My colour(s)' set it "
                   "clicks only items in your colour; without a colour "
                   "it clicks the highest-contrast item instead. "
                   "Never while paused, never on Snappy AI's own "
                   "windows, never on a Never-click colour, and it "
                   "keeps clicking while the item stays visible. "
                   "Press Pause any time to take the mouse back.",
                   "bool", None),
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
              "center_follow",
              "accent_only",
              "adaptive_threshold", "luminance_norm",
              "color_adapt", "accent_variants", "accent_coherent",
              "auto_click"]
TAB_DETECT = ["accent_color", "exclude_color"]
TAB_RETICLE = ["reticle_enabled", "reticle_shape", "reticle_mode",
                 "reticle_size", "reticle_gap", "reticle_thickness",
                 "reticle_rotation", "reticle_opacity", "reticle_color",
                 "reticle_x", "reticle_y"]
TAB_KEYS = ["key_launch", "key_pause", "key_mode1", "key_mode2",
            "key_record", "key_record_stop", "key_play", "key_play1",
            "key_play2", "key_play3", "key_play4", "key_play5"]
# The tab order. "help" is packed on the right of the bar; everything else on
# the left. Module-level so the tests read the same list the UI is built from.
TAB_NAMES = ["assist", "handler", "keyboard", "controller", "macros",
             "reticle",
             "advanced", "experimental", "latency_fix", "latency_live",
             "latency_tools", "help"]

TAB_PAD = ["pad_launch", "pad_pause", "pad_mode1", "pad_mode2",
           "pad_record", "pad_record_stop", "pad_play",
           "pad_play1", "pad_play2", "pad_play3", "pad_play4", "pad_play5"]
# The controller's two role buttons and its bypass. They were config.ini-only
# until now, which left a controller user unable to say what clicks.
TAB_PAD_ROLE = ["pad_click_button", "pad_right_button", "bypass_pad"]
# The mouse's two role buttons: what assistance listens to, and what the
# extra-action settings hang off. Both were hardcoded to left and right.
TAB_MOUSE = ["trigger_button", "options_button"]
# Flat rows for the two input tabs, with no section titles. Each position
# mirrors the other tab: role, role, bypass, then the twelve actions in the
# same order - so row N on one tab is the same kind of setting as row N on
# the other. The bypass on each tab sets that input type only; the on/off
# switch stays on the Assistance tab.
TAB_KEYBOARD = ["trigger_button", "options_button", "bypass_key",
                "key_launch", "key_pause", "key_mode1", "key_mode2",
                "key_record", "key_record_stop", "key_play", "key_play1",
                "key_play2", "key_play3", "key_play4", "key_play5"]
TAB_CONTROLLER = ["pad_click_button", "pad_right_button", "bypass_pad",
                  "pad_launch", "pad_pause", "pad_mode1", "pad_mode2",
                  "pad_record", "pad_record_stop", "pad_play",
                  "pad_play1", "pad_play2", "pad_play3", "pad_play4",
                  "pad_play5"]
TAB_COLOR = ["contrast_threshold", "min_contrast",
             "min_area", "max_area", "max_dim",
             "accent_tolerance", "accent_soft", "accent_boost"]
TAB_ADV = ["radius", "center_blind_area", "center_max_step", "subject_focus_bias", "motion_sense", "on_target_dist", "glide_ms", "watcher_seconds",
           "stall_pause_seconds", "merge_distance",
           "motion_lead_ms", "follow_seconds", "scan_budget_ms",
           "hpose_lead_ms", "hpose_min_conf",
           "snap_offset_x", "snap_offset_y",
           "server_url"]




# Derived: every choice/map setting gets a row here. A setting that is
# switched live by a hotkey instead of stored ("mode", which the 1/2 keys
# flip) deliberately has no META entry at all, so it cannot creep back in.
TAB_EXPERIMENTAL = [k for k in META if META[k][2] in ("choice", "map")]

# Bindings and macro fields are CAPTURED, not typed: rewrite their kind so
# make_row/macro_entry give them a "Press" button. Done here rather than in 24
# META entries so the two cannot drift apart when a slot is added.
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
    _CAPTURE_KIND["macro%d_key" % _i] = "key"
    _CAPTURE_KIND["macro%d_action" % _i] = "key"
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
        "Snappy AI looks at the screen around your cursor, finds the "
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
        "item and Snappy AI clicks it for you. 'How far from the "
        "cursor to look' decides the search circle - raise it when the "
        "item you want is just out of reach, lower it when the wrong "
        "neighbour wins. Holding the bypass key (Keyboard & Mouse tab, Shift by "
        "default) lets one click through untouched in both modes, and which "
        "button assistance listens to at all is yours to move on the "
        "Keyboard & Mouse tab.",
    "help.3.t": "Nothing is found - or too much is",
    "help.3.b":
        "On the Handler tab, 'How faint an edge may be' and 'Minimum "
        "edge strength' decide which edges count at all: lower them to "
        "find fainter items, raise them to ignore noise. 'Smallest "
        "item size', 'Largest item size' and 'Largest item width or "
        "height' drop specks and whole windows. 'Glue nearby pieces "
        "together' joins a broken outline, or an icon beside its "
        "label, into one item. On the Assistance tab, 'Auto-adjust for "
        "dim screens' and 'Judge edges by relative brightness' make "
        "dark themes behave like light ones - keep both on.",
    "help.4.t": "Working with colours",
    "help.4.b":
        "On the Colors tab, 'My colour(s)' is the colour Snappy AI "
        "prefers and 'Never-click colour(s)' is absolute: items with "
        "that colour are never assisted. 'Only assist my colour' "
        "restricts assistance to matching items. On the Handler tab, "
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
        "and moves ahead by 'How far ahead to lead' milliseconds, so the "
        "click lands on a scrolling list or a video seek bar instead "
        "of behind it. The lead caps at 80 pixels and switches itself "
        "off for still or turning items.",
    "help.7.t": "How clicks feel",
    "help.7.b":
        "'Animate cursor moves' = 0 jumps instantly; 40-120 glides so "
        "you can watch, and moving the mouse yourself cancels the "
        "glide. 'Double-click window' turns a second snap near the "
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
        "assistance was the problem.  3. Wrong item wins: lower 'How "
        "far from the cursor to look', then set 'My colour(s)' or "
        "raise 'Colour tie-break strength'.  4. Nothing is found: "
        "lower 'How faint an edge may be' and 'Minimum edge strength', "
        "raise 'Largest item width or height', or raise 'Glue nearby "
        "pieces together'.  5. One thing must never be assisted: put "
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
    "mac.export": "Export",
    "mac.import": "Import",
    "mac.delete": "Delete",
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
        "so the snap, guide, follow and auto-click behaviour is the same code "
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
        "'Human detection' and a neural detector takes over for people. "
        "It draws a real body, fits it to the picture and works out "
        "which way the person is facing - towards the screen, turned "
        "away, or in profile - and what they are doing: standing, "
        "walking, crouching or lying down. Those answers can be used as "
        "conditions: 'only aim at people facing the screen', 'only aim "
        "at crouching people'. A moving person is measured and led, so "
        "the aim lands where they are going rather than where they "
        "were. When the detector finds nobody, the ordinary scanner "
        "takes over, so nothing is lost by leaving it on. The detector "
        "and the models download themselves on the first use and need "
        "onnxruntime; without it the ordinary scanner runs as before.",
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

# One width for every dropdown, so the Experimental tab lines up. The option
# names are deliberately short: each setting's hover text and the Help tab
# carry the explanation, and a per-setting width made the column ragged.
CHOICE_WIDTH = 26

PAD_CAPTURE_SECONDS = 8.0
PAD_CAPTURE_W = 84


def _scrollable(page, kit, pal, bar=False):
    """A page whose content can be taller than the window.

    The visible scrollbar is Help's alone (`bar=True`): on a settings tab a
    permanent grey rail down the right edge is noise, and _fit_window sizes the
    window so settings pages do not need to scroll at all. The canvas and the
    wheel bindings stay either way, because on a short screen the window is
    clamped and the content must still be reachable - hiding the rail must not
    make settings unreachable.
    """
    host = tk.Frame(page, bg=pal["card"])
    kit.reg(host, "card")
    host.pack(fill="both", expand=True)

    cvs = tk.Canvas(host, highlightthickness=0, takefocus=0, bd=0,
                    bg=pal["card"])
    kit.reg(cvs, "canvas_card")
    if bar:
        scroll = ttk.Scrollbar(host, orient="vertical", command=cvs.yview)
        cvs.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
    cvs.pack(side="left", fill="both", expand=True)

    inner = tk.Frame(cvs, bg=pal["card"])
    kit.reg(inner, "card")
    win = cvs.create_window(0, 0, anchor="nw", window=inner)

    def _fit(_event=None):
        try:
            cvs.itemconfigure(win, width=max(120, cvs.winfo_width()))
            cvs.configure(scrollregion=cvs.bbox("all"))
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

    def bind_wheel(widget):
        widget.bind("<MouseWheel>", lambda e: _wheel(-int(e.delta) // 120))
        widget.bind("<Button-4>", lambda e: _wheel(-1))
        widget.bind("<Button-5>", lambda e: _wheel(1))

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
    """A Tk keysym -> this project's hotkey spelling, or None if unbindable.

    Kept beside the dialog rather than in hotkeys.py because it is a Tk
    concern: hotkeys.py owns the grammar ("ctrl+alt+r"), this owns the
    translation from whatever the windowing system called the key.
    """
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
                        allow_modifier=False):
    """Ask for a key or combination and return its spec, or None on cancel.

    Modifiers are tracked from the dialog's own KeyPress/KeyRelease events
    rather than read out of event.state, whose modifier bits mean different
    things on X11 and on Windows and would silently produce the wrong spec on
    one of them.

    `allow_pad` also listens for a controller combination and answers
    "pad:<spec>", which is how a controller macro's OUTPUT can be either a key
    combo or a controller combo from the same button.

    `allow_modifier` accepts a modifier on its own, committed when it is
    released. A shortcut needs a non-modifier key ("shift" alone is nothing to
    register), but the assistance bypass is a HOLD, and "while Shift is down"
    is the one spec people expect to be able to keep. Pressing a second key
    still builds the combination, so the same dialog serves both.

    Escape cancels rather than binding: a global Escape hotkey would break
    every dialog in the program, including this one.
    """
    import input_utils as gamepad
    import input_utils as hotkeys
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
    head = tk.Label(box, text=i18n.L("key.cap_head"), font=F_BOLD,
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
             "sawkey": False}
    aid = []

    def finish(spec):
        # Once, or not at all. Every sequence here is bound twice - on the
        # window and app-wide, so that a dialog without the keyboard focus can
        # still be driven - so one press reaches finish twice. Without this
        # guard the second run recomputes from an already-emptied `held` and
        # overwrites the answer: releasing ctrl+alt answered "ctrl".
        if done["v"] or not _live(win):
            return
        done["v"] = True
        result["spec"] = spec
        for handle in aid:
            try:
                win.after_cancel(handle)
            except (tk.TclError, ValueError):
                pass
        for seq in ("<KeyPress>", "<KeyRelease>", "<Escape>"):
            try:
                win.unbind_all(seq)
            except tk.TclError:
                pass
        try:
            win.destroy()
        except tk.TclError:
            pass

    def on_press(event):
        if not _live(win):
            return
        low = str(getattr(event, "keysym", "") or "").lower()
        mod = _TK_MODS.get(low)
        if mod is not None:
            held.add(mod)
            hint.configure(text=i18n.L(
                "key.cap_holding",
                "+".join(m for m in _MOD_ORDER if m in held)))
            return
        token = keysym_to_spec(getattr(event, "keysym", ""))
        if token is not None:
            track["sawkey"] = True
        if token is None:
            hint.configure(text=i18n.L("key.cap_unbound",
                                       str(getattr(event, "keysym", "?"))))
            return
        spec = "+".join([m for m in _MOD_ORDER if m in held] + [token])
        if hotkeys._parse_hotkey(spec) is None:
            hint.configure(text=i18n.L("key.cap_invalid", spec))
            return
        finish(spec)

    def on_release(event):
        mod = _TK_MODS.get(str(getattr(event, "keysym", "") or "").lower())
        if mod is None:
            return
        # Releasing a modifier with no key ever pressed commits the modifiers
        # that were down - that is how "shift" alone becomes a bypass spec.
        # Anything that reached on_press with a real key already finished.
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
        aid.append(win.after(1100, lambda: finish(None)))

    cancel = kit.button(box, i18n.L("cap.cancel"), lambda: finish(None))
    cancel.pack(anchor="e")
    # bind_all, not just win.bind: a capture dialog that depends on really
    # holding the keyboard focus fails silently on window managers that hand
    # focus elsewhere, and the user sees a countdown that ignores their keys.
    # The dialog is modal (grab_set) and short-lived, and finish() unbinds.
    win.bind("<KeyPress>", on_press)
    win.bind("<KeyRelease>", on_release)
    win.bind("<Escape>", lambda _e: finish(None))
    win.bind_all("<KeyPress>", on_press, add="+")
    win.bind_all("<KeyRelease>", on_release, add="+")
    win.bind_all("<Escape>", lambda _e: finish(None), add="+")
    win.protocol("WM_DELETE_WINDOW", lambda: finish(None))
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
    """Ask for a controller button and return its spec, or None on cancel.

    Polls XInput directly instead of going through the engine, so a binding
    can be captured before controller input is switched on, and while the
    engine is paused. A combination is captured at its WIDEST - every button
    that was down at any point in the press - and returned on release, so
    "rb+a" needs no timing and cannot come back as just "a".

    `single` captures exactly one button and answers on the press instead of
    the release, for the two roles that must not be a combination: the
    controller's click button and its extra-options button. Both are held and
    released by the drive path, so a two-button spec there could never be
    honoured and would leave the user steering with no way to click.
    """
    import input_utils as gamepad
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
        # See _mouse_capture_dialog: Escape has to survive a focus-less event.
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
            # The lowest button that is down, answered on the press: a single
            # role has no combination to wait for.
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


# Tk numbers mouse buttons 1=left, 2=middle, 3=right on every platform, while
# Windows (and this program's config) numbers them mouse1=left, mouse2=right,
# mouse3=middle. 4 and 5 are the two side buttons. One table, one translation.
_TK_MOUSE_BUTTONS = {1: "mouse1", 2: "mouse3", 3: "mouse2",
                     4: "mouse4", 5: "mouse5"}

MOUSE_BUTTON_NAMES = tuple(_TK_MOUSE_BUTTONS[n]
                           for n in sorted(_TK_MOUSE_BUTTONS))


def _mouse_capture_dialog(parent, kit, initial=""):
    """Ask which mouse button a role uses; return its name or None on cancel.

    A real press rather than a dropdown, so the two mouse roles read exactly
    like every other binding on the tab and cannot be set to a name the hook
    does not understand. Escape cancels; the dialog cannot unbind, because a
    role with no button would leave the program with no way to click.
    """
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
        # Escape cancels, from a KeyPress handler as well as the keysym-specific
        # <Escape> pattern: an event that arrives without the window really
        # holding the keyboard focus does not always reach a keysym pattern, and
        # a dialog the user cannot cancel is a dialog they have to kill.
        if str(getattr(event, "keysym", "") or "").lower() in ("escape", "esc"):
            finish(None)

    for seq in ("<Escape>", "<KeyPress>"):
        seqs.append(seq)
        win.bind(seq, on_key if seq == "<KeyPress>"
                 else (lambda _e: finish(None)))
        # bind_all too, for the reason the key dialog gives: a dialog that
        # depends on really holding the keyboard focus cannot be cancelled at
        # all on window managers that hand focus elsewhere.
        win.bind_all(seq, on_key if seq == "<KeyPress>"
                     else (lambda _e: finish(None)), add="+")

    def expire():
        # Same escape hatch the other two dialogs have: a dialog that only a
        # mouse press can close is a trap for anyone who opened it by accident
        # or whose button is stuck.
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


CAPTURE_KINDS = ("key", "keymod", "pad", "padsingle", "padkey", "mouse")


def capture_button(parent, kit, root, app, key, var, kind, status_msg, R):
    """The "Press" button beside a binding field.

    The kind decides what is listened to: "key" the keyboard, "keymod" the
    keyboard including a lone modifier, "pad" a controller combination,
    "padsingle" exactly one controller button, "padkey" both devices - the
    last is how a controller macro's OUTPUT can be either a key combo or a
    controller combo - and "mouse" one of the five mouse buttons. One
    implementation, so keyboard shortcuts, macros, controller bindings, the
    mouse roles and the bypass all capture the same way and report the same
    status lines.

    Returns the button; the caller places it (gridded in a settings row,
    packed in a macro row).
    """
    def _capture(k=key, v=var, kd=kind):
        if kd == "pad":
            spec = _pad_capture_dialog(root, kit, app, v.get())
        elif kd == "padsingle":
            spec = _pad_capture_dialog(root, kit, app, v.get(), single=True)
        elif kd == "mouse":
            spec = _mouse_capture_dialog(root, kit, v.get())
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
    """Tk class of an event's widget, whether Tkinter knows it or not.

    `event.widget` is a WIDGET for anything Tkinter created, but a bare PATH
    STRING for the internals Tk builds itself - a ttk.Combobox popdown, its
    listbox, scrollbar arrows. Those have no Python proxy, so calling
    `.winfo_class()` on them raised AttributeError on every focus change
    inside a dropdown, once per event, straight into the console.
    """
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
    root.withdraw()  # hide until fully built
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
        root.destroy()

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
            side = "right" if tn == "help" else "left"
            gap = (0, 3) if side == "right" else (3, 0)
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
            # switch_to() -> apply() synchronously repaints every
            # registered surface, and refresh_fields is registered at
            # build time - fields are reloaded there. A second explicit
            # pass would be redundant and re-toggles suppress mid-frame.
            kit.switch_to(theme)
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




            w = ttk.Combobox(cell, textvariable=var,
                             values=[d for d, _v in map_pairs_live(key)],
                             state="readonly", width=CHOICE_WIDTH,
                             font=F_BASE)

            def _revals(w=w, k=key):
                if not _live(w):
                    return False
                w.configure(values=[d for d, _v in map_pairs_live(k)],
                            width=CHOICE_WIDTH)
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
                        from input_backend import (InterceptionBackend,
                                                   RebindBackend,
                                                   DEFAULT_SERVER_URL)
                        try:
                            _url = str(cfg.get("server_url")
                                       or DEFAULT_SERVER_URL)
                        except Exception:
                            _url = DEFAULT_SERVER_URL
                        driver_ok = InterceptionBackend.available()
                        server_ok = RebindBackend.available(_url)
                    except Exception:
                        driver_ok, server_ok = (False, False)
                    def _paint(driver_ok=driver_ok, server_ok=server_ok, txt=txt):
                        if not _live(txt):
                            return False
                        bits = []
                        if driver_ok:
                            bits.append("Driver ready")
                        if server_ok:
                            bits.append("Server answering")
                        txt.configure(text=(
                            "; ".join(bits) + "."
                            if bits else
                            "Neither driver nor server found "
                            "here: those choices keep the standard "
                            "Windows method."))
                        return True
                    # (the old outer try/except R(_paint) fallback was
                    #  unreachable: the inner try already swallows every
                    #  failure the outer one claimed to catch)
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
            w = tk.Entry(cell, textvariable=var, width=16, font=F_ENTRY,
                         relief="solid", borderwidth=1, bg=pal["entry"],
                         fg=pal["text"], insertbackground=pal["text"],
                         highlightbackground=pal["entry_bd"],
                         highlightthickness=1)
            kit.reg(w, "entry")
        w.grid(row=0, column=1, sticky="e")









    _chip_strips = {}
    _OFF_WORDS = ("off", "none", "disabled", "false")
    _MAX_CHIPS = 15
    CHIP_SW, CHIP_PAD = 24, 2
    CHIP_OFF_X = "#cc2f38"

    def _token_rgb(tok):
        preset = vision.ACCENT_PRESETS.get(tok.strip().lower())
        if preset is not None:
            return tuple(int(v) for v in vision.hsv_to_rgb(*preset))
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

    # The scrollable inner frame of every SETTINGS page, so _fit_window can
    # measure real content height - a canvas only ever reports its viewport.
    _FIT = []
    # Long-form pages (Help) contribute their WIDTH only: a document is meant
    # to scroll, and sizing the window to it left ~200 px of dead space beside
    # every settings tab.
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
            w = _ch_pv.winfo_width() or 240
            h = _ch_pv.winfo_height() or 140
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
                size * sc, gap * sc, _ch_val("reticle_thickness", 2),
                _ch_val("reticle_rotation", 0),
                reticle.to_hex(reticle.parse_color(
                    _ch_val("reticle_color", "0,255,0"))))
        except Exception:
            pass
        return True
    R(ch_preview)

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

    _b_cur = kit.button(_ch_pos, i18n.L("xh.set_cursor"), _ch_to_cursor,
                        fixed_w=16)
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
    _ch_pre_box = ttk.Combobox(_ch_pos, textvariable=_ch_pre_var,
                               state="readonly", width=18, font=F_BASE)
    _ch_pre_box.pack(side="left")

    def _ch_pre_names():
        try:
            return sorted(reticle.list_presets())
        except Exception:
            return []

    def _ch_pre_sync():
        if not _live(_ch_pre_box):
            return False
        _ch_pre_box.configure(values=_ch_pre_names())
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

    _ch_pre_box.bind("<<ComboboxSelected>>", _ch_pre_load)

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
        try:
            if reticle.delete_preset(name):
                _ch_pre_var.set("")
                _ch_pre_sync()
        except OSError:
            pass

    _b_save = kit.button(_ch_pos, i18n.L("xh.save"), _ch_pre_save,
                         fixed_w=12)
    _b_save.pack(side="left", padx=(0, 6))
    R(lambda: kit.set_text(_b_save, i18n.L("xh.save"))
      if _live(_b_save) else False)
    _b_del = kit.button(_ch_pos, i18n.L("xh.delete"), _ch_pre_delete,
                        fixed_w=12)
    _b_del.pack(side="left")
    R(lambda: kit.set_text(_b_del, i18n.L("xh.delete"))
      if _live(_b_del) else False)
    _ch_pre_sync()




    # ---- Detection tab: colour lists and picker on top, thresholds below, in
    #      ONE scroll region (the separate Colours tab is gone) ----
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
    rgb_cell.grid(row=2, column=0, sticky="we", padx=10, pady=4)
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
                sug = vision.suggest_accents(img, SUG_N)
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
        """One macro field. With a capture `kind` it comes back wrapped in a
        frame holding the entry AND its "Press" button, so the caller's
        .pack(side="left") places the pair without knowing about it."""
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

    # ---- Keyboard & Mouse tab and Controller tab: flat rows, no titles ----
    kbp = pages["keyboard"]
    kinner, kbind = _scrollable(kbp, kit, pal)
    _FIT.append(kinner)
    kgrid = tk.Frame(kinner, bg=pal["card"])
    kit.reg(kgrid, "card")
    kgrid.pack(fill="x", padx=6, pady=8)
    kgrid.columnconfigure(0, weight=1)
    for _ki, _kkey in enumerate(TAB_KEYBOARD):
        make_row(kgrid, _kkey, _ki)

    # ---- Controller tab ----
    ctrlp = pages["controller"]
    cinner, cbind = _scrollable(ctrlp, kit, pal)
    _FIT.append(cinner)
    cgrid = tk.Frame(cinner, bg=pal["card"])
    kit.reg(cgrid, "card")
    cgrid.pack(fill="x", padx=6, pady=8)
    cgrid.columnconfigure(0, weight=1)
    for _pi, _pkey in enumerate(TAB_CONTROLLER):
        make_row(cgrid, _pkey, _pi)

    # ---- Macros tab: two rows, each pairing one macro family (left) with
    #      its own independent recording library (right), top-aligned -
    #      keyboard macros + Recordings on top, controller macros +
    #      Controller Recordings below ----
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

        macro_entry(row, mkey, 10, mkey, "key").pack(side="left",
                                                     padx=(0, 8))
        macro_word(row, "mac.sends", mact)
        macro_entry(row, mact, 13, mact, "key").pack(side="left")

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




    for _rk, _rf, _kd in (("mac.delete", do_delete_rec, "amber"),
                          ("mac.import", do_import_rec, "plain"),
                          ("mac.export", do_export_rec, "plain"),
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

    for _rk, _rf, _kd in (("mac.delete", pdo_delete_rec, "amber"),
                          ("mac.import", pdo_import_rec, "plain"),
                          ("mac.export", pdo_export_rec, "plain"),
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

    lat_handle = latency_tabs.mount(
        root, pages, kit, pal,
        {"base": F_BASE, "bold": F_BOLD, "h1": F_H1, "entry": F_ENTRY},
        out_root=None,
        backend="auto",
        cfg=cfg,
    )
    root.after(latency_tabs.POLL_MS,
               lambda: latency_tabs.pump(root, lat_handle))
    root.bind("<Destroy>", lambda _e: latency_tabs.shutdown(lat_handle),
              add="+")


    _FIT_DOC.append(_fill_help(pages["help"], kit, pal))


    poll_tick = [0]
    rec_ui = [None]

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
                if root.winfo_exists():
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
        """Size the window to the widest and tallest SETTINGS tab, clamped to
        the screen, so no setting needs scrolling at the default size.

        Measured rather than hardcoded: the tabs move as settings are added,
        and a fixed number either wastes space or cuts content off. Help is
        excluded from the height - it is prose that is meant to scroll. Called
        twice because the tab bar's own height depends on the width it is
        given, so the first pass can change how many rows it wraps into.
        """
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
    poll_status()
    watchdog()
    log("GUI ready - signed in as %s."
        % online.identity_label(identity))
    root.deiconify()  # show fully built window
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
                identity, ent = online.me(cfg, fresh=True)
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
                identity, ent = online.me(cfg, fresh=True)
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
