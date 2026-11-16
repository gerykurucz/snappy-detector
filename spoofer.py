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
                    try:
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
                    except (AttributeError, TypeError, ValueError):
                        continue
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
                    default=str,
                )
            os.replace(tmp, self.path)
        except (OSError, TypeError, ValueError):
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
            if len(self._entries) > 2000:
                del self._entries[:-2000]
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
_logger_lock = threading.Lock()


def get_audit_logger() -> AuditLogger:
    global _logger
    with _logger_lock:
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
                removed += int(not os.path.exists(path))
            else:
                os.remove(path)
                removed += 1
        except OSError:
            pass
        return removed

    def _clean_files(self) -> List[str]:
        # Never sweep the system temp directory, captures, or current logs.
        # This operation only removes app logs older than 30 days.
        targets = [os.path.join(self.app_dir, "logs")]
        cutoff = time.time() - 30 * 24 * 60 * 60
        cleaned: List[str] = []
        for target in targets:
            if not os.path.exists(target):
                continue
            try:
                for name in os.listdir(target):
                    p = os.path.join(target, name)
                    try:
                        if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
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
                    low = name.casefold()
                    if not low.startswith(("snappyai-", "snappy-detector-")):
                        continue
                    p = os.path.join(tmp, name)
                    try:
                        if os.path.isdir(p) and not os.path.islink(p):
                            shutil.rmtree(p)
                            cleaned.append(p)
                        elif os.path.isfile(p) or os.path.islink(p):
                            os.remove(p)
                            cleaned.append(p)
                    except OSError:
                        continue
            except OSError:
                pass
        return cleaned

    def _clean_cache(self) -> List[str]:
        target = os.path.join(self.app_dir, "cache")
        if not os.path.exists(target):
            return []
        removed = [target] if self._delete_tree(target) else []
        try:
            os.makedirs(target, exist_ok=True)
        except OSError:
            pass
        return removed

    def _clean_registry(self) -> Dict[str, Any]:
        """Remove the app-owned HKCU key after snapshotting every value."""
        cleaned: List[str] = []
        try:
            import winreg

            root = winreg.HKEY_CURRENT_USER
            sub = r"Software\SnappyAI"

            tracked = RegistrySpoofingEngine(str(get_registry_engine().backup_dir))

            def collect(path: str) -> None:
                with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
                    tracked.track_key("HKCU", path)
                    i = 0
                    while True:
                        try:
                            name, value, value_type = winreg.EnumValue(key, i)
                        except OSError:
                            break
                        tracked.track("HKCU", path, name, value, value_type)
                        i += 1
                    children: List[str] = []
                    i = 0
                    while True:
                        try:
                            children.append(winreg.EnumKey(key, i))
                        except OSError:
                            break
                        i += 1
                for child in children:
                    collect(path + "\\" + child)

            def delete_tree(path: str) -> None:
                try:
                    with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
                        children = []
                        i = 0
                        while True:
                            try:
                                children.append(winreg.EnumKey(key, i))
                            except OSError:
                                break
                            i += 1
                except FileNotFoundError:
                    return
                for child in children:
                    delete_tree(path + "\\" + child)
                winreg.DeleteKey(root, path)

            try:
                collect(sub)
                snapshot = tracked.create_snapshot(
                    "pre_clean_registry_%s_%03d" % (
                        time.strftime("%Y%m%d_%H%M%S"),
                        int((time.time() % 1.0) * 1000)))
                delete_tree(sub)
                cleaned.append(sub)
                return {"cleaned": cleaned, "snapshot": snapshot, "success": True}
            except FileNotFoundError:
                return {"cleaned": [], "success": True}
        except Exception as exc:
            return {"cleaned": cleaned, "success": False,
                    "error": "%s: %s" % (type(exc).__name__, exc)}

    def full_clean(self) -> Dict[str, Any]:
        files = self._clean_files()
        temp = self._clean_temp_files()
        cache = self._clean_cache()
        registry = self._clean_registry()
        return {
            "success": bool(registry.get("success")),
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

DRIVER_NAME = "Kernel.sys"
DRIVER_INF = "Kernel.inf"
DRIVER_LOADER = "User.exe"
DRIVER_SERVICE = "Kernel"


@dataclass
class DriverInterface:
    loaded: bool = False
    path: str = ""
    handle: Optional[int] = None
    installed: bool = False
    service_name: str = DRIVER_SERVICE
    service_state: str = "not_installed"
    service_path: str = ""
    binary_matches: Optional[bool] = None
    error: str = ""

    @property
    def driver(self) -> bool:
        """Compatibility alias used by older GUI builds."""
        return self.loaded


def _driver_path() -> str:
    return os.path.join(driver_dir(), DRIVER_NAME)


def _service_status(name: str) -> tuple[bool, str, str]:
    """Query a kernel service without relying on localized ``sc.exe`` text."""
    if os.name != "nt":
        return False, "unsupported", "Windows service manager unavailable"
    try:
        import ctypes.wintypes as wt

        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi.OpenSCManagerW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, wt.DWORD]
        advapi.OpenSCManagerW.restype = wt.HANDLE
        advapi.OpenServiceW.argtypes = [wt.HANDLE, wt.LPCWSTR, wt.DWORD]
        advapi.OpenServiceW.restype = wt.HANDLE
        advapi.CloseServiceHandle.argtypes = [wt.HANDLE]
        advapi.CloseServiceHandle.restype = wt.BOOL

        class SERVICE_STATUS_PROCESS(ctypes.Structure):
            _fields_ = [
                ("dwServiceType", wt.DWORD),
                ("dwCurrentState", wt.DWORD),
                ("dwControlsAccepted", wt.DWORD),
                ("dwWin32ExitCode", wt.DWORD),
                ("dwServiceSpecificExitCode", wt.DWORD),
                ("dwCheckPoint", wt.DWORD),
                ("dwWaitHint", wt.DWORD),
                ("dwProcessId", wt.DWORD),
                ("dwServiceFlags", wt.DWORD),
            ]

        advapi.QueryServiceStatusEx.argtypes = [
            wt.HANDLE, ctypes.c_int, ctypes.POINTER(ctypes.c_ubyte),
            wt.DWORD, ctypes.POINTER(wt.DWORD),
        ]
        advapi.QueryServiceStatusEx.restype = wt.BOOL
        scm = advapi.OpenSCManagerW(None, None, 0x0001)  # SC_MANAGER_CONNECT
        if not scm:
            return False, "unknown", "OpenSCManager failed (%d)" % ctypes.get_last_error()
        try:
            service = advapi.OpenServiceW(scm, name, 0x0004)  # QUERY_STATUS
            if not service:
                code = ctypes.get_last_error()
                if code == 1060:  # ERROR_SERVICE_DOES_NOT_EXIST
                    return False, "not_installed", ""
                return False, "unknown", "OpenService failed (%d)" % code
            try:
                status = SERVICE_STATUS_PROCESS()
                needed = wt.DWORD()
                ok = advapi.QueryServiceStatusEx(
                    service, 0,
                    ctypes.cast(ctypes.byref(status), ctypes.POINTER(ctypes.c_ubyte)),
                    ctypes.sizeof(status), ctypes.byref(needed),
                )
                if not ok:
                    return True, "unknown", (
                        "QueryServiceStatusEx failed (%d)" % ctypes.get_last_error())
                states = {
                    1: "stopped", 2: "start_pending", 3: "stop_pending",
                    4: "running", 5: "continue_pending", 6: "pause_pending",
                    7: "paused",
                }
                return True, states.get(int(status.dwCurrentState), "unknown"), ""
            finally:
                advapi.CloseServiceHandle(service)
        finally:
            advapi.CloseServiceHandle(scm)
    except Exception as exc:
        return False, "unknown", "%s: %s" % (type(exc).__name__, exc)


def _service_image_path(name: str) -> tuple[str, str]:
    """Resolve a driver's configured ImagePath to an absolute file path."""
    if os.name != "nt":
        return "", "Windows registry unavailable"
    try:
        import winreg as _winreg

        key_path = r"SYSTEM\CurrentControlSet\Services\%s" % name
        with _winreg.OpenKey(_winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            raw = str(_winreg.QueryValueEx(key, "ImagePath")[0] or "").strip()
        if not raw:
            return "", "service ImagePath is empty"
        raw = os.path.expandvars(raw)
        if raw.startswith('"'):
            end = raw.find('"', 1)
            raw = raw[1:end] if end > 1 else raw.strip('"')
        if raw.startswith("\\??\\"):
            raw = raw[4:]
        root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
        low = raw.casefold()
        if low.startswith("\\systemroot\\"):
            raw = os.path.join(root, raw[len("\\SystemRoot\\"):])
        elif low.startswith("system32\\"):
            raw = os.path.join(root, raw)
        return os.path.abspath(raw), ""
    except Exception as exc:
        return "", "%s: %s" % (type(exc).__name__, exc)


def _same_file_hash(left: str, right: str) -> Optional[bool]:
    if not left or not right or not os.path.isfile(left) or not os.path.isfile(right):
        return None
    try:
        import hashlib

        def digest(path: str) -> bytes:
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    h.update(block)
            return h.digest()

        return digest(left) == digest(right)
    except OSError:
        return None


def get_driver() -> Optional[DriverInterface]:
    """Return truthful bundled-file and installed-service state."""
    path = _driver_path()
    installed, state, error = _service_status(DRIVER_SERVICE)
    service_path = ""
    matches: Optional[bool] = None
    if installed:
        service_path, path_error = _service_image_path(DRIVER_SERVICE)
        matches = _same_file_hash(path, service_path)
        if path_error:
            error = path_error if not error else error + "; " + path_error
        elif matches is False:
            mismatch = "installed service binary does not match bundled Kernel.sys"
            error = mismatch if not error else error + "; " + mismatch
        elif matches is None:
            unknown = "installed service binary could not be verified"
            error = unknown if not error else error + "; " + unknown
    return DriverInterface(
        loaded=installed and state == "running" and matches is True,
        path=path,
        installed=installed,
        service_state=state,
        service_path=service_path,
        binary_matches=matches,
        error=error,
    )


def is_driver_loaded() -> bool:
    iface = get_driver()
    return bool(iface and iface.loaded)


def get_loading_status() -> Dict[str, Any]:
    """Report whether the kernel driver is present and active."""
    iface = get_driver()
    driver_file = bool(iface and iface.path and os.path.isfile(iface.path))
    driver_active = bool(iface and iface.loaded)
    root = driver_dir()
    return {
        "driver_active": driver_active,
        "driver_file": driver_file,
        "driver_path": iface.path if iface else "",
        "driver_installed": bool(iface and iface.installed),
        "service_name": DRIVER_SERVICE,
        "service_state": iface.service_state if iface else "unknown",
        "service_path": iface.service_path if iface else "",
        "binary_matches": iface.binary_matches if iface else None,
        "error": iface.error if iface else "driver status unavailable",
        "inf_path": os.path.join(root, DRIVER_INF),
        "loader_path": os.path.join(root, DRIVER_LOADER),
    }


def reload_driver() -> DriverInterface:
    """Start the already-installed bundled driver service.

    Installation is intentionally not implicit: staging a root kernel driver
    changes boot/service state and needs its own explicit, elevated workflow.
    """
    iface = get_driver() or DriverInterface(path=_driver_path())
    if iface.loaded:
        return iface
    if not os.path.isfile(iface.path):
        iface.error = "bundled driver file is missing"
        return iface
    if not iface.installed:
        iface.error = (
            "driver service is not installed; review and install driver\\Kernel.inf "
            "from an elevated Windows driver-install workflow")
        return iface
    if iface.binary_matches is not True:
        iface.error = iface.error or (
            "refusing to start an unverified Kernel service binary")
        return iface
    if not is_admin():
        iface.error = "administrator rights are required to start a kernel driver"
        return iface
    try:
        import subprocess
        from utils import console_encoding, silent_kwargs

        proc = subprocess.run(
            ["sc.exe", "start", DRIVER_SERVICE], capture_output=True, text=True,
            encoding=console_encoding(), errors="replace", timeout=30.0,
            **silent_kwargs(),
        )
        deadline = time.monotonic() + 10.0
        current = get_driver() or iface
        while (not current.loaded and current.installed
               and current.service_state in ("stopped", "start_pending")
               and time.monotonic() < deadline):
            time.sleep(0.1)
            current = get_driver() or current
        if not current.loaded:
            detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
            current.error = detail[-1000:] or "service did not enter running state"
        iface = current
    except Exception as exc:
        _log_driver.warning("driver: reload failed (%r)" % (exc,))
        iface.loaded = False
        iface.error = "%s: %s" % (type(exc).__name__, exc)
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

    Looked up only in application-owned locations.  Spoofing tools may run
    elevated, so an executable planted in the current directory or PATH must
    never be selected implicitly.
    """
    found: Dict[str, str] = {}
    here = os.path.dirname(os.path.abspath(__file__))
    search_dirs = [os.path.join(here, "driver"), here]
    for key, exe in TOOL_NAMES.items():
        path = ""
        for d in search_dirs:
            if not d:
                continue
            candidate = os.path.join(d, exe)
            if os.path.isfile(candidate):
                path = candidate
                break
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
    """Enable token privileges used by driver and registry restore paths."""
    if os.name != "nt" or not is_admin():
        return False
    try:
        import ctypes.wintypes as wt

        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]

        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", wt.DWORD)]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [("PrivilegeCount", wt.DWORD),
                        ("Privileges", LUID_AND_ATTRIBUTES * 1)]

        kernel32.GetCurrentProcess.restype = wt.HANDLE
        kernel32.CloseHandle.argtypes = [wt.HANDLE]
        kernel32.CloseHandle.restype = wt.BOOL
        advapi.OpenProcessToken.argtypes = [
            wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
        advapi.OpenProcessToken.restype = wt.BOOL
        advapi.LookupPrivilegeValueW.argtypes = [
            wt.LPCWSTR, wt.LPCWSTR, ctypes.POINTER(LUID)]
        advapi.LookupPrivilegeValueW.restype = wt.BOOL
        advapi.AdjustTokenPrivileges.argtypes = [
            wt.HANDLE, wt.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES), wt.DWORD,
            ctypes.c_void_p, ctypes.c_void_p]
        advapi.AdjustTokenPrivileges.restype = wt.BOOL

        token = wt.HANDLE()
        if not advapi.OpenProcessToken(
                kernel32.GetCurrentProcess(), 0x0020 | 0x0008,
                ctypes.byref(token)):
            return False
        try:
            enabled = 0
            for privilege in (
                    "SeLoadDriverPrivilege", "SeBackupPrivilege",
                    "SeRestorePrivilege"):
                luid = LUID()
                if not advapi.LookupPrivilegeValueW(
                        None, privilege, ctypes.byref(luid)):
                    continue
                tp = TOKEN_PRIVILEGES()
                tp.PrivilegeCount = 1
                tp.Privileges[0].Luid = luid
                tp.Privileges[0].Attributes = 0x00000002
                ctypes.set_last_error(0)
                if advapi.AdjustTokenPrivileges(
                        token, False, ctypes.byref(tp), 0, None, None) \
                        and ctypes.get_last_error() != 1300:
                    enabled += 1
            return enabled == 3
        finally:
            kernel32.CloseHandle(token)
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
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from utils import app_dir

try:
    import winreg
except Exception:  # pragma: no cover - non-Windows
    winreg = None


@dataclass
class RegistryChange:
    hive: str
    key: str
    value: str
    previous: Any
    previous_type: Optional[int] = None
    key_only: bool = False


class RegistrySpoofingEngine:
    def __init__(self, backup_dir: Optional[str] = None) -> None:
        from pathlib import Path

        self.backup_dir = Path(
            backup_dir or os.path.join(app_dir(), "registry_backups"))
        os.makedirs(self.backup_dir, exist_ok=True)
        self._changes: List[RegistryChange] = []

    @staticmethod
    def _snapshot_name(name: str) -> str:
        value = str(name or "").strip()
        if (not value or value in (".", "..") or len(value) > 120
                or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
                       for c in value)):
            raise ValueError("snapshot name may contain only letters, digits, '.', '_', and '-'")
        return value

    @staticmethod
    def _hive_name(hive: Any) -> str:
        text = str(hive).upper()
        aliases = {
            "1": "HKCU", "2": "HKLM", "3": "HKCR", "4": "HKU",
            "HKCU": "HKCU", "HKEY_CURRENT_USER": "HKCU",
            "HKLM": "HKLM", "HKEY_LOCAL_MACHINE": "HKLM",
            "HKCR": "HKCR", "HKEY_CLASSES_ROOT": "HKCR",
            "HKU": "HKU", "HKEY_USERS": "HKU",
            str(0x80000001): "HKCU", str(0x80000002): "HKLM",
            str(0x80000000): "HKCR", str(0x80000003): "HKU",
        }
        if text not in aliases:
            raise ValueError("unsupported registry hive %r" % (hive,))
        return aliases[text]

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, bytes):
            import base64
            return {"__registry_bytes__": base64.b64encode(value).decode("ascii")}
        if isinstance(value, tuple):
            return [RegistrySpoofingEngine._json_value(v) for v in value]
        return value

    @staticmethod
    def _registry_value(value: Any) -> Any:
        if isinstance(value, dict) and set(value) == {"__registry_bytes__"}:
            import base64
            return base64.b64decode(value["__registry_bytes__"], validate=True)
        return value

    def list_snapshots(self) -> List[str]:
        try:
            names = [
                name[:-5]
                for name in os.listdir(self.backup_dir)
                if name.endswith(".json") and not name.startswith(".")
            ]
            return sorted(names)
        except OSError:
            return []

    def create_snapshot(self, name: str) -> str:
        """Snapshot the current values of the tracked changes under *name*."""
        name = self._snapshot_name(name)
        os.makedirs(self.backup_dir, exist_ok=True)
        path = os.path.join(self.backup_dir, "%s.json" % name)
        if os.path.exists(path):
            raise FileExistsError("snapshot already exists: %s" % path)
        payload = {
            "name": name,
            "changes": [
                {
                    "hive": c.hive,
                    "key": c.key,
                    "value": c.value,
                    "previous": self._json_value(c.previous),
                    "previous_type": c.previous_type,
                    "key_only": c.key_only,
                }
                for c in self._changes
            ],
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)
        return str(path)

    def restore_snapshot(self, name: str) -> bool:
        """Write previously snapshotted values back to the registry."""
        try:
            name = self._snapshot_name(name)
        except ValueError:
            return False
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
            "HKCU": winreg.HKEY_CURRENT_USER,
            "HKLM": winreg.HKEY_LOCAL_MACHINE,
            "HKCR": winreg.HKEY_CLASSES_ROOT,
            "HKU": winreg.HKEY_USERS,
        }
        changes = payload.get("changes", []) if isinstance(payload, dict) else []
        if not isinstance(changes, list):
            return False
        failed = False
        for change in changes:
            try:
                if not isinstance(change, dict):
                    raise ValueError("change is not an object")
                hive = hive_names[self._hive_name(change.get("hive"))]
                key_path = str(change["key"])
                if bool(change.get("key_only")):
                    with winreg.CreateKeyEx(
                            hive, key_path, 0, winreg.KEY_SET_VALUE):
                        pass
                    continue
                value_name = str(change["value"])
                previous_type = change.get("previous_type")
                if previous_type is None:
                    try:
                        with winreg.OpenKey(
                                hive, key_path, 0, winreg.KEY_SET_VALUE) as key:
                            winreg.DeleteValue(key, value_name)
                    except FileNotFoundError:
                        pass
                else:
                    with winreg.CreateKeyEx(
                            hive, key_path, 0, winreg.KEY_SET_VALUE) as key:
                        winreg.SetValueEx(
                            key, value_name, 0, int(previous_type),
                            self._registry_value(change.get("previous")),
                        )
            except (KeyError, OSError, TypeError, ValueError):
                failed = True
        return not failed

    def track(self, hive: Any, key: str, value: str, previous: Any,
              previous_type: Optional[int] = None) -> None:
        """Remember a value so it can be snapshotted/restored later."""
        change = RegistryChange(
            hive=self._hive_name(hive), key=str(key), value=str(value),
            previous=previous, previous_type=previous_type)
        identity = (change.hive.casefold(), change.key.casefold(),
                    change.value.casefold())
        if any((c.hive.casefold(), c.key.casefold(), c.value.casefold()) == identity
               for c in self._changes):
            return
        self._changes.append(change)

    def track_key(self, hive: Any, key: str) -> None:
        """Remember that a key exists, including keys with no values."""
        change = RegistryChange(
            hive=self._hive_name(hive), key=str(key), value="",
            previous=None, previous_type=None, key_only=True)
        identity = (change.hive.casefold(), change.key.casefold())
        if any(c.key_only and (c.hive.casefold(), c.key.casefold()) == identity
               for c in self._changes):
            return
        self._changes.append(change)


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
    if os.name != "nt":
        return False
    try:
        ctypes.WinDLL("srclient", use_last_error=True)
        return True
    except OSError:
        return False


def create_restore_point(name: str = "Snappy Spoofer checkpoint") -> str:
    """Request a system restore point.

    Returns the restore-point description on success, or a reason string on
    failure. Without WMI elevation this degrades gracefully.
    """
    if not is_supported():
        return "unavailable: System Restore API is not installed"
    if not is_admin():
        return "unavailable: administrator rights are required"
    description = str(name or "Snappy Spoofer checkpoint").strip()[:256]
    try:
        import subprocess
        from utils import console_encoding, powershell_exe, silent_kwargs

        quoted = description.replace("'", "''")
        ps = (
            "$ErrorActionPreference='Stop'; "
            "Checkpoint-Computer -Description '%s' "
            "-RestorePointType MODIFY_SETTINGS; 'created'" % quoted
        )
        proc = subprocess.run(
            [powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, encoding=console_encoding(),
            errors="replace", timeout=90.0, **silent_kwargs(),
        )
        if proc.returncode != 0 or "created" not in (proc.stdout or ""):
            detail = ((proc.stderr or "") + (proc.stdout or "")).strip()
            return "unavailable: " + (detail[-1000:] or "restore point command failed")
        return description
    except Exception as exc:
        _log_restore.warning("restore point: %r" % (exc,))
        return "unavailable: %r" % (exc,)


# ======================================================================
# merged from spoofer_core_transaction.py
# ======================================================================

"""Undo transaction helpers for the HWID spoofer (flat module)."""


import json
import os
from dataclasses import dataclass
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
        self._load()

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if not isinstance(payload, dict) or not isinstance(payload.get("steps"), list):
                return
            loaded: List[Step] = []
            for item in payload["steps"]:
                if not isinstance(item, dict):
                    continue
                loaded.append(Step(
                    action=str(item.get("action") or ""),
                    target=str(item.get("target") or ""),
                    previous=item.get("previous"),
                    undo=item.get("undo") if isinstance(item.get("undo"), dict) else None,
                ))
            self.steps = loaded
            self.committed = bool(payload.get("committed"))
        except (OSError, TypeError, ValueError):
            return

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
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, self.path)
        self.committed = True
        return self.path

    def undo(self) -> Dict[str, Any]:
        restored: List[str] = []
        failed: List[Dict[str, str]] = []
        for step in reversed(self.steps):
            if step.undo is None:
                continue
            try:
                kind = str(step.undo.get("kind") or "").casefold()
                if kind == "restore_snapshot":
                    name = str(step.undo.get("name") or "")
                    if not get_registry_engine().restore_snapshot(name):
                        raise OSError("snapshot restore failed")
                elif kind == "registry":
                    if winreg is None:
                        raise OSError("Windows registry is unavailable")
                    hive_name = RegistrySpoofingEngine._hive_name(
                        step.undo.get("hive"))
                    hives = {
                        "HKCU": winreg.HKEY_CURRENT_USER,
                        "HKLM": winreg.HKEY_LOCAL_MACHINE,
                        "HKCR": winreg.HKEY_CLASSES_ROOT,
                        "HKU": winreg.HKEY_USERS,
                    }
                    key_path = str(step.undo["key"])
                    value_name = str(step.undo["value"])
                    previous_type = step.undo.get("previous_type")
                    if previous_type is None:
                        try:
                            with winreg.OpenKey(
                                    hives[hive_name], key_path, 0,
                                    winreg.KEY_SET_VALUE) as key:
                                winreg.DeleteValue(key, value_name)
                        except FileNotFoundError:
                            pass
                    else:
                        with winreg.CreateKeyEx(
                                hives[hive_name], key_path, 0,
                                winreg.KEY_SET_VALUE) as key:
                            winreg.SetValueEx(
                                key, value_name, 0, int(previous_type),
                                step.undo.get("previous"))
                else:
                    raise ValueError("unsupported undo kind %r" % kind)
                restored.append(step.target)
            except Exception as exc:
                failed.append({"target": step.target, "error": str(exc)})
        return {"restored": restored, "failed": failed, "success": not failed}


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
            if drv and not drv.loaded:
                drv = reload_driver()
            if drv and drv.loaded:
                result["success"] = True
                result["requires_driver"] = False
                result["method"] = "kernel-load"
                result["service_state"] = drv.service_state
                result["note"] = (
                    "the bundled driver applies its supported identifier set "
                    "when the service loads; it has no per-identifier IOCTL")
            else:
                result["error"] = (
                    drv.error if drv and drv.error else "kernel driver not loaded")
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
        letter = str(letter or "C").strip().rstrip(":").upper() or "C"
        result: Dict[str, Any] = {"disk": letter, "success": False}
        if len(letter) != 1 or letter not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            result["error"] = "volume letter must be one ASCII letter from A through Z"
            return result
        try:
            drv = get_driver()
            if drv and drv.loaded:
                result["method"] = "kernel-load"
                result["success"] = True
                result["note"] = "the active driver applied disk spoofing when it loaded"
            else:
                tools = detect()
                tool = tools.get("volumeid") or ""
                if not tool:
                    raise FileNotFoundError("driver/Volumeid64.exe is missing")
                if not is_admin():
                    raise PermissionError(
                        "administrator rights are required to change a volume ID")
                import secrets
                import subprocess
                from utils import console_encoding, silent_kwargs

                serial = secrets.token_hex(4).upper()
                serial = serial[:4] + "-" + serial[4:]
                proc = subprocess.run(
                    [tool, "-accepteula", letter + ":", serial],
                    capture_output=True, text=True, encoding=console_encoding(),
                    errors="replace", timeout=60.0, **silent_kwargs(),
                )
                result["method"] = "volumeid"
                result["new_volume_id"] = serial
                result["output"] = ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
                result["success"] = proc.returncode == 0
                result["requires_reboot"] = True
                result["reversible"] = False
                if not result["success"]:
                    result["error"] = result["output"].strip() or (
                        "VolumeId exited with code %d" % proc.returncode)
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
            drv = get_driver()
            if drv and drv.loaded:
                result["method"] = "kernel-load"
                result["success"] = True
                result["note"] = "the active driver applied NIC spoofing when it loaded"
            else:
                if winreg is None:
                    raise OSError("Windows registry is unavailable")
                if not is_admin():
                    raise PermissionError(
                        "administrator rights are required to change a MAC address")
                import secrets

                root_path = (
                    r"SYSTEM\CurrentControlSet\Control\Class"
                    r"\{4d36e972-e325-11ce-bfc1-08002be10318}")
                wanted = str(adapter_filter or "").strip().casefold()
                candidates: list[tuple[str, str, str]] = []
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root_path) as root:
                    index = 0
                    while True:
                        try:
                            sub = winreg.EnumKey(root, index)
                        except OSError:
                            break
                        index += 1
                        path = root_path + "\\" + sub
                        try:
                            with winreg.OpenKey(
                                    winreg.HKEY_LOCAL_MACHINE, path) as key:
                                desc = str(winreg.QueryValueEx(key, "DriverDesc")[0])
                                net_id = str(winreg.QueryValueEx(
                                    key, "NetCfgInstanceId")[0])
                                try:
                                    characteristics = int(winreg.QueryValueEx(
                                        key, "Characteristics")[0])
                                except (OSError, TypeError, ValueError):
                                    characteristics = 0
                        except OSError:
                            continue
                        blob = (desc + " " + net_id + " " + sub).casefold()
                        if characteristics & 0x4 and (not wanted or wanted in blob):
                            candidates.append((path, desc, net_id))
                if not candidates:
                    raise LookupError("no matching physical network adapter was found")
                path, desc, net_id = candidates[0]
                try:
                    with winreg.OpenKey(
                            winreg.HKEY_LOCAL_MACHINE, path) as key:
                        previous, previous_type = winreg.QueryValueEx(
                            key, "NetworkAddress")
                except FileNotFoundError:
                    previous, previous_type = None, None
                engine = get_registry_engine()
                engine.track("HKLM", path, "NetworkAddress", previous, previous_type)
                snapshot = engine.create_snapshot(
                    "pre_nic_%s" % time.strftime("%Y%m%d_%H%M%S"))
                raw = bytearray(secrets.token_bytes(6))
                raw[0] = (raw[0] & 0xFC) | 0x02
                new_mac = "".join("%02X" % b for b in raw)
                with winreg.OpenKey(
                        winreg.HKEY_LOCAL_MACHINE, path, 0,
                        winreg.KEY_SET_VALUE) as key:
                    winreg.SetValueEx(
                        key, "NetworkAddress", 0, winreg.REG_SZ, new_mac)
                result["method"] = "registry"
                result["adapter"] = desc
                result["adapter_id"] = net_id
                result["new_mac"] = new_mac
                result["snapshot"] = snapshot
                result["requires_adapter_restart"] = True
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
        try:
            table_type = int(table_type)
            if not 0 <= table_type <= 255:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return {"table_type": table_type, "success": False,
                    "error": "SMBIOS table type must be an integer from 0 through 255"}
        result: Dict[str, Any] = {"table_type": table_type, "success": False}
        try:
            drv = get_driver()
            if drv and drv.loaded:
                result["method"] = "kernel-load"
                result["success"] = True
                result["note"] = "the active driver applied SMBIOS spoofing when it loaded"
            else:
                result["error"] = (
                    (drv.error if drv else "kernel driver unavailable")
                    + "; SMBIOS firmware values have no truthful registry fallback")
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
