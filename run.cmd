@echo off
rem TG 图片挑选下载器 —— 启动脚本（首次运行会弹出登录窗口）
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到虚拟环境 .venv
    echo 请先在本目录执行：
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

rem 有控制台便于看日志与报错；想完全无窗口可把 python 换成 pythonw
".venv\Scripts\python.exe" tg_picker.py
if errorlevel 1 pause
endlocal
