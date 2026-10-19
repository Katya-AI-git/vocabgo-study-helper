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
  for %%V in (3.13 3.12 3.11 3.10) do (
    if not exist "%PYTHON%" py -%%V -m venv .venv >nul 2>nul
  )
)
if exist "%PYTHON%" goto check_deps

where python >nul 2>nul
if not errorlevel 1 (
  python -m venv .venv >nul 2>nul
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

exit /b 0
