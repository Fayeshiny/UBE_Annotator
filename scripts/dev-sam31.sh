#!/usr/bin/env bash
set -euo pipefail
SSI_PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export SSI_SAM_BACKEND=sam3.1
export SSI_SAM_CHECKPOINT="${SSI_SAM_CHECKPOINT:-$SSI_PROJECT_ROOT/models/sam3.1/sam3.1_multiplex.pt}"
export SSI_SAM_USE_FA3="${SSI_SAM_USE_FA3:-0}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-2}"
# Prefer the cuBLAS shipped with this PyTorch over a system CUDA toolkit.
SSI_CUBLAS_LIB="$("$SSI_PROJECT_ROOT/backend/.venv/bin/python" -c 'import sysconfig; from pathlib import Path; print(Path(sysconfig.get_paths()["purelib"]) / "nvidia/cublas/lib")')"
export LD_LIBRARY_PATH="$SSI_CUBLAS_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
if [[ ! -f "$SSI_SAM_CHECKPOINT" ]]; then
    printf 'SAM 3.1 checkpoint is missing: %s\n' "$SSI_SAM_CHECKPOINT" >&2
    exit 1
fi
exec "$SSI_PROJECT_ROOT/scripts/dev-backend.sh" "$@"
