"""三位独立专家对同一轮、同一内容版本的共识签署。"""
from . import db

REQUIRED = 3


def state(assignment_id, conn=None):
    conn = conn or db.connect()
    return conn.execute("SELECT * FROM consensus_state WHERE assignment_id = ?", (assignment_id,)).fetchone()


def latest_reviews(a, conn=None):
    conn = conn or db.connect()
    s = state(a["id"], conn)
    if s is None:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT r.*, u.username, u.display_name FROM consensus_reviews r JOIN users u ON u.id = r.reviewer_id "
        "WHERE r.assignment_id = ? AND r.round = ? AND r.revision = ? "
        "AND r.id IN (SELECT MAX(id) FROM consensus_reviews WHERE assignment_id = ? AND round = ? "
        "AND revision = ? GROUP BY reviewer_id) ORDER BY r.id",
        (a["id"], s["round"], s["revision"], a["id"], s["round"], s["revision"]),
    )]


def summary(a, conn=None):
    if a["role"] != "consensus":
        return None
    s = state(a["id"], conn)
    reviews = latest_reviews(a, conn)
    current = s is not None and s["review_revision"] == s["revision"] and s["round"] > 0 and a["status"] in ("submitted", "approved")
    approved = [r for r in reviews if r["decision"] == "approve" and r["reviewer_id"] != a["user_id"]]
    rejected = any(r["decision"] == "reject" for r in reviews)
    return {
        "required": REQUIRED, "creator_excluded": True,
        "round": s["round"] if s else 0, "revision": s["revision"] if s else 0,
        "submitted_at": s["submitted_at"] if s else None,
        "current": current, "approved_count": len(approved) if current else 0,
        "complete": current and not rejected and len(approved) >= REQUIRED and a["status"] == "approved",
        "reviews": reviews,
    }


def final_eligible(case, a, conn=None):
    if case["gold_set"] or a["role"] == "consensus":
        return a["role"] == "consensus" and summary(a, conn)["complete"]
    return True


def start_round(conn, a):
    if a["status"] not in ("in_progress", "returned"):
        raise ValueError("请先重新打开共识版本，再提交新一轮共识")
    conn.execute(
        "UPDATE consensus_state SET round = round + 1, review_revision = revision, submitted_at = ? "
        "WHERE assignment_id = ?", (db.now(), a["id"]),
    )


def record_review(conn, a, user, decision, comment, expected_round, expected_revision):
    if user["id"] == a["user_id"]:
        raise PermissionError("共识创建者不能参与独立确认，需要另外三位管理员或专家")
    if a["status"] not in ("submitted", "approved"):
        raise ValueError("共识版本尚未提交或已被退回，请等待创建者提交")
    s = state(a["id"], conn)
    if s is None or s["review_revision"] != s["revision"] or (expected_round, expected_revision) != (s["round"], s["revision"]):
        raise ValueError("共识轮次或内容已变化，请刷新后重新审阅")
    own = next((r for r in latest_reviews(a, conn) if r["reviewer_id"] == user["id"]), None)
    if decision == "approve" and own and own["decision"] == "approve":
        raise ValueError("你已确认本轮共识，不能重复计数")
    if decision == "withdraw" and (not own or own["decision"] != "approve"):
        raise ValueError("你在本轮没有可撤回的确认")
    conn.execute(
        "INSERT INTO consensus_reviews (assignment_id, round, revision, reviewer_id, reviewer_role, decision, comment, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (a["id"], s["round"], s["revision"], user["id"], user["role"], decision, comment, db.now()),
    )
    if decision == "reject":
        return "returned"
    count = sum(r["decision"] == "approve" and r["reviewer_id"] != a["user_id"] for r in latest_reviews(a, conn))
    return "approved" if count >= REQUIRED else "submitted"


def export_rows(case_id, conn=None):
    conn = conn or db.connect()
    return [dict(r) for r in conn.execute(
        "SELECT a.case_id, r.assignment_id, r.round, r.revision, u.username AS reviewer_id, r.reviewer_role, "
        "r.decision, r.comment, r.created_at FROM consensus_reviews r JOIN assignments a ON a.id = r.assignment_id "
        "JOIN users u ON u.id = r.reviewer_id WHERE a.case_id = ? ORDER BY r.id", (case_id,),
    )]
