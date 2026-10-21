"""Authenticated frame delivery support: exact PTS, bounded disk LRU and decoding.

The cache is local to this service process (run one uvicorn worker). Background
prewarming gets at most one of two decoder slots and never queues ahead of a
foreground request. JPEGs are read while holding the index lock, so eviction
cannot remove a file between returning its path and HTTP transmission.
"""

import bisect
import hashlib
import logging
import os
import tempfile
import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import config, media

logger = logging.getLogger(__name__)
BATCH_SIZE = 5
MAX_WARM_JOBS = 8
MAX_WARM_SAMPLES = 10000


class Busy(RuntimeError):
    pass


class SourceChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class Source:
    case_id: str
    video: Path
    map_path: Path
    revision: str
    pts: tuple[float, ...]
    times: tuple[int, ...]


def _revision(video: Path, map_path: Path) -> str:
    parts = ["jpeg-pts-v1"]
    for path in (video, map_path):
        stat = path.stat()
        parts.extend((str(path.resolve()), str(stat.st_ino), str(stat.st_size),
                      str(stat.st_mtime_ns), str(stat.st_ctime_ns)))
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:24]


def revision(case: dict) -> str | None:
    if case.get("media_status") != "ready" or not case.get("video_file"):
        return None
    directory = media.case_video_dir(case["case_id"])
    try:
        return _revision(directory / case["video_file"], directory / "frame_timestamp_map.csv")
    except FileNotFoundError:
        return None


def source(case: dict) -> Source:
    directory = media.case_video_dir(case["case_id"])
    video, map_path = directory / case["video_file"], directory / "frame_timestamp_map.csv"
    rev = _revision(video, map_path)
    pts, times = media.frame_pts(case["case_id"]), media.frame_times(case["case_id"])
    if not pts or len(pts) != len(times) or len(pts) != case["frame_count"]:
        raise media.MediaError("视频帧与时间戳表不一致，请重新处理视频")
    if _revision(video, map_path) != rev:
        raise SourceChanged("视频正在更新，请刷新页面")
    return Source(case["case_id"], video, map_path, rev, pts, times)


class FrameCache:
    def __init__(self, root: Path, max_bytes: int = 2 * 1024**3):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.files: OrderedDict[Path, int] = OrderedDict()
        self.total_bytes = 0
        self.inflight: dict[tuple[str, int], threading.Event] = {}
        self.active = 0
        self.background_active = 0
        self.foreground_waiting = 0
        self.stopped = False
        self.warm_queue = deque()
        self.warm_states: OrderedDict[tuple[str, int], dict] = OrderedDict()
        self.warm_thread: threading.Thread | None = None
        # Only our immutable JPEGs count toward the cache; no video/SAM files.
        entries = []
        for path in root.glob("*/*.jpg"):
            try:
                stat = path.stat()
                entries.append((stat.st_mtime_ns, path, stat.st_size))
            except FileNotFoundError:
                continue
        for _, path, size in sorted(entries):
            self.files[path] = size
            self.total_bytes += size
        self._evict()

    def _evict(self):
        while self.total_bytes > self.max_bytes and self.files:
            path, size = self.files.popitem(last=False)
            path.unlink(missing_ok=True)
            self.total_bytes -= size
            try:
                path.parent.rmdir()
            except OSError:
                pass

    def _read(self, path: Path) -> bytes | None:
        with self.lock:
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                size = self.files.pop(path, 0)
                self.total_bytes -= size
                return None
            if path not in self.files:
                self.files[path] = len(data)
                self.total_bytes += len(data)
            self.files.move_to_end(path)
            return data

    @contextmanager
    def _decoder(self, background: bool, cancelled: threading.Event | None = None):
        with self.condition:
            if self.stopped or (cancelled and cancelled.is_set()):
                raise Busy("帧缓存服务正在停止")
            if background:
                if self.active >= 2 or self.background_active or self.foreground_waiting:
                    raise Busy("正在优先加载当前画面")
                self.background_active += 1
            else:
                if self.foreground_waiting >= 16:
                    raise Busy("帧图片请求较多，请稍后重试")
                self.foreground_waiting += 1
                deadline = time.monotonic() + 10
                try:
                    while self.active >= 2:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or self.stopped or (cancelled and cancelled.is_set()):
                            raise Busy("帧图片请求较多，请稍后重试")
                        self.condition.wait(min(remaining, 0.25))
                finally:
                    self.foreground_waiting -= 1
                if cancelled and cancelled.is_set():
                    raise Busy("请求已取消")
            self.active += 1
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                if background:
                    self.background_active -= 1
                self.condition.notify_all()

    def get(self, src: Source, frame_idx: int, *, prefetch: bool = False,
            adjacent: bool = True, cancelled: threading.Event | None = None) -> tuple[bytes, bool]:
        if not 0 <= frame_idx < len(src.pts):
            raise IndexError("帧号超出视频范围")
        if _revision(src.video, src.map_path) != src.revision:
            raise SourceChanged("视频正在更新，请刷新页面")
        directory = self.root / src.revision
        path = directory / f"{frame_idx:09d}.jpg"
        key = (src.revision, frame_idx // BATCH_SIZE)
        deadline = time.monotonic() + 12
        while True:
            with self.lock:
                cached = self._read(path)
                if cached is not None:
                    return cached, True
                pending = self.inflight.get(key)
                if pending is None:
                    pending = threading.Event()
                    self.inflight[key] = pending
                    break
            if prefetch or time.monotonic() >= deadline or (cancelled and cancelled.is_set()):
                raise Busy("帧图片正在生成，请稍后重试")
            pending.wait(min(0.25, max(0, deadline - time.monotonic())))

        try:
            with self._decoder(prefetch, cancelled):
                first = frame_idx // BATCH_SIZE * BATCH_SIZE if adjacent else frame_idx
                last = min(first + BATCH_SIZE - 1, len(src.pts) - 1) if adjacent else frame_idx
                with tempfile.TemporaryDirectory(prefix="decode-", dir=self.root) as temporary:
                    output = Path(temporary)
                    indices = media.extract_frame_batch(src.video, src.pts, first, last, 1, output, timeout=30)
                    if _revision(src.video, src.map_path) != src.revision:
                        raise SourceChanged("视频正在更新，请刷新页面")
                    result = (output / f"{frame_idx - first:05d}.jpg").read_bytes()
                    with self.lock:
                        directory.mkdir(exist_ok=True)
                        for sequence, idx in enumerate(indices):
                            destination = directory / f"{idx:09d}.jpg"
                            generated = output / f"{sequence:05d}.jpg"
                            size = generated.stat().st_size
                            # A single oversized image can still be served without
                            # violating the disk budget or evicting all useful frames.
                            if size > self.max_bytes:
                                continue
                            generated.replace(destination)
                            self.total_bytes -= self.files.pop(destination, 0)
                            self.files[destination] = size
                            self.total_bytes += size
                        self._evict()
                    return result, False
        finally:
            with self.lock:
                self.inflight.pop(key, None)
                pending.set()

    def request_warm(self, src: Source, interval_ms: int) -> dict:
        if interval_ms < 1000:
            raise ValueError("采样间隔不能小于 1 秒")
        key = (src.revision, interval_ms)
        with self.condition:
            existing = self.warm_states.get(key)
            if existing and existing["status"] in ("queued", "running", "ready"):
                resident = True
                if existing["status"] == "ready":
                    for t in range(0, src.times[-1] + 1, interval_ms)[:MAX_WARM_SAMPLES]:
                        idx = max(0, bisect.bisect_right(src.times, t) - 1)
                        path = self.root / src.revision / f"{idx:09d}.jpg"
                        if path not in self.files or not path.is_file():
                            resident = False
                            break
                if resident:
                    self.warm_states.move_to_end(key)
                    return dict(existing)
            if self.stopped or len(self.warm_queue) >= MAX_WARM_JOBS:
                return {"status": "busy", "prepared": 0}
            state = {"status": "queued", "prepared": 0, "sampling_interval_ms": interval_ms}
            self.warm_states[key] = state
            while len(self.warm_states) > 128:
                self.warm_states.popitem(last=False)
            self.warm_queue.append((src, interval_ms))
            if self.warm_thread is None or not self.warm_thread.is_alive():
                self.warm_thread = threading.Thread(target=self._warm_worker, name="frame-prewarm", daemon=True)
                self.warm_thread.start()
            self.condition.notify_all()
            return dict(state)

    def warm(self, src: Source, interval_ms: int, state: dict | None = None):
        if interval_ms < 1000:
            raise ValueError("采样间隔不能小于 1 秒")
        if state is None:
            state = {"prepared": 0}
        state["status"] = "running"
        # Use the application's time->frame rule (last frame <= sample time).
        samples = range(0, src.times[-1] + 1, interval_ms)
        state["total"] = min(len(samples), MAX_WARM_SAMPLES)
        state["truncated"] = len(samples) > MAX_WARM_SAMPLES
        job_deadline = time.monotonic() + 1800
        for t in samples[:MAX_WARM_SAMPLES]:
            idx = max(0, bisect.bisect_right(src.times, t) - 1)
            while True:
                with self.condition:
                    if self.stopped or time.monotonic() > job_deadline:
                        state["status"] = "stopped"
                        return state
                try:
                    self.get(src, idx, prefetch=True, adjacent=False)
                    state["prepared"] += 1
                    break
                except Busy:
                    with self.condition:
                        self.condition.wait(0.25)
        state["status"] = "ready"
        return dict(state)

    def _warm_worker(self):
        while True:
            with self.condition:
                if self.stopped or not self.warm_queue:
                    self.warm_thread = None
                    return
                src, interval = self.warm_queue.popleft()
                state = self.warm_states[(src.revision, interval)]
            try:
                self.warm(src, interval, state)
            except (OSError, media.MediaError, SourceChanged):
                state["status"] = "error"
                logger.warning("Frame prewarm failed for %s", src.case_id, exc_info=True)

    def close(self):
        with self.condition:
            self.stopped = True
            self.warm_queue.clear()
            self.condition.notify_all()
            worker = self.warm_thread
        if worker is not None:
            worker.join(timeout=1)


_manager_lock = threading.Lock()
_managers: dict[tuple[str, int], FrameCache] = {}


def manager() -> FrameCache:
    root = config.cache_dir() / "frames"
    max_bytes = max(1, int(os.environ.get("SSI_FRAME_CACHE_MAX_MB", "2048"))) * 1024**2
    key = (str(root), max_bytes)
    with _manager_lock:
        cache = _managers.get(key)
        if cache is None or cache.stopped:
            cache = _managers[key] = FrameCache(root, max_bytes)
        return cache


def prewarm_import(case: dict, interval_ms: int):
    if os.environ.get("SSI_FRAME_CACHE_PREWARM", "1") != "0":
        try:
            manager().request_warm(source(case), max(1000, int(interval_ms)))
        except (OSError, ValueError, media.MediaError, SourceChanged):
            # Optimization must not turn a successful video import into an error.
            logger.warning("Could not schedule frame prewarm for %s", case["case_id"], exc_info=True)


def close_all():
    with _manager_lock:
        for cache in _managers.values():
            cache.close()
        _managers.clear()
