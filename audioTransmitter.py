# -*- coding: UTF-8 -*-

import audioSettings
import queue
import numpy as np
import bitarray
from scipy import signal
import math
import configuration
import time
import threading
from timeit import default_timer as cProfileTimer
import logging
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives import padding

''''
This module implements the left side of this drawing:
    
wire connections:

    (TX in)	Voice -> BAND_STOP ->  (+) ->  (TX out)    ->  Channel ->    (RX in) .-> BAND_STOP -> Voice (RX out)
                                                   ^                                                        |
                                                   |                                                         |
                Code -> BAND_PASS -----                                                           -> BAND_PASS -> Code

FFTs:
                ___   ___                       ________                     ________                 ___   ___
                    |_|         ->  (+)  ->        | |          ... ->             | |       ---- >          |_|
                                        ^                                                          |
                     __                |                                                           |                   __
               ___|  |___    -----                                                             -->         ___|  |___
'''


##############################################
# NOTE: about transition bands in Filters
# Filters with sharp frequency cutoffs can produce outputs that ring for a long
# time when they operate on signals with frequency content in the transition band.
# In general, therefore, the wider a transition band that can be tolerated,
# the better behaved the filter will be in the time domain.
##############################################
# settings "band pass filter" to restrict CODE to coding frequency range
BPF_LEFT_MARGIN = 400
BPF_RIGHT_MARGIN = 400
BPF_ORDER = 7
BPF_MAX_RIPPLE = 0.1
BPF_ELL_MIN_ATTENUATION = 145.0
# settings "band stop filter" to not interfere coding frequency range with TX in
BSF_LEFT_MARGIN = 400 # 600
BSF_RIGHT_MARGIN = 400 #600 
BSF_ORDER = 7
BSF_MAX_RIPPLE = 0.1
# settings "elliptic filter"
BSF_ELL_MIN_ATTENUATION = 145.0
# NOTCH filter
# TODO: investiage why 920Hz can be suppressed so well but other frequencies not..
f0 = 920.0 # Modulating frequency to be removed from distorted signal (Hz)
Q = 3.0  # Quality factor
# definitions for transmission state
IDLE = 0
WAIT_ACK = 1


class AudioTransmitter: 
    # ACK timeout handling
    # at high-level we do communicate half-duplex but at a lower level it may be that we are 
    # transmitting while receiving. Sometimes telegrams will just cross.
    # this is no problem because we have different lines for TX and for RX, unless we have "collissions", or in fact "disturbances due e.g. to EM-cross-talk"
    # For such cases we need to retransmit with a random timeout so we dont collide infinitely.
    TX_RETRANSMISSION_POLL_PERIODS = 0
    # protocol
    seqNrTx = [0] # reference to sequence number TX
    seqNrAck = [0] # reference to sequence number for ACK
    seqNrAckRx = [0] # reference to sequence number for ACK RX
    # key
    private_key = [None]
    cipher = [None]
    key_start = None # helper variable to pass 1st part of our public key
    key_end = None # helper variable to pass 2nd part of our public key
    # state transmission
    tx_state = IDLE
    # error message
    errorMessage = ""
    # constant definitions which depend on configuration settings
    # constants for filters
    BPF_F1 = audioSettings.CODE_SINE_FREQUENCY_ONE - BPF_LEFT_MARGIN
    BPF_F2 = audioSettings.CODE_SINE_FREQUENCY_ZERO + BPF_RIGHT_MARGIN
    BSF_F1 = audioSettings.CODE_SINE_FREQUENCY_ONE - BSF_LEFT_MARGIN
    BSF_F2 = audioSettings.CODE_SINE_FREQUENCY_ZERO + BSF_RIGHT_MARGIN
    # lock to manage concurrency between audio callback and transmission thread
    messageLock = threading.Lock()
    # variables to implement a circular buffer
    telegramNrWrite = 0
    telegramNrRead = 0
    chunkNrWrite = 0
    chunkNrRead = 0
    telegramNrReadSize = None
    #################################################
    # NOTE: about audioChunkRef[]
    # old-fashioned-straight-forward-way using an indexed numpy ndarray is BETTER than
    # other approaches like memoryview or objects supporting buffer protocol because:
    # - also fast or even faster
    # - easier to understand (KISS)
    # - long-living solution -> other approaches change frequently!
    # - can be protected against concurrency by using a lock on one index and a size variable
    # - note that audioChunkRef itself does not need to be locked because we always access 
    #   a different part of it, so concurrency is solved with indexes.
    audioChunkRef = None
    #################################################
    # TODO: better module variable?
    noAudioInput = None
    # soften borders of telegram with Gauss-/Normal- shape
    gauss = None
    # flags
    stream_on = [False]
    transmit_on_ref = None # reference to flag for half-duplex communication
    ack_received = [False, 0] # reference to flag for ACK received
    send_ack = [False] # reference to flag for send ACK
    reject_call = False
    end_call = False
    comm_token = [0]
    # queues
    outTextMessageQueue = queue.Queue()
    outCommStatusQueue = queue.Queue()
    # time
    avg_tx_time_ms = 0.0
    time_old = 0.0
    avg_roundtrip_time_ms = 0.0
    time_roundtrip_old = 0.0
    # communication statistics
    telTxOk = 0
    telTxNok = 0
    # filter BAND-PASS
    sos_bandpass = None # signal.ellip(BPF_ORDER, BPF_MAX_RIPPLE, BPF_ELL_MIN_ATTENUATION,
                            # IMPORTANT: we need to divice by Nyquist frequency or pass fs as argument...one thing or the other..
                            # [BPF_F1 / audioSettings.NYQUIST_FREQUENCY, BPF_F2 / audioSettings.NYQUIST_FREQUENCY],'bandpass', analog=False, output='sos')
                            ### [BPF_F1, BPF_F2],'bandpass', analog=False, fs=audioSettings.SAMPLING_FREQUENCY, output='sos')
    # IMPORTANT: we need this TRICK to filter audio signal "in chunks":
    z = None # np.zeros((sos_bandpass.shape[0], 2))
    # filter BAND-STOP
    sos_bandstop = None # signal.ellip(BSF_ORDER, BSF_MAX_RIPPLE, BSF_ELL_MIN_ATTENUATION,
                            # IMPORTANT: we need to divice by Nyquist frequency or pass fs as argument...one thing or the other..
                            ##########################################################
                            # [BPF_F1 / audioSettings.NYQUIST_FREQUENCY, BPF_F2 / audioSettings.NYQUIST_FREQUENCY],'bandpass', analog=False, output='sos')
                            ### [BSF_F1, BSF_F2],'bandstop', analog=False, fs=audioSettings.SAMPLING_FREQUENCY, output='sos')
    # IMPORTANT: we need this TRICK to filter audio signal "in chunks":
    zBandStop = None # np.zeros((sos_bandstop.shape[0], 2))
    # NOTCH filter
    ### b, a = signal.iirnotch(f0, Q, fs=audioSettings.SAMPLING_FREQUENCY) # ,  output='sos') # cannot return sos for some reason..
    # IMPORTANT: we need this TRICK to filter audio signal "in chunks":
    ### Z, P, K = signal.tf2zpk(b, a)
    sos_notch = None # signal.zpk2sos(Z, P, K)
    zNotch = None # np.zeros((sos_notch.shape[0], 2))
 
    def __init__(self, glob_vars): # (self,  stream_on, ack_received, send_ack, seqNrAck, seqNrAckRx, seqNrTx, private_key, cipher, transmit_on_ref, receive_on_ref):      
        self.stream_on = glob_vars[0].stream_on
        self.ack_received = glob_vars[0].ack_received
        self.send_ack = glob_vars[0].send_ack
        self.seqNrAck = glob_vars[0].seqNrAck
        self.seqNrAckRx = glob_vars[0].seqNrAckRx
        self.seqNrTx = glob_vars[0].seqNrTx
        self.private_key = glob_vars[0].private_key
        self.cipher = glob_vars[0].cipher
        self.transmit_on_ref = glob_vars[0].transmit_on_ref
        self.receive_on_ref = glob_vars[0].receive_on_ref
        self.comm_token = glob_vars[0].comm_token
        self.telTxOk = 0
        self.telTxNok = 0
        # size
        self.telegramNrReadSize = [0]*audioSettings.MAX_NR_OF_TELEGRAMS_IN_PARALLEL
        # ACK timeout handling
        self.TX_RETRANSMISSION_POLL_PERIODS = audioSettings.TX_RETRANSMISSION_POLL_PERIODS_SHORT
        logging.info("Initializing audioTransmitter")
        logging.info("TX_RETRANSMISSION_POLL_PERIODS = "+str(self.TX_RETRANSMISSION_POLL_PERIODS))
        logging.info("Allocating memory...")
        #################################################
        ###print("Allocating memory...")
        # pre-allocate buffer (not allocating memory during runtime icreases performance!)
        # this buffer allocates all samples of up to MAX_NR_OF_TELEGRAMS_IN_PARALLEL telegrams with each having up to MAX_NR_OF_CHUNKS_PER_TELEGRAM chunks
        # under normal conditions we will transmit less than max.telegrams each having a different number of chunks generally smaller than max.chunks
        # TODO: some day try instead using a queue as in audioReceiver, it may work better or reduce the complexity of the code.
        self.audioChunkRef = np.array([[0.0]]*audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN*audioSettings.MAX_NR_OF_CHUNKS_PER_TELEGRAM*audioSettings.MAX_NR_OF_TELEGRAMS_IN_PARALLEL)
        logging.info("Memory allocation finished.")
        ###print("Memory allocation finished.")
        #################################################
        # TODO: better module variable?
        self.noAudioInput = np.array([0.0]*audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN)
        # soften borders of telegram with Gauss-/Normal- shape
        self.gauss = signal.gaussian(audioSettings.LEN_BORDER*1, std=7*audioSettings.LEN_BIT_ZERO)
        # filter BAND-PASS
        self.sos_bandpass = signal.ellip(BPF_ORDER, BPF_MAX_RIPPLE, BPF_ELL_MIN_ATTENUATION,
                                # IMPORTANT: we need to divice by Nyquist frequency or pass fs as argument...one thing or the other..
                                # [BPF_F1 / audioSettings.NYQUIST_FREQUENCY, BPF_F2 / audioSettings.NYQUIST_FREQUENCY],'bandpass', analog=False, output='sos')
                                [self.BPF_F1, self.BPF_F2],'bandpass', analog=False, fs=audioSettings.SAMPLING_FREQUENCY, output='sos')
        # IMPORTANT: we need this TRICK to filter audio signal "in chunks":
        self.z = np.zeros((self.sos_bandpass.shape[0], 2))
        # filter BAND-STOP
        self.sos_bandstop = signal.ellip(BSF_ORDER, BSF_MAX_RIPPLE, BSF_ELL_MIN_ATTENUATION,
                                # IMPORTANT: we need to divice by Nyquist frequency or pass fs as argument...one thing or the other..
                                ##########################################################
                                # [BPF_F1 / audioSettings.NYQUIST_FREQUENCY, BPF_F2 / audioSettings.NYQUIST_FREQUENCY],'bandpass', analog=False, output='sos')
                                [self.BSF_F1, self.BSF_F2],'bandstop', analog=False, fs=audioSettings.SAMPLING_FREQUENCY, output='sos')
        # IMPORTANT: we need this TRICK to filter audio signal "in chunks":
        self.zBandStop = np.zeros((self.sos_bandstop.shape[0], 2))
        # NOTCH filter
        b, a = signal.iirnotch(f0, Q, fs=audioSettings.SAMPLING_FREQUENCY) # ,  output='sos') # cannot return sos for some reason..
        # IMPORTANT: we need this TRICK to filter audio signal "in chunks":
        Z, P, K = signal.tf2zpk(b, a)
        self.sos_notch = signal.zpk2sos(Z, P, K)
        self.zNotch = np.zeros((self.sos_notch.shape[0], 2))
        # status
        self.outCommStatusQueue.put("") # ("TX:")
    
    def callback_play(self, outdata, frames, time, status):
        # store time
        self.avg_tx_time_ms = (float(time.currentTime) - self.time_old)*1000.0
        self.time_old = float(time.currentTime)
        if status:
            logging.error("play status : "+str(status))
        # half-duplex communication
        ################
        # NOTE: hard condition may interrupt TX in the middle of a telegram
        #            and produce an error when continuing after reception
        # we could instead wait until a complete telegram is transmitted but for now
        # we just don't do that...the highest priority is RX (we are considerate!)
        # and this way we allow a re-transmission from the other side without
        # blocking it probably with a long telegram 
        ### if self.receive_on_ref[0] == False:
        # put message to TX out:
        ##############
        try:
            readMessage = False # to help reduce blocking time
            readIndexTemp = 0
            # BLOCKING code block...but acceptable because it blocks only very seldom and very shortly
            ####################################################
            with self.messageLock:
                # NEW chunk ?
                if (self.telegramNrRead != self.telegramNrWrite):
                    readIndexTemp = self.telegramNrRead*audioSettings.MAX_NR_OF_CHUNKS_PER_TELEGRAM+self.chunkNrRead*audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN
                    readMessage = True
                    # set half-duplex flag
                    if self.transmit_on_ref[0]  == False:
                        self.transmit_on_ref[0] = True
                        logging.info("TX ON")
                else:
                    # reset half-duplex flag
                    if self.transmit_on_ref[0]:
                        self.transmit_on_ref[0] = False
                        logging.info("TX OFF")
            # this part is not blocking any more
            ####################################################
            if readMessage == True:
                # write chunk
                outdata[:audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN] = self.audioChunkRef[readIndexTemp:readIndexTemp+audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN]
                # increment chunk number
                self.chunkNrRead = (self.chunkNrRead + 1)%self.telegramNrReadSize[self.telegramNrRead]
                # increment telegram counter
                if self.chunkNrRead == 0:
                    self.telegramNrRead = (self.telegramNrRead + 1)%audioSettings.MAX_NR_OF_TELEGRAMS_IN_PARALLEL
            else:
                outdata[:frames] = audioSettings.SILENCE
            # add carrier
            ########
            if audioSettings.ADD_CARRIER:
                # reduce amplitude of voice so we dont saturate output when adding carrier
                outdata[:audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN] = (1.0 - audioSettings.CARRIER_AMPLITUDE)*outdata[:audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN]
                # add carrier
                outdata[:audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN] += audioSettings.CARRIER
        except Exception as e:
            logging.error("Exception in AudioTransmitter.callback_play():"+str(e)+"\n")
             
    ##############################################################
    # TODO: use or implement something better..
    currSample = 0
    def distortFunction(self,  x):
        ret = x*math.sin(2 * np.pi *f0*self.currSample/audioSettings.SAMPLING_FREQUENCY)*2
        self.currSample += 1
        return ret
    distort = np.vectorize(distortFunction)
    ##############################################################
            
    def callback_wire_out(self,  indata, outdata, frames, time, status):
        # store time
        self.avg_tx_time_ms = (float(time.currentTime) - self.time_old)*1000.0
        self.time_old = float(time.currentTime)
        if status:
            logging.error("wire_out status: "+str(status))
        # wire in - out (Voice only for now)
        ####################
        if configuration.TRANSMIT_IN_TX_VOICE:
            # distort?
            if configuration.IN_TX_DISTORT:
                self.currSample = 0
                # DISTORT voice
                outdata[:frames, audioSettings.DEFAULT_CHANNEL] = self.distort(self, indata[:frames, audioSettings.DEFAULT_CHANNEL])
                # remove modulating frequency
                outdata[:, audioSettings.DEFAULT_CHANNEL], self.zNotch = signal.sosfilt(self.sos_notch, outdata[:, audioSettings.DEFAULT_CHANNEL], zi=self.zNotch)
                # deplete coding frequency range to not interfere with code
                outdata[:, audioSettings.DEFAULT_CHANNEL], self.zBandStop = signal.sosfilt(self.sos_bandstop, outdata[:, audioSettings.DEFAULT_CHANNEL], zi=self.zBandStop)
            else:
                # deplete coding frequency range to not interfere with code
                outdata[:, audioSettings.DEFAULT_CHANNEL], self.zBandStop = signal.sosfilt(self.sos_bandstop, indata[:, audioSettings.DEFAULT_CHANNEL], zi=self.zBandStop)
            # TEST: call bandstop a 2nd time..
            ###################
            #outdata[:, audioSettings.DEFAULT_CHANNEL], self.zBandStop = signal.sosfilt(self.sos_bandstop, outdata[:, audioSettings.DEFAULT_CHANNEL], zi=self.zBandStop)
            #outdata[:, audioSettings.DEFAULT_CHANNEL], self.zBandStop = signal.sosfilt(self.sos_bandstop, outdata[:, audioSettings.DEFAULT_CHANNEL], zi=self.zBandStop)
        else:
            outdata[:, audioSettings.DEFAULT_CHANNEL] = self.noAudioInput
        # half-duplex communication
        ################
        # NOTE: hard condition may interrupt TX in the middle of a telegram
        #            and produce an error when continuing after reception
        # we could instead wait until a complete telegram is transmitted but for now
        # we just don't do that...the highest priority is RX (we are considerate)
        # and this way we allow a re-transmission from the other side without
        # blocking it with a long telegram 
        ### if self.receive_on_ref[0] == False:
        # add message to TX in
        #############
        try:
            readMessage = False # to help reduce blocking time
            readIndexTemp = 0
            # BLOCKING code block...but acceptable because it blocks only very seldom and very shortly
            ####################################################
            with self.messageLock:
                # NEW chunk ?
                if (self.telegramNrRead != self.telegramNrWrite):
                    readIndexTemp = self.telegramNrRead*audioSettings.MAX_NR_OF_CHUNKS_PER_TELEGRAM+self.chunkNrRead*audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN
                    readMessage = True
                    # set half-duplex flag
                    if self.transmit_on_ref[0]  == False:
                        self.transmit_on_ref[0] = True
                        logging.info("TX ON")
                else:
                    # reset half-duplex flag
                    if self.transmit_on_ref[0]:
                        self.transmit_on_ref[0] = False
                        logging.info("TX OFF")
            # this part is not blocking any more
            ####################################################
            if readMessage == True:
                # write chunk
                outdata[:audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN] = outdata[:audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN] + \
                                                                                                  self.audioChunkRef[readIndexTemp:readIndexTemp+audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN]
                # increment chunk number
                self.chunkNrRead = (self.chunkNrRead + 1)%self.telegramNrReadSize[self.telegramNrRead]
                # increment telegram counter
                if self.chunkNrRead == 0:
                    self.telegramNrRead = (self.telegramNrRead + 1)%audioSettings.MAX_NR_OF_TELEGRAMS_IN_PARALLEL
            # add carrier
            ########
            if audioSettings.ADD_CARRIER:
                # reduce amplitude of voice so we dont saturate output when adding carrier
                outdata[:audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN] = (1.0 - audioSettings.CARRIER_AMPLITUDE)*outdata[:audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN]
                # add carrier
                outdata[:audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN] += audioSettings.CARRIER
        except Exception as e: # queue.Empty:
            logging.error("Exception in AudioTransmitter.callback_wire_out():"+str(e)+"\n")
    
    # half of the times we retry after "double" the necessary time in order to give the other side
    # a chance to successfully transmit in case we are having collissions due to simultaneous transmissions from both sides.
    # NOTE: for TX and RX we have different physical channels, so it is NOT exactly COLLISSIONS what we have
    #            but probably interferences e.g. due to "cross-talk" (EM coupling between lines).
    def randomRetryTimeout(self):
        rnd = np.random.randint(2)
        if rnd:
            self.TX_RETRANSMISSION_POLL_PERIODS = audioSettings.TX_RETRANSMISSION_POLL_PERIODS_SHORT
        else:
            self.TX_RETRANSMISSION_POLL_PERIODS = audioSettings.TX_RETRANSMISSION_POLL_PERIODS_LONG
            
    # called from soundDeviceManager, and it in turn from GUI-triggered-thread
    def call_once(self):
        msg = [audioSettings.COMMAND_CALL, bytearray([self.comm_token[0]])]
        # will be processed in thread_send_message after get() from queue and call to sendAudioMessage()
        self.outTextMessageQueue.put(msg) 
        
    def call_accept(self):
        msg = [audioSettings.COMMAND_CALL_ACCEPTED, bytearray(0)]
        # will be processed in thread_send_message after get() from queue and call to sendAudioMessage()
        self.outTextMessageQueue.put(msg) 
        
    def call_reject(self):
        self.reject_call = True
        
    def call_end(self):
        self.end_call = True
        
    def pollErrorMessage(self):
        ret = self.errorMessage
        if self.errorMessage != "":
            self.errorMessage = ""
        return ret
        
    def isTxStateWaitAck(self):
        return (self.tx_state == WAIT_ACK)
    
    def thread_send_message(self, name):
        # store info for retransmissions
        old_data = bytearray(0)
        old_command = audioSettings.COMMAND_NONE
        nr_polls = 0
        nr_of_resends = 0
        # statistics
        startRoundtripTime = 0.0
        logging.info("enter thread_send_message")
        # main loop of thread:
        while self.stream_on[0]:
            try:
                # state machine
                if self.tx_state == IDLE:
                    msg = []
                    data = bytearray(0)
                    command = audioSettings.COMMAND_NONE
                    # BLOCKING call on queue to obtain TEXT MESSAGE data from GUI
                    #######################################
                    if self.outTextMessageQueue.empty() == False:
                        msg = self.outTextMessageQueue.get() # .get_nowait()
                        command = msg[0]
                        if msg[1] is not None:
                            data = msg[1]
                    # send command with or without data
                    # we may need to increment seqNr (but only for some commands!)
                    if  (command == audioSettings.COMMAND_CHAT_DATA) or (command == audioSettings.COMMAND_CHAT_DATA_START) or \
                        (command == audioSettings.COMMAND_CHAT_DATA_PART) or (command == audioSettings.COMMAND_CHAT_DATA_END) or \
                        (command == audioSettings.COMMAND_STARTUP_DATA_COMPLETE):
                        self.seqNrTx[0] = (self.seqNrTx[0] + 1)%255
                        self.tx_state = WAIT_ACK
                    elif self.reject_call:
                        self.reject_call = False
                        self.seqNrTx[0] = (self.seqNrTx[0] + 1)%255
                        command = audioSettings.COMMAND_CALL_REJECTED
                        self.tx_state = WAIT_ACK
                    elif self.end_call:
                        self.end_call = False
                        self.seqNrTx[0] = (self.seqNrTx[0] + 1)%255
                        command = audioSettings.COMMAND_CALL_END
                        self.tx_state = WAIT_ACK
                    # workaround
                    ########
                    elif command == audioSettings.COMMAND_CALL:
                        self.seqNrAck[0] = 0
                        self.seqNrAckRx[0] = 0
                        self.seqNrTx[0] = 0
                        logging.info("SeqNrs reset on CALL!")
                    # "append" ACK to command if required
                    if self.send_ack[0] == True:
                        self.send_ack[0] = False
                        command = (command | audioSettings.COMMAND_TELEGRAM_ACK)
                    # send telegram
                    if  command != audioSettings.COMMAND_NONE:
                        # reset flags
                        nr_polls = 0
                        nr_of_resends = 0
                        self.randomRetryTimeout()
                        old_data = data
                        old_command = command
                        ##############
                        # store time to calculate roundtrip time
                        startRoundtripTime = cProfileTimer()
                        ##############
                        # LONG-BLOCKING calls to sendAudioMessage()
                        self.sendAudioMessage(command, data)
                        # status
                        self.outCommStatusQueue.put("TX: "+audioSettings.CMD_STR[command]) # +", data = "+str(data))
                        
                        # TEST
                        if audioSettings.CMD_STR[command] == "":
                            time.sleep(1)
                            
                        ########
                        # because of half-duplex communication we don't want to "force" the transmission of
                        # "consecutive" telegrams, especially in the case of ACKs which may be triggered right before or after
                        # other telegrams...so, in order to keep things clean we wait a little bit..
                        # NOTE: we've seen distortions when telegrams are transmitted right after each other...
                        # TODO: check this time...
                        ### time.sleep(audioSettings.TX_RETRANSMISSION_SEC/2)
                        # increment "retransmission timer" correspondingly
                        ### nr_polls += (audioSettings.TX_RETRANSMISSION_SEC/2)/audioSettings.TX_POLL_PERIOD_SEC
                        ########
                elif self.tx_state == WAIT_ACK:
                    # received an ACK to our command?
                    if self.ack_received[0] == True:
                        self.ack_received[0] = False
                        if self.seqNrAckRx[0] == self.seqNrTx[0]:
                            # reset sequence numbers
                            if old_command == audioSettings.COMMAND_CALL_END:
                                self.seqNrAck[0] = 0
                                self.seqNrAckRx[0] = 0
                                self.seqNrTx[0] = 0
                                logging.info("Reset SeqNrs")
                            #############
                            # update roundtrip time
                            self.avg_roundtrip_time_ms = (self.ack_received[1]- startRoundtripTime)*1000.0
                            #############
                            self.tx_state = IDLE
                            # statistics
                            self.telTxOk += 1
                            # status
                            self.outCommStatusQueue.put("") # ("TX:")
                        else:
                            # statistics
                            self.telTxNok += 1
                            logging.error("ERROR: Got an ACK but not for the last telegram we sent!")
                    # retransmission timer expired?
                    elif (self.tx_state == WAIT_ACK) and (nr_polls > self.TX_RETRANSMISSION_POLL_PERIODS):
                        # "append" ACK to command if required
                        if self.send_ack[0]:
                            self.send_ack[0] = False
                            old_command = old_command | audioSettings.COMMAND_TELEGRAM_ACK
                        # retransmit telegram
                        # LONG-BLOCKING call
                        self.resendAudioMessage(old_command, old_data)
                        nr_of_resends += 1
                        nr_polls = 0
                        self.randomRetryTimeout()
                        # statistics
                        self.telTxNok += 1
                        logging.info("Retransmitted message due to timeout! Nr. of retransmissions = "+str(nr_of_resends))
                        # status
                        self.outCommStatusQueue.put("TX: "+audioSettings.CMD_STR[old_command]+", resend "+str(nr_of_resends)) # +", data = "+str(old_data))
                        
                        # TEST
                        if audioSettings.CMD_STR[old_command] == "":
                            time.sleep(1)
                        
                        ########
                        # because of half-duplex communication we don't want to "force" the transmission of
                        # "consecutive" telegrams, especially in the case of ACKs which may be triggered right before or after
                        # other telegrams...so, in order to keep things clean we wait a little bit..
                        # NOTE: we've seen distortions when telegrams are transmitted right after each other...
                        # TODO: check this time...
                        ### time.sleep(audioSettings.TX_RETRANSMISSION_SEC/2)
                        # increment "retransmission timer" correspondingly
                        ### nr_polls += (audioSettings.TX_RETRANSMISSION_SEC/2)/audioSettings.TX_POLL_PERIOD_SEC
                    # maximum number of resends exceeded?
                    elif (self.tx_state == WAIT_ACK) and (nr_of_resends >= audioSettings.MAX_RESENDS):
                        # reset sequence numbers
                        if old_command == audioSettings.COMMAND_CALL_END:
                            self.seqNrAck[0] = 0
                            self.seqNrAckRx[0] = 0
                            self.seqNrTx[0] = 0
                            logging.info("Reset SeqNrs")
                        self.errorMessage = "TX ERROR: Max. nr. of Resends ("+str(audioSettings.MAX_RESENDS)+") exceeded with:\n"+\
                        " reject_call = "+str(self.reject_call)+"\n"\
                        " end_call = "+str(self.end_call)+"\n"\
                        " data = "+str(old_data)+"\n"\
                        " command = "+str(old_command)
                        # statistics
                        self.telTxNok += 1
                        logging.error("ERROR: Max. nr. of Resends ("+str(audioSettings.MAX_RESENDS)+") exceeded, \
                            we just give up here...and go back to IDLE")
                        # status
                        self.outCommStatusQueue.put("TX: > resend max "+str(audioSettings.MAX_RESENDS)+", "+audioSettings.CMD_STR[old_command]) # +", data = "+str(old_data))
                        self.tx_state = IDLE
                    # NOTE: we comment this code for now...it seems to trigger unnecessary re-transmissions when ACK received.
                    #            Instead, the retransmission itself will make sure to send the corresponding ACK on time.
                    '''
                    # received telegram which triggered an ACK while waiting ourselves for an ACK ? 
                    elif self.send_ack[0]:
                        # "append" ACK to command
                        self.send_ack[0] = False
                        old_command = old_command | audioSettings.COMMAND_TELEGRAM_ACK
                        # retransmit telegram with ACK to telegram received during wait
                        # LONG-BLOCKING call
                        self.resendAudioMessage(old_command, old_data)
                        nr_of_resends +=1
                        nr_polls = 0
                        self.randomRetryTimeout()
                        # statistics
                        self.telTxNok += 1
                        logging.info("Retransmitted message due to telegram reception which triggered an ACK reply from us! \
                            Nr. of retransmissions = "+str(nr_of_resends))
                        ########
                        # because of half-duplex communication we don't want to "force" the transmission of
                        # "consecutive" telegrams, especially in the case of ACKs which may be triggered right before or after
                        # other telegrams...so, in order to keep things clean we wait a little bit..
                        # NOTE: we've seen distortions when telegrams are transmitted right after each other...
                        # TODO: check this time...
                        ###time.sleep(audioSettings.TX_RETRANSMISSION_SEC/2)
                        # increment "retransmission timer" correspondingly
                        ###nr_polls += (audioSettings.TX_RETRANSMISSION_SEC/2)/audioSettings.TX_POLL_PERIOD_SEC
                    # '''
                    # increment "retransmission timer"
                    nr_polls += 1
                # polling sleep pause
                time.sleep(audioSettings.TX_POLL_PERIOD_SEC)
            except Exception as e:
                logging.error("Exception in AudioTransmitter.thread_send_message():"+str(e)+"\n")
        logging.info("leave thread thread_send_message..")
        
    def sendAudioMessage(self, command, message):
        self.sendAudioMessageSeq(command, message)
            
    def resendAudioMessage(self, command, message):
        self.sendAudioMessageSeq(command, message)
    
    # LONG BLOCKNIG function called from internal thread_send_message
    def sendAudioMessageSeq(self, command, byte_message):
        logging.info("TX MSG = "+str(byte_message))
        # trap telegrams which are too long
        # TODO: remove, this ASSERT is not needed anymore?
        ###############################
        data_len = len(byte_message)
        if data_len > audioSettings.DATA_MAX_LEN_BYTES:
            logging.info("ERROR: message discarded, "+str(data_len)+" exceeds maximum lenght " + \
                                str(audioSettings.DATA_MAX_LEN_BYTES) + " !")
            return
        # get bit array
        # n-bytes PREAMBLE and ONE byte START
        start = 85 # = b"\x55"
        address = 1 # = b"\x01"
        logging.info("TX CMD = "+str(command)+" ("+audioSettings.CMD_STR[command]+")")
        logging.info("    SN = "+str(self.seqNrTx[0]))
        logging.info("    SA = "+str(self.seqNrAck[0]))
        checksum = 0 # = b"\x00" # start value
        end = b"\xAA" # = 170
        checksum = checksum^start
        checksum = checksum^address
        checksum = checksum^self.seqNrTx[0]
        checksum = checksum^self.seqNrAck[0]
        checksum = checksum^command
        checksum = checksum^data_len
        for byte in byte_message:
            checksum = checksum^byte
        # form telegram bytearray
        byte_telegram = b"\xFF"*audioSettings.TELEGRAM_PREAMBLE_LEN_BYTES +bytearray([start]) + \
                                bytearray([address]) + bytearray([self.seqNrTx[0]]) + bytearray([self.seqNrAck[0]]) + \
                                bytearray([command]) + bytearray([data_len]) + byte_message + end + bytearray([checksum]) + \
                                b"\x00"*audioSettings.TELEGRAM_TERMINATOR_LEN_BYTES
        bitarray_telegram = bitarray.bitarray()
        # form telegram bitarray
        bitarray_telegram.frombytes(byte_telegram)
        # update write index
        localWriteIndex = self.telegramNrWrite*audioSettings.MAX_NR_OF_CHUNKS_PER_TELEGRAM+self.chunkNrWrite*audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN
        # transform bits into audio samples
        currPos = 0
        for telegram_bit in bitarray_telegram:
            if telegram_bit:
                self.audioChunkRef[localWriteIndex + currPos:localWriteIndex + currPos + audioSettings.LEN_BIT_ONE] = audioSettings.ONE
                currPos = currPos + audioSettings.LEN_BIT_ONE
            else:
                self.audioChunkRef[localWriteIndex + currPos:localWriteIndex + currPos + audioSettings.LEN_BIT_ZERO] = audioSettings.ZERO
                currPos = currPos + audioSettings.LEN_BIT_ZERO
        # soften borders of telegram with Gauss-/Normal- shape
        # this shall avoid generating high-frequencies when coding (beginning of sine from silence is like a step-signal):
        #
        #     |
        #     |                     /
        #   _|       ==>      _/
        #
        # for now we use LEN_BIT_ZERO as a reference for the length because it's usually shorter than LEN_BIT_ONE
        for j in range(0, audioSettings.CODE_TRANSITION_SAMPLES):
            self.audioChunkRef[localWriteIndex + j] = self.audioChunkRef[localWriteIndex + j] * self.gauss[j]
            self.audioChunkRef[(localWriteIndex + currPos) - j] = self.audioChunkRef[(localWriteIndex + currPos) - j] * self.gauss[j]
        # the telegram length in samples is given by the actual combination of ONES and ZEROS which may have different lengths
        lenBitArray = bitarray_telegram.length()
        nr_ones = bitarray_telegram.count()
        nr_zeros = lenBitArray - nr_ones
        currentTelLenInSamples = audioSettings.LEN_BIT_ONE*nr_ones + audioSettings.LEN_BIT_ZERO*nr_zeros
        samplesInLastChunk = currPos%audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN
        # calculate parts to split, that is, the number of chunks
        if samplesInLastChunk == 0:
            split_parts = int(currentTelLenInSamples/audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN)
        else:
            split_parts = int(currentTelLenInSamples/audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN) + 1
            # PADDING: fill last empty part of chunk with silence..to have a full chunk filled with enough samples
            self.audioChunkRef[localWriteIndex + currPos:localWriteIndex + currPos + (audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN - samplesInLastChunk)] = \
                                                                                        audioSettings.SILENCE[:(audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN - samplesInLastChunk)]
            currPos = currPos + (audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN - samplesInLastChunk)
        # trap in case the buffer is full
        while((self.telegramNrWrite+1)%audioSettings.MAX_NR_OF_TELEGRAMS_IN_PARALLEL == self.telegramNrRead):
            logging.error("ERROR: biting our tail! Increase size of circular buffer for transmission")
            # this short delay (positive assumption) may lead to re-entering this while loop several times..
            time.sleep(audioSettings.AUDIO_CHUNK_DELAY_SEC)
        # set size of telegram to read BEFORE incrementing writeIndex !!!
        # NOTE: we don't need a lock here becuse we use a different index than the one possibly being read..
        if split_parts < audioSettings.MAX_NR_OF_CHUNKS_PER_TELEGRAM:
            with self.messageLock:
                self.telegramNrReadSize[self.telegramNrWrite] = split_parts
        else:
            with self.messageLock:
                # for the FIRST telegram - which is filled with max nr of chunks:
                self.telegramNrReadSize[self.telegramNrWrite] = audioSettings.MAX_NR_OF_CHUNKS_PER_TELEGRAM
        # LOOP to transmit chunks and telegrams
        ########################
        for index in range(split_parts):
            # calculate index to be used as offset for current chunk position in buffer self.audioChunkRef
            localWriteIndex = self.telegramNrWrite*audioSettings.MAX_NR_OF_CHUNKS_PER_TELEGRAM+self.chunkNrWrite*audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN
            # filter signal with coded message because it usually contains frequencies outside the coding range...
            # besides, we will add CODE "on top" of voice in time domain so they should be in different frequency-ranges to NOT saturate audio interface
            # fiter CODE
            self.audioChunkRef[localWriteIndex:localWriteIndex + audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN, audioSettings.DEFAULT_CHANNEL], self.z = \
                    signal.sosfilt(self.sos_bandpass, self.audioChunkRef[localWriteIndex:localWriteIndex + audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN, audioSettings.DEFAULT_CHANNEL], zi=self.z) 
            # dont filter CODE
            ### self.audioChunkRef[localWriteIndex:localWriteIndex + audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN, audioSettings.DEFAULT_CHANNEL] = \
                    ### self.audioChunkRef[localWriteIndex:localWriteIndex + audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN, audioSettings.DEFAULT_CHANNEL]
            # increment chunk number
            self.chunkNrWrite = (self.chunkNrWrite + 1)%audioSettings.MAX_NR_OF_CHUNKS_PER_TELEGRAM
            # intermediate complete telegram?
            if self.chunkNrWrite == 0:
                # then we need to increment telegram counter, but before we update the length of telegram
                # BLOCKING code block
                with self.messageLock:
                    # set telegram size to maximum
                    self.telegramNrReadSize[self.telegramNrWrite] = audioSettings.MAX_NR_OF_CHUNKS_PER_TELEGRAM
                    # this increment is like a SIGNAL to the callback
                    self.telegramNrWrite = (self.telegramNrWrite + 1)%audioSettings.MAX_NR_OF_TELEGRAMS_IN_PARALLEL
            # or last chunk?
            elif index == (split_parts - 1):
                # then we need to increment telegram counter, but before we update the length of telegram
                # BLOCKING code block
                with self.messageLock:
                    # set telegram size to nr. of chunks used
                    self.telegramNrReadSize[self.telegramNrWrite] = split_parts%audioSettings.MAX_NR_OF_CHUNKS_PER_TELEGRAM
                    # this increment is like a SIGNAL to the callback
                    self.telegramNrWrite = (self.telegramNrWrite + 1)%audioSettings.MAX_NR_OF_TELEGRAMS_IN_PARALLEL
                # reset chunk counter
                self.chunkNrWrite = 0
        # end of sendAudioMessage()
        ################
        return
        
    # BLOCKNIG function called from external GUI thread (from mainWindow)
    def sendMessage(self, message):
        # encryption uses bytes but we have strings or bytearrays..
        message = bytes(message.encode('utf-8'))
        padder = padding.PKCS7(configuration.PADDING_BITS_LEN).padder()
        padded_data = padder.update(message)
        padded_data += padder.finalize()
        encryptor = self.cipher[0].encryptor()
        encryptedMessage = encryptor.update(padded_data) + encryptor.finalize()
        # Split message into different telegrams if required !!!
        split_message = []
        msg =[]
        # TODO: any relation with configuration.ENRYPTION_BLOCK_BYTES_LEN ?
        n =  audioSettings.DATA_MAX_LEN_BYTES
        # split?
        if len(encryptedMessage) > n:
            split_message = [encryptedMessage[i:i+n] for i in range(0, len(encryptedMessage), n)]
        # put message in queue
        if split_message != []:
            len_split_message = len(split_message)
            for i in range(len_split_message):
                # will be processed in thread_send_message after get() from queue and call to sendAudioMessage()
                if i == 0:
                    msg = [audioSettings.COMMAND_CHAT_DATA_START, split_message[i]]
                elif i == len_split_message - 1:
                    msg = [audioSettings.COMMAND_CHAT_DATA_END, split_message[i]]
                else:
                    msg = [audioSettings.COMMAND_CHAT_DATA_PART, split_message[i]]
                self.outTextMessageQueue.put(msg)
        else:
            msg = [audioSettings.COMMAND_CHAT_DATA, encryptedMessage]
            # will be processed in thread_send_message after get() from queue and call to sendAudioMessage()
            self.outTextMessageQueue.put(msg)
    
    def generatePublicKey(self):
        # generate public key for this session
        self.private_key[0] = x25519.X25519PrivateKey.generate()
        public_key = self.private_key[0].public_key()
        public_key_bytes = public_key.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
        n = audioSettings.DATA_MAX_LEN_BYTES
        if len(public_key_bytes) > n:
            public_key_bytes_split = [public_key_bytes[i:i+n] for i in range(0, len(public_key_bytes), n)]
            self.key_start = public_key_bytes_split[0]
            self.key_end = public_key_bytes_split[1]
        else:
            self.key_start = public_key_bytes
            self.key_end = bytearray(0)
    
    def send_key_start_once(self):
        msg = [audioSettings.COMMAND_KEY_START, self.key_start]
        # will be processed in thread_send_message after get() from queue and call to sendAudioMessage()
        self.outTextMessageQueue.put(msg) 
        
    def send_key_end_once(self):
        msg = [audioSettings.COMMAND_KEY_END, self.key_end]
        # will be processed in thread_send_message after get() from queue and call to sendAudioMessage()
        self.outTextMessageQueue.put(msg) 
        
    def send_startup_data_once(self, my_name):
        # encryption uses bytes but we have strings or bytearrays..
        # cut length to max. possible to be transmitted in a single block
        len_my_name = len(my_name)
        if len_my_name > configuration.ENRYPTION_BLOCK_BYTES_LEN:
            len_my_name = configuration.ENRYPTION_BLOCK_BYTES_LEN
        message = bytes(my_name.encode('utf-8'))[0:len_my_name]
        padder = padding.PKCS7(configuration.PADDING_BITS_LEN).padder()
        padded_data = padder.update(message)
        padded_data += padder.finalize()
        encryptor = self.cipher[0].encryptor()
        encryptedMessage = encryptor.update(padded_data) + encryptor.finalize()
        msg = [audioSettings.COMMAND_STARTUP_DATA, encryptedMessage]
        # will be processed in thread_send_message after get() from queue and call to sendAudioMessage()
        self.outTextMessageQueue.put(msg)
        
    # needs ACK, will be resend automatically up to max. nr. of times...
    def send_startup_data_complete(self, my_name):
        # encryption uses bytes but we have strings or bytearrays..
        # cut length to max. possible to be transmitted in a single block
        len_my_name = len(my_name)
        if len_my_name > configuration.ENRYPTION_BLOCK_BYTES_LEN:
            len_my_name = configuration.ENRYPTION_BLOCK_BYTES_LEN
        message = bytes(my_name.encode('utf-8'))[0:len_my_name]
        padder = padding.PKCS7(configuration.PADDING_BITS_LEN).padder()
        padded_data = padder.update(message)
        padded_data += padder.finalize()
        encryptor = self.cipher[0].encryptor()
        encryptedMessage = encryptor.update(padded_data) + encryptor.finalize()
        msg = [audioSettings.COMMAND_STARTUP_DATA_COMPLETE, encryptedMessage]
        # will be processed in thread_send_message after get() from queue and call to sendAudioMessage()
        self.outTextMessageQueue.put(msg)

    def getAvgTxTimeMs(self):
        return self.avg_tx_time_ms
        
    def getRoundtripTimeMs(self):
        return self.avg_roundtrip_time_ms
        
    def getTelegramCircularBufferSize(self):
        return (self.telegramNrWrite - self.telegramNrRead)%audioSettings.MAX_NR_OF_TELEGRAMS_IN_PARALLEL
        
    def getTelTxOk(self):
        return self.telTxOk
    
    def getTelTxNok(self):
        return self.telTxNok
        
    # TODO: focus only on cleaning the queue instead?
    def purge(self):
        # no method .clear() available..so:
        self.outTextMessageQueue = queue.Queue()
        self.seqNrAck[0] = 0
        self.seqNrAckRx[0] = 0
        self.seqNrTx[0] = 0
        # TODO: reset here also other flags, counters, etc.???
        self.tx_state = IDLE
        self.outCommStatusQueue.put("TX: purged")
        logging.info("TX purge")
       










