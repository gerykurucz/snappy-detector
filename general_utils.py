from __future__ import annotations

import datetime as _dt
import json
import locale
import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class Diagnosis:
    log_path: Path
    json_path: Path
    failed_sections: list[str] = field(default_factory=list)
    sections: dict[str, Any] = field(default_factory=dict)


def _env_section() -> dict[str, Any]:
    enc = locale.getpreferredencoding(False)
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "locale": locale.getlocale(),
        "preferred_encoding": enc,
        "filesystem_encoding": sys.getfilesystemencoding(),
        "env": {
            k: os.environ.get(k, "")
            for k in ("OS", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
                      "PSModulePath", "TEMP", "TMP")
        },
    }


def _parse_ps1(path: Path) -> tuple[bool, str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return (False, f"unreadable: {exc}")
    for enc in ("utf-8-sig", "utf-8", "cp1250", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return (False, "undecodable in utf-8/cp1250/cp1252")
    depth = 0
    in_str: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == in_str and (i == 0 or text[i - 1] != "`"):
                in_str = None
        elif ch in ("'", '"'):
            in_str = ch
        elif ch == "#":
            while i < len(text) and text[i] != "\n":
                i += 1
        elif ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
            if depth < 0:
                return (False, f"unbalanced bracket at offset {i}")
        i += 1
    if in_str:
        return (False, "unterminated string literal")
    if depth != 0:
        return (False, "unbalanced brackets at end of file")
    return (True, f"balanced ({len(text)} chars, decoded as {enc})")


def run_diagnosis(
    backend: Any,
    out: str | Path,
    plan_dir: str | Path | None = None,
    dry_run: bool = True,
    brand: str = "latency",
    on_progress: Callable[[str], None] | None = None,
) -> Diagnosis:
    def note(msg: str) -> None:
        if on_progress is not None:
            on_progress(msg)

    outdir = Path(out)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir.mkdir(parents=True, exist_ok=True)
    tag = brand.lower().replace(" ", "")
    log_path = outdir / f"{tag}-diagnose-{stamp}.log"
    json_path = outdir / f"{tag}-diagnose-{stamp}.json"
    sections: dict[str, Any] = {}
    failed: list[str] = []

    note("environment")
    try:
        sections["environment"] = _env_section()
    except Exception as exc:
        failed.append("environment")
        sections["environment"] = {"error": f"{type(exc).__name__}: {exc}"}

    note("plan scripts")
    plan_info: dict[str, Any] = {"plan_dir": str(plan_dir) if plan_dir else None}
    if plan_dir:
        for name in ("apply.ps1", "undo.ps1", "manifest.json", "snapshot.json"):
            p = Path(plan_dir) / name
            if not p.exists():
                plan_info[name] = "missing"
                continue
            if p.suffix == ".ps1":
                ok, msg = _parse_ps1(p)
                plan_info[name] = msg
                if not ok:
                    failed.append(f"parse:{name}")
            else:
                try:
                    json.loads(p.read_text(encoding="utf-8-sig"))
                    plan_info[name] = "valid json"
                except (OSError, ValueError) as exc:
                    plan_info[name] = f"invalid: {exc}"
                    failed.append(f"parse:{name}")
        if dry_run:
            apply = Path(plan_dir) / "apply.ps1"
            if apply.exists():
                if sys.platform.startswith("win"):
                    res = backend.powershell(
                        f"& '{apply}' -WhatIf2 -NoPause *>&1 | Out-String",
                        timeout=120.0,
                    )
                    plan_info["dry_run_code"] = res.code
                    plan_info["dry_run_tail"] = (res.stdout or res.stderr)[-2000:]
                    if not res.ok:
                        failed.append("dry-run")
                else:
                    plan_info["dry_run"] = "skipped: needs Windows"
    sections["plan"] = plan_info

    note("inventory")
    try:
        from measurement_utils import CollectOptions, collect_inventory

        inv = collect_inventory(
            backend, CollectOptions(detect_processes=False), on_progress=None
        )
        sections["inventory"] = inv.to_dict()
    except Exception as exc:
        failed.append("inventory")
        sections["inventory"] = {"error": f"{type(exc).__name__}: {exc}"}

    lines = [
        f"{brand} diagnosis {stamp}",
        f"python: {sections.get('environment', {}).get('python', '?')}",
        f"platform: {sections.get('environment', {}).get('platform', '?')}",
        "",
    ]
    for key, val in sections.items():
        lines.append(f"[{key}]")
        lines.append(json.dumps(val, indent=1, default=str)[:4000])
        lines.append("")
    if failed:
        lines.append(f"failed sections: {', '.join(failed)}")
    else:
        lines.append("all sections collected")
    log_path.write_text("\n".join(lines), encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {"taken_at": stamp, "failed_sections": failed, "sections": sections},
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )
    return Diagnosis(log_path=log_path, json_path=json_path, failed_sections=failed,
                     sections=sections)

import subprocess


@dataclass(slots=True)
class RunResult:
    code: int
    output: str = ""
    log_path: str = ""


def is_elevated() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def silent_kwargs() -> dict:
    if not sys.platform.startswith("win"):
        return {"stdin": subprocess.DEVNULL}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return {
        "startupinfo": startup,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "stdin": subprocess.DEVNULL,
    }


def console_encoding() -> str:
    return "oem" if sys.platform.startswith("win") else "utf-8"


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def system32_dir() -> str:
    root = os.environ.get("SystemRoot") or os.environ.get("windir")
    return os.path.join(root or "C:\\Windows", "System32")


def powershell_exe() -> str:
    cand = os.path.join(system32_dir(), "WindowsPowerShell", "v1.0",
                        "powershell.exe")
    return cand if os.path.isfile(cand) else "powershell"


def dll_search_paths(names, where: str = "system32") -> list[str]:
    roots = []
    if where in ("system32", "both"):
        roots.append(system32_dir())
    if where in ("appdir", "both"):
        roots.append(app_dir())
    found = []
    for root in roots:
        for name in names:
            path = os.path.join(root, name)
            if os.path.isfile(path) and path not in found:
                found.append(path)
    return found


def dll_pin_ok(path: str) -> bool:
    pin = path + ".sha256"
    if not os.path.isfile(pin):
        return True
    try:
        import hashlib
        with open(pin, "r", encoding="utf-8") as handle:
            expected = handle.read().split()[0].strip().lower()
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest().lower() == expected
    except Exception:
        return False


def _elevated_hidden(command: str, timeout: float) -> RunResult:
    import ctypes
    import ctypes.wintypes

    info = type(
        "_Info",
        (ctypes.Structure,),
        {
            "_fields_": [
                ("cbSize", ctypes.wintypes.DWORD),
                ("fMask", ctypes.wintypes.ULONG),
                ("hwnd", ctypes.wintypes.HWND),
                ("lpVerb", ctypes.wintypes.LPCWSTR),
                ("lpFile", ctypes.wintypes.LPCWSTR),
                ("lpParameters", ctypes.wintypes.LPCWSTR),
                ("lpDirectory", ctypes.wintypes.LPCWSTR),
                ("nShow", ctypes.c_int),
                ("hInstApp", ctypes.wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p),
                ("lpClass", ctypes.wintypes.LPCWSTR),
                ("hkeyClass", ctypes.wintypes.HKEY),
                ("dwHotKey", ctypes.wintypes.DWORD),
                ("hIcon", ctypes.wintypes.HANDLE),
                ("hProcess", ctypes.wintypes.HANDLE),
            ]
        },
    )()
    info.cbSize = ctypes.sizeof(type(info))
    info.fMask = 0x40
    info.lpVerb = "runas"
    info.lpFile = powershell_exe()
    info.lpParameters = command
    info.nShow = 0
    ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info))
    if not ok:
        return RunResult(code=1, output="elevation was refused or failed")
    try:
        kernel32 = ctypes.windll.kernel32
        wait = kernel32.WaitForSingleObject(info.hProcess, int(timeout * 1000))
        code = ctypes.wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        kernel32.CloseHandle(info.hProcess)
        if wait != 0:
            return RunResult(code=124, output="elevated run timed out")
        return RunResult(code=int(code.value), output="")
    except Exception as exc:
        return RunResult(code=1, output=f"elevated run failed: {exc}")


def _ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


_PS_BARE_CHARS = frozenset(" \t'\";|&$`{}()")


def _ps_arg(arg: str) -> str:
    s = str(arg)
    if s.startswith("-") and not any(c in s for c in _PS_BARE_CHARS):
        return s
    return _ps_quote(s)


def run_ps1(
    script: Path | str,
    args: tuple[str, ...] = (),
    timeout: float = 900.0,
    elevate: bool = False,
    log_path: Path | str | None = None,
) -> RunResult:
    if not sys.platform.startswith("win"):
        return RunResult(code=2, output="applying changes requires Windows")
    path = Path(script)
    if not path.exists():
        return RunResult(code=2, output=f"{path} does not exist")
    if elevate and not is_elevated():
        if log_path:
            try:
                Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        inner = ("& " + _ps_quote(path)
                 + "".join(" " + _ps_arg(a) for a in args))
        if log_path:
            inner += f" *>&1 | Out-File -FilePath {_ps_quote(log_path)} -Encoding utf8"
        inner += "; exit $LASTEXITCODE"
        params = " ".join(
            ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", inner]
        )
        res = _elevated_hidden(params, timeout)
        out = ""
        if log_path:
            try:
                out = Path(log_path).read_text(
                    encoding="utf-8-sig", errors="replace"
                )[-8000:]
            except OSError:
                pass
        return RunResult(
            code=res.code, output=out or res.output,
            log_path=str(log_path) if log_path else "",
        )
    try:
        proc = subprocess.run(
            [powershell_exe(), "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", str(path), *args],
            capture_output=True,
            text=True,
            encoding=console_encoding(),
            errors="replace",
            timeout=timeout,
            **silent_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return RunResult(
            code=124, output="run timed out",
            log_path=str(log_path) if log_path else "",
        )
    out = (proc.stdout or "") + (proc.stderr or "")
    if log_path:
        try:
            Path(log_path).write_text(out[-8000:], encoding="utf-8")
        except OSError:
            pass
    return RunResult(
        code=proc.returncode, output=out[-8000:],
        log_path=str(log_path) if log_path else "",
    )

import base64
import hashlib




_KEY = b"SnappyAI/build-secrets/v1"


_BLOB = {
    "DISCORD_CLIENT_ID": "mVzqIJpPmEVQ4kbHjPhdEwNvEw==",
    "DISCORD_CLIENT_SEC": "0FqFddIc+AA0hkWXzfkBYEc9aGvR6yLEfHXHZbN2dlA=",
    "ENTITLEMENT_BRANCH": "xACpdg==",
    "ENTITLEMENT_PUBKEY": "4lqKf9of1F8uhwq51I4RV3lsHynUlVzQYDeaQqkkf1LuD5tmkRbUIiOYNc0=",
    "ENTITLEMENT_REPO": "zwytaskP0wUHrV+D1qgYV0o/Ti/Zzx3LeCzMTpt8aWs=",
    "PRESENCE_WEBHOOK": (
        "wB2rY9FAjl8AvgOT17sMCVAxSi3d0QCNeyXLS5F9dmuHWOomkUOTR1TmQseB+V0QAm4U"
        "OpP5EeU1Gf4apn13YPcgsHySK+0nDrASsYyZAGt+FW9kxuBekm8z+Wi6dFNP/xypWfFX"
        "0xcivSKS/vtaQmoIb1vVkR2PXg=="),
    "ROW_PEPPER": "",
    "STRIPE_MONTHLY": "wB2rY9FAjl8Gogney70aTkM7CWHTzEbWaTPdfJtEbH3+De0r5U/gKVCGPcmIpA52em4X",
    "STRIPE_TRIAL": "wB2rY9FAjl8Gogney70aTkM7CWHTzEbWaTPdfM1xVC7HIehnkk3rRlzlKZbXgg52em4W",
    "DISPATCH_PAT": "",
}


_ENV = {
    "ENTITLEMENT_REPO":   "SNAPPYAI_ENT_REPO",
    "ENTITLEMENT_BRANCH": "SNAPPYAI_ENT_BRANCH",
    "ENTITLEMENT_PUBKEY": "SNAPPYAI_ENT_PUBKEY",
    "STRIPE_MONTHLY":     "SNAPPYAI_STRIPE_MONTHLY",
    "STRIPE_TRIAL":       "SNAPPYAI_STRIPE_TRIAL",
    "DISCORD_CLIENT_ID":  "SNAPPYAI_OAUTH_DISCORD_CLIENT_ID",
    "DISCORD_CLIENT_SEC": "SNAPPYAI_OAUTH_DISCORD_CLIENT_SECRET",
    "PRESENCE_WEBHOOK":   "SNAPPYAI_PRESENCE_WEBHOOK",
    "ROW_PEPPER":         "SNAPPYAI_ROW_PEPPER",
    "DISPATCH_PAT":       "SNAPPYAI_DISPATCH_PAT",
}

_ks = hashlib.sha256(_KEY).digest()
_cache = {}


def _decode(raw):
    data = base64.b64decode(raw, validate=True)
    return bytes(c ^ _ks[i % len(_ks)]
                 for i, c in enumerate(data)).decode("utf-8")


def _get(name):
    if name in _cache:
        return _cache[name]
    value = ""
    if not getattr(sys, "frozen", False):
        env = _ENV.get(name)
        if env:
            value = os.environ.get(env, "").strip()
    if not value:
        raw = _BLOB.get(name, "")
        if raw:
            try:
                value = _decode(raw)
            except Exception:
                value = ""
    if value:
        _cache[name] = value
    return value






def entitlement_repo():
    return _get("ENTITLEMENT_REPO").strip().strip("/").strip()


def entitlement_branch():
    return _get("ENTITLEMENT_BRANCH").strip() or "live"


def entitlement_pubkey():
    return _get("ENTITLEMENT_PUBKEY").strip()


def stripe_monthly_link():
    return _get("STRIPE_MONTHLY").strip()


def stripe_trial_link():
    return _get("STRIPE_TRIAL").strip()


def discord_client_id():
    return _get("DISCORD_CLIENT_ID").strip()


def discord_client_secret():
    return _get("DISCORD_CLIENT_SEC").strip()


def presence_webhook():
    return _get("PRESENCE_WEBHOOK").strip()


def row_pepper():
    return _get("ROW_PEPPER").strip()


def dispatch_pat():
    return _get("DISPATCH_PAT").strip()


def is_test_mode():
    links = stripe_monthly_link() + stripe_trial_link()
    return "buy.stripe.com/test_" in links


def describe_readiness():
    fatal = []
    advisory = []
    if not entitlement_pubkey():
        fatal.append("no Ed25519 public key - a subscription can never be "
                     "verified, so nobody could ever get past the gate")
    if not discord_client_id():
        fatal.append("no Discord OAuth client id - sign-in is impossible")
    if not entitlement_repo():
        fatal.append("no entitlement repo - there is nowhere to fetch the "
                     "signed subscription file from")
    if is_test_mode():
        advisory.append("the Stripe payment links are still in TEST mode - "
                        "checkout will complete, no money will move, and no "
                        "subscription will ever activate")
    if not row_pepper():
        advisory.append("no row-hash pepper - entitlement rows are keyed by "
                        "unsalted sha256(e-mail) in a PUBLIC file")
    if not presence_webhook():
        advisory.append("no presence webhook - account-sharing detection "
                        "is off")
    return fatal, advisory


def is_shippable():
    fatal, advisory = describe_readiness()
    return not fatal and not advisory




SENSITIVE_KEYS = frozenset((
    "entitlement_repo", "entitlement_branch", "entitlement_pubkey",
    "stripe_monthly_link", "stripe_trial_link",
    "discord_client_id", "discord_client_secret",
    "presence_webhook", "dispatch_pat", "row_pepper",
))
