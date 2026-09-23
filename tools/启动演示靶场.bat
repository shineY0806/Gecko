@echo off
setlocal
chcp 936 >nul
cd /d "%~dp0"

rem 启动项目自带的练习靶场，供工具爬取练手
set "VPY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%VPY%" set "VPY=python"

echo.
echo   演示靶场已启动，地址： http://127.0.0.1:8000
echo   把这个地址填到工具的"目标网址"里即可开始练习。
echo   关掉本窗口即停止靶场。
echo.

"%VPY%" demo_server.py
pause
