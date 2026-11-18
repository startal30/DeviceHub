@echo off
REM Tests, builds and installs Device Hub to %LOCALAPPDATA%\Programs\DeviceHub (no Python needed to run it).
REM Usage: build.bat            (pauses at the end)
REM        build.bat /nopause
cd /d "%~dp0"
set "DEST=%LOCALAPPDATA%\Programs\DeviceHub"

python -m unittest discover tests || goto :err
python -m pip install --user -r requirements.txt pyinstaller || goto :err
python -m PyInstaller --noconfirm --clean --onedir --noconsole --name DeviceHub ^
    --icon assets\devicehub.ico --hidden-import pystray._win32 device_hub.py || goto :err

REM Close a running copy so its files can be replaced
taskkill /im DeviceHub.exe /f >nul 2>&1
robocopy dist\DeviceHub "%DEST%" /MIR /NFL /NDL /NJH /NJS /NP >nul
if %errorlevel% geq 8 goto :err

echo.
echo Installed: %DEST%\DeviceHub.exe
echo Start it once; "Start with Windows" then points at this exe automatically.
if /i not "%~1"=="/nopause" pause
exit /b 0
:err
echo Build failed.
if /i not "%~1"=="/nopause" pause
exit /b 1
