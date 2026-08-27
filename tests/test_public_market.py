import json
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

    source = DirectEnrichedMarketDataSource(
        Base(),
        Direct(),
        ttl_seconds=15,
        clock=lambda: 100,
    )

    first = source.read_market_snapshot("601872", detail=True)
    second = source.read_market_snapshot("601872", detail=True)

    assert first.name == "公开名称"
    assert first.quote["price"] == "18.62"
    assert first.quote["large_order_net"] == "-0.16"
    assert first.intraday_series["large_order_net"]["points"][0]["value"] == "-0.16"
    assert first.intraday_series["macdfs"]["points"][0]["value"] == "+0.012"
    assert first.main_fund_flow["today"]["main_net_inflow"] == "1.23"
    assert first.capabilities["l2"]["available"] is True
    assert second.quote == first.quote
    assert direct_calls == ["601872"]


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
