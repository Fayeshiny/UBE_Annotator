import csv
import io
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ssi_annotator import auth, cases, db, frame_cache, media
from ssi_annotator.app import app


@pytest.fixture()
def ready_case(data_dir, numbered_video, monkeypatch):
    monkeypatch.setenv("SSI_FRAME_CACHE_PREWARM", "0")
    cases.create_case("FRAMES", {})
    cases.process_video("FRAMES", numbered_video, transcode=False)
    case = cases.get_case("FRAMES")
    assert case["media_status"] == "ready"
    yield case
    frame_cache.close_all()


def _login(client, user):
    assert client.post("/api/auth/login", json={"username": user, "password": "password123"}).status_code == 200


def test_frame_api_authorization_validation_and_etag(ready_case):
    auth.create_user("admin", "Admin", "admin", "password123")
    aid = auth.create_user("A01", "Annotator", "annotator", "password123")
    client = TestClient(app)
    url = "/api/cases/FRAMES/frame/47.jpg"
    assert client.get(url).status_code == 401
    _login(client, "A01")
    assert client.get(url).status_code == 403
    assert client.post("/api/cases/FRAMES/frame-cache/warm", json={"sampling_interval_ms": 1000}).status_code == 403
    client.cookies.clear()
    _login(client, "admin")
    assert client.get("/api/cases/missing/frame/0.jpg").status_code == 404
    assert client.get("/api/cases/FRAMES/frame/-1.jpg").status_code == 404
    assert client.get("/api/cases/FRAMES/frame/120.jpg").status_code == 404
    assert client.get("/api/cases/FRAMES/frame/nope.jpg").status_code == 422
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-frame-cache"] == "MISS"
    assert response.headers["cache-control"] == "private, no-cache"
    assert Image.open(io.BytesIO(response.content)).size == (128, 96)
    assert client.get(url).headers["x-frame-cache"] == "HIT"
    assert client.get(url, headers={"If-None-Match": response.headers["etag"]}).status_code == 304
    assert client.get(url + "?revision=stale").status_code == 409
    assignment = client.post("/api/cases/FRAMES/assignments", json={"user_id": aid}).json()
    assert client.get(f"/api/assignments/{assignment['id']}").json()["case"]["media_revision"] == ready_case["media_revision"]
    client.cookies.clear()
    _login(client, "A01")
    assert client.get(url).status_code == 200
    client.cookies.clear()
    # Authorization always precedes conditional cache validation.
    assert client.get(url, headers={"If-None-Match": response.headers["etag"]}).status_code == 401
    _login(client, "admin")
    for state in ("processing", "error"):
        with db.tx() as conn:
            conn.execute("UPDATE cases SET media_status = ? WHERE case_id = 'FRAMES'", (state,))
        assert client.get(url).status_code == 409


def test_source_revision_and_frame_map_invalidation(ready_case):
    src = frame_cache.source(ready_case)
    cache = frame_cache.manager()
    assert cache.get(src, 40)[1] is False
    assert cache.get(src, 40)[1] is True
    stat = src.map_path.stat()
    text = src.map_path.read_text()
    changed = text.replace("40,1333,1.333333", "40,1334,1.333334")
    assert changed != text
    replacement = src.map_path.with_suffix(".replacement")
    replacement.write_text(changed)
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    replacement.replace(src.map_path)
    refreshed = frame_cache.source(cases.get_case("FRAMES"))
    assert refreshed.revision != src.revision
    assert refreshed.times[40] == 1334
    assert refreshed.pts[40] == 1.333334
    with pytest.raises(frame_cache.SourceChanged):
        cache.get(src, 40)
    assert cache.get(refreshed, 40)[1] is False
    os.utime(src.video, ns=(src.video.stat().st_atime_ns, src.video.stat().st_mtime_ns + 1))
    assert frame_cache.revision(ready_case) != refreshed.revision


def test_concurrent_batch_dedup_and_disk_lru(ready_case, tmp_path, monkeypatch):
    src = frame_cache.source(ready_case)
    original = media.extract_frame_batch
    calls = []

    def counted(*args, **kwargs):
        calls.append(args[2:4])
        time.sleep(0.03)
        return original(*args, **kwargs)

    monkeypatch.setattr(media, "extract_frame_batch", counted)
    cache = frame_cache.FrameCache(tmp_path / "cache", max_bytes=1_000_000)
    with ThreadPoolExecutor(max_workers=5) as pool:
        images = list(pool.map(lambda i: cache.get(src, i), (45, 46, 47, 48, 49)))
    assert calls == [(45, 49)]
    assert sum(not hit for _, hit in images) == 1
    assert len(images) == 5
    cache.max_bytes = len(images[0][0]) * 2
    cache.get(src, 80)
    assert cache.total_bytes <= cache.max_bytes
    assert sum(p.stat().st_size for p in cache.root.glob("*/*.jpg")) <= cache.max_bytes
    # The bytes remain valid even if eviction removed the requested disk entry.
    tiny = frame_cache.FrameCache(tmp_path / "tiny", max_bytes=1)
    image, hit = tiny.get(src, 47)
    assert Image.open(io.BytesIO(image)).size == (128, 96)
    assert not hit and tiny.total_bytes == 0
    assert not list(tiny.root.glob("*/*.jpg"))


def test_background_priority_and_cancelled_waiter(ready_case, tmp_path):
    cache = frame_cache.FrameCache(tmp_path / "cache")
    src = frame_cache.source(ready_case)
    cache.foreground_waiting = 1
    with pytest.raises(frame_cache.Busy):
        cache.get(src, 40, prefetch=True)
    cache.foreground_waiting = 0
    cache.background_active = 1
    with pytest.raises(frame_cache.Busy):
        cache.get(src, 40, prefetch=True)
    cache.background_active = 0
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(frame_cache.Busy):
        cache.get(src, 40, cancelled=cancelled)
    assert not cache.inflight and cache.active == 0
    cache.foreground_waiting = 16
    with pytest.raises(frame_cache.Busy):
        cache.get(src, 40)
    cache.foreground_waiting = 0


def test_sparse_prewarm_dedup_rewarms_evicted_samples(ready_case, tmp_path):
    cache = frame_cache.FrameCache(tmp_path / "cache")
    src = frame_cache.source(ready_case)
    assert cache.request_warm(src, 1000)["status"] in ("queued", "running")
    deadline = time.monotonic() + 5
    while cache.warm_states[(src.revision, 1000)]["status"] != "ready":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    result = cache.request_warm(src, 1000)
    assert result["status"] == "ready" and result["prepared"] == 4
    assert {int(p.stem) for p in cache.root.glob("*/*.jpg")} == {0, 30, 60, 90}
    (cache.root / src.revision / "000000000.jpg").unlink()
    assert cache.request_warm(src, 1000)["status"] in ("queued", "running")
    cache.close()


def test_busy_api_has_retry_after_and_warm_validation(ready_case):
    auth.create_user("admin", "Admin", "admin", "password123")
    client = TestClient(app)
    _login(client, "admin")
    cache = frame_cache.manager()
    cache.foreground_waiting = 1
    response = client.get("/api/cases/FRAMES/frame/50.jpg?prefetch=true")
    assert response.status_code == 429 and response.headers["retry-after"] == "1"
    cache.foreground_waiting = 16
    response = client.get("/api/cases/FRAMES/frame/50.jpg")
    assert response.status_code == 503 and response.headers["retry-after"] == "1"
    cache.foreground_waiting = 0
    assert client.post("/api/cases/FRAMES/frame-cache/warm", json={"sampling_interval_ms": 999}).status_code == 400
    response = client.post("/api/cases/FRAMES/frame-cache/warm", json={"sampling_interval_ms": 1000})
    assert response.status_code == 200 and response.json()["status"] in ("queued", "running")


@pytest.mark.parametrize("mode", ["cfr", "ntsc", "vfr", "offset"])
def test_exact_frames_match_sequential_decode(tmp_path, numbered_video, mode):
    video = tmp_path / "source.mp4"
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(numbered_video)]
    if mode == "ntsc":
        cmd += ["-vf", "setpts=N*1001/(30000*TB)", "-enc_time_base", "1:30000"]
    elif mode == "vfr":
        cmd += ["-vf", "setpts='if(lt(N,40),N/(30*TB),(40/30+(N-40)/12)/TB)'", "-enc_time_base", "1:30000"]
    elif mode == "offset":
        cmd += ["-output_ts_offset", "2"]
    cmd += ["-fps_mode", "passthrough", "-c:v", "libx264", "-g", "15", "-crf", "0", str(video)]
    subprocess.run(cmd, check=True)
    map_path = tmp_path / "frame_timestamp_map.csv"
    times = tuple(media.build_frame_map(video, map_path))
    with map_path.open() as f:
        pts = tuple(float(row["pts_sec"]) for row in csv.DictReader(f))
    assert len(pts) == 120
    if mode == "offset":
        assert pts[0] >= 2
    if mode == "vfr":
        assert pts[41] - pts[40] > 2 * (pts[1] - pts[0])
    if mode == "ntsc":
        assert abs((pts[90] - pts[0]) - 3.003) < 0.001
    reference = tmp_path / "reference"
    reference.mkdir()
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-threads", "2", "-filter_threads", "1",
        "-noautorotate", "-i", str(video), "-fps_mode", "passthrough", "-c:v", "mjpeg",
        "-threads", "1", "-q:v", "2", "-start_number", "0", str(reference / "%05d.jpg"),
    ], check=True)
    src = frame_cache.Source("test", video, map_path, frame_cache._revision(video, map_path), pts, times)
    cache = frame_cache.FrameCache(tmp_path / "cache")
    for idx in (0, 1, 14, 15, 39, 40, 41, 59, 98, 119):
        image, _ = cache.get(src, idx)
        actual = np.asarray(Image.open(io.BytesIO(image)))
        expected = np.asarray(Image.open(reference / f"{idx:05d}.jpg"))
        assert np.array_equal(actual, expected), (mode, idx, actual.mean(), expected.mean())
    # SAM's stride extraction uses exactly the same PTS alignment as the viewer.
    output = tmp_path / "sam"
    indices = media.extract_frames(video, times, 39, 70, 5, output)
    assert indices == list(range(39, 71, 5))
    for n, idx in enumerate(indices):
        assert np.array_equal(np.asarray(Image.open(output / f"{n:05d}.jpg")),
                              np.asarray(Image.open(reference / f"{idx:05d}.jpg")))


def test_offline_warm_cli_refuses_live_service(ready_case, data_dir, capsys):
    import fcntl
    from ssi_annotator import cli

    with (data_dir / ".server.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit) as error:
            cli.main(["warm-frame-cache", "FRAMES"])
        assert error.value.code == 1
        assert "正式服务正在运行" in capsys.readouterr().err
    cli.main(["warm-frame-cache", "FRAMES", "--interval-ms", "1000"])
    assert "ready, 4/4" in capsys.readouterr().out


def test_cancelled_queued_request_never_decodes(ready_case, tmp_path, monkeypatch):
    cache = frame_cache.FrameCache(tmp_path / "cache")
    src = frame_cache.source(ready_case)
    cancelled = threading.Event()
    monkeypatch.setattr(media, "extract_frame_batch", lambda *a, **kw: pytest.fail("cancelled request decoded"))
    with cache.condition:
        cache.active = 2
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(cache.get, src, 40, cancelled=cancelled)
        deadline = time.monotonic() + 2
        while not cache.foreground_waiting:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        cancelled.set()
        with cache.condition:
            cache.active = 0
            cache.condition.notify_all()
        with pytest.raises(frame_cache.Busy):
            waiting.result(timeout=2)
    assert not cache.inflight and cache.foreground_waiting == 0
