"""Route D++ causal covariance + heterogeneous solo-cap shared-pool portfolio allocator.

Computes per-day Bayesian edge & drawdown-sentry telemetry strictly from closed
history (< trade_date), correlation-penalized multi-symbol capital allocation,
portfolio daily volatility ceiling scaling, intraday reverse-T short-cover cash
reservations, and two-wave (sell-before-buy) 14:53 overnight rebalance plans.
"""

from __future__ import annotations

from collections import OrderedDict
import copy
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .quant_rules import _normalize_eval_criteria, normalize_position_sizing
from .v9_plus_engine import (
    _LOT_SIZE,
    _apply_v9_macd_anchor,
    _enrich_v9_multi_oil_days,
    _peer_adjusted_buy_bar,
    _resolve_cross_market_anchor,
    _suppress_waterfall_short_entry,
    apply_dual_oil_peer_intraday_overlays,
    apply_fof_crude_intraday_overlays,
    apply_fof_t0_pulse_intraday_overlays,
    apply_sp_oilgas_etf_intraday_overlays,
    apply_v9_close_weight_tuning,
    apply_v9_sell_tier_tuning,
    apply_xop_overnight_intraday_overlays,
    build_v9_plus_days,
    clean_buy_tier,
    clean_sell_tier,
    filter_bear_regime_buy,
    resolve_close_weight_fn,
    resolve_effective_close_weight,
    resolve_fof_t0_buy_tier,
    resolve_fof_t0_scalp_frac,
    resolve_fof_t0_sell_tier,
    resolve_sp_oilgas_buy_tier,
    resolve_sp_oilgas_sell_tier,
    should_add_short_tranche,
    should_enter_intraday_dip_scalp,
    should_exit_fof_t0_long,
    should_exit_fof_t0_short,
    should_exit_long_v9,
    should_exit_scalp_long,
    should_exit_short,
    should_exit_sp_oilgas_long,
    should_exit_sp_oilgas_short,
    should_suppress_fof_t0_short_entry,
    signals_v8_with_tuning,
)

VALID_POLICY_ENGINES: tuple[str, ...] = (
    "route_d_plus",
    "signal_proportional",
    "inverse_vol_risk_parity",
    "equal_weight",
    "static_cap",
    "shadow_route_d",
)

SHARED_POOL_EXECUTION_ENGINES = frozenset(
    {"route_d_plus", "signal_proportional", "inverse_vol_risk_parity", "equal_weight"}
)

VALID_CAPITAL_POLICY_MODES = (
    "route_d_sharpe_shield",
    "route_d_calmar_peak",
    "route_d_plus",
    "manual_cap",
    "static_cap",
    "route_d_shadow",
)

CAPITAL_POLICY_MODE_LABELS: dict[str, str] = {
    "route_d_sharpe_shield": "Route D++ 高夏普控回撤旗舰版 (σ≤2.4% · Sharpe 5.65 / 回撤 6.71%)",
    "route_d_calmar_peak": "Route D++ 卡玛均衡优选版 (σ≤2.6% · Calmar 44.59 / 回撤 7.03%)",
    "route_d_plus": "Route D++ 进取高收益版 (σ≤4.2% · 动态共享池)",
    "manual_cap": "人工配置（手动设定每股分配金额）",
    "static_cap": "静态分仓上限（各标的独立额度）",
    "route_d_shadow": "Route D++ 影子对照（实盘按静态上限执行，旁路计算动态目标）",
}

DEFAULT_POOL_POLICY: dict[str, Any] = {
    "total_pool_cap": None,
    "rho_thresh": 0.62,
    "rho_slope": 0.26,
    "intra_rho_slope": 0.24,
    "lookback_days": 20,
    "history_calendar_days": 75,
    "crowd_penalty": 0.20,
    "crowd_rho_thresh": 0.75,
    "sigma_cap_pct": 4.20,
    "solo_equity_coef": 0.64,
    "solo_equity_cap": 0.85,
    "solo_crude_coef": 0.48,
    "solo_crude_cap": 0.68,
    "multi_base_coef": 0.48,
    "multi_min_coef": 0.36,
    "multi_cap_max": 0.68,
    "cap_floor": 0.18,
    "max_total_budget_wt": 0.985,
    "equity_solo_anchors": ["xop_us_ep"],
    "symbol_weight_multipliers": {},
    "symbol_max_caps": {},
    "enforce_min_retained_floor": True,
    "enable_morning_base_catchup": True,
    "morning_catchup_end_hhmm": "09:50",
    "morning_catchup_max_gap_pct": 2.50,
    "morning_catchup_max_sp_fx_pct": 0.38,
    "morning_catchup_max_prem_pct": 2.00,
    "single_fee_rate": 0.00005,
}

_POLICY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{2,64}\Z")
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _parse_float(raw: Any, default: float, *, lo: float, hi: float) -> float:
    try:
        val = float(raw)
        if math.isfinite(val):
            return _clip(val, lo, hi)
    except (TypeError, ValueError):
        pass
    return default


def normalize_pool_policy(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize shared-pool policy configuration with safe defaults and bounds."""
    out = copy.deepcopy(DEFAULT_POOL_POLICY)
    if not isinstance(raw, Mapping):
        return out

    if "total_pool_cap" in raw:
        raw_tpc = raw.get("total_pool_cap")
        if raw_tpc is None or raw_tpc == "":
            out["total_pool_cap"] = None
        else:
            try:
                cand = float(raw_tpc)
                if math.isfinite(cand) and 100.0 <= cand <= 10_000_000.0:
                    out["total_pool_cap"] = round(cand, 2)
            except (TypeError, ValueError):
                pass

    float_bounds: tuple[tuple[str, float, float], ...] = (
        ("rho_thresh", 0.0, 0.95),
        ("rho_slope", 0.0, 1.0),
        ("intra_rho_slope", 0.0, 1.0),
        ("crowd_penalty", 0.0, 0.80),
        ("crowd_rho_thresh", 0.0, 0.99),
        ("solo_equity_coef", 0.10, 1.0),
        ("solo_equity_cap", 0.15, 1.0),
        ("solo_crude_coef", 0.10, 1.0),
        ("solo_crude_cap", 0.15, 1.0),
        ("multi_base_coef", 0.10, 1.0),
        ("multi_min_coef", 0.05, 0.95),
        ("multi_cap_max", 0.15, 1.0),
        ("cap_floor", 0.05, 0.80),
        ("max_total_budget_wt", 0.20, 1.0),
        ("morning_catchup_max_gap_pct", 0.20, 9.50),
        ("morning_catchup_max_sp_fx_pct", -1.0, 3.0),
        ("morning_catchup_max_prem_pct", -2.0, 15.0),
        ("single_fee_rate", 0.0, 0.01),
    )
    for key, lo, hi in float_bounds:
        if key in raw and raw.get(key) is not None:
            out[key] = round(_parse_float(raw.get(key), float(out[key]), lo=lo, hi=hi), 6)

    out["multi_min_coef"] = min(float(out["multi_min_coef"]), float(out["multi_base_coef"]))
    out["cap_floor"] = min(float(out["cap_floor"]), float(out["multi_cap_max"]))

    if "lookback_days" in raw and raw.get("lookback_days") is not None:
        try:
            lb = int(raw["lookback_days"])
            out["lookback_days"] = max(5, min(120, lb))
        except (TypeError, ValueError):
            pass

    if "history_calendar_days" in raw and raw.get("history_calendar_days") is not None:
        try:
            hc = int(raw["history_calendar_days"])
            out["history_calendar_days"] = max(15, min(365, hc))
        except (TypeError, ValueError):
            pass

    if "sigma_cap_pct" in raw:
        raw_sig = raw.get("sigma_cap_pct")
        if raw_sig is None or raw_sig == "":
            out["sigma_cap_pct"] = None
        else:
            try:
                sv = float(raw_sig)
                if not math.isfinite(sv) or sv <= 0.0:
                    out["sigma_cap_pct"] = None
                else:
                    out["sigma_cap_pct"] = round(_clip(sv, 0.50, 25.0), 4)
            except (TypeError, ValueError):
                pass

    if "equity_solo_anchors" in raw:
        raw_anc = raw.get("equity_solo_anchors")
        if isinstance(raw_anc, str):
            items = [item.strip() for item in raw_anc.split(",") if item.strip()]
            out["equity_solo_anchors"] = list(dict.fromkeys(items))
        elif isinstance(raw_anc, (list, tuple, set)):
            items = [str(item).strip() for item in raw_anc if str(item).strip()]
            out["equity_solo_anchors"] = list(dict.fromkeys(items))

    if "enforce_min_retained_floor" in raw:
        out["enforce_min_retained_floor"] = bool(raw.get("enforce_min_retained_floor"))
    if "enable_morning_base_catchup" in raw:
        out["enable_morning_base_catchup"] = bool(raw.get("enable_morning_base_catchup"))
    if "morning_catchup_end_hhmm" in raw:
        hhmm_val = str(raw.get("morning_catchup_end_hhmm") or "").strip()
        if re.match(r"^(?:09:[3-5][0-9]|10:[0-2][0-9]|10:30)\Z", hhmm_val):
            out["morning_catchup_end_hhmm"] = hhmm_val

    for map_key, lo_v, hi_v in (("symbol_weight_multipliers", 0.0, 3.0), ("symbol_max_caps", 0.05, 1.0)):
        raw_map = raw.get(map_key)
        if isinstance(raw_map, Mapping):
            clean_map: dict[str, float] = {}
            for k_raw, v_raw in raw_map.items():
                clean_k = str(k_raw or "").strip().upper()
                if not re.fullmatch(r"[0-9]{6}", clean_k):
                    continue
                clean_map[clean_k] = round(_parse_float(v_raw, 1.0 if map_key == "symbol_weight_multipliers" else 0.85, lo=lo_v, hi=hi_v), 4)
            out[map_key] = clean_map

    return out


def is_valid_policy_id(policy_id: object) -> bool:
    return bool(_POLICY_ID_RE.fullmatch(str(policy_id or "").strip()))


class CausalStats:
    """Strictly causal per-symbol tracker for shrinkage win-rate, Kelly fraction, and sentry gate."""

    def __init__(self) -> None:
        self.events: list[dict[str, float]] = []
        self.tier_counts: dict[str, list[float]] = {}
        self.intraday_wins = 0
        self.intraday_total = 0
        self.intraday_sum_win = 0.0
        self.intraday_sum_loss = 0.0
        self.overnight_wins = 0
        self.overnight_total = 0
        self.overnight_sum_win = 0.0
        self.overnight_sum_loss = 0.0

    def record(self, event: Mapping[str, Any]) -> None:
        """Record one completed historical trade or overnight-hold event."""
        ret = float(event["ret_pct"])
        won = 1.0 if ret > 0.0 else 0.0
        kind = str(event["kind"])
        bucket = str(event.get("bucket") or kind)
        self.events.append({"ret_pct": ret, "won": won})
        tc = self.tier_counts.setdefault(bucket, [0.0, 0.0, 0.0, 0.0])
        tc[0] += won
        tc[1] += 1.0
        if ret > 0.0:
            tc[2] += ret
        elif ret < 0.0:
            tc[3] += abs(ret)

        if kind == "overnight":
            self.overnight_total += 1
            self.overnight_wins += int(won)
            if ret > 0.0:
                self.overnight_sum_win += ret
            elif ret < 0.0:
                self.overnight_sum_loss += abs(ret)
        else:
            self.intraday_total += 1
            self.intraday_wins += int(won)
            if ret > 0.0:
                self.intraday_sum_win += ret
            elif ret < 0.0:
                self.intraday_sum_loss += abs(ret)

    def snapshot(self) -> dict[str, Any]:
        """Compute current causal Bayesian edge & recent-momentum snapshot."""
        rec = self.events[-16:]
        n = len(rec)
        if n < 4:
            m_rec, sentry = 1.0, 0.0
        else:
            w_s = win_a = ret_a = 0.0
            for idx, ev in enumerate(reversed(rec)):
                wt = 0.88 ** idx
                w_s += wt
                win_a += wt * ev["won"]
                ret_a += wt * ev["ret_pct"]
            ew_win = win_a / w_s if w_s > 0.0 else 0.58
            ew_ret = ret_a / w_s if w_s > 0.0 else 0.0
            consec = 0
            for ev in reversed(rec):
                if ev["ret_pct"] <= 0.0:
                    consec += 1
                else:
                    break
            if consec >= 3 or (n >= 5 and ew_win < 0.42 and ew_ret < 0.0):
                m_rec, sentry = 0.30, 1.0
            else:
                m_rec = _clip(1.0 + 1.60 * (ew_win - 0.55) + 0.25 * math.tanh(ew_ret / 2.5), 0.45, 1.80)
                sentry = 0.0

        def _edge(kind: str, bucket: str) -> tuple[float, float]:
            if kind == "overnight":
                bw, bn = float(self.overnight_wins), float(self.overnight_total)
                sw, sl = self.overnight_sum_win, self.overnight_sum_loss
            else:
                bw, bn = float(self.intraday_wins), float(self.intraday_total)
                sw, sl = self.intraday_sum_win, self.intraday_sum_loss
            tc = self.tier_counts.get(bucket)
            if tc and tc[1] >= 1.0:
                kp = (bw + 12.0 * 0.58) / (bn + 12.0)
                ph = (tc[0] + 8.0 * kp) / (tc[1] + 8.0)
                aw = (tc[2] + 4.0 * 0.90) / (max(0.0, tc[0]) + 4.0)
                al = (tc[3] + 4.0 * 0.65) / (max(0.0, tc[1] - tc[0]) + 4.0)
            else:
                ph = (bw + 12.0 * 0.58) / (bn + 12.0)
                aw = (sw + 5.0 * 0.90) / (bw + 5.0)
                al = (sl + 5.0 * 0.65) / (max(0.0, bn - bw) + 5.0)
            b_ratio = max(0.45, min(2.8, aw / max(0.20, al)))
            raw_k = max(0.0, (ph * b_ratio - (1.0 - ph)) / b_ratio)
            return ph, raw_k

        _, k_ovs = _edge("overnight", "overnight_strong")
        _, k_ovm = _edge("overnight", "overnight_mod")
        bkts = ("buy_tier_a", "buy_tier_b", "buy_tier_c", "buy_scalp")
        return {
            "m_rec": m_rec,
            "sentry": sentry,
            "k_ov_strong": _clip(k_ovs * 0.82, 0.10, 0.60),
            "k_ov_mod": _clip(k_ovm * 0.82, 0.10, 0.60),
            "intra": {b: _edge("intraday", b) for b in bkts},
            "event_count": len(self.events),
        }


def bayesian_prior_snapshot() -> dict[str, Any]:
    """Return uninformative Bayesian prior snapshot when history is unavailable."""
    return CausalStats().snapshot()


def compute_ewma_vols_and_corrs(
    symbols: Sequence[str],
    series_by_symbol: Mapping[str, Sequence[tuple[str, float]]],
    *,
    lookback_days: int = 20,
) -> tuple[dict[str, float], dict[tuple[str, str], float]]:
    """Compute shrinkage EWMA daily volatility (%) and pairwise Pearson correlation matrix."""
    rets_map: dict[str, dict[str, float]] = {}
    ordered_rets: dict[str, list[float]] = {}
    for sym in symbols:
        pts = sorted(
            ((str(td), float(px)) for td, px in (series_by_symbol.get(sym) or []) if float(px) > 0.0),
            key=lambda item: item[0],
        )
        r_by_td: dict[str, float] = {}
        r_list: list[float] = []
        for idx in range(1, len(pts)):
            td_cur, px_cur = pts[idx]
            _, px_prev = pts[idx - 1]
            if px_prev > 0.0:
                ret = (px_cur - px_prev) / px_prev * 100.0
                r_by_td[td_cur] = ret
        all_tds = sorted(r_by_td)
        if lookback_days > 0 and len(all_tds) > lookback_days:
            keep_tds = all_tds[-lookback_days:]
            r_by_td = {td: r_by_td[td] for td in keep_tds}
        r_list = [r_by_td[td] for td in sorted(r_by_td)]
        rets_map[sym] = r_by_td
        ordered_rets[sym] = r_list

    vols: dict[str, float] = {}
    for sym in symbols:
        arr = ordered_rets[sym]
        n = len(arr)
        if n >= 1:
            ws = vs = 0.0
            for age, r in enumerate(reversed(arr)):
                wt = 0.88 ** age
                ws += wt
                vs += wt * r * r
            vols[sym] = (n * math.sqrt(vs / ws) + 4.0 * 3.20) / (n + 4.0)
        else:
            vols[sym] = 3.20

    corrs: dict[tuple[str, str], float] = {}
    for s1 in symbols:
        for s2 in symbols:
            if s1 == s2:
                corrs[(s1, s2)] = 1.0
                continue
            m1, m2 = rets_map[s1], rets_map[s2]
            common = sorted(set(m1) & set(m2))
            if lookback_days > 0 and len(common) > lookback_days:
                common = common[-lookback_days:]
            n = len(common)
            if n >= 4:
                a = [m1[td] for td in common]
                b = [m2[td] for td in common]
                ma = sum(a) / n
                mb = sum(b) / n
                da = [x - ma for x in a]
                db = [y - mb for y in b]
                den = math.sqrt(sum(x * x for x in da) * sum(y * y for y in db))
                ec = (sum(x * y for x, y in zip(da, db)) / den) if den > 1e-9 else 0.55
            else:
                ec = 0.55
            corrs[(s1, s2)] = _clip((n * ec + 5.0 * 0.55) / (n + 5.0), -0.20, 0.99)
    return vols, corrs


def resolve_solo_cap_params(anchor: str, policy: Mapping[str, Any]) -> tuple[float, float]:
    """Return `(base_coef, max_cap)` for single-active-symbol exposure based on anchor class."""
    eq_anchors = set(str(x).strip() for x in (policy.get("equity_solo_anchors") or ()) if str(x).strip())
    if str(anchor or "").strip() in eq_anchors:
        return float(policy["solo_equity_coef"]), float(policy["solo_equity_cap"])
    return float(policy["solo_crude_coef"]), float(policy["solo_crude_cap"])


def scale_buy_orders_to_cash(
    buy_diffs: Mapping[str, int],
    prices: Mapping[str, float],
    available_cash: float,
    single_fee: float,
    *,
    lot_size: int = _LOT_SIZE,
) -> dict[str, int]:
    """Proportionally clamp positive share deltas so total cost never exceeds available cash."""
    rem_cash = max(0.0, float(available_cash))
    wants: list[tuple[str, int, float]] = []
    tot_cost = 0.0
    for sym, raw_diff in buy_diffs.items():
        diff_sh = (int(raw_diff) // lot_size) * lot_size
        px = float(prices.get(sym) or 0.0)
        if diff_sh >= lot_size and px > 0.0:
            est = diff_sh * px * (1.0 + float(single_fee))
            wants.append((sym, diff_sh, est))
            tot_cost += est
    if not wants or rem_cash <= 0.0 or tot_cost <= 0.0:
        return {}

    sc_b = min(1.0, rem_cash / tot_cost)
    out: dict[str, int] = {}
    for sym, diff_sh, _ in sorted(wants, key=lambda item: item[2], reverse=True):
        px = float(prices[sym])
        unit_cost = px * lot_size * (1.0 + float(single_fee))
        if unit_cost <= 0.0:
            continue
        q = min(
            int((diff_sh * sc_b) // lot_size) * lot_size,
            int(rem_cash // unit_cost) * lot_size,
        )
        cost = q * px * (1.0 + float(single_fee))
        if q >= lot_size and rem_cash >= cost - 1e-6:
            rem_cash -= cost
            out[sym] = q
    return out


class RouteDPlusAllocator:
    """Thread-safe Route D++ shared-pool portfolio allocator with per-day causal caching."""

    def __init__(self, *, max_cache_days: int = 8) -> None:
        self._lock = RLock()
        self._max_cache_days = max(2, int(max_cache_days))
        self._day_cache: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()

    def clear_cache(self) -> None:
        with self._lock:
            self._day_cache.clear()

    @staticmethod
    def _prev_date_window(trade_date: str, history_calendar_days: int) -> tuple[str, str] | None:
        clean_td = str(trade_date or "").replace("-", "").strip()
        try:
            cur_dt = datetime.strptime(clean_td, "%Y%m%d")
        except ValueError:
            return None
        prev_dt = cur_dt - timedelta(days=1)
        start_dt = prev_dt - timedelta(days=max(10, int(history_calendar_days)))
        return start_dt.strftime("%Y%m%d"), prev_dt.strftime("%Y%m%d")

    @staticmethod
    def _extract_symbol_history(
        research_store: Any,
        *,
        symbol: str,
        rule_spec: Mapping[str, Any] | None,
        start_date: str,
        prev_end_date: str,
        trade_date: str,
        single_fee: float,
    ) -> dict[str, Any]:
        ps = normalize_position_sizing((rule_spec or {}).get("position_sizing"))
        anchor = _resolve_cross_market_anchor(ps)
        v9_tuning = dict(ps.get("v9_tuning") or {})
        close_hhmm = str(ps.get("close_flatten_time") or "14:53")
        ep = str(ps.get("engine_profile") or "v9_plus_aggressive")
        macd = dict((rule_spec or {}).get("macd_settings") or {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.0002})
        crit = _normalize_eval_criteria((rule_spec or {}).get("eval_criteria"))
        sc = float(ps.get("scale_factor") or 1.0)
        cooldown = max(1, int(ps.get("cooldown_minutes") or 4))
        max_legs = max(1, int(ps.get("max_daily_entries") or crit.get("max_daily_trades") or 8))
        strict_scalp = bool(v9_tuning.get("scalp_strict_mode"))
        overnight_fn = apply_v9_close_weight_tuning(resolve_close_weight_fn(ep, anchor), v9_tuning)

        try:
            days, _ = build_v9_plus_days(
                research_store,
                symbol=symbol,
                start_date=start_date,
                end_date=prev_end_date,
                macd_settings=macd,
                allow_network=False,
                close_cutoff_time=close_hhmm,
                cross_market_anchor=anchor,
            )
            days = [d for d in days if str(d.get("td") or "") < str(trade_date)]
            if days:
                _apply_v9_macd_anchor(days, v9_tuning)
                _enrich_v9_multi_oil_days(
                    research_store,
                    days,
                    symbol=symbol,
                    v9_tuning=v9_tuning,
                    allow_network=False,
                )
        except Exception:
            days = []

        if not days:
            return {"anchor": anchor, "events_by_day": {}, "p1500_series": [], "days_count": 0}

        fee_pct = float(single_fee) * 200.0
        cash = 100_000.0
        shares = 0
        b0 = days[0]["bars"][0]
        w0, _ = overnight_fn(days[0], b0, 0)
        pr0 = float(b0["price"])
        sh0 = int((cash * w0) // (pr0 * _LOT_SIZE * (1.0 + single_fee))) * _LOT_SIZE if pr0 > 0 else 0
        if sh0 >= _LOT_SIZE:
            cash -= sh0 * pr0 * (1.0 + single_fee)
            shares += sh0

        is_sp_etf = anchor == "sp_oilgas_etf"
        is_fof_t0 = anchor == "fof_t0_multisource"
        events_by_day: dict[str, list[dict[str, Any]]] = {}
        p1500_series: list[tuple[str, float]] = []

        for d_idx, day in enumerate(days):
            td = str(day["td"])
            evs: list[dict[str, Any]] = []
            prev_sh = 0 if d_idx == 0 else shares
            prev_c = float(day["pre"]) if d_idx == 0 else float(days[d_idx - 1]["p1500"])
            c_px = float(day["p1500"])
            p1500_series.append((td, c_px))

            if d_idx == 0 and sh0 >= _LOT_SIZE and pr0 > 0:
                evs.append({"kind": "overnight", "bucket": "overnight_init", "ret_pct": (c_px - pr0) / pr0 * 100.0 - fee_pct / 2.0})
            elif d_idx > 0 and prev_sh >= _LOT_SIZE and prev_c > 0:
                mv_r = (prev_sh * prev_c) / max(1.0, cash + prev_sh * prev_c)
                evs.append({"kind": "overnight", "bucket": "overnight_strong" if mv_r >= 0.70 else "overnight_mod", "ret_pct": (c_px - prev_c) / prev_c * 100.0})

            pos = leg_q = 0
            entry_px = entry_sp = entry_prem = 0.0
            last_ex = -99
            legs_d = 0
            allow_addon = addon_done = rebalanced = False
            bkt = "intraday_std"
            bear_carry = (
                bool(v9_tuning.get("multi_oil_plunge_mode"))
                and (max(0, shares - _LOT_SIZE) >= _LOT_SIZE)
                and (float(day["ma5"]) < float(day["ma15"]))
            )

            for b in day["bars"]:
                t = str(b["time"])
                pr = float(b["price"])
                eq_now = cash + shares * pr
                s_sig, raw_b_sig = signals_v8_with_tuning(day, b, sc, v9_tuning, bear_carry_active=bear_carry)
                cur_wt, _ = overnight_fn(day, b, 0)
                b_buy = _peer_adjusted_buy_bar(b, v9_tuning, day=day)
                b_sig = raw_b_sig if is_sp_etf else filter_bear_regime_buy(day, b_buy, raw_b_sig, cur_wt, sc, v9_tuning=v9_tuning)
                scalp_b_in = b_buy if anchor in {"fof_crude_core", "fof_t0_multisource"} else b
                scalp_ok, _, s_frac = should_enter_intraday_dip_scalp(
                    scalp_b_in, sc, day=day, strict_mode=strict_scalp, v9_tuning=v9_tuning, cur_close_wt=cur_wt
                )
                has_sellable = max(0, shares - _LOT_SIZE) >= _LOT_SIZE
                dual_meta: dict[str, Any] = {}
                if anchor == "xop_us_ep":
                    s_sig, b_sig, scalp_ok, xop_meta = apply_xop_overnight_intraday_overlays(
                        day, b, s_sig=s_sig, b_sig=b_sig, scalp_ok=scalp_ok, close_wt=cur_wt, sc=sc
                    )
                    if xop_meta.get("scalp_frac_override"):
                        s_frac = float(xop_meta["scalp_frac_override"])
                elif anchor == "uso_bno_sc_peer":
                    s_sig, b_sig, scalp_ok, dual_meta = apply_dual_oil_peer_intraday_overlays(
                        day, b, s_sig=s_sig, b_sig=b_sig, scalp_ok=scalp_ok, close_wt=cur_wt, sc=sc
                    )
                    if dual_meta.get("scalp_frac_override"):
                        s_frac = float(dual_meta["scalp_frac_override"])
                elif anchor == "fof_crude_core":
                    s_sig, b_sig, scalp_ok, fof_meta = apply_fof_crude_intraday_overlays(
                        day, b, s_sig=s_sig, b_sig=b_sig, scalp_ok=scalp_ok, close_wt=cur_wt, sc=sc, has_base_shares=has_sellable
                    )
                    if fof_meta.get("scalp_frac_override"):
                        s_frac = float(fof_meta["scalp_frac_override"])
                elif is_fof_t0:
                    s_sig, b_sig, scalp_ok, t0_meta = apply_fof_t0_pulse_intraday_overlays(
                        day, b, s_sig=s_sig, b_sig=b_sig, scalp_ok=scalp_ok, close_wt=cur_wt, sc=sc, has_base_shares=has_sellable
                    )
                    if t0_meta.get("scalp_frac_override"):
                        s_frac = float(t0_meta["scalp_frac_override"])
                elif is_sp_etf:
                    s_sig, b_sig, scalp_ok, sp_meta = apply_sp_oilgas_etf_intraday_overlays(
                        day, b, s_sig=s_sig, b_sig=b_sig, scalp_ok=scalp_ok, close_wt=cur_wt, sc=sc, has_base_shares=has_sellable
                    )
                    if sp_meta.get("scalp_frac_override"):
                        s_frac = float(sp_meta["scalp_frac_override"])
                    if sp_meta:
                        dual_meta = sp_meta

                sp_waterfall = (
                    is_sp_etf
                    and float(cur_wt) <= 0.0
                    and float(b["m5"]) <= -0.28
                    and float(b["sp_fx"]) < 0.25
                    and float(b.get("raw_prem", b["prem"])) < 1.2
                )
                s_sig_short = False if (s_sig and (sp_waterfall if is_sp_etf else _suppress_waterfall_short_entry(b, cur_wt, sc))) else s_sig
                if s_sig_short and is_fof_t0 and should_suppress_fof_t0_short_entry(b):
                    s_sig_short = False

                if t >= close_hhmm:
                    if pos != 0 and entry_px > 0:
                        g = ((pr - entry_px) / entry_px * 100.0) if pos > 0 else ((entry_px - pr) / entry_px * 100.0)
                        evs.append({"kind": "intraday", "bucket": bkt, "ret_pct": g - fee_pct})
                    if not rebalanced:
                        wt, _ = resolve_effective_close_weight(overnight_fn, day, b, pos, entry_prem, v9_tuning=v9_tuning)
                        raw_tgt = int((eq_now * wt) // (pr * _LOT_SIZE * (1.0 + single_fee))) * _LOT_SIZE if pr > 0 else 0
                        tgt_sh = max(_LOT_SIZE, raw_tgt) if shares >= _LOT_SIZE else raw_tgt
                        diff_sh = tgt_sh - shares
                        if diff_sh >= _LOT_SIZE:
                            q = min(diff_sh, int(cash // (pr * _LOT_SIZE * (1.0 + single_fee))) * _LOT_SIZE) if pr > 0 else 0
                            if q >= _LOT_SIZE:
                                cash -= q * pr * (1.0 + single_fee)
                                shares += q
                        elif diff_sh <= -_LOT_SIZE:
                            sellable = max(0, shares - _LOT_SIZE)
                            q = min((-diff_sh // _LOT_SIZE) * _LOT_SIZE, sellable)
                            if q >= _LOT_SIZE:
                                cash += q * pr * (1.0 - single_fee)
                                shares -= q
                        pos = leg_q = 0
                        allow_addon = addon_done = False
                        rebalanced = True
                    continue

                in_win = ("09:32" <= t <= "11:29") or ("13:02" <= t <= "14:30")
                sellable = max(0, shares - _LOT_SIZE)
                bar_i = int(b["i"])
                if pos == 0 and in_win and (bar_i - last_ex) >= cooldown and legs_d < max_legs:
                    if s_sig_short and sellable >= _LOT_SIZE:
                        if is_sp_etf:
                            sell_frac, t_lbl_s = resolve_sp_oilgas_sell_tier(day, b, cur_wt)
                        elif is_fof_t0:
                            sell_frac, t_lbl_s = resolve_fof_t0_sell_tier(day, b)
                        else:
                            t_lbl_s, sell_frac = apply_v9_sell_tier_tuning(
                                day, b, *clean_sell_tier(day, b), v9_tuning, bear_carry_active=bear_carry
                            )
                        q = sellable if sell_frac >= 0.999 else min(sellable, max(_LOT_SIZE, int((shares * sell_frac) // _LOT_SIZE) * _LOT_SIZE))
                        cash += q * pr * (1.0 - single_fee)
                        shares -= q
                        pos, leg_q, entry_px, entry_sp, entry_prem = -1, q, pr, float(b["sp_fx"]), float(b["prem"])
                        allow_addon, addon_done, bear_carry = ("分批高抛" in t_lbl_s), False, False
                        bkt = "short_sell_t"
                        legs_d += 1
                    elif b_sig and pr > 0 and cash >= pr * _LOT_SIZE * (1.0 + single_fee):
                        if is_sp_etf:
                            buy_frac, _ = resolve_sp_oilgas_buy_tier(day, b_buy)
                        elif is_fof_t0:
                            buy_frac, _ = resolve_fof_t0_buy_tier(day, b_buy)
                        else:
                            _, buy_frac = clean_buy_tier(day, b_buy)
                        if dual_meta.get("buy_frac_floor"):
                            buy_frac = max(buy_frac, float(dual_meta["buy_frac_floor"]))
                        q = max(_LOT_SIZE, int((cash * buy_frac) // (pr * _LOT_SIZE * (1.0 + single_fee))) * _LOT_SIZE)
                        if cash >= q * pr * (1.0 + single_fee):
                            cash -= q * pr * (1.0 + single_fee)
                            shares += q
                            pos, leg_q, entry_px, entry_sp, entry_prem = 1, q, pr, float(b["sp_fx"]), float(b["prem"])
                            bkt = "buy_tier_a" if buy_frac >= 0.95 else ("buy_tier_b" if buy_frac >= 0.75 else "buy_tier_c")
                            legs_d += 1
                    elif scalp_ok and pr > 0 and cash >= pr * _LOT_SIZE * (1.0 + single_fee):
                        eff_sf = resolve_fof_t0_scalp_frac(s_frac) if is_fof_t0 else s_frac
                        q = max(_LOT_SIZE, int((cash * eff_sf) // (pr * _LOT_SIZE * (1.0 + single_fee))) * _LOT_SIZE)
                        if cash >= q * pr * (1.0 + single_fee):
                            cash -= q * pr * (1.0 + single_fee)
                            shares += q
                            pos, leg_q, entry_px, entry_sp, entry_prem = 2, q, pr, float(b["sp_fx"]), float(b["prem"])
                            bkt = "buy_scalp"
                            legs_d += 1
                elif pos == -1:
                    if allow_addon and (not addon_done) and in_win and sellable >= _LOT_SIZE and should_add_short_tranche(b, entry_px=entry_px, entry_sp=entry_sp, s_sig=s_sig_short):
                        q_add = sellable
                        cash += q_add * pr * (1.0 - single_fee)
                        shares -= q_add
                        tot_q = leg_q + q_add
                        if tot_q > 0:
                            entry_px = (entry_px * leg_q + pr * q_add) / tot_q
                            entry_sp = (entry_sp * leg_q + float(b["sp_fx"]) * q_add) / tot_q
                            entry_prem = (entry_prem * leg_q + float(b["prem"]) * q_add) / tot_q
                        leg_q, allow_addon, addon_done = tot_q, False, True
                    else:
                        ex_s = (
                            should_exit_sp_oilgas_short(day, b, entry_px=entry_px, entry_sp=entry_sp, close_wt=cur_wt)
                            if is_sp_etf
                            else (
                                should_exit_fof_t0_short(day, b, entry_px=entry_px, entry_sp=entry_sp, close_wt=cur_wt)
                                if is_fof_t0
                                else should_exit_short(b, entry_sp, True, close_wt=cur_wt, entry_px=entry_px)
                            )
                        )
                        if ex_s and pr > 0:
                            q = min(leg_q, int(cash // (pr * _LOT_SIZE * (1.0 + single_fee))) * _LOT_SIZE)
                            if q >= _LOT_SIZE:
                                cash -= q * pr * (1.0 + single_fee)
                                shares += q
                            if entry_px > 0:
                                evs.append({"kind": "intraday", "bucket": bkt, "ret_pct": ((entry_px - pr) / entry_px * 100.0) - fee_pct})
                            pos, leg_q, last_ex, allow_addon, addon_done = 0, 0, bar_i, False, False
                elif pos == 1:
                    if is_sp_etf:
                        ex1, _ = should_exit_sp_oilgas_long(day, b, entry_px=entry_px, entry_sp=entry_sp, s_sig=s_sig, close_wt=cur_wt, is_scalp=False)
                    elif is_fof_t0:
                        ex1, _ = should_exit_fof_t0_long(day, b, b_buy=b_buy, entry_px=entry_px, entry_sp=entry_sp, entry_prem=entry_prem, s_sig=s_sig, close_wt=cur_wt, is_scalp=False)
                    else:
                        ex1, _ = should_exit_long_v9(day, b_buy, entry_px=entry_px, entry_sp=entry_sp, s_sig=s_sig, close_wt=cur_wt, adaptive_exits=True)
                    if ex1:
                        q = min(leg_q, sellable)
                        if q >= _LOT_SIZE:
                            cash += q * pr * (1.0 - single_fee)
                            shares -= q
                        if entry_px > 0:
                            evs.append({"kind": "intraday", "bucket": bkt, "ret_pct": ((pr - entry_px) / entry_px * 100.0) - fee_pct})
                        pos, leg_q, last_ex = 0, 0, bar_i
                elif pos == 2:
                    if is_sp_etf:
                        ex2, _ = should_exit_sp_oilgas_long(day, b, entry_px=entry_px, entry_sp=entry_sp, s_sig=s_sig, close_wt=cur_wt, is_scalp=True)
                    elif is_fof_t0:
                        ex2, _ = should_exit_fof_t0_long(day, b, b_buy=b_buy, entry_px=entry_px, entry_sp=entry_sp, entry_prem=entry_prem, s_sig=s_sig, close_wt=cur_wt, is_scalp=True)
                    else:
                        ex2, _ = should_exit_scalp_long(b, entry_px=entry_px, entry_sp=entry_sp, s_sig=s_sig, day=day, close_wt=cur_wt)
                    if ex2:
                        q = min(leg_q, sellable)
                        if q >= _LOT_SIZE:
                            cash += q * pr * (1.0 - single_fee)
                            shares -= q
                        if entry_px > 0:
                            evs.append({"kind": "intraday", "bucket": bkt, "ret_pct": ((pr - entry_px) / entry_px * 100.0) - fee_pct})
                        pos, leg_q, last_ex = 0, 0, bar_i
            events_by_day[td] = evs

        return {
            "anchor": anchor,
            "events_by_day": events_by_day,
            "p1500_series": p1500_series,
            "days_count": len(days),
        }

    def get_causal_context(
        self,
        research_store: Any,
        symbol_rules: Mapping[str, Mapping[str, Any] | None],
        trade_date: str,
        policy: Mapping[str, Any] | None = None,
        *,
        override_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Load or return cached strictly-causal (< trade_date) Bayesian stats and covariance."""
        norm_pol = normalize_pool_policy(policy)
        symbols = sorted(str(s).strip() for s in symbol_rules if str(s).strip())
        if isinstance(override_context, Mapping):
            snaps = {
                s: dict((override_context.get("snaps") or {}).get(s) or bayesian_prior_snapshot())
                for s in symbols
            }
            vols = {
                s: float((override_context.get("vols") or {}).get(s) or 3.20)
                for s in symbols
            }
            raw_corrs = override_context.get("corrs") or {}
            corrs: dict[tuple[str, str], float] = {}
            for s1 in symbols:
                for s2 in symbols:
                    corrs[(s1, s2)] = 1.0 if s1 == s2 else float(raw_corrs.get((s1, s2), raw_corrs.get((s2, s1), 0.55)))
            anchors = {
                s: str(
                    (override_context.get("anchors") or {}).get(s)
                    or _resolve_cross_market_anchor((symbol_rules.get(s) or {}).get("position_sizing"))
                )
                for s in symbols
            }
            return {
                "trade_date": str(trade_date),
                "snaps": snaps,
                "vols": vols,
                "corrs": corrs,
                "anchors": anchors,
                "history_days": dict(override_context.get("history_days") or {s: 0 for s in symbols}),
                "cache_hit": False,
            }

        lookback_days = int(norm_pol["lookback_days"])
        cal_days = int(norm_pol["history_calendar_days"])
        single_fee = float(norm_pol["single_fee_rate"])
        rule_sig = tuple(
            (
                s,
                str((symbol_rules.get(s) or {}).get("id") or ""),
                str((symbol_rules.get(s) or {}).get("updated_at") or ""),
                _resolve_cross_market_anchor((symbol_rules.get(s) or {}).get("position_sizing")),
            )
            for s in symbols
        )
        ck = (str(trade_date), rule_sig, lookback_days, cal_days, round(single_fee, 6))

        with self._lock:
            hit = self._day_cache.get(ck)
            if hit is not None:
                self._day_cache.move_to_end(ck)
                return {**hit, "cache_hit": True}

        date_win = self._prev_date_window(trade_date, cal_days)
        snaps: dict[str, dict[str, Any]] = {}
        anchors: dict[str, str] = {}
        series_by_symbol: dict[str, list[tuple[str, float]]] = {}
        history_days: dict[str, int] = {}

        for sym in symbols:
            rule_spec = symbol_rules.get(sym)
            if date_win is None or research_store is None:
                ps = normalize_position_sizing((rule_spec or {}).get("position_sizing"))
                anchors[sym] = _resolve_cross_market_anchor(ps)
                snaps[sym] = bayesian_prior_snapshot()
                series_by_symbol[sym] = []
                history_days[sym] = 0
                continue
            start_str, prev_str = date_win
            hist = self._extract_symbol_history(
                research_store,
                symbol=sym,
                rule_spec=rule_spec,
                start_date=start_str,
                prev_end_date=prev_str,
                trade_date=trade_date,
                single_fee=single_fee,
            )
            anchors[sym] = str(hist["anchor"])
            tracker = CausalStats()
            for td in sorted(hist["events_by_day"]):
                for ev in hist["events_by_day"][td]:
                    tracker.record(ev)
            snaps[sym] = tracker.snapshot()
            series_by_symbol[sym] = list(hist["p1500_series"])
            history_days[sym] = int(hist["days_count"])

        vols, corrs = compute_ewma_vols_and_corrs(symbols, series_by_symbol, lookback_days=lookback_days)
        built = {
            "trade_date": str(trade_date),
            "snaps": snaps,
            "vols": vols,
            "corrs": corrs,
            "anchors": anchors,
            "history_days": history_days,
        }
        with self._lock:
            self._day_cache[ck] = built
            self._day_cache.move_to_end(ck)
            while len(self._day_cache) > self._max_cache_days:
                self._day_cache.popitem(last=False)
        return {**built, "cache_hit": False}

    def compute_overnight_weights(
        self,
        *,
        symbols: Sequence[str],
        raw_weights: Mapping[str, float],
        anchors: Mapping[str, str],
        causal_ctx: Mapping[str, Any],
        policy: Mapping[str, Any] | None = None,
        engine_type: str = "route_d_plus",
        static_caps: Mapping[str, float] | None = None,
        pool_cap: float | None = None,
    ) -> dict[str, Any]:
        """Compute portfolio target weights across engine families with optional vol ceiling."""
        norm_pol = normalize_pool_policy(policy)
        clean_syms = [str(s).strip() for s in symbols if str(s).strip()]
        raw_w = {s: max(0.0, float(raw_weights.get(s) or 0.0)) for s in clean_syms}
        active_syms = [s for s in clean_syms if raw_w[s] > 0.0]
        clean_engine = str(engine_type or "route_d_plus").strip()
        if clean_engine not in VALID_POLICY_ENGINES:
            clean_engine = "route_d_plus"

        if not active_syms:
            return {
                "alloc_weights": {s: 0.0 for s in clean_syms},
                "caps": {s: 0.0 for s in clean_syms},
                "scores": {s: 0.0 for s in clean_syms},
                "portfolio_sigma_pct": 0.0,
                "pre_cap_sigma_pct": 0.0,
                "sigma_scaled": False,
                "is_solo": False,
                "active_symbols": [],
                "avg_active_rho": {},
            }

        is_solo = len(active_syms) == 1
        snaps = causal_ctx.get("snaps") or {}
        vols = causal_ctx.get("vols") or {}
        corrs = causal_ctx.get("corrs") or {}
        mult_map = norm_pol.get("symbol_weight_multipliers") or {}
        sym_max_map = norm_pol.get("symbol_max_caps") or {}
        rho_thresh = float(norm_pol["rho_thresh"])
        rho_slope = float(norm_pol["rho_slope"])
        crowd_rho_thresh = float(norm_pol["crowd_rho_thresh"])
        crowd_penalty = float(norm_pol["crowd_penalty"])
        multi_base = float(norm_pol["multi_base_coef"])
        multi_min = float(norm_pol["multi_min_coef"])
        multi_max = float(norm_pol["multi_cap_max"])
        cap_floor = float(norm_pol["cap_floor"])
        max_budget_wt = float(norm_pol["max_total_budget_wt"])

        scores: dict[str, float] = {}
        caps: dict[str, float] = {}
        avg_active_rho: dict[str, float] = {}
        alloc: dict[str, float] = {s: 0.0 for s in clean_syms}

        def _cap_sym(sym: str, default_hi: float) -> float:
            if sym in sym_max_map:
                return min(default_hi, float(sym_max_map[sym]))
            return default_hi

        if clean_engine == "static_cap":
            tot_static = float(pool_cap or 0.0)
            if tot_static <= 0.0 and static_caps:
                tot_static = sum(max(0.0, float(static_caps.get(s) or 0.0)) for s in clean_syms)
            tot_static = max(1.0, tot_static)
            for s in clean_syms:
                wt = raw_w[s]
                cap_ratio = max(0.0, float((static_caps or {}).get(s) or 0.0)) / tot_static if static_caps else (1.0 / max(1, len(clean_syms)))
                cap_ratio = _cap_sym(s, min(1.0, cap_ratio))
                caps[s] = round(cap_ratio, 6)
                scores[s] = round(wt * cap_ratio, 6)
                alloc[s] = min(1.0, wt) * cap_ratio if wt > 0.0 else 0.0
        elif clean_engine in {"signal_proportional", "inverse_vol_risk_parity", "equal_weight"}:
            for s in clean_syms:
                wt = raw_w[s]
                if wt <= 0.0:
                    scores[s] = 0.0
                    caps[s] = 0.0
                    continue
                mult = float(mult_map.get(s, 1.0))
                if is_solo:
                    _, cap_hi = resolve_solo_cap_params(str(anchors.get(s) or "brent"), norm_pol)
                else:
                    cap_hi = multi_max
                cap_hi = _cap_sym(s, cap_hi)
                caps[s] = cap_hi
                if clean_engine == "equal_weight":
                    scores[s] = max(0.01, mult)
                elif clean_engine == "inverse_vol_risk_parity":
                    vol = max(0.5, float(vols.get(s, 3.20)))
                    scores[s] = max(0.0, wt * mult / vol)
                else:
                    scores[s] = max(0.0, wt * mult)
            tot_sc = sum(scores[s] for s in active_syms)
            if clean_engine == "equal_weight":
                eq_budget = min(max_budget_wt, sum(caps[s] for s in active_syms))
                budget = eq_budget
            else:
                budget = min(max_budget_wt, sum(min(caps[s], raw_w[s] * caps[s]) for s in active_syms))
            rem_budget = budget
            rem_syms = list(active_syms)
            for _ in range(len(active_syms) + 1):
                cur_tot = sum(scores[s] for s in rem_syms)
                if cur_tot <= 1e-12 or rem_budget <= 1e-9 or not rem_syms:
                    break
                capped_any = False
                next_rem: list[str] = []
                for s in rem_syms:
                    cand = alloc[s] + rem_budget * (scores[s] / cur_tot)
                    if cand >= caps[s] - 1e-9:
                        rem_budget -= max(0.0, caps[s] - alloc[s])
                        alloc[s] = caps[s]
                        capped_any = True
                    else:
                        next_rem.append(s)
                if not capped_any:
                    for s in rem_syms:
                        alloc[s] += rem_budget * (scores[s] / cur_tot)
                    break
                rem_syms = next_rem
        else:
            for s in clean_syms:
                wt = raw_w[s]
                if wt <= 0.0:
                    scores[s] = 0.0
                    caps[s] = 0.0
                    continue
                ss = snaps.get(s) or bayesian_prior_snapshot()
                m_rec = float(ss["m_rec"])
                mult = float(mult_map.get(s, 1.0))
                kelly_w = float(ss["k_ov_strong"] if wt >= 0.70 else ss["k_ov_mod"])
                sc = wt * (0.45 + 1.40 * kelly_w) * (m_rec ** 1.25) * mult
                if is_solo:
                    eff_coef, cap_hi = resolve_solo_cap_params(str(anchors.get(s) or "brent"), norm_pol)
                else:
                    eff_coef, cap_hi = multi_base, multi_max
                    oth_w = sum(raw_w[o] for o in active_syms if o != s)
                    if oth_w > 0.0:
                        avg_rho = sum(
                            raw_w[o] * max(0.0, float(corrs.get((s, o), corrs.get((o, s), 0.55))))
                            for o in active_syms
                            if o != s
                        ) / oth_w
                        avg_active_rho[s] = round(avg_rho, 4)
                        eff_coef = max(multi_min, multi_base - rho_slope * max(0.0, avg_rho - rho_thresh))
                        sc *= max(0.05, 1.0 - crowd_penalty * max(0.0, avg_rho - crowd_rho_thresh))
                cap_hi = _cap_sym(s, cap_hi)
                scores[s] = sc
                caps[s] = _clip(eff_coef * m_rec * (1.0 if mult >= 1.0 else mult), min(cap_floor, cap_hi), cap_hi)

            budget = min(
                max_budget_wt,
                sum(min(caps[s], max(0.28, raw_w[s] * caps[s])) for s in active_syms),
            )
            rem_budget = budget
            rem_syms = list(active_syms)
            for _ in range(len(active_syms) + 1):
                tot_sc = sum(scores[s] for s in rem_syms)
                if tot_sc <= 1e-12 or rem_budget <= 1e-9 or not rem_syms:
                    break
                capped_any = False
                next_rem: list[str] = []
                for s in rem_syms:
                    cand = alloc[s] + rem_budget * (scores[s] / tot_sc)
                    if cand >= caps[s] - 1e-9:
                        rem_budget -= max(0.0, caps[s] - alloc[s])
                        alloc[s] = caps[s]
                        capped_any = True
                    else:
                        next_rem.append(s)
                if not capped_any:
                    for s in rem_syms:
                        alloc[s] += rem_budget * (scores[s] / tot_sc)
                    break
                rem_syms = next_rem

        pvar = sum(
            alloc[s1]
            * alloc[s2]
            * float(vols.get(s1, 3.20))
            * float(vols.get(s2, 3.20))
            * (1.0 if s1 == s2 else float(corrs.get((s1, s2), corrs.get((s2, s1), 0.55))))
            for s1 in active_syms
            for s2 in active_syms
        )
        pstd = math.sqrt(max(0.0, pvar))
        post_sigma = pstd
        sigma_scaled = False
        sigma_cap = None if clean_engine == "static_cap" else norm_pol.get("sigma_cap_pct")
        if sigma_cap is not None and float(sigma_cap) > 0.0 and pstd > float(sigma_cap) and pstd > 1e-9:
            scale_v = float(sigma_cap) / pstd
            for s in active_syms:
                alloc[s] *= scale_v
            post_sigma = float(sigma_cap)
            sigma_scaled = True

        return {
            "alloc_weights": {s: round(alloc[s], 6) for s in clean_syms},
            "caps": {s: round(caps[s], 6) for s in clean_syms},
            "scores": {s: round(scores[s], 6) for s in clean_syms},
            "portfolio_sigma_pct": round(post_sigma, 4),
            "pre_cap_sigma_pct": round(pstd, 4),
            "sigma_scaled": sigma_scaled,
            "is_solo": is_solo,
            "active_symbols": active_syms,
            "avg_active_rho": avg_active_rho,
        }

    def plan_overnight_rebalance(
        self,
        *,
        symbols: Sequence[str],
        raw_weights: Mapping[str, float],
        current_shares: Mapping[str, int],
        prices: Mapping[str, float],
        min_retained_by_symbol: Mapping[str, int] | None = None,
        pool_equity: float,
        available_pool_cash: float,
        anchors: Mapping[str, str],
        causal_ctx: Mapping[str, Any],
        policy: Mapping[str, Any] | None = None,
        lot_size: int = _LOT_SIZE,
        skip_order_symbols: Sequence[str] = (),
        engine_type: str = "route_d_plus",
    ) -> dict[str, Any]:
        """Plan two-wave (sell first, buy second) 14:53 rebalance under 100-share floor conservation."""
        norm_pol = normalize_pool_policy(policy)
        single_fee = float(norm_pol["single_fee_rate"])
        enforce_floor = bool(norm_pol["enforce_min_retained_floor"])
        clean_syms = [str(s).strip() for s in symbols if str(s).strip()]
        skip_set = {str(s).strip() for s in skip_order_symbols if str(s).strip()}

        wt_res = self.compute_overnight_weights(
            symbols=clean_syms,
            raw_weights=raw_weights,
            anchors=anchors,
            causal_ctx=causal_ctx,
            policy=norm_pol,
            engine_type=engine_type,
        )
        alloc_wts = wt_res["alloc_weights"]

        floor_by_sym: dict[str, int] = {}
        for s in clean_syms:
            if not enforce_floor:
                floor_by_sym[s] = 0
            elif min_retained_by_symbol is not None and s in min_retained_by_symbol:
                floor_by_sym[s] = max(0, int(min_retained_by_symbol[s]))
            else:
                floor_by_sym[s] = lot_size

        floor_mv_locked = 0.0
        for s in clean_syms:
            fl = floor_by_sym[s]
            px = float(prices.get(s) or 0.0)
            if fl > 0 and px > 0.0 and float(alloc_wts.get(s) or 0.0) <= 0.0:
                need_mv = fl * px * (1.0 + single_fee)
                if float(pool_equity) - floor_mv_locked >= need_mv:
                    floor_mv_locked += need_mv

        alloc_eq = max(0.0, float(pool_equity) - floor_mv_locked)
        target_shares: dict[str, int] = {}
        sell_orders: dict[str, int] = {}
        buy_diffs: dict[str, int] = {}

        for s in clean_syms:
            sh = max(0, int(current_shares.get(s) or 0))
            fl = floor_by_sym[s]
            px = float(prices.get(s) or 0.0)
            if px <= 0.0:
                target_shares[s] = sh
                continue
            raw_tgt = int((alloc_eq * alloc_wts[s]) // (px * lot_size * (1.0 + single_fee))) * lot_size
            tgt_sh = max(fl, raw_tgt) if (fl > 0 and float(pool_equity) >= fl * px * (1.0 + single_fee)) else raw_tgt
            target_shares[s] = tgt_sh
            if s in skip_set:
                continue
            diff_sh = tgt_sh - sh
            if diff_sh <= -lot_size:
                sellable = max(0, sh - fl)
                q_sell = min((-diff_sh // lot_size) * lot_size, sellable)
                if q_sell >= lot_size:
                    sell_orders[s] = q_sell
            elif diff_sh >= lot_size:
                buy_diffs[s] = (diff_sh // lot_size) * lot_size

        est_sell_proceeds = sum(
            q * float(prices.get(s) or 0.0) * (1.0 - single_fee)
            for s, q in sell_orders.items()
        )
        post_sell_cash = max(0.0, float(available_pool_cash) + est_sell_proceeds)
        buy_orders = scale_buy_orders_to_cash(
            buy_diffs,
            prices,
            post_sell_cash,
            single_fee,
            lot_size=lot_size,
        )
        est_buy_cost = sum(
            q * float(prices.get(s) or 0.0) * (1.0 + single_fee)
            for s, q in buy_orders.items()
        )
        return {
            **wt_res,
            "pool_equity": round(float(pool_equity), 2),
            "floor_mv_locked": round(floor_mv_locked, 2),
            "alloc_equity": round(alloc_eq, 2),
            "target_shares": target_shares,
            "sell_orders": sell_orders,
            "buy_diffs": buy_diffs,
            "buy_orders": buy_orders,
            "estimated_post_sell_cash": round(post_sell_cash, 2),
            "estimated_post_buy_cash": round(max(0.0, post_sell_cash - est_buy_cost), 2),
        }

    def compute_symbol_intraday_cap(
        self,
        *,
        symbol: str,
        symbols: Sequence[str],
        held_mvs: Mapping[str, float],
        pool_equity: float,
        anchors: Mapping[str, str],
        causal_ctx: Mapping[str, Any],
        policy: Mapping[str, Any] | None = None,
        engine_type: str = "route_d_plus",
        static_caps: Mapping[str, float] | None = None,
    ) -> dict[str, Any]:
        """Compute dynamic intraday exposure limit and remaining room for one symbol."""
        norm_pol = normalize_pool_policy(policy)
        clean_syms = [str(s).strip() for s in symbols if str(s).strip()]
        snaps = causal_ctx.get("snaps") or {}
        corrs = causal_ctx.get("corrs") or {}
        vols = causal_ctx.get("vols") or {}
        ss = snaps.get(symbol) or bayesian_prior_snapshot()
        m_rec = float(ss["m_rec"])
        sentry = float(ss["sentry"])
        eq_base = max(0.0, float(pool_equity))
        sym_max_map = norm_pol.get("symbol_max_caps") or {}
        mult_map = norm_pol.get("symbol_weight_multipliers") or {}
        clean_engine = str(engine_type or "route_d_plus").strip()

        if clean_engine == "static_cap":
            st_cap = max(0.0, float((static_caps or {}).get(symbol) or 0.0))
            r_max = min(1.0, st_cap / eq_base) if eq_base > 0 else 0.0
            cap_amt = round(st_cap, 2)
            held_mv = max(0.0, float(held_mvs.get(symbol) or 0.0))
            return {
                "symbol": symbol,
                "r_max": round(r_max, 6),
                "cap_amount": cap_amt,
                "held_mv": round(held_mv, 2),
                "room_amount": round(max(0.0, cap_amt - held_mv), 2),
                "is_multi_context": len(clean_syms) > 1,
                "avg_other_rho": None,
                "m_rec": round(m_rec, 4),
                "sentry": sentry,
            }

        oth_mv = sum(max(0.0, float(held_mvs.get(o) or 0.0)) for o in clean_syms if o != symbol)
        is_multi_context = eq_base > 0.0 and oth_mv > eq_base * 0.15
        avg_r = 0.55
        if is_multi_context and oth_mv > 0.0:
            avg_r = sum(
                max(0.0, float(held_mvs.get(o) or 0.0))
                * max(0.0, float(corrs.get((symbol, o), corrs.get((o, symbol), 0.55))))
                for o in clean_syms
                if o != symbol
            ) / oth_mv
            if clean_engine == "inverse_vol_risk_parity":
                inv_map = {s: max(0.05, float(mult_map.get(s, 1.0)) / max(0.5, float(vols.get(s, 3.20)))) for s in clean_syms}
                inv_sum = sum(inv_map.values()) or 1.0
                eff_in = float(norm_pol["max_total_budget_wt"]) * (inv_map.get(symbol, 0.25) / inv_sum) / max(0.25, m_rec)
            elif clean_engine in {"signal_proportional", "equal_weight"}:
                w_sum = sum(max(0.05, float(mult_map.get(s, 1.0))) for s in clean_syms) or 1.0
                eff_in = float(norm_pol["max_total_budget_wt"]) * (float(mult_map.get(symbol, 1.0)) / w_sum) / max(0.25, m_rec)
            else:
                eff_in = max(
                    float(norm_pol["multi_min_coef"]),
                    float(norm_pol["multi_base_coef"])
                    - float(norm_pol["intra_rho_slope"]) * max(0.0, avg_r - float(norm_pol["rho_thresh"])),
                )
            cap_hi = float(norm_pol["multi_cap_max"])
        else:
            eff_in, cap_hi = resolve_solo_cap_params(str(anchors.get(symbol) or "brent"), norm_pol)

        if symbol in sym_max_map:
            cap_hi = min(cap_hi, float(sym_max_map[symbol]))
        cap_floor = min(float(norm_pol["cap_floor"]), cap_hi)
        r_max = _clip(eff_in * (m_rec if clean_engine in {"route_d_plus", "shadow_route_d"} else 1.0), cap_floor, cap_hi)
        cap_amt = round(r_max * eq_base, 2)
        held_mv = max(0.0, float(held_mvs.get(symbol) or 0.0))
        return {
            "symbol": symbol,
            "r_max": round(r_max, 6),
            "cap_amount": cap_amt,
            "held_mv": round(held_mv, 2),
            "room_amount": round(max(0.0, cap_amt - held_mv), 2),
            "is_multi_context": is_multi_context,
            "avg_other_rho": round(avg_r, 4) if is_multi_context else None,
            "m_rec": round(m_rec, 4),
            "sentry": sentry,
        }

    def coordinate_intraday_buys(
        self,
        *,
        buy_requests: Sequence[Mapping[str, Any]],
        symbols: Sequence[str],
        held_mvs: Mapping[str, float],
        pool_equity: float,
        free_pool_cash: float,
        anchors: Mapping[str, str],
        causal_ctx: Mapping[str, Any],
        policy: Mapping[str, Any] | None = None,
        lot_size: int = _LOT_SIZE,
        engine_type: str = "route_d_plus",
    ) -> dict[str, dict[str, Any]]:
        """Size and coordinate simultaneous intraday long entries across the shared cash pool."""
        norm_pol = normalize_pool_policy(policy)
        single_fee = float(norm_pol["single_fee_rate"])
        snaps = causal_ctx.get("snaps") or {}
        eq_base = max(0.0, float(pool_equity))
        free_cash = max(0.0, float(free_pool_cash))
        clean_engine = str(engine_type or "route_d_plus").strip()

        results: dict[str, dict[str, Any]] = {}
        planned: dict[str, float] = {}
        sc_b: dict[str, float] = {}
        req_map: dict[str, Mapping[str, Any]] = {}
        cap_info_map: dict[str, dict[str, Any]] = {}

        for req in buy_requests:
            sym = str(req["symbol"]).strip()
            pr = float(req.get("price") or 0.0)
            sf = max(0.0, min(1.0, float(req.get("signal_frac") or 1.0)))
            sig_tag = str(req.get("signal_tag") or "v9-entry")
            bkt = str(
                req.get("bucket")
                or (
                    "buy_scalp"
                    if sig_tag == "v9-scalp"
                    else ("buy_tier_a" if sf >= 0.95 else ("buy_tier_b" if sf >= 0.75 else "buy_tier_c"))
                )
            )
            ss = snaps.get(sym) or bayesian_prior_snapshot()
            m_rec, sentry = float(ss["m_rec"]), float(ss["sentry"])
            intra_map = ss.get("intra") or {}
            p_bkt, k_bkt = intra_map.get(bkt) or intra_map.get("buy_tier_c") or (0.58, 0.2767)
            p_bkt_f, k_bkt_f = float(p_bkt), float(k_bkt)

            cap_info = self.compute_symbol_intraday_cap(
                symbol=sym,
                symbols=symbols,
                held_mvs=held_mvs,
                pool_equity=eq_base,
                anchors=anchors,
                causal_ctx=causal_ctx,
                policy=norm_pol,
                engine_type=clean_engine,
            )
            cap_info_map[sym] = cap_info

            min_lot_cost = pr * lot_size * (1.0 + single_fee)
            if pr <= 0.0 or eq_base <= 0.0 or free_cash < min_lot_cost:
                results[sym] = {
                    "approved": False,
                    "qty": 0,
                    "reason": "insufficient_pool_cash",
                    "cap_info": cap_info,
                    "p_bkt": round(p_bkt_f, 4),
                }
                continue
            if p_bkt_f < 0.48:
                results[sym] = {
                    "approved": False,
                    "qty": 0,
                    "reason": f"bayesian_win_rate_below_floor({p_bkt_f:.3f}<0.48)",
                    "cap_info": cap_info,
                    "p_bkt": round(p_bkt_f, 4),
                }
                continue

            room = float(cap_info["room_amount"])
            r_max = float(cap_info["r_max"])
            if room < min_lot_cost:
                results[sym] = {
                    "approved": False,
                    "qty": 0,
                    "reason": "intraday_cap_room_full",
                    "cap_info": cap_info,
                    "p_bkt": round(p_bkt_f, 4),
                }
                continue

            held_mv = max(0.0, float(held_mvs.get(sym) or 0.0))
            held_ratio = held_mv / eq_base
            is_s = sf >= 0.90 or p_bkt_f >= 0.66
            fk = _clip(k_bkt_f * 0.85, 0.10, 0.52)
            c_dec = (1.0 - min(0.95, held_ratio / max(0.05, r_max))) ** 0.60
            avail_cap = max(0.0, free_cash - (0.0 if is_s else free_cash * 0.12))
            desire = eq_base * (0.18 + 0.95 * fk) * sf * m_rec * c_dec
            if sentry > 0.0 and p_bkt_f >= 0.52:
                desire = max(desire, min_lot_cost * 1.02)
            cap_amt = min(desire, room, avail_cap)
            if cap_amt < min_lot_cost:
                results[sym] = {
                    "approved": False,
                    "qty": 0,
                    "reason": "budget_below_one_lot",
                    "cap_info": cap_info,
                    "p_bkt": round(p_bkt_f, 4),
                }
                continue

            planned[sym] = cap_amt
            sc_b[sym] = max(0.01, fk * m_rec * sf * c_dec)
            req_map[sym] = req

        if planned:
            if sum(planned.values()) > free_cash:
                tot_sb = sum(sc_b.values())
                if tot_sb > 0.0:
                    for sym in list(planned):
                        planned[sym] = min(planned[sym], free_cash * (sc_b[sym] / tot_sb))
            rem_c = free_cash
            for sym in sorted(planned, key=lambda key: sc_b[key], reverse=True):
                req = req_map[sym]
                pr = float(req["price"])
                unit_cost = pr * lot_size * (1.0 + single_fee)
                q = int(min(planned[sym], rem_c) // unit_cost) * lot_size if unit_cost > 0 else 0
                cost = q * pr * (1.0 + single_fee)
                if q >= lot_size and rem_c >= cost - 1e-6:
                    rem_c -= cost
                    held_mv = max(0.0, float(held_mvs.get(sym) or 0.0))
                    dyn_cap = round(min(float(cap_info_map[sym]["cap_amount"]) * 1.02, held_mv + max(planned[sym], q * pr) * 1.03), 2)
                    results[sym] = {
                        "approved": True,
                        "qty": q,
                        "planned_budget": round(planned[sym], 2),
                        "dynamic_cap_limit": dyn_cap,
                        "score": round(sc_b[sym], 6),
                        "cap_info": cap_info_map[sym],
                    }
                else:
                    results[sym] = {
                        "approved": False,
                        "qty": 0,
                        "reason": "pool_cash_exhausted_by_higher_score",
                        "cap_info": cap_info_map[sym],
                    }
        return results


def _now_shanghai_iso() -> str:
    return datetime.now(timezone.utc).astimezone(_SHANGHAI_TZ).isoformat(timespec="seconds")


def _normalize_policy_metrics(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    out: dict[str, Any] = {}
    for key in ("sharpe_ratio", "max_drawdown_pct", "calmar_ratio", "net_return_pct", "avg_utilization_pct"):
        if key in raw and raw.get(key) is not None:
            try:
                val = float(raw.get(key))
                if math.isfinite(val):
                    out[key] = round(val, 3 if key == "sharpe_ratio" else 2)
            except (TypeError, ValueError):
                pass
    if raw.get("benchmark"):
        out["benchmark"] = str(raw["benchmark"]).strip()[:80]
    return out or None


BUILTIN_PORTFOLIO_POLICIES: tuple[dict[str, Any], ...] = (
    {
        "id": "route_d_sharpe_shield",
        "name": "Route D++ 高夏普控回撤旗舰版 (σ≤2.4%)",
        "description": "4标的实证夏普最高(5.653)、回撤最低(6.71%)：日波动率封顶2.40%，XOP美股独苗放宽0.64/85%，原油独苗审慎控仓0.30/42%，尾盘卖先于买两阶段协同。",
        "source": "builtin",
        "engine_type": "route_d_plus",
        "params": {
            "sigma_cap_pct": 2.40,
            "solo_equity_coef": 0.64,
            "solo_equity_cap": 0.85,
            "solo_crude_coef": 0.30,
            "solo_crude_cap": 0.42,
            "rho_thresh": 0.62,
            "equity_solo_anchors": ["xop_us_ep"],
        },
        "metrics_summary": {
            "sharpe_ratio": 5.653,
            "max_drawdown_pct": 6.71,
            "calmar_ratio": 44.47,
            "net_return_pct": 172.85,
            "benchmark": "4标的183日实证",
        },
        "is_builtin": True,
        "enabled": True,
    },
    {
        "id": "route_d_calmar_peak",
        "name": "Route D++ 卡玛均衡优选版 (σ≤2.6%)",
        "description": "收益风险比峰值(Calmar 44.59)：日波动率封顶2.60%，XOP独苗0.64/85%，原油独苗0.30/42%，兼顾高夏普(5.599)与+180.37%高收益。",
        "source": "builtin",
        "engine_type": "route_d_plus",
        "params": {
            "sigma_cap_pct": 2.60,
            "solo_equity_coef": 0.64,
            "solo_equity_cap": 0.85,
            "solo_crude_coef": 0.30,
            "solo_crude_cap": 0.42,
            "rho_thresh": 0.62,
            "equity_solo_anchors": ["xop_us_ep"],
        },
        "metrics_summary": {
            "sharpe_ratio": 5.599,
            "max_drawdown_pct": 7.03,
            "calmar_ratio": 44.59,
            "net_return_pct": 180.37,
            "benchmark": "4标的183日实证",
        },
        "is_builtin": True,
        "enabled": True,
    },
    {
        "id": "route_d_plus",
        "name": "Route D++ 进取高收益版 (σ≤4.2%)",
        "description": "日波动率封顶4.20%，XOP独苗0.64/85%、原油独苗0.48/68%，4标的183日累计+195.44%（Sharpe 5.274 / 回撤8.93%）。",
        "source": "builtin",
        "engine_type": "route_d_plus",
        "params": {
            "sigma_cap_pct": 4.20,
            "solo_equity_coef": 0.64,
            "solo_equity_cap": 0.85,
            "solo_crude_coef": 0.48,
            "solo_crude_cap": 0.68,
            "rho_thresh": 0.62,
            "equity_solo_anchors": ["xop_us_ep"],
        },
        "metrics_summary": {
            "sharpe_ratio": 5.274,
            "max_drawdown_pct": 8.93,
            "calmar_ratio": 38.57,
            "net_return_pct": 195.44,
            "benchmark": "4标的183日实证",
        },
        "is_builtin": True,
        "enabled": True,
    },
    {
        "id": "manual_cap",
        "name": "人工配置（手动设定每股分配金额）",
        "description": "人工配置模式：仅在此模式下允许在下方「多标的量化资金池与持仓」逐只手动修改每股分配金额；其他选项均为量化资金配比，动态填入资金上限并锁定。",
        "source": "builtin",
        "engine_type": "static_cap",
        "params": {},
        "metrics_summary": None,
        "is_builtin": True,
        "enabled": True,
    },
    {
        "id": "static_cap",
        "name": "静态分仓独立上限策略",
        "description": "传统各标的固定独立资金上限模式，互不借调资金（4标的183日实证 +57.88% / 回撤14.88% / Sharpe 2.579）。",
        "source": "builtin",
        "engine_type": "static_cap",
        "params": {},
        "metrics_summary": {
            "sharpe_ratio": 2.579,
            "max_drawdown_pct": 14.88,
            "calmar_ratio": 5.88,
            "net_return_pct": 57.88,
            "benchmark": "4标的183日实证",
        },
        "is_builtin": True,
        "enabled": True,
    },
    {
        "id": "route_d_shadow",
        "name": "Route D++ 影子对照模式（仅看板测算）",
        "description": "实盘仍按各标的静态独立上限下单，同时在后台旁路实时测算 Route D++ 目标仓位与资金占比。",
        "source": "builtin",
        "engine_type": "shadow_route_d",
        "params": {
            "sigma_cap_pct": 2.40,
            "solo_equity_coef": 0.64,
            "solo_equity_cap": 0.85,
            "solo_crude_coef": 0.30,
            "solo_crude_cap": 0.42,
            "rho_thresh": 0.62,
            "equity_solo_anchors": ["xop_us_ep"],
        },
        "metrics_summary": None,
        "is_builtin": True,
        "enabled": True,
    },
)


def get_portfolio_policy_schema() -> dict[str, Any]:
    """Return JSON contract for publishing dynamic portfolio allocation strategies via API."""
    return {
        "version": 1,
        "engines": [
            {"id": "route_d_plus", "label": "Route D++ 因果协方差+异质独苗动态共享池（实盘执行）"},
            {"id": "inverse_vol_risk_parity", "label": "逆波动率风险平价动态共享池（按 1/σ 加权，实盘执行）"},
            {"id": "signal_proportional", "label": "原始信号比例动态共享池（按 raw_wt×权重乘子，实盘执行）"},
            {"id": "equal_weight", "label": "有效信号等权动态共享池（实盘执行）"},
            {"id": "shadow_route_d", "label": "Route D++ 影子对照（仅测算展示，实盘仍走静态分仓）"},
            {"id": "static_cap", "label": "静态分仓独立上限"},
        ],
        "params_schema": {
            "sigma_cap_pct": {"type": "number|null", "range": "0.5..25.0 or null", "default": 4.20, "note": "组合目标日波动率上限(%)，越小越控回撤"},
            "rho_thresh": {"type": "number", "range": "0.0..0.95", "default": 0.62, "note": "多标的同向相关性惩罚起罚阈值"},
            "rho_slope": {"type": "number", "range": "0.0..1.0", "default": 0.26, "note": "尾盘多标的相关性惩罚斜率"},
            "intra_rho_slope": {"type": "number", "range": "0.0..1.0", "default": 0.24, "note": "盘中动态上限相关性惩罚斜率"},
            "lookback_days": {"type": "integer", "range": "5..120", "default": 20, "note": "EWMA 波动率与相关性滑动窗交易日数"},
            "solo_equity_coef": {"type": "number", "range": "0.10..1.0", "default": 0.64, "note": "美股/权益锚独苗基准仓位系数"},
            "solo_equity_cap": {"type": "number", "range": "0.15..1.0", "default": 0.85, "note": "美股/权益锚独苗最高仓位上限"},
            "solo_crude_coef": {"type": "number", "range": "0.10..1.0", "default": 0.48, "note": "原油锚独苗基准仓位系数"},
            "solo_crude_cap": {"type": "number", "range": "0.15..1.0", "default": 0.68, "note": "原油锚独苗最高仓位上限"},
            "multi_base_coef": {"type": "number", "range": "0.10..1.0", "default": 0.48, "note": "多标的并存单票基准系数"},
            "multi_min_coef": {"type": "number", "range": "0.05..0.95", "default": 0.36, "note": "高相关性下多票最低系数下限"},
            "multi_cap_max": {"type": "number", "range": "0.15..1.0", "default": 0.68, "note": "多标并存单票最高仓位上限"},
            "max_total_budget_wt": {"type": "number", "range": "0.20..1.0", "default": 0.985, "note": "组合总仓位预算上限"},
            "equity_solo_anchors": {"type": "array[string]", "default": ["xop_us_ep"], "note": "适用权益独苗上限的跨市场锚"},
            "symbol_weight_multipliers": {"type": "object<symbol,number>", "default": {}, "note": "按6位证券代码微调得分/权重乘子(0~3)"},
            "symbol_max_caps": {"type": "object<symbol,number>", "default": {}, "note": "按6位证券代码限定最高池占比(0.05~1.0)"},
            "enforce_min_retained_floor": {"type": "boolean", "default": True, "note": "是否强制每只启用标的无条件保留百股底仓"},
            "enable_morning_base_catchup": {"type": "boolean", "default": True, "note": "隔夜有目标仓位但早盘零持仓时，在未大幅高开前提下补建底仓"},
            "morning_catchup_end_hhmm": {"type": "string", "default": "09:50", "note": "早盘补底仓截止时间(HH:MM，09:32起生效)"},
            "morning_catchup_max_gap_pct": {"type": "number", "default": 2.50, "min": 0.20, "max": 9.50, "note": "早盘补底仓允许相对昨收最大涨幅(%)"},
            "morning_catchup_max_sp_fx_pct": {"type": "number", "default": 0.38, "min": -1.0, "max": 3.0, "note": "早盘补底仓允许相对汇率调整外盘锚最大溢价(%)"},
            "morning_catchup_max_prem_pct": {"type": "number", "default": 2.00, "min": -2.0, "max": 15.0, "note": "早盘补底仓允许最大LOF实时溢价(%)"},
        },
        "payload_example": {
            "id": "custom-risk-parity-22",
            "name": "自定义低波风险平价池 (σ≤2.2%)",
            "description": "按近20日EWMA波动率倒数动态配权，组合日波封顶2.2%",
            "engine_type": "inverse_vol_risk_parity",
            "params": {
                "sigma_cap_pct": 2.20,
                "solo_equity_cap": 0.80,
                "multi_cap_max": 0.55,
                "max_total_budget_wt": 0.95,
                "symbol_weight_multipliers": {"162411": 1.25},
            },
            "metrics_summary": {
                "sharpe_ratio": 5.4,
                "max_drawdown_pct": 6.5,
                "net_return_pct": 165.0,
                "benchmark": "4标的回测",
            },
        },
    }


class SQLitePortfolioPolicyStore:
    """Thread-safe WAL SQLite registry for built-in and API-published portfolio capital policies."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_capital_policies (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    source TEXT NOT NULL,
                    engine_type TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    metrics_json TEXT,
                    is_builtin INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            now_str = _now_shanghai_iso()
            for item in BUILTIN_PORTFOLIO_POLICIES:
                params_norm = normalize_pool_policy(item.get("params"))
                params_norm["total_pool_cap"] = None
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO portfolio_capital_policies (
                        id, name, description, source, engine_type,
                        params_json, metrics_json, is_builtin, enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["id"],
                        item["name"],
                        item["description"],
                        item["source"],
                        item["engine_type"],
                        json.dumps(params_norm, ensure_ascii=False),
                        json.dumps(item["metrics_summary"], ensure_ascii=False)
                        if item.get("metrics_summary")
                        else None,
                        1,
                        1 if item.get("enabled", True) else 0,
                        now_str,
                        now_str,
                    ),
                )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        try:
            raw_params = json.loads(row["params_json"])
        except Exception:
            raw_params = {}
        try:
            raw_metrics = json.loads(row["metrics_json"]) if row["metrics_json"] else None
        except Exception:
            raw_metrics = None
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "source": row["source"],
            "engine_type": row["engine_type"],
            "params": normalize_pool_policy(raw_params),
            "metrics_summary": raw_metrics,
            "is_builtin": bool(row["is_builtin"]),
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_policies(self, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        order_map = {item["id"]: idx for idx, item in enumerate(BUILTIN_PORTFOLIO_POLICIES)}
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM portfolio_capital_policies"
                + ("" if include_disabled else " WHERE enabled = 1")
                + " ORDER BY is_builtin DESC, updated_at DESC"
            ).fetchall()
        items = [self._row_to_dict(row) for row in rows]
        items.sort(key=lambda it: (0 if it["is_builtin"] else 1, order_map.get(it["id"], 999), it["name"]))
        return items

    def get_policy(self, policy_id: str) -> dict[str, Any] | None:
        clean_id = str(policy_id or "").strip()
        if not clean_id:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM portfolio_capital_policies WHERE id = ?",
                (clean_id,),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def upsert_policy(
        self,
        payload: Mapping[str, Any],
        *,
        policy_id: str | None = None,
    ) -> dict[str, Any]:
        clean_id = str(policy_id or payload.get("id") or "").strip()
        if not clean_id:
            slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(payload.get("name") or "policy")).strip("-").lower()[:36]
            clean_id = f"custom-{slug or 'pool'}-{datetime.now(timezone.utc).strftime('%H%M%S')}"
        if not is_valid_policy_id(clean_id):
            raise ValueError("策略 id 需为 2-64 位字母、数字、点、下划线或连字符")

        name = str(payload.get("name") or "").strip()
        if not name or len(name) > 120:
            raise ValueError("策略名称长度需在 1-120 字之间")
        description = str(payload.get("description") or "").strip()[:400]
        engine_type = str(payload.get("engine_type") or "route_d_plus").strip()
        if engine_type not in VALID_POLICY_ENGINES:
            raise ValueError(f"不支持的策略引擎类型: {engine_type}（可选: {', '.join(VALID_POLICY_ENGINES)}）")

        params_raw = payload.get("params")
        if params_raw is not None and not isinstance(params_raw, Mapping):
            raise ValueError("params 必须为 JSON 对象")
        norm_params = normalize_pool_policy(params_raw)
        norm_params["total_pool_cap"] = None
        metrics = _normalize_policy_metrics(payload.get("metrics_summary"))
        enabled = bool(payload["enabled"]) if "enabled" in payload else True
        now_str = _now_shanghai_iso()

        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT is_builtin, source, created_at FROM portfolio_capital_policies WHERE id = ?",
                (clean_id,),
            ).fetchone()
            is_builtin = bool(existing["is_builtin"]) if existing is not None else False
            if is_builtin and engine_type not in VALID_POLICY_ENGINES:
                raise ValueError("内置策略引擎不可置空")
            source = (
                str(existing["source"])
                if is_builtin and existing is not None
                else (str(payload.get("source") or (existing["source"] if existing else "api")).strip() or "api")
            )
            created_at = str(existing["created_at"]) if existing is not None else now_str
            self._connection.execute(
                """
                INSERT INTO portfolio_capital_policies (
                    id, name, description, source, engine_type,
                    params_json, metrics_json, is_builtin, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    source = excluded.source,
                    engine_type = excluded.engine_type,
                    params_json = excluded.params_json,
                    metrics_json = excluded.metrics_json,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    clean_id,
                    name,
                    description,
                    source,
                    engine_type,
                    json.dumps(norm_params, ensure_ascii=False),
                    json.dumps(metrics, ensure_ascii=False) if metrics else None,
                    1 if is_builtin else 0,
                    1 if enabled else 0,
                    created_at,
                    now_str,
                ),
            )
        saved = self.get_policy(clean_id)
        assert saved is not None
        return saved

    def delete_policy(self, policy_id: str) -> None:
        clean_id = str(policy_id or "").strip()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT is_builtin FROM portfolio_capital_policies WHERE id = ?",
                (clean_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"未找到组合策略 {clean_id}")
            if bool(row["is_builtin"]):
                raise ValueError("内置基准组合策略不可删除")
            self._connection.execute(
                "DELETE FROM portfolio_capital_policies WHERE id = ?",
                (clean_id,),
            )
