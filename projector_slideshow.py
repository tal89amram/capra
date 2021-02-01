#!/usr/bin/env python3

# Slideshow application for the Capra Explorer
# Allows passing through photos with a smooth fading animation

# FIXME
# HACK
# TODO
# REVIEW
# REFACTOR

# Imports
from classes.capra_data_types import Picture, Hike
from classes.sql_controller import SQLController
from classes.sql_statements import SQLStatements
from classes.ui_components import *

from PyQt5.QtGui import *
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
import PIL
from PIL import ImageTk, Image, ImageQt
# from PIL.ImageQt import ImageQt
# from RPi import GPIO
from datetime import datetime
from enum import Enum, IntEnum, unique, auto

import math
import os
import platform
import psutil
import sys
import time
import traceback

# Database Location
if platform.system() == 'Darwin' or platform.system() == 'Windows':
    print('We on a Mac or PC!')
    # Multiple hikes, but no color data
    # DB = '~/Developer/eds/capra/capra-storage/capra_projector_dec2020_min_hike10_dest.db'

    # Color data, but only hike 10
    DB = '/Users/Jordan/Developer/eds/capra/capra-storage/capra_projector_dec2020_min_hike10_dest.db'
    # DB = '/home/pi/Pictures/capra-projector.db'
    # PATH = '/home/pi/Pictures'

elif platform.system() == 'Linux':
    print('We on a Raspberry Pi!')
    # DB = '/media/pi/capra-hd/capra_projector.db'
    # PATH = '/media/pi/capra-hd'
    DB = '/home/pi/capra-storage-demo/capra_projector.db'
    PATH = 'home/pi/capra-storage/demo'

# Filewide Statuses
# --- Hardware
rotaryCounter = 0
# --- UI
# status_orientation = 0      # (0)landscape (1)portrait
# status_setting = 0          # (0)hike (1)global
# status_mode = 0             # (0)time (1)altitude (2)color
isReadyForNewPicture = True


class StatusOrientation(IntEnum):
    LANDSCAPE = 0
    PORTRAIT = 1


class StatusPlayPause(IntEnum):
    PAUSE = 0
    PLAY = 1


class StatusScope(IntEnum):
    HIKE = 0
    GLOBAL = 1


class StatusMode(IntEnum):
    __order__ = 'TIME ALTITUDE COLOR'
    TIME = 0
    ALTITUDE = 1
    COLOR = 2


# Global status values -- they need to have global file scope due to the modification by the hardware
orientation = StatusOrientation.LANDSCAPE
scope = StatusScope.HIKE
mode = StatusMode.TIME
playpause = StatusPlayPause.PLAY


# Threading Infrastructure
class WorkerSignals(QObject):
    '''
    Defines the signals available from a running worker thread.
    Supported signals are:

    finished
        No data
    error
        `tuple` (exctype, value, traceback.format_exc() )
    result
        `object` data returned from processing, anything
    progress
        `int` indicating % progress
    '''
    finished = pyqtSignal()
    error = pyqtSignal(tuple)
    result = pyqtSignal(object, object)
    # results = pyqtSignal(object, int)
    progress = pyqtSignal(int)


'''
class Worker(QRunnable):
    def __init__(self, *args, **kwargs):
        super(Worker, self).__init__()

        self.clk = 23
        self.cnt = 24

        GPIO.setup(self.clk, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        GPIO.setup(self.cnt, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

        self.clkLastState = GPIO.input(self.clk)

        self.count = 0
        self.signals = WorkerSignals()

    def run(self):
        while True:
            self.clkState = GPIO.input(self.clk)
            self.cntState = GPIO.input(self.cnt)
            if self.clkState != self.clkLastState:
                if self.cntState != self.clkState:
                    self.count += 1
                    # self.signals.result.emit(self.count)
                else:
                    self.count -= 1
                    # self.signals.result.emit(self.count)
            self.clkLastState = self.clkState
            # print(self.count)
'''


# Custom Hardware Controls
class RotaryEncoder(QRunnable):
    def __init__(self, PIN_A: int, PIN_B: int, *args, **kwargs):
        super(RotaryEncoder, self).__init__()

        self.RoAPin = PIN_A
        self.RoBPin = PIN_B

        GPIO.setup(self.RoAPin, GPIO.IN)
        GPIO.setup(self.RoBPin, GPIO.IN)

        self.signals = WorkerSignals()

        self.flag = 0
        self.Last_RoB_Status = 0
        self.Current_RoB_Status = 0
        self.Last_Direction = 0         # 0 for backward, 1 for forward
        self.Current_Direction = 0      # 0 for backward, 1 for forward

        self.MAXQUEUE = 7
        self.lst = list()
        self.last_time = datetime.now().timestamp()
        self.speedText = ""
        self.average = 0
        self.dt = 0
        self.multFactor = 1

    def calculate_speed(self):
        self.dt = round(datetime.now().timestamp() - self.last_time, 5)
        # data sanitation: clean up random stray values that are extremely low
        if self.dt < .005:
            self.dt = .1

        if len(self.lst) > self.MAXQUEUE:
            self.lst.pop()
        self.lst.insert(0, self.dt)
        self.average = sum(self.lst) / len(self.lst)

        self.last_time = datetime.now().timestamp()

        #   .3      .07     .02
        #   .1      .05     .02
        if self.average >= .3:
            self.speedText = "slow"
        elif self.average >= .07 and self.average < .3:
            self.speedText = "medium"
        elif self.average >= .02 and self.average < .07:
            self.speedText = "fast"
        else:
            self.speedText = "super-duper fast"

        return self.average, self.speedText, self.dt

    # Starting logic comes from the following project:
    # https://www.sunfounder.com/learn/Super_Kit_V2_for_RaspberryPi/lesson-8-rotary-encoder-super-kit-for-raspberrypi.html
    def rotaryTurn(self):
        global rotaryCounter

        self.Last_RoB_Status = GPIO.input(self.RoBPin)

        while(not GPIO.input(self.RoAPin)):
            self.Current_RoB_Status = GPIO.input(self.RoBPin)
            self.flag = 1
        if self.flag == 1:
            self.flag = 0

            if (self.Last_RoB_Status == 0) and (self.Current_RoB_Status == 1):
                self.Current_Direction = 1
            if (self.Last_RoB_Status == 1) and (self.Current_RoB_Status == 0):
                self.Current_Direction = 0

            if (self.Current_Direction != self.Last_Direction):
                self.lst.clear()

            self.average, self.speedText, self.dt = self.calculate_speed()

            speed = 0.5 / self.dt
            self.multFactor = int(0.5 / self.average)
            if (self.multFactor < 1 or self.Current_Direction != self.Last_Direction):
                self.multFactor = 1

            if (self.Current_Direction == 1):
                rotaryCounter = rotaryCounter + 1 * self.multFactor
            else:
                rotaryCounter = rotaryCounter - 1 * self.multFactor

            self.Last_Direction = self.Current_Direction
            print('rotaryCounter: {g}, diff_time: {d:.4f}, speed: {s:.2f}, MultFactor: {a:.2f} ({st})'.format(g=rotaryCounter, d=self.dt, s=speed, a=self.multFactor, st=self.speedText))

            # TODO -- remove both of these lines
            # Resetting counter will happen in Main UI Thread
            self.signals.result.emit(rotaryCounter)
            rotaryCounter = 0

    def clear(self, ev=None):
        global rotaryCounter
        rotaryCounter = 0
        print('rotaryCounter = {g}'.format(g=rotaryCounter))
        time.sleep(1)

    def run(self):
        while True:
            self.rotaryTurn()


# Uses global status variable to ensure there are no double presses for hardware buttons
class HardwareButton(QRunnable):
    def __init__(self, PIN: int, *args, **kwargs):
        super(HardwareButton, self).__init__()

        self.status = False
        self.PIN = PIN
        GPIO.setup(self.PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        self.signals = WorkerSignals()

    def run(self):
        while True:
            if GPIO.input(self.PIN) == False:         # Button press detected
                if self.status == False:              # Button was just OFF
                    self.signals.result.emit(True)
                    self.status = True              # Update the status to ON
            else:                                   # Button is not pressed
                self.status = False
            time.sleep(0.05)


# Continually tries fading the next image into the current image
class ImageFader(QRunnable):
    def __init__(self, current_path, *args, **kwargs):
        super(ImageFader, self).__init__()

        # Needed setup
        self.alpha = 0
        self.signals = WorkerSignals()
        self.current_raw = Image.open(current_path, 'r')
        self.next_raw = Image.open(current_path, 'r')

        # Testing
        self.text_current = 'currently'
        self.text_next = 'nextly'
        self.next_value = 0
        self.inside_data = 'shhhh, it is a secret'

    '''
    def run(self):
        while True:
            print(self.text_current)
            time.sleep(0.5)
            # print(self.text_next)
            print(self.next_value)
            time.sleep(3)
            self.signals.result.emit(self.inside_data)
    '''

    def increment_next(self):
        self.next_value += 1

    # Receives a new "next image" which is consequently faded into the current
    # image. If this process happens in the middle of a fade, then the new image
    # will be faded into this already faded together photo
    def set_next_image(self, path):
        self.next_raw = Image.open(path, 'r')
        self.alpha = 0.25

    # Continually runs blending together
    def run(self):
        while True:
            if self.alpha < 0.75:
                self.current_raw = Image.blend(self.current_raw, self.next_raw, self.alpha)
                # self.alpha = self.alpha + 0.02  # Rougly 20 frames until the old picture is blurred out
                self.alpha += 0.04

                self.signals.result.emit(self.current_raw, self.alpha)
                if self.alpha >= 0.75:
                    self.signals.finished.emit()
            time.sleep(0.1)  # 1/20frames = 0.05

    '''
    def run(self):
        if self.alpha < 1.0:
            self.current_raw = Image.blend(self.current_raw, self.next_raw, self.alpha)

            # self.current_raw.save("assets/blended.jpg")
            # self.pictureLandscape.update_image('assets/blended.jpg')
            self.pictureLandscape.update_pixmap(self.current_raw)

            # TODO - how long the last image hangs around
            #   Lower - the longer a piece stays on screen
            #   Higher - the faster the bit of an image leaves
            # self.alpha = self.alpha + 0.0417
            # self.alpha = self.alpha + 0.0209
            self.alpha = self.alpha + 0.1

        # TODO - Change this value to affect the speed of the fade
        #   Lower the number the quicker the fade
        #   Higher the number the slower the fade
        # root.after(40, self.fade_image)
        time.sleep(0.05)

        while True:
            if GPIO.input(self.PIN) == False:         # Button press detected
                if self.status == False:              # Button was just OFF
                    self.signals.result.emit(True)
                    self.status = True              # Update the status to ON
            else:                                   # Button is not pressed
                self.status = False
            time.sleep(0.05)
    '''


# Custom Widgets & UI Elements
# TODO - add rotatable widget container

class MainWindow(QMainWindow):
    def __init__(self, *args, **kwargs):
        super(MainWindow, self).__init__(*args, **kwargs)

        # TODO - make sure this actually works
        # Images from Pillow
        self.alpha = 0
        # self.current_raw = Image.open('assets/5.jpg', 'r')
        # self.next_raw = Image.open('assets/7.jpg', 'r')
        # self.index = 1486

        # TODO - implement with the new Db structure
        self.setupDB()
        self.setupWindowLayout()
        self.setupUI()
        # REMOVE - as they aren't needed anymore (I think)
        # self.setupLandscapeUI()
        # self.setupVerticalUI()

        if platform.system() == 'Darwin' or platform.system() == 'Windows':
            print('---- No GPIO setup needed ----')
            self.setupSoftwareThreads()
        elif platform.system() == 'Linux':
            # self.index = 1
            # self.seconds = 0
            self.setupGPIO()
            self.setupThreads()
            self.setupSoftwareThreads()

        # TODO - decide if this is needed
        # Updates the UI's picture for the first time
        self.updateUIPicture()

        # Show the MainWindow
        if platform.system() == 'Darwin' or platform.system() == 'Windows':
            self.show()
        elif platform.system() == 'Linux':
            self.showFullScreen()

    def setupDB(self):
        self.sql_controller = SQLController(database=DB)
        self.picture = self.sql_controller.get_first_time_picture()
        self.picture.print_obj_mvp()

        # self.picture_starter = self.sql_controller.get_first_time_picture_in_hike(10)

    # UI Setup
    def setupWindowLayout(self):
        self.setWindowTitle("Capra Explorer Slideshow")
        self.setGeometry(0, 300, 1280, 720)
        # self.setStyleSheet("background-color: gray;")

        pagelayout = QVBoxLayout()
        pagelayout.setContentsMargins(0, 0, 0, 0)

        self.stacklayout = QStackedLayout()
        pagelayout.addLayout(self.stacklayout)

        # Landscape view
        # Sets the initial picture to the first selected image from the DB
        self.pictureLandscape = UIImage(self.picture.camera2)
        self.stacklayout.addWidget(self.pictureLandscape)

        # Vertical view
        verticallayout = QHBoxLayout()
        verticallayout.setContentsMargins(0, 0, 0, 0)
        verticallayout.setSpacing(0)
        verticallayout.addWidget(UIImage('assets/cam1.jpg'))
        verticallayout.addWidget(UIImage('assets/cam2.jpg'))
        # TODO - this will eventually be images from DB, but they need to be
        # the proper size or else it'll mess up the size of the window
        # verticallayout.addWidget(UIImage(self.picture.camera2))
        verticallayout.addWidget(UIImage('assets/cam3.jpg'))

        verticalWidget = QWidget()
        verticalWidget.setLayout(verticallayout)
        self.stacklayout.addWidget(verticalWidget)

        # Add central widget
        centralWidget = QWidget()
        centralWidget.setLayout(pagelayout)
        self.setCentralWidget(centralWidget)

        '''
        app = QtWidgets.QApplication(sys.argv)
        screen = app.primaryScreen()
        print('Screen: %s' % screen.name())
        size = screen.size()
        print('Size: %d x %d' % (size.width(), size.height()))
        rect = screen.availableGeometry()
        print('Available: %d x %d' % (rect.width(), rect.height()))
        '''

    def setupUI(self):
        # self.topUnderlay = QLabel(self)
        # topUnderlayImg = QPixmap("assets/TopUnderlay.png")
        # self.topUnderlay.setPixmap(topUnderlayImg)
        # self.topUnderlay.setAlignment(Qt.AlignCenter)
        # self.topUnderlay.setGeometry(0, 0, 1280, 187)

        # self.topUnderlay = UIImageOverlay(self, 'assets/TopUnderlay.png')
        # self.topUnderlay = UIImageOverlay(self, 'assets/bottom.png')
        self.topUnderlay = UIUnderlay(self)

        self.modeOverlay = UIModeOverlay(self, 'assets/Time@1x.png', mode)
        self.leftLabel = UILabelTop(self, '', Qt.AlignLeft)

        # print(self.leftLabel.__class__.__bases__)
        self.rightLabel = UILabelTop(self, '', Qt.AlignRight)
        # print(dir(self.rightLabel))
        self.centerLabel = UILabelTopCenter(self, '', '')

        self.timerFadeOutUI = QTimer()
        self.timerFadeOutUI.setSingleShot(True)
        self.timerFadeOutUI.timeout.connect(self._fadeOutUI)

    def setupGPIO(self):
        # Set the GPIO mode, alternative is GPIO.BOARD
        GPIO.setmode(GPIO.BCM)

        # Hardware Pins
        self.PIN_ROTARY_A = 23
        self.PIN_ROTARY_B = 24
        self.PIN_ROTARY_BUTT = 25

        self.PIN_MODE = 20
        self.PIN_PREV = 6
        self.PIN_PLAY_PAUSE = 5
        self.PIN_NEXT = 13

        # Accelerometer

        # NeoPixels

        # LED indicators

    # Setup threads to check for hardware changes
    def setupThreads(self):
        self.threadpool = QThreadPool()
        self.threadpool.setMaxThreadCount(7)  # TODO - change if more threads are needed

        # Rotary Encoder
        rotaryEncoder = RotaryEncoder(self.PIN_ROTARY_A, self.PIN_ROTARY_B)
        rotaryEncoder.signals.result.connect(self.rotary_changed)
        self.threadpool.start(rotaryEncoder)

        buttonEncoder = HardwareButton(self.PIN_ROTARY_BUTT)
        buttonEncoder.signals.result.connect(self.pressed_encoder)
        self.threadpool.start(buttonEncoder)

        # Buttons
        buttonMode = HardwareButton(self.PIN_MODE)
        buttonMode.signals.result.connect(self.pressed_mode)
        self.threadpool.start(buttonMode)

        buttonPrev = HardwareButton(self.PIN_PREV)
        buttonPrev.signals.result.connect(self.pressed_prev)
        self.threadpool.start(buttonPrev)

        buttonPlayPause = HardwareButton(self.PIN_PLAY_PAUSE)
        buttonPlayPause.signals.result.connect(self.pressed_play_pause)
        self.threadpool.start(buttonPlayPause)

        buttonNext = HardwareButton(self.PIN_NEXT)
        buttonNext.signals.result.connect(self.pressed_next)
        self.threadpool.start(buttonNext)

    def setupSoftwareThreads(self):
        self.threadpoolSoftware = QThreadPool()
        self.threadpoolSoftware.setMaxThreadCount(2)  # TODO - change if more threads are needed

        # Software Threads
        self.imageFader = ImageFader(self.picture.camera2)
        self.imageFader.signals.result.connect(self.load_new_image)
        self.imageFader.signals.finished.connect(self.finished_image_fade)
        self.threadpoolSoftware.start(self.imageFader)

    # UI Callbacks from bg threads
    # Loads the newly faded image from the background thread into the image container
    def load_new_image(self, result, value):
        # print(value)
        # self.pictureLandscape.update_pixmap(self.current_raw)
        self.pictureLandscape.update_pixmap(result)

    # The new image has been faded onto the screen, now fade out all the UI components
    def finished_image_fade(self):
        print('\ndef finished_image_fade() -- result emitted')
        self.timerFadeOutUI.start(1000)

        # Hide the following UI
        # self.topUnderlay.fadeOut()
        # TODO - add back in
        # self.leftLabel.fadeOut()
        # self.rightLabel.doAnimation()  # works - add back in
        # self.centerLabel.fadeOut()  # works - add back in

        # = UILabelTop(self, '', Qt.AlignLeft)
        # self.rightLabel = UILabelTop(self, '', Qt.AlignRight)
        # self.centerLabel

    def _fadeOutUI(self):
        self.topUnderlay.fadeOut()
        self.rightLabel.fadeOut()
        self.leftLabel.fadeOut()
        self.centerLabel.fadeOut()

    '''
    # Animations
    def doAnimation(self):
        self.anim = QPropertyAnimation(self.topUnderlay, b"geometry")
        self.anim.setDuration(400)
        self.anim.setStartValue(QRect(0, 0, 1280, 110))
        self.anim.setEndValue(QRect(0, 600, 1280, 110))
        self.anim.start()

        fadeEffect = QGraphicsOpacityEffect()
        self.topUnderlay.setGraphicsEffect(fadeEffect)
        self.anim2 = QPropertyAnimation(fadeEffect, b"opacity")
        # self.anim2 = QPropertyAnimation(self.label, b"windowOpacity")
        self.anim2.setStartValue(0)
        self.anim2.setEndValue(1)
        self.anim2.setDuration(400)
        self.anim2.start()

        self.update()
    '''

    '''
    def setupLandscapeUI(self):
        # Image
        self.imgLabel = QLabel()

        img = QPixmap(self.buildLandscape(2561))
        self.imgLabel.setPixmap(img)
        self.grid.addWidget(self.imgLabel)

        # Label overlay
        testLabel = QLabel('Time Mode', self)
        testLabel.move(1150, 10)

        # Icon overlay
        # modeBg = QLabel(self)
        # modeBg.setStyleSheet("background-color: rgba(0, 0, 0, 0.3)")
        # modeBg.setGeometry(0, 0, 1280, 720)

        # modeImg = QPixmap('/home/pi/capra/icons/time.png')
        # modeLabel = QLabel(self)  # need the self to set position absolutely
        # modeLabel.setPixmap(modeImg)
        # modeLabel.setGeometry(540, 260, 350, 200)  # left,top,w,h
    '''

    '''
    def changeLandscapeUI(self, picture: Picture):
        # Image
        landscape = picture.camera_landscape
        temp = [x.strip() for x in landscape.split('/')]
        path = '/home/pi/capra-storage-demo/{h}/{jpg}'.format(h=temp[4], jpg=temp[5])
        # print(path)

        self.img = QPixmap(path)
        self.imgLabel.setPixmap(self.img)
    '''

    # def setupVerticalUI(self):
    #     self.img = QPixmap(self.buildFile(2561))

    #     self.grid.addWidget(self.indexLabel, 3, 1)
    #     self.grid.addWidget(self.button, 4, 1)
    #     self.grid.addWidget(self.timerLabel, 2, 1)
    #     self.grid.addWidget(self.workerThreadLabel, 5, 1)
    #     self.addWidget(self.modeLabel)

    # UI Interactions
    def changeMode(self):
        # global status_mode
        # status_mode = (status_mode + 1) % 3
        # print('New status: %d' % status_mode)

        global mode
        mode = StatusMode((mode.value + 1) % 3)
        # print(mode.value)
        # newval = mode.value + 1
        # print(newval)

        if mode == StatusMode.TIME:
            print(mode)
            self.modeOverlay.setTime()
        elif mode == StatusMode.ALTITUDE:
            print(mode)
            self.modeOverlay.setAltitude()
        elif mode == StatusMode.COLOR:
            print(mode)
            self.modeOverlay.setColor()

    def setLandscape(self):
        print('setLandscape()')
        global orientation
        orientation = StatusOrientation.LANDSCAPE
        print(orientation.value)
        self.stacklayout.setCurrentIndex(orientation)

    def setVertical(self):
        print('setVertical()')
        global orientation
        orientation = StatusOrientation.PORTRAIT
        print(orientation.value)
        self.stacklayout.setCurrentIndex(orientation)

    def updateUIPicture(self):
        # Sending to the bg thread - this begins another fading process
        # self.topUnderlay.fadeIn()
        # self.topUnderlay.show()

        self.imageFader.set_next_image(self.picture.camera2)

        # TODO - add back in
        # self.rightLabel.show2()
        # self.leftLabel.show()


        # self.printCurrentMemoryUsage()


        # timeText = self.picture.
        # self.rightLabel.setPrimaryText(self.picture.altitude)
        # self.centerLabel.setPrimaryText(timeText)

        # self.rightLabel

        # self.modeOverlay = UIModeOverlay(self, 'assets/Time@1x.png', mode)
        # self.leftLabel = UILabelTop(self, 'HIKE 10', Qt.AlignLeft)
        # self.rightLabel = UILabelTop(self, 'JULY, 11th, 2019', Qt.AlignRight)
        # self.centerLabel = UILabelTopCenter(self, '2,234', 'M')

    # TODO -- Might be more resource efficient to have all the objects faded out
    # in 1 method, instead of having the fade attached to each individual class
    # Animations


    def updateUITop(self):
        self.timerFadeOutUI.stop()  # stop the timer that is about to cause a fade out

        self.topUnderlay.show()

        self.centerLabel.setPrimaryText(self.picture.altitude)
        self.centerLabel.setSecondaryText('M')
        self.centerLabel.show()

        self.rightLabel.setPrimaryText(self.picture.time)
        self.rightLabel.show()

        hikeText = 'Hike {h}'.format(h=self.picture.hike_id)
        self.leftLabel.setPrimaryText(hikeText)
        self.leftLabel.show()

    def updateUIBottom(self):
        pass


    # REMOVE
    '''
    def increaseIndex(self):
        self.picture = self.sql_controller.get_next_time_in_hikes(self.picture, 1)
        self.picture.print_obj_mvp()

        self.imageFader.set_next_image(self.picture.camera2)
        self.printCurrentMemoryUsage()

        # self.index += 1
        # if self.index > 1799:
        #     self.index = 1486

        # self.rightLabel.setText(str(self.index))

        # return self.index

    def decreaseIndex(self):
        self.picture = self.sql_controller.get_previous_time_in_hikes(self.picture, 1)
        self.picture.print_obj_mvp()

        self.imageFader.set_next_image(self.picture.camera2)
        self.printCurrentMemoryUsage()

        # self.index -= 1
        # if self.index < 1486:
        #     self.index = 1799

        # self.rightLabel.setText(str(self.index))

        # return self.index
    '''

    # Hardware Button Presses
    def rotary_changed(self, result):
        # print(result)

        self.picture = self.sql_controller.get_next_time_in_hikes(self.picture, result)
        # self.picture.print_obj()

        self.changeLandscapeUI(self.picture)

        # if isReadyForNewPicture:
        #     print('ready for new picture')

        # if result > 0:
        #     print('Next: %d' % result)
        # elif result < 0:
        #     print('Previous: %d' % result)

        # change = rotaryCounter - rotaryCounterLast
        # print(change)
        # print('\n')

    def pressed_encoder(self, result):
        print('Encoder button was pressed: %d' % result)

    def pressed_mode(self, result):
        print('Mode button was pressed: %d' % result)

    def pressed_next(self, result):
        print('Next button was pressed: %d' % result)
        self.changeLandscapeUI(11)

    def pressed_prev(self, result):
        print('Previous button was pressed: %d' % result)
        self.changeLandscapeUI(2561)

    def pressed_play_pause(self, result):
        # print('Play Pause button was pressed: %d' % result)
        global orientation
        orientation = ((orientation + 1) % 2)
        if orientation:
            print('Portrait')  # 1
        else:
            print('Landscape')  # 0

    # Keyboard Presses
    def keyPressEvent(self, event):
        global rotaryCounter
        if event.key() == Qt.Key_Escape:
            self.close()

        # Scroll Wheel
        elif event.key() == Qt.Key_Left:
            # print('left')
            self.picture = self.sql_controller.get_previous_time_in_hikes(self.picture, 1)
            self.updateUIPicture()
            self.updateUITop()
            # index = self.decreaseIndex()

            # path = 'capra-storage/hike10/' + str(index) + '_cam2.jpg'
            # print(path)
            # self.pictureLandscape.update_image('assets/cam2f2.jpg')
            # self.imageFader.set_next_image(path)
            # self.printCurrentMemoryUsage()


            # self.fade_image()

            # print(rotaryCounter)
            # self.updatePrev()
        elif event.key() == Qt.Key_Right:
            # print('right')
            self.picture = self.sql_controller.get_next_time_in_hikes(self.picture, 1)
            self.updateUIPicture()
            self.updateUITop()
            # self.rightLabel.show2() # working


            # index = self.increaseIndex()
            # path = 'capra-storage/hike10/' + str(index) + '_cam2.jpg'
            # self.imageFader.set_next_image(path)
            # self.printCurrentMemoryUsage()


            # self.fade_image()

            # self.imageFader.text_next = '+1'
            # self.imageFader.set_next_image('assets/7.jpg')
            # self.imageFader.increment_next()

            # print(rotaryCounter)
            # self.pictureLandscape = Image('assets/cam2f3.jpg')
            # self.alpha = 0.1  # Resets amount of fade between pictures
            # self.pictureLandscape.update_image('assets/cam2f3.jpg')

            # self.alpha = 0.5  # Resets amount of fade between pictures
            # self.fade_image()

            # TODO - trying to pigeon in the fading program from the other
            # self.alpha = 0.1  # Resets amount of fade between pictures
            # if self.index + 1 >= len(self.fileList):
            #     self.index = 0
            # else:
            #     self.index += 1
            # self.next_raw_mid = Image.open(self.fileList[self.index], 'r')

            # self.updateNext()

        # Next Previous Hike / Global
        elif event.key() == Qt.Key_Up:
            print('up arrow')
        elif event.key() == Qt.Key_Down:
            print('down arrow')

        # Pressing Scroll Wheel
        elif event.key() == Qt.Key_Shift:
            print('Shift - change global / hike')

        # Mode (Time, Altitude, Color)
        elif event.key() == Qt.Key_M:
            self.changeMode()

        # Play Pause
        elif event.key() == Qt.Key_Space:
            print('Space - Play/Pause')
        elif event.key() == Qt.Key_L:
            self.setLandscape()
        elif event.key() == Qt.Key_V:
            self.setVertical()
        else:
            print(event)

    # def thread_result(self, result):
    #     print('From MainLoop: %d' % result)
    #     # self.workerThreadLabel.setText('Worker: %d' % result)

    #     img = QPixmap(self.buildFile(result))
    #     self.imgLabel.setPixmap(img)

    '''
    def increment_label(self):
        self.index += 1
        self.indexLabel.setText('Count: %d' % self.index)
    '''

    # def recurring_timer(self):
    #     self.seconds += 1
    #     self.timerLabel.setText('Timer: %d' % self.seconds)

    '''
    # Helper Methods
    def buildLandscape(self, num) -> str:
        path = '/home/pi/capra-storage/images/{n}_fullscreen.jpg'.format(n=num)
        print(path)
        return path

    def buildFile(self, num) -> str:
        # return '~/capra-storage/images/{n}_cam3.jpg'.format(n=num)
        return '/home/pi/capra-storage/images/{n}_cam3.jpg'.format(n=num)
    '''

    # ----------------------Testing-----------------------

    # Memory usage in kB
    def printCurrentMemoryUsage(self):
        process = psutil.Process(os.getpid())
        print(process.memory_info().rss / 1024 ** 2)  # in bytes

    # Fades between the current image and the NEXT image
    def fade_image(self):
        # print('Fading the image at alpha of: ', self.alpha)
        if self.alpha < 1.0:
            self.current_raw = Image.blend(self.current_raw, self.next_raw, self.alpha)

            # self.current_raw.save("assets/blended.jpg")
            # self.pictureLandscape.update_image('assets/blended.jpg')
            self.pictureLandscape.update_pixmap(self.current_raw)

            # TODO - how long the last image hangs around
            #   Lower - the longer a piece stays on screen
            #   Higher - the faster the bit of an image leaves
            # self.alpha = self.alpha + 0.0417
            # self.alpha = self.alpha + 0.0209
            self.alpha = self.alpha + 0.1

        # TODO - Change this value to affect the spee of the fade
        #   Lower the number the quicker the fade
        #   Higher the number the slower the fade
        # root.after(40, self.fade_image)


app = QApplication(sys.argv)
window = MainWindow()
app.exec_()