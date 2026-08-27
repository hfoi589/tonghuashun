from datetime import datetime, timezone

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from level2_service.api import create_app
from level2_service.device_lifecycle import (
    DeviceLifecycleAction,
    DeviceLifecycleError,
    DeviceLifecycleOperation,
    DeviceLifecycleState,
    DeviceLifecycleStatus,
)
from level2_service.models import TaskRecord, TaskStatus
from level2_service.queue import InMemoryStreams, RedisStreamsStore
from level2_service.runner import FakeDeviceBridge
from tests.test_redis_store import FakeRedis


NOW = datetime(2026, 8, 28, 1, 2, 3, tzinfo=timezone.utc)


class FakeDeviceLifecycle:
    def __init__(self, *, error_code: str | None = None) -> None:
        self.error_code = error_code
        self.calls: list[tuple[str, DeviceLifecycleAction]] = []
        self.statuses = (
            DeviceLifecycleStatus(
                "core_metrics",
                DeviceLifecycleState.RUNNING,
                None,
                None,
                NOW,
            ),
            DeviceLifecycleStatus(
                "main_fund_flow",
                DeviceLifecycleState.STOPPED,
                "fund-stop",
                None,
                NOW,
            ),
        )

    def devices(self) -> tuple[DeviceLifecycleStatus, ...]:
        return self.statuses

    def submit(
        self,
        role: str,
        action: DeviceLifecycleAction,
    ) -> DeviceLifecycleOperation:
        self.calls.append((role, action))
        if self.error_code is not None:
            raise DeviceLifecycleError(self.error_code)
        state = (
            DeviceLifecycleState.STARTING
            if action == DeviceLifecycleAction.START_AND_LAUNCH_APP
            else DeviceLifecycleState.STOPPING
        )
        return DeviceLifecycleOperation(
            operation_id=f"{role}-{action.value}",
            role=role,
            action=action,
            state=state,
            error_code=None,
            updated_at=NOW,
        )


def login_admin(client: TestClient) -> str:
    assert client.post(
        "/api/admin/session", json={"password": "admin-secret"}
    ).status_code == 204
    csrf = client.cookies.get("ths_csrf")
    assert csrf is not None
    return csrf


def lifecycle_app(
    lifecycle: object | None = None,
    *,
    store: InMemoryStreams | None = None,
):
    return create_app(
        store=store,
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        device_bridges={
            "core_metrics": FakeDeviceBridge(symbol="600938"),
            "main_fund_flow": FakeDeviceBridge(symbol="600938"),
        },
        device_lifecycle=lifecycle,
    )


@pytest.mark.parametrize(
    "store_factory",
    [
        pytest.param(InMemoryStreams, id="memory"),
        pytest.param(lambda: RedisStreamsStore(FakeRedis()), id="redis"),
    ],
)
@pytest.mark.parametrize(
    ("status", "expected_busy"),
    [
        (TaskStatus.QUEUED, False),
        (TaskStatus.RUNNING, True),
        (TaskStatus.WAITING_ADMIN, False),
        (TaskStatus.PARTIAL, True),
        (TaskStatus.COMPLETED, False),
        (TaskStatus.FAILED, False),
        (TaskStatus.EXPIRED, False),
    ],
)
def test_store_reports_only_running_or_partial_tasks_as_device_busy(
    store_factory,
    status: TaskStatus,
    expected_busy: bool,
) -> None:
    """Counting queued or waiting work would block maintenance after queue pause."""
    store = store_factory()
    store.enqueue(
        TaskRecord(task_id=f"job-{status.value}", symbol="000001", status=status)
    )

    assert store.has_running_task() is expected_busy


def test_redis_busy_scan_ignores_an_invalid_stale_task_payload() -> None:
    """One corrupt stale record must not hide a valid actively running task."""
    redis = FakeRedis()
    store = RedisStreamsStore(redis)
    store.enqueue(TaskRecord(task_id="running", symbol="000001", status=TaskStatus.RUNNING))
    redis.set("ths:jobs:task:stale", "not-json")
    redis.sadd("ths:jobs:tasks", "stale")

    assert store.has_running_task() is True


def test_admin_device_action_requires_csrf_lock_and_idle_runner() -> None:
    lifecycle = FakeDeviceLifecycle()
    app = lifecycle_app(lifecycle)
    client = TestClient(app, base_url="https://testserver")

    assert client.post(
        "/api/admin/devices/core_metrics/actions",
        json={"action": "shutdown"},
    ).status_code == 401
    csrf = login_admin(client)
    assert client.post(
        "/api/admin/devices/core_metrics/actions",
        json={"action": "shutdown"},
    ).status_code == 403
    no_lock = client.post(
        "/api/admin/devices/core_metrics/actions",
        headers={"X-CSRF-Token": csrf},
        json={"action": "shutdown"},
    )

    assert no_lock.status_code == 409
    assert no_lock.json() == {"detail": "DEVICE_LIFECYCLE_LOCK_REQUIRED"}
    assert lifecycle.calls == []


@pytest.mark.parametrize("role", ["core_metrics", "main_fund_flow"])
@pytest.mark.parametrize(
    "action",
    [
        DeviceLifecycleAction.START_AND_LAUNCH_APP,
        DeviceLifecycleAction.SHUTDOWN,
    ],
)
def test_lock_owner_can_submit_each_fixed_action_for_each_role(
    role: str,
    action: DeviceLifecycleAction,
) -> None:
    lifecycle = FakeDeviceLifecycle()
    client = TestClient(lifecycle_app(lifecycle), base_url="https://testserver")
    csrf = login_admin(client)
    assert client.post(
        "/api/admin/lock/acquire", headers={"X-CSRF-Token": csrf}
    ).status_code == 200

    response = client.post(
        f"/api/admin/devices/{role}/actions",
        headers={"X-CSRF-Token": csrf},
        json={"action": action.value},
    )

    assert response.status_code == 202
    assert response.json() == {
        "state": "STARTING" if action.value.startswith("start") else "STOPPING",
        "operation_id": f"{role}-{action.value}",
        "error_code": None,
        "updated_at": "2026-08-28T01:02:03Z",
    }
    assert lifecycle.calls == [(role, action)]
    assert "serial" not in response.text.lower()
    assert "command" not in response.text.lower()


@pytest.mark.parametrize("status", [TaskStatus.RUNNING, TaskStatus.PARTIAL])
def test_admin_device_action_rejects_an_active_device_task(status: TaskStatus) -> None:
    store = InMemoryStreams()
    store.enqueue(TaskRecord(task_id="busy", symbol="000001", status=status))
    lifecycle = FakeDeviceLifecycle()
    client = TestClient(
        lifecycle_app(lifecycle, store=store), base_url="https://testserver"
    )
    csrf = login_admin(client)
    client.post("/api/admin/lock/acquire", headers={"X-CSRF-Token": csrf})

    response = client.post(
        "/api/admin/devices/core_metrics/actions",
        headers={"X-CSRF-Token": csrf},
        json={"action": "shutdown"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "DEVICE_LIFECYCLE_BUSY"}
    assert lifecycle.calls == []


def test_admin_device_action_requires_the_queue_to_remain_paused() -> None:
    lifecycle = FakeDeviceLifecycle()
    app = lifecycle_app(lifecycle)
    client = TestClient(app, base_url="https://testserver")
    csrf = login_admin(client)
    client.post("/api/admin/lock/acquire", headers={"X-CSRF-Token": csrf})
    app.state.runner_control.queue_paused = False

    response = client.post(
        "/api/admin/devices/core_metrics/actions",
        headers={"X-CSRF-Token": csrf},
        json={"action": "shutdown"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "DEVICE_LIFECYCLE_BUSY"}
    assert lifecycle.calls == []


def test_admin_device_action_rejects_unknown_roles_and_extra_fields() -> None:
    lifecycle = FakeDeviceLifecycle()
    client = TestClient(lifecycle_app(lifecycle), base_url="https://testserver")
    csrf = login_admin(client)
    client.post("/api/admin/lock/acquire", headers={"X-CSRF-Token": csrf})

    unknown = client.post(
        "/api/admin/devices/not-a-role/actions",
        headers={"X-CSRF-Token": csrf},
        json={"action": "shutdown"},
    )
    extra = client.post(
        "/api/admin/devices/core_metrics/actions",
        headers={"X-CSRF-Token": csrf},
        json={"action": "shutdown", "serial": "emulator-5556"},
    )

    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "device role not found"}
    assert extra.status_code == 422
    assert lifecycle.calls == []


def test_admin_device_action_returns_fixed_unconfigured_error() -> None:
    client = TestClient(lifecycle_app(), base_url="https://testserver")
    csrf = login_admin(client)
    client.post("/api/admin/lock/acquire", headers={"X-CSRF-Token": csrf})

    response = client.post(
        "/api/admin/devices/core_metrics/actions",
        headers={"X-CSRF-Token": csrf},
        json={"action": "shutdown"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "DEVICE_LIFECYCLE_UNAVAILABLE"}


@pytest.mark.parametrize(
    ("error_code", "status_code"),
    [
        ("DEVICE_ACTION_IN_PROGRESS", 409),
        ("DEVICE_BOOT_TIMEOUT", 504),
        ("DEVICE_AVD_NOT_FOUND", 503),
        ("DEVICE_APP_LAUNCH_FAILED", 503),
        ("DEVICE_SHUTDOWN_FAILED", 503),
        ("DEVICE_LIFECYCLE_FAILED", 503),
        ("DEVICE_LIFECYCLE_UNAVAILABLE", 503),
    ],
)
def test_admin_device_action_maps_only_fixed_upstream_errors(
    error_code: str,
    status_code: int,
) -> None:
    lifecycle = FakeDeviceLifecycle(error_code=error_code)
    client = TestClient(lifecycle_app(lifecycle), base_url="https://testserver")
    csrf = login_admin(client)
    client.post("/api/admin/lock/acquire", headers={"X-CSRF-Token": csrf})

    response = client.post(
        "/api/admin/devices/core_metrics/actions",
        headers={"X-CSRF-Token": csrf},
        json={"action": "shutdown"},
    )

    assert response.status_code == status_code
    assert response.json() == {"detail": error_code}


def test_admin_device_action_sanitizes_unexpected_broker_errors(caplog) -> None:
    class UnsafeLifecycle(FakeDeviceLifecycle):
        def submit(self, role, action):
            raise RuntimeError(
                "token=private serial=emulator-5556 command=adb emu kill"
            )

    client = TestClient(
        lifecycle_app(UnsafeLifecycle()), base_url="https://testserver"
    )
    csrf = login_admin(client)
    client.post("/api/admin/lock/acquire", headers={"X-CSRF-Token": csrf})

    response = client.post(
        "/api/admin/devices/core_metrics/actions",
        headers={"X-CSRF-Token": csrf},
        json={"action": "shutdown"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "DEVICE_LIFECYCLE_FAILED"}
    exposed = (response.text + caplog.text).lower()
    for secret in ("private", "emulator-5556", "adb emu kill"):
        assert secret not in exposed


def test_admin_devices_include_safe_lifecycle_statuses() -> None:
    client = TestClient(
        lifecycle_app(FakeDeviceLifecycle()), base_url="https://testserver"
    )
    login_admin(client)

    response = client.get("/api/admin/devices")

    assert response.status_code == 200
    devices = {device["role"]: device for device in response.json()["devices"]}
    assert devices["core_metrics"]["lifecycle"] == {
        "state": "RUNNING",
        "operation_id": None,
        "error_code": None,
        "updated_at": "2026-08-28T01:02:03Z",
    }
    assert devices["main_fund_flow"]["lifecycle"] == {
        "state": "STOPPED",
        "operation_id": "fund-stop",
        "error_code": None,
        "updated_at": "2026-08-28T01:02:03Z",
    }


def test_admin_devices_report_unconfigured_lifecycle_without_a_client() -> None:
    client = TestClient(lifecycle_app(), base_url="https://testserver")
    login_admin(client)

    devices = client.get("/api/admin/devices").json()["devices"]

    assert {device["lifecycle"]["state"] for device in devices} == {
        "UNCONFIGURED"
    }
    assert all(
        device["lifecycle"] == {
            "state": "UNCONFIGURED",
            "operation_id": None,
            "error_code": None,
            "updated_at": None,
        }
        for device in devices
    )
