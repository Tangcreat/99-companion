@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined PY (
  echo 没有找到 Python。请先装 Python 3.10+ 并勾选 Add to PATH。
  pause
  exit /b 1
)

echo 正在启动… 浏览器会自动打开 http://127.0.0.1:8787
start "" http://127.0.0.1:8787
"%PY%" server.py
pause
