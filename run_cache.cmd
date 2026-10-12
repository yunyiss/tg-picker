@echo off
rem Telegram 缓存挑选器 —— 直接读 tdata 缓存，边看边挑边保存
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [错误/Error] 未找到虚拟环境 .venv / virtual environment .venv not found
    echo 请先执行 / please run the following first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

rem pythonw = 无控制台窗口
start "" ".venv\Scripts\pythonw.exe" cache_picker.py
endlocal
