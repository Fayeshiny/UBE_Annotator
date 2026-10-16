"""最终导出必须经过复核、选定和锁定；工作数据需明确选择。"""
import csv
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from ssi_annotator import auth, cases, db
from ssi_annotator.app import app
from ssi_annotator.protocol import empty_annotation


@pytest.fixture()
def export_client(data_dir):
    auth.create_user("admin", "管理员", "admin", "password123")
    uid = auth.create_user("A01", "标注者", "annotator", "password123")
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": "admin", "password": "password123"}).status_code == 200
    return c, uid


def _case(c, uid, cid, role="primary"):
    cases.create_case(cid, {})
    # 不依赖模型或转码，构造完整的 4 秒核心队列标注。
    with db.tx() as conn:
        conn.execute("UPDATE cases SET video_duration_sec = 4, fps = 30 WHERE case_id = ?", (cid,))
    aid = c.post(f"/api/cases/{cid}/assignments", json={"user_id": uid, "role": role}).json()["id"]
    ann = empty_annotation()
    ann["evaluable"] = [{"start_ms": 0, "label": "E1"}]
    ann["context"] = [{"start_ms": 0, "label": "C1"}]
    ann["samples"] = {"0": dict(scs="2", lamina="V", facet="V", LF_window="V", dura="NA", root="NA")}
    with db.tx() as conn:
        conn.execute("UPDATE assignments SET data = ? WHERE id = ?", (json.dumps(ann), aid))
    return aid


def _zip(response):
    assert response.status_code == 200, response.text
    return zipfile.ZipFile(io.BytesIO(response.content))


def _rows(z, name):
    return list(csv.DictReader(io.StringIO(z.read(name).decode())))


@pytest.mark.parametrize("status", ["in_progress", "submitted", "returned", "approved"])
def test_open_case_cannot_export_final_even_with_assignment_id(export_client, status):
    c, uid = export_client
    aid = _case(c, uid, "OPEN")
    with db.tx() as conn:
        conn.execute("UPDATE assignments SET status = ? WHERE id = ?", (status, aid))
        # 模拟解锁后保留的最终任务指针，也不能绕过锁定要求。
        conn.execute("UPDATE cases SET final_assignment_id = ? WHERE case_id = 'OPEN'", (aid,))
    url = "/api/cases/OPEN/export.zip"
    assert c.get(url).status_code == 409
    assert c.get(url, params={"assignment_id": aid}).status_code == 409
    assert c.get("/api/project/export.zip").status_code == 409
    z = _zip(c.get(url, params={"mode": "working", "assignment_id": aid}))
    manifest = json.loads(z.read("OPEN/export_manifest.json"))
    assert manifest["export_mode"] == "working" and manifest["assignment_status"] == status
    assert f"working_{aid}" in c.get(url, params={"mode": "working", "assignment_id": aid}).headers["content-disposition"]
    assert c.get(url, params={"mode": "working"}).status_code == 400
    assert c.get(url, params={"mode": "unknown"}).status_code == 400
    # 调整最终导出规则不应导致未锁定的采样模拟丢失参考病例。
    assert c.get("/api/project/sampling-simulation").json()["cases"][0]["case_id"] == "OPEN"


def test_final_export_selection_project_filtering_and_unlock(export_client):
    c, uid = export_client
    final_id = _case(c, uid, "FINAL")
    draft_id = _case(c, uid, "DRAFT")
    other_id = c.post("/api/cases/FINAL/assignments", json={"user_id": uid, "role": "secondary"}).json()["id"]
    assert c.post(f"/api/assignments/{final_id}/action", json={"action": "approve"}).status_code == 200
    assert c.post("/api/cases/FINAL/lock", json={"final_assignment_id": final_id}).status_code == 200
    z = _zip(c.get("/api/cases/FINAL/export.zip"))
    manifest = json.loads(z.read("FINAL/export_manifest.json"))
    assert manifest["export_mode"] == "final" and manifest["assignment_id"] == final_id
    assert c.get("/api/cases/FINAL/export.zip", params={"assignment_id": other_id}).status_code == 409
    # 显式选择非最终版本只能作为工作数据导出。
    _zip(c.get("/api/cases/FINAL/export.zip", params={"mode": "working", "assignment_id": other_id}))
    # 防止把其他病例任务混入当前病例包。
    for mode in ("final", "working"):
        assert c.get("/api/cases/FINAL/export.zip", params={"mode": mode, "assignment_id": draft_id}).status_code == 400
    pz = _zip(c.get("/api/project/export.zip?mode=final"))
    assert [r["case_id"] for r in _rows(pz, "cases.csv")] == ["FINAL"]
    assert {r["case_id"] for r in _rows(pz, "sampled_ssi.csv")} == {"FINAL"}
    pm = json.loads(pz.read("export_manifest.json"))
    assert pm["included_case_count"] == 1 and pm["excluded_case_count"] == 1
    assert pm["excluded_cases"][0]["case_id"] == "DRAFT"
    assert [a["assignment_id"] for a in pm["assignments"]] == [final_id]
    all_zip = _zip(c.get("/api/project/export.zip?mode=all"))
    assert {r["case_id"] for r in _rows(all_zip, "cases.csv")} == {"FINAL", "DRAFT"}
    assert len(json.loads(all_zip.read("export_manifest.json"))["assignments"]) == 3
    assert c.post("/api/cases/FINAL/unlock", json={"comment": "继续修改"}).status_code == 200
    assert c.get("/api/cases/FINAL/export.zip").status_code == 409
    assert c.get("/api/project/export.zip?mode=final").status_code == 409
    _zip(c.get("/api/cases/FINAL/export.zip", params={"mode": "working", "assignment_id": final_id}))


def test_consensus_requires_confirmation_before_lock(export_client):
    c, uid = export_client
    source = _case(c, uid, "CONSENSUS")
    aid = c.post("/api/cases/CONSENSUS/consensus", json={"from_assignment_id": source}).json()["id"]
    assert c.post("/api/cases/CONSENSUS/lock", json={"final_assignment_id": aid}).status_code == 409
    assert c.post(f"/api/assignments/{aid}/action", json={"action": "submit"}).status_code == 200
    for name in ("R1", "R2", "R3"):
        auth.create_user(name, name, "reviewer", "password123")
        c.cookies.clear()
        assert c.post("/api/auth/login", json={"username": name, "password": "password123"}).status_code == 200
        state = c.get(f"/api/assignments/{aid}").json()["consensus"]
        assert c.post(f"/api/assignments/{aid}/action", json={"action": "approve", "consensus_round": state["round"],
                      "consensus_revision": state["revision"]}).status_code == 200
    assert c.post("/api/cases/CONSENSUS/lock", json={"final_assignment_id": aid}).status_code == 200
    _zip(c.get("/api/cases/CONSENSUS/export.zip"))


def test_legacy_unapproved_locked_consensus_not_final(export_client):
    c, uid = export_client
    aid = _case(c, uid, "LEGACY", role="consensus")
    # 旧版本曾允许直接锁定未确认的共识任务，不能当作已复核最终数据导出。
    with db.tx() as conn:
        conn.execute("UPDATE cases SET status = 'locked', final_assignment_id = ? WHERE case_id = 'LEGACY'", (aid,))
    assert c.get("/api/cases/LEGACY/export.zip").status_code == 409
    assert c.get("/api/project/export.zip").status_code == 409
    _zip(c.get("/api/cases/LEGACY/export.zip", params={"mode": "working", "assignment_id": aid}))
