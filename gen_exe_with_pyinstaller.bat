python updateRequirements.py
pyinstaller --onefile --windowed AC4QGP.spec 
xcopy /y "requirements.txt" "dist\requirements.txt"
pause
