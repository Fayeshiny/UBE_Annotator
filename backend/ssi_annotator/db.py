import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config
from .protocol import DEFAULT_SETTINGS

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,          -- annotator_id，例如 A01 / EXP01
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'reviewer', 'annotator')),
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    deleted_at REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_deletion_log (
    id INTEGER PRIMARY KEY,
    actor_id INTEGER NOT NULL REFERENCES users(id),
    target_user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    display_name TEXT NOT NULL,
    deletion_mode TEXT NOT NULL CHECK (deletion_mode IN ('deleted', 'archived')),
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,               -- SSI_0001
    surgeon_id TEXT,
    center_id TEXT,
    operative_level TEXT,
    cumulative_UBE_cases INTEGER,
    gold_set INTEGER NOT NULL DEFAULT 0,
    video_file TEXT,                        -- 相对 cases/<case_id>/video/ 的播放文件
    original_file TEXT,
    video_duration_sec REAL,
    fps REAL,
    frame_count INTEGER,
    width INTEGER,
    height INTEGER,
    media_status TEXT NOT NULL DEFAULT 'processing',  -- processing | ready | error
    media_error TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'locked')),
    final_assignment_id INTEGER,
    locked_rule_snapshot_id INTEGER,
    notes TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK (role IN ('primary', 'secondary', 'pilot', 'consensus')),
    status TEXT NOT NULL DEFAULT 'in_progress'
        CHECK (status IN ('in_progress', 'submitted', 'returned', 'approved')),
    data TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (case_id, user_id, role)
);

-- 每次保存都留存完整快照，满足“保留完整修改记录”
CREATE TABLE IF NOT EXISTS assignment_history (
    id INTEGER PRIMARY KEY,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id),
    version INTEGER NOT NULL,
    data TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    saved_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS review_log (
    id INTEGER PRIMARY KEY,
    case_id TEXT NOT NULL,
    assignment_id INTEGER,
    user_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    comment TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS mask_objects (
    id INTEGER PRIMARY KEY,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id),
    object_id TEXT NOT NULL,                -- O001
    class TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    source_frame INTEGER,                   -- 初始化关键帧（毫秒时间戳，与方案示例一致）
    annotation_source TEXT NOT NULL DEFAULT 'sam_propagated',
    verified INTEGER NOT NULL DEFAULT 0,
    verified_by INTEGER,
    frame_stride INTEGER NOT NULL DEFAULT 1, -- 掩膜帧间隔（每 N 帧一张）
    note TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (assignment_id, object_id)
);

CREATE TABLE IF NOT EXISTS mask_frames (
    object_pk INTEGER NOT NULL REFERENCES mask_objects(id) ON DELETE CASCADE,
    frame_idx INTEGER NOT NULL,
    time_ms INTEGER NOT NULL,
    rle TEXT NOT NULL,                      -- 未压缩 COCO RLE（列优先）
    area INTEGER NOT NULL,
    source TEXT NOT NULL,                   -- prompt | sam_propagated | manual_edit
    updated_by INTEGER,
    updated_at REAL NOT NULL,
    PRIMARY KEY (object_pk, frame_idx)
);

CREATE TABLE IF NOT EXISTS mask_history (
    id INTEGER PRIMARY KEY,
    object_pk INTEGER NOT NULL,
    frame_idx INTEGER,
    action TEXT NOT NULL,
    detail TEXT,
    user_id INTEGER,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_rule_snapshots (
    id INTEGER PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    assignment_id INTEGER NOT NULL REFERENCES assignments(id),
    assignment_version INTEGER NOT NULL,
    protocol_version TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    settings_sha256 TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL
);
"""

CONSENSUS_SCHEMA = """
CREATE TABLE IF NOT EXISTS consensus_state (
    assignment_id INTEGER PRIMARY KEY REFERENCES assignments(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 1,
    round INTEGER NOT NULL DEFAULT 0,
    review_revision INTEGER,
    submitted_at REAL
);
CREATE TABLE IF NOT EXISTS consensus_reviews (
    id INTEGER PRIMARY KEY,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    round INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    reviewer_id INTEGER NOT NULL REFERENCES users(id),
    reviewer_role TEXT NOT NULL CHECK (reviewer_role IN ('admin', 'reviewer')),
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject', 'withdraw')),
    comment TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS consensus_review_lookup ON consensus_reviews (assignment_id, round, revision, reviewer_id, id);
INSERT OR IGNORE INTO consensus_state (assignment_id) SELECT id FROM assignments WHERE role = 'consensus';
CREATE TRIGGER IF NOT EXISTS consensus_assignment_created AFTER INSERT ON assignments WHEN NEW.role = 'consensus'
BEGIN
    INSERT INTO consensus_state (assignment_id) VALUES (NEW.id);
END;
CREATE TRIGGER IF NOT EXISTS consensus_data_changed AFTER UPDATE OF data ON assignments
WHEN NEW.role = 'consensus' AND NEW.data != OLD.data
BEGIN
    UPDATE consensus_state SET revision = revision + 1 WHERE assignment_id = NEW.id;
    UPDATE assignments SET status = 'in_progress' WHERE id = NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS consensus_mask_objects_insert AFTER INSERT ON mask_objects
BEGIN
    UPDATE consensus_state SET revision = revision + 1 WHERE assignment_id = NEW.assignment_id;
    UPDATE assignments SET status = 'in_progress' WHERE id = NEW.assignment_id AND role = 'consensus';
END;
CREATE TRIGGER IF NOT EXISTS consensus_mask_objects_update AFTER UPDATE ON mask_objects
BEGIN
    UPDATE consensus_state SET revision = revision + 1 WHERE assignment_id = NEW.assignment_id;
    UPDATE assignments SET status = 'in_progress' WHERE id = NEW.assignment_id AND role = 'consensus';
END;
CREATE TRIGGER IF NOT EXISTS consensus_mask_objects_delete AFTER DELETE ON mask_objects
BEGIN
    UPDATE consensus_state SET revision = revision + 1 WHERE assignment_id = OLD.assignment_id;
    UPDATE assignments SET status = 'in_progress' WHERE id = OLD.assignment_id AND role = 'consensus';
END;
CREATE TRIGGER IF NOT EXISTS consensus_mask_frames_insert AFTER INSERT ON mask_frames
BEGIN
    UPDATE consensus_state SET revision = revision + 1 WHERE assignment_id = (SELECT assignment_id FROM mask_objects WHERE id = NEW.object_pk);
    UPDATE assignments SET status = 'in_progress' WHERE id = (SELECT assignment_id FROM mask_objects WHERE id = NEW.object_pk) AND role = 'consensus';
END;
CREATE TRIGGER IF NOT EXISTS consensus_mask_frames_update AFTER UPDATE ON mask_frames
BEGIN
    UPDATE consensus_state SET revision = revision + 1 WHERE assignment_id = (SELECT assignment_id FROM mask_objects WHERE id = NEW.object_pk);
    UPDATE assignments SET status = 'in_progress' WHERE id = (SELECT assignment_id FROM mask_objects WHERE id = NEW.object_pk) AND role = 'consensus';
END;
CREATE TRIGGER IF NOT EXISTS consensus_mask_frames_delete AFTER DELETE ON mask_frames
BEGIN
    UPDATE consensus_state SET revision = revision + 1 WHERE assignment_id = (SELECT assignment_id FROM mask_objects WHERE id = OLD.object_pk);
    UPDATE assignments SET status = 'in_progress' WHERE id = (SELECT assignment_id FROM mask_objects WHERE id = OLD.object_pk) AND role = 'consensus';
END;
"""


_local = threading.local()


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        # isolation_level=None：自动提交模式，写操作统一通过 tx() 显式开启事务
        conn = sqlite3.connect(config.db_path(), timeout=30, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        _local.conn = conn
    return conn


def reset_connection():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


@contextmanager
def tx():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise



@contextmanager
def read_snapshot():
    """导出时保持同一数据库快照，避免签署、标注和清单来自不同时刻。"""
    conn = connect()
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    if "deleted_at" not in {r["name"] for r in conn.execute("PRAGMA table_info(users)")}:
        conn.execute("ALTER TABLE users ADD COLUMN deleted_at REAL")
    if "locked_rule_snapshot_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(cases)")}:
        # 不用当前规则回填旧病例，无法据此还原历史锁定时的规则。
        conn.execute("ALTER TABLE cases ADD COLUMN locked_rule_snapshot_id INTEGER")
    conn.executescript(CONSENSUS_SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, json.dumps(v)))


def get_settings() -> dict:
    rows = connect().execute("SELECT key, value FROM settings").fetchall()
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: json.loads(r["value"]) for r in rows})
    return s


def set_settings(values: dict):
    with tx() as conn:
        for k, v in values.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, json.dumps(v)),
            )


def now() -> float:
    return time.time()


def row(r) -> dict | None:
    return dict(r) if r is not None else None
