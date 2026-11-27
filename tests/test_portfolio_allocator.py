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
    assert {
        "route_d_oil_regime",
        "route_d_sharpe_shield",
        "route_d_calmar_peak",
        "route_d_plus",
        "manual_cap",
        "static_cap",
        "route_d_shadow",
    } <= ids
    oil_reg_pol = store.get_policy("route_d_oil_regime")
    assert oil_reg_pol is not None
    assert oil_reg_pol["engine_type"] == "route_d_plus"
    assert oil_reg_pol["params"]["regime_router_enabled"] is True
    assert oil_reg_pol["params"]["regime_router_profile"] == "oil_v5_dual_key"
    assert oil_reg_pol["params"]["decouple_overnight_mrec"] is True
    assert oil_reg_pol["params"]["sigma_cap_pct"] == pytest.approx(2.40)
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
    assert len(BUILTIN_PORTFOLIO_POLICIES) == 7
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


def test_causal_stats_decouple_overnight_mrec_prevents_intraday_sentry_lock() -> None:
    stats = CausalStats()
    # 5 winning overnight holds, followed by 4 consecutive intraday T losses
    for _ in range(5):
        stats.record({"kind": "overnight", "bucket": "overnight_strong", "ret_pct": 1.2})
    for _ in range(4):
        stats.record({"kind": "intraday", "bucket": "buy_tier_a", "ret_pct": -0.6})

    coupled = stats.snapshot(decouple_overnight=False)
    assert coupled["sentry"] == 1.0
    assert coupled["m_rec"] == pytest.approx(0.30)

    decoupled = stats.snapshot(decouple_overnight=True)
    assert decoupled["sentry"] == 0.0
    assert decoupled["m_rec"] > 1.20


def test_route_d_oil_regime_relay_downtrend_caps_crude_cluster_and_preserves_solo_501018() -> None:
    alloc = RouteDPlusAllocator()
    syms = ["160723", "161129", "501018", "162411"]
    anchors = {
        "160723": "fof_t0_multisource",
        "161129": "uso_bno_sc_peer",
        "501018": "brent",
        "162411": "xop_us_ep",
    }
    snaps = {s: _make_snap(m_rec=1.35, k_ovs=0.45, p_bkt=0.72, k_bkt=0.42) for s in syms}
    corrs = {(a, b): (1.0 if a == b else 0.90) for a in syms for b in syms}
    vols = {s: 2.0 for s in syms}

    relay_regime = {
        "enforced": True,
        "profile": "oil_v5_dual_key",
        "oil_reference_date": "20261126",
        "oil_close_t1": 91.70,
        "locked_regime": "中继下跌",
        "effective_regime": "中继下跌",
        "probs": {"上升": 0.5, "震荡上行": 1.1, "震荡横盘": 15.9, "中继下跌": 70.3, "下降": 12.2},
        "ma_confirm_ratio": 0.0,
        "effective_cluster_cap_pct": 48.0,
        "effective_sigma_cap_pct": 1.98,
        "solo_crude_coef": 0.33,
        "solo_crude_cap": 0.45,
        "symbol_multipliers": {"160723": 1.08, "161129": 0.64, "501018": 0.94, "162411": 1.08},
        "symbol_tuning_hints": {"rev_t_501018": 0.46, "unconf_wt": 0.52, "taper_501018": 0.50},
        "fast_crash_active": False,
        "degraded_mode": None,
        "degraded_sources": [],
    }
    ctx = {"snaps": snaps, "vols": vols, "corrs": corrs, "oil_regime": relay_regime}

    # Baseline without regime router: 3 crude LOFs firing together surge > 85%
    pol_no_router = normalize_pool_policy({"sigma_cap_pct": None, "regime_router_enabled": False})
    res_base = alloc.compute_overnight_weights(
        symbols=syms,
        raw_weights={"160723": 1.0, "161129": 1.0, "501018": 1.0, "162411": 0.0},
        anchors=anchors,
        causal_ctx=ctx,
        policy=pol_no_router,
    )
    crude_sum_base = sum(res_base["alloc_weights"][s] for s in ("160723", "161129", "501018"))
    assert crude_sum_base > 0.85

    # With route_d_oil_regime enabled: 3-crude resonance is compressed to ~48%
    oil_regime_builtin = next(p for p in BUILTIN_PORTFOLIO_POLICIES if p["id"] == "route_d_oil_regime")
    res_routed = alloc.compute_overnight_weights(
        symbols=syms,
        raw_weights={"160723": 1.0, "161129": 1.0, "501018": 1.0, "162411": 0.0},
        anchors=anchors,
        causal_ctx=ctx,
        policy=oil_regime_builtin["params"],
    )
    crude_sum_routed = sum(res_routed["alloc_weights"][s] for s in ("160723", "161129", "501018"))
    assert res_routed["crude_cluster_scaled"] is True
    assert crude_sum_routed == pytest.approx(0.48, abs=0.01)
    assert res_routed["portfolio_sigma_pct"] <= 1.98 + 1e-3

    # Downtrend extreme: cluster_cap=42%, solo_crude_cap=48% -> solo 501018 MUST keep 48% exemption!
    down_regime = {
        **relay_regime,
        "locked_regime": "下降",
        "effective_regime": "下降",
        "effective_cluster_cap_pct": 42.0,
        "effective_sigma_cap_pct": 2.40,
        "solo_crude_coef": 0.36,
        "solo_crude_cap": 0.48,
        "symbol_multipliers": {"160723": 0.95, "161129": 0.58, "501018": 1.18, "162411": 0.95},
    }
    res_solo_501 = alloc.compute_overnight_weights(
        symbols=syms,
        raw_weights={"160723": 0.0, "161129": 0.0, "501018": 1.0, "162411": 0.0},
        anchors=anchors,
        causal_ctx={**ctx, "oil_regime": down_regime},
        policy=oil_regime_builtin["params"],
    )
    assert res_solo_501["is_solo"] is True
    assert res_solo_501["crude_cluster_scaled"] is False
    assert res_solo_501["alloc_weights"]["501018"] == pytest.approx(0.48, abs=0.01)

    # Intraday coordinate_intraday_buys MUST enforce crude_cluster_cap across multi-crude buys
    pool_eq = 100_000.0
    intraday_res = alloc.coordinate_intraday_buys(
        buy_requests=[
            {"symbol": "160723", "price": 2.000, "signal_frac": 1.0, "signal_tag": "v9-entry"},
            {"symbol": "161129", "price": 1.800, "signal_frac": 1.0, "signal_tag": "v9-entry"},
            {"symbol": "501018", "price": 1.500, "signal_frac": 1.0, "signal_tag": "v9-entry"},
        ],
        symbols=syms,
        held_mvs={"160723": 20_000.0, "161129": 15_000.0, "501018": 5_000.0, "162411": 0.0},
        pool_equity=pool_eq,
        free_pool_cash=60_000.0,
        anchors=anchors,
        causal_ctx=ctx,
        policy=oil_regime_builtin["params"],
    )
    bought_crude_mv = (
        intraday_res["160723"]["qty"] * 2.000
        + intraday_res["161129"]["qty"] * 1.800
        + intraday_res["501018"]["qty"] * 1.500
    )
    # Existing crude held = 40,000; 48% cap = 48,000 -> new crude buys MUST NOT exceed 8,000!
    assert bought_crude_mv > 0.0
    assert 40_000.0 + bought_crude_mv <= 48_000.0 + 1e-3

    # When crude cluster is already >= 48% held, further multi-crude buys must be rejected
    blocked_res = alloc.coordinate_intraday_buys(
        buy_requests=[
            {"symbol": "160723", "price": 2.000, "signal_frac": 1.0, "signal_tag": "v9-entry"},
            {"symbol": "501018", "price": 1.500, "signal_frac": 1.0, "signal_tag": "v9-entry"},
        ],
        symbols=syms,
        held_mvs={"160723": 28_000.0, "161129": 20_000.0, "501018": 0.0, "162411": 0.0},
        pool_equity=pool_eq,
        free_pool_cash=52_000.0,
        anchors=anchors,
        causal_ctx=ctx,
        policy=oil_regime_builtin["params"],
    )
    assert blocked_res["160723"]["approved"] is False
    assert blocked_res["160723"]["reason"] == "crude_cluster_cap_reached"
    assert blocked_res["501018"]["approved"] is False
    assert blocked_res["501018"]["reason"] == "crude_cluster_cap_reached"


def test_normalize_pool_policy_regime_fields_and_schema() -> None:
    pol = normalize_pool_policy(
        {
            "regime_router_enabled": True,
            "regime_router_profile": "oil_v5_dual_key",
            "crude_cluster_cap": 0.12,
            "decouple_overnight_mrec": True,
        }
    )
    assert pol["regime_router_enabled"] is True
    assert pol["regime_router_profile"] == "oil_v5_dual_key"
    assert pol["crude_cluster_cap"] == pytest.approx(0.20)
    assert pol["decouple_overnight_mrec"] is True

    pol_none = normalize_pool_policy({"crude_cluster_cap": ""})
    assert pol_none["crude_cluster_cap"] is None

    schema_keys = set((get_portfolio_policy_schema().get("params_schema") or {}).keys())
    assert {
        "regime_router_enabled",
        "regime_router_profile",
        "crude_cluster_cap",
        "decouple_overnight_mrec",
    } <= schema_keys


def test_get_causal_context_integrates_oil_regime_and_keeps_live_brn_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from datetime import timedelta
    from level2_service.v9_plus_engine import V9MacroDataStore

    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    rstore = SQLiteResearchStore(tmp_path / "causal_regime.db")
    V9MacroDataStore(rstore, allow_network=False)

    # Seed 150 days of oil/ho/xop daily rows strictly before 20261127
    dt = datetime(2026, 4, 1)
    oil_rows = []
    ho_rows = []
    xop_rows = []
    closes = [95.0 + 18.0 * (i / 119.0) for i in range(120)] + [
        113.0 - (113.0 - 86.7) * (i / 25.0) for i in range(1, 26)
    ] + [88.0, 89.2, 90.7, 91.7]
    for px in closes:
        while dt.weekday() >= 5:
            dt += timedelta(days=1)
        d_str = dt.strftime("%Y%m%d")
        ts = int((dt + timedelta(days=1, hours=6)).timestamp())
        oil_rows.append([d_str, ts, round(px, 4)])
        ho_rows.append([d_str, ts, round(px * 0.04, 4)])
        xop_rows.append([d_str, ts, round(px * 1.8, 4)])
        dt += timedelta(days=1)
    while dt.weekday() >= 5:
        dt += timedelta(days=1)
    trade_date = dt.strftime("%Y%m%d")

    with rstore._lock, rstore._connection:
        for key, arr in (("oil_daily_2y", oil_rows), ("ho_daily_2y", ho_rows), ("xop_daily_2y", xop_rows)):
            rstore._connection.execute(
                "INSERT OR REPLACE INTO quant_macro_cache (cache_key, payload_json, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(arr), 1_795_750_000.0),
            )

    rules = {
        "160723": {"id": "r1", "updated_at": "2026-11-26T09:00:00+08:00", "position_sizing": {"cross_market_anchor": "brent"}},
        "501018": {"id": "r2", "updated_at": "2026-11-26T09:00:00+08:00", "position_sizing": {"cross_market_anchor": "brent"}},
    }
    alloc = RouteDPlusAllocator()
    pol_on = {"regime_router_enabled": True, "decouple_overnight_mrec": True}
    ctx1 = alloc.get_causal_context(rstore, rules, trade_date, policy=pol_on)
    assert ctx1["cache_hit"] is False
    assert isinstance(ctx1["oil_regime"], dict)
    assert ctx1["oil_regime"]["enforced"] is True
    assert ctx1["oil_regime"]["fast_crash_active"] is False

    # Intraday crash call hits day cache and overlays read-only fast crash
    ctx_crash = alloc.get_causal_context(
        rstore, rules, trade_date, policy=pol_on, live_brn_price=85.0, live_brn_age_sec=15.0
    )
    assert ctx_crash["cache_hit"] is True
    assert ctx_crash["oil_regime"]["fast_crash_active"] is True
    assert ctx_crash["oil_regime"]["effective_regime"] == "下降"

    # Next regular call still reads clean unpolluted base state from cache
    ctx_clean = alloc.get_causal_context(rstore, rules, trade_date, policy=pol_on)
    assert ctx_clean["cache_hit"] is True
    assert ctx_clean["oil_regime"]["fast_crash_active"] is False
    assert ctx_clean["oil_regime"] == ctx1["oil_regime"]
    rstore.close()


def test_compute_symbol_intraday_cap_aligns_regime_multipliers_and_crude_cluster_cap() -> None:
    alloc = RouteDPlusAllocator()
    syms = ["160723", "161129", "501018", "162411"]
    anchors = {
        "160723": "fof_t0_multisource",
        "161129": "uso_bno_sc_peer",
        "501018": "brent",
        "162411": "xop_us_ep",
    }
    snaps = {
        "160723": _make_snap(m_rec=1.35),
        "161129": _make_snap(m_rec=1.00),
        "501018": _make_snap(m_rec=1.35),
        "162411": _make_snap(m_rec=1.35),
    }
    corrs = {(a, b): (1.0 if a == b else 0.55) for a in syms for b in syms}
    vols = {s: 2.2 for s in syms}
    oil_regime = {
        "enforced": True,
        "locked_regime": "中继下跌",
        "effective_regime": "中继下跌",
        "effective_cluster_cap_pct": 36.0,
        "effective_sigma_cap_pct": 1.98,
        "solo_crude_coef": 0.33,
        "solo_crude_cap": 0.45,
        "symbol_multipliers": {"160723": 1.08, "161129": 0.64, "501018": 0.94, "162411": 1.08},
    }
    ctx = {"snaps": snaps, "vols": vols, "corrs": corrs, "oil_regime": oil_regime}
    pol_routed = next(p for p in BUILTIN_PORTFOLIO_POLICIES if p["id"] == "route_d_oil_regime")["params"]
    pol_base = normalize_pool_policy({"regime_router_enabled": False})

    # 1. Solo context (held_mvs empty -> is_multi_context=False):
    # - 161129 has mult=0.64 < 1.0 -> r_max = 0.33 * 1.00 * 0.64 = 0.2112
    # - 501018 has mult=0.94 < 1.0 -> 0.33 * 1.35 * 0.94 = 0.41877 (NOT clipped to crude_cluster_cap=0.36 in solo!)
    # - 160723 has mult=1.08 >= 1.0 -> 0.33 * 1.35 * 1.0 = 0.4455 (no inflation above 1.0, <= solo_crude_cap=0.45)
    solo_held = {s: 0.0 for s in syms}
    cap_161_base = alloc.compute_symbol_intraday_cap(
        symbol="161129", symbols=syms, held_mvs=solo_held, pool_equity=100_000.0, anchors=anchors, causal_ctx=ctx, policy=pol_base
    )
    cap_161_solo = alloc.compute_symbol_intraday_cap(
        symbol="161129", symbols=syms, held_mvs=solo_held, pool_equity=100_000.0, anchors=anchors, causal_ctx=ctx, policy=pol_routed
    )
    assert cap_161_solo["is_multi_context"] is False
    assert cap_161_solo["r_max"] == pytest.approx(0.33 * 1.00 * 0.64, abs=1e-4)
    assert cap_161_solo["cap_amount"] < cap_161_base["cap_amount"]

    cap_501_solo = alloc.compute_symbol_intraday_cap(
        symbol="501018", symbols=syms, held_mvs=solo_held, pool_equity=100_000.0, anchors=anchors, causal_ctx=ctx, policy=pol_routed
    )
    assert cap_501_solo["is_multi_context"] is False
    assert cap_501_solo["r_max"] == pytest.approx(0.33 * 1.35 * 0.94, abs=1e-4)
    assert cap_501_solo["r_max"] > 0.36

    cap_160_solo = alloc.compute_symbol_intraday_cap(
        symbol="160723", symbols=syms, held_mvs=solo_held, pool_equity=100_000.0, anchors=anchors, causal_ctx=ctx, policy=pol_routed
    )
    assert cap_160_solo["r_max"] == pytest.approx(0.33 * 1.35, abs=1e-4)

    # 2. Multi-symbol context (is_multi_context=True):
    # - Crude anchor 160723 (raw 0.48 * 1.35 = 0.648) is capped by crude_cluster_cap=0.36
    # - Crude anchor 161129 (0.48 * 1.00 * 0.64 = 0.3072 < 0.36) preserves multiplier reduction
    # - Equity anchor 162411 (xop_us_ep) is exempt from crude_cluster_cap=0.36
    multi_held = {"160723": 20_000.0, "161129": 20_000.0, "501018": 15_000.0, "162411": 0.0}
    cap_160_multi = alloc.compute_symbol_intraday_cap(
        symbol="160723", symbols=syms, held_mvs=multi_held, pool_equity=100_000.0, anchors=anchors, causal_ctx=ctx, policy=pol_routed
    )
    assert cap_160_multi["is_multi_context"] is True
    assert cap_160_multi["r_max"] == pytest.approx(0.36, abs=1e-6)
    assert cap_160_multi["cap_amount"] == pytest.approx(36_000.0, abs=1e-2)

    cap_161_multi = alloc.compute_symbol_intraday_cap(
        symbol="161129", symbols=syms, held_mvs=multi_held, pool_equity=100_000.0, anchors=anchors, causal_ctx=ctx, policy=pol_routed
    )
    assert cap_161_multi["is_multi_context"] is True
    assert cap_161_multi["r_max"] == pytest.approx(0.48 * 1.00 * 0.64, abs=1e-4)

    cap_162_multi = alloc.compute_symbol_intraday_cap(
        symbol="162411", symbols=syms, held_mvs=multi_held, pool_equity=100_000.0, anchors=anchors, causal_ctx=ctx, policy=pol_routed
    )
    assert cap_162_multi["is_multi_context"] is True
    assert cap_162_multi["r_max"] == pytest.approx(0.48 * 1.35, abs=1e-4)
    assert cap_162_multi["r_max"] > 0.36



