# -*- coding: utf-8 -*-

import threading
import time
import configuration
import audioSettings
import platform
import ctypes
import os
from audioTransmitter import AudioTransmitter
from audioReceiver import AudioReceiver
import logging
import numpy as np
from dataclasses import dataclass


# WORKAROUND to load sounddevice library also in .exe
################################
if platform.system() == 'Windows':
    # '''
    # script or .exe?
    runningScript = os.path.basename(__file__)
    # different relative paths depending if we debug or run the executable file
    if(runningScript=="soundDeviceManager.py"): 
        # .py script
        # ctypes.windll.kernel32.SetDllDirectoryW("./dist/_sounddevice_data/portaudio-binaries/")
        print('loading ./dist/_sounddevice_data/portaudio-binaries/libportaudio64bit.dll')
        ctypes.CDLL('./dist/_sounddevice_data/portaudio-binaries/libportaudio64bit.dll')
        print('./dist/_sounddevice_data/portaudio-binaries/libportaudio64bit.dll loaded')
    else:
        # .exe file
        # ctypes.windll.kernel32.SetDllDirectoryW("./_sounddevice_data/portaudio-binaries/")
        print('loading ./_sounddevice_data/portaudio-binaries/libportaudio64bit.dll')
        ctypes.CDLL('./_sounddevice_data/portaudio-binaries/libportaudio64bit.dll')
        print('./_sounddevice_data/portaudio-binaries/libportaudio64bit.dll loaded')
    # '''
import sounddevice as sd
################################


# poll flag to leave thread every
THREAD_SLEEP_TIME_SEC = 1


class SoundDeviceManager:
    @dataclass
    class GlobVars:
       # flags as mutable objects (list, dict and set) to pass "reference" to other objects, e.g. object of type AudioReceiver
        stream_on: list
        # NOTE: transmit_on_ref and transmit_on is left here as a reminder how to share memory between modules
        #            we would of course instead just use var = [False]
        transmit_on: bytearray # flag for half-duplex communication
        transmit_on_ref: bytearray # reference to flag for common use in audioTransmitter and audioReceiver
        # NOTE: receive_on_ref and receive_on is left here as a reminder how to share memory between modules
        #            we would of course instead just use var = [False]
        receive_on: bytearray # flag for half-duplex communication
        receive_on_ref: bytearray # reference to flag for common use in audioTransmitter and audioReceiver
        ack_received: list # flag to informa about reception of ACK telegram
        send_ack: list # trigger to send and ACK, acknowledging seqNrAck
        seqNrAck: list # seqNr to be Acknowledged - reference to seqNr AKCnowledged by transmitter
        seqNrAckRx: list # seqNr Acknowledged by the other side - reference to seqNr received from transmitter
        seqNrTx: list # seqNr TX
        private_key: list
        cipher: list
        comm_token: list
    globVars = GlobVars(
        [False], # stream_on
        bytearray([False]), # transmit_on
        None, # transmit_on_ref
        bytearray([False]), # receive_on
        None, # receive_on_ref
        [False, 0], # ack_received
        [False], # send_ack
        [0], # seqNrAck
        [0], # seqNrAckRx
        [0], # seqNrTx
        [None], # private_key
        [None], # cipher
        [0]) # comm_token
    globVars.transmit_on_ref = memoryview(globVars.transmit_on)
    globVars.receive_on_ref = memoryview(globVars.receive_on)
    # variable containing global variables shall be itself mutable, so:
    glob_vars = [globVars]
        
    # class objects
    audioTransmitter = None # AudioTransmitter(..)
    audioReceiver = None # AudioReceiver(..)
    # error message
    errorMessage = ""
    # default device
    default_device = sd.default.device
    print("default audio input = "+str(default_device[0]))
    print("default audio output = "+str(default_device[1]))
    # CALL timeout handling
    # at high-level we do communicate half-duplex but at a lower level it may be that we are 
    # transmitting while receiving. Sometimes telegrams will just cross.
    # this is no problem because we have different lines for TX and for RX, unless we have "collissions", or in fact "disturbances due e.g. to EM-cross-talk"
    # For such cases we need to retransmit with a random timeout so we dont collide infinitely.
    TX_RETRANSMISSION_POLL_PERIODS = 0
    # audio devices
    audio_devices = sd.query_devices() # returns DeviceList
    ad_index_by_name = {}
    for i in range(len(audio_devices)):
        # WORKAROUND: otherwise we find no strings because last space is removed..
        #############################################
        if audio_devices[i]["name"][-1] == " ":
            audio_devices[i]["name"] = audio_devices[i]["name"][:-1]
        #############################################
        # NOTE: devices may be detected several times e.g. on different USB-Ports but still have the same name.
        #            We add the audio_devices index as a prefix in order to be able to distinguish from one another. 
        ad_index_by_name[str(i)+": "+audio_devices[i]["name"]] = i
        print(str(i)+": "+audio_devices[i]["name"])
    # threads
    wire_in = None
    play_out = None
    wire_out = None
    rx_in = None
    ###decode = None
    # plot variables
    plotdata = None
    lines = None
    
    def __init__(self,  plotdata_arg,  lines_arg):
        # initialize plot variables (received from "main loop")
        self.plotdata = plotdata_arg
        self.lines = lines_arg
        # CALL timeout handling
        self.TX_RETRANSMISSION_POLL_PERIODS = audioSettings.TX_RETRANSMISSION_POLL_PERIODS_SHORT
        ### self.randomRetryTimeout()
    
    def thread_wire_in(self, name):
        logging.info("enter thread_wire_in..")
        # check settings
        ##########
        try:
            sd.check_input_settings(device=self.ad_index_by_name[configuration.AUDIO_DEVICE_RX_IN], channels=1, dtype='float32', samplerate=audioSettings.SAMPLING_FREQUENCY)
        except Exception as e:
            msg = type(e).__name__ + ': ' + str(e)
            logging.error(msg)
            self.errorMessage += "Exception in soundDeviceManager.thread_wire_in: RX-IN Settings not supported!\n"
            exit(msg)
            return
        # TODO: why is this check failing???
        '''
        try:
            sd.check_input_settings(device=self.ad_index_by_name[configuration.AUDIO_DEVICE_RX_OUT], channels=1, dtype='float32', samplerate=audioSettings.SAMPLING_FREQUENCY)
            # sd.check_input_settings(device=self.ad_index_by_name[configuration.AUDIO_DEVICE_RX_OUT], channels=2, dtype='float32', samplerate=audioSettings.SAMPLING_FREQUENCY)
        except Exception as e:
            msg = type(e).__name__ + ': ' + str(e)
            print(msg)
            self.errorMessage += "Exception in soundDeviceManager.thread_wire_in: RX-OUT Settings not supported!\n"
            exit(msg)
            return
        # '''
        # create and open stream
        ##############
        try:
            # TODO: check samplerate and other things here or somewhere else
            # stream RX audio (= voice + code) to output device (and "with it" plot)
            with sd.Stream(device=(self.ad_index_by_name[configuration.AUDIO_DEVICE_RX_IN], self.ad_index_by_name[configuration.AUDIO_DEVICE_RX_OUT]), 
                           samplerate=audioSettings.SAMPLING_FREQUENCY, blocksize=audioSettings.AUDIO_RX_CHUNK_SAMPLES_LEN,
                           # dtype=args.dtype, latency=args.latency,
                           channels=1,  # fix = 1 (mono)
                           callback=self.audioReceiver.callback_wire_in):
                # wire_in loop:
                #########
                while(self.glob_vars[0].stream_on[0]):
                    time.sleep(THREAD_SLEEP_TIME_SEC)
            print("leave thread_wire_in..")
        except Exception as e:
            msg = type(e).__name__ + ': ' + str(e)
            logging.error(msg)
            self.errorMessage += "Exception in soundDeviceManager.thread_wire_in: Problems with RX in / RX out devices!\n"
            exit(msg)
        
    def thread_rx_in(self, name):
        logging.info("enter thread_rx_in..")
        # check settings
        ##########
        try:
            sd.check_input_settings(device=self.ad_index_by_name[configuration.AUDIO_DEVICE_RX_IN], channels=1, dtype='float32', samplerate=audioSettings.SAMPLING_FREQUENCY)
        except Exception as e:
            msg = type(e).__name__ + ': ' + str(e)
            logging.error(msg)
            self.errorMessage += "Exception in soundDeviceManager.thread_rx_in: RX-IN Settings not supported!\n"
            exit(msg)
            return
        # create and open stream
        ##############
        try:
            # TODO: check samplerate and other things here or somewhere else
            # stream RX audio (= voice + code) from RX in (and "with it" plot)
            logging.info("try to open stream for rx_in: "+str(self.ad_index_by_name[configuration.AUDIO_DEVICE_RX_IN]))
            with sd.InputStream(device=self.ad_index_by_name[configuration.AUDIO_DEVICE_RX_IN], 
                    # dtype=args.dtype, latency=args.latency,
                    channels=1,  # fix = 1 (mono)
                    callback=self.audioReceiver.callback_rx_in,
                    samplerate=audioSettings.SAMPLING_FREQUENCY, blocksize=audioSettings.AUDIO_RX_CHUNK_SAMPLES_LEN):     
                logging.info("opened stream for rx_in: "+str(self.ad_index_by_name[configuration.AUDIO_DEVICE_RX_IN]))
                # rx_in loop:
                ########
                while(self.glob_vars[0].stream_on[0]):
                    time.sleep(THREAD_SLEEP_TIME_SEC)
            logging.info("leave thread_rx_in..")
        except Exception as e:
            logging.error("EXCEPTION on device = "+str(self.audio_devices[self.ad_index_by_name[configuration.AUDIO_DEVICE_RX_IN]]))
            msg = type(e).__name__ + ': ' + str(e)
            logging.error(msg)
            self.errorMessage += "Exception in soundDeviceManager.thread_rx_in(): Problems with RX in device!\n"
            exit(msg)
        
    def thread_wire_out(self, name):
        logging.info("enter thread_wire_out..")
        # check settings
        ##########
        try:
            sd.check_input_settings(device=self.ad_index_by_name[configuration.AUDIO_DEVICE_TX_IN], channels=1, dtype='float32', samplerate=audioSettings.SAMPLING_FREQUENCY)
        except Exception as e:
            msg = type(e).__name__ + ': ' + str(e)
            logging.error(msg)
            self.errorMessage += "Exception in soundDeviceManager.thread_wire_out: TX-IN Settings not supported!\n"
            exit(msg)
            return
        # TODO: why is this check failing???
        '''
        try:
            sd.check_input_settings(device=self.ad_index_by_name[configuration.AUDIO_DEVICE_TX_OUT], channels=1, dtype='float32', samplerate=audioSettings.SAMPLING_FREQUENCY)
            # sd.check_input_settings(device=self.ad_index_by_name[configuration.AUDIO_DEVICE_TX_OUT], dtype='float32', samplerate=audioSettings.SAMPLING_FREQUENCY)
        except Exception as e:
            msg = type(e).__name__ + ': ' + str(e)
            print(msg)
            self.errorMessage += "Exception in soundDeviceManager.thread_wire_out: TX-OUT Settings not supported!\n"
            exit(msg)
            return
        # '''
        # create and open stream
        ##############
        try:
            # TODO: check samplerate and other things here or somewhere else
            # stream TX audio (=voice + code) to output device
            with sd.Stream(device=(self.ad_index_by_name[configuration.AUDIO_DEVICE_TX_IN], self.ad_index_by_name[configuration.AUDIO_DEVICE_TX_OUT]), 
                           samplerate=audioSettings.SAMPLING_FREQUENCY, blocksize=audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN,
                           # dtype=args.dtype, latency=args.latency,
                           channels=1,  # fix = 1 (mono)
                           callback=self.audioTransmitter.callback_wire_out):
                # wire_out loop:
                ##########
                while(self.glob_vars[0].stream_on[0]):
                    time.sleep(THREAD_SLEEP_TIME_SEC)
            logging.info("leave thread_wire_out..")
        except Exception as e:
            msg = type(e).__name__ + ': ' + str(e)
            logging.error(msg)
            self.errorMessage += "Exception in soundDeviceManager.thread_wire_out(): Problems with TX in / TX out devices!\n"
            exit(type(e).__name__ + ': ' + str(e))

    def thread_play_out(self, name):
        logging.info("enter thread_play_out..")
        # check settings
        ##########
        # TODO: why is this check failing?
        '''
        try:
            sd.check_input_settings(device=self.ad_index_by_name[configuration.AUDIO_DEVICE_TX_OUT], channels=1, dtype='float32', samplerate=audioSettings.SAMPLING_FREQUENCY)
        except Exception as e:
            msg = type(e).__name__ + ': ' + str(e)
            print(msg)
            self.errorMessage += "Exception in soundDeviceManager.thread_play_out: TX-OUT Settings not supported!\n"
            exit(msg)
            return
        # '''
        # create and open stream
        ##############
        try:
            # TODO: check samplerate and other things here or somewhere else
            # stream TX audio (=code) to output device
            with sd.OutputStream(device=self.ad_index_by_name[configuration.AUDIO_DEVICE_TX_OUT],
                    channels=1,  # fix = 1 (mono)
                    callback= self.audioTransmitter.callback_play,
                    samplerate=audioSettings.SAMPLING_FREQUENCY, blocksize=audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN):
                logging.info("opened output stream for play_out: "+str(self.ad_index_by_name[configuration.AUDIO_DEVICE_TX_OUT]))
                while (self.glob_vars[0].stream_on[0]):
                    time.sleep(THREAD_SLEEP_TIME_SEC)
            logging.info("leave thread_play_out..")
        except Exception as e:
            msg = type(e).__name__ + ': ' + str(e)
            logging.error(msg)
            self.errorMessage += "Exception in soundDeviceManager.thread_play_out(): Problems with TX out device!\n"
            exit(msg)

    def startDevices(self):
        self.errorMessage = ""
        self.glob_vars[0].stream_on[0] = True
        # reset shared communication variables
        self.glob_vars[0].ack_received[0] = False
        self.glob_vars[0].send_ack[0] = False
        self.glob_vars[0].seqNrAck[0] = 0
        self.glob_vars[0].seqNrAckRx[0] = 0
        self.glob_vars[0].seqNrTx[0] = 0
        # class objects
        # TODO: pass all shared variables in a STRUCT
        ############################
        self.audioTransmitter = AudioTransmitter(self.glob_vars)
        self.audioReceiver = AudioReceiver(self.glob_vars)
        # audio input thread
        ############
        if configuration.AUDIO_DEVICE_RX_IN != "none":
            if configuration.AUDIO_DEVICE_RX_OUT != "none":
                self.wire_in = threading.Thread(target=self.thread_wire_in, args=(1, ))
                self.wire_in.start()
            else:
                self.rx_in = threading.Thread(target=self.thread_rx_in, args=(1, ))
                self.rx_in.start()
        else:
            return "Error: please select a device for RX in!"
        # TODO: if we notice that we don't need to wait here then remove this line
        #            if we need it see if we can reduce it from 1sec to 1/2sec
        # 2021.01.06: still see crashes -> uncommented, set to THREAD_SLEEP_TIME_SEC
        # 2020.10.28: start of evaluation period -> commented
        time.sleep(THREAD_SLEEP_TIME_SEC)
        # audio output thread
        #############
        if configuration.AUDIO_DEVICE_TX_OUT != "none":
            if configuration.AUDIO_DEVICE_TX_IN != "none":
                self.wire_out = threading.Thread(target=self.thread_wire_out, args=(1,))
                self.wire_out.start()
            else:
                self.play_out = threading.Thread(target=self.thread_play_out, args=(1,))
                self.play_out.start() 
        else:
            return "Error: please select a device for TX out!"
        # message send thread
        # need to decouple: GUI -> thread_send_message -> callback_play/ callback_wire_out
        ################################################
        send_message = threading.Thread(target=self.audioTransmitter.thread_send_message,  args=(1,))
        send_message.start()
        logging.info("leave startDevices..")
        # wait for all threads to start..so we can collect error messages..
        # TODO: use threads start completion flag(s) instead
        time.sleep(THREAD_SLEEP_TIME_SEC//2)
        return self.errorMessage
        
    def stopDevices(self):
        self.glob_vars[0].stream_on[0] = False
        
    # BLOCKING call 
    def sendMessage(self, message):
        self.audioTransmitter.sendMessage(message)
        
    def isPlotRxQueueEmpty(self):
        return self.audioReceiver.qplot.empty()
        
    def plotRxQueueGetNoWait(self):
        return self.audioReceiver.qplot.get_nowait()

    def messageRxQueueGet(self):
        ret = None
        if self.audioReceiver.inMessageQueue.empty() == False:
            # BLOCKING call
            ret = self.audioReceiver.inMessageQueue.get()
        return ret
        
    def statusRxQueueGet(self):
        ret = None
        if self.audioReceiver is not None:
            if self.audioReceiver.inCommStatusQueue.empty() == False:
                # BLOCKING call
                ret = self.audioReceiver.inCommStatusQueue.get()
        return ret
        
    def statusTxQueueGet(self):
        ret = None
        if self.audioTransmitter is not None:
            if self.audioTransmitter.outCommStatusQueue.empty() == False:
                # BLOCKING call
                ret = self.audioTransmitter.outCommStatusQueue.get()
        return ret
        
    def getRxTimeMs(self):
        return self.audioReceiver.getRxTimeMs()
        
    def getRoundtripTimeMs(self):
        return self.audioTransmitter.getRoundtripTimeMs()

    def getAvgTxTimeMs(self):
        return self.audioTransmitter.getAvgTxTimeMs()
        
    def getTelegramCircularBufferSize(self):
        return self.audioTransmitter.getTelegramCircularBufferSize()
        
    def getAvgInAmplitudePercent(self):
        return self.audioReceiver.getAvgInAmplitudePercent()
        
    def isCall(self):
        return self.audioReceiver.isCall()
        
    def isCallEnd(self):
        ret = False
        if self.audioReceiver is not None:
            ret = self.audioReceiver.isCallEnd()
        return ret
        
    def call_accept(self):
        self.audioTransmitter.call_accept()
        
    def call_reject(self):
        self.audioTransmitter.call_reject()
        
    def call_end(self):
        self.audioTransmitter.call_end()
        
    def pollErrorMessages(self):
        # TODO: poll also receiver and concatenate error messages?
        return self.audioTransmitter.pollErrorMessage()
        
    # In average, half of the time we retry after "double" the necessary time in order to give the other side a chance
    # to successfully transmit in case we are having collissions due to simultaneous transmissions from both sides.
    # Half of the time we retry after an even longer time given by TX_RETRANSMISSION_POLL_PERIODS_LONG.
    # NOTE: for TX and RX we have different physical channels, so it is NOT exactly COLLISSIONS what we have
    #            but probably interferences e.g. due to "cross-talk" (EM coupling between lines).
    def randomRetryTimeout(self):
        rnd = np.random.randint(2)
        if rnd:
            self.TX_RETRANSMISSION_POLL_PERIODS = audioSettings.TX_RETRANSMISSION_POLL_PERIODS_SHORT
        else:
            self.TX_RETRANSMISSION_POLL_PERIODS = audioSettings.TX_RETRANSMISSION_POLL_PERIODS_LONG
        
    def call_once(self):
        poll_counter = 0
        # random timeout to avoid collissions when simultaneous CALL from both sides
        self.randomRetryTimeout()
        # CALL once
        self.audioTransmitter.call_once()
        # process CALL answer
        # NOTE: this method will be called again immediately after returning...
        #            we may overload communication if we send too many CALLs, so we add a delay factor, e.g. of 3.
        while poll_counter < self.TX_RETRANSMISSION_POLL_PERIODS*3:
            if self.audioReceiver.isCallAccepted():
                return audioSettings.COMMAND_CALL_ACCEPTED
            elif self.audioReceiver.isCallRejected():
                return audioSettings.COMMAND_CALL_REJECTED
            # polling sleep pause
            time.sleep(audioSettings.TX_POLL_PERIOD_SEC)
            poll_counter += 1
        return audioSettings.COMMAND_ERROR
        
    def send_key_start_once(self):
        poll_counter = 0
        # random timeout to avoid collissions - TODO: check this?
        self.randomRetryTimeout()
        # send KEY START
        self.audioTransmitter.send_key_start_once()
        # process send_key_start() answer
        while poll_counter < self.TX_RETRANSMISSION_POLL_PERIODS:
            if self.audioReceiver.isKeyStartReceived():
                return audioSettings.COMMAND_KEY_START
            elif self.audioReceiver.isCallRejected():
                return audioSettings.COMMAND_CALL_REJECTED
            # polling sleep pause
            time.sleep(audioSettings.TX_POLL_PERIOD_SEC)
            poll_counter += 1
        return audioSettings.COMMAND_ERROR
        
    def send_key_end_once(self):
        poll_counter = 0
        # random timeout to avoid collissions - TODO: check this?
        self.randomRetryTimeout()
        # send KEY END
        self.audioTransmitter.send_key_end_once()
        # process send_key_end() answer
        while poll_counter < self.TX_RETRANSMISSION_POLL_PERIODS:
            if self.audioReceiver.isKeyEndReceived():
                return audioSettings.COMMAND_KEY_END
            elif self.audioReceiver.isCallRejected():
                return audioSettings.COMMAND_CALL_REJECTED
            # polling sleep pause
            time.sleep(audioSettings.TX_POLL_PERIOD_SEC)
            poll_counter += 1
        return audioSettings.COMMAND_ERROR
        
    def respond_key_start_once(self):
        poll_counter = 0
        # random timeout to avoid collissions - TODO: check this?
        self.randomRetryTimeout()
        # send KEY START as soon as we receive KEY START from the other side
        # check response
        while poll_counter < self.TX_RETRANSMISSION_POLL_PERIODS:
            if self.audioReceiver.isKeyStartReceived():
                self.audioTransmitter.send_key_start_once()
                return audioSettings.COMMAND_KEY_START
            elif self.audioReceiver.isCallRejected():
                return audioSettings.COMMAND_CALL_REJECTED
            # polling sleep pause
            time.sleep(audioSettings.TX_POLL_PERIOD_SEC)
            poll_counter += 1
        return audioSettings.COMMAND_ERROR
        
    def respond_key_end_once(self):
        poll_counter = 0
        # random timeout to avoid collissions - TODO: check this?
        self.randomRetryTimeout()
        # send KEY END as soon as we receive KEY END from the other side
        while poll_counter < self.TX_RETRANSMISSION_POLL_PERIODS:
            if self.audioReceiver.isKeyEndReceived():
                self.audioTransmitter.send_key_end_once() 
                return audioSettings.COMMAND_KEY_END
            elif self.audioReceiver.isCallRejected():
                return audioSettings.COMMAND_CALL_REJECTED
            # polling sleep pause
            time.sleep(audioSettings.TX_POLL_PERIOD_SEC)
            poll_counter += 1
        return audioSettings.COMMAND_ERROR
        
    def generatePublicKey(self):
        self.audioTransmitter.generatePublicKey()
        
    def send_startup_data_once(self, my_name):
        poll_counter = 0
        # random timeout to avoid collissions - TODO: check this?
        self.randomRetryTimeout()
        # send STARTUP DATA
        self.audioTransmitter.send_startup_data_once(my_name)
        # process send_startup_data() answer
        while poll_counter < self.TX_RETRANSMISSION_POLL_PERIODS:
            if self.audioReceiver.isStartupDataReceived():
                return audioSettings.COMMAND_STARTUP_DATA
            elif self.audioReceiver.isCallRejected():
                return audioSettings.COMMAND_CALL_REJECTED
            # polling sleep pause
            time.sleep(audioSettings.TX_POLL_PERIOD_SEC)
            poll_counter += 1
        return audioSettings.COMMAND_ERROR
        
    def respond_startup_data_once(self, my_name):
        poll_counter = 0
        # random timeout to avoid collissions - TODO: check this?
        self.randomRetryTimeout()
        # send STARTUP_COMPLETE as soon as we receive STARTUP from the other side
        while poll_counter < self.TX_RETRANSMISSION_POLL_PERIODS:
            if self.audioReceiver.isStartupDataReceived():
                # NOTE: we dont call send_startup_data() which is handled differently with ACK
                #            this telegram will be retransmitted automatically "and non-blocking" up to max. nr. of retransmissions
                self.audioTransmitter.send_startup_data_complete(my_name) 
                return audioSettings.COMMAND_STARTUP_DATA
            elif self.audioReceiver.isCallRejected():
                return audioSettings.COMMAND_CALL_REJECTED
            # polling sleep pause
            time.sleep(audioSettings.TX_POLL_PERIOD_SEC)
            poll_counter += 1
        return audioSettings.COMMAND_ERROR
        
    def isTxStateWaitAck(self):
        return self.audioTransmitter.isTxStateWaitAck()
        
    def getTelTxOk(self):
        return self.audioTransmitter.getTelTxOk()
        
    def getTelTxNok(self):
        return self.audioTransmitter.getTelTxNok()
        
    def getTelRxOk(self):
        return self.audioReceiver.getTelRxOk()
        
    def getTelRxNok(self):
        return self.audioReceiver.getTelRxNok()
        
    def getSessionCode(self):
        return self.audioReceiver.getSessionCode()
        
    def getStartupData(self):
        return self.audioReceiver.getStartupData()
        
    def haveToken(self):
        return self.audioReceiver.haveToken()
        
    def purge(self):
        self.audioTransmitter.purge()
        self.audioReceiver.purge()
        
        
   
        
        


        


