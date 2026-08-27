"""Typed App-internal market snapshots and a shared subscription broker."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo


MARKET_PERIODS = frozenset({"timeshare", "five_day", "min5", "min15", "min30", "min60", "day", "week", "month"})


def is_china_market_open(now: datetime | None = None) -> bool:
    """Return whether A-share continuous trading is scheduled at this instant."""
    current = now or datetime.now(timezone.utc)
    local = current.astimezone(ZoneInfo("Asia/Shanghai"))
    if local.weekday() >= 5:
        return False
    minute = local.hour * 60 + local.minute
    return 9 * 60 + 30 <= minute <= 11 * 60 + 30 or 13 * 60 <= minute <= 15 * 60


@dataclass(frozen=True)
class TimesharePoint:
    time: str
    price: str | None
    average_price: str | None = None
    volume: str | None = None


@dataclass(frozen=True)
class KlineBar:
    time: str
    open: str | None
    high: str | None
    low: str | None
    close: str | None
    volume: str | None
    amount: str | None = None


@dataclass(frozen=True)
class OrderBookLevel:
    side: str
    level: int
    price: str | None
    volume: str | None


@dataclass(frozen=True)
class TradeTick:
    time: str
    price: str | None
    volume: str | None
    side: str | None


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    name: str | None
    market: str
    sequence: int
    source_time: str | None
    collected_at: datetime
    quote: dict[str, str | None]
    source: str | None = None
    price_precision: int = 2
    timeshare: tuple[TimesharePoint, ...] = ()
    intraday_series: dict[str, dict[str, Any]] = field(default_factory=dict)
    order_book: tuple[OrderBookLevel, ...] = ()
    trades: tuple[TradeTick, ...] = ()
    main_fund_flow: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)
    source_errors: dict[str, str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 1 <= self.price_precision <= 6:
            raise ValueError("price_precision must be between 1 and 6")

    def as_public(self, *, stale_after_seconds: float = 5.0) -> dict[str, Any]:
        value = asdict(self)
        value["collected_at"] = self.collected_at.isoformat()
        age = max(0.0, (datetime.now(timezone.utc) - self.collected_at).total_seconds())
        value["stale"] = age > stale_after_seconds
        value["age_seconds"] = round(age, 3)
        return value


@dataclass(frozen=True)
class MarketSeriesPage:
    symbol: str
    period: str
    bars: tuple[KlineBar, ...]
    indicators: dict[str, tuple[str | None, ...]] = field(default_factory=dict)
    next_cursor: str | None = None
    source_error: str | None = None
    adjustment: str | None = None
    source: str | None = None
    cached: bool = False
    stale: bool = False
    source_errors: dict[str, str | None] = field(
        default_factory=lambda: {"public_kline": None, "app_kline": None}
    )

    def as_public(self) -> dict[str, Any]:
        return asdict(self)


class MarketDataSource(Protocol):
    def read_market_snapshot(self, symbol: str, *, detail: bool) -> MarketSnapshot: ...

    def read_market_series(
        self,
        symbol: str,
        period: str,
        cursor: str | None,
        limit: int,
    ) -> MarketSeriesPage: ...


class MarketDataBroker:
    """Coalesce source reads and fan the latest snapshot out to subscribers."""

    def __init__(
        self,
        source: MarketDataSource,
        *,
        detail_interval_seconds: float = 2.0,
        watchlist_interval_seconds: float = 15.0,
        closed_interval_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        is_market_open: Callable[[], bool] = lambda: True,
    ) -> None:
        self.source = source
        self.detail_interval_seconds = detail_interval_seconds
        self.watchlist_interval_seconds = watchlist_interval_seconds
        self.closed_interval_seconds = closed_interval_seconds
        self.clock = clock
        self.is_market_open = is_market_open
        self._subscriptions: dict[str, tuple[set[str], set[str]]] = {}
        self._queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._cache: dict[str, MarketSnapshot] = {}
        self._sequence: dict[str, int] = {}
        self._last_polled: dict[str, float] = {}
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    def subscribe(
        self,
        client_id: str,
        *,
        watchlist_symbols: set[str],
        detail_symbols: set[str],
    ) -> None:
        self._subscriptions[client_id] = (set(watchlist_symbols), set(detail_symbols))
        self._queues.setdefault(client_id, asyncio.Queue(maxsize=1))

    def unsubscribe(self, client_id: str) -> None:
        self._subscriptions.pop(client_id, None)
        self._queues.pop(client_id, None)

    def has_subscriber(self, client_id: str) -> bool:
        return client_id in self._subscriptions

    def stats(self) -> dict[str, Any]:
        subscribed_symbols: set[str] = set()
        for watchlist, detail in self._subscriptions.values():
            subscribed_symbols.update(watchlist)
            subscribed_symbols.update(detail)
        stats: dict[str, Any] = {
            "market_open": bool(self.is_market_open()),
            "subscribers": len(self._subscriptions),
            "subscribed_symbols": len(subscribed_symbols),
            "cached_symbols": len(self._cache),
            "detail_interval_seconds": float(self.detail_interval_seconds),
            "watchlist_interval_seconds": float(self.watchlist_interval_seconds),
            "closed_interval_seconds": float(self.closed_interval_seconds),
        }
        daily_stats = getattr(self.source, "daily_kline_stats", None)
        if callable(daily_stats):
            stats["daily_kline"] = daily_stats()
        return stats

    def cached_snapshot(self, symbol: str) -> MarketSnapshot | None:
        return self._cache.get(symbol)

    def seed(self, snapshot: MarketSnapshot) -> None:
        self._cache[snapshot.symbol] = snapshot
        self._sequence[snapshot.symbol] = max(
            snapshot.sequence,
            self._sequence.get(snapshot.symbol, 0),
        )

    def _wanted(self, client_id: str, symbol: str) -> bool:
        watchlist, detail = self._subscriptions.get(client_id, (set(), set()))
        return symbol in watchlist or symbol in detail

    def _publish(self, symbol: str, event: dict[str, Any]) -> None:
        for client_id, queue in tuple(self._queues.items()):
            if not self._wanted(client_id, symbol):
                continue
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    async def next_event(self, client_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        queue = self._queues.get(client_id)
        if queue is None:
            raise LookupError("market subscriber not found")
        if timeout is None:
            return await queue.get()
        return await asyncio.wait_for(queue.get(), timeout=timeout)

    async def refresh(
        self,
        symbol: str,
        *,
        detail: bool,
        max_age_seconds: float = 0,
    ) -> MarketSnapshot:
        lock = self._refresh_locks.setdefault(symbol, asyncio.Lock())
        async with lock:
            if max_age_seconds > 0:
                cached = self._cache.get(symbol)
                last = self._last_polled.get(symbol)
                if cached is not None and last is not None and self.clock() - last < max_age_seconds:
                    return cached
            snapshot = await asyncio.to_thread(
                self.source.read_market_snapshot,
                symbol,
                detail=detail,
            )
            sequence = self._sequence.get(symbol, 0) + 1
            current = replace(snapshot, sequence=sequence)
            self._sequence[symbol] = sequence
            self._cache[symbol] = current
            self._last_polled[symbol] = self.clock()
            self._publish(symbol, {"type": "snapshot", "data": current.as_public()})
            return current

    async def poll_due(self) -> None:
        now = self.clock()
        watchlist_symbols: set[str] = set()
        detail_symbols: set[str] = set()
        for watchlist, detail in self._subscriptions.values():
            watchlist_symbols.update(watchlist)
            detail_symbols.update(detail)
        for symbol in sorted(watchlist_symbols | detail_symbols, key=lambda value: (value not in detail_symbols, value)):
            detail = symbol in detail_symbols
            interval = (
                self.detail_interval_seconds if detail else self.watchlist_interval_seconds
            ) if self.is_market_open() else self.closed_interval_seconds
            last = self._last_polled.get(symbol)
            if last is not None and now - last < interval:
                continue
            try:
                await self.refresh(symbol, detail=detail)
            except Exception as error:
                self._last_polled[symbol] = now
                error_code = getattr(error, "error_code", None) or str(error) or type(error).__name__
                self._publish(
                    symbol,
                    {
                        "type": "source_status",
                        "symbol": symbol,
                        "status": "OFFLINE",
                        "error_code": error_code,
                    },
                )

    async def series(
        self,
        symbol: str,
        period: str,
        cursor: str | None,
        limit: int,
    ) -> MarketSeriesPage:
        if period not in MARKET_PERIODS:
            raise ValueError("unsupported market series period")
        if not 1 <= limit <= 500:
            raise ValueError("series limit must be between 1 and 500")
        return await asyncio.to_thread(
            self.source.read_market_series,
            symbol,
            period,
            cursor,
            limit,
        )
