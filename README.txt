
Instructions to install AC4QGP (2021.02.27): 

********************************************************************************************************
WARNING:
step 7) for the generation of an executable file for windows 10 (of type .exe) is not yet finished,
but it will be provided soon (tested with Python 3.7 and 3.9).

NOTE: 
on Linux you can run Python scripts directly.
********************************************************************************************************

1) install eric6 IDE and all required dependencies (like PyQt5), check the file requirements.txt
    (configure your environment as required: pip install -r requirements.txt)
    (pipreqs may not catch all dependencies, thus you may need to do also: pip install playsound
    sudo apt-get install python3-tk, then test: python3 -c "import tkinter")
    
2) configure config.ini as required
    IMPORTANT: Note that the default values are just an example and need to be adapted to your specific conditions.
 
3) in folder /backups you find some files you need to replace in the corresponding installation paths:
    installation_path\Python37\Lib\site-packages\pyshark\capture\capture.py
    installation_path\Python37\Lib\site-packages\gmplot\gmplot.py # the interface has been extended!
    installation_path\Python37\Lib\site-packages\pyshark\tshark\tshark.py
    # installation_path/Python37/Lib/site-packages/PyQt5/ __init__.py # bug solved with new version, don't need to adapt

4) obtain the files as described in todo.txt in the following folders:
    dist\PyQt5\Qt\bin
    dist\_sounddevice_data\portaudio-binaries

5) (*** not needed for now! *** Only needed after features which uses sockets are implemented)
   for the next steps you shall check your antivirus settings.
     Some antiviruses may block AC4QGP. Make sure you add it to the exception list of your antivirus if required.
     In rare cases you may also need to check your firewall settings.

6) in Eric6, go to Project -> new Project 
     -> in project folder select the path with the sources 
     -> in main file select AC4QGP.py
     -> press OK
     -> IDE will ask something like "add existing files to project?"
     -> press YES, then OK 
     (do this only in case of problems: -> go to Forms tab, right-mouse-click on ui/mainWindow.ui -> translate Form)
     Press the button "Execute project", then hit OK

7) (*** not yet working!? ***)
    generate an executable file:
     double click on gen_exe_with_pyinstaller.bat
     (the generated file AC4QGP.exe will be inside folder /dist - you can now execute it)
     (note that there is an own config.ini file for the executable)

8) How to use tool: check the Code Project article AC4QGP:
    TODO: put link here: https://www.codeproject.com/Articles/.../AC4QGP



