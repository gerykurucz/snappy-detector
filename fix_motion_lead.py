with open('assist.py', 'r') as f:
    content = f.read()

# Fix non-unibot branch in _center_hold_cycle - add motion lead
old = '''# Apply snap_offset
            tx, ty = self._apply_snap_offset(tx, ty)


            alpha = 1.0 - math.exp(-dt / 0.05)
            alpha = min(max(alpha, 0.0), 1.0)
            sess["px"] += alpha * (tx - sess["px"])
            sess["py"] += alpha * (ty - sess["py"])


        out_x = sess["px"] + sess.get("cx", 0.0)'''

new = '''# Apply snap_offset
            tx, ty = self._apply_snap_offset(tx, ty)


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


        out_x = sess["px"] + sess.get("cx", 0.0)'''

if old in content:
    content = content.replace(old, new)
    with open('assist.py', 'w') as f:
        f.write(content)
    print('Replaced successfully')
else:
    print('Not found')