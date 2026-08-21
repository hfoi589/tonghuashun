from pathlib import Path
from datetime import timedelta

from fastapi.testclient import TestClient

from level2_service.api import create_app
from level2_service.models import CaptureKind, TaskStatus
from level2_service.queue import InMemoryStreams


def test_ready_capture_is_served_only_from_the_configured_capture_root(tmp_path: Path) -> None:
    """Serving an arbitrary recorded path would expose files outside capture retention."""
    capture = tmp_path / "net.png"
    capture.write_bytes(b"verified-png-bytes")
    store = InMemoryStreams()
    client = TestClient(create_app(store=store, capture_root=tmp_path))
    public_id = client.post("/api/v1/jobs", json={"symbol": "600938"}).json()["public_id"]
    store.transition(public_id, TaskStatus.RUNNING)
    store.complete_capture(public_id, CaptureKind.LARGE_ORDER_NET, str(capture))

    response = client.get(f"/api/v1/jobs/{public_id}/captures/LARGE_ORDER_NET")

    assert response.status_code == 200
    assert response.content == b"verified-png-bytes"


def test_status_events_are_exposed_as_sse_envelopes() -> None:
    """Returning plain JSON here would leave the public result page unable to subscribe."""
    store = InMemoryStreams()
    client = TestClient(create_app(store=store))
    public_id = client.post("/api/v1/jobs", json={"symbol": "600938"}).json()["public_id"]

    response = client.get(f"/api/v1/jobs/{public_id}/events?once=true")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: status\ndata:" in response.text


def test_sse_cursor_does_not_replay_events_the_client_has_already_seen() -> None:
    """Ignoring an SSE cursor would repeatedly render stale task state after reconnects."""
    store = InMemoryStreams()
    client = TestClient(create_app(store=store))
    public_id = client.post("/api/v1/jobs", json={"symbol": "600938"}).json()["public_id"]

    response = client.get(f"/api/v1/jobs/{public_id}/events?once=true&after=1")

    assert response.status_code == 200
    assert response.text == ""


def test_retention_deletes_a_ready_capture_and_returns_gone(tmp_path: Path) -> None:
    """Only hiding an expired file from the route would leave retained screenshots on disk."""
    capture = tmp_path / "net.png"
    capture.write_bytes(b"verified-png-bytes")
    store = InMemoryStreams()
    client = TestClient(create_app(store=store, capture_root=tmp_path))
    public_id = client.post("/api/v1/jobs", json={"symbol": "600938"}).json()["public_id"]
    task = store.get(public_id)
    store.transition(public_id, TaskStatus.RUNNING)
    store.complete_capture(public_id, CaptureKind.LARGE_ORDER_NET, str(capture))

    store.cleanup(task.created_at + timedelta(hours=24, seconds=1))

    assert not capture.exists()
    assert client.get(f"/api/v1/jobs/{public_id}/captures/LARGE_ORDER_NET").status_code == 410


def test_retention_never_deletes_a_capture_path_outside_its_configured_root(tmp_path: Path) -> None:
    """A compromised runner record must not turn retention into arbitrary file deletion."""
    outside = tmp_path.parent / "must-not-delete.png"
    outside.write_bytes(b"not-a-capture")
    store = InMemoryStreams()
    client = TestClient(create_app(store=store, capture_root=tmp_path))
    public_id = client.post("/api/v1/jobs", json={"symbol": "600938"}).json()["public_id"]
    task = store.get(public_id)
    store.transition(public_id, TaskStatus.RUNNING)
    store.complete_capture(public_id, CaptureKind.LARGE_ORDER_NET, str(outside))

    store.cleanup(task.created_at + timedelta(hours=24, seconds=1))

    assert outside.exists()
