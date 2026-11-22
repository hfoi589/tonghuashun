from __future__ import annotations

from datetime import datetime, timezone

from argon2 import PasswordHasher
from fastapi.testclient import TestClient
import pytest

from level2_service.api import create_app
from level2_service.main import DeploymentSettings
from level2_service.market_accounts import (
    MONITOR_LIMIT_MESSAGE,
    SQLiteMarketAccountStore,
)
from level2_service.market_data import MarketPhase, MarketSnapshot
from level2_service.parsed_values import SymbolLookup
from level2_service.research_store import SQLiteResearchStore


TOKEN = "goodluck-bridge-secret-token"

CATALOG = {
    "601872": SymbolLookup(symbol="601872", name="招商轮船", market="17"),
    "600036": SymbolLookup(symbol="600036", name="招商银行", market="17"),
    "300750": SymbolLookup(symbol="300750", name="宁德时代", market="33"),
}


class FakeMarketBroker:
    def __init__(self, snapshots: dict[str, MarketSnapshot] | None = None, *, market_open: bool = True) -> None:
        self.snapshots = dict(snapshots or {})
        self.market_open = market_open
        self.refresh_calls: list[tuple[str, bool, float]] = []

    def is_market_open(self) -> bool:
        return self.market_open

    def cached_snapshot(self, symbol: str) -> MarketSnapshot | None:
        return self.snapshots.get(symbol)

    async def refresh(
        self,
        symbol: str,
        *,
        detail: bool,
        max_age_seconds: float = 0,
    ) -> MarketSnapshot:
        self.refresh_calls.append((symbol, detail, max_age_seconds))
        if symbol not in self.snapshots:
            raise RuntimeError("MARKET_QUOTE_UNAVAILABLE")
        return self.snapshots[symbol]


def _make_snapshot(
    symbol: str,
    *,
    with_l2: bool = True,
    with_fund_flow: bool = True,
) -> MarketSnapshot:
    item = CATALOG[symbol]
    quote = {
        "price": "7.12",
        "change_percent": "+1.42%",
        "turnover_rate": "1.28%",
        "large_order_net": "0.18" if with_l2 else None,
        "large_order_amount": "1234.5" if with_l2 else None,
        "retail_count": "-0.34" if with_l2 else None,
        "macdfs": "+0.012" if with_l2 else None,
    }
    main_fund_flow = (
        {
            "today": {
                "unit": "万元",
                "main_net_inflow": "520.10",
                "main_visible_inflow": "320.00",
                "main_hidden_inflow": "200.10",
                "retail_inflow": "-520.10",
            },
            "three_day": {
                "unit": "亿元",
                "main_net_inflow": "1.20",
                "main_visible_inflow": "0.70",
                "main_hidden_inflow": "0.50",
                "retail_inflow": "-1.20",
            },
            "five_day": {
                "unit": "亿元",
                "main_net_inflow": "2.40",
                "main_visible_inflow": "1.50",
                "main_hidden_inflow": "0.90",
                "retail_inflow": "-2.40",
            },
        }
        if with_fund_flow
        else {}
    )
    return MarketSnapshot(
        symbol=symbol,
        name=item.name,
        market=item.market,
        sequence=1,
        source_time="14:55",
        collected_at=datetime.now(timezone.utc),
        quote=quote,
        source="TENCENT_PUBLIC",
        market_phase=MarketPhase.CONTINUOUS,
        price_precision=2,
        main_fund_flow=main_fund_flow,
        capabilities={"l2": {"available": with_l2, "reason": None if with_l2 else "DIRECT_SESSION_UNAVAILABLE"}},
        source_errors={"core_metrics": None if with_l2 else "DIRECT_SESSION_UNAVAILABLE"},
    )


def _headers(user: str = "uid:alice", role: str = "user", token: str = TOKEN) -> dict[str, str]:
    return {
        "X-Goodluck-Internal-Token": token,
        "X-Goodluck-User": user,
        "X-Goodluck-Role": role,
    }


def _build_app(
    tmp_path,
    *,
    token: str | None = TOKEN,
    max_total_symbols: int = 50,
    broker: FakeMarketBroker | None = None,
):
    accounts = SQLiteMarketAccountStore(tmp_path / "market.db", max_total_symbols=max_total_symbols)
    store = SQLiteResearchStore(tmp_path / "research.db")

    def lookup(symbol: str) -> SymbolLookup:
        if symbol not in CATALOG:
            from level2_service.parsed_values import SymbolLookupNotFoundError
            raise SymbolLookupNotFoundError(symbol)
        return CATALOG[symbol]

    def search(query: str, limit: int) -> list[SymbolLookup]:
        q = query.strip()
        results = [
            item
            for item in CATALOG.values()
            if q in item.symbol or q in item.name
        ]
        return results[:limit]

    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        secure_admin_cookies=False,
        market_account_store=accounts,
        research_store=store,
        market_data_broker=broker,
        symbol_lookup=lookup,
        symbol_search=search,
        goodluck_internal_token=token,
    )
    return app, accounts, store


def test_bridge_auth_validation(tmp_path) -> None:
    unconfigured_app, _, _ = _build_app(tmp_path / "unconfigured", token=None)
    with TestClient(unconfigured_app) as client:
        resp = client.get("/api/internal/goodluck/watchlists", headers=_headers())
        assert resp.status_code == 503

    app, _, _ = _build_app(tmp_path / "configured", token=TOKEN)
    with TestClient(app) as client:
        assert client.get("/api/internal/goodluck/watchlists").status_code == 401
        assert client.get("/api/internal/goodluck/watchlists", headers=_headers(token="wrong")).status_code == 401
        assert client.get(
            "/api/internal/goodluck/watchlists",
            headers={"X-Goodluck-Internal-Token": TOKEN, "X-Goodluck-User": "   "},
        ).status_code == 400


def test_bridge_user_isolation_groups_and_global_limit(tmp_path) -> None:
    broker = FakeMarketBroker({"601872": _make_snapshot("601872"), "600036": _make_snapshot("600036")})
    app, accounts, _ = _build_app(tmp_path, max_total_symbols=2, broker=broker)

    with TestClient(app) as client:
        # 搜索与精确代码确认
        search_res = client.get("/api/internal/goodluck/symbols?query=招商", headers=_headers("uid:alice"))
        assert search_res.status_code == 200
        assert {item["symbol"] for item in search_res.json()} == {"601872", "600036"}

        lookup_res = client.get("/api/internal/goodluck/symbols/601872", headers=_headers("uid:alice"))
        assert lookup_res.status_code == 200
        assert lookup_res.json()["name"] == "招商轮船"

        # Alice 获取默认自选分组，创建自定义分组并添加股票
        alice_wl = client.get("/api/internal/goodluck/watchlists", headers=_headers("uid:alice")).json()
        assert len(alice_wl["groups"]) == 1
        assert alice_wl["groups"][0]["name"] == "自选"
        assert alice_wl["groups"][0]["is_primary"] is True
        alice_primary_id = alice_wl["groups"][0]["id"]

        created_group = client.post(
            "/api/internal/goodluck/watchlists/groups",
            headers=_headers("uid:alice"),
            json={"name": "航运"},
        )
        assert created_group.status_code == 201
        alice_custom_id = created_group.json()["id"]

        renamed = client.patch(
            f"/api/internal/goodluck/watchlists/groups/{alice_custom_id}",
            headers=_headers("uid:alice"),
            json={"name": "油运核心"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "油运核心"

        assert client.put(
            "/api/internal/goodluck/watchlists/groups/order",
            headers=_headers("uid:alice"),
            json={"group_ids": [alice_custom_id, alice_primary_id]},
        ).status_code == 204

        add1 = client.post(
            f"/api/internal/goodluck/watchlists/groups/{alice_custom_id}/symbols",
            headers=_headers("uid:alice"),
            json={"symbol": "601872"},
        )
        assert add1.status_code == 201
        assert add1.json()["monitoring_enabled"] is True

        add2 = client.post(
            f"/api/internal/goodluck/watchlists/groups/{alice_primary_id}/symbols",
            headers=_headers("uid:alice"),
            json={"symbol": "600036"},
        )
        assert add2.status_code == 201

        # Alice 的两个分组数据校验：自定义分组添加的 601872 同步到了主分组
        alice_after = client.get("/api/internal/goodluck/watchlists", headers=_headers("uid:alice")).json()
        by_name = {group["name"]: [i["symbol"] for i in group["items"]] for group in alice_after["groups"]}
        assert by_name["油运核心"] == ["601872"]
        assert set(by_name["自选"]) == {"601872", "600036"}
        assert alice_after["limits"] == {"user_count": 2, "user_max": 50, "global_count": 2, "global_max": 2}

        # Bob 独立自选分组；Bob 添加全站已存在的 601872 成功，添加新股票 300750 触发全站上限 2
        bob_wl = client.get("/api/internal/goodluck/watchlists", headers=_headers("uid:bob")).json()
        assert len(bob_wl["groups"]) == 1
        assert bob_wl["groups"][0]["items"] == []
        bob_primary_id = bob_wl["groups"][0]["id"]

        assert client.post(
            f"/api/internal/goodluck/watchlists/groups/{bob_primary_id}/symbols",
            headers=_headers("uid:bob"),
            json={"symbol": "601872"},
        ).status_code == 201

        limit_hit = client.post(
            f"/api/internal/goodluck/watchlists/groups/{bob_primary_id}/symbols",
            headers=_headers("uid:bob"),
            json={"symbol": "300750"},
        )
        assert limit_hit.status_code == 409
        assert limit_hit.json()["detail"] == MONITOR_LIMIT_MESSAGE

        # 后台监控合并两用户自选并集
        assert accounts.list_monitored_symbols() == ["600036", "601872"]
        assert accounts.list_watchlist_symbols() == ["600036", "601872"]

        # 删除自定义分组内标的：只从自定义分组移除，主分组保留
        assert client.delete(
            f"/api/internal/goodluck/watchlists/groups/{alice_custom_id}/symbols/601872",
            headers=_headers("uid:alice"),
        ).status_code == 204
        alice_after_custom_del = client.get("/api/internal/goodluck/watchlists", headers=_headers("uid:alice")).json()
        by_name_del = {group["name"]: [i["symbol"] for i in group["items"]] for group in alice_after_custom_del["groups"]}
        assert by_name_del["油运核心"] == []
        assert "601872" in by_name_del["自选"]

        # 删除主分组内标的：彻底从该用户所有分组移除；Bob 仍持有 601872
        assert client.delete(
            f"/api/internal/goodluck/watchlists/groups/{alice_primary_id}/symbols/601872",
            headers=_headers("uid:alice"),
        ).status_code == 204
        alice_final = client.get("/api/internal/goodluck/watchlists", headers=_headers("uid:alice")).json()
        assert all("601872" not in [i["symbol"] for i in g["items"]] for g in alice_final["groups"])
        assert accounts.list_monitored_symbols() == ["600036", "601872"]

        # 删除自定义分组
        assert client.delete(
            f"/api/internal/goodluck/watchlists/groups/{alice_custom_id}",
            headers=_headers("uid:alice"),
        ).status_code == 204
        # 删除主分组被拒绝
        assert client.delete(
            f"/api/internal/goodluck/watchlists/groups/{alice_primary_id}",
            headers=_headers("uid:alice"),
        ).status_code == 409


def test_bridge_quotes_snapshot_history_daily_monitoring_and_admin(tmp_path) -> None:
    broker = FakeMarketBroker(
        {
            "601872": _make_snapshot("601872", with_l2=True, with_fund_flow=True),
            "600036": _make_snapshot("600036", with_l2=False, with_fund_flow=False),
        }
    )
    app, accounts, store = _build_app(tmp_path, broker=broker)
    store.record_fund_flow(
        "601872",
        trade_date="20261120",
        minute="14:55",
        periods={
            "today": {
                "unit": "万元",
                "main_net_inflow": "520.10",
                "main_visible_inflow": "320.00",
                "main_hidden_inflow": "200.10",
                "retail_inflow": "-520.10",
            },
            "three_day": {
                "unit": "亿元",
                "main_net_inflow": "1.20",
                "main_visible_inflow": "0.70",
                "main_hidden_inflow": "0.50",
                "retail_inflow": "-1.20",
            },
            "five_day": {
                "unit": "亿元",
                "main_net_inflow": "2.40",
                "main_visible_inflow": "1.50",
                "main_hidden_inflow": "0.90",
                "retail_inflow": "-2.40",
            },
        },
        captured_at=datetime(2026, 11, 20, 14, 55, tzinfo=timezone.utc),
    )

    with TestClient(app) as client:
        wl = client.get("/api/internal/goodluck/watchlists", headers=_headers("uid:alice")).json()
        primary_id = wl["groups"][0]["id"]
        assert client.post(
            f"/api/internal/goodluck/watchlists/groups/{primary_id}/symbols",
            headers=_headers("uid:alice"),
            json={"symbol": "601872"},
        ).status_code == 201
        assert client.post(
            f"/api/internal/goodluck/watchlists/groups/{primary_id}/symbols",
            headers=_headers("uid:alice"),
            json={"symbol": "600036"},
        ).status_code == 201

        # 加股首采至少触发过一次 refresh
        assert any(sym == "601872" and detail is True for sym, detail, _ in broker.refresh_calls)

        # 批量报价与暗盘摘要
        quotes = client.get(
            "/api/internal/goodluck/quotes?symbols=601872,600036,300750",
            headers=_headers("uid:alice"),
        )
        assert quotes.status_code == 200
        q_items = quotes.json()["items"]
        assert set(q_items.keys()) == {"601872", "600036"}
        assert q_items["601872"]["today_main_hidden_inflow"] == "200.10"
        assert q_items["601872"]["today_unit"] == "万元"
        assert q_items["600036"]["today_main_hidden_inflow"] is None

        # 单股详情快照（8项指标 + main_fund_flow，缺失指标保持 null）
        snap_ok = client.get("/api/internal/goodluck/symbols/601872/snapshot", headers=_headers("uid:alice"))
        assert snap_ok.status_code == 200
        body = snap_ok.json()
        assert body["metrics"] == {
            "stock_name": "招商轮船",
            "current_price": "7.12",
            "change_percent": "+1.42%",
            "turnover_rate": "1.28%",
            "large_order_net": "0.18",
            "large_order_amount": "1234.5",
            "retail_count": "-0.34",
            "macdfs": "+0.012",
        }
        assert body["main_fund_flow"]["today"]["main_hidden_inflow"] == "200.10"

        snap_partial = client.get("/api/internal/goodluck/symbols/600036/snapshot", headers=_headers("uid:alice"))
        assert snap_partial.status_code == 200
        partial_metrics = snap_partial.json()["metrics"]
        assert partial_metrics["current_price"] == "7.12"
        assert partial_metrics["large_order_net"] is None
        assert partial_metrics["macdfs"] is None

        # 未加入自选的标的不可读详情
        assert client.get("/api/internal/goodluck/symbols/300750/snapshot", headers=_headers("uid:alice")).status_code == 404

        # 分钟暗盘曲线与近30日暗盘流向
        history = client.get("/api/internal/goodluck/symbols/601872/fund-flow/history", headers=_headers("uid:alice"))
        assert history.status_code == 200
        assert history.json()["trade_date"] == "20261120"
        assert history.json()["name"] == "招商轮船"
        assert history.json()["periods"]["today"]["points"][0]["main_hidden_inflow"] == "200.10"

        daily = client.get("/api/internal/goodluck/symbols/601872/fund-flow/daily?limit=30", headers=_headers("uid:alice"))
        assert daily.status_code == 200
        assert len(daily.json()["points"]) == 2  # baseline + 20261120

        # 启停监控与补采
        paused = client.patch(
            "/api/internal/goodluck/monitoring/symbols/601872",
            headers=_headers("uid:alice"),
            json={"enabled": False},
        )
        assert paused.status_code == 200
        assert paused.json()["enabled"] is False
        assert "601872" not in accounts.list_monitored_symbols()

        resumed = client.patch(
            "/api/internal/goodluck/monitoring/symbols/601872",
            headers=_headers("uid:alice"),
            json={"enabled": True},
        )
        assert resumed.status_code == 200
        assert "601872" in accounts.list_monitored_symbols()

        backfill = client.post(
            "/api/internal/goodluck/monitoring/symbols/601872/backfill",
            headers=_headers("uid:alice"),
        )
        assert backfill.status_code == 200
        assert backfill.json()["symbol"] == "601872"

        # 管理员状态接口鉴权
        assert client.get("/api/internal/goodluck/admin/status", headers=_headers("uid:alice", "user")).status_code == 403
        admin_res = client.get("/api/internal/goodluck/admin/status", headers=_headers("uid:admin", "admin"))
        assert admin_res.status_code == 200
        assert admin_res.json()["global_count"] == 2
        assert admin_res.json()["monitored_count"] == 2


def test_deployment_settings_reads_and_hides_goodluck_internal_token() -> None:
    secret = "very-secret-goodluck-token-value"
    settings = DeploymentSettings.from_environ(
        {
            "ADMIN_PASSWORD_HASH": "$argon2id$example",
            "ADMIN_SESSION_SECRET": "s" * 32,
            "ADB_SERIAL": "emulator-5556",
            "GOODLUCK_INTERNAL_TOKEN": f"  {secret}  ",
        }
    )

    assert settings.goodluck_internal_token == secret
    assert secret not in repr(settings)
