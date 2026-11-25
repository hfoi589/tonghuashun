"""Periodic, de-duplicated persistence of the App fund-flow interface."""

from __future__ import annotations

import asyncio
import inspect
import re
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
_QUOTE_DATE_RE = re.compile(r"^\s*(20\d{2})[-/]?(0[1-9]|1[0-2])[-/]?(0[1-9]|[12]\d|3[01])")


def extract_quote_trade_date(source_time: object) -> str | None:
    """Extract YYYYMMDD from quote source_time when an explicit date prefix is present."""
    matched = _QUOTE_DATE_RE.match(str(source_time or ""))
    if not matched:
        return None
    return "".join(matched.groups())


def read_fund_flow_history_for_view(
    store: Any,
    symbol: str,
    trade_date: str | None = None,
    *,
    expected_intraday_trade_date: Callable[[], str | None] | None = None,
) -> dict[str, Any]:
    """Read fund-flow history without falling back to yesterday during today's session."""
    requested_date = trade_date.strip() if trade_date else None
    expected_today = (
        expected_intraday_trade_date()
        if callable(expected_intraday_trade_date)
        else None
    )
    if expected_today and (requested_date is None or requested_date == expected_today):
        try:
            return store.read_fund_flow_history(symbol, expected_today)
        except LookupError:
            avail = list(store.available_fund_flow_dates(symbol))
            merged_avail = [expected_today, *[d for d in avail if d != expected_today]]
            status_reader = getattr(store, "fund_flow_status", None)
            status = status_reader(symbol) if callable(status_reader) else {}
            return {
                "symbol": symbol,
                "trade_date": expected_today,
                "available_dates": merged_avail,
                "latest_sync_at": status.get("last_sync_at"),
                "last_error": status.get("last_error"),
                "periods": {
                    "today": {"points": []},
                    "three_day": {"points": []},
                    "five_day": {"points": []},
                },
            }
    history = dict(store.read_fund_flow_history(symbol, requested_date))
    if expected_today:
        avail = list(history.get("available_dates") or [])
        if expected_today not in avail:
            history["available_dates"] = [expected_today, *avail]
    return history


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
        is_post_close_window: Callable[[datetime], bool] | None = None,
        on_post_close_finalized: Callable[[list[str]], None] | None = None,
        session_notifier: Any | None = None,
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
        self.is_post_close_window = is_post_close_window
        self.on_post_close_finalized = on_post_close_finalized
        self.session_notifier = session_notifier
        self.clock = clock
        self.max_concurrent = max_concurrent
        self.retention_days = retention_days
        self._last_cleanup_date: str | None = None
        self._finalized_close_date: str | None = None

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

    @staticmethod
    def _supports_force_kwarg(fn: object) -> bool:
        try:
            sig = inspect.signature(fn)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        if "force" in sig.parameters:
            return True
        return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())

    async def poll_once(self, *, force: bool = False) -> dict[str, bool]:
        now = self.clock().astimezone(SHANGHAI)
        market_open = self.is_market_open()
        trade_date = now.strftime("%Y%m%d")
        minute = now.strftime("%H:%M")
        post_close = False
        if not force and not market_open:
            if self.is_post_close_window is None or not self.is_post_close_window(now):
                return {}
            if self._finalized_close_date == trade_date:
                return {}
            if now.strftime("%H%M%S") < "150130":
                return {}
            post_close = True
            minute = "15:00"
            allow_today = True
        else:
            allow_today = (not market_open) or is_today_fund_flow_ready(now)

        raw_symbols = await asyncio.to_thread(self.accounts.list_watchlist_symbols)
        symbols = sorted({
            str(symbol)
            for symbol in raw_symbols
            if supports_main_fund_flow(str(symbol))
        })
        if not symbols:
            return {}
        await self._cleanup_if_due(now)

        had_error = False
        batch_reader = getattr(self.source, "read_direct_batch", None)
        if callable(batch_reader):
            try:
                outcomes = await asyncio.to_thread(
                    batch_reader,
                    symbols,
                    force_all_periods=post_close,
                )
            except Exception as error:
                code = fixed_market_error_code(
                    error,
                    "DIRECT_FUND_FLOW_REQUEST_FAILED",
                )
                for symbol in symbols:
                    await asyncio.to_thread(
                        self.store.record_fund_flow_error,
                        symbol,
                        code,
                    )
                if self.session_notifier is not None and code in {
                    "DIRECT_SESSION_EXPIRED",
                    "DIRECT_SESSION_UNAVAILABLE",
                }:
                    try:
                        await asyncio.to_thread(
                            self.session_notifier.notify_error,
                            "main_fund_flow",
                            code,
                            stage="盘中暗盘定时轮询",
                        )
                    except Exception:
                        pass
                return {symbol: False for symbol in symbols}

            to_record: dict[str, dict[str, dict[str, str | None]]] = {}
            out: dict[str, bool] = {}
            partial_symbols: set[str] = set()
            for symbol in symbols:
                result = outcomes.get(symbol) if isinstance(outcomes, Mapping) else None
                if not isinstance(result, DirectReadOutcome) and not isinstance(result, dict):
                    had_error = True
                    await asyncio.to_thread(
                        self.store.record_fund_flow_error,
                        symbol,
                        "DIRECT_FUND_FLOW_REQUEST_FAILED",
                    )
                    out[symbol] = False
                    continue
                periods = self._periods(result, allow_today=allow_today)
                if not periods:
                    if not allow_today:
                        out[symbol] = False
                        continue
                    had_error = True
                    await asyncio.to_thread(
                        self.store.record_fund_flow_error,
                        symbol,
                        "DIRECT_FUND_FLOW_EMPTY",
                    )
                    out[symbol] = False
                    continue
                quality = self._quality(periods)
                if quality == "EMPTY":
                    if not allow_today:
                        out[symbol] = False
                        continue
                    had_error = True
                    await asyncio.to_thread(
                        self.store.record_fund_flow_error,
                        symbol,
                        "DIRECT_FUND_FLOW_EMPTY",
                    )
                    out[symbol] = False
                    continue
                to_record[symbol] = periods
                if quality != "VALID":
                    had_error = True
                    partial_symbols.add(symbol)

            if to_record:
                if self.session_notifier is not None and any(
                    sym not in partial_symbols for sym in to_record
                ):
                    try:
                        await asyncio.to_thread(
                            self.session_notifier.notify_recovered,
                            "main_fund_flow",
                            stage="盘中暗盘定时轮询",
                        )
                    except Exception:
                        pass
                batch_writer = getattr(self.store, "record_fund_flow_batch", None)
                if callable(batch_writer):
                    changed_map = await asyncio.to_thread(
                        batch_writer,
                        to_record,
                        trade_date=trade_date,
                        minute=minute,
                        captured_at=now,
                    )
                else:
                    changed_map = {}
                    for symbol, periods in to_record.items():
                        changed_map[symbol] = await asyncio.to_thread(
                            self.store.record_fund_flow,
                            symbol,
                            trade_date=trade_date,
                            minute=minute,
                            periods=periods,
                            captured_at=now,
                        )
                for symbol, changed in changed_map.items():
                    if symbol in partial_symbols:
                        await asyncio.to_thread(
                            self.store.record_fund_flow_error,
                            symbol,
                            "DIRECT_FUND_FLOW_PARTIAL",
                        )
                        out[symbol] = False
                    else:
                        out[symbol] = bool(changed)

            if post_close and not had_error:
                self._finalized_close_date = trade_date
                if callable(self.on_post_close_finalized):
                    try:
                        await asyncio.to_thread(self.on_post_close_finalized, symbols)
                    except Exception:
                        pass
            return {symbol: out.get(symbol, False) for symbol in symbols}

        semaphore = asyncio.Semaphore(self.max_concurrent)
        accepts_force = self._supports_force_kwarg(self.source.read_direct)
        session_error_codes: list[str] = []
        any_valid = False

        async def collect(symbol: str) -> tuple[str, bool]:
            nonlocal had_error, any_valid
            async with semaphore:
                try:
                    if post_close and accepts_force:
                        result = await asyncio.to_thread(
                            self.source.read_direct, symbol, force=True
                        )
                    else:
                        result = await asyncio.to_thread(self.source.read_direct, symbol)
                    if not isinstance(result, DirectReadOutcome) and not isinstance(result, dict):
                        raise RuntimeError("fund flow source returned an invalid result")
                    periods = self._periods(result, allow_today=allow_today)
                    if not periods:
                        if not allow_today:
                            return symbol, False
                        had_error = True
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
                        had_error = True
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
                        had_error = True
                        await asyncio.to_thread(
                            self.store.record_fund_flow_error,
                            symbol,
                            f"DIRECT_FUND_FLOW_{quality}",
                        )
                    else:
                        any_valid = True
                    return symbol, bool(changed) if quality == "VALID" else False
                except Exception as error:
                    had_error = True
                    code = fixed_market_error_code(
                        error,
                        "DIRECT_FUND_FLOW_REQUEST_FAILED",
                    )
                    if code in {"DIRECT_SESSION_EXPIRED", "DIRECT_SESSION_UNAVAILABLE"}:
                        session_error_codes.append(code)
                    await asyncio.to_thread(
                        self.store.record_fund_flow_error,
                        symbol,
                        code,
                    )
                    return symbol, False

        rows = await asyncio.gather(*(collect(symbol) for symbol in symbols))
        if self.session_notifier is not None:
            if session_error_codes:
                try:
                    await asyncio.to_thread(
                        self.session_notifier.notify_error,
                        "main_fund_flow",
                        session_error_codes[0],
                        stage="盘中暗盘定时轮询",
                    )
                except Exception:
                    pass
            elif any_valid:
                try:
                    await asyncio.to_thread(
                        self.session_notifier.notify_recovered,
                        "main_fund_flow",
                        stage="盘中暗盘定时轮询",
                    )
                except Exception:
                    pass
        if post_close and not had_error:
            self._finalized_close_date = trade_date
            if callable(self.on_post_close_finalized):
                try:
                    await asyncio.to_thread(self.on_post_close_finalized, symbols)
                except Exception:
                    pass
        return dict(rows)

    async def _cleanup_if_due(self, now: datetime) -> None:
        today = now.strftime("%Y%m%d")
        if self._last_cleanup_date == today:
            return
        cutoff = (now - timedelta(days=self.retention_days)).strftime("%Y%m%d")
        await asyncio.to_thread(self.store.cleanup_fund_flow_history, cutoff)
        self._last_cleanup_date = today
