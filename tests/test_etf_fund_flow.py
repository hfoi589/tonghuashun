from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from urllib.request import Request
from zoneinfo import ZoneInfo

from argon2 import PasswordHasher
from fastapi.testclient import TestClient
import pytest

from level2_service.api import create_app
from level2_service.etf_fund_flow import (
    EtfFundFlowError,
    EtfFundFlowMonitor,
    TonghuashunEtfFundFlowService,
    format_etf_compact_rmb,
    format_etf_flow_text,
)
from level2_service.market_accounts import InMemoryMarketSessionStore, SQLiteMarketAccountStore
from level2_service.market_data import KlineBar, MarketSeriesPage
from level2_service.models import MetricKind, TaskStatus
from level2_service.parsed_values import SymbolLookup
from level2_service.queue import InMemoryStreams
from level2_service.safe_http import SafeHttpError, SafeHttpResponse


class StubTransport:
    def __init__(self, routes: dict[str, SafeHttpResponse | Exception]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def request(self, request: Request, _timeout: float) -> SafeHttpResponse:
        self.calls.append(request.full_url)
        for prefix, outcome in self.routes.items():
            if prefix in request.full_url:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise AssertionError(f"unexpected URL: {request.full_url}")


def _json_resp(payload: dict, status: int = 200) -> SafeHttpResponse:
    import json

    return SafeHttpResponse(
        status=status,
        body=json.dumps(payload).encode("utf-8"),
    )


def _sample_summary() -> dict:
    return {
        "status_code": 0,
        "status_msg": "Success",
        "data": {
            "total": 1,
            "indexes": [
                {"index_id": "stockEtfNetBuyFlowDate"},
                {"index_id": "stockEtfNetBuyFlow"},
                {"index_id": "stockEtfNetBuyFlow3d"},
                {"index_id": "stockEtfNetBuyFlow5d"},
                {"index_id": "stockEtfNetBuyFlow20d"},
                {"index_id": "stockEtfNetBuyFlow60d"},
            ],
            "data": [
                {
                    "code": "17:601872",
                    "values": [
                        {"idx": 0, "value": "2026-11-24"},
                        {"idx": 1, "value": -19337560.63},
                        {"idx": 2, "value": 38109165.98},
                        {"idx": 3, "value": 110275413.19},
                        {"idx": 4, "value": 397457535.10},
                        {"idx": 5, "value": 816398234.81},
                    ],
                }
            ],
        },
    }


def _sample_line() -> dict:
    # 2026-11-23 00:00 CST = 1795363200, 2026-11-24 00:00 CST = 1795449600
    return {
        "status_code": 0,
        "status_msg": "Success",
        "data": {
            "time_range": ["1795363200", "1795449600"],
            "indexes": [
                {"index_id": "stock_etf_net_buy_flow_amount"},
                {"index_id": "stock_etf_fund_scale"},
            ],
            "data": [
                {
                    "code": "17:601872",
                    "values": [
                        {"idx": 0, "values": [120000000.0, -19337560.63]},
                        {"idx": 1, "values": [500000000.0, 480000000.0]},
                    ],
                }
            ],
        },
    }


def _sample_holdings() -> dict:
    return {
        "status_code": 0,
        "status_msg": "Success",
        "data": {
            "entity_info": [
                "code",
                "name",
                "stock_etf_subred_holdrate",
                "stock_etf_subred_risekline",
                "stock_etf_subred_net_buy_flow_amount",
                "stock_etf_subred_hold_num",
                "etfNetBuyFlowStreakCount",
                "flowRate",
                "etfNetBuyFlowStreakCount",
                "flowRate",
            ],
            "entity": [
                {
                    "code": "17:601872",
                    "entities": [
                        [
                            ["20:560700", "央企红利ETF广发", "3.12", "63.26", "986443.75", "1200.0", "5", "0.14", "5", ""],
                            ["20:510330", "沪深300ETF华夏", "0.25", "14.79", "6304019.53", "600.0", "7", "", "7", ""],
                            ["20:561570", "油气ETF华泰柏瑞", "8.00", "79.92", "-96350.47", "6300.0", "-1", "0.00", "-1", ""],
                        ]
                    ],
                }
            ],
        },
    }


def test_formatting_helpers_match_ths_rules() -> None:
    assert format_etf_flow_text(-19337560.63) == "-1933.76万"
    assert format_etf_flow_text(816398234.81) == "8.16亿"
    assert format_etf_compact_rmb(6304019.53) == "630.4万"
    assert format_etf_compact_rmb(986443.75) == "98.64万"
    assert format_etf_compact_rmb(120000000.0) == "1.2亿"
    assert format_etf_compact_rmb(8500.0) == "8500.00"
    assert format_etf_compact_rmb(None) == "--"


def test_service_parses_summary_chart_and_holdings_and_caches_per_period() -> None:
    now_clock = [100.0]
    transport = StubTransport(
        {
            "/table/17:601872/": _json_resp(_sample_summary()),
            "/line/17:601872/": _json_resp(_sample_line()),
            "/etf_list/hold_stock/17:601872": _json_resp(_sample_holdings()),
        }
    )
    service = TonghuashunEtfFundFlowService(
        transport=transport,  # type: ignore[arg-type]
        monotonic_clock=lambda: now_clock[0],
        wall_clock=lambda: datetime(2026, 11, 25, 10, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    result = service.fetch(
        "601872",
        "60d",
        close_prices={
            "2026-11-23": (15.43, 0.12),
            "2026-11-24": (15.53, 0.10),
        },
    )

    assert result["available"] is True
    assert result["summary"]["latest_badge"] == {
        "date": "11-24",
        "full_date": "2026-11-24",
        "value_text": "-1933.76万",
        "text": "11-24 净流入 -1933.76万",
        "tone": "down",
    }
    assert result["summary"]["metrics"] == [
        {"key": "60d", "label": "60日净流入", "value_text": "8.16亿", "tone": "up"},
        {"key": "20d", "label": "20日净流入", "value_text": "3.97亿", "tone": "up"},
        {"key": "5d", "label": "5日净流入", "value_text": "1.10亿", "tone": "up"},
        {"key": "3d", "label": "3日净流入", "value_text": "3810.92万", "tone": "up"},
    ]
    assert result["chart"]["cumulative_unit"] == "亿"
    assert result["chart"]["daily_unit"] == "亿"
    assert result["chart"]["points"] == [
        {
            "date": "2026-11-23",
            "short_date": "11-23",
            "cumulative_value": 1.2,
            "cumulative_text": "1.20亿",
            "daily_value": 1.2,
            "daily_text": "1.20亿",
            "close_price": 15.43,
            "price_change": 0.12,
        },
        {
            "date": "2026-11-24",
            "short_date": "11-24",
            "cumulative_value": 1.01,
            "cumulative_text": "1.01亿",
            "daily_value": -0.19,
            "daily_text": "-1933.76万",
            "close_price": 15.53,
            "price_change": 0.10,
        },
    ]
    assert [item["symbol"] for item in result["holdings"]["items"]] == ["510330", "560700", "561570"]
    assert result["holdings"]["items"][0]["net_inflow_text"] == "630.4万"
    assert result["holdings"]["items"][0]["streak_days"] == 7
    assert result["holdings"]["items"][0]["hold_rate_text"] == "0.25%"
    assert result["holdings"]["items"][2]["tone"] == "down"
    assert len(transport.calls) == 3

    # Switching period reuses summary & holdings caches and only fetches new line range.
    result_120 = service.fetch("601872", "120d")
    assert len(transport.calls) == 4
    assert result_120["summary"]["metrics"][0] == {
        "key": "120d",
        "label": "120日净流入",
        "value_text": "1.01亿",
        "tone": "up",
    }


def test_service_short_circuits_exchange_funds_and_surfaces_upstream_error() -> None:
    transport = StubTransport({"/quotation/": SafeHttpError()})
    service = TonghuashunEtfFundFlowService(
        transport=transport,  # type: ignore[arg-type]
        sleeper=lambda _: None,
    )

    etf_res = service.fetch("510330", "60d")
    assert etf_res["available"] is False
    assert etf_res["holdings"]["items"] == []
    assert etf_res["update_status"] == "empty"
    assert transport.calls == []

    with pytest.raises(EtfFundFlowError) as caught:
        service.fetch("601872", "60d")
    assert caught.value.error_code == "ETF_FUND_FLOW_UNAVAILABLE"


def test_market_job_and_goodluck_etf_flow_routes(tmp_path) -> None:
    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = accounts.create_user("trader", "temporary-123")
    accounts.change_password(user.id, "temporary-123", "permanent-456")
    group = accounts.list_watchlists(user.id)[0]
    accounts.add_symbol(user.id, group.id, SymbolLookup(symbol="601872", name="招商轮船", market="17"))

    transport = StubTransport(
        {
            "/table/17:601872/": _json_resp(_sample_summary()),
            "/line/17:601872/": _json_resp(_sample_line()),
            "/etf_list/hold_stock/17:601872": _json_resp(_sample_holdings()),
        }
    )
    etf_service = TonghuashunEtfFundFlowService(transport=transport)  # type: ignore[arg-type]

    class FakeBroker:
        async def series(self, symbol: str, period: str, _cursor: str | None, _limit: int) -> MarketSeriesPage:
            assert symbol == "601872"
            assert period == "day"
            return MarketSeriesPage(
                symbol=symbol,
                period=period,
                bars=(
                    KlineBar(time="2026-11-23", open="15.30", high="15.50", low="15.20", close="15.43", volume="100", amount="1543"),
                    KlineBar(time="2026-11-24", open="15.43", high="15.60", low="15.38", close="15.53", volume="110", amount="1700"),
                ),
            )

    store = InMemoryStreams()
    app = create_app(
        store=store,
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        secure_admin_cookies=False,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        market_data_broker=FakeBroker(),
        symbol_lookup=lambda symbol: SymbolLookup(symbol=symbol, name="招商轮船", market="17"),
        goodluck_internal_token="bridge-secret",
        etf_flow_service=etf_service,
    )

    with TestClient(app) as client:
        # 1. Market endpoint
        assert client.get("/api/v1/market/symbols/601872/etf-flow").status_code == 401
        assert client.post("/api/v1/session", json={"username": "trader", "password": "permanent-456"}).status_code == 200
        assert client.get("/api/v1/market/symbols/601872/etf-flow?period=99d").status_code == 422
        assert client.get("/api/v1/market/symbols/600036/etf-flow").status_code == 404
        market_res = client.get("/api/v1/market/symbols/601872/etf-flow?period=60d")
        assert market_res.status_code == 200
        assert market_res.json()["name"] == "招商轮船"
        assert market_res.json()["chart"]["points"][1]["close_price"] == 15.53

        # 2. Job endpoint (also works for non-watchlist completed task)
        public_id = client.post("/api/v1/jobs", json={"symbol": "601872", "include_long_capture": False}).json()["public_id"]
        assert client.get(f"/api/v1/jobs/{public_id}/etf-flow").status_code == 404
        store.transition(public_id, TaskStatus.RUNNING)
        store.complete_result(
            public_id,
            {
                MetricKind.STOCK_NAME: "招商轮船",
                MetricKind.CURRENT_PRICE: "15.53",
                MetricKind.CHANGE_PERCENT: "+0.65%",
                MetricKind.TURNOVER_RATE: "2.36%",
                MetricKind.LARGE_ORDER_NET: "0.33",
                MetricKind.LARGE_ORDER_AMOUNT: "54165.0万",
                MetricKind.RETAIL_COUNT: "-2.32",
                MetricKind.MACDFS: "0.000",
            },
            None,
        )
        assert client.get(f"/api/v1/jobs/{public_id}/etf-flow?period=bad").status_code == 422
        job_res = client.get(f"/api/v1/jobs/{public_id}/etf-flow?period=60d")
        assert job_res.status_code == 200
        assert job_res.json()["summary"]["latest_badge"]["value_text"] == "-1933.76万"

        # 3. Goodluck bridge endpoint
        bridge_headers = {"X-Goodluck-Internal-Token": "bridge-secret", "X-Goodluck-User": "uid:alice"}
        assert client.get("/api/internal/goodluck/symbols/601872/etf-flow", headers={"X-Goodluck-Internal-Token": "wrong", "X-Goodluck-User": "uid:alice"}).status_code == 401
        assert client.get("/api/internal/goodluck/symbols/601872/etf-flow", headers=bridge_headers).status_code == 404
        accounts_user = accounts.get_or_create_external_user("goodluck", "uid:alice")
        alice_group = accounts.list_watchlists(accounts_user.id)[0]
        accounts.add_symbol(accounts_user.id, alice_group.id, SymbolLookup(symbol="601872", name="招商轮船", market="17"))
        bridge_res = client.get("/api/internal/goodluck/symbols/601872/etf-flow?period=60d", headers=bridge_headers)
        assert bridge_res.status_code == 200
        assert bridge_res.json()["holdings"]["total_count"] == 3



class SequenceTransport:
    def __init__(self, queues: dict[str, list[SafeHttpResponse | Exception]]) -> None:
        self.queues = {k: list(v) for k, v in queues.items()}
        self.calls: list[str] = []

    def request(self, request: Request, _timeout: float) -> SafeHttpResponse:
        self.calls.append(request.full_url)
        for prefix, items in self.queues.items():
            if prefix in request.full_url:
                outcome = items.pop(0) if len(items) > 1 else items[0]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise AssertionError(f"unexpected URL: {request.full_url}")


def _sample_summary_for_date(date_str: str, latest: float = -19337560.63) -> dict:
    payload = _sample_summary()
    payload["data"]["data"][0]["values"][0]["value"] = date_str
    payload["data"]["data"][0]["values"][1]["value"] = latest
    return payload


def _sample_line_through(ts_values: list[tuple[int, float]]) -> dict:
    return {
        "status_code": 0,
        "status_msg": "Success",
        "data": {
            "time_range": [str(ts) for ts, _ in ts_values],
            "indexes": [
                {"index_id": "stock_etf_net_buy_flow_amount"},
                {"index_id": "stock_etf_fund_scale"},
            ],
            "data": [
                {
                    "code": "17:601872",
                    "values": [
                        {"idx": 0, "values": [val for _, val in ts_values]},
                        {"idx": 1, "values": [500000000.0 for _ in ts_values]},
                    ],
                }
            ],
        },
    }


def test_service_retries_transient_errors_and_skips_4xx_retries() -> None:
    sleeps: list[float] = []
    transport = SequenceTransport(
        {
            "/table/": [
                SafeHttpResponse(status=502, body=b"bad gateway"),
                SafeHttpError(),
                _json_resp(_sample_summary()),
            ],
            "/line/": [_json_resp(_sample_line())],
            "/etf_list/hold_stock/": [_json_resp(_sample_holdings())],
        }
    )
    service = TonghuashunEtfFundFlowService(
        transport=transport,  # type: ignore[arg-type]
        sleeper=sleeps.append,
        retry_backoff_seconds=0.2,
    )

    res = service.fetch("601872", "60d")
    assert res["available"] is True
    assert res["stale"] is False
    assert sleeps == [0.2, 0.4]

    sleeps_4xx: list[float] = []
    t_404 = SequenceTransport(
        {
            "/quotation/": [SafeHttpResponse(status=404, body=b"not found")],
        }
    )
    s_404 = TonghuashunEtfFundFlowService(
        transport=t_404,  # type: ignore[arg-type]
        sleeper=sleeps_4xx.append,
    )
    with pytest.raises(EtfFundFlowError) as caught:
        s_404.fetch("601872", "60d")
    assert caught.value.error_code == "ETF_FUND_FLOW_HTTP_4XX"
    assert sleeps_4xx == []
    assert len(t_404.calls) == 3


def test_t1_schedule_ttl_morning_boundary_and_skew_healing() -> None:
    # 2026-11-23 00:00 CST = 1795363200, 2026-11-24 00:00 CST = 1795449600
    mono = [100.0]
    wall = [datetime(2026, 11, 25, 8, 40, tzinfo=ZoneInfo("Asia/Shanghai"))]
    transport = SequenceTransport(
        {
            "/table/": [
                _json_resp(_sample_summary_for_date("2026-11-23", 120000000.0)),
                _json_resp(_sample_summary_for_date("2026-11-24", -19337560.63)),
            ],
            "/line/": [
                _json_resp(_sample_line_through([(1795363200, 120000000.0)])),
                _json_resp(_sample_line_through([(1795363200, 120000000.0)])),
                _json_resp(_sample_line_through([(1795363200, 120000000.0), (1795449600, -19337560.63)])),
            ],
            "/etf_list/hold_stock/": [
                _json_resp(_sample_holdings()),
            ],
        }
    )
    service = TonghuashunEtfFundFlowService(
        transport=transport,  # type: ignore[arg-type]
        monotonic_clock=lambda: mono[0],
        wall_clock=lambda: wall[0],
        sleeper=lambda _: None,
    )

    early = service.fetch("601872", "60d")
    assert early["expected_trade_date"] == "2026-11-23"
    assert early["update_status"] == "settled"
    assert len(transport.calls) == 3

    # Crossing 08:50 CST invalidates pre-08:50 T-2 cache; summary advances to 11-24 while line still lags at 11-23 -> TTL clamped to 60s
    wall[0] = datetime(2026, 11, 25, 8, 51, tzinfo=ZoneInfo("Asia/Shanghai"))
    mono[0] = 110.0
    skewed = service.fetch("601872", "60d")
    assert skewed["summary"]["latest_badge"]["full_date"] == "2026-11-24"
    assert len(transport.calls) == 6

    # After 65s (> 60s clamp), line refreshes and reaches 2026-11-24
    mono[0] = 175.0
    wall[0] = datetime(2026, 11, 25, 9, 2, tzinfo=ZoneInfo("Asia/Shanghai"))
    healed = service.fetch("601872", "60d")
    assert healed["chart"]["points"][-1]["date"] == "2026-11-24"
    assert healed["update_status"] == "settled"
    calls_after_heal = len(transport.calls)

    # Settled cache remains valid past default 300s (uses settled_ttl_seconds=1800s)
    mono[0] = 600.0
    wall[0] = datetime(2026, 11, 25, 9, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    cached = service.fetch("601872", "60d")
    assert cached["update_status"] == "settled"
    assert len(transport.calls) == calls_after_heal


def test_sqlite_persistent_cache_survives_restart_and_serves_stale_fallback(tmp_path: Path) -> None:
    db_path = tmp_path / "etf-flow.db"
    mono = [100.0]
    wall = [datetime(2026, 11, 25, 10, 15, tzinfo=ZoneInfo("Asia/Shanghai"))]

    transport_ok = StubTransport(
        {
            "/table/17:601872/": _json_resp(_sample_summary()),
            "/line/17:601872/": _json_resp(_sample_line()),
            "/etf_list/hold_stock/17:601872": _json_resp(_sample_holdings()),
        }
    )
    service1 = TonghuashunEtfFundFlowService(
        transport=transport_ok,  # type: ignore[arg-type]
        cache_path=db_path,
        monotonic_clock=lambda: mono[0],
        wall_clock=lambda: wall[0],
    )
    initial = service1.fetch("601872", "60d")
    assert initial["update_status"] == "settled"
    assert service1.has_settled_cache("601872", "60d") is True
    service1.close()

    # Restart after settled TTL with upstream broken: SQLite stale fallback works cleanly.
    mono2 = [4000.0]
    wall2 = [datetime(2026, 11, 25, 11, 15, tzinfo=ZoneInfo("Asia/Shanghai"))]
    service2 = TonghuashunEtfFundFlowService(
        transport=StubTransport({"/quotation/": SafeHttpError()}),  # type: ignore[arg-type]
        cache_path=db_path,
        monotonic_clock=lambda: mono2[0],
        wall_clock=lambda: wall2[0],
        sleeper=lambda _: None,
    )
    assert service2.has_settled_cache("601872", "60d") is True
    fallback = service2.fetch("601872", "60d")
    assert fallback["available"] is True
    assert fallback["stale"] is True
    assert fallback["update_status"] == "stale_fallback"
    assert fallback["data_date"] == "2026-11-24"
    assert fallback["summary"]["metrics"][0]["value_text"] == "8.16亿"
    assert fallback["holdings"]["total_count"] == 3
    assert fallback["source_errors"]["summary"] == "ETF_FUND_FLOW_UNAVAILABLE"

    # Past 7d max_stale_seconds, stale data expires rather than misreporting ancient history.
    wall2[0] = datetime(2026, 12, 5, 11, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    with pytest.raises(EtfFundFlowError):
        service2.fetch("601872", "60d")
    service2.close()


def test_sub_endpoint_isolation_and_line_to_summary_cross_derivation() -> None:
    # Summary fails while line & holdings succeed -> summary metrics/badge cross-derived from line.
    service_summary_down = TonghuashunEtfFundFlowService(
        transport=StubTransport(  # type: ignore[arg-type]
            {
                "/table/": SafeHttpError(),
                "/line/": _json_resp(_sample_line()),
                "/etf_list/hold_stock/": _json_resp(_sample_holdings()),
            }
        ),
        wall_clock=lambda: datetime(2026, 11, 25, 10, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
        sleeper=lambda _: None,
    )
    res_a = service_summary_down.fetch("601872", "60d")
    assert res_a["available"] is True
    assert res_a["degraded"] is True
    assert res_a["update_status"] == "partial"
    assert res_a["source_errors"] == {
        "summary": "ETF_FUND_FLOW_UNAVAILABLE",
        "line": None,
        "holdings": None,
        "daily_kline": None,
    }
    assert res_a["summary"]["latest_badge"]["value_text"] == "-1933.76万"
    assert res_a["summary"]["latest_badge"]["full_date"] == "2026-11-24"

    # Holdings fails while summary & line succeed -> chart stays available & degraded flagged.
    service_holdings_down = TonghuashunEtfFundFlowService(
        transport=StubTransport(  # type: ignore[arg-type]
            {
                "/table/": _json_resp(_sample_summary()),
                "/line/": _json_resp(_sample_line()),
                "/etf_list/hold_stock/": SafeHttpResponse(status=500, body=b"oops"),
            }
        ),
        wall_clock=lambda: datetime(2026, 11, 25, 10, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
        sleeper=lambda _: None,
    )
    res_b = service_holdings_down.fetch("601872", "60d")
    assert res_b["available"] is True
    assert res_b["degraded"] is True
    assert res_b["source_errors"]["holdings"] == "ETF_FUND_FLOW_UNAVAILABLE"
    assert len(res_b["chart"]["points"]) == 2
    assert res_b["holdings"]["items"] == []


def test_etf_fund_flow_monitor_morning_prewarm_and_settled_skip(tmp_path: Path) -> None:
    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = accounts.create_user("trader", "temporary-123")
    group = accounts.list_watchlists(user.id)[0]
    accounts.add_symbol(user.id, group.id, SymbolLookup(symbol="601872", name="招商轮船", market="17"))
    accounts.add_symbol(user.id, group.id, SymbolLookup(symbol="510330", name="沪深300ETF", market="20"))

    wall = [datetime(2026, 11, 25, 9, 6, tzinfo=ZoneInfo("Asia/Shanghai"))]
    transport = StubTransport(
        {
            "/table/17:601872/": _json_resp(_sample_summary()),
            "/line/17:601872/": _json_resp(_sample_line()),
            "/etf_list/hold_stock/17:601872": _json_resp(_sample_holdings()),
        }
    )
    service = TonghuashunEtfFundFlowService(
        transport=transport,  # type: ignore[arg-type]
        cache_path=tmp_path / "etf.db",
        wall_clock=lambda: wall[0],
        sleeper=lambda _: None,
    )
    monitor = EtfFundFlowMonitor(
        accounts,
        service,
        clock=lambda: wall[0],
        pacing_seconds=0.0,
    )

    first = asyncio.run(monitor.poll_once())
    assert first == {"skipped": False, "warmed": 1, "failed": 0, "settled": 0}
    assert len(transport.calls) == 3

    wall[0] = datetime(2026, 11, 25, 9, 8, tzinfo=ZoneInfo("Asia/Shanghai"))
    second = asyncio.run(monitor.poll_once())
    assert second == {"skipped": False, "warmed": 0, "failed": 0, "settled": 1}
    assert len(transport.calls) == 3

    wall[0] = datetime(2026, 11, 25, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    afternoon = asyncio.run(monitor.poll_once())
    assert afternoon == {"skipped": True, "warmed": 0, "failed": 0, "settled": 0}
    service.close()


def test_route_reports_daily_kline_source_error_without_failing_etf_flow(tmp_path: Path) -> None:
    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = accounts.create_user("trader", "temporary-123")
    accounts.change_password(user.id, "temporary-123", "permanent-456")
    group = accounts.list_watchlists(user.id)[0]
    accounts.add_symbol(user.id, group.id, SymbolLookup(symbol="601872", name="招商轮船", market="17"))

    etf_service = TonghuashunEtfFundFlowService(
        transport=StubTransport(  # type: ignore[arg-type]
            {
                "/table/17:601872/": _json_resp(_sample_summary()),
                "/line/17:601872/": _json_resp(_sample_line()),
                "/etf_list/hold_stock/17:601872": _json_resp(_sample_holdings()),
            }
        ),
        wall_clock=lambda: datetime(2026, 11, 25, 10, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    class FailingBroker:
        async def series(self, *_args):
            raise RuntimeError("KLINE_SOURCE_DOWN")

    app = create_app(
        store=InMemoryStreams(),
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        secure_admin_cookies=False,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        market_data_broker=FailingBroker(),
        symbol_lookup=lambda symbol: SymbolLookup(symbol=symbol, name="招商轮船", market="17"),
        etf_flow_service=etf_service,
    )
    with TestClient(app) as client:
        assert client.post("/api/v1/session", json={"username": "trader", "password": "permanent-456"}).status_code == 200
        res = client.get("/api/v1/market/symbols/601872/etf-flow?period=60d")
        assert res.status_code == 200
        body = res.json()
        assert body["available"] is True
        assert body["chart"]["points"][0]["close_price"] is None
        assert body["source_errors"]["daily_kline"] == "DAILY_KLINE_UNAVAILABLE"
