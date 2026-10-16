"""病例导入：建立病例目录、去元数据转码、生成帧时间戳表。"""

import re
import shutil
import threading
import traceback
from pathlib import Path

from . import db, media

CASE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
CASE_FIELDS = ("surgeon_id", "center_id", "operative_level", "cumulative_UBE_cases", "gold_set", "notes")


def get_case(case_id: str) -> dict | None:
    return db.row(db.connect().execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone())


def create_case(case_id: str, meta: dict) -> dict:
    if not CASE_ID_RE.match(case_id):
        raise ValueError("病例编号只能包含字母、数字、下划线和连字符，例如 SSI_0001")
    if get_case(case_id):
        raise ValueError(f"病例 {case_id} 已存在")
    fields = {k: meta.get(k) for k in CASE_FIELDS}
    fields["gold_set"] = 1 if fields.get("gold_set") else 0
    with db.tx() as conn:
        conn.execute(
            f"INSERT INTO cases (case_id, {', '.join(fields)}, created_at, updated_at) "
            f"VALUES (?, {', '.join('?' * len(fields))}, ?, ?)",
            (case_id, *fields.values(), db.now(), db.now()),
        )
    return get_case(case_id)


def process_video(case_id: str, src: Path, transcode: bool = True, start_sec=None, end_sec=None, move=False):
    """把源视频处理为病例目录下的 original / web 文件。可在后台线程中运行。"""
    vdir = media.case_video_dir(case_id)
    vdir.mkdir(parents=True, exist_ok=True)
    try:
        original = vdir / f"{case_id}_original{src.suffix.lower() or '.mp4'}"
        trimmed = start_sec is not None or end_sec is not None
        if trimmed:
            # 裁剪到有效视频起止时间后，“原始文件”即为裁剪后的无损/高质量版本
            original = vdir / f"{case_id}_original.mp4"
            media.transcode_for_web(src, original, start_sec, end_sec)
            if move:
                src.unlink(missing_ok=True)
        elif move:
            shutil.move(str(src), original)
        else:
            shutil.copy2(src, original)

        if transcode and not trimmed:
            web = vdir / f"{case_id}_web.mp4"
            media.transcode_for_web(original, web)
        else:
            web = original
        info = media.probe(web)
        times = media.build_frame_map(web, vdir / "frame_timestamp_map.csv")
        with db.tx() as conn:
            conn.execute(
                "UPDATE cases SET video_file = ?, original_file = ?, video_duration_sec = ?, fps = ?, frame_count = ?, "
                "width = ?, height = ?, media_status = 'ready', media_error = NULL, updated_at = ? WHERE case_id = ?",
                (web.name, original.name, round(info["duration_sec"], 3), round(info["fps"], 3), len(times),
                 info["width"], info["height"], db.now(), case_id),
            )
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        with db.tx() as conn:
            conn.execute(
                "UPDATE cases SET media_status = 'error', media_error = ?, updated_at = ? WHERE case_id = ?",
                (str(e)[:2000], db.now(), case_id),
            )


def process_video_async(*args, **kwargs):
    threading.Thread(target=process_video, args=args, kwargs=kwargs, daemon=True).start()
