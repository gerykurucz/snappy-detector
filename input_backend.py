"""Mouse input delivery backends: Windows SendInput (default) and an
optional Interception backend (github.com/oblitum/Interception).

Why both exist
--------------
SendInput is what every accessibility tool uses: no driver, works out of
the box, and Snappy AI's own low-level hooks recognise injected events
(LLMHF_INJECTED) so assistance never feeds itself. Its one weakness is
that a minority of games and Raw-Input applications ignore synthetic
events entirely - clicks simply never arrive there.

Interception injects at the driver layer, below Raw Input, so those
applications see the click as genuine hardware. The price: it needs a
signed kernel driver installed by the user (interception.exe install +
reboot). No DLL is involved anywhere: strokes go straight to the
driver, which is also why driver updates can never desync a bundled
library. That friction is why Win32 stays the DEFAULT and
Interception is strictly opt-in.

Design rules (mirrors the capture_backend pattern)
--------------------------------------------------
- Pure stdlib ctypes; imports cleanly on any OS (the GUI reads
  available() to decorate its dropdown), touches the driver only
  when the user actually selects "interception".
- NEVER fatal: any failure (no driver, device busy, send
  refused) logs once and degrades to Win32 for that call AND for all
  future calls, so a half-installed setup can never eat a click.
- Coordinates: absolute SetCursorPos is emulated with per-move relative
  strokes (x = target - current cursor). Relative deltas are exactly
  what the mouse protocol carries, so apps that read raw motion stay in
  sync with where the pointer actually is.

Backend contract (what assist.py / win32rt.py consume):
    move(x, y)             teleport/warp the cursor to screen (x, y)
    nudge(dx, dy)          RELATIVE motion step (game-aim steering: the
                           view rotates by the delta; the cursor is never
                           teleported, so raw-input games stay in sync)
    button(down)           left-button transition (True = press)
    ok_for_button()        True if a real button event may be sent here
    note_failure(where)    log-once + permanent degrade to Win32
    available()            classmethod: is this even possible right now?
"""

import ctypes
import threading
import time

import input_utils as gamepad
from system_core import IS_WINDOWS, log

DEFAULT_SERVER_URL = "ws://127.0.0.1:19561"


class _Timeout(Exception):
    """Raised by _call_with_timeout when the callee never answered."""


def _call_with_timeout(fn, timeout, *args):
    """Run fn(*args) on a daemon thread; RETURN its result, or raise
    _Timeout if it has not answered within `timeout` seconds.

    v8.61: this used to signal a timeout by returning None - which is
    indistinguishable from a function that legitimately answers "nothing
    found". Our discovery walk returns None exactly that way, so every
    empty answer was mis-reported as "device discovery did not return
    within 8 s (driver installed but wedged)" even when the driver had
    answered instantly and simply listed no mouse. Raising makes the two
    cases impossible to confuse. The ctypes call itself is never
    cancellable, so a genuinely wedged driver still leaves one parked
    daemon thread behind - harmless for a GUI process that would
    otherwise hang at startup forever."""
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
    """Set Enhance-pointer-precision on/off; returns the previous state
    (None when it could not be read, so callers can still restore).

    SPI_GETMOUSE/SPI_SETMOUSE carry [MouseSpeed, MouseThreshold1,
    MouseThreshold2]; "Enhance pointer precision" IS MouseSpeed -
    element 0. Windows ballistics scale fast synthetic flicks
    unpredictably, so the Interception backend turns EPP off once for
    the whole session (not per stroke: each write broadcasts
    WM_SETTINGCHANGE to every top-level window) and restores it on
    close(). Import-safe anywhere: all failures are silent no-ops.
    """
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
    """Say once why a non-left role button is not delivered here.

    The driver and server backends are proven against the left button only.
    Inventing button constants for a kernel driver, or code names for a server
    whose vocabulary is not documented in this tree, is exactly the guess that
    fails silently inside a game - so the role falls back to the standard
    Windows method (assist._button does that on a False return) and the log
    says so once instead of per click.
    """
    key = (backend, int(index))
    if key in _ROLE_NOTED:
        return
    _ROLE_NOTED.add(key)
    log("%s: mouse%d cannot be sent through this backend; that role's clicks "
        "go through the standard Windows method." % (backend, int(index) + 1))


class Win32Backend(object):
    """The default: SetCursorPos warps plus SendInput button events.

    Thin indirection over the exact calls used since v1 - behaviour is
    bit-for-bit unchanged when this backend is active.
    """

    name = "win32"

    @classmethod
    def available(cls):
        return IS_WINDOWS

    def move(self, x, y):
        import win32rt
        win32rt.user32.SetCursorPos(int(x), int(y))

    def nudge(self, dx, dy):
        """One relative motion step (SendInput MOUSEEVENTF_MOVE).

        This is what aim steering uses while a button is held: the
        delta rotates the view in raw-input games and walks the
        pointer on the desktop, without ever teleporting it.
        """
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
        """One transition of ANY mouse button: 0=left ... 4=second side.

        Through win32rt.inject_mouse rather than a hand-built INPUT here,
        because the two side buttons need mouseData as well as the XDOWN/XUP
        flags - and that table already exists and is the same one recording
        playback uses, so a click and a replayed click cannot diverge.
        """
        import win32rt
        return bool(win32rt.inject_mouse(index, down))

    def ok_for_button(self):
        return True

    def note_failure(self, where):
        pass


class InterceptionBackend(object):
    """Driver-level injection straight into keyboard.sys/mouse.sys (opt-in).

    Talks to the kernel driver DIRECTLY through kernel32 (CreateFileA +
    DeviceIoControl on \\\\.\\interception00..19) with 24-byte HHHHIiiI
    strokes - byte-identical to what interception-python packs, verified
    stroke-for-stroke against it on a live driver. NO interception.dll
    is loaded, needed, or searched for.

    Why not the DLL: its interception_send() takes the *user-side*
    InterceptionMouseStroke (state, flags, rolling, x, y) and remaps it
    into the driver layout itself - so handing it an already-packed
    driver stroke comes out axis-swapped (a +40 X step left as +40 Y),
    while a hand-rolled struct comes out a motionless no-op that still
    reports "1 stroke written". Both failure modes are silent and
    neither can happen when the bytes go straight to the driver.

    Device choice mirrors interception-python's auto_capture_devices:
    the first mouse slot (interception10..19) whose hardware ID reads
    back. Sends go through that handle; every other slot is closed -
    this backend never receives, so parked handles would only leak.
    """

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
        """Open all 20 control slots + sync events; return the first
        mouse slot (10..19) whose hardware ID reads back, else None.

        A slot that will not open, sync, or identify is skipped, never
        fatal: one wedged slot must not veto a healthy driver. Every
        slot that is not chosen is closed again - parked handles would
        only leak, this backend never receives.
        """
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
        """Sync-event handshake, as oblitum's create_context does."""
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
        """Hardware ID string of slot `num`, or None when unreadable."""
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
        """Cheap check for the GUI: any mouse control slot opens.

        Safe off Windows; never raises. Results expire after 30 s so
        installing the driver while the app runs takes effect without
        a relaunch. Runs on the GUI's worker thread; a wedged driver
        can only park that daemon thread, bounded by the timeout.
        """
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
        """One relative-motion stroke (flags=0, no buttons)."""
        return self._pack(self._MOUSE_MOVE_RELATIVE, 0, 0, dx, dy)

    def _button_stroke(self, down):
        """One button stroke (flags=ABSOLUTE, x=y=0), exactly what
        interception-python's mouse_down/mouse_up pack - the layout
        Unibot proves against this exact driver."""
        return self._pack(self._MOUSE_MOVE_ABSOLUTE,
                          self._MOUSE_LEFT_DOWN if down
                          else self._MOUSE_LEFT_UP,
                          0, 0, 0)

    def _write(self, payload):
        """One ioctl carrying len(payload)/24 strokes. Returns strokes
        written, or -1 on transport failure."""
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
        """Move the cursor to absolute (x, y).

        Short hops go as relative strokes with a settle-and-correct
        loop (driver-level motion passes through pointer speed and DPI
        exactly like a hand). Distances beyond _MAX_STROKE_TRAVEL use a
        SetCursorPos warp instead: four 128 px strokes can only cover
        ~512 px and the loop silently stops short of farther targets,
        leaving the next click at the wrong place. The warp is honest
        because the caller treats every move as a teleport anyway.
        """
        import win32rt
        tx, ty = int(x), int(y)
        try:
            cx, cy = win32rt.get_cursor_pos()
        except Exception:
            # Transient failure: fall back without degrading
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
        """One relative motion step as a single move-only stroke.

        Fire-and-forget on purpose: per-frame aim steering must never
        do the settle-and-correct cursor loop that move() does (three
        cursor reads per step would triple the cost of every steering
        frame and fight the game's own pointer management). A dropped
        step simply retries next frame; _send() degrades honestly.
        """
        dx, dy = int(round(dx)), int(round(dy))
        if not dx and not dy:
            return True
        # EPP was disabled once, at init - no per-stroke setting churn.
        return self._send(self._move_stroke(dx, dy), "nudge")

    def __del__(self):
        if self._accel_disabled and self._prev_accel is not None:
            try:
                _set_mouse_accel(self._prev_accel)
            except Exception:
                pass

    def _expect_now(self, kind):
        """Ledger one synthetic button event at the live cursor.

        Called BEFORE the stroke goes out; the hook consumes it on
        arrival instead of mistaking it for the user's hand. Cursor
        read failure degrades to position-agnostic (kind + fresh).
        Never raises into the send path.
        """
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
        """True when this hook event is our own synthetic output.

        wparam is the raw mouse message; only LEFT transitions are
        ever ledgered (every other button passes through untouched).
        Unknown backends/ doubles answer False via getattr.
        """
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
        """One complete synthetic click as a SINGLE two-stroke packet
        (down+up delivered in one interception_send transaction). There is
        NO hold between the transitions - the driver queues them back to
        back; the randomized sleep happens AFTER the send and only paces
        the next click. Borrowed from connorabs'
        osu!mania pixel-trigger engine, which sends tap pairs atomically:
        one kernel transaction means nothing interleaves between down and
        up, and the driver's own queue paces the gap instead of Python
        thread scheduling.

        Returns True if the whole packet went out. False = backend
        degraded (caller must hand BOTH events to Win32 itself - that is
        exactly what assist._click does on False, so a mid-call failure
        can never leave the logical button stuck)."""
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
        """Send `count` strokes in ONE ioctl transaction, under the
        same lock _send uses (the atomic tap pair relies on this)."""
        with self._lock:
            if self._degraded or self._handle is None:
                return False
            if self._write(payload) != count:
                self.note_failure(what)
                return False
            return True

    def _send(self, payload, what):
        """Deliver one 24-byte stroke. Returns True on success. NEVER
        swallows a failure silently: any refused/errored send degrades
        the backend AND returns False, so callers can hand the event
        to Win32."""
        with self._lock:
            if self._degraded or self._handle is None:
                return False
            if self._write(payload) != 1:
                self.note_failure(what)
                return False
            return True


class RebindBackend(object):
    """External-HID output through a Rebind Lua server (opt-in).

    The same wire protocol Unibot speaks - websocket JSON frames:
      {"t": "ping"}                              -> {"t": "pong"}
      {"t": "hid.move", "dx": dx, "dy": dy}      relative motion step
      {"t": "hid.press", "code": "Mouse1", "hold_ms": N}  tap unit
    The server drives genuine hardware-level input, so games that
    ignore SendInput AND lack the Interception driver still obey -
    which is exactly why Unibot works where synthetic injection
    does not. Unibot never touches Interception; neither does this.

    repeat_hold = True: the protocol has press UNITS (down+up pairs),
    not separate down/up transitions, so a sustained hold cannot be
    a held level here. Hold loops re-tap at tap_interval instead
    (Unibot's target_cps idea) - full-auto weapons keep firing,
    semi-autos fire per tap. Nothing can ever stick: release sends
    nothing because nothing is held.
    """

    name = "rebind"
    repeat_hold = True
    tap_interval = 1.0 / 12.0
    _CONNECT_TIMEOUT = 5.0
    _PING_TIMEOUT = 3.0

    def __init__(self, cfg):
        import json as _json
        self._json = _json
        self._lock = threading.Lock()
        self._degraded = False
        self._reason = ""
        try:
            url = str(cfg.get("server_url") or DEFAULT_SERVER_URL).strip()
        except Exception:
            url = ""
        if not url:
            raise RuntimeError("server_url is empty")
        self.url = url
        self._ws = None
        # The server delivers taps on its own schedule (40-80 ms holds);
        # if it injects below the hook layer the events carry no
        # LLMHF_INJECTED mark, so ledger them exactly like the driver
        # backend does - with a window wide enough for the round trip.
        self._expected = _ExpectedLedger(tol=12, ttl=0.5)
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError("websocket-client is not installed "
                               "(python -m pip install websocket-client)") \
                from exc
        try:
            ws = websocket.create_connection(
                url, timeout=self._CONNECT_TIMEOUT)
            ws.settimeout(self._PING_TIMEOUT)
        except Exception as exc:
            raise RuntimeError("could not reach %s (%r)" % (url, exc)) \
                from exc
        self._ws = ws


        try:
            ws.send(self._json.dumps({"t": "ping"}))
            raw = ws.recv()
            import json as _j2
            if not (isinstance(raw, str) and
                    _j2.loads(raw).get("t") == "pong"):
                raise RuntimeError("no pong from %s" % url)
        except Exception as exc:
            try:
                ws.close()
            except Exception:
                pass
            self._ws = None
            raise RuntimeError("Rebind handshake failed (%r)" % (exc,)) \
                from exc
        log("Input: Rebind connected (%s) - hardware-level output." % url)

    @classmethod
    def available(cls, url=None):
        """True when the library is installed AND a server answers."""
        try:
            import websocket
            ws = websocket.create_connection(
                str(url or DEFAULT_SERVER_URL), timeout=2.0)
        except Exception:
            return False
        try:
            ws.settimeout(2.0)
            ws.send('{"t":"ping"}')
            import json as _j
            return _j.loads(ws.recv()).get("t") == "pong"
        except Exception:
            return False
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def ok_for_button(self):
        return not self._degraded

    def close(self):
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        self._ws = None

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
        """True when this hook event is our own server-side tap."""
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

    def note_failure(self, where):
        if not self._degraded:
            self._degraded = True
            log("Input: Rebind %s failed; falling back to Windows "
                "injection for the rest of this session." % (where,))

    def _send_obj(self, obj, what):
        """One JSON frame; False (degraded) on any transport problem."""
        with self._lock:
            if self._degraded or self._ws is None:
                return False
            try:
                self._ws.send(self._json.dumps(obj, separators=(",", ":")))
            except Exception:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None
                self.note_failure(what)
                return False
            return True

    def move(self, x, y):
        """Absolute move expressed as one relative step (the protocol
        only carries deltas). Falls back to Win32 when the cursor
        cannot be read - without an anchor a delta is a guess."""
        import win32rt
        tx, ty = int(x), int(y)
        try:
            cx, cy = win32rt.get_cursor_pos()
        except Exception:
            return Win32Backend().move(tx, ty)
        if (cx, cy) == (tx, ty):
            return True
        if not self._send_obj({"t": "hid.move",
                               "dx": tx - cx, "dy": ty - cy}, "move"):
            return Win32Backend().move(tx, ty)
        return True

    def nudge(self, dx, dy):
        """One relative steering step - the per-frame hold primitive.

        No settle loop, no cursor reads (Unibot's send_move is equally
        fire-and-forget): the next steering frame corrects from a
        fresh sighting instead.
        """
        dx, dy = int(round(dx)), int(round(dy))
        if not dx and not dy:
            return True
        if not self._send_obj({"t": "hid.move",
                               "dx": dx, "dy": dy}, "nudge"):
            return Win32Backend().nudge(dx, dy)
        return True

    def button(self, down, index=0):
        """Down = one tap unit (Unibot's 40-80 ms hold); up = no-op.

        There is deliberately no held level to release - see the
        class docstring. Sustained fire comes from the caller's
        hold loop re-tapping at tap_interval.
        """
        if index:
            note_mouse_role("Server input", index)
            return False
        if not down:
            return self.ok_for_button()
        import random
        hold_ms = random.randint(40, 80)
        self._expect_now("down")
        self._expect_now("up")
        if not self._send_obj({"t": "hid.press", "code": "Mouse1",
                               "hold_ms": hold_ms}, "press"):
            return Win32Backend().button(True)
        return True

    def send_click(self, index=0):
        """One tap unit, Unibot-style (press, brief visible hold)."""
        import random
        import time as _time
        if index:
            note_mouse_role("Server input", index)
            return False
        if not self.ok_for_button():
            return False
        hold_ms = random.randint(40, 80)
        self._expect_now("down")
        self._expect_now("up")
        if not self._send_obj({"t": "hid.press", "code": "Mouse1",
                               "hold_ms": hold_ms}, "click"):
            return False
        _time.sleep(random.uniform(0.025, 0.034))
        return True


class _ExpectedLedger(object):
    """Remembers our own synthetic button events so the low-level hook
    can tell them apart from the user's physical ones.

    Driver-level output (Interception) arrives with NO injected mark -
    indistinguishable from hardware. Without this, our own synthetic
    UP lands in the tracked button state and ends a real hold (mode-1
    hold steering died exactly this way: snap-click, synthetic UP,
    session never armed), and our synthetic DOWN can queue phantom
    jobs. Win32 output needs none of this (SendInput marks LLMHF_INJECTED,
    which the hook already skips).

    Matching is kind + position + fresh: a real press far away, or
    more than _EXPECT_TTL after the send, never matches. Entries are
    consumed FIFO; orphans (failed sends) expire instead of piling up.

    The window is deliberately TIGHT (0.3 s, 12 px): synthetic output
    arrives back within milliseconds at exact coordinates, so anything
    looser eats the user's own re-taps - aimed follow-up shots at the
    same spot inside the window pass through raw, unassisted and
    unlogged, which reads exactly like "single clicks never assist".
    """

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


INPUT_BACKEND_CHOICES = ("win32", "driver", "server")

_BACKEND_ALIASES = {
    "win32": "win32",
    "driver": "driver",
    "interception": "driver",
    "server": "server",
    "rebind": "server",
}



# =====================================================================
# Controller OUTPUT
#
# XInput reads a pad; it cannot make one. So a controller recording or a
# pad macro has to leave the program through one of two doors:
#
#   ViGemPadBackend    a real virtual Xbox 360 pad, via the ViGEmBus driver
#                      and its ViGEmClient.dll. Games that only accept a
#                      controller see genuine pad input. Opt-in exactly like
#                      the Interception driver: nothing is bundled, and a
#                      missing driver or DLL degrades instead of failing.
#   EmulatedPadBackend no driver, no DLL. Pad output becomes keyboard and
#                      mouse through the same SendInput path every other
#                      backend uses, using the pad_emulate table.
#
# Contract (what assist.py consumes):
#     button(index, down)   one pad button transition (gamepad.PAD_BUTTONS)
#     axis(index, value)    one analogue axis (gamepad.PAD_AXES)
#     flush()               publish the accumulated report (ViGEm only)
#     ok_for_pad()          may pad output be sent right now
#     note_failure(where)   log once and degrade
#     close()               detach/release
#     available()           classmethod: is this even possible right now
# =====================================================================

PAD_EMU_DEFAULT = gamepad.PAD_EMU_DEFAULT

PAD_EMU_AXIS_STEP = 14.0

_MOUSE_WORDS = {"mouse1": 0, "mouse2": 1, "mouse3": 2, "mouse4": 3, "mouse5": 4}


def parse_emulation(spec):
    """The pad_emulate table -> {button index: action or None}.

    An action is ("key", mods, vk), ("mouse", button), ("wheel", notches) or
    None ("off"). Semicolon-separated `button=output` pairs; unknown buttons
    are ignored with a log line, unknown outputs make that ONE pair off
    rather than throwing the whole table away.
    """
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
    """The ViGEmBus payload. Its button mask is bit-for-bit the XInput mask
    gamepad.py already builds, so a report needs no translation."""

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
    """A virtual Xbox 360 pad (ViGEmBus). True 1:1 controller output.

    ViGEmBus is a signed kernel driver the user installs once, plus its
    ViGEmClient.dll next to SnappyAI.exe. Neither is bundled: without them
    `available()` is False and `make_pad_backend` falls back to emulation
    after saying so once, which is exactly how the Interception backend
    behaves. Every call is guarded and every failure degrades - a pad that
    disappears mid-session must never take the assist engine with it.
    """

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
        """FULL probe: DLL loads AND a virtual pad can be created.

        Deliberately UNUSED in-tree: creating and destroying a probe pad
        flashes a phantom Xbox 360 controller into every game's device
        list, which is exactly why make_pad_backend probes the DLL only.
        Kept for manual diagnostics.
        """
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
            # LT/RT are triggers, not buttons - they go via axis()
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
        """Zero the report and publish it, so nothing can stay held."""
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
    """Pad output as keyboard and mouse. No driver, no DLL, works everywhere.

    The table is `pad_emulate` (see PAD_EMU_DEFAULT). Stick axes move the
    cursor by a fixed step per call, so a recorded sweep replays as the same
    sweep at the rate it was recorded; triggers and buttons become the mapped
    key or mouse transition and can be HELD, because inject_key sends real
    down/up transitions rather than a single tap.
    """

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
        """Stick axes walk the cursor; triggers have no emulated meaning
        beyond the button the threshold already produced."""
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
        # XInput reports +Y as UP and the screen grows downwards, so the
        # vertical axis is negated. Each axis is normalised on its own here:
        # a recording stores one axis per step, so the radial deadzone that
        # the live drive path uses has nothing to pair it with.
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
    """The controller output door. ALWAYS returns something usable.

    pad_output = vigem      -> ViGemPadBackend when the driver and its client
      DLL answer, emulation otherwise (logged once).
    pad_output = emulate    -> EmulatedPadBackend, never touching ViGEm.
    pad_output = auto (default) -> ViGEm when available, emulation otherwise.
    """
    choice = str((cfg.get("pad_output", "auto") if cfg is not None else "auto")
                 or "auto").strip().lower()
    _EMU = ("emulate", "emulated", "km", "keyboard")
    if choice not in ("auto", "vigem", "vigembus") + _EMU:
        log("Input: pad_output %r is not one of auto, vigem, emulate; using "
            "auto." % (choice,))
        choice = "auto"
    if choice in _EMU:
        return EmulatedPadBackend(cfg)

    # Probe the DLL, not the driver: available() would create a real virtual
    # pad and destroy it again, so a machine without ViGEmBus would still
    # flash a phantom Xbox 360 controller into every game's controller list
    # on each start-up.
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
    """Build the configured backend; ALWAYS returns something usable.

    input_backend = win32   (default)  -> Win32Backend, nothing loaded.
    input_backend = driver             -> InterceptionBackend if the
      driver answers, Win32Backend otherwise (logged once). No DLL
      is involved: strokes go straight to keyboard.sys/mouse.sys.
    input_backend = server             -> RebindBackend if the companion
      websocket server answers, Win32Backend otherwise (logged once).
    The pre-v8 spellings "interception" and "rebind" are still accepted.
    """
    raw = str(cfg.get("input_backend", "win32") or "win32").strip().lower()
    choice = _BACKEND_ALIASES.get(raw)
    if choice is None:
        log("Input: input_backend %r is not one of %s; using the standard "
            "Windows method." % (raw, ", ".join(INPUT_BACKEND_CHOICES)))
        return Win32Backend()
    if choice == "server":
        try:
            be = RebindBackend(cfg)
            if be.ok_for_button():
                return be
        except Exception as exc:
            log("Input: Rebind unavailable (%r); using the "
                "standard Windows method." % (exc,))
            be = None
        where = getattr(be, "url", None) if be is not None else None
        reason = getattr(be, "_reason", "") if be is not None else ""
        if where is None and not reason:
            why = ("could not reach the Rebind server (is it running? "
                   "same address Unibot uses, default "
                   "ws://127.0.0.1:19561)")
        elif reason:
            why = "startup check failed: %s" % reason
        else:
            why = "Rebind server at %s did not answer" % where
        log("Input: Rebind selected but not working (%s). Using "
            "the standard Windows method." % why)
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
