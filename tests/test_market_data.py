import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import time

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
    assert is_china_market_open(datetime(2026, 8, 24, 1, 10, tzinfo=timezone.utc)) is True
    assert is_china_market_open(datetime(2026, 8, 24, 1, 9, tzinfo=timezone.utc)) is False
    assert is_china_market_open(datetime(2026, 8, 24, 1, 31, tzinfo=timezone.utc)) is True
    assert is_china_market_open(datetime(2026, 8, 24, 4, 0, tzinfo=timezone.utc)) is False
    assert is_china_market_open(datetime(2026, 8, 23, 1, 31, tzinfo=timezone.utc)) is False


def test_watchlist_only_symbols_refresh_every_two_seconds_during_quote_session() -> None:
    source = FakeMarketSource()
    now = [100.0]
    broker = MarketDataBroker(source, clock=lambda: now[0], is_market_open=lambda: True)
    broker.subscribe("watchlist", watchlist_symbols={"601872"}, detail_symbols=set())

    asyncio.run(broker.poll_due())
    now[0] += 1.9
    asyncio.run(broker.poll_due())
    assert source.snapshot_calls == [("601872", False)]

    now[0] += 0.1
    asyncio.run(broker.poll_due())
    assert source.snapshot_calls == [("601872", False), ("601872", False)]


def test_closed_subscriptions_read_once_and_detail_switch_only_reads_clicked_symbol() -> None:
    source = FakeMarketSource()
    now = [100.0]
    broker = MarketDataBroker(source, clock=lambda: now[0], is_market_open=lambda: False)

    initial_detail = broker.subscribe(
        "client",
        watchlist_symbols={"601872", "300750"},
        detail_symbols={"601872"},
    )
    assert initial_detail == {"601872"}
    asyncio.run(broker.refresh("601872", detail=True))
    asyncio.run(broker.poll_due())
    assert source.snapshot_calls == [("601872", True), ("300750", False)]

    now[0] += 3600
    asyncio.run(broker.poll_due())
    assert source.snapshot_calls == [("601872", True), ("300750", False)]

    changed_detail = broker.subscribe(
        "client",
        watchlist_symbols={"601872", "300750"},
        detail_symbols={"300750"},
    )
    assert changed_detail == {"300750"}
    asyncio.run(broker.refresh("300750", detail=True))
    asyncio.run(broker.poll_due())
    assert source.snapshot_calls == [("601872", True), ("300750", False), ("300750", True)]


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


def test_detail_refresh_does_not_reuse_a_low_detail_snapshot() -> None:
    class DetailAwareSource(FakeMarketSource):
        def read_market_snapshot(self, symbol: str, *, detail: bool) -> MarketSnapshot:
            return replace(
                super().read_market_snapshot(symbol, detail=detail),
                timeshare=(
                    (TimesharePoint(time="09:30", price="19.42"),)
                    if detail
                    else ()
                ),
            )

    source = DetailAwareSource()
    broker = MarketDataBroker(source, clock=lambda: 100.0, is_market_open=lambda: True)
    broker.subscribe("client", watchlist_symbols={"601872"}, detail_symbols=set())
    asyncio.run(broker.refresh("601872", detail=False))
    detail = asyncio.run(broker.refresh("601872", detail=True, max_age_seconds=10))

    assert detail.timeshare
    assert source.snapshot_calls == [("601872", False), ("601872", True)]


def test_poll_due_refreshes_symbols_with_bounded_concurrency() -> None:
    class SlowSource(FakeMarketSource):
        def read_market_snapshot(self, symbol: str, *, detail: bool) -> MarketSnapshot:
            time.sleep(0.05)
            return super().read_market_snapshot(symbol, detail=detail)

    source = SlowSource()
    broker = MarketDataBroker(
        source,
        max_concurrent_refreshes=2,
        clock=lambda: 100.0,
        is_market_open=lambda: True,
    )
    broker.subscribe(
        "client",
        watchlist_symbols={"600000", "600001", "600002", "600003"},
        detail_symbols=set(),
    )
    started = time.monotonic()
    asyncio.run(broker.poll_due())

    assert time.monotonic() - started < 0.18
    assert len(source.snapshot_calls) == 4


def test_unsubscribe_evicts_unreferenced_snapshot_state() -> None:
    source = FakeMarketSource()
    broker = MarketDataBroker(source, is_market_open=lambda: True)
    broker.subscribe("client", watchlist_symbols={"601872"}, detail_symbols=set())
    asyncio.run(broker.refresh("601872", detail=False))

    broker.unsubscribe("client")

    assert broker.cached_snapshot("601872") is None


def test_poll_due_backs_off_after_repeated_source_failures() -> None:
    class FailingSource(FakeMarketSource):
        def read_market_snapshot(self, symbol: str, *, detail: bool) -> MarketSnapshot:
            self.snapshot_calls.append((symbol, detail))
            raise RuntimeError("MARKET_QUOTE_UNAVAILABLE")

    source = FailingSource()
    now = [100.0]
    broker = MarketDataBroker(source, clock=lambda: now[0], is_market_open=lambda: True)
    broker.subscribe("client", watchlist_symbols={"601872"}, detail_symbols=set())

    asyncio.run(broker.poll_due())
    now[0] += 1.0
    asyncio.run(broker.poll_due())
    assert len(source.snapshot_calls) == 1
    now[0] += 1.0
    asyncio.run(broker.poll_due())
    assert len(source.snapshot_calls) == 2
    now[0] += 2.0
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


def test_broker_keeps_the_latest_event_for_each_subscribed_symbol() -> None:
    source = FakeMarketSource()
    broker = MarketDataBroker(source, is_market_open=lambda: True)
    broker.subscribe(
        "multi",
        watchlist_symbols={"601872", "300750"},
        detail_symbols=set(),
    )

    asyncio.run(broker.refresh("601872", detail=False))
    asyncio.run(broker.refresh("300750", detail=False))
    events = [
        asyncio.run(broker.next_event("multi", timeout=0.1)),
        asyncio.run(broker.next_event("multi", timeout=0.1)),
    ]

    assert {event["data"]["symbol"] for event in events} == {
        "601872",
        "300750",
    }


def test_broker_skips_removed_pending_tokens_after_resubscription() -> None:
    source = FakeMarketSource()
    broker = MarketDataBroker(source)
    broker.subscribe(
        "client",
        watchlist_symbols={"601872", "300750"},
        detail_symbols=set(),
    )
    asyncio.run(broker.refresh("601872", detail=False))
    broker.subscribe(
        "client",
        watchlist_symbols={"300750"},
        detail_symbols=set(),
    )
    asyncio.run(broker.refresh("300750", detail=False))

    event = asyncio.run(broker.next_event("client", timeout=0.1))

    assert event["data"]["symbol"] == "300750"


def test_broker_keeps_snapshot_and_source_status_for_the_same_symbol() -> None:
    broker = MarketDataBroker(FakeMarketSource())
    broker.subscribe(
        "client",
        watchlist_symbols={"601872"},
        detail_symbols=set(),
    )
    broker._publish("601872", {
        "type": "snapshot",
        "data": {"symbol": "601872", "sequence": 1},
    })
    broker._publish("601872", {
        "type": "source_status",
        "symbol": "601872",
        "status": "OFFLINE",
        "error_code": "MARKET_QUOTE_UNAVAILABLE",
    })

    events = [
        asyncio.run(broker.next_event("client", timeout=0.1)),
        asyncio.run(broker.next_event("client", timeout=0.1)),
    ]

    assert {event["type"] for event in events} == {
        "snapshot",
        "source_status",
    }


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
        "error_code": "MARKET_SOURCE_FAILED",
    }


def test_broker_redacts_nonfixed_source_error_details() -> None:
    class FailingSource(FakeMarketSource):
        def read_market_snapshot(self, symbol: str, *, detail: bool) -> MarketSnapshot:
            raise RuntimeError("PRIVATE_SECRET_TOKEN")

    broker = MarketDataBroker(FailingSource())
    broker.subscribe("client", watchlist_symbols={"601872"}, detail_symbols=set())

    asyncio.run(broker.poll_due())
    event = asyncio.run(broker.next_event("client", timeout=0.1))

    assert event["error_code"] == "MARKET_SOURCE_FAILED"
    assert "private" not in str(event)
