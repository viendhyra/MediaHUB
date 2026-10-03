param([string]$Message)
$ErrorActionPreference = 'Stop'
function Invoke-Git { & git @args; if ($LASTEXITCODE -ne 0) { throw 'Git сообщил об ошибке' } }
try {
    Set-Location $PSScriptRoot
    if (-not (Test-Path -LiteralPath '.git')) { throw 'Для публикации сначала git clone https://github.com/viendhyra/MediaHUB.git' }
    $remote = & git remote get-url origin
    if ($remote -notin @('https://github.com/viendhyra/MediaHUB.git','git@github.com:viendhyra/MediaHUB.git')) { throw 'origin не указывает на viendhyra/MediaHUB' }
    Invoke-Git fetch origin main
    Invoke-Git merge --ff-only origin/main
    # Только файлы проекта: личные конфиги и временные файлы не добавляются.
    $paths = @('app.py','cache_refresh.py','system_setup.py','download_organizer.py','requirements.txt','install.sh','CHECK_INSTALL.sh','BACKUP_MEDIAHUB.sh','mediahub.env.example','AGENTS.md','VERSION.txt','CHANGELOG.md','README.md','.gitignore','.gitattributes','.github','templates','static','scripts','docs','tests','INSTALL_PROXMOX.bat','INSTALL_PROXMOX.ps1','UPDATE_MEDIAHUB.bat','UPDATE_MEDIAHUB.ps1','PUBLISH_GITHUB.bat','PUBLISH_GITHUB.ps1')
    Invoke-Git add -- @paths
    & git diff --cached --quiet
    if ($LASTEXITCODE -eq 0) { Write-Host 'Нет изменений для публикации'; exit 0 }
    # Сначала показываем, что уйдёт на GitHub, и только потом спрашиваем подтверждение.
    Invoke-Git diff --cached --stat
    $version = (Get-Content -LiteralPath 'VERSION.txt' -Raw).Trim()
    if (-not $Message) {
        $Message = Read-Host "Описание коммита (Enter — «MediaHUB $version», n — отмена)"
        if ($Message -match '^(n|н|нет|no)$') { Invoke-Git reset -q; throw 'Публикация отменена' }
        if (-not $Message.Trim()) { $Message = "MediaHUB $version" }
    }
    Invoke-Git commit -m $Message
    Invoke-Git push origin HEAD:main
    Write-Host 'Опубликовано: https://github.com/viendhyra/MediaHUB'
} catch { Write-Error $_; exit 1 }
