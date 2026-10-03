#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo 'Запустите от root внутри Debian MediaHUB'; exit 1; }
echo 'Будет удалён пакет Jellyfin. Медиатека и её история MediaHUB сохранятся.'
read -r -p 'Для удаления введите REMOVE-JELLYFIN: ' answer
[[ "$answer" == REMOVE-JELLYFIN ]] || exit 0
backup=/var/backups/mediahub/jellyfin-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$backup"; chmod 700 "$backup"
systemctl stop jellyfin.service 2>/dev/null || true
for dir in /etc/jellyfin /var/lib/jellyfin; do [[ ! -d "$dir" ]] || cp -a "$dir" "$backup/"; done
systemctl disable jellyfin.service 2>/dev/null || true
apt-get remove -y jellyfin jellyfin-server jellyfin-web
# ffmpeg MediaHUB — отдельный пакет; autoremove и purge не выполняются.
echo "Готово. Копия настроек и базы Jellyfin: $backup"
