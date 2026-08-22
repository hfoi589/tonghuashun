from datetime import timedelta
from pathlib import Path

import pytest

from level2_service.models import CaptureKind, CaptureStatus, MetricKind, TaskRecord, TaskStatus
from level2_service.queue import InMemoryStreams, QueueFullError


FULL_VALUES = {
    MetricKind.STOCK_NAME: "招商轮船",
    MetricKind.CURRENT_PRICE: "19.78",
    MetricKind.CHANGE_PERCENT: "7.15%",
    MetricKind.TURNOVER_RATE: "2.40%",
    MetricKind.LARGE_ORDER_NET: "-0.02",
    MetricKind.LARGE_ORDER_AMOUNT: "-2802.6万",
    MetricKind.RETAIL_COUNT: "21.23",
    MetricKind.MACDFS: "+0.012",
}


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


def test_terminal_partial_long_result_does_not_consume_queue_capacity() -> None:
    """A completed capture with one unreadable value must not block later FIFO work forever."""
    store = InMemoryStreams(pending_cap=1)
    store.enqueue(TaskRecord(task_id="partial", symbol="601872"))
    store.next_queued()
    store.complete_result(
        "partial",
        FULL_VALUES | {MetricKind.LARGE_ORDER_AMOUNT: None},
        "/tmp/LONG.png",
    )

    store.enqueue(TaskRecord(task_id="next", symbol="000001"))

    assert store.queue_position("next") == 1


def test_capture_completion_requires_the_runner_to_claim_the_task() -> None:
    """Completing a QUEUED task would let a stale worker bypass the FIFO claim."""
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="task", symbol="600938"))

    with pytest.raises(ValueError, match="QUEUED cannot accept a capture"):
        store.complete_capture("task", CaptureKind.LARGE_ORDER_NET, "/tmp/net.png")


def test_long_result_completion_stores_one_image_and_eight_values() -> None:
    """Publishing only legacy per-chart files would leave the current snapshot incomplete."""
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="long-task", symbol="601872"))
    store.next_queued()

    result = store.complete_result(
        "long-task",
        FULL_VALUES,
        "/tmp/LONG.png",
    )

    assert result.status == TaskStatus.COMPLETED
    assert result.long_capture.status == CaptureStatus.READY
    assert result.long_capture.path == Path("/tmp/LONG.png")
    assert result.values[MetricKind.RETAIL_COUNT] == "21.23"
    assert result.values[MetricKind.STOCK_NAME] == "招商轮船"
    assert all(capture.status == CaptureStatus.PENDING for capture in result.captures.values())


def test_long_result_is_partial_when_one_value_cannot_be_validated() -> None:
    """A missing OCR value must stay visibly missing instead of being guessed or called complete."""
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="partial-long-task", symbol="601872"))
    store.next_queued()

    result = store.complete_result(
        "partial-long-task",
        FULL_VALUES | {MetricKind.LARGE_ORDER_AMOUNT: None},
        "/tmp/LONG.png",
    )

    assert result.status == TaskStatus.PARTIAL
    assert result.error_code == "VALUE_RECOGNITION_FAILED"
    assert result.long_capture.status == CaptureStatus.READY


def test_retention_expires_and_removes_the_long_capture(tmp_path: Path) -> None:
    """The replacement long image must obey the same 24-hour retention boundary."""
    capture = tmp_path / "task" / "LONG.png"
    capture.parent.mkdir()
    capture.write_bytes(b"long capture")
    store = InMemoryStreams(capture_root=tmp_path)
    task = TaskRecord(task_id="task", symbol="601872")
    store.enqueue(task)
    store.next_queued()
    store.complete_result(
        task.task_id,
        FULL_VALUES,
        str(capture),
    )
    task.long_capture.captured_at = task.created_at

    store.cleanup(task.created_at + timedelta(hours=25))

    assert task.long_capture.status == CaptureStatus.EXPIRED
    assert task.status == TaskStatus.EXPIRED
    assert not capture.exists()


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


def test_recover_running_restores_an_interrupted_job_ahead_of_later_fifo_work() -> None:
    """A process restart must not leave its already-claimed task permanently RUNNING."""
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="interrupted", symbol="601872"))
    store.enqueue(TaskRecord(task_id="later", symbol="600938"))
    assert store.next_queued().task_id == "interrupted"

    recovered = store.recover_running()

    assert [task.task_id for task in recovered] == ["interrupted"]
    assert store.get("interrupted").status == TaskStatus.QUEUED
    assert store.recover_running() == []
    assert store.next_queued().task_id == "interrupted"


def test_failed_task_can_be_retried_without_losing_verified_captures() -> None:
    store = InMemoryStreams()
    task = TaskRecord(task_id="retry", symbol="600938")
    store.enqueue(task)
    store.transition(task.task_id, TaskStatus.RUNNING)
    store.complete_capture(task.task_id, CaptureKind.LARGE_ORDER_NET, "/tmp/net.png")
    store.transition(task.task_id, TaskStatus.FAILED, error_code="DEVICE_OFFLINE")

    retried = store.retry_failed(task.task_id)

    assert retried.status == TaskStatus.QUEUED
    assert retried.error_code is None
    assert retried.captures[CaptureKind.LARGE_ORDER_NET].status == CaptureStatus.READY
    assert store.next_queued().task_id == task.task_id


def test_waiting_admin_recovery_is_requeued_after_already_queued_jobs() -> None:
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="waiting", symbol="600938"))
    store.enqueue(TaskRecord(task_id="later", symbol="000001"))
    store.next_queued()
    store.transition("waiting", TaskStatus.WAITING_ADMIN, error_code="WAITING_ADMIN")

    store.requeue_waiting("waiting")

    assert store.next_queued().task_id == "later"


def test_failed_retry_is_requeued_after_already_queued_jobs() -> None:
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="failed", symbol="600938"))
    store.enqueue(TaskRecord(task_id="later", symbol="000001"))
    store.next_queued()
    store.transition("failed", TaskStatus.FAILED, error_code="DEVICE_OFFLINE")

    store.retry_failed("failed")

    assert store.next_queued().task_id == "later"
