"""导入被重启中断时应明确失败；临时转码文件不能替代完整播放文件。"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ssi_annotator import auth, cases, db, media
from ssi_annotator.app import app


def test_startup_marks_interrupted_imports_without_touching_files_or_annotations(data_dir):
    uid = auth.create_user("A", "标注者", "annotator", "password123")
    for cid in ("PENDING", "READY", "FAILED"):
        cases.create_case(cid, {})
    with db.tx() as conn:
        conn.execute("UPDATE cases SET media_status='ready',video_duration_sec=30,frame_count=900 WHERE case_id='READY'")
        conn.execute("UPDATE cases SET media_status='error',media_error='existing failure' WHERE case_id='FAILED'")
        conn.execute("INSERT INTO assignments (case_id,user_id,role,data,created_at,updated_at) VALUES ('PENDING',?,'primary',?,?,?)",
                     (uid, json.dumps({"context": [{"start_ms": 0, "label": "C1"}]}), db.now(), db.now()))
    original = media.case_video_dir("PENDING") / "PENDING_original.mp4"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"retained original")
    before = tuple(db.connect().execute("SELECT * FROM assignments").fetchone())
    with TestClient(app):
        interrupted = cases.get_case("PENDING")
        assert interrupted["media_status"] == "error"
        assert "中断" in interrupted["media_error"]
        assert cases.get_case("READY")["frame_count"] == 900
        assert cases.get_case("READY")["media_status"] == "ready"
        assert cases.get_case("FAILED")["media_error"] == "existing failure"
        assert cases.mark_interrupted_imports() == 0
    assert original.read_bytes() == b"retained original"
    assert tuple(db.connect().execute("SELECT * FROM assignments").fetchone()) == before


def test_failed_transcode_keeps_existing_video_and_removes_partial_file(tmp_path, monkeypatch):
    source, destination = tmp_path / "original.mp4", tmp_path / "web.mp4"
    source.write_bytes(b"original")
    destination.write_bytes(b"previous complete playback file")
    attempted = []
    def interrupted(cmd):
        output = Path(cmd[-1])
        attempted.append(output)
        assert output != destination and output.suffix == ".mp4"
        output.write_bytes(b"partial")
        raise media.MediaError("ffmpeg interrupted")
    monkeypatch.setattr(media, "_run", interrupted)
    with pytest.raises(media.MediaError, match="interrupted"):
        media.transcode_for_web(source, destination)
    assert destination.read_bytes() == b"previous complete playback file"
    assert source.read_bytes() == b"original"
    assert attempted and not attempted[0].exists()


@pytest.mark.parametrize("status,reason", [("processing", "视频仍在处理"), ("error", "视频处理失败：test import error")])
def test_video_endpoint_reports_actual_import_state(data_dir, status, reason):
    uid = auth.create_user("admin", "管理员", "admin", "password123")
    cases.create_case("C", {})
    with db.tx() as conn:
        conn.execute("UPDATE cases SET media_status=?,media_error=? WHERE case_id='C'", (status, "test import error"))
    client = TestClient(app)
    assert client.post("/api/auth/login", json={"username": "admin", "password": "password123"}).status_code == 200
    response = client.get("/api/cases/C/video")
    assert response.status_code == 409
    assert reason in response.json()["detail"]
