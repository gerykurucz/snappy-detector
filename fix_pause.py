with open('assist.py', 'r') as f:
    content = f.read()

# Find and replace the section in pause action
old = '''elif action == "pause":
            if self.running:
                self.running = False
                self._drain_queue()
                self._emergency_release()
                self._pad_release_all()
                with self._state_lock:
                    self.follow_lock = None
                    self._center_follow = None
                    self._pick_at = None
                    self._chase_ref = None



                self.stop_playback()'''

new = '''elif action == "pause":
            if self.running:
                self.running = False
                self._drain_queue()
                self._emergency_release()
                self._pad_release_all()
                with self._state_lock:
                    self.follow_lock = None
                    self._center_follow = None
                    self._guide_hold = None
                    self._pick_at = None
                    self._chase_ref = None



                self.stop_playback()'''

if old in content:
    content = content.replace(old, new)
    with open('assist.py', 'w') as f:
        f.write(content)
    print('Replaced successfully')
else:
    print('Not found')