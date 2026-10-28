#!/usr/bin/env bash
# Nightly copy of the accounts and verdicts database, keeping the last 30.
# Uses SQLite's online backup (safe while the app is running). Install with:
#   (crontab -l 2>/dev/null; echo "15 3 * * * bash /opt/s1drops/deploy/backup-db.sh") | crontab -
set -euo pipefail

DATA_DIR="${S1DROPS_DATA_DIR:-/srv/s1drops}"
OUT="$DATA_DIR/db-backups"
mkdir -p "$OUT"
stamp="$(date +%F)"
docker exec s1drops-app-1 python -c "
import sqlite3
src = sqlite3.connect('/data/db/app.db'); dst = sqlite3.connect('/data/db/backup.tmp')
src.backup(dst); dst.close(); src.close()"
mv "$DATA_DIR/db/backup.tmp" "$OUT/app-$stamp.db"
chmod 600 "$OUT/app-$stamp.db"
ls -1t "$OUT"/app-*.db | tail -n +31 | xargs -r rm --
