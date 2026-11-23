@echo off
setlocal
where py >nul 2>nul
if errorlevel 1 (
  python "%~dp0mirror.py" %*
) else (
  py -3 "%~dp0mirror.py" %*
)
set "mirror_status=%ERRORLEVEL%"
if "%~1"=="" if not defined CI pause
exit /b %mirror_status%
