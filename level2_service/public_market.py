"""Public quote, intraday, and K-line providers for the market application."""

from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from threading import RLock
from typing import Callable
from urllib.request import Request, urlopen

from .market_data import KlineBar, MarketSeriesPage, MarketSnapshot, TimesharePoint
from .models import FUND_FLOW_METRICS, FUND_FLOW_PERIODS, MetricKind
from .parsed_values import (
    DirectReadOutcome,
    DirectRequestError,
    SymbolLookup,
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


def _session_time(value: str) -> str | None:
    compact = value.replace(":", "").strip()
    if not re.fullmatch(r"[0-9]{4}", compact):
        return None
    if not ("0930" <= compact <= "1130" or "1300" <= compact <= "1500"):
        return None
    return f"{compact[:2]}:{compact[2:]}"


def _names_compatible(expected: str, actual: str) -> bool:
    expected_normalized = re.sub(r"\s+", "", expected).casefold()
    actual_normalized = re.sub(r"\s+", "", actual).casefold()
    if expected_normalized == actual_normalized:
        return True
    return min(len(expected_normalized), len(actual_normalized)) >= 3 and (
        expected_normalized.startswith(actual_normalized)
        or actual_normalized.startswith(expected_normalized)
    )


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
    name = fields[1].strip()
    if not name or not _names_compatible(identity.name, name):
        raise PublicMarketError("PUBLIC_MARKET_RESPONSE_INVALID")
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
        },
    )


class TencentPublicMarketProvider:
    quote_url = "https://qt.gtimg.cn/q={provider_id}"
    minute_url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={provider_id}"
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
    ) -> None:
        self.catalog = catalog
        self.tencent = tencent
        self.sina = sina

    def _identity(self, symbol: str) -> SymbolLookup:
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
        try:
            snapshot = self.sina.read_snapshot(identity)
        except PublicMarketError:
            raise PublicMarketError("MARKET_QUOTE_UNAVAILABLE") from None
        errors = dict(snapshot.source_errors)
        errors["tencent_public"] = tencent_error
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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.base_source = base_source
        self.direct_source = direct_source
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._lock = RLock()
        self._cache: dict[str, _EnrichmentEntry] = {}

    def _read_enrichment(self, symbol: str) -> _EnrichmentEntry:
        now = self.clock()
        with self._lock:
            cached = self._cache.get(symbol)
            if cached is not None and now - cached.stored_at < self.ttl_seconds:
                return cached
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
        with self._lock:
            self._cache[symbol] = entry
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
            if unit is not None or any(
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
        )

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
        entry = self._read_enrichment(symbol)
        if entry.outcome is None:
            return self._without_enrichment(
                snapshot,
                entry.error_code or "DIRECT_REQUEST_FAILED",
            )
        return self._merge(snapshot, entry.outcome)

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
