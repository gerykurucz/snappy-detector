with open('assist.py', 'r') as f:
    content = f.read()

# Insert _atomic_click before _click_here
atomic_click_code = '''    def _atomic_click(self, index=0):
        fn = getattr(self._input, "send_click", None)
        if fn is None:
            return False
        try:
            return bool(fn(index))
        except Exception as exc:
            log("atomic click error: %r" % (exc,))
            return False


'''

idx = content.find('def _click_here')
if idx >= 0:
    content = content[:idx] + atomic_click_code + content[idx:]
    with open('assist.py', 'w') as f:
        f.write(content)
    print('Restored _atomic_click successfully')
else:
    print('Could not find _click_here')