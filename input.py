# ---- merged from win32.py ----
import atexit
import numpy as np
from system import IS_WINDOWS, log

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
        if isinstance(vk, (tuple, list)):
            keys = [int(k) for k in vk]
        elif vk is None:
            keys = []
        else:
            keys = [int(vk)]

        def kinput(key, up):
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.u.ki.wVk = key
            inp.u.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
            return inp

        if len(keys) <= 1:
            inputs = []
            for key in seq + keys:
                inputs.append(kinput(key, False))
            for key in reversed(seq + keys):
                inputs.append(kinput(key, True))
            if not inputs:
                return False
            arr = (INPUT * len(inputs))(*inputs)
            try:
                sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
            except Exception:
                return False
            return sent == len(inputs)

        inputs = []
        for key in seq + keys:
            inputs.append(kinput(key, False))
        arr = (INPUT * len(inputs))(*inputs)
        try:
            user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
        except Exception:
            return False
        inputs = []
        for key in reversed(keys):
            inputs.append(kinput(key, True))
        for key in reversed(seq):
            inputs.append(kinput(key, True))
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
                    bad_now = True
                else:
                    bad_now = False
            if bad_now:
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

                if self.prefer not in ("dxcam", "wgc"):
                    return None

                try:
                    import dxcam
                    try:
                        import os
                        from utils import capture_dir
                        _dxcam_data = os.path.join(capture_dir(), "dxcam")
                        if os.path.isdir(_dxcam_data) and _dxcam_data not in dxcam.__path__:
                            dxcam.__path__.append(_dxcam_data)
                    except Exception:
                        pass
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
                self._wgc_state = None
                return None
            if frame is None:
                return None
            fh, fw = frame.shape[:2]
            l, t, r, b = region
            if l < 0 or t < 0 or r > fw or b > fh or r <= l or b <= t:
                self._note("Capture: wgc frame does not cover the "
                           "requested region; using fallback.")
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
                        with self._dx_lock:
                            self._dx_failures += 1
                            if self._dx_failures >= 30:
                                self._note("Capture: dxcam failing repeatedly (%r); "
                                           "switching to mss." % (exc,))
                                self._dx_cam = None
                    else:
                        with self._dx_lock:
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


import ctypes
import threading
import time
from contextlib import contextmanager
from weakref import WeakSet

# ---- devices.py original content follows ----

class _Timeout(Exception):
    pass

# Thread pool for timeout calls to avoid thread leaks
_TIMEOUT_EXECUTOR = None
_TIMEOUT_EXECUTOR_LOCK = threading.Lock()

def _get_executor():
    global _TIMEOUT_EXECUTOR
    with _TIMEOUT_EXECUTOR_LOCK:
        if _TIMEOUT_EXECUTOR is None:
            from concurrent.futures import ThreadPoolExecutor
            _TIMEOUT_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="timeout-")
        return _TIMEOUT_EXECUTOR

def _call_with_timeout(fn, timeout, *args):
    """Call fn(*args) with timeout, using a thread pool to avoid leaks."""
    executor = _get_executor()
    future = executor.submit(fn, *args)
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        # Can't cancel running thread in Python, but future will be garbage collected
        raise _Timeout("did not return within %.0f s" % timeout)

_ACCEL_SAVE = None
_ACCEL_LOCK = threading.Lock()

def _set_mouse_accel(enabled):
    global _ACCEL_SAVE
    with _ACCEL_LOCK:
        try:
            import ctypes as _c
            prev = (_c.c_int * 3)()
            if not _c.windll.user32.SystemParametersInfoA(0x3, 0, prev, 0):
                return None
            if enabled:
                if _ACCEL_SAVE is not None:
                    prev[0] = _ACCEL_SAVE[0]
                    prev[1] = _ACCEL_SAVE[1]
                    prev[2] = _ACCEL_SAVE[2]
                    _ACCEL_SAVE = None
                else:
                    # First enable: remember current state so we can restore it
                    _ACCEL_SAVE = (prev[0], prev[1], prev[2])
                return bool(prev[0])
            else:
                _ACCEL_SAVE = (prev[0], prev[1], prev[2])
                prev[0] = 0
                prev[1] = 0
                prev[2] = 0
            if not _c.windll.user32.SystemParametersInfoA(0x4, 0, prev, 0x2):
                return None
            return True
        except Exception:
            return None


# Nesting counter for per-thread mouse accel disable
_accel_nesting = threading.local()

@contextmanager
def _disable_mouse_accel():
    """Context manager to disable mouse acceleration for the duration of a move.
    Supports nested calls - only re-enables when outermost exits.
    """
    if not hasattr(_accel_nesting, 'count'):
        _accel_nesting.count = 0
    if _accel_nesting.count == 0:
        _set_mouse_accel(False)
    _accel_nesting.count += 1
    try:
        yield
    finally:
        _accel_nesting.count -= 1
        if _accel_nesting.count == 0:
            _set_mouse_accel(True)


_ROLE_NOTED = WeakSet()

def note_mouse_role(backend, index):
    key = (backend, int(index))
    if key in _ROLE_NOTED:
        return
    _ROLE_NOTED.add(key)
    log("%s: mouse%d cannot be sent through this backend; that role's clicks "
        "go through the standard Windows method." % (backend, int(index) + 1))

class Win32Backend(object):

    name = "win32"

    @classmethod
    def available(cls):
        return IS_WINDOWS

    def move(self, x, y):
        user32.SetCursorPos(int(x), int(y))

    def nudge(self, dx, dy):
        import ctypes
        dx, dy = int(round(dx)), int(round(dy))
        if not dx and not dy:
            return True
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.u.mi.dx = dx
        inp.u.mi.dy = dy
        inp.u.mi.dwFlags = MOUSEEVENTF_MOVE
        return bool(user32.SendInput(1, ctypes.byref(inp),
                                             ctypes.sizeof(INPUT)))

    def button(self, down, index=0):
        return bool(inject_mouse(index, down))

    def ok_for_button(self):
        return True

    def note_failure(self, where):
        pass

class InterceptionBackend(object):

    name = "interception"

    _IOCTL_WRITE = 0x222080
    _IOCTL_GET_HWID = 0x222200
    _IOCTL_SET_EVENT = 0x222040
    _GENERIC_READ = 0x80000000
    _OPEN_EXISTING = 3

    _MOUSE_MOVE_RELATIVE = 0x000
    _MOUSE_MOVE_ABSOLUTE = 0x001
    _MOUSE_LEFT_DOWN = 0x001
    _MOUSE_LEFT_UP = 0x002
    _MOUSE_FIRST = 10
    _MOUSE_SLOTS = 20
    # Matches interception-python's MouseStroke format: HHHHIiiI
    # (unit_id:H, flags:H, button_flags:H, button_data:H, _raw_buttons:I, x:i, y:i, information:I)
    _STROKE_FMT = "<HHHHIiiI"
    _STROKE_LEN = 24

    def __init__(self, ctx=None):
        self._lock = threading.Lock()
        self._degraded = False
        self._k32 = None
        self._handles = []
        self._handle = None
        self._dev = None
        self._shift = 0
        self._reason = ""
        self._expected = _ExpectedLedger()
        self._accel_disabled = False
        self._prev_accel = None
        if ctx is not None:
            return
        if not IS_WINDOWS:
            self._degraded = True
            return
        try:
            import ctypes as _c
            self._k32 = _c.windll.kernel32

            self._k32.CreateFileA.restype = _c.c_void_p
            self._k32.CreateFileA.argtypes = [
                _c.c_char_p, _c.c_uint32, _c.c_uint32,
                _c.c_void_p, _c.c_uint32, _c.c_uint32, _c.c_void_p]
        except Exception as exc:
            self._reason = "kernel32 unavailable (%r)" % (exc,)
            self._degraded = True
            return
        try:
            dev = _call_with_timeout(self._open_devices, 8.0)
        except _Timeout as exc:
            self._close_all()
            self._reason = "driver handshake %s" % (exc,)
            log("Input: Interception init timed out (%s); using Windows "
                "injection instead." % (exc,))
            self._degraded = True
            return
        except Exception as exc:
            self._close_all()
            self._reason = repr(exc)
            log("Input: Interception init failed (%r); using Windows "
                "injection instead." % (exc,))
            self._degraded = True
            return
        if dev is None:
            self._close_all()
            if not self._reason:
                self._reason = ("no mouse device answered (is the mouse "
                                "plugged in? install: interception.exe "
                                "install, reboot)")
            log("Input: Interception found no mouse device (%s). Using "
                "Windows injection." % (self._reason,))
            self._degraded = True
            return
        self._dev = dev
        self._handle = self._handles[dev][0]
        # Disable mouse acceleration globally for the session (matching original
        # snappy-detector behavior and ensuring consistent relative movement
        # scaling for aim assist). Restored on close.
        if _set_mouse_accel(False) is True:
            self._prev_accel = True
            self._accel_disabled = True
            log("Input: Interception turned 'Enhance pointer precision' off "
                "for this session (Windows ballistics scale synthetic flicks "
                "unpredictably); it is restored when the app quits.")
        else:
            self._accel_disabled = False
            self._prev_accel = None

    def _open_devices(self):
        import ctypes as _c
        self._handles = [None] * self._MOUSE_SLOTS
        for num in range(self._MOUSE_SLOTS):
            try:
                h = self._k32.CreateFileA(
                    ("\\\\.\\interception%02d" % num).encode(),
                    self._GENERIC_READ, 0, None,
                    self._OPEN_EXISTING, 0, None)
            except Exception:
                continue
            if h is None or h == _c.c_void_p(-1).value:
                continue
            try:
                ev = self._k32.CreateEventA(None, 1, 0, None)
            except Exception:
                ev = None
            if not ev:
                try:
                    self._k32.CloseHandle(h)
                except Exception:
                    pass
                continue
            self._handles[num] = (h, ev)
        for num in range(self._MOUSE_FIRST, self._MOUSE_SLOTS):
            slot = self._handles[num]
            if slot is None:
                continue
            if not self._set_event(*slot):
                continue
            if self._hwid(num) is None:
                continue
            for i, other in enumerate(self._handles):
                if i != num and other is not None:
                    self._close_slot(*other)
                    self._handles[i] = None
            return num
        return None

    def _set_event(self, h, ev):
        import ctypes as _c
        buf = (_c.c_void_p * 2)(ev, 0)
        done = (_c.c_uint32 * 1)(0)
        try:
            ok = self._k32.DeviceIoControl(
                h, self._IOCTL_SET_EVENT, buf, 16, None, 0, done, None)
        except Exception:
            return False
        return bool(ok)

    def _hwid(self, num):
        import ctypes as _c
        slot = self._handles[num]
        if slot is None:
            return None
        buf = (_c.c_byte * 500)()
        done = (_c.c_uint32 * 1)(0)
        try:
            ok = self._k32.DeviceIoControl(
                slot[0], self._IOCTL_GET_HWID,
                None, 0, buf, 500, done, None)
        except Exception:
            return None
        if not ok or not done[0]:
            return None
        try:
            return bytes(buf[:done[0]]).decode("utf-16")
        except Exception:
            return None

    def _close_slot(self, h, ev):
        for handle in (h, ev):
            try:
                if handle:
                    self._k32.CloseHandle(handle)
            except Exception:
                pass

    def _close_all(self):
        for slot in (self._handles or []):
            if slot is not None:
                try:
                    self._close_slot(*slot)
                except Exception:
                    pass
        self._handles = []
        self._handle = None

    def close(self):
        if not self._accel_disabled and not self._handles:
            return  # Already closed, idempotent
        self._close_all()
        if self._accel_disabled:
            _set_mouse_accel(True)
        self._prev_accel = None

    _avail_cache_lock = threading.Lock()
    _avail_cache = None
    _avail_cache_at = 0.0

    @classmethod
    def available(cls):
        if not IS_WINDOWS:
            return False
        _now = time.monotonic()
        with cls._avail_cache_lock:
            cached = cls._avail_cache
            cached_at = cls._avail_cache_at
            if cached is not None and (_now - cached_at) < 30.0:
                return cached
        try:
            ok = bool(_call_with_timeout(cls._probe_slots, 8.0))
        except _Timeout:
            ok = False
        except Exception:
            ok = False
        with cls._avail_cache_lock:
            cls._avail_cache = ok
            cls._avail_cache_at = time.monotonic()
        return ok

    @staticmethod
    def _probe_slots():
        import ctypes as _c
        try:
            k32 = _c.windll.kernel32
            k32.CreateFileA.restype = _c.c_void_p
            k32.CreateFileA.argtypes = [
                _c.c_char_p, _c.c_uint32, _c.c_uint32,
                _c.c_void_p, _c.c_uint32, _c.c_uint32, _c.c_void_p]
        except Exception:
            return False
        for num in range(10, 20):
            try:
                h = k32.CreateFileA(
                    ("\\\\.\\interception%02d" % num).encode(),
                    0x80000000, 0, None, 3, 0, None)
            except Exception:
                continue
            if h is not None and h != _c.c_void_p(-1).value:
                try:
                    k32.CloseHandle(h)
                except Exception:
                    pass
                return True
        return False

    @classmethod
    def _pack(cls, flags, btn, data, x, y):
        import struct as _struct
        # Match interception-python MouseStroke layout:
        # (unit_id:H, flags:H, button_flags:H, button_data:H, _raw_buttons:I, x:i, y:i, information:I)
        return _struct.pack(cls._STROKE_FMT, 0, flags, btn, data, 0,
                            int(x), int(y), 0)

    def _move_stroke(self, dx, dy):
        return self._pack(self._MOUSE_MOVE_RELATIVE, 0, 0, dx, dy)

    def _button_stroke(self, down):
        # Button strokes must use MOUSE_MOVE_ABSOLUTE flag (0x001) per interception spec
        return self._pack(self._MOUSE_MOVE_ABSOLUTE,
                          self._MOUSE_LEFT_DOWN if down
                          else self._MOUSE_LEFT_UP,
                          0, 0, 0)

    def _write(self, payload):
        import ctypes as _c
        buf = (_c.c_ubyte * len(payload))(*payload)
        done = (_c.c_uint32 * 1)(0)
        try:
            ok = self._k32.DeviceIoControl(
                self._handle, self._IOCTL_WRITE,
                buf, len(payload), None, 0, done, None)
        except Exception:
            return -1
        if not ok:
            return -1
        return done[0] // self._STROKE_LEN

    def ok_for_button(self):
        return (not self._degraded) and self._dev is not None

    def note_failure(self, where):
        if not self._degraded:
            self._degraded = True
            log("Input: Interception %s failed; falling back to Windows "
                "injection for the rest of this session." % (where,))

    _STEP = 128
    _MAX_STROKE_TRAVEL = 400

    def move(self, x, y):
        tx, ty = int(x), int(y)
        try:
            cx, cy = get_cursor_pos()
        except Exception:
            user32.SetCursorPos(tx, ty)
            return
        if (cx, cy) == (tx, ty):
            return
        if abs(tx - cx) > self._MAX_STROKE_TRAVEL or \
           abs(ty - cy) > self._MAX_STROKE_TRAVEL:
            user32.SetCursorPos(tx, ty)
            return
        for _ in range(4):
            dx = max(-self._STEP, min(self._STEP, tx - cx))
            dy = max(-self._STEP, min(self._STEP, ty - cy))

            sh = self._shift
            if sh > 0 and dx < 0:
                take = min(sh, -dx)
                dx += take; self._shift -= take
            elif sh < 0 and dx > 0:
                take = min(-sh, dx)
                dx -= take; self._shift += take
            s = self._move_stroke(dx, dy)
            with _disable_mouse_accel():
                self._send(s, "move")
            if self._degraded:
                user32.SetCursorPos(tx, ty)
                return
            time.sleep(0.001)
            try:
                nx, ny = get_cursor_pos()
            except Exception:
                try:
                    user32.SetCursorPos(tx, ty)
                except Exception:
                    pass
                return

            if dx:
                real_dx = nx - cx
                if (real_dx > 0) == (dx > 0) and abs(real_dx) > abs(dx):
                    self._shift = real_dx - dx
            cx, cy = nx, ny
            if (cx, cy) == (tx, ty):
                break
        else:

            try:
                if get_cursor_pos() != (tx, ty):
                    user32.SetCursorPos(tx, ty)
            except Exception:
                pass

    def nudge(self, dx, dy):
        dx, dy = int(round(dx)), int(round(dy))
        if not dx and not dy:
            return True
        with _disable_mouse_accel():
            return self._send(self._move_stroke(dx, dy), "nudge")

    def _expect_now(self, kind):
        try:
            cx, cy = get_cursor_pos()
        except Exception:
            cx = cy = None
        try:
            self._expected.expect(kind, cx, cy)
        except Exception:
            pass

    def consume_expected(self, wparam, x, y):
        try:
            if wparam == 0x0201:
                kind = "down"
            elif wparam == 0x0202:
                kind = "up"
            else:
                return False
            return bool(self._expected.consume(kind, x, y))
        except Exception:
            return False

    def button(self, down, index=0):
        if index:
            note_mouse_role("Driver input", index)
            return False
        if not self.ok_for_button():
            return False
        self._expect_now("down" if down else "up")
        self._send(self._button_stroke(down), "button")
        return self.ok_for_button()

    def send_click(self, index=0):
        import random
        if index:
            note_mouse_role("Driver input", index)
            return False
        if not self.ok_for_button():
            return False
        d = self._button_stroke(True)
        u = self._button_stroke(False)
        self._expect_now("down")
        self._expect_now("up")
        ok = self._send_count(d + u, 2, "click")
        return ok

    def _send_count(self, payload, count, what):
        with self._lock:
            if self._degraded or self._handle is None:
                return False
            if self._write(payload) != count:
                self.note_failure(what)
                return False
            return True

    def _send(self, payload, what):
        with self._lock:
            if self._degraded or self._handle is None:
                return False
            if self._write(payload) != 1:
                self.note_failure(what)
                return False
            return True

class _ExpectedLedger(object):

    def __init__(self, tol=12, ttl=0.3, cap=8):
        self._tol = tol
        self._ttl = ttl
        self._cap = cap
        self._items = []
        self._lock = threading.Lock()

    def expect(self, kind, x=None, y=None):
        with self._lock:
            now = time.perf_counter()
            self._items = [e for e in self._items
                           if now - e[3] <= self._ttl][-self._cap:]
            self._items.append((kind, x, y, now))

    def consume(self, kind, x, y):
        with self._lock:
            now = time.perf_counter()
            live = [e for e in self._items
                    if now - e[3] <= self._ttl]
            self._items[:] = live[-self._cap:]
            for i, (k, ex, ey, _t) in enumerate(self._items):
                if k != kind:
                    continue
                if ex is not None and ey is not None \
                        and (abs(x - ex) > self._tol
                             or abs(y - ey) > self._tol):
                    continue
                del self._items[i]
                return True
            return False

INPUT_BACKEND_CHOICES = ("win32", "driver")

_BACKEND_ALIASES = {
    "win32": "win32",
    "driver": "driver",
    "interception": "driver",
}

PAD_EMU_DEFAULT = ("a=enter;b=esc;x=space;y=tab;up=up;down=down;left=left;"
                   "right=right;lb=mouse4;rb=mouse5;ls=mouse1;rs=mouse2;"
                   "lt=mouse2;rt=mouse1;start=off;back=off")

PAD_EMU_AXIS_STEP = 14.0

_MOUSE_WORDS = {"mouse1": 0, "mouse2": 1, "mouse3": 2, "mouse4": 3, "mouse5": 4}

def parse_emulation(spec):
    out = {}
    raw = str(spec or "").strip()
    if not raw:
        return out
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            log("Controller: pad_emulate entry %r has no '='; ignored." % (pair,))
            continue
        name, value = pair.split("=", 1)
        index = parse_token(name)
        if index is None:
            log("Controller: pad_emulate names no button (%r); ignored." % (name,))
            continue
        value = value.strip().lower()
        if not value or value == "off":
            out[index] = None
            continue
        if value in _MOUSE_WORDS:
            out[index] = ("mouse", _MOUSE_WORDS[value])
            continue
        if value in ("wheelup", "wheeldown"):
            out[index] = ("wheel", 1 if value == "wheelup" else -1)
            continue
        if value in ("wheelleft", "wheelright"):
            out[index] = ("hwheel", -1 if value == "wheelleft" else 1)
            continue
        parsed = _parse_hotkey(value)
        if parsed is None:
            log("Controller: pad_emulate output %r for %s is not a key, a "
                "mouse button or a wheel direction; that button stays off."
                % (value, name))
            out[index] = None
            continue
        out[index] = ("key", parsed[0] & 0x000F, parsed[1])
    return out

class _XUSB_REPORT(ctypes.Structure):

    _fields_ = [("wButtons", ctypes.c_ushort),
                ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short),
                ("sThumbRY", ctypes.c_short)]

_VIGEM_DLL_NAMES = ("ViGEmClient.dll", "ViGEmClient_x64.dll", "vigemclient.dll")

_vigem_dll = None
_vigem_tried = False
_vigem_lock = threading.Lock()

def _load_vigem():
    global _vigem_dll, _vigem_tried
    with _vigem_lock:
        if _vigem_tried:
            return _vigem_dll
        _vigem_tried = True
        if not IS_WINDOWS:
            return None
        from utils import dll_pin_ok, dll_search_paths
        for path in dll_search_paths(_VIGEM_DLL_NAMES, "both"):
            if not dll_pin_ok(path):
                log("ViGEm: %s does not match the .sha256 pin next to it, so "
                    "it was not loaded. Put back the ViGEmClient.dll that "
                    "ships with your ViGEmBus install, or delete the stale "
                    "pin file if you replaced the DLL on purpose." % path)
                continue
            try:
                dll = ctypes.windll.LoadLibrary(path)
            except Exception:
                continue
            try:
                dll.vigem_alloc.restype = ctypes.c_void_p
                dll.vigem_alloc.argtypes = []
                dll.vigem_connect.restype = ctypes.c_ulong
                dll.vigem_connect.argtypes = [ctypes.c_void_p]
                dll.vigem_target_x360_alloc.restype = ctypes.c_void_p
                dll.vigem_target_x360_alloc.argtypes = []
                dll.vigem_target_add.restype = ctypes.c_ulong
                dll.vigem_target_add.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                dll.vigem_target_x360_update.restype = ctypes.c_ulong
                dll.vigem_target_x360_update.argtypes = [
                    ctypes.c_void_p, ctypes.c_void_p, _XUSB_REPORT]
            except Exception:
                continue
            _vigem_dll = dll
            return _vigem_dll
        return None

class ViGemPadBackend(object):

    name = "vigem"

    VIGEM_ERROR_NONE = 0x20000000

    def __init__(self, ctx=None):
        self._lock = threading.Lock()
        self._degraded = False
        self._reason = ""
        self._client = None
        self._target = None
        self._report = _XUSB_REPORT()
        self._dirty = False
        if ctx is not None:
            return
        dll = _load_vigem()
        if dll is None:
            self._reason = ("ViGEmClient.dll was not found next to the "
                            "program or in System32")
            self._degraded = True
            return
        try:
            self._client = dll.vigem_alloc()
            if not self._client:
                raise RuntimeError("vigem_alloc returned NULL")
            error = dll.vigem_connect(self._client)
            if error != self.VIGEM_ERROR_NONE:
                raise RuntimeError("vigem_connect failed (0x%08X) - is the "
                                   "ViGEmBus driver installed?" % (error,))
            self._target = dll.vigem_target_x360_alloc()
            if not self._target:
                raise RuntimeError("vigem_target_x360_alloc returned NULL")
            error = dll.vigem_target_add(self._client, self._target)
            if error != self.VIGEM_ERROR_NONE:
                raise RuntimeError("vigem_target_add failed (0x%08X)" % (error,))
        except Exception as exc:
            self._reason = "%r" % (exc,)
            self._degraded = True
            self.close()
            return
        log("Input: ViGEmBus virtual pad created - controller macros and "
            "recordings are delivered as a real Xbox 360 controller.")

    @classmethod
    def available(cls):
        if not IS_WINDOWS:
            return False
        if _load_vigem() is None:
            return False
        probe = cls()
        ok = probe.ok_for_pad()
        probe.close()
        return ok

    def ok_for_pad(self):
        return (not self._degraded) and self._client is not None \
            and self._target is not None

    def note_failure(self, where):
        if not self._degraded:
            self._degraded = True
            log("Input: ViGEmBus failed at %s%s - controller output falls "
                "back to keyboard/mouse emulation."
                % (where, (" (%s)" % self._reason) if self._reason else ""))

    def button(self, index, down):
        if not self.ok_for_pad():
            return False
        with self._lock:
            try:
                name = PAD_BUTTONS[int(index)]
            except (IndexError, TypeError, ValueError):
                return False
            if name in ("lt", "rt"):
                return True
            mask = _XINPUT_MASKS.get(name)
            if mask is None:
                return False
            if down:
                self._report.wButtons |= mask
            else:
                self._report.wButtons &= ~mask & 0xFFFF
            self._dirty = True
            return True

    def axis(self, index, value):
        if not self.ok_for_pad():
            return False
        try:
            name = PAD_AXES[int(index)]
            value = normalise_axis(int(index), value)
        except (IndexError, TypeError, ValueError):
            return False
        with self._lock:
            if name == "lt":
                self._report.bLeftTrigger = value
            elif name == "rt":
                self._report.bRightTrigger = value
            elif name == "lx":
                self._report.sThumbLX = value
            elif name == "ly":
                self._report.sThumbLY = value
            elif name == "rx":
                self._report.sThumbRX = value
            elif name == "ry":
                self._report.sThumbRY = value
            self._dirty = True
        return True

    def flush(self):
        if not self.ok_for_pad():
            return False
        with self._lock:
            if not self._dirty:
                return True
            self._dirty = False
            report = _XUSB_REPORT()
            ctypes.memmove(ctypes.byref(report), ctypes.byref(self._report),
                           ctypes.sizeof(report))
        dll = _load_vigem()
        try:
            error = dll.vigem_target_x360_update(self._client, self._target,
                                                 report)
        except Exception as exc:
            self._reason = "%r" % (exc,)
            self.note_failure("vigem_target_x360_update")
            return False
        if error != self.VIGEM_ERROR_NONE:
            self._reason = "update returned 0x%08X" % (error,)
            self.note_failure("vigem_target_x360_update")
            return False
        return True

    def release_all(self):
        with self._lock:
            self._report = _XUSB_REPORT()
            self._dirty = True
        return self.flush()

    def close(self):
        dll = _vigem_dll
        with self._lock:
            client, target = self._client, self._target
            self._client = None
            self._target = None
        if dll is None:
            return
        try:
            if target is not None:
                if client is not None and hasattr(dll, "vigem_target_remove"):
                    dll.vigem_target_remove(ctypes.c_void_p(client),
                                            ctypes.c_void_p(target))
                if hasattr(dll, "vigem_target_free"):
                    dll.vigem_target_free(ctypes.c_void_p(target))
            if client is not None:
                if hasattr(dll, "vigem_disconnect"):
                    dll.vigem_disconnect(ctypes.c_void_p(client))
                if hasattr(dll, "vigem_free"):
                    dll.vigem_free(ctypes.c_void_p(client))
        except Exception:
            pass

class EmulatedPadBackend(object):

    name = "emulated"

    def __init__(self, cfg=None, table=None):
        self._lock = threading.Lock()
        self._degraded = not IS_WINDOWS
        self._reason = "" if IS_WINDOWS else "not running on Windows"
        self._held = set()
        if table is not None:
            self.table = dict(table)
        else:
            spec = PAD_EMU_DEFAULT
            if cfg is not None:
                try:
                    spec = str(cfg.get("pad_emulate") or PAD_EMU_DEFAULT)
                except Exception:
                    spec = PAD_EMU_DEFAULT
            self.table = parse_emulation(spec)

    @classmethod
    def available(cls):
        return IS_WINDOWS

    def ok_for_pad(self):
        return not self._degraded

    def note_failure(self, where):
        if not self._degraded:
            self._degraded = True
            self._reason = str(where)
            log("Input: controller emulation failed at %s - controller output "
                "stops until the next start." % (where,))

    def button(self, index, down):
        if not self.ok_for_pad():
            return False
        try:
            index = int(index)
        except (TypeError, ValueError):
            return False
        action = self.table.get(index)
        if action is None:
            return True
        kind = action[0]
        if kind in ("key", "mouse"):
            with self._lock:
                if down:
                    self._held.add(index)
                else:
                    self._held.discard(index)
        try:
            if kind == "key":
                return bool(inject_key(action[1], action[2], bool(down)))
            if kind == "mouse":
                return bool(inject_mouse(action[1], bool(down)))
            if down and kind == "wheel":
                return bool(inject_wheel(action[1]))
            if down and kind == "hwheel":
                return bool(inject_wheel(action[1], horizontal=True))
        except Exception as exc:
            self.note_failure("%s(%r)" % (kind, exc))
            return False
        return True

    def axis(self, index, value):
        if not self.ok_for_pad():
            return False
        try:
            name = PAD_AXES[int(index)]
        except (IndexError, TypeError, ValueError):
            return False
        if name not in ("lx", "ly", "rx", "ry"):
            return True
        try:
            value = int(value)
        except (TypeError, ValueError):
            return False
        zone = DEADZONE_LEFT if name in ("lx", "ly") \
            else DEADZONE_RIGHT
        if name in ("lx", "rx"):
            fx, _fy = apply_deadzone(value, 0, zone)
            fy = 0.0
        else:
            _fx, fy = apply_deadzone(0, -value, zone)
            fx = 0.0
        if abs(fx) < 1e-6 and abs(fy) < 1e-6:
            return True
        try:
            x, y = get_cursor_pos()
            step = PAD_EMU_AXIS_STEP
            return bool(move_cursor(int(round(x + fx * step)),
                                            int(round(y + fy * step))))
        except Exception as exc:
            self.note_failure("axis(%r)" % (exc,))
            return False

    def flush(self):
        return True

    def release_all(self):
        with self._lock:
            held = sorted(self._held)
            self._held.clear()
        for index in held:
            try:
                self.button(index, False)
            except Exception:
                continue
        return True

    def close(self):
        self.release_all()

def make_pad_backend(cfg):
    choice = str((cfg.get("pad_output", "auto") if cfg is not None else "auto")
                 or "auto").strip().lower()
    _EMU = ("emulate", "emulated", "km", "keyboard")
    if choice not in ("auto", "vigem", "vigembus") + _EMU:
        log("Input: pad_output %r is not one of auto, vigem, emulate; using "
            "auto." % (choice,))
        choice = "auto"
    if choice in _EMU:
        return EmulatedPadBackend(cfg)

    if _load_vigem() is None:
        if choice == "vigem":
            log("Input: pad_output = vigem was requested but ViGEmClient.dll "
                "could not be loaded. Put it next to SnappyAI.exe (and "
                "install the ViGEmBus driver); until then controller output "
                "uses keyboard/mouse emulation.")
        return EmulatedPadBackend(cfg)

    be = None
    reason = ""
    try:
        be = ViGemPadBackend()
        if be.ok_for_pad():
            return be
        reason = be._reason
    except Exception as exc:
        reason = "%r" % (exc,)
    if be is not None:
        be.close()
    if choice == "vigem":
        log("Input: pad_output = vigem was requested but no virtual pad could "
            "be created (%s). Install the ViGEmBus driver, then reboot; until "
            "then controller output uses keyboard/mouse emulation."
            % (reason or "unknown reason",))
    return EmulatedPadBackend(cfg)

def make_input_backend(cfg):
    raw = str(cfg.get("input_backend", "win32") or "win32").strip().lower()
    choice = _BACKEND_ALIASES.get(raw)
    if choice is None:
        log("Input: input_backend %r is not one of %s; using the standard "
            "Windows method." % (raw, ", ".join(INPUT_BACKEND_CHOICES)))
        return Win32Backend()
    if choice == "driver":
        try:
            be = InterceptionBackend()
            if be.ok_for_button():
                log("Input: Interception active - clicks are delivered "
                    "at the driver level. If the app still ignores them, "
                    "install the driver: run interception.exe install "
                    "from an admin prompt, then reboot.")
                return be
        except Exception as exc:
            log("Input: Interception unavailable (%r); using the "
                "standard Windows method." % (exc,))
            be = None
        reason = getattr(be, "_reason", "") if be is not None else ""
        if reason:
            why = "startup check failed: %s" % reason
        else:
            why = ("no mouse device answered - confirm the driver is "
                   "installed ('interception.exe install' from an "
                   "admin prompt, then reboot) and the mouse is plugged "
                   "in before Snappy AI starts")
        log("Input: Interception selected but not working (%s). Using "
            "the standard Windows method." % why)
    return Win32Backend()

_VIGEM_STEER = None
_VIGEM_STEER_LOCK = threading.Lock()

_RX_GAIN = 80.0
_RY_GAIN = 150.0

def _get_vigem_steer(cfg=None):
    """Get the ViGEm steering backend.
    Returns ViGemPadBackend if available and allowed by config,
    False if disabled or unavailable.
    """
    global _VIGEM_STEER
    with _VIGEM_STEER_LOCK:

        if cfg is not None:
            choice = str(cfg.get("pad_output", "auto") or "auto").strip().lower()
            _EMU = ("emulate", "emulated", "km", "keyboard")
            if choice in _EMU:
                if _VIGEM_STEER is not False and _VIGEM_STEER is not None:
                    try:
                        _VIGEM_STEER.close()
                    except Exception:
                        pass
                    _VIGEM_STEER = None
                return False
        if _VIGEM_STEER is None:
            try:
                _VIGEM_STEER = ViGemPadBackend()
            except Exception:
                _VIGEM_STEER = False
            if _VIGEM_STEER is not False and not _VIGEM_STEER.ok_for_pad():
                _VIGEM_STEER.close()
                _VIGEM_STEER = False
        return _VIGEM_STEER

def aim_assist_step(dx: float, dy: float, cfg=None) -> bool:
    """Unibot-style continuous aim assist.
    Call every frame with target offset from screen center (dx, dy).
    Returns True if stick updated, False if ViGEm unavailable.
    """
    steer = _get_vigem_steer(cfg)
    if steer is False:
        return False

    try:
        AXIS_FULL = 32767
        if dx == 0.0 and dy == 0.0:
            steer.axis(4, 0)
            steer.axis(5, 0)
            return steer.flush()

        gain_x = _RX_GAIN
        gain_y = _RY_GAIN
        if cfg is not None:
            try:
                sp = float(cfg.get("aim_speed", 1.0))
                ym = float(cfg.get("aim_y_speed_multiplier", 1.0))
                if sp > 0.0:
                    gain_x *= sp
                    gain_y *= sp
                if ym > 0.0:
                    gain_y *= ym
            except (TypeError, ValueError):
                pass

        rx_val = max(-AXIS_FULL, min(AXIS_FULL, int(round(-dx * gain_x))))
        ry_val = max(-AXIS_FULL, min(AXIS_FULL, int(round(-dy * gain_y))))
        ok1 = steer.axis(4, rx_val)
        ok2 = steer.axis(5, ry_val)
        if not (ok1 and ok2):
            return False
        return steer.flush()
    except Exception:
        return False

def aim_assist_center(cfg=None) -> bool:
    """Center the stick (call when target lost)."""
    steer = _get_vigem_steer(cfg)
    if steer is False:
        return False
    try:
        steer.axis(4, 0)
        steer.axis(5, 0)
        return steer.flush()
    except Exception:
        return False


# ---- merged from controls.py ----
import sys
import threading
import time
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any, Callable

TAG = 0x41544C54
METHOD = (
    "synthetic click dispatched through the real input stack and timestamped "
    "at a low-level mouse hook with the performance counter"
)
LIMITS = (
    "Excludes the physical switch debounce and the USB wire/poll time, which no "
    "software can observe. Includes driver, HID class driver, input thread and "
    "hook dispatch. One-way values assume symmetric dispatch and are RTT/2. "
    "Each press is held 50 ms so receiver windows render it; a small "
    "on-screen pad flashes with every synthetic click."
)

@dataclass
class ClickLeg:
    ok: bool = False
    rtts_ms: list[float] = field(default_factory=list)
    error: str = ""

def _qpc_ms() -> float:
    return time.perf_counter() * 1000.0

def _install_hook(on_event: Callable[[int], None]) -> Any:

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt", wintypes.POINT),
            ("mouseData", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    @ctypes.WINFUNCTYPE(
        wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
    )
    def _proc(nCode, wParam, lParam):
        if nCode == 0 and int(wParam) in (WM_LBUTTONDOWN, WM_LBUTTONUP):
            try:
                info = ctypes.cast(
                    lParam, ctypes.POINTER(MSLLHOOKSTRUCT)
                ).contents
                extra = info.dwExtraInfo
                tag = extra if isinstance(extra, int) else None
                if tag == TAG:
                    on_event(int(wParam))
            except Exception:
                pass
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, type(_proc), wintypes.HINSTANCE, wintypes.DWORD
    ]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    hook = user32.SetWindowsHookExW(WH_MOUSE_LL, _proc, None, 0)
    if not hook:
        raise OSError(f"SetWindowsHookEx failed: {ctypes.get_last_error()}")
    return {"user32": user32, "hook": hook, "proc": _proc}

def _remove_hook(handle: Any) -> None:
    try:
        handle["user32"].UnhookWindowsHookEx(handle["hook"])
    except Exception:
        pass

CLICK_HOLD_S = 0.05

_SENDINPUT_SET = False

def _ensure_sendinput_sig(user32):
    global _SENDINPUT_SET
    if not _SENDINPUT_SET:
        user32.SendInput.argtypes = [
            wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int
        ]
        user32.SendInput.restype = wintypes.UINT
        _SENDINPUT_SET = True

def _send_click(user32: Any) -> None:
    import time
    _ensure_sendinput_sig(user32)

    def _one(flag: int) -> None:
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.mi.dwFlags = flag
        inp.mi.dwExtraInfo = ctypes.c_void_p(TAG)
        if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
            raise OSError("SendInput injected nothing")

    _one(MOUSEEVENTF_LEFTDOWN)
    time.sleep(CLICK_HOLD_S)
    _one(MOUSEEVENTF_LEFTUP)

def _click_pad() -> dict[str, Any]:

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    user32.DefWindowProcW.restype = wintypes.LPARAM

    W = 244
    H = 64

    @ctypes.WINFUNCTYPE(
        wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
        wintypes.LPARAM,
    )
    def _wndproc(hwnd: int, msg: int, wp: int, lp: int) -> int:
        try:
            if msg in (WM_LBUTTONDOWN, WM_LBUTTONUP):
                return 0
            return user32.DefWindowProcW(hwnd, msg, wp, lp)
        except Exception:
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("style", wintypes.UINT),
            ("lpfnWndProc", type(_wndproc)),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HCURSOR),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HICON),
        ]

    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClassExW.restype = wintypes.ATOM
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UpdateWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.UnregisterClassW.argtypes = [
        wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.SetLayeredWindowAttributes.argtypes = [
        wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD
    ]
    hinst = kernel32.GetModuleHandleW(None)
    name = "SnappyAIClickPad"
    cls = WNDCLASSEXW()
    cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
    cls.lpfnWndProc = _wndproc
    cls.hInstance = hinst
    cls.lpszClassName = name
    if not user32.RegisterClassExW(ctypes.byref(cls)):
        return {}
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    x = max(0, pt.x - W // 2)
    y = max(0, pt.y - H - 24)
    hwnd = user32.CreateWindowExW(
        0x00080008, name, "input leg monitor (clicks flash here)",
        0x80000000, x, y, W, H, None, None, hinst, None,
    )
    if not hwnd:
        user32.UnregisterClassW(name, hinst)
        return {}
    user32.SetLayeredWindowAttributes(hwnd, 0, 0, 0x00000002)
    user32.ShowWindow(hwnd, 8)
    user32.UpdateWindow(hwnd)

    def close() -> None:
        try:
            user32.DestroyWindow(hwnd)
        except Exception:
            pass
        try:
            user32.UnregisterClassW(name, hinst)
        except Exception:
            pass

    return {"user32": user32, "hwnd": hwnd, "proc": _wndproc,
            "close": close, "size": (W, H)}

def _pump_until(stop: threading.Event, user32: Any, timeout: float) -> None:

    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND,
        wintypes.UINT, wintypes.UINT, wintypes.UINT
    ]
    user32.PeekMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]

    msg = wintypes.MSG()
    deadline = time.monotonic() + timeout
    PM_REMOVE = 0x0001
    while not stop.is_set() and time.monotonic() < deadline:
        if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        else:
            time.sleep(0.01)

def run_click_test(
    count: int = 20, interval: float = 0.15,
    on_progress: Callable[[str], None] | None = None,
) -> ClickLeg:
    if not sys.platform.startswith("win"):
        return ClickLeg(ok=False, error="input hook test needs Windows")
    if count <= 0:
        return ClickLeg(ok=False, error="no samples requested")

    stamps: dict[int, float] = {}
    stamps_lock = threading.Lock()
    stop = threading.Event()
    handle = None

    def _stamp(msg):
        with stamps_lock:
            stamps[msg] = _qpc_ms()

    try:
        handle = _install_hook(_stamp)
    except OSError as exc:
        return ClickLeg(ok=False, error=str(exc))
    user32 = handle["user32"]
    pad = None
    try:
        pad = _click_pad() or None
    except Exception:
        pad = None
    errors = 0
    rtts: list[float] = []

    def worker(tid_box: list) -> None:
        nonlocal errors
        try:
            for i in range(count):
                if stop.is_set():
                    break
                t0 = _qpc_ms()
                try:
                    _send_click(user32)
                except OSError:
                    errors += 1
                    continue
                deadline = time.monotonic() + 2.0
                while True:
                    with stamps_lock:
                        down_present = WM_LBUTTONDOWN in stamps
                    if down_present:
                        break
                    if time.monotonic() >= deadline:
                        break
                    if stop.is_set():
                        break
                    time.sleep(0.0005)
                with stamps_lock:
                    t1 = stamps.pop(WM_LBUTTONDOWN, None)
                    stamps.pop(WM_LBUTTONUP, None)
                if t1 is not None:
                    rtts.append(t1 - t0)
                if on_progress is not None:
                    on_progress(f"input click {i + 1}/{count}")
                stop.wait(interval)
        finally:
            try:
                user32.PostThreadMessageW(tid_box[0], WM_QUIT, 0, 0)
            except Exception:
                stop.set()

    tid_box = [ctypes.windll.kernel32.GetCurrentThreadId()]
    t = threading.Thread(target=worker, args=(tid_box,), daemon=True)
    t.start()
    try:
        _pump_until(stop, user32, timeout=count * (interval + 1.0) + 10.0)
    finally:
        stop.set()
        t.join(timeout=5.0)
        if pad is not None:
            try:
                pad["close"]()
            except Exception:
                pass
        _remove_hook(handle)
    if not rtts:
        detail = f"; {errors} injection failures" if errors else ""
        return ClickLeg(ok=False, error="no hook dispatches observed" + detail)
    return ClickLeg(ok=True, rtts_ms=rtts)

def list_hid_mice() -> list[str]:
    if not sys.platform.startswith("win"):
        return []

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        RIDI_DEVICENAME = 0x20000007

        class RAWINPUTDEVICELIST(ctypes.Structure):
            _fields_ = [("hDevice", wintypes.HANDLE), ("dwType", wintypes.DWORD)]

        user32.GetRawInputDeviceList.argtypes = [
            ctypes.POINTER(RAWINPUTDEVICELIST),
            ctypes.POINTER(wintypes.UINT),
            wintypes.UINT,
        ]
        user32.GetRawInputDeviceList.restype = wintypes.UINT
        n = wintypes.UINT(0)
        size = ctypes.sizeof(RAWINPUTDEVICELIST)
        if user32.GetRawInputDeviceList(None, ctypes.byref(n), size) != 0:
            return []
        arr = (RAWINPUTDEVICELIST * max(1, n.value))()
        if user32.GetRawInputDeviceList(arr, ctypes.byref(n), size) == 0xFFFFFFFF:
            return []
        got = n.value
        names: list[str] = []
        user32.GetRawInputDeviceInfoW.argtypes = [
            wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID,
            ctypes.POINTER(wintypes.UINT),
        ]
        for i in range(min(got, len(arr))):
            if arr[i].dwType != 0:
                continue
            size = wintypes.UINT(0)
            if user32.GetRawInputDeviceInfoW(arr[i].hDevice, RIDI_DEVICENAME,
                                             None, ctypes.byref(size)) != 0:
                continue
            buf = ctypes.create_unicode_buffer(size.value)
            if user32.GetRawInputDeviceInfoW(arr[i].hDevice, RIDI_DEVICENAME,
                                             buf, ctypes.byref(size)) <= 0:
                continue
            names.append(buf.value)
        return names
    except Exception:
        return []

def measure_sleep_overshoot(n: int = 50, sleep_s: float = 0.001) -> dict[str, Any]:
    if n <= 0:
        return {"requested_ms": sleep_s * 1000.0, "over_ms": [], "error": "no samples"}
    over: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        time.sleep(sleep_s)
        over.append((time.perf_counter() - t0) * 1000.0 - sleep_s * 1000.0)
    over.sort()
    return {
        "requested_ms": sleep_s * 1000.0,
        "over_ms": over,
        "p50_over_ms": over[len(over) // 2],
        "max_over_ms": over[-1],
    }

def measure_input(
    count: int = 20, interval: float = 0.15,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    leg = run_click_test(count=count, interval=interval, on_progress=on_progress)
    one_way = sorted(v / 2.0 for v in leg.rtts_ms)
    return {
        "ok": leg.ok,
        "error": leg.error,
        "rtts_ms": sorted(leg.rtts_ms),
        "one_way_ms": one_way,
        "p50_one_way_ms": one_way[len(one_way) // 2] if one_way else None,
        "n": len(one_way),
        "hid_devices": list_hid_mice(),
        "sleep": measure_sleep_overshoot(),
        "method": METHOD,
        "limits": LIMITS,
    }

import re

from system import IS_WINDOWS, log

HOTKEY_MODS = {"ctrl": 0x0002, "alt": 0x0001, "shift": 0x0004, "win": 0x0008}
HOTKEY_NOREPEAT = 0x4000

_NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "esc": 0x1B,
    "escape": 0x1B, "tab": 0x09, "backspace": 0x08, "insert": 0x2D,
    "delete": 0x2E, "home": 0x24, "end": 0x23, "pgup": 0x21, "pgdn": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "comma": 0xBC, "period": 0xBE, "minus": 0xBD, "plus": 0xBB,
}

def _key_to_vk(key):
    if len(key) == 1:
        c = key.upper()
        if "A" <= c <= "Z" or "0" <= c <= "9":
            return ord(c)
        return {" ": 0x20, ",": 0xBC, ".": 0xBE, "-": 0xBD, "+": 0xBB}.get(key)

    if key in _NAMED_KEYS:
        return _NAMED_KEYS[key]

    if key.startswith("f") and key[1:].isdigit():
        n = int(key[1:])
        if 1 <= n <= 24:
            return 0x70 + n - 1

    return None

_MOD_HEAD = re.compile(r"^(ctrl|alt|shift|win)\+(.*)$",
                       re.IGNORECASE | re.DOTALL)

def _parse_hotkey(spec):
    if spec is None:
        return None
    raw = re.sub(r"\s*\+\s*", "+", str(spec).strip())
    if not raw:
        return None
    mods = HOTKEY_NOREPEAT
    seen_mods = set()
    while True:
        head = _MOD_HEAD.match(raw)
        if head is None:
            break
        name = head.group(1).lower()
        if name in seen_mods:
            return None
        seen_mods.add(name)
        mods |= HOTKEY_MODS[name]
        raw = head.group(2)
    key = raw.strip().lower()
    if not key:
        return None
    vk = _key_to_vk(key)
    if vk is None:
        return None
    return mods, vk

MACRO_MAX_TOKENS = 8

_MACRO_SEQ_VKS = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}

def _parse_macro_spec(spec, max_tokens=MACRO_MAX_TOKENS):
    """Parse a macro TRIGGER: held modifiers plus one key.

    The trigger is still a chord - the modifiers must be held while the
    trigger key is pressed.  Action sequences use _parse_macro_sequence.
    """
    if spec is None:
        return None
    raw = re.sub(r"\s*\+\s*", "+", str(spec).strip()).strip().lower()
    if not raw:
        return None
    tokens = raw.split("+")
    if not tokens or len(tokens) > max_tokens:
        return None
    mods = 0
    keys = []
    for tok in tokens:
        if not tok:
            return None
        bit = HOTKEY_MODS.get(tok)
        if bit is not None:
            if mods & bit:
                return None
            mods |= bit
            continue
        vk = _key_to_vk(tok)
        if vk is None or vk in keys:
            return None
        keys.append(vk)
    if not mods and not keys:
        return None

    if len(keys) > 1:
        return None
    return mods, tuple(keys)

def _parse_macro_sequence(spec, max_tokens=MACRO_MAX_TOKENS):
    """Parse a macro ACTION as an ordered sequence of key taps.

    Unlike a hotkey chord, order matters and repeats are allowed:
    "S+M+Shift" means tap S, tap M, tap Shift; "Shift+Ctrl+Shift" taps
    Shift twice.  Every token becomes one VK in the returned tuple.
    """
    if spec is None:
        return None
    raw = re.sub(r"\s*\+\s*", "+", str(spec).strip()).strip().lower()
    if not raw:
        return None
    tokens = raw.split("+")
    if not tokens or len(tokens) > max_tokens:
        return None
    seq = []
    for tok in tokens:
        if not tok:
            return None
        vk = _MACRO_SEQ_VKS.get(tok)
        if vk is None:
            vk = _key_to_vk(tok)
        if vk is None:
            return None
        seq.append(vk)
    return tuple(seq) if seq else None

def _macro_spec_text(mods, key_tokens):
    head = [name for name, _bit in ((n, HOTKEY_MODS[n]) for n in MOD_ORDER)
            if mods & HOTKEY_MODS[name]]
    return "+".join(head + [str(t) for t in key_tokens])

MOD_VKS = {"ctrl": (0x11,), "alt": (0x12,), "shift": (0x10,),
           "win": (0x5B, 0x5C)}
MOD_ORDER = ("ctrl", "alt", "shift", "win")

def parse_bypass_spec(spec):
    parsed = _parse_hotkey(spec)
    if parsed is not None:
        return parsed[0] & ~HOTKEY_NOREPEAT, parsed[1]
    raw = re.sub(r"\s*\+\s*", "+", str(spec or "").strip().lower())
    if not raw:
        return None
    mods = 0
    for part in raw.split("+"):
        bit = HOTKEY_MODS.get(part)
        if bit is None or mods & bit:
            return None
        mods |= bit
    return (mods, None) if mods else None

def describe_bypass_spec(spec):
    parsed = parse_bypass_spec(spec)
    if parsed is None:
        return ""
    mods, vk = parsed
    head = [name for name in MOD_ORDER if mods & HOTKEY_MODS[name]]
    if vk is None:
        return "+".join(head)
    raw = re.sub(r"\s*\+\s*", "+", str(spec).strip().lower())
    while True:
        match = _MOD_HEAD.match(raw)
        if match is None:
            break
        raw = match.group(2)
    return "+".join(head + [raw.strip()])

def bypass_mod_vks(mods):
    out = []
    for name in MOD_ORDER:
        if mods & HOTKEY_MODS[name]:
            out.append(MOD_VKS[name])
    return out

def effective_mode_key(cfg, which):
    raw = cfg.get("key_mode%d" % which, str(which))
    if raw is None or str(raw).strip() == "":
        return str(which) + " (fires everywhere, key still types)"
    specs = [p.strip().lower() for p in str(raw).split(",") if p.strip()]
    if not specs:
        return str(which) + " (fires everywhere, key still types)"
    last = specs[-1]
    if "+" not in last:
        if _parse_hotkey(last) is None:
            return last + " (not a valid shortcut)"
        return last + " (fires everywhere, key still types)"
    return last + " (blocks typing while bound)"

MACRO_SLOTS = 5

REC_PLAY_SLOTS = 5

def build_hotkey_table(cfg):
    specs = {
        10: cfg.get("key_launch", "ctrl+l"),
        11: cfg.get("key_pause", "ctrl+p"),
        30: cfg.get("key_record", "ctrl+alt+r"),
        37: cfg.get("key_record_stop", "ctrl+alt+s"),
        31: cfg.get("key_play", "ctrl+alt+m"),
    }

    for i in range(1, REC_PLAY_SLOTS + 1):
        specs[31 + i] = cfg.get("key_play%d" % i, "ctrl+%d" % i)

    always = []
    local = []

    _specs = {}

    for key_id, spec in specs.items():
        if spec is None or str(spec).strip() == "":
            continue
        parsed = _parse_hotkey(spec)
        if parsed is None:
            log("Key binding %r is invalid; that action has no hotkey."
                % (spec,))
            continue
        mods, vk = parsed
        _specs[key_id] = spec
        if bool(mods & 0x000F):
            always.append((key_id, mods, vk))
        else:
            local.append((key_id, vk))

    for which, ids in MODE_KEY_IDS.items():
        raw = str(cfg.get("key_mode%d" % which, str(which)))
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if len(parts) > MAX_KEYS_PER_MODE:
            log("key_mode%d: more than %d keys given; extra ones ignored."
                % (which, MAX_KEYS_PER_MODE))
            parts = parts[:MAX_KEYS_PER_MODE]
        for spec, key_id in zip(parts, ids):
            parsed = _parse_hotkey(spec)
            if parsed is None:
                log("Mode key %r is invalid; skipped." % (spec,))
                continue
            mods, vk = parsed
            _specs[key_id] = spec
            if bool(mods & 0x000F):
                always.append((key_id, mods, vk))
            else:
                local.append((key_id, vk))

    raw = str(cfg.get("key_assist_off", "") or "")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) > MAX_KEYS_PER_MODE:
        log("key_assist_off: more than %d keys given; extra ones ignored."
            % MAX_KEYS_PER_MODE)
        parts = parts[:MAX_KEYS_PER_MODE]
    for spec, key_id in zip(parts, ASSIST_OFF_KEY_IDS):
        parsed = _parse_hotkey(spec)
        if parsed is None:
            log("Assist-off key %r is invalid; skipped." % (spec,))
            continue
        mods, vk = parsed
        _specs[key_id] = spec
        if bool(mods & 0x000F):
            always.append((key_id, mods, vk))
        else:
            local.append((key_id, vk))

    seen_always = {}
    for key_id, mods, vk in always:
        k = (mods & 0x000F, vk)
        if k in seen_always:
            log("Key binding %r is assigned to two actions (ids %d and %d); "
                "only the first can register."
                % (_specs.get(key_id, "?"), seen_always[k], key_id))
        else:
            seen_always[k] = key_id
    seen_local = {}
    for key_id, vk in local:
        if vk in seen_local:
            log("Key binding %r is assigned to two actions (ids %d and %d); "
                "the first one wins."
                % (_specs.get(key_id, "?"), seen_local[vk], key_id))
        else:
            seen_local[vk] = key_id

    return always, local

def build_macro_table(cfg):
    macros = []
    seen = {}

    for i in range(1, MACRO_SLOTS + 1):
        tspec = str(cfg.get("macro%d_key" % i, "") or "").strip()
        aspec = str(cfg.get("macro%d_action" % i, "") or "").strip()
        if not tspec or not aspec:
            continue

        trig = _parse_macro_spec(tspec)
        act = _parse_macro_sequence(aspec)
        if trig is None or (not trig[1] and not trig[0]):
            log("Macro %d: macro key %r is invalid; macro skipped."
                % (i, tspec))
            continue
        if act is None:
            log("Macro %d: action %r is invalid; macro skipped."
                % (i, aspec))
            continue

        skip = False
        if trig[1]:
            key = (trig[0] & 0x000F, trig[1][0])
            if key in seen:
                log("Macro %d: macro key %r is already used by macro %d; "
                    "skipped." % (i, tspec, seen[key]))
                skip = True
            else:
                seen[key] = i
        else:
            for tmods, tvks, _ in macros:
                if not tvks and tmods == trig[0]:
                    log("Macro %d: mods-only macro key %r already used; "
                        "skipped." % (i, tspec))
                    skip = True
                    break
        if skip:
            continue

        macros.append((trig[0], trig[1], act))

    macro_map = {}
    for m in macros:
        tmods, tvks, _ = m
        macro_map[(tmods & 0x000F, tvks[0] if tvks else None)] = m
    return macros, macro_map

ACTION_IDS = {
    10: "launch",
    11: "pause",

    6: "reload",
    20: "mode1",
    22: "mode1",
    23: "mode1",
    24: "mode1",
    21: "mode2",
    25: "mode2",
    26: "mode2",
    27: "mode2",
    40: "assist_off",
    41: "assist_off",
    42: "assist_off",
    43: "assist_off",
    30: "record_macro",
    37: "stop_macro",
    31: "play_macro",
    32: "play_macro1",
    33: "play_macro2",
    34: "play_macro3",
    35: "play_macro4",
    36: "play_macro5",
}

CONTROL_ACTION_NAMES = ("record_macro", "stop_macro", "play_macro",
                        "play_macro1", "play_macro2", "play_macro3",
                        "play_macro4", "play_macro5")

MODE_KEY_IDS = {1: (20, 22, 23, 24), 2: (21, 25, 26, 27)}
ASSIST_OFF_KEY_IDS = (40, 41, 42, 43)
ASSIST_OFF_PAD_ID = 40
MAX_KEYS_PER_MODE = 4

from system import BudgetComponent, Confidence, Risk, Verdict
from maintenance import manual, reg_set
from maintenance import Finding, Step, Tweak

def _refresh_probe(inv: Any) -> Finding:
    disp = inv.primary_display
    if disp is None:
        return Finding(verdict=Verdict.UNKNOWN, summary="no display device was found")
    if disp.current_refresh_hz is None or disp.max_refresh_hz is None:
        return Finding(
            verdict=Verdict.UNKNOWN, summary="refresh rates could not be read"
        )
    if disp.refresh_deficit_hz > 1.0:
        return Finding(
            verdict=Verdict.PROBLEM,
            summary=(
                f"display runs at {disp.current_refresh_hz:.0f} Hz "
                f"but supports {disp.max_refresh_hz:.0f} Hz"
            ),
            measured={
                "current_hz": disp.current_refresh_hz,
                "max_hz": disp.max_refresh_hz,
            },
        )
    return Finding(
        verdict=Verdict.NO_PROBLEM, summary="display runs at its maximum rate"
    )

def _refresh_plan(inv: Any) -> list[Step]:
    _ = inv
    return [
        manual(
            "Switch the monitor to its highest refresh rate",
            "1. Open Settings > System > Display > Advanced display\n"
            "2. Set the refresh rate dropdown to the highest value offered\n"
            "3. Confirm the change and keep it if the picture stays stable\n"
            "4. Set the same rate inside the game video options",
            notes="Each extra hertz shortens the time a finished frame waits for the panel.",
        )
    ]

def _low_latency_probe(inv: Any) -> Finding:
    gpu = inv.primary_gpu
    if gpu is None:
        return Finding(verdict=Verdict.UNKNOWN, summary="no GPU was found")
    return Finding(
        verdict=Verdict.PROBLEM,
        summary=f"driver frame queueing on {gpu.name} was not confirmed optimal",
        measured={"gpu": gpu.name},
    )

def _low_latency_plan(inv: Any) -> list[Step]:
    _ = inv
    return [
        manual(
            "Enable the lowest-latency frame mode in the GPU driver panel",
            "1. NVIDIA: Control Panel > Manage 3D settings > Low Latency Mode > Ultra\n"
            "2. AMD: Adrenalin > Graphics > Anti-Lag > Enabled\n"
            "3. Intel: Arc Control > Performance > Low Latency > On\n"
            "4. Restart the game and confirm the setting survived the reboot",
            notes="One fewer queued frame removes a full frame time of input lag.",
        )
    ]

def _hags_probe(inv: Any) -> Finding:
    gpu = inv.primary_gpu
    if gpu is None:
        return Finding(verdict=Verdict.UNKNOWN, summary="no GPU was found")
    if gpu.hags is None:
        from system import OSFamily
        if getattr(inv, "os_family", None) is OSFamily.WINDOWS:
            return Finding(
                verdict=Verdict.NOT_APPLICABLE,
                summary=(
                    f"this GPU does not offer hardware-accelerated GPU "
                    f"scheduling ({gpu.name or 'the active adapter'} predates "
                    f"it or the driver does not expose it) - there is nothing "
                    f"to toggle"
                ),
            )
        return Finding(
            verdict=Verdict.UNKNOWN, summary="scheduler state could not be read"
        )
    return Finding(
        verdict=Verdict.NO_PROBLEM,
        summary=f"hardware scheduling is {'on' if gpu.hags else 'off'}; test both states",
    )

def _hags_plan(inv: Any) -> list[Step]:
    _ = inv
    return [
        manual(
            "A/B test hardware-accelerated GPU scheduling",
            "1. Open Settings > System > Display > Graphics > default graphics settings\n"
            "2. Toggle Hardware-accelerated GPU scheduling and reboot\n"
            "3. Measure 200 samples with the live monitor and save a baseline\n"
            "4. Toggle back, measure again, and keep the state with the lower p95",
            notes="The effect varies by hardware; only measurement decides.",
        )
    ]

def _dvr_probe(inv: Any) -> Finding:
    cap = inv.game_dvr_app_capture
    gvr = inv.game_dvr_enabled
    if cap is None and gvr is None:
        return Finding(
            verdict=Verdict.UNKNOWN,
            summary="capture registry values could not be read",
        )
    on = [k for k, v in (("AppCaptureEnabled", cap), ("GameDVR_Enabled", gvr))
          if v not in (None, 0)]
    if on:
        return Finding(
            verdict=Verdict.PROBLEM,
            summary=f"background game recording is on ({', '.join(on)})",
            measured={"AppCaptureEnabled": cap, "GameDVR_Enabled": gvr},
        )
    return Finding(
        verdict=Verdict.NO_PROBLEM, summary="game recording is already off"
    )

def _dvr_plan(inv: Any) -> list[Step]:
    _ = inv
    return [
        reg_set(
            "HKCU",
            r"Software\Microsoft\Windows\CurrentVersion\GameDVR",
            "AppCaptureEnabled",
            0,
            notes="Stops background capture from polling the GPU every frame.",
        ),
        reg_set(
            "HKCU",
            r"System\GameConfigStore",
            "GameDVR_Enabled",
            0,
            notes="Master switch for the recording pipeline.",
        ),
    ]

TWEAKS: tuple[Tweak, ...] = (
    Tweak(
        id="display.refresh_rate.maximum",
        title="Monitor runs below its real refresh rate",
        component=BudgetComponent.RENDER_INPUT,
        mechanism="A finished frame waits for the next panel scan-out before photons change, so a 60 Hz panel adds up to 16.7 ms of present latency where a 165 Hz panel adds only 6 ms for the identical rendered frame.",
        confidence=Confidence.HIGH,
        risk=Risk.LOW,
        summary="Refresh rate below maximum.",
        probe=_refresh_probe,
        plan=_refresh_plan,
        expected_effect="Shorter frame wait and visibly smoother motion.",
    ),
    Tweak(
        id="gpu.vendor.low_latency_mode",
        title="Graphics driver queues frames ahead",
        component=BudgetComponent.RENDER_INPUT,
        mechanism="The driver holds one or more fully rendered frames in a flip queue to smooth over hitches, and each queued frame delays the visible reaction to an input by exactly one frame interval after the game already responded.",
        confidence=Confidence.HIGH,
        risk=Risk.LOW,
        summary="Driver frame queueing not optimal.",
        probe=_low_latency_probe,
        plan=_low_latency_plan,
        expected_effect="Input appears on screen up to one frame sooner.",
    ),
    Tweak(
        id="gpu.hags.ab_test",
        title="Hardware GPU scheduling worth testing",
        component=BudgetComponent.RENDER_INPUT,
        mechanism="Hardware scheduling moves flip-queue decisions from the operating system scheduler into dedicated GPU logic, which removes a scheduling hop on some configurations but adds contention overhead on multi-adapter and hybrid graphics machines.",
        confidence=Confidence.MEDIUM,
        risk=Risk.MEDIUM,
        summary="Scheduler effect unmeasured.",
        probe=_hags_probe,
        plan=_hags_plan,
        expected_effect="Hardware-dependent; measurement decides which state to keep.",
        caveats="Effect varies by hardware and driver; keep only the measured winner.",
    ),
    Tweak(
        id="capture.game_dvr.disable",
        title="Background recording steals GPU time",
        component=BudgetComponent.RENDER_INPUT,
        mechanism="The capture pipeline hooks every presented frame to encode a background recording, consuming GPU encode bandwidth and adding a compositing pass even on days when nothing is ever saved or uploaded.",
        confidence=Confidence.MEDIUM,
        risk=Risk.LOW,
        summary="Game DVR capture enabled.",
        probe=_dvr_probe,
        plan=_dvr_plan,
        expected_effect="Frees GPU time previously spent on unobserved recording.",
    ),
)

@dataclass
class Theme:
    bg: str = "#14171a"
    raised: str = "#1b1f24"
    inset: str = "#0f1215"
    fg: str = "#e6e6e6"
    muted: str = "#9aa4af"
    accent: str = "#4c9aff"
    good: str = "#3fb950"
    warn: str = "#d29922"
    bad: str = "#f85149"
    border: str = "#2c3238"
    entry_bg: str = "#0f1215"
    entry_fg: str = "#e6e6e6"
    font: tuple = ("Segoe UI", 10)
    font_bold: tuple = ("Segoe UI", 10, "bold")
    font_mono: tuple = ("Consolas", 9)
    font_h1: tuple = ("Segoe UI Semibold", 20)
    font_h2: tuple = ("Segoe UI Semibold", 12)
    font_big: tuple = ("Segoe UI Semibold", 30)
    font_entry: tuple = ("Segoe UI", 10)

DEFAULT_THEME = Theme()

ButtonFactory = Callable[..., Any]

import os


"""Native controller input: XInput, the button vocabulary, and the spec grammar.

WHY THIS MODULE EXISTS
----------------------
`assist.py` needs to answer three questions about a gamepad and nothing else:
what is the pad doing right now, which of those states is a *new* event, and
what does a user-written binding such as "rb+a" mean. All three live here so
the engine stays free of ctypes and so the grammar can be unit-tested on any
platform (no Windows, no pad, no driver).

XInput is read-only. It reports a pad; it cannot make one. Output therefore
lives in `input_backend.py` (ViGEmBus when the user has it, keyboard/mouse
emulation otherwise) exactly as every other delivery path in this project does.

Design rules (mirrors colorspec.py / input_backend.py):
- pure stdlib ctypes; imports cleanly on any OS, so the GUI can read
  available() to decorate its switch and the tests can run head-less;
- NEVER fatal: no DLL, no pad, a pad unplugged mid-session or a wedged driver
  all degrade to "not connected" plus at most one log line;
- no polling here. `read()` is one call; the caller owns the cadence.

BUTTON AND AXIS IDS
-------------------
`PAD_BUTTONS` and `PAD_AXES` are the stable numeric ids stored in recordings,
so they are append-only: never reorder them, never reuse an index, or every
saved macro library silently changes meaning.

SPEC GRAMMAR
------------
    ""                  -> nothing bound
    "a"                 -> one button
    "rb+a"              -> a combination (order is irrelevant)
    "lt"                -> an analogue trigger, pressed past its threshold
    "LS+RS"             -> case is irrelevant
Accepted names and aliases are in `ALIASES`.
"""

import ctypes
import math

XUSER_MAX_COUNT = 4

PAD_BUTTONS = ("up", "down", "left", "right", "start", "back", "ls", "rs",
               "lb", "rb", "a", "b", "x", "y", "lt", "rt")

PAD_AXES = ("lt", "rt", "lx", "ly", "rx", "ry")

_XINPUT_MASKS = {
    "up": 0x0001, "down": 0x0002, "left": 0x0004, "right": 0x0008,
    "start": 0x0010, "back": 0x0020, "ls": 0x0040, "rs": 0x0080,
    "lb": 0x0100, "rb": 0x0200, "a": 0x1000, "b": 0x2000,
    "x": 0x4000, "y": 0x8000,
}

ALIASES = {
    "dpad_up": "up", "dpad_down": "down", "dpad_left": "left",
    "dpad_right": "right",
    "l3": "ls", "r3": "rs", "left_thumb": "ls", "right_thumb": "rs",
    "left_stick": "ls", "right_stick": "rs",
    "l1": "lb", "r1": "rb", "left_shoulder": "lb", "right_shoulder": "rb",
    "bumper_l": "lb", "bumper_r": "rb",
    "select": "back", "view": "back", "menu": "start",
    "lt": "lt", "rt": "rt", "left_trigger": "lt", "right_trigger": "rt",
    "trigger_l": "lt", "trigger_r": "rt",
    "a": "a", "b": "b", "x": "x", "y": "y",
    "cross": "a", "circle": "b", "square": "x", "triangle": "y",
}

DEADZONE_LEFT = 7849
DEADZONE_RIGHT = 8689
DEADZONE_TRIGGER = 30

AXIS_FULL = 32767
TRIGGER_FULL = 255

DEFAULT_TRIGGER_THRESHOLD = 128

ERROR_SUCCESS = 0
ERROR_DEVICE_NOT_CONNECTED = 1167
ERROR_EMPTY = 4306

_DLL_NAMES = ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll")

_dll = None
_dll_tried = False
_dll_lock = threading.Lock()

class _Gamepad(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort),
                ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short),
                ("sThumbRY", ctypes.c_short)]

class _State(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_ulong),
                ("Gamepad", _Gamepad)]

class _Capabilities(ctypes.Structure):
    _fields_ = [("Type", ctypes.c_ubyte),
                ("SubType", ctypes.c_ubyte),
                ("Flags", ctypes.c_ushort),
                ("Gamepad", _Gamepad),
                ("Vibration", ctypes.c_ushort * 2)]

class _Vibration(ctypes.Structure):
    _fields_ = [("wLeftMotorSpeed", ctypes.c_ushort),
                ("wRightMotorSpeed", ctypes.c_ushort)]

def _load_dll():
    global _dll, _dll_tried
    with _dll_lock:
        if _dll_tried:
            return _dll
        _dll_tried = True
        if not IS_WINDOWS:
            return None
        from utils import dll_search_paths
        candidates = dll_search_paths(_DLL_NAMES, "both")
        for path in candidates:
            try:
                dll = ctypes.windll.LoadLibrary(path)
            except Exception:
                continue
            try:
                dll.XInputGetState.restype = ctypes.c_ulong
                dll.XInputGetState.argtypes = [ctypes.c_ulong,
                                               ctypes.POINTER(_State)]
                dll.XInputSetState.restype = ctypes.c_ulong
                dll.XInputSetState.argtypes = [ctypes.c_ulong,
                                               ctypes.POINTER(_Vibration)]
                if hasattr(dll, "XInputGetCapabilities"):
                    dll.XInputGetCapabilities.restype = ctypes.c_ulong
                    dll.XInputGetCapabilities.argtypes = [
                        ctypes.c_ulong, ctypes.c_ulong,
                        ctypes.POINTER(_Capabilities)]
            except Exception:
                continue
            _dll = dll
            return _dll
        log("Controller: none of %s could be found in System32 or next to the "
            "program, so no controller can be read on this machine. XInput 1.4 "
            "ships with Windows 8 and later; on Windows 7 install the DirectX "
            "end-user runtime. Copies anywhere else are ignored on purpose - a "
            "DLL in the folder we were launched from, or one on PATH, would be "
            "mapped into this process and run with its rights. The rest of "
            "Snappy AI is unaffected." % ", ".join(_DLL_NAMES))
        return None

def available():
    return _load_dll() is not None

class PadState(object):

    __slots__ = ("connected", "packet", "buttons", "lt", "rt",
                 "lx", "ly", "rx", "ry", "when")

    def __init__(self, connected=False, packet=0, buttons=0, lt=0, rt=0,
                 lx=0, ly=0, rx=0, ry=0, when=0.0):
        self.connected = connected
        self.packet = packet
        self.buttons = buttons
        self.lt = lt
        self.rt = rt
        self.lx = lx
        self.ly = ly
        self.rx = rx
        self.ry = ry
        self.when = when

    def down(self, index):
        return bool(self.buttons & (1 << index))

    def axis(self, index):
        name = PAD_AXES[index]
        return getattr(self, name)

    def as_dict(self):
        return {"buttons": self.buttons, "lt": self.lt, "rt": self.rt,
                "lx": self.lx, "ly": self.ly, "rx": self.rx, "ry": self.ry}

    def __repr__(self):
        return ("<PadState connected=%s packet=%d buttons=%s lt=%d rt=%d "
                "l=(%d,%d) r=(%d,%d)>"
                % (self.connected, self.packet, format_buttons(self.buttons),
                   self.lt, self.rt, self.lx, self.ly, self.rx, self.ry))

EMPTY_STATE = PadState()

_LT_IDX = PAD_BUTTONS.index("lt")
_RT_IDX = PAD_BUTTONS.index("rt")

def _threshold_triggers(lt, rt, threshold):
    mask = 0
    if lt >= threshold:
        mask |= 1 << _LT_IDX
    if rt >= threshold:
        mask |= 1 << _RT_IDX
    return mask

def read(index=0, trigger_threshold=DEFAULT_TRIGGER_THRESHOLD, reader=None):
    if reader is not None:
        try:
            state = reader(index)
        except Exception as exc:
            log("Controller: the injected reader failed (%r)." % (exc,))
            return EMPTY_STATE
        if state is None:
            return EMPTY_STATE
        if not isinstance(state, PadState):
            return EMPTY_STATE
        state.buttons = (state.buttons & ~(
            (1 << PAD_BUTTONS.index("lt")) | (1 << PAD_BUTTONS.index("rt")))) \
            | _threshold_triggers(state.lt, state.rt, trigger_threshold)
        return state
    dll = _load_dll()
    if dll is None:
        return EMPTY_STATE
    if not 0 <= int(index) < XUSER_MAX_COUNT:
        return EMPTY_STATE
    raw = _State()
    try:
        result = dll.XInputGetState(ctypes.c_ulong(int(index)),
                                    ctypes.byref(raw))
    except (OSError, ValueError, ctypes.ArgumentError) as exc:
        log("Controller: XInputGetState failed (%r)." % (exc,))
        return EMPTY_STATE
    if result == ERROR_DEVICE_NOT_CONNECTED or result == ERROR_EMPTY:
        return EMPTY_STATE
    if result != ERROR_SUCCESS:
        return EMPTY_STATE
    pad = raw.Gamepad
    # XInput puts A/B/X/Y at bits 12-15, while PAD_BUTTONS indexes them at
    # 10-13. Translate by name instead of reusing the raw mask.
    raw_buttons = int(pad.wButtons)
    buttons = 0
    for name, mask in _XINPUT_MASKS.items():
        if raw_buttons & mask:
            buttons |= 1 << PAD_BUTTONS.index(name)
    lt = int(pad.bLeftTrigger)
    rt = int(pad.bRightTrigger)
    return PadState(connected=True, packet=int(raw.dwPacketNumber),
                    buttons=buttons | _threshold_triggers(lt, rt,
                                                          trigger_threshold),
                    lt=lt, rt=rt,
                    lx=int(pad.sThumbLX), ly=int(pad.sThumbLY),
                    rx=int(pad.sThumbRX), ry=int(pad.sThumbRY),
                    when=time.perf_counter())

def capabilities(index=0):
    dll = _load_dll()
    if dll is None or not hasattr(dll, "XInputGetCapabilities"):
        return None
    caps = _Capabilities()
    try:
        result = dll.XInputGetCapabilities(ctypes.c_ulong(int(index)),
                                           ctypes.c_ulong(0),
                                           ctypes.byref(caps))
    except (OSError, ValueError, ctypes.ArgumentError):
        return None
    if result != ERROR_SUCCESS:
        return None
    return int(caps.Type), int(caps.SubType), int(caps.Flags)

def set_rumble(index, left=0.0, right=0.0, seconds=0.0):
    dll = _load_dll()
    if dll is None or seconds <= 0:
        return False
    vib = _Vibration()
    vib.wLeftMotorSpeed = int(max(0.0, min(1.0, float(left))) * 65535)
    vib.wRightMotorSpeed = int(max(0.0, min(1.0, float(right))) * 65535)
    try:
        if dll.XInputSetState(ctypes.c_ulong(int(index)), ctypes.byref(vib)):
            return False
        time.sleep(max(0.0, float(seconds)))
        vib.wLeftMotorSpeed = 0
        vib.wRightMotorSpeed = 0
        dll.XInputSetState(ctypes.c_ulong(int(index)), ctypes.byref(vib))
        return True
    except (OSError, ValueError, ctypes.ArgumentError):
        return False

def apply_deadzone(x, y, deadzone, scale=1.0):
    try:
        x = float(x)
        y = float(y)
        dz = max(0.0, min(float(deadzone), AXIS_FULL - 1.0))
    except (TypeError, ValueError):
        return 0.0, 0.0
    mag = math.hypot(x, y)
    if mag <= dz or mag <= 0.0:
        return 0.0, 0.0
    normalised = (mag - dz) / (AXIS_FULL - dz)
    k = min(1.0, normalised) / mag * float(scale)
    return x * k, y * k

def normalise_axis(index, value):
    name = PAD_AXES[index]
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 0
    if name in ("lt", "rt"):
        return max(0, min(TRIGGER_FULL, v))
    return max(-AXIS_FULL - 1, min(AXIS_FULL, v))

def parse_token(token):
    name = str(token or "").strip().lower()
    if not name:
        return None
    name = ALIASES.get(name, name)
    if name in PAD_BUTTONS:
        return PAD_BUTTONS.index(name)
    return None

def parse_spec(spec):
    if spec is None:
        return None
    raw = str(spec).strip()
    if not raw:
        return None
    indices = set()
    for token in raw.split("+"):
        index = parse_token(token)
        if index is None:
            return None
        if index in indices:
            return None
        indices.add(index)
    if not indices:
        return None
    return frozenset(indices)

def spec_mask(spec):
    indices = parse_spec(spec)
    if not indices:
        return 0
    mask = 0
    for index in indices:
        mask |= 1 << index
    return mask

def format_buttons(buttons):
    if not buttons:
        return ""
    names = [PAD_BUTTONS[i] for i in range(len(PAD_BUTTONS))
             if buttons & (1 << i)]
    return "+".join(names)

def describe_spec(spec):
    indices = parse_spec(spec)
    if indices is None:
        return ""
    mask = 0
    for index in indices:
        mask |= 1 << index
    return format_buttons(mask)

class PadTracker(object):

    def __init__(self, indices):
        self.indices = frozenset(indices or ())
        self._mask = 0
        for index in self.indices:
            self._mask |= 1 << index
        self.reset()

    def reset(self):
        self.down = False

    def matches(self, buttons):
        return bool(self._mask) and (buttons & self._mask) == self._mask

    def update(self, buttons, now=None):
        if not self.matches(buttons):
            self.down = False
            return None
        if self.down:
            return None
        self.down = True
        return "press"

def shadowed_indices(bindings, buttons):
    matching = [b for b in bindings if b.indices and b.tracker.matches(buttons)]
    out = set()
    for short in matching:
        for long in matching:
            if long is short:
                continue
            if short.indices < long.indices:
                out.add(id(short))
                break
    return out

class PadBinding(object):

    __slots__ = ("key", "spec", "indices", "tracker", "action")

    def __init__(self, key, spec, action):
        self.key = key
        self.spec = spec
        self.indices = parse_spec(spec)
        self.action = action
        self.tracker = PadTracker(self.indices or ())

    @property
    def valid(self):
        return self.indices is not None

def build_bindings(table):
    out = []
    seen = {}
    for key in table:
        spec, action = table[key]
        indices = parse_spec(spec)
        if indices is None:
            if str(spec or "").strip():
                log("Controller binding %r is invalid (%r); that action has "
                    "no controller button." % (key, spec))
            continue
        if indices in seen:
            log("Controller binding %r is assigned to two actions (%s and "
                "%s); only the first can fire."
                % (format_buttons(_mask_of(indices)), seen[indices], key))
            continue
        seen[indices] = key
        out.append(PadBinding(key, spec, action))
    return out

def _mask_of(indices):
    mask = 0
    for index in indices:
        mask |= 1 << index
    return mask

ACTION_KEYS = (
    ("pad_launch", "launch"),
    ("pad_pause", "pause"),
    ("pad_mode1", "mode1"),
    ("pad_mode2", "mode2"),
    ("pad_record", "record_macro"),
    ("pad_record_stop", "stop_macro"),
    ("pad_play", "play_macro"),
    ("pad_play1", "play_macro1"),
    ("pad_play2", "play_macro2"),
    ("pad_play3", "play_macro3"),
    ("pad_play4", "play_macro4"),
    ("pad_play5", "play_macro5"),
)

def action_ids():
    out = {}
    for hid, name in ACTION_IDS.items():
        out.setdefault(name, hid)
    return out

def build_action_bindings(cfg):
    ids = action_ids()
    table = {}
    for key, name in ACTION_KEYS:
        if name not in ids:
            continue
        table[key] = (str(cfg.get(key, "") or ""), ids[name])

    raw = str(cfg.get("pad_assist_off", "") or "")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) > MAX_KEYS_PER_MODE:
        log("pad_assist_off: more than %d buttons given; extra ones ignored."
            % MAX_KEYS_PER_MODE)
        parts = parts[:MAX_KEYS_PER_MODE]
    for i, spec in enumerate(parts, 1):
        table["pad_assist_off%d" % i] = (spec, ASSIST_OFF_PAD_ID)
    return build_bindings(table)

def parse_macro_action(spec):
    raw = str(spec or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("pad:"):
        mask = spec_mask(raw[4:])
        if not mask:
            return None
        return ("pad", mask)
    seq = _parse_macro_sequence(raw)
    if seq is None:
        return None
    return ("key", seq)

def build_pad_macro_table(cfg):
    macros = []
    seen = {}
    for slot in range(1, MACRO_SLOTS + 1):
        tspec = str(cfg.get("pad_macro%d_key" % slot, "") or "").strip()
        aspec = str(cfg.get("pad_macro%d_action" % slot, "") or "").strip()
        if not tspec or not aspec:
            continue
        indices = parse_spec(tspec)
        if indices is None:
            log("Pad macro %d: trigger %r is not a controller button or "
                "combination; macro skipped." % (slot, tspec))
            continue
        action = parse_macro_action(aspec)
        if action is None:
            log("Pad macro %d: action %r is neither a key combo (ctrl+c) nor "
                "a controller combo (pad:a+b); macro skipped." % (slot, aspec))
            continue
        if indices in seen:
            log("Pad macro %d: trigger %r is already used by pad macro %d; "
                "skipped." % (slot, tspec, seen[indices]))
            continue
        seen[indices] = slot
        macros.append((slot, tspec, indices, action))
    return macros

def first_connected(index=0, reader=None, count=XUSER_MAX_COUNT):
    if reader is None and not available():
        return None
    if index is not None and int(index) >= 0:
        state = read(int(index), reader=reader)
        return int(index) if state.connected else None
    for candidate in range(max(1, int(count))):
        if read(candidate, reader=reader).connected:
            return candidate
    return None

def describe(index=None, reader=None):
    if reader is None and not available():
        return ("Controller: no XInput runtime on this machine, so controller "
                "support is unavailable.")
    lines = ["Controller: XInput present."]
    found = 0
    for candidate in range(XUSER_MAX_COUNT):
        state = read(candidate, reader=reader)
        if not state.connected:
            continue
        found += 1
        caps = capabilities(candidate) if reader is None else None
        kind = ""
        if caps:
            kind = " (type %d, subtype %d)" % (caps[0], caps[1])
        lines.append("  pad %d: connected%s, packet %d, buttons %s, "
                     "lt=%d rt=%d, L=(%d,%d) R=(%d,%d)"
                     % (candidate, kind, state.packet,
                        format_buttons(state.buttons) or "none",
                        state.lt, state.rt, state.lx, state.ly,
                        state.rx, state.ry))
    if not found:
        lines.append("  no pad is connected right now.")
    return "\n".join(lines)

import json

import system as i18n
i18n.publish("en", {
    "rec.no_sel": "Nothing selected to export.",
    "rec.exported": "Exported to %s",
    "rec.export_fail": "Export failed: %r",
    "rec.import_fail": "Import failed: %r",
    "rec.notfile": "Import failed: not a recording file.",
    "rec.imported": "Imported %d recording(s)%s.",
    "rec.imported_none": " (none were valid)",
    "rec.bad_version": "Import failed: file format version %s "
                        "(this build reads version %d).",
})

REC_FILE = "macros.json"
PAD_REC_FILE = "pad_macros.json"

STEP_KEY = 0
STEP_MOVE = 1
STEP_BUTTON = 2
STEP_PAD_BUTTON = 3
STEP_PAD_AXIS = 4
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0, 1, 2
BTN_X1, BTN_X2 = 3, 4
BTN_ALL = (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_X1, BTN_X2)
PHASE_DOWN, PHASE_UP = 0, 1

MAX_STEPS = 2000
MAX_RECORDINGS = 5
MAX_DELAY_MS = 5000
MAX_NAME_LEN = 60
MAX_COORD = 65536
FORMAT_VERSION = 3

def pad_axis_range(index):
    name = PAD_AXES[index]
    if name in ("lt", "rt"):
        return 0, TRIGGER_FULL
    return -AXIS_FULL - 1, AXIS_FULL

def validate_steps(raw):
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    if len(raw) > MAX_STEPS:
        raw = list(raw)[:MAX_STEPS]
    out = []
    for step in raw:
        if not isinstance(step, (list, tuple)):
            return None
        vals = []
        for v in step:
            if isinstance(v, bool) or not isinstance(v, int):
                return None
            vals.append(v)
        n = len(vals)
        if n == 3:
            tag, mods, vk, delay = STEP_KEY, vals[0], vals[1], vals[2]
        elif n == 4:
            tag = vals[0]
            if tag == STEP_KEY:
                mods, vk, delay = vals[1], vals[2], vals[3]
            elif tag == STEP_MOVE:
                x, y, delay = vals[1], vals[2], vals[3]
                if not -MAX_COORD <= x <= MAX_COORD \
                        or not -MAX_COORD <= y <= MAX_COORD:
                    return None
                out.append((STEP_MOVE, x, y,
                            max(0, min(MAX_DELAY_MS, delay))))
                continue
            elif tag == STEP_PAD_BUTTON:
                btn, phase, delay = vals[1], vals[2], vals[3]
                if not 0 <= btn < len(PAD_BUTTONS):
                    return None
                if phase not in (PHASE_DOWN, PHASE_UP):
                    return None
                out.append((STEP_PAD_BUTTON, btn, phase,
                            max(0, min(MAX_DELAY_MS, delay))))
                continue
            elif tag == STEP_PAD_AXIS:
                axis, value, delay = vals[1], vals[2], vals[3]
                if not 0 <= axis < len(PAD_AXES):
                    return None
                low, high = pad_axis_range(axis)
                if not low <= value <= high:
                    return None
                out.append((STEP_PAD_AXIS, axis, value,
                            max(0, min(MAX_DELAY_MS, delay))))
                continue
            else:
                return None
        elif n == 6 and vals[0] == STEP_BUTTON:
            btn, phase, x, y, delay = vals[1], vals[2], vals[3], \
                vals[4], vals[5]
            if btn not in BTN_ALL:
                return None
            if phase not in (PHASE_DOWN, PHASE_UP):
                return None
            if not -MAX_COORD <= x <= MAX_COORD \
                    or not -MAX_COORD <= y <= MAX_COORD:
                return None
            out.append((STEP_BUTTON, btn, phase, x, y,
                        max(0, min(MAX_DELAY_MS, delay))))
            continue
        else:
            return None

        if not 0 <= mods <= 0x000F or not 1 <= vk <= 254:
            return None
        out.append((STEP_KEY, mods, vk, max(0, min(MAX_DELAY_MS, delay))))
    return out

def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if value is None:
        return default
    return bool(value)

def _clean_rec(data, fallback_name):
    if not isinstance(data, dict):
        return None
    steps = validate_steps(data.get("steps"))
    if not steps:
        return None
    name = str(data.get("name") or fallback_name).strip()[:MAX_NAME_LEN] \
        or fallback_name
    loop = _as_bool(data.get("loop"), False)
    try:
        created = int(data.get("created", 0))
    except (TypeError, ValueError):
        created = 0
    return {"name": name, "steps": steps, "loop": loop,
            "created": created, "id": None}

class RecordingStore(object):

    def __init__(self, path=None):
        if path is None:
            from utils import recordings_dir
            path = os.path.join(recordings_dir(), REC_FILE)
        self.path = path
        self.recordings = []
        self.revision = 0
        self._next_id = 1
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        items = data.get("recordings") if isinstance(data, dict) else None
        if not isinstance(items, list):
            if data:
                log("macros.json: unreadable library - starting empty.")
            return
        for i, item in enumerate(items):
            if len(self.recordings) >= MAX_RECORDINGS:
                log("macros.json: library cap is %d - ignored %d extra "
                    "recording(s)." % (MAX_RECORDINGS, len(items) - i))
                break
            rec = _clean_rec(item, "Recording %d" % (i + 1))
            if rec is None:
                log("macros.json: ignored an invalid recording entry.")
                continue
            self._add_clean(rec)

    def save(self):
        try:
            folder = os.path.dirname(self.path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"recordings": [
                    {"id": r["id"], "name": r["name"], "loop": r["loop"],
                     "created": r["created"],
                     "steps": [list(s) for s in r["steps"]]}
                    for r in self.recordings]}, fh, indent=2)
            os.replace(tmp, self.path)
            self.revision += 1
            return True
        except (OSError, TypeError, ValueError) as exc:
            log("macros.json: could not save (%r)" % (exc,))
            return False

    def _load_items(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        items = data.get("recordings") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        out = []
        for i, item in enumerate(items):
            if len(out) >= MAX_RECORDINGS:
                log("macros.json: library cap is %d - ignored %d extra "
                    "recording(s)." % (MAX_RECORDINGS, len(items) - i))
                break
            rec = _clean_rec(item, "Recording %d" % (i + 1))
            if rec is None:
                log("macros.json: ignored an invalid recording entry.")
                continue
            out.append(rec)
        return out

    def reload_from_disk(self):
        fresh = self._load_items()
        if fresh is None:
            return False
        nxt = 1
        for rec in fresh:
            rec["id"] = nxt
            nxt += 1
        self.recordings[:] = fresh
        self._next_id = nxt
        self.revision += 1
        return True

    def _add_clean(self, rec):
        rec["id"] = self._next_id
        self._next_id += 1
        self.recordings.append(rec)
        return rec

    def add(self, name, steps, loop=False):
        if len(self.recordings) >= MAX_RECORDINGS:
            log("Recording library full (%d) - remove one first."
                % MAX_RECORDINGS)
            return None
        clean_name = str(name or "").strip()[:MAX_NAME_LEN]
        if not clean_name:
            return None
        rec = _clean_rec({"name": clean_name, "steps": steps, "loop": loop,
                          "created": int(time.time())}, clean_name)
        if rec is None:
            return None
        self._add_clean(rec)
        if not self.save():
            self.recordings.remove(rec)
            return None
        return rec

    def get(self, rid):
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            return None
        for rec in self.recordings:
            if rec["id"] == rid:
                return rec
        return None

    def remove(self, rid):
        rec = self.get(rid)
        if rec is None:
            return False
        self.recordings.remove(rec)
        if not self.save():
            self.recordings.append(rec)
            return False
        return True

    def set_name(self, rid, name):
        rec = self.get(rid)
        if rec is None:
            return False
        name = str(name or "").strip()[:MAX_NAME_LEN]
        if not name:
            return False
        old_name = rec["name"]
        rec["name"] = name
        if not self.save():
            rec["name"] = old_name
            return False
        return True

    def set_loop(self, rid, loop):
        rec = self.get(rid)
        if rec is None:
            return False
        old_loop = rec["loop"]
        rec["loop"] = _as_bool(loop, False)
        if not self.save():
            rec["loop"] = old_loop
            return False
        return True

    def by_position(self, position):
        try:
            n = int(position)
        except (TypeError, ValueError):
            return None
        if 1 <= n <= len(self.recordings):
            return self.recordings[n - 1]
        return None

    def most_recent(self):
        if not self.recordings:
            return None
        return max(self.recordings,
                   key=lambda r: (r["created"], r["id"]))

    def export_to(self, path, rid=None):
        if rid is not None:
            rec = self.get(rid)
            if rec is None:
                return False, i18n.L("rec.no_sel")
            payload = {"snappyai_recording": FORMAT_VERSION,
                       "name": rec["name"],
                       "loop": rec["loop"],
                       "created": rec["created"],
                       "steps": [list(s) for s in rec["steps"]]}
        else:
            payload = {"snappyai_recordings": FORMAT_VERSION,
                       "recordings": [
                {"name": r["name"], "loop": r["loop"],
                 "created": r["created"],
                 "steps": [list(s) for s in r["steps"]]}
                for r in self.recordings]}
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, path)
            return True, i18n.L("rec.exported", path)
        except (OSError, TypeError, ValueError) as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False, i18n.L("rec.export_fail", exc)

    def import_from(self, path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            return False, i18n.L("rec.import_fail", exc)

        if not isinstance(data, dict):
            return False, i18n.L("rec.notfile")

        version = data.get("snappyai_recording") or data.get("snappyai_recordings")
        if version is not None:
            try:
                version = int(version)
            except (TypeError, ValueError):
                return False, i18n.L("rec.notfile")
            if version != FORMAT_VERSION:
                log("recordings: import format version %r != %d, ignoring" % (version, FORMAT_VERSION))
                return False, i18n.L("rec.bad_version", version, FORMAT_VERSION)

        items = []
        if isinstance(data, dict):
            if isinstance(data.get("recordings"), list):
                items = data["recordings"]
                if not items:
                    return True, i18n.L("rec.imported", 0, "")
            elif "steps" in data:
                items = [data]
        if not items:
            return False, i18n.L("rec.notfile")

        added = 0
        pre_len = len(self.recordings)
        pre_id = self._next_id
        for i, item in enumerate(items):
            rec = _clean_rec(item, "Imported %d" % (i + 1))
            if rec is None:
                continue
            try:
                _orig_created = int(item.get("created", 0)) if isinstance(item, dict) else 0
            except (TypeError, ValueError):
                _orig_created = 0
            rec["created"] = _orig_created if _orig_created > 0 else int(time.time())
            if len(self.recordings) >= MAX_RECORDINGS:
                log("Import stopped at %d recordings (library full)."
                    % MAX_RECORDINGS)
                break
            names = set(r["name"] for r in self.recordings)
            base, n = rec["name"], 2
            while rec["name"] in names:
                suffix = " (%d)" % n
                rec["name"] = base[:max(0, MAX_NAME_LEN - len(suffix))] \
                    + suffix
                n += 1
            self._add_clean(rec)
            added += 1

        if added:
            if not self.save():

                del self.recordings[pre_len:]
                self._next_id = pre_id
                return False, i18n.L("rec.import_fail", "could not save library")
        return added > 0, i18n.L(
            "rec.imported", added,
            "" if added else i18n.L("rec.imported_none"))

def _shutdown_executor():
    ex = _TIMEOUT_EXECUTOR
    if ex is not None:
        try:
            ex.shutdown(wait=False)
        except Exception:
            pass

atexit.register(_shutdown_executor)
