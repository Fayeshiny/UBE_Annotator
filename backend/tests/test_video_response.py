import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from ssi_annotator.video_response import PlaybackFileResponse,PLAYBACK_RANGE_BYTES as BLOCK


@pytest.fixture()
def client(tmp_path):
    file=tmp_path/'video.mp4';file.write_bytes(bytes(range(256))*(BLOCK*3//256))
    async def endpoint(request):return PlaybackFileResponse(file,media_type='video/mp4',headers={'ETag':'"revision"'})
    return TestClient(Starlette(routes=[Route('/video',endpoint,methods=['GET','HEAD'])])),file


def test_open_ended_range_is_bounded_and_next_ranges_reconstruct_source(client):
    c,file=client;parts=[]
    for start in range(0,file.stat().st_size,BLOCK):
        r=c.get('/video',headers={'Range':f'bytes={start}-'})
        assert r.status_code==206
        assert len(r.content)==BLOCK
        assert r.headers['content-range']==f'bytes {start}-{start+BLOCK-1}/{file.stat().st_size}'
        parts.append(r.content)
    assert b''.join(parts)==file.read_bytes()


def test_small_explicit_ranges_head_suffix_and_invalid_range(client):
    c,file=client
    r=c.get('/video',headers={'Range':'bytes=42-99'})
    assert r.content==file.read_bytes()[42:100]
    r=c.head('/video',headers={'Range':'bytes=0-'})
    assert r.status_code==206 and int(r.headers['content-length'])==BLOCK and r.content==b''
    assert c.get('/video',headers={'Range':'bytes=-16'}).content==file.read_bytes()[-16:]
    assert c.get('/video',headers={'Range':f'bytes={file.stat().st_size}-'}).status_code==416
