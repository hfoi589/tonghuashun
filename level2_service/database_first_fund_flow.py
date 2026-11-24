"""Database-first fund-flow source shared by Market and task reads."""

from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .fund_flow_history import (
    has_fund_flow_values,
    is_today_fund_flow_ready,
    supports_main_fund_flow,
)
from .models import FUND_FLOW_METRICS, FUND_FLOW_PERIODS, MetricKind
from .parsed_values import DirectReadOutcome, empty_metric_values


SHANGHAI = ZoneInfo("Asia/Shanghai")


class DatabaseFirstFundFlowSource:
    """Use recent shared history before invoking the selected fund interface."""

    def __init__(
        self,
        live_source: object,
        accounts: object,
        store: object,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(SHANGHAI),
        is_market_open: Callable[[], bool] = lambda: True,
        freshness_seconds: float = 120.0,
    ) -> None:
        if freshness_seconds <= 0:
            raise ValueError("freshness_seconds must be positive")
        self.live_source = live_source
        self.accounts = accounts
        self.store = store
        self.clock = clock
        self.is_market_open = is_market_open
        self.freshness_seconds = freshness_seconds
        self._closed_fallbacks: dict[tuple[str, str], DirectReadOutcome] = {}
        self._lock = RLock()

    def __getattr__(self, name: str) -> Any:
        """Preserve transport introspection used by deployment diagnostics."""
        return getattr(self.live_source, name)

    def invalidate(self) -> None:
        with self._lock:
            self._closed_fallbacks.clear()
        invalidator = getattr(self.live_source, "invalidate", None)
        if callable(invalidator):
            invalidator()

    @staticmethod
    def _outcome_from_history(history: dict[str, Any]) -> DirectReadOutcome:
        values = {kind: None for kind in MetricKind}
        for period, _label, unit_kind in FUND_FLOW_PERIODS:
            row = history["periods"][period]
            values[unit_kind] = row.get("unit")
            for name, kind in FUND_FLOW_METRICS[period].items():
                values[kind] = row.get(name)
        return DirectReadOutcome(
            values=values,
            source_errors={"core_metrics": None, "main_fund_flow": None},
        )

    @staticmethod
    def _periods_from_outcome(
        outcome: DirectReadOutcome,
        *,
        allow_today: bool = True,
    ) -> dict[str, dict[str, str | None]]:
        values = outcome.values
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
    def _strip_today_period(outcome: DirectReadOutcome) -> DirectReadOutcome:
        values = dict(outcome.values)
        values[MetricKind.MAIN_FLOW_TODAY_UNIT] = None
        for kind in FUND_FLOW_METRICS["today"].values():
            values[kind] = None
        return DirectReadOutcome(
            values=values,
            source_errors=dict(outcome.source_errors),
            intraday_series=dict(outcome.intraday_series),
            stored_trade_dates=dict(outcome.stored_trade_dates),
        )

    def _is_watchlist_symbol(self, symbol: str) -> bool:
        return symbol in set(self.accounts.list_watchlist_symbols())

    def _closed_key(self, symbol: str, now: datetime) -> tuple[str, str]:
        return symbol, now.strftime("%Y%m%d")

    def read_direct(self, symbol: str) -> DirectReadOutcome:
        if not supports_main_fund_flow(symbol):
            return DirectReadOutcome(
                values=empty_metric_values(),
                source_errors={"core_metrics": None, "main_fund_flow": None},
            )
        now = self.clock().astimezone(SHANGHAI)
        market_open = self.is_market_open()
        allow_today = (not market_open) or is_today_fund_flow_ready(now)
        if self._is_watchlist_symbol(symbol):
            if market_open:
                history = (
                    self.store.latest_fund_flow_snapshot(
                        symbol,
                        now=now,
                        max_age_seconds=self.freshness_seconds,
                        trade_date=now.strftime("%Y%m%d"),
                    )
                    if allow_today
                    else None
                )
            else:
                history = self.store.latest_fund_flow_snapshot(symbol, trade_date=None)
            if history is not None:
                return self._outcome_from_history(history)

        if not market_open:
            key = self._closed_key(symbol, now)
            with self._lock:
                cached = self._closed_fallbacks.get(key)
            if cached is not None:
                return cached

        result = self.live_source.read_direct(symbol)
        outcome = result if isinstance(result, DirectReadOutcome) else DirectReadOutcome(
            values=result,
            source_errors={"core_metrics": None, "main_fund_flow": None},
        )
        if market_open and not allow_today:
            periods = self._periods_from_outcome(outcome, allow_today=False)
            if self._is_watchlist_symbol(symbol) and periods:
                self.store.record_fund_flow(
                    symbol,
                    trade_date=now.strftime("%Y%m%d"),
                    minute=now.strftime("%H:%M"),
                    periods=periods,
                    captured_at=now,
                )
            return self._strip_today_period(outcome)
        if market_open and self._is_watchlist_symbol(symbol):
            periods = self._periods_from_outcome(outcome, allow_today=True)
            if periods:
                self.store.record_fund_flow(
                    symbol,
                    trade_date=now.strftime("%Y%m%d"),
                    minute=now.strftime("%H:%M"),
                    periods=periods,
                    captured_at=now,
                )
        if not market_open:
            with self._lock:
                self._closed_fallbacks[self._closed_key(symbol, now)] = outcome
        return outcome
