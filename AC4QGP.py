# Note: dont remove the following comment. Its for translation:
# -*- coding: utf-8 -*-

from PyQt5 import QtWidgets
from PyQt5.QtGui import QFont
from ui.mainWindow import MainWindow
import sys
import configuration
import logging
import os
import configparser


def initConfig():
    # configuration parameters determined during initialization from .ini file:
    #########################################
    # script or .exe?
    runningScript = os.path.basename(__file__)
    # different relative paths depending if we debug the script or run the executable file
    if(runningScript=="AC4QGP.py"): 
        # .py script
        configuration.IS_SCRIPT = True 
        configuration.PATH_PREFIX = "./dist/"
    else:
        # .exe file
        configuration.IS_SCRIPT = False
        configuration.PATH_PREFIX = "./"
    print("AC4QGP.py: load config.init file.")
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
            if "LOGGING_LEVEL" in config["myConfig"]:
                configuration.LOGGING_LEVEL = config['myConfig']['LOGGING_LEVEL']
                print("LOGGING_LEVEL = ",  configuration.LOGGING_LEVEL)
    except (configparser.NoSectionError, configparser.MissingSectionHeaderError):
        print("Exception raised in AC4QGP.py trying to load config file!\n")
        pass
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
    # if the severity level is INFO, the logger will handle only INFO, WARNING, ERROR, and CRITICAL messages and will ignore DEBUG messages
    # log with more details, e.g. module name, etc.
    ### logging.basicConfig(format='%(asctime)s.%(msecs)03d %(levelname)s {%(module)s} [%(funcName)s] %(message)s', datefmt='%H:%M:%S', level=logging_level)
    # log with less details
    logging.basicConfig(format='%(asctime)s.%(msecs)03d %(message)s', datefmt='%H:%M:%S', level=logging_level)

def main():
    initConfig()
    app = QtWidgets.QApplication(sys.argv)
    app.setStyleSheet('QMainWindow{border-color: darkgray;border: 1px solid black;}')
    font = QFont()
    font.setPointSize(configuration.FONT_SIZE_APP)
    app.setFont(font) 
    ui = MainWindow(app)
    ui.show()
    ui.activateWindow() # to bring window to forderground
    # NOTE: this needs to be called in the "main loop":
    ui.plotThread()
    # NOTE: because of the previous call to ui.plotThread() this point is actually never reached
    sys.exit(app.exec_())

# call main()
if __name__=="__main__":
   main()
   
