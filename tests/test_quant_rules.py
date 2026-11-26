from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
import sqlite3

from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from level2_service.api import create_app
from level2_service.auto_trade import AutoTradeEngine, SHANGHAI_TZ, SQLiteAutoTradeStore
from level2_service.market_accounts import SQLiteMarketAccountStore
from level2_service.parsed_values import SymbolLookup
from level2_service.quant_rules import (
    SQLiteQuantRuleStore,
    backfill_intraday_history,
    enrich_store_with_line_funds,
    evaluate_quant_strategy,
    export_quant_dataset,
)
from level2_service.research_replay import build_replay_markers
from level2_service.research_store import SQLiteResearchStore


TOKEN = "quant-bridge-token"
CATALOG = {
    "160723": SymbolLookup(symbol="160723", name="嘉实原油", market="36"),
}


def _headers(role: str = "admin", user: str = "domain:wilson") -> dict[str, str]:
    return {
        "X-Goodluck-Internal-Token": TOKEN,
        "X-Goodluck-User": user,
        "X-Goodluck-Role": role,
    }


def _seed_day(
    rstore: SQLiteResearchStore,
    trade_date: str,
    prices: list[float],
    *,
    pre_close: float = 2.100,
    flows: list[float] | None = None,
) -> None:
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            """INSERT OR REPLACE INTO research_days(
                   symbol,trade_date,name,market,source,pre_close,quote_price,collected_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            ("160723", trade_date, "嘉实原油", "36", "TEST", f"{pre_close:.3f}", f"{prices[-1]:.3f}", f"{trade_date}T15:00:00+08:00"),
        )
        for idx, p in enumerate(prices):
            minute_val = (570 + idx + 1) if idx < 120 else (780 + (idx - 120) + 1)
            hhmm = f"{minute_val // 60:02d}:{minute_val % 60:02d}"
            avg = sum(prices[: idx + 1]) / (idx + 1)
            vol = 100000 if idx % 10 != 0 else 250000
            flow_val = f"{flows[idx]:.2f}" if flows is not None else None
            rstore._connection.execute(
                """INSERT OR REPLACE INTO research_points(
                       symbol,trade_date,minute,price,average_price,volume,
                       large_order_net,large_order_amount,retail_count,macdfs,captured_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("160723", trade_date, hhmm, f"{p:.3f}", f"{avg:.3f}", str(vol), None, flow_val, None, None, f"{trade_date}T15:00:00+08:00"),
            )


def test_extended_conditions_and_cooldown() -> None:
    points = [
        {"time": f"09:{31 + i:02d}", "price": str(2.100 - 0.002 * i), "average_price": "2.100", "volume": "10000", "diff": -0.002, "dea": -0.001, "macd": -0.002}
        for i in range(6)
    ]
    # Point 6 bounces upward across DEA below VWAP inside time window with volume surge
    points.append(
        {"time": "09:37", "price": "2.094", "average_price": "2.100", "volume": "25000", "diff": -0.0005, "dea": -0.001, "macd": 0.001}
    )
    # Point 7 immediate second trigger within cooldown should be suppressed
    points.append(
        {"time": "09:38", "price": "2.095", "average_price": "2.100", "volume": "25000", "diff": 0.0001, "dea": -0.0005, "macd": 0.0012}
    )
    rules = [
        {
            "id": "ext-buy",
            "enabled": True,
            "side": "buy",
            "title": "均线下方MACD买点",
            "joiner": "and",
            "cooldown_minutes": 5,
            "conditions": [
                {"type": "vwap_deviation_pct", "direction": "lte", "value": -0.10},
                {"type": "macd_state", "metric": "diff", "direction": "lte", "value": 0.0},
                {"type": "time_window", "windows": [["09:35", "11:25"]]},
                {"type": "volume_ratio", "lookback": 5, "direction": "gte", "value": 1.5},
                {"type": "price_momentum_pct", "minutes": 1, "direction": "gte", "value": 0.01},
            ],
        }
    ]
    markers = build_replay_markers("160723", points, rules, marker_threshold=0.0001)
    assert len(markers) == 1
    assert markers[0]["time"] == "09:37"


def test_evaluate_quant_strategy_5m_outcomes_and_flow_scope(tmp_path: Path) -> None:
    db_path = tmp_path / "research.db"
    rstore = SQLiteResearchStore(db_path)
    # Day 1 (no flow): 40 minutes, dip then rally around index 15..25 so buy signal rises >0 after 5m, then falls after peak
    p1 = [2.100] * 10 + [2.095, 2.090, 2.085, 2.080, 2.078, 2.082, 2.090, 2.096, 2.102, 2.108, 2.115, 2.120, 2.118, 2.112, 2.105, 2.098, 2.092, 2.088, 2.085, 2.080] + [2.080] * 10
    _seed_day(rstore, "20260302", p1, pre_close=2.100, flows=None)
    # Day 2 (with flow): 60 minutes with flow data
    p2 = [2.100] * 12 + [2.092, 2.084, 2.076, 2.080, 2.088, 2.098, 2.106, 2.112, 2.118, 2.122, 2.115, 2.105, 2.095, 2.088, 2.082, 2.078, 2.075, 2.072] + [2.072] * 30
    flows2 = [100.0] * len(p2)
    for idx in range(14, 20):
        flows2[idx] = 100.0 + (idx - 13) * 30.0
    for idx in range(22, 29):
        flows2[idx] = 280.0 - (idx - 21) * 35.0
    _seed_day(rstore, "20260601", p2, pre_close=2.100, flows=flows2)

    # Pure MACD covers both days (2 evaluated days)
    pure_res = evaluate_quant_strategy(
        rstore,
        {
            "id": "pure",
            "symbols": ["160723"],
            "macd_settings": {"short": 5, "long": 10, "signal": 3, "marker_threshold": 0.00001},
            "buy_rule": {"title": "金叉", "joiner": "and", "conditions": [{"type": "macd_signal", "signal": "red"}]},
            "sell_rule": {"title": "死叉", "joiner": "and", "conditions": [{"type": "macd_signal", "signal": "green"}]},
        },
        start_date="20260301",
        primary_horizon_minutes=5,
    )
    assert pure_res["coverage"]["evaluated_days"] == 2
    assert pure_res["coverage"]["skipped_no_flow_days"] == 0
    assert pure_res["summary"]["buy_signals"] >= 1
    assert pure_res["summary"]["sell_signals"] >= 1
    assert pure_res["horizons"]["5m"]["buy"]["total"] >= 1
    assert "by_session" in pure_res["attribution"]
    assert "by_vwap_zone" in pure_res["attribution"]

    # Flow rule with data_scope="auto" automatically restricts to flow_days (1 day) and records skipped_no_flow_days=1
    flow_res = evaluate_quant_strategy(
        rstore,
        {
            "id": "flow",
            "symbols": ["160723"],
            "macd_settings": {"short": 5, "long": 10, "signal": 3, "marker_threshold": 0.00001},
            "buy_rule": {
                "title": "大单金叉",
                "joiner": "and",
                "conditions": [
                    {"type": "macd_signal", "signal": "red"},
                    {"type": "flow_change_pct", "flow": "big", "minutes": 1, "direction": "increase", "value": 3.0},
                ],
            },
            "sell_rule": None,
        },
        start_date="20260301",
    )
    assert flow_res["requires_flow"] is True
    assert flow_res["effective_scope"] == "flow_only"
    assert flow_res["coverage"]["evaluated_days"] == 1
    assert flow_res["coverage"]["skipped_no_flow_days"] == 1
    rstore.close()


def test_backfill_opm_tdx_and_line_funds(tmp_path: Path) -> None:
    # Build minimal mock OPM DB
    opm_db = tmp_path / "opm.sqlite3"
    opm_conn = sqlite3.connect(opm_db)
    opm_conn.executescript(
        """
        CREATE TABLE trade_days(code TEXT, trade_date TEXT, name TEXT, pre_close REAL, captured_at TEXT);
        CREATE TABLE minute_quotes(code TEXT, trade_date TEXT, minute TEXT, price REAL, avg_price REAL, volume_shares INTEGER, big_net_wan REAL, small_net_wan REAL, captured_at TEXT);
        """
    )
    opm_conn.execute("INSERT INTO trade_days VALUES('160723','20260518','嘉实原油',2.000,'2026-05-18T15:00:00+08:00')")
    for i in range(240):
        m = (570 + i + 1) if i < 120 else (780 + (i - 120) + 1)
        opm_conn.execute(
            "INSERT INTO minute_quotes VALUES('160723','20260518',?,2.010,2.005,1000,12.5,-4.2,'2026-05-18T15:00:00+08:00')",
            (f"{m//60:02d}:{m%60:02d}",),
        )
    opm_conn.commit()
    opm_conn.close()

    rstore = SQLiteResearchStore(tmp_path / "research.db")

    def fake_kline(url: str, _ref: str, _timeout: float) -> str:
        if url.endswith("/last.js"):
            return 'quotebridge_v6_line_hs_160723_01_last({"year":{"2026":3}})'
        return 'quotebridge_v6_line_hs_160723_01_2026({"data":"20260302,1.600,1.720,1.590,1.700,10000,17000;20260518,2.000,2.020,1.990,2.010,8000,16000;20261125,2.094,2.130,2.090,2.109,5000,10500"})'

    def fake_tdx(_sym: str, dates: list[str] | tuple[str, ...]) -> dict[str, dict]:
        out = {}
        for td in dates:
            pts = []
            for i in range(240):
                m = (570 + i + 1) if i < 120 else (780 + (i - 120) + 1)
                pts.append({"minute": f"{m//60:02d}:{m%60:02d}", "price": "2.109", "average_price": "2.110", "volume": "1000"})
            out[td] = {"pre_close": "2.094", "points": pts}
        return out

    def fake_line_funds(_url: str, _ref: str, _timeout: float) -> str:
        return '{"line":"09:31;12.34;-5.00;-7.34|15:00;-177.69;-581.78;-640.13"}'

    res = backfill_intraday_history(
        rstore,
        "160723",
        start_date="20260301",
        opm_db_path=opm_db,
        fetch_kline_fn=fake_kline,
        fetch_tdx_fn=fake_tdx,
        fetch_line_funds_fn=fake_line_funds,
    )
    assert res["coverage"]["total_days"] == 3
    assert res["coverage"]["flow_days"] == 1  # 20260518 has 240 flow points
    assert res["line_funds"]["updated_minutes"] == 2
    assert res["line_funds"]["trade_date"] == "20261125"

    # Non-latest date must never be overwritten by lineFunds
    res_old = enrich_store_with_line_funds(
        rstore,
        "160723",
        trade_date="20260302",
        fetch_text=fake_line_funds,
    )
    assert res_old["updated_minutes"] == 0
    assert res_old["reason"] == "skip_non_latest_date"
    rstore.close()


def test_quant_routes_admin_only_and_auto_trade_quant_rule(tmp_path: Path) -> None:
    db_path = tmp_path / "research.db"
    rstore = SQLiteResearchStore(db_path)
    qstore = SQLiteQuantRuleStore(db_path)
    astore = SQLiteAutoTradeStore(db_path)
    prices = [2.100] * 12 + [2.090, 2.080, 2.070, 2.060, 2.055, 2.050, 2.048, 2.046, 2.045, 2.045, 2.045, 2.046, 2.085]
    _seed_day(rstore, "20261126", prices, pre_close=2.100, flows=[100.0] * len(prices))

    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=None,
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: datetime(2026, 11, 26, 10, 5, tzinfo=SHANGHAI_TZ),
        epoch_fn=lambda: 1_800_000_000.0,
        quant_rule_store=qstore,
    )
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-pass-123"),
        admin_session_secret="a" * 32,
        symbol_lookup=lambda sym: CATALOG[sym],
        market_account_store=SQLiteMarketAccountStore(tmp_path / "market.db"),
        research_store=rstore,
        goodluck_internal_token=TOKEN,
        auto_trade_engine=engine,
        quant_rule_store=qstore,
    )
    client = TestClient(app)

    # Regular users MUST be blocked with 403 on all /quant/* endpoints
    assert client.get("/api/internal/goodluck/quant/schema", headers=_headers(role="user")).status_code == 403
    assert client.get("/api/internal/goodluck/quant/dataset", headers=_headers(role="user")).status_code == 403
    assert client.get("/api/internal/goodluck/quant/rules", headers=_headers(role="user")).status_code == 403
    assert client.post("/api/internal/goodluck/quant/evaluate", headers=_headers(role="user"), json={"symbol": "160723"}).status_code == 403
    assert client.post("/api/internal/goodluck/quant/rules", headers=_headers(role="user"), json={"name": "x", "buy_rule": {"conditions": [{"type": "macd_signal", "signal": "red"}]}}).status_code == 403

    # Admin can inspect schema, dataset, rules, evaluate, and save AI rule
    assert client.get("/api/internal/goodluck/quant/schema", headers=_headers(role="admin")).status_code == 200
    ds = client.get("/api/internal/goodluck/quant/dataset?mode=minutes&limit_days=5", headers=_headers(role="admin"))
    assert ds.status_code == 200
    assert ds.json()["returned_days"] == 1

    rules_res = client.get("/api/internal/goodluck/quant/rules", headers=_headers(role="admin"))
    assert rules_res.status_code == 200
    assert len(rules_res.json()["rules"]) >= 2

    # Builtin rule cannot be deleted
    assert client.delete("/api/internal/goodluck/quant/rules/preset-macd-flow-baseline", headers=_headers(role="admin")).status_code == 409

    # AI saves a new rule
    ai_rule_res = client.post(
        "/api/internal/goodluck/quant/rules",
        headers=_headers(role="admin"),
        json={
            "id": "ai-test-vwap-cross",
            "name": "AI测试均线金叉",
            "description": "AI生成的测试规则",
            "source": "ai",
            "symbols": ["160723"],
            "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.00001},
            "buy_rule": {
                "title": "AI金叉",
                "joiner": "and",
                "conditions": [{"type": "macd_signal", "signal": "red"}],
            },
            "sell_rule": {
                "title": "AI死叉",
                "joiner": "and",
                "conditions": [{"type": "macd_signal", "signal": "green"}],
            },
        },
    )
    assert ai_rule_res.status_code == 200
    assert ai_rule_res.json()["rule"]["latest_eval"]["summary"]["buy_signals"] >= 1

    # Auto-trade engine switches to signal_source="quant_rule" and triggers from AI rule
    put_res = client.put(
        "/api/internal/goodluck/trade/strategy",
        headers=_headers(role="admin"),
        json={
            "enabled": True,
            "signal_source": "quant_rule",
            "quant_rule_id": "ai-test-vwap-cross",
            "execution_mode": "semi_auto",
            "symbols": ["160723"],
            "order_quantity": 100,
            "slippage_ticks": 3,
            "auto_cancel_seconds": 30,
            "daily_max_buy_amount": 50000,
            "block_non_continuous_session": True,
        },
    )
    assert put_res.status_code == 200
    assert put_res.json()["config"]["quant_rule_id"] == "ai-test-vwap-cross"
    assert "AI测试均线金叉" in put_res.json()["signal_source_label"]

    poll_res = asyncio.run(engine.poll_once())
    assert poll_res["triggered"] == 1
    pending = astore.list_pending_signals()
    assert len(pending) == 1
    assert pending[0]["rule_id"] == "ai-test-vwap-cross"
    assert "AI测试均线金叉" in pending[0]["rule_title"]

    # AI rule can be deleted
    assert client.delete("/api/internal/goodluck/quant/rules/ai-test-vwap-cross", headers=_headers(role="admin")).status_code == 204

    # Compact rules list strips bulky arrays while single-rule endpoint returns full payload
    re_ai = client.post(
        "/api/internal/goodluck/quant/rules",
        headers=_headers(role="admin"),
        json={
            "id": "ai-test-compact-check",
            "name": "AI紧凑列表检查",
            "source": "ai",
            "symbols": ["160723"],
            "buy_rule": {"title": "B", "joiner": "and", "conditions": [{"type": "macd_signal", "signal": "red"}]},
            "sell_rule": {"title": "S", "joiner": "and", "conditions": [{"type": "macd_signal", "signal": "green"}]},
        },
    )
    assert re_ai.status_code == 200
    compact_res = client.get("/api/internal/goodluck/quant/rules?compact=true", headers=_headers(role="admin"))
    assert compact_res.status_code == 200
    compact_item = next(r for r in compact_res.json()["rules"] if r["id"] == "ai-test-compact-check")
    assert compact_item["latest_eval"]["signals"] == []
    assert compact_item["latest_eval"]["signals_truncated"] is True
    assert compact_item["latest_eval"]["signal_count"] >= 1
    assert compact_item["latest_eval"]["base_position_t0"]["daily_ledger"] == []

    single_res = client.get("/api/internal/goodluck/quant/rules/ai-test-compact-check", headers=_headers(role="admin"))
    assert single_res.status_code == 200
    assert len(single_res.json()["rule"]["latest_eval"]["signals"]) >= 1
    assert client.get("/api/internal/goodluck/quant/rules/non-existent", headers=_headers(role="admin")).status_code == 404

    summary_res = client.get("/api/internal/goodluck/quant/dataset?mode=summary&start_date=20261101", headers=_headers(role="admin"))
    assert summary_res.status_code == 200
    assert summary_res.json()["monthly_breakdown"][0]["days"] == 1
    assert summary_res.json()["monthly_breakdown"][0]["points"] == len(prices)

    # /trade/strategy/live-eval evaluates active strategy buy/sell indicators cleanly
    assert client.put(
        "/api/internal/goodluck/trade/strategy",
        headers=_headers(role="admin"),
        json={
            "enabled": True,
            "signal_source": "quant_rule",
            "quant_rule_id": "ai-test-compact-check",
            "execution_mode": "semi_auto",
            "symbols": ["160723"],
            "order_quantity": 100,
            "slippage_ticks": 3,
            "auto_cancel_seconds": 30,
            "daily_max_buy_amount": 50000,
            "block_non_continuous_session": True,
        },
    ).status_code == 200
    live_res = client.get("/api/internal/goodluck/trade/strategy/live-eval?symbol=160723", headers=_headers(role="admin"))
    assert live_res.status_code == 200
    live_body = live_res.json()
    assert live_body["symbol"] == "160723"
    assert live_body["data_ready"] is True
    assert len(live_body["market_indicators"]) >= 2
    assert isinstance(live_body["buy"]["conditions"], list)
    assert isinstance(live_body["sell"]["conditions"], list)

    rstore.close()
    qstore.close()
    astore.close()


def _seed_symbol_bars(
    store: SQLiteResearchStore,
    symbol: str,
    trade_date: str,
    pre_close: float,
    minute_prices: list[tuple[str, float]],
    *,
    name: str = "测试标的",
    market: str = "SZ",
) -> None:
    close_p = minute_prices[-1][1]
    open_p = minute_prices[0][1]
    high_p = max(p for _, p in minute_prices)
    low_p = min(p for _, p in minute_prices)
    chg_pct = f"{((close_p - pre_close) / pre_close * 100.0):+.2f}%"
    captured_at = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}T15:00:00+08:00"
    with store._lock, store._connection:
        store._connection.execute(
            """INSERT INTO research_days(
                   symbol, trade_date, name, market, source, pre_close,
                   quote_price, quote_change_percent, quote_open, quote_high,
                   quote_low, quote_volume, collected_at
               ) VALUES(?, ?, ?, ?, 'test', ?, ?, ?, ?, ?, ?, '10000', ?)
               ON CONFLICT(symbol, trade_date) DO UPDATE SET
                   pre_close=excluded.pre_close,
                   quote_price=excluded.quote_price""",
            (
                symbol,
                trade_date,
                name,
                market,
                f"{pre_close:.3f}",
                f"{close_p:.3f}",
                chg_pct,
                f"{open_p:.3f}",
                f"{high_p:.3f}",
                f"{low_p:.3f}",
                captured_at,
            ),
        )
        store._connection.executemany(
            """INSERT INTO research_points(
                   symbol, trade_date, minute, price, average_price, volume, captured_at
               ) VALUES(?, ?, ?, ?, ?, '100', ?)
               ON CONFLICT(symbol, trade_date, minute) DO UPDATE SET
                   price=excluded.price,
                   average_price=excluded.average_price""",
            [
                (
                    symbol,
                    trade_date,
                    hhmm,
                    f"{px:.3f}",
                    f"{px:.3f}",
                    captured_at,
                )
                for hhmm, px in minute_prices
            ],
        )


def test_brent_schedule_dst_alignment_and_base_position_t0(tmp_path) -> None:
    from level2_service.brent_market import is_brent_market_open

    # Summer EDT (2026-07-15 CST): daily break 06:00-08:00 CST (18:00-20:00 EDT previous ET date)
    assert is_brent_market_open(datetime(2026, 7, 15, 5, 30, tzinfo=SHANGHAI_TZ))[0] is True
    assert is_brent_market_open(datetime(2026, 7, 15, 6, 30, tzinfo=SHANGHAI_TZ))[0] is False
    assert is_brent_market_open(datetime(2026, 7, 15, 8, 5, tzinfo=SHANGHAI_TZ))[0] is True

    # Winter EST (2026-11-25 CST): daily break 07:00-09:00 CST (18:00-20:00 EST)
    assert is_brent_market_open(datetime(2026, 11, 25, 6, 30, tzinfo=SHANGHAI_TZ))[0] is True
    assert is_brent_market_open(datetime(2026, 11, 25, 7, 30, tzinfo=SHANGHAI_TZ))[0] is False
    assert is_brent_market_open(datetime(2026, 11, 25, 9, 5, tzinfo=SHANGHAI_TZ))[0] is True

    # Weekend closure and Sunday open (Sun 18:00 EST = Mon 07:00 CST)
    assert is_brent_market_open(datetime(2026, 11, 28, 12, 0, tzinfo=SHANGHAI_TZ))[0] is False
    assert is_brent_market_open(datetime(2026, 11, 29, 20, 0, tzinfo=SHANGHAI_TZ))[0] is False
    assert is_brent_market_open(datetime(2026, 11, 30, 7, 5, tzinfo=SHANGHAI_TZ))[0] is True

    rstore = SQLiteResearchStore(tmp_path / "research.db")
    qstore = SQLiteQuantRuleStore(tmp_path / "quant.db")

    # Seed Day 0 (2026-07-01): BRN0Y ends 15:00 at 80.00
    _seed_symbol_bars(
        rstore,
        "BRN0Y",
        "20260701",
        80.00,
        [("09:30", 80.00), ("10:00", 80.00), ("15:00", 80.00)],
        name="ICE布伦特原油连续",
        market="ICE",
    )
    _seed_symbol_bars(
        rstore,
        "160723",
        "20260701",
        1.000,
        [
            ("09:30", 1.000), ("09:35", 1.000), ("09:40", 1.000), ("09:45", 1.000),
            ("09:50", 1.000), ("09:55", 1.000), ("10:00", 1.000), ("10:05", 1.000),
            ("10:10", 1.000), ("15:00", 1.000),
        ],
        name="嘉实原油",
    )

    # Seed Day 1 (2026-07-02): BRN0Y rallies overnight to 81.60 (+2.0%), stays strong;
    # 160723 lags at 10:00 (1.000, spread = 0 - 2.0 = -2.0%), closes at 1.020 -> buy-T gain = +2.0% - 0.02% = +1.98%
    _seed_symbol_bars(
        rstore,
        "BRN0Y",
        "20260702",
        80.00,
        [("09:30", 81.60), ("10:00", 81.60), ("10:05", 81.70), ("15:00", 82.00)],
        name="ICE布伦特原油连续",
        market="ICE",
    )
    _seed_symbol_bars(
        rstore,
        "160723",
        "20260702",
        1.000,
        [
            ("09:30", 1.005), ("09:35", 1.004), ("09:40", 1.003), ("09:45", 1.003),
            ("09:50", 1.002), ("09:55", 1.002), ("10:00", 1.000), ("10:05", 1.006),
            ("10:10", 1.008), ("15:00", 1.020),
        ],
        name="嘉实原油",
    )

    # Seed Day 2 (2026-07-03): BRN0Y drops at 10:00 (79.54 vs prev 15:00 82.00 => -3.0%) while 160723 spikes to 1.020 (+0% vs prev 1.020 => spread=+3.0%),
    # then 160723 falls to 1.000 at 15:00 -> sell-T gain = (1.020 - 1.000) / 1.020 * 100 - 0.02 = +1.9408%
    _seed_symbol_bars(
        rstore,
        "BRN0Y",
        "20260703",
        82.00,
        [("09:30", 80.36), ("10:00", 79.54), ("10:05", 79.40), ("15:00", 79.50)],
        name="ICE布伦特原油连续",
        market="ICE",
    )
    _seed_symbol_bars(
        rstore,
        "160723",
        "20260703",
        1.020,
        [
            ("09:30", 1.018), ("09:35", 1.019), ("09:40", 1.019), ("09:45", 1.020),
            ("09:50", 1.021), ("09:55", 1.021), ("10:00", 1.020), ("10:05", 1.015),
            ("10:10", 1.012), ("15:00", 1.000),
        ],
        name="嘉实原油",
    )

    rule_spec = {
        "id": "test-brent-t0",
        "name": "测试布伦特跨市场早盘做T",
        "symbols": ["160723", "BRN0Y"],
        "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0},
        "buy_rule": {
            "id": "b-brent",
            "side": "buy",
            "title": "布伦特强势A股滞涨",
            "joiner": "and",
            "conditions": [
                {"type": "brent_overnight_pct", "direction": "gte", "value": 1.5},
                {"type": "brent_change_pct", "anchor": "prev_1500", "direction": "gte", "value": 1.5},
                {"type": "brent_spread_pct", "direction": "lte", "value": -1.5},
                {"type": "time_window", "windows": [["09:58", "10:02"]]},
            ],
        },
        "sell_rule": {
            "id": "s-brent",
            "side": "sell",
            "title": "布伦特转弱A股溢价",
            "joiner": "and",
            "conditions": [
                {"type": "brent_overnight_pct", "direction": "lte", "value": -1.5},
                {"type": "brent_momentum_pct", "minutes": 30, "direction": "lte", "value": -0.5},
                {"type": "brent_spread_pct", "direction": "gte", "value": 1.5},
                {"type": "time_window", "windows": [["09:58", "10:02"]]},
            ],
        },
        "eval_criteria": {
            "start_date": "20260702",
            "morning_start": "09:30",
            "morning_end": "11:30",
            "max_daily_trades": 1,
            "round_trip_fee_pct": 0.02,
        },
    }

    result = evaluate_quant_strategy(rstore, rule_spec, symbol="160723")
    t0 = result["base_position_t0"]
    assert t0["evaluated_days"] == 2
    assert t0["traded_days"] == 2
    assert t0["buy_t_days"] == 1
    assert t0["sell_t_days"] == 1
    assert abs(t0["buy_t_net_return_pct"] - 1.98) < 0.01
    expected_sell = round((1.020 - 1.000) / 1.020 * 100.0 - 0.02, 4)
    assert abs(t0["sell_t_net_return_pct"] - expected_sell) < 0.01
    assert len(t0["monthly_returns"]) == 1
    assert t0["monthly_returns"][0]["buy_t_days"] == 1
    assert t0["monthly_returns"][0]["sell_t_days"] == 1
    assert "by_brent_spread" in result["attribution"]
    assert result["signals"]

    # BRN0Y direct dataset export + 160723 enriched minute fields
    brn_ds = export_quant_dataset(rstore, "BRN0Y", start_date="20260701", mode="daily")
    assert brn_ds["symbol"] == "BRN0Y"
    assert len(brn_ds["days"]) == 3

    min_ds = export_quant_dataset(rstore, "160723", start_date="20260702", mode="minutes")
    sample_pt = next(p for p in min_ds["days"][0]["points"] if p["time"] == "10:00")
    assert sample_pt["brn_price"] == 81.6
    assert abs(sample_pt["brn_overnight_pct"] - 2.0) < 0.05
    assert "brn_mom_15m_pct" in sample_pt
    assert sample_pt["to_close_ret_pct"] is not None

    # Persist rule with BRN0Y + check v2 schema via API
    from level2_service.quant_rules import get_quant_rule_schema

    saved = qstore.upsert_rule(rule_spec, research_store=rstore)
    assert "BRN0Y" in saved["symbols"]
    schema = get_quant_rule_schema()
    assert schema["version"] == 4
    assert len(schema["data_packs"]) == 5
    assert len(schema["condition_types"]) == 22
    assert "position_sizing_contract" in schema
    assert schema["default_start_date"] == "20260701"
    assert schema["cross_market_brent"]["symbol"] == "BRN0Y"

    # v3 base_t0_close dynamic tiered sizing (100% on deep discount day 1, 50% default on day 2) with 20,000 cap
    rule_t0_v3 = {
        **rule_spec,
        "id": "test-v3-t0-dynamic",
        "position_sizing": {
            "mode": "base_t0_close",
            "default_weight_pct": 50.0,
            "max_position_pct": 100.0,
            "max_daily_entries": 2,
            "close_flatten_time": "14:53",
            "conviction_tiers": [
                {
                    "label": "深折价重仓100%",
                    "weight_pct": 100.0,
                    "joiner": "and",
                    "conditions": [{"type": "brent_spread_pct", "direction": "lte", "value": -1.8}],
                }
            ],
        },
    }
    eval_t0_v3 = evaluate_quant_strategy(rstore, rule_t0_v3, symbol="160723", capital_cap=20000.0)
    csim_t0 = eval_t0_v3["capital_sim"]
    assert csim_t0["mode"] == "base_t0_close"
    assert csim_t0["capital_cap"] == 20000.0
    assert csim_t0["ending_shares"] == 0
    assert csim_t0["net_profit_amount"] > 500.0  # Day1 20000sh @1.000->1.020 (+400) + Day2 9800sh @1.020->1.000 (+196)
    assert csim_t0["total_orders"] == 4
    sig_buy_day1 = next(s for s in eval_t0_v3["signals"] if s["trade_date"] == "20260702")
    sig_sell_day2 = next(s for s in eval_t0_v3["signals"] if s["trade_date"] == "20260703")
    assert sig_buy_day1["planned_shares"] == 20000
    assert sig_buy_day1["sizing_tier"] == "深折价重仓100%"
    assert sig_sell_day2["planned_shares"] == 9800

    # v3 cash_pool_rolling mode: Day1 buys 20000 shares @1.000 (holds overnight to 1.020 -> +400 profit),
    # Day2 sells 100% held shares @1.020 before drop -> locks gain!
    rule_pool_v3 = {
        **rule_spec,
        "id": "test-v3-cashpool",
        "position_sizing": {
            "mode": "cash_pool_rolling",
            "default_weight_pct": 100.0,
            "max_position_pct": 100.0,
            "max_daily_entries": 2,
            "allow_same_day_turnaround": "auto",
            "conviction_tiers": [],
            "sell_conviction_tiers": [],
        },
    }
    saved_pool = qstore.upsert_rule({**rule_pool_v3, "eval_criteria": {"start_date": "20260702", "capital_cap": 20000.0}}, research_store=rstore)
    csim_pool = saved_pool["latest_eval"]["capital_sim"]
    assert csim_pool["mode"] == "cash_pool_rolling"
    assert csim_pool["same_day_turnaround"] is True
    assert csim_pool["ending_shares"] == 0
    assert csim_pool["buy_orders"] == 1
    assert csim_pool["sell_orders"] == 1
    assert csim_pool["net_profit_amount"] > 380.0

    # Ordinary A-share 601872 automatically disables same-day turnaround
    assert saved_pool["position_sizing"]["mode"] == "cash_pool_rolling"
    from level2_service.quant_rules import is_t0_turnaround_symbol

    assert is_t0_turnaround_symbol("160723") is True
    assert is_t0_turnaround_symbol("601872") is False

    rstore.close()
    qstore.close()


def test_v9_plus_aggressive_offline_backtest(tmp_path: Path, monkeypatch) -> None:
    import json as _json
    import time as _time
    from level2_service.v9_plus_engine import V9MacroDataStore

    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    rstore = SQLiteResearchStore(tmp_path / "v9p_research.db")
    qstore = SQLiteQuantRuleStore(tmp_path / "v9p_research.db")

    # Seed macro cache directly (NAV=2.000, FX=6.70 constant, OIL 2y [60.0 .. 90.0])
    V9MacroDataStore(rstore, allow_network=False)
    now_ts = _time.time()
    oil_rows = [
        ["20250601", int(datetime(2025, 6, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 60.0],
        ["20260630", int(datetime(2026, 7, 1, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 90.0],
    ]
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("nav_160723", _json.dumps({"20260628": 2.000, "20260629": 2.000, "20260630": 2.000}), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("oil_daily_2y", _json.dumps(oil_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcny_series", _json.dumps({"ts": [int(datetime(2026, 6, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [6.70]}), now_ts),
        )

    def _day_mins(base_p: float, updates: dict[str, float]) -> list[tuple[str, float]]:
        pts = []
        for idx in range(240):
            m = (570 + idx + 1) if idx < 120 else (780 + (idx - 120) + 1)
            hhmm = f"{m // 60:02d}:{m % 60:02d}"
            pts.append((hhmm, updates.get(hhmm, base_p)))
        return pts

    # Prior warmup day (20260630): stable anchor BRN=70.0 (brn_pos=0.333 <= 0.42 -> low regime), 160723=2.000
    _seed_symbol_bars(rstore, "BRN0Y", "20260630", 70.0, [("09:30", 70.0), ("15:00", 70.0)], name="布伦特", market="ICE")
    _seed_symbol_bars(rstore, "160723", "20260630", 2.000, _day_mins(2.000, {}), name="嘉实原油")

    # Day 1 (20260701): BRN=70.0 at open (low 33% -> 100% initial carry), rallies to 72.1 (+3.0%) while 160723 dips to 1.980 (-1.0%, prem=-3.5%, sp_fx=-3.0%)
    # Then 160723 recovers to 2.040 at 10:15 (exits +T at +3%) -> holds 100% at 14:53 because brn_pos=0.40 <= 0.42
    brn_d1 = _day_mins(70.0, {f"09:{m:02d}": 72.1 for m in range(35, 60)})
    brn_d1 = [(t, 72.1 if "09:35" <= t <= "10:30" else 71.0) for t, _ in brn_d1]
    _seed_symbol_bars(rstore, "BRN0Y", "20260701", 70.0, [("09:30", 70.0)] + brn_d1, name="布伦特", market="ICE")
    lof_d1 = _day_mins(
        2.000,
        {
            "09:58": 1.985,
            "09:59": 1.982,
            "10:00": 1.980,
            "10:15": 2.040,
        },
    )
    _seed_symbol_bars(rstore, "160723", "20260701", 2.000, lof_d1, name="嘉实原油")

    # Day 2 (20260702): BRN surges past 90 -> 88.5 at 14:53 (brn_pos >= 0.95 >= 0.90) -> triggers Inverse-T high sell & 14:53 0% clear-out!
    brn_d2 = [("09:30", 71.0)] + [(t, 88.5 if t >= "14:50" else 69.0) for t, _ in _day_mins(69.0, {})]
    _seed_symbol_bars(rstore, "BRN0Y", "20260702", 71.0, brn_d2, name="布伦特", market="ICE")
    lof_d2 = _day_mins(2.000, {"10:00": 2.040, "10:15": 1.980, "14:53": 2.000})
    _seed_symbol_bars(rstore, "160723", "20260702", 2.000, lof_d2, name="嘉实原油")

    saved = qstore.upsert_rule(
        {
            "id": "ai-v9p-test",
            "name": "V9+ 进取测试",
            "source": "ai",
            "symbols": ["160723", "BRN0Y"],
            "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0002},
            "buy_rule": {"title": "正T买点", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "lte", "value": -0.25}]},
            "sell_rule": {"title": "反T卖点", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "gte", "value": 0.42}]},
            "position_sizing": {
                "mode": "cash_pool_rolling",
                "engine_profile": "v9_plus_aggressive",
                "default_weight_pct": 80.0,
                "max_position_pct": 100.0,
                "max_daily_entries": 8,
                "cooldown_minutes": 4,
                "scale_factor": 1.0,
                "close_flatten_time": "14:53",
            },
            "eval_criteria": {
                "start_date": "20260701",
                "round_trip_fee_pct": 0.01,
                "capital_cap": 50000.0,
            },
        },
        research_store=rstore,
    )
    ev = saved["latest_eval"]
    csim = ev["capital_sim"]
    assert saved["position_sizing"]["engine_profile"] == "v9_plus_aggressive"
    assert csim["capital_cap"] == 50000.0
    assert csim["evaluated_days"] == 2
    assert csim["ending_shares"] == 0  # Cleared to 0% at 14:53 on Day 2 when brn_pos >= 0.90
    assert csim["total_orders"] >= 2
    assert csim["net_profit_amount"] > 0.0
    assert len(ev["base_position_t0"]["monthly_returns"]) == 1

    rstore.close()
    qstore.close()


def test_v9_plus_high_premium_flat_account_intraday_scalp_and_close_zero(tmp_path: Path, monkeypatch) -> None:
    import json as _json
    import time as _time
    from datetime import timedelta as _timedelta
    from level2_service.v9_plus_engine import V9MacroDataStore, evaluate_v9_plus_backtest, evaluate_v9_plus_live_decision

    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    rstore = SQLiteResearchStore(tmp_path / "v9p_scalp.db")
    V9MacroDataStore(rstore, allow_network=False)
    now_ts = _time.time()
    oil_rows = [
        ["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 55.0],
        ["20261124", int(datetime(2026, 11, 25, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 81.0],
    ]
    nav_payload = {}
    base_d = datetime(2026, 11, 1, tzinfo=SHANGHAI_TZ)
    for k in range(25):
        td_k = (base_d + _timedelta(days=k)).strftime("%Y%m%d")
        nav_payload[td_k] = 2.080
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("nav_160723", _json.dumps(nav_payload), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("oil_daily_2y", _json.dumps(oil_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcny_series", _json.dumps({"ts": [int(datetime(2026, 10, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [6.70]}), now_ts),
        )

    # Seed 16 warmup days declining from 2.280 to 2.094 so MA5 < MA15 (bear trend), BRN=68.0 (brn_pos=0.50 > 0.45)
    warmup_dates = [f"202611{d:02d}" for d in range(3, 19)]
    for idx, td_w in enumerate(warmup_dates):
        close_w = round(2.280 - idx * 0.012, 3)
        pre_w = round(close_w + 0.012, 3)
        _seed_symbol_bars(rstore, "BRN0Y", td_w, 68.0, [("09:30", 68.0), ("15:00", 68.0)], name="布伦特", market="ICE")
        pts_w = []
        for m_i in range(240):
            mv = (570 + m_i + 1) if m_i < 120 else (780 + (m_i - 120) + 1)
            pts_w.append((f"{mv // 60:02d}:{mv % 60:02d}", close_w))
        _seed_symbol_bars(rstore, "160723", td_w, pre_w, pts_w, name="嘉实原油")

    # Day 20261124 closes bear + high prem so overnight target at 14:53 is 0% (starts next morning flat = 0 shares)
    td_prev = "20261124"
    _seed_symbol_bars(rstore, "BRN0Y", td_prev, 68.0, [("09:30", 68.0), ("15:00", 68.0)], name="布伦特", market="ICE")
    pts_prev = [
        (f"{((570 + m_i + 1) if m_i < 120 else (780 + (m_i - 120) + 1)) // 60:02d}:{((570 + m_i + 1) if m_i < 120 else (780 + (m_i - 120) + 1)) % 60:02d}", 2.094)
        for m_i in range(240)
    ]
    _seed_symbol_bars(rstore, "160723", td_prev, 2.110, pts_prev, name="嘉实原油")

    # Day 20261125: starts flat (0 shares), gaps up to high premium (+1.5~+1.9%), pulls back to 11:13 @ 2.112 below VWAP 2.122,
    # turns up at 11:14 @ 2.114 (triggers pure-T0 intraday dip scalp buy), rallies to 14:06 @ 2.129 (triggers peak sell), settles at 2.108 (0% overnight)
    td_live = "20261125"
    brn_live = [("09:30", 68.0)]
    lof_rows: list[tuple[str, float, float]] = []
    for m_i in range(240):
        mv = (570 + m_i + 1) if m_i < 120 else (780 + (m_i - 120) + 1)
        hhmm = f"{mv // 60:02d}:{mv % 60:02d}"
        brn_live.append((hhmm, 68.0))
        if m_i < 25:  # 09:31..09:55 initial morning high 2.126
            px = 2.126
        elif m_i < 103:  # 09:56..11:13 smooth slide to 2.111
            px = round(2.126 - (m_i - 24) * (0.015 / 78.0), 3)
        elif m_i < 106:  # 11:14..11:16 rebound -> gc10 + dmc10 turn up below VWAP 2.122!
            px = 2.113 if m_i == 103 else (2.114 if m_i == 104 else 2.115)
        elif m_i < 170:  # hold steady ~2.115..2.118 until 13:50
            px = 2.116
        elif m_i <= 186:  # 13:51..14:06 afternoon surge to 2.129 (s_sig peak)
            px = round(2.116 + (m_i - 169) * (0.013 / 17.0), 3)
        else:  # 14:07..15:00 fade to 2.108
            px = 2.108
        avg_px = 2.122 if m_i < 160 else 2.120
        lof_rows.append((hhmm, px, avg_px))

    _seed_symbol_bars(rstore, "BRN0Y", td_live, 68.0, brn_live, name="布伦特", market="ICE")
    _seed_symbol_bars(rstore, "160723", td_live, 2.094, [(t, p) for t, p, _ in lof_rows], name="嘉实原油")
    # Update average_price to match real intraday VWAP trajectory
    with rstore._lock, rstore._connection:
        rstore._connection.executemany(
            "UPDATE research_points SET average_price=? WHERE symbol='160723' AND trade_date=? AND minute=?",
            [(f"{avg:.4f}", td_live, t) for t, _, avg in lof_rows],
        )

    rule_spec = {
        "id": "ai-160723-v9-plus-aggressive",
        "name": "V9+ 进取版",
        "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0002},
    }
    pos_sizing = {
        "mode": "cash_pool_rolling",
        "engine_profile": "v9_plus_aggressive",
        "default_weight_pct": 80.0,
        "max_position_pct": 100.0,
        "max_daily_entries": 8,
        "cooldown_minutes": 4,
        "scale_factor": 1.0,
        "close_flatten_time": "14:53",
    }
    bt = evaluate_v9_plus_backtest(
        rstore,
        rule_spec,
        {"start_date": "20261124", "round_trip_fee_pct": 0.01, "capital_cap": 100000.0},
        pos_sizing,
        symbol="160723",
        cleaned_end="20261125",
        allow_network=False,
    )
    csim = bt["capital_sim"]
    assert csim["ending_shares"] == 0
    day25 = next(d for d in csim["daily_ledger"] if d["trade_date"] == "20261125")
    assert day25["ending_shares"] == 0
    assert day25["buy_shares"] > 0
    assert day25["sell_shares"] == day25["buy_shares"]
    assert day25["day_pnl_amount"] > 0
    assert any("日内超跌做T" in str(o["tier"]) for o in day25["orders"])

    # Also verify live decision state machine emits v9-scalp when flat at the afternoon/morning dip,
    # then emits v9-exit when the afternoon spike hits, and enforces 0% at 14:53.
    dip_ord = next(o for o in day25["orders"] if o["side"] == "buy")
    sell_ord = next(o for o in day25["orders"] if o["side"] == "sell")
    # Simulate live polling right at dip_ord["time"] with 0 shares held
    with rstore._lock, rstore._connection:
        saved_later = rstore._connection.execute(
            "SELECT minute, price, average_price, volume FROM research_points WHERE symbol='160723' AND trade_date=? AND minute>?",
            (td_live, dip_ord["time"]),
        ).fetchall()
        rstore._connection.execute(
            "DELETE FROM research_points WHERE symbol='160723' AND trade_date=? AND minute>?",
            (td_live, dip_ord["time"]),
        )
    dec_buy = evaluate_v9_plus_live_decision(
        rstore,
        symbol="160723",
        trade_date=td_live,
        now_hhmm=dip_ord["time"],
        cap_limit=100000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=[],
        rolling_shares=0,
        allow_network=False,
    )
    assert len(dec_buy["intraday"]) == 1
    assert dec_buy["intraday"][0]["side"] == "buy"
    assert dec_buy["intraday"][0]["signal_tag"] == "v9-scalp"

    # Restore bars up to sell_ord["time"] and verify live decision exits the open scalp leg
    with rstore._lock, rstore._connection:
        rstore._connection.executemany(
            "INSERT INTO research_points(symbol, trade_date, minute, price, average_price, volume, captured_at) VALUES(?,?,?,?,?,?,?)",
            [("160723", td_live, r[0], r[1], r[2], r[3], "2026-11-25T15:00:00+08:00") for r in saved_later if str(r[0]) <= str(sell_ord["time"])],
        )
    dec_sell = evaluate_v9_plus_live_decision(
        rstore,
        symbol="160723",
        trade_date=td_live,
        now_hhmm=sell_ord["time"],
        cap_limit=100000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=[
            {
                "signal_key": f"160723:{td_live}:{dip_ord['time']}:v9-scalp:buy",
                "side": "buy",
                "quantity": int(dec_buy["intraday"][0]["qty_override"]),
                "signal_price": str(dip_ord["price"]),
                "minute": dip_ord["time"],
            }
        ],
        rolling_shares=int(dec_buy["intraday"][0]["qty_override"]),
        allow_network=False,
    )
    assert len(dec_sell["intraday"]) == 1
    assert dec_sell["intraday"][0]["side"] == "sell"
    assert dec_sell["intraday"][0]["signal_tag"] == "v9-exit"

    rstore.close()


def test_v9_plus_discount_brent_uptrend_guard_and_dynamic_short_cover(tmp_path: Path, monkeypatch) -> None:
    import json as _json
    import time as _time
    from level2_service.v9_plus_engine import (
        V9MacroDataStore,
        evaluate_v9_plus_live_decision,
        evaluate_v9_plus_live_indicators,
        is_discount_uptrend_sell_guarded,
        should_exit_short,
        signals_v8_regime_macd,
    )

    day_mock = {"ma5": 2.137, "ma15": 2.198}
    bar_low_spi = {
        "price": 2.174,
        "prem": -0.645,
        "sp_fx": 0.442,
        "sp_intra": 0.255,
        "coint10": 0.834,
        "brn_chg": 4.045,
        "brn_m5": 0.128,
        "macd10": 0.00057,
        "dmc10": 0.00018,
        "diff10": 0.00102,
        "dea10": 0.00073,
        "max_red12": 0.00046,
        "min_grn12": 0.0,
        "gc10": False,
        "dc10": False,
        "m1": 0.046,
        "m3": 0.184,
        "m5": 0.184,
        "iopv_intra": 0.10,
    }
    # 1. Discount zone + insufficient intraday overshoot (sp_intra < 0.32%) blocks early sell at sp_fx=+0.442%
    guarded_low_spi, _ = is_discount_uptrend_sell_guarded(bar_low_spi, 1.0)
    assert guarded_low_spi is True
    assert signals_v8_regime_macd(day_mock, bar_low_spi, 1.0)[0] is False

    # 2. Discount zone + surging Brent (brn_chg>=2% & brn_m5>=0.06%) blocks sell even when sp_intra>=0.32%
    bar_strong_brn = dict(bar_low_spi, price=2.180, sp_fx=0.545, sp_intra=0.357, brn_m5=0.215)
    guarded_brn, _ = is_discount_uptrend_sell_guarded(bar_strong_brn, 1.0)
    assert guarded_brn is True
    assert signals_v8_regime_macd(day_mock, bar_strong_brn, 1.0)[0] is False

    # 3. Released on extreme sp_fx >= +0.58% OR MACD dead-cross dc10=True
    bar_extreme = dict(bar_low_spi, price=2.182, sp_fx=0.646, sp_intra=0.459)
    assert is_discount_uptrend_sell_guarded(bar_extreme, 1.0)[0] is False
    assert signals_v8_regime_macd(day_mock, bar_extreme, 1.0)[0] is True

    bar_dc10 = dict(bar_low_spi, dc10=True, max_red12=0.0018)
    assert is_discount_uptrend_sell_guarded(bar_dc10, 1.0)[0] is False
    assert signals_v8_regime_macd(day_mock, bar_dc10, 1.0)[0] is True

    # 4. Dynamic short-leg cover in deep discount (prem <= -0.75%, spread dropped >= 0.28%, dmc10 > 0)
    assert should_exit_short({"prem": -0.81, "sp_fx": 0.34, "coint10": 0.73, "m5": -0.14, "macd10": -0.0002, "dmc10": -0.0001}, 0.442, True) is False
    assert should_exit_short({"prem": -0.80, "sp_fx": 0.15, "coint10": 0.15, "m5": -0.08, "macd10": -0.0004, "dmc10": -0.0001}, 0.442, True) is False
    assert should_exit_short({"prem": -0.80, "sp_fx": 0.15, "coint10": 0.15, "m5": -0.08, "macd10": -0.0004, "dmc10": 0.0001}, 0.442, True) is True
    assert should_exit_short({"prem": -0.80, "sp_fx": 0.15, "coint10": 0.15, "m5": -0.08, "macd10": -0.0004, "dmc10": 0.0001}, 0.442, False) is False

    # 5. Live state-machine & indicators verification for open reverse-T sell leg
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    rstore = SQLiteResearchStore(tmp_path / "v9p_guard_cover.db")
    V9MacroDataStore(rstore, allow_network=False)
    now_ts = _time.time()
    oil_rows = [
        ["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 70.0],
        ["20261125", int(datetime(2026, 11, 26, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 95.0],
    ]
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("nav_160723", _json.dumps({"20261124": 2.1881}), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("oil_daily_2y", _json.dumps(oil_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcny_series", _json.dumps({"ts": [int(datetime(2026, 10, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [6.70]}), now_ts),
        )

    def _full_day_pts(val: float) -> list[tuple[str, float]]:
        pts = []
        for idx in range(240):
            mv = (570 + idx + 1) if idx < 120 else (780 + (idx - 120) + 1)
            pts.append((f"{mv // 60:02d}:{mv % 60:02d}", val))
        return pts

    _seed_symbol_bars(rstore, "BRN0Y", "20261124", 90.70, [("09:30", 90.70), ("15:00", 90.70)], name="布伦特", market="ICE")
    _seed_symbol_bars(rstore, "160723", "20261124", 2.109, _full_day_pts(2.109), name="嘉实原油")
    _seed_symbol_bars(rstore, "BRN0Y", "20261125", 87.48, [("09:30", 87.48), ("15:00", 87.48)], name="布伦特", market="ICE")
    _seed_symbol_bars(rstore, "160723", "20261125", 2.109, _full_day_pts(2.109), name="嘉实原油")

    brn_26 = [("09:30", 90.775), ("11:08", 90.775), ("13:01", 91.15), ("13:15", 91.15)]
    lof_26: list[tuple[str, float]] = []
    for idx in range(120):
        mv = 570 + idx + 1
        hhmm = f"{mv // 60:02d}:{mv % 60:02d}"
        lof_26.append((hhmm, 2.174 if hhmm == "11:08" else 2.170))
    afternoon_prices = [
        ("13:01", 2.176), ("13:02", 2.176), ("13:03", 2.175), ("13:04", 2.175),
        ("13:05", 2.174), ("13:06", 2.173), ("13:07", 2.172), ("13:08", 2.171),
        ("13:09", 2.170), ("13:10", 2.169), ("13:11", 2.169), ("13:12", 2.170),
        ("13:13", 2.171), ("13:14", 2.172), ("13:15", 2.174),
    ]
    lof_26.extend(afternoon_prices)
    _seed_symbol_bars(rstore, "BRN0Y", "20261126", 87.48, brn_26, name="布伦特", market="ICE")
    _seed_symbol_bars(rstore, "160723", "20261126", 2.109, lof_26, name="嘉实原油")

    rule_spec = {
        "id": "ai-160723-v9-plus-aggressive",
        "name": "V9+ 进取版",
        "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0002},
    }
    pos_sizing = {
        "mode": "cash_pool_rolling",
        "engine_profile": "v9_plus_aggressive",
        "default_weight_pct": 80.0,
        "max_position_pct": 100.0,
        "max_daily_entries": 8,
        "cooldown_minutes": 4,
        "scale_factor": 1.0,
        "close_flatten_time": "14:53",
    }
    today_sigs = [
        {
            "signal_key": "160723:20261126:11:08:v9-entry:sell",
            "side": "sell",
            "quantity": 7800,
            "signal_price": "2.174",
            "minute": "11:08",
        }
    ]
    dec_cover = evaluate_v9_plus_live_decision(
        rstore,
        symbol="160723",
        trade_date="20261126",
        now_hhmm="13:15",
        cap_limit=150000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=today_sigs,
        rolling_shares=7900,
        allow_network=False,
    )
    assert len(dec_cover["intraday"]) == 1
    assert dec_cover["intraday"][0]["side"] == "buy"
    assert dec_cover["intraday"][0]["signal_tag"] == "v9-exit"
    assert dec_cover["intraday"][0]["qty_override"] == 7800
    assert "溢价-" in dec_cover["intraday"][0]["rule_summary"]

    ind = evaluate_v9_plus_live_indicators(
        rstore,
        symbol="160723",
        trade_date="20261126",
        now_hhmm="13:15",
        cap_limit=150000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=today_sigs,
        rolling_shares=7900,
        allow_network=False,
    )
    buy_ids = [c["id"] for c in ind["buy"]["conditions"]]
    sell_ids = [c["id"] for c in ind["sell"]["conditions"]]
    assert "v9-buy-cover" in buy_ids
    assert "v9-sell-brent-guard" in sell_ids
    cover_cond = next(c for c in ind["buy"]["conditions"] if c["id"] == "v9-buy-cover")
    assert cover_cond["matched"] is True
    assert "深折价" in cover_cond["target_rule"]
    sell_gate_cond = next(c for c in ind["sell"]["conditions"] if c["id"] == "v9-sell-gate")
    assert sell_gate_cond["matched"] is False
    assert "持反T空腿" in sell_gate_cond["current_value"]
    diag = ind["diagnostics"]
    assert diag["open_leg_side"] == "sell"
    assert diag["open_leg_qty"] == 7800
    assert diag["short_cover_gate_ok"] is True
    assert diag["short_entry_gate_ok"] is False
    assert diag["long_exit_gate_ok"] is False

    rstore.close()


def test_v9_plus_open_leg_external_reconcile_and_broker_cash_cap(tmp_path: Path, monkeypatch) -> None:
    import json as _json
    import time as _time
    import pytest as _pytest
    from level2_service.v9_plus_engine import (
        V9MacroDataStore,
        evaluate_v9_plus_live_decision,
        evaluate_v9_plus_live_indicators,
    )

    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    rstore = SQLiteResearchStore(tmp_path / "v9p_reconcile_cash.db")
    V9MacroDataStore(rstore, allow_network=False)
    now_ts = _time.time()
    oil_rows = [
        ["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 70.0],
        ["20261125", int(datetime(2026, 11, 26, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 95.0],
    ]
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("nav_160723", _json.dumps({"20261124": 2.1881}), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("oil_daily_2y", _json.dumps(oil_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcny_series", _json.dumps({"ts": [int(datetime(2026, 10, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [6.70]}), now_ts),
        )

    def _full_day_pts(val: float) -> list[tuple[str, float]]:
        pts = []
        for idx in range(240):
            mv = (570 + idx + 1) if idx < 120 else (780 + (idx - 120) + 1)
            pts.append((f"{mv // 60:02d}:{mv % 60:02d}", val))
        return pts

    _seed_symbol_bars(rstore, "BRN0Y", "20261124", 90.70, [("09:30", 90.70), ("15:00", 90.70)], name="布伦特", market="ICE")
    _seed_symbol_bars(rstore, "160723", "20261124", 2.109, _full_day_pts(2.109), name="嘉实原油")
    _seed_symbol_bars(rstore, "BRN0Y", "20261125", 87.48, [("09:30", 87.48), ("15:00", 87.48)], name="布伦特", market="ICE")
    _seed_symbol_bars(rstore, "160723", "20261125", 2.109, _full_day_pts(2.109), name="嘉实原油")

    brn_26 = [("09:30", 90.775), ("11:08", 90.775), ("13:01", 91.15), ("14:53", 86.00)]
    lof_26: list[tuple[str, float]] = []
    for idx in range(120):
        mv = 570 + idx + 1
        hhmm = f"{mv // 60:02d}:{mv % 60:02d}"
        lof_26.append((hhmm, 2.174 if hhmm == "11:08" else 2.170))
    for mm, px in [
        ("13:01", 2.176), ("13:02", 2.176), ("13:05", 2.174), ("13:08", 2.171),
        ("13:10", 2.169), ("13:12", 2.170), ("13:14", 2.172), ("13:15", 2.174),
        ("14:53", 2.174),
    ]:
        lof_26.append((mm, px))
    _seed_symbol_bars(rstore, "BRN0Y", "20261126", 87.48, brn_26, name="布伦特", market="ICE")
    _seed_symbol_bars(rstore, "160723", "20261126", 2.109, lof_26, name="嘉实原油")

    rule_spec = {
        "id": "ai-160723-v9-plus-aggressive",
        "name": "V9+ 进取版",
        "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0002},
    }
    pos_sizing = {
        "mode": "cash_pool_rolling",
        "engine_profile": "v9_plus_aggressive",
        "default_weight_pct": 80.0,
        "max_position_pct": 100.0,
        "max_daily_entries": 8,
        "cooldown_minutes": 4,
        "scale_factor": 1.0,
        "close_flatten_time": "14:53",
    }
    today_sigs = [
        {
            "signal_key": "160723:20261126:11:08:v9-entry:sell",
            "side": "sell",
            "quantity": 7800,
            "signal_price": "2.174",
            "minute": "11:08",
        }
    ]

    # 1. Partial manual buyback (3,000 shares externally bought -> 10,900 vs expected 7,900) trims open_leg to 4,800
    ind_partial = evaluate_v9_plus_live_indicators(
        rstore,
        symbol="160723",
        trade_date="20261126",
        now_hhmm="13:15",
        cap_limit=150000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=today_sigs,
        rolling_shares=10900,
        broker_available_cash=200000.0,
        expected_ledger_shares=7900,
        allow_network=False,
    )
    assert ind_partial is not None
    assert ind_partial["diagnostics"]["open_leg_side"] == "sell"
    assert ind_partial["diagnostics"]["open_leg_qty"] == 4800

    # 2. Full manual buyback (15,700 vs expected 7,900) closes open_leg cleanly and 14:53 respects broker_available_cash=30,000
    ind_full = evaluate_v9_plus_live_indicators(
        rstore,
        symbol="160723",
        trade_date="20261126",
        now_hhmm="14:53",
        cap_limit=150000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=today_sigs,
        rolling_shares=15700,
        broker_available_cash=30000.0,
        expected_ledger_shares=7900,
        allow_network=False,
    )
    assert ind_full is not None
    assert ind_full["diagnostics"]["open_leg_side"] is None
    assert ind_full["diagnostics"]["broker_available_cash"] == 30000.0
    assert ind_full["diagnostics"]["effective_avail_cash"] == _pytest.approx(29985.0)

    dec_close = evaluate_v9_plus_live_decision(
        rstore,
        symbol="160723",
        trade_date="20261126",
        now_hhmm="14:53",
        cap_limit=150000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=today_sigs,
        rolling_shares=15700,
        broker_available_cash=30000.0,
        expected_ledger_shares=7900,
        allow_network=False,
    )
    assert dec_close["intraday"] == []
    assert dec_close["close_rebalance"] is not None
    assert dec_close["close_rebalance"]["side"] == "buy"
    max_affordable = int((30000.0 * 0.9995) // (2.174 * 100)) * 100
    assert dec_close["close_rebalance"]["qty_override"] == max_affordable

    # 3. External long sell clears positive-T buy leg cleanly
    buy_sigs = [
        {
            "signal_key": "160723:20261126:10:10:v9-entry:buy",
            "side": "buy",
            "quantity": 5000,
            "signal_price": "2.170",
            "minute": "10:10",
        }
    ]
    ind_long_cleared = evaluate_v9_plus_live_indicators(
        rstore,
        symbol="160723",
        trade_date="20261126",
        now_hhmm="13:15",
        cap_limit=150000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=buy_sigs,
        rolling_shares=0,
        broker_available_cash=50000.0,
        expected_ledger_shares=5000,
        allow_network=False,
    )
    assert ind_long_cleared is not None
    assert ind_long_cleared["diagnostics"]["open_leg_side"] is None

    rstore.close()


def test_v9_plus_live_warmup_preserves_20d_brent_quantile(tmp_path: Path, monkeypatch) -> None:
    import json as _json
    import time as _time
    from datetime import timedelta as _timedelta
    from level2_service.v9_plus_engine import (
        V9MacroDataStore,
        evaluate_v9_plus_backtest,
        evaluate_v9_plus_live_decision,
        evaluate_v9_plus_live_indicators,
        resolve_effective_close_weight,
        v9_pro_close_weight,
    )

    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    rstore = SQLiteResearchStore(tmp_path / "v9p_warmup_20d.db")
    V9MacroDataStore(rstore, allow_network=False)
    now_ts = _time.time()
    oil_rows = [
        ["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 60.0],
        ["20261101", int(datetime(2026, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 110.0],
    ]
    nav_payload = {}
    base_d = datetime(2026, 10, 28, tzinfo=SHANGHAI_TZ)
    for k in range(25):
        nav_payload[(base_d + _timedelta(days=k)).strftime("%Y%m%d")] = 2.250
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("nav_160723", _json.dumps(nav_payload), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("oil_daily_2y", _json.dumps(oil_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcny_series", _json.dumps({"ts": [int(datetime(2026, 10, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [6.70]}), now_ts),
        )

    def _day_mins(val: float) -> list[tuple[str, float]]:
        pts = []
        for idx in range(240):
            mv = (570 + idx + 1) if idx < 120 else (780 + (idx - 120) + 1)
            pts.append((f"{mv // 60:02d}:{mv % 60:02d}", val))
        return pts

    # Seed 16 prior warmup days (20261103..20261118): early day 20261105 hits BRN=108.0 (96.0% >= 72%),
    # then cools to BRN=88.0 (56.0%) while 160723 closes decline from 2.320 to 2.140 -> MA5(2.164) < MA15(2.224).
    for idx in range(16):
        td_w = f"202611{3 + idx:02d}"
        brn_w = 108.0 if td_w == "20261105" else 88.0
        close_w = round(2.320 - idx * 0.012, 3)
        pre_w = round(close_w + 0.012, 3)
        _seed_symbol_bars(rstore, "BRN0Y", td_w, brn_w, [("09:30", brn_w), ("14:53", brn_w), ("15:00", brn_w)], name="布伦特", market="ICE")
        _seed_symbol_bars(rstore, "160723", td_w, pre_w, _day_mins(close_w), name="嘉实原油")

    # Target day 20261119: BRN=90.0 (60.0% quantile < 72%), 160723 rises from 2.140 to 2.190 (MA5 < 2.190 < MA15).
    # Note: BRN +2.27% (88->90) while LOF +2.34% keeps sp_fx near 0 to isolate the 14:53 close rebalance decision.
    td_target = "20261119"
    _seed_symbol_bars(rstore, "BRN0Y", td_target, 88.0, [("09:30", 90.0), ("14:53", 90.0), ("15:00", 90.0)], name="布伦特", market="ICE")
    _seed_symbol_bars(rstore, "160723", td_target, 2.140, _day_mins(2.190), name="嘉实原油")

    rule_spec = {
        "id": "ai-160723-v9-plus-aggressive",
        "name": "V9+ 进取版",
        "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0002},
    }
    pos_sizing = {
        "mode": "cash_pool_rolling",
        "engine_profile": "v9_plus_aggressive",
        "default_weight_pct": 80.0,
        "max_position_pct": 100.0,
        "max_daily_entries": 8,
        "cooldown_minutes": 4,
        "scale_factor": 1.0,
        "close_flatten_time": "14:53",
    }

    # Case A: Below MA15 (2.190 < 2.224) — full-history backtest and single-day live evaluation must agree on 96.0% peak and 0% close weight.
    bt_below = evaluate_v9_plus_backtest(
        rstore,
        rule_spec,
        {"start_date": "20261103", "round_trip_fee_pct": 0.01, "capital_cap": 100000.0},
        pos_sizing,
        symbol="160723",
        cleaned_end=td_target,
        allow_network=False,
    )
    assert bt_below["capital_sim"]["ending_shares"] == 0

    ind_below = evaluate_v9_plus_live_indicators(
        rstore,
        symbol="160723",
        trade_date=td_target,
        now_hhmm="14:53",
        cap_limit=100000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=[],
        rolling_shares=7900,
        allow_network=False,
        broker_available_cash=150000.0,
    )
    assert ind_below is not None
    assert ind_below["diagnostics"]["max_bpos_20d_pct"] == 96.0
    assert ind_below["diagnostics"]["close_target_weight_pct"] == 0.0
    assert ind_below["diagnostics"]["min_retained_shares"] == 100
    assert ind_below["diagnostics"]["close_target_shares"] == 100
    assert "高油价回落确认空头趋势" in ind_below["diagnostics"]["close_target_reason"]

    dec_below = evaluate_v9_plus_live_decision(
        rstore,
        symbol="160723",
        trade_date=td_target,
        now_hhmm="14:53",
        cap_limit=100000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=[],
        rolling_shares=7900,
        allow_network=False,
        broker_available_cash=150000.0,
    )
    assert dec_below["diagnostics"]["max_bpos_20d_pct"] == 96.0
    assert dec_below["diagnostics"]["close_target_weight_pct"] == 0.0
    assert dec_below["diagnostics"]["min_retained_shares"] == 100
    assert dec_below["diagnostics"]["close_target_shares"] == 100
    assert dec_below["close_rebalance"] is not None
    assert dec_below["close_rebalance"]["side"] == "sell"
    assert dec_below["close_rebalance"]["qty_override"] == 7800

    # Case B: Reclaiming MA15 (2.230 >= 2.224) — lifts bear lock cleanly in BOTH backtest and single-day live eval.
    pts_reclaim = [(hhmm, 2.230 if hhmm >= "14:53" else 2.190) for hhmm, _ in _day_mins(2.190)]
    _seed_symbol_bars(rstore, "160723", td_target, 2.140, pts_reclaim, name="嘉实原油")

    bt_above = evaluate_v9_plus_backtest(
        rstore,
        rule_spec,
        {"start_date": "20261103", "round_trip_fee_pct": 0.01, "capital_cap": 100000.0},
        pos_sizing,
        symbol="160723",
        cleaned_end=td_target,
        allow_network=False,
    )
    day_target_bt = {
        "ma5": ind_below["diagnostics"]["ma5"],
        "ma15": ind_below["diagnostics"]["ma15"],
        "max_bpos_20d": 0.96,
    }
    bt_wt, _ = resolve_effective_close_weight(
        v9_pro_close_weight,
        day_target_bt,
        {"brn_pos": 0.60, "prem": -2.80, "price": 2.230},
        0,
        0.0,
    )
    assert bt_above["capital_sim"]["ending_shares"] > 0

    ind_above = evaluate_v9_plus_live_indicators(
        rstore,
        symbol="160723",
        trade_date=td_target,
        now_hhmm="14:53",
        cap_limit=100000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=[],
        rolling_shares=7900,
        allow_network=False,
        broker_available_cash=150000.0,
    )
    assert ind_above is not None
    assert ind_above["diagnostics"]["max_bpos_20d_pct"] == 96.0
    assert ind_above["diagnostics"]["close_target_weight_pct"] == round(bt_wt * 100.0, 1)
    assert ind_above["diagnostics"]["close_target_weight_pct"] >= 80.0

    dec_above = evaluate_v9_plus_live_decision(
        rstore,
        symbol="160723",
        trade_date=td_target,
        now_hhmm="14:53",
        cap_limit=100000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=[],
        rolling_shares=7900,
        allow_network=False,
        broker_available_cash=150000.0,
    )
    assert dec_above["diagnostics"]["close_target_weight_pct"] == ind_above["diagnostics"]["close_target_weight_pct"]
    assert dec_above["close_rebalance"] is not None
    assert dec_above["close_rebalance"]["side"] == "buy"
    assert dec_above["close_rebalance"]["qty_override"] > 0

    rstore.close()


def test_v9_plus_161129_tuning_and_default_isolation(tmp_path: Path, monkeypatch) -> None:
    import json as _json
    import time as _time
    from datetime import timedelta as _timedelta
    from level2_service.quant_rules import normalize_position_sizing
    from level2_service.v9_plus_engine import (
        V9MacroDataStore,
        apply_v9_close_weight_tuning,
        apply_v9_sell_tier_tuning,
        check_v9_tuning_sell_guard,
        clean_sell_tier,
        evaluate_v9_plus_live_decision,
        evaluate_v9_plus_live_indicators,
        should_enter_intraday_dip_scalp,
        v9_pro_close_weight,
    )

    # 1. Default normalization preserves exact keys without injecting v9_tuning
    default_ps = normalize_position_sizing({})
    assert "v9_tuning" not in default_ps
    tuned_ps = normalize_position_sizing(
        {
            "mode": "cash_pool_rolling",
            "engine_profile": "v9_plus_aggressive",
            "v9_tuning": {
                "bear_high_prem_flat_pct": 2.5,
                "bull_oil_surge_guard": True,
                "bull_oil_surge_tier_cap_pct": 50.0,
                "scalp_strict_mode": True,
            },
        }
    )
    assert tuned_ps["v9_tuning"] == {
        "bear_high_prem_flat_pct": 2.5,
        "bull_oil_surge_guard": True,
        "bull_oil_surge_tier_cap_pct": 50.0,
        "scalp_strict_mode": True,
    }

    # 2. Bear high-premium close guard clears to 0% (while default keeps 85% at low brn_pos=0.30)
    day_bear = {"ma5": 1.650, "ma15": 1.780, "max_bpos_20d": 0.65}
    bar_bear_prem = {"brn_pos": 0.30, "prem": 3.20, "price": 1.560}
    w_def, _ = v9_pro_close_weight(day_bear, bar_bear_prem, 0)
    assert w_def == 0.85
    wrapped_ov = apply_v9_close_weight_tuning(v9_pro_close_weight, tuned_ps["v9_tuning"])
    w_tuned, lbl_tuned = wrapped_ov(day_bear, bar_bear_prem, 0)
    assert w_tuned == 0.0
    assert "防杀溢价清仓" in lbl_tuned

    # 3. Bull oil surge guard & tier cap
    assert check_v9_tuning_sell_guard(
        {"sp_fx": 0.02, "sp_intra": 0.15, "brn_chg": 0.5, "dc10": True},
        tuned_ps["v9_tuning"],
    )[0] is True
    assert check_v9_tuning_sell_guard(
        {"sp_fx": 0.80, "sp_intra": 0.01, "brn_chg": 3.0, "dc10": False},
        tuned_ps["v9_tuning"],
    )[0] is True
    assert check_v9_tuning_sell_guard(
        {"sp_fx": 0.80, "sp_intra": 0.45, "brn_chg": 3.0, "dc10": False},
        tuned_ps["v9_tuning"],
    )[0] is False
    base_lbl, base_frac = clean_sell_tier({"ma5": 1.800}, {"prem": 5.0, "sp_fx": 0.80, "max_red12": 0.0020})
    assert base_frac == 1.0
    capped_lbl, capped_frac = apply_v9_sell_tier_tuning(
        {"ma5": 1.800},
        {"price": 1.850, "brn_chg": 3.0, "sp_intra": 0.45},
        base_lbl,
        base_frac,
        tuned_ps["v9_tuning"],
    )
    assert capped_frac == 0.5
    assert "半仓保护" in capped_lbl

    # 4. Strict scalp filters late/non-lagging/shallow dips while allowing deep genuine dips
    dip_base = {
        "time": "11:10",
        "price": 1.750,
        "vdev": -0.40,
        "px_dd_hi": 0.55,
        "sp_dd_hi": 0.30,
        "prem": 1.20,
        "gc10": True,
        "macd10": -0.0002,
        "dmc10": 0.0003,
        "min_grn12": -0.0010,
        "brn_m5": 0.02,
        "m1": 0.01,
        "sp_fx": 0.30,
        "sp_intra": 0.10,
    }
    assert should_enter_intraday_dip_scalp(dip_base, strict_mode=False)[0] is True
    assert should_enter_intraday_dip_scalp(dip_base, day=day_bear, strict_mode=True)[0] is False
    assert should_enter_intraday_dip_scalp(
        {**dip_base, "time": "14:05", "vdev": -0.55, "sp_fx": -0.25, "sp_intra": -0.35},
        day={"ma5": 1.76, "ma15": 1.75},
        strict_mode=True,
    )[0] is False
    assert should_enter_intraday_dip_scalp(
        {**dip_base, "time": "11:10", "vdev": -0.55, "sp_fx": -0.25, "sp_intra": -0.35},
        day={"ma5": 1.76, "ma15": 1.75},
        strict_mode=True,
    )[0] is True

    # 5. Live decision & indicators end-to-end verification on 161129
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    rstore = SQLiteResearchStore(tmp_path / "v9p_161129_live.db")
    V9MacroDataStore(rstore, allow_network=False)
    now_ts = _time.time()
    oil_rows = [
        ["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 60.0],
        ["20261101", int(datetime(2026, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 110.0],
    ]
    nav_payload = {
        (datetime(2026, 10, 28, tzinfo=SHANGHAI_TZ) + _timedelta(days=k)).strftime("%Y%m%d"): 1.500
        for k in range(25)
    }
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("nav_161129", _json.dumps(nav_payload), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("oil_daily_2y", _json.dumps(oil_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcny_series", _json.dumps({"ts": [int(datetime(2026, 10, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [6.70]}), now_ts),
        )

    def _day_mins(val: float) -> list[tuple[str, float]]:
        return [
            (f"{((570 + i + 1) if i < 120 else (780 + (i - 120) + 1)) // 60:02d}:{((570 + i + 1) if i < 120 else (780 + (i - 120) + 1)) % 60:02d}", val)
            for i in range(240)
        ]

    for idx in range(16):
        td_w = f"202611{3 + idx:02d}"
        close_w = round(1.820 - idx * 0.012, 3)
        _seed_symbol_bars(rstore, "BRN0Y", td_w, 75.0, [("09:30", 75.0), ("14:53", 75.0), ("15:00", 75.0)], name="布伦特", market="ICE")
        _seed_symbol_bars(rstore, "161129", td_w, round(close_w + 0.012, 3), _day_mins(close_w), name="原油基金")

    # Target day: brn=75.0 (brn_pos=0.30 <= 0.42), 161129=1.560 (< ma5 < ma15) with +4.0% prem
    td_target = "20261119"
    _seed_symbol_bars(rstore, "BRN0Y", td_target, 75.0, [("09:30", 75.0), ("14:53", 75.0), ("15:00", 75.0)], name="布伦特", market="ICE")
    _seed_symbol_bars(rstore, "161129", td_target, 1.560, _day_mins(1.560), name="原油基金")

    rule_spec = {
        "id": "ai-161129-v9-plus-aggressive",
        "name": "161129专享",
        "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0002},
    }
    base_ps = {
        "mode": "cash_pool_rolling",
        "engine_profile": "v9_plus_aggressive",
        "default_weight_pct": 80.0,
        "max_position_pct": 100.0,
        "max_daily_entries": 8,
        "cooldown_minutes": 4,
        "scale_factor": 1.0,
        "close_flatten_time": "14:53",
    }
    ind_default = evaluate_v9_plus_live_indicators(
        rstore,
        symbol="161129",
        trade_date=td_target,
        now_hhmm="14:53",
        cap_limit=50000.0,
        rule_spec=rule_spec,
        pos_sizing=base_ps,
        today_signals=[],
        rolling_shares=5000,
        allow_network=False,
    )
    assert ind_default["diagnostics"]["close_target_weight_pct"] == 85.0

    ind_tuned = evaluate_v9_plus_live_indicators(
        rstore,
        symbol="161129",
        trade_date=td_target,
        now_hhmm="14:53",
        cap_limit=50000.0,
        rule_spec=rule_spec,
        pos_sizing=tuned_ps,
        today_signals=[],
        rolling_shares=5000,
        allow_network=False,
    )
    assert ind_tuned["diagnostics"]["close_target_weight_pct"] == 0.0
    assert ind_tuned["diagnostics"]["close_target_shares"] == 100
    assert "防杀溢价清仓" in ind_tuned["diagnostics"]["close_target_reason"]
    scalp_cond = next(c for c in ind_tuned["buy"]["conditions"] if c["id"] == "v9-buy-scalp")
    assert "去泡沫严选" in scalp_cond["label"]
    assert "vdev≤-0.50%" in scalp_cond["target_rule"]

    dec_tuned = evaluate_v9_plus_live_decision(
        rstore,
        symbol="161129",
        trade_date=td_target,
        now_hhmm="14:53",
        cap_limit=50000.0,
        rule_spec=rule_spec,
        pos_sizing=tuned_ps,
        today_signals=[],
        rolling_shares=5000,
        allow_network=False,
    )
    assert dec_tuned["close_rebalance"] is not None
    assert dec_tuned["close_rebalance"]["side"] == "sell"
    assert dec_tuned["close_rebalance"]["qty_override"] == 4900
    assert "溢价泡沫" in dec_tuned["close_rebalance"]["push_why"]

    rstore.close()


def test_evaluate_and_save_rule_preserves_owner_symbol_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "owner_snapshot.db"
    rstore = SQLiteResearchStore(db_path)
    qstore = SQLiteQuantRuleStore(db_path)

    p_23 = [2.100] * 10 + [2.095, 2.090, 2.085, 2.092, 2.102, 2.110, 2.115, 2.112, 2.108, 2.105] + [2.105] * 20
    p_29 = [1.800] * 10 + [1.795, 1.790, 1.785, 1.792, 1.802, 1.810, 1.815, 1.812, 1.808, 1.805] + [1.805] * 20
    _seed_symbol_bars(
        rstore,
        "160723",
        "20260702",
        2.100,
        [(f"{((570 + i + 1) if i < 120 else (780 + (i - 120) + 1)) // 60:02d}:{((570 + i + 1) if i < 120 else (780 + (i - 120) + 1)) % 60:02d}", v) for i, v in enumerate(p_23)],
        name="嘉实原油",
    )
    _seed_symbol_bars(
        rstore,
        "161129",
        "20260702",
        1.800,
        [(f"{((570 + i + 1) if i < 120 else (780 + (i - 120) + 1)) // 60:02d}:{((570 + i + 1) if i < 120 else (780 + (i - 120) + 1)) % 60:02d}", v) for i, v in enumerate(p_29)],
        name="原油基金",
    )

    saved = qstore.upsert_rule(
        {
            "id": "ai-160723-owner-check",
            "name": "160723专属规则",
            "source": "ai",
            "symbols": ["160723", "BRN0Y"],
            "buy_rule": {"title": "金叉", "joiner": "and", "conditions": [{"type": "macd_signal", "signal": "red"}]},
            "sell_rule": {"title": "死叉", "joiner": "and", "conditions": [{"type": "macd_signal", "signal": "green"}]},
            "eval_criteria": {"start_date": "20260701", "capital_cap": 20000.0},
        },
        research_store=rstore,
    )
    assert saved["latest_eval"]["symbol"] == "160723"
    assert saved["latest_eval"]["capital_sim"]["capital_cap"] == 20000.0

    # Cross-symbol ad-hoc re-eval on 161129 returns 161129 evaluation but preserves 160723 snapshot in DB
    cross_res = qstore.evaluate_and_save_rule(
        "ai-160723-owner-check",
        rstore,
        symbol="161129",
        capital_cap=50000.0,
    )
    assert cross_res["evaluation"]["symbol"] == "161129"
    assert cross_res["evaluation"]["capital_sim"]["capital_cap"] == 50000.0
    assert cross_res["rule"]["latest_eval"]["symbol"] == "160723"
    assert cross_res["rule"]["latest_eval"]["capital_sim"]["capital_cap"] == 20000.0
    assert qstore.get_rule("ai-160723-owner-check")["latest_eval"]["symbol"] == "160723"

    # Re-evaluating on owner symbol 160723 updates persisted snapshot as expected
    owner_res = qstore.evaluate_and_save_rule(
        "ai-160723-owner-check",
        rstore,
        symbol="160723",
        capital_cap=30000.0,
    )
    assert owner_res["rule"]["latest_eval"]["symbol"] == "160723"
    assert owner_res["rule"]["latest_eval"]["capital_sim"]["capital_cap"] == 30000.0

    rstore.close()
    qstore.close()


def test_v9_plus_xop_anchor_162411_pricing_overnight_and_close_weight(tmp_path: Path, monkeypatch) -> None:
    import json as _json
    import time as _time
    from datetime import timedelta as _timedelta
    from level2_service.quant_rules import normalize_position_sizing
    from level2_service.v9_plus_engine import (
        V9MacroDataStore,
        _parse_sina_mink_payload,
        _parse_us_daily_payload,
        _parse_yahoo_daily_payload,
        apply_xop_overnight_intraday_overlays,
        evaluate_v9_plus_backtest,
        evaluate_v9_plus_live_decision,
        evaluate_v9_plus_live_indicators,
        v9_xop_close_weight,
    )

    # 1. Default normalization stays clean; xop_us_ep preserves cross_market_anchor
    assert "cross_market_anchor" not in normalize_position_sizing({})
    xop_ps = normalize_position_sizing(
        {
            "mode": "cash_pool_rolling",
            "engine_profile": "v9_plus_aggressive",
            "cross_market_anchor": "xop_us_ep",
            "default_weight_pct": 80.0,
            "max_position_pct": 100.0,
            "max_daily_entries": 8,
            "cooldown_minutes": 4,
            "scale_factor": 1.0,
            "close_flatten_time": "14:53",
        }
    )
    assert xop_ps["cross_market_anchor"] == "xop_us_ep"

    # 2. Causal US daily + Yahoo fallback + Sina 1m parsers
    us_rows = _parse_us_daily_payload(
        'var x=([{"d":"2026-11-24","c":"166.26"},{"d":"2026-11-25","c":"167.93"}]);'
    )
    assert len(us_rows) == 2
    assert us_rows[1][0] == "20261125"
    assert us_rows[1][1] == int(datetime(2026, 11, 26, 6, 0, tzinfo=SHANGHAI_TZ).timestamp())
    assert us_rows[1][2] == 167.93

    yh_rows = _parse_yahoo_daily_payload(
        _json.dumps(
            {
                "chart": {
                    "result": [
                        {
                            "timestamp": [
                                int(datetime(2026, 11, 25, 20, 0, tzinfo=SHANGHAI_TZ).timestamp()),
                            ],
                            "indicators": {"quote": [{"close": [167.93]}]},
                        }
                    ]
                }
            }
        )
    )
    assert yh_rows[0][0] == "20261125" and yh_rows[0][2] == 167.93

    mink_parsed = _parse_sina_mink_payload(
        'var x=([{"d":"2026-11-26 05:00:00","c":"3.650"},{"d":"2026-11-26 14:53:00","c":"3.610"}]);'
    )
    assert len(mink_parsed["ts"]) == 2 and mink_parsed["px"] == [3.65, 3.61]

    # 3. Overnight XOP / SPY / NG intraday overlays
    strong_day = {"xop_ov": 2.4, "spy_ov": 0.4, "ng_ov": 1.2, "xop_vs_oil_alpha": 1.4}
    s1, b1, sc1, meta1 = apply_xop_overnight_intraday_overlays(
        strong_day,
        {
            "time": "09:45",
            "prem": -0.55,
            "sp_fx": -0.35,
            "dmc10": 0.0002,
            "m1": 0.02,
            "dc10": False,
        },
        s_sig=False,
        b_sig=False,
        scalp_ok=False,
        close_wt=0.80,
        sc=1.0,
    )
    assert b1 is True and meta1["buy_catchup_xop"] is True

    s2, _, _, meta2 = apply_xop_overnight_intraday_overlays(
        strong_day,
        {
            "time": "10:10",
            "prem": -0.25,
            "sp_fx": 0.48,
            "dmc10": 0.0001,
            "m1": 0.05,
            "dc10": False,
        },
        s_sig=True,
        b_sig=False,
        scalp_ok=False,
        close_wt=0.80,
        sc=1.0,
    )
    assert s2 is False and meta2["sell_guarded_xop"] is True

    weak_day = {"xop_ov": -2.1, "spy_ov": -1.3, "ng_ov": -0.5, "xop_vs_oil_alpha": -1.2}
    s3, b3, sc3, meta3 = apply_xop_overnight_intraday_overlays(
        weak_day,
        {
            "time": "09:50",
            "prem": 0.45,
            "sp_fx": 0.35,
            "dmc10": -0.0003,
            "m1": -0.05,
            "dc10": True,
        },
        s_sig=False,
        b_sig=True,
        scalp_ok=True,
        close_wt=0.50,
        sc=1.0,
    )
    assert b3 is False and meta3["buy_knife_blocked_xop"] is True
    assert sc3 is False and meta3["scalp_blocked_xop"] is True
    assert s3 is True and meta3["sell_weak_ov_triggered"] is True

    # 4. 14:53 v9_xop_close_weight decision matrix
    base_day = {
        "ma5": 0.870,
        "ma15": 0.860,
        "max_bpos_20d": 0.60,
        "max_xpos_20d": 0.65,
        "xop_bull": True,
        "xop_above_ma5": True,
    }
    w_prem, _ = v9_xop_close_weight(
        base_day, {"brn_pos": 0.55, "xop_pos": 0.60, "prem": 0.90, "price": 0.880, "daytime_lead_pct": 0.10}, 0
    )
    assert w_prem == 0.0
    w_rollover, _ = v9_xop_close_weight(
        {**base_day, "xop_above_ma5": False},
        {"brn_pos": 0.55, "xop_pos": 0.60, "prem": 0.05, "price": 0.875, "daytime_lead_pct": -0.60},
        0,
    )
    assert w_rollover == 0.0
    w_lead_up, _ = v9_xop_close_weight(
        base_day, {"brn_pos": 0.55, "xop_pos": 0.60, "prem": -0.20, "price": 0.875, "daytime_lead_pct": 0.65}, 0
    )
    assert w_lead_up == 1.0

    # 5. Full end-to-end offline store: XOP IOPV precision, rule isolation, 20d XOP peak warmup consistency & 100-share floor
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    rstore = SQLiteResearchStore(tmp_path / "v9p_xop_162411.db")
    qstore = SQLiteQuantRuleStore(tmp_path / "v9p_xop_162411.db")
    V9MacroDataStore(rstore, allow_network=False)
    now_ts = _time.time()

    oil_rows = [
        ["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 60.0],
        ["20261101", int(datetime(2026, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 100.0],
    ]
    # XOP 2y range [120 .. 200]; early warmup peaks at 190.0 (87.5% >= 80%), then cools to 160.0 (50.0%) while MA5 < MA15
    xop_rows = [
        ["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 120.0],
        ["20251102", int(datetime(2025, 11, 3, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 200.0],
    ]
    spy_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 600.0]]
    ng_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 3.00]]
    nav_payload = {}

    def _day_mins(val: float) -> list[tuple[str, float]]:
        return [
            (f"{((570 + i + 1) if i < 120 else (780 + (i - 120) + 1)) // 60:02d}:{((570 + i + 1) if i < 120 else (780 + (i - 120) + 1)) % 60:02d}", val)
            for i in range(240)
        ]

    for idx in range(16):
        us_d = (datetime(2026, 11, 2, tzinfo=SHANGHAI_TZ) + _timedelta(days=idx)).strftime("%Y%m%d")
        a_d = (datetime(2026, 11, 3, tzinfo=SHANGHAI_TZ) + _timedelta(days=idx)).strftime("%Y%m%d")
        causal_ts = int((datetime.strptime(us_d, "%Y%m%d").replace(tzinfo=SHANGHAI_TZ) + _timedelta(days=1, hours=6)).timestamp())
        x_val = 190.0 if idx == 2 else (175.0 - idx * 1.0)
        xop_rows.append([us_d, causal_ts, x_val])
        spy_rows.append([us_d, causal_ts, 700.0])
        ng_rows.append([us_d, causal_ts, 3.50])
        nav_payload[us_d] = 0.900
        close_w = round(0.950 - idx * 0.006, 3)
        _seed_symbol_bars(rstore, "BRN0Y", a_d, 80.0, [("09:30", 80.0), ("14:53", 80.0), ("15:00", 80.0)], name="布伦特", market="ICE")
        _seed_symbol_bars(rstore, "162411", a_d, round(close_w + 0.006, 3), _day_mins(close_w), name="华宝油气")

    # Target day 20261119: previous US close 20261118 surges +3.0% (160 -> 164.8) while BRN stays flat at 80.0!
    td_target = "20261119"
    nav_payload["20261117"] = 0.860
    xop_rows.append(["20261117", int(datetime(2026, 11, 18, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 160.0])
    xop_rows.append(["20261118", int(datetime(2026, 11, 19, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 164.8])
    spy_rows.append(["20261118", int(datetime(2026, 11, 19, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 705.0])
    ng_rows.append(["20261118", int(datetime(2026, 11, 19, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 3.60])
    _seed_symbol_bars(rstore, "BRN0Y", td_target, 80.0, [("09:30", 80.0), ("14:53", 80.0), ("15:00", 80.0)], name="布伦特", market="ICE")
    _seed_symbol_bars(rstore, "162411", td_target, 0.860, _day_mins(0.860), name="华宝油气")

    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("nav_162411", _json.dumps(nav_payload), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("oil_daily_2y", _json.dumps(oil_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("xop_daily_2y", _json.dumps(xop_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("spy_daily_2y", _json.dumps(spy_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("ng_daily_2y", _json.dumps(ng_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcny_series", _json.dumps({"ts": [int(datetime(2026, 10, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [6.70]}), now_ts),
        )

    saved_xop = qstore.upsert_rule(
        {
            "id": "ai-162411-v9-xop-aggressive",
            "name": "V9+ 华宝油气专版 · XOP隔夜油气汇调三态量化",
            "source": "ai",
            "symbols": ["162411", "BRN0Y"],
            "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0002},
            "buy_rule": {"title": "XOP折价正T", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "lte", "value": -0.25}]},
            "sell_rule": {"title": "XOP溢价反T", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "gte", "value": 0.42}]},
            "position_sizing": xop_ps,
            "eval_criteria": {"start_date": "20261103", "round_trip_fee_pct": 0.01, "capital_cap": 50000.0},
        },
        research_store=rstore,
    )
    assert saved_xop["position_sizing"]["cross_market_anchor"] == "xop_us_ep"
    assert saved_xop["latest_eval"]["coverage"]["evaluated_days"] == 17

    ind_xop = evaluate_v9_plus_live_indicators(
        rstore,
        symbol="162411",
        trade_date=td_target,
        now_hhmm="14:53",
        cap_limit=50000.0,
        rule_spec=saved_xop,
        pos_sizing=saved_xop["position_sizing"],
        today_signals=[],
        rolling_shares=5000,
        allow_network=False,
    )
    assert ind_xop is not None
    diag = ind_xop["diagnostics"]
    assert diag["cross_market_anchor"] == "xop_us_ep"
    assert diag["max_xpos_20d_pct"] == 87.5  # Warmup branch tracked early 190.0 XOP peak accurately!
    # XOP surged +3.0% overnight so XOP-anchored IOPV ~ 0.884 -> 162411 at 0.860 is deeply discounted (~-2.7% prem)
    assert diag["prem_pct"] < -2.0
    assert diag["xop_overnight_pct"] == 3.0
    assert any(card["key"] == "xop" for card in ind_xop["market_indicators"])
    assert any(c["id"] == "v9-buy-xop-guard" for c in ind_xop["buy"]["conditions"])
    assert any(c["id"] == "v9-sell-xop-guard" for c in ind_xop["sell"]["conditions"])

    # Verify high-premium 14:53 clearing retains 100 shares on 162411
    _seed_symbol_bars(rstore, "162411", td_target, 0.860, _day_mins(0.905), name="华宝油气")
    dec_clear = evaluate_v9_plus_live_decision(
        rstore,
        symbol="162411",
        trade_date=td_target,
        now_hhmm="14:53",
        cap_limit=50000.0,
        rule_spec=saved_xop,
        pos_sizing=saved_xop["position_sizing"],
        today_signals=[],
        rolling_shares=5000,
        allow_network=False,
    )
    assert dec_clear["diagnostics"]["close_target_weight_pct"] == 0.0
    assert dec_clear["diagnostics"]["close_target_shares"] == 100
    assert dec_clear["close_rebalance"] is not None
    assert dec_clear["close_rebalance"]["side"] == "sell"
    assert dec_clear["close_rebalance"]["qty_override"] == 4900
    assert "XOP" in dec_clear["close_rebalance"]["push_why"]

    rstore.close()
    qstore.close()


def test_v9_plus_p0_p1_p2_postmortem_optimizations(tmp_path: Path, monkeypatch) -> None:
    import json as _json
    import time as _time
    from datetime import timedelta as _timedelta
    from level2_service.v9_plus_engine import (
        V9MacroDataStore,
        _is_early_unclosed_live_bar,
        _prepare_v9_plus_live_state,
        _suppress_waterfall_short_entry,
        clean_sell_tier,
        evaluate_v9_plus_live_decision,
        evaluate_v9_plus_live_indicators,
        is_discount_uptrend_sell_guarded,
        should_exit_short,
    )

    # 1. P0: Regime-aware short cover blocks higher-price / uncompressed cover in wt<=0 regime,
    #    allows it when spread compresses >= 0.15% & price < entry_px, and keeps wt>0 behavior intact.
    trap_bar = {
        "price": 2.189,
        "sp_fx": 0.68,
        "coint10": 1.07,
        "m5": -0.55,
        "macd10": -0.0020,
        "prem": -0.55,
        "dmc10": -0.0007,
    }
    assert should_exit_short(trap_bar, entry_sp=0.44, close_wt=0.0, entry_px=2.173) is False
    assert should_exit_short(trap_bar, entry_sp=0.44, close_wt=0.80, entry_px=2.173) is True
    assert should_exit_short(
        {**trap_bar, "price": 2.165, "sp_fx": 0.25},
        entry_sp=0.44,
        close_wt=0.0,
        entry_px=2.191,
    ) is True
    assert should_exit_short(
        {**trap_bar, "sp_fx": -0.10, "coint10": -0.05},
        entry_sp=0.44,
        close_wt=0.0,
        entry_px=2.173,
    ) is True
    assert _suppress_waterfall_short_entry({"m5": -0.35, "macd10": -0.0020, "sp_fx": 0.10}, close_wt=0.0) is True
    assert _suppress_waterfall_short_entry({"m5": -0.35, "macd10": -0.0020, "sp_fx": 0.10}, close_wt=0.80) is False

    # 2. P1: Anti-noise dead-cross + strong-oil discount 60% first tranche + 2nd-tranche add-on retaining 100 shares
    assert is_discount_uptrend_sell_guarded(
        {"prem": -0.67, "sp_fx": 0.49, "sp_intra": 0.29, "brn_chg": 4.43, "brn_m5": 0.06, "dc10": True, "max_red12": 0.0001}
    )[0] is True
    assert is_discount_uptrend_sell_guarded(
        {"prem": -0.67, "sp_fx": 0.49, "sp_intra": 0.29, "brn_chg": 4.43, "brn_m5": 0.06, "dc10": True, "max_red12": 0.0016}
    )[0] is False

    t_lbl, t_frac = clean_sell_tier(
        {"ma5": 2.14},
        {"prem": -0.49, "sp_fx": 0.65, "brn_chg": 4.30, "dmc10": 0.0006, "max_red12": 0.0011},
    )
    assert t_frac == 0.60 and "60%" in t_lbl

    # P2 helper sanity
    assert _is_early_unclosed_live_bar("11:23", datetime(2026, 11, 26, 11, 22, 4, tzinfo=SHANGHAI_TZ), 15) is True
    assert _is_early_unclosed_live_bar("11:23", datetime(2026, 11, 26, 11, 22, 18, tzinfo=SHANGHAI_TZ), 15) is False
    assert _is_early_unclosed_live_bar("11:23", None, 15) is False

    # 3. End-to-end offline live decision tests for P1 (60% tranche + v9-addon) and P2 (15s anti-flicker)
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    rstore = SQLiteResearchStore(tmp_path / "v9p_opt.db")
    V9MacroDataStore(rstore, allow_network=False)
    now_ts = _time.time()
    oil_rows = [
        ["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 55.0],
        ["20261125", int(datetime(2026, 11, 26, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 92.0],
    ]
    nav_payload = {
        (datetime(2026, 11, 1, tzinfo=SHANGHAI_TZ) + _timedelta(days=k)).strftime("%Y%m%d"): 2.120
        for k in range(26)
    }
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("nav_160723", _json.dumps(nav_payload), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("oil_daily_2y", _json.dumps(oil_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcny_series", _json.dumps({"ts": [int(datetime(2026, 10, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [6.70]}), now_ts),
        )

    def _day_mins(val: float, updates: dict[str, float] | None = None) -> list[tuple[str, float]]:
        upd = updates or {}
        return [
            (
                hh := f"{((570 + i + 1) if i < 120 else (780 + (i - 120) + 1)) // 60:02d}:{((570 + i + 1) if i < 120 else (780 + (i - 120) + 1)) % 60:02d}",
                upd.get(hh, val),
            )
            for i in range(240)
        ]

    # Seed 16 warmup days ending 20261125 at 2.109; BRN=87.2
    for idx in range(16):
        td_w = (datetime(2026, 11, 10, tzinfo=SHANGHAI_TZ) + _timedelta(days=idx)).strftime("%Y%m%d")
        _seed_symbol_bars(rstore, "BRN0Y", td_w, 87.2, [("09:30", 87.2), ("15:00", 87.2)], name="布伦特", market="ICE")
        _seed_symbol_bars(rstore, "160723", td_w, 2.109, _day_mins(2.109), name="嘉实原油")

    rule_spec = {
        "id": "ai-160723-v9-plus-aggressive",
        "name": "V9+ 进取版",
        "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0002},
    }
    pos_sizing = {
        "mode": "cash_pool_rolling",
        "engine_profile": "v9_plus_aggressive",
        "default_weight_pct": 80.0,
        "max_position_pct": 100.0,
        "max_daily_entries": 8,
        "cooldown_minutes": 4,
        "scale_factor": 1.0,
        "close_flatten_time": "14:53",
        "min_retained_shares": 100,
    }

    # P2 test: 11:22 bar ramps gently (2.158 -> sp_fx ~ +0.14%), 11:23 jumps into sell zone at 2.166 (sp_fx ~ +0.52% < 0.65%, dmc10 > 0.0002 -> 60% tier)
    brn_pts_p2 = [("09:30", 90.0)] + [
        (hh, 90.0) for hh, _ in _day_mins(90.0) if hh <= "11:23"
    ]
    lof_pts_p2 = [("09:30", 2.155)] + [
        (hh, 2.155 if hh < "11:22" else (2.158 if hh == "11:22" else 2.166))
        for hh, _ in _day_mins(2.155)
        if hh <= "11:23"
    ]
    _seed_symbol_bars(rstore, "BRN0Y", "20261126", 87.2, brn_pts_p2, name="布伦特", market="ICE")
    _seed_symbol_bars(rstore, "160723", "20261126", 2.109, lof_pts_p2, name="嘉实原油")

    # At 11:22:04 (4s into ceiling-labeled 11:23 bar) with |sp_fx| < 0.65%: P2 defers signal
    dec_early = evaluate_v9_plus_live_decision(
        rstore,
        symbol="160723",
        trade_date="20261126",
        now_hhmm="11:22",
        now_dt=datetime(2026, 11, 26, 11, 22, 4, tzinfo=SHANGHAI_TZ),
        cap_limit=100000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=[],
        rolling_shares=15700,
        allow_network=False,
    )
    assert abs(dec_early["diagnostics"]["sp_fx_pct"]) < 0.65
    assert dec_early["intraday"] == []

    # At 11:22:18 (>= 15s into bar): P2 fires 60% first-tranche sell (9,400 shares)!
    dec_mature = evaluate_v9_plus_live_decision(
        rstore,
        symbol="160723",
        trade_date="20261126",
        now_hhmm="11:22",
        now_dt=datetime(2026, 11, 26, 11, 22, 18, tzinfo=SHANGHAI_TZ),
        cap_limit=100000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=[],
        rolling_shares=15700,
        allow_network=False,
    )
    assert len(dec_mature["intraday"]) == 1
    first_sell = dec_mature["intraday"][0]
    assert first_sell["side"] == "sell"
    assert first_sell["signal_tag"] == "v9-entry"
    assert first_sell["qty_override"] == 9400
    assert "60%" in first_sell["sizing_tier"]

    # Extreme |sp_fx| >= 0.65 bypasses 15s wait immediately even at 11:22:04!
    lof_pts_p2[-1] = ("11:23", 2.172)
    _seed_symbol_bars(rstore, "160723", "20261126", 2.109, lof_pts_p2, name="嘉实原油")
    dec_extreme = evaluate_v9_plus_live_decision(
        rstore,
        symbol="160723",
        trade_date="20261126",
        now_hhmm="11:22",
        now_dt=datetime(2026, 11, 26, 11, 22, 4, tzinfo=SHANGHAI_TZ),
        cap_limit=100000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=[],
        rolling_shares=15700,
        allow_network=False,
    )
    assert abs(dec_extreme["diagnostics"]["sp_fx_pct"]) >= 0.65
    assert len(dec_extreme["intraday"]) == 1

    # P1 second-tranche add-on ("v9-addon"): after 9,400 @ 2.166 at 11:23 (6,300 shares remaining),
    # price pushes higher to 2.180 at 13:45..13:46 then ticks flat/down (2.179 at 13:47 -> dmc10 <= 0.0002)
    brn_pts_addon = [("09:30", 90.0)] + [
        (hh, 90.0) for hh, _ in _day_mins(90.0) if hh <= "13:47"
    ]
    lof_pts_addon = [("09:30", 2.155)]
    for hh, _ in _day_mins(2.155):
        if hh > "13:47":
            break
        if hh < "11:23":
            px = 2.155
        elif hh < "13:42":
            px = 2.166
        elif hh < "13:45":
            px = round(2.166 + (int(hh[3:]) - 41) * 0.0045, 3)
        elif hh <= "13:46":
            px = 2.180
        else:
            px = 2.179  # deceleration -> dmc10 <= 0.0002 while sp_fx >= entry_sp + 0.15 & px >= 2.166 * 1.004!
        lof_pts_addon.append((hh, px))
    _seed_symbol_bars(rstore, "BRN0Y", "20261126", 87.2, brn_pts_addon, name="布伦特", market="ICE")
    _seed_symbol_bars(rstore, "160723", "20261126", 2.109, lof_pts_addon, name="嘉实原油")

    leg1_sig = {
        "signal_key": "160723:20261126:11:23:v9-entry:sell",
        "rule_title": "V9+ 进取版 · 反T高抛(B档·折价强油分批高抛(60%))",
        "side": "sell",
        "quantity": 9400,
        "minute": "11:23",
        "signal_price": "2.166",
        "status": "FILLED_OR_CLOSED",
        "is_simulation": 0,
    }
    ind_mid = evaluate_v9_plus_live_indicators(
        rstore,
        symbol="160723",
        trade_date="20261126",
        now_hhmm="13:47",
        cap_limit=100000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=[leg1_sig],
        rolling_shares=6300,
        allow_network=False,
    )
    assert ind_mid is not None
    assert ind_mid["sell"]["matched"] is True

    dec_addon = evaluate_v9_plus_live_decision(
        rstore,
        symbol="160723",
        trade_date="20261126",
        now_hhmm="13:47",
        now_dt=datetime(2026, 11, 26, 13, 47, 20, tzinfo=SHANGHAI_TZ),
        cap_limit=100000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=[leg1_sig],
        rolling_shares=6300,
        allow_network=False,
    )
    assert len(dec_addon["intraday"]) == 1
    addon_sig = dec_addon["intraday"][0]
    assert addon_sig["side"] == "sell"
    assert addon_sig["signal_tag"] == "v9-addon"
    assert addon_sig["qty_override"] == 6200  # Retains 100-share floor strictly!

    # Check weighted-average open_leg reconstruction after addon is persisted
    leg2_sig = {
        "signal_key": "160723:20261126:13:47:v9-addon:sell",
        "side": "sell",
        "quantity": 6200,
        "minute": "13:47",
        "signal_price": "2.179",
        "status": "FILLED_OR_CLOSED",
        "is_simulation": 0,
    }
    ctx_after = _prepare_v9_plus_live_state(
        rstore,
        symbol="160723",
        trade_date="20261126",
        cap_limit=100000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=[leg1_sig, leg2_sig],
        rolling_shares=100,
        allow_network=False,
    )
    assert ctx_after is not None
    assert ctx_after["open_leg"]["qty"] == 15600
    assert ctx_after["open_leg"]["addon_done"] is True
    assert ctx_after["open_leg"]["allow_addon"] is False
    assert 2.166 < ctx_after["entry_px"] < 2.179

    rstore.close()


def test_v9_plus_501018_tuning_and_tolerant_backfill(tmp_path: Path, monkeypatch) -> None:
    import json as _json
    import time as _time
    from level2_service.quant_rules import _normalize_v9_tuning, normalize_position_sizing
    from level2_service.v9_plus_engine import (
        V9MacroDataStore,
        _apply_v9_macd_anchor,
        _shift_buy_bar_prem,
        apply_v9_close_weight_tuning,
        evaluate_v9_plus_live_decision,
        evaluate_v9_plus_live_indicators,
        should_enter_intraday_dip_scalp,
        signals_v8_regime_macd,
        signals_v8_with_tuning,
        v9_pro_close_weight,
    )

    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")

    # 1. Normalization contract & clean default isolation
    assert _normalize_v9_tuning({}) == {}
    default_ps = normalize_position_sizing({})
    assert "v9_tuning" not in default_ps
    assert "macd_price_anchor" not in default_ps
    assert "strict_dip_scalp" not in default_ps

    full_tuning = _normalize_v9_tuning(
        {
            "bear_high_prem_flat_pct": 2.0,
            "bull_oil_surge_tier_cap_pct": 50.0,
            "bull_oil_surge_guard": True,
            "scalp_strict_mode": True,
            "macd_price_anchor": 2.20,
            "buy_prem_offset_pct": 0.60,
            "bubble_prem_cap_pct": 16.5,
            "bubble_overnight_weight_pct": 45.0,
            "bull_pullback_weight_pct": 65.0,
            "strict_dip_scalp": True,
        }
    )
    assert full_tuning == {
        "bear_high_prem_flat_pct": 2.0,
        "bull_oil_surge_tier_cap_pct": 50.0,
        "macd_price_anchor": 2.2,
        "buy_prem_offset_pct": 0.6,
        "bubble_prem_cap_pct": 16.5,
        "bubble_overnight_weight_pct": 45.0,
        "bull_pullback_weight_pct": 65.0,
        "bull_oil_surge_guard": True,
        "scalp_strict_mode": True,
        "strict_dip_scalp": True,
    }
    assert _normalize_v9_tuning({"macd_price_anchor": 0.0, "buy_prem_offset_pct": 0.0}) == {}

    # 2. MACD price-anchor causal rescaling (price=1.10 -> anchor=2.20 scales amplitudes x2)
    sample_days = [
        {
            "pre": 1.10,
            "bars": [
                {
                    "price": 1.10,
                    "diff10": 0.0010,
                    "dea10": 0.0005,
                    "macd10": -0.0012,
                    "dmc10": 0.0003,
                    "max_red12": 0.0009,
                    "min_grn12": -0.0015,
                    "gc10": True,
                    "dc10": False,
                }
            ],
        }
    ]
    _apply_v9_macd_anchor(sample_days, full_tuning)
    scaled_bar = sample_days[0]["bars"][0]
    assert abs(scaled_bar["macd10"] - (-0.0024)) < 1e-9
    assert abs(scaled_bar["dmc10"] - 0.0006) < 1e-9
    assert abs(scaled_bar["diff10"] - 0.0020) < 1e-9

    # 3. Structural buy_prem_offset_pct unlocks +0.05% bar on 501018 without altering unshifted sell or 160723
    day_ref = {"ma5": 1.120, "ma15": 1.100}
    bar_low_prem = {
        "sp_fx": -0.30,
        "sp_intra": -0.25,
        "prem": 0.05,
        "macd10": -0.0010,
        "dmc10": 0.0002,
        "price": 1.130,
        "dc10": False,
        "gc10": False,
        "max_red12": 0.0002,
        "min_grn12": -0.0010,
        "diff10": -0.0010,
        "coint10": -0.10,
        "m1": 0.02,
        "m3": -0.15,
        "m5": -0.15,
        "iopv_intra": 0.05,
    }
    tuning_501018 = {
        "macd_price_anchor": 2.20,
        "buy_prem_offset_pct": 0.60,
        "bubble_prem_cap_pct": 16.5,
        "bubble_overnight_weight_pct": 45.0,
        "bull_pullback_weight_pct": 65.0,
        "strict_dip_scalp": True,
    }
    assert _shift_buy_bar_prem(bar_low_prem, {}) is bar_low_prem
    assert signals_v8_regime_macd(day_ref, bar_low_prem, 0.95) == (False, False)
    assert signals_v8_with_tuning(day_ref, bar_low_prem, 0.95, tuning_501018) == (False, True)

    # 4. Close weight tuning: 16.5% bubble (45% intact / 0% deep negative spread) & 65% bull pullback & broken trend 0%
    assert apply_v9_close_weight_tuning(v9_pro_close_weight, {}) is v9_pro_close_weight
    tuned_cw = apply_v9_close_weight_tuning(v9_pro_close_weight, tuning_501018)
    w_bub_intact, lbl_bub_intact = tuned_cw(
        {"ma5": 1.20, "ma15": 1.10, "max_bpos_20d": 0.45},
        {"brn_pos": 0.40, "prem": 18.0, "price": 1.25, "sp_fx": 0.50},
        0,
    )
    assert abs(w_bub_intact - 0.45) < 1e-6 and "泡沫" in lbl_bub_intact
    w_bub_neg_sp, _ = tuned_cw(
        {"ma5": 1.20, "ma15": 1.10, "max_bpos_20d": 0.45},
        {"brn_pos": 0.40, "prem": 18.0, "price": 1.25, "sp_fx": -1.80},
        0,
    )
    assert w_bub_neg_sp == 0.0
    w_pullback, _ = tuned_cw(
        {"ma5": 1.20, "ma15": 1.10, "max_bpos_20d": 0.50},
        {"brn_pos": 0.50, "prem": 6.0, "price": 1.16},
        0,
    )
    assert abs(w_pullback - 0.65) < 1e-6
    w_broken, _ = tuned_cw(
        {"ma5": 1.15, "ma15": 1.20, "max_bpos_20d": 0.50},
        {"brn_pos": 0.50, "prem": 4.0, "price": 1.12},
        0,
    )
    assert w_broken == 0.0

    # 5. strict_dip_scalp blocks 0% close weight & high BRN percentile, permits aligned lag dip
    scalp_bar = {
        "time": "10:45",
        "vdev": -0.48,
        "px_dd_hi": 0.52,
        "sp_dd_hi": 0.30,
        "prem": 0.35,
        "sp_fx": -0.22,
        "sp_intra": -0.25,
        "brn_pos": 0.50,
        "price": 1.13,
        "gc10": True,
        "macd10": -0.001,
        "dmc10": 0.0004,
        "min_grn12": -0.0012,
        "brn_m5": 0.01,
        "m1": 0.0,
    }
    assert should_enter_intraday_dip_scalp(scalp_bar, 0.95, day=day_ref, v9_tuning=tuning_501018, cur_close_wt=0.0)[0] is False
    assert should_enter_intraday_dip_scalp({**scalp_bar, "brn_pos": 0.88}, 0.95, day=day_ref, v9_tuning=tuning_501018, cur_close_wt=0.80)[0] is False
    assert should_enter_intraday_dip_scalp(scalp_bar, 0.95, day=day_ref, v9_tuning=tuning_501018, cur_close_wt=0.80)[0] is True
    # Un-tuned default ignores cur_close_wt=0.0 & brn_pos=0.88
    assert should_enter_intraday_dip_scalp(scalp_bar, 1.0, day=day_ref, v9_tuning={}, cur_close_wt=0.0)[0] is True

    # 6. End-to-end 501018 offline backtest + live decision + indicators + tolerant 10jqka backfill
    rstore = SQLiteResearchStore(tmp_path / "r501018.db")
    qstore = SQLiteQuantRuleStore(tmp_path / "r501018.db")
    V9MacroDataStore(rstore, allow_network=False)
    now_ts = _time.time()
    oil_rows = [
        ["20250601", int(datetime(2025, 6, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 60.0],
        ["20260630", int(datetime(2026, 7, 1, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 90.0],
    ]
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("nav_501018", _json.dumps({"20260628": 1.100, "20260629": 1.100, "20260630": 1.100}), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("oil_daily_2y", _json.dumps(oil_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcny_series", _json.dumps({"ts": [int(datetime(2026, 6, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [6.70]}), now_ts),
        )

    def _mins(base_p: float, updates: dict[str, float]) -> list[tuple[str, float]]:
        out = []
        for idx in range(240):
            m = (570 + idx + 1) if idx < 120 else (780 + (idx - 120) + 1)
            hhmm = f"{m // 60:02d}:{m % 60:02d}"
            out.append((hhmm, updates.get(hhmm, base_p)))
        return out

    _seed_symbol_bars(rstore, "BRN0Y", "20260630", 70.0, [("09:30", 70.0), ("15:00", 70.0)], name="布伦特", market="ICE")
    _seed_symbol_bars(rstore, "501018", "20260630", 1.100, _mins(1.100, {}), name="南方原油", market="33")
    # Day 1 (20260701): Brent rises +2% while 501018 carries structural +0.4% prem and dips intraday -> buys & exits +T
    brn_d1 = [("09:30", 70.0)] + [(t, 71.5 if "09:40" <= t <= "10:30" else 70.6) for t, _ in _mins(70.6, {})]
    _seed_symbol_bars(rstore, "BRN0Y", "20260701", 70.0, brn_d1, name="布伦特", market="ICE")
    lof_d1 = _mins(1.105, {"09:58": 1.101, "09:59": 1.099, "10:00": 1.097, "10:15": 1.124})
    _seed_symbol_bars(rstore, "501018", "20260701", 1.100, lof_d1, name="南方原油", market="33")

    spec_501018 = {
        "id": "ai-501018-v9-plus-aggressive",
        "name": "V9+ 进取版(501018) · 南方原油汇调溢价三态量化",
        "source": "ai",
        "symbols": ["501018", "BRN0Y"],
        "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0002},
        "buy_rule": {"title": "正T买点", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "lte", "value": -0.15}]},
        "sell_rule": {"title": "反T卖点", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "gte", "value": 0.42}]},
        "position_sizing": {
            "mode": "cash_pool_rolling",
            "engine_profile": "v9_plus_aggressive",
            "default_weight_pct": 85.0,
            "max_position_pct": 100.0,
            "max_daily_entries": 8,
            "cooldown_minutes": 4,
            "scale_factor": 0.95,
            "close_flatten_time": "14:53",
            "min_retained_shares": 100,
            "v9_tuning": tuning_501018,
        },
        "eval_criteria": {"start_date": "20260701", "round_trip_fee_pct": 0.01, "capital_cap": 20000.0},
    }
    res_501018 = evaluate_quant_strategy(rstore, spec_501018, symbol="501018", start_date="20260701", capital_cap=20000.0)
    assert res_501018["coverage"]["evaluated_days"] == 1
    assert res_501018["capital_sim"]["buy_orders"] >= 1

    ind = evaluate_v9_plus_live_indicators(
        rstore,
        symbol="501018",
        trade_date="20260701",
        now_hhmm="10:00",
        cap_limit=20000.0,
        rule_spec=spec_501018,
        pos_sizing=spec_501018["position_sizing"],
        today_signals=[],
        rolling_shares=5000,
        allow_network=False,
    )
    assert ind is not None
    buy_conds = {c["id"]: c for c in ind["buy"]["conditions"]}
    assert "防飞刀严选" in buy_conds["v9-buy-scalp"]["label"]
    assert "+1.10%" in buy_conds["v9-buy-b"]["target_rule"]

    dec = evaluate_v9_plus_live_decision(
        rstore,
        symbol="501018",
        trade_date="20260701",
        now_hhmm="14:53",
        now_dt=datetime(2026, 7, 1, 14, 53, 20, tzinfo=SHANGHAI_TZ),
        cap_limit=20000.0,
        rule_spec=spec_501018,
        pos_sizing=spec_501018["position_sizing"],
        today_signals=[],
        rolling_shares=5000,
        allow_network=False,
    )
    assert dec is not None

    # Tolerant 10jqka fallback in backfill_intraday_history when kline fetch fails or returns junk
    _seed_symbol_bars(rstore, "160723", "20260702", 2.000, [("09:31", 2.001)], name="嘉实原油")

    def _broken_kline(_sym: str, _mkt: str, _to: float) -> str:
        return "not-a-jsonp-payload"

    def _fake_tdx(_sym: str, _dates: object) -> dict[str, dict]:
        return {
            "20260702": {
                "pre_close": 1.124,
                "points": [{"minute": "09:31", "price": 1.125, "average_price": 1.125, "volume": 1000}],
            }
        }

    bf = backfill_intraday_history(
        rstore,
        symbol="501018",
        start_date="20260702",
        end_date="20260702",
        name="南方原油",
        market="33",
        fetch_kline_fn=_broken_kline,
        fetch_tdx_fn=_fake_tdx,
        fetch_line_funds_fn=lambda _s, _m, _t: "",
    )
    assert bf["tdx_days_backfilled"] == 1
    assert bf["tdx_points_written"] == 1

    qstore.close()
    rstore.close()


def test_v9_plus_xop_v2_multifactor_crack_iwm_and_crash_contrarian(tmp_path: Path, monkeypatch) -> None:
    import json as _json
    import time as _time
    from datetime import timedelta as _timedelta
    from level2_service.v9_plus_engine import (
        V9MacroDataStore,
        _check_xop_bear_quick_tp,
        apply_xop_overnight_intraday_overlays,
        evaluate_v9_plus_live_indicators,
        should_exit_scalp_long,
        v9_xop_close_weight,
    )

    # 1. Contrarian stabilization rebound, MA5 reclaim continuation, exhaustion TP, & E&P high-Brent breakout
    w_rebound, lbl_rebound = v9_xop_close_weight(
        {
            "ma5": 0.900,
            "ma15": 0.950,
            "max_bpos_20d": 0.65,
            "max_xpos_20d": 0.70,
            "xop_bull": False,
            "xop_above_ma5": True,
            "xop_5d": -7.5,
            "xop_ov": 0.50,
            "iwm_ov": 0.80,
            "crack_3d": 0.20,
        },
        {"brn_pos": 0.48, "xop_pos": 0.52, "prem": -0.50, "price": 0.890, "daytime_lead_pct": 0.15},
        0,
    )
    assert w_rebound == 0.90 and "逆势反弹" in lbl_rebound

    w_cont, _ = v9_xop_close_weight(
        {
            "ma5": 0.880,
            "ma15": 0.950,
            "max_bpos_20d": 0.85,
            "max_xpos_20d": 0.85,
            "xop_bull": False,
            "xop_above_ma5": True,
            "xop_5d": 1.5,
            "xop_ov": 1.50,
            "iwm_ov": 0.20,
            "crack_3d": 0.0,
        },
        {"brn_pos": 0.50, "xop_pos": 0.55, "prem": -0.05, "price": 0.902, "daytime_lead_pct": 0.10},
        0,
    )
    assert w_cont == 0.85

    w_exhaust, _ = v9_xop_close_weight(
        {
            "ma5": 0.920,
            "ma15": 0.960,
            "max_bpos_20d": 0.90,
            "max_xpos_20d": 0.90,
            "xop_bull": False,
            "xop_above_ma5": True,
            "xop_5d": 5.5,
            "xop_ov": 0.80,
            "iwm_ov": 0.10,
            "crack_3d": 0.0,
        },
        {"brn_pos": 0.65, "xop_pos": 0.70, "prem": 0.05, "price": 0.931, "daytime_lead_pct": 0.10},
        0,
    )
    assert w_exhaust == 0.0

    day_ep_breakout = {
        "ma5": 0.990,
        "ma15": 0.960,
        "max_bpos_20d": 0.85,
        "max_xpos_20d": 0.85,
        "xop_bull": True,
        "xop_above_ma5": True,
        "crack_3d": 1.0,
    }
    w_breakout, _ = v9_xop_close_weight(
        day_ep_breakout,
        {"brn_pos": 0.85, "xop_pos": 0.80, "prem": -0.20, "price": 1.000, "daytime_lead_pct": 0.15},
        0,
    )
    assert w_breakout == 0.90
    w_break_cut, _ = v9_xop_close_weight(
        day_ep_breakout,
        {"brn_pos": 0.85, "xop_pos": 0.80, "prem": -0.20, "price": 0.985, "daytime_lead_pct": 0.15},
        0,
    )
    assert w_break_cut == 0.0

    # 2. Panic capitulation bounce scalp, late bear entry block, and XOP-isolated quick TP
    bear_day = {
        "ma5": 0.900,
        "ma15": 0.950,
        "xop_ov": -0.30,
        "spy_ov": 0.10,
        "ng_ov": 0.0,
        "iwm_ov": 0.10,
        "crack_3d": 0.20,
        "xop_5d": -6.0,
        "xop_vs_oil_alpha": 0.0,
        "cross_market_anchor": "xop_us_ep",
    }
    _, _, sc_panic, m_panic = apply_xop_overnight_intraday_overlays(
        bear_day,
        {
            "time": "11:15",
            "prem": -0.60,
            "sp_fx": -0.35,
            "dmc10": 0.00025,
            "m1": 0.05,
            "dc10": False,
            "gc10": False,
            "price": 0.885,
            "vdev": -0.50,
            "px_dd_hi": 0.60,
            "vol_ratio": 1.40,
            "bounce_from_lo_pct": 0.22,
            "daytime_lead_pct": 0.0,
        },
        s_sig=False,
        b_sig=False,
        scalp_ok=False,
        close_wt=0.0,
        sc=1.0,
    )
    assert sc_panic is True
    assert m_panic["panic_scalp_xop"] is True
    assert m_panic["scalp_frac_override"] == 0.85

    _, b_late, sc_late, m_late = apply_xop_overnight_intraday_overlays(
        bear_day,
        {
            "time": "13:35",
            "prem": -0.35,
            "sp_fx": -0.30,
            "dmc10": 0.00010,
            "m1": 0.01,
            "dc10": False,
            "gc10": False,
            "price": 0.889,
            "vdev": -0.30,
            "px_dd_hi": 0.35,
            "vol_ratio": 0.90,
            "bounce_from_lo_pct": 0.05,
            "daytime_lead_pct": 0.0,
        },
        s_sig=False,
        b_sig=True,
        scalp_ok=True,
        close_wt=0.0,
        sc=1.0,
    )
    assert b_late is False and sc_late is False and m_late["bear_late_or_weak_blocked"] is True

    # Quick TP fires on xop_us_ep bear-zero (+0.36% stall even when sp_gain=0.20 < 0.30), never mutates brent rules
    qtp_xop, lbl_xop_tp = should_exit_scalp_long(
        {"price": 1.0036, "sp_fx": -0.10, "dmc10": 0.0, "time": "11:30"},
        entry_px=1.000,
        entry_sp=-0.30,
        s_sig=False,
        day=bear_day,
        close_wt=0.0,
    )
    assert qtp_xop is True and "暴跌反抽快进止盈" in lbl_xop_tp
    assert _check_xop_bear_quick_tp({**bear_day, "cross_market_anchor": "brent"}, {"dmc10": 0.0, "time": "11:30"}, 0.36, 0.0)[0] is False
    qtp_brent, _ = should_exit_scalp_long(
        {"price": 1.0036, "sp_fx": -0.10, "dmc10": 0.0, "time": "11:30"},
        entry_px=1.000,
        entry_sp=-0.30,
        s_sig=False,
        day={**bear_day, "cross_market_anchor": "brent"},
        close_wt=0.0,
    )
    assert qtp_brent is False

    # 3. Offline SQLite store: iwm_daily_2y, xle_daily_2y, cl_daily_2y, ho_daily_2y surface in live indicators
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    rstore = SQLiteResearchStore(tmp_path / "v9p_xop_v2.db")
    V9MacroDataStore(rstore, allow_network=False)
    now_ts = _time.time()

    def _day_mins(val: float) -> list[tuple[str, float]]:
        return [
            (f"{((570 + i + 1) if i < 120 else (780 + (i - 120) + 1)) // 60:02d}:{((570 + i + 1) if i < 120 else (780 + (i - 120) + 1)) % 60:02d}", val)
            for i in range(240)
        ]

    oil_rows = [
        ["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 60.0],
        ["20261101", int(datetime(2026, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 100.0],
    ]
    xop_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 120.0]]
    spy_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 600.0]]
    ng_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 3.00]]
    iwm_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 200.0]]
    xle_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 50.0]]
    cl_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 70.0]]
    ho_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 3.00]]
    nav_payload = {}

    for idx in range(7):
        us_d = (datetime(2026, 11, 10, tzinfo=SHANGHAI_TZ) + _timedelta(days=idx)).strftime("%Y%m%d")
        a_d = (datetime(2026, 11, 11, tzinfo=SHANGHAI_TZ) + _timedelta(days=idx)).strftime("%Y%m%d")
        cts = int((datetime.strptime(us_d, "%Y%m%d").replace(tzinfo=SHANGHAI_TZ) + _timedelta(days=1, hours=6)).timestamp())
        xop_rows.append([us_d, cts, 160.0 - idx * 2.0])
        spy_rows.append([us_d, cts, 700.0])
        ng_rows.append([us_d, cts, 3.50])
        iwm_rows.append([us_d, cts, 260.0 + (4.0 if idx == 6 else 0.0)])
        xle_rows.append([us_d, cts, 60.0])
        cl_rows.append([us_d, cts, 80.0])
        ho_rows.append([us_d, cts, 3.40 + (0.17 if idx == 6 else 0.0)])  # HO surges +5.0% in 3d -> crack_3d = +5.0%
        nav_payload[us_d] = 0.880
        _seed_symbol_bars(rstore, "BRN0Y", a_d, 80.0, [("09:30", 80.0), ("14:53", 80.0), ("15:00", 80.0)], name="布伦特", market="ICE")
        _seed_symbol_bars(rstore, "162411", a_d, 0.880, _day_mins(0.880), name="华宝油气")

    td_check = "20261117"
    with rstore._lock, rstore._connection:
        for k_name, payload in (
            ("nav_162411", nav_payload),
            ("oil_daily_2y", oil_rows),
            ("xop_daily_2y", xop_rows),
            ("spy_daily_2y", spy_rows),
            ("ng_daily_2y", ng_rows),
            ("iwm_daily_2y", iwm_rows),
            ("xle_daily_2y", xle_rows),
            ("cl_daily_2y", cl_rows),
            ("ho_daily_2y", ho_rows),
            ("fx_usdcny_series", {"ts": [int(datetime(2026, 10, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [6.70]}),
        ):
            rstore._connection.execute(
                "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
                (k_name, _json.dumps(payload), now_ts),
            )

    ind_v2 = evaluate_v9_plus_live_indicators(
        rstore,
        symbol="162411",
        trade_date=td_check,
        now_hhmm="14:53",
        cap_limit=50000.0,
        rule_spec={"id": "ai-162411-v9-xop-aggressive", "name": "162411 XOP v2", "macd_settings": {"short": 10, "long": 20, "signal": 5}},
        pos_sizing={"mode": "cash_pool_rolling", "engine_profile": "v9_plus_aggressive", "cross_market_anchor": "xop_us_ep", "min_retained_shares": 100},
        today_signals=[],
        rolling_shares=0,
        allow_network=False,
    )
    assert ind_v2 is not None
    diag_v2 = ind_v2["diagnostics"]
    assert diag_v2["crack_3d_pct"] == 5.0
    assert round(diag_v2["iwm_overnight_pct"], 2) == round((264.0 / 260.0 - 1.0) * 100.0, 2)
    assert diag_v2["xop_5d_pct"] < -5.0
    assert diag_v2["xle_overnight_pct"] == 0.0
    assert diag_v2["vol_ratio"] > 0.0

    rstore.close()


def test_v9_plus_501018_multi_oil_and_plunge_contrarian(tmp_path: Path, monkeypatch) -> None:
    import json as _json
    import time as _time
    from datetime import timedelta as _timedelta
    from level2_service.quant_rules import _normalize_v9_tuning
    from level2_service.v9_plus_engine import (
        V9MacroDataStore,
        apply_v9_close_weight_tuning,
        apply_v9_sell_tier_tuning,
        clean_sell_tier,
        evaluate_v9_plus_live_decision,
        evaluate_v9_plus_live_indicators,
        filter_bear_regime_buy,
        signals_v8_with_tuning,
        v9_pro_close_weight,
    )

    # 1. Tuning normalization & bounds check
    norm = _normalize_v9_tuning(
        {
            "macd_price_anchor": 2.2,
            "buy_prem_offset_pct": 0.6,
            "bubble_prem_cap_pct": 16.5,
            "bubble_overnight_weight_pct": 45.0,
            "bull_pullback_weight_pct": 65.0,
            "strict_dip_scalp": True,
            "multi_oil_plunge_mode": True,
            "plunge_dd15_pct": -11.0,
            "high_oil_bull_cap_pct": 87.0,
            "peer_symbol": "160723",
        }
    )
    assert norm["multi_oil_plunge_mode"] is True
    assert norm["plunge_dd15_pct"] == -11.0
    assert norm["high_oil_bull_cap_pct"] == 87.0
    assert norm["peer_symbol"] == "160723"

    clamped = _normalize_v9_tuning({"plunge_dd15_pct": -60.0, "high_oil_bull_cap_pct": 99.0, "peer_symbol": "../bad"})
    assert clamped["plunge_dd15_pct"] == -35.0
    assert clamped["high_oil_bull_cap_pct"] == 95.0
    assert "peer_symbol" not in clamped

    # 2. Contrarian plunge overnight & dual-anchor bubble guard
    close_fn = apply_v9_close_weight_tuning(v9_pro_close_weight, norm)
    day_plunge = {"ma5": 1.85, "ma15": 2.00, "max_bpos_20d": 0.90, "macro_oil_ov": 1.25, "xop_ov": 0.50}
    b_debubbled = {
        "brn_pos": 0.55,
        "prem": -1.40,
        "prem_etf": -1.30,
        "price": 1.82,
        "sp_fx": -0.20,
        "dd15_pct": -13.0,
        "lof_chg": -0.80,
        "brn_chg": 0.50,
        "vwap_intra": 1.81,
    }
    w_cap, lbl_cap = close_fn(day_plunge, b_debubbled, 0)
    assert w_cap == 0.85 and "逆势" in lbl_cap

    # Bubble in USO/BNO ETF anchor blocks contrarian overnight even when Brent-only IOPV looks discounted
    w_bub, _ = close_fn(day_plunge, {**b_debubbled, "prem_etf": 5.50}, 0)
    assert w_bub == 0.0

    w_turn, lbl_turn = close_fn(
        day_plunge,
        {**b_debubbled, "prem": 0.20, "prem_etf": 0.30, "lof_chg": 0.60, "price": 1.82, "vwap_intra": 1.81},
        0,
    )
    assert w_turn == 0.90 and "逆势" in lbl_turn

    # 3. Late bear knife block (>=13:20 when close_wt==0) & morning bear profit lock (>=+2% gap-up)
    b_late_bear = {
        "time": "13:45",
        "price": 1.82,
        "vdev": -0.25,
        "lof_chg": -0.50,
        "gc10": True,
        "dc10": False,
        "dmc10": 0.0002,
        "m1": 0.02,
        "sp_fx": -0.50,
        "coint10": -0.30,
        "prem": -0.80,
        "m3": -0.15,
        "m5": -0.10,
        "macd10": -0.001,
        "diff10": -0.002,
        "dea10": -0.001,
        "max_red12": 0.0005,
        "min_grn12": -0.0015,
        "brn_chg": -0.50,
        "sp_intra": -0.20,
    }
    assert filter_bear_regime_buy(day_plunge, b_late_bear, True, 0.0, 0.95, v9_tuning=norm) is False
    assert filter_bear_regime_buy(day_plunge, b_late_bear, True, 0.0, 0.95, v9_tuning=None) is True

    b_morning_gap = {
        **b_late_bear,
        "time": "09:45",
        "price": 1.89,
        "lof_chg": 2.35,
        "sp_fx": 0.25,
        "coint10": 0.10,
        "prem": -0.40,
        "dmc10": -0.00005,
    }
    s_carry, _ = signals_v8_with_tuning(day_plunge, b_morning_gap, 0.95, norm, bear_carry_active=True)
    assert s_carry is True
    lbl_lock, frac_lock = apply_v9_sell_tier_tuning(
        day_plunge, b_morning_gap, *clean_sell_tier(day_plunge, b_morning_gap), norm, bear_carry_active=True
    )
    assert frac_lock == 1.0 and "锁利" in lbl_lock

    # 4. Offline end-to-end store integration with USO+BNO+WTI+XOP+160723 peer LOF
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    rstore = SQLiteResearchStore(tmp_path / "v9p_501018_multi.db")
    V9MacroDataStore(rstore, allow_network=False)
    now_ts = _time.time()

    def _day_mins_ramp(p0: float, p_end: float) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        for i in range(240):
            hh = ((570 + i + 1) if i < 120 else (780 + (i - 120) + 1)) // 60
            mm = ((570 + i + 1) if i < 120 else (780 + (i - 120) + 1)) % 60
            px = p0 + (p_end - p0) * (i / 239.0)
            out.append((f"{hh:02d}:{mm:02d}", round(px, 4)))
        return out

    oil_rows = [
        ["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 60.0],
        ["20261025", int(datetime(2026, 10, 26, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 100.0],
    ]
    uso_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 70.0]]
    bno_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 30.0]]
    cl_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 65.0]]
    xop_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 140.0]]
    nav_payload = {}

    closes_501 = [2.20 - i * 0.02 for i in range(18)]
    last_td = ""
    for idx, c_val in enumerate(closes_501):
        us_d = (datetime(2026, 11, 1, tzinfo=SHANGHAI_TZ) + _timedelta(days=idx)).strftime("%Y%m%d")
        a_d = (datetime(2026, 11, 2, tzinfo=SHANGHAI_TZ) + _timedelta(days=idx)).strftime("%Y%m%d")
        last_td = a_d
        cts = int((datetime.strptime(us_d, "%Y%m%d").replace(tzinfo=SHANGHAI_TZ) + _timedelta(days=1, hours=6)).timestamp())
        uso_px = 92.0 - idx * 1.1 + (2.0 if idx == len(closes_501) - 1 else 0.0)
        bno_px = 40.0 - idx * 0.5 + (0.9 if idx == len(closes_501) - 1 else 0.0)
        cl_px = 88.0 - idx * 1.0 + (1.5 if idx == len(closes_501) - 1 else 0.0)
        uso_rows.append([us_d, cts, round(uso_px, 3)])
        bno_rows.append([us_d, cts, round(bno_px, 3)])
        cl_rows.append([us_d, cts, round(cl_px, 3)])
        xop_rows.append([us_d, cts, round(155.0 - idx * 1.0, 3)])
        oil_rows.append([us_d, cts, 90.0 - idx * 0.8])
        nav_payload[us_d] = round(c_val + 0.03, 4)

        pre_501 = closes_501[idx - 1] if idx > 0 else 2.22
        is_last = idx == len(closes_501) - 1
        end_501 = pre_501 * (0.985 if is_last else c_val / pre_501)
        end_160 = 2.00 * (0.998 if is_last else 0.995)
        _seed_symbol_bars(rstore, "BRN0Y", a_d, 78.0, [("09:30", 78.0), ("14:53", 78.0), ("15:00", 78.0)], name="布伦特", market="ICE")
        _seed_symbol_bars(rstore, "501018", a_d, pre_501, _day_mins_ramp(pre_501, end_501), name="南方原油")
        _seed_symbol_bars(rstore, "160723", a_d, 2.00, _day_mins_ramp(2.00, end_160), name="嘉实原油")

    with rstore._lock, rstore._connection:
        for k_name, payload in (
            ("nav_501018", nav_payload),
            ("nav_160723", nav_payload),
            ("oil_daily_2y", oil_rows),
            ("uso_daily_2y", uso_rows),
            ("bno_daily_2y", bno_rows),
            ("cl_daily_2y", cl_rows),
            ("xop_daily_2y", xop_rows),
            ("fx_usdcny_series", {"ts": [int(datetime(2026, 10, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [7.10]}),
        ):
            rstore._connection.execute(
                "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
                (k_name, _json.dumps(payload), now_ts),
            )

    pos_501 = {
        "mode": "cash_pool_rolling",
        "engine_profile": "v9_plus_aggressive",
        "capital_cap": 20000.0,
        "scale_factor": 0.95,
        "min_retained_shares": 100,
        "v9_tuning": norm,
    }
    rule_501 = {"id": "ai-501018-v9-plus-aggressive", "name": "501018 Multi-Oil", "macd_settings": {"short": 10, "long": 20, "signal": 5}}

    dec = evaluate_v9_plus_live_decision(
        rstore,
        symbol="501018",
        trade_date=last_td,
        now_hhmm="14:53",
        cap_limit=20000.0,
        rule_spec=rule_501,
        pos_sizing=pos_501,
        today_signals=[],
        rolling_shares=100,
        allow_network=False,
    )
    ind = evaluate_v9_plus_live_indicators(
        rstore,
        symbol="501018",
        trade_date=last_td,
        now_hhmm="14:53",
        cap_limit=20000.0,
        rule_spec=rule_501,
        pos_sizing=pos_501,
        today_signals=[],
        rolling_shares=100,
        allow_network=False,
    )
    assert dec is not None and ind is not None
    diag = ind["diagnostics"]
    assert diag["etf_ov_pct"] > 0.0
    assert diag["wti_ov_pct"] > 0.0
    assert diag["dd15_pct"] <= -11.0
    assert diag["peer_gap_pct"] <= -1.0
    assert dec["diagnostics"]["peer_gap_pct"] == diag["peer_gap_pct"]
    assert any(c["id"] == "v9-macro-plunge" for c in ind["buy"]["conditions"])

    # Isolation check: standard 160723 must never receive v9-macro-plunge or multi-oil fields
    ind_160 = evaluate_v9_plus_live_indicators(
        rstore,
        symbol="160723",
        trade_date=last_td,
        now_hhmm="14:53",
        cap_limit=50000.0,
        rule_spec={"id": "ai-160723-v9-plus-aggressive", "name": "160723", "macd_settings": {"short": 10, "long": 20, "signal": 5}},
        pos_sizing={"mode": "cash_pool_rolling", "engine_profile": "v9_plus_aggressive", "min_retained_shares": 100},
        today_signals=[],
        rolling_shares=100,
        allow_network=False,
    )
    assert ind_160 is not None
    assert not any(c["id"] == "v9-macro-plunge" for c in ind_160["buy"]["conditions"])
    assert "peer_gap_pct" not in ind_160["diagnostics"]

    rstore.close()


def test_v9_plus_uso_bno_sc_peer_161129_engine(tmp_path: Path, monkeypatch) -> None:
    import json as _json
    import time as _time
    from datetime import timedelta as _timedelta
    from level2_service.quant_rules import normalize_position_sizing
    from level2_service.v9_plus_engine import (
        V9MacroDataStore,
        _parse_sc_daily_payload,
        apply_dual_oil_peer_intraday_overlays,
        clean_buy_tier,
        evaluate_v9_plus_backtest,
        evaluate_v9_plus_live_decision,
        evaluate_v9_plus_live_indicators,
        should_exit_long_v9,
        should_exit_scalp_long,
        should_hold_dual_oil_rebound,
        v9_dual_oil_peer_close_weight,
    )

    # 1. Anchor normalization & SC0 / A-share parser verification
    assert "cross_market_anchor" not in normalize_position_sizing({})
    ps_dual = normalize_position_sizing(
        {
            "mode": "cash_pool_rolling",
            "engine_profile": "v9_plus_aggressive",
            "cross_market_anchor": "uso_bno_sc_peer",
            "min_retained_shares": 100,
        }
    )
    assert ps_dual["cross_market_anchor"] == "uso_bno_sc_peer"

    sc_rows = _parse_sc_daily_payload(
        'var x=([{"d":"2026-11-24","o":"580.0","c":"571.1"},{"d":"2026-11-25","o":"572.9","c":"573.8"}]);'
    )
    assert len(sc_rows) == 2 and sc_rows[1][0] == "20261125" and sc_rows[1][2] == 573.8 and sc_rows[1][3] == 572.9
    q_peer = V9MacroDataStore._extract_sina_a_quote(
        (["x"] * 2) + ["2.109", "2.174"] + (["0"] * 26) + ["2026-11-26", "14:53:00", "00"]
    )
    assert q_peer["pre_close"] == 2.109 and q_peer["price"] == 2.174 and q_peer["date"] == "20261126" and q_peer["time"] == "14:53"

    # 2. Close weight: high-oil dual-bull retention, bubble exit, early reversal, quiet accumulation overnight
    day_bull = {"ma5": 2.10, "ma15": 2.02, "max_bpos_20d": 0.80, "dual_ov": 1.2}
    w_high, _ = v9_dual_oil_peer_close_weight(
        day_bull,
        {
            "brn_pos": 0.78,
            "prem": 0.30,
            "dual_prem": 0.10,
            "rel_prem": 0.0,
            "price": 2.15,
            "uso_bull": True,
            "bno_bull": True,
            "dual_bull": True,
            "sc_bull": True,
            "sc_intra": 0.40,
            "sc_vs_prev": 0.90,
            "lof_chg": 0.60,
            "peer_chg": 0.70,
        },
    )
    assert w_high == 0.95

    w_bub, _ = v9_dual_oil_peer_close_weight(
        day_bull,
        {"brn_pos": 0.65, "prem": 6.50, "dual_prem": 1.50, "rel_prem": 2.0, "price": 2.05},
    )
    assert w_bub == 0.0

    w_rev, _ = v9_dual_oil_peer_close_weight(
        {"ma5": 2.08, "ma15": 2.15, "max_bpos_20d": 0.78, "dual_ov": 1.90},
        {
            "brn_pos": 0.58,
            "prem": 1.10,
            "dual_prem": 0.40,
            "rel_prem": -0.20,
            "price": 2.11,
            "uso_bull": True,
            "dual_bull": True,
            "sc_bull": True,
            "sc_intra": 0.60,
            "sc_vs_prev": 2.10,
            "lof_chg": 1.90,
            "peer_chg": 1.50,
        },
    )
    assert w_rev == 0.95

    w_quiet, lbl_quiet = v9_dual_oil_peer_close_weight(
        {"ma5": 2.18, "ma15": 2.25, "max_bpos_20d": 0.65, "dual_ov": 0.50},
        {
            "brn_pos": 0.52,
            "prem": 1.25,
            "dual_prem": 0.35,
            "rel_prem": -0.10,
            "price": 2.12,
            "lof_chg": 0.60,
            "peer_chg": 0.85,
            "sc_intra": 0.12,
            "sc_vs_prev": 0.20,
        },
    )
    assert w_quiet == 0.90 and "逆势" in lbl_quiet

    # 3. Bear knife-filter + post-10:08 3-channel contrarian + crash 100% tier + trailing hold
    bear_day = {"ma5": 2.18, "ma15": 2.26, "open_gap_pct": 0.10, "eq_prem_5d": 0.80, "cross_market_anchor": "uso_bno_sc_peer"}
    base_bar = {
        "time": "10:02",
        "price": 2.13,
        "prem": -0.50,
        "dual_prem": -0.95,
        "rel_prem": -0.75,
        "sp_fx": -0.65,
        "coint10": -0.35,
        "lof_chg": -0.40,
        "vdev": -0.30,
        "px_dd_hi": 0.55,
        "bounce_from_lo_pct": 0.18,
        "dmc10": 0.00025,
        "m1": 0.05,
        "m3": 0.10,
        "gc10": True,
        "dc10": False,
        "iopv_intra": 0.05,
        "brn_m5": 0.02,
        "sc_intra": 0.15,
        "peer_chg": 0.25,
        "peer_m3": 0.15,
        "peer_sp": -0.65,
        "peer_sp_intra": -0.35,
    }
    _, b_early, sc_early, meta_early = apply_dual_oil_peer_intraday_overlays(
        bear_day, base_bar, s_sig=False, b_sig=True, scalp_ok=True, close_wt=0.0, sc=1.0
    )
    assert b_early is False and sc_early is False and meta_early["knife_blocked"] is True

    _, b_late, sc_late, _ = apply_dual_oil_peer_intraday_overlays(
        bear_day, {**base_bar, "time": "13:40"}, s_sig=False, b_sig=True, scalp_ok=True, close_wt=0.0, sc=1.0
    )
    assert b_late is False and sc_late is False

    _, _, sc_ctr, meta_ctr = apply_dual_oil_peer_intraday_overlays(
        bear_day, {**base_bar, "time": "10:25"}, s_sig=False, b_sig=False, scalp_ok=False, close_wt=0.0, sc=1.0
    )
    assert sc_ctr is True and meta_ctr["contrarian_triggered"] is True
    assert set(meta_ctr["contrarian_channels"]) >= {"c_disc", "c_peer"}
    assert meta_ctr["scalp_frac_override"] == 1.00

    _, _, sc_aug, meta_aug = apply_dual_oil_peer_intraday_overlays(
        bear_day,
        {**base_bar, "time": "11:10", "prem": 1.80, "dual_prem": 0.70, "rel_prem": -0.70, "vdev": -0.65, "px_dd_hi": 0.85, "peer_m3": 0.05, "peer_sp_intra": -0.10},
        s_sig=False,
        b_sig=False,
        scalp_ok=False,
        close_wt=0.0,
        sc=1.0,
    )
    assert sc_aug is True and "c_aug" in meta_aug["contrarian_channels"]

    # Trailing rebound hold overrides mild positive TP while respecting stop-loss and anchor isolation
    hold_bar = {**base_bar, "price": 2.142, "px_dd_hi": 0.10, "sp_fx": 0.35, "sc_intra": 0.25, "peer_m3": 0.04, "dmc10": 0.00015}
    assert should_hold_dual_oil_rebound(bear_day, hold_bar, gain_pct=0.56, s_sig=False, is_scalp=True) is True
    assert should_exit_scalp_long(hold_bar, entry_px=2.13, entry_sp=-0.45, s_sig=False, day=bear_day, close_wt=0.0)[0] is False
    assert should_exit_long_v9(
        bear_day,
        {**hold_bar, "price": 2.10, "dmc10": -0.0002},
        entry_px=2.13,
        entry_sp=-0.45,
        s_sig=False,
        close_wt=0.0,
    )[0] is True
    assert should_hold_dual_oil_rebound({**bear_day, "cross_market_anchor": "brent"}, hold_bar, gain_pct=0.56) is False
    assert clean_buy_tier(bear_day, {**base_bar, "price": 2.13, "sc_intra": 0.10})[1] == 1.00

    # 4. Offline end-to-end store test + brent isolation check
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    rstore = SQLiteResearchStore(tmp_path / "v9p_161129_dual.db")
    V9MacroDataStore(rstore, allow_network=False)
    now_ts = _time.time()

    def _day_mins_ramp(p0: float, p_end: float) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        for i in range(240):
            hh = ((570 + i + 1) if i < 120 else (780 + (i - 120) + 1)) // 60
            mm = ((570 + i + 1) if i < 120 else (780 + (i - 120) + 1)) % 60
            px = p0 + (p_end - p0) * (i / 239.0)
            out.append((f"{hh:02d}:{mm:02d}", round(px, 4)))
        return out

    oil_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 60.0]]
    uso_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 70.0]]
    bno_rows = [["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 30.0]]
    sc_daily_rows = [["20251101", int(datetime(2025, 11, 2, 15, 5, tzinfo=SHANGHAI_TZ).timestamp()), 520.0, 518.0]]
    sc_min_events: dict[int, float] = {}
    nav_161 = {}

    closes_161 = [2.25 - i * 0.015 for i in range(18)]
    first_td = ""
    last_td = ""
    for idx, c_val in enumerate(closes_161):
        us_d = (datetime(2026, 11, 1, tzinfo=SHANGHAI_TZ) + _timedelta(days=idx)).strftime("%Y%m%d")
        a_d = (datetime(2026, 11, 2, tzinfo=SHANGHAI_TZ) + _timedelta(days=idx)).strftime("%Y%m%d")
        if not first_td:
            first_td = a_d
        last_td = a_d
        cts = int((datetime.strptime(us_d, "%Y%m%d").replace(tzinfo=SHANGHAI_TZ) + _timedelta(days=1, hours=6)).timestamp())
        uso_rows.append([us_d, cts, round(92.0 - idx * 0.8 + (1.5 if idx == len(closes_161) - 1 else 0.0), 3)])
        bno_rows.append([us_d, cts, round(40.0 - idx * 0.35 + (0.6 if idx == len(closes_161) - 1 else 0.0), 3)])
        oil_rows.append([us_d, cts, round(86.0 - idx * 0.7, 3)])
        sc_ts = int(datetime.strptime(a_d, "%Y%m%d").replace(hour=15, minute=5, tzinfo=SHANGHAI_TZ).timestamp())
        sc_c = round(600.0 - idx * 2.5 + (6.0 if idx == len(closes_161) - 1 else 0.0), 2)
        sc_daily_rows.append([a_d, sc_ts, sc_c, round(sc_c - 2.0, 2)])
        sc_min_events[int(datetime.strptime(a_d, "%Y%m%d").replace(hour=9, minute=30, tzinfo=SHANGHAI_TZ).timestamp())] = round(sc_c - 2.0, 2)
        sc_min_events[int(datetime.strptime(a_d, "%Y%m%d").replace(hour=14, minute=50, tzinfo=SHANGHAI_TZ).timestamp())] = sc_c
        nav_161[us_d] = round(c_val - 0.01, 4)

        pre_161 = closes_161[idx - 1] if idx > 0 else 2.26
        _seed_symbol_bars(rstore, "BRN0Y", a_d, 80.0, [("09:30", 80.0), ("14:53", 80.0), ("15:00", 80.0)], name="布伦特", market="ICE")
        _seed_symbol_bars(rstore, "161129", a_d, pre_161, _day_mins_ramp(pre_161, c_val), name="易方达原油")
        _seed_symbol_bars(rstore, "160723", a_d, 2.00, _day_mins_ramp(2.00, 2.00 * (1.008 if idx == len(closes_161) - 1 else 0.996)), name="嘉实原油")
        _seed_symbol_bars(rstore, "501018", a_d, 1.90, _day_mins_ramp(1.90, 1.90 * (1.007 if idx == len(closes_161) - 1 else 0.996)), name="南方原油")

    sc_ts_sorted = sorted(sc_min_events)
    with rstore._lock, rstore._connection:
        for k_name, payload in (
            ("nav_161129", nav_161),
            ("nav_160723", nav_161),
            ("oil_daily_2y", oil_rows),
            ("uso_daily_2y", uso_rows),
            ("bno_daily_2y", bno_rows),
            ("sc0_daily_all", sc_daily_rows),
            ("sc0_min_series", {"ts": sc_ts_sorted, "px": [sc_min_events[t] for t in sc_ts_sorted]}),
            ("fx_usdcny_series", {"ts": [int(datetime(2026, 10, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [6.70]}),
        ):
            rstore._connection.execute(
                "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
                (k_name, _json.dumps(payload), now_ts),
            )

    pos_161 = {
        "mode": "cash_pool_rolling",
        "engine_profile": "v9_plus_aggressive",
        "cross_market_anchor": "uso_bno_sc_peer",
        "min_retained_shares": 100,
        "v9_tuning": {
            "bear_high_prem_flat_pct": 2.5,
            "bull_oil_surge_guard": True,
            "bull_oil_surge_tier_cap_pct": 50.0,
            "scalp_strict_mode": True,
            "macd_price_anchor": 2.0,
        },
    }
    rule_161 = {"id": "ai-161129-v9-plus-aggressive", "name": "161129 双油沪SC同类", "macd_settings": {"short": 10, "long": 20, "signal": 5}}
    bt = evaluate_v9_plus_backtest(
        rstore,
        rule_161,
        {"start_date": first_td, "end_date": last_td, "capital_cap": 50000.0, "round_trip_fee_pct": 0.01},
        pos_161,
        symbol="161129",
        cleaned_end=last_td,
        allow_network=False,
    )
    assert bt["capital_sim"]["evaluated_days"] == len(closes_161)

    dec = evaluate_v9_plus_live_decision(
        rstore,
        symbol="161129",
        trade_date=last_td,
        now_hhmm="14:53",
        cap_limit=50000.0,
        rule_spec=rule_161,
        pos_sizing=pos_161,
        today_signals=[],
        rolling_shares=100,
        allow_network=False,
    )
    ind = evaluate_v9_plus_live_indicators(
        rstore,
        symbol="161129",
        trade_date=last_td,
        now_hhmm="14:53",
        cap_limit=50000.0,
        rule_spec=rule_161,
        pos_sizing=pos_161,
        today_signals=[],
        rolling_shares=100,
        allow_network=False,
    )
    assert dec is not None and ind is not None
    assert dec["diagnostics"]["cross_market_anchor"] == "uso_bno_sc_peer"
    assert ind["diagnostics"]["cross_market_anchor"] == "uso_bno_sc_peer"
    for dk in ("dual_prem_pct", "eq_prem_5d_pct", "rel_prem_pct", "sc_intra_pct", "peer_chg_pct"):
        assert dk in ind["diagnostics"]
    card_keys = {c["key"] for c in ind["market_indicators"]}
    assert {"dual_prem", "sc_peer"} <= card_keys
    assert any(c["id"] == "v9-buy-contrarian" for c in ind["buy"]["conditions"])

    # Isolation: standard 160723 must never emit dual_prem / v9-buy-contrarian
    ind_160 = evaluate_v9_plus_live_indicators(
        rstore,
        symbol="160723",
        trade_date=last_td,
        now_hhmm="14:53",
        cap_limit=50000.0,
        rule_spec={"id": "ai-160723-v9-plus-aggressive", "name": "160723", "macd_settings": {"short": 10, "long": 20, "signal": 5}},
        pos_sizing={"mode": "cash_pool_rolling", "engine_profile": "v9_plus_aggressive", "min_retained_shares": 100},
        today_signals=[],
        rolling_shares=100,
        allow_network=False,
    )
    assert ind_160 is not None
    assert not any(c["id"] == "v9-buy-contrarian" for c in ind_160["buy"]["conditions"])
    assert "dual_prem_pct" not in ind_160["diagnostics"]
    rstore.close()


def _seed_extended_pack_environment(rstore: SQLiteResearchStore, etf_db_path: Path) -> None:
    """Seed deterministic offline macro, L2 multi-period, peer LOF, and ETF cache data for 20260701-20260702."""
    from level2_service.etf_fund_flow import SQLiteEtfFundFlowCache
    from level2_service.quant_rules import V9MacroDataStore

    # Day 0: 20260701
    _seed_symbol_bars(
        rstore,
        "BRN0Y",
        "20260701",
        80.0,
        [("09:30", 80.0), ("10:00", 80.0), ("15:00", 80.0)],
        name="ICE布伦特",
        market="ICE",
    )
    _seed_symbol_bars(
        rstore,
        "160723",
        "20260701",
        1.000,
        [("09:30", 1.000), ("09:45", 1.000), ("10:00", 1.000), ("15:00", 1.000)],
        name="嘉实原油",
    )
    _seed_symbol_bars(
        rstore,
        "501018",
        "20260701",
        1.000,
        [("09:30", 1.000), ("09:45", 1.000), ("10:00", 1.000), ("15:00", 1.000)],
        name="南方原油",
    )

    # Day 1: 20260702 — BRN0Y rallies to 82.0 (+2.5%), SC0 drops -1.0%, 501018 rallies +2.0% while 160723 lags at 0.990 (-1.0% => discount + peer spread -3.0%)
    _seed_symbol_bars(
        rstore,
        "BRN0Y",
        "20260702",
        80.0,
        [("09:30", 81.6), ("10:00", 82.0), ("10:05", 82.1), ("15:00", 82.0)],
        name="ICE布伦特",
        market="ICE",
    )
    _seed_symbol_bars(
        rstore,
        "160723",
        "20260702",
        1.000,
        [
            ("09:30", 0.995),
            ("09:35", 0.994),
            ("09:40", 0.993),
            ("09:45", 0.992),
            ("09:50", 0.991),
            ("09:55", 0.990),
            ("10:00", 0.990),
            ("10:05", 1.002),
            ("10:10", 1.008),
            ("15:00", 1.020),
        ],
        name="嘉实原油",
    )
    _seed_symbol_bars(
        rstore,
        "501018",
        "20260702",
        1.000,
        [("09:30", 1.010), ("10:00", 1.020), ("10:10", 1.020), ("15:00", 1.020)],
        name="南方原油",
    )

    # Seed L2 today / 3d / 5d flow points (today uses 亿元 -> converts *10000 to 万元)
    with rstore._lock, rstore._connection:
        for period, unit, mn, vis, hid, ret in (
            ("today", "亿元", "0.025", "0.010", "0.015", "-0.025"),
            ("three_day", "万元", "420.0", "180.0", "240.0", "-420.0"),
            ("five_day", "万元", "880.0", "350.0", "530.0", "-880.0"),
        ):
            rstore._connection.execute(
                """INSERT INTO fund_flow_history_points(
                       symbol, trade_date, minute, period, unit,
                       main_net_inflow, main_visible_inflow, main_hidden_inflow,
                       retail_inflow, captured_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("160723", "20260702", "10:00", period, unit, mn, vis, hid, ret, "2026-07-02T10:00:00+08:00"),
            )
        rstore._connection.execute(
            """INSERT INTO fund_flow_history_status(
                   symbol, latest_trade_date, latest_time, last_sync_at, last_error
               ) VALUES(?, ?, ?, ?, NULL)
               ON CONFLICT(symbol) DO UPDATE SET latest_trade_date=excluded.latest_trade_date""",
            ("160723", "20260702", "10:00", "2026-07-02T10:00:00+08:00"),
        )

    macro = V9MacroDataStore(rstore, allow_network=False)
    ts_0630 = int(datetime(2026, 6, 30, 15, 0, tzinfo=SHANGHAI_TZ).timestamp())
    ts_0701_us = int(datetime(2026, 7, 1, 4, 0, tzinfo=SHANGHAI_TZ).timestamp())
    ts_0701_15 = int(datetime(2026, 7, 1, 15, 0, tzinfo=SHANGHAI_TZ).timestamp())
    ts_0702_us = int(datetime(2026, 7, 2, 4, 0, tzinfo=SHANGHAI_TZ).timestamp())
    ts_0702_10 = int(datetime(2026, 7, 2, 10, 0, tzinfo=SHANGHAI_TZ).timestamp())
    ts_0702_15 = int(datetime(2026, 7, 2, 15, 0, tzinfo=SHANGHAI_TZ).timestamp())

    macro._write_db_cache("nav_160723", {"20260630": 1.000, "20260701": 1.000}, 9999999999.0)
    ts_0630_oil = int(datetime(2026, 7, 1, 6, 0, tzinfo=SHANGHAI_TZ).timestamp())
    ts_0701_oil = int(datetime(2026, 7, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp())
    ts_0702_oil = int(datetime(2026, 7, 3, 6, 0, tzinfo=SHANGHAI_TZ).timestamp())
    ts_0701_1505 = int(datetime(2026, 7, 1, 15, 5, tzinfo=SHANGHAI_TZ).timestamp())
    ts_0702_1505 = int(datetime(2026, 7, 2, 15, 5, tzinfo=SHANGHAI_TZ).timestamp())
    macro._write_db_cache(
        "oil_daily_2y",
        [["20260630", ts_0630_oil, 78.0], ["20260701", ts_0701_oil, 80.0], ["20260702", ts_0702_oil, 82.0]],
        9999999999.0,
    )
    macro._write_db_cache(
        "fx_usdcny_series",
        {"ts": [ts_0630, ts_0701_15, ts_0702_10, ts_0702_15], "px": [7.20, 7.20, 7.21, 7.21]},
        9999999999.0,
    )
    macro._write_db_cache("xop_daily_2y", [["20260630", ts_0701_us, 130.0], ["20260701", ts_0702_us, 135.2]], 9999999999.0)
    macro._write_db_cache("xle_daily_2y", [["20260630", ts_0701_us, 90.0], ["20260701", ts_0702_us, 91.8]], 9999999999.0)
    macro._write_db_cache("spy_daily_2y", [["20260630", ts_0701_us, 550.0], ["20260701", ts_0702_us, 555.5]], 9999999999.0)
    macro._write_db_cache("iwm_daily_2y", [["20260630", ts_0701_us, 210.0], ["20260701", ts_0702_us, 212.1]], 9999999999.0)
    macro._write_db_cache("cl_daily_2y", [["20260630", ts_0701_us, 76.0], ["20260701", ts_0702_us, 77.52]], 9999999999.0)
    macro._write_db_cache("ho_daily_2y", [["20260630", ts_0701_us, 2.40], ["20260701", ts_0702_us, 2.496]], 9999999999.0)
    macro._write_db_cache("ng_daily_2y", [["20260630", ts_0701_us, 2.50], ["20260701", ts_0702_us, 2.55]], 9999999999.0)
    macro._write_db_cache("uso_daily_2y", [["20260630", ts_0701_us, 74.0], ["20260701", ts_0702_us, 75.48]], 9999999999.0)
    macro._write_db_cache("bno_daily_2y", [["20260630", ts_0701_us, 30.0], ["20260701", ts_0702_us, 30.60]], 9999999999.0)
    macro._write_db_cache(
        "sc0_daily_all",
        [["20260701", ts_0701_1505, 600.0, 600.0], ["20260702", ts_0702_1505, 594.0, 596.0]],
        9999999999.0,
    )
    macro._write_db_cache(
        "sc0_min_series",
        {"ts": [ts_0701_15, ts_0702_10], "px": [600.0, 594.0]},
        9999999999.0,
    )

    etf_cache = SQLiteEtfFundFlowCache(etf_db_path)
    etf_cache.put(
        "line:160723:60d",
        symbol="160723",
        kind="line",
        data_date="2026-07-03",
        updated_at=9999999999.0,
        value=[
            ["2026-06-29", "06-29", 1000000.0],
            ["2026-06-30", "06-30", 2000000.0],
            ["2026-07-01", "07-01", 5000000.0],
            ["2026-07-02", "07-02", -99000000.0],  # Must NOT leak into trade_date=20260702
            ["2026-07-03", "07-03", -88000000.0],
        ],
    )
    etf_cache.put(
        "summary:160723",
        symbol="160723",
        kind="summary",
        data_date="2026-07-01",
        updated_at=9999999999.0,
        value={
            "stockEtfNetBuyFlowDate": "2026-07-01",
            "stockEtfNetBuyFlow": 5000000.0,
            "stockEtfNetBuyFlow3d": 8000000.0,
            "stockEtfFundScale": 1000000000.0,
        },
    )
    etf_cache.put(
        "holdings:160723",
        symbol="160723",
        kind="holdings",
        data_date="2026-07-01",
        updated_at=9999999999.0,
        value=[{"etf_code": "160723", "name": "嘉实原油LOF", "net_inflow_raw": 5000000.0, "streak_days": 3, "hold_rate": 12.5}],
    )
    etf_cache.close()


def test_v4_dataset_export_five_data_packs_and_causal_t1_cutoff(tmp_path: Path) -> None:
    db_path = tmp_path / "research.db"
    etf_db_path = tmp_path / "etf-fund-flow.db"
    rstore = SQLiteResearchStore(db_path)
    _seed_extended_pack_environment(rstore, etf_db_path)

    summary = export_quant_dataset(
        rstore,
        "160723",
        start_date="20260701",
        mode="summary",
        etf_flow_db_path=etf_db_path,
    )
    assert summary["schema"]["version"] == 4
    avail = summary["available_data_packs"]
    assert avail["l2_darkpool_flow"]["available"] is True
    assert avail["etf_redemption_flow"]["available"] is True
    assert avail["iopv_fx"]["available"] is True
    assert avail["xop_us_ep"]["available"] is True
    assert avail["uso_bno_sc_peer"]["available"] is True

    # Daily pack whitelist isolation + strict T-1 ETF cutoff (20260702 sees 20260701=500万, never 20260702=-9900万)
    daily_res = export_quant_dataset(
        rstore,
        "160723",
        start_date="20260701",
        mode="daily",
        packs="iopv_fx,etf_redemption_flow",
        allow_network=False,
        etf_flow_db_path=etf_db_path,
    )
    assert daily_res["data_packs_included"] == ["iopv_fx", "etf_redemption_flow"]
    day_0702 = next(d for d in daily_res["days"] if d["trade_date"] == "20260702")
    assert day_0702["etf_flow_ref_date"] == "20260701"
    assert day_0702["etf_net_buy_1d_wan"] == 500.0
    assert day_0702["etf_net_buy_3d_wan"] == 800.0
    assert day_0702["etf_share_scale_yi"] == 10.0
    assert day_0702["iopv_fx_close"] is not None
    assert "xop_ov_pct" not in day_0702

    # Minutes export with remaining 3 packs
    min_res = export_quant_dataset(
        rstore,
        "160723",
        start_date="20260702",
        mode="minutes",
        packs=["l2_darkpool_flow", "uso_bno_sc_peer", "xop_us_ep"],
        allow_network=False,
        etf_flow_db_path=etf_db_path,
    )
    assert min_res["data_packs_included"] == ["l2_darkpool_flow", "uso_bno_sc_peer", "xop_us_ep"]
    pt_1000 = next(p for p in min_res["days"][0]["points"] if p["time"] == "10:00")
    assert abs(pt_1000["l2_today_main_net_wan"] - 250.0) < 0.01  # 0.025亿元 => 250万元
    assert abs(pt_1000["l2_today_hidden_wan"] - 150.0) < 0.01
    assert abs(pt_1000["l2_3d_hidden_wan"] - 240.0) < 0.01
    assert abs(pt_1000["l2_5d_hidden_wan"] - 530.0) < 0.01
    assert abs(pt_1000["xop_change_pct"] - 4.0) < 0.05
    assert abs(pt_1000["sc0_change_pct"] - (-1.0)) < 0.05
    assert abs(pt_1000["peer_501018_spread_pct"] - (-3.0)) < 0.05
    assert "iopv_premium_pct" not in pt_1000
    rstore.close()


def test_extended_dsl_operators_backtest_and_live_autopilot_symmetry(tmp_path: Path) -> None:
    db_path = tmp_path / "research.db"
    etf_db_path = tmp_path / "etf-fund-flow.db"
    rstore = SQLiteResearchStore(db_path)
    qstore = SQLiteQuantRuleStore(db_path)
    astore = SQLiteAutoTradeStore(db_path)
    _seed_extended_pack_environment(rstore, etf_db_path)

    rule_spec = {
        "id": "ext-7ops-symmetry",
        "name": "partner01-160723-嘉实原油-全维度7算子共振",
        "symbols": ["160723"],
        "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0},
        "buy_rule": {
            "id": "b-7ops",
            "side": "buy",
            "title": "7算子共振买点",
            "joiner": "and",
            "conditions": [
                {"type": "iopv_premium_pct", "metric": "premium_pct", "direction": "lte", "value": -1.5},
                {"type": "fx_spread_pct", "metric": "usdcny", "direction": "gte", "value": 7.15},
                {"type": "xop_macro_pct", "metric": "xop_change_pct", "direction": "gte", "value": 3.0},
                {"type": "sc_crude_pct", "metric": "sc0_change_pct", "direction": "lte", "value": -0.5},
                {"type": "peer_spread_pct", "peer": "501018", "metric": "spread_pct", "direction": "lte", "value": -2.0},
                {"type": "l2_flow_level", "period": "three_day", "flow": "main_hidden", "direction": "gte", "value": 200.0},
                {"type": "etf_net_flow", "metric": "net_buy_1d_wan", "direction": "gte", "value": 300.0},
                {"type": "time_window", "windows": [["09:58", "10:02"]]},
            ],
        },
        "sell_rule": {
            "id": "s-7ops",
            "side": "sell",
            "title": "尾盘涨幅止盈",
            "joiner": "and",
            "conditions": [{"type": "change_pct", "direction": "gte", "value": 1.8}],
        },
        "eval_criteria": {
            "start_date": "20260702",
            "data_scope": "all",
            "morning_start": "09:30",
            "morning_end": "11:30",
            "max_daily_trades": 1,
            "round_trip_fee_pct": 0.02,
        },
    }

    eval_res = evaluate_quant_strategy(rstore, rule_spec, symbol="160723")
    assert eval_res["base_position_t0"]["buy_t_days"] == 1
    assert any(sig["time"] == "10:00" and sig["side"] == "buy" for sig in eval_res["signals"])

    saved = qstore.upsert_rule(rule_spec, research_store=rstore)
    astore.update_config(
        {
            "enabled": True,
            "symbols": ["160723"],
            "signal_source": "quant_rule",
            "quant_rule_id": saved["id"],
        }
    )
    # Trim post-10:00 bars so the simulated 10:00 live snapshot evaluates at 10:00 causally
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "DELETE FROM research_points WHERE trade_date='20260702' AND REPLACE(minute, ':', '') > '1000'"
        )
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=None,
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: datetime(2026, 7, 2, 10, 0, tzinfo=SHANGHAI_TZ),
        epoch_fn=lambda: 1_800_000_000.0,
        quant_rule_store=qstore,
    )
    snap = engine.evaluate_live_snapshot(symbol="160723")
    assert snap["buy"]["matched"] is True
    assert len(snap["buy"]["conditions"]) == 8
    assert all(c["matched"] for c in snap["buy"]["conditions"])
    rstore.close()


def test_bridge_strict_dsl_120_char_name_metadata_and_symbol_filter(tmp_path: Path) -> None:
    db_path = tmp_path / "research.db"
    rstore = SQLiteResearchStore(db_path)
    qstore = SQLiteQuantRuleStore(db_path)
    astore = SQLiteAutoTradeStore(db_path)
    _seed_day(rstore, "20260702", [1.0 + i * 0.002 for i in range(15)], pre_close=1.0, flows=[50.0] * 15)

    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=None,
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: datetime(2026, 7, 2, 10, 5, tzinfo=SHANGHAI_TZ),
        epoch_fn=lambda: 1_800_000_000.0,
        quant_rule_store=qstore,
    )
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-pass-123"),
        admin_session_secret="b" * 32,
        symbol_lookup=lambda sym: CATALOG[sym],
        market_account_store=SQLiteMarketAccountStore(tmp_path / "market.db"),
        research_store=rstore,
        goodluck_internal_token=TOKEN,
        auto_trade_engine=engine,
        quant_rule_store=qstore,
    )
    client = TestClient(app)

    # Unknown DSL operator must be rejected with 422
    bad_resp = client.post(
        "/api/internal/goodluck/quant/rules",
        headers=_headers(),
        json={
            "name": "bad-op-rule",
            "symbols": ["160723"],
            "buy_rule": {"conditions": [{"type": "totally_unknown_op", "value": 1}]},
        },
    )
    assert bad_resp.status_code == 422

    # 110-char canonical rule name + provenance metadata
    long_name = "quant_partner_01-501018-南方原油-" + ("超短跨市场均值回归多因子联动" * 4)[:60]
    assert 80 < len(long_name) <= 120
    create_resp = client.post(
        "/api/internal/goodluck/quant/rules",
        headers=_headers(),
        json={
            "id": "ext-501018-canonical",
            "name": long_name,
            "source": "ai",
            "symbols": ["501018"],
            "target_symbol": "501018",
            "stock_name": "南方原油",
            "raw_strategy_name": "超短跨市场均值回归多因子联动",
            "submitter_id": "key_partner_01",
            "submitter_name": "quant_partner_01",
            "submission_id": "sub_001",
            "approved_by": "admin",
            "approved_at": "2026-07-02T12:00:00Z",
            "buy_rule": {"conditions": [{"type": "iopv_premium_pct", "metric": "premium_pct", "direction": "lte", "value": -1.0}]},
        },
    )
    assert create_resp.status_code == 200
    saved_rule = create_resp.json()["rule"]
    assert saved_rule["name"] == long_name
    assert saved_rule["submitter_name"] == "quant_partner_01"
    assert saved_rule["target_symbol"] == "501018"

    # Symbol filter strictly separates 501018 vs 160723
    rules_501 = client.get("/api/internal/goodluck/quant/rules?symbol=501018", headers=_headers()).json()["rules"]
    rules_160 = client.get("/api/internal/goodluck/quant/rules?symbol=160723", headers=_headers()).json()["rules"]
    assert any(r["id"] == "ext-501018-canonical" for r in rules_501)
    assert not any(r["id"] == "ext-501018-canonical" for r in rules_160)
    rstore.close()


def test_v9_plus_three_state_nav_reconciliation_and_compact_strip(tmp_path: Path, monkeypatch) -> None:
    import json as _json
    import time as _time
    from datetime import timedelta as _timedelta
    from level2_service.quant_rules import compact_quant_rule_record
    from level2_service.v9_plus_engine import V9MacroDataStore, evaluate_v9_plus_backtest

    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    rstore = SQLiteResearchStore(tmp_path / "v9p_reconcile.db")
    V9MacroDataStore(rstore, allow_network=False)
    now_ts = _time.time()

    # Build 21 trading days across 2026-10 and 2026-11 so multi-month NAV compounding & events execute
    all_tds = [
        f"202610{d:02d}" for d in range(1, 20)
    ] + ["20261102", "20261103"]
    nav_payload = {"20260925": 1.000, **{td: 1.000 for td in all_tds}}
    oil_rows = [
        ["20251001", int(datetime(2025, 10, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 60.0],
        ["20260925", int(datetime(2026, 9, 26, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 100.0],
    ]
    for td in all_tds:
        y, m, d = int(td[:4]), int(td[4:6]), int(td[6:8])
        px = 72.0 if td == "20261102" else (96.0 if td == "20261103" else 70.0)
        ts = int((datetime(y, m, d, 6, 0, tzinfo=SHANGHAI_TZ) + _timedelta(days=1)).timestamp())
        oil_rows.append([td, ts, px])
        brn_bars = [("09:30", 70.0 if td != "20261103" else 72.0), ("10:00", px), ("14:53", px), ("15:00", px)]
        _seed_symbol_bars(rstore, "BRN0Y", td, px, brn_bars, name="布伦特", market="ICE")
        # Intraday dip then recovery + day-over-day move across 2026-11-02 -> 2026-11-03
        base_px = 1.050 if td == "20261102" else (1.080 if td == "20261103" else 1.000)
        bars: list[tuple[str, float]] = []
        for idx in range(241):
            hh = 9 + (30 + idx) // 60 if idx < 120 else 13 + (idx - 120) // 60
            mm = (30 + idx) % 60 if idx < 120 else (idx - 120) % 60
            tstr = f"{hh:02d}:{mm:02d}"
            p = base_px
            if "09:42" <= tstr <= "09:50":
                p = round(base_px * 0.985, 3)
            elif "14:15" <= tstr <= "14:52":
                p = round(base_px * 1.006, 3)
            elif tstr >= "14:53":
                p = round(base_px * (1.002 if td == "20261102" else 1.008), 3)
            bars.append((tstr, p))
        _seed_symbol_bars(rstore, "160723", td, round(base_px * 0.998, 3), bars, name="嘉实原油")

    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("nav_160723", _json.dumps(nav_payload), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("oil_daily_2y", _json.dumps(oil_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcny_series", _json.dumps({"ts": [int(datetime(2026, 9, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [6.80]}), now_ts),
        )

    res = evaluate_v9_plus_backtest(
        rstore,
        {
            "id": "ai-160723-v9-plus-aggressive",
            "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0002},
        },
        {"start_date": "20261001", "round_trip_fee_pct": 0.01, "capital_cap": 50000.0},
        {
            "mode": "cash_pool_rolling",
            "engine_profile": "v9_plus_aggressive",
            "close_flatten_time": "14:53",
            "max_daily_entries": 8,
            "cooldown_minutes": 4,
            "scale_factor": 1.0,
        },
        symbol="160723",
        cleaned_end="20261103",
        signal_limit=200,
        allow_network=False,
    )
    t0 = res["base_position_t0"]
    assert t0["accounting_mode"] == "three_state_nav"
    assert t0["account_events_truncated"] is False
    events = t0["account_events"]
    assert len(events) > 0
    assert any(ev["event_kind"] == "overnight_hold" for ev in events)
    assert any(ev["event_kind"] in {"intraday_t0", "close_rebalance"} for ev in events)

    # Monthly identity: overnight_return_pct + intraday_t0_return_pct == net_return_pct == sum(month_contrib_pct)
    for m in t0["monthly_returns"]:
        m_key = m["month"]
        m_evs = [ev for ev in events if ev["month"] == m_key]
        sum_month = sum(float(ev["month_contrib_pct"]) for ev in m_evs)
        sum_ov = sum(float(ev["month_contrib_pct"]) for ev in m_evs if ev["event_kind"] == "overnight_hold")
        sum_intra = sum(float(ev["month_contrib_pct"]) for ev in m_evs if ev["event_kind"] != "overnight_hold")
        assert abs((m["overnight_return_pct"] + m["intraday_t0_return_pct"]) - m["net_return_pct"]) < 1e-2
        assert abs(sum_month - m["net_return_pct"]) < 1e-2
        assert abs(sum_ov - m["overnight_return_pct"]) < 1e-2
        assert abs(sum_intra - m["intraday_t0_return_pct"]) < 1e-2

    # Overall evaluation identity: sum(total_contrib_pct) == net_cum_return_pct
    assert abs((t0["overnight_return_pct"] + t0["intraday_t0_return_pct"]) - t0["net_cum_return_pct"]) < 0.02
    assert abs(sum(float(ev["total_contrib_pct"]) for ev in events) - t0["net_cum_return_pct"]) < 0.02

    # Position transition continuity in chronological order: every event explains shares & weight change (pure %)
    chrono_events = list(reversed(events))
    assert chrono_events
    for idx_ev, ev in enumerate(chrono_events):
        assert isinstance(ev.get("before_shares"), int)
        assert isinstance(ev.get("after_shares"), int)
        act_lbl = str(ev.get("position_action_label") or "").strip()
        trans_lbl = str(ev.get("position_transition_label") or "").strip()
        ev_title = str(ev.get("title") or "").strip()
        assert act_lbl and "手" not in act_lbl
        assert trans_lbl and "手" not in trans_lbl
        assert "手" not in ev_title
        if idx_ev > 0:
            assert chrono_events[idx_ev - 1]["after_shares"] == ev["before_shares"], (
                f"Share discontinuity between {chrono_events[idx_ev - 1]} and {ev}"
            )

    # Compact rule list strips heavy account_events and sets account_events_truncated=True
    compacted = compact_quant_rule_record({"id": "ai-160723-v9-plus-aggressive", "latest_eval": res})
    assert compacted["latest_eval"]["base_position_t0"]["account_events"] == []
    assert compacted["latest_eval"]["base_position_t0"]["account_events_truncated"] is True
    assert len(compacted["latest_eval"]["base_position_t0"]["monthly_returns"]) == len(t0["monthly_returns"])
    rstore.close()


def test_export_dataset_supports_all_13_monitor_sources_and_orderbook_qdii_snapshots(
    tmp_path: Path, monkeypatch
) -> None:
    import json as _json
    import time as _time
    from level2_service.v9_plus_engine import V9MacroDataStore

    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    rstore = SQLiteResearchStore(tmp_path / "monitor_sources_export.db")
    qstore = SQLiteQuantRuleStore(tmp_path / "monitor_sources_export.db")
    V9MacroDataStore(rstore, allow_network=False)
    now_ts = _time.time()

    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("nav_160723", _json.dumps({"20260701": 1.8825, "20260702": 1.9050}), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            (
                "fx_usdcnh_series",
                _json.dumps(
                    {
                        "ts": [int(datetime(2026, 7, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())],
                        "px": [7.12345],
                    }
                ),
                now_ts,
            ),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            (
                "uso_daily_2y",
                _json.dumps([
                    ["20260701", int(datetime(2026, 7, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 80.0],
                    ["20260702", int(datetime(2026, 7, 3, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 81.6],
                ]),
                now_ts,
            ),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            (
                "bno_daily_2y",
                _json.dumps([
                    ["20260701", int(datetime(2026, 7, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 30.0],
                    ["20260702", int(datetime(2026, 7, 3, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 30.9],
                ]),
                now_ts,
            ),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            (
                "dbo_daily_2y",
                _json.dumps([
                    ["20260701", int(datetime(2026, 7, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 50.0],
                    ["20260702", int(datetime(2026, 7, 3, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 50.5],
                ]),
                now_ts,
            ),
        )

    rstore.save_premium_snapshot(
        {
            "as_of": "2026-07-03T10:05:00+08:00",
            "rows": [
                {
                    "code": "160723",
                    "name": "嘉实原油",
                    "current_price": 1.912,
                    "estimated_price": 1.930,
                    "premium_rate": -0.009326,
                    "estimate_source": "HaoETF",
                    "stale": False,
                }
            ],
            "valid_count": 1,
            "errors": {},
        }
    )

    for td, px in (("20260701", 1.880), ("20260702", 1.895), ("20260703", 1.912)):
        _seed_symbol_bars(
            rstore,
            "160723",
            td,
            px,
            [("09:30", px), ("10:00", px), ("15:00", px)],
            name="嘉实原油",
        )

    engine = AutoTradeEngine(
        auto_store=SQLiteAutoTradeStore(tmp_path / "monitor_sources_export.db"),
        research_store=rstore,
        broker=None,
        trade_executor=None,
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: datetime(2026, 7, 3, 10, 5, tzinfo=SHANGHAI_TZ),
        epoch_fn=lambda: 1_800_000_000.0,
        quant_rule_store=qstore,
    )
    import time as _tm

    engine._order_book_cache["160723"] = (
        _tm.monotonic(),
        {
            "symbol": "160723",
            "name": "嘉实原油",
            "last_price": 1.912,
            "previous_close": 1.895,
            "limit_up": 2.085,
            "limit_down": 1.706,
            "bids": [{"level": 1, "price": 1.911, "volume": 1200}],
            "asks": [{"level": 1, "price": 1.912, "volume": 800}],
            "source_time": "10:05:00",
        },
    )

    # Export with monitor source IDs directly
    ds_res = export_quant_dataset(
        rstore,
        "160723",
        start_date="20260703",
        end_date="20260703",
        mode="minutes",
        sources=(
            "ashare_intraday_1m,"
            "ashare_orderbook_l1,"
            "fx_usdcny_usdcnh,"
            "eastmoney_fund_nav,"
            "qdii_premium_panel_feed,"
            "us_oil_etf_uso_bno_dbo"
        ),
        auto_trade_engine=engine,
    )
    assert set(ds_res["data_packs_included"]) == {"iopv_fx", "uso_bno_sc_peer"}
    assert "ashare_orderbook_l1" in ds_res["requested_sources"]
    assert len(ds_res["monitored_data_sources"]) == 13
    assert ds_res["orderbook_l1_snapshot"]["bids"][0]["price"] == 1.911
    assert ds_res["qdii_panel_snapshot"]["premium_pct"] == -0.933

    day_row = ds_res["days"][0]
    assert day_row["day_pack_summary"]["official_nav"] == 1.8825
    assert day_row["day_pack_summary"]["official_nav_date"] == "20260701"
    assert abs(day_row["day_pack_summary"]["uso_ov_pct"] - 2.0) < 0.01
    assert abs(day_row["day_pack_summary"]["bno_ov_pct"] - 3.0) < 0.01
    assert abs(day_row["day_pack_summary"]["dbo_ov_pct"] - 1.0) < 0.01
    assert abs(day_row["day_pack_summary"]["fof_3oil_ov_pct"] - 2.05) < 0.01

    pt0 = day_row["points"][0]
    assert pt0["usdcnh"] == 7.12345
    assert pt0["official_nav"] == 1.8825
    assert abs(pt0["dbo_change_pct"] - 1.0) < 0.01

    # HTTP bridge /quant/dataset?sources=... passthrough
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-pass-123"),
        admin_session_secret="c" * 32,
        symbol_lookup=lambda sym: CATALOG[sym],
        market_account_store=SQLiteMarketAccountStore(tmp_path / "market.db"),
        research_store=rstore,
        goodluck_internal_token=TOKEN,
        auto_trade_engine=engine,
        quant_rule_store=qstore,
    )
    client = TestClient(app)
    http_res = client.get(
        "/api/internal/goodluck/quant/dataset?symbol=160723&start_date=20260703&mode=summary&sources=ashare_orderbook_l1,fx_usdcny_usdcnh",
        headers=_headers(),
    )
    assert http_res.status_code == 200
    body = http_res.json()
    assert body["orderbook_l1_snapshot"]["last_price"] == 1.912
    assert len(body["monitored_data_sources"]) == 13
    rstore.close()
    qstore.close()


