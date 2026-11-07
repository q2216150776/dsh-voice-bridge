@echo off
rem  Restart the bridge (stop then start)
setlocal
call "%~dp0stop.cmd"
ping -n 2 127.0.0.1 >nul
call "%~dp0start.cmd"