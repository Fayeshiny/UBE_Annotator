"""按方案第八、九节导出 CSV / COCO JSON，以及病例文件夹结构。"""

import csv
import io
import json
import zipfile
from pathlib import Path

from . import analysis, config, consensus, db, media, rle, rules
from .protocol import ANCHOR_FIELDS, MASK_CLASSES, MILESTONES

CASE_COLUMNS = [
    "case_id", "surgeon_id", "center_id", "operative_level", "cumulative_UBE_cases",
    "video_duration_sec", "fps", "gold_set",
]
INTERVAL_COLUMNS = ["case_id", "{label}", "start_ms", "end_ms", "start_frame", "end_frame", "annotator_id"]
SAMPLE_COLUMNS = ["case_id", "time_ms", "frame_idx", "context", "evaluable", "SCS", *ANCHOR_FIELDS, "annotator_id"]
OL_COLUMNS = [
    "case_id", "event_id", "onset_ms", "offset_ms", "recovery_ms", "SRT_ms", "context", "disturbance",
    "confidence", "reviewer_id",
    # 以下为补充列：帧号、持续时间、标注者、备注
    "onset_frame", "offset_frame", "recovery_frame", "duration_ms", "annotator_id", "note",
]
MILESTONE_COLUMNS = ["case_id", "milestone", "time_ms", "frame_idx", "assessable", "confidence", "annotator_id"]
MASK_COLUMNS = [
    "case_id", "object_id", "class", "start_ms", "end_ms", "source_frame", "annotation_source", "verified",
    "start_frame", "end_frame", "n_mask_frames", "frame_stride", "annotator_id",
]
REVIEW_COLUMNS = ["case_id", "assignment_id", "annotator_id", "user_id", "action", "comment", "created_at"]
CONSENSUS_COLUMNS = ["case_id", "assignment_id", "round", "revision", "reviewer_id", "reviewer_role", "decision", "comment", "created_at"]
METRIC_COLUMNS = [
    "case_id", "annotator_id", "n_evaluable_samples", "scs_stable_ratio", "evaluable_duration_sec", "ol_events",
    "olf_per_10min", "srt_mean_ms", "srt_median_ms", *[f"LPS_{f}" for f in ANCHOR_FIELDS],
]


def _v(x):
    if x is None:
        return ""
    if isinstance(x, float):
        return f"{x:.6g}"
    return x


def to_csv(columns: list[str], rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow([_v(r.get(c)) for c in columns])
    return buf.getvalue()


def assignment_meta(a: dict) -> dict:
    u = db.connect().execute("SELECT username FROM users WHERE id = ?", (a["user_id"],)).fetchone()
    reviewer = db.connect().execute(
        "SELECT u.username FROM review_log r JOIN users u ON u.id = r.user_id "
        "WHERE r.assignment_id = ? AND r.action IN ('approve', 'lock') ORDER BY r.id DESC LIMIT 1",
        (a["id"],),
    ).fetchone()
    return {"annotator_id": u["username"] if u else "", "reviewer_id": reviewer["username"] if reviewer else ""}


def case_tables(case: dict, a: dict, settings: dict, *, applied_rules=None) -> dict[str, list[dict]]:
    settings = (applied_rules or rules.for_assignment(case, a, settings))["settings"]
    ann = json.loads(a["data"]) if isinstance(a["data"], str) else a["data"]
    meta = assignment_meta(a)
    aid = meta["annotator_id"]
    cid = case["case_id"]
    dur = analysis.duration_ms_of(case)
    times = media.frame_times(cid)

    def fr(t):
        return media.time_to_frame(times, t, case.get("fps"))

    out: dict[str, list[dict]] = {}
    for key, label in (("evaluable", "evaluable"), ("context", "context")):
        out[f"{key}_intervals"] = [
            {"case_id": cid, label: iv["label"], "start_ms": iv["start_ms"], "end_ms": iv["end_ms"],
             "start_frame": fr(iv["start_ms"]), "end_frame": fr(iv["end_ms"]), "annotator_id": aid}
            for iv in analysis.intervals(ann[key], dur)
        ]
    out["sampled_ssi"] = [
        {"case_id": cid, **r, "frame_idx": fr(r["time_ms"]), "annotator_id": aid}
        for r in analysis.sample_rows(case, ann, settings)
    ]
    out["orientation_loss"] = [
        {"case_id": cid, "event_id": e["id"], "onset_ms": e.get("onset_ms"), "offset_ms": e.get("offset_ms"),
         "recovery_ms": e.get("recovery_ms"), "SRT_ms": e["SRT_ms"], "context": e["context"],
         "disturbance": e.get("disturbance"), "confidence": e.get("confidence"), "reviewer_id": meta["reviewer_id"],
         "onset_frame": fr(e.get("onset_ms")), "offset_frame": fr(e.get("offset_ms")),
         "recovery_frame": fr(e.get("recovery_ms")), "duration_ms": e["duration_ms"], "annotator_id": aid,
         "note": e.get("note", "")}
        for e in analysis.event_rows(ann)
    ]
    if case["gold_set"]:
        rows = []
        for m in MILESTONES:
            v = ann["milestones"].get(m["code"])
            if v is None:
                continue
            assessable = int(v.get("assessable", 1))
            t = v.get("time_ms") if assessable else None
            rows.append({"case_id": cid, "milestone": m["code"], "time_ms": t, "frame_idx": fr(t),
                         "assessable": assessable, "confidence": v.get("confidence") if assessable else None,
                         "annotator_id": aid})
        out["milestones"] = rows
        objs = mask_objects(a["id"])
        out["mask_index"] = [
            {"case_id": cid, "object_id": o["object_id"], "class": o["class"], "start_ms": o["start_ms"],
             "end_ms": o["end_ms"], "source_frame": o["source_frame"], "annotation_source": o["annotation_source"],
             "verified": o["verified"], "start_frame": fr(o["start_ms"]), "end_frame": fr(o["end_ms"]),
             "n_mask_frames": o["n_frames"], "frame_stride": o["frame_stride"], "annotator_id": aid}
            for o in objs
        ]
    m = analysis.metrics(case, ann, settings)
    out["metrics"] = [{
        "case_id": cid, "annotator_id": aid, **{k: m[k] for k in m if k != "lps"},
        **{f"LPS_{f}": m["lps"][f] for f in ANCHOR_FIELDS},
    }]
    return out


def mask_objects(assignment_id: int) -> list[dict]:
    return [dict(r) for r in db.connect().execute(
        "SELECT o.*, (SELECT COUNT(*) FROM mask_frames f WHERE f.object_pk = o.id) AS n_frames "
        "FROM mask_objects o WHERE assignment_id = ? ORDER BY object_id", (assignment_id,),
    ).fetchall()]


def coco_json(case: dict, assignment_id: int) -> dict:
    cats = [{"id": i + 1, "name": c["code"], "supercategory": "anatomy"} for i, c in enumerate(MASK_CLASSES)]
    cat_id = {c["name"]: c["id"] for c in cats}
    images, anns = {}, []
    rows = db.connect().execute(
        "SELECT f.*, o.object_id, o.class, o.annotation_source, o.verified FROM mask_frames f "
        "JOIN mask_objects o ON o.id = f.object_pk WHERE o.assignment_id = ? ORDER BY f.frame_idx, o.object_id",
        (assignment_id,),
    ).fetchall()
    for i, r in enumerate(rows):
        fi = r["frame_idx"]
        if fi not in images:
            images[fi] = {"id": fi, "file_name": f"{case['case_id']}_{fi:06d}.jpg", "width": case["width"],
                          "height": case["height"], "frame_idx": fi, "time_ms": r["time_ms"]}
        seg = json.loads(r["rle"])
        anns.append({
            "id": i + 1, "image_id": fi, "category_id": cat_id[r["class"]], "segmentation": seg,
            "area": r["area"], "bbox": rle.bbox(rle.decode(seg)), "iscrowd": 1,
            "attributes": {"object_id": r["object_id"], "frame_source": r["source"],
                           "annotation_source": r["annotation_source"], "verified": r["verified"]},
        })
    return {
        "info": {"description": f"UBE SSI key-structure masks · {case['case_id']}", "version": "SSI Protocol v1.0"},
        "categories": cats, "images": list(images.values()), "annotations": anns,
    }


def review_rows(case_id: str) -> list[dict]:
    return [dict(r) for r in db.connect().execute(
        "SELECT r.case_id, r.assignment_id, ua.username AS annotator_id, u.username AS user_id, r.action, r.comment, "
        "datetime(r.created_at, 'unixepoch', 'localtime') AS created_at FROM review_log r "
        "JOIN users u ON u.id = r.user_id LEFT JOIN assignments a ON a.id = r.assignment_id "
        "LEFT JOIN users ua ON ua.id = a.user_id WHERE r.case_id = ? ORDER BY r.id", (case_id,),
    ).fetchall()]


class ExportNotReady(ValueError):
    pass


def require_valid_event_separation(a: dict, settings: dict) -> None:
    """旧版已通过/锁定的数据也必须检查，不能继续作为分离事件交付。"""
    ann = json.loads(a["data"]) if isinstance(a["data"], str) else a["data"]
    issues = analysis.event_merge_issues(ann, settings)
    if issues:
        raise ExportNotReady(f"最终版本含应合并的定向丢失事件：{issues[0]['message']}；请解锁并合并事件、重新复核锁定")


def export_assignments(case: dict, mode: str = "final") -> list[dict]:
    conn = db.connect()
    if mode == "all":
        return [dict(r) for r in conn.execute("SELECT * FROM assignments WHERE case_id = ? ORDER BY id", (case["case_id"],))]
    if mode == "final":
        if case.get("status") != "locked" or not case.get("final_assignment_id"):
            return []
        r = conn.execute(
            "SELECT * FROM assignments WHERE id = ? AND case_id = ? AND status = 'approved'",
            (case["final_assignment_id"], case["case_id"]),
        ).fetchone()
        if r and consensus.final_eligible(case, dict(r)):
            return [dict(r)]
        return []
    if mode != "working":
        raise ValueError("无效的导出模式")
    # 仅供采样模拟选择参考数据，不作为最终版本导出的回退。
    if case.get("final_assignment_id"):
        r = conn.execute(
            "SELECT * FROM assignments WHERE id = ? AND case_id = ?",
            (case["final_assignment_id"], case["case_id"]),
        ).fetchone()
        if r:
            return [dict(r)]
    r = conn.execute(
        "SELECT * FROM assignments WHERE case_id = ? ORDER BY CASE role WHEN 'consensus' THEN 0 WHEN 'primary' THEN 1 "
        "WHEN 'pilot' THEN 2 ELSE 3 END, id LIMIT 1", (case["case_id"],),
    ).fetchone()
    return [dict(r)] if r else []


TABLES = {
    "evaluable_intervals": ("temporal", [c.replace("{label}", "evaluable") for c in INTERVAL_COLUMNS]),
    "context_intervals": ("temporal", [c.replace("{label}", "context") for c in INTERVAL_COLUMNS]),
    "sampled_ssi": ("temporal", SAMPLE_COLUMNS),
    "orientation_loss": ("temporal", OL_COLUMNS),
    "milestones": ("gold", MILESTONE_COLUMNS),
    "mask_index": ("masks", MASK_COLUMNS),
    "metrics": ("qc", METRIC_COLUMNS),
}


def case_files(case: dict, a: dict, settings: dict, *, applied_rules=None) -> dict[str, str | bytes]:
    """病例数据包：相对路径 → 内容（不含视频）。"""
    applied_rules = applied_rules or rules.for_assignment(case, a, settings)
    settings = applied_rules["settings"]
    files: dict[str, str | bytes] = {"qc/rules.json": json.dumps(applied_rules, ensure_ascii=False, indent=2)}
    for name, rows in case_tables(case, a, settings, applied_rules=applied_rules).items():
        folder, cols = TABLES[name]
        files[f"{folder}/{name}.csv"] = to_csv(cols, rows)
    if case["gold_set"]:
        files["masks/annotations_coco.json"] = json.dumps(coco_json(case, a["id"]), ensure_ascii=False)
    files["qc/review_log.csv"] = to_csv(REVIEW_COLUMNS, review_rows(case["case_id"]))
    files["qc/consensus_reviews.csv"] = to_csv(CONSENSUS_COLUMNS,
        [r for r in consensus.export_rows(case["case_id"]) if r["assignment_id"] == a["id"]])
    files["qc/issues.json"] = json.dumps(
        analysis.validate(case, json.loads(a["data"]), settings, mask_objects(a["id"]) if case["gold_set"] else None),
        ensure_ascii=False, indent=1,
    )
    return files


def write_case_folder(case: dict, settings: dict) -> Path | None:
    """写入 data/cases/<case_id>/{temporal,gold,masks,qc}/，与方案第九节目录结构一致。"""
    # 重新读取病例并保持同一数据库快照，防止全局设置或锁定状态并发变化。
    with db.read_snapshot() as conn:
        case = dict(conn.execute("SELECT * FROM cases WHERE case_id = ?", (case["case_id"],)).fetchone())
        assigns = export_assignments(case, "final")
        if not assigns:
            return None
        try:
            applied_rules = rules.locked(case, assigns[0])
            require_valid_event_separation(assigns[0], applied_rules["settings"])
            files = case_files(case, assigns[0], settings, applied_rules=applied_rules)
        except rules.MissingRuleSnapshot:
            return None  # 不覆盖旧病例磁盘上已有的历史文件。
    root = config.cases_dir() / case["case_id"]
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
    return root


def case_zip(case: dict, a: dict, settings: dict, include_video: bool = False, mode: str = "final") -> bytes:
    if mode == "final":
        eligible = export_assignments(case, "final")
        if not eligible or eligible[0]["id"] != a["id"]:
            raise ExportNotReady("所选任务不是已复核通过且锁定的最终版本")
        applied_rules = rules.locked(case, a)
        require_valid_event_separation(a, applied_rules["settings"])
    else:
        try:
            applied_rules = rules.for_assignment(case, a, settings)
        except rules.MissingRuleSnapshot as e:
            applied_rules = {**rules.current(settings), "warning": str(e)}
    buf = io.BytesIO()
    cid = case["case_id"]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{cid}/export_manifest.json", json.dumps({
            "export_mode": mode,
            "description": "已锁定最终版本" if mode == "final" else "工作版本，不能视为已锁定最终数据",
            "case_id": cid, "case_status": case["status"],
            "assignment_id": a["id"], "assignment_status": a["status"], "assignment_version": a["version"],
            "consensus": consensus.summary(a),
            "applied_rules": applied_rules,
        }, ensure_ascii=False, indent=2))
        for rel, content in case_files(case, a, settings, applied_rules=applied_rules).items():
            z.writestr(f"{cid}/{rel}", content)
        vdir = media.case_video_dir(cid)
        fm = vdir / "frame_timestamp_map.csv"
        if fm.exists():
            z.write(fm, f"{cid}/video/frame_timestamp_map.csv")
        if include_video and case.get("original_file"):
            z.write(vdir / case["original_file"], f"{cid}/video/{case['original_file']}")
    return buf.getvalue()


def project_zip(settings: dict, mode: str = "final") -> bytes:
    """项目级主表：cases.csv + 各核心表纵向汇总。mode=all 时包含每位标注者的独立标注（用于一致性分析）。"""
    conn = db.connect()
    cases = [dict(r) for r in conn.execute("SELECT * FROM cases WHERE deleted_at IS NULL ORDER BY case_id")]
    agg: dict[str, list[dict]] = {k: [] for k in TABLES}
    review: list[dict] = []
    consensus_rows: list[dict] = []
    included_cases, included_assignments, excluded_cases = [], [], []
    for c in cases:
        assigns = export_assignments(c, mode)
        if not assigns:
            excluded_cases.append({"case_id": c["case_id"], "reason": "无已复核通过且锁定的最终版本" if mode == "final" else "无标注任务"})
            continue
        locked_rules = None
        if mode == "final":
            try:
                locked_rules = rules.locked(c, assigns[0])
                require_valid_event_separation(assigns[0], locked_rules["settings"])
            except (rules.MissingRuleSnapshot, ExportNotReady) as e:
                excluded_cases.append({"case_id": c["case_id"], "reason": str(e)})
                continue
        included_cases.append(c)
        for a in assigns:
            if locked_rules is not None:
                applied_rules = locked_rules
            else:
                try:
                    applied_rules = rules.for_assignment(c, a, settings)
                except rules.MissingRuleSnapshot as e:
                    applied_rules = {**rules.current(settings), "warning": str(e)}
            included_assignments.append({"case_id": c["case_id"], "assignment_id": a["id"],
                                         "status": a["status"], "version": a["version"], "consensus": consensus.summary(a),
                                         "applied_rules": applied_rules})
            consensus_rows += [r for r in consensus.export_rows(c["case_id"]) if r["assignment_id"] == a["id"]]
            for name, rows in case_tables(c, a, settings, applied_rules=applied_rules).items():
                agg[name] += rows
        review += review_rows(c["case_id"])
    if mode == "final" and not included_cases:
        reason = f"；{excluded_cases[0]['case_id']}：{excluded_cases[0]['reason']}" if excluded_cases else ""
        raise ExportNotReady("暂无通过最终导出检查的已复核锁定版本；请检查规则快照及事件合并错误，工作数据可使用全部标注者导出" + reason)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("export_manifest.json", json.dumps({
            "export_mode": mode,
            "description": "已锁定最终版本" if mode == "final" else "全部标注者工作版本，包含未复核数据",
            "included_case_count": len(included_cases), "excluded_case_count": len(excluded_cases),
            "assignments": included_assignments, "excluded_cases": excluded_cases,
        }, ensure_ascii=False, indent=2))
        z.writestr("cases.csv", to_csv(CASE_COLUMNS, included_cases))
        for name, rows in agg.items():
            z.writestr(f"{name}.csv", to_csv(TABLES[name][1], rows))
        z.writestr("review_log.csv", to_csv(REVIEW_COLUMNS, review))
        z.writestr("consensus_reviews.csv", to_csv(CONSENSUS_COLUMNS, consensus_rows))
        z.writestr("README.txt", (
            "UBE SSI Protocol v1.0 项目级主表\n"
            f"导出模式：{'全部标注者工作版本（包含未复核数据）' if mode == 'all' else '仅已复核通过且锁定的最终版本'}\n"
            f"纳入 {len(included_cases)} 例，排除 {len(excluded_cases)} 例；详情见 export_manifest.json。\n"
            "各任务实际使用的规则、采样间隔及快照编号见 export_manifest.json 中的 applied_rules；最终版本逐病例使用锁定时的快照，可能包含不同规则版本。\n"
            "时间以毫秒时间戳为主索引，帧号来自 video/frame_timestamp_map.csv。\n"
        ))
    return buf.getvalue()
