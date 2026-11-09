@echo off
chcp 65001 >nul 2>&1
title BiliNotes - Bilibili Note Generator
cd /d "%~dp0"

rem 有打包好的 exe 就直接用，没有再回退到 Python 源码模式
if exist "%~dp0dist\BiliNotes\BiliNotes.exe" (
  start "" "%~dp0dist\BiliNotes\BiliNotes.exe"
  exit /b 0
)

set "PY=C:\Users\86159\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo   ============================================
echo     BiliNotes  -  Bilibili Note Generator
echo   ============================================
echo.
echo   Starting service, your browser will open
echo   automatically in a few seconds.
echo.
echo   ** Close this window to quit the app **
echo.

"%PY%" -c "import fastapi, yt_dlp, rapidocr_onnxruntime" >nul 2>&1
if errorlevel 1 (
  echo   [ERROR] Python dependencies are missing.
  echo.
  echo   Please run in this folder:
  echo     pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

"%PY%" -m app.main

echo.
echo   BiliNotes stopped.
pause
