from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from level2_service.fund_flow_history import FundFlowHistoryMonitor
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
