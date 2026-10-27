
import json
import os
import sys
import threading
from configparser import ConfigParser

import general_utils as build_secrets
import input_utils as gamepad
import input_utils as hotkeys
import system_core as i18n
from system_core import log
from system_core import parse_accents



MOUSE_BUTTONS = ("mouse1", "mouse2", "mouse3", "mouse4", "mouse5")

i18n.publish("en", {
    "st.import_fail": "Import failed: %r",
    "st.import_notfile": "Import failed: not a settings file.",
    "st.imported": "Imported %d setting(s)%s.",
    "st.imported_ignored": ", ignored: %s",
    "st.exported": "Settings exported to %s",
    "st.export_fail": "Export failed: %r",
})

DEFAULT_CONFIG = {






















    "mode": 2,


    "radius": 150,

    "unibot_aim": True,
    "aim_speed": 1.0,


























    "theme": "light",



    "watcher_seconds": 0.45,


    "stall_pause_seconds": 0.25,


    "contrast_threshold": 38,
    "min_contrast": 48,
    "min_area": 220,
    "max_area": 45000,
    "max_dim": 300,
    "on_target_dist": 10,







    "adaptive_threshold": True,
    "luminance_norm": True,










    "capture_backend": "auto",

















    "input_backend": "win32",

    "timer_resolution_us": 0,








    "merge_distance": 18,









    "accent_color": "off",
    "accent_tolerance": 26,









    "accent_soft": 60,












    "accent_matcher": "oklab",

    "accent_boost": 0.9,








    "exclude_color": "off",








    "motion_predict": False,
    "motion_lead_ms": 50,

    "hpose_mode": "off",
    "hpose_view": "any",
    "hpose_stance": "any",
    "hpose_aim": "center",
    "hpose_lead_ms": 60,
    "hpose_min_conf": 35,
    "hpose_model": "yolo26n-pose.onnx",
    "blob_watch": True,








    "watch_center_only": False,









    "center_follow": False,






    "scan_budget_ms": 350,








    "motion_3d": False,








    "accent_only": False,





























    "follow_enabled": False,
    "follow_boost": 1.0,
    "follow_seconds": 20,






    "glide_ms": 0,








    "snap_offset_x": 0,
    "snap_offset_y": 0,










    "color_adapt": True,



    "accent_variants": True,



    "accent_coherent": True,






    "language": "en",













    "trigger_button": "mouse1",
    "options_button": "mouse2",
    "shift_bypass": True,
    "bypass_key": "shift",
    "bypass_pad": "",





    "hold_follow": True,











    "center_blind_area": 10,







    "center_max_step": 160,






    "subject_focus_bias": 0,









    "motion_sense": 0,














    "motion_priority": True,
    "motion_camera_comp": True,

    "subject_priority": True,
    "subject_only": False,
    "debug_visualize": False,

    "reticle_enabled": False,
    "reticle_shape": "cross",
    "reticle_mode": "center",
    "reticle_size": 24,
    "reticle_gap": 6,
    "reticle_thickness": 2,
    "reticle_rotation": 0,
    "reticle_opacity": 90,
    "reticle_color": "0,255,0",
    "reticle_x": 0,
    "reticle_y": 0,









    "rapid_click_snap": False,










    "mouse2_action": 4,









    "mouse2_hold_action": 4,






    "follow_learn": "accent",






    "key_launch": "ctrl+l",
    "key_pause": "ctrl+p",
    "key_mode1": "1",
    "key_mode2": "2",
    "key_record": "ctrl+alt+r",
    "key_record_stop": "ctrl+alt+s",
    "key_play": "ctrl+alt+m",
    "key_play1": "ctrl+1",
    "key_play2": "ctrl+2",
    "key_play3": "ctrl+3",
    "key_play4": "ctrl+4",
    "key_play5": "ctrl+5",






    "macro1_key": "",       "macro1_action": "",
    "macro2_key": "",       "macro2_action": "",
    "macro3_key": "",       "macro3_action": "",
    "macro4_key": "",       "macro4_action": "",
    "macro5_key": "",       "macro5_action": "",

    "controller_input": False,
    "pad_index": -1,
    "pad_output": "auto",
    "pad_drive": True,
    "pad_deadzone": 20,
    "pad_sensitivity": 1.0,
    "pad_trigger_threshold": 128,
    "pad_poll_hz": 125,
    "pad_rumble": True,
    "pad_click_button": "rt",
    "pad_right_button": "lt",
    "pad_emulate": gamepad.PAD_EMU_DEFAULT,

    "pad_launch": "start",
    "pad_pause": "back",
    "pad_mode1": "",
    "pad_mode2": "",
    "pad_record": "ls+rs",
    "pad_record_stop": "",
    "pad_play": "",
    "pad_play1": "",
    "pad_play2": "",
    "pad_play3": "",
    "pad_play4": "",
    "pad_play5": "",

    "pad_macro1_key": "",   "pad_macro1_action": "",
    "pad_macro2_key": "",   "pad_macro2_action": "",
    "pad_macro3_key": "",   "pad_macro3_action": "",
    "pad_macro4_key": "",   "pad_macro4_action": "",
    "pad_macro5_key": "",   "pad_macro5_action": "",



    "config_version": 8,
}

RADIUS_STEPS = [(80, "small"), (150, "medium"), (250, "large")]


def config_dir():
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))

    try:
        probe = os.path.join(base, ".snappyai_write_test")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.remove(probe)
        return base
    except OSError:
        appdata = os.environ.get("APPDATA")
        if appdata:
            d = os.path.join(appdata, "SnappyAI")
        else:
            d = os.path.join(os.path.expanduser("~"), ".snappyai")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        return d


class SafeConfigReader:

    def __init__(self, path):
        self.path = path

    def read_overrides(self):
        overrides = {}
        bad = []
        if not os.path.exists(self.path):
            return overrides

        parser = ConfigParser(strict=False, interpolation=None)
        parser.optionxform = lambda s: s.lower()

        try:
            parser.read(self.path, encoding="utf-8")
        except Exception:
            return overrides









        sections = [s for s in ("accessibility", "snappyai")
                    if parser.has_section(s)]

        def _collect(section, has_option, getter):
            for key, default in DEFAULT_CONFIG.items():
                if key in build_secrets.SENSITIVE_KEYS:
                    continue
                if not has_option(key):
                    continue
                try:
                    raw = getter(key)
                except Exception:
                    continue
                try:
                    overrides[key] = self._convert(raw, default)
                except Exception:
                    bad.append((section, key, raw))

        for section in sections:
            _collect(section,
                   lambda k, s=section: parser.has_option(s, k),
                   lambda k, s=section: parser.get(s, k))

        defaults = parser.defaults()
        if defaults:
            _collect("DEFAULT",
                     lambda k: k in defaults,
                     lambda k: defaults[k])


        if "input_backend" in overrides:
            val = str(overrides["input_backend"]).strip().lower()
            if val == "interception":
                overrides["input_backend"] = "driver"
            elif val in ("rebind", "server"):
                overrides["input_backend"] = "win32"

        for section, key, raw in bad:
            log("config.ini: ignored invalid %s in [%s]: %r"
                % (key, section, raw))

        return overrides

    @staticmethod
    def _convert(raw, default):
        raw = raw.strip()

        if isinstance(default, bool):
            val = raw.lower()
            if val not in ("1", "true", "yes", "on", "0", "false", "no", "off"):
                raise ValueError("not a boolean")
            return val in ("1", "true", "yes", "on")

        if isinstance(default, int):



            val = float(raw)
            rounded = int(round(val))
            if rounded != val:
                log("Config value %r is not an integer; rounded to "
                    "%d." % (raw, rounded))
            return rounded

        if isinstance(default, float):
            return float(raw)

        return raw


def _coerce_value(raw, default, key=None):
    try:
        if isinstance(default, bool):
            if isinstance(raw, bool):
                return True, raw
            if isinstance(raw, str):
                return True, raw.strip().lower() in ("1", "true", "yes", "on")
            if isinstance(raw, (int, float)):
                return True, bool(raw)
            return False, None

        if isinstance(default, int):
            if isinstance(raw, bool):
                return False, None
            if isinstance(raw, int):
                return True, raw
            if isinstance(raw, float):
                return True, int(round(raw))
            if isinstance(raw, str):
                return True, int(round(float(raw.strip())))
            return False, None

        if isinstance(default, float):
            if isinstance(raw, bool):
                return False, None
            if isinstance(raw, (int, float)):
                return True, float(raw)
            if isinstance(raw, str):
                return True, float(raw.strip())
            return False, None


        if isinstance(raw, str):
            return True, raw
        if isinstance(raw, (list, tuple)):
            if key not in ("accent_color", "exclude_color"):
                return False, None
            return True, raw

        return False, None
    except (ValueError, TypeError, OverflowError):


        return False, None



class Config:
    def __init__(self):
        self.lock = threading.RLock()
        self.dir = config_dir()
        self.path = os.path.join(self.dir, "config.json")
        self.ini_path = os.path.join(self.dir, "config.ini")
        self.values = dict(DEFAULT_CONFIG)
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)

            if isinstance(data, dict):
                _RENAMED_KEYS = {
                    "figure_head_bias": "subject_focus_bias",
                    "game_shape_priority": "subject_priority",
                }
                for _old, _new in _RENAMED_KEYS.items():
                    if _old in data and _new not in data:
                        data[_new] = data[_old]
                    elif _old in data:
                        log("config.json: both %s and %s present; using %s."
                            % (_old, _new, _new))
                if str(data.get("input_backend", "")).strip().lower() == "interception":
                    data["input_backend"] = "driver"
                elif str(data.get("input_backend", "")).strip().lower() in ("rebind", "server"):
                    data["input_backend"] = "win32"
                for key in DEFAULT_CONFIG:
                    if key in data:
                        ok, value = _coerce_value(data[key],
                                                   DEFAULT_CONFIG[key], key)
                        if ok:
                            self.values[key] = value
                        else:
                            log("config.json: ignored invalid %s = %r"
                                % (key, data[key]))






                stale = [k for k in build_secrets.SENSITIVE_KEYS
                         if k in data]
                if stale:
                    for k in stale:
                        self.values.pop(k, None)
                    log("config.json: removed %d deployment key(s) left by "
                        "an older version (%s); these now come from the "
                        "build, not from settings."
                        % (len(stale), ", ".join(sorted(stale))))









                retired = ("auto_click",
                           "auto_click_interval",
                           "auto_click_min_contrast",
                           "auto_click_max_clicks",
                           "auto_click_settle_ms",
                           "auto_click_repeat_px",
                           "auto_click_radius",
                           "server_url", "rebind_url",
                           "block_bound_keys", "bare_mode_keys")
                dropped = [k for k in retired if k in data]
                if dropped:
                    for k in dropped:
                        self.values.pop(k, None)
                    log("config.json: removed %d retired setting(s) "
                        "(%s); these are fixed behaviour now, not "
                        "settings." % (len(dropped),
                                       ", ".join(sorted(dropped))))



                if str(data.get("macro6_key") or "").strip() \
                        or str(data.get("macro6_action") or "").strip():
                    log("config: macro6 was removed in v8.17 (five combo "
                        "slots + recordings); its value was dropped - "
                        "re-create it as a recording or in slots 1-5.")

                try:
                    version = int(data.get("config_version", 1))
                except Exception:
                    version = 1

                if version < 8:




                    self.values["config_version"] = 8
        except (OSError, ValueError):
            pass

        try:
            overrides = SafeConfigReader(self.ini_path).read_overrides()
            if overrides:
                self.values.update(overrides)
        except Exception:
            pass

        self._sanitize()




    _RANGES = {
        "radius": (40, 400),
        "mode": (1, 2),





        "contrast_threshold": (1, 200),
        "min_contrast": (1, 200),
        "min_area": (1, 100000),
        "max_area": (100, 1000000),
        "max_dim": (10, 2000),

        "center_blind_area": (0, 100000),

        "center_max_step": (0, 500),
        "subject_focus_bias": (0, 100),
        "motion_sense": (0, 100),
        "merge_distance": (0, 64),
        "accent_tolerance": (4, 90),
        "aim_speed": (0.1, 2.0),
        "accent_soft": (0, 100),
        "accent_boost": (0.0, 3.0),
        "follow_boost": (0.0, 3.0),
        "follow_seconds": (1, 120),
        "glide_ms": (0, 500),
        "watcher_seconds": (0.0, 3.0),
        "stall_pause_seconds": (0.0, 3.0),
        "on_target_dist": (0, 100),
        "motion_lead_ms": (0, 300),
        "hpose_lead_ms": (0, 300),
        "hpose_min_conf": (0, 99),
        "scan_budget_ms": (60, 2000),



        "snap_offset_x": (-200, 200),
        "snap_offset_y": (-200, 200),

        "reticle_size": (8, 96),
        "reticle_gap": (0, 48),
        "reticle_thickness": (1, 10),
        "reticle_rotation": (0, 90),
        "reticle_opacity": (10, 100),
        "reticle_x": (-10000, 10000),
        "reticle_y": (-10000, 10000),

        "pad_index": (-1, 3),
        "pad_deadzone": (0, 60),
        "pad_sensitivity": (0.1, 4.0),
        "pad_trigger_threshold": (1, 255),
        "pad_poll_hz": (30, 500),

        "timer_resolution_us": (0, 15625),
    }

    _PAD_SPECS = ("pad_launch", "pad_pause", "pad_mode1", "pad_mode2",
                  "pad_record", "pad_record_stop", "pad_play",
                  "pad_play1", "pad_play2", "pad_play3", "pad_play4",
                  "pad_play5", "pad_macro1_key", "pad_macro2_key",
                  "pad_macro3_key", "pad_macro4_key", "pad_macro5_key",
                  "bypass_pad")

    _PAD_SINGLE = ("pad_click_button", "pad_right_button")

    _CHOICES = {
        "accent_matcher": ("hsv", "oklab"),
        "capture_backend": ("auto", "dxcam", "mss", "wgc"),
        "input_backend": ("win32", "driver"),
        "follow_learn": ("accent", "any"),
        "reticle_shape": ("cross", "plus", "dot", "circle", "x"),
        "reticle_mode": ("center", "custom"),
        "theme": ("light", "dark"),        "pad_output": ("auto", "vigem", "emulate"),
        "trigger_button": MOUSE_BUTTONS,
        "options_button": MOUSE_BUTTONS,
    }

    _LEGACY_CHOICES = {
        "input_backend": {"interception": "driver",
                          "rebind": "win32", "server": "win32"},
        "pad_output": {"vigembus": "vigem", "emulated": "emulate",
                       "km": "emulate", "keyboard": "emulate"},
    }

    @classmethod
    def _normalize_choice(cls, key, raw):
        allowed = cls._CHOICES.get(key)
        if allowed is None:
            return raw, True
        value = str(raw).strip().lower() if raw is not None else ""
        value = cls._LEGACY_CHOICES.get(key, {}).get(value, value)
        if value not in allowed:
            return DEFAULT_CONFIG[key], False
        return value, True

    def sanitize(self):
        return self._sanitize()

    def _sanitize(self):
        for key, (lo, hi) in self._RANGES.items():
            try:
                value = self.values[key]
                clamped = min(hi, max(lo, value))
                if isinstance(lo, int):
                    clamped = min(hi, max(lo, int(round(clamped))))
                else:
                    clamped = type(lo)(clamped)
                if clamped != value:
                    log("config: %s %r clamped to %r" % (key, value, clamped))
                self.values[key] = clamped
            except (KeyError, TypeError, ValueError):



                if key in DEFAULT_CONFIG:
                    self.values[key] = DEFAULT_CONFIG[key]
                else:
                    self.values.pop(key, None)



        if self.values.get("min_area", 0) > self.values.get("max_area", 0):
            log("config: min_area %r is above max_area %r - nothing could "
                "match; using max_area for both."
                % (self.values.get("min_area"), self.values.get("max_area")))
            self.values["min_area"] = self.values["max_area"]




        for key in self._CHOICES:
            raw = self.values.get(key, DEFAULT_CONFIG[key])
            value, ok = self._normalize_choice(key, raw)
            if not ok:
                log("config: %s %r unknown; using %r"
                    % (key, raw, DEFAULT_CONFIG[key]))
            self.values[key] = value









        if self.values.get("accent_only") \
                and not parse_accents(self.values.get("accent_color")):
            log("config: accent_only is on but accent_color is off - "
                "it stays on and simply does nothing until a colour "
                "is set again.")



        lang = str(self.values.get("language", "en")).strip().lower()
        if lang not in i18n.codes():
            log("config: unknown language %r, defaulting to en" % (lang,))
            lang = "en"
        self.values["language"] = lang


        for key in self._PAD_SPECS:
            raw = str(self.values.get(key, "") or "").strip()
            if not raw:
                self.values[key] = ""
                continue
            if gamepad.parse_spec(raw) is None:
                log("config: %s %r is not a controller button or a "
                    "combination of them (try 'a', 'rb+a', 'ls+rs'); that "
                    "action stays unbound." % (key, raw))
                self.values[key] = ""
            else:
                self.values[key] = gamepad.describe_spec(raw)

        for key in self._PAD_SINGLE:
            raw = str(self.values.get(key, "") or "").strip()
            index = gamepad.parse_token(raw)
            if index is None:
                log("config: %s %r is not a single controller button; using "
                    "%r." % (key, raw, DEFAULT_CONFIG[key]))
                self.values[key] = DEFAULT_CONFIG[key]
            else:
                self.values[key] = gamepad.PAD_BUTTONS[index]

        if (str(self.values.get("options_button"))
                == str(self.values.get("trigger_button"))):
            log("config: options_button %r is the same as trigger_button; "
                "using %r for the extra-options role."
                % (self.values.get("options_button"),
                   DEFAULT_CONFIG["options_button"]))
            self.values["options_button"] = DEFAULT_CONFIG["options_button"]
            if (str(self.values["options_button"])
                    == str(self.values.get("trigger_button"))):
                log("config: trigger_button %r collides with the default "
                    "options button; using %r for the trigger role."
                    % (self.values.get("trigger_button"),
                       DEFAULT_CONFIG["trigger_button"]))
                self.values["trigger_button"] = DEFAULT_CONFIG["trigger_button"]

        raw = str(self.values.get("bypass_key", "shift") or "").strip()
        canonical = hotkeys.describe_bypass_spec(raw)
        if not canonical:
            log("config: bypass_key %r is not a key, a modifier or a "
                "combination of them; using %r."
                % (raw, DEFAULT_CONFIG["bypass_key"]))
            canonical = str(DEFAULT_CONFIG["bypass_key"])
        self.values["bypass_key"] = canonical

        for key in ("mouse2_action", "mouse2_hold_action"):
            try:
                 m2 = int(round(self.values.get(key, 4)))
            except (TypeError, ValueError):
                m2 = 4
            if m2 not in (1, 2, 3, 4):
                log("config: %s %r invalid; using 4 (nothing)"
                    % (key, self.values.get(key)))
                m2 = 4
            self.values[key] = m2

    def export_to(self, path, values=None):
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self._public_values(values), fh, indent=2)
            return True, i18n.L("st.exported", path)
        except OSError as exc:
            return False, i18n.L("st.export_fail", exc)

    def import_from(self, path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            return False, i18n.L("st.import_fail", exc)

        if not isinstance(data, dict):
            return False, i18n.L("st.import_notfile")

        applied = 0
        skipped = []
        refused_sensitive = []
        imported_keys = []

        with self.lock:
            for key in DEFAULT_CONFIG:
                if key not in data:
                    continue
                if key in build_secrets.SENSITIVE_KEYS:
                    refused_sensitive.append(key)
                    continue
                ok, value = _coerce_value(data[key], DEFAULT_CONFIG[key], key)
                if ok:
                    self.values[key] = value
                    imported_keys.append(key)
                else:
                    skipped.append(key)
            self._sanitize()
            applied = len([k for k in imported_keys if k in self.values])

        if refused_sensitive:
            log("import: ignored %d deployment key(s) (%s) - those come "
                "from the build, not from settings."
                % (len(refused_sensitive), ", ".join(refused_sensitive)))
        if skipped:
            log("import: ignored invalid %s" % ", ".join(skipped))

        return True, i18n.L(
            "st.imported", applied,
            "" if not skipped else i18n.L(
                "st.imported_ignored", ", ".join(skipped)))

    def reload(self):
        with self.lock:
            live_mode = self.values.get("mode")
            self.values = dict(DEFAULT_CONFIG)
            self._load()
            if live_mode in (1, 2):
                self.values["mode"] = live_mode

    def _public_values(self, values=None):
        src = self.values if values is None else values
        out = {}
        dropped = []
        for key, value in src.items():
            if key in build_secrets.SENSITIVE_KEYS:
                dropped.append(key)
                continue
            out[key] = value
        return out

    _leak_warned = False

    def save(self):
        tmp = self.path + ".tmp"
        try:
            with self.lock:
                payload = self._public_values()
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value, save=True):
        if key in build_secrets.SENSITIVE_KEYS:
            log("config.set(%r) ignored - that is a build constant, not a "
                "setting." % (key,))
            return
        if key not in DEFAULT_CONFIG:
            log("config.set(%r) ignored - unknown key" % (key,))
            return

        with self.lock:
            default = DEFAULT_CONFIG[key]
            if isinstance(value, str) and not isinstance(default, str):
                ok, coerced = _coerce_value(value, default, key)
                if ok:
                    value = coerced
                else:
                    log("config: %s %r invalid; using %r"
                        % (key, value, default))
                    value = default
            bounds = self._RANGES.get(key)
            if bounds is not None:
                try:
                    lo, hi = bounds
                    clamped = min(hi, max(lo, value))
                    if isinstance(lo, int):
                        clamped = min(hi, max(lo, int(round(clamped))))
                    else:
                        clamped = type(lo)(clamped)
                    value = clamped
                except (TypeError, ValueError):
                    log("config: %s %r invalid; using %r"
                        % (key, value, DEFAULT_CONFIG.get(key)))
                    value = DEFAULT_CONFIG.get(key)
            raw = value
            value, accepted = self._normalize_choice(key, value)
            if not accepted:
                log("config: %s %r unknown; using %r" % (key, raw, value))
            self.values[key] = value
            if save:
                self.save()

    def cycle(self, key, steps):
        current = self.get(key)
        values = [v for v, _ in steps]

        if not steps:
            return current, ""

        if current in values:
            idx = values.index(current)
        else:
            idx = -1

        idx = (idx + 1) % len(steps)
        value, label = steps[idx]
        self.set(key, value)
        return value, label


