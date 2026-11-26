from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
import time

from argon2 import PasswordHasher
from fastapi.testclient import TestClient
import pytest

from level2_service.api import create_app
from level2_service.auto_trade import (
    SHANGHAI_TZ,
    AutoTradeEngine,
    SQLiteAutoTradeStore,
)
from level2_service.market_accounts import SQLiteMarketAccountStore
from level2_service.parsed_values import SymbolLookup
from level2_service.quant_data_sources import (
    QUANT_DATA_SOURCE_CATALOG,
    VALID_QUANT_DATA_SOURCE_IDS,
    _check_macro_source_health,
    build_quant_data_sources_report,
    check_rule_live_data_guard,
    derive_rule_data_sources,
    derive_rule_usage_tags,
    normalize_and_validate_rule_data_sources,
)
from level2_service.quant_rules import SQLiteQuantRuleStore, get_quant_rule_schema
from level2_service.research_store import SQLiteResearchStore
from level2_service.v9_fof_crude import fetch_live_fof_crude_spot
from level2_service.v9_plus_engine import (
    _MEMORY_MACRO_CACHE,
    V9MacroDataStore,
    _enrich_v9_multi_oil_days,
    get_live_spot_telemetry,
)


TOKEN = "quant-ds-test-token"
CATALOG = {
    "160723": SymbolLookup(symbol="160723", name="嘉实原油LOF", market="36"),
    "161129": SymbolLookup(symbol="161129", name="易方达原油LOF", market="36"),
    "501018": SymbolLookup(symbol="501018", name="南方原油LOF", market="20"),
    "162411": SymbolLookup(symbol="162411", name="华宝油气LOF", market="36"),
}


def _headers(role: str = "admin", user: str = "domain:wilson") -> dict[str, str]:
    return {
        "X-Goodluck-Internal-Token": TOKEN,
        "X-Goodluck-User": user,
        "X-Goodluck-Role": role,
    }


def test_derive_and_validate_rule_data_sources_covers_all_rule_archetypes(tmp_path: Path) -> None:
    schema = get_quant_rule_schema()
    assert schema["version"] >= 4
    assert "data_sources_catalog" in schema
    assert len(schema["data_sources_catalog"]) >= 13
    assert all(isinstance(item.get("usage_tags"), list) and item["usage_tags"] for item in schema["data_sources_catalog"])

    fof_ds = derive_rule_data_sources(
        {
            "symbols": ["160723", "BRN0Y"],
            "position_sizing": {"engine_profile": "v9_plus_aggressive", "cross_market_anchor": "fof_crude_core"},
        }
    )
    fof_ids = {s["id"] for s in fof_ds}
    assert {
        "ashare_intraday_1m",
        "ashare_orderbook_l1",
        "ice_brent_1m",
        "ashare_peer_lof_quote",
        "ine_sc0_futures",
        "us_oil_etf_uso_bno_dbo",
        "cme_energy_macro_futures",
        "fx_usdcny_usdcnh",
        "eastmoney_fund_nav",
    }.issubset(fof_ids)
    assert "us_ep_xop_crack_macro" not in fof_ids

    xop_ds = derive_rule_data_sources(
        {
            "symbols": ["162411", "BRN0Y"],
            "position_sizing": {"engine_profile": "v9_plus_aggressive", "cross_market_anchor": "xop_us_ep"},
        }
    )
    xop_ids = {s["id"] for s in xop_ds}
    assert xop_ids == {
        "ashare_intraday_1m",
        "ashare_orderbook_l1",
        "ice_brent_1m",
        "us_ep_xop_crack_macro",
        "cme_energy_macro_futures",
        "fx_usdcny_usdcnh",
        "eastmoney_fund_nav",
    }
    xop_by_id = {s["id"]: s for s in xop_ds}
    assert xop_by_id["us_ep_xop_crack_macro"]["symbols"] == ["XOP", "XLE", "SPY", "IWM"]
    assert xop_by_id["cme_energy_macro_futures"]["symbols"] == ["CL", "HO", "NG", "ES"]
    assert xop_by_id["fx_usdcny_usdcnh"]["symbols"] == ["USDCNY", "USDCNH"]
    assert xop_by_id["ashare_orderbook_l1"]["required_in_session"] is False

    l2_ids = {
        s["id"]
        for s in derive_rule_data_sources(
            {
                "symbols": ["601872"],
                "buy_rule": {"conditions": [{"type": "l2_flow_level", "period": "today", "flow": "main_hidden", "direction": "gte", "value": 500}]},
            }
        )
    }
    assert {"ashare_intraday_1m", "ashare_orderbook_l1", "ashare_l2_fund_flow"}.issubset(l2_ids)

    with pytest.raises(ValueError, match="未知量化数据源"):
        normalize_and_validate_rule_data_sources([{"id": "non_existent_feed"}], {"symbols": ["160723"]})

    with pytest.raises(ValueError, match="必须显式声明 data_sources"):
        normalize_and_validate_rule_data_sources(None, {"symbols": ["160723"]}, strict_declared=True)

    with pytest.raises(ValueError, match="未完整声明依赖的数据源"):
        normalize_and_validate_rule_data_sources(
            ["ashare_intraday_1m"],
            {"symbols": ["160723", "BRN0Y"], "position_sizing": {"engine_profile": "v9_plus_aggressive"}},
            strict_declared=True,
        )

    # Backfill existing pre-v5 rules automatically on store init
    db_path = tmp_path / "rules.db"
    rstore = SQLiteResearchStore(db_path)
    qstore = SQLiteQuantRuleStore(db_path)
    rules = qstore.list_rules()
    assert len(rules) >= 2
    for r in rules:
        assert isinstance(r.get("data_sources"), list) and len(r["data_sources"]) >= 1
        assert all(ds["id"] in VALID_QUANT_DATA_SOURCE_IDS for ds in r["data_sources"])
    qstore.close()
    rstore.close()


def test_intraday_missing_data_guard_blocks_stale_feed_and_surfaces_in_live_eval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    db_path = tmp_path / "guard.db"
    rstore = SQLiteResearchStore(db_path)
    qstore = SQLiteQuantRuleStore(db_path)
    astore = SQLiteAutoTradeStore(db_path)

    # Seed macro caches for 160723 V9+
    V9MacroDataStore(rstore, allow_network=False)
    now_ts = time.time()
    oil_rows = [
        ["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 60.0],
        ["20261125", int(datetime(2026, 11, 26, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 90.0],
    ]
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("nav_160723", json.dumps({"20261124": 2.000, "20261125": 2.000}), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("oil_daily_2y", json.dumps(oil_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcny_series", json.dumps({"ts": [int(datetime(2026, 11, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [6.70]}), now_ts),
        )

    def _seed_day(sym: str, td: str, pre: float, bars: list[tuple[str, float]], collected_at: str, market: str = "36") -> None:
        with rstore._lock, rstore._connection:
            rstore._connection.execute(
                """INSERT OR REPLACE INTO research_days(
                       symbol, trade_date, name, market, source, pre_close, quote_price, collected_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (sym, td, sym, market, "TEST", f"{pre:.3f}", f"{bars[-1][1]:.3f}", collected_at),
            )
            rstore._connection.executemany(
                """INSERT OR REPLACE INTO research_points(
                       symbol, trade_date, minute, price, average_price, volume, captured_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                [(sym, td, hhmm, f"{px:.3f}", f"{px:.3f}", "10000", collected_at) for hhmm, px in bars],
            )

    _seed_day("BRN0Y", "20261125", 70.0, [("09:30", 70.0), ("15:00", 70.0)], "2026-11-25T15:00:00+08:00", market="ICE")
    _seed_day("160723", "20261125", 2.000, [("09:31", 2.000), ("15:00", 2.000)], "2026-11-25T15:00:00+08:00")

    # Seed STALE today data: frozen at 09:45 while clock is 10:15 (30m lag > 3m and sync age > 180s)
    stale_brn = [("09:30", 70.0), ("09:44", 72.5), ("09:45", 72.5)]
    stale_lof = [("09:31", 2.000), ("09:44", 1.982), ("09:45", 1.980)]
    _seed_day("BRN0Y", "20261126", 70.0, stale_brn, "2026-11-26T09:45:00+08:00", market="ICE")
    _seed_day("160723", "20261126", 2.000, stale_lof, "2026-11-26T09:45:00+08:00")

    qstore.upsert_rule(
        {
            "id": "ai-160723-v9-guard-test",
            "name": "V9+ 缺数防误单测试规则",
            "source": "ai",
            "symbols": ["160723", "BRN0Y"],
            "buy_rule": {"title": "正T买", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "lte", "value": -0.25}]},
            "sell_rule": {"title": "反T卖", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "gte", "value": 0.42}]},
            "position_sizing": {
                "mode": "cash_pool_rolling",
                "engine_profile": "v9_plus_aggressive",
                "default_weight_pct": 80.0,
                "max_position_pct": 100.0,
            },
        }
    )
    astore.update_config(
        {
            "enabled": True,
            "signal_source": "quant_rule",
            "quant_rule_id": "ai-160723-v9-guard-test",
            "execution_mode": "semi_auto",
            "sizing_mode": "model_auto",
            "symbols": ["160723"],
            "daily_max_buy_amount": 50000.0,
            "block_non_continuous_session": True,
        }
    )

    clock_dt = {"dt": datetime(2026, 11, 26, 10, 15, tzinfo=SHANGHAI_TZ)}
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=None,
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: clock_dt["dt"],
        quant_rule_store=qstore,
    )

    # 1. Poll during stale 09:45 @ 10:15 -> MUST NOT emit any trade signals
    poll_stale = asyncio.run(engine.poll_once())
    assert poll_stale["triggered"] == 0
    assert astore.list_pending_signals() == []

    # live-eval MUST surface blocked guard and force data_ready=False & matched=False
    live_stale = engine.evaluate_live_snapshot("160723")
    assert live_stale["data_ready"] is False
    assert live_stale["data_guard"]["blocked"] is True
    assert "ashare_intraday_1m" in live_stale["data_guard"]["blocking_sources"]
    assert "ice_brent_1m" in live_stale["data_guard"]["blocking_sources"]
    assert live_stale["buy"]["matched"] is False
    assert live_stale["buy"]["conditions"][0]["id"] == "data-source-guard"

    # 2. Outside continuous session (e.g. 19:30 post-close), check_rule_live_data_guard stands by cleanly
    closed_guard = check_rule_live_data_guard(
        rstore,
        qstore.get_rule("ai-160723-v9-guard-test"),  # type: ignore[arg-type]
        symbol="160723",
        trade_date="20261126",
        actual_trade_date="20261126",
        points=[],
        now_dt=datetime(2026, 11, 26, 19, 30, tzinfo=SHANGHAI_TZ),
        is_trading_day=True,
    )
    assert closed_guard["blocked"] is False
    assert closed_guard["status"] == "standby_closed"

    # 3. Restore fresh 10:15 bars on both 160723 and BRN0Y -> live-eval recovers data_ready=True
    fresh_brn = [("09:30", 70.0)] + [(f"{9 + (31 + i) // 60:02d}:{(31 + i) % 60:02d}", 72.1) for i in range(45)]
    fresh_lof = [(f"{9 + (31 + i) // 60:02d}:{(31 + i) % 60:02d}", 1.980) for i in range(45)]
    _seed_day("BRN0Y", "20261126", 70.0, fresh_brn, "2026-11-26T10:15:00+08:00", market="ICE")
    _seed_day("160723", "20261126", 2.000, fresh_lof, "2026-11-26T10:15:00+08:00")

    live_fresh = engine.evaluate_live_snapshot("160723")
    assert live_fresh["data_guard"]["blocked"] is False
    assert live_fresh["data_ready"] is True

    # 4. Also verify bridge endpoints enforce admin auth and return full report structure
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-pass-123"),
        admin_session_secret="d" * 32,
        symbol_lookup=lambda sym: CATALOG[sym],
        market_account_store=SQLiteMarketAccountStore(tmp_path / "market.db"),
        research_store=rstore,
        goodluck_internal_token=TOKEN,
        auto_trade_engine=engine,
        quant_rule_store=qstore,
    )
    client = TestClient(app)

    r_user = client.get("/api/internal/goodluck/quant/data-sources", headers=_headers(role="user", user="uid:u1"))
    assert r_user.status_code == 403

    r_admin = client.get("/api/internal/goodluck/quant/data-sources", headers=_headers(role="admin"))
    assert r_admin.status_code == 200
    body = r_admin.json()
    assert body["summary"]["total_sources"] >= 13
    assert len(body["sources"]) >= 13
    assert len(body["rules"]) >= 3
    assert len(body["market_sessions"]) == 6
    assert any(s["market"] == "cme_futures" for s in body["market_sessions"])
    for src in body["sources"]:
        assert isinstance(src.get("usage_tags"), list) and len(src["usage_tags"]) >= 1
        for br in src.get("bound_rules") or []:
            assert br.get("short_label")
            assert br.get("role_label")
            assert isinstance(br.get("consumed_symbols"), list)

    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    r_probe = client.post("/api/internal/goodluck/quant/data-sources/probe", headers=_headers(role="admin"))
    assert r_probe.status_code == 200
    assert r_probe.json()["summary"]["total_sources"] >= 13

    r_bad = client.post(
        "/api/internal/goodluck/quant/rules",
        headers=_headers(role="admin"),
        json={
            "name": "非法数据源拒绝测试",
            "symbols": ["160723"],
            "buy_rule": {"conditions": [{"type": "macd_signal", "signal": "red"}]},
            "data_sources": [{"id": "bogus_source"}],
        },
    )
    assert r_bad.status_code == 422

    # Sanity check direct helper report during offline stale state
    rep_direct = build_quant_data_sources_report(
        rstore,
        qstore,
        auto_trade_engine=engine,
        now_dt=datetime(2026, 11, 26, 10, 15, tzinfo=SHANGHAI_TZ),
    )
    assert rep_direct["summary"]["ready_rules_count"] >= 1
    assert any(s["id"] == "qdii_premium_panel_feed" for s in rep_direct["sources"])
    assert any(s["id"] == "ashare_orderbook_l1" for s in rep_direct["sources"])
    assert any(s["id"] == "cme_energy_macro_futures" for s in rep_direct["sources"])

    qstore.close()
    astore.close()
    rstore.close()


def test_501018_multi_oil_dedup_usage_tags_and_live_open_enrichment(tmp_path: Path) -> None:
    # Ensure catalog entries remain strictly deduplicated with unique 501018 usage tags
    cat_ids = [item["id"] for item in QUANT_DATA_SOURCE_CATALOG]
    assert len(cat_ids) == len(set(cat_ids)) == 13
    cat_by_id = {item["id"]: item for item in QUANT_DATA_SOURCE_CATALOG}
    for sid in (
        "ashare_intraday_1m",
        "ice_brent_1m",
        "ashare_peer_lof_quote",
        "us_oil_etf_uso_bno_dbo",
        "us_ep_xop_crack_macro",
        "cme_energy_macro_futures",
        "fx_usdcny_usdcnh",
        "eastmoney_fund_nav",
        "qdii_premium_panel_feed",
    ):
        labels = [t["label"] for t in cat_by_id[sid]["usage_tags"]]
        assert len(labels) == len(set(labels))
        if sid != "fx_usdcny_usdcnh":
            assert any("501018" in lbl for lbl in labels), f"missing 501018 tag on {sid}"

    rule_501018 = {
        "id": "ai-501018-v9-plus-aggressive",
        "name": "501018专享 · V9+ 南方原油多源油价+暴跌逆势增强量化",
        "source": "ai",
        "symbols": ["501018", "BRN0Y"],
        "target_symbol": "501018",
        "position_sizing": {
            "mode": "cash_pool_rolling",
            "engine_profile": "v9_plus_aggressive",
            "cross_market_anchor": "brent",
            "v9_tuning": {
                "multi_oil_plunge_mode": True,
                "peer_symbol": "160723",
                "macd_price_anchor": 2.2,
                "plunge_dd15_pct": -11.0,
                "high_oil_bull_cap_pct": 87.0,
            },
        },
    }
    ds_list = derive_rule_data_sources(rule_501018)
    assert [d["id"] for d in ds_list] == [
        "ashare_intraday_1m",
        "ashare_orderbook_l1",
        "ice_brent_1m",
        "ashare_peer_lof_quote",
        "us_oil_etf_uso_bno_dbo",
        "us_ep_xop_crack_macro",
        "cme_energy_macro_futures",
        "fx_usdcny_usdcnh",
        "eastmoney_fund_nav",
    ]
    by_id = {d["id"]: d for d in ds_list}
    assert by_id["us_oil_etf_uso_bno_dbo"]["role"] == "anchor"
    assert by_id["us_oil_etf_uso_bno_dbo"]["symbols"] == ["USO", "BNO"]
    assert by_id["cme_energy_macro_futures"]["role"] == "anchor"
    assert by_id["cme_energy_macro_futures"]["symbols"] == ["CL"]
    assert by_id["us_ep_xop_crack_macro"]["symbols"] == ["XOP"]
    assert by_id["ashare_peer_lof_quote"]["symbols"] == ["160723"]

    r_tags = [t["label"] for t in derive_rule_usage_tags(rule_501018)]
    assert any("55%USO+45%BNO" in lbl for lbl in r_tags)
    assert any("dd15" in lbl for lbl in r_tags)
    assert any("160723" in lbl for lbl in r_tags)

    db_path = tmp_path / "m501018.db"
    rstore = SQLiteResearchStore(db_path)
    qstore = SQLiteQuantRuleStore(db_path)
    V9MacroDataStore(rstore, allow_network=False)
    now_ts = time.time()
    ts_prev = int(datetime(2026, 11, 24, 6, 0, tzinfo=SHANGHAI_TZ).timestamp())
    ts_t2 = int(datetime(2026, 11, 25, 6, 0, tzinfo=SHANGHAI_TZ).timestamp())
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("nav_501018", json.dumps({"20261124": 1.350}), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            (
                "uso_daily_2y",
                json.dumps([["20261123", ts_prev, 70.0], ["20261124", ts_t2, 72.0]]),
                now_ts,
            ),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            (
                "bno_daily_2y",
                json.dumps([["20261123", ts_prev, 30.0], ["20261124", ts_t2, 31.0]]),
                now_ts,
            ),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            (
                "cl_daily_2y",
                json.dumps([["20261124", ts_prev, 68.0], ["20261125", ts_t2, 70.0]]),
                now_ts,
            ),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            (
                "xop_daily_2y",
                json.dumps([["20261124", ts_prev, 130.0], ["20261125", ts_t2, 132.0]]),
                now_ts,
            ),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcny_series", json.dumps({"ts": [ts_prev], "px": [7.20]}), now_ts),
        )
        rstore._connection.execute(
            """INSERT OR REPLACE INTO research_days(
                   symbol, trade_date, name, market, source, pre_close, quote_price, collected_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            ("BRN0Y", "20261125", "BRN0Y", "ICE", "TEST", "72.0", "72.0", "2026-11-25T23:59:00+08:00"),
        )
        rstore._connection.execute(
            """INSERT OR REPLACE INTO research_points(
                   symbol, trade_date, minute, price, average_price, volume, captured_at
               ) VALUES(?,?,?,?,?,?,?)""",
            ("BRN0Y", "20261125", "23:59", "72.0", "72.0", "1", "2026-11-25T23:59:00+08:00"),
        )

    qstore.upsert_rule(
        {
            **rule_501018,
            "buy_rule": {"conditions": [{"type": "brent_spread_pct", "direction": "lte", "value": -0.2}]},
            "sell_rule": {"conditions": [{"type": "brent_spread_pct", "direction": "gte", "value": 0.4}]},
        }
    )
    rep = build_quant_data_sources_report(
        rstore,
        qstore,
        now_dt=datetime(2026, 11, 26, 20, 0, tzinfo=SHANGHAI_TZ),
    )
    rule_row = next(r for r in rep["rules"] if r["id"] == "ai-501018-v9-plus-aggressive")
    assert rule_row["short_label"] == "501018·V9+南方多油"
    assert rule_row["cross_market_anchor"] == "brent_multi_oil_plunge"
    assert any("55%USO+45%BNO" in t["label"] for t in rule_row["usage_tags"])
    uso_card = next(s for s in rep["sources"] if s["id"] == "us_oil_etf_uso_bno_dbo")
    assert any(b["rule_id"] == "ai-501018-v9-plus-aggressive" and b["role"] == "anchor" for b in uso_card["bound_rules"])

    # Live dual spot enrichment fills T-1 USO/BNO & 09:30 peer_160723 snapshot without touching historical no-live path
    days_hist = [
        {
            "td": "20261126",
            "prev_td": "20261125",
            "td_m2": "20261124",
            "bars": [{"time": "09:31", "price": 1.380, "brn": 72.5, "fx": 7.20, "prem": 0.0, "lof_chg": 0.8}],
        }
    ]
    days_live = [
        {
            "td": "20261126",
            "prev_td": "20261125",
            "td_m2": "20261124",
            "bars": [{"time": "09:31", "price": 1.380, "brn": 72.5, "fx": 7.20, "prem": 0.0, "lof_chg": 0.8}],
        }
    ]
    _enrich_v9_multi_oil_days(
        rstore,
        days_hist,
        symbol="501018",
        v9_tuning=rule_501018["position_sizing"]["v9_tuning"],
        allow_network=False,
    )
    assert days_hist[0]["bars"][0]["peer_chg"] is None
    assert days_hist[0]["etf_ov"] > 0.0

    _enrich_v9_multi_oil_days(
        rstore,
        days_live,
        symbol="501018",
        v9_tuning=rule_501018["position_sizing"]["v9_tuning"],
        allow_network=False,
        live_dual_spot={
            "uso_reg_date": "20261125",
            "uso_close": 75.6,
            "bno_reg_date": "20261125",
            "bno_close": 32.55,
            "peer_160723": {"pre_close": 2.0, "price": 2.04, "date": "20261126", "time": "09:31"},
        },
    )
    assert days_live[0]["etf_ov"] > days_hist[0]["etf_ov"]
    assert days_live[0]["bars"][0]["peer_chg"] == pytest.approx(2.0, abs=1e-4)
    assert days_live[0]["bars"][0]["peer_gap"] == pytest.approx(-1.2, abs=1e-4)

    qstore.close()
    rstore.close()


def test_160723_fof_crude_dedup_usage_tags_telemetry_and_health_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cat_ids = [item["id"] for item in QUANT_DATA_SOURCE_CATALOG]
    assert len(cat_ids) == len(set(cat_ids)) == 13
    cat_by_id = {item["id"]: item for item in QUANT_DATA_SOURCE_CATALOG}
    for sid in (
        "ashare_intraday_1m",
        "ashare_orderbook_l1",
        "ashare_l2_fund_flow",
        "ice_brent_1m",
        "ashare_peer_lof_quote",
        "ine_sc0_futures",
        "us_oil_etf_uso_bno_dbo",
        "cme_energy_macro_futures",
        "fx_usdcny_usdcnh",
        "eastmoney_fund_nav",
        "qdii_premium_panel_feed",
    ):
        labels = [t["label"] for t in cat_by_id[sid]["usage_tags"]]
        assert len(labels) == len(set(labels)), f"duplicate usage_tags on {sid}"
        assert any("160723" in lbl for lbl in labels), f"missing 160723 tag on {sid}"

    rule_160723 = {
        "id": "ai-160723-v9-plus-aggressive",
        "name": "V9+ 进取版 · FOF三油+汇率+SC0+裂解跨市场量化",
        "source": "ai",
        "symbols": ["160723", "BRN0Y"],
        "target_symbol": "160723",
        "position_sizing": {
            "mode": "cash_pool_rolling",
            "engine_profile": "v9_plus_aggressive",
            "cross_market_anchor": "fof_crude_core",
            "min_retained_shares": 100,
        },
    }
    ds_list = derive_rule_data_sources(rule_160723)
    assert [d["id"] for d in ds_list] == [
        "ashare_intraday_1m",
        "ashare_orderbook_l1",
        "ice_brent_1m",
        "ashare_peer_lof_quote",
        "ine_sc0_futures",
        "us_oil_etf_uso_bno_dbo",
        "cme_energy_macro_futures",
        "fx_usdcny_usdcnh",
        "eastmoney_fund_nav",
    ]
    by_id = {d["id"]: d for d in ds_list}
    assert by_id["us_oil_etf_uso_bno_dbo"]["symbols"] == ["USO", "BNO", "DBO"]
    assert by_id["cme_energy_macro_futures"]["symbols"] == ["CL", "HO"]
    assert by_id["ashare_peer_lof_quote"]["symbols"] == ["501018", "161129"]
    assert by_id["fx_usdcny_usdcnh"]["symbols"] == ["USDCNY", "USDCNH"]
    assert "100份底仓" in by_id["ashare_orderbook_l1"]["note"]
    assert "35%USO+35%BNO+30%DBO" in by_id["us_oil_etf_uso_bno_dbo"]["note"]

    r_tags = [t["label"] for t in derive_rule_usage_tags(rule_160723)]
    assert "35%USO+35%BNO+30%DBO三油锚" in r_tags
    assert "10合1新浪8s秒级补尾" in r_tags
    assert "保留100份底仓锁" in r_tags

    # auto_derived=True entries refresh canonical notes, while auto_derived=False custom notes are preserved
    refreshed = normalize_and_validate_rule_data_sources(
        [
            {"id": "us_oil_etf_uso_bno_dbo", "auto_derived": True, "note": "legacy stale note"},
            {"id": "cme_energy_macro_futures", "auto_derived": False, "note": "custom manual note"},
        ],
        rule_160723,
    )
    ref_by_id = {d["id"]: d for d in refreshed}
    assert ref_by_id["us_oil_etf_uso_bno_dbo"]["note"] == by_id["us_oil_etf_uso_bno_dbo"]["note"]
    assert ref_by_id["cme_energy_macro_futures"]["note"] == "custom manual note"

    # fetch_live_fof_crude_spot records 10-field telemetry and satisfies CNH / CL+HO / BRN / SC0 health checks
    _MEMORY_MACRO_CACHE.pop(("global", "live_spot_fof_crude_bundle"), None)
    db_path = tmp_path / "fof160723.db"
    rstore = SQLiteResearchStore(db_path)
    macro = V9MacroDataStore(rstore, allow_network=True)
    now_ts = time.time()
    ts_prev = int(datetime(2026, 11, 24, 6, 0, tzinfo=SHANGHAI_TZ).timestamp())
    ts_t2 = int(datetime(2026, 11, 25, 6, 0, tzinfo=SHANGHAI_TZ).timestamp())
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("oil_daily_2y", json.dumps([["20261124", ts_prev, 71.0], ["20261125", ts_t2, 72.0]]), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("cl_daily_2y", json.dumps([["20261124", ts_prev, 68.0], ["20261125", ts_t2, 69.5]]), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("ho_daily_2y", json.dumps([["20261124", ts_prev, 2.15], ["20261125", ts_t2, 2.21]]), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcny_series", json.dumps({"ts": [ts_prev], "px": [7.2050]}), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcnh_series", json.dumps({"ts": [ts_t2], "px": [7.2180]}), now_ts),
        )

    sina_bundle = (
        'var hq_str_fx_susdcnh="09:35:00,7.2185,7.2180,7.2190,1,7.2100,7.2200,7.2150,7.2185,离岸人民币,0.01,0.1,0.001,x,x,x,x,2026-11-26";\n'
        'var hq_str_USDCNY="09:35:00,7.2090,7.2080,7.2100,1,7.2000,7.2110,7.2060,7.2090,在岸人民币,0.01,0.1,0.001,x,x,x,x,2026-11-26";\n'
        'var hq_str_hf_OIL="72.35,0.42,72.30,72.40,72.80,71.90,09:35:00,72.00,72.10,1000,0,0,2026-11-26,布伦特原油";\n'
        'var hq_str_hf_CL="69.85,0.35,69.80,69.90,70.20,69.10,09:35:00,69.50,69.60,1000,0,0,2026-11-26,WTI原油";\n'
        'var hq_str_nf_SC0="原油连续,093500,535.0,540.0,532.0,534.0,536.5,536.8,536.6,535.5,534.0,10,12,1000,2000,上期所,原油,2026-11-26";\n'
        'var hq_str_gb_uso="USO,74.20,1.2,2026-11-25 16:00:00,0.88,73.50,74.50,73.10,80,60,1000,500,74.20";\n'
        'var hq_str_gb_bno="BNO,31.40,1.1,2026-11-25 16:00:00,0.34,31.10,31.60,30.90,35,25,1000,500,31.40";\n'
        'var hq_str_gb_dbo="DBO,14.80,0.9,2026-11-25 16:00:00,0.13,14.65,14.90,14.55,16,12,1000,500,14.80";\n'
        'var hq_str_sh501018="南方原油LOF,1.380,1.375,1.392,1.395,1.372,1.391,1.392,10000,1392000,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,2026-11-26,09:35:00";\n'
        'var hq_str_sz161129="易方达原油LOF,1.210,1.205,1.218,1.220,1.202,1.217,1.218,8000,974400,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,2026-11-26,09:35:00";\n'
    )
    monkeypatch.setattr(macro, "fetch_text", lambda url, **kwargs: sina_bundle if "hq.sinajs.cn/list=USDCNY" in url else "")
    monkeypatch.setattr(macro, "fetch_live_xop_spot", lambda: None)
    monkeypatch.setattr(macro, "fetch_live_dual_oil_spot", lambda: None)
    monkeypatch.setattr(macro, "fetch_live_spot", lambda: (None, None))

    spot = fetch_live_fof_crude_spot(macro)
    assert spot is not None and spot["fx_cnh_px"] == pytest.approx(7.2185) and spot["fx_cny_px"] == pytest.approx(7.2090)
    tel = get_live_spot_telemetry().get("live_spot_fof_crude_bundle") or {}
    assert tel.get("non_null_fields") == 10 and tel.get("total_fields") == 10

    now_open = datetime(2026, 11, 26, 10, 15, tzinfo=SHANGHAI_TZ)
    fx_st, fx_msg = _check_macro_source_health(
        macro, "fx_usdcny_usdcnh", symbol="160723", today_str="20261126", now_dt=now_open, is_trading_day=True, local_points_check=["USDCNY", "USDCNH"], research_store=rstore
    )
    assert fx_st == "ok" and "USD/CNH+CNY" in fx_msg

    cme_st, _ = _check_macro_source_health(
        macro, "cme_energy_macro_futures", symbol="160723", today_str="20261126", now_dt=now_open, is_trading_day=True, local_points_check=["CL", "HO"], research_store=rstore
    )
    assert cme_st == "ok"

    brn_st, brn_msg = _check_macro_source_health(
        macro, "ice_brent_1m", symbol="160723", today_str="20261126", now_dt=now_open, is_trading_day=True, research_store=rstore, rule_profile="v9_plus_aggressive"
    )
    assert brn_st == "fallback" and "hf_OIL" in brn_msg

    sc_st, sc_msg = _check_macro_source_health(
        macro, "ine_sc0_futures", symbol="160723", today_str="20261126", now_dt=now_open, is_trading_day=True, research_store=rstore
    )
    assert sc_st == "ok" and "536.6" in sc_msg

    _MEMORY_MACRO_CACHE.pop(("global", "live_spot_fof_crude_bundle"), None)
    rstore.close()


def test_t0_protective_exit_escape_hatch_under_auxiliary_data_outage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    db_path = tmp_path / "escape_hatch.db"
    rstore = SQLiteResearchStore(db_path)
    qstore = SQLiteQuantRuleStore(db_path)
    astore = SQLiteAutoTradeStore(db_path)

    V9MacroDataStore(rstore, allow_network=False)
    now_ts = time.time()
    oil_rows = [
        ["20251101", int(datetime(2025, 11, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 60.0],
        ["20261125", int(datetime(2026, 11, 26, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 90.0],
    ]
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("nav_160723", json.dumps({"20261124": 2.000, "20261125": 2.000}), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("oil_daily_2y", json.dumps(oil_rows), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcny_series", json.dumps({"ts": [int(datetime(2026, 11, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [6.70]}), now_ts),
        )

    def _seed_day(sym: str, td: str, pre: float, bars: list[tuple[str, float]], collected_at: str, market: str = "36") -> None:
        with rstore._lock, rstore._connection:
            rstore._connection.execute(
                """INSERT OR REPLACE INTO research_days(
                       symbol, trade_date, name, market, source, pre_close, quote_price, collected_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (sym, td, sym, market, "TEST", f"{pre:.3f}", f"{bars[-1][1]:.3f}", collected_at),
            )
            rstore._connection.executemany(
                """INSERT OR REPLACE INTO research_points(
                       symbol, trade_date, minute, price, average_price, volume, captured_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                [(sym, td, hhmm, f"{px:.3f}", f"{px:.3f}", "10000", collected_at) for hhmm, px in bars],
            )

    def _hm_bars(count: int, px_fn) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        for idx in range(count):
            total_m = 9 * 60 + 31 + idx
            if total_m >= 11 * 60 + 30:
                total_m += 90  # jump lunch break cleanly
            out.append((f"{total_m // 60:02d}:{total_m % 60:02d}", float(px_fn(idx))))
        return out

    qstore.upsert_rule(
        {
            "id": "ai-160723-v9-escape-test",
            "name": "V9+ T+0 缺数逃生通道测试",
            "source": "ai",
            "symbols": ["160723", "BRN0Y"],
            "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.00001},
            "buy_rule": {"title": "正T买", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "lte", "value": -0.25}]},
            "sell_rule": {"title": "反T卖", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "gte", "value": 0.42}]},
            "position_sizing": {
                "mode": "cash_pool_rolling",
                "engine_profile": "v9_plus_aggressive",
                "default_weight_pct": 80.0,
                "max_position_pct": 100.0,
                "max_daily_entries": 8,
                "cooldown_minutes": 4,
                "close_flatten_time": "14:53",
            },
        }
    )
    astore.update_config(
        {
            "enabled": True,
            "signal_source": "quant_rule",
            "quant_rule_id": "ai-160723-v9-escape-test",
            "execution_mode": "semi_auto",
            "sizing_mode": "model_auto",
            "pricing_mode": "fixed_ticks",
            "slippage_ticks": 0,
            "symbols": ["160723"],
            "daily_max_buy_amount": 50000.0,
            "block_non_continuous_session": True,
        }
    )

    clock_dt = {"dt": datetime(2026, 11, 26, 10, 5, tzinfo=SHANGHAI_TZ)}
    engine = AutoTradeEngine(
        auto_store=astore,
        research_store=rstore,
        broker=None,
        trade_executor=None,
        symbol_lookup=lambda s: CATALOG.get(s),
        is_trading_day=lambda _d: True,
        now_fn=lambda: clock_dt["dt"],
        quant_rule_store=qstore,
    )

    # Scenario A: 160723 1m is fresh at 10:05, BRN0Y is stale at 09:40 -> new buy MUST be blocked, escape armed
    _seed_day("BRN0Y", "20261125", 70.0, [("09:30", 70.0), ("15:00", 70.0)], "2026-11-25T15:00:00+08:00", market="ICE")
    _seed_day("160723", "20261125", 2.000, [("09:31", 2.000), ("15:00", 2.000)], "2026-11-25T15:00:00+08:00")
    _seed_day(
        "BRN0Y",
        "20261126",
        70.0,
        [("09:30", 70.0), ("09:39", 72.5), ("09:40", 72.5)],
        "2026-11-26T09:40:00+08:00",
        market="ICE",
    )
    fresh_drop_bars = _hm_bars(35, lambda i: 2.000 if i < 25 else 2.000 - (i - 24) * 0.003)
    _seed_day("160723", "20261126", 2.000, fresh_drop_bars, "2026-11-26T10:05:00+08:00")

    poll_a = asyncio.run(engine.poll_once())
    assert poll_a["triggered"] == 0
    assert astore.list_pending_signals() == []
    live_a = engine.evaluate_live_snapshot("160723")
    assert live_a["data_guard"]["blocked"] is True
    assert "ice_brent_1m" in live_a["data_guard"]["blocking_sources"]
    assert "ashare_intraday_1m" not in live_a["data_guard"]["blocking_sources"]
    assert live_a["data_guard"]["primary_intraday_ok"] is True
    assert live_a["data_guard"]["allow_protective_exit"] is True
    assert live_a["buy"]["matched"] is False
    assert live_a["sell"]["matched"] is False

    # Scenario B: Existing open morning T+0 buy leg + fresh A-share plunge -> hard stop-loss sell allowed!
    morning_buy = astore.claim_and_insert_signal(
        signal_key="160723:20261126:09:50:v9-scalp:buy",
        trade_date="20261126",
        symbol="160723",
        name="嘉实原油LOF",
        side="buy",
        signal_source="quant_rule",
        rule_id="ai-160723-v9-escape-test",
        rule_title="V9+ T+0 日内做T买入",
        rule_summary="buy",
        minute="09:50",
        signal_price="2.000",
        limit_price="2.000",
        precision=3,
        quantity=2000,
        execution_mode="semi_auto",
        is_simulation=False,
        status="FILLED_OR_CLOSED",
        now_str="2026-11-26T09:50:00+08:00",
    )
    assert morning_buy is not None

    plunge_bars = _hm_bars(50, lambda i: 2.000 if i < 40 else round(2.000 - (i - 39) * 0.002, 3))
    assert plunge_bars[-1] == ("10:20", 1.980) or plunge_bars[-1][1] <= 1.982
    _seed_day("160723", "20261126", 2.000, plunge_bars, "2026-11-26T10:20:00+08:00")
    clock_dt["dt"] = datetime(2026, 11, 26, 10, 20, tzinfo=SHANGHAI_TZ)

    live_b = engine.evaluate_live_snapshot("160723")
    assert live_b["data_guard"]["allow_protective_exit"] is True
    assert live_b["buy"]["matched"] is False
    assert live_b["sell"]["matched"] is True
    assert live_b["sell"]["conditions"][0]["id"] == "t0-protective-exit"

    poll_b = asyncio.run(engine.poll_once())
    assert poll_b["triggered"] == 1
    pend_b = astore.list_pending_signals()
    assert len(pend_b) == 1
    assert pend_b[0]["side"] == "sell"
    assert pend_b[0]["quantity"] == 1900  # 2000 - MIN_RETAINED_SHARES(100)
    assert ":v9-exit:sell" in pend_b[0]["signal_key"]

    # Scenario C: 14:53 protective T+0 flatten sells unclosed intraday buy leg while BRN0Y is stale
    with astore._lock, astore._connection:
        astore._connection.execute("DELETE FROM auto_trade_signals")
    _seed_day("BRN0Y", "20261126", 70.0, [("09:30", 70.0), ("15:00", 70.0)], "2026-11-26T15:00:00+08:00", market="ICE")
    _seed_day("160723", "20261126", 2.000, [("09:31", 2.000), ("15:00", 2.000)], "2026-11-26T15:00:00+08:00")
    _seed_day(
        "BRN0Y",
        "20261127",
        70.0,
        [("09:30", 70.0), ("14:09", 71.0), ("14:10", 71.0)],
        "2026-11-27T14:10:00+08:00",
        market="ICE",
    )
    calm_bars_1453 = _hm_bars(233, lambda _i: 2.004)
    assert calm_bars_1453[-1][0] == "14:53"
    _seed_day("160723", "20261127", 2.000, calm_bars_1453, "2026-11-27T14:53:00+08:00")
    day2_buy = astore.claim_and_insert_signal(
        signal_key="160723:20261127:10:10:v9-entry:buy",
        trade_date="20261127",
        symbol="160723",
        name="嘉实原油LOF",
        side="buy",
        signal_source="quant_rule",
        rule_id="ai-160723-v9-escape-test",
        rule_title="V9+ 正T买入",
        rule_summary="buy",
        minute="10:10",
        signal_price="2.000",
        limit_price="2.000",
        precision=3,
        quantity=1500,
        execution_mode="semi_auto",
        is_simulation=False,
        status="FILLED_OR_CLOSED",
        now_str="2026-11-27T10:10:00+08:00",
    )
    assert day2_buy is not None
    clock_dt["dt"] = datetime(2026, 11, 27, 14, 53, tzinfo=SHANGHAI_TZ)

    poll_c = asyncio.run(engine.poll_once())
    assert poll_c["triggered"] == 1
    pend_c = astore.list_pending_signals()
    assert len(pend_c) == 1
    assert pend_c[0]["side"] == "sell"
    assert pend_c[0]["quantity"] == 1400  # 1500 - MIN_RETAINED_SHARES(100)
    assert ":v9-close:sell" in pend_c[0]["signal_key"]

    # Scenario D: Primary ashare_intraday_1m ALSO stale -> full block (allow_protective_exit=False)
    with astore._lock, astore._connection:
        astore._connection.execute("DELETE FROM auto_trade_signals WHERE signal_key LIKE '%:v9-close:%'")
        astore._connection.execute("DELETE FROM research_points WHERE symbol='160723' AND trade_date='20261127'")
        astore._connection.execute("DELETE FROM research_days WHERE symbol='160723' AND trade_date='20261127'")
    _seed_day(
        "160723",
        "20261127",
        2.000,
        [("09:31", 2.000), ("14:09", 2.004), ("14:10", 2.004)],
        "2026-11-27T14:10:00+08:00",
    )
    poll_d = asyncio.run(engine.poll_once())
    assert poll_d["triggered"] == 0
    assert astore.list_pending_signals() == []
    live_d = engine.evaluate_live_snapshot("160723")
    assert live_d["data_guard"]["blocked"] is True
    assert live_d["data_guard"]["primary_intraday_ok"] is False
    assert live_d["data_guard"]["allow_protective_exit"] is False
    assert live_d["sell"]["matched"] is False

    qstore.close()
    astore.close()
    rstore.close()


def test_sp_oilgas_etf_513350_data_sources_short_label_and_probe(tmp_path: Path, monkeypatch) -> None:
    from level2_service.quant_data_sources import (
        _check_macro_source_health,
        _format_rule_short_label,
        derive_rule_data_sources,
        derive_rule_usage_tags,
    )
    from level2_service.v9_plus_engine import V9MacroDataStore, _MEMORY_MACRO_CACHE
    from level2_service.v9_sp_oilgas_etf import fetch_live_sp_oilgas_etf_spot

    rule_513 = {
        "id": "ai-513350-v9-sp-oilgas-etf",
        "name": "513350专享 · V9+ 标普油气ETF全复制XOP+T+0双向量化",
        "symbols": ["513350", "BRN0Y"],
        "target_symbol": "513350",
        "position_sizing": {
            "mode": "base_t0_close",
            "engine_profile": "v9_plus_aggressive",
            "cross_market_anchor": "sp_oilgas_etf",
            "min_retained_shares": 100,
            "allow_same_day_turnaround": True,
        },
        "buy_conditions": [],
        "sell_conditions": [],
    }
    derived = derive_rule_data_sources(rule_513)
    ids = {item["id"] for item in derived}
    assert ids == {
        "ashare_intraday_1m",
        "ashare_orderbook_l1",
        "ice_brent_1m",
        "fx_usdcny_usdcnh",
        "eastmoney_fund_nav",
        "us_ep_xop_crack_macro",
        "cme_energy_macro_futures",
        "ine_sc0_futures",
        "ashare_peer_lof_quote",
    }
    assert "ashare_etf_redemption" not in ids
    assert "ashare_l2_fund_flow" not in ids
    assert "us_oil_etf_uso_bno_dbo" not in ids

    assert _format_rule_short_label("ai-513350-v9-sp-oilgas-etf", rule_513["name"], "513350") == "513350·V9+标普油气T0"
    tag_labels = {t["label"] for t in derive_rule_usage_tags(rule_513)}
    assert "XOP全复制(0.985)+5d中位配额脱敏" in tag_labels
    assert "保留100份底仓锁" in tag_labels

    rstore = SQLiteResearchStore(tmp_path / "research_513.db")
    macro = V9MacroDataStore(rstore, allow_network=True)
    _MEMORY_MACRO_CACHE.clear()
    now_ts = float(datetime(2026, 11, 26, 10, 15, tzinfo=SHANGHAI_TZ).timestamp())
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("nav_513350", json.dumps({"20261124": 2.15}), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("oil_daily_2y", json.dumps([["20261125", int(now_ts), 91.2]]), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcnh_series", json.dumps({"ts": [int(now_ts)], "px": [7.218]}), now_ts),
        )
    sina_13 = (
        'var hq_str_USDCNY="10:15:00,7.2080,7.2070,7.2090,1,7.2000,7.2100,7.2050,7.2085,x,0,0,0,x,x,x,x,2026-11-26";\n'
        'var hq_str_fx_susdcnh="10:15:00,7.2190,7.2180,7.2200,1,7.2100,7.2210,7.2150,7.2195,x,0,0,0,x,x,x,x,2026-11-26";\n'
        'var hq_str_hf_OIL="73.20,0.45,73.15,73.25,73.50,72.60,10:15:00,72.80,72.90,1000,0,0,2026-11-26,BRN";\n'
        'var hq_str_hf_CL="69.90,0.40,69.85,69.95,70.10,69.20,10:15:00,69.50,69.60,1000,0,0,2026-11-26,CL";\n'
        'var hq_str_hf_HO="2.24,0.35,2.23,2.25,2.28,2.20,10:15:00,2.21,2.22,500,0,0,2026-11-26,HO";\n'
        'var hq_str_hf_NG="3.38,0.60,3.37,3.39,3.42,3.31,10:15:00,3.33,3.35,500,0,0,2026-11-26,NG";\n'
        'var hq_str_hf_ES="5985.5,0.20,5984.0,5986.0,5995.0,5970.0,10:15:00,5973.0,5975.0,500,0,0,2026-11-26,ES";\n'
        'var hq_str_nf_SC0="原油,101500,536.0,541.0,533.0,535.0,537.5,537.8,537.6,536.5,535.0,10,12,1000,2000,INE,SC,2026-11-26";\n'
        'var hq_str_gb_xop="XOP,138.40,1.45,2026-11-25 16:00:00,1.98,136.80,139.10,136.00,145,110,1000,500,138.40";\n'
        'var hq_str_gb_iwm="IWM,239.50,0.85,2026-11-25 16:00:00,2.02,238.00,240.10,237.20,245,190,1000,500,239.50";\n'
        'var hq_str_gb_xle="XLE,92.10,1.10,2026-11-25 16:00:00,1.00,91.40,92.50,90.90,98,80,1000,500,92.10";\n'
        'var hq_str_gb_spy="SPY,598.20,0.55,2026-11-25 16:00:00,3.27,596.00,599.00,594.50,605,500,1000,500,598.20";\n'
        'var hq_str_sz162411="华宝油气LOF,1.062,1.055,1.071,1.075,1.052,1.070,1.071,10000,1071000,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,2026-11-26,10:15:00";\n'
    )
    monkeypatch.setattr(macro, "fetch_text", lambda url, **_kw: sina_13 if "sz162411" in url else "")
    monkeypatch.setattr(macro, "fetch_live_xop_spot", lambda: None)
    monkeypatch.setattr(macro, "fetch_live_dual_oil_spot", lambda: None)
    monkeypatch.setattr(macro, "fetch_live_spot", lambda: (None, None))
    assert fetch_live_sp_oilgas_etf_spot(macro) is not None
    now_open = datetime(2026, 11, 26, 10, 15, tzinfo=SHANGHAI_TZ)
    peer_st, peer_msg = _check_macro_source_health(
        macro,
        "ashare_peer_lof_quote",
        symbol="513350",
        today_str="20261126",
        now_dt=now_open,
        is_trading_day=True,
        local_points_check=["162411"],
        research_store=rstore,
    )
    assert peer_st == "fallback" and "162411" in peer_msg
    _MEMORY_MACRO_CACHE.clear()
    rstore.close()
