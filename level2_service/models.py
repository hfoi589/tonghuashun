"""Stable API and queue-domain values shared by the web service and runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CaptureKind(str, Enum):
    LARGE_ORDER_NET = "LARGE_ORDER_NET"
    LARGE_ORDER_AMOUNT = "LARGE_ORDER_AMOUNT"
    RETAIL_COUNT = "RETAIL_COUNT"


class CaptureStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    EXPIRED = "EXPIRED"


class TaskStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_ADMIN = "WAITING_ADMIN"
    PARTIAL = "PARTIAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass
class CaptureRecord:
    kind: CaptureKind
    status: CaptureStatus = CaptureStatus.PENDING
    path: Path | None = None
    captured_at: datetime | None = None


@dataclass
class TaskRecord:
    task_id: str
    symbol: str
    status: TaskStatus = TaskStatus.QUEUED
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None
    error_code: str | None = None
    captures: dict[CaptureKind, CaptureRecord] = field(
        default_factory=lambda: {kind: CaptureRecord(kind) for kind in CaptureKind}
    )

    @property
    def capture_expires_at(self) -> datetime:
        return self.created_at + timedelta(hours=24)

    @property
    def metadata_expires_at(self) -> datetime:
        return self.created_at + timedelta(days=7)

    def as_public(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "symbol": self.symbol,
            "status": self.status.value,
            "error_code": self.error_code,
            "created_at": self.created_at.isoformat(),
            "capture_expires_at": self.capture_expires_at.isoformat(),
            "captures": [
                {
                    "kind": kind.value,
                    "status": self.captures[kind].status.value,
                    "url": (
                        f"/api/tasks/{self.task_id}/captures/{kind.value}"
                        if self.captures[kind].status == CaptureStatus.READY
                        else None
                    ),
                }
                for kind in CaptureKind
            ],
        }
