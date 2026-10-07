from datetime import datetime, timezone
import json

import pytest

from level2_service.market_data import MarketPhase, MarketPhaseState, MarketSnapshot
from level2_service.parsed_values import DirectRequestError
from level2_service.models import MetricKind
from level2_service.parsed_values import DirectReadOutcome
from level2_service.preopen_market import (
    CoreDirectPreopenQuoteClient,
    Preopen9528Client,
    PreopenMarketDataSource,
    RedisPreopenQuoteCache,
)


def _snapshot(symbol: str, *, source: str, price: str) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        name="测试股票",
        market="17",
        sequence=0,
        source_time="09:16:00",
        collected_at=datetime(2026, 8, 24, 1, 16, tzinfo=timezone.utc),
        quote={"price": price, "change_percent": "+1.23%"},
        source=source,
    )


class FakeBaseSource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def read_market_snapshot(self, symbol: str, *, detail: bool) -> MarketSnapshot:
        self.calls.append((symbol, detail))
        return _snapshot(symbol, source="TENCENT_PUBLIC", price="10.00")


class FakePreopenClient:
    def __init__(self, phase: MarketPhase, *, fail: bool = False) -> None:
        self.phase = phase
        self.fail = fail
        self.calls: list[tuple[str, bool, MarketPhase]] = []

    def read_phase(self) -> MarketPhaseState:
        return MarketPhaseState(
            phase=self.phase,
            source="APP_STATUS",
            local_time="09:16:00",
            server_time="09:16:00",
        )

    def read_quote(self, symbol: str, *, detail: bool, phase: MarketPhase) -> MarketSnapshot:
        self.calls.append((symbol, detail, phase))
        if self.fail:
            raise DirectRequestError("AUCTION_QUOTE_UNAVAILABLE")
        return _snapshot(symbol, source="THS_AUCTION", price="10.20")

    def read_watchlist_quotes(self, symbols: set[str], *, phase: MarketPhase) -> dict[str, MarketSnapshot]:
        return {
            symbol: _snapshot(symbol, source="THS_DIRECT_QUOTE", price="10.10")
            for symbol in symbols
        }


class FakeSessionProvider:
    def __init__(self, session: object | None) -> None:
        self.session = session
        self.errors: list[tuple[str, str]] = []

    def get(self, role: str) -> object | None:
        assert role == "core_metrics"
        return self.session

    def mark_error(self, role: str, code: str) -> None:
        self.errors.append((role, code))


class FakeProtocol:
    def __init__(self) -> None:
        self.phase_calls = 0
        self.quote_calls: list[tuple[str, str, bool, MarketPhase]] = []

    def read_phase(self, _session: object) -> MarketPhaseState:
        self.phase_calls += 1
        return MarketPhaseState(MarketPhase.CALL_AUCTION, "APP_STATUS", "09:16:00", "09:16:00")

    def read_quote(self, _session: object, symbol: str, market: str, *, detail: bool, phase: MarketPhase) -> MarketSnapshot:
        self.quote_calls.append((symbol, market, detail, phase))
        return _snapshot(symbol, source="THS_AUCTION", price="10.20")


def test_preopen_source_uses_internal_quote_and_skips_public_source() -> None:
    base = FakeBaseSource()
    client = FakePreopenClient(MarketPhase.CALL_AUCTION)
    source = PreopenMarketDataSource(base, client, clock=lambda: datetime(2026, 8, 24, 1, 16, tzinfo=timezone.utc))

    snapshot = source.read_market_snapshot("600000", detail=True)

    assert snapshot.source == "THS_AUCTION"
    assert snapshot.market_phase is MarketPhase.CALL_AUCTION
    assert snapshot.quote["price"] == "10.20"
    assert client.calls == [("600000", True, MarketPhase.CALL_AUCTION)]
    assert base.calls == []


def test_preopen_source_returns_public_quote_after_continuous_session_starts() -> None:
    base = FakeBaseSource()
    client = FakePreopenClient(MarketPhase.CONTINUOUS)
    source = PreopenMarketDataSource(base, client, clock=lambda: datetime(2026, 8, 24, 1, 31, tzinfo=timezone.utc))

    snapshot = source.read_market_snapshot("600000", detail=True)

    assert snapshot.source == "TENCENT_PUBLIC"
    assert snapshot.market_phase is MarketPhase.CONTINUOUS
    assert client.calls == []
    assert base.calls == [("600000", True)]


def test_public_quote_does_not_overwrite_same_day_auction_cache() -> None:
    base = FakeBaseSource()
    client = FakePreopenClient(MarketPhase.CALL_AUCTION)
    source = PreopenMarketDataSource(
        base,
        client,
        clock=lambda: datetime(2026, 8, 24, 1, 16, tzinfo=timezone.utc),
    )
    auction = source.read_market_snapshot("600000", detail=False)
    source.clock = lambda: datetime(2026, 8, 24, 1, 31, tzinfo=timezone.utc)
    client.phase = MarketPhase.CONTINUOUS
    public = source.read_market_snapshot("600000", detail=False)

    assert auction.source == "THS_AUCTION"
    assert public.source == "TENCENT_PUBLIC"
    source.clock = lambda: datetime(2026, 8, 24, 1, 16, tzinfo=timezone.utc)
    assert source._cached("600000").source == "THS_AUCTION"


def test_preopen_source_keeps_same_day_value_when_internal_quote_fails() -> None:
    base = FakeBaseSource()
    client = FakePreopenClient(MarketPhase.CALL_AUCTION, fail=True)
    source = PreopenMarketDataSource(base, client, clock=lambda: datetime(2026, 8, 24, 1, 16, tzinfo=timezone.utc))
    source.seed(_snapshot("600000", source="THS_AUCTION", price="10.18"))

    snapshot = source.read_market_snapshot("600000", detail=False)

    assert snapshot.quote["price"] == "10.18"
    assert snapshot.market_phase is MarketPhase.CALL_AUCTION
    assert snapshot.source_errors["auction_quote"] == "AUCTION_QUOTE_UNAVAILABLE"
    assert base.calls == []


def test_preopen_source_does_not_fallback_to_public_quote_without_cache() -> None:
    base = FakeBaseSource()
    client = FakePreopenClient(MarketPhase.CALL_AUCTION, fail=True)
    source = PreopenMarketDataSource(base, client, clock=lambda: datetime(2026, 8, 24, 1, 16, tzinfo=timezone.utc))

    with pytest.raises(DirectRequestError, match="AUCTION_QUOTE_UNAVAILABLE"):
        source.read_market_snapshot("600000", detail=False)

    assert base.calls == []


def test_direct_preopen_mode_does_not_use_public_source_without_client() -> None:
    base = FakeBaseSource()
    source = PreopenMarketDataSource(
        base,
        None,
        enforce_preopen=True,
        clock=lambda: datetime(2026, 8, 24, 1, 16, tzinfo=timezone.utc),
    )

    with pytest.raises(DirectRequestError, match="DIRECT_SESSION_UNAVAILABLE"):
        source.read_market_snapshot("600000", detail=False)

    assert base.calls == []


def test_preopen_client_uses_core_session_and_verified_market_code() -> None:
    protocol = FakeProtocol()
    client = Preopen9528Client(FakeSessionProvider(object()), protocol)

    phase = client.read_phase()
    quote = client.read_quote("600000", detail=True, phase=MarketPhase.CALL_AUCTION)

    assert phase is not None and phase.phase is MarketPhase.CALL_AUCTION
    assert quote.source == "THS_AUCTION"
    assert protocol.quote_calls == [("600000", "17", True, MarketPhase.CALL_AUCTION)]


def test_preopen_client_reports_missing_session_without_calling_protocol() -> None:
    protocol = FakeProtocol()
    client = Preopen9528Client(FakeSessionProvider(None), protocol)

    with pytest.raises(DirectRequestError, match="DIRECT_SESSION_UNAVAILABLE"):
        client.read_phase()

    assert protocol.phase_calls == 0


def test_core_direct_preopen_client_maps_verified_quote_values() -> None:
    class DirectSource:
        def read_direct(self, symbol: str) -> DirectReadOutcome:
            assert symbol == "600000"
            return DirectReadOutcome(
                values={
                    MetricKind.STOCK_NAME: "浦发银行",
                    MetricKind.CURRENT_PRICE: "10.20",
                    MetricKind.CHANGE_PERCENT: "+1.23%",
                },
                source_errors={"core_metrics": None, "main_fund_flow": None},
            )

    client = CoreDirectPreopenQuoteClient(DirectSource())
    snapshot = client.read_quote("600000", detail=True, phase=MarketPhase.PREOPEN_QUOTE)

    assert snapshot.source == "THS_DIRECT_QUOTE"
    assert snapshot.quote["price"] == "10.20"
    assert snapshot.quote["change_percent"] == "+1.23%"
    assert snapshot.market_phase is MarketPhase.PREOPEN_QUOTE


def test_redis_preopen_cache_restores_same_day_quote_without_public_fallback() -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.values: dict[str, bytes] = {}

        def setex(self, key: str, _ttl: int, value: str) -> None:
            self.values[key] = value.encode()

        def get(self, key: str) -> bytes | None:
            return self.values.get(key)

    redis = FakeRedis()
    cache = RedisPreopenQuoteCache(redis)
    first = PreopenMarketDataSource(
        FakeBaseSource(),
        FakePreopenClient(MarketPhase.CALL_AUCTION),
        cache=cache,
        clock=lambda: datetime(2026, 8, 24, 1, 16, tzinfo=timezone.utc),
    )
    saved = first.read_market_snapshot("600000", detail=False)
    assert json.loads(next(iter(redis.values.values())).decode())["quote"]["price"] == "10.20"

    second = PreopenMarketDataSource(
        FakeBaseSource(),
        FakePreopenClient(MarketPhase.CALL_AUCTION, fail=True),
        cache=RedisPreopenQuoteCache(redis),
        clock=lambda: datetime(2026, 8, 24, 1, 16, tzinfo=timezone.utc),
    )
    restored = second.read_market_snapshot("600000", detail=False)

    assert restored.quote["price"] == saved.quote["price"]
    assert restored.source_errors["auction_quote"] == "AUCTION_QUOTE_UNAVAILABLE"


def test_preopen_source_uses_batch_watchlist_reader_when_available() -> None:
    base = FakeBaseSource()
    client = FakePreopenClient(MarketPhase.PREOPEN_QUOTE)
    source = PreopenMarketDataSource(
        base,
        client,
        clock=lambda: datetime(2026, 8, 24, 1, 12, tzinfo=timezone.utc),
    )

    snapshots = source.read_market_snapshots({"600000", "000001"}, detail_symbols=set())

    assert set(snapshots) == {"600000", "000001"}
    assert all(item.source == "THS_DIRECT_QUOTE" for item in snapshots.values())
    assert base.calls == []
