from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any

import pytest

from level2_service.quant_rules import (
    VALID_CROSS_MARKET_ANCHORS,
    backfill_intraday_history,
    evaluate_quant_strategy,
)
from level2_service.research_store import SQLiteResearchStore
from level2_service.v9_plus_engine import (
    SHANGHAI_TZ,
    V9MacroDataStore,
    _MEMORY_MACRO_CACHE,
    build_v9_plus_days,
    evaluate_v9_plus_live_decision,
    evaluate_v9_plus_live_indicators,
    get_live_spot_telemetry,
    resolve_effective_close_weight,
)
from level2_service.v9_sp_oilgas_etf import (
    apply_sp_oilgas_etf_intraday_overlays,
    fetch_live_sp_oilgas_etf_spot,
    resolve_sp_oilgas_sell_tier,
    v9_sp_oilgas_etf_close_weight,
)


def _minute_str(idx: int) -> str:
    mv = (570 + idx + 1) if idx < 120 else (780 + (idx - 120) + 1)
    return f"{mv // 60:02d}:{mv % 60:02d}"


def _seed_symbol_day(
    rstore: SQLiteResearchStore,
    symbol: str,
    name: str,
    trade_date: str,
    prices: list[float],
    *,
    pre_close: float | None,
    market: str = "20",
    vols: list[float] | None = None,
) -> None:
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            """INSERT OR REPLACE INTO research_days(
                   symbol,trade_date,name,market,source,pre_close,quote_price,collected_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                symbol,
                trade_date,
                name,
                market,
                "TEST",
                f"{pre_close:.3f}" if pre_close is not None else None,
                f"{prices[-1]:.3f}",
                f"{trade_date}T15:00:00+08:00",
            ),
        )
        rows = []
        run_sum = 0.0
        for idx, p in enumerate(prices):
            run_sum += p
            avg = run_sum / (idx + 1)
            vol = vols[idx] if vols is not None else (120000.0 if idx % 15 != 0 else 280000.0)
            rows.append((symbol, trade_date, _minute_str(idx), p, round(avg, 4), vol, None, None, f"{trade_date}T15:00:00+08:00"))
        rstore._connection.executemany(
            """INSERT OR REPLACE INTO research_points(
                   symbol,trade_date,minute,price,average_price,volume,large_order_amount,large_order_net,captured_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            rows,
        )


def _seed_brent_day(rstore: SQLiteResearchStore, trade_date: str, base_px: float, drift: float = 0.25) -> None:
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            """INSERT OR REPLACE INTO research_days(
                   symbol,trade_date,name,market,source,pre_close,quote_price,collected_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            ("BRN0Y", trade_date, "布伦特连续", "OIL", "TEST", f"{base_px:.2f}", f"{base_px + drift:.2f}", f"{trade_date}T15:00:00+08:00"),
        )
        rows = []
        for idx in range(240):
            p = round(base_px + drift * (idx / 239.0), 3)
            rows.append(("BRN0Y", trade_date, _minute_str(idx), p, p, 100.0, None, None, f"{trade_date}T15:00:00+08:00"))
        rstore._connection.executemany(
            """INSERT OR REPLACE INTO research_points(
                   symbol,trade_date,minute,price,average_price,volume,large_order_amount,large_order_net,captured_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            rows,
        )


def test_fetch_live_sp_oilgas_etf_spot_13_in_1_bundle_and_cache(tmp_path: Path, monkeypatch) -> None:
    assert "sp_oilgas_etf" in VALID_CROSS_MARKET_ANCHORS
    rstore = SQLiteResearchStore(tmp_path / "research.db")
    macro = V9MacroDataStore(rstore, allow_network=True)
    _MEMORY_MACRO_CACHE.clear()

    sina_bundle = (
        'var hq_str_USDCNY="10:15:00,7.2080,7.2070,7.2090,1,7.2000,7.2100,7.2050,7.2085,在岸人民币,0,0,0,x,x,x,x,2026-11-26";\n'
        'var hq_str_fx_susdcnh="10:15:00,7.2190,7.2180,7.2200,1,7.2100,7.2210,7.2150,7.2195,离岸人民币,0,0,0,x,x,x,x,2026-11-26";\n'
        'var hq_str_hf_OIL="73.20,0.45,73.15,73.25,73.50,72.60,10:15:00,72.80,72.90,1000,0,0,2026-11-26,布伦特";\n'
        'var hq_str_hf_CL="69.90,0.40,69.85,69.95,70.10,69.20,10:15:00,69.50,69.60,1000,0,0,2026-11-26,WTI";\n'
        'var hq_str_hf_HO="2.24,0.35,2.23,2.25,2.28,2.20,10:15:00,2.21,2.22,500,0,0,2026-11-26,HO";\n'
        'var hq_str_hf_NG="3.38,0.60,3.37,3.39,3.42,3.31,10:15:00,3.33,3.35,500,0,0,2026-11-26,NG";\n'
        'var hq_str_hf_ES="5985.5,0.20,5984.0,5986.0,5995.0,5970.0,10:15:00,5973.0,5975.0,500,0,0,2026-11-26,ES";\n'
        'var hq_str_nf_SC0="原油连续,101500,536.0,541.0,533.0,535.0,537.5,537.8,537.6,536.5,535.0,10,12,1000,2000,INE,SC,2026-11-26";\n'
        'var hq_str_gb_xop="XOP,138.40,1.45,2026-11-25 16:00:00,1.98,136.80,139.10,136.00,145,110,1000,500,138.40";\n'
        'var hq_str_gb_iwm="IWM,239.50,0.85,2026-11-25 16:00:00,2.02,238.00,240.10,237.20,245,190,1000,500,239.50";\n'
        'var hq_str_gb_xle="XLE,92.10,1.10,2026-11-25 16:00:00,1.00,91.40,92.50,90.90,98,80,1000,500,92.10";\n'
        'var hq_str_gb_spy="SPY,598.20,0.55,2026-11-25 16:00:00,3.27,596.00,599.00,594.50,605,500,1000,500,598.20";\n'
        'var hq_str_sz162411="华宝油气LOF,1.062,1.055,1.071,1.075,1.052,1.070,1.071,10000,1071000,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,2026-11-26,10:15:00";\n'
    )
    called_urls: list[str] = []

    def _fake_fetch(url: str, **_kwargs: Any) -> str:
        called_urls.append(url)
        return sina_bundle

    monkeypatch.setattr(macro, "fetch_text", _fake_fetch)
    spot = fetch_live_sp_oilgas_etf_spot(macro)
    assert spot is not None
    assert len(called_urls) == 1
    for expected_code in ("USDCNY", "fx_susdcnh", "hf_OIL", "hf_CL", "hf_HO", "hf_NG", "hf_ES", "nf_SC0", "gb_xop", "gb_iwm", "gb_xle", "gb_spy", "sz162411"):
        assert expected_code in called_urls[0]
    assert spot["fx_px"] == pytest.approx(7.2195)
    assert spot["fx_cnh_px"] == pytest.approx(7.2195)
    assert spot["fx_cny_px"] == pytest.approx(7.2085)
    assert spot["brn_px"] == pytest.approx(73.20)
    assert spot["cl_px"] == pytest.approx(69.90)
    assert spot["ho_px"] == pytest.approx(2.24)
    assert spot["ng_px"] == pytest.approx(3.38)
    assert spot["es_px"] == pytest.approx(5985.5)
    assert spot["sc_px"] == pytest.approx(537.6)
    assert spot["xop_close"] == pytest.approx(138.40)
    assert isinstance(spot["peer_162411"], dict) and spot["peer_162411"]["price"] == pytest.approx(1.071)

    tel = get_live_spot_telemetry().get("live_spot_sp_oilgas_etf_bundle") or {}
    assert (
        tel.get("last_ok_at") is not None
        and tel.get("consecutive_failures") == 0
        and tel.get("non_null_fields") == 12
        and tel.get("total_fields") == 12
    )

    # Second call hits 8s TTL memory cache even offline
    offline_macro = V9MacroDataStore(rstore, allow_network=False)
    cached = fetch_live_sp_oilgas_etf_spot(offline_macro)
    assert cached is not None and cached["brn_px"] == pytest.approx(73.20)
    _MEMORY_MACRO_CACHE.clear()
    rstore.close()


def test_v9_sp_oilgas_etf_close_weight_5_layers() -> None:
    # Layer 1: extreme quota bubble >= 5.6% (weak/below MA5) -> 0%
    w, lbl = v9_sp_oilgas_etf_close_weight(
        {"ma5": 2.10, "ma15": 2.00, "max_bpos_20d": 0.55, "max_xpos_20d": 0.55, "xop_ov": 0.1},
        {"price": 2.09, "brn_pos": 0.55, "xop_pos": 0.55, "prem": 5.8, "raw_prem": 5.8, "rel_prem": 2.1, "prem162": 0.1, "sp_fx": 0.5},
    )
    assert w == 0.0 and "泡沫避险" in lbl

    # Layer 2 bull-trap fade: Nov bear MA5<MA15, above MA5 but closed below VWAP / lof_chg<=0 & pm>=2.0 -> 0% (11/16 top)
    w_trap, trap_lbl = v9_sp_oilgas_etf_close_weight(
        {"ma5": 2.05, "ma15": 2.18, "max_bpos_20d": 0.65, "max_xpos_20d": 0.65, "xop_ov": 0.8},
        {"price": 2.07, "brn_pos": 0.55, "xop_pos": 0.55, "prem": 2.1, "raw_prem": 2.1, "rel_prem": 0.4, "lof_chg": -0.45, "vwap_intra": 2.09, "px_dd_hi": 1.60},
    )
    assert w_trap == 0.0 and "补跌" in trap_lbl

    # Layer 2/3: Nov crash bear shield (MA5 < MA15, below MA15 & MA5, 20d peak >= 75%) -> 0%
    w_nov, _ = v9_sp_oilgas_etf_close_weight(
        {"ma5": 2.10, "ma15": 2.18, "max_bpos_20d": 0.86, "max_xpos_20d": 0.78, "xop_ov": -0.4, "xop_bull": False},
        {"price": 2.07, "brn_pos": 0.56, "xop_pos": 0.54, "prem": 0.4, "raw_prem": 0.4, "rel_prem": 0.2, "lof_chg": -0.8, "vwap_intra": 2.08},
    )
    assert w_nov == 0.0

    # Layer 2 override: V-bottom contrarian debubbled + deep oversold + US/Asia turning -> 0.95 & preserved
    day_vbot = {
        "ma5": 2.10,
        "ma15": 2.18,
        "max_bpos_20d": 0.86,
        "max_xpos_20d": 0.78,
        "xop_ov": 0.70,
        "iwm_ov": 0.30,
        "xop_5d": -6.2,
        "cross_market_anchor": "sp_oilgas_etf",
    }
    bar_vbot = {
        "price": 2.06,
        "brn_pos": 0.56,
        "xop_pos": 0.55,
        "prem": 1.10,
        "raw_prem": 1.10,
        "rel_prem": -0.25,
        "prem162": -0.50,
        "dd_ma15": -5.5,
        "dd15_pct": -9.5,
        "daytime_lead_pct": 0.15,
        "sc_intra": 0.3,
        "lof_chg": 0.8,
        "vwap_intra": 2.05,
        "sp_fx": 0.10,
    }
    w_con, lbl_con = v9_sp_oilgas_etf_close_weight(day_vbot, bar_vbot)
    assert w_con == 0.95 and "逆势" in lbl_con
    eff_w, _ = resolve_effective_close_weight(v9_sp_oilgas_etf_close_weight, day_vbot, bar_vbot, 2, 0.8)
    assert eff_w == 0.95

    # Layer 4: Main rally trend above MA5 & MA15 with moderate prem -> 100%
    w_hi, _ = v9_sp_oilgas_etf_close_weight(
        {"ma5": 2.18, "ma15": 2.10, "max_bpos_20d": 0.78, "max_xpos_20d": 0.80, "xop_bull": True, "xop_ov": 1.4},
        {"price": 2.22, "brn_pos": 0.75, "xop_pos": 0.79, "prem": 2.2, "raw_prem": 2.2, "rel_prem": 0.2, "daytime_lead_pct": 0.30, "lof_chg": 1.6, "vwap_intra": 2.20},
    )
    assert w_hi == 1.00

    # Layer 5: 2Y bottom value zone -> 90%
    w_bot, _ = v9_sp_oilgas_etf_close_weight(
        {"ma5": 1.92, "ma15": 1.95, "max_bpos_20d": 0.42, "max_xpos_20d": 0.40, "xop_bull": False, "xop_ov": -0.1},
        {"price": 1.94, "brn_pos": 0.35, "xop_pos": 0.38, "prem": 0.8, "raw_prem": 0.8, "rel_prem": 0.3, "lof_chg": 0.2, "vwap_intra": 1.93},
    )
    assert w_bot == 0.90


def test_sp_oilgas_etf_55d_warmup_and_t0_backtest_live_parity(tmp_path: Path) -> None:
    rstore = SQLiteResearchStore(tmp_path / "research.db")
    _ = V9MacroDataStore(rstore, allow_network=False)
    _MEMORY_MACRO_CACHE.clear()

    base_dt = datetime(2026, 10, 19, 9, 30, tzinfo=SHANGHAI_TZ)
    trade_dates: list[str] = []
    cur = base_dt
    while len(trade_dates) < 25:
        if cur.weekday() < 5:
            trade_dates.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)

    oil_rows: list[list[Any]] = []
    xop_rows: list[list[Any]] = []
    xle_rows: list[list[Any]] = []
    spy_rows: list[list[Any]] = []
    iwm_rows: list[list[Any]] = []
    cl_rows: list[list[Any]] = []
    ho_rows: list[list[Any]] = []
    ng_rows: list[list[Any]] = []
    sc_daily_rows: list[list[Any]] = []
    nav_513: dict[str, float] = {}
    nav_162: dict[str, float] = {}
    fx_ts: list[int] = []
    fx_px: list[float] = []
    fut_ts: list[int] = []
    cl_m_px: list[float] = []
    ho_m_px: list[float] = []
    ng_m_px: list[float] = []
    es_m_px: list[float] = []
    sc_m_ts: list[int] = []
    sc_m_px: list[float] = []

    prev_513 = 2.050
    prev_162 = 1.020
    for d_idx, td in enumerate(trade_dates):
        # First 10 days high oil, next 10 days selloff + final dip recovery day to trigger T+0 & quantiles
        brn_base = 88.0 - (d_idx - 8) * 1.4 if d_idx >= 8 else 86.0 + d_idx * 0.4
        xop_px = 148.0 - (d_idx - 8) * 1.8 if d_idx >= 8 else 145.0 + d_idx * 0.5
        ts_us = int(datetime.strptime(td, "%Y%m%d").replace(tzinfo=SHANGHAI_TZ, hour=23).timestamp())
        oil_rows.append([td, ts_us, round(brn_base, 2)])
        xop_rows.append([td, ts_us, round(xop_px, 2)])
        xle_rows.append([td, ts_us, round(xop_px * 0.65, 2)])
        spy_rows.append([td, ts_us, round(580.0 + d_idx * 0.4, 2)])
        iwm_rows.append([td, ts_us, round(230.0 + d_idx * 0.3, 2)])
        cl_rows.append([td, ts_us, round(brn_base - 3.0, 2)])
        ho_rows.append([td, ts_us, round((brn_base - 3.0) * 0.032, 3)])
        ng_rows.append([td, ts_us, round(3.10 + (d_idx % 4) * 0.05, 3)])
        sc_daily_rows.append([td, ts_us, round(530.0 + (brn_base - 80.0) * 3.0, 2), round(529.0 + (brn_base - 80.0) * 3.0, 2)])
        nav_513[td] = round(2.00 + d_idx * 0.002 if d_idx < 8 else 2.02 - (d_idx - 8) * 0.012, 4)
        nav_162[td] = round(1.00 + d_idx * 0.001 if d_idx < 8 else 1.01 - (d_idx - 8) * 0.006, 4)

        pts_513 = []
        pts_162 = []
        for m_i in range(240):
            dt_m = datetime.strptime(f"{td} {_minute_str(m_i)}", "%Y%m%d %H:%M").replace(tzinfo=SHANGHAI_TZ)
            ts_m = int(dt_m.timestamp())
            if m_i % 10 == 0:
                fx_ts.append(ts_m)
                fx_px.append(7.21)
                fut_ts.append(ts_m)
                cl_m_px.append(round(brn_base - 3.0, 2))
                ho_m_px.append(round((brn_base - 3.0) * 0.032, 3))
                ng_m_px.append(3.20)
                es_m_px.append(5900.0)
                sc_m_ts.append(ts_m)
                sc_m_px.append(round(530.0 + (brn_base - 80.0) * 3.0, 2))
            if d_idx == len(trade_dates) - 1 and 20 <= m_i <= 80:
                wave = -0.035 * ((40 - abs(m_i - 50)) / 40.0)
            else:
                wave = 0.006 * ((m_i - 120) / 120.0)
            p513 = round(max(1.50, prev_513 * (1.0 + wave)), 3)
            p162 = round(max(0.80, prev_162 * (1.0 + wave * 0.4)), 3)
            pts_513.append(p513)
            pts_162.append(p162)

        _seed_symbol_day(rstore, "513350", "标普油气ETF富国", td, pts_513, pre_close=prev_513, market="20")
        _seed_symbol_day(rstore, "162411", "华宝油气LOF", td, pts_162, pre_close=prev_162, market="36")
        _seed_brent_day(rstore, td, brn_base, drift=0.35 if d_idx < 8 else -0.25)
        prev_513 = pts_513[-1]
        prev_162 = pts_162[-1]

    # Seed long 2Y oil/xop hi/lo range prior to window
    old_ts = int(datetime(2025, 6, 1, 23, 0, tzinfo=SHANGHAI_TZ).timestamp())
    oil_rows_full = [["20250601", old_ts, 60.0], ["20250602", old_ts + 86400, 96.0], *oil_rows]
    xop_rows_full = [["20250601", old_ts, 110.0], ["20250602", old_ts + 86400, 155.0], *xop_rows]

    now_t = float(int(datetime.now().timestamp()))
    with rstore._lock, rstore._connection:
        for k, v in (
            ("oil_daily_2y", oil_rows_full),
            ("xop_daily_2y", xop_rows_full),
            ("xle_daily_2y", xle_rows),
            ("spy_daily_2y", spy_rows),
            ("iwm_daily_2y", iwm_rows),
            ("cl_daily_2y", cl_rows),
            ("ho_daily_2y", ho_rows),
            ("ng_daily_2y", ng_rows),
            ("sc0_daily_all", sc_daily_rows),
            ("sc0_min_series", {"ts": sc_m_ts, "px": sc_m_px}),
            ("fut_1m_cl", {"ts": fut_ts, "px": cl_m_px}),
            ("fut_1m_ho", {"ts": fut_ts, "px": ho_m_px}),
            ("fut_1m_ng", {"ts": fut_ts, "px": ng_m_px}),
            ("fut_1m_es", {"ts": fut_ts, "px": es_m_px}),
            ("fx_usdcny_series", {"ts": fx_ts, "px": fx_px}),
            ("fx_usdcnh_series", {"ts": fx_ts, "px": fx_px}),
            ("nav_513350", nav_513),
            ("nav_162411", nav_162),
        ):
            rstore._connection.execute(
                "INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)",
                (k, json.dumps(v), now_t),
            )

    days_full, _ = build_v9_plus_days(
        rstore,
        symbol="513350",
        start_date=trade_dates[0],
        end_date=trade_dates[-1],
        allow_network=False,
        cross_market_anchor="sp_oilgas_etf",
    )
    days_win, _ = build_v9_plus_days(
        rstore,
        symbol="513350",
        start_date=trade_dates[-5],
        end_date=trade_dates[-1],
        warmup_days=55,
        allow_network=False,
        cross_market_anchor="sp_oilgas_etf",
    )
    assert len(days_full) == 25 and len(days_win) == 5
    # Causal 55d warmup reproduces identical 20d quantiles, 5d prem, MA5/15 and close_wt
    assert days_full[-1]["max_bpos_20d"] == pytest.approx(days_win[-1]["max_bpos_20d"])
    assert days_full[-1]["max_xpos_20d"] == pytest.approx(days_win[-1]["max_xpos_20d"])
    assert days_full[-1]["eq_prem_5d"] == pytest.approx(days_win[-1]["eq_prem_5d"])
    assert days_full[-1]["ma5"] == pytest.approx(days_win[-1]["ma5"])
    assert days_full[-1]["ma15"] == pytest.approx(days_win[-1]["ma15"])

    # Confirm T+0 intraday washout dip-scalp (09:36..14:15) and two-tranche split tier
    _, _, scalp_ov, ov_meta = apply_sp_oilgas_etf_intraday_overlays(
        {"ma5": 2.00, "ma15": 1.95, "xop_ov": 0.4},
        {
            "time": "10:15",
            "price": 2.02,
            "prem": 1.50,
            "raw_prem": 1.50,
            "eff_sell_pm": 0.10,
            "eff_buy_pm": -0.30,
            "sp_fx": -0.25,
            "sp_xop": -0.20,
            "peer_gap_intra": -0.25,
            "peer_m3": 0.02,
            "chg162": -0.10,
            "daytime_lead_pct": 0.10,
            "sc_intra": 0.10,
            "vwap_intra": 2.03,
            "m1": 0.02,
            "macd10": -0.0010,
            "dmc10": 0.00025,
            "dc10": False,
            "gc10": True,
            "px_dd_hi": 0.50,
            "sp_dd_hi": 0.25,
            "vdev": -0.40,
        },
        s_sig=False,
        b_sig=False,
        scalp_ok=False,
        close_wt=0.80,
        sc=0.90,
        has_base_shares=True,
    )
    assert scalp_ov is True and ov_meta.get("scalp_frac_override") == 0.85

    split_frac, split_lbl = resolve_sp_oilgas_sell_tier(
        {"ma5": 2.10, "xop_ov": 1.4},
        {"price": 2.15, "prem": 1.2, "raw_prem": 1.2, "eff_sell_pm": 0.4, "sp_fx": 0.35, "sp_intra": 0.25, "peer_gap_intra": 0.10, "daytime_lead_pct": 0.4},
        1.0,
    )
    assert split_frac == 0.50 and "分批高抛" in split_lbl

    rule_spec = {
        "id": "ai-513350-v9-sp-oilgas-etf",
        "name": "513350专享 · V9+ 标普油气ETF全复制XOP+T+0双向量化",
        "symbols": ["513350", "BRN0Y"],
        "target_symbol": "513350",
        "macd_settings": {"short": 10, "long": 20, "signal": 5},
        "position_sizing": {
            "mode": "base_t0_close",
            "engine_profile": "v9_plus_aggressive",
            "cross_market_anchor": "sp_oilgas_etf",
            "capital_cap": 50000.0,
            "round_trip_fee_pct": 0.01,
            "scale_factor": 0.90,
            "cooldown_minutes": 3,
            "max_daily_entries": 10,
            "close_flatten_time": "14:53",
            "min_retained_shares": 100,
            "allow_same_day_turnaround": True,
        },
        "buy_rule": {"title": "T+0折价正T", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "lte", "value": -0.20}]},
        "sell_rule": {"title": "T+0溢价反T", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "gte", "value": 0.60}]},
    }
    eval_res = evaluate_quant_strategy(
        rstore,
        rule_spec,
        symbol="513350",
        start_date=trade_dates[5],
        end_date=trade_dates[-1],
        capital_cap=50000.0,
    )
    assert eval_res["coverage"]["evaluated_days"] == 20
    assert eval_res["capital_sim"]["ending_equity"] > 0

    live_dec = evaluate_v9_plus_live_decision(
        rstore,
        symbol="513350",
        trade_date=trade_dates[-1],
        now_hhmm="14:30",
        cap_limit=50000.0,
        rule_spec=rule_spec,
        pos_sizing=rule_spec["position_sizing"],
        today_signals=[],
        rolling_shares=5000,
        allow_network=False,
    )
    assert live_dec is not None
    assert live_dec["diagnostics"]["anchor_mode"] == "sp_oilgas_etf"
    assert live_dec["diagnostics"]["min_retained_shares"] == 100

    live_ind = evaluate_v9_plus_live_indicators(
        rstore,
        symbol="513350",
        trade_date=trade_dates[-1],
        now_hhmm="14:30",
        cap_limit=50000.0,
        rule_spec=rule_spec,
        pos_sizing=rule_spec["position_sizing"],
        today_signals=[],
        rolling_shares=5000,
        allow_network=False,
    )
    assert live_ind is not None
    assert live_ind["diagnostics"]["anchor_mode"] == "sp_oilgas_etf"
    card_keys = {c["key"] for c in live_ind["market_indicators"]}
    assert {"sp_t0_spread", "quota_prem", "xop_cme_anchor", "peer_162411", "quantile_close"}.issubset(card_keys)
    _MEMORY_MACRO_CACHE.clear()
    rstore.close()


def test_backfill_intraday_history_populates_missing_pre_close_without_10jqka_bars(tmp_path: Path) -> None:
    rstore = SQLiteResearchStore(tmp_path / "research.db")
    # Pre-seed 20261120 (known close 2.100) and 20261123 (pre_close NULL, close 2.140, complete 240 pts)
    _seed_symbol_day(rstore, "513350", "标普油气ETF富国", "20261120", [2.100] * 240, pre_close=2.080, market="20")
    _seed_symbol_day(rstore, "513350", "标普油气ETF富国", "20261123", [2.140] * 240, pre_close=None, market="20")
    _seed_symbol_day(rstore, "162411", "华宝油气LOF", "20261124", [1.060] * 240, pre_close=1.050, market="36")
    _seed_brent_day(rstore, "20261124", 73.0)

    def _empty_kline(_url: str, _ref: str, _timeout: float) -> str:
        raise RuntimeError("10jqka daily kline missing for 513350")

    def _fake_tdx(_sym: str, missing_tds: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for td in missing_tds:
            if td == "20261124":
                out[td] = {
                    "pre_close": "2.140",
                    "points": [{"minute": _minute_str(i), "price": 2.155, "average_price": 2.150, "volume": 10000} for i in range(240)],
                }
        return out

    backfill_intraday_history(
        rstore,
        "513350",
        start_date="20261120",
        end_date="20261124",
        fetch_kline_fn=_empty_kline,
        fetch_tdx_fn=_fake_tdx,
        fetch_line_funds_fn=lambda *_args, **_kw: "",
    )
    with rstore._lock:
        rows = rstore._connection.execute(
            "SELECT trade_date, pre_close FROM research_days WHERE symbol='513350' ORDER BY trade_date ASC"
        ).fetchall()
    by_td = {str(r[0]): r[1] for r in rows}
    assert by_td["20261120"] == "2.080"
    assert by_td["20261123"] == "2.100"
    assert by_td["20261124"] == "2.140"
    rstore.close()
