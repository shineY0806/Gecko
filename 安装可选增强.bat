@echo off
setlocal
chcp 936 >nul
cd /d "%~dp0"

rem 用途：桌面窗口、浏览器渲染、TLS 指纹伪装。主程序启动不需要它们
set "VPY=%CD%\.venv\Scripts\python.exe"
set "M1=https://mirrors.tencent.com/pypi/simple"
set "M2=https://mirrors.aliyun.com/pypi/simple"
set "PIPOPT=--disable-pip-version-check --no-input --timeout 30 --retries 2"

if not exist "%VPY%" (
  echo.
  echo [错误] 还没创建运行环境。请先双击 启动Gecko.bat 成功启动一次，
  echo        再回来执行本文件。
  echo.
  pause
  exit /b 1
)

echo [1/3] 桌面窗口支持 pywebview（不装就用浏览器打开）...
"%VPY%" -m pip install %PIPOPT% -i %M1% pywebview
if errorlevel 1 "%VPY%" -m pip install %PIPOPT% -i %M2% pywebview

echo [2/3] 指纹伪装库 curl_cffi（约 20MB）...
"%VPY%" -m pip install %PIPOPT% -i %M1% curl_cffi
if errorlevel 1 "%VPY%" -m pip install %PIPOPT% -i %M2% curl_cffi

echo [3/3] 浏览器渲染 playwright / patchright + Chromium 内核（约 150MB）...
"%VPY%" -m pip install %PIPOPT% -i %M1% playwright patchright
if errorlevel 1 "%VPY%" -m pip install %PIPOPT% -i %M2% playwright patchright
"%VPY%" -m playwright install chromium
"%VPY%" -m patchright install chromium

echo.
echo 全部完成。重启工具后，界面里的"浏览器渲染"就能选了。
echo.
pause
