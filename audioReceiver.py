# -*- coding: utf-8 -*-

import numpy as np
from scipy.fft import rfft
import audioSettings
import queue
import configuration
import math
from scipy import signal
import threading
import logging
### import time
from dataclasses import dataclass
from bitarray import bitarray
from bitarray.util import ba2int
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import copy # from copy import deepcopy
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from timeit import default_timer as cProfileTimer
import random

''''
This module implements the right side of this drawing:
    
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
# settings "band pass filter" to recover CODE
 # WARNING: BPF only to hear and/or plot CODE-Frequencies but NOT for decoding!
#################################################
BPF_LEFT_MARGIN = 400
BPF_RIGHT_MARGIN = 400
BPF_ORDER = 7
BPF_MAX_RIPPLE = 0.1
BPF_ELL_MIN_ATTENUATION = 145.0
# settings "band stop filter" to recover VOICE / Audio transmitted by the other side
BSF_LEFT_MARGIN = 400
BSF_RIGHT_MARGIN = 400
BSF_ORDER = 7
BSF_MAX_RIPPLE = 0.1
# settings "elliptic filter"
BSF_ELL_MIN_ATTENUATION = 145.0
# settings for "notsch filter" to remove "carrier" if configured
# Quality factor
Q = 0.3 # 0.3 # 3.0
# for undistorting voice
# TODO: investiage why 920Hz can be suppressed so well but other frequencies not..
f0 = 920.0 # Modulating frequency to be removed from distorted signal (Hz) - see NOTCH filter in audioTransmitter
# definitions for decoding state machine
SEARCH_PREAMBLE = 0
SEARCH_START = 1
DECODE_FRAME = 2
# definitions for decoding telegram fields (= sub-state of DECODE_FRAME)
DECODE_ADDRESS = 0
DECODE_SEQ_NR = 1
DECODE_SEQ_NR_ACK = 2
DECODE_COMMAND = 3
DECODE_DATA_LEN = 4
DECODE_DATA = 5
DECODE_END = 6
DECODE_CHECKSUM = 7
# take also ONE byte from PREAMBLE to avoid detecting START by coincidentially/random correct value
# we need to do this because otherwise we shall know exactly where the preamble finishes...but that is exactly what we want to find out!
LAST_PREAMBLE_BYTE_AND_START_BITS = bitarray([True,True,True,True,True,True,True,True,False,True,False,True,False,True,False,True]) # = b"\xFF\x55"
START_BITS = bitarray([False,True,False,True,False,True,False,True]) # = b"\x55"
END_BITS = bitarray([True,False,True,False,True,False,True,False]) # = b"\xAA"
# definitions for reception state
IDLE = 0
CALL_ACCEPTED = 1
KEY_START_RECEIVED = 2
KEY_END_RECEIVED = 3


class AudioReceiver():
    # protocol
    seqNrAckRx = [0] # reference to sequence number ACK from transmitter (correctly received)
    seqNrAck = [0] # reference to sequence number for ACK
    seqNrTx = [0] # reference to TX seqNr
    # cipher object containing key and decryptor
    cipher = [None]
    peer_public_key_start = None # helper variable to pass 1st part of our public key
    peer_public_key_end = None # helper variable to pass 2nd part of our public key
    # out-band-verification of key-exchange using a session-code,
    # which is an integer number derived from the common session key,
    # which in turn results from both public keys.
    session_code = ""
    # flags
    stream_on = [False]
    transmit_on_ref = None # reference to flag for half-duplex communication
    receive_on_ref = None # reference to flag for half-duplex communication
    ack_received = [False, 0] # reference to flag for ACK received
    send_ack = [False] # reference to flag for send ACK
    call = False
    call_accepted = False
    call_rejected = False
    call_end = False
    key_start_received = False
    key_end_received = False
    startup_data_received = False
    comm_token = [255]
    have_token = True
    # reception state
    rx_state = IDLE
    # constant definitions which depend on configuration settings
    # constants for filters
    BPF_F1 = audioSettings.CODE_SINE_FREQUENCY_ONE - BPF_LEFT_MARGIN
    BPF_F2 = audioSettings.CODE_SINE_FREQUENCY_ZERO + BPF_RIGHT_MARGIN
    BSF_F1 = audioSettings.CODE_SINE_FREQUENCY_ONE - BSF_LEFT_MARGIN
    BSF_F2 = audioSettings.CODE_SINE_FREQUENCY_ZERO + BSF_RIGHT_MARGIN
    # constants for detection algorithm
    BIG_SCAN_ROUNDS = 4
    BIG_STEP = audioSettings.LEN_BIT_ONE//BIG_SCAN_ROUNDS
    SMALL_SCAN_ROUNDS = (audioSettings.LEN_BIT_ONE//BIG_SCAN_ROUNDS)*2
    SMALL_STEP = 1
    # state parse and decode
    parse_state = SEARCH_PREAMBLE
    decode_state = DECODE_ADDRESS
    # performance statistics
    avg_rx_time_ms = 0.0
    time_old = 0.0
    avg_in_amplitude_percent = 0
    # communication statistics
    telRxOk = 0
    telRxNok = 0
    # queues
    qplot = queue.Queue() # to update matplotlib plot in "main loop"
    # NOTE: for some reason, qin works better than using a circular buffer as we do in audioTransmitter
    # here we don't have "bursts" of chunks that want to be put into buffer all at once as in the case of transmission
    qin = queue.Queue() # audio data from RX in -> to be decoded AND to be forwarded to matplot over qplot
    # input messages to be read e.g. by chat in GUI
    inMessageQueue = queue.Queue() 
    inCommStatusQueue = queue.Queue()
    # timer for setting receive_on_ref
    receive_on_timer_event = threading.Event()
    # TODO: better module variable?
    telegram_bits = None
    telegram_bits_start_pos = 0
    telegram_bits_end_pos = 0
    # filter BAND-PASS
    # WARNING: BPF only to hear and/or plot CODE-Frequencies but NOT for decoding!
    #################################################
    sos_bandpass = None
    z = None
    # filter BAND-STOP
    sos_bandstop = None
    zBandStop = None
    # NOTCH filter
    sos_notch = None
    zNotch = None
    # helper variable
    bit_prev = None
    # definition and variable used to recover cut-bits between audio-chunks or parts of size N of audio-chunk
    PREVIOUS_SAMPLES = 0
    # samples from last round (or previous call) containing enough info to hold a complete "cut" PREAMBLE-LAST-BYTE + START marker (in general the used nr. of samples will be lower)
    data_prev = None
    
    @dataclass
    class StartupDataClass:
        comm_partner: str
    startup_data = StartupDataClass("")
    
    @dataclass
    class TelegramClass:
        address: int # int8
        seqNr: int
        seqNrAck: int
        command: int # int8
        data_length: int # int8
        data: bytearray # [audioSettings.DATA_MAX_LEN_BYTES]
        end: int # int8
        checksum: int # int8 # calculated on bytes from START to last byte of DATA
        decodedDataBytes: int # int8
        seqNrRepeated: bool
    telegram = TelegramClass(0,0,0,0,0,bytearray(audioSettings.DATA_MAX_LEN_BYTES),0,0,0,False)
    data_part = bytearray(audioSettings.MAX_TEXT_LEN)
    part_end_idx = 0

    def __init__(self, glob_vars):
        self.stream_on = glob_vars[0].stream_on
        self.ack_received = glob_vars[0].ack_received
        self.send_ack = glob_vars[0].send_ack
        self.transmit_on_ref = glob_vars[0].transmit_on_ref
        self.receive_on_ref = glob_vars[0].receive_on_ref
        self.seqNrAck = glob_vars[0].seqNrAck
        self.seqNrAckRx = glob_vars[0].seqNrAckRx
        self.seqNrTx = glob_vars[0].seqNrTx
        self.private_key = glob_vars[0].private_key
        self.cipher = glob_vars[0].cipher
        self.comm_token = glob_vars[0].comm_token
        self.comm_token[0] = random.randint(0, 255)
        self.have_token = True # assume for now we have the token
        self.session_code = ""
        self.peer_public_key_start = bytearray(0)
        self.telRxOk = 0
        self.telRxNok = 0
        self.rx_state = IDLE
        self.PREVIOUS_SAMPLES = audioSettings.START_LEN_SAMPLES + 8*audioSettings.LEN_BIT_ONE
        self.data_prev = np.array([0.0]*(audioSettings.N + self.PREVIOUS_SAMPLES))
        # TODO: better module variable?
        self.telegram_bits = bitarray(audioSettings.TELEGRAM_MAX_LEN_BITS)
        # filter BAND-PASS
        # WARNING: BPF only to hear and/or plot CODE-Frequencies but NOT for decoding!
        #################################################
        self.sos_bandpass = signal.ellip(BPF_ORDER, BPF_MAX_RIPPLE, BPF_ELL_MIN_ATTENUATION,
                                # IMPORTANT: we need to divice by Nyquist frequency or pass fs as argument...one thing or the other..
                                # [BPF_F1 / audioSettings.NYQUIST_FREQUENCY, BPF_F2 / audioSettings.NYQUIST_FREQUENCY],'bandpass', analog=False, output='sos')
                                [self.BPF_F1, self.BPF_F2],'bandpass', analog=False, fs=audioSettings.SAMPLING_FREQUENCY, output='sos')
                                # TODO: check if detection can be improved by using a HIGH-PASS-FILTER instead:
                                # BPF_F1,'highpass', analog=False, fs=audioSettings.SAMPLING_FREQUENCY, output='sos')
        # IMPORTANT: we need this TRICK to filter audio signal "in chunks":
        self.z = np.zeros((self.sos_bandpass.shape[0], 2))
        # filter BAND-STOP
        self.sos_bandstop = signal.ellip(BSF_ORDER, BSF_MAX_RIPPLE, BSF_ELL_MIN_ATTENUATION,
                                # IMPORTANT: we need to divice by Nyquist frequency or pass fs as argument...one thing or the other..
                                # [BPF_F1 / audioSettings.NYQUIST_FREQUENCY, BPF_F2 / audioSettings.NYQUIST_FREQUENCY],'bandpass', analog=False, output='sos')
                                [self.BSF_F1, self.BSF_F2],'bandstop', analog=False, fs=audioSettings.SAMPLING_FREQUENCY, output='sos')
        # IMPORTANT: we need this TRICK to filter audio signal "in chunks":
        self.zBandStop = np.zeros((self.sos_bandstop.shape[0], 2))
         # flag timer thread for half-duplex communication
        receive_on_timer_thread = threading.Thread(target=self.thread_receive_on_timer,  args=(1,))
        receive_on_timer_thread.start()
        # NOTCH filter
        # TODO: in case "both" sides need a carrier then the frequencies need to be different, therefore we need a new definition different to CARRIER_FREQUENCY_HZ
        b, a = signal.iirnotch(audioSettings.CARRIER_FREQUENCY_HZ, Q, fs=audioSettings.SAMPLING_FREQUENCY) # ,  output='sos') # cannot return sos for some reason..
        # IMPORTANT: we need this TRICK to filter audio signal "in chunks":
        Z, P, K = signal.tf2zpk(b, a)
        self.sos_notch = signal.zpk2sos(Z, P, K)
        self.zNotch = np.zeros((self.sos_notch.shape[0], 2))
        # start decoder thread
        decode = threading.Thread(target=self.thread_decode,  args=(1,))
        decode.start()
        # status
        self.inCommStatusQueue.put("") # ("RX:")
        
    ################################################################
    # TODO: cannot invert if we are NOT exactly synchronized, right? we have different starts for currSample in RX
    #            investigate and correct this mess...use or implement something better..
    currSample = 0
    def undistortFunction(self,  x):
        # need to revert: ret = x*math.sin(2 * np.pi *f0*self.currSample/audioSettings.SAMPLING_FREQUENCY)*2
        #                                                      ret = x * sin (F * sample) * 2
        #                        TEST3 below is ==>  ret = x / (2 * sin(F * sample))
        #############################################################
        # TEST1
        #####
        # ret= math.asin(x) * (2 * np.pi *f0*self.currSample/audioSettings.SAMPLING_FREQUENCY) * 2
        # ret = ret/audioSettings.N
        #####
        # TEST2
        #####
        # if self.currSample == 0:
        #     ret = 0
        # else:
        #     ret= math.asin(x/2) / (2 * np.pi *f0*self.currSample/audioSettings.SAMPLING_FREQUENCY)
        # ret = ret/audioSettings.N
        #####
        # TEST3
        #####
        if math.sin(2 * np.pi *f0*self.currSample/audioSettings.SAMPLING_FREQUENCY) == 0:
            ret = 0
        else:
            ret = x / (2*math.sin(2 * np.pi *f0*self.currSample/audioSettings.SAMPLING_FREQUENCY))
        self.currSample += 1
        return ret
    undistort = np.vectorize(undistortFunction, otypes=[float])
    ##############################################################
    
    def callback_wire_in(self,  indata, outdata, frames, time, status):
        # store time between callbacks in ms
        self.avg_rx_time_ms = (float(time.currentTime) - self.time_old)*1000.0
        self.time_old = float(time.currentTime)
        if status:
            logging.error("wire_in: "+str(status))
        # filter audio output (RX out)
        #################
        if configuration.OUT_RX_HEAR_VOICE:
            # TODO: Alternatives 1 and 2 should be the same..but they are not...which one is correct?
            ##################################################
            # ALTERNATIVE 1: first undistort, then fiter
            ############
            # UN-DISTORT voice
            # if configuration.IN_RX_UNDISTORT:
            #    outdata[:frames, audioSettings.DEFAULT_CHANNEL] = self.undistort(self, indata[:frames, audioSettings.DEFAULT_CHANNEL])
                # RX in -> BAND_STOP -> Voice
            #    outdata[:, audioSettings.DEFAULT_CHANNEL], self.zBandStop = signal.sosfilt(self.sos_bandstop, outdata[:, audioSettings.DEFAULT_CHANNEL], zi=self.zBandStop)
            # else:
                # RX in -> BAND_STOP -> Voice
            #    outdata[:, audioSettings.DEFAULT_CHANNEL], self.zBandStop = signal.sosfilt(self.sos_bandstop, indata[:, audioSettings.DEFAULT_CHANNEL], zi=self.zBandStop)
            # ALTERNATIVE 2: first filter, then undistort
            ############
            # RX in -> BAND_STOP -> Voice
            outdata[:, audioSettings.DEFAULT_CHANNEL], self.zBandStop = signal.sosfilt(self.sos_bandstop, indata[:, audioSettings.DEFAULT_CHANNEL], zi=self.zBandStop)
            # remove RX carrier
            if audioSettings.REMOVE_RX_CARRIER:
                outdata[:, audioSettings.DEFAULT_CHANNEL], self.zNotch = signal.sosfilt(self.sos_notch, outdata[:, audioSettings.DEFAULT_CHANNEL], zi=self.zNotch)
            # UN-DISTORT voice
            if configuration.IN_RX_UNDISTORT:
                outdata[:frames, audioSettings.DEFAULT_CHANNEL] = self.undistort(self, outdata[:frames, audioSettings.DEFAULT_CHANNEL])
        else:
            # filter coding-range (remove left and right frequencies with Voice content)
            # RX in -> BAND_PASS -> Code
            outdata[:, audioSettings.DEFAULT_CHANNEL], self.z = signal.sosfilt(self.sos_bandpass, indata[:, audioSettings.DEFAULT_CHANNEL], zi=self.z)
        # pass input audio to decoder
        ################
        ### if self.transmit_on_ref[0] == False: # half-duplex communication
        # this is a BLOCKING call
        self.qin.put(indata[:, audioSettings.DEFAULT_CHANNEL])
           
    def callback_rx_in(self,  indata, frames, time, status):
        # TODO: BUT: time is always zero, why? HW-Bug???
        ##############################
        # store time between callbacks in ms
        self.avg_rx_time_ms = (float(time.currentTime) - self.time_old)*1000.0
        self.time_old = float(time.currentTime)
        if status:
            logging.error("rx_in: "+str(status))
        # pass input audio to decoder
        ################
        ### if self.transmit_on_ref[0] == False: # half-duplex communication
        # this is a BLOCKING call
        self.qin.put(indata[:, audioSettings.DEFAULT_CHANNEL])
    
    # If START is correctly detected, then
    # return sample position of field START (*** WARNING: this is RELATIVE to argument sample_buffer but the caller may pass another buffer with a different buffer offset !!!).
    # If available, telegram contents starting and including ADDRESS,etc. are already copied to permanent bitarray buffer.
    # NOTE: the algorithm is:
    #            rough-scan to detect "best" bit-gap in marker = START, coding with threshold between FFT(one) and FFT(zero), but first we find marker = last-PREAMBLE-byte followed by START
    #            fine-scan to detect START at approx. position found in rough-scan (moving left to right), considering best result given by "maximum minimum gap" between FFT(one) and FFT(zero) in any START-bit.
    #            Markers (PRE+START for rough scan and START for fine scan) shall be checked every time, otherwise we may be considering good gaps which belong to shifted and incorrect samples!
    #            Note that all bits in buffer will only be used in the especial case where START is found at fine-scan-step == 0, otherwise we always discard the last bit, because due to the offset it is "cut".
    #            In that case we will have some rest_samples which will be joined togeher with samples input to putInBitArrayBuffer() in the following call, in order to restore the "cut-bit".
    '''
        Example with bit-width = 40 samples
                          rough-scan-step = 10
                          fine-scan-step = 1
                          
                 ---------------------
                |      |      |      |      |
                 ---------------------
                       10    20    30           (3 rough steps,
                                                     best found in 30)
                              ^             ^
                              |              |
                               \________/
                                    (20 fine scans around 30,
                                    between 20 and 40,
                                    best found in 33)
    # '''
    ################################################################################################################
    def getStartSamplePosition(self, sample_buffer):
        # return value (default is error)
        startSamplePosition = -1 
        # gap variables
        max_min_rough_gap = 0
        startSamplePositionRough = 0
        # bit array
        # we subtract one bit because in 39 out of 40 cases we will always have an offset and therefore have a cut-bit which needs to be discarded
        # in the case where offset = 0, that is, where PRE+START are found at sample zero, we shall consider the last bit again
        # but that is no longer relevant for bits[] becuase in that case what matters is tel_bits[], and its bits are re-calculated based e.g. on len(sample_buffer),
        # therefore, not losing any bit.
        NR_OF_BITS = int(len(sample_buffer)/audioSettings.LEN_BIT_ONE) - 1
        # TODO: pre-allocate memory and set to zero here if necessary
        bits = bitarray(NR_OF_BITS)
        # search marker = last PREAMBLE byte followed by START in rough scan over complete buffer
        # PRE+START shall be detectable already in at least one of these "rough" steps
        ############################################
        for offset in range(self.BIG_STEP, self.BIG_SCAN_ROUNDS*self.BIG_STEP, self.BIG_STEP):
            logging.debug("Rough scan on PRE+START with offset = " + str(offset) + " on nr. of bits = " + str(NR_OF_BITS))
            # scan complete bit-stream with offset (rough scan)
            # coding criteria: maximum of FFT(FREQ_ONE) and FFT(FREQ_ZERO)
            #########################################
            for i in range(NR_OF_BITS):
                ffty = rfft(sample_buffer[offset + i*audioSettings.LEN_BIT_ONE:offset + (i+1)*audioSettings.LEN_BIT_ONE])
                absfft = 2.0 * abs(ffty[:audioSettings.LEN_BIT_ONE//2])/audioSettings.LEN_BIT_ONE
                # code bit according to FFT threshold
                if absfft[audioSettings.BIN_FREQUENCY_ONE_FINE] > absfft[audioSettings.BIN_FREQUENCY_ZERO_FINE]:
                    bits[i] = True
                else:
                    bits[i] = False
            logging.debug("Bit stream in buffer with offset = " + str(offset))
            logging.debug(bits)
            # search start of frame by detecting pattern/marker in bits = last PREAMBLE-byte followed by START
            iter = bits.itersearch(LAST_PREAMBLE_BYTE_AND_START_BITS)
            startPosition = -1
            for markerPosition in iter:
                startPosition = markerPosition + 8 # +8 because we also searched for last byte of preamble..
             # found PRE+START in complete bit-stream?
             #########################
            if startPosition != -1:
                # translate startPosition to absolute samples coordinate system:
                startSamplePositionRoughTemp = startPosition*audioSettings.LEN_BIT_ONE + offset
                min_rough_gap = 999999
                # TODO: average all gaps instead?
                # determine GAPs of START considering offset (rough step)
                ##################################
                for i in range(audioSettings.START_LEN_BYTES*8):
                    ffty = rfft(sample_buffer[startSamplePositionRoughTemp + i*audioSettings.LEN_BIT_ONE:startSamplePositionRoughTemp + (i + 1)*audioSettings.LEN_BIT_ONE])
                    absfft = 2.0 * abs(ffty[:audioSettings.LEN_BIT_ONE//2])/audioSettings.LEN_BIT_ONE
                    diff_rough = abs(absfft[audioSettings.BIN_FREQUENCY_ONE_FINE] - absfft[audioSettings.BIN_FREQUENCY_ZERO_FINE])
                    # we search for minimum gap between ONE and ZERO in each of the bits in START
                    if diff_rough < min_rough_gap:
                        min_rough_gap = diff_rough
                # we search for maximum min_gap in BIG_SCAN_ROUNDS
                if min_rough_gap > max_min_rough_gap:
                        max_min_rough_gap = min_rough_gap
                        startSamplePositionRough = startSamplePositionRoughTemp
            # found best PRE+START in rough scan?
            ######################
            if (offset == (self.BIG_SCAN_ROUNDS - 1)*self.BIG_STEP):
                if max_min_rough_gap != 0:
                    logging.debug("Best START at best worst-case rough_gap = " + str(max_min_rough_gap))
                    logging.debug("               startSamplePositionRough = " + str(startSamplePositionRough))
                    # gap
                    max_min_gap = 0
                    diff = 0
                    # bitarray
                    # TODO: pre-allocate memory instead..
                    bits_start = bitarray(audioSettings.START_LEN_BYTES*8)
                    # fine scan on detected START with an accuracy given by SMALL_SCAN_ROUNDS
                    # we scan left and right of startSamplePositionRough.
                    # (from here onwards we don't care about PREAMBLE anymore)
                    for m in range(startSamplePositionRough - self.SMALL_SCAN_ROUNDS//2, startSamplePositionRough + self.SMALL_SCAN_ROUNDS//2, self.SMALL_STEP):
                        min_gap = 999999
                        # TODO: average all gaps instead?
                        # fine scan bits of START considerung:
                        # offset (rough step) and m (small step)
                        # coding criteria: maximum of minimum gaps between FFT(FREQ_ONE) and FFT(FREQ_ZERO)
                        #####################################################
                        for i in range(audioSettings.START_LEN_BYTES*8):
                            ffty = rfft(sample_buffer[m + i*audioSettings.LEN_BIT_ONE:m + (i + 1)*audioSettings.LEN_BIT_ONE])
                            absfft = 2.0 * abs(ffty[:audioSettings.LEN_BIT_ONE//2])/audioSettings.LEN_BIT_ONE
                            # code bit according to FFT threshold
                            if absfft[audioSettings.BIN_FREQUENCY_ONE_FINE] > absfft[audioSettings.BIN_FREQUENCY_ZERO_FINE]:
                                bits_start[i] = True
                            else:
                                bits_start[i] = False
                            # gaps
                            diff = abs(absfft[audioSettings.BIN_FREQUENCY_ONE_FINE] - absfft[audioSettings.BIN_FREQUENCY_ZERO_FINE])
                            # we search for minimum gap between ONE and ZERO in each of the bits in START
                            if diff < min_gap:
                                min_gap = diff
                        logging.debug("Bit stream in approx. START with fine scan step sample = " + str(m))
                        logging.debug(bits_start)
                        # search start of frame by detecting pattern/marker in bits = last PREAMBLE-byte followed by START
                        iter = bits_start.itersearch(START_BITS)
                        startPosition = -1
                        for markerPosition in iter:
                            startPosition = markerPosition
                        # found START marker?
                        if startPosition == 0:
                            # we search for maximum min_gap in SMALL_SCAN_ROUNDS
                            if min_gap > max_min_gap:
                                max_min_gap = min_gap
                                # return value
                                startSamplePosition = m
                        # found best START in fine scan?
                        ###################
                        if m == startSamplePositionRough + self.SMALL_SCAN_ROUNDS//2 - self.SMALL_STEP:
                            if max_min_gap != 0:
                                # update RX volume for visualization
                                # RX volume based on signal coding START which contains both ones and zeros in the same amount
                                self.updateRxVolume(sample_buffer[startSamplePosition:startSamplePosition + audioSettings.START_LEN_SAMPLES])
                                # log
                                logging.debug("Best START at best worst-case fine gap = " + str(max_min_gap))
                                logging.debug("               bit start position fine = " + str(startPosition))
                                logging.debug("              startSamplePosition fine = " + str(startSamplePosition))
                                # calculate telegram bits using best result, beginning with ADDRESS
                                #######################################
                                sample_pos_address = startSamplePosition + audioSettings.START_LEN_SAMPLES
                                #######################################
                                #######################################
                                # WORKAROUND: we need to add 3 to sample_pos_address
                                # TODO: find out why we need this
                                # TODO: find out if we are losing/discarding the last bit in the stream (?)
                                sample_pos_address = sample_pos_address + 3
                                #######################################
                                #######################################
                                # final values
                                ########
                                rest_samples = (len(sample_buffer) - sample_pos_address)%audioSettings.LEN_BIT_ONE
                                BITS_FROM_ADDRESS = (len(sample_buffer) - sample_pos_address - rest_samples)//audioSettings.LEN_BIT_ONE
                                ########################################################
                                # WORKAROND not working: sometimes we obtain BITS_FROM_ADDRESS = -1 so we set it to zero in such case
                                # TODO: see why this happens
                                if BITS_FROM_ADDRESS < 0:
                                    logging.error("ERROR: BITS_FROM_ADDRESS = " + str(BITS_FROM_ADDRESS)) #  + ", we set it to zero.")
                                    ### BITS_FROM_ADDRESS = 0
                                    #######
                                    return -1
                                    #######
                                ########################################################
                                logging.debug("sample_pos_address = "+str(sample_pos_address))
                                logging.debug("nr. of rest samples = "+str(rest_samples))
                                logging.debug("BITS_FROM_ADDRESS = "+str(BITS_FROM_ADDRESS))
                                # allocate memory for tel_bits
                                # TODO: pre-allocate fix-memory instead and set to zero here if necessary
                                tel_bits = bitarray(BITS_FROM_ADDRESS)
                                # final scan with BEST found position
                                for i in range(BITS_FROM_ADDRESS):
                                    ffty = rfft(sample_buffer[sample_pos_address + i*audioSettings.LEN_BIT_ONE:sample_pos_address + (i+1)*audioSettings.LEN_BIT_ONE])
                                    absfft = 2.0 * abs(ffty[:audioSettings.LEN_BIT_ONE//2])/audioSettings.LEN_BIT_ONE
                                    # code bit according to FFT threshold
                                    if absfft[audioSettings.BIN_FREQUENCY_ONE_FINE] > absfft[audioSettings.BIN_FREQUENCY_ZERO_FINE]:
                                        # now that we stopped scanning with offsets which may lead to insufficient signal-levels we need to check if we actually have a strong enough signal
                                        if absfft[audioSettings.BIN_FREQUENCY_ONE_FINE] > audioSettings.FFT_DETECTION_LEVEL:
                                            tel_bits[i] = True
                                        else:
                                            logging.error("ERROR: START not found, bit ONE too weak with level = " + str(absfft[audioSettings.BIN_FREQUENCY_ONE_FINE]))
                                            # return with error
                                            return -1
                                    else:
                                        # now that we stopped scanning with offsets which may lead to insufficient signal-levels we need to check if we actually have a strong enough signal
                                        if absfft[audioSettings.BIN_FREQUENCY_ZERO_FINE] > audioSettings.FFT_DETECTION_LEVEL:
                                            tel_bits[i] = False
                                        else:
                                            logging.error("ERROR: START not found, bit ZERO too weak with level = " + str(absfft[audioSettings.BIN_FREQUENCY_ZERO_FINE]))
                                            # return with error
                                            return -1
                                # init variable
                                self.telegram.decodedDataBytes = 0
                                # store bits of telegram part
                                self.telegram_bits_start_pos = 0
                                self.telegram_bits_end_pos = BITS_FROM_ADDRESS # which is = len(tel_bits)
                                self.telegram_bits[self.telegram_bits_start_pos:self.telegram_bits_end_pos] = tel_bits[:]
                                # store last samples
                                '''
                                    If fine_step is NOT zero, then we definitely have a "cut-bit" at the end of the buffer.
                                    Example of a ZERO cut-bit:

                                    rest_samples (17)   first_samples (23)

                                     |   _---_         |  _---_             |
                                     |  /       \        | /       \            |
                                     | /         \        /          \         /|
                                     |             \_  _/            \_    _/ |
                                     |                -  |               -.-    |
                                # '''
                                # TODO: avoid this memory allocation and set to zero here if necessary
                                self.bit_prev = np.array([0.0]*(rest_samples))
                                self.bit_prev[0:rest_samples] = sample_buffer[len(sample_buffer) - rest_samples:]
                                logging.debug("telegram_bits:")
                                logging.debug(self.telegram_bits[self.telegram_bits_start_pos:self.telegram_bits_end_pos])
                                # START detected successfully!
                                # set half-duplex flag
                                if self.receive_on_ref[0]==False:
                                    self.receive_on_timer_event.set()
                            else:
                                logging.error("ERROR: START not found in fine scan.")
                else:
                    logging.error("ERROR: START not found in rough scan.")
        # return of getStartSamplePosition()
        return startSamplePosition
        
    def putInBitArrayBuffer(self, sample_buffer):
        # calculate telegram bits using offset determined by len(self.bit_prev)  (previous rest_samples)
        startSamplePosition = audioSettings.LEN_BIT_ONE - len(self.bit_prev)
        rest_samples = (len(sample_buffer) - startSamplePosition)%audioSettings.LEN_BIT_ONE
        BITS_FROM_TEL_PART = (len(sample_buffer) - startSamplePosition - rest_samples)//audioSettings.LEN_BIT_ONE
        tel_bits = bitarray(BITS_FROM_TEL_PART)
        logging.debug("*** putInBitArrayBuffer():")
        logging.debug("startSamplePosition = "+str(startSamplePosition))
        logging.debug("nr. of rest samples = "+str(rest_samples))
        logging.debug("BITS_FROM_TEL_PART = "+str(BITS_FROM_TEL_PART))
        # scan with found position
        for i in range(BITS_FROM_TEL_PART):
            ffty = rfft(sample_buffer[startSamplePosition + i*audioSettings.LEN_BIT_ONE:startSamplePosition + (i+1)*audioSettings.LEN_BIT_ONE])
            absfft = 2.0 * abs(ffty[:audioSettings.LEN_BIT_ONE//2])/audioSettings.LEN_BIT_ONE
            # code bit according to FFT threshold
            # TODO: if we knew that this is a valid bit inside a "telegram-byte" we shall always check if we have a strong enough signal using FFT_DETECTION_LEVEL.
            #            Because we don't have that information (we should NOT have it at this "abstraction level"?) then we don't check that.
            #            We may have some "noise" after the telegram, which is also decoded...just to be discarded by the telegram-decoder afterwards.
            if absfft[audioSettings.BIN_FREQUENCY_ONE_FINE] > absfft[audioSettings.BIN_FREQUENCY_ZERO_FINE]:
                tel_bits[i] = True
            else:
                tel_bits[i] = False
        # management of cut-bits
        ##############
        '''
            If startSamplePosition is NOT zero, then we have a "cut-bit" at start and end of buffer.
            Example of a ZERO cut-bit:

            rest_samples (17)   first_samples (23)

             |   _---_         |  _---_             |
             |  /       \        | /       \            |
             | /         \        /          \         /|
             |             \_  _/            \_    _/ |
             |                -  |               -.-    |
        # '''
        # recover cut-bit
        ##########
        if startSamplePosition > 0: # this is the same as: if len(self.bit_prev) > 0:
            complete_bit_samples = np.append(self.bit_prev, sample_buffer[:startSamplePosition])
            ffty = rfft(complete_bit_samples)
            absfft = 2.0 * abs(ffty[:audioSettings.LEN_BIT_ONE//2])/audioSettings.LEN_BIT_ONE
            # code bit according to FFT threshold
            bit = False
            if absfft[audioSettings.BIN_FREQUENCY_ONE_FINE] > absfft[audioSettings.BIN_FREQUENCY_ZERO_FINE]:
                bit = True
            logging.debug("cut-bit:")
            logging.debug(bit)
            # copy cut-bit to telegram_bits
            ##################
            self.telegram_bits[self.telegram_bits_end_pos:self.telegram_bits_end_pos+1] = bit
            self.telegram_bits_end_pos += 1
        # now add tel_bits to telegram_bits
        #####################
        self.telegram_bits[self.telegram_bits_end_pos:self.telegram_bits_end_pos+len(tel_bits)] = tel_bits[:]
        self.telegram_bits_end_pos += len(tel_bits)
        # store rest samples
        ############
        # TODO: avoid this memory allocation and set to zero instead if required
        self.bit_prev = np.array([0.0]*(rest_samples))
        self.bit_prev[0:rest_samples] = sample_buffer[len(sample_buffer) - rest_samples:]
        logging.debug("telegram_bits including cut-bit and next part:")
        logging.debug(self.telegram_bits[self.telegram_bits_start_pos:self.telegram_bits_end_pos])
        
    def decodeTelegram(self):
        while (self.telegram_bits_end_pos - self.telegram_bits_start_pos) >= 8:
            if self.decode_state == DECODE_ADDRESS:
                self.telegram.address = ba2int(self.telegram_bits[self.telegram_bits_start_pos:self.telegram_bits_start_pos+audioSettings.START_LEN_BYTES*8])
                self.telegram_bits_start_pos += audioSettings.START_LEN_BYTES*8
                logging.info("ADDRESS = "+str(self.telegram.address))
                # check address
                # TODO: remove this hard-coded check...
                if self.telegram.address == 1:
                     self.decode_state = DECODE_SEQ_NR
                else:
                    # ADDRESS ERROR: we just go back to PREAMBLE search state
                    # the transmitter will re-send on timeout
                    self.parse_state = SEARCH_PREAMBLE
                    # WARNING: always reset sub-state when going back to SEARCH_PREAMBLE
                    self.decode_state = DECODE_ADDRESS
                    # reset half-duplex flag
                    if self.receive_on_ref[0]==True:
                        self.receive_on_timer_event.set()
                    # statistics
                    self.telRxNok += 1
                    logging.error("ADDRESS ERROR, address = "+str(self.telegram.address)+" not expected one = 1")
                    ###################################################
                    # TODO: shall we do something like this to avoid trying to decode wrong data on next call?
                    # self.telegram_bits_start_pos = 0
                    # self.telegram_bits_end_pos = 0
                    ###################################################
                    return # force return, dont delete this line!
            elif self.decode_state == DECODE_SEQ_NR:
                self.telegram.seqNr = ba2int(self.telegram_bits[self.telegram_bits_start_pos:self.telegram_bits_start_pos+audioSettings.SEQ_NR_LEN_BYTES*8])
                self.telegram_bits_start_pos += audioSettings.SEQ_NR_LEN_BYTES*8
                logging.info("SEQ_NR = "+str(self.telegram.seqNr))
                ### if self.rx_state == KEY_END_RECEIVED:
                # check if seqNr ok
                expectedSeqNr = (self.seqNrAck[0] + 1)%255
                # new telegram?
                if self.telegram.seqNr == expectedSeqNr:
                    self.telegram.seqNrRepeated = False
                    # we dont increment seqNrAck yet, we do that when we checked all other fields, especially the CRC
                    self.decode_state = DECODE_SEQ_NR_ACK
                # repeated telegram?
                elif self.telegram.seqNr == self.seqNrAck[0]:
                    # we dont increment or reset seqNr
                    # telegram will be discarded and acknowledged at the end if CRC and other things are ok
                    self.telegram.seqNrRepeated = True
                    self.decode_state = DECODE_SEQ_NR_ACK
                # incorrect seqNrAck
                else:
                    # SEQ ERROR: we just go back to PREAMBLE search state
                    # the transmitter will re-send on timeout
                    self.parse_state = SEARCH_PREAMBLE
                    # WARNING: always reset sub-state when going back to SEARCH_PREAMBLE
                    self.decode_state = DECODE_ADDRESS
                    # reset half-duplex flag
                    if self.receive_on_ref[0]==True:
                        self.receive_on_timer_event.set()
                    # statistics
                    self.telRxNok += 1
                    logging.error("SEQ_NR ERROR, seqNr = "+str(self.telegram.seqNr)+" not expected one = "+str(expectedSeqNr))
                    return # force return # DONT DELETE THIS LINE
                ### else:
                    ### self.decode_state = DECODE_SEQ_NR_ACK
                    ### logging.info("SEQ_NR not yet evaluated!")
            elif self.decode_state == DECODE_SEQ_NR_ACK:
                self.telegram.seqNrAck = ba2int(self.telegram_bits[self.telegram_bits_start_pos:self.telegram_bits_start_pos+audioSettings.SEQ_NR_ACK_LEN_BYTES*8])
                self.telegram_bits_start_pos += audioSettings.SEQ_NR_ACK_LEN_BYTES*8
                logging.info("SEQ_NR_ACK = "+str(self.telegram.seqNrAck))
                self.decode_state = DECODE_COMMAND
            elif self.decode_state == DECODE_COMMAND:
                self.telegram.command = ba2int(self.telegram_bits[self.telegram_bits_start_pos:self.telegram_bits_start_pos+audioSettings.COMMAND_LEN_BYTES*8])
                self.telegram_bits_start_pos += audioSettings.COMMAND_LEN_BYTES*8
                self.decode_state = DECODE_DATA_LEN
                logging.info("COMMAND = "+str(self.telegram.command)+" ("+audioSettings.CMD_STR[self.telegram.command]+")")
            elif self.decode_state == DECODE_DATA_LEN:
                self.telegram.data_length = ba2int(self.telegram_bits[self.telegram_bits_start_pos:self.telegram_bits_start_pos+audioSettings.DATA_SIZE_LEN_BYTES*8])
                self.telegram_bits_start_pos += audioSettings.DATA_SIZE_LEN_BYTES*8
                logging.info("DATA_LEN = "+str(self.telegram.data_length))
                # check against max. possible data length
                if self.telegram.data_length <= audioSettings.DATA_MAX_LEN_BYTES:
                    if self.telegram.data_length > 0:
                        self.decode_state = DECODE_DATA
                    else:
                        self.decode_state = DECODE_END
                # data_length is too large!
                else:
                    # DATA_LEN ERROR: we just go back to PREAMBLE search state
                    # the transmitter will re-send on timeout
                    self.parse_state = SEARCH_PREAMBLE
                    # WARNING: always reset sub-state when going back to SEARCH_PREAMBLE
                    self.decode_state = DECODE_ADDRESS
                    # reset half-duplex flag
                    if self.receive_on_ref[0]==True:
                        self.receive_on_timer_event.set()
                    # statistics
                    self.telRxNok += 1
                    logging.info("DATA_LEN ERROR, length = "+str(self.telegram.data_length)+" shall be <= "+str(audioSettings.DATA_MAX_LEN_BYTES))
                    return # force return # DONT DELETE THIS LINE
            elif self.decode_state == DECODE_DATA:
                self.telegram.data[self.telegram.decodedDataBytes] = ba2int(self.telegram_bits[self.telegram_bits_start_pos:self.telegram_bits_start_pos+8])
                self.telegram_bits_start_pos += 8
                self.telegram.decodedDataBytes += 1
                logging.info("DATA["+str(self.telegram.decodedDataBytes-1)+"] = "+str(self.telegram.data[self.telegram.decodedDataBytes-1]))
                if self.telegram.decodedDataBytes == self.telegram.data_length:
                    self.decode_state = DECODE_END
            elif self.decode_state == DECODE_END:
                self.telegram.end = ba2int(self.telegram_bits[self.telegram_bits_start_pos:self.telegram_bits_start_pos+audioSettings.END_LEN_BYTES*8])
                self.telegram_bits_start_pos += audioSettings.END_LEN_BYTES*8
                logging.info("END = "+str(self.telegram.end))
                # pattern to detect END-byte pattern
                if self.telegram.end == 170: # = 0xAA
                    self.decode_state = DECODE_CHECKSUM
                else:
                    # END ERROR: we just go back to PREAMBLE search state
                    # the transmitter will re-send on timeout
                    self.parse_state = SEARCH_PREAMBLE
                    # WARNING: always reset sub-state when going back to SEARCH_PREAMBLE
                    self.decode_state = DECODE_ADDRESS
                    # reset half-duplex flag
                    if self.receive_on_ref[0]==True:
                        self.receive_on_timer_event.set()
                    # statistics
                    self.telRxNok += 1
                    logging.error("END ERROR, END = "+str(self.telegram.end)+" not expected one = 0xAA")
                    return # dont remove this line!
            elif self.decode_state == DECODE_CHECKSUM:
                self.telegram.checksum = ba2int(self.telegram_bits[self.telegram_bits_start_pos:self.telegram_bits_start_pos+audioSettings.CHECKSUM_LEN_BYTES*8])
                self.telegram_bits_start_pos += audioSettings.CHECKSUM_LEN_BYTES*8
                logging.info("CHECKSUM = "+str(self.telegram.checksum))
                # calculate checksum
                start = 85 # = b"\x55"
                checksum = 0 # = b"\x00" # start value
                checksum = checksum^start
                checksum = checksum^self.telegram.address
                checksum = checksum^self.telegram.seqNr
                checksum = checksum^self.telegram.seqNrAck
                checksum = checksum^self.telegram.command
                checksum = checksum^self.telegram.data_length
                for i in range(self.telegram.decodedDataBytes):
                    checksum = checksum^self.telegram.data[i]
                logging.info("Calculated CHECKSUM = "+str(checksum))
                # is checksum ok?
                if checksum == self.telegram.checksum:
                    # process command
                    ###########
                    masked_command = (self.telegram.command & audioSettings.COMMAND_MASK)
                    # TODO: remove use of resetSeqNrFlags
                    ### resetSeqNrFlags = False
                    if self.telegram.seqNrRepeated == False:
                        # now update seqNrAck (the complete telegram was correct and had an increased seqNr)
                        self.seqNrAck[0] = (self.seqNrAck[0] + 1)%255
                        # statistics
                        self.telRxOk += 1
                        # process commands with increased seqNr
                        ########################
                        if masked_command == audioSettings.COMMAND_CHAT_DATA:
                            # TODO: need to consider "\n" or hyperlink stuff, etc. ?
                            decryptor = self.cipher[0].decryptor()
                            data = decryptor.update(self.telegram.data[:self.telegram.decodedDataBytes]) + decryptor.finalize()
                            unpadder = padding.PKCS7(configuration.PADDING_BITS_LEN).unpadder()
                            unpadded_data = unpadder.update(data)
                            decryptedData = unpadded_data + unpadder.finalize()
                            decryptedData = decryptedData.decode('utf-8')
                            self.inMessageQueue.put(decryptedData)
                            self.inCommStatusQueue.put("RX: DATA")
                            logging.info("Received DATA: "+str(decryptedData))
                        elif masked_command == audioSettings.COMMAND_CHAT_DATA_START:
                            self.part_end_idx = 0
                            # TODO: need to consider "\n" or hyperlink stuff, etc. ?
                            self.data_part[self.part_end_idx:self.part_end_idx + self.telegram.decodedDataBytes] = self.telegram.data[:self.telegram.decodedDataBytes]
                            self.part_end_idx += self.telegram.decodedDataBytes
                            self.inCommStatusQueue.put("RX: Receiving data..")
                            self.inCommStatusQueue.put("RX: DATA START")
                            logging.info("Received DATA START: "+str(self.telegram.data[:self.telegram.decodedDataBytes]))
                        elif masked_command == audioSettings.COMMAND_CHAT_DATA_PART:
                            # TODO: need to consider "\n" or hyperlink stuff, etc. ?
                            self.data_part[self.part_end_idx:self.part_end_idx + self.telegram.decodedDataBytes] = self.telegram.data[:self.telegram.decodedDataBytes]
                            self.part_end_idx += self.telegram.decodedDataBytes
                            self.inCommStatusQueue.put("RX: DATA PART")
                            logging.info("Received DATA PART: "+str(self.telegram.data[:self.telegram.decodedDataBytes]))
                        elif masked_command == audioSettings.COMMAND_CHAT_DATA_END:
                            # TODO: need to consider "\n" or hyperlink stuff, etc. ?
                            self.data_part[self.part_end_idx:self.part_end_idx + self.telegram.decodedDataBytes] = self.telegram.data[:self.telegram.decodedDataBytes]
                            self.part_end_idx += self.telegram.decodedDataBytes
                            decryptor = self.cipher[0].decryptor()
                            data = decryptor.update(self.data_part[0:self.part_end_idx]) + decryptor.finalize()
                            unpadder = padding.PKCS7(configuration.PADDING_BITS_LEN).unpadder()
                            unpadded_data = unpadder.update(data)
                            decryptedData = unpadded_data + unpadder.finalize()
                            decryptedData = decryptedData.decode('utf-8')
                            self.inMessageQueue.put(decryptedData)
                            self.inCommStatusQueue.put("RX: DATA END")
                            logging.info("Received DATA END: "+str(decryptedData))
                        elif masked_command == audioSettings.COMMAND_CALL_REJECTED:
                            self.call_rejected = True
                            self.inCommStatusQueue.put("RX: CALL REJECTED")
                            logging.info("Received CALL REJECTED")
                        elif masked_command == audioSettings.COMMAND_CALL_END:
                            # set flag to reset sequence numbers
                            # TODO: check this removal from 2021.02.07-15:24 - remove permanently
                            # we need to ACK already with reset SeqNr because recepient has already reset seqNr too..
                            ### resetSeqNrFlags = True
                            self.seqNrAck[0] = 0
                            self.seqNrAckRx[0] = 0
                            self.seqNrTx[0] = 0
                            # set flag
                            self.call_end = True
                            self.inCommStatusQueue.put("RX: CALL END")
                            logging.info("Received CALL END")
                            logging.info("SeqNrs reset!")
                        elif (masked_command == audioSettings.COMMAND_STARTUP_DATA_COMPLETE):
                            # TODO: add check against reception of retransmissions?
                            decryptor = self.cipher[0].decryptor()
                            data = decryptor.update(self.telegram.data[:self.telegram.decodedDataBytes]) + decryptor.finalize()
                            unpadder = padding.PKCS7(configuration.PADDING_BITS_LEN).unpadder()
                            unpadded_data = unpadder.update(data)
                            self.startup_data.comm_partner = unpadded_data + unpadder.finalize()
                            self.startup_data.comm_partner = self.startup_data.comm_partner.decode('utf-8')
                            self.startup_data_received = True
                            self.inCommStatusQueue.put("RX: STARTUP COMPLETE")
                            logging.info("Received STARTUP_DATA COMPLETE, COMM_PARTNER: "+str(self.startup_data.comm_partner))
                        # elif XXX: TODO: add here processing of other commands..
                        ##################################
                    # process command without increased seqNr
                    #########################
                    elif masked_command == audioSettings.COMMAND_CALL:
                        # TODO: handle reception of retransmissions ?
                        # handle communication token
                        comm_token_partner = int.from_bytes(self.telegram.data[:self.telegram.decodedDataBytes], byteorder='big', signed=False)
                        if comm_token_partner > self.comm_token[0]:
                            self.have_token = False
                        elif comm_token_partner == self.comm_token[0]:
                            self.comm_token[0] = random.randint(0, 255)
                        # set flag
                        self.call = True
                        # statistics
                        self.telRxOk += 1
                        self.inCommStatusQueue.put("RX: CALL")
                        logging.info("Received CALL, we dont trigger ACK in this case..")
                    elif masked_command == audioSettings.COMMAND_CALL_ACCEPTED:
                        if self.rx_state == IDLE:
                            self.rx_state  = CALL_ACCEPTED
                            self.call_accepted = True
                            self.inCommStatusQueue.put("RX: CALL ACCEPTED")
                            logging.info("Received CALL ACCEPTED")
                        else:
                            self.inCommStatusQueue.put("RX: CALL ACCEPTED (rep)")
                            logging.info("Received CALL ACCEPTED again, just ignore it.")
                    elif masked_command == audioSettings.COMMAND_KEY_START:
                        # accept if our call is accepted or if we accept their call...
                        if (self.rx_state == CALL_ACCEPTED) or (self.rx_state == IDLE):
                            self.rx_state = KEY_START_RECEIVED
                            # NOTE: with deep copy we asure we have a copy of the data so we dont use the data when it changes...overwriten by next message
                            #            we do make dynamic allocation thaough, but dont have to manage indexes as we did in self.data_part
                            self.peer_public_key_start = copy.deepcopy(self.telegram.data[:self.telegram.decodedDataBytes])
                            self.key_start_received = True
                            self.inCommStatusQueue.put("RX: KEY START")
                            logging.info("Received KEY START : "+str(self.peer_public_key_start))
                        else:
                            self.inCommStatusQueue.put("RX: KEY START (rep)")
                            logging.info("Received KEY START AGAIN, just ignore it!")
                    elif masked_command == audioSettings.COMMAND_KEY_END:
                        if  self.rx_state == KEY_START_RECEIVED:
                            self.rx_state = KEY_END_RECEIVED
                            # NOTE: with deep copy we asure we have a copy of the data so we dont use the data when it changes...overwriten by next message
                            #            we do make dynamic allocation thaough, but dont have to manage indexes as we did in self.data_part
                            self.peer_public_key_end = copy.deepcopy(self.telegram.data[:self.telegram.decodedDataBytes])
                            # put together peer public key
                            public_key_peer_bytes = self.peer_public_key_start + self.peer_public_key_end
                            print("RX public_key_bytes as bytearray: "+str(public_key_peer_bytes))
                            public_key_peer_bytes = bytes(public_key_peer_bytes)
                            print("RX public_key_bytes as bytes: "+str(public_key_peer_bytes))
                            loaded_public_key_from_peer = x25519.X25519PublicKey.from_public_bytes(public_key_peer_bytes)
                            shared_key =  self.private_key[0].exchange(loaded_public_key_from_peer)
                            # TODO: use a common password here?
                            iv = bytes(b"1234567890123456") # fix to a common value for both sides..
                            # NOTE: possible modes:  CBC, GCM, CFB, CFB8, OFB
                            self.cipher[0] = Cipher(algorithms.AES(shared_key),  modes.CBC(iv))
                            derived_session_key = HKDF(
                                algorithm=hashes.SHA256(),
                                length=32,
                                salt=None,
                                info=b'out-band-verification',
                            ).derive(shared_key)
                            # we create a session code calculated as an integer value based on the derived session key, not very conventional, but it works!
                            self.session_code = str(int(derived_session_key[0]+derived_session_key[1]*16+derived_session_key[2]*32))
                            self.key_end_received = True
                            self.inCommStatusQueue.put("RX: KEY END")
                            logging.info("Received KEY END: "+str(self.peer_public_key_end))
                            logging.info("Complete peer KEY: "+str(public_key_peer_bytes))
                        else:
                            self.inCommStatusQueue.put("RX: KEY END (rep)")
                            logging.info("Received KEY END AGAIN, just ignore it!")
                    elif (masked_command == audioSettings.COMMAND_STARTUP_DATA):
                        # TODO: add check against reception of retransmissions?
                        decryptor = self.cipher[0].decryptor()
                        data = decryptor.update(self.telegram.data[:self.telegram.decodedDataBytes]) + decryptor.finalize()
                        unpadder = padding.PKCS7(configuration.PADDING_BITS_LEN).unpadder()
                        unpadded_data = unpadder.update(data)
                        self.startup_data.comm_partner = unpadded_data + unpadder.finalize()
                        self.startup_data.comm_partner = self.startup_data.comm_partner.decode('utf-8')
                        self.startup_data_received = True
                        self.inCommStatusQueue.put("RX: STARTUP")
                        logging.info("Received STARTUP_DATA, COMM_PARTNER: "+str(self.startup_data.comm_partner))
                    elif self.telegram.command  != audioSettings.COMMAND_TELEGRAM_ACK:
                        # statistics
                        self.telRxOk += 1 # TODO: can this be considered a CORRECT telegram? but it is repeated...hmm..
                        # Telegram really repeated
                        logging.info("Telegram with repeated SeqNr. Discard it BUT Acknowledge it.")
                    # process ACK
                    if (self.telegram.command & audioSettings.ACK_MASK) == audioSettings.COMMAND_TELEGRAM_ACK:
                        self.seqNrAckRx[0] = self.telegram.seqNrAck
                        self.ack_received[0] = True
                        self.ack_received[1] = cProfileTimer()
                        self.inCommStatusQueue.put("") # ("RX:") # clear status..
                        logging.info("Detected ACK with SeqNr = "+str(self.telegram.seqNrAck))
                    # trigger/send ACK
                    if self.telegram.command  != audioSettings.COMMAND_TELEGRAM_ACK:
                        # this list shall contain ALL commands which require ACK - all other commands dont
                        if  (masked_command == audioSettings.COMMAND_CHAT_DATA) or (masked_command == audioSettings.COMMAND_CHAT_DATA_START) or \
                            (masked_command == audioSettings.COMMAND_CHAT_DATA_PART) or (masked_command == audioSettings.COMMAND_CHAT_DATA_END) or \
                            (masked_command == audioSettings.COMMAND_CALL_REJECTED) or (masked_command == audioSettings.COMMAND_CALL_END) or \
                            (masked_command == audioSettings.COMMAND_STARTUP_DATA_COMPLETE):
                            self.send_ack[0] = True
                            self.inCommStatusQueue.put("") # ("RX:")
                            logging.info("Trigger Send ACK")
                    self.parse_state = SEARCH_PREAMBLE
                    # WARNING: always reset sub-state when going back to SEARCH_PREAMBLE
                    self.decode_state = DECODE_ADDRESS
                    # reset half-duplex flag
                    if self.receive_on_ref[0]==True:
                        self.receive_on_timer_event.set()
                    # do this "after" we reset receive_on_ref (RX -> OFF)
                    ''' TODO: remove use of resetSeqNrFlags
                    if resetSeqNrFlags:
                        resetSeqNrFlags = False
                        # give enough time to send ACK to other side so it also resets its SeqNrs
                        # optimistic delay without retransmission
                        # TODO: consider making sure to get an ACK to this notification or implement life-signs
                        delay = audioSettings.TELEGRAM_MAX_LEN_SECONDS + audioSettings.CHANNEL_DELAY_SEC
                        time.sleep(delay)
                        self.seqNrAck[0] = 0
                        self.seqNrAckRx[0] = 0
                        self.seqNrTx[0] = 0
                        logging.info("SeqNrs reset!")
                    # '''
                    return # dont remove this line!
                else:
                    # CHECKSUM ERROR: we just go back to PREAMBLE search state
                    # the transmitter will re-send on timeout
                    self.parse_state = SEARCH_PREAMBLE
                    # WARNING: always reset sub-state when going back to SEARCH_PREAMBLE
                    self.decode_state = DECODE_ADDRESS
                    # reset half-duplex flag
                    if self.receive_on_ref[0]==True:
                        self.receive_on_timer_event.set()
                    # statistics
                    self.telRxNok += 1
                    logging.error("CHECKSUM ERROR, checksum = "+str(self.telegram.checksum)+" not expected one = "+str(checksum))
                    return # force return (in case we decide to add further elifs later) # DONT DELETE THIS LINE
        return
    
    def thread_decode(self, frame):
        # log to shell
        logging.info("enter thread_decode")
        # main loop of thread
        ############
        while self.stream_on[0]:
            try:
                # BLOCKING call on queue to obtain audio data from RX in
                # TODO: define dataComplete as module variable to avoid memory allocation?
                dataComplete = self.qin.get() # .get_nowait()
                # decode / analyze / plot in chunks of size audioSettings.N = TELEGRAM_PREAMBLE_LEN_SAMPLES
                rounds = int(audioSettings.AUDIO_RX_CHUNK_SAMPLES_LEN/audioSettings.N)
                # flag
                preamble_round = -100 # NOTE: do NOT initilaize to -1
                # rounds, split audio-chunk 
                ###############
                for m in range(rounds):
                    # indexes for part of dataComplete[]
                    start_of_round = audioSettings.N*m
                    end_of_round = audioSettings.N*(m+1)
                    # DETECT PREAMBLE
                    #############
                    # TODO: shall we better use a data length which is 2^x  to calculate FFT ?
                    ##########################################
                    # authors of Numpy recommend using FFT from ScyPy instead
                    # Windowing the signal with a dedicated window function helps mitigate spectral leakage,
                    # but tests show better results without windowing...probably because of the reduced samples size.
                    # rfft for real input is faster than fft
                    ### w = blackman(audioSettings.N)
                    ffty = rfft(dataComplete[start_of_round:end_of_round]) ### *w)
                    absfft = 2.0 * abs(ffty[:audioSettings.N//2])/audioSettings.N
                    # parse audio-chunk-part
                    ##############
                    if (self.parse_state == SEARCH_PREAMBLE) or (self.parse_state == SEARCH_START):
                        preamble_fft = absfft[audioSettings.BIN_FREQUENCY_ONE]
                        logging.debug(str(preamble_fft))
                        # detected PREAMBLE
                        #############
                        if preamble_fft>audioSettings.FFT_DETECTION_LEVEL:
                            # set flag
                            preamble_round = m
                            logging.info("Detected PREAMBLE in round "+str(m))
                            # state transition (or set same state again unnecessarily - we avoid having an if statement)
                            self.parse_state = SEARCH_START
                            # search START of frame
                            # we always take some samples from previous round in case START was cut
                            # for first round with m==0 we need to recover previous samples from self.data_prev[]
                            startSamplePosition = -1
                            if m == 0:
                                self.data_prev[self.PREVIOUS_SAMPLES:(self.PREVIOUS_SAMPLES + audioSettings.N)] = dataComplete[start_of_round:end_of_round]
                                startSamplePosition = self.getStartSamplePosition(self.data_prev[0:(self.PREVIOUS_SAMPLES + audioSettings.N)])
                            else:
                                startSamplePosition = self.getStartSamplePosition(dataComplete[audioSettings.N*m - self.PREVIOUS_SAMPLES:audioSettings.N*(m+1)])
                            # found START?
                            if startSamplePosition >= 0:
                                logging.info("Detected START in round "+str(m)+" at position "+str(startSamplePosition))
                                self.parse_state = DECODE_FRAME
                                # DECODE telegram
                                ############
                                self.decodeTelegram()
                            elif m == (rounds-1):
                                # START is probably not yet received or was cut...
                                # store last samples of this last part, big enough to allocate last PREAMBLE byte and START
                                # NOTE: data is already filtered..
                                self.data_prev[0:self.PREVIOUS_SAMPLES] = dataComplete[audioSettings.N*(m+1) - self.PREVIOUS_SAMPLES:audioSettings.N*(m+1)]
                        # no PREAMBLE detected
                        ###############
                        else:
                            # detected PREAMBLE in previous round of this audio chunk ?
                            if preamble_round == (m-1):
                                # search START of frame
                                startSamplePosition = self.getStartSamplePosition(dataComplete[audioSettings.N*m - self.PREVIOUS_SAMPLES:audioSettings.N*(m+1)])
                                # found START in first part?
                                if startSamplePosition >= 0:
                                    logging.info("Detected START in round "+str(m)+" at position "+str(startSamplePosition))
                                    self.parse_state = DECODE_FRAME
                                    # DECODE telegram
                                    ############
                                    self.decodeTelegram()
                                else:
                                    # START not found although PREAMBLE was found in previous round
                                    # TODO: discard silently when no START found! ...and comment this:
                                    logging.info("START NOT found in round "+str(m))
                                    # transition on error event back to initial state
                                    self.parse_state = SEARCH_PREAMBLE
                                    # WARNING: always reset sub-state when going back to SEARCH_PREAMBLE
                                    self.decode_state = DECODE_ADDRESS
                            # detected PREAMBLE in last round of previous audio chunk?
                            elif self.parse_state == SEARCH_START:
                                # TODO: remove this check if error message does NOT appear. It shall always be zero..but we actually GOT into the else: during tests...hmm..
                                # 2021.01.06 started evaluation...
                                # this shall be an assert instead...?
                                if m == 0:
                                    # search START of frame
                                    self.data_prev[self.PREVIOUS_SAMPLES:(self.PREVIOUS_SAMPLES + audioSettings.N)] = dataComplete[start_of_round:end_of_round] # dataComplete[0:audioSettings.N]
                                    startSamplePosition = self.getStartSamplePosition(self.data_prev[0:(self.PREVIOUS_SAMPLES + audioSettings.N)])
                                    if startSamplePosition >= 0:
                                        logging.info("Detected START in round "+str(m)+" at position "+str(startSamplePosition))
                                        self.parse_state = DECODE_FRAME
                                        # DECODE telegram
                                        ############
                                        self.decodeTelegram()
                                    else:
                                        # START not found although PREAMBLE was found in last round of previous audio-chunk
                                        # TODO: discard silently when no START found! ...and comment this:
                                        logging.info("START NOT found in round "+str(m))
                                        # transition on error event back to initial state
                                        self.parse_state = SEARCH_PREAMBLE
                                        # WARNING: always reset sub-state when going back to SEARCH_PREAMBLE
                                        self.decode_state = DECODE_ADDRESS
                                else:
                                    # during "evaluation period" we want to show a POPUP if this condition occurs...see TODO above to remove if-else condition for m==0
                                    self.inMessageQueue.put("ERROR: PREAMBLE found BUT then STRANGE CONDITION?!\n")
                                    logging.error("ERROR: PREAMBLE found BUT then STRANGE CONDITION?!\n")
                                    # just to make sure..
                                    self.parse_state = SEARCH_PREAMBLE
                                    # WARNING: always reset sub-state when going back to SEARCH_PREAMBLE
                                    self.decode_state = DECODE_ADDRESS
                            else:
                                # no preamble found while in state SEARCH_PREAMBLE...kepp on searching...
                                pass
                    elif self.parse_state == DECODE_FRAME:
                    ############################
                        # put sample data in bit array, it will be decoded as well
                        self.putInBitArrayBuffer(dataComplete[start_of_round:end_of_round])
                        # DECODE telegram
                        # we check state again, putInBitArrayBuffer() may have forced  state change due to decoding errors...
                        #########################################################
                        if self.parse_state == DECODE_FRAME:
                            self.decodeTelegram()
                    # pass data to plot
                    ###########
                    if configuration.SHOW_PLOT:
                        ############################
                        # filter coding-range (remove left and right frequencies with Voice content)
                        ###########################################
                        if configuration.PLOT_CODE_ONLY:
                            dataComplete[start_of_round:end_of_round], self.z = signal.sosfilt(self.sos_bandpass, dataComplete[start_of_round:end_of_round], zi=self.z)
                        # plot FFT or time signal
                        if configuration.PLOT_FFT:
                            # TODO: shall we better use a data length which is 2^x  to calculate FFT ?
                            ##########################################
                            # authors of Numpy recommend using FFT from ScyPy instead
                            # Windowing the signal with a dedicated window function helps mitigate spectral leakage,
                            # but tests show better results without windowing, probably due to the reduced number of samples.
                            # rfft for real input is faster than fft
                            if configuration.PLOT_CODE_ONLY:
                                ### w = blackman(audioSettings.N)
                                ffty = rfft(dataComplete[start_of_round:end_of_round]) ###*w)
                                absfft = 2.0 * abs(ffty[:audioSettings.N//2])/audioSettings.N
                            # downsample FFT of data before plotting
                            absfft_downsampled = absfft[::audioSettings.DOWNSAMPLE]
                            self.qplot.put(absfft_downsampled)
                        else:
                            # downsample data before plotting
                            data_downsampled = dataComplete[start_of_round:end_of_round][::audioSettings.DOWNSAMPLE]
                            self.qplot.put(data_downsampled)
            except Exception as e:
                logging.error("Exception in AudioReceiver.thread_decode():"+str(e)+"\n")
                # TEST: continue despite Exception...???
                ###break
        logging.info("leave thread AudioReceiver.thread_decode()..")
        
    # to visualize RX volume
    def updateRxVolume(self, data):
        tempMax = np.amax(data)*100.0
        if math.isnan(tempMax ) == False:
            tempMax = int(np.amax(data)*100.0)
        else:
            tempMax = 0
        # ignore 0 and avoid max. value 
        if (tempMax > 1) and (tempMax < 100):
            self.avg_in_amplitude_percent = tempMax
        
    # flag timer thread for half-duplex communication
    def thread_receive_on_timer(self, frame):
        logging.info("enter thread_receive_on_timer")
        # main loop of thread
        # NOTE: as this thread instance is inside the AudioReceiver object, we don't need to evaluate the flag stream_on[0]
        while True: # self.stream_on[0]:
            try:
                # BLOCKING wait on event
                self.receive_on_timer_event.clear()
                self.receive_on_timer_event.wait()
                # half-duplex
                # TODO: need this check?
                if self.receive_on_ref[0] == False:
                    self.receive_on_ref[0] = True
                    logging.info("RX ON")
                    # BLOCKING wait on event again to receive end of telegram...or timeout after maximum value
                    self.receive_on_timer_event.clear()
                    event_set  = self.receive_on_timer_event.wait(audioSettings.CHANNEL_DELAY_SEC + audioSettings.TELEGRAM_MAX_LEN_SAMPLES/audioSettings.SAMPLING_FREQUENCY)
                    if event_set:
                        # complete telegram has been received, we reset flag
                        self.receive_on_ref[0] = False
                    else:
                        # error condition, got here due to timeout
                        self.receive_on_ref[0] = False
                        logging.warning("WARNING: reset half-duplex flag receive_on_ref due to timeout.")
                    logging.info("RX OFF")
            except Exception as e:
                logging.error("EXCEPTION: in AudioReceiver.thread_receive_on_timer():"+str(e)+"\n")
                # TEST: continue despite Exception...???
                ###break
        logging.info("leave thread AudioReceiver.thread_receive_on_timer()..")
            
    def getRxTimeMs(self):
        return self.avg_rx_time_ms
        
    def getAvgInAmplitudePercent(self):
        return self.avg_in_amplitude_percent
        
    def isCall(self):
        ret = self.call
        # autoreset flag - don't evaluate in order to save time
        self.call = False
        return ret
        
    def isCallEnd(self):
        ret = self.call_end
        # autoreset flag - don't evaluate in order to save time
        self.call_end = False
        return ret
        
    def isCallAccepted(self):
        ret = self.call_accepted
        # autoreset flag - don't evaluate in order to save time
        self.call_accepted = False
        return ret
        
    def isCallRejected(self):
        ret = self.call_rejected
        # autoreset flag - don't evaluate in order to save time
        self.call_rejected = False
        return ret
        
    def isKeyStartReceived(self):
        ret = self.key_start_received
        # autoreset flag - don't evaluate in order to save time
        self.key_start_received = False
        return ret
        
    def isKeyEndReceived(self):
        ret = self.key_end_received
        # autoreset flag - don't evaluate in order to save time
        self.key_end_received = False
        return ret
        
    def isStartupDataReceived(self):
        ret = self.startup_data_received
        # autoreset flag - don't evaluate in order to save time
        self.startup_data_received = False
        return ret
        
    def getTelRxOk(self):
        return self.telRxOk
    
    def getTelRxNok(self):
        return self.telRxNok
        
    def getSessionCode(self):
        return self.session_code
        
    def getStartupData(self):
        return self.startup_data
        
    def haveToken(self):
        return self.have_token
        
    def purge(self):
        # reset variables, flags used during Startup, etc. 
        # TODO: with use of rx_state we dont need to reset/clear these variables anymore...check..
        self.comm_token[0] = random.randint(0, 255)
        self.have_token = True # assume for now we have the token
        self.session_code = "" # also used as startup "flag"
        self.peer_public_key_start = bytearray(0) # also used as startup "flag"
        self.telRxOk = 0 # comment to keep old statistics
        self.telRxNok = 0 # comment to keep old statistics
        self.rx_state = IDLE
        # no method .clear() available..so:
        self.inMessageQueue = queue.Queue()
        # these are also purged in TX, but ok...
        self.seqNrAck[0] = 0
        self.seqNrAckRx[0] = 0
        self.seqNrTx[0] = 0
        self.call_end = False
        # TODO: reset here also other flags, counters, etc.???
        '''
        self.call = False
        self.call_accepted = False
        self.call_rejected = False
        # '''
        self.inCommStatusQueue.put("RX: purged")
        logging.info("RX purge")

        

        

            
            

