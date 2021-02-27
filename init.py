# -*- coding: utf-8 -*-

import configuration
import audioSettings
import configparser
from tkinter.filedialog import askopenfilename,  asksaveasfilename
import os
from tkinter import *


# we need this, otherwise we see the Tk window
Tk().withdraw() 

    
class InitApp(object):
    config = configparser.ConfigParser(allow_no_value=True)
    config_filename = configuration.CONFIG_FILENAME
    
    def __init__(self):
        # script or .exe?
        runningScript = os.path.basename(__file__)
        # we get different relative paths depending if we debug or run the executable file
        if(runningScript=="init.py"): 
            # .py script
            configuration.IS_SCRIPT = True 
            configuration.PATH_PREFIX = "./dist/"
        else:
            # .exe file
            configuration.IS_SCRIPT = False
            configuration.PATH_PREFIX = "./"
        print("init.py.__init__(): load config.init file.")
        # Load the "default" configuration file
        self.loadConfigFile(self.config_filename)
        
    def loadConfig(self):
        files = [ # ('All Files', '*.*'),  
                ('Ini Files', '*.ini'), 
                ('Text Document', '*.txt')] 
        filename = askopenfilename(initialdir="./", filetypes = files, defaultextension = files) 
        if (filename is not None) and (filename != ""): 
            self.loadConfigFile(filename)
        return os.path.basename(filename)

    # NOTE: Audio Settings are read and set in audioSettings.py
    #            and LOGGING_LEVEL is read in AC4QGP.py
    def loadConfigFile(self,  filename):
        print("Reading "+filename)
        try:
            self.config.read(filename)
            print("sections: ",  self.config.sections())
            if "myConfig" in self.config:
                print("keys in section myConfig:")
                if "FONT_SIZE_APP" in self.config["myConfig"]:
                    configuration.FONT_SIZE_APP = int(self.config['myConfig']['FONT_SIZE_APP'])
                    print("FONT_SIZE_APP = ",  int(self.config['myConfig']['FONT_SIZE_APP']))
                if "TEXT_SIZE" in self.config["myConfig"]:
                    configuration.TEXT_SIZE = int(self.config['myConfig']['TEXT_SIZE'])
                    print("TEXT_SIZE = ",  int(self.config['myConfig']['TEXT_SIZE']))
                if "SHOW_ADVANCED_SETTINGS" in self.config["myConfig"]:
                    configuration.SHOW_ADVANCED_SETTINGS = self.config.getboolean('myConfig','SHOW_ADVANCED_SETTINGS')
                    print("SHOW_ADVANCED_SETTINGS = ",  configuration.SHOW_ADVANCED_SETTINGS)
                if "GUI_UPDATE_PERIOD_IN_SEC" in self.config["myConfig"]:
                    configuration.GUI_UPDATE_PERIOD_IN_SEC = float(self.config['myConfig']['GUI_UPDATE_PERIOD_IN_SEC'])
                    print("GUI_UPDATE_PERIOD_IN_SEC = ",  configuration.GUI_UPDATE_PERIOD_IN_SEC)
                if "USER_NAME" in self.config["myConfig"]:
                    configuration.USER_NAME = self.config['myConfig']['USER_NAME']
                    print("USER_NAME = ",  configuration.USER_NAME)
                if "CALL_ANSWER_AUTO" in self.config["myConfig"]:
                    # configuration.CALL_ANSWER_AUTO = self.config['myConfig']['CALL_ANSWER_AUTO'] # NOTE: alternative way
                    configuration.CALL_ANSWER_AUTO = self.config.getboolean('myConfig','CALL_ANSWER_AUTO')
                    print("CALL_ANSWER_AUTO = ",  configuration.CALL_ANSWER_AUTO)
                if "SHOW_LIVE_STATUS" in self.config["myConfig"]:
                    configuration.SHOW_LIVE_STATUS = self.config.getboolean('myConfig','SHOW_LIVE_STATUS')
                    print("SHOW_LIVE_STATUS = ",  configuration.SHOW_LIVE_STATUS)
                if "AUDIO_DEVICE_TX_IN" in self.config["myConfig"]:
                    configuration.AUDIO_DEVICE_TX_IN = self.config['myConfig']['AUDIO_DEVICE_TX_IN']
                    print("AUDIO_DEVICE_TX_IN = ",  configuration.AUDIO_DEVICE_TX_IN)
                if "AUDIO_DEVICE_TX_OUT" in self.config["myConfig"]:
                    configuration.AUDIO_DEVICE_TX_OUT = self.config['myConfig']['AUDIO_DEVICE_TX_OUT']
                    print("AUDIO_DEVICE_TX_OUT = ",  configuration.AUDIO_DEVICE_TX_OUT)
                if "AUDIO_DEVICE_RX_IN" in self.config["myConfig"]:
                    configuration.AUDIO_DEVICE_RX_IN = self.config['myConfig']['AUDIO_DEVICE_RX_IN']
                    print("AUDIO_DEVICE_RX_IN = ",  configuration.AUDIO_DEVICE_RX_IN)
                if "AUDIO_DEVICE_RX_OUT" in self.config["myConfig"]:
                    configuration.AUDIO_DEVICE_RX_OUT = self.config['myConfig']['AUDIO_DEVICE_RX_OUT']
                    print("AUDIO_DEVICE_RX_OUT = ",  configuration.AUDIO_DEVICE_RX_OUT)
                if "SHOW_PLOT" in self.config["myConfig"]:
                    configuration.SHOW_PLOT = self.config.getboolean('myConfig','SHOW_PLOT')
                    print("SHOW_PLOT = ",  configuration.SHOW_PLOT)
                if "PLOT_FFT" in self.config["myConfig"]:
                    configuration.PLOT_FFT = self.config.getboolean('myConfig','PLOT_FFT')
                    print("PLOT_FFT = ",  configuration.PLOT_FFT)
                if "PLOT_CODE_ONLY" in self.config["myConfig"]:
                    configuration.PLOT_CODE_ONLY = self.config.getboolean('myConfig','PLOT_CODE_ONLY')
                    print("PLOT_CODE_ONLY = ",  configuration.PLOT_CODE_ONLY)
                if "TEXT_BOLD" in self.config["myConfig"]:
                    configuration.TEXT_BOLD = self.config.getboolean('myConfig','TEXT_BOLD')
                    print("TEXT_BOLD = ",  configuration.TEXT_BOLD)
                if "TEXT_SIZE" in self.config["myConfig"]:
                    configuration.TEXT_SIZE = self.config.getint('myConfig','TEXT_SIZE')
                    print("TEXT_SIZE = ",  configuration.TEXT_SIZE)
                if "TEXT_FAMILY" in self.config["myConfig"]:
                    configuration.TEXT_FAMILY = self.config['myConfig']['TEXT_FAMILY']
                    print("TEXT_FAMILY = ",  configuration.TEXT_FAMILY)
                if "SOUND_EFFECTS" in self.config["myConfig"]:
                    configuration.SOUND_EFFECTS = self.config.getboolean('myConfig','SOUND_EFFECTS')
                    print("SOUND_EFFECTS = ",  configuration.SOUND_EFFECTS)
                if "SEND_ON_ENTER" in self.config["myConfig"]:
                    configuration.SEND_ON_ENTER = self.config.getboolean('myConfig','SEND_ON_ENTER')
                    print("SEND_ON_ENTER = ",  configuration.SEND_ON_ENTER)
                if "IN_TX_DISTORT" in self.config["myConfig"]:
                    configuration.IN_TX_DISTORT = self.config.getboolean('myConfig','IN_TX_DISTORT')
                    print("IN_TX_DISTORT = ",  configuration.IN_TX_DISTORT)
                if "IN_RX_UNDISTORT" in self.config["myConfig"]:
                    configuration.IN_RX_UNDISTORT = self.config.getboolean('myConfig','IN_RX_UNDISTORT')
                    print("IN_RX_UNDISTORT = ",  configuration.IN_RX_UNDISTORT)
                if "IN_TX_SCRAMBLE" in self.config["myConfig"]:
                    configuration.IN_TX_SCRAMBLE = self.config.getboolean('myConfig','IN_TX_SCRAMBLE')
                    print("IN_TX_SCRAMBLE = ",  configuration.IN_TX_SCRAMBLE)
                if "OUT_RX_HEAR_VOICE" in self.config["myConfig"]:
                    configuration.OUT_RX_HEAR_VOICE = self.config.getboolean('myConfig','OUT_RX_HEAR_VOICE')
                    print("OUT_RX_HEAR_VOICE = ",  configuration.OUT_RX_HEAR_VOICE)
                if "TRANSMIT_IN_TX_VOICE" in self.config["myConfig"]:
                    configuration.TRANSMIT_IN_TX_VOICE = self.config.getboolean('myConfig','TRANSMIT_IN_TX_VOICE')
                    print("TRANSMIT_IN_TX_VOICE = ",  configuration.TRANSMIT_IN_TX_VOICE)
                if "SHOW_PERFORMANCE" in self.config["myConfig"]:
                    configuration.SHOW_PERFORMANCE = self.config.getboolean('myConfig','SHOW_PERFORMANCE')
                    print("SHOW_PERFORMANCE = ",  configuration.SHOW_PERFORMANCE)
                # from now on we have a new filename
                self.config_filename = filename
            else:
                print("Could not load config file: "+filename)
        except (configparser.NoSectionError, configparser.MissingSectionHeaderError):
            print("Exception raised in init.loadConfigFile() trying to load config file!\n")
            pass
        
    def saveConfigAs(self):
        files = [ # ('All Files', '*.*'),  
                ('Ini Files', '*.ini'), 
                ('Text Document', '*.txt')] 
        filename = asksaveasfilename(initialdir="./", filetypes = files, defaultextension = files) 
        if (filename is not None) and (filename != ""): 
            self.saveConfigFile(filename)
            return os.path.basename(filename)
        else:
            return ""
            
    def saveConfig(self):
        self.saveConfigFile(self.config_filename)
        return os.path.basename(self.config_filename)
                
    def saveConfigFile(self, filename):
        self.config['myConfig']['LOGGING_LEVEL'] = str(configuration.LOGGING_LEVEL)
        self.config['myConfig']['FONT_SIZE_APP'] = str(configuration.FONT_SIZE_APP)
        self.config['myConfig']['SHOW_ADVANCED_SETTINGS'] = str(configuration.SHOW_ADVANCED_SETTINGS)
        self.config['myConfig']['GUI_UPDATE_PERIOD_IN_SEC'] = str(configuration.GUI_UPDATE_PERIOD_IN_SEC)
        self.config['myConfig']['SHOW_LIVE_STATUS'] = str(configuration.SHOW_LIVE_STATUS)
        # config.set('myConfig', 'USER_NAME', configuration.USER_NAME) # NOTE: alternative way
        self.config['myConfig']['USER_NAME'] = configuration.USER_NAME
        self.config['myConfig']['CALL_ANSWER_AUTO'] = str(configuration.CALL_ANSWER_AUTO)
        self.config['myConfig']['AUDIO_DEVICE_TX_IN'] = configuration.AUDIO_DEVICE_TX_IN
        self.config['myConfig']['AUDIO_DEVICE_TX_OUT'] = configuration.AUDIO_DEVICE_TX_OUT
        self.config['myConfig']['AUDIO_DEVICE_RX_IN'] = configuration.AUDIO_DEVICE_RX_IN
        self.config['myConfig']['AUDIO_DEVICE_RX_OUT'] = configuration.AUDIO_DEVICE_RX_OUT
        self.config['myConfig']['SHOW_PLOT'] = str(configuration.SHOW_PLOT)
        self.config['myConfig']['SHOW_PERFORMANCE'] = str(configuration.SHOW_PERFORMANCE)
        self.config['myConfig']['PLOT_FFT'] = str(configuration.PLOT_FFT)
        self.config['myConfig']['PLOT_CODE_ONLY'] = str(configuration.PLOT_CODE_ONLY)
        self.config['myConfig']['TEXT_BOLD'] = str(configuration.TEXT_BOLD)
        self.config['myConfig']['TEXT_SIZE'] = str(configuration.TEXT_SIZE)
        self.config['myConfig']['TEXT_FAMILY'] = configuration.TEXT_FAMILY
        self.config['myConfig']['SOUND_EFFECTS'] = str(configuration.SOUND_EFFECTS)
        self.config['myConfig']['SEND_ON_ENTER'] = str(configuration.SEND_ON_ENTER)
        self.config['myConfig']['IN_TX_DISTORT'] = str(configuration.IN_TX_DISTORT)
        self.config['myConfig']['IN_RX_UNDISTORT'] = str(configuration.IN_RX_UNDISTORT)
        self.config['myConfig']['IN_TX_SCRAMBLE'] = str(configuration.IN_TX_SCRAMBLE)
        self.config['myConfig']['OUT_RX_HEAR_VOICE'] = str(configuration.OUT_RX_HEAR_VOICE)
        self.config['myConfig']['TRANSMIT_IN_TX_VOICE'] = str(configuration.TRANSMIT_IN_TX_VOICE)
        # audio settings:
        self.config['myConfig']['CURRENT_FREQUENCY_CHANNEL'] = str(audioSettings.CURRENT_FREQUENCY_CHANNEL)
        self.config['myConfig']['SAMPLING_FREQUENCY'] = str(audioSettings.SAMPLING_FREQUENCY)
        self.config['myConfig']['TELEGRAM_MAX_LEN_BYTES'] = str(audioSettings.TELEGRAM_MAX_LEN_BYTES)
        self.config['myConfig']['MAX_NR_OF_CHUNKS_PER_TELEGRAM'] = str(audioSettings.MAX_NR_OF_CHUNKS_PER_TELEGRAM)
        self.config['myConfig']['AMPLITUDE'] = str(audioSettings.AMPLITUDE)
        self.config['myConfig']['FFT_DETECTION_LEVEL'] = str(audioSettings.FFT_DETECTION_LEVEL)
        self.config['myConfig']['CHANNEL_DELAY_MS'] = str(audioSettings.CHANNEL_DELAY_MS)
        self.config['myConfig']['MAX_RESENDS'] = str(audioSettings.MAX_RESENDS)
        self.config['myConfig']['CARRIER_FREQUENCY_HZ'] = str(audioSettings.CARRIER_FREQUENCY_HZ)
        self.config['myConfig']['CARRIER_AMPLITUDE'] = str(audioSettings.CARRIER_AMPLITUDE)
        self.config['myConfig']['ADD_CARRIER'] = str(audioSettings.ADD_CARRIER)
        self.config['myConfig']['REMOVE_RX_CARRIER'] = str(audioSettings.REMOVE_RX_CARRIER)
        
        with open(filename, 'w') as configfile:
            # write new settings into file
            self.config.write(configfile)
            # from now on we have a new filename
            self.config_filename = filename

    
    




