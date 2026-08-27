"""Public quote, intraday, and K-line providers for the market application."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Callable
from urllib.request import Request, urlopen

from .market_data import KlineBar, MarketSeriesPage, MarketSnapshot, TimesharePoint
from .parsed_values import DirectRequestError, SymbolLookup


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
    if not name:
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
    if high is not None and low is not None and high < low:
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
        for raw_row in rows:
            parts = str(raw_row).split()
            if len(parts) < 3 or (point_time := _session_time(parts[0])) is None:
                continue
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
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                raise PublicMarketError("PUBLIC_MARKET_RESPONSE_INVALID")
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
                    time=str(row[0]),
                    open=_number(open_price, precision),
                    high=_number(high, precision),
                    low=_number(low, precision),
                    close=_number(close, precision),
                    volume=_number(volume_lots * Decimal(100)),
                    amount=None,
                )
            )
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
