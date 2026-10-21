#!/usr/bin/env bash
set -euo pipefail
umask 077
SSI_PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SSI_ENV_FILE="${SSI_ENV_FILE:-$SSI_PROJECT_ROOT/deploy/production.env}"
if [[ ! -f "$SSI_ENV_FILE" ]]; then
    printf 'Missing production config: %s\n' "$SSI_ENV_FILE" >&2
    exit 1
fi
set -a
source "$SSI_ENV_FILE"
set +a
: "${SSI_DATA_DIR:?Set an absolute SSI_DATA_DIR in production.env}"
[[ "$SSI_DATA_DIR" = /* ]] || { echo 'SSI_DATA_DIR must be absolute' >&2; exit 1; }
export SSI_STATIC_DIR="${SSI_STATIC_DIR:-$SSI_PROJECT_ROOT/frontend/dist}"
export SSI_SAM_BACKEND="${SSI_SAM_BACKEND:-mock}"
if [[ ! -f "$SSI_STATIC_DIR/index.html" ]]; then
    echo 'Build the frontend before starting production: cd frontend && npm ci && npm run build' >&2
    exit 1
fi
if [[ "$SSI_SAM_BACKEND" == sam3 || "$SSI_SAM_BACKEND" == sam3.1 ]]; then
    : "${SSI_SAM_CHECKPOINT:?Set SSI_SAM_CHECKPOINT for SAM}"
    [[ -f "$SSI_SAM_CHECKPOINT" ]] || { echo 'SAM checkpoint is missing' >&2; exit 1; }
    SSI_CUBLAS_LIB="$("$SSI_PROJECT_ROOT/backend/.venv/bin/python" -c 'import sysconfig; from pathlib import Path; print(Path(sysconfig.get_paths()["purelib"]) / "nvidia/cublas/lib")')"
    export LD_LIBRARY_PATH="$SSI_CUBLAS_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
mkdir -p "$SSI_DATA_DIR"
cd "$SSI_PROJECT_ROOT"
# Sessions and import jobs are process-local: run exactly one production worker.
# The lock also prevents accidentally starting this script twice on one data dir.
exec flock --no-fork --nonblock "$SSI_DATA_DIR/.server.lock" \
    "$SSI_PROJECT_ROOT/backend/.venv/bin/python" -m ssi_annotator.cli \
    serve --host "${SSI_HOST:-127.0.0.1}" --port "${SSI_PORT:-8002}"
