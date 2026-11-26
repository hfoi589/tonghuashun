from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import time
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from level2_service.quant_data_sources import (
    derive_rule_data_sources,
    derive_rule_usage_tags,
)
from level2_service.quant_rules import SQLiteQuantRuleStore, normalize_position_sizing
from level2_service.research_replay import detect_rule_required_packs
from level2_service.research_store import SQLiteResearchStore
from level2_service.v9_fof_t0_pulse import (
    apply_fof_t0_pulse_intraday_overlays,
    fetch_live_fof_t0_spot,
    should_exit_fof_t0_long,
    should_exit_fof_t0_short,
    should_suppress_fof_t0_short_entry,
    v9_fof_t0_pulse_close_weight,
)
from level2_service.v9_plus_engine import (
    _LIVE_SPOT_TELEMETRY,
    V9MacroDataStore,
    _MEMORY_MACRO_CACHE,
    build_v9_plus_days,
    evaluate_v9_plus_backtest,
    evaluate_v9_plus_live_decision,
    evaluate_v9_plus_live_indicators,
    resolve_effective_close_weight,
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
    market: str = "SZ",
    minute_vols: list[float] | None = None,
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
            vol = float(minute_vols[idx]) if (minute_vols is not None and idx < len(minute_vols)) else 120.0
            rstore._connection.execute(
                """
                INSERT OR REPLACE INTO research_points (
                    symbol, trade_date, minute, price, average_price, volume, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (symbol, trade_date, tm, f"{float(px):.4f}", f"{avg_px:.4f}", f"{vol:.1f}", now_iso),
            )


def _sample_rule_spec() -> dict[str, Any]:
    ps = normalize_position_sizing(
        {
            "engine_profile": "v9_plus_aggressive",
            "cross_market_anchor": "fof_t0_multisource",
            "min_retained_shares": 100,
            "max_daily_entries": 8,
            "cooldown_minutes": 4,
            "close_flatten_time": "14:53",
            "scale_factor": 1.0,
            "allow_same_day_turnaround": "auto",
        }
    )
    return {
        "id": "ai-160723-t0-multisource-v1",
        "name": "嘉实原油 T+0双池多源增强 V1",
        "source": "ai",
        "symbols": ["160723", "BRN0Y"],
        "target_symbol": "160723",
        "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0002},
        "buy_rule": {"title": "折价正T", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "lte", "value": -0.20}]},
        "sell_rule": {"title": "溢价反T", "joiner": "and", "conditions": [{"type": "brent_spread_pct", "direction": "gte", "value": 0.60}]},
        "position_sizing": ps,
        "eval_criteria": {"start_date": "20261102", "round_trip_fee_pct": 0.01, "capital_cap": 100000.0},
    }


def test_v9_fof_t0_multisource_data_source_derivation_and_schema() -> None:
    spec = _sample_rule_spec()
    assert spec["position_sizing"]["cross_market_anchor"] == "fof_t0_multisource"
    assert spec["position_sizing"]["min_retained_shares"] == 100

    packs = set(
        detect_rule_required_packs(
            [spec["buy_rule"], spec["sell_rule"]],
            spec["position_sizing"],
        )
    )
    assert packs == {"iopv_fx", "uso_bno_sc_peer", "xop_us_ep"}

    derived = derive_rule_data_sources(spec)
    by_id = {d["id"]: d for d in derived}
    expected_ids = {
        "ashare_intraday_1m",
        "ashare_orderbook_l1",
        "ice_brent_1m",
        "fx_usdcny_usdcnh",
        "eastmoney_fund_nav",
        "us_ep_xop_crack_macro",
        "cme_energy_macro_futures",
        "us_oil_etf_uso_bno_dbo",
        "ine_sc0_futures",
        "ashare_peer_lof_quote",
    }
    assert set(by_id.keys()) == expected_ids
    assert by_id["ashare_orderbook_l1"]["required_in_session"] is False
    assert set(by_id["ashare_peer_lof_quote"]["symbols"]) == {"501018", "161129", "162411"}
    assert set(by_id["us_oil_etf_uso_bno_dbo"]["symbols"]) == {"USO", "BNO", "DBO"}
    assert set(by_id["us_ep_xop_crack_macro"]["symbols"]) == {"XOP", "XLE", "SPY", "IWM"}
    assert set(by_id["cme_energy_macro_futures"]["symbols"]) == {"CL", "HO"}
    assert set(by_id["fx_usdcny_usdcnh"]["symbols"]) == {"USDCNY", "USDCNH"}

    tags = [t["label"] for t in derive_rule_usage_tags(spec)]
    assert any("双池" in t for t in tags)
    assert any("162411" in t for t in tags)


def test_fetch_live_fof_t0_spot_13_in_1_parse_and_telemetry() -> None:
    _MEMORY_MACRO_CACHE.pop(("global", "live_spot_fof_t0_pulse_bundle"), None)
    _LIVE_SPOT_TELEMETRY.pop("live_spot_fof_t0_pulse_bundle", None)

    class _StubMacro:
        allow_network = True

        @staticmethod
        def fetch_text(_url: str, **_kwargs: Any) -> str:
            return "\n".join(
                [
                    'var hq_str_USDCNY="09:45:00,7.1120,7.1100";',
                    'var hq_str_fx_susdcnh="09:45:00,7.1250,7.1240";',
                    'var hq_str_hf_OIL="74.85,0.0,74.80,74.90";',
                    'var hq_str_hf_CL="71.25,0.0,71.20,71.30";',
                    'var hq_str_hf_HO="2.31,0.0,2.30,2.32";',
                    'var hq_str_nf_SC0="原油连续,094500,545.0,552.0,543.0,546.0,548.0,549.0,548.8,0,0,0,0,0,0,0,0,2026-11-26";',
                    'var hq_str_gb_uso="uso,80.12";',
                    'var hq_str_gb_bno="bno,34.56";',
                    'var hq_str_gb_dbo="dbo,16.78";',
                    'var hq_str_gb_xop="xop,142.34";',
                    'var hq_str_sh501018="南方原油,1.250";',
                    'var hq_str_sz161129="易方达原油,1.180";',
                    'var hq_str_sz162411="华宝油气,1.020";',
                ]
            )

        @staticmethod
        def _extract_sina_us_quote(parts: list[str]) -> dict[str, Any]:
            px = float(parts[1]) if len(parts) > 1 else 0.0
            return {"close": px, "reg_date": "20261125"}

        @staticmethod
        def _extract_sina_a_quote(parts: list[str]) -> dict[str, Any]:
            name = str(parts[0])
            px = float(parts[1]) if len(parts) > 1 else 0.0
            pre = round(px * 0.99, 4)
            return {"name": name, "price": px, "pre_close": pre, "date": "20261126", "time": "09:45"}

    bundle = fetch_live_fof_t0_spot(_StubMacro(), ttl_seconds=8.0)
    assert bundle is not None
    assert bundle["brn_px"] == pytest.approx(74.85)
    assert bundle["cl_px"] == pytest.approx(71.25)
    assert bundle["ho_px"] == pytest.approx(2.31)
    assert bundle["fx_px"] == pytest.approx(7.1250)  # CNH priority
    assert bundle["fx_cny_px"] == pytest.approx(7.1120)
    assert bundle["fx_cnh_px"] == pytest.approx(7.1250)
    assert bundle["sc_px"] == pytest.approx(548.8)
    assert bundle["sc_date"] == "20261126"
    assert bundle["sc_time"] == "09:45"
    assert bundle["uso_close"] == pytest.approx(80.12)
    assert bundle["bno_close"] == pytest.approx(34.56)
    assert bundle["dbo_close"] == pytest.approx(16.78)
    assert bundle["xop_close"] == pytest.approx(142.34)
    assert bundle["peer_501018"]["price"] == pytest.approx(1.250)
    assert bundle["peer_161129"]["price"] == pytest.approx(1.180)
    assert bundle["peer_162411"]["price"] == pytest.approx(1.020)

    tel = _LIVE_SPOT_TELEMETRY.get("live_spot_fof_t0_pulse_bundle") or {}
    assert tel.get("non_null_fields") == 13
    assert tel.get("total_fields") == 13


def test_v9_fof_t0_pulse_close_weight_dual_bucket_architecture() -> None:
    # 1. Extreme Brent quantile >= 90% -> 0% overnight (100% cash pure intraday T+0)
    w_hi, lbl_hi = v9_fof_t0_pulse_close_weight(
        {"ma5": 1.320, "ma15": 1.250, "fof_bull": True, "fof_ov": 0.4, "max_bpos_20d": 0.92},
        {"brn_pos": 0.91, "prem": 0.50, "fof_prem": 0.30, "price": 1.330, "dd15_pct": -0.5, "sc_vs_prev": 0.30},
        0,
    )
    assert w_hi == 0.0 and "纯T+0" in lbl_hi

    # 2. Normal bull capped at 0.85, reserving 15% T+0 dry-powder cash
    w_bull, lbl_bull = v9_fof_t0_pulse_close_weight(
        {"ma5": 1.260, "ma15": 1.200, "fof_bull": True, "fof_ov": 0.5, "max_bpos_20d": 0.60},
        {"brn_pos": 0.55, "prem": 0.60, "fof_prem": 0.35, "price": 1.270, "dd15_pct": -1.2, "sc_vs_prev": 0.50},
        0,
    )
    assert w_bull == 0.85 and "15%专属T+0" in lbl_bull

    # 3. Deep crash contrarian allows 90% and resolve_effective_close_weight preserves it
    crash_day = {
        "cross_market_anchor": "fof_t0_multisource",
        "ma5": 1.300,
        "ma15": 1.380,
        "fof_bull": False,
        "fof_ov": 0.2,
        "max_bpos_20d": 0.30,
    }
    crash_bar = {
        "brn_pos": 0.25,
        "prem": 1.44,
        "fof_prem": -0.90,
        "price": 1.245,
        "dd15_pct": -13.6,
        "sc_vs_prev": 0.47,
        "sc_intra": 0.20,
    }
    w_crash, lbl_crash = v9_fof_t0_pulse_close_weight(crash_day, crash_bar, 2)
    assert w_crash == 0.90 and "逆势" in lbl_crash
    eff_w, _ = resolve_effective_close_weight(v9_fof_t0_pulse_close_weight, crash_day, crash_bar, pos_mode=2, entry_prem=0.0)
    assert eff_w == 0.90

    # 4. Non-contrarian scalp leg below MA5 with prem >= 1.20% forces 0% EOD exit (even when entry_prem=0)
    weak_day = {
        "cross_market_anchor": "fof_t0_multisource",
        "ma5": 1.320,
        "ma15": 1.280,
        "fof_bull": True,
        "fof_ov": 0.2,
        "max_bpos_20d": 0.55,
    }
    weak_bar = {
        "brn_pos": 0.50,
        "prem": 1.40,
        "fof_prem": 1.40,
        "price": 1.310,
        "dd15_pct": -1.0,
        "sc_vs_prev": 0.20,
    }
    eff_weak, _ = resolve_effective_close_weight(v9_fof_t0_pulse_close_weight, weak_day, weak_bar, pos_mode="scalp", entry_prem=0.0)
    assert eff_weak == 0.0


def test_v9_fof_t0_pulse_overlays_and_zero_loss_exit_guards() -> None:
    bull_day = {"ma5": 1.300, "ma15": 1.260, "fof_ov": 0.30, "xop_ov": 0.20, "eq_prem_5d": 1.10}

    # Anti-chase block after 13:55 and suppress late reverse-T short entry after 14:10
    late_bar = {
        "time": "14:15",
        "price": 1.305,
        "prem": 0.20,
        "fof_prem": 0.20,
        "sp_fx": -0.25,
        "sp_intra": 0.05,
        "vdev": -0.30,
        "px_dd_hi": 0.40,
        "sp_dd_hi": 0.30,
        "sc_intra": 0.0,
        "cl_intra": 0.0,
        "brn_pos": 0.55,
        "brn_m5": 0.0,
        "dmc10": 0.0002,
        "macd10": 0.0005,
        "m1": 0.02,
        "peer_m3": 0.0,
        "p162_m3": 0.0,
        "gc10": False,
        "dc10": False,
    }
    _, b_out, _, meta_late = apply_fof_t0_pulse_intraday_overlays(
        bull_day, late_bar, s_sig=False, b_sig=True, scalp_ok=False, close_wt=0.85, sc=1.0
    )
    assert b_out is False
    assert meta_late.get("chase_blocked_reasons")
    assert should_suppress_fof_t0_short_entry(late_bar) is True

    # Quota-premium intraday dip T+0 channel
    quota_bar = {
        **late_bar,
        "time": "10:35",
        "prem": 2.65,
        "fof_prem": 2.40,
        "vdev": -0.52,
        "px_dd_hi": 0.72,
        "sp_dd_hi": 0.55,
        "spi_from_hi": 0.45,
        "sp_fx": 0.30,
        "sp_intra": -0.20,
        "brn_m5": 0.02,
        "cl_intra": 0.10,
        "gc10": True,
    }
    _, _, sc_quota, meta_quota = apply_fof_t0_pulse_intraday_overlays(
        bull_day, quota_bar, s_sig=False, b_sig=False, scalp_ok=False, close_wt=0.85, sc=1.0
    )
    assert sc_quota is True
    assert "c_quota_dip_t0" in (meta_quota.get("contrarian_channels") or [])

    # Tri-peer + SC0 catch-up T+0 channel (triggered via 162411 3m surge)
    tri_bar = {
        **late_bar,
        "time": "10:15",
        "prem": 0.40,
        "fof_prem": 0.35,
        "sp_fx": -0.15,
        "sp_intra": 0.05,
        "peer_gap": -0.35,
        "peer_m3": 0.02,
        "p162_m3": 0.09,
        "sc_intra": 0.18,
        "gc10": True,
    }
    _, _, sc_tri, meta_tri = apply_fof_t0_pulse_intraday_overlays(
        bull_day, tri_bar, s_sig=False, b_sig=False, scalp_ok=False, close_wt=0.85, sc=1.0
    )
    assert sc_tri is True
    assert "c_tri_peer_sc_catchup" in (meta_tri.get("contrarian_channels") or [])

    # Zero-loss guard prevents non-stop long exit at tiny/negative gain <= +0.06%
    no_loss_bar = {
        **late_bar,
        "time": "11:00",
        "price": 1.300,
        "sp_fx": 0.45,
        "coint10": 0.25,
        "prem": 0.30,
        "m3": 0.10,
        "m5": 0.15,
        "m1": -0.02,
        "dc10": True,
        "dmc10": -0.0002,
        "macd10": 0.0020,
        "iopv_intra": -0.10,
    }
    ex_blk, _ = should_exit_fof_t0_long(
        bull_day,
        no_loss_bar,
        b_buy=no_loss_bar,
        entry_px=1.300,  # gain_p = 0.00% <= 0.06%
        entry_sp=-0.30,
        entry_prem=0.10,
        s_sig=True,
        close_wt=0.85,
        is_scalp=False,
    )
    assert ex_blk is False

    # Fast reverse-T cover when gain + spread collapse is locked
    assert (
        should_exit_fof_t0_short(
            bull_day,
            {"price": 1.290, "sp_fx": 0.05, "vdev": -0.30, "dmc10": 0.0002, "m1": 0.01, "m5": -0.10, "prem": 0.10, "coint10": -0.05},
            entry_px=1.300,
            entry_sp=0.60,
            close_wt=0.85,
        )
        is True
    )


def test_v9_fof_t0_pulse_causal_timeline_and_full_engine_parity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THS_QUANT_OFFLINE", "1")
    db_path = tmp_path / "v9_fof_t0_160723.db"
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
    xop_rows = [["20260701", int(datetime(2026, 7, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 140.0]]
    xle_rows = [["20260701", int(datetime(2026, 7, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 92.0]]
    spy_rows = [["20260701", int(datetime(2026, 7, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 580.0]]
    iwm_rows = [["20260701", int(datetime(2026, 7, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 225.0]]
    cl_rows = [["20260701", int(datetime(2026, 7, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 74.0]]
    ho_rows = [["20260701", int(datetime(2026, 7, 2, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 2.40]]
    cl_1m_ts: list[int] = []
    cl_1m_px: list[float] = []
    sc0_daily: list[list[float | str]] = []
    sc0_mins_ts: list[int] = []
    sc0_mins_px: list[float] = []
    nav_payload: dict[str, float] = {}

    for idx in range(18):
        us_d = (datetime(2026, 11, 1, tzinfo=SHANGHAI_TZ) + timedelta(days=idx)).strftime("%Y%m%d")
        a_dt = datetime(2026, 11, 2, tzinfo=SHANGHAI_TZ) + timedelta(days=idx)
        a_d = a_dt.strftime("%Y%m%d")
        causal_ts = int((datetime.strptime(us_d, "%Y%m%d").replace(tzinfo=SHANGHAI_TZ) + timedelta(days=1, hours=6)).timestamp())
        oil_rows.append([us_d, causal_ts, 75.0 + idx * 0.2])
        uso_rows.append([us_d, causal_ts, 80.0 + idx * 0.3])
        bno_rows.append([us_d, causal_ts, 34.0 + idx * 0.1])
        dbo_rows.append([us_d, causal_ts, 16.0 + idx * 0.05])
        xop_rows.append([us_d, causal_ts, 140.0 - idx * 0.4])
        xle_rows.append([us_d, causal_ts, 92.0 - idx * 0.2])
        spy_rows.append([us_d, causal_ts, 580.0 + idx * 0.2])
        iwm_rows.append([us_d, causal_ts, 225.0 + idx * 0.1])
        cl_rows.append([us_d, causal_ts, 72.0 + idx * 0.15])
        ho_rows.append([us_d, causal_ts, 2.35 + idx * 0.01])
        sc0_daily.append([a_d, 560.0 + idx * 1.2])
        nav_payload[us_d] = round(1.300 + idx * 0.004, 4)

        px_day = round(1.310 + idx * 0.005, 3)
        bars_160 = [round(px_day * (1.0 - 0.004 * (1 - abs(i - 60) / 60.0)), 3) if 20 <= i <= 100 else px_day for i in range(240)]
        _seed_symbol_bars(rstore, "BRN0Y", a_d, 75.0, [("09:30", 75.0 + idx * 0.2), ("14:53", 75.1 + idx * 0.2), ("15:00", 75.2 + idx * 0.2)], name="布伦特", market="ICE")
        _seed_symbol_bars(rstore, "160723", a_d, px_day, bars_160, name="嘉实原油")
        _seed_symbol_bars(rstore, "501018", a_d, 1.300, [1.300 + (i / 240.0) * 0.01 for i in range(240)], name="南方原油", market="SH")
        _seed_symbol_bars(rstore, "161129", a_d, 1.200, [1.200 + (i / 240.0) * 0.008 for i in range(240)], name="易方达原油")
        _seed_symbol_bars(rstore, "162411", a_d, 1.000, [1.000 + (i / 240.0) * 0.009 for i in range(240)], name="华宝油气")

        open_ts = int(datetime.strptime(a_d + " 09:31", "%Y%m%d %H:%M").replace(tzinfo=SHANGHAI_TZ).timestamp())
        cl_1m_ts.extend([open_ts, open_ts + 19380])
        cl_1m_px.extend([72.0 + idx * 0.15, 72.3 + idx * 0.15])
        sc0_mins_ts.extend([open_ts, open_ts + 19380])
        sc0_mins_px.extend([560.0 + idx, 562.0 + idx])

    def _write_caches(xop_list: list[list[Any]], cl_1m_pair: tuple[list[int], list[float]]) -> None:
        with rstore._lock, rstore._connection:
            for k, v in (
                ("nav_160723", nav_payload),
                ("oil_daily_2y", oil_rows),
                ("uso_daily_2y", uso_rows),
                ("bno_daily_2y", bno_rows),
                ("dbo_daily_2y", dbo_rows),
                ("xop_daily_2y", xop_list),
                ("xle_daily_2y", xle_rows),
                ("spy_daily_2y", spy_rows),
                ("iwm_daily_2y", iwm_rows),
                ("cl_daily_2y", cl_rows),
                ("ho_daily_2y", ho_rows),
                ("fut_1m_cl", {"ts": cl_1m_pair[0], "px": cl_1m_pair[1]}),
                ("sc0_daily_all", sc0_daily),
                ("sc0_min_series", {"ts": sc0_mins_ts, "px": sc0_mins_px}),
                ("fx_usdcny_series", {"ts": [int(datetime(2026, 10, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [7.10]}),
                ("fx_usdcnh_series", {"ts": [int(datetime(2026, 10, 1, 9, 0, tzinfo=SHANGHAI_TZ).timestamp())], "px": [7.10]}),
            ):
                rstore._connection.execute("INSERT OR REPLACE INTO quant_macro_cache VALUES (?, ?, ?)", (k, json.dumps(v), now_ts))

    _write_caches(xop_rows, (cl_1m_ts, cl_1m_px))
    spec = _sample_rule_spec()
    saved = qstore.upsert_rule(spec, research_store=rstore)

    days_a, _ = build_v9_plus_days(
        rstore,
        symbol="160723",
        start_date="20261102",
        end_date="20261118",
        allow_network=False,
        warmup_days=10,
        cross_market_anchor="fof_t0_multisource",
    )
    assert len(days_a) >= 10
    assert days_a[-1]["cross_market_anchor"] == "fof_t0_multisource"
    snap_bar = days_a[-2]["bars"][100]

    # Mutate future (20261125) XOP & CL 1m -> historical day must remain bit-for-bit identical (zero lookahead)
    xop_mut = [*xop_rows, ["20261125", int(datetime(2026, 11, 26, 6, 0, tzinfo=SHANGHAI_TZ).timestamp()), 250.0]]
    fut_ts = int(datetime(2026, 11, 26, 10, 0, tzinfo=SHANGHAI_TZ).timestamp())
    _write_caches(xop_mut, ([*cl_1m_ts, fut_ts], [*cl_1m_px, 120.0]))
    _MEMORY_MACRO_CACHE.clear()
    days_b, _ = build_v9_plus_days(
        rstore,
        symbol="160723",
        start_date="20261102",
        end_date="20261118",
        allow_network=False,
        warmup_days=10,
        cross_market_anchor="fof_t0_multisource",
    )
    assert days_b[-2]["xop_ov"] == pytest.approx(days_a[-2]["xop_ov"])
    assert days_b[-2]["bars"][100]["cl_intra"] == pytest.approx(snap_bar["cl_intra"])
    assert days_b[-2]["bars"][100]["p162_m3"] == pytest.approx(snap_bar["p162_m3"])

    bt = evaluate_v9_plus_backtest(
        rstore,
        saved,
        saved["eval_criteria"],
        saved["position_sizing"],
        symbol="160723",
        cleaned_end="20261119",
        allow_network=False,
    )
    assert bt["coverage"]["evaluated_days"] == 18
    assert "capital_sim" in bt
    assert bt["base_position_t0"]["overnight_return_pct"] + bt["base_position_t0"]["intraday_t0_return_pct"] == pytest.approx(
        bt["base_position_t0"]["net_cum_return_pct"], abs=0.05
    )

    last_td = days_a[-1]["td"]
    dec = evaluate_v9_plus_live_decision(
        rstore,
        symbol="160723",
        trade_date=last_td,
        now_hhmm="14:53",
        cap_limit=100000.0,
        rule_spec=saved,
        pos_sizing=saved["position_sizing"],
        today_signals=[],
        rolling_shares=60000,
        allow_network=False,
        broker_available_cash=20000.0,
        expected_ledger_shares=60000,
    )
    assert dec is not None
    assert dec["diagnostics"]["cross_market_anchor"] == "fof_t0_multisource"
    assert dec["diagnostics"]["allow_same_day_t0"] is True
    assert dec["diagnostics"]["t0_cash_reserve_pct"] == pytest.approx(15.0)
    assert dec["diagnostics"]["close_target_weight_pct"] <= 85.0

    ind = evaluate_v9_plus_live_indicators(
        rstore,
        symbol="160723",
        trade_date=last_td,
        now_hhmm="10:30",
        cap_limit=100000.0,
        rule_spec=saved,
        pos_sizing=saved["position_sizing"],
        today_signals=[],
        rolling_shares=100,
        allow_network=False,
    )
    assert ind is not None
    assert ind["diagnostics"]["cross_market_anchor"] == "fof_t0_multisource"
    card_keys = [c.get("key") for c in (ind.get("market_indicators") or [])]
    assert "fof_t0_cme_xop_peer162" in card_keys
    cond_ids = [c.get("id") for c in (ind["buy"].get("conditions") or [])]
    assert "v9-buy-fof-t0-multisource" in cond_ids

    # Graceful offline fallback when optional macro & peer caches are absent
    with rstore._lock, rstore._connection:
        rstore._connection.execute(
            "DELETE FROM quant_macro_cache WHERE cache_key IN ('xop_daily_2y', 'xle_daily_2y', 'spy_daily_2y', 'iwm_daily_2y', 'fut_1m_cl', 'uso_daily_2y', 'bno_daily_2y', 'dbo_daily_2y', 'sc0_daily_all', 'sc0_min_series')"
        )
        rstore._connection.execute("DELETE FROM research_days WHERE symbol IN ('501018', '161129', '162411')")
        rstore._connection.execute("DELETE FROM research_points WHERE symbol IN ('501018', '161129', '162411')")
    _MEMORY_MACRO_CACHE.clear()
    bt_fb = evaluate_v9_plus_backtest(
        rstore,
        saved,
        saved["eval_criteria"],
        saved["position_sizing"],
        symbol="160723",
        cleaned_end="20261119",
        allow_network=False,
    )
    assert bt_fb["coverage"]["evaluated_days"] == 18
    rstore.close()
