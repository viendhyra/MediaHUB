#!/usr/bin/env bash
set +e
ok(){ printf '\033[32m[ OK ]\033[0m %s\n' "$*"; }
warn(){ printf '\033[33m[WARN]\033[0m %s\n' "$*"; }
bad(){ printf '\033[31m[FAIL]\033[0m %s\n' "$*"; }

VER=$(cat /opt/mediahub/VERSION.txt 2>/dev/null || echo unknown)
printf '\n=== MediaHub v%s check ===\n\n' "$VER"

if systemctl is-active --quiet mediahub.service; then ok 'mediahub.service active'; else bad 'mediahub.service not active'; fi
if systemctl is-enabled --quiet mediahub.service; then ok 'mediahub.service enabled'; else warn 'mediahub.service not enabled'; fi

printf '\nOptional media stack:\n'
# qBittorrent мог быть установлен вручную под другим именем службы.
QBIT_UNIT=qbittorrent-nox.service
for unit in qbittorrent-nox.service qbittorrent.service; do
  if systemctl is-active --quiet "$unit"; then QBIT_UNIT=$unit; break; fi
done
for svc in "$QBIT_UNIT" radarr.service sonarr.service prowlarr.service; do
  if systemctl list-unit-files "$svc" --no-legend 2>/dev/null | grep -q .; then
    if systemctl is-active --quiet "$svc"; then ok "$svc installed + active"; else warn "$svc installed but stopped"; fi
  else
    printf '[ -- ] %s not installed (install from MediaHub -> Установка)\n' "$svc"
  fi
done

printf '\nMediaHub automation:\n'
for t in mediahub-local-cache.timer mediahub-cache.timer mediahub-organizer.timer; do
  if systemctl list-unit-files "$t" --no-legend 2>/dev/null | grep -q .; then
    if systemctl is-active --quiet "$t"; then ok "$t active"; else warn "$t installed but inactive"; fi
  else
    printf '[ -- ] %s not installed yet\n' "$t"
  fi
done

printf '\nCore files:\n'
for p in /opt/mediahub/app.py /opt/mediahub/system_setup.py /opt/mediahub/templates/index.html /etc/mediahub.env; do
  if [[ -e "$p" ]]; then ok "$p"; else bad "$p missing"; fi
done

if grep -qE '^(MEDIAHUB_USER|MEDIAHUB_PASS)=' /etc/mediahub.env 2>/dev/null; then
  warn 'old MediaHub Basic Auth variables still present'
else
  ok 'built-in MediaHub Basic Auth removed'
fi

printf '\nPython syntax:\n'
if /opt/mediahub/venv/bin/python -m py_compile /opt/mediahub/app.py /opt/mediahub/cache_refresh.py /opt/mediahub/download_organizer.py /opt/mediahub/system_setup.py 2>/tmp/mediahub-compile.err; then
  ok 'Python modules compile'
else
  bad 'Python compile failed'; cat /tmp/mediahub-compile.err
fi
rm -f /tmp/mediahub-compile.err

printf '\nHTTP/API:\n'
/opt/mediahub/venv/bin/python - <<'PY'
import json, urllib.request
for url in ['http://127.0.0.1:8090/','http://127.0.0.1:8090/api/version','http://127.0.0.1:8090/api/discovery-health','http://127.0.0.1:8090/api/setup/status','http://127.0.0.1:8090/api/setup/storage/disks','http://127.0.0.1:8090/api/browse/options?kind=movies']:
    try:
        with urllib.request.urlopen(url,timeout=5) as r:
            print('[ OK ]',url,r.status)
            if url.endswith('/version'):
                j=json.load(r); print('       version:',j.get('version'))
            elif url.endswith('/discovery-health'):
                j=json.load(r); print('       discovery:',j.get('counts',{}))
            elif url.endswith('/status'):
                j=json.load(r); print('       host:',j.get('host',{}).get('name'),'components:',len(j.get('components',[])))
            elif url.endswith('/storage/disks'):
                j=json.load(r); print('       pool:',j.get('name'),'configured:',j.get('configured'),'disks:',len(j.get('disks',[])))
            elif '/api/browse/options' in url:
                j=json.load(r); print('       browse genres:',len(j.get('genres',[])),'moods:',len(j.get('moods',[])))
    except Exception as e:
        print('[FAIL]',url,e)
PY

ROOT="$(grep -m1 '^MEDIA_ROOT=' /etc/mediahub.env 2>/dev/null | cut -d= -f2-)"
ROOT="${ROOT:-/mnt/media}"
DB_PATH="$(grep -m1 '^MEDIAHUB_CACHE_DB=' /etc/mediahub.env 2>/dev/null | cut -d= -f2-)"
DB_PATH="${DB_PATH:-/var/lib/mediahub/cache.db}"
printf '\nMedia root: %s\n' "$ROOT"
printf 'Database: %s\n' "$DB_PATH"
if [[ -f "$DB_PATH" ]]; then ok "MediaHub database present"; else warn "MediaHub database not created yet"; fi
if [[ -e "$ROOT" ]]; then df -h "$ROOT" 2>/dev/null || true; else warn 'media root not created yet; use Setup Center'; fi

printf '\nStorage Wizard:\n'
if [[ -f /var/lib/mediahub/setup/storage.json ]]; then
  ok 'storage.json present'
  /opt/mediahub/venv/bin/python - <<'PY2'
import json
p='/var/lib/mediahub/setup/storage.json'
try:
    j=json.load(open(p))
    print('       type:',j.get('type'),'name:',j.get('name'),'mount:',j.get('mountpoint'),'branches:',len(j.get('branches',[])),'pending:',j.get('pending',False))
except Exception as e: print('[WARN] cannot read storage.json:',e)
PY2
  if grep -q '^# BEGIN MEDIAHUB STORAGE' /etc/fstab 2>/dev/null; then ok 'managed /etc/fstab block present'; else warn 'storage.json exists but fstab block not found'; fi
  if findmnt -rn -M "$ROOT" >/dev/null 2>&1; then ok "$ROOT mounted"; else warn "$ROOT is not a mountpoint"; fi
else
  printf '[ -- ] MediaPool not configured; existing-folder mode may still be used.\n'
fi

printf '\nSetup log:\n'
[[ -f /var/lib/mediahub/setup/install.log ]] && tail -n 12 /var/lib/mediahub/setup/install.log || printf 'No component installation has run yet.\n'

printf '\n=== Done ===\n'
printf 'Web: http://%s:8090\n' "$(hostname -I 2>/dev/null | awk '{print $1}')"
printf 'Logs: journalctl -u mediahub -n 200 --no-pager\n\n'
