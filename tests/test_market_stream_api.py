from datetime import datetime, timezone

from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from level2_service.api import create_app
from level2_service.market_accounts import InMemoryMarketSessionStore, SQLiteMarketAccountStore
from level2_service.market_data import KlineBar, MarketDataBroker, MarketSeriesPage, MarketSnapshot
from level2_service.parsed_values import SymbolLookup


class ApiMarketSource:
    def read_market_snapshot(self, symbol: str, *, detail: bool) -> MarketSnapshot:
        return MarketSnapshot(
            symbol=symbol,
            name="招商轮船",
            market="17",
            sequence=0,
            source_time="2026-08-24T09:31:02+08:00",
            collected_at=datetime.now(timezone.utc),
            quote={"current_price": "19.78", "change_percent": "+2.22%"},
            intraday_series={
                "macd_dif": {
                    "unit": None,
                    "points": [{"time": "09:31", "value": "+0.002"}],
                },
                "macd_dea": {
                    "unit": None,
                    "points": [{"time": "09:31", "value": "-0.005"}],
                },
                "macdfs": {
                    "unit": None,
                    "points": [{"time": "09:31", "value": "+0.012"}],
                }
            },
            capabilities={"order_book": {"available": True, "actual_depth": 10, "permission_limited": False}},
        )

    def read_market_series(self, symbol: str, period: str, cursor: str | None, limit: int) -> MarketSeriesPage:
        return MarketSeriesPage(
            symbol=symbol,
            period=period,
            bars=(KlineBar(time="2026-08-22", open="19.10", high="19.90", low="19.00", close="19.78", volume="10000", amount="200000"),),
            indicators={"ma5": ("19.22",)},
            next_cursor=None,
            adjustment="qfq",
            source="THS_PUBLIC",
            cached=True,
            stale=False,
            source_errors={"public_kline": None, "app_kline": None},
        )


def _authenticated_market_app(tmp_path):
    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = accounts.create_user("trader", "temporary-123")
    accounts.change_password(user.id, "temporary-123", "permanent-456")
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        secure_admin_cookies=False,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        market_data_broker=MarketDataBroker(ApiMarketSource(), is_market_open=lambda: True),
        symbol_lookup=lambda symbol: SymbolLookup(symbol=symbol, name="招商轮船", market="17"),
    )
    return app


def test_market_snapshot_and_kline_routes_require_user_authentication(tmp_path) -> None:
    app = _authenticated_market_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/v1/market/symbols/601872/snapshot").status_code == 401
        assert client.post("/api/v1/session", json={"username": "trader", "password": "permanent-456"}).status_code == 200

        snapshot = client.get("/api/v1/market/symbols/601872/snapshot")
        series = client.get("/api/v1/market/symbols/601872/series?period=day&limit=120")

    assert snapshot.status_code == 200
    assert snapshot.json()["quote"]["current_price"] == "19.78"
    assert snapshot.json()["intraday_series"]["macdfs"]["points"] == [
        {"time": "09:31", "value": "+0.012"}
    ]
    assert snapshot.json()["intraday_series"]["macd_dif"]["points"][0]["value"] == "+0.002"
    assert snapshot.json()["intraday_series"]["macd_dea"]["points"][0]["value"] == "-0.005"
    assert snapshot.json()["sequence"] == 1
    assert series.status_code == 200
    assert series.json()["bars"][0]["close"] == "19.78"
    body = series.json()
    assert {key: body[key] for key in (
        "adjustment", "source", "cached", "stale", "source_errors"
    )} == {
        "adjustment": "qfq",
        "source": "THS_PUBLIC",
        "cached": True,
        "stale": False,
        "source_errors": {"public_kline": None, "app_kline": None},
    }


def test_market_snapshot_redacts_arbitrary_upstream_exception_details(tmp_path) -> None:
    class FailingSource(ApiMarketSource):
        def read_market_snapshot(self, symbol: str, *, detail: bool) -> MarketSnapshot:
            raise RuntimeError("https://upstream.invalid/?token=private body=secret")

    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = accounts.create_user("trader", "temporary-123")
    accounts.change_password(user.id, "temporary-123", "permanent-456")
    app = create_app(
        secure_admin_cookies=False,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        market_data_broker=MarketDataBroker(FailingSource()),
        symbol_lookup=lambda symbol: SymbolLookup(symbol, "招商轮船", "17"),
    )

    with TestClient(app) as client:
        assert client.post(
            "/api/v1/session",
            json={"username": "trader", "password": "permanent-456"},
        ).status_code == 200
        response = client.get("/api/v1/market/symbols/601872/snapshot")

    assert response.status_code == 503
    assert response.json() == {"detail": "MARKET_QUOTE_UNAVAILABLE"}
    assert "private" not in response.text


def test_market_series_does_not_publish_source_value_error_text(tmp_path) -> None:
    class FailingSource(ApiMarketSource):
        def read_market_series(self, symbol, period, cursor, limit):
            raise ValueError("PRIVATE_SECRET_TOKEN")

    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = accounts.create_user("trader", "temporary-123")
    accounts.change_password(user.id, "temporary-123", "permanent-456")
    app = create_app(
        secure_admin_cookies=False,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        market_data_broker=MarketDataBroker(FailingSource()),
        symbol_lookup=lambda symbol: SymbolLookup(symbol, "招商轮船", "17"),
    )

    with TestClient(app) as client:
        assert client.post(
            "/api/v1/session",
            json={"username": "trader", "password": "permanent-456"},
        ).status_code == 200
        response = client.get(
            "/api/v1/market/symbols/601872/series?period=day&limit=120"
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "MARKET_SERIES_UNAVAILABLE"}
    assert "PRIVATE_SECRET_TOKEN" not in response.text


def test_market_websocket_authenticates_and_pushes_subscribed_snapshots(tmp_path) -> None:
    app = _authenticated_market_app(tmp_path)
    with TestClient(app) as client:
        unauthenticated = client.websocket_connect("/api/v1/market/stream")
        try:
            unauthenticated.__enter__()
            raise AssertionError("unauthenticated websocket unexpectedly connected")
        except Exception:
            pass

        assert client.post("/api/v1/session", json={"username": "trader", "password": "permanent-456"}).status_code == 200
        with client.websocket_connect("/api/v1/market/stream") as websocket:
            websocket.send_json({"type": "subscribe", "watchlist": [], "detail": "601872"})
            subscribed = websocket.receive_json()
            snapshot = websocket.receive_json()

    assert subscribed == {"type": "subscribed", "watchlist": [], "detail": "601872"}
    assert snapshot["type"] == "snapshot"
    assert snapshot["data"]["symbol"] == "601872"
    assert snapshot["data"]["intraday_series"]["macdfs"]["points"][0]["value"] == "+0.012"


def test_admin_market_health_reports_broker_cadence_and_cache(tmp_path) -> None:
    app = _authenticated_market_app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
        health = client.get("/api/admin/market")

    assert health.status_code == 200
    assert health.json() == {
        "market_open": True,
        "subscribers": 0,
        "subscribed_symbols": 0,
        "cached_symbols": 0,
        "detail_interval_seconds": 2.0,
        "watchlist_interval_seconds": 15.0,
        "closed_interval_seconds": 60.0,
    }
