from datetime import timedelta

import pytest

from level2_service.models import CaptureKind, CaptureStatus, TaskRecord, TaskStatus
from level2_service.queue import InMemoryStreams, QueueFullError


def test_queue_returns_oldest_queued_task_first() -> None:
    """Iterating newest-first would starve older public requests."""
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="first", symbol="600938"))
    store.enqueue(TaskRecord(task_id="second", symbol="000001"))

    assert store.next_queued().task_id == "first"


def test_queue_rejects_invalid_state_transition() -> None:
    """Allowing QUEUED directly to COMPLETED would publish unverified results."""
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="task", symbol="600938"))

    try:
        store.transition("task", TaskStatus.COMPLETED)
    except ValueError as error:
        assert str(error) == "QUEUED cannot transition to COMPLETED"
    else:
        raise AssertionError("invalid transition was accepted")


def test_one_finished_capture_makes_task_partial_until_all_three_are_ready() -> None:
    """Marking a task successful after one screenshot would hide missing Level2 views."""
    store = InMemoryStreams()
    task = TaskRecord(task_id="task", symbol="600938")
    store.enqueue(task)
    store.transition("task", TaskStatus.RUNNING)

    partial = store.complete_capture("task", CaptureKind.LARGE_ORDER_NET, "/tmp/net.png")
    assert partial.status == TaskStatus.PARTIAL
    assert partial.captures[CaptureKind.LARGE_ORDER_NET].status == CaptureStatus.READY
    assert partial.captures[CaptureKind.RETAIL_COUNT].status == CaptureStatus.PENDING

    store.complete_capture("task", CaptureKind.LARGE_ORDER_AMOUNT, "/tmp/amount.png")
    complete = store.complete_capture("task", CaptureKind.RETAIL_COUNT, "/tmp/retail.png")
    assert complete.status.value == "COMPLETED"


def test_retention_expires_captures_after_24_hours_then_removes_metadata_after_7_days() -> None:
    """Leaking a capture beyond its retention window exposes market screenshots too long."""
    store = InMemoryStreams()
    task = TaskRecord(task_id="task", symbol="600938")
    store.enqueue(task)
    store.transition("task", TaskStatus.RUNNING)
    store.complete_capture("task", CaptureKind.LARGE_ORDER_NET, "/tmp/net.png")

    task.captures[CaptureKind.LARGE_ORDER_NET].captured_at = task.created_at + timedelta(hours=23)
    assert store.cleanup(task.created_at + timedelta(hours=25, seconds=1)) == []
    assert task.captures[CaptureKind.LARGE_ORDER_NET].status == CaptureStatus.READY
    assert task.status.value != "EXPIRED"
    assert store.cleanup(task.created_at + timedelta(hours=47, seconds=1)) == []
    assert task.captures[CaptureKind.LARGE_ORDER_NET].status == CaptureStatus.EXPIRED
    assert task.status.value == "EXPIRED"
    assert store.cleanup(task.created_at + timedelta(days=7, seconds=1)) == [task]
    assert store.get("task") is None


def test_queue_cap_rejects_a_new_pending_task() -> None:
    """Ignoring the global cap would allow unbounded single-runner backlog growth."""
    store = InMemoryStreams(pending_cap=1)
    store.enqueue(TaskRecord(task_id="first", symbol="600938"))

    with pytest.raises(QueueFullError, match="global pending queue cap reached"):
        store.enqueue(TaskRecord(task_id="second", symbol="000001"))


def test_capture_completion_requires_the_runner_to_claim_the_task() -> None:
    """Completing a QUEUED task would let a stale worker bypass the FIFO claim."""
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="task", symbol="600938"))

    with pytest.raises(ValueError, match="QUEUED cannot accept a capture"):
        store.complete_capture("task", CaptureKind.LARGE_ORDER_NET, "/tmp/net.png")


def test_next_queued_atomically_claims_the_oldest_job() -> None:
    """Returning a queued job twice lets concurrent runners capture the same request."""
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="first", symbol="600938"))
    store.enqueue(TaskRecord(task_id="second", symbol="000001"))

    first = store.next_queued()
    second = store.next_queued()

    assert first.task_id == "first"
    assert first.status == TaskStatus.RUNNING
    assert second.task_id == "second"
