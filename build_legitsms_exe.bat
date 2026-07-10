@echo off
setlocal

REM Build Windows EXE for LegitSMS tool
REM Run this on Windows CMD in the project folder.

python -m pip install --upgrade pip
python -m pip install pyinstaller

pyinstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name LegitSMS-Tool ^
  legit_sms_tool.py

echo.
echo Build finished.
echo EXE path: dist\LegitSMS-Tool.exe
pause

