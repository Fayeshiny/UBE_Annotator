"""COCO 未压缩 RLE（列优先）编解码，避免依赖 pycocotools。

格式：{"size": [h, w], "counts": [n0, n1, ...]}，counts 从 0 的游程开始交替。
COCO 格式允许 segmentation 使用未压缩 RLE（iscrowd=1）。
"""

import numpy as np
from PIL import Image, ImageDraw


def encode(mask: np.ndarray) -> dict:
    h, w = mask.shape
    flat = np.asarray(mask, dtype=bool).flatten(order="F")
    if flat.size == 0:
        return {"size": [h, w], "counts": []}
    change = np.flatnonzero(flat[1:] != flat[:-1]) + 1
    bounds = np.concatenate([[0], change, [flat.size]])
    runs = np.diff(bounds).tolist()
    if flat[0]:
        runs = [0] + runs
    return {"size": [h, w], "counts": runs}


def decode(rle: dict) -> np.ndarray:
    h, w = rle["size"]
    counts = rle["counts"]
    flat = np.zeros(h * w, dtype=bool)
    pos = 0
    val = False
    for c in counts:
        if val:
            flat[pos : pos + c] = True
        pos += c
        val = not val
    return flat.reshape((w, h)).T


def area(rle: dict) -> int:
    return int(sum(rle["counts"][1::2]))


def bbox(mask: np.ndarray) -> list[float]:
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return [0, 0, 0, 0]
    return [float(xs.min()), float(ys.min()), float(xs.max() - xs.min() + 1), float(ys.max() - ys.min() + 1)]


def polygon_mask(h: int, w: int, polygon: list[list[float]]) -> np.ndarray:
    img = Image.new("1", (w, h), 0)
    if len(polygon) >= 3:
        ImageDraw.Draw(img).polygon([tuple(p) for p in polygon], fill=1)
    return np.array(img, dtype=bool)
