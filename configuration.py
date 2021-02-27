# -*- coding: utf-8 -*-

# Version format: MAJOR.MINOR.BUGFIX
VERSION = "1.0.0"
VERSION_TOOL_TIP = "First release version containing basic set of features for demonstration purposes.\n\
                    This PoC (Proof of Concept) still needs to be \"refactored\" and \"un-extremed\"."
                    
# LOGGING_LEVEL specifies the lowest-severity log message a logger will handle, where debug is the lowest built-in severity level and critical is the highest built-in severity.
# For example, if the severity level is INFO, the logger will handle only INFO, WARNING, ERROR, and CRITICAL messages and will ignore DEBUG messages.
LOGGING_LEVEL = "logging.INFO"

# encryption (fix values)
ENRYPTION_BLOCK_BYTES_LEN = 16 # any relation to audioSettings.DATA_MAX_LEN_BYTES ???
PADDING_BITS_LEN = (ENRYPTION_BLOCK_BYTES_LEN*8)

# app font size
FONT_SIZE_APP = 8

# send on ENTER
SEND_ON_ENTER = True

# show advance settings
SHOW_ADVANCED_SETTINGS = False

# update period of GUI
GUI_UPDATE_PERIOD_IN_SEC = 0.5

# user name
USER_NAME = "Alice"

# call/answer automatically
CALL_ANSWER_AUTO = True

# show live status
SHOW_LIVE_STATUS = True

# audio device settings
AUDIO_DEVICE_TX_IN = "none"
AUDIO_DEVICE_TX_OUT = "none"
AUDIO_DEVICE_RX_IN = "none"
AUDIO_DEVICE_RX_OUT = "none"

# diagram (advanced settings)
SHOW_PLOT = True
PLOT_FFT = True
PLOT_CODE_ONLY = False

# chat
TEXT_SIZE = 12
TEXT_BOLD = True
TEXT_FAMILY = "Arial"

# sound effects (e.g. when pushing buttons)
SOUND_EFFECTS = True

# Audio Input for TX: distortion, scrambling on/off
IN_TX_DISTORT = True
IN_TX_SCRAMBLE = False
IN_RX_UNDISTORT = False

# hear RX voice or RX code?
OUT_RX_HEAR_VOICE = False

# transmit voice?
TRANSMIT_IN_TX_VOICE = True

# performance (advanced settings)
SHOW_PERFORMANCE = True
TX_PROC_MS = 0.0
RX_PROC_MS = 0.0
ROUNDTRIP_MS = 0.0

# script or .exe?
# the following parameters are determined at runtime (not stored in config.ini)
IS_SCRIPT = True 
PATH_PREFIX = "./dist/"
CONFIG_FILENAME = "config.ini"









