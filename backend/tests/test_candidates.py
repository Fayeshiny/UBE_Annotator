"""单帧候选必须使用精确帧、隔离模型状态，并且不写正式标注。"""
import importlib
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ssi_annotator import auth, cases, config, db, rle
from ssi_annotator.sam import service
from ssi_annotator.sam.candidates import accept_mask, grid_points
from ssi_annotator.sam.mock import MockBackend


def wait_for(fn):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("后台任务未按时完成")


class RecordingBackend(MockBackend):
    def __init__(self):
        super().__init__()
        self.loaded_pixels = []
        self.prompt_hook = None
        self.calls = []

    def start(self, frames_dir, width, height):
        files = sorted(frames_dir.glob("*.jpg"))
        self.loaded_pixels.append([float(np.asarray(Image.open(f)).mean()) for f in files])
        return super().start(frames_dir, width, height)

    def prompt(self, session, frame, obj_id, points, labels, box):
        self.calls.append((session, frame, obj_id, points))
        if self.prompt_hook:
            self.prompt_hook()
        # Each automatic point must start with no existing target/prompt.
        if self.sessions[session]["n"] == 1:
            assert self.sessions[session]["objs"] == {}
        return super().prompt(session, frame, obj_id, points, labels, box)


@pytest.fixture()
def setup_candidates(data_dir, numbered_video, monkeypatch):
    app_module = importlib.import_module("ssi_annotator.app")
    backend = RecordingBackend()
    monkeypatch.setattr(service, "_sessions", {})
    monkeypatch.setattr(service, "get_backend", lambda: backend)
    monkeypatch.setattr(app_module, "get_backend", lambda: backend)
    owner_id = auth.create_user("owner", "标注者", "annotator", "password123")
    auth.create_user("other", "其他标注者", "annotator", "password123")
    auth.create_user("expert", "专家", "reviewer", "password123")
    cases.create_case("CANDIDATE", {"gold_set": True})
    cases.process_video("CANDIDATE", numbered_video, transcode=False)
    case = cases.get_case("CANDIDATE")
    assert case["media_status"] == "ready"
    with db.tx() as conn:
        aid = conn.execute(
            "INSERT INTO assignments (case_id, user_id, role, data, created_at, updated_at) "
            "VALUES (?, ?, 'primary', '{}', ?, ?)", (case["case_id"], owner_id, db.now(), db.now()),
        ).lastrowid
    clients = {}
    for name in ("owner", "other", "expert"):
        client = TestClient(app_module.app)
        assert client.post("/api/auth/login", json={"username": name, "password": "password123"}).status_code == 200
        clients[name] = client
    response = clients["owner"].post(f"/api/assignments/{aid}/sam/sessions", json={"start_ms": 200, "end_ms": 1600, "stride": 5})
    assert response.status_code == 200, response.text
    sid = response.json()["id"]
    session = service.get_session(sid)
    wait_for(lambda: session.status != "loading")
    assert session.status == "ready", session.error
    env = SimpleNamespace(client=clients["owner"], clients=clients, aid=aid, session=session, backend=backend,
                          url=f"/api/sam/sessions/{sid}/candidates")
    yield env
    for s in list(service._sessions.values()):
        service.close_session(s.id)
    if session.candidate_job:
        wait_for(lambda: not session.candidates_busy)
    for client in clients.values():
        client.close()


def finish(env):
    wait_for(lambda: not env.session.candidates_busy)
    response = env.client.get(env.url)
    assert response.status_code == 200, response.text
    return response.json()


def formal_data():
    return {table: [tuple(row) for row in db.connect().execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("mask_objects", "mask_frames", "mask_history", "assignments", "assignment_history")}


def test_grid_covers_whole_image_and_filters_masks():
    points = grid_points(128, 96)
    assert len(points) == len({tuple(p) for p in points}) == 36
    assert points[0] == [10, 8] and points[-1] == [117, 88]
    assert accept_mask(np.zeros((96, 128)), 128, 96, []) is None
    assert accept_mask(np.ones((96, 128)), 128, 96, []) is None
    mask = np.zeros((96, 128), bool)
    mask[20:40, 30:50] = True
    assert np.array_equal(accept_mask(mask, 128, 96, []), mask)
    almost_same = mask.copy()
    almost_same[20, 30] = False
    assert accept_mask(almost_same, 128, 96, [mask]) is None
    with pytest.raises(ValueError, match="尺寸"):
        accept_mask(np.zeros((1, 1)), 128, 96, [])


def test_exact_frame_without_target_or_manual_prompt(setup_candidates):
    e = setup_candidates
    assert e.client.get(e.url).json() is None
    assert 17 not in e.session.frames  # 片段按每 5 帧抽取，所选帧仍须精确读取。
    before = formal_data()
    response = e.client.post(e.url, json={"frame_idx": 17})
    assert response.status_code == 200, response.text
    job = finish(e)
    assert job["status"] == "completed" and job["progress"] == 1
    assert job["frame_idx"] == 17 and job["time_ms"] == 567
    assert len(job["candidates"]) == 36
    assert abs(e.backend.loaded_pixels[-1][0] - 34) <= 2  # 编号视频像素值=帧号×2。
    assert len(e.backend.loaded_pixels[-1]) == 1
    for candidate in job["candidates"]:
        mask = rle.decode(candidate["rle"])
        assert mask.shape == (96, 128)
        assert mask.sum() == candidate["area"]
        assert rle.bbox(mask) == candidate["bbox"]
    assert formal_data() == before
    assert e.session.objects == {}
    assert list(e.backend.sessions) == [e.session.backend_sid]
    assert not (config.cache_dir() / "candidates" / job["id"]).exists()
    # 会话列表中的任务摘要不携带大体积 RLE。
    assert "candidates" not in e.session.public()["candidate_job"]


def test_preserves_verified_annotations_and_existing_prompt(setup_candidates):
    e = setup_candidates
    obj = e.client.post(f"/api/assignments/{e.aid}/mask-objects", json={"cls": "lamina", "start_ms": 200, "end_ms": 1600}).json()
    response = e.client.post(f"/api/sam/sessions/{e.session.id}/prompt", json={"object_pk": obj["id"], "frame_idx": 16, "points": [[60, 40]], "labels": [1]})
    assert response.status_code == 200, response.text
    assert e.clients["expert"].post(f"/api/mask-objects/{obj['id']}/verify", json={"verified": True}).status_code == 200
    original = e.backend.sessions[e.session.backend_sid]["objs"][1][2].copy()
    before = formal_data()
    assert e.client.post(e.url, json={"frame_idx": 17}).status_code == 200
    assert finish(e)["status"] == "completed"
    assert formal_data() == before
    assert np.array_equal(e.backend.sessions[e.session.backend_sid]["objs"][1][2], original)
    assert e.session.objects == {obj["id"]: 1}


def test_busy_cancel_and_retry(setup_candidates):
    e = setup_candidates
    entered, release = threading.Event(), threading.Event()
    def block():
        entered.set()
        assert release.wait(5)
    e.backend.prompt_hook = block
    try:
        assert e.client.post(e.url, json={"frame_idx": 17}).status_code == 200
        assert entered.wait(5)
        assert e.client.post(e.url, json={"frame_idx": 18}).status_code == 409
        assert e.client.get(e.url).json()["candidates"] == []
        with pytest.raises(RuntimeError):
            service.prompt(e.session, 123, 17, [[30, 30]], [1], None, 1)
        with pytest.raises(RuntimeError):
            service.start_propagation(e.session, 17, "both", 1)
        assert e.client.post(e.url + "/cancel").status_code == 200
    finally:
        release.set()
    assert finish(e)["status"] == "cancelled"
    assert len(e.backend.sessions) == 1
    e.backend.prompt_hook = None
    assert e.client.post(e.url, json={"frame_idx": 18}).status_code == 200
    assert finish(e)["status"] == "completed"


def test_failure_cleans_up_and_can_retry(setup_candidates):
    e = setup_candidates
    def fail():
        raise RuntimeError("test inference failure")
    e.backend.prompt_hook = fail
    before = formal_data()
    assert e.client.post(e.url, json={"frame_idx": 17}).status_code == 200
    job = finish(e)
    assert job["status"] == "error" and "test inference failure" in job["error"]
    assert not job["candidates"] and len(e.backend.sessions) == 1
    assert not (config.cache_dir() / "candidates" / job["id"]).exists()
    assert formal_data() == before
    e.backend.prompt_hook = None
    assert e.client.post(e.url, json={"frame_idx": 17}).status_code == 200
    assert finish(e)["status"] == "completed"


def test_validation_and_permissions(setup_candidates):
    e = setup_candidates
    for frame in (-1, 0, 60, 120):
        assert e.client.post(e.url, json={"frame_idx": frame}).status_code == 400
    e.session.status = "propagating"
    assert e.client.post(e.url, json={"frame_idx": 17}).status_code == 409
    e.session.status = "ready"
    for method in ("get", "post"):
        kwargs = {"json": {"frame_idx": 17}} if method == "post" else {}
        assert getattr(e.clients["other"], method)(e.url, **kwargs).status_code == 403
    assert e.clients["other"].post(e.url + "/cancel").status_code == 403
    assert e.clients["expert"].get(e.url).status_code == 200
    assert e.clients["expert"].post(e.url, json={"frame_idx": 17}).status_code == 403
    with db.tx() as conn:
        conn.execute("UPDATE assignments SET status = 'submitted' WHERE id = ?", (e.aid,))
    assert e.client.post(e.url, json={"frame_idx": 17}).status_code == 403
    assert e.client.get(e.url).status_code == 200


def test_closed_fragment_cancels_and_releases_temporary_session(setup_candidates, monkeypatch):
    e = setup_candidates
    entered, release = threading.Event(), threading.Event()
    extract = service.media.extract_frames
    def block_extract(*args):
        entered.set()
        assert release.wait(5)
        return extract(*args)
    monkeypatch.setattr(service.media, "extract_frames", block_extract)
    before = formal_data()
    try:
        assert e.client.post(e.url, json={"frame_idx": 17}).status_code == 200
        assert entered.wait(5)
        service.close_session(e.session.id)
    finally:
        release.set()
    wait_for(lambda: not e.session.candidates_busy)
    job = e.session.candidate_job
    assert job.status == "cancelled"
    assert e.client.get(e.url).status_code == 404
    assert e.backend.sessions == {}
    assert formal_data() == before
    assert not (config.cache_dir() / "candidates" / job.id).exists()


def test_no_masks_is_a_successful_empty_result(setup_candidates, monkeypatch):
    e = setup_candidates
    monkeypatch.setattr(e.backend, "prompt", lambda *args: np.zeros((96, 128), dtype=bool))
    assert e.client.post(e.url, json={"frame_idx": 17}).status_code == 200
    job = finish(e)
    assert job["status"] == "completed" and job["candidates"] == []
    assert job["progress"] == 1 and job["error"] is None
