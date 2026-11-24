from __future__ import annotations

from datetime import datetime, timezone

from level2_service.market_data import MarketSnapshot, TimesharePoint
from level2_service.models import MetricKind
from level2_service.research_store import SQLiteResearchStore


def snapshot(*, last_price: str = "10.20") -> MarketSnapshot:
    return MarketSnapshot(
        symbol="601872",
        name="招商轮船",
        market="17",
        sequence=1,
        source_time="20260831 09:31:00",
        collected_at=datetime(2026, 8, 31, 1, 31, tzinfo=timezone.utc),
        source="TENCENT_PUBLIC",
        quote={"pre_close": "10.00"},
        timeshare=(
            TimesharePoint(time="09:30", price="10.10", average_price="10.05", volume="100"),
            TimesharePoint(time="09:31", price=last_price, average_price="10.10", volume="200"),
        ),
        intraday_series={
            "large_order_net": {
                "unit": None,
                "points": [
                    {"time": "09:30", "value": "1.00"},
                    {"time": "09:31", "value": "2.00"},
                ],
            },
            "large_order_amount": {
                "unit": "万",
                "points": [{"time": "09:31", "value": "3.0"}],
            },
        },
    )


def test_record_snapshot_returns_only_new_or_changed_minutes(tmp_path):
    store = SQLiteResearchStore(tmp_path / "research.db")

    assert store.record_snapshot(snapshot()) == ["09:30", "09:31"]
    assert store.record_snapshot(snapshot()) == []
    assert store.record_snapshot(snapshot(last_price="10.30")) == ["09:31"]


def test_fund_history_preserves_unrounded_amount(tmp_path):
    store = SQLiteResearchStore(tmp_path / "research.db")
    store.record_fund_flow("601872", trade_date="20260915", minute="10:00",
        periods={"today": {"unit": "亿元", "main_net_inflow": "1.23456789"}},
        captured_at=datetime.now(timezone.utc))
    point = store.read_fund_flow_history("601872")["periods"]["today"]["points"][0]
    assert point["main_net_inflow"] == "1.23456789"


def test_read_series_contains_dynamic_macd_and_authorized_source_metadata(tmp_path):
    store = SQLiteResearchStore(tmp_path / "research.db")
    store.save_macd_settings({"short": 2, "long": 3, "signal": 2, "marker_threshold": 0.001})
    store.record_snapshot(snapshot())

    result = store.read_series("601872", "20260831")

    assert result["symbol"] == "601872"
    assert result["trade_date"] == "20260831"
    assert result["source"] == "TENCENT_PUBLIC"
    assert [point["time"] for point in result["points"]] == ["09:30", "09:31"]
    assert result["points"][1]["large_order_net"] == "2.00"
    assert result["points"][1]["large_order_amount"] == "3.0"
    assert result["points"][1]["diff"] is not None
    assert result["macd_settings"] == {
        "short": 2,
        "long": 3,
        "signal": 2,
        "marker_threshold": 0.001,
    }


def test_latest_market_enrichment_reconstructs_closed_l2_and_fund_flow(tmp_path):
    store = SQLiteResearchStore(tmp_path / "research.db")
    store.record_snapshot(snapshot())
    store.record_fund_flow(
        "601872",
        trade_date="20260831",
        minute="15:00",
        periods={
            "today": {
                "unit": "亿元",
                "main_net_inflow": "10.67",
                "main_visible_inflow": "5.30",
                "main_hidden_inflow": "5.37",
                "retail_inflow": "-10.67",
            },
            "three_day": {
                "unit": "亿元",
                "main_net_inflow": "13.75",
                "main_visible_inflow": "8.10",
                "main_hidden_inflow": "5.65",
                "retail_inflow": "-13.75",
            },
            "five_day": {
                "unit": "亿元",
                "main_net_inflow": "22.30",
                "main_visible_inflow": "11.63",
                "main_hidden_inflow": "10.67",
                "retail_inflow": "-22.30",
            },
        },
        captured_at=datetime(2026, 8, 31, 7, 0, tzinfo=timezone.utc),
    )

    result = store.latest_market_enrichment("601872")

    assert result is not None
    assert result.values[MetricKind.LARGE_ORDER_NET] == "2.00"
    assert result.values[MetricKind.LARGE_ORDER_AMOUNT] == "3.0"
    assert result.values[MetricKind.MAIN_FLOW_TODAY_NET] == "10.67"
    assert result.intraday_series[MetricKind.LARGE_ORDER_NET]["points"][-1]["time"] == "09:31"


def test_latest_market_snapshot_preserves_the_last_complete_public_quote(tmp_path):
    store = SQLiteResearchStore(tmp_path / "research.db")
    store.record_snapshot(
        MarketSnapshot(
            symbol="601872",
            name="招商轮船",
            market="17",
            sequence=1,
            source_time="20260831 09:31:00",
            collected_at=datetime(2026, 8, 31, 1, 31, tzinfo=timezone.utc),
            source="TENCENT_PUBLIC",
            quote={
                "price": "10.20",
                "change_percent": "+2.00%",
                "previous_close": "10.00",
                "open": "10.10",
                "high": "10.30",
                "low": "10.05",
                "turnover_rate": "1.20%",
                "volume": "300",
                "amount": "123456",
            },
            timeshare=(
                TimesharePoint(time="09:30", price="10.10", volume="100"),
                TimesharePoint(time="09:31", price="10.20", volume="200"),
            ),
        )
    )

    result = store.latest_market_snapshot("601872")

    assert result is not None
    assert result.quote == {
        "price": "10.20",
        "change_percent": "+2.00%",
        "previous_close": "10.00",
        "open": "10.10",
        "high": "10.30",
        "low": "10.05",
        "turnover_rate": "1.20%",
        "volume": "300",
        "amount": "123456",
    }


def test_latest_market_enrichment_selects_core_and_fund_dates_independently(tmp_path):
    store = SQLiteResearchStore(tmp_path / "research.db")
    store.record_snapshot(snapshot())
    store.record_fund_flow(
        "601872",
        trade_date="20260914",
        minute="15:00",
        periods={
            period: {
                "unit": "亿元",
                "main_net_inflow": "-1.42",
                "main_visible_inflow": "-0.92",
                "main_hidden_inflow": "-0.50",
                "retail_inflow": "1.42",
            }
            for period in ("today", "three_day", "five_day")
        },
        captured_at=datetime(2026, 9, 14, 7, 0, tzinfo=timezone.utc),
    )

    result = store.latest_market_enrichment("601872")

    assert result is not None
    assert result.values[MetricKind.LARGE_ORDER_NET] == "2.00"
    assert result.values[MetricKind.MAIN_FLOW_TODAY_NET] == "-1.42"
    assert result.intraday_series[MetricKind.LARGE_ORDER_NET]["points"][-1]["time"] == "09:31"
    assert result.stored_trade_dates == {
        "core_metrics": "20260831",
        "main_fund_flow": "20260914",
    }


def test_monitoring_status_reports_latest_collection_and_error(tmp_path):
    store = SQLiteResearchStore(tmp_path / "research.db")
    store.record_snapshot(snapshot())
    store.record_error("300750", "MARKET_QUOTE_UNAVAILABLE")

    status = {item["symbol"]: item for item in store.monitoring_status(["601872", "300750"])}

    assert status["601872"]["latest_time"] == "09:31"
    assert status["601872"]["last_error"] is None
    assert status["300750"]["last_error"] == "MARKET_QUOTE_UNAVAILABLE"


def test_push_config_keeps_secrets_server_side_and_preserves_blank_updates(tmp_path):
    store = SQLiteResearchStore(tmp_path / "research.db")
    store.save_push_config({
        "enabled": True,
        "bark_groups": [{"id": "phone", "name": "手机", "base_url": "https://api.day.app", "device_key": "secret-key"}],
        "sc3_bot": {"enabled": True, "base_url": "https://bot.example", "token": "bot-secret", "chat_id": "1"},
        "wecom": {"enabled": True, "corp_id": "corp", "corp_secret": "corp-secret", "agent_id": 1, "to_user": "@all"},
        "rules": [],
    })

    public = store.push_config(public=True)
    assert public["bark_groups"][0]["device_key"] == ""
    assert public["bark_groups"][0]["device_key_configured"] is True
    assert public["sc3_bot"]["token"] == ""
    assert public["wecom"]["corp_secret"] == ""

    store.save_push_config({
        **public,
        "bark_groups": [{**public["bark_groups"][0], "name": "主手机"}],
    })
    private = store.push_config(public=False)
    assert private["bark_groups"][0]["device_key"] == "secret-key"
    assert private["sc3_bot"]["token"] == "bot-secret"
    assert private["wecom"]["corp_secret"] == "corp-secret"


def test_push_event_claim_is_atomic_and_deduplicated(tmp_path):
    store = SQLiteResearchStore(tmp_path / "research.db")

    assert store.claim_push_event("601872:20260831:09:31:buy") is True
    assert store.claim_push_event("601872:20260831:09:31:buy") is False


def test_research_store_resolves_weekend_snapshot_to_previous_trading_date(tmp_path):
    store = SQLiteResearchStore(
        tmp_path / "research.db",
        resolve_trade_date=lambda d: "20261120" if d.weekday() >= 5 else d.strftime("%Y%m%d"),
    )
    saturday_snapshot = MarketSnapshot(
        symbol="601872",
        name="招商轮船",
        market="17",
        sequence=1,
        source_time="15:00",
        collected_at=datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc),
        source="TENCENT_PUBLIC",
        quote={"pre_close": "10.00"},
        timeshare=(TimesharePoint(time="09:30", price="10.10"),),
    )

    store.record_snapshot(saturday_snapshot)

    assert store.available_dates("601872") == ["20261120"]


def test_read_fund_flow_daily_migrates_null_rows_and_uses_latest_valid_minute(tmp_path):
    import sqlite3

    db_path = tmp_path / "research.db"
    store = SQLiteResearchStore(db_path)
    store.record_fund_flow(
        "601872",
        trade_date="20261120",
        minute="14:58",
        periods={"today": {"unit": "亿元", "main_net_inflow": "1.20", "main_visible_inflow": "0.80", "main_hidden_inflow": "0.40", "retail_inflow": "-1.20"}},
        captured_at=datetime(2026, 11, 20, 14, 58, tzinfo=timezone.utc),
    )
    store.close()
    # Inject legacy pre-0929 and all-null polluted rows directly before reopening.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO fund_flow_history_points VALUES('601872','20261120','09:12','today','亿元','9.99','5.00','4.99','-9.99','2026-11-20T09:12:00')"
        )
        conn.execute(
            "INSERT INTO fund_flow_history_points VALUES('601872','20261120','15:00','today','亿元',NULL,NULL,NULL,NULL,'2026-11-20T15:00:00')"
        )
    reopened = SQLiteResearchStore(db_path)
    daily = reopened.read_fund_flow_daily("601872", 30, lambda d: "20261119")
    assert len(daily["points"]) == 2
    assert daily["points"][1]["time"] == "14:58"
    assert daily["points"][1]["main_net_inflow"] == "1.20"
    history = reopened.read_fund_flow_history("601872", "20261120")
    assert [pt["time"] for pt in history["periods"]["today"]["points"]] == ["14:58"]


def test_record_fund_flow_batch_today_map_and_expected_trade_date(tmp_path):
    from zoneinfo import ZoneInfo
    from level2_service.parsed_values import DirectReadOutcome

    store = SQLiteResearchStore(tmp_path / "research.db")
    captured = datetime(2026, 11, 24, 15, 2, tzinfo=ZoneInfo("Asia/Shanghai"))
    records = {
        sym: {
            period: {
                "unit": "亿元",
                "main_net_inflow": val,
                "main_visible_inflow": "1.00",
                "main_hidden_inflow": "0.50",
                "retail_inflow": f"-{val}",
            }
            for period in ("today", "three_day", "five_day")
        }
        for sym, val in (("601318", "5.68"), ("000001", "1.45"))
    }
    changed = store.record_fund_flow_batch(
        records,
        trade_date="20261124",
        minute="15:00",
        captured_at=captured,
    )
    assert changed == {"601318": True, "000001": True}

    today_map = store.latest_today_fund_flow_map(["601318", "000001", "300059"])
    assert set(today_map) == {"601318", "000001"}
    assert today_map["601318"]["main_net_inflow"] == "5.68"
    assert today_map["000001"]["minute"] == "15:00"

    # Prior L2 point from 20260831 must not leak when expected_trade_date="20261124".
    store.record_snapshot(snapshot())
    isolated = store.latest_market_enrichment("601872", expected_trade_date="20261124")
    assert isolated is None
    unscoped = store.latest_market_enrichment("601872")
    assert isinstance(unscoped, DirectReadOutcome)
    assert unscoped.stored_trade_dates["core_metrics"] == "20260831"
