"""V9+ S&P Oil & Gas ETF (513350) dedicated T+0 cross-market & peer engine.

Implements causal feature engineering, 5-layer close-weight allocation, and
T+0 bidirectional intraday turnaround overlays for `513350` (标普油气 ETF 富国,
tracking S&P Oil & Gas Exploration & Production Select Industry Index `SPSIOP`/`XOP`):

1. Full-replication XOP T-1 NAV re-anchoring (`W_XOP_ETF = 0.985`) with 24h off-shore
   USD/CNH prioritized over on-shore USD/CNY (`load_merged_fx_cnh_series`).
2. 5-day median QDII premium de-biasing (`eq_prem_5d`) so high-quota premium regimes
   do not choke intraday T+0 reversals while bubble-exit & V-bottom layers guard extremes.
3. Blended continuous intraday spread (`sp_fx = 0.55 * sp_xop_intra + 0.45 * sp_brn`)
   combining CME 23h energy/index futures (`CL/HO/NG/ES`), Brent `BRN0Y`, INE `SC0`,
   and peer `162411` (`W_XOP_PEER = 0.94`) relative momentum (`peer_gap_intra`).
4. T+0 bidirectional intraday turnaround (`allow_same_day_turnaround=True`): two-tranche
   main-rally split reverse-T + intraday dip-scalp buys + strict T+0 take-profit.
5. 13-in-1 Sina real-time bundle (`fetch_live_sp_oilgas_etf_spot`) feeding both
   `evaluate_v9_plus_live_decision` / `indicators` and `/quant/data-sources/probe`.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime
import statistics
import time
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .v9_fof_crude import load_merged_fx_cnh_series

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

_W_XOP_ETF = 0.985
_W_XOP_PEER_162 = 0.94
_BETA_BRN = 0.68
_PEER_SYM = "162411"
_DEFAULT_FX = 7.10
_SP_LIVE_SPOT_KEY = "live_spot_sp_oilgas_etf_bundle"
_SP_LIVE_SPOT_CODES: tuple[str, ...] = (
    "USDCNY",
    "fx_susdcnh",
    "hf_OIL",
    "hf_CL",
    "hf_HO",
    "hf_NG",
    "hf_ES",
    "nf_SC0",
    "gb_xop",
    "gb_iwm",
    "gb_xle",
    "gb_spy",
    "sz162411",
)


def _v9p():
    from . import v9_plus_engine

    return v9_plus_engine


def _safe_float(val: Any, default: float | None = None) -> float | None:
    try:
        num = float(val)
    except (TypeError, ValueError):
        return default
    return num


def _parse_hq_assignments(raw_text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for raw_line in str(raw_text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("var hq_str_") or "=" not in line:
            continue
        lhs, _, rhs = line.partition("=")
        code = lhs.replace("var hq_str_", "", 1).strip()
        payload = rhs.strip().rstrip(";").strip('"')
        out[code] = payload.split(",") if payload else []
    return out


def _extract_fx_quote(cols: Sequence[str], *, lo: float = 4.5, hi: float = 10.5) -> float | None:
    for idx in (8, 1, 2, 3, 5):
        if len(cols) > idx:
            val = _safe_float(cols[idx])
            if val is not None and lo <= val <= hi:
                return val
    return None


def _extract_hf_quote(cols: Sequence[str], *, lo: float = 0.1, hi: float = 20000.0) -> tuple[float | None, float | None]:
    last = _safe_float(cols[0]) if len(cols) > 0 else None
    pre = _safe_float(cols[7]) if len(cols) > 7 else None
    if last is not None and not (lo <= last <= hi):
        last = None
    if pre is not None and not (lo <= pre <= hi):
        pre = None
    return last, pre


def _extract_nf_sc0_quote(cols: Sequence[str]) -> dict[str, Any]:
    last = _safe_float(cols[8]) if len(cols) > 8 else None
    if not last or last <= 0:
        last = _safe_float(cols[10]) if len(cols) > 10 else None
    if not last or last <= 0:
        last = _safe_float(cols[5]) if len(cols) > 5 else None
    open_px = _safe_float(cols[2]) if len(cols) > 2 else None
    pre_px = _safe_float(cols[10]) if len(cols) > 10 else None
    if not pre_px or pre_px <= 0:
        pre_px = _safe_float(cols[5]) if len(cols) > 5 else None
    raw_date = str(cols[17]).strip().replace("-", "") if len(cols) > 17 else ""
    sc_date = raw_date if len(raw_date) == 8 and raw_date.isdigit() else None
    return {
        "price": last if (last and 80.0 <= last <= 3000.0) else None,
        "open": open_px if (open_px and 80.0 <= open_px <= 3000.0) else None,
        "pre_close": pre_px if (pre_px and 80.0 <= pre_px <= 3000.0) else None,
        "date": sc_date,
    }


def fetch_live_sp_oilgas_etf_spot(macro: Any, *, ttl_seconds: float = 8.0) -> dict[str, Any] | None:
    """Fetch 13-in-1 Sina spot bundle for 513350 (FX, Brent, CME CL/HO/NG/ES, SC0, US ETFs, 162411)."""
    v9p = _v9p()
    now_t = time.time()
    cache_key = ("global", _SP_LIVE_SPOT_KEY)
    cached = v9p._MEMORY_MACRO_CACHE.get(cache_key)
    if cached is not None and (now_t - float(cached[0])) <= ttl_seconds and isinstance(cached[1], Mapping):
        return dict(cached[1])
    if not getattr(macro, "allow_network", True):
        return dict(cached[1]) if (cached is not None and isinstance(cached[1], Mapping)) else None

    url = "https://hq.sinajs.cn/list=" + ",".join(_SP_LIVE_SPOT_CODES)
    try:
        raw = macro.fetch_text(url, referer="https://finance.sina.com.cn/", encoding="gbk", timeout=4.0)
        parsed = _parse_hq_assignments(raw)
        fx_cny_px = _extract_fx_quote(parsed.get("USDCNY", []))
        fx_cnh_px = _extract_fx_quote(parsed.get("fx_susdcnh", []))
        fx_px = fx_cnh_px or fx_cny_px

        brn_px, brn_pre = _extract_hf_quote(parsed.get("hf_OIL", []), lo=10.0, hi=500.0)
        cl_px, cl_pre = _extract_hf_quote(parsed.get("hf_CL", []), lo=10.0, hi=500.0)
        ho_px, ho_pre = _extract_hf_quote(parsed.get("hf_HO", []), lo=0.2, hi=30.0)
        ng_px, ng_pre = _extract_hf_quote(parsed.get("hf_NG", []), lo=0.2, hi=50.0)
        es_px, es_pre = _extract_hf_quote(parsed.get("hf_ES", []), lo=500.0, hi=25000.0)
        sc_info = _extract_nf_sc0_quote(parsed.get("nf_SC0", []))

        us_quotes: dict[str, dict[str, Any] | None] = {}
        for us_code, key_name in (("gb_xop", "xop"), ("gb_iwm", "iwm"), ("gb_xle", "xle"), ("gb_spy", "spy")):
            cols = parsed.get(us_code, [])
            us_quotes[key_name] = macro._extract_sina_us_quote(cols) if cols else None

        peer_cols = parsed.get("sz162411", [])
        peer_162411 = macro._extract_sina_a_quote(peer_cols) if peer_cols else None

        xop_q = us_quotes.get("xop")
        iwm_q = us_quotes.get("iwm")
        xle_q = us_quotes.get("xle")
        spy_q = us_quotes.get("spy")

        payload: dict[str, Any] = {
            "captured_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
            "fx_cny_px": fx_cny_px,
            "fx_cnh_px": fx_cnh_px,
            "fx_px": fx_px,
            "brn_px": brn_px,
            "brn_pre": brn_pre,
            "cl_px": cl_px,
            "cl_pre": cl_pre,
            "ho_px": ho_px,
            "ho_pre": ho_pre,
            "ng_px": ng_px,
            "ng_pre": ng_pre,
            "es_px": es_px,
            "es_pre": es_pre,
            "sc_px": sc_info.get("price"),
            "sc_open": sc_info.get("open"),
            "sc_pre": sc_info.get("pre_close"),
            "sc_date": sc_info.get("date"),
            "xop_quote": xop_q,
            "xop_close": xop_q.get("close") if isinstance(xop_q, Mapping) else None,
            "xop_reg_date": xop_q.get("reg_date") if isinstance(xop_q, Mapping) else None,
            "xop_ext_chg_pct": xop_q.get("ext_chg_pct") if isinstance(xop_q, Mapping) else None,
            "iwm_quote": iwm_q,
            "iwm_close": iwm_q.get("close") if isinstance(iwm_q, Mapping) else None,
            "xle_quote": xle_q,
            "xle_close": xle_q.get("close") if isinstance(xle_q, Mapping) else None,
            "spy_quote": spy_q,
            "spy_close": spy_q.get("close") if isinstance(spy_q, Mapping) else None,
            "peer_162411": peer_162411,
        }
        core_fields = (
            fx_px,
            brn_px,
            cl_px,
            ho_px,
            ng_px,
            es_px,
            sc_info.get("price"),
            payload["xop_close"],
            payload["iwm_close"],
            payload["xle_close"],
            payload["spy_close"],
            peer_162411.get("price") if isinstance(peer_162411, Mapping) else None,
        )
        non_null = sum(1 for v in core_fields if v is not None)
        v9p._record_live_spot_telemetry(
            cache_key,
            attempt_ts=now_t,
            ok=non_null >= 3,
            error=None if non_null >= 3 else "partial_or_empty_sina_bundle",
            non_null_fields=non_null,
            total_fields=len(core_fields),
        )
        if non_null >= 1:
            v9p._MEMORY_MACRO_CACHE[cache_key] = (now_t, payload)
        return payload
    except Exception as exc:
        v9p._record_live_spot_telemetry(
            cache_key,
            attempt_ts=now_t,
            ok=False,
            error=str(exc),
            non_null_fields=0,
            total_fields=12,
        )
        if cached is not None and isinstance(cached[1], Mapping) and (now_t - float(cached[0])) <= 300.0:
            return dict(cached[1])
        return None


def _merge_today_fut_1m(
    fut_ts: Sequence[int],
    fut_px: Sequence[float],
    live_spot: Mapping[str, Any] | None,
    px_key: str,
) -> tuple[list[int], list[float]]:
    ts_out = list(fut_ts)
    px_out = list(fut_px)
    if not live_spot or not live_spot.get(px_key):
        return ts_out, px_out
    try:
        spot_v = float(live_spot[px_key])
    except (TypeError, ValueError):
        return ts_out, px_out
    if spot_v <= 0:
        return ts_out, px_out
    now_ts = int(time.time())
    if not ts_out or now_ts > ts_out[-1]:
        ts_out.append(now_ts)
        px_out.append(spot_v)
    return ts_out, px_out


def build_sp_oilgas_etf_days_timeline(
    *,
    store: Any,
    symbol: str,
    lookback_start: str,
    day_rows: Sequence[Any],
    pts_by_day: Mapping[str, list[dict[str, Any]]],
    clean_start: str,
    end_date: str | None,
    close_cutoff_time: str,
    eff_macd: Mapping[str, Any],
    brn_ts: Sequence[int],
    brn_px: Sequence[float],
    fx_ts: Sequence[int],
    fx_px: Sequence[float],
    oil_rows: Sequence[Sequence[Any]],
    nav_map: Mapping[str, float],
    nav_162411: Mapping[str, float],
    xop_rows: Sequence[Sequence[Any]],
    xle_rows: Sequence[Sequence[Any]],
    spy_rows: Sequence[Sequence[Any]],
    iwm_rows: Sequence[Sequence[Any]],
    cl_rows: Sequence[Sequence[Any]],
    ho_rows: Sequence[Sequence[Any]],
    ng_rows: Sequence[Sequence[Any]],
    cl_1m: tuple[Sequence[int], Sequence[float]],
    ho_1m: tuple[Sequence[int], Sequence[float]],
    ng_1m: tuple[Sequence[int], Sequence[float]],
    es_1m: tuple[Sequence[int], Sequence[float]],
    sc_daily_rows: Sequence[Sequence[Any]],
    sc_min_ts: Sequence[int],
    sc_min_px: Sequence[float],
    live_sp_etf_spot: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Build 55-day causal warmup feature timeline for 513350 (`sp_oilgas_etf`) matching task-144 math."""
    v9p = _v9p()

    def _filt_rows(rows: Sequence[Sequence[Any]]) -> tuple[list[Sequence[Any]], list[int]]:
        filt = [r for r in rows if len(r) >= 3 and float(r[2]) > 0]
        return filt, [int(r[1]) for r in filt]

    xop_filt, xop_ts = _filt_rows(xop_rows)
    xop_by_d = {str(r[0]): float(r[2]) for r in xop_filt}
    xle_filt, xle_ts = _filt_rows(xle_rows)
    spy_filt, spy_ts = _filt_rows(spy_rows)
    iwm_filt, iwm_ts = _filt_rows(iwm_rows)
    cl_filt, cl_ts = _filt_rows(cl_rows)
    ho_filt, ho_ts = _filt_rows(ho_rows)
    ng_filt, ng_ts = _filt_rows(ng_rows)

    cl_1m_ts, cl_1m_px = _merge_today_fut_1m(cl_1m[0], cl_1m[1], live_sp_etf_spot, "cl_px")
    ho_1m_ts, ho_1m_px = _merge_today_fut_1m(ho_1m[0], ho_1m[1], live_sp_etf_spot, "ho_px")
    ng_1m_ts, ng_1m_px = _merge_today_fut_1m(ng_1m[0], ng_1m[1], live_sp_etf_spot, "ng_px")
    es_1m_ts, es_1m_px = _merge_today_fut_1m(es_1m[0], es_1m[1], live_sp_etf_spot, "es_px")

    sc_daily_map = {
        str(r[0]): (float(r[2]), float(r[3]) if len(r) >= 4 else float(r[2]))
        for r in sc_daily_rows
        if len(r) >= 3 and float(r[2]) > 0
    }
    sc_daily_dates = sorted(sc_daily_map)
    nav_dates = sorted(str(k) for k in nav_map)
    nav162_dates = sorted(str(k) for k in nav_162411)

    peer_pre, peer_pts = v9p._load_peer_intraday_series(
        store, (_PEER_SYM,), symbol, lookback_start, end_date
    )

    ratio_brn_hist: list[float] = []
    ratio_xop_hist: list[float] = []
    close_hist: list[float] = []
    recent_bpos: list[float] = []
    recent_xpos: list[float] = []
    close_prem_hist: list[float] = []

    days_out: list[dict[str, Any]] = []
    candidate_days = sum(1 for d in day_rows if str(d["trade_date"]) >= clean_start)

    for idx, day_row in enumerate(day_rows):
        td = str(day_row["trade_date"])
        raw_pts = pts_by_day.get(td, [])
        if len(raw_pts) < 2:
            continue
        prices = [float(p["price"]) for p in raw_pts]
        macd_rows = v9p.calculate_macd(prices, eff_macd)
        pts = [{**p, **m} for p, m in zip(raw_pts, macd_rows)]
        p1500 = next((p for p in reversed(prices) if p > 0), 0.0)

        pre_raw = float(day_row["pre_close"] or 0.0)
        pre = pre_raw if pre_raw > 0 else (close_hist[-1] if close_hist else prices[0])
        if pre <= 0:
            continue

        has_prev = idx >= 1
        prev_td = str(day_rows[idx - 1]["trade_date"]) if has_prev else td
        td_m2 = str(day_rows[idx - 2]["trade_date"]) if idx >= 2 else prev_td
        t0930_dt = v9p._parse_sh(td, "09:30")
        t0930_ts = int(t0930_dt.timestamp())
        prev0930_ts = int(v9p._parse_sh(prev_td, "09:30").timestamp())

        if xop_filt:
            xi = max(0, bisect_right(xop_ts, t0930_ts) - 1)
            xi_prev = max(0, bisect_right(xop_ts, prev0930_ts) - 1) if has_prev else max(0, xi - 1)
            x_cur = float(xop_filt[xi][2])
            x_prev_us = float(xop_filt[xi_prev][2])
            xop_ov = (x_cur / x_prev_us - 1.0) * 100.0 if x_prev_us > 0 else 0.0
            xop_5d = ((x_cur / float(xop_filt[xi - 5][2]) - 1.0) * 100.0) if (xi >= 5 and float(xop_filt[xi - 5][2]) > 0) else 0.0
            x_ma5 = statistics.mean(float(r[2]) for r in xop_filt[max(0, xi - 4) : xi + 1])
            x_ma15 = statistics.mean(float(r[2]) for r in xop_filt[max(0, xi - 14) : xi + 1])
            dt_us_close = datetime.fromtimestamp(int(xop_filt[xi][1]) - 3600, tz=SHANGHAI_TZ)
            dt_prev_us = datetime.fromtimestamp(int(xop_filt[xi_prev][1]) - 3600, tz=SHANGHAI_TZ)
            us_d1_str = str(xop_filt[xi][0])
        else:
            x_cur = 100.0
            x_prev_us = 100.0
            xop_ov = 0.0
            xop_5d = 0.0
            x_ma5 = 100.0
            x_ma15 = 100.0
            dt_us_close = v9p._parse_sh(prev_td, "23:00")
            dt_prev_us = dt_us_close
            us_d1_str = prev_td

        spy_ov, _, _ = v9p._causal_daily_ov_and_nd(spy_filt, spy_ts, t0930_ts, prev0930_ts, has_prev)
        ng_ov, _, _ = v9p._causal_daily_ov_and_nd(ng_filt, ng_ts, t0930_ts, prev0930_ts, has_prev)
        iwm_ov, _, _ = v9p._causal_daily_ov_and_nd(iwm_filt, iwm_ts, t0930_ts, prev0930_ts, has_prev)
        xle_ov, _, _ = v9p._causal_daily_ov_and_nd(xle_filt, xle_ts, t0930_ts, prev0930_ts, has_prev)
        cl_ov, cl_3d, cl_ci = v9p._causal_daily_ov_and_nd(cl_filt, cl_ts, t0930_ts, prev0930_ts, has_prev, n_days=3)
        ho_ov, ho_3d, ho_ci = v9p._causal_daily_ov_and_nd(ho_filt, ho_ts, t0930_ts, prev0930_ts, has_prev, n_days=3)
        same_ed = cl_ci >= 0 and ho_ci >= 0 and str(cl_filt[cl_ci][0]) == str(ho_filt[ho_ci][0])
        crack_ov = (ho_ov - cl_ov) if same_ed else 0.0
        crack_3d = (ho_3d - cl_3d) if (same_ed and cl_ci >= 3 and ho_ci >= 3) else 0.0

        x_lo, x_hi = v9p._day_xop_range(xop_filt, td, t0930_ts)
        xop_pos_day = v9p._brent_range_position(x_cur, x_lo, x_hi)
        lo_h, hi_h = v9p._day_oil_range(oil_rows, brn_px, td, t0930_ts)

        b_us_close = v9p._val_at(brn_ts, brn_px, dt_us_close) or v9p._val_at(brn_ts, brn_px, t0930_dt) or 70.0
        f_us_close = v9p._val_at(fx_ts, fx_px, v9p._parse_sh(us_d1_str, "23:00")) or _DEFAULT_FX
        b_prev_us = v9p._val_at(brn_ts, brn_px, dt_prev_us) or b_us_close
        b1500 = v9p._val_at(brn_ts, brn_px, v9p._parse_sh(td, "15:00")) or b_us_close
        f1500 = v9p._val_at(fx_ts, fx_px, v9p._parse_sh(td, "15:00")) or f_us_close
        b_open_td = v9p._val_at(brn_ts, brn_px, t0930_dt) or b_us_close
        f_open_td = v9p._val_at(fx_ts, fx_px, t0930_dt) or f_us_close
        b_p15 = (v9p._val_at(brn_ts, brn_px, v9p._parse_sh(prev_td, "15:00")) if has_prev else b_open_td) or b_prev_us
        f_p15 = (v9p._val_at(fx_ts, fx_px, v9p._parse_sh(prev_td, "15:00")) if has_prev else f_open_td) or f_us_close
        x_syn_p15 = x_prev_us * (1.0 + 0.45 * (b_p15 / b_prev_us - 1.0)) if (has_prev and b_prev_us > 0) else x_cur

        def _est_nav_t1(nmap: Mapping[str, float], ndates: Sequence[str], w_beta: float, fallback_pre: float) -> float:
            ntd = next((k for k in reversed(ndates) if k <= td_m2 and float(nmap.get(k) or 0) > 0), None) if has_prev else None
            if ntd is None:
                return fallback_pre
            nbase = float(nmap[ntd])
            nxtd = max((d for d in xop_by_d if d <= ntd), default=None)
            xn0 = xop_by_d[nxtd] if nxtd else x_cur
            fn0 = v9p._val_at(fx_ts, fx_px, v9p._parse_sh(ntd, "23:00")) or f_us_close
            if xn0 > 0 and fn0 > 0:
                return nbase * (1.0 + w_beta * (x_cur / xn0 - 1.0)) * (f_us_close / fn0)
            return fallback_pre

        nav_est_t1 = _est_nav_t1(nav_map, nav_dates, _W_XOP_ETF, pre)
        p162_pre = peer_pre.get(_PEER_SYM, {}).get(td) or 0.0
        live_p162 = (live_sp_etf_spot or {}).get("peer_162411") or {}
        if (p162_pre <= 0) and str(live_p162.get("date") or "") == td and live_p162.get("pre_close"):
            p162_pre = float(live_p162["pre_close"])
        nav162_t1 = _est_nav_t1(nav_162411, nav162_dates, _W_XOP_PEER_162, p162_pre)

        eq_prem_5d = statistics.median(close_prem_hist[-5:]) if close_prem_hist else 0.0
        ma5 = statistics.mean(close_hist[-5:]) if close_hist else pre
        ma15 = statistics.mean(close_hist[-15:]) if close_hist else pre
        hi15_pre = max(close_hist[-15:] + [pre]) if close_hist else pre

        cur_r_brn = p1500 / ((b1500 ** _BETA_BRN) * f1500) if (b1500 > 0 and f1500 > 0 and p1500 > 0) else None
        eq10_brn = (
            statistics.mean(ratio_brn_hist[-10:])
            if ratio_brn_hist
            else (pre / ((b_p15 ** _BETA_BRN) * f_p15) if b_p15 > 0 and f_p15 > 0 else 0.0)
        )
        x_syn_1500 = x_cur * (1.0 + 0.45 * (b1500 / b_us_close - 1.0)) if b_us_close > 0 else x_cur
        cur_r_xop = p1500 / ((x_syn_1500 ** _W_XOP_ETF) * f1500) if (x_syn_1500 > 0 and f1500 > 0 and p1500 > 0) else None
        eq10_xop = (
            statistics.mean(ratio_xop_hist[-10:])
            if ratio_xop_hist
            else (pre / ((max(1e-6, x_syn_p15) ** _W_XOP_ETF) * f_p15) if x_syn_p15 > 0 and f_p15 > 0 else 0.0)
        )

        # Causal INE SC0 prev close, bull regime & today open
        sc_hist = [sc_daily_map[d][0] for d in sc_daily_dates if d < td]
        sc_prev = sc_hist[-1] if sc_hist else None
        if sc_hist:
            sc_bull_day = (statistics.mean(sc_hist[-5:]) >= statistics.mean(sc_hist[-15:])) or (sc_hist[-1] >= statistics.mean(sc_hist[-15:]))
        else:
            sc_bull_day = True
        sc_op_pair = v9p._val_at_with_ts(sc_min_ts, sc_min_px, t0930_dt)
        if sc_op_pair is not None and abs(t0930_ts - sc_op_pair[1]) <= 6 * 3600:
            sc_open_v: float | None = sc_op_pair[0]
        elif td in sc_daily_map:
            sc_open_v = sc_daily_map[td][1]
        elif live_sp_etf_spot and str(live_sp_etf_spot.get("sc_date") or "") == td and live_sp_etf_spot.get("sc_open"):
            sc_open_v = float(live_sp_etf_spot["sc_open"])
        else:
            sc_open_v = sc_prev

        p162_rows = list(peer_pts.get(_PEER_SYM, {}).get(td, []))
        if (
            live_p162
            and str(live_p162.get("date") or "") == td
            and live_p162.get("time")
            and live_p162.get("price")
            and (not p162_rows or str(live_p162["time"]) > p162_rows[-1][0])
        ):
            p162_rows.append((str(live_p162["time"]), float(live_p162["price"])))
        i162 = 0
        last162: float | None = None

        def _anchor_fut(ts_s: Sequence[int], px_s: Sequence[float]) -> tuple[float | None, int]:
            pair = v9p._val_at_with_ts(ts_s, px_s, dt_us_close)
            if pair is not None and abs(dt_us_close.timestamp() - pair[1]) <= 12 * 3600:
                return float(pair[0]), int(pair[1])
            return None, 0

        ng_uc, ng_bts = _anchor_fut(ng_1m_ts, ng_1m_px)
        es_uc, es_bts = _anchor_fut(es_1m_ts, es_1m_px)
        cl_uc, cl_bts = _anchor_fut(cl_1m_ts, cl_1m_px)
        ho_uc, ho_bts = _anchor_fut(ho_1m_ts, ho_1m_px)

        def _fut_r(ts_s: Sequence[int], px_s: Sequence[float], bv: float | None, bts: int, dt_n: datetime, cap: float) -> tuple[float, bool]:
            if bv and bv > 0:
                pair = v9p._val_at_with_ts(ts_s, px_s, dt_n)
                if pair is not None and pair[1] > bts:
                    return max(-cap, min(cap, float(pair[0]) / bv - 1.0)), True
            return 0.0, False

        in_range = td >= clean_start and (not end_date or td <= end_date)
        is_live_today = bool(live_sp_etf_spot) and (td == day_rows[-1]["trade_date"])

        bars: list[dict[str, Any]] = []
        cum_pv = 0.0
        cum_v = 0.0
        hi_px = 0.0
        lo_px = 999999.0
        hi_sp = -999.0
        sp0_xop: float | None = None
        b0_iopv: float | None = None
        peer_gap0: float | None = None
        px_scale = 2.0 / max(0.50, pre)
        vols_hist: list[float] = []

        for i, pt in enumerate(pts):
            pr = float(pt["price"])
            if pr <= 0:
                continue
            tm = str(pt["time"])
            t_dt = v9p._parse_sh(td, tm)
            is_last_bar = i == len(pts) - 1

            bn = v9p._val_at(brn_ts, brn_px, t_dt) or b_us_close
            fn = v9p._val_at(fx_ts, fx_px, t_dt) or f_us_close
            if is_live_today and is_last_bar and isinstance(live_sp_etf_spot, Mapping):
                if live_sp_etf_spot.get("brn_px"):
                    bn = float(live_sp_etf_spot["brn_px"])
                if live_sp_etf_spot.get("fx_px"):
                    fn = float(live_sp_etf_spot["fx_px"])

            d_brn_r = max(-0.15, min(0.15, bn / b_us_close - 1.0)) if b_us_close > 0 else 0.0
            d_ng_r, _ = _fut_r(ng_1m_ts, ng_1m_px, ng_uc, ng_bts, t_dt, 0.15)
            d_es_r, _ = _fut_r(es_1m_ts, es_1m_px, es_uc, es_bts, t_dt, 0.10)
            d_cl_r, has_cl = _fut_r(cl_1m_ts, cl_1m_px, cl_uc, cl_bts, t_dt, 0.15)
            d_ho_r, has_ho = _fut_r(ho_1m_ts, ho_1m_px, ho_uc, ho_bts, t_dt, 0.15)
            d_crude_r = (0.55 * d_cl_r + 0.45 * d_brn_r) if has_cl else d_brn_r
            d_crack_r = (d_ho_r - d_cl_r) if (has_cl and has_ho) else 0.0
            d_xop_r = 0.45 * d_crude_r + 0.10 * d_crack_r + 0.06 * d_ng_r + 0.28 * d_es_r
            daytime_lead_pct = (d_crude_r + 0.20 * d_crack_r + 0.15 * d_ng_r + 0.30 * d_es_r) * 100.0

            x_syn = x_cur * (1.0 + d_xop_r)
            iopv = nav_est_t1 * (1.0 + _W_XOP_ETF * d_xop_r) * (fn / f_us_close) if f_us_close > 0 else pre
            if iopv <= 0:
                iopv = pre
            if b0_iopv is None:
                b0_iopv = iopv
            iopv_intra = ((iopv / b0_iopv - 1.0) * 100.0) if b0_iopv > 0 else 0.0

            prem = (pr / iopv - 1.0) * 100.0
            rel_prem = prem - eq_prem_5d
            eff_buy_pm = min(prem, rel_prem) if prem <= 4.5 else prem
            eff_sell_pm = max(prem - max(0.0, min(2.2, eq_prem_5d)), rel_prem)

            lof_chg = (pr - pre) / pre * 100.0
            brn_chg = (bn / b_p15 - 1.0) * 100.0 if b_p15 > 0 else 0.0
            fx_chg = (fn / f_p15 - 1.0) * 100.0 if f_p15 > 0 else 0.0
            xop_syn_chg = (x_syn / x_syn_p15 - 1.0) * 100.0 if x_syn_p15 > 0 else 0.0

            sp_brn = lof_chg - (_BETA_BRN * brn_chg + fx_chg)
            sp_xop = lof_chg - (_W_XOP_ETF * xop_syn_chg + fx_chg)
            if sp0_xop is None:
                sp0_xop = sp_xop
            sp_xop_intra = sp_xop - sp0_xop
            sp_eff = 0.55 * sp_xop_intra + 0.45 * sp_brn
            hi_sp = max(hi_sp, sp_eff)

            coint10_brn = ((pr / (eq10_brn * (bn ** _BETA_BRN) * fn) - 1.0) * 100.0) if (eq10_brn > 0 and bn > 0 and fn > 0) else 0.0
            coint10_xop = ((pr / (eq10_xop * (max(1e-6, x_syn) ** _W_XOP_ETF) * fn) - 1.0) * 100.0) if (eq10_xop > 0 and x_syn > 0 and fn > 0) else sp_xop_intra
            coint10 = 0.5 * coint10_brn + 0.5 * coint10_xop

            sc_pair = v9p._val_at_with_ts(sc_min_ts, sc_min_px, t_dt)
            sc_now = float(sc_pair[0]) if (sc_pair is not None and abs(t_dt.timestamp() - sc_pair[1]) <= 4 * 3600) else None
            if is_live_today and is_last_bar and isinstance(live_sp_etf_spot, Mapping) and live_sp_etf_spot.get("sc_px"):
                sc_now = float(live_sp_etf_spot["sc_px"])
            sc_intra = (
                ((sc_now / sc_open_v - 1.0) * 100.0)
                if (sc_now and sc_open_v and sc_open_v > 0)
                else (((bn / b_open_td - 1.0) * 100.0) if b_open_td > 0 else 0.0)
            )
            sc_chg = ((sc_now / sc_prev - 1.0) * 100.0) if (sc_now and sc_prev and sc_prev > 0) else sc_intra

            while i162 < len(p162_rows) and p162_rows[i162][0] <= tm:
                last162 = p162_rows[i162][1]
                i162 += 1
            chg162 = ((last162 - p162_pre) / p162_pre * 100.0) if (last162 and p162_pre > 0) else None
            iopv162 = nav162_t1 * (1.0 + _W_XOP_PEER_162 * d_xop_r) * (fn / f_us_close) if (nav162_t1 > 0 and f_us_close > 0) else 0.0
            prem162 = ((last162 / iopv162 - 1.0) * 100.0) if (last162 and iopv162 > 0) else 0.0
            peer_gap = (lof_chg - chg162) if chg162 is not None else 0.0
            if chg162 is not None and peer_gap0 is None:
                peer_gap0 = peer_gap
            peer_gap_intra = (peer_gap - peer_gap0) if (chg162 is not None and peer_gap0 is not None) else 0.0
            peer_m3 = (
                (chg162 - float(bars[-3]["chg162"]))
                if (chg162 is not None and len(bars) >= 3 and bars[-3].get("chg162") is not None)
                else 0.0
            )

            vol_f = max(0.0, float(pt.get("volume") or 0.0))
            vols_hist.append(vol_f)
            avg_v20 = statistics.mean(vols_hist[-20:]) if vols_hist else 0.0
            vol_ratio = (vol_f / avg_v20) if avg_v20 > 0 else 1.0
            if vol_f > 0:
                cum_pv += pr * vol_f
                cum_v += vol_f
            avg_pt = float(pt.get("average_price") or 0.0)
            vwap_intra = avg_pt if avg_pt > 0 else (cum_pv / cum_v if cum_v > 0 else pr)
            lo_px = min(lo_px, pr)
            hi_px = max(hi_px, pr)

            macd_raw = v9p._causal_macd_fields(pts, i)
            macd_f = {
                k: (float(val) * px_scale if isinstance(val, (int, float)) and not isinstance(val, bool) else val)
                for k, val in macd_raw.items()
            }

            p1 = float(pts[i - 1]["price"]) if i >= 1 and float(pts[i - 1]["price"]) > 0 else pr
            p3 = float(pts[i - 3]["price"]) if i >= 3 and float(pts[i - 3]["price"]) > 0 else pr
            p5 = float(pts[i - 5]["price"]) if i >= 5 and float(pts[i - 5]["price"]) > 0 else pr
            prev5_brn = float(bars[-5]["brn"]) if len(bars) >= 5 else bn

            bars.append(
                {
                    "i": len(bars),
                    "time": tm,
                    "price": pr,
                    "raw_prem": prem,
                    "prem": prem,
                    "eff_buy_pm": eff_buy_pm,
                    "eff_sell_pm": eff_sell_pm,
                    "rel_prem": rel_prem,
                    "eq_prem_5d": eq_prem_5d,
                    "prem162": prem162,
                    "peer_prem_diff": prem - prem162,
                    "chg162": chg162,
                    "peer_chg": chg162,
                    "peer_gap": peer_gap,
                    "peer_gap_intra": peer_gap_intra,
                    "peer_m3": peer_m3,
                    "lof_chg": lof_chg,
                    "vwap_intra": vwap_intra,
                    "vdev": ((pr / vwap_intra - 1.0) * 100.0) if vwap_intra > 0 else 0.0,
                    "px_dd_hi": ((hi_px - pr) / hi_px * 100.0) if hi_px > 0 else 0.0,
                    "bounce_from_lo_pct": ((pr / lo_px - 1.0) * 100.0) if lo_px > 0 else 0.0,
                    "dd15_pct": ((pr / hi15_pre - 1.0) * 100.0) if hi15_pre > 0 else 0.0,
                    "dd_ma15": ((pr / ma15 - 1.0) * 100.0) if ma15 > 0 else 0.0,
                    "vol_ratio": vol_ratio,
                    "brn_m5": ((bn / prev5_brn - 1.0) * 100.0) if (len(bars) >= 5 and prev5_brn > 0) else 0.0,
                    "sp_fx": sp_eff,
                    "sp_intra": sp_xop_intra,
                    "sp_xop_intra": sp_xop_intra,
                    "sp_brn": sp_brn,
                    "sp_dd_hi": hi_sp - sp_eff,
                    "sp_xop": sp_xop,
                    "iopv": iopv,
                    "iopv_intra": iopv_intra,
                    "coint10_brn": coint10_brn,
                    "coint10_xop": coint10_xop,
                    "coint10": coint10,
                    "coint_sp_spread": coint10 - sp_eff,
                    "brn": bn,
                    "brn_chg": brn_chg,
                    "fx_chg": fx_chg,
                    "brn_pos": v9p._brent_range_position(bn, lo_h, hi_h),
                    "xop_pos": v9p._brent_range_position(x_syn, x_lo, x_hi),
                    "daytime_lead_pct": daytime_lead_pct,
                    "sc": sc_now,
                    "sc_intra": sc_intra,
                    "sc_chg": sc_chg,
                    "fx": fn,
                    "raw_macd10": float(macd_raw.get("macd10") or 0.0),
                    "raw_dmc10": float(macd_raw.get("dmc10") or 0.0),
                    **macd_f,
                    "m1": (pr - p1) / p1 * 100.0 if i >= 1 and p1 > 0 else 0.0,
                    "m3": (pr - p3) / p3 * 100.0 if i >= 3 and p3 > 0 else 0.0,
                    "m5": (pr - p5) / p5 * 100.0 if i >= 5 and p5 > 0 else 0.0,
                }
            )

        b_cut = next((b for b in reversed(bars) if b["time"] <= close_cutoff_time), bars[-1] if bars else None)
        if b_cut is not None:
            max_bpos_20d = max(recent_bpos[-20:] + [b_cut["brn_pos"]])
            max_xpos_20d = max(recent_xpos[-20:] + [xop_pos_day])
            recent_bpos.append(b_cut["brn_pos"])
            recent_xpos.append(xop_pos_day)
            close_prem_hist.append(float(b_cut["prem"]))
        else:
            max_bpos_20d = 0.5
            max_xpos_20d = 0.5

        if cur_r_brn is not None:
            ratio_brn_hist.append(cur_r_brn)
        if cur_r_xop is not None:
            ratio_xop_hist.append(cur_r_xop)
        if p1500 > 0:
            close_hist.append(p1500)

        if in_range and bars:
            b_last = bars[-1]
            xop_bull_flag = (x_ma5 >= x_ma15) or (x_cur >= x_ma15)
            days_out.append(
                {
                    "td": td,
                    "trade_date": td,
                    "ym": f"{td[:4]}-{td[4:6]}",
                    "pre": pre,
                    "pre_close": pre,
                    "ma5": ma5,
                    "ma15": ma15,
                    "p1500": float(b_last["price"]),
                    "dd15": ((pre / hi15_pre - 1.0) * 100.0) if hi15_pre > 0 else 0.0,
                    "px_scale": px_scale,
                    "eq_prem_5d": eq_prem_5d,
                    "max_bpos_20d": max_bpos_20d,
                    "max_xpos_20d": max_xpos_20d,
                    "xop_close": x_cur,
                    "xop_ov": xop_ov,
                    "xop_5d": xop_5d,
                    "xle_ov": xle_ov,
                    "spy_ov": spy_ov,
                    "iwm_ov": iwm_ov,
                    "cl_ov": cl_ov,
                    "ho_ov": ho_ov,
                    "ng_ov": ng_ov,
                    "crack_ov": crack_ov,
                    "crack_3d": crack_3d,
                    "xop_ma5": x_ma5,
                    "xop_ma15": x_ma15,
                    "xop_bull": xop_bull_flag,
                    "xop_bull_day": xop_bull_flag,
                    "xop_above_ma5": x_cur >= x_ma5,
                    "sc_open": sc_open_v,
                    "sc_prev_close": sc_prev,
                    "sc_bull": sc_bull_day,
                    "nav_t1": nav_est_t1,
                    "peer_nav_t1": nav162_t1,
                    "peer_symbol": _PEER_SYM,
                    "cross_market_anchor": "sp_oilgas_etf",
                    "bars": bars,
                }
            )

    return days_out, candidate_days


def v9_sp_oilgas_etf_close_weight(day: Mapping[str, Any], b: Mapping[str, Any], _pos: int = 0) -> tuple[float, str]:
    """5-layer causal 14:53 overnight target weight for T+0 513350 (`sp_oilgas_etf`)."""
    pm = float(b.get("raw_prem", b["prem"]))
    rel_pm = float(b.get("rel_prem", pm - float(day.get("eq_prem_5d") or 0.0)))
    pm162 = float(b.get("prem162") or 0.0)
    pr = float(b["price"])
    ma5, ma15 = float(day["ma5"]), float(day["ma15"])
    above_ma5 = pr >= ma5
    bull_ma15 = ma5 >= ma15
    non_bear_ma15 = bull_ma15 or pr >= ma15
    bp = float(b["brn_pos"])
    xp = float(b.get("xop_pos", bp))
    xop_bull = bool(day.get("xop_bull", day.get("xop_bull_day", True)))
    xop_ov = float(day.get("xop_ov") or 0.0)
    iwm_ov = float(day.get("iwm_ov") or 0.0)
    xop_5d = float(day.get("xop_5d") or 0.0)
    day_lead = float(b.get("daytime_lead_pct") or 0.0)
    sc_intra = float(b.get("sc_intra") or 0.0)
    lof_chg = float(b.get("lof_chg") or 0.0)
    vwap_intra = float(b.get("vwap_intra", pr))
    px_dd_hi = float(b.get("px_dd_hi") or 0.0)
    dd_ma15 = float(b.get("dd_ma15", (pr / ma15 - 1.0) * 100.0 if ma15 > 0 else 0.0))
    dd15 = float(b.get("dd15_pct", day.get("dd15") or 0.0))
    max_bpos_20d = float(day.get("max_bpos_20d") or bp)
    max_xpos_20d = float(day.get("max_xpos_20d") or xp)

    if pm >= 5.6:
        if (not above_ma5) or xop_ov <= 0.5 or bp >= 0.88 or (pm - pm162) >= 6.0:
            return 0.0, "极端配额溢价泡沫避险清仓(0%)"
        return 0.35, "高配额溢价强趋势轻仓留底(35%)"
    if bp >= 0.93 and (not above_ma5 or xop_ov <= -0.30 or pm >= 4.6):
        return 0.0, "布伦特极高分位泡沫/走弱清仓(0%)"
    if (not non_bear_ma15) and pm >= 3.2 and not (above_ma5 and lof_chg >= 1.2 and pr >= vwap_intra and xop_ov >= 1.0):
        return 0.0, "空头区间高溢价防杀估值清仓(0%)"
    if (not above_ma5) and pm >= 2.5 and (xop_ov <= -0.8 or day_lead <= -0.35):
        return 0.0, "破MA5外盘走弱高溢价避险(0%)"

    if not non_bear_ma15:
        if above_ma5 and (lof_chg <= 0.0 or pr < vwap_intra or px_dd_hi >= 1.50) and pm >= 2.0:
            return 0.0, "空头反抽冲高回落破均价防补跌清仓(0%)"
        debubbled = pm <= 1.85 and (rel_pm <= 0.05 or pm <= 1.55 or pm162 <= -0.35)
        deep_os = (dd_ma15 <= -3.5) or (dd15 <= -8.0) or (xop_5d <= -5.0)
        us_asia_turn = (
            (xop_ov >= 0.15 and iwm_ov >= -1.00 and day_lead >= -0.35)
            or (xop_ov >= 0.60 and day_lead >= -0.25)
            or (day_lead >= 0.50 and xop_ov >= -0.60 and sc_intra >= 0.0)
            or (lof_chg >= 1.00 and pr >= vwap_intra and xop_ov >= 0.15)
        )
        if debubbled and deep_os and us_asia_turn and bp < 0.85:
            w_v = 0.95 if (xop_ov >= 0.55 or pm162 <= -0.45) else 0.85
            return w_v, f"暴跌去泡沫超跌V反逆势重仓({int(w_v * 100)}%)"
        if above_ma5 and xop_ov >= 0.50 and pm <= 3.6 and day_lead >= -0.35 and sc_intra >= -0.60 and lof_chg > 0.0:
            return 0.95, "超跌V反收复MA5逆势延续仓(95%)"
        if bp <= 0.47 and pm <= 0.50 and rel_pm <= 0.0 and xop_ov >= -0.60 and lof_chg >= -0.40 and max_bpos_20d < 0.62:
            return 0.70, "油价中低位零泡沫企稳逆势底仓(70%)"

    if (not non_bear_ma15) and (not above_ma5):
        if (not xop_bull) and rel_pm > -1.20 and pm > 0.20:
            return 0.0, "美股XOP与场内均线双空头避险(0%)"
        if max_bpos_20d >= 0.75 or max_xpos_20d >= 0.85:
            return 0.0, "高位回落空头保护清仓(0%)"

    if above_ma5 and (non_bear_ma15 or (xop_bull and xop_ov >= 1.2 and lof_chg >= 1.5)):
        if day_lead <= -0.65 and xop_ov <= -1.0 and pm >= 1.8:
            return 0.35, "亚欧盘转弱防守减仓(35%)"
        if pm <= 4.6:
            w_b = 1.00 if (pm <= 3.0 or xop_ov >= 1.2 or day_lead >= 0.25) else 0.85
            return w_b, f"标普油气主升浪顺势持仓({int(w_b * 100)}%)"
        return 0.50, "主升浪高溢价半仓控险(50%)"

    if non_bear_ma15 and xop_bull and pm <= 2.8:
        if pm <= 0.0 and bp <= 0.60:
            return 0.90, "折价多头价值区重仓(90%)"
        if xop_ov <= -2.0 and day_lead <= -0.40:
            return 0.30, "外盘急挫防守轻仓(30%)"
        return (0.80, "多头常态底仓(80%)") if above_ma5 else (0.65, "回踩MA15防守底仓(65%)")
    if bp <= 0.42 or xp <= 0.38:
        return (0.90, "2Y底部价值区重仓(90%)") if above_ma5 else (0.75, "2Y底部震荡底仓(75%)")
    return (0.75, "站上MA5稳健底仓(75%)") if above_ma5 else (0.0, "弱势破MA5空仓避险(0%)")


def sp_oilgas_adjusted_sell_bar(day: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    """Adjust sell-side bar with quota de-biasing (`eff_sell_pm`)."""
    del day
    return {**b, "prem": float(b.get("eff_sell_pm", b["prem"]))}


def sp_oilgas_adjusted_buy_bar(day: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    """Adjust buy-side bar with `eff_buy_pm` and `162411` peer lag boost (`peer_gap_intra`)."""
    del day
    pg_i = float(b.get("peer_gap_intra") or 0.0)
    return {
        **b,
        "prem": float(b.get("eff_buy_pm", b["prem"])),
        "sp_fx": float(b["sp_fx"]) - (0.20 * abs(pg_i) if pg_i <= -0.18 else 0.0),
    }


def apply_sp_oilgas_etf_intraday_overlays(
    day: Mapping[str, Any],
    b: Mapping[str, Any],
    *,
    s_sig: bool,
    b_sig: bool,
    scalp_ok: bool,
    close_wt: float,
    sc: float = 0.90,
    has_base_shares: bool = True,
) -> tuple[bool, bool, bool, dict[str, Any]]:
    """T+0 bidirectional overlays matching task-144 (Peer 162411 + XOP/CME + SC0)."""
    del scalp_ok
    tm = str(b.get("time") or "")
    pr = float(b["price"])
    ma5 = float(day["ma5"])
    ma15 = float(day["ma15"])
    raw_prem = float(b.get("raw_prem", b["prem"]))
    sp_fx = float(b["sp_fx"])
    sp_xop = float(b.get("sp_xop") or 0.0)
    pg_intra = float(b.get("peer_gap_intra") or 0.0)
    peer_m3 = float(b.get("peer_m3") or 0.0)
    day_lead = float(b.get("daytime_lead_pct") or 0.0)
    sc_intra = float(b.get("sc_intra") or 0.0)
    xop_ov = float(day.get("xop_ov") or 0.0)
    vwap_intra = float(b.get("vwap_intra", pr))
    px_dd_hi = float(b.get("px_dd_hi") or 0.0)
    sp_dd_hi = float(b.get("sp_dd_hi") or 0.0)
    vdev = float(b.get("vdev") or 0.0)
    macd10 = float(b.get("macd10") or 0.0)
    dmc10 = float(b.get("dmc10") or 0.0)
    dc10 = bool(b.get("dc10"))
    gc10 = bool(b.get("gc10"))
    m1 = float(b.get("m1") or 0.0)

    bear_zero = (close_wt <= 0.0) and (pr < ma5)
    meta: dict[str, Any] = {}

    if (not s_sig) and has_base_shares:
        if xop_ov <= -1.20 and raw_prem >= 1.20 and sp_xop >= 0.80 and (dc10 or dmc10 <= 0.0):
            s_sig = True
            meta["sell_weak_ov_triggered"] = True
        elif (ma5 < ma15) and raw_prem >= 2.10 and px_dd_hi >= 0.90 and pr < vwap_intra and dmc10 <= 0.0:
            s_sig = True
            meta["sell_prem_triggered"] = True
        elif pg_intra >= 0.45 and sp_fx >= 0.30 * sc and (dc10 or dmc10 <= 0.0):
            s_sig = True
            meta["sell_peer_triggered"] = True

    if s_sig and (xop_ov >= 1.30 or day_lead >= 0.35) and raw_prem < 4.5 and (not dc10) and sp_fx < 0.72 * sc:
        s_sig = False
        meta["sell_guarded_sp"] = True

    if bear_zero and b_sig:
        if vdev > -0.18 or dmc10 < 0.00012 or day_lead <= -0.35 or sc_intra <= -0.50 or tm >= "13:55":
            b_sig = False
            meta["knife_blocked"] = True

    scalp_triggered = False
    if "09:36" <= tm <= "14:15" and raw_prem <= 4.8 and (not bear_zero):
        macd_turn = gc10 or (macd10 < 0.0 and dmc10 >= 0.00018 * sc and m1 >= -0.02)
        no_crash = (day_lead >= -0.45) and (sc_intra >= -0.60) and ((b.get("chg162") is None) or peer_m3 >= -0.02)
        if vdev <= -0.32 and px_dd_hi >= 0.36 and sp_dd_hi >= 0.20 and macd_turn and no_crash:
            scalp_triggered = True
            meta["scalp_lbl_override"] = "T+0日内急跌洗盘波段档(85%)"
            meta["scalp_frac_override"] = 0.85

    return s_sig, b_sig, scalp_triggered, meta


def resolve_sp_oilgas_sell_tier(day: Mapping[str, Any], b: Mapping[str, Any], close_wt: float) -> tuple[float, str]:
    """Two-tranche reverse-T sell tier (splits 50% on main rally to enable addon)."""
    raw_prem = float(b.get("raw_prem", b["prem"]))
    eff_sell_pm = float(b.get("eff_sell_pm", raw_prem))
    sp_fx = float(b["sp_fx"])
    sp_intra = float(b.get("sp_intra") or 0.0)
    pg_intra = float(b.get("peer_gap_intra") or 0.0)
    day_lead = float(b.get("daytime_lead_pct") or 0.0)
    xop_ov = float(day.get("xop_ov") or 0.0)
    pr = float(b["price"])
    ma5 = float(day["ma5"])

    frac = 1.0 if (eff_sell_pm >= 0.80 or sp_fx >= 0.65 or pg_intra >= 0.55 or close_wt <= 0.0) else 0.80
    if close_wt >= 0.80 and (xop_ov >= 1.2 or day_lead >= 0.30) and pr >= ma5 and sp_intra < 0.75 and raw_prem < 4.6:
        return min(frac, 0.50), "T+0主升分批高抛档(50%)"
    if frac >= 0.999:
        return 1.00, "A档(100%)"
    return 0.80, "B档(80%)"


def resolve_sp_oilgas_buy_tier(day: Mapping[str, Any], b_buy: Mapping[str, Any]) -> tuple[float, str]:
    """Two-tier positive-T buy fraction for T+0 513350."""
    del day
    eff_buy_pm = float(b_buy.get("eff_buy_pm", b_buy["prem"]))
    sp_fx = float(b_buy["sp_fx"])
    if eff_buy_pm <= -0.55 or sp_fx <= -0.50:
        return 1.00, "T+0超跌强共振档(100%)"
    return 0.85, "T+0滞涨补涨档(85%)"


def should_exit_sp_oilgas_short(
    day: Mapping[str, Any],
    b: Mapping[str, Any],
    *,
    entry_px: float,
    entry_sp: float,
    close_wt: float,
) -> bool:
    """Exit check for reverse-T short leg (task-144 bear protection + fast bull turnaround)."""
    pr = float(b["price"])
    ma5 = float(day["ma5"])
    ma15 = float(day["ma15"])
    sp_fx = float(b["sp_fx"])
    coint10 = float(b["coint10"])
    raw_prem = float(b.get("raw_prem", b["prem"]))
    dmc10 = float(b.get("dmc10") or 0.0)
    m5 = float(b.get("m5") or 0.0)
    macd10 = float(b.get("macd10") or 0.0)
    px_drop = ((entry_px - pr) / entry_px * 100.0) if entry_px > 0 else 0.0
    sp_drop = entry_sp - sp_fx
    bear_ma15 = (ma5 < ma15) and (pr < ma15)

    if bear_ma15 and close_wt <= 0.0:
        return px_drop >= 0.48 and sp_drop >= 0.30 and dmc10 > 0.0 and raw_prem <= 1.20
    return (
        (sp_fx <= -0.06 and coint10 <= 0.05)
        or (px_drop >= 0.28 and dmc10 >= 0.0 and (close_wt > 0.0 or px_drop >= 0.42))
        or (sp_drop >= 0.26 and px_drop >= 0.12 and dmc10 >= 0.0 and close_wt > 0.0)
        or (m5 <= -0.28 and macd10 <= -0.0018 and (close_wt > 0.0 or px_drop >= 0.38))
    )


def should_exit_sp_oilgas_long(
    day: Mapping[str, Any],
    b: Mapping[str, Any],
    *,
    entry_px: float,
    entry_sp: float,
    s_sig: bool,
    close_wt: float,
    is_scalp: bool = False,
) -> tuple[bool, str]:
    """Same-day T+0 take-profit / risk-cut exit for positive-T and dip-scalp legs."""
    del day
    tm = str(b.get("time") or "")
    pr = float(b["price"])
    sp_fx = float(b["sp_fx"])
    eff_buy_pm = float(b.get("eff_buy_pm", b["prem"]))
    dmc10 = float(b.get("dmc10") or 0.0)
    m1 = float(b.get("m1") or 0.0)
    peer_m3 = float(b.get("peer_m3") or 0.0)
    day_lead = float(b.get("daytime_lead_pct") or 0.0)
    gain = ((pr - entry_px) / entry_px * 100.0) if entry_px > 0 else 0.0
    sp_gain = sp_fx - entry_sp

    riding = (
        gain > 0.0
        and (not s_sig)
        and dmc10 > 0.00008
        and m1 >= 0.0
        and (peer_m3 >= 0.03 or day_lead >= 0.20)
        and sp_fx < 0.80
    )
    stop_hit = gain <= (-0.48 if close_wt <= 0.0 else -0.62) and dmc10 < 0.0
    bear_tp = (close_wt <= 0.0) and (
        (gain >= 0.30 and dmc10 <= 0.0)
        or (gain >= 0.46 and not riding)
        or (tm >= "14:18" and gain >= 0.14)
    )
    norm_tp = (not riding) and (
        (s_sig and gain >= 0.18)
        or ((not is_scalp) and sp_fx >= (0.12 if eff_buy_pm < 0.0 else 0.02) and gain >= 0.18)
        or (gain >= 0.45 and dmc10 <= 0.0)
        or (sp_gain >= 0.28 and gain >= 0.24 and dmc10 <= 0.0)
    )

    if stop_hit:
        return True, f"T+0破位保护止损({gain:+.2f}%)"
    if bear_tp:
        return True, f"T+0空头快进止盈({gain:+.2f}%)"
    if norm_tp:
        return True, f"T+0正T落袋止盈({gain:+.2f}%)"
    return False, ""


def build_sp_oilgas_diag_payload(day: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    """Diagnostics block for live decision & indicator endpoints."""
    return {
        "anchor_mode": "sp_oilgas_etf",
        "peer_symbol": _PEER_SYM,
        "w_xop_etf": _W_XOP_ETF,
        "w_xop_peer_162": _W_XOP_PEER_162,
        "nav_t1": round(float(day.get("nav_t1") or 0.0), 4),
        "iopv": round(float(b.get("iopv") or 0.0), 4),
        "raw_prem_pct": round(float(b.get("raw_prem", b.get("prem") or 0.0)), 3),
        "eq_prem_5d_pct": round(float(b.get("eq_prem_5d") or day.get("eq_prem_5d") or 0.0), 3),
        "eff_sell_pm_pct": round(float(b.get("eff_sell_pm") or 0.0), 3),
        "eff_buy_pm_pct": round(float(b.get("eff_buy_pm") or 0.0), 3),
        "sp_fx_pct": round(float(b.get("sp_fx") or 0.0), 3),
        "sp_xop_intra_pct": round(float(b.get("sp_xop_intra") or 0.0), 3),
        "sp_brn_pct": round(float(b.get("sp_brn") or 0.0), 3),
        "daytime_lead_pct": round(float(b.get("daytime_lead_pct") or 0.0), 3),
        "bpos_pct": round(float(b.get("brn_pos") or 0.5) * 100.0, 1),
        "xpos_pct": round(float(b.get("xop_pos") or 0.5) * 100.0, 1),
        "max_bpos_20d_pct": round(float(day.get("max_bpos_20d") or 0.5) * 100.0, 1),
        "max_xpos_20d_pct": round(float(day.get("max_xpos_20d") or 0.5) * 100.0, 1),
        "xop_ov_pct": round(float(day.get("xop_ov") or 0.0), 2),
        "cl_ov_pct": round(float(day.get("cl_ov") or 0.0), 2),
        "crack_ov_pct": round(float(day.get("crack_ov") or 0.0), 2),
        "sc_chg_pct": round(float(b.get("sc_chg") or 0.0), 2),
        "peer_162411_chg_pct": round(float(b["peer_chg"]) if b.get("peer_chg") is not None else 0.0, 2),
        "peer_162411_prem_pct": round(float(b.get("prem162") or 0.0), 2),
        "peer_gap_intra_pct": round(float(b.get("peer_gap_intra") or 0.0), 2),
        "allow_same_day_t0": True,
    }


def build_sp_oilgas_live_ui_blocks(
    day: Mapping[str, Any],
    b: Mapping[str, Any],
    meta: Mapping[str, Any],
    scalp_lbl: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build structured UI condition rows & indicator cards for 513350."""
    raw_pm = float(b.get("raw_prem", b.get("prem") or 0.0))
    eq5 = float(b.get("eq_prem_5d") or day.get("eq_prem_5d") or 0.0)
    eff_sell = float(b.get("eff_sell_pm") or (raw_pm - eq5))
    sp_fx = float(b.get("sp_fx") or 0.0)
    sp_xop_i = float(b.get("sp_xop_intra") or 0.0)
    sp_brn = float(b.get("sp_brn") or 0.0)
    xop_ov = float(day.get("xop_ov") or 0.0)
    cl_ov = float(day.get("cl_ov") or 0.0)
    crack_ov = float(day.get("crack_ov") or 0.0)
    sc_chg = float(b.get("sc_chg") or 0.0)
    pg_intra = float(b.get("peer_gap_intra") or 0.0)
    p162_chg = float(b["peer_chg"]) if b.get("peer_chg") is not None else 0.0
    bpos = float(b.get("brn_pos") or 0.5) * 100.0
    xpos = float(b.get("xop_pos") or 0.5) * 100.0
    max20 = max(float(day.get("max_bpos_20d") or 0.5) * 100.0, float(day.get("max_xpos_20d") or 0.5) * 100.0, bpos, xpos)
    cw = float(meta.get("close_weight") or 0.0)

    conds = [
        {
            "id": "sp-etf-t0-spread",
            "label": "T+0 混合相对价差 (55% XOP日内 + 45% BRN)",
            "matched": sp_fx <= -0.30 or sp_fx >= 0.40,
            "current_value": f"sp_fx {sp_fx:+.2f}% · sp_xop_intra {sp_xop_i:+.2f}% · 162411价差 {pg_intra:+.2f}%",
            "target_rule": "正T低吸 sp_fx≤-0.30% / 反T高抛 sp_fx≥+0.40%",
            "detail": f"脱敏卖溢价 {eff_sell:+.2f}% (原始溢价 {raw_pm:+.2f}%, 5日中位 {eq5:+.2f}%)",
        },
        {
            "id": "sp-etf-xop-cme",
            "label": "美股 XOP / CME 裂解与 SC0 日内动量",
            "matched": xop_ov >= 1.20 or xop_ov <= -1.20 or abs(sc_chg) >= 1.50,
            "current_value": f"XOP {xop_ov:+.2f}% · WTI {cl_ov:+.2f}% · HO裂解 {crack_ov:+.2f}% · SC0 {sc_chg:+.2f}%",
            "target_rule": "XOP≥+1.2%配合低溢价加权低吸；XOP≤-1.2%或SC0走弱触发防守",
            "detail": f"2Y分位 BRN {bpos:.1f}% / XOP {xpos:.1f}% · 20日峰值 {max20:.1f}%",
        },
    ]

    cards = [
        {
            "key": "sp_t0_spread",
            "label": "T+0 混合价差 sp_fx",
            "value": f"{sp_fx:+.2f}%",
            "raw": round(sp_fx, 3),
            "hint": f"55% XOP日内({sp_xop_i:+.2f}%) + 45% BRN({sp_brn:+.2f}%)",
            "tone": "warn" if sp_fx >= 0.40 else ("good" if sp_fx <= -0.30 else "neutral"),
        },
        {
            "key": "quota_prem",
            "label": "XOP IOPV 实时折溢价",
            "value": f"{raw_pm:+.2f}%",
            "raw": round(raw_pm, 3),
            "hint": f"5日中位均值 {eq5:+.2f}% · 脱敏卖溢价 {eff_sell:+.2f}%",
            "tone": "danger" if raw_pm >= 5.60 else ("good" if eff_sell <= -0.30 else "neutral"),
        },
        {
            "key": "xop_cme_anchor",
            "label": "美股 XOP / CME 裂解 + SC0",
            "value": f"XOP {xop_ov:+.2f}%",
            "raw": round(xop_ov, 3),
            "hint": f"WTI {cl_ov:+.2f}% · HO裂解 {crack_ov:+.2f}% · SC0 {sc_chg:+.2f}%",
            "tone": "good" if xop_ov >= 1.0 else ("danger" if xop_ov <= -1.2 else "neutral"),
        },
        {
            "key": "peer_162411",
            "label": f"同业 {_PEER_SYM} 价差",
            "value": f"{pg_intra:+.2f}%",
            "raw": round(pg_intra, 3),
            "hint": f"华宝油气 162411 日涨 {p162_chg:+.2f}% · 溢价 {float(b.get('prem162') or 0.0):+.2f}%",
            "tone": "warn" if pg_intra >= 0.45 else ("good" if pg_intra <= -0.45 else "neutral"),
        },
        {
            "key": "quantile_close",
            "label": "2Y分位 & 14:53过夜目标",
            "value": f"{cw * 100:.0f}% ({scalp_lbl or meta.get('close_label', '')})",
            "raw": round(cw * 100.0, 1),
            "hint": f"BRN {bpos:.1f}% / XOP {xpos:.1f}% · 20日峰值 {max20:.1f}%",
            "tone": "danger" if cw == 0.0 else ("good" if cw >= 0.70 else "neutral"),
        },
    ]
    return conds, cards
