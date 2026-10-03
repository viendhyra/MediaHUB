# Установка MediaHUB

## Отдельная VM в Proxmox

1. Убедитесь, что Proxmox VE 8/9 работает на x86-64, SSH доступен и в хранилище есть место. Для DHCP нужен DHCP-сервер в сети выбранного моста.
2. Из Windows распакуйте ZIP и запустите `INSTALL_PROXMOX.bat`. Или скачайте `scripts/proxmox-install.sh` и выполните его от root в Shell Proxmox.
3. Выберите свободный ID VM и два хранилища с типом `images`. Они могут совпадать. Отдельно выбирается файловое хранилище `snippets` для cloud-init; стандартный `local` при необходимости дополняется этим типом.
4. Задайте CPU, RAM и объёмы новых виртуальных дисков. Укажите мост (обычно `vmbr0`). Для статического IP введите адрес с маской, шлюз и DNS. Для автоматического адреса оставьте `dhcp`.
5. Проверьте сводку и введите `CREATE`. Отмена до этой точки не создаёт VM и не меняет конфигурацию хранилищ.
6. Дождитесь установки. Откройте `http://IP_VM:8090`.

В VM используется Debian 13 genericcloud. Новая файловая система ext4 создаётся только на диске с serial `mediahub-data`, назначенном этому установщику; системный диск и другие диски не выбираются автоматически для форматирования. Диск медиатеки монтируется по UUID в `/mnt/media`, база хранится в `/mnt/media/.mediahub/mediahub.db`.

Для доступа администратора создаётся отдельный ключ `/root/.ssh/mediahub-ID_VM` на Proxmox. Приватный ключ в репозиторий и VM не передаётся. В VM доступен пользователь `mediahub` с sudo, пароли SSH выключены:

```bash
ssh -i /root/.ssh/mediahub-ID_VM mediahub@IP_VM
```

Команды гостевого агента на Proxmox:

```bash
qm guest cmd ID_VM network-get-interfaces
qm guest exec ID_VM -- tail -n 80 /var/log/mediahub-bootstrap.log
qm guest exec ID_VM -- tail -n 80 /var/log/cloud-init-output.log
```

Если истёк таймаут, VM не удаляется. Проверьте сеть, DNS, свободное место и журналы. Повторный запуск мастера с тем же ID прекращается с сообщением «ID занят» — он не перезаписывает существующую VM. Если требуется повторить bootstrap внутри уже созданной VM, сначала выясните причину ошибки; маркер новой VM и проверка диска предотвращают повторное форматирование.

## Уже установленный Debian

Минимум: Debian 12/13, systemd, Python 3.9+, FFmpeg и доступ root. Установка вне Proxmox не создаёт и не форматирует диски:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/viendhyra/MediaHUB.git
cd MediaHUB
sudo bash install.sh
```

В этом варианте сначала устанавливается портал. Затем в **Настройки → Хранилище, компоненты и обслуживание** выберите существующую папку/точку монтирования и установите комплект. Для новой VM эти действия уже выполняет bootstrap.

Старую рабочую установку обновляйте по [инструкции перехода](UPDATES_RU.md), которая создаёт резервную копию перед заменой кода.

## Диагностика в Debian

```bash
systemctl status mediahub --no-pager
journalctl -u mediahub -n 100 --no-pager
cat /opt/mediahub/VERSION.txt
curl -fsS http://127.0.0.1:8090/api/version
```

Проверьте `qBittorrent`, `Radarr`, `Sonarr`, `Prowlarr` в настройках портала. Для ручного повторного подключения: `sudo /opt/mediahub/venv/bin/python /opt/mediahub/system_setup.py connect`, затем `sudo systemctl restart mediahub`.

Официальные справочники: [cloud-init Proxmox](https://pve.proxmox.com/wiki/Cloud-Init_Support), [qm](https://pve.proxmox.com/pve-docs/qm.1.html), [образы Debian](https://cloud.debian.org/images/cloud/trixie/latest/).
