from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any

import pytest

from level2_service.oil_regime_router import (
    CRUDE_CLUSTER_SYMBOLS,
    OIL_REGIMES,
    V5_REGIME_TABLE,
    V62_DIMENSION_ORDER,
    V62_DIMENSION_SHORT_LABELS,
    OilRegimeRouter,
)
from level2_service.research_store import SQLiteResearchStore
from level2_service.v9_plus_engine import V9MacroDataStore


def _make_daily_rows(
    start_yyyymmdd: str,
    closes: list[float],
    *,
    skip_weekends: bool = True,
) -> list[list[Any]]:
    dt = datetime.strptime(start_yyyymmdd, "%Y%m%d")
    rows: list[list[Any]] = []
    for px in closes:
        while skip_weekends and dt.weekday() >= 5:
            dt += timedelta(days=1)
        d_str = dt.strftime("%Y%m%d")
        causal_ts = int((dt + timedelta(days=1, hours=6)).timestamp())
        rows.append([d_str, causal_ts, round(float(px), 4)])
        dt += timedelta(days=1)
    return rows


def _seed_macro_cache(
    rstore: SQLiteResearchStore,
    *,
    oil_rows: list[list[Any]] | None = None,
    ho_rows: list[list[Any]] | None = None,
    xop_rows: list[list[Any]] | None = None,
) -> None:
    # Ensure quant_macro_cache table is initialized
    macro = V9MacroDataStore(rstore, allow_network=False)
    now_ts = 1_795_750_000.0
    payloads = {
        "oil_daily_2y": oil_rows,
        "ho_daily_2y": ho_rows,
        "xop_daily_2y": xop_rows,
    }
    with rstore._lock, rstore._connection:
        for key, data in payloads.items():
            if data is None:
                rstore._connection.execute("DELETE FROM quant_macro_cache WHERE cache_key = ?", (key,))
            else:
                rstore._connection.execute(
                    """
                    INSERT OR REPLACE INTO quant_macro_cache (cache_key, payload_json, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (key, json.dumps(data), now_ts),
                )
    assert macro is not None


def _build_relay_downtrend_closes() -> list[float]:
    """Build 160 trading days ending in a steep plunge followed by a weak 4-day bounce under MA15."""
    # 125 calm/rising days around 95 -> 113, then 31-day plunge to 86.7, then 4-day bounce to 91.7
    closes: list[float] = []
    for i in range(125):
        closes.append(95.0 + 18.0 * (i / 124.0))
    for i in range(1, 32):
        closes.append(113.0 - (113.0 - 86.7) * (i / 31.0))
    closes.extend([88.0, 89.2, 90.7, 91.7])
    return closes


def _build_steady_uptrend_closes() -> list[float]:
    """Build 160 trading days of steady bullish uptrend."""
    return [70.0 * (1.0035 ** i) for i in range(160)]


def test_strict_zero_lookahead_ignores_same_day_oil_ho_xop(tmp_path: Path) -> None:
    rstore = SQLiteResearchStore(tmp_path / "lookahead.db")
    oil_closes = _build_relay_downtrend_closes()
    oil_rows = _make_daily_rows("20260401", oil_closes)
    ho_rows = _make_daily_rows("20260401", [c * 0.04 for c in oil_closes])
    xop_rows = _make_daily_rows("20260401", [c * 1.8 for c in oil_closes])

    last_d = oil_rows[-1][0]
    td_dt = datetime.strptime(last_d, "%Y%m%d") + timedelta(days=1)
    while td_dt.weekday() >= 5:
        td_dt += timedelta(days=1)
    trade_date = td_dt.strftime("%Y%m%d")

    _seed_macro_cache(rstore, oil_rows=oil_rows, ho_rows=ho_rows, xop_rows=xop_rows)
    router_base = OilRegimeRouter()
    res_clean = router_base.evaluate_regime(rstore, trade_date=trade_date)

    # Inject extreme +60% spike AND -50% crash on `date == trade_date` and future date `> trade_date`
    future_d = (td_dt + timedelta(days=1)).strftime("%Y%m%d")
    oil_spike = oil_rows + [
        [trade_date, int((td_dt + timedelta(days=1, hours=6)).timestamp()), 150.0],
        [future_d, int((td_dt + timedelta(days=2, hours=6)).timestamp()), 35.0],
    ]
    ho_spike = ho_rows + [[trade_date, int((td_dt + timedelta(days=1, hours=6)).timestamp()), 9.99]]
    xop_spike = xop_rows + [[trade_date, int((td_dt + timedelta(days=1, hours=6)).timestamp()), 350.0]]

    _seed_macro_cache(rstore, oil_rows=oil_spike, ho_rows=ho_spike, xop_rows=xop_spike)
    router_spike = OilRegimeRouter()
    res_spike = router_spike.evaluate_regime(rstore, trade_date=trade_date)

    assert res_clean == res_spike
    assert res_clean["oil_reference_date"] == last_d
    assert res_clean["oil_reference_date"] < trade_date
    assert set(res_clean["probs"].keys()) == set(OIL_REGIMES)
    assert sum(res_clean["probs"].values()) == pytest.approx(100.0, abs=0.05)
    assert res_clean["locked_regime"] in ("中继下跌", "下降")
    assert res_clean["effective_cluster_cap_pct"] < 58.0
    rstore.close()


def test_120_day_stateless_replay_is_deterministic_across_restarts(tmp_path: Path) -> None:
    rstore = SQLiteResearchStore(tmp_path / "replay.db")
    # Build 260 days so replay starts strictly at `len - 120`
    base = [78.0 + 4.0 * ((i % 14) / 14.0) for i in range(100)] + _build_relay_downtrend_closes()
    oil_rows = _make_daily_rows("20251001", base)
    ho_rows = _make_daily_rows("20251001", [c * 0.04 for c in base])
    xop_rows = _make_daily_rows("20251001", [c * 1.75 for c in base])

    last_dt = datetime.strptime(oil_rows[-1][0], "%Y%m%d") + timedelta(days=1)
    while last_dt.weekday() >= 5:
        last_dt += timedelta(days=1)
    trade_date = last_dt.strftime("%Y%m%d")
    _seed_macro_cache(rstore, oil_rows=oil_rows, ho_rows=ho_rows, xop_rows=xop_rows)

    # Fresh router instance vs warm cached instance vs truncated-to-180-day history (same last 120+60 days)
    r1 = OilRegimeRouter()
    r2 = OilRegimeRouter()
    out1 = r1.evaluate_regime(rstore, trade_date=trade_date)
    out1_cached = r1.evaluate_regime(rstore, trade_date=trade_date)
    out2 = r2.evaluate_regime(rstore, trade_date=trade_date)

    assert out1 == out1_cached == out2

    # Truncating irrelevant older history before `len - 180` yields identical replay result
    _seed_macro_cache(
        rstore,
        oil_rows=oil_rows[-180:],
        ho_rows=ho_rows[-180:],
        xop_rows=xop_rows[-180:],
    )
    r3 = OilRegimeRouter()
    out3 = r3.evaluate_regime(rstore, trade_date=trade_date)
    assert out3 == out1
    rstore.close()


def test_three_tier_degradation_and_fast_crash_does_not_pollute_cache(tmp_path: Path) -> None:
    rstore = SQLiteResearchStore(tmp_path / "degrade.db")
    oil_closes = _build_steady_uptrend_closes()
    oil_rows = _make_daily_rows("20260401", oil_closes)
    last_dt = datetime.strptime(oil_rows[-1][0], "%Y%m%d") + timedelta(days=1)
    while last_dt.weekday() >= 5:
        last_dt += timedelta(days=1)
    trade_date = last_dt.strftime("%Y%m%d")

    # --- Tier 1: missing ho_daily & xop_daily ---
    _seed_macro_cache(rstore, oil_rows=oil_rows, ho_rows=[], xop_rows=[])
    router = OilRegimeRouter()
    res_t1 = router.evaluate_regime(rstore, trade_date=trade_date)
    assert res_t1["degraded_mode"] == "auxiliary_degraded"
    assert "ho_daily" in res_t1["degraded_sources"]
    assert "xop_daily" in res_t1["degraded_sources"]
    assert res_t1["locked_regime"] in ("上升", "震荡上行")
    assert res_t1["fast_crash_active"] is False

    # --- Tier 3: intraday Fast Crash ramp is read-only and never mutates daily cache ---
    ref_close = float(res_t1["oil_close_t1"])
    crash_px = ref_close * 0.94  # -6.0% intraday plunge -> full w_crash=0.55 >= 0.35
    res_crash = router.evaluate_regime(
        rstore,
        trade_date=trade_date,
        live_brn_price=crash_px,
        live_brn_age_sec=30.0,
    )
    assert res_crash["fast_crash_active"] is True
    assert res_crash["effective_regime"] == "下降"
    assert res_crash["locked_regime"] == res_t1["locked_regime"]
    assert res_crash["probs"]["下降"] > res_t1["probs"]["下降"] + 30.0
    assert res_crash["effective_cluster_cap_pct"] < res_t1["effective_cluster_cap_pct"]
    assert res_crash["effective_sigma_cap_pct"] < res_t1["effective_sigma_cap_pct"]

    # Subsequent call without live_brn_price must match clean daily baseline identically!
    res_after_crash = router.evaluate_regime(rstore, trade_date=trade_date)
    assert res_after_crash == res_t1

    # Stale live_brn (> 600s) must disable Fast Crash override
    res_stale_live = router.evaluate_regime(
        rstore,
        trade_date=trade_date,
        live_brn_price=crash_px,
        live_brn_age_sec=900.0,
    )
    assert res_stale_live["fast_crash_active"] is False
    assert "live_brn" in res_stale_live["degraded_sources"]
    assert res_stale_live["effective_regime"] == res_t1["effective_regime"]

    # --- Tier 2: missing or too-short oil_daily -> safe fallback ---
    _seed_macro_cache(rstore, oil_rows=oil_rows[:40], ho_rows=[], xop_rows=[])
    res_short = router.evaluate_regime(rstore, trade_date=trade_date)
    assert res_short["degraded_mode"] == "oil_daily_unavailable"
    assert res_short["degraded_sources"] == ["oil_daily"]
    assert res_short["locked_regime"] == "震荡横盘"
    assert res_short["effective_regime"] == "震荡横盘"
    assert res_short["effective_cluster_cap_pct"] == pytest.approx(55.0)
    assert res_short["effective_sigma_cap_pct"] == pytest.approx(2.40)
    assert res_short["probs"] == {"上升": 10.0, "震荡上行": 20.0, "震荡横盘": 45.0, "中继下跌": 15.0, "下降": 10.0}

    # --- Tier 2: stale oil_daily (> 10 calendar days) -> safe fallback ---
    _seed_macro_cache(rstore, oil_rows=oil_rows, ho_rows=[], xop_rows=[])
    stale_td = (datetime.strptime(oil_rows[-1][0], "%Y%m%d") + timedelta(days=15)).strftime("%Y%m%d")
    res_stale = router.evaluate_regime(rstore, trade_date=stale_td)
    assert res_stale["degraded_mode"] == "oil_daily_unavailable"
    assert res_stale["degraded_sources"] == ["oil_daily"]
    assert res_stale["effective_cluster_cap_pct"] == pytest.approx(55.0)

    # --- Tier 2: None research_store ---
    res_none = router.evaluate_regime(None, trade_date=trade_date)
    assert res_none["degraded_mode"] == "oil_daily_unavailable"
    rstore.close()


def test_dual_key_ma_confirmation_ignores_162411_and_applies_overbought_taper(tmp_path: Path) -> None:
    rstore = SQLiteResearchStore(tmp_path / "dual_key.db")
    oil_closes = _build_steady_uptrend_closes()
    oil_rows = _make_daily_rows("20260401", oil_closes)
    ho_rows = _make_daily_rows("20260401", [c * 0.04 for c in oil_closes])
    xop_rows = _make_daily_rows("20260401", [c * 1.8 for c in oil_closes])
    last_dt = datetime.strptime(oil_rows[-1][0], "%Y%m%d") + timedelta(days=1)
    while last_dt.weekday() >= 5:
        last_dt += timedelta(days=1)
    trade_date = last_dt.strftime("%Y%m%d")
    _seed_macro_cache(rstore, oil_rows=oil_rows, ho_rows=ho_rows, xop_rows=xop_rows)

    router = OilRegimeRouter()
    # Crude cluster unconfirmed (MA5 < MA15), while 162411 (equity XOP) has huge golden cross
    res_unconf = router.evaluate_regime(
        rstore,
        trade_date=trade_date,
        symbol_ma_map={
            "160723": (1.90, 2.00),
            "161129": (1.70, 1.80),
            "501018": (1.40, 1.50),
            "162411": (1.50, 1.00),  # MUST be ignored for crude cluster g_crude!
        },
    )
    assert CRUDE_CLUSTER_SYMBOLS == frozenset({"160723", "161129", "501018"})
    assert res_unconf["ma_confirm_ratio"] == pytest.approx(0.0)

    # Now crude cluster confirms golden cross -> unlocks bull cluster & sigma caps
    res_conf = router.evaluate_regime(
        rstore,
        trade_date=trade_date,
        symbol_ma_map={
            "160723": (2.10, 2.00),
            "161129": (1.90, 1.80),
            "501018": (1.60, 1.50),
            "162411": (0.80, 1.00),  # Ignored
        },
    )
    assert res_conf["ma_confirm_ratio"] > 0.5
    assert res_conf["effective_cluster_cap_pct"] > res_unconf["effective_cluster_cap_pct"] + 15.0
    assert res_conf["effective_sigma_cap_pct"] > res_unconf["effective_sigma_cap_pct"]

    # Overbought momentum-blunting taper: pos25 >= 95 & ret10 >= 11 & ret3 <= 1.0
    surge_closes = [75.0] * 140 + [75.0 * (1.022 ** k) for k in range(1, 17)] + [109.5, 109.6, 109.7, 109.8]
    oil_surge = _make_daily_rows("20260401", surge_closes)
    ho_surge = _make_daily_rows("20260401", [c * 0.04 for c in surge_closes])
    xop_surge = _make_daily_rows("20260401", [c * 1.8 for c in surge_closes])
    td_surge = (datetime.strptime(oil_surge[-1][0], "%Y%m%d") + timedelta(days=3)).strftime("%Y%m%d")
    _seed_macro_cache(rstore, oil_rows=oil_surge, ho_rows=ho_surge, xop_rows=xop_surge)
    res_taper = router.evaluate_regime(
        rstore,
        trade_date=td_surge,
        symbol_ma_map={"160723": (2.20, 2.00), "161129": (2.00, 1.80), "501018": (1.70, 1.50)},
    )
    assert res_taper["effective_cluster_cap_pct"] <= 82.0 + 1e-6
    assert res_taper["effective_sigma_cap_pct"] <= 2.25 + 1e-6
    assert set(V5_REGIME_TABLE.keys()) == set(OIL_REGIMES)
    rstore.close()


def test_live_brn_fields_next_day_preview_and_off_hours_freshness(tmp_path: Path) -> None:
    rstore = SQLiteResearchStore(tmp_path / "live_preview.db")
    oil_closes = _build_steady_uptrend_closes()
    oil_rows = _make_daily_rows("20260401", oil_closes)
    ho_rows = _make_daily_rows("20260401", [c * 0.04 for c in oil_closes])
    xop_rows = _make_daily_rows("20260401", [c * 1.8 for c in oil_closes])
    last_dt = datetime.strptime(oil_rows[-1][0], "%Y%m%d") + timedelta(days=1)
    while last_dt.weekday() >= 5:
        last_dt += timedelta(days=1)
    trade_date = last_dt.strftime("%Y%m%d")
    _seed_macro_cache(rstore, oil_rows=oil_rows, ho_rows=ho_rows, xop_rows=xop_rows)

    router = OilRegimeRouter()
    sym_ma = {"160723": (2.10, 2.00), "161129": (1.90, 1.80), "501018": (1.60, 1.50)}

    # 1. Baseline without live_brn_price has empty live fields & next_day_preview=None
    res_base = router.evaluate_regime(rstore, trade_date=trade_date, symbol_ma_map=sym_ma)
    assert res_base["live_brn_price"] is None
    assert res_base["live_brn_chg_pct"] is None
    assert res_base["live_brn_fresh"] is False
    assert res_base["next_day_preview"] is None
    assert res_base["degraded_mode"] is None
    assert res_base["degraded_sources"] == []

    # 2. Fresh live_brn_price emits live telemetry + read-only next_day_preview without cache pollution
    ref_close = float(res_base["oil_close_t1"])
    live_up_px = round(ref_close * 1.02, 4)
    res_live = router.evaluate_regime(
        rstore,
        trade_date=trade_date,
        symbol_ma_map=sym_ma,
        live_brn_price=live_up_px,
        live_brn_age_sec=20.0,
    )
    assert res_live["live_brn_price"] == pytest.approx(live_up_px)
    assert res_live["live_brn_chg_pct"] == pytest.approx(2.0, abs=0.02)
    assert res_live["live_brn_fresh"] is True
    assert res_live["fast_crash_active"] is False
    assert res_live["degraded_mode"] is None
    assert res_live["effective_cluster_cap_pct"] == pytest.approx(res_base["effective_cluster_cap_pct"])
    assert res_live["effective_sigma_cap_pct"] == pytest.approx(res_base["effective_sigma_cap_pct"])

    preview = res_live["next_day_preview"]
    assert isinstance(preview, dict)
    assert preview["basis_price"] == pytest.approx(live_up_px)
    assert preview["locked_regime"] in OIL_REGIMES
    assert set(preview["probs"].keys()) == set(OIL_REGIMES)
    assert sum(preview["probs"].values()) == pytest.approx(100.0, abs=0.05)
    assert preview["oil_ma5"] > 0.0
    assert preview["oil_ma15"] > 0.0
    assert preview["ma_gap_pct"] == pytest.approx(
        round((preview["oil_ma5"] / preview["oil_ma15"] - 1.0) * 100.0, 2), abs=0.05
    )
    assert preview["preview_cluster_cap_pct"] > 0.0
    assert preview["preview_sigma_cap_pct"] > 0.0

    # Zero cache contamination: subsequent clean call matches baseline identically
    res_clean_again = router.evaluate_regime(rstore, trade_date=trade_date, symbol_ma_map=sym_ma)
    assert res_clean_again == res_base

    # 3. Off-hours stale live_brn (require_fresh_live_brn=False, age=3600s):
    #    no degraded_mode, no Fast Crash compression, yet live fields & next_day_preview still populated
    crash_px = round(ref_close * 0.94, 4)
    res_off_hours = router.evaluate_regime(
        rstore,
        trade_date=trade_date,
        symbol_ma_map=sym_ma,
        live_brn_price=crash_px,
        live_brn_age_sec=3600.0,
        require_fresh_live_brn=False,
    )
    assert res_off_hours["degraded_mode"] is None
    assert "live_brn" not in res_off_hours["degraded_sources"]
    assert res_off_hours["fast_crash_active"] is False
    assert res_off_hours["live_brn_fresh"] is False
    assert res_off_hours["live_brn_price"] == pytest.approx(crash_px)
    assert res_off_hours["live_brn_chg_pct"] == pytest.approx(-6.0, abs=0.02)
    assert res_off_hours["effective_cluster_cap_pct"] == pytest.approx(res_base["effective_cluster_cap_pct"])
    assert isinstance(res_off_hours["next_day_preview"], dict)
    assert res_off_hours["next_day_preview"]["basis_price"] == pytest.approx(crash_px)

    # 4. In-session stale live_brn (default require_fresh_live_brn=True, age=3600s) -> auxiliary_degraded
    res_in_session_stale = router.evaluate_regime(
        rstore,
        trade_date=trade_date,
        symbol_ma_map=sym_ma,
        live_brn_price=crash_px,
        live_brn_age_sec=3600.0,
    )
    assert res_in_session_stale["degraded_mode"] == "auxiliary_degraded"
    assert "live_brn" in res_in_session_stale["degraded_sources"]
    assert res_in_session_stale["fast_crash_active"] is False
    assert res_in_session_stale["live_brn_fresh"] is False

    # 5. Degraded oil_daily fallback includes consistent default live keys
    res_degraded = router.evaluate_regime(None, trade_date=trade_date)
    assert res_degraded["live_brn_price"] is None
    assert res_degraded["live_brn_chg_pct"] is None
    assert res_degraded["live_brn_fresh"] is False
    assert res_degraded["next_day_preview"] is None
    rstore.close()


def _seed_v62_all_macro_cache(
    rstore: SQLiteResearchStore,
    series_map: dict[str, list[list[Any]]],
) -> None:
    macro = V9MacroDataStore(rstore, allow_network=False)
    now_ts = 1_795_750_000.0
    with rstore._lock, rstore._connection:
        for key, data in series_map.items():
            rstore._connection.execute(
                """
                INSERT OR REPLACE INTO quant_macro_cache (cache_key, payload_json, updated_at)
                VALUES (?, ?, ?)
                """,
                (key, json.dumps(data), now_ts),
            )
    assert macro is not None


def _assert_v62_ci_invariants(res: dict[str, Any]) -> None:
    ci_map = res["ci_by_regime"]
    assert isinstance(ci_map, dict)
    assert set(ci_map.keys()) == set(OIL_REGIMES)
    for reg in OIL_REGIMES:
        ci = ci_map[reg]
        assert ci["mean_pct"] == pytest.approx(res["probs"][reg], abs=1e-6)
        assert (
            0.0
            <= ci["conformal_90_low_pct"]
            <= ci["ci_low_pct"]
            <= ci["mean_pct"]
            <= ci["ci_high_pct"]
            <= ci["conformal_90_high_pct"]
            <= 100.0
        )
        assert 0.0 <= ci["min_pct"] <= ci["max_pct"] <= 100.0


def test_v62_five_dimension_committee_ci_and_guardrails(tmp_path: Path) -> None:
    rstore = SQLiteResearchStore(tmp_path / "v62_committee.db")

    # --- Scenario 1: Bearish high-consensus resonance -> bear_high_conf_compress ---
    oil_bear = _build_relay_downtrend_closes()
    n = len(oil_bear)
    bear_series: dict[str, list[list[Any]]] = {
        "oil_daily_2y": _make_daily_rows("20260401", oil_bear),
        "ho_daily_2y": _make_daily_rows(
            "20260401", [c * 0.04 * (0.999 ** max(0, i - 130)) for i, c in enumerate(oil_bear)]
        ),
        "cl_daily_2y": _make_daily_rows("20260401", [c * 0.94 for c in oil_bear]),
        "ng_daily_2y": _make_daily_rows("20260401", [3.0 * (0.996 ** i) for i in range(n)]),
        "uso_daily_2y": _make_daily_rows(
            "20260401", [c * 0.85 * (0.998 ** max(0, i - 130)) for i, c in enumerate(oil_bear)]
        ),
        "bno_daily_2y": _make_daily_rows(
            "20260401", [c * 0.42 * (0.998 ** max(0, i - 130)) for i, c in enumerate(oil_bear)]
        ),
        "dbo_daily_2y": _make_daily_rows("20260401", [c * 0.20 for c in oil_bear]),
        "sc0_daily_all": _make_daily_rows(
            "20260401", [c * 6.5 * (0.998 ** max(0, i - 130)) for i, c in enumerate(oil_bear)]
        ),
        "xop_daily_2y": _make_daily_rows(
            "20260401", [c * 1.6 * (0.997 ** max(0, i - 130)) for i, c in enumerate(oil_bear)]
        ),
        "xle_daily_2y": _make_daily_rows(
            "20260401", [c * 1.0 * (0.998 ** max(0, i - 130)) for i, c in enumerate(oil_bear)]
        ),
        "spy_daily_2y": _make_daily_rows("20260401", [500.0 * (1.001 ** i) for i in range(n)]),
        "iwm_daily_2y": _make_daily_rows("20260401", [200.0 * (0.999 ** i) for i in range(n)]),
    }
    last_dt = datetime.strptime(bear_series["oil_daily_2y"][-1][0], "%Y%m%d") + timedelta(days=1)
    while last_dt.weekday() >= 5:
        last_dt += timedelta(days=1)
    trade_date = last_dt.strftime("%Y%m%d")

    _seed_v62_all_macro_cache(rstore, bear_series)
    router = OilRegimeRouter()
    live_px_bear = round(oil_bear[-1] * 0.995, 4)
    res_bear = router.evaluate_regime(
        rstore,
        trade_date=trade_date,
        live_brn_price=live_px_bear,
        live_brn_age_sec=15.0,
    )
    res_bear_v5 = router.evaluate_regime(
        rstore,
        trade_date=trade_date,
        profile="oil_v5_legacy",
    )

    assert res_bear["active_dimensions"] == list(V62_DIMENSION_ORDER)
    assert res_bear["missing_dimensions"] == []
    assert len(res_bear["dimension_votes"]) == 5
    assert res_bear["degraded_mode"] is None
    assert res_bear["degraded_sources"] == []
    assert res_bear["locked_regime"] in ("中继下跌", "下降")
    assert res_bear["consensus_pct"] >= 74.5
    assert res_bear["consensus_band"] == "高置信共振"
    assert res_bear["ci_guard_action"] == "bear_high_conf_compress"
    assert res_bear["effective_cluster_cap_pct"] < res_bear_v5["effective_cluster_cap_pct"]
    assert res_bear["effective_sigma_cap_pct"] < res_bear_v5["effective_sigma_cap_pct"]
    _assert_v62_ci_invariants(res_bear)

    for vote in res_bear["dimension_votes"]:
        assert vote["id"] in V62_DIMENSION_SHORT_LABELS
        assert vote["label"] == V62_DIMENSION_SHORT_LABELS[vote["id"]]
        assert vote["vote_regime"] in OIL_REGIMES
        assert 0.0 < vote["weight_pct"] <= 100.0
        assert 0.0 <= vote["vote_prob_pct"] <= 100.0
        assert sum(vote["probs"].values()) == pytest.approx(100.0, abs=0.05)

    preview = res_bear["next_day_preview"]
    assert isinstance(preview, dict)
    assert isinstance(preview["consensus_pct"], float)
    assert preview["consensus_band"] in ("高置信共振", "中等共识", "多空分歧")
    assert isinstance(preview["ci_guard_action"], str)
    assert isinstance(preview["locked_ci_pct"], list) and len(preview["locked_ci_pct"]) == 2
    assert 0.0 <= preview["locked_ci_pct"][0] <= preview["locked_ci_pct"][1] <= 100.0

    # Fast Crash overlay preserves CI ordering invariants without polluting cache
    res_crash = router.evaluate_regime(
        rstore,
        trade_date=trade_date,
        live_brn_price=round(oil_bear[-1] * 0.93, 4),
        live_brn_age_sec=10.0,
    )
    assert res_crash["fast_crash_active"] is True
    _assert_v62_ci_invariants(res_crash)

    # --- Scenario 2: Bearish divergence with strong term_physical support -> bear_divergence_support ---
    div_series = dict(bear_series)
    div_series["ho_daily_2y"] = _make_daily_rows("20260401", [3.0 * (1.003 ** i) for i in range(n)])
    div_series["cl_daily_2y"] = _make_daily_rows(
        "20260401", [c * (0.92 + 0.08 * (max(0, i - 135) / 25.0)) for i, c in enumerate(oil_bear)]
    )
    div_series["uso_daily_2y"] = _make_daily_rows("20260401", [60.0 * (1.005 ** i) for i in range(n)])
    div_series["bno_daily_2y"] = _make_daily_rows("20260401", [30.0 * (1.005 ** i) for i in range(n)])
    div_series["dbo_daily_2y"] = _make_daily_rows("20260401", [15.0 * (1.0005 ** i) for i in range(n)])
    div_series["sc0_daily_all"] = _make_daily_rows("20260401", [500.0 * (1.004 ** i) for i in range(n)])
    div_series["xop_daily_2y"] = _make_daily_rows("20260401", [120.0 * (1.005 ** i) for i in range(n)])
    div_series["xle_daily_2y"] = _make_daily_rows("20260401", [80.0 * (1.004 ** i) for i in range(n)])
    div_series["spy_daily_2y"] = _make_daily_rows("20260401", [500.0 * (0.9995 ** i) for i in range(n)])
    div_series["iwm_daily_2y"] = _make_daily_rows("20260401", [200.0 * (1.002 ** i) for i in range(n)])
    _seed_v62_all_macro_cache(rstore, div_series)
    router_div = OilRegimeRouter()
    res_div = router_div.evaluate_regime(rstore, trade_date=trade_date)
    res_div_v5 = router_div.evaluate_regime(rstore, trade_date=trade_date, profile="oil_v5_legacy")

    assert len(res_div["active_dimensions"]) == 5
    assert res_div["missing_dimensions"] == []
    assert res_div["locked_regime"] in ("中继下跌", "下降")
    assert res_div["consensus_pct"] < 51.9
    assert res_div["consensus_band"] == "多空分歧"
    assert res_div["ci_guard_action"] == "bear_divergence_support"
    assert res_div["effective_cluster_cap_pct"] > res_div_v5["effective_cluster_cap_pct"]
    assert res_div["effective_cluster_cap_pct"] <= 56.0 + 1e-6
    assert res_div["symbol_multipliers"]["160723"] > res_div_v5["symbol_multipliers"]["160723"]
    assert res_div["symbol_multipliers"]["501018"] > res_div_v5["symbol_multipliers"]["501018"]
    _assert_v62_ci_invariants(res_div)

    # --- Scenario 3: Bullish high-consensus resonance -> bull_high_conf_unlock ---
    oil_bull = _build_steady_uptrend_closes()
    bull_series: dict[str, list[list[Any]]] = {
        "oil_daily_2y": _make_daily_rows("20260401", oil_bull),
        "ho_daily_2y": _make_daily_rows(
            "20260401", [c * 0.04 * (1.001 ** max(0, i - 130)) for i, c in enumerate(oil_bull)]
        ),
        "cl_daily_2y": _make_daily_rows(
            "20260401", [c * (0.93 + 0.03 * (max(0, i - 130) / 30.0)) for i, c in enumerate(oil_bull)]
        ),
        "ng_daily_2y": _make_daily_rows("20260401", [2.5 * (1.003 ** i) for i in range(n)]),
        "uso_daily_2y": _make_daily_rows(
            "20260401", [c * 0.85 * (1.002 ** max(0, i - 130)) for i, c in enumerate(oil_bull)]
        ),
        "bno_daily_2y": _make_daily_rows(
            "20260401", [c * 0.42 * (1.002 ** max(0, i - 130)) for i, c in enumerate(oil_bull)]
        ),
        "dbo_daily_2y": _make_daily_rows("20260401", [c * 0.20 for c in oil_bull]),
        "sc0_daily_all": _make_daily_rows(
            "20260401", [c * 6.5 * (1.002 ** max(0, i - 130)) for i, c in enumerate(oil_bull)]
        ),
        "xop_daily_2y": _make_daily_rows(
            "20260401", [c * 1.8 * (1.002 ** max(0, i - 130)) for i, c in enumerate(oil_bull)]
        ),
        "xle_daily_2y": _make_daily_rows(
            "20260401", [c * 1.1 * (1.002 ** max(0, i - 130)) for i, c in enumerate(oil_bull)]
        ),
        "spy_daily_2y": _make_daily_rows("20260401", [500.0 * (1.0005 ** i) for i in range(n)]),
        "iwm_daily_2y": _make_daily_rows("20260401", [200.0 * (1.002 ** i) for i in range(n)]),
    }
    _seed_v62_all_macro_cache(rstore, bull_series)
    router_bull = OilRegimeRouter()
    sym_ma_bull = {"160723": (2.10, 2.00), "161129": (1.90, 1.80), "501018": (1.60, 1.50)}
    res_bull = router_bull.evaluate_regime(rstore, trade_date=trade_date, symbol_ma_map=sym_ma_bull)
    res_bull_v5 = router_bull.evaluate_regime(
        rstore, trade_date=trade_date, symbol_ma_map=sym_ma_bull, profile="oil_v5_legacy"
    )

    assert len(res_bull["active_dimensions"]) == 5
    assert res_bull["missing_dimensions"] == []
    assert res_bull["consensus_pct"] >= 74.5
    assert res_bull["consensus_band"] == "高置信共振"
    assert res_bull["ci_guard_action"] == "bull_high_conf_unlock"
    assert res_bull["effective_cluster_cap_pct"] >= res_bull_v5["effective_cluster_cap_pct"]
    assert res_bull["effective_sigma_cap_pct"] >= res_bull_v5["effective_sigma_cap_pct"]
    _assert_v62_ci_invariants(res_bull)

    # --- Tier-2 degraded result exposes consistent V6.2 fallback keys ---
    res_deg = router_bull.evaluate_regime(None, trade_date=trade_date)
    assert res_deg["consensus_pct"] is None
    assert res_deg["consensus_band"] is None
    assert res_deg["ci_guard_action"] == "neutral"
    assert res_deg["active_dimensions"] == []
    assert res_deg["missing_dimensions"] == list(V62_DIMENSION_ORDER)
    assert res_deg["ci_by_regime"] is None
    assert res_deg["dimension_votes"] == []
    rstore.close()
