# -*- coding: utf-8 -*-

###################################################################
###################################################################
# The maximum supported scenario is "m telegrams with each n chunks"
#
# with: m = MAX_NR_OF_TELEGRAMS_IN_PARALLEL
#         n = MAX_NR_OF_CHUNKS_PER_TELEGRAM
#
# But under normal conditions we will transmit less than m telegrams each
# having different number of chunks < n
#
#    ---- ----- -----    ----- -----          ----- ----- -----    ----- -----          ----- ----- -----    ----- -----
#   |     |      |     |...|      |      | ----- |      |      |      |...|     |      | ----- |      |      |     |...|      |      |
#    ---- ----- -----    ----- -----          ----- ----- -----    ----- -----          ----- ----- -----    ----- -----
#
#     c0   c1    c2       cn-2  cn-1            c0    c1    c2              cn1            c0    c1    c2             cn-1          (chunks)
#
#           telegram 0                                    telegram 1                                   telegram m-1                   (telegrams)
#
# Each chunk has AUDIO_TX_CHUNK_SAMPLES_LEN samples per chunk
# and audio data is a numpy array of the form
# [[0.1]
#  [0.2]
#  ..
#  [0.4]]
# with shape (AUDIO_TX_CHUNK_SAMPLES_LEN, 1)
#
# Note that we work with only one audio channel (mono) because teleconference systems are mono.
# The default audio channel is the left channel (e.g. for skype microphone).
###################################################################
###################################################################


import numpy as np
import audioSettings # needs to import itself !?
import os
import configuration
import configparser


#############################################
# NOTE:  capacity vs. robustness
#           *** for LOW OVERHEAD => HIGHER PAYLOAD, e.g. with:
# TELEGRAM_MAX_LEN_BYTES = 1920
# and
# MAX_NR_OF_CHUNKS_PER_TELEGRAM = 60
# we have 32 bytes per audio chunk TX in 200ms = blocksize of stream.
#
#           *** for ROBUSTNESS IN NOISY CHANNEL => LOWER PAYLOAD, e.g. with:
# TELEGRAM_MAX_LEN_BYTES = 256 # WARNING: not set over GUI, check value in config.ini as well
# and
# MAX_NR_OF_CHUNKS_PER_TELEGRAM = 8
# we have AGAIN 32 bytes per audio chunk TX in 200ms = blocksize of stream.
# but we retransmit smaller telegrams that actually "go through" thus incrasing performance
# in case of nosiy channels.
# The same is also valid for VoIP channels with low QoS or smartphone devices using 
# COMMUNICATION_MODE instead of CALL_MODE.
#############################################

#############################################################
# only these parameters can be set in the GUI or in the .ini file (the other definitions are derived from them)
CURRENT_FREQUENCY_CHANNEL = 5 # be aware of ALLOWED_FREQUENCY_CHANNELS
SAMPLING_FREQUENCY = int(48000) # int(44100) # TODO: get this from config or interface...
AMPLITUDE = float(0.5)
FFT_DETECTION_LEVEL = float(0.001)
# we assume channel maximum delay in one direction
# NOTE: measure this and adapt as corresponding.
#            TODO: Or even better, do it automatically and adapt the channel delay value used below...
CHANNEL_DELAY_TRIFA_MS = 1500
CHANNEL_DELAY_CABLE_MS = 350 # 500 # 250
CHANNEL_DELAY_MS = CHANNEL_DELAY_CABLE_MS
CHANNEL_DELAY_SEC = float(CHANNEL_DELAY_MS/1000.0)
# max. resends
MAX_RESENDS = 3
# detect using Grötzel algorithm NOT USED FOR NOW..
DETECT_USING_GROETZEL = False
# value of "carrier" frequency determined during tests. 200Hz and 400Hz work also but nr. of samples not round.
CARRIER_FREQUENCY_HZ = 375
CARRIER_AMPLITUDE = 0.01 # 0.01 # 0.05
# carrier on/off
ADD_CARRIER = False
REMOVE_RX_CARRIER = True
#############################################################

'''
Protocol:
######

    Preamble - Header - Data - Footer - Terminator
    ################################
    
    -------------------------------- ---- ---- ----- ---- ----   ---- ---- ------------------------
   |              PRE                         | S   |  A | SN | SNA | CMD |SZ |  d0 ... dn | E  |CHK| TERM |
    -------------------------------- ---- ---- ----- ---- ----   ---- ---- ------------------------

                    4                             1      1      1     1      1       1     n (mx242)   1     1      1
    Field   nr.bytes    description
    ###################
    Preamble	4 -20	Synchronization and Carrier Detect
    Start byte	1 	0xAA or 0x00 or 0xF2 or some useful information instead like src-address???
    Address		1 	0:chat, 1:console, 2-255:files (here we could also TX source-address e.g. Gateway or Socket or is this already covered by Mode ?!)
    Sequence Number 1   0-255   "own" Seq. Nr.  (TX)
    Seq. Nr. ACK    1   0-255   "remote" Seq. Nr. (RX)
    Command		1	command	
    Nrofdata bytes	1	size of data
    Data		0-255	data
    End byte	1 	0x55 or 0xAA or 0xFF or 0xF2 or ~0xF2=0x0D or some useful information instead???
    Checksum	1	XOR of all bytes from Start Byte to Last Byte of Data
    Terminator  1   Workaround to NOT distort Checksum bits due to signal-deformation (or echoes?)
'''
# TODO: if we need to define TELEGRAM_MAX_LEN_BYTES > 256, then we need to increase size of ADDRESS field e.g. to 2 bytes.
#            We may need that in order to increase capacity by reducing ACKs and corresponding delays BUT,
#            the precondition is that the complete bit-stream can go through the channel without disturbances (high-quality channel).
#            Smaller telegrams have more overhead BUT they are more robust against low-quality communication channels.
TELEGRAM_MAX_LEN_BYTES = 32 # 32 # 256 # 512 # 1024 # 2048 # 4096 ...
# TELEGRAM_PREAMBLE_LEN_BYTES can be 2,4,8,16,...,max= TELEGRAM_MAX_LEN_BYTES/MAX_NR_OF_CHUNKS_PER_TELEGRAM
# that is, it has to fit exactly a number of times inside AUDIO_RX_CHUNK_SAMPLES_LEN so we can split incoming audio data into arrays of PREAMBLE length
TELEGRAM_PREAMBLE_LEN_BYTES = 4 # 4 # 8 # NOTE: *** shall be "exactly" divisible by TELEGRAM_PREAMBLE_LEN_BYTES so we obtain an int rounds in thread_decode
START_LEN_BYTES = 1
ADDRESS_LEN_BYTES = 1
SEQ_NR_LEN_BYTES = 1
SEQ_NR_ACK_LEN_BYTES = 1
COMMAND_LEN_BYTES = 1
DATA_SIZE_LEN_BYTES = 1 # limits max. data-len to 255 bytes
END_LEN_BYTES = 1
CHECKSUM_LEN_BYTES = 1
# WORKAROUND: the "Terminator" shall get rid of signal deformation at the end of the bit-stream, or even "echoes" produced by previous ones which may falsify the last bits of the checksum.
TELEGRAM_TERMINATOR_LEN_BYTES = 1 # 1 # TELEGRAM_PREAMBLE_LEN_BYTES
HEADER_LEN_BYTES = START_LEN_BYTES + ADDRESS_LEN_BYTES + SEQ_NR_LEN_BYTES + SEQ_NR_LEN_BYTES + COMMAND_LEN_BYTES + DATA_SIZE_LEN_BYTES
FOOTER_LEN_BYTES = END_LEN_BYTES + CHECKSUM_LEN_BYTES

# NOTE:
# REQUEST-RESPONSE: responses at application layer shall set ACK bit as well
# these are cases where a clear request-response sequence shall be attained and 
# the ACK can be sent within timeout, even if it is triggered at application level
# CALL_ACCEPT is in turn a Request to send key-start -> KEY_START
# KEY_START is in turn a Request to send key-end -> KEY_END
# protocol definitions
# commands
COMMAND_NONE = 0x00 # usually XORed with ACK
COMMAND_CALL = 0x01
COMMAND_CALL_ACCEPTED = 0x02 # answered with app-level ACK
COMMAND_CALL_REJECTED = 0x03
COMMAND_CALL_END = 0x04
COMMAND_KEY_START = 0x05 # used together with ACK - answered with app-level ACK
COMMAND_KEY_PART = 0x06 # used together with ACK - answered with app-level ACK
COMMAND_KEY_END = 0x07 # used together with ACK - answered with app-level ACK
COMMAND_STARTUP_DATA = 0x08 # used together with ACK - answered with app-level ACK
COMMAND_STARTUP_DATA_COMPLETE = 0x09 # sent by part who accepted the call, used together with ACK - NOT answered with app-level ACK
COMMAND_CHAT_DATA_START = 0x0A
COMMAND_CHAT_DATA_PART = 0x0B
COMMAND_CHAT_DATA_END = 0x0C
COMMAND_CHAT_DATA = 0x0D
# especial commands 
COMMAND_ERROR = 0x7E
COMMAND_BROADCAST = 0x7F
# ack
COMMAND_TELEGRAM_ACK = 0x80
# masks
COMMAND_MASK = 0x7F
ACK_MASK = 0x80
######################
# command strings
CMD_STR = [""]*255
CMD_STR[COMMAND_NONE] = "NONE"
CMD_STR[COMMAND_CALL] = "CALL"
CMD_STR[COMMAND_CALL_ACCEPTED] = "CALL ACCEPTED"
CMD_STR[COMMAND_CALL_REJECTED] = "CALL REJECTED"
CMD_STR[COMMAND_CALL_END] = "CALL END"
CMD_STR[COMMAND_KEY_START] = "KEY START"
CMD_STR[COMMAND_KEY_PART] = "KEY PART"
CMD_STR[COMMAND_KEY_END] = "KEY END"
CMD_STR[COMMAND_STARTUP_DATA] = "STARTUP"
CMD_STR[COMMAND_STARTUP_DATA_COMPLETE] = "STARTUP COMPLETE"
CMD_STR[COMMAND_CHAT_DATA_START] = "DATA START"
CMD_STR[COMMAND_CHAT_DATA_PART] = "DATA PART"
CMD_STR[COMMAND_CHAT_DATA_END] = "DATA END"
CMD_STR[COMMAND_CHAT_DATA] = "DATA"
CMD_STR[COMMAND_ERROR] = "ERROR"
CMD_STR[COMMAND_BROADCAST] = "BROADCAST"
CMD_STR[COMMAND_TELEGRAM_ACK] = "ACK"
###########################################
# example of startup/initialization sequence
'''
      -------------------
                                 \    CALL
                                   --------------------->
                                ...
     -------------------
                                 \    CALL
                                   --------------------->
                                   ---------------------- (button_press)
                                 /    ACCEPT
    <-------------------
   (
    -------------------
                                 \    KEY_START
                                   --------------------->   
                                                                   )
                                   ----------------------    
                                 /    KEY_START
    <-------------------
   (
     -------------------
                                 \    KEY_END
                                   --------------------->   
                                                                   )
                                   ----------------------    
                                 /    KEY_END
    <-------------------
   (
    -------------------
                                 \    STARTUP
                                   --------------------->   
                                                                   )
                                   ----------------------    
                                 /    STARTUP_DATA_COMPLETE
    <-------------------
   (
    -------------------
                                 \    ACK
                                   --------------------->   
'''
###########################################

# fix definitions (cannot be set in GUI or .ini file)
#############################
# TODO: decouple TELEGRAM_MAX_LEN_BYTES from blocksize in audio streams
#            big blocksize is good to avoid overload of audio interface
#            but telegrams which are too big may become problematic...
print("TELEGRAM_MAX_LEN_BYTES = "+str(TELEGRAM_MAX_LEN_BYTES))
print("TELEGRAM_PREAMBLE_LEN_BYTES = "+str(TELEGRAM_PREAMBLE_LEN_BYTES))
print("HEADER_LEN_BYTES = "+str(HEADER_LEN_BYTES))
print("FOOTER_LEN_BYTES = "+str(FOOTER_LEN_BYTES))
OVERHEAD_MAX_LEN_BYTES = TELEGRAM_PREAMBLE_LEN_BYTES + HEADER_LEN_BYTES + FOOTER_LEN_BYTES + TELEGRAM_TERMINATOR_LEN_BYTES
# NOTE: AUDIO_RX_CHUNK_SAMPLES_LEN must be exactly divisible by N! so DATA_MAX_LEN_BYTES cannot have any value.
DATA_MAX_LEN_BYTES = TELEGRAM_MAX_LEN_BYTES - OVERHEAD_MAX_LEN_BYTES # 505 # 241 # 49 # 50 # 64 # 242 # 255 # limited by DATA_SIZE_LEN_BYTES
print("DATA_MAX_LEN_BYTES = "+str(DATA_MAX_LEN_BYTES))
print("OVERHEAD_MAX_LEN_BYTES = "+str(OVERHEAD_MAX_LEN_BYTES))
# TODO: check this..
# got value experimentally seeing problems when texts in Chat are too large -> audio overflow/underflow
# may need to tie this value to some audio settings?
MAX_TEXT_LEN = (DATA_MAX_LEN_BYTES*5000)
print("MAX_TEXT_LEN = "+str(MAX_TEXT_LEN))
# NOTE: MAX_NR_OF_TELEGRAMS_IN_PARALLEL determines how much memory we pre-allocate to be able to store all those telegrams in a buffer.
MAX_NR_OF_TELEGRAMS_IN_PARALLEL =  8 # 8 # 64
# nr of chunks per tel.
MAX_NR_OF_CHUNKS_PER_TELEGRAM = 1 # 2 # 4 # 8 # 64
print("MAX_NR_OF_CHUNKS_PER_TELEGRAM = "+str(MAX_NR_OF_CHUNKS_PER_TELEGRAM))
ALLOWED_FREQUENCY_CHANNELS = [0, 2, 3, 4, 5, 6, 7] # TODO: remove constraint?
DEFAULT_FREQUENCY_CHANNEL = 1 # selected on incorrect configuration
if CURRENT_FREQUENCY_CHANNEL not in ALLOWED_FREQUENCY_CHANNELS:
    print("ERROR: configuration problem, check the default value of CURRENT_FREQUENCY_CHANNEL in audioSettings.py. Change to first default.")
    CURRENT_FREQUENCY_CHANNEL = ALLOWED_FREQUENCY_CHANNELS[DEFAULT_FREQUENCY_CHANNEL]
print("CURRENT_FREQUENCY_CHANNEL = "+str(CURRENT_FREQUENCY_CHANNEL))

# INFO: Frequency channels / bands:
#   Narrowband (Tel. call): 300Hz - 3.4kHz  (sampling freq. 8kHz, 8-bit per sample, BitRate = 64kbps) -> G.711 ?
#                                            -> because of subsampling (8kHz) we have only 4kHz bandwidth = LPF
#   Wideband (VoIP): 50Hz - 7kHz (sampling freq. 16kHz, 16-bit per sample, BitRate = 64kbps) -> G.722 ?
#                                            -> because of subsampling (16kHz) we have only 8kHz bandwidth = LPF
#   Enhanced Voice Services (EVS for VoLTE / LTE-Networks): -> 20kHz
#     when both smartphones support EVS and communicate over LTE-Network (e.g. Telekom or Vodafone in Germany)           
# TODO: add definitions for EVS
# round nr. of samples per bit (note: overlaps with AFSK): f1 = 1225Hz (36 samples @ 44100), f2 = 2205Hz (20 samples @ 44100)   
#                                                                               f1 = 1200Hz (40 samples @ 48000), f2 = 2400Hz (2x20 samples @ 48000 -> same nr. of samples for ONE and ZERO)
# V.23: f1=1300Hz for ONE, f2 = 1700Hz (Mode1) or 2100Hz (Mode2) for ZERO (note: overlap with other channels)
FREQUENCY_CHANNELS = ["0: 600Hz, 1200Hz", "1: 1200Hz, 1700Hz (V.23 Mode 1)", "2: 1200Hz, 2100Hz (V.23 Mode 2)", "3: 1200Hz, 2200Hz (AFSK, Bell 202)", "4: 1225Hz, 2205Hz (exact samples x 44100)", "5: 1200Hz, 2400Hz (exact samples x 48000)", "6: 3kHz, 4kHz", "7: 3kHz, 6kHz", "8: 4.8kHz, 5.8kHz", "9: 6.6kHz, 6.8kHz" ]
CODE_SINE_IN_CHANNEL = [[600, 1200], [1200, 1700], [1200, 2100], [1200, 2200], [1225, 2205], [1200, 2400], [3000, 4000], [3000, 6000], [4800, 5800], [6600, 6800]]
# NOTE-1: we only use the left channel (mono only) as required e.g. for VoIP or telephone.
# NOTE-2: both communication parties use the same channel to TX/RX in half-duplex mode,
#               tests showed that even coding in different frequency-ranges will not avoid "HALF-DUPLEX-BEHAVIOR" 
#               FORCED by most of the messengers (Skype, Messenger, TRIfA, Signal, WhatsApp, Discord, Wire, Citadel, etc.)
DEFAULT_CHANNEL = 0 # index to left channel
# plot parameters
TIME_WINDOW_MS = 400
INTERVAL = 30
DOWNSAMPLE = 10

# configuration parameters determined during initialization from .ini file:
#########################################
# script or .exe?
runningScript = os.path.basename(__file__)
# different relative paths depending if we debug the script or run the executable file
if(runningScript=="audioSettings.py"): 
    # .py script
    configuration.IS_SCRIPT = True 
    configuration.PATH_PREFIX = "./dist/"
else:
    # .exe file
    configuration.IS_SCRIPT = False
    configuration.PATH_PREFIX = "./"
print("audioSettings.py: load config.init file.")
config = configparser.ConfigParser(allow_no_value=True)
config_filename = configuration.CONFIG_FILENAME

# Load the configuration file
#################
print("Reading "+config_filename)
try:
    config.read(config_filename)
    print("sections: ",  config.sections())
    if "myConfig" in config:
        print("keys in section myConfig:")
        if "TELEGRAM_MAX_LEN_BYTES" in config["myConfig"]:
            audioSettings.TELEGRAM_MAX_LEN_BYTES = config.getint('myConfig','TELEGRAM_MAX_LEN_BYTES')
            print("TELEGRAM_MAX_LEN_BYTES = ",  audioSettings.TELEGRAM_MAX_LEN_BYTES)
        if "MAX_NR_OF_CHUNKS_PER_TELEGRAM" in config["myConfig"]:
            audioSettings.MAX_NR_OF_CHUNKS_PER_TELEGRAM = config.getint('myConfig','MAX_NR_OF_CHUNKS_PER_TELEGRAM')
            print("MAX_NR_OF_CHUNKS_PER_TELEGRAM = ",  audioSettings.MAX_NR_OF_CHUNKS_PER_TELEGRAM)
        if "CURRENT_FREQUENCY_CHANNEL" in config["myConfig"]:
            audioSettings.CURRENT_FREQUENCY_CHANNEL = config.getint('myConfig','CURRENT_FREQUENCY_CHANNEL')
            print("CURRENT_FREQUENCY_CHANNEL = ",  audioSettings.CURRENT_FREQUENCY_CHANNEL)
            # check CURRENT_FREQUENCY_CHANNEL
            if CURRENT_FREQUENCY_CHANNEL not in ALLOWED_FREQUENCY_CHANNELS:
                print("ERROR: configuration problem, check the default value of CURRENT_FREQUENCY_CHANNEL in .ini file. Change to first default.")
                CURRENT_FREQUENCY_CHANNEL = ALLOWED_FREQUENCY_CHANNELS[DEFAULT_FREQUENCY_CHANNEL]
        if "SAMPLING_FREQUENCY" in config["myConfig"]:
            audioSettings.SAMPLING_FREQUENCY = config.getint('myConfig','SAMPLING_FREQUENCY')
            print("SAMPLING_FREQUENCY = ",  audioSettings.SAMPLING_FREQUENCY)
        if "AMPLITUDE" in config["myConfig"]:
            audioSettings.AMPLITUDE = config.getfloat('myConfig','AMPLITUDE')
            print("AMPLITUDE = ",  audioSettings.AMPLITUDE)
        if "FFT_DETECTION_LEVEL" in config["myConfig"]:
            audioSettings.FFT_DETECTION_LEVEL = config.getfloat('myConfig','FFT_DETECTION_LEVEL')
            print("FFT_DETECTION_LEVEL = ",  audioSettings.FFT_DETECTION_LEVEL)
        if "CHANNEL_DELAY_MS" in config["myConfig"]:
            audioSettings.CHANNEL_DELAY_MS = config.getint('myConfig','CHANNEL_DELAY_MS')
            print("CHANNEL_DELAY_MS = ",  audioSettings.CHANNEL_DELAY_MS)
        if "MAX_RESENDS" in config["myConfig"]:
            audioSettings.MAX_RESENDS = config.getint('myConfig','MAX_RESENDS')
            print("MAX_RESENDS = ",  audioSettings.MAX_RESENDS)
        if "CARRIER_FREQUENCY_HZ" in config["myConfig"]:
            audioSettings.CARRIER_FREQUENCY_HZ = config.getint('myConfig','CARRIER_FREQUENCY_HZ')
            print("CARRIER_FREQUENCY_HZ = ",  audioSettings.CARRIER_FREQUENCY_HZ)
        if "CARRIER_AMPLITUDE" in config["myConfig"]:
            audioSettings.CARRIER_AMPLITUDE = config.getfloat('myConfig','CARRIER_AMPLITUDE')
            print("CARRIER_AMPLITUDE = ",  audioSettings.CARRIER_AMPLITUDE)
        if "ADD_CARRIER" in config["myConfig"]:
            audioSettings.ADD_CARRIER = config.getboolean('myConfig','ADD_CARRIER')
            print("ADD_CARRIER = ",  audioSettings.ADD_CARRIER)
        if "REMOVE_RX_CARRIER" in config["myConfig"]:
            audioSettings.REMOVE_RX_CARRIER = config.getboolean('myConfig','REMOVE_RX_CARRIER')
            print("REMOVE_RX_CARRIER = ",  audioSettings.REMOVE_RX_CARRIER)
except (configparser.NoSectionError, configparser.MissingSectionHeaderError):
    print("Exception raised in init.loadConfigFile() trying to load config file!\n")
    pass
                    
# derived definitions
############
# frequency codes
CODE_SINE_FREQUENCY_ONE = CODE_SINE_IN_CHANNEL[CURRENT_FREQUENCY_CHANNEL][0]
CODE_SINE_FREQUENCY_ZERO =  CODE_SINE_IN_CHANNEL[CURRENT_FREQUENCY_CHANNEL][1]
# Nyquist frequency
NYQUIST_FREQUENCY = (SAMPLING_FREQUENCY/2.0)
# coding parameters AFSK
# samples
SAMPLES_PER_CYCLE_ONE = int(SAMPLING_FREQUENCY / CODE_SINE_FREQUENCY_ONE)
print("SAMPLES_PER_CYCLE_ONE = "+str(SAMPLES_PER_CYCLE_ONE))
SAMPLES_PER_CYCLE_ZERO = int(SAMPLING_FREQUENCY / CODE_SINE_FREQUENCY_ZERO)
print("SAMPLES_PER_CYCLE_ZERO = "+str(SAMPLES_PER_CYCLE_ZERO))
LEN_BIT_ONE = SAMPLES_PER_CYCLE_ONE
print("LEN_BIT_ONE = "+str(LEN_BIT_ONE))
LEN_BIT_ZERO = (SAMPLES_PER_CYCLE_ZERO*2) # NOTE: *2 to have the same nr. of samples as ONE
print("LEN_BIT_ZERO = "+str(LEN_BIT_ZERO))
LEN_BIT_BETWEEN_ZERO_AND_ONE = (LEN_BIT_ZERO + (LEN_BIT_ONE - LEN_BIT_ZERO)//2)
LEN_BIT_ZERO_MIN = (LEN_BIT_ZERO - (LEN_BIT_ONE - LEN_BIT_ZERO)//2)
LEN_BIT_ONE_MAX = (LEN_BIT_ONE + (LEN_BIT_ONE - LEN_BIT_ZERO)//2)
# FIX - derived from definitions above
TELEGRAM_MAX_LEN_BITS = (TELEGRAM_MAX_LEN_BYTES*8)
TELEGRAM_PREAMBLE_LEN_BITS = (TELEGRAM_PREAMBLE_LEN_BYTES*8)
OVERHEAD_MAX_LEN_BITS = (OVERHEAD_MAX_LEN_BYTES*8)
DATA_MAX_LEN_BITS = (DATA_MAX_LEN_BYTES*8)
# samples
TELEGRAM_MAX_LEN_SAMPLES = (LEN_BIT_ONE*TELEGRAM_MAX_LEN_BITS)
TELEGRAM_MAX_LEN_SECONDS = (TELEGRAM_MAX_LEN_SAMPLES/SAMPLING_FREQUENCY)
TELEGRAM_PREAMBLE_LEN_SAMPLES = (LEN_BIT_ONE*TELEGRAM_PREAMBLE_LEN_BITS)
START_LEN_SAMPLES = (START_LEN_BYTES*4*LEN_BIT_ONE) + (START_LEN_BYTES*4*LEN_BIT_ZERO) 
print("TELEGRAM_MAX_LEN_SAMPLES = "+str(TELEGRAM_MAX_LEN_SAMPLES))
print("TELEGRAM_MAX_LEN_SECONDS = "+str(TELEGRAM_MAX_LEN_SECONDS))
print("TELEGRAM_PREAMBLE_LEN_SAMPLES = "+str(TELEGRAM_PREAMBLE_LEN_SAMPLES))
print("START_LEN_SAMPLES = "+str(START_LEN_SAMPLES))
# chunks
AUDIO_TX_CHUNK_SAMPLES_LEN = int(TELEGRAM_MAX_LEN_SAMPLES/MAX_NR_OF_CHUNKS_PER_TELEGRAM) 
# poll period for transmission (and reception?)
TX_POLL_PERIOD_SEC = 0.01
# Roundtrip delay until we get the ACK (maximum values):
#        TX-Telegram + Channel-delay + RX-processing + TX-processing + ACK-Telegram + Channel-delay + RX-processing
#      = 2*Telegram-delay + 2*Channel-delay + RX-TX-processing
# we assume RX-TX + RX-processing delay to be max.
TX_RX_PROCESSING_SEC = (3*TX_POLL_PERIOD_SEC)*2 # *2 is security factor due to rounding polling errors
# retransmission delay
# we may miss the audio chunk and need to wait until the current audio chunk is transmitted (fix delay in our system due to buffering)
AUDIO_CHUNK_DELAY_SEC = (AUDIO_TX_CHUNK_SAMPLES_LEN//SAMPLING_FREQUENCY)
TX_RETRANSMISSION_SEC = (2*TELEGRAM_MAX_LEN_SECONDS + 2*CHANNEL_DELAY_SEC + TX_RX_PROCESSING_SEC + AUDIO_CHUNK_DELAY_SEC) 
TX_RETRANSMISSION_POLL_PERIODS_SHORT = int(TX_RETRANSMISSION_SEC/TX_POLL_PERIOD_SEC)
TX_RETRANSMISSION_POLL_PERIODS_LONG = int(1.5*TX_RETRANSMISSION_POLL_PERIODS_SHORT)
print("TX_RETRANSMISSION_POLL_PERIODS_SHORT = "+str(TX_RETRANSMISSION_POLL_PERIODS_SHORT))
print("TX_RETRANSMISSION_POLL_PERIODS_LONG = "+str(TX_RETRANSMISSION_POLL_PERIODS_LONG))
# NOTE: reduce AUDIO_RX_CHUNK_SAMPLES_LEN for faster recognition of
#            preambles and processing of telegrams...especially the short ones...otherwise additional delays introduced. -> have really advantages?
AUDIO_RX_CHUNK_SAMPLES_LEN = AUDIO_TX_CHUNK_SAMPLES_LEN # TELEGRAM_PREAMBLE_LEN_SAMPLES == AUDIO_TX_CHUNK_SAMPLES_LEN or TELEGRAM_MAX_LEN_SAMPLES
print("AUDIO_TX_CHUNK_SAMPLES_LEN = "+str(AUDIO_TX_CHUNK_SAMPLES_LEN))
print("AUDIO_RX_CHUNK_SAMPLES_LEN = "+str(AUDIO_RX_CHUNK_SAMPLES_LEN))
AUDIO_CHUNK_BYTES_LEN = int(TELEGRAM_MAX_LEN_BYTES/MAX_NR_OF_CHUNKS_PER_TELEGRAM) # not used anywhere, just for info - TODO: check
# coding AFSK - further parameters
t = np.linspace(0.0, TELEGRAM_MAX_LEN_SAMPLES, TELEGRAM_MAX_LEN_SAMPLES) / SAMPLING_FREQUENCY
PREAMBLE = AMPLITUDE * np.sin(2 * np.pi * audioSettings.CODE_SINE_FREQUENCY_ONE * t[:TELEGRAM_PREAMBLE_LEN_SAMPLES])
ONE = AMPLITUDE * np.sin(2 * np.pi * audioSettings.CODE_SINE_FREQUENCY_ONE * t[:LEN_BIT_ONE])
ZERO = AMPLITUDE * np.sin(2 * np.pi * audioSettings.CODE_SINE_FREQUENCY_ZERO * t[:LEN_BIT_ZERO])
ONE = ONE.reshape(-1, 1)
ZERO = ZERO.reshape(-1, 1)
# carrier
CARRIER = CARRIER_AMPLITUDE * np.sin(2 * np.pi * CARRIER_FREQUENCY_HZ * t[:AUDIO_TX_CHUNK_SAMPLES_LEN])
CARRIER = CARRIER.reshape(-1, 1)
SILENCE = [[0.0]]*AUDIO_TX_CHUNK_SAMPLES_LEN
# for FFT, plot
N = TELEGRAM_PREAMBLE_LEN_SAMPLES # //2 # *2 # FFT on audio-input-chunks..
print("N = "+str(N))
# sample spacing
T = (1.0 / SAMPLING_FREQUENCY)
# check:
if int(audioSettings.AUDIO_RX_CHUNK_SAMPLES_LEN/audioSettings.N) != (audioSettings.AUDIO_RX_CHUNK_SAMPLES_LEN/audioSettings.N):
    print("Configuration ERROR: AUDIO_RX_CHUNK_SAMPLES_LEN must be exactly divisible by N!")
    exit()
# TODO: how do we consider digits after the comma?
BIN_FREQUENCY_ONE = int(round(CODE_SINE_FREQUENCY_ONE*N/SAMPLING_FREQUENCY))
BIN_FREQUENCY_ZERO = int(round(CODE_SINE_FREQUENCY_ZERO*N/SAMPLING_FREQUENCY))
#####################################################################
# NOTE: we decode a single bit, where we only have e.g. 40 samples and therefore 1 BIN for every 1000Hz, which results in:
#            20 samples per FFT ==> 20.000 Hz / 20 = 1000 Hz per Frequency-Bin ==>
#            bin-ONE = 1, bin-ZERO = 2
#            *** therefore, we can distinguish ONEs and ZEROs coded in frequencies separated by "at least" 1kHz ! ***
BIN_FREQUENCY_ONE_FINE = int(round(CODE_SINE_FREQUENCY_ONE*LEN_BIT_ONE/SAMPLING_FREQUENCY))
BIN_FREQUENCY_ZERO_FINE = int(round(CODE_SINE_FREQUENCY_ZERO*LEN_BIT_ONE/SAMPLING_FREQUENCY))
#####################################################################
print("BIN_FREQUENCY_ONE = "+str(BIN_FREQUENCY_ONE))
print("BIN_FREQUENCY_ZERO = "+str(BIN_FREQUENCY_ZERO))
print("BIN_FREQUENCY_ONE_FINE = "+str(BIN_FREQUENCY_ONE_FINE))
print("BIN_FREQUENCY_ZERO_FINE = "+str(BIN_FREQUENCY_ZERO_FINE))
# definitions needed to soften borders of telegram with Gauss-/Normal- shape
# this shall avoid generating high-frequencies when coding (beginning of sine from silence is like a step-signal):
#
#     |
#     |                     /
#   _|       ==>      _/
#
# for now we use LEN_BIT_ZERO as a reference for the length because it's usually shorter(?) than LEN_BIT_ONE
LEN_BORDER = 0 # LEN_BIT_ZERO*TELEGRAM_TERMINATOR_LEN_BYTES*8
CODE_TRANSITION_SAMPLES = 0 # LEN_BORDER//4 - 1 # 0 # LEN_BORDER//2 - 1


def updateDerivedAudioSettings():
    audioSettings.CHANNEL_DELAY_SEC = float(audioSettings.CHANNEL_DELAY_MS/1000.0)
    audioSettings.CODE_SINE_FREQUENCY_ONE = audioSettings.CODE_SINE_IN_CHANNEL[CURRENT_FREQUENCY_CHANNEL][0]
    audioSettings.CODE_SINE_FREQUENCY_ZERO =  audioSettings.CODE_SINE_IN_CHANNEL[CURRENT_FREQUENCY_CHANNEL][1]
    audioSettings.NYQUIST_FREQUENCY = (audioSettings.SAMPLING_FREQUENCY/2.0)
    audioSettings.SAMPLES_PER_CYCLE_ONE = int(audioSettings.SAMPLING_FREQUENCY / audioSettings.CODE_SINE_FREQUENCY_ONE) + 0 # + 1
    audioSettings.SAMPLES_PER_CYCLE_ZERO = int(audioSettings.SAMPLING_FREQUENCY / audioSettings.CODE_SINE_FREQUENCY_ZERO) + 0
    audioSettings.LEN_BIT_ONE = audioSettings.SAMPLES_PER_CYCLE_ONE
    audioSettings.LEN_BIT_ZERO = (audioSettings.SAMPLES_PER_CYCLE_ZERO*2)
    audioSettings.LEN_BIT_BETWEEN_ZERO_AND_ONE = (audioSettings.LEN_BIT_ZERO + (audioSettings.LEN_BIT_ONE - audioSettings.LEN_BIT_ZERO)//2)
    audioSettings.LEN_BIT_ZERO_MIN = (audioSettings.LEN_BIT_ZERO - (audioSettings.LEN_BIT_ONE - audioSettings.LEN_BIT_ZERO)//2)
    audioSettings.LEN_BIT_ONE_MAX = (audioSettings.LEN_BIT_ONE + (audioSettings.LEN_BIT_ONE - audioSettings.LEN_BIT_ZERO)//2)
    audioSettings.TELEGRAM_MAX_LEN_SAMPLES = (audioSettings.LEN_BIT_ONE*audioSettings.TELEGRAM_MAX_LEN_BITS)
    audioSettings.TELEGRAM_MAX_LEN_SECONDS = (audioSettings.TELEGRAM_MAX_LEN_SAMPLES/audioSettings.SAMPLING_FREQUENCY)
    audioSettings.TELEGRAM_PREAMBLE_LEN_SAMPLES = (audioSettings.LEN_BIT_ONE*audioSettings.TELEGRAM_PREAMBLE_LEN_BITS)
    audioSettings.START_LEN_SAMPLES = (audioSettings.START_LEN_BYTES*4*audioSettings.LEN_BIT_ONE) + (audioSettings.START_LEN_BYTES*4*audioSettings.LEN_BIT_ZERO) 
    audioSettings.MAX_TEXT_LEN = (audioSettings.DATA_MAX_LEN_BYTES*500)
    audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN = int(audioSettings.TELEGRAM_MAX_LEN_SAMPLES/audioSettings.MAX_NR_OF_CHUNKS_PER_TELEGRAM)
    audioSettings.AUDIO_RX_CHUNK_SAMPLES_LEN = audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN
    audioSettings.AUDIO_CHUNK_BYTES_LEN = int(audioSettings.TELEGRAM_MAX_LEN_BYTES/audioSettings.MAX_NR_OF_CHUNKS_PER_TELEGRAM)
    audioSettings.AUDIO_CHUNK_RESOLUTION_DELAY_SEC = audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN/audioSettings.SAMPLING_FREQUENCY
    audioSettings.TX_RETRANSMISSION_SEC = 2*audioSettings.TELEGRAM_MAX_LEN_SECONDS + 2*audioSettings.CHANNEL_DELAY_SEC + audioSettings.TX_RX_PROCESSING_SEC + audioSettings.AUDIO_CHUNK_RESOLUTION_DELAY_SEC 
    audioSettings.TX_RETRANSMISSION_POLL_PERIODS_SHORT = int(audioSettings.TX_RETRANSMISSION_SEC/audioSettings.TX_POLL_PERIOD_SEC)
    audioSettings.TX_RETRANSMISSION_POLL_PERIODS_LONG = int(1.5*audioSettings.TX_RETRANSMISSION_POLL_PERIODS_SHORT)
    audioSettings.t = np.linspace(0.0, audioSettings.TELEGRAM_MAX_LEN_SAMPLES, audioSettings.TELEGRAM_MAX_LEN_SAMPLES) / audioSettings.SAMPLING_FREQUENCY
    audioSettings.t = audioSettings.t.reshape(-1, 1)
    audioSettings.ONE = audioSettings.AMPLITUDE * np.sin(2 * np.pi * audioSettings.CODE_SINE_FREQUENCY_ONE * audioSettings.t[:audioSettings.LEN_BIT_ONE])
    audioSettings.ZERO = audioSettings.AMPLITUDE * np.sin(2 * np.pi * audioSettings.CODE_SINE_FREQUENCY_ZERO * audioSettings.t[:audioSettings.LEN_BIT_ZERO])
    audioSettings.CARRIER = audioSettings.CARRIER_AMPLITUDE * np.sin(2 * np.pi * audioSettings.CARRIER_FREQUENCY_HZ * t[:audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN])
    ###audioSettings.CARRIER[0:audioSettings.SAMPLING_FREQUENCY//audioSettings.CARRIER_FREQUENCY_HZ] = 5.0*audioSettings.CARRIER[0:audioSettings.SAMPLING_FREQUENCY//audioSettings.CARRIER_FREQUENCY_HZ]
    audioSettings.ONE = audioSettings.ONE.reshape(-1, 1)
    audioSettings.ZERO = audioSettings.ZERO.reshape(-1, 1)
    audioSettings.CARRIER = audioSettings.CARRIER.reshape(-1, 1)
    audioSettings.SILENCE = [[0.]]*audioSettings.AUDIO_TX_CHUNK_SAMPLES_LEN
    audioSettings.N = audioSettings.TELEGRAM_PREAMBLE_LEN_SAMPLES # *2
    audioSettings.BIN_FREQUENCY_ONE = int(round(audioSettings.CODE_SINE_FREQUENCY_ONE*audioSettings.N/audioSettings.SAMPLING_FREQUENCY))
    audioSettings.BIN_FREQUENCY_ZERO = int(round(audioSettings.CODE_SINE_FREQUENCY_ZERO*audioSettings.N/audioSettings.SAMPLING_FREQUENCY))
    audioSettings.BIN_FREQUENCY_ONE_FINE = int(round(audioSettings.CODE_SINE_FREQUENCY_ONE*audioSettings.LEN_BIT_ONE/audioSettings.SAMPLING_FREQUENCY))
    audioSettings.BIN_FREQUENCY_ZERO_FINE = int(round(audioSettings.CODE_SINE_FREQUENCY_ZERO*audioSettings.LEN_BIT_ONE/audioSettings.SAMPLING_FREQUENCY))
    audioSettings.T = (1.0 / audioSettings.SAMPLING_FREQUENCY)
    audioSettings.LEN_BORDER = 0 # audioSettings.LEN_BIT_ZERO*audioSettings.TELEGRAM_TERMINATOR_LEN_BYTES*8
    audioSettings.CODE_TRANSITION_SAMPLES = 0 # audioSettings.LEN_BORDER//4 # audioSettings.LEN_BORDER//2 - 1
        




