import os
import sys

if "__compiled__" in globals():
    sys.frozen = True

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))



from system_core import (APP_NAME, IS_WINDOWS, attach_log_file, log,
                  log_path)
from config import DEFAULT_CONFIG, Config


_INSTANCE_MUTEX = None


def _ensure_single_instance():
    if not IS_WINDOWS:
        return True
    global _INSTANCE_MUTEX
    try:
        import ctypes
        import ctypes.wintypes as wt
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = wt.HANDLE
        kernel32.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
        kernel32.GetLastError.restype = wt.DWORD
        kernel32.CloseHandle.argtypes = [wt.HANDLE]
        handle = kernel32.CreateMutexW(None, False,
                                       "SnappyAI_SingleInstance_Mutex")
        if not handle:
            return True
        if kernel32.GetLastError() == 183:
            kernel32.CloseHandle(handle)
            _announce(APP_NAME + " is already running",
                      "Another Snappy AI is already active (one copy "
                      "drives the mouse hooks - two fight over every "
                      "click).\n\n"
                      "Use the running window instead of starting a "
                      "second one.")
            return False
        _INSTANCE_MUTEX = handle
        return True
    except Exception as exc:
        try:
            log("instance guard failed: %r" % (exc,))
        except Exception:
            pass
        return True


def attach_app_log():
    try:
        import online
        base = online.secure_dir()
    except Exception:
        base = None
    if not base:
        try:
            import tempfile
            base = tempfile.gettempdir()
        except Exception:
            return None
    try:
        return attach_log_file(os.path.join(base, "logs", "snappyai.log"))
    except Exception:
        return None


def apply_cli_overrides(cfg, argv):




    if "--mode1" in argv:
        cfg.set("mode", 1, save=False)

    if "--mode2" in argv:
        cfg.set("mode", 2, save=False)

    if "--accent-only" in argv:
        cfg.set("accent_only", True, save=False)

    if "--follow" in argv:
        cfg.set("follow_enabled", True, save=False)

    if "--motion-3d" in argv:
        cfg.set("motion_3d", True, save=False)

    if "--controller" in argv:
        cfg.set("controller_input", True, save=False)

    if "--no-controller-drive" in argv:
        cfg.set("pad_drive", False, save=False)

    if "--no-adaptive" in argv:
        cfg.set("adaptive_threshold", False, save=False)

    if "--no-lumnorm" in argv:
        cfg.set("luminance_norm", False, save=False)

    def _next_value(idx):
        if idx + 1 >= len(argv):
            return None
        nxt = argv[idx + 1]
        if nxt.startswith("--"):
            return None
        return nxt

    for i, arg in enumerate(argv):
        if arg == "--repo":
            nxt = _next_value(i)
            if nxt is None:
                print("Ignored --repo (missing value)")
                continue
            try:
                import online
                online.set_dev_repo(nxt)
            except Exception as exc:
                print("Ignored --repo (%r)" % (exc,))

        elif arg == "--radius":
            nxt = _next_value(i)
            if nxt is None:
                print("Ignored --radius (missing value)")
                continue
            value = nxt.lower()

            if value in ("small", "medium", "large"):
                cfg.set("radius", {"small": 80, "medium": 150,
                                   "large": 250}[value], save=False)
            else:

                try:
                    cfg.set("radius", int(float(value)), save=False)
                except (ValueError, OverflowError):
                    print("Ignored invalid --radius %r" % (argv[i + 1],))

        elif arg == "--capture":
            nxt = _next_value(i)
            if nxt is None:
                print("Ignored --capture (missing value)")
                continue
            value = nxt.lower()

            if value in ("auto", "dxcam", "mss", "wgc"):
                cfg.set("capture_backend", value, save=False)
            else:
                print("Ignored invalid --capture %r" % (argv[i + 1],))

        elif arg == "--merge":
            nxt = _next_value(i)
            if nxt is None:
                print("Ignored --merge (missing value)")
                continue
            try:
                cfg.set("merge_distance", int(float(nxt)), save=False)
            except (ValueError, OverflowError):






                print("Ignored invalid --merge %r" % (argv[i + 1],))

        elif arg == "--accent":
            nxt = _next_value(i)
            if nxt is None:
                print("Ignored --accent (missing value)")
                continue
            cfg.set("accent_color", nxt.strip(), save=False)

        elif arg == "--glide":
            nxt = _next_value(i)
            if nxt is None:
                print("Ignored --glide (missing value)")
                continue
            try:
                cfg.set("glide_ms", int(float(nxt)), save=False)
            except (ValueError, OverflowError):
                print("Ignored invalid --glide %r" % (nxt,))




_KNOWN_FLAGS = ("--mode1", "--mode2", "--accent-only",
                "--follow", "--motion-3d", "--no-adaptive",
                "--no-lumnorm", "--console", "--reset-config", "--help",
                "-h", "--repo", "--radius", "--capture", "--merge", "--accent",
                "--glide", "--controller", "--no-controller-drive")


def _announce(title, body, fatal=False):
    text = "%s\n\n%s" % (title, body)
    try:
        log(text.replace("\n", " | "))
    except Exception:
        pass
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        (messagebox.showerror if fatal else messagebox.showwarning)(
            APP_NAME, text)
        root.destroy()
        return
    except Exception:
        pass
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, text, APP_NAME, (0x10 if fatal else 0x30) | 0x40000)
            return
        except Exception:
            pass
    print(text)


def _crash_guard(exc_type, exc, tb):
    import traceback
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        log("FATAL start-up error:\n" + text)
    except Exception:
        pass
    where = None
    try:
        where = log_path()
    except Exception:
        pass
    body = text.strip().splitlines()
    tail = "\n".join(body[-12:]) if len(body) > 12 else text.strip()
    _announce("%s could not start" % APP_NAME,
              "%s\n\n%s%s" % (
                  tail,
                  "Log file: %s" % where if where else
                  "No log file could be opened.",
                  ""),
              fatal=True)


def warn_unknown_flags(argv):
    for arg in argv[1:]:
        if arg.startswith("--") and arg not in _KNOWN_FLAGS:
            print("Ignored unknown option %r (try --help)." % (arg,))


def print_usage():
    print("")
    print("Snappy AI - accessibility click helper")
    print("")
    print("  (no option)        start the graphical interface")
    print("  --console          classic text interface")
    print("  --help             this text")
    print("")
    print("  --mode1 | --mode2  classic (suppress + snap) / guide (never eat a click)")
    print("  --radius N|small|medium|large   search distance in pixels")
    print("  --capture auto|dxcam|mss|wgc    screen-capture backend")
    print("  --merge N          merge detected pieces within N px")
    print("  --accent SPEC      preferred colour(s): blue, 60,120,220, or a ; list")
    print("  --glide MS         animate cursor moves over MS milliseconds")
    print("  --accent-only      assist only items carrying the accent colour")
    print("  --follow           remember and prefer the last snapped item")
    print("  --motion-3d        lead moving targets with the position+scale tracker")
    print("  --controller       enable controller (XInput) input")
    print("  --no-controller-drive  controller binds actions only; the mouse stays the pointer")
    print("  --no-adaptive      disable the adaptive ink threshold (A/B)")
    print("  --no-lumnorm       disable luminance normalisation (A/B)")
    print("  --reset-config     restore every setting to its default")
    print("")
    print("Settings live in config.json / config.ini next to the executable.")
    print("Deployment constants (entitlement repo, payment links, provider")
    print("credentials) are build-time and deliberately not settings.")
    print("")


def main():
    sys.excepthook = _crash_guard
    if "--help" in sys.argv or "-h" in sys.argv:
        print_usage()
        sys.exit(0)
    try:
        _run()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except BaseException:


        _crash_guard(*sys.exc_info())
        sys.exit(1)


def _run():
    if not _ensure_single_instance():
        return
    attach_app_log()
    try:
        cfg = Config()
    except Exception as exc:
        try:
            from config import DEFAULT_CONFIG as _DC
            from config import Config as _C
            cfg = _C.__new__(_C)
            import threading as _th
            cfg.lock = _th.RLock()
            from config import config_dir as _cd
            cfg.dir = _cd()
            import os as _os
            cfg.path = _os.path.join(cfg.dir, "config.json")
            cfg.ini_path = _os.path.join(cfg.dir, "config.ini")
            cfg.values = dict(_DC)
        except Exception:
            raise
        try:
            log("config load failed (%r); continuing with defaults" % (exc,))
        except Exception:
            pass

    if "--reset-config" in sys.argv:
        try:
            cfg.values = dict(DEFAULT_CONFIG)
            cfg.save()
        except Exception as exc:
            try:
                log("config reset save failed (%r); continuing" % (exc,))
            except Exception:
                pass
        print("Config reset to defaults: %s" % cfg.path)

    warn_unknown_flags(sys.argv)
    apply_cli_overrides(cfg, sys.argv)
    cfg.sanitize()
















    try:
        import online
        import general_utils as build_secrets
        fatal, advisory = online.build_is_ready()
        for problem in advisory:
            log("BUILD: %s" % problem)
        if not fatal and not build_secrets.is_shippable():



            if getattr(sys, "frozen", False):
                _announce(
                    "This is not a release build",
                    "\n".join("- " + p for p in advisory)
                    + "\n\nThe app will start normally. Fix these before "
                      "distributing it.")
        if fatal:
            _announce("%s cannot start" % APP_NAME,
                      "This build is missing the constants it needs:\n\n"
                      + "\n".join("- " + p for p in fatal)
                      + "\n\nRe-package it with the production deployment "
                        "constants injected (see general_utils.py).",
                      fatal=True)
            sys.exit(1)
    except SystemExit:
        raise
    except Exception as exc:
        log("build readiness check failed: %r" % (exc,))

    if not IS_WINDOWS:
        print("")
        print("Snappy AI active mouse assistance currently requires Windows.")
        print("")
        sys.exit(1)



    use_gui = "--console" not in sys.argv
    if use_gui:
        try:
            import importlib
            for _probe in ("tkinter", "tkinter.font", "tkinter.ttk",
                           "tkinter.filedialog", "tkinter.messagebox",
                           "tkinter.simpledialog"):
                importlib.import_module(_probe)
        except ImportError as exc:
            print("Tkinter unavailable (%s) - starting in console mode."
                  % (exc,))
            use_gui = False

    if use_gui:
        from gui import run_gui
        sys.exit(run_gui(cfg))
    from console import console_gate, windows_main
    identity = console_gate(cfg)
    if identity is None:
        sys.exit(1)
    sys.exit(windows_main(cfg, identity))


if __name__ == "__main__":
    main()
