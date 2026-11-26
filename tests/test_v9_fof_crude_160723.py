from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from level2_service.quant_rules import (
    VALID_CROSS_MARKET_ANCHORS,
    SQLiteQuantRuleStore,
    normalize_position_sizing,
)
from level2_service.research_store import SQLiteResearchStore
from level2_service.v9_fof_crude import (
    apply_fof_crude_intraday_overlays,
    fetch_live_fof_crude_spot,
    fof_adjusted_buy_bar,
    load_dbo_daily_rows,
    load_merged_fx_cnh_series,
    v9_fof_crude_close_weight,
)
from level2_service.v9_plus_engine import (
    V9MacroDataStore,
    build_v9_plus_days,
    evaluate_v9_plus_backtest,
    evaluate_v9_plus_live_decision,
    evaluate_v9_plus_live_indicators,
    resolve_effective_close_weight,
)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _day_mins(prices: list[float] | float, start_hhmm: str = "09:31") -> list[tuple[str, float]]:
    if isinstance(prices, (int, float)):
        px_list = [float(prices)] * 240
    else:
        px_list = [float(p) for p in prices]
    out: list[tuple[str, float]] = []
    for i, px in enumerate(px_list):
        m = (570 + i + 1) if i < 120 else (780 + (i - 120) + 1)
        out.append((f"{m // 60:02d}:{m % 60:02d}", px))
    return out


def _seed_symbol_bars(
    store: SQLiteResearchStore,
    symbol: str,
    trade_date: str,
    pre_close: float,
    minute_prices: list[tuple[str, float]],
    *,
    name: str = "嘉实原油LOF",
    market: str = "SZ",
    minute_vols: list[int] | None = None,
) -> None:
    close_p = minute_prices[-1][1]
    open_p = minute_prices[0][1]
    high_p = max(p for _, p in minute_prices)
    low_p = min(p for _, p in minute_prices)
    chg_pct = f"{((close_p - pre_close) / pre_close * 100.0):+.2f}%"
    captured_at = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}T15:00:00+08:00"
    total_vol = sum(minute_vols) if minute_vols else 100 * len(minute_prices)
    with store._lock, store._connection:
        store._connection.execute(
            """INSERT INTO research_days(
                   symbol, trade_date, name, market, source, pre_close,
                   quote_price, quote_change_percent, quote_open, quote_high,
                   quote_low, quote_volume, collected_at
               ) VALUES(?, ?, ?, ?, 'test', ?, ?, ?, ?, ?, ?, ?, ?)
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
                str(total_vol),
                captured_at,
            ),
        )
        store._connection.executemany(
            """INSERT INTO research_points(
                   symbol, trade_date, minute, price, average_price, volume, captured_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol, trade_date, minute) DO UPDATE SET
                   price=excluded.price,
                   average_price=excluded.average_price,
                   volume=excluded.volume""",
            [
                (
                    symbol,
                    trade_date,
                    hhmm,
                    f"{px:.3f}",
                    f"{px:.3f}",
                    str(minute_vols[idx] if minute_vols and idx < len(minute_vols) else 100),
                    captured_at,
                )
                for idx, (hhmm, px) in enumerate(minute_prices)
            ],
        )


def test_fof_crude_position_sizing_normalization_preserves_siblings() -> None:
    assert "fof_crude_core" in VALID_CROSS_MARKET_ANCHORS
    ps = normalize_position_sizing(
        {
            "cross_market_anchor": "fof_crude_core",
            "min_retained_shares": 100,
            "mode": "dynamic_brent_schedule",
        }
    )
    assert ps["cross_market_anchor"] == "fof_crude_core"
    assert ps["min_retained_shares"] == 100

    # Sibling defaults and anchors must remain completely untouched
    default_ps = normalize_position_sizing({})
    assert "cross_market_anchor" not in default_ps
    assert "cross_market_anchor" not in normalize_position_sizing({"cross_market_anchor": "brent"})
    assert normalize_position_sizing({"cross_market_anchor": "uso_bno_sc_peer"})["cross_market_anchor"] == "uso_bno_sc_peer"
    assert normalize_position_sizing({"cross_market_anchor": "xop_us_ep"})["cross_market_anchor"] == "xop_us_ep"


def test_fof_crude_macro_loaders_and_offline_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    rstore = SQLiteResearchStore(tmp_path / "fof_macro_offline.db")
    macro = V9MacroDataStore(rstore, allow_network=False)

    # Empty offline cache falls back safely without network exceptions
    assert load_dbo_daily_rows(macro) == []
    assert load_merged_fx_cnh_series(macro) == ([], [])
    assert fetch_live_fof_crude_spot(macro) is None

    # Populate onshore USDCNY only -> load_merged_fx_cnh_series falls back to macro.load_fx_series()
    ts_onshore = int(datetime(2026, 11, 2, 10, 0, tzinfo=SHANGHAI_TZ).timestamp())
    ts_offshore = int(datetime(2026, 11, 2, 22, 0, tzinfo=SHANGHAI_TZ).timestamp())
    now_ts = time.time()
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcny_series", json.dumps({"ts": [ts_onshore], "px": [7.10]}), now_ts),
        )
    ts_res, px_res = load_merged_fx_cnh_series(macro)
    assert ts_res == [ts_onshore]
    assert px_res == [7.10]

    # Add CNH + DBO cache -> merges chronologically & returns cached DBO rows
    dbo_rows = [["20261102", ts_offshore, 14.25]]
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("fx_usdcnh_series", json.dumps({"ts": [ts_offshore], "px": [7.14]}), now_ts),
        )
        rstore._connection.execute(
            "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
            ("dbo_daily_2y", json.dumps(dbo_rows), now_ts),
        )
    ts_merged, px_merged = load_merged_fx_cnh_series(macro)
    assert ts_merged == [ts_onshore, ts_offshore]
    assert px_merged == [7.10, 7.14]
    assert load_dbo_daily_rows(macro) == dbo_rows
    rstore.close()


def test_v9_fof_crude_close_weight_and_contrarian_matrix() -> None:
    # 1. Aug broken-trend bubble exit (ma5 < ma15 and fof_prem >= 5.0% -> 0.0)
    wt_aug_exit, _ = v9_fof_crude_close_weight(
        {
            "ma5": 1.180,
            "ma15": 1.210,
            "fof_bull": False,
            "fof_ov": 0.1,
            "max_bpos_20d": 0.45,
        },
        {"brn_pos": 0.35, "prem": 5.4, "fof_prem": 5.2, "price": 1.170, "dd15_pct": -4.0, "sc_vs_prev": -0.20},
        0,
    )
    assert wt_aug_exit == 0.0

    # 2. July bull hold (ma5 >= ma15, price >= ma5, brn_pos < 0.72 -> high weight >= 0.85 when FOF prem <= 0.50%, else 0.80)
    wt_july_hold, _ = v9_fof_crude_close_weight(
        {
            "ma5": 1.250,
            "ma15": 1.180,
            "fof_bull": True,
            "fof_ov": 0.5,
            "max_bpos_20d": 0.65,
        },
        {"brn_pos": 0.55, "prem": 0.70, "fof_prem": 0.40, "price": 1.265, "dd15_pct": -1.2, "sc_vs_prev": 0.60},
        0,
    )
    assert wt_july_hold == 0.90

    # 3. Oct high-oil bull breakout (0.72 <= brn_pos < 0.90, above_ma5, ma5 >= ma15, fof_bull, fof_prem <= 0.80%)
    wt_oct_break, _ = v9_fof_crude_close_weight(
        {
            "ma5": 1.280,
            "ma15": 1.240,
            "fof_bull": True,
            "fof_ov": 0.4,
            "max_bpos_20d": 0.84,
        },
        {"brn_pos": 0.80, "prem": 1.10, "fof_prem": 0.65, "price": 1.295, "dd15_pct": -0.8, "sc_vs_prev": 0.40},
        0,
    )
    assert wt_oct_break in (0.85, 0.95)

    # 4. Nov 20261125-style crash contrarian overnight (nominal prem=+1.44%, FOF fof_prem=-1.00%, dd15=-13.6%, sc_vs_prev=+0.47%)
    nov_day = {
        "cross_market_anchor": "fof_crude_core",
        "ma5": 1.320,
        "ma15": 1.390,
        "fof_bull": False,
        "fof_ov": 0.2,
        "max_bpos_20d": 0.30,
    }
    nov_b1453 = {
        "brn_pos": 0.25,
        "prem": 1.44,
        "fof_prem": -1.00,
        "price": 1.245,
        "dd15_pct": -13.6,
        "sc_vs_prev": 0.47,
        "sc_intra": 0.15,
    }
    wt_nov_contra, tag_nov_contra = v9_fof_crude_close_weight(nov_day, nov_b1453, 2)
    assert wt_nov_contra == 0.90
    assert "逆势" in tag_nov_contra

    # resolve_effective_close_weight preserves "逆势" overnight even when pos == 2 nominal prem >= 1.20%
    eff_wt, eff_tag = resolve_effective_close_weight(
        v9_fof_crude_close_weight,
        nov_day,
        {**nov_b1453, "prem": 1.44, "fof_prem": -0.60},
        pos_mode=2,
        entry_prem=1.0,
    )
    assert eff_wt == 0.90
    assert "逆势" in eff_tag


def test_apply_fof_crude_intraday_overlays_knife_guard_and_contrarian_channels() -> None:
    # A. 0% below-MA5 afternoon knife block (13:30+, sc_intra <= -0.45, iopv_intra <= -0.15, peer_m3 < -0.02)
    knife_day = {"ma5": 1.300, "eq_prem_5d": 1.20}
    knife_bar = {
        "time": "13:45",
        "price": 1.270,
        "sc_intra": -0.60,
        "iopv_intra": -0.30,
        "peer_m3": -0.10,
        "peer_chg": -0.50,
        "peer_sp_intra": -0.20,
        "vol_ratio": 1.2,
        "bounce_from_lo_pct": 0.10,
        "dd15_pct": -3.0,
        "fof_prem": 1.50,
        "rel_prem": 0.30,
        "prem": 1.50,
        "sp_fx": -0.20,
    }
    _, b_out, scalp_out, meta_knife = apply_fof_crude_intraday_overlays(
        knife_day,
        knife_bar,
        s_sig=False,
        b_sig=True,
        scalp_ok=True,
        close_wt=0.0,
        sc=1.0,
        has_base_shares=False,
    )
    assert b_out is False
    assert scalp_out is False
    assert meta_knife["knife_blocked"] is True
    assert len(meta_knife["knife_reasons"]) == 4

    # B. Channel 1: c_peer_lag (peers bounce while 160723 lags)
    ch1_day = {"ma5": 1.300, "eq_prem_5d": 1.20}
    ch1_bar = {
        "time": "10:15",
        "price": 1.260,
        "sc_intra": 0.40,
        "iopv_intra": 0.30,
        "peer_m3": 0.12,
        "peer_chg": 0.85,
        "peer_gap": -0.40,
        "vol_ratio": 1.5,
        "bounce_from_lo_pct": 0.45,
        "dd15_pct": -6.0,
        "fof_prem": -0.40,
        "rel_prem": -1.60,
        "prem": 0.20,
        "sp_fx": -0.25,
        "dmc10": 0.00020,
        "m1": 0.05,
        "gc10": False,
    }
    _, b_ch1, scalp_ch1, meta_ch1 = apply_fof_crude_intraday_overlays(
        ch1_day,
        ch1_bar,
        s_sig=False,
        b_sig=False,
        scalp_ok=False,
        close_wt=0.0,
        sc=1.0,
        has_base_shares=False,
    )
    assert b_ch1 is False
    assert scalp_ch1 is True
    assert meta_ch1["contrarian_triggered"] is True
    assert "c_peer_lag" in meta_ch1["contrarian_channels"]
    assert meta_ch1["scalp_frac_override"] == 0.90

    # C. Channel 2: c_panic_vol (volume climax + deep drawdown + bounce off intraday low)
    ch2_day = {"ma5": 1.300, "eq_prem_5d": 1.00}
    ch2_bar = {
        "time": "11:05",
        "price": 1.225,
        "sc_intra": 0.10,
        "iopv_intra": 0.05,
        "peer_m3": 0.08,
        "peer_chg": 0.20,
        "peer_gap": -0.10,
        "vol_ratio": 4.2,
        "vdev": -0.60,
        "px_dd_hi": 0.55,
        "bounce_from_lo_pct": 0.75,
        "dd15_pct": -12.5,
        "fof_prem": -0.50,
        "rel_prem": -1.50,
        "prem": 0.50,
        "sp_fx": -0.55,
        "dmc10": 0.00025,
        "m1": 0.02,
        "gc10": False,
    }
    _, _, scalp_ch2, meta_ch2 = apply_fof_crude_intraday_overlays(
        ch2_day,
        ch2_bar,
        s_sig=False,
        b_sig=False,
        scalp_ok=False,
        close_wt=0.0,
        sc=1.0,
        has_base_shares=False,
    )
    assert scalp_ch2 is True
    assert "c_panic_vol" in meta_ch2["contrarian_channels"]

    # D. Channel 3: c_debubble_rel (enabled only when not has_base_shares; gated off when has_base_shares=True)
    ch3_day = {"ma5": 1.300, "eq_prem_5d": 3.00}
    ch3_bar = {
        "time": "11:25",
        "price": 1.255,
        "sc_intra": 0.15,
        "iopv_intra": 0.10,
        "peer_m3": 0.04,
        "peer_chg": 0.20,
        "peer_gap": -0.15,
        "vol_ratio": 1.1,
        "vdev": -0.35,
        "px_dd_hi": 0.20,
        "bounce_from_lo_pct": 0.40,
        "dd15_pct": -9.0,
        "fof_prem": -0.20,
        "rel_prem": -3.20,
        "prem": 0.90,
        "sp_fx": -0.60,
        "dmc10": 0.00025,
        "m1": 0.02,
        "gc10": False,
    }
    _, _, scalp_with_base, meta_with_base = apply_fof_crude_intraday_overlays(
        ch3_day,
        ch3_bar,
        s_sig=False,
        b_sig=False,
        scalp_ok=False,
        close_wt=0.0,
        sc=1.0,
        has_base_shares=True,
    )
    assert scalp_with_base is False
    assert "c_debubble_rel" not in meta_with_base["contrarian_channels"]

    _, _, scalp_no_base, meta_no_base = apply_fof_crude_intraday_overlays(
        ch3_day,
        ch3_bar,
        s_sig=False,
        b_sig=False,
        scalp_ok=False,
        close_wt=0.0,
        sc=1.0,
        has_base_shares=False,
    )
    assert scalp_no_base is True
    assert "c_debubble_rel" in meta_no_base["contrarian_channels"]

    # E. fof_adjusted_buy_bar swaps buy-side prem evaluation to fof_prem and widens sp_fx by peer lag
    adj = fof_adjusted_buy_bar(
        {"eq_prem_5d": 1.50},
        {"prem": 1.44, "fof_prem": -1.00, "rel_prem": -0.40, "sp_fx": -0.40, "peer_gap": -0.60, "price": 1.245},
    )
    assert adj["prem"] == -1.00
    assert abs(adj["sp_fx"] - (-0.55)) < 1e-9


def test_fof_crude_end_to_end_backtest_live_and_100_share_floor(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    db_path = tmp_path / "v9_fof_160723.db"
    rstore = SQLiteResearchStore(db_path)
    qstore = SQLiteQuantRuleStore(db_path)
    V9MacroDataStore(rstore, allow_network=False)
    now_ts = time.time()

    oil_rows = [
        ["20260701", int(datetime(2026, 7, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 70.0],
        ["20260801", int(datetime(2026, 8, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 100.0],
    ]
    uso_rows = [["20260701", int(datetime(2026, 7, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 80.0]]
    bno_rows = [["20260701", int(datetime(2026, 7, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 34.0]]
    dbo_rows = [["20260701", int(datetime(2026, 7, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 16.0]]
    cl_rows = [["20260701", int(datetime(2026, 7, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 78.0]]
    ho_rows = [["20260701", int(datetime(2026, 7, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 2.40]]
    sc0_daily: list[list[float | str]] = []
    sc0_mins_ts: list[int] = []
    sc0_mins_px: list[float] = []
    nav_payload: dict[str, float] = {}

    # Seed 16 warmup + active days (Nov 02..Nov 17, 2026) with steady sell-off followed by contrarian setup
    for idx in range(16):
        us_d = (datetime(2026, 11, 1, tzinfo=SHANGHAI_TZ) + timedelta(days=idx)).strftime("%Y%m%d")
        a_dt = datetime(2026, 11, 2, tzinfo=SHANGHAI_TZ) + timedelta(days=idx)
        a_d = a_dt.strftime("%Y%m%d")
        causal_ts = int((datetime.strptime(us_d, "%Y%m%d").replace(tzinfo=SHANGHAI_TZ) + timedelta(days=1, hours=6)).timestamp())
        oil_rows.append([us_d, causal_ts, 75.0])
        uso_rows.append([us_d, causal_ts, 80.0])
        bno_rows.append([us_d, causal_ts, 34.0])
        dbo_rows.append([us_d, causal_ts, 16.0])
        cl_rows.append([us_d, causal_ts, 74.0])
        ho_rows.append([us_d, causal_ts, 2.35])
        sc0_daily.append([a_d, 560.0])
        nav_payload[us_d] = 1.400 - idx * 0.012
        px_day = round(1.420 - idx * 0.012, 3)
        _seed_symbol_bars(rstore, "BRN0Y", a_d, 75.0, [("09:30", 75.0), ("14:53", 75.0), ("15:00", 75.0)], name="布伦特", market="ICE")
        _seed_symbol_bars(rstore, "160723", a_d, round(px_day + 0.01, 3), _day_mins(px_day), name="嘉实原油")
        _seed_symbol_bars(rstore, "501018", a_d, 1.300, _day_mins(1.300), name="南方原油", market="SH")
        sc_open_ts = int(datetime.strptime(a_d + " 09:31", "%Y%m%d %H:%M").replace(tzinfo=SHANGHAI_TZ).timestamp())
        sc0_mins_ts.extend([sc_open_ts, sc_open_ts + 18000])
        sc0_mins_px.extend([560.0, 560.0])

    # Contiguous day 17 (td_target=20261118) mapped to US T-1 20261117:
    # BRN T-1 flat while FOF basket (USO/BNO/DBO) rallies +3.0% -> nominal prem positive, FOF prem negative!
    # Meanwhile 501018 peer surges +1.4% intraday and 160723 spikes volume on dip & bounce.
    td_target = "20261118"
    nav_payload["20261116"] = 1.240
    oil_rows.append(["20261116", int(datetime(2026, 11, 17, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 75.0])
    oil_rows.append(["20261117", int(datetime(2026, 11, 18, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 75.0])
    uso_rows.append(["20261116", int(datetime(2026, 11, 17, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 80.0])
    uso_rows.append(["20261117", int(datetime(2026, 11, 18, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 82.4])
    bno_rows.append(["20261116", int(datetime(2026, 11, 17, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 34.0])
    bno_rows.append(["20261117", int(datetime(2026, 11, 18, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 35.0])
    dbo_rows.append(["20261116", int(datetime(2026, 11, 17, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 16.0])
    dbo_rows.append(["20261117", int(datetime(2026, 11, 18, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 16.5])
    cl_rows.append(["20261117", int(datetime(2026, 11, 18, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 75.0])
    ho_rows.append(["20261117", int(datetime(2026, 11, 18, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 2.40])
    sc0_daily.append([td_target, 565.0])
    ts_target_start = int(datetime(2026, 11, 18, 9, 31, tzinfo=SHANGHAI_TZ).timestamp())
    sc0_mins_ts.extend([ts_target_start, ts_target_start + 19380])
    sc0_mins_px.extend([562.0, 566.0])

    px_target = [1.245] * 30 + [1.220] * 30 + [1.235] * 180
    vols_target = [100] * 55 + [2500] * 10 + [150] * 175
    px_peer = [1.300] * 30 + [1.318] * 210
    _seed_symbol_bars(rstore, "BRN0Y", td_target, 75.0, [("09:30", 75.0), ("14:53", 75.0), ("15:00", 75.2)], name="布伦特", market="ICE")
    _seed_symbol_bars(rstore, "160723", td_target, 1.245, _day_mins(px_target), name="嘉实原油", minute_vols=vols_target)
    _seed_symbol_bars(rstore, "501018", td_target, 1.300, _day_mins(px_peer), name="南方原油", market="SH")

    # Bubble day 18 (td_bubble=20261119): ma5 < ma15, price stays below ma15, yet T-2 NAV crash leaves >+8% premium -> 0% at 14:53
    td_bubble = "20261119"
    nav_payload["20261117"] = 1.150
    oil_rows.append(["20261118", int(datetime(2026, 11, 19, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 75.0])
    uso_rows.append(["20261118", int(datetime(2026, 11, 19, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 82.4])
    bno_rows.append(["20261118", int(datetime(2026, 11, 19, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 35.0])
    dbo_rows.append(["20261118", int(datetime(2026, 11, 19, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 16.5])
    cl_rows.append(["20261118", int(datetime(2026, 11, 19, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 75.0])
    ho_rows.append(["20261118", int(datetime(2026, 11, 19, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 2.40])
    sc0_daily.append([td_bubble, 560.0])
    ts_bubble_start = int(datetime(2026, 11, 19, 9, 31, tzinfo=SHANGHAI_TZ).timestamp())
    sc0_mins_ts.extend([ts_bubble_start, ts_bubble_start + 19380])
    sc0_mins_px.extend([560.0, 560.0])
    _seed_symbol_bars(rstore, "BRN0Y", td_bubble, 75.2, [("09:30", 75.0), ("14:53", 75.0), ("15:00", 75.0)], name="布伦特", market="ICE")
    _seed_symbol_bars(rstore, "160723", td_bubble, 1.235, _day_mins(1.260), name="嘉实原油")
    _seed_symbol_bars(rstore, "501018", td_bubble, 1.318, _day_mins(1.318), name="南方原油", market="SH")

    with rstore._lock, rstore._connection:
        for k, v in (
            ("nav_160723", nav_payload),
            ("oil_daily_2y", oil_rows),
            ("uso_daily_2y", uso_rows),
            ("bno_daily_2y", bno_rows),
            ("dbo_daily_2y", dbo_rows),
            ("cl_daily_2y", cl_rows),
            ("ho_daily_2y", ho_rows),
            ("sc0_daily_all", sc0_daily),
            ("sc0_min_series", {"ts": sc0_mins_ts, "px": sc0_mins_px}),
            ("fx_usdcny_series", {"ts": [int(datetime(2026, 10, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [7.10]}),
            ("fx_usdcnh_series", {"ts": [int(datetime(2026, 10, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [7.10]}),
        ):
            rstore._connection.execute("INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)", (k, json.dumps(v), now_ts))

    ps_fof = normalize_position_sizing(
        {
            "cross_market_anchor": "fof_crude_core",
            "min_retained_shares": 100,
        }
    )
    saved = qstore.upsert_rule(
        {
            "id": "ai-160723-v9-plus-aggressive",
            "name": "V9+ 进取版 · FOF Crude Causal",
            "source": "ai",
            "symbols": ["160723", "BRN0Y"],
            "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0002},
            "buy_rule": {"title": "折价正T", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "lte", "value": -0.20}]},
            "sell_rule": {"title": "溢价反T", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "gte", "value": 0.60}]},
            "position_sizing": ps_fof,
            "eval_criteria": {"start_date": "20261102", "round_trip_fee_pct": 0.01, "capital_cap": 100000.0},
        },
        research_store=rstore,
    )
    assert saved["position_sizing"]["cross_market_anchor"] == "fof_crude_core"
    assert saved["position_sizing"]["min_retained_shares"] == 100

    days, _ = build_v9_plus_days(
        rstore,
        symbol="160723",
        start_date="20261102",
        allow_network=False,
        warmup_days=10,
        cross_market_anchor="fof_crude_core",
    )
    target_days = [d for d in days if d["td"] == td_target]
    assert len(target_days) == 1
    assert target_days[0].get("cross_market_anchor") == "fof_crude_core"
    assert float(target_days[0]["fof_ov"]) > 2.0
    last_bar = target_days[0]["bars"][-1]
    assert float(last_bar["fof_iopv"]) > float(last_bar["iopv"])
    assert float(last_bar["fof_prem"]) < float(last_bar["prem"])
    assert float(last_bar["sc_vs_prev"]) != 0.0
    assert last_bar["peer_chg"] is not None

    bt = evaluate_v9_plus_backtest(
        rstore,
        saved,
        saved["eval_criteria"],
        ps_fof,
        symbol="160723",
        cleaned_end=None,
        allow_network=False,
    )
    assert bt["coverage"]["evaluated_days"] == 18
    assert "capital_sim" in bt

    # Bubble exit at 14:53 forces 0% weight -> sells 15,600 shares and strictly leaves min_retained_shares=100
    dec_sell = evaluate_v9_plus_live_decision(
        rstore,
        symbol="160723",
        trade_date=td_bubble,
        now_hhmm="14:53",
        cap_limit=100000.0,
        rule_spec=saved,
        pos_sizing=ps_fof,
        today_signals=[],
        rolling_shares=15700,
        allow_network=False,
        broker_available_cash=50000.0,
        expected_ledger_shares=15700,
    )
    assert dec_sell["diagnostics"]["cross_market_anchor"] == "fof_crude_core"
    assert dec_sell["diagnostics"]["min_retained_shares"] == 100
    assert dec_sell["diagnostics"]["close_target_weight_pct"] == 0.0
    assert dec_sell["diagnostics"]["close_target_shares"] == 100
    assert "fof_prem_pct" in dec_sell["diagnostics"]
    cr = dec_sell["close_rebalance"]
    assert cr is not None
    assert cr["side"] == "sell"
    assert cr["qty_override"] == 15600

    ind = evaluate_v9_plus_live_indicators(
        rstore,
        symbol="160723",
        trade_date=td_target,
        now_hhmm="10:30",
        cap_limit=100000.0,
        rule_spec=saved,
        pos_sizing=ps_fof,
        today_signals=[],
        rolling_shares=100,
        allow_network=False,
    )
    assert ind is not None
    assert ind["diagnostics"]["cross_market_anchor"] == "fof_crude_core"
    assert ind["diagnostics"]["min_retained_shares"] == 100
    card_keys = [c.get("key") for c in (ind.get("market_indicators") or [])]
    assert "fof_prem" in card_keys and "sc_peer_fof" in card_keys
    cond_ids = [c.get("id") for c in (ind["buy"].get("conditions") or [])]
    assert "v9-buy-fof-contrarian" in cond_ids

    # Clear all external FOF/SC/peer caches -> offline fallback still runs cleanly without crashing
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "DELETE FROM quant_macro_cache WHERE cache_key IN ('uso_daily_2y', 'bno_daily_2y', 'dbo_daily_2y', 'cl_daily_2y', 'ho_daily_2y', 'sc0_daily_all', 'sc0_min_series', 'fx_usdcnh_series')"
        )
        rstore._connection.execute("DELETE FROM research_days WHERE symbol='501018'")
        rstore._connection.execute("DELETE FROM research_points WHERE symbol='501018'")
    bt_fallback = evaluate_v9_plus_backtest(
        rstore,
        saved,
        saved["eval_criteria"],
        ps_fof,
        symbol="160723",
        cleaned_end=None,
        allow_network=False,
    )
    assert bt_fallback["coverage"]["evaluated_days"] == 18
    rstore.close()
