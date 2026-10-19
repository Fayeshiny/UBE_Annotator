"""单帧自动候选：均匀采样提示点，过滤空掩膜、极端面积与高度重复结果。"""
import numpy as np

GRID_SIZE = 6


def grid_points(width: int, height: int) -> list[list[int]]:
    return [[min(width - 1, int((x + 0.5) * width / GRID_SIZE)),
             min(height - 1, int((y + 0.5) * height / GRID_SIZE))]
            for y in range(GRID_SIZE) for x in range(GRID_SIZE)]


def accept_mask(mask, width: int, height: int, accepted: list[np.ndarray]) -> np.ndarray | None:
    mask = np.asarray(mask, dtype=bool)
    if mask.shape != (height, width):
        raise ValueError(f"候选尺寸 {mask.shape} 与当前画面 {(height, width)} 不一致")
    area = int(mask.sum())
    if area < max(4, int(width * height * 0.0005)) or area > width * height * 0.95:
        return None
    for previous in accepted:
        intersection = int(np.count_nonzero(mask & previous))
        union = area + int(previous.sum()) - intersection
        if union and intersection / union >= 0.85:
            return None
    return mask
