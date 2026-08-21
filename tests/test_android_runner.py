from pathlib import Path

from level2_service.models import CaptureKind, TaskRecord, TaskStatus
from level2_service.queue import InMemoryStreams
from level2_service.runner import (
    FakeDeviceBridge,
    Level2Navigator,
    Level2Runner,
    NavigationError,
    NeedsAdminError,
    RunnerControl,
)


def test_navigator_uses_visual_fallback_when_selector_is_missing() -> None:
    """A skin change must not make a verified Level2 tab unreachable."""
    bridge = FakeDeviceBridge(symbol="SZ.000001", selector_available=False)
    navigator = Level2Navigator(bridge)

    image = navigator.capture("SZ.000001", CaptureKind.LARGE_ORDER_NET)

    assert image.startswith(b"\x89PNG")
    assert bridge.visual_actions == ["search"]


def test_navigator_requires_exact_symbol_and_tab_before_capturing() -> None:
    """A similarly named stock or tab must never be published as this task's result."""
    bridge = FakeDeviceBridge(symbol="SZ.000001", visible_symbol="SZ.000002")

    try:
        Level2Navigator(bridge).capture("SZ.000001", CaptureKind.RETAIL_COUNT)
    except NavigationError as error:
        assert "symbol" in str(error)
    else:
        raise AssertionError("mismatched symbol was captured")


def test_navigator_requires_the_requested_tab_to_be_active_before_capturing() -> None:
    """Tab labels can all remain visible in the tab bar; only the active one is valid."""
    bridge = FakeDeviceBridge(symbol="SZ.000001", tab_activation=False)

    try:
        Level2Navigator(bridge).capture("SZ.000001", CaptureKind.LARGE_ORDER_AMOUNT)
    except NavigationError as error:
        assert "active" in str(error)
    else:
        raise AssertionError("inactive tab was captured")


def test_runner_retries_transient_navigation_up_to_three_attempts(tmp_path: Path) -> None:
    """A temporary UI miss should be retried, but cannot spin forever."""
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="retry-task", symbol="SZ.000001"))
    bridge = FakeDeviceBridge(symbol="SZ.000001", failures=[NavigationError("temporary"), NavigationError("temporary")])
    runner = Level2Runner(store, Level2Navigator(bridge), tmp_path, RunnerControl())

    task = runner.run_once()

    assert task is not None
    assert task.status == TaskStatus.COMPLETED
    assert bridge.capture_attempts == 5


def test_runner_marks_login_requirement_waiting_for_admin(tmp_path: Path) -> None:
    """Login/CAPTCHA/device gates must wait for a human instead of being bypassed."""
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="admin-task", symbol="SZ.000001"))
    bridge = FakeDeviceBridge(symbol="SZ.000001", failures=[NeedsAdminError("login required")])
    runner = Level2Runner(store, Level2Navigator(bridge), tmp_path, RunnerControl())

    task = runner.run_once()

    assert task is not None
    assert task.status == TaskStatus.WAITING_ADMIN
    assert task.error_code == "WAITING_ADMIN"


def test_waiting_admin_task_can_be_requeued_and_run_after_intervention(tmp_path: Path) -> None:
    """A human-cleared login gate must not strand the claimed FIFO task forever."""
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="recover-task", symbol="SZ.000001"))
    blocked = Level2Runner(store, Level2Navigator(FakeDeviceBridge(symbol="SZ.000001", failures=[NeedsAdminError("login")])), tmp_path, RunnerControl()).run_once()

    assert blocked is not None and blocked.status == TaskStatus.WAITING_ADMIN
    assert store.requeue_waiting("recover-task").status == TaskStatus.QUEUED
    recovered = Level2Runner(store, Level2Navigator(FakeDeviceBridge(symbol="SZ.000001")), tmp_path, RunnerControl()).run_once()

    assert recovered is not None and recovered.status == TaskStatus.COMPLETED


def test_runner_keeps_completed_tabs_when_a_later_tab_fails(tmp_path: Path) -> None:
    """Successful verified screens remain available when one later tab is unavailable."""
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="partial-task", symbol="SZ.000001"))
    bridge = FakeDeviceBridge(symbol="SZ.000001", failures=[None, NavigationError("bad tab"), NavigationError("bad tab"), NavigationError("bad tab")])
    runner = Level2Runner(store, Level2Navigator(bridge), tmp_path, RunnerControl())

    task = runner.run_once()

    assert task is not None
    assert task.status == TaskStatus.PARTIAL
    assert task.captures[CaptureKind.LARGE_ORDER_NET].path == tmp_path / "partial-task" / "LARGE_ORDER_NET.png"
    assert (tmp_path / "partial-task" / "LARGE_ORDER_NET.png").is_file()


def test_runner_control_pauses_queue_and_only_lock_owner_can_forward_input() -> None:
    """Queue and remote-device control belong to one authenticated admin session."""
    control = RunnerControl()
    control.heartbeat("READY")
    control.pause_queue()

    assert control.health()["state"] == "READY"
    assert control.health()["queue_paused"] is True
    assert control.lock("owner") is True
    assert control.authorizes_input("owner") is True
    assert control.authorizes_input("other") is False
    assert control.status("owner")["state"] == "ADMIN_CONTROL"
    assert control.release("owner") is True
    assert control.status("owner")["state"] == "READY"
