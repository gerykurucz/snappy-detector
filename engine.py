
from __future__ import annotations

import datetime as _dt
import html
import json
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from general_utils import console_encoding, powershell_exe, silent_kwargs
from maintenance_utils import applicable_tweaks, refused_tweaks
from measurement_utils import build_fake_inventory
from models import Step, StepKind, Tweak
from system_core import (CPU, DNS, GPU, NIC, AntiCheatPolicy, BudgetComponent,
                         Display, Inventory, OSFamily, PolicyDecision, Power,
                         RefusedTweakError, SafetyPolicy, Timer,
                         TweakValidationError, Vendor, Verdict,
                         query_timer_resolution)

__all__ = [
    "Backend", "BackendResult", "FakeBackend", "RealBackend", "make_backend",
    "parse_gpus_displays", "parse_nics", "parse_powercfg_index", "parse_scheme",
    "PlannedChange", "ScriptBundle", "render_bundle",
    "render_maintenance_script", "write_powershell", "indent_body",
    "Report", "TweakOutcome", "render_html", "render_markdown",
    "PlanResult", "build_plan", "build_report", "evaluate_all", "render",
    "load_baseline", "save_baseline",
]

_SCHEME_RE = re.compile(r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\s*\(([^)]*)\)")
_AC_INDEX_RE = re.compile(r"Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)")
_DC_INDEX_RE = re.compile(r"Current DC Power Setting Index:\s*0x([0-9a-fA-F]+)")


def _maybe_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _maybe_int(value):
    f = _maybe_float(value)
    return None if f is None else int(f)


def _as_rows(value):
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [r for r in value if isinstance(r, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def parse_scheme(text):
    m = _SCHEME_RE.search(str(text or ""))
    if not m:
        return "", ""
    return m.group(1), m.group(2).strip()


def parse_powercfg_index(text, which="AC"):
    rx = _AC_INDEX_RE if str(which).upper() == "AC" else _DC_INDEX_RE
    m = rx.search(str(text or ""))
    if not m:
        return None
    try:
        return int(m.group(1), 16)
    except ValueError:
        return None


def parse_nics(payload):
    adapters = _as_rows(payload.get("a") if isinstance(payload, dict) else payload)
    power = {}
    for row in _as_rows(payload.get("p") if isinstance(payload, dict) else None):
        name = str(row.get("Name") or "")
        val = str(row.get("AllowComputerToTurnOffDevice") or "").lower()
        if name and val in ("enabled", "disabled"):
            power[name] = val == "enabled"
    moderation = {}
    for row in _as_rows(payload.get("m") if isinstance(payload, dict) else None):
        name = str(row.get("Name") or "")
        val = str(row.get("DisplayValue") or "").lower()
        if name and val in ("enabled", "disabled"):
            moderation[name] = val == "enabled"
    nics = []
    for row in adapters:
        name = str(row.get("Name") or "")
        media = str(row.get("PhysicalMediaType") or "").lower()
        desc = str(row.get("InterfaceDescription") or "")
        if "802.11" in media or "802.11" in desc.lower() or "wi-fi" in desc.lower():
            physical = "wireless"
        elif media:
            physical = "wired"
        else:
            physical = ""
        nics.append(NIC(
            interface_description=desc,
            name=name,
            physical=physical,
            status=str(row.get("Status") or ""),
            mtu=_maybe_int(row.get("MtuSize")) or 0,
            allow_turn_off=power.get(name),
            interrupt_moderation=moderation.get(name),
        ))
    return nics


def parse_gpus_displays(rows, hags=None):
    gpus = []
    displays = []
    for row in _as_rows(rows):
        name = str(row.get("Name") or "")
        if not name:
            continue
        gpus.append(GPU(
            name=name,
            vendor=_vendor_of(name),
            driver_version=str(row.get("DriverVersion") or ""),
            hags=hags,
        ))
        cur = _maybe_float(row.get("CurrentRefreshRate"))
        mx = _maybe_float(row.get("MaxRefreshRate"))
        hres = _maybe_int(row.get("CurrentHorizontalResolution"))
        vres = _maybe_int(row.get("CurrentVerticalResolution"))
        if cur is None and not (hres and vres):
            continue
        displays.append(Display(
            name=name,
            resolution=f"{hres}x{vres}" if hres and vres else "",
            current_refresh_hz=cur,
            max_refresh_hz=mx if mx is not None else cur,
        ))
    return gpus, displays


@dataclass(slots=True)
class BackendResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    code: int = 0


class Backend:
    def fixture_inventory(self) -> Inventory | None:
        return None

    def powershell(self, script: str, timeout: float = 60.0) -> BackendResult:
        raise NotImplementedError

    def read_system(self) -> Inventory:
        raise NotImplementedError

    def read_network(self, inv: Inventory) -> None:
        raise NotImplementedError

    def read_display(self, inv: Inventory) -> None:
        raise NotImplementedError

    def read_dns(self, inv: Inventory) -> None:
        raise NotImplementedError

    def read_processes(self, inv: Inventory) -> None:
        raise NotImplementedError

    def read_anti_cheat(self, inv: Inventory) -> None:
        raise NotImplementedError


class RealBackend(Backend):
    def powershell(self, script: str, timeout: float = 60.0) -> BackendResult:
        try:
            proc = subprocess.run(
                [
                    powershell_exe(),
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                capture_output=True,
                text=True,
                encoding=console_encoding(),
                errors="replace",
                timeout=timeout,
                **silent_kwargs(),
            )
        except FileNotFoundError as exc:
            return BackendResult(ok=False, stderr=str(exc), code=127)
        except subprocess.TimeoutExpired:
            return BackendResult(ok=False, stderr="powershell timed out", code=124)
        return BackendResult(
            ok=proc.returncode == 0,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            code=proc.returncode,
        )

    def read_system(self) -> Inventory:
        import ctypes
        import platform

        elevated = False
        if sys.platform.startswith("win"):
            try:
                elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
            except Exception:
                elevated = False
        build = 0
        try:
            build = int(platform.version().split(".")[-1])
        except (ValueError, IndexError):
            build = 0
        inv = Inventory(
            os_name=f"{platform.system()} {platform.release()}",
            os_family=OSFamily.WINDOWS
            if sys.platform.startswith("win")
            else OSFamily.OTHER,
            windows_build=build,
            is_elevated=elevated,
            cpu=CPU(name=platform.processor() or platform.machine()),
            power=Power(),
            dns=DNS(),
            timer=Timer(),
        )
        self.read_platform_details(inv)
        self.read_cpu_details(inv)
        self.read_power_details(inv)
        self.read_timer_details(inv)
        self.read_usb_suspend(inv)
        return inv

    def read_platform_details(self, inv: Inventory) -> None:
        import json

        res = self.powershell(
            "$o = @{}; "
            "try { $p = Get-ItemProperty "
            "'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion'; "
            "$o.edition = $p.EditionID; $o.display = $p.DisplayVersion } catch {}; "
            "try { $o.domain = "
            "(Get-CimInstance Win32_ComputerSystem).PartOfDomain } catch {}; "
            "try { $o.cap = (Get-ItemProperty "
            "'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR' "
            "-Name AppCaptureEnabled -ErrorAction Stop).AppCaptureEnabled } catch {}; "
            "try { $o.gvr = (Get-ItemProperty 'HKCU:\\System\\GameConfigStore' "
            "-Name GameDVR_Enabled -ErrorAction Stop).GameDVR_Enabled } catch {}; "
            "$o | ConvertTo-Json -Compress",
            timeout=30.0,
        )
        if not res.ok or not res.stdout.strip():
            inv.warnings.append("platform details unreadable; edition-dependent tweaks stay unknown")
            return
        try:
            raw = json.loads(res.stdout)
        except ValueError:
            inv.warnings.append("platform details returned unparsable output")
            return
        if not isinstance(raw, dict):
            return
        edition = str(raw.get("edition") or "")
        inv.display_version = str(raw.get("display") or "")
        inv.supports_update_policy = edition in (
            "Professional", "ProfessionalEducation", "ProfessionalWorkstation",
            "Education", "Enterprise",
        )
        if "domain" in raw:
            inv.domain_joined = bool(raw["domain"])
        for key, attr in (("cap", "game_dvr_app_capture"), ("gvr", "game_dvr_enabled")):
            if key in raw and raw[key] is not None:
                try:
                    setattr(inv, attr, int(raw[key]))
                except (TypeError, ValueError):
                    pass

    def read_cpu_details(self, inv: Inventory) -> None:
        import json

        res = self.powershell(
            "@{lp=(Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors; "
            "l3=@(Get-CimInstance Win32_CacheMemory | Where-Object {$_.Level -eq 3}).Count} "
            "| ConvertTo-Json -Compress",
            timeout=30.0,
        )
        if not res.ok or not res.stdout.strip():
            inv.warnings.append("cpu topology query failed; core/cache counts unknown")
            return
        try:
            raw = json.loads(res.stdout)
        except ValueError:
            inv.warnings.append("cpu topology query returned unparsable output")
            return
        if not isinstance(raw, dict):
            return
        lp = _maybe_int(raw.get("lp"))
        if lp and lp > 0:
            inv.cpu.logical_processors = lp
        l3 = _maybe_int(raw.get("l3"))
        if l3 and l3 > 0:
            inv.cpu.l3_domains = l3

    def read_power_details(self, inv: Inventory) -> None:
        import json

        res = self.powershell(
            "$s = (powercfg /getactivescheme | Out-String); "
            "$b = $null; "
            "try { $b = (Get-CimInstance Win32_Battery | "
            "Select-Object -First 1).BatteryStatus } catch {}; "
            "@{s=$s; b=$b} | ConvertTo-Json -Compress",
            timeout=30.0,
        )
        if not res.ok or not res.stdout.strip():
            inv.warnings.append("power scheme query failed; plan name unknown")
            return
        try:
            raw = json.loads(res.stdout)
        except ValueError:
            inv.warnings.append("power scheme query returned unparsable output")
            return
        if not isinstance(raw, dict):
            return
        guid, name = parse_scheme(raw.get("s"))
        inv.power.active_scheme_guid = guid
        inv.power.active_scheme_name = name
        status = _maybe_int(raw.get("b"))
        inv.power.on_battery = status == 1

    def read_timer_details(self, inv: Inventory) -> None:
        if sys.platform.startswith("win"):
            try:
                res = query_timer_resolution()
                inv.timer.current_period_us = res.current_us
                inv.timer.min_period_us = res.min_us
                inv.timer.max_period_us = res.max_us
            except Exception:
                pass
            try:
                import winreg

                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SYSTEM\CurrentControlSet\Control\Session Manager\kernel",
                ) as key:
                    try:
                        value, _type = winreg.QueryValueEx(
                            key, "GlobalTimerResolutionRequests")
                        inv.timer.global_timer_resolution_requests = int(value)
                    except OSError:
                        inv.timer.global_timer_resolution_requests = 0
            except OSError:
                inv.timer.global_timer_resolution_requests = -1
            try:
                import winreg

                key = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
                    r"\Multimedia\SystemProfile\Tasks\Games",
                )
                try:
                    value, _type = winreg.QueryValueEx(key, "Scheduling Category")
                    inv.timer.mmcss_responsiveness = str(value or "")
                finally:
                    winreg.CloseKey(key)
            except OSError:
                inv.timer.mmcss_responsiveness = ""

    def read_usb_suspend(self, inv: Inventory) -> None:
        res = self.powershell(
            "powercfg /query SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 "
            "48e6b7a6-50f5-4782-a5d4-53bb8f07e226 | Out-String",
            timeout=30.0,
        )
        if not res.ok:
            inv.warnings.append(
                "usb selective suspend query failed; setting unknown")
            return
        ac = parse_powercfg_index(res.stdout, "AC")
        if ac is None:
            inv.warnings.append(
                "usb selective suspend setting unreadable; state unknown")
            return
        inv.usb_selective_suspend = bool(ac)

    def read_network(self, inv: Inventory) -> None:
        import json

        res = self.powershell(
            "$ErrorActionPreference='SilentlyContinue'; "
            "$a = @(Get-NetAdapter -Physical | Select-Object "
            "InterfaceDescription,Name,Status,MtuSize,PhysicalMediaType); "
            "$p = @(Get-NetAdapter -Physical | Get-NetAdapterPowerManagement | "
            "Select-Object Name,AllowComputerToTurnOffDevice); "
            "$m = @(Get-NetAdapter -Physical | Get-NetAdapterAdvancedProperty | "
            "Where-Object {$_.DisplayName -eq 'Interrupt Moderation'} | "
            "Select-Object Name,DisplayValue); "
            "@{a=$a;p=$p;m=$m} | ConvertTo-Json -Compress -Depth 3",
            timeout=45.0,
        )
        if not res.ok or not res.stdout.strip():
            inv.warnings.append("network query failed; NIC state unknown")
            return
        try:
            raw = json.loads(res.stdout)
        except ValueError:
            inv.warnings.append("network query returned unparsable output")
            return
        inv.nics.extend(parse_nics(raw))

    def read_display(self, inv: Inventory) -> None:
        import json

        hags = None
        if sys.platform.startswith("win"):
            try:
                import winreg

                key = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers",
                )
                try:
                    mode, _type = winreg.QueryValueEx(key, "HwSchMode")
                    hags = int(mode) == 2
                finally:
                    winreg.CloseKey(key)
            except (OSError, ValueError):
                hags = None

        res = self.powershell(
            "Get-CimInstance Win32_VideoController | Select-Object "
            "Name,DriverVersion,CurrentRefreshRate,MaxRefreshRate,"
            "CurrentHorizontalResolution,CurrentVerticalResolution | "
            "ConvertTo-Json -Compress",
            timeout=30.0,
        )
        if not res.ok or not res.stdout.strip():
            inv.warnings.append("display query failed; GPU state unknown")
            return
        try:
            raw = json.loads(res.stdout)
        except ValueError:
            inv.warnings.append("display query returned unparsable output")
            return
        gpus, displays = parse_gpus_displays(raw, hags)
        inv.gpus.extend(gpus)
        inv.displays.extend(displays)

    def read_dns(self, inv: Inventory) -> None:
        import json
        import socket
        import time

        start = time.perf_counter()
        try:
            socket.getaddrinfo("example.com", 443)
            inv.dns.resolve_ms = (time.perf_counter() - start) * 1000.0
        except OSError as exc:
            inv.warnings.append(f"dns probe failed: {exc}")
        res = self.powershell(
            "Get-DnsClientServerAddress -AddressFamily IPv4 | "
            "Select-Object -ExpandProperty ServerAddresses | ConvertTo-Json -Compress",
            timeout=30.0,
        )
        if res.ok and res.stdout.strip():
            try:
                raw = json.loads(res.stdout)
                addrs = raw if isinstance(raw, list) else [raw]
                inv.dns.server_addresses = [str(a) for a in addrs if a]
            except ValueError:
                inv.warnings.append("dns server list returned unparsable output")

    def read_processes(self, inv: Inventory) -> None:
        rows = self._process_counter_rows()
        if rows is None:
            rows = self._process_delta_rows()
        if not rows:
            return
        latest: dict[str, tuple[str, float]] = {}
        for name, stamp, pct in rows:
            if name.lower() in ("idle", "_total"):
                continue
            prev = latest.get(name)
            if prev is None or stamp >= prev[0]:
                latest[name] = (stamp, pct)
        ranked = sorted(latest.items(), key=lambda kv: kv[1][1], reverse=True)
        for name, (_, pct) in ranked[:8]:
            inv.top_cpu_processes.append((name, round(pct, 1)))

    def _process_counter_rows(self) -> list[tuple[str, str, float]] | None:
        import json

        res = self.powershell(
            "Get-Counter '\\Process(*)\\% Processor Time' -SampleInterval 1 "
            "-MaxSamples 2 | Select-Object -ExpandProperty CounterSamples | "
            "Select-Object InstanceName,CookedValue,Timestamp | ConvertTo-Json -Compress",
            timeout=30.0,
        )
        if not res.ok or not res.stdout.strip():
            return None
        try:
            raw = json.loads(res.stdout)
        except ValueError:
            return None
        rows = raw if isinstance(raw, list) else [raw]
        out: list[tuple[str, str, float]] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("InstanceName"):
                continue
            try:
                pct = float(row.get("CookedValue") or 0.0)
            except (TypeError, ValueError):
                continue
            out.append(
                (str(row["InstanceName"]), str(row.get("Timestamp") or ""), pct)
            )
        return out or None

    def _process_delta_rows(self) -> list[tuple[str, str, float]]:
        import json

        res = self.powershell(
            "$n = $env:NUMBER_OF_PROCESSORS; "
            "$a = @{}; "
            "Get-Process -ErrorAction SilentlyContinue | ForEach-Object { "
            "$a[$_.Id] = @($_.ProcessName, $_.CPU) }; "
            "Start-Sleep -Seconds 1; "
            "Get-Process -ErrorAction SilentlyContinue | ForEach-Object { "
            "if ($a.ContainsKey($_.Id)) { "
            "$old = $a[$_.Id]; "
            "$pct = (($_.CPU - $old[1]) / $n) * 100.0; "
            "[pscustomobject]@{ Name = $old[0]; Pct = $pct } } } | "
            "Sort-Object Pct -Descending | Select-Object -First 8 Name,Pct | "
            "ConvertTo-Json -Compress",
            timeout=30.0,
        )
        if not res.ok or not res.stdout.strip():
            return []
        try:
            raw = json.loads(res.stdout)
        except ValueError:
            return []
        rows = raw if isinstance(raw, list) else [raw]
        out: list[tuple[str, str, float]] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("Name"):
                continue
            try:
                pct = float(row.get("Pct") or 0.0)
            except (TypeError, ValueError):
                continue
            out.append((str(row["Name"]), "", pct))
        return out

    def read_anti_cheat(self, inv: Inventory) -> None:
        from system_core import AntiCheatPolicy

        inv.running_anti_cheat = AntiCheatPolicy().detect(inv)


class FakeBackend(Backend):
    def fixture_inventory(self) -> Inventory | None:
        return build_fake_inventory()

    def powershell(self, script: str, timeout: float = 60.0) -> BackendResult:
        _ = (script, timeout)
        return BackendResult(ok=True, stdout="{}", stderr="", code=0)

    def read_system(self) -> Inventory:
        return build_fake_inventory()

    def read_network(self, inv: Inventory) -> None:
        _ = inv

    def read_display(self, inv: Inventory) -> None:
        _ = inv

    def read_dns(self, inv: Inventory) -> None:
        _ = inv

    def read_processes(self, inv: Inventory) -> None:
        _ = inv

    def read_anti_cheat(self, inv: Inventory) -> None:
        _ = inv


def _vendor_of(name: str) -> Vendor:
    low = name.lower()
    if "nvidia" in low or "geforce" in low:
        return Vendor.NVIDIA
    if "amd" in low or "radeon" in low:
        return Vendor.AMD
    if "intel" in low:
        return Vendor.INTEL
    return Vendor.OTHER


def make_backend(name: str | None) -> Backend:
    if name == "fake":
        return FakeBackend()
    if sys.platform.startswith("win"):
        return RealBackend()
    return FakeBackend()


def _exit_name(brand: str) -> str:
    clean = "".join(c for c in brand if c.isalnum()) or "SnappyAI"
    return f"Exit-{clean}"


def write_powershell(path: str | Path, text: str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    return p


_HEADER = """<#
.SYNOPSIS
    Generated by @@BRAND@@. Review this file before running it.

.DESCRIPTION
    @@BRAND@@ does not modify your system. It generates this script so you can read exactly
    what will change, run it yourself, and undo it yourself.

    Snapshot : @@SNAPSHOT@@
    Undo     : .\\@@UNDO@@
    Generated: @@GENERATED_AT@@

    Every change below records its previous value into the snapshot BEFORE it is applied,
    so the undo script restores YOUR prior state rather than a generic default.

.NOTES
    Run from an elevated PowerShell prompt:
        Set-ExecutionPolicy -Scope Process Bypass -Force
        .\\@@APPLY@@
#>
"""

_APPLY_PREAMBLE = r"""#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$WhatIf2,
    [switch]$SkipManual,
    [switch]$Force,
    [switch]$NoPause,
    [switch]$Elevate
)

$ErrorActionPreference = 'Stop'
$script:Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:Snapshot = Join-Path $Here '@@SNAPSHOT@@'
$script:Changes = @()
$script:Interactive = [Environment]::UserInteractive
$script:IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

function Exit-@@EXIT@@ {
    param([int]$Code = 0)
    if ($script:Interactive -and -not $NoPause -and -not $WhatIf2) {
        Write-Host ''
        if ($Code -ne 0) {
            Write-Host ("Exiting with code {0}. The text above says what failed." -f $Code) -ForegroundColor Red
        }
        Read-Host 'Press Enter to close this window'
    }
    exit $Code
}
$script:NeedsAdmin = @@NEEDS_ADMIN@@
if ($script:NeedsAdmin -and -not $script:IsAdmin) {
    Write-Host ''
    Write-Host 'This plan contains changes that require ADMINISTRATOR rights.' -ForegroundColor Yellow
    Write-Host 'Running without elevation will fail on those steps.' -ForegroundColor Yellow
    if ($Elevate) {
        Write-Host 'Relaunching elevated...' -ForegroundColor Cyan
        $arglist = @('-ExecutionPolicy','Bypass','-NoProfile','-File', ('"' + $MyInvocation.MyCommand.Path + '"'), '-NoPause')
        if ($WhatIf2) { $arglist += '-WhatIf2' }
        if ($SkipManual) { $arglist += '-SkipManual' }
        if ($Force) { $arglist += '-Force' }
        try {
            Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $arglist
            Exit-@@EXIT@@ 0
        } catch {
            Write-Host ("Could not relaunch elevated ({0}); continuing WITHOUT elevation." -f $_.Exception.Message) -ForegroundColor Yellow
        }
    } else {
        Write-Host 'Re-run from an elevated PowerShell, or pass -Elevate to relaunch automatically:' -ForegroundColor Yellow
        Write-Host ('    powershell -ExecutionPolicy Bypass -File "' + $MyInvocation.MyCommand.Path + '" -Elevate')
    }
    Write-Host ''
}

function Write-Step {
    param([string]$Tag, [string]$Text)
    Write-Host ("[{0}] {1}" -f $Tag, $Text) -ForegroundColor Cyan
}

function Write-Warn2 {
    param([string]$Text)
    Write-Host "  ! $Text" -ForegroundColor Yellow
}

function Save-Snapshot {
    # Merge with any existing snapshot: the EARLIEST recorded prior value
    # for a change wins, and records from earlier runs that this run did
    # not touch are kept. A second apply (or a subset apply) can therefore
    # never replace the original priors with post-change values.
    $new = @($script:Changes)
    $merged = $new
    try {
        if (Test-Path -LiteralPath $script:Snapshot) {
            $old = @(Get-Content -LiteralPath $script:Snapshot -Raw | ConvertFrom-Json)
            if ($old.Count -gt 0 -and $null -ne $old[0]) {
                $byKey = [ordered]@{}
                foreach ($o in $old) {
                    if ($null -eq $o) { continue }
                    $k = "$($o.tweak)|$($o.kind)|$($o.hive)|$($o.path)|$($o.name)|$($o.command)|$($o.description)"
                    if (-not $byKey.Contains($k)) { $byKey[$k] = $o }
                }
                foreach ($n in $new) {
                    $k = "$($n.tweak)|$($n.kind)|$($n.hive)|$($n.path)|$($n.name)|$($n.command)|$($n.description)"
                    if (-not $byKey.Contains($k)) { $byKey[$k] = $n }
                }
                $merged = @($byKey.Values)
            }
        }
    } catch {
        $merged = $new
    }
    $merged | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $script:Snapshot -Encoding UTF8
    Write-Host ""
    Write-Host "Snapshot written to $script:Snapshot (earliest recorded prior values win)." -ForegroundColor Green
    Write-Host ("{0} change(s) recorded. Undo with: .\@@UNDO@@" -f @($merged).Count) -ForegroundColor Green
}

function Read-RegistryValue {
    param([string]$Path, [string]$Name)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    $item = Get-Item -LiteralPath $Path
    $ErrorActionPreference = $prev
    if ($null -eq $item) { return $null }
    if ($item.GetValueNames() -notcontains $Name) { return $null }
    $v = $item.GetValue($Name, $null, 'DoNotExpandEnvironmentNames')
    $k = [string]$item.GetValueKind($Name)
    return [pscustomobject]@{ value = $v; kind = $k }
}

function Confirm-Disruptive {
    param([string]$What)
    if ($Force) { return $true }
    Write-Host ""
    Write-Warn2 "DISRUPTIVE: $What"
    $ans = Read-Host "  Continue? [y/N]"
    return ($ans -match '^(y|Y)$')
}

function Invoke-Change {
    param(
        [string]$TweakId,
        [string]$Description,
        [string]$Kind,
        [string]$Hive,
        [string]$Path,
        [string]$Name,
        $Value,
        [string]$ValueType,
        [string]$Command,
        [string]$Notes,
        [string]$Text,
        [string]$Safety,
        [bool]$Reversible,
        [bool]$RequiresReboot,
        [scriptblock]$Apply,
        [scriptblock]$Undo
    )
    Write-Step $TweakId $Description
    if ($Notes) { Write-Warn2 $Notes }
    if (-not $Reversible) {
        Write-Host "    IRREVERSIBLE: there is no undo for this step." -ForegroundColor Red
        if ($Safety) { Write-Host ("    Safeguard: {0}" -f $Safety) -ForegroundColor DarkYellow }
        if (-not $Force) {
            $ans = Read-Host "    Run it anyway? [y/N]"
            if ($ans -notmatch '^(y|Y)$') {
                Write-Host "    skipped by user" -ForegroundColor DarkGray
                return
            }
        }
    }
    $prior = $null
    if (($Kind -eq 'registry_set' -or $Kind -eq 'registry_delete') -and $Path -and $Name) {
        $root = switch ($Hive) { 'HKLM' { 'HKLM:' } 'HKCU' { 'HKCU:' } default { $Hive } }
        $full = if ($root) { Join-Path $root $Path } else { $Path }
        $prior = Read-RegistryValue -Path $full -Name $Name
        $shown = if ($null -eq $prior) { '<absent>' } else { "$($prior.value) ($($prior.kind))" }
        Write-Host ("    prior value: {0}" -f $shown)
    }
    $record = [pscustomobject]@{
        tweak = $TweakId
        description = $Description
        kind = $Kind
        hive = $Hive
        path = $Path
        name = $Name
        prior = $prior
        applied = $Value
        valueType = $ValueType
        command = $Command
        undoScript = $Undo.ToString()
        reversible = $Reversible
        safety = $Safety
        requiresReboot = $RequiresReboot
    }
    if ($WhatIf2) {
        Write-Host "    [preview] no change made" -ForegroundColor DarkGray
        $script:Changes += $record
        return
    }
    try {
        & $Apply
        $script:Changes += $record
        Write-Host "    applied" -ForegroundColor Green
    } catch {
        Write-Host ("    FAILED: {0}" -f $_.Exception.Message) -ForegroundColor Red
        Write-Warn2 "Stopping. Partial application is worse than no application."
        Save-Snapshot
        Exit-@@EXIT@@ 1
    }
}

Write-Host ""
Write-Host "@@BRAND@@ apply script" -ForegroundColor Magenta
Write-Host "--------------------" -ForegroundColor Magenta
if ($WhatIf2) { Write-Host "PREVIEW MODE - no changes will be made" -ForegroundColor Yellow }
Write-Host ""
"""

_UNDO_HEADER = r"""<#
.SYNOPSIS
    @@BRAND@@ undo. Restores state from the snapshot written by the apply script.

.DESCRIPTION
    Restores the values recorded in @@SNAPSHOT@@, which are the values observed on THIS
    machine before the change, not generic defaults. If the snapshot is missing, this
    script changes nothing rather than guessing at a default.

    Preview what it would do:  .\@@UNDO@@ -WhatIf2
#>
#Requires -Version 5.1
[CmdletBinding()]
param([switch]$WhatIf2, [switch]$NoPause, [switch]$Force, [switch]$SkipManual)

$ErrorActionPreference = 'Stop'
$script:Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:Snapshot = Join-Path $Here '@@SNAPSHOT@@'
$script:Interactive = [Environment]::UserInteractive

function Exit-@@EXIT@@ {
    param([int]$Code = 0)
    if ($script:Interactive -and -not $NoPause -and -not $WhatIf2) {
        Write-Host ''
        Read-Host 'Press Enter to close this window'
    }
    exit $Code
}

if (-not (Test-Path -LiteralPath $script:Snapshot)) {
    Write-Host "Snapshot not found at $script:Snapshot" -ForegroundColor Red
    Write-Host "Nothing to undo, and @@BRAND@@ will not guess at your prior values." -ForegroundColor Red
    Exit-@@EXIT@@ 1
}

$raw = Get-Content -LiteralPath $script:Snapshot -Raw
$changes = @($raw | ConvertFrom-Json)
if ($changes.Count -eq 0 -or ($changes.Count -eq 1 -and $null -eq $changes[0].tweak)) {
    Write-Host "Snapshot is empty; nothing was applied." -ForegroundColor Yellow
    Exit-@@EXIT@@ 0
}

Write-Host ""
Write-Host "@@BRAND@@ undo" -ForegroundColor Magenta
Write-Host ("Restoring {0} recorded change(s), newest first." -f $changes.Count)
Write-Host ""

$ordered = @($changes)
[array]::Reverse($ordered)

$failed = 0
foreach ($c in $ordered) {
    Write-Host ("[{0}] {1}" -f $c.tweak, $c.description) -ForegroundColor Cyan
    if ($WhatIf2) {
        Write-Host "    [preview] would restore prior value" -ForegroundColor DarkGray
        continue
    }
    try {
        if (-not $c.undoScript) {
            Write-Host "    no undo recorded for this change" -ForegroundColor Yellow
            continue
        }
        $undo = [scriptblock]::Create($c.undoScript)
        & $undo
        Write-Host "    restored" -ForegroundColor Green
    } catch {
        Write-Host ("    FAILED to restore: {0}" -f $_.Exception.Message) -ForegroundColor Red
        $failed++
    }
}

Write-Host ""
if ($failed -gt 0) {
    Write-Host ("{0} change(s) could not be restored. Review them manually." -f $failed) -ForegroundColor Red
    Exit-@@EXIT@@ 1
}
Write-Host "All recorded changes restored." -ForegroundColor Green
if (@($changes | Where-Object { $_.requiresReboot }).Count -gt 0) {
    Write-Host "Some of these changes require a reboot to fully unwind." -ForegroundColor Yellow
}
Exit-@@EXIT@@ 0
"""


def _fill(template: str, **kw: str) -> str:
    out = template
    for key, val in kw.items():
        out = out.replace(f"@@{key.upper()}@@", val)
    return out


@dataclass(slots=True)
class PlannedChange:
    tweak: Tweak
    steps: list[Step]
    finding_summary: str = ""
    policy_notes: list[str] = field(default_factory=list)

    @property
    def requires_reboot(self) -> bool:
        return self.tweak.requires_reboot or any(s.needs_reboot for s in self.steps)

    @property
    def is_manual_only(self) -> bool:
        return all(s.is_manual for s in self.steps)

    @property
    def automated_step_count(self) -> int:
        return sum(1 for s in self.steps if not s.is_manual)


@dataclass(slots=True)
class ScriptBundle:
    apply_ps1: str
    undo_ps1: str
    snapshot_name: str
    apply_name: str
    undo_name: str
    changes: list[PlannedChange]
    manifest: dict[str, Any]

    def write(self, outdir: str | Path) -> dict[str, Path]:
        d = Path(outdir)
        d.mkdir(parents=True, exist_ok=True)
        paths = {
            "apply": d / self.apply_name,
            "undo": d / self.undo_name,
            "manifest": d / "manifest.json",
        }
        write_powershell(paths["apply"], self.apply_ps1)
        write_powershell(paths["undo"], self.undo_ps1)
        tmp = d / "manifest.json.tmp"
        tmp.write_text(
            json.dumps(self.manifest, indent=1, default=str), encoding="utf-8"
        )
        with open(tmp, "rb") as fh:
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, paths["manifest"])
        return paths


def _ps_string(value: Any) -> str:
    if value is None:
        return "$null"
    if isinstance(value, bool):
        return "$true" if value else "$false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "@(" + ", ".join(_ps_string(v) for v in value) + ")"
    s = str(value).replace("'", "''")
    return f"'{s}'"


def _hive_to_ps(hive: str) -> str:
    return {
        "HKLM": "HKLM:",
        "HKEY_LOCAL_MACHINE": "HKLM:",
        "HKCU": "HKCU:",
        "HKEY_CURRENT_USER": "HKCU:",
    }.get(hive.upper(), hive)


_PS_VALUE_TYPES = ("DWord", "String", "QWord", "Binary", "MultiString",
                   "ExpandString")


def _ps_lit(value: Any) -> str:
    return str(value).replace("'", "''")


def _step_apply_block(step: Step) -> str:
    if step.kind == StepKind.REGISTRY_SET:
        if step.value_type not in _PS_VALUE_TYPES:
            raise ValueError(
                f"unsupported registry value type {step.value_type!r}")
        path = _ps_lit(f"{_hive_to_ps(step.hive or 'HKLM')}\\{step.path}")
        name = _ps_lit(step.value_name)
        return textwrap.dedent(
            f"""
            if (-not (Test-Path -LiteralPath '{path}')) {{
                New-Item -Path '{path}' -Force | Out-Null
            }}
            New-ItemProperty -LiteralPath '{path}' -Name '{name}' `
                -Value {_ps_string(step.value)} -PropertyType {step.value_type} -Force | Out-Null
            """
        ).strip()
    if step.kind == StepKind.REGISTRY_DELETE:
        path = _ps_lit(f"{_hive_to_ps(step.hive or 'HKLM')}\\{step.path}")
        name = _ps_lit(step.value_name)
        return (
            "$ErrorActionPreference='SilentlyContinue'; "
            f"Remove-ItemProperty -LiteralPath '{path}' -Name '{name}' "
            "-ErrorAction SilentlyContinue; $ErrorActionPreference='Stop'"
        )
    if step.kind in (
        StepKind.POWERSHELL,
        StepKind.NETSH,
        StepKind.POWERCFG,
        StepKind.BCDEDIT,
        StepKind.SERVICE,
        StepKind.SCHEDULED_TASK,
        StepKind.FILE_DELETE,
        StepKind.WINGET_INSTALL,
        StepKind.MEMORY_ACTION,
    ):
        return step.command or ""
    raise ValueError(f"cannot render step kind {step.kind!r}")


def _registry_restore_block(
    hive: str | None, path: str | None, name: str | None
) -> str:
    ps_hive = _hive_to_ps(hive or "HKLM")
    full = _ps_lit(f"{ps_hive}\\{path}")
    name_lit = _ps_lit(name)
    return textwrap.dedent(
        f"""\
        $root = '{ps_hive}'
        $full = '{full}'
        if ($null -eq $c.prior) {{
            Remove-ItemProperty -LiteralPath $full -Name '{name_lit}' `
                -ErrorAction SilentlyContinue
        }} else {{
            if (-not (Test-Path -LiteralPath $full)) {{
                New-Item -Path $full -Force | Out-Null
            }}
            $v = $c.prior.value
            if ("$($c.prior.kind)" -eq 'Binary') {{
                if ($v -is [string]) {{ $v = [Convert]::FromBase64String($v) }}
                else {{ $v = [byte[]]@($v) }}
            }}
            New-ItemProperty -LiteralPath $full -Name '{name_lit}' -Value $v `
                -PropertyType $c.prior.kind -Force | Out-Null
        }}"""
    ).strip()


def _step_undo_block(step: Step) -> str:
    if step.kind in (StepKind.REGISTRY_SET, StepKind.REGISTRY_DELETE):
        return _registry_restore_block(step.hive, step.path, step.value_name)
    if not step.reversible:
        return (
            "Write-Host 'This step was irreversible; there is nothing to restore.' "
            "-ForegroundColor Yellow"
        )
    parts = []
    for u in step.undo:
        if u.is_manual:
            continue
        try:
            parts.append(_step_apply_block(u))
        except ValueError:
            continue
    if not parts:
        parts.append(
            f"Write-Host 'No automatic undo recorded for: {step.description}' "
            "-ForegroundColor Yellow"
        )
    return "\n".join(parts)


def _is_disruptive(step: Step) -> bool:
    cmd = (step.command or "").lower()
    return (
        "disable-netadapter " in cmd
        or "restart-netadapter" in cmd
        or "restart-computer" in cmd
        or "set-dnsclientserveraddress" in cmd
        or "remove-appxpackage" in cmd
        or "stop-process -name explorer" in cmd
        or "clear-recyclebin" in cmd
        or "stop-service" in cmd
    )


def indent_body(body: str, prefix: str) -> str:
    out = []
    for ln in body.split("\n"):
        if not ln.strip():
            out.append("")
        else:
            out.append(prefix + ln)
    return "\n".join(out)


def _plan_needs_admin(changes: Iterable[PlannedChange]) -> bool:
    admin_tokens = (
        "set-netadapteradvancedproperty",
        "disable-netadapter",
        "enable-netadapter",
        "set-dnsclientserveraddress",
        "new-netqospolicy",
        "remove-netqospolicy",
        "powercfg /set",
        "bcdedit",
    )
    for pc in changes:
        for step in pc.steps:
            if step.is_manual:
                continue
            if step.hive and step.hive.upper() in ("HKLM", "HKEY_LOCAL_MACHINE"):
                return True
            if step.kind in (StepKind.BCDEDIT, StepKind.SERVICE):
                return True
            cmd = (step.command or "").lower()
            if any(tok in cmd for tok in admin_tokens):
                return True
    return False


def render_bundle(
    changes: Iterable[PlannedChange],
    *,
    generated_at: str,
    brand: str = "latency",
    inventory_digest: dict[str, Any] | None = None,
    apply_name: str = "apply.ps1",
    undo_name: str = "undo.ps1",
    snapshot_name: str = "snapshot.json",
    warnings: int = 0,
) -> ScriptBundle:
    changes = list(changes)
    needs_admin = "$true" if _plan_needs_admin(changes) else "$false"
    exit_name = _exit_name(brand)
    header = _fill(
        _HEADER,
        snapshot=snapshot_name,
        undo=undo_name,
        apply=apply_name,
        generated_at=generated_at,
        brand=brand,
        exit=exit_name,
    )
    preamble = _fill(
        _APPLY_PREAMBLE, snapshot=snapshot_name, undo=undo_name,
        needs_admin=needs_admin, brand=brand, exit=exit_name,
    )
    body: list[str] = []
    manual_blocks: list[str] = []
    reboot_needed = False
    total_automated = 0
    for pc in changes:
        t = pc.tweak
        body.append(f"\n# {'=' * 74}")
        body.append(f"# {t.id} - {t.title}")
        body.append(f"# component : {t.component.value}")
        body.append(f"# confidence: {t.confidence.value}   risk: {t.risk.value}")
        if pc.finding_summary:
            body.append("#")
            for line in textwrap.wrap(pc.finding_summary, 76):
                body.append(f"#   {line}")
        body.append(f"# expected  : {t.expected_effect}")
        body.append(
            f"# measure   : {t.measure_metric}, meaningful if >= "
            f"{t.measure_min_effect_ms} ms"
        )
        if t.caveats:
            body.append("#")
            for line in textwrap.wrap("CAVEAT: " + t.caveats, 76):
                body.append(f"#   {line}")
        body.append(f"# {'=' * 74}")
        if pc.requires_reboot:
            reboot_needed = True
        for step in pc.steps:
            if step.is_manual:
                manual_blocks.append(
                    _render_manual_block(t.id, step, skip_var="$SkipManual")
                )
                continue
            total_automated += 1
            apply_body = _step_apply_block(step)
            undo_body = _step_undo_block(step)
            call = (
                "Invoke-Change "
                f"-TweakId {_ps_string(t.id)} "
                f"-Description {_ps_string(step.description)} "
                f"-Kind {_ps_string(step.kind)} "
                f"-Hive {_ps_string(step.hive)} "
                f"-Path {_ps_string(step.path)} "
                f"-Name {_ps_string(step.value_name)} "
                f"-Value {_ps_string(step.value)} "
                f"-ValueType {_ps_string(step.value_type)} "
                f"-Command {_ps_string(step.command)} "
                f"-Notes {_ps_string(step.notes)} "
                f"-Text {_ps_string(step.text)} "
                f"-Safety {_ps_string(step.safety)} "
                f"-Reversible ${'true' if step.reversible else 'false'} "
                f"-RequiresReboot ${'true' if step.requires_reboot else 'false'} "
                f"-Apply {{\n{indent_body(apply_body, '        ')}\n    }} "
                f"-Undo {{\n{indent_body(undo_body, '        ')}\n    }}"
            )
            if _is_disruptive(step):
                call = (
                    f"if (-not (Confirm-Disruptive -What "
                    f"{_ps_string(step.description)})) {{\n"
                    f"    Write-Step {t.id!r} 'skipped by user'\n"
                    "} else {\n" + textwrap.indent(call, "    ") + "\n}"
                )
            body.append(call)
            body.append("")
    tail: list[str] = []
    tail.append("\nif (-not $WhatIf2) {")
    tail.append("    Save-Snapshot")
    tail.append("} else {")
    tail.append('    Write-Host "Preview mode - the existing snapshot'
                + ' (if any) was left untouched." -ForegroundColor DarkGray')
    tail.append("}")
    tail.append("")
    if manual_blocks:
        tail.append("# " + "-" * 74)
        tail.append("# MANUAL STEPS")
        tail.append("# These could not be automated reliably and stop for reading instead.")
        tail.append("# " + "-" * 74)
        tail.extend(manual_blocks)
    tail.append("\nWrite-Host ''")
    if total_automated == 0:
        tail.append(
            'Write-Host "This plan contains no automated changes; everything above is a '
            'diagnostic or a manual instruction." -ForegroundColor Yellow'
        )
    tail.append(
        f'Write-Host "{len(changes)} ops applied, {warnings} warnings, reboot required: '
        f'{"yes" if reboot_needed else "no"}"'
    )
    tail.append(
        'Write-Host "Now measure again BEFORE deciding to keep these changes." '
        "-ForegroundColor Magenta"
    )
    tail.append(
        f'Write-Host "If the measurement did not improve, run the undo script: '
        f'{undo_name}" -ForegroundColor Magenta'
    )
    if reboot_needed:
        tail.append(
            'Write-Host "A reboot is required for some changes to take effect." '
            "-ForegroundColor Yellow"
        )
    tail.append("")
    apply_ps1 = header + preamble + "\n".join(body) + "\n".join(tail)
    undo_ps1 = _fill(
        _UNDO_HEADER, snapshot=snapshot_name, undo=undo_name, brand=brand,
        exit=exit_name,
    )
    manifest = {
        "version": 1,
        "generated_at": generated_at,
        "applied_utc": None,
        "apply_script": apply_name,
        "undo_script": undo_name,
        "snapshot": snapshot_name,
        "reboot_required": reboot_needed,
        "warning_count": warnings,
        "automated_step_count": total_automated,
        "manual_step_count": len(manual_blocks),
        "changes": [
            {
                "id": pc.tweak.id,
                "title": pc.tweak.title,
                "component": pc.tweak.component.value,
                "confidence": pc.tweak.confidence.value,
                "risk": pc.tweak.risk.value,
                "requires_reboot": pc.requires_reboot,
                "finding": pc.finding_summary,
                "expected_effect": pc.tweak.expected_effect,
                "measure": {
                    "metric": pc.tweak.measure_metric,
                    "min_effect_ms": pc.tweak.measure_min_effect_ms,
                    "needs_load": pc.tweak.needs_load_measurement,
                    "samples": pc.tweak.measure_samples,
                },
                "caveats": pc.tweak.caveats,
                "evidence": list(pc.tweak.evidence_urls),
                "policy_notes": pc.policy_notes,
                "steps": [
                    {
                        "kind": s.kind,
                        "description": s.description,
                        "command": s.command,
                        "hive": s.hive,
                        "path": s.path,
                        "value_name": s.value_name,
                        "value": s.value,
                        "requires_reboot": s.requires_reboot,
                        "text": s.text,
                        "notes": s.notes,
                        "reversible": s.reversible,
                        "safety": s.safety,
                        "manual": s.is_manual,
                        "undo_steps": len(s.undo),
                    }
                    for s in pc.steps
                ],
            }
            for pc in changes
        ],
        "inventory_digest": inventory_digest or {},
    }
    return ScriptBundle(
        apply_ps1=apply_ps1,
        undo_ps1=undo_ps1,
        snapshot_name=snapshot_name,
        apply_name=apply_name,
        undo_name=undo_name,
        changes=changes,
        manifest=manifest,
    )


def render_maintenance_script(
    steps: list[Step], *, title: str, generated_at: str, brand: str = "latency"
) -> str:
    exit_name = _exit_name(brand)
    out = [
        "<#",
        ".SYNOPSIS",
        f"    {title}",
        "",
        ".DESCRIPTION",
        "    Generated by @@BRAND@@. Review before running.",
        "",
        "    These are MAINTENANCE operations: they free disk space or install missing",
        "    runtime packages. They are NOT latency optimizations and no claim is made",
        "    that they will reduce your ping or your frametimes.",
        "",
        "    Every registry/command step records its previous value into",
        "    <this script>.snapshot.json BEFORE it applies. Run this same script",
        "    with -Undo to restore that snapshot (newest change first).",
        "",
        "    Irreversible steps (deletions, installs) prompt before running unless -Force",
        "    is passed. Each states its safeguard. Declining one step skips ONLY that",
        "    step - the rest still run.",
        "",
        f"    Generated: {generated_at}",
        "#>",
        "#Requires -Version 5.1",
        "[CmdletBinding()]",
        "param([switch]$Force, [switch]$SkipManual, [switch]$NoPause, [switch]$Undo)",
        "",
        "$ErrorActionPreference = 'Continue'",
        "$script:Interactive = [Environment]::UserInteractive",
        "$script:Here = Split-Path -Parent $MyInvocation.MyCommand.Path",
        "$script:Leaf = Split-Path -Leaf $MyInvocation.MyCommand.Path",
        "$script:Snapshot = Join-Path $Here ($script:Leaf + '.snapshot.json')",
        "$script:Changes = @()",
        "",
        "function Exit-@@EXIT@@ {",
        "    param([int]$Code = 0)",
        "    if ($script:Interactive -and -not $NoPause) {",
        "        Write-Host ''",
        "        if ($Code -ne 0) {",
        "            Write-Host ('Exiting with code ' + $Code + '.') -ForegroundColor Red",
        "        }",
        "        Read-Host 'Press Enter to close this window'",
        "    }",
        "    exit $Code",
        "}",
        "",
        "function Read-RegistryValue {",
        "    param([string]$Path, [string]$Name)",
        "    $prev = $ErrorActionPreference",
        "    $ErrorActionPreference = 'SilentlyContinue'",
        "    $item = Get-Item -LiteralPath $Path",
        "    $ErrorActionPreference = $prev",
        "    if ($null -eq $item) { return $null }",
        "    if ($item.GetValueNames() -notcontains $Name) { return $null }",
        "    $v = $item.GetValue($Name, $null, 'DoNotExpandEnvironmentNames')",
        "    $k = [string]$item.GetValueKind($Name)",
        "    return [pscustomobject]@{ value = $v; kind = $k }",
        "}",
        "",
        "function Save-Snapshot {",
        "    # Merge: earliest recorded prior wins; records from earlier runs",
        "    # that this run did not touch are kept (re-runs cannot replace",
        "    # the ORIGINAL priors with post-change values).",
        "    $new = @($script:Changes)",
        "    $merged = $new",
        "    try {",
        "        if (Test-Path -LiteralPath $script:Snapshot) {",
        "            $old = @(Get-Content -LiteralPath $script:Snapshot -Raw | ConvertFrom-Json)",
        "            if ($old.Count -gt 0 -and $null -ne $old[0]) {",
        "                $byKey = [ordered]@{}",
        "                foreach ($o in $old) {",
        "                    if ($null -eq $o) { continue }",
        '                    $k = "$($o.kind)|$($o.description)"',
        "                    if (-not $byKey.Contains($k)) { $byKey[$k] = $o }",
        "                }",
        "                foreach ($n in $new) {",
        '                    $k = "$($n.kind)|$($n.description)"',
        "                    if (-not $byKey.Contains($k)) { $byKey[$k] = $n }",
        "                }",
        "                $merged = @($byKey.Values)",
        "            }",
        "        }",
        "    } catch {",
        "        $merged = $new",
        "    }",
        "    $merged | ConvertTo-Json -Depth 10 |",
        "        Set-Content -LiteralPath $script:Snapshot -Encoding UTF8",
        '    Write-Host ("Snapshot written to {0} ({1} change(s), earliest priors kept)." -f '
        "$script:Snapshot, @($merged).Count) -ForegroundColor Green",
        "}",
        "",
        "$script:IsAdmin = ([Security.Principal.WindowsPrincipal]"
        "[Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole("
        "[Security.Principal.WindowsBuiltInRole]::Administrator)",
        "if (-not $script:IsAdmin) {",
        "    Write-Host 'Not running elevated. Steps needing administrator rights will fail and be reported.' -ForegroundColor Yellow",
        "    Write-Host ''",
        "}",
        "",
        "if ($Undo) {",
        "    $ErrorActionPreference = 'Stop'",
        "    if (-not (Test-Path -LiteralPath $script:Snapshot)) {",
        "        Write-Host 'No snapshot next to this script - it applied nothing, so there is nothing to undo.' -ForegroundColor Red",
        "        Exit-@@EXIT@@ 1",
        "    }",
        "    $raw = Get-Content -LiteralPath $script:Snapshot -Raw",
        "    $changes = @($raw | ConvertFrom-Json)",
        "    if ($changes.Count -eq 0 -or ($changes.Count -eq 1 -and $null -eq $changes[0].description)) {",
        "        Write-Host 'Snapshot is empty; nothing was applied.' -ForegroundColor Yellow",
        "        Exit-@@EXIT@@ 0",
        "    }",
        "    Write-Host ''",
        "    Write-Host '@@BRAND@@ maintenance undo' -ForegroundColor Magenta",
        '    Write-Host ("Restoring {0} recorded change(s), newest first." -f $changes.Count)',
        "    $ordered = @($changes)",
        "    [array]::Reverse($ordered)",
        "    $failed = 0",
        "    foreach ($c in $ordered) {",
        '        Write-Host ("[{0}]" -f $c.description) -ForegroundColor Cyan',
        "        try {",
        "            if (-not $c.undoScript) {",
        "                Write-Host '    no undo recorded for this change' -ForegroundColor Yellow",
        "                continue",
        "            }",
        "            $undo = [scriptblock]::Create($c.undoScript)",
        "            & $undo",
        "            Write-Host '    restored' -ForegroundColor Green",
        "        } catch {",
        '            Write-Host ("    FAILED to restore: {0}" -f $_.Exception.Message) -ForegroundColor Red',
        "            $failed++",
        "        }",
        "    }",
        "    Write-Host ''",
        "    if ($failed -gt 0) {",
        '        Write-Host ("{0} change(s) could not be restored. Review them manually." -f $failed) -ForegroundColor Red',
        "        Exit-@@EXIT@@ 1",
        "    }",
        "    Write-Host 'All recorded changes restored.' -ForegroundColor Green",
        "    Exit-@@EXIT@@ 0",
        "}",
        "",
        f"Write-Host '{title}' -ForegroundColor Magenta",
        "Write-Host '--------------------' -ForegroundColor Magenta",
        "Write-Host ''",
        "",
    ]
    n_irrev = 0
    for step in steps:
        if step.is_manual:
            out.append("if (-not $SkipManual) {")
            out.append("    Write-Host ''")
            out.append("    Write-Host 'MANUAL' -ForegroundColor Magenta")
            for ln in (step.text or step.notes or step.description).split("\n"):
                out.append(f"    Write-Host {_ps_string(ln)}")
            out.append("    Read-Host 'Press Enter when you have read this'")
            out.append("}")
            continue
        out.append("Write-Host ''")
        out.append(f"Write-Host {_ps_string(step.description)} -ForegroundColor Cyan")
        if step.notes:
            out.append(
                f"Write-Host ('  ' + {_ps_string(step.notes)}) -ForegroundColor DarkGray"
            )
        out.append("$run = $true")
        if not step.reversible:
            n_irrev += 1
            out.append(
                "Write-Host '  IRREVERSIBLE: there is no undo for this step.' "
                "-ForegroundColor Red"
            )
            if step.safety:
                out.append(
                    f"Write-Host ('  Safeguard: ' + {_ps_string(step.safety)}) "
                    "-ForegroundColor DarkYellow"
                )
            out.append("if (-not $Force) {")
            out.append("    $ans = Read-Host '  Run it anyway? [y/N]'")
            out.append(
                "    if ($ans -notmatch '^(y|Y)$') {"
                " Write-Host '  skipped' -ForegroundColor DarkGray; $run = $false }"
            )
            out.append("}")
        undo_body = _step_undo_block(step)
        out.append("if ($run) {")
        if step.kind in (StepKind.REGISTRY_SET, StepKind.REGISTRY_DELETE):
            full = f"{_hive_to_ps(step.hive or 'HKLM')}\\{step.path}"
            out.append(f"    $prior = Read-RegistryValue -Path {_ps_string(full)} "
                       f"-Name {_ps_string(step.value_name)}")
        else:
            out.append("    $prior = $null")
        out.append("    $record = [pscustomobject]@{")
        out.append(f"        description = {_ps_string(step.description)}")
        out.append(f"        kind = {_ps_string(step.kind)}")
        out.append("        prior = $prior")
        out.append("        undoScript = @'")
        for ln in undo_body.split("\n"):
            out.append(ln)
        out.append("'@")
        out.append("    }")
        out.append("    try {")
        out.append("        $ErrorActionPreference = 'Stop'")
        out.append(indent_body(_step_apply_block(step), "        "))
        out.append("        $ErrorActionPreference = 'Continue'")
        out.append("        $script:Changes += $record")
        out.append("    } catch {")
        out.append("        $ErrorActionPreference = 'Continue'")
        out.append(
            "        Write-Host ('  FAILED: ' + $_.Exception.Message) -ForegroundColor Red"
        )
        out.append("    }")
        out.append("}")
    out.append("")
    out.append("if (@($script:Changes).Count -gt 0) {")
    out.append("    Save-Snapshot")
    out.append(
        "    Write-Host ('Undo everything this script applied with:  powershell "
        "-ExecutionPolicy Bypass -File ' + $MyInvocation.MyCommand.Path + ' -Undo') "
        "-ForegroundColor Green"
    )
    out.append("}")
    out.append("Write-Host ''")
    out.append(
        f"Write-Host ('Done. {n_irrev} irreversible operation(s) were included.') "
        "-ForegroundColor Magenta"
    )
    out.append("Exit-@@EXIT@@ 0")
    return _fill("\n".join(out), brand=brand, exit=exit_name)


def _wrap_preserving(text: str, width: int = 76) -> list[str]:
    out: list[str] = []
    for line in text.split("\n"):
        if len(line) <= width:
            out.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        prefix = " " * min(indent, 8)
        chunks = textwrap.wrap(
            line,
            width=width,
            subsequent_indent=prefix,
            break_long_words=False,
            break_on_hyphens=False,
        )
        out.extend(chunks or [line])
    return out


def _render_manual_block(tweak_id: str, step: Step, skip_var: str = "") -> str:
    body = step.text or step.notes or step.description
    sep = "'" + "-" * 60 + "'"
    out = [
        "",
        f"if (-not {skip_var}) {{",
        "    Write-Host ''",
        f"    Write-Host 'MANUAL STEP [{tweak_id}]' -ForegroundColor Magenta",
        f"    Write-Host {sep} -ForegroundColor DarkGray",
    ]
    for ln in _wrap_preserving(body):
        out.append(f"    Write-Host {_ps_string(ln)}")
    if step.notes and step.text:
        out.append("    Write-Host ''")
        out.append(
            "    Write-Host 'Why this is not automated:' -ForegroundColor DarkYellow"
        )
        for ln in _wrap_preserving(step.notes):
            out.append(f"    Write-Host {_ps_string(ln)} -ForegroundColor DarkGray")
    out.append(f"    Write-Host {sep} -ForegroundColor DarkGray")
    out.append("    Read-Host 'Press Enter when you have read this'")
    out.append("}")
    return "\n".join(out)


_VERDICT_ICON = {
    Verdict.PROBLEM: "PROBLEM",
    Verdict.NO_PROBLEM: "ok",
    Verdict.NOT_APPLICABLE: "n/a",
    Verdict.UNKNOWN: "UNKNOWN",
}

_COMPONENT_ORDER = (
    BudgetComponent.LOCAL_QUEUE,
    BudgetComponent.NETWORK_PATH,
    BudgetComponent.HOST_PROCESSING,
    BudgetComponent.RENDER_INPUT,
)


@dataclass(slots=True)
class TweakOutcome:
    tweak: Tweak
    verdict: Verdict
    summary: str
    policy: PolicyDecision | None = None
    measured: dict[str, Any] = field(default_factory=dict)
    comparison: dict[str, Any] | None = None
    step_count: int = 0
    step_count_total: int = 0
    skipped_reason: str = ""
    requested_explicitly: bool = False

    @property
    def will_change(self) -> bool:
        return (
            self.step_count_total > 0
            and (self.policy is None or self.policy.allowed)
            and (self.verdict is Verdict.PROBLEM or self.requested_explicitly)
        )


@dataclass
class Report:
    inventory: Any
    outcomes: list[TweakOutcome]
    network: dict[str, Any] = field(default_factory=dict)
    bufferbloat: dict[str, Any] | None = None
    route: dict[str, Any] | None = None
    render_budget: dict[str, Any] = field(default_factory=dict)
    maintenance: list[dict[str, Any]] = field(default_factory=list)
    generated_at: str = ""
    measured_comparisons: list[dict[str, Any]] = field(default_factory=list)

    @property
    def actionable(self) -> list[TweakOutcome]:
        return [o for o in self.outcomes if o.will_change]

    @property
    def clean(self) -> list[TweakOutcome]:
        return [o for o in self.outcomes if o.verdict is Verdict.NO_PROBLEM]

    @property
    def not_applicable(self) -> list[TweakOutcome]:
        return [o for o in self.outcomes if o.verdict is Verdict.NOT_APPLICABLE]

    @property
    def unknowns(self) -> list[TweakOutcome]:
        return [o for o in self.outcomes if o.verdict is Verdict.UNKNOWN]

    @property
    def refused(self) -> list[TweakOutcome]:
        return [o for o in self.outcomes if o.tweak.is_refused]

    @property
    def policy_denied(self) -> list[TweakOutcome]:
        return [
            o
            for o in self.outcomes
            if o.policy is not None and not o.policy.allowed and not o.tweak.is_refused
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "inventory_warnings": list(getattr(self.inventory, "warnings", [])),
            "network": self.network,
            "bufferbloat": self.bufferbloat,
            "route": self.route,
            "render_budget": self.render_budget,
            "maintenance": self.maintenance,
            "comparisons": self.measured_comparisons,
            "counts": {
                "actionable": len(self.actionable),
                "clean": len(self.clean),
                "not_applicable": len(self.not_applicable),
                "unknown": len(self.unknowns),
                "refused": len(self.refused),
                "policy_denied": len(self.policy_denied),
            },
            "outcomes": [
                {
                    "id": o.tweak.id,
                    "title": o.tweak.title,
                    "component": o.tweak.component.value,
                    "confidence": o.tweak.confidence.value,
                    "risk": o.tweak.risk.value,
                    "verdict": o.verdict.value,
                    "summary": o.summary,
                    "measured": o.measured,
                    "step_count": o.step_count,
                    "step_count_total": o.step_count_total,
                    "requested_explicitly": o.requested_explicitly,
                    "skipped_reason": o.skipped_reason,
                    "policy_allowed": None if o.policy is None else o.policy.allowed,
                    "policy_reason": "" if o.policy is None else o.policy.reason,
                    "refusal_reason": o.tweak.refusal_reason,
                    "expected_effect": o.tweak.expected_effect,
                    "caveats": o.tweak.caveats,
                    "evidence": list(o.tweak.evidence_urls),
                    "comparison": o.comparison,
                }
                for o in self.outcomes
            ],
        }


def render_markdown(report: Report, brand: str = "latency") -> str:
    inv = report.inventory
    parts: list[str] = []
    add = parts.append
    add(f"# {brand} report")
    add("")
    add(f"Generated: {report.generated_at}")
    add(
        f"Host: {getattr(inv, 'os_name', '?')} "
        f"(build {getattr(inv, 'windows_build', '?')})"
    )
    cpu = getattr(inv, "cpu", None)
    add(f"CPU: {getattr(cpu, 'name', '?')}")
    gpu = inv.primary_gpu if hasattr(inv, "primary_gpu") else None
    add(f"GPU: {gpu.name if gpu else '?'}")
    nic = inv.primary_nic if hasattr(inv, "primary_nic") else None
    if nic:
        add(f"Primary NIC: {nic.interface_description or nic.name} ({nic.status})")
    add("")
    add("## Latency budget")
    add("")
    add(
        "Four independent terms. "
        "A tool that reports only one of them misattributes problems."
    )
    add("")
    add("| Component | What this run found |")
    add("|---|---|")
    for comp in _COMPONENT_ORDER:
        rows = [
            o
            for o in report.outcomes
            if o.tweak.component is comp and not o.tweak.is_refused
        ]
        problems = [o for o in rows if o.verdict is Verdict.PROBLEM]
        unknown = [o for o in rows if o.verdict is Verdict.UNKNOWN]
        if problems:
            cell = "**" + "; ".join(o.tweak.id for o in problems) + "**"
        elif unknown:
            cell = f"not determined ({len(unknown)} check(s) returned UNKNOWN)"
        elif rows:
            cell = "no issue found"
        else:
            cell = "no checks apply to this machine"
        add(f"| `{comp.value}` | {cell} |")
    add("")
    if report.render_budget:
        add("### Render/input budget estimate")
        add("")
        add("| Term | Estimate |")
        add("|---|---|")
        for k, v in report.render_budget.items():
            add(f"| {k} | {v} |")
        add("")
    net = report.network
    if net:
        add("## Network measurement")
        add("")
        for key in ("idle", "gateway"):
            d = net.get(key)
            if d:
                add(
                    f"- **{key}**: n={d.get('n')} p50={_f(d.get('p50'))} "
                    f"p95={_f(d.get('p95'))} p99={_f(d.get('p99'))} "
                    f"max={_f(d.get('max'))} ms, "
                    f"loss={_f(d.get('loss_pct'))}%, "
                    f"jitter={_f(d.get('jitter_mad'))} ms"
                )
        if net.get("transport_note"):
            add(f"- {net['transport_note']}")
        add("")
    rt = report.route
    if rt:
        add("## Route and physical floor")
        add("")
        add(f"- target: `{rt.get('target')}` -> `{rt.get('resolved_ip')}`")
        add(f"- responding hops: {rt.get('responding_hop_count')}")
        add(f"- measured RTT: {_f(rt.get('final_rtt_ms'))} ms")
        if rt.get("floor_ms") is not None:
            add(f"- propagation floor: ~{_f(rt.get('floor_ms'))} ms (**irreducible**)")
            add(f"- addressable headroom: ~{_f(rt.get('addressable_headroom_ms'))} ms")
        else:
            add(
                "- propagation floor: **not determined**; "
                "a traceroute alone cannot set it"
            )
        add("")
    bb = report.bufferbloat
    if bb:
        add("## Bufferbloat (latency under load)")
        add("")
        add(f"**Grade: {str(bb.get('grade', '?')).upper()}**")
        add("")
        add(str(bb.get("explanation", "")))
        add("")
        add("| Phase | n | p50 | p95 | p99 | max | loss |")
        add("|---|---|---|---|---|---|---|")
        for phase in ("idle", "download", "upload"):
            d = bb.get(phase)
            if not d:
                add(f"| {phase} | — | — | — | — | — | NOT TESTED |")
            else:
                add(
                    f"| {phase} | {d.get('n')} | {_f(d.get('p50'))} | "
                    f"{_f(d.get('p95'))} | {_f(d.get('p99'))} | "
                    f"{_f(d.get('max'))} | {_f(d.get('loss_pct'))}% |"
                )
        add("")
        add(
            f"idle -> loaded p95 increase: **{_f(bb.get('increase_p95_ms'))} ms** "
            f"(p99: {_f(bb.get('increase_p99_ms'))} ms)"
        )
        add("")
        for n in bb.get("notes", []):
            add(f"> {n}")
        add("")
    if report.measured_comparisons:
        add("## Before / after measurements")
        add("")
        for c in report.measured_comparisons:
            add(f"### `{c.get('tweak_id')}` — {c.get('metric')}")
            add("")
            add(f"**{c.get('recommendation')}**")
            add("")
            add("| | n | p50 | p95 | p99 | max | loss |")
            add("|---|---|---|---|---|---|---|")
            for arm in ("baseline", "after"):
                d = c.get(arm) or {}
                add(
                    f"| {arm} | {d.get('n')} | {_f(d.get('p50'))} | "
                    f"{_f(d.get('p95'))} | {_f(d.get('p99'))} | "
                    f"{_f(d.get('max'))} | {_f(d.get('loss_pct'))}% |"
                )
            add("")
    add("## Changes recommended")
    add("")
    if not report.actionable:
        add(
            "**None.** Every applicable check came back clean, not applicable, or unknown. "
            "Most machines have no software-fixable latency problem."
        )
    else:
        for o in report.actionable:
            t = o.tweak
            add(f"### `{t.id}` — {t.title}")
            add("")
            add(
                f"- component: `{t.component.value}` · "
                f"confidence: **{t.confidence.value}** · "
                f"risk: {t.risk.value} · "
                f"reboot: {'yes' if t.requires_reboot else 'no'}"
            )
            add(f"- finding: {o.summary}")
            add(f"- expected effect: {t.expected_effect}")
            add(f"- mechanism: {t.mechanism}")
            add(f"- automated steps: {o.step_count}")
            add(
                f"- verify with: `{t.measure_metric}` over {t.measure_samples} samples"
                + (", **under load**" if t.needs_load_measurement else "")
                + f"; meaningful if >= {t.measure_min_effect_ms} ms"
            )
            if t.caveats:
                add(f"- caveat: {t.caveats}")
            if t.evidence_urls:
                add("- evidence: " + ", ".join(f"<{u}>" for u in t.evidence_urls))
            add("")
    if report.clean:
        add("## Checked and fine — no change needed")
        add("")
        for o in report.clean:
            add(f"- `{o.tweak.id}`: {o.summary}")
        add("")
    if report.unknowns:
        add("## Could not be determined")
        add("")
        add("Reported explicitly instead of substituted with a default.")
        add("")
        for o in report.unknowns:
            add(f"- `{o.tweak.id}`: {o.summary}")
        add("")
    na = [o for o in report.not_applicable if not o.tweak.is_refused]
    if na:
        add("## Not applicable to this machine")
        add("")
        for o in na:
            add(f"- `{o.tweak.id}`: {o.summary}")
        add("")
    if report.policy_denied:
        add("## Withheld by safety policy")
        add("")
        for o in report.policy_denied:
            add(f"- `{o.tweak.id}`: {o.policy.reason if o.policy else o.skipped_reason}")
        add("")
    if report.refused:
        add(f"## Refused by {brand}, and why")
        add("")
        add("Widely recommended changes that "
            f"{brand} will not generate.")
        add("")
        for o in report.refused:
            t = o.tweak
            add(f"### `{t.id}` — {t.title}")
            add("")
            add(f"{t.refusal_reason}")
            add("")
            if t.evidence_urls:
                add("References: " + ", ".join(f"<{u}>" for u in t.evidence_urls))
                add("")
    if report.maintenance:
        add("## Maintenance operations")
        add("")
        add(
            "Not latency tweaks; reported separately so they are never confused "
            "with one."
        )
        add("")
        for m in report.maintenance:
            add(f"### {m.get('title', m.get('id', '?'))}")
            add("")
            if m.get("summary"):
                add(f"{m['summary']}")
                add("")
            items = m.get("items") or []
            if items:
                add("| Operation | Reversible | Safeguard |")
                add("|---|---|---|")
                for it in items:
                    add(
                        f"| {it.get('description', '')} | "
                        f"{'yes' if it.get('reversible', True) else '**no**'} | "
                        f"{it.get('safety', '') or '—'} |"
                    )
                add("")
            for n in m.get("notes") or []:
                add(f"> {n}")
            if m.get("notes"):
                add("")
    warns = list(getattr(inv, "warnings", []))
    if warns:
        add("## Collection warnings")
        add("")
        for w in warns:
            add(f"- {w}")
        add("")
    add("---")
    add("")
    add(
        f"_{brand} measures, explains, and generates scripts for review. "
        f"Negative results stay visible._"
    )
    return "\n".join(parts)


def _f(v: Any, nd: int = 2) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f:
        return "—"
    return f"{f:.{nd}f}"


_CSS = """
:root { color-scheme: light dark; }
body { font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
       max-width: 980px; margin: 0 auto; padding: 28px 22px 60px; line-height: 1.55;
       color: #1b1f24; background: #fff; }
@media (prefers-color-scheme: dark) { body { color:#e6e6e6; background:#14171a; }
  table, .card { border-color:#2c3238 !important; } th { background:#1d2226 !important; } }
h1 { font-size: 1.7rem; margin: 0 0 4px; }
h2 { font-size: 1.25rem; margin: 34px 0 10px; padding-bottom: 6px; border-bottom: 1px solid #d7dce1; }
h3 { font-size: 1.02rem; margin: 20px 0 6px; font-family: ui-monospace, Menlo, monospace; }
table { border-collapse: collapse; width: 100%; margin: 10px 0 16px; font-size: .92rem; }
th, td { border: 1px solid #d7dce1; padding: 6px 9px; text-align: left; vertical-align: top; }
th { background: #f2f4f7; font-weight: 600; }
code { font-family: ui-monospace, Menlo, monospace; font-size: .88em;
       background: rgba(127,127,127,.13); padding: 1px 5px; border-radius: 4px; }
.card { border: 1px solid #d7dce1; border-left-width: 4px; border-radius: 6px;
        padding: 12px 15px; margin: 12px 0; }
.problem { border-left-color: #d1242f; }
.ok { border-left-color: #1a7f37; }
.na { border-left-color: #9aa0a6; }
.unk { border-left-color: #bf8700; }
.refused { border-left-color: #6f42c1; }
.badge { display: inline-block; font-size: .72rem; font-weight: 700; letter-spacing: .04em;
         padding: 2px 7px; border-radius: 999px; text-transform: uppercase; margin-right: 6px; }
.b-problem { background:#ffebe9; color:#a40e26; }
.b-ok { background:#dafbe1; color:#116329; }
.b-na { background:#eaeef2; color:#57606a; }
.b-unk { background:#fff8c5; color:#7d4e00; }
.b-refused { background:#f3e8ff; color:#5a2a9c; }
.meta { color:#6b7280; font-size:.86rem; margin:2px 0; }
.note { background: rgba(127,127,127,.09); border-radius: 6px; padding: 10px 13px;
        font-size: .92rem; margin: 10px 0; }
.warn { color: #9a6700; font-size: .88rem; }
ul { margin: 6px 0 12px; padding-left: 22px; }
.small { font-size:.86rem; }
"""

_BADGE_CLASS = {
    Verdict.PROBLEM: "b-problem",
    Verdict.NO_PROBLEM: "b-ok",
    Verdict.NOT_APPLICABLE: "b-na",
    Verdict.UNKNOWN: "b-unk",
}
_CARD_CLASS = {
    Verdict.PROBLEM: "problem",
    Verdict.NO_PROBLEM: "ok",
    Verdict.NOT_APPLICABLE: "na",
    Verdict.UNKNOWN: "unk",
}


def render_html(report: Report, brand: str = "latency") -> str:
    inv = report.inventory
    e = html.escape
    out: list[str] = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{e(brand)} report</title>",
        f"<style>{_CSS}</style></head><body>",
        f"<h1>{e(brand)} report</h1>",
        f"<p class='meta'>Generated {e(str(report.generated_at))} &middot; "
        f"{e(str(getattr(inv, 'os_name', '?')))} "
        f"(build {e(str(getattr(inv, 'windows_build', '?')))})</p>",
        f"<p class='meta'><b>{len(report.actionable)}</b> change(s) recommended &middot; "
        f"<b>{len(report.clean)}</b> checked and fine &middot; "
        f"<b>{len(report.not_applicable)}</b> not applicable &middot; "
        f"<b>{len(report.unknowns)}</b> could not be determined</p>",
        "<h2>Latency budget</h2>",
    ]
    out.append("<table><tr><th>Component</th><th>Finding</th></tr>")
    for comp in _COMPONENT_ORDER:
        rows = [
            o
            for o in report.outcomes
            if o.tweak.component is comp and not o.tweak.is_refused
        ]
        problems = [o for o in rows if o.verdict is Verdict.PROBLEM]
        unknown = [o for o in rows if o.verdict is Verdict.UNKNOWN]
        if problems:
            cell = "<b>" + ", ".join(e(o.tweak.id) for o in problems) + "</b>"
        elif unknown:
            cell = f"not determined ({len(unknown)} check(s) returned UNKNOWN)"
        elif rows:
            cell = "no issue found"
        else:
            cell = "no checks apply to this machine"
        out.append(
            f"<tr><td><code>{e(comp.value)}</code>"
            f"<div class='meta small'>{e(comp.floor_note)}</div></td>"
            f"<td>{cell}</td></tr>"
        )
    out.append("</table>")
    if report.render_budget:
        out.append(
            "<h3>Render/input budget estimate</h3>"
            "<table><tr><th>Term</th><th>Estimate</th></tr>"
        )
        for k, v in report.render_budget.items():
            out.append(f"<tr><td>{e(str(k))}</td><td>{e(str(v))}</td></tr>")
        out.append("</table>")
    net = report.network
    if net:
        out.append("<h2>Network measurement</h2><ul>")
        for key in ("idle", "gateway"):
            d = net.get(key)
            if d:
                out.append(
                    f"<li><b>{e(key)}</b>: n={e(str(d.get('n')))} "
                    f"p50={_f(d.get('p50'))} p95={_f(d.get('p95'))} "
                    f"p99={_f(d.get('p99'))} max={_f(d.get('max'))} ms, "
                    f"loss={_f(d.get('loss_pct'))}%, "
                    f"jitter={_f(d.get('jitter_mad'))} ms</li>")
        if net.get("transport_note"):
            out.append(f"<li>{e(str(net['transport_note']))}</li>")
        out.append("</ul>")
    rt = report.route
    if rt:
        out.append("<h2>Route and physical floor</h2><ul>")
        out.append(
            f"<li>target: <code>{e(str(rt.get('target')))}</code>"
            f" &rarr; <code>{e(str(rt.get('resolved_ip')))}</code></li>")
        out.append(
            f"<li>responding hops: {e(str(rt.get('responding_hop_count')))}</li>")
        out.append(f"<li>measured RTT: {_f(rt.get('final_rtt_ms'))} ms</li>")
        if rt.get("floor_ms") is not None:
            out.append(
                f"<li>propagation floor: ~{_f(rt.get('floor_ms'))} ms "
                f"(<b>irreducible</b>)</li>")
            out.append(
                f"<li>addressable headroom: "
                f"~{_f(rt.get('addressable_headroom_ms'))} ms</li>")
        else:
            out.append(
                "<li>propagation floor: <b>not determined</b>; a traceroute "
                "alone cannot set it</li>")
        out.append("</ul>")
    bb = report.bufferbloat
    if bb:
        out.append("<h2>Bufferbloat (latency under load)</h2>")
        out.append(
            f"<p><b>Grade: {e(str(bb.get('grade', '?')).upper())}</b>"
            f" &mdash; {e(str(bb.get('explanation', '')))}</p>")
        out.append(
            "<table><tr><th>Phase</th><th>n</th><th>p50</th><th>p95</th>"
            "<th>p99</th><th>max</th><th>loss</th></tr>")
        for phase in ("idle", "download", "upload"):
            d = bb.get(phase)
            if not d:
                out.append(
                    f"<tr><td>{phase}</td><td colspan='5'>NOT TESTED</td>"
                    "<td>&mdash;</td></tr>")
            else:
                out.append(
                    f"<tr><td>{phase}</td><td>{e(str(d.get('n')))}</td>"
                    f"<td>{_f(d.get('p50'))}</td><td>{_f(d.get('p95'))}</td>"
                    f"<td>{_f(d.get('p99'))}</td><td>{_f(d.get('max'))}</td>"
                    f"<td>{_f(d.get('loss_pct'))}%</td></tr>")
        out.append("</table>")
        out.append(
            f"<p>idle &rarr; loaded p95 increase: "
            f"<b>{_f(bb.get('increase_p95_ms'))} ms</b> "
            f"(p99: {_f(bb.get('increase_p99_ms'))} ms)</p>")
        for n in bb.get("notes", []):
            out.append(f"<div class='note'>{e(str(n))}</div>")
    if report.measured_comparisons:
        out.append("<h2>Before / after measurements</h2>")
        for c in report.measured_comparisons:
            out.append(
                f"<h3><code>{e(str(c.get('tweak_id')))}</code>"
                f" &mdash; {e(str(c.get('metric')))}</h3>")
            out.append(f"<p><b>{e(str(c.get('recommendation')))}</b></p>")
            out.append(
                "<table><tr><th></th><th>n</th><th>p50</th><th>p95</th>"
                "<th>p99</th><th>max</th><th>loss</th></tr>")
            for arm in ("baseline", "after"):
                d = c.get(arm) or {}
                out.append(
                    f"<tr><td>{arm}</td><td>{e(str(d.get('n')))}</td>"
                    f"<td>{_f(d.get('p50'))}</td><td>{_f(d.get('p95'))}</td>"
                    f"<td>{_f(d.get('p99'))}</td><td>{_f(d.get('max'))}</td>"
                    f"<td>{_f(d.get('loss_pct'))}%</td></tr>")
            out.append("</table>")
    out.append("<h2>Changes recommended</h2>")
    if not report.actionable:
        out.append(
            "<div class='note'><b>None.</b> Every applicable check came back clean, not "
            "applicable, or unknown. Most machines have no software-fixable problem.</div>"
        )
    for o in report.actionable:
        out.append(_card(o, e))
    if report.clean:
        out.append("<h2>Checked and fine &mdash; no change needed</h2>")
        for o in report.clean:
            out.append(_card(o, e))
    if report.unknowns:
        out.append("<h2>Could not be determined</h2>")
        for o in report.unknowns:
            out.append(_card(o, e))
    na = [o for o in report.not_applicable if not o.tweak.is_refused]
    if na:
        out.append("<h2>Not applicable to this machine</h2>")
        for o in na:
            out.append(_card(o, e))
    if report.policy_denied:
        out.append("<h2>Withheld by safety policy</h2>")
        for o in report.policy_denied:
            out.append(_card(o, e))
    if report.refused:
        out.append(f"<h2>Refused by {e(brand)}, and why</h2>")
        for o in report.refused:
            out.append(_card(o, e))
    if report.maintenance:
        out.append("<h2>Maintenance operations</h2>")
        out.append(
            "<p class='meta'>Not latency tweaks; reported separately so "
            "they are never confused with one.</p>")
        for m in report.maintenance:
            out.append(f"<h3>{e(str(m.get('title', m.get('id', '?'))))}</h3>")
            if m.get("summary"):
                out.append(f"<p>{e(str(m['summary']))}</p>")
            items = m.get("items") or []
            if items:
                out.append(
                    "<table><tr><th>Operation</th><th>Reversible</th>"
                    "<th>Safeguard</th></tr>")
                for it in items:
                    rev = "yes" if it.get("reversible", True) else "<b>no</b>"
                    out.append(
                        f"<tr><td>{e(str(it.get('description', '')))}</td>"
                        f"<td>{rev}</td>"
                        f"<td>{e(str(it.get('safety', '') or chr(8212)))}</td>"
                        "</tr>")
                out.append("</table>")
            for n in m.get("notes") or []:
                out.append(f"<div class='note'>{e(str(n))}</div>")
    warns = list(getattr(inv, "warnings", []))
    if warns:
        out.append("<h2>Collection warnings</h2><ul>")
        for w in warns:
            out.append(f"<li class='warn small'>{e(str(w))}</li>")
        out.append("</ul>")
    out.append(
        "<p class='meta' style='margin-top:34px;border-top:1px solid #d7dce1;"
        f"padding-top:12px'>{e(brand)} measures, explains, and generates scripts "
        f"for review.</p>"
    )
    out.append("</body></html>")
    return "\n".join(out)


def _card(o: TweakOutcome, e) -> str:
    t = o.tweak
    v = o.verdict
    badge = _BADGE_CLASS.get(v, "b-na")
    card = _CARD_CLASS.get(v, "na")
    if t.is_refused:
        badge, card = "b-refused", "refused"
    label = "REFUSED" if t.is_refused else _VERDICT_ICON.get(v, str(v.value))
    bits = [
        f"<div class='card {card}'>",
        f"<span class='badge {badge}'>{e(label)}</span>",
        f"<b><code>{e(t.id)}</code></b> &mdash; {e(t.title)}",
        f"<div class='meta'>component <code>{e(t.component.value)}</code> &middot; "
        f"confidence <b>{e(t.confidence.value)}</b> &middot; risk {e(t.risk.value)}"
        f"{' &middot; reboot required' if t.requires_reboot else ''}</div>",
        f"<p>{e(o.summary)}</p>",
    ]
    if t.refusal_reason:
        bits.append(f"<p><b>Why refused:</b> {e(t.refusal_reason)}</p>")
    if o.policy is not None and not o.policy.allowed and not t.is_refused:
        bits.append(f"<p class='warn'><b>Withheld:</b> {e(o.policy.reason)}</p>")
    if t.expected_effect and not t.is_refused:
        bits.append(
            f"<p class='small'><b>Expected effect:</b> {e(t.expected_effect)}</p>"
        )
    if t.caveats:
        bits.append(f"<p class='small'><b>Caveat:</b> {e(t.caveats)}</p>")
    if o.measured:
        try:
            bits.append(
                f"<p class='small'><b>Measured:</b> "
                f"<code>{e(json.dumps(o.measured, default=str)[:600])}</code></p>"
            )
        except (TypeError, ValueError):
            pass
    if t.evidence_urls:
        bits.append(
            "<p class='small'>Evidence: "
            + " ".join(f"<a href='{e(u)}'>{e(u)}</a>" for u in t.evidence_urls)
            + "</p>"
        )
    bits.append("</div>")
    return "\n".join(bits)




@dataclass
class PlanResult:
    inventory: Inventory
    outcomes: list[TweakOutcome] = field(default_factory=list)
    planned: list[PlannedChange] = field(default_factory=list)
    policy_notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def actionable_ids(self) -> list[str]:
        return [pc.tweak.id for pc in self.planned]

    @property
    def automated_step_count(self) -> int:
        return sum(pc.automated_step_count for pc in self.planned)

    @property
    def manual_step_count(self) -> int:
        return sum(1 for pc in self.planned for s in pc.steps if s.is_manual)

    @property
    def requires_reboot(self) -> bool:
        return any(pc.requires_reboot for pc in self.planned)

    def summary_lines(self) -> list[str]:
        counts: dict[str, int] = {}
        for o in self.outcomes:
            key = o.verdict.value
            counts[key] = counts.get(key, 0) + 1
        lines = [
            f"evaluated {len(self.outcomes)} checks on this machine",
            "  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())),
            f"  planned changes: {len(self.planned)} "
            f"({self.automated_step_count} automated, {self.manual_step_count} manual)",
            f"  warnings: {len(self.inventory.warnings)}",
        ]
        if self.requires_reboot:
            lines.append("  a reboot is required for at least one change")
        if self.errors:
            lines.append(f"  errors: {len(self.errors)}")
        return lines


def evaluate_all(
    inv: Inventory,
    policy: SafetyPolicy | None = None,
    tweaks: Sequence[Tweak] | None = None,
    *,
    include_refused: bool = True,
    force_ids: Iterable[str] = (),
) -> tuple[list[TweakOutcome], list[str]]:
    policy = policy or SafetyPolicy()
    detected = list(inv.running_anti_cheat) or AntiCheatPolicy().detect(inv)
    notes: list[str] = []
    if detected:
        notes.append(
            f"anti-cheat runtime detected ({', '.join(detected[:4])}). lattool dropped to "
            f"diagnostic-only posture for process and interrupt tweaks."
        )
    candidates: list[Tweak] = (
        list(tweaks)
        if tweaks is not None
        else list(applicable_tweaks(include_experimental=policy.allow_experimental))
    )
    if include_refused and tweaks is None:
        candidates += list(refused_tweaks())
    forced = set(force_ids)
    outcomes: list[TweakOutcome] = []
    for t in candidates:
        finding = t.evaluate(inv)
        decision = policy.evaluate(t, inv, tuple(detected))
        outcome = TweakOutcome(
            tweak=t,
            verdict=finding.verdict,
            summary=finding.summary,
            policy=decision,
            measured=dict(finding.measured),
            requested_explicitly=t.id in forced,
        )
        if not decision.allowed:
            outcome.skipped_reason = decision.reason
        outcomes.append(outcome)
    return outcomes, notes


def build_plan(
    inv: Inventory,
    policy: SafetyPolicy | None = None,
    tweaks: Sequence[Tweak] | None = None,
    *,
    include_refused: bool = True,
    force_ids: Iterable[str] = (),
    dry_run: bool = False,
) -> PlanResult:
    policy = policy or SafetyPolicy()
    forced = set(force_ids)
    outcomes, notes = evaluate_all(
        inv, policy, tweaks, include_refused=include_refused, force_ids=forced
    )
    if any("no-internet" in w.lower() for w in inv.warnings):
        notes.append("no-internet signal: install operations are unavailable offline")
    result = PlanResult(inventory=inv, outcomes=outcomes, policy_notes=notes)
    would: list[str] = []
    for o in outcomes:
        if o.tweak.is_refused:
            continue
        if o.verdict is not Verdict.PROBLEM and not o.requested_explicitly:
            continue
        if o.policy is not None and not o.policy.allowed:
            continue
        would.append(o.tweak.id)
    by_id = {o.tweak.id: o.tweak for o in outcomes}
    kept: list[str] = []
    for tid in would:
        t = by_id.get(tid)
        clash = [
            k for k in kept
            if t is not None and (
                k in (t.conflicts_with or ())
                or tid in ((by_id[k].conflicts_with or ())
                           if k in by_id else ())
            )
        ]
        if clash:
            notes.append(
                f"conflict: {tid} dropped - it declares or receives a "
                f"conflict with the already-planned {', '.join(clash)}"
            )
            for o in outcomes:
                if o.tweak.id == tid:
                    o.skipped_reason = f"conflicts with {', '.join(clash)}"
            continue
        kept.append(tid)
    would = kept
    if dry_run:
        if would:
            notes.append("dry run: would plan: " + ", ".join(would))
        else:
            notes.append("dry run: nothing would be planned")
        return result
    planned_ids = set(would)
    for o in outcomes:
        if o.tweak.id not in planned_ids:
            continue
        try:
            steps = o.tweak.build_plan(inv)
        except RefusedTweakError as exc:
            result.errors.append(f"fatal: {o.tweak.id}: refused at plan time: {exc}")
            continue
        except TweakValidationError as exc:
            result.errors.append(f"cosmetic: {o.tweak.id}: invalid plan: {exc}")
            continue
        except Exception as exc:
            result.errors.append(
                f"fatal: {o.tweak.id}: plan() raised {type(exc).__name__}: {exc}"
            )
            continue
        scan = policy.check_plan(o.tweak, steps)
        if not scan.allowed:
            result.errors.append(
                f"fatal: {o.tweak.id}: blocked by policy after planning: {scan.reason}"
            )
            o.skipped_reason = scan.reason
            o.step_count = 0
            o.step_count_total = 0
            continue
        o.step_count = sum(1 for s in steps if not s.is_manual)
        o.step_count_total = len(steps)
        result.planned.append(
            PlannedChange(
                tweak=o.tweak,
                steps=steps,
                finding_summary=o.summary,
                policy_notes=[n for n in notes if o.tweak.id in n] or [],
            )
        )
    return result


def render(
    result: PlanResult,
    *,
    generated_at: str | None = None,
    brand: str = "lattool",
) -> ScriptBundle:
    ts = generated_at or _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    nic = result.inventory.primary_nic
    return render_bundle(
        result.planned,
        generated_at=ts,
        brand=brand,
        inventory_digest={
            "os": result.inventory.os_name,
            "build": result.inventory.windows_build,
            "cpu": result.inventory.cpu.name,
            "primary_nic": nic.interface_description if nic else None,
            "warnings": list(result.inventory.warnings),
        },
        warnings=len(result.inventory.warnings),
    )


def save_baseline(path: str | Path, payload: dict[str, Any]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    return p


def load_baseline(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"no baseline at {p}; save a measurement first, otherwise any claimed "
            f"improvement would be unfounded"
        )
    return json.loads(p.read_text(encoding="utf-8"))


def build_report(
    result: PlanResult,
    *,
    network: dict[str, Any] | None = None,
    bufferbloat: dict[str, Any] | None = None,
    route: dict[str, Any] | None = None,
    render_budget: dict[str, Any] | None = None,
    comparisons: list[dict[str, Any]] | None = None,
    maintenance: list[dict[str, Any]] | None = None,
    generated_at: str | None = None,
) -> Report:
    return Report(
        inventory=result.inventory,
        outcomes=result.outcomes,
        network=network or {},
        bufferbloat=bufferbloat,
        route=route,
        render_budget=render_budget or {},
        generated_at=generated_at
        or _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        measured_comparisons=comparisons or [],
        maintenance=maintenance or [],
    )
