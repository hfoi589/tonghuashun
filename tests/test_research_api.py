from __future__ import annotations

from datetime import datetime, timezone

from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from level2_service.api import create_app
from level2_service.market_accounts import InMemoryMarketSessionStore, SQLiteMarketAccountStore
from level2_service.market_data import MarketSnapshot, TimesharePoint
from level2_service.parsed_values import SymbolLookup
from level2_service.research_store import SQLiteResearchStore


class Broker:
    async def refresh(self, symbol: str, *, detail: bool, max_age_seconds: float = 0):
        assert detail is True
        return MarketSnapshot(
            symbol=symbol,
            name="招商轮船",
            market="17",
            sequence=1,
            source_time="20260831 09:31:00",
            collected_at=datetime(2026, 8, 31, 1, 31, tzinfo=timezone.utc),
            source="TENCENT_PUBLIC",
            quote={"pre_close": "10.00"},
            timeshare=(
                TimesharePoint(time="09:30", price="10.00"),
                TimesharePoint(time="09:31", price="10.20"),
            ),
        )

    def stats(self):
        return {}


class Dispatcher:
    def __init__(self):
        self.calls = []

    def send(self, config, title, body):
        self.calls.append((config, title, body))
        return {"ok": True, "channels": {"bark": {"sent": 1}}, "errors": {}}


def client(tmp_path):
    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    research = SQLiteResearchStore(tmp_path / "research.db")
    dispatcher = Dispatcher()
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        secure_admin_cookies=False,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        market_data_broker=Broker(),
        research_store=research,
        push_dispatcher=dispatcher,
        symbol_lookup=lambda symbol: SymbolLookup(symbol=symbol, name="招商轮船", market="17"),
    )
    return TestClient(app), dispatcher


def login_market(client: TestClient):
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    assert client.post(
        "/api/admin/users",
        headers={"X-CSRF-Token": client.cookies.get("ths_csrf")},
        json={"username": "trader", "temporary_password": "temporary-123"},
    ).status_code == 201
    client.post(
        "/api/admin/session/logout",
        headers={"X-CSRF-Token": client.cookies.get("ths_csrf")},
    )
    assert client.post(
        "/api/v1/session",
        json={"username": "trader", "password": "temporary-123"},
    ).status_code == 200
    assert client.post(
        "/api/v1/session/password",
        headers={"X-CSRF-Token": client.cookies.get("ths_market_csrf")},
        json={
            "current_password": "temporary-123",
            "new_password": "permanent-456",
            "new_password_confirmation": "permanent-456",
        },
    ).status_code == 204
    group_id = client.get("/api/v1/watchlists").json()["groups"][0]["id"]
    assert client.post(
        f"/api/v1/watchlists/groups/{group_id}/symbols",
        headers={"X-CSRF-Token": client.cookies.get("ths_market_csrf")},
        json={"symbol": "601872"},
    ).status_code == 201


def test_market_user_backfills_and_reads_replay_series(tmp_path):
    web, _dispatcher = client(tmp_path)
    login_market(web)

    collected = web.post(
        "/api/v1/monitoring/symbols/601872/backfill",
        headers={"X-CSRF-Token": web.cookies.get("ths_market_csrf")},
    )
    replay = web.get("/api/v1/replay/601872?trade_date=20260831")

    assert collected.status_code == 200
    assert collected.json()["changed_minutes"] == ["09:30", "09:31"]
    assert replay.status_code == 200
    assert replay.json()["source"] == "TENCENT_PUBLIC"
    assert len(replay.json()["points"]) == 2

    legacy_watchlist = web.get("/api/watchlist")
    legacy_series = web.get("/api/series?code=601872&date=20260831")
    assert legacy_watchlist.status_code == 200
    assert legacy_watchlist.json()["items"][0]["code"] == "601872"
    assert legacy_series.status_code == 200
    assert legacy_series.json()["code"] == "601872"
    legacy_toggle = web.post(
        "/api/watchlist",
        headers={"X-CSRF-Token": web.cookies.get("ths_market_csrf")},
        json={"action": "deactivate", "code": "601872"},
    )
    assert legacy_toggle.status_code == 200
    assert legacy_toggle.json()["items"][0]["active"] == 0

    web.patch(
        "/api/v1/monitoring/symbols/601872",
        headers={"X-CSRF-Token": web.cookies.get("ths_market_csrf")},
        json={"enabled": True},
    )
    portfolio = web.get("/api/v1/monitoring/portfolio")
    assert portfolio.status_code == 200
    assert portfolio.json()["items"][0]["symbol"] == "601872"
    assert portfolio.json()["items"][0]["latest_price"] == "10.20"


def test_admin_reads_and_updates_dynamic_macd_settings(tmp_path):
    web, _dispatcher = client(tmp_path)
    assert web.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204

    updated = web.put(
        "/api/admin/research/macd",
        headers={"X-CSRF-Token": web.cookies.get("ths_csrf")},
        json={"short": 8, "long": 17, "signal": 5, "marker_threshold": 0.002},
    )

    assert updated.status_code == 200
    assert web.get("/api/admin/research/macd").json() == {
        "short": 8,
        "long": 17,
        "signal": 5,
        "marker_threshold": 0.002,
    }


def test_admin_configures_all_push_channels_and_market_user_runs_rule_replay(tmp_path):
    web, dispatcher = client(tmp_path)
    assert web.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    config = {
        "enabled": True,
        "premium_push_enabled": True,
        "bark_groups": [{"id": "phone", "name": "手机", "base_url": "https://api.day.app", "device_key": "device"}],
        "sc3_bot": {"enabled": True, "base_url": "https://bot.example", "token": "token", "chat_id": "7"},
        "wecom": {"enabled": True, "api_base_url": "https://qyapi.weixin.qq.com", "corp_id": "corp", "corp_secret": "secret", "agent_id": 1, "to_user": "@all"},
        "rules": [{
            "id": "price-buy", "enabled": True, "side": "buy", "title": "价格买点", "joiner": "and",
            "conditions": [{"type": "price_threshold", "direction": "gte", "value": 10.1}],
        }],
    }
    assert web.put(
        "/api/admin/push/config",
        headers={"X-CSRF-Token": web.cookies.get("ths_csrf")},
        json=config,
    ).status_code == 200
    public = web.get("/api/admin/push/config").json()
    assert public["bark_groups"][0]["device_key"] == ""
    assert web.post(
        "/api/admin/push/test",
        headers={"X-CSRF-Token": web.cookies.get("ths_csrf")},
    ).status_code == 200
    assert dispatcher.calls

    web.post(
        "/api/admin/session/logout",
        headers={"X-CSRF-Token": web.cookies.get("ths_csrf")},
    )
    login_market(web)
    assert web.post(
        "/api/v1/monitoring/symbols/601872/backfill",
        headers={"X-CSRF-Token": web.cookies.get("ths_market_csrf")},
    ).status_code == 200

    replay = web.post(
        "/api/v1/replay/601872/evaluate",
        headers={"X-CSRF-Token": web.cookies.get("ths_market_csrf")},
        json={"trade_date": "20260831", "rule_ids": ["price-buy"]},
    )

    assert replay.status_code == 200
    assert replay.json()["markers"][0]["time"] == "09:31"
    assert replay.json()["performance"]["forced_exit"]["time"] == "09:31"


def test_premium_snapshot_is_visible_in_market_and_manually_pushable_by_admin(tmp_path):
    web, dispatcher = client(tmp_path)
    web.app.state.research_store.save_premium_snapshot({
        "as_of": "2026-08-31T09:31:00+08:00",
        "rows": [{"code": "160723", "name": "嘉实原油", "current_price": 1.2, "estimated_price": 1.0, "premium_rate": 0.2}],
        "valid_count": 1,
        "errors": {},
    })
    web.app.state.research_store.save_push_config({
        "enabled": True,
        "bark_groups": [{"id": "phone", "name": "手机", "base_url": "https://api.day.app", "device_key": "device"}],
        "rules": [],
    })
    login_market(web)

    premium = web.get("/api/v1/monitoring/premium")
    assert premium.status_code == 200
    assert premium.json()["rows"][0]["premium_rate"] == 0.2
    web.delete("/api/v1/session", headers={"X-CSRF-Token": web.cookies.get("ths_market_csrf")})
    assert web.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    pushed = web.post("/api/admin/premium/push", headers={"X-CSRF-Token": web.cookies.get("ths_csrf")})
    assert pushed.status_code == 200
    assert dispatcher.calls[-1][1] == "QDII/LOF 溢价估算"
