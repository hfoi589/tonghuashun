import json

import pytest

from fastapi.testclient import TestClient

from level2_service.api import create_app
from level2_service.models import CaptureKind, TaskRecord, TaskStatus
from level2_service.queue import RedisStreamsStore


class FakeRedis:
    def __init__(self) -> None:
        self.values = {}
        self.lists = {}
        self.sets = {}
        self.streams = {}

    def set(self, key, value): self.values[key] = value
    def get(self, key): return self.values.get(key)
    def delete(self, key): self.values.pop(key, None)
    def rpush(self, key, value): self.lists.setdefault(key, []).append(value)
    def sadd(self, key, value): self.sets.setdefault(key, set()).add(value)
    def srem(self, key, value): self.sets.setdefault(key, set()).discard(value)
    def smembers(self, key): return self.sets.get(key, set())
    def scard(self, key): return len(self.sets.get(key, set()))
    def xadd(self, key, fields):
        entries = self.streams.setdefault(key, [])
        entries.append((str(len(entries) + 1), fields))
        return entries[-1][0]
    def xrange(self, key, _start, _end): return self.streams.get(key, [])
    def eval(self, _script, _keys, queue_key, prefix, event_stream, timestamp):
        while self.lists.get(queue_key):
            task_id = self.lists[queue_key].pop(0)
            key = prefix + task_id
            payload = self.values.get(key)
            if payload:
                task = json.loads(payload)
                if task["status"] == "QUEUED":
                    task["status"] = "RUNNING"
                    task["updated_at"] = timestamp
                    updated = json.dumps(task)
                    self.values[key] = updated
                    self.xadd(event_stream, {"event": "status", "task_id": task_id, "data": "RUNNING"})
                    return updated
        return False


def test_redis_store_exposes_the_full_task_store_contract() -> None:
    """A partial adapter cannot back production routes when Redis is configured."""
    store = RedisStreamsStore(FakeRedis())

    for method in ("enqueue", "get", "transition", "complete_capture", "events_after", "cleanup", "next_queued"):
        assert callable(getattr(store, method, None))


def test_redis_store_rejects_an_incomplete_client_before_app_construction() -> None:
    """Deferring a missing Redis method until the first public request hides deployment errors."""
    with pytest.raises(TypeError, match="RedisStreamsStore requires redis client methods"):
        RedisStreamsStore(object())


def test_redis_store_persists_state_and_claims_fifo_jobs_once() -> None:
    """A non-atomic Redis claim would hand one public job to multiple workers."""
    redis = FakeRedis()
    store = RedisStreamsStore(redis)
    store.enqueue(TaskRecord(task_id="first", symbol="600938"))
    store.enqueue(TaskRecord(task_id="second", symbol="000001"))

    first = store.next_queued()
    second = RedisStreamsStore(redis).next_queued()
    store.complete_capture(first.task_id, CaptureKind.LARGE_ORDER_NET, "/tmp/net.png")

    assert first.task_id == "first"
    assert first.status == TaskStatus.RUNNING
    assert second.task_id == "second"
    assert store.get("first").status == TaskStatus.PARTIAL
    assert [event["data"] for event in store.events_after("first")] == ["QUEUED", "RUNNING", "PARTIAL"]


def test_public_routes_work_when_the_redis_adapter_is_configured() -> None:
    """Injecting a real-store adapter must not make public submission return a server error."""
    client = TestClient(create_app(store=RedisStreamsStore(FakeRedis())))

    created = client.post("/api/v1/jobs", json={"symbol": "600938"})
    public_id = created.json()["public_id"]

    assert created.status_code == 202
    assert client.get(f"/api/v1/jobs/{public_id}").status_code == 200
