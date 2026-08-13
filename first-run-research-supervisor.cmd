@echo off
setlocal
wscript.exe "%~dp0Research Supervisor.vbs" -FirstRun
if errorlevel 1 pause
