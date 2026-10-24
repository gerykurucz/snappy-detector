# ===== merged from cleanup.py =====
from __future__ import annotations

from dataclasses import dataclass

from models import Step, StepKind


def _ps_name(value):
    return str(value).replace("'", "''")


def _rx_lit(value):
    return str(value).replace("\\", "\\\\").replace(".", "\\.")


@dataclass(slots=True)
class CleanupOp:
    id: str
    title: str
    summary: str
    group: str = "standard"
    safety: str = ""
    targets: tuple[str, ...] = ()


CLEANUP_OPERATIONS: tuple[CleanupOp, ...] = (
    CleanupOp(
        id="temp-files",
        title="Temporary files",
        summary="User and Windows temp folders older than 7 days",
        group="standard",
        safety="Only files older than 7 days; nothing touched in the last week.",
        targets=("$env:TEMP", "$env:SystemRoot\\Temp"),
    ),
    CleanupOp(
        id="update-cache",
        title="Windows Update download cache",
        summary="Stale update payloads kept after installation",
        group="standard",
        safety="Only the download cache; the update history stays.",
        targets=("$env:SystemRoot\\SoftwareDistribution\\Download",),
    ),
    CleanupOp(
        id="shader-cache",
        title="DirectX shader cache",
        summary="Compiled shaders; games rebuild them on next launch",
        group="standard",
        safety="Shaders recompile automatically; first launch after cleanup is slower.",
        targets=("$env:LOCALAPPDATA\\D3DSCache",),
    ),
    CleanupOp(
        id="delivery-opt",
        title="Delivery Optimization cache",
        summary="Peer-to-peer update files shared with other PCs",
        group="standard",
        safety="Only the shared payload cache; settings are untouched.",
        targets=(
            "$env:SystemRoot\\ServiceProfiles\\NetworkService\\AppData\\Local"
            "\\Microsoft\\Windows\\DeliveryOptimization\\Cache",
        ),
    ),
    CleanupOp(
        id="recycle-bin",
        title="Recycle Bin",
        summary="Everything currently in the Recycle Bin",
        group="opt-in",
        safety="Bin contents are listed first; files cannot be recovered afterwards.",
        targets=("RecycleBin",),
    ),
    CleanupOp(
        id="event-logs",
        title="Windows event logs",
        summary="Event log files that are not locked by live channels",
        group="opt-in",
        safety="Live channel files are locked by Windows and survive; "
               "archived and unlocked .evtx files are deleted. The event "
               "channels keep running.",
        targets=("$env:SystemRoot\\System32\\winevt\\Logs",),
    ),
)

NOT_DONE: dict[str, str] = {
    "system-restore": "Restore points can rescue a bad driver; cleanup never touches them.",
    "winsxs": "The component store is deduplicated by Windows; manual deletion breaks servicing.",
    "prefetch": "Prefetch trains boot layout; deleting it slows launches with no latency gain.",
}

_CLEANUP_BY_ID: dict[str, CleanupOp] = {o.id: o for o in CLEANUP_OPERATIONS}


def _survey_command(op: CleanupOp) -> str:
    if op.id == "recycle-bin":
        return (
            "$s = (New-Object -ComObject Shell.Application).NameSpace(10).Items() | "
            "Measure-Object; Write-Host ('Recycle Bin holds ' + $s.Count + ' item(s)') "
            "-ForegroundColor Cyan"
        )
    paths = " @(" + ", ".join(f"'{t}'" for t in op.targets) + ")"
    return (
        "$total = 0; "
        f"foreach ($p in{paths}) {{ "
        "$e = $ExecutionContext.InvokeCommand.ExpandString($p); "
        "if (Test-Path -LiteralPath $e) { "
        "$m = Get-ChildItem -LiteralPath $e -Recurse -File -Force "
        "-ErrorAction SilentlyContinue "
        "| Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } "
        "| Measure-Object Length -Sum; $total += ($m.Sum | ForEach-Object { $_ }) } }; "
        f"Write-Host ('[{op.id}] reclaimable older than 7 days: ' + "
        "[math]::Round($total / 1MB, 1) + ' MB') -ForegroundColor Cyan"
    )


def _delete_command(op: CleanupOp) -> str:
    if op.id == "recycle-bin":
        return "Clear-RecycleBin -Force -ErrorAction Stop"
    if op.id == "event-logs":
        return (
            "Get-ChildItem -LiteralPath $env:SystemRoot\\System32\\winevt\\Logs "
            "-Filter *.evtx -ErrorAction SilentlyContinue | Remove-Item -Force "
            "-ErrorAction SilentlyContinue; "
            "Write-Host 'Locked live-channel logs were skipped.' "
            "-ForegroundColor DarkGray"
        )
    paths = " @(" + ", ".join(f"'{t}'" for t in op.targets) + ")"
    return (
        f"foreach ($p in{paths}) {{ "
        "$e = $ExecutionContext.InvokeCommand.ExpandString($p); "
        "if (Test-Path -LiteralPath $e) { "
        "Get-ChildItem -LiteralPath $e -Recurse -File -Force "
        "-ErrorAction SilentlyContinue "
        "| Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } "
        "| Remove-Item -Force -ErrorAction Stop } }"
    )


def build_cleanup_steps(
    selected: tuple[str, ...] | None = None,
    include_opt_in: bool = False,
    survey_only: bool = False,
) -> list[Step]:
    if selected is None:
        ops = [
            o
            for o in CLEANUP_OPERATIONS
            if o.group == "standard" or (include_opt_in and o.group == "opt-in")
        ]
    else:
        unknown = [i for i in selected if i not in _CLEANUP_BY_ID]
        if unknown:
            raise ValueError(f"unknown cleanup operations: {', '.join(unknown)}")
        ops = [_CLEANUP_BY_ID[i] for i in selected]
    steps: list[Step] = []
    for op in ops:
        if survey_only:
            steps.append(
                Step(
                    kind=StepKind.POWERSHELL,
                    description=f"Survey {op.title}: {op.summary}",
                    command=_survey_command(op),
                    terminal=True,
                    notes="Survey only: reports reclaimable size, deletes nothing.",
                )
            )
        else:
            steps.append(
                Step(
                    kind=StepKind.FILE_DELETE,
                    description=f"Clean {op.title}: {op.summary}",
                    command=_delete_command(op),
                    reversible=False,
                    safety=op.safety,
                )
            )
    return steps


def cleanup_report() -> list[dict]:
    return [
        {
            "id": "cleanup",
            "title": "Disk cleanup",
            "summary": "Frees disk space. Not a latency change.",
            "items": [
                {
                    "id": o.id,
                    "description": f"{o.title}: {o.summary}",
                    "reversible": False,
                    "safety": o.safety,
                }
                for o in CLEANUP_OPERATIONS
            ],
            "notes": [f"Deliberately not done: {k}: {v}" for k, v in NOT_DONE.items()],
        }
    ]


# ===== merged from memory.py =====


@dataclass(slots=True)
class MemoryOp:
    id: str
    title: str
    summary: str
    reversible: bool = True
    safety: str = ""
    schedulable: bool = False
    needs_process_arg: bool = False


MEMORY_OPERATIONS: tuple[MemoryOp, ...] = (
    MemoryOp(
        id="memory.measure",
        title="Report memory state",
        summary="Available megabytes, standby cache, and modified pages",
    ),
    MemoryOp(
        id="memory.purge_standby",
        title="Purge the standby list",
        summary="Reclaims cached pages Windows would free on demand anyway",
        reversible=False,
        safety="Standby pages are cache; applications fault them back on access.",
        schedulable=True,
    ),
    MemoryOp(
        id="memory.purge_standby_low",
        title="Purge the low-priority standby list",
        summary="Reclaims only the lowest-priority cached pages",
        reversible=False,
        safety="Low-priority cache only; hot cache is preserved.",
        schedulable=True,
    ),
    MemoryOp(
        id="memory.flush_modified",
        title="Flush the modified page list",
        summary="Writes dirty pages out so their frames become available",
        reversible=False,
        safety="Pages are written to disk first; no data is discarded.",
        schedulable=True,
    ),
    MemoryOp(
        id="memory.trim_process",
        title="Trim working sets of named processes",
        summary="On-demand trim of processes named at generation time",
        reversible=False,
        safety="Trimmed pages fault back on access; a short burst of faults follows.",
        needs_process_arg=True,
    ),
)

_MEMORY_BY_ID: dict[str, MemoryOp] = {o.id: o for o in MEMORY_OPERATIONS}

_COMMANDS = {
    "memory.measure": (
        "$c = Get-Counter '\\Memory\\Available MBytes' -ErrorAction SilentlyContinue; "
        "$m = ($c.CounterSamples | Select-Object -First 1).CookedValue; "
        "if ($null -eq $m) { $m = (Get-CimInstance Win32_OperatingSystem)."
        "FreePhysicalMemory / 1KB }; "
        "Write-Host ('Available memory: ' + [math]::Round($m, 1) + ' MB') "
        "-ForegroundColor Cyan"
    ),
    "memory.purge_standby": (
        "$v = 4; [MemClean]::NtSetSystemInformation(80, [ref]$v, 4) | Out-Null"
    ),
    "memory.purge_standby_low": (
        "$v = 5; [MemClean]::NtSetSystemInformation(80, [ref]$v, 4) | Out-Null"
    ),
    "memory.flush_modified": (
        "$v = 3; [MemClean]::NtSetSystemInformation(80, [ref]$v, 4) | Out-Null"
    ),
}

_PREAMBLE = (
    "$sig = 'using System; using System.Runtime.InteropServices; "
    "public static class MemClean { "
    "[DllImport(\"ntdll.dll\")] "
    "public static extern int NtSetSystemInformation(int c, ref int i, int l); }'; "
    "Add-Type -TypeDefinition $sig -ErrorAction Stop"
)


def build_memory_steps(
    selected: tuple[str, ...] = ("memory.measure",),
    process_names: tuple[str, ...] = (),
) -> list[Step]:
    unknown = [i for i in selected if i not in _MEMORY_BY_ID]
    if unknown:
        raise ValueError(f"unknown memory operations: {', '.join(unknown)}")
    if "memory.trim_process" in selected and not process_names:
        raise ValueError("memory.trim_process needs at least one process name")
    steps: list[Step] = []
    needs_api = any(s != "memory.measure" for s in selected)
    if needs_api:
        steps.append(
            Step(
                kind=StepKind.MEMORY_ACTION,
                description="Load the memory-management API",
                command=_PREAMBLE,
                terminal=True,
            )
        )
    for sid in selected:
        op = _MEMORY_BY_ID[sid]
        if sid == "memory.measure":
            steps.append(
                Step(
                    kind=StepKind.MEMORY_ACTION,
                    description=f"{op.title}: {op.summary}",
                    command=_COMMANDS[sid],
                    terminal=True,
                )
            )
        elif sid == "memory.trim_process":
            for proc in process_names:
                steps.append(
                    Step(
                        kind=StepKind.MEMORY_ACTION,
                        description=f"Trim working set of {proc}",
                        command=(
                            f"Get-Process -Name '{_ps_name(proc)}' -ErrorAction Stop | "
                            "ForEach-Object { $_.TrimWorkingSet() }"
                        ),
                        reversible=False,
                        safety=op.safety,
                    )
                )
        else:
            steps.append(
                Step(
                    kind=StepKind.MEMORY_ACTION,
                    description=f"{op.title}: {op.summary}",
                    command=_COMMANDS[sid],
                    reversible=False,
                    safety=op.safety,
                )
            )
    return steps


def memory_report() -> list[dict]:
    return [
        {
            "id": "memory",
            "title": "Memory",
            "summary": "Reports state; purge and trim are on-demand only, never scheduled.",
            "items": [
                {
                    "id": o.id,
                    "description": f"{o.title}: {o.summary}",
                    "reversible": o.reversible,
                    "safety": o.safety,
                }
                for o in MEMORY_OPERATIONS
            ],
            "notes": [
                "Continuous working-set trimming raises hard page faults "
                "instead of helping."
            ],
        }
    ]


# ===== merged from power.py =====


@dataclass(slots=True)
class PowerOp:
    id: str
    title: str
    summary: str
    survey: bool = False


POWER_OPERATIONS: tuple[PowerOp, ...] = (
    PowerOp(
        id="power.survey",
        title="Report parked cores and energy settings",
        summary="Reads current core-parking and EPP values without changing them",
        survey=True,
    ),
    PowerOp(
        id="power.core_parking.disable",
        title="Disable CPU core parking",
        summary="Keeps all cores unparked so wake latency never applies",
    ),
    PowerOp(
        id="power.epp.max_performance",
        title="Set energy performance preference to maximum",
        summary="Prefers performance over efficiency on AC and DC power",
    ),
)

_POWER_BY_ID: dict[str, PowerOp] = {o.id: o for o in POWER_OPERATIONS}

_SURVEY_COMMAND = (
    "$parked = (Get-CimInstance Win32_Processor | "
    "Measure-Object NumberOfCores -Sum).Sum; "
    "$schemes = powercfg /list; "
    "Write-Host ('Cores visible: ' + $parked) -ForegroundColor Cyan; "
    "Write-Host '[survey] active scheme and parking values follow:' "
    "-ForegroundColor DarkGray; "
    "powercfg /query SCHEME_CURRENT SUB_PROCESSOR CPMINCORES; "
    "powercfg /query SCHEME_CURRENT SUB_PROCESSOR PERFEPP"
)


def build_power_steps(
    selected: tuple[str, ...] | None = None,
    include_opt_in: bool = False,
) -> list[Step]:
    _ = include_opt_in
    if selected is None:
        ops = [o for o in POWER_OPERATIONS if not o.survey]
    else:
        unknown = [i for i in selected if i not in _POWER_BY_ID]
        if unknown:
            raise ValueError(f"unknown power operations: {', '.join(unknown)}")
        ops = [_POWER_BY_ID[i] for i in selected]
    steps: list[Step] = []
    for op in ops:
        if op.survey or op.id == "power.survey":
            steps.append(
                Step(
                    kind=StepKind.POWERCFG,
                    description=f"{op.title} [survey]",
                    command=_SURVEY_COMMAND,
                    terminal=True,
                )
            )
        elif op.id == "power.core_parking.disable":
            steps.append(
                Step(
                    kind=StepKind.POWERCFG,
                    description=f"{op.title}: {op.summary}",
                    command=(
                        "powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR "
                        "CPMINCORES 100; "
                        "powercfg /setdcvalueindex SCHEME_CURRENT SUB_PROCESSOR "
                        "CPMINCORES 100; "
                        "powercfg /setactive SCHEME_CURRENT"
                    ),
                    undo=(
                        Step(
                            kind=StepKind.POWERCFG,
                            description="Restore default core parking",
                            command=(
                                "powercfg /setacvalueindex SCHEME_CURRENT "
                                "SUB_PROCESSOR CPMINCORES 10; "
                                "powercfg /setdcvalueindex SCHEME_CURRENT "
                                "SUB_PROCESSOR CPMINCORES 10; "
                                "powercfg /setactive SCHEME_CURRENT"
                            ),
                            terminal=True,
                        ),
                    ),
                    notes="Raises idle power draw; heat and fan noise may rise.",
                )
            )
        elif op.id == "power.epp.max_performance":
            steps.append(
                Step(
                    kind=StepKind.POWERCFG,
                    description=f"{op.title}: {op.summary}",
                    command=(
                        "powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR "
                        "PERFEPP 0; "
                        "powercfg /setdcvalueindex SCHEME_CURRENT SUB_PROCESSOR "
                        "PERFEPP 0; "
                        "powercfg /setactive SCHEME_CURRENT"
                    ),
                    undo=(
                        Step(
                            kind=StepKind.POWERCFG,
                            description="Restore balanced EPP",
                            command=(
                                "powercfg /setacvalueindex SCHEME_CURRENT "
                                "SUB_PROCESSOR PERFEPP 50; "
                                "powercfg /setdcvalueindex SCHEME_CURRENT "
                                "SUB_PROCESSOR PERFEPP 50; "
                                "powercfg /setactive SCHEME_CURRENT"
                            ),
                            terminal=True,
                        ),
                    ),
                    notes="CPU boosts more aggressively; battery life drops.",
                )
            )
    return steps



# ===== merged from registry.py =====

import json
from pathlib import Path
from typing import Any

from system_core import TweakValidationError
from models import reg_set

_HIVES = {
    "HKLM": "HKLM",
    "HKEY_LOCAL_MACHINE": "HKLM",
    "HKCU": "HKCU",
    "HKEY_CURRENT_USER": "HKCU",
}

_TYPES = {
    "REG_DWORD": "DWord",
    "DWORD": "DWord",
    "REG_SZ": "String",
    "SZ": "String",
}

_ROOT_NAMES = {
    "HKCU", "HKEY_CURRENT_USER", "HKLM", "HKEY_LOCAL_MACHINE", "HKCR",
    "HKEY_CLASSES_ROOT", "HKU", "HKEY_USERS", "HKCC", "HKEY_CURRENT_CONFIG",
}

_FIELDS = {"hive", "key_path", "name", "value_type", "value"}


@dataclass(slots=True)
class RegistryChange:
    hive: str
    key_path: str
    name: str
    value_type: str
    value: Any


def normalize_registry_changes(payload: Any) -> list[RegistryChange]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise TweakValidationError(
                f"registry payload contains invalid JSON: {exc}"
            ) from exc
    if isinstance(payload, dict):
        if set(payload) not in ({"modifications"}, {"items"}):
            raise TweakValidationError(
                "registry payload must contain only 'modifications' or 'items'"
            )
        rows = next(iter(payload.values()))
        if not isinstance(rows, list):
            raise TweakValidationError(
                "registry payload must contain a list in 'modifications' or 'items'"
            )
    elif isinstance(payload, list):
        rows = payload
    else:
        raise TweakValidationError("registry payload must be a list or object")
    out: list[RegistryChange] = []
    seen: set[tuple[str, str, str]] = set()
    for idx, raw in enumerate(rows):
        label = f"registry_changes[{idx}]"
        if not isinstance(raw, dict) or set(raw) != _FIELDS:
            raise TweakValidationError(
                f"{label} must contain exactly hive, key_path, name, value_type, "
                f"and value"
            )
        hive = _HIVES.get(str(raw["hive"]).strip().upper(), "")
        if not hive:
            raise TweakValidationError(f"{label}: hive must be HKLM or HKCU")
        if not isinstance(raw["key_path"], str) or not isinstance(raw["name"], str):
            raise TweakValidationError(f"{label}: key_path and name must be strings")
        key_path, name = raw["key_path"].strip(), raw["name"].strip()
        segments = key_path.split("\\")
        if (
            not key_path
            or len(key_path) > 255
            or any(ord(c) < 32 for c in key_path)
            or "/" in key_path
            or any(part.strip() in ("", ".", "..") for part in segments)
            or segments[0].upper().rstrip(":") in _ROOT_NAMES
        ):
            raise TweakValidationError(
                f"{label}: key_path must be a valid path relative to the hive"
            )
        if not name or len(name) > 16383 or any(ord(c) < 32 for c in name):
            raise TweakValidationError(
                f"{label}: name must be a nonempty registry value name"
            )
        vtype = _TYPES.get(str(raw["value_type"]).strip().upper(), "")
        if not vtype:
            raise TweakValidationError(f"{label}: value_type must be REG_DWORD or REG_SZ")
        value = raw["value"]
        if vtype == "DWord":
            if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
                raise TweakValidationError(
                    f"{label}: REG_DWORD value must be an integer from 0 to 4294967295"
                )
        elif not isinstance(value, str) or "\0" in value:
            raise TweakValidationError(
                f"{label}: REG_SZ value must be a string without null characters"
            )
        target = (hive, key_path.casefold(), name.casefold())
        if target in seen:
            raise TweakValidationError(f"{label}: duplicate registry target")
        seen.add(target)
        out.append(
            RegistryChange(
                hive=hive, key_path=key_path, name=name, value_type=vtype,
                value=value,
            )
        )
    if not out:
        raise TweakValidationError("registry payload is empty")
    return out


def load_registry_payload(path: str | Path) -> list[RegistryChange]:
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise TweakValidationError(f"cannot read registry payload {p}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise TweakValidationError(f"registry payload {p} is not JSON: {exc}") from exc
    return normalize_registry_changes(data)


def registry_steps(changes: list[RegistryChange]) -> list[Step]:
    steps: list[Step] = []
    for c in changes:
        steps.append(
            reg_set(
                c.hive,
                c.key_path,
                c.name,
                c.value,
                value_type=c.value_type,
                notes="From a user-supplied registry payload; validated before generation.",
            )
        )
    return steps


# ===== merged from host.py =====

import base64

from system_core import (TIMER_TARGET_US, BudgetComponent, Confidence, Risk,
                         TimerResolution, Verdict, clamp_timer_target,
                         query_timer_resolution)
from models import Finding, Tweak, manual, ps_step

_TIMER_RUN_NAME = "SnappyAI Timer Resolution"
_TIMER_RUN_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_TIMER_MIN_UNITS = 100
_TIMER_MAX_UNITS = 200000


def resident_timer_command(units: int) -> str:
    """The hidden logon command that holds a finer system tick.

    A timer request belongs to the process that made it and is dropped the
    moment that process exits, which is why this is a resident hidden
    powershell rather than a one-shot call. -EncodedCommand keeps the whole
    body out of the quoting chain: the Run value, the generated apply.ps1 and
    the C# source all stay literal, so nothing needs escaping twice.
    """
    units = int(units)
    if not _TIMER_MIN_UNITS <= units <= _TIMER_MAX_UNITS:
        raise ValueError(
            f"timer resolution {units} is outside "
            f"{_TIMER_MIN_UNITS}..{_TIMER_MAX_UNITS} (100 ns units)"
        )
    body = (
        "$sig = @'\n"
        "using System;\n"
        "using System.Runtime.InteropServices;\n"
        "public static class SnappyAITimer {\n"
        "  [DllImport(\"ntdll.dll\")]\n"
        "  public static extern int NtSetTimerResolution(int Desired, byte Set, ref int Current);\n"
        "  [DllImport(\"ntdll.dll\")]\n"
        "  public static extern int NtQueryTimerResolution(ref int Min, ref int Max, ref int Cur);\n"
        "  [DllImport(\"kernel32.dll\", SetLastError = true)]\n"
        "  public static extern bool SetProcessInformation(IntPtr h, int c, ref T s, int n);\n"
        "  [StructLayout(LayoutKind.Sequential)]\n"
        "  public struct T { public uint Version; public uint ControlMask; public uint StateMask; }\n"
        "}\n"
        "'@\n"
        "Add-Type -TypeDefinition $sig -ErrorAction Stop\n"
        "$t = New-Object SnappyAITimer+T\n"
        "$t.Version = 1\n"
        "$t.ControlMask = 4\n"
        "$t.StateMask = 0\n"
        "[void][SnappyAITimer]::SetProcessInformation([IntPtr](-1), 4, [ref]$t, "
        "[Runtime.InteropServices.Marshal]::SizeOf($t))\n"
        "$lo = 0\n"
        "$hi = 0\n"
        "$now = 0\n"
        "[void][SnappyAITimer]::NtQueryTimerResolution([ref]$lo, [ref]$hi, [ref]$now)\n"
        f"$want = {units}\n"
        "if ($lo -gt 0 -and $want -lt $lo) { $want = $lo }\n"
        "if ($hi -gt 0 -and $want -gt $hi) { $want = $hi }\n"
        "$cur = 0\n"
        "$st = [SnappyAITimer]::NtSetTimerResolution($want, [byte]1, [ref]$cur)\n"
        "if ($st -ne 0) { exit $st }\n"
        "while ($true) { Start-Sleep -Seconds 3600 }\n"
    )
    encoded = base64.b64encode(body.encode("utf-16-le")).decode("ascii")
    return f"powershell -NoProfile -WindowStyle Hidden -EncodedCommand {encoded}"


def _timer_probe(inv: Any) -> Finding:
    tick = inv.timer.current_period_us
    if tick is None:
        return Finding(
            verdict=Verdict.UNKNOWN, summary="timer resolution could not be read"
        )
    floor = inv.timer.min_period_us
    measured = {
        "tick_us": tick,
        "finest_us": floor,
        "coarsest_us": inv.timer.max_period_us,
        "global_timer_requests": inv.timer.global_timer_resolution_requests,
    }
    if floor is None:
        return Finding(
            verdict=Verdict.UNKNOWN,
            summary=f"system tick is {tick / 1000.0:.3f} ms but the platform floor "
                    "could not be read",
            measured=measured,
        )
    if tick <= floor + 1.0:
        return Finding(
            verdict=Verdict.NO_PROBLEM,
            summary=f"system tick is already the finest this platform allows "
                    f"({floor / 1000.0:.3f} ms)",
            measured=measured,
        )
    if floor > TIMER_TARGET_US:
        return Finding(
            verdict=Verdict.NO_PROBLEM,
            summary=f"system tick is {tick / 1000.0:.3f} ms and the platform floor is "
                    f"{floor / 1000.0:.3f} ms, so 0.5 ms is not reachable here",
            measured=measured,
        )
    if tick >= 10000:
        return Finding(
            verdict=Verdict.PROBLEM,
            summary=f"system tick is {tick / 1000.0:.3f} ms; scheduling quantum is coarse",
            measured=measured,
        )
    return Finding(
        verdict=Verdict.PROBLEM,
        summary=f"system tick is {tick / 1000.0:.3f} ms while this platform allows "
                f"{floor / 1000.0:.3f} ms",
        measured=measured,
    )


def _timer_plan(inv: Any) -> list[Step]:
    floor = getattr(inv.timer, "min_period_us", None) if inv is not None else None
    if floor:
        res = TimerResolution(min_us=floor)
    else:
        res = query_timer_resolution()
    units = clamp_timer_target(TIMER_TARGET_US, res)
    command = resident_timer_command(units)
    encoded = command.rsplit(" ", 1)[-1]
    return [
        ps_step(
            f"Request a {units / 10000.0:.4f} ms system tick at every logon",
            (
                "$p = 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run'\n"
                "if (-not (Test-Path -LiteralPath $p)) {\n"
                "    New-Item -Path $p -Force | Out-Null\n"
                "}\n"
                f"$old = (Get-ItemProperty -LiteralPath $p -Name '{_TIMER_RUN_NAME}' "
                f"-ErrorAction SilentlyContinue).'{_TIMER_RUN_NAME}'\n"
                f"if ($old -and $old -ne '{command}') {{\n"
                "    $oldb64 = ($old -split '-EncodedCommand')[-1].Trim()\n"
                "    if ($oldb64) {\n"
                "        Get-CimInstance Win32_Process | Where-Object { "
                "$_.CommandLine -like ('*' + $oldb64 + '*') } | ForEach-Object { "
                "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }\n"
                "    }\n"
                "}\n"
                f"Set-ItemProperty -LiteralPath $p -Name '{_TIMER_RUN_NAME}' `\n"
                f"    -Value '{command}' -Type String\n"
                "Start-Process powershell -WindowStyle Hidden -ArgumentList "
                f"'-NoProfile','-WindowStyle','Hidden','-EncodedCommand','{encoded}'"
            ),
            (
                "Get-CimInstance Win32_Process | "
                f"Where-Object {{ $_.CommandLine -like '*{encoded}*' }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                "-ErrorAction SilentlyContinue }\n"
                "$p = 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run'\n"
                f"Remove-ItemProperty -LiteralPath $p -Name '{_TIMER_RUN_NAME}' "
                "-ErrorAction SilentlyContinue"
            ),
            notes=(
                "Windows drops a timer request when the requesting process exits, "
                "so a hidden helper holds it from logon. It needs no administrator "
                "rights and raising the tick rate increases idle power draw."
            ),
        )
    ]

def _usb_probe(inv: Any) -> Finding:
    state = getattr(inv, "usb_selective_suspend", None)
    if state is None:
        return Finding(
            verdict=Verdict.UNKNOWN,
            summary="USB selective suspend state could not be read",
        )
    if state:
        return Finding(
            verdict=Verdict.PROBLEM,
            summary="USB selective suspend is enabled on the active power plan",
        )
    return Finding(
        verdict=Verdict.NO_PROBLEM,
        summary="USB selective suspend is already disabled",
    )


_USB_SUBGROUP = "2a737441-1930-4402-8d77-b2bebba308a3"
_USB_SUSPEND_SETTING = "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"


def _usb_plan(inv: Any) -> list[Step]:
    _ = inv
    disable = (
        "powercfg /setacvalueindex SCHEME_CURRENT SUB_USB USBSELECTSUSPEND 0; "
        "powercfg /setdcvalueindex SCHEME_CURRENT SUB_USB USBSELECTSUSPEND 0; "
        "powercfg /setactive SCHEME_CURRENT"
    )
    restore = (
        "powercfg /setacvalueindex SCHEME_CURRENT SUB_USB USBSELECTSUSPEND 1; "
        "powercfg /setdcvalueindex SCHEME_CURRENT SUB_USB USBSELECTSUSPEND 1; "
        "powercfg /setactive SCHEME_CURRENT"
    )
    return [
        Step(
            kind=StepKind.POWERCFG,
            description="Disable USB selective suspend on the active power plan",
            command=disable,
            undo=(
                Step(
                    kind=StepKind.POWERCFG,
                    description="Restore USB selective suspend",
                    command=restore,
                    terminal=True,
                ),
            ),
            notes="Keeps mice and keyboards awake so polls never wait for a resume.",
        )
    ]


def _aspm_probe(inv: Any) -> Finding:
    _ = inv
    return Finding(
        verdict=Verdict.UNKNOWN,
        summary="PCIe link power state cannot be read without elevated ACPI queries",
    )


def _aspm_plan(inv: Any) -> list[Step]:
    _ = inv
    return [
        manual(
            "Disable PCIe link power saving for the gaming slots",
            "1. Open the BIOS or UEFI setup and find PCI Express power settings\n"
            "2. Set ASPM to Disabled for the GPU slot\n"
            "3. In Windows, set the power plan PCI Express Link State to Off\n"
            "4. Reboot and confirm the setting with the latency monitor",
            notes="Each link retrain costs microseconds on every transaction burst.",
        )
    ]


def _mmcss_probe(inv: Any) -> Finding:
    if not inv.timer.mmcss_responsiveness:
        return Finding(
            verdict=Verdict.PROBLEM,
            summary="no multimedia scheduling profile prioritises the game task",
        )
    return Finding(
        verdict=Verdict.NO_PROBLEM, summary="multimedia scheduling profile present"
    )


def _mmcss_plan(inv: Any) -> list[Step]:
    _ = inv
    return [
        reg_set(
            "HKLM",
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games",
            "GPU Priority",
            8,
            notes="Raises the GPU scheduling priority of threads tagged as games.",
        ),
        reg_set(
            "HKLM",
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games",
            "Priority",
            6,
            notes="Raises the CPU priority class of the games scheduling category.",
        ),
        reg_set(
            "HKLM",
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games",
            "Scheduling Category",
            "High",
            value_type="String",
            notes="Moves game threads into the high scheduling category.",
        ),
    ]


def _background_probe(inv: Any) -> Finding:
    hot = [
        (n, c)
        for n, c in inv.top_cpu_processes
        if c > 5.0 and n.lower() != (inv.foreground_process or "").lower()
    ]
    if hot:
        names = ", ".join(f"{n} ({c:.1f}%)" for n, c in hot[:3])
        return Finding(
            verdict=Verdict.PROBLEM,
            summary=f"background load competes with the game: {names}",
            measured={"hot": hot[:3]},
        )
    if not inv.top_cpu_processes:
        return Finding(
            verdict=Verdict.UNKNOWN, summary="process sampling produced no rows"
        )
    return Finding(
        verdict=Verdict.NO_PROBLEM, summary="no heavy background competitor seen"
    )


def _background_plan(inv: Any) -> list[Step]:
    hot = [
        n
        for n, c in inv.top_cpu_processes
        if c > 5.0 and n.lower() != (inv.foreground_process or "").lower()
    ]
    target = hot[0] if hot else "background process"
    base = target[:-4] if target.lower().endswith(".exe") else target
    return [
        ps_step(
            f"Lower {target} to BelowNormal priority while gaming",
            f"Get-Process -Name '{_ps_name(base)}' -ErrorAction SilentlyContinue | "
            "ForEach-Object { $_.PriorityClass = 'BelowNormal' }",
            "Get-Process -Name '" + _ps_name(base) + "' "
            "-ErrorAction SilentlyContinue | "
            "ForEach-Object { $_.PriorityClass = 'Normal' }",
            notes="Transient only; priorities reset when the process restarts.",
        )
    ]


def _affinity_probe(inv: Any) -> Finding:
    _ = inv
    return Finding(
        verdict=Verdict.UNKNOWN,
        summary="interrupt-to-core routing cannot be read without elevated queries",
    )


def _affinity_plan(inv: Any) -> list[Step]:
    _ = inv
    return [
        manual(
            "Move device interrupts off the cores the game uses",
            "1. Open Device Manager > network adapter > Properties > Advanced\n"
            "2. Note the MSI interrupt, then open the registry Interrupt Management key\n"
            "3. Assign an affinity mask covering cores the game does not use\n"
            "4. Reboot and verify with the latency monitor under load",
            notes="Sharing a core between interrupts and the game thread adds jitter.",
        )
    ]


def _cpusets_probe(inv: Any) -> Finding:
    if inv.cpu.l3_domains > 1:
        return Finding(
            verdict=Verdict.PROBLEM,
            summary=f"CPU has {inv.cpu.l3_domains} cache domains; threads may roam",
            measured={"l3_domains": inv.cpu.l3_domains},
        )
    return Finding(
        verdict=Verdict.NO_PROBLEM, summary="single cache domain; roaming is cheap"
    )


def _cpusets_plan(inv: Any) -> list[Step]:
    _ = inv
    return [
        manual(
            "Pin the game process to one cache domain",
            "1. Open Task Manager > Details, right-click the game > Set affinity\n"
            "2. Keep only the cores of one CCD or one performance cluster ticked\n"
            "3. Launch the game after setting affinity each session\n"
            "4. Compare p95 against the unpinned baseline before keeping it",
            notes="Cross-domain hops turn cache hits into memory fetches.",
        )
    ]


HOST_TWEAKS: tuple[Tweak, ...] = (
    Tweak(
        id="input.usb.selective_suspend_off",
        title="USB devices sleep between inputs",
        component=BudgetComponent.HOST_PROCESSING,
        mechanism="Selective suspend parks human-interface devices in a low-power state after milliseconds of idleness, and the first input after a pause waits for USB resume signalling before the game thread is even woken.",
        confidence=Confidence.MEDIUM,
        risk=Risk.LOW,
        summary="USB selective suspend enabled.",
        probe=_usb_probe,
        plan=_usb_plan,
        expected_effect="First input after idle responds without a resume stall.",
    ),
    Tweak(
        id="bus.pcie.aspm_off",
        title="PCI Express links sleep between transactions",
        component=BudgetComponent.HOST_PROCESSING,
        mechanism="Active-state power management drops idle PCIe links into low-power states whose exit latency stalls GPU and NIC traffic bursts, turning sporadic transfers into periodic latency spikes under variable game load.",
        confidence=Confidence.LOW,
        risk=Risk.LOW,
        summary="ASPM state unreadable.",
        probe=_aspm_probe,
        plan=_aspm_plan,
        expected_effect="Removes link-retrain stalls from bursty device traffic.",
    ),
    Tweak(
        id="timer.global_resolution_requests",
        title="Coarse Windows scheduling tick",
        component=BudgetComponent.HOST_PROCESSING,
        mechanism="The default 15.6 ms timer quantum rounds every Sleep, WaitForSingleObject timeout, and scheduler decision up to the next tick boundary, so frame pacing and network waits inherit millisecond-scale quantization error.",
        confidence=Confidence.HIGH,
        risk=Risk.LOW,
        summary="System tick is coarse.",
        probe=_timer_probe,
        plan=_timer_plan,
        expected_effect="Finer scheduling granularity for waits under a "
                        "millisecond inside the holding process.",
        measure_metric="p95",
        measure_min_effect_ms=0.4,
        caveats=(
            "Since Windows 10 2004 a timer request is PROCESS-scoped: the "
            "holder tightens its own waits, and other processes (including "
            "the game) only see a finer system tick when the experimental "
            "GlobalTimerResolutionRequests tweak is also enabled. A finer "
            "tick raises the clock interrupt rate, which increases idle "
            "power draw and can shorten battery life on laptops."
        ),
    ),
    Tweak(
        id="sched.mmcss.games_task",
        title="Multimedia scheduler ignores games",
        component=BudgetComponent.HOST_PROCESSING,
        mechanism="Threads registered under the Games multimedia profile receive elevated CPU priority, GPU priority, and clock-rate guarantees, and without that registration the game competes on equal terms with indexers and updaters.",
        confidence=Confidence.MEDIUM,
        risk=Risk.LOW,
        summary="Games scheduling profile missing.",
        probe=_mmcss_probe,
        plan=_mmcss_plan,
        expected_effect="Game threads preempt background work more reliably.",
    ),
    Tweak(
        id="sched.background.lower_priority",
        title="Background programs fight the game for CPU",
        component=BudgetComponent.HOST_PROCESSING,
        mechanism="A background process burning whole cores forces the scheduler to time-slice the game thread, and each preemption evicts cache and delays interrupt handling by the length of the competing quantum.",
        confidence=Confidence.MEDIUM,
        risk=Risk.LOW,
        summary="Background CPU contention seen.",
        probe=_background_probe,
        plan=_background_plan,
        expected_effect="Fewer preemptions while the heaviest competitor idles lower.",
    ),
    Tweak(
        id="cpu.interrupt_affinity.delegate",
        title="Device interrupts land on game cores",
        component=BudgetComponent.HOST_PROCESSING,
        mechanism="Every network and GPU interrupt preempts whatever runs on its target core, so routing those interrupts onto the same cores as the game thread injects jitter exactly where frame deadlines are tightest.",
        confidence=Confidence.MEDIUM,
        risk=Risk.LOW,
        summary="Interrupt routing unmeasured.",
        probe=_affinity_probe,
        plan=_affinity_plan,
        expected_effect="Game cores serve fewer interruptions per second.",
    ),
    Tweak(
        id="cpu.cpu_sets.delegate",
        title="Game jumps between CPU cache domains",
        component=BudgetComponent.HOST_PROCESSING,
        mechanism="Migrating a hot game thread across cache domains discards the warm L3 footprint, and refetching textures, physics state, and network buffers from memory stalls the thread for hundreds of nanoseconds per miss in bulk.",
        confidence=Confidence.LOW,
        risk=Risk.LOW,
        summary="Thread roaming risk judged.",
        probe=_cpusets_probe,
        plan=_cpusets_plan,
        expected_effect="Warmer caches and steadier frame times on multi-domain CPUs.",
    ),
)


# ===== merged from network.py =====

def _wifi_probe(inv: Any) -> Finding:
    if not inv.nics:
        return Finding(verdict=Verdict.UNKNOWN, summary="no network adapter was found")
    if inv.uses_wifi_for_primary:
        nic = inv.primary_nic
        return Finding(
            verdict=Verdict.PROBLEM,
            summary=f"primary adapter {nic.interface_description} is wireless",
            measured={"adapter": nic.interface_description},
        )
    return Finding(verdict=Verdict.NO_PROBLEM, summary="primary adapter is wired")


def _wifi_plan(inv: Any) -> list[Step]:
    _ = inv
    return [
        manual(
            "Switch the gaming PC from Wi-Fi to a wired Ethernet cable",
            "1. Connect an Ethernet cable from the PC to the router\n"
            "2. Open Settings > Network and confirm the Ethernet adapter shows Connected\n"
            "3. Disconnect Wi-Fi or set the Ethernet adapter metric lower than Wi-Fi\n"
            "4. Re-run the live latency monitor and compare p95 before and after",
            notes="No registry value reproduces a cable; the medium itself is the fix.",
        )
    ]


def _power_probe(inv: Any) -> Finding:
    if not inv.nics:
        return Finding(verdict=Verdict.UNKNOWN, summary="no network adapter was found")
    known = [n for n in inv.nics if n.allow_turn_off is not None]
    if not known:
        return Finding(
            verdict=Verdict.UNKNOWN,
            summary="adapter power management state could not be read",
        )
    on = [n for n in known if n.allow_turn_off]
    if on:
        return Finding(
            verdict=Verdict.PROBLEM,
            summary=(
                f"power management can turn off {len(on)} adapter(s): "
                + ", ".join(n.name or n.interface_description for n in on[:3])
            ),
            measured={"adapters": [n.name for n in on]},
        )
    return Finding(
        verdict=Verdict.NO_PROBLEM,
        summary="adapter power management is already disabled",
    )


def _power_plan(inv: Any) -> list[Step]:
    _ = inv
    return [
        ps_step(
            "Disable power management on all physical network adapters",
            "Get-NetAdapter -Physical | Disable-NetAdapterPowerManagement",
            "Get-NetAdapter -Physical | Enable-NetAdapterPowerManagement",
            notes="Prevents the NIC from sleeping between packet bursts.",
        )
    ]


def _moderation_probe(inv: Any) -> Finding:
    if not inv.nics:
        return Finding(verdict=Verdict.UNKNOWN, summary="no network adapter was found")
    known = [n for n in inv.nics if n.interrupt_moderation is not None]
    if not known:
        return Finding(
            verdict=Verdict.UNKNOWN,
            summary="interrupt moderation state could not be read "
                    "(the driver exposes no such property)",
        )
    on = [n for n in known if n.interrupt_moderation]
    if on:
        return Finding(
            verdict=Verdict.PROBLEM,
            summary=(
                f"interrupt moderation is enabled on {len(on)} adapter(s), "
                "batching packets and delaying each delivery to save CPU"
            ),
            measured={"adapters": [n.name for n in on]},
        )
    return Finding(
        verdict=Verdict.NO_PROBLEM,
        summary="interrupt moderation is already disabled",
    )


def _moderation_plan(inv: Any) -> list[Step]:
    _ = inv
    return [
        ps_step(
            "Disable interrupt moderation on all physical network adapters",
            "Get-NetAdapter -Physical | Set-NetAdapterAdvancedProperty "
            "-DisplayName 'Interrupt Moderation' -DisplayValue 'Disabled' -NoRestart",
            "Get-NetAdapter -Physical | Set-NetAdapterAdvancedProperty "
            "-DisplayName 'Interrupt Moderation' -DisplayValue 'Enabled' -NoRestart",
            notes="If the driver has no such property the step reports the failure openly.",
        )
    ]


def _dns_probe(inv: Any) -> Finding:
    servers = list(inv.dns.server_addresses)
    slow = inv.dns.resolve_ms is not None and inv.dns.resolve_ms > 30.0
    gateway = any(s.startswith("192.168.") or s.startswith("10.") for s in servers)
    if slow or gateway:
        return Finding(
            verdict=Verdict.PROBLEM,
            summary=f"resolver {servers or ['unknown']} took {inv.dns.resolve_ms} ms",
            measured={"servers": servers, "resolve_ms": inv.dns.resolve_ms},
        )
    if not servers and inv.dns.resolve_ms is None:
        return Finding(
            verdict=Verdict.UNKNOWN, summary="no resolver measurement available"
        )
    return Finding(verdict=Verdict.NO_PROBLEM, summary="resolver answers quickly")


def _dns_plan(inv: Any) -> list[Step]:
    _ = inv
    return [
        ps_step(
            "Point all adapters at fast public resolvers (1.1.1.1, 8.8.8.8)",
            "Get-NetAdapter -Physical | Set-DnsClientServerAddress "
            "-ServerAddresses '1.1.1.1','8.8.8.8'",
            "Get-NetAdapter -Physical | Set-DnsClientServerAddress -ResetServerAddresses",
            notes="Undo returns the adapters to DHCP-provided resolvers.",
        )
    ]


def _mtu_probe(inv: Any) -> Finding:
    _ = inv
    return Finding(
        verdict=Verdict.UNKNOWN,
        summary="path MTU was not measured; fragmentation cannot be judged yet",
    )


def _mtu_plan(inv: Any) -> list[Step]:
    _ = inv
    return [
        manual(
            "Find the correct MTU and set it on the active adapter",
            "1. Run: ping -f -l 1472 1.1.1.1 and lower 1472 until replies stop fragmenting\n"
            "2. Add 28 to the largest working value; that is the path MTU\n"
            "3. Open the adapter properties > Configure > Advanced > Jumbo Packet/MTU\n"
            "4. Set the discovered value and re-run the latency monitor",
            notes="A wrong MTU causes fragmentation or drops, both of which look like lag.",
        )
    ]


def _upload_cap_probe(inv: Any) -> Finding:
    bloat = inv.bloat or {}
    inc = bloat.get("increase_p95_ms")
    if inc is None:
        return Finding(
            verdict=Verdict.UNKNOWN,
            summary="no loaded-latency measurement attached, so upload queueing is unjudged",
        )
    if float(inc) > 20.0:
        return Finding(
            verdict=Verdict.PROBLEM,
            summary=f"latency under upload rises by {inc} ms p95",
            measured={"increase_p95_ms": inc},
        )
    return Finding(verdict=Verdict.NO_PROBLEM, summary="loaded latency stays flat")


def _upload_cap_plan(inv: Any) -> list[Step]:
    _ = inv
    return [
        manual(
            "Cap upstream bandwidth so the upload queue cannot grow without bound",
            "1. Measure the real upload speed with a speed test while idle\n"
            "2. Open the router QoS page and set the upload ceiling to 90 percent of it\n"
            "3. If the router has no QoS page, pause cloud sync and streaming while gaming\n"
            "4. Re-run the bufferbloat test and confirm the loaded p95 drops",
            notes="The queue that holds game packets hostage lives outside the PC.",
        )
    ]


def _sqm_probe(inv: Any) -> Finding:
    bloat = inv.bloat or {}
    grade = str(bloat.get("grade") or "").upper()
    if not grade:
        return Finding(
            verdict=Verdict.UNKNOWN,
            summary="no bufferbloat grade measured, so router queueing is unjudged",
        )
    if grade in ("C", "D", "F"):
        return Finding(
            verdict=Verdict.PROBLEM,
            summary=f"bufferbloat grade {grade}: the router queue grows under load",
            measured={"grade": grade},
        )
    return Finding(
        verdict=Verdict.NO_PROBLEM, summary=f"bufferbloat grade {grade} is fine"
    )


def _sqm_plan(inv: Any) -> list[Step]:
    _ = inv
    return [
        manual(
            "Enable Smart Queue Management on the router",
            "1. Log into the router admin page, usually 192.168.1.1\n"
            "2. Find the QoS, SQM, fq_codel or cake setting and enable it\n"
            "3. Enter the measured download and upload speeds as the ceilings\n"
            "4. Save, reboot the router, and re-run the bufferbloat test",
            notes="SQM keeps the queue shallow even when the link is saturated.",
        )
    ]


NETWORK_TWEAKS: tuple[Tweak, ...] = (
    Tweak(
        id="net.wifi.prefer_wired",
        title="Gaming over Wi-Fi instead of a cable",
        component=BudgetComponent.NETWORK_PATH,
        mechanism="A wireless medium is shared and half-duplex, so every frame contends for airtime, retries after interference, and waits behind neighbours traffic before the game packet even reaches the router queue.",
        confidence=Confidence.HIGH,
        risk=Risk.LOW,
        summary="Primary adapter is wireless.",
        probe=_wifi_probe,
        plan=_wifi_plan,
        expected_effect="Lower jitter and fewer latency spikes on the first hop.",
    ),
    Tweak(
        id="nic.power_saving.disable",
        title="Network card sleeps between packets",
        component=BudgetComponent.NETWORK_PATH,
        mechanism="PCI power management parks the network controller in a low-power link state between bursts, and each wake transition stalls packet delivery for hundreds of microseconds that accumulate across thousands of packets per second.",
        confidence=Confidence.MEDIUM,
        risk=Risk.LOW,
        summary="NIC power management enabled.",
        probe=_power_probe,
        plan=_power_plan,
        expected_effect="Removes wake-up stalls from the packet delivery path.",
    ),
    Tweak(
        id="nic.interrupt_moderation.disable",
        title="Network card delays interrupts to save CPU",
        component=BudgetComponent.NETWORK_PATH,
        mechanism="Interrupt coalescing holds completed packets in the NIC ring until a timer expires or a batch fills, trading per-packet delivery latency for fewer CPU interrupts, which directly delays the game network thread wakeup.",
        confidence=Confidence.MEDIUM,
        risk=Risk.LOW,
        summary="Interrupt moderation enabled.",
        probe=_moderation_probe,
        plan=_moderation_plan,
        expected_effect="Packets reach the game thread sooner at slightly higher CPU cost.",
    ),
    Tweak(
        id="net.dns.fastest_resolver",
        title="Slow domain name resolver",
        component=BudgetComponent.NETWORK_PATH,
        mechanism="Every new game session, matchmaking call, and content fetch starts with a DNS lookup, and a slow or distant resolver adds its full round trip to connection setup before any game traffic flows.",
        confidence=Confidence.MEDIUM,
        risk=Risk.LOW,
        summary="DNS resolver is slow.",
        probe=_dns_probe,
        plan=_dns_plan,
        expected_effect="Faster connection setup for new sessions and services.",
    ),
    Tweak(
        id="net.mtu.correctness",
        title="Misconfigured network packet size",
        component=BudgetComponent.NETWORK_PATH,
        mechanism="An MTU larger than the path fragments every oversized datagram into pieces that must all arrive before reassembly, while a smaller one wastes header overhead, and either mismatch shows up as retransmissions and unstable round trips.",
        confidence=Confidence.LOW,
        risk=Risk.LOW,
        summary="MTU correctness unknown.",
        probe=_mtu_probe,
        plan=_mtu_plan,
        expected_effect="Fewer retransmissions once the true path MTU is set.",
    ),
    Tweak(
        id="net.bufferbloat.upload_cap",
        title="Upload queue holds game packets hostage",
        component=BudgetComponent.LOCAL_QUEUE,
        mechanism="Upstream bandwidth is usually a fraction of downstream, so a single cloud upload fills the modem queue with bulk packets and each small game packet waits behind the entire backlog before leaving the home.",
        confidence=Confidence.HIGH,
        risk=Risk.LOW,
        summary="Upload queueing under load.",
        probe=_upload_cap_probe,
        plan=_upload_cap_plan,
        expected_effect="Loaded latency drops toward the idle baseline.",
        measure_needs_load=True,
    ),
    Tweak(
        id="net.bufferbloat.router_sqm",
        title="Router queue fills up under load",
        component=BudgetComponent.LOCAL_QUEUE,
        mechanism="A first-in-first-out router queue grows to hundreds of milliseconds of backlog under a speed test or download, and fair queueing disciplines such as fq_codel isolate the thin game flow from the bulk flow.",
        confidence=Confidence.HIGH,
        risk=Risk.LOW,
        summary="Router bufferbloat present.",
        probe=_sqm_probe,
        plan=_sqm_plan,
        expected_effect="Large reduction of latency measured while the link is loaded.",
        measure_needs_load=True,
    ),
)


# ===== merged from debloat.py =====

import re

from models import DebloatOp

POLICY_ROOTS: tuple[str, ...] = (
    "HKLM:\\Software\\Policies\\Microsoft\\Windows",
    "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Policies",
)

_POLICY_PREFIXES = (
    "software\\policies",
    "software\\microsoft\\windows\\currentversion\\policies",
)

_EDGE_APP_ID = "{56EB18F8-B008-4CBD-B6D2-8C97FE7E9062}"
_ONEDRIVE_CLSID = "{018D5C66-4533-4307-9B53-224DE2ED1FE6}"

DEBLOAT_OPERATIONS: tuple[DebloatOp, ...] = (
    DebloatOp(
        id="policy.game_dvr_capture",
        title="Disable Game DVR policy",
        summary="Turns off background game recording through policy keys",
        reversible=True,
    ),
    DebloatOp(
        id="policy.consumer_features",
        title="Disable consumer feature downloads",
        summary="Stops Windows from auto-installing suggested third-party apps",
        reversible=True,
    ),
    DebloatOp(
        id="policy.telemetry_basic",
        title="Set telemetry to the minimum level",
        summary="Limits diagnostic data collection to the basic level",
        reversible=True,
    ),
    DebloatOp(
        id="policy.copilot",
        title="Disable Windows Copilot",
        summary="Hides Copilot and disables its policy entry points",
        reversible=True,
    ),
    DebloatOp(
        id="policy.update_deferral",
        title="Defer Windows feature and quality updates",
        summary="Business deferral policies on Pro+, release pinning on Home",
        reversible=True,
    ),
    DebloatOp(
        id="app.xbox_overlay",
        title="Remove Xbox overlay packages",
        summary="Removes the Xbox Game Bar overlay packages for all users",
        reversible=False,
        safety="Reinstallable from the Microsoft Store; game recording stops working.",
    ),
    DebloatOp(
        id="app.onedrive_remove",
        title="Remove OneDrive",
        summary="Uninstalls OneDrive and stops its sync processes",
        reversible=False,
        safety="Reinstallable from Microsoft; local files stay, sync stops.",
        requires_reboot=True,
    ),
    DebloatOp(
        id="app.edge_remove",
        title="Remove Microsoft Edge",
        summary="Runs Edge's own uninstaller, verifies removal, cleans registrations",
        reversible=False,
        safety=(
            "Vendor uninstaller only; refuses redirected installs and foreign "
            "registrations; browser profiles preserved; reinstallable from "
            "Microsoft; a reboot may be required."
        ),
        requires_reboot=True,
    ),
    DebloatOp(
        id="app.outlook_remove",
        title="Remove Outlook app packages",
        summary="Removes Outlook AppX packages and provisioning for all users",
        reversible=False,
        safety="Reinstallable from the Microsoft Store; mailbox data stays on the server.",
    ),
)

_DEBLOAT_BY_ID: dict[str, DebloatOp] = {o.id: o for o in DEBLOAT_OPERATIONS}


def validate_policy_path(hive: str, path: str) -> None:
    if hive.upper() not in ("HKLM", "HKCU"):
        raise ValueError(f"policy writes are confined to HKLM/HKCU, got {hive!r}")
    low = path.casefold().replace("/", "\\")
    if not any(
        low == root or low.startswith(root + "\\") for root in _POLICY_PREFIXES
    ):
        raise ValueError(
            f"policy writes are confined to {', '.join(POLICY_ROOTS)}, got {path!r}"
        )


def _reg(
    hive: str, path: str, name: str, value: object, value_type: str = "DWord",
    notes: str = "",
) -> Step:
    validate_policy_path(hive, path)
    return reg_set(hive, path, name, value, value_type=value_type, notes=notes)


def _registry_for(op_id: str, inv: Any = None) -> list[Step]:
    if op_id == "policy.game_dvr_capture":
        return [
            _reg(
                "HKLM",
                r"SOFTWARE\Policies\Microsoft\Windows\GameDVR",
                "AllowGameDVR",
                0,
                notes="Policy-level master switch for game recording.",
            )
        ]
    if op_id == "policy.consumer_features":
        return [
            _reg(
                "HKLM",
                r"SOFTWARE\Policies\Microsoft\Windows\CloudContent",
                "DisableWindowsConsumerFeatures",
                1,
                notes="Stops suggested-app auto-installs on new user profiles.",
            )
        ]
    if op_id == "policy.telemetry_basic":
        return [
            _reg(
                "HKLM",
                r"SOFTWARE\Policies\Microsoft\Windows\DataCollection",
                "AllowTelemetry",
                1,
                notes="Minimum diagnostic level; fully disabling breaks servicing.",
            )
        ]
    if op_id == "policy.copilot":
        return [
            _reg(
                "HKLM",
                r"SOFTWARE\Policies\Microsoft\Windows\WindowsCopilot",
                "TurnOffWindowsCopilot",
                1,
                notes="Hides Copilot surfaces; the underlying model service stays.",
            )
        ]
    if op_id == "policy.update_deferral":
        return _deferral_steps(inv)
    raise ValueError(f"no registry mapping for {op_id}")


def _deferral_steps(inv: Any) -> list[Step]:
    supported = bool(inv is not None and inv.supports_update_policy)
    if supported:
        steps = [
            _reg(
                "HKLM", r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate",
                "DeferFeatureUpdates", 1,
                notes="Business deferral; Home editions ignore this key.",
            ),
            _reg(
                "HKLM", r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate",
                "DeferFeatureUpdatesPeriodInDays", 365,
                notes="One year of feature-update deferral.",
            ),
            _reg(
                "HKLM", r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate",
                "DeferQualityUpdates", 1,
                notes="Business deferral; Home editions ignore this key.",
            ),
            _reg(
                "HKLM", r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate",
                "DeferQualityUpdatesPeriodInDays", 4,
                notes="Four days of quality-update deferral.",
            ),
            _reg(
                "HKLM", r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate",
                "ExcludeWUDriversInQualityUpdate", 1,
                notes="Keeps driver updates out of quality updates.",
            ),
            _reg(
                "HKLM", r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU",
                "AUOptions", 2,
                notes="Notify before download.",
            ),
        ]
    else:
        release = (inv.display_version if inv is not None else "") or ""
        if not re.fullmatch(r"\d{2}H[12]", release):
            raise ValueError(
                "refusing update deferral: the installed release could not be read, "
                "and pinning a guessed release would invent a value"
            )
        steps = [
            _reg(
                "HKLM", r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate",
                "DeferQualityUpdates", 1,
                notes=f"Home path: release pinned to the installed {release}.",
            ),
            _reg(
                "HKLM", r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate",
                "DeferQualityUpdatesPeriodInDays", 4,
                notes="Four days of quality-update deferral.",
            ),
            _reg(
                "HKLM", r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate",
                "ProductVersion",
                "Windows 11"
                if (getattr(inv, "windows_build", 0) or 0) >= 22000
                else "Windows 10",
                value_type="String",
                notes="Pins the targeted product for the release hold.",
            ),
            _reg(
                "HKLM", r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate",
                "TargetReleaseVersion", 1,
                notes="Enables the release hold below.",
            ),
            _reg(
                "HKLM", r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate",
                "TargetReleaseVersionInfo", release, value_type="String",
                notes=f"Hold at the installed release {release}; re-run after upgrading.",
            ),
        ]
    steps.append(
        Step(
            kind=StepKind.SERVICE,
            description="Restart the Windows Update service with its dependents",
            command=(
                "Get-Service -Name wuauserv | "
                "Select-Object -ExpandProperty DependentServices | "
                "Stop-Service -Force -ErrorAction SilentlyContinue; "
                "Stop-Service -Name wuauserv -Force -ErrorAction Stop; "
                "Start-Service -Name wuauserv -ErrorAction Stop; "
                "Get-Service -Name wuauserv | "
                "Select-Object -ExpandProperty DependentServices | ForEach-Object "
                "{ Start-Service -Name $_.Name -ErrorAction SilentlyContinue }"
            ),
            restarts_service="wuauserv",
            undo=(
                Step(
                    kind=StepKind.SERVICE,
                    description="Ensure the Windows Update service is running",
                    command="Start-Service -Name wuauserv -ErrorAction SilentlyContinue",
                    terminal=True,
                ),
            ),
            notes="Transient stop so policy is re-read; everything is restarted.",
        )
    )
    return steps


def _edge_policy_step(inv: Any) -> Step:
    if inv is not None and inv.domain_joined is True:
        return _reg(
            "HKLM",
            r"SOFTWARE\Policies\Microsoft\EdgeUpdate",
            "Install" + _EDGE_APP_ID,
            0,
            notes="Blocks stable Edge reinstall through Edge Update policy (domain).",
        )
    return manual(
        "Edge may reinstall itself after removal",
        "The Edge Update install block only takes effect on an Active Directory "
        "domain, and this machine is not domain-joined (or membership could not be "
        "read).\n"
        "No policy was written, because it would do nothing. If Edge returns after "
        "an update, remove it again or join a domain to enforce the block.",
        notes="A policy that cannot take effect is not written.",
    )


_EDGE_UNINSTALL = (
    "$roots = @(); "
    "foreach ($v in @('ProgramFiles(x86)', 'ProgramW6432', 'ProgramFiles', 'LOCALAPPDATA')) { "
    "$b = [Environment]::GetEnvironmentVariable($v); "
    "if ($b) { $roots += (Join-Path $b 'Microsoft\\Edge\\Application') } }; "
    "if (-not $roots) { throw 'No Windows application directories are available for Edge removal.' }; "
    "foreach ($app in $roots) { "
    "if (Test-Path -LiteralPath $app) { "
    "if ((Get-Item -LiteralPath $app -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { "
    "throw \"Refusing a redirected Edge installation: $app\" }; } }; "
    "Stop-Process -Name msedge,msedge_proxy,msedge_pwa_launcher -Force "
    "-ErrorAction SilentlyContinue; "
    "$appId = '" + _EDGE_APP_ID + "'; "
    "foreach ($app in $roots) { "
    "if (-not (Test-Path -LiteralPath $app)) { continue }; "
    "$vers = Get-ChildItem -LiteralPath $app -Directory -ErrorAction SilentlyContinue | "
    "Where-Object { $_.Name -match '^\\d+\\.\\d+\\.\\d+\\.\\d+$' } | "
    "Sort-Object Name -Descending; "
    "foreach ($ver in $vers) { "
    "$setup = Join-Path $ver.FullName 'Installer\\setup.exe'; "
    "if (Test-Path -LiteralPath $setup) { "
    "$code = (Start-Process -FilePath $setup -ArgumentList '--uninstall','--msedge',"
    "'--force-uninstall','--verbose-logging','--system-level' -Wait -PassThru).ExitCode; "
    "if ($code -notin @(0, 19, 29, 3010)) { "
    "throw \"Edge uninstaller failed with exit code $code.\" }; "
    "if ($code -in @(29, 3010)) { "
    "Write-Host 'Edge uninstaller requested a restart.' -ForegroundColor Yellow }; "
    "break } }; "
    "if (Test-Path -LiteralPath $app) { "
    "Remove-Item -LiteralPath $app -Recurse -Force -ErrorAction Stop } }; "
    "$reg = 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\msedge.exe'; "
    "try { $cur = (Get-ItemProperty -LiteralPath $reg -ErrorAction Stop).'(default)' } "
    "catch { $cur = $null }; "
    "if ($cur) { "
    "$inside = $false; foreach ($r in $roots) { "
    "if ($cur.Trim('\"') -like ($r + '*')) { $inside = $true } }; "
    "if ($inside) { "
    "Remove-Item -LiteralPath $reg -Recurse -Force -ErrorAction SilentlyContinue } "
    "else { Write-Host 'Edge registered outside the removed roots; registrations preserved.' "
    "-ForegroundColor Yellow } }; "
    "foreach ($br in @('Clients', 'ClientState', 'ClientStateMedium')) { "
    "Remove-Item -LiteralPath (\"HKLM:\\SOFTWARE\\Microsoft\\EdgeUpdate\\\" + $br + "
    "\"\\\" + $appId) -Recurse -Force -ErrorAction SilentlyContinue }; "
    "Remove-Item \"$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs\\*Edge*.lnk\" "
    "-Force -ErrorAction SilentlyContinue; "
    "Remove-Item \"$env:PUBLIC\\Desktop\\*Edge*.lnk\" -Force -ErrorAction SilentlyContinue; "
    "Stop-Process -Name explorer -Force -ErrorAction SilentlyContinue"
)


def _removal_for(op_id: str, inv: Any = None) -> list[Step]:
    if op_id == "app.xbox_overlay":
        op = _DEBLOAT_BY_ID[op_id]
        return [
            Step(
                kind=StepKind.POWERSHELL,
                description="Remove Xbox overlay packages for all users",
                command=(
                    "Get-AppxPackage -AllUsers *XboxGamingOverlay* | "
                    "Remove-AppxPackage -AllUsers -ErrorAction Stop"
                ),
                reversible=False,
                safety=op.safety,
            )
        ]
    if op_id == "app.onedrive_remove":
        op = _DEBLOAT_BY_ID[op_id]
        return [
            Step(
                kind=StepKind.POWERSHELL,
                description="Stop OneDrive processes and uninstall OneDrive",
                command=(
                    "Stop-Process -Name OneDrive -Force -ErrorAction SilentlyContinue; "
                    "$setup = Join-Path $env:SystemRoot "
                    "'SysWOW64\\OneDriveSetup.exe'; "
                    "if (-not (Test-Path -LiteralPath $setup)) { "
                    "$setup = Join-Path $env:SystemRoot 'System32\\OneDriveSetup.exe' }; "
                    "if (-not (Test-Path -LiteralPath $setup)) { "
                    "throw 'No supported OneDrive uninstaller was found.' }; "
                    "$code = (Start-Process -FilePath $setup -ArgumentList '/uninstall' "
                    "-Wait -PassThru).ExitCode; "
                    "if ($code -notin @(0, 3010)) { "
                    "throw \"OneDrive uninstaller exited with code $code.\" }; "
                    "if ($code -eq 3010) { "
                    "Write-Host 'OneDrive removal requires a restart.' "
                    "-ForegroundColor Yellow }"
                ),
                reversible=False,
                safety=op.safety,
                requires_reboot=True,
            ),
            Step(
                kind=StepKind.POWERSHELL,
                description="Remove OneDrive shell integration and leftover folders",
                command=(
                    "foreach ($t in @("
                    "'HKCR:\\CLSID\\" + _ONEDRIVE_CLSID + "', "
                    "'HKCR:\\Wow6432Node\\CLSID\\" + _ONEDRIVE_CLSID + "', "
                    "'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\"
                    "Desktop\\NameSpace\\" + _ONEDRIVE_CLSID + "')) { "
                    "if (Test-Path -LiteralPath $t) { "
                    "Remove-Item -LiteralPath $t -Recurse -Force "
                    "-ErrorAction Stop } }; "
                    "foreach ($t in @("
                    "\"$env:LOCALAPPDATA\\Microsoft\\OneDrive\", "
                    "\"$env:ProgramData\\Microsoft\\OneDrive\")) { "
                    "if (Test-Path -LiteralPath $t) { "
                    "Remove-Item -LiteralPath $t -Recurse -Force "
                    "-ErrorAction Stop } }"
                ),
                reversible=False,
                safety="Only the Explorer namespace entries and the two well-known "
                "OneDrive folders; synced user files are untouched.",
            ),
            Step(
                kind=StepKind.REGISTRY_DELETE,
                description="Clear pinned taskbar favorites left by removed apps",
                hive="HKCU",
                path=r"Software\Microsoft\Windows\CurrentVersion\Explorer\Taskband",
                value_name="Favorites",
                terminal=True,
                notes="Snapshot-restored on undo. NOTE: this clears ALL "
                      "taskbar pins, not only the removed app's.",
            ),
        ]
    if op_id == "app.edge_remove":
        op = _DEBLOAT_BY_ID[op_id]
        return [
            Step(
                kind=StepKind.POWERSHELL,
                description="Run Edge's own uninstaller and verify removal",
                command=_EDGE_UNINSTALL,
                reversible=False,
                safety=op.safety,
                requires_reboot=True,
            ),
            _edge_policy_step(inv),
        ]
    if op_id == "app.outlook_remove":
        op = _DEBLOAT_BY_ID[op_id]
        return [
            Step(
                kind=StepKind.POWERSHELL,
                description="Remove Outlook packages and provisioning for all users",
                command=(
                    "Get-AppxPackage -AllUsers *Outlook* | "
                    "Remove-AppxPackage -AllUsers -ErrorAction Stop; "
                    "Get-AppxProvisionedPackage -Online | "
                    "Where-Object { $_.DisplayName -like '*Outlook*' } | "
                    "Remove-AppxProvisionedPackage -Online -ErrorAction Stop"
                ),
                reversible=False,
                safety=op.safety,
            )
        ]
    raise ValueError(f"no removal mapping for {op_id}")


def build_selected(
    selected: tuple[str, ...] | None, inv: Any = None
) -> list[Step]:
    ids = list(selected) if selected else [o.id for o in DEBLOAT_OPERATIONS if o.reversible]
    unknown = [i for i in ids if i not in _DEBLOAT_BY_ID]
    if unknown:
        raise ValueError(f"unknown debloat operations: {', '.join(unknown)}")
    steps: list[Step] = []
    for op_id in ids:
        op = _DEBLOAT_BY_ID[op_id]
        if op_id.startswith("policy."):
            steps.extend(_registry_for(op_id, inv))
        elif op_id.startswith("app."):
            steps.extend(_removal_for(op_id, inv))
        else:
            steps.append(
                manual(
                    f"Apply {op.title}",
                    f"{op.summary}\nReversible: {op.reversible}",
                    notes=op.safety,
                )
            )
    if any(i.startswith("policy.") for i in ids):
        steps.append(
            Step(
                kind=StepKind.POWERSHELL,
                description="Refresh Group Policy so the new policies apply",
                command="gpupdate /target:computer /force /wait:60",
                terminal=True,
                notes="Refresh only; it re-applies the current policy state.",
            )
        )
    return steps



# ===== merged from tweak_utils.py =====
# ===== merged from experimental.py =====


def _platform_probe(inv: Any) -> Finding:
    _ = inv
    return Finding(
        verdict=Verdict.UNKNOWN,
        summary="boot timer platform was not inspected; changing it is opt-in only",
    )


def _platform_plan(inv: Any) -> list[Step]:
    _ = inv
    return [
        Step(
            kind=StepKind.BCDEDIT,
            description="Use the platform clock as the boot timer source",
            command="bcdedit /set useplatformclock true",
            undo=(
                Step(
                    kind=StepKind.BCDEDIT,
                    description="Restore default boot timer source",
                    command="bcdedit /deletevalue useplatformclock",
                    terminal=True,
                ),
            ),
            notes="Reboot required; measure before and after, revert on regression.",
        )
    ]


_GTRR_PATH = r"SYSTEM\CurrentControlSet\Control\Session Manager\kernel"
_GTRR_MIN_BUILD = 19041


def _gtrr_probe(inv: Any) -> Finding:
    build = getattr(inv, "windows_build", 0) or 0
    if build and build < _GTRR_MIN_BUILD:
        return Finding(
            verdict=Verdict.NOT_APPLICABLE,
            summary="GlobalTimerResolutionRequests needs Windows 10 2004+ / "
                    "Windows 11 / Server 2022 "
                    f"(build {_GTRR_MIN_BUILD}+); this is build {build}",
        )
    state = getattr(inv.timer, "global_timer_resolution_requests", -1)
    if state == 1:
        return Finding(
            verdict=Verdict.NO_PROBLEM,
            summary="system-wide timer requests are already enabled",
            measured={"global_timer_requests": state},
        )
    return Finding(
        verdict=Verdict.UNKNOWN,
        summary="a per-process 0.5 ms request only reaches other processes when "
                "GlobalTimerResolutionRequests is on; it is experimental and off by "
                "default",
        measured={"global_timer_requests": state},
    )


def _gtrr_plan(inv: Any) -> list[Step]:
    _ = inv
    return [
        reg_set(
            "HKLM",
            _GTRR_PATH,
            "GlobalTimerResolutionRequests",
            1,
            notes=(
                "Makes a higher-resolution request apply system-wide instead of only "
                "to the requesting process. Reboot required; Microsoft documents this "
                "for debugging, and it raises the clock interrupt rate for every "
                "process, so measure and revert on any regression."
            ),
        )
    ]


EXPERIMENTAL_TWEAKS: tuple[Tweak, ...] = (
    Tweak(
        id="timer.bcdedit_platform.experimental",
        title="Boot-time timer source override",
        component=BudgetComponent.HOST_PROCESSING,
        mechanism="The boot configuration timer source selects which hardware clock drives kernel timekeeping, and a mismatched source skews high-resolution waits while adding interrupt overhead that varies wildly between firmware implementations and silicon generations.",
        confidence=Confidence.LOW,
        risk=Risk.HIGH,
        summary="Boot timer source untested.",
        probe=_platform_probe,
        plan=_platform_plan,
        expected_effect="Unpredictable; may help, hurt, or destabilise the boot.",
        experimental=True,
        requires_reboot=True,
        caveats="Boot-level change: save a baseline first and revert on any regression.",
    ),
    Tweak(
        id="timer.global_requests.experimental",
        title="System-wide timer resolution requests",
        component=BudgetComponent.HOST_PROCESSING,
        mechanism="By default a process's higher-resolution timer request only tightens that process's own waits, so the game thread keeps inheriting the coarse 15.6 ms tick driven by every other scheduler client; enabling GlobalTimerResolutionRequests promotes the finest requested resolution to a kernel-wide clock interrupt rate that all threads observe.",
        confidence=Confidence.LOW,
        risk=Risk.HIGH,
        summary="System-wide timer requests are off by default.",
        probe=_gtrr_probe,
        plan=_gtrr_plan,
        expected_effect="A 0.5 ms request from any process tightens the tick for all of them.",
        experimental=True,
        requires_reboot=True,
        caveats=(
            "Windows 10 2004+ / Windows 11 / Server 2022. Microsoft documents "
            "this key for "
            "debugging: it raises the clock interrupt rate machine-wide, increasing "
            "idle power draw and heat. Reboot required; save a baseline and revert on "
            "any regression."
        ),
    ),
)


# ===== merged from gaming.py =====


def _game_mode_probe(inv: Any) -> Finding:
    _ = inv
    return Finding(
        verdict=Verdict.UNKNOWN,
        summary="Game Mode effect cannot be read; only an A/B measurement decides",
    )


def _game_mode_plan(inv: Any) -> list[Step]:
    _ = inv
    return [
        manual(
            "A/B test Game Mode and fullscreen optimizations",
            "1. Open Settings > Gaming > Game Mode and note the current state\n"
            "2. Save a 200-sample baseline with the live monitor\n"
            "3. Toggle Game Mode, reboot, and measure the same target again\n"
            "4. Keep the state with the lower p95 and discard the other",
            notes="Both features reroute the frame path; the winner differs per machine.",
        )
    ]


GAMING_TWEAKS: tuple[Tweak, ...] = (
    Tweak(
        id="os.game_mode.ab_test",
        title="Game Mode and fullscreen optimizations untested",
        component=BudgetComponent.RENDER_INPUT,
        mechanism="Game Mode reparents scheduling priorities while fullscreen optimizations redirect the swap chain through the desktop compositor, accompanied by driver-specific presentation paths where each redirection either removes or adds a frame of presentation latency depending on driver and display combination.",
        confidence=Confidence.MEDIUM,
        risk=Risk.MEDIUM,
        summary="Game Mode effect unmeasured.",
        probe=_game_mode_probe,
        plan=_game_mode_plan,
        expected_effect="One toggle direction measurably wins; the other is discarded.",
        caveats="Neither state is a defect; apply only the measured winner.",
    ),
)


# ===== merged from redist.py =====


@dataclass(slots=True)
class RedistOp:
    id: str
    title: str
    optional: bool = False
    why: str = ""
    packages: tuple[str, ...] = ()
    search: str = ""
    whitelist: tuple[str, ...] = ()
    blacklist: tuple[str, ...] = ()


REDIST_OPERATIONS: tuple[RedistOp, ...] = (
    RedistOp(
        id="vcredist",
        title="Visual C++ runtimes (all supported years, x64/x86)",
        why="Missing MSVCP140.dll and VCRUNTIME140.dll stop games from launching.",
        packages=("Microsoft.VCRedist.2015+.x64", "Microsoft.VCRedist.2015+.x86"),
    ),
    RedistOp(
        id="dotnet",
        title=".NET Desktop Runtime 8 (x64)",
        why="Launchers and overlays built on .NET fail without the desktop runtime.",
        packages=("Microsoft.DotNet.DesktopRuntime.8",),
    ),
    RedistOp(
        id="directx",
        title="DirectX End-User Runtime",
        why="Legacy D3DX9/D3DX11 helpers are not part of Windows 11.",
        packages=(),
        search="DirectX End-User Runtime",
        whitelist=("Microsoft.DirectX",),
        blacklist=("Demo", "SDK"),
    ),
    RedistOp(
        id="aspnet",
        title="ASP.NET Core Runtime 8 (optional)",
        optional=True,
        why="Only needed by server-style tools and mod managers with web UIs.",
        packages=("Microsoft.DotNet.AspNetCore.8",),
    ),
)

_REDIST_BY_ID: dict[str, RedistOp] = {r.id: r for r in REDIST_OPERATIONS}


def _redist_discovery_cmd(op):
    white = "|".join(_rx_lit(w) for w in op.whitelist) or _rx_lit(op.search)
    cmd = (f"$found = winget search --exact '{_ps_name(op.search)}' | "
           f"Select-String -Pattern '{white}'")
    if op.blacklist:
        cmd += (" | Select-String -NotMatch '%s'"
                % "|".join(_rx_lit(b) for b in op.blacklist))
    return (cmd + "; if (-not $found) { throw 'no whitelisted package found' }; "
            "Write-Host $found -ForegroundColor Cyan")


def build_redist_steps(
    selected: tuple[str, ...] | None = None,
    include_optional: bool = False,
) -> list[Step]:
    if selected is None:
        ops = [r for r in REDIST_OPERATIONS if include_optional or not r.optional]
    else:
        unknown = [i for i in selected if i not in _REDIST_BY_ID]
        if unknown:
            raise ValueError(
                f"unknown redistributable operations: {', '.join(unknown)}"
            )
        ops = [_REDIST_BY_ID[i] for i in selected]
    steps: list[Step] = []
    for op in ops:
        if op.packages:
            for pkg in op.packages:
                steps.append(
                    Step(
                        kind=StepKind.WINGET_INSTALL,
                        description=f"Install {pkg} ({op.title})",
                        command=(
                            f"winget install --exact --id {pkg} --silent "
                            "--accept-package-agreements --accept-source-agreements"
                        ),
                        undo=(
                            Step(
                                kind=StepKind.WINGET_INSTALL,
                                description=f"Uninstall {pkg}",
                                command=f"winget uninstall --exact --id {pkg} --silent",
                                terminal=True,
                            ),
                        ),
                        notes=op.why,
                    )
                )
        else:
            steps.append(
                Step(
                    kind=StepKind.WINGET_INSTALL,
                    description=f"Discover {op.title} via winget search {op.search!r}",
                    command=_redist_discovery_cmd(op),
                    undo=(
                        Step(
                            kind=StepKind.WINGET_INSTALL,
                            description="No install performed by discovery",
                            command="Write-Host 'discovery only; nothing to undo'",
                            terminal=True,
                        ),
                    ),
                    notes=op.why,
                )
            )
    return steps


def redist_report() -> list[dict]:
    return [
        {
            "id": "redists",
            "title": "Gaming redistributables",
            "summary": "Installs missing runtimes so games launch. Not a latency change.",
            "items": [
                {
                    "id": r.id,
                    "description": r.title,
                    "reversible": True,
                    "safety": "Uninstallable via winget or Apps settings.",
                }
                for r in REDIST_OPERATIONS
            ],
            "notes": [],
        }
    ]


# ===== merged from refused.py =====

from system_core import RefusedTweakError


def _refused_probe(summary: str):
    def probe(inv: Any) -> Finding:
        _ = inv
        return Finding(verdict=Verdict.NOT_APPLICABLE, summary=summary)

    return probe


def _refused_plan(inv: Any) -> list[Step]:
    _ = inv
    raise RefusedTweakError("refused tweaks never produce a plan")


REFUSED_TWEAKS: tuple[Tweak, ...] = (
    Tweak(
        id="kb.delay.speed",
        title="Keyboard repeat delay and speed",
        component=BudgetComponent.RENDER_INPUT,
        mechanism="The Control Panel keyboard settings govern the character repeat rate of held keys in text fields, which travels through an entirely different path from the interrupt-driven key events that games poll each frame.",
        confidence=Confidence.HIGH,
        risk=Risk.HIGH,
        summary="Keyboard repeat tuning.",
        probe=_refused_probe("repeat rate affects typing, not game input latency"),
        plan=_refused_plan,
        expected_effect="None for gaming; text entry repeats faster.",
        refusal_reason=(
            "Refused: keyboard repeat delay changes the Control Panel character repeat "
            "rate, not in-game input latency. No causal path to any budget term exists."
        ),
    ),
    Tweak(
        id="net.nagle.disable",
        title="Disable Nagle's algorithm globally",
        component=BudgetComponent.NETWORK_PATH,
        mechanism="Nagle coalescing delays small outbound segments up to 200 ms awaiting acknowledgements, but modern game sockets already set TCP_NODELAY per connection, so a global registry override changes nothing for games while risking congestion collapse elsewhere.",
        confidence=Confidence.HIGH,
        risk=Risk.HIGH,
        summary="Global Nagle override.",
        probe=_refused_probe("games already disable Nagle per socket where it matters"),
        plan=_refused_plan,
        expected_effect="None for games using TCP_NODELAY; global risk for other apps.",
        refusal_reason=(
            "Refused: per-connection TCP_NODELAY already covers games; a global override "
            "adds congestion risk with no measured benefit."
        ),
    ),
    Tweak(
        id="net.throttling.disable",
        title="Disable network throttling index",
        component=BudgetComponent.NETWORK_PATH,
        mechanism="The throttling index caps non-multimedia network throughput to protect multimedia scheduling guarantees, and games run inside the multimedia class already, so removing the cap only unleashes background bulk flows that then compete with the game.",
        confidence=Confidence.HIGH,
        risk=Risk.HIGH,
        summary="Throttling index override.",
        probe=_refused_probe("games already sit in the exempt multimedia class"),
        plan=_refused_plan,
        expected_effect="None for games; background flows may starve the game instead.",
        refusal_reason=(
            "Refused: the throttle protects multimedia traffic including games; "
            "disabling it helps bulk downloaders at the game's expense."
        ),
    ),
)

# ===== merged from catalog.py =====

from models import validate_catalog
from input_utils import TWEAKS as _DISPLAY_TWEAKS


def _build() -> tuple[Tweak, ...]:
    return validate_catalog(
        (
            *NETWORK_TWEAKS,
            *_DISPLAY_TWEAKS,
            *HOST_TWEAKS,
            *GAMING_TWEAKS,
            *EXPERIMENTAL_TWEAKS,
            *REFUSED_TWEAKS,
        )
    )


_ALL: tuple[Tweak, ...] = _build()

_TWEAK_BY_ID: dict[str, Tweak] = {t.id: t for t in _ALL}


def all_tweaks() -> tuple[Tweak, ...]:
    return _ALL


def applicable_tweaks(include_experimental: bool = False) -> tuple[Tweak, ...]:
    return tuple(
        t for t in _ALL if not t.is_refused and (include_experimental or not t.experimental)
    )


def experimental_tweaks() -> tuple[Tweak, ...]:
    return tuple(t for t in _ALL if t.experimental and not t.is_refused)


def refused_tweaks() -> tuple[Tweak, ...]:
    return tuple(t for t in _ALL if t.is_refused)




