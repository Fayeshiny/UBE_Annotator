"""稳定期不足阈值的事件必须合并，所有最终数据出口均执行此要求。"""
import csv
import io
import json
import zipfile
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from ssi_annotator import analysis, auth, cases, db, rules
from ssi_annotator.app import app
from ssi_annotator.protocol import DEFAULT_SETTINGS, empty_annotation


def event(eid, onset, offset, recovery):
    return dict(id=eid, onset_ms=onset, offset_ms=offset, recovery_ms=recovery,
                disturbance="none", confidence="high", note="")


def annotation(stable_ms=1000):
    ann = empty_annotation()
    ann["evaluable"] = [{"start_ms": 0, "label": "E1"}]
    ann["context"] = [{"start_ms": 0, "label": "C1"}]
    ann["samples"] = {str(t): dict(scs="1", lamina="V", facet="V", LF_window="V", dura="NA", root="NA")
                      for t in range(0, 30000, 5000)}
    second = 7000 + stable_ms
    ann["events"] = [event("OL001", 1000, 6000, 7000), event("OL002", second, second + 6000, second + 8000)]
    return ann


@pytest.fixture()
def workflow(data_dir):
    admin_id = auth.create_user("admin", "管理员", "admin", "password123")
    uid = auth.create_user("A01", "标注者", "annotator", "password123")
    admin, annotator = TestClient(app), TestClient(app)
    for c, name in ((admin, "admin"), (annotator, "A01")):
        assert c.post("/api/auth/login", json={"username": name, "password": "password123"}).status_code == 200
    return admin, annotator, uid, admin_id, data_dir


def create_assignment(workflow, cid, ann):
    admin, annotator, uid, *_ = workflow
    cases.create_case(cid, {})
    with db.tx() as conn:
        conn.execute("UPDATE cases SET video_duration_sec=30,fps=30 WHERE case_id=?", (cid,))
    aid = admin.post(f"/api/cases/{cid}/assignments", json={"user_id": uid, "role": "primary"}).json()["id"]
    saved = annotator.put(f"/api/assignments/{aid}/data", json={"data": ann, "version": 1})
    assert saved.status_code == 200, saved.text
    return aid, saved.json()


def export_rows(response, filename):
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        return list(csv.DictReader(io.StringIO(z.read(filename).decode())))


@pytest.mark.parametrize("stable_ms,blocked", [(0, True), (1000, True), (1999, True), (2000, False), (2001, False)])
def test_merge_is_blocking_at_submit_review_and_lock(workflow, stable_ms, blocked):
    admin, annotator, *_ = workflow
    aid, saved = create_assignment(workflow, "C", annotation(stable_ms))
    issues = [i for i in saved["issues"] if i.get("fix", {}).get("type") == "merge_events"]
    assert bool(issues) == blocked
    if blocked:
        assert issues[0]["level"] == "error"
        assert issues[0]["fix"]["ids"] == ["OL001", "OL002"]
    submitted = annotator.post(f"/api/assignments/{aid}/action", json={"action": "submit"})
    assert submitted.status_code == (409 if blocked else 200)
    approved = admin.post(f"/api/assignments/{aid}/action", json={"action": "approve"})
    assert approved.status_code == (409 if blocked else 200)
    if blocked:
        assert "必须合并" in submitted.json()["detail"] and "必须合并" in approved.json()["detail"]
        assert annotator.get(f"/api/assignments/{aid}").json()["assignment"]["status"] == "in_progress"
        # 即使旧版本已经复核通过，锁定仍须重新检查。
        with db.tx() as conn:
            conn.execute("UPDATE assignments SET status='approved' WHERE id=?", (aid,))
    locked = admin.post("/api/cases/C/lock", json={"final_assignment_id": aid})
    assert locked.status_code == (409 if blocked else 200), locked.text
    if blocked:
        assert "必须合并" in locked.json()["detail"]
        assert cases.get_case("C")["status"] == "open"
        assert db.connect().execute("SELECT COUNT(*) FROM case_rule_snapshots").fetchone()[0] == 0
    else:
        rows = export_rows(admin.get("/api/cases/C/export.zip"), "C/temporal/orientation_loss.csv")
        assert len(rows) == 2


def test_merge_then_resubmit_produces_one_event_and_correct_olf(workflow):
    admin, annotator, *_ = workflow
    ann = annotation()
    aid, saved = create_assignment(workflow, "C", ann)
    assert annotator.post(f"/api/assignments/{aid}/action", json={"action": "submit"}).status_code == 409
    # 对应前端一键合并：保留首次起点、最后一次结束和恢复点。
    first, last = ann["events"]
    ann["events"] = [{**first, "offset_ms": last["offset_ms"], "recovery_ms": last["recovery_ms"],
                      "note": "合并自 OL001+OL002"}]
    corrected = annotator.put(f"/api/assignments/{aid}/data", json={"data": ann, "version": saved["assignment"]["version"]})
    assert corrected.status_code == 200, corrected.text
    assert not [i for i in corrected.json()["issues"] if i["level"] == "error"]
    assert annotator.post(f"/api/assignments/{aid}/action", json={"action": "submit"}).status_code == 200
    assert admin.post(f"/api/assignments/{aid}/action", json={"action": "approve"}).status_code == 200
    assert admin.post("/api/cases/C/lock", json={"final_assignment_id": aid}).status_code == 200
    response = admin.get("/api/cases/C/export.zip")
    rows = export_rows(response, "C/temporal/orientation_loss.csv")
    assert len(rows) == 1
    assert (rows[0]["onset_ms"], rows[0]["offset_ms"], rows[0]["recovery_ms"], rows[0]["SRT_ms"]) == ("1000", "14000", "16000", "2000")
    metrics = export_rows(response, "C/qc/metrics.csv")[0]
    assert metrics["ol_events"] == "1" and metrics["olf_per_10min"] == "20"


def test_consensus_cannot_submit_unmerged_events(workflow):
    admin, *_ = workflow
    aid, _ = create_assignment(workflow, "C", annotation())
    consensus = admin.post("/api/cases/C/consensus", json={"from_assignment_id": aid}).json()["id"]
    response = admin.post(f"/api/assignments/{consensus}/action", json={"action": "submit"})
    assert response.status_code == 409 and "必须合并" in response.json()["detail"]
    assert admin.get(f"/api/assignments/{consensus}").json()["consensus"]["round"] == 0


def legacy_lock(workflow, cid, aid):
    """模拟旧代码允许带合并警告的版本成为已锁定最终数据。"""
    with db.tx() as conn:
        conn.execute("UPDATE assignments SET status='approved' WHERE id=?", (aid,))
        assignment = dict(conn.execute("SELECT * FROM assignments WHERE id=?", (aid,)).fetchone())
        snapshot = rules.capture(conn, cases.get_case(cid), assignment, db.get_settings(), workflow[3])
        conn.execute("UPDATE cases SET status='locked',final_assignment_id=?,locked_rule_snapshot_id=? WHERE case_id=?", (aid, snapshot, cid))


def test_legacy_final_is_rejected_but_work_data_and_old_files_are_preserved(workflow):
    admin, _, _, _, data_dir = workflow
    aid, _ = create_assignment(workflow, "OLD", annotation())
    legacy_lock(workflow, "OLD", aid)
    path = data_dir / "cases" / "OLD" / "temporal" / "orientation_loss.csv"
    path.parent.mkdir(parents=True)
    path.write_text("old export\n")
    response = admin.get("/api/cases/OLD/export.zip")
    assert response.status_code == 409 and "应合并" in response.json()["detail"]
    project = admin.get("/api/project/export.zip")
    assert project.status_code == 409 and "应合并" in project.json()["detail"]
    written = admin.post("/api/project/write-folders").json()
    assert written["written"] == [] and "应合并" in written["skipped"][0]["reason"]
    assert path.read_text() == "old export\n"
    assert len(export_rows(admin.get(f"/api/cases/OLD/export.zip?mode=working&assignment_id={aid}"), "OLD/temporal/orientation_loss.csv")) == 2
    valid, _ = create_assignment(workflow, "VALID", annotation(2000))
    legacy_lock(workflow, "VALID", valid)
    response = admin.get("/api/project/export.zip")
    assert {r["case_id"] for r in export_rows(response, "orientation_loss.csv")} == {"VALID"}
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        manifest = json.loads(z.read("export_manifest.json"))
    assert manifest["excluded_cases"][0]["case_id"] == "OLD"
    assert "应合并" in manifest["excluded_cases"][0]["reason"]


@pytest.mark.parametrize("locked_min,current_min,status", [(2000, 3000, 200), (3000, 1000, 409)])
def test_final_merge_check_uses_locked_rule_snapshot(workflow, locked_min, current_min, status):
    admin = workflow[0]
    assert admin.put("/api/settings", json={"values": {"recovery_min_stable_ms": locked_min}}).status_code == 200
    aid, _ = create_assignment(workflow, "C", annotation(2000))
    legacy_lock(workflow, "C", aid)
    assert admin.put("/api/settings", json={"values": {"recovery_min_stable_ms": current_min}}).status_code == 200
    assert admin.get("/api/cases/C/export.zip").status_code == status
    assert admin.get("/api/project/export.zip").status_code == status


def test_merge_uses_recovery_time_not_offset_and_finds_chains():
    ann = annotation()
    ann["events"] = [event("OL003", 16000, 22000, 24000), event("OL001", 1000, 6000, 9000),
                     event("OL002", 10000, 15000, 15500)]
    issues = analysis.event_merge_issues(ann, DEFAULT_SETTINGS)
    assert [i["fix"]["ids"] for i in issues] == [["OL001", "OL002"], ["OL002", "OL003"]]
    settings = deepcopy(DEFAULT_SETTINGS)
    settings["recovery_min_stable_ms"] = 2500
    ann = annotation(2499)
    assert "≥2.5s" in analysis.event_merge_issues(ann, settings)[0]["message"]
    assert not analysis.event_merge_issues(annotation(2500), settings)
