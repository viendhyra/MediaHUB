#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo "Запусти от root."; exit 1; }
TS="$(date +%Y%m%d-%H%M%S)"
OUT="/root/mediahub-backup-$TS.tar.gz"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/etc-systemd"

[[ -f /etc/mediahub.env ]] && cp -a /etc/mediahub.env "$TMP/"
DB_PATH="$(awk -F= '$1=="MEDIAHUB_CACHE_DB"{print substr($0,index($0,"=")+1)}' /etc/mediahub.env 2>/dev/null | tail -n1)"
DB_PATH="${DB_PATH:-/var/lib/mediahub/cache.db}"
if [[ -f "$DB_PATH" ]]; then
  python3 - "$DB_PATH" "$TMP/mediahub.db" <<'PYDB'
import sqlite3,sys
src,dst=sys.argv[1:]
a=sqlite3.connect(src,timeout=10);b=sqlite3.connect(dst,timeout=10)
try:a.backup(b)
finally:b.close();a.close()
PYDB
fi
[[ -f /var/lib/mediahub/secrets.json ]] && cp -a /var/lib/mediahub/secrets.json "$TMP/"
[[ -f /var/lib/mediahub/setup/storage.json ]] && { mkdir -p "$TMP/setup"; cp -a /var/lib/mediahub/setup/storage.json "$TMP/setup/"; }
for f in /etc/systemd/system/mediahub.service /etc/systemd/system/mediahub-*.service /etc/systemd/system/mediahub-*.timer; do
  [[ -e "$f" ]] && cp -a "$f" "$TMP/etc-systemd/"
done

cat > "$TMP/RESTORE.txt" <<TXT
MediaHub backup created: $TS
Database source: $DB_PATH

Restore /etc/mediahub.env first. The MEDIAHUB_CACHE_DB path inside it determines
where mediahub.db belongs. Restore the database to that path, then restart:
  systemctl daemon-reload
  systemctl restart mediahub

The archive contains API keys/secrets. Keep it private.
TXT

tar -C "$TMP" -czf "$OUT" .
chmod 600 "$OUT"
echo "Backup created: $OUT"
echo "Database: $DB_PATH"
echo "IMPORTANT: archive contains API keys/passwords; keep it private."
