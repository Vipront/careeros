#!/usr/bin/env bash
set -Eeuo pipefail

archive_input=${1:?usage: release.sh ARCHIVE_PATH RELEASE_ID}
release_id=${2:?usage: release.sh ARCHIVE_PATH RELEASE_ID}

if [[ ! "$release_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ || "$release_id" == "." || "$release_id" == ".." ]]; then
  echo "Invalid release id." >&2
  exit 2
fi

home_real=$(realpath -e -- "$HOME")
root_input=${CAREEROS_ROOT:-"$HOME/careeros"}
root_real=$(realpath -m -- "$root_input")
case "$root_real" in
  "$home_real"/*) ;;
  *) echo "CAREEROS_ROOT must resolve to a directory inside the deploy user's home." >&2; exit 2 ;;
esac

incoming_real=$(realpath -m -- "$root_real/incoming")
if [[ "$incoming_real" != "$root_real/incoming" ]]; then
  echo "Incoming directory must remain inside CAREEROS_ROOT." >&2
  exit 2
fi
expected_archive="$incoming_real/release-$release_id.tar"
archive_real=$(realpath -e -- "$archive_input")
if [[ "$archive_real" != "$expected_archive" || ! -f "$archive_real" ]]; then
  echo "Archive must be the matching release tar file inside $incoming_real." >&2
  exit 2
fi

shared="$root_real/shared"
releases="$root_real/releases"
current="$root_real/current"
stage="$releases/.staging-$release_id-$$"
release="$releases/$release_id"

# Serialize installations and pruning across deploys.
exec 9>"$root_real/.deploy.lock"
flock -n 9 || { echo "Another deployment is running." >&2; exit 2; }

if [[ ! -L "$current" ]]; then
  echo "Deployment requires the documented release-layout migration and a current symlink." >&2
  exit 2
fi
if [[ -L "$shared" || -L "$releases" ]]; then
  echo "shared and releases must be real directories inside CAREEROS_ROOT." >&2
  exit 2
fi
previous=$(realpath -e -- "$current")
case "$previous" in
  "$releases"/*) ;;
  *) echo "Current symlink must point to a release below $releases." >&2; exit 2 ;;
esac
for shared_path in "$shared/data" "$shared/vault" "$shared/output"; do
  if [[ ! -d "$shared_path" ]]; then
    echo "Required persistent directory is missing: $shared_path" >&2
    exit 2
  fi
done
if [[ ! -f "$shared/.env" ]]; then
  echo "Required persistent environment file is missing: $shared/.env" >&2
  exit 2
fi
if [[ -e "$release" || -L "$release" || -e "$stage" || -L "$stage" ]]; then
  echo "Release or staging path already exists; refusing to overwrite it." >&2
  exit 2
fi

if ! tar -tf "$archive_real" | grep -Fx "requirements.txt" >/dev/null; then
  echo "Committed requirements.txt is required for release installs." >&2
  exit 2
fi

while IFS= read -r entry; do
  case "$entry" in
    /*|..|../*|*/../*|*/..)
      echo "Archive contains an unsafe path: $entry" >&2
      exit 2
      ;;
    data/|data|data/schema.sql|data/semantic_profiles.json)
      ;;
    .env.example)
      ;;
    data/*|vault|vault/*|output|output/*|.env|.env.*|*/.env|*/.env.*|*.db|*.sqlite|*.sqlite3|*.key|*.pem)
      echo "Archive contains a forbidden runtime or sensitive path: $entry" >&2
      exit 2
      ;;
  esac
done < <(tar -tf "$archive_real")
if tar -tvf "$archive_real" | awk 'substr($0, 1, 1) == "l" || substr($0, 1, 1) == "h" { found = 1 } END { exit !found }'; then
  echo "Archive symlinks and hard links are not allowed." >&2
  exit 2
fi

mkdir -m 750 -- "$stage"
activated=0

rollback_on_error() {
  status=$?
  if [[ "$activated" == 1 ]]; then
    rollback_link="$root_real/.current-rollback-$release_id-$$"
    if [[ ! -e "$rollback_link" && ! -L "$rollback_link" ]]; then
      ln -s -- "$previous" "$rollback_link"
      mv -Tf -- "$rollback_link" "$current"
      echo "Release health check failed; restored $previous." >&2
      sudo systemctl restart careeros-web.service careeros.service || true
    else
      echo "Rollback link already exists; current remains $current for manual recovery." >&2
    fi
  fi
  exit "$status"
}
trap rollback_on_error ERR

tar --no-same-owner --no-same-permissions -xf "$archive_real" -C "$stage"
if [[ ! -d "$stage/data" || ! -f "$stage/requirements.txt" ]]; then
  echo "Release archive is missing required data or dependency files." >&2
  exit 1
fi

# Seed shared data assets only when absent; never replace a database or config.
cp -an -- "$stage/data/." "$shared/data/"
mv -- "$stage/data" "$stage/data-code"
for name in vault output; do
  if [[ -e "$stage/$name" || -L "$stage/$name" ]]; then
    echo "Release archive unexpectedly contains persistent path $name." >&2
    exit 1
  fi
done
if [[ -e "$stage/.env" || -L "$stage/.env" ]]; then
  echo "Release archive unexpectedly contains .env." >&2
  exit 1
fi
ln -s -- "$shared/data" "$stage/data"
ln -s -- "$shared/vault" "$stage/vault"
ln -s -- "$shared/output" "$stage/output"
ln -s -- "$shared/.env" "$stage/.env"

python3 -m venv "$stage/.venv"
lockfile="$stage/requirements-linux-aarch64-py312.lock.txt"
if [[ "$(uname -m)" != "aarch64" || ! -f "$lockfile" ]] || \
   ! "$stage/.venv/bin/python" -c 'import sys; sys.exit(sys.version_info[:2] != (3,12))'; then
  echo "This release requires the committed Linux aarch64 / Python 3.12 artifact lock." >&2
  exit 1
fi
PIP_NO_CACHE_DIR=1 "$stage/.venv/bin/python" -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: --requirement "$lockfile"
PYTHONPATH="$stage" "$stage/.venv/bin/python" -m src.ops.readiness
timeout 120 env PYTHONPATH="$stage" "$stage/.venv/bin/python" -m src.dashboard_cache

next_link="$root_real/.current-$release_id-$$"
if [[ -e "$next_link" || -L "$next_link" ]]; then
  echo "Temporary activation link already exists; refusing to overwrite it." >&2
  exit 1
fi
mv -- "$stage" "$release"
ln -s -- "$release" "$next_link"
mv -Tf -- "$next_link" "$current"
activated=1

sudo systemctl restart careeros-web.service careeros.service
sudo systemctl is-active --quiet careeros-web.service
sudo systemctl is-active --quiet careeros.service
PYTHONPATH="$root_real/current" "$root_real/current/.venv/bin/python" -m src.ops.readiness

activated=0
trap - ERR
rm -- "$archive_real"
touch -- "$release/.deployment-success"
if ! python3 "$release/scripts/cleanup_releases.py" "$root_real" --previous "$previous"; then
  echo "Release is healthy, but retention cleanup failed; inspect before retrying." >&2
fi
echo "Release $release_id is active and both services are running."
