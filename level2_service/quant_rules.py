"""Quantitative rule library, multi-day history backfill, and AI evaluation engine."""

from __future__ import annotations

from bisect import bisect_right
import copy
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import socket
import sqlite3
import statistics
import struct
from threading import RLock
from typing import Any, Callable, Mapping, Sequence
import urllib.request
import uuid
import zlib
from zoneinfo import ZoneInfo

from .daily_kline import DailyKlineSourceError, TonghuashunPublicDailyKlineProvider
from .market_data import KlineBar
from .parsed_values import is_exchange_fund_symbol
from .quant_data_sources import (
    QUANT_DATA_SOURCE_CATALOG,
    derive_rule_data_sources,
    normalize_and_validate_rule_data_sources,
)
from .research_premium import PREMIUM_NAMES
from .research_replay import (
    EXTENDED_QUANT_CONDITION_TYPES,
    VALID_QUANT_CONDITION_TYPES,
    _number,
    _trading_minute,
    build_replay_markers,
    calculate_macd,
    calculate_replay_performance,
    detect_rule_required_packs,
    evaluate_rule_detail,
    rule_uses_flow,
    validate_dsl_condition,
)
from .v9_fof_crude import (
    load_dbo_daily_rows,
    load_merged_fx_cnh_series,
)
from .v9_plus_engine import (
    _MEMORY_MACRO_CACHE,
    _causal_daily_ov_and_nd,
    _parse_sh,
    _val_at,
    V9MacroDataStore,
    build_v9_plus_days,
    evaluate_v9_plus_backtest,
)


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_DATE_RE = re.compile(r"^[0-9]{8}\Z")
_SYMBOL_RE = re.compile(r"^[0-9]{6}\Z")
_QUANT_SYMBOL_RE = re.compile(r"^(?:[0-9]{6}|BRN0Y)\Z")
_TIME_HHMM_RE = re.compile(r"^[0-2][0-9]:[0-5][0-9]\Z")
_TDX_HOSTS = (
    ("124.71.187.122", 7709),
    ("115.238.56.198", 7709),
    ("180.153.18.170", 7709),
)
_HORIZONS = (1, 3, 5, 10, 15, 30)

VALID_SIZING_MODES = ("base_t0_close", "cash_pool_rolling")
VALID_ENGINE_PROFILES = ("standard", "v9_plus_aggressive", "v9_plus_balanced")
VALID_CROSS_MARKET_ANCHORS = ("brent", "xop_us_ep", "uso_bno_sc_peer", "fof_crude_core", "fof_t0_multisource", "sp_oilgas_etf")
VALID_QUANT_DATA_PACKS = (
    "iopv_fx",
    "xop_us_ep",
    "uso_bno_sc_peer",
    "l2_darkpool_flow",
    "etf_redemption_flow",
)
MONITOR_SOURCE_TO_PACK: dict[str, str | None] = {
    "ashare_intraday_1m": None,
    "ashare_orderbook_l1": None,
    "ice_brent_1m": None,
    "ashare_l2_fund_flow": "l2_darkpool_flow",
    "fx_usdcny_usdcnh": "iopv_fx",
    "eastmoney_fund_nav": "iopv_fx",
    "qdii_premium_panel_feed": "iopv_fx",
    "us_ep_xop_crack_macro": "xop_us_ep",
    "cme_energy_macro_futures": "xop_us_ep",
    "us_oil_etf_uso_bno_dbo": "uso_bno_sc_peer",
    "ine_sc0_futures": "uso_bno_sc_peer",
    "ashare_peer_lof_quote": "uso_bno_sc_peer",
    "ashare_etf_redemption": "etf_redemption_flow",
}
_OIL_PEER_SYMBOLS = ("160723", "161129", "501018", "162411")
_OIL_QDII_NAV_SYMBOLS = frozenset(
    {"160723", "161129", "501018", "162411", "160416", "162719", "163208", "159518", "513350"}
)
_T0_TURNAROUND_EXACT = frozenset(
    {"160723", "501018", "160416", "161129", "162411", "162719", "163208", "160216"}
)
_T0_TURNAROUND_PREFIXES = ("513", "5188", "1599", "1595")


def is_t0_turnaround_symbol(symbol: str) -> bool:
    """Return True for QDII/commodity/cross-border funds allowing T+0 same-day buy->sell."""
    sym = str(symbol or "").strip().upper()
    return sym in _T0_TURNAROUND_EXACT or sym.startswith(_T0_TURNAROUND_PREFIXES)


DEFAULT_EVAL_CRITERIA: dict[str, Any] = {
    "start_date": "20260701",
    "data_scope": "auto",
    "ranking_metric": "t0_net_return_pct",
    "morning_start": "09:30",
    "morning_end": "11:30",
    "max_daily_trades": 1,
    "round_trip_fee_pct": 0.02,
    "capital_cap": 20000.0,
    "primary_horizon_minutes": 5,
    "min_move_pct": 0.0,
}

DEFAULT_POSITION_SIZING: dict[str, Any] = {
    "mode": "base_t0_close",
    "engine_profile": "standard",
    "default_weight_pct": 50.0,
    "max_position_pct": 100.0,
    "max_daily_entries": 2,
    "cooldown_minutes": 4,
    "scale_factor": 1.0,
    "lot_size": 100,
    "close_flatten_time": "14:53",
    "allow_same_day_turnaround": "auto",
    "conviction_tiers": [],
    "sell_conviction_tiers": [],
}

DEFAULT_MACD_SETTINGS: dict[str, int | float] = {
    "short": 10,
    "long": 20,
    "signal": 5,
    "marker_threshold": 0.00001,
}


def _normalize_quant_symbol(symbol: str) -> str:
    normalized = str(symbol or "").strip().upper()
    if not _QUANT_SYMBOL_RE.fullmatch(normalized):
        raise ValueError("symbol must be a 6-digit code or BRN0Y")
    return normalized

BUILTIN_QUANT_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "preset-macd-flow-baseline",
        "name": "现行MACD(10,20,5)+大单共振",
        "description": "当前实盘与盯盘默认规则：MACD(10,20,5) 红/绿交叉配合 1 分钟大单净额变化 ≥3%。",
        "source": "builtin",
        "symbols": ["160723"],
        "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.00001},
        "buy_rule": {
            "id": "preset-macd-flow-buy",
            "enabled": True,
            "side": "buy",
            "title": "MACD大单买入",
            "joiner": "and",
            "conditions": [
                {"id": "c-macd", "type": "macd_signal", "signal": "red"},
                {
                    "id": "c-flow",
                    "type": "flow_change_pct",
                    "flow": "big",
                    "minutes": 1,
                    "direction": "increase",
                    "value": 3.0,
                },
            ],
        },
        "sell_rule": {
            "id": "preset-macd-flow-sell",
            "enabled": True,
            "side": "sell",
            "title": "MACD大单卖出",
            "joiner": "and",
            "conditions": [
                {"id": "c-macd", "type": "macd_signal", "signal": "green"},
                {
                    "id": "c-flow",
                    "type": "flow_change_pct",
                    "flow": "big",
                    "minutes": 1,
                    "direction": "decrease",
                    "value": 3.0,
                },
            ],
        },
        "eval_criteria": dict(DEFAULT_EVAL_CRITERIA),
        "enabled": True,
    },
    {
        "id": "preset-macd-pure-baseline",
        "name": "纯MACD(10,20,5)全量基准",
        "description": "不依赖分钟大单流向，默认自 2026-07-01 起按早盘底仓做T与全窗口胜率检验基础 MACD(10,20,5) 金叉买/死叉卖（仍支持显式传入 20260301 全区间测算）。",
        "source": "builtin",
        "symbols": ["160723"],
        "macd_settings": {"short": 10, "long": 20, "signal": 5, "marker_threshold": 0.00001},
        "buy_rule": {
            "id": "preset-macd-pure-buy",
            "enabled": True,
            "side": "buy",
            "title": "MACD金叉买入",
            "joiner": "and",
            "conditions": [{"id": "c-macd", "type": "macd_signal", "signal": "red"}],
        },
        "sell_rule": {
            "id": "preset-macd-pure-sell",
            "enabled": True,
            "side": "sell",
            "title": "MACD死叉卖出",
            "joiner": "and",
            "conditions": [{"id": "c-macd", "type": "macd_signal", "signal": "green"}],
        },
        "eval_criteria": dict(DEFAULT_EVAL_CRITERIA),
        "enabled": True,
    },
)


def _now_iso() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")


def _direct_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_get_direct(url: str, *, referer: str, timeout: float = 8.0) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Referer": referer,
        },
    )
    with _direct_opener().open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _normalize_macd_settings(raw: Mapping[str, Any] | None) -> dict[str, int | float]:
    src = raw if isinstance(raw, Mapping) else {}
    short = max(2, min(200, int(src.get("short") or DEFAULT_MACD_SETTINGS["short"])))
    long = max(short + 1, min(200, int(src.get("long") or DEFAULT_MACD_SETTINGS["long"])))
    signal = max(1, min(100, int(src.get("signal") or DEFAULT_MACD_SETTINGS["signal"])))
    thr = float(
        src.get("marker_threshold")
        if src.get("marker_threshold") is not None
        else DEFAULT_MACD_SETTINGS["marker_threshold"]
    )
    thr = max(0.0, min(1.0, thr))
    return {
        "short": short,
        "long": long,
        "signal": signal,
        "marker_threshold": thr,
    }


def _normalize_hhmm(raw: object, default: str) -> str:
    text = str(raw or "").strip()
    return text if _TIME_HHMM_RE.fullmatch(text) else default


def _normalize_eval_criteria(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    src = raw if isinstance(raw, Mapping) else {}
    horizon = int(src.get("primary_horizon_minutes") or 5)
    if horizon not in _HORIZONS:
        horizon = 5
    min_move = max(0.0, min(5.0, float(src.get("min_move_pct") or 0.0)))
    start_date = str(src.get("start_date") or "20260701").replace("-", "").strip()
    if not _DATE_RE.fullmatch(start_date):
        start_date = "20260701"
    scope = str(src.get("data_scope") or "auto").strip().lower()
    if scope not in {"auto", "all", "flow_only"}:
        scope = "auto"
    m_start = _normalize_hhmm(src.get("morning_start"), "09:30")
    m_end = _normalize_hhmm(src.get("morning_end"), "11:30")
    if m_end < m_start:
        m_start, m_end = m_end, m_start
    try:
        max_trades = max(1, min(20, int(src.get("max_daily_trades") or 1)))
    except (TypeError, ValueError):
        max_trades = 1
    try:
        fee_raw = src.get("round_trip_fee_pct")
        fee_pct = 0.02 if fee_raw is None else max(0.0, min(1.0, float(fee_raw)))
    except (TypeError, ValueError):
        fee_pct = 0.02
    try:
        cap_raw = src.get("capital_cap")
        capital_cap = 20000.0 if cap_raw is None else max(1000.0, min(10_000_000.0, float(cap_raw)))
    except (TypeError, ValueError):
        capital_cap = 20000.0
    ranking_metric = str(src.get("ranking_metric") or "t0_net_return_pct").strip() or "t0_net_return_pct"
    return {
        "start_date": start_date,
        "data_scope": scope,
        "ranking_metric": ranking_metric,
        "morning_start": m_start,
        "morning_end": m_end,
        "max_daily_trades": max_trades,
        "round_trip_fee_pct": round(fee_pct, 4),
        "capital_cap": round(capital_cap, 2),
        "primary_horizon_minutes": horizon,
        "min_move_pct": round(min_move, 4),
    }


def compute_lot_shares_from_budget(budget_yuan: float, exec_price: float, lot_size: int = 100) -> int:
    """Convert a target yuan budget into a whole A-share board lot quantity (multiples of 100)."""
    if budget_yuan <= 0 or exec_price <= 0 or lot_size <= 0:
        return 0
    lots = int(math.floor((float(budget_yuan) + 1e-6) / (float(exec_price) * int(lot_size))))
    return max(0, lots * int(lot_size))


def _normalize_sizing_tiers(raw: object, *, side_prefix: str) -> list[dict[str, Any]]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    tiers: list[dict[str, Any]] = []
    for idx, item in enumerate(raw[:10]):
        if not isinstance(item, Mapping):
            continue
        conds = item.get("conditions")
        if not isinstance(conds, Sequence) or isinstance(conds, (str, bytes)) or not conds:
            continue
        cleaned_conds: list[dict[str, Any]] = []
        valid = True
        for c_idx, cond in enumerate(conds):
            if not isinstance(cond, Mapping) or not str(cond.get("type") or "").strip():
                valid = False
                break
            c_copy = copy.deepcopy(dict(cond))
            c_copy.setdefault("id", f"{side_prefix}-tier-{idx + 1}-c{c_idx + 1}")
            cleaned_conds.append(c_copy)
        if not valid or not cleaned_conds:
            continue
        try:
            weight = float(item.get("weight_pct") or 50.0)
        except (TypeError, ValueError):
            weight = 50.0
        weight = round(max(5.0, min(100.0, weight)), 2)
        joiner = "or" if str(item.get("joiner") or "and").lower() == "or" else "and"
        label = str(item.get("label") or f"分档仓位{idx + 1}({weight:.0f}%)").strip()[:48]
        tiers.append(
            {
                "label": label,
                "weight_pct": weight,
                "joiner": joiner,
                "conditions": cleaned_conds,
            }
        )
    return tiers


def _as_bool_flag(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes"}
    return False


def _normalize_v9_tuning(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = {}
    float_specs: tuple[tuple[str, float, float, int, str], ...] = (
        ("bear_high_prem_flat_pct", 0.5, 30.0, 2, "any"),
        ("bull_oil_surge_tier_cap_pct", 20.0, 100.0, 1, "any"),
        ("macd_price_anchor", 0.5, 20.0, 3, "positive"),
        ("buy_prem_offset_pct", -2.0, 3.0, 3, "nonzero"),
        ("bubble_prem_cap_pct", 5.0, 50.0, 2, "positive"),
        ("bubble_overnight_weight_pct", 0.0, 100.0, 2, "any"),
        ("bull_pullback_weight_pct", 0.0, 100.0, 2, "any"),
        ("plunge_dd15_pct", -35.0, -4.0, 2, "any"),
        ("high_oil_bull_cap_pct", 72.0, 95.0, 1, "any"),
    )
    for key, lo, hi, digits, mode in float_specs:
        raw_val = raw.get(key)
        if raw_val is None or raw_val == "" or isinstance(raw_val, bool):
            continue
        try:
            fval = float(raw_val)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(fval):
            continue
        if mode == "positive" and fval <= 0.0:
            continue
        if mode == "nonzero" and abs(fval) <= 1e-9:
            continue
        out[key] = round(max(lo, min(hi, fval)), digits)
    for bool_key in ("bull_oil_surge_guard", "scalp_strict_mode", "strict_dip_scalp", "multi_oil_plunge_mode"):
        if _as_bool_flag(raw.get(bool_key)):
            out[bool_key] = True
    raw_peer = str(raw.get("peer_symbol") or "").strip()
    if _SYMBOL_RE.fullmatch(raw_peer):
        out["peer_symbol"] = raw_peer
    return out


def normalize_position_sizing(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate and normalize v3 autonomous position-sizing configuration."""
    src = raw if isinstance(raw, Mapping) else {}
    mode = str(src.get("mode") or DEFAULT_POSITION_SIZING["mode"]).strip().lower()
    if mode not in VALID_SIZING_MODES:
        mode = "base_t0_close"
    try:
        default_w = float(src.get("default_weight_pct") if src.get("default_weight_pct") is not None else 50.0)
    except (TypeError, ValueError):
        default_w = 50.0
    default_w = round(max(5.0, min(100.0, default_w)), 2)
    try:
        max_w = float(src.get("max_position_pct") if src.get("max_position_pct") is not None else 100.0)
    except (TypeError, ValueError):
        max_w = 100.0
    max_w = round(max(10.0, min(100.0, max_w)), 2)
    try:
        max_entries = int(src.get("max_daily_entries") if src.get("max_daily_entries") is not None else 2)
    except (TypeError, ValueError):
        max_entries = 2
    max_entries = max(1, min(20, max_entries))
    flatten_time = _normalize_hhmm(src.get("close_flatten_time"), "14:53")
    if flatten_time < "14:40" or flatten_time > "14:55":
        flatten_time = "14:53"
    turnaround_raw = src.get("allow_same_day_turnaround", "auto")
    if isinstance(turnaround_raw, bool):
        turnaround: Any = turnaround_raw
    elif str(turnaround_raw).strip().lower() in {"true", "1", "yes"}:
        turnaround = True
    elif str(turnaround_raw).strip().lower() in {"false", "0", "no"}:
        turnaround = False
    else:
        turnaround = "auto"

    engine_profile = str(src.get("engine_profile") or "standard").strip().lower()
    if engine_profile not in VALID_ENGINE_PROFILES:
        engine_profile = "standard"
    try:
        cooldown_m = int(src.get("cooldown_minutes") if src.get("cooldown_minutes") is not None else 4)
    except (TypeError, ValueError):
        cooldown_m = 4
    cooldown_m = max(1, min(60, cooldown_m))
    try:
        scale_f = float(src.get("scale_factor") if src.get("scale_factor") is not None else 1.0)
    except (TypeError, ValueError):
        scale_f = 1.0
    scale_f = round(max(0.5, min(1.5, scale_f)), 3)

    out: dict[str, Any] = {
        "mode": mode,
        "engine_profile": engine_profile,
        "default_weight_pct": default_w,
        "max_position_pct": max_w,
        "max_daily_entries": max_entries,
        "cooldown_minutes": cooldown_m,
        "scale_factor": scale_f,
        "lot_size": 100,
        "close_flatten_time": flatten_time,
        "allow_same_day_turnaround": turnaround,
        "conviction_tiers": _normalize_sizing_tiers(src.get("conviction_tiers"), side_prefix="buy"),
        "sell_conviction_tiers": _normalize_sizing_tiers(src.get("sell_conviction_tiers"), side_prefix="sell"),
    }
    v9_tuning = _normalize_v9_tuning(src.get("v9_tuning"))
    if v9_tuning:
        out["v9_tuning"] = v9_tuning
    raw_anchor = str(src.get("cross_market_anchor") or "").strip().lower()
    if raw_anchor in VALID_CROSS_MARKET_ANCHORS and raw_anchor != "brent":
        out["cross_market_anchor"] = raw_anchor
    raw_min_hold = src.get("min_retained_shares")
    if raw_min_hold is not None and raw_min_hold != "" and not isinstance(raw_min_hold, bool):
        try:
            out["min_retained_shares"] = max(0, min(100000, int(raw_min_hold)))
        except (TypeError, ValueError):
            pass
    return out


def resolve_signal_conviction_tier(
    symbol: str,
    points: Sequence[Mapping[str, object]],
    index: int,
    side: str,
    position_sizing: Mapping[str, Any] | None,
    *,
    marker_threshold: float = 0.0,
) -> tuple[float, str]:
    """Resolve model conviction weight (%) and tier label from real-time bar features."""
    ps = normalize_position_sizing(position_sizing)
    tiers = ps["sell_conviction_tiers"] if side == "sell" and ps["sell_conviction_tiers"] else ps["conviction_tiers"]
    for idx, tier in enumerate(tiers):
        pseudo = {
            "id": f"sizing-tier-{idx}",
            "joiner": tier["joiner"],
            "conditions": tier["conditions"],
        }
        detail = evaluate_rule_detail(
            symbol,
            points,
            pseudo,
            marker_threshold=marker_threshold,
            index=index,
        )
        if detail.get("matched"):
            return float(tier["weight_pct"]), str(tier["label"])
    default_w = float(ps["default_weight_pct"])
    return default_w, f"默认仓位({default_w:.0f}%)"


def _normalize_side_rule(
    raw: Mapping[str, Any] | None,
    *,
    side: str,
    default_id: str,
    default_title: str,
) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(f"{side}_rule must be an object or null")
    conditions = raw.get("conditions")
    if not isinstance(conditions, Sequence) or isinstance(conditions, (str, bytes)) or not conditions:
        raise ValueError(f"{side}_rule.conditions must be a non-empty list")
    cleaned_conditions = []
    for idx, cond in enumerate(conditions):
        if not isinstance(cond, Mapping) or not str(cond.get("type") or "").strip():
            raise ValueError(f"{side}_rule.conditions[{idx}] requires type")
        item = copy.deepcopy(dict(cond))
        item.setdefault("id", f"{default_id}-c{idx + 1}")
        cleaned_conditions.append(item)
    joiner = "or" if str(raw.get("joiner") or "and").lower() == "or" else "and"
    cooldown = max(0, min(240, int(_number(raw.get("cooldown_minutes")) or 0)))
    out: dict[str, Any] = {
        "id": str(raw.get("id") or default_id).strip() or default_id,
        "enabled": bool(raw.get("enabled", True)),
        "side": "sell" if side == "sell" else "buy",
        "title": str(raw.get("title") or default_title).strip() or default_title,
        "joiner": joiner,
        "conditions": cleaned_conditions,
    }
    if cooldown > 0:
        out["cooldown_minutes"] = cooldown
    return out


def get_quant_rule_schema() -> dict[str, Any]:
    """Return self-describing schema for external AI rule synthesis and evaluation."""
    return {
        "version": 4,
        "target_default_symbol": "160723",
        "supported_special_symbols": ["BRN0Y"],
        "default_start_date": "20260701",
        "data_sources_catalog": list(QUANT_DATA_SOURCE_CATALOG),
        "monitor_source_to_pack": dict(MONITOR_SOURCE_TO_PACK),
        "data_sources_contract": {
            "summary": "所有存量及新增量化规则均须绑定并归集其依赖数据源 (data_sources)，自动进入好运器「数据源监控」中心巡检，并在盘中主/必选数据源异常时阻断误单。",
            "required_fields": ["id", "role", "required_in_session"],
            "optional_fields": ["symbols", "note"],
            "valid_ids": [item["id"] for item in QUANT_DATA_SOURCE_CATALOG],
            "auto_derivation": "保存规则时若未显式传递 data_sources，系统会基于 symbols/条件算子/position_sizing 自动推导并补全；显式传递 data_sources 时会校验全部 ID 合法性并与 AST 必选依赖合并，传入 strict_data_sources=true 时强制要求显式声明全部必选数据源。",
        },
        "data_packs": {
            "iopv_fx": {
                "title": "汇调净值与汇率折溢价/协整维度",
                "scope": "油气QDII基金及宏观汇率锚定标的 (160723/161129/501018/162411/159518/513350/160416/162719/163208)",
                "minute_fields": [
                    "iopv_fx",
                    "iopv_premium_pct",
                    "sp_fx_pct",
                    "coint10_pct",
                    "usdcny",
                    "usdcnh",
                    "usdcny_change_pct",
                    "fx_adjusted_nav_change_pct",
                    "official_nav",
                    "brent_q2y_pct",
                ],
                "daily_fields": [
                    "iopv_fx_close",
                    "iopv_premium_close_pct",
                    "sp_fx_close_pct",
                    "coint10_close_pct",
                    "usdcny_close",
                    "usdcnh_close",
                    "official_nav",
                    "official_nav_date",
                    "brent_q2y_close_pct",
                ],
            },
            "xop_us_ep": {
                "title": "美股 XOP/XLE/SPY/IWM 与 WTI/裂解/天然气高频维度",
                "scope": "162411 华宝油气及美股上游勘探映射标的",
                "minute_fields": [
                    "xop_change_pct",
                    "xle_change_pct",
                    "spy_change_pct",
                    "iwm_change_pct",
                    "cl_change_pct",
                    "ho_crack_change_pct",
                    "ng_change_pct",
                    "es_change_pct",
                    "xop_synthetic_premium_pct",
                    "daytime_lead_pct",
                    "daytime_crack_pct",
                ],
                "daily_fields": [
                    "xop_ov_pct",
                    "xle_ov_pct",
                    "spy_ov_pct",
                    "iwm_ov_pct",
                    "cl_ov_pct",
                    "ho_crack_ov_pct",
                    "ng_ov_pct",
                ],
            },
            "uso_bno_sc_peer": {
                "title": "USO+BNO+DBO美油篮子 / 沪油SC0 / 同业油气价差联动维度",
                "scope": "161129/501018/160723 等内盘-外盘-同业三锚联动",
                "minute_fields": [
                    "uso_bno_change_pct",
                    "uso_change_pct",
                    "bno_change_pct",
                    "dbo_change_pct",
                    "fof_3oil_change_pct",
                    "dual_oil_premium_pct",
                    "sc0_change_pct",
                    "sc0_intraday_pct",
                    "sc_brent_spread_pct",
                    "peer_spread_pct",
                    "peer_change_pct",
                    "peer_premium_diff_pct",
                    "peer_160723_spread_pct",
                    "peer_161129_spread_pct",
                    "peer_501018_spread_pct",
                ],
                "daily_fields": [
                    "uso_bno_ov_pct",
                    "uso_ov_pct",
                    "bno_ov_pct",
                    "dbo_ov_pct",
                    "fof_3oil_ov_pct",
                    "sc0_close_pct",
                    "peer_close_spread_pct",
                ],
            },
            "l2_darkpool_flow": {
                "title": "A股个股 Level-2 主力明盘/暗盘/散户分钟资金流（当日/3日/5日）",
                "scope": "A股及场内基金 L2 分钟资金流（统一归一化为万元）",
                "minute_fields": [
                    "l2_today_main_net_wan",
                    "l2_today_visible_wan",
                    "l2_today_hidden_wan",
                    "l2_today_retail_wan",
                    "l2_today_hidden_ratio_pct",
                    "l2_3d_main_net_wan",
                    "l2_3d_visible_wan",
                    "l2_3d_hidden_wan",
                    "l2_3d_retail_wan",
                    "l2_5d_main_net_wan",
                    "l2_5d_visible_wan",
                    "l2_5d_hidden_wan",
                    "l2_5d_retail_wan",
                ],
                "daily_fields": [
                    "l2_today_main_net_wan",
                    "l2_today_visible_wan",
                    "l2_today_hidden_wan",
                    "l2_today_retail_wan",
                    "l2_3d_main_net_wan",
                    "l2_5d_main_net_wan",
                ],
            },
            "etf_redemption_flow": {
                "title": "场内 ETF T+1 申赎净流入与基金规模维度（严格 T-1 无前瞻）",
                "scope": "支持 THS 申赎数据的 ETF 与个股联结基金（单位：万元 / 亿元）",
                "minute_fields": [
                    "etf_net_buy_1d_wan",
                    "etf_net_buy_3d_wan",
                    "etf_net_buy_5d_wan",
                    "etf_net_buy_20d_wan",
                    "etf_share_scale_yi",
                    "etf_net_buy_ratio_pct",
                ],
                "daily_fields": [
                    "etf_flow_ref_date",
                    "etf_net_buy_1d_wan",
                    "etf_net_buy_3d_wan",
                    "etf_net_buy_5d_wan",
                    "etf_net_buy_20d_wan",
                    "etf_share_scale_yi",
                    "etf_net_buy_ratio_pct",
                ],
            },
        },
        "evaluation_contract": {
            "primary_metric": "base_position_t0.net_cum_return_pct",
            "capital_sim_metric": "capital_sim.net_return_pct",
            "t0_rule": "账户持有固定底仓，每日早盘窗口(默认09:30-11:30，默认取首个信号)若触发买点则早盘买入1份并在15:00收盘卖出恢复底仓；若触发卖点则早盘卖出1份底仓并在15:00收盘买回恢复底仓；扣减双边交易成本 round_trip_fee_pct（默认0.02%）。",
            "legacy_win_rate_reference": "同时保留 +1m/+3m/+5m/+10m/+15m/+30m/收盘 多窗口胜率、MFE/MAE 与 7 维归因作为辅助诊断。",
            "horizons_minutes": list(_HORIZONS),
            "tail_handling": "尾盘剩余不足 N 分钟的信号单独计入 insufficient_tail，不计入该窗口胜率分母",
        },
        "position_sizing_contract": {
            "summary": "用户只需配置一个「策略可用金额上限(capital_cap / daily_max_buy_amount)」，量化模型通过 position_sizing 自主决策运作模式（底仓日内做T或资金池波段滚动）及每笔买卖 100 股整数倍股数。",
            "modes": {
                "base_t0_close": "底仓日内做T闭环模式：日内按可用金额上限与信号强度分档买入/卖出 100 的整数倍股数，并于 close_flatten_time（默认 14:53）自动生成对称平T报单恢复底仓。",
                "cash_pool_rolling": "纯可用资金池滚动模式：在可用金额上限内按买点信号分档买入整手股数，按卖点信号分档卖出策略持仓对应比例股数（160723 等跨境/商品 LOF 支持日内 T+0 卖出，普通 A 股自动遵循 T+1）。",
            },
            "conviction_tiers": "按规则条件从上到下匹配，命中首条分档即采用其 weight_pct（5~100% 动用可用资金或可卖持仓比例），未命中则回退 default_weight_pct；再结合实时对手盘挂单价向下取整为 100 股整数倍。",
            "fields": {
                "mode": "base_t0_close | cash_pool_rolling",
                "engine_profile": "standard | v9_plus_aggressive | v9_plus_balanced（V9+ 支持汇调实时溢价、BRN0Y 2年分位三态收盘调仓与三档动态做T）",
                "cross_market_anchor": "brent | xop_us_ep | uso_bno_sc_peer | fof_crude_core | fof_t0_multisource | sp_oilgas_etf（可选，默认 brent；xop_us_ep 适配华宝油气 162411；uso_bno_sc_peer 适配易方达原油 161129；fof_crude_core 适配嘉实原油 160723 原版；fof_t0_multisource 为嘉实原油 160723 专属 T+0 双池 10 源高频回转引擎，融合 85% 底仓+15% T+0 机动金、CME CL 1m、XOP 篮子与 501018/161129/162411 三同业共振；sp_oilgas_etf 适配标普油气 ETF 513350）",
                "default_weight_pct": "5..100（默认 50.0）",
                "max_position_pct": "10..100（默认 100.0，单日或总仓位相对可用金额上限的最大占比）",
                "max_daily_entries": "1..20（默认 2）",
                "cooldown_minutes": "1..60（默认 4，V9+ 日内平仓后再入场冷却分钟）",
                "scale_factor": "0.5..1.5（默认 1.0，V9+ 阈值灵敏度缩放）",
                "lot_size": "100（A股/ETF/LOF 整手单位）",
                "close_flatten_time": "14:40..14:55（默认 14:53，仅 base_t0_close 生效）",
                "allow_same_day_turnaround": "auto | true | false（auto 对 160723/501018/513xxx/1599xx 自动启用 T+0）",
                "conviction_tiers": "[{\"label\": str, \"weight_pct\": float, \"joiner\": \"and|or\", \"conditions\": [...]}]",
                "sell_conviction_tiers": "可选卖出/减仓分档比例列表；为空时卖出复用 conviction_tiers 或 default_weight_pct",
                "v9_tuning": "可选 V9+ 标的微观结构微调参数（bear_high_prem_flat_pct / bull_oil_surge_guard / bull_oil_surge_tier_cap_pct / scalp_strict_mode / macd_price_anchor / buy_prem_offset_pct / bubble_prem_cap_pct / bubble_overnight_weight_pct / bull_pullback_weight_pct / strict_dip_scalp / multi_oil_plunge_mode / plunge_dd15_pct / high_oil_bull_cap_pct / peer_symbol）",
            },
        },
        "cross_market_brent": {
            "symbol": "BRN0Y",
            "name": "ICE 布伦特原油连续",
            "trading_hours_et": "Sun 18:00 - Fri 17:00 America/New_York (daily settlement break Mon-Thu 18:00-20:00 ET)",
            "alignment_rule": "严格无前瞻对齐(Asia/Shanghai): BRN0Y 分钟时间戳 <= (trade_date, minute); prev_1500 取前一A股交易日 <=15:00 最新 BRN0Y 成交价; open_0930 取当日 <=09:30 最新 BRN0Y 成交价",
            "minute_fields": [
                "brn_price",
                "brn_overnight_pct",
                "brn_from_prev_close_pct",
                "brn_intraday_pct",
                "brn_mom_5m_pct",
                "brn_mom_15m_pct",
                "brn_spread_pct",
            ],
            "daily_fields": [
                "brn_prev_1500",
                "brn_open_0930",
                "brn_close_1500",
                "brn_overnight_pct",
            ],
            "direct_dataset_query": "GET /api/darkpool/quant/dataset?symbol=BRN0Y&start_date=20260701&mode=summary|daily|minutes",
            "live_execution_support": "AutoTradeEngine (signal_source=\"quant_rule\") 每次轮询实时为 A 股分时序列注入严格对齐的 BRN0Y 跨市场特征，确保回测与实盘/模拟盘 brent_* 条件触发逻辑完全一致。",
        },
        "rule_payload_example": {
            "id": "ai-160723-brn0y-morning-t0-v1",
            "name": "布伦特隔夜价差早盘底仓做T v1",
            "description": "20260701起样本外验证：利用BRN0Y隔夜及盘中跨市场价差，早盘低吸/高抛并在15:00收盘恢复底仓",
            "source": "ai",
            "symbols": ["160723"],
            "macd_settings": {"short": 8, "long": 17, "signal": 5, "marker_threshold": 0.0003},
            "buy_rule": {
                "title": "布伦特偏强+A股滞涨低吸",
                "joiner": "and",
                "cooldown_minutes": 15,
                "conditions": [
                    {"type": "time_window", "windows": [["09:31", "11:25"]]},
                    {"type": "brent_spread_pct", "direction": "lte", "value": -0.35},
                    {"type": "vwap_deviation_pct", "direction": "lte", "value": -0.05},
                ],
            },
            "sell_rule": {
                "title": "布伦特偏弱+A股溢价高抛",
                "joiner": "and",
                "cooldown_minutes": 15,
                "conditions": [
                    {"type": "time_window", "windows": [["09:31", "11:25"]]},
                    {"type": "brent_spread_pct", "direction": "gte", "value": 0.35},
                    {"type": "vwap_deviation_pct", "direction": "gte", "value": 0.05},
                ],
            },
            "position_sizing": {
                "mode": "base_t0_close",
                "default_weight_pct": 50.0,
                "max_position_pct": 100.0,
                "max_daily_entries": 2,
                "lot_size": 100,
                "close_flatten_time": "14:53",
                "allow_same_day_turnaround": "auto",
                "conviction_tiers": [
                    {
                        "label": "高胜率强分歧重仓(100%)",
                        "weight_pct": 100.0,
                        "joiner": "and",
                        "conditions": [{"type": "brent_spread_pct", "direction": "lte", "value": -0.6}],
                    }
                ],
                "sell_conviction_tiers": [
                    {
                        "label": "高溢价重仓反T(100%)",
                        "weight_pct": 100.0,
                        "joiner": "and",
                        "conditions": [{"type": "brent_spread_pct", "direction": "gte", "value": 0.6}],
                    }
                ],
            },
            "eval_criteria": {
                "start_date": "20260701",
                "data_scope": "auto",
                "ranking_metric": "t0_net_return_pct",
                "morning_start": "09:30",
                "morning_end": "11:30",
                "max_daily_trades": 1,
                "round_trip_fee_pct": 0.02,
                "capital_cap": 20000.0,
            },
            "data_sources": [
                {"id": "ashare_intraday_1m", "role": "primary", "required_in_session": True, "symbols": ["160723"]},
                {"id": "ice_brent_1m", "role": "anchor", "required_in_session": True, "symbols": ["BRN0Y"]},
            ],
        },
        "condition_types": [
            {"type": "macd_signal", "fields": {"signal": "red | green | both"}, "note": "MACD DIFF/DEA 金叉红点或死叉绿点，受 marker_threshold 过滤"},
            {"type": "vwap_deviation_pct", "fields": {"direction": "lte | gte", "value": "float (%)"}, "note": "(最新价 - 当日均价) / 当日均价 * 100"},
            {"type": "macd_state", "fields": {"metric": "diff | dea | macd | abs_macd", "direction": "lte | gte", "value": "float"}, "note": "MACD 绝对位置或柱体强度"},
            {"type": "time_window", "fields": {"windows": "[[\"09:31\",\"11:25\"]]"}, "note": "限制触发时段"},
            {"type": "volume_ratio", "fields": {"lookback": "int (2..120, default 20)", "direction": "lte | gte", "value": "float"}, "note": "当前分钟成交量 / 前 lookback 分钟均量"},
            {"type": "price_momentum_pct", "fields": {"minutes": "int (1..240)", "direction": "lte | gte", "value": "float (%)"}, "note": "相对前 N 个交易分钟的价格涨跌幅"},
            {"type": "flow_change_pct", "fields": {"flow": "big | retail | hidden | mid", "minutes": "int", "direction": "increase | decrease", "value": "float (%)"}, "note": "依赖大单/资金流数据；仅在有大单样本日生效"},
            {"type": "change_pct", "fields": {"direction": "lte | gte", "value": "float (%)"}, "note": "相对昨收的日内涨跌幅"},
            {"type": "price_threshold", "fields": {"direction": "lte | gte", "value": "float"}, "note": "绝对价格条件"},
            {"type": "swing_reversal", "fields": {"side": "buy | sell", "drawdownPct": "float", "bouncePct": "float", "takeProfitPct": "float", "stopLossPct": "float", "pullbackPct": "float", "gapMinutes": "int"}, "note": "T0 波段反转状态机"},
            {"type": "code_filter", "fields": {"codes": "list[str]"}, "note": "6位代码白名单，留空代表全部"},
            {"type": "brent_overnight_pct", "fields": {"direction": "gte | lte", "value": "float (%)"}, "note": "BRN0Y 隔夜涨跌幅（当日09:30布伦特 vs 前一A股交易日15:00布伦特）"},
            {"type": "brent_change_pct", "fields": {"anchor": "prev_1500 | open_0930", "direction": "gte | lte", "value": "float (%)"}, "note": "当前分钟 BRN0Y 相对前日15:00或今日09:30的累计涨跌幅"},
            {"type": "brent_momentum_pct", "fields": {"minutes": "int (1..240)", "direction": "gte | lte", "value": "float (%)"}, "note": "当前分钟 BRN0Y 过去 N 分钟动量涨跌幅"},
            {"type": "brent_spread_pct", "fields": {"direction": "lte | gte", "value": "float (%)"}, "note": "跨市场偏离：A股当日涨跌幅% - BRN0Y较前日15:00涨跌幅%（负值代表A股滞涨折价）"},
            {"type": "iopv_premium_pct", "data_pack": "iopv_fx", "fields": {"metric": "premium_pct | sp_fx_pct | coint10_pct | usdcny_change_pct | brent_q2y_pct", "direction": "lte | gte", "value": "float (%)"}, "note": "基于汇调IOPV的实时折溢价率、跨市场价差 sp_fx、10日协整偏离或布伦特2年分位数"},
            {"type": "fx_spread_pct", "data_pack": "iopv_fx", "fields": {"metric": "sp_fx_pct | usdcny | usdcny_change_pct | fx_adjusted_nav_change_pct", "direction": "lte | gte", "value": "float"}, "note": "离岸汇率 USD/CNY 及汇调价差过滤算子"},
            {"type": "xop_macro_pct", "data_pack": "xop_us_ep", "fields": {"metric": "xop_change_pct | xle_change_pct | spy_change_pct | iwm_change_pct | cl_change_pct | ho_crack_change_pct | ng_change_pct | es_change_pct | xop_synthetic_premium_pct | daytime_lead_pct | daytime_crack_pct", "direction": "lte | gte", "value": "float (%)"}, "note": "美股能源 ETF / WTI裂解 / 天然气 / ES期指高频代理条件"},
            {"type": "sc_crude_pct", "data_pack": "uso_bno_sc_peer", "fields": {"metric": "sc0_change_pct | sc0_intraday_pct | uso_bno_change_pct | dual_oil_premium_pct | sc_brent_spread_pct", "direction": "lte | gte", "value": "float (%)"}, "note": "沪原油 SC0、USO+BNO双油锚与内外盘油价差条件"},
            {"type": "peer_spread_pct", "data_pack": "uso_bno_sc_peer", "fields": {"peer": "auto | 160723 | 161129 | 501018 | 162411", "metric": "spread_pct | peer_change_pct | peer_premium_diff_pct", "direction": "lte | gte", "value": "float (%)"}, "note": "同业油气LOF日内相对价差或同业涨跌幅偏离"},
            {"type": "l2_flow_level", "data_pack": "l2_darkpool_flow", "fields": {"period": "today | three_day | five_day", "flow": "main_net | main_visible | main_hidden | retail | hidden_ratio", "direction": "lte | gte", "value": "float (万元或%)"}, "note": "A股 Level-2 主力明盘/暗盘/散户资金流净额及暗盘占比（当日/3日/5日）"},
            {"type": "etf_net_flow", "data_pack": "etf_redemption_flow", "fields": {"metric": "net_buy_1d_wan | net_buy_3d_wan | net_buy_5d_wan | net_buy_20d_wan | share_scale_yi | net_buy_ratio_pct", "direction": "lte | gte", "value": "float (万元/亿元/%)"}, "note": "严格 T-1 无前瞻场内 ETF 申赎净流入与基金规模过滤"},
        ],
    }


def fetch_10jqka_line_funds(
    symbol: str,
    *,
    timeout: float = 8.0,
    fetch_text: Callable[[str, str, float], str] | None = None,
) -> list[dict[str, str]]:
    """Fetch intraday minute big/mid/small net inflow (万元) from Tonghuashun spService."""
    normalized = symbol.strip()
    if not _SYMBOL_RE.fullmatch(normalized):
        raise ValueError("symbol must be a 6-digit code")
    url = f"https://stockpage.10jqka.com.cn/spService/{normalized}/Funds/lineFunds"
    referer = f"https://stockpage.10jqka.com.cn/{normalized}/funds/"
    raw_text = (
        fetch_text(url, referer, timeout)
        if fetch_text is not None
        else _http_get_direct(url, referer=referer, timeout=timeout)
    )
    payload = json.loads(raw_text)
    line = str(payload.get("line") or "")
    rows: list[dict[str, str]] = []
    for chunk in line.split("|"):
        parts = [p.strip() for p in chunk.split(";")]
        if len(parts) < 4:
            continue
        hhmm = parts[0]
        if not re.fullmatch(r"[0-9]{2}:[0-9]{2}", hhmm):
            continue
        compact = hhmm.replace(":", "")
        if not ("0930" <= compact <= "1130" or "1300" <= compact <= "1500"):
            continue
        big_val = _number(parts[1])
        mid_val = _number(parts[2])
        small_val = _number(parts[3])
        if big_val is None:
            continue
        rows.append(
            {
                "time": hhmm,
                "large_order_amount": f"{big_val:.2f}",
                "mid_order_net": f"{mid_val:.2f}" if mid_val is not None else "",
                "retail_count": f"{small_val:.2f}" if small_val is not None else "",
            }
        )
    return rows


def enrich_store_with_line_funds(
    store: Any,
    symbol: str,
    *,
    trade_date: str | None = None,
    now: datetime | None = None,
    timeout: float = 8.0,
    fetch_text: Callable[[str, str, float], str] | None = None,
) -> dict[str, Any]:
    """Backfill missing large_order_amount / retail_count from 10jqka lineFunds safely."""
    local_now = (now or datetime.now(SHANGHAI_TZ)).astimezone(SHANGHAI_TZ)
    rows = fetch_10jqka_line_funds(symbol, timeout=timeout, fetch_text=fetch_text)
    if not rows:
        return {"symbol": symbol, "trade_date": trade_date, "updated_minutes": 0, "reason": "empty_line_funds"}

    with store._lock:
        if trade_date is None:
            day_row = store._connection.execute(
                "SELECT trade_date FROM research_days WHERE symbol=? ORDER BY trade_date DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            target_date = str(day_row["trade_date"]) if day_row else None
        else:
            target_date = trade_date
        if target_date is None:
            return {"symbol": symbol, "trade_date": None, "updated_minutes": 0, "reason": "no_research_day"}

        latest_db_row = store._connection.execute(
            "SELECT trade_date FROM research_days WHERE symbol=? ORDER BY trade_date DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        latest_db_date = str(latest_db_row["trade_date"]) if latest_db_row else target_date
        if target_date != latest_db_date:
            return {
                "symbol": symbol,
                "trade_date": target_date,
                "updated_minutes": 0,
                "reason": "skip_non_latest_date",
            }

        today_str = local_now.strftime("%Y%m%d")
        now_hhmm = local_now.strftime("%H:%M")
        last_line_time = rows[-1]["time"]
        if target_date == today_str and now_hhmm < "15:00":
            now_min = int(now_hhmm[:2]) * 60 + int(now_hhmm[3:5])
            line_min = int(last_line_time[:2]) * 60 + int(last_line_time[3:5])
            if line_min > now_min + 5:
                return {
                    "symbol": symbol,
                    "trade_date": target_date,
                    "updated_minutes": 0,
                    "reason": "stale_previous_day_line_funds",
                }

        updated = 0
        with store._connection:
            for item in rows:
                if target_date == today_str and item["time"] > now_hhmm and now_hhmm < "15:00":
                    continue
                cur = store._connection.execute(
                    """UPDATE research_points
                       SET large_order_amount = COALESCE(large_order_amount, ?),
                           retail_count = COALESCE(retail_count, ?)
                       WHERE symbol=? AND trade_date=? AND minute=?
                         AND (large_order_amount IS NULL OR retail_count IS NULL)""",
                    (
                        item["large_order_amount"],
                        item["retail_count"] or None,
                        symbol,
                        target_date,
                        item["time"],
                    ),
                )
                updated += int(cur.rowcount or 0)
    return {"symbol": symbol, "trade_date": target_date, "updated_minutes": updated}


def _tdx_decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    pos_byte = 6
    bdata = data[pos]
    intdata = bdata & 0x3F
    sign = bool(bdata & 0x40)
    if bdata & 0x80:
        while True:
            pos += 1
            bdata = data[pos]
            intdata += (bdata & 0x7F) << pos_byte
            pos_byte += 7
            if not (bdata & 0x80):
                break
    pos += 1
    return (-intdata if sign else intdata), pos


def _tdx_recv_packet(sock: socket.socket) -> bytes:
    hdr = b""
    while len(hdr) < 16:
        chunk = sock.recv(16 - len(hdr))
        if not chunk:
            raise EOFError("TDX header EOF")
        hdr += chunk
    _, _, _, zipsize, unzipsize = struct.unpack("<IIIHH", hdr)
    body = b""
    while len(body) < zipsize:
        chunk = sock.recv(zipsize - len(body))
        if not chunk:
            raise EOFError("TDX body EOF")
        body += chunk
    return zlib.decompress(body) if zipsize != unzipsize else body


def fetch_tdx_history_minute_data(
    symbol: str,
    trade_dates: Sequence[str],
    *,
    hosts: Sequence[tuple[str, int]] = _TDX_HOSTS,
    timeout: float = 5.0,
) -> dict[str, dict[str, Any]]:
    """Fetch 240x1m historical intraday bars via TDX 0x0fb4 protocol."""
    normalized = symbol.strip()
    if not _SYMBOL_RE.fullmatch(normalized) or not trade_dates:
        return {}
    market = 1 if normalized.startswith(("5", "6", "9")) else 0
    is_fund = is_exchange_fund_symbol(normalized)
    divisor = 1000.0 if is_fund else 100.0
    precision = 3 if is_fund else 2
    code_bytes = normalized.encode("ascii")

    last_err: Exception | None = None
    for host, port in hosts:
        sock: socket.socket | None = None
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.settimeout(timeout)
            sock.sendall(bytes.fromhex("0c0218930001030003000d0001"))
            sock.recv(1024)
            sock.sendall(bytes.fromhex("0c0218940001030003000d0002"))
            sock.recv(1024)

            results: dict[str, dict[str, Any]] = {}
            for trade_date in trade_dates:
                dt_int = int(trade_date)
                pkt = bytes.fromhex("0c01300001010d000d00b40f") + struct.pack("<IB6s", dt_int, market, code_bytes)
                sock.sendall(pkt)
                raw = _tdx_recv_packet(sock)
                if len(raw) < 6:
                    continue
                num, pre_raw = struct.unpack("<Hf", raw[:6])
                if num <= 0:
                    continue
                pos = 6
                last_p = 0
                cum_amt = 0.0
                cum_vol = 0
                pts: list[dict[str, str]] = []
                for idx in range(num):
                    dp, pos = _tdx_decode_varint(raw, pos)
                    _, pos = _tdx_decode_varint(raw, pos)
                    vol_lots, pos = _tdx_decode_varint(raw, pos)
                    last_p += dp
                    price_val = last_p / divisor
                    vol_shares = max(0, vol_lots) * 100
                    cum_vol += vol_shares
                    cum_amt += price_val * vol_shares
                    avg_val = (cum_amt / cum_vol) if cum_vol > 0 else price_val
                    minute_val = (570 + idx + 1) if idx < 120 else (780 + (idx - 120) + 1)
                    pts.append(
                        {
                            "minute": f"{minute_val // 60:02d}:{minute_val % 60:02d}",
                            "price": f"{price_val:.{precision}f}",
                            "average_price": f"{avg_val:.3f}",
                            "volume": str(vol_shares),
                        }
                    )
                results[trade_date] = {
                    "pre_close": f"{pre_raw:.{precision}f}" if pre_raw > 0 else None,
                    "points": pts,
                }
            return results
        except Exception as err:
            last_err = err
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    if last_err is not None:
        raise RuntimeError(f"TDX 0x0fb4 failed: {last_err}") from last_err
    return {}


def import_opm_history_to_store(
    store: Any,
    opm_db_path: Path,
    symbol: str,
    *,
    start_date: str = "20260301",
) -> dict[str, int]:
    """Read-only import of historical minute + big_net_wan flow from OPM quotes.sqlite3."""
    resolved = Path(opm_db_path).expanduser().resolve()
    if not resolved.is_file():
        return {"days": 0, "points": 0}
    src = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        day_rows = src.execute(
            """SELECT code, trade_date, name, pre_close, captured_at
               FROM trade_days WHERE code=? AND trade_date>=? ORDER BY trade_date""",
            (symbol, start_date),
        ).fetchall()
        pt_rows = src.execute(
            """SELECT code, trade_date, minute, price, avg_price, volume_shares,
                      big_net_wan, small_net_wan, captured_at
               FROM minute_quotes WHERE code=? AND trade_date>=?
               ORDER BY trade_date, REPLACE(minute, ':', '')""",
            (symbol, start_date),
        ).fetchall()
    finally:
        src.close()

    if not pt_rows:
        return {"days": 0, "points": 0}

    is_fund = is_exchange_fund_symbol(symbol)
    precision = 3 if is_fund else 2
    market = "36" if symbol.startswith(("15", "16", "18")) else ("20" if symbol.startswith("5") else ("17" if symbol.startswith("6") else "33"))
    last_point_by_day: dict[str, sqlite3.Row] = {}
    for pt in pt_rows:
        last_point_by_day[str(pt["trade_date"])] = pt

    day_params: list[tuple[Any, ...]] = []
    seen_days: set[str] = set()
    for d in day_rows:
        td = str(d["trade_date"])
        seen_days.add(td)
        pre = float(d["pre_close"]) if d["pre_close"] else None
        last_pt = last_point_by_day.get(td)
        close_p = float(last_pt["price"]) if last_pt is not None and last_pt["price"] else None
        chg_pct = (
            f"{((close_p - pre) / pre * 100):+.2f}%"
            if pre and pre > 0 and close_p and close_p > 0
            else None
        )
        day_params.append(
            (
                symbol,
                td,
                d["name"] or symbol,
                market,
                "OPM_ARCHIVE",
                f"{pre:.{precision}f}" if pre and pre > 0 else None,
                f"{close_p:.{precision}f}" if close_p and close_p > 0 else None,
                chg_pct,
                str(d["captured_at"] or _now_iso()),
            )
        )
    for td, last_pt in last_point_by_day.items():
        if td in seen_days:
            continue
        close_p = float(last_pt["price"]) if last_pt["price"] else None
        day_params.append(
            (
                symbol,
                td,
                symbol,
                market,
                "OPM_ARCHIVE",
                None,
                f"{close_p:.{precision}f}" if close_p and close_p > 0 else None,
                None,
                str(last_pt["captured_at"] or _now_iso()),
            )
        )

    pt_params: list[tuple[Any, ...]] = []
    for pt in pt_rows:
        price_val = pt["price"]
        avg_val = pt["avg_price"]
        vol_val = pt["volume_shares"]
        big_val = pt["big_net_wan"]
        small_val = pt["small_net_wan"]
        pt_params.append(
            (
                symbol,
                str(pt["trade_date"]),
                str(pt["minute"]),
                f"{float(price_val):.{precision}f}" if price_val is not None else None,
                f"{float(avg_val):.3f}" if avg_val is not None else None,
                str(int(vol_val)) if vol_val is not None else None,
                None,
                f"{float(big_val):.2f}" if big_val is not None else None,
                f"{float(small_val):.2f}" if small_val is not None else None,
                None,
                str(pt["captured_at"] or _now_iso()),
            )
        )

    with store._lock, store._connection:
        store._connection.executemany(
            """INSERT INTO research_days(
                   symbol,trade_date,name,market,source,pre_close,
                   quote_price,quote_change_percent,collected_at
               ) VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol,trade_date) DO UPDATE SET
                   name=COALESCE(NULLIF(research_days.name,''), excluded.name),
                   source=COALESCE(research_days.source, excluded.source),
                   pre_close=COALESCE(research_days.pre_close, excluded.pre_close),
                   quote_price=COALESCE(research_days.quote_price, excluded.quote_price),
                   quote_change_percent=COALESCE(research_days.quote_change_percent, excluded.quote_change_percent)""",
            day_params,
        )
        store._connection.executemany(
            """INSERT INTO research_points(
                   symbol,trade_date,minute,price,average_price,volume,
                   large_order_net,large_order_amount,retail_count,macdfs,captured_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol,trade_date,minute) DO UPDATE SET
                   price=COALESCE(research_points.price, excluded.price),
                   average_price=COALESCE(research_points.average_price, excluded.average_price),
                   volume=COALESCE(research_points.volume, excluded.volume),
                   large_order_amount=COALESCE(research_points.large_order_amount, excluded.large_order_amount),
                   retail_count=COALESCE(research_points.retail_count, excluded.retail_count)""",
            pt_params,
        )
    return {"days": len(day_params), "points": len(pt_params)}


def _read_tolerant_10jqka_bars(
    symbol: str,
    fetch_text_fn: Callable[[str, float], str],
    *,
    timeout: float = 8.0,
) -> tuple[KlineBar, ...]:
    """Fallback 10jqka daily reader tolerating partial post-close rows and old-year HTTP 502s."""
    cur_year = datetime.now(SHANGHAI_TZ).year
    years: list[int] = [cur_year, cur_year - 1]
    try:
        last_txt = fetch_text_fn(f"https://d.10jqka.com.cn/v6/line/hs_{symbol}/01/last.js", timeout)
        m_last = re.search(r"(\{.*\})\s*;?\s*$", last_txt.strip(), re.S)
        if m_last:
            yr_map = json.loads(m_last.group(1)).get("year")
            if isinstance(yr_map, Mapping):
                parsed_yrs = sorted(
                    {int(k) for k, v in yr_map.items() if str(k).isdigit() and int(v or 0) > 0},
                    reverse=True,
                )
                if parsed_yrs:
                    years = parsed_yrs[:2]
    except Exception:
        pass

    by_date: dict[str, KlineBar] = {}
    for yr in years:
        try:
            txt = fetch_text_fn(f"https://d.10jqka.com.cn/v6/line/hs_{symbol}/01/{yr}.js", timeout)
            m = re.search(r"(\{.*\})\s*;?\s*$", txt.strip(), re.S)
            if not m:
                continue
            raw_data = json.loads(m.group(1)).get("data")
            if not isinstance(raw_data, str):
                continue
        except Exception:
            continue
        for raw_row in raw_data.split(";"):
            if not raw_row:
                continue
            fields = raw_row.split(",")
            if len(fields) < 7 or not _DATE_RE.fullmatch(fields[0]):
                continue
            o_s, h_s, l_s, c_s, v_s, a_s = fields[1:7]
            if not (o_s and h_s and l_s and c_s):
                continue
            try:
                o_f, h_f, l_f, c_f = float(o_s), float(h_s), float(l_s), float(c_s)
                v_f = float(v_s) if v_s else 0.0
                a_f = float(a_s) if a_s else 0.0
            except ValueError:
                continue
            if (
                min(o_f, h_f, l_f, c_f) <= 0
                or h_f < max(o_f, c_f, l_f)
                or l_f > min(o_f, c_f, h_f)
                or v_f < 0
                or a_f < 0
            ):
                continue
            td_iso = f"{fields[0][:4]}-{fields[0][4:6]}-{fields[0][6:8]}"
            by_date[td_iso] = KlineBar(
                time=td_iso,
                open=o_s,
                high=h_s,
                low=l_s,
                close=c_s,
                volume=v_s or "0",
                amount=a_s or "0",
            )
    return tuple(by_date[k] for k in sorted(by_date))


def backfill_intraday_history(
    store: Any,
    symbol: str = "160723",
    *,
    start_date: str = "20260301",
    end_date: str | None = None,
    opm_db_path: Path | None = None,
    name: str | None = None,
    market: str | None = None,
    fetch_kline_fn: Callable[[str, str, float], str] | None = None,
    fetch_tdx_fn: Callable[[str, Sequence[str]], dict[str, dict[str, Any]]] | None = None,
    fetch_line_funds_fn: Callable[[str, str, float], str] | None = None,
) -> dict[str, Any]:
    """Backfill OPM archive + TDX 0x0fb4 minute history + 10jqka daily pre_close + lineFunds."""
    normalized = symbol.strip()
    if not _SYMBOL_RE.fullmatch(normalized):
        raise ValueError("symbol must be a 6-digit code")

    opm_stats = {"days": 0, "points": 0}
    if opm_db_path is not None:
        opm_stats = import_opm_history_to_store(store, opm_db_path, normalized, start_date=start_date)

    if fetch_kline_fn is None:
        raw_kline_fetch = lambda url, timeout: _http_get_direct(
            url, referer="https://stockpage.10jqka.com.cn/", timeout=timeout
        )
    else:
        raw_kline_fetch = lambda url, timeout: fetch_kline_fn(url, "https://stockpage.10jqka.com.cn/", timeout)
    provider = TonghuashunPublicDailyKlineProvider(fetch=raw_kline_fetch)
    try:
        bars = provider.read(normalized, minimum_bars=300)
    except Exception:
        bars = _read_tolerant_10jqka_bars(normalized, raw_kline_fetch)
    bar_by_date = {b.time.replace("-", ""): b for b in bars}
    sorted_dates = sorted(bar_by_date)
    pre_close_map: dict[str, str] = {}
    for idx, td in enumerate(sorted_dates):
        if idx > 0:
            pre_close_map[td] = str(bar_by_date[sorted_dates[idx - 1]].close)

    target_end = end_date or (sorted_dates[-1] if sorted_dates else datetime.now(SHANGHAI_TZ).strftime("%Y%m%d"))
    with store._lock:
        own_db_rows = store._connection.execute(
            "SELECT trade_date, pre_close FROM research_days WHERE symbol=? AND trade_date>=? AND trade_date<=?",
            (normalized, start_date, target_end),
        ).fetchall()
        own_db_dates = {str(r[0]) for r in own_db_rows}
        for r in own_db_rows:
            if r[1] and str(r[0]) not in pre_close_map:
                pre_close_map[str(r[0])] = str(r[1])
        peer_db_dates: set[str] = set()
        if not bar_by_date:
            peer_db_dates = {
                str(r[0])
                for r in store._connection.execute(
                    "SELECT DISTINCT trade_date FROM research_days WHERE symbol != 'BRN0Y' AND trade_date>=? AND trade_date<=?",
                    (start_date, target_end),
                ).fetchall()
            }
        counts = {
            str(r[0]): int(r[1])
            for r in store._connection.execute(
                "SELECT trade_date, COUNT(*) FROM research_points WHERE symbol=? AND trade_date>=? GROUP BY trade_date",
                (normalized, start_date),
            ).fetchall()
        }
    expected_dates = sorted(
        {td for td in sorted_dates if start_date <= td <= target_end} | own_db_dates | peer_db_dates
    )
    missing_or_partial = [td for td in expected_dates if counts.get(td, 0) < 235]

    tdx_loaded_days = 0
    tdx_points_written = 0
    is_fund = is_exchange_fund_symbol(normalized, market)
    precision = 3 if is_fund else 2
    if missing_or_partial:
        if fetch_tdx_fn is not None:
            tdx_map = fetch_tdx_fn(normalized, missing_or_partial)
        else:
            tdx_map = {}
            for chunk_start in range(0, len(missing_or_partial), 45):
                tdx_map.update(
                    fetch_tdx_history_minute_data(normalized, missing_or_partial[chunk_start : chunk_start + 45])
                )
        resolved_market = market or ("36" if normalized.startswith(("15", "16", "18")) else ("20" if normalized.startswith("5") else ("17" if normalized.startswith("6") else "33")))
        resolved_name = name or PREMIUM_NAMES.get(normalized, normalized)
        captured_at = _now_iso()
        day_rows_to_write: list[tuple[Any, ...]] = []
        pt_rows_to_write: list[tuple[Any, ...]] = []
        for td in missing_or_partial:
            tdx_day = tdx_map.get(td)
            if not tdx_day or not tdx_day.get("points"):
                continue
            tdx_loaded_days += 1
            bar = bar_by_date.get(td)
            pre = pre_close_map.get(td) or tdx_day.get("pre_close")
            if pre and td not in pre_close_map:
                try:
                    if float(pre) > 0:
                        pre_close_map[td] = f"{float(pre):.{precision}f}"
                except (TypeError, ValueError):
                    pass
            close_p = str(bar.close) if bar is not None else str(tdx_day["points"][-1]["price"])
            chg_pct = None
            if pre and float(pre) > 0:
                chg_pct = f"{((float(close_p) - float(pre)) / float(pre) * 100):+.2f}%"
            day_rows_to_write.append(
                (
                    normalized,
                    td,
                    resolved_name,
                    resolved_market,
                    "TDX_HIST_0FB4",
                    f"{float(pre):.{precision}f}" if pre else None,
                    f"{float(close_p):.{precision}f}" if close_p else None,
                    chg_pct,
                    str(bar.open) if bar is not None else None,
                    str(bar.high) if bar is not None else None,
                    str(bar.low) if bar is not None else None,
                    str(bar.volume) if bar is not None else None,
                    str(bar.amount) if bar is not None else None,
                    captured_at,
                )
            )
            for pt in tdx_day["points"]:
                pt_rows_to_write.append(
                    (
                        normalized,
                        td,
                        pt["minute"],
                        pt["price"],
                        pt["average_price"],
                        pt["volume"],
                        captured_at,
                    )
                )
        if day_rows_to_write or pt_rows_to_write:
            with store._lock, store._connection:
                if day_rows_to_write:
                    store._connection.executemany(
                        """INSERT INTO research_days(
                               symbol,trade_date,name,market,source,pre_close,
                               quote_price,quote_change_percent,quote_open,quote_high,
                               quote_low,quote_volume,quote_amount,collected_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(symbol,trade_date) DO UPDATE SET
                               pre_close=COALESCE(research_days.pre_close, excluded.pre_close),
                               quote_price=COALESCE(research_days.quote_price, excluded.quote_price),
                               quote_change_percent=COALESCE(research_days.quote_change_percent, excluded.quote_change_percent),
                               quote_open=COALESCE(research_days.quote_open, excluded.quote_open),
                               quote_high=COALESCE(research_days.quote_high, excluded.quote_high),
                               quote_low=COALESCE(research_days.quote_low, excluded.quote_low),
                               quote_volume=COALESCE(research_days.quote_volume, excluded.quote_volume),
                               quote_amount=COALESCE(research_days.quote_amount, excluded.quote_amount)""",
                        day_rows_to_write,
                    )
                if pt_rows_to_write:
                    store._connection.executemany(
                        """INSERT INTO research_points(
                               symbol,trade_date,minute,price,average_price,volume,captured_at
                           ) VALUES(?,?,?,?,?,?,?)
                           ON CONFLICT(symbol,trade_date,minute) DO UPDATE SET
                               price=COALESCE(research_points.price, excluded.price),
                               average_price=COALESCE(research_points.average_price, excluded.average_price),
                               volume=COALESCE(research_points.volume, excluded.volume)""",
                        pt_rows_to_write,
                    )
                    tdx_points_written = len(pt_rows_to_write)

    # Derive missing pre_close from prior trading day's close when 10jqka daily bars are unavailable
    with store._lock:
        all_sym_days = store._connection.execute(
            "SELECT trade_date, quote_price FROM research_days WHERE symbol=? ORDER BY trade_date ASC",
            (normalized,),
        ).fetchall()
        prev_td_close: str | None = None
        for row_td, row_close in all_sym_days:
            td_s = str(row_td)
            if prev_td_close and td_s not in pre_close_map:
                try:
                    if float(prev_td_close) > 0:
                        pre_close_map[td_s] = f"{float(prev_td_close):.{precision}f}"
                except (TypeError, ValueError):
                    pass
            cur_close = str(bar_by_date[td_s].close) if td_s in bar_by_date else (str(row_close) if row_close else None)
            if not cur_close:
                last_pt = store._connection.execute(
                    "SELECT price FROM research_points WHERE symbol=? AND trade_date=? ORDER BY minute DESC LIMIT 1",
                    (normalized, td_s),
                ).fetchone()
                if last_pt and last_pt[0] is not None:
                    cur_close = str(last_pt[0])
            if cur_close:
                prev_td_close = cur_close

    # Also backfill daily OHLCV / missing pre_close on existing days from 10jqka daily bars or prior-day close
    with store._lock, store._connection:
        for td in expected_dates:
            bar = bar_by_date.get(td)
            pre = pre_close_map.get(td)
            if bar is None and pre is None:
                continue
            store._connection.execute(
                """UPDATE research_days
                   SET pre_close = COALESCE(pre_close, ?),
                       quote_open = COALESCE(quote_open, ?),
                       quote_high = COALESCE(quote_high, ?),
                       quote_low = COALESCE(quote_low, ?),
                       quote_price = COALESCE(quote_price, ?),
                       quote_volume = COALESCE(quote_volume, ?),
                       quote_amount = COALESCE(quote_amount, ?)
                   WHERE symbol=? AND trade_date=?""",
                (
                    pre,
                    str(bar.open) if bar else None,
                    str(bar.high) if bar else None,
                    str(bar.low) if bar else None,
                    str(bar.close) if bar else None,
                    str(bar.volume) if bar else None,
                    str(bar.amount) if bar else None,
                    normalized,
                    td,
                ),
            )

    line_funds_result: dict[str, Any] = {"updated_minutes": 0}
    try:
        line_funds_result = enrich_store_with_line_funds(
            store, normalized, fetch_text=fetch_line_funds_fn
        )
    except Exception as err:
        line_funds_result = {"updated_minutes": 0, "error": str(err)}

    coverage = get_symbol_coverage(store, normalized, start_date=start_date)
    return {
        "symbol": normalized,
        "opm_imported": opm_stats,
        "tdx_days_backfilled": tdx_loaded_days,
        "tdx_points_written": tdx_points_written,
        "line_funds": line_funds_result,
        "coverage": coverage,
    }


def get_symbol_coverage(
    store: Any,
    symbol: str = "160723",
    *,
    start_date: str = "20260301",
) -> dict[str, Any]:
    with store._lock:
        rows = store._connection.execute(
            """SELECT d.trade_date,
                      COUNT(p.minute) AS point_count,
                      SUM(CASE WHEN p.large_order_net IS NOT NULL OR p.large_order_amount IS NOT NULL THEN 1 ELSE 0 END) AS flow_count
               FROM research_days d
               LEFT JOIN research_points p
                 ON p.symbol = d.symbol AND p.trade_date = d.trade_date
               WHERE d.symbol = ? AND d.trade_date >= ?
               GROUP BY d.trade_date
               ORDER BY d.trade_date ASC""",
            (symbol, start_date),
        ).fetchall()
    dates = [str(r["trade_date"]) for r in rows]
    flow_dates = [str(r["trade_date"]) for r in rows if int(r["flow_count"] or 0) >= 50]
    price_only_dates = [str(r["trade_date"]) for r in rows if int(r["flow_count"] or 0) < 50]
    total_points = sum(int(r["point_count"] or 0) for r in rows)
    return {
        "symbol": symbol,
        "start_date": start_date,
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "total_days": len(dates),
        "flow_days": len(flow_dates),
        "price_only_days": len(price_only_dates),
        "total_points": total_points,
        "flow_first_date": flow_dates[0] if flow_dates else None,
        "flow_last_date": flow_dates[-1] if flow_dates else None,
    }


def normalize_requested_data_packs(
    raw_packs: Sequence[str] | str | None,
    *,
    default_all: bool = False,
) -> list[str]:
    """Normalize data-pack selectors from query params or pack lists."""
    if raw_packs is None:
        return list(VALID_QUANT_DATA_PACKS) if default_all else []
    if isinstance(raw_packs, str):
        tokens = [tok.strip().lower() for tok in raw_packs.split(",") if tok.strip()]
    else:
        tokens = [str(tok).strip().lower() for tok in raw_packs if str(tok).strip()]
    if not tokens:
        return []
    if "all" in tokens:
        return list(VALID_QUANT_DATA_PACKS)
    valid_set = set(VALID_QUANT_DATA_PACKS)
    deduped: list[str] = []
    for tok in tokens:
        if tok in valid_set and tok not in deduped:
            deduped.append(tok)
    return deduped


def normalize_requested_source_ids(
    raw_sources: Sequence[str] | str | None,
    *,
    default_all: bool = False,
) -> list[str]:
    """Normalize monitor data-source IDs from query params or sequence inputs."""
    all_ids = [str(item["id"]) for item in QUANT_DATA_SOURCE_CATALOG]
    if raw_sources is None:
        return all_ids if default_all else []
    if isinstance(raw_sources, str):
        tokens = [tok.strip() for tok in raw_sources.split(",") if tok.strip()]
    else:
        tokens = [str(tok).strip() for tok in raw_sources if str(tok).strip()]
    if not tokens:
        return []
    if any(t.lower() == "all" for t in tokens):
        return all_ids
    valid_set = set(MONITOR_SOURCE_TO_PACK.keys())
    deduped: list[str] = []
    for tok in tokens:
        if tok in valid_set and tok not in deduped:
            deduped.append(tok)
    return deduped


def resolve_packs_from_sources_and_packs(
    raw_packs: Sequence[str] | str | None,
    raw_sources: Sequence[str] | str | None = None,
    *,
    default_all: bool = False,
) -> tuple[list[str], list[str]]:
    """Resolve union of selected packs & requested 13-catalog monitor source IDs."""
    has_packs_arg = raw_packs is not None and (
        bool(raw_packs.strip()) if isinstance(raw_packs, str) else bool(list(raw_packs))
    )
    has_sources_arg = raw_sources is not None and (
        bool(raw_sources.strip()) if isinstance(raw_sources, str) else bool(list(raw_sources))
    )
    if not has_packs_arg and not has_sources_arg:
        selected_packs = list(VALID_QUANT_DATA_PACKS) if (default_all and raw_packs is None and raw_sources is None) else []
        all_ids = [str(item["id"]) for item in QUANT_DATA_SOURCE_CATALOG]
        if selected_packs:
            return selected_packs, all_ids
        base_sources = [sid for sid, pk in MONITOR_SOURCE_TO_PACK.items() if pk is None]
        return [], base_sources

    pack_list = normalize_requested_data_packs(raw_packs, default_all=False)
    src_list = normalize_requested_source_ids(raw_sources, default_all=False)
    combined_packs: list[str] = list(pack_list)
    for sid in src_list:
        pk = MONITOR_SOURCE_TO_PACK.get(sid)
        if pk and pk not in combined_packs:
            combined_packs.append(pk)

    if has_sources_arg and not has_packs_arg:
        final_sources = src_list
    else:
        pack_set = set(combined_packs)
        final_sources = list(src_list)
        for item in QUANT_DATA_SOURCE_CATALOG:
            sid = str(item["id"])
            pk = MONITOR_SOURCE_TO_PACK.get(sid)
            if (pk is None or pk in pack_set) and sid not in final_sources:
                final_sources.append(sid)
    return combined_packs, final_sources


def validate_quant_rule_spec(
    buy_rule: Mapping[str, Any] | None,
    sell_rule: Mapping[str, Any] | None,
    position_sizing: Mapping[str, Any] | None = None,
) -> None:
    """Strictly validate all buy/sell/tier conditions in a quant rule spec."""
    for side_name, rule_obj in (("buy_rule", buy_rule), ("sell_rule", sell_rule)):
        if rule_obj is None:
            continue
        if not isinstance(rule_obj, Mapping):
            raise ValueError(f"{side_name} must be an object or null")
        for idx, cond in enumerate(rule_obj.get("conditions") or []):
            validate_dsl_condition(cond, prefix=f"{side_name}.conditions[{idx}]")
    if isinstance(position_sizing, Mapping):
        for tiers_key in ("conviction_tiers", "sell_conviction_tiers"):
            for t_idx, tier in enumerate(position_sizing.get(tiers_key) or []):
                if not isinstance(tier, Mapping):
                    continue
                for c_idx, cond in enumerate(tier.get("conditions") or []):
                    validate_dsl_condition(
                        cond,
                        prefix=f"position_sizing.{tiers_key}[{t_idx}].conditions[{c_idx}]",
                    )


def _parse_flow_to_wan(raw: Any, unit: Any = None) -> float | None:
    """Normalize THS fund flow numbers into 万元 (ten-thousand CNY)."""
    val = _number(raw)
    if val is None:
        return None
    u = str(unit or "").strip()
    if "亿" in u or u == "100000000":
        return round(val * 10000.0, 4)
    if u == "元" or u == "1":
        return round(val / 10000.0, 4)
    return round(val, 4)


def _compute_l2_flow_fields(
    ff_today: Mapping[str, Any] | None,
    ff_3d: Mapping[str, Any] | None,
    ff_5d: Mapping[str, Any] | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for prefix, row in (
        ("l2_today", ff_today),
        ("l2_3d", ff_3d),
        ("l2_5d", ff_5d),
    ):
        src = row if isinstance(row, Mapping) else {}
        unit = src.get("unit") if src.get("unit") is not None else src.get("fund_unit")
        main_net = _parse_flow_to_wan(src.get("main_net_inflow"), unit)
        visible = _parse_flow_to_wan(src.get("main_visible_inflow"), unit)
        hidden = _parse_flow_to_wan(src.get("main_hidden_inflow"), unit)
        retail = _parse_flow_to_wan(src.get("retail_inflow"), unit)
        out[f"{prefix}_main_net_wan"] = main_net
        out[f"{prefix}_visible_wan"] = visible
        out[f"{prefix}_hidden_wan"] = hidden
        out[f"{prefix}_retail_wan"] = retail
        denom = abs(visible or 0.0) + abs(hidden or 0.0)
        out[f"{prefix}_hidden_ratio_pct"] = (
            round(hidden / denom * 100.0, 2)
            if hidden is not None and denom > 0
            else None
        )
    return out


def _read_l2_period_maps(
    store: Any,
    symbol: str,
    trade_date: str | None,
) -> dict[str, dict[str, dict[str, Any]]]:
    maps: dict[str, dict[str, dict[str, Any]]] = {
        "today": {},
        "three_day": {},
        "five_day": {},
    }
    read_ff = getattr(store, "read_fund_flow_history", None)
    if not callable(read_ff):
        return maps
    try:
        ff_hist = read_ff(symbol, trade_date) if trade_date else read_ff(symbol)
    except Exception:
        return maps
    periods = ff_hist.get("periods") if isinstance(ff_hist, Mapping) else None
    if not isinstance(periods, Mapping):
        return maps
    for key in ("today", "three_day", "five_day"):
        pts = ((periods.get(key) or {}).get("points") or []) if isinstance(periods.get(key), Mapping) else []
        for pt in pts:
            if isinstance(pt, Mapping) and pt.get("time"):
                maps[key][str(pt["time"])] = dict(pt)
    return maps


def enrich_points_with_l2_periods(
    store: Any,
    symbol: str,
    trade_date: str | None,
    points: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach unit-normalized today/3d/5d L2 main/visible/hidden/retail fields onto minute points."""
    if not points:
        return points
    period_maps = _read_l2_period_maps(store, symbol, trade_date)
    t_map = period_maps["today"]
    d3_map = period_maps["three_day"]
    d5_map = period_maps["five_day"]
    out: list[dict[str, Any]] = []
    for pt in points:
        tm = str(pt.get("time") or "")
        today_row = t_map.get(tm)
        if not today_row and any(
            pt.get(k) is not None
            for k in ("main_net_inflow", "main_visible_inflow", "main_hidden_inflow", "retail_inflow")
        ):
            today_row = {
                "unit": pt.get("fund_unit") or pt.get("unit"),
                "main_net_inflow": pt.get("main_net_inflow"),
                "main_visible_inflow": pt.get("main_visible_inflow"),
                "main_hidden_inflow": pt.get("main_hidden_inflow"),
                "retail_inflow": pt.get("retail_inflow"),
            }
        l2_fields = _compute_l2_flow_fields(today_row, d3_map.get(tm), d5_map.get(tm))
        merged = {**pt, **l2_fields}
        if today_row:
            merged.setdefault("main_net_inflow", today_row.get("main_net_inflow"))
            merged.setdefault("main_visible_inflow", today_row.get("main_visible_inflow"))
            merged.setdefault("main_hidden_inflow", today_row.get("main_hidden_inflow"))
            merged.setdefault("retail_inflow", today_row.get("retail_inflow"))
        out.append(merged)
    return out


def _resolve_etf_flow_db_path(
    store: Any,
    explicit_path: Path | str | None = None,
) -> Path | None:
    if explicit_path is not None:
        p = Path(explicit_path).expanduser()
        return p if p.exists() else None
    attr_path = getattr(store, "etf_flow_db_path", None)
    if attr_path:
        p = Path(attr_path).expanduser()
        if p.exists():
            return p
    store_path = getattr(store, "path", None)
    if store_path:
        p = Path(store_path).expanduser().parent / "etf-fund-flow.db"
        if p.exists():
            return p
    return None


def _load_etf_flow_cache(db_path: Path | None, symbol: str) -> dict[str, Any]:
    """Read-only lookup of summary, cumulative line series, and holdings from etf-fund-flow.db."""
    if db_path is None or not db_path.exists():
        return {}
    clean_sym = str(symbol or "").strip().upper()
    keys = [
        f"summary:{clean_sym}",
        f"line:{clean_sym}:250d",
        f"line:{clean_sym}:120d",
        f"line:{clean_sym}:60d",
        f"holdings:{clean_sym}",
    ]
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in keys)
        rows = conn.execute(
            f"SELECT cache_key, data_date, updated_at, payload_json FROM etf_flow_cache WHERE cache_key IN ({placeholders})",
            tuple(keys),
        ).fetchall()
    except sqlite3.Error:
        try:
            conn = sqlite3.connect(db_path, timeout=2.0)
            conn.row_factory = sqlite3.Row
            placeholders = ",".join("?" for _ in keys)
            rows = conn.execute(
                f"SELECT cache_key, data_date, updated_at, payload_json FROM etf_flow_cache WHERE cache_key IN ({placeholders})",
                tuple(keys),
            ).fetchall()
        except sqlite3.Error:
            return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    by_key: dict[str, Any] = {}
    for row in rows:
        try:
            by_key[str(row["cache_key"])] = {
                "data_date": str(row["data_date"] or "").strip() or None,
                "payload": json.loads(row["payload_json"]),
            }
        except Exception:
            continue

    line_rows: list[tuple[str, float, float | None]] = []
    for suffix in ("250d", "120d", "60d"):
        entry = by_key.get(f"line:{clean_sym}:{suffix}")
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if isinstance(payload, list) and payload:
            for item in payload:
                if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) < 3:
                    continue
                d_str = str(item[0] or "").replace("-", "").strip()[:8]
                flow_v = _number(item[2])
                scale_v = _number(item[3]) if len(item) >= 4 else None
                if _DATE_RE.fullmatch(d_str) and flow_v is not None:
                    line_rows.append((d_str, float(flow_v), float(scale_v) if scale_v is not None else None))
            if line_rows:
                break
    line_rows.sort(key=lambda r: r[0])

    summary_entry = by_key.get(f"summary:{clean_sym}")
    summary_payload = summary_entry.get("payload") if isinstance(summary_entry, dict) and isinstance(summary_entry.get("payload"), dict) else {}

    holdings_entry = by_key.get(f"holdings:{clean_sym}")
    holdings_raw = holdings_entry.get("payload") if isinstance(holdings_entry, dict) and isinstance(holdings_entry.get("payload"), list) else []
    top_holdings: list[dict[str, Any]] = []
    for h in holdings_raw[:8]:
        if not isinstance(h, Mapping):
            continue
        raw_net = _number(h.get("net_inflow_raw"))
        top_holdings.append(
            {
                "etf_code": str(h.get("etf_code") or "").strip(),
                "name": str(h.get("name") or "").strip(),
                "net_inflow_wan": round(raw_net / 10000.0, 2) if raw_net is not None else None,
                "streak_days": h.get("streak_days"),
                "hold_rate": h.get("hold_rate"),
            }
        )

    return {
        "symbol": clean_sym,
        "summary": summary_payload,
        "line": line_rows,
        "holdings": top_holdings,
    }


def _causal_etf_flow_metrics(
    cache_data: Mapping[str, Any],
    target_td: str,
) -> dict[str, Any]:
    """Return strictly T-1 causal ETF net-buy inflow metrics (< target_td)."""
    clean_td = str(target_td or "").replace("-", "").strip()[:8]
    if not clean_td:
        return {}
    line_rows = [
        item for item in (cache_data.get("line") or [])
        if str(item[0]) < clean_td
    ]
    summary = cache_data.get("summary") if isinstance(cache_data.get("summary"), Mapping) else {}
    sum_date = str(summary.get("stockEtfNetBuyFlowDate") or "").replace("-", "").strip()[:8]
    use_summary = bool(sum_date and sum_date < clean_td)

    ref_date: str | None = None
    f1d_yuan: float | None = None
    f3d_yuan: float | None = None
    f5d_yuan: float | None = None
    f20d_yuan: float | None = None
    scale_yuan: float | None = None

    if line_rows:
        ref_date = line_rows[-1][0]
        f1d_yuan = line_rows[-1][1]
        scale_yuan = next((r[2] for r in reversed(line_rows) if r[2] is not None and r[2] > 0), None)
        if len(line_rows) >= 3:
            f3d_yuan = sum(r[1] for r in line_rows[-3:])
        if len(line_rows) >= 5:
            f5d_yuan = sum(r[1] for r in line_rows[-5:])
        if len(line_rows) >= 20:
            f20d_yuan = sum(r[1] for r in line_rows[-20:])

    if use_summary:
        if ref_date is None or sum_date >= ref_date:
            ref_date = sum_date
            if f1d_yuan is None:
                f1d_yuan = _number(summary.get("stockEtfNetBuyFlow"))
        if f3d_yuan is None:
            f3d_yuan = _number(summary.get("stockEtfNetBuyFlow3d"))
        if f5d_yuan is None:
            f5d_yuan = _number(summary.get("stockEtfNetBuyFlow5d"))
        if f20d_yuan is None:
            f20d_yuan = _number(summary.get("stockEtfNetBuyFlow20d"))
        if scale_yuan is None:
            scale_yuan = (
                _number(summary.get("stockEtfScale"))
                or _number(summary.get("stockEtfFundScale"))
                or _number(summary.get("stock_etf_fund_scale"))
                or _number(summary.get("fundScale"))
            )

    ratio_pct = (
        round((f1d_yuan / scale_yuan) * 100.0, 4)
        if (f1d_yuan is not None and scale_yuan is not None and scale_yuan > 0)
        else None
    )
    return {
        "etf_flow_ref_date": ref_date,
        "etf_net_buy_1d_wan": round(f1d_yuan / 10000.0, 2) if f1d_yuan is not None else None,
        "etf_net_buy_3d_wan": round(f3d_yuan / 10000.0, 2) if f3d_yuan is not None else None,
        "etf_net_buy_5d_wan": round(f5d_yuan / 10000.0, 2) if f5d_yuan is not None else None,
        "etf_net_buy_20d_wan": round(f20d_yuan / 10000.0, 2) if f20d_yuan is not None else None,
        "etf_share_scale_yi": round(scale_yuan / 100000000.0, 4) if (scale_yuan is not None and scale_yuan > 0) else None,
        "etf_net_buy_ratio_pct": ratio_pct,
    }


def _causal_us_macro_day_fields(
    macro: V9MacroDataStore,
    td: str,
    prev_td: str | None,
) -> dict[str, Any]:
    """Fallback causal daily return computation from cached US/futures series."""
    out: dict[str, Any] = {}
    try:
        from .v9_plus_engine import _causal_daily_ov_and_nd, _parse_sh
    except Exception:
        return out
    has_prev = bool(prev_td and prev_td != td)
    t0930_ts = int(_parse_sh(td, "09:30").timestamp())
    prev0930_ts = int(_parse_sh(prev_td or td, "09:30").timestamp())
    series_loaders = (
        ("xop_change_pct", macro.load_xop_daily_rows),
        ("xle_change_pct", macro.load_xle_daily_rows),
        ("spy_change_pct", macro.load_spy_daily_rows),
        ("iwm_change_pct", macro.load_iwm_daily_rows),
        ("cl_change_pct", macro.load_cl_daily_rows),
        ("ng_change_pct", macro.load_ng_daily_rows),
    )
    cl_ov_val: float | None = None
    cl_ok = False
    ho_rows: list[list[Any]] = []
    for field_name, loader in series_loaders:
        try:
            rows = loader()
            if not rows:
                continue
            ts = [int(r[1]) for r in rows]
            ov, _, ci = _causal_daily_ov_and_nd(rows, ts, t0930_ts, prev0930_ts, has_prev)
            if ci >= 0:
                out[field_name] = round(float(ov), 3)
                if field_name == "cl_change_pct":
                    cl_ov_val = float(ov)
                    cl_ok = True
        except Exception:
            continue
    try:
        ho_rows = macro.load_ho_daily_rows()
        if ho_rows and cl_ok and cl_ov_val is not None:
            ho_ts = [int(r[1]) for r in ho_rows]
            ho_ov, _, ho_ci = _causal_daily_ov_and_nd(ho_rows, ho_ts, t0930_ts, prev0930_ts, has_prev)
            if ho_ci >= 0:
                out["ho_crack_change_pct"] = round(float(ho_ov) - cl_ov_val, 3)
    except Exception:
        pass
    return out


def _align_bar_for_minute(
    bars_by_time: Mapping[str, Mapping[str, Any]],
    sorted_times: Sequence[str],
    tm: str,
) -> Mapping[str, Any] | None:
    exact = bars_by_time.get(tm)
    if exact is not None:
        return exact
    if not sorted_times:
        return None
    pos = bisect_right(sorted_times, tm) - 1
    if pos >= 0:
        return bars_by_time.get(sorted_times[pos])
    return bars_by_time.get(sorted_times[0])


def _load_peer_intraday_lookup(
    store: Any,
    target_symbol: str,
    start_td: str,
    end_td: str | None,
) -> dict[str, dict[str, Any]]:
    peers = [sym for sym in _OIL_PEER_SYMBOLS if sym != target_symbol]
    if not peers:
        return {}
    out: dict[str, dict[str, Any]] = {}
    placeholders = ",".join("?" for _ in peers)
    with store._lock:
        d_sql = f"SELECT symbol, trade_date, pre_close FROM research_days WHERE symbol IN ({placeholders}) AND trade_date>=?"
        d_params: list[Any] = [*peers, start_td]
        if end_td:
            d_sql += " AND trade_date<=?"
            d_params.append(end_td)
        pre_map: dict[tuple[str, str], float] = {}
        for r in store._connection.execute(d_sql, tuple(d_params)).fetchall():
            pv = _number(r["pre_close"])
            if pv and pv > 0:
                pre_map[(str(r["symbol"]), str(r["trade_date"]))] = float(pv)

        p_sql = (
            f"SELECT symbol, trade_date, minute, price FROM research_points WHERE symbol IN ({placeholders}) AND trade_date>=?"
            + (" AND trade_date<=?" if end_td else "")
            + " ORDER BY trade_date ASC, REPLACE(minute, ':', '') ASC"
        )
        for r in store._connection.execute(p_sql, tuple(d_params)).fetchall():
            sym = str(r["symbol"])
            td = str(r["trade_date"])
            px = _number(r["price"])
            if px is None or px <= 0:
                continue
            slot = out.setdefault(td, {}).setdefault(sym, {"pre": pre_map.get((sym, td)), "pts": []})
            slot["pts"].append((str(r["minute"]), float(px)))
    return out


class QuantPackEnricher:
    """Precomputed causal enricher attaching 5 data packs onto day/minute points."""

    def __init__(
        self,
        store: Any,
        symbol: str,
        *,
        packs: Sequence[str],
        start_date: str,
        end_date: str | None = None,
        allow_network: bool | None = None,
        etf_flow_db_path: Path | str | None = None,
    ) -> None:
        self.store = store
        self.symbol = symbol
        self.packs = set(normalize_requested_data_packs(packs))
        env_offline = os.environ.get("THS_QUANT_OFFLINE", "").strip() in {"1", "true", "TRUE", "yes"}
        self.allow_network = False if env_offline else bool(allow_network)
        self.macro: V9MacroDataStore | None = None
        self.brent_days_by_td: dict[str, dict[str, Any]] = {}
        self.xop_days_by_td: dict[str, dict[str, Any]] = {}
        self.dual_days_by_td: dict[str, dict[str, Any]] = {}
        self.es_series: tuple[list[int], list[float]] = ([], [])
        self.sc_daily: list[list[Any]] = []
        self.sc_min: tuple[list[int], list[float]] = ([], [])
        self.peer_lookup: dict[str, dict[str, Any]] = {}
        self.etf_cache: dict[str, Any] = {}
        self.nav_map: dict[str, float] = {}
        self.nav_dates: list[str] = []
        self.cnh_series: tuple[list[int], list[float]] = ([], [])
        self.us_oil_rows: dict[str, list[list[Any]]] = {}
        self.trade_dates_ordered: list[str] = []
        self._prev_fx: float | None = None

        if symbol != "BRN0Y" and self.packs & {"iopv_fx", "uso_bno_sc_peer"}:
            try:
                with store._lock:
                    d_rows = store._connection.execute(
                        "SELECT trade_date FROM research_days WHERE symbol=? ORDER BY trade_date ASC",
                        (symbol,),
                    ).fetchall()
                self.trade_dates_ordered = [str(r["trade_date"]) for r in d_rows if r["trade_date"]]
            except Exception:
                self.trade_dates_ordered = []

        if symbol != "BRN0Y" and self.packs & {"iopv_fx", "xop_us_ep", "uso_bno_sc_peer"}:
            try:
                self.macro = V9MacroDataStore(store, allow_network=self.allow_network)
            except Exception:
                self.macro = None

        if symbol != "BRN0Y" and "iopv_fx" in self.packs:
            try:
                v9_days, _ = build_v9_plus_days(
                    store,
                    symbol=symbol,
                    start_date=start_date,
                    end_date=end_date,
                    allow_network=self.allow_network,
                    cross_market_anchor="brent",
                )
                self.brent_days_by_td = {str(d["td"]): d for d in v9_days if d.get("td")}
            except Exception:
                self.brent_days_by_td = {}
            if self.macro is not None:
                try:
                    self.nav_map = self.macro.load_nav_map(symbol)
                    self.nav_dates = sorted(str(k) for k in self.nav_map)
                except Exception:
                    self.nav_map, self.nav_dates = {}, []
                try:
                    self.cnh_series = load_merged_fx_cnh_series(self.macro)
                except Exception:
                    self.cnh_series = ([], [])

        if symbol != "BRN0Y" and "xop_us_ep" in self.packs:
            try:
                xop_days, _ = build_v9_plus_days(
                    store,
                    symbol=symbol,
                    start_date=start_date,
                    end_date=end_date,
                    allow_network=self.allow_network,
                    cross_market_anchor="xop_us_ep",
                )
                self.xop_days_by_td = {str(d["td"]): d for d in xop_days if d.get("td")}
            except Exception:
                self.xop_days_by_td = {}
            if self.macro is not None:
                try:
                    self.es_series = self.macro.load_fut_1m_series("ES")
                except Exception:
                    self.es_series = ([], [])

        if symbol != "BRN0Y" and "uso_bno_sc_peer" in self.packs:
            try:
                dual_days, _ = build_v9_plus_days(
                    store,
                    symbol=symbol,
                    start_date=start_date,
                    end_date=end_date,
                    allow_network=self.allow_network,
                    cross_market_anchor="uso_bno_sc_peer",
                )
                self.dual_days_by_td = {str(d["td"]): d for d in dual_days if d.get("td")}
            except Exception:
                self.dual_days_by_td = {}
            if self.macro is not None:
                try:
                    self.sc_daily = self.macro.load_sc_daily_rows()
                    self.sc_min = self.macro.load_sc_min_series()
                except Exception:
                    self.sc_daily, self.sc_min = [], ([], [])
                for oil_k, loader_fn in (
                    ("uso", self.macro.load_uso_daily_rows),
                    ("bno", self.macro.load_bno_daily_rows),
                    ("dbo", lambda: load_dbo_daily_rows(self.macro)),
                ):
                    try:
                        self.us_oil_rows[oil_k] = loader_fn()
                    except Exception:
                        self.us_oil_rows[oil_k] = []
            try:
                self.peer_lookup = _load_peer_intraday_lookup(store, symbol, start_date, end_date)
            except Exception:
                self.peer_lookup = {}

        if "etf_redemption_flow" in self.packs:
            db_p = _resolve_etf_flow_db_path(store, etf_flow_db_path)
            self.etf_cache = _load_etf_flow_cache(db_p, symbol)

    def _causal_official_nav_for_day(
        self,
        td: str,
        prev_td: str | None,
        prev_prev_td: str | None = None,
    ) -> tuple[float | None, str | None]:
        if not self.nav_dates or not self.nav_map:
            return None, None
        eff_prev = prev_td
        if eff_prev is None and td in self.trade_dates_ordered:
            pos = self.trade_dates_ordered.index(td)
            if pos >= 1:
                eff_prev = self.trade_dates_ordered[pos - 1]
        if self.symbol in _OIL_QDII_NAV_SYMBOLS:
            cutoff = prev_prev_td
            if cutoff is None and td in self.trade_dates_ordered:
                pos = self.trade_dates_ordered.index(td)
                if pos >= 2:
                    cutoff = self.trade_dates_ordered[pos - 2]
                elif pos == 1:
                    cutoff = self.trade_dates_ordered[0]
            if cutoff is None:
                cutoff = eff_prev
        else:
            cutoff = eff_prev or (datetime.strptime(td, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
        if not cutoff:
            return None, None
        nav_td = next((k for k in reversed(self.nav_dates) if k <= cutoff), None)
        if not nav_td:
            return None, None
        raw_v = _number(self.nav_map.get(nav_td))
        return (round(raw_v, 4) if raw_v and raw_v > 0 else None), nav_td

    def _causal_us_oil_ov_for_day(self, td: str, prev_td: str | None) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        eff_prev = prev_td
        if eff_prev is None and td in self.trade_dates_ordered:
            pos = self.trade_dates_ordered.index(td)
            if pos >= 1:
                eff_prev = self.trade_dates_ordered[pos - 1]
        has_prev = bool(eff_prev and eff_prev != td)
        try:
            t0930_ts = int(_parse_sh(td, "09:30").timestamp())
            prev0930_ts = int(_parse_sh(eff_prev or td, "09:30").timestamp())
        except Exception:
            return out
        ov_vals: dict[str, float] = {}
        for key in ("uso", "bno", "dbo"):
            rows = self.us_oil_rows.get(key) or []
            if not rows:
                continue
            try:
                ts = [int(r[1]) for r in rows]
                ov, _, ci = _causal_daily_ov_and_nd(rows, ts, t0930_ts, prev0930_ts, has_prev)
                if ci >= 0:
                    rounded = round(float(ov), 3)
                    out[f"{key}_ov_pct"] = rounded
                    ov_vals[key] = float(ov)
            except Exception:
                continue
        if all(k in ov_vals for k in ("uso", "bno", "dbo")):
            out["fof_3oil_ov_pct"] = round(
                0.35 * ov_vals["uso"] + 0.35 * ov_vals["bno"] + 0.30 * ov_vals["dbo"], 3
            )
        return out

    def _sc_fallback_for_day(
        self,
        td: str,
        pts: Sequence[Mapping[str, Any]],
        brent_bars_by_tm: Mapping[str, Mapping[str, Any]],
        brent_tms: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        res_by_tm: dict[str, dict[str, Any]] = {}
        sc_min_ts, sc_min_px = self.sc_min
        if not self.sc_daily and not sc_min_ts:
            return res_by_tm
        sc_prev = None
        for row in self.sc_daily:
            rd = str(row[0]).replace("-", "")[:8]
            if rd < td:
                sc_prev = _number(row[2])
        if sc_min_ts:
            sc_prev = sc_prev or _val_at(sc_min_ts, sc_min_px, _parse_sh(td, "09:29"))
        sc_open = _val_at(sc_min_ts, sc_min_px, _parse_sh(td, "09:30")) if sc_min_ts else None
        for pt in pts:
            tm = str(pt.get("time") or "")
            if not tm:
                continue
            sc_now = _val_at(sc_min_ts, sc_min_px, _parse_sh(td, tm)) if sc_min_ts else None
            if sc_now is None and self.sc_daily:
                # fallback to causal prior SC daily close
                for row in reversed(self.sc_daily):
                    if str(row[0]).replace("-", "")[:8] < td:
                        sc_now = _number(row[2])
                        break
            if sc_now is None:
                continue
            sc_vs_prev = round((sc_now / sc_prev - 1.0) * 100.0, 3) if (sc_prev and sc_prev > 0) else 0.0
            base_open = sc_open or sc_prev or sc_now
            sc_intra = round((sc_now / base_open - 1.0) * 100.0, 3) if base_open > 0 else 0.0
            b_bar = _align_bar_for_minute(brent_bars_by_tm, brent_tms, tm)
            brn_chg = _number(b_bar.get("brn_chg")) if b_bar else _number(pt.get("brn_from_prev_close_pct"))
            item: dict[str, Any] = {
                "sc0_change_pct": sc_vs_prev,
                "sc0_intraday_pct": sc_intra,
            }
            if brn_chg is not None:
                item["sc_brent_spread_pct"] = round(sc_vs_prev - brn_chg, 3)
            res_by_tm[tm] = item
        return res_by_tm

    def enrich_day(
        self,
        td: str,
        prev_td: str | None,
        points: list[dict[str, Any]],
        *,
        pre_close: float | None = None,
        prev_prev_td: str | None = None,
    ) -> dict[str, Any]:
        """Mutate points in place with selected pack fields and return day-level summary."""
        daily_summary: dict[str, Any] = {}
        if not points:
            return daily_summary

        # 1. iopv_fx
        brent_day = self.brent_days_by_td.get(td) or {}
        brent_bars = {str(b["time"]): b for b in (brent_day.get("bars") or []) if b.get("time")}
        brent_tms = sorted(brent_bars)
        day_pre = pre_close if (pre_close and pre_close > 0) else _number(brent_day.get("pre"))
        if "iopv_fx" in self.packs:
            official_nav, official_nav_date = self._causal_official_nav_for_day(td, prev_td, prev_prev_td)
            cnh_ts, cnh_px = self.cnh_series
            if official_nav is not None:
                for pt in points:
                    pt["official_nav"] = official_nav
                daily_summary["official_nav"] = official_nav
                daily_summary["official_nav_date"] = official_nav_date
            if cnh_ts and cnh_px:
                for pt in points:
                    tm = str(pt.get("time") or "")
                    if not tm:
                        continue
                    try:
                        cnh_v = _val_at(cnh_ts, cnh_px, _parse_sh(td, tm))
                        if cnh_v and cnh_v > 0:
                            pt["usdcnh"] = round(float(cnh_v), 5)
                    except Exception:
                        pass
                if points[-1].get("usdcnh") is not None:
                    daily_summary["usdcnh_close"] = points[-1].get("usdcnh")

        if "iopv_fx" in self.packs and brent_bars:
            first_bar = brent_bars[brent_tms[0]]
            fx_base = self._prev_fx if (self._prev_fx and self._prev_fx > 0) else (_number(first_bar.get("fx")) or 0.0)
            last_bar = brent_bars[brent_tms[-1]]
            last_fx = _number(last_bar.get("fx"))
            if last_fx and last_fx > 0:
                self._prev_fx = last_fx
            for pt in points:
                tm = str(pt.get("time") or "")
                bar = _align_bar_for_minute(brent_bars, brent_tms, tm)
                if not bar:
                    continue
                px_pt = _number(pt.get("price"))
                iopv_val = _number(bar.get("iopv"))
                fx_val = _number(bar.get("fx"))
                prem_val = (
                    round((px_pt / iopv_val - 1.0) * 100.0, 3)
                    if (px_pt and px_pt > 0 and iopv_val and iopv_val > 0)
                    else (round(float(bar["prem"]), 3) if _number(bar.get("prem")) is not None else None)
                )
                pt["iopv_fx"] = round(iopv_val, 4) if iopv_val is not None else None
                pt["iopv_premium_pct"] = prem_val
                pt["sp_fx_pct"] = round(float(bar["sp_fx"]), 3) if bar.get("sp_fx") is not None else None
                pt["coint10_pct"] = round(float(bar["coint10"]), 3) if bar.get("coint10") is not None else None
                pt["usdcny"] = round(fx_val, 5) if fx_val is not None else None
                if pt.get("usdcnh") is None and fx_val is not None:
                    pt["usdcnh"] = round(fx_val, 5)
                pt["usdcny_change_pct"] = (
                    round((fx_val - fx_base) / fx_base * 100.0, 3)
                    if (fx_val is not None and fx_base > 0)
                    else None
                )
                pt["fx_adjusted_nav_change_pct"] = (
                    round((iopv_val - day_pre) / day_pre * 100.0, 3)
                    if (iopv_val is not None and day_pre and day_pre > 0)
                    else None
                )
                pt["brent_q2y_pct"] = (
                    round(float(bar["brn_pos"]) * 100.0, 2)
                    if bar.get("brn_pos") is not None
                    else None
                )
            close_pt = points[-1]
            daily_summary.update(
                {
                    "iopv_fx_close": close_pt.get("iopv_fx"),
                    "iopv_premium_close_pct": close_pt.get("iopv_premium_pct"),
                    "sp_fx_close_pct": close_pt.get("sp_fx_pct"),
                    "coint10_close_pct": close_pt.get("coint10_pct"),
                    "usdcny_close": close_pt.get("usdcny"),
                    "usdcnh_close": daily_summary.get("usdcnh_close") or close_pt.get("usdcnh"),
                    "brent_q2y_close_pct": close_pt.get("brent_q2y_pct"),
                }
            )

        # 2. xop_us_ep
        if "xop_us_ep" in self.packs:
            xop_day = self.xop_days_by_td.get(td) or {}
            xop_bars = {str(b["time"]): b for b in (xop_day.get("bars") or []) if b.get("time")}
            xop_tms = sorted(xop_bars)
            day_macro_fallback = (
                _causal_us_macro_day_fields(self.macro, td, prev_td)
                if (not xop_day and self.macro is not None)
                else {}
            )
            ov_fields = {
                "xop_change_pct": round(float(xop_day["xop_ov"]), 3) if xop_day.get("xop_ov") is not None else day_macro_fallback.get("xop_change_pct"),
                "xle_change_pct": round(float(xop_day["xle_ov"]), 3) if xop_day.get("xle_ov") is not None else day_macro_fallback.get("xle_change_pct"),
                "spy_change_pct": round(float(xop_day["spy_ov"]), 3) if xop_day.get("spy_ov") is not None else day_macro_fallback.get("spy_change_pct"),
                "iwm_change_pct": round(float(xop_day["iwm_ov"]), 3) if xop_day.get("iwm_ov") is not None else day_macro_fallback.get("iwm_change_pct"),
                "cl_change_pct": round(float(xop_day["cl_ov"]), 3) if xop_day.get("cl_ov") is not None else day_macro_fallback.get("cl_change_pct"),
                "ho_crack_change_pct": round(float(xop_day["crack_ov"]), 3) if xop_day.get("crack_ov") is not None else day_macro_fallback.get("ho_crack_change_pct"),
                "ng_change_pct": round(float(xop_day["ng_ov"]), 3) if xop_day.get("ng_ov") is not None else day_macro_fallback.get("ng_change_pct"),
            }
            es_ts, es_px = self.es_series
            es_prev_px: float | None = None
            if es_ts:
                try:
                    ref_dt = _parse_sh(prev_td, "15:00") if prev_td else _parse_sh(td, "09:30")
                    es_prev_px = _val_at(es_ts, es_px, ref_dt)
                except Exception:
                    es_prev_px = None
            for pt in points:
                tm = str(pt.get("time") or "")
                for fk, fv in ov_fields.items():
                    if fv is not None:
                        pt[fk] = fv
                bar = _align_bar_for_minute(xop_bars, xop_tms, tm)
                if bar:
                    px_pt = _number(pt.get("price"))
                    x_iopv = _number(bar.get("iopv"))
                    pt["xop_synthetic_premium_pct"] = (
                        round((px_pt / x_iopv - 1.0) * 100.0, 3)
                        if (px_pt and px_pt > 0 and x_iopv and x_iopv > 0)
                        else (_number(bar.get("prem")) and round(float(bar["prem"]), 3))
                    )
                    if bar.get("daytime_lead_pct") is not None:
                        pt["daytime_lead_pct"] = round(float(bar["daytime_lead_pct"]), 3)
                    if bar.get("daytime_crack_pct") is not None:
                        pt["daytime_crack_pct"] = round(float(bar["daytime_crack_pct"]), 3)
                if es_ts and es_prev_px and es_prev_px > 0:
                    try:
                        es_now = _val_at(es_ts, es_px, _parse_sh(td, tm))
                        if es_now is not None:
                            pt["es_change_pct"] = round((es_now - es_prev_px) / es_prev_px * 100.0, 3)
                    except Exception:
                        pass
            daily_summary.update(
                {
                    "xop_ov_pct": ov_fields["xop_change_pct"],
                    "xle_ov_pct": ov_fields["xle_change_pct"],
                    "spy_ov_pct": ov_fields["spy_change_pct"],
                    "iwm_ov_pct": ov_fields["iwm_change_pct"],
                    "cl_ov_pct": ov_fields["cl_change_pct"],
                    "ho_crack_ov_pct": ov_fields["ho_crack_change_pct"],
                    "ng_ov_pct": ov_fields["ng_change_pct"],
                }
            )

        # 3. uso_bno_sc_peer
        if "uso_bno_sc_peer" in self.packs:
            dual_day = self.dual_days_by_td.get(td) or {}
            dual_bars = {str(b["time"]): b for b in (dual_day.get("bars") or []) if b.get("time")}
            dual_tms = sorted(dual_bars)
            sc_fallback = (
                {}
                if dual_bars
                else self._sc_fallback_for_day(td, points, brent_bars, brent_tms)
            )
            peers_day = self.peer_lookup.get(td) or {}
            peer_cursors: dict[str, dict[str, Any]] = {
                sym: {"pre": info.get("pre"), "pts": info.get("pts") or [], "idx": 0, "last": None}
                for sym, info in peers_day.items()
            }
            dual_ov = _number(dual_day.get("dual_ov"))
            us_oil_ov = self._causal_us_oil_ov_for_day(td, prev_td)
            for pt in points:
                tm = str(pt.get("time") or "")
                if dual_ov is not None:
                    pt["uso_bno_change_pct"] = round(dual_ov, 3)
                if us_oil_ov.get("uso_ov_pct") is not None:
                    pt["uso_change_pct"] = us_oil_ov["uso_ov_pct"]
                if us_oil_ov.get("bno_ov_pct") is not None:
                    pt["bno_change_pct"] = us_oil_ov["bno_ov_pct"]
                if us_oil_ov.get("dbo_ov_pct") is not None:
                    pt["dbo_change_pct"] = us_oil_ov["dbo_ov_pct"]
                if us_oil_ov.get("fof_3oil_ov_pct") is not None:
                    pt["fof_3oil_change_pct"] = us_oil_ov["fof_3oil_ov_pct"]
                dbar = _align_bar_for_minute(dual_bars, dual_tms, tm)
                if dbar:
                    px_pt = _number(pt.get("price"))
                    d_iopv = _number(dbar.get("dual_iopv") or dbar.get("iopv"))
                    pt["dual_oil_premium_pct"] = (
                        round((px_pt / d_iopv - 1.0) * 100.0, 3)
                        if (px_pt and px_pt > 0 and d_iopv and d_iopv > 0)
                        else (_number(dbar.get("dual_prem")) and round(float(dbar["dual_prem"]), 3))
                    )
                    if dbar.get("sc_vs_prev") is not None:
                        pt["sc0_change_pct"] = round(float(dbar["sc_vs_prev"]), 3)
                    if dbar.get("sc_intra") is not None:
                        pt["sc0_intraday_pct"] = round(float(dbar["sc_intra"]), 3)
                    sc_v = _number(dbar.get("sc_vs_prev"))
                    brn_v = _number(dbar.get("brn_chg"))
                    if sc_v is not None and brn_v is not None:
                        pt["sc_brent_spread_pct"] = round(sc_v - brn_v, 3)
                    if dbar.get("peer_chg") is not None:
                        pt["peer_change_pct"] = round(float(dbar["peer_chg"]), 3)
                    if dbar.get("peer_sp") is not None:
                        pt["peer_spread_pct"] = round(float(dbar["peer_sp"]), 3)
                    if dbar.get("rel_prem") is not None:
                        pt["peer_premium_diff_pct"] = round(float(dbar["rel_prem"]), 3)
                elif tm in sc_fallback:
                    for k, v in sc_fallback[tm].items():
                        pt[k] = v

                # Causal per-peer spread (from research_points)
                self_chg = _number(pt.get("change_pct"))
                if self_chg is None and day_pre and day_pre > 0 and _number(pt.get("price")) is not None:
                    self_chg = (float(pt["price"]) - day_pre) / day_pre * 100.0
                peer_chgs_here: list[float] = []
                for p_sym, st in peer_cursors.items():
                    p_pts = st["pts"]
                    idx_p = st["idx"]
                    while idx_p < len(p_pts) and p_pts[idx_p][0] <= tm:
                        st["last"] = p_pts[idx_p][1]
                        idx_p += 1
                    st["idx"] = idx_p
                    p_pre = _number(st.get("pre"))
                    if st["last"] is not None and p_pre and p_pre > 0:
                        p_chg = round((float(st["last"]) - p_pre) / p_pre * 100.0, 3)
                        pt[f"peer_{p_sym}_change_pct"] = p_chg
                        if self_chg is not None:
                            pt[f"peer_{p_sym}_spread_pct"] = round(self_chg - p_chg, 3)
                        peer_chgs_here.append(p_chg)
                if peer_chgs_here and pt.get("peer_change_pct") is None:
                    mean_peer = round(statistics.mean(peer_chgs_here), 3)
                    pt["peer_change_pct"] = mean_peer
                    if self_chg is not None:
                        pt["peer_spread_pct"] = round(self_chg - mean_peer, 3)

            last_pt = points[-1]
            daily_summary.update(
                {
                    "uso_bno_ov_pct": round(dual_ov, 3) if dual_ov is not None else None,
                    "uso_ov_pct": us_oil_ov.get("uso_ov_pct"),
                    "bno_ov_pct": us_oil_ov.get("bno_ov_pct"),
                    "dbo_ov_pct": us_oil_ov.get("dbo_ov_pct"),
                    "fof_3oil_ov_pct": us_oil_ov.get("fof_3oil_ov_pct"),
                    "sc0_close_pct": last_pt.get("sc0_change_pct"),
                    "peer_close_spread_pct": last_pt.get("peer_spread_pct"),
                }
            )

        # 4. l2_darkpool_flow summary
        if "l2_darkpool_flow" in self.packs:
            last_pt = points[-1]
            daily_summary.update(
                {
                    "l2_today_main_net_wan": last_pt.get("l2_today_main_net_wan"),
                    "l2_today_visible_wan": last_pt.get("l2_today_visible_wan"),
                    "l2_today_hidden_wan": last_pt.get("l2_today_hidden_wan"),
                    "l2_today_retail_wan": last_pt.get("l2_today_retail_wan"),
                    "l2_3d_main_net_wan": last_pt.get("l2_3d_main_net_wan"),
                    "l2_5d_main_net_wan": last_pt.get("l2_5d_main_net_wan"),
                }
            )

        # 5. etf_redemption_flow (strict T-1)
        if "etf_redemption_flow" in self.packs and self.etf_cache:
            etf_meta = _causal_etf_flow_metrics(self.etf_cache, td)
            for pt in points:
                for k, v in etf_meta.items():
                    if k != "etf_flow_ref_date":
                        pt[k] = v
            daily_summary.update(etf_meta)

        return daily_summary


def enrich_live_points_with_quant_packs(
    store: Any,
    symbol: str,
    trade_date: str,
    points: list[dict[str, Any]],
    *,
    packs: Sequence[str] | set[str],
    allow_network: bool | None = None,
    etf_flow_db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Enrich live intraday polling points symmetrically with historical backtest packs."""
    eff_packs = set(normalize_requested_data_packs(list(packs)))
    if not points or not eff_packs:
        return points
    working = [dict(pt) for pt in points]
    if "l2_darkpool_flow" in eff_packs:
        working = enrich_points_with_l2_periods(store, symbol, trade_date, working)
    cross_or_etf = eff_packs & {"iopv_fx", "xop_us_ep", "uso_bno_sc_peer", "etf_redemption_flow"}
    if cross_or_etf:
        pre_close = None
        try:
            with store._lock:
                row = store._connection.execute(
                    "SELECT pre_close FROM research_days WHERE symbol=? AND trade_date=?",
                    (symbol, trade_date),
                ).fetchone()
                prev_row = store._connection.execute(
                    "SELECT MAX(trade_date) AS td FROM research_days WHERE symbol=? AND trade_date<?",
                    (symbol, trade_date),
                ).fetchone()
            pre_close = _number(row["pre_close"]) if row else None
            prev_td = str(prev_row["td"]) if (prev_row and prev_row["td"]) else None
        except Exception:
            prev_td = None
        enricher = QuantPackEnricher(
            store,
            symbol,
            packs=sorted(cross_or_etf),
            start_date=trade_date,
            end_date=trade_date,
            allow_network=allow_network,
            etf_flow_db_path=etf_flow_db_path,
        )
        enricher.enrich_day(trade_date, prev_td, working, pre_close=pre_close)
    return working


def _enrich_day_points(
    store: Any,
    symbol: str,
    trade_date: str,
    raw_rows: Sequence[sqlite3.Row],
    pre_close: float | None,
    macd_settings: Mapping[str, int | float],
) -> tuple[list[dict[str, Any]], bool]:
    prices = [float(r["price"]) if r["price"] is not None else 0.0 for r in raw_rows]
    macd_rows = calculate_macd(prices, macd_settings)
    period_maps = _read_l2_period_maps(store, symbol, trade_date)
    ff_by_time = period_maps["today"]
    ff_3d_by_time = period_maps["three_day"]
    ff_5d_by_time = period_maps["five_day"]

    flow_count = 0
    points: list[dict[str, Any]] = []
    for idx, r in enumerate(raw_rows):
        t = str(r["minute"])
        ff = ff_by_time.get(t, {})
        l2_fields = _compute_l2_flow_fields(
            ff_by_time.get(t),
            ff_3d_by_time.get(t),
            ff_5d_by_time.get(t),
        )
        p_val = prices[idx]
        chg_pct = (
            round((p_val - pre_close) / pre_close * 100.0, 4)
            if pre_close and pre_close > 0 and r["price"] is not None
            else None
        )
        has_flow_here = (
            r["large_order_net"] is not None
            or r["large_order_amount"] is not None
            or ff.get("main_net_inflow") is not None
            or ff.get("main_visible_inflow") is not None
            or ff.get("main_hidden_inflow") is not None
            or ff.get("retail_inflow") is not None
        )
        if has_flow_here:
            flow_count += 1
        points.append(
            {
                "time": t,
                "price": r["price"],
                "average_price": r["average_price"],
                "volume": r["volume"],
                "change_pct": chg_pct,
                "large_order_net": r["large_order_net"],
                "large_order_amount": r["large_order_amount"],
                "retail_count": r["retail_count"],
                "macdfs": r["macdfs"],
                "fund_unit": ff.get("unit"),
                "main_net_inflow": ff.get("main_net_inflow"),
                "main_visible_inflow": ff.get("main_visible_inflow"),
                "main_hidden_inflow": ff.get("main_hidden_inflow"),
                "retail_inflow": ff.get("retail_inflow"),
                **l2_fields,
                **macd_rows[idx],
            }
        )
    return points, flow_count >= 50


def _bucket_session(hhmm: str) -> str:
    compact = hhmm.replace(":", "")
    if compact < "1000":
        return "09:30-10:00 开盘脉冲"
    if compact <= "1130":
        return "10:00-11:30 上午盘"
    if compact < "1400":
        return "13:00-14:00 午后盘"
    return "14:00-15:00 尾盘"


def _bucket_vwap(dev_pct: float | None) -> str:
    if dev_pct is None:
        return "无均价基线"
    if dev_pct <= -0.10:
        return "均线下方 (≤-0.10%)"
    if dev_pct >= 0.10:
        return "均线上方 (≥+0.10%)"
    return "贴近均线 (-0.10%~+0.10%)"


def _bucket_macd_zero(diff_val: float | None) -> str:
    if diff_val is None:
        return "未知"
    return "零轴下方 (DIFF≤0)" if diff_val <= 0 else "零轴上方 (DIFF>0)"


def _bucket_hist_strength(macd_val: float | None) -> str:
    if macd_val is None:
        return "未知"
    a = abs(macd_val)
    if a < 0.0005:
        return "微弱粘合 (<0.0005)"
    if a < 0.0015:
        return "中等交叉 (0.0005~0.0015)"
    return "强柱突破 (≥0.0015)"


def _bucket_vol_ratio(vr: float | None) -> str:
    if vr is None:
        return "基线不足"
    if vr < 0.8:
        return "缩量 (<0.8x)"
    if vr < 1.5:
        return "常态 (0.8x~1.5x)"
    return "放量 (≥1.5x)"


def _bucket_open_gap(gap_pct: float | None) -> str:
    if gap_pct is None:
        return "无昨收基线"
    if gap_pct < -0.5:
        return "低开 (<-0.5%)"
    if gap_pct > 0.5:
        return "高开 (>+0.5%)"
    return "平开 (-0.5%~+0.5%)"


def _bucket_brent_spread(spread_pct: float | None) -> str:
    if spread_pct is None:
        return "无布伦特基线"
    if spread_pct <= -0.5:
        return "A股折价滞涨 (≤-0.5%)"
    if spread_pct >= 0.5:
        return "A股溢价超涨 (≥+0.5%)"
    return "跨市场同步 (-0.5%~+0.5%)"


def load_brent_alignment_index(
    store: Any,
    start_date: str,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Load sorted BRN0Y minute timeline & 15:00 anchors for cross-market alignment."""
    clean_start = str(start_date or "20260701").replace("-", "").strip()
    try:
        lookback_start = (
            datetime.strptime(clean_start, "%Y%m%d") - timedelta(days=20)
        ).strftime("%Y%m%d")
    except ValueError:
        lookback_start = clean_start
    with store._lock:
        sql = (
            "SELECT trade_date, minute, price FROM research_points "
            "WHERE symbol='BRN0Y' AND trade_date>=?"
        )
        params: list[Any] = [lookback_start]
        if end_date:
            sql += " AND trade_date<=?"
            params.append(end_date)
        sql += " ORDER BY trade_date ASC, REPLACE(minute,':','') ASC"
        rows = store._connection.execute(sql, tuple(params)).fetchall()

    ts_keys: list[str] = []
    prices: list[float] = []
    brn_1500_by_day: dict[str, float] = {}
    brn_1500_dates: list[str] = []
    for r in rows:
        td = str(r["trade_date"])
        mm = str(r["minute"]).replace(":", "").zfill(4)
        p = _number(r["price"])
        if p is None or p <= 0 or len(mm) != 4:
            continue
        ts_keys.append(f"{td}{mm}")
        prices.append(float(p))
        if mm <= "1500":
            brn_1500_by_day[td] = float(p)
    brn_1500_dates = sorted(brn_1500_by_day)
    return {
        "ts_keys": ts_keys,
        "prices": prices,
        "brn_1500_by_day": brn_1500_by_day,
        "brn_1500_dates": brn_1500_dates,
    }


def _lookup_brent_at(brent_index: Mapping[str, Any], ts_key: str) -> float | None:
    keys: Sequence[str] = brent_index.get("ts_keys") or ()
    vals: Sequence[float] = brent_index.get("prices") or ()
    if not keys:
        return None
    pos = bisect_right(keys, ts_key) - 1
    if pos < 0:
        return None
    return float(vals[pos])


def enrich_points_with_brent(
    points: list[dict[str, Any]],
    trade_date: str,
    prev_trade_date: str | None,
    brent_index: Mapping[str, Any] | None,
) -> dict[str, float | None]:
    """Attach BRN0Y aligned price, overnight return, momentum, and spread onto A-share points."""
    if not brent_index or not (brent_index.get("ts_keys") or ()):
        return {"prev_1500": None, "open_0930": None, "close_1500": None, "overnight_pct": None}

    brn_1500_by_day: Mapping[str, float] = brent_index.get("brn_1500_by_day") or {}
    brn_1500_dates: Sequence[str] = brent_index.get("brn_1500_dates") or ()
    prev_1500: float | None = None
    if prev_trade_date and prev_trade_date in brn_1500_by_day:
        prev_1500 = float(brn_1500_by_day[prev_trade_date])
    elif brn_1500_dates:
        pos = bisect_right(brn_1500_dates, prev_trade_date or "") if prev_trade_date else bisect_right(brn_1500_dates, trade_date)
        # Ensure strictly before trade_date
        for idx in range(min(pos, len(brn_1500_dates)) - 1, -1, -1):
            if brn_1500_dates[idx] < trade_date:
                prev_1500 = float(brn_1500_by_day[brn_1500_dates[idx]])
                break

    open_0930 = _lookup_brent_at(brent_index, f"{trade_date}0930")
    close_1500 = _lookup_brent_at(brent_index, f"{trade_date}1500")
    overnight_pct = (
        round((open_0930 - prev_1500) / prev_1500 * 100.0, 4)
        if open_0930 and prev_1500 and prev_1500 > 0
        else None
    )
    base_day = datetime.strptime(trade_date, "%Y%m%d")

    for pt in points:
        raw_t = str(pt.get("time") or "").strip()
        compact = raw_t.replace(":", "")
        if len(compact) != 4 or not compact.isdigit():
            continue
        hh, mm = int(compact[:2]), int(compact[2:])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            continue
        dt_pt = base_day.replace(hour=hh, minute=mm)
        cur_b = _lookup_brent_at(brent_index, f"{trade_date}{compact}")
        b_5m = _lookup_brent_at(brent_index, (dt_pt - timedelta(minutes=5)).strftime("%Y%m%d%H%M"))
        b_15m = _lookup_brent_at(brent_index, (dt_pt - timedelta(minutes=15)).strftime("%Y%m%d%H%M"))
        from_prev = (
            round((cur_b - prev_1500) / prev_1500 * 100.0, 4)
            if cur_b and prev_1500 and prev_1500 > 0
            else None
        )
        intraday_b = (
            round((cur_b - open_0930) / open_0930 * 100.0, 4)
            if cur_b and open_0930 and open_0930 > 0
            else None
        )
        mom_5m = round((cur_b - b_5m) / b_5m * 100.0, 4) if cur_b and b_5m and b_5m > 0 else None
        mom_15m = round((cur_b - b_15m) / b_15m * 100.0, 4) if cur_b and b_15m and b_15m > 0 else None
        a_chg = _number(pt.get("change_pct"))
        spread = (
            round(a_chg - from_prev, 4)
            if a_chg is not None and from_prev is not None
            else None
        )
        pt["brn_price"] = round(cur_b, 3) if cur_b is not None else None
        pt["brn_overnight_pct"] = overnight_pct
        pt["brn_from_prev_close_pct"] = from_prev
        pt["brn_intraday_pct"] = intraday_b
        pt["brn_mom_5m_pct"] = mom_5m
        pt["brn_mom_15m_pct"] = mom_15m
        pt["brn_spread_pct"] = spread

    return {
        "prev_1500": round(prev_1500, 3) if prev_1500 else None,
        "open_0930": round(open_0930, 3) if open_0930 else None,
        "close_1500": round(close_1500, 3) if close_1500 else None,
        "overnight_pct": overnight_pct,
    }


def _summarize_side_horizon(
    items: Sequence[dict[str, Any]],
    *,
    side: str,
    field: str,
    min_move_pct: float,
) -> dict[str, Any]:
    vals = [float(x[field]) for x in items if x.get(field) is not None]
    total = len(vals)
    insufficient = len(items) - total
    if not total:
        return {
            "total": 0,
            "insufficient_tail": insufficient,
            "wins": 0,
            "win_rate_pct": None,
            "avg_price_change_pct": None,
            "median_price_change_pct": None,
            "avg_favorable_pct": None,
            "median_favorable_pct": None,
        }
    favorable = vals if side == "buy" else [-v for v in vals]
    wins = sum(1 for f in favorable if (f > 0 if min_move_pct <= 0 else f >= min_move_pct))
    return {
        "total": total,
        "insufficient_tail": insufficient,
        "wins": wins,
        "win_rate_pct": round(wins / total * 100.0, 2),
        "avg_price_change_pct": round(statistics.mean(vals), 4),
        "median_price_change_pct": round(statistics.median(vals), 4),
        "avg_favorable_pct": round(statistics.mean(favorable), 4),
        "median_favorable_pct": round(statistics.median(favorable), 4),
    }


def evaluate_quant_strategy(
    store: Any,
    rule_spec: Mapping[str, Any],
    *,
    symbol: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    primary_horizon_minutes: int | None = None,
    min_move_pct: float | None = None,
    data_scope: str | None = None,
    morning_start: str | None = None,
    morning_end: str | None = None,
    max_daily_trades: int | None = None,
    round_trip_fee_pct: float | None = None,
    capital_cap: float | None = None,
    signal_limit: int = 200,
) -> dict[str, Any]:
    """Run multi-day historical replay, base-position T+0 return, dynamic-share capital_sim, and attribution."""
    criteria = _normalize_eval_criteria(rule_spec.get("eval_criteria"))
    if start_date:
        cleaned_start = str(start_date).replace("-", "").strip()
        if _DATE_RE.fullmatch(cleaned_start):
            criteria["start_date"] = cleaned_start
    cleaned_end = None
    if end_date:
        cand_end = str(end_date).replace("-", "").strip()
        if _DATE_RE.fullmatch(cand_end):
            cleaned_end = cand_end
    if primary_horizon_minutes in _HORIZONS:
        criteria["primary_horizon_minutes"] = int(primary_horizon_minutes)
    if min_move_pct is not None:
        criteria["min_move_pct"] = max(0.0, min(5.0, float(min_move_pct)))
    if data_scope in {"auto", "all", "flow_only"}:
        criteria["data_scope"] = data_scope
    if morning_start:
        criteria["morning_start"] = _normalize_hhmm(morning_start, criteria["morning_start"])
    if morning_end:
        criteria["morning_end"] = _normalize_hhmm(morning_end, criteria["morning_end"])
    if criteria["morning_end"] < criteria["morning_start"]:
        criteria["morning_start"], criteria["morning_end"] = criteria["morning_end"], criteria["morning_start"]
    if max_daily_trades is not None:
        criteria["max_daily_trades"] = max(1, min(20, int(max_daily_trades)))
    if round_trip_fee_pct is not None:
        criteria["round_trip_fee_pct"] = round(max(0.0, min(1.0, float(round_trip_fee_pct))), 4)
    if capital_cap is not None:
        criteria["capital_cap"] = round(max(1000.0, min(10_000_000.0, float(capital_cap))), 2)

    pos_sizing = normalize_position_sizing(rule_spec.get("position_sizing"))

    target_symbol = _normalize_quant_symbol(
        str(symbol or (rule_spec.get("symbols") or ["160723"])[0] or "160723")
    )
    macd_settings = _normalize_macd_settings(rule_spec.get("macd_settings"))
    threshold = float(macd_settings["marker_threshold"])

    rules: list[dict[str, Any]] = []
    if rule_spec.get("buy_rule"):
        buy_r = _normalize_side_rule(
            rule_spec.get("buy_rule"),
            side="buy",
            default_id=f"{rule_spec.get('id', 'rule')}-buy",
            default_title="买点",
        )
        if buy_r:
            rules.append({**buy_r, "enabled": True})
    if rule_spec.get("sell_rule"):
        sell_r = _normalize_side_rule(
            rule_spec.get("sell_rule"),
            side="sell",
            default_id=f"{rule_spec.get('id', 'rule')}-sell",
            default_title="卖点",
        )
        if sell_r:
            rules.append({**sell_r, "enabled": True})
    if not rules:
        raise ValueError("strategy must contain at least one buy_rule or sell_rule")
    validate_quant_rule_spec(
        rule_spec.get("buy_rule"),
        rule_spec.get("sell_rule"),
        pos_sizing,
    )

    if pos_sizing.get("engine_profile") in {"v9_plus_aggressive", "v9_plus_balanced"} and target_symbol != "BRN0Y":
        return evaluate_v9_plus_backtest(
            store,
            {
                "id": rule_spec.get("id"),
                "macd_settings": macd_settings,
            },
            criteria,
            pos_sizing,
            symbol=target_symbol,
            cleaned_end=cleaned_end,
            signal_limit=signal_limit,
        )

    requires_flow = rule_uses_flow(rules)
    effective_scope = criteria["data_scope"]
    if effective_scope == "auto":
        effective_scope = "flow_only" if requires_flow else "all"

    with store._lock:
        sql = "SELECT * FROM research_days WHERE symbol=? AND trade_date>=?"
        params: list[Any] = [target_symbol, criteria["start_date"]]
        if cleaned_end:
            sql += " AND trade_date<=?"
            params.append(cleaned_end)
        sql += " ORDER BY trade_date ASC"
        day_rows = store._connection.execute(sql, tuple(params)).fetchall()

        prev_row = store._connection.execute(
            "SELECT MAX(trade_date) AS td FROM research_days WHERE symbol=? AND trade_date<?",
            (target_symbol, criteria["start_date"]),
        ).fetchone()
        initial_prev_td = str(prev_row["td"]) if prev_row and prev_row["td"] else None

        day_points_map: dict[str, list[sqlite3.Row]] = {}
        if day_rows:
            pt_sql = (
                "SELECT * FROM research_points WHERE symbol=? AND trade_date>=?"
                + (" AND trade_date<=?" if cleaned_end else "")
                + " ORDER BY trade_date ASC, REPLACE(minute,':','') ASC"
            )
            for row in store._connection.execute(pt_sql, tuple(params)).fetchall():
                day_points_map.setdefault(str(row["trade_date"]), []).append(row)

    brent_index = (
        load_brent_alignment_index(store, criteria["start_date"], cleaned_end)
        if target_symbol != "BRN0Y"
        else None
    )
    required_packs = detect_rule_required_packs(rules, pos_sizing)
    pack_enricher: QuantPackEnricher | None = None
    ext_pack_needed = required_packs & {
        "iopv_fx",
        "xop_us_ep",
        "uso_bno_sc_peer",
        "etf_redemption_flow",
    }
    if ext_pack_needed:
        pack_enricher = QuantPackEnricher(
            store,
            target_symbol,
            packs=sorted(ext_pack_needed),
            start_date=criteria["start_date"],
            end_date=cleaned_end,
        )

    evaluated_dates: list[str] = []
    skipped_no_flow_dates: list[str] = []
    all_signals: list[dict[str, Any]] = []
    daily_equity_curve: list[dict[str, Any]] = []
    paired_returns: list[float] = []
    cum_return_pct = 0.0
    max_consec_losses = 0
    cur_consec_losses = 0
    primary_h = int(criteria["primary_horizon_minutes"])
    min_move = float(criteria["min_move_pct"])

    # Base-position morning entry -> 15:00 close restore simulation accumulators
    m_start_compact = str(criteria["morning_start"]).replace(":", "")
    m_end_compact = str(criteria["morning_end"]).replace(":", "")
    max_daily = int(criteria["max_daily_trades"])
    fee_pct = float(criteria["round_trip_fee_pct"])

    t0_cum_gross_pct = 0.0
    t0_cum_net_pct = 0.0
    t0_buy_net_pct = 0.0
    t0_sell_net_pct = 0.0
    t0_buy_days = 0
    t0_sell_days = 0
    t0_traded_days = 0
    t0_hold_days = 0
    t0_compound_nav = 1.0
    base_hold_nav = 1.0
    combined_50_50_nav = 1.0
    with_base_nav = 1.0
    peak_t0_nav = 1.0
    max_dd_pct = 0.0
    t0_daily_net_rets: list[float] = []
    t0_traded_net_rets: list[float] = []
    t0_daily_curve: list[dict[str, Any]] = []
    t0_monthly_map: dict[str, dict[str, Any]] = {}

    # Capital-capped dynamic share simulation accumulators (v3)
    cap_limit = float(criteria["capital_cap"])
    cap_mode = str(pos_sizing["mode"])
    cap_max_pos_pct = float(pos_sizing["max_position_pct"])
    cap_max_entries = int(pos_sizing["max_daily_entries"])
    cap_lot = int(pos_sizing["lot_size"])
    cap_flatten_hhmm = str(pos_sizing["close_flatten_time"])
    cap_flatten_compact = cap_flatten_hhmm.replace(":", "")
    cap_turnaround_raw = pos_sizing["allow_same_day_turnaround"]
    cap_is_t0 = (
        is_t0_turnaround_symbol(target_symbol)
        if cap_turnaround_raw == "auto"
        else bool(cap_turnaround_raw)
    )
    single_fee_rate = (fee_pct / 2.0) / 100.0
    cap_cash = cap_limit
    cap_held_shares = 0
    cap_t0_cum_profit = 0.0
    cap_peak_equity = cap_limit
    cap_max_dd_pct = 0.0
    cap_max_dd_amount = 0.0
    cap_total_fees = 0.0
    cap_buy_orders = 0
    cap_sell_orders = 0
    cap_traded_days = 0
    cap_share_sizes: list[int] = []
    cap_daily_ret_pcts: list[float] = []
    cap_daily_ledger: list[dict[str, Any]] = []

    prev_candidate_date: str | None = initial_prev_td
    for day in day_rows:
        td = str(day["trade_date"])
        raw_pts = day_points_map.get(td, [])
        if len(raw_pts) < 10:
            prev_candidate_date = td
            continue
        pre_close = _number(day["pre_close"])
        points, has_flow = _enrich_day_points(
            store, target_symbol, td, raw_pts, pre_close, macd_settings
        )
        brent_meta = enrich_points_with_brent(points, td, prev_candidate_date, brent_index)
        if pack_enricher is not None:
            pack_enricher.enrich_day(td, prev_candidate_date, points, pre_close=pre_close)
        prev_candidate_date = td
        if effective_scope == "flow_only" and not has_flow:
            skipped_no_flow_dates.append(td)
            continue

        evaluated_dates.append(td)
        markers = build_replay_markers(
            target_symbol, points, rules, marker_threshold=threshold
        )
        # Build trading-minute lookup
        tm_list = [_trading_minute(pt.get("time")) for pt in points]
        price_list = [float(_number(pt.get("price")) or 0.0) for pt in points]
        avg_list = [_number(pt.get("average_price")) for pt in points]
        vol_list = [_number(pt.get("volume")) for pt in points]
        open_price = next((p for p in price_list if p > 0), 0.0)
        open_gap_pct = (
            round((open_price - pre_close) / pre_close * 100.0, 4)
            if pre_close and pre_close > 0 and open_price > 0
            else None
        )
        tm_to_idx: dict[int, int] = {}
        for i, tm in enumerate(tm_list):
            if tm is not None and tm not in tm_to_idx:
                tm_to_idx[tm] = i

        close_price = next((p for p in reversed(price_list) if p > 0), 0.0)

        # Precompute v3 dynamic share sizing per marker & daily capital_sim ledger
        marker_tier_map: dict[tuple[int, str], tuple[float, str]] = {}
        marker_sizing_map: dict[tuple[int, str], dict[str, Any]] = {}
        for m in markers:
            m_idx = int(m["index"])
            m_side = str(m["side"])
            marker_tier_map[(m_idx, m_side)] = resolve_signal_conviction_tier(
                target_symbol,
                points,
                m_idx,
                m_side,
                pos_sizing,
                marker_threshold=threshold,
            )

        day_cap_orders_log: list[dict[str, Any]] = []
        day_cap_buy_shares = 0
        day_cap_sell_shares = 0
        if cap_mode == "base_t0_close":
            flatten_px = close_price
            for pt_idx, pt in enumerate(points):
                t_cmp = str(pt.get("time") or "").replace(":", "")
                if t_cmp >= cap_flatten_compact and price_list[pt_idx] > 0:
                    flatten_px = price_list[pt_idx]
                    break
            day_buy_yuan = 0.0
            day_sell_yuan = 0.0
            day_net_shares = 0
            day_cash_flow = 0.0
            day_fees = 0.0
            day_entries = 0
            for m in markers:
                m_idx = int(m["index"])
                m_side = str(m["side"])
                m_px = float(m["price"])
                m_cmp = str(m["time"]).replace(":", "")
                tw, t_lbl = marker_tier_map[(m_idx, m_side)]
                if m_cmp >= cap_flatten_compact or m_cmp < m_start_compact or m_px <= 0:
                    marker_sizing_map[(m_idx, m_side)] = {
                        "planned_shares": 0,
                        "planned_amount": 0.0,
                        "weight_pct": tw,
                        "sizing_tier": t_lbl,
                    }
                    continue
                if day_entries >= cap_max_entries:
                    marker_sizing_map[(m_idx, m_side)] = {
                        "planned_shares": 0,
                        "planned_amount": 0.0,
                        "weight_pct": tw,
                        "sizing_tier": f"{t_lbl}·超日内次数",
                    }
                    continue
                used_amt = day_buy_yuan if m_side == "buy" else day_sell_yuan
                rem_pct = max(0.0, cap_max_pos_pct - (used_amt / cap_limit * 100.0)) if cap_limit > 0 else 0.0
                eff_w = min(tw, rem_pct)
                alloc = min(max(0.0, cap_limit - used_amt), cap_limit * (eff_w / 100.0))
                sh = compute_lot_shares_from_budget(alloc, m_px, cap_lot)
                if sh >= cap_lot:
                    notional = sh * m_px
                    fee = notional * single_fee_rate
                    day_entries += 1
                    day_fees += fee
                    cap_share_sizes.append(sh)
                    if m_side == "buy":
                        day_buy_yuan += notional
                        day_net_shares += sh
                        day_cash_flow -= notional + fee
                        day_cap_buy_shares += sh
                        cap_buy_orders += 1
                    else:
                        day_sell_yuan += notional
                        day_net_shares -= sh
                        day_cash_flow += notional - fee
                        day_cap_sell_shares += sh
                        cap_sell_orders += 1
                    day_cap_orders_log.append(
                        {"time": str(m["time"]), "side": m_side, "price": round(m_px, 3), "shares": sh, "tier": t_lbl}
                    )
                marker_sizing_map[(m_idx, m_side)] = {
                    "planned_shares": sh,
                    "planned_amount": round(sh * m_px, 2),
                    "weight_pct": round(eff_w, 2),
                    "sizing_tier": t_lbl,
                }
            if day_net_shares != 0 and flatten_px > 0:
                fl_sh = abs(day_net_shares)
                fl_side = "sell" if day_net_shares > 0 else "buy"
                notional = fl_sh * flatten_px
                fee = notional * single_fee_rate
                day_fees += fee
                cap_share_sizes.append(fl_sh)
                if fl_side == "sell":
                    day_cash_flow += notional - fee
                    day_cap_sell_shares += fl_sh
                    cap_sell_orders += 1
                else:
                    day_cash_flow -= notional + fee
                    day_cap_buy_shares += fl_sh
                    cap_buy_orders += 1
                day_cap_orders_log.append(
                    {"time": cap_flatten_hhmm, "side": fl_side, "price": round(flatten_px, 3), "shares": fl_sh, "tier": "尾盘平T恢复底仓"}
                )
            cap_total_fees += day_fees
            if day_cap_orders_log:
                cap_traded_days += 1
            prev_eq = cap_limit + cap_t0_cum_profit
            cap_t0_cum_profit += day_cash_flow
            day_eq = cap_limit + cap_t0_cum_profit
            day_pnl = day_cash_flow
            day_ret_pct = (day_pnl / cap_limit * 100.0) if cap_limit > 0 else 0.0
        else:
            # cash_pool_rolling mode
            mark_ref = pre_close if pre_close and pre_close > 0 else (open_price if open_price > 0 else close_price)
            prev_eq = cap_cash + cap_held_shares * mark_ref
            sellable_sh = cap_held_shares
            day_entries = 0
            day_fees = 0.0
            for m in markers:
                m_idx = int(m["index"])
                m_side = str(m["side"])
                m_px = float(m["price"])
                tw, t_lbl = marker_tier_map[(m_idx, m_side)]
                sh = 0
                if m_side == "buy" and m_px > 0 and day_entries < cap_max_entries:
                    deployed = cap_held_shares * m_px
                    max_pos_budget = max(0.0, cap_limit * (cap_max_pos_pct / 100.0) - deployed)
                    avail_cash = max(0.0, min(cap_cash / (1.0 + single_fee_rate), max_pos_budget))
                    alloc = min(avail_cash, cap_limit * (tw / 100.0))
                    sh = compute_lot_shares_from_budget(alloc, m_px, cap_lot)
                    if sh >= cap_lot:
                        notional = sh * m_px
                        fee = notional * single_fee_rate
                        cap_cash -= notional + fee
                        cap_held_shares += sh
                        if cap_is_t0:
                            sellable_sh += sh
                        day_fees += fee
                        day_entries += 1
                        day_cap_buy_shares += sh
                        cap_buy_orders += 1
                        cap_share_sizes.append(sh)
                        day_cap_orders_log.append(
                            {"time": str(m["time"]), "side": "buy", "price": round(m_px, 3), "shares": sh, "tier": t_lbl}
                        )
                elif m_side == "sell" and m_px > 0 and sellable_sh >= cap_lot:
                    raw_sell = int(round((sellable_sh * (tw / 100.0)) / cap_lot)) * cap_lot
                    sh = min(sellable_sh, max(cap_lot, raw_sell))
                    if sh >= cap_lot:
                        notional = sh * m_px
                        fee = notional * single_fee_rate
                        cap_cash += notional - fee
                        cap_held_shares -= sh
                        sellable_sh -= sh
                        day_fees += fee
                        day_cap_sell_shares += sh
                        cap_sell_orders += 1
                        cap_share_sizes.append(sh)
                        day_cap_orders_log.append(
                            {"time": str(m["time"]), "side": "sell", "price": round(m_px, 3), "shares": sh, "tier": t_lbl}
                        )
                marker_sizing_map[(m_idx, m_side)] = {
                    "planned_shares": sh,
                    "planned_amount": round(sh * m_px, 2),
                    "weight_pct": tw,
                    "sizing_tier": t_lbl,
                }
            cap_total_fees += day_fees
            if day_cap_orders_log:
                cap_traded_days += 1
            end_px = close_price if close_price > 0 else mark_ref
            day_eq = cap_cash + cap_held_shares * end_px
            day_pnl = day_eq - prev_eq
            day_ret_pct = (day_pnl / cap_limit * 100.0) if cap_limit > 0 else 0.0

        cap_peak_equity = max(cap_peak_equity, day_eq)
        dd_amt = max(0.0, cap_peak_equity - day_eq)
        dd_pct = (dd_amt / cap_peak_equity * 100.0) if cap_peak_equity > 0 else 0.0
        cap_max_dd_amount = max(cap_max_dd_amount, dd_amt)
        cap_max_dd_pct = max(cap_max_dd_pct, dd_pct)
        cap_daily_ret_pcts.append(day_ret_pct)
        cap_daily_ledger.append(
            {
                "trade_date": td,
                "buy_shares": day_cap_buy_shares,
                "sell_shares": day_cap_sell_shares,
                "ending_shares": 0 if cap_mode == "base_t0_close" else cap_held_shares,
                "day_pnl_amount": round(day_pnl, 2),
                "cum_pnl_amount": round(day_eq - cap_limit, 2),
                "equity": round(day_eq, 2),
                "orders": day_cap_orders_log,
            }
        )

        for m in markers:
            idx = int(m["index"])
            side = str(m["side"])
            entry_price = float(m["price"])
            curr_tm = tm_list[idx]
            avg_p = avg_list[idx]
            vwap_dev = (
                round((entry_price - avg_p) / avg_p * 100.0, 4)
                if avg_p and avg_p > 0
                else None
            )
            prev_vols = [
                v for v in vol_list[max(0, idx - 20) : idx] if v is not None and v >= 0
            ]
            cur_v = vol_list[idx]
            vol_ratio = (
                round(cur_v / (sum(prev_vols) / len(prev_vols)), 3)
                if cur_v is not None and prev_vols and sum(prev_vols) > 0
                else None
            )
            flow_val = _number(
                points[idx].get("large_order_net")
                or points[idx].get("large_order_amount")
            )

            horizon_rets: dict[str, float | None] = {}
            for h in _HORIZONS:
                future_idx: int | None = None
                if curr_tm is not None:
                    target_tm = curr_tm + h
                    if target_tm in tm_to_idx:
                        future_idx = tm_to_idx[target_tm]
                    elif target_tm <= 240:
                        for cand in range(idx + 1, len(points)):
                            cand_tm = tm_list[cand]
                            if cand_tm is not None and cand_tm >= target_tm:
                                future_idx = cand
                                break
                elif idx + h < len(points):
                    future_idx = idx + h
                if future_idx is not None and price_list[future_idx] > 0:
                    horizon_rets[f"ret_{h}m_pct"] = round(
                        (price_list[future_idx] - entry_price) / entry_price * 100.0, 4
                    )
                else:
                    horizon_rets[f"ret_{h}m_pct"] = None

            to_close = (
                round((close_price - entry_price) / entry_price * 100.0, 4)
                if close_price > 0 and entry_price > 0
                else None
            )
            t0_close_ret = (
                round((to_close if side == "buy" else -to_close) - fee_pct, 4)
                if to_close is not None
                else None
            )

            # 5-minute (or primary_h) MFE / MAE path
            window_prices: list[float] = []
            for j in range(idx + 1, len(points)):
                j_tm = tm_list[j]
                if curr_tm is not None and j_tm is not None:
                    if j_tm > curr_tm + primary_h:
                        break
                elif j - idx > primary_h:
                    break
                if price_list[j] > 0:
                    window_prices.append(price_list[j])
            mfe_pct: float | None = None
            mae_pct: float | None = None
            if window_prices:
                hi_p, lo_p = max(window_prices), min(window_prices)
                if side == "buy":
                    mfe_pct = round((hi_p - entry_price) / entry_price * 100.0, 4)
                    mae_pct = round((lo_p - entry_price) / entry_price * 100.0, 4)
                else:
                    mfe_pct = round((entry_price - lo_p) / entry_price * 100.0, 4)
                    mae_pct = round((entry_price - hi_p) / entry_price * 100.0, 4)

            prim_ret = horizon_rets.get(f"ret_{primary_h}m_pct")
            favorable_prim = (
                None
                if prim_ret is None
                else (prim_ret if side == "buy" else round(-prim_ret, 4))
            )
            is_success: bool | None = None
            if favorable_prim is not None:
                is_success = (
                    (favorable_prim > 0) if min_move <= 0 else (favorable_prim >= min_move)
                )

            sz_info = marker_sizing_map.get((idx, side), {})
            all_signals.append(
                {
                    "trade_date": td,
                    "time": str(m["time"]),
                    "side": side,
                    "rule_id": str(m["rule_id"]),
                    "rule_title": str(m["rule_title"]),
                    "price": entry_price,
                    "average_price": round(avg_p, 4) if avg_p is not None else None,
                    "vwap_dev_pct": vwap_dev,
                    "diff": points[idx].get("diff"),
                    "dea": points[idx].get("dea"),
                    "macd": points[idx].get("macd"),
                    "volume_ratio": vol_ratio,
                    "flow_value": flow_val,
                    "open_gap_pct": open_gap_pct,
                    "brn_price": points[idx].get("brn_price"),
                    "brn_overnight_pct": points[idx].get("brn_overnight_pct"),
                    "brn_from_prev_close_pct": points[idx].get("brn_from_prev_close_pct"),
                    "brn_spread_pct": points[idx].get("brn_spread_pct"),
                    "planned_shares": sz_info.get("planned_shares", 0),
                    "planned_amount": sz_info.get("planned_amount", 0.0),
                    "weight_pct": sz_info.get("weight_pct"),
                    "sizing_tier": sz_info.get("sizing_tier"),
                    **horizon_rets,
                    "to_close_pct": to_close,
                    "t0_close_net_pct": t0_close_ret,
                    "favorable_primary_pct": favorable_prim,
                    "mfe_primary_pct": mfe_pct,
                    "mae_primary_pct": mae_pct,
                    "success": is_success,
                }
            )

        # Base-position morning T+0 -> 15:00 close restore simulation
        base_ref = pre_close if pre_close and pre_close > 0 else open_price
        base_day_ret = (
            ((close_price - base_ref) / base_ref * 100.0)
            if base_ref > 0 and close_price > 0
            else 0.0
        )
        base_hold_nav *= 1.0 + base_day_ret / 100.0

        morning_hits = [
            m
            for m in markers
            if m_start_compact <= str(m["time"]).replace(":", "") <= m_end_compact
            and float(m["price"]) > 0
            and close_price > 0
        ][:max_daily]

        day_gross_t0 = 0.0
        day_net_t0 = 0.0
        day_side: str | None = None
        day_entry_time: str | None = None
        day_entry_price: float | None = None
        day_brn_spread: float | None = None

        if morning_hits:
            t0_traded_days += 1
            hit_gross_list: list[float] = []
            hit_net_list: list[float] = []
            has_buy_leg = False
            has_sell_leg = False
            buy_legs_net: list[float] = []
            sell_legs_net: list[float] = []
            for h in morning_hits:
                ep = float(h["price"])
                hs = str(h["side"])
                g = ((close_price - ep) / ep * 100.0) if hs == "buy" else ((ep - close_price) / ep * 100.0)
                n_val = g - fee_pct
                hit_gross_list.append(g)
                hit_net_list.append(n_val)
                if hs == "buy":
                    has_buy_leg = True
                    buy_legs_net.append(n_val)
                else:
                    has_sell_leg = True
                    sell_legs_net.append(n_val)
            day_gross_t0 = sum(hit_gross_list) / len(hit_gross_list)
            day_net_t0 = sum(hit_net_list) / len(hit_net_list)
            if has_buy_leg:
                t0_buy_days += 1
                t0_buy_net_pct += sum(buy_legs_net) / len(hit_net_list)
            if has_sell_leg:
                t0_sell_days += 1
                t0_sell_net_pct += sum(sell_legs_net) / len(hit_net_list)
            first_hit = morning_hits[0]
            day_side = str(first_hit["side"]) if len(morning_hits) == 1 else (
                "both" if has_buy_leg and has_sell_leg else str(first_hit["side"])
            )
            day_entry_time = str(first_hit["time"])
            day_entry_price = float(first_hit["price"])
            first_idx = int(first_hit["index"])
            day_brn_spread = _number(points[first_idx].get("brn_spread_pct"))
            t0_traded_net_rets.append(day_net_t0)
        else:
            t0_hold_days += 1

        t0_cum_gross_pct += day_gross_t0
        t0_cum_net_pct += day_net_t0
        t0_compound_nav *= 1.0 + day_net_t0 / 100.0
        combined_50_50_nav *= 1.0 + (0.5 * base_day_ret + 0.5 * day_net_t0) / 100.0
        with_base_nav *= 1.0 + (base_day_ret + day_net_t0) / 100.0
        peak_t0_nav = max(peak_t0_nav, t0_compound_nav)
        dd_here = (peak_t0_nav - t0_compound_nav) / peak_t0_nav * 100.0 if peak_t0_nav > 0 else 0.0
        max_dd_pct = max(max_dd_pct, dd_here)
        t0_daily_net_rets.append(day_net_t0)

        ym_dash = f"{td[:4]}-{td[4:6]}"
        m_blk = t0_monthly_map.setdefault(
            ym_dash,
            {
                "month": ym_dash,
                "days": 0,
                "traded_days": 0,
                "buy_t_days": 0,
                "sell_t_days": 0,
                "gross_return_pct": 0.0,
                "net_return_pct": 0.0,
                "base_nav": 1.0,
            },
        )
        m_blk["days"] += 1
        if morning_hits:
            m_blk["traded_days"] += 1
            if day_side in {"buy", "both"}:
                m_blk["buy_t_days"] += 1
            if day_side in {"sell", "both"}:
                m_blk["sell_t_days"] += 1
        m_blk["gross_return_pct"] += day_gross_t0
        m_blk["net_return_pct"] += day_net_t0
        m_blk["base_nav"] *= 1.0 + base_day_ret / 100.0

        t0_daily_curve.append(
            {
                "trade_date": td,
                "side": day_side,
                "entry_time": day_entry_time,
                "entry_price": round(day_entry_price, 3) if day_entry_price is not None else None,
                "close_price": round(close_price, 3) if close_price > 0 else None,
                "brn_overnight_pct": brent_meta.get("overnight_pct"),
                "brn_spread_pct": round(day_brn_spread, 4) if day_brn_spread is not None else None,
                "gross_ret_pct": round(day_gross_t0, 4),
                "net_ret_pct": round(day_net_t0, 4),
                "cum_net_ret_pct": round(t0_cum_net_pct, 4),
                "base_day_ret_pct": round(base_day_ret, 4),
                "base_cum_pct": round((base_hold_nav - 1.0) * 100.0, 4),
            }
        )

        # Legacy Daily T+0 B->S paired performance
        perf = calculate_replay_performance(markers, points)
        day_ret = perf.get("formula_return_pct")
        if day_ret is not None:
            cum_return_pct += float(day_ret)
        daily_equity_curve.append(
            {
                "trade_date": td,
                "signal_count": len(markers),
                "buy_count": perf["buy_count"],
                "sell_count": perf["sell_count"],
                "return_pct": round(float(day_ret), 4) if day_ret is not None else 0.0,
                "cumulative_return_pct": round(cum_return_pct, 4),
            }
        )
        # Individual round-trips (avg buy cost -> sell exit)
        open_qty = 0
        open_cost = 0.0
        for m in markers:
            p = float(m["price"])
            if m["side"] == "buy":
                open_qty += 100
                open_cost += 100 * p
            elif open_qty > 0:
                trip_ret = (open_qty * p / open_cost - 1.0) * 100.0
                paired_returns.append(trip_ret)
                if trip_ret <= 0:
                    cur_consec_losses += 1
                    max_consec_losses = max(max_consec_losses, cur_consec_losses)
                else:
                    cur_consec_losses = 0
                open_qty = 0
                open_cost = 0.0
        if open_qty > 0 and close_price > 0:
            trip_ret = (open_qty * close_price / open_cost - 1.0) * 100.0
            paired_returns.append(trip_ret)
            if trip_ret <= 0:
                cur_consec_losses += 1
                max_consec_losses = max(max_consec_losses, cur_consec_losses)
            else:
                cur_consec_losses = 0

    buy_signals = [s for s in all_signals if s["side"] == "buy"]
    sell_signals = [s for s in all_signals if s["side"] == "sell"]

    horizons_summary: dict[str, Any] = {}
    for h in _HORIZONS:
        field = f"ret_{h}m_pct"
        b_stat = _summarize_side_horizon(buy_signals, side="buy", field=field, min_move_pct=min_move)
        s_stat = _summarize_side_horizon(sell_signals, side="sell", field=field, min_move_pct=min_move)
        tot_n = b_stat["total"] + s_stat["total"]
        tot_w = b_stat["wins"] + s_stat["wins"]
        horizons_summary[f"{h}m"] = {
            "minutes": h,
            "total": tot_n,
            "wins": tot_w,
            "overall_win_rate_pct": round(tot_w / tot_n * 100.0, 2) if tot_n else None,
            "buy": b_stat,
            "sell": s_stat,
        }
    b_close = _summarize_side_horizon(buy_signals, side="buy", field="to_close_pct", min_move_pct=min_move)
    s_close = _summarize_side_horizon(sell_signals, side="sell", field="to_close_pct", min_move_pct=min_move)
    c_n = b_close["total"] + s_close["total"]
    c_w = b_close["wins"] + s_close["wins"]
    horizons_summary["close"] = {
        "minutes": None,
        "total": c_n,
        "wins": c_w,
        "overall_win_rate_pct": round(c_w / c_n * 100.0, 2) if c_n else None,
        "buy": b_close,
        "sell": s_close,
    }

    prim_key = f"{primary_h}m"
    prim_block = horizons_summary[prim_key]
    buy_mfe = [float(s["mfe_primary_pct"]) for s in buy_signals if s.get("mfe_primary_pct") is not None]
    buy_mae = [float(s["mae_primary_pct"]) for s in buy_signals if s.get("mae_primary_pct") is not None]
    sell_mfe = [float(s["mfe_primary_pct"]) for s in sell_signals if s.get("mfe_primary_pct") is not None]
    sell_mae = [float(s["mae_primary_pct"]) for s in sell_signals if s.get("mae_primary_pct") is not None]

    pos_trips = [r for r in paired_returns if r > 0]
    neg_trips = [abs(r) for r in paired_returns if r < 0]
    profit_factor = (
        round(sum(pos_trips) / sum(neg_trips), 3)
        if neg_trips and sum(neg_trips) > 0
        else (None if not pos_trips else 999.0)
    )

    # 7-dimension context attribution (with to-close T+0 avg return included)
    dim_extractors: dict[str, Callable[[dict[str, Any]], str]] = {
        "by_session": lambda s: _bucket_session(s["time"]),
        "by_vwap_zone": lambda s: _bucket_vwap(s.get("vwap_dev_pct")),
        "by_macd_zero": lambda s: _bucket_macd_zero(s.get("diff")),
        "by_macd_hist_strength": lambda s: _bucket_hist_strength(s.get("macd")),
        "by_volume_ratio": lambda s: _bucket_vol_ratio(s.get("volume_ratio")),
        "by_day_open_gap": lambda s: _bucket_open_gap(s.get("open_gap_pct")),
        "by_brent_spread": lambda s: _bucket_brent_spread(s.get("brn_spread_pct")),
    }
    attribution: dict[str, list[dict[str, Any]]] = {}
    prim_field = f"ret_{primary_h}m_pct"
    for dim_key, fn in dim_extractors.items():
        buckets: dict[str, list[dict[str, Any]]] = {}
        for sig in all_signals:
            if sig.get(prim_field) is None and sig.get("t0_close_net_pct") is None:
                continue
            buckets.setdefault(fn(sig), []).append(sig)
        rows_out: list[dict[str, Any]] = []
        for b_name, b_items in buckets.items():
            b_buy = [x for x in b_items if x["side"] == "buy"]
            b_sell = [x for x in b_items if x["side"] == "sell"]
            bs = _summarize_side_horizon(b_buy, side="buy", field=prim_field, min_move_pct=min_move)
            ss = _summarize_side_horizon(b_sell, side="sell", field=prim_field, min_move_pct=min_move)
            t0_vals = [float(x["t0_close_net_pct"]) for x in b_items if x.get("t0_close_net_pct") is not None]
            tot = bs["total"] + ss["total"]
            wins = bs["wins"] + ss["wins"]
            rows_out.append(
                {
                    "bucket": b_name,
                    "total": tot,
                    "wins": wins,
                    "overall_win_rate_pct": round(wins / tot * 100.0, 2) if tot else None,
                    "avg_to_close_t0_pct": round(statistics.mean(t0_vals), 4) if t0_vals else None,
                    "buy_total": bs["total"],
                    "buy_wins": bs["wins"],
                    "buy_win_rate_pct": bs["win_rate_pct"],
                    "buy_avg_pct": bs["avg_favorable_pct"],
                    "sell_total": ss["total"],
                    "sell_wins": ss["wins"],
                    "sell_win_rate_pct": ss["win_rate_pct"],
                    "sell_avg_drop_pct": ss["avg_favorable_pct"],
                }
            )
        attribution[dim_key] = rows_out

    # Base-position T+0 aggregate summary
    pos_t0 = [r for r in t0_traded_net_rets if r > 0]
    neg_t0 = [abs(r) for r in t0_traded_net_rets if r < 0]
    t0_profit_factor = (
        round(sum(pos_t0) / sum(neg_t0), 3)
        if neg_t0 and sum(neg_t0) > 0
        else (None if not pos_t0 else 999.0)
    )
    t0_sharpe: float | None = None
    if len(t0_daily_net_rets) >= 5:
        stdev = statistics.pstdev(t0_daily_net_rets)
        if stdev > 1e-9:
            t0_sharpe = round((statistics.mean(t0_daily_net_rets) / stdev) * math.sqrt(252.0), 3)
    eval_n = len(evaluated_dates)
    compound_net_pct = (t0_compound_nav - 1.0) * 100.0
    base_hold_pct = (base_hold_nav - 1.0) * 100.0
    with_base_total_pct = (with_base_nav - 1.0) * 100.0
    combined_50_50_pct = (combined_50_50_nav - 1.0) * 100.0
    annualized_net = (
        (((t0_compound_nav) ** (252.0 / eval_n)) - 1.0) * 100.0
        if eval_n > 0 and t0_compound_nav > 0
        else None
    )
    monthly_returns = [
        {
            "month": item["month"],
            "days": item["days"],
            "traded_days": item["traded_days"],
            "buy_t_days": item["buy_t_days"],
            "sell_t_days": item["sell_t_days"],
            "gross_return_pct": round(item["gross_return_pct"], 4),
            "net_return_pct": round(item["net_return_pct"], 4),
            "overnight_days": 0,
            "overnight_return_pct": 0.0,
            "intraday_t0_return_pct": round(item["net_return_pct"], 4),
            "base_hold_pct": round((item["base_nav"] - 1.0) * 100.0, 4),
        }
        for item in sorted(t0_monthly_map.values(), key=lambda x: x["month"])
    ]
    base_position_t0 = {
        "accounting_mode": "pure_t0",
        "start_date": evaluated_dates[0] if evaluated_dates else criteria["start_date"],
        "end_date": evaluated_dates[-1] if evaluated_dates else None,
        "morning_window": f"{criteria['morning_start']}-{criteria['morning_end']}",
        "max_daily_trades": max_daily,
        "round_trip_fee_pct": fee_pct,
        "evaluated_days": eval_n,
        "traded_days": t0_traded_days,
        "buy_t_days": t0_buy_days,
        "sell_t_days": t0_sell_days,
        "hold_days": t0_hold_days,
        "overnight_hold_days": 0,
        "daily_win_rate_pct": (
            round(len(pos_t0) / t0_traded_days * 100.0, 2) if t0_traded_days > 0 else None
        ),
        "avg_trade_net_pct": (
            round(statistics.mean(t0_traded_net_rets), 4) if t0_traded_net_rets else None
        ),
        "gross_cum_return_pct": round(t0_cum_gross_pct, 4),
        "net_cum_return_pct": round(t0_cum_net_pct, 4),
        "overnight_return_pct": 0.0,
        "intraday_t0_return_pct": round(t0_cum_net_pct, 4),
        "compound_net_return_pct": round(compound_net_pct, 4),
        "buy_t_net_return_pct": round(t0_buy_net_pct, 4),
        "sell_t_net_return_pct": round(t0_sell_net_pct, 4),
        "base_hold_return_pct": round(base_hold_pct, 4),
        "with_base_total_return_pct": round(with_base_total_pct, 4),
        "combined_50_50_return_pct": round(combined_50_50_pct, 4),
        "alpha_vs_hold_pct": round(with_base_total_pct - base_hold_pct, 4),
        "annualized_net_return_pct": round(annualized_net, 2) if annualized_net is not None else None,
        "max_drawdown_pct": round(max_dd_pct, 4),
        "sharpe_ratio": t0_sharpe,
        "profit_factor": t0_profit_factor,
        "monthly_returns": monthly_returns,
        "daily_curve": t0_daily_curve[-240:],
    }

    cap_ending_equity = cap_daily_ledger[-1]["equity"] if cap_daily_ledger else cap_limit
    cap_net_profit = round(cap_ending_equity - cap_limit, 2)
    cap_net_return_pct = round((cap_net_profit / cap_limit * 100.0) if cap_limit > 0 else 0.0, 2)
    cap_sharpe: float | None = None
    if len(cap_daily_ret_pcts) >= 5:
        c_std = statistics.pstdev(cap_daily_ret_pcts)
        if c_std > 1e-9:
            cap_sharpe = round((statistics.mean(cap_daily_ret_pcts) / c_std) * math.sqrt(252.0), 3)
    capital_sim = {
        "mode": cap_mode,
        "capital_cap": round(cap_limit, 2),
        "ending_equity": round(cap_ending_equity, 2),
        "net_profit_amount": cap_net_profit,
        "net_return_pct": cap_net_return_pct,
        "max_drawdown_pct": round(cap_max_dd_pct, 2),
        "max_drawdown_amount": round(cap_max_dd_amount, 2),
        "sharpe_ratio": cap_sharpe,
        "evaluated_days": eval_n,
        "traded_days": cap_traded_days,
        "total_orders": cap_buy_orders + cap_sell_orders,
        "buy_orders": cap_buy_orders,
        "sell_orders": cap_sell_orders,
        "avg_order_shares": round(statistics.mean(cap_share_sizes)) if cap_share_sizes else 0,
        "min_order_shares": min(cap_share_sizes) if cap_share_sizes else 0,
        "max_order_shares": max(cap_share_sizes) if cap_share_sizes else 0,
        "total_fees_amount": round(cap_total_fees, 2),
        "ending_shares": 0 if cap_mode == "base_t0_close" else cap_held_shares,
        "same_day_turnaround": cap_is_t0,
        "close_flatten_time": cap_flatten_hhmm if cap_mode == "base_t0_close" else None,
        "daily_ledger": cap_daily_ledger[-120:],
    }

    capped_signals = list(reversed(all_signals))[: max(1, min(5000, int(signal_limit)))]
    return {
        "symbol": target_symbol,
        "evaluated_at": _now_iso(),
        "criteria": criteria,
        "macd_settings": macd_settings,
        "position_sizing": pos_sizing,
        "requires_flow": requires_flow,
        "effective_scope": effective_scope,
        "coverage": {
            "total_candidate_days": len(day_rows),
            "evaluated_days": len(evaluated_dates),
            "skipped_no_flow_days": len(skipped_no_flow_dates),
            "first_date": evaluated_dates[0] if evaluated_dates else None,
            "last_date": evaluated_dates[-1] if evaluated_dates else None,
            "skipped_dates_sample": skipped_no_flow_dates[:10],
        },
        "base_position_t0": base_position_t0,
        "capital_sim": capital_sim,
        "summary": {
            "total_signals": len(all_signals),
            "primary_valid_signals": prim_block["total"],
            "primary_wins": prim_block["wins"],
            "overall_win_rate_pct": prim_block["overall_win_rate_pct"],
            "buy_signals": len(buy_signals),
            "buy_valid": prim_block["buy"]["total"],
            "buy_wins": prim_block["buy"]["wins"],
            "buy_win_rate_pct": prim_block["buy"]["win_rate_pct"],
            "buy_avg_pct": prim_block["buy"]["avg_price_change_pct"],
            "buy_median_pct": prim_block["buy"]["median_price_change_pct"],
            "buy_avg_mfe_pct": round(statistics.mean(buy_mfe), 4) if buy_mfe else None,
            "buy_avg_mae_pct": round(statistics.mean(buy_mae), 4) if buy_mae else None,
            "sell_signals": len(sell_signals),
            "sell_valid": prim_block["sell"]["total"],
            "sell_wins": prim_block["sell"]["wins"],
            "sell_win_rate_pct": prim_block["sell"]["win_rate_pct"],
            "sell_avg_price_change_pct": prim_block["sell"]["avg_price_change_pct"],
            "sell_avg_drop_pct": prim_block["sell"]["avg_favorable_pct"],
            "sell_median_drop_pct": prim_block["sell"]["median_favorable_pct"],
            "sell_avg_mfe_drop_pct": round(statistics.mean(sell_mfe), 4) if sell_mfe else None,
            "sell_avg_mae_pct": round(statistics.mean(sell_mae), 4) if sell_mae else None,
        },
        "horizons": horizons_summary,
        "trade_sim": {
            "paired_trades": len(paired_returns),
            "paired_win_rate_pct": (
                round(len(pos_trips) / len(paired_returns) * 100.0, 2)
                if paired_returns
                else None
            ),
            "expectancy_pct": (
                round(statistics.mean(paired_returns), 4) if paired_returns else None
            ),
            "profit_factor": profit_factor,
            "cumulative_return_pct": round(cum_return_pct, 4),
            "max_consecutive_losses": max_consec_losses,
        },
        "attribution": attribution,
        "daily_equity_curve": daily_equity_curve[-180:],
        "signals": capped_signals,
        "total_signals_returned": len(capped_signals),
    }


def _build_orderbook_l1_snapshot(
    store: Any,
    symbol: str,
    auto_trade_engine: Any = None,
) -> dict[str, Any] | None:
    """Return latest L1 5-level orderbook / price-cage snapshot without triggering external IO."""
    if symbol == "BRN0Y":
        return None
    book: dict[str, Any] | None = None
    source_tag = "ORDERBOOK_CACHE"
    ob_cache = getattr(auto_trade_engine, "_order_book_cache", None) if auto_trade_engine is not None else None
    if isinstance(ob_cache, Mapping):
        entry = ob_cache.get(symbol)
        if isinstance(entry, tuple) and len(entry) >= 2 and isinstance(entry[1], Mapping):
            book = dict(entry[1])
    if book is None:
        mem_probe = _MEMORY_MACRO_CACHE.get(("global", "probe_orderbook_l1"))
        if isinstance(mem_probe, tuple) and len(mem_probe) >= 2 and isinstance(mem_probe[1], Mapping):
            probe_bk = mem_probe[1].get(symbol)
            if isinstance(probe_bk, Mapping):
                book = dict(probe_bk)
                source_tag = "ORDERBOOK_PROBE_CACHE"
    if book is not None:
        return {
            "symbol": symbol,
            "name": book.get("name") or PREMIUM_NAMES.get(symbol, symbol),
            "last_price": book.get("last_price"),
            "previous_close": book.get("previous_close"),
            "limit_up": book.get("limit_up"),
            "limit_down": book.get("limit_down"),
            "bids": list(book.get("bids") or []),
            "asks": list(book.get("asks") or []),
            "source_time": book.get("source_time"),
            "source": source_tag,
        }
    snap_fn = getattr(store, "latest_market_snapshot", None)
    if callable(snap_fn):
        try:
            ms = snap_fn(symbol)
            if ms is not None:
                q = getattr(ms, "quote", None) or {}
                return {
                    "symbol": symbol,
                    "name": getattr(ms, "name", None) or PREMIUM_NAMES.get(symbol, symbol),
                    "last_price": _number(q.get("price")),
                    "previous_close": _number(q.get("previous_close")),
                    "open": _number(q.get("open")),
                    "high": _number(q.get("high")),
                    "low": _number(q.get("low")),
                    "volume": _number(q.get("volume")),
                    "limit_up": None,
                    "limit_down": None,
                    "bids": [],
                    "asks": [],
                    "source_time": getattr(ms, "source_time", None),
                    "source": getattr(ms, "source", "MARKET_DATABASE"),
                }
        except Exception:
            return None
    return None


def _build_qdii_panel_snapshot(store: Any, symbol: str) -> dict[str, Any] | None:
    """Extract sanitized QDII real-time valuation/premium row for `symbol`."""
    if symbol == "BRN0Y":
        return None
    snap_fn = getattr(store, "premium_snapshot", None)
    if not callable(snap_fn):
        return None
    try:
        snap = snap_fn()
    except Exception:
        return None
    if not isinstance(snap, Mapping):
        return None
    rows = snap.get("rows")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return None
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        code = str(row.get("code") or "").strip().upper()
        if code != symbol:
            continue
        prem_rate = _number(row.get("premium_rate"))
        return {
            "code": code,
            "name": row.get("name") or PREMIUM_NAMES.get(code, code),
            "current_price": _number(row.get("current_price")),
            "estimated_price": _number(row.get("estimated_price")),
            "premium_rate": prem_rate,
            "premium_pct": round(prem_rate * 100.0, 3) if prem_rate is not None else None,
            "estimate_source": row.get("estimate_source"),
            "estimate_date": row.get("estimate_date"),
            "stale": bool(row.get("stale")),
            "error": row.get("error"),
            "as_of": snap.get("as_of"),
        }
    return None


def _build_monitored_data_sources_summary(
    *,
    symbol: str,
    coverage: Mapping[str, Any],
    available_packs: Mapping[str, Mapping[str, Any]],
    macro_keys: set[str],
    orderbook_snap: Mapping[str, Any] | None,
    qdii_panel_snap: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Build sanitized 13-catalog data source readiness descriptors (no internal rule/live bindings)."""
    brent_cov = coverage.get("brent_coverage") if isinstance(coverage.get("brent_coverage"), Mapping) else {}
    brent_days = int((coverage if symbol == "BRN0Y" else brent_cov).get("total_days") or 0)
    base_days = int(coverage.get("total_days") or 0)
    out: list[dict[str, Any]] = []
    for meta in QUANT_DATA_SOURCE_CATALOG:
        sid = str(meta["id"])
        pack_id = MONITOR_SOURCE_TO_PACK.get(sid)
        avail = True
        latest_time: str | None = str(coverage.get("last_date") or "") or None
        latest_note: str | None = None
        if sid == "ashare_intraday_1m":
            avail = base_days > 0
            latest_note = f"{base_days} 日 1m 已入库"
        elif sid == "ashare_orderbook_l1":
            avail = orderbook_snap is not None
            latest_time = str((orderbook_snap or {}).get("source_time") or latest_time or "") or None
            latest_note = (orderbook_snap or {}).get("source") or "待采集盘口快照"
        elif sid == "ice_brent_1m":
            avail = brent_days > 0
            latest_time = str((brent_cov or coverage).get("last_date") or latest_time or "") or None
            latest_note = f"{brent_days} 日 BRN0Y 已入库"
        elif sid == "fx_usdcny_usdcnh":
            avail = bool((available_packs.get("iopv_fx") or {}).get("available"))
            latest_note = "fx_usdcnh" if "fx_usdcnh_series" in macro_keys else "fx_usdcny"
        elif sid == "eastmoney_fund_nav":
            avail = symbol != "BRN0Y" and (f"nav_{symbol}" in macro_keys or bool((available_packs.get("iopv_fx") or {}).get("available")))
            latest_note = f"nav_{symbol}" if f"nav_{symbol}" in macro_keys else "按需拉取天天基金净值"
        elif sid == "qdii_premium_panel_feed":
            avail = qdii_panel_snap is not None or symbol in _OIL_QDII_NAV_SYMBOLS
            latest_time = str((qdii_panel_snap or {}).get("as_of") or latest_time or "") or None
            latest_note = (
                f"溢价 {(qdii_panel_snap or {}).get('premium_pct')}%"
                if (qdii_panel_snap and qdii_panel_snap.get("premium_pct") is not None)
                else "9只油气基金估值面板"
            )
        elif pack_id is not None:
            p_info = available_packs.get(pack_id) or {}
            avail = bool(p_info.get("available"))
            if sid == "ashare_l2_fund_flow":
                latest_note = f"{p_info.get('l2_days', 0)} 日 L2 资金流"
            elif sid == "ashare_etf_redemption":
                latest_note = f"{p_info.get('line_days', 0)} 日申赎流"
        out.append(
            {
                "id": sid,
                "name": str(meta["name"]),
                "category": str(meta["category"]),
                "category_label": str(meta["category_label"]),
                "pack_id": pack_id,
                "fetch_frequency": str(meta.get("fetch_frequency") or ""),
                "trading_hours_cst": str(meta.get("trading_hours_cst") or ""),
                "available": bool(avail),
                "latest_time": latest_time,
                "latest_note": latest_note,
            }
        )
    return out


def export_quant_dataset(
    store: Any,
    symbol: str = "160723",
    *,
    start_date: str = "20260701",
    end_date: str | None = None,
    mode: str = "summary",
    limit_days: int = 30,
    macd_settings: Mapping[str, Any] | None = None,
    packs: Sequence[str] | str | None = None,
    sources: Sequence[str] | str | None = None,
    allow_network: bool | None = None,
    etf_flow_db_path: Path | str | None = None,
    auto_trade_engine: Any = None,
) -> dict[str, Any]:
    """Export structured dataset for external AI research (summary / daily / minutes)."""
    normalized = _normalize_quant_symbol(symbol)
    explicit_selector = packs is not None or sources is not None
    selected_packs, requested_sources = resolve_packs_from_sources_and_packs(
        packs,
        sources,
        default_all=not explicit_selector,
    )
    eff_allow_network = (
        allow_network
        if allow_network is not None
        else (True if explicit_selector else False)
    )
    clean_start = str(start_date or "20260701").replace("-", "").strip()
    if not _DATE_RE.fullmatch(clean_start):
        clean_start = "20260701"
    clean_end = None
    if end_date:
        cand = str(end_date).replace("-", "").strip()
        if _DATE_RE.fullmatch(cand):
            clean_end = cand
    eff_macd = _normalize_macd_settings(macd_settings)
    coverage = get_symbol_coverage(store, normalized, start_date=clean_start)
    if normalized != "BRN0Y":
        coverage["brent_coverage"] = get_symbol_coverage(store, "BRN0Y", start_date=clean_start)

    with store._lock:
        sql = "SELECT * FROM research_days WHERE symbol=? AND trade_date>=?"
        params: list[Any] = [normalized, clean_start]
        if clean_end:
            sql += " AND trade_date<=?"
            params.append(clean_end)
        sql += " ORDER BY trade_date ASC"
        day_rows = store._connection.execute(sql, tuple(params)).fetchall()

        prev_rows = store._connection.execute(
            "SELECT trade_date FROM research_days WHERE symbol=? AND trade_date<? ORDER BY trade_date DESC LIMIT 2",
            (normalized, clean_start),
        ).fetchall()
        initial_prev_td = str(prev_rows[0]["trade_date"]) if len(prev_rows) >= 1 and prev_rows[0]["trade_date"] else None
        initial_prev_prev_td = str(prev_rows[1]["trade_date"]) if len(prev_rows) >= 2 and prev_rows[1]["trade_date"] else initial_prev_td

        l2_days_cnt = 0
        try:
            l2_row = store._connection.execute(
                "SELECT COUNT(DISTINCT trade_date) AS cnt FROM fund_flow_history_points WHERE symbol=? AND trade_date>=?",
                (normalized, clean_start),
            ).fetchone()
            l2_days_cnt = int(l2_row["cnt"] or 0) if l2_row else 0
        except sqlite3.Error:
            l2_days_cnt = 0

        macro_keys: set[str] = set()
        try:
            m_rows = store._connection.execute(
                "SELECT cache_key FROM quant_macro_cache"
            ).fetchall()
            macro_keys = {str(r["cache_key"]) for r in m_rows}
        except sqlite3.Error:
            macro_keys = set()

        etf_cache_meta = _load_etf_flow_cache(
            _resolve_etf_flow_db_path(store, etf_flow_db_path),
            normalized,
        )
        brent_ok = bool((coverage.get("brent_coverage") or {}).get("total_days"))
        available_packs_summary = {
            "iopv_fx": {
                "available": normalized != "BRN0Y" and (
                    brent_ok
                    or f"nav_{normalized}" in macro_keys
                    or "fx_usdcny_series" in macro_keys
                    or "fx_usdcnh_series" in macro_keys
                ),
            },
            "xop_us_ep": {
                "available": normalized != "BRN0Y" and ("xop_daily_2y" in macro_keys or normalized == "162411"),
            },
            "uso_bno_sc_peer": {
                "available": normalized != "BRN0Y" and (
                    "sc0_daily_all" in macro_keys
                    or "uso_daily_2y" in macro_keys
                    or "dbo_daily_2y" in macro_keys
                    or normalized in _OIL_PEER_SYMBOLS
                ),
            },
            "l2_darkpool_flow": {
                "available": l2_days_cnt > 0,
                "l2_days": l2_days_cnt,
            },
            "etf_redemption_flow": {
                "available": bool(etf_cache_meta.get("line") or etf_cache_meta.get("summary")),
                "line_days": len(etf_cache_meta.get("line") or []),
                "holdings_count": len(etf_cache_meta.get("holdings") or []),
            },
        }

        orderbook_snap = (
            _build_orderbook_l1_snapshot(store, normalized, auto_trade_engine)
            if "ashare_orderbook_l1" in requested_sources
            else None
        )
        qdii_panel_snap = (
            _build_qdii_panel_snapshot(store, normalized)
            if ("iopv_fx" in selected_packs and "qdii_premium_panel_feed" in requested_sources)
            else None
        )
        monitored_sources_summary = _build_monitored_data_sources_summary(
            symbol=normalized,
            coverage=coverage,
            available_packs=available_packs_summary,
            macro_keys=macro_keys,
            orderbook_snap=orderbook_snap,
            qdii_panel_snap=qdii_panel_snap,
        )

        if mode == "summary":
            sum_sql = (
                "SELECT trade_date, COUNT(*) AS pt_cnt, "
                "SUM(CASE WHEN large_order_net IS NOT NULL OR large_order_amount IS NOT NULL THEN 1 ELSE 0 END) AS flow_cnt "
                "FROM research_points WHERE symbol=? AND trade_date>=?"
                + (" AND trade_date<=?" if clean_end else "")
                + " GROUP BY trade_date ORDER BY trade_date ASC"
            )
            pts_by_day_summary = {
                str(r["trade_date"]): (int(r["pt_cnt"] or 0), int(r["flow_cnt"] or 0))
                for r in store._connection.execute(sum_sql, tuple(params)).fetchall()
            }
            monthly: dict[str, dict[str, Any]] = {}
            for d in day_rows:
                td = str(d["trade_date"])
                ym = td[:6]
                m = monthly.setdefault(ym, {"month": ym, "days": 0, "flow_days": 0, "points": 0})
                pt_cnt, flow_cnt = pts_by_day_summary.get(td, (0, 0))
                m["days"] += 1
                m["points"] += pt_cnt
                if flow_cnt >= 50:
                    m["flow_days"] += 1
            res_summary: dict[str, Any] = {
                "mode": "summary",
                "symbol": normalized,
                "coverage": coverage,
                "available_data_packs": available_packs_summary,
                "requested_packs": selected_packs,
                "requested_sources": requested_sources,
                "monitored_data_sources": monitored_sources_summary,
                "monthly_breakdown": list(monthly.values()),
                "schema": get_quant_rule_schema(),
            }
            if orderbook_snap is not None:
                res_summary["orderbook_l1_snapshot"] = orderbook_snap
            if qdii_panel_snap is not None:
                res_summary["qdii_panel_snapshot"] = qdii_panel_snap
            return res_summary

        pt_sql = (
            "SELECT * FROM research_points WHERE symbol=? AND trade_date>=?"
            + (" AND trade_date<=?" if clean_end else "")
            + " ORDER BY trade_date ASC, REPLACE(minute,':','') ASC"
        )
        points_by_day: dict[str, list[sqlite3.Row]] = {}
        for r in store._connection.execute(pt_sql, tuple(params)).fetchall():
            points_by_day.setdefault(str(r["trade_date"]), []).append(r)

    brent_index = (
        load_brent_alignment_index(store, clean_start, clean_end)
        if normalized != "BRN0Y" and mode in {"daily", "minutes"}
        else None
    )
    pack_enricher: QuantPackEnricher | None = (
        QuantPackEnricher(
            store,
            normalized,
            packs=selected_packs,
            start_date=clean_start,
            end_date=clean_end,
            allow_network=eff_allow_network,
            etf_flow_db_path=etf_flow_db_path,
        )
        if selected_packs and mode in {"daily", "minutes"}
        else None
    )

    if mode == "daily":
        daily_out: list[dict[str, Any]] = []
        prev_td: str | None = initial_prev_td
        prev_prev_td: str | None = initial_prev_prev_td
        for d in day_rows:
            td = str(d["trade_date"])
            raw_pts = points_by_day.get(td, [])
            prices = [float(r["price"]) for r in raw_pts if r["price"] is not None]
            pre = _number(d["pre_close"])
            open_p = _number(d["quote_open"]) or (prices[0] if prices else None)
            high_p = _number(d["quote_high"]) or (max(prices) if prices else None)
            low_p = _number(d["quote_low"]) or (min(prices) if prices else None)
            close_p = _number(d["quote_price"]) or (prices[-1] if prices else None)
            flow_cnt = sum(
                1 for r in raw_pts if r["large_order_net"] is not None or r["large_order_amount"] is not None
            )
            b_meta = enrich_points_with_brent([], td, prev_td, brent_index)
            pack_daily_meta: dict[str, Any] = {}
            if pack_enricher is not None:
                if raw_pts:
                    pts_enriched, ff_has_flow = _enrich_day_points(store, normalized, td, raw_pts, pre, eff_macd)
                    enrich_points_with_brent(pts_enriched, td, prev_td, brent_index)
                    pack_daily_meta = pack_enricher.enrich_day(
                        td,
                        prev_td,
                        pts_enriched,
                        pre_close=pre,
                        prev_prev_td=prev_prev_td,
                    )
                    if ff_has_flow:
                        flow_cnt = max(flow_cnt, 50)
                elif "etf_redemption_flow" in pack_enricher.packs and pack_enricher.etf_cache:
                    pack_daily_meta = _causal_etf_flow_metrics(pack_enricher.etf_cache, td)
            prev_prev_td = prev_td or prev_prev_td
            prev_td = td
            b_prev = b_meta.get("prev_1500")
            b_c15 = b_meta.get("close_1500")
            b_day_pct = (
                round((b_c15 - b_prev) / b_prev * 100.0, 3)
                if b_c15 and b_prev and b_prev > 0
                else None
            )
            daily_item = {
                "trade_date": td,
                "source": d["source"],
                "point_count": len(raw_pts),
                "has_flow": flow_cnt >= 50,
                "pre_close": pre,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "open_gap_pct": round((open_p - pre) / pre * 100.0, 3) if pre and open_p and pre > 0 else None,
                "close_vs_open_pct": round((close_p - open_p) / open_p * 100.0, 3) if open_p and close_p and open_p > 0 else None,
                "intraday_amp_pct": round((high_p - low_p) / pre * 100.0, 3) if pre and high_p and low_p and pre > 0 else None,
                "day_return_pct": round((close_p - pre) / pre * 100.0, 3) if pre and close_p and pre > 0 else None,
                "brn_prev_1500": b_prev,
                "brn_open_0930": b_meta.get("open_0930"),
                "brn_close_1500": b_c15,
                "brn_overnight_pct": b_meta.get("overnight_pct"),
                "brn_day_pct": b_day_pct,
                **pack_daily_meta,
            }
            daily_out.append(daily_item)
        res_daily: dict[str, Any] = {
            "mode": "daily",
            "symbol": normalized,
            "coverage": coverage,
            "data_packs_included": selected_packs,
            "requested_sources": requested_sources,
            "monitored_data_sources": monitored_sources_summary,
            "days": daily_out,
        }
        if orderbook_snap is not None:
            res_daily["orderbook_l1_snapshot"] = orderbook_snap
        if qdii_panel_snap is not None:
            res_daily["qdii_panel_snapshot"] = qdii_panel_snap
        if pack_enricher is not None and "etf_redemption_flow" in pack_enricher.packs:
            res_daily["etf_top_holdings"] = pack_enricher.etf_cache.get("holdings") or []
        return res_daily

    # mode == "minutes"
    cap_days = max(1, min(200, int(limit_days)))
    selected_days = day_rows[-cap_days:]
    selected_set = {str(d["trade_date"]) for d in selected_days}
    all_dates_ordered = [str(d["trade_date"]) for d in day_rows]
    minute_days: list[dict[str, Any]] = []
    pack_field_groups: dict[str, tuple[str, ...]] = {
        "iopv_fx": (
            "iopv_fx",
            "iopv_premium_pct",
            "sp_fx_pct",
            "coint10_pct",
            "usdcny",
            "usdcnh",
            "usdcny_change_pct",
            "fx_adjusted_nav_change_pct",
            "official_nav",
            "brent_q2y_pct",
        ),
        "xop_us_ep": (
            "xop_change_pct",
            "xle_change_pct",
            "spy_change_pct",
            "iwm_change_pct",
            "cl_change_pct",
            "ho_crack_change_pct",
            "ng_change_pct",
            "es_change_pct",
            "xop_synthetic_premium_pct",
            "daytime_lead_pct",
            "daytime_crack_pct",
        ),
        "uso_bno_sc_peer": (
            "uso_bno_change_pct",
            "uso_change_pct",
            "bno_change_pct",
            "dbo_change_pct",
            "fof_3oil_change_pct",
            "dual_oil_premium_pct",
            "sc0_change_pct",
            "sc0_intraday_pct",
            "sc_brent_spread_pct",
            "peer_spread_pct",
            "peer_change_pct",
            "peer_premium_diff_pct",
            "peer_160723_change_pct",
            "peer_160723_spread_pct",
            "peer_161129_change_pct",
            "peer_161129_spread_pct",
            "peer_501018_change_pct",
            "peer_501018_spread_pct",
            "peer_162411_change_pct",
            "peer_162411_spread_pct",
        ),
        "l2_darkpool_flow": (
            "l2_today_main_net_wan",
            "l2_today_visible_wan",
            "l2_today_hidden_wan",
            "l2_today_retail_wan",
            "l2_today_hidden_ratio_pct",
            "l2_3d_main_net_wan",
            "l2_3d_visible_wan",
            "l2_3d_hidden_wan",
            "l2_3d_retail_wan",
            "l2_3d_hidden_ratio_pct",
            "l2_5d_main_net_wan",
            "l2_5d_visible_wan",
            "l2_5d_hidden_wan",
            "l2_5d_retail_wan",
            "l2_5d_hidden_ratio_pct",
        ),
        "etf_redemption_flow": (
            "etf_net_buy_1d_wan",
            "etf_net_buy_3d_wan",
            "etf_net_buy_5d_wan",
            "etf_net_buy_20d_wan",
            "etf_share_scale_yi",
            "etf_net_buy_ratio_pct",
        ),
    }
    allowed_extra_keys = [
        k for p_name in selected_packs for k in pack_field_groups.get(p_name, ())
    ]
    for idx_d, d in enumerate(day_rows):
        td = str(d["trade_date"])
        if td not in selected_set:
            continue
        raw_pts = points_by_day.get(td, [])
        if not raw_pts:
            continue
        prev_td = all_dates_ordered[idx_d - 1] if idx_d > 0 else initial_prev_td
        prev_prev_td = (
            all_dates_ordered[idx_d - 2]
            if idx_d >= 2
            else (initial_prev_td if idx_d == 1 else initial_prev_prev_td)
        )
        pre = _number(d["pre_close"])
        points, has_flow = _enrich_day_points(store, normalized, td, raw_pts, pre, eff_macd)
        enrich_points_with_brent(points, td, prev_td, brent_index)
        day_pack_meta = (
            pack_enricher.enrich_day(
                td,
                prev_td,
                points,
                pre_close=pre,
                prev_prev_td=prev_prev_td,
            )
            if pack_enricher is not None
            else {}
        )
        prices = [float(_number(p["price"]) or 0.0) for p in points]
        close_p = next((p for p in reversed(prices) if p > 0), 0.0)
        vols = [_number(p.get("volume")) for p in points]
        rows_out: list[dict[str, Any]] = []
        for i, pt in enumerate(points):
            p = prices[i]
            avg = _number(pt.get("average_price"))
            vwap_dev = round((p - avg) / avg * 100.0, 4) if avg and avg > 0 and p > 0 else None
            prev_vols = [v for v in vols[max(0, i - 20) : i] if v is not None]
            vr = (
                round(vols[i] / (sum(prev_vols) / len(prev_vols)), 3)
                if vols[i] is not None and prev_vols and sum(prev_vols) > 0
                else None
            )
            flow_v = _number(pt.get("large_order_net") or pt.get("large_order_amount"))
            prev_flow_1m = _number(points[i - 1].get("large_order_net") or points[i - 1].get("large_order_amount")) if i > 0 else None
            flow_1m_chg = (
                round((flow_v - prev_flow_1m) / abs(prev_flow_1m) * 100.0, 2)
                if flow_v is not None and prev_flow_1m not in (None, 0)
                else None
            )
            ret_5m = round((prices[i + 5] - p) / p * 100.0, 4) if i + 5 < len(prices) and p > 0 and prices[i + 5] > 0 else None
            ret_15m = round((prices[i + 15] - p) / p * 100.0, 4) if i + 15 < len(prices) and p > 0 and prices[i + 15] > 0 else None
            to_close_ret = round((close_p - p) / p * 100.0, 4) if p > 0 and close_p > 0 else None
            pt_row = {
                "time": pt["time"],
                "price": p,
                "avg_price": avg,
                "vwap_dev_pct": vwap_dev,
                "volume": vols[i],
                "vol_ratio_20m": vr,
                "diff": pt["diff"],
                "dea": pt["dea"],
                "macd": pt["macd"],
                "flow_wan": flow_v,
                "flow_1m_chg_pct": flow_1m_chg,
                "brn_price": pt.get("brn_price"),
                "brn_overnight_pct": pt.get("brn_overnight_pct"),
                "brn_from_prev_close_pct": pt.get("brn_from_prev_close_pct"),
                "brn_intraday_pct": pt.get("brn_intraday_pct"),
                "brn_mom_5m_pct": pt.get("brn_mom_5m_pct"),
                "brn_mom_15m_pct": pt.get("brn_mom_15m_pct"),
                "brn_spread_pct": pt.get("brn_spread_pct"),
                "fwd_ret_5m_pct": ret_5m,
                "fwd_ret_15m_pct": ret_15m,
                "to_close_ret_pct": to_close_ret,
            }
            for ek in allowed_extra_keys:
                if ek in pt:
                    pt_row[ek] = pt.get(ek)
            rows_out.append(pt_row)
        minute_days.append(
            {
                "trade_date": td,
                "pre_close": pre,
                "has_flow": has_flow,
                "day_pack_summary": day_pack_meta,
                "points": rows_out,
            }
        )
    res_min: dict[str, Any] = {
        "mode": "minutes",
        "symbol": normalized,
        "coverage": coverage,
        "macd_settings": eff_macd,
        "data_packs_included": selected_packs,
        "requested_sources": requested_sources,
        "monitored_data_sources": monitored_sources_summary,
        "returned_days": len(minute_days),
        "days": minute_days,
    }
    if orderbook_snap is not None:
        res_min["orderbook_l1_snapshot"] = orderbook_snap
    if qdii_panel_snap is not None:
        res_min["qdii_panel_snapshot"] = qdii_panel_snap
    if pack_enricher is not None and "etf_redemption_flow" in pack_enricher.packs:
        res_min["etf_top_holdings"] = pack_enricher.etf_cache.get("holdings") or []
    return res_min


class SQLiteQuantRuleStore:
    """Persistent rule repository backed by research.db."""

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
        self.ensure_builtin_rules()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _migrate(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS quant_strategy_rules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    source TEXT NOT NULL,
                    symbols_json TEXT NOT NULL,
                    macd_json TEXT NOT NULL,
                    buy_rule_json TEXT,
                    sell_rule_json TEXT,
                    position_sizing_json TEXT,
                    eval_criteria_json TEXT NOT NULL,
                    latest_eval_json TEXT,
                    meta_json TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_quant_rules_updated
                    ON quant_strategy_rules(updated_at DESC);
                """
            )
            cols = {
                str(r[1])
                for r in self._connection.execute("PRAGMA table_info(quant_strategy_rules)").fetchall()
            }
            if "position_sizing_json" not in cols:
                self._connection.execute("ALTER TABLE quant_strategy_rules ADD COLUMN position_sizing_json TEXT")
            if "meta_json" not in cols:
                self._connection.execute("ALTER TABLE quant_strategy_rules ADD COLUMN meta_json TEXT")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def ensure_builtin_rules(self) -> None:
        now = _now_iso()
        with self._lock, self._connection:
            for item in BUILTIN_QUANT_RULES:
                existing = self._connection.execute(
                    "SELECT 1 FROM quant_strategy_rules WHERE id=?",
                    (item["id"],),
                ).fetchone()
                if existing is not None:
                    continue
                derived_ds = derive_rule_data_sources(item)
                meta_payload = {
                    "target_symbol": (item.get("symbols") or ["160723"])[0],
                    "data_sources": derived_ds,
                }
                self._connection.execute(
                    """INSERT INTO quant_strategy_rules(
                           id, name, description, source, symbols_json, macd_json,
                           buy_rule_json, sell_rule_json, eval_criteria_json,
                           latest_eval_json, meta_json, enabled, created_at, updated_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
                    (
                        item["id"],
                        item["name"],
                        item["description"],
                        item["source"],
                        json.dumps(item["symbols"], ensure_ascii=False),
                        json.dumps(item["macd_settings"], ensure_ascii=False),
                        json.dumps(item["buy_rule"], ensure_ascii=False),
                        json.dumps(item["sell_rule"], ensure_ascii=False),
                        json.dumps(item["eval_criteria"], ensure_ascii=False),
                        json.dumps(meta_payload, ensure_ascii=False),
                        1 if item["enabled"] else 0,
                        now,
                        now,
                    ),
                )
            # Backfill or upgrade auto-derived data_sources manifest in meta_json when catalog schema advances
            data_sources_schema_rev = 3
            all_rows = self._connection.execute("SELECT * FROM quant_strategy_rules").fetchall()
            for row in all_rows:
                rule_dict = self._row_to_dict(row)
                raw_meta: dict[str, Any] = dict(rule_dict.get("meta") or {})
                cur_ds = raw_meta.get("data_sources")
                has_custom_ds = isinstance(cur_ds, list) and any(
                    isinstance(d, Mapping) and d.get("auto_derived") is False for d in cur_ds
                )
                cur_rev = int(raw_meta.get("data_sources_schema_rev") or 0)
                if cur_rev >= data_sources_schema_rev and isinstance(cur_ds, list) and cur_ds:
                    continue
                raw_meta.setdefault("target_symbol", rule_dict["target_symbol"])
                if has_custom_ds and isinstance(cur_ds, list) and cur_ds:
                    raw_meta["data_sources"] = normalize_and_validate_rule_data_sources(
                        cur_ds, rule_dict, strict_declared=False
                    )
                else:
                    raw_meta["data_sources"] = derive_rule_data_sources(rule_dict)
                raw_meta["data_sources_schema_rev"] = data_sources_schema_rev
                self._connection.execute(
                    "UPDATE quant_strategy_rules SET meta_json=? WHERE id=?",
                    (json.dumps(raw_meta, ensure_ascii=False), rule_dict["id"]),
                )

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        raw_ps = None
        if "position_sizing_json" in row.keys() and row["position_sizing_json"]:
            try:
                raw_ps = json.loads(row["position_sizing_json"])
            except Exception:
                raw_ps = None
        raw_meta: dict[str, Any] = {}
        if "meta_json" in row.keys() and row["meta_json"]:
            try:
                parsed_meta = json.loads(row["meta_json"])
                if isinstance(parsed_meta, dict):
                    raw_meta = parsed_meta
            except Exception:
                raw_meta = {}
        symbols = json.loads(row["symbols_json"])
        primary_sym = str(raw_meta.get("target_symbol") or (symbols[0] if symbols else "160723")).strip().upper()
        pos_sizing = normalize_position_sizing(raw_ps)
        buy_rule = json.loads(row["buy_rule_json"]) if row["buy_rule_json"] else None
        sell_rule = json.loads(row["sell_rule_json"]) if row["sell_rule_json"] else None
        rule_skeleton = {
            "symbols": symbols,
            "target_symbol": primary_sym,
            "buy_rule": buy_rule,
            "sell_rule": sell_rule,
            "position_sizing": pos_sizing,
        }
        try:
            normalized_ds = normalize_and_validate_rule_data_sources(
                raw_meta.get("data_sources"),
                rule_skeleton,
                strict_declared=False,
            )
        except ValueError:
            normalized_ds = derive_rule_data_sources(rule_skeleton)
        return {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "description": str(row["description"]),
            "source": str(row["source"]),
            "symbols": symbols,
            "target_symbol": primary_sym,
            "stock_name": raw_meta.get("stock_name"),
            "raw_strategy_name": raw_meta.get("raw_strategy_name"),
            "submitter_id": raw_meta.get("submitter_id"),
            "submitter_name": raw_meta.get("submitter_name"),
            "submission_id": raw_meta.get("submission_id"),
            "approved_by": raw_meta.get("approved_by"),
            "approved_at": raw_meta.get("approved_at"),
            "data_sources": normalized_ds,
            "meta": raw_meta,
            "macd_settings": json.loads(row["macd_json"]),
            "buy_rule": buy_rule,
            "sell_rule": sell_rule,
            "position_sizing": pos_sizing,
            "eval_criteria": json.loads(row["eval_criteria_json"]),
            "latest_eval": json.loads(row["latest_eval_json"]) if row["latest_eval_json"] else None,
            "enabled": bool(row["enabled"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def list_rules(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM quant_strategy_rules
                   ORDER BY CASE WHEN source='builtin' THEN 0 ELSE 1 END, updated_at DESC"""
            ).fetchall()
        items = [self._row_to_dict(r) for r in rows]
        if symbol:
            clean_sym = _normalize_quant_symbol(symbol)
            items = [
                item
                for item in items
                if clean_sym in {str(s).strip().upper() for s in (item.get("symbols") or [])}
                or str(item.get("target_symbol") or "").strip().upper() == clean_sym
            ]
        return items

    def get_rule(self, rule_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM quant_strategy_rules WHERE id=?",
                (str(rule_id).strip(),),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def upsert_rule(
        self,
        payload: Mapping[str, Any],
        *,
        research_store: Any | None = None,
        rule_id: str | None = None,
    ) -> dict[str, Any]:
        rid = str(rule_id or payload.get("id") or f"ai-{uuid.uuid4().hex[:10]}").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{2,64}", rid):
            raise ValueError("rule id must be 2-64 alphanumeric/._- characters")
        existing = self.get_rule(rid)
        name = str(payload.get("name") or (existing["name"] if existing else "")).strip()
        if not name or len(name) > 120:
            raise ValueError("name must be 1-120 characters")
        description = str(
            payload.get("description") if "description" in payload else (existing["description"] if existing else "")
        ).strip()
        source = str(payload.get("source") or (existing["source"] if existing else "ai")).strip().lower()
        if existing and existing["source"] == "builtin":
            source = "builtin"
        elif source not in {"ai", "manual", "builtin"}:
            source = "ai"

        raw_symbols = payload.get("symbols", existing["symbols"] if existing else ["160723"])
        symbols = [
            str(s).strip().upper()
            for s in (raw_symbols if isinstance(raw_symbols, Sequence) and not isinstance(raw_symbols, (str, bytes)) else ["160723"])
            if _QUANT_SYMBOL_RE.fullmatch(str(s).strip().upper())
        ]
        if not symbols:
            symbols = ["160723"]

        macd_settings = _normalize_macd_settings(
            payload.get("macd_settings") if "macd_settings" in payload else (existing["macd_settings"] if existing else None)
        )
        buy_rule = _normalize_side_rule(
            payload.get("buy_rule") if "buy_rule" in payload else (existing["buy_rule"] if existing else None),
            side="buy",
            default_id=f"{rid}-buy",
            default_title=f"{name}买点",
        )
        sell_rule = _normalize_side_rule(
            payload.get("sell_rule") if "sell_rule" in payload else (existing["sell_rule"] if existing else None),
            side="sell",
            default_id=f"{rid}-sell",
            default_title=f"{name}卖点",
        )
        if buy_rule is None and sell_rule is None:
            raise ValueError("at least one of buy_rule or sell_rule is required")

        criteria = _normalize_eval_criteria(
            payload.get("eval_criteria") if "eval_criteria" in payload else (existing["eval_criteria"] if existing else None)
        )
        pos_sizing = normalize_position_sizing(
            payload.get("position_sizing") if "position_sizing" in payload else (existing.get("position_sizing") if existing else None)
        )
        validate_quant_rule_spec(buy_rule, sell_rule, pos_sizing)
        enabled = bool(payload.get("enabled", existing["enabled"] if existing else True))

        merged_meta: dict[str, Any] = dict((existing or {}).get("meta") or {})
        if isinstance(payload.get("meta"), Mapping):
            merged_meta.update(dict(payload["meta"]))
        for meta_key in (
            "target_symbol",
            "stock_name",
            "raw_strategy_name",
            "submitter_id",
            "submitter_name",
            "submission_id",
            "approved_by",
            "approved_at",
        ):
            if meta_key in payload and payload.get(meta_key) is not None:
                merged_meta[meta_key] = payload.get(meta_key)
        merged_meta.setdefault("target_symbol", symbols[0])
        rule_skeleton = {
            "id": rid,
            "symbols": symbols,
            "target_symbol": merged_meta["target_symbol"],
            "buy_rule": buy_rule,
            "sell_rule": sell_rule,
            "position_sizing": pos_sizing,
        }
        raw_ds_input = (
            payload.get("data_sources")
            if "data_sources" in payload
            else (
                payload.get("meta", {}).get("data_sources")
                if isinstance(payload.get("meta"), Mapping) and "data_sources" in payload["meta"]
                else (existing.get("data_sources") if existing and not any(k in payload for k in ("symbols", "buy_rule", "sell_rule", "position_sizing")) else None)
            )
        )
        normalized_ds = normalize_and_validate_rule_data_sources(
            raw_ds_input,
            rule_skeleton,
            strict_declared=_as_bool_flag(payload.get("strict_data_sources")),
        )
        merged_meta["data_sources"] = normalized_ds

        latest_eval = existing["latest_eval"] if existing else None
        if research_store is not None:
            latest_eval = evaluate_quant_strategy(
                research_store,
                {
                    "id": rid,
                    "symbols": symbols,
                    "macd_settings": macd_settings,
                    "buy_rule": buy_rule,
                    "sell_rule": sell_rule,
                    "position_sizing": pos_sizing,
                    "eval_criteria": criteria,
                },
                symbol=symbols[0],
                capital_cap=float(criteria.get("capital_cap") or 20000.0),
                signal_limit=200,
            )

        now = _now_iso()
        created_at = existing["created_at"] if existing else now
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO quant_strategy_rules(
                       id, name, description, source, symbols_json, macd_json,
                       buy_rule_json, sell_rule_json, position_sizing_json, eval_criteria_json,
                       latest_eval_json, meta_json, enabled, created_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       name=excluded.name,
                       description=excluded.description,
                       source=excluded.source,
                       symbols_json=excluded.symbols_json,
                       macd_json=excluded.macd_json,
                       buy_rule_json=excluded.buy_rule_json,
                       sell_rule_json=excluded.sell_rule_json,
                       position_sizing_json=excluded.position_sizing_json,
                       eval_criteria_json=excluded.eval_criteria_json,
                       latest_eval_json=excluded.latest_eval_json,
                       meta_json=excluded.meta_json,
                       enabled=excluded.enabled,
                       updated_at=excluded.updated_at""",
                (
                    rid,
                    name,
                    description,
                    source,
                    json.dumps(symbols, ensure_ascii=False),
                    json.dumps(macd_settings, ensure_ascii=False),
                    json.dumps(buy_rule, ensure_ascii=False) if buy_rule else None,
                    json.dumps(sell_rule, ensure_ascii=False) if sell_rule else None,
                    json.dumps(pos_sizing, ensure_ascii=False),
                    json.dumps(criteria, ensure_ascii=False),
                    json.dumps(latest_eval, ensure_ascii=False) if latest_eval is not None else None,
                    json.dumps(merged_meta, ensure_ascii=False) if merged_meta else None,
                    1 if enabled else 0,
                    created_at,
                    now,
                ),
            )
        saved = self.get_rule(rid)
        assert saved is not None
        return saved

    def evaluate_and_save_rule(
        self,
        rule_id: str,
        research_store: Any,
        *,
        symbol: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        primary_horizon_minutes: int | None = None,
        min_move_pct: float | None = None,
        data_scope: str | None = None,
        morning_start: str | None = None,
        morning_end: str | None = None,
        max_daily_trades: int | None = None,
        round_trip_fee_pct: float | None = None,
        capital_cap: float | None = None,
        signal_limit: int = 200,
    ) -> dict[str, Any]:
        rule = self.get_rule(rule_id)
        if rule is None:
            raise LookupError("quant rule not found")
        eval_res = evaluate_quant_strategy(
            research_store,
            rule,
            symbol=symbol or rule["symbols"][0],
            start_date=start_date,
            end_date=end_date,
            primary_horizon_minutes=primary_horizon_minutes,
            min_move_pct=min_move_pct,
            data_scope=data_scope,
            morning_start=morning_start,
            morning_end=morning_end,
            max_daily_trades=max_daily_trades,
            round_trip_fee_pct=round_trip_fee_pct,
            capital_cap=capital_cap,
            signal_limit=signal_limit,
        )
        owner_symbols = [
            str(s).strip().upper()
            for s in (rule.get("symbols") or [])
            if _SYMBOL_RE.fullmatch(str(s).strip().upper())
        ]
        target_sym = str(eval_res.get("symbol") or "").strip().upper()
        if (not owner_symbols) or (target_sym in owner_symbols):
            # Store compact 200-signal snapshot in DB, return full requested signals in response
            stored_eval = (
                {**eval_res, "signals": eval_res["signals"][:200]}
                if len(eval_res.get("signals") or []) > 200
                else eval_res
            )
            now = _now_iso()
            with self._lock, self._connection:
                self._connection.execute(
                    "UPDATE quant_strategy_rules SET latest_eval_json=?, updated_at=? WHERE id=?",
                    (json.dumps(stored_eval, ensure_ascii=False), now, rule_id),
                )
        updated_rule = self.get_rule(rule_id)
        assert updated_rule is not None
        return {"rule": updated_rule, "evaluation": eval_res}

    def delete_rule(self, rule_id: str) -> None:
        rule = self.get_rule(rule_id)
        if rule is None:
            raise LookupError("quant rule not found")
        if rule["source"] == "builtin":
            raise ValueError("内置基准规则不可删除")
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM quant_strategy_rules WHERE id=?",
                (rule_id,),
            )


def compact_quant_rule_record(rule: Mapping[str, Any]) -> dict[str, Any]:
    """Strip heavy per-signal / daily-ledger arrays from list payloads while keeping summary KPIs."""
    out = dict(rule)
    latest_eval = rule.get("latest_eval")
    if not isinstance(latest_eval, Mapping):
        return out
    compact_eval = dict(latest_eval)
    signals = latest_eval.get("signals")
    compact_eval["signal_count"] = len(signals) if isinstance(signals, Sequence) and not isinstance(signals, (str, bytes)) else 0
    compact_eval["signals"] = []
    compact_eval["signals_truncated"] = True

    base_t0 = latest_eval.get("base_position_t0")
    if isinstance(base_t0, Mapping):
        compact_t0 = dict(base_t0)
        compact_t0["daily_ledger"] = []
        compact_t0["equity_curve"] = []
        compact_t0["account_events"] = []
        if base_t0.get("account_events"):
            compact_t0["account_events_truncated"] = True
        compact_eval["base_position_t0"] = compact_t0

    cap_sim = latest_eval.get("capital_sim")
    if isinstance(cap_sim, Mapping):
        compact_cap = dict(cap_sim)
        compact_cap["daily_ledger"] = []
        compact_eval["capital_sim"] = compact_cap

    out["latest_eval"] = compact_eval
    return out
