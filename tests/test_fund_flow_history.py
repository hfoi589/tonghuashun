from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from level2_service.fund_flow_history import (
    FundFlowHistoryMonitor,
    intraday_fund_flow_cap_minute,
    settled_fund_flow_minute,
)
from level2_service.models import FUND_FLOW_METRICS, FUND_FLOW_PERIODS, MetricKind
from level2_service.parsed_values import DirectReadOutcome
from level2_service.research_store import SQLiteResearchStore


def _period_values(seed: str = "1.00") -> dict[str, dict[str, str | None]]:
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


def _outcome(seed: str = "1.00") -> DirectReadOutcome:
    values = {kind: None for kind in MetricKind}
    for period, _label, unit_kind in FUND_FLOW_PERIODS:
        values[unit_kind] = "亿元"
        for name, kind in FUND_FLOW_METRICS[period].items():
            values[kind] = seed if name != "retail_inflow" else f"-{seed}"
    return DirectReadOutcome(
        values=values,
        source_errors={"core_metrics": None, "main_fund_flow": None},
    )


def test_fund_flow_history_upserts_periods_and_preserves_non_null_values(tmp_path):
    store = SQLiteResearchStore(tmp_path / "research.db")
    captured_at = datetime(2026, 9, 9, 2, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert store.record_fund_flow(
        "601872",
        trade_date="20260909",
        minute="10:30",
        periods=_period_values(),
        captured_at=captured_at,
    ) is True

    partial = _period_values("2.00")
    partial["today"]["main_hidden_inflow"] = None
    assert store.record_fund_flow(
        "601872",
        trade_date="20260909",
        minute="10:30",
        periods=partial,
        captured_at=captured_at,
    ) is True

    result = store.read_fund_flow_history("601872", "20260909")
    assert result["available_dates"] == ["20260909"]
    assert result["periods"]["today"]["points"][0]["main_visible_inflow"] == "2.00"
    assert result["periods"]["today"]["points"][0]["main_hidden_inflow"] == "1.00"


def test_fund_flow_history_cleanup_removes_only_expired_dates(tmp_path):
    store = SQLiteResearchStore(tmp_path / "research.db")
    for trade_date in ("20260301", "20260909"):
        store.record_fund_flow(
            "601872",
            trade_date=trade_date,
            minute="10:30",
            periods=_period_values(),
            captured_at=datetime(2026, 9, 9, 2, 30),
        )

    assert store.cleanup_fund_flow_history("20260901") == 3
    assert store.available_fund_flow_dates("601872") == ["20260909"]


class _Accounts:
    def list_watchlist_symbols(self):
        return ["601872", "601872", "600026"]


class _FundSource:
    def __init__(self):
        self.calls: list[str] = []

    def read_direct(self, symbol: str):
        self.calls.append(symbol)
        return _outcome("1.00" if symbol == "601872" else "2.00")


def test_fund_flow_monitor_deduplicates_symbols_and_writes_shared_history(tmp_path):
    store = SQLiteResearchStore(tmp_path / "research.db")
    source = _FundSource()
    monitor = FundFlowHistoryMonitor(
        _Accounts(),
        source,
        store,
        is_market_open=lambda: True,
        clock=lambda: datetime(2026, 9, 9, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    result = asyncio.run(monitor.poll_once())

    assert sorted(source.calls) == ["600026", "601872"]
    assert result == {"600026": True, "601872": True}
    assert store.available_fund_flow_dates("601872") == ["20260909"]


def test_fund_flow_monitor_marks_unit_only_response_as_empty(tmp_path):
    store = SQLiteResearchStore(tmp_path / "research.db")

    class EmptyMetricsSource:
        def read_direct(self, _symbol: str):
            values = {kind: None for kind in MetricKind}
            for _period, _label, unit_kind in FUND_FLOW_PERIODS:
                values[unit_kind] = "万元"
            return DirectReadOutcome(
                values=values,
                source_errors={"core_metrics": None, "main_fund_flow": None},
            )

    monitor = FundFlowHistoryMonitor(
        _Accounts(),
        EmptyMetricsSource(),
        store,
        is_market_open=lambda: True,
        clock=lambda: datetime(2026, 9, 9, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    result = asyncio.run(monitor.poll_once())

    assert result == {"600026": False, "601872": False}
    assert store.read_fund_flow_history("601872") ["last_error"] == "DIRECT_FUND_FLOW_EMPTY"


def test_fund_flow_monitor_skips_exchange_funds_and_suppresses_pre_0929_today(tmp_path):
    store = SQLiteResearchStore(tmp_path / "research.db")

    class MixedAccounts:
        def list_watchlist_symbols(self):
            return ["601872", "513350", "162411"]

    source = _FundSource()
    monitor = FundFlowHistoryMonitor(
        MixedAccounts(),
        source,
        store,
        is_market_open=lambda: True,
        clock=lambda: datetime(2026, 9, 9, 9, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    result = asyncio.run(monitor.poll_once())

    assert source.calls == ["601872"]
    assert result == {"601872": True}
    history = store.read_fund_flow_history("601872", "20260909")
    assert history["periods"]["today"]["points"] == []
    assert len(history["periods"]["three_day"]["points"]) == 1
    assert len(history["periods"]["five_day"]["points"]) == 1
    assert store.available_fund_flow_dates("513350") == []


def test_fund_flow_monitor_drops_unit_only_today_when_other_periods_have_values(tmp_path):
    store = SQLiteResearchStore(tmp_path / "research.db")

    class PartialTodaySource:
        def read_direct(self, _symbol: str):
            values = {kind: None for kind in MetricKind}
            values[MetricKind.MAIN_FLOW_TODAY_UNIT] = "万元"
            values[MetricKind.MAIN_FLOW_THREE_DAY_UNIT] = "亿元"
            values[MetricKind.MAIN_FLOW_THREE_DAY_NET] = "2.50"
            values[MetricKind.MAIN_FLOW_THREE_DAY_VISIBLE] = "1.50"
            values[MetricKind.MAIN_FLOW_THREE_DAY_HIDDEN] = "1.00"
            values[MetricKind.MAIN_FLOW_THREE_DAY_RETAIL] = "-2.50"
            values[MetricKind.MAIN_FLOW_FIVE_DAY_UNIT] = "亿元"
            values[MetricKind.MAIN_FLOW_FIVE_DAY_NET] = "3.50"
            values[MetricKind.MAIN_FLOW_FIVE_DAY_VISIBLE] = "2.00"
            values[MetricKind.MAIN_FLOW_FIVE_DAY_HIDDEN] = "1.50"
            values[MetricKind.MAIN_FLOW_FIVE_DAY_RETAIL] = "-3.50"
            return DirectReadOutcome(
                values=values,
                source_errors={"core_metrics": None, "main_fund_flow": None},
            )

    monitor = FundFlowHistoryMonitor(
        _Accounts(),
        PartialTodaySource(),
        store,
        is_market_open=lambda: True,
        clock=lambda: datetime(2026, 9, 9, 9, 35, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    asyncio.run(monitor.poll_once())

    history = store.read_fund_flow_history("601872", "20260909")
    assert history["periods"]["today"]["points"] == []
    assert history["periods"]["three_day"]["points"][0]["main_net_inflow"] == "2.50"


def test_fund_flow_monitor_post_close_150130_sweeps_1500_and_fires_callback_once(tmp_path):
    store = SQLiteResearchStore(tmp_path / "research.db")

    class TwoAccounts:
        @staticmethod
        def list_watchlist_symbols():
            return ["601872", "000001", "160723"]

    class BatchSource:
        def __init__(self) -> None:
            self.calls: list[tuple[list[str], bool]] = []

        def read_direct(self, _symbol: str):
            return _outcome("9.99")

        def read_direct_batch(self, symbols: list[str], *, force_all_periods: bool = False):
            self.calls.append((list(symbols), force_all_periods))
            return {sym: _outcome("3.21") for sym in symbols}

    finalized: list[list[str]] = []
    source = BatchSource()
    monitor = FundFlowHistoryMonitor(
        TwoAccounts(),
        source,
        store,
        is_market_open=lambda: False,
        is_post_close_window=lambda _dt: True,
        on_post_close_finalized=lambda syms: finalized.append(list(syms)),
        clock=lambda: datetime(2026, 11, 24, 15, 1, 45, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    first = asyncio.run(monitor.poll_once())
    second = asyncio.run(monitor.poll_once())

    assert source.calls == [(["000001", "601872"], True)]
    assert first == {"000001": True, "601872": True}
    assert second == {}
    assert finalized == [["000001", "601872"]]
    assert [pt["time"] for pt in store.read_fund_flow_history("601872", "20261124")["periods"]["today"]["points"]] == ["15:00"]


def test_settled_fund_flow_minute_and_intraday_cap_respect_trading_windows():
    sh = ZoneInfo("Asia/Shanghai")
    assert settled_fund_flow_minute("20261124", datetime(2026, 11, 25, 11, 52, tzinfo=sh)) == "15:00"
    assert settled_fund_flow_minute("20261125", datetime(2026, 11, 25, 9, 15, tzinfo=sh)) is None
    assert settled_fund_flow_minute("20261125", datetime(2026, 11, 25, 10, 42, tzinfo=sh)) == "10:42"
    assert settled_fund_flow_minute("20261125", datetime(2026, 11, 25, 11, 52, tzinfo=sh)) == "11:30"
    assert settled_fund_flow_minute("20261125", datetime(2026, 11, 25, 13, 45, tzinfo=sh)) == "13:45"
    assert settled_fund_flow_minute("20261125", datetime(2026, 11, 25, 15, 2, tzinfo=sh)) == "15:00"

    assert intraday_fund_flow_cap_minute("20261124", datetime(2026, 11, 25, 12, 26, tzinfo=sh)) is None
    assert intraday_fund_flow_cap_minute("20261125", datetime(2026, 11, 25, 9, 15, tzinfo=sh)) == "09:29"
    assert intraday_fund_flow_cap_minute("20261125", datetime(2026, 11, 25, 12, 26, tzinfo=sh)) == "11:30"
    assert intraday_fund_flow_cap_minute("20261125", datetime(2026, 11, 25, 15, 1, tzinfo=sh)) is None


def test_store_startup_purges_premature_1500_rows_captured_intraday(tmp_path):
    sh = ZoneInfo("Asia/Shanghai")
    db_path = tmp_path / "research.db"
    store = SQLiteResearchStore(db_path)
    store.record_fund_flow(
        "601872",
        trade_date="20261124",
        minute="15:00",
        periods=_period_values("1.00"),
        captured_at=datetime(2026, 11, 24, 15, 1, tzinfo=sh),
    )
    store.record_fund_flow(
        "601872",
        trade_date="20261125",
        minute="11:30",
        periods=_period_values("2.00"),
        captured_at=datetime(2026, 11, 25, 11, 30, tzinfo=sh),
    )
    store.record_fund_flow(
        "601872",
        trade_date="20261125",
        minute="15:00",
        periods=_period_values("2.00"),
        captured_at=datetime(2026, 11, 25, 11, 52, 41, tzinfo=sh),
    )
    store.close()

    reopened = SQLiteResearchStore(db_path)
    today_pts = reopened.read_fund_flow_history("601872", "20261125")["periods"]["today"]["points"]
    yday_pts = reopened.read_fund_flow_history("601872", "20261124")["periods"]["today"]["points"]
    assert [pt["time"] for pt in today_pts] == ["11:30"]
    assert [pt["time"] for pt in yday_pts] == ["15:00"]
    assert reopened.fund_flow_status("601872")["latest_time"] == "11:30"
    reopened.close()
