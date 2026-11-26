"""160723 FOF multi-source crude engine (USO+BNO+DBO basket, USDCNH, WTI/Crack, INE SC0, Peer LOFs)."""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta
import re
import statistics
import time
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .research_replay import calculate_macd

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_LOT_SIZE = 100
_DEFAULT_FX = 6.70
_W_OIL = 0.867
_BETA = 0.68
_W_FOF = 0.88
_FOF_WEIGHTS = (0.35, 0.35, 0.30)  # USO, BNO, DBO


def _v9() -> Any:
    from . import v9_plus_engine as v9

    return v9


def load_dbo_daily_rows(macro: Any) -> list[list[Any]]:
    """Load US DBO daily closes (2y cached via V9MacroDataStore)."""
    return macro._load_us_etf_daily_rows("dbo", "DBO", "dbo_daily_2y")


def load_merged_fx_cnh_series(macro: Any) -> tuple[list[int], list[float]]:
    """Merge onshore USDCNY with 24h offshore USDCNH (offshore overrides matching timestamps)."""
    v9 = _v9()
    cny_ts, cny_px = macro.load_fx_series()

    def _fetch_cnh() -> dict[str, list[Any]]:
        daily_txt = macro.fetch_text(
            "https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/var%20x=/"
            "NewForexService.getDayKLine?symbol=fx_susdcnh",
            referer="https://finance.sina.com.cn/",
            encoding="utf-8",
        )
        min_txt: str | None = None
        try:
            min_txt = macro.fetch_text(
                "https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/var%20x=/"
                "NewForexService.getMinKline?symbol=fx_susdcnh&scale=60&datalen=1200",
                referer="https://finance.sina.com.cn/",
                encoding="utf-8",
            )
        except Exception:
            min_txt = None
        parsed = v9._parse_fx_series_payload(daily_txt, min_txt)
        if not parsed["ts"]:
            raise ValueError("empty cnh series")
        return parsed

    cnh_data = macro._get_cached_or_fetch("fx_usdcnh_series", 600.0, _fetch_cnh)
    if not (isinstance(cnh_data, Mapping) and cnh_data.get("ts")):
        return cny_ts, cny_px

    events: dict[int, float] = {int(t): float(p) for t, p in zip(cny_ts, cny_px)}
    for t_v, p_v in zip(cnh_data["ts"], cnh_data["px"]):
        if float(p_v) > 0:
            events[int(t_v)] = float(p_v)
    ts_sorted = sorted(events)
    return ts_sorted, [events[t] for t in ts_sorted]


def fetch_live_fof_crude_spot(macro: Any) -> dict[str, Any] | None:
    """Fetch live USDCNH/USDCNY, Brent, WTI, INE SC0, USO/BNO/DBO, and 501018/161129 quotes (8s cache)."""
    v9 = _v9()
    if not macro.allow_network:
        return None
    now = time.time()
    spot_key = ("global", "live_spot_fof_crude_bundle")
    cached = v9._MEMORY_MACRO_CACHE.get(spot_key)
    if cached is not None and (now - cached[0]) < 8.0:
        return dict(cached[1]) if isinstance(cached[1], Mapping) else None

    brn_px: float | None = None
    cl_px: float | None = None
    fx_cny_px: float | None = None
    fx_cnh_px: float | None = None
    sc_px: float | None = None
    sc_open: float | None = None
    sc_date: str | None = None
    sc_time: str | None = None
    uso_q: dict[str, Any] = {}
    bno_q: dict[str, Any] = {}
    dbo_q: dict[str, Any] = {}
    peer_501: dict[str, Any] = {}
    peer_161: dict[str, Any] = {}
    fetch_err: str | None = None

    try:
        raw = macro.fetch_text(
            "https://hq.sinajs.cn/list=USDCNY,fx_susdcnh,hf_OIL,hf_CL,nf_SC0,gb_uso,gb_bno,gb_dbo,sh501018,sz161129",
            referer="https://finance.sina.com.cn/",
            encoding="gbk",
            timeout=4.0,
        )
        m_cnh = re.search(r'hq_str_fx_susdcnh="([^"]+)"', raw)
        if m_cnh:
            val = float(m_cnh.group(1).split(",")[1])
            if val > 0:
                fx_cnh_px = val
        m_cny = re.search(r'hq_str_USDCNY="([^"]+)"', raw)
        if m_cny:
            val = float(m_cny.group(1).split(",")[1])
            if val > 0:
                fx_cny_px = val
        for var_name, target in (("hq_str_hf_OIL", "brn"), ("hq_str_hf_CL", "cl")):
            m_f = re.search(rf'{var_name}="([^"]+)"', raw)
            if m_f:
                val = float(m_f.group(1).split(",")[0])
                if val > 0:
                    if target == "brn":
                        brn_px = val
                    else:
                        cl_px = val
        m_sc = re.search(r'hq_str_nf_SC0="([^"]+)"', raw)
        if m_sc:
            sc_parts = m_sc.group(1).split(",")
            if len(sc_parts) > 17:
                try:
                    v_sc = float(sc_parts[8] or 0.0)
                    if v_sc > 0:
                        sc_px = v_sc
                except (TypeError, ValueError):
                    pass
                try:
                    v_op = float(sc_parts[2] or 0.0)
                    if v_op > 0:
                        sc_open = v_op
                except (TypeError, ValueError):
                    pass
                sd = str(sc_parts[17] or "").strip().replace("-", "")
                if v9._DATE_RE.fullmatch(sd):
                    sc_date = sd
                st_raw = str(sc_parts[1] or "").strip()
                if len(st_raw) >= 4 and st_raw[:4].isdigit():
                    sc_time = f"{st_raw[:2]}:{st_raw[2:4]}"
        for var_name, holder in (("hq_str_gb_uso", uso_q), ("hq_str_gb_bno", bno_q), ("hq_str_gb_dbo", dbo_q)):
            m_u = re.search(rf'{var_name}="([^"]+)"', raw)
            if m_u:
                holder.update(macro._extract_sina_us_quote(m_u.group(1).split(",")))
        for var_name, holder in (("hq_str_sh501018", peer_501), ("hq_str_sz161129", peer_161)):
            m_a = re.search(rf'{var_name}="([^"]+)"', raw)
            if m_a:
                holder.update(macro._extract_sina_a_quote(m_a.group(1).split(",")))
    except Exception as exc:
        fetch_err = str(exc)

    non_null = sum(
        1
        for v in (
            brn_px,
            cl_px,
            fx_cnh_px,
            fx_cny_px,
            sc_px,
            uso_q.get("close"),
            bno_q.get("close"),
            dbo_q.get("close"),
            peer_501.get("price"),
            peer_161.get("price"),
        )
        if v is not None
    )
    v9._record_live_spot_telemetry(
        spot_key,
        attempt_ts=now,
        ok=(non_null > 0 and fetch_err is None),
        error=fetch_err if non_null == 0 else None,
        non_null_fields=non_null,
        total_fields=10,
    )
    if non_null == 0:
        if cached is not None and isinstance(cached[1], Mapping):
            return dict(cached[1])
        return None

    bundle = {
        "brn_px": brn_px,
        "cl_px": cl_px,
        "fx_px": fx_cnh_px or fx_cny_px,
        "fx_cny_px": fx_cny_px,
        "fx_cnh_px": fx_cnh_px,
        "sc_px": sc_px,
        "sc_open": sc_open,
        "sc_date": sc_date,
        "sc_time": sc_time,
        "uso_close": uso_q.get("close"),
        "uso_reg_date": uso_q.get("reg_date"),
        "bno_close": bno_q.get("close"),
        "bno_reg_date": bno_q.get("reg_date"),
        "dbo_close": dbo_q.get("close"),
        "dbo_reg_date": dbo_q.get("reg_date"),
        "peer_501018": peer_501,
        "peer_161129": peer_161,
    }
    v9._MEMORY_MACRO_CACHE[spot_key] = (now, bundle)
    return dict(bundle)


def _compute_fof_basket_day_anchor(
    *,
    v9: Any,
    td: str,
    nav_td: str | None,
    nav_map: Mapping[str, float],
    uso_by_d: Mapping[str, float],
    bno_by_d: Mapping[str, float],
    dbo_by_d: Mapping[str, float],
    fof_dates: Sequence[str],
    fof_ts: Sequence[int],
    fx_ts: Sequence[int],
    fx_px: Sequence[float],
    brn_ts: Sequence[int],
    brn_px: Sequence[float],
    f_nav0: float,
    f_open_td: float,
    b_open_td: float | None,
    b_p15: float,
    t0930_ts: int,
    cl_by_d: Mapping[str, float],
    ho_by_d: Mapping[str, float],
) -> dict[str, Any]:
    """Compute causal T-1 NAV re-anchor and US overnight metrics from USO+BNO+DBO, WTI, and Crack."""
    fi = bisect_right(fof_ts, t0930_ts) - 1
    us_d1 = fof_dates[fi] if fi >= 0 else None
    nav_t1_est: float | None = None
    b_us1 = 0.0
    f_us1 = 0.0
    w_u, w_b, w_d = _FOF_WEIGHTS

    if nav_td and us_d1:
        nav_us_d = max((d for d in fof_dates if d <= nav_td), default=None)
        if nav_us_d is not None and nav_us_d <= us_d1:
            fof_ret = (
                w_u * (uso_by_d[us_d1] / uso_by_d[nav_us_d] - 1.0)
                + w_b * (bno_by_d[us_d1] / bno_by_d[nav_us_d] - 1.0)
                + w_d * (dbo_by_d[us_d1] / dbo_by_d[nav_us_d] - 1.0)
            )
            dt_us_nav = v9._parse_sh(nav_us_d, "23:59") + timedelta(hours=5)
            dt_us1 = v9._parse_sh(us_d1, "23:59") + timedelta(hours=5)
            f_us_nav = v9._val_at(fx_ts, fx_px, dt_us_nav) or f_nav0
            f_us1 = v9._val_at(fx_ts, fx_px, dt_us1) or f_open_td
            b_us1 = v9._val_at(brn_ts, brn_px, dt_us1) or b_open_td or b_p15
            if f_us_nav > 0 and f_us1 > 0 and b_us1 > 0:
                nav_t1_est = float(nav_map[nav_td]) * (1.0 + _W_FOF * fof_ret) * (f_us1 / f_us_nav)

    if fi >= 1 and us_d1 is not None:
        us_d0 = fof_dates[fi - 1]
        uso_ov = (uso_by_d[us_d1] / uso_by_d[us_d0] - 1.0) * 100.0
        bno_ov = (bno_by_d[us_d1] / bno_by_d[us_d0] - 1.0) * 100.0
        dbo_ov = (dbo_by_d[us_d1] / dbo_by_d[us_d0] - 1.0) * 100.0
        fof_ov = w_u * uso_ov + w_b * bno_ov + w_d * dbo_ov
        cl_ov = ((cl_by_d[us_d1] / cl_by_d[us_d0] - 1.0) * 100.0) if (us_d1 in cl_by_d and us_d0 in cl_by_d) else fof_ov
    else:
        fof_ov, cl_ov = 0.0, 0.0

    if fi >= 0:
        base_d = fof_dates[0]
        fof_s15 = [
            w_u * uso_by_d[d] / uso_by_d[base_d]
            + w_b * bno_by_d[d] / bno_by_d[base_d]
            + w_d * dbo_by_d[d] / dbo_by_d[base_d]
            for d in fof_dates[max(0, fi - 14) : fi + 1]
        ]
        fof_bull = (statistics.mean(fof_s15[-5:]) >= statistics.mean(fof_s15)) or (
            fof_s15[-1] >= statistics.mean(fof_s15)
        )
    else:
        fof_bull = True

    crack_3d = 0.0
    if fi >= 3 and us_d1 is not None:
        d_m3 = fof_dates[fi - 3]
        if us_d1 in ho_by_d and d_m3 in ho_by_d and us_d1 in cl_by_d and d_m3 in cl_by_d:
            crack_3d = (ho_by_d[us_d1] / ho_by_d[d_m3] - cl_by_d[us_d1] / cl_by_d[d_m3]) * 100.0

    return {
        "us_d1": us_d1,
        "nav_t1_est": nav_t1_est,
        "b_us1": b_us1,
        "f_us1": f_us1,
        "fof_ov": fof_ov,
        "cl_ov": cl_ov,
        "crack_3d": crack_3d,
        "fof_bull": fof_bull,
    }


def build_fof_crude_days_timeline(
    *,
    store: Any,
    symbol: str,
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
    uso_rows: Sequence[Sequence[Any]],
    bno_rows: Sequence[Sequence[Any]],
    dbo_rows: Sequence[Sequence[Any]],
    cl_rows: Sequence[Sequence[Any]],
    ho_rows: Sequence[Sequence[Any]],
    sc_daily_rows: Sequence[Sequence[Any]],
    sc_min_ts: Sequence[int],
    sc_min_px: Sequence[float],
    live_fof_spot: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Build causal feature timeline for `160723` fusing FOF basket, CNH, WTI/Crack, INE SC0, and peers."""
    v9 = _v9()
    uso_by_d = {str(r[0]): float(r[2]) for r in uso_rows if len(r) >= 3 and float(r[2]) > 0}
    bno_by_d = {str(r[0]): float(r[2]) for r in bno_rows if len(r) >= 3 and float(r[2]) > 0}
    dbo_by_d = {str(r[0]): float(r[2]) for r in dbo_rows if len(r) >= 3 and float(r[2]) > 0}
    cl_by_d = {str(r[0]): float(r[2]) for r in cl_rows if len(r) >= 3 and float(r[2]) > 0}
    ho_by_d = {str(r[0]): float(r[2]) for r in ho_rows if len(r) >= 3 and float(r[2]) > 0}
    uso_ts_m = {str(r[0]): int(r[1]) for r in uso_rows if len(r) >= 3 and float(r[2]) > 0}
    fof_dates = sorted(d for d in uso_by_d if d in bno_by_d and d in dbo_by_d)
    fof_ts = [uso_ts_m[d] for d in fof_dates]

    sc_daily_map = {
        str(r[0]): (float(r[2]), float(r[3]) if len(r) >= 4 else float(r[2]))
        for r in sc_daily_rows
        if len(r) >= 3 and float(r[2]) > 0
    }
    sc_daily_dates = sorted(sc_daily_map)
    nav_dates = sorted(str(k) for k in nav_map)

    peer_pre, peer_pts = v9._load_peer_intraday_series(
        store, ("501018", "161129"), symbol, clean_start, end_date
    )

    ratio_hist: list[float] = []
    close_hist: list[float] = []
    recent_bpos: list[float] = []
    close_prem_hist: list[float] = []
    days_out: list[dict[str, Any]] = []
    candidate_days = sum(1 for d in day_rows if str(d["trade_date"]) >= clean_start)

    for idx, day_row in enumerate(day_rows):
        td = str(day_row["trade_date"])
        raw_pts = pts_by_day.get(td, [])
        if len(raw_pts) < 2:
            continue
        prices = [float(p["price"]) for p in raw_pts]
        macd_rows = calculate_macd(prices, eff_macd)
        pts = [{**p, **m} for p, m in zip(raw_pts, macd_rows)]
        p1500 = next((p for p in reversed(prices) if p > 0), 0.0)
        b1500 = v9._val_at(brn_ts, brn_px, v9._parse_sh(td, "15:00"))
        f1500 = v9._val_at(fx_ts, fx_px, v9._parse_sh(td, "15:00")) or _DEFAULT_FX
        cur_r = p1500 / ((b1500 ** _BETA) * f1500) if (b1500 and f1500 and p1500 > 0) else None

        pre_raw = day_row["pre_close"]
        pre = float(pre_raw) if pre_raw is not None and float(pre_raw) > 0 else (close_hist[-1] if close_hist else prices[0])
        has_prev_td = idx >= 1
        prev_td = str(day_rows[idx - 1]["trade_date"]) if has_prev_td else td
        td_m2 = str(day_rows[idx - 2]["trade_date"]) if idx >= 2 else prev_td
        t0930_dt = v9._parse_sh(td, "09:30")
        t0930_ts = int(t0930_dt.timestamp())
        b_open_td = v9._val_at(brn_ts, brn_px, t0930_dt)
        f_open_td = v9._val_at(fx_ts, fx_px, t0930_dt) or _DEFAULT_FX
        nav_td = next((k for k in reversed(nav_dates) if k <= td_m2), None) if has_prev_td else None
        if nav_td is not None:
            nav_base = float(nav_map[nav_td])
            dt_nav = v9._parse_sh(nav_td, "23:59") + timedelta(hours=5)
            b_nav0 = v9._val_at(brn_ts, brn_px, dt_nav) or b_open_td or b1500 or 1.0
            f_nav0 = v9._val_at(fx_ts, fx_px, dt_nav) or f_open_td or _DEFAULT_FX
        else:
            nav_base = pre
            b_nav0 = (v9._val_at(brn_ts, brn_px, v9._parse_sh(prev_td, "15:00")) if has_prev_td else b_open_td) or b1500 or 1.0
            f_nav0 = (v9._val_at(fx_ts, fx_px, v9._parse_sh(prev_td, "15:00")) if has_prev_td else f_open_td) or _DEFAULT_FX
        b_p15 = (v9._val_at(brn_ts, brn_px, v9._parse_sh(prev_td, "15:00")) if has_prev_td else b_open_td) or b_nav0
        f_p15 = (v9._val_at(fx_ts, fx_px, v9._parse_sh(prev_td, "15:00")) if has_prev_td else f_open_td) or f_nav0

        in_range = td >= clean_start and (not end_date or td <= end_date)
        if in_range:
            eq_prem_5d = statistics.mean(close_prem_hist[-5:]) if close_prem_hist else 0.0
            eq10 = (
                statistics.mean(ratio_hist[-10:])
                if ratio_hist
                else (pre / ((b_p15 ** _BETA) * f_p15) if b_p15 > 0 and f_p15 > 0 else None)
            )
            ma5 = statistics.mean(close_hist[-5:]) if close_hist else pre
            ma15 = statistics.mean(close_hist[-15:]) if close_hist else pre
            hi15_pre = max(close_hist[-15:] + [pre]) if close_hist else pre
            lo_h, hi_h = v9._day_oil_range(oil_rows, brn_px, td, t0930_ts)

            fof_anc = _compute_fof_basket_day_anchor(
                v9=v9,
                td=td,
                nav_td=nav_td,
                nav_map=nav_map,
                uso_by_d=uso_by_d,
                bno_by_d=bno_by_d,
                dbo_by_d=dbo_by_d,
                fof_dates=fof_dates,
                fof_ts=fof_ts,
                fx_ts=fx_ts,
                fx_px=fx_px,
                brn_ts=brn_ts,
                brn_px=brn_px,
                f_nav0=f_nav0,
                f_open_td=f_open_td,
                b_open_td=b_open_td,
                b_p15=b_p15,
                t0930_ts=t0930_ts,
                cl_by_d=cl_by_d,
                ho_by_d=ho_by_d,
            )
            nav_t1_est = fof_anc["nav_t1_est"]
            b_us1 = fof_anc["b_us1"]
            f_us1 = fof_anc["f_us1"]

            sc_hist = [sc_daily_map[d][0] for d in sc_daily_dates if d < td]
            sc_prev = sc_hist[-1] if sc_hist else None
            sc_op_pair = v9._val_at_with_ts(sc_min_ts, sc_min_px, t0930_dt)
            if sc_op_pair is not None and abs(t0930_ts - sc_op_pair[1]) <= 6 * 3600:
                sc_open_v: float | None = sc_op_pair[0]
            elif td in sc_daily_map:
                sc_open_v = sc_daily_map[td][1]
            elif live_fof_spot and str(live_fof_spot.get("sc_date") or "") == td and live_fof_spot.get("sc_open"):
                sc_open_v = float(live_fof_spot["sc_open"])
            else:
                sc_open_v = sc_prev

            peer_states: dict[str, dict[str, Any]] = {}
            for p_sym in ("501018", "161129"):
                if p_sym == symbol:
                    continue
                p_pre = peer_pre.get(p_sym, {}).get(td)
                live_p = (live_fof_spot or {}).get(f"peer_{p_sym}") or {}
                if (p_pre is None or p_pre <= 0) and str(live_p.get("date") or "") == td and live_p.get("pre_close"):
                    p_pre = float(live_p["pre_close"])
                rows_p = list(peer_pts.get(p_sym, {}).get(td, []))
                if (
                    live_p
                    and str(live_p.get("date") or "") == td
                    and live_p.get("time")
                    and live_p.get("price")
                    and (not rows_p or str(live_p["time"]) > rows_p[-1][0])
                ):
                    rows_p.append((str(live_p["time"]), float(live_p["price"])))
                peer_states[p_sym] = {"pre": p_pre, "rows": rows_p, "idx": 0, "last_px": None}

            bars: list[dict[str, Any]] = []
            recent_vols: list[float] = []
            sp0: float | None = None
            b0_iopv: float | None = None
            peer_sp0: float | None = None
            cum_pv = 0.0
            cum_v = 0.0
            hi_px = 0.0
            hi_sp = -999.0
            lo_px = 999999.0
            open_gap_pct = ((prices[0] - pre) / pre * 100.0) if (pre > 0 and prices[0] > 0) else 0.0

            for i, pt in enumerate(pts):
                pr = float(pt["price"])
                if pr <= 0:
                    continue
                tm = str(pt["time"])
                t_dt = v9._parse_sh(td, tm)
                bn = v9._val_at(brn_ts, brn_px, t_dt) or b_p15
                fn = v9._val_at(fx_ts, fx_px, t_dt) or f_p15
                iopv = nav_base * (1.0 + _W_OIL * (bn / b_nav0 - 1.0)) * (fn / f_nav0) if b_nav0 > 0 and f_nav0 > 0 else pre
                prem = (pr / iopv - 1.0) * 100.0 if iopv > 0 else 0.0
                if nav_t1_est is not None and b_us1 > 0 and f_us1 > 0:
                    fof_iopv = nav_t1_est * (1.0 + _W_FOF * (bn / b_us1 - 1.0)) * (fn / f_us1)
                    fof_prem = (pr / fof_iopv - 1.0) * 100.0 if fof_iopv > 0 else prem
                else:
                    fof_iopv = iopv
                    fof_prem = prem

                brn_chg = (bn / b_p15 - 1.0) * 100.0 if b_p15 > 0 else 0.0
                daytime_brn_pct = ((bn / b_open_td - 1.0) * 100.0) if (b_open_td and b_open_td > 0) else brn_chg
                fx_chg = (fn / f_p15 - 1.0) * 100.0 if f_p15 > 0 else 0.0
                lof_chg = (pr - pre) / pre * 100.0 if pre > 0 else 0.0
                sp_fx = lof_chg - (_BETA * brn_chg + fx_chg)
                if sp0 is None:
                    sp0 = sp_fx
                    b0_iopv = iopv if iopv > 0 else pr
                coint10 = (
                    (pr / (eq10 * (bn ** _BETA) * fn) - 1.0) * 100.0
                    if eq10 and bn > 0 and fn > 0
                    else 0.0
                )

                sc_pair = v9._val_at_with_ts(sc_min_ts, sc_min_px, t_dt)
                sc_now = sc_pair[0] if (sc_pair is not None and abs(t_dt.timestamp() - sc_pair[1]) <= 4 * 3600) else None
                sc_intra = ((sc_now / sc_open_v - 1.0) * 100.0) if (sc_now and sc_open_v and sc_open_v > 0) else daytime_brn_pct
                sc_vs_prev = ((sc_now / sc_prev - 1.0) * 100.0) if (sc_now and sc_prev and sc_prev > 0) else brn_chg
                if len(sc_hist) >= 14 and sc_now and sc_now > 0:
                    sc_ma5 = statistics.mean(sc_hist[-4:] + [sc_now])
                    sc_ma15 = statistics.mean(sc_hist[-14:] + [sc_now])
                    sc_bull = (sc_ma5 >= sc_ma15) or (sc_now >= sc_ma15)
                elif len(sc_hist) >= 15:
                    sc_ma5 = statistics.mean(sc_hist[-5:])
                    sc_ma15 = statistics.mean(sc_hist[-15:])
                    sc_bull = (sc_ma5 >= sc_ma15) or (sc_hist[-1] >= sc_ma15)
                else:
                    sc_bull = sc_vs_prev >= 0.50

                p_chgs: list[float] = []
                for p_st in peer_states.values():
                    p_pre = p_st["pre"]
                    rows_p = p_st["rows"]
                    p_i = p_st["idx"]
                    while p_i < len(rows_p) and rows_p[p_i][0] <= tm:
                        p_st["last_px"] = rows_p[p_i][1]
                        p_i += 1
                    p_st["idx"] = p_i
                    if p_pre and p_pre > 0 and p_st["last_px"] is not None:
                        p_chgs.append((float(p_st["last_px"]) - p_pre) / p_pre * 100.0)
                peer_chg: float | None = statistics.mean(p_chgs) if p_chgs else None
                if peer_chg is not None and len(bars) >= 3 and bars[-3].get("peer_chg") is not None:
                    peer_m3 = peer_chg - float(bars[-3]["peer_chg"])
                else:
                    peer_m3 = 0.0
                peer_gap = (lof_chg - peer_chg) if peer_chg is not None else 0.0
                if peer_chg is not None and peer_sp0 is None:
                    peer_sp0 = peer_gap
                peer_sp_intra = (peer_gap - peer_sp0) if (peer_chg is not None and peer_sp0 is not None) else 0.0

                vol_f = float(pt.get("volume") or 0.0)
                avg_v20 = (
                    statistics.mean(recent_vols[-20:])
                    if len(recent_vols) >= 5
                    else (vol_f if vol_f > 0 else 1.0)
                )
                vol_ratio = (vol_f / avg_v20) if (vol_f > 0 and avg_v20 > 0) else 1.0
                if vol_f > 0:
                    cum_pv += pr * vol_f
                    cum_v += vol_f
                    recent_vols.append(vol_f)
                avg_raw = pt.get("average_price")
                vwap_intra = (
                    float(avg_raw)
                    if (avg_raw is not None and float(avg_raw) > 0)
                    else (cum_pv / cum_v if cum_v > 0 else pr)
                )
                lo_px = min(lo_px, pr)
                hi_px = max(hi_px, pr)
                hi_sp = max(hi_sp, sp_fx)
                prev5_brn = float(bars[-5]["brn"]) if len(bars) >= 5 else bn
                brn_m5 = (bn / prev5_brn - 1.0) * 100.0 if len(bars) >= 5 and prev5_brn > 0 else 0.0
                p1 = float(pts[i - 1]["price"]) if i >= 1 and float(pts[i - 1]["price"]) > 0 else pr
                p3 = float(pts[i - 3]["price"]) if i >= 3 and float(pts[i - 3]["price"]) > 0 else pr
                p5 = float(pts[i - 5]["price"]) if i >= 5 and float(pts[i - 5]["price"]) > 0 else pr
                bars.append(
                    {
                        "i": len(bars),
                        "time": tm,
                        "price": pr,
                        "average_price": pt.get("average_price"),
                        "volume": pt.get("volume"),
                        "vol_ratio": vol_ratio,
                        "iopv": iopv,
                        "fof_iopv": fof_iopv,
                        "prem": prem,
                        "fof_prem": fof_prem,
                        "rel_prem": prem - eq_prem_5d,
                        "eq_prem_5d": eq_prem_5d,
                        "lof_chg": lof_chg,
                        "vwap_intra": vwap_intra,
                        "vdev": (pr / vwap_intra - 1.0) * 100.0 if vwap_intra > 0 else 0.0,
                        "px_dd_hi": (hi_px - pr) / hi_px * 100.0 if hi_px > 0 else 0.0,
                        "bounce_from_lo_pct": (pr / lo_px - 1.0) * 100.0 if lo_px > 0 else 0.0,
                        "dd15_pct": (pr / hi15_pre - 1.0) * 100.0 if hi15_pre > 0 else 0.0,
                        "dd_ma15": (pr / ma15 - 1.0) * 100.0 if ma15 > 0 else 0.0,
                        "sp_dd_hi": hi_sp - sp_fx,
                        "brn_m5": brn_m5,
                        "sp_fx": sp_fx,
                        "sp_intra": sp_fx - sp0,
                        "iopv_intra": ((iopv / b0_iopv - 1.0) * 100.0) if b0_iopv else 0.0,
                        "coint10": coint10,
                        "brn": bn,
                        "brn_chg": brn_chg,
                        "daytime_brn_pct": daytime_brn_pct,
                        "brn_pos": v9._brent_range_position(bn, lo_h, hi_h),
                        "fx": fn,
                        "sc_now": sc_now,
                        "sc_intra": sc_intra,
                        "sc_vs_prev": sc_vs_prev,
                        "sc_bull": sc_bull,
                        "peer_chg": peer_chg,
                        "peer_m3": peer_m3,
                        "peer_gap": peer_gap,
                        "peer_sp_intra": peer_sp_intra,
                        **v9._causal_macd_fields(pts, i),
                        "m1": (pr - p1) / p1 * 100.0 if i >= 1 else 0.0,
                        "m3": (pr - p3) / p3 * 100.0 if i >= 3 else 0.0,
                        "m5": (pr - p5) / p5 * 100.0 if i >= 5 else 0.0,
                    }
                )

            if bars:
                b_cut = next((b for b in reversed(bars) if b["time"] <= close_cutoff_time), bars[-1])
                max_bpos_20d = max(recent_bpos[-20:] + [b_cut["brn_pos"]])
                recent_bpos.append(b_cut["brn_pos"])
                close_prem_hist.append(float(b_cut["prem"]))
                days_out.append(
                    {
                        "td": td,
                        "ym": f"{td[:4]}-{td[4:6]}",
                        "pre": pre,
                        "ma5": ma5,
                        "ma15": ma15,
                        "p1500": bars[-1]["price"],
                        "lo_hist": lo_h,
                        "hi_hist": hi_h,
                        "hi15_pre": hi15_pre,
                        "max_bpos_20d": max_bpos_20d,
                        "open_gap_pct": open_gap_pct,
                        "eq_prem_5d": eq_prem_5d,
                        "fof_ov": fof_anc["fof_ov"],
                        "cl_ov": fof_anc["cl_ov"],
                        "crack_3d": fof_anc["crack_3d"],
                        "fof_bull": fof_anc["fof_bull"],
                        "sc_prev_close": sc_prev,
                        "sc_open": sc_open_v,
                        "sc_bull": bool(b_cut.get("sc_bull", False)),
                        "cross_market_anchor": "fof_crude_core",
                        "bars": bars,
                    }
                )
        else:
            valid_pts = [pt for pt in pts if float(pt["price"]) > 0]
            if valid_pts:
                pt_cut = next((pt for pt in reversed(valid_pts) if pt["time"] <= close_cutoff_time), valid_pts[-1])
                t_cut = v9._parse_sh(td, str(pt_cut["time"]))
                bn_cut = v9._val_at(brn_ts, brn_px, t_cut) or b_p15
                fn_cut = v9._val_at(fx_ts, fx_px, t_cut) or f_p15
                iopv_cut = (
                    nav_base * (1.0 + _W_OIL * (bn_cut / b_nav0 - 1.0)) * (fn_cut / f_nav0)
                    if b_nav0 > 0 and f_nav0 > 0
                    else pre
                )
                prem_cut = (float(pt_cut["price"]) / iopv_cut - 1.0) * 100.0 if iopv_cut > 0 else 0.0
                close_prem_hist.append(prem_cut)
                lo_h, hi_h = v9._day_oil_range(oil_rows, brn_px, td, t0930_ts)
                recent_bpos.append(v9._brent_range_position(bn_cut, lo_h, hi_h))

        if cur_r is not None:
            ratio_hist.append(cur_r)
        if p1500 > 0:
            close_hist.append(p1500)

    return days_out, candidate_days


def v9_fof_crude_close_weight(
    day: Mapping[str, Any],
    b1453: Mapping[str, Any],
    _pos: int = 0,
) -> tuple[float, str]:
    """14:53 overnight position weight for `160723` fusing FOF basket IOPV, INE SC0, and crash contrarian."""
    bp = float(b1453["brn_pos"])
    pm = float(b1453["prem"])
    fpm = float(b1453.get("fof_prem", pm))
    pm_risk = max(pm, fpm)
    pr = float(b1453["price"])
    ma5 = float(day["ma5"])
    ma15 = float(day["ma15"])
    above_ma5 = pr >= ma5
    bull_ma15 = ma5 >= ma15
    non_bear_ma15 = bull_ma15 or pr >= ma15
    m20 = float(day["max_bpos_20d"])
    dd15 = float(b1453.get("dd15_pct") or 0.0)
    lof_chg = float(b1453.get("lof_chg") or 0.0)
    fof_ov = float(day.get("fof_ov") or 0.0)
    fof_bull = bool(day.get("fof_bull", False))
    sc_bull = bool(b1453.get("sc_bull", False))
    sc_intra = float(b1453.get("sc_intra") or 0.0)
    sc_vs_prev = float(b1453.get("sc_vs_prev") or 0.0)

    # 1. Broken-trend QDII bubble exit & below-MA5 risk guard
    if (not non_bear_ma15) and pm_risk >= 5.0:
        return 0.0, f"均线空头期高溢价泡沫({pm_risk:+.2f}%>=+5.0%)防补跌清仓(0%)"
    if (not above_ma5) and pm_risk >= 2.20 and (sc_intra <= -0.50 or fof_ov <= -1.20):
        return 0.0, f"跌破MA5且溢价偏高({pm_risk:+.2f}%)+外盘/沪油走弱清仓(0%)"

    # 2. Extreme oil top (>=90%)
    if bp >= 0.90:
        if above_ma5 and bull_ma15 and fof_bull and fpm <= -0.50 and sc_vs_prev >= 0.30 and bp < 0.965:
            return 0.65, f"极高油价但FOF折价({fpm:+.2f}%)且内外盘共振主升，适度留底(65%)"
        return 0.0, f"布伦特2年极高分位({bp * 100:.1f}%>=90%)收盘全平避险(0%)"

    # 3. Crash capitulation & deep oversold stabilization contrarian engine (Nov crash)
    if (not non_bear_ma15) and bp < 0.72 and pm_risk <= 1.60:
        if (lof_chg <= -6.0 and fpm <= 0.0) or (
            dd15 <= -11.5 and lof_chg <= -4.2 and fpm <= -0.85 and sc_intra >= -1.60
        ):
            return 0.90, f"暴跌恐慌出清+FOF真折价({fpm:+.2f}%)逆势抄底过夜(90%)"
        if dd15 <= -11.5 and fpm <= -0.55 and (not above_ma5):
            sc_firm = (sc_vs_prev >= 0.40 and sc_intra >= -1.20) or (sc_intra >= 0.80 and sc_vs_prev >= 0.0)
            us_firm = fof_ov >= 1.20 and sc_vs_prev >= 0.0
            if sc_firm or us_firm:
                return 0.90, f"深跌去泡沫(dd15 {dd15:+.1f}%)+FOF真折价({fpm:+.2f}%)+沪油/美油企稳逆势过夜(90%)"

    # 4. Bear & post-peak structural guards
    if bp >= 0.72 and not above_ma5:
        return 0.0, f"布伦特高位({bp * 100:.1f}%>=72%)且跌破MA5，收盘空仓避险(0%)"
    if m20 >= 0.72 and not non_bear_ma15:
        return 0.0, "高油价回落确认空头趋势(MA5<MA15)，收盘全平(0%)"
    if (not non_bear_ma15) and (not above_ma5) and bp > 0.45 and fpm > -1.50:
        return 0.0, f"空头趋势下无深折价保护(FOF溢价{fpm:+.2f}%)，收盘避险空仓(0%)"

    # 5. Pullback below-MA5 risk trim when US FOF & INE SC0 weaken together
    if not above_ma5:
        if m20 >= 0.75 and (fof_ov <= -2.0 or sc_vs_prev <= -1.5 or sc_intra <= -1.0):
            return 0.0, f"高位回踩破MA5且美油({fof_ov:+.2f}%)/沪油({sc_intra:+.2f}%)转弱清仓避险(0%)"
        if fpm >= 0.0 and fof_ov <= -1.5 and sc_intra <= -0.8:
            return 0.0, f"破MA5无折价垫({fpm:+.2f}%)且美油与沪油双弱避险(0%)"

    # 6. High-oil bull trend breakout retention (72% <= bp < 90%)
    if bp >= 0.72:
        if above_ma5 and bull_ma15 and (fof_bull or sc_bull) and fpm <= 0.80 and pm <= 2.20 and sc_vs_prev > -1.50:
            w = 0.95 if (fpm <= -0.30 or sc_vs_prev >= 0.80) else 0.85
            return w, f"高油价但FOF低估与沪油多头共振守住MA5({bp * 100:.1f}%)，顺势留底({w * 100:.0f}%)"
        w = 0.45 if fpm <= -0.30 else 0.35
        return w, f"布伦特偏高但维持MA5上方({bp * 100:.1f}%)，轻仓留底({w * 100:.0f}%)"

    # 7. Low & neutral allocation
    if bp <= 0.42:
        w = 1.00 if (fpm <= 1.20 or above_ma5) else 0.85
        return w, f"布伦特2年低位区({bp * 100:.1f}%<=42%)，高仓位留底过夜({w * 100:.0f}%)"
    if (pm <= -0.30 or fpm <= -0.30) and non_bear_ma15:
        w = 1.00 if above_ma5 else 0.85
        return w, f"负溢价趋势安全区(名义{pm:+.2f}%/FOF{fpm:+.2f}%且非空头)，重仓留底({w * 100:.0f}%)"
    w = 0.90 if (above_ma5 and bull_ma15 and fof_bull and fpm <= 0.50) else (0.80 if above_ma5 else 0.55)
    if pm_risk >= 3.00 and not above_ma5:
        w = 0.30
    return w, f"中枢常态区间(BRN分位{bp * 100:.1f}%, FOF溢价{fpm:+.2f}%)，均衡底仓({w * 100:.0f}%)"


def fof_adjusted_buy_bar(day: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    """Use causal FOF basket discount & peer lag to prevent nominal T-2 premium from blocking genuine dips."""
    pm = float(b["prem"])
    fpm = float(b.get("fof_prem", pm))
    eff_pm = min(pm, fpm)
    eq5 = float(day.get("eq_prem_5d") or 0.0)
    rel_pm = float(b.get("rel_prem", pm))
    if eq5 >= 1.20 and rel_pm <= -0.60 and fpm <= 1.80:
        eff_pm = min(eff_pm, rel_pm)
    sp_raw = float(b["sp_fx"])
    pg = float(b.get("peer_gap") or 0.0)
    eff_sp = sp_raw + (0.25 * pg if (sp_raw < 0.0 and pg <= -0.20) else 0.0)
    return {**b, "prem": eff_pm, "sp_fx": eff_sp}


def apply_fof_crude_intraday_overlays(
    day: Mapping[str, Any],
    b: Mapping[str, Any],
    *,
    s_sig: bool,
    b_sig: bool,
    scalp_ok: bool,
    close_wt: float,
    sc: float = 1.0,
    has_base_shares: bool = False,
) -> tuple[bool, bool, bool, dict[str, Any]]:
    """Apply zero-overnight bear knife guard and 3-channel contrarian dip-T engine for 160723."""
    t = str(b.get("time") or "")
    pr = float(b.get("price") or 0.0)
    ma5 = float(day.get("ma5") or 0.0)
    sc_intra = float(b.get("sc_intra") or 0.0)
    iopv_intra = float(b.get("iopv_intra") or 0.0)
    peer_chg = b.get("peer_chg")
    peer_m3 = float(b.get("peer_m3") or 0.0)

    bear_zero = (float(close_wt) <= 0.0) and (ma5 > 0.0) and (pr < ma5)
    knife_reasons: list[str] = []
    if bear_zero:
        if t >= "13:30":
            knife_reasons.append("13:30后0%避险态破MA5禁接飞刀")
        if sc_intra <= -0.45:
            knife_reasons.append(f"沪原油SC日内走弱({sc_intra:+.2f}%)")
        if iopv_intra <= -0.15:
            knife_reasons.append(f"IOPV日内走弱({iopv_intra:+.2f}%)")
        if peer_chg is not None and peer_m3 < -0.02:
            knife_reasons.append(f"同类原油LOF 3m下挫({peer_m3:+.2f}%)")

    knife_blocked = bool(knife_reasons)
    if knife_blocked:
        b_sig = False
        scalp_ok = False

    channels: list[str] = []
    if (not b_sig) and (not scalp_ok) and (not knife_blocked) and ("09:50" <= t <= "13:25"):
        dmc = float(b.get("dmc10") or 0.0)
        m1 = float(b.get("m1") or 0.0)
        gc10 = bool(b.get("gc10"))
        bounce_lo = float(b.get("bounce_from_lo_pct") or 0.0)
        fpm = float(b.get("fof_prem", b["prem"]))
        pm = float(b["prem"])
        sp = float(b["sp_fx"])
        vdev = float(b.get("vdev") or 0.0)
        px_dd_hi = float(b.get("px_dd_hi") or 0.0)
        dd15 = float(b.get("dd15_pct") or 0.0)
        vol_ratio = float(b.get("vol_ratio") or 1.0)
        eq5 = float(day.get("eq_prem_5d") or 0.0)
        rel_pm = float(b.get("rel_prem", pm))
        peer_gap = float(b.get("peer_gap") or 0.0)

        micro_up = gc10 or (dmc >= 0.00018 * sc and m1 >= 0.0 and bounce_lo >= 0.10)
        sc_peer_ok = (sc_intra >= -0.15) and (iopv_intra >= -0.05) and (peer_chg is None or peer_m3 >= 0.0)

        if (
            peer_chg is not None
            and peer_gap <= -0.22
            and peer_m3 >= 0.03
            and fpm <= 0.40
            and pm <= 2.20
            and sp <= -0.15
            and dmc >= 0.00015 * sc
            and m1 >= 0.0
            and sc_peer_ok
        ):
            channels.append("c_peer_lag")

        if (
            bear_zero
            and dd15 <= -10.0
            and vdev <= -0.35
            and px_dd_hi >= 0.38
            and fpm <= -0.20
            and (vol_ratio >= 1.30 or sc_intra >= 0.25)
            and micro_up
            and sc_peer_ok
        ):
            channels.append("c_panic_vol")

        if (
            (not has_base_shares)
            and eq5 >= 1.00
            and rel_pm <= -0.80
            and fpm <= 1.50
            and sp <= -0.45 * sc
            and vdev <= -0.20
            and micro_up
            and sc_peer_ok
        ):
            channels.append("c_debubble_rel")

    contrarian_triggered = bool(channels)
    meta: dict[str, Any] = {
        "knife_blocked": knife_blocked,
        "knife_reasons": knife_reasons,
        "contrarian_triggered": contrarian_triggered,
        "contrarian_channels": channels,
    }
    if contrarian_triggered:
        ch_lbl_map = {"c_peer_lag": "同业领涨", "c_panic_vol": "放量暴跌反抽", "c_debubble_rel": "去泡沫相对折价"}
        ch_lbl = "/".join(ch_lbl_map.get(c, c) for c in channels)
        scalp_ok = True
        meta["scalp_lbl_override"] = f"FOF多源逆势做T档({ch_lbl}·90%)"
        meta["scalp_frac_override"] = 0.90

    return s_sig, b_sig, scalp_ok, meta


def build_fof_crude_diag_payload(day: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    """Build JSON diagnostics fields for `fof_crude_core`."""
    pm = float(b["prem"])
    peer_chg = b.get("peer_chg")
    sc_now = b.get("sc_now")
    return {
        "fof_prem_pct": round(float(b.get("fof_prem", pm)), 3),
        "eq_prem_5d_pct": round(float(day.get("eq_prem_5d") or 0.0), 3),
        "rel_prem_pct": round(float(b.get("rel_prem", pm)), 3),
        "fof_ov_pct": round(float(day.get("fof_ov") or 0.0), 2),
        "wti_ov_pct": round(float(day.get("cl_ov") or 0.0), 2),
        "crack_3d_pct": round(float(day.get("crack_3d") or 0.0), 2),
        "fof_bull": bool(day.get("fof_bull")),
        "sc_bull": bool(b.get("sc_bull")),
        "sc_now": round(float(sc_now), 2) if sc_now is not None else None,
        "sc_intra_pct": round(float(b.get("sc_intra") or 0.0), 2),
        "sc_vs_prev_pct": round(float(b.get("sc_vs_prev") or 0.0), 2),
        "peer_chg_pct": round(float(peer_chg), 3) if peer_chg is not None else None,
        "peer_m3_pct": round(float(b.get("peer_m3") or 0.0), 3),
        "peer_gap_pct": round(float(b.get("peer_gap") or 0.0), 3),
        "dd15_pct": round(float(b.get("dd15_pct") or 0.0), 2),
        "vol_ratio": round(float(b.get("vol_ratio") or 1.0), 2),
    }


def build_fof_crude_live_ui_blocks(
    day: Mapping[str, Any],
    b: Mapping[str, Any],
    meta: Mapping[str, Any],
    scalp_lbl: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build buy checklist item `v9-buy-fof-contrarian` and 2 market indicator cards."""
    pm = float(b["prem"])
    fpm = float(b.get("fof_prem", pm))
    rel_pm = float(b.get("rel_prem", pm))
    eq5 = float(day.get("eq_prem_5d") or 0.0)
    fof_ov = float(day.get("fof_ov") or 0.0)
    sc_now = b.get("sc_now")
    sc_intra = float(b.get("sc_intra") or 0.0)
    sc_vs_prev = float(b.get("sc_vs_prev") or 0.0)
    peer_chg = b.get("peer_chg")
    peer_m3 = float(b.get("peer_m3") or 0.0)
    peer_gap = float(b.get("peer_gap") or 0.0)
    dd15 = float(b.get("dd15_pct") or 0.0)

    knife_blk = bool(meta.get("knife_blocked"))
    knife_rsns = list(meta.get("knife_reasons") or [])
    c_trig = bool(meta.get("contrarian_triggered"))
    c_chs = list(meta.get("contrarian_channels") or [])
    peer_desc = f"{float(peer_chg):+.2f}%(3m{peer_m3:+.2f}%)" if peer_chg is not None else "暂无"

    buy_cond = {
        "id": "v9-buy-fof-contrarian",
        "label": "FOF多源篮子(USO/BNO/DBO)+沪SC0+同类联动逆势/防飞刀",
        "matched": c_trig or ((not knife_blk) and (fpm <= 0.40 or rel_pm <= -0.60)),
        "current_value": (
            f"FOF真实溢价 {fpm:+.2f}% (5d偏离 {rel_pm:+.2f}%), SC日内 {sc_intra:+.2f}%, "
            f"同类 {peer_desc}, dd15 {dd15:+.1f}%"
        ),
        "target_rule": "09:50-13:25 触发同业领涨/暴跌放量反抽/去泡沫相对折价通道，且0%破MA5无飞刀风险",
        "detail": (
            f"触发FOF多源逆势通道({'/'.join(c_chs)}): {scalp_lbl}"
            if c_trig
            else (f"触发防飞刀拦截：{'；'.join(knife_rsns)}" if knife_blk else "通过FOF多源与沪SC/同类LOF风控检查")
        ),
    }

    cards = [
        {
            "key": "fof_prem",
            "label": "FOF篮子(USO/BNO/DBO)真实溢价",
            "value": f"{fpm:+.2f}%",
            "raw": round(fpm, 3),
            "hint": (
                f"5日均衡({eq5:+.2f}%)偏离 {rel_pm:+.2f}% · 昨夜美股篮子 {fof_ov:+.2f}% · "
                f"多头{'是' if day.get('fof_bull') else '否'} · dd15 {dd15:+.1f}%"
            ),
        },
        {
            "key": "sc_peer_fof",
            "label": "沪SC0 / 同类原油LOF联动",
            "value": f"SC {sc_intra:+.2f}% / 同类 {f'{float(peer_chg):+.2f}%' if peer_chg is not None else '--'}",
            "raw": round(sc_intra, 3),
            "hint": (
                f"SC现价 {f'{float(sc_now):.2f}' if sc_now is not None else '--'} (较昨收 {sc_vs_prev:+.2f}%, "
                f"{'多' if b.get('sc_bull') else '空'}) · 同类3m {peer_m3:+.2f}% · 差 {peer_gap:+.2f}%"
            ),
        },
    ]
    return buy_cond, cards
