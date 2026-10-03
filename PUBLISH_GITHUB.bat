@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0PUBLISH_GITHUB.ps1" %*
pause
