"""病例锁定时的规则快照；每次重新锁定追加记录，不覆盖历史。"""

import hashlib
import json

from . import db
from .protocol import PROTOCOL_VERSION


class MissingRuleSnapshot(ValueError):
    pass


def _encode(settings):
    return json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def current(settings):
    raw = _encode(settings)
    return {"source": "current_project_settings", "protocol_version": PROTOCOL_VERSION,
            "settings": json.loads(raw), "settings_sha256": hashlib.sha256(raw.encode()).hexdigest()}


def capture(conn, case, assignment, settings, user_id):
    """调用方在锁定事务中同时校验、保存快照、更新病例指针。"""
    raw = _encode(settings)
    return conn.execute(
        "INSERT INTO case_rule_snapshots (case_id, assignment_id, assignment_version, protocol_version, "
        "settings_json, settings_sha256, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (case["case_id"], assignment["id"], assignment["version"], PROTOCOL_VERSION, raw,
         hashlib.sha256(raw.encode()).hexdigest(), user_id, db.now()),
    ).lastrowid


def locked(case, assignment):
    row = db.connect().execute(
        "SELECT * FROM case_rule_snapshots WHERE id = ? AND case_id = ? AND assignment_id = ? "
        "AND assignment_version = ?",
        (case.get("locked_rule_snapshot_id"), case["case_id"], assignment["id"], assignment["version"]),
    ).fetchone()
    if row is None:
        raise MissingRuleSnapshot("该锁定病例缺少有效的历史规则快照，不能生成最终数据；请由管理员解锁，按确认的规则重新提交、复核并锁定")
    if hashlib.sha256(row["settings_json"].encode()).hexdigest() != row["settings_sha256"]:
        raise MissingRuleSnapshot("该锁定病例的规则快照校验失败，不能生成最终数据，请检查数据完整性")
    return {"source": "locked_snapshot", "id": row["id"], "case_id": row["case_id"],
            "assignment_id": row["assignment_id"], "assignment_version": row["assignment_version"],
            "protocol_version": row["protocol_version"], "settings": json.loads(row["settings_json"]),
            "settings_sha256": row["settings_sha256"], "created_by": row["created_by"],
            "created_at": row["created_at"]}


def for_assignment(case, assignment, settings):
    if case.get("status") == "locked" and case.get("final_assignment_id") == assignment["id"]:
        return locked(case, assignment)
    return current(settings)
