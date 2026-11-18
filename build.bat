@echo off
REM Tests, builds and installs Device Hub to %LOCALAPPDATA%\Programs\DeviceHub (no Python needed to run it).
REM Usage: build.bat            (pauses at the end)
REM        build.bat /nopause
cd /d "%~dp0"
set "DEST=%LOCALAPPDATA%\Programs\DeviceHub"

python -m unittest discover tests || goto :err
python -m pip install --user -r requirements.txt pyinstaller==6.22.3 || goto :err
python -m PyInstaller --noconfirm --clean --onedir --noconsole --name DeviceHub ^
    --icon assets\devicehub.ico --hidden-import pystray._win32 device_hub.py || goto :err

REM Close a running copy so its files can be replaced, and give Windows a moment to release them.
REM /R:5 /W:2 = give up after 5 retries 2 s apart (robocopy's default is a million retries, 30 s apart).
taskkill /im DeviceHub.exe /f >nul 2>&1
REM (ping instead of timeout: timeout fails when there's no console input, e.g. when run from a script)
ping -n 3 127.0.0.1 >nul
robocopy dist\DeviceHub "%DEST%" /MIR /R:5 /W:2 /NFL /NDL /NJH /NJS /NP >nul
if %errorlevel% geq 8 (
    echo Couldn't replace the installed files. Exit Device Hub from its tray icon and run build.bat again.
    goto :err
)

REM Desktop shortcut (uses the real Desktop folder, even if OneDrive moved it)
powershell -NoProfile -Command "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Device Hub.lnk')); $s.TargetPath='%DEST%\DeviceHub.exe'; $s.WorkingDirectory='%DEST%'; $s.IconLocation='%DEST%\DeviceHub.exe,0'; $s.Description='Battery and settings for keyboard, mouse and headset'; $s.Save()" || goto :err

echo.
echo Installed: %DEST%\DeviceHub.exe
echo Start it once; "Start with Windows" then points at this exe automatically.
if /i not "%~1"=="/nopause" pause
exit /b 0
:err
echo Build failed.
if /i not "%~1"=="/nopause" pause
exit /b 1
