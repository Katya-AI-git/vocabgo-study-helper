@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHON=.venv\Scripts\python.exe"

call "%~dp0prepare.bat"
if errorlevel 1 (
  pause
  exit /b 1
)

echo [*] 启动图形界面...
"%PYTHON%" vocabgo_app.py %*
if errorlevel 1 pause
