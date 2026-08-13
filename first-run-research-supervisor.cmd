@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch-research-supervisor.ps1" -FirstRun
if errorlevel 1 pause
