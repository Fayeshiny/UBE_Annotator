"""Authenticated whole-video caching metadata and byte-range transport."""

import pytest
from fastapi.testclient import TestClient

from ssi_annotator import auth, cases, db, frame_cache, media
from ssi_annotator.app import app


@pytest.fixture()
def video_case(data_dir):
    cases.create_case("VIDEO_CACHE", {})
    directory = media.case_video_dir("VIDEO_CACHE")
    directory.mkdir(parents=True)
    content = bytes(range(256)) * 16
    video = directory / "video.mp4"
    video.write_bytes(content)
    (directory / "frame_timestamp_map.csv").write_text("frame_idx,t_ms,pts_sec\n0,0,0\n")
    with db.tx() as conn:
        conn.execute(
            "UPDATE cases SET media_status = 'ready', video_file = ?, frame_count = 1 "
            "WHERE case_id = 'VIDEO_CACHE'", (video.name,),
        )
    auth.create_user("admin", "Admin", "admin", "password123")
    assigned = auth.create_user("assigned", "Assigned", "annotator", "password123")
    auth.create_user("other", "Other", "annotator", "password123")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO assignments (case_id, user_id, role, data, created_at, updated_at) "
            "VALUES ('VIDEO_CACHE', ?, 'primary', '{}', ?, ?)",
            (assigned, db.now(), db.now()),
        )
    return {"path": video, "content": content,
            "revision": frame_cache.revision(cases.get_case("VIDEO_CACHE"))}


def _client(username="admin"):
    client = TestClient(app)
    result = client.post("/api/auth/login", json={"username": username, "password": "password123"})
    assert result.status_code == 200
    return client


@pytest.mark.parametrize("method", ["get", "head"])
def test_video_metadata_and_transport_without_reading_frame_map(video_case, monkeypatch, method):
    def no_frame_map(*_args, **_kwargs):
        pytest.fail("Video metadata/transport must not load all frame timestamps")

    monkeypatch.setattr(media, "frame_pts", no_frame_map)
    monkeypatch.setattr(media, "frame_times", no_frame_map)
    client = _client("assigned")
    response = getattr(client, method)(f"/api/cases/VIDEO_CACHE/video?revision={video_case['revision']}")
    assert response.status_code == 200
    assert response.content == (video_case["content"] if method == "get" else b"")
    assert response.headers["content-length"] == str(len(video_case["content"]))
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["etag"] == f'"{video_case["revision"]}"'
    assert response.headers["x-media-revision"] == video_case["revision"]
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["vary"] == "Cookie"


def test_video_range_and_if_range(video_case):
    client = _client()
    url = f"/api/cases/VIDEO_CACHE/video?revision={video_case['revision']}"
    headers = {"Range": "bytes=100-199", "If-Range": f'"{video_case["revision"]}"'}
    response = client.get(url, headers=headers)
    assert response.status_code == 206
    assert response.content == video_case["content"][100:200]
    assert response.headers["content-range"] == f"bytes 100-199/{len(video_case['content'])}"
    assert response.headers["content-length"] == "100"
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["vary"] == "Cookie"
    assert response.headers["x-media-revision"] == video_case["revision"]
    head = client.head(url, headers=headers)
    assert head.status_code == 206 and head.content == b""
    assert head.headers["content-range"] == response.headers["content-range"]
    assert head.headers["content-length"] == "100"
    # An old If-Range validator must never produce a slice of a new source.
    response = client.get(url, headers={"Range": "bytes=100-199", "If-Range": '"old-version"'})
    assert response.status_code == 200 and response.content == video_case["content"]


@pytest.mark.parametrize("method", ["get", "head"])
def test_video_authorization_precedes_revision_and_range(video_case, method):
    url = "/api/cases/VIDEO_CACHE/video?revision=stale"
    headers = {"Range": "bytes=0-0"}
    assert getattr(TestClient(app), method)(url, headers=headers).status_code == 401
    assert getattr(_client("other"), method)(url, headers=headers).status_code == 403
    assert getattr(_client("assigned"), method)(url, headers=headers).status_code == 409
    assert getattr(_client(), method)("/api/cases/missing/video").status_code == 404


@pytest.mark.parametrize("method", ["get", "head"])
@pytest.mark.parametrize("state", ["processing", "error"])
def test_video_not_ready_is_conflict(video_case, method, state):
    with db.tx() as conn:
        conn.execute("UPDATE cases SET media_status = ? WHERE case_id = 'VIDEO_CACHE'", (state,))
    assert getattr(_client(), method)("/api/cases/VIDEO_CACHE/video").status_code == 409


@pytest.mark.parametrize("missing", ["video", "map"])
def test_video_missing_source_is_conflict(video_case, missing):
    path = video_case["path"] if missing == "video" else video_case["path"].parent / "frame_timestamp_map.csv"
    path.unlink()
    client = _client()
    for method in (client.get, client.head):
        assert method("/api/cases/VIDEO_CACHE/video").status_code == 409


def test_changed_video_invalidates_pinned_revision(video_case):
    video_case["path"].write_bytes(video_case["content"] + b"changed")
    client = _client()
    url = f"/api/cases/VIDEO_CACHE/video?revision={video_case['revision']}"
    assert client.get(url, headers={"Range": "bytes=0-0"}).status_code == 409
    assert client.head(url).status_code == 409
    current = client.head("/api/cases/VIDEO_CACHE/video")
    assert current.status_code == 200
    assert current.headers["x-media-revision"] != video_case["revision"]
    assert current.headers["content-length"] == str(len(video_case["content"]) + 7)
