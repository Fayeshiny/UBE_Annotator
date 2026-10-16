"""Meta SAM 2.1 视频预测器适配（https://github.com/facebookresearch/sam2），作为 SAM 3.x 的备选。

    pip install 'git+https://github.com/facebookresearch/sam2.git'
    # 下载权重，例如 sam2.1_hiera_large.pt
    export SSI_SAM_CHECKPOINT=/path/to/sam2.1_hiera_large.pt
    export SSI_SAM2_CONFIG=configs/sam2.1/sam2.1_hiera_l.yaml
"""

import os
import uuid
from pathlib import Path

import numpy as np

from .base import SamBackend


class Sam2Backend(SamBackend):
    name = "sam2"

    def __init__(self):
        import torch
        from sam2.build_sam import build_sam2_video_predictor

        if torch.cuda.is_available():
            device = "cuda"
        elif torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"
        self.device = device
        self.predictor = build_sam2_video_predictor(
            os.environ.get("SSI_SAM2_CONFIG", "configs/sam2.1/sam2.1_hiera_l.yaml"),
            os.environ["SSI_SAM_CHECKPOINT"],
            device=device,
        )
        self.states: dict[str, object] = {}

    def _ctx(self):
        import contextlib

        import torch

        if self.device == "cuda":
            return torch.autocast("cuda", dtype=torch.bfloat16)
        return contextlib.nullcontext()

    def start(self, frames_dir: Path, width: int, height: int) -> str:
        import torch

        sid = uuid.uuid4().hex
        with torch.inference_mode(), self._ctx():
            self.states[sid] = self.predictor.init_state(video_path=str(frames_dir), offload_video_to_cpu=True)
        return sid

    def prompt(self, session, frame, obj_id, points, labels, box):
        import torch

        kwargs = {}
        if points:
            kwargs["points"] = np.array(points, dtype=np.float32)
            kwargs["labels"] = np.array(labels, dtype=np.int32)
        if box is not None:
            kwargs["box"] = np.array(box, dtype=np.float32)
        with torch.inference_mode(), self._ctx():
            _, obj_ids, logits = self.predictor.add_new_points_or_box(
                inference_state=self.states[session], frame_idx=frame, obj_id=obj_id, clear_old_points=True, **kwargs
            )
        i = list(obj_ids).index(obj_id)
        return (logits[i] > 0.0).cpu().numpy()[0]

    def propagate(self, session, start_frame, direction):
        import torch

        dirs = {"forward": [False], "backward": [True], "both": [False, True]}[direction]
        for reverse in dirs:
            with torch.inference_mode(), self._ctx():
                for f, obj_ids, logits in self.predictor.propagate_in_video(
                    self.states[session], start_frame_idx=start_frame, reverse=reverse
                ):
                    yield f, {int(o): (logits[i] > 0.0).cpu().numpy()[0] for i, o in enumerate(obj_ids)}

    def remove_object(self, session, obj_id):
        self.predictor.remove_object(self.states[session], obj_id)

    def close(self, session):
        st = self.states.pop(session, None)
        if st is not None:
            self.predictor.reset_state(st)
