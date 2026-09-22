@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
set "taskPython=%~dp0.venv\Scripts\python.exe"
if not exist "%taskPython%" set "taskPython=python"
if not exist ".env" echo 提示：还没有 .env，请先运行「安装.cmd」并填入 API Key。
"%taskPython%" "%~dp0auto_cut.py" "%~1"
set taskExit=%errorlevel%
pause
exit /b %taskExit%
