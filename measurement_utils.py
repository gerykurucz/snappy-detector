# ===== merged from bufferbloat.py =====
from __future__ import annotations

import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class LoadController:
    direction: str
    url: str = ""
    concurrency: int = 3
    upload_bytes: int = 1024 * 1024

    @property
    def configured(self) -> bool:
        return bool(self.url.strip())


@dataclass
class BufferbloatResult:
    target: str
    idle: dict[str, Any] = field(default_factory=dict)
    download: dict[str, Any] = field(default_factory=dict)
    upload: dict[str, Any] = field(default_factory=dict)
    grade: str = "?"
    explanation: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def tested_any_load(self) -> bool:
        return bool(self.download or self.upload)

    @property
    def increase_p95_ms(self) -> float | None:
        base = (self.idle or {}).get("p95")
        vals = [
            (self.download or {}).get("p95"),
            (self.upload or {}).get("p95"),
        ]
        vals = [v for v in vals if v is not None]
        if base is None or not vals:
            return None
        return max(vals) - base

    @property
    def increase_p99_ms(self) -> float | None:
        base = (self.idle or {}).get("p99")
        vals = [
            (self.download or {}).get("p99"),
            (self.upload or {}).get("p99"),
        ]
        vals = [v for v in vals if v is not None]
        if base is None or not vals:
            return None
        return max(vals) - base

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "idle": self.idle,
            "download": self.download,
            "upload": self.upload,
            "grade": self.grade,
            "explanation": self.explanation,
            "notes": self.notes,
            "increase_p95_ms": self.increase_p95_ms,
            "increase_p99_ms": self.increase_p99_ms,
            "tested_any_load": self.tested_any_load,
        }

    def text(self) -> str:
        lines = [f"bufferbloat grade {self.grade.upper()} against {self.target}"]
        for phase in ("idle", "download", "upload"):
            d = getattr(self, phase)
            if not d:
                lines.append(f"  {phase:<9} NOT TESTED")
            else:
                lines.append(
                    f"  {phase:<9} p50={d.get('p50')} p95={d.get('p95')} "
                    f"max={d.get('max')} ms loss={d.get('loss_pct')}%"
                )
        lines.append(f"  loaded p95 increase: {self.increase_p95_ms} ms")
        lines.append(f"  {self.explanation}")
        return "\n".join(lines)


def _grade(increase: float | None) -> tuple[str, str]:
    if increase is None:
        return ("?", "only the idle line was measured")
    if increase < 30.0:
        return ("A", "queues stay shallow under load; the link is healthy")
    if increase < 100.0:
        return ("B", "mild queue growth under load; SQM would still help")
    if increase < 250.0:
        return ("C", "the queue grows badly under load; enable SQM")
    if increase < 500.0:
        return ("D", "severe bufferbloat; gaming under load will stutter")
    return ("F", "extreme bufferbloat; the loaded link is barely usable")


class _Load:
    def __init__(self, ctl: LoadController) -> None:
        self.ctl = ctl
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []

    def _download(self) -> None:
        while not self.stop.is_set():
            try:
                with urllib.request.urlopen(self.ctl.url, timeout=10) as res:
                    while not self.stop.is_set():
                        chunk = res.read(256 * 1024)
                        if not chunk:
                            break
            except Exception:
                self.stop.wait(0.5)

    def _upload(self) -> None:
        try:
            n = int(self.ctl.upload_bytes)
        except (TypeError, ValueError):
            n = 1024 * 1024
        payload = b"x" * max(1, min(n, 64 * 1024 * 1024))
        while not self.stop.is_set():
            try:
                req = urllib.request.Request(
                    self.ctl.url, data=payload, method="POST"
                )
                with urllib.request.urlopen(req, timeout=10):
                    pass
            except Exception:
                self.stop.wait(0.5)

    def start(self) -> None:
        fn = self._download if self.ctl.direction == "download" else self._upload
        for _ in range(max(1, self.ctl.concurrency)):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self.threads.append(t)

    def halt(self) -> None:
        self.stop.set()
        for t in self.threads:
            t.join(timeout=2.0)


def run_bufferbloat_test(
    target: str,
    samples_per_phase: int = 60,
    interval: float = 0.02,
    transport: str = "auto",
    port: int = 443,
    download: LoadController | None = None,
    upload: LoadController | None = None,
    settle_seconds: float = 3.0,
    on_phase: Callable[[str, str], None] | None = None,
) -> BufferbloatResult:
    def note(phase: str, msg: str) -> None:
        if on_phase is not None:
            on_phase(phase, msg)

    result = BufferbloatResult(target=target)
    note("idle", f"probing {target} without load")
    idle = probe(
        target, count=samples_per_phase, interval=interval, transport=transport,
        port=port,
    ).distribution(target)
    result.idle = idle.as_dict(include_samples=True)

    phases: list[tuple[str, LoadController | None]] = [
        ("download", download),
        ("upload", upload),
    ]
    for phase, ctl in phases:
        if ctl is None or not ctl.configured:
            note(phase, "no load endpoint configured; skipping")
            continue
        note(phase, f"saturating {phase} for {settle_seconds:g}s to fill queues")
        load = _Load(ctl)
        load.start()
        time.sleep(settle_seconds)
        try:
            note(phase, f"probing {target} under {phase} load")
            dist = probe(
                target, count=samples_per_phase, interval=interval,
                transport=transport, port=port,
            ).distribution(target)
        finally:
            load.halt()
        if phase == "download":
            result.download = dist.as_dict(include_samples=True)
        else:
            result.upload = dist.as_dict(include_samples=True)

    grade, explanation = _grade(result.increase_p95_ms)
    result.grade = grade
    result.explanation = explanation
    if not result.tested_any_load:
        result.notes.append(
            "No load endpoint was configured, so only the idle line was measured. "
            "Pass a download URL (and ideally an upload URL) to test the term that "
            "usually matters most."
        )
    return result


# ===== merged from measure_utils.py =====
# ===== merged from chain.py =====

from typing import Any

ASSUMPTIONS = (
    "input one-way is half the hook round-trip (symmetric dispatch assumed); "
    "render is present-to-vblank on a blank test surface, not game engine time; "
    "network is ICMP/TCP ping p95, not game-protocol latency; panel scan-out and "
    "pixel response are excluded and need a camera to observe"
)


def build_chain(
    input_res: dict[str, Any], render_res: dict[str, Any],
    net: dict[str, Any],
) -> dict[str, Any]:
    legs: dict[str, Any] = {
        "input_ms": input_res.get("p50_one_way_ms"),
        "render_ms": render_res.get("p95_ms"),
        "network_ms": net.get("p95"),
    }
    missing = [k for k, v in legs.items() if v is None]
    total = None
    if not missing:
        total = legs["input_ms"] + legs["render_ms"] + legs["network_ms"]
    return {
        "legs": legs,
        "missing": missing,
        "total_ms": total,
        "assumptions": ASSUMPTIONS,
    }


# ===== merged from collect.py =====

from dataclasses import dataclass
from typing import Any, Callable

from system_core import Inventory


@dataclass
class CollectOptions:
    benchmark_dns: bool = True
    detect_processes: bool = True
    detect_anti_cheat: bool = True


def collect_inventory(
    backend: Any,
    opts: CollectOptions,
    on_progress: Callable[[str], None] | None = None,
) -> Inventory:
    def note(msg: str) -> None:
        if on_progress is not None:
            on_progress(msg)

    fixture = backend.fixture_inventory()
    if fixture is not None:
        note("fixture inventory")
        return fixture
    note("system")
    inv = backend.read_system()
    note("network")
    backend.read_network(inv)
    note("display")
    backend.read_display(inv)
    if opts.benchmark_dns:
        note("dns")
        backend.read_dns(inv)
    if opts.detect_processes:
        note("processes")
        backend.read_processes(inv)
    if opts.detect_anti_cheat:
        note("anti-cheat")
        backend.read_anti_cheat(inv)
    return inv


# ===== merged from probe.py =====

import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from system_core import SnappyAIError
from general_utils import console_encoding, silent_kwargs


class ProbeUnavailableError(SnappyAIError):
    pass


@dataclass(slots=True)
class Sample:
    rtt_ms: float | None = None


@dataclass
class ProbeResult:
    samples: list[Sample] = field(default_factory=list)
    transport: str = ""
    target: str = ""

    def distribution(self, target: str = "") -> Distribution:
        vals = [s.rtt_ms for s in self.samples if s.rtt_ms is not None]
        lost = sum(1 for s in self.samples if s.rtt_ms is None)
        return Distribution(
            samples=vals, lost=lost, sent=len(self.samples),
            label=self.transport or target,
        )

    @property
    def reordering_or_loss_bursts(self) -> list[dict[str, Any]]:
        bursts: list[dict[str, Any]] = []
        run = 0
        for i, s in enumerate(self.samples):
            if s.rtt_ms is None:
                run += 1
            elif run:
                bursts.append({"at": i - run, "lost": run})
                run = 0
        if run:
            bursts.append({"at": len(self.samples) - run, "lost": run})
        return bursts


def _parse_ping_ms(stdout: str) -> float | None:
    """Locale-proof RTT extraction from one ping reply.

    English "time=35ms"/"time<1ms", Hungarian "idő=35ms", German "Zeit=35ms":
    every Windows locale keeps the "=<NUMBER> ms" shape, and some locales use
    a decimal comma. The first match is the reply line (statistics follow)."""
    m = re.search(r"[=<>]\s*(\d+(?:[.,]\d+)?)\s*ms", stdout or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _ping_once(target: str, timeout: float) -> float | None:
    ms = int(max(100, min(4000, timeout * 1000.0)))
    if sys.platform.startswith("win"):
        cmd = ["ping", "-n", "1", "-w", str(ms), target]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout))), target]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding=console_encoding(), errors="replace",
            timeout=timeout + 2.0,
            **silent_kwargs(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return _parse_ping_ms(proc.stdout)


def _tcp_once(target: str, port: int, timeout: float) -> float | None:
    try:
        infos = socket.getaddrinfo(target, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    for fam, _st, _pr, _cn, addr in infos:
        if fam not in (socket.AF_INET, socket.AF_INET6):
            continue
        try:
            sock = socket.socket(fam, socket.SOCK_STREAM)
        except OSError:
            continue
        sock.settimeout(timeout)
        start = time.perf_counter()
        try:
            sock.connect(addr)
        except OSError:
            try:
                sock.close()
            except OSError:
                pass
            continue
        rtt = (time.perf_counter() - start) * 1000.0
        try:
            sock.close()
        except OSError:
            pass
        return rtt
    return None


def probe(
    target: str,
    count: int = 4,
    interval: float = 0.02,
    transport: str = "auto",
    port: int = 443,
    timeout: float = 2.0,
) -> ProbeResult:
    if not target:
        raise ProbeUnavailableError("no target to probe")
    use_tcp = transport == "tcp"
    use_icmp = transport == "icmp"
    samples: list[Sample] = []
    chosen = transport
    for i in range(max(1, count)):
        if use_tcp or (transport == "auto" and i > 0 and not any(s.rtt_ms is not None for s in samples)):
            rtt = _tcp_once(target, port, timeout)
            if rtt is not None:
                chosen = "tcp"
            else:
                rtt = _ping_once(target, timeout)
                if rtt is not None:
                    chosen = "icmp" if chosen == "auto" else "tcp+icmp"
        elif use_icmp or transport == "auto":
            rtt = _ping_once(target, timeout)
            if rtt is not None and chosen == "auto":
                chosen = "icmp"
        else:
            rtt = _tcp_once(target, port, timeout)
        samples.append(Sample(rtt_ms=rtt))
        if interval and i < count - 1:
            time.sleep(interval)
    if chosen == "auto":
        chosen = "icmp" if any(s.rtt_ms is not None for s in samples) else "unreachable"
    return ProbeResult(samples=samples, transport=chosen, target=target)


def measure(
    target: str,
    count: int = 200,
    interval: float = 0.02,
    transport: str = "auto",
    port: int = 443,
    timeout: float = 2.0,
) -> Distribution:
    return probe(
        target, count=count, interval=interval, transport=transport, port=port,
        timeout=timeout,
    ).distribution(target)


# ===== merged from fake_host.py =====

from system_core import OSFamily, Vendor
from system_core import CPU, DNS, NIC, Display, GPU, Inventory, Power, Timer


def build_fake_inventory() -> Inventory:
    return Inventory(
        os_name="Microsoft Windows 11 Pro",
        os_family=OSFamily.WINDOWS,
        windows_build=22631,
        is_elevated=False,
        nics=[
            NIC(
                interface_description="Intel(R) Wi-Fi 6 AX201 160MHz",
                name="Wi-Fi",
                physical="wireless",
                status="up",
                mtu=1500,
                allow_turn_off=True,
                interrupt_moderation=True,
            )
        ],
        gpus=[
            GPU(
                name="NVIDIA GeForce RTX 4070",
                vendor=Vendor.NVIDIA,
                driver_version="551.23",
                hags=False,
            )
        ],
        displays=[
            Display(
                name="Generic PnP Monitor",
                resolution="1920x1080",
                current_refresh_hz=60.0,
                max_refresh_hz=165.0,
            )
        ],
        cpu=CPU(
            name="AMD Ryzen 7 7800X3D",
            logical_processors=16,
            l3_domains=1,
            has_p_and_e_cores=False,
        ),
        power=Power(
            active_scheme_name="Balanced", active_scheme_guid="", on_battery=False
        ),
        dns=DNS(server_addresses=["192.168.1.1"], resolve_ms=48.0),
        timer=Timer(
            current_period_us=15600,
            min_period_us=500,
            max_period_us=15625,
            global_timer_resolution_requests=0,
            mmcss_responsiveness="",
        ),
        warnings=[],
        running_anti_cheat=(),
        top_cpu_processes=[("game.exe", 34.2), ("chrome.exe", 8.1)],
        foreground_process="game.exe",
        bloat=None,
        display_version="24H2",
        supports_update_policy=True,
        domain_joined=False,
        game_dvr_app_capture=1,
        game_dvr_enabled=1,
        usb_selective_suspend=True,
    )


# ===== merged from stats.py =====

import math
from dataclasses import dataclass, field
from typing import Any


def _pct(sorted_samples: list[float], q: float) -> float | None:
    if not sorted_samples:
        return None
    if len(sorted_samples) == 1:
        return sorted_samples[0]
    pos = q / 100.0 * (len(sorted_samples) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_samples[lo]
    frac = pos - lo
    return sorted_samples[lo] * (1.0 - frac) + sorted_samples[hi] * frac


@dataclass
class Distribution:
    samples: list[float] = field(default_factory=list)
    lost: int = 0
    sent: int = 0
    label: str = ""

    @property
    def n(self) -> int:
        return len(self.samples)

    @property
    def ordered(self) -> list[float]:
        return sorted(self.samples)

    @property
    def mean(self) -> float | None:
        if not self.samples:
            return None
        return sum(self.samples) / len(self.samples)

    @property
    def p50(self) -> float | None:
        return _pct(self.ordered, 50.0)

    @property
    def p95(self) -> float | None:
        return _pct(self.ordered, 95.0)

    @property
    def p99(self) -> float | None:
        return _pct(self.ordered, 99.0)

    @property
    def max(self) -> float | None:
        return max(self.samples) if self.samples else None

    @property
    def min(self) -> float | None:
        return min(self.samples) if self.samples else None

    @property
    def loss_pct(self) -> float:
        return (100.0 * self.lost / self.sent) if self.sent else 0.0

    @property
    def jitter_mad(self) -> float | None:
        if len(self.samples) < 2:
            return None
        diffs = [
            abs(self.samples[i] - self.samples[i - 1])
            for i in range(1, len(self.samples))
        ]
        if not diffs:
            return None
        return sum(diffs) / len(diffs)

    @property
    def tail_ratio(self) -> float | None:
        p50 = self.p50
        p99 = self.p99
        if p50 is None or p99 is None or p50 == 0:
            return None
        return p99 / p50

    def one_line(self) -> str:
        if not self.samples:
            return "no samples"
        return (
            f"n={self.n} p50={self.p50:.2f} p95={self.p95:.2f} "
            f"max={self.max:.2f} ms loss={self.loss_pct:.1f}%"
        )

    def as_dict(self, include_samples: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "n": self.n,
            "lost": self.lost,
            "sent": self.sent,
            "label": self.label,
            "mean": self.mean,
            "p50": self.p50,
            "p95": self.p95,
            "p99": self.p99,
            "max": self.max,
            "min": self.min,
            "loss_pct": self.loss_pct,
            "jitter_mad": self.jitter_mad,
            "tail_ratio": self.tail_ratio,
        }
        if include_samples:
            d["samples"] = list(self.samples)
        return d


@dataclass(slots=True)
class _TestResult:
    p: float
    t: float = 0.0
    df: float = 0.0
    rank_biserial: float = 0.0


def _mann_whitney(a: list[float], b: list[float]) -> _TestResult:
    n1, n2 = len(a), len(b)
    if n1 < 8 or n2 < 8:
        return _TestResult(p=float("nan"))
    pooled = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    ranks: list[float] = []
    tie_sizes: list[int] = []
    i = 0
    while i < len(pooled):
        j = i
        while j + 1 < len(pooled) and pooled[j + 1][0] == pooled[i][0]:
            j += 1
        avg = (i + 1 + j + 1) / 2.0
        ranks.extend([avg] * (j - i + 1))
        if j > i:
            tie_sizes.append(j - i + 1)
        i = j + 1
    r1 = sum(r for r, (_, g) in zip(ranks, pooled) if g == 0)
    u1 = r1 - n1 * (n1 + 1) / 2.0
    mean_u = n1 * n2 / 2.0
    n = n1 + n2
    tie_term = float(sum(t ** 3 - t for t in tie_sizes))
    var_u = (n1 * n2 / 12.0) * ((n + 1) - tie_term / (n * (n - 1.0)))
    if var_u <= 0:
        return _TestResult(p=float("nan"))
    z = (u1 - mean_u) / math.sqrt(var_u)
    p = math.erfc(abs(z) / math.sqrt(2.0))
    rbc = 1.0 - (2.0 * u1) / (n1 * n2)
    return _TestResult(p=p, rank_biserial=rbc)


def _welch(a: list[float], b: list[float]) -> _TestResult:
    # p is the normal approximation of the t tail (no incomplete beta in
    # the stdlib); it is only honest from ~8 samples per arm, so smaller
    # arms answer NaN and decide() treats that as not significant.
    n1, n2 = len(a), len(b)
    if n1 < 8 or n2 < 8:
        return _TestResult(p=float("nan"))
    m1 = sum(a) / n1
    m2 = sum(b) / n2
    v1 = sum((x - m1) ** 2 for x in a) / (n1 - 1)
    v2 = sum((x - m2) ** 2 for x in b) / (n2 - 1)
    if v1 == 0 and v2 == 0:
        return _TestResult(p=1.0 if m1 == m2 else 0.0)
    se = math.sqrt(v1 / n1 + v2 / n2)
    if se == 0:
        return _TestResult(p=float("nan"))
    t = (m1 - m2) / se
    df_num = (v1 / n1 + v2 / n2) ** 2
    df_den = (v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1)
    df = df_num / df_den if df_den else 1.0
    p = math.erfc(abs(t) / math.sqrt(2.0))
    return _TestResult(p=p, t=t, df=df)


@dataclass
class Comparison:
    metric: str
    delta: float | None
    delta_pct: float | None
    significant: bool
    meaningful: bool
    mwu: _TestResult = field(default_factory=lambda: _TestResult(p=float("nan")))
    welch: _TestResult = field(default_factory=lambda: _TestResult(p=float("nan")))
    recommendation: str = ""
    baseline: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "delta": self.delta,
            "delta_pct": self.delta_pct,
            "significant": self.significant,
            "meaningful": self.meaningful,
            "mwu_p": self.mwu.p,
            "mwu_rank_biserial": self.mwu.rank_biserial,
            "welch_p": self.welch.p,
            "welch_t": self.welch.t,
            "welch_df": self.welch.df,
            "recommendation": self.recommendation,
            "baseline": self.baseline,
            "after": self.after,
        }


def _metric_value(dist: Distribution, metric: str) -> float | None:
    if metric == "mean":
        return dist.mean
    if metric == "max":
        return dist.max
    if metric.startswith("p"):
        try:
            q = float(metric[1:])
        except ValueError:
            return None
        return _pct(dist.ordered, q)
    return None


def decide(
    base: Distribution,
    after: Distribution,
    metric: str = "p95",
    min_effect: float = 1.0,
) -> Comparison:
    b = _metric_value(base, metric)
    a = _metric_value(after, metric)
    if b is None or a is None:
        return Comparison(
            metric=metric,
            delta=None,
            delta_pct=None,
            significant=False,
            meaningful=False,
            recommendation="KEEP nothing: a side had no samples, so no verdict is possible.",
            baseline=base.as_dict(),
            after=after.as_dict(),
        )
    delta = a - b
    delta_pct = (100.0 * delta / b) if b else None
    mwu = _mann_whitney(base.samples, after.samples)
    welch = _welch(base.samples, after.samples)
    significant = (mwu.p is not None and not math.isnan(mwu.p) and mwu.p < 0.05) or (
        welch.p is not None and not math.isnan(welch.p) and welch.p < 0.05
    )
    meaningful = abs(delta) >= min_effect
    if significant and meaningful and delta < 0:
        rec = (
            f"KEEP the change: {metric} improved by {abs(delta):.2f} ms "
            f"and the difference is statistically significant."
        )
    elif significant and meaningful:
        rec = (
            f"REVERT the change: {metric} regressed by {abs(delta):.2f} ms "
            f"and the difference is statistically significant."
        )
    elif significant and delta < 0:
        rec = (
            f"The improvement is real but small: {metric} fell by {abs(delta):.3f} ms, "
            f"below the {min_effect} ms worth-keeping threshold. Keeping or "
            f"reverting are both defensible; prefer the simpler configuration."
        )
    elif significant:
        rec = (
            f"REVERT the change: the {abs(delta):.3f} ms regression is significant "
            f"even though it is below the {min_effect} ms worth-keeping threshold."
        )
    else:
        rec = (
            f"No significant difference in {metric} "
            f"(delta {delta:+.3f} ms); keep the simpler configuration."
        )
    return Comparison(
        metric=metric,
        delta=delta,
        delta_pct=delta_pct,
        significant=significant,
        meaningful=meaningful,
        mwu=mwu,
        welch=welch,
        recommendation=rec,
        baseline=base.as_dict(),
        after=after.as_dict(),
    )


def diagnose_verdict(
    base_median: float | None, after_median: float | None, n1: int, n2: int
) -> tuple[str, str]:
    if base_median is None or after_median is None:
        return ("no-data", "a side produced no usable samples")
    if min(n1, n2) < 30:
        return (
            "thin-evidence",
            f"only {min(n1, n2)} samples on the smaller arm; treat the verdict as weak",
        )
    if abs(after_median - base_median) < 1.0:
        return ("no-effect", "medians differ by under a millisecond")
    direction = "improvement" if after_median < base_median else "regression"
    return ("clear-signal", f"median moved {direction} by {abs(after_median - base_median):.2f} ms")


# ===== merged from traceroute.py =====

import re
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

from system_core import SnappyAIError
from general_utils import console_encoding, silent_kwargs


@dataclass(slots=True)
class Hop:
    index: int
    address: str = ""
    rtts_ms: tuple[float | None, ...] = ()

    @property
    def best_ms(self) -> float | None:
        vals = [r for r in self.rtts_ms if r is not None]
        return min(vals) if vals else None

    def as_dict(self) -> dict[str, Any]:
        return {"hop": self.index, "address": self.address, "rtts_ms": list(self.rtts_ms)}


@dataclass
class TraceResult:
    target: str
    resolved_ip: str = ""
    hops: list[Hop] = field(default_factory=list)
    floor_ms: float | None = None

    @property
    def responding_hop_count(self) -> int:
        return sum(1 for h in self.hops if h.address and h.address != "*")

    @property
    def final_rtt_ms(self) -> float | None:
        for h in reversed(self.hops):
            if h.best_ms is not None:
                return h.best_ms
        return None

    @property
    def implied_path_km(self) -> float | None:
        final = self.final_rtt_ms
        if final is None:
            return None
        return final / 2.0 / 1000.0 * 200000.0

    @property
    def addressable_headroom_ms(self) -> float | None:
        if self.final_rtt_ms is None or self.floor_ms is None:
            return None
        return max(0.0, self.final_rtt_ms - self.floor_ms)

    @property
    def cgnat(self) -> bool:
        return any(
            h.address.startswith("100.") and 64 <= int(h.address.split(".")[1] or 0) <= 127
            for h in self.hops
            if h.address and h.address != "*" and h.address[0].isdigit()
        )

    @property
    def double_nat(self) -> bool:
        def _private(addr: str) -> bool:
            if addr.startswith("192.168.") or addr.startswith("10."):
                return True
            if addr.startswith("172."):
                parts = addr.split(".")
                try:
                    return len(parts) == 4 and 16 <= int(parts[1]) <= 31
                except ValueError:
                    return False
            return False

        privates = sum(
            1 for h in self.hops[:3] if h.address and _private(h.address)
        )
        return privates >= 2

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "resolved_ip": self.resolved_ip,
            "hops": [h.as_dict() for h in self.hops],
            "responding_hop_count": self.responding_hop_count,
            "final_rtt_ms": self.final_rtt_ms,
            "floor_ms": self.floor_ms,
            "addressable_headroom_ms": self.addressable_headroom_ms,
            "implied_path_km": self.implied_path_km,
            "cgnat": self.cgnat,
            "double_nat": self.double_nat,
        }

    def text(self) -> str:
        lines = [f"trace to {self.target} ({self.resolved_ip or 'unresolved'}):"]
        for h in self.hops:
            cells = " ".join(
                f"{r:.1f} ms" if r is not None else "*" for r in h.rtts_ms
            )
            lines.append(f"{h.index:>3}  {h.address or '*':<16}  {cells}")
        return "\n".join(lines)


def floor_from_geoip(
    client: tuple[float, float],
    server: tuple[float, float],
    stretch: str = "conservative",
) -> float:
    import math

    ratios = {"conservative": 1.3, "typical": 1.6, "pessimistic": 2.0}
    ratio = ratios.get(stretch, 1.3)
    lat1, lon1 = (math.radians(v) for v in client)
    lat2, lon2 = (math.radians(v) for v in server)
    d = (
        math.sin(lat1) * math.sin(lat2)
        + math.cos(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    )
    great_circle_km = math.acos(max(-1.0, min(1.0, d))) * 6371.0
    return great_circle_km * ratio / 200000.0 * 2.0 * 1000.0


def traceroute(
    target: str,
    max_hops: int = 30,
    probes_per_hop: int = 3,
    timeout: float = 2.0,
) -> TraceResult:
    if not sys.platform.startswith("win"):
        raise SnappyAIError("traceroute currently needs Windows tracert")
    try:
        resolved = socket.gethostbyname(target)
    except OSError as exc:
        raise SnappyAIError(f"cannot resolve {target}: {exc}") from exc
    try:
        proc = subprocess.run(
            ["tracert", "-d", "-h", str(max_hops), "-w",
             str(int(timeout * 1000.0)), target],
            capture_output=True,
            text=True,
            encoding=console_encoding(),
            errors="replace",
            timeout=timeout * max_hops * probes_per_hop + 10.0,
            **silent_kwargs(),
        )
    except FileNotFoundError as exc:
        raise SnappyAIError(f"tracert not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SnappyAIError(f"tracert timed out: {exc}") from exc
    hops: list[Hop] = []
    for line in (proc.stdout or "").splitlines():
        m = re.match(r"\s*(\d+)\s+(.*)", line)
        if not m:
            continue
        idx = int(m.group(1))
        rest = m.group(2)
        times = [
            float(v) for v in re.findall(r"(\d+)\s*ms", rest)
        ]
        addr_m = re.search(r"(\d+\.\d+\.\d+\.\d+)", rest)
        addr = addr_m.group(1) if addr_m else "*"
        while len(times) < probes_per_hop:
            times.append(None)
        hops.append(
            Hop(index=idx, address=addr, rtts_ms=tuple(times[:probes_per_hop]))
        )
        if addr == resolved:
            break
    return TraceResult(target=target, resolved_ip=resolved, hops=hops)


# ===== merged from render.py =====

METHOD = (
    "real presents on a test swapchain, timed from Present() to the vsync that "
    "displays them via DXGI frame statistics"
)
LIMITS = (
    "Measures this machine's compositor/present path with a blank test surface, "
    "not the game's engine time. Excludes panel scan-out and pixel response, "
    "which need a camera. Flip-discard is tried first, then legacy discard."
)


WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
SW_SHOWNA = 8


def _vcall(ptr: Any, index: int, restype: Any, *argtypes: Any) -> Any:
    import ctypes

    addr = ptr.value if isinstance(ptr, ctypes.c_void_p) else ptr
    vtable = ctypes.cast(addr, ctypes.POINTER(ctypes.c_void_p))[0]
    table = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))
    entry = table[index]
    if isinstance(entry, ctypes.c_void_p):
        entry = entry.value
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(entry)


def _open_window() -> dict[str, Any]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    user32.DefWindowProcW.restype = wintypes.LPARAM

    @ctypes.WINFUNCTYPE(
        wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
        wintypes.LPARAM,
    )
    def _wndproc(hwnd, msg, wp, lp):
        try:
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

    # Explicit prototypes: default c_int restypes would truncate HWND/
    # HINSTANCE on x64. Win64 handles are documented 32-bit-sign-extended
    # so the untyped calls happened to work - typed ones must not rely on
    # that quirk.
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
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    hinst = kernel32.GetModuleHandleW(None)
    cls = WNDCLASSEXW()
    cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
    cls.lpfnWndProc = _wndproc
    cls.hInstance = hinst
    cls.lpszClassName = "SnappyAIPresentProbe"
    if not user32.RegisterClassExW(ctypes.byref(cls)):
        raise OSError("RegisterClassEx failed")
    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    hwnd = user32.CreateWindowExW(
        0, "SnappyAIPresentProbe", "present probe", WS_POPUP | WS_VISIBLE,
        max(0, sw - 96), max(0, sh - 128), 64, 64, None, None, hinst, None,
    )
    if not hwnd:
        raise OSError("CreateWindowEx failed")
    user32.ShowWindow(hwnd, SW_SHOWNA)
    user32.UpdateWindow(hwnd)

    def close() -> None:
        try:
            user32.DestroyWindow(hwnd)
        except Exception:
            pass
        try:
            user32.UnregisterClassW("SnappyAIPresentProbe", hinst)
        except Exception:
            pass

    return {"user32": user32, "hwnd": hwnd, "proc": _wndproc, "close": close}


def _adapter_desc() -> str:
    import ctypes
    from ctypes import wintypes

    try:
        dxgi = ctypes.WinDLL("dxgi", use_last_error=True)

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class ADAPTER_DESC(ctypes.Structure):
            _fields_ = [
                ("Description", wintypes.WCHAR * 128),
                ("VendorId", wintypes.UINT),
                ("DeviceId", wintypes.UINT),
                ("SubSysId", wintypes.UINT),
                ("Revision", wintypes.UINT),
                ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", LUID),
            ]

        dxgi.CreateDXGIFactory.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        dxgi.CreateDXGIFactory.restype = ctypes.c_long

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8),
            ]

        iid = GUID(
            0x7B7166EC, 0x21C7, 0x44AE,
            (wintypes.BYTE * 8)(0xB2, 0x1A, 0xC9, 0xAE, 0x32, 0x1A, 0xE3, 0x69),
        )
        factory = ctypes.c_void_p()
        if dxgi.CreateDXGIFactory(ctypes.byref(iid), ctypes.byref(factory)) != 0:
            return ""
        if not factory.value:
            return ""
        adapter = ctypes.c_void_p()
        get = _vcall(factory, 7, ctypes.c_long, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))
        if get(factory, 0, ctypes.byref(adapter)) != 0 or not adapter.value:
            return ""
        desc = ADAPTER_DESC()
        getdesc = _vcall(adapter, 8, ctypes.c_long, ctypes.POINTER(ADAPTER_DESC))
        name = ""
        if getdesc(adapter, ctypes.byref(desc)) == 0:
            name = "".join(desc.Description).split("\x00")[0]
        release = _vcall(adapter, 2, wintypes.ULONG)
        release(adapter)
        release_f = _vcall(factory, 2, wintypes.ULONG)
        release_f(factory)
        return name
    except Exception:
        return ""


def _open_device(hwnd: Any) -> dict[str, Any]:
    import ctypes
    from ctypes import wintypes

    d3d11 = ctypes.WinDLL("d3d11", use_last_error=True)

    class RATIONAL(ctypes.Structure):
        _fields_ = [("Numerator", wintypes.UINT), ("Denominator", wintypes.UINT)]

    class MODE_DESC(ctypes.Structure):
        _fields_ = [
            ("Width", wintypes.UINT),
            ("Height", wintypes.UINT),
            ("RefreshRate", RATIONAL),
            ("Format", wintypes.UINT),
            ("ScanlineOrdering", wintypes.UINT),
            ("Scaling", wintypes.UINT),
        ]

    class SAMPLE_DESC(ctypes.Structure):
        _fields_ = [("Count", wintypes.UINT), ("Quality", wintypes.UINT)]

    class SWAP_DESC(ctypes.Structure):
        _fields_ = [
            ("BufferDesc", MODE_DESC),
            ("SampleDesc", SAMPLE_DESC),
            ("BufferUsage", wintypes.UINT),
            ("BufferCount", wintypes.UINT),
            ("OutputWindow", wintypes.HWND),
            ("Windowed", wintypes.BOOL),
            ("SwapEffect", wintypes.UINT),
            ("Flags", wintypes.UINT),
        ]

    class FRAME_STATS(ctypes.Structure):
        _fields_ = [
            ("PresentCount", wintypes.UINT),
            ("PresentRefreshCount", wintypes.UINT),
            ("SyncRefreshCount", wintypes.UINT),
            ("SyncQPCTime", ctypes.c_int64),
            ("SyncGPUTime", ctypes.c_int64),
        ]

    level = (wintypes.UINT * 1)(0xB000)
    out_level = wintypes.UINT()
    swapchain = ctypes.c_void_p()
    device = ctypes.c_void_p()
    context = ctypes.c_void_p()
    d3d11.D3D11CreateDeviceAndSwapChain.argtypes = [
        ctypes.c_void_p, ctypes.c_int, wintypes.HMODULE, wintypes.UINT,
        ctypes.POINTER(wintypes.UINT), wintypes.UINT, wintypes.UINT,
        ctypes.POINTER(SWAP_DESC),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT), ctypes.POINTER(ctypes.c_void_p),
    ]
    d3d11.D3D11CreateDeviceAndSwapChain.restype = ctypes.c_long

    def make_desc(effect: int) -> SWAP_DESC:
        desc = SWAP_DESC()
        desc.BufferDesc.Width = 64
        desc.BufferDesc.Height = 64
        desc.BufferDesc.RefreshRate.Numerator = 60
        desc.BufferDesc.RefreshRate.Denominator = 1
        desc.BufferDesc.Format = 28
        desc.SampleDesc.Count = 1
        desc.BufferUsage = 32
        desc.BufferCount = 2
        desc.OutputWindow = hwnd
        desc.Windowed = True
        desc.SwapEffect = effect
        return desc

    effect = ""
    for code, name in ((4, "flip-discard"), (0, "discard")):
        hr = d3d11.D3D11CreateDeviceAndSwapChain(
            None, 1, None, 0, level, 1, 7, ctypes.byref(make_desc(code)),
            ctypes.byref(swapchain), ctypes.byref(device),
            ctypes.byref(out_level), ctypes.byref(context),
        )
        if hr == 0 and swapchain.value:
            effect = name
            break
    if not effect:
        raise OSError("D3D11 device creation failed")

    # vtable: IUnknown 0-2, IDXGIObject 3-6, IDXGIDeviceSubObject::GetDevice 7,
    # Present 8, GetBuffer 9, SetFullscreenState 10, GetFullscreenState 11,
    # GetContainingOutput 12, GetFrameStatistics 13, GetLastPresentCount 14.
    present = _vcall(swapchain, 8, ctypes.c_long, wintypes.UINT, wintypes.UINT)
    getstats = _vcall(swapchain, 13, ctypes.c_long, ctypes.POINTER(FRAME_STATS))
    release = _vcall(swapchain, 2, wintypes.ULONG)

    def do_present(sync: int, flags: int) -> int:
        return int(present(swapchain, sync, flags))

    def do_release() -> None:
        for ptr in (swapchain, context, device):
            try:
                release(ptr)
            except Exception:
                pass

    def close() -> None:
        do_release()

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    freq = ctypes.c_int64()
    kernel32.QueryPerformanceFrequency(ctypes.byref(freq))

    def stats() -> dict[str, int]:
        s = FRAME_STATS()
        hr = getstats(swapchain, ctypes.byref(s))
        return {
            "hr": hr, "pc": s.PresentCount, "prc": s.PresentRefreshCount,
            "src": s.SyncRefreshCount, "qpc": s.SyncQPCTime,
        }

    def now_ms() -> float:
        c = ctypes.c_int64()
        kernel32.QueryPerformanceCounter(ctypes.byref(c))
        return c.value / freq.value * 1000.0

    return {
        "present": do_present, "stats": stats, "now_ms": now_ms, "close": close,
        "effect": effect,
    }


def measure_present(
    samples: int = 40, warmup: int = 30,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if not sys.platform.startswith("win"):
        return {
            "ok": False, "error": "present measurement needs Windows with DirectX",
            "method": METHOD, "limits": LIMITS,
        }
    if samples <= 0:
        return {"ok": False, "error": "no samples requested",
                "method": METHOD, "limits": LIMITS}
    win = None
    dev = None
    try:
        try:
            win = _open_window()
        except OSError as exc:
            return {"ok": False, "error": f"test window failed: {exc}",
                    "method": METHOD, "limits": LIMITS}
        try:
            dev = _open_device(win["hwnd"])
        except OSError as exc:
            return {"ok": False, "error": f"device failed: {exc}",
                    "method": METHOD, "limits": LIMITS}
        present = dev["present"]
        stats = dev["stats"]
        now_ms = dev["now_ms"]
        user32 = win["user32"]
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        freq_raw = ctypes.c_int64()
        kernel32.QueryPerformanceFrequency(ctypes.byref(freq_raw))
        freq = float(freq_raw.value)
        msg = wintypes.MSG()
        user32.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
            wintypes.UINT, wintypes.UINT,
        ]

        def pump() -> None:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

        alive = False
        for round in range(3):
            s_pre = stats()
            for i in range(warmup):
                present(1, 0)
                pump()
                if on_progress is not None and round == 0 and i % 10 == 0:
                    on_progress(f"render warmup {i + 1}/{warmup}")
            s_post = stats()
            if s_post["hr"] == 0 and s_post["prc"] > s_pre.get("prc", -1):
                alive = True
                break
        if not alive:
            return {"ok": False,
                    "error": "presents are not reaching the screen; the test "
                    "window may be covered, minimized, or the session headless",
                    "method": METHOD, "limits": LIMITS}
        lats: list[float] = []
        syncs: list[int] = []
        timeouts = 0
        for i in range(samples):
            s0 = stats()
            if s0["hr"] != 0:
                continue
            t0 = now_ms()
            if present(1, 0) != 0:
                continue
            deadline = time.monotonic() + 1.0
            s1 = None
            while time.monotonic() < deadline:
                pump()
                cur = stats()
                if cur["hr"] == 0 and cur["prc"] > s0["prc"]:
                    s1 = cur
                    break
                time.sleep(0.001)
            if s1 is None:
                timeouts += 1
                if timeouts >= 5:
                    return {"ok": False,
                            "error": "presents stopped reaching the screen mid-run",
                            "method": METHOD, "limits": LIMITS}
                continue
            timeouts = 0
            lats.append(s1["qpc"] / freq * 1000.0 - t0)
            syncs.append(s1["qpc"])
            if on_progress is not None:
                on_progress(f"render sample {i + 1}/{samples}")
        if len(lats) < 2:
            return {"ok": False,
                    "error": "no usable presents; window may be occluded",
                    "method": METHOD, "limits": LIMITS}
        ordered = sorted(lats)
        iv = sorted(b - a for a, b in zip(syncs, syncs[1:]))
        iv_ms = sorted(v / freq * 1000.0 for v in iv)
        try:
            adapter = _adapter_desc()
        except Exception:
            adapter = ""
        dist = Distribution(samples=ordered, lost=timeouts,
                            sent=len(ordered) + timeouts)
        return {
            "ok": True, "error": "",
            "present_ms": ordered,
            "p50_ms": dist.p50,
            "p95_ms": dist.p95,
            "n": len(ordered),
            "interval_ms": iv_ms[len(iv_ms) // 2] if iv_ms else None,
            "adapter": adapter,
            "effect": dev.get("effect", ""),
            "method": METHOD, "limits": LIMITS,
        }
    finally:
        if dev is not None:
            try:
                dev["close"]()
            except Exception:
                pass
        if win is not None:
            try:
                win["close"]()
            except Exception:
                pass
