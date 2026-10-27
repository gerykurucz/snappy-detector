from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

TAG = 0x41544C54
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_QUIT = 0x0012
WH_MOUSE_LL = 14
INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

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
    import ctypes
    from ctypes import wintypes

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


def _send_click(user32: Any) -> None:
    import ctypes
    import time
    from ctypes import wintypes

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("mi", MOUSEINPUT)]

    user32.SendInput.argtypes = [
        wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int
    ]
    user32.SendInput.restype = wintypes.UINT

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
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    user32.DefWindowProcW.restype = wintypes.LPARAM

    W = 244
    H = 64
    state = {"down": False}

    @ctypes.WINFUNCTYPE(
        wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
        wintypes.LPARAM,
    )
    def _wndproc(hwnd: int, msg: int, wp: int, lp: int) -> int:
        try:
            if msg in (WM_LBUTTONDOWN, WM_LBUTTONUP):
                state["down"] = msg == WM_LBUTTONDOWN
                return 0
            return user32.DefWindowProcW(hwnd, msg, wp, lp)
        except Exception:
            return 0

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
    import ctypes
    from ctypes import wintypes

    msg = wintypes.MSG()
    deadline = time.monotonic() + timeout
    while not stop.is_set() and time.monotonic() < deadline:
        got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if got <= 0:
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def run_click_test(
    count: int = 20, interval: float = 0.15,
    on_progress: Callable[[str], None] | None = None,
) -> ClickLeg:
    if not sys.platform.startswith("win"):
        return ClickLeg(ok=False, error="input hook test needs Windows")
    if count <= 0:
        return ClickLeg(ok=False, error="no samples requested")
    import ctypes

    stamps: dict[int, float] = {}
    stop = threading.Event()
    handle = None
    try:
        handle = _install_hook(lambda msg: stamps.__setitem__(msg, _qpc_ms()))
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
                while WM_LBUTTONDOWN not in stamps and time.monotonic() < deadline:
                    if stop.is_set():
                        break
                    time.sleep(0.0005)
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
    _pump_until(stop, user32, timeout=count * (interval + 1.0) + 10.0)
    stop.set()
    t.join(timeout=5.0)
    if pad is not None:
        pad["close"]()
    _remove_hook(handle)
    if not rtts:
        detail = f"; {errors} injection failures" if errors else ""
        return ClickLeg(ok=False, error="no hook dispatches observed" + detail)
    return ClickLeg(ok=True, rtts_ms=rtts)


def list_hid_mice() -> list[str]:
    if not sys.platform.startswith("win"):
        return []
    import ctypes
    from ctypes import wintypes

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

from system_core import IS_WINDOWS, log







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


MACRO_MAX_TOKENS = 5


def _parse_macro_spec(spec, max_tokens=MACRO_MAX_TOKENS):
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
    return mods, tuple(keys)


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
                "the last one wins."
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
        act = _parse_macro_spec(aspec)
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
            for tmods, tvks, _, _ in macros:
                if not tvks and tmods == trig[0]:
                    log("Macro %d: mods-only macro key %r already used; "
                        "skipped." % (i, tspec))
                    skip = True
                    break
        if skip:
            continue

        macros.append((trig[0], trig[1], act[0], act[1]))

    macro_map = {}
    for m in macros:
        tmods, tvks, _, _ = m
        if tvks:
            macro_map[(tmods & 0x000F, tvks[0])] = m
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
MAX_KEYS_PER_MODE = 4

from system_core import BudgetComponent, Confidence, Risk, Verdict
from models import manual, reg_set
from models import Finding, Step, Tweak






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
        from system_core import OSFamily
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


def snappy_theme(pal: dict[str, str], fonts: dict[str, tuple]) -> Theme:
    return Theme(
        bg=pal["card"],
        raised=pal["desc_bg"],
        inset=pal["desc_bg"],
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

import os

POLL_MS = 80

TAB_FIX = "latency_fix"
TAB_LIVE = "latency_live"
TAB_TOOLS = "latency_tools"
LATENCY_TABS = (TAB_FIX, TAB_LIVE, TAB_TOOLS)

BRAND = "SnappyAI"


def default_out_root():
    try:
        import online

        base = online.secure_dir()
    except Exception:
        base = None
    if not base:
        base = os.getcwd()
    return os.path.join(base, "latency")


def mount(root, pages, kit, pal, fonts, out_root=None, backend="auto",
          brand=BRAND, cfg=None):
    from lattool_controller import GuiController
    from lattool_gui import SnappyAIApp

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
        app.var_timer.set(True)
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
        from system_core import stop_timer_request
        stop_timer_request()
    except Exception:
        pass

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

TRIGGER_BUTTONS = frozenset((PAD_BUTTONS.index("lt"), PAD_BUTTONS.index("rt")))

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

PAD_EMU_DEFAULT = ("a=enter;b=esc;x=space;y=tab;up=up;down=down;left=left;"
                   "right=right;lb=mouse4;rb=mouse5;ls=mouse1;rs=mouse2;"
                   "lt=mouse2;rt=mouse1;start=off;back=off")

DEFAULT_TRIGGER_THRESHOLD = 128
DEFAULT_POLL_HZ = 125

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
        from general_utils import dll_search_paths
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


def _threshold_triggers(lt, rt, threshold):
    mask = 0
    if lt >= threshold:
        mask |= 1 << PAD_BUTTONS.index("lt")
    if rt >= threshold:
        mask |= 1 << PAD_BUTTONS.index("rt")
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
    buttons = int(pad.wButtons) & 0xF3FF
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
    parsed = _parse_hotkey(raw)
    if parsed is None:
        return None
    return ("key", parsed[0] & 0x000F, parsed[1])


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

import system_core as i18n

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
        try:
            vals = []
            for v in step:
                if isinstance(v, bool) or not isinstance(v, int):
                    return None
                vals.append(v)
        except (TypeError, ValueError):
            return None
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
            from config import config_dir
            path = os.path.join(config_dir(), REC_FILE)
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
        if version is not None and version != FORMAT_VERSION:
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
                return False, i18n.L("rec.import_fail", "could not save library")
        return added > 0, i18n.L(
            "rec.imported", added,
            "" if added else i18n.L("rec.imported_none"))
