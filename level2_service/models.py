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
    MAIN_FLOW_TODAY_NET = "MAIN_FLOW_TODAY_NET"
    MAIN_FLOW_TODAY_VISIBLE = "MAIN_FLOW_TODAY_VISIBLE"
    MAIN_FLOW_TODAY_HIDDEN = "MAIN_FLOW_TODAY_HIDDEN"
    MAIN_FLOW_TODAY_RETAIL = "MAIN_FLOW_TODAY_RETAIL"
    MAIN_FLOW_THREE_DAY_NET = "MAIN_FLOW_THREE_DAY_NET"
    MAIN_FLOW_THREE_DAY_VISIBLE = "MAIN_FLOW_THREE_DAY_VISIBLE"
    MAIN_FLOW_THREE_DAY_HIDDEN = "MAIN_FLOW_THREE_DAY_HIDDEN"
    MAIN_FLOW_THREE_DAY_RETAIL = "MAIN_FLOW_THREE_DAY_RETAIL"
    MAIN_FLOW_FIVE_DAY_NET = "MAIN_FLOW_FIVE_DAY_NET"
    MAIN_FLOW_FIVE_DAY_VISIBLE = "MAIN_FLOW_FIVE_DAY_VISIBLE"
    MAIN_FLOW_FIVE_DAY_HIDDEN = "MAIN_FLOW_FIVE_DAY_HIDDEN"
    MAIN_FLOW_FIVE_DAY_RETAIL = "MAIN_FLOW_FIVE_DAY_RETAIL"
    MAIN_FLOW_TODAY_UNIT = "MAIN_FLOW_TODAY_UNIT"
    MAIN_FLOW_THREE_DAY_UNIT = "MAIN_FLOW_THREE_DAY_UNIT"
    MAIN_FLOW_FIVE_DAY_UNIT = "MAIN_FLOW_FIVE_DAY_UNIT"


REQUIRED_METRICS = frozenset(
    {
        MetricKind.STOCK_NAME,
        MetricKind.CURRENT_PRICE,
        MetricKind.CHANGE_PERCENT,
        MetricKind.TURNOVER_RATE,
        MetricKind.RETAIL_COUNT,
        MetricKind.LARGE_ORDER_NET,
        MetricKind.LARGE_ORDER_AMOUNT,
        MetricKind.MACDFS,
    }
)

FUND_FLOW_PERIODS = (
    ("today", "当日", MetricKind.MAIN_FLOW_TODAY_UNIT),
    ("three_day", "3日", MetricKind.MAIN_FLOW_THREE_DAY_UNIT),
    ("five_day", "5日", MetricKind.MAIN_FLOW_FIVE_DAY_UNIT),
)

FUND_FLOW_METRICS = {
    "today": {
        "main_net_inflow": MetricKind.MAIN_FLOW_TODAY_NET,
        "main_visible_inflow": MetricKind.MAIN_FLOW_TODAY_VISIBLE,
        "main_hidden_inflow": MetricKind.MAIN_FLOW_TODAY_HIDDEN,
        "retail_inflow": MetricKind.MAIN_FLOW_TODAY_RETAIL,
    },
    "three_day": {
        "main_net_inflow": MetricKind.MAIN_FLOW_THREE_DAY_NET,
        "main_visible_inflow": MetricKind.MAIN_FLOW_THREE_DAY_VISIBLE,
        "main_hidden_inflow": MetricKind.MAIN_FLOW_THREE_DAY_HIDDEN,
        "retail_inflow": MetricKind.MAIN_FLOW_THREE_DAY_RETAIL,
    },
    "five_day": {
        "main_net_inflow": MetricKind.MAIN_FLOW_FIVE_DAY_NET,
        "main_visible_inflow": MetricKind.MAIN_FLOW_FIVE_DAY_VISIBLE,
        "main_hidden_inflow": MetricKind.MAIN_FLOW_FIVE_DAY_HIDDEN,
        "retail_inflow": MetricKind.MAIN_FLOW_FIVE_DAY_RETAIL,
    },
}

INTRADAY_METRICS = (
    ("large_order_net", MetricKind.LARGE_ORDER_NET, None),
    ("large_order_amount", MetricKind.LARGE_ORDER_AMOUNT, "万"),
    ("retail_count", MetricKind.RETAIL_COUNT, None),
)

SOURCE_ERROR_KEYS = ("core_metrics", "main_fund_flow")


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
    source_errors: dict[str, str | None] = field(
        default_factory=lambda: {key: None for key in SOURCE_ERROR_KEYS}
    )
    captures: dict[CaptureKind, CaptureRecord] = field(
        default_factory=lambda: {kind: CaptureRecord(kind) for kind in CaptureKind}
    )
    values: dict[MetricKind, str | None] = field(
        default_factory=lambda: {kind: None for kind in MetricKind}
    )
    value_sources: dict[MetricKind, ValueSource | None] = field(
        default_factory=lambda: {kind: None for kind in MetricKind}
    )
    intraday_series: dict[MetricKind, dict[str, Any]] = field(
        default_factory=lambda: normalized_intraday_series(None)
    )
    long_capture: LongCaptureRecord = field(default_factory=LongCaptureRecord)

    def __post_init__(self) -> None:
        if not self.include_long_capture and self.long_capture.status == CaptureStatus.PENDING:
            self.long_capture.status = CaptureStatus.SKIPPED

    @property
    def metadata_expires_at(self) -> datetime:
        return self.created_at + timedelta(days=7)

    def as_public(self) -> dict[str, Any]:
        main_fund_flow: dict[str, dict[str, str | None]] = {}
        main_fund_flow_sources: dict[str, dict[str, str | None]] = {}
        intraday_series: dict[str, dict[str, Any]] = {}
        intraday_series_sources: dict[str, str | None] = {}
        for period, _, unit_kind in FUND_FLOW_PERIODS:
            metrics = FUND_FLOW_METRICS[period]
            main_fund_flow[period] = {
                "unit": self.values[unit_kind],
                **{
                    field: self.values[kind]
                    for field, kind in metrics.items()
                },
            }
            main_fund_flow_sources[period] = {
                field: self._public_value_source(kind)
                for field, kind in metrics.items()
            }
        for field_name, kind, default_unit in INTRADAY_METRICS:
            series = self.intraday_series.get(
                kind,
                {"unit": default_unit, "points": []},
            )
            points = [dict(point) for point in series.get("points", [])]
            intraday_series[field_name] = {
                "unit": series.get("unit", default_unit),
                "points": points,
            }
            intraday_series_sources[field_name] = (
                ValueSource.INTERFACE.value
                if any(point.get("value") is not None for point in points)
                else None
            )
        return {
            "public_id": self.task_id,
            "symbol": self.symbol,
            "include_long_capture": self.include_long_capture,
            "status": self.status.value,
            "error_code": self.error_code,
            "source_errors": {
                key: self.source_errors.get(key)
                for key in SOURCE_ERROR_KEYS
            },
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
                "intraday_series": intraday_series,
                "main_fund_flow": main_fund_flow,
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
                "intraday_series": intraday_series_sources,
                "main_fund_flow": main_fund_flow_sources,
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


def normalized_intraday_series(
    provided: dict[MetricKind, dict[str, Any]] | None,
) -> dict[MetricKind, dict[str, Any]]:
    source = provided or {}
    normalized: dict[MetricKind, dict[str, Any]] = {}
    for _, kind, default_unit in INTRADAY_METRICS:
        series = source.get(kind, {})
        raw_points = series.get("points", []) if isinstance(series, dict) else []
        points = [
            {"time": point.get("time"), "value": point.get("value")}
            for point in raw_points
            if isinstance(point, dict) and point.get("time") is not None
        ]
        normalized[kind] = {
            "unit": series.get("unit", default_unit) if isinstance(series, dict) else default_unit,
            "points": points,
        }
    return normalized
