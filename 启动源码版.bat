@echo off
title BiliNotes 源码版
cd /d "%~dp0"

rem ============================================================
rem  源码版专用入口：直接跑当前目录下的代码，不走 dist 里的打包 exe
rem  （原来的「启动BiliNotes.bat」只要有 dist\BiliNotes\BiliNotes.exe
rem    就会优先启动旧打包版，所以调试改动时要用这个）
rem ============================================================

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo.
  echo   [错误] 找不到项目虚拟环境 .venv
  echo.
  echo   请先在当前目录执行：
  echo     python -m venv .venv
  echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

rem 固定源码版端口，和平时书签里的地址一致（8765 留给打包版）
set "BILINOTES_PORT=8766"

rem 用独立的单实例锁：打包版（dist 里的 exe）开着时，
rem 源码版不会被当成重复实例而只去唤起旧窗口，两者可以各跑各的
set "BILINOTES_LOCK_NAME=Local\BiliNotes-single-instance-dev"

"%PY%" -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
  echo.
  echo   [错误] .venv 里缺少依赖，请执行：
  echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

echo.
echo   ============================================
echo     BiliNotes 源码版
echo     http://127.0.0.1:8766
echo   ============================================
echo.
echo   正在启动，稍后会自动打开窗口...
echo   关闭这个黑窗口即退出程序。
echo.

"%PY%" -m app.main

echo.
echo   BiliNotes 已停止。
pause
