"""Decoders for the verified pre-open UnifiedRequest responses.

This module only decodes already-decrypted response payloads. It does not
invent request packets or bypass App authentication; the transport layer must
provide a previously validated session contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import Any, Mapping

from .market_data import MarketPhase, MarketPhaseState, MarketSnapshot
from .parsed_values import DirectRequestError, market_code_for_symbol


_TIME = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9](?::[0-5][0-9])?$")
AUCTION_PROTOCOL_ID = 1004
AUCTION_PAGE_ID = 6002


def build_auction_request(symbol: str, *, subscribe: bool = True) -> str:
    """Build the verified ``f2a`` request emitted by the APK."""

    if not re.fullmatch(r"[0-9A-Z]{6}", str(symbol)):
        raise DirectRequestError("AUCTION_REQUEST_INVALID")
    action = "subscribe" if subscribe else "unsubscribe"
    return (
        f"key=dpjjyd_cas_{symbol}\r\n"
        f"action={action}\r\n"
        "data_id_list=5\r\n"
        "stock_list=all\r\n"
        "max_msg_num=61"
    )


def _node(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("data", payload)
    if isinstance(value, Mapping):
        nested = value.get("data")
        return nested if isinstance(nested, Mapping) else value
    if isinstance(value, list) and value and isinstance(value[0], Mapping):
        return value[0]
    return payload


def _decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value).strip().removesuffix("%"))
    except (InvalidOperation, ValueError):
        raise DirectRequestError("AUCTION_RESPONSE_INVALID") from None
    if not parsed.is_finite():
        raise DirectRequestError("AUCTION_RESPONSE_INVALID")
    return parsed


def _fixed(value: object, places: int) -> str:
    return f"{_decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP):.{places}f}"


def _time(value: object) -> str:
    text = str(value or "").strip()
    if not _TIME.fullmatch(text):
        raise DirectRequestError("AUCTION_RESPONSE_INVALID")
    return text[:8]


def _phase_value(value: object, phase_map: Mapping[str, MarketPhase]) -> MarketPhase:
    key = str(value).strip().upper()
    try:
        return MarketPhase(key)
    except ValueError:
        try:
            return phase_map[key]
        except KeyError:
            raise DirectRequestError("AUCTION_RESPONSE_INVALID") from None


def decode_market_phase(
    payload: Mapping[str, Any],
    *,
    phase_map: Mapping[str, MarketPhase] | None = None,
) -> MarketPhaseState:
    """Decode 4051/34834 and server time from a sanitized response object."""

    rows = payload.get("data", payload)
    if isinstance(rows, Mapping):
        rows = rows.get("rows", rows.get("items", []))
    if not isinstance(rows, list):
        raise DirectRequestError("AUCTION_RESPONSE_INVALID")
    values: dict[int, object] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        try:
            data_id = int(row.get("dataId", row.get("data_id")))
        except (TypeError, ValueError):
            continue
        values[data_id] = row.get("value")
    if 34834 not in values:
        raise DirectRequestError("AUCTION_RESPONSE_INVALID")
    server_time = str(values.get(1) or "").strip() or None
    if server_time is not None:
        _time(server_time)
    return MarketPhaseState(
        phase=_phase_value(values[34834], phase_map or {}),
        source="APP_STATUS",
        local_time=server_time or "00:00:00",
        server_time=server_time,
    )


def _identity(symbol: str, market: str) -> None:
    try:
        if market_code_for_symbol(symbol) != market:
            raise ValueError
    except (ValueError, TypeError):
        raise DirectRequestError("AUCTION_RESPONSE_INVALID") from None


def _snapshot(
    *,
    symbol: str,
    market: str,
    name: str | None,
    price: str,
    change_percent: str,
    source: str,
    source_time: str | None,
    phase: MarketPhase,
) -> MarketSnapshot:
    precision = 3 if market in {"20", "36"} else 2
    return MarketSnapshot(
        symbol=symbol,
        name=name,
        market=market,
        sequence=0,
        source_time=source_time,
        collected_at=datetime.now(timezone.utc),
        quote={
            "price": price,
            "change_percent": change_percent,
            "previous_close": None,
            "open": None,
            "high": None,
            "low": None,
            "turnover_rate": None,
            "volume": None,
            "amount": None,
        },
        source=source,
        market_phase=phase,
        market_phase_source="APP_STATUS",
        price_precision=precision,
        capabilities={
            "auction": {"available": True, "reason": None},
            "timeshare": {"available": False, "reason": "AUCTION_PRICE_ONLY"},
        },
        source_errors={
            "market_phase": None,
            "auction_quote": None,
            "core_metrics": None,
            "main_fund_flow": None,
        },
    )


def decode_auction_quote(
    payload: Mapping[str, Any],
    *,
    symbol: str,
    market: str,
) -> MarketSnapshot:
    _identity(symbol, market)
    node = _node(payload)
    actual_symbol = str(node.get("stockcode", node.get("symbol", ""))).strip()
    if actual_symbol != symbol:
        raise DirectRequestError("AUCTION_RESPONSE_INVALID")
    name_value = node.get("stock_name", node.get("name"))
    name = str(name_value).strip() if name_value is not None else None
    price = _fixed(node.get("bidding_direction"), 3 if market in {"20", "36"} else 2)
    change = f"{_fixed(node.get('rise_percent'), 2)}%"
    source_time = _time(node["time"]) if node.get("time") is not None else None
    return _snapshot(
        symbol=symbol,
        market=market,
        name=name or None,
        price=price,
        change_percent=change,
        source="THS_AUCTION",
        source_time=source_time,
        phase=MarketPhase.CALL_AUCTION,
    )


def decode_watchlist_quote(
    payload: Mapping[str, Any],
    *,
    symbol: str,
    market: str,
    phase: MarketPhase = MarketPhase.PREOPEN_QUOTE,
) -> MarketSnapshot:
    _identity(symbol, market)
    node = _node(payload)
    actual_symbol = str(node.get("symbol", node.get("4", ""))).strip()
    if actual_symbol != symbol:
        raise DirectRequestError("AUCTION_RESPONSE_INVALID")
    name = str(node.get("name", node.get("55", ""))).strip()
    if not name:
        raise DirectRequestError("AUCTION_RESPONSE_INVALID")
    precision = 3 if market in {"20", "36"} else 2
    price = _fixed(node.get("price", node.get("10")), precision)
    change = f"{_fixed(node.get('change_percent', node.get('34818')), 2)}%"
    source_time = str(node.get("time", "")).strip() or None
    if source_time is not None:
        _time(source_time)
    return _snapshot(
        symbol=symbol,
        market=market,
        name=name,
        price=price,
        change_percent=change,
        source="THS_DIRECT_QUOTE",
        source_time=source_time,
        phase=phase,
    )
