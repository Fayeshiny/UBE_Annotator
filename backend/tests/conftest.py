import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SSI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SSI_SAM_BACKEND", "mock")
    from ssi_annotator import db

    db.reset_connection()
    db.init_db()
    yield tmp_path / "data"
    db.reset_connection()


@pytest.fixture(scope="session")
def numbered_video(tmp_path_factory) -> Path:
    """每帧灰度 = 帧号 × 2 的合成视频（120 帧，30 fps），用于验证帧号对齐。"""
    d = tmp_path_factory.mktemp("vid")
    for i in range(120):
        Image.fromarray(np.full((96, 128, 3), i * 2, dtype=np.uint8)).save(d / f"{i:04d}.png")
    out = d / "numbered.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-framerate", "30", "-i", str(d / "%04d.png"),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "0", str(out),
    ], check=True)
    return out
