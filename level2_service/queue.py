"""Queue state backed by Redis Streams in production and a deterministic fake in tests."""

from __future__ import annotations

from collections import deque
from datetime import datetime
import json
from pathlib import Path
from threading import Lock
from typing import Protocol

from .models import CaptureKind, CaptureRecord, CaptureStatus, LongCaptureRecord, MetricKind, REQUIRED_METRICS, SOURCE_ERROR_KEYS, TaskRecord, TaskStatus, ValueSource, utc_now


class QueueFullError(RuntimeError):
    pass


class InvalidTransitionError(ValueError):
    pass


class TaskStore(Protocol):
    def enqueue(self, task: TaskRecord) -> None: ...
    def get(self, task_id: str) -> TaskRecord | None: ...
    def queue_position(self, task_id: str) -> int | None: ...
    def next_queued(self) -> TaskRecord | None: ...
    def recover_running(self) -> list[TaskRecord]: ...
    def requeue_waiting(self, task_id: str) -> TaskRecord: ...
    def retry_failed(self, task_id: str) -> TaskRecord: ...
    def transition(self, task_id: str, status: TaskStatus, *, error_code: str | None = None, source_errors: dict[str, str | None] | None = None) -> TaskRecord: ...
    def complete_capture(self, task_id: str, kind: CaptureKind, path: str) -> TaskRecord: ...
    def complete_result(self, task_id: str, values: dict[MetricKind, str | None], path: str | None, *, ocr_metrics: set[MetricKind] | None = None, source_errors: dict[str, str | None] | None = None) -> TaskRecord: ...
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


def _normalized_source_errors(
    source_errors: dict[str, str | None] | None,
) -> dict[str, str | None]:
    provided = source_errors or {}
    return {key: provided.get(key) for key in SOURCE_ERROR_KEYS}


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
            item.status in {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING_ADMIN}
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

    def queue_position(self, task_id: str) -> int | None:
        task = self._tasks.get(task_id)
        if task is None or task.status != TaskStatus.QUEUED:
            return None
        position = 0
        for queued_id in self._fifo:
            candidate = self._tasks.get(queued_id)
            if candidate is None or candidate.status != TaskStatus.QUEUED:
                continue
            position += 1
            if queued_id == task_id:
                return position
        return None

    def requeue_waiting(self, task_id: str) -> TaskRecord:
        task = self._tasks[task_id]
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
        task = self._tasks[task_id]
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

    def complete_result(self, task_id: str, values: dict[MetricKind, str | None], path: str | None, *, ocr_metrics: set[MetricKind] | None = None, source_errors: dict[str, str | None] | None = None) -> TaskRecord:
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
        return self._events.get(task_id, [])[event_index:]

    def cleanup(self, now: datetime) -> list[TaskRecord]:
        removed: list[TaskRecord] = []
        for task in list(self._tasks.values()):
            expired_capture = False
            long_capture = task.long_capture
            if (
                long_capture.status == CaptureStatus.READY
                and long_capture.expires_at is not None
                and now >= long_capture.expires_at
            ):
                expired_capture = True
                if long_capture.path is not None:
                    path = long_capture.path.resolve()
                    if self.capture_root is not None:
                        try:
                            path.relative_to(self.capture_root)
                        except ValueError:
                            pass
                        else:
                            path.unlink(missing_ok=True)
                long_capture.status = CaptureStatus.EXPIRED
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

    def _move_to_fifo_tail(self, task_id: str) -> None:
        self._fifo = deque(item for item in self._fifo if item != task_id)
        self._fifo.append(task_id)


class RedisStreamsStore:
    """Redis-backed TaskStore using a stream for events and Lua for FIFO claims."""

    _REQUIRED_CLIENT_METHODS = ("delete", "eval", "get", "lrange", "rpush", "sadd", "set", "smembers", "srem", "xadd", "xrange")
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
return true
"""
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
local payload = redis.call('GET', KEYS[2] .. ARGV[1])
if not payload then return false end
local task = cjson.decode(payload)
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
local payload = redis.call('GET', KEYS[2] .. ARGV[1])
if not payload then return false end
local task = cjson.decode(payload)
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
        accepted = self.client.eval(
            self._ENQUEUE_SCRIPT,
            4,
            self._queue_key,
            self._prefix,
            self._index_key,
            self._event_stream,
            self.pending_cap,
            task.task_id,
            self._serialize(task),
        )
        if not accepted:
            raise QueueFullError("global pending queue cap reached")

    def get(self, task_id: str) -> TaskRecord | None:
        payload = self.client.get(self._key(task_id))
        return self._deserialize(payload) if payload else None

    def queue_position(self, task_id: str) -> int | None:
        task = self.get(task_id)
        if task is None or task.status != TaskStatus.QUEUED:
            return None
        position = 0
        for raw_task_id in self.client.lrange(self._queue_key, 0, -1):
            queued_id = self._text(raw_task_id)
            candidate = self.get(queued_id)
            if candidate is None or candidate.status != TaskStatus.QUEUED:
                continue
            position += 1
            if queued_id == task_id:
                return position
        return None

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

    def requeue_waiting(self, task_id: str) -> TaskRecord:
        payload = self.client.eval(
            self._REQUEUE_WAITING_SCRIPT,
            3,
            self._queue_key,
            self._prefix,
            self._event_stream,
            task_id,
            utc_now().isoformat(),
        )
        if payload:
            return self._deserialize(payload)
        task = self._required(task_id)
        if task.status == TaskStatus.QUEUED:
            return task
        raise InvalidTransitionError(f"{task.status.value} cannot be requeued")

    def retry_failed(self, task_id: str) -> TaskRecord:
        payload = self.client.eval(
            self._RETRY_FAILED_SCRIPT,
            3,
            self._queue_key,
            self._prefix,
            self._event_stream,
            task_id,
            utc_now().isoformat(),
        )
        if payload:
            return self._deserialize(payload)
        task = self._required(task_id)
        if task.status == TaskStatus.QUEUED:
            return task
        raise InvalidTransitionError(f"{task.status.value} cannot be retried")

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

    def complete_result(self, task_id: str, values: dict[MetricKind, str | None], path: str | None, *, ocr_metrics: set[MetricKind] | None = None, source_errors: dict[str, str | None] | None = None) -> TaskRecord:
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
            if task.long_capture.status == CaptureStatus.READY and task.long_capture.expires_at and now >= task.long_capture.expires_at:
                expired = True
                self._unlink_capture(task.long_capture)
                task.long_capture.status = CaptureStatus.EXPIRED
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
            "include_long_capture": task.include_long_capture,
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
            "long_capture": {
                "status": task.long_capture.status.value,
                "path": str(task.long_capture.path) if task.long_capture.path else None,
                "captured_at": task.long_capture.captured_at.isoformat() if task.long_capture.captured_at else None,
            },
        })

    @staticmethod
    def _deserialize(payload: object) -> TaskRecord:
        raw = json.loads(RedisStreamsStore._text(payload))
        task = TaskRecord(
            task_id=raw["task_id"],
            symbol=raw["symbol"],
            include_long_capture=raw.get("include_long_capture", True),
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
