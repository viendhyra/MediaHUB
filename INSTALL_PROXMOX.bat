@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0INSTALL_PROXMOX.ps1" %*
if errorlevel 1 echo Установка завершилась с ошибкой. Прочитайте сообщение выше.
pause
