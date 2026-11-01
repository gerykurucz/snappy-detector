import interception
from interception.strokes import MouseStroke
import struct

print("Testing MouseStroke creation...")

# Create a stroke with all required parameters
stroke = interception.MouseStroke(
    flags=0,
    button_flags=0,
    button_data=0,
    x=10,
    y=20
)

# Pack it
data = stroke.data
print(f"Packed length: {len(stroke.data)}")

# Unpack with the format
unpacked = struct.unpack('<HHHHIiiI', stroke.data)
print(f'Unpacked: {unpacked}')

# Test round-trip
stroke2 = MouseStroke.parse(stroke.data)
print(f'Parsed x: {stroke2.x}, y: {stroke2.y}')

# Also test the format used in the library
print(f"Format: {MouseStroke.format}")