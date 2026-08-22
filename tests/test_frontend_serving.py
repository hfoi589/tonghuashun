from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from level2_service.api import create_app


@pytest.fixture
def frontend_root(tmp_path: Path) -> Path:
    root = tmp_path / "frontend"
    assets = root / "assets"
    assets.mkdir(parents=True)
    (root / "index.html").write_text(
        "<!doctype html><html><body>current Level2 frontend</body></html>",
        encoding="utf-8",
    )
    (assets / "app.js").write_text("window.__LEVEL2_APP__ = true;", encoding="utf-8")
    return root


def test_frontend_root_and_assets_are_served_by_fastapi(frontend_root: Path) -> None:
    client = TestClient(create_app(frontend_root=frontend_root))

    root_response = client.get("/")
    asset_response = client.get("/assets/app.js")

    assert root_response.status_code == 200
    assert root_response.headers["content-type"].startswith("text/html")
    assert "current Level2 frontend" in root_response.text
    assert asset_response.status_code == 200
    assert asset_response.text == "window.__LEVEL2_APP__ = true;"


def test_frontend_browser_routes_fall_back_to_index_html(frontend_root: Path) -> None:
    client = TestClient(create_app(frontend_root=frontend_root))

    response = client.get("/portfolio/601872")
    head_response = client.head("/portfolio/601872")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "current Level2 frontend" in response.text
    assert head_response.status_code == 200
    assert head_response.content == b""


@pytest.mark.parametrize("path", ["/api", "/api/not-a-route"])
def test_frontend_fallback_never_rewrites_unknown_api_routes(frontend_root: Path, path: str) -> None:
    client = TestClient(create_app(frontend_root=frontend_root))

    response = client.get(path)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Not Found"}
