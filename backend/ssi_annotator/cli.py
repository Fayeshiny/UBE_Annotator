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

from . import auth, cases, db


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


def cmd_demo_video(a):
    """生成一段合成测试视频（不含任何真实医疗数据），用于开发和培训。"""
    out = Path(a.out)
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
        f"testsrc2=size=1280x720:rate=30:duration={a.seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
    ], check=True)
    print(f"已生成 {out}")


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
