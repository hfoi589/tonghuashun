import json
import threading
import time
from datetime import datetime, timezone

import pytest

from level2_service.market_data import MarketSnapshot
from level2_service.models import MetricKind
from level2_service.parsed_values import (
    DirectReadOutcome,
    DirectRequestError,
    SymbolLookup,
)


def _tencent_quote_fields(
    symbol: str,
    name: str,
    *,
    price: str,
    previous_close: str,
    open_price: str,
    high: str,
    low: str,
    volume_lots: str,
    amount_wan: str,
    turnover: str,
    change_percent: str,
) -> list[str]:
    fields = [""] * 88
    values = {
        0: "1",
        1: name,
        2: symbol,
        3: price,
        4: previous_close,
        5: open_price,
        6: volume_lots,
        30: "20260827150000",
        32: change_percent,
        33: high,
        34: low,
        37: amount_wan,
        38: turnover,
    }
    for index, value in values.items():
        fields[index] = value
    return fields


def _quote_wire(provider_id: str, fields: list[str]) -> bytes:
    return f'v_{provider_id}="{"~".join(fields)}";'.encode("gb18030")


def test_tencent_quote_normalizes_stock_and_fund_precision_and_units() -> None:
    from level2_service.public_market import TencentPublicMarketProvider

    payloads = {
        "sh601872": _quote_wire(
            "sh601872",
            _tencent_quote_fields(
                "601872",
                "招商轮船",
                price="18.62",
                previous_close="18.38",
                open_price="17.80",
                high="18.68",
                low="17.80",
                volume_lots="902383",
                amount_wan="165812.0265",
                turnover="1.12",
                change_percent="1.31",
            ),
        ),
        "sh510300": _quote_wire(
            "sh510300",
            _tencent_quote_fields(
                "510300",
                "沪深300ETF",
                price="4.123",
                previous_close="4.100",
                open_price="4.101",
                high="4.130",
                low="4.090",
                volume_lots="100",
                amount_wan="41.23",
                turnover="0.25",
                change_percent="0.56",
            ),
        ),
    }

    def fetch(url: str, _timeout: float) -> bytes:
        provider_id = url.split("q=", 1)[1]
        return payloads[provider_id]

    provider = TencentPublicMarketProvider(fetch=fetch)
    stock = provider.read_snapshot(
        SymbolLookup("601872", "招商轮船", "17"),
        detail=False,
    )
    fund = provider.read_snapshot(
        SymbolLookup("510300", "沪深300ETF", "20"),
        detail=False,
    )

    assert stock.source == "TENCENT_PUBLIC"
    assert stock.price_precision == 2
    assert stock.quote["volume"] == "90238300"
    assert stock.quote["amount"] == "1658120265"
    assert stock.quote["change_percent"] == "1.31%"
    assert fund.price_precision == 3
    assert fund.quote["price"] == "4.123"


def test_tencent_minute_filters_sessions_and_converts_cumulative_volume() -> None:
    from level2_service.public_market import TencentPublicMarketProvider

    fields = _tencent_quote_fields(
        "601872",
        "招商轮船",
        price="18.62",
        previous_close="18.38",
        open_price="17.80",
        high="18.68",
        low="17.80",
        volume_lots="200",
        amount_wan="36.80",
        turnover="1.12",
        change_percent="1.31",
    )
    payload = {
        "code": 0,
        "data": {
            "sh601872": {
                "qt": {"sh601872": fields},
                "data": {
                    "date": "20260827",
                    "data": [
                        "0929 17.70 50 88500.00",
                        "0930 17.80 100 178000.00",
                        "0931 18.00 150 268000.00",
                        "1131 18.10 170 304000.00",
                        "1300 18.20 200 358000.00",
                    ],
                },
            }
        },
    }
    provider = TencentPublicMarketProvider(
        fetch=lambda _url, _timeout: json.dumps(payload).encode(),
    )

    snapshot = provider.read_snapshot(
        SymbolLookup("601872", "招商轮船", "17"),
        detail=True,
    )

    assert [point.time for point in snapshot.timeshare] == [
        "09:30",
        "09:31",
        "13:00",
    ]
    assert [point.volume for point in snapshot.timeshare] == [
        "10000",
        "5000",
        "5000",
    ]
    assert snapshot.timeshare[1].average_price == "17.867"


def test_tencent_beijing_minute_accepts_rows_without_amount() -> None:
    from level2_service.public_market import TencentPublicMarketProvider

    fields = _tencent_quote_fields(
        "920002",
        "万达轴承",
        price="52.02",
        previous_close="51.37",
        open_price="51.80",
        high="52.10",
        low="50.50",
        volume_lots="34",
        amount_wan="1.80",
        turnover="1.65",
        change_percent="1.27",
    )
    payload = {
        "code": 0,
        "data": {
            "bj920002": {
                "qt": {"bj920002": fields},
                "data": {
                    "date": "20260827",
                    "data": ["0930 51.80 16", "0931 52.00 34"],
                },
            }
        },
    }
    provider = TencentPublicMarketProvider(
        fetch=lambda _url, _timeout: json.dumps(payload).encode(),
    )

    snapshot = provider.read_snapshot(
        SymbolLookup("920002", "万达轴承", "151"),
        detail=True,
    )

    assert [point.volume for point in snapshot.timeshare] == ["1600", "1800"]
    assert [point.average_price for point in snapshot.timeshare] == [None, None]


@pytest.mark.parametrize(
    ("period", "response_key"),
    [("day", "qfqday"), ("week", "qfqweek"), ("month", "qfqmonth")],
)
def test_tencent_qfq_series_parses_supported_periods(
    period: str,
    response_key: str,
) -> None:
    from level2_service.public_market import TencentPublicMarketProvider

    rows = [
        [f"2026-08-{day:02d}", "4.100", "4.120", "4.130", "4.090", "100"]
        for day in range(20, 27)
    ]
    payload = {
        "code": 0,
        "data": {
            "sh510300": {
                response_key: rows,
                "qt": {"sh510300": _tencent_quote_fields(
                    "510300", "沪深300ETF", price="4.120",
                    previous_close="4.100", open_price="4.100",
                    high="4.130", low="4.090", volume_lots="100",
                    amount_wan="41.20", turnover="0.25",
                    change_percent="0.49",
                )},
            }
        },
    }
    provider = TencentPublicMarketProvider(
        fetch=lambda _url, _timeout: json.dumps(payload).encode(),
    )

    page = provider.read_series(
        SymbolLookup("510300", "沪深300ETF", "20"),
        period,
        5,
    )

    assert page.source == "TENCENT_PUBLIC"
    assert len(page.bars) == 5
    assert page.bars[-1].close == "4.120"
    assert page.bars[-1].volume == "10000"


def test_tencent_five_day_uses_the_latest_five_daily_bars() -> None:
    from level2_service.public_market import TencentPublicMarketProvider

    rows = [
        [f"2026-08-{day:02d}", "18.00", "18.10", "18.20", "17.90", "10"]
        for day in range(20, 27)
    ]
    payload = {"code": 0, "data": {"sh601872": {"qfqday": rows}}}
    provider = TencentPublicMarketProvider(
        fetch=lambda _url, _timeout: json.dumps(payload).encode(),
    )

    page = provider.read_series(
        SymbolLookup("601872", "招商轮船", "17"),
        "five_day",
        240,
    )

    assert [bar.time for bar in page.bars] == [
        "2026-08-22",
        "2026-08-23",
        "2026-08-24",
        "2026-08-25",
        "2026-08-26",
    ]


def test_public_market_falls_back_to_sina_quote_without_timeshare() -> None:
    from level2_service.public_market import (
        PublicMarketDataSource,
        SinaPublicQuoteProvider,
        TencentPublicMarketProvider,
    )

    class Catalog:
        @staticmethod
        def lookup(symbol: str) -> SymbolLookup:
            return SymbolLookup(symbol, "招商轮船", "17")

    def tencent_fetch(_url: str, _timeout: float) -> bytes:
        raise TimeoutError("synthetic")

    sina_wire = (
        'var hq_str_sh601872="招商轮船,17.800,18.380,18.620,18.680,'
        '17.800,18.620,18.630,90238346,1658120265.000,0,0,0,0,0,0,'
        '0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-08-27,15:00:00,00";'
    ).encode("gb18030")
    source = PublicMarketDataSource(
        Catalog(),
        TencentPublicMarketProvider(fetch=tencent_fetch),
        SinaPublicQuoteProvider(fetch=lambda _url, _timeout: sina_wire),
    )

    snapshot = source.read_market_snapshot("601872", detail=True)

    assert snapshot.source == "SINA_PUBLIC"
    assert snapshot.quote["price"] == "18.62"
    assert snapshot.timeshare == ()
    assert snapshot.source_errors["tencent_public"] == "MARKET_QUOTE_UNAVAILABLE"


def test_detail_quote_uses_fresh_tencent_quote_when_minute_endpoint_fails() -> None:
    from level2_service.public_market import (
        PublicMarketDataSource,
        SinaPublicQuoteProvider,
        TencentPublicMarketProvider,
    )

    class Catalog:
        @staticmethod
        def lookup(symbol: str) -> SymbolLookup:
            return SymbolLookup(symbol, "招商轮船", "17")

    quote_fields = _tencent_quote_fields(
        "601872",
        "招商轮船",
        price="21.13",
        previous_close="20.78",
        open_price="20.98",
        high="21.35",
        low="20.78",
        volume_lots="630376",
        amount_wan="133289.9819",
        turnover="0.87",
        change_percent="1.68",
    )
    quote_fields[30] = "20260924110821"

    def tencent_fetch(url: str, _timeout: float) -> bytes:
        if "minute/query" in url:
            raise OSError("Tencent minute endpoint returned HTTP 501")
        return _quote_wire("sh601872", quote_fields)

    def sina_fetch(_url: str, _timeout: float) -> bytes:
        raise AssertionError("Sina must not replace an available fresh Tencent quote")

    source = PublicMarketDataSource(
        Catalog(),
        TencentPublicMarketProvider(fetch=tencent_fetch),
        SinaPublicQuoteProvider(fetch=sina_fetch),
    )

    snapshot = source.read_market_snapshot("601872", detail=True)

    assert snapshot.source == "TENCENT_PUBLIC"
    assert snapshot.quote["price"] == "21.13"
    assert snapshot.source_time == "11:08"
    assert snapshot.timeshare == ()
    assert snapshot.capabilities["timeshare"]["available"] is False
    assert snapshot.source_errors["tencent_public"] is None
    assert snapshot.source_errors["tencent_intraday"] == "MARKET_QUOTE_UNAVAILABLE"


def test_public_market_rejects_provider_identity_mismatch_with_fixed_error() -> None:
    from level2_service.public_market import PublicMarketError, TencentPublicMarketProvider

    fields = _tencent_quote_fields(
        "600000",
        "错误股票",
        price="10.00",
        previous_close="9.90",
        open_price="9.95",
        high="10.10",
        low="9.80",
        volume_lots="1",
        amount_wan="0.10",
        turnover="0.01",
        change_percent="1.01",
    )
    provider = TencentPublicMarketProvider(
        fetch=lambda _url, _timeout: _quote_wire("sh600000", fields),
    )

    with pytest.raises(PublicMarketError) as caught:
        provider.read_snapshot(
            SymbolLookup("601872", "招商轮船", "17"),
            detail=False,
        )

    assert caught.value.error_code == "PUBLIC_MARKET_RESPONSE_INVALID"
    assert "错误股票" not in str(caught.value)


def test_tencent_quote_accepts_provider_name_alias_when_symbol_matches() -> None:
    from level2_service.public_market import TencentPublicMarketProvider

    fields = _tencent_quote_fields(
        "688027",
        "科大国盾量子",
        price="360.80",
        previous_close="359.00",
        open_price="361.00",
        high="362.98",
        low="358.58",
        volume_lots="11249",
        amount_wan="40200.00",
        turnover="1.23",
        change_percent="0.50",
    )
    provider = TencentPublicMarketProvider(
        fetch=lambda _url, _timeout: _quote_wire("sh688027", fields),
    )

    snapshot = provider.read_snapshot(
        SymbolLookup("688027", "国盾量子", "17"),
        detail=False,
    )

    assert snapshot.name == "国盾量子"
    assert snapshot.quote["open"] == "361.00"
    assert snapshot.quote["high"] == "362.98"
    assert snapshot.quote["low"] == "358.58"
    assert snapshot.quote["previous_close"] == "359.00"
    assert snapshot.quote["turnover_rate"] == "1.23%"
    assert snapshot.quote["volume"] == "1124900"


def test_tencent_quote_rejects_impossible_price_range_even_when_name_differs() -> None:
    from level2_service.public_market import PublicMarketError, TencentPublicMarketProvider

    fields = _tencent_quote_fields(
        "601872",
        "完全错误名称",
        price="100.00",
        previous_close="9.90",
        open_price="9.95",
        high="10.10",
        low="9.80",
        volume_lots="1",
        amount_wan="0.10",
        turnover="0.01",
        change_percent="1.01",
    )
    provider = TencentPublicMarketProvider(
        fetch=lambda _url, _timeout: _quote_wire("sh601872", fields),
    )

    with pytest.raises(PublicMarketError) as caught:
        provider.read_snapshot(
            SymbolLookup("601872", "招商轮船", "17"),
            detail=False,
        )

    assert caught.value.error_code == "PUBLIC_MARKET_RESPONSE_INVALID"


def test_tencent_minute_rejects_non_monotonic_times() -> None:
    from level2_service.public_market import PublicMarketError, TencentPublicMarketProvider

    fields = _tencent_quote_fields(
        "601872", "招商轮船", price="18.62", previous_close="18.38",
        open_price="17.80", high="18.68", low="17.80", volume_lots="20",
        amount_wan="36.80", turnover="1.12", change_percent="1.31",
    )
    payload = {"data": {"sh601872": {
        "qt": {"sh601872": fields},
        "data": {"data": ["1000 18.00 10 18000", "0930 17.80 20 35800"]},
    }}}
    provider = TencentPublicMarketProvider(
        fetch=lambda _url, _timeout: json.dumps(payload).encode(),
    )

    with pytest.raises(PublicMarketError) as caught:
        provider.read_snapshot(
            SymbolLookup("601872", "招商轮船", "17"),
            detail=True,
        )

    assert caught.value.error_code == "PUBLIC_MARKET_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [["not-a-date", "4.10", "4.12", "4.13", "4.09", "100"]],
        [
            ["2026-08-20", "4.10", "4.12", "4.13", "4.09", "100"],
            ["2026-08-20", "4.10", "4.12", "4.13", "4.09", "100"],
        ],
    ],
)
def test_tencent_kline_rejects_empty_invalid_or_duplicate_dates(rows) -> None:
    from level2_service.public_market import PublicMarketError, TencentPublicMarketProvider

    payload = {"data": {"sh510300": {"qfqday": rows}}}
    provider = TencentPublicMarketProvider(
        fetch=lambda _url, _timeout: json.dumps(payload).encode(),
    )

    with pytest.raises(PublicMarketError) as caught:
        provider.read_series(
            SymbolLookup("510300", "沪深300ETF华泰柏瑞", "20"),
            "day",
            5,
        )

    assert caught.value.error_code == "PUBLIC_MARKET_RESPONSE_INVALID"


def test_direct_enrichment_merges_only_l2_owned_fields_and_is_cached() -> None:
    from level2_service.public_market import DirectEnrichedMarketDataSource

    public_snapshot = MarketSnapshot(
        symbol="601872",
        name="公开名称",
        market="17",
        sequence=0,
        source_time="15:00",
        collected_at=datetime.now(timezone.utc),
        quote={
            "price": "18.62",
            "change_percent": "1.31%",
            "large_order_net": None,
            "large_order_amount": None,
            "retail_count": None,
            "macdfs": None,
        },
        source="TENCENT_PUBLIC",
    )

    class Base:
        @staticmethod
        def read_market_snapshot(_symbol: str, *, detail: bool):
            assert detail is True
            return public_snapshot

    values = {kind: None for kind in MetricKind}
    values.update(
        {
            MetricKind.STOCK_NAME: "私有名称不得覆盖",
            MetricKind.CURRENT_PRICE: "99.99",
            MetricKind.LARGE_ORDER_NET: "-0.16",
            MetricKind.LARGE_ORDER_AMOUNT: "-22920.5",
            MetricKind.RETAIL_COUNT: "16.24",
            MetricKind.MACDFS: "+0.012",
            MetricKind.MAIN_FLOW_TODAY_UNIT: "亿元",
            MetricKind.MAIN_FLOW_TODAY_NET: "1.23",
        }
    )
    direct_calls: list[str] = []

    class Direct:
        @staticmethod
        def read_direct(symbol: str):
            direct_calls.append(symbol)
            return DirectReadOutcome(
                values=values,
                source_errors={"core_metrics": None, "main_fund_flow": None},
                intraday_series={
                    MetricKind.LARGE_ORDER_NET: {
                        "unit": None,
                        "points": [{"time": "15:00", "value": "-0.16"}],
                    },
                    MetricKind.MACDFS: {
                        "unit": None,
                        "points": [{"time": "15:00", "value": "+0.012"}],
                    },
                },
            )

    now = [100.0]
    source = DirectEnrichedMarketDataSource(
        Base(),
        Direct(),
        clock=lambda: now[0],
    )

    first = source.read_market_snapshot("601872", detail=True)
    second = source.read_market_snapshot("601872", detail=True)
    now[0] += 4.9
    third = source.read_market_snapshot("601872", detail=True)
    now[0] += 0.1
    fourth = source.read_market_snapshot("601872", detail=True)

    assert first.name == "公开名称"
    assert first.quote["price"] == "18.62"
    assert first.quote["large_order_net"] == "-0.16"
    assert first.intraday_series["large_order_net"]["points"][0]["value"] == "-0.16"
    assert first.intraday_series["macdfs"]["points"][0]["value"] == "+0.012"
    assert first.main_fund_flow["today"]["main_net_inflow"] == "1.23"
    assert first.capabilities["l2"]["available"] is True
    assert second.quote == first.quote
    assert third.quote == first.quote
    assert fourth.quote == first.quote
    assert direct_calls == ["601872", "601872"]


def test_public_market_uses_existing_identity_when_strict_catalog_is_stale() -> None:
    from level2_service.public_market import PublicMarketDataSource

    class Catalog:
        @staticmethod
        def lookup(_symbol: str):
            raise DirectRequestError("SYMBOL_CATALOG_STALE")

        @staticmethod
        def lookup_existing(symbol: str):
            return SymbolLookup(symbol=symbol, name="招商轮船", market="17")

    class Tencent:
        @staticmethod
        def read_snapshot(identity: SymbolLookup, *, detail: bool):
            assert detail is True
            return MarketSnapshot(
                symbol=identity.symbol,
                name=identity.name,
                market=identity.market,
                sequence=0,
                source_time="15:00",
                collected_at=datetime.now(timezone.utc),
                quote={"price": "20.99"},
                source="TENCENT_PUBLIC",
            )

    result = PublicMarketDataSource(Catalog(), Tencent(), object()).read_market_snapshot(
        "601872", detail=True
    )

    assert result.name == "招商轮船"
    assert result.quote["price"] == "20.99"


def test_public_market_uses_database_snapshot_only_for_watchlist_when_public_sources_fail() -> None:
    from level2_service.public_market import PublicMarketDataSource, PublicMarketError

    stored = MarketSnapshot(
        symbol="601872",
        name="招商轮船",
        market="17",
        sequence=0,
        source_time="20260909 15:00:00",
        collected_at=datetime(2026, 9, 9, 7, 0, tzinfo=timezone.utc),
        quote={"price": "20.99"},
        source="MARKET_DATABASE",
        stored_trade_dates={"core_metrics": "20260909"},
    )

    class Catalog:
        @staticmethod
        def lookup_existing(symbol: str):
            return SymbolLookup(symbol=symbol, name="招商轮船", market="17")

    class Failing:
        @staticmethod
        def read_snapshot(_identity: SymbolLookup, *, detail: bool):
            raise PublicMarketError("MARKET_QUOTE_UNAVAILABLE")

    class FailingSina:
        @staticmethod
        def read_snapshot(_identity: SymbolLookup):
            raise PublicMarketError("MARKET_QUOTE_UNAVAILABLE")

    source = PublicMarketDataSource(
        Catalog(),
        Failing(),
        FailingSina(),
        database_fallback=lambda _symbol: stored,
        is_watchlist_symbol=lambda _symbol: True,
    )

    result = source.read_market_snapshot("601872", detail=True)

    assert result.source == "MARKET_DATABASE"
    assert result.quote["price"] == "20.99"


def test_direct_enrichment_skips_l2_for_watchlist_snapshots() -> None:
    from level2_service.public_market import DirectEnrichedMarketDataSource

    snapshot = MarketSnapshot(
        symbol="601872",
        name="招商轮船",
        market="17",
        sequence=0,
        source_time="15:00",
        collected_at=datetime.now(timezone.utc),
        quote={"price": "18.62"},
        source="TENCENT_PUBLIC",
    )
    direct_calls: list[str] = []

    class Base:
        @staticmethod
        def read_market_snapshot(_symbol: str, *, detail: bool):
            assert detail is False
            return snapshot

    class Direct:
        @staticmethod
        def read_direct(symbol: str):
            direct_calls.append(symbol)
            raise AssertionError("watchlist snapshots must not read L2")

    result = DirectEnrichedMarketDataSource(Base(), Direct()).read_market_snapshot(
        "601872", detail=False
    )

    assert result.quote == {"price": "18.62"}
    assert direct_calls == []


def test_direct_enrichment_failure_keeps_the_public_snapshot_available() -> None:
    from level2_service.public_market import DirectEnrichedMarketDataSource

    snapshot = MarketSnapshot(
        symbol="601872",
        name="招商轮船",
        market="17",
        sequence=0,
        source_time="15:00",
        collected_at=datetime.now(timezone.utc),
        quote={"price": "18.62"},
        source="TENCENT_PUBLIC",
    )

    class Base:
        @staticmethod
        def read_market_snapshot(_symbol: str, *, detail: bool):
            return snapshot

    class Direct:
        @staticmethod
        def read_direct(_symbol: str):
            raise DirectRequestError("DIRECT_SESSION_UNAVAILABLE")

    result = DirectEnrichedMarketDataSource(
        Base(),
        Direct(),
    ).read_market_snapshot("601872", detail=True)

    assert result.quote["price"] == "18.62"
    assert result.capabilities["l2"]["available"] is False
    assert result.source_errors["core_metrics"] == "DIRECT_SESSION_UNAVAILABLE"


def test_closed_direct_enrichment_uses_stored_outcome_without_live_interface() -> None:
    from level2_service.public_market import DirectEnrichedMarketDataSource

    snapshot = MarketSnapshot(
        symbol="601872",
        name="招商轮船",
        market="17",
        sequence=0,
        source_time="15:00",
        collected_at=datetime.now(timezone.utc),
        quote={"price": "20.99"},
        source="TENCENT_PUBLIC",
    )
    values = {kind: None for kind in MetricKind}
    values.update(
        {
            MetricKind.LARGE_ORDER_NET: "0.33",
            MetricKind.LARGE_ORDER_AMOUNT: "54165.0万",
            MetricKind.RETAIL_COUNT: "-2.32",
            MetricKind.MACDFS: "0.000",
            MetricKind.MAIN_FLOW_TODAY_UNIT: "亿元",
            MetricKind.MAIN_FLOW_TODAY_NET: "10.67",
            MetricKind.MAIN_FLOW_TODAY_VISIBLE: "5.30",
            MetricKind.MAIN_FLOW_TODAY_HIDDEN: "5.37",
            MetricKind.MAIN_FLOW_TODAY_RETAIL: "-10.67",
        }
    )
    stored = DirectReadOutcome(
        values=values,
        source_errors={"core_metrics": None, "main_fund_flow": None},
    )

    class Base:
        @staticmethod
        def read_market_snapshot(_symbol: str, *, detail: bool):
            return snapshot

    class Direct:
        @staticmethod
        def read_direct(_symbol: str):
            raise AssertionError("closed snapshots must not call live enrichment")

    result = DirectEnrichedMarketDataSource(
        Base(),
        Direct(),
        is_market_open=lambda: False,
        closed_enrichment=lambda _symbol: stored,
    ).read_market_snapshot("601872", detail=True)

    assert result.quote["large_order_net"] == "0.33"
    assert result.main_fund_flow["today"]["main_net_inflow"] == "10.67"
    assert result.source_errors["core_metrics"] is None


def test_closed_direct_enrichment_hides_database_l2_for_non_watchlist_symbol() -> None:
    from level2_service.public_market import DirectEnrichedMarketDataSource

    snapshot = MarketSnapshot(
        symbol="601872",
        name="招商轮船",
        market="17",
        sequence=0,
        source_time="15:00",
        collected_at=datetime.now(timezone.utc),
        quote={"price": "20.99"},
        source="TENCENT_PUBLIC",
    )
    values = {kind: None for kind in MetricKind}
    values[MetricKind.LARGE_ORDER_NET] = "0.33"

    class Base:
        @staticmethod
        def read_market_snapshot(_symbol: str, *, detail: bool):
            return snapshot

    result = DirectEnrichedMarketDataSource(
        Base(),
        None,
        is_market_open=lambda: False,
        is_watchlist_symbol=lambda _symbol: False,
        closed_enrichment=lambda _symbol: DirectReadOutcome(
            values=values,
            source_errors={"core_metrics": None, "main_fund_flow": None},
        ),
    ).read_market_snapshot("601872", detail=True)

    assert result.quote.get("large_order_net") is None
    assert result.capabilities["l2"]["available"] is False


def test_closed_snapshot_never_falls_back_to_live_enrichment_when_history_is_missing() -> None:
    from level2_service.public_market import DirectEnrichedMarketDataSource

    snapshot = MarketSnapshot(
        symbol="601872",
        name="招商轮船",
        market="17",
        sequence=0,
        source_time="15:00",
        collected_at=datetime.now(timezone.utc),
        quote={"price": "20.99"},
        source="TENCENT_PUBLIC",
    )
    values = {kind: None for kind in MetricKind}
    values.update({
        MetricKind.LARGE_ORDER_NET: "0.33",
        MetricKind.LARGE_ORDER_AMOUNT: "54165.0万",
        MetricKind.RETAIL_COUNT: "-2.32",
        MetricKind.MACDFS: "0.000",
    })
    direct_calls: list[str] = []

    class Base:
        @staticmethod
        def read_market_snapshot(_symbol: str, *, detail: bool):
            return snapshot

    class Direct:
        @staticmethod
        def read_direct(symbol: str):
            direct_calls.append(symbol)
            return DirectReadOutcome(
                values=values,
                source_errors={"core_metrics": None, "main_fund_flow": None},
            )

    source = DirectEnrichedMarketDataSource(
        Base(),
        Direct(),
        is_market_open=lambda: False,
        closed_enrichment=lambda _symbol: None,
    )

    first = source.read_market_snapshot("601872", detail=True)
    second = source.read_market_snapshot("601872", detail=True)

    assert first.quote.get("large_order_net") is None
    assert second.quote.get("large_order_net") is None
    assert first.capabilities["l2"] == {
        "available": False,
        "reason": "MARKET_CLOSED_DATA_UNAVAILABLE",
    }
    assert direct_calls == []


def test_busy_direct_enrichment_does_not_block_public_snapshot() -> None:
    from level2_service.public_market import DirectEnrichedMarketDataSource

    snapshot = MarketSnapshot(
        symbol="601872",
        name="招商轮船",
        market="17",
        sequence=0,
        source_time="15:00",
        collected_at=datetime.now(timezone.utc),
        quote={"price": "18.62"},
        source="TENCENT_PUBLIC",
    )

    class Base:
        @staticmethod
        def read_market_snapshot(_symbol: str, *, detail: bool):
            assert detail is True
            return snapshot

    entered = threading.Event()
    release = threading.Event()

    class Direct:
        @staticmethod
        def read_direct(_symbol: str):
            entered.set()
            release.wait(timeout=2)
            raise DirectRequestError("DIRECT_PROTOCOL_RESPONSE_TIMEOUT")

    source = DirectEnrichedMarketDataSource(Base(), Direct())
    first = threading.Thread(
        target=lambda: source.read_market_snapshot("601872", detail=True),
        daemon=True,
    )
    first.start()
    assert entered.wait(timeout=1)

    started = time.monotonic()
    second = source.read_market_snapshot("601872", detail=True)
    elapsed = time.monotonic() - started
    release.set()
    first.join(timeout=2)

    assert elapsed < 0.2
    assert second.quote["price"] == "18.62"
    assert second.capabilities["l2"]["available"] is False
    assert second.source_errors["core_metrics"] == "DIRECT_ENRICHMENT_BUSY"


def test_empty_direct_enrichment_does_not_mark_l2_available() -> None:
    from level2_service.public_market import DirectEnrichedMarketDataSource

    snapshot = MarketSnapshot(
        symbol="601872",
        name="招商轮船",
        market="17",
        sequence=0,
        source_time="15:00",
        collected_at=datetime.now(timezone.utc),
        quote={"price": "18.62"},
        source="TENCENT_PUBLIC",
    )
    values = {kind: None for kind in MetricKind}

    class Base:
        @staticmethod
        def read_market_snapshot(_symbol: str, *, detail: bool):
            return snapshot

    class Direct:
        @staticmethod
        def read_direct(_symbol: str):
            return DirectReadOutcome(
                values=values,
                source_errors={"core_metrics": None, "main_fund_flow": None},
            )

    result = DirectEnrichedMarketDataSource(
        Base(),
        Direct(),
    ).read_market_snapshot("601872", detail=True)

    assert result.capabilities["l2"]["available"] is False
    assert result.capabilities["l2"]["reason"] == "DIRECT_ENRICHMENT_EMPTY"


def test_busy_direct_enrichment_error_returned_by_source_is_not_cached_for_ttl() -> None:
    from level2_service.public_market import DirectEnrichedMarketDataSource

    snapshot = MarketSnapshot(
        symbol="601872",
        name="招商轮船",
        market="17",
        sequence=0,
        source_time="15:00",
        collected_at=datetime.now(timezone.utc),
        quote={"price": "18.62"},
        source="TENCENT_PUBLIC",
    )

    class Base:
        @staticmethod
        def read_market_snapshot(_symbol: str, *, detail: bool):
            return snapshot

    calls = [0]

    class FlakyDirect:
        @staticmethod
        def read_direct(_symbol: str):
            calls[0] += 1
            if calls[0] == 1:
                raise DirectRequestError("DIRECT_ENRICHMENT_BUSY")
            values = {kind: None for kind in MetricKind}
            values[MetricKind.LARGE_ORDER_NET] = "0.50"
            return DirectReadOutcome(
                values=values,
                source_errors={"core_metrics": None, "main_fund_flow": None},
            )

    source = DirectEnrichedMarketDataSource(
        Base(),
        FlakyDirect(),
        ttl_seconds=60.0,
        clock=lambda: 100.0,
    )

    first = source.read_market_snapshot("601872", detail=True)
    second = source.read_market_snapshot("601872", detail=True)

    assert first.source_errors["core_metrics"] == "DIRECT_ENRICHMENT_BUSY"
    assert second.quote["large_order_net"] == "0.50"
    assert calls[0] == 2


def test_direct_enrichment_drops_empty_unit_shell_and_falls_back_to_stored_fund_flow() -> None:
    from level2_service.public_market import DirectEnrichedMarketDataSource

    snapshot = MarketSnapshot(
        symbol="301396",
        name="宏景科技",
        market="33",
        sequence=0,
        source_time="11:30",
        collected_at=datetime.now(timezone.utc),
        quote={"price": "106.33"},
        source="TENCENT_PUBLIC",
    )

    class Base:
        @staticmethod
        def read_market_snapshot(_symbol: str, *, detail: bool):
            return snapshot

    values = {kind: None for kind in MetricKind}
    values.update(
        {
            MetricKind.LARGE_ORDER_NET: "0.03",
            MetricKind.MAIN_FLOW_TODAY_UNIT: "万元",
            MetricKind.MAIN_FLOW_THREE_DAY_UNIT: "万元",
            MetricKind.MAIN_FLOW_FIVE_DAY_UNIT: "万元",
        }
    )

    class Direct:
        @staticmethod
        def read_direct(_symbol: str):
            return DirectReadOutcome(
                values=values,
                source_errors={"core_metrics": None, "main_fund_flow": "DIRECT_SESSION_EXPIRED"},
            )

    stored_periods = {
        "today": {
            "unit": "亿元",
            "main_net_inflow": "-1.75",
            "main_visible_inflow": "0.05",
            "main_hidden_inflow": "-1.80",
            "retail_inflow": "1.75",
        },
        "three_day": {
            "unit": "亿元",
            "main_net_inflow": "3.49",
            "main_visible_inflow": "2.96",
            "main_hidden_inflow": "0.52",
            "retail_inflow": "-3.49",
        },
        "five_day": {
            "unit": "亿元",
            "main_net_inflow": "13.25",
            "main_visible_inflow": "7.91",
            "main_hidden_inflow": "5.34",
            "retail_inflow": "-13.25",
        },
    }

    without_fallback = DirectEnrichedMarketDataSource(
        Base(),
        Direct(),
        is_market_open=lambda: True,
    ).read_market_snapshot("301396", detail=True)
    assert without_fallback.main_fund_flow == {}

    with_fallback = DirectEnrichedMarketDataSource(
        Base(),
        Direct(),
        is_market_open=lambda: True,
        is_watchlist_symbol=lambda _s: True,
        fund_flow_fallback=lambda _s: {"trade_date": "20261124", "periods": stored_periods},
    ).read_market_snapshot("301396", detail=True)

    assert with_fallback.main_fund_flow == stored_periods
    assert with_fallback.stored_trade_dates["main_fund_flow"] == "20261124"
    assert with_fallback.source_errors["main_fund_flow"] == "DIRECT_SESSION_EXPIRED"


def test_busy_direct_enrichment_reuses_last_good_and_today_core_fallback_and_skips_fund_flow_for_funds() -> None:
    from dataclasses import replace
    from level2_service.public_market import DirectEnrichedMarketDataSource

    snapshot = MarketSnapshot(
        symbol="601318",
        name="中国平安",
        market="17",
        sequence=0,
        source_time="13:57",
        collected_at=datetime.now(timezone.utc),
        quote={"price": "54.67", "turnover_rate": "0.60%"},
        source="TENCENT_PUBLIC",
    )

    class Base:
        @staticmethod
        def read_market_snapshot(symbol: str, *, detail: bool):
            return replace(snapshot, symbol=symbol, market="36" if symbol == "160723" else "17")

    l2_values = {kind: None for kind in MetricKind}
    l2_values.update(
        {
            MetricKind.LARGE_ORDER_NET: "0.05",
            MetricKind.LARGE_ORDER_AMOUNT: "31116.89",
            MetricKind.RETAIL_COUNT: "-13.60",
            MetricKind.MACDFS: "+0.001",
        }
    )
    l2_outcome = DirectReadOutcome(
        values=l2_values,
        source_errors={"core_metrics": None, "main_fund_flow": None},
    )

    now = [100.0]

    class DirectOnceThenBusy:
        calls = 0

        def read_direct(self, _symbol: str):
            self.calls += 1
            if self.calls == 1:
                return l2_outcome
            raise AssertionError("busy lock should short-circuit before read_direct")

    stale_source = DirectEnrichedMarketDataSource(
        Base(),
        DirectOnceThenBusy(),
        ttl_seconds=5.0,
        stale_ttl_seconds=120.0,
        clock=lambda: now[0],
        is_watchlist_symbol=lambda _s: True,
    )
    first = stale_source.read_market_snapshot("601318", detail=True)
    assert first.quote["large_order_net"] == "0.05"
    now[0] += 10.0  # past ttl_seconds=5.0, still within stale_ttl_seconds=120.0
    assert stale_source._enrichment_lock.acquire(blocking=False)
    try:
        while_busy = stale_source.read_market_snapshot("601318", detail=True)
    finally:
        stale_source._enrichment_lock.release()
    assert while_busy.quote["large_order_net"] == "0.05"
    assert while_busy.capabilities["l2"]["available"] is True
    assert while_busy.source_errors["core_metrics"] is None

    # Cold cache with busy lock falls back to today's stored core metrics transparently.
    cold_source = DirectEnrichedMarketDataSource(
        Base(),
        DirectOnceThenBusy(),
        is_watchlist_symbol=lambda _s: True,
        core_fallback=lambda _s: l2_outcome,
    )
    assert cold_source._enrichment_lock.acquire(blocking=False)
    try:
        cold_busy = cold_source.read_market_snapshot("601318", detail=True)
    finally:
        cold_source._enrichment_lock.release()
    assert cold_busy.quote["large_order_amount"] == "31116.89"
    assert cold_busy.capabilities["l2"]["available"] is True
    assert cold_busy.source_errors["core_metrics"] is None

    # Exchange funds never invoke fund_flow_fallback.
    fund_fallback_calls: list[str] = []
    fund_source = DirectEnrichedMarketDataSource(
        Base(),
        type("FundDirect", (), {"read_direct": staticmethod(lambda _s: l2_outcome)})(),
        is_watchlist_symbol=lambda _s: True,
        fund_flow_fallback=lambda sym: fund_fallback_calls.append(sym) or {},
    )
    fund_snap = fund_source.read_market_snapshot("160723", detail=True)
    assert fund_snap.main_fund_flow == {}
    assert fund_fallback_calls == []


def test_tencent_read_quotes_batch_and_exchange_fund_l2_short_circuit() -> None:
    from level2_service.public_market import (
        DirectEnrichedMarketDataSource,
        TencentPublicMarketProvider,
    )

    f1 = "~".join(_tencent_quote_fields(
        "601872", "招商轮船", price="10.20", previous_close="10.00",
        open_price="10.05", high="10.30", low="10.00", volume_lots="1200",
        amount_wan="1234.5", turnover="1.20", change_percent="2.00",
    ))
    f2 = "~".join(_tencent_quote_fields(
        "000001", "平安银行", price="11.50", previous_close="11.40",
        open_price="11.42", high="11.60", low="11.35", volume_lots="2400",
        amount_wan="2345.6", turnover="0.80", change_percent="0.88",
    ))
    f3 = "~".join(_tencent_quote_fields(
        "160723", "嘉实原油", price="1.23", previous_close="1.21",
        open_price="1.22", high="1.25", low="1.21", volume_lots="500",
        amount_wan="61.5", turnover="2.10", change_percent="1.65",
    ))
    seen_urls: list[str] = []

    def fake_request(url: str, _timeout: float) -> bytes:
        seen_urls.append(url)
        if "sh601872,sz000001" in url:
            return f'v_sh601872="{f1}";v_sz000001="{f2}";'.encode("gb18030")
        return f'v_sz160723="{f3}";'.encode("gb18030")

    provider = TencentPublicMarketProvider(fetch=fake_request)
    batch = provider.read_quotes_batch(
        [
            SymbolLookup(symbol="601872", name="招商轮船", market="17"),
            SymbolLookup(symbol="000001", name="平安银行", market="33"),
            SymbolLookup(symbol="160723", name="嘉实原油", market="36"),
        ],
        chunk_size=2,
    )
    assert len(seen_urls) == 2
    assert set(batch) == {"601872", "000001", "160723"}
    assert batch["601872"].quote["price"] == "10.20"
    assert batch["160723"].quote["open"] == "1.220"

    direct_calls: list[str] = []

    class Base:
        @staticmethod
        def read_market_snapshot(symbol: str, *, detail: bool):
            del detail
            return batch[symbol]

    enriched = DirectEnrichedMarketDataSource(
        Base(),
        type("NoFundDirect", (), {"read_direct": staticmethod(lambda sym: direct_calls.append(sym))})(),
        is_watchlist_symbol=lambda _s: True,
    )
    fund_snap = enriched.read_market_snapshot("160723", detail=True)
    assert fund_snap.capabilities["l2"] == {"available": False, "reason": "FUND_L2_UNSUPPORTED"}
    assert fund_snap.source_errors["core_metrics"] is None
    assert direct_calls == []
