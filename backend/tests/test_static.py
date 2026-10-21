import pytest
from fastapi.testclient import TestClient

from ssi_annotator import app as application


@pytest.fixture
def static_client(tmp_path, monkeypatch):
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<html>app shell</html>")
    (tmp_path / "private.txt").write_text("outside the published directory")
    (static / "escape.txt").symlink_to(tmp_path / "private.txt")
    monkeypatch.setattr(application, "_static", static)
    return TestClient(application.app)


@pytest.mark.parametrize("url", [
    "/%2e%2e/private.txt",
    "/%2e%2e%2fprivate.txt",
    "/nested/%2e%2e/%2e%2e/private.txt",
    "/escape.txt",
])
def test_static_files_cannot_escape_build_directory(static_client, url):
    response = static_client.get(url)
    assert response.status_code == 404
    assert "outside the published directory" not in response.text


def test_spa_routes_and_public_files_still_work(static_client):
    for url in ("/", "/index.html", "/workspace/123"):
        response = static_client.get(url)
        assert response.status_code == 200
        assert response.text == "<html>app shell</html>"
    assert static_client.get("/api/missing-endpoint").status_code == 404


def test_api_only_install_returns_404_for_frontend(monkeypatch):
    monkeypatch.setattr(application, "_static", None)
    assert TestClient(application.app).get("/").status_code == 404
