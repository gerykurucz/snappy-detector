import numpy as np
import cv2
bg = np.full((480, 640, 3), 128, dtype=np.uint8)
cv2.imwrite('temp_bg.jpg', bg)
print('Created temp_bg.jpg')