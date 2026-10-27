
import json
import math
import os

SHAPES = ("cross", "plus", "dot", "circle", "x")
MODES = ("center", "custom")
DEFAULT_COLOR = (0, 255, 0)
_TRANSPARENT = "black"


def parse_color(raw):
    try:
        s = str(raw or "").strip()
    except Exception:
        return DEFAULT_COLOR
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 6:
            try:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            except ValueError:
                return DEFAULT_COLOR
        return DEFAULT_COLOR
    try:
        parts = [int(p.strip()) for p in s.split(",")]
    except (ValueError, AttributeError):
        return DEFAULT_COLOR
    if len(parts) != 3:
        return DEFAULT_COLOR
    return tuple(min(255, max(0, p)) for p in parts)


def to_hex(rgb):
    try:
        r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    except (TypeError, ValueError, IndexError):
        r, g, b = DEFAULT_COLOR
    return "#%02x%02x%02x" % (
        min(255, max(0, r)), min(255, max(0, g)), min(255, max(0, b)))


def _rot(x, y, ang):
    c, s = math.cos(ang), math.sin(ang)
    return (x * c - y * s, x * s + y * c)


def shape_geometry(shape, size, gap, thickness, rotation=0):
    try:
        size = max(2.0, float(size))
    except (TypeError, ValueError):
        size = 24.0
    try:
        gap = max(0.0, float(gap))
    except (TypeError, ValueError):
        gap = 0.0
    try:
        ang = math.radians(max(0.0, min(90.0, float(rotation))))
    except (TypeError, ValueError):
        ang = 0.0
    shape = str(shape or "cross").strip().lower()
    lines, ovals = [], []
    if shape == "dot":
        try:
            r = max(2.0, float(thickness) * 1.5)
        except (TypeError, ValueError):
            r = 3.0
        ovals.append((-r, -r, r, r, True))
    elif shape == "circle":
        r = size / 2.0
        ovals.append((-r, -r, r, r, False))
        dr = 1.6
        ovals.append((-dr, -dr, dr, dr, True))
    else:
        axes = [(1.0, 0.0), (0.0, 1.0)] if shape in ("cross", "plus") \
            else [(0.7071, 0.7071), (0.7071, -0.7071)]
        start = 0.0 if shape in ("plus", "x") else gap
        for dx, dy in axes:
            for sgn in (1.0, -1.0):
                x1, y1 = _rot(dx * sgn * start, dy * sgn * start, ang)
                x2, y2 = _rot(dx * sgn * (start + size / 2.0),
                              dy * sgn * (start + size / 2.0), ang)
                lines.append((x1, y1, x2, y2))
    return {"lines": lines, "ovals": ovals}


def render(canvas, cx, cy, shape, size, gap, thickness, rotation, hexcolor):
    try:
        width = max(1, int(round(float(thickness))))
    except (TypeError, ValueError):
        width = 2
    geo = shape_geometry(shape, size, gap, thickness, rotation)
    for x1, y1, x2, y2 in geo["lines"]:
        canvas.create_line(cx + x1, cy + y1, cx + x2, cy + y2,
                           fill=hexcolor, width=width, capstyle="round")
    for x0, y0, x1, y1, filled in geo["ovals"]:
        if filled:
            canvas.create_oval(cx + x0, cy + y0, cx + x1, cy + y1,
                               fill=hexcolor, outline=hexcolor)
        else:
            canvas.create_oval(cx + x0, cy + y0, cx + x1, cy + y1,
                               outline=hexcolor, width=width)


def preset_path(config_dir=None):
    if config_dir is None:
        try:
            from config import config_dir as _dir
            config_dir = _dir()
        except Exception:
            config_dir = os.path.expanduser("~")
    return os.path.join(config_dir, "reticle_presets.json")


def list_presets(config_dir=None):
    try:
        with open(preset_path(config_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_preset(name, values, config_dir=None):
    name = str(name or "").strip()
    if not name:
        raise ValueError("preset name is empty")
    data = list_presets(config_dir)
    data[name] = {k: v for k, v in dict(values).items()
                  if str(k).startswith("reticle_")}
    tmp = preset_path(config_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, preset_path(config_dir))
    return True


def load_preset(name, config_dir=None):
    return list_presets(config_dir).get(str(name or "").strip())


def delete_preset(name, config_dir=None):
    data = list_presets(config_dir)
    if str(name or "").strip() not in data:
        return False
    del data[str(name or "").strip()]
    tmp = preset_path(config_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, preset_path(config_dir))
    return True


def shape_bounds(shape, size, gap, thickness, rotation=0):
    try:
        thick = max(1.0, float(thickness))
    except (TypeError, ValueError):
        thick = 2.0
    geo = shape_geometry(shape, size, gap, thickness, rotation)
    reach = 0.0
    for x1, y1, x2, y2 in geo["lines"]:
        reach = max(reach, abs(x1), abs(y1), abs(x2), abs(y2))
    for x0, y0, x1, y1, _filled in geo["ovals"]:
        reach = max(reach, abs(x0), abs(y0), abs(x1), abs(y1))
    return int(math.ceil(reach + thick + 4.0))


def _click_through_ok(win):
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = win.winfo_id()
        prev = user32.GetWindowLongW(hwnd, -20)
        user32.SetWindowLongW(hwnd, -20, prev | 0x80000 | 0x20)
        return bool(user32.GetWindowLongW(hwnd, -20) & 0x20)
    except Exception:
        return False


def _apply_layered(win, opacity):
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = win.winfo_id()
        try:
            alpha = int(round(min(1.0, max(0.1, float(opacity))) * 255.0))
        except (TypeError, ValueError):
            alpha = 230
        return bool(user32.SetLayeredWindowAttributes(hwnd, 0x000000,
                                                      alpha, 0x1 | 0x2))
    except Exception:
        return False


class ReticleOverlay(object):
    def __init__(self, root, cfg):
        self._root = root
        self._cfg = cfg
        self._win = None
        self._canvas = None
        self._sig = None
        self._tick_id = None
        self._debug = None
        self._dbg_win = None
        self._dbg_canvas = None

    def set_debug(self, cx, cy, radius, target_box=None, candidates=None):
        self._debug = (cx, cy, radius, target_box, candidates or ())

    def _screen(self):
        try:
            from win32rt import virtual_screen
            if virtual_screen is None:
                raise ValueError("no virtual screen")
            return virtual_screen()
        except Exception:
            try:
                w = self._root.winfo_screenwidth()
                h = self._root.winfo_screenheight()
            except Exception:
                w, h = 1920, 1080
            return (0, 0, w, h)

    def _signature(self):
        g = self._cfg.get
        return (bool(g("reticle_enabled", False)), g("reticle_shape"),
                g("reticle_mode"), g("reticle_size"), g("reticle_gap"),
                g("reticle_thickness"), g("reticle_rotation"),
                g("reticle_opacity"), g("reticle_color"),
                g("reticle_x"), g("reticle_y"), self._screen())

    def start(self):
        self.tick()

    def stop(self):
        try:
            if self._tick_id is not None:
                self._root.after_cancel(self._tick_id)
        except Exception:
            pass
        self._tick_id = None
        self.hide()
        self._debug_destroy()

    def tick(self):
        try:
            sig = self._signature()
        except Exception:
            sig = None
        if sig is not None and sig != self._sig:
            self._sig = sig
            try:
                self.refresh()
            except Exception:
                pass
        try:
            self._debug_refresh()
        except Exception:
            pass
        try:
            self._tick_id = self._root.after(250, self.tick)
        except Exception:
            self._tick_id = None

    def hide(self):
        win, self._win = self._win, None
        self._canvas = None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def _debug_destroy(self):
        win, self._dbg_win = self._dbg_win, None
        self._dbg_canvas = None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def _debug_refresh(self):
        if not bool(self._cfg.get('debug_visualize', False)) \
                or not self._debug:
            self._debug_destroy()
            return
        vx, vy, vw, vh = self._screen()
        if self._dbg_win is None:
            import tkinter as tk
            try:
                win = tk.Toplevel(self._root)
                win.overrideredirect(True)
                win.attributes('-topmost', True)
                win.configure(bg=_TRANSPARENT)
                win.attributes('-transparentcolor', _TRANSPARENT)
            except Exception:
                self._debug_destroy()
                return
            if not _click_through_ok(win):
                try:
                    win.destroy()
                except Exception:
                    pass
                return
            if not _apply_layered(win, 1.0):
                try:
                    win.destroy()
                except Exception:
                    pass
                return
            canvas = tk.Canvas(win, bg=_TRANSPARENT, highlightthickness=0,
                               bd=0, width=vw, height=vh)
            canvas.pack()
            try:
                win.geometry('%dx%d+%d+%d' % (vw, vh, vx, vy))
            except Exception:
                pass
            self._dbg_win = win
            self._dbg_canvas = canvas
        else:
            want = '%dx%d+%d+%d' % (vw, vh, vx, vy)
            try:
                if self._dbg_win.geometry() != want:
                    self._dbg_win.geometry(want)
                    self._dbg_canvas.configure(width=vw, height=vh)
            except Exception:
                self._debug_destroy()
                return
        try:
            cx, cy, radius, target_center, _candidates = self._debug
            c = self._dbg_canvas
            c.delete('all')
            if target_center:
                tx, ty = target_center
                lx, ly = tx - vx, ty - vy
                # draw a green dot at the absolute center of the detected object
                c.create_oval(lx - 4, ly - 4, lx + 4, ly + 4,
                              outline='#00FF00', fill='#00FF00', width=0)
        except Exception:
            self._debug_destroy()

    def refresh(self):
        if not bool(self._cfg.get("reticle_enabled", False)):
            self.hide()
            return
        vx, vy, vw, vh = self._screen()
        size = int(self._cfg.get("reticle_size", 24) or 24)
        gap = int(self._cfg.get("reticle_gap", 6) or 0)
        thick = int(self._cfg.get("reticle_thickness", 2) or 2)
        rot = int(self._cfg.get("reticle_rotation", 0) or 0)
        shape = self._cfg.get("reticle_shape", "cross")
        try:
            r = shape_bounds(shape, size, gap, thick, rot)
        except Exception:
            r = size + 20
        w = h = r * 2 + 4
        if self._win is None:
            import tkinter as tk
            win = tk.Toplevel(self._root)
            try:
                win.overrideredirect(True)
            except Exception:
                pass
            try:
                win.attributes("-topmost", True)
            except Exception:
                pass
            try:
                win.configure(bg=_TRANSPARENT)
                win.attributes("-transparentcolor", _TRANSPARENT)
            except Exception:
                pass
            if not _click_through_ok(win):
                try:
                    win.destroy()
                except Exception:
                    pass
                self._win = None
                self._canvas = None
                return
            canvas = tk.Canvas(win, bg=_TRANSPARENT, highlightthickness=0,
                               bd=0, width=w, height=h)
            canvas.pack()
            self._win = win
            self._canvas = canvas
        mode = str(self._cfg.get("reticle_mode", "center") or "center")
        if mode.strip().lower() == "custom":
            try:
                px = int(self._cfg.get("reticle_x", 0) or 0)
                py = int(self._cfg.get("reticle_y", 0) or 0)
            except (TypeError, ValueError):
                px, py = vx + vw // 2, vy + vh // 2
        else:
            px, py = vx + vw // 2, vy + vh // 2
        try:
            self._win.geometry("%dx%d+%d+%d" % (w, h, px - r, py - r))
        except Exception:
            pass
        try:
            _opac = float(self._cfg.get("reticle_opacity", 90)) / 100.0
        except (TypeError, ValueError):
            _opac = 0.9
        if not _apply_layered(self._win, _opac):
            self.hide()
            return
        _col = parse_color(self._cfg.get("reticle_color"))
        if _col == (0, 0, 0):
            _col = (1, 1, 1)
        try:
            self._canvas.delete("all")
            render(self._canvas, r, r,
                   self._cfg.get("reticle_shape", "cross"),
                   size, gap, thick, rot, to_hex(_col))
        except Exception:
            pass

    def _click_through(self, win):
        try:
            import ctypes
            hwnd = win.winfo_id()
            gwl = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, gwl | 0x80000 | 0x20)
            trans = int("0x000000", 16)
            ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, trans, 0, 0x1)
        except Exception:
            pass
