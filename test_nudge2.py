import interception
import time
import struct
from win32rt import get_cursor_pos

ctx = interception.Interception()
print('Devices:', len(ctx.devices))

# Find a mouse device
mouse_dev = None
for d in ctx.devices:
    if not d.is_keyboard:
        mouse_dev = d
        break

if mouse_dev is None:
    print("No mouse device found")
    exit()

print(f'Using mouse device')

# Set the parser to MouseStroke for this device
mouse_dev.set_parser(interception.MouseStroke)

# Test nudge
print('Testing nudge (10, 10)...')
x1, y1 = get_cursor_pos()

# Create a mouse stroke
stroke = interception.MouseStroke(
    flags=0,
    button_flags=0,
    button_data=0,
    x=10,
    y=10,
    information=0
)
result = mouse_dev.send(stroke)
print(f'Nudge result: {result}')
time.sleep(0.1)
x2, y2 = get_cursor_pos()
print(f'Delta: ({x2-x1}, {y2-y1})')