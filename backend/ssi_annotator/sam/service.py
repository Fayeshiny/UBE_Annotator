"""SAM 交互式分割会话管理。

一个会话对应一个“分割片段”（同一空间情境或稳定可视区间内的一段视频），
片段内的多个关键结构作为不同 obj_id 并行跟踪。会话只保存在内存中，服务重启后需重新建立；
已生成的掩膜保存在数据库 mask_frames 表中，不受影响。
"""

import json
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field

from .. import config, db, media, rle
from . import get_backend
from .candidates import accept_mask, grid_points

_gpu_lock = threading.Lock()
_sessions: dict[str, "Session"] = {}
_sessions_lock = threading.Lock()
IDLE_TIMEOUT_SEC = 30 * 60


@dataclass
class CandidateJob:
    id: str
    frame_idx: int
    time_ms: int
    status: str = "queued"
    progress: float = 0.0
    error: str | None = None
    candidates: list[dict] = field(default_factory=list)
    cancel: bool = False

    def public(self, include_masks=True):
        result = {"id": self.id, "frame_idx": self.frame_idx, "time_ms": self.time_ms,
                  "status": self.status, "progress": self.progress, "error": self.error}
        if include_masks:
            result["candidates"] = self.candidates if self.status == "completed" else []
        return result


@dataclass
class Session:
    id: str
    assignment_id: int
    case_id: str
    start_ms: int
    end_ms: int
    stride: int
    width: int
    height: int
    status: str = "loading"  # loading | ready | propagating | error | closed
    error: str | None = None
    progress: float = 0.0
    frames: list[int] = field(default_factory=list)  # 局部帧号 → 全局帧号
    backend_sid: str | None = None
    objects: dict[int, int] = field(default_factory=dict)  # mask_objects.id → obj_id
    last_used: float = field(default_factory=time.time)
    cancel: bool = False
    candidate_job: CandidateJob | None = None

    @property
    def candidates_busy(self):
        return self.candidate_job is not None and self.candidate_job.status in ("queued", "running")

    def public(self) -> dict:
        return {
            "id": self.id,
            "assignment_id": self.assignment_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "stride": self.stride,
            "status": self.status,
            "error": self.error,
            "progress": self.progress,
            "frames": self.frames,
            "objects": list(self.objects.keys()),
            "backend": get_backend().name if self.status != "error" else None,
            "candidate_job": self.candidate_job.public(False) if self.candidate_job else None,
        }

    def local(self, global_idx: int) -> int:
        """全局帧号 → 最近的局部帧号。"""
        import bisect

        i = bisect.bisect_left(self.frames, global_idx)
        if i >= len(self.frames):
            return len(self.frames) - 1
        if i > 0 and global_idx - self.frames[i - 1] < self.frames[i] - global_idx:
            return i - 1
        return i


def _cleanup_idle():
    now = time.time()
    with _sessions_lock:
        stale = [s for s in _sessions.values() if now - s.last_used > IDLE_TIMEOUT_SEC
                 and s.status != "propagating" and not s.candidates_busy]
    for s in stale:
        close_session(s.id)


def create_session(assignment_id: int, case: dict, start_ms: int, end_ms: int, stride: int) -> Session:
    _cleanup_idle()
    s = Session(
        id=uuid.uuid4().hex[:12],
        assignment_id=assignment_id,
        case_id=case["case_id"],
        start_ms=start_ms,
        end_ms=end_ms,
        stride=max(1, stride),
        width=case["width"],
        height=case["height"],
    )
    with _sessions_lock:
        _sessions[s.id] = s
    threading.Thread(target=_load, args=(s, case), daemon=True).start()
    return s


def _frames_dir(sid: str):
    return config.cache_dir() / "sam" / sid


def _load(s: Session, case: dict):
    try:
        times = media.frame_times(s.case_id)
        video = media.case_video_dir(s.case_id) / case["video_file"]
        first = media.time_to_frame(times, s.start_ms)
        last = media.time_to_frame(times, s.end_ms)
        s.frames = media.extract_frames(video, times, first, last, s.stride, _frames_dir(s.id))
        with _gpu_lock:
            s.backend_sid = get_backend().start(_frames_dir(s.id), s.width, s.height)
        s.status = "ready"
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        s.status, s.error = "error", str(e)


def get_session(sid: str) -> Session | None:
    s = _sessions.get(sid)
    if s:
        s.last_used = time.time()
    return s


def list_sessions(assignment_id: int) -> list[Session]:
    return [s for s in _sessions.values() if s.assignment_id == assignment_id]


def close_session(sid: str):
    with _sessions_lock:
        s = _sessions.pop(sid, None)
    if s is None:
        return
    s.cancel = True
    if s.candidate_job:
        s.candidate_job.cancel = True
    s.status = "closed"
    if s.backend_sid:
        try:
            with _gpu_lock:
                get_backend().close(s.backend_sid)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    shutil.rmtree(_frames_dir(sid), ignore_errors=True)


def attach_object(s: Session, object_pk: int) -> int:
    if object_pk not in s.objects:
        s.objects[object_pk] = (max(s.objects.values()) + 1) if s.objects else 1
    return s.objects[object_pk]


def save_frame(object_pk: int, frame_idx: int, time_ms: int, mask, source: str, user_id: int | None, conn=None):
    r = rle.encode(mask)
    sql = (
        "INSERT INTO mask_frames (object_pk, frame_idx, time_ms, rle, area, source, updated_by, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(object_pk, frame_idx) DO UPDATE SET "
        "rle = excluded.rle, area = excluded.area, source = excluded.source, "
        "updated_by = excluded.updated_by, updated_at = excluded.updated_at"
    )
    args = (object_pk, frame_idx, time_ms, json.dumps(r), rle.area(r), source, user_id, db.now())
    if conn is not None:
        conn.execute(sql, args)
    else:
        with db.tx() as c:
            c.execute(sql, args)
    return r


def prompt(s: Session, object_pk: int, frame_idx: int, points, labels, box, user_id: int) -> tuple[int, dict]:
    if s.candidates_busy:
        raise RuntimeError("正在生成自动候选，请等待完成或取消后再添加提示")
    if s.status != "ready":
        raise RuntimeError(f"会话状态为 {s.status}，暂不能添加提示")
    obj = attach_object(s, object_pk)
    local = s.local(frame_idx)
    with _gpu_lock:
        mask = get_backend().prompt(s.backend_sid, local, obj, points, labels, box)
    g = s.frames[local]
    times = media.frame_times(s.case_id)
    t = times[g] if g < len(times) else 0
    with db.tx() as conn:
        r = save_frame(object_pk, g, t, mask, "prompt", user_id, conn)
        conn.execute(
            "UPDATE mask_objects SET source_frame = COALESCE(source_frame, ?), annotation_source = 'sam_propagated', "
            "verified = 0, frame_stride = ?, updated_at = ? WHERE id = ?",
            (t, s.stride, db.now(), object_pk),
        )
        conn.execute(
            "INSERT INTO mask_history (object_pk, frame_idx, action, detail, user_id, created_at) VALUES (?, ?, 'prompt', ?, ?, ?)",
            (object_pk, g, json.dumps({"points": points, "labels": labels, "box": box, "backend": get_backend().name}), user_id, db.now()),
        )
    return g, r


def start_propagation(s: Session, start_frame_idx: int, direction: str, user_id: int):
    if s.candidates_busy:
        raise RuntimeError("正在生成自动候选，请等待完成或取消后再传播")
    if s.status != "ready":
        raise RuntimeError(f"会话状态为 {s.status}，暂不能传播")
    if not s.objects:
        raise RuntimeError("请先在关键帧上为至少一个结构添加提示")
    s.status, s.progress, s.cancel = "propagating", 0.0, False
    threading.Thread(target=_propagate, args=(s, s.local(start_frame_idx), direction, user_id), daemon=True).start()


def start_candidates(s: Session, case: dict, frame_idx: int) -> CandidateJob:
    times = media.frame_times(s.case_id)
    if not 0 <= frame_idx < len(times) or not s.start_ms <= times[frame_idx] <= s.end_ms:
        raise ValueError("请选择当前分割片段内的一帧")
    with _sessions_lock:
        if s.status != "ready" or s.candidates_busy:
            raise RuntimeError("片段尚未就绪或已有任务正在运行，请稍后再生成候选")
        job = CandidateJob(uuid.uuid4().hex[:12], frame_idx, times[frame_idx])
        s.candidate_job = job
    threading.Thread(target=_generate_candidates, args=(s, case, job), daemon=True).start()
    return job


def _generate_candidates(s: Session, case: dict, job: CandidateJob):
    """使用独立单帧会话；不读取或写入正式目标、掩膜及复核状态。"""
    directory = config.cache_dir() / "candidates" / job.id
    backend = None
    backend_sid = None
    try:
        job.status = "running"
        times = media.frame_times(s.case_id)
        video = media.case_video_dir(s.case_id) / case["video_file"]
        # 使用用户选中的精确帧；即使不在片段抽帧序列中，也不替换为邻近帧。
        frames = media.extract_frames(video, times, job.frame_idx, job.frame_idx, 1, directory)
        if frames != [job.frame_idx] or len(list(directory.glob("*.jpg"))) != 1:
            raise RuntimeError("无法读取所选视频帧")
        if job.cancel:
            return
        with _gpu_lock:
            backend = get_backend()
            backend_sid = backend.start(directory, s.width, s.height)
        accepted = []
        results = []
        points = grid_points(s.width, s.height)
        for index, point in enumerate(points):
            if job.cancel:
                return
            with _gpu_lock:
                mask = backend.prompt(backend_sid, 0, 1, [point], [1], None)
                # 每次独立尝试一个目标，下一位置不继承上一次提示和目标状态。
                backend.remove_object(backend_sid, 1)
            if mask is not None:
                mask = accept_mask(mask, s.width, s.height, accepted)
                if mask is not None:
                    accepted.append(mask.copy())
                    results.append({"id": len(results) + 1, "rle": rle.encode(mask),
                                    "area": int(mask.sum()), "bbox": rle.bbox(mask), "point": point})
            job.progress = (index + 1) / len(points)
        if not job.cancel:
            job.candidates = results
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        job.error = f"候选生成失败：{e}"
    finally:
        if backend_sid is not None:
            try:
                with _gpu_lock:
                    backend.close(backend_sid)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                job.error = job.error or f"候选会话清理失败：{e}"
        shutil.rmtree(directory, ignore_errors=True)
        job.status = "cancelled" if job.cancel else ("error" if job.error else "completed")


def _propagate(s: Session, start_local: int, direction: str, user_id: int):
    times = media.frame_times(s.case_id)
    n = len(s.frames)
    total = {"forward": n - start_local, "backward": start_local + 1, "both": n}[direction] or 1
    inv = {v: k for k, v in s.objects.items()}
    done = 0
    try:
        with _gpu_lock:
            for local, masks in get_backend().propagate(s.backend_sid, start_local, direction):
                if s.cancel:
                    break
                g = s.frames[local]
                t = times[g] if g < len(times) else 0
                with db.tx() as conn:
                    for obj, m in masks.items():
                        pk = inv.get(obj)
                        if pk is None or m is None:
                            continue
                        cur = conn.execute(
                            "SELECT source FROM mask_frames WHERE object_pk = ? AND frame_idx = ?", (pk, g)
                        ).fetchone()
                        # 人工修改过或作为提示的帧不被传播结果覆盖
                        if cur and cur["source"] in ("manual_edit", "prompt"):
                            continue
                        save_frame(pk, g, t, m, "sam_propagated", user_id, conn)
                done += 1
                s.progress = min(1.0, done / total)
        with db.tx() as conn:
            for pk in s.objects:
                conn.execute(
                    "UPDATE mask_objects SET annotation_source = 'sam_propagated', verified = 0, updated_at = ? WHERE id = ?",
                    (db.now(), pk),
                )
                conn.execute(
                    "INSERT INTO mask_history (object_pk, action, detail, user_id, created_at) VALUES (?, 'propagate', ?, ?, ?)",
                    (pk, json.dumps({"direction": direction, "start_frame": s.frames[start_local], "backend": get_backend().name}), user_id, db.now()),
                )
        s.status = "ready"
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        s.status, s.error = "ready", f"传播失败：{e}"
