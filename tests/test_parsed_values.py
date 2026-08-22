from __future__ import annotations

import sys
import types

from level2_service.models import MetricKind
import pytest

from level2_service.parsed_values import (
    DirectRequestError,
    FridaParsedValueSource,
    SymbolLookup,
    SymbolLookupAmbiguousError,
    SymbolLookupNotFoundError,
    UnsupportedMarketError,
    market_code_for_symbol,
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


def test_frida_source_selects_the_requested_stock_and_formats_all_runtime_values() -> None:
    """Selecting the first cached object would mix 招商南油 into the 601872 result."""
    source = FridaParsedValueSource(
        "127.0.0.1:27042",
        runtime_reader=lambda *_args: _runtime_payload(),
    )

    values = source.read("601872")

    assert values == {
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
    """The production reader must pass the exact code to the attached App process."""
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
    assert calls == {
        "endpoint": "127.0.0.1:27042",
        "pid": 26226,
        "loaded": True,
        "lookup": ("600143", 3500),
        "unloaded": True,
        "detached": True,
    }
