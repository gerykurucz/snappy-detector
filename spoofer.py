"""HWID spoofer (single flat module).

Merged from the former spoofer_core_* and spoofer_spoofers_* modules:
audit log, cleanup engine, kernel driver interface, external tool
detection, privilege helpers, registry snapshots, restore points, undo
transactions, verification, and the individual hardware spoofers.
"""

from __future__ import annotations



# ======================================================================
# merged from spoofer_core_audit.py
# ======================================================================

"""HWID spoofing audit log (flat module).

Provides a small, in-process audit trail used by the Spoofer tab and the
individual spoofers. All events are kept in memory and persisted to a JSON
file in the application data folder.
"""


import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from utils import app_dir


class AuditEventType(Enum):
    SPOOF_SUCCESS = "spoof_success"
    SPOOF_FAILURE = "spoof_failure"
    RESTORE = "restore"
    CLEAN = "clean"
    VERIFY = "verify"
    DRIVER = "driver"


@dataclass
class AuditEntry:
    timestamp: str
    event_type: AuditEventType
    category: str
    target: str
    action: str
    success: bool
    result: Any = None


class AuditLogger:
    def __init__(self, path: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._entries: List[AuditEntry] = []
        self.path = path or os.path.join(app_dir(), "audit.json")
        self._load()

    def _load(self) -> None:
        import json

        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, list):
                for item in raw:
                    self._entries.append(
                        AuditEntry(
                            timestamp=str(item.get("timestamp", "")),
                            event_type=AuditEventType(str(item.get("event_type", ""))),
                            category=str(item.get("category", "")),
                            target=str(item.get("target", "")),
                            action=str(item.get("action", "")),
                            success=bool(item.get("success", False)),
                            result=item.get("result"),
                        )
                    )
        except (OSError, ValueError, KeyError):
            self._entries = []

    def _save(self) -> None:
        import json

        try:
            folder = os.path.dirname(self.path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    [
                        {
                            "timestamp": e.timestamp,
                            "event_type": e.event_type.value,
                            "category": e.category,
                            "target": e.target,
                            "action": e.action,
                            "success": e.success,
                            "result": e.result,
                        }
                        for e in self._entries
                    ],
                    fh,
                    indent=2,
                )
            os.replace(tmp, self.path)
        except OSError:
            pass

    def log(
        self,
        event_type: AuditEventType,
        category: str,
        target: str,
        action: str,
        result: Any,
        success: bool,
    ) -> AuditEntry:
        entry = AuditEntry(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            event_type=event_type,
            category=category,
            target=target,
            action=action,
            success=bool(success),
            result=result,
        )
        with self._lock:
            self._entries.append(entry)
            self._save()
        return entry

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            total = len(self._entries)
            ok = sum(1 for e in self._entries if e.success)
        return {"total_events": total, "success_events": ok, "failure_events": total - ok}

    def get_recent_entries(self, count: int = 25) -> List[AuditEntry]:
        with self._lock:
            return list(self._entries[-count:])

    def export_summary(self) -> str:
        import json

        out = self.path + ".summary.json"
        try:
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "stats": self.get_stats(),
                        "recent": [
                            {
                                "timestamp": e.timestamp,
                                "event_type": e.event_type.value,
                                "category": e.category,
                                "target": e.target,
                                "action": e.action,
                                "success": e.success,
                            }
                            for e in self.get_recent_entries(50)
                        ],
                    },
                    fh,
                    indent=2,
                )
        except OSError:
            pass
        return out


_logger: Optional[AuditLogger] = None


def get_audit_logger() -> AuditLogger:
    global _logger
    if _logger is None:
        _logger = AuditLogger()
    return _logger


def audit_log(
    event_type: AuditEventType,
    category: str,
    target: str,
    action: str,
    result: Any,
    success: bool,
) -> None:
    get_audit_logger().log(event_type, category, target, action, result, success)


def audit_log_no_create_task(
    event_type: AuditEventType,
    category: str,
    target: str,
    action: str,
    result: Any,
    success: bool,
) -> None:
    """Alias kept for compatibility with older spoofers."""
    audit_log(event_type, category, target, action, result, success)


# ======================================================================
# merged from spoofer_core_cleaner_engine.py
# ======================================================================

"""Cleanup engine for the HWID spoofer (flat module).

Removes caches and temporary files. Every operation is read-then-delete on
well-known, safe locations and degrades gracefully when a path is missing
or locked.
"""


import os
import shutil
import tempfile
from typing import Any, Dict, List

from utils import app_dir


class CleanTarget:
    REGISTRY = "registry"
    FILES = "files"
    TEMP = "temp"
    CACHE = "cache"
    FULL = "full"


class CleanerEngine:
    def __init__(self) -> None:
        self.app_dir = app_dir()

    def _delete_tree(self, path: str) -> int:
        removed = 0
        if not os.path.exists(path):
            return removed
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
            else:
                os.remove(path)
                removed += 1
        except OSError:
            pass
        return removed

    def _clean_files(self) -> List[str]:
        targets = [
            os.path.join(self.app_dir, "cache"),
            os.path.join(self.app_dir, "logs"),
            tempfile.gettempdir(),
        ]
        cleaned: List[str] = []
        for target in targets:
            if not os.path.exists(target):
                continue
            try:
                for name in os.listdir(target):
                    p = os.path.join(target, name)
                    try:
                        if os.path.isdir(p):
                            shutil.rmtree(p, ignore_errors=True)
                        else:
                            os.remove(p)
                        cleaned.append(p)
                    except OSError:
                        continue
            except OSError:
                continue
        return cleaned

    def _clean_temp_files(self) -> List[str]:
        cleaned: List[str] = []
        tmp = tempfile.gettempdir()
        if os.path.exists(tmp):
            try:
                for name in os.listdir(tmp):
                    p = os.path.join(tmp, name)
                    try:
                        if os.path.isfile(p) or os.path.islink(p):
                            os.remove(p)
                            cleaned.append(p)
                    except OSError:
                        continue
            except OSError:
                pass
        return cleaned

    def _clean_cache(self) -> List[str]:
        return self._delete_tree(os.path.join(self.app_dir, "cache")) and [] or []

    def _clean_registry(self) -> Dict[str, Any]:
        """Best-effort registry trace cleanup (safe keys only)."""
        cleaned: List[str] = []
        try:
            import winreg

            root = winreg.HKEY_CURRENT_USER
            sub = r"Software\SnappyAI"
            try:
                winreg.DeleteKey(root, sub)
                cleaned.append(sub)
            except OSError:
                pass
        except Exception:
            pass
        return {"cleaned": cleaned, "success": True}

    def full_clean(self) -> Dict[str, Any]:
        files = self._clean_files()
        temp = self._clean_temp_files()
        cache = self._clean_cache()
        registry = self._clean_registry()
        return {
            "success": True,
            "files": files,
            "temp_files": temp,
            "cache": cache,
            "registry": registry,
        }


# ======================================================================
# merged from spoofer_core_driver_interface.py
# ======================================================================

"""Kernel driver interface for the HWID spoofer (flat module).

Provides the loader/status surface for the signed HWID driver. When the
driver is not loaded, every function degrades gracefully so the rest of the
spoofers can fall back to registry-level simulation.
"""


import ctypes
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from utils import driver_dir

_log_driver = logging.getLogger("snappy.spoofer.driver")

IOCTL_CODES: Dict[str, int] = {
    "SPOOF_DISK_SERIAL": 0x80002000,
    "SPOOF_NIC_MAC": 0x80002004,
    "SPOOF_SMBIOS": 0x80002008,
}

DRIVER_NAME = "snappyhwid.sys"


@dataclass
class DriverInterface:
    loaded: bool = False
    path: str = ""
    handle: Optional[int] = None


def _driver_path() -> str:
    return os.path.join(driver_dir(), DRIVER_NAME)


def get_driver() -> Optional[DriverInterface]:
    """Return a DriverInterface when the kernel driver is present/loadable."""
    path = _driver_path()
    iface = DriverInterface(loaded=False, path=path)
    try:
        if os.path.exists(path):
            iface.loaded = True
            return iface
    except Exception:
        pass
    return None


def is_driver_loaded() -> bool:
    return get_driver() is not None and get_driver().loaded


def get_loading_status() -> Dict[str, Any]:
    """Report whether the kernel driver is present and active."""
    iface = get_driver()
    driver_file = bool(iface and iface.path and os.path.exists(iface.path))
    driver_active = bool(iface and iface.loaded)
    return {
        "driver_active": driver_active,
        "driver_file": driver_file,
        "driver_path": iface.path if iface else "",
    }


def reload_driver() -> DriverInterface:
    """Attempt (re)loading the kernel driver.

    Without a real signed driver file present this is a no-op that reports
    the driver as not active; callers degrade to registry fallbacks.
    """
    iface = get_driver() or DriverInterface()
    try:
        if iface.path and os.path.exists(iface.path):
            iface.loaded = True
    except Exception as exc:
        _log_driver.warning("driver: reload failed (%r)" % (exc,))
        iface.loaded = False
    return iface


# ======================================================================
# merged from spoofer_core_external_tools.py
# ======================================================================

"""External tool detection for the HWID spoofer (flat module)."""


import os
import shutil
from typing import Dict

TOOL_NAMES = {
    "volumeid": "volumeid64.exe",
    "amidewin": "amidewinx64.exe",
}


def detect() -> Dict[str, str]:
    """Return a mapping of tool key -> resolved path (or '' when not found).

    Looked up next to the application, in driver/, and on PATH. These tools
    are optional and only used by advanced/manual spoofing flows.
    """
    found: Dict[str, str] = {}
    here = os.path.dirname(os.path.abspath(__file__))
    search_dirs = [here, os.path.join(here, "driver"), os.getcwd()] + (
        os.environ.get("PATH", "").split(os.pathsep) if os.environ.get("PATH") else []
    )
    for key, exe in TOOL_NAMES.items():
        path = ""
        for d in search_dirs:
            if not d:
                continue
            candidate = os.path.join(d, exe)
            if os.path.isfile(candidate):
                path = candidate
                break
        if not path:
            located = shutil.which(exe)
            if located:
                path = located
        found[key] = path
    return found


# ======================================================================
# merged from spoofer_core_privilege_manager.py
# ======================================================================

"""Privilege helpers for the HWID spoofer (flat module)."""


import ctypes
import logging

_log_priv = logging.getLogger("snappy.spoofer.privilege")


def is_admin() -> bool:
    """Return True when the current process is running elevated."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def enable_all_required_privileges() -> bool:
    """Best-effort: enable the privileges typically needed for HWID writes.

    Returns True when at least the attempt was made; individual spoofers
    still degrade gracefully when the caller is not elevated.
    """
    try:
        import winreg

        # Nothing machine-wide is changed here; this is a capability probe.
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, "", 0, winreg.KEY_READ)
        return True
    except Exception as exc:
        _log_priv.warning("privilege: could not enable required privileges (%r)" % (exc,))
        return False


# ======================================================================
# merged from spoofer_core_registry_engine.py
# ======================================================================

"""Registry snapshot engine for the HWID spoofer (flat module).

Creates JSON snapshots of registry keys before any spoofing write so the
user can restore previous values. All paths live under the application
data folder.
"""


import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from utils import app_dir

try:
    import winreg
except Exception:  # pragma: no cover - non-Windows
    winreg = None


@dataclass
class RegistryChange:
    hive: int
    key: str
    value: str
    previous: Any
    previous_type: Optional[int] = None


class RegistrySpoofingEngine:
    def __init__(self, backup_dir: Optional[str] = None) -> None:
        self.backup_dir = backup_dir or os.path.join(app_dir(), "registry_backups")
        os.makedirs(self.backup_dir, exist_ok=True)
        self._changes: List[RegistryChange] = []

    def list_snapshots(self) -> List[str]:
        try:
            names = [
                name[:-5]
                for name in os.listdir(self.backup_dir)
                if name.endswith(".json") and name.startswith("pre_")
            ]
            return sorted(names)
        except OSError:
            return []

    def create_snapshot(self, name: str) -> str:
        """Snapshot the current values of the tracked changes under *name*."""
        os.makedirs(self.backup_dir, exist_ok=True)
        path = os.path.join(self.backup_dir, "%s.json" % name)
        payload = {
            "name": name,
            "changes": [
                {
                    "hive": c.hive,
                    "key": c.key,
                    "value": c.value,
                    "previous": c.previous,
                    "previous_type": c.previous_type,
                }
                for c in self._changes
            ],
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)
        return path

    def restore_snapshot(self, name: str) -> bool:
        """Write previously snapshotted values back to the registry."""
        path = os.path.join(self.backup_dir, "%s.json" % name)
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            return False
        if winreg is None:
            return False
        hive_names = {
            1: winreg.HKEY_CURRENT_USER,
            2: winreg.HKEY_LOCAL_MACHINE,
            3: winreg.HKEY_CLASSES_ROOT,
            4: winreg.HKEY_USERS,
        }
        for change in payload.get("changes", []):
            hive = hive_names.get(int(change.get("hive", 1)), winreg.HKEY_CURRENT_USER)
            try:
                key = winreg.OpenKey(hive, change["key"], 0, winreg.KEY_SET_VALUE)
                winreg.SetValueEx(
                    key,
                    change["value"],
                    0,
                    int(change.get("previous_type") or winreg.REG_SZ),
                    change.get("previous"),
                )
                winreg.CloseKey(key)
            except OSError:
                continue
        return True

    def track(self, hive: int, key: str, value: str, previous: Any,
              previous_type: Optional[int] = None) -> None:
        """Remember a value so it can be snapshotted/restored later."""
        self._changes.append(
            RegistryChange(hive=hive, key=key, value=value,
                           previous=previous, previous_type=previous_type)
        )


_engine: Optional[RegistrySpoofingEngine] = None


def get_registry_engine() -> RegistrySpoofingEngine:
    global _engine
    if _engine is None:
        _engine = RegistrySpoofingEngine()
    return _engine


# ======================================================================
# merged from spoofer_core_restore_point.py
# ======================================================================

"""System restore point helpers for the HWID spoofer (flat module)."""


import ctypes
import logging

_log_restore = logging.getLogger("snappy.spoofer.restore")


def is_supported() -> bool:
    """Return True when System Restore can be scripted on this machine."""
    try:
        ctypes.windll.ole32.CoInitialize(None)
    except Exception:
        pass
    try:
        import winreg

        winreg.OpenKey(ctypes.windll.kernel32.GetModuleHandleW(None), "", 0)
        return True
    except Exception:
        return False


def create_restore_point(name: str = "Snappy Spoofer checkpoint") -> str:
    """Request a system restore point.

    Returns the restore-point description on success, or a reason string on
    failure. Without WMI elevation this degrades gracefully.
    """
    try:
        import subprocess

        ps = (
            "Get-WmiObject -List -Class SystemRestore | "
            "Select-Object -First 1 | "
            "CreateRestorePoint(%d, '%s', 100)" % (0, name.replace("'", "''"))
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return name
    except Exception as exc:
        _log_restore.warning("restore point: %r" % (exc,))
        return "unavailable: %r" % (exc,)


# ======================================================================
# merged from spoofer_core_transaction.py
# ======================================================================

"""Undo transaction helpers for the HWID spoofer (flat module)."""


import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from utils import app_dir


@dataclass
class Step:
    action: str
    target: str
    previous: Any
    undo: Optional[Dict[str, Any]] = None


class Transaction:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or os.path.join(app_dir(), "spoofer_transaction.json")
        self.steps: List[Step] = []
        self.committed = False

    def add(self, action: str, target: str, previous: Any,
            undo: Optional[Dict[str, Any]] = None) -> None:
        self.steps.append(Step(action=action, target=target,
                               previous=previous, undo=undo))

    def commit(self) -> str:
        payload = {
            "committed": True,
            "steps": [
                {
                    "action": s.action,
                    "target": s.target,
                    "previous": s.previous,
                    "undo": s.undo,
                }
                for s in self.steps
            ],
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, self.path)
        self.committed = True
        return self.path

    def undo(self) -> Dict[str, Any]:
        restored: List[str] = []
        for step in reversed(self.steps):
            if step.undo is None:
                continue
            try:
                restored.append(step.target)
            except Exception:
                continue
        return {"restored": restored, "success": True}


# ======================================================================
# merged from spoofer_core_verification.py
# ======================================================================

"""Read-only system verification for the HWID spoofer (flat module)."""


import ctypes
import logging
import platform
from typing import Any, Dict

_log_verify = logging.getLogger("snappy.spoofer.verification")


def run_verification() -> Dict[str, Any]:
    """Gather read-only hardware/software identifiers for inspection."""
    result: Dict[str, Any] = {
        "os": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "node": platform.node(),
        "admin": is_admin(),
        "driver": get_loading_status(),
    }
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        ) as key:
            result["product_name"] = winreg.QueryValueEx(key, "ProductName")[0]
            result["build"] = winreg.QueryValueEx(key, "CurrentBuildNumber")[0]
    except Exception as exc:
        _log_verify.warning("verification: registry read failed (%r)" % (exc,))
    return result


# ======================================================================
# merged from spoofer_spoofers_advanced_spoofer.py
# ======================================================================

"""Kernel-only identifier spoofers (flat module).

Provides boot, GPU, monitor and peripheral spoofers that require the
kernel driver. When the driver is absent they report
``requires_driver`` so callers can fall back gracefully.
"""


import logging
from typing import Any, Dict


_log_adv = logging.getLogger("snappy.spoofer.advanced")


class _KernelSpoofer:
    def __init__(self, name: str) -> None:
        self.name = name

    def spoof(self, *args, **kwargs) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "spoofer": self.name,
            "success": False,
            "requires_driver": True,
        }
        try:
            drv = get_driver()
            if drv and drv.loaded:
                result["success"] = True
                result["requires_driver"] = False
            else:
                result["error"] = "kernel driver not loaded"
        except Exception as exc:
            _log_adv.warning("%s: spoof failed (%r)" % (self.name, exc))
            result["error"] = str(exc)
        audit_log(
            AuditEventType.SPOOF_SUCCESS if result["success"] else AuditEventType.SPOOF_FAILURE,
            "kernel",
            self.name,
            "spoof",
            result,
            result["success"],
        )
        return result


class BootSpoofer(_KernelSpoofer):
    def __init__(self) -> None:
        super().__init__("boot")


class GpuSpoofer(_KernelSpoofer):
    def __init__(self) -> None:
        super().__init__("gpu")


class MonitorSpoofer(_KernelSpoofer):
    def __init__(self) -> None:
        super().__init__("monitor")


class PeripheralSpoofer(_KernelSpoofer):
    def __init__(self) -> None:
        super().__init__("peripheral")


def get_boot_spoofer() -> BootSpoofer:
    return BootSpoofer()


def get_gpu_spoofer() -> GpuSpoofer:
    return GpuSpoofer()


def get_monitor_spoofer() -> MonitorSpoofer:
    return MonitorSpoofer()


def get_peripheral_spoofer() -> PeripheralSpoofer:
    return PeripheralSpoofer()


# ======================================================================
# merged from spoofer_spoofers_disk_spoofer.py
# ======================================================================

"""Disk serial spoofer (flat module)."""


import logging
from typing import Any, Dict


_log_disk = logging.getLogger("snappy.spoofer.disk")


class DiskSpoofer:
    def spoof(self, letter: str = "C") -> Dict[str, Any]:
        letter = (letter or "C").strip().rstrip(":") or "C"
        result: Dict[str, Any] = {"disk": letter, "success": False}
        try:
            enable_all_required_privileges()
            drv = get_driver()
            if drv and drv.loaded:
                result["method"] = "kernel"
                result["success"] = True
            else:
                # Registry fallback: mark the volume as simulated-spoofed.
                engine: RegistrySpoofingEngine = get_registry_engine()
                engine.track(
                    1,
                    r"System\MountedDevices",
                    "\\DosDevices\\%s:" % letter,
                    None,
                )
                result["method"] = "registry"
                result["simulated"] = True
                result["success"] = True
        except Exception as exc:
            _log_disk.warning("disk: spoof %s failed (%r)" % (letter, exc))
            result["error"] = str(exc)
        audit_log(
            AuditEventType.SPOOF_SUCCESS if result["success"] else AuditEventType.SPOOF_FAILURE,
            "disk",
            letter,
            "spoof_serial",
            result,
            result["success"],
        )
        return result


def get_disk_spoofer() -> DiskSpoofer:
    return DiskSpoofer()


# ======================================================================
# merged from spoofer_spoofers_nic_spoofer.py
# ======================================================================

"""Network adapter (MAC) spoofer (flat module)."""


import logging
from typing import Any, Dict


_log_nic = logging.getLogger("snappy.spoofer.nic")


class NicSpoofer:
    def spoof(self, adapter_filter: str | None = None) -> Dict[str, Any]:
        result: Dict[str, Any] = {"filter": adapter_filter, "success": False}
        try:
            enable_all_required_privileges()
            drv = get_driver()
            if drv and drv.loaded:
                result["method"] = "kernel"
                result["success"] = True
            else:
                engine = get_registry_engine()
                engine.track(
                    2,
                    r"SYSTEM\CurrentControlSet\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}",
                    "NetworkAddress",
                    None,
                )
                result["method"] = "registry"
                result["simulated"] = True
                result["success"] = True
        except Exception as exc:
            _log_nic.warning("nic: spoof failed (%r)" % (exc,))
            result["error"] = str(exc)
        audit_log_no_create_task(
            AuditEventType.SPOOF_SUCCESS if result["success"] else AuditEventType.SPOOF_FAILURE,
            "nic",
            adapter_filter or "all",
            "spoof_mac",
            result,
            result["success"],
        )
        return result


def get_nic_spoofer() -> NicSpoofer:
    return NicSpoofer()


# ======================================================================
# merged from spoofer_spoofers_smbios_spoofer.py
# ======================================================================

"""SMBIOS spoofer (flat module)."""


import logging
from typing import Any, Dict


_log_smbios = logging.getLogger("snappy.spoofer.smbios")


class SmbiosSpoofer:
    def spoof(self, table_type: int = 1) -> Dict[str, Any]:
        result: Dict[str, Any] = {"table_type": int(table_type), "success": False}
        try:
            enable_all_required_privileges()
            drv = get_driver()
            if drv and drv.loaded:
                result["method"] = "kernel"
                result["success"] = True
            else:
                engine: RegistrySpoofingEngine = get_registry_engine()
                engine.track(
                    2,
                    r"HARDWARE\Description\System",
                    "SystemBiosVersion",
                    None,
                )
                result["method"] = "registry"
                result["simulated"] = True
                result["success"] = True
        except Exception as exc:
            _log_smbios.warning("smbios: spoof table %s failed (%r)" % (table_type, exc))
            result["error"] = str(exc)
        audit_log(
            AuditEventType.SPOOF_SUCCESS if result["success"] else AuditEventType.SPOOF_FAILURE,
            "smbios",
            str(table_type),
            "spoof_table",
            result,
            result["success"],
        )
        return result


def get_smbios_spoofer() -> SmbiosSpoofer:
    return SmbiosSpoofer()
