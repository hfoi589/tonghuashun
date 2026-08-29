import json
import re
from threading import Barrier, RLock, Thread
from time import monotonic

import pytest

from fastapi.testclient import TestClient

from level2_service.api import create_app
from level2_service.models import CaptureKind, MetricKind, TaskRecord, TaskStatus
from level2_service.queue import RedisStreamsStore


class FakeRedis:
    def __init__(self, *, clock=None) -> None:
        self.values = {}
        self.lists = {}
        self.sets = {}
        self.hashes = {}
        self.streams = {}
        self.clock = clock or monotonic
        self.expiries = {}
        self._lock = RLock()

    def _expire(self, key):
        deadline = self.expiries.get(key)
        if deadline is not None and self.clock() >= deadline:
            self.values.pop(key, None)
            self.expiries.pop(key, None)

    def advance(self, seconds):
        advance = getattr(self.clock, "advance", None)
        if callable(advance):
            advance(seconds)

    def set(self, key, value, nx=False, px=None, keepttl=False):
        self._expire(key)
        if nx and key in self.values:
            return None
        self.values[key] = value
        if px is not None:
            self.expiries[key] = self.clock() + float(px) / 1000.0
        elif not keepttl:
            self.expiries.pop(key, None)
        return True
    def get(self, key):
        self._expire(key)
        return self.values.get(key)
    def delete(self, key):
        existed = self.get(key) is not None
        self.values.pop(key, None)
        self.expiries.pop(key, None)
        return int(existed)
    def pexpire(self, key, milliseconds):
        if self.get(key) is None:
            return 0
        self.expiries[key] = self.clock() + float(milliseconds) / 1000.0
        return 1
    def pttl(self, key):
        if self.get(key) is None:
            return -2
        deadline = self.expiries.get(key)
        if deadline is None:
            return -1
        return max(0, int((deadline - self.clock()) * 1000))
    def rpush(self, key, value): self.lists.setdefault(key, []).append(value)
    def lpush(self, key, value): self.lists.setdefault(key, []).insert(0, value)
    def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        stop = None if end == -1 else end + 1
        return values[start:stop]
    def lrem(self, key, count, value):
        values = self.lists.get(key, [])
        original = len(values)
        if count == 0:
            self.lists[key] = [item for item in values if item != value]
        else:
            remaining = count
            kept = []
            for item in values:
                if item == value and remaining > 0:
                    remaining -= 1
                else:
                    kept.append(item)
            self.lists[key] = kept
        return original - len(self.lists[key])
    def sadd(self, key, value): self.sets.setdefault(key, set()).add(value)
    def srem(self, key, value): self.sets.setdefault(key, set()).discard(value)
    def smembers(self, key): return self.sets.get(key, set())
    def scard(self, key): return len(self.sets.get(key, set()))
    def hset(self, key, field=None, value=None, mapping=None):
        target = self.hashes.setdefault(key, {})
        if mapping is not None:
            target.update(mapping)
            return len(mapping)
        target[field] = value
        return 1
    def hget(self, key, field): return self.hashes.get(key, {}).get(field)
    def hgetall(self, key): return dict(self.hashes.get(key, {}))
    def hdel(self, key, *fields):
        target = self.hashes.get(key, {})
        removed = 0
        for field in fields:
            if field in target:
                removed += 1
                del target[field]
        return removed
    def xadd(self, key, fields):
        entries = self.streams.setdefault(key, [])
        entries.append((str(len(entries) + 1), fields))
        return entries[-1][0]
    def xrange(self, key, _start, _end): return self.streams.get(key, [])
    def xdel(self, key, *entry_ids):
        entries = self.streams.get(key, [])
        original = len(entries)
        self.streams[key] = [entry for entry in entries if entry[0] not in set(entry_ids)]
        return original - len(self.streams[key])

    @staticmethod
    def _active_status(status):
        return status in {"QUEUED", "RUNNING", "WAITING_ADMIN"}

    def _ordinary_canonical(self, prefix, index_key, symbol, removed_id):
        candidates = []
        for raw_id in self.sets.get(index_key, set()):
            if raw_id == removed_id:
                continue
            payload = self.get(prefix + raw_id)
            if not payload:
                continue
            try:
                task = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if (
                task.get("task_id") != raw_id
                or task.get("symbol") != symbol
                or task.get("maintenance")
            ):
                continue
            candidates.append(
                (
                    self._active_status(task.get("status")),
                    task.get("created_at") or "",
                    task.get("updated_at") or "",
                    raw_id,
                )
            )
        return max(candidates)[-1] if candidates else None

    @staticmethod
    def _valid_owner_digest(value):
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)

    def _validated_maintenance_raw(
        self,
        prefix,
        raw_id,
        *,
        owner_digest=None,
        allow_legacy=False,
    ):
        payload = self.get(prefix + raw_id)
        if not payload:
            return None
        try:
            task = json.loads(payload)
        except (TypeError, ValueError):
            return None
        if not (
            task.get("task_id") == raw_id
            and task.get("symbol") == "601872"
            and task.get("include_long_capture") is False
        ):
            return None
        maintenance = task.get("maintenance")
        if maintenance:
            digest = maintenance.get("owner_digest")
            if not (
                maintenance.get("namespace") == "deployment_acceptance"
                and self._valid_owner_digest(digest)
                and (owner_digest is None or digest == owner_digest)
            ):
                return None
        elif not allow_legacy:
            return None
        return payload, task

    def _remove_maintenance_raw(
        self,
        *,
        queue_key,
        prefix,
        index_key,
        event_stream,
        symbol_index,
        alias_key,
        raw_id,
        payload,
        task,
    ):
        canonical = self._ordinary_canonical(
            prefix, index_key, task["symbol"], raw_id
        )
        aliases = self.hashes.get(alias_key, {})
        for alias, target in list(aliases.items()):
            if alias == raw_id:
                self.hdel(alias_key, alias)
            elif target == raw_id:
                if canonical is None:
                    self.hdel(alias_key, alias)
                else:
                    self.hset(alias_key, alias, canonical)
        if canonical is None:
            self.hdel(symbol_index, task["symbol"])
        else:
            self.hset(symbol_index, task["symbol"], canonical)
        self.lrem(queue_key, 0, raw_id)
        self.delete(prefix + raw_id)
        self.srem(index_key, raw_id)
        event_ids = [
            event_id
            for event_id, fields in self.streams.get(event_stream, [])
            if fields.get("task_id") == raw_id
        ]
        if event_ids:
            self.xdel(event_stream, *event_ids)
        return payload

    def eval(self, script, key_count, *values):
        keys = values[:key_count]
        args = values[key_count:]
        if "THS_PERSIST_EVENT" in script:
            task_key, event_stream = keys
            payload, task_id, status, retention = args
            previous = self.values.get(task_key)
            self.set(task_key, payload)
            try:
                event_id = self.xadd(
                    event_stream,
                    {"event": "status", "task_id": task_id, "data": status},
                )
            except Exception:
                if previous is None:
                    self.values.pop(task_key, None)
                else:
                    self.values[task_key] = previous
                raise
            entries = self.streams.get(event_stream, [])
            excess = len(entries) - int(retention)
            if excess > 0:
                self.xdel(event_stream, *(entry_id for entry_id, _ in entries[:excess]))
            return event_id
        if "THS_ACQUIRE_DEPLOYMENT_LEASE" in script:
            lease_key, prefix, index_key = keys
            owner, owner_digest, ttl_ms = args
            with self._lock:
                if self.get(lease_key) is not None:
                    return 0
                for task_id in self.sets.get(index_key, set()):
                    payload = self.get(prefix + task_id)
                    if not payload:
                        continue
                    task = json.loads(payload)
                    if task["status"] == "RUNNING" or (
                        task["status"] == "PARTIAL"
                        and task.get("completed_at") is None
                    ):
                        return -1
                self.set(
                    lease_key,
                    json.dumps(
                        {"owner_token": owner, "bound_task_id": None},
                        separators=(",", ":"),
                    ),
                    nx=True,
                    px=int(ttl_ms),
                )
                lease = json.loads(self.values[lease_key])
                lease["owner_digest"] = owner_digest
                self.set(
                    lease_key,
                    json.dumps(lease, separators=(",", ":")),
                    keepttl=True,
                )
                return 1
        if "THS_RENEW_DEPLOYMENT_LEASE" in script:
            (lease_key,) = keys
            owner, owner_digest, ttl_ms = args
            with self._lock:
                payload = self.get(lease_key)
                if not payload:
                    return 0
                lease = json.loads(payload)
                if lease["owner_token"] != owner or lease.get(
                    "owner_digest", owner_digest
                ) != owner_digest:
                    return 0
                lease["owner_digest"] = owner_digest
                self.set(
                    lease_key,
                    json.dumps(lease, separators=(",", ":")),
                    keepttl=True,
                )
                return self.pexpire(lease_key, int(ttl_ms))
        if "THS_DEPLOYMENT_LEASE_STATUS" in script:
            (lease_key,) = keys
            with self._lock:
                payload = self.get(lease_key)
                ttl = self.pttl(lease_key)
                return [payload, ttl] if payload and ttl > 0 else False
        if "THS_BIND_DEPLOYMENT_ACCEPTANCE" in script:
            lease_key, queue_key, prefix, index_key, event_stream, symbol_index = keys
            owner, owner_digest, task_id, payload, symbol, cap = args
            with self._lock:
                lease_payload = self.get(lease_key)
                if not lease_payload:
                    return False
                lease = json.loads(lease_payload)
                if lease["owner_token"] != owner:
                    return False
                if lease.get("owner_digest", owner_digest) != owner_digest:
                    return False
                lease["owner_digest"] = owner_digest
                candidate = json.loads(payload)
                candidate_maintenance = candidate.get("maintenance")
                if not (
                    candidate["task_id"] == task_id
                    and candidate["symbol"] == "601872"
                    and candidate["include_long_capture"] is False
                    and candidate["status"] == "QUEUED"
                    and candidate_maintenance
                    and candidate_maintenance.get("namespace")
                    == "deployment_acceptance"
                    and candidate_maintenance.get("owner_digest") == owner_digest
                ):
                    return False
                self.set(
                    lease_key,
                    json.dumps(lease, separators=(",", ":")),
                    keepttl=True,
                )
                bound = lease.get("bound_task_id")
                if bound:
                    existing = self.get(prefix + bound)
                    if not existing:
                        return False
                    task = json.loads(existing)
                    if not (
                        task["task_id"] == bound
                        and
                        task["symbol"] == "601872"
                        and task["include_long_capture"] is False
                    ):
                        return False
                    maintenance = task.get("maintenance")
                    if maintenance:
                        if not (
                            maintenance.get("namespace")
                            == "deployment_acceptance"
                            and maintenance.get("owner_digest") == owner_digest
                        ):
                            return False
                    else:
                        task["maintenance"] = candidate_maintenance
                        existing = json.dumps(task)
                        self.set(prefix + bound, existing)
                    if self.hget(symbol_index, task["symbol"]) == bound:
                        self.hdel(symbol_index, task["symbol"])
                    if task["status"] in {"COMPLETED", "FAILED", "EXPIRED"} or (
                        task["status"] == "PARTIAL"
                        and task.get("completed_at") is not None
                    ):
                        pending = sum(
                            json.loads(self.get(prefix + existing_id))["status"]
                            in {"QUEUED", "RUNNING", "WAITING_ADMIN"}
                            for existing_id in self.sets.get(index_key, set())
                            if self.get(prefix + existing_id)
                        )
                        if pending >= int(cap):
                            return "QUEUE_FULL"
                        refreshed = candidate
                        refreshed["task_id"] = bound
                        updated = json.dumps(refreshed)
                        self.set(prefix + bound, updated)
                        self.lrem(queue_key, 0, bound)
                        self.rpush(queue_key, bound)
                        self.sadd(index_key, bound)
                        self.xadd(
                            event_stream,
                            {"event": "status", "task_id": bound, "data": "QUEUED"},
                        )
                        return updated
                    return existing
                if self.get(prefix + task_id):
                    return False
                pending = sum(
                    json.loads(self.get(prefix + existing_id))["status"]
                    in {"QUEUED", "RUNNING", "WAITING_ADMIN"}
                    for existing_id in self.sets.get(index_key, set())
                    if self.get(prefix + existing_id)
                )
                if pending >= int(cap):
                    return "QUEUE_FULL"
                self.set(prefix + task_id, payload)
                self.sadd(index_key, task_id)
                self.rpush(queue_key, task_id)
                self.xadd(
                    event_stream,
                    {
                        "event": "status",
                        "task_id": task_id,
                        "data": "QUEUED",
                    },
                )
                lease["bound_task_id"] = task_id
                self.set(
                    lease_key,
                    json.dumps(lease, separators=(",", ":")),
                    keepttl=True,
                )
                return payload
        if "THS_CLEANUP_EXPIRED_DEPLOYMENT_ACCEPTANCE" in script:
            (
                lease_key,
                queue_key,
                prefix,
                index_key,
                event_stream,
                symbol_index,
                alias_key,
            ) = keys
            with self._lock:
                if self.get(lease_key):
                    return []
                targets = []
                for raw_id in list(self.sets.get(index_key, set())):
                    payload = self.get(prefix + raw_id)
                    if not payload:
                        continue
                    try:
                        task = json.loads(payload)
                    except (TypeError, ValueError):
                        continue
                    if task.get("maintenance"):
                        validated = self._validated_maintenance_raw(
                            prefix, raw_id
                        )
                        if validated is None:
                            return "INVALID"
                        targets.append((raw_id, *validated))
                removed = []
                for raw_id, payload, task in targets:
                    removed.append(
                        self._remove_maintenance_raw(
                            queue_key=queue_key,
                            prefix=prefix,
                            index_key=index_key,
                            event_stream=event_stream,
                            symbol_index=symbol_index,
                            alias_key=alias_key,
                            raw_id=raw_id,
                            payload=payload,
                            task=task,
                        )
                    )
                return removed
        if "THS_HOST_RELEASE_DEPLOYMENT_LEASE" in script:
            (
                lease_key,
                queue_key,
                prefix,
                index_key,
                event_stream,
                symbol_index,
                alias_key,
            ) = keys
            owner_digest, owner = args
            with self._lock:
                payload = self.get(lease_key)
                if not payload:
                    return 0
                try:
                    lease = json.loads(payload)
                except (TypeError, ValueError):
                    return 0
                if (
                    lease.get("owner_token") != owner
                    or not self._valid_owner_digest(owner_digest)
                    or lease.get("owner_digest", owner_digest) != owner_digest
                ):
                    return 0
                task_id = lease.get("bound_task_id")
                if task_id:
                    validated = self._validated_maintenance_raw(
                        prefix,
                        task_id,
                        owner_digest=owner_digest,
                        allow_legacy=True,
                    )
                    if validated is None:
                        return 0
                    task_payload, task = validated
                    self._remove_maintenance_raw(
                        queue_key=queue_key,
                        prefix=prefix,
                        index_key=index_key,
                        event_stream=event_stream,
                        symbol_index=symbol_index,
                        alias_key=alias_key,
                        raw_id=task_id,
                        payload=task_payload,
                        task=task,
                    )
                return self.delete(lease_key)
        if "THS_RELEASE_DEPLOYMENT_LEASE" in script:
            (
                lease_key,
                queue_key,
                prefix,
                index_key,
                event_stream,
                symbol_index,
                alias_key,
            ) = keys
            owner, owner_digest = args
            with self._lock:
                payload = self.get(lease_key)
                if not payload:
                    return 0
                lease = json.loads(payload)
                if lease["owner_token"] != owner or lease.get(
                    "owner_digest", owner_digest
                ) != owner_digest:
                    return 0
                task_id = lease.get("bound_task_id")
                if task_id:
                    validated = self._validated_maintenance_raw(
                        prefix,
                        task_id,
                        owner_digest=owner_digest,
                        allow_legacy=True,
                    )
                    if validated is None:
                        return 0
                    task_payload, task = validated
                    self._remove_maintenance_raw(
                        queue_key=queue_key,
                        prefix=prefix,
                        index_key=index_key,
                        event_stream=event_stream,
                        symbol_index=symbol_index,
                        alias_key=alias_key,
                        raw_id=task_id,
                        payload=task_payload,
                        task=task,
                    )
                return self.delete(lease_key)
        if "THS_CLAIM_WITH_DEPLOYMENT_LEASE" in script:
            queue_key, prefix, event_stream, lease_key = keys
            (timestamp,) = args
            with self._lock:
                lease_payload = self.get(lease_key)
                if lease_payload:
                    task_id = json.loads(lease_payload).get("bound_task_id")
                    if not task_id:
                        return False
                    payload = self.get(prefix + task_id)
                    if not payload:
                        return False
                    task = json.loads(payload)
                    maintenance = task.get("maintenance")
                    owner_digest = json.loads(lease_payload).get("owner_digest")
                    if (
                        task["status"] != "QUEUED"
                        or task["symbol"] != "601872"
                        or task["include_long_capture"] is not False
                        or task["task_id"] != task_id
                        or not maintenance
                        or maintenance.get("namespace")
                        != "deployment_acceptance"
                        or maintenance.get("owner_digest") != owner_digest
                    ):
                        return False
                    task["status"] = "RUNNING"
                    task["updated_at"] = timestamp
                    updated = json.dumps(task)
                    self.set(prefix + task_id, updated)
                    self.lrem(queue_key, 0, task_id)
                    self.xadd(
                        event_stream,
                        {
                            "event": "status",
                            "task_id": task_id,
                            "data": "RUNNING",
                        },
                    )
                    return updated
                while self.lists.get(queue_key):
                    task_id = self.lists[queue_key].pop(0)
                    payload = self.get(prefix + task_id)
                    if payload:
                        task = json.loads(payload)
                        if task["status"] == "QUEUED" and not task.get(
                            "maintenance"
                        ):
                            task["status"] = "RUNNING"
                            task["updated_at"] = timestamp
                            updated = json.dumps(task)
                            self.set(prefix + task_id, updated)
                            self.xadd(
                                event_stream,
                                {
                                    "event": "status",
                                    "task_id": task_id,
                                    "data": "RUNNING",
                                },
                            )
                            return updated
                return False
        if "THS_ENQUEUE" in script:
            queue_key, prefix, index_key, event_stream, symbol_index_key = keys
            cap, task_id, payload, symbol = args
            pending = 0
            for existing_id in self.sets.get(index_key, set()):
                raw = self.values.get(prefix + existing_id)
                if raw and json.loads(raw)["status"] in {"QUEUED", "RUNNING", "WAITING_ADMIN"}:
                    pending += 1
            if pending >= int(cap):
                return False
            self.values[prefix + task_id] = payload
            self.sadd(index_key, task_id)
            self.rpush(queue_key, task_id)
            self.xadd(event_stream, {"event": "status", "task_id": task_id, "data": "QUEUED"})
            self.hset(symbol_index_key, symbol, task_id)
            return True
        if "THS_SUBMIT_OR_REFRESH" in script:
            (
                queue_key,
                prefix,
                index_key,
                event_stream,
                symbol_index_key,
                lease_key,
            ) = keys
            cap, symbol, new_task_id, new_payload, refresh_payload = args
            lease_payload = self.get(lease_key)
            lease_bound_id = (
                json.loads(lease_payload).get("bound_task_id")
                if lease_payload
                else None
            )
            existing_id = self.hget(symbol_index_key, symbol)
            if existing_id:
                payload = self.values.get(prefix + existing_id)
                if payload:
                    current = json.loads(payload)
                    if existing_id == lease_bound_id or current.get("maintenance"):
                        self.hdel(symbol_index_key, symbol)
                    else:
                        if current["status"] in {
                            "QUEUED",
                            "RUNNING",
                            "WAITING_ADMIN",
                        }:
                            return payload
                        pending = sum(
                            json.loads(self.values[prefix + task_id])["status"]
                            in {"QUEUED", "RUNNING", "WAITING_ADMIN"}
                            for task_id in self.sets.get(index_key, set())
                            if prefix + task_id in self.values
                        )
                        if pending >= int(cap):
                            return "QUEUE_FULL"
                        refreshed = json.loads(refresh_payload)
                        refreshed["task_id"] = existing_id
                        refreshed["symbol"] = symbol
                        updated = json.dumps(refreshed)
                        self.lrem(queue_key, 0, existing_id)
                        self.values[prefix + existing_id] = updated
                        self.rpush(queue_key, existing_id)
                        self.xadd(
                            event_stream,
                            {
                                "event": "status",
                                "task_id": existing_id,
                                "data": "QUEUED",
                            },
                        )
                        return updated
                else:
                    self.hdel(symbol_index_key, symbol)
            pending = sum(
                json.loads(self.values[prefix + task_id])["status"] in {"QUEUED", "RUNNING", "WAITING_ADMIN"}
                for task_id in self.sets.get(index_key, set())
                if prefix + task_id in self.values
            )
            if pending >= int(cap):
                return "QUEUE_FULL"
            self.values[prefix + new_task_id] = new_payload
            self.sadd(index_key, new_task_id)
            self.rpush(queue_key, new_task_id)
            self.hset(symbol_index_key, symbol, new_task_id)
            self.xadd(event_stream, {"event": "status", "task_id": new_task_id, "data": "QUEUED"})
            return new_payload
        if "THS_RECOVER_RUNNING" in script:
            queue_key, prefix, index_key, event_stream = keys
            (timestamp,) = args
            recovered = []
            for task_id in self.sets.get(index_key, set()):
                key = prefix + task_id
                payload = self.values.get(key)
                if not payload:
                    continue
                task = json.loads(payload)
                if task["status"] == "RUNNING":
                    recovered.append((task["created_at"], task_id, task))
            recovered.sort()
            updated_payloads = []
            for _created_at, task_id, task in recovered:
                task["status"] = "QUEUED"
                task["error_code"] = None
                task["completed_at"] = None
                task["updated_at"] = timestamp
                updated = json.dumps(task)
                self.values[prefix + task_id] = updated
                self.xadd(event_stream, {"event": "status", "task_id": task_id, "data": "QUEUED"})
                updated_payloads.append(updated)
            for _created_at, task_id, _task in reversed(recovered):
                self.lpush(queue_key, task_id)
            return updated_payloads
        if "THS_REFRESH_TASK" in script:
            queue_key, prefix, event_stream, index_key, lease_key = keys
            task_id, updated_payload, cap = args
            key = prefix + task_id
            payload = self.values.get(key)
            if not payload:
                return False
            current = json.loads(payload)
            lease_payload = self.get(lease_key)
            if current.get("maintenance") or (
                lease_payload
                and json.loads(lease_payload).get("bound_task_id") == task_id
            ):
                return "MAINTENANCE"
            if current["status"] in {"QUEUED", "RUNNING", "WAITING_ADMIN"}:
                return payload
            if current["status"] not in {"COMPLETED", "PARTIAL", "FAILED", "EXPIRED"}:
                return False
            pending = sum(
                json.loads(self.values[prefix + existing_id])["status"] in {"QUEUED", "RUNNING", "WAITING_ADMIN"}
                for existing_id in self.sets.get(index_key, set())
                if prefix + existing_id in self.values
            )
            if pending >= int(cap):
                return "QUEUE_FULL"
            self.values[key] = updated_payload
            self.sadd(index_key, task_id)
            self.rpush(queue_key, task_id)
            self.xadd(event_stream, {"event": "status", "task_id": task_id, "data": "QUEUED"})
            return updated_payload
        if "THS_RETRY_FAILED" in script:
            queue_key, prefix, event_stream, lease_key = keys
            task_id, timestamp = args
            key = prefix + task_id
            payload = self.values.get(key)
            if not payload:
                return False
            task = json.loads(payload)
            lease_payload = self.get(lease_key)
            if task.get("maintenance") or (
                lease_payload
                and json.loads(lease_payload).get("bound_task_id") == task_id
            ):
                return "MAINTENANCE"
            if task["status"] == "QUEUED":
                return payload
            if task["status"] != "FAILED":
                return False
            task["status"] = "QUEUED"
            task["error_code"] = None
            task["completed_at"] = None
            task["updated_at"] = timestamp
            updated = json.dumps(task)
            self.values[key] = updated
            self.rpush(queue_key, task_id)
            self.xadd(event_stream, {"event": "status", "task_id": task_id, "data": "QUEUED"})
            return updated
        if "THS_REQUEUE_WAITING" in script:
            queue_key, prefix, event_stream, lease_key = keys
            task_id, timestamp = args
            key = prefix + task_id
            payload = self.values.get(key)
            if not payload:
                return False
            task = json.loads(payload)
            lease_payload = self.get(lease_key)
            if task.get("maintenance") or (
                lease_payload
                and json.loads(lease_payload).get("bound_task_id") == task_id
            ):
                return "MAINTENANCE"
            if task["status"] != "WAITING_ADMIN":
                return False
            task["status"] = "QUEUED"
            task["error_code"] = None
            task["updated_at"] = timestamp
            updated = json.dumps(task)
            self.values[key] = updated
            self.rpush(queue_key, task_id)
            self.xadd(event_stream, {"event": "status", "task_id": task_id, "data": "QUEUED"})
            return updated
        queue_key, prefix, event_stream = keys
        (timestamp,) = args
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

    for method in ("enqueue", "submit_or_refresh", "get", "resolve_task_id", "find_by_symbol", "deduplicate_by_symbol", "refresh_task", "transition", "complete_capture", "events_after", "cleanup", "next_queued", "recover_running", "requeue_waiting", "retry_failed"):
        assert callable(getattr(store, method, None))


def test_redis_refresh_reuses_the_task_id_and_clears_the_terminal_payload() -> None:
    redis = FakeRedis()
    store = RedisStreamsStore(redis)
    task = TaskRecord(task_id="refresh", symbol="600938", include_long_capture=False)
    store.enqueue(task)
    store.next_queued()
    store.complete_result(task.task_id, {kind: f"value-{kind.value}" for kind in MetricKind}, None)

    refreshed = store.refresh_task(task.task_id)
    restored = RedisStreamsStore(redis).get(task.task_id)

    assert refreshed.task_id == "refresh"
    assert refreshed.status == TaskStatus.QUEUED
    assert restored is not None
    assert restored.values[MetricKind.STOCK_NAME] is None
    assert redis.lists["ths:jobs:pending"] == ["refresh"]


def test_redis_deduplication_deletes_old_payload_queue_events_and_keeps_alias() -> None:
    redis = FakeRedis()
    store = RedisStreamsStore(redis)
    older = TaskRecord(task_id="older", symbol="600938", include_long_capture=False)
    older.created_at = older.created_at.replace(year=2025)
    older.updated_at = older.created_at
    newer = TaskRecord(task_id="newer", symbol="600938", include_long_capture=False)
    store.enqueue(older)
    store.enqueue(newer)

    result = store.deduplicate_by_symbol()

    assert result == {"total": 2, "kept": 1, "deleted": 1, "aliases": 1}
    assert "ths:jobs:task:older" not in redis.values
    assert redis.lists["ths:jobs:pending"] == ["newer"]
    assert all(fields["task_id"] != "older" for _, fields in redis.streams["ths:jobs:events"])
    assert store.resolve_task_id("older") == "newer"
    assert store.get("older").task_id == "newer"


def test_redis_submit_or_refresh_uses_one_symbol_index_across_store_instances() -> None:
    redis = FakeRedis()
    first = RedisStreamsStore(redis)
    second = RedisStreamsStore(redis)

    first_result = first.submit_or_refresh(TaskRecord(task_id="first", symbol="601872", include_long_capture=False))
    second_result = second.submit_or_refresh(TaskRecord(task_id="second", symbol="601872", include_long_capture=False))

    assert first_result.task_id == "first"
    assert second_result.task_id == "first"
    assert redis.hashes["ths:jobs:symbols"] == {"601872": "first"}
    assert redis.sets["ths:jobs:tasks"] == {"first"}


def test_redis_store_rejects_an_incomplete_client_before_app_construction() -> None:
    """Deferring a missing Redis method until the first public request hides deployment errors."""
    with pytest.raises(TypeError, match="RedisStreamsStore requires redis client methods"):
        RedisStreamsStore(object())


def test_redis_pending_cap_is_atomic_across_concurrent_submissions() -> None:
    class InterleavingRedis(FakeRedis):
        def __init__(self) -> None:
            super().__init__()
            self.barrier = Barrier(2)

        def smembers(self, key):
            result = super().smembers(key)
            self.barrier.wait()
            return result

    redis = InterleavingRedis()
    store = RedisStreamsStore(redis, pending_cap=1)
    results: list[Exception | None] = []

    def enqueue(task_id: str) -> None:
        try:
            store.enqueue(TaskRecord(task_id=task_id, symbol="600938"))
        except Exception as error:
            results.append(error)
        else:
            results.append(None)

    threads = [Thread(target=enqueue, args=(task_id,)) for task_id in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(None) == 1
    assert sum(result is not None for result in results) == 1


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


def test_redis_events_accept_stream_cursor_and_limit_reads() -> None:
    redis = FakeRedis()
    store = RedisStreamsStore(redis, event_retention=20)
    task = TaskRecord(task_id="cursor", symbol="600938")
    store.enqueue(task)
    store.next_queued()
    store.transition(task.task_id, TaskStatus.WAITING_ADMIN)

    events = store.events_after(task.task_id, "1", limit=1)

    assert [event["data"] for event in events] == ["RUNNING"]


def test_redis_events_after_cursor_returns_next_global_cursor() -> None:
    redis = FakeRedis()
    store = RedisStreamsStore(redis, event_retention=20)
    first = TaskRecord(task_id="cursor-first", symbol="600938")
    second = TaskRecord(task_id="cursor-second", symbol="000001")
    store.enqueue(first)
    store.enqueue(second)

    events, cursor = store.events_after_cursor(first.task_id)

    assert [event["data"] for event in events] == ["QUEUED"]
    assert cursor == "2"
    events, next_cursor = store.events_after_cursor(first.task_id, cursor)
    assert events == []
    assert next_cursor == cursor


def test_redis_event_retention_bounds_stream_size() -> None:
    redis = FakeRedis()
    store = RedisStreamsStore(redis, event_retention=2)
    for index in range(3):
        task = TaskRecord(task_id=f"retention-{index}", symbol=f"6009{index:02d}")
        store.enqueue(task)

    assert len(redis.streams["ths:jobs:events"]) <= 2


def test_redis_transition_persists_state_and_event_together() -> None:
    class FailingEmitRedis(FakeRedis):
        def xadd(self, key, fields):
            if key == "ths:jobs:events" and fields.get("data") == "WAITING_ADMIN":
                raise RuntimeError("event write failed")
            return super().xadd(key, fields)

    redis = FailingEmitRedis()
    store = RedisStreamsStore(redis)
    task = TaskRecord(task_id="atomic", symbol="600938")
    store.enqueue(task)
    store.next_queued()

    with pytest.raises(RuntimeError, match="event write failed"):
        store.transition(task.task_id, TaskStatus.WAITING_ADMIN)

    assert store.get(task.task_id).status == TaskStatus.RUNNING


def test_redis_restart_recovery_atomically_requeues_running_work_before_later_jobs() -> None:
    redis = FakeRedis()
    original = RedisStreamsStore(redis)
    original.enqueue(TaskRecord(task_id="interrupted", symbol="601872"))
    original.enqueue(TaskRecord(task_id="later", symbol="600938"))
    assert original.next_queued().task_id == "interrupted"

    restarted = RedisStreamsStore(redis)
    recovered = restarted.recover_running()

    assert [task.task_id for task in recovered] == ["interrupted"]
    assert redis.lists["ths:jobs:pending"] == ["interrupted", "later"]
    assert [event["data"] for event in restarted.events_after("interrupted")] == [
        "QUEUED", "RUNNING", "QUEUED",
    ]
    assert restarted.recover_running() == []
    assert restarted.next_queued().task_id == "interrupted"


def test_redis_store_round_trips_the_long_result_and_values() -> None:
    """A process restart must not lose the image URL or recognized values."""
    redis = FakeRedis()
    store = RedisStreamsStore(redis)
    store.enqueue(TaskRecord(task_id="long", symbol="601872"))
    store.next_queued()

    store.complete_result(
        "long",
        {
            MetricKind.STOCK_NAME: "招商轮船",
            MetricKind.CURRENT_PRICE: "19.78",
            MetricKind.CHANGE_PERCENT: "7.15%",
            MetricKind.TURNOVER_RATE: "2.40%",
            MetricKind.LARGE_ORDER_NET: "-0.02",
            MetricKind.LARGE_ORDER_AMOUNT: "-2802.6万",
            MetricKind.RETAIL_COUNT: "21.23",
            MetricKind.MACDFS: "+0.012",
        },
        "/tmp/LONG.png",
        ocr_metrics={MetricKind.LARGE_ORDER_AMOUNT},
        intraday_series={
            MetricKind.LARGE_ORDER_NET: {
                "unit": None,
                "points": [
                    {"time": "09:30", "value": "-0.03"},
                    {"time": "09:31", "value": "-0.02"},
                ],
            }
        },
    )
    restored = RedisStreamsStore(redis).get("long")

    assert restored is not None and restored.status == TaskStatus.COMPLETED
    assert restored.long_capture.path.as_posix() == "/tmp/LONG.png"
    assert restored.values[MetricKind.LARGE_ORDER_AMOUNT] == "-2802.6万"
    assert restored.value_sources[MetricKind.LARGE_ORDER_AMOUNT].value == "OCR"
    assert restored.value_sources[MetricKind.LARGE_ORDER_NET].value == "INTERFACE"
    assert restored.intraday_series[MetricKind.LARGE_ORDER_NET] == {
        "unit": None,
        "points": [
            {"time": "09:30", "value": "-0.03"},
            {"time": "09:31", "value": "-0.02"},
        ],
    }
    assert restored.collected_at is not None


def test_redis_store_round_trips_the_data_only_option() -> None:
    """A restart must not turn a data-only task back into a screenshot task."""
    redis = FakeRedis()
    store = RedisStreamsStore(redis)
    store.enqueue(TaskRecord(task_id="data-only", symbol="601872", include_long_capture=False))

    restored = RedisStreamsStore(redis).get("data-only")

    assert restored is not None
    assert restored.include_long_capture is False
    assert restored.long_capture.status.value == "SKIPPED"


def test_redis_store_round_trips_source_errors_and_defaults_legacy_tasks_to_empty() -> None:
    redis = FakeRedis()
    store = RedisStreamsStore(redis)
    store.enqueue(TaskRecord(task_id="fund-error", symbol="600938", include_long_capture=False))
    store.next_queued()
    store.complete_result(
        "fund-error",
        {kind: f"value-{kind.value}" for kind in MetricKind},
        None,
        source_errors={"main_fund_flow": "DIRECT_FUND_FLOW_TIMEOUT"},
    )

    restored = RedisStreamsStore(redis).get("fund-error")

    assert restored is not None
    assert restored.source_errors == {
        "core_metrics": None,
        "main_fund_flow": "DIRECT_FUND_FLOW_TIMEOUT",
    }

    legacy = RedisStreamsStore._deserialize(json.dumps({
        "task_id": "old-task",
        "symbol": "601872",
        "status": "COMPLETED",
        "created_at": "2026-08-21T00:00:00+00:00",
        "updated_at": "2026-08-21T00:01:00+00:00",
        "completed_at": "2026-08-21T00:01:00+00:00",
        "error_code": None,
        "captures": {},
    }))
    assert legacy.source_errors == {
        "core_metrics": None,
        "main_fund_flow": None,
    }


def test_redis_store_completes_a_data_only_task_without_an_image_path() -> None:
    """Production persistence must accept direct values without inventing a PNG path."""
    redis = FakeRedis()
    store = RedisStreamsStore(redis)
    store.enqueue(TaskRecord(task_id="data-only-result", symbol="601872", include_long_capture=False))
    store.next_queued()

    result = store.complete_result(
        "data-only-result",
        {kind: f"value-{kind.value}" for kind in MetricKind},
        None,
    )
    restored = RedisStreamsStore(redis).get("data-only-result")

    assert result.status == TaskStatus.COMPLETED
    assert restored is not None and restored.status == TaskStatus.COMPLETED
    assert restored.long_capture.status.value == "SKIPPED"
    assert restored.long_capture.path is None
    assert restored.collected_at is not None


def test_redis_store_reads_tasks_written_before_long_results_existed() -> None:
    """Deploying the new schema must not make queued or completed legacy tasks unreadable."""
    payload = json.dumps({
        "task_id": "legacy",
        "symbol": "601872",
        "status": "QUEUED",
        "created_at": "2026-08-21T00:00:00+00:00",
        "updated_at": "2026-08-21T00:00:00+00:00",
        "completed_at": None,
        "error_code": None,
        "captures": {
            kind.value: {"status": "PENDING", "path": None, "captured_at": None}
            for kind in CaptureKind
        },
    })

    restored = RedisStreamsStore._deserialize(payload)

    assert all(value is None for value in restored.values.values())
    assert restored.include_long_capture is True
    assert restored.long_capture.status.value == "PENDING"


def test_redis_store_reads_tasks_written_with_only_the_three_legacy_values() -> None:
    """Adding quote and MACDFS fields must not make existing completed tasks unreadable."""
    payload = json.dumps({
        "task_id": "legacy-values",
        "symbol": "601872",
        "status": "COMPLETED",
        "created_at": "2026-08-21T00:00:00+00:00",
        "updated_at": "2026-08-21T00:01:00+00:00",
        "completed_at": "2026-08-21T00:01:00+00:00",
        "error_code": None,
        "captures": {
            kind.value: {"status": "PENDING", "path": None, "captured_at": None}
            for kind in CaptureKind
        },
        "values": {
            "LARGE_ORDER_NET": "-0.02",
            "LARGE_ORDER_AMOUNT": "-2802.6万",
            "RETAIL_COUNT": "21.23",
        },
    })

    restored = RedisStreamsStore._deserialize(payload)

    assert restored.values[MetricKind.LARGE_ORDER_NET] == "-0.02"
    assert restored.values[MetricKind.STOCK_NAME] is None


def test_redis_requeue_waiting_is_atomic_and_idempotent() -> None:
    """Concurrent resume clicks must yield one pending entry and one QUEUED event."""
    redis = FakeRedis()
    first = RedisStreamsStore(redis)
    second = RedisStreamsStore(redis)
    first.enqueue(TaskRecord(task_id="waiting", symbol="SZ.000001"))
    first.next_queued()
    first.transition("waiting", TaskStatus.WAITING_ADMIN, error_code="WAITING_ADMIN")

    assert first.requeue_waiting("waiting").status == TaskStatus.QUEUED
    assert second.requeue_waiting("waiting").status == TaskStatus.QUEUED

    assert redis.lists["ths:jobs:pending"] == ["waiting"]
    assert [event["data"] for event in first.events_after("waiting")] == ["QUEUED", "RUNNING", "WAITING_ADMIN", "QUEUED"]


def test_redis_waiting_recovery_is_requeued_after_already_queued_jobs() -> None:
    redis = FakeRedis()
    store = RedisStreamsStore(redis)
    store.enqueue(TaskRecord(task_id="waiting", symbol="600938"))
    store.enqueue(TaskRecord(task_id="later", symbol="000001"))
    store.next_queued()
    store.transition("waiting", TaskStatus.WAITING_ADMIN, error_code="WAITING_ADMIN")

    store.requeue_waiting("waiting")

    assert store.next_queued().task_id == "later"


def test_redis_retry_failed_is_atomic_and_preserves_capture_state() -> None:
    redis = FakeRedis()
    store = RedisStreamsStore(redis)
    store.enqueue(TaskRecord(task_id="failed", symbol="SZ.000001"))
    store.next_queued()
    store.complete_capture("failed", CaptureKind.LARGE_ORDER_NET, "/tmp/net.png")
    store.transition("failed", TaskStatus.FAILED, error_code="DEVICE_OFFLINE")

    assert store.retry_failed("failed").status == TaskStatus.QUEUED
    assert RedisStreamsStore(redis).retry_failed("failed").status == TaskStatus.QUEUED
    assert redis.lists["ths:jobs:pending"] == ["failed"]
    assert store.get("failed").captures[CaptureKind.LARGE_ORDER_NET].status.value == "READY"


def test_public_routes_work_when_the_redis_adapter_is_configured() -> None:
    """Injecting a real-store adapter must not make public submission return a server error."""
    client = TestClient(create_app(store=RedisStreamsStore(FakeRedis())))

    created = client.post("/api/v1/jobs", json={"symbol": "600938"})
    public_id = created.json()["public_id"]

    assert created.status_code == 202
    assert client.get(f"/api/v1/jobs/{public_id}").status_code == 200
