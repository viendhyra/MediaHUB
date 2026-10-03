#!/usr/bin/env bash
# Подключение старой установки к обновлениям GitHub, без повторной установки стека.
set -Eeuo pipefail
[[ $EUID -eq 0 && -f /opt/mediahub/app.py && -f /etc/mediahub.env ]] || { echo 'Нужен root на существующем Debian MediaHUB'; exit 1; }
apt-get update -qq
apt-get install -y -qq git curl python3 python3-venv util-linux ca-certificates ffmpeg
SOURCE=$(cd "$(dirname "$0")/.." && pwd)
COMMIT=$(git -C "$SOURCE" rev-parse HEAD)
bash "$SOURCE/scripts/update.sh" "$COMMIT"
cat > /usr/local/sbin/mediahub-update <<'EOF'
#!/usr/bin/env bash
exec bash /opt/mediahub/scripts/update.sh "$@"
EOF
chmod 755 /usr/local/sbin/mediahub-update
echo 'Готово. Следующие обновления доступны из портала или mediahub-update.'
