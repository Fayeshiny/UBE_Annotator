import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from ssi_annotator.sam.sam3_backend import Sam3Backend


def backend_with_loader(loader):
    requests = []
    predictor = SimpleNamespace(
        _all_inference_states={
            "test-session": {"state": SimpleNamespace()}
        }
    )
    predictor._all_inference_states["test-session"]["state"] = {
        "input_batch": SimpleNamespace(img_batch=SimpleNamespace(tensors=loader))
    }

    def handle_request(*, request):
        requests.append(request["type"])
        return {"session_id": "test-session"}

    predictor.handle_request = handle_request
    backend = Sam3Backend.__new__(Sam3Backend)
    backend.name = "sam3.1"
    backend.predictor = predictor
    backend.size = {}
    return backend, requests


def test_start_waits_for_all_frames(tmp_path):
    release = threading.Event()
    joining = threading.Event()
    worker = threading.Thread(target=release.wait, daemon=True)
    worker.start()

    class LoaderThread:
        def join(self):
            joining.set()
            worker.join()

    loader = SimpleNamespace(thread=LoaderThread(), exception=None)
    backend, requests = backend_with_loader(loader)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(backend.start, tmp_path, 128, 96)
        try:
            assert joining.wait(timeout=2)
            assert not result.done()
        finally:
            release.set()
        assert result.result(timeout=2) == "test-session"
    assert requests == ["start_session"]
    assert backend.size["test-session"] == (128, 96)


def test_start_closes_failed_loading_session(tmp_path):
    failure = RuntimeError("out of memory")
    loader = SimpleNamespace(exception=failure)
    backend, requests = backend_with_loader(loader)
    with pytest.raises(RuntimeError, match="视频帧加载失败") as error:
        backend.start(tmp_path, 128, 96)
    assert error.value.__cause__ is failure
    assert requests == ["start_session", "close_session"]
    assert "test-session" not in backend.size
