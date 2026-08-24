from datetime import timedelta
from pathlib import Path
from time import sleep

from fastapi.testclient import TestClient

from level2_service.api import create_app
from level2_service.models import CaptureKind, CaptureStatus, TaskRecord, TaskStatus
from level2_service.queue import InMemoryStreams


def test_app_lifespan_runs_retention_and_stops_its_background_task(tmp_path: Path) -> None:
    """Screenshot retention must run without expiring the persistent task result."""
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
        assert task.status == TaskStatus.PARTIAL
        assert store.get(public_id) is task

    assert app.state.cleanup_task.done()


def test_app_startup_recovers_a_task_claimed_by_the_previous_process() -> None:
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="interrupted", symbol="601872"))
    assert store.next_queued().status == TaskStatus.RUNNING

    with TestClient(create_app(store=store)):
        assert store.get("interrupted").status == TaskStatus.QUEUED

    assert [event["data"] for event in store.events_after("interrupted")] == [
        "QUEUED", "RUNNING", "QUEUED",
    ]


def test_app_startup_deduplicates_tasks_before_background_work_begins() -> None:
    store = InMemoryStreams()
    older = TaskRecord(task_id="older", symbol="600938")
    older.created_at = older.created_at.replace(year=2025)
    older.updated_at = older.created_at
    store.enqueue(older)
    store.enqueue(TaskRecord(task_id="newer", symbol="600938"))
    app = create_app(store=store)

    with TestClient(app):
        assert app.state.task_migration == {"total": 2, "kept": 1, "deleted": 1, "aliases": 1}
        assert store.resolve_task_id("older") == "newer"
