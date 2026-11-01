import interception
import time
from win32rt import get_cursor_pos

ctx = interception.Interception()
print('Devices:', len(ctx.devices))

# Find a mouse device
mouse_dev = None
for d in ctx.devices:
    if d.is_mouse:
        mouse_dev = d
        break

if mouse_dev is None:
    print("No mouse device found")
    exit()

print(f'Using mouse device: {mouse_dev}')

# Test nudge
print('Testing nudge (10, 10)...')
x1, y1 = get_cursor_pos()
result = mouse_dev.send(interception.MouseStroke(flags=0, button_flags=0, button_data=0, x=10, y=10))
print(f'Nudge result: {result}')
time.sleep(0.1)
x2, y2 = get_cursor_pos()
print(f'Delta: ({x2-x1}, {y2-y1})')