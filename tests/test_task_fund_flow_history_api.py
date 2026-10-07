from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from level2_service.api import create_app
from level2_service.market_accounts import InMemoryMarketSessionStore, SQLiteMarketAccountStore
from level2_service.models import MetricKind, TaskStatus
from level2_service.parsed_values import SymbolLookup
from level2_service.queue import InMemoryStreams
from level2_service.research_store import SQLiteResearchStore


def _values():
    return {
        MetricKind.STOCK_NAME: "招商轮船",
        MetricKind.CURRENT_PRICE: "20.99",
        MetricKind.CHANGE_PERCENT: "+10.01%",
        MetricKind.TURNOVER_RATE: "2.36%",
        MetricKind.LARGE_ORDER_NET: "0.33",
        MetricKind.LARGE_ORDER_AMOUNT: "54165.0万",
        MetricKind.RETAIL_COUNT: "-2.32",
        MetricKind.MACDFS: "0.000",
    }


def test_task_fund_flow_history_returns_today_only_for_watchlist_symbol(tmp_path):
    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = accounts.create_user("trader", "temporary-123")
    group = accounts.list_watchlists(user.id)[0]
    accounts.add_symbol(user.id, group.id, SymbolLookup(symbol="601872", name="招商轮船", market="17"))
    research = SQLiteResearchStore(tmp_path / "research.db")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    research.record_fund_flow(
        "601872",
        trade_date=today,
        minute="14:59",
        periods={
            "today": {
                "unit": "亿元",
                "main_net_inflow": "10.67",
                "main_visible_inflow": "5.30",
                "main_hidden_inflow": "5.37",
                "retail_inflow": "-10.67",
            }
        },
        captured_at=datetime.now(ZoneInfo("Asia/Shanghai")),
    )
    store = InMemoryStreams()
    app = create_app(
        store=store,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        research_store=research,
        symbol_lookup=lambda symbol: SymbolLookup(symbol=symbol, name="招商轮船", market="17"),
    )
    client = TestClient(app)
    public_id = client.post("/api/v1/jobs", json={"symbol": "601872", "include_long_capture": False}).json()["public_id"]
    store.transition(public_id, TaskStatus.RUNNING)
    store.complete_result(public_id, _values(), None)

    response = client.get(f"/api/v1/jobs/{public_id}/fund-flow-history")

    assert response.status_code == 200
    assert response.json()["trade_date"] == today
    assert response.json()["points"][0]["main_visible_inflow"] == "5.30"


def test_task_fund_flow_daily_returns_recent_closing_points_for_watchlist_symbol(tmp_path):
    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = accounts.create_user("trader", "temporary-123")
    group = accounts.list_watchlists(user.id)[0]
    accounts.add_symbol(user.id, group.id, SymbolLookup(symbol="601872", name="招商轮船", market="17"))
    research = SQLiteResearchStore(tmp_path / "research.db")
    for trade_date, value in (("20260915", "8.00"), ("20260916", "10.00")):
        research.record_fund_flow(
            "601872",
            trade_date=trade_date,
            minute="14:59",
            periods={
                "today": {
                    "unit": "万元",
                    "main_net_inflow": value,
                    "main_visible_inflow": "5.00",
                    "main_hidden_inflow": "3.00",
                    "retail_inflow": f"-{value}",
                },
                "three_day": {
                    "unit": "万元",
                    "main_net_inflow": f"3{value}",
                    "main_visible_inflow": "15.00",
                    "main_hidden_inflow": "18.00",
                    "retail_inflow": f"-3{value}",
                },
                "five_day": {
                    "unit": "万元",
                    "main_net_inflow": f"5{value}",
                    "main_visible_inflow": "25.00",
                    "main_hidden_inflow": "28.00",
                    "retail_inflow": f"-5{value}",
                },
            },
            captured_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        )
    store = InMemoryStreams()
    app = create_app(
        store=store,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        research_store=research,
        previous_trading_date=lambda value: "20260914",
        symbol_lookup=lambda symbol: SymbolLookup(symbol=symbol, name="招商轮船", market="17"),
    )
    client = TestClient(app)
    public_id = client.post("/api/v1/jobs", json={"symbol": "601872", "include_long_capture": False}).json()["public_id"]
    store.transition(public_id, TaskStatus.RUNNING)
    store.complete_result(public_id, _values(), None)

    response = client.get(f"/api/v1/jobs/{public_id}/fund-flow-daily?limit=30")

    assert response.status_code == 200
    assert response.json()["symbol"] == "601872"
    assert [point["trade_date"] for point in response.json()["points"]] == ["20260914", "20260915", "20260916"]
    assert response.json()["points"][-1]["main_net_inflow"] == "10.00"
    assert response.json()["points"][-1]["periods"]["three_day"]["main_net_inflow"] == "310.00"
    assert response.json()["points"][-1]["periods"]["five_day"]["main_net_inflow"] == "510.00"
    assert response.json()["points"][0]["periods"] is None


def test_task_fund_flow_history_hides_non_watchlist_symbol(tmp_path):
    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    research = SQLiteResearchStore(tmp_path / "research.db")
    store = InMemoryStreams()
    app = create_app(
        store=store,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        research_store=research,
        symbol_lookup=lambda symbol: SymbolLookup(symbol=symbol, name="招商轮船", market="17"),
    )
    client = TestClient(app)
    public_id = client.post("/api/v1/jobs", json={"symbol": "601872", "include_long_capture": False}).json()["public_id"]
    store.transition(public_id, TaskStatus.RUNNING)
    store.complete_result(public_id, _values(), None)

    assert client.get(f"/api/v1/jobs/{public_id}/fund-flow-history").status_code == 404


def test_closed_market_watchlist_uses_latest_previous_history(tmp_path, monkeypatch):
    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = accounts.create_user("trader", "temporary-123")
    group = accounts.list_watchlists(user.id)[0]
    accounts.add_symbol(user.id, group.id, SymbolLookup(symbol="601872", name="招商轮船", market="17"))
    research = SQLiteResearchStore(tmp_path / "research.db")
    research.record_fund_flow(
        "601872",
        trade_date="20260910",
        minute="14:59",
        periods={"today": {"unit": "亿元", "main_net_inflow": "2.00", "main_visible_inflow": "1.20", "main_hidden_inflow": "0.80", "retail_inflow": "-2.00"}},
        captured_at=datetime.now(ZoneInfo("Asia/Shanghai")),
    )
    monkeypatch.setattr("level2_service.api.is_china_market_open", lambda: False)
    store = InMemoryStreams()
    app = create_app(
        store=store,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        research_store=research,
        symbol_lookup=lambda symbol: SymbolLookup(symbol=symbol, name="招商轮船", market="17"),
    )
    client = TestClient(app)
    public_id = client.post("/api/v1/jobs", json={"symbol": "601872", "include_long_capture": False}).json()["public_id"]
    store.transition(public_id, TaskStatus.RUNNING)
    store.complete_result(public_id, _values(), None)

    response = client.get(f"/api/v1/jobs/{public_id}/fund-flow-history")

    assert response.status_code == 200
    assert response.json()["trade_date"] == "20260910"
    assert response.json()["points"][0]["main_visible_inflow"] == "1.20"


def test_closed_market_snapshot_uses_latest_previous_history(tmp_path, monkeypatch):
    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = accounts.create_user("trader", "temporary-123")
    group = accounts.list_watchlists(user.id)[0]
    accounts.add_symbol(user.id, group.id, SymbolLookup(symbol="601872", name="招商轮船", market="17"))
    research = SQLiteResearchStore(tmp_path / "research.db")
    research.record_fund_flow(
        "601872",
        trade_date="20260910",
        minute="14:59",
        periods={"today": {"unit": "亿元", "main_net_inflow": "2.00", "main_visible_inflow": "1.20", "main_hidden_inflow": "0.80", "retail_inflow": "-2.00"}},
        captured_at=datetime.now(ZoneInfo("Asia/Shanghai")),
    )
    monkeypatch.setattr("level2_service.api.is_china_market_open", lambda: False)
    store = InMemoryStreams()
    app = create_app(
        store=store,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        research_store=research,
        symbol_lookup=lambda symbol: SymbolLookup(symbol=symbol, name="招商轮船", market="17"),
    )
    client = TestClient(app)
    public_id = client.post("/api/v1/jobs", json={"symbol": "601872", "include_long_capture": False}).json()["public_id"]
    store.transition(public_id, TaskStatus.RUNNING)
    store.complete_market_snapshot(public_id, {"symbol": "601872", "quote": {"price": "20.99"}})

    response = client.get(f"/api/v1/jobs/{public_id}/fund-flow-history")

    assert response.status_code == 200
    assert response.json()["trade_date"] == "20260910"
    assert response.json()["points"][0]["main_visible_inflow"] == "1.20"


def test_open_market_without_today_history_hides_previous_history(tmp_path, monkeypatch):
    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = accounts.create_user("trader", "temporary-123")
    group = accounts.list_watchlists(user.id)[0]
    accounts.add_symbol(user.id, group.id, SymbolLookup(symbol="601872", name="招商轮船", market="17"))
    research = SQLiteResearchStore(tmp_path / "research.db")
    research.record_fund_flow(
        "601872",
        trade_date="20260910",
        minute="14:59",
        periods={"today": {"unit": "亿元", "main_net_inflow": "2.00", "main_visible_inflow": "1.20", "main_hidden_inflow": "0.80", "retail_inflow": "-2.00"}},
        captured_at=datetime.now(ZoneInfo("Asia/Shanghai")),
    )
    monkeypatch.setattr("level2_service.api.is_china_market_open", lambda: True)
    store = InMemoryStreams()
    app = create_app(
        store=store,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        research_store=research,
        symbol_lookup=lambda symbol: SymbolLookup(symbol=symbol, name="招商轮船", market="17"),
    )
    client = TestClient(app)
    public_id = client.post("/api/v1/jobs", json={"symbol": "601872", "include_long_capture": False}).json()["public_id"]
    store.transition(public_id, TaskStatus.RUNNING)
    store.complete_result(public_id, _values(), None)

    assert client.get(f"/api/v1/jobs/{public_id}/fund-flow-history").status_code == 404


def test_failed_protocol_timeout_closed_market_uses_previous_history(tmp_path, monkeypatch):
    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = accounts.create_user("trader", "temporary-123")
    group = accounts.list_watchlists(user.id)[0]
    accounts.add_symbol(user.id, group.id, SymbolLookup(symbol="601872", name="招商轮船", market="17"))
    research = SQLiteResearchStore(tmp_path / "research.db")
    research.record_fund_flow(
        "601872",
        trade_date="20260910",
        minute="14:59",
        periods={"today": {"unit": "亿元", "main_net_inflow": "2.00", "main_visible_inflow": "1.20", "main_hidden_inflow": "0.80", "retail_inflow": "-2.00"}},
        captured_at=datetime.now(ZoneInfo("Asia/Shanghai")),
    )
    monkeypatch.setattr("level2_service.api.is_china_market_open", lambda: False)
    store = InMemoryStreams()
    app = create_app(
        store=store,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        research_store=research,
        symbol_lookup=lambda symbol: SymbolLookup(symbol=symbol, name="招商轮船", market="17"),
    )
    client = TestClient(app)
    public_id = client.post("/api/v1/jobs", json={"symbol": "601872", "include_long_capture": False}).json()["public_id"]
    store.transition(public_id, TaskStatus.FAILED, error_code="DIRECT_PROTOCOL_RESPONSE_TIMEOUT")

    response = client.get(f"/api/v1/jobs/{public_id}/fund-flow-history")

    assert response.status_code == 200
    assert response.json()["trade_date"] == "20260910"


def test_failed_protocol_timeout_open_market_stays_hidden(tmp_path, monkeypatch):
    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = accounts.create_user("trader", "temporary-123")
    group = accounts.list_watchlists(user.id)[0]
    accounts.add_symbol(user.id, group.id, SymbolLookup(symbol="601872", name="招商轮船", market="17"))
    research = SQLiteResearchStore(tmp_path / "research.db")
    research.record_fund_flow(
        "601872",
        trade_date="20260910",
        minute="14:59",
        periods={"today": {"unit": "亿元", "main_net_inflow": "2.00", "main_visible_inflow": "1.20", "main_hidden_inflow": "0.80", "retail_inflow": "-2.00"}},
        captured_at=datetime.now(ZoneInfo("Asia/Shanghai")),
    )
    monkeypatch.setattr("level2_service.api.is_china_market_open", lambda: True)
    store = InMemoryStreams()
    app = create_app(
        store=store,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        research_store=research,
        symbol_lookup=lambda symbol: SymbolLookup(symbol=symbol, name="招商轮船", market="17"),
    )
    client = TestClient(app)
    public_id = client.post("/api/v1/jobs", json={"symbol": "601872", "include_long_capture": False}).json()["public_id"]
    store.transition(public_id, TaskStatus.FAILED, error_code="DIRECT_PROTOCOL_RESPONSE_TIMEOUT")

    assert client.get(f"/api/v1/jobs/{public_id}/fund-flow-history").status_code == 404
