"""Causal five-state crude oil regime router (zero look-ahead, stateless replay).

Classifies global Brent crude oil daily structure strictly from closed history
(`< trade_date`) into five regimes (`上升`, `震荡上行`, `震荡横盘`, `中继下跌`,
`下降`), applies dual-speed EWMA smoothing and asymmetric hysteresis locking,
computes crude-cluster dual-key MA confirmation (`g_crude`), and optionally
overlays a read-only intraday Fast Crash ramp without mutating daily cache.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
import math
from statistics import median
from threading import RLock
from typing import Any, Mapping, Sequence

from .v9_plus_engine import V9MacroDataStore

OIL_REGIMES: tuple[str, ...] = ("上升", "震荡上行", "震荡横盘", "中继下跌", "下降")

CRUDE_CLUSTER_SYMBOLS: frozenset[str] = frozenset({"160723", "161129", "501018"})

FROZEN_TRAINED_WINDOW: str = "20161128~20211231"

FROZEN_FEATURE_KEYS: tuple[str, ...] = (
    "px_ma15",
    "px_ma20",
    "px_ma60",
    "ma5_ma15",
    "ma15_ma60",
    "ma20_slope5",
    "ret3",
    "ret5",
    "ret10",
    "ret20",
    "dd_hi25",
    "plunge25",
    "reb_lo10",
    "pos25",
    "er20",
    "vol20",
)

FROZEN_FEATURE_MEANS: dict[str, float] = {
    "px_ma15": 0.248487,
    "px_ma20": 0.33676,
    "px_ma60": 1.163384,
    "ma5_ma15": 0.145412,
    "ma15_ma60": 0.762798,
    "ma20_slope5": 0.155289,
    "ret3": 0.177766,
    "ret5": 0.292362,
    "ret10": 0.57909,
    "ret20": 1.146027,
    "dd_hi25": -5.670387,
    "plunge25": -9.443973,
    "reb_lo10": 4.367188,
    "pos25": 60.221868,
    "er20": 0.255497,
    "vol20": 2.012057,
}

FROZEN_FEATURE_STDS: dict[str, float] = {
    "px_ma15": 5.310354,
    "px_ma20": 6.222123,
    "px_ma60": 11.532226,
    "ma5_ma15": 3.805571,
    "ma15_ma60": 9.106566,
    "ma20_slope5": 2.756437,
    "ret3": 4.327262,
    "ret5": 5.472465,
    "ret10": 7.788105,
    "ret20": 11.14975,
    "dd_hi25": 7.912679,
    "plunge25": 8.225966,
    "reb_lo10": 5.023402,
    "pos25": 35.289452,
    "er20": 0.170601,
    "vol20": 1.483037,
}

# Frozen L2-regularized Softmax weights fitted on 20161128~20211231 (5 x 21: [1.0] + 16 z-features + 4 interactions)
FROZEN_SOFTMAX_WEIGHTS: tuple[tuple[float, ...], ...] = (
    (
        -0.339425,
        0.214596,
        0.32981,
        0.079058,
        0.25002,
        -0.034092,
        0.199411,
        0.083375,
        0.068387,
        0.171766,
        0.176699,
        0.09316,
        0.047377,
        0.044798,
        0.343294,
        0.221642,
        0.099336,
        -0.094373,
        0.057121,
        -0.060959,
        -0.025163,
    ),
    (
        0.191552,
        0.099072,
        0.076156,
        0.017396,
        0.244818,
        -0.007082,
        0.052341,
        -0.096151,
        -0.007093,
        0.147505,
        0.036467,
        0.147065,
        0.219439,
        -0.121137,
        0.349613,
        -0.105882,
        -0.011255,
        -0.116813,
        0.087454,
        0.013486,
        0.142854,
    ),
    (
        0.665815,
        -0.164603,
        -0.243742,
        -0.074428,
        -0.189665,
        -0.00811,
        -0.103161,
        -0.002092,
        -0.103752,
        -0.137722,
        -0.170681,
        0.02497,
        0.100515,
        -0.059985,
        0.126031,
        -0.183965,
        -0.09363,
        -0.238747,
        0.049161,
        0.060261,
        0.21896,
    ),
    (
        0.027188,
        0.008899,
        -0.001265,
        0.095825,
        -0.19711,
        0.072289,
        -0.12487,
        0.1357,
        0.084916,
        0.015867,
        0.016446,
        -0.175981,
        -0.416699,
        0.389742,
        -0.22108,
        -0.065998,
        0.178734,
        0.590924,
        0.003397,
        -0.110023,
        -0.134352,
    ),
    (
        -0.545129,
        -0.157964,
        -0.160958,
        -0.117851,
        -0.108064,
        -0.023004,
        -0.023721,
        -0.120832,
        -0.042458,
        -0.197416,
        -0.05893,
        -0.089214,
        0.049369,
        -0.253419,
        -0.597859,
        0.134204,
        -0.173185,
        -0.140991,
        -0.197132,
        0.097235,
        -0.202299,
    ),
)

V5_REGIME_TABLE: dict[str, dict[str, Any]] = {
    "上升": {
        "cluster_bull": 0.94,
        "cluster_unconf": 0.62,
        "sig_bull": 2.60,
        "sig_unconf": 2.30,
        "solo_c_coef": 0.38,
        "solo_c_cap": 0.52,
        "rev_t_501018": 0.52,
        "unconf_wt": 0.65,
        "taper_501018": 0.65,
        "mults": {"160723": 1.12, "161129": 0.82, "501018": 0.92, "162411": 1.22},
    },
    "震荡上行": {
        "cluster_bull": 0.96,
        "cluster_unconf": 0.66,
        "sig_bull": 2.75,
        "sig_unconf": 2.45,
        "solo_c_coef": 0.42,
        "solo_c_cap": 0.58,
        "rev_t_501018": 0.50,
        "unconf_wt": 0.68,
        "taper_501018": 0.65,
        "mults": {"160723": 1.14, "161129": 0.86, "501018": 0.96, "162411": 1.25},
    },
    "震荡横盘": {
        "cluster_bull": 0.90,
        "cluster_unconf": 0.55,
        "sig_bull": 2.48,
        "sig_unconf": 2.20,
        "solo_c_coef": 0.33,
        "solo_c_cap": 0.45,
        "rev_t_501018": 0.48,
        "unconf_wt": 0.60,
        "taper_501018": 0.55,
        "mults": {"160723": 1.08, "161129": 0.75, "501018": 0.95, "162411": 1.15},
    },
    "中继下跌": {
        "cluster_bull": 0.90,
        "cluster_unconf": 0.48,
        "sig_bull": 2.50,
        "sig_unconf": 1.98,
        "solo_c_coef": 0.33,
        "solo_c_cap": 0.45,
        "rev_t_501018": 0.46,
        "unconf_wt": 0.52,
        "taper_501018": 0.50,
        "mults": {"160723": 1.08, "161129": 0.64, "501018": 0.94, "162411": 1.08},
    },
    "下降": {
        "cluster_bull": 0.86,
        "cluster_unconf": 0.42,
        "sig_bull": 2.40,
        "sig_unconf": 1.90,
        "solo_c_coef": 0.36,
        "solo_c_cap": 0.48,
        "rev_t_501018": 0.48,
        "unconf_wt": 0.48,
        "taper_501018": 0.58,
        "mults": {"160723": 0.95, "161129": 0.58, "501018": 1.18, "162411": 0.95},
    },
}

_DEGRADED_FALLBACK_PROBS: dict[str, float] = {
    "上升": 10.0,
    "震荡上行": 20.0,
    "震荡横盘": 45.0,
    "中继下跌": 15.0,
    "下降": 10.0,
}

_MIN_OIL_DAYS = 65
_REPLAY_WINDOW_DAYS = 120
_MAX_OIL_STALE_CALENDAR_DAYS = 10
_MAX_HO_LAG_CALENDAR_DAYS = 2
_MAX_XOP_DECAY_LAG_CALENDAR_DAYS = 3
_MAX_LIVE_BRN_AGE_SEC = 600.0


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _clean_yyyymmdd(raw: Any) -> str:
    txt = str(raw or "").replace("-", "").strip()[:8]
    if len(txt) == 8 and txt.isdigit():
        return txt
    return ""


def _calendar_day_gap(later_yyyymmdd: str, earlier_yyyymmdd: str) -> int | None:
    try:
        dt1 = datetime.strptime(later_yyyymmdd, "%Y%m%d")
        dt0 = datetime.strptime(earlier_yyyymmdd, "%Y%m%d")
        return (dt1 - dt0).days
    except ValueError:
        return None


def _parse_causal_daily_series(
    rows: Sequence[Sequence[Any]] | None,
    clean_td: str,
) -> tuple[list[str], list[float]]:
    """Parse `[date, ts, close, ...]` rows strictly filtering `date < clean_td`."""
    if not rows or not clean_td:
        return [], []
    dedup: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < 3:
            continue
        d_str = _clean_yyyymmdd(row[0])
        if not d_str or d_str >= clean_td:
            continue
        try:
            close_px = float(row[2])
        except (TypeError, ValueError):
            continue
        if math.isfinite(close_px) and close_px > 0.0:
            dedup[d_str] = close_px
    if not dedup:
        return [], []
    ordered_dates = sorted(dedup)
    return ordered_dates, [dedup[d] for d in ordered_dates]


def _find_latest_index_on_or_before(sorted_dates: Sequence[str], target_date: str) -> int:
    """Binary search for largest index `j` with `sorted_dates[j] <= target_date`, or -1."""
    lo, hi = 0, len(sorted_dates) - 1
    ans = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if sorted_dates[mid] <= target_date:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def _compute_daily_features(closes: Sequence[float], idx: int) -> tuple[dict[str, float], float, float]:
    """Compute the 16 dimensionless price-structure features at index `idx >= 60`."""
    c = float(closes[idx])
    ma5 = sum(closes[idx - 4 : idx + 1]) / 5.0
    ma15 = sum(closes[idx - 14 : idx + 1]) / 15.0
    ma20 = sum(closes[idx - 19 : idx + 1]) / 20.0
    ma60 = sum(closes[idx - 59 : idx + 1]) / 60.0
    ma20_prev5 = sum(closes[idx - 24 : idx - 4]) / 20.0

    w25 = closes[idx - 24 : idx + 1]
    hi25 = max(w25)
    lo25 = min(w25)
    lo10 = min(closes[idx - 9 : idx + 1])

    drets = [(closes[j] / closes[j - 1] - 1.0) for j in range(idx - 19, idx + 1)]
    path_pct = sum(abs(r) for r in drets)
    er20 = abs(c / closes[idx - 20] - 1.0) / path_pct if path_pct > 1e-12 else 0.0
    drets_pct = [r * 100.0 for r in drets]
    mean_ret = sum(drets_pct) / len(drets_pct)
    vol20 = math.sqrt(sum((r - mean_ret) ** 2 for r in drets_pct) / len(drets_pct))

    feats = {
        "px_ma15": (c / ma15 - 1.0) * 100.0,
        "px_ma20": (c / ma20 - 1.0) * 100.0,
        "px_ma60": (c / ma60 - 1.0) * 100.0,
        "ma5_ma15": (ma5 / ma15 - 1.0) * 100.0,
        "ma15_ma60": (ma15 / ma60 - 1.0) * 100.0,
        "ma20_slope5": (ma20 / ma20_prev5 - 1.0) * 100.0,
        "ret3": (c / closes[idx - 3] - 1.0) * 100.0,
        "ret5": (c / closes[idx - 5] - 1.0) * 100.0,
        "ret10": (c / closes[idx - 10] - 1.0) * 100.0,
        "ret20": (c / closes[idx - 20] - 1.0) * 100.0,
        "dd_hi25": (c / hi25 - 1.0) * 100.0,
        "plunge25": (lo10 / hi25 - 1.0) * 100.0,
        "reb_lo10": (c / lo10 - 1.0) * 100.0,
        "pos25": ((c - lo25) / (hi25 - lo25) * 100.0) if hi25 > lo25 else 50.0,
        "er20": er20,
        "vol20": vol20,
    }
    return feats, ma5, ma15


def _softmax_probs(feats: Mapping[str, float]) -> list[float]:
    """Evaluate frozen 5-class Softmax with 16 standardized features + 4 interactions."""
    z_vec = [
        _clip((float(feats[k]) - FROZEN_FEATURE_MEANS[k]) / max(FROZEN_FEATURE_STDS[k], 1e-9), -4.0, 4.0)
        for k in FROZEN_FEATURE_KEYS
    ]
    # 4 non-linear regime-structure interaction indicators
    ind_relay = 1.0 if (feats["plunge25"] <= -10.0 and feats["reb_lo10"] >= 2.0 and feats["ma5_ma15"] < 0.0) else 0.0
    ind_bull = 1.0 if (feats["ma5_ma15"] > 0.0 and feats["ma15_ma60"] > 0.0) else 0.0
    ind_bear = 1.0 if (feats["ma5_ma15"] < 0.0 and feats["ma15_ma60"] < 0.0) else 0.0
    ind_range = 1.0 if (abs(feats["px_ma20"]) < 3.0 and feats["er20"] < 0.25) else 0.0

    x_vec = [1.0, *z_vec, ind_relay, ind_bull, ind_bear, ind_range]
    logits = [
        sum(FROZEN_SOFTMAX_WEIGHTS[k][j] * x_vec[j] for j in range(len(x_vec)))
        for k in range(len(OIL_REGIMES))
    ]
    max_logit = max(logits)
    exps = [math.exp(v - max_logit) for v in logits]
    denom = sum(exps) or 1.0
    return [e / denom for e in exps]


def _normalize_probs(probs: Sequence[float]) -> list[float]:
    clipped = [max(0.0, float(v)) for v in probs]
    total = sum(clipped)
    if total <= 1e-12:
        return [0.2] * len(OIL_REGIMES)
    return [v / total for v in clipped]


def _format_probs_pct(probs: Sequence[float]) -> dict[str, float]:
    norm = _normalize_probs(probs)
    rounded = [round(v * 100.0, 2) for v in norm]
    diff = round(100.0 - sum(rounded), 2)
    if abs(diff) > 0.0:
        max_idx = max(range(len(rounded)), key=lambda idx: rounded[idx])
        rounded[max_idx] = round(max(0.0, rounded[max_idx] + diff), 2)
    return {OIL_REGIMES[idx]: rounded[idx] for idx in range(len(OIL_REGIMES))}


class OilRegimeRouter:
    """Thread-safe causal five-state crude oil regime router with stateless replay."""

    def __init__(self, *, max_cache_entries: int = 32) -> None:
        self._lock = RLock()
        self._max_cache_entries = max(4, int(max_cache_entries))
        self._base_cache: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()

    def clear_cache(self) -> None:
        with self._lock:
            self._base_cache.clear()

    @staticmethod
    def _load_macro_series(
        research_store: Any,
        clean_td: str,
    ) -> tuple[list[str], list[float], list[str], list[float], list[str], list[float]]:
        if research_store is None or not clean_td:
            return [], [], [], [], [], []
        macro = V9MacroDataStore(research_store, allow_network=False)
        try:
            oil_rows = macro.load_oil_daily_rows()
        except Exception:
            oil_rows = []
        try:
            ho_rows = macro.load_ho_daily_rows()
        except Exception:
            ho_rows = []
        try:
            xop_rows = macro.load_xop_daily_rows()
        except Exception:
            xop_rows = []
        oil_d, oil_c = _parse_causal_daily_series(oil_rows, clean_td)
        ho_d, ho_c = _parse_causal_daily_series(ho_rows, clean_td)
        xop_d, xop_c = _parse_causal_daily_series(xop_rows, clean_td)
        return oil_d, oil_c, ho_d, ho_c, xop_d, xop_c

    def peek_reference_dates(
        self,
        research_store: Any,
        *,
        trade_date: str,
    ) -> tuple[str | None, str | None, str | None]:
        """Return latest causal (`< trade_date`) dates for `(oil, ho, xop)` for cache keying."""
        clean_td = _clean_yyyymmdd(trade_date)
        oil_d, _, ho_d, _, xop_d, _ = self._load_macro_series(research_store, clean_td)
        return (
            oil_d[-1] if oil_d else None,
            ho_d[-1] if ho_d else None,
            xop_d[-1] if xop_d else None,
        )

    @staticmethod
    def _build_degraded_result(
        *,
        enforced: bool,
        profile: str,
        oil_reference_date: str | None = None,
        oil_close_t1: float | None = None,
        degraded_mode: str = "oil_daily_unavailable",
        degraded_sources: Sequence[str] = ("oil_daily",),
    ) -> dict[str, Any]:
        neutral_tbl = V5_REGIME_TABLE["震荡横盘"]
        return {
            "enforced": bool(enforced),
            "profile": profile,
            "oil_reference_date": oil_reference_date,
            "oil_close_t1": round(float(oil_close_t1), 4) if oil_close_t1 is not None else None,
            "locked_regime": "震荡横盘",
            "effective_regime": "震荡横盘",
            "probs": dict(_DEGRADED_FALLBACK_PROBS),
            "ma_confirm_ratio": 0.0,
            "effective_cluster_cap_pct": 55.0,
            "effective_sigma_cap_pct": 2.40,
            "solo_crude_coef": float(neutral_tbl["solo_c_coef"]),
            "solo_crude_cap": float(neutral_tbl["solo_c_cap"]),
            "symbol_multipliers": {str(k): float(v) for k, v in neutral_tbl["mults"].items()},
            "symbol_tuning_hints": {
                "rev_t_501018": float(neutral_tbl["rev_t_501018"]),
                "unconf_wt": float(neutral_tbl["unconf_wt"]),
                "taper_501018": float(neutral_tbl["taper_501018"]),
            },
            "fast_crash_active": False,
            "degraded_mode": degraded_mode,
            "degraded_sources": list(dict.fromkeys(str(s) for s in degraded_sources if str(s))),
        }

    @staticmethod
    def _replay_daily_base(
        *,
        oil_dates: Sequence[str],
        oil_closes: Sequence[float],
        ho_dates: Sequence[str],
        ho_closes: Sequence[float],
        xop_dates: Sequence[str],
        xop_closes: Sequence[float],
    ) -> dict[str, Any]:
        """Run deterministic 120-day stateless replay ending at `oil_dates[-1]`."""
        n_oil = len(oil_closes)
        start_idx = max(60, n_oil - _REPLAY_WINDOW_DAYS)

        sm_p: list[float] | None = None
        locked_idx = 2  # Start from neutral "震荡横盘"
        last_feats: dict[str, float] = {}
        last_ma5 = float(oil_closes[-1])
        last_ma15 = float(oil_closes[-1])
        last_crack3 = 0.0
        last_degraded: list[str] = []

        for idx in range(start_idx, n_oil):
            cur_date = oil_dates[idx]
            feats, ma5, ma15 = _compute_daily_features(oil_closes, idx)
            p = _softmax_probs(feats)
            step_degraded: list[str] = []
            crack3 = 0.0

            # Tier 1 auxiliary source 1: Heating Oil crack spread (lag <= 2 calendar days)
            ho_j = _find_latest_index_on_or_before(ho_dates, cur_date)
            ho_ok = False
            if ho_j >= 3:
                ho_gap = _calendar_day_gap(cur_date, ho_dates[ho_j])
                if ho_gap is not None and 0 <= ho_gap <= _MAX_HO_LAG_CALENDAR_DAYS:
                    ho_ret3 = (ho_closes[ho_j] / ho_closes[ho_j - 3] - 1.0) * 100.0
                    crack3 = ho_ret3 - feats["ret3"]
                    ho_ok = True
                    if crack3 < -2.0:
                        p[0] *= 0.82
                        p[3] *= 1.18
                        p = _normalize_probs(p)
                    elif crack3 > 2.0:
                        p[1] *= 1.15
                        p[3] *= 0.85
                        p = _normalize_probs(p)
            if not ho_ok:
                step_degraded.append("ho_daily")

            # Tier 1 auxiliary source 2: XOP 5-day relative strength (1..3d lag -> 0.5x decay, >4d or missing -> 0)
            xop_j = _find_latest_index_on_or_before(xop_dates, cur_date)
            xop_decay = 0.0
            if xop_j >= 5:
                xop_gap = _calendar_day_gap(cur_date, xop_dates[xop_j])
                if xop_gap is not None:
                    if xop_gap == 0:
                        xop_decay = 1.0
                    elif 1 <= xop_gap <= _MAX_XOP_DECAY_LAG_CALENDAR_DAYS:
                        xop_decay = 0.5
                    elif xop_gap <= 4:
                        xop_decay = 0.5
                    else:
                        step_degraded.append("xop_daily")
                else:
                    step_degraded.append("xop_daily")
            else:
                step_degraded.append("xop_daily")

            if xop_decay > 0.0 and xop_j >= 5:
                xop_ret5 = (xop_closes[xop_j] / xop_closes[xop_j - 5] - 1.0) * 100.0
                xop_rel5 = (xop_ret5 - feats["ret5"]) * xop_decay
                if xop_rel5 > 3.0:
                    p[1] *= 1.0 + 0.10 * xop_decay
                    p[4] *= max(0.5, 1.0 - 0.10 * xop_decay)
                    p = _normalize_probs(p)
                elif xop_rel5 < -3.0:
                    p[0] *= max(0.5, 1.0 - 0.10 * xop_decay)
                    p[3] *= 1.0 + 0.10 * xop_decay
                    p = _normalize_probs(p)

            # Dual-speed EWMA posterior smoothing
            alpha = 0.90 if abs(feats["ret3"]) >= 4.0 else 0.65
            if sm_p is None:
                sm_p = list(p)
            else:
                sm_p = _normalize_probs([alpha * p[k] + (1.0 - alpha) * sm_p[k] for k in range(len(OIL_REGIMES))])

            # Asymmetric hysteresis state lock
            best_idx = max(range(len(OIL_REGIMES)), key=lambda k: sm_p[k])
            if best_idx != locked_idx:
                if best_idx in (3, 4) and locked_idx not in (3, 4):
                    threshold = 0.03
                elif locked_idx in (3, 4) and best_idx not in (3, 4):
                    threshold = 0.07
                else:
                    threshold = 0.05
                if (sm_p[best_idx] - sm_p[locked_idx]) >= threshold:
                    locked_idx = best_idx

            if idx == n_oil - 1:
                last_feats = feats
                last_ma5 = ma5
                last_ma15 = ma15
                last_crack3 = crack3
                last_degraded = step_degraded

        assert sm_p is not None
        return {
            "base_probs": tuple(sm_p),
            "locked_idx": locked_idx,
            "oil_reference_date": str(oil_dates[-1]),
            "oil_close_t1": float(oil_closes[-1]),
            "oil_ma5": float(last_ma5),
            "oil_ma15": float(last_ma15),
            "last_feats": dict(last_feats),
            "last_crack3": float(last_crack3),
            "degraded_sources": tuple(last_degraded),
        }

    def evaluate_regime(
        self,
        research_store: Any,
        *,
        trade_date: str,
        profile: str = "oil_v5_dual_key",
        symbol_ma_map: Mapping[str, tuple[float, float]] | None = None,
        live_brn_price: float | None = None,
        live_brn_age_sec: float | None = None,
        enforced: bool = False,
    ) -> dict[str, Any]:
        """Evaluate causal oil regime, dual-key parameters, and optional read-only Fast Crash overlay."""
        clean_td = _clean_yyyymmdd(trade_date)
        clean_profile = str(profile or "oil_v5_dual_key").strip()[:64] or "oil_v5_dual_key"

        if research_store is None or not clean_td:
            return self._build_degraded_result(enforced=enforced, profile=clean_profile)

        oil_dates, oil_closes, ho_dates, ho_closes, xop_dates, xop_closes = self._load_macro_series(
            research_store, clean_td
        )
        oil_last_date = oil_dates[-1] if oil_dates else None
        oil_close_t1 = oil_closes[-1] if oil_closes else None
        ho_last_date = ho_dates[-1] if ho_dates else None
        xop_last_date = xop_dates[-1] if xop_dates else None

        # Tier 2: primary oil_daily missing, too short (< 65), or stale (> 10 calendar days)
        stale_gap = _calendar_day_gap(clean_td, oil_last_date) if oil_last_date else None
        if (
            len(oil_closes) < _MIN_OIL_DAYS
            or oil_last_date is None
            or stale_gap is None
            or stale_gap > _MAX_OIL_STALE_CALENDAR_DAYS
        ):
            return self._build_degraded_result(
                enforced=enforced,
                profile=clean_profile,
                oil_reference_date=oil_last_date,
                oil_close_t1=oil_close_t1,
                degraded_mode="oil_daily_unavailable",
                degraded_sources=("oil_daily",),
            )

        cache_key = (clean_td, oil_last_date, ho_last_date, xop_last_date, clean_profile)
        with self._lock:
            base = self._base_cache.get(cache_key)
            if base is not None:
                self._base_cache.move_to_end(cache_key)

        if base is None:
            base = self._replay_daily_base(
                oil_dates=oil_dates,
                oil_closes=oil_closes,
                ho_dates=ho_dates,
                ho_closes=ho_closes,
                xop_dates=xop_dates,
                xop_closes=xop_closes,
            )
            with self._lock:
                self._base_cache[cache_key] = base
                self._base_cache.move_to_end(cache_key)
                while len(self._base_cache) > self._max_cache_entries:
                    self._base_cache.popitem(last=False)

        # Read-only copy from cached daily base state (never mutates `base`)
        base_probs = list(base["base_probs"])
        locked_idx = int(base["locked_idx"])
        locked_regime = OIL_REGIMES[locked_idx]
        effective_regime = locked_regime
        feats = base["last_feats"]
        oil_ma5 = float(base["oil_ma5"])
        oil_ma15 = float(base["oil_ma15"])
        ref_close_t1 = float(base["oil_close_t1"])
        crack3 = float(base["last_crack3"])
        degraded_sources = list(base["degraded_sources"])

        # Dual-key continuous MA confirmation `g_crude` (crude cluster only; explicitly ignores 162411)
        oil_ratio = (oil_ma5 / oil_ma15) if oil_ma15 > 0.0 else 1.0
        crude_ratios: list[float] = []
        if isinstance(symbol_ma_map, Mapping):
            for sym, pair in symbol_ma_map.items():
                clean_sym = str(sym or "").strip().upper()
                if clean_sym not in CRUDE_CLUSTER_SYMBOLS:
                    continue
                if isinstance(pair, Sequence) and not isinstance(pair, (str, bytes)) and len(pair) >= 2:
                    try:
                        s_ma5 = float(pair[0])
                        s_ma15 = float(pair[1])
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(s_ma5) and math.isfinite(s_ma15) and s_ma5 > 0.0 and s_ma15 > 0.0:
                        crude_ratios.append(s_ma5 / s_ma15)
        eff_ratio = min(oil_ratio, float(median(crude_ratios))) if crude_ratios else oil_ratio
        g_crude = _clip((eff_ratio - 0.998) / 0.012, 0.0, 1.0)

        # Golden-cross conditional posterior reweighting for parameter interpolation
        interp_p = list(base_probs)
        if g_crude > 0.0 and float(feats.get("ret5", 0.0)) > 0.0:
            cut = interp_p[3] * 0.70 * g_crude
            interp_p[3] = max(0.0, interp_p[3] - cut)
            up_sum = interp_p[0] + interp_p[1] + interp_p[2]
            if up_sum > 1e-12:
                for k in (0, 1, 2):
                    interp_p[k] += cut * (interp_p[k] / up_sum)
            else:
                for k in (0, 1, 2):
                    interp_p[k] += cut / 3.0
            interp_p = _normalize_probs(interp_p)

        # Convex combination across V5_REGIME_TABLE
        cluster_bull = sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["cluster_bull"]) for k in range(5))
        cluster_unconf = sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["cluster_unconf"]) for k in range(5))
        sig_bull = sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["sig_bull"]) for k in range(5))
        sig_unconf = sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["sig_unconf"]) for k in range(5))
        solo_c_coef = sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["solo_c_coef"]) for k in range(5))
        solo_c_cap = sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["solo_c_cap"]) for k in range(5))
        rev_t_501018 = sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["rev_t_501018"]) for k in range(5))
        unconf_wt = sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["unconf_wt"]) for k in range(5))
        taper_501018 = sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["taper_501018"]) for k in range(5))

        mult_syms = tuple(V5_REGIME_TABLE["震荡横盘"]["mults"].keys())
        symbol_multipliers = {
            sym: round(
                sum(interp_p[k] * float(V5_REGIME_TABLE[OIL_REGIMES[k]]["mults"][sym]) for k in range(5)),
                4,
            )
            for sym in mult_syms
        }

        cluster_eff = (1.0 - g_crude) * cluster_unconf + g_crude * cluster_bull
        sigma_eff = (1.0 - g_crude) * sig_unconf + g_crude * sig_bull

        # Overbought momentum-blunting taper
        pos25 = float(feats.get("pos25", 50.0))
        ret10 = float(feats.get("ret10", 0.0))
        ret3 = float(feats.get("ret3", 0.0))
        vol20 = float(feats.get("vol20", 2.0))
        if pos25 >= 95.0 and ret10 >= 11.0 and (ret3 <= 1.0 or vol20 >= 3.2 or crack3 <= -2.0):
            sigma_eff = min(sigma_eff, 2.25)
            cluster_eff = min(cluster_eff, 0.82)

        # Tier 3: Read-only intraday Fast Crash ramp overlay (never mutates cached daily state)
        display_p = list(base_probs)
        fast_crash_active = False
        if live_brn_price is not None:
            try:
                live_px = float(live_brn_price)
            except (TypeError, ValueError):
                live_px = 0.0
            age_ok = True
            if live_brn_age_sec is not None:
                try:
                    age_ok = float(live_brn_age_sec) <= _MAX_LIVE_BRN_AGE_SEC
                except (TypeError, ValueError):
                    age_ok = False
            if not age_ok:
                if "live_brn" not in degraded_sources:
                    degraded_sources.append("live_brn")
            elif math.isfinite(live_px) and live_px > 0.0 and ref_close_t1 > 0.0:
                brn_intra_pct = _clip((live_px / ref_close_t1 - 1.0) * 100.0, -20.0, 20.0)
                if brn_intra_pct <= -2.5:
                    w_crash = _clip((-brn_intra_pct - 2.5) / 2.0, 0.0, 1.0) * 0.55
                    if w_crash > 0.0:
                        display_p = _normalize_probs(
                            [
                                (1.0 - w_crash) * display_p[k] + (w_crash if k == 4 else 0.0)
                                for k in range(len(OIL_REGIMES))
                            ]
                        )
                        down_tbl = V5_REGIME_TABLE["下降"]
                        cluster_eff = min(
                            cluster_eff,
                            (1.0 - w_crash) * cluster_eff + w_crash * float(down_tbl["cluster_unconf"]),
                        )
                        sigma_eff = min(
                            sigma_eff,
                            (1.0 - w_crash) * sigma_eff + w_crash * float(down_tbl["sig_unconf"]),
                        )
                        solo_c_coef = (1.0 - w_crash) * solo_c_coef + w_crash * float(down_tbl["solo_c_coef"])
                        solo_c_cap = (1.0 - w_crash) * solo_c_cap + w_crash * float(down_tbl["solo_c_cap"])
                        if w_crash >= 0.35:
                            effective_regime = "下降"
                            fast_crash_active = True

        degraded_mode: str | None = None
        if degraded_sources:
            degraded_mode = "auxiliary_degraded"

        return {
            "enforced": bool(enforced),
            "profile": clean_profile,
            "oil_reference_date": str(base["oil_reference_date"]),
            "oil_close_t1": round(ref_close_t1, 4),
            "locked_regime": locked_regime,
            "effective_regime": effective_regime,
            "probs": _format_probs_pct(display_p),
            "ma_confirm_ratio": round(g_crude, 4),
            "effective_cluster_cap_pct": round(cluster_eff * 100.0, 2),
            "effective_sigma_cap_pct": round(sigma_eff, 4),
            "solo_crude_coef": round(solo_c_coef, 4),
            "solo_crude_cap": round(solo_c_cap, 4),
            "symbol_multipliers": symbol_multipliers,
            "symbol_tuning_hints": {
                "rev_t_501018": round(rev_t_501018, 4),
                "unconf_wt": round(unconf_wt, 4),
                "taper_501018": round(taper_501018, 4),
            },
            "fast_crash_active": fast_crash_active,
            "degraded_mode": degraded_mode,
            "degraded_sources": list(dict.fromkeys(degraded_sources)),
        }
