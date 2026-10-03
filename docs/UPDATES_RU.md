# Обновления и восстановление

## Подключить установленный MediaHUB 21.x

Выполните **в Debian MediaHUB**, не на хосте Proxmox:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/viendhyra/MediaHUB.git mediahub-upgrade
cd mediahub-upgrade
sudo bash scripts/adopt.sh
```

Сценарий сначала скачивает и проверяет новую версию, строит отдельное Python-окружение, затем останавливает портал и фоновые задачи, копирует прежний код, настройки и базу. Медиатека не перемещается. Существующие qBittorrent/Servarr не переустанавливаются. Jellyfin больше не предлагается в портале, но его пакет автоматически не удаляется.

Следующие обновления:

```bash
sudo mediahub-update
```

Или **Настройки → Обновления**. Новости берутся из первой записи `CHANGELOG.md` того же коммита, что и версия. Проверки кэшируются на час; кнопка «Проверить» обновляет ответ. Без доступа к GitHub портал сообщает о проблеме, просмотр своих файлов продолжает работать.

## Замена файлов после доработки

1. В клоне проекта измените код и увеличьте числовую версию `VERSION.txt`.
2. В начало `CHANGELOG.md` добавьте новости для пользователя.
3. Выполните проверки и `PUBLISH_GITHUB.bat "Что изменилось"`.
4. На сервере выполните `sudo mediahub-update` либо установите обновление через портал.

Команда обновления скачивает конкретный коммит только из `viendhyra/MediaHUB`. В портале нельзя передать произвольный URL или shell-команду. Скрипт публикации использует обычный push без force; если ветка разошлась, остановится для разрешения конфликта.

## Что сохраняется

- `/mnt/media` — видео и структура медиатеки.
- `/etc/mediahub.env` — пути, ключи, настройки соединений.
- `/var/lib/mediahub` — состояние установки и локальные секреты.
- Локальные дополнительные static-файлы и APK.

Резервная копия каждого обновления находится в `/var/backups/mediahub/ДАТА-КОММИТ/`: `app/`, `code.tar.gz`, `mediahub.env`, `mediahub.db`, `database-path.txt`, при наличии `secrets.json` и `launch.conf`. Каталог доступен только root. Убедитесь, что для копии кода, окружения и базы достаточно места. Старые копии удаляются вручную после проверки.

Журналы: `/var/lib/mediahub/update.log`, `/var/lib/mediahub/update.json`, `journalctl -u mediahub-update`. При ошибке запуска нового кода сценарий возвращает старый код, прежнюю базу и конфигурацию запуска. Он не отменяет изменения сторонних сервисов или скачанные торренты.

## Ручное восстановление

Остановите портал и фоновые службы. Выберите нужную копию; не меняйте указанный путь на произвольный системный каталог:

```bash
sudo systemctl stop mediahub mediahub-cache.timer mediahub-local-cache.timer mediahub-organizer.timer
sudo systemctl stop mediahub-cache.service mediahub-local-cache.service mediahub-organizer.service
sudo mv /opt/mediahub /opt/mediahub-failed-$(date +%s)
sudo cp -a /var/backups/mediahub/ДАТА-КОММИТ/app /opt/mediahub
sudo systemctl start mediahub
sudo systemctl start mediahub-cache.timer mediahub-local-cache.timer mediahub-organizer.timer
```

Это восстанавливает код. Для восстановления истории и настроек используйте соответствующие `mediahub.db` и `mediahub.env` из той же копии, пока службы остановлены; актуальный путь базы записан в `database-path.txt`. Сначала сохраните текущую базу. Восстановление старой базы удаляет записи истории, появившиеся после её копирования.

## Удаление старого Jellyfin

В Debian MediaHUB после перехода:

```bash
sudo bash /opt/mediahub/scripts/remove-jellyfin.sh
```

Введите `REMOVE-JELLYFIN`. Скрипт остановит службу, сохранит `/etc/jellyfin` и `/var/lib/jellyfin`, удалит пакеты сервера и веб-интерфейса. Медиафайлы и база MediaHUB не удаляются. `purge` и `autoremove` не выполняются; FFmpeg MediaHUB остаётся отдельным пакетом.
