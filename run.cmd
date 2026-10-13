@echo off
rem TG media picker - unified launcher.
rem
rem Shows a mode-selection screen first, then enters either API mode (online
rem picker, sign-in required) or local cache mode (read the tdata cache).
rem
rem ASCII-only on purpose: cmd.exe parses .cmd files using the console code page
rem (GBK on Chinese Windows), so non-ASCII text here gets mis-decoded and can
rem break parsing ("... is not recognized as an internal or external command").
rem
rem Launches pythonw.exe, so no console window is shown. The app redirects its
rem own stdout/stderr to logs\tg_picker.log, so startup errors and tracebacks
rem are still recorded there.
rem
rem All paths are resolved relative to this script's own directory (%~dp0).
setlocal
cd /d "%~dp0"

set "PYW=%~dp0.venv\Scripts\pythonw.exe"
set "APP=%~dp0launcher.py"

if not exist "%PYW%" (
    echo [ERROR] Virtual environment not found: .venv
    echo Please run the following in this folder first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

rem Detach so this launcher exits at once; pythonw shows no console window.
start "" "%PYW%" "%APP%"
endlocal