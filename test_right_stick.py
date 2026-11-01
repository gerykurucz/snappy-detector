import sys
sys.path.insert(0, '.')
from input_backend import ViGemPadBackend
import time

backend = ViGemPadBackend()
print(f'ViGEm backend degraded: {backend._degraded}')

if backend.ok_for_pad():
    print('ViGEm backend OK')
    # Test right stick aim - rx=4, ry=5
    result = backend.axis(4, 1000)
    print(f'RX axis result: {result}')
    result = backend.axis(5, -1000)
    print(f'RY axis result: {result}')
    result = backend.flush()
    print(f'Flush result: {result}')
else:
    print('ViGEm backend not available')