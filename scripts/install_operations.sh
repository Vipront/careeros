#!/usr/bin/env bash
# Install repeatable user cron entries and keep the previous crontab for recovery.
set -euo pipefail
root="/home/ubuntu/careeros"
cd "$root/current"
test -f scripts/ops_guard.py
test -f scripts/daily_backup.sh
mkdir -p "$root/operations-backups" data/runtime
saved="$root/operations-backups/crontab-before-ops-$(date -u +%Y%m%dT%H%M%SZ).txt"
crontab -l > "$saved" 2>/dev/null || true
.venv/bin/python - "$saved" "$root" <<'PY' | crontab -
from pathlib import Path
import sys
saved, root = sys.argv[1:]
lines = [line for line in Path(saved).read_text().splitlines()
         if '# careeros-ops:' not in line]
# Preserve the existing daily pipeline schedule; only add operational jobs.
lines += [
    f'15 3 * * * cd {root}/current && bash scripts/daily_backup.sh {root}/current >> {root}/operations.log 2>&1 # careeros-ops:backup',
    f'5,35 * * * * cd {root}/current && flock -n {root}/data/runtime/ops-guard.lock timeout 180 .venv/bin/python scripts/ops_guard.py {root}/current >> {root}/operations.log 2>&1 # careeros-ops:guard',
]
print('\n'.join(lines))
PY
printf 'Operational cron installed; previous crontab: %s\n' "$saved"
