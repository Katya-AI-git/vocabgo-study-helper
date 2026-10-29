@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal EnableExtensions
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHON=.venv\Scripts\python.exe"

if exist "%PYTHON%" goto check_deps

echo [*] 未找到本地 Python 环境，正在检测系统 Python...

if exist "runtime\python\python.exe" (
  echo [*] 使用项目内置 Python...
  "runtime\python\python.exe" -m venv .venv
  if exist "%PYTHON%" goto check_deps
)

where py >nul 2>nul
if not errorlevel 1 (
  for %%V in (3.12 3.11 3.10 3.13 3.14 3.15) do (
    if not exist "%PYTHON%" (
      rem Must verify tkinter exists BEFORE building the venv from this version.
      rem Lesson learned: checking "version exists" is NOT enough. A machine had
      rem Python 3.13 installed without tcl/tk (the official installer only adds
      rem it when "tcl/tk and IDLE" is ticked), so the venv built from it lacked
      rem tkinter and the GUI would not start -- with symptoms identical to the
      rem line-ending bug, which made it very hard to diagnose.
      rem tkinter cannot be installed via pip, so the only fix is another Python.
      rem NOTE: keep rem comments ASCII-only. A UTF-8 .bat with Chinese text in a
      rem "rem" line makes cmd mis-split the line and execute garbage. Use echo
      rem for any Chinese you want the user to see.
      py -%%V -c "import tkinter" >nul 2>nul
      if not errorlevel 1 (
        echo [*] 使用 Python %%V（已确认自带 tkinter）...
        py -%%V -m venv .venv >nul 2>nul
      )
    )
  )
)
if exist "%PYTHON%" goto check_deps

where python >nul 2>nul
if not errorlevel 1 (
  python -c "import tkinter" >nul 2>nul
  if not errorlevel 1 (
    echo [*] 使用 PATH 上的 python...
    python -m venv .venv >nul 2>nul
  ) else (
    echo [!] PATH 上的 python 没有 tkinter，跳过它（换用带 tkinter 的版本）
  )
)
if exist "%PYTHON%" goto check_deps

echo [*] 没有找到可用的 Python，准备自动下载官方 Python 3.12...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap_python.ps1"
if errorlevel 1 (
  echo [!] Python 自动下载或安装失败，请检查网络后重试
  exit /b 1
)

if not exist "runtime\python\python.exe" (
  echo [!] 官方 Python 安装完成后未找到 runtime\python\python.exe
  exit /b 1
)

echo [*] 正在创建项目虚拟环境...
"runtime\python\python.exe" -m venv .venv
if not exist "%PYTHON%" (
  echo [!] 创建项目虚拟环境失败
  exit /b 1
)

:check_deps
"%PYTHON%" -c "import requests, cryptography, qrcode, PIL" >nul 2>nul
if errorlevel 1 (
  echo [*] 正在安装运行依赖...
  "%PYTHON%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [!] 依赖安装失败，请检查网络后重试
    exit /b 1
  )
)

rem tkinter is a hard requirement for the GUI, and it CANNOT be installed via pip
rem (it ships with Python itself). So check it separately and tell the user exactly
rem how to fix it, instead of letting them stare at a window that flashes and dies.
rem NOTE: ASCII-only rem text -- see the note above about cmd and UTF-8 comments.
"%PYTHON%" -c "import tkinter" >nul 2>nul
if errorlevel 1 (
  echo.
  echo [!] 当前 Python 没有 tkinter，图形界面无法启动。
  echo     原因：安装 Python 时没勾选 "tcl/tk and IDLE" 组件。
  echo     处理：重装官方 Python 并勾上该组件（或用 py -3.12 这类自带 tkinter 的版本），
  echo           然后删掉本目录下的 .venv 文件夹，重新双击 启动.bat。
  echo.
  exit /b 1
)

exit /b 0
