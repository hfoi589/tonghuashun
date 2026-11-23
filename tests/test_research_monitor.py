from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from level2_service.market_data import MarketSnapshot, TimesharePoint
from level2_service.research_monitor import ResearchMonitor, format_push_change_pct


class Accounts:
    def list_monitored_symbols(self):
        return ["300750", "601872"]


class Broker:
    async def refresh(self, symbol, *, detail, max_age_seconds):
        if symbol == "300750":
            error = RuntimeError("offline")
            error.error_code = "MARKET_QUOTE_UNAVAILABLE"
            raise error
        return MarketSnapshot(
            symbol=symbol,
            name="招商轮船",
            market="17",
            sequence=1,
            source_time="20260831 09:30:00",
            collected_at=datetime(2026, 8, 31, 1, 30, tzinfo=timezone.utc),
            quote={},
            source="TENCENT_PUBLIC",
            timeshare=(TimesharePoint(time="09:30", price="10.00"),),
        )


class Store:
    def __init__(self):
        self.snapshots = []
        self.errors = []

    def record_snapshot(self, snapshot):
        self.snapshots.append(snapshot.symbol)
        return ["09:30"]

    def record_error(self, symbol, error):
        self.errors.append((symbol, error))


def test_monitor_polls_enabled_union_and_isolates_per_symbol_errors():
    store = Store()
    monitor = ResearchMonitor(Accounts(), Broker(), store, max_concurrent=2)

    result = asyncio.run(monitor.poll_once())

    assert store.snapshots == ["601872"]
    assert store.errors == [("300750", "MARKET_QUOTE_UNAVAILABLE")]
    assert result == {"300750": [], "601872": ["09:30"]}


class PushStore(Store):
    def __init__(self):
        super().__init__()
        self.claimed = set()

    def read_series(self, symbol):
        return {
            "symbol": symbol,
            "name": "招商轮船",
            "pre_close": "9.80",
            "trade_date": "20260831",
            "macd_settings": {"marker_threshold": 0},
            "points": [{"time": "09:30", "price": "10.00", "diff": 0, "dea": 0, "macd": 0}],
        }

    def push_config(self, public=False):
        return {
            "enabled": True,
            "bark_groups": [{"id": "phone"}],
            "rules": [{
                "id": "buy", "enabled": True, "side": "buy", "title": "买入", "joiner": "and",
                "conditions": [{"type": "price_threshold", "direction": "gte", "value": 9}],
            }],
        }

    def claim_push_event(self, key):
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True


class Dispatcher:
    def __init__(self):
        self.sent = []

    def send(self, config, title, body):
        self.sent.append((title, body))
        return {"ok": True}


def test_monitor_pushes_only_changed_minutes_and_deduplicates_events():
    store = PushStore()
    dispatcher = Dispatcher()
    monitor = ResearchMonitor(Accounts(), Broker(), store, dispatcher=dispatcher, max_concurrent=2)

    asyncio.run(monitor.poll_once())
    asyncio.run(monitor.poll_once())

    assert dispatcher.sent == [("买入", "招商轮船 (601872) 09:30 价格 10.00 涨跌幅 +2.04%")]


class FundAccounts:
    def list_monitored_symbols(self):
        return ["160723"]


class FundBroker:
    async def refresh(self, symbol, *, detail, max_age_seconds):
        return MarketSnapshot(
            symbol=symbol,
            name="嘉实原油",
            market="16",
            sequence=1,
            source_time="20260831 09:30:00",
            collected_at=datetime(2026, 8, 31, 1, 30, tzinfo=timezone.utc),
            quote={},
            source="TENCENT_PUBLIC",
            timeshare=(TimesharePoint(time="09:30", price="1.523"),),
        )


class EmptyNameFundStore(PushStore):
    def read_series(self, symbol):
        return {
            "symbol": symbol,
            "name": "",
            "pre_close": "1.550",
            "trade_date": "20260831",
            "macd_settings": {"marker_threshold": 0},
            "points": [{"time": "09:30", "price": "1.523", "diff": 0, "dea": 0, "macd": 0}],
        }

    def push_config(self, public=False):
        return {
            "enabled": True,
            "bark_groups": [{"id": "phone"}],
            "rules": [{
                "id": "buy", "enabled": True, "side": "buy", "title": "买入", "joiner": "and",
                "conditions": [{"type": "price_threshold", "direction": "gte", "value": 1}],
            }],
        }


def test_monitor_resolves_fallback_name_and_three_decimal_price_for_fund():
    store = EmptyNameFundStore()
    dispatcher = Dispatcher()
    monitor = ResearchMonitor(FundAccounts(), FundBroker(), store, dispatcher=dispatcher, max_concurrent=2)

    asyncio.run(monitor.poll_once())

    assert dispatcher.sent == [("买入", "嘉实原油 (160723) 09:30 价格 1.523 涨跌幅 -1.74%")]


def test_format_push_change_pct_handles_positive_negative_and_negative_zero():
    assert format_push_change_pct(2.345) == "+2.35%"
    assert format_push_change_pct(-1.2) == "-1.20%"
    assert format_push_change_pct(-0.001) == "0.00%"
    assert format_push_change_pct(None) == ""
