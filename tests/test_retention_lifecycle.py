from datetime import timedelta
from pathlib import Path
from threading import Event, Lock
from time import sleep

from fastapi.testclient import TestClient

from level2_service.api import create_app
from level2_service.models import CaptureKind, CaptureStatus, TaskRecord, TaskStatus
from level2_service.queue import InMemoryStreams


def test_job_submission_wakes_runner_before_the_poll_interval() -> None:
    first_call = Event()
    next_call = Event()

    class Runner:
        def __init__(self) -> None:
            self.calls = 0
            self.lock = Lock()

        def run_once(self):
            with self.lock:
                self.calls += 1
                calls = self.calls
            if calls == 1:
                first_call.set()
            else:
                next_call.set()
            return None

    runner = Runner()
    app = create_app(
        runner=runner,
        runner_poll_interval_seconds=30,
    )

    with TestClient(app) as client:
        assert first_call.wait(1)
        assert client.post(
            "/api/v1/jobs",
            json={"symbol": "601872", "include_long_capture": False},
        ).status_code == 202
        assert next_call.wait(1)


def test_app_lifespan_refreshes_an_uninitialized_symbol_catalog() -> None:
    refreshed = Event()

    class Catalog:
        @staticmethod
        def startup_refresh_required() -> bool:
            return True

        @staticmethod
        def refresh() -> None:
            refreshed.set()

    app = create_app(symbol_catalog=Catalog())

    with TestClient(app):
        assert refreshed.wait(1)

    assert app.state.symbol_catalog_task.done()


def test_app_lifespan_prewarms_and_closes_managed_resources() -> None:
    calls: list[str] = []

    class Resource:
        @staticmethod
        def prewarm() -> None:
            calls.append("prewarm")

        @staticmethod
        def close() -> None:
            calls.append("close")

    with TestClient(create_app(managed_resources=(Resource(),))):
        assert calls == ["prewarm"]

    assert calls == ["prewarm", "close"]


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
