"""视频处理：探测、去元数据转码、帧时间戳表、按帧号抽帧。

所有时间以“播放文件”（web 版 MP4）的帧时间戳为准：
frame_idx 是按显示时间排序的帧序号，time_ms 是该帧相对首帧的毫秒时间戳。
"""

import bisect
import csv
import json
import shutil
import subprocess
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
    cmd = ["ffmpeg", "-y", "-v", "error"]
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
        str(dst),
    ]
    _run(cmd)


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
def _load_frame_times(path: str, mtime: float) -> tuple[int, ...]:
    with open(path) as f:
        r = csv.DictReader(f)
        return tuple(int(row["time_ms"]) for row in r)


def frame_times(case_id: str) -> tuple[int, ...]:
    p = case_video_dir(case_id) / "frame_timestamp_map.csv"
    if not p.exists():
        return ()
    return _load_frame_times(str(p), p.stat().st_mtime)


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


def extract_frames(video: Path, times: tuple[int, ...], first: int, last: int, stride: int, out_dir: Path) -> list[int]:
    """把 [first, last] 区间、间隔 stride 的帧抽取为 JPEG 序列（00000.jpg ...），返回对应的全局帧号。

    先精确 seek 到首帧之前，再按相对帧号选帧，避免长视频从头解码。
    """
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    first = max(0, first)
    last = min(last, len(times) - 1)
    if last < first:
        raise MediaError("抽帧区间为空")
    half_frame = 0.0
    if len(times) > 1:
        half_frame = max(1, (times[min(first + 1, len(times) - 1)] - times[first])) / 2000
    seek = max(0.0, times[first] / 1000 - half_frame)
    count = last - first + 1
    sel = f"select='lt(n\\,{count})*not(mod(n\\,{stride}))'"
    _run([
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{seek:.4f}", "-i", str(video),
        "-vf", sel, "-fps_mode", "passthrough",
        "-q:v", "2", "-start_number", "0",
        str(out_dir / "%05d.jpg"),
    ])
    produced = sorted(out_dir.glob("*.jpg"))
    idxs = list(range(first, last + 1, stride))[: len(produced)]
    return idxs
