#!/usr/bin/env bash
# Проверяемая замена кода с резервной копией и откатом после ошибки запуска.
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo 'Нужен root'; exit 1; }
APP=/opt/mediahub
STATE=/var/lib/mediahub/update.json
LOG=/var/lib/mediahub/update.log
mkdir -p /var/lib/mediahub /var/backups/mediahub /var/lib/mediahub/setup
exec 9>/run/mediahub-maintenance.lock; flock -n 9 || { echo 'Обновление уже выполняется'; exit 1; }
exec > >(tee -a "$LOG") 2>&1
state(){ python3 - "$STATE" "$1" "$2" <<'PY'
import json,sys,time,pathlib
p=pathlib.Path(sys.argv[1]);t=p.with_suffix('.tmp')
t.write_text(json.dumps(dict(status=sys.argv[2],message=sys.argv[3],updatedAt=time.time()),ensure_ascii=False));t.replace(p)
PY
}
exec 8>/var/lib/mediahub/setup/install.lock; flock -n 8 || { state error 'Дождитесь установки или подключения компонентов'; exit 1; }
COMMIT=${1:-}
if [[ -z "$COMMIT" ]]; then
  COMMIT=$(curl -fsSL --retry 3 https://api.github.com/repos/viendhyra/MediaHUB/commits/main | python3 -c 'import json,sys;print(json.load(sys.stdin)["sha"])') || { state error 'GitHub недоступен'; exit 1; }
fi
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || { state error 'Некорректный коммит'; exit 1; }
WORK=$(mktemp -d /opt/.mediahub-update.XXXXXX)
BACKUP=/var/backups/mediahub/$(date -u +%Y%m%dT%H%M%SZ)-${COMMIT:0:8}
OLD="$WORK/previous"
SWAPPED=0; STOPPED=0; QUIESCED=0
ACTIVE_UNITS=()
resume_units(){ for unit in "${ACTIVE_UNITS[@]}"; do systemctl start --no-block "$unit" || true; done; }
recover(){
  local code=$?; trap - ERR; set +e
  if ((SWAPPED)); then
    systemctl stop mediahub.service
    mv "$APP" "$WORK/failed"
    mv "$OLD" "$APP"
    python3 - "$BACKUP" <<'PY'
import pathlib,sys,sqlite3
b=pathlib.Path(sys.argv[1]);p=pathlib.Path((b/'database-path.txt').read_text())
if (b/'mediahub.db').exists():
    a=sqlite3.connect(str(b/'mediahub.db'));c=sqlite3.connect(str(p))
    try:a.backup(c)
    finally:c.close();a.close()
PY
    if [[ -f "$BACKUP/launch.conf" ]]; then cp "$BACKUP/launch.conf" /etc/systemd/system/mediahub.service.d/launch.conf; else rm -f /etc/systemd/system/mediahub.service.d/launch.conf; fi
    systemctl daemon-reload
  fi
  if ((STOPPED)); then systemctl start mediahub.service; fi
  if ((QUIESCED)); then resume_units; fi
  state error "Обновление не завершено; проверьте $LOG. Резервная копия: $BACKUP"
  echo "Ошибка; прежний код восстановлен, если замена уже началась. $BACKUP"
  exit "$code"
}
trap recover ERR
trap 'rm -rf -- "$WORK"' EXIT
state running 'Скачиваю и проверяю новую версию'
git init -q "$WORK/source"
git -C "$WORK/source" remote add origin https://github.com/viendhyra/MediaHUB.git
git -C "$WORK/source" fetch -q --depth 1 origin "$COMMIT"
git -C "$WORK/source" checkout -q --detach FETCH_HEAD
SOURCE="$WORK/source"
[[ "$(git -C "$SOURCE" rev-parse HEAD)" == "$COMMIT" ]]
for file in app.py cache_refresh.py system_setup.py download_organizer.py VERSION.txt requirements.txt templates/index.html static/app.css scripts/update.sh; do [[ -f "$SOURCE/$file" ]]; done
VERSION=$(tr -d '\r\n' < "$SOURCE/VERSION.txt")
[[ "$VERSION" =~ ^[0-9]+(\.[0-9]+){1,3}$ ]]
python3 -m compileall -q "$SOURCE/app.py" "$SOURCE/cache_refresh.py" "$SOURCE/system_setup.py" "$SOURCE/download_organizer.py"
# venv содержит абсолютные пути: строим сразу по окончательному пути staging и
# запускаем через python -m uvicorn (без перемещённых entrypoint-скриптов).
python3 -m venv "$SOURCE/venv"
"$SOURCE/venv/bin/python" -m pip install -q -r "$SOURCE/requirements.txt"
"$SOURCE/venv/bin/python" -c 'import fastapi,httpx,jinja2,multipart,uvicorn'
[[ -d "$APP" && -f /etc/mediahub.env ]] || { echo 'Сначала установите MediaHUB'; false; }
# Не теряем вручную загруженные APK и локальные static-файлы.
cp -an "$APP/static/." "$SOURCE/static/"
git -C "$SOURCE" rev-parse HEAD > "$SOURCE/COMMIT.txt"
mkdir -p "$BACKUP"
chmod 700 "$BACKUP"
state running 'Сохраняю код, настройки и базу'
for unit in mediahub-cache.timer mediahub-local-cache.timer mediahub-organizer.timer mediahub-cache.service mediahub-local-cache.service mediahub-organizer.service; do
  if systemctl is-active --quiet "$unit"; then ACTIVE_UNITS+=("$unit"); fi
done
QUIESCED=1
systemctl stop mediahub-cache.timer mediahub-local-cache.timer mediahub-organizer.timer 2>/dev/null || true
systemctl stop mediahub-cache.service mediahub-local-cache.service mediahub-organizer.service 2>/dev/null || true
STOPPED=1
systemctl stop mediahub.service
tar -czf "$BACKUP/code.tar.gz" --exclude=venv --exclude=__pycache__ -C "$APP" .
cp /etc/mediahub.env "$BACKUP/mediahub.env"
[[ ! -f /etc/systemd/system/mediahub.service.d/launch.conf ]] || cp /etc/systemd/system/mediahub.service.d/launch.conf "$BACKUP/launch.conf"
[[ ! -f /var/lib/mediahub/secrets.json ]] || cp /var/lib/mediahub/secrets.json "$BACKUP/secrets.json"
python3 - "$BACKUP" <<'PY'
import sqlite3,pathlib,sys
env=dict(line.split('=',1) for line in pathlib.Path('/etc/mediahub.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
p=pathlib.Path(env.get('MEDIAHUB_CACHE_DB','/var/lib/mediahub/cache.db'))
if p.exists():
    a=sqlite3.connect(str(p));b=sqlite3.connect(str(pathlib.Path(sys.argv[1])/'mediahub.db'))
    try:a.backup(b)
    finally:b.close();a.close()
(pathlib.Path(sys.argv[1])/'database-path.txt').write_text(str(p))
PY
cp -a "$APP" "$BACKUP/app"
mv "$APP" "$OLD"
SWAPPED=1
mv "$SOURCE" "$APP"
# Старые установки используют console entrypoint с путём /opt/mediahub/venv.
# Обновляем unit на module launch, чтобы новый venv не зависел от пути staging.
mkdir -p /etc/systemd/system/mediahub.service.d
PORT=$(awk -F= '$1=="MEDIAHUB_PORT" {print $2}' /etc/mediahub.env | tail -n1)
PORT=${PORT:-8090}
[[ "$PORT" =~ ^[0-9]+$ && "$PORT" -ge 1 && "$PORT" -le 65535 ]]
cat > /etc/systemd/system/mediahub.service.d/launch.conf <<EOF
[Service]
ExecStart=
ExecStart=/opt/mediahub/venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port $PORT
EOF
systemctl daemon-reload
systemctl start mediahub.service
for ((i=0;i<40;i++)); do
  ACTIVE=$(curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/version" 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])' 2>/dev/null || true)
  if [[ "$ACTIVE" == "$VERSION" ]]; then
    resume_units
    state complete "Установлена версия $VERSION. Резервная копия: $BACKUP"
    echo "MediaHUB $VERSION готов; резервная копия: $BACKUP"
    exit 0
  fi
  sleep 2
done
echo 'Новая версия не прошла проверку запуска'; false
