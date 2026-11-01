import interception
import time
from win32rt import get_cursor_pos

ctx = interception.Interception()

# Find mouse device by filtering for is_mouse
mouse_dev = None
for dev in ctx.devices:
    if dev.is_mouse:
        mouse_dev = dev
        break

if mouse_dev is None:
    print("No mouse device found")
    exit(1)

print('Mouse device:', mouse_dev)
print('Mouse parser:', type(mouse_dev._parser))

# Test nudge
x1, y1 = get_cursor_pos()
stroke = mouse_dev._parser(flags=0, button_flags=0, button_data=0, x=10, y=10)
result = ctx.send(mouse_dev.device_id, stroke)
print(f'Nudge result: {result}')
time.sleep(0.1)
x2, y2 = get_cursor_pos()
print(f'Delta: ({x2-x1}, {y2-y1})')