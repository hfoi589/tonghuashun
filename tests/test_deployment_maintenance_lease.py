from __future__ import annotations

from datetime import datetime, timezone
import json
from threading import Barrier, Event, Thread, current_thread

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
from level2_service.queue import (
    InMemoryStreams,
    InvalidTransitionError,
    RedisStreamsStore,
)
from level2_service.runner import FakeDeviceBridge, RunnerControl
from tests.test_redis_store import FakeRedis


NOW = datetime(2026, 8, 28, 6, 0, tzinfo=timezone.utc)
OWNER = "deployment-owner-token-abcdefghijklmnopqrstuvwxyz"
OTHER_OWNER = "other-deployment-owner-token-abcdefghijklmnop"
MAINTENANCE_NAMESPACE = "deployment_acceptance"


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


def store_for_kind(kind: str, clock: ManualClock):
    if kind == "memory":
        return memory_store(clock), clock
    redis = FakeRedis(clock=clock)
    return RedisStreamsStore(redis), redis


def persist_task_mutation(store, backing, task_id: str, mutation) -> None:
    task = store.get(task_id)
    assert task is not None
    mutation(task)
    if isinstance(store, RedisStreamsStore):
        backing.set(store._key(task_id), store._serialize(task))


def bind_legacy_task_id(store, backing, task_id: str) -> None:
    if isinstance(store, InMemoryStreams):
        assert store._deployment_lease is not None
        store._deployment_lease["bound_task_id"] = task_id
        return
    payload = backing.get(store._deployment_lease_key)
    assert payload is not None
    lease = json.loads(payload)
    lease["bound_task_id"] = task_id
    backing.set(
        store._deployment_lease_key,
        json.dumps(lease, separators=(",", ":")),
        keepttl=True,
    )


def seed_alias(store, backing, alias: str, target: str) -> None:
    if isinstance(store, InMemoryStreams):
        store._aliases[alias] = target
        return
    backing.hset(store._alias_key, alias, target)


def raw_task_exists(store, backing, task_id: str) -> bool:
    if isinstance(store, InMemoryStreams):
        return task_id in store._tasks
    return backing.get(store._key(task_id)) is not None


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
def test_binding_never_mutates_the_candidate_and_it_is_reusable_by_a_new_owner(
    kind: str,
) -> None:
    """The store owns its maintenance copy; caller state stays an ordinary queued task."""
    clock = ManualClock()
    store, _backing = store_for_kind(kind, clock)
    candidate = TaskRecord(
        task_id="reusable-candidate",
        symbol="601872",
        include_long_capture=False,
    )
    assert store.acquire_deployment_lease(OWNER, 60.0)

    first = store.bind_deployment_acceptance(OWNER, candidate)

    assert first is not None and first.task_id == candidate.task_id
    assert candidate.maintenance_namespace is None
    assert candidate.maintenance_owner_digest is None
    assert candidate.status == TaskStatus.QUEUED
    assert store.bind_deployment_acceptance(OWNER, candidate).task_id == first.task_id
    assert store.release_deployment_lease(OWNER)
    assert store.acquire_deployment_lease(OTHER_OWNER, 60.0)
    second = store.bind_deployment_acceptance(OTHER_OWNER, candidate)
    assert second is not None and second.task_id == candidate.task_id
    assert second.maintenance_owner_digest != first.maintenance_owner_digest
    assert candidate.maintenance_namespace is None
    assert candidate.maintenance_owner_digest is None


@pytest.mark.parametrize("kind", ["memory", "redis"])
def test_owner_rebind_does_not_requeue_intermediate_partial_acceptance(
    kind: str,
) -> None:
    """A PARTIAL task without completed_at is still active and cannot be reset."""
    clock = ManualClock()
    store, _backing = store_for_kind(kind, clock)
    assert store.acquire_deployment_lease(OWNER, 60.0)
    bound = store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="intermediate-partial",
            symbol="601872",
            include_long_capture=False,
        ),
    )
    assert bound is not None
    assert store.next_runnable().task_id == bound.task_id
    partial = store.transition(bound.task_id, TaskStatus.PARTIAL)
    assert partial.completed_at is None

    rebound = store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="ignored-partial-id",
            symbol="601872",
            include_long_capture=False,
        ),
    )

    assert rebound is not None
    assert rebound.task_id == bound.task_id
    assert rebound.status == TaskStatus.PARTIAL
    assert rebound.completed_at is None
    assert store.next_runnable() is None


@pytest.mark.parametrize("kind", ["memory", "redis"])
def test_bound_acceptance_is_namespaced_away_from_public_same_symbol_work(
    kind: str,
) -> None:
    """A public 601872 submission must never reuse or mutate the maintenance task."""
    clock = ManualClock()
    store, _backing = store_for_kind(kind, clock)
    assert store.acquire_deployment_lease(OWNER, 60.0)
    bound = store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="maintenance-601872",
            symbol="601872",
            include_long_capture=False,
        ),
    )
    assert bound is not None
    assert bound.maintenance_namespace == MAINTENANCE_NAMESPACE
    assert bound.maintenance_owner_digest is not None
    assert OWNER not in repr(bound)
    if kind == "redis":
        raw = json.loads(_backing.get(store._key(bound.task_id)))
        assert raw["maintenance"]["namespace"] == MAINTENANCE_NAMESPACE
        assert raw["maintenance"]["owner_digest"] == bound.maintenance_owner_digest

    public = store.submit_or_refresh(
        TaskRecord(
            task_id="public-601872",
            symbol="601872",
            include_long_capture=True,
        )
    )
    store.transition(public.task_id, TaskStatus.FAILED, error_code="PUBLIC_FAILED")
    refreshed = store.refresh_task(public.task_id, include_long_capture=False)

    assert public.task_id == refreshed.task_id == "public-601872"
    assert store.find_by_symbol("601872").task_id == "public-601872"
    maintenance = store.get("maintenance-601872")
    assert maintenance is not None
    assert maintenance.status == TaskStatus.QUEUED
    assert maintenance.include_long_capture is False
    assert maintenance.maintenance_namespace == MAINTENANCE_NAMESPACE


@pytest.mark.parametrize("kind", ["memory", "redis"])
def test_startup_dedup_ignores_terminal_bound_acceptance_and_owner_requeues_it(
    kind: str,
) -> None:
    """Startup symbol repair deduplicates public history without deleting its bound peer."""
    clock = ManualClock()
    store, _backing = store_for_kind(kind, clock)
    older = TaskRecord(
        task_id="public-older", symbol="601872", include_long_capture=False
    )
    older.created_at = older.created_at.replace(year=2024)
    older.updated_at = older.created_at
    newer = TaskRecord(
        task_id="public-newer", symbol="601872", include_long_capture=True
    )
    newer.created_at = newer.created_at.replace(year=2025)
    newer.updated_at = newer.created_at
    store.enqueue(older)
    store.enqueue(newer)
    assert store.acquire_deployment_lease(OWNER, 60.0)
    bound = store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="maintenance-terminal",
            symbol="601872",
            include_long_capture=False,
        ),
    )
    assert bound is not None
    assert store.next_runnable().task_id == "maintenance-terminal"
    store.transition(
        "maintenance-terminal",
        TaskStatus.FAILED,
        error_code="DIRECT_APP_OFFLINE",
    )
    if isinstance(store, RedisStreamsStore):
        store = RedisStreamsStore(_backing)

    result = store.deduplicate_by_symbol()

    assert result == {"total": 2, "kept": 1, "deleted": 1, "aliases": 1}
    terminal = store.get("maintenance-terminal")
    assert terminal is not None and terminal.status == TaskStatus.FAILED
    assert store.find_by_symbol("601872").task_id == "public-newer"
    rebound = store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="discarded-new-id",
            symbol="601872",
            include_long_capture=False,
        ),
    )
    assert rebound is not None
    assert rebound.task_id == "maintenance-terminal"
    assert rebound.status == TaskStatus.QUEUED
    assert store.next_runnable().task_id == "maintenance-terminal"


@pytest.mark.parametrize("kind", ["memory", "redis"])
def test_public_refresh_and_retry_cannot_requeue_a_terminal_bound_acceptance(
    kind: str,
) -> None:
    """Only the lease owner binding path may reset the fixed maintenance task."""
    clock = ManualClock()
    store, _backing = store_for_kind(kind, clock)
    assert store.acquire_deployment_lease(OWNER, 60.0)
    bound = store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="maintenance-retry",
            symbol="601872",
            include_long_capture=False,
        ),
    )
    assert bound is not None
    assert store.next_runnable().task_id == "maintenance-retry"
    store.transition(
        "maintenance-retry", TaskStatus.FAILED, error_code="DIRECT_APP_OFFLINE"
    )

    with pytest.raises(InvalidTransitionError):
        store.refresh_task("maintenance-retry", include_long_capture=True)
    with pytest.raises(InvalidTransitionError):
        store.retry_failed("maintenance-retry")

    terminal = store.get("maintenance-retry")
    assert terminal is not None
    assert terminal.status == TaskStatus.FAILED
    assert terminal.include_long_capture is False
    rebound = store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="new-id-is-ignored",
            symbol="601872",
            include_long_capture=False,
        ),
    )
    assert rebound is not None
    assert rebound.task_id == "maintenance-retry"
    assert rebound.status == TaskStatus.QUEUED


def test_public_retry_endpoint_cannot_mutate_bound_acceptance_task() -> None:
    """The public retry route preserves the maintenance marker and options."""
    store = memory_store()
    assert store.acquire_deployment_lease(OWNER, 60.0)
    bound = store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="maintenance-public-route",
            symbol="601872",
            include_long_capture=False,
        ),
    )
    assert bound is not None
    assert store.next_runnable().task_id == bound.task_id
    store.transition(bound.task_id, TaskStatus.FAILED, error_code="DIRECT_APP_OFFLINE")
    client = TestClient(lease_app(store), base_url="http://testserver")

    response = client.post(f"/api/v1/jobs/{bound.task_id}/retry")

    assert response.status_code == 409
    unchanged = store.get(bound.task_id)
    assert unchanged is not None
    assert unchanged.status == TaskStatus.FAILED
    assert unchanged.include_long_capture is False
    assert unchanged.maintenance_namespace == MAINTENANCE_NAMESPACE


@pytest.mark.parametrize("kind", ["memory", "redis"])
@pytest.mark.parametrize(
    "mutation",
    ["symbol", "capture", "marker", "owner", "task_id", "status"],
)
def test_bound_claim_validates_every_maintenance_invariant(
    kind: str,
    mutation: str,
) -> None:
    """A lease cannot claim a task whose identity, ownership, or state was altered."""
    clock = ManualClock()
    store, backing = store_for_kind(kind, clock)
    assert store.acquire_deployment_lease(OWNER, 60.0)
    bound = store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="maintenance-claim",
            symbol="601872",
            include_long_capture=False,
        ),
    )
    assert bound is not None

    def mutate(task: TaskRecord) -> None:
        if mutation == "symbol":
            task.symbol = "000001"
        elif mutation == "capture":
            task.include_long_capture = True
        elif mutation == "marker":
            task.maintenance_namespace = None
            task.maintenance_owner_digest = None
        elif mutation == "owner":
            task.maintenance_owner_digest = "0" * 64
        elif mutation == "task_id":
            task.task_id = "different-task-id"
        elif mutation == "status":
            task.status = TaskStatus.FAILED

    persist_task_mutation(store, backing, "maintenance-claim", mutate)

    assert store.next_runnable() is None
    stored = store.get("maintenance-claim")
    assert stored is not None
    assert stored.status != TaskStatus.RUNNING


@pytest.mark.parametrize("kind", ["memory", "redis"])
def test_legacy_bound_task_is_migrated_but_unbound_legacy_tasks_stay_ordinary(
    kind: str,
) -> None:
    """The active lease identifies one pre-marker task without reclassifying old history."""
    clock = ManualClock()
    store, backing = store_for_kind(kind, clock)
    assert store.acquire_deployment_lease(OWNER, 60.0)
    store.enqueue(
        TaskRecord(
            task_id="legacy-bound",
            symbol="601872",
            include_long_capture=False,
        )
    )
    bind_legacy_task_id(store, backing, "legacy-bound")

    assert store.find_by_symbol("601872") is None
    public = store.submit_or_refresh(
        TaskRecord(
            task_id="legacy-public",
            symbol="601872",
            include_long_capture=True,
        )
    )
    migrated = store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="ignored-migration-id",
            symbol="601872",
            include_long_capture=False,
        ),
    )

    assert public.task_id == "legacy-public"
    assert migrated is not None and migrated.task_id == "legacy-bound"
    assert migrated.maintenance_namespace == MAINTENANCE_NAMESPACE
    assert store.find_by_symbol("601872").task_id == "legacy-public"


@pytest.mark.parametrize("kind", ["memory", "redis"])
@pytest.mark.parametrize("cleanup_mode", ["release", "expired"])
def test_maintenance_cleanup_removes_only_the_bound_task(
    kind: str,
    cleanup_mode: str,
) -> None:
    """Lease cleanup cannot delete or alias the ordinary 601872 history."""
    clock = ManualClock()
    store, backing = store_for_kind(kind, clock)
    assert store.acquire_deployment_lease(OWNER, 10.0)
    bound = store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="maintenance-cleanup",
            symbol="601872",
            include_long_capture=False,
        ),
    )
    assert bound is not None
    public = store.submit_or_refresh(
        TaskRecord(
            task_id="public-history",
            symbol="601872",
            include_long_capture=True,
        )
    )

    if cleanup_mode == "release":
        assert store.release_deployment_lease(OWNER) is True
    else:
        backing.advance(11.0)
        store.cleanup(NOW)

    assert store.get("maintenance-cleanup") is None
    assert store.get("public-history") is not None
    assert store.find_by_symbol("601872").task_id == public.task_id
    assert store.next_runnable().task_id == "public-history"


@pytest.mark.parametrize("kind", ["memory", "redis"])
@pytest.mark.parametrize("cleanup_mode", ["release", "expired"])
def test_maintenance_cleanup_repairs_aliases_and_rebuilds_canonical_symbol_reuse(
    kind: str,
    cleanup_mode: str,
) -> None:
    """Every public alias/index moves to the same surviving ordinary canonical task."""
    clock = ManualClock()
    store, backing = store_for_kind(kind, clock)
    active = TaskRecord(
        task_id="ordinary-active",
        symbol="601872",
        include_long_capture=False,
    )
    active.created_at = active.created_at.replace(year=2024)
    active.updated_at = active.created_at
    terminal = TaskRecord(
        task_id="ordinary-terminal",
        symbol="601872",
        include_long_capture=True,
        status=TaskStatus.COMPLETED,
    )
    terminal.created_at = terminal.created_at.replace(year=2026)
    terminal.updated_at = terminal.created_at
    terminal.completed_at = terminal.updated_at
    store.enqueue(active)
    store.enqueue(terminal)
    assert store.acquire_deployment_lease(OWNER, 10.0)
    bound = store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="maintenance-alias-target",
            symbol="601872",
            include_long_capture=False,
        ),
    )
    assert bound is not None
    seed_alias(store, backing, "old-public-id", bound.task_id)
    seed_alias(store, backing, "older-public-id", bound.task_id)
    seed_alias(store, backing, bound.task_id, active.task_id)
    if isinstance(store, RedisStreamsStore):
        backing.hset(store._symbol_index_key, "601872", bound.task_id)

    if cleanup_mode == "release":
        assert store.release_deployment_lease(OWNER)
    else:
        backing.advance(11.0)
        store.cleanup(NOW)

    assert store.resolve_task_id("old-public-id") == active.task_id
    assert store.resolve_task_id("older-public-id") == active.task_id
    assert store.resolve_task_id(bound.task_id) == bound.task_id
    assert store.get("old-public-id").task_id == active.task_id
    assert store.find_by_symbol("601872").task_id == active.task_id
    if isinstance(store, RedisStreamsStore):
        assert backing.hget(store._symbol_index_key, "601872") == active.task_id
        assert bound.task_id not in backing.hashes[store._alias_key].values()
    else:
        assert bound.task_id not in store._aliases.values()
    submitted = store.submit_or_refresh(
        TaskRecord(
            task_id="must-not-duplicate-history",
            symbol="601872",
            include_long_capture=True,
        )
    )
    assert submitted.task_id == active.task_id
    assert not raw_task_exists(store, backing, "must-not-duplicate-history")


@pytest.mark.parametrize("kind", ["memory", "redis"])
def test_release_removes_aliases_when_no_ordinary_symbol_history_survives(
    kind: str,
) -> None:
    """A public alias cannot continue resolving to a deleted maintenance ID."""
    clock = ManualClock()
    store, backing = store_for_kind(kind, clock)
    assert store.acquire_deployment_lease(OWNER, 60.0)
    bound = store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="maintenance-only",
            symbol="601872",
            include_long_capture=False,
        ),
    )
    assert bound is not None
    seed_alias(store, backing, "orphan-public-id", bound.task_id)

    assert store.release_deployment_lease(OWNER)

    assert store.resolve_task_id("orphan-public-id") == "orphan-public-id"
    assert store.get("orphan-public-id") is None
    if isinstance(store, RedisStreamsStore):
        assert backing.hget(store._alias_key, "orphan-public-id") is None
        assert backing.hget(store._symbol_index_key, "601872") is None
    else:
        assert "orphan-public-id" not in store._aliases


@pytest.mark.parametrize("kind", ["memory", "redis"])
@pytest.mark.parametrize("cleanup_mode", ["release", "expired"])
def test_maintenance_cleanup_fails_closed_on_raw_payload_id_mismatch(
    kind: str,
    cleanup_mode: str,
) -> None:
    """Cleanup is keyed by the store's raw ID and never trusts a forged payload task_id."""
    clock = ManualClock()
    store, backing = store_for_kind(kind, clock)
    assert store.acquire_deployment_lease(OWNER, 10.0)
    bound = store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="raw-maintenance-id",
            symbol="601872",
            include_long_capture=False,
        ),
    )
    assert bound is not None
    persist_task_mutation(
        store,
        backing,
        bound.task_id,
        lambda task: setattr(task, "task_id", "forged-payload-id"),
    )

    if cleanup_mode == "release":
        assert store.release_deployment_lease(OWNER) is False
    else:
        backing.advance(11.0)
        assert store.cleanup(NOW) == []

    assert raw_task_exists(store, backing, "raw-maintenance-id")
    assert not raw_task_exists(store, backing, "forged-payload-id")


def test_redis_expiry_cleanup_cannot_delete_a_concurrently_bound_new_task() -> None:
    """Lease inspection and orphan cleanup serialize in one Redis operation."""

    class RacingRedis(FakeRedis):
        def __init__(self, *, clock) -> None:
            super().__init__(clock=clock)
            self.cleanup_waiting = Event()
            self.continue_cleanup = Event()
            self.atomic_cleanup_seen = False

        def eval(self, script, key_count, *values):
            if (
                "THS_CLEANUP_EXPIRED_DEPLOYMENT_ACCEPTANCE" in script
                and current_thread().name == "cleanup"
            ):
                self.atomic_cleanup_seen = True
                self.cleanup_waiting.set()
                assert self.continue_cleanup.wait(timeout=2)
            return super().eval(script, key_count, *values)

        def smembers(self, key):
            if (
                not self.atomic_cleanup_seen
                and key == "ths:jobs:tasks"
                and current_thread().name == "cleanup"
            ):
                self.cleanup_waiting.set()
                assert self.continue_cleanup.wait(timeout=2)
            return super().smembers(key)

    clock = ManualClock()
    redis = RacingRedis(clock=clock)
    old_store = RedisStreamsStore(redis)
    assert old_store.acquire_deployment_lease(OWNER, 10.0)
    assert old_store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="expired-old-task",
            symbol="601872",
            include_long_capture=False,
        ),
    )
    redis.advance(11.0)
    cleanup_result: list[TaskRecord] = []

    cleanup_thread = Thread(
        name="cleanup",
        target=lambda: cleanup_result.extend(old_store.cleanup(NOW)),
    )
    cleanup_thread.start()
    assert redis.cleanup_waiting.wait(timeout=2)
    new_store = RedisStreamsStore(redis)
    assert new_store.acquire_deployment_lease(OTHER_OWNER, 60.0)
    new_bound = new_store.bind_deployment_acceptance(
        OTHER_OWNER,
        TaskRecord(
            task_id="newly-bound-task",
            symbol="601872",
            include_long_capture=False,
        ),
    )
    assert new_bound is not None
    redis.continue_cleanup.set()
    cleanup_thread.join(timeout=2)

    assert not cleanup_thread.is_alive()
    assert raw_task_exists(new_store, redis, new_bound.task_id)
    lease = new_store.deployment_lease_status()
    assert lease is not None and lease.bound_task_id == new_bound.task_id


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


@pytest.mark.parametrize("kind", ["memory", "redis"])
def test_repeated_binding_requeues_the_same_terminal_acceptance_task(
    kind: str,
) -> None:
    """A retained lease can retry after repair without creating a second task ID."""
    clock = ManualClock()
    store = (
        memory_store(clock)
        if kind == "memory"
        else RedisStreamsStore(FakeRedis(clock=clock))
    )
    assert store.acquire_deployment_lease(OWNER, 60.0)
    first = store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="acceptance-one", symbol="601872", include_long_capture=False
        ),
    )
    assert first is not None
    assert store.next_runnable().task_id == "acceptance-one"
    store.transition(
        "acceptance-one", TaskStatus.FAILED, error_code="DIRECT_APP_OFFLINE"
    )

    rebound = store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="acceptance-two", symbol="601872", include_long_capture=False
        ),
    )

    assert rebound is not None
    assert rebound.task_id == "acceptance-one"
    assert rebound.status == TaskStatus.QUEUED
    assert rebound.error_code is None
    assert store.next_runnable().task_id == "acceptance-one"


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
