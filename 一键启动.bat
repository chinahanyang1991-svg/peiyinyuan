@echo off
chcp 65001 >nul
:: PY one-click entry. This shell holds NO second startup logic:
:: it only hands the user to the single implementation (start-all.cmd -> start_all.py).
title PY-Launcher
setlocal
set "PYY=%~dp0"
echo [唯一入口] 旧三枚 start.bat / start.ps1 / start_gui.py 已于 2026-09-27 下线，今后只需双击这一枚。
call "%PYY%start-all.cmd" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
