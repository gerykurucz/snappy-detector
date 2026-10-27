with open('assist.py', 'r') as f:
    content = f.read()

# Fix the _atomic_click function - remove the pass and ensure proper structure
old = '''    def _atomic_click(self, index=0):
        fn = getattr(self._input, "send_click", None)
        if fn is None:
            return False
        try:
            return bool(fn(index))
        except Exception as exc:
            log("atomic click error: %r" % (exc,))
            return False
        pass

    def _atomic_click'''

new = '''    def _atomic_click(self, index=0):
        fn = getattr(self._input, "send_click", None)
        if fn is None:
            return False
        try:
            return bool(fn(index))
        except Exception as exc:
            log("atomic click error: %r" % (exc,))
            return False

    def _atomic_click'''

if old in content:
    content = content.replace(old, new)
    with open('assist.py', 'w') as f:
        f.write(content)
    print('Replaced successfully')
else:
    print('Not found')
    idx = content.find('def _atomic_click')
    if idx >= 0:
        print(repr(content[idx:idx+800]))