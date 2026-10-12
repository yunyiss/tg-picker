@echo off
rem Telegram 缓存挑选器 —— 直接读 tdata 缓存，边看边挑边保存
rem 双击即可运行。用 pythonw 启动（无控制台窗口）；所有路径都以本脚本所在目录
rem (%~dp0) 为基准，因此不依赖当前工作目录，从任何位置调用都一致。
setlocal
cd /d "%~dp0"

set "PYW=%~dp0.venv\Scripts\pythonw.exe"
set "APP=%~dp0cache_picker.py"

if not exist "%PYW%" (
    echo [错误/Error] 未找到虚拟环境 .venv / virtual environment .venv not found
    echo 请先执行 / please run the following first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

rem pythonw = 无控制台窗口；start 让启动器立刻退出，应用在后台运行
start "" "%PYW%" "%APP%"
endlocal