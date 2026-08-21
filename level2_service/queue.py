"""Queue state backed by Redis Streams in production and a deterministic fake in tests."""

from __future__ import annotations

from collections import deque
from datetime import datetime
import json
from pathlib import Path
from threading import Lock
from typing import Protocol

from .models import CaptureKind, CaptureRecord, CaptureStatus, TaskRecord, TaskStatus, utc_now


class QueueFullError(RuntimeError):
    pass


class InvalidTransitionError(ValueError):
    pass


class TaskStore(Protocol):
    def enqueue(self, task: TaskRecord) -> None: ...
    def get(self, task_id: str) -> TaskRecord | None: ...
    def next_queued(self) -> TaskRecord | None: ...
    def transition(self, task_id: str, status: TaskStatus, *, error_code: str | None = None) -> TaskRecord: ...
    def complete_capture(self, task_id: str, kind: CaptureKind, path: str) -> TaskRecord: ...
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


class InMemoryStreams:
    """A small Redis-Streams-compatible state model for local development and tests."""

    def __init__(self, pending_cap: int = 200, capture_root: Path | None = None) -> None:
        self.pending_cap = pending_cap
        self.capture_root = capture_root.resolve() if capture_root else None
        self._tasks: dict[str, TaskRecord] = {}
        self._fifo: deque[str] = deque()
        self._events: dict[str, list[dict[str, str]]] = {}
        self._claim_lock = Lock()

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
        with self._claim_lock:
            for task_id in self._fifo:
                task = self._tasks.get(task_id)
                if task and task.status == TaskStatus.QUEUED:
                    task.status = TaskStatus.RUNNING
                    task.updated_at = utc_now()
                    self._emit(task)
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

    def events_after(self, task_id: str, event_index: int = 0) -> list[dict[str, str]]:
        return self._events.get(task_id, [])[event_index:]

    def cleanup(self, now: datetime) -> list[TaskRecord]:
        removed: list[TaskRecord] = []
        for task in list(self._tasks.values()):
            expired_capture = False
            for capture in task.captures.values():
                if (
                    capture.status == CaptureStatus.READY
                    and capture.expires_at is not None
                    and now >= capture.expires_at
                ):
                    expired_capture = True
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
            if expired_capture and task.status != TaskStatus.EXPIRED:
                task.status = TaskStatus.EXPIRED
                task.updated_at = now
                self._emit(task)
            if now >= task.metadata_expires_at:
                removed.append(task)
                del self._tasks[task.task_id]
                self._events.pop(task.task_id, None)
                self._fifo.remove(task.task_id)
        return removed

    def _emit(self, task: TaskRecord) -> None:
        self._events.setdefault(task.task_id, []).append({"event": "status", "data": task.status.value})


class RedisStreamsStore:
    """Redis-backed TaskStore using a stream for events and Lua for FIFO claims."""

    _REQUIRED_CLIENT_METHODS = ("delete", "eval", "get", "rpush", "sadd", "scard", "set", "smembers", "srem", "xadd", "xrange")
    _CLAIM_SCRIPT = """
local task_id = redis.call('LPOP', KEYS[1])
while task_id do
  local key = KEYS[2] .. task_id
  local payload = redis.call('GET', key)
  if payload then
    local task = cjson.decode(payload)
    if task.status == 'QUEUED' then
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

    def set_capture_root(self, capture_root: Path) -> None:
        self.capture_root = capture_root.resolve()

    def enqueue(self, task: TaskRecord) -> None:
        pending = sum(
            item.status in {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING_ADMIN, TaskStatus.PARTIAL}
            for item in (self.get(self._text(task_id)) for task_id in list(self.client.smembers(self._index_key)))
            if item is not None
        )
        if pending >= self.pending_cap:
            raise QueueFullError("global pending queue cap reached")
        self._save(task)
        self.client.sadd(self._index_key, task.task_id)
        self.client.rpush(self._queue_key, task.task_id)
        self._emit(task)

    def get(self, task_id: str) -> TaskRecord | None:
        payload = self.client.get(self._key(task_id))
        return self._deserialize(payload) if payload else None

    def next_queued(self) -> TaskRecord | None:
        payload = self.client.eval(
            self._CLAIM_SCRIPT,
            3,
            self._queue_key,
            self._prefix,
            self._event_stream,
            utc_now().isoformat(),
        )
        return self._deserialize(payload) if payload else None

    def transition(self, task_id: str, status: TaskStatus, *, error_code: str | None = None) -> TaskRecord:
        task = self._required(task_id)
        if status not in _ALLOWED_TRANSITIONS[task.status]:
            raise InvalidTransitionError(f"{task.status.value} cannot transition to {status.value}")
        task.status = status
        task.error_code = error_code
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

    def events_after(self, task_id: str, event_index: int = 0) -> list[dict[str, str]]:
        events: list[dict[str, str]] = []
        for _, fields in self.client.xrange(self._event_stream, "-", "+"):
            normalized = {self._text(key): self._text(value) for key, value in fields.items()}
            if normalized.get("task_id") == task_id:
                events.append({"event": normalized["event"], "data": normalized["data"]})
        return events[event_index:]

    def cleanup(self, now: datetime) -> list[TaskRecord]:
        removed: list[TaskRecord] = []
        for raw_task_id in list(self.client.smembers(self._index_key)):
            task_id = self._text(raw_task_id)
            task = self.get(task_id)
            if task is None:
                self.client.srem(self._index_key, task_id)
                continue
            expired = False
            for capture in task.captures.values():
                if capture.status == CaptureStatus.READY and capture.expires_at and now >= capture.expires_at:
                    expired = True
                    self._unlink_capture(capture)
                    capture.status = CaptureStatus.EXPIRED
            if expired and task.status != TaskStatus.EXPIRED:
                task.status = TaskStatus.EXPIRED
                task.updated_at = now
                self._save(task)
                self._emit(task)
            if now >= task.metadata_expires_at:
                removed.append(task)
                self.client.delete(self._key(task_id))
                self.client.srem(self._index_key, task_id)
        return removed

    def _required(self, task_id: str) -> TaskRecord:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _save(self, task: TaskRecord) -> None:
        self.client.set(self._key(task.task_id), self._serialize(task))

    def _emit(self, task: TaskRecord) -> None:
        self.client.xadd(self._event_stream, {"event": "status", "task_id": task.task_id, "data": task.status.value})

    def _unlink_capture(self, capture: CaptureRecord) -> None:
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
            "status": task.status.value,
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "error_code": task.error_code,
            "captures": {
                kind.value: {
                    "status": capture.status.value,
                    "path": str(capture.path) if capture.path else None,
                    "captured_at": capture.captured_at.isoformat() if capture.captured_at else None,
                }
                for kind, capture in task.captures.items()
            },
        })

    @staticmethod
    def _deserialize(payload: object) -> TaskRecord:
        raw = json.loads(RedisStreamsStore._text(payload))
        task = TaskRecord(
            task_id=raw["task_id"],
            symbol=raw["symbol"],
            status=TaskStatus(raw["status"]),
            created_at=datetime.fromisoformat(raw["created_at"]),
            updated_at=datetime.fromisoformat(raw["updated_at"]),
            completed_at=datetime.fromisoformat(raw["completed_at"]) if raw["completed_at"] else None,
            error_code=raw["error_code"],
        )
        for kind in CaptureKind:
            capture = raw["captures"][kind.value]
            task.captures[kind] = CaptureRecord(
                kind=kind,
                status=CaptureStatus(capture["status"]),
                path=Path(capture["path"]) if capture["path"] else None,
                captured_at=datetime.fromisoformat(capture["captured_at"]) if capture["captured_at"] else None,
            )
        return task
