from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

import numpy as np


class SamBackend(ABC):
    """统一的视频分割接口。帧号均为“片段内局部帧号”（JPEG 序列中的序号）。

    坐标均为像素绝对坐标；box 为 [x0, y0, x1, y1]。
    """

    name = "base"

    @abstractmethod
    def start(self, frames_dir: Path, width: int, height: int) -> str:
        """为 JPEG 帧序列建立推理会话，返回 session id。"""

    @abstractmethod
    def prompt(
        self,
        session: str,
        frame: int,
        obj_id: int,
        points: list[list[float]],
        labels: list[int],
        box: list[float] | None,
    ) -> np.ndarray:
        """在某帧上为某目标添加提示（会替换该帧旧提示），返回该帧该目标的二值掩膜 (H, W)。"""

    @abstractmethod
    def propagate(self, session: str, start_frame: int, direction: str) -> Iterator[tuple[int, dict[int, np.ndarray]]]:
        """传播；逐帧产出 (局部帧号, {obj_id: mask})。direction: forward | backward | both"""

    @abstractmethod
    def remove_object(self, session: str, obj_id: int):
        ...

    @abstractmethod
    def close(self, session: str):
        ...
