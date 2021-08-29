# -*- coding: utf-8 -*-

from PyQt5.QtCore import Qt,  pyqtSlot, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QTextCursor,  QIntValidator, QDoubleValidator
from PyQt5.QtWidgets import QMainWindow, QInputDialog, QFontDialog
from PyQt5 import QtGui
from PyQt5 import QtCore
from time import sleep,  gmtime,  strftime
import threading
from threading import Lock
from playsound import playsound
from .Ui_mainWindow import Ui_MainWindow
import configuration
import init
import soundDeviceManager as sdm
import numpy as np
from matplotlib.animation import FuncAnimation
import matplotlib.pyplot as plt
import queue
from pynput.keyboard import Key
import pynput.keyboard as kb
keyboard = kb.Controller()
import tkinter
import tkinter.messagebox # need to import explicitly
from tkinter.filedialog import askopenfilename
import os
import audioSettings
import platform
import logging
if platform.system() != 'Windows':
    import shlex, subprocess


# NOTE: we need root so we can close the messagebox
root = tkinter.Tk()
root.withdraw()

# different relative paths depending if we run the script or the executable file
runningScript = os.path.basename(__file__)
if (runningScript=="mainWindow.py"): 
    # .py script
    # go to current file path
    os.chdir(os.path.dirname(__file__))
    # now go one level up so configuration.PATH_PREFIX is correct again (we are now in folder /ui)
    os.chdir("../")

# defines for chat state machine
CHAT_STATE_OFF = "off"
CHAT_STATE_IDLE = "idle"
CHAT_STATE_INCOMING_CALL = "incoming_call"
CHAT_STATE_CALLING = "calling"
CHAT_STATE_REJECT_CALL = "reject_call" # short-state
CHAT_STATE_KEY_EXCHANGE_START = "key_exchange_start"
CHAT_STATE_KEY_EXCHANGE_END = "key_exchange_end"
CHAT_STATE_STARTUP_DATA = "startup_data"
CHAT_STATE_CONNECTED = "connected"


class MainWindow(QMainWindow, Ui_MainWindow):
    app = None  # QApplication
    nrOfMessagesSent = 0
    qWriteChat = queue.Queue()
    cfg = None # __init__.InitApp()
    sdMgr = None # sdm.SoundDeviceManager()
    appStarted = False
    # status
    status = ["/", "-", "\\", "|"]
    statusCnt = 0
    chat_state = CHAT_STATE_OFF
    tx_state_wait_ack = False
    pushWaitThread = None
    abort_outgoing_call = False
    # plot variables
    plotdata = None
    lines = None
    ax = None
    fig = None
    # Lock
    teChatLock = Lock()
    #######################################
    # WARNING: we actually try to refresch while this lock is set so..
    #                   yes, we would have problems if we did NOT have this lock !!!
    #######################################
    updateLock = False # Lock()
    #######################################
    # helper variables for pollStatistics()
    txOk = 0
    txNok = 0
    rxOk = 0
    rxNok = 0
    
    ###################################################
    # IMPORTANT: we don't modify GUI objects from a QThread
    #                     or even worse, from a python thread!!!
    #                     Instead, we send a signal to the GUI / MainWindow.
    # Ref.: https://stackoverflow.com/questions/12083034/pyqt-updating-gui-from-a-callback
    ###################################################
    class MyGuiUpdateThread(QThread):
        updated = pyqtSignal(str)
        def run( self ):
            while True:
                sleep(configuration.GUI_UPDATE_PERIOD_IN_SEC)
                # TODO: improvement: pass e.g. time, counter or something useful to update function?
                self.updated.emit("Hi")
    
    # thread to update GUI
    # by "polling" information
    # TODO: replace polling with threading.Event() ?
    #            but polling gives a nice and predictable order/structure
    #####################################
    def updateGui(self):
        if self.updateLock == False:
            self.updateLock = True
            # write chat
            if self.qWriteChat.empty() == False:
                textMsg = self.qWriteChat.get()
                self.sendMessage(textMsg)
            # update status on GUI
            ##############
            if (self.lblStatus is not None) and (configuration.SHOW_LIVE_STATUS): # (self.cbShowLiveStatus.isChecked()):
                # set alternating symbol
                self.lblStatus.setText(" "+self.status[self.statusCnt])
                self.statusCnt = (self.statusCnt + 1)%4
            # poll messages, counters, responses,..
            ######################
            if self.chat_state == CHAT_STATE_CONNECTED:
                # poll WAIT_ACK status
                waitAck = self.sdMgr.isTxStateWaitAck()
                if waitAck != self.tx_state_wait_ack:
                    self.tx_state_wait_ack = waitAck
                    if waitAck:
                        self.pbCallState.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/wait_ack.png'))
                    else:
                        self.pbCallState.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/connected.png'))
                # poll RxMessages
                ###########
                self.pollRxMessage()
                # poll performance
                ##########
                self.pollPerformance()
                # poll statistics
                self.pollStatistics()
                # check for hang-up of call on the other side
                #########################
                if self.sdMgr.isCallEnd():
                    # execute this method to a separate thread in order not to block
                    incomingCallThread = threading.Thread(name="incomingCall", target=self.incoming_call_end)
                    incomingCallThread.start()
            elif (self.chat_state == CHAT_STATE_IDLE) or (self.chat_state == CHAT_STATE_CALLING):
                # check for incoming call
                if self.sdMgr.isCall():
                    self.incoming_call()
            # check for hang-up of call on the other side (could come in every state? TODO: restrict this only to some states?)
            ########################################
            elif self.sdMgr.isCallEnd():
                # execute this method to a separate thread in order not to block
                incomingCallThread = threading.Thread(name="incomingCall", target=self.incoming_call_end)
                incomingCallThread.start()
            # poll error messages
            ############
            if self.chat_state != CHAT_STATE_OFF:
                errorMessage = self.sdMgr.pollErrorMessages()
                if errorMessage != "":
                    tkinter.messagebox.showerror(title="ERROR", message=errorMessage)
                    # NOTE: call root.mainloop() to enable the program to respond to events. 
                    root.update()
            # poll RxStatus
            #########
            self.pollRxStatus()
            # poll TxStatus
            #########
            self.pollTxStatus()
            # unlock
            #####
            self.updateLock = False
        else:
            logging.warning("updateGui locked ++++++++++++++++++++")
            
    # thread
    def messageSound(self):
        playsound(configuration.PATH_PREFIX+'sounds/message.mp3')
   
    # TODO:
    # loop all rx data until queue is empty? with:
    # while self.sdMgr.isMessageRxQueueEmpty() == False:
    def pollRxMessage(self):
        rxMsg = None
        try:
            rxMsg = self.sdMgr.messageRxQueueGet()
            if rxMsg is not None:
                # add input message to chat
                self.teChatLock.acquire()
                self.teChat.moveCursor(QTextCursor.End)
                self.teChat.setAlignment(Qt.AlignLeft)
                self.teChat.setTextColor(QColor(0, 100, 0))
                tc = self.teChat.textCursor()
                tc.insertText(rxMsg)
                self.teChat.moveCursor(QTextCursor.End)
                self.teChatLock.release()
                if configuration.SOUND_EFFECTS:
                    # play this sound to a separate thread in order not to block
                    messageSoundThread = threading.Thread(name="messageSound", target=self.messageSound)
                    messageSoundThread.start()
        except Exception as e:
            logging.error("Exception in mainWindow.pollRxMessage():"+str(e)+"\n")
            
    def pollRxStatus(self):
        rxStatus = None
        try:
            rxStatus = self.sdMgr.statusRxQueueGet()
            if rxStatus is not None:
                # set RX status
                self.lblChatInfo.setText(rxStatus)
        except Exception as e:
            logging.error("Exception in mainWindow.pollRxStatus():"+str(e)+"\n")
            
    def pollTxStatus(self):
        txStatus = None
        try:
            txStatus = self.sdMgr.statusTxQueueGet()
            if txStatus is not None:
                # set TX status
                self.lblChatInfoTx.setText(txStatus)
        except Exception as e:
            logging.error("Exception in mainWindow.pollTxStatus():"+str(e)+"\n")
            
    def pollStatistics(self):
         if configuration.SHOW_PERFORMANCE:
            if self.txOk != self.sdMgr.getTelTxOk():
                self.txOk = self.sdMgr.getTelTxOk()
                self.lblTelTxOk.setText("%d" % self.txOk) 
                if self.txOk != 0:
                    self.lblTelTxErrPercent.setText("%.2f" % (self.txNok/self.txOk*100.0))
                else:
                    self.lblTelTxErrPercent.setText("%.2f" % (self.txNok/1*100.0))
            if self.txNok != self.sdMgr.getTelTxNok():
                self.txNok = self.sdMgr.getTelTxNok()
                self.lblTelTxNok.setText("%d" % self.txNok) 
                if self.txOk != 0:
                    self.lblTelTxErrPercent.setText("%.2f" % (self.txNok/self.txOk*100.0))
                else:
                    self.lblTelTxErrPercent.setText("%.2f" % (self.txNok/1*100.0))
            if self.rxOk != self.sdMgr.getTelRxOk():
                self.rxOk = self.sdMgr.getTelRxOk()
                self.lblTelRxOk.setText("%d" % self.rxOk) 
                if self.rxOk != 0:
                    self.lblTelRxErrPercent.setText("%.2f" % (self.rxNok/self.rxOk*100.0))
                else:
                    self.lblTelRxErrPercent.setText("%.2f" % (self.rxNok/1*100.0))
            if self.rxNok != self.sdMgr.getTelRxNok():
                self.rxNok = self.sdMgr.getTelRxNok()
                self.lblTelRxNok.setText("%d" % self.rxNok) 
                if self.rxOk != 0:
                    self.lblTelRxErrPercent.setText("%.2f" % (self.rxNok/self.rxOk*100.0))
                else:
                    self.lblTelRxErrPercent.setText("%.2f" % (self.rxNok/1*100.0))
            
    def pollPerformance(self):
        if configuration.SHOW_PERFORMANCE:
            self.lblRxTime.setText("%.2f" % self.sdMgr.getRxTimeMs())
            self.lblTxTime.setText("%.2f" %self.sdMgr.getAvgTxTimeMs())
            self.lblRoundtripTime.setText("%.2f" % self.sdMgr.getRoundtripTimeMs())
            self.vsQueueUsage.setValue(self.sdMgr.getTelegramCircularBufferSize())
            self.vsVolume.setValue(self.sdMgr.getAvgInAmplitudePercent())
            # QSlider gradient color:
            v = self.sdMgr.getAvgInAmplitudePercent()
            d = self.vsVolume.maximum() - self.vsVolume.minimum()
            v = v - self.vsVolume.minimum()
            rv =float(v / d)
            if rv < 0.5:
                c = QColor.fromHsl(192*rv, 128, 128)
            else:
                c = QColor.fromHsl(192 - 192*rv, 128, 128)
            self.vsVolume.setStyleSheet("QSlider {background-color:"+c.name()+";}")
            # QSlider gradient color:
            v = self.vsQueueUsage.value()
            d = self.vsQueueUsage.maximum() - self.vsQueueUsage.minimum()
            v = v - self.vsQueueUsage.minimum()
            rv =float(v / d)
            c = QColor.fromHsl(128 - 128*rv, 128, 128)
            self.vsQueueUsage.setStyleSheet("QSlider {background-color:"+c.name()+";}")
                
    # called perdiodically by matplotlib animation (in the "main loop") to update the plot
    # Typically, audio callbacks happen more frequently than plot updates,
    # therefore the queue tends to contain multiple blocks of audio data
    def update_plot(self, frame):       
        data = None
        # loop all rx data until queue is empty
        while (self.chat_state != CHAT_STATE_OFF) and (configuration.SHOW_PLOT) and (self.sdMgr.isPlotRxQueueEmpty() == False):
            try:
                data = self.sdMgr.plotRxQueueGetNoWait()
                if data is not None:
                    shift = len(data)
                    if configuration.PLOT_FFT == False:
                        self.plotdata = np.roll(self.plotdata, -shift, axis=0)
                    # WORKAROUND
                    # loop i.o. correct data structure and
                    # len(plotdata) i.o. correct length?
                    # TODO: better implementation or OK like this?
                    for i in range(len(self.plotdata)):
                        self.plotdata[i] = data[i]
                    for column, line in enumerate(self.lines):
                        line.set_ydata(self.plotdata[:])
            except Exception as e:
                logging.error("Exception in mainWindow.update_plot():"+str(e)+"\n")
        return self.lines
        
    # called from the "main loop"
    def plotThread(self):
        try:
            # build plot
            # IMPORTANT: FuncAnimation uses Tk and MUST run in main loop !!!
            #######################################
            if configuration.PLOT_FFT:
                length = int((audioSettings.N/2)/audioSettings.DOWNSAMPLE)
            else:
                length = int(audioSettings.N/audioSettings.DOWNSAMPLE)
            self.plotdata = np.zeros((length, 1))
            # figure
            fig, ax = plt.subplots(num='Plot')
            self.lines = plt.plot(self.plotdata, '-b')
            if configuration.PLOT_FFT:
                plt.title("Audio signal, FFT")
                # TODO: find out how we can normalize FFT
                # ax.axis((0, len(self.plotdata), 0.0, 0.3))
                ax.axis((0, len(self.plotdata), 0.0, 0.05))
            else:
                plt.title("Audio signal, time domain")
                ax.axis((0, len(self.plotdata), -1.0, 1.0))
            ax.set_yticks([0])
            ax.yaxis.grid(True)
            ax.tick_params(bottom=False, top=False, labelbottom=False,
                           right=False, left=False, labelleft=False)
            fig.tight_layout(pad=0)
            # NOTE:
            # The object created by FuncAnimation must be assigned to a global variable apparently; otherwise, nothing will happen.
            ###################################################################
            objectFunctionAnimation = FuncAnimation(fig, self.update_plot, interval=audioSettings.INTERVAL, blit=True)
            logging.info("objectFunctionAnimation = "+str(objectFunctionAnimation))
            # this call BLOCKS as long as plt remains open
            ###########################
            plt.show()
            # plt.close() was called
            logging.info("leave plotThread..")
        except Exception as e:
            logging.error("Exception in plotThread...leaving thread"+str(e))
            
    def setTeChatStyleSheet(self):
        if configuration.TEXT_BOLD:
           self.teChat.setStyleSheet("background-image : url("+configuration.PATH_PREFIX+\
           "images/chat_background.png); border : 1px solid grey; font: "+\
           str(configuration.TEXT_SIZE)+"pt "+configuration.TEXT_FAMILY+"; font-weight: 1200")
        else:
           self.teChat.setStyleSheet("background-image : url("+configuration.PATH_PREFIX+\
           "images/chat_background.png); border : 1px solid grey; font: "+\
           str(configuration.TEXT_SIZE)+"pt "+configuration.TEXT_FAMILY+"; font-weight: 400")
        
    def __init__(self, qApplication, parent=None,  sdm_arg=None):
        # call super
        super(MainWindow, self).__init__(parent)
        self.app = qApplication
        # setup Ui
        self.setupUi(self)
        # event filter to detect ENTER in teWrite
        self.teWrite.installEventFilter(self)
        # set focus
        self.setFocus()
       # init audio
        self.sdMgr = sdm.SoundDeviceManager(self.plotdata,  self.lines)
        # fill combo boxes
        logging.info("audio devices:")
        for i in range(0, len(self.sdMgr.audio_devices)):
            if self.sdMgr.audio_devices[i]["max_input_channels"] != 0:
                # NOTE: devices may be detected several times e.g. on different USB-Ports but still have the same name.
                #            We add the audio_devices index as a prefix in order to be able to distinguish from one another. 
                input_device_str = str(i)+": "+self.sdMgr.audio_devices[i]["name"]
                self.cbTxIn.addItem(input_device_str)
                self.cbRxIn.addItem(input_device_str)
            if self.sdMgr.audio_devices[i]["max_output_channels"] != 0:
                # NOTE: devices may be detected several times e.g. on different USB-Ports but still have the same name.
                #            We add the audio_devices index as a prefix in order to be able to distinguish from one another. 
                output_device_str = str(i)+": "+self.sdMgr.audio_devices[i]["name"]
                self.cbTxOut.addItem(output_device_str)
                self.cbRxOut.addItem(output_device_str)
        self.cbTxIn.addItem("none")
        self.cbRxIn.addItem("none")
        self.cbTxOut.addItem("none")
        self.cbRxOut.addItem("none")
        # combo boxes
        self.cbLoggingLevel.addItem("logging.DEBUG")
        self.cbLoggingLevel.addItem("logging.INFO")
        self.cbLoggingLevel.addItem("logging.WARNING")
        self.cbLoggingLevel.addItem("logging.ERROR")
        self.cbLoggingLevel.addItem("logging.CRITICAL")
        self.updateLoggingLevel()
        # default input and output devices
        self.lblDefaultInputDevice.setText(self.sdMgr.audio_devices[self.sdMgr.default_device[0]]["name"])
        self.lblDefaultOutputDevice.setText(self.sdMgr.audio_devices[self.sdMgr.default_device[1]]["name"])
        # audio frequency channels
        for i in range(len(audioSettings.FREQUENCY_CHANNELS)):
            self.cbFrequencyChannel.addItem(audioSettings.FREQUENCY_CHANNELS[i])
        self.cbFrequencyChannel.setCurrentIndex(audioSettings.CURRENT_FREQUENCY_CHANNEL)
        # NOTE: possible validators are:
        # QDoubleValidator, QIntValidator, QRegExpValidator, QRegularExpressionValidator
        self.leSamplingFrequency.setValidator(QIntValidator())
        self.leCarrierFrequency.setValidator(QIntValidator())
        self.leCarrierAmplitude.setValidator(QDoubleValidator())
        self.leTelegramMaxLenBytes.setValidator(QIntValidator())
        self.leMaxNrOfChunksPerTelegram.setValidator(QIntValidator())
        self.leAmplitude.setValidator(QDoubleValidator())
        self.leFftDetectionLevel.setValidator(QDoubleValidator())
        self.leMaxNrOfResends.setValidator(QIntValidator())
        self.leMaxChannelDelayMs.setValidator(QIntValidator())
        # load .ini file and set values in GUI
        #####################
        self.cfg = init.InitApp()
        self.updateGuiConfig()
        #####################
        # fix size of window
        self.setFixedSize(self.size())
        # title
        system = platform.system()
        currentTime = strftime("%Y.%m.%d %H:%M:%S - ", gmtime()) + system
        self.setWindowTitle("AC4QGP")
        self.lblVersion.setText(configuration.VERSION+"\n"+currentTime)
        # show Advanced Tab on/off
        tabIndex = 2
        self.tabWidget.setTabEnabled(tabIndex,configuration.SHOW_ADVANCED_SETTINGS)
        self.tabWidget.setStyleSheet("QTabBar::tab::disabled {width: 0; height: 0; margin: 0; padding: 0; border: none;} ")
        # slider showing usage of telegram queue
        self.vsQueueUsage.setMaximum(audioSettings.MAX_NR_OF_TELEGRAMS_IN_PARALLEL)
        self.vsQueueUsage.setStyleSheet("QSlider {color: black}") # dont work ?
        self.vsVolume.setStyleSheet("QSlider {color: black}") # dont work ?
        # tab color:
        self.tabWidget.setStyleSheet("background-color: gainsboro;")
        # line edit background color
        self.leUser.setStyleSheet("background-color: white;")
        self.leSamplingFrequency.setStyleSheet("background-color: white;")
        self.leCarrierFrequency.setStyleSheet("background-color: white;")
        self.leCarrierAmplitude.setStyleSheet("background-color: white;")
        self.leTelegramMaxLenBytes.setStyleSheet("background-color: white;")
        self.leMaxNrOfChunksPerTelegram.setStyleSheet("background-color: white;")
        self.leAmplitude.setStyleSheet("background-color: white;")
        self.leFftDetectionLevel.setStyleSheet("background-color: white;")
        self.leMaxNrOfResends.setStyleSheet("background-color: white;")
        self.leMaxChannelDelayMs.setStyleSheet("background-color: white;")
        # label
        self.lblAudioChunksBytesLen.setText(str(audioSettings.AUDIO_CHUNK_BYTES_LEN))
        # borders:
        self.gbMode.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbAudioDevices.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbStego.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbChatTop.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbStoreSettings.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbUser.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbAdvancedGeneral.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbProtocol.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbLogging.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbPlot.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbChatInfo.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbChatInfo2.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbAdvancedAudio.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbPerformance.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbFrequencyChannel.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbAudioInput.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbAudioInputRx.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbAudioOutput.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbCarrier.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbStatistics.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbSessionCode.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.gbCommPartner.setStyleSheet("QGroupBox { border: 1px solid gray;}")
        self.lblDefaultInputDevice.setStyleSheet("QLabel { color : gray; border: 1px solid gray;}")
        self.lblDefaultOutputDevice.setStyleSheet("QLabel { color : gray; border: 1px solid gray;}")
        # chat scroll direction
        self.teChat.verticalScrollBar().setValue(self.teChat.verticalScrollBar().maximum())
        self.teChat.moveCursor(QTextCursor.End)
        # chat colors, images,..
        self.teWrite.setStyleSheet("border: 1px solid solid gray; background-color : gainsboro; font: "+str(configuration.TEXT_SIZE)+"pt "+configuration.TEXT_FAMILY)
        self.setTeChatStyleSheet()
        # chat TX
        self.teChat.setAlignment(Qt.AlignRight)
        self.teChat.setTextColor(QColor(0, 0, 255))
        tc = self.teChat.textCursor()
        # set anchor to true so we can use hyperlinks when needed
        tc.charFormat().setAnchor(True)
        # WORKAROUND: to force scroll to bottom
        for i in range(25):
            tc.insertText("\n")
        # position cursors
        self.teChat.moveCursor(QTextCursor.End)
        self.teWrite.moveCursor(QTextCursor.End)
        # set current tab
        tabIndex = 0
        self.tabWidget.setCurrentIndex(tabIndex)
        # state of buttons
        self.pbVoiceOn.setEnabled(False)
        self.pbHangUp.setEnabled(False)
        self.enableChat(False)
        # NOTE: pbCall shall be disabled "after" calling enableChat(False) because there pbCall is "enabled".
        self.pbCall.setEnabled(False)
        # tooltips
        self.pbOnOff.setToolTip("Turn chat on/off")
        self.pbVoiceOn.setToolTip("Turn voice transmission on/off.\nVoice may interfere with text chat!")
        self.pbCall.setToolTip("Call/accept call")
        self.pbRemoteCam.setToolTip("Remote image (videoconference in low resolution)")
        self.pbLocalCam.setToolTip("Local image (videoconference in low resolution)")
        self.pbHangUp.setToolTip("Hang-up/reject call")
        self.pbCallState.setToolTip("Call status")
        self.pbSend.setToolTip("Send the written message")
        self.pbAttachEmoji.setToolTip("Add an emoji to the message")
        self.pbOpenCam.setToolTip("Open a webcam for low resolution videoconferencing in parallel to chat (FEATURE NOT YET AVAILABLE!)")
        self.pbOpenConsole.setToolTip("Open a remote console in parallel to chat (FEATURE NOT YET AVAILABLE!)")
        self.pbAttachFile.setToolTip("Select and send a file.\n(Not implemented yet. For now only file name with path sent.)")
        self.pbAttachLink.setToolTip("Insert and send a link beginning with http, https, or www.\n(Not implemented yet. For now only link text sent.)")
        self.pbAttachPhoto.setToolTip("Take a photo and send it (FEATURE NOT YET AVAILABLE!)")
        self.pbAttachSound.setToolTip("Record a sound message and send it (FEATURE NOT YET AVAILABLE!)")
        self.cbShowLiveStatus.setToolTip("Show live status on/off\nEnable if you really want to know if\nyour application is still working\ne.g. when you are waiting for an important call")
        self.cbSoundEffects.setToolTip("out sound-effects to default speaker/output-device ONLY if it is different than TX out")
        self.cbGroetzel.setToolTip("Detect bits in frequency domain using the Grötzel algorithm")
        self.cbRemoveRxCarrier.setToolTip("Remove carrier frequency received so we dont hear it")
        self.cbAddTxCarrier.setToolTip("Add carrier frequency to transmission (needed e.g. when communication device does not support CALL_MODE)")
        self.leMaxNrOfResends.setToolTip("Maximum number of retries after unacknowledged telegrams")
        self.leMaxChannelDelayMs.setToolTip("Maximum delay in communication channel (in one direction) in milliseconds")
        self.leCarrierFrequency.setToolTip("Frequency of carrier (needed e.g. when communication device does not support CALL_MODE)")
        self.leCarrierAmplitude.setToolTip("Amplitude of carrier (needed e.g. when communication device does not support CALL_MODE)")
        self.lblTelTxOk.setToolTip("Nr. of telegrams transmitted correctly")
        self.lblTelRxOk.setToolTip("Nr. of telegrams received correctly (even if it is repeated)")
        self.lblTelTxNok.setToolTip("Nr. of telegrams transmitted which were not acknowledged within timeout (= nr. of retransmissions)")
        self.lblTelRxNok.setToolTip("Nr. of telegrams received with errors, e.g. CRC (only telegrams with correct START considered)")
        self.lblTelTxErrPercent.setToolTip("Nr. of unacknowledged telegrams transmitted / total telegrams transmitted, in percent")
        self.lblTelRxErrPercent.setToolTip("Nr. of telegrams received with errors / total telegrams received, in percent")
        self.gbPerformance.setToolTip("Timing performance gathered during connection")
        self.gbStatistics.setToolTip("Communication statistics gathered during connection")
        self.gbCarrier.setToolTip("Transmit a tone to force permanent audio transmission and avoid signal deformation/cuts.\nWorkaround needed e.g. for smartphones in COMMUNICATION_MODE")
        self.gbAdvancedAudio.setToolTip("Advanced audio settings")
        self.gbPlot.setToolTip("Plot settings")
        self.gbLogging.setToolTip("Select logging level")
        self.gbProtocol.setToolTip("Advanced settings of communication protocol")
        self.gbAdvancedGeneral.setToolTip("General information like SW version")
        self.cbSendOnEnter.setToolTip("Send message after pressing ENTER")
        self.lblVersion.setToolTip(configuration.VERSION_TOOL_TIP)
        self.gbSessionCode.setToolTip("Verify with your communication partner over Voice Channel that you both have the \"same\" code for the current session!")
        self.lblSessionCode.setToolTip("Verify with your communication partner over Voice Channel that you both have the \"same\" code for the current session!")
        self.gbCommPartner.setToolTip("Name of communication partner (received securely/confidendtially!)")
        self.lblCommPartner.setToolTip("Name of communication partner (received securely/confidendtially!)")
        self.cbAudioInputRxUndistort.setToolTip("Undistort voice (NOT IMPLEMENTED YET!)")
        self.gbStego.setToolTip("Hide communication using steganography (NOT IMPLEMENTED YET!)")
        self.cbStegoOn.setToolTip("Hide communication using steganography (NOT IMPLEMENTED YET!)")
        # disable MODE until we implement it
        self.gbMode.setEnabled(False)
        # set color
        self.lblStatus.setStyleSheet('QLabel {color: green}')
        # distortion
        self.cbAudioInputRxUndistort.setEnabled(False) # not yet implemented!
        #####################
        # thread to upate GUI perdiodically
        #####################
        self._thread = self.MyGuiUpdateThread(self)
        self._thread.updated.connect(self.updateGui)
        self._thread.start()
        # set flag
        self.appStarted = True
        
    def clearSessionData(self):
         self.lblCommPartner.setText("----")
         self.lblSessionCode.setText("----")
        
    # NOTE: here we always do the opposite with pbCall
    def enableChat(self, enable):
        self.pbCall.setEnabled(not enable) 
        self.pbSend.setEnabled(enable)
        self.pbOpenCam.setEnabled(False) # (enable) # disable until implemented
        self.pbOpenConsole.setEnabled(False) # (enable) # disable until implemented
        self.pbAttachFile.setEnabled(enable)
        self.pbAttachLink.setEnabled(enable)
        self.pbAttachPhoto.setEnabled(False) # (enable) # disable until implemented
        self.pbAttachSound.setEnabled(False) # (enable) # disable until implemented
        
    def pushWait(self):
        sleep(0.1) # (0.1)
        
    def callingIcon(self):
        sleep(0.5)
        currentIcon = 2
        while(self.chat_state==CHAT_STATE_CALLING):
            self.pbCallState.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/calling'+str(currentIcon)+'.png'))
            sleep(0.5)
            currentIcon = (currentIcon)%6 + 1
        if self.chat_state==CHAT_STATE_KEY_EXCHANGE_START: # or self.chat_state=="connected":
            sleep(2.0) # to be in sync with audio
            self.pbCallState.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/connected.png'))
        else: # elif self.chat_state == "idle": # we leave this thread leaving always the idle icon
            sleep(2.0) # to be in sync with audio
            self.pbCallState.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/idle.png'))
            
    # thread
    def callingSound(self):
        # we check configuration.SOUND_EFFECTS, it can be disabled and re-enabled while inside this thread...
        while (self.chat_state==CHAT_STATE_CALLING) and configuration.SOUND_EFFECTS:
            playsound(configuration.PATH_PREFIX+'sounds/calling.mp3')
            if self.chat_state==CHAT_STATE_CONNECTED:
                playsound(configuration.PATH_PREFIX+'sounds/connected.mp3')
                return
            else:
                sleep(2.5)
        if self.chat_state==CHAT_STATE_CONNECTED:
            playsound(configuration.PATH_PREFIX+'sounds/connected.mp3')
            
    # helper method
    def processCallReject(self):
        # purge event
        self.sdMgr.purge()
        self.sdMgr.isCall()
        self.chat_state = CHAT_STATE_IDLE
        logging.info("chat_state -> idle (processCallReject)")
        self.clearSessionData()
        self.pbHangUp.setEnabled(False)
        self.enableChat(False)
        self.pbCallState.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/idle.png'))
        if configuration.SOUND_EFFECTS:
            playsound(configuration.PATH_PREFIX+'sounds/cancel_call.mp3')
            
    # thread
    def calling(self):
        self.chat_state = CHAT_STATE_CALLING
        logging.info("chat_state -> calling (calling)")
        self.clearSessionData()
        self.pbCall.setEnabled(False)
        self.pbHangUp.setEnabled(True)
        self.pbCallState.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/calling1.png'))
        # update calling icon thread
        callingIconThread = threading.Thread(name="callingIcon", target=self.callingIcon)
        callingIconThread.start()
        # update calling sound
        if configuration.SOUND_EFFECTS:
            callingSoundThread = threading.Thread(name="callingSound", target=self.callingSound)
            callingSoundThread.start()
        resend_cntr = -1
        # call *** INFINITE LOOP ***
        while((self.chat_state==CHAT_STATE_CALLING) and (self.abort_outgoing_call == False) and (resend_cntr<audioSettings.MAX_RESENDS)):
            # blocking-function-call
            call_answer = self.sdMgr.call_once()
            # TODO: add a delay here?
            # call was accepted?
            if call_answer == audioSettings.COMMAND_CALL_ACCEPTED:
                self.chat_state = CHAT_STATE_KEY_EXCHANGE_START
                logging.info("chat_state -> key_exchange_start (calling)")
                # generate public key
                self.sdMgr.generatePublicKey()
                resend_cntr = -1
                while((self.chat_state==CHAT_STATE_KEY_EXCHANGE_START) and (self.abort_outgoing_call == False) and (resend_cntr<audioSettings.MAX_RESENDS)):
                    # blocking-function-call (resends automatically..)
                    key_start_answer = self.sdMgr.send_key_start_once()
                    # first part of keys exchanged?
                    if key_start_answer == audioSettings.COMMAND_KEY_START:
                        self.chat_state = CHAT_STATE_KEY_EXCHANGE_END
                        logging.info("chat_state -> key_exchange_end (calling)")
                        resend_cntr = -1
                        while((self.chat_state==CHAT_STATE_KEY_EXCHANGE_END) and (self.abort_outgoing_call == False) and (resend_cntr<audioSettings.MAX_RESENDS)):
                            # blocking-function-call (resends automatically..)
                            key_end_answer = self.sdMgr.send_key_end_once()
                            # last part of key exchanged?
                            if key_end_answer == audioSettings.COMMAND_KEY_END:
                                # set session code label
                                self.lblSessionCode.setText(self.sdMgr.getSessionCode())
                                self.chat_state = CHAT_STATE_STARTUP_DATA
                                logging.info("chat_state -> startup_data (calling)")
                                resend_cntr = -1
                                while((self.chat_state==CHAT_STATE_STARTUP_DATA) and (self.abort_outgoing_call == False) and (resend_cntr<audioSettings.MAX_RESENDS)):
                                    # blocking-function-call
                                    startup_data_answer = self.sdMgr.send_startup_data_once(configuration.USER_NAME) # same as (self.leUser.text())
                                    # last part of key exchanged?
                                    if startup_data_answer == audioSettings.COMMAND_STARTUP_DATA:
                                        # set comm_partner label
                                        startup_data = self.sdMgr.getStartupData()
                                        self.lblCommPartner.setText(startup_data.comm_partner)
                                        # state transition
                                        self.chat_state = CHAT_STATE_CONNECTED
                                        logging.info("chat_state -> connected (calling)")
                                        self.enableChat(True)
                                    # call was rejected?
                                    elif key_end_answer == audioSettings.COMMAND_CALL_REJECTED: 
                                        self.processCallReject()
                                    resend_cntr += 1
                            # call was rejected?
                            elif key_end_answer == audioSettings.COMMAND_CALL_REJECTED: 
                                self.processCallReject()
                            resend_cntr += 1
                    # call was rejected?
                    elif key_start_answer == audioSettings.COMMAND_CALL_REJECTED: 
                        self.processCallReject()
                    resend_cntr += 1
            # call was rejected?
            elif call_answer == audioSettings.COMMAND_CALL_REJECTED: 
                self.processCallReject()
            ### resend_cntr += 1 # infinite loop for CALL commands
        # max resends exceeded?
        if resend_cntr > audioSettings.MAX_RESENDS: 
            self.processCallReject()
        # reset flag
        self.abort_outgoing_call = False
        
    def incomingCallIcon(self):
        sleep(0.5)
        while(self.chat_state==CHAT_STATE_INCOMING_CALL):
            self.pbCallState.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/call.png'))
            sleep(0.5)
            self.pbCallState.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/incoming_call.png'))
            sleep(0.5)
        if self.chat_state==CHAT_STATE_CONNECTED:
            sleep(2.0) # to be in sync with audio
            self.pbCallState.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/connected.png'))
        else: ### elif self.chat_state=="reject_call": # we leave this thread always setting idle icon
            sleep(2.0) # to be in sync with audio
            self.pbCallState.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/idle.png'))
            
    # thread
    def incomingCallSound(self):
        # we check configuration.SOUND_EFFECTS, it can be disabled and re-enabled while inside this thread...
        while (self.chat_state==CHAT_STATE_INCOMING_CALL) and configuration.SOUND_EFFECTS:
            playsound(configuration.PATH_PREFIX+'sounds/incoming_call.mp3')
            if self.chat_state==CHAT_STATE_CONNECTED:
                playsound(configuration.PATH_PREFIX+'sounds/connected.mp3')
                return
            else:
                sleep(2.5)
        if self.chat_state==CHAT_STATE_CONNECTED:
            playsound(configuration.PATH_PREFIX+'sounds/connected.mp3')
            
    # thread
    def incoming_call(self):
        # NOTE: we may have already accepted the incoming call and now we are in chat_state == key_exchange_start
        #            while a CALL retry from the other side is received. In that case we just go out of this method.
        if (self.chat_state != CHAT_STATE_REJECT_CALL) and (self.chat_state != CHAT_STATE_KEY_EXCHANGE_START):
            # are we also calling?
            if self.chat_state == CHAT_STATE_CALLING:
                # accept incoming call while calling ONLY if we dont have the token, otherwise ignore!
                if self.sdMgr.haveToken() == False:
                    # then establish connection
                    logging.info("incoming_call will be accpeted, we don't have the token, so we stop calling..")
                    # here we send out an accept_call() and change chat_state to connected
                    self.on_pbCall_clicked()
                else:
                    logging.info("incoming_call ignored, we have the token, so we continue calling..")
            else:
                self.chat_state = CHAT_STATE_INCOMING_CALL
                logging.info("chat_state -> incoming_call (incoming_call)")
                self.pbCall.setEnabled(True)
                self.pbHangUp.setEnabled(True)
                self.pbCallState.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/incoming_call.png'))
                # update incoming icon thread
                incomingCallIconThread = threading.Thread(name="incomingCallIcon", target=self.incomingCallIcon)
                incomingCallIconThread.start()
                # update calling sound
                if configuration.SOUND_EFFECTS:
                    incomingCallSoundThread = threading.Thread(name="incomingCallSound", target=self.incomingCallSound)
                    incomingCallSoundThread.start()
                # answer automatically
                if configuration.CALL_ANSWER_AUTO == True:
                    self.on_pbCall_clicked()
            
    # thread
    def callReject(self):
        # blocking-function-call
        self.sdMgr.call_reject()
        # block for some time in order to reject new calls sent before the sender realized that we have rejected
        # note we are in chat_state = "reject_call"
        # optimistic delay without retransmission
        #######################
        # TODO: consider making sure to get an ACK to this notification or implement life-signs
        delay = audioSettings.TELEGRAM_MAX_LEN_SECONDS + audioSettings.CHANNEL_DELAY_SEC
        sleep(delay)
        #######################
        # purge event
        self.sdMgr.purge() # may cancel outgoing resends...
        self.sdMgr.isCall()
        # now make the actual state transition
        self.chat_state = CHAT_STATE_IDLE
        logging.info("chat_state -> idle (callReject)")
        self.clearSessionData()
        self.pbCallState.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/idle.png'))
        
    # thread
    def callAccept(self):
        # start key exchange
        # NOTE: the other side may have already sent or is currently sending KEY_START as well...as reaction to our CALL_ACCEPT..
        #######################################################################
        self.chat_state = CHAT_STATE_KEY_EXCHANGE_START
        logging.info("chat_state -> key_exchange_start (callAccept)")
        # generate public key
        self.sdMgr.generatePublicKey()
        resend_cntr = -1
        while((self.chat_state==CHAT_STATE_KEY_EXCHANGE_START) and (self.abort_outgoing_call == False) and (resend_cntr<audioSettings.MAX_RESENDS)):
            # accept call
            self.sdMgr.call_accept()
            # TODO: need delay here?
            # blocking-function-call
            key_start_answer = self.sdMgr.respond_key_start_once()
            # first part of keys exchanged?
            if key_start_answer == audioSettings.COMMAND_KEY_START:
                self.chat_state = CHAT_STATE_KEY_EXCHANGE_END
                logging.info("chat_state -> key_exchange_end (callAccept)")
                resend_cntr = -1
                while((self.chat_state==CHAT_STATE_KEY_EXCHANGE_END) and (self.abort_outgoing_call == False) and (resend_cntr<audioSettings.MAX_RESENDS)):
                    # blocking-function-call (resend automatically..)
                    key_end_answer = self.sdMgr.respond_key_end_once()
                    # last part of key exchanged?
                    if key_end_answer == audioSettings.COMMAND_KEY_END:
                        # set session code label
                        self.lblSessionCode.setText(self.sdMgr.getSessionCode())
                        self.chat_state = CHAT_STATE_STARTUP_DATA
                        logging.info("chat_state -> startup_data (callAccept)")
                        resend_cntr = -1
                        while((self.chat_state==CHAT_STATE_STARTUP_DATA) and (self.abort_outgoing_call == False) and (resend_cntr<audioSettings.MAX_RESENDS)):
                            # blocking-function-call (resend automatically..)
                            key_end_answer = self.sdMgr.respond_startup_data_once(configuration.USER_NAME) # same as (self.leUser.text())
                            # last part of key exchanged?
                            if key_end_answer == audioSettings.COMMAND_STARTUP_DATA:
                                # set comm_partner label
                                startup_data = self.sdMgr.getStartupData()
                                self.lblCommPartner.setText(startup_data.comm_partner)
                                # state transition
                                self.chat_state = CHAT_STATE_CONNECTED
                                logging.info("chat_state -> connected (callAccept)")
                                self.enableChat(True)
                                # call was rejected?
                            elif key_end_answer == audioSettings.COMMAND_CALL_REJECTED: 
                                self.processCallReject()
                            # timeout?
                            # last telegram sent may have been lost, so resend it..
                            else:
                                self.sdMgr.send_key_end_once()
                            resend_cntr += 1
                    # call was rejected?
                    elif key_end_answer == audioSettings.COMMAND_CALL_REJECTED: 
                        self.processCallReject()
                    # timeout?
                    # last telegram sent may have been lost, so resend it..
                    else:
                       self.sdMgr.send_key_start_once()
                    resend_cntr += 1
            # call was rejected?
            elif key_start_answer == audioSettings.COMMAND_CALL_REJECTED: 
                self.processCallReject()
            resend_cntr += 1
        # max resends exceeded?
        if resend_cntr > audioSettings.MAX_RESENDS: 
            self.processCallReject()
        # reset flag
        self.abort_outgoing_call = False
            
    # thread
    def incoming_call_end(self):
        # no matter who has the token we just abort any call..
        self.abort_outgoing_call = True
        # so, we received a CALL_END and "right now" we may be:
        # re-enabling transmission of telegrams and sending the ACK to that command and waiting a little bit to make sure ACK goes out with correct SeqNrs
        # therefore, before we transition back to IDLE, which may allow another CALL and may corrupt communication (e.g. seqNrs), we wait here also a little bit.
        delay = audioSettings.TELEGRAM_MAX_LEN_SECONDS + audioSettings.CHANNEL_DELAY_SEC
        sleep(delay)
        # purge event
        self.sdMgr.purge()
        self.sdMgr.isCall()
        self.chat_state = CHAT_STATE_IDLE
        logging.info("chat_state -> idle (incomng_call_end)")
        self.clearSessionData()
        self.pbHangUp.setEnabled(False)
        self.enableChat(False)
        self.pbCallState.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/idle.png'))
        if configuration.SOUND_EFFECTS:
            if self.chat_state == CHAT_STATE_CALLING:
                playsound(configuration.PATH_PREFIX+'sounds/cancel_call.mp3')
            else:
                playsound(configuration.PATH_PREFIX+'sounds/hang_up.mp3')
        # TODO: check this: is delay() above enough to get to evaluate flag in other thread before reset?
        # we reset flag
        self.abort_outgoing_call = False
                
    # thread
    def cancelOrHangUpSound(self):
        if self.chat_state == CHAT_STATE_CALLING:
            playsound(configuration.PATH_PREFIX+'sounds/cancel_call.mp3')
        else:
            playsound(configuration.PATH_PREFIX+'sounds/hang_up.mp3')
            
    def processCallHangUp(self):
        # DEBOUNCE: avoids actions on excessive clicking
        if (self.pushWaitThread is not None) and (self.pushWaitThread.is_alive() == True):
            return
        # process click
        if self.chat_state == CHAT_STATE_IDLE:
            # calling thread
            callingThread = threading.Thread(name="calling", target=self.calling)
            callingThread.start()
        elif (self.chat_state == CHAT_STATE_CALLING) or (self.chat_state == CHAT_STATE_CONNECTED) or (self.chat_state == CHAT_STATE_INCOMING_CALL):
            if self.chat_state == CHAT_STATE_CALLING:
                sleep(2.0) # to sync with calling icon/sound
            if self.chat_state == CHAT_STATE_INCOMING_CALL:
                # thread to reject call
                self.chat_state = CHAT_STATE_REJECT_CALL
                logging.info("chat_state -> reject_call (processCallHangUp)")
                self.clearSessionData()
                callRejectThread = threading.Thread(name="callReject", target=self.callReject)
                callRejectThread.start()
            else:
                # inform other side that we hang-up
                # BLOCKING call
                self.sdMgr.call_end()
                # give enough time for call_end to go out..
                delay = audioSettings.TELEGRAM_MAX_LEN_SECONDS + audioSettings.CHANNEL_DELAY_SEC
                sleep(delay)
                # purge event
                ###################
                # TODO: check: we dont purge in order to re-send call_end if required...this happens automatically at lower level (comm. stack)
                self.sdMgr.purge() ######
                ###################
                self.sdMgr.isCall()
                self.chat_state = CHAT_STATE_IDLE
                logging.info("chat_state -> idle (processCallHangUp)")
                self.clearSessionData()
            self.pbHangUp.setEnabled(False)
            self.enableChat(False)
            self.pbCallState.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/idle.png'))
            if configuration.SOUND_EFFECTS:
                cancelOrHangUpSoundThread = threading.Thread(name="cancelOrHangUpSound", target=self.cancelOrHangUpSound)
                cancelOrHangUpSoundThread.start()
        # debounce thread
        self.pushWaitThread = threading.Thread(name="pushWait", target=self.pushWait)
        self.pushWaitThread.start()
        
    @pyqtSlot()
    def on_pbCall_clicked(self):
        if self.abort_outgoing_call == False:
            if (self.chat_state == CHAT_STATE_INCOMING_CALL) or (self.chat_state == CHAT_STATE_CALLING):
                # thread to accept call
                #############
                callAcceptThread = threading.Thread(name="callAccept", target=self.callAccept)
                callAcceptThread.start()
            else:
                self.processCallHangUp()
        else:
            logging.info("pbCall pressed while abort_outgoing_call == True..we ignore button press!")
        
    @pyqtSlot()
    def on_pbHangUp_clicked(self):
        self.processCallHangUp()
        
    @pyqtSlot()
    def on_pbOnOff_clicked(self):
       # DEBOUNCE: avoids actions on excessive clicking
        if (self.pushWaitThread is not None) and (self.pushWaitThread.is_alive() == True):
            return
        # sound
        if configuration.SOUND_EFFECTS:
            playsound(configuration.PATH_PREFIX+'sounds/power_on_off.mp3')
        # process click
        if self.chat_state == CHAT_STATE_OFF:
            errorMessage = self.sdMgr.startDevices()
            if errorMessage != "":
                tkinter.messagebox.showerror(title="ERROR", message=errorMessage)
                # NOTE: call root.mainloop() to enable the program to respond to events. 
                root.update()
            else:
                self.pbOnOff.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/off.png'))
                self.pbCall.setEnabled(True)
                if configuration.AUDIO_DEVICE_TX_IN != "none":
                    if configuration.TRANSMIT_IN_TX_VOICE:
                        self.pbVoiceOn.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/voice_off.png'))
                        self.showTemporaryMessage("Warning: input TX audio may interfere text chat and slow it down!\n\n\
                Turn voice transmission off if you need a faster text chat.", 10000)
                    else:
                        self.pbVoiceOn.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/voice_on.png'))
                    self.pbVoiceOn.setEnabled(True)
                self.pbHangUp.setEnabled(False)
                # purge event
                self.sdMgr.purge() # TODO: need this?
                self.sdMgr.isCall()
                self.chat_state = CHAT_STATE_IDLE
                logging.info("chat_state -> idle (on_pbOnOff_clicked)")
                self.clearSessionData()
                if configuration.CALL_ANSWER_AUTO == True:
                    logging.info("Call automatically..")
                    self.processCallHangUp()
        else:
            if self.chat_state == CHAT_STATE_CONNECTED:
                # inform other side that we hang-up
                # BLOCKING call
                self.sdMgr.call_end()
                ##########################################
                # TODO: check return value of self.sdMgr.isCallEnd()
                #            for some time, and continue after it turns False again or timeout
                # For now we just add a delay to tive enough time to transmit call_end
                # optimistic delay without retransmission
                delay = audioSettings.TELEGRAM_MAX_LEN_SECONDS + audioSettings.CHANNEL_DELAY_SEC
                sleep(delay)
                ##########################################
            ### self.pbCall.setEnabled(False) # see comment below..
            self.pbVoiceOn.setEnabled(False)
            self.pbHangUp.setEnabled(False)
            self.clearSessionData()
            self.pbOnOff.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/on.png'))
            self.pbCall.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/call.png'))
            self.pbCallState.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/idle.png'))
            # in case we are receiving a call, we are so kind to send a CALL_REJECTED before stopping devices...
            # on the other side, the delay introduced below may give the chance for a new CALL to be processed...dont care, right?
            if self.chat_state == CHAT_STATE_INCOMING_CALL:
                # blocking-function-call
                self.sdMgr.call_reject()
                # block for some time in order to reject new calls sent before the sender realized that we have rejected
                # note we are in chat_state = "reject_call"
                # optimistic delay without retransmission
                # TODO: consider making sure to get an ACK to this notification or implement life-signs
                delay = audioSettings.TELEGRAM_MAX_LEN_SECONDS + audioSettings.CHANNEL_DELAY_SEC
                sleep(delay)
                # purge event
                ###################
                # TODO: check: we dont purge in order to re-send call_end if required...this happens automatically at lower level (comm. stack)
                ### self.sdMgr.purge() ###
                ###################
                self.sdMgr.isCall()
            # in all other cases we are so kind and send a CALL_END
            else:
                # inform other side that we hang-up
                # blocking-function-call
                self.sdMgr.call_end()
                # give enough time for call_end to go out..
                delay = audioSettings.TELEGRAM_MAX_LEN_SECONDS + audioSettings.CHANNEL_DELAY_SEC
                sleep(delay)
                # purge event
                ###################
                # TODO: check: we dont purge in order to re-send call_end if required...this happens automatically at lower level (comm. stack)
                ### self.sdMgr.purge() ###
                ###################
                self.sdMgr.isCall()
            # state transition
            self.chat_state = CHAT_STATE_OFF
            logging.info("chat_state -> off (on_pbOnOff_clicked)")
            # stop devices
            self.sdMgr.stopDevices()
            #######################################
            # TODO: set pbSend to disabled also in other states
            # for now we do it only here:
            self.enableChat(False)
            # TODO: is this the exception where we need disable BOTH pbCall and enableChat() icons ?:
            # so here we "override" what enableChat() did...which is setting pbCall always to the opposite enable state..
            self.pbCall.setEnabled(False)
            #######################################
        # debounce thread
        if configuration.CALL_ANSWER_AUTO == False:
            self.pushWaitThread = threading.Thread(name="pushWait", target=self.pushWait)
            self.pushWaitThread.start()
        
    def updateGuiConfig(self):
        self.leUser.setText(configuration.USER_NAME)
        self.cbCallAnswerAuto.setChecked(configuration.CALL_ANSWER_AUTO)
        self.cbShowLiveStatus.setChecked(configuration.SHOW_LIVE_STATUS)
        self.leFontFamily.setText(configuration.TEXT_FAMILY)
        self.cbTextBold.setChecked(configuration.TEXT_BOLD)
        self.setTeChatStyleSheet()
        self.hsTextSize.setValue(configuration.TEXT_SIZE) # this in turn sets lblTextSize
        index = self.cbTxIn.findText(configuration.AUDIO_DEVICE_TX_IN, Qt.MatchFixedString)
        # TX_IN
        if index >= 0:
            self.cbTxIn.setCurrentIndex(index)
        else:
            self.cbTxIn.setCurrentIndex(self.cbTxIn.count()-1) # last item is none
        # TX_OUT
        index = self.cbTxOut.findText(configuration.AUDIO_DEVICE_TX_OUT, Qt.MatchFixedString)
        if index >= 0:
            self.cbTxOut.setCurrentIndex(index)
        else:
            self.cbTxOut.setCurrentIndex(self.cbTxOut.count()-1) # last item is none
        # RX_IN
        index = self.cbRxIn.findText(configuration.AUDIO_DEVICE_RX_IN, Qt.MatchFixedString)
        if index >= 0:
            self.cbRxIn.setCurrentIndex(index)
        else:
            self.cbRxIn.setCurrentIndex(self.cbRxIn.count()-1) # last item is none
        # RX_OUT
        index = self.cbRxOut.findText(configuration.AUDIO_DEVICE_RX_OUT, Qt.MatchFixedString)
        if index >= 0:
            self.cbRxOut.setCurrentIndex(index)
        else:
            self.cbRxOut.setCurrentIndex(self.cbRxOut.count()-1) # last item is none
        # logging level
        self.updateLoggingLevel()
        # further settings
        self.cbPlotShow.setChecked(configuration.SHOW_PLOT)
        self.cbShowPerformance.setChecked(configuration.SHOW_PERFORMANCE)
        self.cbPlotFFT.setChecked(configuration.PLOT_FFT)
        self.cbPlotShowCodeOnly.setChecked(configuration.PLOT_CODE_ONLY)
        # dependencies between parameters need to be checked
        ###############################
        # enable sound effect option if required
        if self.cbRxOut.currentText() != "none":
            self.cbSoundEffects.setEnabled(True)
            self.cbSoundEffects.setChecked(configuration.SOUND_EFFECTS)
        else:
            self.cbSoundEffects.setEnabled(False)
            self.cbSoundEffects.setChecked(False)
            configuration.SOUND_EFFECTS = False
        ###############################
        self.cbSendOnEnter.setChecked(configuration.SEND_ON_ENTER)
        self.cbAudioInputTxDistort.setChecked(configuration.IN_TX_DISTORT)
        self.cbAudioInputRxUndistort.setChecked(configuration.IN_RX_UNDISTORT)
        self.cbAudioInputTxScramble.setChecked(configuration.IN_TX_SCRAMBLE)
        self.cbAudioOutputRxHearVoice.setChecked(configuration.OUT_RX_HEAR_VOICE)
        self.updateVoiceOn()
        # set values of audio settings
        self.cbFrequencyChannel.setCurrentIndex(audioSettings.CURRENT_FREQUENCY_CHANNEL)
        self.leSamplingFrequency.setText(str(audioSettings.SAMPLING_FREQUENCY))
        self.leCarrierFrequency.setText(str(audioSettings.CARRIER_FREQUENCY_HZ))
        self.leCarrierAmplitude.setText(str(audioSettings.CARRIER_AMPLITUDE))
        self.leTelegramMaxLenBytes.setText(str(audioSettings.TELEGRAM_MAX_LEN_BYTES))
        self.leMaxNrOfChunksPerTelegram.setText(str(audioSettings.MAX_NR_OF_CHUNKS_PER_TELEGRAM))
        self.leAmplitude.setText(str(audioSettings.AMPLITUDE))
        self.leFftDetectionLevel.setText(str(audioSettings.FFT_DETECTION_LEVEL))
        self.cbShowAdvancedSettings.setChecked(configuration.SHOW_ADVANCED_SETTINGS)
        self.leMaxChannelDelayMs.setText(str(audioSettings.CHANNEL_DELAY_MS))
        self.leMaxNrOfResends.setText(str(audioSettings.MAX_RESENDS))
        self.cbGroetzel.setChecked(audioSettings.DETECT_USING_GROETZEL)
        # deactivated for now, DETECT_USING_GROETZEL not used 
        self.cbGroetzel.setEnabled(False)
        self.cbRemoveRxCarrier.setChecked(audioSettings.REMOVE_RX_CARRIER)
        self.cbAddTxCarrier.setChecked(audioSettings.ADD_CARRIER)
    
    @pyqtSlot()
    def on_pbSettingsLoad_clicked(self):
        restorePlot = False
        # first close plot if active, otherwise thread sync error
        if configuration.SHOW_PLOT:
            configuration.SHOW_PLOT = False
            self.on_cbPlotShow_clicked()
            restorePlot = True
        # load config
        self.lblConfigFileName.setText(self.cfg.loadConfig())
        self.updateGuiConfig() 
        # restore plot if required
        if restorePlot:
            configuration.SHOW_PLOT = True
            self.on_cbPlotShow_clicked()
        # NOTE: call root.mainloop() to enable the program to respond to events. 
        root.update()
    
    @pyqtSlot()
    def on_pbSettingsSave_clicked(self):
        self.lblConfigFileName.setText(self.cfg.saveConfig())
        
    
    @pyqtSlot()
    def on_pbSettingsSaveAs_clicked(self):
        restorePlot = False
        # first close plot if active, otherwise thread sync error
        if configuration.SHOW_PLOT:
            configuration.SHOW_PLOT = False
            self.on_cbPlotShow_clicked()
            restorePlot = True
        # save config as
        self.lblConfigFileName.setText(self.cfg.saveConfigAs())
        # restore plot if required
        if restorePlot:
            configuration.SHOW_PLOT = True
            self.on_cbPlotShow_clicked()
        # NOTE: call root.mainloop() to enable the program to respond to events. 
        root.update()
    
    @pyqtSlot()
    def on_leUser_editingFinished(self):
        configuration.USER_NAME = self.leUser.text()
    
    @pyqtSlot(str)
    def on_cbTxIn_currentIndexChanged(self, p0):
        configuration.AUDIO_DEVICE_TX_IN = p0 # = self.cbTxIn.currentText()
    
    @pyqtSlot(str)
    def on_cbTxOut_currentIndexChanged(self, p0):
        configuration.AUDIO_DEVICE_TX_OUT = p0 # = self.cbTxOut.currentText()
    
    @pyqtSlot(str)
    def on_cbRxIn_currentIndexChanged(self, p0):
        configuration.AUDIO_DEVICE_RX_IN = p0 # = self.cbRxIn.currentText()
    
    @pyqtSlot(str)
    def on_cbRxOut_currentIndexChanged(self, p0):
        configuration.AUDIO_DEVICE_RX_OUT = p0 # = self.cbRxOut.currentText()
        # enable sound effect option if required
        if p0 != "none":
            self.cbSoundEffects.setEnabled(True)
        else:
            self.cbSoundEffects.setEnabled(False)
            self.cbSoundEffects.setChecked(False)
            configuration.SOUND_EFFECTS = False
    
    @pyqtSlot()
    def on_cbPlotFFT_clicked(self):
        configuration.PLOT_FFT = self.cbPlotFFT.isChecked()
        # TODO: check why this code does not change the titles...
        if configuration.PLOT_FFT:
            plt.title("Audio signal, FFT")
        else:
            plt.title("Audio signal, time domain")
        tkinter.messagebox.showwarning(title="WARNING", message="Save and restart in order to plot correctly.")
        # NOTE: call root.mainloop() to enable the program to respond to events. 
        root.update()
    
    @pyqtSlot()
    def on_cbCallAnswerAuto_clicked(self):
        configuration.CALL_ANSWER_AUTO = self.cbCallAnswerAuto.isChecked()
        
    @pyqtSlot()
    def on_cbTextBold_clicked(self):
       configuration.TEXT_BOLD = self.cbTextBold.isChecked()
       self.setTeChatStyleSheet()
       
    # TODO: use Lock to access teChat...?
    @pyqtSlot(int)
    def on_hsTextSize_valueChanged(self, value):
        self.lblTextSize.setText(str(value))
        configuration.TEXT_SIZE = value
        self.teWrite.setStyleSheet("border: 1px solid solid gray; background-color : gainsboro; font: "+\
        str(configuration.TEXT_SIZE)+"pt "+configuration.TEXT_FAMILY)
        self.setTeChatStyleSheet()
        self.teChat.moveCursor(QTextCursor.End)
        self.teWrite.moveCursor(QTextCursor.End)
        
    def changeShowPlotThread(self):
        # TODO:  this call just blocks forever. Investigate, and if possible call plt.close in Slot() instead.
        #             a "dead/blocked" thread remains in memory every time we switch off here.
        plt.close()
        # TODO: by the way, why can't we just do this instead?
        # plt.gcf().set_visible(False)
        # plt.draw() # needed to make previous call effective?
    
    @pyqtSlot()
    def on_cbPlotShow_clicked(self):
        if configuration.SHOW_PLOT != self.cbPlotShow.isChecked():
            configuration.SHOW_PLOT = self.cbPlotShow.isChecked()
            if configuration.SHOW_PLOT:
                # create and start plot thread anew..
                plotThread = threading.Thread(name="plotThread", target=self.plotThread)
                plotThread.start()
            else:
                # close plot thread in a separate thread which will BLOCK - that thread will call plt.close()
                changeShowPlotThread = threading.Thread(name="changeShowPlotThread", target=self.changeShowPlotThread)
                changeShowPlotThread.start()
     
    def sendMessage(self, writeText):
        self.teChatLock.acquire()
        # in case we clicked on teChat somewhere else:
        self.teChat.moveCursor(QTextCursor.End)
        self.teChat.setAlignment(Qt.AlignRight)
        self.teChat.setTextColor(QColor(0, 0, 255))
        tc = self.teChat.textCursor()
        if writeText != "":
            if writeText[-1] != "\n":
                writeText += "\n"
            tc.insertText(writeText)
            self.teChat.moveCursor(QTextCursor.End)
            self.teChatLock.release()
            # send over audio interface
            self.nrOfMessagesSent = self.nrOfMessagesSent + 1
            self.sdMgr.sendMessage(writeText)
        else:
            self.teChatLock.release()
            
    @pyqtSlot()
    def on_pbSend_clicked(self):
        #################################################################################
        # TODO: solve problems with Lock access to teChat WHEN MESSAGES ARE TOO BIG...
        #            if problem arises it messes-up AUDIO INTERFACES also..for ever!!!
        #            The exact limit is currently unknown but it may have something to do with the default buffers used by PyQt5 for the widget-objects...
        #################################################################################
        # for now we have this workaround:
        ####################
        texti = self.teWrite.toPlainText()
        if len(texti) < audioSettings.MAX_TEXT_LEN:
            self.teWrite.clear()
            # BLOCKING CALL...but it shall NOT take long...
            self.qWriteChat.put(texti)
        else:
            tkinter.messagebox.showerror(title="ERROR", message="Message too large! Allowed maximum message size is ..")
            # NOTE: call root.mainloop() to enable the program to respond to events. 
            root.update()
            
    @pyqtSlot()
    def on_pbAttachEmoji_clicked(self):
        # Key.cmd: a generic command button. On PC platforms, this corresponds to the Super key or Windows key.rk
        # Win10 feature to open an emoji-picker (we just press Win+'.' for the user)
        ###########################################
        if platform.system() == 'Windows':
            self.teWrite.setFocus() # without this it will NOT work
            keyboard.press(Key.cmd) # Win key in Win10
            keyboard.press('.')
            keyboard.release('.')
            keyboard.release(Key.cmd) # Win key in Win10
        else: # Linux
            command = "emote"
            # TODO: for some reason it does NOT wait here until we return
            # and the information we obtain "without" waiting is not what we need to paste on teWrite
            ###################################################
            p1 = subprocess.Popen(shlex.split(command),shell=True, stdout=subprocess.PIPE)
            # WORKAROUND: Popen returns immediately...
            # so we replace the wait(), which is also not working..
            # it does NOT work because we have: python3 -> batch (pid) -> python3 (emote, which disappears right away! even if still open...)
            #######################################################################
            # WORKAROUND 1: does not work!
            # while(self.checkIfProcessRunning(p1.pid)):
                # time.sleep(0.1)
            # WORKAROUND 2:  does not work!
            # time.sleep(2.0)
            # while(self.isActiveWindow() == False):
                 # time.sleep(0.1)
            # WORKAROUND 3:
            tkinter.messagebox.showinfo(title="INFO", message="Select icon(s) with right-mouse button and press OK")
            #######################################################################
            # p1.wait()
            out, err = p1.communicate()
            p1.wait()
            if p1.returncode == 0:
                self.teWrite.setFocus() # without this it will NOT work
                '''
                  # WORKAROUND 4: does not work!
                  keyboard.press(Key.ctrl)
                  keybos('V')
                  keyboard.release('V')
                  keyboard.release(Key.ctrl)
                # '''
                '''
                  keyboard.press(Key.shift)
                  keyboard.press(Key.insert)
                  keyboard.release(Key.insert)
                  keyboard.release(Key.shift)
                # '''
                tc = self.teWrite.textCursor()
                tc.insertText(self.app.clipboard().text())
                #pyperclip.paste()
                ###tc.insertText(pyperclip.paste())
            else:
                logging.error("Error in call to emote !")
                tkinter.messagebox.showerror(title="ERROR", message="Make sure emote is installed on your system.\n"\
                "Alternatively, you can copy&paste the icons from your editor (e.g. xed in Linux Mint).\n\
                In that case you may need to use the font 'Noto color emoji'.")
                # NOTE: call root.mainloop() to enable the program to respond to events. 
                root.update()
            p1.terminate()
            p1.kill()
    
    # TODO: use Lock to access teChat...?
    @pyqtSlot()
    def on_pbAttachFile_clicked(self):
        files = [ ('All Files', '*.*')]
        filename = askopenfilename(initialdir="./", filetypes = files, defaultextension = files) 
        if filename:
            os_filename = os.path.basename(filename)
            self.teChat.moveCursor(QTextCursor.End)
            tc = self.teChat.textCursor()
            linkFormat = tc.charFormat() # returns an object of type QTextCharFormat
            linkFormat.setFontUnderline(True)
            linkFormat.setAnchorHref(filename)
            tc.insertText(os_filename, linkFormat)
            # remove hyperlink by deleting AnchorHref and making a dummy write..
            linkFormat.setFontUnderline(False)
            linkFormat.setAnchorHref('')
            tc.insertText('\n', linkFormat)
            self.teChat.moveCursor(QTextCursor.End)
            # send over audio interface
            # TODO: need to transfer the actual file here....
            self.nrOfMessagesSent = self.nrOfMessagesSent + 1
            self.sdMgr.sendMessage(filename+"\n")
        # NOTE: call root.mainloop() to enable the program to respond to events. 
        root.update()
    
    # TODO: use Lock to access teChat...?
    @pyqtSlot()
    def on_pbAttachLink_clicked(self):
        text, ok = QInputDialog.getText(self, 'Input Dialog', 'Enter link:')
        if ok:
            if ("http" in str(text)) or ("www" in str(text)):
                self.teChat.moveCursor(QTextCursor.End)
                tc = self.teChat.textCursor()
                linkFormat = tc.charFormat() # returns an object of type QTextCharFormat
                linkFormat.setFontUnderline(True)
                linkFormat.setAnchorHref(str(text))
                tc.insertText(str(text), linkFormat)
                # remove hyperlink by deleting AnchorHref and making a dummy write..
                linkFormat.setFontUnderline(False)
                linkFormat.setAnchorHref('')
                tc.insertText('\n', linkFormat)
                self.teChat.moveCursor(QTextCursor.End)
                # send over audio interface
                # TODO: need to pass with some tag or especial format in order to make it clickable on the receiver
                self.nrOfMessagesSent = self.nrOfMessagesSent + 1
                self.sdMgr.sendMessage(str(text)+"\n")
    
    @pyqtSlot()
    def on_cbSoundEffects_clicked(self):
        configuration.SOUND_EFFECTS = self.cbSoundEffects.isChecked()
    
    @pyqtSlot()
    def on_leSamplingFrequency_editingFinished(self):
        if self.appStarted:
            # NOTE: in an encapsulated class setting SAMPLING_FREQUENCY with a setter method would hide the details
            #            and would itself update all necessary things in the background.
            #            Besides, audioSettings should better be hidden behind soundDeviceManager or some nice interface.
            #            As usual, in "extreme programming" we take all sorts of short-cuts and leave all that stuff until the end..
            #            ...but then of course, there is no time for doing that!  ;-)
            #            Well yes, this is certainly not an academic code.
            if audioSettings.SAMPLING_FREQUENCY != int(self.leSamplingFrequency.text()):
                audioSettings.SAMPLING_FREQUENCY = int(self.leSamplingFrequency.text())
                audioSettings.updateDerivedAudioSettings()
                tkinter.messagebox.showwarning(title="WARNING", message="Press Save button, \
                selected value will take effect after new start..")
                # NOTE: call root.mainloop() to enable the program to respond to events. 
                root.update()
            
    @pyqtSlot()
    def on_leTelegramMaxLenBytes_editingFinished(self):
         if self.appStarted:
            if audioSettings.TELEGRAM_MAX_LEN_BYTES != int(self.leTelegramMaxLenBytes.text()):
                audioSettings.TELEGRAM_MAX_LEN_BYTES = int(self.leTelegramMaxLenBytes.text())
                audioSettings.updateDerivedAudioSettings()
                self.lblAudioChunksBytesLen.setText(str(audioSettings.AUDIO_CHUNK_BYTES_LEN))
                tkinter.messagebox.showwarning(title="WARNING", message="Press Save button, \
                selected value will take effect after new start..")
                # NOTE: call root.mainloop() to enable the program to respond to events. 
                root.update()
            
    @pyqtSlot()
    def on_leMaxNrOfChunksPerTelegram_editingFinished(self):
           if self.appStarted:
            if audioSettings.MAX_NR_OF_CHUNKS_PER_TELEGRAM != int(self.leMaxNrOfChunksPerTelegram.text()):
                audioSettings.MAX_NR_OF_CHUNKS_PER_TELEGRAM = int(self.leMaxNrOfChunksPerTelegram.text())
                audioSettings.updateDerivedAudioSettings()
                self.lblAudioChunksBytesLen.setText(str(audioSettings.AUDIO_CHUNK_BYTES_LEN))
                tkinter.messagebox.showwarning(title="WARNING", message="Press Save button, \
                selected value will take effect after new start..")
                # NOTE: call root.mainloop() to enable the program to respond to events. 
                root.update()
            
    @pyqtSlot()
    def on_leAmplitude_editingFinished(self):
        audioSettings.AMPLITUDE = float(self.leAmplitude.text())
        audioSettings.updateDerivedAudioSettings()
        
    @pyqtSlot()
    def on_leFftDetectionLevel_editingFinished(self):
        audioSettings.FFT_DETECTION_LEVEL = float(self.leFftDetectionLevel.text())
        audioSettings.updateDerivedAudioSettings()
    
    @pyqtSlot()
    def on_cbShowPerformance_clicked(self):
        configuration.SHOW_PERFORMANCE = self.cbShowPerformance.isChecked()
    
    @pyqtSlot(str)
    def on_leSamplingFrequency_textChanged(self, p0):
        # NOTE: we'd rather don't use this method but for some reason editingFinished() is no longer working
        # TODO: make editingFinished() work and remove this method.
        self.on_leSamplingFrequency_editingFinished()
    
    @pyqtSlot(str)
    def on_leAmplitude_textChanged(self, p0):
        # NOTE: we'd rather don't use this method but for some reason editingFinished() is no longer working
        # TODO: make editingFinished() work and remove this method.
        self.on_leAmplitude_editingFinished()
        
    @pyqtSlot(str)
    def on_leFftDetectionLevel_textChanged(self, p0):
        # NOTE: we'd rather don't use this method but for some reason editingFinished() is no longer working
        # TODO: make editingFinished() work and remove this method.
        self.on_leFftDetectionLevel_editingFinished()
    
    @pyqtSlot(int)
    def on_cbFrequencyChannel_currentIndexChanged(self, index):
        if self.appStarted:
            # WARNING: for now only some values allowed...
            if index not in audioSettings.ALLOWED_FREQUENCY_CHANNELS:
                tkinter.messagebox.showerror(title="ERROR", message="Frequency Channel/Band not support for now.")
                # NOTE: call root.mainloop() to enable the program to respond to events. 
                root.update()
                self.cbFrequencyChannel.setCurrentIndex(audioSettings.CURRENT_FREQUENCY_CHANNEL)
            else:
                if index != audioSettings.CURRENT_FREQUENCY_CHANNEL:
                    audioSettings.CURRENT_FREQUENCY_CHANNEL = index
                    tkinter.messagebox.showwarning(title="WARNING", message="Press Save button, \
                    selected Frequency Channel/Band will take effect after new start..")
                    # NOTE: call root.mainloop() to enable the program to respond to events. 
                    root.update()
            
    @pyqtSlot()
    def on_cbAudioInputTxDistort_clicked(self):
        configuration.IN_TX_DISTORT = self.cbAudioInputTxDistort.isChecked()
        
    @pyqtSlot()
    def on_cbAudioInputRxUndistort_clicked(self):
        configuration.IN_RX_UNDISTORT = self.cbAudioInputRxUndistort.isChecked()
    
    @pyqtSlot()
    def on_cbAudioInputTxScramble_clicked(self):
        configuration.IN_TX_SCRAMBLE = self.cbAudioInputTxScramble.isChecked()
        if configuration.IN_TX_SCRAMBLE == True:
            tkinter.messagebox.showwarning(title="WARNING", message="Scrambling not yet implemented..")
            # NOTE: call root.mainloop() to enable the program to respond to events. 
            root.update()
    
    @pyqtSlot()
    def on_cbAudioOutputRxHearVoice_clicked(self):
        configuration.OUT_RX_HEAR_VOICE = self.cbAudioOutputRxHearVoice.isChecked()
    
    @pyqtSlot()
    def on_cbPlotShowCodeOnly_clicked(self):
        configuration.PLOT_CODE_ONLY = self.cbPlotShowCodeOnly.isChecked()
    
    @pyqtSlot()
    def on_cbShowAdvancedSettings_clicked(self):
        configuration.SHOW_ADVANCED_SETTINGS = self.cbShowAdvancedSettings.isChecked()
        # show Advanced Tab on/off
        tabIndex = 2
        self.tabWidget.setTabEnabled(tabIndex,configuration.SHOW_ADVANCED_SETTINGS)
    
    @pyqtSlot()
    def on_pbSelectFont_clicked(self):
        font, ok = QFontDialog.getFont()
        if ok:
            configuration.TEXT_BOLD = font.bold()
            configuration.TEXT_FAMILY = font.family()
            configuration.TEXT_SIZE = font.pointSize()
            self.cbTextBold.setChecked(configuration.TEXT_BOLD)
            self.leFontFamily.setText(configuration.TEXT_FAMILY)
            self.hsTextSize.setValue(configuration.TEXT_SIZE) # this in turn sets lblTextSize
            self.teWrite.setStyleSheet("border: 1px solid solid gray; background-color : \
            gainsboro; font: "+str(configuration.TEXT_SIZE)+"pt "+configuration.TEXT_FAMILY)
            self.setTeChatStyleSheet()
            
    def showTemporaryMessage(self, message, durationMs):
        top = tkinter.Toplevel()
        top.title('Warning')
        root.geometry("500x500")
        tkinter.Message(top, text=message, padx=20, pady=100).pack()
        # NOTE: call root.mainloop() to enable the program to respond to events. 
        root.update()
        top.after(durationMs, top.destroy)
    
    def updateVoiceOn(self):
        # TODO: remove if condition
        if configuration.AUDIO_DEVICE_TX_IN != "none":
            # configuration.TRANSMIT_IN_TX_VOICE = not configuration.TRANSMIT_IN_TX_VOICE
            if configuration.TRANSMIT_IN_TX_VOICE:
                self.pbVoiceOn.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/voice_off.png'))
                if self.appStarted:
                    self.showTemporaryMessage("Warning: input TX audio may interfere text chat and slow it down!\n\n\
            Turn voice transmission off if you need a faster text chat.", 10000)
            else:
                self.pbVoiceOn.setIcon(QtGui.QIcon(configuration.PATH_PREFIX+'icons/voice_on.png'))
            if self.chat_state != CHAT_STATE_OFF:
                self.pbVoiceOn.setEnabled(True)
        else:
            self.pbVoiceOn.setEnabled(False)
            
    def toggleVoiceOn(self):
        configuration.TRANSMIT_IN_TX_VOICE = not configuration.TRANSMIT_IN_TX_VOICE
        self.updateVoiceOn()
    
    @pyqtSlot()
    def on_pbVoiceOn_clicked(self):
        self.toggleVoiceOn()
    
    @pyqtSlot(str)
    def on_leMaxNrOfResends_textChanged(self, p0):
        # NOTE: we'd rather don't use this method but for some reason editingFinished() is no longer working
        # TODO: make editingFinished() work and remove this method.
        self.on_leMaxNrOfResends_editingFinished()
    
    @pyqtSlot()
    def on_leMaxNrOfResends_editingFinished(self):
      if self.appStarted:
            if audioSettings.MAX_RESENDS != int(self.leMaxNrOfResends.text()):
                audioSettings.MAX_RESENDS = int(self.leMaxNrOfResends.text())
                audioSettings.updateDerivedAudioSettings()
                tkinter.messagebox.showwarning(title="WARNING", message="Press Save button, \
                selected value will take effect after new start..")
                # NOTE: call root.mainloop() to enable the program to respond to events. 
                root.update()
    
    @pyqtSlot(str)
    def on_leMaxChannelDelayMs_textChanged(self, p0):
        # NOTE: we'd rather don't use this method but for some reason editingFinished() is no longer working
        # TODO: make editingFinished() work and remove this method.
        self.on_leMaxChannelDelayMs_editingFinished()
    
    @pyqtSlot()
    def on_leMaxChannelDelayMs_editingFinished(self):
        if self.appStarted:
            if audioSettings.CHANNEL_DELAY_MS != int(self.leMaxChannelDelayMs.text()):
                audioSettings.CHANNEL_DELAY_MS = int(self.leMaxChannelDelayMs.text())
                audioSettings.updateDerivedAudioSettings()
                tkinter.messagebox.showwarning(title="WARNING", message="Press Save button, \
                selected value will take effect after new start..")
                # NOTE: call root.mainloop() to enable the program to respond to events. 
                root.update()
    
    @pyqtSlot()
    def on_cbGroetzel_clicked(self):
        audioSettings.DETECT_USING_GROETZEL = self.cbGroetzel.isChecked()
        tkinter.messagebox.showwarning(title="WARNING", message="Save and restart in order to plot correctly.")
        # NOTE: call root.mainloop() to enable the program to respond to events. 
        root.update()
    
    @pyqtSlot(str)
    def on_cbLoggingLevel_currentIndexChanged(self, p0):
        if self.appStarted:
            configuration.LOGGING_LEVEL = p0 # self.cbLoggingLevel.currentText()
            self.updateLoggingLevel()
            tkinter.messagebox.showwarning(title="WARNING", message="Save and restart in order to plot correctly.")
            # NOTE: call root.mainloop() to enable the program to respond to events. 
            root.update()
            
    def updateLoggingLevel(self):
        index = self.cbLoggingLevel.findText(configuration.LOGGING_LEVEL, Qt.MatchFixedString)
        if index >= 0:
            self.cbLoggingLevel.setCurrentIndex(index)
            # if the severity level is INFO, the logger will handle only INFO, WARNING, ERROR, and CRITICAL messages and will ignore DEBUG messages
            # logging.basicConfig(format='%(asctime)s.%(msecs)03d %(levelname)s {%(module)s} [%(funcName)s] %(message)s', datefmt='%H:%M:%S', level=logging.INFO)
            logging_level = logging.INFO
            if configuration.LOGGING_LEVEL == "logging.DEBUG":
                logging_level = logging.DEBUG
            if configuration.LOGGING_LEVEL == "logging.INFO":
                logging_level = logging.INFO
            if configuration.LOGGING_LEVEL == "logging.WARNING":
                logging_level = logging.WARNING
            if configuration.LOGGING_LEVEL == "logging.ERROR":
                logging_level = logging.ERROR
            if configuration.LOGGING_LEVEL == "logging.CRITICAL":
                logging_level = logging.CRITICAL    
            logging.basicConfig(format='%(asctime)s.%(msecs)03d %(message)s', datefmt='%H:%M:%S', level=logging_level)
        else:
            self.cbLoggingLevel.setCurrentIndex(1) # default INFO
            logging.basicConfig(format='%(asctime)s.%(msecs)03d %(message)s', datefmt='%H:%M:%S', level=logging.INFO)
    
    @pyqtSlot()
    def on_cbAddTxCarrier_clicked(self):
        audioSettings.ADD_CARRIER = self.cbAddTxCarrier.isChecked()
    
    @pyqtSlot()
    def on_cbRemoveRxCarrier_clicked(self):
        audioSettings.REMOVE_RX_CARRIER = self.cbRemoveRxCarrier.isChecked()
    
    @pyqtSlot(str)
    def on_leCarrierFrequency_textChanged(self, p0):
        # NOTE: we'd rather don't use this method but for some reason editingFinished() is no longer working
        # TODO: make editingFinished() work and remove this method.
        self.on_leCarrierFrequency_editingFinished()
    
    @pyqtSlot()
    def on_leCarrierFrequency_editingFinished(self):
        if audioSettings.CARRIER_FREQUENCY_HZ != int(self.leCarrierFrequency.text()):
            audioSettings.CARRIER_FREQUENCY_HZ = int(self.leCarrierFrequency.text())
            audioSettings.updateDerivedAudioSettings()
    
    @pyqtSlot()
    def on_cbSendOnEnter_clicked(self):
        configuration.SEND_ON_ENTER = self.cbSendOnEnter.isChecked()

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.KeyPress and obj is self.teWrite:
            if event.key() == QtCore.Qt.Key_Return and self.teWrite.hasFocus():
                if self.pbSend.isEnabled() and configuration.SEND_ON_ENTER:
                    self.on_pbSend_clicked()
        return super().eventFilter(obj, event)
    
    @pyqtSlot(str)
    def on_leCarrierAmplitude_textChanged(self, p0):
         # NOTE: we'd rather don't use this method but for some reason editingFinished() is no longer working
        # TODO: make editingFinished() work and remove this method.
        self.on_leCarrierAmplitude_editingFinished()
    
    @pyqtSlot()
    def on_leCarrierAmplitude_editingFinished(self):
        if audioSettings.CARRIER_AMPLITUDE != float(self.leCarrierAmplitude.text()):
            audioSettings.CARRIER_AMPLITUDE = float(self.leCarrierAmplitude.text())
            audioSettings.updateDerivedAudioSettings()
    
    @pyqtSlot()
    def on_cbShowLiveStatus_clicked(self):
        configuration.SHOW_LIVE_STATUS = self.cbCallAnswerAuto.isChecked()
        
        
        
        

