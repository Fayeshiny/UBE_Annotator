#!/usr/bin/env bash
set -euo pipefail

SSI_PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Always use this checkout's development data, even if the shell points at the old service.
export SSI_DATA_DIR="$SSI_PROJECT_ROOT/data-dev"
export SSI_SAM_BACKEND="${SSI_SAM_BACKEND:-mock}"
export SSI_STATIC_DIR="$SSI_PROJECT_ROOT/frontend/dist"
# Poll for edits as filesystem notifications may be missed on mounted workspaces.
export WATCHFILES_FORCE_POLLING="${WATCHFILES_FORCE_POLLING:-true}"
export WATCHFILES_POLL_DELAY_MS="${WATCHFILES_POLL_DELAY_MS:-500}"
cd "$SSI_PROJECT_ROOT"

if [[ $# -eq 0 ]]; then
    set -- serve --host 0.0.0.0 --port 8001 --reload
fi

exec "$SSI_PROJECT_ROOT/backend/.venv/bin/python" -m ssi_annotator.cli "$@"
