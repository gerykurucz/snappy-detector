with open('assist.py', 'r') as f:
    content = f.read()

# Find the _nudge function and replace it entirely - use a more flexible pattern
old = '''        if view:
            with self._state_lock:
                self._view_moved = True
        pass

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
    print('Pattern not found, trying alternative...')
    # Try without the blank line in old
    old2 = '''        if view:
            with self._state_lock:
                self._view_moved = True
        pass
    def _atomic_click'''
    if old2 in content:
        content = content.replace(old2, new)
        with open('assist.py', 'w') as f:
            f.write(content)
        print('Replaced with alt pattern')
    else:
        print('Still not found')
        idx = content.find('def _nudge')
        print(repr(content[idx+3000:idx+3500]))