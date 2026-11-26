"""Public quote, intraday, and K-line providers for the market application."""

from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from threading import Lock, RLock
from typing import Any, Callable, Iterable, Sequence
from urllib.request import Request, urlopen

from .fund_flow_history import extract_quote_trade_date
from .market_data import KlineBar, MarketSeriesPage, MarketSnapshot, TimesharePoint
from .models import FUND_FLOW_METRICS, FUND_FLOW_PERIODS, MetricKind
from .parsed_values import (
    DirectReadOutcome,
    DirectRequestError,
    SymbolLookup,
    is_exchange_fund_symbol,
    sanitized_direct_error_code,
)


class PublicMarketError(DirectRequestError):
    """A public market provider failed with a fixed, response-safe code."""


def _http_get(url: str, timeout: float) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "*/*",
            "Referer": "https://gu.qq.com/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/125.0 Safari/537.36"
            ),
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def _provider_id(identity: SymbolLookup) -> str:
    if identity.market in {"17", "20"}:
        return f"sh{identity.symbol}"
    if identity.market in {"33", "36"}:
        return f"sz{identity.symbol}"
    if identity.market == "151":
        return f"bj{identity.symbol}"
    raise PublicMarketError("PUBLIC_MARKET_SYMBOL_INVALID")


def _precision(identity: SymbolLookup) -> int:
    return 3 if identity.market in {"20", "36"} else 2


def _decimal(value: object) -> Decimal | None:
    text = str(value).strip() if value is not None else ""
    if not text or text in {"-", "--"}:
        return None
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _number(value: Decimal | None, places: int | None = None) -> str | None:
    if value is None:
        return None
    if places is not None:
        value = value.quantize(
            Decimal(1).scaleb(-places),
            rounding=ROUND_HALF_UP,
        )
        return f"{value:.{places}f}"
    integral = value.to_integral_value()
    if value == integral:
        return str(integral)
    return format(value.normalize(), "f")


def _percent(value: object) -> str | None:
    return (
        None
        if (decimal := _decimal(value)) is None
        else f"{decimal.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}%"
    )


def _source_time(value: object) -> str | None:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    if len(digits) < 4:
        return None
    hhmm = digits[-6:-2] if len(digits) >= 6 else digits[-4:]
    hour, minute = int(hhmm[:2]), int(hhmm[2:])
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return None


def _quote_clock_time(value: object) -> str | None:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    if len(digits) >= 6:
        hhmmss = digits[-6:]
        hour, minute, second = int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:6])
        if 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59:
            return f"{hour:02d}:{minute:02d}:{second:02d}"
    return _source_time(value)


def _session_time(value: str) -> str | None:
    compact = value.replace(":", "").strip()
    if not re.fullmatch(r"[0-9]{4}", compact):
        return None
    if not ("0930" <= compact <= "1130" or "1300" <= compact <= "1500"):
        return None
    return f"{compact[:2]}:{compact[2:]}"


def _quote_snapshot(
    identity: SymbolLookup,
    fields: list[str],
    *,
    source: str,
    timeshare: tuple[TimesharePoint, ...] = (),
    source_errors: dict[str, str | None] | None = None,
) -> MarketSnapshot:
    if len(fields) < 39 or fields[2].strip() != identity.symbol:
        raise PublicMarketError("PUBLIC_MARKET_RESPONSE_INVALID")
    # The symbol code is the upstream identity key. Provider display names can
    # legitimately differ after a rename or between public quote sources.
    name = identity.name.strip() or fields[1].strip()
    precision = _precision(identity)
    price = _decimal(fields[3])
    previous_close = _decimal(fields[4])
    open_price = _decimal(fields[5])
    high = _decimal(fields[33])
    low = _decimal(fields[34])
    volume_lots = _decimal(fields[6])
    amount_wan = _decimal(fields[37])
    if any(value is not None and value < 0 for value in (price, previous_close, open_price, high, low, volume_lots, amount_wan)):
        raise PublicMarketError("PUBLIC_MARKET_RESPONSE_INVALID")
    comparable = [
        value
        for value in (price, open_price, high, low)
        if value is not None
    ]
    if (
        high is not None
        and comparable
        and high < max(comparable)
    ) or (
        low is not None
        and comparable
        and low > min(comparable)
    ):
        raise PublicMarketError("PUBLIC_MARKET_RESPONSE_INVALID")
    quote = {
        "price": _number(price, precision),
        "previous_close": _number(previous_close, precision),
        "change_percent": _percent(fields[32]),
        "turnover_rate": _percent(fields[38]),
        "open": _number(open_price, precision),
        "high": _number(high, precision),
        "low": _number(low, precision),
        "volume": _number(
            None if volume_lots is None else volume_lots * Decimal(100)
        ),
        "amount": _number(
            None if amount_wan is None else amount_wan * Decimal(10000)
        ),
        "large_order_net": None,
        "large_order_amount": None,
        "retail_count": None,
        "macdfs": None,
    }
    has_timeshare = bool(timeshare)
    return MarketSnapshot(
        symbol=identity.symbol,
        name=name,
        market=identity.market,
        sequence=0,
        source_time=(timeshare[-1].time if timeshare else _source_time(fields[30])),
        collected_at=datetime.now(timezone.utc),
        quote=quote,
        source=source,
        price_precision=precision,
        timeshare=timeshare,
        capabilities={
            "timeshare": {
                "available": has_timeshare,
                "reason": None if has_timeshare else "PUBLIC_TIMESHARE_UNAVAILABLE",
            },
            "kline": {"available": True, "adjustment": "qfq"},
            "order_book": {
                "available": False,
                "reason": "PUBLIC_LEVEL2_UNAVAILABLE",
            },
            "trades": {
                "available": False,
                "reason": "PUBLIC_LEVEL2_UNAVAILABLE",
            },
            "l2": {
                "available": False,
                "reason": "DIRECT_ENRICHMENT_UNAVAILABLE",
            },
        },
        source_errors=source_errors
        or {
            "tencent_public": None,
            "sina_public": None,
            "core_metrics": None,
            "main_fund_flow": None,
            "market_phase": None,
            "auction_quote": None,
        },
    )


class TencentPublicMarketProvider:
    quote_url = "https://qt.gtimg.cn/q={provider_id}"
    # Tencent's minute endpoint is served by the non-web alias. The web alias
    # began returning its 501 WAF page for server requests while quote and K-line
    # requests on the same backend remained available.
    minute_url = "https://ifzq.gtimg.cn/appstock/app/minute/query?code={provider_id}"
    kline_url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
        "param={provider_id},{period},,,{limit},qfq"
    )

    def __init__(
        self,
        *,
        fetch: Callable[[str, float], bytes] = _http_get,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.fetch = fetch
        self.timeout_seconds = timeout_seconds

    def _request(self, url: str) -> bytes:
        try:
            return self.fetch(url, self.timeout_seconds)
        except PublicMarketError:
            raise
        except Exception:
            raise PublicMarketError("MARKET_QUOTE_UNAVAILABLE") from None

    @staticmethod
    def _quote_fields(payload: bytes, provider_id: str) -> list[str]:
        try:
            text = payload.decode("gb18030")
            prefix = f"v_{provider_id}=\""
            if not text.startswith(prefix):
                raise ValueError
            body = text[len(prefix) :]
            body = body.rsplit('"', 1)[0]
            return body.split("~")
        except (UnicodeDecodeError, ValueError):
            raise PublicMarketError("PUBLIC_MARKET_RESPONSE_INVALID") from None

    @staticmethod
    def _parse_order_book_fields(
        identity: SymbolLookup,
        fields: Sequence[str],
    ) -> dict[str, Any]:
        if len(fields) < 35 or fields[2].strip() != identity.symbol:
            raise PublicMarketError("PUBLIC_MARKET_RESPONSE_INVALID")
        precision = _precision(identity)
        name = identity.name.strip() or fields[1].strip()
        last_price = _decimal(fields[3])
        previous_close = _decimal(fields[4])
        limit_up = _decimal(fields[47]) if len(fields) > 47 else None
        limit_down = _decimal(fields[48]) if len(fields) > 48 else None

        def _parse_ladder(start_idx: int, prefix: str) -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            for idx in range(5):
                p_idx = start_idx + idx * 2
                v_idx = p_idx + 1
                raw_p = _decimal(fields[p_idx]) if p_idx < len(fields) else None
                raw_v = _decimal(fields[v_idx]) if v_idx < len(fields) else None
                valid_price = raw_p is not None and raw_p > 0
                items.append(
                    {
                        "level": idx + 1,
                        "label": f"{prefix}{idx + 1}",
                        "price": _number(raw_p, precision) if valid_price else None,
                        "volume_lots": int(raw_v) if raw_v is not None and raw_v >= 0 else 0,
                    }
                )
            return items

        return {
            "symbol": identity.symbol,
            "name": name,
            "market": identity.market,
            "precision": precision,
            "last_price": _number(last_price, precision) if last_price is not None and last_price > 0 else None,
            "previous_close": _number(previous_close, precision) if previous_close is not None and previous_close > 0 else None,
            "limit_up": _number(limit_up, precision) if limit_up is not None and limit_up > 0 else None,
            "limit_down": _number(limit_down, precision) if limit_down is not None and limit_down > 0 else None,
            "source_time": _quote_clock_time(fields[30]) if len(fields) > 30 else None,
            "bids": _parse_ladder(9, "买"),
            "asks": _parse_ladder(19, "卖"),
        }

    def read_order_book(self, identity: SymbolLookup) -> dict[str, Any]:
        """Fetch real-time 5-level bid/ask order book from Tencent quote endpoint."""
        provider_id = _provider_id(identity)
        fields = self._quote_fields(
            self._request(self.quote_url.format(provider_id=provider_id)),
            provider_id,
        )
        return self._parse_order_book_fields(identity, fields)

    def read_order_books_batch(
        self,
        identities: Sequence[SymbolLookup],
        *,
        chunk_size: int = 60,
    ) -> dict[str, dict[str, Any]]:
        """Fetch 5-level bid/ask order books for multiple symbols using batched Tencent requests."""
        if not identities:
            return {}
        results: dict[str, dict[str, Any]] = {}
        size = max(1, int(chunk_size))
        for start in range(0, len(identities), size):
            chunk = list(identities[start : start + size])
            by_pid: dict[str, SymbolLookup] = {}
            for identity in chunk:
                try:
                    by_pid[_provider_id(identity)] = identity
                except PublicMarketError:
                    continue
            if not by_pid:
                continue
            raw_bytes = self._request(
                self.batch_quote_url.format(provider_ids=",".join(by_pid))
            )
            try:
                text = raw_bytes.decode("gb18030")
            except UnicodeDecodeError:
                raise PublicMarketError("PUBLIC_MARKET_RESPONSE_INVALID") from None
            for line in text.split(";"):
                snippet = line.strip()
                if not snippet.startswith("v_") or '="' not in snippet:
                    continue
                head, rest = snippet.split('="', 1)
                pid = head[2:].strip()
                identity = by_pid.get(pid)
                if identity is None:
                    continue
                body = rest.rsplit('"', 1)[0]
                try:
                    results[identity.symbol] = self._parse_order_book_fields(
                        identity,
                        body.split("~"),
                    )
                except PublicMarketError:
                    continue
        return results

    def read_snapshot(
        self,
        identity: SymbolLookup,
        *,
        detail: bool,
    ) -> MarketSnapshot:
        provider_id = _provider_id(identity)
        if not detail:
            fields = self._quote_fields(
                self._request(self.quote_url.format(provider_id=provider_id)),
                provider_id,
            )
            return _quote_snapshot(identity, fields, source="TENCENT_PUBLIC")
        try:
            payload = json.loads(
                self._request(
                    self.minute_url.format(provider_id=provider_id)
                )
            )
            node = payload["data"][provider_id]
            fields = list(node["qt"][provider_id])
            rows = list(node["data"]["data"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise PublicMarketError("PUBLIC_MARKET_RESPONSE_INVALID") from None
        points: list[TimesharePoint] = []
        previous_volume = Decimal(0)
        previous_time: str | None = None
        for raw_row in rows:
            parts = str(raw_row).split()
            if len(parts) < 3 or (point_time := _session_time(parts[0])) is None:
                continue
            if previous_time is not None and point_time <= previous_time:
                raise PublicMarketError("PUBLIC_MARKET_RESPONSE_INVALID")
            price = _decimal(parts[1])
            cumulative_lots = _decimal(parts[2])
            cumulative_amount = _decimal(parts[3]) if len(parts) >= 4 else None
            if (
                price is None
                or cumulative_lots is None
                or cumulative_lots < previous_volume
                or min(price, cumulative_lots) < 0
                or (
                    cumulative_amount is not None
                    and cumulative_amount < 0
                )
            ):
                raise PublicMarketError("PUBLIC_MARKET_RESPONSE_INVALID")
            delta_lots = cumulative_lots - previous_volume
            previous_volume = cumulative_lots
            previous_time = point_time
            shares = cumulative_lots * Decimal(100)
            average = (
                cumulative_amount / shares
                if cumulative_amount is not None and shares > 0
                else None
            )
            points.append(
                TimesharePoint(
                    time=point_time,
                    price=_number(price, _precision(identity)),
                    average_price=_number(average, 3),
                    volume=_number(delta_lots * Decimal(100)),
                )
            )
        if not points:
            raise PublicMarketError("PUBLIC_MARKET_RESPONSE_INVALID")
        return _quote_snapshot(
            identity,
            fields,
            source="TENCENT_PUBLIC",
            timeshare=tuple(points),
        )

    batch_quote_url = "https://qt.gtimg.cn/q={provider_ids}"

    def read_quotes_batch(
        self,
        identities: Sequence[SymbolLookup],
        *,
        chunk_size: int = 60,
    ) -> dict[str, MarketSnapshot]:
        if not identities:
            return {}
        results: dict[str, MarketSnapshot] = {}
        size = max(1, int(chunk_size))
        for start in range(0, len(identities), size):
            chunk = list(identities[start : start + size])
            by_pid: dict[str, SymbolLookup] = {}
            for identity in chunk:
                try:
                    by_pid[_provider_id(identity)] = identity
                except PublicMarketError:
                    continue
            if not by_pid:
                continue
            raw_bytes = self._request(
                self.batch_quote_url.format(provider_ids=",".join(by_pid))
            )
            try:
                text = raw_bytes.decode("gb18030")
            except UnicodeDecodeError:
                raise PublicMarketError("PUBLIC_MARKET_RESPONSE_INVALID") from None
            for line in text.split(";"):
                snippet = line.strip()
                if not snippet.startswith("v_") or '="' not in snippet:
                    continue
                head, rest = snippet.split('="', 1)
                pid = head[2:].strip()
                identity = by_pid.get(pid)
                if identity is None:
                    continue
                body = rest.rsplit('"', 1)[0]
                try:
                    results[identity.symbol] = _quote_snapshot(
                        identity,
                        body.split("~"),
                        source="TENCENT_PUBLIC",
                    )
                except PublicMarketError:
                    continue
        return results

    def read_series(
        self,
        identity: SymbolLookup,
        period: str,
        limit: int,
    ) -> MarketSeriesPage:
        if period not in {"five_day", "day", "week", "month"}:
            raise PublicMarketError("PUBLIC_MARKET_PERIOD_UNSUPPORTED")
        provider_period = "day" if period == "five_day" else period
        provider_id = _provider_id(identity)
        request_limit = max(6 if period == "five_day" else limit, limit)
        try:
            payload = json.loads(
                self._request(
                    self.kline_url.format(
                        provider_id=provider_id,
                        period=provider_period,
                        limit=request_limit,
                    )
                )
            )
            node = payload["data"][provider_id]
            key = f"qfq{provider_period}"
            rows = node.get(key, node.get(provider_period))
            if not isinstance(rows, list):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise PublicMarketError("PUBLIC_MARKET_RESPONSE_INVALID") from None
        precision = _precision(identity)
        bars: list[KlineBar] = []
        seen_dates: set[str] = set()
        previous_date: str | None = None
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                raise PublicMarketError("PUBLIC_MARKET_RESPONSE_INVALID")
            raw_date = str(row[0])
            try:
                date.fromisoformat(raw_date)
            except ValueError:
                raise PublicMarketError(
                    "PUBLIC_MARKET_RESPONSE_INVALID"
                ) from None
            if raw_date in seen_dates or (
                previous_date is not None and raw_date <= previous_date
            ):
                raise PublicMarketError("PUBLIC_MARKET_RESPONSE_INVALID")
            seen_dates.add(raw_date)
            previous_date = raw_date
            open_price = _decimal(row[1])
            close = _decimal(row[2])
            high = _decimal(row[3])
            low = _decimal(row[4])
            volume_lots = _decimal(row[5])
            if (
                None in {open_price, close, high, low, volume_lots}
                or min(open_price, close, high, low, volume_lots) < 0
                or high < max(open_price, close, low)
                or low > min(open_price, close, high)
            ):
                raise PublicMarketError("PUBLIC_MARKET_RESPONSE_INVALID")
            bars.append(
                KlineBar(
                    time=raw_date,
                    open=_number(open_price, precision),
                    high=_number(high, precision),
                    low=_number(low, precision),
                    close=_number(close, precision),
                    volume=_number(volume_lots * Decimal(100)),
                    amount=None,
                )
            )
        if not bars:
            raise PublicMarketError("PUBLIC_MARKET_RESPONSE_INVALID")
        bars = bars[-5:] if period == "five_day" else bars[-limit:]
        return MarketSeriesPage(
            symbol=identity.symbol,
            period=period,
            bars=tuple(bars),
            adjustment="qfq",
            source="TENCENT_PUBLIC",
            source_errors={"tencent_public": None},
        )


class SinaPublicQuoteProvider:
    quote_url = "https://hq.sinajs.cn/list={provider_id}"

    def __init__(
        self,
        *,
        fetch: Callable[[str, float], bytes] = _http_get,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.fetch = fetch
        self.timeout_seconds = timeout_seconds

    def read_snapshot(self, identity: SymbolLookup) -> MarketSnapshot:
        provider_id = _provider_id(identity)
        try:
            payload = self.fetch(
                self.quote_url.format(provider_id=provider_id),
                self.timeout_seconds,
            ).decode("gb18030")
            if not payload.startswith(f"var hq_str_{provider_id}=\""):
                raise ValueError
            body = payload.split('="', 1)[1].rsplit('"', 1)[0]
            values = body.split(",")
            if len(values) < 32 or not values[0].strip():
                raise ValueError
        except Exception:
            raise PublicMarketError("MARKET_QUOTE_UNAVAILABLE") from None
        precision = _precision(identity)
        fields = [""] * 39
        fields[1] = values[0]
        fields[2] = identity.symbol
        fields[3] = values[3]
        fields[4] = values[2]
        fields[5] = values[1]
        fields[6] = _number(
            None if (volume := _decimal(values[8])) is None else volume / Decimal(100)
        ) or ""
        fields[30] = f"{values[30]} {values[31]}"
        current = _decimal(values[3])
        previous = _decimal(values[2])
        fields[32] = _number(
            None
            if current is None or previous in {None, Decimal(0)}
            else (current / previous - Decimal(1)) * Decimal(100)
        ) or ""
        fields[33] = values[4]
        fields[34] = values[5]
        fields[37] = _number(
            None if (amount := _decimal(values[9])) is None else amount / Decimal(10000)
        ) or ""
        fields[38] = ""
        return _quote_snapshot(
            identity,
            fields,
            source="SINA_PUBLIC",
            source_errors={
                "tencent_public": "MARKET_QUOTE_UNAVAILABLE",
                "sina_public": None,
                "core_metrics": None,
                "main_fund_flow": None,
            },
        )


class PublicMarketDataSource:
    """Resolve identity locally and keep basic market data public-first."""

    def __init__(
        self,
        catalog: object,
        tencent: TencentPublicMarketProvider,
        sina: SinaPublicQuoteProvider,
        *,
        database_fallback: Callable[[str], MarketSnapshot | None] | None = None,
        is_watchlist_symbol: Callable[[str], bool] = lambda _symbol: False,
    ) -> None:
        self.catalog = catalog
        self.tencent = tencent
        self.sina = sina
        self.database_fallback = database_fallback
        self.is_watchlist_symbol = is_watchlist_symbol

    def _identity(self, symbol: str) -> SymbolLookup:
        lookup = getattr(self.catalog, "lookup_existing", None)
        if not callable(lookup):
            lookup = getattr(self.catalog, "lookup", None)
        if not callable(lookup):
            lookup = getattr(self.catalog, "lookup_symbol", None)
        if not callable(lookup):
            raise PublicMarketError("PUBLIC_MARKET_SYMBOL_INVALID")
        return lookup(symbol)

    def read_market_snapshot(
        self,
        symbol: str,
        *,
        detail: bool,
    ) -> MarketSnapshot:
        identity = self._identity(symbol)
        try:
            return self.tencent.read_snapshot(identity, detail=detail)
        except PublicMarketError as error:
            tencent_error = error.error_code
        intraday_error = tencent_error if detail else None
        if detail:
            try:
                snapshot = self.tencent.read_snapshot(identity, detail=False)
            except PublicMarketError as error:
                tencent_error = error.error_code
            else:
                source_errors = dict(snapshot.source_errors)
                source_errors["tencent_intraday"] = intraday_error
                return replace(snapshot, source_errors=source_errors)
        try:
            snapshot = self.sina.read_snapshot(identity)
        except PublicMarketError:
            if self.is_watchlist_symbol(symbol) and self.database_fallback is not None:
                try:
                    stored = self.database_fallback(symbol)
                except Exception:
                    stored = None
                if isinstance(stored, MarketSnapshot):
                    return stored
            raise PublicMarketError("MARKET_QUOTE_UNAVAILABLE") from None
        errors = dict(snapshot.source_errors)
        errors["tencent_public"] = tencent_error
        if intraday_error is not None:
            errors["tencent_intraday"] = intraday_error
        return replace(snapshot, source_errors=errors)

    def read_market_series(
        self,
        symbol: str,
        period: str,
        cursor: str | None,
        limit: int,
    ) -> MarketSeriesPage:
        del cursor
        return self.tencent.read_series(
            self._identity(symbol),
            period,
            limit,
        )

    def read_quotes_batch(
        self,
        symbols: Sequence[str],
    ) -> dict[str, MarketSnapshot]:
        ordered_symbols = list(dict.fromkeys(str(symbol) for symbol in symbols if symbol))
        if not ordered_symbols:
            return {}
        identities: list[SymbolLookup] = []
        valid_symbols: list[str] = []
        for symbol in ordered_symbols:
            try:
                identities.append(self._identity(symbol))
                valid_symbols.append(symbol)
            except PublicMarketError:
                continue
        results: dict[str, MarketSnapshot] = {}
        if identities:
            try:
                results.update(self.tencent.read_quotes_batch(identities))
            except PublicMarketError:
                pass
        for symbol in valid_symbols:
            if symbol in results:
                continue
            try:
                results[symbol] = self.read_market_snapshot(symbol, detail=False)
            except PublicMarketError:
                continue
        return results


@dataclass(frozen=True)
class _EnrichmentEntry:
    outcome: DirectReadOutcome | None
    error_code: str | None
    stored_at: float


class DirectEnrichedMarketDataSource:
    """Merge cached direct L2 fields without owning the public quote."""

    _INTRADAY_NAMES = {
        MetricKind.LARGE_ORDER_NET: "large_order_net",
        MetricKind.LARGE_ORDER_AMOUNT: "large_order_amount",
        MetricKind.RETAIL_COUNT: "retail_count",
        MetricKind.MACDFS: "macdfs",
    }

    def __init__(
        self,
        base_source: object,
        direct_source: object | None,
        *,
        ttl_seconds: float = 5.0,
        stale_ttl_seconds: float = 120.0,
        max_cache_entries: int = 2048,
        clock: Callable[[], float] = time.monotonic,
        is_market_open: Callable[[], bool] = lambda: True,
        is_watchlist_symbol: Callable[[str], bool] = lambda _symbol: True,
        closed_enrichment: Callable[[str], DirectReadOutcome | None] | None = None,
        core_fallback: Callable[[str], DirectReadOutcome | None] | None = None,
        fund_flow_fallback: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if stale_ttl_seconds < 0:
            raise ValueError("stale_ttl_seconds must be non-negative")
        if not isinstance(max_cache_entries, int) or max_cache_entries <= 0:
            raise ValueError("max_cache_entries must be positive")
        self.base_source = base_source
        self.direct_source = direct_source
        self.ttl_seconds = ttl_seconds
        self.stale_ttl_seconds = stale_ttl_seconds
        self.max_cache_entries = max_cache_entries
        self.clock = clock
        self.is_market_open = is_market_open
        self.is_watchlist_symbol = is_watchlist_symbol
        self.closed_enrichment = closed_enrichment
        self.core_fallback = core_fallback
        self.fund_flow_fallback = fund_flow_fallback
        self._lock = RLock()
        # Direct L2 reads are optional and may serialize behind the App
        # protocol client's connection lock. Never let one slow read stall
        # every public snapshot for the watchlist.
        self._enrichment_lock = Lock()
        self._cache: dict[str, _EnrichmentEntry] = {}
        self._last_good: dict[str, _EnrichmentEntry] = {}
        self._closed_fallback_attempted: set[str] = set()
        self._closed_fallback_outcomes: dict[str, DirectReadOutcome] = {}

    def invalidate_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            self._last_good.clear()

    def _read_enrichment(self, symbol: str) -> _EnrichmentEntry:
        now = self.clock()
        with self._lock:
            cached = self._cache.get(symbol)
            if cached is not None and now - cached.stored_at < self.ttl_seconds:
                return cached
        if not self._enrichment_lock.acquire(blocking=False):
            with self._lock:
                last_good = self._last_good.get(symbol)
            if (
                last_good is not None
                and last_good.outcome is not None
                and now - last_good.stored_at < self.stale_ttl_seconds
            ):
                return last_good
            return _EnrichmentEntry(
                outcome=None,
                error_code="DIRECT_ENRICHMENT_BUSY",
                stored_at=now,
            )
        try:
            if self.direct_source is None:
                entry = _EnrichmentEntry(
                    outcome=None,
                    error_code="DIRECT_SESSION_UNAVAILABLE",
                    stored_at=now,
                )
            else:
                read_direct = getattr(self.direct_source, "read_direct", None)
                try:
                    if not callable(read_direct):
                        raise DirectRequestError("DIRECT_REQUEST_UNAVAILABLE")
                    outcome = read_direct(symbol)
                    if not isinstance(outcome, DirectReadOutcome):
                        raise DirectRequestError("DIRECT_REQUEST_FAILED")
                    entry = _EnrichmentEntry(
                        outcome=outcome,
                        error_code=None,
                        stored_at=now,
                    )
                except DirectRequestError as error:
                    entry = _EnrichmentEntry(
                        outcome=None,
                        error_code=sanitized_direct_error_code(
                            error.error_code,
                            "DIRECT_REQUEST_FAILED",
                        ),
                        stored_at=now,
                    )
                except Exception:
                    entry = _EnrichmentEntry(
                        outcome=None,
                        error_code="DIRECT_REQUEST_FAILED",
                        stored_at=now,
                    )
        finally:
            self._enrichment_lock.release()
        if entry.error_code != "DIRECT_ENRICHMENT_BUSY":
            with self._lock:
                self._cache[symbol] = entry
                while len(self._cache) > self.max_cache_entries:
                    self._cache.pop(next(iter(self._cache)))
                if entry.outcome is not None:
                    self._last_good[symbol] = entry
                    while len(self._last_good) > self.max_cache_entries:
                        self._last_good.pop(next(iter(self._last_good)))
        return entry

    @staticmethod
    def _without_enrichment(
        snapshot: MarketSnapshot,
        error_code: str,
    ) -> MarketSnapshot:
        capabilities = dict(snapshot.capabilities)
        capabilities["l2"] = {
            "available": False,
            "reason": error_code,
        }
        source_errors = dict(snapshot.source_errors)
        source_errors["core_metrics"] = error_code
        return replace(
            snapshot,
            capabilities=capabilities,
            source_errors=source_errors,
        )

    def _merge(
        self,
        snapshot: MarketSnapshot,
        outcome: DirectReadOutcome,
    ) -> MarketSnapshot:
        values = outcome.values
        quote = dict(snapshot.quote)
        quote.update(
            {
                "large_order_net": values.get(MetricKind.LARGE_ORDER_NET),
                "large_order_amount": values.get(
                    MetricKind.LARGE_ORDER_AMOUNT
                ),
                "retail_count": values.get(MetricKind.RETAIL_COUNT),
                "macdfs": values.get(MetricKind.MACDFS),
            }
        )
        intraday_series = dict(snapshot.intraday_series)
        for kind, series in outcome.intraday_series.items():
            name = self._INTRADAY_NAMES.get(kind)
            if name is not None and series.get("points"):
                intraday_series[name] = series
        main_fund_flow: dict[str, dict[str, str | None]] = {}
        for period, _label, unit_kind in FUND_FLOW_PERIODS:
            metrics = FUND_FLOW_METRICS[period]
            unit = values.get(unit_kind)
            period_values = {
                name: values.get(kind)
                for name, kind in metrics.items()
            }
            if any(
                value is not None for value in period_values.values()
            ):
                main_fund_flow[period] = {
                    "unit": unit,
                    **period_values,
                }
        has_l2_values = any(
            quote.get(name) is not None
            for name in (
                "large_order_net",
                "large_order_amount",
                "retail_count",
                "macdfs",
            )
        ) or any(
            series.get("points")
            for series in outcome.intraday_series.values()
        ) or bool(main_fund_flow)
        if not has_l2_values:
            return self._without_enrichment(
                snapshot,
                "DIRECT_ENRICHMENT_EMPTY",
            )
        capabilities = dict(snapshot.capabilities)
        capabilities["l2"] = {"available": True, "reason": None}
        source_errors = dict(snapshot.source_errors)
        source_errors.update(outcome.source_errors)
        return replace(
            snapshot,
            quote=quote,
            intraday_series=intraday_series,
            main_fund_flow=main_fund_flow,
            capabilities=capabilities,
            source_errors=source_errors,
            stored_trade_dates=dict(outcome.stored_trade_dates),
        )

    def read_quotes_batch(
        self,
        symbols: Sequence[str],
    ) -> dict[str, MarketSnapshot]:
        batch_reader = getattr(self.base_source, "read_quotes_batch", None)
        if callable(batch_reader):
            return batch_reader(symbols)
        read_snapshot = getattr(self.base_source, "read_market_snapshot", None)
        if not callable(read_snapshot):
            return {}
        out: dict[str, MarketSnapshot] = {}
        for symbol in symbols:
            try:
                out[symbol] = read_snapshot(symbol, detail=False)
            except Exception:
                continue
        return out

    def read_market_snapshot(
        self,
        symbol: str,
        *,
        detail: bool,
    ) -> MarketSnapshot:
        read_snapshot = getattr(
            self.base_source,
            "read_market_snapshot",
            None,
        )
        if not callable(read_snapshot):
            raise PublicMarketError("MARKET_QUOTE_UNAVAILABLE")
        snapshot = read_snapshot(symbol, detail=detail)
        if not detail:
            return snapshot
        if is_exchange_fund_symbol(symbol, snapshot.market):
            capabilities = dict(snapshot.capabilities)
            capabilities["l2"] = {
                "available": False,
                "reason": "FUND_L2_UNSUPPORTED",
            }
            return replace(
                snapshot,
                capabilities=capabilities,
                source_errors={
                    **snapshot.source_errors,
                    "core_metrics": None,
                    "main_fund_flow": None,
                },
            )
        if not self.is_market_open():
            if self.is_watchlist_symbol(symbol) and self.closed_enrichment is not None:
                try:
                    stored = self.closed_enrichment(symbol)
                except Exception:
                    stored = None
                if isinstance(stored, DirectReadOutcome):
                    return self._merge(snapshot, stored)
            capabilities = dict(snapshot.capabilities)
            capabilities["l2"] = {
                "available": False,
                "reason": "MARKET_CLOSED_DATA_UNAVAILABLE",
            }
            return replace(
                snapshot,
                capabilities=capabilities,
                source_errors={
                    **snapshot.source_errors,
                    "core_metrics": None,
                    "main_fund_flow": None,
                },
            )
        entry = self._read_enrichment(symbol)
        if entry.outcome is None:
            recovered_outcome: DirectReadOutcome | None = None
            if self.is_watchlist_symbol(symbol):
                now = self.clock()
                with self._lock:
                    last_good = self._last_good.get(symbol)
                if (
                    entry.error_code != "DIRECT_ENRICHMENT_BUSY"
                    and last_good is not None
                    and last_good.outcome is not None
                    and now - last_good.stored_at < self.stale_ttl_seconds
                ):
                    recovered_outcome = last_good.outcome
                elif callable(self.core_fallback):
                    try:
                        recovered_outcome = self.core_fallback(symbol)
                    except Exception:
                        recovered_outcome = None
            if isinstance(recovered_outcome, DirectReadOutcome):
                merged = self._merge(snapshot, recovered_outcome)
                if merged.capabilities.get("l2", {}).get("available") and entry.error_code != "DIRECT_ENRICHMENT_BUSY":
                    capabilities = dict(merged.capabilities)
                    capabilities["l2"] = {
                        "available": False,
                        "reason": entry.error_code or "DIRECT_REQUEST_FAILED",
                    }
                    source_errors = dict(merged.source_errors)
                    source_errors["core_metrics"] = entry.error_code or "DIRECT_REQUEST_FAILED"
                    merged = replace(
                        merged,
                        capabilities=capabilities,
                        source_errors=source_errors,
                    )
            else:
                merged = self._without_enrichment(
                    snapshot,
                    entry.error_code or "DIRECT_REQUEST_FAILED",
                )
        else:
            merged = self._merge(snapshot, entry.outcome)
        if (
            not merged.main_fund_flow
            and not is_exchange_fund_symbol(symbol, merged.market)
            and self.is_watchlist_symbol(symbol)
            and callable(self.fund_flow_fallback)
        ):
            try:
                stored_ff = self.fund_flow_fallback(symbol)
            except Exception:
                stored_ff = None
            if isinstance(stored_ff, dict) and isinstance(stored_ff.get("periods"), dict):
                stored_trade_date = (
                    str(stored_ff["trade_date"]).strip()
                    if stored_ff.get("trade_date") is not None
                    else None
                )
                quote_trade_date = extract_quote_trade_date(merged.source_time)
                cross_day_mismatch = (
                    quote_trade_date is not None
                    and stored_trade_date is not None
                    and len(stored_trade_date) == 8
                    and stored_trade_date.isdigit()
                    and stored_trade_date != quote_trade_date
                )
                fallback_periods = (
                    {}
                    if cross_day_mismatch
                    else {
                        period: row
                        for period, row in stored_ff["periods"].items()
                        if isinstance(row, dict)
                        and any(
                            row.get(k) is not None
                            for k in (
                                "main_net_inflow",
                                "main_visible_inflow",
                                "main_hidden_inflow",
                                "retail_inflow",
                            )
                        )
                    }
                )
                if fallback_periods:
                    stored_trade_dates = dict(merged.stored_trade_dates)
                    if stored_trade_date is not None:
                        stored_trade_dates["main_fund_flow"] = stored_trade_date
                    merged = replace(
                        merged,
                        main_fund_flow=fallback_periods,
                        stored_trade_dates=stored_trade_dates,
                    )
        return merged

    def read_market_series(
        self,
        symbol: str,
        period: str,
        cursor: str | None,
        limit: int,
    ) -> MarketSeriesPage:
        read_series = getattr(self.base_source, "read_market_series", None)
        if not callable(read_series):
            raise PublicMarketError("PUBLIC_MARKET_PERIOD_UNSUPPORTED")
        return read_series(symbol, period, cursor, limit)
