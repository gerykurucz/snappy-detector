
import queue
import sys
import threading
import time

import online
from system_core import APP_NAME, log

from config import RADIUS_STEPS
from assist import start_app
from input_utils import effective_mode_key
from vision import (_parse_accents, accent_soft_of, accent_matcher_of,
                    calibrate_accent)

def print_banner(app):
    cfg = app.cfg

    try:
        rad_value = int(cfg.get("radius"))
    except (TypeError, ValueError):
        rad_value = 150
    scaled_px = app.radius_px()
    rad_label = dict(RADIUS_STEPS).get(rad_value, "custom")
    if scaled_px != rad_value:
        rad_label = "%s (scaled %d px)" % (rad_label, scaled_px)

    print("")
    print("=" * 70)
    print("   S N A P P Y   A I  -  accessibility click helper")
    print("=" * 70)
    try:
        _mode = int(str(cfg.get("mode", "2")))
    except (TypeError, ValueError):
        _mode = 2
    print("   MODE        : %s" % ("1 classic" if _mode == 1 else "2 guide"))
    print("   Radius      : %s (%d px)" % (rad_label, app.radius_px()))
    print("   Capture     : %s" % cfg.get("capture_backend", "auto"))
    try:
        _merge = int(cfg.get("merge_distance", 0) or 0)
    except (TypeError, ValueError):
        _merge = 0
    print("   Merge       : %d px" % _merge)
    print("   Accent      : %s%s" % (
        cfg.get("accent_color") or "off",
        "  [ONLY - other items ignored]"
        if cfg.get("accent_only") else ""))
    try:
        glide = int(cfg.get("glide_ms", 0) or 0)
    except (TypeError, ValueError):
        glide = 0
    print("   Glide       : %s" % ("%d ms" % glide if glide > 0
                                   else "off (instant)"))
    try:
        _fb = float(cfg.get("follow_boost", 1.0))
    except (TypeError, ValueError):
        _fb = 1.0
    try:
        _fs = int(cfg.get("follow_seconds", 20))
    except (TypeError, ValueError):
        _fs = 20
    print("   Follow      : %s" % (
        "ON (%.1fx boost, %ds memory)"
        % (_fb, _fs)
        if cfg.get("follow_enabled") else "OFF"))
    if cfg.get("controller_input"):
        _binds = [str(cfg.get(k) or "") for k in
                  ("pad_launch", "pad_pause", "pad_record", "pad_play")]
        print("   Controller  : ON (%s drive, output %s%s)"
              % ("stick+trigger" if cfg.get("pad_drive", True) else "bindings",
                 cfg.get("pad_output", "auto"),
                 ", bound: " + "/".join(b for b in _binds if b)
                 if any(_binds) else ""))
    else:
        print("   Controller  : OFF (Assistance tab: Enable controller input)")
    print("")
    print("   STATE       : %s" % ("LAUNCHED" if app.running else "PAUSED"))
    print("")
    print("   Mouse behaviour")
    print("     Mode 2 guide:")
    print("       Your clicks are never eaten.")
    print("       After release, cursor moves onto the nearest clear item.")
    print("")
    print("     Mode 1 classic:")
    print("       If you click near an item, the mis-click is suppressed,")
    print("       cursor snaps to the item, and SnappyAI clicks it for you.")
    print("")
    print("   SHIFT + click : no assist for that click.")
    print("")
    print("   Always-available keys (customizable: config.ini or GUI)")
    print("     %-14s launch" % str(cfg.get("key_launch", "ctrl+l")).lower())
    print("     %-14s pause" % str(cfg.get("key_pause", "ctrl+p")).lower())
    print("     %-14s record macro (start)"
          % str(cfg.get("key_record", "ctrl+alt+r")).lower())
    print("     %-14s record macro (stop + save)"
          % str(cfg.get("key_record_stop", "ctrl+alt+s")).lower())
    print("     %-14s play recording (selected/newest)"
          % str(cfg.get("key_play", "ctrl+alt+m")).lower())
    print("     %-14s play recording slots 1-5"
          % (str(cfg.get("key_play1", "ctrl+1")).lower() + "..."))
    print("")
    print("   Mode keys (fire everywhere, keys still type)")
    print("     %-14s mode 1 classic" % effective_mode_key(cfg, 1).split()[0])
    print("     %-14s mode 2 guide" % effective_mode_key(cfg, 2).split()[0])
    print("")
    print("   Console commands")
    print("     s=status t=toggle h=help q=quit a=accent f=follow g=gamepad v=calibrate p=capture")
    print("=" * 70)
    print("")

def print_status(app):
    cfg = app.cfg

    print("-" * 70)
    print(
        " STATUS   mode: %s | radius: %d px | state: %s"
        % (
            cfg.get("mode"),
            app.radius_px(),
            "LAUNCHED" if app.running else "PAUSED",
        )
    )
    print(
        "          assisted: %d   |   normal/passed: %d"
        % (app.snap_count, app.pass_count)
    )
    mem = app.follow
    mem_txt = "no memory yet"
    if mem.when > 0:
        age = int(time.perf_counter() - mem.when)
        mem_txt = "last snap %ds ago (%d total)" % (age, mem.hits)
    print(
        "          follow: %s  |  %s"
        % ("ON" if cfg.get("follow_enabled") else "OFF", mem_txt)
    )
    print("-" * 70)

def print_help(app):
    print("-" * 70)
    print(" SNAPPYAI HELP")
    print("  - Start paused. Press Ctrl+L to launch assistance.")
    print("  - Press Ctrl+P to pause (customizable). While paused,")
    print("    nothing is intercepted - macros and recorder included.")
    print("  - Ctrl+Alt+R starts the macro recorder: your keys are")
    print("    captured and still reach your apps; Ctrl+Alt+S saves")
    print("    into the library (GUI Macros tab: rename, per-recording")
    print("    repeat-until-stopped, export, import, delete).")
    print("  - Ctrl+Alt+M plays the selected (or newest) recording;")
    print("    Ctrl+1..Ctrl+5 play recordings #1..#5 by list position.")
    print("  - Hold SHIFT while clicking to bypass assistance once.")
    print("  - Mode 2 guide is safest: it never eats your clicks.")
    print("  - Mode 1 classic is stronger: it can suppress a mis-click and")
    print("    click the nearby item for you.")
    print("  - Settings reload from disk whenever you press Apply in")
    print("    the GUI (there is no reload hotkey anymore); quitting")
    print("    goes through the window's close button.")
    print("  - All hotkeys are customizable (key_launch, key_pause, ...) in")
    print("    config.ini or the GUI's Keyboard & Mouse tab.")
    print("  - Single-key binds (1, 2, v ...) work in every app by")
    print("    default AND never block typing: the action fires and the")
    print("    key still reaches the focused app.")
    print("  - follow_learn = accent: the follow memory only learns items")
    print("    matching accent_color, so a misclick that snapped elsewhere")
    print("    never retargets the memory away from your colour.")
    print("  - glide_ms animates cursor moves so you can follow them")
    print("    (0 = instant; moving your mouse cancels a glide).")
    print("  - accent_only (console 'a') restricts assistance to items")
    print("    containing your accent colour - 'click the blue thing' mode.")
    print("    Clicks where nothing matches pass through untouched.")
    print("  - follow memory (console 'f') remembers your last snapped item:")
    print("    for a few seconds, similar-looking or same-place items win")
    print("    ties - handy for scrollbars and repeated clicks on one")
    print("    control. It learns only from your own successful clicks.")
    print("  - calibrate (console 'v') measures your colour on THIS screen:")
    print("    point at the app you work in, press v, and it suggests the")
    print("    accent_soft value tuned to your display and lighting.")
    print("    Set it on the Detection tab ('Accept near-miss shades').")
    print("  - capture report (console 'p'): if assistance works in normal")
    print("    windows but fails in a hardware-accelerated app, run")
    print("    it there. BLACK/FROZEN frames mean the capture engine cannot")
    print("    SEE that window (Desktop Duplication misses overlays/DRM,")
    print("    mss misses GPU-exclusive content) even though dxcam is")
    print("    installed fine. Fix: Settings -> Screen capture engine ->")
    print("    Windows Graphics Capture (pip install windows-capture), or")
    print("    Compatible mode; then restart. Exclusive-fullscreen apps")
    print("    need borderless-window mode for ANY capturer to see them.")
    print("  - Detection adapts to your screen: dim regions lower their own")
    print("    ink bar (adaptive_threshold) and edges are scored relative to")
    print("    local brightness (luminance_norm), so dark themes work like")
    print("    light ones. Both default on; A/B test with the CLI flags")
    print("    --no-adaptive / --no-lumnorm.")
    print("  - Sign-in is online only (Discord, straight from the")
    print("    app); the Stripe")
    print("    subscription bound to that login is re-checked at every")
    print("    start - no reachable server, no engine.")
    print("  - exclude_color (Detection tab): items containing")
    print("    that colour are NEVER clickable - clicks on them pass")
    print("    straight through, in both modes. Exclusion outranks")
    print("    accent boosts.")
    print("  - motion_predict + motion_lead_ms (Detection tab):")
    print("    track ahead of MOVING items (scrolling lists, video seek")
    print("    bars) by a measured speed+direction lead; standing")
    print("    items behave exactly as before.")
    print("  - motion_3d (Assistance tab): the same lead through a")
    print("    position-plus-size tracker - bad scans are ignored, turns")
    print("    are followed through, and a sudden size jump restarts the")
    print("    track. Same gates and cap; only the brains change.")
    print("  - Macros tab: five macro keys that send a whole key combo")
    print("    instead of typing ('B means Ctrl+C'). One press = one")
    print("    combo press, and the macro key is always consumed while")
    print("    launched, so D = Ctrl+D never leaks a bare D.")
    print("    Recorder: Ctrl+Alt+R capture, Ctrl+Alt+S stop and save,")
    print("    Ctrl+Alt+M play, Ctrl+1..5 play by list position. Each")
    print("    recording is named and carries its own playback style:")
    print("    play once, or repeat until stopped.")
    print("  - Controller (Assistance tab: 'Enable controller input'):")
    print("    Windows' own XInput, so a pad is a full second input device.")
    print("    Left stick steers the cursor, right trigger is the press that")
    print("    assistance snaps from, left trigger is a right click, and the")
    print("    right stick steers the view while that press is held - so the")
    print("    snap, guide and follow behaviour is the SAME code")
    print("    a mouse user gets. Start launches, Back pauses; every action")
    print("    can be re-bound on the Controller tab, including to a")
    print("    combination ('rb+a'), which always wins over the shorter")
    print("    binding it contains. Recordings capture controller buttons and")
    print("    sticks into the independent Controller Recordings list; keyboard")
    print("    input goes into the Recordings list; controller")
    print("    macros sit on the Macros tab. Replaying controller output as a")
    print("    REAL virtual gamepad needs the optional ViGEmBus driver -")
    print("    without it, output falls back to keyboard and mouse.")
    print("    Console 'g' prints what is connected and what is bound.")
    print("  - capture_backend, merge_distance, accent_color and friends can")
    print("    be tuned in config.ini / config.json; see %s." % app.cfg.path)
    print("  - Settings are stored in config.json and optional config.ini.")
    print("-" * 70)


def _retry_prompt():
    try:
        ans = input("   Retry? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("")
        return False
    return ans not in ("n", "no", "q", "quit")


def _console_login(cfg):
    while True:
        print("")
        print("   Sign in with Discord (your browser opens; the tab")
        print("   returns to this app on its own).")
        try:
            identity, msg = online.login_with_provider(cfg, "discord")
        except online.OnlineError as exc:
            print("   Sign-in unreachable: %s" % exc)
            if not _retry_prompt():
                return None
            continue
        if identity:
            return identity
        print("   %s" % (msg or "No sign-in came back - try again."))
        if not _retry_prompt():
            return None
        time.sleep(1.0)


def _console_subscribe_loop(cfg, identity):
    while True:
        try:
            identity, ent = online.me(cfg, fresh=True)
        except online.OnlineError as exc:
            print("   Auth server unreachable: %s" % exc)
            if not _retry_prompt():
                return None
            continue
        if identity is None:
            return None
        print("   Status    : %s" % online.entitlement_summary(ent))
        if online.entitlement_active(ent) and not (ent or {}).get("shared"):
            return identity
        if (ent or {}).get("shared"):
            print("")
            print("   This account is in use on ANOTHER computer right")
            print("   now. Snappy AI stays off while it is shared -")
            print("   close the other session, then re-check.")
            try:
                ans = input("   Enter re-checks, q quits: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("")
                return None
            if ans in ("q", "quit"):
                return None
            online.nudge_publisher(cfg)
            continue
        state = str((ent or {}).get("status", "unsubscribed"))
        print("")
        print("   Account   : %s" % online.identity_label(identity))
        print("   Subscrip. : %s - the engine stays off." % state)
        if state in ("unsubscribed", ""):
            print("   Pay with THIS e-mail - a purchase made on any")
            print("   other address cannot activate this login.")
        print("   Subscribe on Stripe (activation lands up to ~10 min")
        print("   after payment, or use 'I paid' in the GUI):")
        print("     monthly : %s"
              % online.subscribe_url(cfg, identity, "monthly"))
        if online.payment_link(cfg, "trial"):
            print("     trial   : %s"
                  % online.subscribe_url(cfg, identity, "trial"))
        try:
            ans = input("   Enter re-checks, q quits: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("")
            return None
        if ans in ("q", "quit"):
            return None
        online.nudge_publisher(cfg)


def console_gate(cfg):
    print("")
    print("   %s - online sign-in" % APP_NAME)
    print("   (subscriptions are bound to your provider login)")
    while True:
        ent = None
        identity = None
        sess = online.load_session()
        if sess and sess.get("provider"):
            try:
                identity, ent = online.me(cfg, fresh=True)
            except online.OnlineError as exc:
                print("   Auth server unreachable: %s" % exc)
                if not _retry_prompt():
                    return None
                continue
            if identity is None:
                online.clear_session()
        if identity is None:
            identity = _console_login(cfg)
            if identity is None:
                return None
        else:
            if online.entitlement_active(ent) and not (ent or {}).get("shared"):
                return identity
        identity = _console_subscribe_loop(cfg, identity)
        if identity is None:
            return None
        return identity


def windows_main(cfg, identity=None):
    app, hook_thread = start_app(cfg, identity)
    if app is None:
        return 1

    print_banner(app)

    if app.running:
        log("Ready. Assistance is LAUNCHED. Press Ctrl+P to pause.")
    else:
        log("Ready. Program is PAUSED. Press Ctrl+L to launch.")

    cmds = queue.Queue()

    def reader():
        try:
            for line in sys.stdin:
                cmds.put(line)
        except (OSError, ValueError):
            pass
        cmds.put(None)

    threading.Thread(target=reader, daemon=True).start()

    def shared_watch():
        if not identity:
            return
        idh = online.row_hash(identity)
        legacy = online.row_hash_legacy(identity)
        _last_shared_log = 0.0
        while not app.stop.is_set():
            try:
                interval = online.presence_interval_seconds()
            except Exception:
                interval = 60
            try:
                interval = max(1, int(interval))
            except (TypeError, ValueError):
                interval = 60
            if app.stop.wait(interval):
                return
            try:
                rows, gen = online.fetch_entitlements(cfg)
            except online.OnlineError:
                continue
            except Exception:
                continue
            try:
                key = idh if idh in rows else legacy
                shared = online.row_shared(rows, key, gen)
            except Exception:
                continue
            if shared and app.running:
                app.toggle_running()
                log("ACCOUNT IN USE ELSEWHERE - assistance paused. "
                    "Snappy AI runs on one machine per account; close "
                    "the other session and launch again.")
                _last_shared_log = time.monotonic()
            elif shared:
                now_m = time.monotonic()
                if now_m - _last_shared_log > 300:
                    _last_shared_log = now_m
                    log("ACCOUNT IN USE ELSEWHERE - staying paused.")

    threading.Thread(target=shared_watch, daemon=True,
                     name="shared-watch").start()

    try:
        while not app.stop.is_set():
            try:
                line = cmds.get(timeout=0.25)
            except queue.Empty:
                continue

            if line is None:
                break

            line = line.strip().lower()

            if not line:
                continue

            if line in ("q", "quit", "exit"):
                app.request_quit()
                break

            elif line in ("t", "toggle"):
                app.toggle_running()

            elif line in ("s", "status"):
                print_status(app)

            elif line in ("p", "capture"):





                try:
                    print(app._capture.capture_report())
                except Exception as exc:
                    print("capture report failed: %r" % (exc,))

            elif line in ("h", "help", "?"):
                print_help(app)

            elif line in ("a", "accent"):
                if not _parse_accents(cfg.get("accent_color")):
                    print("accent_color is off. Set it first (GUI Detection")
                    print("tab, or config.ini: accent_color = blue")
                    print("then press Apply in the GUI, and 'a' will work.")
                else:
                    new_state = not bool(cfg.get("accent_only"))
                    cfg.set("accent_only", new_state)
                    log("ACCENT-ONLY: %s (%s)"
                        % ("ON - only matching items are clickable"
                           if new_state
                           else "OFF - all items eligible",
                           cfg.get("accent_color")))

            elif line in ("f", "follow"):
                new_state = not bool(cfg.get("follow_enabled"))
                cfg.set("follow_enabled", new_state)
                try:
                    _fb = float(cfg.get("follow_boost", 1.0))
                except (TypeError, ValueError):
                    _fb = 1.0
                try:
                    _fs = int(cfg.get("follow_seconds", 20))
                except (TypeError, ValueError):
                    _fs = 20
                log("FOLLOW MEMORY: %s (%.1fx boost for %ds after each snap)"
                    % ("ON" if new_state else "OFF", _fb, _fs))

            elif line in ("g", "gamepad", "controller", "pad"):
                try:
                    print(app.pad_status())
                except Exception as exc:
                    print("controller status failed: %r" % (exc,))

            elif line in ("v", "calibrate"):
                accents = _parse_accents(cfg.get("accent_color"))
                if not accents:
                    print("accent_color is off - there is nothing to "
                          "calibrate. Set your colour first (GUI Colors "
                          "tab), point at the app you work in, and press "
                          "v again.")
                else:
                    try:
                        from win32rt import get_cursor_pos
                        x, y = get_cursor_pos()
                        img, cx, cy, _o, _e = app.grab_around(
                            x, y, app.radius_px())
                    except Exception as exc:
                        print("Calibration capture failed: %r" % (exc,))
                        img = None
                    if img is None:
                        print("Calibration capture failed - nothing "
                              "grabbed around the cursor.")
                    else:
                        try:
                            _tol = float(cfg.get("accent_tolerance", 26))
                        except (TypeError, ValueError):
                            _tol = 26.0
                        try:
                            _soft_now = int(cfg.get("accent_soft", 60))
                        except (TypeError, ValueError):
                            _soft_now = 60
                        try:
                            rep = calibrate_accent(
                                img, cx, cy, accents, _tol,
                                accent_soft_of(cfg.values),
                                accent_matcher_of(cfg.values))
                        except Exception as exc:
                            print("Calibration failed: %r" % (exc,))
                            rep = None
                        if rep is None:
                            print("Nothing measurable under the cursor.")
                        else:
                            print("Colour calibration (%d px sampled, "
                                  "unit %s):" % (rep["pixels"], rep["unit"]))
                            print("  median distance: %.2f   95th: %.2f"
                                  % (rep["p50"], rep["p95"]))
                            print("  strong matches now: %.0f%%"
                                  % (rep["strong_frac"] * 100.0))
                            print("  suggested accent_soft: %d (now %d)"
                                  % (rep["suggested_soft"], _soft_now))

            else:
                print("Unknown command '%s'. Type h for help, q to quit." % line)

    except KeyboardInterrupt:
        app.request_quit()
    finally:
        hook_thread.join(timeout=2.0)

    print("")
    log(
        "Snappy AI stopped. Assisted %d clicks this session. Goodbye."
        % app.snap_count
    )

    return 0





