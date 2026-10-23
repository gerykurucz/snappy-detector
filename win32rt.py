
import threading
import time

import numpy as np

from system_core import IS_WINDOWS, log

if IS_WINDOWS:
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    WH_MOUSE_LL = 14
    WH_KEYBOARD_LL = 13
    HC_ACTION = 0
    WM_QUIT = 0x0012
    WM_HOTKEY = 0x0312
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    LLKHF_INJECTED = 0x00000010





    WM_MOUSEMOVE = 0x0200
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203

    WM_RBUTTONDOWN = 0x0204
    WM_RBUTTONUP = 0x0205

    WM_MBUTTONDOWN = 0x0207
    WM_MBUTTONUP = 0x0208
    WM_XBUTTONDOWN = 0x020B
    WM_XBUTTONUP = 0x020C

    LLMHF_INJECTED = 0x00000001
    LLMHF_LOWER_IL_INJECTED = 0x00000002

    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_NOREPEAT = 0x4000

    VK_SHIFT = 0x10
    VK_CONTROL = 0x11
    VK_MENU = 0x12
    VK_LBUTTON = 0x01
    VK_RBUTTON = 0x02
    VK_LWIN = 0x5B

    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79

    INPUT_MOUSE = 0
    INPUT_KEYBOARD = 1
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_MIDDLEDOWN = 0x0020
    MOUSEEVENTF_MIDDLEUP = 0x0040
    MOUSEEVENTF_XDOWN = 0x0080
    MOUSEEVENTF_XUP = 0x0100
    MOUSEEVENTF_WHEEL = 0x0800
    MOUSEEVENTF_HWHEEL = 0x1000
    XBUTTON1 = 0x0001
    XBUTTON2 = 0x0002
    WHEEL_DELTA = 120
    KEYEVENTF_KEYUP = 0x0002

    WPARAM_T = ctypes.c_size_t
    LPARAM_T = ctypes.c_ssize_t
    LRESULT_T = ctypes.c_ssize_t

    class POINT(ctypes.Structure):
        _fields_ = [("x", wt.LONG), ("y", wt.LONG)]

    class _CURSORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD),
                    ("flags", wt.DWORD),
                    ("hCursor", wt.HANDLE),
                    ("pt", POINT)]

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt", POINT),
            ("mouseData", wt.DWORD),
            ("flags", wt.DWORD),
            ("time", wt.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wt.DWORD),
            ("scanCode", wt.DWORD),
            ("flags", wt.DWORD),
            ("time", wt.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("message", wt.UINT),
            ("wParam", WPARAM_T),
            ("lParam", LPARAM_T),
            ("time", wt.DWORD),
            ("pt", POINT),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wt.LONG),
            ("dy", wt.LONG),
            ("mouseData", wt.DWORD),
            ("dwFlags", wt.DWORD),
            ("time", wt.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wt.WORD),
            ("wScan", wt.WORD),
            ("dwFlags", wt.DWORD),
            ("time", wt.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wt.DWORD), ("u", _INPUT_UNION)]

    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT_T, ctypes.c_int, WPARAM_T, LPARAM_T)

    def _resolve(lib, *names):
        for name in names:
            try:
                return getattr(lib, name)
            except AttributeError:
                continue
        raise AttributeError("function not found: %s" % (names,))

    RegisterHotKey = _resolve(user32, "RegisterHotKey", "RegisterHotKeyW")
    UnregisterHotKey = _resolve(user32, "UnregisterHotKey")
    PostThreadMessage = _resolve(user32, "PostThreadMessageW", "PostThreadMessage")
    GetMessage = _resolve(user32, "GetMessageW", "GetMessage")
    PostMessageW = _resolve(user32, "PostMessageW", "PostMessage")

    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int,
        HOOKPROC,
        ctypes.c_void_p,
        wt.DWORD,
    ]

    user32.CallNextHookEx.restype = LRESULT_T
    user32.CallNextHookEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        WPARAM_T,
        LPARAM_T,
    ]

    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]

    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]

    kernel32.GetCurrentThreadId.restype = wt.DWORD

    GetMessage.restype = ctypes.c_int
    GetMessage.argtypes = [
        ctypes.POINTER(MSG),
        ctypes.c_void_p,
        wt.UINT,
        wt.UINT,
    ]

    RegisterHotKey.restype = wt.BOOL
    RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]

    UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]

    PostThreadMessage.argtypes = [wt.DWORD, wt.UINT, WPARAM_T, LPARAM_T]

    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]

    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetForegroundWindow.argtypes = []

    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]

    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]

    user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    try:
        user32.GetCursorInfo.restype = wt.BOOL
        user32.GetCursorInfo.argtypes = [ctypes.POINTER(_CURSORINFO)]
    except Exception:
        pass

    if hasattr(user32, "GetPhysicalCursorPos"):
        user32.GetPhysicalCursorPos.argtypes = [ctypes.POINTER(POINT)]

    user32.WindowFromPoint.restype = ctypes.c_void_p
    user32.WindowFromPoint.argtypes = [POINT]

    user32.ScreenToClient.argtypes = [ctypes.c_void_p, ctypes.POINTER(POINT)]

    PostMessageW.restype = wt.BOOL
    PostMessageW.argtypes = [ctypes.c_void_p, wt.UINT, WPARAM_T, LPARAM_T]

    user32.SendInput.restype = wt.UINT
    user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]



    _MACRO_MOD_VKS = ((0x0004, VK_SHIFT), (0x0002, VK_CONTROL),
                      (0x0001, VK_MENU), (0x0008, VK_LWIN))

    def inject_combo(mods, vk):
        seq = [mv for mask, mv in _MACRO_MOD_VKS if mods & mask]
        seq.append(vk)

        inputs = []
        for key in seq:
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.u.ki.wVk = key
            inputs.append(inp)
        for key in reversed(seq):
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.u.ki.wVk = key
            inp.u.ki.dwFlags = KEYEVENTF_KEYUP
            inputs.append(inp)

        arr = (INPUT * len(inputs))(*inputs)
        try:
            sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
        except Exception:
            return False
        return sent == len(inputs)







    def _key_input(vk, up):
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.u.ki.wVk = vk
        inp.u.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
        return inp

    def inject_key(mods, vk, down):
        """A modifier+key transition that can be HELD, unlike inject_combo.

        Down presses the modifiers then the key; up releases the key then the
        modifiers in reverse, so a held controller button behaves exactly like
        a held key on the keyboard and nothing is ever left stuck.
        """
        held = [mv for mask, mv in _MACRO_MOD_VKS if mods & mask]
        if down:
            order = [(k, False) for k in held] + [(vk, False)]
        else:
            order = [(vk, True)] + [(k, True) for k in reversed(held)]
        inputs = [_key_input(k, up) for k, up in order]
        if not inputs:
            return False
        arr = (INPUT * len(inputs))(*inputs)
        try:
            sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
        except Exception:
            return False
        return sent == len(inputs)

    _MOUSE_BTN_FLAGS = (
        (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP),
        (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP),
    )

    _MOUSE_BTN_DATA = (0, 0, 0, XBUTTON1, XBUTTON2)

    def move_cursor(x, y):
        try:
            return bool(user32.SetCursorPos(int(x), int(y)))
        except Exception:
            return False

    def inject_mouse(button, down):
        """One button transition. 0=left 1=right 2=middle 3=XBUTTON1 4=XBUTTON2.

        The two side buttons need mouseData as well as the XDOWN/XUP flags,
        which is why the table carries a third column.
        """
        button = int(button)
        if not 0 <= button < len(_MOUSE_BTN_FLAGS):
            return False
        flags = _MOUSE_BTN_FLAGS[button]
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.u.mi.dwFlags = flags[0] if down else flags[1]
        if _MOUSE_BTN_DATA[button]:
            inp.u.mi.mouseData = _MOUSE_BTN_DATA[button]
        try:
            return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) == 1
        except Exception:
            return False

    def inject_wheel(clicks, horizontal=False):
        """Scroll wheel notches (negative = down/left). Never raises."""
        try:
            steps = int(clicks)
        except (TypeError, ValueError):
            return False
        if not steps:
            return False
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.u.mi.dwFlags = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
        inp.u.mi.mouseData = (steps * WHEEL_DELTA) & 0xFFFFFFFF
        try:
            return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) == 1
        except Exception:
            return False

    def apply_window_theme(hwnd, dark, caption_rgb, text_rgb):
        try:
            dwm = ctypes.windll.dwmapi

            def _cref(rgb):
                r, g, b = rgb
                return (b << 16) | (g << 8) | r

            flag = ctypes.c_int(1 if dark else 0)
            # DWMWA_USE_IMMERSIVE_DARK_MODE is 20 on Win10 20H1+/Win11 and
            # 19 on Win10 1809-2004; try the documented value, fall back.
            if dwm.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd), 20,
                    ctypes.byref(flag), ctypes.sizeof(flag)) != 0:
                dwm.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd), 19,
                    ctypes.byref(flag), ctypes.sizeof(flag))

            cap = ctypes.c_int(_cref(caption_rgb))
            dwm.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), 35,
                ctypes.byref(cap), ctypes.sizeof(cap))

            txt = ctypes.c_int(_cref(text_rgb))
            dwm.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), 36,
                ctypes.byref(txt), ctypes.sizeof(txt))
        except Exception:
            pass

    def get_cursor_pos():
        pt = POINT()
        try:
            if user32.GetCursorPos(ctypes.byref(pt)):
                return int(pt.x), int(pt.y)
        except Exception:
            pass
        raise RuntimeError("GetCursorPos failed")

    def cursor_visible():
        try:
            ci = _CURSORINFO()
            ci.cbSize = ctypes.sizeof(_CURSORINFO)
            if user32.GetCursorInfo(ctypes.byref(ci)):
                return bool(ci.flags & 0x00000001)
        except Exception:
            pass
        return True

    def setup_dpi_awareness():
        try:
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
        except (AttributeError, OSError):
            pass
        try:
            if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
                return
        except (AttributeError, OSError):
            pass
        try:
            if user32.SetProcessDPIAware():
                return
        except (AttributeError, OSError):
            pass

    MDT_EFFECTIVE_DPI = 0
    MONITOR_DEFAULTTONEAREST = 2

    def get_dpi_scale(x=None, y=None):
        """Effective DPI scale of the monitor the point sits on (cursor when
        no point is given).

        The process asks for PER_MONITOR_AWARE_V2, so the SYSTEM dpi is the
        wrong answer on a mixed-DPI setup: it scaled every radius, centre box
        and step cap by the primary monitor's factor while the cursor was on
        the other one. Per-monitor first, system dpi next, 1.0 last.
        """
        try:
            if x is None or y is None:
                x, y = get_cursor_pos()
            user32.MonitorFromPoint.restype = ctypes.c_void_p
            user32.MonitorFromPoint.argtypes = [POINT, wt.DWORD]
            monitor = user32.MonitorFromPoint(POINT(int(x), int(y)),
                                              MONITOR_DEFAULTTONEAREST)
            if monitor:
                shcore = ctypes.windll.shcore
                shcore.GetDpiForMonitor.restype = ctypes.c_long
                shcore.GetDpiForMonitor.argtypes = [
                    ctypes.c_void_p, ctypes.c_int,
                    ctypes.POINTER(wt.UINT), ctypes.POINTER(wt.UINT)]
                dpi_x = wt.UINT()
                dpi_y = wt.UINT()
                if shcore.GetDpiForMonitor(monitor, MDT_EFFECTIVE_DPI,
                                           ctypes.byref(dpi_x),
                                           ctypes.byref(dpi_y)) == 0 \
                        and dpi_x.value:
                    return max(0.5, float(dpi_x.value) / 96.0)
        except (AttributeError, OSError, ValueError, TypeError):
            pass
        try:
            get_dpi = getattr(user32, "GetDpiForSystem", None)
            if get_dpi is not None:
                try:
                    get_dpi.restype = ctypes.c_uint
                except Exception:
                    pass
                dpi = get_dpi()
                if dpi:
                    return max(0.5, float(dpi) / 96.0)
        except (AttributeError, OSError):
            pass
        return 1.0

    def virtual_screen():
        try:
            return (int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
                    int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
                    int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
                    int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)))
        except Exception:
            try:
                return (0, 0, int(user32.GetSystemMetrics(0)),
                        int(user32.GetSystemMetrics(1)))
            except Exception:
                return 0, 0, 1920, 1080





    class CaptureBackend(object):

        def __init__(self, prefer="auto"):
            self.prefer = str(prefer or "auto").lower()
            self._dx_forced = self.prefer == "dxcam"
            self._dx_cam = None
            self._dx_tried = False
            self._dx_failures = 0
            self._dx_lock = threading.Lock()
            self._wgc_state = None
            self._wgc_tried = False
            self._wgc_lock = threading.Lock()






            self._mss_lock = threading.Lock()
            self._mss_cache = {}
            self._notes = set()
            self._note_lock = threading.Lock()







            self.stats_lock = threading.Lock()
            self.stats = {
                "dxcam": {"frames": 0, "black": 0, "frozen": 0},
                "mss": {"frames": 0, "black": 0, "frozen": 0},
                "wgc": {"frames": 0, "black": 0, "frozen": 0},
            }
            self._last_sig = {}
            self._bad_noted = False

        def _note(self, msg):
            with self._note_lock:
                if msg not in self._notes:
                    self._notes.add(msg)
                    log(msg)

        def _record(self, which, img):
            try:
                if img is None or img.size == 0:
                    sig = None
                    blank = True
                else:
                    view = img[:: max(1, img.shape[0] // 8),
                               :: max(1, img.shape[1] // 8)]
                    if not view.any():
                        with self.stats_lock:
                            self.stats[which]["frames"] += 1
                            self.stats[which]["black"] += 1
                        self._check_bad(which)
                        return
                    sig = hash(view.tobytes())
                    blank = bool((view == view[0, 0]).all())
                with self.stats_lock:
                    st = self.stats[which]
                    st["frames"] += 1
                    prev = self._last_sig.get(which)
                    if prev is not None and sig == prev and not blank:
                        st["frozen"] += 1
                    if sig is not None:
                        self._last_sig[which] = sig
            except Exception:
                return
            self._check_bad(which)

        def _check_bad(self, which):
            with self.stats_lock:
                st = self.stats[which]
                total = st["frames"]
                bad = st["black"] + st["frozen"]
            if total >= 20 and bad * 4 >= total * 3 and not self._bad_noted:
                self._bad_noted = True
                self._note(
                    "Capture: %s frames are mostly black or unchanging "
                    "(%d/%d). The screen looks correct to YOUR eyes but "
                    "this engine cannot see the target window - common "
                    "with hardware-accelerated apps and DRM video. "
                    "Try capture_backend = wgc (or mss) in Settings; run "
                    "'capture report' in the console for details."
                    % (which, bad, total))

        def capture_report(self):
            lines = []
            active = ("wgc" if self.prefer == "wgc" and self._wgc_state
                      else "dxcam" if (self._dx_forced or self.prefer == "wgc")
                      and self._dx_cam is not None else "mss")
            lines.append("Capture engine in use: preferred=%s, active=%s"
                         % (self.prefer, active))
            for name in ("dxcam", "wgc", "mss"):
                tried = {"dxcam": self._dx_tried,
                         "wgc": self._wgc_tried,
                         "mss": True}.get(name)
                with self.stats_lock:
                    st = dict(self.stats[name])
                if not tried and st["frames"] == 0:
                    hint = (" (auto prefers mss; set capture_backend = dxcam "
                            "to opt in)") if name == "dxcam" else ""
                    lines.append("  %-5s : not used%s" % (name, hint))
                else:
                    bad = st["black"] + st["frozen"]
                    verdict = ("OK" if st["frames"] and bad * 4 < st["frames"]
                               else "BLACK/FROZEN - cannot see some windows"
                               if st["frames"] else "no frames yet")
                    lines.append(
                        "  %-5s : %d frames, %d black, %d frozen -> %s"
                        % (name, st["frames"], st["black"],
                           st["frozen"], verdict))
            return "\n".join(lines)

        def _dx(self):
            with self._dx_lock:
                if self._dx_tried:
                    return self._dx_cam
                self._dx_tried = True

                # dxcam is strictly opt-in. "auto" deliberately means mss
                # (the compatible path that always works - Desktop
                # Duplication can silently deliver black/frozen frames on
                # remote/DRM/hybrid-GPU setups), and "mss" never uses this
                # either. Only an explicit "dxcam" choice, or "wgc" (where
                # dxcam is the fallback behind WGC), reaches the driver.
                if self.prefer not in ("dxcam", "wgc"):
                    return None




                try:
                    import dxcam
                except ImportError:
                    if self._dx_forced:
                        self._note("Capture: dxcam requested but not installed; using mss.")
                    return None

                try:
                    self._dx_cam = dxcam.create(output_color="BGR")
                    if self._dx_cam is None:
                        self._note("Capture: dxcam output busy; using mss.")
                    else:
                        self._note("Capture: dxcam active (fast path); mss kept as fallback.")
                except Exception as exc:
                    self._dx_cam = None
                    self._note("Capture: dxcam unavailable (%r); using mss. "
                               "Normal on Remote Desktop/VMs/old GPU drivers; "
                               "set capture_backend = mss in config.ini to "
                               "skip this message." % (exc,))

                return self._dx_cam

        def _mss(self):
            cur = threading.current_thread()
            with self._mss_lock:
                for tid in list(self._mss_cache):
                    thr, sct = self._mss_cache[tid]
                    if not thr.is_alive():
                        try:
                            sct.close()
                        except Exception:
                            pass
                        del self._mss_cache[tid]
                entry = self._mss_cache.get(cur.ident)
                if entry is None:
                    import mss


                    factory = getattr(mss, "MSS", None) or mss.mss
                    sct = factory()
                    self._mss_cache[cur.ident] = (cur, sct)
                else:
                    sct = entry[1]
            return sct

        def _wgc(self):
            with self._wgc_lock:
                if self._wgc_tried:
                    return self._wgc_state
                self._wgc_tried = True
                if self.prefer != "wgc":
                    return None

                try:
                    from windows_capture import WindowsCapture
                except ImportError as exc:
                    self._note("Capture: wgc requested but the optional "
                               "'windows-capture' package could not be "
                               "imported (%r); falling back to dxcam/mss. "
                               "(pip install windows-capture - it also "
                               "needs numpy and opencv-python.)" % (exc,))
                    return None

                try:
                    state = {"frame": None, "closed": False, "errs": 0}
                    lock = threading.Lock()




                    cap = WindowsCapture(cursor_capture=False,
                                         draw_border=False,
                                         monitor_index=1,
                                         window_name=None)

                    def on_frame_arrived(frame, control):
                        try:
                            arr = None








                            buf = getattr(frame, "frame_buffer", None)
                            if buf is not None:
                                arr = np.ascontiguousarray(
                                    np.asarray(buf)[:, :, :3],
                                    dtype=np.uint8)
                            else:
                                to_bgr = getattr(frame, "to_bgr", None)
                                if to_bgr is not None:




                                    arr = np.ascontiguousarray(
                                        to_bgr(), dtype=np.uint8)
                                else:
                                    to_np = getattr(frame, "to_numpy", None)
                                    if to_np is not None:
                                        arr = np.array(
                                            to_np(), dtype=np.uint8
                                        )[:, :, :3]
                            if arr is None:

                                ptr = getattr(frame, "buffer_pointer", None)
                                fw = int(getattr(frame, "width", 0) or 0)
                                fh = int(getattr(frame, "height", 0) or 0)
                                if ptr and fw and fh:
                                    import ctypes
                                    cbuf = (ctypes.c_uint8
                                            * (fw * fh * 4)).from_address(
                                                int(ptr))
                                    arr = np.array(
                                        np.frombuffer(cbuf, np.uint8
                                                      ).reshape(fh, fw, 4
                                                                )[:, :, :3])
                            if (arr is None or arr.ndim != 3
                                    or arr.shape[2] != 3):
                                raise RuntimeError("frame has no usable "
                                                   "numpy accessor")
                            with lock:


                                state["frame"] = np.ascontiguousarray(
                                    arr, dtype=np.uint8)
                                state["errs"] = 0
                        except Exception:
                            with lock:
                                state["errs"] += 1
                                if state["errs"] >= 30:
                                    state["closed"] = True

                    def on_closed():
                        with lock:
                            state["closed"] = True

                    cap.event(on_frame_arrived)
                    cap.event(on_closed)

                    if hasattr(cap, "start_free_threaded"):
                        cap.start_free_threaded()
                    else:
                        threading.Thread(target=cap.start, daemon=True,
                                         name="wgc-capture").start()

                    deadline = time.perf_counter() + 1.5
                    while time.perf_counter() < deadline:
                        with lock:
                            if state["frame"] is not None:
                                break
                            closed = state["closed"]
                        if closed:
                            break
                        time.sleep(0.02)

                    with lock:
                        first = state["frame"]
                    if first is None:
                        try:
                            stop = getattr(cap, "stop", None)
                            if callable(stop):
                                stop()
                        except Exception:
                            pass
                        try:
                            close = getattr(cap, "close", None)
                            if callable(close):
                                close()
                        except Exception:
                            pass
                        raise RuntimeError("session delivered no frame "
                                           "within 1.5 s")

                    self._wgc_state = (cap, state, lock)
                    self._note("Capture: wgc active "
                               "(Windows.Graphics.Capture, primary "
                               "monitor, frames %dx%d); dxcam/mss kept "
                               "as fallback."
                               % (first.shape[0], first.shape[1]))
                except Exception as exc:
                    self._wgc_state = None
                    self._note("Capture: wgc unavailable (%r); falling "
                               "back to dxcam/mss." % (exc,))
                return self._wgc_state

        def _wgc_grab(self, region):
            st = self._wgc()
            if st is None:
                return None
            _cap, state, lock = st
            with lock:
                frame = state["frame"]
                closed = state["closed"]
            if closed:
                self._note("Capture: wgc session closed; using "
                           "dxcam/mss fallback.")
                return None
            if frame is None:
                return None
            fh, fw = frame.shape[:2]
            # The frame covers the PRIMARY monitor, whose top-left is by
            # definition the virtual-screen origin (0, 0): absolute
            # virtual coordinates map into the frame UNCHANGED. (Shifting
            # by SM_X/YVIRTUALSCREEN here would map secondary-monitor
            # regions ONTO primary-monitor pixels whenever a monitor sits
            # left of or above the primary.) Off-primary regions fail the
            # bounds check and fall back to dxcam/mss.
            l, t, r, b = region
            if l < 0 or t < 0 or r > fw or b > fh or r <= l or b <= t:
                return None
            return np.ascontiguousarray(frame[t:b, l:r])

        def grab(self, left, top, right, bottom):
            region = (int(left), int(top), int(right), int(bottom))

            if self.prefer == "wgc":
                shot = self._wgc_grab(region)
                if shot is not None:
                    self._record("wgc", shot)
                    return shot











            if self._dx_forced or self.prefer == "wgc":
                cam = self._dx()
                if cam is not None:
                    frame = None
                    try:
                        with self._dx_lock:







                            for _ in range(5):
                                frame = cam.grab(region=region)
                                if frame is not None:
                                    break
                                time.sleep(0.001)
                    except Exception as exc:
                        frame = None




                        self._dx_failures += 1
                        if self._dx_failures >= 30:
                            self._note("Capture: dxcam failing repeatedly (%r); "
                                       "switching to mss." % (exc,))
                            with self._dx_lock:
                                self._dx_cam = None
                    else:
                        self._dx_failures = 0

                    if frame is not None:
                        img = np.ascontiguousarray(frame[:, :, :3], dtype=np.uint8)
                        self._record("dxcam", img)
                        return img

            try:
                shot = self._mss().grab({
                    "left": region[0],
                    "top": region[1],
                    "width": region[2] - region[0],
                    "height": region[3] - region[1],
                })
                img = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(
                    shot.height, shot.width, 4
                )[:, :, :3]
                img = np.ascontiguousarray(img)
                self._record("mss", img)
                return img
            except Exception as exc:
                self._note("Capture: mss error %r" % (exc,))
                return None

