with open('assist.py', 'r') as f:
    content = f.read()

old = '''def _nudge(self, dx, dy, view=False, continuous=False, sess=None):
        # The continuous parameter is passed to aim_assist_step for backends
        # that need it (e.g., virtual gamepad hold). Default is False.
        if view and self._unibot_aim():'''

new = '''def _nudge(self, dx, dy, view=False, sess=None):
        # sess: optional hold session dict for per-session residuals
        if view and self._unibot_aim():'''

if old in content:
    content = content.replace(old, new)
    with open('assist.py', 'w') as f:
        f.write(content)
    print('Replaced successfully')
else:
    print('Not found')