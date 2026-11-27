from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from level2_service.auto_trade import AutoTradeEngine, SQLiteAutoTradeStore
from level2_service.quant_rules import normalize_position_sizing
from level2_service.research_store import SQLiteResearchStore
from level2_service.v9_fof_t0_pulse import (
    apply_fof_t0_pulse_intraday_overlays,
    should_exit_fof_t0_long,
)
from level2_service.v9_plus_engine import (
    _apply_v9_antislip_intraday_gates,
    _apply_v9_macd_anchor,
    _has_morning_init_base_signal,
    apply_v9_close_weight_tuning,
    evaluate_v9_plus_backtest,
    evaluate_v9_plus_live_decision,
    should_enter_intraday_dip_scalp,
    v9_dual_oil_peer_close_weight,
)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _minute_str(idx: int) -> str:
    if idx < 120:
        dt = datetime(2026, 11, 2, 9, 31) + timedelta(minutes=idx)
    else:
        dt = datetime(2026, 11, 2, 13, 1) + timedelta(minutes=idx - 120)
    return dt.strftime("%H:%M")


def _seed_symbol_bars(
    rstore: SQLiteResearchStore,
    symbol: str,
    trade_date: str,
    pre_close: float,
    minute_prices: list[tuple[str, float]] | list[float],
    *,
    name: str,
    market: str = "SH",
) -> None:
    if minute_prices and isinstance(minute_prices[0], tuple):
        pairs = list(minute_prices)  # type: ignore[arg-type]
    else:
        pairs = [(_minute_str(i), float(px)) for i, px in enumerate(minute_prices)]  # type: ignore[arg-type]
    last_px = float(pairs[-1][1]) if pairs else float(pre_close)
    now_iso = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}T15:05:00+08:00"
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            """
            INSERT OR REPLACE INTO research_days (
                symbol, trade_date, name, market, source, pre_close, quote_price, collected_at
            ) VALUES (?, ?, ?, ?, 'test', ?, ?, ?)
            """,
            (symbol, trade_date, name, market, f"{pre_close:.4f}", f"{last_px:.4f}", now_iso),
        )
        run_sum = 0.0
        for idx, (tm, px) in enumerate(pairs):
            run_sum += float(px)
            avg_px = run_sum / (idx + 1)
            rstore._connection.execute(
                """
                INSERT OR REPLACE INTO research_points (
                    symbol, trade_date, minute, price, average_price, volume, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (symbol, trade_date, tm, f"{float(px):.4f}", f"{avg_px:.4f}", "200.0", now_iso),
            )


def _seed_brent_flat(rstore: SQLiteResearchStore, trade_date: str, brn_px: float = 75.0) -> None:
    pairs = [(_minute_str(i), brn_px) for i in range(240)]
    _seed_symbol_bars(rstore, "BRN0Y", trade_date, brn_px, pairs, name="Brent", market="US")


def _scalp_trigger_bar() -> dict[str, Any]:
    return {
        "time": "10:30",
        "price": 1.18,
        "vdev": -0.60,
        "px_dd_hi": 0.70,
        "sp_dd_hi": 0.45,
        "prem": 0.20,
        "gc10": True,
        "macd10": -0.001,
        "dmc10": 0.0004,
        "min_grn12": -0.0012,
        "brn_m5": 0.02,
        "m1": 0.02,
        "sp_fx": -0.30,
        "sp_intra": -0.40,
        "brn_pos": 0.50,
    }


def test_apply_v9_macd_anchor_attaches_v9_tuning_without_anchor() -> None:
    days = [{"td": "20261110", "pre": 1.2, "bars": [{"price": 1.2, "macd10": 0.001}]}]
    tuning = {"disable_scalp": True, "fof_t0_min_tp_pct": 0.15}
    _apply_v9_macd_anchor(days, tuning)
    assert days[0]["v9_tuning"] == tuning
    assert days[0]["bars"][0]["macd10"] == pytest.approx(0.001)


def test_disable_scalp_blocks_dip_scalp_and_fof_t0_overlay() -> None:
    bar = _scalp_trigger_bar()
    ok_default, lbl_default, frac_default = should_enter_intraday_dip_scalp(bar, 1.0, v9_tuning={})
    assert ok_default is True
    assert frac_default > 0.0 and lbl_default

    ok_disabled, lbl_disabled, frac_disabled = should_enter_intraday_dip_scalp(
        bar, 1.0, v9_tuning={"disable_scalp": True}
    )
    assert (ok_disabled, lbl_disabled, frac_disabled) == (False, "", 0.0)

    # Also verify reading disable_scalp via day["v9_tuning"]
    ok_day_disabled, _, _ = should_enter_intraday_dip_scalp(
        bar, 1.0, day={"v9_tuning": {"disable_scalp": True}}
    )
    assert ok_day_disabled is False

    # FOF T+0 overlay: c_quota_dip_t0 triggers by default, suppressed when disable_scalp=True
    day_base = {"ma5": 1.35, "ma15": 1.30, "fof_ov": 2.4, "xop_ov": 1.0}
    bar_t0 = {
        "time": "10:15",
        "price": 1.32,
        "prem": 2.60,
        "fof_prem": 0.50,
        "vdev": -0.50,
        "px_dd_hi": 0.70,
        "sp_dd_hi": 0.60,
        "spi_from_hi": 0.30,
        "sp_intra": -0.70,
        "sp_fx": -0.35,
        "peer_gap": -0.35,
        "peer_m3": 0.10,
        "p162_m3": 0.10,
        "sc_intra": 0.30,
        "cl_intra": 0.20,
        "brn_pos": 0.55,
        "brn_m5": 0.05,
        "gc10": True,
        "dc10": False,
        "macd10": -0.001,
        "dmc10": 0.0004,
        "m1": 0.05,
    }
    _, _, scalp_on, meta_on = apply_fof_t0_pulse_intraday_overlays(
        day_base, bar_t0, s_sig=False, b_sig=False, scalp_ok=False, close_wt=0.80, sc=1.0
    )
    assert scalp_on is True
    assert "c_quota_dip_t0" in (meta_on.get("contrarian_channels") or [])

    day_off = {**day_base, "v9_tuning": {"disable_scalp": True}}
    _, _, scalp_off, meta_off = apply_fof_t0_pulse_intraday_overlays(
        day_off, bar_t0, s_sig=False, b_sig=False, scalp_ok=True, close_wt=0.80, sc=1.0
    )
    assert scalp_off is False
    assert "c_quota_dip_t0" not in (meta_off.get("contrarian_channels") or [])


def test_fof_t0_min_tp_pct_configurable_and_preserves_stop_loss() -> None:
    day_default = {"ma5": 1.35, "ma15": 1.30}
    day_tuned = {"ma5": 1.35, "ma15": 1.30, "v9_tuning": {"fof_t0_min_tp_pct": 0.15}}

    # Bar triggers should_exit_long_strict via sp_fx >= 0.35 & dmc10 <= 0
    def _exit_bar(price: float, time_str: str = "11:00") -> dict[str, Any]:
        return {
            "time": time_str,
            "price": price,
            "sp_fx": 0.45,
            "sp_intra": 0.40,
            "prem": 0.30,
            "vdev": 0.30,
            "dmc10": -0.0003,
            "macd10": 0.001,
            "dc10": True,
            "m1": -0.05,
            "m3": -0.08,
            "max_red12": 0.001,
        }

    entry_px = 1.000
    # +0.10% gain: default (0.06%) exits; tuned (0.15%) suppresses
    b_010 = _exit_bar(1.001)
    ex_def, lbl_def = should_exit_fof_t0_long(
        day_default, b_010, b_buy=b_010, entry_px=entry_px, entry_sp=-0.40, entry_prem=0.10, s_sig=True, close_wt=0.80, is_scalp=False
    )
    assert ex_def is True and "止损" not in lbl_def

    ex_tuned_low, _ = should_exit_fof_t0_long(
        day_tuned, b_010, b_buy=b_010, entry_px=entry_px, entry_sp=-0.40, entry_prem=0.10, s_sig=True, close_wt=0.80, is_scalp=False
    )
    assert ex_tuned_low is False

    # +0.20% gain: exceeds 0.15% pad -> exits
    b_020 = _exit_bar(1.002)
    ex_tuned_hi, _ = should_exit_fof_t0_long(
        day_tuned, b_020, b_buy=b_020, entry_px=entry_px, entry_sp=-0.40, entry_prem=0.10, s_sig=True, close_wt=0.80, is_scalp=False
    )
    assert ex_tuned_hi is True

    # Protective stop loss in bear regime (gain = -0.60%) MUST NOT be blocked by fof_t0_min_tp_pct
    day_bear_tuned = {"ma5": 1.05, "ma15": 1.15, "v9_tuning": {"fof_t0_min_tp_pct": 0.15}}
    b_stop = _exit_bar(0.994)
    ex_stop, lbl_stop = should_exit_fof_t0_long(
        day_bear_tuned, b_stop, b_buy=b_stop, entry_px=entry_px, entry_sp=-0.40, entry_prem=0.10, s_sig=False, close_wt=0.0, is_scalp=False
    )
    assert ex_stop is True and "止损" in lbl_stop

    # Scalp exit path also respects fof_t0_min_tp_pct before 14:40
    ex_scalp_suppressed, _ = should_exit_fof_t0_long(
        day_tuned, b_010, b_buy=b_010, entry_px=entry_px, entry_sp=-0.40, entry_prem=0.10, s_sig=True, close_wt=0.80, is_scalp=True
    )
    assert ex_scalp_suppressed is False


def test_close_weight_tuning_unconfirmed_caps_and_rebound_taper() -> None:
    def _dummy_base(_day: Any, _b: Any, _pos: int = 0) -> tuple[float, str]:
        return 0.85, "基准满配(85%)"

    # 1. unconfirmed_ma15_cap_wt
    fn_cap = apply_v9_close_weight_tuning(_dummy_base, {"unconfirmed_ma15_cap_wt": 0.60})
    wt_bear, lbl_bear = fn_cap({"ma5": 1.18, "ma15": 1.22}, {"price": 1.19, "prem": 0.0}, 0)
    assert wt_bear == pytest.approx(0.60)
    assert "·未金叉渐进限仓" in lbl_bear

    wt_bull, lbl_bull = fn_cap({"ma5": 1.25, "ma15": 1.22}, {"price": 1.26, "prem": 0.0}, 0)
    assert wt_bull == pytest.approx(0.85)
    assert "未金叉渐进限仓" not in lbl_bull

    # 2. dual_prem_unconf_cap_wt works on v9_dual_oil_peer_close_weight (161129) without bear_high_prem_flat_pct
    fn_161129 = apply_v9_close_weight_tuning(
        v9_dual_oil_peer_close_weight,
        {"dual_prem_unconf_cap_wt": 0.50, "dual_prem_unconf_thresh": 0.45},
    )
    day_161129 = {
        "ma5": 1.48,
        "ma15": 1.52,
        "max_bpos_20d": 0.55,
        "uso_bull": True,
        "bno_bull": True,
        "dual_bull": True,
        "dual_ov": 0.8,
        "sc_bull": True,
    }
    bar_161129_fake_disc = {
        "price": 1.53,  # pr >= ma15 -> non_bear_ma15=True, base gives 0.85 or 1.0
        "prem": -0.40,
        "dual_prem": 0.60,
        "rel_prem": 0.10,
        "brn_pos": 0.50,
        "sp_fx": 0.0,
    }
    base_wt_161129, _ = v9_dual_oil_peer_close_weight(day_161129, bar_161129_fake_disc, 0)
    assert base_wt_161129 > 0.50
    tuned_wt_161129, _ = fn_161129(day_161129, bar_161129_fake_disc, 0)
    assert tuned_wt_161129 == pytest.approx(0.50)

    bar_161129_real_disc = {**bar_161129_fake_disc, "dual_prem": 0.20}
    tuned_wt_real, _ = fn_161129(day_161129, bar_161129_real_disc, 0)
    assert tuned_wt_real == pytest.approx(base_wt_161129)

    # 3. rebound_taper_wt (501018) triggered by lo10_prev rebound >= 4.0% OR price >= ma15 * 0.996
    fn_rebound = apply_v9_close_weight_tuning(
        _dummy_base,
        {"rebound_taper_wt": 0.55, "rebound_taper_pct": 4.0},
    )
    # Trigger A: +4.5% above lo10_prev while still well below ma15
    wt_rb_a, lbl_rb_a = fn_rebound(
        {"ma5": 1.10, "ma15": 1.25, "lo10_prev": 1.00},
        {"price": 1.045, "prem": 0.0},
        0,
    )
    assert wt_rb_a == pytest.approx(0.55)
    assert "·反弹阶梯退坡" in lbl_rb_a

    # Trigger B: near ma15 resistance (price >= ma15 * 0.996) even with small rebound from lo10_prev
    wt_rb_b, lbl_rb_b = fn_rebound(
        {"ma5": 1.20, "ma15": 1.25, "lo10_prev": 1.22},
        {"price": 1.246, "prem": 0.0},
        0,
    )
    assert wt_rb_b == pytest.approx(0.55)
    assert "·反弹阶梯退坡" in lbl_rb_b

    # Non-trigger: rebound < 4.0% and price < ma15 * 0.996
    wt_rb_no, lbl_rb_no = fn_rebound(
        {"ma5": 1.10, "ma15": 1.25, "lo10_prev": 1.00},
        {"price": 1.02, "prem": 0.0},
        0,
    )
    assert wt_rb_no == pytest.approx(0.85)
    assert "反弹阶梯退坡" not in lbl_rb_no


def test_antislip_intraday_gates_rev_t_min_sp_fx_and_sub_1yuan_pure_overnight() -> None:
    bar_mid = {"price": 1.35, "sp_fx": 0.42}
    # rev_t_min_sp_fx blocks weak reverse-T when bear_carry_active=False
    b1, s1, sc1 = _apply_v9_antislip_intraday_gates(
        bar_mid,
        b_sig=True,
        s_sig_short=True,
        scalp_ok=True,
        v9_tuning={"rev_t_min_sp_fx": 0.65},
        bear_carry_active=False,
    )
    assert (b1, s1, sc1) == (True, False, True)

    # Bear carry active exempts rev_t_min_sp_fx
    b2, s2, sc2 = _apply_v9_antislip_intraday_gates(
        bar_mid,
        b_sig=True,
        s_sig_short=True,
        scalp_ok=True,
        v9_tuning={"rev_t_min_sp_fx": 0.65},
        bear_carry_active=True,
    )
    assert (b2, s2, sc2) == (True, True, True)

    # Strong sp_fx >= rev_t_min_sp_fx passes
    b3, s3, sc3 = _apply_v9_antislip_intraday_gates(
        {"price": 1.35, "sp_fx": 0.72},
        b_sig=False,
        s_sig_short=True,
        scalp_ok=False,
        v9_tuning={"rev_t_min_sp_fx": 0.65},
        bear_carry_active=False,
    )
    assert (b3, s3, sc3) == (False, True, False)

    # sub_1yuan_pure_overnight disables all intraday entries below threshold (default 1.20)
    b4, s4, sc4 = _apply_v9_antislip_intraday_gates(
        {"price": 0.884, "sp_fx": 1.20},
        b_sig=True,
        s_sig_short=True,
        scalp_ok=True,
        v9_tuning={"sub_1yuan_pure_overnight": True},
        bear_carry_active=False,
    )
    assert (b4, s4, sc4) == (False, False, False)

    # Above threshold (1.25 >= 1.20) passes normally
    b5, s5, sc5 = _apply_v9_antislip_intraday_gates(
        {"price": 1.25, "sp_fx": 1.20},
        b_sig=True,
        s_sig_short=True,
        scalp_ok=True,
        v9_tuning={"sub_1yuan_pure_overnight": True, "sub_1yuan_price_thresh": 1.20},
        bear_carry_active=False,
    )
    assert (b5, s5, sc5) == (True, True, True)


def test_backtest_sub_1yuan_pure_overnight_keeps_close_rebalance_only(tmp_path: Path) -> None:
    rstore = SQLiteResearchStore(tmp_path / "research.db")
    # Build 2 days at sub-1-yuan (~0.884) with big intraday swing that would normally trigger intraday T+0
    d1, d2 = "20261102", "20261103"
    _seed_brent_flat(rstore, d1, 75.0)
    _seed_brent_flat(rstore, d2, 75.0)
    _seed_symbol_bars(rstore, "162411", d1, 0.880, [0.882] * 240, name="华宝油气", market="SZ")

    # Day 2 has sharp dip then sharp spike, then settles at 0.884
    day2_px = [0.884] * 240
    for i in range(20, 45):
        day2_px[i] = 0.855  # sharp dip -> positive-T trigger if not blocked
    for i in range(60, 85):
        day2_px[i] = 0.915  # sharp spike -> reverse-T trigger if not blocked
    _seed_symbol_bars(rstore, "162411", d2, 0.882, day2_px, name="华宝油气", market="SZ")

    rule_spec = {
        "id": "test-162411-sub1",
        "name": "162411 Sub-1-Yuan Pure Overnight Test",
        "symbols": ["162411", "BRN0Y"],
        "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0002},
    }
    criteria = {"start_date": d1, "round_trip_fee_pct": 0.01, "capital_cap": 100000.0}
    ps_pure = {
        "mode": "cash_pool_rolling",
        "engine_profile": "v9_plus_aggressive",
        "close_flatten_time": "14:53",
        "v9_tuning": {"sub_1yuan_pure_overnight": True, "sub_1yuan_price_thresh": 1.20},
    }
    res_pure = evaluate_v9_plus_backtest(
        rstore,
        rule_spec,
        criteria,
        ps_pure,
        symbol="162411",
        cleaned_end=d2,
        allow_network=False,
    )
    # Zero paired intraday T+0 trades, while initial/close overnight position is still managed
    assert res_pure["trade_sim"]["paired_trades"] == 0
    assert res_pure["capital_sim"]["ending_shares"] > 0

    # Control: without sub_1yuan_pure_overnight, the sharp swing triggers intraday T+0 trade(s)
    ps_ctrl = {**ps_pure, "v9_tuning": {}}
    res_ctrl = evaluate_v9_plus_backtest(
        rstore,
        rule_spec,
        criteria,
        ps_ctrl,
        symbol="162411",
        cleaned_end=d2,
        allow_network=False,
    )
    assert res_ctrl["trade_sim"]["paired_trades"] >= 1


def test_morning_init_base_exempts_bear_carry_active_live_501018(tmp_path: Path) -> None:
    rstore = SQLiteResearchStore(tmp_path / "research_501018.db")
    # Seed 16 prior days trending DOWN so ma5 < ma15 on trade_date
    base_dt = datetime(2026, 10, 12)
    prior_dates: list[str] = []
    for k in range(16):
        td = (base_dt + timedelta(days=k)).strftime("%Y%m%d")
        prior_dates.append(td)
        px = 1.40 - k * 0.015  # 1.40 down to 1.175 -> ma5 < ma15
        _seed_brent_flat(rstore, td, 75.0)
        _seed_symbol_bars(rstore, "501018", td, px + 0.01, [px] * 240, name="南方原油", market="SH")

    trade_date = "20261110"
    pre_close = 1.175
    _seed_brent_flat(rstore, trade_date, 75.0)
    # Morning bars 09:31..09:42: Brent rises +4.67% so sp_fx < 0 (normal V8 sell cannot fire),
    # while LOF rises +2.38% >= 2.00% with m1 < 0 so ONLY bear_carry_active triggers morning full sell!
    brn_td_pairs = [(_minute_str(i), 78.50 if i >= 5 else 75.0) for i in range(15)]
    _seed_symbol_bars(rstore, "BRN0Y", trade_date, 75.0, brn_td_pairs, name="Brent", market="US")
    lof_td_pairs = [
        ("09:31", 1.180),
        ("09:32", 1.188),
        ("09:33", 1.195),
        ("09:34", 1.201),
        ("09:35", 1.206),
        ("09:36", 1.208),
        ("09:37", 1.209),
        ("09:38", 1.210),
        ("09:39", 1.210),
        ("09:40", 1.209),
        ("09:41", 1.206),
        ("09:42", 1.203),  # lof_chg = +2.38%, m1 < 0, sp_fx ≈ +0.04% -> ONLY bear_carry triggers sell!
    ]
    _seed_symbol_bars(rstore, "501018", trade_date, pre_close, lof_td_pairs, name="南方原油", market="SH")

    rule_spec = {
        "id": "ai-501018-v9-multi-oil",
        "name": "501018 多油共振 V9+",
        "symbols": ["501018", "BRN0Y"],
        "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0002},
    }
    pos_sizing = normalize_position_sizing(
        {
            "engine_profile": "v9_plus_aggressive",
            "min_retained_shares": 100,
            "max_daily_entries": 6,
            "cooldown_minutes": 4,
            "close_flatten_time": "14:53",
            "v9_tuning": {"multi_oil_plunge_mode": True},
        }
    )
    now_dt = datetime(2026, 11, 10, 9, 42, 40, tzinfo=SHANGHAI_TZ)

    # Without :v9-init-base: signal -> bear_carry_active=True triggers full sell of sellable shares (4900)
    dec_no_init = evaluate_v9_plus_live_decision(
        rstore,
        symbol="501018",
        trade_date=trade_date,
        now_hhmm="09:42",
        now_dt=now_dt,
        cap_limit=50000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=[],
        rolling_shares=5000,
        allow_network=False,
    )
    assert len(dec_no_init["intraday"]) == 1
    assert dec_no_init["intraday"][0]["side"] == "sell"
    assert dec_no_init["intraday"][0]["qty_override"] == 4900

    # With :v9-init-base: signal in today_signals -> bear_carry_active=False -> NO forced sell!
    init_sig_by_key = [
        {
            "signal_key": f"501018:{trade_date}:init-base:v9-init-base:buy",
            "side": "buy",
            "quantity": 5000,
            "minute": "09:35",
            "signal_price": "1.206",
        }
    ]
    assert _has_morning_init_base_signal(init_sig_by_key) is True
    dec_with_init_key = evaluate_v9_plus_live_decision(
        rstore,
        symbol="501018",
        trade_date=trade_date,
        now_hhmm="09:42",
        now_dt=now_dt,
        cap_limit=50000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=init_sig_by_key,
        rolling_shares=5000,
        allow_network=False,
    )
    assert dec_with_init_key["intraday"] == []

    # With signal_tag == "v9-init-base" -> also exempts bear_carry_active
    init_sig_by_tag = [
        {
            "signal_key": f"501018:{trade_date}:custom-key",
            "signal_tag": "v9-init-base",
            "side": "buy",
            "quantity": 5000,
            "minute": "09:35",
            "signal_price": "1.206",
        }
    ]
    dec_with_init_tag = evaluate_v9_plus_live_decision(
        rstore,
        symbol="501018",
        trade_date=trade_date,
        now_hhmm="09:42",
        now_dt=now_dt,
        cap_limit=50000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=init_sig_by_tag,
        rolling_shares=5000,
        allow_network=False,
    )
    assert dec_with_init_tag["intraday"] == []

    # Explicit has_morning_init_base=True flag also exempts bear_carry_active
    dec_with_flag = evaluate_v9_plus_live_decision(
        rstore,
        symbol="501018",
        trade_date=trade_date,
        now_hhmm="09:42",
        now_dt=now_dt,
        cap_limit=50000.0,
        rule_spec=rule_spec,
        pos_sizing=pos_sizing,
        today_signals=[],
        rolling_shares=5000,
        allow_network=False,
        has_morning_init_base=True,
    )
    assert dec_with_flag["intraday"] == []


class _FakePolicyStore:
    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    def get_policy(self, policy_id: str) -> dict[str, Any] | None:
        return dict(self._row) if policy_id == self._row["id"] else None


def test_auto_trade_resolve_active_policy_and_pool_summary_oil_regime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auto_store = SQLiteAutoTradeStore(tmp_path / "auto.db")
    rstore = SQLiteResearchStore(tmp_path / "res.db")
    engine = AutoTradeEngine(
        auto_store=auto_store,
        research_store=rstore,
        broker=None,
        trade_executor=None,
    )

    # 1. Verify _resolve_active_capital_policy preserves stored_params & inherits user total_pool_cap for new builtin policy
    engine.portfolio_policy_store = _FakePolicyStore(
        {
            "id": "route_d_oil_regime",
            "name": "Route D++ 五态油价周期路由版",
            "engine_type": "route_d_plus",
            "is_builtin": True,
            "enabled": True,
            "params": {
                "sigma_cap_pct": 2.60,
                "rho_thresh": 0.58,
                "total_pool_cap": 100000.0,
            },
        }
    )
    resolved = engine._resolve_active_capital_policy(
        {
            "capital_policy_mode": "route_d_oil_regime",
            "pool_policy": {"total_pool_cap": 250000.0, "sigma_cap_pct": 4.20},
        }
    )
    assert resolved["policy_id"] == "route_d_oil_regime"
    assert resolved["is_shared_pool"] is True
    assert resolved["params"]["sigma_cap_pct"] == pytest.approx(2.60)
    assert resolved["params"]["rho_thresh"] == pytest.approx(0.58)
    assert resolved["params"]["total_pool_cap"] == pytest.approx(250000.0)

    # 2. Verify empty symbols branch exposes oil_regime=None
    empty_state = {
        "active_capital_policy": resolved,
        "engine_type": "route_d_plus",
        "policy": {"sigma_cap_pct": 2.60, "regime_router_enabled": True},
        "pool_cap": 250000.0,
        "pool_equity": 250000.0,
        "symbols": [],
        "sym_cfgs": {},
        "rules_by_sym": {},
        "ps_by_sym": {},
        "anchors": {},
        "held_shares": {},
        "prices": {},
        "held_mvs": {},
        "reserves": {},
        "total_held": 0.0,
        "total_reserve_short": 0.0,
        "available_pool_cash": 250000.0,
        "free_pool_cash": 250000.0,
        "causal_ctx": {},
    }
    monkeypatch.setattr(engine, "_gather_pool_state", lambda *a, **kw: empty_state)
    summary_empty = engine._compute_pool_allocation_summary({}, "20261110")
    assert summary_empty is not None
    assert "oil_regime" in summary_empty
    assert summary_empty["oil_regime"] is None

    # 3. Verify normal branch with oil_regime & regime_router_enabled=True updates portfolio_sigma_cap_pct and enforced=True
    orig_oil_regime = {
        "enforced": False,
        "locked_regime": "BOTTOM_REVERSAL",
        "effective_regime": "BOTTOM_REVERSAL",
        "probs": {"BOTTOM_REVERSAL": 64.5, "RANGE_OSCILLATION": 20.5},
        "ma_confirm_ratio": 0.25,
        "effective_cluster_cap_pct": 48.0,
        "effective_sigma_cap_pct": 2.15,
        "fast_crash_active": False,
        "degraded_mode": False,
        "degraded_sources": [],
    }
    normal_state = {
        "active_capital_policy": resolved,
        "engine_type": "route_d_plus",
        "policy": {"sigma_cap_pct": 4.20, "regime_router_enabled": True},
        "pool_cap": 200000.0,
        "pool_equity": 200000.0,
        "symbols": ["160723"],
        "sym_cfgs": {"160723": {"daily_max_buy_amount": 50000.0}},
        "rules_by_sym": {"160723": {}},
        "ps_by_sym": {"160723": {}},
        "anchors": {"160723": "fof_t0_multisource"},
        "held_shares": {"160723": 0},
        "prices": {"160723": 1.35},
        "held_mvs": {"160723": 0.0},
        "reserves": {"160723": 0.0},
        "total_held": 0.0,
        "total_reserve_short": 0.0,
        "available_pool_cash": 200000.0,
        "free_pool_cash": 200000.0,
        "causal_ctx": {
            "cache_hit": True,
            "vols": {"160723": 2.8},
            "history_days": {"160723": 20},
            "oil_regime": orig_oil_regime,
        },
    }
    monkeypatch.setattr(engine, "_gather_pool_state", lambda *a, **kw: normal_state)
    monkeypatch.setattr(
        engine,
        "_resolve_live_raw_close_weights",
        lambda **kw: ({"160723": 0.60}, {"160723": "测试信号"}, {"160723": "20261110"}),
    )
    monkeypatch.setattr(
        engine._pool_allocator,
        "compute_overnight_weights",
        lambda **kw: {"alloc_weights": {"160723": 0.48}, "portfolio_sigma_pct": 1.95},
    )
    monkeypatch.setattr(
        engine._pool_allocator,
        "compute_symbol_intraday_cap",
        lambda **kw: {"cap_amount": 96000.0, "r_max": 0.48, "room_amount": 96000.0, "m_rec": 1.0, "sentry": 0.0},
    )

    summary_enforced = engine._compute_pool_allocation_summary({}, "20261110")
    assert summary_enforced is not None
    assert summary_enforced["portfolio_sigma_cap_pct"] == pytest.approx(2.15)
    assert summary_enforced["oil_regime"] is not None
    assert summary_enforced["oil_regime"]["enforced"] is True
    assert summary_enforced["oil_regime"]["locked_regime"] == "BOTTOM_REVERSAL"
    assert summary_enforced["oil_regime"]["effective_cluster_cap_pct"] == pytest.approx(48.0)
    # Original causal_ctx dict must not be mutated in place
    assert orig_oil_regime["enforced"] is False

    # 4. Verify when regime_router_enabled=False -> oil_regime.enforced=False & portfolio_sigma_cap_pct stays 4.20
    normal_state_shadow = {
        **normal_state,
        "policy": {"sigma_cap_pct": 4.20, "regime_router_enabled": False},
    }
    monkeypatch.setattr(engine, "_gather_pool_state", lambda *a, **kw: normal_state_shadow)
    summary_obs = engine._compute_pool_allocation_summary({}, "20261110")
    assert summary_obs is not None
    assert summary_obs["portfolio_sigma_cap_pct"] == pytest.approx(4.20)
    assert summary_obs["oil_regime"] is not None
    assert summary_obs["oil_regime"]["enforced"] is False


def test_quant_rules_normalize_position_sizing_preserves_antislip_v9_tuning() -> None:
    from level2_service.quant_rules import normalize_position_sizing

    norm = normalize_position_sizing(
        {
            "mode": "cash_pool_rolling",
            "engine_profile": "v9_plus_aggressive",
            "v9_tuning": {
                "disable_scalp": True,
                "sub_1yuan_pure_overnight": True,
                "fof_t0_min_tp_pct": 0.18,
                "unconfirmed_ma15_cap_wt": 0.65,
                "dual_prem_unconf_cap_wt": 0.50,
                "dual_prem_unconf_thresh": 0.45,
                "rebound_taper_wt": 0.55,
                "rebound_taper_pct": 4.0,
                "rev_t_min_sp_fx": 0.50,
                "sub_1yuan_price_thresh": 1.20,
            },
        }
    )
    vt = norm["v9_tuning"]
    assert vt["disable_scalp"] is True
    assert vt["sub_1yuan_pure_overnight"] is True
    assert vt["fof_t0_min_tp_pct"] == pytest.approx(0.18)
    assert vt["unconfirmed_ma15_cap_wt"] == pytest.approx(0.65)
    assert vt["dual_prem_unconf_cap_wt"] == pytest.approx(0.50)
    assert vt["dual_prem_unconf_thresh"] == pytest.approx(0.45)
    assert vt["rebound_taper_wt"] == pytest.approx(0.55)
    assert vt["rebound_taper_pct"] == pytest.approx(4.0)
    assert vt["rev_t_min_sp_fx"] == pytest.approx(0.50)
    assert vt["sub_1yuan_price_thresh"] == pytest.approx(1.20)


def test_pool_allocation_summary_oil_regime_dual_track_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from level2_service.portfolio_allocator import SQLitePortfolioPolicyStore

    auto_store = SQLiteAutoTradeStore(tmp_path / "auto_cmp.db")
    rstore = SQLiteResearchStore(tmp_path / "res_cmp.db")
    pstore = SQLitePortfolioPolicyStore(tmp_path / "policies_cmp.db")
    engine = AutoTradeEngine(
        auto_store=auto_store,
        research_store=rstore,
        broker=None,
        trade_executor=None,
    )
    engine.portfolio_policy_store = pstore

    syms = ["160723", "161129", "501018", "162411"]
    anchors = {
        "160723": "fof_t0_multisource",
        "161129": "uso_bno_sc_peer",
        "501018": "brent",
        "162411": "xop_us_ep",
    }
    snap = {
        "m_rec": 1.35,
        "sentry": 0.0,
        "k_ov_strong": 0.45,
        "k_ov_mod": 0.45,
        "intra": {"buy_tier_a": (0.72, 0.42)},
        "event_count": 20,
    }
    oil_regime = {
        "enforced": False,
        "profile": "oil_v5_dual_key",
        "locked_regime": "中继下跌",
        "effective_regime": "中继下跌",
        "probs": {"上升": 0.5, "震荡上行": 1.1, "震荡横盘": 15.9, "中继下跌": 70.3, "下降": 12.2},
        "ma_confirm_ratio": 0.0,
        "effective_cluster_cap_pct": 48.0,
        "effective_sigma_cap_pct": 1.98,
        "solo_crude_coef": 0.33,
        "solo_crude_cap": 0.45,
        "symbol_multipliers": {"160723": 1.08, "161129": 0.64, "501018": 0.94, "162411": 1.08},
        "fast_crash_active": False,
        "degraded_mode": None,
        "degraded_sources": [],
    }
    causal_ctx = {
        "cache_hit": True,
        "snaps": {s: dict(snap) for s in syms},
        "vols": {s: 2.0 for s in syms},
        "corrs": {(a, b): (1.0 if a == b else 0.90) for a in syms for b in syms},
        "history_days": {s: 20 for s in syms},
        "oil_regime": oil_regime,
    }
    monkeypatch.setattr(
        engine,
        "_resolve_live_raw_close_weights",
        lambda **kw: (
            {"160723": 1.0, "161129": 1.0, "501018": 1.0, "162411": 0.0},
            {s: "测试共振信号" for s in syms},
            {s: "20261127" for s in syms},
        ),
    )

    def _build_state(mode: str) -> dict[str, Any]:
        active_cap = engine._resolve_active_capital_policy(
            {"capital_policy_mode": mode, "pool_policy": {"total_pool_cap": 100000.0}}
        )
        return {
            "active_capital_policy": active_cap,
            "engine_type": active_cap["engine_type"],
            "policy": active_cap["params"],
            "pool_cap": 100000.0,
            "pool_equity": 100000.0,
            "symbols": syms,
            "sym_cfgs": {s: {"daily_max_buy_amount": 35000.0} for s in syms},
            "rules_by_sym": {s: {"id": f"r-{s}"} for s in syms},
            "ps_by_sym": {s: {} for s in syms},
            "anchors": anchors,
            "held_shares": {"160723": 10000, "161129": 8000, "501018": 5000, "162411": 0},
            "prices": {"160723": 2.0, "161129": 1.8, "501018": 1.5, "162411": 1.0},
            "held_mvs": {"160723": 20000.0, "161129": 14400.0, "501018": 7500.0, "162411": 0.0},
            "reserves": {s: 0.0 for s in syms},
            "total_held": 41900.0,
            "total_reserve_short": 0.0,
            "available_pool_cash": 58100.0,
            "free_pool_cash": 58100.0,
            "causal_ctx": causal_ctx,
        }

    # Case 1: enforced=False (route_d_sharpe_shield active) -> comparison shows baseline vs routed
    monkeypatch.setattr(engine, "_gather_pool_state", lambda *a, **kw: _build_state("route_d_sharpe_shield"))
    sum_off = engine._compute_pool_allocation_summary({}, "20261127")
    assert sum_off is not None
    assert sum_off["oil_regime"] is not None
    assert sum_off["oil_regime"]["enforced"] is False
    cmp_off = sum_off["oil_regime"]["comparison"]
    assert cmp_off["baseline_policy_id"] == "route_d_sharpe_shield"
    assert cmp_off["routed_policy_id"] == "route_d_oil_regime"
    assert cmp_off["baseline_total_pct"] == pytest.approx(sum_off["total_target_position_pct"])
    assert cmp_off["baseline_total_amount"] == pytest.approx(sum_off["total_target_capital_amount"])
    assert cmp_off["routed_total_pct"] < cmp_off["baseline_total_pct"]
    assert cmp_off["delta_total_pct"] == pytest.approx(
        round(cmp_off["routed_total_pct"] - cmp_off["baseline_total_pct"], 2)
    )
    assert cmp_off["delta_total_amount"] == pytest.approx(
        round(cmp_off["routed_total_amount"] - cmp_off["baseline_total_amount"], 2)
    )
    assert cmp_off["baseline_sigma_cap_pct"] == pytest.approx(2.40)
    assert cmp_off["routed_sigma_cap_pct"] == pytest.approx(1.98)
    assert cmp_off["routed_portfolio_sigma_pct"] <= 1.98 + 1e-3
    for sym in syms:
        sc = cmp_off["symbols"][sym]
        assert sc["delta_pct"] == pytest.approx(round(sc["routed_pct"] - sc["baseline_pct"], 2))
        assert sc["delta_amount"] == pytest.approx(round(sc["routed_amount"] - sc["baseline_amount"], 2))
        assert sc["delta_intraday_cap"] == pytest.approx(
            round(sc["routed_intraday_cap"] - sc["baseline_intraday_cap"], 2)
        )
        assert sc["baseline_intraday_cap"] == pytest.approx(sum_off["symbols"][sym]["dynamic_intraday_cap"])
    assert cmp_off["symbols"]["161129"]["routed_intraday_cap"] < cmp_off["symbols"]["161129"]["baseline_intraday_cap"]

    # Case 2: enforced=True (route_d_oil_regime active) -> comparison shows same dual-track symmetry
    monkeypatch.setattr(engine, "_gather_pool_state", lambda *a, **kw: _build_state("route_d_oil_regime"))
    sum_on = engine._compute_pool_allocation_summary({}, "20261127")
    assert sum_on is not None
    assert sum_on["oil_regime"] is not None
    assert sum_on["oil_regime"]["enforced"] is True
    cmp_on = sum_on["oil_regime"]["comparison"]
    assert cmp_on["baseline_policy_id"] == "route_d_sharpe_shield"
    assert cmp_on["routed_policy_id"] == "route_d_oil_regime"
    assert cmp_on["routed_total_pct"] == pytest.approx(sum_on["total_target_position_pct"])
    assert cmp_on["routed_total_amount"] == pytest.approx(sum_on["total_target_capital_amount"])
    assert cmp_on["delta_total_pct"] == pytest.approx(
        round(cmp_on["routed_total_pct"] - cmp_on["baseline_total_pct"], 2)
    )
    assert cmp_on["delta_total_amount"] == pytest.approx(
        round(cmp_on["routed_total_amount"] - cmp_on["baseline_total_amount"], 2)
    )
    assert cmp_on["baseline_total_pct"] == pytest.approx(cmp_off["baseline_total_pct"])
    assert cmp_on["routed_total_pct"] == pytest.approx(cmp_off["routed_total_pct"])
    assert cmp_on["delta_total_amount"] == pytest.approx(cmp_off["delta_total_amount"])
    for sym in syms:
        sc = cmp_on["symbols"][sym]
        assert sc["routed_intraday_cap"] == pytest.approx(sum_on["symbols"][sym]["dynamic_intraday_cap"])
        assert sc["delta_amount"] == pytest.approx(round(sc["routed_amount"] - sc["baseline_amount"], 2))
        assert sc["delta_intraday_cap"] == pytest.approx(
            round(sc["routed_intraday_cap"] - sc["baseline_intraday_cap"], 2)
        )

    # Case 3: fallback without portfolio_policy_store still builds valid comparison for both enforced states
    state_on_cached = _build_state("route_d_oil_regime")
    state_off_cached = _build_state("route_d_sharpe_shield")
    engine.portfolio_policy_store = None
    monkeypatch.setattr(engine, "_gather_pool_state", lambda *a, **kw: state_on_cached)
    sum_fb_on = engine._compute_pool_allocation_summary({}, "20261127")
    assert sum_fb_on is not None
    cmp_fb_on = sum_fb_on["oil_regime"]["comparison"]
    assert cmp_fb_on["baseline_policy_id"] == "route_d_sharpe_shield"
    assert cmp_fb_on["routed_policy_id"] == "route_d_oil_regime"
    assert cmp_fb_on["delta_total_pct"] == pytest.approx(
        round(cmp_fb_on["routed_total_pct"] - cmp_fb_on["baseline_total_pct"], 2)
    )
    monkeypatch.setattr(engine, "_gather_pool_state", lambda *a, **kw: state_off_cached)
    sum_fb_off = engine._compute_pool_allocation_summary({}, "20261127")
    assert sum_fb_off is not None
    cmp_fb_off = sum_fb_off["oil_regime"]["comparison"]
    assert cmp_fb_off["baseline_policy_id"] == "route_d_sharpe_shield"
    assert cmp_fb_off["routed_policy_id"] == "route_d_oil_regime"
    assert cmp_fb_off["delta_total_pct"] == pytest.approx(
        round(cmp_fb_off["routed_total_pct"] - cmp_fb_off["baseline_total_pct"], 2)
    )
    pstore.close()
    auto_store.close()
    rstore.close()


