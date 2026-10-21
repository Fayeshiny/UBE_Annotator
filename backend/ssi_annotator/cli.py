"""命令行：初始化、用户管理、批量导入病例、启动服务。

    ssi-annotator init --admin admin
    ssi-annotator add-user A01 "张医生" annotator
    ssi-annotator import-case SSI_0001 /path/to/video.mp4 --surgeon SUR_01 --center C01 --level L4/5 --gold
    ssi-annotator import-csv cases.csv
    ssi-annotator serve --host 0.0.0.0 --port 8000
"""

import argparse
import csv
import getpass
import subprocess
import sys
from pathlib import Path

from . import auth, cases, config, db, frame_cache, segments


def _password(prompt="密码: ") -> str:
    while True:
        p = getpass.getpass(prompt)
        if len(p) >= 8:
            return p
        print("密码至少 8 位")


def cmd_init(a):
    db.init_db()
    if db.connect().execute("SELECT 1 FROM users WHERE username = ?", (a.admin,)).fetchone():
        print(f"用户 {a.admin} 已存在")
        return
    auth.create_user(a.admin, a.admin, "admin", a.password or _password(f"为管理员 {a.admin} 设置密码: "))
    print(f"已创建管理员 {a.admin}")


def cmd_add_user(a):
    db.init_db()
    auth.create_user(a.username, a.display_name, a.role, a.password or _password())
    print(f"已创建 {a.role} {a.username}")


def _import(case_id, path, meta, transcode, start, end):
    src = Path(path).expanduser()
    if not src.is_file():
        print(f"[跳过] {case_id}: 找不到 {src}")
        return
    cases.create_case(case_id, meta)
    print(f"[处理] {case_id} ← {src.name}")
    cases.process_video(case_id, src, transcode, start, end)
    c = cases.get_case(case_id)
    if c["media_status"] == "ready":
        print(f"[完成] {case_id}: {c['video_duration_sec']}s, {c['fps']}fps, {c['frame_count']} 帧")
    else:
        print(f"[失败] {case_id}: {c['media_error']}")


def cmd_import_case(a):
    db.init_db()
    meta = {"surgeon_id": a.surgeon, "center_id": a.center, "operative_level": a.level,
            "cumulative_UBE_cases": a.cumulative, "gold_set": a.gold}
    _import(a.case_id, a.video, meta, not a.no_transcode, a.start, a.end)


def cmd_import_csv(a):
    """CSV 列：case_id,video_path,surgeon_id,center_id,operative_level,cumulative_UBE_cases,gold_set[,start_sec,end_sec]"""
    db.init_db()
    with open(a.csv, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if cases.get_case(r["case_id"]):
                print(f"[跳过] {r['case_id']} 已存在")
                continue
            meta = {k: (r.get(k) or None) for k in ("surgeon_id", "center_id", "operative_level")}
            meta["cumulative_UBE_cases"] = int(r["cumulative_UBE_cases"]) if r.get("cumulative_UBE_cases") else None
            meta["gold_set"] = str(r.get("gold_set", "0")).strip() in ("1", "true", "True", "是")
            start = float(r["start_sec"]) if r.get("start_sec") else None
            end = float(r["end_sec"]) if r.get("end_sec") else None
            _import(r["case_id"], r["video_path"], meta, not a.no_transcode, start, end)


def cmd_serve(a):
    import uvicorn

    db.init_db()
    uvicorn.run("ssi_annotator.app:app", host=a.host, port=a.port, reload=a.reload,
                reload_dirs=[str(Path(__file__).resolve().parent)] if a.reload else None)


def cmd_warm_frame_cache(a):
    import fcntl

    # The disk LRU and decoder budget are process-local. Share the production
    # service lock, and use the authenticated HTTP warm endpoint while live.
    with (config.data_dir() / ".server.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise ValueError("正式服务正在运行：请停止服务后离线预热，或在工作台打开病例进行在线预热") from e
        db.init_db()
        case = cases.get_case(a.case_id)
        if case is None or case["media_status"] != "ready":
            raise ValueError("病例不存在或视频尚未准备完成")
        interval = a.interval_ms if a.interval_ms is not None else db.get_settings()["sampling_interval_ms"]
        result = frame_cache.manager().warm(frame_cache.source(case), interval)
        print(f"{a.case_id}: {result['status']}, {result['prepared']}/{result['total']} 采样帧")


def cmd_demo_video(a):
    """生成一段合成测试视频（不含任何真实医疗数据），用于开发和培训。"""
    out = Path(a.out)
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
        f"testsrc2=size=1280x720:rate=30:duration={a.seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
    ], check=True)
    print(f"已生成 {out}")


def cmd_prepare_segments(a):
    db.init_db()
    if a.case_id:
        c = cases.get_case(a.case_id)
        if c is None or c['media_status'] != 'ready':
            raise ValueError('病例不存在或原视频尚未准备完成')
        print(segments.enqueue_case(c,retry=a.retry))
    else:
        print(f'已检查并排队 {segments.enqueue_existing()} 个病例；Web 服务后台执行切分。')


def main(argv=None):
    p = argparse.ArgumentParser(prog="ssi-annotator", description="UBE SSI 标注平台")
    sub = p.add_subparsers(required=True)

    s = sub.add_parser("init", help="初始化数据库并创建管理员")
    s.add_argument("--admin", default="admin")
    s.add_argument("--password")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("add-user", help="新增用户（username 即导出表中的 annotator_id）")
    s.add_argument("username")
    s.add_argument("display_name")
    s.add_argument("role", choices=["admin", "reviewer", "annotator"])
    s.add_argument("--password")
    s.set_defaults(func=cmd_add_user)

    s = sub.add_parser("import-case", help="导入单个病例视频")
    s.add_argument("case_id")
    s.add_argument("video")
    s.add_argument("--surgeon")
    s.add_argument("--center")
    s.add_argument("--level")
    s.add_argument("--cumulative", type=int)
    s.add_argument("--gold", action="store_true")
    s.add_argument("--start", type=float, help="有效视频起点（秒）")
    s.add_argument("--end", type=float, help="有效视频终点（秒）")
    s.add_argument("--no-transcode", action="store_true")
    s.set_defaults(func=cmd_import_case)

    s = sub.add_parser("import-csv", help="按 CSV 批量导入病例")
    s.add_argument("csv")
    s.add_argument("--no-transcode", action="store_true")
    s.set_defaults(func=cmd_import_csv)

    s = sub.add_parser("serve", help="启动 Web 服务")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("warm-frame-cache", help="离线预生成指定病例的采样帧缓存（正式服务需已停止）")
    s.add_argument("case_id")
    s.add_argument("--interval-ms", type=int, help="采样间隔，至少 1000 毫秒，默认使用项目设置")
    s.set_defaults(func=cmd_warm_frame_cache)

    s = sub.add_parser("prepare-segments", help="为已有视频预生成子视频排队（由运行中的 Web 服务后台处理）")
    s.add_argument("case_id", nargs="?", help="省略则扫描全部已有视频")
    s.add_argument("--retry", action="store_true", help="重试指定病例失败的片段")
    s.set_defaults(func=cmd_prepare_segments)

    s = sub.add_parser("demo-video", help="生成合成测试视频")
    s.add_argument("out")
    s.add_argument("--seconds", type=int, default=120)
    s.set_defaults(func=cmd_demo_video)

    a = p.parse_args(argv)
    try:
        a.func(a)
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
