import sys
sys.path.insert(0, '.')
from input_backend import Win32Backend
import time

print('Testing Win32 backend...')
wb = Win32Backend()
print(f'Win32Backend degraded: {getattr(wb, "_degraded", "no attr")}')
print(f'Win32Backend reason: {getattr(wb, "_reason", "no attr")}')

# Disable acceleration
from input_backend import _set_mouse_accel
result = _set_mouse_accel(False)
print(f'Acceleration disabled: {result}')

# Test nudge
print('Testing nudge (10, 10)...')
result = wb.nudge(10, 10)
print(f'Nudge result: {result}')

# Check cursor position before/after
from win32rt import get_cursor_pos
x1, y1 = get_cursor_pos()
print(f'Before: ({x1}, {y1})')

time.sleep(0.1)

wb.nudge(10, 10)
time.sleep(0.1)
x2, y2 = get_cursor_pos()
print(f'After: ({x2}, {y2})')
print(f'Delta: ({x2-x1}, {y2-y1})')