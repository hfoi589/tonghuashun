"""Queue state backed by Redis Streams in production and a deterministic fake in tests."""

from __future__ import annotations

from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .models import CaptureKind, CaptureStatus, TaskRecord, TaskStatus, utc_now


class QueueFullError(RuntimeError):
    pass


class InvalidTransitionError(ValueError):
    pass


class TaskStore(Protocol):
    def enqueue(self, task: TaskRecord) -> None: ...
    def get(self, task_id: str) -> TaskRecord | None: ...
    def transition(self, task_id: str, status: TaskStatus, *, error_code: str | None = None) -> TaskRecord: ...
    def complete_capture(self, task_id: str, kind: CaptureKind, path: str) -> TaskRecord: ...
    def events_after(self, task_id: str, event_index: int = 0) -> list[dict[str, str]]: ...
    def cleanup(self, now: datetime) -> list[TaskRecord]: ...


_ALLOWED_TRANSITIONS = {
    TaskStatus.QUEUED: {TaskStatus.RUNNING, TaskStatus.FAILED},
    TaskStatus.RUNNING: {TaskStatus.WAITING_ADMIN, TaskStatus.PARTIAL, TaskStatus.SUCCEEDED, TaskStatus.FAILED},
    TaskStatus.WAITING_ADMIN: {TaskStatus.RUNNING, TaskStatus.FAILED},
    TaskStatus.PARTIAL: {TaskStatus.RUNNING, TaskStatus.SUCCEEDED, TaskStatus.FAILED},
    TaskStatus.SUCCEEDED: set(),
    TaskStatus.FAILED: set(),
}


class InMemoryStreams:
    """A small Redis-Streams-compatible state model for local development and tests."""

    def __init__(self, pending_cap: int = 200, capture_root: Path | None = None) -> None:
        self.pending_cap = pending_cap
        self.capture_root = capture_root.resolve() if capture_root else None
        self._tasks: dict[str, TaskRecord] = {}
        self._fifo: deque[str] = deque()
        self._events: dict[str, list[dict[str, str]]] = {}

    def enqueue(self, task: TaskRecord) -> None:
        pending = sum(
            item.status in {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING_ADMIN, TaskStatus.PARTIAL}
            for item in self._tasks.values()
        )
        if pending >= self.pending_cap:
            raise QueueFullError("global pending queue cap reached")
        self._tasks[task.task_id] = task
        self._fifo.append(task.task_id)
        self._emit(task)

    def next_queued(self) -> TaskRecord | None:
        for task_id in self._fifo:
            task = self._tasks.get(task_id)
            if task and task.status == TaskStatus.QUEUED:
                return task
        return None

    def get(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def set_capture_root(self, capture_root: Path) -> None:
        self.capture_root = capture_root.resolve()

    def transition(self, task_id: str, status: TaskStatus, *, error_code: str | None = None) -> TaskRecord:
        task = self._tasks[task_id]
        if status not in _ALLOWED_TRANSITIONS[task.status]:
            raise InvalidTransitionError(f"{task.status.value} cannot transition to {status.value}")
        task.status = status
        task.error_code = error_code
        task.updated_at = utc_now()
        if status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}:
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
        task.status = TaskStatus.SUCCEEDED if ready == len(CaptureKind) else TaskStatus.PARTIAL
        if task.status == TaskStatus.SUCCEEDED:
            task.completed_at = task.updated_at
        self._emit(task)
        return task

    def events_after(self, task_id: str, event_index: int = 0) -> list[dict[str, str]]:
        return self._events.get(task_id, [])[event_index:]

    def cleanup(self, now: datetime) -> list[TaskRecord]:
        removed: list[TaskRecord] = []
        for task in list(self._tasks.values()):
            if now >= task.capture_expires_at:
                for capture in task.captures.values():
                    if capture.status == CaptureStatus.READY:
                        if capture.path is not None:
                            path = capture.path.resolve()
                            if self.capture_root is not None:
                                try:
                                    path.relative_to(self.capture_root)
                                except ValueError:
                                    pass
                                else:
                                    path.unlink(missing_ok=True)
                        capture.status = CaptureStatus.EXPIRED
            if now >= task.metadata_expires_at:
                removed.append(task)
                del self._tasks[task.task_id]
                self._events.pop(task.task_id, None)
                self._fifo.remove(task.task_id)
        return removed

    def _emit(self, task: TaskRecord) -> None:
        self._events.setdefault(task.task_id, []).append({"event": "status", "data": task.status.value})


class RedisStreamsStore:
    """Thin adapter boundary for a redis-py client; workers own consumer-group ACKs."""

    def __init__(self, client: object, stream: str = "ths:jobs") -> None:
        self.client = client
        self.stream = stream

    def append_job(self, task_id: str, symbol: str) -> str:
        return self.client.xadd(self.stream, {"event": "queued", "task_id": task_id, "symbol": symbol})
