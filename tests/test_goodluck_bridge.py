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
    "160723": SymbolLookup(symbol="160723", name="嘉实原油", market="36"),
    "161129": SymbolLookup(symbol="161129", name="原油LOF易方达", market="36"),
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
    expected_intraday_trade_date=None,
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
        expected_intraday_trade_date=expected_intraday_trade_date,
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

        # 单股详情快照（含开高低与成交额 + 8项指标 + main_fund_flow，缺失指标保持 null）
        snap_ok = client.get("/api/internal/goodluck/symbols/601872/snapshot", headers=_headers("uid:alice"))
        assert snap_ok.status_code == 200
        body = snap_ok.json()
        assert body["metrics"] == {
            "stock_name": "招商轮船",
            "current_price": "7.12",
            "change_percent": "+1.42%",
            "turnover_rate": "1.28%",
            "open": None,
            "high": None,
            "low": None,
            "amount": None,
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


def test_bridge_admin_only_intraday_replay_and_premium(tmp_path) -> None:
    from level2_service.market_data import TimesharePoint

    snap = _make_snapshot("601872")
    snap = MarketSnapshot(
        symbol="601872",
        name="招商轮船",
        market="17",
        sequence=2,
        source_time="20261123 09:35:00",
        collected_at=datetime(2026, 11, 23, 1, 35, tzinfo=timezone.utc),
        quote={**snap.quote, "previous_close": "7.00"},
        source="TENCENT_PUBLIC",
        market_phase=MarketPhase.CONTINUOUS,
        price_precision=2,
        timeshare=(
            TimesharePoint(time="09:30", price="7.02", average_price="7.01", volume="10000"),
            TimesharePoint(time="09:31", price="7.08", average_price="7.04", volume="15000"),
            TimesharePoint(time="09:32", price="7.12", average_price="7.07", volume="18000"),
        ),
        main_fund_flow=snap.main_fund_flow,
    )
    broker = FakeMarketBroker({"601872": snap}, market_open=True)
    app, _accounts, store = _build_app(tmp_path, broker=broker)
    store.save_premium_snapshot(
        {
            "as_of": "2026-11-23T09:35:00+08:00",
            "valid_count": 1,
            "rows": [
                {
                    "code": "160723",
                    "name": "嘉实原油",
                    "current_price": 1.452,
                    "estimated_price": 1.4102,
                    "premium_rate": 0.0296,
                    "estimate_source": "实时估值",
                    "estimate_date": "2026-11-23 09:35",
                    "stale": False,
                    "error": None,
                }
            ],
            "errors": {},
        }
    )

    with TestClient(app) as client:
        # 普通用户可读油气溢价，但禁止访问分时监控与管理刷新
        prem_user = client.get("/api/internal/goodluck/premium", headers=_headers("uid:alice", "user"))
        assert prem_user.status_code == 200
        assert prem_user.json()["valid_count"] == 1
        assert client.post("/api/internal/goodluck/premium/refresh", headers=_headers("uid:alice", "user")).status_code == 403
        assert client.get("/api/internal/goodluck/intraday/series?symbol=601872", headers=_headers("uid:alice", "user")).status_code == 403
        assert client.get("/api/internal/goodluck/intraday/macd-settings", headers=_headers("uid:alice", "user")).status_code == 403
        assert client.get("/api/internal/goodluck/intraday/push-config", headers=_headers("uid:alice", "user")).status_code == 403

        # 管理员可调 MACD 参数、保存规则、实时拉取分时与执行复盘测算
        macd_res = client.put(
            "/api/internal/goodluck/intraday/macd-settings",
            headers=_headers("uid:admin", "admin"),
            json={"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.001},
        )
        assert macd_res.status_code == 200
        assert macd_res.json()["short"] == 10

        push_save = client.put(
            "/api/internal/goodluck/intraday/push-config",
            headers=_headers("uid:admin", "admin"),
            json={
                "enabled": True,
                "premium_push_enabled": True,
                "bark_groups": [{"id": "b1", "name": "iPhone", "base_url": "https://api.day.app", "device_key": "sec123"}],
                "rules": [
                    {
                        "id": "r-up",
                        "enabled": False,
                        "side": "buy",
                        "title": "涨幅突破1%",
                        "joiner": "and",
                        "conditions": [{"type": "change_pct", "direction": "gte", "value": 1.0}],
                    }
                ],
            },
        )
        assert push_save.status_code == 200
        assert push_save.json()["bark_groups"][0]["device_key"] == ""
        assert push_save.json()["bark_groups"][0]["device_key_configured"] is True

        series_res = client.get(
            "/api/internal/goodluck/intraday/series?symbol=601872&refresh=1&rule_ids=r-up",
            headers=_headers("uid:admin", "admin"),
        )
        assert series_res.status_code == 200
        s_body = series_res.json()
        assert s_body["symbol"] == "601872"
        assert len(s_body["points"]) == 3
        assert s_body["points"][1]["change_pct"] is not None
        assert s_body["replay"]["performance"]["buy_count"] >= 1

        rule_test = client.post(
            "/api/internal/goodluck/intraday/push-rule-test",
            headers=_headers("uid:admin", "admin"),
            json={
                "symbol": "601872",
                "rule": {
                    "id": "r-up",
                    "side": "buy",
                    "title": "涨幅突破1%",
                    "joiner": "and",
                    "conditions": [{"type": "change_pct", "direction": "gte", "value": 1.0}],
                },
            },
        )
        assert rule_test.status_code == 200
        assert rule_test.json()["evaluation"]["matched"] is True


def test_bridge_marks_funds_no_darkpool_and_excludes_from_global_dedup_counts(tmp_path) -> None:
    broker = FakeMarketBroker(
        {
            "601872": _make_snapshot("601872", with_l2=True, with_fund_flow=True),
            "160723": _make_snapshot("160723", with_l2=True, with_fund_flow=True),
            "161129": _make_snapshot("161129", with_l2=True, with_fund_flow=False),
        }
    )
    app, _accounts, _store = _build_app(tmp_path, max_total_symbols=1, broker=broker)

    with TestClient(app) as client:
        wl = client.get("/api/internal/goodluck/watchlists", headers=_headers("uid:alice")).json()
        primary_id = wl["groups"][0]["id"]

        # Occupy the single global darkpool slot with an equity.
        add_stock = client.post(
            f"/api/internal/goodluck/watchlists/groups/{primary_id}/symbols",
            headers=_headers("uid:alice"),
            json={"symbol": "601872"},
        )
        assert add_stock.status_code == 201
        assert add_stock.json()["supports_darkpool"] is True

        # Adding funds without darkpool succeeds without consuming the global darkpool quota.
        for code in ("160723", "161129"):
            res = client.post(
                f"/api/internal/goodluck/watchlists/groups/{primary_id}/symbols",
                headers=_headers("uid:alice"),
                json={"symbol": code},
            )
            assert res.status_code == 201
            assert res.json()["supports_darkpool"] is False

        wl_after = client.get("/api/internal/goodluck/watchlists", headers=_headers("uid:alice")).json()
        assert wl_after["limits"]["global_count"] == 1
        by_sym = {item["symbol"]: item for item in wl_after["groups"][0]["items"]}
        assert by_sym["601872"]["supports_darkpool"] is True
        assert by_sym["160723"]["supports_darkpool"] is False

        snap = client.get("/api/internal/goodluck/symbols/160723/snapshot", headers=_headers("uid:alice")).json()
        assert snap["supports_darkpool"] is False
        assert snap["main_fund_flow"] == {}
        assert snap["capabilities"]["darkpool"] == {
            "available": False,
            "reason": "FUND_DARKPOOL_UNSUPPORTED",
        }

        hist = client.get("/api/internal/goodluck/symbols/160723/fund-flow/history", headers=_headers("uid:alice"))
        assert hist.status_code == 200
        assert hist.json()["supports_darkpool"] is False
        assert hist.json()["periods"]["today"]["points"] == []

        daily = client.get("/api/internal/goodluck/symbols/160723/fund-flow/daily", headers=_headers("uid:alice"))
        assert daily.status_code == 200
        assert daily.json()["supports_darkpool"] is False
        assert daily.json()["points"] == []

        admin_status = client.get("/api/internal/goodluck/admin/status", headers=_headers("uid:admin", "admin")).json()
        assert admin_status["global_count"] == 1
        assert admin_status["monitored_count"] == 1
        assert [row["symbol"] for row in admin_status["monitoring"]] == ["601872"]


def test_bridge_quotes_refreshes_closed_continuous_cache_and_post_close_backfill(tmp_path) -> None:
    from dataclasses import replace

    stale_quote = replace(
        _make_snapshot("601872"),
        source_time="14:31",
        market_phase=MarketPhase.CONTINUOUS,
        quote={
            "price": "7.20",
            "change_percent": "+2.10%",
            "turnover_rate": "1.55%",
            "open": "7.05",
            "high": "7.25",
            "low": "7.01",
            "amount": "51234.5",
            "large_order_net": "0.18",
            "large_order_amount": "1234.5",
            "retail_count": "-0.34",
            "macdfs": "+0.012",
        },
    )

    class ClosedRefreshBroker(FakeMarketBroker):
        async def refresh(self, symbol: str, *, detail: bool, max_age_seconds: float = 0, force: bool = False):
            self.refresh_calls.append((symbol, detail, max_age_seconds))
            if force:
                return replace(stale_quote, symbol=symbol, market_phase=MarketPhase.CLOSED, source_time="15:00")
            return replace(stale_quote, symbol=symbol, market_phase=MarketPhase.CLOSED, source_time="15:00")

    broker = ClosedRefreshBroker({"601872": stale_quote}, market_open=False)
    app, _, _ = _build_app(tmp_path, broker=broker)
    backfill_calls: list[str] = []
    closed_snap = replace(stale_quote, market_phase=MarketPhase.CLOSED, source_time="15:00")
    app.state.post_close_backfill = lambda sym: (backfill_calls.append(sym), closed_snap)[1]

    with TestClient(app) as client:
        wl = client.get("/api/internal/goodluck/watchlists", headers=_headers("uid:alice")).json()
        primary_id = wl["groups"][0]["id"]
        assert client.post(
            f"/api/internal/goodluck/watchlists/groups/{primary_id}/symbols",
            headers=_headers("uid:alice"),
            json={"symbol": "601872"},
        ).status_code == 201
        backfill_calls.clear()
        broker.refresh_calls.clear()

        quotes = client.get("/api/internal/goodluck/quotes?symbols=601872", headers=_headers("uid:alice"))
        assert quotes.status_code == 200
        assert quotes.json()["items"]["601872"]["market_phase"] == "CLOSED"
        assert quotes.json()["items"]["601872"]["source_time"] == "15:00"
        assert ("601872", False, 10.0) in broker.refresh_calls

        snap = client.get("/api/internal/goodluck/symbols/601872/snapshot", headers=_headers("uid:alice")).json()
        assert snap["metrics"]["open"] == "7.05"
        assert snap["metrics"]["high"] == "7.25"
        assert snap["metrics"]["low"] == "7.01"
        assert snap["metrics"]["amount"] == "51234.5"

        bf = client.post(
            "/api/internal/goodluck/monitoring/symbols/601872/backfill",
            headers=_headers("uid:alice"),
        )
        assert bf.status_code == 200
        assert backfill_calls == ["601872"]


def test_bridge_quotes_and_history_isolate_cross_day_fund_flow_during_intraday(tmp_path) -> None:
    from dataclasses import replace

    today_snap = replace(_make_snapshot("601872", with_l2=True, with_fund_flow=False), source_time="09:35")
    broker = FakeMarketBroker({"601872": today_snap}, market_open=True)
    app, _, store = _build_app(
        tmp_path,
        broker=broker,
        expected_intraday_trade_date=lambda: "20261125",
    )
    store.record_fund_flow(
        "601872",
        trade_date="20261124",
        minute="15:00",
        periods={
            "today": {
                "unit": "亿元",
                "main_net_inflow": "2.00",
                "main_visible_inflow": "0.86",
                "main_hidden_inflow": "1.14",
                "retail_inflow": "-2.00",
            },
            "three_day": {
                "unit": "亿元",
                "main_net_inflow": "3.00",
                "main_visible_inflow": "1.20",
                "main_hidden_inflow": "1.80",
                "retail_inflow": "-3.00",
            },
            "five_day": {
                "unit": "亿元",
                "main_net_inflow": "4.00",
                "main_visible_inflow": "1.50",
                "main_hidden_inflow": "2.50",
                "retail_inflow": "-4.00",
            },
        },
        captured_at=datetime(2026, 11, 24, 7, 0, tzinfo=timezone.utc),
    )
    store.record_fund_flow_error("601872", "DIRECT_SESSION_EXPIRED")

    with TestClient(app) as client:
        wl = client.get("/api/internal/goodluck/watchlists", headers=_headers("uid:alice")).json()
        primary_id = wl["groups"][0]["id"]
        assert client.post(
            f"/api/internal/goodluck/watchlists/groups/{primary_id}/symbols",
            headers=_headers("uid:alice"),
            json={"symbol": "601872"},
        ).status_code == 201

        quotes = client.get("/api/internal/goodluck/quotes?symbols=601872", headers=_headers("uid:alice"))
        assert quotes.status_code == 200
        q_row = quotes.json()["items"]["601872"]
        assert q_row["today_main_hidden_inflow"] is None
        assert q_row["fund_flow_trade_date"] is None

        # Default history request during active session returns empty today placeholder with error instead of yesterday.
        hist_today = client.get("/api/internal/goodluck/symbols/601872/fund-flow/history", headers=_headers("uid:alice"))
        assert hist_today.status_code == 200
        body_today = hist_today.json()
        assert body_today["trade_date"] == "20261125"
        assert body_today["available_dates"] == ["20261125", "20261124"]
        assert body_today["last_error"] == "DIRECT_SESSION_EXPIRED"
        assert body_today["periods"]["today"]["points"] == []

        # Explicit historical date still returns yesterday's data normally.
        hist_yday = client.get(
            "/api/internal/goodluck/symbols/601872/fund-flow/history?trade_date=20261124",
            headers=_headers("uid:alice"),
        )
        assert hist_yday.status_code == 200
        body_yday = hist_yday.json()
        assert body_yday["trade_date"] == "20261124"
        assert body_yday["available_dates"] == ["20261125", "20261124"]
        assert body_yday["periods"]["today"]["points"][0]["main_hidden_inflow"] == "1.14"

        # Once today points land, default history immediately serves today's curve.
        store.record_fund_flow(
            "601872",
            trade_date="20261125",
            minute="09:43",
            periods={
                "today": {
                    "unit": "万元",
                    "main_net_inflow": "320.00",
                    "main_visible_inflow": "100.00",
                    "main_hidden_inflow": "220.00",
                    "retail_inflow": "-320.00",
                },
            },
            captured_at=datetime(2026, 11, 25, 1, 43, tzinfo=timezone.utc),
        )
        hist_recovered = client.get(
            "/api/internal/goodluck/symbols/601872/fund-flow/history",
            headers=_headers("uid:alice"),
        )
        assert hist_recovered.status_code == 200
        assert hist_recovered.json()["trade_date"] == "20261125"
        assert hist_recovered.json()["periods"]["today"]["points"][0]["main_hidden_inflow"] == "220.00"


def test_bridge_premium_includes_brent_panel(tmp_path) -> None:
    app, _, store = _build_app(tmp_path)
    store.save_premium_snapshot(
        {
            "as_of": "2026-11-25 14:55:00",
            "rows": [
                {
                    "symbol": "160723",
                    "name": "嘉实原油LOF",
                    "price": "1.250",
                    "premium_rate": "1.20%",
                }
            ],
            "valid_count": 1,
            "errors": {},
        }
    )
    with store._lock, store._connection:
        store._connection.execute(
            """INSERT INTO research_days(
                   symbol, trade_date, name, market, source, pre_close,
                   quote_price, quote_change_percent, quote_open, quote_high,
                   quote_low, quote_volume, collected_at
               ) VALUES('BRN0Y', '20261125', 'ICE布伦特原油连续', 'ICE', 'test', '72.000',
                        '73.440', '+2.00%', '72.100', '73.800', '71.900', '5000',
                        '2026-11-25T15:10:00+08:00')"""
        )
        store._connection.executemany(
            """INSERT INTO research_points(
                   symbol, trade_date, minute, price, average_price, volume, captured_at
               ) VALUES('BRN0Y', '20261125', ?, ?, ?, '100', '2026-11-25T15:10:00+08:00')""",
            [
                ("09:30", "72.100", "72.100"),
                ("15:00", "73.000", "72.600"),
                ("15:10", "73.440", "72.700"),
            ],
        )
        store._connection.execute(
            """INSERT INTO research_days(
                   symbol, trade_date, name, market, source, pre_close,
                   quote_price, quote_change_percent, quote_open, quote_high,
                   quote_low, quote_volume, collected_at
               ) VALUES('160723', '20261125', '嘉实原油', 'SZ', 'test', '1.235',
                        '1.250', '+1.21%', '1.240', '1.255', '1.235', '2000',
                        '2026-11-25T15:00:00+08:00')"""
        )
        store._connection.execute(
            """INSERT INTO research_days(
                   symbol, trade_date, name, market, source, pre_close,
                   quote_price, quote_change_percent, quote_open, quote_high,
                   quote_low, quote_volume, collected_at
               ) VALUES('513350', '20261125', '标普油气ETF富国', 'SH', 'test', '1.150',
                        '1.210', '+5.22%', '1.160', '1.220', '1.150', '3000',
                        '2026-11-25T15:00:00+08:00')"""
        )
        store._connection.executemany(
            """INSERT INTO research_points(
                   symbol, trade_date, minute, price, average_price, volume, captured_at
               ) VALUES(?, '20261125', ?, ?, ?, '100', '2026-11-25T15:00:00+08:00')""",
            [
                ("160723", "09:30", "1.240", "1.240"),
                ("160723", "15:00", "1.250", "1.245"),
                ("513350", "09:30", "1.160", "1.160"),
                ("513350", "15:00", "1.210", "1.190"),
            ],
        )

    with TestClient(app) as client:
        res = client.get("/api/internal/goodluck/premium", headers=_headers("uid:alice"))
        assert res.status_code == 200
        body = res.json()
        assert len(body["rows"]) == 1
        brent = body["brent"]
        assert brent is not None
        assert brent["symbol"] == "BRN0Y"
        assert brent["price"] == 73.44
        assert brent["ref_1500_price"] == 73.0
        assert abs(brent["since_1500_pct"] - 0.6) < 0.05
        assert len(brent["intraday_24h"]) >= 3
        assert len(brent["series_5d"]) >= 3
        assert len(brent["daily_july"]) >= 1
        overlays = brent["fund_overlays"]
        assert "160723" in overlays
        assert "513350" in overlays
        assert len(overlays["513350"]["daily_july"]) == 1
        assert overlays["513350"]["intraday_24h"][-1]["fund_price"] == 1.21


def test_bridge_trade_idempotency_50_symbols_and_sse_events(tmp_path) -> None:
    import asyncio
    from level2_service.auto_trade import AutoTradeEngine, SQLiteAutoTradeStore

    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    rstore = SQLiteResearchStore(tmp_path / "research.db")
    astore = SQLiteAutoTradeStore(tmp_path / "research.db")

    place_calls = 0

    class _FakeExec:
        adb_serial = "emulator-5556"
        active_tab = "buy"

        def place_order(self, *, symbol: str, side: str, price: str, quantity: int, **_kwargs):
            nonlocal place_calls
            place_calls += 1
            return {
                "accepted": True,
                "status": "SUBMITTED",
                "entrust_no": f"HT-{place_calls}",
                "symbol": symbol,
                "side": side,
                "price": price,
                "quantity": quantity,
                "timings_ms": {"total": 12},
            }

        def get_account_summary(self, **_kwargs):
            return {"logged_in": True, "metrics": {"available_cash": "50000.00"}, "positions": []}

        def get_today_orders(self, **_kwargs):
            return {"orders": []}

    executor = _FakeExec()
    lookup = lambda sym: SymbolLookup(symbol=sym, name=f"证券{sym}", market="17" if sym.startswith(("6", "5")) else "36")
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=executor,
        symbol_lookup=lookup,
    )
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        secure_admin_cookies=False,
        market_account_store=accounts,
        research_store=rstore,
        symbol_lookup=lookup,
        goodluck_internal_token=TOKEN,
        trade_executor=executor,
        auto_trade_engine=engine,
        auto_trade_interval_seconds=3600.0,
    )

    with TestClient(app) as client:
        # 1. X-Idempotency-Key replay, 409 conflict, 422 format validation
        bad = client.post(
            "/api/internal/goodluck/trade/orders",
            headers={**_headers("uid:admin", "admin"), "X-Idempotency-Key": "bad key!"},
            json={"symbol": "160723", "side": "buy", "price": "2.100", "quantity": 100, "dry_run": True},
        )
        assert bad.status_code == 422

        first = client.post(
            "/api/internal/goodluck/trade/orders",
            headers={**_headers("uid:admin", "admin"), "X-Idempotency-Key": "idem-ord-1"},
            json={"symbol": "160723", "side": "buy", "price": "2.100", "quantity": 100, "dry_run": True},
        )
        assert first.status_code == 200
        assert place_calls == 1
        assert first.json()["entrust_no"] == "HT-1"

        replay = client.post(
            "/api/internal/goodluck/trade/orders",
            headers={**_headers("uid:admin", "admin"), "X-Idempotency-Key": "idem-ord-1"},
            json={"symbol": "160723", "side": "buy", "price": "2.100", "quantity": 100, "dry_run": True},
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replayed"] is True
        assert place_calls == 1

        conflict = client.post(
            "/api/internal/goodluck/trade/orders",
            headers={**_headers("uid:admin", "admin"), "X-Idempotency-Key": "idem-ord-1"},
            json={"symbol": "160723", "side": "buy", "price": "2.100", "quantity": 200, "dry_run": True},
        )
        assert conflict.status_code == 409

        # 2. Up to 50 symbols & strict single-rule profile; 51 symbols and multi-rule rule_bindings rejected with 422
        syms_50 = [f"60{i:04d}" for i in range(50)]
        cfg_ok = client.put(
            "/api/internal/goodluck/trade/strategy",
            headers=_headers("uid:admin", "admin"),
            json={
                "enabled": True,
                "signal_source": "intraday_macd_flow",
                "execution_mode": "semi_auto",
                "symbols": syms_50,
                "order_quantity": 100,
                "pricing_mode": "counterparty_book",
                "order_book_level": 3,
                "slippage_ticks": 3,
                "auto_cancel_seconds": 30,
                "daily_max_buy_amount": 50000.0,
                "block_non_continuous_session": True,
                "symbol_profiles": {
                    "600000": {
                        "enabled": True,
                        "signal_source": "quant_rule",
                        "quant_rule_id": "rule-a",
                    }
                },
            },
        )
        assert cfg_ok.status_code == 200
        assert len(cfg_ok.json()["config"]["symbols"]) == 50
        prof_600 = cfg_ok.json()["config"]["symbol_profiles"]["600000"]
        assert prof_600["signal_source"] == "quant_rule"
        assert prof_600["quant_rule_id"] == "rule-a"
        assert "rule_bindings" not in prof_600

        cfg_multi_rule = client.put(
            "/api/internal/goodluck/trade/strategy",
            headers=_headers("uid:admin", "admin"),
            json={
                "enabled": True,
                "signal_source": "intraday_macd_flow",
                "execution_mode": "semi_auto",
                "symbols": ["600000"],
                "order_quantity": 100,
                "pricing_mode": "counterparty_book",
                "order_book_level": 3,
                "slippage_ticks": 3,
                "auto_cancel_seconds": 30,
                "daily_max_buy_amount": 50000.0,
                "block_non_continuous_session": True,
                "symbol_profiles": {
                    "600000": {
                        "enabled": True,
                        "rule_bindings": [
                            {"signal_source": "quant_rule", "quant_rule_id": "rule-a", "enabled": True, "priority": 5},
                            {"signal_source": "intraday_macd_flow", "enabled": True, "priority": 10},
                        ],
                    }
                },
            },
        )
        assert cfg_multi_rule.status_code == 422

        syms_51 = [f"60{i:04d}" for i in range(51)]
        cfg_too_many = client.put(
            "/api/internal/goodluck/trade/strategy",
            headers=_headers("uid:admin", "admin"),
            json={
                "enabled": True,
                "signal_source": "intraday_macd_flow",
                "execution_mode": "semi_auto",
                "symbols": syms_51,
                "order_quantity": 100,
                "slippage_ticks": 3,
                "auto_cancel_seconds": 30,
                "daily_max_buy_amount": 50000.0,
                "block_non_continuous_session": True,
            },
        )
        assert cfg_too_many.status_code == 422

    # 3. Direct ASGI SSE test on /api/internal/goodluck/trade/events without blocking TestClient
    async def _verify_sse() -> list[str]:
        chunks: list[str] = []
        disconnected = asyncio.Event()
        sent_req = False

        async def receive():
            nonlocal sent_req
            if not sent_req:
                sent_req = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(msg):
            if msg["type"] == "http.response.body" and msg.get("body"):
                chunks.append(msg["body"].decode("utf-8"))
                if len(chunks) == 1:
                    engine._publish_event("signal_pending", {"signal": {"id": "sig-sse-1", "symbol": "160723"}})
                elif len(chunks) >= 2:
                    disconnected.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/internal/goodluck/trade/events",
            "raw_path": b"/api/internal/goodluck/trade/events",
            "query_string": b"viewer_open=1",
            "headers": [
                (b"x-goodluck-internal-token", TOKEN.encode("utf-8")),
                (b"x-goodluck-user", b"uid:admin"),
                (b"x-goodluck-role", b"admin"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=3.0)
        return chunks

    sse_chunks = asyncio.run(_verify_sse())
    assert len(sse_chunks) >= 2
    assert "event: snapshot" in sse_chunks[0]
    assert "event: signal_pending" in sse_chunks[1]
    assert "sig-sse-1" in sse_chunks[1]
    assert len(engine._subscribers) == 0
    astore.close()
    rstore.close()


def test_bridge_capital_policies_api_crud_activation_and_guards(tmp_path: Path) -> None:
    from level2_service.auto_trade import AutoTradeEngine, SQLiteAutoTradeStore
    from level2_service.quant_rules import SQLiteQuantRuleStore

    db_path = tmp_path / "capital_policies_api.db"
    rstore = SQLiteResearchStore(db_path)
    qstore = SQLiteQuantRuleStore(db_path)
    astore = SQLiteAutoTradeStore(db_path)
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=None,
        symbol_lookup=lambda s: CATALOG.get(s),
        quant_rule_store=qstore,
    )
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-pass-123"),
        admin_session_secret="c" * 32,
        symbol_lookup=lambda s: CATALOG.get(s),
        market_account_store=SQLiteMarketAccountStore(tmp_path / "market.db"),
        research_store=rstore,
        goodluck_internal_token=TOKEN,
        auto_trade_engine=engine,
        quant_rule_store=qstore,
    )
    client = TestClient(app)
    headers = {
        "x-goodluck-internal-token": TOKEN,
        "x-goodluck-user": "uid:admin",
        "x-goodluck-role": "admin",
    }

    schema_res = client.get("/api/internal/goodluck/trade/capital-policies/schema", headers=headers)
    assert schema_res.status_code == 200
    assert any(e["id"] == "route_d_plus" for e in schema_res.json()["engines"])

    list_res = client.get("/api/internal/goodluck/trade/capital-policies", headers=headers)
    assert list_res.status_code == 200
    assert any(p["id"] == "route_d_sharpe_shield" for p in list_res.json()["policies"])

    create_res = client.post(
        "/api/internal/goodluck/trade/capital-policies",
        headers=headers,
        json={
            "id": "api-risk-parity-test",
            "name": "API发布风险平价测试",
            "description": "动态发布免发版策略",
            "engine_type": "inverse_vol_risk_parity",
            "params": {"sigma_cap_pct": 2.3, "max_total_budget_wt": 0.92},
            "metrics_summary": {"sharpe_ratio": 5.51, "max_drawdown_pct": 6.4},
        },
    )
    assert create_res.status_code == 200
    assert create_res.json()["policy"]["id"] == "api-risk-parity-test"

    # Activate custom strategy via strategy config API
    put_res = client.put(
        "/api/internal/goodluck/trade/strategy",
        headers=headers,
        json={
            "enabled": True,
            "signal_source": "intraday_macd_flow",
            "execution_mode": "semi_auto",
            "sizing_mode": "model_auto",
            "symbols": ["160723", "161129"],
            "order_quantity": 100,
            "pricing_mode": "counterparty_book",
            "order_book_level": 2,
            "slippage_ticks": 3,
            "auto_cancel_seconds": 30,
            "daily_max_buy_amount": 140000.0,
            "block_non_continuous_session": True,
            "capital_policy_mode": "api-risk-parity-test",
            "pool_policy": {"total_pool_cap": 140000.0},
        },
    )
    assert put_res.status_code == 200
    put_body = put_res.json()
    assert put_body["capital_policy_mode"] == "api-risk-parity-test"
    assert put_body["active_capital_policy"]["engine_type"] == "inverse_vol_risk_parity"
    assert put_body["capital_ledger"]["pool_allocation"]["pool_cap"] == pytest.approx(140000.0)

    # Active strategy cannot be deleted (409)
    del_active = client.delete("/api/internal/goodluck/trade/capital-policies/api-risk-parity-test", headers=headers)
    assert del_active.status_code == 409

    # Switch to built-in shield strategy, then delete custom policy succeeds
    put_shield = client.put(
        "/api/internal/goodluck/trade/strategy",
        headers=headers,
        json={**put_body["config"], "capital_policy_mode": "route_d_sharpe_shield"},
    )
    assert put_shield.status_code == 200
    assert put_shield.json()["capital_policy_mode"] == "route_d_sharpe_shield"

    del_ok = client.delete("/api/internal/goodluck/trade/capital-policies/api-risk-parity-test", headers=headers)
    assert del_ok.status_code == 204

    qstore.close()
    astore.close()
    rstore.close()
