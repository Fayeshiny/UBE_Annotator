"""锁定规则的复现性、跨病例版本、解锁重审和旧库兼容。"""
import csv
import io
import json
import zipfile
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from ssi_annotator import auth, cases, db
from ssi_annotator.app import app
from ssi_annotator.protocol import empty_annotation


@pytest.fixture()
def clients(data_dir):
    auth.create_user("admin", "管理员", "admin", "password123")
    uid = auth.create_user("A01", "标注者", "annotator", "password123")
    admin, annotator = TestClient(app), TestClient(app)
    for client, username in ((admin, "admin"), (annotator, "A01")):
        assert client.post("/api/auth/login", json={"username": username, "password": "password123"}).status_code == 200
    return admin, annotator, uid, data_dir


def make_case(clients, cid, interval=5000):
    admin, annotator, uid, _ = clients
    cases.create_case(cid, {})
    with db.tx() as conn:
        conn.execute("UPDATE cases SET video_duration_sec=30, fps=30 WHERE case_id=?", (cid,))
    aid = admin.post(f"/api/cases/{cid}/assignments", json={"user_id": uid, "role": "primary"}).json()["id"]
    ann = empty_annotation()
    ann["evaluable"] = [{"start_ms": 0, "label": "E1"}]
    ann["context"] = [{"start_ms": 0, "label": "C4"}]
    applicable = db.get_settings()["anchor_applicability"]["C4"]
    ann["samples"] = {str(t): {"scs": "2" if t % 10000 == 0 else "1",
                              **{f: ("V" if t % 10000 == 0 else "N") if f in applicable else "NA"
                                 for f in ("lamina", "facet", "LF_window", "dura", "root")}}
                      for t in range(0, 30000, interval)}
    response = annotator.put(f"/api/assignments/{aid}/data", json={"version": 1, "data": ann})
    assert response.status_code == 200, response.text
    review(clients, aid)
    return aid


def review(clients, aid):
    admin, annotator, *_ = clients
    assert annotator.post(f"/api/assignments/{aid}/action", json={"action": "submit"}).status_code == 200
    assert admin.post(f"/api/assignments/{aid}/action", json={"action": "approve"}).status_code == 200


def lock(clients, cid, aid):
    response = clients[0].post(f"/api/cases/{cid}/lock", json={"final_assignment_id": aid})
    assert response.status_code == 200, response.text
    return response.json()


def entries(response):
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        return {name: z.read(name) for name in z.namelist()}


def rows(content):
    return list(csv.DictReader(io.StringIO(content.decode())))


def revise_rules(admin):
    applicability = deepcopy(db.get_settings()["anchor_applicability"])
    applicability["C4"].remove("root")
    response = admin.put("/api/settings", json={"values": {
        "rules_frozen": False, "sampling_interval_ms": 10000,
        "anchor_applicability": applicability, "ol_min_duration_ms": 7000,
    }})
    assert response.status_code == 200, response.text


def test_final_exports_folders_and_workspace_survive_global_rule_changes(clients):
    admin, _, _, data_dir = clients
    aid = make_case(clients, "OLD")
    assert admin.put("/api/settings", json={"values": {"rules_frozen": True}}).status_code == 200
    locked = lock(clients, "OLD", aid)
    before = entries(admin.get("/api/cases/OLD/export.zip"))
    original = admin.get(f"/api/assignments/{aid}").json()
    assert len(rows(before["OLD/temporal/sampled_ssi.csv"])) == 6
    assert original["metrics"]["scs_stable_ratio"] == 0.5
    assert original["metrics"]["lps"]["root"] == 0.5

    revise_rules(admin)
    assert entries(admin.get("/api/cases/OLD/export.zip")) == before
    after = admin.get(f"/api/assignments/{aid}").json()
    assert after["metrics"] == original["metrics"] and after["issues"] == original["issues"]
    assert after["settings"]["sampling_interval_ms"] == 5000
    assert after["settings"]["ol_min_duration_ms"] == 5000
    assert "root" in after["settings"]["anchor_applicability"]["C4"]
    assert after["applied_rules"]["id"] == locked["locked_rule_snapshot_id"]
    assert after["assignment"]["version"] == original["assignment"]["version"]
    assert admin.post("/api/project/write-folders").json() == {"written": ["OLD"], "skipped": []}
    root = data_dir / "cases" / "OLD"
    for rel in ("temporal/sampled_ssi.csv", "qc/metrics.csv", "qc/issues.json", "qc/rules.json"):
        assert (root / rel).read_bytes() == before[f"OLD/{rel}"]
    project = entries(admin.get("/api/project/export.zip"))
    assert rows(project["sampled_ssi.csv"]) == rows(before["OLD/temporal/sampled_ssi.csv"])
    assert rows(project["metrics.csv"]) == rows(before["OLD/qc/metrics.csv"])
    # 工作版本和全部标注者出口若包含已锁定最终任务，也使用同一快照。
    working = entries(admin.get(f"/api/cases/OLD/export.zip?mode=working&assignment_id={aid}"))
    assert working["OLD/temporal/sampled_ssi.csv"] == before["OLD/temporal/sampled_ssi.csv"]
    all_data = entries(admin.get("/api/project/export.zip?mode=all"))
    assert all_data["sampled_ssi.csv"] == project["sampled_ssi.csv"]


def test_project_exports_each_case_with_its_own_snapshot(clients):
    admin = clients[0]
    aid = make_case(clients, "FIVE")
    first = lock(clients, "FIVE", aid)
    revise_rules(admin)
    bid = make_case(clients, "TEN", interval=10000)
    second = lock(clients, "TEN", bid)
    project = entries(admin.get("/api/project/export.zip"))
    samples = rows(project["sampled_ssi.csv"])
    assert sum(r["case_id"] == "FIVE" for r in samples) == 6
    assert sum(r["case_id"] == "TEN" for r in samples) == 3
    metrics = {r["case_id"]: r for r in rows(project["metrics.csv"])}
    assert metrics["FIVE"]["scs_stable_ratio"] == "0.5"
    assert metrics["TEN"]["scs_stable_ratio"] == "1"
    assert metrics["FIVE"]["LPS_root"] == "0.5" and metrics["TEN"]["LPS_root"] == ""
    manifest = json.loads(project["export_manifest.json"])
    applied = {a["case_id"]: a["applied_rules"] for a in manifest["assignments"]}
    assert applied["FIVE"]["id"] == first["locked_rule_snapshot_id"]
    assert applied["TEN"]["id"] == second["locked_rule_snapshot_id"]
    assert applied["FIVE"]["settings_sha256"] != applied["TEN"]["settings_sha256"]


def test_unlock_requires_new_review_and_relock_keeps_old_snapshot(clients):
    admin, _, _, _ = clients
    aid = make_case(clients, "C")
    first = lock(clients, "C", aid)
    old_snapshot = dict(db.connect().execute("SELECT * FROM case_rule_snapshots WHERE id=?",
                                            (first["locked_rule_snapshot_id"],)).fetchone())
    assert admin.post("/api/cases/C/unlock", json={"comment": "修订采样规则"}).status_code == 200
    assert admin.get("/api/cases/C/export.zip").status_code == 409
    assert admin.post("/api/cases/C/lock", json={"final_assignment_id": aid}).status_code == 409
    assert admin.get(f"/api/assignments/{aid}").json()["assignment"]["status"] == "in_progress"
    # 仅改变间隔，保留原始 5 秒采样数据；新的 10 秒版本须重新复核。
    assert admin.put("/api/settings", json={"values": {"sampling_interval_ms": 10000}}).status_code == 200
    review(clients, aid)
    second = lock(clients, "C", aid)
    assert second["locked_rule_snapshot_id"] != first["locked_rule_snapshot_id"]
    assert dict(db.connect().execute("SELECT * FROM case_rule_snapshots WHERE id=?", (old_snapshot["id"],)).fetchone()) == old_snapshot
    assert len(rows(entries(admin.get("/api/cases/C/export.zip"))["C/temporal/sampled_ssi.csv"])) == 3


def test_legacy_lock_without_snapshot_is_explicitly_excluded(clients):
    admin, _, _, data_dir = clients
    aid = make_case(clients, "LEGACY")
    with db.tx() as conn:
        conn.execute("UPDATE cases SET status='locked',final_assignment_id=? WHERE case_id='LEGACY'", (aid,))
    historical = data_dir / "cases" / "LEGACY" / "temporal" / "sampled_ssi.csv"
    historical.parent.mkdir(parents=True)
    historical.write_text("historical export,do not overwrite\n")
    assert admin.get("/api/cases/LEGACY/export.zip").status_code == 409
    assert "历史规则快照" in admin.get("/api/cases/LEGACY/export.zip").json()["detail"]
    preview = admin.get(f"/api/assignments/{aid}").json()
    assert preview["rules_warning"] and preview["applied_rules"]["source"] == "current_project_settings"
    result = admin.post("/api/project/write-folders").json()
    assert result["written"] == [] and result["skipped"][0]["case_id"] == "LEGACY"
    assert historical.read_text() == "historical export,do not overwrite\n"
    working = entries(admin.get(f"/api/cases/LEGACY/export.zip?mode=working&assignment_id={aid}"))
    assert json.loads(working["LEGACY/qc/rules.json"])["warning"]
    fresh = make_case(clients, "FRESH")
    lock(clients, "FRESH", fresh)
    manifest = json.loads(entries(admin.get("/api/project/export.zip"))["export_manifest.json"])
    assert manifest["included_case_count"] == 1
    assert manifest["excluded_cases"][0]["case_id"] == "LEGACY"
    assert "历史规则快照" in manifest["excluded_cases"][0]["reason"]


def test_upgrade_does_not_invent_historical_rule_snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("SSI_DATA_DIR", str(tmp_path / "legacy"))
    db.reset_connection()
    try:
        db.connect().executescript(db.SCHEMA.replace("    locked_rule_snapshot_id INTEGER,\n", ""))
        uid = auth.create_user("old", "old", "admin", "password123")
        cases.create_case("OLD", {})
        data = json.dumps(empty_annotation())
        with db.tx() as conn:
            aid = conn.execute("INSERT INTO assignments(case_id,user_id,role,status,data,created_at,updated_at) "
                               "VALUES('OLD',?,'primary','approved',?,0,0)", (uid, data)).lastrowid
            conn.execute("UPDATE cases SET status='locked',final_assignment_id=? WHERE case_id='OLD'", (aid,))
        db.init_db()
        db.init_db()
        assert cases.get_case("OLD")["locked_rule_snapshot_id"] is None
        assert db.connect().execute("SELECT COUNT(*) FROM case_rule_snapshots").fetchone()[0] == 0
        assert db.connect().execute("SELECT data FROM assignments WHERE id=?", (aid,)).fetchone()[0] == data
    finally:
        db.reset_connection()


@pytest.mark.parametrize("corruption", ["missing", "hash", "assignment_version"])
def test_invalid_snapshot_cannot_fall_back_to_current_rules(clients, corruption):
    admin = clients[0]
    aid = make_case(clients, "C")
    case = lock(clients, "C", aid)
    with db.tx() as conn:
        if corruption == "missing":
            conn.execute("UPDATE cases SET locked_rule_snapshot_id=NULL WHERE case_id='C'")
        elif corruption == "hash":
            conn.execute("UPDATE case_rule_snapshots SET settings_json='{}' WHERE id=?", (case["locked_rule_snapshot_id"],))
        else:
            conn.execute("UPDATE assignments SET version=version+1 WHERE id=?", (aid,))
    assert admin.get("/api/cases/C/export.zip").status_code == 409
    assert admin.get("/api/project/export.zip").status_code == 409
    assert admin.post("/api/project/write-folders").json()["written"] == []


def test_failed_or_duplicate_lock_does_not_create_snapshot(clients):
    admin = clients[0]
    aid = make_case(clients, "C")
    assert admin.post(f"/api/assignments/{aid}/action", json={"action": "reopen"}).status_code == 200
    assert admin.post("/api/cases/C/lock", json={"final_assignment_id": aid}).status_code == 409
    assert db.connect().execute("SELECT COUNT(*) FROM case_rule_snapshots").fetchone()[0] == 0
    review(clients, aid)
    lock(clients, "C", aid)
    assert admin.post("/api/cases/C/lock", json={"final_assignment_id": aid}).status_code == 409
    assert db.connect().execute("SELECT COUNT(*) FROM case_rule_snapshots").fetchone()[0] == 1
