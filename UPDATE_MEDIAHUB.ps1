param([string]$Server, [string]$User = 'root')
$ErrorActionPreference = 'Stop'
try {
    if (-not $Server) { $Server = Read-Host 'IP Debian MediaHUB (не Proxmox)' }
    if ($Server -notmatch '^[a-zA-Z0-9][a-zA-Z0-9.-]*$' -or $User -notmatch '^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$') { throw 'Неверный адрес или пользователь' }
    & ssh -t "${User}@${Server}" 'sudo /usr/local/sbin/mediahub-update'
    if ($LASTEXITCODE -ne 0) { throw 'Обновление завершилось с ошибкой' }
} catch { Write-Error $_; exit 1 }
