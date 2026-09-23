@echo off
chcp 936 >nul
cd /d "%~dp0"

rem ============================================================
rem  CloudPulse 靶场一键启动脚本
rem   双击直接启动；命令行加 --reset 可重建练习数据
rem ============================================================

rem ---- 1. 定位 Node ----
set "NODE_EXE=node"
where node >nul 2>nul
if errorlevel 1 set "NODE_EXE="
if not defined NODE_EXE if exist "C:/Users/%USERNAME%/.workbuddy/binaries/node/versions/22.22.2-3/node.exe" set "NODE_EXE=C:/Users/%USERNAME%/.workbuddy/binaries/node/versions/22.22.2-3/node.exe"

if defined NODE_EXE goto :START_NODE

echo.
echo   [错误] 没有找到 Node.js
echo   请先安装 Node 18 或更高版本: https://nodejs.org
echo   安装后在命令行执行:  cd CloudPulse\server  ^&^& npm install
echo.
pause
exit /b 1

:START_NODE
rem ---- 2. 依赖自检 ----
if not exist "CloudPulse\server\node_modules" goto :NEED_INSTALL
if not exist "CloudPulse\web\dist" goto :NEED_BUILD
if not exist "CloudPulse\data\range.db" goto :NEED_DB
goto :READY

:NEED_INSTALL
echo.
echo   [提示] 后端依赖未安装，请先执行:
echo         cd CloudPulse\server
echo         npm install
echo.
pause
exit /b 1

:NEED_BUILD
echo.
echo   [提示] 前端未构建，请先执行:
echo         cd CloudPulse\web
echo         npm install
echo         npm run build
echo.
pause
exit /b 1

:NEED_DB
echo.
echo   [提示] 缺少数据库 CloudPulse\data\range.db
echo         请把 CloudPulse靶场-数据库.zip 里的 range.db 解压到该位置。
echo.
pause
exit /b 1

:READY
rem ---- 3. 可选：重建数据 ----
if not "%1"=="--reset" goto :LAUNCH
echo.
echo   [提示] 正在重建练习数据，请稍候...
"%NODE_EXE%" "CloudPulse\tools\gen-data.js" --force

:LAUNCH
echo.
echo   CloudPulse 靶场启动中...
echo.
echo     门户首页 : http://127.0.0.1:8050
echo     后台入口 : http://127.0.0.1:8050/admin
echo     测试账号 : admin / Admin@123
echo.
echo   关掉本窗口即停止靶场。
echo.

cd /d "%~dp0CloudPulse\server"
"%NODE_EXE%" app.js
pause
