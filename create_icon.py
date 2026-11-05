from PIL import Image, ImageDraw

img = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# Outer circle - Snappy blue
draw.ellipse([16, 16, 240, 240], fill=(0, 120, 255, 255))

# Center crosshair shape - white
draw.rectangle([80, 100, 176, 156], fill=(255, 255, 255, 255))
draw.rectangle([108, 72, 148, 184], fill=(255, 255, 255, 255))

# Center dot - blue
draw.ellipse([112, 112, 144, 144], fill=(0, 120, 255, 255))

img.save('assets/icon.ico', format='ICO', sizes=[(256,256),(128,128),(64,64),(48,48),(32,32),(16,16)])
print('Created assets/icon.ico')