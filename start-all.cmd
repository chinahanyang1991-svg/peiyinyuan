@echo off
chcp 65001 >nul
:: Thin shell of the ONLY launcher implementation (start_all.py); no second startup logic here.
:: For daily use double-click the Chinese-named entry in this folder (same implementation).
title PY-Launcher
echo [唯一入口] 旧三枚 start.bat / start.ps1 / start_gui.py 已于 2026-09-27 下线；今后只需双击 一键启动.bat（与本文件同一条实现）。
setlocal
set "PYY=%~dp0"
cd /d "%PYY%"
python --version >nul 2>nul
if errorlevel 1 (
  echo [ERR] PATH 上找不到 python。请先装 Python 3.11 并加入 PATH。
  pause
  exit /b 2
)
python -X utf8 "%PYY%start_all.py" %*
set "RC=%ERRORLEVEL%"
echo.
echo [start-all] exit=%RC%
if not "%PYY_NO_PAUSE%"=="1" if "%1"=="" (
  echo （日志目录 %PYY%logs\ ；三条腿各自一枚带标题的窗口）
)
endlocal & exit /b %RC%
