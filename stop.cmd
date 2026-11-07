@echo off
rem  Stop dsh-voice-bridge
setlocal
set "DIR=%~dp0"
if exist "%DIR%bridge.pid" (
  for /f "usebackq delims=" %%i in ("%DIR%bridge.pid") do (
    taskkill /PID %%i /F >nul 2>&1
  )
  del /q "%DIR%bridge.pid" >nul 2>&1
  echo Voice bridge stopped.
) else (
  powershell -NoProfile -WindowStyle Hidden -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*voice_bridge.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
  echo Voice bridge stopped.
)
ping -n 3 127.0.0.1 >nul
