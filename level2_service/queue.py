"""Queue state backed by Redis Streams in production and a deterministic fake in tests."""

from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import json
from pathlib import Path
import re
from threading import RLock
from time import monotonic
from typing import Callable, Protocol

from .models import CaptureKind, CaptureRecord, CaptureStatus, INTRADAY_METRICS, LongCaptureRecord, MetricKind, REQUIRED_METRICS, SOURCE_ERROR_KEYS, TaskRecord, TaskStatus, ValueSource, normalized_intraday_series, utc_now


class QueueFullError(RuntimeError):
    pass


class InvalidTransitionError(ValueError):
    pass


_DEPLOYMENT_OWNER = re.compile(r"[A-Za-z0-9_-]{32,256}")
_MAINTENANCE_OWNER_DIGEST = re.compile(r"[0-9a-f]{64}")
_MAX_DEPLOYMENT_LEASE_SECONDS = 7200.0
_DEPLOYMENT_ACCEPTANCE_NAMESPACE = "deployment_acceptance"


@dataclass(frozen=True)
class DeploymentMaintenanceLease:
    owner_token: str = field(repr=False)
    bound_task_id: str | None
    ttl_seconds: float

    def owned_by(self, candidate: str) -> bool:
        return isinstance(candidate, str) and hmac.compare_digest(
            self.owner_token, candidate
        )


def _valid_lease_request(owner_token: str, ttl_seconds: float) -> bool:
    return (
        isinstance(owner_token, str)
        and _DEPLOYMENT_OWNER.fullmatch(owner_token) is not None
        and isinstance(ttl_seconds, (int, float))
        and 0 < float(ttl_seconds) <= _MAX_DEPLOYMENT_LEASE_SECONDS
    )


def _maintenance_owner_digest(owner_token: str) -> str:
    return hashlib.sha256(owner_token.encode("utf-8")).hexdigest()


def _has_maintenance_metadata(task: TaskRecord) -> bool:
    return (
        task.maintenance_namespace is not None
        or task.maintenance_owner_digest is not None
    )


def _valid_acceptance_task(task: TaskRecord) -> bool:
    return (
        isinstance(task, TaskRecord)
        and task.symbol == "601872"
        and task.include_long_capture is False
        and task.status == TaskStatus.QUEUED
        and task.completed_at is None
        and not _has_maintenance_metadata(task)
    )


def _mark_deployment_acceptance(task: TaskRecord, owner_token: str) -> None:
    task.maintenance_namespace = _DEPLOYMENT_ACCEPTANCE_NAMESPACE
    task.maintenance_owner_digest = _maintenance_owner_digest(owner_token)


def _valid_bound_acceptance(
    task: TaskRecord,
    *,
    bound_task_id: str,
    owner_digest: str,
    require_queued: bool = False,
) -> bool:
    return (
        task.task_id == bound_task_id
        and task.symbol == "601872"
        and task.include_long_capture is False
        and task.maintenance_namespace == _DEPLOYMENT_ACCEPTANCE_NAMESPACE
        and isinstance(task.maintenance_owner_digest, str)
        and _MAINTENANCE_OWNER_DIGEST.fullmatch(task.maintenance_owner_digest)
        is not None
        and hmac.compare_digest(task.maintenance_owner_digest, owner_digest)
        and (not require_queued or task.status == TaskStatus.QUEUED)
    )


def _valid_legacy_bound_acceptance(
    task: TaskRecord, *, bound_task_id: str
) -> bool:
    return (
        task.task_id == bound_task_id
        and task.symbol == "601872"
        and task.include_long_capture is False
        and not _has_maintenance_metadata(task)
    )


def _terminal_acceptance_retryable(task: TaskRecord) -> bool:
    return task.status in {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.EXPIRED,
    } or (
        task.status == TaskStatus.PARTIAL and task.completed_at is not None
    )


class TaskStore(Protocol):
    def enqueue(self, task: TaskRecord) -> None: ...
    def submit_or_refresh(self, task: TaskRecord) -> TaskRecord: ...
    def get(self, task_id: str) -> TaskRecord | None: ...
    def resolve_task_id(self, task_id: str) -> str: ...
    def find_by_symbol(self, symbol: str) -> TaskRecord | None: ...
    def deduplicate_by_symbol(self) -> dict[str, int]: ...
    def queue_position(self, task_id: str) -> int | None: ...
    def next_queued(self) -> TaskRecord | None: ...
    def next_runnable(self) -> TaskRecord | None: ...
    def recover_running(self) -> list[TaskRecord]: ...
    def has_running_task(self) -> bool: ...
    def acquire_deployment_lease(self, owner_token: str, ttl_seconds: float) -> bool: ...
    def renew_deployment_lease(self, owner_token: str, ttl_seconds: float) -> bool: ...
    def deployment_lease_status(self) -> DeploymentMaintenanceLease | None: ...
    def bind_deployment_acceptance(self, owner_token: str, task: TaskRecord) -> TaskRecord | None: ...
    def release_deployment_lease(self, owner_token: str) -> bool: ...
    def requeue_waiting(self, task_id: str) -> TaskRecord: ...
    def retry_failed(self, task_id: str) -> TaskRecord: ...
    def refresh_task(self, task_id: str, include_long_capture: bool | None = None) -> TaskRecord: ...
    def transition(self, task_id: str, status: TaskStatus, *, error_code: str | None = None, source_errors: dict[str, str | None] | None = None) -> TaskRecord: ...
    def complete_capture(self, task_id: str, kind: CaptureKind, path: str) -> TaskRecord: ...
    def complete_result(self, task_id: str, values: dict[MetricKind, str | None], path: str | None, *, ocr_metrics: set[MetricKind] | None = None, source_errors: dict[str, str | None] | None = None, intraday_series: dict[MetricKind, dict[str, object]] | None = None) -> TaskRecord: ...
    def events_after(self, task_id: str, event_index: int = 0) -> list[dict[str, str]]: ...
    def cleanup(self, now: datetime) -> list[TaskRecord]: ...


_ALLOWED_TRANSITIONS = {
    TaskStatus.QUEUED: {TaskStatus.RUNNING, TaskStatus.FAILED},
    TaskStatus.RUNNING: {TaskStatus.WAITING_ADMIN, TaskStatus.PARTIAL, TaskStatus.COMPLETED, TaskStatus.FAILED},
    TaskStatus.WAITING_ADMIN: {TaskStatus.RUNNING, TaskStatus.PARTIAL, TaskStatus.FAILED},
    TaskStatus.PARTIAL: {TaskStatus.RUNNING, TaskStatus.WAITING_ADMIN, TaskStatus.COMPLETED, TaskStatus.FAILED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.EXPIRED: set(),
}

_ACTIVE_STATUSES = {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING_ADMIN}
_REFRESHABLE_STATUSES = {TaskStatus.COMPLETED, TaskStatus.PARTIAL, TaskStatus.FAILED, TaskStatus.EXPIRED}


def _normalized_source_errors(
    source_errors: dict[str, str | None] | None,
) -> dict[str, str | None]:
    provided = source_errors or {}
    return {key: provided.get(key) for key in SOURCE_ERROR_KEYS}


def _reset_task_for_refresh(task: TaskRecord, include_long_capture: bool | None = None) -> None:
    """Clear a terminal result while keeping its public ID for a fresh run."""
    now = utc_now()
    if include_long_capture is not None:
        task.include_long_capture = include_long_capture
    task.status = TaskStatus.QUEUED
    task.created_at = now
    task.updated_at = now
    task.completed_at = None
    task.collected_at = None
    task.error_code = None
    task.source_errors = _normalized_source_errors(None)
    task.captures = {kind: CaptureRecord(kind) for kind in CaptureKind}
    task.values = {kind: None for kind in MetricKind}
    task.value_sources = {kind: None for kind in MetricKind}
    task.intraday_series = normalized_intraday_series(None)
    task.long_capture = LongCaptureRecord(
        status=CaptureStatus.PENDING if task.include_long_capture else CaptureStatus.SKIPPED,
    )


def _canonical_task(tasks: list[TaskRecord]) -> TaskRecord:
    return max(
        tasks,
        key=lambda task: (
            task.status in _ACTIVE_STATUSES,
            task.created_at,
            task.updated_at,
            task.task_id,
        ),
    )


def _restore_expired_task(task: TaskRecord) -> None:
    if task.status != TaskStatus.EXPIRED:
        return
    required_complete = all(task.values[kind] is not None for kind in REQUIRED_METRICS)
    fund_error = task.source_errors.get("main_fund_flow")
    has_result = any(task.values[kind] is not None for kind in REQUIRED_METRICS)
    if required_complete and fund_error is None:
        task.status = TaskStatus.COMPLETED
        task.error_code = None
    elif has_result:
        task.status = TaskStatus.PARTIAL
        task.error_code = fund_error if required_complete else "VALUE_RECOGNITION_FAILED"
    else:
        task.status = TaskStatus.FAILED
        task.error_code = task.error_code or "RESULT_NOT_AVAILABLE"
    task.updated_at = utc_now()


class InMemoryStreams:
    """A small Redis-Streams-compatible state model for local development and tests."""

    def __init__(
        self,
        pending_cap: int = 200,
        capture_root: Path | None = None,
        *,
        lease_clock: Callable[[], float] = monotonic,
    ) -> None:
        self.pending_cap = pending_cap
        self.capture_root = capture_root.resolve() if capture_root else None
        self._tasks: dict[str, TaskRecord] = {}
        self._fifo: deque[str] = deque()
        self._events: dict[str, list[dict[str, str]]] = {}
        self._aliases: dict[str, str] = {}
        self._claim_lock = RLock()
        self._lease_clock = lease_clock
        self._deployment_lease: dict[str, object] | None = None

    def enqueue(self, task: TaskRecord) -> None:
        with self._claim_lock:
            self._enqueue_locked(task)

    def _enqueue_locked(
        self, task: TaskRecord, *, maintenance: bool = False
    ) -> None:
        if maintenance:
            if task.maintenance_namespace != _DEPLOYMENT_ACCEPTANCE_NAMESPACE:
                raise InvalidTransitionError("invalid maintenance task")
        elif _has_maintenance_metadata(task):
            raise InvalidTransitionError(
                "maintenance task requires the maintenance binding path"
            )
        pending = sum(
            item.status in {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING_ADMIN}
            for item in self._tasks.values()
        )
        if pending >= self.pending_cap:
            raise QueueFullError("global pending queue cap reached")
        self._tasks[task.task_id] = task
        self._fifo.append(task.task_id)
        self._emit(task)

    def submit_or_refresh(self, task: TaskRecord) -> TaskRecord:
        if _has_maintenance_metadata(task):
            raise InvalidTransitionError(
                "maintenance task requires the maintenance binding path"
            )
        with self._claim_lock:
            existing = self._find_by_symbol_raw(task.symbol)
            if existing is None:
                self._enqueue_locked(task)
                return task
            if existing.status in _ACTIVE_STATUSES:
                return existing
            return self._refresh_locked(existing, task.include_long_capture)

    def next_queued(self) -> TaskRecord | None:
        return self.next_runnable()

    def next_runnable(self) -> TaskRecord | None:
        with self._claim_lock:
            lease = self._active_deployment_lease_locked()
            if lease is not None:
                bound_task_id = lease.get("bound_task_id")
                owner_digest = lease.get("owner_digest")
                if not isinstance(bound_task_id, str) or not isinstance(
                    owner_digest, str
                ):
                    return None
                task = self._tasks.get(bound_task_id)
                if task is None or not _valid_bound_acceptance(
                    task,
                    bound_task_id=bound_task_id,
                    owner_digest=owner_digest,
                    require_queued=True,
                ):
                    return None
                self._fifo = deque(
                    task_id for task_id in self._fifo if task_id != bound_task_id
                )
                task.status = TaskStatus.RUNNING
                task.updated_at = utc_now()
                self._emit(task)
                return task
            for task_id in self._fifo:
                task = self._tasks.get(task_id)
                if (
                    task
                    and task.status == TaskStatus.QUEUED
                    and not _has_maintenance_metadata(task)
                ):
                    task.status = TaskStatus.RUNNING
                    task.updated_at = utc_now()
                    self._emit(task)
                    return task
        return None

    def get(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(self.resolve_task_id(task_id))

    def resolve_task_id(self, task_id: str) -> str:
        resolved = task_id
        visited = set()
        while resolved in self._aliases and resolved not in visited:
            visited.add(resolved)
            resolved = self._aliases[resolved]
        return resolved

    def find_by_symbol(self, symbol: str) -> TaskRecord | None:
        with self._claim_lock:
            return self._find_by_symbol_raw(symbol)

    def _find_by_symbol_raw(self, symbol: str) -> TaskRecord | None:
        lease = self._active_deployment_lease_locked()
        bound_task_id = lease.get("bound_task_id") if lease is not None else None
        candidates = [
            task
            for task in self._tasks.values()
            if task.symbol == symbol
            and not _has_maintenance_metadata(task)
            and task.task_id != bound_task_id
        ]
        return _canonical_task(candidates) if candidates else None

    def deduplicate_by_symbol(self) -> dict[str, int]:
        with self._claim_lock:
            groups: dict[str, list[TaskRecord]] = defaultdict(list)
            lease = self._active_deployment_lease_locked()
            bound_task_id = (
                lease.get("bound_task_id") if lease is not None else None
            )
            for task in self._tasks.values():
                if (
                    _has_maintenance_metadata(task)
                    or task.task_id == bound_task_id
                ):
                    continue
                _restore_expired_task(task)
                groups[task.symbol].append(task)
            deleted = 0
            aliases = 0
            for tasks in groups.values():
                canonical = _canonical_task(tasks)
                duplicate_ids = {task.task_id for task in tasks if task.task_id != canonical.task_id}
                if not duplicate_ids:
                    continue
                for alias, target in list(self._aliases.items()):
                    if target in duplicate_ids:
                        self._aliases[alias] = canonical.task_id
                for duplicate_id in duplicate_ids:
                    duplicate = self._tasks.pop(duplicate_id)
                    for capture in duplicate.captures.values():
                        self._unlink_capture(capture)
                    self._unlink_capture(duplicate.long_capture)
                    self._events.pop(duplicate_id, None)
                    self._aliases[duplicate_id] = canonical.task_id
                    deleted += 1
                    aliases += 1
                self._fifo = deque(task_id for task_id in self._fifo if task_id not in duplicate_ids)
            return {
                "total": sum(len(tasks) for tasks in groups.values()),
                "kept": len(groups),
                "deleted": deleted,
                "aliases": aliases,
            }

    def recover_running(self) -> list[TaskRecord]:
        recovered = [
            task
            for task in self._tasks.values()
            if task.status == TaskStatus.RUNNING
        ]
        recovered.sort(key=lambda task: task.created_at)
        for task in recovered:
            task.status = TaskStatus.QUEUED
            task.error_code = None
            task.source_errors = _normalized_source_errors(None)
            task.completed_at = None
            task.updated_at = utc_now()
            self._emit(task)
        return recovered

    def has_running_task(self) -> bool:
        with self._claim_lock:
            return any(
                task.status == TaskStatus.RUNNING
                or (
                    task.status == TaskStatus.PARTIAL
                    and task.completed_at is None
                )
                for task in self._tasks.values()
            )

    def acquire_deployment_lease(
        self, owner_token: str, ttl_seconds: float
    ) -> bool:
        if not _valid_lease_request(owner_token, ttl_seconds):
            return False
        with self._claim_lock:
            if self._active_deployment_lease_locked() is not None:
                return False
            if any(
                task.status == TaskStatus.RUNNING
                or (
                    task.status == TaskStatus.PARTIAL
                    and task.completed_at is None
                )
                for task in self._tasks.values()
            ):
                return False
            self._deployment_lease = {
                "owner_token": owner_token,
                "owner_digest": _maintenance_owner_digest(owner_token),
                "bound_task_id": None,
                "expires_at": self._lease_clock() + float(ttl_seconds),
            }
            return True

    def renew_deployment_lease(
        self, owner_token: str, ttl_seconds: float
    ) -> bool:
        if not _valid_lease_request(owner_token, ttl_seconds):
            return False
        with self._claim_lock:
            lease = self._active_deployment_lease_locked()
            if lease is None or not hmac.compare_digest(
                str(lease["owner_token"]), owner_token
            ):
                return False
            owner_digest = _maintenance_owner_digest(owner_token)
            stored_digest = lease.get("owner_digest")
            if stored_digest is not None and not hmac.compare_digest(
                str(stored_digest), owner_digest
            ):
                return False
            lease["owner_digest"] = owner_digest
            lease["expires_at"] = self._lease_clock() + float(ttl_seconds)
            return True

    def deployment_lease_status(self) -> DeploymentMaintenanceLease | None:
        with self._claim_lock:
            lease = self._active_deployment_lease_locked()
            if lease is None:
                return None
            remaining = max(
                0.0, float(lease["expires_at"]) - self._lease_clock()
            )
            return DeploymentMaintenanceLease(
                owner_token=str(lease["owner_token"]),
                bound_task_id=(
                    str(lease["bound_task_id"])
                    if isinstance(lease.get("bound_task_id"), str)
                    else None
                ),
                ttl_seconds=remaining,
            )

    def bind_deployment_acceptance(
        self, owner_token: str, task: TaskRecord
    ) -> TaskRecord | None:
        if not _valid_acceptance_task(task):
            return None
        candidate = deepcopy(task)
        with self._claim_lock:
            lease = self._active_deployment_lease_locked()
            if lease is None or not hmac.compare_digest(
                str(lease["owner_token"]), owner_token
            ):
                return None
            owner_digest = _maintenance_owner_digest(owner_token)
            stored_digest = lease.get("owner_digest")
            if stored_digest is not None and not hmac.compare_digest(
                str(stored_digest), owner_digest
            ):
                return None
            lease["owner_digest"] = owner_digest
            bound_task_id = lease.get("bound_task_id")
            if isinstance(bound_task_id, str):
                existing = self._tasks.get(bound_task_id)
                if existing is None:
                    return None
                if _has_maintenance_metadata(existing):
                    if not _valid_bound_acceptance(
                        existing,
                        bound_task_id=bound_task_id,
                        owner_digest=owner_digest,
                    ):
                        return None
                elif _valid_legacy_bound_acceptance(
                    existing, bound_task_id=bound_task_id
                ):
                    _mark_deployment_acceptance(existing, owner_token)
                else:
                    return None
                if _terminal_acceptance_retryable(existing):
                    try:
                        return self._refresh_locked(existing, False)
                    except QueueFullError:
                        return None
                return existing
            if candidate.task_id in self._tasks:
                return None
            _mark_deployment_acceptance(candidate, owner_token)
            try:
                self._enqueue_locked(candidate, maintenance=True)
            except QueueFullError:
                return None
            lease["bound_task_id"] = candidate.task_id
            return candidate

    def release_deployment_lease(self, owner_token: str) -> bool:
        with self._claim_lock:
            lease = self._active_deployment_lease_locked()
            if lease is None or not isinstance(owner_token, str):
                return False
            if not hmac.compare_digest(str(lease["owner_token"]), owner_token):
                return False
            owner_digest = _maintenance_owner_digest(owner_token)
            stored_digest = lease.get("owner_digest")
            if stored_digest is not None and not hmac.compare_digest(
                str(stored_digest), owner_digest
            ):
                return False
            bound_task_id = lease.get("bound_task_id")
            if isinstance(bound_task_id, str):
                task = self._tasks.get(bound_task_id)
                if task is None:
                    return False
                if _has_maintenance_metadata(task):
                    if not _valid_bound_acceptance(
                        task,
                        bound_task_id=bound_task_id,
                        owner_digest=owner_digest,
                    ):
                        return False
                elif not _valid_legacy_bound_acceptance(
                    task, bound_task_id=bound_task_id
                ):
                    return False
                if (
                    self._remove_maintenance_task_locked(
                        bound_task_id,
                        owner_digest=owner_digest,
                        allow_legacy=True,
                    )
                    is None
                ):
                    return False
            self._deployment_lease = None
            return True

    def _active_deployment_lease_locked(self) -> dict[str, object] | None:
        lease = self._deployment_lease
        if lease is None:
            return None
        if self._lease_clock() >= float(lease["expires_at"]):
            self._deployment_lease = None
            return None
        return lease

    def queue_position(self, task_id: str) -> int | None:
        with self._claim_lock:
            task_id = self.resolve_task_id(task_id)
            task = self._tasks.get(task_id)
            lease = self._active_deployment_lease_locked()
            bound_task_id = (
                lease.get("bound_task_id") if lease is not None else None
            )
            if (
                task is None
                or task.status != TaskStatus.QUEUED
                or _has_maintenance_metadata(task)
                or task.task_id == bound_task_id
            ):
                return None
            position = 0
            for queued_id in self._fifo:
                candidate = self._tasks.get(queued_id)
                if (
                    candidate is None
                    or candidate.status != TaskStatus.QUEUED
                    or _has_maintenance_metadata(candidate)
                    or candidate.task_id == bound_task_id
                ):
                    continue
                position += 1
                if queued_id == task_id:
                    return position
        return None

    def requeue_waiting(self, task_id: str) -> TaskRecord:
        with self._claim_lock:
            task = self._tasks[task_id]
            if self._is_maintenance_task_locked(task):
                raise InvalidTransitionError("maintenance task cannot be requeued")
            if task.status != TaskStatus.WAITING_ADMIN:
                raise InvalidTransitionError(f"{task.status.value} cannot be requeued")
            self._move_to_fifo_tail(task_id)
            task.status = TaskStatus.QUEUED
            task.error_code = None
            task.source_errors = _normalized_source_errors(None)
            task.updated_at = utc_now()
            self._emit(task)
            return task

    def retry_failed(self, task_id: str) -> TaskRecord:
        with self._claim_lock:
            task = self._tasks[task_id]
            if self._is_maintenance_task_locked(task):
                raise InvalidTransitionError("maintenance task cannot be retried")
            if task.status == TaskStatus.QUEUED:
                return task
            if task.status != TaskStatus.FAILED:
                raise InvalidTransitionError(f"{task.status.value} cannot be retried")
            self._move_to_fifo_tail(task_id)
            task.status = TaskStatus.QUEUED
            task.error_code = None
            task.completed_at = None
            task.source_errors = _normalized_source_errors(None)
            task.updated_at = utc_now()
            self._emit(task)
            return task

    def refresh_task(self, task_id: str, include_long_capture: bool | None = None) -> TaskRecord:
        with self._claim_lock:
            task_id = self.resolve_task_id(task_id)
            task = self._tasks[task_id]
            if self._is_maintenance_task_locked(task):
                raise InvalidTransitionError("maintenance task cannot be refreshed")
            if task.status in _ACTIVE_STATUSES:
                return task
            if task.status not in _REFRESHABLE_STATUSES:
                raise InvalidTransitionError(f"{task.status.value} cannot be refreshed")
            return self._refresh_locked(task, include_long_capture)

    def _refresh_locked(self, task: TaskRecord, include_long_capture: bool | None = None) -> TaskRecord:
        task_id = task.task_id
        if task.status in _ACTIVE_STATUSES:
            return task
        if task.status not in _REFRESHABLE_STATUSES:
            raise InvalidTransitionError(f"{task.status.value} cannot be refreshed")
        pending = sum(item.status in _ACTIVE_STATUSES for item in self._tasks.values())
        if pending >= self.pending_cap:
            raise QueueFullError("global pending queue cap reached")
        for capture in task.captures.values():
            self._unlink_capture(capture)
        self._unlink_capture(task.long_capture)
        _reset_task_for_refresh(task, include_long_capture)
        self._move_to_fifo_tail(task_id)
        self._emit(task)
        return task

    def set_capture_root(self, capture_root: Path) -> None:
        self.capture_root = capture_root.resolve()

    def transition(self, task_id: str, status: TaskStatus, *, error_code: str | None = None, source_errors: dict[str, str | None] | None = None) -> TaskRecord:
        task = self._tasks[task_id]
        if status not in _ALLOWED_TRANSITIONS[task.status]:
            raise InvalidTransitionError(f"{task.status.value} cannot transition to {status.value}")
        task.status = status
        task.error_code = error_code
        if source_errors is not None:
            task.source_errors = _normalized_source_errors(source_errors)
        task.updated_at = utc_now()
        if status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            task.completed_at = task.updated_at
        self._emit(task)
        return task

    def complete_capture(self, task_id: str, kind: CaptureKind, path: str) -> TaskRecord:
        task = self._tasks[task_id]
        if task.status not in {TaskStatus.RUNNING, TaskStatus.PARTIAL}:
            raise InvalidTransitionError(f"{task.status.value} cannot accept a capture")
        capture = task.captures[kind]
        capture.status = CaptureStatus.READY
        capture.path = Path(path)
        capture.captured_at = utc_now()
        task.updated_at = capture.captured_at
        ready = sum(item.status == CaptureStatus.READY for item in task.captures.values())
        task.status = TaskStatus.COMPLETED if ready == len(CaptureKind) else TaskStatus.PARTIAL
        if task.status == TaskStatus.COMPLETED:
            task.completed_at = task.updated_at
        self._emit(task)
        return task

    def complete_result(self, task_id: str, values: dict[MetricKind, str | None], path: str | None, *, ocr_metrics: set[MetricKind] | None = None, source_errors: dict[str, str | None] | None = None, intraday_series: dict[MetricKind, dict[str, object]] | None = None) -> TaskRecord:
        task = self._tasks[task_id]
        if task.status not in {TaskStatus.RUNNING, TaskStatus.PARTIAL}:
            raise InvalidTransitionError(f"{task.status.value} cannot accept a result")
        task.values = {kind: values.get(kind) for kind in MetricKind}
        ocr_kinds = ocr_metrics or set()
        task.value_sources = {
            kind: (
                ValueSource.OCR
                if kind in ocr_kinds and task.values[kind] is not None
                else ValueSource.INTERFACE if task.values[kind] is not None else None
            )
            for kind in MetricKind
        }
        task.intraday_series = normalized_intraday_series(intraday_series)
        task.source_errors = _normalized_source_errors(source_errors)
        now = utc_now()
        if task.include_long_capture:
            if path is None:
                raise ValueError("a long capture path is required for this task")
            task.long_capture.status = CaptureStatus.READY
            task.long_capture.path = Path(path)
            task.long_capture.captured_at = now
        else:
            if path is not None:
                raise ValueError("a data-only task cannot accept a long capture path")
            task.long_capture.status = CaptureStatus.SKIPPED
            task.long_capture.path = None
            task.long_capture.captured_at = None
        task.collected_at = now
        task.updated_at = now
        required_complete = all(task.values[kind] is not None for kind in REQUIRED_METRICS)
        fund_error = task.source_errors["main_fund_flow"]
        task.status = (
            TaskStatus.COMPLETED
            if required_complete and fund_error is None
            else TaskStatus.PARTIAL
        )
        task.error_code = (
            None
            if task.status == TaskStatus.COMPLETED
            else fund_error if required_complete else "VALUE_RECOGNITION_FAILED"
        )
        task.completed_at = task.updated_at
        self._emit(task)
        return task

    def events_after(self, task_id: str, event_index: int = 0) -> list[dict[str, str]]:
        return self._events.get(self.resolve_task_id(task_id), [])[event_index:]

    def cleanup(self, now: datetime) -> list[TaskRecord]:
        with self._claim_lock:
            removed: list[TaskRecord] = []
            lease = self._active_deployment_lease_locked()
            bound_task_id = (
                lease.get("bound_task_id") if lease is not None else None
            )
            for raw_task_id, task in list(self._tasks.items()):
                if _has_maintenance_metadata(task):
                    if isinstance(bound_task_id, str) and raw_task_id == bound_task_id:
                        continue
                    removed_task = self._remove_maintenance_task_locked(
                        raw_task_id,
                        owner_digest=None,
                        allow_legacy=False,
                    )
                    if removed_task is not None:
                        removed.append(removed_task)
                    continue
                long_capture = task.long_capture
                if (
                    long_capture.status == CaptureStatus.READY
                    and long_capture.expires_at is not None
                    and now >= long_capture.expires_at
                ):
                    self._unlink_capture(long_capture)
                    long_capture.status = CaptureStatus.EXPIRED
                for capture in task.captures.values():
                    if (
                        capture.status == CaptureStatus.READY
                        and capture.expires_at is not None
                        and now >= capture.expires_at
                    ):
                        self._unlink_capture(capture)
                        capture.status = CaptureStatus.EXPIRED
            return removed

    def _is_maintenance_task_locked(self, task: TaskRecord) -> bool:
        if _has_maintenance_metadata(task):
            return True
        lease = self._active_deployment_lease_locked()
        return lease is not None and lease.get("bound_task_id") == task.task_id

    def _remove_maintenance_task_locked(
        self,
        raw_task_id: str,
        *,
        owner_digest: str | None,
        allow_legacy: bool,
    ) -> TaskRecord | None:
        task = self._tasks.get(raw_task_id)
        if task is None or task.task_id != raw_task_id:
            return None
        if _has_maintenance_metadata(task):
            expected_digest = owner_digest or task.maintenance_owner_digest
            if not isinstance(expected_digest, str) or not _valid_bound_acceptance(
                task,
                bound_task_id=raw_task_id,
                owner_digest=expected_digest,
            ):
                return None
        elif not allow_legacy or not _valid_legacy_bound_acceptance(
            task, bound_task_id=raw_task_id
        ):
            return None
        ordinary_candidates = [
            candidate
            for candidate_raw_id, candidate in self._tasks.items()
            if candidate_raw_id != raw_task_id
            and candidate.task_id == candidate_raw_id
            and candidate.symbol == task.symbol
            and not _has_maintenance_metadata(candidate)
        ]
        canonical = (
            _canonical_task(ordinary_candidates) if ordinary_candidates else None
        )
        for alias, target in list(self._aliases.items()):
            if alias == raw_task_id:
                self._aliases.pop(alias, None)
            elif target == raw_task_id:
                if canonical is None:
                    self._aliases.pop(alias, None)
                else:
                    self._aliases[alias] = canonical.task_id
        self._tasks.pop(raw_task_id, None)
        for capture in task.captures.values():
            self._unlink_capture(capture)
        self._unlink_capture(task.long_capture)
        self._fifo = deque(item for item in self._fifo if item != raw_task_id)
        self._events.pop(raw_task_id, None)
        return task

    def _emit(self, task: TaskRecord) -> None:
        self._events.setdefault(task.task_id, []).append({"event": "status", "data": task.status.value})

    def _move_to_fifo_tail(self, task_id: str) -> None:
        self._fifo = deque(item for item in self._fifo if item != task_id)
        self._fifo.append(task_id)

    def _unlink_capture(self, capture: CaptureRecord | LongCaptureRecord) -> None:
        if capture.path is None or self.capture_root is None:
            return
        path = capture.path.resolve()
        try:
            path.relative_to(self.capture_root)
        except ValueError:
            return
        path.unlink(missing_ok=True)


class RedisStreamsStore:
    """Redis-backed TaskStore using a stream for events and Lua for FIFO claims."""

    _REQUIRED_CLIENT_METHODS = ("delete", "eval", "get", "hdel", "hget", "hgetall", "hset", "lrange", "lrem", "rpush", "sadd", "set", "smembers", "srem", "xadd", "xdel", "xrange")
    _ENQUEUE_SCRIPT = """
-- THS_ENQUEUE
local pending = 0
for _, task_id in ipairs(redis.call('SMEMBERS', KEYS[3])) do
  local payload = redis.call('GET', KEYS[2] .. task_id)
  if payload then
    local task = cjson.decode(payload)
    if task.status == 'QUEUED' or task.status == 'RUNNING' or task.status == 'WAITING_ADMIN' then
      pending = pending + 1
    end
  end
end
if pending >= tonumber(ARGV[1]) then return false end
redis.call('SET', KEYS[2] .. ARGV[2], ARGV[3])
redis.call('SADD', KEYS[3], ARGV[2])
redis.call('RPUSH', KEYS[1], ARGV[2])
redis.call('XADD', KEYS[4], '*', 'event', 'status', 'task_id', ARGV[2], 'data', 'QUEUED')
redis.call('HSET', KEYS[5], ARGV[4], ARGV[2])
return true
"""
    _SUBMIT_OR_REFRESH_SCRIPT = """
-- THS_SUBMIT_OR_REFRESH
local lease_bound_id = false
local lease_payload = redis.call('GET', KEYS[6])
if lease_payload then
  local lease = cjson.decode(lease_payload)
  if lease.bound_task_id and lease.bound_task_id ~= cjson.null then
    lease_bound_id = lease.bound_task_id
  end
end
local existing_id = redis.call('HGET', KEYS[5], ARGV[2])
if existing_id then
  local existing_payload = redis.call('GET', KEYS[2] .. existing_id)
  if existing_payload then
    local existing = cjson.decode(existing_payload)
    local maintenance = existing.maintenance
    if existing_id == lease_bound_id or (maintenance and maintenance ~= cjson.null) then
      redis.call('HDEL', KEYS[5], ARGV[2])
    else
      if existing.status == 'QUEUED' or existing.status == 'RUNNING' or existing.status == 'WAITING_ADMIN' then
        return existing_payload
      end
      local pending = 0
      for _, task_id in ipairs(redis.call('SMEMBERS', KEYS[3])) do
        local payload = redis.call('GET', KEYS[2] .. task_id)
        if payload then
          local queued = cjson.decode(payload)
          if queued.status == 'QUEUED' or queued.status == 'RUNNING' or queued.status == 'WAITING_ADMIN' then
            pending = pending + 1
          end
        end
      end
      if pending >= tonumber(ARGV[1]) then return 'QUEUE_FULL' end
      local refreshed = cjson.decode(ARGV[5])
      refreshed.task_id = existing_id
      refreshed.symbol = ARGV[2]
      local updated = cjson.encode(refreshed)
      redis.call('LREM', KEYS[1], 0, existing_id)
      redis.call('SET', KEYS[2] .. existing_id, updated)
      redis.call('RPUSH', KEYS[1], existing_id)
      redis.call('XADD', KEYS[4], '*', 'event', 'status', 'task_id', existing_id, 'data', 'QUEUED')
      return updated
    end
  else
    redis.call('HDEL', KEYS[5], ARGV[2])
  end
end
local pending = 0
for _, task_id in ipairs(redis.call('SMEMBERS', KEYS[3])) do
  local payload = redis.call('GET', KEYS[2] .. task_id)
  if payload then
    local task = cjson.decode(payload)
    if task.status == 'QUEUED' or task.status == 'RUNNING' or task.status == 'WAITING_ADMIN' then
      pending = pending + 1
    end
  end
end
if pending >= tonumber(ARGV[1]) then return 'QUEUE_FULL' end
redis.call('SET', KEYS[2] .. ARGV[3], ARGV[4])
redis.call('SADD', KEYS[3], ARGV[3])
redis.call('RPUSH', KEYS[1], ARGV[3])
redis.call('HSET', KEYS[5], ARGV[2], ARGV[3])
redis.call('XADD', KEYS[4], '*', 'event', 'status', 'task_id', ARGV[3], 'data', 'QUEUED')
return ARGV[4]
"""
    _CLAIM_SCRIPT = """
-- THS_CLAIM_WITH_DEPLOYMENT_LEASE
local lease_payload = redis.call('GET', KEYS[4])
if lease_payload then
  local lease = cjson.decode(lease_payload)
  local task_id = lease.bound_task_id
  local owner_digest = lease.owner_digest
  if not task_id or task_id == cjson.null or not owner_digest or owner_digest == cjson.null then return false end
  local key = KEYS[2] .. task_id
  local payload = redis.call('GET', key)
  if not payload then return false end
  local task = cjson.decode(payload)
  local maintenance = task.maintenance
  if task.task_id ~= task_id or task.status ~= 'QUEUED' or task.symbol ~= '601872' or task.include_long_capture ~= false or not maintenance or maintenance == cjson.null or maintenance.namespace ~= 'deployment_acceptance' or maintenance.owner_digest ~= owner_digest then
    return false
  end
  task.status = 'RUNNING'
  task.updated_at = ARGV[1]
  local updated = cjson.encode(task)
  redis.call('SET', key, updated)
  redis.call('LREM', KEYS[1], 0, task_id)
  redis.call('XADD', KEYS[3], '*', 'event', 'status', 'task_id', task_id, 'data', 'RUNNING')
  return updated
end
local task_id = redis.call('LPOP', KEYS[1])
while task_id do
  local key = KEYS[2] .. task_id
  local payload = redis.call('GET', key)
  if payload then
    local task = cjson.decode(payload)
    local maintenance = task.maintenance
    if task.status == 'QUEUED' and (not maintenance or maintenance == cjson.null) then
      task.status = 'RUNNING'
      task.updated_at = ARGV[1]
      local updated = cjson.encode(task)
      redis.call('SET', key, updated)
      redis.call('XADD', KEYS[3], '*', 'event', 'status', 'task_id', task_id, 'data', 'RUNNING')
      return updated
    end
  end
  task_id = redis.call('LPOP', KEYS[1])
end
return false
"""
    _ACQUIRE_DEPLOYMENT_LEASE_SCRIPT = """
-- THS_ACQUIRE_DEPLOYMENT_LEASE
if redis.call('GET', KEYS[1]) then return 0 end
for _, task_id in ipairs(redis.call('SMEMBERS', KEYS[3])) do
  local payload = redis.call('GET', KEYS[2] .. task_id)
  if payload then
    local task = cjson.decode(payload)
    if task.status == 'RUNNING' or (task.status == 'PARTIAL' and (not task.completed_at or task.completed_at == cjson.null)) then
      return -1
    end
  end
end
local lease = cjson.encode({owner_token = ARGV[1], owner_digest = ARGV[2], bound_task_id = cjson.null})
local accepted = redis.call('SET', KEYS[1], lease, 'NX', 'PX', ARGV[3])
if accepted then return 1 end
return 0
"""
    _RENEW_DEPLOYMENT_LEASE_SCRIPT = """
-- THS_RENEW_DEPLOYMENT_LEASE
local payload = redis.call('GET', KEYS[1])
if not payload then return 0 end
local lease = cjson.decode(payload)
if lease.owner_token ~= ARGV[1] then return 0 end
if lease.owner_digest and lease.owner_digest ~= cjson.null and lease.owner_digest ~= ARGV[2] then return 0 end
lease.owner_digest = ARGV[2]
redis.call('SET', KEYS[1], cjson.encode(lease), 'KEEPTTL')
return redis.call('PEXPIRE', KEYS[1], ARGV[3])
"""
    _DEPLOYMENT_LEASE_STATUS_SCRIPT = """
-- THS_DEPLOYMENT_LEASE_STATUS
local payload = redis.call('GET', KEYS[1])
if not payload then return false end
local ttl = redis.call('PTTL', KEYS[1])
if ttl <= 0 then return false end
return {payload, ttl}
"""
    _BIND_DEPLOYMENT_ACCEPTANCE_SCRIPT = """
-- THS_BIND_DEPLOYMENT_ACCEPTANCE
local lease_payload = redis.call('GET', KEYS[1])
if not lease_payload then return false end
local lease = cjson.decode(lease_payload)
if lease.owner_token ~= ARGV[1] then return false end
if lease.owner_digest and lease.owner_digest ~= cjson.null and lease.owner_digest ~= ARGV[2] then return false end
lease.owner_digest = ARGV[2]
local candidate = cjson.decode(ARGV[4])
local candidate_maintenance = candidate.maintenance
if candidate.task_id ~= ARGV[3] or candidate.symbol ~= '601872' or candidate.include_long_capture ~= false or candidate.status ~= 'QUEUED' or not candidate_maintenance or candidate_maintenance == cjson.null or candidate_maintenance.namespace ~= 'deployment_acceptance' or candidate_maintenance.owner_digest ~= ARGV[2] then
  return false
end
redis.call('SET', KEYS[1], cjson.encode(lease), 'KEEPTTL')
if lease.bound_task_id and lease.bound_task_id ~= cjson.null then
  local existing = redis.call('GET', KEYS[3] .. lease.bound_task_id)
  if not existing then return false end
  local existing_task = cjson.decode(existing)
  if existing_task.task_id ~= lease.bound_task_id or existing_task.symbol ~= '601872' or existing_task.include_long_capture ~= false then
    return false
  end
  local existing_maintenance = existing_task.maintenance
  if existing_maintenance and existing_maintenance ~= cjson.null then
    if existing_maintenance.namespace ~= 'deployment_acceptance' or existing_maintenance.owner_digest ~= ARGV[2] then return false end
  else
    existing_task.maintenance = candidate_maintenance
    existing = cjson.encode(existing_task)
    redis.call('SET', KEYS[3] .. lease.bound_task_id, existing)
  end
  if redis.call('HGET', KEYS[6], existing_task.symbol) == lease.bound_task_id then
    redis.call('HDEL', KEYS[6], existing_task.symbol)
  end
  if existing_task.status == 'COMPLETED' or existing_task.status == 'FAILED' or existing_task.status == 'EXPIRED' or (existing_task.status == 'PARTIAL' and existing_task.completed_at and existing_task.completed_at ~= cjson.null) then
    local pending = 0
    for _, task_id in ipairs(redis.call('SMEMBERS', KEYS[4])) do
      local payload = redis.call('GET', KEYS[3] .. task_id)
      if payload then
        local queued = cjson.decode(payload)
        if queued.status == 'QUEUED' or queued.status == 'RUNNING' or queued.status == 'WAITING_ADMIN' then
          pending = pending + 1
        end
      end
    end
    if pending >= tonumber(ARGV[6]) then return 'QUEUE_FULL' end
    local refreshed = candidate
    refreshed.task_id = lease.bound_task_id
    local updated = cjson.encode(refreshed)
    redis.call('SET', KEYS[3] .. lease.bound_task_id, updated)
    redis.call('LREM', KEYS[2], 0, lease.bound_task_id)
    redis.call('RPUSH', KEYS[2], lease.bound_task_id)
    redis.call('SADD', KEYS[4], lease.bound_task_id)
    redis.call('XADD', KEYS[5], '*', 'event', 'status', 'task_id', lease.bound_task_id, 'data', 'QUEUED')
    return updated
  end
  return existing
end
if redis.call('GET', KEYS[3] .. ARGV[3]) then return false end
local pending = 0
for _, task_id in ipairs(redis.call('SMEMBERS', KEYS[4])) do
  local payload = redis.call('GET', KEYS[3] .. task_id)
  if payload then
    local task = cjson.decode(payload)
    if task.status == 'QUEUED' or task.status == 'RUNNING' or task.status == 'WAITING_ADMIN' then
      pending = pending + 1
    end
  end
end
if pending >= tonumber(ARGV[6]) then return 'QUEUE_FULL' end
redis.call('SET', KEYS[3] .. ARGV[3], ARGV[4])
redis.call('SADD', KEYS[4], ARGV[3])
redis.call('RPUSH', KEYS[2], ARGV[3])
redis.call('XADD', KEYS[5], '*', 'event', 'status', 'task_id', ARGV[3], 'data', 'QUEUED')
lease.bound_task_id = ARGV[3]
redis.call('SET', KEYS[1], cjson.encode(lease), 'KEEPTTL')
return ARGV[4]
"""
    _RELEASE_DEPLOYMENT_LEASE_SCRIPT = """
-- THS_RELEASE_DEPLOYMENT_LEASE
local function is_active(status)
  return status == 'QUEUED' or status == 'RUNNING' or status == 'WAITING_ADMIN'
end
local function better(candidate_id, candidate, best_id, best)
  if not best then return true end
  local candidate_active = is_active(candidate.status)
  local best_active = is_active(best.status)
  if candidate_active ~= best_active then return candidate_active end
  local candidate_created = candidate.created_at or ''
  local best_created = best.created_at or ''
  if candidate_created ~= best_created then return candidate_created > best_created end
  local candidate_updated = candidate.updated_at or ''
  local best_updated = best.updated_at or ''
  if candidate_updated ~= best_updated then return candidate_updated > best_updated end
  return candidate_id > best_id
end
local function ordinary_canonical(symbol, removed_id)
  local best_id = false
  local best = false
  for _, raw_id in ipairs(redis.call('SMEMBERS', KEYS[4])) do
    if raw_id ~= removed_id then
      local candidate_payload = redis.call('GET', KEYS[3] .. raw_id)
      if candidate_payload then
        local ok, candidate = pcall(cjson.decode, candidate_payload)
        if ok and type(candidate) == 'table' and candidate.task_id == raw_id and candidate.symbol == symbol then
          local maintenance = candidate.maintenance
          if not maintenance or maintenance == cjson.null then
            if better(raw_id, candidate, best_id, best) then
              best_id = raw_id
              best = candidate
            end
          end
        end
      end
    end
  end
  return best_id
end
local function remove_task(raw_id, task)
  local canonical_id = ordinary_canonical(task.symbol, raw_id)
  local aliases = redis.call('HGETALL', KEYS[7])
  for index = 1, #aliases, 2 do
    local alias = aliases[index]
    local target = aliases[index + 1]
    if alias == raw_id then
      redis.call('HDEL', KEYS[7], alias)
    elseif target == raw_id then
      if canonical_id then
        redis.call('HSET', KEYS[7], alias, canonical_id)
      else
        redis.call('HDEL', KEYS[7], alias)
      end
    end
  end
  if canonical_id then
    redis.call('HSET', KEYS[6], task.symbol, canonical_id)
  else
    redis.call('HDEL', KEYS[6], task.symbol)
  end
  redis.call('LREM', KEYS[2], 0, raw_id)
  redis.call('DEL', KEYS[3] .. raw_id)
  redis.call('SREM', KEYS[4], raw_id)
  for _, entry in ipairs(redis.call('XRANGE', KEYS[5], '-', '+')) do
    local fields = entry[2]
    for index = 1, #fields, 2 do
      if fields[index] == 'task_id' and fields[index + 1] == raw_id then
        redis.call('XDEL', KEYS[5], entry[1])
        break
      end
    end
  end
end
local payload = redis.call('GET', KEYS[1])
if not payload then return 0 end
local lease_ok, lease = pcall(cjson.decode, payload)
if not lease_ok or type(lease) ~= 'table' then return 0 end
if lease.owner_token ~= ARGV[1] then return 0 end
if string.len(ARGV[2]) ~= 64 or not string.match(ARGV[2], '^[0-9a-f]+$') then return 0 end
if lease.owner_digest and lease.owner_digest ~= cjson.null and lease.owner_digest ~= ARGV[2] then return 0 end
local task_id = lease.bound_task_id
if task_id and task_id ~= cjson.null then
  local task_payload = redis.call('GET', KEYS[3] .. task_id)
  if not task_payload then return 0 end
  local task_ok, task = pcall(cjson.decode, task_payload)
  if not task_ok or type(task) ~= 'table' then return 0 end
  if task.task_id ~= task_id or task.symbol ~= '601872' or task.include_long_capture ~= false then return 0 end
  local maintenance = task.maintenance
  if maintenance and maintenance ~= cjson.null then
    if maintenance.namespace ~= 'deployment_acceptance' or maintenance.owner_digest ~= ARGV[2] then return 0 end
  end
  remove_task(task_id, task)
end
return redis.call('DEL', KEYS[1])
"""
    _CLEANUP_EXPIRED_DEPLOYMENT_ACCEPTANCE_SCRIPT = """
-- THS_CLEANUP_EXPIRED_DEPLOYMENT_ACCEPTANCE
if redis.call('GET', KEYS[1]) then return {} end
local function is_active(status)
  return status == 'QUEUED' or status == 'RUNNING' or status == 'WAITING_ADMIN'
end
local function better(candidate_id, candidate, best_id, best)
  if not best then return true end
  local candidate_active = is_active(candidate.status)
  local best_active = is_active(best.status)
  if candidate_active ~= best_active then return candidate_active end
  local candidate_created = candidate.created_at or ''
  local best_created = best.created_at or ''
  if candidate_created ~= best_created then return candidate_created > best_created end
  local candidate_updated = candidate.updated_at or ''
  local best_updated = best.updated_at or ''
  if candidate_updated ~= best_updated then return candidate_updated > best_updated end
  return candidate_id > best_id
end
local function ordinary_canonical(symbol, removed_id)
  local best_id = false
  local best = false
  for _, raw_id in ipairs(redis.call('SMEMBERS', KEYS[4])) do
    if raw_id ~= removed_id then
      local candidate_payload = redis.call('GET', KEYS[3] .. raw_id)
      if candidate_payload then
        local ok, candidate = pcall(cjson.decode, candidate_payload)
        if ok and type(candidate) == 'table' and candidate.task_id == raw_id and candidate.symbol == symbol then
          local maintenance = candidate.maintenance
          if not maintenance or maintenance == cjson.null then
            if better(raw_id, candidate, best_id, best) then
              best_id = raw_id
              best = candidate
            end
          end
        end
      end
    end
  end
  return best_id
end
local function remove_task(raw_id, task)
  local canonical_id = ordinary_canonical(task.symbol, raw_id)
  local aliases = redis.call('HGETALL', KEYS[7])
  for index = 1, #aliases, 2 do
    local alias = aliases[index]
    local target = aliases[index + 1]
    if alias == raw_id then
      redis.call('HDEL', KEYS[7], alias)
    elseif target == raw_id then
      if canonical_id then
        redis.call('HSET', KEYS[7], alias, canonical_id)
      else
        redis.call('HDEL', KEYS[7], alias)
      end
    end
  end
  if canonical_id then
    redis.call('HSET', KEYS[6], task.symbol, canonical_id)
  else
    redis.call('HDEL', KEYS[6], task.symbol)
  end
  redis.call('LREM', KEYS[2], 0, raw_id)
  redis.call('DEL', KEYS[3] .. raw_id)
  redis.call('SREM', KEYS[4], raw_id)
  for _, entry in ipairs(redis.call('XRANGE', KEYS[5], '-', '+')) do
    local fields = entry[2]
    for index = 1, #fields, 2 do
      if fields[index] == 'task_id' and fields[index + 1] == raw_id then
        redis.call('XDEL', KEYS[5], entry[1])
        break
      end
    end
  end
end
local targets = {}
for _, raw_id in ipairs(redis.call('SMEMBERS', KEYS[4])) do
  local task_payload = redis.call('GET', KEYS[3] .. raw_id)
  if task_payload then
    local task_ok, task = pcall(cjson.decode, task_payload)
    if task_ok and type(task) == 'table' then
      local maintenance = task.maintenance
      if maintenance and maintenance ~= cjson.null then
        if task.task_id ~= raw_id or task.symbol ~= '601872' or task.include_long_capture ~= false or maintenance.namespace ~= 'deployment_acceptance' or type(maintenance.owner_digest) ~= 'string' or string.len(maintenance.owner_digest) ~= 64 or not string.match(maintenance.owner_digest, '^[0-9a-f]+$') then
          return 'INVALID'
        end
        targets[#targets + 1] = {raw_id = raw_id, payload = task_payload, task = task}
      end
    end
  end
end
local removed = {}
for index, entry in ipairs(targets) do
  remove_task(entry.raw_id, entry.task)
  removed[index] = entry.payload
end
return removed
"""
    _RECOVER_RUNNING_SCRIPT = """
-- THS_RECOVER_RUNNING
local recovered = {}
for _, task_id in ipairs(redis.call('SMEMBERS', KEYS[3])) do
  local key = KEYS[2] .. task_id
  local payload = redis.call('GET', key)
  if payload then
    local task = cjson.decode(payload)
    if task.status == 'RUNNING' then
      table.insert(recovered, {task_id = task_id, created_at = task.created_at or '', task = task})
    end
  end
end
table.sort(recovered, function(left, right)
  if left.created_at == right.created_at then return left.task_id < right.task_id end
  return left.created_at < right.created_at
end)
local updated = {}
for index, entry in ipairs(recovered) do
  entry.task.status = 'QUEUED'
  entry.task.error_code = cjson.null
  entry.task.source_errors = {core_metrics = cjson.null, main_fund_flow = cjson.null}
  entry.task.completed_at = cjson.null
  entry.task.updated_at = ARGV[1]
  local payload = cjson.encode(entry.task)
  redis.call('SET', KEYS[2] .. entry.task_id, payload)
  redis.call('XADD', KEYS[4], '*', 'event', 'status', 'task_id', entry.task_id, 'data', 'QUEUED')
  updated[index] = payload
end
for index = #recovered, 1, -1 do
  redis.call('LPUSH', KEYS[1], recovered[index].task_id)
end
return updated
"""
    _REQUEUE_WAITING_SCRIPT = """
-- THS_REQUEUE_WAITING
local payload = redis.call('GET', KEYS[2] .. ARGV[1])
if not payload then return false end
local task = cjson.decode(payload)
local maintenance = task.maintenance
if maintenance and maintenance ~= cjson.null then return 'MAINTENANCE' end
local lease_payload = redis.call('GET', KEYS[4])
if lease_payload then
  local lease = cjson.decode(lease_payload)
  if lease.bound_task_id == ARGV[1] then return 'MAINTENANCE' end
end
if task.status ~= 'WAITING_ADMIN' then return false end
task.status = 'QUEUED'
task.error_code = cjson.null
task.source_errors = {core_metrics = cjson.null, main_fund_flow = cjson.null}
task.updated_at = ARGV[2]
local updated = cjson.encode(task)
redis.call('SET', KEYS[2] .. ARGV[1], updated)
redis.call('RPUSH', KEYS[1], ARGV[1])
redis.call('XADD', KEYS[3], '*', 'event', 'status', 'task_id', ARGV[1], 'data', 'QUEUED')
return updated
"""
    _RETRY_FAILED_SCRIPT = """
-- THS_RETRY_FAILED
local payload = redis.call('GET', KEYS[2] .. ARGV[1])
if not payload then return false end
local task = cjson.decode(payload)
local maintenance = task.maintenance
if maintenance and maintenance ~= cjson.null then return 'MAINTENANCE' end
local lease_payload = redis.call('GET', KEYS[4])
if lease_payload then
  local lease = cjson.decode(lease_payload)
  if lease.bound_task_id == ARGV[1] then return 'MAINTENANCE' end
end
if task.status == 'QUEUED' then return payload end
if task.status ~= 'FAILED' then return false end
task.status = 'QUEUED'
task.error_code = cjson.null
task.source_errors = {core_metrics = cjson.null, main_fund_flow = cjson.null}
task.completed_at = cjson.null
task.updated_at = ARGV[2]
local updated = cjson.encode(task)
redis.call('SET', KEYS[2] .. ARGV[1], updated)
redis.call('RPUSH', KEYS[1], ARGV[1])
redis.call('XADD', KEYS[3], '*', 'event', 'status', 'task_id', ARGV[1], 'data', 'QUEUED')
return updated
"""
    _REFRESH_TASK_SCRIPT = """
-- THS_REFRESH_TASK
local key = KEYS[2] .. ARGV[1]
local payload = redis.call('GET', key)
if not payload then return false end
local current = cjson.decode(payload)
local maintenance = current.maintenance
if maintenance and maintenance ~= cjson.null then return 'MAINTENANCE' end
local lease_payload = redis.call('GET', KEYS[5])
if lease_payload then
  local lease = cjson.decode(lease_payload)
  if lease.bound_task_id == ARGV[1] then return 'MAINTENANCE' end
end
if current.status == 'QUEUED' or current.status == 'RUNNING' or current.status == 'WAITING_ADMIN' then
  return payload
end
if current.status ~= 'COMPLETED' and current.status ~= 'PARTIAL' and current.status ~= 'FAILED' and current.status ~= 'EXPIRED' then
  return false
end
local pending = 0
for _, task_id in ipairs(redis.call('SMEMBERS', KEYS[4])) do
  local existing = redis.call('GET', KEYS[2] .. task_id)
  if existing then
    local task = cjson.decode(existing)
    if task.status == 'QUEUED' or task.status == 'RUNNING' or task.status == 'WAITING_ADMIN' then
      pending = pending + 1
    end
  end
end
if pending >= tonumber(ARGV[3]) then return 'QUEUE_FULL' end
redis.call('SET', key, ARGV[2])
redis.call('RPUSH', KEYS[1], ARGV[1])
redis.call('SADD', KEYS[4], ARGV[1])
redis.call('XADD', KEYS[3], '*', 'event', 'status', 'task_id', ARGV[1], 'data', 'QUEUED')
return ARGV[2]
"""

    def __init__(self, client: object, stream: str = "ths:jobs", pending_cap: int = 200, capture_root: Path | None = None) -> None:
        missing = [name for name in self._REQUIRED_CLIENT_METHODS if not callable(getattr(client, name, None))]
        if missing:
            raise TypeError(f"RedisStreamsStore requires redis client methods: {', '.join(missing)}")
        self.client = client
        self.stream = stream
        self.pending_cap = pending_cap
        self.capture_root = capture_root.resolve() if capture_root else None
        self._queue_key = f"{stream}:pending"
        self._index_key = f"{stream}:tasks"
        self._prefix = f"{stream}:task:"
        self._event_stream = f"{stream}:events"
        self._symbol_index_key = f"{stream}:symbols"
        self._alias_key = f"{stream}:aliases"
        self._deployment_lease_key = f"{stream}:deployment-maintenance"

    def set_capture_root(self, capture_root: Path) -> None:
        self.capture_root = capture_root.resolve()

    def enqueue(self, task: TaskRecord) -> None:
        if _has_maintenance_metadata(task):
            raise InvalidTransitionError(
                "maintenance task requires the maintenance binding path"
            )
        accepted = self.client.eval(
            self._ENQUEUE_SCRIPT,
            5,
            self._queue_key,
            self._prefix,
            self._index_key,
            self._event_stream,
            self._symbol_index_key,
            self.pending_cap,
            task.task_id,
            self._serialize(task),
            task.symbol,
        )
        if not accepted:
            raise QueueFullError("global pending queue cap reached")

    def submit_or_refresh(self, task: TaskRecord) -> TaskRecord:
        if _has_maintenance_metadata(task):
            raise InvalidTransitionError(
                "maintenance task requires the maintenance binding path"
            )
        payload = self.client.eval(
            self._SUBMIT_OR_REFRESH_SCRIPT,
            6,
            self._queue_key,
            self._prefix,
            self._index_key,
            self._event_stream,
            self._symbol_index_key,
            self._deployment_lease_key,
            self.pending_cap,
            task.symbol,
            task.task_id,
            self._serialize(task),
            self._serialize(task),
        )
        marker = self._text(payload) if payload else ""
        if marker == "QUEUE_FULL":
            raise QueueFullError("global pending queue cap reached")
        if not payload:
            raise RuntimeError("task submission failed")
        return self._deserialize(payload)

    def get(self, task_id: str) -> TaskRecord | None:
        payload = self.client.get(self._key(self.resolve_task_id(task_id)))
        return self._deserialize(payload) if payload else None

    def _get_raw(self, task_id: str) -> TaskRecord | None:
        payload = self.client.get(self._key(task_id))
        return self._deserialize(payload) if payload else None

    def resolve_task_id(self, task_id: str) -> str:
        resolved = task_id
        visited = set()
        while resolved not in visited:
            visited.add(resolved)
            target = self.client.hget(self._alias_key, resolved)
            if not target:
                break
            resolved = self._text(target)
        return resolved

    def find_by_symbol(self, symbol: str) -> TaskRecord | None:
        lease = self.deployment_lease_status()
        bound_task_id = lease.bound_task_id if lease is not None else None
        indexed = self.client.hget(self._symbol_index_key, symbol)
        if indexed:
            indexed_id = self._text(indexed)
            task = self._get_raw(indexed_id)
            if (
                task is not None
                and not _has_maintenance_metadata(task)
                and task.task_id != bound_task_id
            ):
                return task
            self.client.hdel(self._symbol_index_key, symbol)
        candidates = []
        for raw_task_id in self.client.smembers(self._index_key):
            task = self._get_raw(self._text(raw_task_id))
            if (
                task is not None
                and task.symbol == symbol
                and not _has_maintenance_metadata(task)
                and task.task_id != bound_task_id
            ):
                candidates.append(task)
        result = _canonical_task(candidates) if candidates else None
        if result is not None:
            self.client.hset(self._symbol_index_key, result.symbol, result.task_id)
        return result

    def deduplicate_by_symbol(self) -> dict[str, int]:
        groups: dict[str, list[TaskRecord]] = defaultdict(list)
        lease = self.deployment_lease_status()
        bound_task_id = lease.bound_task_id if lease is not None else None
        excluded_ids: dict[str, set[str]] = defaultdict(set)
        for raw_task_id in list(self.client.smembers(self._index_key)):
            task = self._get_raw(self._text(raw_task_id))
            if task is None:
                self.client.srem(self._index_key, self._text(raw_task_id))
                continue
            if (
                _has_maintenance_metadata(task)
                or task.task_id == bound_task_id
            ):
                excluded_ids[task.symbol].add(task.task_id)
                continue
            _restore_expired_task(task)
            groups[task.symbol].append(task)
        deleted = 0
        aliases = 0
        existing_aliases = {
            self._text(alias): self._text(target)
            for alias, target in self.client.hgetall(self._alias_key).items()
        }
        events = list(self.client.xrange(self._event_stream, "-", "+"))
        for symbol, tasks in groups.items():
            canonical = _canonical_task(tasks)
            self._save(canonical)
            self.client.hset(self._symbol_index_key, symbol, canonical.task_id)
            duplicate_ids = {task.task_id for task in tasks if task.task_id != canonical.task_id}
            if not duplicate_ids:
                continue
            for alias, target in existing_aliases.items():
                if target in duplicate_ids:
                    self.client.hset(self._alias_key, alias, canonical.task_id)
            event_ids = []
            for event_id, fields in events:
                normalized = {self._text(key): self._text(value) for key, value in fields.items()}
                if normalized.get("task_id") in duplicate_ids:
                    event_ids.append(event_id)
            if event_ids:
                self.client.xdel(self._event_stream, *event_ids)
            for duplicate in tasks:
                if duplicate.task_id == canonical.task_id:
                    continue
                for capture in duplicate.captures.values():
                    self._unlink_capture(capture)
                self._unlink_capture(duplicate.long_capture)
                self.client.lrem(self._queue_key, 0, duplicate.task_id)
                self.client.delete(self._key(duplicate.task_id))
                self.client.srem(self._index_key, duplicate.task_id)
                self.client.hset(self._alias_key, duplicate.task_id, canonical.task_id)
                deleted += 1
                aliases += 1
        for symbol, task_ids in excluded_ids.items():
            if symbol in groups:
                continue
            indexed = self.client.hget(self._symbol_index_key, symbol)
            if indexed and self._text(indexed) in task_ids:
                self.client.hdel(self._symbol_index_key, symbol)
        return {
            "total": sum(len(tasks) for tasks in groups.values()),
            "kept": len(groups),
            "deleted": deleted,
            "aliases": aliases,
        }

    def queue_position(self, task_id: str) -> int | None:
        task_id = self.resolve_task_id(task_id)
        task = self.get(task_id)
        lease = self.deployment_lease_status()
        bound_task_id = lease.bound_task_id if lease is not None else None
        if (
            task is None
            or task.status != TaskStatus.QUEUED
            or _has_maintenance_metadata(task)
            or task.task_id == bound_task_id
        ):
            return None
        position = 0
        for raw_task_id in self.client.lrange(self._queue_key, 0, -1):
            queued_id = self._text(raw_task_id)
            candidate = self.get(queued_id)
            if (
                candidate is None
                or candidate.status != TaskStatus.QUEUED
                or _has_maintenance_metadata(candidate)
                or candidate.task_id == bound_task_id
            ):
                continue
            position += 1
            if queued_id == task_id:
                return position
        return None

    def next_queued(self) -> TaskRecord | None:
        return self.next_runnable()

    def next_runnable(self) -> TaskRecord | None:
        payload = self.client.eval(
            self._CLAIM_SCRIPT,
            4,
            self._queue_key,
            self._prefix,
            self._event_stream,
            self._deployment_lease_key,
            utc_now().isoformat(),
        )
        return self._deserialize(payload) if payload else None

    def recover_running(self) -> list[TaskRecord]:
        payloads = self.client.eval(
            self._RECOVER_RUNNING_SCRIPT,
            4,
            self._queue_key,
            self._prefix,
            self._index_key,
            self._event_stream,
            utc_now().isoformat(),
        )
        return [self._deserialize(payload) for payload in (payloads or [])]

    def has_running_task(self) -> bool:
        for raw_task_id in self.client.smembers(self._index_key):
            payload = self.client.get(self._key(self._text(raw_task_id)))
            if not payload:
                continue
            try:
                task = self._deserialize(payload)
            except (KeyError, TypeError, ValueError):
                continue
            if task.status == TaskStatus.RUNNING or (
                task.status == TaskStatus.PARTIAL
                and task.completed_at is None
            ):
                return True
        return False

    def acquire_deployment_lease(
        self, owner_token: str, ttl_seconds: float
    ) -> bool:
        if not _valid_lease_request(owner_token, ttl_seconds):
            return False
        result = self.client.eval(
            self._ACQUIRE_DEPLOYMENT_LEASE_SCRIPT,
            3,
            self._deployment_lease_key,
            self._prefix,
            self._index_key,
            owner_token,
            _maintenance_owner_digest(owner_token),
            max(1, int(float(ttl_seconds) * 1000)),
        )
        return result == 1

    def renew_deployment_lease(
        self, owner_token: str, ttl_seconds: float
    ) -> bool:
        if not _valid_lease_request(owner_token, ttl_seconds):
            return False
        result = self.client.eval(
            self._RENEW_DEPLOYMENT_LEASE_SCRIPT,
            1,
            self._deployment_lease_key,
            owner_token,
            _maintenance_owner_digest(owner_token),
            max(1, int(float(ttl_seconds) * 1000)),
        )
        return result == 1

    def deployment_lease_status(self) -> DeploymentMaintenanceLease | None:
        result = self.client.eval(
            self._DEPLOYMENT_LEASE_STATUS_SCRIPT,
            1,
            self._deployment_lease_key,
        )
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            return None
        try:
            document = json.loads(self._text(result[0]))
            ttl_seconds = float(result[1]) / 1000.0
            owner_token = document["owner_token"]
            bound_task_id = document.get("bound_task_id")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            not isinstance(owner_token, str)
            or _DEPLOYMENT_OWNER.fullmatch(owner_token) is None
            or (bound_task_id is not None and not isinstance(bound_task_id, str))
            or ttl_seconds <= 0
        ):
            return None
        return DeploymentMaintenanceLease(
            owner_token=owner_token,
            bound_task_id=bound_task_id,
            ttl_seconds=ttl_seconds,
        )

    def bind_deployment_acceptance(
        self, owner_token: str, task: TaskRecord
    ) -> TaskRecord | None:
        if not _valid_acceptance_task(task) or not isinstance(owner_token, str):
            return None
        owner_digest = _maintenance_owner_digest(owner_token)
        candidate = deepcopy(task)
        _mark_deployment_acceptance(candidate, owner_token)
        payload = self.client.eval(
            self._BIND_DEPLOYMENT_ACCEPTANCE_SCRIPT,
            6,
            self._deployment_lease_key,
            self._queue_key,
            self._prefix,
            self._index_key,
            self._event_stream,
            self._symbol_index_key,
            owner_token,
            owner_digest,
            candidate.task_id,
            self._serialize(candidate),
            candidate.symbol,
            self.pending_cap,
        )
        if not payload or self._text(payload) == "QUEUE_FULL":
            return None
        try:
            bound = self._deserialize(payload)
        except (KeyError, TypeError, ValueError):
            return None
        if not _valid_bound_acceptance(
            bound,
            bound_task_id=bound.task_id,
            owner_digest=owner_digest,
        ):
            return None
        return bound

    def release_deployment_lease(self, owner_token: str) -> bool:
        if not isinstance(owner_token, str):
            return False
        result = self.client.eval(
            self._RELEASE_DEPLOYMENT_LEASE_SCRIPT,
            7,
            self._deployment_lease_key,
            self._queue_key,
            self._prefix,
            self._index_key,
            self._event_stream,
            self._symbol_index_key,
            self._alias_key,
            owner_token,
            _maintenance_owner_digest(owner_token),
        )
        return result == 1

    def requeue_waiting(self, task_id: str) -> TaskRecord:
        payload = self.client.eval(
            self._REQUEUE_WAITING_SCRIPT,
            4,
            self._queue_key,
            self._prefix,
            self._event_stream,
            self._deployment_lease_key,
            task_id,
            utc_now().isoformat(),
        )
        marker = self._text(payload) if payload else ""
        if marker == "MAINTENANCE":
            raise InvalidTransitionError("maintenance task cannot be requeued")
        if payload:
            return self._deserialize(payload)
        task = self._required(task_id)
        if task.status == TaskStatus.QUEUED:
            return task
        raise InvalidTransitionError(f"{task.status.value} cannot be requeued")

    def retry_failed(self, task_id: str) -> TaskRecord:
        payload = self.client.eval(
            self._RETRY_FAILED_SCRIPT,
            4,
            self._queue_key,
            self._prefix,
            self._event_stream,
            self._deployment_lease_key,
            task_id,
            utc_now().isoformat(),
        )
        marker = self._text(payload) if payload else ""
        if marker == "MAINTENANCE":
            raise InvalidTransitionError("maintenance task cannot be retried")
        if payload:
            return self._deserialize(payload)
        task = self._required(task_id)
        if task.status == TaskStatus.QUEUED:
            return task
        raise InvalidTransitionError(f"{task.status.value} cannot be retried")

    def refresh_task(self, task_id: str, include_long_capture: bool | None = None) -> TaskRecord:
        task_id = self.resolve_task_id(task_id)
        task = self._required(task_id)
        if self._is_maintenance_task(task):
            raise InvalidTransitionError("maintenance task cannot be refreshed")
        if task.status in _ACTIVE_STATUSES:
            return task
        if task.status not in _REFRESHABLE_STATUSES:
            raise InvalidTransitionError(f"{task.status.value} cannot be refreshed")
        candidate = deepcopy(task)
        _reset_task_for_refresh(candidate, include_long_capture)
        payload = self.client.eval(
            self._REFRESH_TASK_SCRIPT,
            5,
            self._queue_key,
            self._prefix,
            self._event_stream,
            self._index_key,
            self._deployment_lease_key,
            task_id,
            self._serialize(candidate),
            self.pending_cap,
        )
        marker = self._text(payload) if payload else ""
        if marker == "QUEUE_FULL":
            raise QueueFullError("global pending queue cap reached")
        if marker == "MAINTENANCE":
            raise InvalidTransitionError("maintenance task cannot be refreshed")
        if payload:
            return self._deserialize(payload)
        current = self._required(task_id)
        if current.status in _ACTIVE_STATUSES:
            return current
        raise InvalidTransitionError(f"{current.status.value} cannot be refreshed")

    def transition(self, task_id: str, status: TaskStatus, *, error_code: str | None = None, source_errors: dict[str, str | None] | None = None) -> TaskRecord:
        task = self._required(task_id)
        if status not in _ALLOWED_TRANSITIONS[task.status]:
            raise InvalidTransitionError(f"{task.status.value} cannot transition to {status.value}")
        task.status = status
        task.error_code = error_code
        if source_errors is not None:
            task.source_errors = _normalized_source_errors(source_errors)
        task.updated_at = utc_now()
        if status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            task.completed_at = task.updated_at
        self._save(task)
        self._emit(task)
        return task

    def complete_capture(self, task_id: str, kind: CaptureKind, path: str) -> TaskRecord:
        task = self._required(task_id)
        if task.status not in {TaskStatus.RUNNING, TaskStatus.PARTIAL}:
            raise InvalidTransitionError(f"{task.status.value} cannot accept a capture")
        capture = task.captures[kind]
        capture.status = CaptureStatus.READY
        capture.path = Path(path)
        capture.captured_at = utc_now()
        task.updated_at = capture.captured_at
        ready = sum(item.status == CaptureStatus.READY for item in task.captures.values())
        task.status = TaskStatus.COMPLETED if ready == len(CaptureKind) else TaskStatus.PARTIAL
        if task.status == TaskStatus.COMPLETED:
            task.completed_at = task.updated_at
        self._save(task)
        self._emit(task)
        return task

    def complete_result(self, task_id: str, values: dict[MetricKind, str | None], path: str | None, *, ocr_metrics: set[MetricKind] | None = None, source_errors: dict[str, str | None] | None = None, intraday_series: dict[MetricKind, dict[str, object]] | None = None) -> TaskRecord:
        task = self._required(task_id)
        if task.status not in {TaskStatus.RUNNING, TaskStatus.PARTIAL}:
            raise InvalidTransitionError(f"{task.status.value} cannot accept a result")
        task.values = {kind: values.get(kind) for kind in MetricKind}
        ocr_kinds = ocr_metrics or set()
        task.value_sources = {
            kind: (
                ValueSource.OCR
                if kind in ocr_kinds and task.values[kind] is not None
                else ValueSource.INTERFACE if task.values[kind] is not None else None
            )
            for kind in MetricKind
        }
        task.intraday_series = normalized_intraday_series(intraday_series)
        task.source_errors = _normalized_source_errors(source_errors)
        now = utc_now()
        if task.include_long_capture:
            if path is None:
                raise ValueError("a long capture path is required for this task")
            task.long_capture.status = CaptureStatus.READY
            task.long_capture.path = Path(path)
            task.long_capture.captured_at = now
        else:
            if path is not None:
                raise ValueError("a data-only task cannot accept a long capture path")
            task.long_capture.status = CaptureStatus.SKIPPED
            task.long_capture.path = None
            task.long_capture.captured_at = None
        task.collected_at = now
        task.updated_at = now
        required_complete = all(task.values[kind] is not None for kind in REQUIRED_METRICS)
        fund_error = task.source_errors["main_fund_flow"]
        task.status = (
            TaskStatus.COMPLETED
            if required_complete and fund_error is None
            else TaskStatus.PARTIAL
        )
        task.error_code = (
            None
            if task.status == TaskStatus.COMPLETED
            else fund_error if required_complete else "VALUE_RECOGNITION_FAILED"
        )
        task.completed_at = task.updated_at
        self._save(task)
        self._emit(task)
        return task

    def events_after(self, task_id: str, event_index: int = 0) -> list[dict[str, str]]:
        task_id = self.resolve_task_id(task_id)
        events: list[dict[str, str]] = []
        for _, fields in self.client.xrange(self._event_stream, "-", "+"):
            normalized = {self._text(key): self._text(value) for key, value in fields.items()}
            if normalized.get("task_id") == task_id:
                events.append({"event": normalized["event"], "data": normalized["data"]})
        return events[event_index:]

    def cleanup(self, now: datetime) -> list[TaskRecord]:
        removed: list[TaskRecord] = []
        maintenance_payloads = self.client.eval(
            self._CLEANUP_EXPIRED_DEPLOYMENT_ACCEPTANCE_SCRIPT,
            7,
            self._deployment_lease_key,
            self._queue_key,
            self._prefix,
            self._index_key,
            self._event_stream,
            self._symbol_index_key,
            self._alias_key,
        )
        if maintenance_payloads and self._text(maintenance_payloads) == "INVALID":
            maintenance_payloads = []
        if isinstance(maintenance_payloads, (list, tuple)):
            for payload in maintenance_payloads:
                try:
                    removed.append(self._deserialize(payload))
                except (KeyError, TypeError, ValueError):
                    return []
        for raw_task_id in list(self.client.smembers(self._index_key)):
            task_id = self._text(raw_task_id)
            task = self._get_raw(task_id)
            if task is None:
                self.client.srem(self._index_key, task_id)
                continue
            if task.task_id != task_id or _has_maintenance_metadata(task):
                continue
            changed = False
            if task.long_capture.status == CaptureStatus.READY and task.long_capture.expires_at and now >= task.long_capture.expires_at:
                changed = True
                self._unlink_capture(task.long_capture)
                task.long_capture.status = CaptureStatus.EXPIRED
            for capture in task.captures.values():
                if capture.status == CaptureStatus.READY and capture.expires_at and now >= capture.expires_at:
                    changed = True
                    self._unlink_capture(capture)
                    capture.status = CaptureStatus.EXPIRED
            if changed:
                self._save(task)
        return removed

    def _is_maintenance_task(self, task: TaskRecord) -> bool:
        if _has_maintenance_metadata(task):
            return True
        lease = self.deployment_lease_status()
        return lease is not None and lease.bound_task_id == task.task_id

    def _required(self, task_id: str) -> TaskRecord:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _save(self, task: TaskRecord) -> None:
        self.client.set(self._key(task.task_id), self._serialize(task))

    def _emit(self, task: TaskRecord) -> None:
        self.client.xadd(self._event_stream, {"event": "status", "task_id": task.task_id, "data": task.status.value})

    def _unlink_capture(self, capture: CaptureRecord | LongCaptureRecord) -> None:
        if capture.path is None or self.capture_root is None:
            return
        path = capture.path.resolve()
        try:
            path.relative_to(self.capture_root)
        except ValueError:
            return
        path.unlink(missing_ok=True)

    def _key(self, task_id: str) -> str:
        return f"{self._prefix}{task_id}"

    @staticmethod
    def _text(value: object) -> str:
        return value.decode() if isinstance(value, bytes) else str(value)

    @staticmethod
    def _serialize(task: TaskRecord) -> str:
        return json.dumps({
            "task_id": task.task_id,
            "symbol": task.symbol,
            "include_long_capture": task.include_long_capture,
            "maintenance": (
                {
                    "namespace": task.maintenance_namespace,
                    "owner_digest": task.maintenance_owner_digest,
                }
                if _has_maintenance_metadata(task)
                else None
            ),
            "status": task.status.value,
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "collected_at": task.collected_at.isoformat() if task.collected_at else None,
            "error_code": task.error_code,
            "source_errors": {
                key: task.source_errors.get(key)
                for key in SOURCE_ERROR_KEYS
            },
            "captures": {
                kind.value: {
                    "status": capture.status.value,
                    "path": str(capture.path) if capture.path else None,
                    "captured_at": capture.captured_at.isoformat() if capture.captured_at else None,
                }
                for kind, capture in task.captures.items()
            },
            "values": {kind.value: task.values.get(kind) for kind in MetricKind},
            "value_sources": {
                kind.value: source.value if source is not None else None
                for kind, source in task.value_sources.items()
            },
            "intraday_series": {
                kind.value: series
                for _, kind, _ in INTRADAY_METRICS
                if (series := task.intraday_series.get(kind)) is not None
            },
            "long_capture": {
                "status": task.long_capture.status.value,
                "path": str(task.long_capture.path) if task.long_capture.path else None,
                "captured_at": task.long_capture.captured_at.isoformat() if task.long_capture.captured_at else None,
            },
        })

    @staticmethod
    def _deserialize(payload: object) -> TaskRecord:
        raw = json.loads(RedisStreamsStore._text(payload))
        maintenance = raw.get("maintenance")
        if maintenance is None:
            maintenance_namespace = None
            maintenance_owner_digest = None
        elif isinstance(maintenance, dict):
            maintenance_namespace = maintenance.get("namespace")
            maintenance_owner_digest = maintenance.get("owner_digest")
            if not isinstance(maintenance_namespace, str) or not isinstance(
                maintenance_owner_digest, str
            ):
                raise ValueError("invalid task maintenance metadata")
        else:
            raise ValueError("invalid task maintenance metadata")
        task = TaskRecord(
            task_id=raw["task_id"],
            symbol=raw["symbol"],
            include_long_capture=raw.get("include_long_capture", True),
            maintenance_namespace=maintenance_namespace,
            maintenance_owner_digest=maintenance_owner_digest,
            status=TaskStatus(raw["status"]),
            created_at=datetime.fromisoformat(raw["created_at"]),
            updated_at=datetime.fromisoformat(raw["updated_at"]),
            completed_at=datetime.fromisoformat(raw["completed_at"]) if raw["completed_at"] else None,
            collected_at=(
                datetime.fromisoformat(raw["collected_at"])
                if raw.get("collected_at")
                else None
            ),
            error_code=raw["error_code"],
            source_errors=_normalized_source_errors(raw.get("source_errors")),
        )
        for kind in CaptureKind:
            capture = raw.get("captures", {}).get(
                kind.value,
                {"status": "PENDING", "path": None, "captured_at": None},
            )
            task.captures[kind] = CaptureRecord(
                kind=kind,
                status=CaptureStatus(capture["status"]),
                path=Path(capture["path"]) if capture["path"] else None,
                captured_at=datetime.fromisoformat(capture["captured_at"]) if capture["captured_at"] else None,
            )
        for kind in MetricKind:
            task.values[kind] = raw.get("values", {}).get(kind.value)
            source = raw.get("value_sources", {}).get(kind.value)
            task.value_sources[kind] = (
                ValueSource(source)
                if source is not None
                else ValueSource.INTERFACE if task.values[kind] is not None else None
            )
        stored_intraday = raw.get("intraday_series", {})
        task.intraday_series = normalized_intraday_series({
            kind: stored_intraday.get(kind.value, {})
            for _, kind, _ in INTRADAY_METRICS
        })
        long_capture = raw.get("long_capture")
        if long_capture:
            task.long_capture = LongCaptureRecord(
                status=CaptureStatus(long_capture["status"]),
                path=Path(long_capture["path"]) if long_capture["path"] else None,
                captured_at=(
                    datetime.fromisoformat(long_capture["captured_at"])
                    if long_capture["captured_at"]
                    else None
                ),
            )
        return task
