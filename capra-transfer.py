import globals as g
import glob                                         # File path pattern matching
import os
import sys
import os.path
import datetime
import threading
from concurrent import futures
from concurrent.futures import ThreadPoolExecutor
import time
import sqlite3                                      # Database Library
import subprocess                                   # Deploy RSyncs
import traceback
import RPi.GPIO as GPIO
from PIL import ImageTk, Image                      # Pillow image functions
from pathlib import Path
from classes.capra_data_types import Picture, Hike
from classes.sql_controller import SQLController
from classes.sql_statements import SQLStatements
from classes.kmeans import get_dominant_colors_for_picture
from classes.kmeans import get_dominant_color_1D
import logging
g.init()

VERBOSE = False

GPIO.setmode(GPIO.BCM)                              # Set's GPIO pins to BCM GPIO numbering
GPIO.setup(g.HALL_EFFECT_PIN, GPIO.IN)              # Set our input pin to be an input
HALL_EFFECT_ON = threading.Event()                  # https://blog.miguelgrinberg.com/post/how-to-make-python-wait

logger = None
rsync_status = None
cDBController = None
pDBController = None
retry = 0
RETRY_MAX = 5

checkSum_transferred = 0
checkSum_rotated = 0
checkSum_total = 0

domColors = []
commits = []        # deferred commits due to concurrency
threads = []
threadPool = None
color_rows_checked = 0
color_rows_error = 0

# ### Database location ###
CAPRAPATH = g.CAPRAPATH_PROJECTOR
DATAPATH = g.DATAPATH_PROJECTOR
CAMERA_DB = DATAPATH + g.DBNAME_CAMERA
CAMERA_BAK_DB = DATAPATH + g.DBNAME_CAMERA_BAK
PROJECTOR_DB = DATAPATH + g.DBNAME_MASTER


class readHallEffectThread(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        # deploy this as a background process
        self.daemon = True
        self.start()

    def run(self):
        while True:
            if (GPIO.input(g.HALL_EFFECT_PIN)):
                g.HALL_EFFECT = False
            else:
                g.HALL_EFFECT = True

            if (g.HALL_EFFECT != g.PREV_HALL_VALUE):
                if (g.HALL_BOUNCE_TIMER is None):
                    print("signal change! setting the timer..")
                    g.HALL_BOUNCE_TIMER = current_milli_time()

                if (current_milli_time() - g.HALL_BOUNCE_TIMER > g.HALL_BOUNCE_LIMIT):
                    print("the signal is valid!")
                    if (g.HALL_EFFECT):
                        print("\tFalse -> True")
                        g.PREV_HALL_VALUE = True
                        HALL_EFFECT_ON.set()
                        g.flag_start_transfer = True
                    else:
                        print("\tTrue -> False")
                        g.PREV_HALL_VALUE = False
                        HALL_EFFECT_ON.clear()
                        g.flag_start_transfer = False

            elif (g.HALL_BOUNCE_TIMER is not None):
                print("signal change is lost. resetting the timer")
                g.HALL_BOUNCE_TIMER = None


def createLogger():
    global logger

    # ### Create Logger ###
    if os.name == 'nt':
        log_file = "C:\tmp\transfer.log"
    else:
        directory = CAPRAPATH + "log/"
        log_file = directory + 'transferLog-' + time.strftime("%Y%m%d") + '.log'
        if not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True, mode=0o755)

    # create logger with 'spam_application'
    logger = logging.getLogger('CapraTransferLogger')
    logger.setLevel(logging.INFO)

    # create file handler which logs even debug messages
    fh = logging.FileHandler(log_file, 'a+')
    fh.setLevel(logging.INFO)
    logger.addHandler(fh)


def timenow():
    return str(datetime.datetime.now()).split('.')[0]


def current_milli_time():
    return int(round(time.time() * 1000))


# https://stackoverflow.com/questions/28769023/get-output-of-system-ping-without-printing-to-the-console
def isCameraUp():
    is_up = False
    with open(os.devnull, 'w') as DEVNULL:
        try:
            subprocess.check_call(
                ['ping', '-c', '3', g.IP_ADDR_CAMERA],
                stdout=DEVNULL,  # suppress output
                stderr=DEVNULL
            )
            is_up = True
        except subprocess.CalledProcessError:
            is_up = False
    return is_up


def updateDB():
    proc = subprocess.Popen(["sqldiff", CAMERA_DB, CAMERA_BAK_DB], stdout=subprocess.PIPE)
    line = proc.stdout.readline()
    if line != b'':
        # there are new incoming changes in DB
        print("## Updated DB detected. Starting the transfer process...")
        return True
    else:
        # two databases are identical
        print("## DB is still fresh.")
        return False


def copy_remote_db():
    subprocess.Popen(['rsync', '--inplace', '-avAI', '--no-perms', '--rsh="ssh"', "pi@" + g.IP_ADDR_CAMERA + ":/media/pi/capra-hd/capra_camera_test.db", "/media/pi/capra-hd/"], stdout=subprocess.PIPE)
    time.sleep(1)
    return


def make_backup_remote_db():
    subprocess.Popen(['cp', CAMERA_DB, CAMERA_BAK_DB], stdout=subprocess.PIPE)
    return


# 1. make db connections
def getDBControllers():
    global cDBController, pDBController

    # this will be a local db, copied from camera
    cDBController = SQLController(database=CAMERA_DB)
    # master projector db
    pDBController = SQLController(database=PROJECTOR_DB)


# 2. copy remote DB
def update_transfer_animation_db():
    # if date.today() != date.fromtimestamp(Path(g.PATH_TRANSFER_ANIMATION_DB).stat().st_mtime):
    transfer_from_camera(g.PATH_CAMERA_DB, g.PATH_TRANSFER_ANIMATION_DB)


def count_files_in_directory(path, pattern):
    if (not os.path.exists(path)):
        return 0
    else:
        return len(glob.glob(path + pattern))


def build_hike_path(hikeID, makeNew=False):
    res = DATAPATH + 'hike' + str(hikeID) + '/'
    if makeNew and not os.path.exists(res):
        os.makedirs(res, mode=0o755)
    return res


def build_picture_path(hikeID, index, camNum, rotated=False):
    insert = ""
    if (rotated):
        insert = "r"
    return build_hike_path(hikeID) + str(index) + '_cam' + str(camNum) + insert + '.jpg'


def resize_photo(photoPath, w, h):
    im = Image.open(photoPath)
    im = im.resize((w, h), Image.ANTIALIAS)
    im.save(photoPath)


def rotate_photo(srcFile, destFile, angle):
    image = Image.open(srcFile)
    image_rotated = image.copy().rotate(angle, expand=True)
    image_rotated.save(destFile)


def compute_checksum(currHike):
    global checkSum_total, checkSum_rotated, checkSum_transferred
    checkSum_transferred = count_files_in_directory(build_hike_path(currHike), g.FILENAME)
    checkSum_rotated = count_files_in_directory(build_hike_path(currHike), g.FILENAME_ROTATED)
    checkSum_total = checkSum_transferred + checkSum_rotated


def validate_color(hikeColor):
    return hikeColor is not None and hikeColor and not (hikeColor[0] < 0.001 and hikeColor[1] < 0.001 and hikeColor[2] < 0.001)


def check_hike_postprocessing(currHike):
    hikeColor1 = pDBController.get_hike_average_color(currHike, 1)
    hikeColor2 = pDBController.get_hike_average_color(currHike, 2)
    hikeColor3 = pDBController.get_hike_average_color(currHike, 3)
    return validate_color(hikeColor1) and validate_color(hikeColor2) and validate_color(hikeColor3)


def dominant_color_wrapper(currHike, row, picPathCam2):
    global color_rows_error, color_rows_checked

    try:
        color_resCode, color_res = get_dominant_colors_for_picture(picPathCam2)

    # TODO: check if invalid files are handled correctly
    # TODO: how do we redo failed rows?
    except:
        print("[{}]     Exception at Hike {}, row {} while extracting dominant color".format(timenow(), currHike, str(row[4])))
        print(traceback.format_exc())
        logger.info("[{}]     Exception at Hike {}, row {} while extracting dominant color".format(timenow(), currHike, str(row[4])))
        logger.info(traceback.format_exc())

    if (color_resCode < 0):
        color_rows_error += 1
    else:
        color_rows_checked += 1

    color = color_res.split(", ")

    # round color values to the nearest hundredth
    for i in range(len(color)):
        color[i] = round(color[i], 2)

    # (time, hikeID, index_in_hike, altitude, hue, saturation, value, red, green, blue, camera1, camera2, camera3, camera_landscape)
    picPathCam1 = build_picture_path(currHike, row[4], 1)
    picPathCam3 = build_picture_path(currHike, row[4], 3)
    commit = (row[0], currHike, row[4], row[1], color[0], color[1], color[2], color[3], color[4], color[5], picPathCam1, picPathCam2, picPathCam3, "tmp")

    # TODO: deliver transfer animation info

    return color, commit


def start_transfer():
    global cDBController, pDBController, rsync_status, retry, hall_effect
    global checkSum_transferred, checkSum_rotated, checkSum_total
    global logger
    global color_rows_checked, color_rows_error, domColors, commits, threads, threadPool

    latest_master_hikeID = pDBController.get_last_hike_id()
    latest_remote_hikeID = cDBController.get_last_hike_id()
    print("[{}] @@@ # hikes on Projector: {}".format(timenow(), str(latest_master_hikeID)))
    print("[{}] @@@ # hikes on Camera: {}".format(timenow(), str(latest_remote_hikeID)))
    logger.info("[{}] @@@ # hikes on Projector: {}".format(timenow(), str(latest_master_hikeID)))
    logger.info("[{}] @@@ # hikes on Camera: {}".format(timenow(), str(latest_remote_hikeID)))

    currHike = 1
    checkSum = 0

    # 3. determine how many hikes should be transferred
    while currHike <= latest_remote_hikeID:

        if (not g.HALL_EFFECT):
            print("[{}]     HALL-EFFECT SIGNAL LOST !! Terminating transfer process..".format(timenow()))
            logger.info("[{}]     HALL-EFFECT SIGNAL LOST !! Terminating transfer process..".format(timenow()))
            return
        if (not isCameraUp()):
            print("[{}]     CAMERA SIGNAL LOST !! Please check the connection and retry. Terminating transfer process..".format(timenow()))
            logger.info("[{}]     CAMERA SIGNAL LOST !! Please check the connection and retry. Terminating transfer process..".format(timenow()))
            return

        currExpectedHikeSize = cDBController.get_size_of_hike(currHike)
        if (currExpectedHikeSize is None):
            currExpectedHikeSize = 0
        expectedCheckSumTotal = currExpectedHikeSize * 4

        # 1. skip empty hikes
        if (currExpectedHikeSize == 0):
            print("[{}] Hike {} is empty. Proceeding to the next hike...".format(timenow(), str(currHike)))
            logger.info("[{}] Hike {} is empty. Proceeding to the next hike...".format(timenow(), str(currHike)))
            currHike += 1
            continue

        compute_checksum(currHike)
        print("[{}] Hike {}: Total {} rows -- {} out of {} photos transferred".format(timenow(), str(currHike), str(currExpectedHikeSize), str(checkSum_transferred), str(currExpectedHikeSize * 3)))
        print("[{}] Hike {}: Total {} photos expected, found {} photos".format(timenow(), str(currHike), str(expectedCheckSumTotal), str(checkSum_total)))
        logger.info("[{}] Hike {}: Total {} rows -- {} out of {} photos transferred".format(timenow(), str(currHike), str(currExpectedHikeSize), str(checkSum_transferred), str(currExpectedHikeSize * 3)))
        logger.info("[{}] Hike {}: Total {} photos expected, found {} photos".format(timenow(), str(currHike), str(expectedCheckSumTotal), str(checkSum_total)))

        # TODO:
        #   a. try setting "--remove-source-files" option in rsync
        #     * may need to check few things:
        #       - can it recover from disconnection? - total number of files on the source dir will change
        #       - when is the best time to resize?
        #   b. pass information needed for the transfer animation as a JSON file
        #       >> this may be easier since as soon as post-processing is done
        #           because that's the best time to run complex queries since we have complete data

        # 2. if a hike is fully transferred, resized and rotated, then skip the transfer for this hike
        # also check if DB is updated to post-processed values as well
        if (currExpectedHikeSize != 0 and checkSum_transferred == currExpectedHikeSize * 3 and expectedCheckSumTotal == checkSum_total and check_hike_postprocessing(currHike)):
            print("[{}]     # Hike {} fully transferred. Proceeding to the next hike...".format(timenow(), str(currHike)))
            logger.info("[{}]     # Hike {} fully transferred. Proceeding to the next hike...".format(timenow(), str(currHike)))
            currHike += 1
            continue

        # validity check
        #   ** For photos with invalid data, we won't bother restoring/fixing incorrect metatdata.
        #      The row (all 3 photos) will be dropped as a whole
        validRows = cDBController.get_valid_photos_in_given_hike(currHike)
        numValidRows = len(validRows)
        checkSum_transfer_and_rotated = 4 * numValidRows
        dest = build_hike_path(currHike, True)
        hikeTimer = time.time()

        # completed hikes will have:
        #   i) 4 * hikesize (cam 1 + 2 + 3 + rotated pics)
        #   ii) valid color value of dominant color for each corresponding row in the hike table
        if (checkSum_total < currExpectedHikeSize * 4 or not check_hike_postprocessing(currHike)):
            # 3. transfer is not complete - still need to copy more pictures
            if (checkSum_transferred < currExpectedHikeSize * 3 or not check_hike_postprocessing(currHike)):

                print("[{}]   Resume transfer on Hike {}: {} out of {} files".format(timenow(), currHike, checkSum_transferred, str(currExpectedHikeSize * 3)))
                logger.info("[{}]   Resume transfer on Hike {}: {} out of {} files".format(timenow(), currHike, checkSum_transferred, str(currExpectedHikeSize * 3)))
                # TRANSFER

                # POST-PROCESSING
                avgAlt = 0
                startTime = 9999999999
                endTime = -1

                # for colors
                domColors = []
                color_rows_checked = 0
                color_rows_error = 0

                threads = []
                threadPool = ThreadPoolExecutor(max_workers=5)
                commits = []

                i = 0
                # for row in validRows:
                while(i < len(validRows)):
                    row = validRows[i]

                    if (not g.HALL_EFFECT):
                        print("[{}]     HALL-EFFECT SIGNAL LOST !! Terminating transfer process..".format(timenow()))
                        logger.info("[{}]     HALL-EFFECT SIGNAL LOST !! Terminating transfer process..".format(timenow()))
                        return
                    if (not isCameraUp()):
                        print("[{}]     CAMERA SIGNAL LOST !! Please check the connection and retry. Terminating transfer process..".format(timenow()))
                        logger.info("[{}]     CAMERA SIGNAL LOST !! Please check the connection and retry. Terminating transfer process..".format(timenow()))
                        return

                    camNum = 1
                    while (camNum <= 3):
                        # (time, alt, color, hike, index, cam1, cam2, cam3, date_created, date_updated)
                        src = row[5][:-5] + str(camNum) + row[5][-4:]      # "/home/pi/capra-storage/hike1/1_cam2.jpg" --> "/home/pi/capra-storage/hike1/1_cam(*).jpg"
                        expectedPath = build_hike_path(currHike) + src.split('/')[-1]
                        isNew = False

                        if (not os.path.exists(build_picture_path(currHike, row[4], camNum))):
                            isNew = True
                            rsync_status = subprocess.Popen(['rsync', '--ignore-existing', '--remove-source-files', '-avA', '--no-perms', '--rsh="ssh"', 'pi@' + g.IP_ADDR_CAMERA + ':' + src, dest], stdout=subprocess.PIPE)
                            rsync_status.wait()

                            # report if rsync is failed
                            if (rsync_status.returncode != 0):
                                print("[{}] ### Rsync failed at row {}".format(timenow(), str(row[4] - 1)))
                                logger.info("[{}] ### Rsync failed at row {}".format(timenow(), str(row[4] - 1)))

                        # Do Post-processing for each row
                        if (camNum == 2 and os.path.exists(build_picture_path(currHike, row[4], camNum))):
                            # update timestamps
                            if (row[0] < startTime):
                                startTime = row[0]
                            if (row[0] > endTime):
                                endTime = row[0]

                            avgAlt += int(row[1])

                            # skip this row if a row with the specific timestamp already exists
                            # ** we still want to consider the timestamp and the average altitude even when skipping rows
                            if (pDBController.get_picture_at_timestamp(row[0]) > 0):
                                col = pDBController.get_picture_dominant_color(row[0])
                                domColors.append([col[0], col[1], col[2]])
                                color_rows_checked += 1
                                camNum += 1
                                continue

                            # concurrently extract the dominant color
                            #  1. calculate dominant HSV/RGB colors
                            #  2. update path to each picture for camera 1, 2, 3
                            threads.append(threadPool.submit(dominant_color_wrapper, currHike, row, build_picture_path(currHike, row[4], 2)))

                            # resize and rotate for newly added pictures
                            if (isNew):
                                if (camNum == 2):
                                    rotate_photo(build_picture_path(currHike, row[4], camNum), build_picture_path(currHike, row[4], camNum, True), 90)

                                resize_photo(build_picture_path(currHike, row[4], camNum), 427, 720)

                        camNum += 1

                    i += 1

                # wait for threads to finish
                for thread in futures.as_completed(threads):
                    color, commit = thread.result()
                    domColors.append([color[0], color[1], color[2]])
                    commits.append(commit)
                threadPool.shutdown(wait=True)

                # commit changes
                #  ** sqlite does not support concurrent write options
                for commit in commits:
                    pDBController.upsert_picture(*commit)

                # make a row for the hike table with postprocessed values
                compute_checksum(currHike)
                avgAlt /= numValidRows
                hikeDomCol = []
                if (checkSum_total / 4 > g.COLOR_CLUSTER):
                    hikeDomCol = get_dominant_color_1D(domColors, g.COLOR_CLUSTER)

                # (hike_id, avg_altitude, avg_hue, avg_saturation, avg_value, start_time, end_time, pictures, path)
                print("[{}] @@ Writing a row to hikes table for Hike {} ...".format(timenow(), currHike))
                logger.info("[{}] @@ Writing a row to hikes table for Hike {} ...".format(timenow(), currHike))
                pDBController.upsert_hike(currHike, avgAlt, round(hikeDomCol[0], 2), round(hikeDomCol[1], 2), round(hikeDomCol[2], 2), startTime, endTime, color_rows_checked, dest)

                # suppose hike is finished, now do the resizing
                print("[{}]   Hike {} took {} seconds for transfer & PP.".format(timenow(), str(currHike), str(time.time() - hikeTimer)))
                logger.info("[{}]   Hike {} took {} seconds for transfer & PP.".format(timenow(), str(currHike), str(time.time() - hikeTimer)))

            # resizing/rotating is not done
            else:
                rTimer = time.time()
                i = 0
                while(i < len(validRows)):
                    row = validRows[i]
                    # Make a copy for the second image and rorate CCW 90
                    # TODO: make sure we rotate photos in the right direction
                    rotate_photo(build_picture_path(currHike, row[4], 2), build_picture_path(currHike, row[4], 2, True), 90)

                    # Resize three images
                    resize_photo(build_picture_path(currHike, row[4], 1), 427, 720)
                    resize_photo(build_picture_path(currHike, row[4], 2), 427, 720)
                    resize_photo(build_picture_path(currHike, row[4], 3), 427, 720)

                    i += 1

                print("[{}]   Hike {} rotating and resizing took {} seconds.".format(timenow(), str(currHike), str(time.time() - rTimer)))
                logger.info("[{}]   Hike {} rotating and resizing took {} seconds.".format(timenow(), str(currHike), str(time.time() - rTimer)))

            if (numValidRows != currExpectedHikeSize):
                print("[{}] !!! Invalid rows detected in hike {}".format(timenow(), str(currHike)))
                logger.info("[{}] !!! Invalid rows detected in hike {}".format(timenow(), str(currHike)))

            # Log summary
            compute_checksum(currHike)
            print("[{}] Total valid rows in Hike {}: {}".format(timenow(), str(currHike), str(numValidRows)))
            print("[{}] Total transferred files in hike {}: {}".format(timenow(), str(currHike), str(checkSum_transferred)))
            logger.info("[{}] Total valid rows in Hike {}: {}".format(timenow(), str(currHike), str(numValidRows)))
            logger.info("[{}] Total transferred files in hike {}: {}".format(timenow(), str(currHike), str(checkSum_transferred)))

        else:
            print("[{}] Hike {} is fully transferred.".format(timenow(), str(currHike)))
            logger.info("[{}] Hike {} is fully transferred.".format(timenow(), str(currHike)))

        print("[{}] Hike {} complete. Took total {} seconds.".format(timenow(), currHike, str(time.time() - hikeTimer)))
        print("[{}] Proceeding to the next hike... {} -> {}".format(timenow(), str(currHike), str(currHike + 1)))
        logger.info("[{}] Hike {} complete. Took total {} seconds.".format(timenow(), currHike, str(time.time() - hikeTimer)))
        logger.info("[{}] Proceeding to the next hike... {} -> {}".format(timenow(), str(currHike), str(currHike + 1)))

        currHike += 1

    print("[{}] --- {} seconds ---".format(timenow(), str(time.time() - start_time)))
    logger.info("[{}] --- {} seconds ---".format(timenow(), str(time.time() - start_time)))


# ==================================================================
getDBControllers()
readHallEffectThread()

while True:
    HALL_EFFECT_ON.wait()
    createLogger()
    start_time = time.time()
    try:
        if (isCameraUp()):
            copy_remote_db()

            # if camera DB is still fresh, do not run transfer script
            if (not updateDB()):
                g.flag_start_transfer = False
                HALL_EFFECT_ON.clear()
                continue

            start_transfer()
            # if transfer is successfully finished pause running until camera is dismounted and re-mounted
            print("## Transfer finished. Pause the script")
            g.flag_start_transfer = False
            HALL_EFFECT_ON.clear()
            make_backup_remote_db()
        else:
            print("[{}]     CAMERA SIGNAL LOST !! Please check the connection and retry. Terminating transfer process..".format(timenow()))
            logger.info("[{}]     CAMERA SIGNAL LOST !! Please check the connection and retry. Terminating transfer process..".format(timenow()))

    # TODO: clean up hanging processes when restarting
    #           fuser capra_projector.db -k
    except Exception as e:
        print("[{}]: !!   Encounter an exception while transferring restarting the script..".format(timenow()))
        logger.info("[{}]: !!   Encounter an exception while transferring restarting the script..".format(timenow()))
        if hasattr(e, 'message'):
            print(e.message, '\n')
        print(e)
        print(traceback.format_exc())

        python = sys.executable
        os.execl(python, python, * sys.argv)
