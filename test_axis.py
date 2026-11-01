import sys
sys.path.insert(0, '.')
import input_utils as gamepad
print('PAD_AXES:', gamepad.PAD_AXES)
print('normalize_axis(5, 1000):', gamepad.normalise_axis(5, 1000))
print('normalize_axis(4, -1000):', gamepad.normalise_axis(4, -1000))