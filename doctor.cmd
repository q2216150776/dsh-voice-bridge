@echo off
rem  Self-check: prints one line per capability and keeps the window open
setlocal
set "DIR=%~dp0"
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY ( where py >nul 2>&1 && set "PY=py" )
if not defined PY (
  for %%P in ("%USERPROFILE%\.dsh\dsh-runtimes\*\dependencies\python\python.exe") do (
    if exist "%%~P" set "PY=%%~P"
  )
)
if not defined PY (
  echo [ERROR] python.exe not found.
  pause
  exit /b 1
)
"%PY%" "%DIR%voice_bridge.py" --doctor
echo.
pause