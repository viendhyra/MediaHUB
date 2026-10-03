param([string]$Server)
$ErrorActionPreference = 'Stop'
try {
    if (-not $Server) { $Server = Read-Host 'IP или имя Proxmox (без https и порта 8006)' }
    if ($Server -notmatch '^[a-zA-Z0-9][a-zA-Z0-9.-]*$') { throw 'Неверный адрес Proxmox' }
    foreach ($tool in @('ssh','scp')) { if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "Включите OpenSSH Client Windows: нет $tool" } }
    $target = "root@$Server"
    $remote = '/root/mediahub-install-' + [guid]::NewGuid().ToString('N') + '.sh'
    Write-Host 'Подтвердите ключ SSH Proxmox. Пароль вводится в стандартном запросе SSH.'
    & scp -o ConnectTimeout=15 (Join-Path $PSScriptRoot 'scripts/proxmox-install.sh') "${target}:$remote"
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось отправить установщик' }
    & ssh -t -o ConnectTimeout=15 $target "bash $remote; result=`$?; rm -f $remote; exit `$result"
    if ($LASTEXITCODE -ne 0) { throw 'Установщик Proxmox сообщил об ошибке; созданная VM сохранена для диагностики' }
} catch { Write-Error $_; exit 1 }
