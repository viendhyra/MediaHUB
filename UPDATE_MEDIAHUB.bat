@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0UPDATE_MEDIAHUB.ps1" %*
pause
