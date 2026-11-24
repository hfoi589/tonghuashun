"""Periodic, de-duplicated persistence of the App fund-flow interface."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from .market_data import fixed_market_error_code, is_china_market_open
from .models import FUND_FLOW_METRICS, FUND_FLOW_PERIODS, MetricKind
from .parsed_values import (
    DirectReadOutcome,
    UnsupportedMarketError,
    market_code_for_symbol,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
FUND_FLOW_VALUE_FIELDS = (
    "main_net_inflow",
    "main_visible_inflow",
    "main_hidden_inflow",
    "retail_inflow",
)


def has_fund_flow_values(row: Mapping[str, object]) -> bool:
    """Return True when at least one fund-flow metric (not merely unit) is populated."""
    return any(row.get(field) is not None for field in FUND_FLOW_VALUE_FIELDS)


def supports_main_fund_flow(symbol: str) -> bool:
    """Exchange-traded funds (`20`/`36`) do not expose THS main/dark-pool fund flow."""
    try:
        return market_code_for_symbol(symbol) not in {"20", "36"}
    except UnsupportedMarketError:
        return False


def is_today_fund_flow_ready(now: datetime) -> bool:
    """Return True once call auction matching settles today's fund-flow endpoint at 09:29."""
    return now.astimezone(SHANGHAI).strftime("%H%M") >= "0929"


class FundFlowHistoryMonitor:
    """Collect one shared fund-flow sample per symbol and refresh cycle."""

    def __init__(
        self,
        accounts: object,
        source: object,
        store: object,
        *,
        is_market_open: Callable[[], bool] = is_china_market_open,
        clock: Callable[[], datetime] = lambda: datetime.now(SHANGHAI),
        max_concurrent: int = 4,
        retention_days: int = 180,
    ) -> None:
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be positive")
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
        self.accounts = accounts
        self.source = source
        self.store = store
        self.is_market_open = is_market_open
        self.clock = clock
        self.max_concurrent = max_concurrent
        self.retention_days = retention_days
        self._last_cleanup_date: str | None = None

    @staticmethod
    def _periods(
        outcome: DirectReadOutcome | dict[MetricKind, str | None],
        *,
        allow_today: bool = True,
    ) -> dict[str, dict[str, str | None]]:
        values = outcome.values if isinstance(outcome, DirectReadOutcome) else outcome
        periods: dict[str, dict[str, str | None]] = {}
        for period, _label, unit_kind in FUND_FLOW_PERIODS:
            if period == "today" and not allow_today:
                continue
            metrics = FUND_FLOW_METRICS[period]
            row = {
                "unit": values.get(unit_kind),
                **{name: values.get(kind) for name, kind in metrics.items()},
            }
            if has_fund_flow_values(row):
                periods[period] = row
        return periods

    @staticmethod
    def _quality(periods: dict[str, dict[str, str | None]]) -> str:
        metric_values = [
            row.get(name)
            for row in periods.values()
            for name in FUND_FLOW_VALUE_FIELDS
        ]
        if not metric_values or not any(value is not None for value in metric_values):
            return "EMPTY"
        return "VALID" if all(value is not None for value in metric_values) else "PARTIAL"

    async def poll_once(self) -> dict[str, bool]:
        if not self.is_market_open():
            return {}
        raw_symbols = await asyncio.to_thread(self.accounts.list_watchlist_symbols)
        symbols = sorted({
            str(symbol)
            for symbol in raw_symbols
            if supports_main_fund_flow(str(symbol))
        })
        if not symbols:
            return {}
        now = self.clock().astimezone(SHANGHAI)
        trade_date = now.strftime("%Y%m%d")
        minute = now.strftime("%H:%M")
        allow_today = is_today_fund_flow_ready(now)
        await self._cleanup_if_due(now)
        semaphore = asyncio.Semaphore(self.max_concurrent)

        async def collect(symbol: str) -> tuple[str, bool]:
            async with semaphore:
                try:
                    result = await asyncio.to_thread(self.source.read_direct, symbol)
                    if not isinstance(result, DirectReadOutcome) and not isinstance(result, dict):
                        raise RuntimeError("fund flow source returned an invalid result")
                    periods = self._periods(result, allow_today=allow_today)
                    if not periods:
                        if not allow_today:
                            return symbol, False
                        await asyncio.to_thread(
                            self.store.record_fund_flow_error,
                            symbol,
                            "DIRECT_FUND_FLOW_EMPTY",
                        )
                        return symbol, False
                    quality = self._quality(periods)
                    if quality == "EMPTY":
                        if not allow_today:
                            return symbol, False
                        await asyncio.to_thread(
                            self.store.record_fund_flow_error,
                            symbol,
                            "DIRECT_FUND_FLOW_EMPTY",
                        )
                        return symbol, False
                    changed = await asyncio.to_thread(
                        self.store.record_fund_flow,
                        symbol,
                        trade_date=trade_date,
                        minute=minute,
                        periods=periods,
                        captured_at=now,
                    )
                    if quality != "VALID":
                        await asyncio.to_thread(
                            self.store.record_fund_flow_error,
                            symbol,
                            f"DIRECT_FUND_FLOW_{quality}",
                        )
                    return symbol, bool(changed) if quality == "VALID" else False
                except Exception as error:
                    code = fixed_market_error_code(
                        error,
                        "DIRECT_FUND_FLOW_REQUEST_FAILED",
                    )
                    await asyncio.to_thread(
                        self.store.record_fund_flow_error,
                        symbol,
                        code,
                    )
                    return symbol, False

        rows = await asyncio.gather(*(collect(symbol) for symbol in symbols))
        return dict(rows)

    async def _cleanup_if_due(self, now: datetime) -> None:
        today = now.strftime("%Y%m%d")
        if self._last_cleanup_date == today:
            return
        cutoff = (now - timedelta(days=self.retention_days)).strftime("%Y%m%d")
        await asyncio.to_thread(self.store.cleanup_fund_flow_history, cutoff)
        self._last_cleanup_date = today
