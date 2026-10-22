#!/usr/bin/env bash
# Configure and start a persistent SAM 3.1 instance after setup-environment.sh.
set -euo pipefail
umask 077

usage() {
    cat <<'EOF'
Usage: bash scripts/start-production-sam31.sh [--checkpoint PATH] [--gpu ID]

Defaults: configured checkpoint, otherwise models/sam3.1/sam3.1_multiplex.pt;
configured GPU, otherwise GPU 0. Preserves the configured data directory,
host, port and HTTPS cookie setting; new configurations use data-production,
127.0.0.1:8002 and HTTP cookies for SSH port forwarding.

Creates admin interactively if absent, installs/enables the user systemd
service, enables startup after logout/reboot, starts it and checks health.
Run as your normal user. sudo is used only to enable lingering.
If the service is already running, stop it before running this script.
EOF
}
SSI_REQUEST_CHECKPOINT=''
SSI_REQUEST_GPU=''
while [[ $# -gt 0 ]]; do
    case "$1" in
        --checkpoint|--gpu)
            [[ $# -ge 2 && -n "$2" ]] || { usage >&2; exit 2; }
            case "$1" in
                --checkpoint) SSI_REQUEST_CHECKPOINT="$2" ;;
                --gpu) SSI_REQUEST_GPU="$2" ;;
            esac
            shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done
[[ "$EUID" -ne 0 ]] || { echo 'Run as your normal user, not sudo bash.' >&2; exit 1; }
SSI_PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SSI_PROJECT_ROOT"
SSI_PYTHON="$SSI_PROJECT_ROOT/backend/.venv/bin/python"
[[ -x "$SSI_PYTHON" ]] || { echo 'Run scripts/setup-environment.sh --sam31 first.' >&2; exit 1; }
for tool in systemctl loginctl curl ffmpeg ffprobe flock; do
    command -v "$tool" >/dev/null || { printf 'Missing command: %s\n' "$tool" >&2; exit 1; }
done
systemctl --user show-environment >/dev/null
SSI_INSTALLED_ROOT="$(systemctl --user show ssi-annotator.service -p WorkingDirectory --value 2>/dev/null || true)"
if [[ -n "$SSI_INSTALLED_ROOT" && "$SSI_INSTALLED_ROOT" != "$SSI_PROJECT_ROOT" ]]; then
    echo 'ssi-annotator.service belongs to another checkout; refusing to replace it.' >&2
    exit 1
fi
if systemctl --user is-active --quiet ssi-annotator.service; then
    echo 'Service is already running. To reconfigure, first run:' >&2
    echo '  systemctl --user stop ssi-annotator.service' >&2
    exit 1
fi

# Use this checkout's trusted production config, not settings from a dev shell.
unset SSI_DATA_DIR SSI_HOST SSI_PORT SSI_COOKIE_SECURE SSI_SAM_CHECKPOINT CUDA_VISIBLE_DEVICES SSI_STATIC_DIR SSI_ENV_FILE
if [[ -f deploy/production.env ]]; then
    set -a
    # shellcheck source=/dev/null
    source deploy/production.env
    set +a
fi
export SSI_DATA_DIR="${SSI_DATA_DIR:-$SSI_PROJECT_ROOT/data-production}"
export SSI_HOST="${SSI_HOST:-127.0.0.1}"
export SSI_PORT="${SSI_PORT:-8002}"
export SSI_COOKIE_SECURE="${SSI_COOKIE_SECURE:-0}"
export SSI_SAM_BACKEND=sam3.1
export SSI_SAM_CHECKPOINT="${SSI_REQUEST_CHECKPOINT:-${SSI_SAM_CHECKPOINT:-$SSI_PROJECT_ROOT/models/sam3.1/sam3.1_multiplex.pt}}"
export CUDA_VISIBLE_DEVICES="${SSI_REQUEST_GPU:-${CUDA_VISIBLE_DEVICES:-0}}"
export SSI_SAM_USE_FA3=0
[[ "$SSI_DATA_DIR" = /* ]] || { echo 'SSI_DATA_DIR must be an absolute path.' >&2; exit 1; }
[[ -s "$SSI_SAM_CHECKPOINT" ]] || { printf 'Missing or empty checkpoint: %s\nUse --checkpoint /absolute/path/to/sam3.1_multiplex.pt\n' "$SSI_SAM_CHECKPOINT" >&2; exit 1; }
export SSI_SAM_CHECKPOINT="$(realpath -- "$SSI_SAM_CHECKPOINT")"
[[ -f "${SSI_STATIC_DIR:-$SSI_PROJECT_ROOT/frontend/dist}/index.html" ]] || {
    echo 'Frontend build missing. Run scripts/setup-environment.sh --sam31 first.' >&2; exit 1;
}
SSI_CUBLAS_LIB="$("$SSI_PYTHON" -c 'import sysconfig; from pathlib import Path; print(Path(sysconfig.get_paths()["purelib"]) / "nvidia/cublas/lib")')"
export LD_LIBRARY_PATH="$SSI_CUBLAS_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
"$SSI_PYTHON" - <<'PY'
import os
import socket
import torch
import sam3

assert torch.cuda.is_available(), 'CUDA unavailable; check --gpu and PyTorch installation'
assert torch.ones(16, device='cuda').sum().item() == 16
print('CUDA check passed:', torch.cuda.get_device_name(0))
with socket.socket() as sock:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((os.environ['SSI_HOST'], int(os.environ['SSI_PORT'])))
PY

# Ensure persistence is available before writing config or creating accounts.
SSI_SERVICE_USER="$(id -un)"
if [[ "$(loginctl show-user "$SSI_SERVICE_USER" -p Linger --value)" != yes ]]; then
    sudo loginctl enable-linger "$SSI_SERVICE_USER"
fi
mkdir -p "$SSI_DATA_DIR"
exec 9>"$SSI_DATA_DIR/.server.lock"
flock --nonblock 9 || { echo 'Another process is using this data directory.' >&2; exit 1; }
"$SSI_PYTHON" - "$SSI_PROJECT_ROOT" <<'PY'
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile

root = Path(sys.argv[1])
config = root / 'deploy/production.env'
keys = ('SSI_DATA_DIR', 'SSI_HOST', 'SSI_PORT', 'SSI_COOKIE_SECURE',
        'SSI_SAM_BACKEND', 'SSI_SAM_CHECKPOINT', 'CUDA_VISIBLE_DEVICES', 'SSI_SAM_USE_FA3')
original = config.read_text() if config.exists() else (root / 'deploy/production.env.example').read_text()
lines = [line for line in original.splitlines()
         if not re.match(r'^\s*(?:export\s+)?(?:' + '|'.join(keys) + r')=', line)]
lines += ['', '# Persistent SAM 3.1 service settings']
lines += [f'{key}={shlex.quote(os.environ[key])}' for key in keys]
fd, temporary = tempfile.mkstemp(dir=config.parent, prefix='.production.env-')
try:
    with os.fdopen(fd, 'w') as stream:
        stream.write('\n'.join(lines) + '\n')
    os.replace(temporary, config)
finally:
    Path(temporary).unlink(missing_ok=True)
PY
"$SSI_PYTHON" -m ssi_annotator.cli init --admin admin
bash scripts/install-production-service.sh
flock --unlock 9
exec 9>&-
systemctl --user start ssi-annotator.service

SSI_HEALTH_HOST="$SSI_HOST"
[[ "$SSI_HEALTH_HOST" != 0.0.0.0 ]] || SSI_HEALTH_HOST=127.0.0.1
for ((attempt=0; attempt<30; attempt++)); do
    if systemctl --user is-active --quiet ssi-annotator.service && \
        curl --fail --silent --max-time 2 "http://$SSI_HEALTH_HOST:$SSI_PORT/api/health"; then
        printf '\nService ready. Forward port %s in VS Code to open the platform.\n' "$SSI_PORT"
        printf 'Data directory: %s\n' "$SSI_DATA_DIR"
        echo 'SAM loads on demand; the health check does not verify model inference.'
        echo 'Logs: journalctl --user -u ssi-annotator.service -n 60 --no-pager'
        exit 0
    fi
    sleep 1
done
echo 'Service did not become healthy. Recent logs:' >&2
journalctl --user -u ssi-annotator.service -n 60 --no-pager >&2
exit 1
