from __future__ import annotations

import datetime as _dt
import json
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import  engine
from engine import make_backend
from system_core import Confidence
from measurement_utils import CollectOptions, collect_inventory
from system_core import Inventory
from maintenance_utils import (DEBLOAT_OPERATIONS,
                              build_selected as build_debloat)
from maintenance_utils import (build_cleanup_steps, build_memory_steps,
                               build_power_steps, build_redist_steps)
from system_core import SafetyPolicy
from system_core import (request_timer_resolution, stop_timer_request,
                         timer_request_active)
from general_utils import is_elevated, run_ps1
from maintenance_utils import (all_tweaks, applicable_tweaks,
                              experimental_tweaks, refused_tweaks)

__all__ = [
    "EventType",
    "Event",
    "TaskState",
    "ScanResult",
    "GuiController",
    "PlainFinding",
    "CONSENT_TEXT",
    "issues_message",
    "apply_confirm_text",
    "undo_confirm_text",
    "run_confirm_text",
    "chain_confirm_text",
    "classify_scan",
    "manual_instructions",
]


class EventType(str, Enum):
    LOG = "log"
    PROGRESS = "progress"
    INVENTORY = "inventory"
    SCAN_DONE = "scan_done"
    CHAIN_DONE = "chain_done"
    SCRIPT_DONE = "script_done"
    APPLY_DONE = "apply_done"
    UNDO_DONE = "undo_done"
    MONITOR_TICK = "monitor_tick"
    MEMORY_CLEAN_DONE = "memory_clean_done"
    TIMER_DONE = "timer_done"
    ERROR = "error"
    BUSY = "busy"
    IDLE = "idle"


@dataclass(slots=True)
class Event:
    kind: EventType
    message: str = ""
    data: Any = None
    progress: float | None = None


class TaskState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"


CHAIN_BUDGET_SECONDS = 20.0


@dataclass
class ScanResult:
    inventory: Inventory
    planned_ids: list[str] = field(default_factory=list)
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    automated_steps: int = 0
    manual_steps: int = 0
    requires_reboot: bool = False
    policy_notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    out_dir: Path | None = None
    files: list[str] = field(default_factory=list)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


CONSENT_TEXT = (
    "Your PC informations remain private - none of your data will be sent anywhere. "
    "Do you want to continue?"
)


def issues_message(
    n_total: int, n_auto: int, n_manual: int, brand: str = "latency"
) -> str:
    return (
        f"{n_total} issues found, {n_auto} issues are possible to resolve with "
        f"{brand} but {n_manual} needs manual adjustments. After you continue "
        f"{brand} will solve the issues and list you exactly what you need to do "
        f"for the rest. Do you want to continue?"
    )


def apply_confirm_text(brand: str = "latency") -> str:
    return (
        f"{brand} will now run the generated apply script SILENTLY with "
        f"administrator rights: no terminal window opens, and every change is "
        f"recorded for undo. Do you want to continue?"
    )


def undo_confirm_text(brand: str = "latency") -> str:
    return (
        f"{brand} will now run the undo script SILENTLY to restore the values "
        f"recorded before the changes were applied. Do you want to continue?"
    )


def run_confirm_text(path: str, brand: str = "latency") -> str:
    return (
        f"{brand} will now run this script SILENTLY with administrator rights: "
        f"no terminal window opens. Irreversible steps still state their safeguard "
        f"in the log. Do you want to continue?\n\n{path}"
    )


def chain_confirm_text(count: int, brand: str = "latency") -> str:
    return (
        f"{brand} will now send {count} synthetic mouse clicks and measure how "
        f"fast they travel through the input stack, then present test frames to "
        f"measure render timing. The live graph's own pings then serve as the "
        f"network measurement for the rest of the {CHAIN_BUDGET_SECONDS:g}-second "
        f"window - one measurement, shown in both places. Park the cursor on "
        f"empty desktop and do not touch the mouse until it finishes. "
        f"Do you want to continue?"
    )


@dataclass
class PlainFinding:
    tweak_id: str
    headline: str
    detail: str
    fix_kind: str
    fix_text: str
    severity: str
    component: str
    expected: str


_HEADLINES = {
    "net.wifi.prefer_wired": "You are gaming over Wi-Fi",
    "nic.power_saving.disable": "Your network card sleeps between packets",
    "nic.interrupt_moderation.disable": "Your network card delays interrupts to save CPU",
    "net.dns.fastest_resolver": "Your domain name lookups go through a slow resolver",
    "net.mtu.correctness": "Your network packet size is misconfigured",
    "net.bufferbloat.upload_cap": "Your upload queue fills up and holds game packets hostage",
    "net.bufferbloat.router_sqm": "Your router's queue fills up under load (bufferbloat)",
    "display.refresh_rate.maximum": "Your monitor is running below its real refresh rate",
    "gpu.vendor.low_latency_mode": "Your graphics driver is queueing frames ahead of you",
    "gpu.hags.ab_test": "Hardware GPU scheduling is worth testing on your machine",
    "input.usb.selective_suspend_off": "Your USB devices are put to sleep between inputs",
    "bus.pcie.aspm_off": "PCI Express links sleep between transactions",
    "timer.global_resolution_requests": "Windows schedules your game on a coarse 15.6 ms tick",
    "timer.bcdedit_platform.experimental": "Boot-time timer settings (contested, optional)",
    "sched.mmcss.games_task": "The multimedia scheduler is not prioritising games",
    "sched.background.lower_priority": "Background programs are fighting your game for CPU",
    "os.game_mode.ab_test": "Game Mode / fullscreen optimisations are worth testing",
    "capture.game_dvr.disable": "Background recording is stealing GPU time",
    "cpu.interrupt_affinity.delegate": "Device interrupts land on the cores your game uses",
    "cpu.cpu_sets.delegate": "Your game jumps between CPU cache domains",
}


def _headline(tweak_id: str, title: str) -> str:
    return _HEADLINES.get(tweak_id, title)


def classify_scan(scan: ScanResult, brand: str = "latency") -> dict[str, list[PlainFinding]]:
    auto: list[PlainFinding] = []
    manual: list[PlainFinding] = []
    fine: list[PlainFinding] = []
    unsure: list[PlainFinding] = []
    for o in scan.outcomes:
        if o.get("refused"):
            continue
        verdict = o["verdict"]
        headline = _headline(o["id"], o["title"])
        if verdict == "problem":
            if o.get("automated", 0) > 0:
                kind = "auto"
                fix = f"{brand} will change this for you automatically."
            else:
                kind = "manual"
                fix = f"This one needs you: {brand} will show the exact steps."
            pf = PlainFinding(
                tweak_id=o["id"], headline=headline, detail=o["summary"],
                fix_kind=kind, fix_text=fix, severity="problem",
                component=o["component"], expected=o.get("expected_effect", ""),
            )
            (auto if kind == "auto" else manual).append(pf)
        elif verdict == "unknown":
            unsure.append(
                PlainFinding(
                    tweak_id=o["id"], headline=headline, detail=o["summary"],
                    fix_kind="none", fix_text="", severity="unknown",
                    component=o["component"], expected="",
                )
            )
        elif verdict == "no_problem":
            fine.append(
                PlainFinding(
                    tweak_id=o["id"], headline=headline, detail=o["summary"],
                    fix_kind="none", fix_text="", severity="ok",
                    component=o["component"], expected="",
                )
            )
        else:
            unsure.append(
                PlainFinding(
                    tweak_id=o["id"], headline=headline, detail=o["summary"],
                    fix_kind="none", fix_text="", severity="unknown",
                    component=o["component"], expected="",
                )
            )
    return {"auto": auto, "manual": manual, "fine": fine, "unsure": unsure}


def manual_instructions(scan: ScanResult) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not scan.out_dir:
        return out
    manifest_path = Path(scan.out_dir) / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for change in manifest.get("changes", []):
        for step in change.get("steps", []):
            if step.get("manual"):
                out.append(
                    {
                        "tweak": change.get("id", ""),
                        "title": change.get("title", ""),
                        "instruction": (
                            step.get("text") or step.get("notes")
                            or step.get("description", "")
                        ),
                        "why": step.get("notes") or "",
                    }
                )
    return out


_MEMORY_CLEAN_SCRIPT = r"""
$sig = @'
using System;
using System.Runtime.InteropServices;
public static class MemClean {
  [DllImport("ntdll.dll")]
  public static extern int NtSetSystemInformation(int infoClass, ref int info, int length);
}
'@
Add-Type -TypeDefinition $sig -ErrorAction Stop

function Read-MemStats {
  $c = Get-Counter '\Memory\Available MBytes' -ErrorAction SilentlyContinue
  $m = @{}
  foreach ($s in $c.CounterSamples) {
    $m['Available MBytes'] = [math]::Round($s.CookedValue, 1)
  }
  if (-not $m.ContainsKey('Available MBytes')) {
    try {
      $m['Available MBytes'] = [math]::Round(
        (Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1KB, 1)
    } catch {}
  }
  return $m
}

$before = Read-MemStats
$areas = New-Object System.Collections.ArrayList

function Invoke-Area {
  param([string]$Label, [int]$Command)
  $v = $Command
  $st = [MemClean]::NtSetSystemInformation(80, [ref]$v, 4)
  [void]$areas.Add([pscustomobject]@{
    area   = $Label
    status = $(if ($st -eq 0) { 'ok' } else { 'NTSTATUS 0x{0:X}' -f $st })
  })
}

Invoke-Area 'standby list' 4
Invoke-Area 'low-priority standby list' 5
Invoke-Area 'modified page list' 3
Invoke-Area 'working sets (all processes)' 2

$v = 0
$st = [MemClean]::NtSetSystemInformation(155, [ref]$v, 0)
[void]$areas.Add([pscustomobject]@{
  area   = 'registry cache'
  status = $(if ($st -eq 0) { 'ok' } else { 'NTSTATUS 0x{0:X}' -f $st })
})

Start-Sleep -Milliseconds 600
$after = Read-MemStats
[pscustomobject]@{
  before = $before
  after  = $after
  areas  = $areas
  caveat = 'Working sets were emptied machine-wide. Expect a short burst of page faults while applications fault their pages back in.'
} | ConvertTo-Json -Depth 4 -Compress
"""


class GuiController:
    def __init__(
        self, *, backend_name: str = "auto", out_root: str | Path | None = None,
        brand: str = "latency",
    ) -> None:
        self.backend_name = backend_name
        self.brand = brand
        self.out_root = (
            Path(out_root) if out_root
            else Path.cwd() / f"{brand.lower()}-output"
        )
        self.events: queue.Queue[Event] = queue.Queue()
        self.inventory: Inventory | None = None
        self.last_scan: ScanResult | None = None
        self.last_script: dict[str, Any] | None = None
        self.state: TaskState = TaskState.IDLE
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._monitor_stop: threading.Event | None = None
        self._monitor_thread: threading.Thread | None = None
        self._monitor_samples: list[float] = []
        self._monitor_sent = 0
        self._monitor_lost = 0
        self._monitor_target: str | None = None
        self._monitor_lock = threading.Lock()

    def backend(self) -> Any:
        return make_backend("fake" if self.backend_name == "fake" else None)

    def poll(self) -> list[Event]:
        out: list[Event] = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out

    def _emit(
        self, kind: EventType, message: str = "", data: Any = None,
        progress: float | None = None,
    ) -> None:
        self.events.put(Event(kind=kind, message=message, data=data, progress=progress))

    def log(self, message: str) -> None:
        self._emit(EventType.LOG, message)

    def _run(self, name: str, fn: Callable[[], None]) -> bool:
        if self.state is TaskState.RUNNING:
            self._emit(
                EventType.ERROR,
                "another operation is still running; wait for it to finish",
            )
            return False
        self._stop.clear()
        self.state = TaskState.RUNNING
        self._emit(EventType.BUSY, name)

        def wrapper() -> None:
            try:
                fn()
            except Exception as exc:
                self._emit(
                    EventType.ERROR,
                    f"{type(exc).__name__}: {exc}",
                    data=traceback.format_exc(limit=12),
                )
            finally:
                self.state = TaskState.IDLE
                self._emit(EventType.IDLE, name)

        t = threading.Thread(target=wrapper, name=f"latency-{name}", daemon=True)
        t.start()
        self._threads.append(t)
        self._threads = [x for x in self._threads if x.is_alive()]
        return True

    def _plan_dir(self, plan_dir: str | Path | None) -> Path | None:
        if plan_dir:
            return Path(plan_dir)
        if self.last_scan and self.last_scan.out_dir:
            return Path(self.last_scan.out_dir)
        return None

    def start_apply(self, plan_dir: str | Path | None = None) -> bool:
        target = self._plan_dir(plan_dir)
        if target is None:
            self._emit(EventType.ERROR, "nothing to apply: run a scan first")
            return False
        apply_ps1 = target / "apply.ps1"
        if not apply_ps1.exists():
            self._emit(EventType.ERROR, f"{apply_ps1} does not exist")
            return False
        return self._run("apply", lambda: self._run_script_file(
            apply_ps1, EventType.APPLY_DONE, "apply", target / "apply-run.log"
        ))

    def start_undo(self, plan_dir: str | Path | None = None) -> bool:
        target = self._plan_dir(plan_dir)
        if target is None:
            self._emit(EventType.ERROR, "nothing to undo: run a scan first")
            return False
        undo_ps1 = target / "undo.ps1"
        if not undo_ps1.exists():
            self._emit(EventType.ERROR, f"{undo_ps1} does not exist")
            return False
        if not (target / "snapshot.json").exists():
            self._emit(
                EventType.ERROR,
                "no snapshot.json next to undo.ps1: nothing was applied, so there "
                "is nothing to restore",
            )
            return False
        return self._run("undo", lambda: self._run_script_file(
            undo_ps1, EventType.UNDO_DONE, "undo", target / "undo-run.log"
        ))

    def start_run_script(self, script_path: str | Path) -> bool:
        path = Path(script_path)
        if not path.exists() or path.suffix.lower() != ".ps1":
            self._emit(EventType.ERROR, f"{path} is not a runnable script")
            return False
        log = path.parent / (path.stem + "-run.log")
        return self._run("run-script", lambda: self._run_script_file(
            path, EventType.SCRIPT_DONE, path.stem, log, extra_data={"path": str(path)}
        ))

    def _run_script_file(
        self, script: Path, done: EventType, label: str, log: Path,
        extra_data: dict[str, Any] | None = None,
    ) -> None:
        res = run_ps1(
            script,
            args=("-NoPause", "-Force", "-SkipManual"),
            timeout=900.0,
            elevate=True,
            log_path=log,
        )
        data = {
            "exit_code": res.code,
            "log": res.output,
            "log_path": str(log),
            "script": str(script),
        }
        if extra_data:
            data.update(extra_data)
        self._emit(
            done,
            f"{label} finished with exit code {res.code}",
            data=data,
        )

    @staticmethod
    def elevated() -> bool:
        return is_elevated()

    def start_monitor(
        self, target: str, interval: float = 1.0, transport: str = "auto",
        port: int = 443,
    ) -> bool:
        if self.monitoring:
            self.stop_monitor()
        with self._monitor_lock:
            self._monitor_samples = []
            self._monitor_sent = 0
            self._monitor_lost = 0
            self._monitor_target = target
        self._monitor_stop = threading.Event()
        t = threading.Thread(
            target=self._monitor_loop,
            args=(target, interval, transport, port, self._monitor_stop),
            name="latency-monitor",
            daemon=True,
        )
        t.start()
        self._monitor_thread = t
        return True

    def _monitor_loop(
        self, target: str, interval: float, transport: str, port: int,
        stop: threading.Event,
    ) -> None:
        from collections import deque

        from measurement_utils import Distribution, probe

        window: deque[float] = deque(maxlen=240)
        lost = 0
        sent = 0
        while not stop.is_set():
            try:
                res = probe(
                    target, count=1, interval=0.0, transport=transport, port=port,
                    timeout=min(2.0, max(0.5, interval)),
                )
            except Exception as exc:
                if stop is not self._monitor_stop:
                    return
                self._emit(
                    EventType.MONITOR_TICK, f"probe error: {exc}",
                    data={"error": str(exc), "series": list(window)},
                )
                stop.wait(interval)
                continue
            if stop is not self._monitor_stop:
                return
            for smp in res.samples:
                sent += 1
                if smp.rtt_ms is None:
                    lost += 1
                else:
                    window.append(smp.rtt_ms)
            with self._monitor_lock:
                self._monitor_sent = sent
                self._monitor_lost = lost
                for smp in res.samples:
                    if smp.rtt_ms is not None:
                        self._monitor_samples.append(float(smp.rtt_ms))
                if len(self._monitor_samples) > 5000:
                    del self._monitor_samples[:2500]
            if window:
                d = Distribution(samples=list(window), lost=lost, sent=sent)
                payload = {
                    "current": window[-1], "p50": d.p50, "p95": d.p95,
                    "max": d.max, "loss_pct": d.loss_pct, "jitter": d.jitter_mad,
                    "n": d.n, "series": list(window),
                }
            else:
                payload = {
                    "current": None, "p50": None, "p95": None, "max": None,
                    "loss_pct": (100.0 * lost / sent) if sent else 0.0,
                    "jitter": None, "n": 0, "series": [],
                }
            self._emit(EventType.MONITOR_TICK, "", data=payload)
            stop.wait(interval)

    def stop_monitor(self) -> None:
        stop, self._monitor_stop = self._monitor_stop, None
        if stop is not None:
            stop.set()
        thread, self._monitor_thread = self._monitor_thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.5)

    def monitor_samples(self, target: str) -> dict[str, Any] | None:
        with self._monitor_lock:
            if self._monitor_target != target:
                return None
            return {
                "samples": list(self._monitor_samples),
                "lost": self._monitor_lost,
                "sent": self._monitor_sent,
            }

    @property
    def monitoring(self) -> bool:
        return self._monitor_stop is not None and not self._monitor_stop.is_set()

    def start_memory_clean(self) -> bool:
        return self._run("memory-clean", self._memory_clean)

    def _memory_clean(self) -> None:
        if self.backend_name == "fake":
            res = self.backend().powershell(_MEMORY_CLEAN_SCRIPT, timeout=180.0)
            ok, out, err = res.ok, res.stdout, res.stderr
        else:
            import tempfile
            script = Path(tempfile.gettempdir()) / "snappyai-memory-clean.ps1"
            try:
                engine.write_powershell(script, _MEMORY_CLEAN_SCRIPT)
                res = run_ps1(script, timeout=180.0, elevate=True,
                              log_path=str(script) + ".log")
                ok, out, err = res.code == 0, res.output, res.output
            except Exception as exc:
                self._emit(EventType.ERROR, f"memory clean failed: {exc}")
                return
        if not ok:
            self._emit(
                EventType.ERROR,
                f"memory clean failed: {((err or out) or '')[:400]}",
            )
            return
        try:
            data = json.loads((out or "").strip() or "{}")
        except ValueError:
            self._emit(
                EventType.ERROR,
                f"memory clean returned no result: {(out or '')[:200]}",
            )
            return
        self._emit(EventType.MEMORY_CLEAN_DONE, "memory clean complete", data=data)

    def start_timer_request(self, target_us: float) -> bool:
        return self._run("timer-request", lambda: self._timer_request(target_us))

    def _timer_request(self, target_us: float) -> None:
        res = request_timer_resolution(target_us)
        res["active"] = timer_request_active()
        if not res.get("ok"):
            self._emit(
                EventType.TIMER_DONE,
                f"timer request failed: {res.get('error') or 'unknown'}",
                data=res,
            )
            return
        actual = res.get("actual_us")
        ms = actual / 1000.0 if actual else target_us / 1000.0
        self._emit(
            EventType.TIMER_DONE,
            f"timer resolution held at {ms:.4f} ms for SnappyAI's own "
            f"waits while it runs (process-scoped on Windows 10 2004+; "
            f"the experimental system-wide tweak promotes it machine-wide)",
            data=res,
        )

    def start_timer_stop(self) -> bool:
        return self._run("timer-stop", self._timer_stop)

    def _timer_stop(self) -> None:
        res = stop_timer_request()
        res["active"] = timer_request_active()
        cur = res.get("current_us")
        if res.get("superseded"):
            msg = "a newer timer request is active - nothing was released"
        else:
            msg = "timer request released"
            if res.get("released") is False:
                msg += (" (the coarse re-request failed - the fine tick "
                        "may persist until SnappyAI exits)")
        if cur:
            msg += f"; tick now {cur / 1000.0:.4f} ms"
        self._emit(EventType.TIMER_DONE, msg, data=res)

    @property
    def timer_active(self) -> bool:
        return timer_request_active()

    def join(self, timeout: float = 5.0) -> None:
        for t in list(self._threads):
            t.join(timeout=timeout)

    def counts(self) -> dict[str, int]:
        return {
            "applicable": len(applicable_tweaks()),
            "experimental": len(experimental_tweaks()),
            "refused": len(refused_tweaks()),
            "total": len(all_tweaks()),
        }

    @staticmethod
    def debloat_options() -> list[dict[str, Any]]:
        return [
            {
                "id": o.id, "title": o.title, "summary": o.summary,
                "reversible": o.reversible, "safety": o.safety,
                "requires_reboot": o.requires_reboot,
            }
            for o in DEBLOAT_OPERATIONS
        ]

    def start_scan(
        self, *, experimental: bool = False, with_maintenance: bool = False,
        min_confidence: str = "low", out_dir: str | Path | None = None,
    ) -> bool:
        return self._run(
            "scan",
            lambda: self._scan(
                experimental=experimental, with_maintenance=with_maintenance,
                min_confidence=min_confidence, out_dir=out_dir,
            ),
        )

    def _scan(
        self, *, experimental: bool, with_maintenance: bool, min_confidence: str,
        out_dir: str | Path | None,
    ) -> None:
        self._emit(EventType.PROGRESS, "collecting inventory", progress=0.05)
        opts = CollectOptions(benchmark_dns=True, detect_processes=True)
        inv = collect_inventory(
            self.backend(), opts,
            on_progress=lambda s: self._emit(EventType.LOG, f"inventory: {s}"),
        )
        self.inventory = inv
        self._emit(
            EventType.INVENTORY, "inventory collected",
            data=self._inventory_summary(inv), progress=0.4,
        )
        policy = SafetyPolicy(
            min_confidence=Confidence(min_confidence),
            allow_experimental=experimental,
        )
        self._emit(
            EventType.PROGRESS, "probing every check against this machine",
            progress=0.55,
        )
        result = engine.build_plan(inv, policy=policy)
        target = Path(out_dir) if out_dir else self.out_root / "plan"
        self._emit(EventType.PROGRESS, f"writing scripts to {target}", progress=0.8)
        maintenance: list[dict] = []
        maint_script: str | None = None
        if with_maintenance:
            from engine import render_maintenance_script, write_powershell
            from maintenance_utils import (cleanup_report, memory_report,
                                           redist_report)

            steps = [*build_cleanup_steps(survey_only=True), *build_redist_steps()]
            maint_script = render_maintenance_script(
                steps,
                title=f"{self.brand} maintenance (cleanup SURVEY ONLY - deletes nothing; "
                "plus redistributables)",
                generated_at=_now(),
                brand=self.brand,
            )
            maintenance = [*cleanup_report(), *redist_report(), *memory_report()]
        bundle = engine.render(result, generated_at=_now(), brand=self.brand)
        paths = bundle.write(target)
        from engine import render_html, render_markdown

        report = engine.build_report(
            result, maintenance=maintenance, generated_at=_now()
        )
        (target / "report.md").write_text(
            render_markdown(report, brand=self.brand), encoding="utf-8"
        )
        (target / "report.html").write_text(
            render_html(report, brand=self.brand), encoding="utf-8"
        )
        paths["report_md"] = target / "report.md"
        paths["report_html"] = target / "report.html"
        if maint_script:
            write_powershell(target / "maintenance.ps1", maint_script)
            paths["maintenance"] = target / "maintenance.ps1"
        counts: dict[str, int] = {}
        outcomes: list[dict[str, Any]] = []
        for o in result.outcomes:
            counts[o.verdict.value] = counts.get(o.verdict.value, 0) + 1
            outcomes.append(
                {
                    "id": o.tweak.id,
                    "title": o.tweak.title,
                    "component": o.tweak.component.value,
                    "confidence": o.tweak.confidence.value,
                    "risk": o.tweak.risk.value,
                    "verdict": o.verdict.value,
                    "summary": o.summary,
                    "refused": o.tweak.is_refused,
                    "experimental": o.tweak.experimental,
                    "expected_effect": o.tweak.expected_effect,
                    "caveats": o.tweak.caveats,
                    "steps": o.step_count_total,
                    "automated": o.step_count,
                    "requires_reboot": o.tweak.requires_reboot,
                    "evidence": list(o.tweak.evidence_urls),
                    "policy_allowed": None if o.policy is None else o.policy.allowed,
                    "policy_reason": "" if o.policy is None else o.policy.reason,
                    "refusal_reason": o.tweak.refusal_reason,
                }
            )
        self.last_scan = ScanResult(
            inventory=inv,
            planned_ids=list(result.actionable_ids),
            outcomes=outcomes,
            counts=counts,
            automated_steps=result.automated_step_count,
            manual_steps=result.manual_step_count,
            requires_reboot=result.requires_reboot,
            policy_notes=list(result.policy_notes),
            errors=list(result.errors),
            out_dir=target,
            files=[p.name for p in paths.values()],
        )
        self._emit(
            EventType.SCAN_DONE,
            f"scan complete: {len(result.planned)} change(s), "
            f"{result.automated_step_count} automated step(s)",
            data=self._scan_summary(self.last_scan),
            progress=1.0,
        )

    @staticmethod
    def _inventory_summary(inv: Inventory) -> dict[str, Any]:
        nic = inv.primary_nic
        gpu = inv.primary_gpu
        disp = inv.primary_display
        return {
            "os_name": inv.os_name,
            "windows_build": inv.windows_build,
            "is_elevated": inv.is_elevated,
            "cpu": inv.cpu.name,
            "logical_processors": inv.cpu.logical_processors,
            "l3_domains": inv.cpu.l3_domains,
            "gpu": gpu.name if gpu else None,
            "gpu_vendor": gpu.vendor.value if gpu else None,
            "hags": gpu.hags if gpu else None,
            "display": disp.name if disp else None,
            "refresh_hz": disp.current_refresh_hz if disp else None,
            "max_refresh_hz": disp.max_refresh_hz if disp else None,
            "refresh_deficit_hz": disp.refresh_deficit_hz if disp else None,
            "nic": (nic.interface_description or nic.name) if nic else None,
            "nic_physical": nic.physical if nic else None,
            "nic_mtu": nic.mtu if nic else None,
            "is_wireless": inv.uses_wifi_for_primary,
            "power_scheme": inv.power.active_scheme_name or inv.power.active_scheme_guid,
            "on_battery": inv.power.on_battery,
            "timer_tick_us": inv.timer.current_period_us,
            "timer_floor_us": inv.timer.min_period_us,
            "timer_coarsest_us": inv.timer.max_period_us,
            "global_timer_requests": inv.timer.global_timer_resolution_requests,
            "mmcss_responsiveness": inv.timer.mmcss_responsiveness,
            "dns_servers": list(inv.dns.server_addresses),
            "dns_resolve_ms": inv.dns.resolve_ms,
            "anti_cheat": list(inv.running_anti_cheat),
            "foreground": inv.foreground_process,
            "top_processes": [
                {"name": n, "cpu_pct": round(c, 1)}
                for n, c in inv.top_cpu_processes[:8]
            ],
            "warnings": list(inv.warnings),
        }

    @staticmethod
    def _scan_summary(res: ScanResult) -> dict[str, Any]:
        return {
            "planned_ids": list(res.planned_ids),
            "counts": dict(res.counts),
            "automated_steps": res.automated_steps,
            "manual_steps": res.manual_steps,
            "requires_reboot": res.requires_reboot,
            "policy_notes": list(res.policy_notes),
            "errors": list(res.errors),
            "out_dir": str(res.out_dir) if res.out_dir else None,
            "files": list(res.files),
            "outcomes": list(res.outcomes),
        }

    def start_chain(
        self, *, target: str, input_count: int = 20, render_samples: int = 40,
        net_samples: int = 30, save_path: str | Path | None = None,
        budget_s: float = CHAIN_BUDGET_SECONDS,
    ) -> bool:
        return self._run(
            "chain",
            lambda: self._chain(
                target=target, input_count=input_count,
                render_samples=render_samples, net_samples=net_samples,
                save_path=save_path, budget_s=budget_s,
            ),
        )

    def _chain(
        self, *, target: str, input_count: int, render_samples: int,
        net_samples: int, save_path: str | Path | None,
        budget_s: float = CHAIN_BUDGET_SECONDS,
    ) -> None:
        from input_utils import measure_input
        from measurement_utils import (Distribution, ProbeResult, build_chain,
                                       probe)
        from measurement_utils import measure_present

        started = time.monotonic()
        payload: dict[str, Any] = {
            "taken_at": _now(), "target": target, "brand": self.brand,
        }
        self._emit(EventType.PROGRESS, "input leg: synthetic clicks", progress=0.1)
        input_res = measure_input(
            count=input_count,
            on_progress=lambda m: self._emit(EventType.LOG, m),
        )
        payload["input"] = input_res
        self._emit(EventType.PROGRESS, "render leg: presenting frames", progress=0.3)
        render_res = measure_present(
            samples=render_samples,
            on_progress=lambda m: self._emit(EventType.LOG, m),
        )
        payload["render"] = render_res
        self._emit(
            EventType.PROGRESS, f"network leg: probing {target}", progress=0.4
        )
        deadline = started + max(1.0, float(budget_s))
        try:
            if self.monitoring and self.monitor_samples(target) is not None:
                next_report = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    time.sleep(0.1)
                    now = time.monotonic()
                    if now >= next_report:
                        snap = self.monitor_samples(target) or {}
                        frac = min(1.0, (now - started)
                                   / max(1.0, deadline - started))
                        self._emit(
                            EventType.PROGRESS,
                            "network leg: live watch (%d samples, %ds of %gs)"
                            % (len(snap.get("samples") or []),
                               int(round(now - started)), float(budget_s)),
                            progress=0.4 + 0.55 * frac,
                        )
                        next_report = now + 2.0
                snap = self.monitor_samples(target) or {}
                dist = Distribution(
                    samples=list(snap.get("samples") or []),
                    lost=int(snap.get("lost") or 0),
                    sent=int(snap.get("sent") or 0),
                    label="live watch",
                )
                payload["network"] = dist.as_dict()
                payload["network"]["budget_s"] = budget_s
                payload["network"]["source"] = "live-watch"
            else:
                samples: list[Any] = []
                transport = "auto"
                last_rtt: float | None = None
                next_report = time.monotonic() + 2.0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.05:
                        break
                    est = 0.05 if last_rtt is None else max(0.005, last_rtt / 1000.0) + 0.011
                    count = max(1, min(20, int(remaining / est)))
                    timeout = min(2.0, max(0.25, 4.0 * est + 0.1))
                    res = probe(
                        target, count=count, interval=0.01, transport=transport,
                        timeout=timeout,
                    )
                    if res.transport:
                        transport = res.transport
                    for smp in res.samples:
                        samples.append(smp)
                        if smp.rtt_ms is not None:
                            last_rtt = float(smp.rtt_ms)
                    now = time.monotonic()
                    if now >= next_report:
                        frac = min(1.0, (now - started) / max(1.0, deadline - started))
                        self._emit(
                            EventType.PROGRESS,
                            "network leg: probing %s (%ds of %gs)"
                            % (target, int(round(now - started)),
                               float(budget_s)),
                            progress=0.4 + 0.55 * frac,
                        )
                        next_report = now + 2.0
                if not samples:
                    res = probe(target, count=min(net_samples, 20), interval=0.01,
                                transport=transport, timeout=2.0)
                    samples = list(res.samples)
                    if res.transport:
                        transport = res.transport
                dist = ProbeResult(samples=samples, transport=transport,
                                   target=target).distribution(target)
                payload["network"] = dist.as_dict()
                payload["network"]["budget_s"] = budget_s
                payload["network"]["source"] = "chain-probe"
        except Exception as exc:
            payload["network"] = {"error": f"{type(exc).__name__}: {exc}"}
            payload["network_error"] = str(exc)
        payload["chain"] = build_chain(
            input_res, render_res, payload["network"]
        )
        payload["duration_s"] = round(time.monotonic() - started, 2)
        if save_path:
            payload["saved_to"] = str(engine.save_baseline(save_path, payload))
        total = payload["chain"]["total_ms"]
        self._emit(
            EventType.CHAIN_DONE,
            f"chain complete: {total:.1f} ms" if total is not None
            else "chain incomplete: a leg produced no measurement",
            data=payload, progress=1.0,
        )

    def start_diagnose(
        self, *, plan_dir: str | Path | None = None,
        out_dir: str | Path | None = None,
    ) -> bool:
        return self._run(
            "diagnose",
            lambda: self._diagnose(plan_dir=plan_dir, out_dir=out_dir),
        )

    def _diagnose(
        self, *, plan_dir: str | Path | None, out_dir: str | Path | None
    ) -> None:
        from general_utils import run_diagnosis

        plan = self._plan_dir(plan_dir)
        out = Path(out_dir) if out_dir else self.out_root / "diagnose"
        diag = run_diagnosis(
            self.backend(), out, plan_dir=plan, dry_run=True, brand=self.brand,
            on_progress=lambda m: self._emit(EventType.LOG, f"diagnose: {m}"),
        )
        self._emit(
            EventType.SCRIPT_DONE,
            f"diagnosis written: {diag.log_path.name}"
            + (
                f" (failed sections: {', '.join(diag.failed_sections)})"
                if diag.failed_sections
                else ""
            ),
            data={
                "path": str(diag.log_path),
                "json": str(diag.json_path),
                "failed": diag.failed_sections,
            },
        )
        self.last_script = {"path": str(diag.log_path), "kind": "diagnose"}

    def start_generate(
        self, *, kind: str, selected: tuple[str, ...] | None = None,
        include_opt_in: bool = False, include_optional: bool = False,
        execute: bool = False, process_names: tuple[str, ...] = (),
        out_dir: str | Path | None = None,
    ) -> bool:        return self._run(
            f"generate-{kind}",
            lambda: self._generate(
                kind=kind, selected=selected, include_opt_in=include_opt_in,
                include_optional=include_optional, execute=execute,
                process_names=process_names, out_dir=out_dir,
            ),
        )

    def _generate(
        self, *, kind: str, selected: tuple[str, ...] | None,
        include_opt_in: bool, include_optional: bool, execute: bool,
        process_names: tuple[str, ...], out_dir: str | Path | None,
    ) -> None:
        from engine import render_maintenance_script, write_powershell

        if kind == "cleanup":
            steps = build_cleanup_steps(
                selected, include_opt_in=include_opt_in, survey_only=not execute
            )
            title = f"{self.brand} disk cleanup" + (
                " (SURVEY ONLY - deletes nothing)" if not execute else ""
            )
            fname = "clean.ps1"
        elif kind == "redists":
            steps = build_redist_steps(selected, include_optional=include_optional)
            title = f"{self.brand} gaming redistributables"
            fname = "redists.ps1"
        elif kind == "memory":
            steps = build_memory_steps(
                selected or ("memory.measure",), process_names=process_names
            )
            title = f"{self.brand} memory"
            fname = "memory.ps1"
        elif kind == "power":
            steps = build_power_steps(selected, include_opt_in=include_opt_in)
            title = f"{self.brand} power (core parking / EPP)"
            fname = "power.ps1"
        elif kind == "debloat":
            inv = self.inventory
            if inv is None:
                self._emit(EventType.LOG, "collecting inventory for debloat plan")
                inv = collect_inventory(
                    self.backend(), CollectOptions(detect_processes=False)
                )
                self.inventory = inv
            steps = build_debloat(selected, inv)
            title = f"{self.brand} debloat and policy operations"
            fname = "debloat.ps1"
        else:
            raise ValueError(f"unknown maintenance kind {kind!r}")
        target = Path(out_dir) if out_dir else self.out_root / kind
        text = render_maintenance_script(
            steps, title=title, generated_at=_now(), brand=self.brand
        )
        target.mkdir(parents=True, exist_ok=True)
        write_powershell(target / fname, text)
        irreversible = sum(1 for s in steps if not s.reversible and not s.is_manual)
        self.last_script = {
            "path": str(target / fname),
            "kind": kind,
            "steps": len(steps),
            "irreversible": irreversible,
            "title": title,
        }
        self._emit(
            EventType.SCRIPT_DONE,
            f"wrote {target / fname} ({len(steps)} step(s), {irreversible} irreversible)",
            data=dict(self.last_script),
            progress=1.0,
        )

    def start_registry(
        self, *, payload: str | Path, out_dir: str | Path | None = None
    ) -> bool:
        return self._run(
            "registry", lambda: self._registry(payload=payload, out_dir=out_dir)
        )

    def _registry(
        self, *, payload: str | Path, out_dir: str | Path | None
    ) -> None:
        from engine import render_maintenance_script, write_powershell
        from maintenance_utils import load_registry_payload, registry_steps

        changes = load_registry_payload(payload)
        steps = registry_steps(changes)
        target = Path(out_dir) if out_dir else self.out_root / "registry"
        text = render_maintenance_script(
            steps, title=f"{self.brand} registry payload", generated_at=_now(),
            brand=self.brand,
        )
        target.mkdir(parents=True, exist_ok=True)
        write_powershell(target / "registry.ps1", text)
        self.last_script = {
            "path": str(target / "registry.ps1"),
            "kind": "registry",
            "steps": len(steps),
            "irreversible": 0,
            "title": f"{self.brand} registry payload",
        }
        self._emit(
            EventType.SCRIPT_DONE,
            f"validated {len(changes)} change(s); wrote {target / 'registry.ps1'}",
            data=dict(self.last_script),
            progress=1.0,
        )


def _reconstruct_from_percentiles(d: dict[str, Any]) -> list[float]:
    p50, p95, p99 = d.get("p50"), d.get("p95"), d.get("p99")
    n = int(d.get("n") or 100)
    if p50 is None:
        return []
    out: list[float] = []
    for i in range(n):
        frac = i / max(1, n - 1)
        if frac < 0.5:
            out.append(float(p50))
        elif frac < 0.95:
            out.append(float(p50) if p95 is None else float(p95))
        else:
            out.append(float(p95) if p99 is None else float(p99))
    return out
