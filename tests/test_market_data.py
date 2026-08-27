import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from level2_service.market_data import (
    KlineBar,
    MarketDataBroker,
    MarketSeriesPage,
    MarketSnapshot,
    TimesharePoint,
    is_china_market_open,
)


class FakeMarketSource:
    def __init__(self) -> None:
        self.snapshot_calls: list[tuple[str, bool]] = []
        self.series_calls: list[tuple[str, str, str | None, int]] = []

    def read_market_snapshot(self, symbol: str, *, detail: bool) -> MarketSnapshot:
        self.snapshot_calls.append((symbol, detail))
        return MarketSnapshot(
            symbol=symbol,
            name="招商轮船",
            market="17",
            sequence=0,
            source_time="2026-08-24T09:31:02+08:00",
            collected_at=datetime(2026, 8, 24, 1, 31, 3, tzinfo=timezone.utc),
            quote={"current_price": "19.78", "change_percent": "+2.22%"},
            timeshare=(TimesharePoint(time="09:30", price="19.42", average_price="19.42", volume="10200"),),
        )
    def read_market_series(
        self,
        symbol: str,
        period: str,
        cursor: str | None,
        limit: int,
    ) -> MarketSeriesPage:
        self.series_calls.append((symbol, period, cursor, limit))
        return MarketSeriesPage(
            symbol=symbol,
            period=period,
            bars=(KlineBar(time="2026-08-22", open="19.10", high="19.90", low="19.00", close="19.78", volume="10000", amount="200000"),),
            indicators={"ma5": ("19.22",)},
            next_cursor="older-page",
        )


def test_china_market_schedule_uses_asia_shanghai_sessions() -> None:
    assert is_china_market_open(datetime(2026, 8, 24, 1, 31, tzinfo=timezone.utc)) is True
    assert is_china_market_open(datetime(2026, 8, 24, 4, 0, tzinfo=timezone.utc)) is False
    assert is_china_market_open(datetime(2026, 8, 23, 1, 31, tzinfo=timezone.utc)) is False


def test_market_snapshot_exposes_public_source_and_price_precision() -> None:
    snapshot = MarketSnapshot(
        symbol="510300",
        name="沪深300ETF",
        market="20",
        sequence=0,
        source_time="15:00",
        collected_at=datetime.now(timezone.utc),
        quote={"price": "4.123"},
        source="TENCENT_PUBLIC",
        price_precision=3,
    )

    public = snapshot.as_public()

    assert public["source"] == "TENCENT_PUBLIC"
    assert public["price_precision"] == 3
    with pytest.raises(ValueError, match="price_precision"):
        replace(snapshot, price_precision=0)


def test_broker_coalesces_multiple_subscribers_and_prioritizes_detail_refresh() -> None:
    source = FakeMarketSource()
    now = [100.0]
    broker = MarketDataBroker(source, clock=lambda: now[0], is_market_open=lambda: True)
    broker.subscribe("first", watchlist_symbols={"601872"}, detail_symbols=set())
    broker.subscribe("second", watchlist_symbols={"601872"}, detail_symbols={"601872"})

    asyncio.run(broker.poll_due())

    assert source.snapshot_calls == [("601872", True)]
    assert broker.cached_snapshot("601872").sequence == 1
    now[0] += 1.9
    asyncio.run(broker.poll_due())
    assert len(source.snapshot_calls) == 1
    now[0] += 0.1
    asyncio.run(broker.poll_due())
    assert len(source.snapshot_calls) == 2


def test_broker_keeps_only_the_latest_event_for_a_slow_subscriber() -> None:
    source = FakeMarketSource()
    broker = MarketDataBroker(source, is_market_open=lambda: True)
    broker.subscribe("slow", watchlist_symbols={"601872"}, detail_symbols={"601872"})

    first = asyncio.run(broker.refresh("601872", detail=True))
    second = asyncio.run(broker.refresh("601872", detail=True))
    event = asyncio.run(broker.next_event("slow", timeout=0.1))

    assert first.sequence == 1
    assert second.sequence == 2
    assert event["type"] == "snapshot"
    assert event["data"]["sequence"] == 2


def test_broker_pages_series_through_the_app_source_and_validates_periods() -> None:
    source = FakeMarketSource()
    broker = MarketDataBroker(source)

    page = asyncio.run(broker.series("601872", "day", "cursor", 120))

    assert page.next_cursor == "older-page"
    assert source.series_calls == [("601872", "day", "cursor", 120)]


def test_broker_retains_the_last_snapshot_and_publishes_source_errors() -> None:
    class FailingSource(FakeMarketSource):
        def read_market_snapshot(self, symbol: str, *, detail: bool) -> MarketSnapshot:
            raise RuntimeError("DIRECT_APP_OFFLINE")

    broker = MarketDataBroker(FailingSource())
    cached = MarketSnapshot(
        symbol="601872",
        name="招商轮船",
        market="17",
        sequence=9,
        source_time=None,
        collected_at=datetime.now(timezone.utc),
        quote={"current_price": "19.78"},
    )
    broker.seed(replace(cached, sequence=1))
    broker.subscribe("client", watchlist_symbols=set(), detail_symbols={"601872"})

    asyncio.run(broker.poll_due())
    event = asyncio.run(broker.next_event("client", timeout=0.1))

    assert broker.cached_snapshot("601872").quote["current_price"] == "19.78"
    assert event == {
        "type": "source_status",
        "symbol": "601872",
        "status": "OFFLINE",
        "error_code": "DIRECT_APP_OFFLINE",
    }
