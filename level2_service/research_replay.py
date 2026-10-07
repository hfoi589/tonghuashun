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
        "buy_cost": buy_cost,
        "sell_proceeds": sell_proceeds,
        "profit": profit,
        "formula_return_pct": (
            (sell_proceeds / buy_cost - 1) * 100 if buy_cost else None
        ),
        "forced_exit": forced_exit,
    }


def _number(value: object) -> float | None:
    try:
        result = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return result


def _minute(value: object) -> int | None:
    text = str(value or "")
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) < 4:
        return None
    return int(digits[:2]) * 60 + int(digits[2:4])


def _macd_signal(
    points: Sequence[Mapping[str, object]],
    index: int,
    marker_threshold: float,
) -> str | None:
    if index <= 0:
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


def _flow_change(
    points: Sequence[Mapping[str, object]],
    index: int,
    minutes: int,
    field: str,
) -> float | None:
    current_value = _number(points[index].get(field))
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
    prior_value = _number(prior.get(field))
    if prior_value in (None, 0):
        return None
    return (current_value - prior_value) / abs(prior_value) * 100


def _condition_matches(
    condition: Mapping[str, Any],
    *,
    symbol: str,
    points: Sequence[Mapping[str, object]],
    index: int,
    marker_threshold: float,
) -> bool:
    kind = str(condition.get("type") or "")
    current = points[index]
    if kind == "code_filter":
        return symbol in {str(value) for value in condition.get("codes", [])}
    if kind == "price_threshold":
        price = _number(current.get("price"))
        target = _number(condition.get("value"))
        if price is None or target is None:
            return False
        return price <= target if condition.get("direction") == "lte" else price >= target
    if kind == "change_pct":
        change = _number(current.get("change_pct"))
        target = _number(condition.get("value"))
        if change is None or target is None:
            return False
        return change <= target if condition.get("direction") == "lte" else change >= target
    if kind == "macd_signal":
        signal = _macd_signal(points, index, marker_threshold)
        wanted = str(condition.get("signal") or "both")
        return signal is not None and wanted in {"both", signal}
    if kind == "flow_change_pct":
        field = "mid_order_net" if condition.get("flow") == "mid" else "large_order_net"
        change = _flow_change(
            points,
            index,
            max(1, int(condition.get("minutes") or 1)),
            field,
        )
        target = _number(condition.get("value"))
        if change is None or target is None:
            return False
        return change <= -target if condition.get("direction") == "decrease" else change >= target
    return False


def build_replay_markers(
    symbol: str,
    points: Sequence[Mapping[str, object]],
    rules: Sequence[Mapping[str, Any]],
    *,
    marker_threshold: float = 0,
) -> list[dict[str, object]]:
    """Evaluate enabled global rules against one symbol's minute history."""
    markers: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    for rule in rules:
        if not rule.get("enabled"):
            continue
        conditions = [condition for condition in rule.get("conditions", []) if isinstance(condition, Mapping)]
        joiner = "or" if rule.get("joiner") == "or" else "and"
        for index, point in enumerate(points):
            matches = [
                _condition_matches(
                    condition,
                    symbol=symbol,
                    points=points,
                    index=index,
                    marker_threshold=marker_threshold,
                )
                for condition in conditions
            ]
            matched = any(matches) if joiner == "or" else bool(matches) and all(matches)
            if not matched:
                continue
            price = _number(point.get("price"))
            if price is None or price <= 0:
                continue
            side = "sell" if rule.get("side") == "sell" else "buy"
            key = (index, side, str(rule.get("id") or ""))
            if key in seen:
                continue
            seen.add(key)
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
