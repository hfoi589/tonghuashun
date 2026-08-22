from __future__ import annotations

from datetime import datetime
from pathlib import Path
import types
from zoneinfo import ZoneInfo

from level2_service.models import MetricKind, TaskRecord, TaskStatus
from level2_service.queue import InMemoryStreams
from level2_service import runner as runner_module
from level2_service.runner import Level2Runner, NavigationError, RunnerControl


SHANGHAI = ZoneInfo("Asia/Shanghai")
VALUES = {
    MetricKind.STOCK_NAME: "招商轮船",
    MetricKind.CURRENT_PRICE: "19.78",
    MetricKind.CHANGE_PERCENT: "7.15%",
    MetricKind.TURNOVER_RATE: "2.40%",
    MetricKind.RETAIL_COUNT: "21.23",
    MetricKind.LARGE_ORDER_NET: "-0.02",
    MetricKind.LARGE_ORDER_AMOUNT: "-2802.6万",
    MetricKind.MACDFS: "+0.012",
}


def _daily_state(path: Path, current: list[datetime]):
    daily_check_state = getattr(runner_module, "DailyCheckState")
    return daily_check_state(path, clock=lambda: current[0])


class DailyCheckNavigator:
    def __init__(self, blockers: set[str] | None = None) -> None:
        self.blockers = blockers or set()
        self.opened: list[str] = []
        self.checks = 0

    def open_stock(self, symbol: str) -> None:
        self.opened.append(symbol)

    def admin_blocking_texts(self) -> frozenset[str]:
        self.checks += 1
        return frozenset(self.blockers)

    def capture_chart_frames(self) -> tuple[bytes, ...]:
        raise AssertionError("data-only tasks must not capture frames")

    def device_online(self) -> bool:
        return True


def _data_only_task(task_id: str, symbol: str = "601872") -> TaskRecord:
    return TaskRecord(task_id=task_id, symbol=symbol, include_long_capture=False)


def _runner(store, navigator, tmp_path: Path, daily_check_state, control: RunnerControl | None = None):
    return Level2Runner(
        store,
        navigator,
        tmp_path / "captures",
        control or RunnerControl(),
        parsed_value_source=types.SimpleNamespace(read_direct=lambda _symbol: dict(VALUES)),
        daily_check_state=daily_check_state,
    )


def test_daily_check_state_persists_success_across_new_instances(tmp_path: Path) -> None:
    """Container recreation must not repeat a check already completed today."""
    current = [datetime(2026, 8, 22, 9, 0, tzinfo=SHANGHAI)]
    path = tmp_path / "daily-check.json"
    state = _daily_state(path, current)

    assert state.passed_today() is False
    state.mark_passed()

    assert _daily_state(path, current).passed_today() is True


def test_daily_check_state_expires_on_the_next_beijing_day(tmp_path: Path) -> None:
    """A persisted success must not suppress tomorrow's first-task check."""
    current = [datetime(2026, 8, 22, 23, 59, tzinfo=SHANGHAI)]
    state = _daily_state(tmp_path / "daily-check.json", current)
    state.mark_passed()

    current[0] = datetime(2026, 8, 23, 0, 1, tzinfo=SHANGHAI)

    assert state.passed_today() is False


def test_first_task_checks_once_and_later_task_skips_the_check(tmp_path: Path) -> None:
    """After today's successful gate, normal data tasks must stay on the fast path."""
    current = [datetime(2026, 8, 22, 9, 0, tzinfo=SHANGHAI)]
    daily_state = _daily_state(tmp_path / "daily-check.json", current)
    store = InMemoryStreams()
    store.enqueue(_data_only_task("first"))
    navigator = DailyCheckNavigator()
    runner = _runner(store, navigator, tmp_path, daily_state)

    first = runner.run_once()
    store.enqueue(_data_only_task("second", "601975"))
    second = runner.run_once()

    assert first is not None and first.status == TaskStatus.COMPLETED
    assert second is not None and second.status == TaskStatus.COMPLETED
    assert navigator.opened == []
    assert navigator.checks == 1
    assert daily_state.passed_today() is True


def test_first_task_blocker_waits_for_admin_without_recording_success(tmp_path: Path) -> None:
    """Recording a failed gate would hide the same login problem from the retry."""
    current = [datetime(2026, 8, 22, 9, 0, tzinfo=SHANGHAI)]
    daily_state = _daily_state(tmp_path / "daily-check.json", current)
    store = InMemoryStreams()
    store.enqueue(_data_only_task("blocked"))
    navigator = DailyCheckNavigator({"验证码"})
    control = RunnerControl()

    task = _runner(store, navigator, tmp_path, daily_state, control).run_once()

    assert task is not None and task.status == TaskStatus.WAITING_ADMIN
    assert control.queue_paused is True
    assert navigator.checks == 1
    assert daily_state.passed_today() is False


def test_admin_retry_checks_again_and_records_only_after_success(tmp_path: Path) -> None:
    current = [datetime(2026, 8, 22, 9, 0, tzinfo=SHANGHAI)]
    daily_state = _daily_state(tmp_path / "daily-check.json", current)
    store = InMemoryStreams()
    store.enqueue(_data_only_task("retry"))
    navigator = DailyCheckNavigator({"登录"})
    control = RunnerControl()
    runner = _runner(store, navigator, tmp_path, daily_state, control)

    blocked = runner.run_once()
    assert blocked is not None and blocked.status == TaskStatus.WAITING_ADMIN
    navigator.blockers.clear()
    store.requeue_waiting("retry")
    control.resume_queue()
    recovered = runner.run_once()

    assert recovered is not None and recovered.status == TaskStatus.COMPLETED
    assert navigator.checks == 2
    assert daily_state.passed_today() is True


def test_next_day_first_task_runs_the_check_again(tmp_path: Path) -> None:
    current = [datetime(2026, 8, 22, 9, 0, tzinfo=SHANGHAI)]
    daily_state = _daily_state(tmp_path / "daily-check.json", current)
    store = InMemoryStreams()
    store.enqueue(_data_only_task("day-one"))
    navigator = DailyCheckNavigator()
    runner = _runner(store, navigator, tmp_path, daily_state)
    runner.run_once()

    current[0] = datetime(2026, 8, 23, 9, 0, tzinfo=SHANGHAI)
    store.enqueue(_data_only_task("day-two"))
    runner.run_once()

    assert navigator.checks == 2


def test_navigation_failure_checks_current_page_for_an_admin_gate(tmp_path: Path) -> None:
    """A login overlay that breaks stock navigation must be classified as WAITING_ADMIN."""
    class BlockedNavigation(DailyCheckNavigator):
        def open_stock(self, symbol: str) -> None:
            self.opened.append(symbol)
            raise NavigationError("stock page did not open")

    current = [datetime(2026, 8, 22, 9, 0, tzinfo=SHANGHAI)]
    daily_state = _daily_state(tmp_path / "daily-check.json", current)
    store = InMemoryStreams()
    store.enqueue(_data_only_task("blocked-navigation"))
    navigator = BlockedNavigation({"设备验证"})

    task = _runner(store, navigator, tmp_path, daily_state).run_once()

    assert task is not None and task.status == TaskStatus.WAITING_ADMIN
    assert navigator.opened == []
    assert navigator.checks == 1
    assert daily_state.passed_today() is False
