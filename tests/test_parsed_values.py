from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import traceback
import types
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

from level2_service.models import MetricKind
import pytest

from level2_service.parsed_values import (
    DirectReadOutcome,
    DirectRequestError,
    DualAccountParsedValueSource,
    FridaParsedValueSource,
    SymbolLookup,
    SymbolLookupAmbiguousError,
    SymbolLookupNotFoundError,
    UnsupportedMarketError,
    market_code_for_symbol,
    _FRIDA_CORE_DIRECT_SCRIPT,
    _FRIDA_DIRECT_SCRIPT,
    _FRIDA_FUND_DIRECT_SCRIPT,
    _format_intraday_time,
)


def _runtime_payload(macdfs: float = 0.011903988574406313) -> dict:
    return {
        "quotes": [
            {
                "symbol": "601975",
                "name": "招商南油",
                "price": 3.21,
                "previous_close": 3.20,
                "change_percent": 0.31,
                "turnover_rate": 1.10,
            },
            {
                "symbol": "601872",
                "name": "招商轮船",
                "price": 19.78,
                "previous_close": 18.46,
                "change_percent": None,
                "turnover_rate": 2.3977,
            },
        ],
        "indicators": [
            {"symbol": "601975", "techid": 7051, "values": [0.002536]},
            {"symbol": "601872", "techid": 7031, "values": [-0.020211, -0.016403]},
            {"symbol": "601872", "techid": 7032, "values": [-33970070, -28025640]},
            {"symbol": "601872", "techid": 7034, "values": [21.753745905766515, 21.22634653875312]},
            {"symbol": "601872", "techid": 7051, "values": [0.01270280923973885, macdfs]},
        ],
    }


def test_intraday_time_decodes_the_app_packed_hour_and_minute_bits() -> None:
    """Exposing packed values such as 132688478 breaks every chart time label."""
    assert _format_intraday_time(132688478) == "09:30"
    assert _format_intraday_time(132688606) == "11:30"
    assert _format_intraday_time(132688705) == "13:01"
    assert _format_intraday_time(132688832) == "15:00"


def test_intraday_time_does_not_misread_dates_timestamps_or_lunch_as_packed_times() -> None:
    packed_lunch = (126 << 20) | (8 << 16) | (21 << 11) | (12 << 6)
    assert _format_intraday_time(20200101) == "20200101"
    assert _format_intraday_time(1724486400) == "1724486400"
    assert _format_intraday_time(packed_lunch) == str(packed_lunch)
    assert _format_intraday_time(-2147483648) == "-2147483648"


def test_frida_source_selects_the_requested_stock_and_formats_all_runtime_values() -> None:
    """Selecting the first cached object would mix 招商南油 into the 601872 result."""
    source = FridaParsedValueSource(
        "127.0.0.1:27042",
        runtime_reader=lambda *_args: _runtime_payload(),
    )

    values = source.read("601872")

    assert {kind: values[kind] for kind in (
        MetricKind.STOCK_NAME,
        MetricKind.CURRENT_PRICE,
        MetricKind.CHANGE_PERCENT,
        MetricKind.TURNOVER_RATE,
        MetricKind.RETAIL_COUNT,
        MetricKind.LARGE_ORDER_NET,
        MetricKind.LARGE_ORDER_AMOUNT,
        MetricKind.MACDFS,
    )} == {
        MetricKind.STOCK_NAME: "招商轮船",
        MetricKind.CURRENT_PRICE: "19.78",
        MetricKind.CHANGE_PERCENT: "7.15%",
        MetricKind.TURNOVER_RATE: "2.40%",
        MetricKind.RETAIL_COUNT: "21.23",
        MetricKind.LARGE_ORDER_NET: "-0.02",
        MetricKind.LARGE_ORDER_AMOUNT: "-2802.6万",
        MetricKind.MACDFS: "+0.012",
    }


def test_frida_source_rejects_runtime_objects_for_a_different_stock() -> None:
    """A techid match alone is insufficient when another stock remains cached."""
    source = FridaParsedValueSource(
        "127.0.0.1:27042",
        runtime_reader=lambda *_args: {
            "quotes": _runtime_payload()["quotes"][:1],
            "indicators": _runtime_payload()["indicators"][:1],
        },
    )

    assert all(value is None for value in source.read("601872").values())


def test_frida_source_reads_the_latest_macdfs_on_every_task() -> None:
    """Caching a previous result would keep showing +0.003 or an earlier +0.013 point."""
    payloads = iter((_runtime_payload(0.002536), _runtime_payload(0.011903988574406313)))
    calls = 0

    def runtime_reader(*_args):
        nonlocal calls
        calls += 1
        return next(payloads)

    source = FridaParsedValueSource("127.0.0.1:27042", runtime_reader=runtime_reader)

    assert source.read("601872")[MetricKind.MACDFS] == "+0.003"
    assert source.read("601872")[MetricKind.MACDFS] == "+0.012"
    assert calls == 2


def test_frida_source_degrades_to_missing_values_when_runtime_reading_fails() -> None:
    """A missing Frida bridge must not abort long capture or the OCR fallback."""
    def unavailable(*_args):
        raise TimeoutError("Frida server unavailable")

    source = FridaParsedValueSource("127.0.0.1:27042", runtime_reader=unavailable)

    assert all(value is None for value in source.read("601872").values())


@pytest.mark.parametrize(
    ("symbol", "expected_market"),
    [
        ("600000", "17"),
        ("601872", "17"),
        ("603000", "17"),
        ("605000", "17"),
        ("688001", "17"),
        ("689009", "17"),
        ("000001", "33"),
        ("001234", "33"),
        ("002594", "33"),
        ("003816", "33"),
        ("300750", "33"),
        ("301269", "33"),
        ("920799", "151"),
    ],
)
def test_market_code_maps_confirmed_stock_prefixes(
    symbol: str, expected_market: str
) -> None:
    """A wrong market code would make the App sign a valid request for the wrong exchange."""
    assert market_code_for_symbol(symbol) == expected_market


@pytest.mark.parametrize(
    ("symbol", "expected_market"),
    [
        ("501001", "20"),
        ("502000", "20"),
        ("506000", "20"),
        ("508000", "20"),
        ("510010", "20"),
        ("511010", "20"),
        ("512000", "20"),
        ("513000", "20"),
        ("515000", "20"),
        ("516000", "20"),
        ("517000", "20"),
        ("518600", "20"),
        ("519007", "20"),
        ("520500", "20"),
        ("526000", "20"),
        ("530000", "20"),
        ("551000", "20"),
        ("560010", "20"),
        ("561000", "20"),
        ("562000", "20"),
        ("563000", "20"),
        ("588000", "20"),
        ("589000", "20"),
        ("158003", "36"),
        ("159001", "36"),
        ("160105", "36"),
        ("161005", "36"),
        ("162006", "36"),
        ("163001", "36"),
        ("164105", "36"),
        ("165309", "36"),
        ("166001", "36"),
        ("167001", "36"),
        ("168101", "36"),
        ("169101", "36"),
        ("180101", "36"),
    ],
)
def test_market_code_maps_every_current_exchange_fund_prefix(
    symbol: str, expected_market: str
) -> None:
    """Dropping any listed fund prefix would reject a valid App-signed request."""
    assert market_code_for_symbol(symbol) == expected_market


@pytest.mark.parametrize("symbol", ["430001", "830001", "900901", "AAPL", "60000", "6000000"])
def test_market_code_rejects_unconfirmed_or_malformed_prefixes(symbol: str) -> None:
    """Unknown prefixes must never be guessed into a signed App request."""
    with pytest.raises(UnsupportedMarketError):
        market_code_for_symbol(symbol)


def test_direct_read_passes_the_derived_market_to_the_app_and_formats_fresh_values() -> None:
    calls: list[tuple[str, str, float, str, str]] = []

    def direct_reader(endpoint, package, timeout, symbol, market):
        calls.append((endpoint, package, timeout, symbol, market))
        return _runtime_payload()

    source = FridaParsedValueSource(
        "127.0.0.1:27042",
        direct_reader=direct_reader,
    )

    values = source.read_direct("601872")

    assert calls == [
        ("127.0.0.1:27042", "com.hexin.plat.android", 8, "601872", "17")
    ]
    assert values[MetricKind.STOCK_NAME] == "招商轮船"
    assert values[MetricKind.RETAIL_COUNT] == "21.23"
    assert values[MetricKind.LARGE_ORDER_AMOUNT] == "-2802.6万"


def test_direct_read_preserves_the_three_app_intraday_series_with_their_time_axis() -> None:
    """Taking only values[-1] would make the public charts lose the App curve."""
    payload = _runtime_payload()
    payload["indicators"] = [
        {
            "symbol": "601872",
            "techid": 7031,
            "times": [930, 931],
            "values": [-0.020211, -0.016403],
        },
        {
            "symbol": "601872",
            "techid": 7032,
            "times": ["09:30", "09:31"],
            "values": [-33970070, -28025640],
        },
        {
            "symbol": "601872",
            "techid": 7034,
            "times": ["0930", "0931"],
            "values": [21.753745905766515, 21.22634653875312],
        },
    ]
    source = FridaParsedValueSource(
        "127.0.0.1:27042",
        request_scope="core_metrics",
        direct_reader=lambda *_args: payload,
    )

    outcome = source.read_direct("601872")

    assert outcome[MetricKind.LARGE_ORDER_NET] == "-0.02"
    assert outcome.intraday_series == {
        MetricKind.LARGE_ORDER_NET: {
            "unit": None,
            "points": [
                {"time": "09:30", "value": "-0.02"},
                {"time": "09:31", "value": "-0.02"},
            ],
        },
        MetricKind.LARGE_ORDER_AMOUNT: {
            "unit": "万",
            "points": [
                {"time": "09:30", "value": "-3397.0"},
                {"time": "09:31", "value": "-2802.6"},
            ],
        },
        MetricKind.RETAIL_COUNT: {
            "unit": None,
            "points": [
                {"time": "09:30", "value": "21.75"},
                {"time": "09:31", "value": "21.23"},
            ],
        },
    }


def test_direct_read_preserves_the_macdfs_intraday_series_with_signed_three_decimal_values() -> None:
    """Dropping techid 7051 would leave the Market MACDFS chart empty."""
    payload = _runtime_payload()
    payload["indicators"] = [
        {
            "symbol": "601872",
            "techid": 7051,
            "times": [930, 931, 932],
            "values": [-0.00249, 0.011903988574406313, -2147483648],
        },
    ]

    outcome = FridaParsedValueSource(
        "127.0.0.1:27042",
        request_scope="core_metrics",
        direct_reader=lambda *_args: payload,
    ).read_direct("601872")

    assert outcome.intraday_series[MetricKind.MACDFS] == {
        "unit": None,
        "points": [
            {"time": "09:30", "value": "-0.002"},
            {"time": "09:31", "value": "+0.012"},
            {"time": "09:32", "value": None},
        ],
    }


def test_market_snapshot_preserves_app_macd_dif_dea_and_histogram_from_techid_7051() -> None:
    """Reading only data id 36883 drops the two MACD lines shown by the App."""
    payload = _runtime_payload()
    payload["indicators"] = [
        {
            "symbol": "601872",
            "techid": 7051,
            "times": [930, 931],
            "values": ["0.010", "0.012"],
            "data_series": {
                "36881": ["-0.001", "0.0024"],
                "36882": ["-0.006", "-0.0045"],
                "36883": ["0.010", "0.012"],
            },
        },
    ]

    snapshot = FridaParsedValueSource(
        "127.0.0.1:27042",
        request_scope="core_metrics",
        direct_reader=lambda *_args: payload,
    ).read_market_snapshot("601872", detail=True)

    assert snapshot.intraday_series["macd_dif"]["points"] == [
        {"time": "09:30", "value": "-0.001"},
        {"time": "09:31", "value": "+0.002"},
    ]
    assert snapshot.intraday_series["macd_dea"]["points"] == [
        {"time": "09:30", "value": "-0.006"},
        {"time": "09:31", "value": "-0.005"},
    ]
    assert snapshot.intraday_series["macdfs"]["points"][-1] == {
        "time": "09:31",
        "value": "+0.012",
    }


def test_market_snapshot_uses_the_direct_app_quote_and_timeshare_arrays() -> None:
    payload = _runtime_payload()
    payload["quotes"][1].update(
        {
            "price": 8.33,
            "times": [930, 931, 932],
            "prices": [8.31, 8.34, 8.33],
            "volumes": [1200, 800, 500],
            "amounts": [9972, 6672, 4165],
        }
    )
    payload["indicators"].append(
        {
            "symbol": "601872",
            "techid": 7051,
            "times": [930, 931, 932],
            "values": [0.009, 0.0104, 0.011903988574406313],
        }
    )
    source = FridaParsedValueSource(
        "core:27043",
        request_scope="core_metrics",
        direct_reader=lambda *_args: payload,
    )

    snapshot = source.read_market_snapshot("601872", detail=True)

    assert snapshot.symbol == "601872"
    assert snapshot.market == "17"
    assert snapshot.name == "招商轮船"
    assert snapshot.quote["price"] == "8.33"
    assert snapshot.quote["volume"] == "2500"
    assert snapshot.source_time == "09:32"
    assert [point.time for point in snapshot.timeshare] == ["09:30", "09:31", "09:32"]
    assert [point.price for point in snapshot.timeshare] == ["8.31", "8.34", "8.33"]
    assert snapshot.intraday_series["macdfs"]["points"][-1] == {
        "time": "09:32",
        "value": "+0.012",
    }
    assert snapshot.capabilities["timeshare"]["available"] is True
    assert snapshot.capabilities["order_book"] == {
        "available": False,
        "reason": "APP_INTERFACE_NOT_CONFIRMED",
    }


def test_market_series_parses_only_an_injected_app_internal_kline_response() -> None:
    calls: list[tuple[object, ...]] = []

    def series_reader(endpoint, package, timeout, symbol, market, period, cursor, limit):
        calls.append((endpoint, package, timeout, symbol, market, period, cursor, limit))
        return {
            "bars": [
                {
                    "time": "2026-08-21",
                    "open": 8.20,
                    "high": 8.48,
                    "low": 8.16,
                    "close": 8.33,
                    "volume": 952210,
                    "amount": 7928451,
                }
            ],
            "indicators": {"ma5": [8.29], "macd": [0.018]},
            "next_cursor": "app-cursor-2",
        }

    source = FridaParsedValueSource("core:27043", market_series_reader=series_reader)

    page = source.read_market_series("601872", "day", "app-cursor-1", 120)

    assert calls == [
        (
            "core:27043",
            "com.hexin.plat.android",
            8,
            "601872",
            "17",
            "day",
            "app-cursor-1",
            120,
        )
    ]
    assert page.bars[0].close == "8.33"
    assert page.indicators["ma5"] == ("8.29",)
    assert page.next_cursor == "app-cursor-2"


def test_market_series_reports_an_explicit_capability_gap_without_ui_fallback() -> None:
    source = FridaParsedValueSource("core:27043")

    page = source.read_market_series("601872", "day", None, 120)

    assert page.bars == ()
    assert page.source_error == "DIRECT_KLINE_UNAVAILABLE"


def test_dual_account_market_snapshot_keeps_core_quote_when_fund_interface_fails() -> None:
    core_payload = _runtime_payload()
    core_payload["quotes"][1].update({"times": [930], "prices": [19.78]})
    core = FridaParsedValueSource(
        "core:27043",
        request_scope="core_metrics",
        direct_reader=lambda *_args: core_payload,
    )

    def fund_failure(*_args):
        raise DirectRequestError("DIRECT_FUND_FLOW_TIMEOUT")

    fund = FridaParsedValueSource(
        "fund:27042",
        request_scope="main_fund_flow",
        direct_reader=fund_failure,
    )
    source = DualAccountParsedValueSource(core, fund)

    snapshot = source.read_market_snapshot("601872", detail=True)

    assert snapshot.quote["price"] == "19.78"
    assert snapshot.main_fund_flow == {}
    assert snapshot.source_errors == {
        "core_metrics": None,
        "main_fund_flow": "DIRECT_FUND_FLOW_TIMEOUT",
    }


def test_dual_account_market_snapshot_refreshes_fund_flow_on_a_slower_cadence() -> None:
    core_payload = _runtime_payload()
    core_payload["quotes"][1].update({"times": [930], "prices": [19.78]})
    fund_calls = 0

    def fund_reader(*_args):
        nonlocal fund_calls
        fund_calls += 1
        return {
            "fund_flows": [{
                "period": "today",
                "current_unit": "万元",
                "main_in": "12000",
                "main_listed": "7000",
                "main_grey": "5000",
                "main_retail_investor": "-12000",
            }]
        }

    source = DualAccountParsedValueSource(
        FridaParsedValueSource("core:27043", request_scope="core_metrics", direct_reader=lambda *_args: core_payload),
        FridaParsedValueSource("fund:27042", request_scope="main_fund_flow", direct_reader=fund_reader),
        fund_market_interval_seconds=15,
    )

    first = source.read_market_snapshot("601872", detail=True)
    second = source.read_market_snapshot("601872", detail=True)

    assert first.main_fund_flow["today"]["main_net_inflow"] == "12000.00"
    assert second.main_fund_flow == first.main_fund_flow
    assert fund_calls == 1


def test_intraday_series_right_aligns_short_values_and_keeps_permission_gaps() -> None:
    """Dropping a sentinel or left-aligning a short computed curve shifts every tooltip."""
    payload = _runtime_payload()
    payload["indicators"] = [
        {
            "symbol": "601872",
            "techid": 7034,
            "times": [930, 931, 932],
            "values": [21.2263, -2147483648],
        },
    ]

    outcome = FridaParsedValueSource(
        "127.0.0.1:27042",
        request_scope="core_metrics",
        direct_reader=lambda *_args: payload,
    ).read_direct("601872")

    assert outcome.intraday_series[MetricKind.RETAIL_COUNT]["points"] == [
        {"time": "09:30", "value": None},
        {"time": "09:31", "value": "21.23"},
        {"time": "09:32", "value": None},
    ]


def test_direct_payload_turns_the_big_order_permission_sentinel_into_missing_values() -> None:
    payload = _runtime_payload()
    payload["indicators"] = [
        {"symbol": "601872", "techid": 7031, "values": [-2147483648]},
        {"symbol": "601872", "techid": 7032, "values": [-2147483648]},
        {"symbol": "601872", "techid": 7034, "values": [21.2263]},
        {"symbol": "601872", "techid": 7051, "values": [0.0119]},
    ]

    values = FridaParsedValueSource._parse_payload(payload, "601872")

    assert values[MetricKind.LARGE_ORDER_NET] is None
    assert values[MetricKind.LARGE_ORDER_AMOUNT] is None
    assert values[MetricKind.RETAIL_COUNT] == "21.23"


def test_dual_account_source_queries_both_apps_in_parallel_and_merges_by_whitelist() -> None:
    core_started = Event()
    fund_started = Event()

    class CoreSource:
        def read_direct(self, _symbol: str):
            core_started.set()
            assert fund_started.wait(1), "fund query did not start in parallel"
            return DirectReadOutcome(
                values={
                    MetricKind.STOCK_NAME: "中国海油",
                    MetricKind.CURRENT_PRICE: "29.10",
                    MetricKind.MAIN_FLOW_TODAY_NET: "must-not-cross-source-boundary",
                },
                source_errors={"core_metrics": None, "main_fund_flow": None},
                intraday_series={
                    MetricKind.LARGE_ORDER_NET: {
                        "unit": None,
                        "points": [{"time": "09:30", "value": "0.12"}],
                    }
                },
            )

        def lookup_symbol(self, symbol: str):
            return SymbolLookup(symbol=symbol, name="中国海油", market="17")

    class FundSource:
        def read_direct(self, _symbol: str):
            fund_started.set()
            assert core_started.wait(1), "core query did not start in parallel"
            return {
                MetricKind.STOCK_NAME: "must-not-cross-source-boundary",
                MetricKind.MAIN_FLOW_TODAY_NET: "1.56",
                MetricKind.MAIN_FLOW_TODAY_UNIT: "亿元",
            }

    source = DualAccountParsedValueSource(CoreSource(), FundSource())

    outcome = source.read_direct("600938")

    assert isinstance(outcome, DirectReadOutcome)
    assert outcome.values[MetricKind.STOCK_NAME] == "中国海油"
    assert outcome.values[MetricKind.CURRENT_PRICE] == "29.10"
    assert outcome.values[MetricKind.MAIN_FLOW_TODAY_NET] == "1.56"
    assert outcome.values[MetricKind.MAIN_FLOW_TODAY_UNIT] == "亿元"
    assert outcome.intraday_series == {
        MetricKind.LARGE_ORDER_NET: {
            "unit": None,
            "points": [{"time": "09:30", "value": "0.12"}],
        }
    }
    assert outcome.source_errors == {
        "core_metrics": None,
        "main_fund_flow": None,
    }
    assert source.lookup_symbol("600938").name == "中国海油"


def test_dual_account_core_failure_does_not_wait_for_slow_fund_future() -> None:
    def fail_core(_symbol: str):
        raise DirectRequestError("DIRECT_APP_OFFLINE")

    def slow_fund(_symbol: str):
        time.sleep(0.5)
        return {}

    source = DualAccountParsedValueSource(
        types.SimpleNamespace(read_direct=fail_core),
        types.SimpleNamespace(read_direct=slow_fund),
    )
    started = time.monotonic()

    with pytest.raises(DirectRequestError, match="DIRECT_APP_OFFLINE"):
        source.read_direct("600938")

    assert time.monotonic() - started < 0.25
    source.close()


def test_dual_account_source_keeps_core_values_when_the_fund_interface_fails() -> None:
    core = types.SimpleNamespace(
        read_direct=lambda _symbol: {
            MetricKind.STOCK_NAME: "中国海油",
            MetricKind.CURRENT_PRICE: "29.10",
        },
        lookup_symbol=lambda symbol: SymbolLookup(symbol=symbol, name="中国海油", market="17"),
    )

    def fund_failure(_symbol: str):
        raise DirectRequestError("FUND_QUERY_REJECTED")

    source = DualAccountParsedValueSource(
        core,
        types.SimpleNamespace(read_direct=fund_failure),
    )

    outcome = source.read_direct("600938")

    assert outcome.values[MetricKind.STOCK_NAME] == "中国海油"
    assert outcome.values[MetricKind.MAIN_FLOW_TODAY_NET] is None
    assert outcome.source_errors["main_fund_flow"] == "FUND_QUERY_REJECTED"


def test_dual_account_source_preserves_a_core_interface_error_as_fatal() -> None:
    def core_failure(_symbol: str):
        raise DirectRequestError("DIRECT_MANAGER_UNAVAILABLE")

    source = DualAccountParsedValueSource(
        types.SimpleNamespace(read_direct=core_failure),
        types.SimpleNamespace(read_direct=lambda _symbol: {}),
    )

    with pytest.raises(DirectRequestError) as raised:
        source.read_direct("600938")

    assert raised.value.error_code == "DIRECT_MANAGER_UNAVAILABLE"


def test_direct_payload_formats_main_fund_flow_by_period_and_keeps_app_net_value() -> None:
    payload = _runtime_payload()
    payload["fund_flows"] = [
        {
            "period": "today",
            "current_unit": 100000000,
            "main_in": 5.35,
            "main_listed": -0.28,
            "main_grey": 5.63,
            "main_retail_investor": -5.35,
        },
        {
            "period": "three_day",
            "current_unit": 100000000,
            "main_in": 12.96,
            "main_listed": 3.63,
            "main_grey": 9.34,
            "main_retail_investor": -12.96,
        },
        {
            "period": "five_day",
            "current_unit": 100000000,
            "main_in": 15.95,
            "main_listed": 3.39,
            "main_grey": 12.57,
            "main_retail_investor": -15.95,
        },
    ]

    values = FridaParsedValueSource._parse_payload(payload, "601872")

    assert values[MetricKind.MAIN_FLOW_TODAY_UNIT] == "亿元"
    assert values[MetricKind.MAIN_FLOW_TODAY_NET] == "5.35"
    assert values[MetricKind.MAIN_FLOW_TODAY_VISIBLE] == "-0.28"
    assert values[MetricKind.MAIN_FLOW_TODAY_HIDDEN] == "5.63"
    assert values[MetricKind.MAIN_FLOW_TODAY_RETAIL] == "-5.35"
    assert values[MetricKind.MAIN_FLOW_THREE_DAY_NET] == "12.96"
    assert values[MetricKind.MAIN_FLOW_THREE_DAY_VISIBLE] == "3.63"
    assert values[MetricKind.MAIN_FLOW_THREE_DAY_HIDDEN] == "9.34"
    assert values[MetricKind.MAIN_FLOW_THREE_DAY_RETAIL] == "-12.96"
    assert values[MetricKind.MAIN_FLOW_FIVE_DAY_NET] == "15.95"
    assert values[MetricKind.MAIN_FLOW_FIVE_DAY_VISIBLE] == "3.39"
    assert values[MetricKind.MAIN_FLOW_FIVE_DAY_HIDDEN] == "12.57"
    assert values[MetricKind.MAIN_FLOW_FIVE_DAY_RETAIL] == "-15.95"


def test_direct_payload_formats_fund_flow_units_and_leaves_missing_fields_empty() -> None:
    values = FridaParsedValueSource._parse_payload(
        {
            "quotes": [],
            "indicators": [],
            "fund_flows": [
                {
                    "period": "today",
                    "current_unit": 10000,
                    "main_in": "-123.456",
                    "main_listed": None,
                    "main_grey": "0",
                    "main_retail_investor": "123.456",
                },
                {"period": "three_day", "current_unit": 100000000, "main_in": None},
            ],
        },
        "601872",
    )

    assert values[MetricKind.MAIN_FLOW_TODAY_UNIT] == "万元"
    assert values[MetricKind.MAIN_FLOW_TODAY_NET] == "-123.46"
    assert values[MetricKind.MAIN_FLOW_TODAY_VISIBLE] is None
    assert values[MetricKind.MAIN_FLOW_TODAY_HIDDEN] == "0.00"
    assert values[MetricKind.MAIN_FLOW_TODAY_RETAIL] == "123.46"
    assert values[MetricKind.MAIN_FLOW_THREE_DAY_UNIT] == "亿元"
    assert values[MetricKind.MAIN_FLOW_THREE_DAY_NET] is None


def test_direct_script_uses_the_three_capital_indicators_and_sequential_windows() -> None:
    for query_id in (
        "charge_main_capital",
        "charge_main_listed_capital",
        "charge_main_grey_capital",
    ):
        assert query_id in _FRIDA_DIRECT_SCRIPT
    assert "sif-charge-indicator-capital" in _FRIDA_DIRECT_SCRIPT
    assert "HurricaneDataSource" in _FRIDA_DIRECT_SCRIPT
    assert "win_size" in _FRIDA_DIRECT_SCRIPT
    assert _FRIDA_DIRECT_SCRIPT.index("['today', 1]") < _FRIDA_DIRECT_SCRIPT.index("['three_day', 3]")
    assert _FRIDA_DIRECT_SCRIPT.index("['three_day', 3]") < _FRIDA_DIRECT_SCRIPT.index("['five_day', 5]")
    assert "requestFundFlow(spec[0], spec[1])" in _FRIDA_DIRECT_SCRIPT


def test_dual_account_scripts_keep_core_and_fund_requests_on_their_owned_apps() -> None:
    assert "7031" in _FRIDA_CORE_DIRECT_SCRIPT
    assert "charge_main_capital" in _FRIDA_FUND_DIRECT_SCRIPT
    assert "requestScope === 'core_metrics'" in _FRIDA_CORE_DIRECT_SCRIPT
    assert "requestScope === 'main_fund_flow'" in _FRIDA_FUND_DIRECT_SCRIPT
    assert "? Promise.resolve()" in _FRIDA_CORE_DIRECT_SCRIPT
    assert _FRIDA_FUND_DIRECT_SCRIPT.index("['today', 1]") < _FRIDA_FUND_DIRECT_SCRIPT.index("['three_day', 3]")
    assert _FRIDA_FUND_DIRECT_SCRIPT.index("['three_day', 3]") < _FRIDA_FUND_DIRECT_SCRIPT.index("['five_day', 5]")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_core_direct_script_initializes_an_app_owned_manager_without_an_open_stock_page() -> None:
    """Falling back to a live qwg instance makes every cold App start fail."""
    harness = f"""
const calls = [];
const manager = {{}};
const registry = {{
  p: (pageKey) => {{ calls.push(['p', pageKey]); return manager; }},
  w: (pageKey, requests, mode) => {{ calls.push(['w', pageKey, requests.items.length, mode]); }},
  t: (pageKey) => {{ calls.push(['t', pageKey]); }}
}};
globalThis.rpc = {{ exports: {{}} }};
globalThis.Java = {{
  perform: (callback) => callback(),
  choose: (_name, callbacks) => callbacks.onComplete(),
  cast: (value) => value,
  use: (name) => {{
    if (name === 'rwg') return {{ i: () => registry }};
    if (name === 'java.util.ArrayList') return {{ $new: () => ({{
      items: [],
      add(value) {{ this.items.push(value); }}
    }}) }};
    if (name === 'java.util.HashMap') return {{ $new: () => ({{ put() {{}} }}) }};
    if (name === 'java.lang.Integer') return {{ valueOf: (value) => value }};
    if (name === 'ayg') return {{ $new: () => ({{ z() {{}} }}) }};
    if (name === 'com.hexin.android.biz_frame.eqframe.event.struct.EQBasicStockInfo') {{
      return {{ $new: () => {{ throw new Error('stop after manager initialization'); }} }};
    }}
    return {{}};
  }}
}};
new Function({json.dumps(_FRIDA_CORE_DIRECT_SCRIPT)})();
rpc.exports.request('600938', '17', 1000, 'core_metrics').then((result) => {{
  process.stdout.write(JSON.stringify({{ calls, result }}));
}});
"""

    completed = subprocess.run(
        ["node", "-e", harness],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert [call[0] for call in result["calls"]] == ["p", "w", "t"]
    assert result["calls"][1][2:] == [1, 3]
    assert result["calls"][0][1] == result["calls"][2][1]
    assert result["result"]["error_code"] == "DIRECT_REQUEST_UNAVAILABLE"


def test_request_scope_selects_the_matching_frida_runtime() -> None:
    core = FridaParsedValueSource("core:27042", request_scope="core_metrics")
    fund = FridaParsedValueSource("fund:27042", request_scope="main_fund_flow")

    assert core._direct_reader is FridaParsedValueSource._read_core_runtime
    assert fund._direct_reader is FridaParsedValueSource._read_fund_runtime


@pytest.mark.parametrize(
    ("scope", "expected_code"),
    [
        ("core_metrics", "DIRECT_APP_OFFLINE"),
        ("main_fund_flow", "DIRECT_FUND_FLOW_APP_OFFLINE"),
    ],
)
def test_scoped_direct_read_reports_a_stopped_frida_server_as_app_offline(
    monkeypatch, scope: str, expected_code: str
) -> None:
    """A dead role-specific Frida server must not be hidden as DIRECT_REQUEST_FAILED."""

    class ServerNotRunningError(Exception):
        pass

    class FakeDevice:
        def enumerate_applications(self):
            raise ServerNotRunningError("unable to connect to remote frida-server")

    fake_frida = types.SimpleNamespace(
        ServerNotRunningError=ServerNotRunningError,
        get_device_manager=lambda: types.SimpleNamespace(
            add_remote_device=lambda _endpoint: FakeDevice()
        ),
    )
    monkeypatch.setitem(sys.modules, "frida", fake_frida)
    source = FridaParsedValueSource("role:27042", request_scope=scope)

    with pytest.raises(DirectRequestError) as raised:
        source.read_direct("600938")

    assert raised.value.error_code == expected_code


def test_role_specific_frida_device_creation_is_serialized(monkeypatch) -> None:
    """Concurrent add_remote_device calls can invalidate the first Frida device handle."""
    start = Barrier(2)
    counter_lock = Lock()
    active_calls = 0
    maximum_active_calls = 0

    class FakeExports:
        def request(self, _symbol, _market, _timeout, _scope):
            return _runtime_payload()

    class FakeScript:
        exports_sync = FakeExports()

        def load(self):
            pass

        def unload(self):
            pass

    class FakeSession:
        def create_script(self, _source):
            return FakeScript()

        def detach(self):
            pass

    class FakeDevice:
        def enumerate_applications(self):
            return [types.SimpleNamespace(identifier="com.hexin.plat.android", pid=3526)]

        def attach(self, _pid):
            return FakeSession()

    class RaceSensitiveDeviceManager:
        def add_remote_device(self, _endpoint):
            nonlocal active_calls, maximum_active_calls
            with counter_lock:
                active_calls += 1
                maximum_active_calls = max(maximum_active_calls, active_calls)
                raced = active_calls > 1
            time.sleep(0.05)
            with counter_lock:
                active_calls -= 1
            if raced:
                raise RuntimeError("device is gone")
            return FakeDevice()

    manager = RaceSensitiveDeviceManager()
    fake_frida = types.SimpleNamespace(get_device_manager=lambda: manager)
    monkeypatch.setitem(sys.modules, "frida", fake_frida)
    sources = (
        FridaParsedValueSource("core:27043", request_scope="core_metrics"),
        FridaParsedValueSource("fund:27042", request_scope="main_fund_flow"),
    )

    def read(source):
        start.wait()
        return source.read_direct("601872")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(read, sources))

    assert all(result[MetricKind.STOCK_NAME] == "招商轮船" for result in results)
    assert maximum_active_calls == 1


def test_direct_read_surfaces_an_app_request_timeout_instead_of_scanning_stale_cache() -> None:
    source = FridaParsedValueSource(
        "127.0.0.1:27042",
        direct_reader=lambda *_args: {
            "error_code": "DIRECT_REQUEST_TIMEOUT",
            "error_message": "main quote response timed out",
        },
    )

    with pytest.raises(DirectRequestError) as raised:
        source.read_direct("300750")

    assert raised.value.error_code == "DIRECT_REQUEST_TIMEOUT"


def test_direct_read_preserves_the_specific_app_bridge_error_code() -> None:
    def unavailable(*_args):
        raise DirectRequestError("DIRECT_MANAGER_UNAVAILABLE", "curve manager is absent")

    source = FridaParsedValueSource("127.0.0.1:27042", direct_reader=unavailable)

    with pytest.raises(DirectRequestError) as raised:
        source.read_direct("601872")

    assert raised.value.error_code == "DIRECT_MANAGER_UNAVAILABLE"


@pytest.mark.parametrize(
    ("reader", "expected_code"),
    [
        (
            lambda *_args: (_ for _ in ()).throw(
                DirectRequestError(
                    "DIRECT_MANAGER_UNAVAILABLE",
                    "synthetic-frida-direct-secret-marker",
                )
            ),
            "DIRECT_MANAGER_UNAVAILABLE",
        ),
        (
            lambda *_args: (_ for _ in ()).throw(
                RuntimeError("synthetic-frida-generic-secret-marker")
            ),
            "DIRECT_REQUEST_FAILED",
        ),
        (
            lambda *_args: {
                "error_code": "DIRECT_REQUEST_TIMEOUT",
                "error_message": "synthetic-frida-payload-secret-marker",
            },
            "DIRECT_REQUEST_TIMEOUT",
        ),
    ],
)
def test_frida_direct_boundary_preserves_only_a_fixed_code_without_secret_traceback(
    reader,
    expected_code: str,
) -> None:
    source = FridaParsedValueSource("127.0.0.1:27042", direct_reader=reader)

    with pytest.raises(DirectRequestError) as caught:
        source.read_direct("601872")

    rendered = "".join(
        traceback.format_exception(
            type(caught.value), caught.value, caught.value.__traceback__
        )
    )
    assert caught.value.error_code == expected_code
    assert "synthetic-frida-direct-secret-marker" not in rendered
    assert "synthetic-frida-generic-secret-marker" not in rendered
    assert "synthetic-frida-payload-secret-marker" not in rendered


def test_frida_direct_boundary_rejects_an_untrusted_payload_error_code() -> None:
    source = FridaParsedValueSource(
        "127.0.0.1:27042",
        direct_reader=lambda *_args: {
            "error_code": "secret=cookie-marker",
            "error_message": "synthetic-frida-secret-marker",
        },
    )

    with pytest.raises(DirectRequestError) as caught:
        source.read_direct("601872")

    assert caught.value.error_code == "DIRECT_REQUEST_FAILED"
    assert "synthetic-frida-secret-marker" not in str(caught.value)


def test_direct_read_preserves_a_fund_flow_callback_error_code() -> None:
    source = FridaParsedValueSource(
        "127.0.0.1:27042",
        direct_reader=lambda *_args: {
            "error_code": "FUND_QUERY_REJECTED",
            "error_message": "capital indicator rejected",
        },
    )

    with pytest.raises(DirectRequestError) as raised:
        source.read_direct("601872")

    assert raised.value.error_code == "FUND_QUERY_REJECTED"


def test_default_direct_reader_calls_the_app_rpc_with_symbol_market_and_timeout(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeExports:
        def request(self, symbol, market, timeout):
            calls["request"] = (symbol, market, timeout)
            return _runtime_payload()

    class FakeScript:
        exports_sync = FakeExports()

        def load(self) -> None:
            calls["loaded"] = True

        def unload(self) -> None:
            calls["unloaded"] = True

    class FakeSession:
        def create_script(self, source: str):
            calls["script_has_request_export"] = "request: function" in source
            calls["script_has_intraday_time_axis"] = (
                "valuesFromCurve(parsed._d.value, 1)" in source
                and "times: times || []" in source
            )
            return FakeScript()

        def detach(self) -> None:
            calls["detached"] = True

    class FakeDevice:
        def enumerate_applications(self):
            return [types.SimpleNamespace(identifier="com.hexin.plat.android", pid=26226)]

        def attach(self, pid: int):
            calls["pid"] = pid
            return FakeSession()

    fake_frida = types.SimpleNamespace(
        get_device_manager=lambda: types.SimpleNamespace(
            add_remote_device=lambda endpoint: calls.setdefault("endpoint", endpoint) and FakeDevice()
        )
    )
    monkeypatch.setitem(sys.modules, "frida", fake_frida)
    source = FridaParsedValueSource("127.0.0.1:27042", timeout_seconds=3.5)

    values = source.read_direct("601872")

    assert values[MetricKind.STOCK_NAME] == "招商轮船"
    assert calls == {
        "endpoint": "127.0.0.1:27042",
        "pid": 26226,
        "script_has_request_export": True,
        "script_has_intraday_time_axis": True,
        "loaded": True,
        "request": ("601872", "17", 3500),
        "unloaded": True,
        "detached": True,
    }


def test_symbol_lookup_returns_the_only_exact_stock_match() -> None:
    """Selecting a fuzzy search result would allow the wrong stock to enter the queue."""
    source = FridaParsedValueSource(
        "127.0.0.1:27042",
        lookup_reader=lambda *_args: {
            "results": [
                {
                    "stock_code": "600143",
                    "stock_name": "金发科技",
                    "market_id": "17",
                    "market_label": "沪A",
                    "securities_code": None,
                },
                {
                    "stock_code": "600143.SH",
                    "stock_name": "非精确结果",
                    "market_id": "17",
                    "market_label": "沪A",
                    "securities_code": "600143.SH",
                },
            ]
        },
    )

    assert source.lookup_symbol("600143") == SymbolLookup(
        symbol="600143",
        name="金发科技",
        market="17",
        market_label="沪A",
        securities_code=None,
    )


def test_symbol_search_returns_supported_unique_app_candidates_in_app_order() -> None:
    source = FridaParsedValueSource(
        "127.0.0.1:27043",
        lookup_reader=lambda *_args: {
            "results": [
                {
                    "stock_code": "688027",
                    "stock_name": "国盾量子",
                    "market_id": "17",
                    "market_label": "科创",
                    "securities_code": None,
                },
                {
                    "stock_code": "123456",
                    "stock_name": "不支持市场",
                    "market_id": "17",
                    "market_label": "其他",
                    "securities_code": None,
                },
                {
                    "stock_code": "688027",
                    "stock_name": "国盾量子",
                    "market_id": "17",
                    "market_label": "科创",
                    "securities_code": None,
                },
                {
                    "stock_code": "501018",
                    "stock_name": "债券碰撞",
                    "market_id": "35",
                    "market_label": "债券",
                    "securities_code": None,
                },
                {
                    "stock_code": "501018",
                    "stock_name": "南方原油LOF",
                    "market_id": "20",
                    "market_label": "沪基",
                    "securities_code": None,
                },
                {
                    "stock_code": "300750",
                    "stock_name": "",
                    "market_id": "33",
                    "market_label": "创业",
                    "securities_code": None,
                },
            ]
        },
    )

    assert source.search_symbols("国盾", limit=8) == [
        SymbolLookup(
            symbol="688027",
            name="国盾量子",
            market="17",
            market_label="科创",
            securities_code=None,
        ),
        SymbolLookup(
            symbol="501018",
            name="南方原油LOF",
            market="20",
            market_label="沪基",
            securities_code=None,
        ),
    ]


@pytest.mark.parametrize(
    ("query", "limit"),
    [("", 8), ("国", 8), ("x" * 33, 8), ("科技", 0), ("科技", 9)],
)
def test_symbol_search_rejects_invalid_query_or_limit(query: str, limit: int) -> None:
    source = FridaParsedValueSource(
        "127.0.0.1:27043",
        lookup_reader=lambda *_args: {"results": []},
    )

    with pytest.raises(ValueError):
        source.search_symbols(query, limit=limit)


def test_symbol_search_limits_results_and_preserves_app_error_code() -> None:
    candidates = [
        {
            "stock_code": symbol,
            "stock_name": f"候选{symbol}",
            "market_id": "17",
            "market_label": "沪A",
            "securities_code": None,
        }
        for symbol in ("600000", "600001", "600002")
    ]
    source = FridaParsedValueSource(
        "127.0.0.1:27043",
        lookup_reader=lambda *_args: {"results": candidates},
    )

    assert [item.symbol for item in source.search_symbols("候选", limit=2)] == [
        "600000",
        "600001",
    ]

    failing = FridaParsedValueSource(
        "127.0.0.1:27043",
        lookup_reader=lambda *_args: {
            "error_code": "SYMBOL_LOOKUP_TIMEOUT",
            "error_message": "timeout",
        },
    )
    with pytest.raises(DirectRequestError) as captured:
        failing.search_symbols("科技")
    assert captured.value.error_code == "SYMBOL_LOOKUP_TIMEOUT"


def test_dual_account_symbol_search_uses_only_the_core_account() -> None:
    core = types.SimpleNamespace(
        search_symbols=lambda query, limit=8: [
            SymbolLookup("688027", "国盾量子", "17", "科创", None)
        ]
    )
    fund = types.SimpleNamespace(
        search_symbols=lambda *_args: (_ for _ in ()).throw(
            AssertionError("fund account must not search symbols")
        )
    )

    assert DualAccountParsedValueSource(core, fund).search_symbols("国盾", 8)[0].symbol == "688027"


def test_dual_account_can_keep_symbol_lookup_on_a_separate_app_source() -> None:
    calls: list[tuple[str, str]] = []

    class CoreTransport:
        def lookup_symbol(self, _symbol: str):
            raise AssertionError("direct core transport must not own symbol lookup")

    class SymbolSource:
        def lookup_symbol(self, symbol: str) -> SymbolLookup:
            calls.append(("lookup", symbol))
            return SymbolLookup(symbol=symbol, name="招商轮船", market="17")

        def search_symbols(self, query: str, limit: int) -> list[SymbolLookup]:
            calls.append(("search", query))
            return [SymbolLookup(symbol="601872", name="招商轮船", market="17")][:limit]

    source = DualAccountParsedValueSource(
        CoreTransport(),
        object(),
        symbol_source=SymbolSource(),
    )

    assert source.lookup_symbol("601872").name == "招商轮船"
    assert source.search_symbols("招商", 8)[0].symbol == "601872"
    assert calls == [("lookup", "601872"), ("search", "招商")]


def test_symbol_lookup_rejects_an_empty_exact_result() -> None:
    """An empty App response must not be presented as a valid stock."""
    source = FridaParsedValueSource(
        "127.0.0.1:27042",
        lookup_reader=lambda *_args: {"results": []},
    )

    with pytest.raises(SymbolLookupNotFoundError):
        source.lookup_symbol("600142")


def test_symbol_lookup_rejects_multiple_exact_results() -> None:
    """Two exact rows are ambiguous and must never be guessed between."""
    exact = {
        "stock_code": "600143",
        "stock_name": "金发科技",
        "market_id": "17",
        "market_label": "沪A",
        "securities_code": None,
    }
    source = FridaParsedValueSource(
        "127.0.0.1:27042",
        lookup_reader=lambda *_args: {"results": [exact, exact]},
    )

    with pytest.raises(SymbolLookupAmbiguousError):
        source.lookup_symbol("600143")


@pytest.mark.parametrize(
    ("symbol", "fund_market", "fund_label", "fund_name", "bond_market", "bond_name"),
    [
        ("501018", "20", "沪基", "南方原油LOF", "35", "奇消23B"),
        ("563000", "20", "沪基", "中国A50ETF易方达", "35", "山西2430"),
        ("160105", "36", "深基", "南方积配LOF", "19", "19山东47"),
        ("163208", "36", "深基", "全球油气能源LOF", "19", "20武金01"),
    ],
)
def test_symbol_lookup_ignores_exact_bond_collision_for_a_fund(
    symbol: str,
    fund_market: str,
    fund_label: str,
    fund_name: str,
    bond_market: str,
    bond_name: str,
) -> None:
    """Selecting the first exact code would return a bond instead of the listed fund."""
    source = FridaParsedValueSource(
        "127.0.0.1:27042",
        lookup_reader=lambda *_args: {
            "results": [
                {
                    "stock_code": symbol,
                    "stock_name": bond_name,
                    "market_id": bond_market,
                    "market_label": "债券",
                    "securities_code": None,
                },
                {
                    "stock_code": symbol,
                    "stock_name": fund_name,
                    "market_id": fund_market,
                    "market_label": fund_label,
                    "securities_code": None,
                },
            ]
        },
    )

    assert source.lookup_symbol(symbol) == SymbolLookup(
        symbol=symbol,
        name=fund_name,
        market=fund_market,
        market_label=fund_label,
        securities_code=None,
    )


def test_default_symbol_lookup_reader_calls_the_app_rpc_and_cleans_up(monkeypatch) -> None:
    """The production reader must use the App's UI-independent local search database."""
    calls: dict[str, object] = {}

    class FakeExports:
        def lookup(self, symbol, timeout):
            calls["lookup"] = (symbol, timeout)
            return {
                "results": [
                    {
                        "stock_code": "600143",
                        "stock_name": "金发科技",
                        "market_id": "17",
                        "market_label": "沪A",
                        "securities_code": None,
                    }
                ]
            }

    class FakeScript:
        exports_sync = FakeExports()

        def load(self) -> None:
            calls["loaded"] = True

        def unload(self) -> None:
            calls["unloaded"] = True

    class FakeSession:
        def create_script(self, _source: str):
            calls["script_source"] = _source
            return FakeScript()

        def detach(self) -> None:
            calls["detached"] = True

    class FakeDevice:
        def enumerate_applications(self):
            return [types.SimpleNamespace(identifier="com.hexin.plat.android", pid=26226)]

        def attach(self, pid: int):
            calls["pid"] = pid
            return FakeSession()

    fake_frida = types.SimpleNamespace(
        get_device_manager=lambda: types.SimpleNamespace(
            add_remote_device=lambda endpoint: calls.setdefault("endpoint", endpoint) and FakeDevice()
        )
    )
    monkeypatch.setitem(sys.modules, "frida", fake_frida)
    source = FridaParsedValueSource("127.0.0.1:27042", timeout_seconds=3.5)

    result = source.lookup_symbol("600143")

    assert result.name == "金发科技"
    script_source = calls.pop("script_source")
    assert calls == {
        "endpoint": "127.0.0.1:27042",
        "pid": 26226,
        "loaded": True,
        "lookup": ("600143", 3500),
        "unloaded": True,
        "detached": True,
    }
    assert "SearchStockFromHexinDB" in script_source
    assert "loadStockFromHexinDB" in script_source
    assert "AssociateViewModel" not in script_source
