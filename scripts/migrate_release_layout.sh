#!/usr/bin/env bash
# Preserve flat paths; shared links refer to existing runtime data.
set -Eeuo pipefail
umask 077
root=$(realpath -e "${CAREEROS_ROOT:-$HOME/careeros}")
home_real=$(realpath -e "$HOME")
case "$root" in "$home_real"/*) ;; *) echo 'Unsafe application root'; exit 2;; esac
for path in current shared releases; do
  if [[ -e "$root/$path" || -L "$root/$path" ]]; then
    echo "Ambiguous existing layout: $path"; exit 2
  fi
done
for name in data vault output; do
  test -d "$root/$name" && test ! -L "$root/$name"
done
test -f "$root/.env"
test -x "$root/.venv/bin/python"
test -f "${DB_BACKUP_HELPER:?A reviewed database backup helper is required}"
sudo -n true
backup="$root/operations-backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup"
for service in careeros-web.service careeros.service; do
  sudo -n cp -- "/etc/systemd/system/$service" "$backup/$service"
done
crontab -l > "$backup/crontab" 2>/dev/null || true
tar -czf "$backup/flat-app.tar.gz" --exclude='./.venv' --exclude='./.git' \
    --exclude='./operations-backups' -C "$root" .
mkdir "$root/shared" "$root/releases" "$root/releases/legacy"
rsync -a --exclude='/.venv' --exclude='/.git' --exclude='/data' --exclude='/vault' \
  --exclude='/output' --exclude='/.env' --exclude='/operations-backups' \
  --exclude='/shared' --exclude='/releases' --exclude='/current' \
  "$root/" "$root/releases/legacy/"
for name in data vault output .env; do
  ln -s "$root/$name" "$root/shared/$name"
  ln -s "$root/shared/$name" "$root/releases/legacy/$name"
done
ln -s "$root/.venv" "$root/releases/legacy/.venv"
python3 - "$root" "$backup" <<'PY'
import sys
from pathlib import Path
root, backup = map(Path, sys.argv[1:])
for name in ('careeros-web.service', 'careeros.service', 'crontab'):
    original = (backup/name).read_text()
    if name.endswith('.service') and str(root) not in original:
        raise SystemExit('Unexpected service paths')
    (backup/(name+'.next')).write_text(original.replace(str(root), str(root/'current')))
PY
stopped=0
rollback() {
  result=$?
  if [[ "$stopped" == 1 ]]; then
    for service in careeros-web.service careeros.service; do
      sudo -n cp -- "$backup/$service" "/etc/systemd/system/$service"
    done
    crontab "$backup/crontab"
    sudo -n systemctl daemon-reload
    sudo -n systemctl restart careeros-web.service careeros.service || true
  fi
  echo "Migration failed; flat application retained. Backup: $backup" >&2
  exit "$result"
}
trap rollback ERR
stopped=1
sudo -n systemctl stop careeros-web.service careeros.service
"$root/.venv/bin/python" "$DB_BACKUP_HELPER" "$root" "$root/data/backups/pre-release-$(date -u +%Y%m%dT%H%M%SZ).json"
ln -s "$root/releases/legacy" "$root/current"
for service in careeros-web.service careeros.service; do
  sudo -n install -m 644 "$backup/$service.next" "/etc/systemd/system/$service"
done
crontab "$backup/crontab.next"
sudo -n systemctl daemon-reload
sudo -n systemctl restart careeros-web.service careeros.service
sudo -n systemctl is-active --quiet careeros-web.service
sudo -n systemctl is-active --quiet careeros.service
stopped=0
trap - ERR
echo "Legacy release active. Original flat files preserved. Backup: $backup"
