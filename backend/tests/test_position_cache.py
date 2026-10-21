import io
import json
import struct

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from ssi_annotator import auth, cases, media, frame_cache
from ssi_annotator.app import app


@pytest.fixture()
def ready(data_dir, numbered_video):
    cases.create_case("POSITION", {})
    cases.process_video("POSITION", numbered_video, transcode=False)
    auth.create_user("admin", "Admin", "admin", "password123")
    auth.create_user("other", "Other", "annotator", "password123")
    client = TestClient(app)
    assert client.post("/api/auth/login", json={"username": "admin", "password": "password123"}).status_code == 200
    yield client, cases.get_case("POSITION")
    frame_cache.close_all()


def test_video_index_real_packet_offsets_and_authorization(ready):
    c, case = ready
    url = f"/api/cases/POSITION/video-index?revision={case['media_revision']}"
    assert TestClient(app).get(url).status_code == 401
    r = c.get(url)
    assert r.status_code == 200, r.text
    index = r.json()
    assert index['revision'] == case['media_revision']
    assert index['ranges'][0][0] == 0
    assert max(g[1] for g in index['ranges']) == 119
    size = (media.case_video_dir('POSITION') / case['video_file']).stat().st_size
    assert all(0 <= a <= b < 120 and 0 <= start <= end < size for a,b,start,end in index['ranges'])
    assert c.get('/api/cases/POSITION/video-index?revision=stale').status_code == 409
    c.post('/api/auth/login', json={'username':'other','password':'password123'})
    assert c.get(url).status_code == 403


def test_frame_chunk_exact_payloads_and_bounds(ready):
    c, case = ready
    url = f"/api/cases/POSITION/frame-chunk?start_frame=45&count=5&exclude_frame=47&revision={case['media_revision']}"
    assert TestClient(app).get(url).status_code == 401
    r = c.get(url)
    assert r.status_code == 200, r.text
    n = struct.unpack('>I', r.content[:4])[0]
    meta = json.loads(r.content[4:4+n]);offset=4+n
    assert [f for f,_ in meta['frames']] == [48,46,49,45]
    assert meta['revision'] == case['media_revision']
    for frame, size in meta['frames']:
        jpeg=r.content[offset:offset+size];offset+=size
        assert jpeg == c.get(f'/api/cases/POSITION/frame/{frame}.jpg').content
        assert Image.open(io.BytesIO(jpeg)).size == (128,96)
    assert offset == len(r.content)
    for suffix,status in [('start_frame=118&count=5',200),('start_frame=-1',404),('start_frame=120',404),('start_frame=0&count=6',422),('start_frame=0&revision=stale',409)]:
        assert c.get('/api/cases/POSITION/frame-chunk?'+suffix).status_code == status
    c.post('/api/auth/login',json={'username':'other','password':'password123'})
    assert c.get(url).status_code == 403
