from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from level2_service.auto_trade import SHANGHAI_TZ
from level2_service.portfolio_allocator import (
    BUILTIN_PORTFOLIO_POLICIES,
    DEFAULT_POOL_POLICY,
    CausalStats,
    RouteDPlusAllocator,
    SQLitePortfolioPolicyStore,
    bayesian_prior_snapshot,
    compute_ewma_vols_and_corrs,
    get_portfolio_policy_schema,
    normalize_pool_policy,
)
from level2_service.quant_rules import SQLiteQuantRuleStore
from level2_service.research_store import SQLiteResearchStore


def _make_snap(*, m_rec: float = 1.0, sentry: float = 0.0, k_ovs: float = 0.25, p_bkt: float = 0.58, k_bkt: float = 0.30) -> dict:
    return {
        "m_rec": m_rec,
        "sentry": sentry,
        "k_ov_strong": k_ovs,
        "k_ov_mod": k_ovs,
        "intra": {
            "buy_tier_a": (p_bkt, k_bkt),
            "buy_tier_b": (p_bkt, k_bkt),
            "buy_tier_c": (p_bkt, k_bkt),
            "buy_scalp": (p_bkt, k_bkt),
        },
        "event_count": 20,
    }


def test_normalize_pool_policy_defaults_and_bounds() -> None:
    pol = normalize_pool_policy(None)
    assert pol["rho_thresh"] == pytest.approx(0.62)
    assert pol["rho_slope"] == pytest.approx(0.26)
    assert pol["intra_rho_slope"] == pytest.approx(0.24)
    assert pol["sigma_cap_pct"] == pytest.approx(4.20)
    assert pol["solo_equity_coef"] == pytest.approx(0.64)
    assert pol["solo_equity_cap"] == pytest.approx(0.85)
    assert pol["solo_crude_coef"] == pytest.approx(0.48)
    assert pol["solo_crude_cap"] == pytest.approx(0.68)
    assert pol["equity_solo_anchors"] == ["xop_us_ep"]
    assert DEFAULT_POOL_POLICY["sigma_cap_pct"] == pytest.approx(4.20)

    custom = normalize_pool_policy(
        {
            "total_pool_cap": 140000,
            "sigma_cap_pct": None,
            "equity_solo_anchors": [],
            "solo_equity_cap": 1.5,
            "lookback_days": 3,
        }
    )
    assert custom["total_pool_cap"] == pytest.approx(140000.0)
    assert custom["sigma_cap_pct"] is None
    assert custom["equity_solo_anchors"] == []
    assert custom["solo_equity_cap"] == pytest.approx(1.0)
    assert custom["lookback_days"] == 5

    disabled_sig = normalize_pool_policy({"sigma_cap_pct": 0})
    assert disabled_sig["sigma_cap_pct"] is None


def test_solo_heterogeneous_caps_xop_vs_crude_vs_sp_oilgas() -> None:
    alloc = RouteDPlusAllocator()
    pol = normalize_pool_policy({"sigma_cap_pct": None})
    syms = ["160723", "161129", "501018", "162411", "513350"]
    anchors = {
        "160723": "fof_t0_multisource",
        "161129": "uso_bno_sc_peer",
        "501018": "brent",
        "162411": "xop_us_ep",
        "513350": "sp_oilgas_etf",
    }
    ctx = {
        "snaps": {s: _make_snap(m_rec=1.45, k_ovs=0.45) for s in syms},
        "vols": {s: 2.0 for s in syms},
        "corrs": {(s1, s2): (1.0 if s1 == s2 else 0.55) for s1 in syms for s2 in syms},
    }

    res_xop = alloc.compute_overnight_weights(
        symbols=syms,
        raw_weights={**{s: 0.0 for s in syms}, "162411": 1.0},
        anchors=anchors,
        causal_ctx=ctx,
        policy=pol,
    )
    assert res_xop["is_solo"] is True
    assert res_xop["caps"]["162411"] == pytest.approx(0.85)
    assert res_xop["alloc_weights"]["162411"] == pytest.approx(0.85)

    res_crude = alloc.compute_overnight_weights(
        symbols=syms,
        raw_weights={**{s: 0.0 for s in syms}, "160723": 1.0},
        anchors=anchors,
        causal_ctx=ctx,
        policy=pol,
    )
    assert res_crude["is_solo"] is True
    assert res_crude["caps"]["160723"] == pytest.approx(0.68)
    assert res_crude["alloc_weights"]["160723"] == pytest.approx(0.68)

    # 513350 keeps (0.48, 68%) solo profile while only xop_us_ep widens to 85%
    res_sp = alloc.compute_overnight_weights(
        symbols=syms,
        raw_weights={**{s: 0.0 for s in syms}, "513350": 1.0},
        anchors=anchors,
        causal_ctx=ctx,
        policy=pol,
    )
    assert res_sp["is_solo"] is True
    assert res_sp["caps"]["513350"] == pytest.approx(0.68)
    assert res_sp["alloc_weights"]["513350"] == pytest.approx(0.68)


def test_multi_symbol_high_vs_low_correlation_waterfilling_and_sigma_cap() -> None:
    alloc = RouteDPlusAllocator()
    syms = ["160723", "161129", "162411"]
    anchors = {"160723": "fof_t0_multisource", "161129": "uso_bno_sc_peer", "162411": "xop_us_ep"}
    snaps = {s: _make_snap(m_rec=1.10, k_ovs=0.35) for s in syms}

    hi_corrs = {(s1, s2): (1.0 if s1 == s2 else 0.90) for s1 in syms for s2 in syms}
    lo_corrs = {(s1, s2): (1.0 if s1 == s2 else 0.25) for s1 in syms for s2 in syms}

    res_hi = alloc.compute_overnight_weights(
        symbols=syms,
        raw_weights={s: 1.0 for s in syms},
        anchors=anchors,
        causal_ctx={"snaps": snaps, "vols": {s: 2.5 for s in syms}, "corrs": hi_corrs},
        policy=normalize_pool_policy({"sigma_cap_pct": None}),
    )
    res_lo = alloc.compute_overnight_weights(
        symbols=syms,
        raw_weights={s: 1.0 for s in syms},
        anchors=anchors,
        causal_ctx={"snaps": snaps, "vols": {s: 2.5 for s in syms}, "corrs": lo_corrs},
        policy=normalize_pool_policy({"sigma_cap_pct": None}),
    )
    assert res_hi["caps"]["160723"] < res_lo["caps"]["160723"]
    assert res_hi["scores"]["160723"] < res_lo["scores"]["160723"]
    assert sum(res_lo["alloc_weights"].values()) <= 0.985 + 1e-6

    # Volatility ceiling sigma_cap_pct=4.20 scales down total allocation under extreme 7.0% vol
    res_vol = alloc.compute_overnight_weights(
        symbols=syms,
        raw_weights={s: 1.0 for s in syms},
        anchors=anchors,
        causal_ctx={"snaps": snaps, "vols": {s: 7.0 for s in syms}, "corrs": hi_corrs},
        policy=normalize_pool_policy({"sigma_cap_pct": 4.20}),
    )
    assert res_vol["sigma_scaled"] is True
    assert res_vol["pre_cap_sigma_pct"] > 4.20
    assert res_vol["portfolio_sigma_pct"] == pytest.approx(4.20, abs=1e-3)


def test_overnight_rebalance_100_share_floor_and_cash_conservation() -> None:
    alloc = RouteDPlusAllocator()
    syms = ["160723", "162411"]
    anchors = {"160723": "fof_t0_multisource", "162411": "xop_us_ep"}
    snaps = {"160723": _make_snap(m_rec=1.0), "162411": _make_snap(m_rec=1.35, k_ovs=0.45)}
    corrs = {("160723", "160723"): 1.0, ("162411", "162411"): 1.0, ("160723", "162411"): 0.30, ("162411", "160723"): 0.30}
    prices = {"160723": 2.000, "162411": 1.000}
    shares = {"160723": 50_000, "162411": 0}
    avail_cash = 40_000.0
    pool_eq = avail_cash + 50_000 * 2.000

    plan = alloc.plan_overnight_rebalance(
        symbols=syms,
        raw_weights={"160723": 0.0, "162411": 1.0},
        current_shares=shares,
        prices=prices,
        min_retained_by_symbol={"160723": 100, "162411": 100},
        pool_equity=pool_eq,
        available_pool_cash=avail_cash,
        anchors=anchors,
        causal_ctx={"snaps": snaps, "vols": {"160723": 2.5, "162411": 2.5}, "corrs": corrs},
        policy=normalize_pool_policy({"sigma_cap_pct": 4.20}),
    )
    assert plan["target_shares"]["160723"] == 100
    assert plan["sell_orders"] == {"160723": 49_900}
    assert plan["floor_mv_locked"] == pytest.approx(100 * 2.000 * 1.00005, abs=0.05)
    assert plan["target_shares"]["162411"] == plan["buy_orders"]["162411"]
    assert plan["buy_orders"]["162411"] >= 115_000  # ~0.85 * 139,800 = 118,800 shares
    assert plan["estimated_post_buy_cash"] >= 0.0


def test_causal_context_strict_date_cutoff_and_cache_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    db_path = tmp_path / "causal_ctx.db"
    rstore = SQLiteResearchStore(db_path)
    qstore = SQLiteQuantRuleStore(db_path)

    def _seed(sym: str, td: str, pre_c: float, px: float, mkt: str = "36") -> None:
        rstore._connection.execute(
            """
            INSERT OR REPLACE INTO research_days
            (symbol, trade_date, name, market, source, pre_close, collected_at)
            VALUES (?, ?, ?, ?, 'test', ?, ?)
            """,
            (sym, td, sym, mkt, str(pre_c), f"{td[:4]}-{td[4:6]}-{td[6:8]}T15:00:00+08:00"),
        )
        rows = []
        for idx, tm in enumerate(["09:30", "10:00", "14:53", "15:00"]):
            rows.append((sym, td, tm, str(px), str(px), str(100 * (idx + 1)), None, None, None, None, f"{td[:4]}-{td[4:6]}-{td[6:8]}T{tm}:00+08:00"))
        rstore._connection.executemany(
            """
            INSERT OR REPLACE INTO research_points
            (symbol, trade_date, minute, price, average_price, volume, large_order_net, large_order_amount, retail_count, macdfs, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    dates = ["20261119", "20261120", "20261123", "20261124", "20261125"]
    with rstore._lock, rstore._connection:
        for i, td in enumerate(dates):
            px_brn = 70.0 + i * 0.2
            _seed("BRN0Y", td, px_brn, px_brn, mkt="ICE")
            _seed("160723", td, 2.00, 2.00 + i * 0.02)
            _seed("501018", td, 1.50, 1.50 + i * 0.015)
        # Future/target day 20261126 has extreme +40% outlier: MUST NEVER leak into 20261126 causal snapshot!
        _seed("BRN0Y", "20261126", 71.0, 99.0, mkt="ICE")
        _seed("160723", "20261126", 2.08, 2.95)
        _seed("501018", "20261126", 1.56, 2.15)

    r_160 = {
        "id": "rule-160",
        "updated_at": "2026-11-26T09:00:00+08:00",
        "position_sizing": {"mode": "cash_pool_rolling", "engine_profile": "v9_plus_aggressive", "cross_market_anchor": "brent"},
    }
    r_501 = {
        "id": "rule-501",
        "updated_at": "2026-11-26T09:00:00+08:00",
        "position_sizing": {"mode": "cash_pool_rolling", "engine_profile": "v9_plus_aggressive", "cross_market_anchor": "brent"},
    }

    alloc = RouteDPlusAllocator()
    ctx1 = alloc.get_causal_context(rstore, {"160723": r_160, "501018": r_501}, "20261126")
    assert ctx1["cache_hit"] is False
    # Vol remains calm (~1-3%) rather than spiking from the 20261126 +40% outlier
    assert ctx1["vols"]["160723"] < 4.5
    assert ctx1["vols"]["501018"] < 4.5

    ctx2 = alloc.get_causal_context(rstore, {"160723": r_160, "501018": r_501}, "20261126")
    assert ctx2["cache_hit"] is True
    assert ctx2["vols"] == ctx1["vols"]

    qstore.close()
    rstore.close()


def test_coordinate_intraday_buys_filters_low_edge_and_scales_concurrent_buys() -> None:
    alloc = RouteDPlusAllocator()
    syms = ["160723", "162411", "501018"]
    anchors = {"160723": "fof_t0_multisource", "162411": "xop_us_ep", "501018": "brent"}
    bad_snap = _make_snap(m_rec=1.0, p_bkt=0.41, k_bkt=0.05)  # below 0.48 floor!
    good_snap = _make_snap(m_rec=1.30, p_bkt=0.72, k_bkt=0.42)
    ctx = {
        "snaps": {"160723": good_snap, "162411": good_snap, "501018": bad_snap},
        "vols": {s: 2.5 for s in syms},
        "corrs": {(s1, s2): (1.0 if s1 == s2 else 0.40) for s1 in syms for s2 in syms},
    }
    res = alloc.coordinate_intraday_buys(
        buy_requests=[
            {"symbol": "160723", "price": 2.000, "signal_frac": 1.0, "signal_tag": "v9-entry"},
            {"symbol": "162411", "price": 1.000, "signal_frac": 1.0, "signal_tag": "v9-entry"},
            {"symbol": "501018", "price": 1.500, "signal_frac": 1.0, "signal_tag": "v9-entry"},
        ],
        symbols=syms,
        held_mvs={"160723": 0.0, "162411": 0.0, "501018": 0.0},
        pool_equity=100_000.0,
        free_pool_cash=50_000.0,
        anchors=anchors,
        causal_ctx=ctx,
    )
    assert res["501018"]["approved"] is False
    assert "0.48" in res["501018"]["reason"]
    assert res["160723"]["approved"] is True
    assert res["162411"]["approved"] is True
    spent = res["160723"]["qty"] * 2.0 + res["162411"]["qty"] * 1.0
    assert spent <= 50_000.0

    # Prior & EWMA cov helper sanity check
    prior = bayesian_prior_snapshot()
    assert prior["m_rec"] == 1.0
    vols, corrs = compute_ewma_vols_and_corrs(syms, {s: [] for s in syms})
    assert vols == {"160723": 3.20, "162411": 3.20, "501018": 3.20}
    assert corrs[("160723", "162411")] == pytest.approx(0.55)
    assert CausalStats().snapshot()["sentry"] == 0.0


def test_sqlite_portfolio_policy_store_crud_and_multi_engine_weights(tmp_path: Path) -> None:
    store = SQLitePortfolioPolicyStore(tmp_path / "policies.db")
    policies = store.list_policies()
    ids = {p["id"] for p in policies}
    assert {"route_d_sharpe_shield", "route_d_calmar_peak", "route_d_plus", "static_cap", "route_d_shadow"} <= ids
    shield = store.get_policy("route_d_sharpe_shield")
    assert shield is not None
    assert shield["engine_type"] == "route_d_plus"
    assert shield["params"]["sigma_cap_pct"] == pytest.approx(2.40)
    assert shield["params"]["solo_crude_cap"] == pytest.approx(0.42)
    assert shield["params"]["total_pool_cap"] is None

    # Built-in policy cannot be deleted
    with pytest.raises(ValueError):
        store.delete_policy("route_d_sharpe_shield")

    # API-published custom strategy lifecycle
    schema = get_portfolio_policy_schema()
    assert any(e["id"] == "inverse_vol_risk_parity" for e in schema["engines"])

    custom = store.upsert_policy(
        {
            "id": "custom-inv-vol-v1",
            "name": "逆波动率动态池",
            "description": "按1/sigma分配",
            "engine_type": "inverse_vol_risk_parity",
            "params": {
                "sigma_cap_pct": 2.1,
                "max_total_budget_wt": 0.90,
                "symbol_weight_multipliers": {"162411": 1.5},
                "total_pool_cap": 999999.0,  # stripped from reusable strategy template
            },
            "metrics_summary": {"sharpe_ratio": 5.42, "max_drawdown_pct": 6.2},
        }
    )
    assert custom["is_builtin"] is False
    assert custom["params"]["total_pool_cap"] is None
    assert custom["params"]["symbol_weight_multipliers"] == {"162411": pytest.approx(1.5)}

    alloc = RouteDPlusAllocator()
    syms = ["160723", "162411", "501018"]
    ctx = {
        "snaps": {s: bayesian_prior_snapshot() for s in syms},
        "vols": {"160723": 3.0, "162411": 1.5, "501018": 3.0},
        "corrs": {(a, b): (1.0 if a == b else 0.35) for a in syms for b in syms},
    }
    w_iv = alloc.compute_overnight_weights(
        symbols=syms,
        raw_weights={s: 1.0 for s in syms},
        anchors={"160723": "fof", "162411": "xop_us_ep", "501018": "brent"},
        causal_ctx=ctx,
        policy=custom["params"],
        engine_type="inverse_vol_risk_parity",
    )
    # 162411 has lower vol AND 1.5x multiplier -> highest weight
    assert w_iv["alloc_weights"]["162411"] > w_iv["alloc_weights"]["160723"]
    assert w_iv["portfolio_sigma_pct"] <= 2.12

    w_eq = alloc.compute_overnight_weights(
        symbols=syms,
        raw_weights={"160723": 0.9, "162411": 0.9, "501018": 0.0},
        anchors={"160723": "fof", "162411": "xop_us_ep", "501018": "brent"},
        causal_ctx=ctx,
        policy={"sigma_cap_pct": None, "max_total_budget_wt": 0.80},
        engine_type="equal_weight",
    )
    assert w_eq["alloc_weights"]["501018"] == pytest.approx(0.0)
    assert w_eq["alloc_weights"]["160723"] == pytest.approx(w_eq["alloc_weights"]["162411"], rel=1e-4)

    store.delete_policy("custom-inv-vol-v1")
    assert store.get_policy("custom-inv-vol-v1") is None
    assert len(BUILTIN_PORTFOLIO_POLICIES) == 5
    store.close()


def test_overnight_rebalance_initializes_and_holds_100_share_floor_unconditionally() -> None:
    from level2_service.portfolio_allocator import (
        RouteDPlusAllocator,
        get_portfolio_policy_schema,
        normalize_pool_policy,
    )

    alloc = RouteDPlusAllocator()
    pp = normalize_pool_policy(
        {
            "morning_catchup_end_hhmm": "09:45",
            "morning_catchup_max_gap_pct": 12.0,
        },
    )
    assert pp["enable_morning_base_catchup"] is True
    assert pp["morning_catchup_end_hhmm"] == "09:45"
    assert pp["morning_catchup_max_gap_pct"] == pytest.approx(9.50)

    bad_time = normalize_pool_policy({"morning_catchup_end_hhmm": "14:50"})
    assert bad_time["morning_catchup_end_hhmm"] == "09:50"
    schema_keys = set((get_portfolio_policy_schema().get("params_schema") or {}).keys())
    assert {
        "enforce_min_retained_floor",
        "enable_morning_base_catchup",
        "morning_catchup_end_hhmm",
        "morning_catchup_max_gap_pct",
        "morning_catchup_max_sp_fx_pct",
        "morning_catchup_max_prem_pct",
    } <= schema_keys

    syms = ["160723", "161129", "162411"]
    causal_ctx = {
        "vols": {s: 0.02 for s in syms},
        "corrs": {(a, b): (1.0 if a == b else 0.55) for a in syms for b in syms},
        "shrink_mults": {s: 1.0 for s in syms},
    }
    plan = alloc.plan_overnight_rebalance(
        symbols=syms,
        raw_weights={"160723": 0.0, "161129": 0.0, "162411": 1.0},
        current_shares={"160723": 50000, "161129": 0, "162411": 0},
        prices={"160723": 2.00, "161129": 1.80, "162411": 1.00},
        pool_equity=100000.0,
        available_pool_cash=2000.0,
        anchors={"160723": "brent", "161129": "brent", "162411": "xop_us_ep"},
        causal_ctx=causal_ctx,
        min_retained_by_symbol={"160723": 100, "161129": 100, "162411": 100},
        policy=pp,
        engine_type="route_d_sharpe_shield",
    )
    assert plan["target_shares"]["160723"] == 100
    assert plan["sell_orders"]["160723"] == 49900
    assert plan["target_shares"]["161129"] == 100
    assert plan["buy_orders"]["161129"] == 100
    assert plan["target_shares"]["162411"] >= 100
    assert plan["buy_orders"]["162411"] == plan["target_shares"]["162411"]
    fee = float(pp["single_fee_rate"])
    net_cash = (
        plan["sell_orders"]["160723"] * 2.00 * (1.0 - fee)
        + 2000.0
        - plan["buy_orders"]["161129"] * 1.80 * (1.0 + fee)
        - plan["buy_orders"]["162411"] * 1.00 * (1.0 + fee)
    )
    assert net_cash >= -1e-3
