@echo off
setlocal
chcp 936 >nul
cd /d "%~dp0"

rem 统一使用项目自带的 .venv，避免依赖装到别的 Python 里导致功能缺失
set "VPY=%CD%\.venv\Scripts\python.exe"
set "M1=https://mirrors.tencent.com/pypi/simple"
set "M2=https://mirrors.aliyun.com/pypi/simple"
set "PIPOPT=-q --disable-pip-version-check --no-input --timeout 20 --retries 1"

if exist "%VPY%" goto CORE

echo [1/4] 首次运行，正在创建独立运行环境 .venv ...
echo       只需这一次，请稍候。

rem 依次尝试各种可能的 Python，有一个能用就接着往下走
call :TRY python -m venv .venv
if exist "%VPY%" goto CORE
call :TRY py -3 -m venv .venv
if exist "%VPY%" goto CORE
call :TRY python3 -m venv .venv
if exist "%VPY%" goto CORE
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
  call :TRY "%%D\python.exe" -m venv .venv
  if exist "%VPY%" goto CORE
)
for /d %%D in ("C:/Python3*") do (
  call :TRY "%%D\python.exe" -m venv .venv
  if exist "%VPY%" goto CORE
)
echo.
echo [错误] 没找到可用的 Python，运行环境创建失败。
echo.
echo        请到 python.org 下载安装 Python 3.9 或更高版本，
echo        安装时务必勾选 "Add Python to PATH"，然后重新双击本文件。
echo.
echo        如果明明装过 Python 却仍失败，多半是 py 启动器记着旧路径，
echo        可手动执行（把路径换成你的 python.exe）：
echo          C:/你的路径/python.exe -m venv .venv
echo.
pause
exit /b 1

:CORE
echo [2/4] 检查核心依赖（约 10MB）...
"%VPY%" -c "import requests, bs4, w3lib, lxml" >nul 2>nul
if not errorlevel 1 (
  echo       已就绪，跳过下载
  goto OPT
)
"%VPY%" -m pip install %PIPOPT% -i %M1% requests beautifulsoup4 w3lib lxml
if errorlevel 1 "%VPY%" -m pip install %PIPOPT% -i %M2% requests beautifulsoup4 w3lib lxml
if errorlevel 1 "%VPY%" -m pip install %PIPOPT% requests beautifulsoup4 w3lib lxml
"%VPY%" -c "import requests, bs4" >nul 2>nul
if errorlevel 1 (
  echo.
  echo [错误] 核心依赖下载失败，请检查网络后重试。
  echo        离线环境请先手动执行：
  echo          .venv\Scripts\python.exe -m pip install requests beautifulsoup4 w3lib lxml
  echo.
  pause
  exit /b 1
)

:OPT
echo [3/4] 检查桌面窗口支持（装不上会自动用浏览器打开）...
"%VPY%" -c "import webview" >nul 2>nul
if not errorlevel 1 (
  echo       已就绪，跳过下载
  goto RUN
)
"%VPY%" -m pip install %PIPOPT% -i %M1% pywebview
if errorlevel 1 "%VPY%" -m pip install %PIPOPT% -i %M2% pywebview

:RUN
echo [4/4] 启动Gecko ...
echo.
echo   界面马上打开。若弹出防火墙提示，请选择"允许访问"。
echo   关掉界面窗口即停止服务。
echo.
"%VPY%" gecko\ui\webui.py
if errorlevel 1 (
  echo.
  echo [错误] 启动失败。请把上面的提示内容发给我，我据此排查。
  echo.
  pause
  exit /b 1
)
echo.
echo   提示：想要浏览器渲染或 TLS 指纹伪装，
echo       再单独执行一次 安装可选增强.bat
echo.
pause
exit /b 0

:TRY
rem %1 起是完整命令，例如：python -m venv .venv
%* >nul 2>nul
if exist "%VPY%" exit /b 0
if exist .venv rd /q /s .venv
exit /b 1
