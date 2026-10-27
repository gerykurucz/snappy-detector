#!/usr/bin/env python3
"""Comprehensive fix script for assist.py - applies all fixes from the review."""
import re

with open('assist.py', 'r') as f:
    content = f.read()

# ============================================================
# FIX 1: _nudge function - restore aim_speed/aim_y_multiplier and add session support
# ============================================================
old_nudge = '''    def _nudge(self, dx, dy, view=False, continuous=False):
        if view and self._unibot_aim():
            sp = self._aim_speed()
            x = float(dx) * sp + self._aim_rem_x
            y = float(dy) * sp + self._aim_rem_y
            mx = int(x)
            my = int(y)
            self._aim_rem_x = x - mx
            self._aim_rem_y = y - my
            if mx or my:
                try:
                    self._input.nudge(mx, my)
                except Exception as exc:
                    log("unibot nudge error: %r" % (exc,))
            with self._state_lock:
                self._view_moved = True
            return
        if view:
            try:
                from input_backend import aim_assist_step
                if aim_assist_step(dx, dy, self.cfg):
                    with self._state_lock:
                        self._view_moved = True
                    return
            except Exception as exc:
                log("nudge view steer error: %r" % (exc,))
        try:
            self._input.nudge(dx, dy)
        except Exception as exc:
            log("nudge error: %r" % (exc,))
        if view:
            with self._state_lock:
                self._view_moved = True'''

new_nudge = '''    def _nudge(self, dx, dy, view=False, sess=None):
        # sess: optional hold session dict for per-session residuals
        if view and self._unibot_aim():
            sp = self._aim_speed()
            ym = self._aim_y_multiplier()
            # Use session-aware residuals if available, else fall back to global
            rem_x = sess["rem_x"] if sess else self._aim_rem_x
            rem_y = sess["rem_y"] if sess else self._aim_rem_y
            x = float(dx) * sp + rem_x
            y = float(dy) * sp * ym + rem_y
            mx = int(x)
            my = int(y)
            if sess:
                sess["rem_x"] = x - mx
                sess["rem_y"] = y - my
            else:
                self._aim_rem_x = x - mx
                self._aim_rem_y = y - my
            if mx or my:
                try:
                    self._input.nudge(mx, my)
                except Exception as exc:
                    log("unibot nudge error: %r" % (exc,))
            with self._state_lock:
                self._view_moved = True
            return
        if view:
            try:
                from input_backend import aim_assist_step
                if aim_assist_step(dx, dy, self.cfg):
                    with self._state_lock:
                        self._view_moved = True
                    return
            except Exception as exc:
                log("nudge view steer error: %r" % (exc,))
        try:
            self._input.nudge(dx, dy)
        except Exception as exc:
            log("nudge error: %r" % (exc,))
        if view:
            with self._state_lock:
                self._view_moved = True'''

content = content.replace(old_nudge, new_nudge)

# ============================================================
# FIX 2: _pad_drive - remove manual aim_speed pre-multiply, let _nudge handle it
# ============================================================
old_pad_drive = '''        rx, ry = gamepad.apply_deadzone(state.rx, state.ry,
                                        settings["deadzone"])
        if (rx or ry) and self._pad_click_down:
            self._nudge(int(round(rx * 24.0)), int(round(-ry * 24.0)),
                        view=True, continuous=True)'''

new_pad_drive = '''        rx, ry = gamepad.apply_deadzone(state.rx, state.ry,
                                        settings["deadzone"])
        if (rx or ry) and self._pad_click_down:
            # Let _nudge handle aim_speed, cap_step, and gain
            dx = int(round(rx * 24.0))
            dy = int(round(-ry * 24.0))
            cap = self._max_step_cfg()
            dx, dy = self._cap_step(dx, dy, cap)
            if dx or dy:
                self._nudge(dx, dy, view=True, continuous=True)'''

content = content.replace(old_pad_drive, new_pad_drive)

# ============================================================
# FIX 3: _pad_drive right button - restore inject_mouse and add rbutton_down_time
# ============================================================
old_right_btn = '''        if right_index is not None and right_index != click_index:
            right_down = state.down(right_index)
            if right_down and not self._pad_right_down:
                self._pad_right_down = True
                # Route through mouse2 action handlers so mouse2_action/mouse2_hold_action work
                if self._armed_now():
                    action = self._mouse2_hold_action()
                    if action == 4:
                        self._handle_mouse2()
                    else:
                        self._rbtn_armed = True
                        self._start_mouse2_hold_timer()
            elif not right_down and self._pad_right_down:
                self._pad_right_down = False
                try:
                    self._finish_mouse2(time.perf_counter())
                except Exception:
                    pass'''

new_right_btn = '''        if right_index is not None and right_index != click_index:
            right_down = state.down(right_index)
            if right_down and not self._pad_right_down:
                self._pad_right_down = True
                self.rbutton_down_time = time.perf_counter()
                try:
                    inject_mouse(BTN_RIGHT, True)
                except Exception:
                    pass
                # Route through mouse2 action handlers so mouse2_action/mouse2_hold_action work
                if self._armed_now():
                    action = self._mouse2_hold_action()
                    if action == 4:
                        self._handle_mouse2()
                    else:
                        self._rbtn_armed = True
                        self._start_mouse2_hold_timer()
            elif not right_down and self._pad_right_down:
                self._pad_right_down = False
                try:
                    inject_mouse(BTN_RIGHT, False)
                except Exception:
                    pass
                try:
                    self._finish_mouse2(time.perf_counter())
                except Exception:
                    pass'''

content = content.replace(old_right_btn, new_right_btn)

# ============================================================
# FIX 4: _mouse2_hold_check - add pad_right_down check
# ============================================================
old_mouse2_check = '''            try:
                still = bool(user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000)
            except Exception:
                still = True
            if not (still and self._armed_now()):'''

new_mouse2_check = '''            try:
                still = bool(user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000) \
                        or self._pad_right_down
            except Exception:
                still = True
            if not (still and self._armed_now()):'''

content = content.replace(old_mouse2_check, new_mouse2_check)

# ============================================================
# FIX 5: _center_hold_cycle - fix dt calculation, add motion lead, add snap_offset
# ============================================================
old_center_hold_unibot = '''            if self._unibot_aim():
                self._last_hold_steered = True
                self._nudge(float(target.dx), float(target.dy), view=True)
                if now - sess.get("last_beat", 0.0) > 2.0:
                    sess["last_beat"] = now
                    log("Unibot steer: mouse delta (%+d,%+d) px/frame."
                        % (int(round(target.dx)), int(round(target.dy))))
                return True
            self._last_hold_steered = False
            if self._reticle_active():


                tx, ty = float(target.dx), float(target.dy)
            else:


                try:
                    _pcx, _pcy = get_cursor_pos()
                    tx = (_mx + float(target.dx)) - _pcx
                    ty = (_my + float(target.dy)) - _pcy
                except Exception:
                    tx, ty = float(target.dx), float(target.dy)'''

new_center_hold_unibot = '''            if self._unibot_aim():
                # Apply EMA smoothing like the non-unibot path
                tx, ty = float(target.dx), float(target.dy)
                # Apply snap_offset to the raw target offset
                tx, ty = self._apply_snap_offset(tx, ty)
                dt = min(max(now - sess.get("last_t", now), 0.001), 0.1)
                alpha = 1.0 - math.exp(-dt / 0.05)
                alpha = min(max(alpha, 0.0), 1.0)
                sess["px"] += alpha * (tx - sess["px"])
                sess["py"] += alpha * (ty - sess["py"])

                # Apply motion lead if enabled
                if (bool(self.cfg.get("motion_predict", False)) or
                        bool(self.cfg.get("motion_3d", False))):
                    out_x, out_y = self._apply_motion_lead(sess["px"], sess["py"])
                else:
                    out_x, out_y = sess["px"], sess["py"]

                # Compute step with cap_step
                ix, iy = int(round(out_x)), int(round(out_y))
                cap = self._max_step_cfg()
                jx, jy = self._cap_step(ix, iy, cap)
                if cap > 0:
                    sess["cx"] = min(max(out_x - jx, -cap), cap)
                    sess["cy"] = min(max(out_y - jy, -cap), cap)
                else:
                    sess["cx"], sess["cy"] = (out_x - ix, out_y - iy)
                ix, iy = jx, jy
                if ix or iy:
                    self._nudge(ix, iy, view=True, continuous=True, sess=sess)
                if now - sess.get("last_beat", 0.0) > 2.0:
                    sess["last_beat"] = now
                    log("Unibot steer: steering (%+d,%+d)/frame toward "
                        "(%+d,%+d) EMA."
                        % (ix, iy, int(round(sess["px"])),
                           int(round(sess["py"]))))
                return True
            self._last_hold_steered = False
            if self._reticle_active():
                tx, ty = float(target.dx), float(target.dy)
            else:
                try:
                    _pcx, _pcy = get_cursor_pos()
                    tx = (_mx + float(target.dx)) - _pcx
                    ty = (_my + float(target.dy)) - _pcy
                except Exception:
                    tx, ty = float(target.dx), float(target.dy)
            # Apply snap_offset
            tx, ty = self._apply_snap_offset(tx, ty)'''

content = content.replace(old_center_hold_unibot, new_center_hold_unibot)

# ============================================================
# FIX 6: _center_hold_cycle non-unibot - add motion lead after EMA
# ============================================================
old_center_nonunibot = '''            alpha = 1.0 - math.exp(-dt / 0.05)
            alpha = min(max(alpha, 0.0), 1.0)
            sess["px"] += alpha * (tx - sess["px"])
            sess["py"] += alpha * (ty - sess["py"])


        out_x = sess["px"] + sess.get("cx", 0.0)'''

new_center_nonunibot = '''            alpha = 1.0 - math.exp(-dt / 0.05)
            alpha = min(max(alpha, 0.0), 1.0)
            sess["px"] += alpha * (tx - sess["px"])
            sess["py"] += alpha * (ty - sess["py"])

            # Apply motion lead if enabled
            if (bool(self.cfg.get("motion_predict", False)) or
                    bool(self.cfg.get("motion_3d", False))):
                out_x, out_y = self._apply_motion_lead(sess["px"], sess["py"])
            else:
                out_x, out_y = sess["px"], sess["py"]


        out_x = sess["px"] + sess.get("cx", 0.0)'''

content = content.replace(old_center_nonunibot, new_center_nonunibot)

# ============================================================
# FIX 7: _guide_hold_cycle unibot - fix dt, add motion lead, add snap_offset
# ============================================================
old_guide_unibot = '''            if self._unibot_aim():
                self._last_hold_steered = True
                self._nudge(float(target.dx), float(target.dy), view=True)
                if now - sess.get("last_beat", 0.0) > 2.0:
                    sess["last_beat"] = now
                    log("Guide steer: mouse delta (%+d,%+d) px/frame."
                        % (int(round(target.dx)), int(round(target.dy))))
                return True
            self._last_hold_steered = False
            if self._reticle_active():
                tx, ty = float(target.dx), float(target.dy)
            else:
                try:
                    _pcx, _pcy = get_cursor_pos()
                    tx = (_mx + float(target.dx)) - _pcx
                    ty = (_my + float(target.dy)) - _pcy
                except Exception:
                    tx, ty = float(target.dx), float(target.dy)'''

new_guide_unibot = '''            if self._unibot_aim():
                # Apply EMA smoothing like the non-unibot path
                tx, ty = float(target.dx), float(target.dy)
                # Apply snap_offset to the raw target offset
                tx, ty = self._apply_snap_offset(tx, ty)
                # Use dt computed at the top of the function
                alpha = 1.0 - math.exp(-dt / 0.05)
                alpha = min(max(alpha, 0.0), 1.0)
                sess["px"] += alpha * (tx - sess["px"])
                sess["py"] += alpha * (ty - sess["py"])

                # Apply motion lead if enabled
                if (bool(self.cfg.get("motion_predict", False)) or
                        bool(self.cfg.get("motion_3d", False))):
                    out_x, out_y = self._apply_motion_lead(sess["px"], sess["py"])
                else:
                    out_x, out_y = sess["px"], sess["py"]

                # Compute step with cap_step
                ix, iy = int(round(out_x)), int(round(out_y))
                cap = self._max_step_cfg()
                jx, jy = self._cap_step(ix, iy, cap)
                if cap > 0:
                    sess["cx"] = min(max(out_x - jx, -cap), cap)
                    sess["cy"] = min(max(out_y - jy, -cap), cap)
                else:
                    sess["cx"], sess["cy"] = (out_x - ix, out_y - iy)
                ix, iy = jx, jy
                if ix or iy:
                    self._nudge(ix, iy, view=True, continuous=True, sess=sess)
                if now - sess.get("last_beat", 0.0) > 2.0:
                    sess["last_beat"] = now
                    log("Guide steer: steering (%+d,%+d)/frame toward "
                        "(%+d,%+d) EMA."
                        % (ix, iy, int(round(sess["px"])),
                           int(round(sess["py"]))))
                return True
            self._last_hold_steered = False
            if self._reticle_active():
                tx, ty = float(target.dx), float(target.dy)
            else:
                try:
                    _pcx, _pcy = get_cursor_pos()
                    tx = (_mx + float(target.dx)) - _pcx
                    ty = (_my + float(target.dy)) - _pcy
                except Exception:
                    tx, ty = float(target.dx), float(target.dy)
            # Apply snap_offset
            tx, ty = self._apply_snap_offset(tx, ty)'''

content = content.replace(old_guide_unibot, new_guide_unibot)

# ============================================================
# FIX 8: _guide_hold_cycle non-unibot - add motion lead after EMA
# ============================================================
old_guide_nonunibot = '''            alpha = 1.0 - math.exp(-dt / 0.05)
            alpha = min(max(alpha, 0.0), 1.0)
            sess["px"] += alpha * (tx - sess["px"])
            sess["py"] += alpha * (ty - sess["py"])

        out_x = sess["px"] + sess.get("cx", 0.0)'''

new_guide_nonunibot = '''            alpha = 1.0 - math.exp(-dt / 0.05)
            alpha = min(max(alpha, 0.0), 1.0)
            sess["px"] += alpha * (tx - sess["px"])
            sess["py"] += alpha * (ty - sess["py"])

            # Apply motion lead if enabled
            if (bool(self.cfg.get("motion_predict", False)) or
                    bool(self.cfg.get("motion_3d", False))):
                out_x, out_y = self._apply_motion_lead(sess["px"], sess["py"])
            else:
                out_x, out_y = sess["px"], sess["py"]

        out_x = sess["px"] + sess.get("cx", 0.0)'''

content = content.replace(old_guide_nonunibot, new_guide_nonunibot)

# ============================================================
# FIX 9: _center_hold_cycle - remove subject_priority hard filter
# ============================================================
old_subject_filter = '''        if got is None:
            steer = False
        elif bool(self.cfg.get("subject_priority", False)):
            try:
                _hs0 = float(getattr(got[0], "hscore", 0.0) or 0.0)
            except (TypeError, ValueError):
                _hs0 = 0.0
            try:
                if _hs0 >= 0.30:
                    steer = True
                elif _shape_tier(
                        got[0].bw, got[0].bh,
                        getattr(got[0], "area", 0) or 0) > 0:
                    steer = True
                else:
                    steer = (got[0].bh >= 14.0
                             and 0.40 <= got[0].bh / max(1.0, got[0].bw)
                             <= 3.5)
            except Exception:
                steer = False
        else:
            steer = True
        if not steer:'''

new_subject_filter = '''        if got is None:
            steer = False
        else:
            steer = True
        if not steer:'''

content = content.replace(old_subject_filter, new_subject_filter)

# ============================================================
# FIX 10: _guide_hold_cycle - skip _scan_center when watch_center_only=False
# ============================================================
old_guide_scan = '''        hint = self._follow_hint_of()
        got = None
        try:
            got = self._scan_center(None, arm_follow=False, hint=hint)
        except Exception as exc:
            log("guide-hold scan error: %r" % (exc,))
            got = None
        with self._state_lock:
            if self._guide_hold is not sess:
                return False
        if got is None:
            # In non-centre-watch mode, fall back to scanning near cursor
            if not bool(self.cfg.get("watch_center_only", False)):
                try:
                    cx, cy = get_cursor_pos()
                    target = self.scan_near(cx, cy, None, None)
                    if target is not None:
                        got = (target, cx, cy)
                except Exception:
                    pass
            if got is None:'''

new_guide_scan = '''        hint = self._follow_hint_of()
        got = None
        # Only call _scan_center when watch_center_only is enabled;
        # otherwise fall back to scan_near immediately.
        if bool(self.cfg.get("watch_center_only", False)):
            try:
                got = self._scan_center(None, arm_follow=False, hint=hint)
            except Exception as exc:
                log("guide-hold scan error: %r" % (exc,))
                got = None
        else:
            # Non-centre mode: scan near cursor
            try:
                cx, cy = get_cursor_pos()
                target = self.scan_near(cx, cy, None, None)
                if target is not None:
                    got = (target, cx, cy)
            except Exception:
                pass

        with self._state_lock:
            if self._guide_hold is not sess:
                return False

        if got is None:'''

content = content.replace(old_guide_scan, new_guide_scan)

# ============================================================
# FIX 11: _apply_motion_lead - early return when motion settings off
# ============================================================
old_motion_lead = '''    def _apply_motion_lead(self, nx, ny):
        try:
            lead_ms = float(self.cfg.get("motion_lead_ms", 50))
        except Exception:
            lead_ms = 50.0
        tag = ""
        if bool(self.cfg.get("motion_3d", False)):
            with self._state_lock:
                pred = self._tracker.predict(lead_ms, MOTION_MAX_OFFSET)
            ox, oy = float(pred[0]), float(pred[1])
            if len(pred) > 2:
                try:
                    ratio = float(pred[2])
                    tag = ", scale x%.2f" % ratio \
                        if abs(ratio - 1.0) >= 0.02 else ""
                except (TypeError, ValueError):
                    tag = ""
        else:
            with self._state_lock:
                _obs = list(self._track)
            ox, oy = vision.predict_motion(_obs, lead_ms,
                                           MOTION_MAX_OFFSET)
        if bool(self.cfg.get("motion_camera_comp", True)) \
                and self._reticle_active():
            cdx, cdy = self._camera_lead(lead_ms)
            if cdx or cdy:
                ox -= cdx
                oy -= cdy
                tag += ", camera %+.0f,%+.0f px/s" % (
                    cdx / lead_ms * 1000.0, cdy / lead_ms * 1000.0)
        if abs(ox) + abs(oy) >= 1.5:
            nx = int(round(nx + ox))
            ny = int(round(ny + oy))
            if time.perf_counter() - self._lead_log_at > 5.0:
                self._lead_log_at = time.perf_counter()
                log("Motion lead: %+d,%+d px%s" % (ox, oy, tag))
        return nx, ny'''

new_motion_lead = '''    def _apply_motion_lead(self, nx, ny):
        # Only apply motion lead if motion_predict or motion_3d is enabled
        motion_on = (bool(self.cfg.get("motion_predict", False))
                    or bool(self.cfg.get("motion_3d", False)))
        if not motion_on:
            return nx, ny
        try:
            lead_ms = float(self.cfg.get("motion_lead_ms", 50))
        except Exception:
            lead_ms = 50.0
        tag = ""
        if bool(self.cfg.get("motion_3d", False)):
            with self._state_lock:
                pred = self._tracker.predict(lead_ms, MOTION_MAX_OFFSET)
            ox, oy = float(pred[0]), float(pred[1])
            if len(pred) > 2:
                try:
                    ratio = float(pred[2])
                    tag = ", scale x%.2f" % ratio \
                        if abs(ratio - 1.0) >= 0.02 else ""
                except (TypeError, ValueError):
                    tag = ""
        else:
            with self._state_lock:
                _obs = list(self._track)
            ox, oy = vision.predict_motion(_obs, lead_ms,
                                           MOTION_MAX_OFFSET)
        if bool(self.cfg.get("motion_camera_comp", True)) \
                and self._reticle_active():
            cdx, cdy = self._camera_lead(lead_ms)
            if cdx or cdy:
                ox -= cdx
                oy -= cdy
                tag += ", camera %+.0f,%+.0f px/s" % (
                    cdx / lead_ms * 1000.0, cdy / lead_ms * 1000.0)
        if abs(ox) + abs(oy) >= 1.5:
            nx = int(round(nx + ox))
            ny = int(round(ny + oy))
            if time.perf_counter() - self._lead_log_at > 5.0:
                self._lead_log_at = time.perf_counter()
                log("Motion lead: %+d,%+d px%s" % (ox, oy, tag))
        return nx, ny'''

content = content.replace(old_motion_lead, new_motion_lead)

# ============================================================
# FIX 12: _scan_center - gate subject_focus_bias by subject_priority/subject_only
# ============================================================
old_scan_bias = '''        try:
            hb = float(self.cfg.get("subject_focus_bias", 0) or 0) / 100.0
        except (TypeError, ValueError):
            hb = 0.0
        try:
            subject_ctx = self._subject_context(origin)'''

new_scan_bias = '''        try:
            subject_priority_on = (bool(self.cfg.get("subject_priority", False))
                                  or bool(self.cfg.get("subject_only", False)))
            hb = float(self.cfg.get("subject_focus_bias", 0) or 0) / 100.0
        except (TypeError, ValueError):
            hb = 0.0
        if not subject_priority_on:
            hb = 0.0
        try:
            subject_ctx = self._subject_context(origin)'''

content = content.replace(old_scan_bias, new_scan_bias)

# ============================================================
# FIX 13: _new_hold_session - add rem_x/rem_y for per-session residuals
# ============================================================
old_new_session = '''    def _new_hold_session(self):
        return {"px": 0.0, "py": 0.0, "cx": 0.0, "cy": 0.0,
                "last_t": time.perf_counter(), "last_tap": 0.0}'''

new_new_session = '''    def _new_hold_session(self):
        return {"px": 0.0, "py": 0.0, "cx": 0.0, "cy": 0.0,
                "rem_x": 0.0, "rem_y": 0.0,
                "last_t": time.perf_counter(), "last_tap": 0.0}'''

content = content.replace(old_new_session, new_new_session)

# ============================================================
# FIX 14: _release_holds - clear _guide_hold, _chase_ref, _center_prev, _cam_prev
# ============================================================
old_release = '''        if was_center:
                self._track.clear()
                try:
                    self._tracker.reset()
                except Exception:
                    pass'''

new_release = '''        if was_center:
                self._track.clear()
                try:
                    self._tracker.reset()
                except Exception:
                    pass
                self._chase_ref = None
                self._center_prev = None
                self._cam_prev = None'''

content = content.replace(old_release, new_release)

# ============================================================
# FIX 15: _force_mode - clear _guide_hold alongside _center_hold
# ============================================================
old_force = '''        with self._state_lock:
            self.follow_lock = None
            self._center_follow = None
            self._pick_at = None
            self._chase_ref = None
            self._track.clear()'''

new_force = '''        with self._state_lock:
            self.follow_lock = None
            self._center_follow = None
            self._guide_hold = None
            self._pick_at = None
            self._chase_ref = None
            self._track.clear()'''

content = content.replace(old_force, new_force)

# ============================================================
# FIX 16: pause action - clear _guide_hold
# ============================================================
old_pause = '''        with self._state_lock:
            self.follow_lock = None
            self._center_follow = None
            self._pick_at = None
            self._chase_ref = None


                self.stop_playback()'''

new_pause = '''        with self._state_lock:
            self.follow_lock = None
            self._center_follow = None
            self._guide_hold = None
            self._pick_at = None
            self._chase_ref = None


                self.stop_playback()'''

content = content.replace(old_pause, new_pause)

# ============================================================
# FIX 17: _emergency_release - clear _guide_hold
# ============================================================
old_emergency = '''        with self._state_lock:
            had_hold = self._center_hold is not None
            self._center_hold = None
            if had_hold:
                self._center_hold_tomb = time.perf_counter()
            need_up = self._center_syn_down
            if need_up:
                self._center_syn_down = False'''

new_emergency = '''        with self._state_lock:
            had_hold = self._center_hold is not None
            self._center_hold = None
            self._guide_hold = None
            if had_hold:
                self._center_hold_tomb = time.perf_counter()
            need_up = self._center_syn_down
            if need_up:
                self._center_syn_down = False'''

content = content.replace(old_emergency, new_emergency)

# ============================================================
# FIX 18: input_utils.mount - fix theme regression
# ============================================================
# This will be applied to input_utils.py separately

# ============================================================
# FIX 19: input_backend.py - aim_assist_step reads aim_speed/aim_y_speed_multiplier
# ============================================================
# This will be applied to input_backend.py separately

# ============================================================
# FIX 20: gui.py - add aim_y_speed_multiplier and motion_camera_comp to META and TAB_ADV
# ============================================================
# This will be applied to gui.py separately

# ============================================================
# Apply all changes and save
# ============================================================
with open('assist.py', 'w') as f:
    f.write(content)

print("All fixes applied to assist.py")