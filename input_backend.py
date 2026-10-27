
import ctypes
import threading
import time

import input_utils as gamepad
from system_core import IS_WINDOWS, log

class _Timeout(Exception):
    pass


def _call_with_timeout(fn, timeout, *args):
    box = {}

    def run():
        try:
            box["result"] = fn(*args)
        except Exception as exc:
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise _Timeout("did not return within %.0f s" % timeout)
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _set_mouse_accel(enabled):
    try:
        import ctypes as _c
        prev = (_c.c_int * 3)()
        if not _c.windll.user32.SystemParametersInfoA(0x3, 0, prev, 0):
            return None
        was = bool(prev[0])
        prev[0] = int(bool(enabled))
        if not _c.windll.user32.SystemParametersInfoA(0x4, 0, prev, 0x2):
            return None
        return was
    except Exception:
        return None


_ROLE_NOTED = set()


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
        import win32rt
        win32rt.user32.SetCursorPos(int(x), int(y))

    def nudge(self, dx, dy):
        import ctypes
        import win32rt
        dx, dy = int(round(dx)), int(round(dy))
        if not dx and not dy:
            return True
        inp = win32rt.INPUT()
        inp.type = win32rt.INPUT_MOUSE
        inp.u.mi.dx = dx
        inp.u.mi.dy = dy
        inp.u.mi.dwFlags = win32rt.MOUSEEVENTF_MOVE
        return bool(win32rt.user32.SendInput(1, ctypes.byref(inp),
                                             ctypes.sizeof(win32rt.INPUT)))

    def button(self, down, index=0):
        import win32rt
        return bool(win32rt.inject_mouse(index, down))

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
        if _set_mouse_accel(False) is True:
            self._prev_accel = True
            self._accel_disabled = True
            log("Input: Interception turned 'Enhance pointer precision' off "
                "for this session (Windows ballistics scale synthetic flicks "
                "unpredictably); it is restored when the app quits.")

    def _open_devices(self):
        self._handles = [None] * self._MOUSE_SLOTS
        for num in range(self._MOUSE_SLOTS):
            try:
                h = self._k32.CreateFileA(
                    ("\\\\.\\interception%02d" % num).encode(),
                    self._GENERIC_READ, 0, None,
                    self._OPEN_EXISTING, 0, None)
            except Exception:
                continue
            if h is None or h == -1:
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
        self._close_all()
        if self._accel_disabled and self._prev_accel is not None:
            _set_mouse_accel(self._prev_accel)
        self._accel_disabled = False
        self._prev_accel = None

    @classmethod
    def available(cls):
        if not IS_WINDOWS:
            return False
        _now = time.monotonic()
        cached = getattr(cls, "_avail_cache", None)
        cached_at = getattr(cls, "_avail_cache_at", 0.0)
        if cached is not None and (_now - cached_at) < 30.0:
            return cached
        try:
            ok = bool(_call_with_timeout(cls._probe_slots, 8.0))
        except _Timeout:
            ok = False
        except Exception:
            ok = False
        cls._avail_cache = ok
        cls._avail_cache_at = time.monotonic()
        return ok

    @staticmethod
    def _probe_slots():
        import ctypes as _c
        try:
            k32 = _c.windll.kernel32
        except Exception:
            return False
        for num in range(10, 20):
            try:
                h = k32.CreateFileA(
                    ("\\\\.\\interception%02d" % num).encode(),
                    0x80000000, 0, None, 3, 0, None)
            except Exception:
                continue
            if h is not None and h != -1:
                try:
                    k32.CloseHandle(h)
                except Exception:
                    pass
                return True
        return False

    @classmethod
    def _pack(cls, flags, btn, data, x, y):
        import struct as _struct
        return _struct.pack(cls._STROKE_FMT, 0, flags, btn, data, 0,
                            int(x), int(y), 0)

    def _move_stroke(self, dx, dy):
        return self._pack(self._MOUSE_MOVE_RELATIVE, 0, 0, dx, dy)

    def _button_stroke(self, down):
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
        import win32rt
        tx, ty = int(x), int(y)
        try:
            cx, cy = win32rt.get_cursor_pos()
        except Exception:
            win32rt.user32.SetCursorPos(tx, ty)
            win32rt.user32.SetCursorPos(tx, ty)
            return
        if (cx, cy) == (tx, ty):
            return
        if abs(tx - cx) > self._MAX_STROKE_TRAVEL or \
           abs(ty - cy) > self._MAX_STROKE_TRAVEL:
            win32rt.user32.SetCursorPos(tx, ty)
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
            self._send(s, "move")
            if self._degraded:
                win32rt.user32.SetCursorPos(tx, ty)
                return
            time.sleep(0.001)
            try:
                nx, ny = win32rt.get_cursor_pos()
            except Exception:
                try:
                    win32rt.user32.SetCursorPos(tx, ty)
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
                if win32rt.get_cursor_pos() != (tx, ty):
                    win32rt.user32.SetCursorPos(tx, ty)
            except Exception:
                pass

    def nudge(self, dx, dy):
        dx, dy = int(round(dx)), int(round(dy))
        if not dx and not dy:
            return True
        return self._send(self._move_stroke(dx, dy), "nudge")

    def __del__(self):
        if self._accel_disabled and self._prev_accel is not None:
            try:
                _set_mouse_accel(self._prev_accel)
            except Exception:
                pass

    def _expect_now(self, kind):
        try:
            import win32rt
            cx, cy = win32rt.get_cursor_pos()
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

        time.sleep(random.uniform(0.015, 0.035))
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

PAD_EMU_DEFAULT = gamepad.PAD_EMU_DEFAULT

PAD_EMU_AXIS_STEP = 14.0

_MOUSE_WORDS = {"mouse1": 0, "mouse2": 1, "mouse3": 2, "mouse4": 3, "mouse5": 4}


def parse_emulation(spec):
    import input_utils as hotkeys
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
        index = gamepad.parse_token(name)
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
        parsed = hotkeys._parse_hotkey(value)
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
        from general_utils import dll_pin_ok, dll_search_paths
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
                dll.vigem_target_x360_alloc.argtypes = [ctypes.c_void_p]
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
            self._target = dll.vigem_target_x360_alloc(self._client)
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
                name = gamepad.PAD_BUTTONS[int(index)]
            except (IndexError, TypeError, ValueError):
                return False
            if name in ("lt", "rt"):
                return True
            mask = gamepad._XINPUT_MASKS.get(name)
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
            name = gamepad.PAD_AXES[int(index)]
            value = gamepad.normalise_axis(int(index), value)
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
        import win32rt
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
                return bool(win32rt.inject_key(action[1], action[2], bool(down)))
            if kind == "mouse":
                return bool(win32rt.inject_mouse(action[1], bool(down)))
            if down and kind == "wheel":
                return bool(win32rt.inject_wheel(action[1]))
            if down and kind == "hwheel":
                return bool(win32rt.inject_wheel(action[1], horizontal=True))
        except Exception as exc:
            self.note_failure("%s(%r)" % (kind, exc))
            return False
        return True

    def axis(self, index, value):
        if not self.ok_for_pad():
            return False
        import win32rt
        try:
            name = gamepad.PAD_AXES[int(index)]
        except (IndexError, TypeError, ValueError):
            return False
        if name not in ("lx", "ly", "rx", "ry"):
            return True
        try:
            value = int(value)
        except (TypeError, ValueError):
            return False
        zone = gamepad.DEADZONE_LEFT if name in ("lx", "ly") \
            else gamepad.DEADZONE_RIGHT
        if name in ("lx", "rx"):
            fx, _fy = gamepad.apply_deadzone(value, 0, zone)
            fy = 0.0
        else:
            _fx, fy = gamepad.apply_deadzone(0, -value, zone)
            fx = 0.0
        if abs(fx) < 1e-6 and abs(fy) < 1e-6:
            return True
        try:
            x, y = win32rt.get_cursor_pos()
            step = PAD_EMU_AXIS_STEP
            return bool(win32rt.move_cursor(int(round(x + fx * step)),
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
        # Check if ViGEm steering is allowed by config
        if cfg is not None:
            choice = str(cfg.get("pad_output", "auto") or "auto").strip().lower()
            _EMU = ("emulate", "emulated", "km", "keyboard")
            if choice in _EMU:
                return False  # User explicitly disabled ViGEm output
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

        # Read aim_speed and aim_y_speed_multiplier from cfg if provided
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
