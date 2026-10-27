with open('assist.py', 'r') as f:
    content = f.read()

# Fix the right stick nudge in _pad_drive
old = '''rx, ry = gamepad.apply_deadzone(state.rx, state.ry,
                                        settings["deadzone"])
        if (rx or ry) and self._pad_click_down:
            self._nudge(int(round(rx * 24.0)), int(round(-ry * 24.0)),
                        view=True, continuous=True)

        click_'''

new = '''rx, ry = gamepad.apply_deadzone(state.rx, state.ry,
                                        settings["deadzone"])
        if (rx or ry) and self._pad_click_down:
            # Apply aim_speed and cap_step like other view steering paths
            sp = self._aim_speed()
            dx = int(round(rx * 24.0 * sp))
            dy = int(round(-ry * 24.0 * sp))
            cap = self._max_step_cfg()
            dx, dy = self._cap_step(dx, dy, cap)
            if dx or dy:
                self._nudge(dx, dy, view=True, continuous=True)

        click_'''

if old in content:
    content = content.replace(old, new)
    with open('assist.py', 'w') as f:
        f.write(content)
    print('Replaced successfully')
else:
    print('Not found')