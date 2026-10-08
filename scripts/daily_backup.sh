#!/usr/bin/env bash
# Keep seven successful automatic snapshots; preserve all manual backups.
set -euo pipefail
root="$(readlink -f "${1:?application root required}")"
cd "$root"
mkdir -p data/runtime
exec 9>data/runtime/automatic-backup.lock
flock -n 9 || exit 0
destination="$root/data/backups/daily-auto-$(date -u +%Y%m%dT%H%M%SZ).json"
timeout 1800 .venv/bin/python scripts/backup_database.py "$root" "$destination"
.venv/bin/python - "$root" <<'PY'
from pathlib import Path
import re
import sys
directory = (Path(sys.argv[1]) / 'data/backups').resolve()
files = sorted(p for p in directory.iterdir()
               if re.fullmatch(r'daily-auto-\d{8}T\d{6}Z\.json', p.name)
               and not p.is_symlink() and p.is_file() and p.stat().st_size > 0)
for path in files[:-7]:
    if path.resolve().parent != directory:
        raise RuntimeError('Backup path escaped directory')
    path.unlink()
PY
