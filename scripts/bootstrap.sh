#!/usr/bin/env bash
# Выполняется внутри новой Debian VM, никогда на хосте Proxmox.
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo 'Нужен root'; exit 1; }
[[ -f /etc/mediahub-new-vm ]] || { echo 'Нет маркера новой VM; форматирование запрещено'; exit 1; }
DISK=/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_mediahub-data
if [[ ! -b "$DISK" ]]; then
  mapfile -t candidates < <(lsblk -dn -o PATH,SERIAL | awk '$2=="mediahub-data" {print $1}')
  ((${#candidates[@]}==1)) || { echo 'Не найден единственный диск serial=mediahub-data'; exit 1; }
  DISK=${candidates[0]}
fi
[[ -b "$DISK" ]] || { echo "Не найден новый диск: $DISK"; exit 1; }
# Только выделенный установщиком диск с уникальным serial. Повторный запуск не стирает данные.
if ! blkid "$DISK" >/dev/null 2>&1; then
  [[ -z "$(lsblk -nr -o MOUNTPOINT "$DISK" | tr -d '[:space:]')" ]] || exit 1
  [[ "$(lsblk -nr -o TYPE "$DISK" | wc -l)" -eq 1 ]] || { echo 'На диске есть разделы'; exit 1; }
  [[ -z "$(wipefs -n --noheadings "$DISK")" ]] || { echo 'Диск не пустой'; exit 1; }
  mkfs.ext4 -L MediaHUB "$DISK"
fi
[[ "$(blkid -s TYPE -o value "$DISK")" == ext4 && "$(blkid -s LABEL -o value "$DISK")" == MediaHUB ]] || exit 1
mkdir -p /mnt/media
UUID=$(blkid -s UUID -o value "$DISK")
grep -q "^UUID=$UUID " /etc/fstab || printf 'UUID=%s /mnt/media ext4 defaults 0 2\n' "$UUID" >> /etc/fstab
mountpoint -q /mnt/media || mount /mnt/media
cd "$(dirname "$0")/.."
bash install.sh
set -a; source /etc/mediahub.env; set +a
/opt/mediahub/venv/bin/python /opt/mediahub/system_setup.py install recommended
systemctl restart mediahub.service
curl -fsS --retry 20 --retry-connrefused --retry-delay 2 http://127.0.0.1:8090/api/version
touch /var/lib/mediahub/bootstrap-complete
rm -f /etc/mediahub-new-vm
