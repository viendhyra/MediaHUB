#!/usr/bin/env bash
set -Eeuo pipefail

[[ $EUID -eq 0 ]] || { echo "Запусти установщик от root."; exit 1; }

APPDIR="/opt/mediahub"
DATADIR="/var/lib/mediahub"
PORT="${MEDIAHUB_PORT:-8090}"
SRC="$(cd "$(dirname "$0")" && pwd)"
PACKAGE_VERSION="$(tr -d '\r\n' < "$SRC/VERSION.txt")"

printf '\n======================================================\n'
printf ' MediaHub v%s Library Sets\n' "$PACKAGE_VERSION"
printf ' Устанавливается только MediaHub. Медиа-стек ставится из Web UI.\n'
printf '======================================================\n\n'

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ca-certificates curl ffmpeg git util-linux >/dev/null
# adb нужен только для установки приложения на ТВ из настроек; без него портал работает.
apt-get install -y -qq adb >/dev/null || echo "Предупреждение: пакет adb не установлен, установка на ТВ из настроек поставит его позже."

install -d -m 755 "$APPDIR" "$APPDIR/templates" "$APPDIR/static"
install -d -m 755 "$DATADIR" "$DATADIR/setup"

install -m 644 "$SRC/app.py" "$APPDIR/app.py"
install -m 644 "$SRC/cache_refresh.py" "$APPDIR/cache_refresh.py"
install -m 644 "$SRC/download_organizer.py" "$APPDIR/download_organizer.py"
install -m 755 "$SRC/system_setup.py" "$APPDIR/system_setup.py"
install -m 644 "$SRC/requirements.txt" "$APPDIR/requirements.txt"
install -m 644 "$SRC/VERSION.txt" "$APPDIR/VERSION.txt"
install -m 644 "$SRC/CHANGELOG.md" "$APPDIR/CHANGELOG.md"
cp -a "$SRC/scripts" "$APPDIR/"
if [[ -d "$SRC/.git" ]]; then git -C "$SRC" rev-parse HEAD > "$APPDIR/COMMIT.txt"; fi
cat > /usr/local/sbin/mediahub-update <<'UPDATER'
#!/usr/bin/env bash
exec bash /opt/mediahub/scripts/update.sh "$@"
UPDATER
chmod 755 /usr/local/sbin/mediahub-update
rm -rf "$APPDIR/templates"
install -d -m 755 "$APPDIR/static"
cp -a "$SRC/templates" "$APPDIR/templates"
cp -a "$SRC/static/." "$APPDIR/static/"

if [[ ! -x "$APPDIR/venv/bin/python" ]]; then
  python3 -m venv "$APPDIR/venv"
fi
# pip через python -m: после автообновления venv собран в staging-папке, и у скрипта bin/pip битый shebang.
"$APPDIR/venv/bin/python" -m pip install -q --upgrade pip
"$APPDIR/venv/bin/python" -m pip install -q -r "$APPDIR/requirements.txt"

if [[ ! -f /etc/mediahub.env ]]; then
cat > /etc/mediahub.env <<'EOF'
# MediaHub core. External services are optional and installed from the Setup Center.
MEDIA_ROOT=/mnt/media
MOVIES_ROOT=/mnt/media/movies
TV_ROOT=/mnt/media/tv
ANIME_ROOT=/mnt/media/anime
INBOX_ROOT=/mnt/media/inbox
MEDIAHUB_CACHE_DB=/var/lib/mediahub/cache.db

RADARR_URL=http://127.0.0.1:7878
SONARR_URL=http://127.0.0.1:8989
PROWLARR_URL=http://127.0.0.1:9696
QBIT_URL=http://127.0.0.1:8080

RADARR_API_KEY=
SONARR_API_KEY=
PROWLARR_API_KEY=
QBIT_USER=
QBIT_PASS=
TMDB_API_KEY=
ANILIBERTY_DISCOVERY_URL=https://aniliberty.top/
TVMAZE_ENABLED=1
KINOPOISK_API_KEY=
KINOPOISK_API_URL=https://kinopoiskapiunofficial.tech
MEDIAHUB_OUTBOUND_PROXY=

# Setup Center system-changing actions are LAN/private-IP only by default.
# Set to 1 only if you deliberately place MediaHub behind your own secure access layer.
MEDIAHUB_ALLOW_PUBLIC_SETUP=0
EOF
else
  # v20 intentionally removes MediaHub Basic Auth. Keep all other existing settings.
  sed -i '/^MEDIAHUB_USER=/d;/^MEDIAHUB_PASS=/d' /etc/mediahub.env
  ensure_env(){ grep -q "^$1=" /etc/mediahub.env || echo "$1=$2" >> /etc/mediahub.env; }
  ensure_env MEDIA_ROOT /mnt/media
  ensure_env MOVIES_ROOT /mnt/media/movies
  ensure_env TV_ROOT /mnt/media/tv
  ensure_env ANIME_ROOT /mnt/media/anime
  ensure_env INBOX_ROOT /mnt/media/inbox
  ensure_env MEDIAHUB_CACHE_DB /var/lib/mediahub/cache.db
  ensure_env RADARR_URL http://127.0.0.1:7878
  ensure_env SONARR_URL http://127.0.0.1:8989
  ensure_env PROWLARR_URL http://127.0.0.1:9696
  ensure_env QBIT_URL http://127.0.0.1:8080
  ensure_env RADARR_API_KEY ''
  ensure_env SONARR_API_KEY ''
  ensure_env PROWLARR_API_KEY ''
  ensure_env QBIT_USER ''
  ensure_env QBIT_PASS ''
  ensure_env TMDB_API_KEY ''
  ensure_env ANILIBERTY_DISCOVERY_URL https://aniliberty.top/
  ensure_env TVMAZE_ENABLED 1
  ensure_env KINOPOISK_API_KEY ''
  ensure_env KINOPOISK_API_URL https://kinopoiskapiunofficial.tech
  ensure_env MEDIAHUB_OUTBOUND_PROXY ''
  ensure_env MEDIAHUB_ALLOW_PUBLIC_SETUP 0
fi
chmod 600 /etc/mediahub.env
grep -q '^MEDIAHUB_PORT=' /etc/mediahub.env || echo "MEDIAHUB_PORT=$PORT" >> /etc/mediahub.env

# v20.9: keep MediaHub's SQLite brain with the media storage whenever a real
# media root already exists. Fresh installs without configured storage stay in
# /var/lib/mediahub until the Storage Wizard creates/selects a media root.
systemctl stop mediahub.service 2>/dev/null || true
MEDIA_ROOT_VALUE="$(awk -F= '$1=="MEDIA_ROOT"{print substr($0,index($0,"=")+1)}' /etc/mediahub.env | tail -n1)"
MEDIA_ROOT_VALUE="${MEDIA_ROOT_VALUE:-/mnt/media}"
OLD_DB_VALUE="$(awk -F= '$1=="MEDIAHUB_CACHE_DB"{print substr($0,index($0,"=")+1)}' /etc/mediahub.env | tail -n1)"
OLD_DB_VALUE="${OLD_DB_VALUE:-/var/lib/mediahub/cache.db}"
if [[ -d "$MEDIA_ROOT_VALUE" ]] && { findmnt -rn -M "$MEDIA_ROOT_VALUE" >/dev/null 2>&1 || [[ -d "$MEDIA_ROOT_VALUE/movies" || -d "$MEDIA_ROOT_VALUE/tv" || -d "$MEDIA_ROOT_VALUE/anime" ]]; }; then
  NEW_DB_VALUE="$MEDIA_ROOT_VALUE/.mediahub/mediahub.db"
  install -d -m 700 "$MEDIA_ROOT_VALUE/.mediahub"
  if [[ "$OLD_DB_VALUE" != "$NEW_DB_VALUE" ]]; then
    python3 - "$OLD_DB_VALUE" "$NEW_DB_VALUE" <<'PYDB'
import sqlite3,sys
from pathlib import Path
src=Path(sys.argv[1]); dst=Path(sys.argv[2]); dst.parent.mkdir(parents=True,exist_ok=True)
if src.exists() and not dst.exists():
    a=sqlite3.connect(str(src),timeout=10); b=sqlite3.connect(str(dst),timeout=10)
    try:a.backup(b)
    finally:b.close();a.close()
elif not dst.exists():
    sqlite3.connect(str(dst)).close()
try:dst.chmod(0o600)
except Exception:pass
PYDB
  fi
  if grep -q '^MEDIAHUB_CACHE_DB=' /etc/mediahub.env; then
    sed -i "s#^MEDIAHUB_CACHE_DB=.*#MEDIAHUB_CACHE_DB=$NEW_DB_VALUE#" /etc/mediahub.env
  else
    echo "MEDIAHUB_CACHE_DB=$NEW_DB_VALUE" >> /etc/mediahub.env
  fi
  echo "База MediaHub: $NEW_DB_VALUE"
fi

# Migrate an already saved TMDB credential into the persistent root-only store.
python3 - <<'PYSECRET'
import json
from pathlib import Path
env=Path('/etc/mediahub.env')
secrets=Path('/var/lib/mediahub/secrets.json')
vals={}
try:
    for raw in env.read_text(encoding='utf-8',errors='replace').splitlines():
        line=raw.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        k,v=line.split('=',1)
        if k.strip() in {'TMDB_API_KEY','JELLYFIN_API_KEY','KINOPOISK_API_KEY','QBIT_PASS','MEDIAHUB_OUTBOUND_PROXY'} and v.strip():
            vals[k.strip()]=v.strip()
except Exception:
    pass
if vals:
    try:
        current=json.loads(secrets.read_text(encoding='utf-8')) if secrets.exists() else {}
        if not isinstance(current,dict): current={}
    except Exception:
        current={}
    current.update(vals)
    secrets.parent.mkdir(parents=True,exist_ok=True)
    tmp=secrets.with_suffix('.tmp')
    tmp.write_text(json.dumps(current,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    tmp.chmod(0o600); tmp.replace(secrets); secrets.chmod(0o600)
PYSECRET

# v18 and older could have used this helper as ExecStartPre. v20 never auto-starts the stack.
rm -f /usr/local/sbin/mediahub-start-stack

cat > /etc/systemd/system/mediahub.service <<EOF
[Unit]
Description=MediaHub Setup Center and Media Control Panel
After=network-online.target
Wants=network-online.target
RequiresMountsFor=$MEDIA_ROOT_VALUE

[Service]
Type=simple
WorkingDirectory=$APPDIR
EnvironmentFile=/etc/mediahub.env
ExecStart=$APPDIR/venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# v21.1 pre-flight: a syntax error must fail the installer here, while the old
# service is still running, instead of leaving a dead unit behind.
if ! "$APPDIR/venv/bin/python" -m compileall -q "$APPDIR/app.py" "$APPDIR/cache_refresh.py" \
      "$APPDIR/system_setup.py" "$APPDIR/download_organizer.py" >/dev/null; then
  echo "ОШИБКА: исходники MediaHub не компилируются, установка прервана" >&2
  "$APPDIR/venv/bin/python" -m compileall "$APPDIR/app.py" >&2 || true
  exit 1
fi

systemctl daemon-reload
systemctl enable mediahub.service >/dev/null
systemctl stop mediahub.service 2>/dev/null || true
# Remove a stale MediaHub uvicorn from an older manual/systemd launch if it survived the stop.
pkill -f "$APPDIR/venv/bin/uvicorn app:app.*--port $PORT" 2>/dev/null || true
systemctl reset-failed mediahub.service 2>/dev/null || true
systemctl start mediahub.service

EXPECTED_VERSION="$(tr -d '\r\n' < "$APPDIR/VERSION.txt")"
ACTIVE_VERSION=""
for _ in $(seq 1 30); do
  ACTIVE_VERSION="$(curl -fsS --max-time 2 http://127.0.0.1:$PORT/api/version 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' 2>/dev/null || true)"
  [[ "$ACTIVE_VERSION" == "$EXPECTED_VERSION" ]] && break
  sleep 1
done
if [[ "$ACTIVE_VERSION" != "$EXPECTED_VERSION" ]]; then
  echo "ОШИБКА: после обновления ожидалась версия $EXPECTED_VERSION, сервер отвечает: ${ACTIVE_VERSION:-нет ответа}" >&2
  echo "--- systemctl status mediahub ---" >&2
  systemctl --no-pager -l status mediahub.service >&2 || true
  echo "--- journalctl mediahub ---" >&2
  journalctl -u mediahub.service -n 80 --no-pager >&2 || true
  exit 1
fi

# Remove obsolete red diagnostics from old builds immediately, then refresh in
# the background with the new resilient networking code.
ACTIVE_DB_VALUE="$(awk -F= '$1=="MEDIAHUB_CACHE_DB"{print substr($0,index($0,"=")+1)}' /etc/mediahub.env | tail -n1)"
MEDIAHUB_CACHE_DB="${ACTIVE_DB_VALUE:-/var/lib/mediahub/cache.db}" "$APPDIR/venv/bin/python" - <<'PYCLEAN' || true
import os,sqlite3
path=os.environ.get('MEDIAHUB_CACHE_DB','/var/lib/mediahub/cache.db')
if os.path.exists(path):
    con=sqlite3.connect(path)
    try:
        con.execute("delete from source_state where source in ('AniList new','AniList popular','AniList upcoming','TMDB movies trending','TMDB tv trending','TMDB anime trending','TMDB anime upcoming','Jellyfin metadata')")
        con.execute("delete from catalog where mode in ('trending','upcoming')")
        con.commit()
    finally:
        con.close()
PYCLEAN
if systemctl cat mediahub-cache.service >/dev/null 2>&1; then
  systemctl start mediahub-cache.service >/dev/null 2>&1 || true
else
  set -a
  # shellcheck disable=SC1091
  source /etc/mediahub.env
  set +a
  nohup "$APPDIR/venv/bin/python" "$APPDIR/cache_refresh.py" >/var/lib/mediahub/cache-refresh-install.log 2>&1 &
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
IP="${IP:-127.0.0.1}"

echo
echo "======================================================"
echo " MEDIAHUB v$ACTIVE_VERSION: http://$IP:$PORT"
echo "======================================================"
echo "Активная версия подтверждена через API: $ACTIVE_VERSION"
echo "Установлен только MediaHub."
echo "Открой: Установка -> Хранилище и MediaPool"
echo "Сначала подготовь диск/пул или выбери существующую папку, затем устанавливай медиастек."
echo
echo "Первый вход: admin / admin. Смените пароль в Настройки -> Аккаунт. Не публикуйте порт $PORT напрямую в интернет."
echo "Системная установка из Web UI по умолчанию разрешена только из локальной/private сети."
