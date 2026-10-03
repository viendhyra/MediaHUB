#!/usr/bin/env bash
# Создаёт отдельную VM; физических дисков хоста не касается.
set -Eeuo pipefail
REPO=https://github.com/viendhyra/MediaHUB.git
[[ $EUID -eq 0 && -d /etc/pve ]] || { echo 'Запустите от root на Proxmox VE'; exit 1; }
for tool in qm pvesm pvesh curl sha512sum python3 flock ssh-keygen; do command -v "$tool" >/dev/null || { echo "Не найден $tool"; exit 1; }; done
exec 9>/run/mediahub-proxmox-install.lock; flock -n 9 || { echo 'Установщик уже работает'; exit 1; }
ask(){ local answer; read -r -p "$1 [$2]: " answer; printf '%s' "${answer:-$2}"; }
number(){ [[ "$1" =~ ^[0-9]+$ && ${#1} -le 6 ]] && ((10#$1 >= $2 && 10#$1 <= $3)); }
choose_storage(){
  local content=$1 selected; local -a choices
  pvesm status --content "$content" >&2
  mapfile -t choices < <(pvesm status --content "$content" | awk 'NR>1 && $3=="active" {print $1}')
  if ((${#choices[@]}==0)) && [[ "$content" == snippets ]]; then
    # Стандартный local обычно не имеет snippets: добавляем тип содержимого,
    # сохраняя все его текущие типы. Само изменение выполняется после CREATE.
    pvesh get /storage/local --output-format json | python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d.get("type")=="dir" else 1)' || return 1
    echo 'Для cloud-init будет добавлен тип snippets к хранилищу local.' >&2
    choices=(local)
  fi
  ((${#choices[@]})) || { echo "Нет активного хранилища с content=$content" >&2; return 1; }
  selected=$(ask "Хранилище $content" "${choices[0]}")
  printf '%s\n' "${choices[@]}" | grep -Fxq "$selected" || { echo 'Выбрано недоступное хранилище' >&2; return 1; }
  [[ "$selected" =~ ^[A-Za-z][A-Za-z0-9_-]*$ ]] || return 1
  printf '%s' "$selected"
}
echo 'MediaHUB • Debian 13 • отдельная VM • два новых виртуальных диска'
VMID=$(ask 'ID новой VM' "$(pvesh get /cluster/nextid)")
number "$VMID" 100 999999 || { echo 'Недопустимый ID'; exit 1; }
[[ ! -e /etc/pve/qemu-server/$VMID.conf && ! -e /etc/pve/lxc/$VMID.conf ]] || { echo 'ID занят'; exit 1; }
SYSTEM_STORAGE=$(choose_storage images)
DATA_STORAGE=$(choose_storage images)
SNIPPET_STORAGE=$(choose_storage snippets)
ROOT_GB=$(ask 'Системный диск, GB' 24); number "$ROOT_GB" 16 1024 || exit 1
DATA_GB=$(ask 'Диск медиатеки, GB' 100); number "$DATA_GB" 10 999999 || exit 1
CORES=$(ask 'Ядра CPU' 4); number "$CORES" 2 128 || exit 1
RAM=$(ask 'Оперативная память, MB' 4096); number "$RAM" 2048 999999 || exit 1
ip -br link show type bridge
BRIDGE=$(ask 'Сетевой мост' vmbr0)
[[ "$BRIDGE" =~ ^[A-Za-z0-9_-]+$ && -d /sys/class/net/$BRIDGE/bridge ]] || { echo 'Нет такого моста'; exit 1; }
IPCFG=$(ask 'IPv4: dhcp или адрес/маска' dhcp)
if [[ "$IPCFG" != dhcp ]]; then
  python3 - "$IPCFG" <<'PY'
import ipaddress,sys
ipaddress.IPv4Interface(sys.argv[1])
PY
  GW=$(ask 'IPv4 шлюз' '')
  python3 - "$GW" <<'PY'
import ipaddress,sys
ipaddress.IPv4Address(sys.argv[1])
PY
  IPCFG="ip=$IPCFG,gw=$GW"
else IPCFG=ip=dhcp; fi
DNS=$(ask 'DNS сервер IPv4' 1.1.1.1)
python3 - "$DNS" <<'PY'
import ipaddress,sys
ipaddress.IPv4Address(sys.argv[1])
PY
echo "Будет создана VM $VMID: $SYSTEM_STORAGE:$ROOT_GB GB + $DATA_STORAGE:$DATA_GB GB; $CORES CPU; $RAM MB; $BRIDGE; $IPCFG"
read -r -p 'Для создания введите CREATE: ' CONFIRM
[[ "$CONFIRM" == CREATE ]] || exit 0
if ! pvesm status --content snippets | awk 'NR>1 && $3=="active" {print $1}' | grep -Fxq "$SNIPPET_STORAGE"; then
  CONTENT=$(pvesh get "/storage/$SNIPPET_STORAGE" --output-format json | python3 -c 'import json,sys;d=json.load(sys.stdin);c=d.get("content","");print(c+",snippets" if c else "snippets")')
  pvesm set "$SNIPPET_STORAGE" --content "$CONTENT"
fi
mkdir -p /root/.ssh; chmod 700 /root/.ssh
SSHKEY=/root/.ssh/mediahub-$VMID
[[ ! -e "$SSHKEY" ]] || { echo 'SSH ключ с таким ID уже существует'; exit 1; }
ssh-keygen -q -t ed25519 -N '' -C "mediahub-$VMID" -f "$SSHKEY"
PUBKEY=$(cat "$SSHKEY.pub")
WORK=$(mktemp -d /var/tmp/mediahub-vm.XXXXXX)
trap 'rm -rf -- "$WORK"' EXIT
trap 'echo "Установка прервана. VM $VMID автоматически не удаляется. Проверьте Proxmox и cloud-init: /var/log/cloud-init-output.log" >&2' ERR
BASE=https://cloud.debian.org/images/cloud/trixie/latest
IMAGE=debian-13-genericcloud-amd64.qcow2
curl -fL --retry 3 "$BASE/$IMAGE" -o "$WORK/$IMAGE"
curl -fL --retry 3 "$BASE/SHA512SUMS" -o "$WORK/SHA512SUMS"
(cd "$WORK"; grep -E " [*]?$IMAGE\$" SHA512SUMS > image.sha512; [[ -s image.sha512 ]]; sha512sum -c image.sha512)
# Привязываем установку к конкретному коммиту, а не меняющейся ветке.
COMMIT=$(curl -fsSL --retry 3 https://api.github.com/repos/viendhyra/MediaHUB/commits/main | python3 -c 'import json,sys;print(json.load(sys.stdin)["sha"])')
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || exit 1
SNIPPET_VOL="$SNIPPET_STORAGE:snippets/mediahub-$VMID.yaml"
SNIPPET=$(pvesm path "$SNIPPET_VOL")
[[ ! -e "$SNIPPET" ]] || { echo 'Файл cloud-init уже существует'; exit 1; }
mkdir -p "$(dirname "$SNIPPET")"
cat > "$SNIPPET" <<EOF
#cloud-config
hostname: mediahub
manage_etc_hosts: true
timezone: Europe/Moscow
disable_root: true
ssh_pwauth: false
users:
  - name: mediahub
    lock_passwd: true
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - '$PUBKEY'
package_update: true
packages: [qemu-guest-agent, git, curl, ca-certificates, python3, e2fsprogs]
write_files:
  - path: /etc/mediahub-new-vm
    permissions: '0600'
    content: '$VMID'
runcmd:
  - [systemctl, enable, --now, qemu-guest-agent]
  - [bash, -c, 'git init /root/mediahub-source && cd /root/mediahub-source && git remote add origin $REPO && git fetch --depth 1 origin $COMMIT && git checkout --detach FETCH_HEAD && bash scripts/bootstrap.sh > /var/log/mediahub-bootstrap.log 2>&1']
EOF
chmod 600 "$SNIPPET"
qm create "$VMID" --name mediahub --memory "$RAM" --cores "$CORES" --cpu host --ostype l26 --scsihw virtio-scsi-single --net0 "virtio,bridge=$BRIDGE" --agent enabled=1 --onboot 1
qm set "$VMID" --scsi0 "$SYSTEM_STORAGE:0,import-from=$WORK/$IMAGE,discard=on,ssd=1"
qm resize "$VMID" scsi0 "${ROOT_GB}G"
qm set "$VMID" --scsi1 "$DATA_STORAGE:$DATA_GB,serial=mediahub-data,discard=on" --ide2 "$SYSTEM_STORAGE:cloudinit" --boot order=scsi0 --serial0 socket --vga serial0
qm set "$VMID" --ipconfig0 "$IPCFG" --nameserver "$DNS" --cicustom "user=$SNIPPET_VOL"
qm start "$VMID"
echo 'VM запущена. Установка служб может занять 10–30 минут.'
for ((i=0;i<180;i++)); do
  RESULT=$(qm guest exec "$VMID" --timeout 10 -- /bin/test -f /var/lib/mediahub/bootstrap-complete 2>/dev/null || true)
  if printf '%s' "$RESULT" | python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d.get("exited") and d.get("exitcode")==0 else 1)' 2>/dev/null; then
    echo 'MediaHUB установлен. Адреса VM:'
    qm guest cmd "$VMID" network-get-interfaces
    echo 'Откройте http://IPv4_МАШИНЫ:8090 • раздел Настройки → Приложения'
    echo "SSH с Proxmox: ssh -i $SSHKEY mediahub@IPv4_МАШИНЫ"
    exit 0
  fi
  if ((i % 6 == 0)); then echo "Ожидаю готовности VM $VMID… ($((i/6)) мин)"; fi
  sleep 10
done
echo "Не дождались завершения. VM сохранена; журнал: qm guest exec $VMID -- tail -n 80 /var/log/mediahub-bootstrap.log" >&2
exit 1
