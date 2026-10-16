import os
from pathlib import Path


def data_dir() -> Path:
    """数据根目录：数据库、病例视频与导出文件。含医疗视频，只能保存在本地/内网服务器。"""
    d = Path(os.environ.get("SSI_DATA_DIR", Path.cwd() / "data")).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "ssi.sqlite3"


def cases_dir() -> Path:
    d = data_dir() / "cases"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    d = data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sam_backend() -> str:
    """mock | sam2 | sam3 | sam3.1"""
    return os.environ.get("SSI_SAM_BACKEND", "mock")


def static_dir() -> Path | None:
    d = os.environ.get("SSI_STATIC_DIR")
    if d:
        return Path(d)
    default = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    return default if default.exists() else None


def cookie_secure() -> bool:
    return os.environ.get("SSI_COOKIE_SECURE", "0") == "1"
