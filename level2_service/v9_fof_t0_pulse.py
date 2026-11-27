"""160723 Dedicated T+0 Dual-Bucket Multi-Source Engine (`fof_t0_multisource`).

Built specifically for `160723` (嘉实原油 LOF — SZSE QDII-LOF supporting
unlimited same-day T+0 turnaround with 0% stamp duty and 0.01% round-trip fee):

1. Dual-Bucket T+0 Capital Architecture (`v9_fof_t0_pulse_close_weight`):
   - Normal bull sessions cap 14:53 overnight retention at 85%, reserving >=15%
     dedicated T+0 dry-powder cash for next-morning positive-T dip sweeps while
     keeping base inventory for reverse-T high-sells.
   - Extreme 2-year Brent highs (>=90%) flatten to 0% overnight, shifting 100%
     of capital into pure intraday T+0 turnaround (immune to overnight gap-downs).
   - Extreme crash capitulation (`dd15 <= -11.5%` and `fof_prem <= 0%`) allows
     90% contrarian overnight positioning.
2. 10-Source Causal Data Fusion (`build_fof_t0_pulse_days_timeline`):
   - Extends `v9_fof_crude` (35%USO+35%BNO+30%DBO T-1 IOPV, 24h USDCNH, INE SC0,
     501018/161129 peers, BRN0Y, 160723 1m/L1) with US E&P (`XOP/XLE/SPY/IWM`),
     CME 1m WTI `CL` intraday momentum (`cl_intra`), and 3rd peer `162411`
     intraday 1m/3m momentum (`p162_chg`, `p162_m3`).
3. Asymmetric T+0 Intraday Overlays & Zero-Wear Exits:
   - Anti-high-chase filter blocking late afternoon new longs (>13:55) and
     above-VWAP intraday spread chasing (`sp_intra >= +0.30%`, `vdev > -0.25%`).
   - Discount-regime VWAP/spread stretch reverse-T & weak US XOP morning high-sell.
   - Quota-premium intraday dip T+0 channel (`prem > 2.20%`, EOD flat if weak).
   - Tri-peer (`501018` + `161129` + `162411`) + `SC0` + `BRN0Y` resonant catch-up.
   - Zero-loss non-stop take-profit guard (`gain_pct > 0.06%`) eliminating
     microstructure spread wear, plus fast short cover to recycle T+0 cash.
"""

from __future__ import annotations

from bisect import bisect_right
import re
import time
from typing import Any, Mapping, Sequence

from . import v9_fof_crude as fof_base

_FOF_T0_LIVE_SPOT_KEY = "live_spot_fof_t0_pulse_bundle"
_FOF_T0_LIVE_SPOT_URL = (
    "https://hq.sinajs.cn/list="
    "USDCNY,fx_susdcnh,hf_OIL,hf_CL,hf_HO,nf_SC0,"
    "gb_uso,gb_bno,gb_dbo,gb_xop,sh501018,sz161129,sz162411"
)


def _v9() -> Any:
    from . import v9_plus_engine as v9

    return v9


def _parse_hf_first(raw: str, var_name: str, *, lo: float = 0.05, hi: float = 5000.0) -> float | None:
    m_f = re.search(rf'{var_name}="([^"]+)"', raw)
    if not m_f:
        return None
    try:
        val = float(m_f.group(1).split(",")[0])
    except (TypeError, ValueError, IndexError):
        return None
    return val if (lo <= val <= hi) else None


def fetch_live_fof_t0_spot(macro: Any, *, ttl_seconds: float = 8.0) -> dict[str, Any] | None:
    """Fetch 13-in-1 Sina spot bundle for 160723 T+0 multi-source engine (8s cache)."""
    v9 = _v9()
    now = time.time()
    spot_key = ("global", _FOF_T0_LIVE_SPOT_KEY)
    cached = v9._MEMORY_MACRO_CACHE.get(spot_key)
    if cached is not None and (now - float(cached[0])) < ttl_seconds and isinstance(cached[1], Mapping):
        return dict(cached[1])
    if not getattr(macro, "allow_network", True):
        return dict(cached[1]) if (cached is not None and isinstance(cached[1], Mapping)) else None

    brn_px: float | None = None
    cl_px: float | None = None
    ho_px: float | None = None
    fx_cny_px: float | None = None
    fx_cnh_px: float | None = None
    sc_px: float | None = None
    sc_open: float | None = None
    sc_date: str | None = None
    sc_time: str | None = None
    uso_q: dict[str, Any] = {}
    bno_q: dict[str, Any] = {}
    dbo_q: dict[str, Any] = {}
    xop_q: dict[str, Any] = {}
    peer_501: dict[str, Any] = {}
    peer_161: dict[str, Any] = {}
    peer_162: dict[str, Any] = {}
    fetch_err: str | None = None

    try:
        raw = macro.fetch_text(
            _FOF_T0_LIVE_SPOT_URL,
            referer="https://finance.sina.com.cn/",
            encoding="gbk",
            timeout=4.0,
        )
        m_cnh = re.search(r'hq_str_fx_susdcnh="([^"]+)"', raw)
        if m_cnh:
            try:
                val = float(m_cnh.group(1).split(",")[1])
                if val > 0:
                    fx_cnh_px = val
            except (TypeError, ValueError, IndexError):
                pass
        m_cny = re.search(r'hq_str_USDCNY="([^"]+)"', raw)
        if m_cny:
            try:
                val = float(m_cny.group(1).split(",")[1])
                if val > 0:
                    fx_cny_px = val
            except (TypeError, ValueError, IndexError):
                pass

        brn_px = _parse_hf_first(raw, "hq_str_hf_OIL", lo=10.0, hi=500.0)
        cl_px = _parse_hf_first(raw, "hq_str_hf_CL", lo=10.0, hi=500.0)
        ho_px = _parse_hf_first(raw, "hq_str_hf_HO", lo=0.2, hi=30.0)

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

        for var_name, holder in (
            ("hq_str_gb_uso", uso_q),
            ("hq_str_gb_bno", bno_q),
            ("hq_str_gb_dbo", dbo_q),
            ("hq_str_gb_xop", xop_q),
        ):
            m_u = re.search(rf'{var_name}="([^"]+)"', raw)
            if m_u:
                holder.update(macro._extract_sina_us_quote(m_u.group(1).split(",")))

        for var_name, holder in (
            ("hq_str_sh501018", peer_501),
            ("hq_str_sz161129", peer_161),
            ("hq_str_sz162411", peer_162),
        ):
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
            ho_px,
            fx_cnh_px,
            fx_cny_px,
            sc_px,
            uso_q.get("close"),
            bno_q.get("close"),
            dbo_q.get("close"),
            xop_q.get("close"),
            peer_501.get("price"),
            peer_161.get("price"),
            peer_162.get("price"),
        )
        if v is not None
    )
    v9._record_live_spot_telemetry(
        spot_key,
        attempt_ts=now,
        ok=(non_null > 0 and fetch_err is None),
        error=fetch_err if non_null == 0 else None,
        non_null_fields=non_null,
        total_fields=13,
    )
    if non_null == 0:
        if cached is not None and isinstance(cached[1], Mapping):
            return dict(cached[1])
        return None

    bundle = {
        "brn_px": brn_px,
        "cl_px": cl_px,
        "ho_px": ho_px,
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
        "xop_close": xop_q.get("close"),
        "xop_reg_date": xop_q.get("reg_date"),
        "peer_501018": peer_501,
        "peer_161129": peer_161,
        "peer_162411": peer_162,
    }
    v9._MEMORY_MACRO_CACHE[spot_key] = (now, bundle)
    return dict(bundle)


def _daily_ov_map(rows: Sequence[Sequence[Any]]) -> tuple[list[str], list[int], dict[str, float]]:
    by_d = {str(r[0]): float(r[2]) for r in rows if len(r) >= 3 and float(r[2]) > 0}
    ts_m = {str(r[0]): int(r[1]) for r in rows if len(r) >= 3 and float(r[2]) > 0}
    dates = sorted(by_d)
    return dates, [ts_m[d] for d in dates], by_d


def _causal_ov_pct(dates: Sequence[str], ts_list: Sequence[int], by_d: Mapping[str, float], t0930_ts: int) -> float:
    idx = bisect_right(ts_list, t0930_ts) - 1
    if idx < 1:
        return 0.0
    d1, d0 = dates[idx], dates[idx - 1]
    v0 = by_d.get(d0) or 0.0
    v1 = by_d.get(d1) or 0.0
    return ((v1 / v0 - 1.0) * 100.0) if (v0 > 0 and v1 > 0) else 0.0


def build_fof_t0_pulse_days_timeline(
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
    xop_rows: Sequence[Sequence[Any]],
    xle_rows: Sequence[Sequence[Any]],
    spy_rows: Sequence[Sequence[Any]],
    iwm_rows: Sequence[Sequence[Any]],
    cl_rows: Sequence[Sequence[Any]],
    ho_rows: Sequence[Sequence[Any]],
    cl_1m: tuple[Sequence[int], Sequence[float]],
    sc_daily_rows: Sequence[Sequence[Any]],
    sc_min_ts: Sequence[int],
    sc_min_px: Sequence[float],
    live_fof_t0_spot: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Build strictly causal 10-source feature timeline for `fof_t0_multisource`."""
    v9 = _v9()
    days_out, candidate_days = fof_base.build_fof_crude_days_timeline(
        store=store,
        symbol=symbol,
        day_rows=day_rows,
        pts_by_day=pts_by_day,
        clean_start=clean_start,
        end_date=end_date,
        close_cutoff_time=close_cutoff_time,
        eff_macd=eff_macd,
        brn_ts=brn_ts,
        brn_px=brn_px,
        fx_ts=fx_ts,
        fx_px=fx_px,
        oil_rows=oil_rows,
        nav_map=nav_map,
        uso_rows=uso_rows,
        bno_rows=bno_rows,
        dbo_rows=dbo_rows,
        cl_rows=cl_rows,
        ho_rows=ho_rows,
        sc_daily_rows=sc_daily_rows,
        sc_min_ts=sc_min_ts,
        sc_min_px=sc_min_px,
        live_fof_spot=live_fof_t0_spot,
    )
    if not days_out:
        return days_out, candidate_days

    xop_d, xop_ts, xop_m = _daily_ov_map(xop_rows)
    xle_d, xle_ts, xle_m = _daily_ov_map(xle_rows)
    spy_d, spy_ts, spy_m = _daily_ov_map(spy_rows)
    iwm_d, iwm_ts, iwm_m = _daily_ov_map(iwm_rows)

    cl_1m_ts = list(cl_1m[0])
    cl_1m_px = list(cl_1m[1])
    if live_fof_t0_spot and live_fof_t0_spot.get("cl_px"):
        try:
            lv_cl = float(live_fof_t0_spot["cl_px"])
        except (TypeError, ValueError):
            lv_cl = 0.0
        if lv_cl > 0:
            now_ts = int(time.time())
            if not cl_1m_ts or now_ts > cl_1m_ts[-1]:
                cl_1m_ts.append(now_ts)
                cl_1m_px.append(lv_cl)

    peer162_pre, peer162_pts = v9._load_peer_intraday_series(
        store, ("162411",), symbol, clean_start, end_date
    )

    for day in days_out:
        day["cross_market_anchor"] = "fof_t0_multisource"
        td = str(day["td"])
        t0930_dt = v9._parse_sh(td, "09:30")
        t0930_ts = int(t0930_dt.timestamp())

        day["xop_ov"] = _causal_ov_pct(xop_d, xop_ts, xop_m, t0930_ts)
        day["xle_ov"] = _causal_ov_pct(xle_d, xle_ts, xle_m, t0930_ts)
        day["spy_ov"] = _causal_ov_pct(spy_d, spy_ts, spy_m, t0930_ts)
        day["iwm_ov"] = _causal_ov_pct(iwm_d, iwm_ts, iwm_m, t0930_ts)

        cl_op_p = v9._val_at_with_ts(cl_1m_ts, cl_1m_px, t0930_dt)
        cl_open = cl_op_p[0] if (cl_op_p is not None and abs(t0930_ts - cl_op_p[1]) <= 6 * 3600) else None

        p162_pre = peer162_pre.get("162411", {}).get(td)
        live_162 = (live_fof_t0_spot or {}).get("peer_162411") or {}
        if (p162_pre is None or p162_pre <= 0) and str(live_162.get("date") or "") == td and live_162.get("pre_close"):
            p162_pre = float(live_162["pre_close"])
        rows_162 = list(peer162_pts.get("162411", {}).get(td, []))
        if (
            live_162
            and str(live_162.get("date") or "") == td
            and live_162.get("time")
            and live_162.get("price")
            and (not rows_162 or str(live_162["time"]) > rows_162[-1][0])
        ):
            rows_162.append((str(live_162["time"]), float(live_162["price"])))

        p_idx = 0
        last_p162: float | None = None
        hi_spi = -999.0
        bars = day["bars"]
        for idx_b, b in enumerate(bars):
            tm = str(b["time"])
            b_dt = v9._parse_sh(td, tm)
            while p_idx < len(rows_162) and rows_162[p_idx][0] <= tm:
                last_p162 = rows_162[p_idx][1]
                p_idx += 1
            p162_chg = ((last_p162 - p162_pre) / p162_pre * 100.0) if (p162_pre and p162_pre > 0 and last_p162) else None
            b["p162_chg"] = p162_chg
            if p162_chg is not None and idx_b >= 3 and bars[idx_b - 3].get("p162_chg") is not None:
                b["p162_m3"] = p162_chg - float(bars[idx_b - 3]["p162_chg"])
            else:
                b["p162_m3"] = 0.0

            cl_p = v9._val_at_with_ts(cl_1m_ts, cl_1m_px, b_dt)
            b["cl_intra"] = (
                ((cl_p[0] / cl_open - 1.0) * 100.0)
                if (cl_p is not None and cl_open and cl_open > 0 and abs(b_dt.timestamp() - cl_p[1]) <= 3600)
                else float(b.get("daytime_brn_pct") or 0.0)
            )
            spi_val = float(b.get("sp_intra") or 0.0)
            hi_spi = max(hi_spi, spi_val)
            b["spi_from_hi"] = hi_spi - spi_val

    return days_out, candidate_days


def fof_t0_adjusted_buy_bar(day: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    """Causal buy-side FOF discount & peer-lag spread normalization."""
    return fof_base.fof_adjusted_buy_bar(day, b)


def v9_fof_t0_pulse_close_weight(
    day: Mapping[str, Any],
    b1453: Mapping[str, Any],
    _pos: int = 0,
    *,
    ov_cap_norm: float = 0.85,
) -> tuple[float, str]:
    """Dual-Bucket T+0 14:53 overnight target: 0% at Brent >=90%, max 85% normal (15% T+0 reserve)."""
    bp = float(b1453["brn_pos"])
    if bp >= 0.90:
        return 0.0, f"布伦特极高分位({bp * 100:.1f}%>=90%)收盘全平转纯T+0(0%)"
    w, lbl = fof_base.v9_fof_crude_close_weight(day, b1453, _pos)
    fpm = float(b1453.get("fof_prem", b1453["prem"]))
    dd15 = float(b1453.get("dd15_pct") or 0.0)
    is_crash_contrarian = (dd15 <= -11.5) and (fpm <= 0.0)
    if w > ov_cap_norm and not is_crash_contrarian:
        reserve_pct = round((1.0 - ov_cap_norm) * 100.0)
        return ov_cap_norm, f"{lbl} · 预留{reserve_pct:.0f}%专属T+0机动金"
    return w, lbl


def should_suppress_fof_t0_short_entry(b: Mapping[str, Any]) -> bool:
    """Suppress late-afternoon new reverse-T shorts lacking time/VWAP room before 14:53 close."""
    t = str(b.get("time") or "")
    vdev = float(b.get("vdev") or 0.0)
    return bool(t > "14:10" or (t >= "13:30" and vdev < 0.18))


def resolve_fof_t0_sell_tier(day: Mapping[str, Any], b: Mapping[str, Any]) -> tuple[float, str]:
    """High-conviction T+0 reverse-T sell sizing preserving two-tranche label when applicable."""
    v9 = _v9()
    t_lbl, base_frac = v9.clean_sell_tier(day, b)
    sp = float(b.get("sp_fx") or 0.0)
    spi = float(b.get("sp_intra") or 0.0)
    vdev = float(b.get("vdev") or 0.0)
    frac = max(float(base_frac), 0.95 if (sp >= 0.45 or spi >= 0.45 or vdev >= 0.45) else 0.85)
    return frac, t_lbl


def resolve_fof_t0_buy_tier(day: Mapping[str, Any], b_buy: Mapping[str, Any]) -> tuple[float, str]:
    """Aggressive T+0 dry-powder deployment for positive-T dips."""
    v9 = _v9()
    t_lbl, base_frac = v9.clean_buy_tier(day, b_buy)
    frac = 1.00 if float(base_frac) >= 0.80 else 0.92
    return frac, f"{t_lbl}·T+0双池"


def resolve_fof_t0_scalp_frac(s_frac: float) -> float:
    """Upgrade T+0 dip-scalp cash utilization to 92%-100%."""
    return 1.00 if float(s_frac) >= 0.80 else 0.92


def apply_fof_t0_pulse_intraday_overlays(
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
    """Apply T+0 anti-chase guards, discount spike reverse-T, quota-dip T+0, and tri-peer resonance."""
    del has_base_shares  # Dual-bucket reserve cash stays eligible for c_debubble_rel even with base shares.
    t = str(b.get("time") or "")
    in_long_win = ("09:32" <= t <= "11:29") or ("13:02" <= t <= "13:55")
    below_ma15 = float(day.get("ma5") or 0.0) < float(day.get("ma15") or 0.0)
    fof_ov = float(day.get("fof_ov") or 0.0)
    xop_ov = float(day.get("xop_ov") or 0.0)

    spi = float(b.get("sp_intra") or 0.0)
    vdev = float(b.get("vdev") or 0.0)
    sp = float(b.get("sp_fx") or 0.0)
    pm = float(b.get("prem") or 0.0)
    fpm = float(b.get("fof_prem", pm))
    px_dd_hi = float(b.get("px_dd_hi") or 0.0)
    sp_dd_hi = float(b.get("sp_dd_hi") or 0.0)
    spi_from_hi = float(b.get("spi_from_hi") or sp_dd_hi)
    pgap = float(b.get("peer_gap") or 0.0)
    pm3 = float(b.get("peer_m3") or 0.0)
    p162_m3 = float(b.get("p162_m3") or 0.0)
    sc_i = float(b.get("sc_intra") or 0.0)
    cl_i = float(b.get("cl_intra") if b.get("cl_intra") is not None else b.get("daytime_brn_pct") or 0.0)
    brn_pos = float(b.get("brn_pos") or 0.0)
    brn_m5 = float(b.get("brn_m5") or 0.0)
    dmc = float(b.get("dmc10") or 0.0)
    mc = float(b.get("macd10") or 0.0)
    m1 = float(b.get("m1") or 0.0)
    dc10 = bool(b.get("dc10"))
    gc10 = bool(b.get("gc10"))

    # 1. Positive-T high-chase and late-window guards
    chase_blocked_reasons: list[str] = []
    if b_sig:
        if not in_long_win:
            b_sig = False
            chase_blocked_reasons.append("13:55后尾盘禁新开正T多腿")
        elif spi >= 0.30 * sc and vdev > -0.25:
            b_sig = False
            chase_blocked_reasons.append(f"sp_intra({spi:+.2f}%)≥+{0.30 * sc:.2f}%且未深蹲VWAP防追高")
        elif below_ma15 and fof_ov <= -1.0 and spi >= 0.20 * sc:
            b_sig = False
            chase_blocked_reasons.append("空头昨夜外盘大跌期禁追日内反抽")
        elif below_ma15 and (vdev > -0.18 or px_dd_hi < 0.25) and sp > -0.85 * sc:
            b_sig = False
            chase_blocked_reasons.append("空头趋势未破VWAP跌坑前禁左侧早买")
        elif brn_pos >= 0.90 and fof_ov > -0.5 and vdev > -0.25 and sp > -0.50 * sc:
            b_sig = False
            chase_blocked_reasons.append("BRN≥90%高位禁追浅回调")

    if scalp_ok:
        if not in_long_win:
            scalp_ok = False
        elif below_ma15 and fof_ov <= -1.0 and spi >= 0.20 * sc:
            scalp_ok = False
        elif brn_pos >= 0.95 and t >= "13:00" and fpm >= 0.60 and sc_i <= 0.20:
            scalp_ok = False

    # 2. Distinguish bull high-quantile flat-overnight T+0 from bear falling-knife
    eff_overlay_wt = (
        0.35
        if (float(close_wt) <= 0.0 and (not below_ma15) and sc_i >= -0.15 and cl_i >= -0.15 and pm3 >= 0.0)
        else float(close_wt)
    )
    s_sig, b_sig, scalp_ok, meta = fof_base.apply_fof_crude_intraday_overlays(
        day,
        b,
        s_sig=s_sig,
        b_sig=b_sig,
        scalp_ok=scalp_ok,
        close_wt=eff_overlay_wt,
        sc=sc,
        has_base_shares=False,
    )
    meta = dict(meta)
    if chase_blocked_reasons:
        meta["chase_blocked_reasons"] = chase_blocked_reasons

    # 3. Discount-regime intraday spike reverse-T & weak XOP/FOF morning high-sell
    rev_t_channel: str | None = None
    if not s_sig:
        if (
            vdev >= 0.52 * sc
            and spi >= 0.52 * sc
            and (dc10 or (dmc <= -0.00015 and m1 <= 0.0))
            and (sc_i <= 1.20 or pgap >= 0.65)
            and cl_i <= 0.80
        ):
            s_sig = True
            rev_t_channel = "c_disc_spike_rev_t"
        elif (
            "09:34" <= t <= "10:45"
            and (xop_ov <= -1.80 or fof_ov <= -1.80)
            and vdev >= 0.35 * sc
            and spi >= 0.38 * sc
            and (dc10 or (dmc <= -0.0002 and m1 <= 0.0))
            and sc_i <= 1.15
        ):
            s_sig = True
            rev_t_channel = "c_weak_xop_rev_t"
    if rev_t_channel:
        meta["rev_t_channel"] = rev_t_channel

    # 4. Multi-source T+0 positive-T channels (quota-premium intraday dip & tri-peer/SC0 catch-up)
    day_tuning = day.get("v9_tuning") if isinstance(day.get("v9_tuning"), Mapping) else {}
    disable_scalp = bool(day_tuning.get("disable_scalp"))
    knife_blk = bool(meta.get("knife_blocked"))
    t0_channels = list(meta.get("contrarian_channels") or [])
    if in_long_win and (not disable_scalp) and (not knife_blk) and (not b_sig) and (not scalp_ok):
        macd_up = gc10 or (mc < 0.0 and dmc >= 0.00020 * sc) or (dmc >= 0.00030 * sc and m1 >= 0.0)
        if (
            pm > 2.20
            and (
                (vdev <= -0.38 * sc and px_dd_hi >= 0.55 * sc and sp_dd_hi >= 0.45 * sc)
                or (fof_ov >= 2.0 and spi <= -0.55 * sc and px_dd_hi >= 0.55 * sc and spi_from_hi >= 0.0)
            )
            and macd_up
            and brn_m5 >= -0.08
            and cl_i >= -0.35
        ):
            scalp_ok = True
            t0_channels.append("c_quota_dip_t0")
            meta["contrarian_triggered"] = True
            meta["contrarian_channels"] = t0_channels
            meta["scalp_lbl_override"] = "配额溢价日内急跌做T档(95%·收盘防泡沫)"
            meta["scalp_frac_override"] = 0.95
        elif (
            fpm <= 0.85
            and sp <= 0.10 * sc
            and spi <= 0.15
            and (
                (pgap <= -0.28 * sc and (pm3 >= 0.05 or p162_m3 >= 0.06) and sc_i >= 0.10)
                or (sc_i >= 0.65 * sc and brn_m5 >= 0.05 and vdev <= 0.15 and fpm <= -0.25 and pm3 >= 0.02)
            )
            and (gc10 or (dmc >= 0.00018 * sc and m1 >= 0.03))
        ):
            scalp_ok = True
            t0_channels.append("c_tri_peer_sc_catchup")
            meta["contrarian_triggered"] = True
            meta["contrarian_channels"] = t0_channels
            meta["scalp_lbl_override"] = "三同业+沪油共振补涨T+0档(95%)"
            meta["scalp_frac_override"] = 0.95

    if disable_scalp:
        scalp_ok = False
    return s_sig, b_sig, scalp_ok, meta


def should_exit_fof_t0_short(
    day: Mapping[str, Any],
    b: Mapping[str, Any],
    *,
    entry_px: float,
    entry_sp: float,
    close_wt: float,
) -> bool:
    """Fast reverse-T cover back to lock intraday profits and free capital for positive-T."""
    del day
    v9 = _v9()
    if v9.should_exit_short(b, entry_sp, True, close_wt=close_wt, entry_px=entry_px):
        return True
    if entry_px > 0:
        pr = float(b["price"])
        sp = float(b["sp_fx"])
        vdev = float(b.get("vdev") or 0.0)
        dmc = float(b.get("dmc10") or 0.0)
        m1 = float(b.get("m1") or 0.0)
        drop_p = (entry_px - pr) / entry_px * 100.0
        sp_c = entry_sp - sp
        if (drop_p >= 0.42 and sp_c >= 0.30 and vdev <= -0.25 and dmc >= 0.0 and m1 >= 0.0) or (
            drop_p >= 0.65 and dmc > 0.0
        ):
            return True
    return False


def should_exit_fof_t0_long(
    day: Mapping[str, Any],
    b: Mapping[str, Any],
    *,
    b_buy: Mapping[str, Any],
    entry_px: float,
    entry_sp: float,
    entry_prem: float,
    s_sig: bool,
    close_wt: float,
    is_scalp: bool,
) -> tuple[bool, str]:
    """Positive-T / scalp long exit preventing lossy non-stop exits and premature quota scalps."""
    v9 = _v9()
    pr = float(b["price"])
    t = str(b.get("time") or "")
    sp = float(b["sp_fx"])
    vdev = float(b.get("vdev") or 0.0)
    dmc = float(b.get("dmc10") or 0.0)
    m1 = float(b.get("m1") or 0.0)
    dc10 = bool(b.get("dc10"))
    gain_p = ((pr - entry_px) / entry_px * 100.0) if entry_px > 0 else 0.0
    day_tuning = day.get("v9_tuning") if isinstance(day.get("v9_tuning"), Mapping) else {}
    raw_min_tp = day_tuning.get("fof_t0_min_tp_pct")
    try:
        min_tp_pct = float(raw_min_tp) if raw_min_tp is not None else 0.06
    except (TypeError, ValueError):
        min_tp_pct = 0.06

    if not is_scalp:
        ex_1, ex_lbl = v9.should_exit_long_v9(
            day,
            b_buy,
            entry_px=entry_px,
            entry_sp=entry_sp,
            s_sig=s_sig,
            close_wt=close_wt,
            adaptive_exits=True,
        )
        if ex_1 and ("止损" not in ex_lbl) and gain_p <= min_tp_pct:
            return False, ""
        return ex_1, ex_lbl

    eff_s_scalp = s_sig
    if float(entry_prem) > 2.20 and s_sig:
        eff_s_scalp = bool((vdev >= 0.45 or (sp - entry_sp) >= 0.55) and (dmc <= 0.0 or m1 <= 0.0 or dc10))
    ex_2, ex_2_lbl = v9.should_exit_scalp_long(
        b,
        entry_px=entry_px,
        entry_sp=entry_sp,
        s_sig=eff_s_scalp,
        day=day,
        close_wt=close_wt,
    )
    if float(entry_prem) > 2.20 and ex_2 and gain_p > 0.0 and dmc > 0.0 and m1 > 0.0:
        return False, ""
    if ex_2 and gain_p <= min_tp_pct and gain_p > -0.45 and t < "14:40":
        return False, ""
    return ex_2, ex_2_lbl


def build_fof_t0_diag_payload(day: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    """Build diagnostics fields extending `build_fof_crude_diag_payload` with 10-source T+0 metrics."""
    payload = fof_base.build_fof_crude_diag_payload(day, b)
    p162_chg = b.get("p162_chg")
    cl_intra = b.get("cl_intra")
    payload.update(
        {
            "anchor_mode": "fof_t0_multisource",
            "allow_same_day_t0": True,
            "t0_cash_reserve_pct": 15.0,
            "xop_ov_pct": round(float(day.get("xop_ov") or 0.0), 2),
            "spy_ov_pct": round(float(day.get("spy_ov") or 0.0), 2),
            "cl_intra_pct": round(float(cl_intra), 2) if cl_intra is not None else None,
            "peer_162411_chg_pct": round(float(p162_chg), 3) if p162_chg is not None else None,
            "peer_162411_m3_pct": round(float(b.get("p162_m3") or 0.0), 3),
        }
    )
    return payload


def build_fof_t0_live_ui_blocks(
    day: Mapping[str, Any],
    b: Mapping[str, Any],
    meta: Mapping[str, Any],
    scalp_lbl: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build live checklist condition and market indicator cards for `fof_t0_multisource`."""
    buy_cond, cards = fof_base.build_fof_crude_live_ui_blocks(day, b, meta, scalp_lbl)
    xop_ov = float(day.get("xop_ov") or 0.0)
    cl_i = float(b.get("cl_intra") if b.get("cl_intra") is not None else b.get("daytime_brn_pct") or 0.0)
    p162_chg = b.get("p162_chg")
    p162_m3 = float(b.get("p162_m3") or 0.0)
    t0_chs = list(meta.get("contrarian_channels") or [])

    buy_cond = dict(buy_cond)
    buy_cond["id"] = "v9-buy-fof-t0-multisource"
    buy_cond["label"] = "160723 T+0双池(85%底仓+15%机动金) · 10源因果联动"
    buy_cond["target_rule"] = (
        "09:32-13:55 正T双池机动低吸/三同业补涨/配额急跌做T；BRN≥90%转100%纯T+0；禁高位追涨与尾盘接飞刀"
    )
    if t0_chs:
        buy_cond["detail"] = f"触发T+0多源通道({'/'.join(t0_chs)}): {scalp_lbl}"

    cards = list(cards)
    cards.append(
        {
            "key": "fof_t0_cme_xop_peer162",
            "label": "CME CL 1m / XOP隔夜 / 162411联动",
            "value": f"CL {cl_i:+.2f}% / 162411 {f'{float(p162_chg):+.2f}%' if p162_chg is not None else '--'}",
            "raw": round(cl_i, 3),
            "hint": f"XOP昨夜 {xop_ov:+.2f}% · 162411 3m {p162_m3:+.2f}% · 双池T+0机动 reserve 15%",
        }
    )
    return buy_cond, cards
