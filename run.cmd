@echo off
rem TG 图片挑选下载器 —— 启动脚本（首次运行会弹出登录窗口）
rem 保留控制台便于查看日志与报错；所有路径以本脚本所在目录 (%~dp0) 为基准。
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
set "APP=%~dp0tg_picker.py"

if not exist "%PY%" (
    echo [错误/Error] 未找到虚拟环境 .venv / virtual environment .venv not found
    echo 请先在本目录执行 / please run the following in this folder:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

rem 想完全无窗口可把 PY 换成 pythonw
"%PY%" "%APP%"
if errorlevel 1 pause
endlocal