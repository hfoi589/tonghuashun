"""Pure research indicators and replay calculations.

This module has no market-data transport responsibilities. Callers must supply
already-authorized market/research points; it never fetches public or App data.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def _ema(values: Sequence[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (span + 1)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1 - alpha) * result[-1])
    return result


def calculate_macd(
    prices: Sequence[float],
    settings: Mapping[str, int | float],
) -> list[dict[str, float]]:
    """Calculate MACD from explicit dynamic settings."""
    if not prices:
        return []
    short = int(settings["short"])
    long = int(settings["long"])
    signal = int(settings["signal"])
    if short < 1 or long <= short or signal < 1:
        raise ValueError("invalid MACD settings")
    short_ema = _ema(prices, short)
    long_ema = _ema(prices, long)
    diff = [left - right for left, right in zip(short_ema, long_ema)]
    dea = _ema(diff, signal)
    return [
        {
            "diff": round(diff[index], 6),
            "dea": round(dea[index], 6),
            "macd": round(2 * (diff[index] - dea[index]), 6),
        }
        for index in range(len(diff))
    ]


def calculate_replay_performance(
    markers: Sequence[Mapping[str, object]],
    points: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Apply the OPM replay contract to B/S markers.

    Each B marker buys 100 shares. An S marker liquidates the full position and
    is ignored when no position exists. Remaining shares exit at the final
    intraday point.
    """
    ordered = sorted(
        markers,
        key=lambda marker: (
            int(marker.get("index", 2**31 - 1)),
            0 if marker.get("side") == "buy" else 1,
            str(marker.get("time", "")),
        ),
    )
    position = 0
    buy_cost = 0.0
    sell_proceeds = 0.0
    buy_count = 0
    sell_count = 0
    forced_exit: dict[str, object] | None = None
    for marker in ordered:
        try:
            price = float(marker.get("price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        if marker.get("side") == "sell":
            if position <= 0:
                continue
            sell_proceeds += position * price
            sell_count += 1
            position = 0
            continue
        position += 100
        buy_cost += 100 * price
        buy_count += 1
    if position > 0 and points:
        final = points[-1]
        try:
            close_price = float(final.get("price", 0) or 0)
        except (TypeError, ValueError):
            close_price = 0
        if close_price > 0:
            sell_proceeds += position * close_price
            sell_count += 1
            forced_exit = {
                "time": str(final.get("time") or "15:00"),
                "price": close_price,
            }
            position = 0
    profit = sell_proceeds - buy_cost
    return {
        "buy_count": buy_count,
        "sell_count": sell_count,
        "buy_cost": round(buy_cost, 4),
        "sell_proceeds": round(sell_proceeds, 4),
        "profit": round(profit, 4),
        "formula_return_pct": (
            (sell_proceeds / buy_cost - 1) * 100 if buy_cost else None
        ),
        "forced_exit": forced_exit,
    }


def _number(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("%", "")
    for unit in ("万元", "亿元", "万", "亿"):
        if text.endswith(unit):
            text = text[: -len(unit)].strip()
            break
    if not text:
        return None
    try:
        result = float(text)
    except (TypeError, ValueError):
        return None
    return result


def _parse_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _minute(value: object) -> int | None:
    text = str(value or "")
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) < 4:
        return None
    return int(digits[:2]) * 60 + int(digits[2:4])


def _trading_minute(value: object) -> int | None:
    """Map A-share intraday HH:MM to continuous trading minutes (0..240)."""
    clock = _minute(value)
    if clock is None:
        return None
    if 9 * 60 + 30 <= clock <= 11 * 60 + 30:
        return clock - (9 * 60 + 30)
    if 13 * 60 <= clock <= 15 * 60:
        return 120 + (clock - 13 * 60)
    return None


BASE_QUANT_CONDITION_TYPES: tuple[str, ...] = (
    "macd_signal",
    "vwap_deviation_pct",
    "macd_state",
    "time_window",
    "volume_ratio",
    "price_momentum_pct",
    "flow_change_pct",
    "change_pct",
    "price_threshold",
    "swing_reversal",
    "code_filter",
    "brent_overnight_pct",
    "brent_change_pct",
    "brent_momentum_pct",
    "brent_spread_pct",
)

EXTENDED_QUANT_CONDITION_TYPES: tuple[str, ...] = (
    "iopv_premium_pct",
    "fx_spread_pct",
    "xop_macro_pct",
    "sc_crude_pct",
    "peer_spread_pct",
    "l2_flow_level",
    "etf_net_flow",
)

VALID_QUANT_CONDITION_TYPES: frozenset[str] = frozenset(
    (*BASE_QUANT_CONDITION_TYPES, *EXTENDED_QUANT_CONDITION_TYPES)
)

CONDITION_TYPE_TO_PACK: dict[str, str] = {
    "iopv_premium_pct": "iopv_fx",
    "fx_spread_pct": "iopv_fx",
    "xop_macro_pct": "xop_us_ep",
    "sc_crude_pct": "uso_bno_sc_peer",
    "peer_spread_pct": "uso_bno_sc_peer",
    "l2_flow_level": "l2_darkpool_flow",
    "etf_net_flow": "etf_redemption_flow",
}

_REQUIRED_NUMERIC_VALUE_CONDITIONS: frozenset[str] = frozenset(
    {
        "vwap_deviation_pct",
        "macd_state",
        "volume_ratio",
        "price_momentum_pct",
        "flow_change_pct",
        "change_pct",
        "price_threshold",
        "brent_overnight_pct",
        "brent_change_pct",
        "brent_momentum_pct",
        "brent_spread_pct",
        *EXTENDED_QUANT_CONDITION_TYPES,
    }
)

_EXTENDED_ALLOWED_METRICS: dict[str, frozenset[str]] = {
    "iopv_premium_pct": frozenset(
        {"premium_pct", "sp_fx_pct", "coint10_pct", "usdcny_change_pct", "brent_q2y_pct"}
    ),
    "fx_spread_pct": frozenset(
        {"sp_fx_pct", "usdcny", "usdcny_change_pct", "fx_adjusted_nav_change_pct"}
    ),
    "xop_macro_pct": frozenset(
        {
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
        }
    ),
    "sc_crude_pct": frozenset(
        {
            "sc0_change_pct",
            "sc0_intraday_pct",
            "uso_bno_change_pct",
            "dual_oil_premium_pct",
            "sc_brent_spread_pct",
        }
    ),
    "peer_spread_pct": frozenset(
        {"spread_pct", "peer_change_pct", "peer_premium_diff_pct"}
    ),
    "etf_net_flow": frozenset(
        {
            "net_buy_1d_wan",
            "net_buy_3d_wan",
            "net_buy_5d_wan",
            "net_buy_20d_wan",
            "share_scale_yi",
            "net_buy_ratio_pct",
        }
    ),
}


def validate_dsl_condition(
    condition: Mapping[str, Any],
    *,
    prefix: str = "condition",
) -> None:
    """Validate one DSL condition dict and raise ValueError on unknown operators or malformed fields."""
    if not isinstance(condition, Mapping):
        raise ValueError(f"{prefix} must be an object")
    ctype = str(condition.get("type") or "").strip()
    if not ctype:
        raise ValueError(f"{prefix}.type is required")
    if ctype not in VALID_QUANT_CONDITION_TYPES:
        raise ValueError(f"{prefix}.type '{ctype}' is not supported")
    if ctype in _REQUIRED_NUMERIC_VALUE_CONDITIONS and _number(condition.get("value")) is None:
        raise ValueError(f"{prefix} ({ctype}) requires numeric value")
    direction = condition.get("direction")
    if direction is not None and ctype != "flow_change_pct":
        if str(direction).strip().lower() not in {"lte", "gte"}:
            raise ValueError(f"{prefix} ({ctype}).direction must be 'lte' or 'gte'")
    if ctype in _EXTENDED_ALLOWED_METRICS:
        metric = str(condition.get("metric") or "").strip()
        if metric and metric not in _EXTENDED_ALLOWED_METRICS[ctype]:
            raise ValueError(f"{prefix} ({ctype}).metric '{metric}' is not supported")
    if ctype == "l2_flow_level":
        period = str(condition.get("period") or "today").strip()
        if period not in {"today", "three_day", "five_day"}:
            raise ValueError(f"{prefix} (l2_flow_level).period '{period}' must be today|three_day|five_day")
        flow = str(condition.get("flow") or "main_net").strip().lower()
        if flow not in {"main_net", "main_visible", "main_hidden", "retail", "hidden_ratio"}:
            raise ValueError(
                f"{prefix} (l2_flow_level).flow '{flow}' must be main_net|main_visible|main_hidden|retail|hidden_ratio"
            )
    if ctype == "peer_spread_pct":
        peer = str(condition.get("peer") or "auto").strip().upper()
        if peer and peer != "AUTO" and not (len(peer) == 6 and peer.isdigit()):
            raise ValueError(f"{prefix} (peer_spread_pct).peer must be 'auto' or a 6-digit symbol")
    if ctype == "time_window":
        windows = condition.get("windows")
        has_windows = (
            isinstance(windows, Sequence)
            and not isinstance(windows, (str, bytes))
            and len(windows) > 0
        )
        has_start_end = bool(condition.get("start") or condition.get("end"))
        if not has_windows and not has_start_end:
            raise ValueError(f"{prefix} (time_window) requires windows or start/end")


def detect_rule_required_packs(
    rules: Sequence[Mapping[str, Any]],
    position_sizing: Mapping[str, Any] | None = None,
) -> set[str]:
    """Return external/extended data packs needed to evaluate the given rules and conviction tiers."""
    required: set[str] = set()
    candidates: list[Mapping[str, Any]] = []
    for rule in rules:
        if isinstance(rule, Mapping):
            candidates.append(rule)
    if isinstance(position_sizing, Mapping):
        for key in ("conviction_tiers", "sell_conviction_tiers"):
            for tier in position_sizing.get(key) or []:
                if isinstance(tier, Mapping):
                    candidates.append(tier)
        profile = str(position_sizing.get("engine_profile") or "").strip().lower()
        anchor = str(position_sizing.get("cross_market_anchor") or "").strip().lower()
        if profile in {"v9_plus_aggressive", "v9_plus_balanced"}:
            required.add("iopv_fx")
        if anchor == "xop_us_ep":
            required.update({"iopv_fx", "xop_us_ep"})
        elif anchor == "fof_t0_multisource":
            required.update({"iopv_fx", "uso_bno_sc_peer", "xop_us_ep"})
        elif anchor in {"uso_bno_sc_peer", "fof_crude_core"}:
            required.update({"iopv_fx", "uso_bno_sc_peer"})
    for container in candidates:
        for cond in container.get("conditions") or []:
            if not isinstance(cond, Mapping):
                continue
            ctype = str(cond.get("type") or "").strip()
            pack = CONDITION_TYPE_TO_PACK.get(ctype)
            if pack:
                required.add(pack)
            elif ctype == "flow_change_pct":
                flow = str(cond.get("flow") or "big").strip().lower()
                if flow in {"visible", "main_visible", "hidden", "main_hidden", "retail", "small"}:
                    required.add("l2_darkpool_flow")
    return required


def rule_uses_flow(rules: Sequence[Mapping[str, Any]]) -> bool:
    """Return True when any rule condition depends on intraday fund/large-order flow."""
    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        for cond in rule.get("conditions") or []:
            if not isinstance(cond, Mapping):
                continue
            ctype = str(cond.get("type") or "")
            if ctype in {"flow_change_pct", "l2_flow_level"}:
                return True
            if ctype == "swing_reversal" and str(cond.get("flowFilter") or "off") in {
                "increase",
                "decrease",
            }:
                return True
    return False


def _macd_signal(
    points: Sequence[Mapping[str, object]],
    index: int,
    marker_threshold: float,
) -> str | None:
    if index <= 0 or index >= len(points):
        return None
    previous = points[index - 1]
    current = points[index]
    previous_delta = (_number(previous.get("diff")) or 0) - (_number(previous.get("dea")) or 0)
    current_delta = (_number(current.get("diff")) or 0) - (_number(current.get("dea")) or 0)
    if abs(_number(current.get("macd")) or 0) < marker_threshold:
        return None
    if previous_delta <= 0 < current_delta:
        return "red"
    if previous_delta >= 0 > current_delta:
        return "green"
    return None


def _resolve_flow_value(point: Mapping[str, object], flow: str) -> float | None:
    kind = str(flow or "big").strip().lower()
    if kind in {"retail", "small"}:
        for key in ("l2_today_retail_wan", "retail_count", "small_net_wan", "retail_inflow"):
            val = _number(point.get(key))
            if val is not None:
                return val
        return None
    if kind in {"hidden", "main_hidden"}:
        for key in ("l2_today_hidden_wan", "main_hidden_inflow"):
            val = _number(point.get(key))
            if val is not None:
                return val
        return None
    if kind in {"visible", "main_visible"}:
        for key in ("l2_today_visible_wan", "main_visible_inflow"):
            val = _number(point.get(key))
            if val is not None:
                return val
        return None
    if kind == "mid":
        for key in ("mid_order_net", "mid_net_wan"):
            val = _number(point.get(key))
            if val is not None:
                return val
        return None
    for key in ("large_order_net", "large_order_amount", "big_net_wan", "l2_today_main_net_wan", "main_net_inflow"):
        val = _number(point.get(key))
        if val is not None:
            return val
    return None


def _flow_change(
    points: Sequence[Mapping[str, object]],
    index: int,
    minutes: int,
    flow: str,
) -> float | None:
    current_value = _resolve_flow_value(points[index], flow)
    current_minute = _minute(points[index].get("time"))
    if current_value is None or current_minute is None:
        return None
    target = current_minute - minutes
    prior: Mapping[str, object] | None = None
    for candidate in reversed(points[:index]):
        candidate_minute = _minute(candidate.get("time"))
        if candidate_minute is None:
            continue
        prior = candidate
        if candidate_minute <= target:
            break
    if prior is None:
        return None
    prior_value = _resolve_flow_value(prior, flow)
    if prior_value in (None, 0):
        return None
    return (current_value - prior_value) / abs(prior_value) * 100


def build_swing_reversal_markers(
    points: Sequence[Mapping[str, object]],
    condition: Mapping[str, Any],
) -> list[dict[str, object]]:
    """Port of OPM's T0 high-frequency swing reversal / trend marker engine."""
    if not points:
        return []
    reversal = float(_number(condition.get("reversalPct")) or 0.58) / 100
    raw_drawdown = _number(condition.get("drawdownPct"))
    drawdown = (raw_drawdown if raw_drawdown is not None else 0.0) / 100
    raw_bounce = _number(condition.get("bouncePct"))
    bounce = (raw_bounce / 100) if raw_bounce is not None else reversal
    take_profit = float(_number(condition.get("takeProfitPct")) or 0.4) / 100
    stop_loss = float(_number(condition.get("stopLossPct")) or 0.0) / 100
    raw_pullback = _number(condition.get("pullbackPct"))
    pullback_threshold = (raw_pullback / 100) if raw_pullback is not None else reversal
    gap = max(0, int(_number(condition.get("gapMinutes")) or 2))
    start_index = max(0, int(_number(condition.get("startIndex")) or 0))
    reset_low_on_high = _parse_bool(condition.get("resetLowOnHigh"), False)
    raw_side = str(condition.get("side") or "").strip().lower()
    target_side = raw_side if raw_side in {"buy", "sell"} else None
    prices = [float(_number(point.get("price")) or 0.0) for point in points]

    def extra_filters_match(idx: int) -> bool:
        point = points[idx]
        volume_filter = str(condition.get("volumeFilter") or "off")
        if volume_filter in {"gte", "lte"}:
            start = max(0, idx - 20)
            previous = points[start:idx]
            avg_vol = (
                sum(float(_number(item.get("volume") or item.get("volume_shares")) or 0.0) for item in previous)
                / len(previous)
                if previous
                else 0.0
            )
            curr_vol = float(_number(point.get("volume") or point.get("volume_shares")) or 0.0)
            ratio = (curr_vol / avg_vol) if avg_vol > 0 else 1.0
            threshold = float(_number(condition.get("volumeRatio")) or 1.0)
            if volume_filter == "gte" and ratio < threshold:
                return False
            if volume_filter == "lte" and ratio > threshold:
                return False

        macd_filter = str(condition.get("macdFilter") or "off")
        if macd_filter in {"positive", "negative", "red"}:
            curr_delta = (_number(point.get("diff")) or 0.0) - (_number(point.get("dea")) or 0.0)
            prev_point = points[idx - 1] if idx > 0 else point
            prev_delta = (_number(prev_point.get("diff")) or 0.0) - (_number(prev_point.get("dea")) or 0.0)
            red = idx > 0 and prev_delta <= 0 < curr_delta
            if macd_filter == "positive" and curr_delta <= 0:
                return False
            if macd_filter == "negative" and curr_delta > 0:
                return False
            if macd_filter == "red" and not (red or curr_delta > 0):
                return False

        flow_filter = str(condition.get("flowFilter") or "off")
        if flow_filter in {"increase", "decrease"}:
            flow_kind = str(condition.get("flow") or "big")
            flow_minutes = max(1, int(_number(condition.get("flowMinutes")) or 1))
            change = _flow_change(points, idx, flow_minutes, flow_kind)
            threshold = float(_number(condition.get("flowChangePct")) or 0.0)
            if change is None:
                return False
            if flow_filter == "increase" and change < threshold:
                return False
            if flow_filter == "decrease" and change > -threshold:
                return False
        return True

    if _parse_bool(condition.get("trendMode"), False):
        trend_index = min(max(1, int(_number(condition.get("trendMinutes")) or 45)), len(points) - 1)
        open_price = prices[0]
        trend_price = prices[trend_index]
        ma_start_index = max(0, trend_index - 20)
        ma_now_vals = prices[max(0, trend_index - 19) : trend_index + 1]
        ma_start_vals = prices[max(0, ma_start_index - 19) : ma_start_index + 1]
        ma_now = sum(ma_now_vals) / len(ma_now_vals) if ma_now_vals else 0.0
        ma_start = sum(ma_start_vals) / len(ma_start_vals) if ma_start_vals else ma_now
        trend_ret = (trend_price / open_price - 1) if open_price > 0 else 0.0
        ma_ret = (ma_now / ma_start - 1) if ma_start > 0 else 0.0
        trend_up = (
            trend_ret >= float(_number(condition.get("trendUpPct")) or 0.0) / 100
            and ma_ret >= float(_number(condition.get("trendMaPct")) or 0.0) / 100
        )
        if trend_up:
            markers: list[dict[str, object]] = []
            trend_bounce = float(_number(condition.get("trendBouncePct")) or 0.0) / 100
            trend_min_hold = max(0, int(_number(condition.get("trendMinHoldMinutes")) or 30))
            trend_pullback = float(_number(condition.get("trendPullbackPct")) or 3.0) / 100
            trend_macd_filter = str(condition.get("trendMacdFilter") or "off")
            if target_side == "sell":
                high = prices[trend_index]
                armed = False
                entry_index = trend_index
                hold_low = high
                for idx in range(trend_index, len(points)):
                    price = prices[idx]
                    if price <= 0:
                        continue
                    if not armed:
                        if price > high:
                            high = price
                        dropped = (high / price - 1) if price > 0 else 0.0
                        macd_ok = True
                        if trend_macd_filter == "positive":
                            delta = (_number(points[idx].get("diff")) or 0.0) - (_number(points[idx].get("dea")) or 0.0)
                            macd_ok = delta > 0
                        if dropped >= trend_bounce and macd_ok:
                            armed = True
                            entry_index = idx
                            hold_low = price
                            markers.append({"index": idx, "side": "sell", "price": price})
                        continue
                    if price < hold_low:
                        hold_low = price
                    rebound = (price / hold_low - 1) if hold_low > 0 else 0.0
                    if (idx - entry_index >= trend_min_hold and rebound >= trend_pullback) or idx == len(points) - 1:
                        break
                return markers
            low = prices[trend_index]
            bought = False
            entry_index = trend_index
            hold_high = low
            for idx in range(trend_index, len(points)):
                price = prices[idx]
                if price <= 0:
                    continue
                if not bought:
                    if price < low:
                        low = price
                    bounced = (price / low - 1) if low > 0 else 0.0
                    macd_ok = True
                    if trend_macd_filter == "positive":
                        delta = (_number(points[idx].get("diff")) or 0.0) - (_number(points[idx].get("dea")) or 0.0)
                        macd_ok = delta > 0
                    if bounced >= trend_bounce and macd_ok:
                        bought = True
                        entry_index = idx
                        hold_high = price
                        if target_side in {None, "buy"}:
                            markers.append({"index": idx, "side": "buy", "price": price})
                    continue
                if price > hold_high:
                    hold_high = price
                pullback = (hold_high / price - 1) if price > 0 else 0.0
                if (idx - entry_index >= trend_min_hold and pullback >= trend_pullback) or idx == len(points) - 1:
                    if target_side in {None, "sell"}:
                        markers.append({"index": idx, "side": "sell", "price": price})
                    break
            return markers

    holding = False
    entry = 0.0
    last_trade_index = -999
    recent_high = prices[0]
    recent_low = recent_high
    hold_high = recent_high
    markers = []
    if target_side == "sell":
        for idx, price in enumerate(prices):
            if price <= 0:
                continue
            if not holding:
                if price < recent_low:
                    recent_low = price
                if price > recent_high:
                    recent_high = price
                rise = ((recent_high - recent_low) / recent_low) if recent_low > 0 else 0.0
                dropped = (recent_high / price - 1) if price > 0 else 0.0
                if (
                    idx >= start_index
                    and idx > 0
                    and idx - last_trade_index >= gap
                    and rise >= drawdown
                    and dropped >= bounce
                    and extra_filters_match(idx)
                ):
                    holding = True
                    entry = price
                    hold_high = price
                    last_trade_index = idx
                    recent_high = price
                    recent_low = price
                    markers.append({"index": idx, "side": "sell", "price": price})
                continue
            if price < hold_high:
                hold_high = price
            rebound = (price / hold_high - 1) if hold_high > 0 else 0.0
            should_release = idx - last_trade_index >= gap and rebound >= pullback_threshold
            if take_profit and (entry / price - 1) >= take_profit:
                should_release = True
            if stop_loss and (entry / price - 1) <= -stop_loss:
                should_release = True
            if should_release or idx == len(points) - 1:
                holding = False
                last_trade_index = idx
                recent_high = price
                recent_low = price
        return markers

    for idx, price in enumerate(prices):
        if price <= 0:
            continue
        if not holding:
            if price > recent_high:
                recent_high = price
                if reset_low_on_high:
                    recent_low = price
            if price < recent_low:
                recent_low = price
            draw = ((recent_high - recent_low) / recent_high) if recent_high > 0 else 0.0
            bounced = (price / recent_low - 1) if recent_low > 0 else 0.0
            if (
                idx >= start_index
                and idx > 0
                and idx - last_trade_index >= gap
                and draw >= drawdown
                and bounced >= bounce
                and extra_filters_match(idx)
            ):
                holding = True
                entry = price
                hold_high = price
                last_trade_index = idx
                recent_high = price
                recent_low = price
                if target_side in {None, "buy"}:
                    markers.append({"index": idx, "side": "buy", "price": price})
            continue
        if price > hold_high:
            hold_high = price
        ret = (price / entry - 1) if entry > 0 else 0.0
        pullback = (hold_high / price - 1) if price > 0 else 0.0
        should_sell = idx - last_trade_index >= gap and pullback >= pullback_threshold
        if take_profit and ret >= take_profit:
            should_sell = True
        if stop_loss and ret <= -stop_loss:
            should_sell = True
        if should_sell or idx == len(points) - 1:
            holding = False
            last_trade_index = idx
            recent_high = price
            recent_low = price
            if target_side in {None, "sell"}:
                markers.append({"index": idx, "side": "sell", "price": price})
    return markers


def _condition_matches(
    condition: Mapping[str, Any],
    *,
    symbol: str,
    points: Sequence[Mapping[str, object]],
    index: int,
    marker_threshold: float,
    swing_lookup: set[tuple[int, str]] | None = None,
) -> tuple[bool, str]:
    kind = str(condition.get("type") or "")
    current = points[index]
    if kind == "code_filter":
        codes = {str(value).strip() for value in condition.get("codes", []) if str(value).strip()}
        matched = (symbol in codes) if codes else True
        return matched, f"股票代码属于 {'、'.join(sorted(codes)) if codes else '全部自选'}"
    if kind == "price_threshold":
        price = _number(current.get("price"))
        target = _number(condition.get("value"))
        if price is None or target is None:
            return False, "最新价或阈值无效"
        is_lte = condition.get("direction") == "lte"
        matched = price <= target if is_lte else price >= target
        return matched, f"最新价 {price:.3f} {'<=' if is_lte else '>='} {target:g}"
    if kind == "change_pct":
        change = _number(current.get("change_pct"))
        target = _number(condition.get("value"))
        if change is None or target is None:
            return False, "涨跌幅或阈值无效"
        is_lte = condition.get("direction") == "lte"
        matched = change <= target if is_lte else change >= target
        return matched, f"涨跌幅 {change:+.2f}% {'<=' if is_lte else '>='} {target:g}%"
    if kind == "macd_signal":
        signal = _macd_signal(points, index, marker_threshold)
        wanted = str(condition.get("signal") or "both")
        matched = signal is not None and wanted in {"both", signal}
        label = "红点" if signal == "red" else ("绿点" if signal == "green" else "未交叉")
        return matched, f"MACD {label}"
    if kind == "flow_change_pct":
        flow_kind = str(condition.get("flow") or "big")
        minutes = max(1, int(_number(condition.get("minutes")) or 1))
        change = _flow_change(points, index, minutes, flow_kind)
        target = _number(condition.get("value"))
        flow_labels = {
            "big": "大单净额",
            "mid": "中单净额",
            "retail": "散户数",
            "small": "小单/散户",
            "hidden": "主力暗盘",
            "main_hidden": "主力暗盘",
        }
        flow_label = flow_labels.get(flow_kind, "大单净额")
        if change is None or target is None:
            return False, f"{flow_label} {minutes}分钟窗口数据不足"
        is_dec = condition.get("direction") == "decrease"
        matched = change <= -target if is_dec else change >= target
        verb = "减少" if is_dec else "增长"
        return matched, f"{flow_label} {minutes}分钟窗口{verb} {abs(change):.2f}%"
    if kind == "swing_reversal":
        side = "sell" if str(condition.get("side")) == "sell" else "buy"
        if swing_lookup is not None:
            matched = (index, side) in swing_lookup
        else:
            markers = build_swing_reversal_markers(points, condition)
            matched = any(int(m["index"]) == index and m["side"] == side for m in markers)
        return matched, f"T0波段{'卖点' if side == 'sell' else '买点'}{'触发' if matched else '未触发'}"
    if kind == "vwap_deviation_pct":
        price = _number(current.get("price"))
        avg_price = _number(current.get("average_price"))
        target = _number(condition.get("value"))
        if price is None or avg_price is None or avg_price <= 0 or target is None:
            return False, "分时均价偏离数据或阈值无效"
        dev = (price - avg_price) / avg_price * 100.0
        is_lte = str(condition.get("direction") or "lte").lower() == "lte"
        matched = dev <= target if is_lte else dev >= target
        return matched, f"均价偏离 {dev:+.3f}% {'<=' if is_lte else '>='} {target:g}%"
    if kind == "macd_state":
        metric = str(condition.get("metric") or "diff").strip().lower()
        raw_val = (
            abs(_number(current.get("macd")) or 0.0)
            if metric == "abs_macd" and _number(current.get("macd")) is not None
            else _number(current.get(metric if metric in {"diff", "dea", "macd"} else "diff"))
        )
        target = _number(condition.get("value"))
        if target is None:
            target = 0.0
        if raw_val is None:
            return False, "MACD 状态指标无效"
        is_lte = str(condition.get("direction") or "gte").lower() == "lte"
        matched = raw_val <= target if is_lte else raw_val >= target
        return matched, f"{metric} {raw_val:.5f} {'<=' if is_lte else '>='} {target:g}"
    if kind == "time_window":
        cur_min = _minute(current.get("time"))
        if cur_min is None:
            return False, "当前分钟时间无效"
        windows = condition.get("windows")
        parsed_windows: list[tuple[int, int]] = []
        if isinstance(windows, Sequence) and not isinstance(windows, (str, bytes)):
            for w in windows:
                if isinstance(w, Sequence) and len(w) >= 2:
                    s_min, e_min = _minute(w[0]), _minute(w[1])
                    if s_min is not None and e_min is not None:
                        parsed_windows.append((min(s_min, e_min), max(s_min, e_min)))
        elif condition.get("start") or condition.get("end"):
            s_min = _minute(condition.get("start") or "09:30")
            e_min = _minute(condition.get("end") or "15:00")
            if s_min is not None and e_min is not None:
                parsed_windows.append((min(s_min, e_min), max(s_min, e_min)))
        if not parsed_windows:
            return False, "时间窗口配置无效"
        matched = any(s <= cur_min <= e for s, e in parsed_windows)
        return matched, f"时间 {current.get('time')} {'命中' if matched else '不在'}目标时段"
    if kind == "volume_ratio":
        lookback = max(2, min(120, int(_number(condition.get("lookback")) or 20)))
        curr_vol = _number(current.get("volume") or current.get("volume_shares"))
        prev_vols = [
            v
            for item in points[max(0, index - lookback) : index]
            if (v := _number(item.get("volume") or item.get("volume_shares"))) is not None and v >= 0
        ]
        target = _number(condition.get("value"))
        if curr_vol is None or not prev_vols or target is None:
            return False, "成交量均量基线不足"
        avg_vol = sum(prev_vols) / len(prev_vols)
        if avg_vol <= 0:
            return False, "成交量均量为 0"
        ratio = curr_vol / avg_vol
        is_lte = str(condition.get("direction") or "gte").lower() == "lte"
        matched = ratio <= target if is_lte else ratio >= target
        return matched, f"量比 {ratio:.2f}x {'<=' if is_lte else '>='} {target:g}x"
    if kind == "price_momentum_pct":
        minutes = max(1, min(240, int(_number(condition.get("minutes")) or 5)))
        curr_price = _number(current.get("price"))
        curr_tm = _trading_minute(current.get("time"))
        target = _number(condition.get("value"))
        if curr_price is None or curr_price <= 0 or target is None:
            return False, "价格动量数据无效"
        prior_price: float | None = None
        if curr_tm is not None:
            target_tm = curr_tm - minutes
            for candidate in reversed(points[:index]):
                cand_tm = _trading_minute(candidate.get("time"))
                cand_p = _number(candidate.get("price"))
                if cand_tm is None or cand_p is None or cand_p <= 0:
                    continue
                prior_price = cand_p
                if cand_tm <= target_tm:
                    break
        elif index - minutes >= 0:
            prior_price = _number(points[index - minutes].get("price"))
        if prior_price is None or prior_price <= 0:
            return False, f"{minutes}分钟动量前值不足"
        mom = (curr_price - prior_price) / prior_price * 100.0
        is_lte = str(condition.get("direction") or "gte").lower() == "lte"
        matched = mom <= target if is_lte else mom >= target
        return matched, f"{minutes}分钟动量 {mom:+.3f}% {'<=' if is_lte else '>='} {target:g}%"
    if kind == "brent_overnight_pct":
        val = _number(current.get("brn_overnight_pct"))
        target = _number(condition.get("value"))
        if val is None or target is None:
            return False, "布伦特隔夜涨跌幅数据或阈值无效"
        is_lte = str(condition.get("direction") or "gte").lower() == "lte"
        matched = val <= target if is_lte else val >= target
        return matched, f"BRN0Y隔夜 {val:+.2f}% {'<=' if is_lte else '>='} {target:g}%"
    if kind == "brent_change_pct":
        anchor = str(condition.get("anchor") or "prev_1500").strip().lower()
        field = "brn_intraday_pct" if anchor == "open_0930" else "brn_from_prev_close_pct"
        val = _number(current.get(field))
        target = _number(condition.get("value"))
        if val is None or target is None:
            return False, "布伦特累计涨跌幅数据或阈值无效"
        is_lte = str(condition.get("direction") or "gte").lower() == "lte"
        matched = val <= target if is_lte else val >= target
        label = "BRN0Y盘中" if anchor == "open_0930" else "BRN0Y较昨15:00"
        return matched, f"{label} {val:+.2f}% {'<=' if is_lte else '>='} {target:g}%"
    if kind == "brent_momentum_pct":
        minutes = max(1, min(240, int(_number(condition.get("minutes")) or 5)))
        curr_b = _number(current.get("brn_price"))
        curr_tm = _trading_minute(current.get("time"))
        target = _number(condition.get("value"))
        if curr_b is None or curr_b <= 0 or target is None:
            return False, "布伦特动量数据无效"
        prior_b: float | None = None
        if curr_tm is not None:
            target_tm = curr_tm - minutes
            for candidate in reversed(points[:index]):
                cand_tm = _trading_minute(candidate.get("time"))
                cand_b = _number(candidate.get("brn_price"))
                if cand_tm is None or cand_b is None or cand_b <= 0:
                    continue
                prior_b = cand_b
                if cand_tm <= target_tm:
                    break
        elif index - minutes >= 0:
            prior_b = _number(points[index - minutes].get("brn_price"))
        if prior_b is None or prior_b <= 0:
            return False, f"BRN0Y {minutes}分钟动量前值不足"
        mom = (curr_b - prior_b) / prior_b * 100.0
        is_lte = str(condition.get("direction") or "gte").lower() == "lte"
        matched = mom <= target if is_lte else mom >= target
        return matched, f"BRN0Y {minutes}m动量 {mom:+.3f}% {'<=' if is_lte else '>='} {target:g}%"
    if kind == "brent_spread_pct":
        val = _number(current.get("brn_spread_pct"))
        target = _number(condition.get("value"))
        if val is None or target is None:
            return False, "布伦特跨市场价差数据或阈值无效"
        is_lte = str(condition.get("direction") or "lte").lower() == "lte"
        matched = val <= target if is_lte else val >= target
        return matched, f"A股-BRN0Y价差 {val:+.2f}% {'<=' if is_lte else '>='} {target:g}%"
    if kind == "iopv_premium_pct":
        metric = str(condition.get("metric") or "premium_pct").strip()
        field_map = {
            "premium_pct": ("iopv_premium_pct", "汇调IOPV溢价率", "%"),
            "sp_fx_pct": ("sp_fx_pct", "汇调跨市场价差sp_fx", "%"),
            "coint10_pct": ("coint10_pct", "10日协整偏离coint10", "%"),
            "usdcny_change_pct": ("usdcny_change_pct", "离岸汇率波动", "%"),
            "brent_q2y_pct": ("brent_q2y_pct", "布油2年分位", "%"),
        }
        field, label, unit = field_map.get(metric, field_map["premium_pct"])
        val = _number(current.get(field))
        target = _number(condition.get("value"))
        if val is None or target is None:
            return False, f"{label}数据或阈值无效"
        is_lte = str(condition.get("direction") or "lte").lower() == "lte"
        matched = val <= target if is_lte else val >= target
        return matched, f"{label} {val:+.2f}{unit} {'<=' if is_lte else '>='} {target:g}{unit}"
    if kind == "fx_spread_pct":
        metric = str(condition.get("metric") or "sp_fx_pct").strip()
        field_map = {
            "sp_fx_pct": ("sp_fx_pct", "汇调价差sp_fx", "%"),
            "usdcny": ("usdcny", "离岸汇率USD/CNY", ""),
            "usdcny_change_pct": ("usdcny_change_pct", "汇率日波动", "%"),
            "fx_adjusted_nav_change_pct": ("fx_adjusted_nav_change_pct", "汇调估值涨跌", "%"),
        }
        field, label, unit = field_map.get(metric, field_map["sp_fx_pct"])
        val = _number(current.get(field))
        target = _number(condition.get("value"))
        if val is None or target is None:
            return False, f"{label}数据或阈值无效"
        is_lte = str(condition.get("direction") or "lte").lower() == "lte"
        matched = val <= target if is_lte else val >= target
        fmt = f"{val:.4f}" if metric == "usdcny" else f"{val:+.2f}"
        return matched, f"{label} {fmt}{unit} {'<=' if is_lte else '>='} {target:g}{unit}"
    if kind == "xop_macro_pct":
        metric = str(condition.get("metric") or "xop_change_pct").strip()
        field_map = {
            "xop_change_pct": ("xop_change_pct", "美股XOP涨跌幅"),
            "xle_change_pct": ("xle_change_pct", "美股XLE涨跌幅"),
            "spy_change_pct": ("spy_change_pct", "美股SPY涨跌幅"),
            "iwm_change_pct": ("iwm_change_pct", "美股IWM涨跌幅"),
            "cl_change_pct": ("cl_change_pct", "WTI原油涨跌幅"),
            "ho_crack_change_pct": ("ho_crack_change_pct", "取暖油裂解价差"),
            "ng_change_pct": ("ng_change_pct", "天然气NG涨跌幅"),
            "es_change_pct": ("es_change_pct", "标普期货ES涨跌幅"),
            "xop_synthetic_premium_pct": ("xop_synthetic_premium_pct", "XOP合成净值溢价率"),
            "daytime_lead_pct": ("daytime_lead_pct", "美股能源日盘领先指标"),
            "daytime_crack_pct": ("daytime_crack_pct", "日盘裂解价差变化"),
        }
        field, label = field_map.get(metric, field_map["xop_change_pct"])
        val = _number(current.get(field))
        target = _number(condition.get("value"))
        if val is None or target is None:
            return False, f"{label}数据或阈值无效"
        is_lte = str(condition.get("direction") or "gte").lower() == "lte"
        matched = val <= target if is_lte else val >= target
        return matched, f"{label} {val:+.2f}% {'<=' if is_lte else '>='} {target:g}%"
    if kind == "sc_crude_pct":
        metric = str(condition.get("metric") or "sc0_change_pct").strip()
        field_map = {
            "sc0_change_pct": ("sc0_change_pct", "沪油SC0较前收"),
            "sc0_intraday_pct": ("sc0_intraday_pct", "沪油SC0日内涨跌"),
            "uso_bno_change_pct": ("uso_bno_change_pct", "USO+BNO双油隔夜"),
            "dual_oil_premium_pct": ("dual_oil_premium_pct", "双油合成溢价率"),
            "sc_brent_spread_pct": ("sc_brent_spread_pct", "沪油-布油价差"),
        }
        field, label = field_map.get(metric, field_map["sc0_change_pct"])
        val = _number(current.get(field))
        target = _number(condition.get("value"))
        if val is None or target is None:
            return False, f"{label}数据或阈值无效"
        is_lte = str(condition.get("direction") or "gte").lower() == "lte"
        matched = val <= target if is_lte else val >= target
        return matched, f"{label} {val:+.2f}% {'<=' if is_lte else '>='} {target:g}%"
    if kind == "peer_spread_pct":
        peer = str(condition.get("peer") or "auto").strip().upper()
        metric = str(condition.get("metric") or "spread_pct").strip()
        candidate_fields: list[str] = []
        if peer and peer != "AUTO":
            if metric == "peer_change_pct":
                candidate_fields.append(f"peer_{peer}_change_pct")
            elif metric == "peer_premium_diff_pct":
                candidate_fields.append(f"peer_{peer}_premium_diff_pct")
            else:
                candidate_fields.append(f"peer_{peer}_spread_pct")
        default_field_map = {
            "spread_pct": ("peer_spread_pct", "同业相对价差"),
            "peer_change_pct": ("peer_change_pct", "同业标的涨跌幅"),
            "peer_premium_diff_pct": ("peer_premium_diff_pct", "同业汇调溢价差"),
        }
        fallback_field, label = default_field_map.get(metric, default_field_map["spread_pct"])
        candidate_fields.append(fallback_field)
        val = None
        for key in candidate_fields:
            val = _number(current.get(key))
            if val is not None:
                break
        target = _number(condition.get("value"))
        if val is None or target is None:
            return False, f"{label}数据或阈值无效"
        is_lte = str(condition.get("direction") or "lte").lower() == "lte"
        matched = val <= target if is_lte else val >= target
        peer_tag = f"[{peer}]" if peer and peer != "AUTO" else ""
        return matched, f"{label}{peer_tag} {val:+.2f}% {'<=' if is_lte else '>='} {target:g}%"
    if kind == "l2_flow_level":
        period = str(condition.get("period") or "today").strip()
        flow = str(condition.get("flow") or "main_net").strip().lower()
        period_prefix = {"today": "today", "three_day": "3d", "five_day": "5d"}.get(period, "today")
        period_label = {"today": "当日", "three_day": "3日", "five_day": "5日"}.get(period, "当日")
        flow_meta = {
            "main_net": ("main_net_wan", "主力净流入", "万"),
            "main_visible": ("visible_wan", "主力明盘", "万"),
            "main_hidden": ("hidden_wan", "主力暗盘", "万"),
            "retail": ("retail_wan", "散户净流入", "万"),
            "hidden_ratio": ("hidden_ratio_pct", "暗盘占比", "%"),
        }
        suffix_key, flow_label, unit = flow_meta.get(flow, flow_meta["main_net"])
        field = f"l2_{period_prefix}_{suffix_key}"
        val = _number(current.get(field))
        if val is None and period == "today":
            raw_fallback = {
                "main_net": "main_net_inflow",
                "main_visible": "main_visible_inflow",
                "main_hidden": "main_hidden_inflow",
                "retail": "retail_inflow",
            }.get(flow)
            if raw_fallback:
                val = _number(current.get(raw_fallback))
        target = _number(condition.get("value"))
        if val is None or target is None:
            return False, f"L2 {period_label}{flow_label}数据或阈值无效"
        is_lte = str(condition.get("direction") or "gte").lower() == "lte"
        matched = val <= target if is_lte else val >= target
        return matched, f"L2 {period_label}{flow_label} {val:+.2f}{unit} {'<=' if is_lte else '>='} {target:g}{unit}"
    if kind == "etf_net_flow":
        metric = str(condition.get("metric") or "net_buy_1d_wan").strip()
        field_map = {
            "net_buy_1d_wan": ("etf_net_buy_1d_wan", "ETF前1日净申购", "万"),
            "net_buy_3d_wan": ("etf_net_buy_3d_wan", "ETF近3日净申购", "万"),
            "net_buy_5d_wan": ("etf_net_buy_5d_wan", "ETF近5日净申购", "万"),
            "net_buy_20d_wan": ("etf_net_buy_20d_wan", "ETF近20日净申购", "万"),
            "share_scale_yi": ("etf_share_scale_yi", "ETF基金规模", "亿"),
            "net_buy_ratio_pct": ("etf_net_buy_ratio_pct", "ETF申赎净流入率", "%"),
        }
        field, label, unit = field_map.get(metric, field_map["net_buy_1d_wan"])
        val = _number(current.get(field))
        target = _number(condition.get("value"))
        if val is None or target is None:
            return False, f"{label}数据或阈值无效"
        is_lte = str(condition.get("direction") or "gte").lower() == "lte"
        matched = val <= target if is_lte else val >= target
        return matched, f"{label} {val:+.2f}{unit} {'<=' if is_lte else '>='} {target:g}{unit}"
    return False, "未知条件"


def evaluate_rule_detail(
    symbol: str,
    points: Sequence[Mapping[str, object]],
    rule: Mapping[str, Any],
    *,
    marker_threshold: float = 0,
    index: int | None = None,
) -> dict[str, Any]:
    """Evaluate one rule at a specific minute (defaults to latest point) with human-readable details."""
    if not points:
        return {
            "matched": False,
            "condition_results": [],
            "summary": "暂无分时数据",
            "time": None,
            "price": None,
        }
    target_index = (len(points) - 1) if index is None else max(0, min(len(points) - 1, index))
    conditions = [c for c in rule.get("conditions", []) if isinstance(c, Mapping)]
    joiner = "or" if rule.get("joiner") == "or" else "and"
    results = []
    summary_parts = []
    for cond_idx, condition in enumerate(conditions):
        swing_lookup = None
        if condition.get("type") == "swing_reversal":
            swing_lookup = {
                (int(m["index"]), str(m["side"]))
                for m in build_swing_reversal_markers(points, condition)
            }
        matched, detail = _condition_matches(
            condition,
            symbol=symbol,
            points=points,
            index=target_index,
            marker_threshold=marker_threshold,
            swing_lookup=swing_lookup,
        )
        results.append(
            {
                "id": str(condition.get("id") or f"cond-{cond_idx}"),
                "type": str(condition.get("type") or ""),
                "matched": matched,
                "detail": detail,
            }
        )
        if condition.get("type") != "code_filter":
            summary_parts.append(detail)
    rule_matched = any(r["matched"] for r in results) if joiner == "or" else (bool(results) and all(r["matched"] for r in results))
    join_text = " OR " if joiner == "or" else " AND "
    point = points[target_index]
    return {
        "matched": rule_matched,
        "condition_results": results,
        "summary": join_text.join(summary_parts) if summary_parts else "条件已评估",
        "time": str(point.get("time") or ""),
        "price": _number(point.get("price")),
    }


def build_replay_markers(
    symbol: str,
    points: Sequence[Mapping[str, object]],
    rules: Sequence[Mapping[str, Any]],
    *,
    marker_threshold: float = 0,
) -> list[dict[str, object]]:
    """Evaluate enabled rules against one symbol's minute history."""
    markers: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    for rule in rules:
        if not rule.get("enabled"):
            continue
        conditions = [condition for condition in rule.get("conditions", []) if isinstance(condition, Mapping)]
        if not conditions:
            continue
        joiner = "or" if rule.get("joiner") == "or" else "and"
        cooldown_minutes = max(0, int(_number(rule.get("cooldown_minutes")) or 0))
        last_hit_tm: int | None = None
        last_hit_index: int | None = None
        swing_lookups: dict[int, set[tuple[int, str]]] = {}
        for cond_idx, condition in enumerate(conditions):
            if condition.get("type") == "swing_reversal":
                swing_lookups[cond_idx] = {
                    (int(m["index"]), str(m["side"]))
                    for m in build_swing_reversal_markers(points, condition)
                }
        for index, point in enumerate(points):
            matches = [
                _condition_matches(
                    condition,
                    symbol=symbol,
                    points=points,
                    index=index,
                    marker_threshold=marker_threshold,
                    swing_lookup=swing_lookups.get(cond_idx),
                )[0]
                for cond_idx, condition in enumerate(conditions)
            ]
            matched = any(matches) if joiner == "or" else bool(matches) and all(matches)
            if not matched:
                continue
            price = _number(point.get("price"))
            if price is None or price <= 0:
                continue
            curr_tm = _trading_minute(point.get("time"))
            if cooldown_minutes > 0:
                if curr_tm is not None and last_hit_tm is not None:
                    if curr_tm - last_hit_tm < cooldown_minutes:
                        continue
                elif last_hit_index is not None and index - last_hit_index < cooldown_minutes:
                    continue
            side = "sell" if rule.get("side") == "sell" else "buy"
            key = (index, side, str(rule.get("id") or ""))
            if key in seen:
                continue
            seen.add(key)
            last_hit_tm = curr_tm
            last_hit_index = index
            markers.append(
                {
                    "index": index,
                    "time": str(point.get("time") or ""),
                    "price": price,
                    "side": side,
                    "rule_id": str(rule.get("id") or ""),
                    "rule_title": str(rule.get("title") or "提醒"),
                }
            )
    return sorted(markers, key=lambda marker: (int(marker["index"]), 0 if marker["side"] == "buy" else 1))
