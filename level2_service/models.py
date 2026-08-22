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


class MetricKind(str, Enum):
    STOCK_NAME = "STOCK_NAME"
    CURRENT_PRICE = "CURRENT_PRICE"
    CHANGE_PERCENT = "CHANGE_PERCENT"
    TURNOVER_RATE = "TURNOVER_RATE"
    RETAIL_COUNT = "RETAIL_COUNT"
    LARGE_ORDER_NET = "LARGE_ORDER_NET"
    LARGE_ORDER_AMOUNT = "LARGE_ORDER_AMOUNT"
    MACDFS = "MACDFS"


class ValueSource(str, Enum):
    INTERFACE = "INTERFACE"
    OCR = "OCR"


class CaptureStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    SKIPPED = "SKIPPED"
    EXPIRED = "EXPIRED"


class TaskStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_ADMIN = "WAITING_ADMIN"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


@dataclass
class CaptureRecord:
    kind: CaptureKind
    status: CaptureStatus = CaptureStatus.PENDING
    path: Path | None = None
    captured_at: datetime | None = None

    @property
    def expires_at(self) -> datetime | None:
        return self.captured_at + timedelta(hours=24) if self.captured_at else None


@dataclass
class LongCaptureRecord:
    status: CaptureStatus = CaptureStatus.PENDING
    path: Path | None = None
    captured_at: datetime | None = None

    @property
    def expires_at(self) -> datetime | None:
        return self.captured_at + timedelta(hours=24) if self.captured_at else None


@dataclass
class TaskRecord:
    task_id: str
    symbol: str
    include_long_capture: bool = True
    status: TaskStatus = TaskStatus.QUEUED
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None
    collected_at: datetime | None = None
    error_code: str | None = None
    captures: dict[CaptureKind, CaptureRecord] = field(
        default_factory=lambda: {kind: CaptureRecord(kind) for kind in CaptureKind}
    )
    values: dict[MetricKind, str | None] = field(
        default_factory=lambda: {kind: None for kind in MetricKind}
    )
    value_sources: dict[MetricKind, ValueSource | None] = field(
        default_factory=lambda: {kind: None for kind in MetricKind}
    )
    long_capture: LongCaptureRecord = field(default_factory=LongCaptureRecord)

    def __post_init__(self) -> None:
        if not self.include_long_capture and self.long_capture.status == CaptureStatus.PENDING:
            self.long_capture.status = CaptureStatus.SKIPPED

    @property
    def metadata_expires_at(self) -> datetime:
        return self.created_at + timedelta(days=7)

    def as_public(self) -> dict[str, Any]:
        return {
            "public_id": self.task_id,
            "symbol": self.symbol,
            "include_long_capture": self.include_long_capture,
            "status": self.status.value,
            "error_code": self.error_code,
            "queue_position": None,
            "created_at": self.created_at.isoformat(),
            "collected_at": self.collected_at.isoformat() if self.collected_at else None,
            "captures": [
                {
                    "kind": kind.value,
                    "status": self.captures[kind].status.value,
                    "url": (
                        f"/api/v1/jobs/{self.task_id}/captures/{kind.value}"
                        if self.captures[kind].status == CaptureStatus.READY
                        else None
                    ),
                    "expires_at": (
                        self.captures[kind].expires_at.isoformat()
                        if self.captures[kind].expires_at
                        else None
                    ),
                }
                for kind in CaptureKind
            ],
            "values": {
                "stock_name": self.values[MetricKind.STOCK_NAME],
                "current_price": self.values[MetricKind.CURRENT_PRICE],
                "change_percent": self.values[MetricKind.CHANGE_PERCENT],
                "turnover_rate": self.values[MetricKind.TURNOVER_RATE],
                "large_order_net": self.values[MetricKind.LARGE_ORDER_NET],
                "large_order_amount": self.values[MetricKind.LARGE_ORDER_AMOUNT],
                "retail_count": self.values[MetricKind.RETAIL_COUNT],
                "macdfs": self.values[MetricKind.MACDFS],
            },
            "value_sources": {
                "stock_name": self._public_value_source(MetricKind.STOCK_NAME),
                "current_price": self._public_value_source(MetricKind.CURRENT_PRICE),
                "change_percent": self._public_value_source(MetricKind.CHANGE_PERCENT),
                "turnover_rate": self._public_value_source(MetricKind.TURNOVER_RATE),
                "large_order_net": self._public_value_source(MetricKind.LARGE_ORDER_NET),
                "large_order_amount": self._public_value_source(MetricKind.LARGE_ORDER_AMOUNT),
                "retail_count": self._public_value_source(MetricKind.RETAIL_COUNT),
                "macdfs": self._public_value_source(MetricKind.MACDFS),
            },
            "long_capture": {
                "status": self.long_capture.status.value,
                "url": (
                    f"/api/v1/jobs/{self.task_id}/capture"
                    if self.long_capture.status == CaptureStatus.READY
                    else None
                ),
                "expires_at": (
                    self.long_capture.expires_at.isoformat()
                    if self.long_capture.expires_at
                    else None
                ),
            },
        }

    def _public_value_source(self, kind: MetricKind) -> str | None:
        source = self.value_sources[kind]
        return source.value if source is not None else None
