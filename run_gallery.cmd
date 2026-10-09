@echo off
rem 本地图片挑选器 —— 双击启动（无需联网、无需登录）
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [错误] 未找到虚拟环境 .venv
    echo 请先执行：
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

rem pythonw = 无控制台窗口，纯 GUI
start "" ".venv\Scripts\pythonw.exe" gallery_picker.py
endlocal
