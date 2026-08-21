from datetime import timedelta
from pathlib import Path
from time import sleep

from fastapi.testclient import TestClient

from level2_service.api import create_app
from level2_service.models import CaptureKind, CaptureStatus, TaskStatus
from level2_service.queue import InMemoryStreams


def test_app_lifespan_runs_retention_and_stops_its_background_task(tmp_path: Path) -> None:
    """Retention must not rely on a runner or undocumented external scheduler call."""
    capture = tmp_path / "net.png"
    capture.write_bytes(b"screenshot")
    store = InMemoryStreams()
    app = create_app(store=store, capture_root=tmp_path, cleanup_interval_seconds=0.01)

    with TestClient(app) as client:
        public_id = client.post("/api/v1/jobs", json={"symbol": "600938"}).json()["public_id"]
        task = store.get(public_id)
        store.transition(public_id, TaskStatus.RUNNING)
        store.complete_capture(public_id, CaptureKind.LARGE_ORDER_NET, str(capture))
        task.captures[CaptureKind.LARGE_ORDER_NET].captured_at -= timedelta(hours=25)
        sleep(0.05)
        assert task.captures[CaptureKind.LARGE_ORDER_NET].status == CaptureStatus.EXPIRED
        assert task.status == TaskStatus.EXPIRED

    assert app.state.cleanup_task.done()
