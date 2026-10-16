import pytest
import io
import time
import zipfile

from fastapi.testclient import TestClient


def _client():
    from ssi_annotator.app import app

    return TestClient(app)


def _login(c, u, p="password123"):
    r = c.post("/api/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text


def _wait_ready(c, cid):
    for _ in range(200):
        case = c.get(f"/api/cases/{cid}").json()
        if case["media_status"] != "processing":
            return case
        time.sleep(0.1)
    raise AssertionError("视频处理超时")


def _full_annotation(duration_ms, interval=5000, gold=False):
    d = {
        "evaluable": [{"start_ms": 0, "label": "E1"}],
        "context": [{"start_ms": 0, "label": "C1"}, {"start_ms": 2000, "label": "C2"}],
        "samples": {str(t): {"scs": "2", "lamina": "V", "facet": "V", "LF_window": "V", "dura": "NA", "root": "NA"}
                    for t in range(0, duration_ms, interval)},
        "events": [],
        "milestones": {},
    }
    if gold:
        d["milestones"] = {f"M{i}": {"time_ms": i * 100, "assessable": 1, "confidence": "high"} for i in range(1, 19)}
    return d


def test_full_workflow(data_dir, numbered_video):
    from ssi_annotator import auth

    auth.create_user("admin", "管理员", "admin", "password123")
    auth.create_user("A01", "标注者1", "annotator", "password123")
    auth.create_user("A02", "标注者2", "annotator", "password123")
    auth.create_user("EXP01", "专家", "reviewer", "password123")
    auth.create_user("EXP02", "专家2", "reviewer", "password123")
    auth.create_user("EXP03", "专家3", "reviewer", "password123")

    c = _client()
    assert c.get("/api/cases").status_code == 401
    _login(c, "admin")
    r = c.post("/api/cases/import", json={"case_id": "SSI_0001", "source_path": str(numbered_video),
                                          "surgeon_id": "SUR_01", "center_id": "C01", "operative_level": "L4/5",
                                          "cumulative_UBE_cases": 86, "gold_set": True})
    assert r.status_code == 200, r.text
    case = _wait_ready(c, "SSI_0001")
    assert case["media_status"] == "ready", case
    assert case["frame_count"] == 120
    users = {u["username"]: u["id"] for u in c.get("/api/users").json()}
    a1 = c.post("/api/cases/SSI_0001/assignments", json={"user_id": users["A01"], "role": "primary"}).json()["id"]
    a2 = c.post("/api/cases/SSI_0001/assignments", json={"user_id": users["A02"], "role": "secondary"}).json()["id"]

    # 视频支持 Range 请求（浏览器拖动进度条依赖它）
    v = c.get("/api/cases/SSI_0001/video", headers={"Range": "bytes=0-99"})
    assert v.status_code == 206 and len(v.content) == 100

    # 标注者 A01
    c.cookies.clear()
    _login(c, "A01")
    assert [x["case_id"] for x in c.get("/api/cases").json()] == ["SSI_0001"]
    assert c.get(f"/api/assignments/{a2}").status_code == 403
    p = c.get(f"/api/assignments/{a1}").json()
    assert p["editable"] and any(i["level"] == "error" for i in p["issues"])
    dur = round(case["video_duration_sec"] * 1000)
    data = _full_annotation(dur, gold=True)
    r = c.put(f"/api/assignments/{a1}/data", json={"data": data, "version": p["assignment"]["version"]})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["metrics"]["scs_stable_ratio"] == 1.0
    # 版本冲突
    assert c.put(f"/api/assignments/{a1}/data", json={"data": data, "version": 1}).status_code == 409

    # 掩膜：建立目标 → SAM(mock) 会话 → 提示 → 传播 → 人工修正 → 确认
    obj = c.post(f"/api/assignments/{a1}/mask-objects", json={"cls": "lamina", "start_ms": 0, "end_ms": 2000}).json()
    assert obj["object_id"] == "O001"
    # 分割列表刷新时获取的任务检查应从“尚无分割”变为“尚未复核”。
    mask_issues = [i for i in c.get(f"/api/assignments/{a1}").json()["issues"] if i["track"] == "masks"]
    assert len(mask_issues) == 1 and "尚未经专家复核" in mask_issues[0]["message"]
    s = c.post(f"/api/assignments/{a1}/sam/sessions", json={"start_ms": 0, "end_ms": 2000, "stride": 2}).json()
    for _ in range(100):
        s = c.get(f"/api/sam/sessions/{s['id']}").json()
        if s["status"] != "loading":
            break
        time.sleep(0.1)
    assert s["status"] == "ready", s
    assert s["frames"][:3] == [0, 2, 4]
    r = c.post(f"/api/sam/sessions/{s['id']}/prompt", json={"object_pk": obj["id"], "frame_idx": 10,
                                                            "points": [[60, 40]], "labels": [1]})
    assert r.status_code == 200, r.text
    assert r.json()["frame_idx"] == 10
    c.post(f"/api/sam/sessions/{s['id']}/propagate", json={"frame_idx": 10, "direction": "both"})
    for _ in range(200):
        s = c.get(f"/api/sam/sessions/{s['id']}").json()
        if s["status"] == "ready":
            break
        time.sleep(0.05)
    objs = c.get(f"/api/assignments/{a1}/mask-objects").json()
    assert objs[0]["n_frames"] == len(s["frames"]) and objs[0]["annotation_source"] == "sam_propagated"
    m = c.get(f"/api/assignments/{a1}/masks", params={"frame_idx": 11}).json()
    assert m and m[0]["frame_idx"] in (10, 12)
    r = c.post(f"/api/mask-objects/{obj['id']}/edit", json={"frame_idx": 20, "op": "add",
                                                           "polygon": [[0, 0], [20, 0], [20, 20]]})
    assert r.status_code == 200, r.text
    # 未确认的掩膜阻止提交
    r = c.post(f"/api/assignments/{a1}/action", json={"action": "submit"})
    assert r.status_code == 409 and "复核" in r.json()["detail"]
    verify_url = f"/api/mask-objects/{obj['id']}/verify"
    # 普通标注者不能通过接口确认或撤销确认；专家可以复核他人的目标。
    for verified in (True, False):
        assert c.post(verify_url, json={"verified": verified}).status_code == 403
    c.cookies.clear()
    _login(c, "EXP01")
    r = c.post(verify_url, json={"verified": True})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["annotation_source"] == "sam_corrected" and v["verified"] == 1
    assert v["verified_by"] == users["EXP01"]
    c.cookies.clear()
    _login(c, "admin")
    r = c.post(verify_url, json={"verified": False})
    assert r.status_code == 200 and r.json()["verified"] == 0
    assert r.json()["verified_by"] is None
    r = c.post(verify_url, json={"verified": True})
    assert r.status_code == 200 and r.json()["verified_by"] == users["admin"]
    assert not [i for i in c.get(f"/api/assignments/{a1}").json()["issues"] if i["track"] == "masks"]
    c.cookies.clear()
    _login(c, "A01")
    assert c.post(verify_url, json={"verified": False}).status_code == 403
    c.delete(f"/api/sam/sessions/{s['id']}")
    r = c.post(f"/api/assignments/{a1}/action", json={"action": "submit"})
    assert r.status_code == 200, r.text
    assert not r.json()["editable"]

    # 标注者 A02（略有差异）
    c.cookies.clear()
    _login(c, "A02")
    d2 = _full_annotation(dur, gold=True)
    d2["context"][1]["start_ms"] = 2500
    p2 = c.get(f"/api/assignments/{a2}").json()
    assert c.put(f"/api/assignments/{a2}/data", json={"data": d2, "version": p2["assignment"]["version"]}).status_code == 200

    # 复核
    c.cookies.clear()
    _login(c, "EXP01")
    assert c.post(f"/api/assignments/{a1}/action", json={"action": "return"}).status_code == 400
    assert c.post(f"/api/assignments/{a1}/action", json={"action": "approve"}).status_code == 200
    ag = c.get("/api/cases/SSI_0001/agreement").json()
    assert ag[0]["summary"]["context"]["boundary_mean_abs_err_ms"] == 500
    assert c.post("/api/cases/SSI_0001/lock", json={"final_assignment_id": a2}).status_code == 409  # A02 无掩膜确认
    assert c.post("/api/cases/SSI_0001/lock", json={"final_assignment_id": a1}).status_code == 409
    consensus_id = c.post("/api/cases/SSI_0001/consensus", json={"from_assignment_id": a1}).json()["id"]
    assert c.post(f"/api/assignments/{consensus_id}/action", json={"action": "submit"}).status_code == 200
    for name in ("admin", "EXP02", "EXP03"):
        c.cookies.clear()
        _login(c, name)
        state = c.get(f"/api/assignments/{consensus_id}").json()["consensus"]
        r = c.post(f"/api/assignments/{consensus_id}/action", json={"action": "approve", "consensus_round": state["round"],
                   "consensus_revision": state["revision"]})
        assert r.status_code == 200, r.text
    r = c.post("/api/cases/SSI_0001/lock", json={"final_assignment_id": consensus_id})
    assert r.status_code == 200, r.text
    assert (data_dir / "cases/SSI_0001/temporal/sampled_ssi.csv").exists()
    assert (data_dir / "cases/SSI_0001/masks/annotations_coco.json").exists()

    z = zipfile.ZipFile(io.BytesIO(c.get("/api/cases/SSI_0001/export.zip").content))
    names = set(z.namelist())
    assert "SSI_0001/gold/milestones.csv" in names and "SSI_0001/video/frame_timestamp_map.csv" in names
    sampled = z.read("SSI_0001/temporal/sampled_ssi.csv").decode().splitlines()
    assert sampled[0] == "case_id,time_ms,frame_idx,context,evaluable,SCS,lamina,facet,LF_window,dura,root,annotator_id"
    assert len(sampled) == 2 and sampled[1] == "SSI_0001,0,0,C1,1,2,V,V,V,NA,NA,EXP01"
    ctx = z.read("SSI_0001/temporal/context_intervals.csv").decode().splitlines()
    assert ctx[1] == "SSI_0001,C1,0,2000,0,60,EXP01"

    pz = zipfile.ZipFile(io.BytesIO(c.get("/api/project/export.zip", params={"mode": "all"}).content))
    assert pz.read("cases.csv").decode().splitlines()[1] == "SSI_0001,SUR_01,C01,L4/5,86,4,30,1"
    assert len(pz.read("context_intervals.csv").decode().splitlines()) == 1 + 2 + 2 + 2
    sim = c.get("/api/project/sampling-simulation").json()
    assert sim["cases"][0]["scs_stable_ratio"]["5000"] == 1.0

    # 锁定后，即使管理员或专家也不能更改确认状态。
    for username in ("EXP01", "admin"):
        c.cookies.clear()
        _login(c, username)
        for verified in (True, False):
            assert c.post(verify_url, json={"verified": verified}).status_code == 409

    # 锁定后不可编辑
    c.cookies.clear()
    _login(c, "A01")
    assert c.put(f"/api/assignments/{a1}/data", json={"data": data, "version": 99}).status_code == 403


def test_anchor_applicability_enforced_on_save(data_dir):
    from copy import deepcopy
    import csv
    import json
    from ssi_annotator import auth, cases, db
    from ssi_annotator.protocol import DEFAULT_SETTINGS

    auth.create_user("admin", "管理员", "admin", "password123")
    uid = auth.create_user("A01", "标注者", "annotator", "password123")
    cases.create_case("ANCHORS", {})
    with db.tx() as conn:
        conn.execute("UPDATE cases SET video_duration_sec = 10, fps = 30 WHERE case_id = 'ANCHORS'")
    c = _client()
    _login(c, "admin")
    aid = c.post("/api/cases/ANCHORS/assignments", json={"user_id": uid, "role": "primary"}).json()["id"]
    c.cookies.clear()
    _login(c, "A01")
    url = f"/api/assignments/{aid}/data"
    data = _full_annotation(10000)
    data["context"] = [{"start_ms": 0, "label": "C1"}]
    version = 1
    for invalid in ("V", "N", "U"):
        data["samples"]["0"]["dura"] = invalid
        r = c.put(url, json={"data": data, "version": version})
        assert r.status_code == 400 and "必须为 NA" in r.json()["detail"]
    data["samples"]["0"].pop("dura")
    r = c.put(url, json={"data": data, "version": version})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["samples"]["0"]["dura"] == "NA"
    assert r.json()["metrics"]["lps"]["dura"] is None
    version = r.json()["assignment"]["version"]
    # 情境改为适用后，不能沿用自动 NA；允许先保存空值再继续标注。
    data = r.json()["data"]
    data["context"][0]["label"] = "C4"
    assert c.put(url, json={"data": data, "version": version}).status_code == 400
    for sample in data["samples"].values():
        sample.pop("dura")
        sample.pop("root")
    r = c.put(url, json={"data": data, "version": version})
    assert r.status_code == 200, r.text
    assert any(i["track"] == "anchors" and i["level"] == "error" for i in r.json()["issues"])
    version = r.json()["assignment"]["version"]
    data["samples"]["0"].update(dura="V", root="U")
    data["samples"]["5000"].update(dura="N", root="U")
    r = c.put(url, json={"data": data, "version": version})
    assert r.status_code == 200, r.text
    assert r.json()["metrics"]["lps"]["dura"] == 0.5
    c.cookies.clear()
    _login(c, "admin")
    z = zipfile.ZipFile(io.BytesIO(c.get("/api/cases/ANCHORS/export.zip", params={"mode": "working", "assignment_id": aid}).content))
    rows = list(csv.DictReader(io.StringIO(z.read("ANCHORS/qc/metrics.csv").decode())))
    assert rows[0]["LPS_dura"] == "0.5" and rows[0]["LPS_root"] == ""
    sim = c.get("/api/project/sampling-simulation").json()
    assert sim["cases"][0]["lps"]["5000"]["dura"] == 0.5
    assert sim["cases"][0]["lps"]["10000"]["dura"] == 1.0
    settings = deepcopy(DEFAULT_SETTINGS)
    settings["anchor_applicability"]["C4"].remove("dura")
    assert c.put("/api/settings", json={"values": {"anchor_applicability": settings["anchor_applicability"]}}).status_code == 200
    assert c.get(f"/api/assignments/{aid}").json()["metrics"]["lps"]["dura"] is None
    # 防止配置缺项或非法字段造成 UI 和统计口径不一致。
    assert c.put("/api/settings", json={"values": {"anchor_applicability": {"C1": []}}}).status_code == 400
    settings["anchor_applicability"]["C4"].append("invalid_anchor")
    assert c.put("/api/settings", json={"values": {"anchor_applicability": settings["anchor_applicability"]}}).status_code == 400


@pytest.mark.parametrize("duration_ms", [1000, 4999, 5000])
def test_short_event_blocks_submit_and_lock(data_dir, duration_ms):
    from ssi_annotator import auth, cases, db

    auth.create_user("admin", "管理员", "admin", "password123")
    uid = auth.create_user("A01", "标注者", "annotator", "password123")
    cases.create_case("DURATION", {})
    with db.tx() as conn:
        conn.execute("UPDATE cases SET video_duration_sec = 15, fps = 30 WHERE case_id = 'DURATION'")
    c = _client()
    _login(c, "admin")
    aid = c.post("/api/cases/DURATION/assignments", json={"user_id": uid, "role": "primary"}).json()["id"]
    c.cookies.clear()
    _login(c, "A01")
    data = _full_annotation(15000)
    data["events"] = [{"id": "OL001", "onset_ms": 1000, "offset_ms": 1000 + duration_ms,
                       "recovery_ms": 1000 + duration_ms + 2000, "disturbance": "none", "confidence": "high"}]
    # 短事件可保存草稿，以便在工作台修改；检查项必须显示 error。
    r = c.put(f"/api/assignments/{aid}/data", json={"data": data, "version": 1})
    assert r.status_code == 200, r.text
    errors = [i for i in r.json()["issues"] if i["level"] == "error"]
    assert bool(errors) == (duration_ms < 5000)
    r = c.post(f"/api/assignments/{aid}/action", json={"action": "submit"})
    if duration_ms < 5000:
        assert r.status_code == 409 and "短于预设" in r.json()["detail"]
        assert c.get(f"/api/assignments/{aid}").json()["assignment"]["status"] == "in_progress"
        # 即便已有 approved 状态，锁定也必须独立重新检查事件时长。
        with db.tx() as conn:
            conn.execute("UPDATE assignments SET status = 'approved' WHERE id = ?", (aid,))
    else:
        assert r.status_code == 200, r.text
    c.cookies.clear()
    _login(c, "admin")
    if duration_ms == 5000:
        assert c.post(f"/api/assignments/{aid}/action", json={"action": "approve"}).status_code == 200
    r = c.post("/api/cases/DURATION/lock", json={"final_assignment_id": aid})
    if duration_ms < 5000:
        assert r.status_code == 409 and "短于预设" in r.json()["detail"]
        assert c.get("/api/cases/DURATION").json()["status"] == "open"
    else:
        assert r.status_code == 200, r.text
