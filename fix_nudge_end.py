with open('assist.py', 'r') as f:
    content = f.read()

# Fix the end of _nudge function - add a pass statement at function body level after the if view block
old = '''        if view:
            with self._state_lock:
                self._view_moved = True
    def _atomic_click'''

new = '''        if view:
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