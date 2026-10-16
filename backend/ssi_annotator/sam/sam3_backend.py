"""Meta SAM 3 / SAM 3.1 视频预测器适配（https://github.com/facebookresearch/sam3）。

安装（GPU 服务器，CUDA ≥ 12.6，Python ≥ 3.12）：
    pip install torch==2.10.0 torchvision --index-url https://download.pytorch.org/whl/cu128
    pip install 'git+https://github.com/facebookresearch/sam3.git'
    hf auth login   # 需先在 huggingface.co/facebook/sam3.1 申请权重访问

接口基于官方 handle_request / handle_stream_request：
    start_session(resource_path=JPEG 目录) → add_prompt(points 相对坐标, point_labels, obj_id)
    → propagate_in_video(propagation_direction, start_frame_index) → 每帧 outputs:
    out_obj_ids / out_binary_masks。

注意：SAM 3 的 bounding_boxes 是“概念示例框”（会检测所有同类实例），不是 SAM 2 式的单目标框，
因此这里把框提示转换为框中心的正点；需要用框初始化时建议使用 sam2 后端。
"""

import os
from pathlib import Path

import numpy as np

from .base import SamBackend


class Sam3Backend(SamBackend):
    def __init__(self, version: str = "sam3.1"):
        import torch  # noqa: F401

        self.name = version
        ckpt = os.environ.get("SSI_SAM_CHECKPOINT") or None
        if version == "sam3.1":
            from sam3.model_builder import build_sam3_multiplex_video_predictor

            self.predictor = build_sam3_multiplex_video_predictor(
                checkpoint_path=ckpt,
                use_fa3=os.environ.get("SSI_SAM_USE_FA3", "0") == "1",  # FA3 需要 flash_attn_interface（Hopper）
            )
            self._accept_only_known_init_kwargs(self.predictor.model)
            self._keep_refined_masks_without_detector_cache(self.predictor.model)
        else:
            from sam3.model_builder import build_sam3_video_predictor

            kwargs = {"checkpoint_path": ckpt} if ckpt else {}
            self.predictor = build_sam3_video_predictor(**kwargs)
        self.size: dict[str, tuple[int, int]] = {}

    @staticmethod
    def _accept_only_known_init_kwargs(model) -> None:
        """sam3 的 Sam3BasePredictor.start_session 会传 offload_state_to_cpu 等参数，
        但 SAM 3.1 multiplex 模型的 init_state 不接受，这里丢弃它不认识的参数。"""
        import inspect

        orig = model.init_state
        known = set(inspect.signature(orig).parameters)

        def init_state(*args, **kwargs):
            return orig(*args, **{k: v for k, v in kwargs.items() if k in known})

        model.init_state = init_state

    @staticmethod
    def _keep_refined_masks_without_detector_cache(model) -> None:
        """纯点击交互时没有检测器（文本提示）预测，cached_frame_outputs 里没有该帧，
        sam3 的 _build_sam2_output 此时直接返回 {}，把刚算出的 SAM2 跟踪掩膜一并丢掉，
        导致只有提示帧有掩膜。这里在无缓存时直接返回跟踪得到的掩膜。"""
        orig = model._build_sam2_output

        def build(inference_state, frame_idx, refined_obj_id_to_mask=None):
            out = orig(inference_state, frame_idx, refined_obj_id_to_mask)
            if not out and refined_obj_id_to_mask:
                out = dict(refined_obj_id_to_mask)
            return out

        model._build_sam2_output = build

    def start(self, frames_dir: Path, width: int, height: int) -> str:
        r = self.predictor.handle_request(request={"type": "start_session", "resource_path": str(frames_dir)})
        sid = r["session_id"]
        self.size[sid] = (width, height)
        if self.name == "sam3.1":
            try:
                # SAM opens the session after the first image, then keeps loading
                # on a worker. Do not report ready while that worker is active.
                state = self.predictor._all_inference_states[sid]["state"]
                images = state["input_batch"].img_batch.tensors
                loader = getattr(images, "thread", None)
                if loader is not None:
                    loader.join()
                error = getattr(images, "exception", None)
                if error is not None:
                    raise RuntimeError(f"视频帧加载失败：{error}") from error
            except Exception:
                self.close(sid)
                raise
        return sid

    @staticmethod
    def _extract(outputs: dict, obj_id: int | None = None) -> dict[int, np.ndarray]:
        ids = np.asarray(outputs["out_obj_ids"]).tolist()
        masks = outputs["out_binary_masks"]
        res = {}
        for i, oid in enumerate(ids):
            m = masks[i]
            if hasattr(m, "cpu"):
                m = m.cpu().numpy()
            m = np.asarray(m).astype(bool)
            if m.ndim == 3:
                m = m[0]
            res[int(oid)] = m
        return res if obj_id is None else {obj_id: res.get(obj_id)}

    def prompt(self, session, frame, obj_id, points, labels, box):
        w, h = self.size[session]
        pts, labs = list(points), list(labels)
        if box is not None and not pts:
            pts, labs = [[(box[0] + box[2]) / 2, (box[1] + box[3]) / 2]], [1]
        rel = [[x / w, y / h] for x, y in pts]
        r = self.predictor.handle_request(request={
            "type": "add_prompt",
            "session_id": session,
            "frame_index": frame,
            "points": rel,
            "point_labels": labs,
            "obj_id": obj_id,
        })
        m = self._extract(r["outputs"], obj_id)[obj_id]
        return m if m is not None else np.zeros((h, w), dtype=bool)

    def propagate(self, session, start_frame, direction):
        import torch

        # autocast 是线程局部的：sam3 在构建模型的线程里开启了 bf16 autocast，
        # 服务在另一个线程里传播时必须自己开启，否则卷积会出现 bf16/float 类型不一致。
        with torch.autocast(device_type="cuda", dtype=torch.bfloat16):
            for r in self.predictor.handle_stream_request(request={
                "type": "propagate_in_video",
                "session_id": session,
                "propagation_direction": direction,
                "start_frame_index": start_frame,
            }):
                yield r["frame_index"], self._extract(r["outputs"])

    def remove_object(self, session, obj_id):
        self.predictor.handle_request(request={"type": "remove_object", "session_id": session, "obj_id": obj_id})

    def close(self, session):
        self.predictor.handle_request(request={"type": "close_session", "session_id": session})
        self.size.pop(session, None)
