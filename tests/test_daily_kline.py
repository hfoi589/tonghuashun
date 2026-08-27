import json
import threading
import time
from datetime import date, timedelta
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _jsonp(symbol: str, suffix: str, payload: dict) -> str:
    return (
        f"quotebridge_v6_line_hs_{symbol}_01_{suffix}("
        f"{json.dumps(payload, ensure_ascii=False)})"
    )


def _year_rows(year: int, count: int, start_price: float) -> str:
    start = date(year, 1, 1)
    rows = []
    for index in range(count):
        day = start + timedelta(days=index)
        price = start_price + index / 100
        rows.append(
            f"{day:%Y%m%d},{price:.2f},{price + .2:.2f},{price - .1:.2f},"
            f"{price + .1:.2f},{10000 + index},{20000 + index}.00,1.0,,,0"
        )
    return ";".join(rows)


def test_parse_10jqka_year_rows_validates_identity_and_ohlcv_fields() -> None:
    from level2_service.daily_kline import DailyKlineSourceError, parse_10jqka_year

    payload = _jsonp(
        "601872",
        "2026",
        {
            "data": (
                "20260820,18.70,19.00,18.55,18.66,10000,188000.00,1.2,,,0;"
                "20260821,18.49,20.10,18.49,19.78,193604931,3808373130.00,2.4,,,0"
            )
        },
    )

    bars = parse_10jqka_year(payload, "601872", 2026)

    assert [bar.time for bar in bars] == ["2026-08-20", "2026-08-21"]
    assert bars[-1].open == "18.49"
    assert bars[-1].high == "20.10"
    assert bars[-1].low == "18.49"
    assert bars[-1].close == "19.78"
    assert bars[-1].volume == "193604931"
    assert bars[-1].amount == "3808373130.00"

    with pytest.raises(DailyKlineSourceError, match="callback"):
        parse_10jqka_year(payload.replace("601872", "300750", 1), "601872", 2026)
    with pytest.raises(DailyKlineSourceError, match="field"):
        parse_10jqka_year(
            _jsonp("601872", "2026", {"data": "20260821,18.49,20.10"}),
            "601872",
            2026,
        )
    with pytest.raises(DailyKlineSourceError, match="impossible"):
        parse_10jqka_year(
            _jsonp(
                "601872",
                "2026",
                {"data": "20260821,18.49,18.00,18.49,19.78,10000,188000"},
            ),
            "601872",
            2026,
        )


@pytest.mark.parametrize("symbol", ["601872", "301396", "510300", "159915", "920002"])
def test_parse_10jqka_year_accepts_stock_fund_and_beijing_symbol_callbacks(
    symbol: str,
) -> None:
    from level2_service.daily_kline import parse_10jqka_year

    bars = parse_10jqka_year(
        _jsonp(
            symbol,
            "2026",
            {"data": "20260821,10.00,10.50,9.90,10.20,10000,102000"},
        ),
        symbol,
        2026,
    )

    assert bars[0].time == "2026-08-21"
    assert bars[0].close == "10.20"


def test_public_provider_fetches_exact_years_until_indicator_warmup_is_satisfied() -> None:
    from level2_service.daily_kline import TonghuashunPublicDailyKlineProvider

    responses = {
        "last.js": _jsonp(
            "601872",
            "last",
            {"name": "招商轮船", "year": {"2024": 200, "2025": 200, "2026": 200}},
        ),
        "2024.js": _jsonp("601872", "2024", {"data": _year_rows(2024, 200, 6)}),
        "2025.js": _jsonp("601872", "2025", {"data": _year_rows(2025, 200, 8)}),
        "2026.js": _jsonp("601872", "2026", {"data": _year_rows(2026, 200, 10)}),
    }
    calls: list[tuple[str, float]] = []

    def fetch(url: str, timeout: float) -> str:
        calls.append((url, timeout))
        return next(value for suffix, value in responses.items() if url.endswith(suffix))

    provider = TonghuashunPublicDailyKlineProvider(fetch=fetch)

    bars = provider.read("601872", minimum_bars=489)

    assert len(bars) == 600
    assert bars[0].time == "2024-01-01"
    assert bars[-1].time.startswith("2026-")
    assert [url.rsplit("/", 1)[-1] for url, _timeout in calls] == [
        "last.js",
        "2026.js",
        "2025.js",
        "2024.js",
    ]
    assert {timeout for _url, timeout in calls} == {8.0}


def test_public_provider_survives_two_transient_http_failures_by_default() -> None:
    from level2_service.daily_kline import TonghuashunPublicDailyKlineProvider

    attempts = 0

    def fetch(url: str, _timeout: float) -> str:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise OSError("temporary upstream reset")
        if url.endswith("last.js"):
            return _jsonp("601872", "last", {"year": {"2026": 1}})
        return _jsonp(
            "601872",
            "2026",
            {"data": "20260821,18.49,20.10,18.49,19.78,193604931,3808373130.00"},
        )

    bars = TonghuashunPublicDailyKlineProvider(fetch=fetch).read(
        "601872", minimum_bars=1
    )

    assert attempts == 4
    assert bars[0].time == "2026-08-21"


def test_daily_indicators_align_ma_boll_and_macd_with_every_bar() -> None:
    from level2_service.daily_kline import calculate_daily_indicators
    from level2_service.market_data import KlineBar

    bars = tuple(
        KlineBar(
            time=f"2026-01-{index + 1:02d}",
            open=str(index + 1),
            high=str(index + 1),
            low=str(index + 1),
            close=str(index + 1),
            volume="100",
            amount="1000",
        )
        for index in range(300)
    )

    indicators = calculate_daily_indicators(bars)

    assert set(indicators) == {
        "ma5",
        "ma13",
        "ma21",
        "ma60",
        "ma120",
        "ma250",
        "boll_mid",
        "boll_upper",
        "boll_lower",
        "macd_dif",
        "macd_dea",
        "macd_hist",
    }
    assert {len(values) for values in indicators.values()} == {300}
    assert indicators["ma5"][3] is None
    assert indicators["ma5"][4] == "3"
    assert indicators["ma13"][12] == "7"
    assert indicators["ma250"][248] is None
    assert indicators["ma250"][249] == "125.5"
    assert indicators["boll_mid"][18] is None
    assert indicators["boll_mid"][19] == "10.5"
    assert indicators["boll_upper"][19] == "22.032563"
    assert indicators["boll_lower"][19] == "-1.032563"
    assert indicators["macd_dif"][:2] == ("0", "0.079772")
    assert indicators["macd_dea"][:2] == ("0", "0.015954")
    assert indicators["macd_hist"][:2] == ("0", "0.127635")


def _bars(count: int) -> tuple:
    from level2_service.market_data import KlineBar

    return tuple(
        KlineBar(
            time=(date(2020, 1, 1) + timedelta(days=index)).isoformat(),
            open=str(index + 1),
            high=str(index + 2),
            low=str(index + 0.5),
            close=str(index + 1),
            volume=str(10000 + index),
            amount=str(20000 + index),
        )
        for index in range(count)
    )


class _AppSource:
    def __init__(self, page=None) -> None:
        self.page = page
        self.series_calls: list[tuple[str, str, str | None, int]] = []

    def read_market_snapshot(self, symbol: str, *, detail: bool):
        from level2_service.market_data import MarketSnapshot

        return MarketSnapshot(
            symbol=symbol,
            name="招商轮船",
            market="17",
            sequence=0,
            source_time=None,
            collected_at=datetime.now(timezone.utc),
            quote={},
            capabilities={"kline": {"available": False}},
        )

    def read_market_series(self, symbol, period, cursor, limit):
        from level2_service.market_data import MarketSeriesPage

        self.series_calls.append((symbol, period, cursor, limit))
        if isinstance(self.page, Exception):
            raise self.page
        return self.page or MarketSeriesPage(
            symbol=symbol,
            period=period,
            bars=(),
            source_error="DIRECT_KLINE_UNAVAILABLE",
        )


class _PublicProvider:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[tuple[str, int]] = []

    def read(self, symbol: str, *, minimum_bars: int):
        self.calls.append((symbol, minimum_bars))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_daily_source_prefers_public_and_returns_240_aligned_qfq_bars() -> None:
    from level2_service.daily_kline import DailyKlineMarketDataSource

    public = _PublicProvider(_bars(500))
    app = _AppSource()
    source = DailyKlineMarketDataSource(app, public, is_market_open=lambda: True)

    page = source.read_market_series("601872", "day", None, 240)
    cached = source.read_market_series("601872", "day", None, 240)

    assert len(page.bars) == 240
    assert {len(values) for values in page.indicators.values()} == {240}
    assert page.indicators["ma250"][0] is not None
    assert page.adjustment == "qfq"
    assert page.source == "THS_PUBLIC"
    assert page.cached is False
    assert page.stale is False
    assert page.source_errors == {
        "ths_public_kline": None,
        "tencent_public_kline": None,
    }
    assert cached.cached is True
    assert public.calls == [("601872", 489)]
    assert app.series_calls == []


def test_daily_source_falls_back_to_tencent_and_recomputes_all_indicators() -> None:
    from level2_service.daily_kline import (
        DailyKlineMarketDataSource,
        DailyKlineSourceError,
    )
    from level2_service.market_data import MarketSeriesPage

    app = _AppSource(
        MarketSeriesPage(
            symbol="601872",
            period="day",
            bars=_bars(500),
            source="TENCENT_PUBLIC",
        )
    )
    public = _PublicProvider(DailyKlineSourceError("PUBLIC_KLINE_HTTP_ERROR"))
    source = DailyKlineMarketDataSource(app, public)

    page = source.read_market_series("601872", "day", None, 240)

    assert page.source == "TENCENT_PUBLIC"
    assert page.adjustment == "qfq"
    assert page.source_errors == {
        "ths_public_kline": "PUBLIC_KLINE_HTTP_ERROR",
        "tencent_public_kline": None,
    }
    assert app.series_calls == [("601872", "day", None, 489)]
    assert len(page.bars) == len(page.indicators["macd_hist"]) == 240


def test_daily_source_serves_only_stale_cache_after_both_sources_fail() -> None:
    from level2_service.daily_kline import (
        DailyKlineMarketDataSource,
        DailyKlineSourceError,
    )

    now = [100.0]
    public = _PublicProvider(_bars(500))
    app = _AppSource()
    source = DailyKlineMarketDataSource(
        app,
        public,
        clock=lambda: now[0],
        is_market_open=lambda: True,
    )
    source.read_market_series("601872", "day", None, 240)
    now[0] += 61
    public.result = DailyKlineSourceError("PUBLIC_KLINE_HTTP_ERROR")

    page = source.read_market_series("601872", "day", None, 240)

    assert page.cached is True
    assert page.stale is True
    assert page.source == "THS_PUBLIC"
    assert page.source_error == "KLINE_SOURCES_UNAVAILABLE"
    assert page.source_errors == {
        "ths_public_kline": "PUBLIC_KLINE_HTTP_ERROR",
        "tencent_public_kline": "DIRECT_KLINE_UNAVAILABLE",
    }
    assert source.daily_kline_stats() == {
        "cache_entries": 1,
        "public_successes": 1,
        "fallback_successes": 0,
        "stale_cache_hits": 1,
        "failures": 0,
    }


def test_daily_source_returns_explicit_empty_page_without_any_cache() -> None:
    from level2_service.daily_kline import (
        DailyKlineMarketDataSource,
        DailyKlineSourceError,
    )

    source = DailyKlineMarketDataSource(
        _AppSource(),
        _PublicProvider(DailyKlineSourceError("PUBLIC_KLINE_HTTP_ERROR")),
    )

    page = source.read_market_series("601872", "day", None, 240)

    assert page.bars == ()
    assert page.source is None
    assert page.adjustment == "qfq"
    assert page.cached is False
    assert page.stale is False
    assert page.source_error == "KLINE_SOURCES_UNAVAILABLE"
    assert source.daily_kline_stats()["failures"] == 1


def test_daily_source_advertises_only_daily_kline_capability() -> None:
    from level2_service.daily_kline import DailyKlineMarketDataSource

    source = DailyKlineMarketDataSource(_AppSource(), _PublicProvider(_bars(500)))

    snapshot = source.read_market_snapshot("601872", detail=True)

    assert snapshot.capabilities["daily_kline"] == {
        "available": True,
        "adjustment": "qfq",
    }
    assert snapshot.capabilities["kline"] == {"available": False}

    from level2_service.market_data import MarketDataBroker

    assert MarketDataBroker(source).stats()["daily_kline"] == {
        "cache_entries": 0,
        "public_successes": 0,
        "fallback_successes": 0,
        "stale_cache_hits": 0,
        "failures": 0,
    }


def test_daily_source_coalesces_concurrent_requests_for_the_same_symbol() -> None:
    from level2_service.daily_kline import DailyKlineMarketDataSource

    class SlowProvider(_PublicProvider):
        def read(self, symbol: str, *, minimum_bars: int):
            self.calls.append((symbol, minimum_bars))
            time.sleep(0.05)
            return self.result

    provider = SlowProvider(_bars(500))
    source = DailyKlineMarketDataSource(
        _AppSource(),
        provider,
        is_market_open=lambda: True,
    )
    pages = []

    threads = [
        threading.Thread(
            target=lambda: pages.append(
                source.read_market_series("601872", "day", None, 240)
            )
        )
        for _index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert provider.calls == [("601872", 489)]
    assert sorted(page.cached for page in pages) == [False, True]


def test_core_app_probe_fixture_records_the_confirmed_qfq_request_and_data_ids() -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "app_daily_kline_qfq.json").read_text()
    )

    assert fixture["probe_scope"] == "core_metrics"
    assert fixture["device_role"] == "emulator-5556"
    assert fixture["request"] == {
        "stockcode": "301396",
        "marketid": "33",
        "period": "5",
        "quan": "10",
        "klinecount": "378",
        "primary_data_id": 7101,
    }
    assert fixture["response_data_ids"] == {
        "time": 1,
        "open": 7,
        "high": 8,
        "low": 9,
        "close": 11,
        "volume": 13,
        "amount": 19,
    }
    assert fixture["bars"][-1] == {
        "time": "20260821",
        "open": "185.07",
        "high": "190.24",
        "low": "180.42",
        "close": "186.06",
        "volume": "12525535",
        "amount": "2325301300",
    }
