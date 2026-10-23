"""Shared core: app constants, logging, i18n, the latency-budget enums,
error types, Ed25519 verification, the colour-spec vocabulary, the system
inventory model, and the safety policy. Leaf module: stdlib only, imported
by everything, imports nothing from the project."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

# ================== budget (latency enums) ==================

"""The latency budget.

Total input-to-photon latency for an online game is a sum of four largely independent
terms. Every tweak in the catalog must declare which term it addresses, because that
declaration is what makes the catalog auditable.
"""

class BudgetComponent(str, Enum):
    """Which term of the latency budget a tweak addresses."""
    NETWORK_PATH = "network_path"
    LOCAL_QUEUE = "local_queue"
    HOST_PROCESSING = "host_processing"
    RENDER_INPUT = "render_input"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")

    @property
    def floor_note(self) -> str:
        return _FLOOR_NOTES[self]


_FLOOR_NOTES: dict[BudgetComponent, str] = {
    BudgetComponent.NETWORK_PATH: (
        "Bounded by the speed of light: ~10 ms RTT per 1000 km of fibre, before any "
        "queueing. No PC-side software can go below this floor."
    ),
    BudgetComponent.LOCAL_QUEUE: (
        "Unbounded in practice. A saturated link with a deep dumb queue can add "
        "hundreds of ms. This is usually the largest addressable term."
    ),
    BudgetComponent.HOST_PROCESSING: (
        "Typically single-digit ms. Hardware- and driver-specific; effects are "
        "usually in the tail (spikes), not the mean."
    ),
    BudgetComponent.RENDER_INPUT: (
        "Often the dominant term. Roughly: (1 / refresh_rate) for the panel, plus "
        "queued frames times frame time, plus HID polling interval."
    ),
}


class Verdict(str, Enum):
    """Outcome of a tweak's probe against a specific machine."""
    PROBLEM = "problem"
    NO_PROBLEM = "no_problem"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"

    @property
    def actionable(self) -> bool:
        return self is Verdict.PROBLEM


class Risk(str, Enum):
    """How bad the worst plausible outcome is."""
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Confidence(str, Enum):
    """Strength of evidence that the mechanism produces the claimed effect."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNVERIFIED = "unverified"


class Vendor(str, Enum):
    """GPU / NIC vendor, for driver-specific gating."""
    NVIDIA = "nvidia"
    AMD = "amd"
    INTEL = "intel"
    REALTEK = "realtek"
    QUALCOMM = "qualcomm"
    BROADCOM = "broadcom"
    MELLANOX = "mellanox"
    OTHER = "other"
    UNKNOWN = "unknown"


class OSFamily(str, Enum):
    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"
    OTHER = "other"


# ================== errors ==================

class LattoolError(Exception):
    """Base lattool error."""
    pass


class RefusedTweakError(LattoolError):
    """A tweak was refused by policy or catalog rules."""
    pass


class TweakValidationError(LattoolError):
    """A tweak plan could not be validated."""
    pass

SnappyAIError = LattoolError


# ================== edsig (Ed25519, stdlib only) ==================


_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def _sha512(b):
    return hashlib.sha512(b).digest()


def _inv(x):
    return pow(x, _P - 2, _P)


def _recover_x(y, sign):
    if y >= _P:
        return None
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * pow(2, (_P - 1) // 4, _P)) % _P
    if (x * x - xx) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_Gy = 4 * _inv(5) % _P
_Gx = _recover_x(_Gy, 0)


def _decode_point(s):
    if len(s) != 32:
        return None
    b = bytearray(s)
    sign = b[31] >> 7
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _encode_point(pt):
    x, y, z, _t = pt
    if z == 0:
        return _encode_point((0, 1, 1, 0))
    zi = _inv(z)
    x = x * zi % _P
    y = y * zi % _P
    b = (y | ((x & 1) << 255)).to_bytes(32, "little")
    return bytes(b)


def _add(p1, p2):
    x1, y1, z1, t1 = p1
    x2, y2, z2, t2 = p2
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = 2 * t1 * t2 % _P * _D % _P
    d = 2 * z1 * z2 % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s, pt):
    out = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            out = _add(out, pt)
        pt = _add(pt, pt)
        s >>= 1
    return out


def _point_equal(a, b):
    return _encode_point(a) == _encode_point(b)


def public_from_secret(secret):
    if len(secret) != 32:
        raise ValueError("secret must be 32 bytes")
    h = _sha512(secret)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return _encode_point(_mul(a, (_Gx, _Gy, 1, _Gx * _Gy % _P)))


def sign(secret, message):
    if len(secret) != 32:
        raise ValueError("secret must be 32 bytes")
    h = _sha512(secret)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    prefix = h[32:]
    pub = _encode_point(_mul(a, (_Gx, _Gy, 1, _Gx * _Gy % _P)))
    r = int.from_bytes(_sha512(prefix + message), "little") % _L
    R = _encode_point(_mul(r, (_Gx, _Gy, 1, _Gx * _Gy % _P)))
    k = int.from_bytes(_sha512(R + pub + message), "little") % _L
    S = (r + k * a) % _L
    return R + S.to_bytes(32, "little")


def verify(pub, message, sig):
    try:
        if len(sig) != 64 or len(pub) != 32:
            return False
        A = _decode_point(pub)
        if A is None:
            return False
        R = _decode_point(sig[:32])
        if R is None:
            return False
        S = int.from_bytes(sig[32:], "little")
        if S >= _L:
            return False
        k = int.from_bytes(_sha512(sig[:32] + pub + message),
                           "little") % _L
        left = _mul(S, (_Gx, _Gy, 1, _Gx * _Gy % _P))
        right = _add(R, _mul(k, A))
        return _point_equal(left, right)
    except Exception:
        return False


# ================== colorspec ==================

"""Colour specs: the one vocabulary for "which colour does the user mean".

Shared leaf module, no dependencies beyond the standard library.

WHY IT IS SEPARATE
------------------
`config.py` needs to answer one question - "does this accent spec name a
real colour?" - so that accent_only / auto_click can warn when they are
switched on with nothing to aim at. That question used to be answered by
importing `vision`, which pulled numpy and the whole analysis engine into
the settings layer: config.py stopped being pure-Python and independently
testable, and `hotkeys.py`'s "cross-platform and unit-testable" claim was
the only one left standing.

So the parsing vocabulary lives here. `vision.py` imports it and keeps its
historical private aliases (`_parse_accent`, `_parse_accents`) so every
existing call site, and every saved config file, keeps working unchanged.

Accepted forms, for accent_color and exclude_color alike:
    "off" / "none" / "disabled" / "false" / ""   -> no colour
    "blue"                                        -> a preset name
    "60,120,220"                                  -> r,g,b 0-255
    "blue; red; 60,120,220"                       -> several, ";"-separated
    [60, 120, 220]                                -> one JSON triple
    [[60, 120, 220], "red"]                       -> several, JSON list
"""

ACCENT_PRESETS = {
    "red":    (0.0, 210.0, 190.0),
    "orange": (15.0, 215.0, 215.0),
    "yellow": (30.0, 215.0, 215.0),
    "green":  (60.0, 210.0, 180.0),
    "cyan":   (90.0, 210.0, 200.0),
    "blue":   (120.0, 215.0, 200.0),
    "purple": (145.0, 190.0, 180.0),
    "pink":   (168.0, 150.0, 215.0),
    "lime":   (45.0, 190.0, 200.0),
    "teal":   (85.0, 220.0, 175.0),
    "indigo": (130.0, 210.0, 175.0),
    "brown":  (12.0, 165.0, 140.0),
    "grey":   (0.0, 0.0, 150.0),
    "gray":   (0.0, 0.0, 150.0),
    "white":  (0.0, 0.0, 235.0),
    "black":  (0.0, 0.0, 30.0),
}


def parse_accent(spec):
    if spec is None:
        return None

    # Handle numpy arrays and other sequence types
    if hasattr(spec, '__iter__') and not isinstance(spec, (str, bytes)):
        try:
            spec = list(spec)
        except (TypeError, ValueError):
            return None

    if isinstance(spec, str):
        preset = ACCENT_PRESETS.get(spec.strip().lower())
        if preset is not None:
            return preset
        # Parse "r,g,b" format
        try:
            parts = [int(p.strip()) for p in spec.split(",")]
        except (ValueError, OverflowError):
            return None
    elif isinstance(spec, (list, tuple)):
        try:
            if any(isinstance(p, bool) for p in spec):
                return None
            def _to_int(p):
                if isinstance(p, int):
                    return p
                if isinstance(p, float):
                    return int(p) if p.is_integer() else None
                if isinstance(p, str):
                    f = float(p)
                    return int(f) if f.is_integer() else None
                return None
            parts = [_to_int(p) for p in spec]
            if any(p is None for p in parts):
                return None
        except (TypeError, ValueError, OverflowError):
            return None
    else:
        return None
    if len(parts) != 3:
        return None
    for p in parts:
        if p < 0 or p > 255:
            return None
    r, g, b = parts
    rf, gf, bf = r / 255.0, g / 255.0, b / 255.0

    mx = max(rf, gf, bf)
    mn = min(rf, gf, bf)
    d = mx - mn
    v = mx
    s = 0.0 if mx == 0 else d / mx

    if d == 0:
        h = 0.0
    elif mx == rf:
        h = ((gf - bf) / d) % 6.0
    elif mx == gf:
        h = ((bf - rf) / d + 2.0) % 6.0
    else:
        h = ((rf - gf) / d + 4.0) % 6.0

    return (min(h * 30.0, 179.0), s * 255.0, v * 255.0)


def parse_accents(spec):
    if spec is None:
        return []

    items = spec

    # Handle numpy arrays and other sequence types (but not strings)
    if hasattr(items, '__iter__') and not isinstance(items, (str, bytes)):
        try:
            items = list(items)
        except (TypeError, ValueError):
            return []

    if isinstance(items, str):
        s = items.strip()
        if not s or s.lower() in ("off", "none", "disabled", "false"):
            return []
        items = [t.strip() for t in s.split(";") if t.strip()]
        from_string = True
    else:
        from_string = False

    if not isinstance(items, (list, tuple)):
        items = [items]

    if (not from_string and len(items) == 3
            and all(isinstance(p, (int, float, str)) and not isinstance(p, bool)
                    for p in items)):
        all_presets = all(isinstance(p, str)
                          and p.strip().lower() in ACCENT_PRESETS
                          for p in items)
        if not all_presets:
            items = [items]

    out = []
    for item in items:
        if isinstance(item, str):
            preset = ACCENT_PRESETS.get(item.strip().lower())
            if preset is not None:
                out.append(preset)
                continue

        hsv = parse_accent(item)
        if hsv is not None:
            out.append(hsv)

    return out


def hsv_to_rgb(h, s, v):
    h = max(0.0, min(359.999, float(h) * 2.0))
    s = max(0.0, min(1.0, float(s) / 255.0))
    v = max(0.0, min(1.0, float(v) / 255.0))
    c = v * s
    x = c * (1.0 - abs((h / 60.0) % 2.0 - 1.0))
    m = v - c
    if h < 60:
        r, g, b = c, x, 0.0
    elif h < 120:
        r, g, b = x, c, 0.0
    elif h < 180:
        r, g, b = 0.0, c, x
    elif h < 240:
        r, g, b = 0.0, x, c
    elif h < 300:
        r, g, b = x, 0.0, c
    else:
        r, g, b = c, 0.0, x
    return (max(0, min(255, int(round((r + m) * 255.0)))),
            max(0, min(255, int(round((g + m) * 255.0)))),
            max(0, min(255, int(round((b + m) * 255.0)))))


def _parse_accent(spec):
    return parse_accent(spec)


def _parse_accents(spec):
    return parse_accents(spec)


_ACCENT_PRESETS = ACCENT_PRESETS


# ================== app core: constants / logging / i18n / inventory ==================

APP_NAME = "SnappyAI"
APP_VERSION = "9.0"
IS_WINDOWS = sys.platform == "win32"

_LOG_LOCK = threading.Lock()
_LOG_FILE = None
_LOG_PATH = None


def attach_log_file(path):
    global _LOG_FILE, _LOG_PATH
    with _LOG_LOCK:
        try:
            folder = os.path.dirname(str(path))
            if folder:
                os.makedirs(folder, exist_ok=True)
            fh = open(str(path), "a", encoding="utf-8", errors="replace")
        except OSError:
            return None
        if _LOG_FILE is not None:
            try:
                _LOG_FILE.close()
            except OSError:
                pass
        _LOG_FILE = fh
        _LOG_PATH = str(path)
        try:
            import time
            fh.write("\n==== %s %s started %s (python %s) ====\n"
                     % (APP_NAME, APP_VERSION,
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        sys.version.split()[0]))
            fh.flush()
        except OSError:
            pass
        return _LOG_PATH


def log_path():
    return _LOG_PATH


def log(message):
    text = str(message)
    try:
        print(text, flush=True)
    except Exception:
        pass
    with _LOG_LOCK:
        if _LOG_FILE is not None:
            try:
                import time
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                for line in text.splitlines() or [""]:
                    _LOG_FILE.write("[%s] %s\n" % (stamp, line))
                _LOG_FILE.flush()
            except (OSError, ValueError):
                pass


_LANG = "en"
_CATALOGS: dict[str, dict[str, str]] = {"en": {}}
_I18N_LOCK = threading.Lock()


def publish(lang, mapping):
    with _I18N_LOCK:
        cat = _CATALOGS.setdefault(str(lang), {})
        cat.update({str(k): str(v) for k, v in dict(mapping).items()})


def set_language(lang):
    global _LANG
    with _I18N_LOCK:
        _LANG = str(lang or "en")


def get_language():
    return _LANG


def available_languages():
    with _I18N_LOCK:
        return sorted(_CATALOGS)


_LANG_CODES = ("en",)


def codes():
    return _LANG_CODES


def L(key, *args):
    with _I18N_LOCK:
        text = _CATALOGS.get(_LANG, {}).get(key)
        if text is None:
            text = _CATALOGS.get("en", {}).get(key)
    if text is None:
        text = key
    if args:
        try:
            return text % args
        except (TypeError, ValueError):
            return text
    return text


@dataclass
class CPU:
    name: str = ""
    logical_processors: int = 0
    l3_domains: int = 1
    has_p_and_e_cores: bool = False


@dataclass
class GPU:
    name: str = ""
    vendor: Vendor = Vendor.OTHER
    driver_version: str = ""
    hags: bool | None = None


@dataclass
class NIC:
    interface_description: str = ""
    name: str = ""
    physical: str = ""
    status: str = ""
    mtu: int = 0
    allow_turn_off: bool | None = None
    interrupt_moderation: bool | None = None


@dataclass
class DNS:
    server_addresses: list[str] = field(default_factory=list)
    resolve_ms: float | None = None


@dataclass
class Power:
    active_scheme_name: str = ""
    active_scheme_guid: str = ""
    on_battery: bool = False


@dataclass
class Timer:
    current_period_us: float | None = None
    min_period_us: float | None = None
    max_period_us: float | None = None
    global_timer_resolution_requests: int = -1
    mmcss_responsiveness: str = ""


@dataclass
class Display:
    name: str = ""
    resolution: str = ""
    current_refresh_hz: float | None = None
    max_refresh_hz: float | None = None

    @property
    def refresh_deficit_hz(self) -> float:
        if self.current_refresh_hz is None or self.max_refresh_hz is None:
            return 0.0
        return max(0.0, float(self.max_refresh_hz) - float(self.current_refresh_hz))


_WIFI_MARKERS = ("wi-fi", "wifi", "wireless", "wlan", "802.11")

# Discrete-GPU naming markers, for hybrid-graphics machines where the game
# renders on the dGPU while the iGPU often drives the panel (and is what WMI
# lists first). Vendor covers NVIDIA (only discrete in practice); AMD/Intel
# integrated parts keep their generic names ("Radeon(TM) Graphics",
# "UHD/Iris Graphics") while discrete families carry these markers.
_DISCRETE_GPU_MARKERS = (
    "geforce", "quadro", "tesla", "rtx", "gtx",
    "radeon rx", "rx vega", "radeon pro", "firepro", "vega 56", "vega 64",
    "arc a", "arc b", "arc ",
)


def _gpu_discrete_score(gpu: "GPU") -> int:
    # WMI names carry "(R)"/"(TM)" noise ("Radeon (TM) RX 480", "Arc(TM) A770")
    name = (gpu.name or "").lower().replace("(r)", " ").replace("(tm)", " ")
    if gpu.vendor is Vendor.NVIDIA:
        return 2
    if any(mark in name for mark in _DISCRETE_GPU_MARKERS):
        return 2
    if gpu.vendor in (Vendor.AMD, Vendor.INTEL):
        return 0
    return 1


@dataclass
class Inventory:
    os_name: str = ""
    os_family: OSFamily = OSFamily.OTHER
    windows_build: int = 0
    is_elevated: bool = False
    nics: list[NIC] = field(default_factory=list)
    gpus: list[GPU] = field(default_factory=list)
    displays: list[Display] = field(default_factory=list)
    cpu: CPU = field(default_factory=CPU)
    power: Power = field(default_factory=Power)
    dns: DNS = field(default_factory=DNS)
    timer: Timer = field(default_factory=Timer)
    warnings: list[str] = field(default_factory=list)
    running_anti_cheat: tuple[str, ...] = ()
    top_cpu_processes: list[tuple[str, float]] = field(default_factory=list)
    foreground_process: str = ""
    bloat: Any = None
    display_version: str = ""
    supports_update_policy: bool = False
    domain_joined: bool = False
    game_dvr_app_capture: int = 0
    game_dvr_enabled: int = 0
    usb_selective_suspend: bool | None = None

    @property
    def primary_nic(self) -> NIC | None:
        for nic in self.nics:
            if str(nic.status).lower() in ("up", "connected"):
                return nic
        return self.nics[0] if self.nics else None

    @property
    def primary_gpu(self) -> GPU | None:
        # On hybrid-graphics machines the game renders on the discrete GPU
        # even when the integrated one is listed first (and often drives the
        # panel), so "primary" for latency purposes means "the discrete one
        # when there is one". max() keeps the first among equals.
        if not self.gpus:
            return None
        return max(self.gpus, key=_gpu_discrete_score)

    @property
    def primary_display(self) -> Display | None:
        return self.displays[0] if self.displays else None

    @property
    def uses_wifi_for_primary(self) -> bool:
        nic = self.primary_nic
        if nic is None:
            return False
        blob = " ".join((str(nic.physical), str(nic.name),
                         str(nic.interface_description))).lower()
        return any(mark in blob for mark in _WIFI_MARKERS)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["os_family"] = self.os_family.value
        for gpu in data.get("gpus", []):
            if isinstance(gpu.get("vendor"), Vendor):
                gpu["vendor"] = gpu["vendor"].value
        data["primary_nic"] = (
            asdict(pn) if (pn := self.primary_nic) is not None else None)
        return data


# ================== safety policy ==================


_CONFIDENCE_RANK = {
    Confidence.HIGH: 3,
    Confidence.MEDIUM: 2,
    Confidence.LOW: 1,
    Confidence.UNVERIFIED: 0,
}


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""


@dataclass
class SafetyPolicy:
    min_confidence: Confidence = Confidence.LOW
    allow_experimental: bool = False

    def evaluate(
        self,
        tweak: Any,
        inventory: Any,
        detected: tuple[str, ...] = (),
    ) -> PolicyDecision:
        _ = inventory
        if detected and not tweak.anti_cheat_safe:
            return PolicyDecision(
                allowed=False,
                reason=(
                    f"anti-cheat runtime detected ({', '.join(detected[:4])}); "
                    f"tweak touches processes or interrupt routing"
                ),
            )
        if tweak.experimental and not self.allow_experimental:
            return PolicyDecision(allowed=False, reason="experimental tweaks not enabled")
        if tweak.is_refused:
            return PolicyDecision(
                allowed=False, reason=tweak.refusal_reason or "refused tweak"
            )
        if _CONFIDENCE_RANK[tweak.confidence] < _CONFIDENCE_RANK[self.min_confidence]:
            return PolicyDecision(
                allowed=False,
                reason=(
                    f"confidence {tweak.confidence.value} below minimum "
                    f"{self.min_confidence.value}"
                ),
            )
        return PolicyDecision(allowed=True, reason="")

    def check_plan(self, tweak: Any, steps: list) -> PolicyDecision:
        _ = tweak
        from models import StepKind

        banned = ("mimikatz", "invoke-mimikatz", "bypass amsi", "amsiutils")
        for step in steps:
            if step.is_manual:
                continue
            cmd = (step.command or "").lower()
            for token in banned:
                if token in cmd:
                    return PolicyDecision(
                        allowed=False, reason=f"plan contains banned token {token!r}"
                    )
            if "set-service" in cmd:
                return PolicyDecision(
                    allowed=False,
                    reason="Set-Service changes a start type; that is disabling, "
                    "not restarting",
                )
            if "stop-service" in cmd:
                declared = (step.restarts_service or "").lower()
                flat = cmd.replace(" ", "").replace("\t", "")
                svc = declared.replace(" ", "")
                stop_names = (
                    f"stop-service-name{svc}" in flat
                    or f"stop-service{svc}" in flat
                )
                start_names = (
                    f"start-service-name{svc}" in flat
                    or f"start-service{svc}" in flat
                    or f"restart-service-name{svc}" in flat
                    or f"restart-service{svc}" in flat
                )
                if stop_names and start_names:
                    continue
                return PolicyDecision(
                    allowed=False,
                    reason="Stop-Service without a declared and performed restart "
                    "is stopping, not restarting",
                )
            if step.kind == StepKind.BCDEDIT and not self.allow_experimental:
                return PolicyDecision(
                    allowed=False, reason="boot configuration edits need opt-in"
                )
        return PolicyDecision(allowed=True, reason="")


class AntiCheatPolicy:
    _NAMES = (
        "vgc",
        "vgk",
        "easyanticheat",
        "eac",
        "battleye",
        "bedaisy",
        "faceit",
    )

    def detect(self, inventory: Any) -> tuple[str, ...]:
        names = [str(p).lower() for p in getattr(inventory, "top_cpu_processes", [])]
        names.append(str(getattr(inventory, "foreground_process", "") or "").lower())
        found = [n for n in names for mark in self._NAMES if mark and mark in n]
        seen: list[str] = []
        for n in found:
            if n not in seen:
                seen.append(n)
        return tuple(seen)


# ================== timer resolution runtime ==================

"""Raising the system clock interrupt frequency from this process.

WHY A RESIDENT HOLDER EXISTS
----------------------------
NtSetTimerResolution is a *request* owned by the calling process: the kernel
drops it the moment that process exits, so a setter that returns and lets the
caller carry on does nothing. request_timer_resolution() therefore parks a
daemon thread on an event for the lifetime of the request. That is the same
shape as SetTimerResolution.exe sleeping forever, minus the second process.

Two Windows behaviours decide whether the request survives contact with a real
desktop:
- Windows 11 ignores the timer request of a window-owning process that is
  minimized, fully occluded or otherwise not visible, unless the process
  explicitly clears PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION. This app
  is a window-owning GUI that people park in the tray while gaming, so the
  opt-out is applied before every request. It is best-effort: the flag is
  newer than Windows 10 and can come back ERROR_INVALID_PARAMETER.
- The request is clamped to the finest period the platform reports, so 0.5 ms
  is a target, never a promise. Callers get the achieved value back.
"""

_TIMER_LOCK = threading.Lock()
_TIMER_STOP: threading.Event | None = None
_TIMER_THREAD: threading.Thread | None = None
_TIMER_STATE: dict[str, Any] = {}

TIMER_TARGET_US = 500.0

_POWER_THROTTLING_CURRENT_VERSION = 1
_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4


@dataclass
class TimerResolution:
    min_us: float | None = None
    max_us: float | None = None
    current_us: float | None = None


def _ntdll():
    import ctypes

    ntdll = ctypes.windll.ntdll
    if not getattr(ntdll, "_snappy_timer_typed", False):
        ntdll.NtQueryTimerResolution.argtypes = [
            ctypes.POINTER(ctypes.c_ulong)] * 3
        ntdll.NtQueryTimerResolution.restype = ctypes.c_long
        ntdll.NtSetTimerResolution.argtypes = [
            ctypes.c_ulong, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_ulong)]
        ntdll.NtSetTimerResolution.restype = ctypes.c_long
        ntdll._snappy_timer_typed = True
    return ntdll


def _clear_timer_power_throttling() -> dict[str, Any]:
    """Ask Windows to always honour this process's timer request.

    ControlMask selects the mechanism, StateMask=0 turns it off, which is the
    "always honour" direction documented for SetProcessInformation. Resolved
    through kernel32 by name so an older Windows degrades to a reported no-op
    instead of an AttributeError.
    """
    import ctypes

    out: dict[str, Any] = {"attempted": False, "ok": False, "error": ""}
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            spi = kernel32.SetProcessInformation
        except AttributeError:
            out["error"] = "SetProcessInformation unavailable"
            return out
        spi.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                        ctypes.c_ulong]
        spi.restype = ctypes.c_int

        class _Throttling(ctypes.Structure):
            _fields_ = [
                ("Version", ctypes.c_ulong),
                ("ControlMask", ctypes.c_ulong),
                ("StateMask", ctypes.c_ulong),
            ]

        state = _Throttling()
        state.Version = _POWER_THROTTLING_CURRENT_VERSION
        state.ControlMask = _POWER_THROTTLING_IGNORE_TIMER_RESOLUTION
        state.StateMask = 0
        out["attempted"] = True
        current = ctypes.c_void_p(-1)
        if spi(current, 4, ctypes.byref(state), ctypes.sizeof(state)):
            out["ok"] = True
        else:
            getter = getattr(ctypes, "get_last_error", None)
            code = getter() if getter else 0
            out["error"] = f"SetProcessInformation failed (Win32 error {code})"
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def query_timer_resolution() -> TimerResolution:
    """Finest, coarsest and current system tick, in microseconds."""
    if not IS_WINDOWS:
        return TimerResolution()
    import ctypes

    try:
        ntdll = _ntdll()
        lo = ctypes.c_ulong(0)
        hi = ctypes.c_ulong(0)
        cur = ctypes.c_ulong(0)
        status = ntdll.NtQueryTimerResolution(
            ctypes.byref(lo), ctypes.byref(hi), ctypes.byref(cur))
        if status != 0:
            return TimerResolution()

        def us(v: int) -> float | None:
            return v / 10.0 if v else None

        return TimerResolution(
            min_us=us(lo.value), max_us=us(hi.value), current_us=us(cur.value))
    except Exception as exc:
        log(f"timer resolution query failed: {exc}")
        return TimerResolution()


def clamp_timer_target(want_us: float, res: TimerResolution) -> int:
    """The 100 ns unit count to actually ask for, clamped to the platform.

    NtSetTimerResolution rejects or ignores a request finer than the reported
    minimum, so asking for 500 us on a machine whose floor is 1 ms would fail
    where 1 ms would have succeeded.
    """
    want = int(round(float(want_us) * 10.0))
    if want <= 0:
        want = int(round(TIMER_TARGET_US * 10.0))
    if res.min_us:
        want = max(want, int(round(res.min_us * 10.0)))
    if res.max_us:
        want = min(want, int(round(res.max_us * 10.0)))
    return max(1, want)


def request_timer_resolution(
    target_us: float = TIMER_TARGET_US, *, power_throttling_optout: bool = True,
) -> dict[str, Any]:
    """Hold a finer system tick for as long as this process lives."""
    global _TIMER_STOP, _TIMER_THREAD, _TIMER_STATE
    if not IS_WINDOWS:
        return {"ok": False, "supported": False,
                "error": "timer resolution requests require Windows"}
    with _TIMER_LOCK:
        if _TIMER_THREAD is not None and _TIMER_THREAD.is_alive():
            prior = dict(_TIMER_STATE)
            prior["already_active"] = True
            return prior
        res = query_timer_resolution()
        throttling = (
            _clear_timer_power_throttling() if power_throttling_optout
            else {"attempted": False, "ok": False, "error": "skipped"}
        )
        units = clamp_timer_target(target_us, res)
        import ctypes

        out: dict[str, Any] = {
            "ok": False, "supported": True, "requested_us": units / 10.0,
            "target_us": float(target_us), "clamped": units / 10.0 != float(target_us),
            "throttling": throttling, "error": "",
        }
        try:
            ntdll = _ntdll()
            cur = ctypes.c_ulong(0)
            status = ntdll.NtSetTimerResolution(units, 1, ctypes.byref(cur))
            if status != 0:
                out["error"] = f"NtSetTimerResolution returned 0x{status & 0xFFFFFFFF:08X}"
                _TIMER_STATE = out
                return out
            out["ok"] = True
            out["actual_us"] = cur.value / 10.0 if cur.value else None
            out["min_us"] = res.min_us
            out["max_us"] = res.max_us
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
            _TIMER_STATE = out
            return out
        stop = threading.Event()
        thread = threading.Thread(
            target=stop.wait, args=(None,), name="timer-resolution-holder",
            daemon=True,
        )
        thread.start()
        _TIMER_STOP = stop
        _TIMER_THREAD = thread
        _TIMER_STATE = out
        log(
            "timer resolution request held at %.4f ms%s"
            % (
                (out.get("actual_us") or 0.0) / 1000.0,
                "" if throttling.get("ok") else
                " (power-throttling opt-out unavailable: %s)"
                % (throttling.get("error") or "unknown"),
            )
        )
        return dict(out)


def stop_timer_request() -> dict[str, Any]:
    """Release the request.

    The request is owned by the PROCESS, not by the holder thread: ending
    the thread alone leaves the fine tick in force until the app exits.
    Releasing therefore replaces this process's request with the coarsest
    period the platform reports (the last request per process wins), then
    stops the holder.
    """
    global _TIMER_STOP, _TIMER_THREAD, _TIMER_STATE
    with _TIMER_LOCK:
        stop, thread = _TIMER_STOP, _TIMER_THREAD
        _TIMER_STOP = None
        _TIMER_THREAD = None
        _TIMER_STATE = {}
    if stop is not None:
        stop.set()
    if thread is not None:
        thread.join(timeout=2.0)
    if stop is None:
        return {"ok": False, "was_active": False}
    released = False
    superseded = False
    if IS_WINDOWS:
        try:
            import ctypes
            ntdll = _ntdll()
            with _TIMER_LOCK:
                # A request that raced in between this stop and its own
                # fine NtSetTimerResolution now owns the tick - do not
                # stomp it back to coarse.
                if _TIMER_THREAD is not None and _TIMER_THREAD.is_alive():
                    superseded = True
                else:
                    res = query_timer_resolution()
                    want = clamp_timer_target(res.max_us or 15625.0, res)
                    cur = ctypes.c_ulong(0)
                    status = ntdll.NtSetTimerResolution(
                        want, 1, ctypes.byref(cur))
                    released = status == 0
        except Exception as exc:
            log("timer release call failed: %r" % (exc,))
    after = query_timer_resolution()
    if superseded:
        log("timer release skipped: a newer request is active; tick is %s"
            % ("%.4f ms" % (after.current_us / 1000.0)
               if after.current_us else "unknown"))
    else:
        log("timer resolution request released%s; tick is now %s"
            % ("" if released else
               " (coarse re-request failed - the fine tick may persist"
               " until this process exits)",
               "%.4f ms" % (after.current_us / 1000.0)
               if after.current_us else "unknown"))
    return {"ok": True, "was_active": True, "released": released,
            "superseded": superseded, "current_us": after.current_us}


def timer_request_active() -> bool:
    return _TIMER_THREAD is not None and _TIMER_THREAD.is_alive()


def timer_request_state() -> dict[str, Any]:
    with _TIMER_LOCK:
        out = dict(_TIMER_STATE)
    out["active"] = timer_request_active()
    res = query_timer_resolution()
    out["current_us"] = res.current_us
    out["min_us"] = res.min_us
    out["max_us"] = res.max_us
    return out
