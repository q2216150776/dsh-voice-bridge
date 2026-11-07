@echo off
rem ==========================================================================
rem  dsh-voice-bridge launcher
rem  Start it by double-clicking this file (NOT from a sandboxed terminal).
rem ==========================================================================
setlocal
set "DIR=%~dp0"
set "PYW="

rem 1) pythonw on PATH
where pythonw >nul 2>&1 && set "PYW=pythonw"

rem 2) py launcher
if not defined PYW (
  where pyw >nul 2>&1 && set "PYW=pyw"
)

rem 3) the Python runtime shipped with DeepSeek Harness
if not defined PYW (
  for %%P in ("%USERPROFILE%\.dsh\dsh-runtimes\*\dependencies\python\pythonw.exe") do (
    if exist "%%~P" set "PYW=%%~P"
  )
)

if not defined PYW (
  echo [ERROR] pythonw.exe not found.
  echo         Install Python 3.9+ or run this while DeepSeek Harness is installed.
  pause
  exit /b 1
)
if not exist "%DIR%voice_bridge.py" (
  echo [ERROR] voice_bridge.py not found next to this script.
  pause
  exit /b 1
)

rem --- stop a leftover instance (old code would keep the hook alive) ----------
powershell -NoProfile -WindowStyle Hidden -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*voice_bridge.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
ping -n 2 127.0.0.1 >nul

del /q "%DIR%status.txt" >nul 2>&1
start "" "%PYW%" "%DIR%voice_bridge.py"
ping -n 5 127.0.0.1 >nul

echo.
echo   ---- bridge status ----
if exist "%DIR%status.txt" (
  type "%DIR%status.txt"
) else (
  echo   NOT RUNNING - status.txt was not written.
  echo   Check crash.log in this folder.
)
echo   -----------------------
echo.
echo   Click the pet once, speak, click again.
echo.
if exist "%DIR%status.txt" ping -n 8 127.0.0.1 >nul
if not exist "%DIR%status.txt" pause
