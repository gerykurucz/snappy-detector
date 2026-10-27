with open('assist.py', 'r') as f:
    content = f.read()

# Find and replace the section in _center_hold_cycle
old = '''if self._unibot_aim():
                # Apply EMA smoothing like the non-unibot path
                tx, ty = float(target.dx), float(target.dy)
                dt = min(max(now - sess.get("last_t", now), 0.001), 0.1)
                alpha = 1.0 - math.exp(-dt / 0.05)
                alpha = min(max(alpha, 0.0), 1.0)
                sess["px"] += alpha * (tx - sess["px"])
                sess["py"] += alpha * (ty - sess["py"])

                # Compute step with cap_step
                out_x = sess["px"] + sess.get("cx", 0.0)
                out_y = sess["py"] + sess.get("cy", 0.0)
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
                    self._nudge(ix, iy, view=True, continuous=True)
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
                    tx, ty = float(target.dx), float(target.dy)'''

new = '''if self._unibot_aim():
                # Apply EMA smoothing like the non-unibot path
                tx, ty = float(target.dx), float(target.dy)
                # Apply snap_offset to the raw target offset
                tx, ty = self._apply_snap_offset(tx, ty)
                dt = min(max(now - sess.get("last_t", now), 0.001), 0.1)
                alpha = 1.0 - math.exp(-dt / 0.05)
                alpha = min(max(alpha, 0.0), 1.0)
                sess["px"] += alpha * (tx - sess["px"])
                sess["py"] += alpha * (ty - sess["py"])

                # Compute step with cap_step
                out_x = sess["px"] + sess.get("cx", 0.0)
                out_y = sess["py"] + sess.get("cy", 0.0)
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
                    self._nudge(ix, iy, view=True, continuous=True)
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

if old in content:
    content = content.replace(old, new)
    with open('assist.py', 'w') as f:
        f.write(content)
    print('Replaced successfully')
else:
    print('Not found - trying without the last line')
    # Try to find what's actually there
    idx = content.find('if self._unibot_aim():')
    print(content[idx:idx+2200])