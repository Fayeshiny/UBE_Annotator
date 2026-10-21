#!/usr/bin/env bash
set -euo pipefail
SSI_PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$SSI_PROJECT_ROOT/deploy/production.env" ]] || {
    echo 'Configure deploy/production.env first.' >&2; exit 1;
}
"$SSI_PROJECT_ROOT/backend/.venv/bin/python" - "$SSI_PROJECT_ROOT" <<'PY'
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"
unit_dir.mkdir(parents=True, exist_ok=True)
# Escape systemd specifiers and quoted-string syntax in the installation path.
escaped = str(root).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
template = (root / "deploy/ssi-annotator.service.in").read_text()
(unit_dir / "ssi-annotator.service").write_text(template.replace("@PROJECT_ROOT@", escaped))
PY
systemctl --user daemon-reload
systemctl --user enable ssi-annotator.service
echo 'Installed. Start with: systemctl --user start ssi-annotator.service'
echo 'For startup at boot and survival after logout: sudo loginctl enable-linger "$USER"'
