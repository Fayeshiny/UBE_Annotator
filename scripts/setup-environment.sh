#!/usr/bin/env bash
# Bootstrap a fresh Ubuntu VM; run as the account that will own the service.
set -euo pipefail
trap 'printf "Setup failed at line %s. Fix the error above and rerun.\n" "$LINENO" >&2' ERR

usage() {
    cat <<'EOF'
Usage: bash scripts/setup-environment.sh [--sam31] [--skip-system]
  --sam31        Also install CUDA PyTorch and pinned SAM 3.1 source/dependencies.
                 Model weights must be obtained separately.
  --skip-system  Skip apt; required system tools must already be installed.

Installs Python 3.12 via uv, Node 22 via nvm, backend dependencies, and builds
the frontend. Creates deploy/production.env only if it does not exist.
Does not initialize a database, start services, download weights, or open ports.
EOF
}

SSI_WITH_SAM=0
SSI_SKIP_SYSTEM=0
for arg in "$@"; do
    case "$arg" in
        --sam31) SSI_WITH_SAM=1 ;;
        --skip-system) SSI_SKIP_SYSTEM=1 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$arg" >&2; usage; exit 2 ;;
    esac
done

[[ "$(uname -s)" == Linux ]] || { echo 'Run this on the Ubuntu VM.' >&2; exit 1; }
[[ "$EUID" -ne 0 ]] || { echo 'Run as your normal user, not sudo bash.' >&2; exit 1; }
SSI_PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SSI_PROJECT_ROOT"
if command -v systemctl >/dev/null && systemctl --user is-active --quiet ssi-annotator.service 2>/dev/null; then
    echo 'Stop ssi-annotator.service before changing its environment.' >&2
    exit 1
fi

if [[ "$SSI_SKIP_SYSTEM" == 0 ]]; then
    command -v apt-get >/dev/null || { echo 'Requires Ubuntu/Debian apt-get.' >&2; exit 1; }
    command -v sudo >/dev/null || { echo 'sudo is required, or use --skip-system after administrator setup.' >&2; exit 1; }
    sudo apt-get update
    sudo apt-get install -y ca-certificates curl git rsync ffmpeg build-essential util-linux
fi
for tool in curl git ffmpeg ffprobe flock; do
    command -v "$tool" >/dev/null || { printf 'Missing system tool: %s\n' "$tool" >&2; exit 1; }
done

export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
uv python install 3.12
if [[ -e backend/.venv ]]; then
    backend/.venv/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 12), "Existing venv must use Python 3.12; move it aside manually before rerunning."'
else
    uv venv --python 3.12 --seed backend/.venv
fi
uv pip install --python backend/.venv/bin/python -e ./backend

export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [[ ! -s "$NVM_DIR/nvm.sh" ]]; then
    curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash
fi
# nvm is a shell function, loaded explicitly for this non-interactive script.
# shellcheck source=/dev/null
. "$NVM_DIR/nvm.sh"
nvm install 22
nvm use 22
(
    cd frontend
    npm ci
    npm run build
)

if [[ "$SSI_WITH_SAM" == 1 ]]; then
    command -v nvidia-smi >/dev/null || { echo 'Install the NVIDIA driver before --sam31.' >&2; exit 1; }
    nvidia-smi
    SSI_SAM_REV=7f473cfd983d5eb43040ff608f66bff0d7bdcb53
    mkdir -p .deps
    if [[ ! -e .deps/sam3 ]]; then
        git clone https://github.com/facebookresearch/sam3.git .deps/sam3
        git -C .deps/sam3 checkout --detach "$SSI_SAM_REV"
    fi
    [[ "$(git -C .deps/sam3 rev-parse HEAD)" == "$SSI_SAM_REV" ]] || {
        echo 'Existing .deps/sam3 has a different revision; resolve it manually.' >&2; exit 1;
    }
    [[ -z "$(git -C .deps/sam3 status --porcelain)" ]] || {
        echo 'Existing SAM source has local changes; resolve them manually.' >&2; exit 1;
    }
    uv pip install --python backend/.venv/bin/python \
        torch==2.10.0 torchvision==0.25.0 \
        --index-url https://download.pytorch.org/whl/cu128
    uv pip install --python backend/.venv/bin/python -r backend/requirements-sam31.txt
    backend/.venv/bin/python - <<'PY'
import torch
assert torch.cuda.is_available(), 'PyTorch cannot access CUDA'
x = torch.ones(16, device='cuda')
assert x.sum().item() == 16
print('CUDA tensor check passed:', torch.cuda.get_device_name(0))
PY
fi

uv pip check --python backend/.venv/bin/python
backend/.venv/bin/python -c 'import ssi_annotator.app; print("Backend import passed")'
backend/.venv/bin/python - "$SSI_PROJECT_ROOT" <<'PY'
import os
import shlex
import sys
from pathlib import Path

root = Path(sys.argv[1])
config = root / 'deploy/production.env'
try:
    fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print('Preserved existing deploy/production.env')
else:
    template = (root / 'deploy/production.env.example').read_text()
    template = template.replace('/absolute/path/to/annotator/data-production',
                                shlex.quote(str(root / 'data-production')))
    with os.fdopen(fd, 'w') as stream:
        stream.write(template)
    print('Created deploy/production.env (localhost:8002, mock backend)')
PY

cat <<'EOF'

Environment ready. Next, from the project root:
  # Review deploy/production.env, then initialize a NEW database if desired:
  set -a; source deploy/production.env; set +a
  backend/.venv/bin/ssi-annotator init --admin admin
  bash scripts/install-production-service.sh
  sudo loginctl enable-linger "$USER"
  systemctl --user start ssi-annotator.service
  curl --fail http://127.0.0.1:8002/api/health

For real segmentation after --sam31, obtain the SAM 3.1 checkpoint separately
and set SSI_SAM_BACKEND=sam3.1, SSI_SAM_CHECKPOINT=<absolute path>,
CUDA_VISIBLE_DEVICES=0, and SSI_SAM_USE_FA3=0 in deploy/production.env.
Leave SSI_COOKIE_SECURE=0 for HTTP through an SSH tunnel; use 1 for HTTPS.
See deploy/README.md for data migration, backups, and public HTTPS setup.
EOF
