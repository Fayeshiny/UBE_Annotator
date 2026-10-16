import itertools
from contextlib import asynccontextmanager
import json
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import analysis, auth, cases, config, consensus, db, export, media, rle, rules
from .auth import current_user, require_role
from .protocol import ANCHOR_FIELDS, CONTEXTS, MASK_CLASSES, empty_annotation, protocol_dict
from .sam import get_backend
from .sam import service as sam

@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    yield


app = FastAPI(title="UBE SSI 标注平台", version="0.1.0", lifespan=lifespan)
REVIEWERS = ("admin", "reviewer")


# ------------------------------------------------------------------ 认证


class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def login(body: LoginIn, response: Response):
    res = auth.login(body.username.strip(), body.password)
    if res is None:
        raise HTTPException(401, "用户名或密码错误")
    token, user = res
    response.set_cookie(
        auth.SESSION_COOKIE, token, httponly=True, samesite="lax", secure=config.cookie_secure(),
        max_age=auth.SESSION_TTL_SEC,
    )
    return user


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        auth.logout(token)
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return user


# ------------------------------------------------------------------ 协议与设置


@app.get("/api/protocol")
def get_protocol(user=Depends(current_user)):
    return {**protocol_dict(), "settings": db.get_settings(), "sam_backend": config.sam_backend()}


class SettingsIn(BaseModel):
    values: dict[str, Any]


FROZEN_KEYS = {"sampling_interval_ms", "ol_min_duration_ms", "recovery_min_stable_ms", "anchor_applicability"}


@app.put("/api/settings")
def put_settings(body: SettingsIn, user=Depends(require_role("admin"))):
    s = db.get_settings()
    vals = {k: v for k, v in body.values.items() if k in s}
    if s["rules_frozen"] and vals.get("rules_frozen", True) and FROZEN_KEYS & {k for k in vals if vals[k] != s[k]}:
        raise HTTPException(409, "规则已冻结，不能再修改采样间隔或事件规则")
    if "sampling_interval_ms" in vals and int(vals["sampling_interval_ms"]) < 1000:
        raise HTTPException(400, "采样间隔不能小于 1 秒")
    if "anchor_applicability" in vals:
        applic = vals["anchor_applicability"]
        contexts = {c["code"] for c in CONTEXTS}
        if not isinstance(applic, dict) or set(applic) != contexts or any(
            not isinstance(fields, list) or any(f not in ANCHOR_FIELDS for f in fields)
            or len(fields) != len(set(fields)) for fields in applic.values()
        ):
            raise HTTPException(400, "锚点适用关系必须覆盖 C1–C6/CU，且每项为不重复的合法锚点列表")
    db.set_settings(vals)
    return db.get_settings()


# ------------------------------------------------------------------ 用户


class UserIn(BaseModel):
    username: str
    display_name: str
    role: str
    password: str


class UserPatch(BaseModel):
    display_name: str | None = None
    role: str | None = None
    password: str | None = None
    active: bool | None = None


@app.get("/api/users")
def list_users(user=Depends(current_user)):
    rows = db.connect().execute("SELECT id, username, display_name, role, active FROM users WHERE deleted_at IS NULL ORDER BY username").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/users")
def create_user(body: UserIn, user=Depends(require_role("admin"))):
    if body.role not in ("admin", "reviewer", "annotator"):
        raise HTTPException(400, "角色无效")
    if len(body.password) < 8:
        raise HTTPException(400, "密码至少 8 位")
    if db.connect().execute("SELECT 1 FROM users WHERE username = ? AND deleted_at IS NOT NULL", (body.username.strip(),)).fetchone():
        raise HTTPException(409, "该用户名已删除但保留了历史记录，请使用其他用户名")
    try:
        uid = auth.create_user(body.username.strip(), body.display_name.strip(), body.role, body.password)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"创建失败：{e}") from e
    return {"id": uid}


def _available_user(conn, uid):
    target = conn.execute("SELECT * FROM users WHERE id = ? AND deleted_at IS NULL", (uid,)).fetchone()
    if target is None:
        raise HTTPException(404, "用户不存在或已删除")
    return target


def _protect_last_admin(conn, target, new_role, new_active):
    if target["role"] == "admin" and target["active"] and (new_role != "admin" or not new_active):
        count = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1 AND deleted_at IS NULL").fetchone()[0]
        if count <= 1:
            raise HTTPException(409, "不能删除、停用或降级最后一个启用的管理员")


@app.patch("/api/users/{uid}")
def patch_user(uid: int, body: UserPatch, user=Depends(require_role("admin"))):
    sets, args = [], []
    if body.display_name is not None:
        sets.append("display_name = ?"); args.append(body.display_name)  # noqa: E702
    if body.role is not None:
        if body.role not in ("admin", "reviewer", "annotator"):
            raise HTTPException(400, "角色无效")
        sets.append("role = ?"); args.append(body.role)  # noqa: E702
    if body.password:
        if len(body.password) < 8:
            raise HTTPException(400, "密码至少 8 位")
        sets.append("password_hash = ?"); args.append(auth.hash_password(body.password))  # noqa: E702
    if body.active is not None:
        sets.append("active = ?"); args.append(1 if body.active else 0)  # noqa: E702
    with db.tx() as conn:
        target = _available_user(conn, uid)
        _protect_last_admin(conn, target, body.role if body.role is not None else target["role"],
                            body.active if body.active is not None else target["active"])
        if sets:
            conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", (*args, uid))
            if body.active is False or body.password:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
    return {"ok": True}


@app.delete("/api/users/{uid}")
def delete_user(uid: int, user=Depends(require_role("admin"))):
    if uid == user["id"]:
        raise HTTPException(409, "不能删除当前登录账号")
    with db.tx() as conn:
        target = _available_user(conn, uid)
        _protect_last_admin(conn, target, target["role"], False)
        # 既含外键，也含历史表中未声明外键的操作人字段。
        references = (("assignments", "user_id"), ("assignment_history", "user_id"),
                      ("review_log", "user_id"), ("mask_objects", "verified_by"),
                      ("mask_frames", "updated_by"), ("mask_history", "user_id"),
                      ("consensus_reviews", "reviewer_id"), ("user_deletion_log", "actor_id"),
                      ("case_rule_snapshots", "created_by"))
        linked = any(conn.execute(f"SELECT 1 FROM {table} WHERE {field} = ? LIMIT 1", (uid,)).fetchone()
                     for table, field in references)
        mode = "archived" if linked else "deleted"
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
        if linked:
            conn.execute("UPDATE users SET active = 0, deleted_at = ?, password_hash = '' WHERE id = ?", (db.now(), uid))
        else:
            conn.execute("DELETE FROM users WHERE id = ?", (uid,))
        conn.execute(
            "INSERT INTO user_deletion_log (actor_id, target_user_id, username, display_name, deletion_mode, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user["id"], uid, target["username"], target["display_name"], mode, db.now()),
        )
    return {"ok": True, "mode": mode}


# ------------------------------------------------------------------ 病例


def _case_or_404(case_id: str) -> dict:
    c = cases.get_case(case_id)
    if c is None:
        raise HTTPException(404, "病例不存在")
    return c


def _assignment_summary(case_id: str) -> list[dict]:
    rows = db.connect().execute(
        "SELECT a.id, a.user_id, a.role, a.status, a.version, a.updated_at, u.username, u.display_name "
        "FROM assignments a JOIN users u ON u.id = a.user_id WHERE a.case_id = ? ORDER BY a.id", (case_id,),
    ).fetchall()
    out = [dict(r) for r in rows]
    for a in out:
        a["consensus"] = consensus.summary(a)
    return out


@app.get("/api/cases")
def list_cases(user=Depends(current_user)):
    conn = db.connect()
    if user["role"] in REVIEWERS:
        rows = conn.execute("SELECT * FROM cases ORDER BY case_id").fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT c.* FROM cases c JOIN assignments a ON a.case_id = c.case_id WHERE a.user_id = ? "
            "ORDER BY c.case_id", (user["id"],),
        ).fetchall()
    out = []
    for r in rows:
        c = dict(r)
        c["assignments"] = _assignment_summary(c["case_id"])
        out.append(c)
    return out


class CaseImportIn(BaseModel):
    case_id: str
    source_path: str
    surgeon_id: str | None = None
    center_id: str | None = None
    operative_level: str | None = None
    cumulative_UBE_cases: int | None = None
    gold_set: bool = False
    notes: str | None = None
    start_sec: float | None = None
    end_sec: float | None = None
    transcode: bool = True


@app.post("/api/cases/import")
def import_case(body: CaseImportIn, user=Depends(require_role("admin"))):
    """从服务器本地路径导入（推荐大文件使用）。"""
    src = Path(body.source_path).expanduser()
    if not src.is_file():
        raise HTTPException(400, f"找不到文件：{src}")
    try:
        cases.create_case(body.case_id, body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    cases.process_video_async(body.case_id, src, body.transcode, body.start_sec, body.end_sec)
    return cases.get_case(body.case_id)


@app.post("/api/cases/upload")
async def upload_case(
    case_id: str = Form(...),
    file: UploadFile = File(...),
    surgeon_id: str | None = Form(None),
    center_id: str | None = Form(None),
    operative_level: str | None = Form(None),
    cumulative_UBE_cases: int | None = Form(None),
    gold_set: bool = Form(False),
    start_sec: float | None = Form(None),
    end_sec: float | None = Form(None),
    user=Depends(require_role("admin")),
):
    try:
        cases.create_case(case_id, {
            "surgeon_id": surgeon_id, "center_id": center_id, "operative_level": operative_level,
            "cumulative_UBE_cases": cumulative_UBE_cases, "gold_set": gold_set,
        })
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    tmp = config.cache_dir() / f"upload_{case_id}{Path(file.filename or 'v.mp4').suffix}"
    with open(tmp, "wb") as f:
        while chunk := await file.read(8 * 1024 * 1024):
            f.write(chunk)
    cases.process_video_async(case_id, tmp, True, start_sec, end_sec, move=True)
    return cases.get_case(case_id)


class CasePatch(BaseModel):
    surgeon_id: str | None = None
    center_id: str | None = None
    operative_level: str | None = None
    cumulative_UBE_cases: int | None = None
    gold_set: bool | None = None
    notes: str | None = None


@app.patch("/api/cases/{case_id}")
def patch_case(case_id: str, body: CasePatch, user=Depends(require_role(*REVIEWERS))):
    c = _case_or_404(case_id)
    if c["status"] == "locked":
        raise HTTPException(409, "病例已锁定")
    vals = body.model_dump(exclude_unset=True)
    if "gold_set" in vals:
        vals["gold_set"] = 1 if vals["gold_set"] else 0
    if vals:
        with db.tx() as conn:
            conn.execute(
                f"UPDATE cases SET {', '.join(f'{k} = ?' for k in vals)}, updated_at = ? WHERE case_id = ?",
                (*vals.values(), db.now(), case_id),
            )
    return cases.get_case(case_id)


def _can_view_case(user, case_id):
    if user["role"] in REVIEWERS:
        return True
    r = db.connect().execute(
        "SELECT 1 FROM assignments WHERE case_id = ? AND user_id = ?", (case_id, user["id"])
    ).fetchone()
    return r is not None


@app.get("/api/cases/{case_id}")
def get_case(case_id: str, user=Depends(current_user)):
    c = _case_or_404(case_id)
    if not _can_view_case(user, case_id):
        raise HTTPException(403, "无权限")
    c["assignments"] = _assignment_summary(case_id)
    return c


@app.get("/api/cases/{case_id}/video")
def case_video(case_id: str, user=Depends(current_user)):
    c = _case_or_404(case_id)
    if not _can_view_case(user, case_id):
        raise HTTPException(403, "无权限")
    if c["media_status"] != "ready":
        raise HTTPException(409, "视频仍在处理")
    return FileResponse(media.case_video_dir(case_id) / c["video_file"], media_type="video/mp4")


@app.get("/api/cases/{case_id}/frames")
def case_frames(case_id: str, user=Depends(current_user)):
    if not _can_view_case(user, case_id):
        raise HTTPException(403, "无权限")
    return list(media.frame_times(case_id))


class AssignIn(BaseModel):
    user_id: int
    role: str = "primary"


@app.post("/api/cases/{case_id}/assignments")
def create_assignment(case_id: str, body: AssignIn, user=Depends(require_role(*REVIEWERS))):
    c = _case_or_404(case_id)
    if c["status"] == "locked":
        raise HTTPException(409, "病例已锁定")
    if body.role not in ("primary", "secondary", "pilot", "consensus"):
        raise HTTPException(400, "角色无效")
    try:
        with db.tx() as conn:
            target = _available_user(conn, body.user_id)
            if not target["active"]:
                raise HTTPException(409, "不能向停用账号分配任务")
            cur = conn.execute(
                "INSERT INTO assignments (case_id, user_id, role, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (case_id, body.user_id, body.role, json.dumps(empty_annotation()), db.now(), db.now()),
            )
            _log(conn, case_id, cur.lastrowid, user, "assign", f"role={body.role}")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"分配失败（同一人同一角色只能分配一次）：{e}") from e
    return {"id": cur.lastrowid}


@app.delete("/api/assignments/{aid}")
def delete_assignment(aid: int, user=Depends(require_role("admin"))):
    a = _assignment_or_404(aid)
    if cases.get_case(a["case_id"])["status"] == "locked":
        raise HTTPException(409, "病例已锁定")
    if a["version"] > 1:
        raise HTTPException(409, "该任务已有标注数据，不能删除")
    with db.tx() as conn:
        conn.execute("DELETE FROM mask_frames WHERE object_pk IN (SELECT id FROM mask_objects WHERE assignment_id = ?)", (aid,))
        conn.execute("DELETE FROM mask_objects WHERE assignment_id = ?", (aid,))
        conn.execute("DELETE FROM assignments WHERE id = ?", (aid,))
        _log(conn, a["case_id"], aid, user, "unassign", None)
    return {"ok": True}


def _log(conn, case_id, aid, user, action, comment):
    conn.execute(
        "INSERT INTO review_log (case_id, assignment_id, user_id, action, comment, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (case_id, aid, user["id"], action, comment, db.now()),
    )


class LockIn(BaseModel):
    final_assignment_id: int
    comment: str | None = None


@app.post("/api/cases/{case_id}/lock")
def lock_case(case_id: str, body: LockIn, user=Depends(require_role(*REVIEWERS))):
    with db.tx() as conn:
        c = _case_or_404(case_id)
        a = _assignment_or_404(body.final_assignment_id)
        if c["status"] == "locked":
            raise HTTPException(409, "病例已锁定")
        if a["case_id"] != case_id:
            raise HTTPException(400, "最终版本不属于该病例")
        if not consensus.final_eligible(c, a, conn):
            raise HTTPException(409, "金标准病例或共识版本必须完成三专家独立共识，创建者不计入确认人数")
        if a["status"] != "approved":
            raise HTTPException(409, "最终版本需先经复核通过")
        settings = db.get_settings()
        issues = analysis.validate(c, json.loads(a["data"]), settings,
                                   export.mask_objects(a["id"]) if c["gold_set"] else None)
        errors = [i for i in issues if i["level"] == "error"]
        if errors:
            raise HTTPException(409, f"最终版本仍有 {len(errors)} 个错误，不能锁定：{errors[0]['message']}")
        snapshot_id = rules.capture(conn, c, a, settings, user["id"])
        conn.execute(
            "UPDATE cases SET status = 'locked', final_assignment_id = ?, locked_rule_snapshot_id = ?, updated_at = ? WHERE case_id = ?",
            (a["id"], snapshot_id, db.now(), case_id),
        )
        _log(conn, case_id, a["id"], user, "lock", body.comment)
    export.write_case_folder(cases.get_case(case_id), db.get_settings())
    return cases.get_case(case_id)


@app.post("/api/cases/{case_id}/unlock")
def unlock_case(case_id: str, body: dict, user=Depends(require_role("admin"))):
    with db.tx() as conn:
        c = _case_or_404(case_id)
        if c["status"] != "locked":
            raise HTTPException(409, "病例尚未锁定")
        # 旧快照保留，但采用新规则必须重新提交、复核；共识签署也需重新取得。
        conn.execute("UPDATE assignments SET status = 'in_progress', updated_at = ? WHERE case_id = ? AND status IN ('approved', 'submitted')",
                     (db.now(), case_id))
        conn.execute("UPDATE consensus_state SET revision = revision + 1, review_revision = NULL WHERE assignment_id IN "
                     "(SELECT id FROM assignments WHERE case_id = ?)", (case_id,))
        conn.execute("UPDATE cases SET status = 'open', locked_rule_snapshot_id = NULL, updated_at = ? WHERE case_id = ?", (db.now(), case_id))
        _log(conn, case_id, None, user, "unlock", body.get("comment"))
    return cases.get_case(case_id)


@app.get("/api/cases/{case_id}/agreement")
def case_agreement(case_id: str, user=Depends(require_role(*REVIEWERS))):
    c = _case_or_404(case_id)
    settings = db.get_settings()
    rows = db.connect().execute(
        "SELECT a.*, u.username FROM assignments a JOIN users u ON u.id = a.user_id "
        "WHERE a.case_id = ? AND a.role != 'consensus' ORDER BY a.id", (case_id,),
    ).fetchall()
    out = []
    for x, y in itertools.combinations(rows, 2):
        seq = analysis.pair_sequences(c, json.loads(x["data"]), json.loads(y["data"]), settings)
        out.append({"a": x["username"], "b": y["username"], "summary": analysis.summarize([seq]),
                    "event_matches": seq["event_match"]})
    return out


@app.get("/api/cases/{case_id}/export.zip")
def case_export(case_id: str, assignment_id: int | None = None, include_video: bool = False,
                mode: str = "final", user=Depends(require_role(*REVIEWERS))):
    with db.read_snapshot():
        c = _case_or_404(case_id)
        if mode not in ("final", "working"):
            raise HTTPException(400, "mode 只能是 final 或 working")
        a = _assignment_or_404(assignment_id) if assignment_id is not None else None
        if a is not None and a["case_id"] != case_id:
            raise HTTPException(400, "标注任务不属于该病例")
        if mode == "final":
            lst = export.export_assignments(c, "final")
            if not lst:
                raise HTTPException(409, "该病例尚无已复核通过且锁定的最终版本，请先复核并锁定，或选择工作版本导出")
            if a is not None and a["id"] != lst[0]["id"]:
                raise HTTPException(409, "所选任务不是已锁定最终版本；请使用工作版本导出")
            a = lst[0]
        elif a is None:
            raise HTTPException(400, "导出工作版本时必须指定标注任务 assignment_id")
        try:
            data = export.case_zip(c, a, db.get_settings(), include_video, mode=mode)
        except (rules.MissingRuleSnapshot, export.ExportNotReady) as e:
            raise HTTPException(409, str(e)) from e
    suffix = "final" if mode == "final" else f"working_{a['id']}"
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{case_id}_{suffix}.zip"'})


# ------------------------------------------------------------------ 标注任务


def _assignment_or_404(aid: int) -> dict:
    r = db.connect().execute("SELECT * FROM assignments WHERE id = ?", (aid,)).fetchone()
    if r is None:
        raise HTTPException(404, "任务不存在")
    return dict(r)


def _can_edit(user, a, case) -> bool:
    if case["status"] == "locked":
        return False
    if a["user_id"] != user["id"]:
        return False
    if a["role"] == "consensus":
        return user["role"] in REVIEWERS and a["status"] in ("in_progress", "returned")
    return a["status"] in ("in_progress", "returned")


def _check_view(user, a):
    if user["role"] not in REVIEWERS and a["user_id"] != user["id"]:
        raise HTTPException(403, "无权限查看他人的标注")


def _assignment_payload(a: dict, user) -> dict:
    c = cases.get_case(a["case_id"])
    data = json.loads(a["data"])
    settings = db.get_settings()
    rules_warning = None
    try:
        applied_rules = rules.for_assignment(c, a, settings)
    except rules.MissingRuleSnapshot as e:
        # 旧病例仍可查看原始标注，但当前规则计算的预览不能冒充历史最终结果。
        applied_rules = rules.current(settings)
        rules_warning = f"{e}。当前页面仅按现行规则预览。"
    settings = applied_rules["settings"]
    objs = export.mask_objects(a["id"]) if c["gold_set"] else None
    owner = db.connect().execute("SELECT username, display_name FROM users WHERE id = ?", (a["user_id"],)).fetchone()
    return {
        "assignment": {k: v for k, v in a.items() if k != "data"} | {"owner": dict(owner)},
        "case": c,
        "data": data,
        "settings": settings,
        "applied_rules": applied_rules,
        "rules_warning": rules_warning,
        "editable": _can_edit(user, a, c),
        "issues": analysis.validate(c, data, settings, objs),
        "metrics": analysis.metrics(c, data, settings),
        "consensus": consensus.summary(a),
    }


@app.get("/api/assignments/{aid}")
def get_assignment(aid: int, user=Depends(current_user)):
    a = _assignment_or_404(aid)
    _check_view(user, a)
    return _assignment_payload(a, user)


class DataIn(BaseModel):
    data: dict
    version: int


def _clean_data(d: dict) -> dict:
    base = empty_annotation()
    out = {k: d.get(k, base[k]) for k in base}
    out["evaluable"] = analysis.normalize_parts(out["evaluable"])
    out["context"] = analysis.normalize_parts(out["context"])
    out["samples"] = {str(int(k)): v for k, v in out["samples"].items() if any(v.get(f) for f in ("scs", "note", *ANCHOR_FIELDS))}
    out["events"] = sorted(out["events"], key=lambda e: (e.get("onset_ms") is None, e.get("onset_ms") or 0))
    return out


@app.put("/api/assignments/{aid}/data")
def save_data(aid: int, body: DataIn, user=Depends(current_user)):
    a = _assignment_or_404(aid)
    c = cases.get_case(a["case_id"])
    if not _can_edit(user, a, c):
        raise HTTPException(403, "当前状态不可编辑（已提交、已锁定或不是你的任务）")
    if body.version != a["version"]:
        raise HTTPException(409, "标注已在别处更新，请刷新后再保存")
    data = _clean_data(body.data)
    settings = db.get_settings()
    anchor_errors = analysis.anchor_issues(data, settings)
    if anchor_errors:
        raise HTTPException(400, anchor_errors[0]["message"])
    analysis.fill_inapplicable_anchors(data, settings)
    with db.tx() as conn:
        fresh = _assignment_or_404(aid)
        if not _can_edit(user, fresh, _case_or_404(a["case_id"])) or fresh["version"] != body.version:
            raise HTTPException(409, "任务状态或版本已变化，请刷新后再保存")
        conn.execute(
            "UPDATE assignments SET data = ?, version = version + 1, updated_at = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), db.now(), aid),
        )
        conn.execute(
            "INSERT INTO assignment_history (assignment_id, version, data, user_id, saved_at) VALUES (?, ?, ?, ?, ?)",
            (aid, a["version"] + 1, json.dumps(data, ensure_ascii=False), user["id"], db.now()),
        )
    return _assignment_payload(_assignment_or_404(aid), user)


class ActionIn(BaseModel):
    action: str
    comment: str | None = None
    consensus_round: int | None = None
    consensus_revision: int | None = None


def _check_consensus_content(a, c):
    if any(s.status in ("loading", "propagating") for s in sam.list_sessions(a["id"])):
        raise HTTPException(409, "分割片段仍在建立或传播，请等待完成后再提交或确认共识")
    errors = [i for i in analysis.validate(c, json.loads(a["data"]), db.get_settings(),
              export.mask_objects(a["id"]) if c["gold_set"] else None) if i["level"] == "error"]
    if errors:
        raise HTTPException(409, f"仍有 {len(errors)} 个必填项/错误：{errors[0]['message']}")


@app.post("/api/assignments/{aid}/action")
def assignment_action(aid: int, body: ActionIn, user=Depends(current_user)):
    with db.tx() as conn:
        a = _assignment_or_404(aid)
        c = cases.get_case(a["case_id"])
        if c["status"] == "locked":
            raise HTTPException(409, "病例已锁定")
        act = body.action
        is_consensus = a["role"] == "consensus"
        if act == "submit":
            if a["user_id"] != user["id"]:
                raise HTTPException(403, "只能提交自己的任务")
            _check_consensus_content(a, c)
            if is_consensus:
                if user["role"] not in REVIEWERS:
                    raise HTTPException(403, "需要专家权限")
                try:
                    consensus.start_round(conn, a)
                except ValueError as e:
                    raise HTTPException(409, str(e)) from e
            new = "submitted"
        elif act in ("approve", "return", "withdraw"):
            if user["role"] not in REVIEWERS:
                raise HTTPException(403, "需要复核权限")
            if act == "return" and not (body.comment or "").strip():
                raise HTTPException(400, "退回时请填写原因")
            if is_consensus:
                if a["user_id"] == user["id"]:
                    raise HTTPException(403, "共识创建者不能参与独立确认，需要另外三位管理员或专家")
                if body.consensus_round is None or body.consensus_revision is None:
                    raise HTTPException(400, "请提供共识轮次和内容版本，刷新页面后再操作")
                if act == "approve":
                    _check_consensus_content(a, c)
                try:
                    new = consensus.record_review(conn, a, user,
                        {"approve": "approve", "return": "reject", "withdraw": "withdraw"}[act],
                        (body.comment or ("确认一致" if act == "approve" else "撤回确认")).strip(),
                        body.consensus_round, body.consensus_revision)
                except PermissionError as e:
                    raise HTTPException(403, str(e)) from e
                except ValueError as e:
                    raise HTTPException(409, str(e)) from e
            else:
                if act == "withdraw":
                    raise HTTPException(400, "仅共识版本支持撤回确认")
                if a["user_id"] == user["id"]:
                    raise HTTPException(403, "不能复核自己的标注")
                if act == "approve":
                    merge_errors = analysis.event_merge_issues(json.loads(a["data"]), db.get_settings())
                    if merge_errors:
                        raise HTTPException(409, merge_errors[0]["message"])
                new = "approved" if act == "approve" else "returned"
        elif act == "reopen":
            if is_consensus:
                if a["user_id"] != user["id"] or user["role"] not in REVIEWERS:
                    raise HTTPException(403, "共识版本只能由创建者重新打开")
                conn.execute("UPDATE consensus_state SET revision = revision + 1, review_revision = NULL WHERE assignment_id = ?", (aid,))
            elif a["user_id"] != user["id"] and user["role"] not in REVIEWERS:
                raise HTTPException(403, "无权限")
            new = "in_progress"
        else:
            raise HTTPException(400, "未知操作")
        conn.execute("UPDATE assignments SET status = ?, updated_at = ? WHERE id = ?", (new, db.now(), aid))
        action = f"consensus_{act}" if is_consensus else act
        _log(conn, a["case_id"], aid, user, action, body.comment)
    return _assignment_payload(_assignment_or_404(aid), user)


class ConsensusIn(BaseModel):
    from_assignment_id: int


@app.post("/api/cases/{case_id}/consensus")
def create_consensus(case_id: str, body: ConsensusIn, user=Depends(require_role(*REVIEWERS))):
    """以某位标注者的版本为起点建立裁决/共识版本（归属当前复核者）。"""
    c = _case_or_404(case_id)
    if c["status"] == "locked":
        raise HTTPException(409, "病例已锁定")
    src = _assignment_or_404(body.from_assignment_id)
    if src["case_id"] != case_id:
        raise HTTPException(400, "来源任务不属于该病例")
    conn = db.connect()
    if conn.execute("SELECT 1 FROM assignments WHERE case_id = ? AND user_id = ? AND role = 'consensus'",
                    (case_id, user["id"])).fetchone():
        raise HTTPException(409, "你在该病例已有共识版本")
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO assignments (case_id, user_id, role, data, created_at, updated_at) VALUES (?, ?, 'consensus', ?, ?, ?)",
            (case_id, user["id"], src["data"], db.now(), db.now()),
        )
        new_id = cur.lastrowid
        for o in conn.execute("SELECT * FROM mask_objects WHERE assignment_id = ?", (src["id"],)).fetchall():
            o = dict(o)
            old_pk = o.pop("id")
            o["assignment_id"] = new_id
            cols = ", ".join(o)
            pk = conn.execute(f"INSERT INTO mask_objects ({cols}) VALUES ({', '.join('?' * len(o))})", tuple(o.values())).lastrowid
            conn.execute(
                "INSERT INTO mask_frames (object_pk, frame_idx, time_ms, rle, area, source, updated_by, updated_at) "
                "SELECT ?, frame_idx, time_ms, rle, area, source, updated_by, updated_at FROM mask_frames WHERE object_pk = ?",
                (pk, old_pk),
            )
        _log(conn, case_id, new_id, user, "consensus_from", f"assignment {src['id']}")
    return {"id": new_id}


@app.get("/api/assignments/{aid}/consensus-reviews")
def consensus_reviews(aid: int, user=Depends(current_user)):
    a = _assignment_or_404(aid)
    _check_view(user, a)
    if a["role"] != "consensus":
        raise HTTPException(400, "该任务不是共识版本")
    return [r for r in consensus.export_rows(a["case_id"]) if r["assignment_id"] == aid]


@app.get("/api/assignments/{aid}/history")
def history(aid: int, user=Depends(current_user)):
    a = _assignment_or_404(aid)
    _check_view(user, a)
    rows = db.connect().execute(
        "SELECT h.version, h.saved_at, u.username FROM assignment_history h JOIN users u ON u.id = h.user_id "
        "WHERE assignment_id = ? ORDER BY version DESC LIMIT 500", (aid,),
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/assignments/{aid}/history/{version}")
def history_version(aid: int, version: int, user=Depends(current_user)):
    a = _assignment_or_404(aid)
    _check_view(user, a)
    r = db.connect().execute(
        "SELECT data FROM assignment_history WHERE assignment_id = ? AND version = ?", (aid, version)
    ).fetchone()
    if r is None:
        raise HTTPException(404, "版本不存在")
    return json.loads(r["data"])


# ------------------------------------------------------------------ 掩膜（轨道 7）


def _editable_assignment(aid: int, user) -> tuple[dict, dict]:
    a = _assignment_or_404(aid)
    c = cases.get_case(a["case_id"])
    if not c["gold_set"]:
        raise HTTPException(400, "仅金标准病例启用关键结构分割")
    if not _can_edit(user, a, c):
        raise HTTPException(403, "当前状态不可编辑")
    return a, c


def _object_or_404(pk: int) -> dict:
    r = db.connect().execute("SELECT * FROM mask_objects WHERE id = ?", (pk,)).fetchone()
    if r is None:
        raise HTTPException(404, "分割目标不存在")
    return dict(r)


@app.get("/api/assignments/{aid}/mask-objects")
def list_mask_objects(aid: int, user=Depends(current_user)):
    a = _assignment_or_404(aid)
    _check_view(user, a)
    objs = export.mask_objects(aid)
    conn = db.connect()
    for o in objs:
        o["frames"] = [dict(r) for r in conn.execute(
            "SELECT frame_idx, source FROM mask_frames WHERE object_pk = ? ORDER BY frame_idx", (o["id"],)
        ).fetchall()]
    return objs


class MaskObjectIn(BaseModel):
    cls: str
    start_ms: int
    end_ms: int
    note: str | None = None


@app.post("/api/assignments/{aid}/mask-objects")
def create_mask_object(aid: int, body: MaskObjectIn, user=Depends(current_user)):
    _editable_assignment(aid, user)
    if body.cls not in {c["code"] for c in MASK_CLASSES}:
        raise HTTPException(400, "结构类别无效")
    if body.end_ms <= body.start_ms:
        raise HTTPException(400, "结束时间必须晚于开始时间")
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) AS n, MAX(CAST(SUBSTR(object_id, 2) AS INTEGER)) AS m FROM mask_objects WHERE assignment_id = ?", (aid,)).fetchone()
    object_id = f"O{(n['m'] or 0) + 1:03d}"
    with db.tx() as conn:
        pk = conn.execute(
            "INSERT INTO mask_objects (assignment_id, object_id, class, start_ms, end_ms, note, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (aid, object_id, body.cls, body.start_ms, body.end_ms, body.note, db.now(), db.now()),
        ).lastrowid
        conn.execute("INSERT INTO mask_history (object_pk, action, detail, user_id, created_at) VALUES (?, 'create', ?, ?, ?)",
                     (pk, json.dumps(body.model_dump()), user["id"], db.now()))
    return _object_or_404(pk)


class MaskObjectPatch(BaseModel):
    cls: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    note: str | None = None


@app.patch("/api/mask-objects/{pk}")
def patch_mask_object(pk: int, body: MaskObjectPatch, user=Depends(current_user)):
    o = _object_or_404(pk)
    _editable_assignment(o["assignment_id"], user)
    vals = body.model_dump(exclude_unset=True)
    if "cls" in vals:
        vals["class"] = vals.pop("cls")
    start = vals.get("start_ms", o["start_ms"])
    end = vals.get("end_ms", o["end_ms"])
    if end <= start:
        raise HTTPException(400, "结束时间必须晚于开始时间")
    with db.tx() as conn:
        if vals:
            conn.execute(
                f"UPDATE mask_objects SET {', '.join(f'{k} = ?' for k in vals)}, verified = 0, updated_at = ? WHERE id = ?",
                (*vals.values(), db.now(), pk),
            )
        # 区间缩短时删除区间外的掩膜帧
        conn.execute("DELETE FROM mask_frames WHERE object_pk = ? AND (time_ms < ? OR time_ms > ?)", (pk, start, end))
        conn.execute("INSERT INTO mask_history (object_pk, action, detail, user_id, created_at) VALUES (?, 'update', ?, ?, ?)",
                     (pk, json.dumps(vals), user["id"], db.now()))
    return _object_or_404(pk)


@app.delete("/api/mask-objects/{pk}")
def delete_mask_object(pk: int, user=Depends(current_user)):
    o = _object_or_404(pk)
    _editable_assignment(o["assignment_id"], user)
    with db.tx() as conn:
        conn.execute("DELETE FROM mask_frames WHERE object_pk = ?", (pk,))
        conn.execute("DELETE FROM mask_objects WHERE id = ?", (pk,))
        conn.execute("INSERT INTO mask_history (object_pk, action, detail, user_id, created_at) VALUES (?, 'delete', ?, ?, ?)",
                     (pk, json.dumps({"object_id": o["object_id"], "class": o["class"]}), user["id"], db.now()))
    return {"ok": True}


@app.get("/api/assignments/{aid}/masks")
def masks_at(aid: int, frame_idx: int, user=Depends(current_user)):
    """当前帧各目标的掩膜；有帧间隔时显示最近一张（不超过一个间隔）。"""
    a = _assignment_or_404(aid)
    _check_view(user, a)
    conn = db.connect()
    out = []
    for o in conn.execute("SELECT * FROM mask_objects WHERE assignment_id = ?", (aid,)).fetchall():
        r = conn.execute(
            "SELECT frame_idx, rle, source FROM mask_frames WHERE object_pk = ? AND frame_idx BETWEEN ? AND ? "
            "ORDER BY ABS(frame_idx - ?) LIMIT 1",
            (o["id"], frame_idx - o["frame_stride"] + 1, frame_idx + o["frame_stride"] - 1, frame_idx),
        ).fetchone()
        if r:
            out.append({"object_pk": o["id"], "object_id": o["object_id"], "class": o["class"],
                        "frame_idx": r["frame_idx"], "exact": r["frame_idx"] == frame_idx,
                        "source": r["source"], "rle": json.loads(r["rle"])})
    return out


class EditIn(BaseModel):
    frame_idx: int
    op: str  # add | subtract | replace
    polygon: list[list[float]]


@app.post("/api/mask-objects/{pk}/edit")
def edit_mask(pk: int, body: EditIn, user=Depends(current_user)):
    o = _object_or_404(pk)
    _, c = _editable_assignment(o["assignment_id"], user)
    h, w = c["height"], c["width"]
    times = media.frame_times(c["case_id"])
    if not (0 <= body.frame_idx < len(times)):
        raise HTTPException(400, "帧号超出范围")
    t = times[body.frame_idx]
    if not (o["start_ms"] <= t <= o["end_ms"]):
        raise HTTPException(400, "当前帧不在该目标的分割区间内")
    poly = rle.polygon_mask(h, w, body.polygon)
    r = db.connect().execute("SELECT rle FROM mask_frames WHERE object_pk = ? AND frame_idx = ?", (pk, body.frame_idx)).fetchone()
    cur = rle.decode(json.loads(r["rle"])) if r else np.zeros((h, w), dtype=bool)
    if body.op == "add":
        new = cur | poly
    elif body.op == "subtract":
        new = cur & ~poly
    elif body.op == "replace":
        new = poly
    else:
        raise HTTPException(400, "op 无效")
    with db.tx() as conn:
        sam.save_frame(pk, body.frame_idx, t, new, "manual_edit", user["id"], conn)
        conn.execute("UPDATE mask_objects SET verified = 0, source_frame = COALESCE(source_frame, ?), updated_at = ? WHERE id = ?",
                     (t, db.now(), pk))
        conn.execute("INSERT INTO mask_history (object_pk, frame_idx, action, detail, user_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (pk, body.frame_idx, f"edit_{body.op}", json.dumps(body.polygon), user["id"], db.now()))
    return {"frame_idx": body.frame_idx, "rle": rle.encode(new)}


@app.delete("/api/mask-objects/{pk}/frames/{frame_idx}")
def delete_mask_frame(pk: int, frame_idx: int, user=Depends(current_user)):
    o = _object_or_404(pk)
    _editable_assignment(o["assignment_id"], user)
    with db.tx() as conn:
        conn.execute("DELETE FROM mask_frames WHERE object_pk = ? AND frame_idx = ?", (pk, frame_idx))
        conn.execute("UPDATE mask_objects SET verified = 0, updated_at = ? WHERE id = ?", (db.now(), pk))
        conn.execute("INSERT INTO mask_history (object_pk, frame_idx, action, user_id, created_at) VALUES (?, ?, 'delete_frame', ?, ?)",
                     (pk, frame_idx, user["id"], db.now()))
    return {"ok": True}


class VerifyIn(BaseModel):
    verified: bool


@app.post("/api/mask-objects/{pk}/verify")
def verify_mask_object(pk: int, body: VerifyIn, user=Depends(require_role(*REVIEWERS))):
    o = _object_or_404(pk)
    a = _assignment_or_404(o["assignment_id"])
    c = _case_or_404(a["case_id"])
    if not c["gold_set"]:
        raise HTTPException(400, "仅金标准病例启用关键结构分割")
    if c["status"] == "locked":
        raise HTTPException(409, "病例已锁定，不能更改掩膜确认状态")
    if a["role"] == "consensus" and a["status"] not in ("in_progress", "returned"):
        raise HTTPException(409, "共识正在评审或已通过，请创建者重新打开后再更改掩膜确认")
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) AS n, SUM(source != 'manual_edit') AS sam FROM mask_frames WHERE object_pk = ?", (pk,)).fetchone()
    if body.verified and not n["n"]:
        raise HTTPException(400, "该目标还没有任何掩膜")
    source = ("sam_corrected" if n["sam"] else "manual") if body.verified else (
        "sam_propagated" if n["sam"] else "manual")
    with db.tx() as conn:
        conn.execute(
            "UPDATE mask_objects SET verified = ?, annotation_source = ?, verified_by = ?, updated_at = ? WHERE id = ?",
            (1 if body.verified else 0, source, user["id"] if body.verified else None, db.now(), pk),
        )
        conn.execute("INSERT INTO mask_history (object_pk, action, user_id, created_at) VALUES (?, ?, ?, ?)",
                     (pk, "verify" if body.verified else "unverify", user["id"], db.now()))
    return _object_or_404(pk)


# ------------------------------------------------------------------ SAM 会话


@app.get("/api/sam/info")
def sam_info(user=Depends(current_user)):
    return {"backend": config.sam_backend()}


class SamSessionIn(BaseModel):
    start_ms: int
    end_ms: int
    stride: int = 1


MAX_SESSION_FRAMES = 6000


@app.post("/api/assignments/{aid}/sam/sessions")
def create_sam_session(aid: int, body: SamSessionIn, user=Depends(current_user)):
    a, c = _editable_assignment(aid, user)
    try:
        get_backend()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"SAM 后端不可用：{e}") from e
    if body.end_ms <= body.start_ms:
        raise HTTPException(400, "片段为空")
    n = (body.end_ms - body.start_ms) / 1000 * (c["fps"] or 30) / max(1, body.stride)
    if n > MAX_SESSION_FRAMES:
        raise HTTPException(400, f"片段约 {int(n)} 帧，超过上限 {MAX_SESSION_FRAMES}；请缩短片段或增大帧间隔")
    s = sam.create_session(aid, c, body.start_ms, body.end_ms, body.stride)
    return s.public()


@app.get("/api/assignments/{aid}/sam/sessions")
def list_sam_sessions(aid: int, user=Depends(current_user)):
    _check_view(user, _assignment_or_404(aid))
    return [s.public() for s in sam.list_sessions(aid)]


def _session_or_404(sid: str, user) -> sam.Session:
    s = sam.get_session(sid)
    if s is None:
        raise HTTPException(404, "会话不存在或已过期，请重新建立")
    _editable_assignment(s.assignment_id, user)
    return s


@app.get("/api/sam/sessions/{sid}")
def get_sam_session(sid: str, user=Depends(current_user)):
    s = sam.get_session(sid)
    if s is None:
        raise HTTPException(404, "会话不存在或已过期")
    _check_view(user, _assignment_or_404(s.assignment_id))
    return s.public()


class PromptIn(BaseModel):
    object_pk: int
    frame_idx: int
    points: list[list[float]] = []
    labels: list[int] = []
    box: list[float] | None = None


@app.post("/api/sam/sessions/{sid}/prompt")
def sam_prompt(sid: str, body: PromptIn, user=Depends(current_user)):
    s = _session_or_404(sid, user)
    o = _object_or_404(body.object_pk)
    if o["assignment_id"] != s.assignment_id:
        raise HTTPException(400, "目标不属于该任务")
    if not body.points and body.box is None:
        raise HTTPException(400, "请至少点击一个点或画一个框")
    if len(body.points) != len(body.labels):
        raise HTTPException(400, "points 与 labels 数量不一致")
    try:
        g, r = sam.prompt(s, body.object_pk, body.frame_idx, body.points, body.labels, body.box, user["id"])
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"frame_idx": g, "rle": r}


class PropagateIn(BaseModel):
    frame_idx: int
    direction: str = "forward"


@app.post("/api/sam/sessions/{sid}/propagate")
def sam_propagate(sid: str, body: PropagateIn, user=Depends(current_user)):
    s = _session_or_404(sid, user)
    if body.direction not in ("forward", "backward", "both"):
        raise HTTPException(400, "direction 无效")
    try:
        sam.start_propagation(s, body.frame_idx, body.direction, user["id"])
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return s.public()


@app.post("/api/sam/sessions/{sid}/cancel")
def sam_cancel(sid: str, user=Depends(current_user)):
    s = _session_or_404(sid, user)
    s.cancel = True
    return s.public()


@app.delete("/api/sam/sessions/{sid}")
def sam_close(sid: str, user=Depends(current_user)):
    s = sam.get_session(sid)
    if s is not None:
        _check_view(user, _assignment_or_404(s.assignment_id))
        sam.close_session(sid)
    return {"ok": True}


# ------------------------------------------------------------------ 项目级


@app.get("/api/project/agreement")
def project_agreement(user=Depends(require_role(*REVIEWERS))):
    """跨病例汇总的标注者间一致性（按标注者对分组，另给出全部配对合并结果）。"""
    settings = db.get_settings()
    conn = db.connect()
    by_pair: dict[str, list] = {}
    all_seqs = []
    for c in conn.execute("SELECT * FROM cases").fetchall():
        c = dict(c)
        rows = conn.execute(
            "SELECT a.*, u.username FROM assignments a JOIN users u ON u.id = a.user_id "
            "WHERE a.case_id = ? AND a.role != 'consensus' ORDER BY u.username", (c["case_id"],),
        ).fetchall()
        for x, y in itertools.combinations(rows, 2):
            seq = analysis.pair_sequences(c, json.loads(x["data"]), json.loads(y["data"]), settings)
            by_pair.setdefault(f"{x['username']} vs {y['username']}", []).append(seq)
            all_seqs.append(seq)
    return {
        "pooled": analysis.summarize(all_seqs) if all_seqs else None,
        "pairs": {k: analysis.summarize(v) for k, v in by_pair.items()},
    }


@app.get("/api/project/sampling-simulation")
def sampling_simulation(multiples: str = "1,2,3", user=Depends(require_role(*REVIEWERS))):
    """用基础间隔（如 5 s）的数据模拟更稀疏采样（10 s、15 s），比较 SCS 稳定比例和 LPS 的 ICC。"""
    settings = db.get_settings()
    base = settings["sampling_interval_ms"]
    ks = [int(x) for x in multiples.split(",") if x.strip()]
    rows = []
    for c in db.connect().execute("SELECT * FROM cases").fetchall():
        c = dict(c)
        for a in export.export_assignments(c, "working"):
            ann = json.loads(a["data"])
            per = {k: analysis.metrics(c, ann, settings, base * k) for k in ks}
            rows.append({
                "case_id": c["case_id"],
                "scs_stable_ratio": {k * base: per[k]["scs_stable_ratio"] for k in ks},
                "lps": {k * base: per[k]["lps"] for k in ks},
            })
    icc = {}
    for k in ks[1:]:
        icc[k * base] = {
            "scs_stable_ratio": analysis.icc_a1([[r["scs_stable_ratio"][base], r["scs_stable_ratio"][k * base]] for r in rows]),
            **{f"LPS_{f}": analysis.icc_a1([[r["lps"][base][f], r["lps"][k * base][f]] for r in rows]) for f in ANCHOR_FIELDS},
        }
    return {"base_interval_ms": base, "cases": rows, "icc": icc, "target": 0.90}


@app.get("/api/project/stats")
def project_stats(user=Depends(require_role(*REVIEWERS))):
    conn = db.connect()
    return {
        "cases": conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0],
        "gold": conn.execute("SELECT COUNT(*) FROM cases WHERE gold_set = 1").fetchone()[0],
        "locked": conn.execute("SELECT COUNT(*) FROM cases WHERE status = 'locked'").fetchone()[0],
        "assignments": {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM assignments GROUP BY status")},
    }


@app.get("/api/project/export.zip")
def project_export(mode: str = "final", user=Depends(require_role(*REVIEWERS))):
    if mode not in ("final", "all"):
        raise HTTPException(400, "mode 只能是 final 或 all")
    try:
        with db.read_snapshot():
            data = export.project_zip(db.get_settings(), mode)
    except export.ExportNotReady as e:
        raise HTTPException(409, str(e)) from e
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="ssi_project_{mode}.zip"'})


@app.post("/api/project/write-folders")
def write_folders(user=Depends(require_role(*REVIEWERS))):
    settings = db.get_settings()
    done = []
    skipped = []
    for c in db.connect().execute("SELECT * FROM cases WHERE status = 'locked'").fetchall():
        try:
            if export.write_case_folder(dict(c), settings):
                done.append(c["case_id"])
            else:
                skipped.append({"case_id": c["case_id"], "reason": "最终版本不可用或缺少有效的历史规则快照，未覆盖已有文件"})
        except export.ExportNotReady as e:
            skipped.append({"case_id": c["case_id"], "reason": str(e)})
    return {"written": done, "skipped": skipped}


# ------------------------------------------------------------------ 前端静态文件


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


_static = config.static_dir()
if _static:
    app.mount("/assets", StaticFiles(directory=_static / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "Not found")
        f = _static / path
        if path and f.is_file():
            return FileResponse(f)
        return FileResponse(_static / "index.html")
