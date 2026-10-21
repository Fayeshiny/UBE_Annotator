"""视频处理：探测、去元数据转码、帧时间戳表、按帧号抽帧。

所有时间以“播放文件”（web 版 MP4）的帧时间戳为准：
frame_idx 是按显示时间排序的帧序号，time_ms 是该帧相对首帧的毫秒时间戳。
"""

import bisect
import csv
import json
import shutil
import subprocess
import uuid
from functools import lru_cache
from pathlib import Path

from . import config


class MediaError(RuntimeError):
    pass


def _run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise MediaError(f"{cmd[0]} 失败: {p.stderr.strip()[-800:]}")
    return p.stdout


def probe(path: Path) -> dict:
    out = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,codec_name,nb_frames:format=duration",
        "-of", "json", str(path),
    ])
    info = json.loads(out)
    if not info.get("streams"):
        raise MediaError("文件中没有视频流")
    s = info["streams"][0]

    def rate(v):
        n, _, d = (v or "0/1").partition("/")
        return float(n) / float(d or 1) if float(d or 1) else 0.0

    return {
        "width": int(s["width"]),
        "height": int(s["height"]),
        "fps": rate(s.get("avg_frame_rate")) or rate(s.get("r_frame_rate")),
        "codec": s.get("codec_name"),
        "duration_sec": float(info.get("format", {}).get("duration", 0) or 0),
    }


def transcode_for_web(src: Path, dst: Path, start_sec: float | None = None, end_sec: float | None = None):
    """转为浏览器可精确逐帧定位的 H.264 MP4。

    - 不改变帧率、不丢帧（fps_mode passthrough），保证帧号与原始视频一一对应；
    - 每 15 帧一个关键帧，保证随机定位速度；
    - 去掉音轨和全部容器元数据（拍摄设备、时间、可能的患者信息）。
    画面内烧录的文字/患者信息无法自动去除，需要在导入前人工确认。
    """
    # 中断时只留下临时文件，不把不完整 MP4 发布为正式播放文件。
    temporary = dst.with_name(f".{dst.stem}.{uuid.uuid4().hex}.partial{dst.suffix}")
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error"]
    if start_sec is not None:
        cmd += ["-ss", f"{start_sec:.3f}"]
    cmd += ["-i", str(src)]
    if end_sec is not None:
        cmd += ["-t", f"{end_sec - (start_sec or 0):.3f}"]
    cmd += [
        "-map", "0:v:0", "-an", "-sn", "-dn",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-fps_mode", "passthrough",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-g", "15", "-keyint_min", "15", "-sc_threshold", "0",
        "-movflags", "+faststart",
        str(temporary),
    ]
    try:
        _run(cmd)
        temporary.replace(dst)
    finally:
        temporary.unlink(missing_ok=True)


def build_frame_map(video: Path, out_csv: Path) -> list[int]:
    """生成 frame_timestamp_map.csv（frame_idx, time_ms, pts_sec），并返回 time_ms 列表。

    使用 packet 时间戳（无需解码，长视频也很快），按 pts 排序得到显示顺序。
    """
    out = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(video),
    ])
    pts = sorted(float(x.strip().rstrip(",")) for x in out.splitlines() if x.strip() and x.strip() != "N/A")
    if not pts:
        raise MediaError("无法读取帧时间戳")
    t0 = pts[0]
    times_ms = [round((p - t0) * 1000) for p in pts]
    with open(out_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["frame_idx", "time_ms", "pts_sec"])
        for i, (p, t) in enumerate(zip(pts, times_ms)):
            w.writerow([i, t, f"{p:.6f}"])
    return times_ms


def case_video_dir(case_id: str) -> Path:
    return config.cases_dir() / case_id / "video"


@lru_cache(maxsize=64)
def _load_frame_times(path: str, size: int, mtime_ns: int, ctime_ns: int, inode: int) -> tuple[int, ...]:
    with open(path) as f:
        r = csv.DictReader(f)
        return tuple(int(row["time_ms"]) for row in r)


def frame_times(case_id: str) -> tuple[int, ...]:
    p = case_video_dir(case_id) / "frame_timestamp_map.csv"
    if not p.exists():
        return ()
    stat = p.stat()
    return _load_frame_times(str(p), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)


def time_to_frame(times: tuple[int, ...], time_ms: int | None, fps: float | None = None) -> int | None:
    """时间戳所在帧：最后一个 time_ms <= t 的帧。"""
    if time_ms is None:
        return None
    if times:
        i = bisect.bisect_right(times, int(time_ms)) - 1
        return max(0, min(i, len(times) - 1))
    if fps:
        return int(time_ms * fps / 1000 + 1e-6)
    return None


@lru_cache(maxsize=16)
def _load_pts(path: str, size: int, mtime_ns: int, ctime_ns: int, inode: int) -> tuple[float, ...]:
    with open(path) as f:
        return tuple(float(row["pts_sec"]) for row in csv.DictReader(f))


def frame_pts(case_id: str) -> tuple[float, ...]:
    """Unrounded display timestamps, including a possible nonzero stream start."""
    path = case_video_dir(case_id) / "frame_timestamp_map.csv"
    stat = path.stat()
    return _load_pts(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)


@lru_cache(maxsize=8)
def _probe_pts(path: str, size: int, mtime_ns: int, ctime_ns: int, inode: int) -> tuple[float, ...]:
    out = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time", "-of", "csv=p=0", path,
    ])
    return tuple(sorted(float(x.strip().rstrip(",")) for x in out.splitlines()
                        if x.strip() and x.strip() != "N/A"))


def extract_frame_batch(video: Path, pts: tuple[float, ...], first: int, last: int,
                        stride: int, out_dir: Path, timeout: float | None = None) -> list[int]:
    """Seek to the preceding keyframe and stop after the requested output count.

    A threshold between the preceding and requested frame avoids rounded-ms and
    29.97/VFR off-by-one errors. Keep absolute PTS: input accurate_seek otherwise
    applies the stream start offset twice for some nonzero-start MP4s.
    """
    if stride < 1 or first < 0 or last < first or last >= len(pts):
        raise MediaError("抽帧区间无效")
    threshold = (pts[first - 1] + pts[first]) / 2 if first else pts[0] - 0.00001
    indices = list(range(first, last + 1, stride))
    out_dir.mkdir(parents=True, exist_ok=True)
    vf = f"select='gte(t,{threshold:.9f})',select='not(mod(n\\,{stride}))'"
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-v", "error", "-threads", "2",
        "-filter_threads", "1", "-copyts", "-seek_timestamp", "1",
        "-ss", f"{threshold:.9f}", "-noaccurate_seek", "-noautorotate", "-i", str(video),
        "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", vf,
        "-fps_mode", "passthrough", "-frames:v", str(len(indices)),
        "-c:v", "mjpeg", "-threads", "1", "-q:v", "2", "-start_number", "0",
        str(out_dir / "%05d.jpg"),
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise MediaError("帧图片生成超时，请稍后重试") from e
    if p.returncode:
        raise MediaError(f"帧图片生成失败：{p.stderr.strip()[-800:]}")
    if len(list(out_dir.glob("*.jpg"))) != len(indices):
        raise MediaError("视频帧与时间戳表不一致，请重新处理视频")
    return indices


def extract_frames(video: Path, times: tuple[int, ...], first: int, last: int, stride: int, out_dir: Path) -> list[int]:
    """Extract exact display-order frames for SAM, using the same PTS as the viewer."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    first = max(0, first)
    last = min(last, len(times) - 1)
    map_path = video.parent / "frame_timestamp_map.csv"
    if map_path.is_file():
        stat = map_path.stat()
        pts = _load_pts(str(map_path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
    else:
        stat = video.stat()
        pts = _probe_pts(str(video), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
    if len(pts) != len(times):
        raise MediaError("视频帧与时间戳表不一致，请重新处理视频")
    return extract_frame_batch(video, pts, first, last, stride, out_dir)


@lru_cache(maxsize=16)
def _packet_ranges(path: str, size: int, mtime_ns: int, ctime_ns: int, inode: int) -> tuple:
    """Keyframe groups with byte bounds; no video decoding or frame-rate conversion."""
    info = json.loads(_run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,pos,size,flags", "-of", "json", path,
    ]))
    groups = []
    current = []
    for packet in info.get("packets", []):
        if not all(k in packet for k in ("pts_time", "pos", "size")):
            raise MediaError("视频缺少可定位的 packet 索引")
        if "K" in packet.get("flags", "") and current:
            groups.append(current)
            current = []
        current.append((float(packet["pts_time"]), int(packet["pos"]), int(packet["size"])))
    if current:
        groups.append(current)
    return tuple((min(p[0] for p in g), max(p[0] for p in g),
                  min(p[1] for p in g), max(p[1] + p[2] for p in g) - 1) for g in groups)


def video_byte_index(video: Path, pts: tuple[float, ...]) -> list[list[int]]:
    stat = video.stat()
    groups = _packet_ranges(str(video), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
    return [[bisect.bisect_left(pts, first), bisect.bisect_right(pts, last) - 1, start, end]
            for first, last, start, end in groups if 0 <= start <= end < stat.st_size]
