from __future__ import annotations

from datetime import datetime

from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from level2_service.api import create_app
from level2_service.market_accounts import InMemoryMarketSessionStore, SQLiteMarketAccountStore
from level2_service.parsed_values import SymbolLookup
from level2_service.research_store import SQLiteResearchStore


def test_fund_flow_history_requires_watchlist_and_returns_date_periods(tmp_path):
    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = accounts.create_user("trader", "temporary-123")
    accounts.change_password(user.id, "temporary-123", "permanent-456")
    group = accounts.list_watchlists(user.id)[0]
    accounts.add_symbol(user.id, group.id, SymbolLookup(symbol="601872", name="招商轮船", market="17"))
    store = SQLiteResearchStore(tmp_path / "research.db")
    store.record_fund_flow(
        "601872",
        trade_date="20260909",
        minute="10:30",
        periods={
            "today": {
                "unit": "万元",
                "main_net_inflow": "100.00",
                "main_visible_inflow": "80.00",
                "main_hidden_inflow": "20.00",
                "retail_inflow": "-100.00",
            },
            "three_day": {"unit": "亿元", "main_visible_inflow": "1.00"},
            "five_day": {"unit": "亿元", "retail_inflow": "-2.00"},
        },
        captured_at=datetime(2026, 9, 9, 10, 30),
    )
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        secure_admin_cookies=False,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        research_store=store,
        symbol_lookup=lambda symbol: SymbolLookup(symbol=symbol, name="招商轮船", market="17"),
    )

    with TestClient(app) as client:
        assert client.post("/api/v1/session", json={"username": "trader", "password": "permanent-456"}).status_code == 200
        response = client.get("/api/v1/market/symbols/601872/fund-flow/history")

    assert response.status_code == 200
    body = response.json()
    assert body["trade_date"] == "20260909"
    assert body["available_dates"] == ["20260909"]
    assert body["periods"]["today"]["points"][0]["unit"] == "万元"
    assert body["periods"]["three_day"]["points"][0]["main_visible_inflow"] == "1.00"


def test_fund_flow_history_rejects_symbol_outside_user_watchlist(tmp_path):
    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = accounts.create_user("trader", "temporary-123")
    accounts.change_password(user.id, "temporary-123", "permanent-456")
    store = SQLiteResearchStore(tmp_path / "research.db")
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        secure_admin_cookies=False,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        research_store=store,
        symbol_lookup=lambda symbol: SymbolLookup(symbol=symbol, name="招商轮船", market="17"),
    )

    with TestClient(app) as client:
        client.post("/api/v1/session", json={"username": "trader", "password": "permanent-456"})
        response = client.get("/api/v1/market/symbols/601872/fund-flow/history")

    assert response.status_code == 404


def test_fund_flow_history_uses_stored_watchlist_identity_when_catalog_is_stale(tmp_path):
    from level2_service.parsed_values import DirectRequestError

    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = accounts.create_user("trader", "temporary-123")
    accounts.change_password(user.id, "temporary-123", "permanent-456")
    group = accounts.list_watchlists(user.id)[0]
    accounts.add_symbol(
        user.id,
        group.id,
        SymbolLookup(symbol="601872", name="招商轮船", market="17"),
    )
    store = SQLiteResearchStore(tmp_path / "research.db")
    store.record_fund_flow(
        "601872",
        trade_date="20260914",
        minute="15:00",
        periods={
            "today": {
                "unit": "亿元",
                "main_net_inflow": "-1.42",
                "main_visible_inflow": "-0.92",
                "main_hidden_inflow": "-0.50",
                "retail_inflow": "1.42",
            }
        },
        captured_at=datetime(2026, 9, 14, 15, 0),
    )

    def stale_lookup(_symbol: str):
        raise DirectRequestError("SYMBOL_CATALOG_STALE")

    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        secure_admin_cookies=False,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        research_store=store,
        symbol_lookup=stale_lookup,
    )

    with TestClient(app) as client:
        client.post(
            "/api/v1/session",
            json={"username": "trader", "password": "permanent-456"},
        )
        response = client.get(
            "/api/v1/market/symbols/601872/fund-flow/history"
        )

    assert response.status_code == 200
    assert response.json()["name"] == "招商轮船"
    assert response.json()["trade_date"] == "20260914"


def test_fund_flow_daily_returns_the_last_today_point_for_the_latest_30_trade_dates(tmp_path):
    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = accounts.create_user("trader", "temporary-123")
    accounts.change_password(user.id, "temporary-123", "permanent-456")
    group = accounts.list_watchlists(user.id)[0]
    accounts.add_symbol(user.id, group.id, SymbolLookup(symbol="601872", name="招商轮船", market="17"))
    store = SQLiteResearchStore(tmp_path / "research.db")
    for day in range(1, 32):
        trade_date = f"202608{day:02d}"
        store.record_fund_flow(
            "601872",
            trade_date=trade_date,
            minute="14:59",
            periods={
                "today": {"unit": "万元", "main_net_inflow": f"{day}.00", "main_visible_inflow": "1.00", "main_hidden_inflow": "2.00", "retail_inflow": f"-{day}.00"},
                "three_day": {"unit": "万元", "main_net_inflow": "9999.00"},
            },
            captured_at=datetime(2026, 8, day, 14, 59),
        )
        store.record_fund_flow(
            "601872",
            trade_date=trade_date,
            minute="15:00",
            periods={
                "today": {"unit": "万元", "main_net_inflow": f"{day + 100}.00", "main_visible_inflow": "3.00", "main_hidden_inflow": "4.00", "retail_inflow": f"-{day + 100}.00"},
            },
            captured_at=datetime(2026, 8, day, 15, 0),
        )
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        secure_admin_cookies=False,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        research_store=store,
        symbol_lookup=lambda symbol: SymbolLookup(symbol=symbol, name="招商轮船", market="17"),
    )

    with TestClient(app) as client:
        client.post("/api/v1/session", json={"username": "trader", "password": "permanent-456"})
        response = client.get("/api/v1/market/symbols/601872/fund-flow/daily?limit=30")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "招商轮船"
    assert len(body["points"]) == 31
    assert body["baseline_trade_date"] == "20260731"
    assert body["points"][0] == {
        "trade_date": "20260731",
        "time": "00:00",
        "unit": "万元",
        "main_net_inflow": "0",
        "main_visible_inflow": "0",
        "main_hidden_inflow": "0",
        "retail_inflow": "0",
        "periods": None,
    }
    assert body["points"][1]["trade_date"] == "20260802"
    assert body["points"][1]["main_net_inflow"] == "102.00"
    assert body["points"][-1] == {
        "trade_date": "20260831",
        "time": "15:00",
        "unit": "万元",
        "main_net_inflow": "131.00",
            "main_visible_inflow": "3.00",
            "main_hidden_inflow": "4.00",
            "retail_inflow": "-131.00",
            "periods": {
                "today": {
                    "unit": "万元",
                    "main_net_inflow": "131.00",
                    "main_visible_inflow": "3.00",
                    "main_hidden_inflow": "4.00",
                    "retail_inflow": "-131.00",
                },
                "three_day": {
                    "unit": "万元",
                    "main_net_inflow": "9999.00",
                    "main_visible_inflow": None,
                    "main_hidden_inflow": None,
                    "retail_inflow": None,
                },
                "five_day": None,
            },
        }
