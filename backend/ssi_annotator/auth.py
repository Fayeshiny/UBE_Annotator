import hashlib
import hmac
import os
import secrets

from fastapi import Depends, HTTPException, Request

from . import db

SESSION_COOKIE = "ssi_session"
SESSION_TTL_SEC = 12 * 3600
_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt, digest = stored.split("$")
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
    return hmac.compare_digest(calc.hex(), digest)


def create_user(username: str, display_name: str, role: str, password: str) -> int:
    with db.tx() as conn:
        # users 使用 INTEGER PRIMARY KEY；显式避开已删除编号，防止旧页面操作落到新账号上。
        uid = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM (SELECT id FROM users UNION ALL "
                           "SELECT target_user_id AS id FROM user_deletion_log)").fetchone()[0]
        conn.execute(
            "INSERT INTO users (id, username, display_name, role, password_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, username, display_name, role, hash_password(password), db.now()),
        )
        return uid


def login(username: str, password: str) -> tuple[str, dict] | None:
    u = db.connect().execute("SELECT * FROM users WHERE username = ? AND active = 1 AND deleted_at IS NULL", (username,)).fetchone()
    if u is None or not verify_password(password, u["password_hash"]):
        return None
    token = secrets.token_urlsafe(32)
    with db.tx() as conn:
        fresh = conn.execute(
            "SELECT * FROM users WHERE id = ? AND active = 1 AND deleted_at IS NULL AND password_hash = ?",
            (u["id"], u["password_hash"]),
        ).fetchone()
        if fresh is None:
            return None  # 密码校验期间账号可能已被删除、停用或重置密码。
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (db.now(),))
        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
            (token, u["id"], db.now() + SESSION_TTL_SEC),
        )
    return token, public_user(fresh)


def logout(token: str):
    with db.tx() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def public_user(u) -> dict:
    return {"id": u["id"], "username": u["username"], "display_name": u["display_name"], "role": u["role"]}


def current_user(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "未登录")
    r = db.connect().execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ? AND s.expires_at > ? AND u.active = 1 AND u.deleted_at IS NULL",
        (token, db.now()),
    ).fetchone()
    if r is None:
        raise HTTPException(401, "登录已过期")
    return public_user(r)


def require_role(*roles):
    def dep(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(403, "无权限")
        return user

    return dep
