from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from level2_service.database_first_fund_flow import DatabaseFirstFundFlowSource
from level2_service.models import FUND_FLOW_METRICS, FUND_FLOW_PERIODS, MetricKind
from level2_service.parsed_values import DirectReadOutcome
from level2_service.research_store import SQLiteResearchStore


SHANGHAI = ZoneInfo("Asia/Shanghai")


def periods(seed: str = "1.00") -> dict[str, dict[str, str | None]]:
    return {
        period: {
            "unit": "亿元",
            "main_net_inflow": seed,
            "main_visible_inflow": seed,
            "main_hidden_inflow": seed,
            "retail_inflow": f"-{seed}",
        }
        for period, _label, _unit in FUND_FLOW_PERIODS
    }


def outcome(seed: str = "9.00") -> DirectReadOutcome:
    values = {kind: None for kind in MetricKind}
    for period, _label, unit_kind in FUND_FLOW_PERIODS:
        values[unit_kind] = "亿元"
        for name, kind in FUND_FLOW_METRICS[period].items():
            values[kind] = seed if name != "retail_inflow" else f"-{seed}"
    return DirectReadOutcome(
        values=values,
        source_errors={"core_metrics": None, "main_fund_flow": None},
    )


def seed_store(tmp_path, captured_at: datetime, seed: str = "1.00") -> SQLiteResearchStore:
    store = SQLiteResearchStore(tmp_path / "research.db")
    store.record_fund_flow(
        "601872",
        trade_date="20260909",
        minute="15:00",
        periods=periods(seed),
        captured_at=captured_at,
    )
    return store


class Accounts:
    def __init__(self, symbols):
        self.symbols = symbols

    def list_watchlist_symbols(self):
        return self.symbols


class Live:
    def __init__(self):
        self.calls = []

    def read_direct(self, symbol: str):
        self.calls.append(symbol)
        return outcome()


def test_latest_fund_flow_snapshot_returns_shared_latest_minute(tmp_path):
    captured_at = datetime(2026, 9, 9, 15, 0, tzinfo=SHANGHAI)
    store = seed_store(tmp_path, captured_at, "2.00")

    result = store.latest_fund_flow_snapshot("601872", now=captured_at)

    assert result is not None
    assert result["trade_date"] == "20260909"
    assert result["minute"] == "15:00"
    assert result["periods"]["today"]["main_visible_inflow"] == "2.00"


def test_open_market_uses_fresh_database_without_live_request(tmp_path):
    now = datetime(2026, 9, 9, 10, 30, tzinfo=SHANGHAI)
    store = seed_store(tmp_path, now, "2.00")
    live = Live()
    source = DatabaseFirstFundFlowSource(
        live,
        Accounts(["601872"]),
        store,
        clock=lambda: now,
        is_market_open=lambda: True,
    )

    # Seed a current-minute database point for the open-session lookup.
    store.record_fund_flow(
        "601872",
        trade_date="20260909",
        minute="10:30",
        periods=periods("3.00"),
        captured_at=now,
    )
    result = source.read_direct("601872")

    assert live.calls == []
    assert result.values[MetricKind.MAIN_FLOW_TODAY_VISIBLE] == "3.00"


def test_open_market_falls_back_to_live_when_database_is_stale(tmp_path):
    now = datetime(2026, 9, 9, 10, 30, tzinfo=SHANGHAI)
    store = seed_store(tmp_path, datetime(2026, 9, 9, 10, 0, tzinfo=SHANGHAI), "2.00")
    live = Live()
    source = DatabaseFirstFundFlowSource(
        live,
        Accounts(["601872"]),
        store,
        clock=lambda: now,
        is_market_open=lambda: True,
    )

    result = source.read_direct("601872")

    assert live.calls == ["601872"]
    assert result.values[MetricKind.MAIN_FLOW_TODAY_VISIBLE] == "9.00"


def test_closed_market_freezes_database_and_allows_only_one_missing_data_fallback(tmp_path):
    now = datetime(2026, 9, 9, 16, 30, tzinfo=SHANGHAI)
    store = SQLiteResearchStore(tmp_path / "research.db")
    live = Live()
    source = DatabaseFirstFundFlowSource(
        live,
        Accounts(["601872"]),
        store,
        clock=lambda: now,
        is_market_open=lambda: False,
    )

    first = source.read_direct("601872")
    second = source.read_direct("601872")

    assert live.calls == ["601872"]
    assert first.values == second.values


def test_non_watchlist_symbol_always_uses_live_source(tmp_path):
    now = datetime(2026, 9, 9, 10, 30, tzinfo=SHANGHAI)
    store = seed_store(tmp_path, now, "2.00")
    live = Live()
    source = DatabaseFirstFundFlowSource(
        live,
        Accounts([]),
        store,
        clock=lambda: now,
        is_market_open=lambda: True,
    )

    source.read_direct("601872")

    assert live.calls == ["601872"]
