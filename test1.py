import picamera
import time
import os
from envirophat import light
from envirophat import leds
from envirophat import motion
index = 0

cam = picamera.PiCamera()

# check recorded hikes currently on card




for i in range(3):
    leds.off()
    time.sleep(1)
    leds.on()
    time.sleep(0.5)

while(1):
    print(light.rgb())
    time.sleep(2)


    name = 'Hikex' + str(index) + '.jpg'
    cam.capture(name)
    index = index + 1
