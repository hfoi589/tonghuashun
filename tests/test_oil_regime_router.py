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
