from __future__ import annotations

from datetime import datetime, timezone
from threading import Barrier, Thread

from argon2 import PasswordHasher
from fastapi.testclient import TestClient
import pytest

from level2_service.api import create_app
from level2_service.device_lifecycle import (
    DeviceLifecycleAction,
    DeviceLifecycleOperation,
    DeviceLifecycleState,
    DeviceLifecycleStatus,
)
from level2_service.models import TaskRecord, TaskStatus
from level2_service.queue import InMemoryStreams, RedisStreamsStore
from level2_service.runner import FakeDeviceBridge, RunnerControl
from tests.test_redis_store import FakeRedis


NOW = datetime(2026, 8, 28, 6, 0, tzinfo=timezone.utc)
OWNER = "deployment-owner-token-abcdefghijklmnopqrstuvwxyz"
OTHER_OWNER = "other-deployment-owner-token-abcdefghijklmnop"


class ManualClock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def memory_store(clock: ManualClock | None = None) -> InMemoryStreams:
    return InMemoryStreams(lease_clock=clock or ManualClock())


def stores() -> list[tuple[object, object | None]]:
    clock = ManualClock()
    redis = FakeRedis(clock=clock)
    return [
        (memory_store(clock), clock),
        (RedisStreamsStore(redis), redis),
    ]


@pytest.mark.parametrize("kind", ["memory", "redis"])
def test_deployment_lease_owner_ttl_and_compare_release(kind: str) -> None:
    """Only the live owner may renew or release the durable deployment gate."""
    clock = ManualClock()
    if kind == "memory":
        store = memory_store(clock)
        advancer = clock
    else:
        redis = FakeRedis(clock=clock)
        store = RedisStreamsStore(redis)
        advancer = redis

    assert store.acquire_deployment_lease(OWNER, 30.0) is True
    lease = store.deployment_lease_status()
    assert lease is not None
    assert lease.bound_task_id is None
    assert lease.ttl_seconds > 0
    assert OWNER not in repr(lease)
    assert store.renew_deployment_lease(OTHER_OWNER, 60.0) is False
    assert store.release_deployment_lease(OTHER_OWNER) is False
    assert store.renew_deployment_lease(OWNER, 60.0) is True

    advancer.advance(61.0)

    assert store.deployment_lease_status() is None
    assert store.release_deployment_lease(OWNER) is False


@pytest.mark.parametrize("kind", ["memory", "redis"])
def test_active_lease_claims_only_its_bound_acceptance_regardless_of_fifo_order(
    kind: str,
) -> None:
    """User work queued first remains frozen while the fixed acceptance job runs."""
    clock = ManualClock()
    if kind == "memory":
        store = memory_store(clock)
    else:
        store = RedisStreamsStore(FakeRedis(clock=clock))
    ordinary = TaskRecord(
        task_id="ordinary-first", symbol="000001", include_long_capture=False
    )
    acceptance = TaskRecord(
        task_id="bound-acceptance", symbol="601872", include_long_capture=False
    )
    store.enqueue(ordinary)
    assert store.acquire_deployment_lease(OWNER, 60.0) is True
    bound = store.bind_deployment_acceptance(OWNER, acceptance)
    assert bound is not None and bound.task_id == acceptance.task_id

    claimed = store.next_runnable()

    assert claimed is not None and claimed.task_id == "bound-acceptance"
    assert store.get("ordinary-first").status == TaskStatus.QUEUED
    assert store.next_runnable() is None
    assert store.release_deployment_lease(OWNER) is True
    assert store.next_runnable().task_id == "ordinary-first"


@pytest.mark.parametrize("kind", ["memory", "redis"])
def test_binding_is_idempotent_and_rejects_wrong_owner_or_mismatched_task(
    kind: str,
) -> None:
    """One lease can name exactly one immutable fixed acceptance task."""
    clock = ManualClock()
    store = (
        memory_store(clock)
        if kind == "memory"
        else RedisStreamsStore(FakeRedis(clock=clock))
    )
    assert store.acquire_deployment_lease(OWNER, 60.0)
    first = TaskRecord(
        task_id="acceptance-one", symbol="601872", include_long_capture=False
    )
    assert store.bind_deployment_acceptance(OWNER, first).task_id == "acceptance-one"
    assert (
        store.bind_deployment_acceptance(
            OWNER,
            TaskRecord(
                task_id="acceptance-two",
                symbol="601872",
                include_long_capture=False,
            ),
        ).task_id
        == "acceptance-one"
    )
    assert (
        store.bind_deployment_acceptance(
            OTHER_OWNER,
            TaskRecord(
                task_id="attacker", symbol="601872", include_long_capture=False
            ),
        )
        is None
    )
    assert (
        store.bind_deployment_acceptance(
            OWNER,
            TaskRecord(
                task_id="wrong-symbol", symbol="000001", include_long_capture=False
            ),
        )
        is None
    )
    assert (
        store.bind_deployment_acceptance(
            OWNER,
            TaskRecord(
                task_id="wrong-options", symbol="601872", include_long_capture=True
            ),
        )
        is None
    )


@pytest.mark.parametrize("kind", ["memory", "redis"])
def test_claim_and_lease_acquisition_race_cannot_cross(kind: str) -> None:
    """Either the task starts first or the lease wins; both outcomes cannot occur."""
    clock = ManualClock()
    store = (
        memory_store(clock)
        if kind == "memory"
        else RedisStreamsStore(FakeRedis(clock=clock))
    )
    store.enqueue(TaskRecord(task_id="ordinary", symbol="000001"))
    barrier = Barrier(2)
    results: dict[str, object] = {}

    def acquire() -> None:
        barrier.wait()
        results["lease"] = store.acquire_deployment_lease(OWNER, 60.0)

    def claim() -> None:
        barrier.wait()
        results["task"] = store.next_runnable()

    threads = [Thread(target=acquire), Thread(target=claim)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    claimed = results["task"]
    acquired = results["lease"]
    assert not (acquired is True and claimed is not None)


def test_redis_lease_survives_store_and_runner_replacement() -> None:
    """A new API process sees the same lease and may claim only its bound task."""
    redis = FakeRedis(clock=ManualClock())
    old_store = RedisStreamsStore(redis)
    old_store.enqueue(TaskRecord(task_id="ordinary", symbol="000001"))
    assert old_store.acquire_deployment_lease(OWNER, 60.0)
    old_store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="acceptance", symbol="601872", include_long_capture=False
        ),
    )

    new_store = RedisStreamsStore(redis)
    new_control = RunnerControl()
    claimed = new_control.claim_next_task(new_store)

    assert claimed is not None and claimed.task_id == "acceptance"
    assert new_store.get("ordinary").status == TaskStatus.QUEUED


def test_failed_acceptance_retains_the_lease_for_safe_rollback() -> None:
    """A terminal acceptance failure never resumes queued user work implicitly."""
    store = memory_store()
    store.enqueue(TaskRecord(task_id="ordinary", symbol="000001"))
    assert store.acquire_deployment_lease(OWNER, 60.0)
    acceptance = store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="acceptance", symbol="601872", include_long_capture=False
        ),
    )
    assert acceptance is not None
    assert store.next_runnable().task_id == "acceptance"
    store.transition("acceptance", TaskStatus.FAILED, error_code="DIRECT_APP_OFFLINE")

    assert store.deployment_lease_status() is not None
    assert store.next_runnable() is None
    assert store.get("ordinary").status == TaskStatus.QUEUED


def lease_app(store):
    return create_app(
        store=store,
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        device_bridges={
            "core_metrics": FakeDeviceBridge(symbol="601872"),
            "main_fund_flow": FakeDeviceBridge(symbol="601872"),
        },
        runner_control=RunnerControl(),
    )


def test_internal_acceptance_endpoint_binds_one_fixed_task_and_is_idempotent() -> None:
    """The host endpoint exposes neither symbol nor capture options."""
    store = memory_store()
    store.enqueue(TaskRecord(task_id="ordinary", symbol="000001"))
    assert store.acquire_deployment_lease(OWNER, 60.0)
    client = TestClient(lease_app(store), base_url="http://testserver")
    headers = {"Authorization": f"Bearer {OWNER}"}

    first = client.post(
        "/internal/deployment/acceptance", headers=headers, json={}
    )
    second = client.post(
        "/internal/deployment/acceptance", headers=headers, json={}
    )
    extra = client.post(
        "/internal/deployment/acceptance",
        headers=headers,
        json={"symbol": "000001", "include_long_capture": True},
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["public_id"] == second.json()["public_id"]
    assert first.json()["symbol"] == "601872"
    assert first.json()["include_long_capture"] is False
    assert extra.status_code == 422
    assert store.get("ordinary").status == TaskStatus.QUEUED
    assert store.next_runnable().task_id == first.json()["public_id"]


def test_internal_acceptance_endpoint_fails_closed_without_exposing_owner(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing, wrong, or expired owner tokens produce fixed safe responses."""
    clock = ManualClock()
    store = memory_store(clock)
    assert store.acquire_deployment_lease(OWNER, 10.0)
    client = TestClient(lease_app(store), base_url="http://testserver")

    missing = client.post("/internal/deployment/acceptance", json={})
    wrong = client.post(
        "/internal/deployment/acceptance",
        headers={"Authorization": f"Bearer {OTHER_OWNER}"},
        json={},
    )
    clock.advance(11.0)
    expired = client.post(
        "/internal/deployment/acceptance",
        headers={"Authorization": f"Bearer {OWNER}"},
        json={},
    )

    assert [missing.status_code, wrong.status_code, expired.status_code] == [
        401,
        409,
        409,
    ]
    exposed = missing.text + wrong.text + expired.text + caplog.text
    assert OWNER not in exposed
    assert OTHER_OWNER not in exposed


class StatefulLifecycle:
    def __init__(self) -> None:
        self.operation_id = "operation-core"
        self.state = DeviceLifecycleState.STARTING

    def submit(self, role: str, action: DeviceLifecycleAction):
        return DeviceLifecycleOperation(
            self.operation_id,
            role,
            action,
            self.state,
            None,
            NOW,
        )

    def devices(self):
        return (
            DeviceLifecycleStatus(
                "core_metrics", self.state, self.operation_id, None, NOW
            ),
            DeviceLifecycleStatus(
                "main_fund_flow",
                DeviceLifecycleState.RUNNING,
                None,
                None,
                NOW,
            ),
        )


def login_and_lock(client: TestClient) -> str:
    assert client.post(
        "/api/admin/session", json={"password": "admin-secret"}
    ).status_code == 204
    csrf = client.cookies["ths_csrf"]
    assert client.post(
        "/api/admin/lock/acquire", headers={"X-CSRF-Token": csrf}
    ).status_code == 200
    return csrf


def operation_app(lifecycle: StatefulLifecycle, control: RunnerControl, store):
    return create_app(
        store=store,
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        device_bridges={
            "core_metrics": FakeDeviceBridge(symbol="601872"),
            "main_fund_flow": FakeDeviceBridge(symbol="601872"),
        },
        device_lifecycle=lifecycle,
        runner_control=control,
    )


def test_lifecycle_operation_lease_blocks_release_resume_and_runner_claim_until_terminal() -> None:
    """Submission ownership lasts through the broker's matching terminal state."""
    store = memory_store()
    store.enqueue(TaskRecord(task_id="queued", symbol="000001"))
    control = RunnerControl()
    lifecycle = StatefulLifecycle()
    client = TestClient(
        operation_app(lifecycle, control, store), base_url="https://testserver"
    )
    csrf = login_and_lock(client)

    submitted = client.post(
        "/api/admin/devices/core_metrics/actions",
        headers={"X-CSRF-Token": csrf},
        json={"action": "start_and_launch_app"},
    )
    released = client.post(
        "/api/admin/lock/release", headers={"X-CSRF-Token": csrf}
    )
    resumed = client.post(
        "/api/admin/queue/resume", headers={"X-CSRF-Token": csrf}
    )

    assert submitted.status_code == 202
    assert released.status_code == 409
    assert resumed.status_code == 409
    assert control.claim_next_task(store) is None

    lifecycle.operation_id = "stale-operation"
    lifecycle.state = DeviceLifecycleState.RUNNING
    assert client.get("/api/admin/devices").status_code == 200
    assert control.has_active_operation is True

    lifecycle.operation_id = "operation-core"
    assert client.get("/api/admin/devices").status_code == 200
    assert control.has_active_operation is False
    assert client.post(
        "/api/admin/lock/release", headers={"X-CSRF-Token": csrf}
    ).status_code == 200
    assert client.post(
        "/api/admin/queue/resume", headers={"X-CSRF-Token": csrf}
    ).status_code == 200
    assert control.claim_next_task(store).task_id == "queued"


def test_disconnect_during_lifecycle_operation_keeps_queue_and_operation_paused() -> None:
    """Session loss never turns an in-flight host action into automatic runner recovery."""
    store = memory_store()
    control = RunnerControl()
    lifecycle = StatefulLifecycle()
    app = operation_app(lifecycle, control, store)
    client = TestClient(app, base_url="https://testserver")
    csrf = login_and_lock(client)
    assert client.post(
        "/api/admin/devices/core_metrics/actions",
        headers={"X-CSRF-Token": csrf},
        json={"action": "shutdown"},
    ).status_code == 202

    assert client.post(
        "/api/admin/session/logout", headers={"X-CSRF-Token": csrf}
    ).status_code == 204

    assert control.queue_paused is True
    assert control.has_active_operation is True
    assert control.claim_next_task(store) is None


@pytest.mark.parametrize("kind", ["memory", "redis"])
def test_terminal_partial_is_not_active_but_intermediate_partial_is(kind: str) -> None:
    """A completed PARTIAL result cannot block maintenance forever."""
    store = (
        memory_store()
        if kind == "memory"
        else RedisStreamsStore(FakeRedis(clock=ManualClock()))
    )
    intermediate = TaskRecord(
        task_id="intermediate", symbol="000001", status=TaskStatus.PARTIAL
    )
    terminal = TaskRecord(
        task_id="terminal",
        symbol="000002",
        status=TaskStatus.PARTIAL,
        completed_at=NOW,
    )
    store.enqueue(intermediate)
    store.enqueue(terminal)

    assert store.has_running_task() is True
    intermediate.completed_at = NOW
    if kind == "redis":
        store.client.set(store._key("intermediate"), store._serialize(intermediate))
    assert store.has_running_task() is False
