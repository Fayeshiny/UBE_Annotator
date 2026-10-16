"""SAM 视频分割后端。

通过环境变量 SSI_SAM_BACKEND 选择：
- mock   ：不加载模型，用几何形状模拟掩膜，用于开发和界面培训；
- sam3.1 ：Meta SAM 3.1 Object Multiplex（需要 CUDA GPU 与 HF 权重授权）；
- sam3   ：Meta SAM 3；
- sam2   ：Meta SAM 2.1（可在 Apple Silicon 的 MPS 上运行，作为备选）。
"""

from functools import lru_cache

from .. import config
from .base import SamBackend


@lru_cache(maxsize=1)
def get_backend() -> SamBackend:
    name = config.sam_backend()
    if name == "mock":
        from .mock import MockBackend

        return MockBackend()
    if name in ("sam3", "sam3.1"):
        from .sam3_backend import Sam3Backend

        return Sam3Backend(version=name)
    if name == "sam2":
        from .sam2_backend import Sam2Backend

        return Sam2Backend()
    raise ValueError(f"未知的 SSI_SAM_BACKEND: {name}")
