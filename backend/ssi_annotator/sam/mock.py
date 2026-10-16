import time
import uuid
from pathlib import Path

import numpy as np

from .base import SamBackend


class MockBackend(SamBackend):
    """模拟后端：点提示 → 以正点为中心的椭圆并扣除负点附近区域；框提示 → 内切椭圆。
    传播时掩膜逐帧轻微平移。仅用于开发、演示与标注员培训，不产生任何真实分割。"""

    name = "mock"

    def __init__(self):
        self.sessions: dict[str, dict] = {}

    def start(self, frames_dir: Path, width: int, height: int) -> str:
        sid = uuid.uuid4().hex
        n = len(list(frames_dir.glob("*.jpg")))
        self.sessions[sid] = {"w": width, "h": height, "n": n, "objs": {}}
        return sid

    def _shape(self, s, points, labels, box):
        h, w = s["h"], s["w"]
        yy, xx = np.mgrid[0:h, 0:w]
        if box is not None:
            x0, y0, x1, y1 = box
            cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, max(abs(x1 - x0) / 2, 2), max(abs(y1 - y0) / 2, 2)
        else:
            pos = [p for p, lab in zip(points, labels) if lab == 1] or points
            cx, cy = np.mean([p[0] for p in pos]), np.mean([p[1] for p in pos])
            rx, ry = w * 0.08, h * 0.08
        m = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1
        for p, lab in zip(points, labels):
            if lab == 0:
                m &= (xx - p[0]) ** 2 + (yy - p[1]) ** 2 > (min(w, h) * 0.04) ** 2
        return m

    def prompt(self, session, frame, obj_id, points, labels, box):
        s = self.sessions[session]
        m = self._shape(s, points, labels, box)
        s["objs"].setdefault(obj_id, {})[frame] = m
        return m

    def propagate(self, session, start_frame, direction):
        s = self.sessions[session]
        frames = []
        if direction in ("forward", "both"):
            frames += list(range(start_frame, s["n"]))
        if direction in ("backward", "both"):
            frames += list(range(start_frame - 1, -1, -1))
        for f in frames:
            out = {}
            for oid, prompts in s["objs"].items():
                if f in prompts:
                    out[oid] = prompts[f]
                    continue
                ref = min(prompts, key=lambda k: abs(k - f))
                shift = int(round((f - ref) * 0.5))
                out[oid] = np.roll(prompts[ref], shift, axis=1)
            time.sleep(0.005)
            yield f, out

    def remove_object(self, session, obj_id):
        self.sessions[session]["objs"].pop(obj_id, None)

    def close(self, session):
        self.sessions.pop(session, None)
