with open('assist.py', 'r') as f:
    content = f.read()

# Find the _nudge function and replace it entirely
old = '''def _nudge(self, dx, dy, view=False, sess=None):
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
                self._view_moved = True
        pass

    def _atomic_click'''

new = '''def _nudge(self, dx, dy, view=False, sess=None):
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
                self._view_moved = True

    def _atomic_click'''

if old in content:
    content = content.replace(old, new)
    with open('assist.py', 'w') as f:
        f.write(content)
    print('Replaced successfully')
else:
    print('Not found')
    # Debug
    idx = content.find('def _nudge')
    if idx >= 0:
        print('Found at', idx)
        print(repr(content[idx:idx+2000]))
    else:
        print('Not found at all')