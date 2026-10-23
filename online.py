
import base64
import concurrent.futures
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import re
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import general_utils as build_secrets
import system_core as edsig
import system_core as i18n
from config import config_dir




i18n.publish("en", {
    "online.cancelled": "Discord authentication has been cancelled (or "
                        "failed). Please try again.",
    "ent.st.active": "active",
    "ent.st.trial": "trial",
    "ent.st.grant": "support grant",
    "ent.st.canceling": "active but cancelling",
    "ent.st.past_due": "PAYMENT FAILED",
    "ent.st.canceled": "ended",
    "ent.st.refunded": "refunded / charged back",
    "ent.st.unsubscribed": "no subscription",
    "ent.until": " until %s",
    "ent.plan": " (%s)",
    "ent.cancel_line": "active until %s - will NOT renew",
    "ent.past_due_line": "payment failed - fix the card in your Stripe "
                         "receipt e-mail; access returns when the "
                         "invoice pays",
    "ent.canceled_line": "subscription ended%s",
    "ent.refunded_line": "refunded / charged back - entitlement revoked",
    "ent.expired_line": "expired on %s - renew on Stripe",
})
from system_core import APP_VERSION, IS_WINDOWS, log

























ENTITLEMENT_PATH = "entitlements.json"



FRESHNESS_SECONDS = 6 * 3600




_DEV_REPO = ""


def set_dev_repo(value):
    global _DEV_REPO
    if getattr(sys, "frozen", False):
        log("--repo ignored: this is a shipped build.")
        return False
    _DEV_REPO = str(value or "").strip().strip("/")
    return True







class _Lazy(object):
    def __init__(self, fn):
        self._fn = fn

    def __str__(self):
        return self._fn()

    def __repr__(self):
        return self._fn()

    def __eq__(self, other):
        return self._fn() == other

    def __hash__(self):
        return hash(self._fn())

    def __add__(self, other):
        return self._fn() + other

    def __radd__(self, other):
        return other + self._fn()



LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_PORT = 48321
LOOPBACK_PATH = "/oauth"






SESSION_FILE = "session.json"






PRESENCE_HEARTBEAT_MINUTES = 2

HTTP_TIMEOUT = 15.0
LOGIN_TIMEOUT = 300.0

def CANCELLED_MSG():
    return i18n.L("online.cancelled")







def _provider_table():
    return {
        "discord": dict(
            label="Discord",
            auth="https://discord.com/oauth2/authorize",
            token="https://discord.com/api/oauth2/token",
            userinfo="https://discord.com/api/users/@me",






            scope="identify email", pkce=True, refresh=True,
            client_id=build_secrets.discord_client_id(),
            client_secret=build_secrets.discord_client_secret() or None,
            identity=lambda u: (u.get("id"), u.get("email"),
                                u.get("username")),
        ),
    }


class _ProviderProxy(object):

    def __getitem__(self, key):
        return _provider_table()[key]

    def get(self, key, default=None):
        return _provider_table().get(key, default)

    def __contains__(self, key):
        return key in _provider_table()

    def __iter__(self):
        return iter(_provider_table())

    def __len__(self):
        return len(_provider_table())

    def keys(self):
        return _provider_table().keys()

    def items(self):
        return _provider_table().items()

    def values(self):
        return _provider_table().values()


PROVIDER_TABLE = _ProviderProxy()

PROVIDERS = [(k, PROVIDER_TABLE[k]["label"]) for k in ("discord",)]








class OnlineError(Exception):
    pass






def entitlement_repo(cfg=None):
    if _DEV_REPO:
        return _DEV_REPO
    return build_secrets.entitlement_repo()


def entitlement_branch(cfg=None):
    raw = build_secrets.entitlement_branch() or "live"
    return _safe_url_segment(raw, "live")


def _safe_url_segment(value, fallback):
    v = str(value or "").strip().strip("/")
    if not v:
        return fallback

    for ch in ("/", "?", "#", "@", ".."):
        if ch in v:
            log("entitlement branch %r contains %r - using %r."
                % (value, ch, fallback))
            return fallback
    return v


def entitlement_pubkey(cfg=None):
    return build_secrets.entitlement_pubkey()


def entitlement_url(cfg=None):
    return "https://raw.githubusercontent.com/%s/%s/%s" % (
        entitlement_repo(cfg), entitlement_branch(cfg), ENTITLEMENT_PATH)


def payment_link(cfg=None, kind="monthly"):
    if kind == "trial":
        return build_secrets.stripe_trial_link()
    return build_secrets.stripe_monthly_link()


def build_is_ready():
    return build_secrets.describe_readiness()


def configured(cfg=None):
    return bool(entitlement_repo(cfg) and entitlement_pubkey(cfg)
                and build_secrets.discord_client_id())


def _http(url, data=None, headers=None, timeout=HTTP_TIMEOUT,
          form=None):
    hdrs = {"Accept": "application/json",
            "User-Agent": "SnappyAI/" + APP_VERSION}
    hdrs.update(headers or {})
    body = None
    if form is not None:
        body = urllib.parse.urlencode(
            {k: v for k, v in form.items() if v is not None}).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = data
    req = urllib.request.Request(url, data=body, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read()
        except Exception:
            return exc.code, b""
    except Exception as exc:
        raise OnlineError("%r" % (exc,))


def _json(status, body):
    try:
        return status, json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        raise OnlineError("non-JSON answer (HTTP %d)" % status)






def _dpapi_protect(blob):
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wt.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        inp = DATA_BLOB(len(blob),
                        ctypes.create_string_buffer(blob, len(blob)))
        out = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptProtectData(
                ctypes.byref(inp), "SnappyAI session", None, None, None,
                0, ctypes.byref(out)):
            return None
        data = ctypes.string_at(out.pbData, out.cbData)
        ctypes.windll.kernel32.LocalFree(out.pbData)
        return data
    except Exception:
        return None


def _dpapi_unprotect(blob):
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wt.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        inp = DATA_BLOB(len(blob),
                        ctypes.create_string_buffer(blob, len(blob)))
        out = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(inp), None, None, None, None, 0,
                ctypes.byref(out)):
            return None
        data = ctypes.string_at(out.pbData, out.cbData)
        ctypes.windll.kernel32.LocalFree(out.pbData)
        return data
    except Exception:
        return None


def secure_dir():
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA")
        if base:
            cand = os.path.join(base, "SnappyAI")
        else:
            cand = os.path.join(os.path.expanduser("~"), "AppData",
                                "Local", "SnappyAI")
    else:
        base = os.environ.get("XDG_DATA_HOME")
        cand = os.path.join(base, "snappyai") if base else os.path.join(
            os.path.expanduser("~"), ".local", "share", "snappyai")
    try:
        os.makedirs(cand, exist_ok=True)
        probe = os.path.join(cand, ".write_test")
        with open(probe, "w") as fh:
            fh.write("x")
        os.remove(probe)
        return cand
    except OSError:
        return config_dir()


def _session_path():
    return os.path.join(secure_dir(), SESSION_FILE)


def load_session():
    try:
        with open(_session_path(), "rb") as fh:
            wrap = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    try:
        raw = base64.b64decode(wrap.get("data", ""))
        if wrap.get("prot") == "dpapi":
            raw = _dpapi_unprotect(raw)
            if raw is None:
                return None
        sess = json.loads(raw.decode("utf-8"))
        return sess if isinstance(sess, dict) else None
    except Exception:
        return None


def save_session(sess):
    raw = json.dumps(sess).encode("utf-8")
    prot = "b64"
    blob = _dpapi_protect(raw)


    if blob is not None and _dpapi_unprotect(blob) == raw:
        raw, prot = blob, "dpapi"
    else:
        if blob is not None:
            log("session.json: DPAPI round-trip failed - storing "
                "obfuscated, not encrypted.")
        else:
            log("session.json: DPAPI unavailable - storing obfuscated, "
                "not encrypted (same guarantee as a browser cookie).")
    try:
        with open(_session_path(), "wb") as fh:
            fh.write(json.dumps({"v": 2, "prot": prot,
                                 "data": base64.b64encode(raw).decode()})
                     .encode("utf-8"))
        return True
    except OSError as exc:
        log("session.json: could not save (%r)" % (exc,))
        return False


def clear_session():
    try:
        os.remove(_session_path())
    except OSError:
        pass


def identity_key(identity):
    identity = identity or {}
    return "%s:%s" % (str(identity.get("provider", "")).lower(),
                      str(identity.get("sub", "")))


def identity_label(identity):
    identity = identity or {}
    return (str(identity.get("email") or "").strip()
            or str(identity.get("name") or "").strip()
            or identity_key(identity))






AUTH_BROWSER_PROFILE = "authbrowser"


















FAST_EXIT_SECONDS = 3.0
LAUNCH_FAIL_OPEN_AFTER = 1.0
UNVERIFIED_OPEN_AFTER = 12.0
CANCEL_GRACE_SECONDS = 2.0
PROFILES_TO_KEEP = 2


def _auth_profile_dir():
    return os.path.join(secure_dir(), AUTH_BROWSER_PROFILE)


_KNOWN_BROWSER_PATHS = (
    ("msedge", ("ProgramFiles(x86)", r"Microsoft\Edge\Application\msedge.exe"),
     ("ProgramFiles", r"Microsoft\Edge\Application\msedge.exe"),
     ("LocalAppData", r"Microsoft\Edge\Application\msedge.exe")),
    ("chrome", ("LocalAppData", r"Google\Chrome\Application\chrome.exe"),
     ("ProgramFiles", r"Google\Chrome\Application\chrome.exe"),
     ("ProgramFiles(x86)", r"Google\Chrome\Application\chrome.exe")),
    ("brave", ("LocalAppData", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
     ("ProgramFiles", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
     ("ProgramFiles(x86)", r"BraveSoftware\Brave-Browser\Application\brave.exe")),
    ("vivaldi", ("LocalAppData", r"Vivaldi\Application\vivaldi.exe"),
     ("ProgramFiles", r"Vivaldi\Application\vivaldi.exe"),
     ("ProgramFiles(x86)", r"Vivaldi\Application\vivaldi.exe")),
    ("firefox", ("ProgramFiles", r"Mozilla Firefox\firefox.exe"),
     ("ProgramFiles(x86)", r"Mozilla Firefox\firefox.exe"),
     ("LocalAppData", r"Mozilla\firefox\firefox.exe")),
)
_BROWSER_ORDER = ("msedge", "chrome", "brave", "vivaldi", "firefox")
_BROWSER_EXES = ("msedge.exe", "chrome.exe", "brave.exe", "vivaldi.exe",
                 "firefox.exe")




_PROGID_KINDS = (("MSEdgeHTM", "msedge"), ("ChromeHTML", "chrome"),
                 ("BraveHTML", "brave"), ("Vivaldi", "vivaldi"),
                 ("FirefoxURL", "firefox"), ("FirefoxHTML", "firefox"))





_FIREFOX_USER_JS = """\
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("trailhead.firstrun.didSeeAboutWelcome", true);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("toolkit.startup.max_resumed_crashes", -1);
user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("app.update.auto", false);
"""


def _winreg_views():
    try:
        import winreg
    except Exception:
        return None, ()
    views = []
    for flag in (getattr(winreg, "KEY_WOW64_64KEY", 0x0100),
                 getattr(winreg, "KEY_WOW64_32KEY", 0x0200)):
        views.append(winreg.KEY_READ | flag)
    views.append(winreg.KEY_READ)
    return winreg, views


def _app_paths_browser(exe_name):
    winreg, views = _winreg_views()
    if winreg is None:
        return None
    sub = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\\"
           + exe_name)
    for view in views:
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                key = winreg.OpenKey(hive, sub, 0, view)
                val = winreg.QueryValue(key, None)
                winreg.CloseKey(key)
            except OSError:
                continue
            if val and os.path.exists(val):
                return val
    return None


def _exe_from_command(cmd):
    cmd = (cmd or "").strip()
    if not cmd:
        return None
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        return cmd[1:end] if end > 1 else None
    idx = cmd.lower().find(".exe")
    return cmd[:idx + 4] if idx > 0 else None


def _default_browser_exe():
    winreg, views = _winreg_views()
    if winreg is None:
        return None
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\Shell\Associations"
            r"\UrlAssociations\https\UserChoice", 0, winreg.KEY_READ)
        progid = str(winreg.QueryValueEx(key, "ProgId")[0])
        winreg.CloseKey(key)
    except OSError:
        return None
    kind = None
    for prefix, name in _PROGID_KINDS:
        if progid.lower().startswith(prefix.lower()):
            kind = name
            break
    if kind is None:
        return None
    for view in views:
        try:
            key = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                                 progid + r"\shell\open\command", 0, view)
            cmd = winreg.QueryValue(key, "")
            winreg.CloseKey(key)
        except OSError:
            continue
        exe = _exe_from_command(cmd)
        if exe and os.path.exists(exe):
            return kind, exe
    return None


def _find_auth_browser():
    if not IS_WINDOWS:
        return None
    import shutil
    found = {}
    for exe in _BROWSER_EXES:
        val = _app_paths_browser(exe)
        if val:
            found.setdefault(exe.split(".")[0], val)
    for kind, *locs in _KNOWN_BROWSER_PATHS:
        if kind in found:
            continue
        for envvar, sub in locs:
            base = os.environ.get(envvar, "")
            if not base:
                continue
            cand = os.path.join(base, sub)
            if os.path.exists(cand):
                found[kind] = cand
                break
    for kind in _BROWSER_ORDER:
        if kind in found:
            return kind, found[kind]
    default = _default_browser_exe()
    if default:
        return default
    for kind in _BROWSER_ORDER:
        which = shutil.which(kind)
        if which:
            return kind, which
    return None














CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_DEFAULT_ERROR_MODE = 0x04000000
_SPAWN_FLAGS = (CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
                | CREATE_DEFAULT_ERROR_MODE)
_NO_WINDOW_FLAGS = 0x00000001
_SW_HIDE = 0


class _StartupInfo:
    __slots__ = ("dwFlags", "wShowWindow")

    def __init__(self):
        self.dwFlags = 0
        self.wShowWindow = 0


def _hidden_spawn_kwargs():
    if not IS_WINDOWS:
        return {}
    try:
        import subprocess
        si = getattr(subprocess, "STARTUPINFO", _StartupInfo)()
        si.dwFlags |= _NO_WINDOW_FLAGS
        si.wShowWindow = _SW_HIDE
        return {"creationflags": _SPAWN_FLAGS, "startupinfo": si,
                "close_fds": True}
    except Exception:
        return {"creationflags": _SPAWN_FLAGS}


def _gui_spawn_flags():
    if not IS_WINDOWS:
        return 0
    return CREATE_NO_WINDOW | CREATE_DEFAULT_ERROR_MODE


def _open_url(url):
    url = str(url or "").strip()
    if not url:
        return False
    if IS_WINDOWS:


        try:
            os.startfile(url)
            return True
        except Exception:
            pass
        try:
            import ctypes
            shell32 = ctypes.windll.shell32
            shell32.ShellExecuteW.restype = ctypes.c_void_p
            shell32.ShellExecuteW.argtypes = [ctypes.c_void_p,
                                              ctypes.c_wchar_p,
                                              ctypes.c_wchar_p,
                                              ctypes.c_void_p,
                                              ctypes.c_wchar_p, ctypes.c_int]
            rc = shell32.ShellExecuteW(None, "open", url, None, None, 5)
            if isinstance(rc, int) and rc > 32:
                return True
        except Exception:
            pass
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def _kill_browser_tree(proc):
    if proc is None:
        return
    if IS_WINDOWS:
        if _win32_kill_tree(proc.pid):
            log("Sign-in: window closed (process tree terminated in-process "
                "- no console window involved).")
            _reap(proc)
            return
        log("Sign-in: could not enumerate the process tree; falling back to "
            "a hidden taskkill.")
        try:
            import subprocess
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, **_hidden_spawn_kwargs())
            _reap(proc)
            return
        except Exception:
            pass
    for stop in (proc.terminate, proc.kill):
        try:
            stop()
            proc.wait(timeout=2)
            return
        except Exception:
            continue


def _reap(proc):
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


def _win32_kill_tree(root_pid):
    if not IS_WINDOWS:
        return False
    snap = _process_snapshot()
    if not snap:
        return False
    me = os.getpid()
    pids = [p for p in _process_tree(snap, root_pid) if p and p != me]
    if not pids:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                    wintypes.DWORD]
        k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
    except Exception:
        return False
    killed = False
    for pid in pids:
        try:
            handle = k32.OpenProcess(0x0001, False, int(pid))
            if not handle:
                continue
            try:
                if k32.TerminateProcess(handle, 1):
                    killed = True
            finally:
                k32.CloseHandle(handle)
        except Exception:
            continue
    return killed


def _process_snapshot():
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    def _as_bool(value):



        return value is True or (isinstance(value, int) and value != 0)

    try:
        k32 = ctypes.windll.kernel32
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD,
                                                wintypes.DWORD]





        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.Process32FirstW.restype = wintypes.BOOL
        k32.Process32FirstW.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.restype = wintypes.BOOL
        k32.Process32NextW.argtypes = [ctypes.c_void_p,
                                       ctypes.POINTER(PROCESSENTRY32W)]
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)
        if (not isinstance(snap, int) or not snap
                or snap in (0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF)):
            return None
        table = {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = _as_bool(k32.Process32FirstW(snap, ctypes.byref(entry)))
            walked = 0
            while ok and walked < 20000:
                walked += 1
                try:
                    table[int(entry.th32ProcessID)] = (
                        str(entry.szExeFile).lower(),
                        int(entry.th32ParentProcessID))
                except (TypeError, ValueError):
                    pass
                ok = _as_bool(k32.Process32NextW(snap, ctypes.byref(entry)))
        finally:
            k32.CloseHandle(snap)
        return table or None
    except Exception:
        return None


def _process_tree(snapshot, root_pid):
    try:
        root = int(root_pid)
    except (TypeError, ValueError):
        return []
    if not snapshot or root not in snapshot:
        return [root]
    order, seen, level = [], {root}, [root]
    while level and len(order) < 512:
        nxt = []
        for pid in level:
            for cand, entry in snapshot.items():
                if (entry[1] == pid and cand not in seen
                        and cand != pid and cand != root):
                    seen.add(cand)
                    nxt.append(cand)
                    order.append(cand)
        level = nxt
    order.reverse()
    order.append(root)
    return order


def _browser_pids(exe_path):
    snap = _process_snapshot()
    if not snap:
        return None


    want = str(exe_path or "").lower().replace("\\", "/").rsplit("/", 1)[-1]
    return {pid for pid, entry in snap.items() if entry[0] == want}


def _prune_profiles(root, keep):
    import shutil
    try:
        dirs = [os.path.join(root, n) for n in os.listdir(root)]
        dirs = [d for d in dirs if d != keep and os.path.isdir(d)]
        dirs.sort(key=lambda d: os.path.getmtime(d))
    except OSError:
        return
    for stale in dirs[:max(0, len(dirs) - PROFILES_TO_KEEP)]:
        shutil.rmtree(stale, ignore_errors=True)


def _fresh_profile_dir(kind):
    root = _auth_profile_dir()
    prof = os.path.join(root, "%s-%s" % (kind, secrets.token_hex(4)))
    try:
        os.makedirs(prof, exist_ok=True)
    except OSError:
        return os.path.join(root, kind)
    try:
        if kind == "firefox":
            with open(os.path.join(prof, "user.js"), "w",
                      encoding="utf-8") as fh:
                fh.write(_FIREFOX_USER_JS)
        else:
            default = os.path.join(prof, "Default")
            os.makedirs(default, exist_ok=True)
            with open(os.path.join(prof, "First Run"), "w") as fh:
                fh.write("")
            with open(os.path.join(default, "Preferences"), "w",
                      encoding="utf-8") as fh:
                json.dump({"profile": {"exit_type": "Normal",
                                       "exited_cleanly": True}}, fh)
    except OSError:
        pass
    _prune_profiles(root, prof)
    return prof


class _Launch:
    __slots__ = ("proc", "label", "exe", "before")

    def __init__(self, proc, label, exe, before):
        self.proc = proc
        self.label = label
        self.exe = exe
        self.before = before


def _spawn_auth_browser(url):
    found = _find_auth_browser()
    if found is None:
        log("Sign-in: no spawnable browser found (registry App Paths, "
            "install folders and the default https handler were all "
            "empty).")
        return None
    kind, path = found
    before = _browser_pids(path)
    prof = _fresh_profile_dir(kind)
    try:
        import subprocess
        if kind == "firefox":
            args = [path,
                    "-no-remote",
                    "-new-instance",
                    "--wait-for-browser",
                    "-profile", prof,
                    "-new-window", url]
        else:
            args = [path, "--user-data-dir=" + prof, "--no-first-run",
                    "--no-default-browser-check", "--disable-sync",
                    "--disable-session-crashed-bubble",
                    "--hide-crash-restore-bubble", "--app=" + url]
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=_gui_spawn_flags())
        return _Launch(proc, "%s (%s)" % (kind, path), path, before)
    except Exception as exc:
        log("Sign-in: %s did not start (%r)." % (kind, exc))
        return None



def _redirect_uri():
    return "http://%s:%d%s" % (LOOPBACK_HOST, LOOPBACK_PORT, LOOPBACK_PATH)


def _exchange_code(provider, code, verifier=None):
    p = PROVIDER_TABLE[provider]

    def _post(use_verifier):
        return _json(*_http(p["token"], form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri(),
            "client_id": p["client_id"],
            "client_secret": p["client_secret"],
            "code_verifier": verifier if use_verifier else None,
        }, headers={"Accept": "application/json"}))

    status, tok = _post(bool(verifier))
    if verifier and (status != 200 or "access_token" not in tok):
        log("Sign-in: the token endpoint rejected the PKCE verifier "
            "(HTTP %d); retrying without it. If this repeats, set pkce=False "
            "for %r in the provider table." % (status, provider))
        status, tok = _post(False)
    if status != 200 or "access_token" not in tok:
        raise OnlineError("token exchange failed (HTTP %d)" % status)
    return tok


def _refresh(provider, refresh_token):
    p = PROVIDER_TABLE[provider]
    status, tok = _json(*_http(p["token"], form={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": p["client_id"],
        "client_secret": p["client_secret"],
    }, headers={"Accept": "application/json"}))
    if status != 200 or "access_token" not in tok:
        return None
    return tok


def _userinfo(provider, access_token):
    p = PROVIDER_TABLE[provider]
    status, user = _json(*_http(
        p["userinfo"], headers={"Authorization": "Bearer %s" % access_token,
                                "User-Agent": "SnappyAI/" + APP_VERSION}))
    if status != 200:
        return None
    sub, email, name = p["identity"](user)
    if not sub:
        return None
    return {"provider": provider, "sub": str(sub),
            "email": email or None, "name": name or None}


def _token_expiry(tok):
    try:
        return time.time() + float(tok.get("expires_in", 0)) - 60
    except (TypeError, ValueError):
        return 0.0






def _canonical_bytes(doc):
    return json.dumps({k: v for k, v in doc.items() if k != "sig"},
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


CACHE_BUCKET_SECONDS = 30


def fetch_entitlements(cfg, fresh=False):
    if not configured(cfg):
        raise OnlineError("entitlement_repo / entitlement_pubkey are "
                          "not configured (see GITHUB-BACKEND.md)")
    if fresh:
        qs = "?ts=%d" % int(time.time())
    else:
        qs = "?cb=%d" % (int(time.time()) // CACHE_BUCKET_SECONDS)
    status, body = _http(entitlement_url(cfg) + qs)
    if status == 404:
        raise OnlineError("entitlements.json not found in %s"
                          % entitlement_repo(cfg))
    if status != 200:
        raise OnlineError("GitHub raw answered HTTP %d" % status)
    try:
        doc = json.loads(body.decode("utf-8"))
    except ValueError:
        raise OnlineError("entitlements.json is not valid JSON")
    try:
        pub = base64.b64decode(entitlement_pubkey(cfg))
        sig = base64.b64decode(str(doc.get("sig", "")))
    except Exception:
        raise OnlineError("entitlement_pubkey / sig undecodable")
    if not edsig.verify(pub, _canonical_bytes(doc), sig):
        raise OnlineError("entitlements.json signature INVALID - "
                          "refusing an untrusted file")
    try:
        gen = int(doc.get("generated_at", 0) or 0)
    except (TypeError, ValueError):
        raise OnlineError("entitlements.json has a bad generated_at")
    age = time.time() - gen
    if age > FRESHNESS_SECONDS:
        raise OnlineError(
            "entitlements.json is stale (published %s UTC, %d h %02d m ago; "
            "limit %d h) - the publisher pipeline is down. Run the "
            "'entitlements' workflow in the private controller repo "
            "(Actions -> entitlements -> Run workflow); see "
            "GITHUB-BACKEND.md section 12."
            % (time.strftime("%Y-%m-%d %H:%M", time.gmtime(gen)),
               int(age) // 3600, (int(age) % 3600) // 60,
               FRESHNESS_SECONDS // 3600))
    return doc.get("rows") or {}, gen


def _row_to_ent(row, now):
    if not isinstance(row, dict):
        return {"active": False, "status": "unsubscribed", "plan": None,
                "until": 0}
    try:
        until = float(row.get("until", 0) or 0)
    except (TypeError, ValueError):
        until = 0
    status = str(row.get("status", "unsubscribed"))
    active = until > now and status in ("trial", "active", "grant",
                                        "canceling")
    return {"active": active, "status": status,
            "plan": row.get("plan"), "until": until,
            "shared": bool(row.get("shared", False))}


IDENTITY_REFRESH_SECONDS = 6 * 3600


def me(cfg, fresh=False):
    sess = load_session()
    if not sess or not sess.get("provider"):
        return None, None
    provider = sess["provider"]
    p = PROVIDER_TABLE.get(provider)
    if p is None:
        return None, None
    access = sess.get("access_token")
    try:
        _exp = float(sess.get("expires_at") or 0)
    except (TypeError, ValueError):
        _exp = 0.0
    if not access or (_exp and time.time() > _exp):
        if not (p["refresh"] and sess.get("refresh_token")):
            return None, None
        tok = _refresh(provider, sess["refresh_token"])
        if tok is None:
            return None, None
        sess["access_token"] = tok["access_token"]
        sess["refresh_token"] = tok.get("refresh_token",
                                        sess.get("refresh_token"))
        sess["expires_at"] = _token_expiry(tok)
        save_session(sess)
        access = sess["access_token"]

    cached = sess.get("identity")
    cached_ok = (isinstance(cached, dict) and cached.get("sub")
                 and cached.get("provider") == provider)
    try:
        age = time.time() - float(sess.get("identity_at", 0) or 0)
    except (TypeError, ValueError):
        age = float("inf")
    need_refresh = not cached_ok or age > IDENTITY_REFRESH_SECONDS

    if need_refresh:


        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            fut_id = pool.submit(_safe_userinfo, provider, sess["access_token"])
            fut_rows = pool.submit(fetch_entitlements, cfg, fresh)
            fresh_ident = fut_id.result()
            rows, _gen = fut_rows.result()
        if fresh_ident is not None:
            sess["identity"] = fresh_ident
            sess["identity_at"] = time.time()
            save_session(sess)
            cached = fresh_ident
        elif not cached_ok:


            raise OnlineError("could not confirm the sign-in with %s "
                              "(no cached identity)" % provider)
        else:
            log("Identity re-validation failed - keeping the cached "
                "identity (the entitlement file is the access control).")
    else:
        rows, _gen = fetch_entitlements(cfg, fresh)

    row, _used = lookup_row(rows, cached)
    ent = _row_to_ent(row, time.time())




    idh = _used or row_hash(cached)
    ent["shared"] = row_shared(rows, idh, _gen)
    return cached, ent


def _safe_userinfo(provider, access_token):
    try:
        return _userinfo(provider, access_token)
    except OnlineError as exc:
        log("userinfo(%s) failed: %s" % (provider, exc))
        return None


def entitlement_active(ent, now=None):
    if not isinstance(ent, dict):
        return False
    if not ent.get("active"):
        return False
    try:
        until = float(ent.get("until", 0) or 0)
    except (TypeError, ValueError):
        return False
    now = now if now is not None else time.time()
    return until > now


def entitlement_key(identity):
    identity = identity or {}
    email = str(identity.get("email") or "").strip().lower()
    return ("email:" + email) if email else identity_key(identity)


def _hash_key(key, pepper):
    return hashlib.sha256((pepper + key).encode("utf-8")).hexdigest()


def row_hash(identity):
    return _hash_key(entitlement_key(identity), build_secrets.row_pepper())


def row_hash_legacy(identity):
    return _hash_key(entitlement_key(identity), "")


def row_hashes(identity):
    out = [row_hash(identity)]
    legacy = row_hash_legacy(identity)
    if legacy not in out:
        out.append(legacy)
    return out


def lookup_row(rows, identity):
    if not isinstance(rows, dict):
        return None, None
    for h in row_hashes(identity):
        row = rows.get(h)
        if row is not None:
            if h == row_hash_legacy(identity) and build_secrets.row_pepper():
                log("Entitlement row matched the LEGACY unsalted hash - the "
                    "publisher has not been rotated to the peppered key yet.")
            return row, h
    return None, None


_STATUS_KEYS = {
    "active": "ent.st.active",
    "trial": "ent.st.trial",
    "grant": "ent.st.grant",
    "canceling": "ent.st.canceling",
    "past_due": "ent.st.past_due",
    "canceled": "ent.st.canceled",
    "refunded": "ent.st.refunded",
    "unsubscribed": "ent.st.unsubscribed",
}


def entitlement_summary(ent, now=None):
    if not isinstance(ent, dict) or not ent:
        return i18n.L(_STATUS_KEYS["unsubscribed"])
    now = time.time() if now is None else now
    st = str(ent.get("status", "unsubscribed"))
    try:
        until = float(ent.get("until") or 0)
    except (TypeError, ValueError):
        until = 0
    day = time.strftime("%Y-%m-%d", time.gmtime(until)) if until else ""
    plan = str(ent.get("plan") or "")
    if st in ("active", "trial", "grant") and until > now:


        label = i18n.L(_STATUS_KEYS[st])
        show_plan = plan and plan.lower() not in label.lower()
        return "%s%s%s" % (label,
                           i18n.L("ent.plan", plan) if show_plan else "",
                           i18n.L("ent.until", day) if day else "")
    if st == "canceling" and until > now:
        return i18n.L("ent.cancel_line", day)
    if st == "past_due":
        return i18n.L("ent.past_due_line")
    if st == "canceled":
        return i18n.L("ent.canceled_line", " on %s" % day if day else "")
    if st == "refunded":
        return i18n.L("ent.refunded_line")
    if until and until <= now:
        return i18n.L("ent.expired_line", day)
    return i18n.L(_STATUS_KEYS.get(st, "ent.st.unsubscribed"))


def subscribe_url(cfg, identity, kind="monthly"):
    link = payment_link(cfg, kind)
    if not link:
        return ""
    sep = "&" if "?" in link else "?"
    email = str(identity.get("email") or "").strip().lower()
    out = "%s%sclient_reference_id=%s" % (
        link, sep, urllib.parse.quote(identity_key(identity), safe=""))
    if email:
        out += "&prefilled_email=%s" % urllib.parse.quote(email, safe="")
    return out










DEFAULT_DISPATCH_PAT = ""


def dispatch_pat(cfg=None):
    import os as _os
    if not getattr(sys, "frozen", False):
        env = _os.environ.get("SNAPPYAI_DISPATCH_PAT", "").strip()
        if env:
            return env
    try:
        import general_utils as _bs
        _v = _bs.dispatch_pat() if hasattr(_bs, "dispatch_pat") else ""
        if _v:
            return _v
    except Exception:
        pass
    return DEFAULT_DISPATCH_PAT


_NUDGE_MIN_SECONDS = 60.0
_last_nudge = [0.0]


def nudge_publisher(cfg=None):
    """Ask the controller workflow to republish now.

    Dev builds only: a shipped exe never carries a dispatch PAT, and this
    refuses to run frozen so a customer cannot trigger workflows. Throttled
    to one dispatch per minute because it sits on the manual re-check path.
    """
    if getattr(sys, "frozen", False):
        return False
    now = time.monotonic()
    if now - _last_nudge[0] < _NUDGE_MIN_SECONDS:
        return False
    if not dispatch_pat(cfg):
        return False
    _last_nudge[0] = now
    ok = dispatch_publish(cfg)
    log("Publisher nudge %s." % ("accepted" if ok else "refused"))
    return ok


def dispatch_publish(cfg=None):
    pat = dispatch_pat(cfg)
    if not pat:
        return False
    repo = entitlement_repo(cfg)
    owner = repo.split("/")[0] if "/" in repo else ""
    if not owner:
        return False
    url = "https://api.github.com/repos/%s/actions/workflows/entitlements.yml/dispatches" % repo
    body = json.dumps({"ref": "main"}).encode()
    try:
        status, _ = _http(url, data=body, timeout=10, headers={
            "Authorization": "Bearer %s" % pat,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json"})
        return status in (200, 202, 204)
    except OnlineError:
        return False






































SHARED_CONFIRM_SNAPSHOTS = 2


class _SharedGuard(object):

    def __init__(self):
        self._lock = threading.Lock()
        self._seen = {}

    def observe(self, idh, raw_shared, gen):
        if not idh:
            return False
        with self._lock:
            st = self._seen.setdefault(idh, {"gens": [], "clean": 0})
            if not raw_shared:
                st["gens"] = []
                st["clean"] += 1
                return False
            if gen and gen not in st["gens"]:
                st["gens"].append(gen)
                st["gens"] = st["gens"][-SHARED_CONFIRM_SNAPSHOTS - 1:]
            st["clean"] = 0
            confirmed = len(st["gens"]) >= SHARED_CONFIRM_SNAPSHOTS
            if not confirmed and st["gens"]:
                log("Entitlement file reports this account as in use "
                    "elsewhere (generation %s). Not acting yet: a single "
                    "sighting can be a forged heartbeat, so this needs %d "
                    "consecutive signed generations."
                    % (gen, SHARED_CONFIRM_SNAPSHOTS))
            return confirmed

    def reset(self, idh=None):
        with self._lock:
            if idh is None:
                self._seen.clear()
            else:
                self._seen.pop(idh, None)


SHARED_GUARD = _SharedGuard()


def presence_webhook(cfg=None):
    return build_secrets.presence_webhook()


def row_shared(rows, idh, gen):
    row = rows.get(idh) if isinstance(rows, dict) else None
    raw = bool((row or {}).get("shared", False))
    return SHARED_GUARD.observe(idh, raw, gen)


def presence_interval_seconds(cfg=None):
    return PRESENCE_HEARTBEAT_MINUTES * 60


def post_presence(cfg, identity_hash, nonce, bye=False):
    url = presence_webhook(cfg)
    if not url:
        return False
    payload = json.dumps({"k": identity_hash, "n": nonce,
                          "t": int(time.time()),
                          "bye": bool(bye), "v": 1})
    body = json.dumps({"content": payload,
                       "allowed_mentions": {"parse": []}}).encode()
    try:
        status, _ = _http(url, data=body, timeout=5,
                          headers={"Content-Type": "application/json"})
        return 200 <= status < 300
    except OnlineError:
        return False


_EMAIL_IN_URL = re.compile(r"(prefilled_email=|client_reference_id=)[^&]*")


def redact_url(url):
    return _EMAIL_IN_URL.sub(r"\1<redacted>", str(url or ""))


def open_subscribe(cfg, identity, kind="monthly"):
    url = subscribe_url(cfg, identity, kind)
    log("Opening Stripe: %s" % redact_url(url))
    try:
        return _open_url(url)
    except Exception:
        return False






PING_PATH = "/__snappy_ping"
PING_TOKEN = "snappyai-loopback"


class _Attempt:
    __slots__ = ("state", "result", "done", "live")

    def __init__(self, state):
        self.state = state
        self.result = None
        self.done = threading.Event()
        self.live = True


_ATTEMPT = None
_ATTEMPT_LOCK = threading.Lock()


class _OAuthReturn(BaseHTTPRequestHandler):
    def _send(self, body, code=200, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == PING_PATH:
            self._send(PING_TOKEN, ctype="text/plain; charset=utf-8")
            return
        if parsed.path != LOOPBACK_PATH:
            self._send(_PAGE_FAIL % _esc("wrong address"), code=404)
            return
        qs = urllib.parse.parse_qs(parsed.query)
        state = (qs.get("state") or [""])[0]
        with _ATTEMPT_LOCK:
            att = _ATTEMPT




            if att is None or not att.live or state != att.state:
                att = None
        if att is None:


            self._send(_PAGE_STALE)
            return
        code = (qs.get("code") or [""])[0]
        err = (qs.get("error_description") or qs.get("error") or [""])[0]
        if err:
            att.result = ("error", err, None)
            self._send(_PAGE_FAIL % _esc(err))
        elif not code:
            att.result = ("error", "the provider returned no code", None)
            self._send(_PAGE_FAIL % _esc("the provider returned no code"))
        else:
            att.result = ("ok", code, None)
            self._send(_PAGE_OK)
        att.done.set()

    def log_message(self, *args):
        pass


_CLOSE_SCRIPT = """<script>
/* The app has everything it needs the instant this page loads. A window
   WE started is removed by the app itself; this covers the fallback
   (the system browser), where a page may only close a window it opened
   - so it also blanks itself and titles the tab as a receipt rather
   than leaving an error behind. */
document.title = "\\u2713 Snappy AI";
try { window.close(); } catch (e) {}
setTimeout(function () {
  document.body.innerHTML = "";
  try { window.close(); } catch (e) {}
}, 700);
</script>"""

_PAGE_OK = ("""<html><head><title>\u2713 Snappy AI</title></head>"""
            """<body style="font-family:Segoe UI,Arial,sans-serif;"""
            """background:#1e2026;color:#e9eaee;text-align:center;"""
            """padding-top:18vh"><h2>Snappy AI</h2>"""
            """<p>Signed in - returning to the app...</p>"""
            + _CLOSE_SCRIPT + """</body></html>""")

_PAGE_FAIL = ("""<html><head><title>Snappy AI</title></head>"""
              """<body style="font-family:Segoe UI,Arial,sans-serif;"""
              """background:#1e2026;color:#e9eaee;text-align:center;"""
              """padding-top:18vh"><h2>Snappy AI</h2>"""
              """<p>Sign-in failed: %s</p>"""
              """<p style="opacity:0.6">Close this window and try """
              """again in the app.</p></body></html>""")

_PAGE_STALE = ("""<html><head><title>Snappy AI</title></head>"""
               """<body style="font-family:Segoe UI,Arial,sans-serif;"""
               """background:#1e2026;color:#e9eaee;text-align:center;"""
               """padding-top:18vh"><h2>Snappy AI</h2>"""
               """<p>This sign-in window is no longer needed - the app """
               """stopped waiting for it.</p>"""
               """<p style="opacity:0.6">You can close it. To sign in, """
               """start again from the app.</p></body></html>""")


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


_HTTPD = None
_HTTPD_LOCK = threading.Lock()


def _probe_listener():
    url = "http://%s:%d%s" % (LOOPBACK_HOST, LOOPBACK_PORT, PING_PATH)
    for _ in range(12):
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                body = resp.read(64).decode("utf-8", "replace")
            return body == PING_TOKEN
        except Exception:
            time.sleep(0.15)
    return False


def _loopback_listener():
    global _HTTPD
    with _HTTPD_LOCK:
        if _HTTPD is not None:
            return _HTTPD
        httpd, last = None, None
        for _ in range(6):
            try:
                httpd = ThreadingHTTPServer((LOOPBACK_HOST, LOOPBACK_PORT),
                                            _OAuthReturn)
                break
            except OSError as exc:
                last = exc
                time.sleep(0.3)
        if httpd is None:
            raise OnlineError(
                "cannot listen on %s:%d (%r) - another program (or "
                "another Snappy AI) is using that port. Close it and "
                "try again." % (LOOPBACK_HOST, LOOPBACK_PORT, last))
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True,
                         name="oauth-loopback").start()
        if not _probe_listener():
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass
            raise OnlineError(
                "port %d is taken by another program, which answers "
                "instead of Snappy AI. Close that program (or the other "
                "Snappy AI) and try again." % LOOPBACK_PORT)
        _HTTPD = httpd
        log("Sign-in: loopback listener ready on http://%s:%d%s"
            % (LOOPBACK_HOST, LOOPBACK_PORT, LOOPBACK_PATH))
        return httpd


def close_loopback():
    global _HTTPD
    with _HTTPD_LOCK:
        httpd, _HTTPD = _HTTPD, None
    if httpd is None:
        return
    try:
        httpd.shutdown()
        httpd.server_close()
    except Exception:
        pass


def login_with_provider(cfg, provider, timeout=LOGIN_TIMEOUT,
                        opener=None):
    p = PROVIDER_TABLE.get(provider)
    if p is None:
        raise OnlineError("unknown provider %r" % (provider,))
    if not p["client_id"]:
        raise OnlineError("provider %r has no client_id yet - paste the "
                          "OAuth app credentials into online.py "
                          "PROVIDER_TABLE (GITHUB-BACKEND.md §3), or set "
                          "SNAPPYAI_OAUTH_%s_CLIENT_ID/_SECRET for a dev "
                          "run without rebuilding"
                          % (provider, provider.upper()))
    if not configured(cfg):
        raise OnlineError("entitlement_repo / entitlement_pubkey are "
                          "not configured (see GITHUB-BACKEND.md)")

    _loopback_listener()

    state = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(64) if p["pkce"] else None
    challenge = None
    if verifier:
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    global _ATTEMPT
    att = _Attempt(state)
    with _ATTEMPT_LOCK:
        _ATTEMPT = att

    q = {"client_id": p["client_id"], "redirect_uri": _redirect_uri(),
         "response_type": "code", "scope": p["scope"], "state": state}
    if challenge:
        q["code_challenge"] = challenge
        q["code_challenge_method"] = "S256"
    url = p["auth"] + "?" + urllib.parse.urlencode(q)

    launch = _spawn_auth_browser(url)
    if launch is not None:
        opened = True
        log("Sign-in: dedicated auto-closing sign-in window opened - "
            "%s." % launch.label)
    else:
        log("Sign-in: using your system browser instead (its tab must be "
            "closed by hand; browser policy forbids pages from closing "
            "OS-opened tabs).")
        try:
            opened = (opener or _open_url)(url)
        except Exception:
            opened = False
    if not opened:
        with _ATTEMPT_LOCK:
            if _ATTEMPT is att:
                _ATTEMPT = None
        msg = "Could not open a browser automatically - open this URL by hand:\n    %s" % url
        log(msg)
        return None, msg








    started = time.time()
    cancel_event = threading.Event()
    handed_off = threading.Event()
    launch_failed = threading.Event()
    if launch is not None:
        def _watch():
            try:
                launch.proc.wait()
            except Exception:
                return
            lived = time.time() - started
            if lived >= FAST_EXIT_SECONDS:

                time.sleep(CANCEL_GRACE_SECONDS)
                cancel_event.set()
                return
            alive = None
            for _ in range(2):
                now = _browser_pids(launch.exe)
                if now is not None:
                    if launch.before is not None:


                        alive = bool(now - launch.before)
                    else:







                        alive = False
                    if alive:
                        break
                time.sleep(0.3)
            if alive:
                handed_off.set()
                log("Sign-in: the process we started exited after %.1fs but "
                    "its window is alive in another process - still "
                    "listening on port %d." % (lived, LOOPBACK_PORT))
            elif alive is None:
                handed_off.set()
                log("Sign-in: that browser process exited after %.1fs and "
                    "we cannot enumerate processes to check its window - "
                    "still listening on port %d." % (lived, LOOPBACK_PORT))
            else:
                launch_failed.set()
                log("Sign-in: %s exited after %.1fs and left no window "
                    "behind." % (launch.label, lived))
        threading.Thread(target=_watch, daemon=True,
                         name="auth-browser-watch").start()

    deadline = started + timeout
    reopened = False
    while time.time() < deadline:
        if att.done.wait(0.2):
            break
        if cancel_event.is_set():
            break
        if not reopened and (launch_failed.is_set()
                             and time.time() - started
                             >= LAUNCH_FAIL_OPEN_AFTER):


            reopened = True
            log("Sign-in: opening the link in your default browser "
                "instead - the sign-in still ends here.")
            try:
                (opener or _open_url)(url)
            except Exception:
                pass
        if not reopened and handed_off.is_set() and time.time() - started \
                >= UNVERIFIED_OPEN_AFTER:


            reopened = True
            log("Sign-in: no window appeared - opening the link in your "
                "default browser; the sign-in still ends here.")
            try:
                (opener or _open_url)(url)
            except Exception:
                pass
    came = att.done.is_set()

    with _ATTEMPT_LOCK:
        att.live = False
        if _ATTEMPT is att:
            _ATTEMPT = None
    if launch is not None and not handed_off.is_set():


        if came:
            time.sleep(0.6)
        _kill_browser_tree(launch.proc)

    # (the single _ATTEMPT cleanup already happened above, next to
    #  att.live = False - a second block here was dead code)
    result = att.result
    if not came or result is None or result[0] != "ok":
        if result and result[0] == "error":
            msg = ("Discord authentication failed: %s - please try "
                   "again." % result[1])
        elif cancel_event.is_set():
            msg = CANCELLED_MSG()
        else:
            msg = ("Discord authentication timed out - please try "
                   "again.")
        log(msg)
        return None, msg

    code = result[1]
    tok = _exchange_code(provider, code, verifier)
    identity = _userinfo(provider, tok["access_token"])
    if identity is None:
        raise OnlineError("provider did not return an identity")
    save_session({
        "provider": provider,
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "expires_at": _token_expiry(tok) if tok.get("expires_in")
        else 0.0,
        "identity": identity,
        "identity_at": time.time(),
    })



    log("Signed in as %s (%s)." % (identity_key(identity), provider))
    return identity, ""
