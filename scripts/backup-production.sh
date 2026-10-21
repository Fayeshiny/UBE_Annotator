#!/usr/bin/env bash
set -euo pipefail
umask 077
SSI_PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "${SSI_ENV_FILE:-$SSI_PROJECT_ROOT/deploy/production.env}"
: "${SSI_DATA_DIR:?Missing SSI_DATA_DIR}"
if [[ $# -ne 1 ]]; then
    echo 'Usage: scripts/backup-production.sh /absolute/path/to/new-backup.tar' >&2
    exit 1
fi
SSI_BACKUP_PATH="$(realpath -m -- "$1")"
SSI_SOURCE_PATH="$(realpath -- "$SSI_DATA_DIR")"
case "$SSI_BACKUP_PATH" in
    "$SSI_SOURCE_PATH"|"$SSI_SOURCE_PATH"/*) echo 'Backup must be outside the data directory.' >&2; exit 1 ;;
esac
[[ ! -e "$SSI_BACKUP_PATH" ]] || { echo 'Backup already exists; refusing to overwrite it.' >&2; exit 1; }
SSI_BACKUP_TEMP="$(mktemp -- "${SSI_BACKUP_PATH}.partial.XXXXXX")"
SSI_RESUME=0
cleanup() {
    rm -f -- "$SSI_BACKUP_TEMP"
    if [[ "$SSI_RESUME" == 1 ]]; then systemctl --user start ssi-annotator.service; fi
}
trap cleanup EXIT
if systemctl --user is-active --quiet ssi-annotator.service; then
    SSI_RESUME=1
    systemctl --user stop ssi-annotator.service
fi
# Stop writes for a consistent snapshot of BOTH database and case/mask files.
# The lock rejects a concurrent start via serve-production.sh.
flock --nonblock "$SSI_SOURCE_PATH/.server.lock" \
    tar -C "$SSI_SOURCE_PATH" --exclude=./cache --exclude=./.server.lock -cf "$SSI_BACKUP_TEMP" .
mv -n -- "$SSI_BACKUP_TEMP" "$SSI_BACKUP_PATH"
printf 'Backup: %s\n' "$SSI_BACKUP_PATH"
