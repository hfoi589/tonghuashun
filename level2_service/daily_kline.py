"""Public Tonghuashun daily K-line ingestion and derived indicators."""

from __future__ import annotations

import json
import math
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .market_data import KlineBar, MarketSeriesPage, MarketSnapshot


_MA_WINDOWS = (5, 13, 21, 60, 120, 250)


def _indicator_value(value: float) -> str:
    rounded = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if rounded in {"-0", ""} else rounded


def calculate_daily_indicators(
    bars: tuple[KlineBar, ...],
) -> dict[str, tuple[str | None, ...]]:
    """Calculate aligned MA, BOLL(20,2), and MACD(12,26,9) series."""
    closes = [float(bar.close) for bar in bars]
    length = len(closes)
    indicators: dict[str, tuple[str | None, ...]] = {}

    prefix = [0.0]
    for close in closes:
        prefix.append(prefix[-1] + close)

    for window in _MA_WINDOWS:
        values: list[str | None] = [None] * length
        for index in range(window - 1, length):
            mean = (prefix[index + 1] - prefix[index + 1 - window]) / window
            values[index] = _indicator_value(mean)
        indicators[f"ma{window}"] = tuple(values)

    boll_mid: list[str | None] = [None] * length
    boll_upper: list[str | None] = [None] * length
    boll_lower: list[str | None] = [None] * length
    for index in range(19, length):
        window_values = closes[index - 19 : index + 1]
        mean = sum(window_values) / 20
        deviation = math.sqrt(
            sum((value - mean) ** 2 for value in window_values) / 20
        )
        boll_mid[index] = _indicator_value(mean)
        boll_upper[index] = _indicator_value(mean + 2 * deviation)
        boll_lower[index] = _indicator_value(mean - 2 * deviation)
    indicators["boll_mid"] = tuple(boll_mid)
    indicators["boll_upper"] = tuple(boll_upper)
    indicators["boll_lower"] = tuple(boll_lower)

    dif_values: list[str | None] = []
    dea_values: list[str | None] = []
    hist_values: list[str | None] = []
    if closes:
        ema12 = closes[0]
        ema26 = closes[0]
        dea = 0.0
        for index, close in enumerate(closes):
            if index:
                ema12 += (close - ema12) * (2 / 13)
                ema26 += (close - ema26) * (2 / 27)
            dif = ema12 - ema26
            if index:
                dea += (dif - dea) * (2 / 10)
            hist = 2 * (dif - dea)
            dif_values.append(_indicator_value(dif))
            dea_values.append(_indicator_value(dea))
            hist_values.append(_indicator_value(hist))
    indicators["macd_dif"] = tuple(dif_values)
    indicators["macd_dea"] = tuple(dea_values)
    indicators["macd_hist"] = tuple(hist_values)
    return indicators


def _validate_bars(
    bars: tuple[KlineBar, ...],
    *,
    error_code: str,
) -> tuple[KlineBar, ...]:
    seen: set[str] = set()
    for bar in bars:
        try:
            date.fromisoformat(bar.time)
            if bar.time in seen:
                raise ValueError("duplicate date")
            seen.add(bar.time)
            values = tuple(
                Decimal(value)
                for value in (
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                    bar.amount,
                )
                if value is not None
            )
            if len(values) != 6 or not all(value.is_finite() for value in values):
                raise ValueError("missing value")
            open_value, high_value, low_value, close_value, volume, amount = values
            if (
                min(open_value, high_value, low_value, close_value) <= 0
                or high_value < max(open_value, close_value)
                or low_value > min(open_value, close_value)
                or volume < 0
                or amount < 0
            ):
                raise ValueError("impossible OHLCV")
        except (InvalidOperation, TypeError, ValueError) as error:
            raise DailyKlineSourceError(
                error_code,
                "daily K-line source returned invalid bars",
            ) from error
    return tuple(sorted(bars, key=lambda bar: bar.time))


class DailyKlineSourceError(RuntimeError):
    """A stable public/App daily-series failure."""

    def __init__(self, error_code: str, message: str | None = None) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code


def _payload(text: str, symbol: str, suffix: str) -> dict[str, object]:
    callback = f"quotebridge_v6_line_hs_{symbol}_01_{suffix}"
    match = re.fullmatch(rf"{re.escape(callback)}\((.*)\)\s*;?", text.strip(), re.DOTALL)
    if match is None:
        raise DailyKlineSourceError(
            "PUBLIC_KLINE_SYMBOL_MISMATCH",
            "10jqka JSONP callback does not match the requested symbol",
        )
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise DailyKlineSourceError(
            "PUBLIC_KLINE_RESPONSE_INVALID",
            "10jqka JSONP payload is invalid",
        ) from error
    if not isinstance(value, dict):
        raise DailyKlineSourceError(
            "PUBLIC_KLINE_RESPONSE_INVALID",
            "10jqka JSONP payload must be an object",
        )
    return value


def _number(value: str, *, field: str) -> str:
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise DailyKlineSourceError(
            "PUBLIC_KLINE_RESPONSE_INVALID",
            f"10jqka {field} field is invalid",
        ) from error
    if not number.is_finite():
        raise DailyKlineSourceError(
            "PUBLIC_KLINE_RESPONSE_INVALID",
            f"10jqka {field} field is invalid",
        )
    return value


def parse_10jqka_year(text: str, symbol: str, year: int) -> tuple[KlineBar, ...]:
    """Parse one exact qfq year response into validated, ordered bars."""
    payload = _payload(text, symbol, str(year))
    raw_data = payload.get("data")
    if not isinstance(raw_data, str):
        raise DailyKlineSourceError(
            "PUBLIC_KLINE_RESPONSE_INVALID",
            "10jqka data field is missing",
        )
    bars: list[KlineBar] = []
    for raw_row in raw_data.split(";"):
        if not raw_row:
            continue
        fields = raw_row.split(",")
        if len(fields) < 7:
            raise DailyKlineSourceError(
                "PUBLIC_KLINE_RESPONSE_INVALID",
                "10jqka daily row field count is invalid",
            )
        try:
            parsed_date = date.fromisoformat(
                f"{fields[0][0:4]}-{fields[0][4:6]}-{fields[0][6:8]}"
            )
        except (ValueError, IndexError) as error:
            raise DailyKlineSourceError(
                "PUBLIC_KLINE_RESPONSE_INVALID",
                "10jqka date field is invalid",
            ) from error
        if parsed_date.year != year or len(fields[0]) != 8:
            raise DailyKlineSourceError(
                "PUBLIC_KLINE_RESPONSE_INVALID",
                "10jqka date field does not match the requested year",
            )
        open_value = _number(fields[1], field="open")
        high_value = _number(fields[2], field="high")
        low_value = _number(fields[3], field="low")
        close_value = _number(fields[4], field="close")
        volume_value = _number(fields[5], field="volume")
        amount_value = _number(fields[6], field="amount")
        open_number = Decimal(open_value)
        high_number = Decimal(high_value)
        low_number = Decimal(low_value)
        close_number = Decimal(close_value)
        if (
            min(open_number, high_number, low_number, close_number) <= 0
            or high_number < max(open_number, close_number, low_number)
            or low_number > min(open_number, close_number, high_number)
            or Decimal(volume_value) < 0
            or Decimal(amount_value) < 0
        ):
            raise DailyKlineSourceError(
                "PUBLIC_KLINE_RESPONSE_INVALID",
                "10jqka daily row contains impossible OHLCV values",
            )
        bars.append(
            KlineBar(
                time=parsed_date.isoformat(),
                open=open_value,
                high=high_value,
                low=low_value,
                close=close_value,
                volume=volume_value,
                amount=amount_value,
            )
        )
    return tuple(sorted(bars, key=lambda bar: bar.time))


def parse_10jqka_metadata(text: str, symbol: str) -> tuple[int, ...]:
    payload = _payload(text, symbol, "last")
    raw_years = payload.get("year")
    if not isinstance(raw_years, dict):
        raise DailyKlineSourceError(
            "PUBLIC_KLINE_RESPONSE_INVALID",
            "10jqka year metadata is missing",
        )
    years: list[int] = []
    for raw_year, raw_count in raw_years.items():
        try:
            year = int(raw_year)
            count = int(raw_count)
        except (TypeError, ValueError) as error:
            raise DailyKlineSourceError(
                "PUBLIC_KLINE_RESPONSE_INVALID",
                "10jqka year metadata is invalid",
            ) from error
        if not 1990 <= year <= 2100 or count < 0:
            raise DailyKlineSourceError(
                "PUBLIC_KLINE_RESPONSE_INVALID",
                "10jqka year metadata is invalid",
            )
        if count:
            years.append(year)
    if not years:
        raise DailyKlineSourceError(
            "PUBLIC_KLINE_RESPONSE_INVALID",
            "10jqka returned no daily history years",
        )
    return tuple(sorted(set(years), reverse=True))


def _http_get(url: str, timeout: float) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Referer": "https://stockpage.10jqka.com.cn/",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="strict")


class TonghuashunPublicDailyKlineProvider:
    """Fetch qfq yearly bars from Tonghuashun's public web quote bridge."""

    def __init__(
        self,
        *,
        fetch: Callable[[str, float], str] = _http_get,
        timeout_seconds: float = 8.0,
        attempts: int = 2,
    ) -> None:
        self.fetch = fetch
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts

    @staticmethod
    def _url(symbol: str, suffix: str) -> str:
        return f"https://d.10jqka.com.cn/v6/line/hs_{symbol}/01/{suffix}.js"

    def _request(self, symbol: str, suffix: str) -> str:
        last_error: Exception | None = None
        for attempt in range(self.attempts):
            try:
                return self.fetch(self._url(symbol, suffix), self.timeout_seconds)
            except Exception as error:  # urllib exposes several transport subclasses.
                last_error = error
                if attempt + 1 < self.attempts:
                    time.sleep(0.2 * (attempt + 1))
        raise DailyKlineSourceError(
            "PUBLIC_KLINE_HTTP_ERROR",
            f"10jqka daily request failed: {last_error}",
        ) from last_error

    def read(self, symbol: str, *, minimum_bars: int) -> tuple[KlineBar, ...]:
        if not re.fullmatch(r"[0-9]{6}", symbol):
            raise DailyKlineSourceError(
                "PUBLIC_KLINE_SYMBOL_INVALID",
                "daily K-line symbol must contain six digits",
            )
        years = parse_10jqka_metadata(self._request(symbol, "last"), symbol)
        by_date: dict[str, KlineBar] = {}
        for year in years:
            for bar in parse_10jqka_year(self._request(symbol, str(year)), symbol, year):
                by_date[bar.time] = bar
            if len(by_date) >= minimum_bars:
                break
        if not by_date:
            raise DailyKlineSourceError(
                "PUBLIC_KLINE_RESPONSE_INVALID",
                "10jqka returned no valid daily K-line bars",
            )
        return tuple(by_date[key] for key in sorted(by_date))


@dataclass(frozen=True)
class _DailyCacheEntry:
    bars: tuple[KlineBar, ...]
    source: str
    stored_at: float


class DailyKlineMarketDataSource:
    """Add a public-first, App-fallback daily qfq series to a market source."""

    def __init__(
        self,
        app_source: Any,
        public_provider: TonghuashunPublicDailyKlineProvider,
        *,
        clock: Callable[[], float] = time.monotonic,
        is_market_open: Callable[[], bool] = lambda: False,
        open_cache_seconds: float = 60.0,
        closed_cache_seconds: float = 15 * 60.0,
    ) -> None:
        self.app_source = app_source
        self.public_provider = public_provider
        self.clock = clock
        self.is_market_open = is_market_open
        self.open_cache_seconds = open_cache_seconds
        self.closed_cache_seconds = closed_cache_seconds
        self._cache: dict[tuple[str, str], _DailyCacheEntry] = {}
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._state_lock = threading.RLock()
        self._stats = {
            "public_successes": 0,
            "app_fallbacks": 0,
            "stale_cache_hits": 0,
            "failures": 0,
        }

    def read_market_snapshot(self, symbol: str, *, detail: bool) -> MarketSnapshot:
        snapshot = self.app_source.read_market_snapshot(symbol, detail=detail)
        capabilities = dict(snapshot.capabilities)
        capabilities["daily_kline"] = {"available": True, "adjustment": "qfq"}
        return replace(snapshot, capabilities=capabilities)

    @staticmethod
    def _error_code(error: Exception) -> str:
        return str(getattr(error, "error_code", None) or str(error) or type(error).__name__)

    def _lock_for(self, key: tuple[str, str]) -> threading.Lock:
        with self._state_lock:
            return self._locks.setdefault(key, threading.Lock())

    def _fresh(self, entry: _DailyCacheEntry, now: float) -> bool:
        ttl = self.open_cache_seconds if self.is_market_open() else self.closed_cache_seconds
        return now - entry.stored_at < ttl

    @staticmethod
    def _page(
        symbol: str,
        period: str,
        entry: _DailyCacheEntry,
        limit: int,
        *,
        cached: bool,
        stale: bool,
        source_error: str | None,
        source_errors: dict[str, str | None],
    ) -> MarketSeriesPage:
        all_indicators = calculate_daily_indicators(entry.bars)
        start = max(0, len(entry.bars) - limit)
        bars = entry.bars[start:]
        indicators = {
            name: values[start:] for name, values in all_indicators.items()
        }
        return MarketSeriesPage(
            symbol=symbol,
            period=period,
            bars=bars,
            indicators=indicators,
            adjustment="qfq",
            source=entry.source,
            cached=cached,
            stale=stale,
            source_error=source_error,
            source_errors=source_errors,
        )

    def read_market_series(
        self,
        symbol: str,
        period: str,
        cursor: str | None,
        limit: int,
    ) -> MarketSeriesPage:
        if period != "day":
            return self.app_source.read_market_series(symbol, period, cursor, limit)

        key = (symbol, period)
        with self._lock_for(key):
            now = self.clock()
            with self._state_lock:
                cached_entry = self._cache.get(key)
            if cached_entry is not None and self._fresh(cached_entry, now):
                return self._page(
                    symbol,
                    period,
                    cached_entry,
                    limit,
                    cached=True,
                    stale=False,
                    source_error=None,
                    source_errors={"public_kline": None, "app_kline": None},
                )

            required = max(489, limit + 249)
            source_errors: dict[str, str | None] = {
                "public_kline": None,
                "app_kline": None,
            }
            entry: _DailyCacheEntry | None = None
            try:
                bars = _validate_bars(
                    self.public_provider.read(symbol, minimum_bars=required),
                    error_code="PUBLIC_KLINE_RESPONSE_INVALID",
                )
                entry = _DailyCacheEntry(
                    bars=bars[-required:],
                    source="THS_PUBLIC",
                    stored_at=now,
                )
                with self._state_lock:
                    self._stats["public_successes"] += 1
            except Exception as error:
                source_errors["public_kline"] = self._error_code(error)

            if entry is None:
                try:
                    app_page = self.app_source.read_market_series(
                        symbol,
                        period,
                        cursor,
                        required,
                    )
                    if not app_page.bars:
                        raise DailyKlineSourceError(
                            app_page.source_error or "DIRECT_KLINE_UNAVAILABLE"
                        )
                    bars = _validate_bars(
                        app_page.bars,
                        error_code="DIRECT_KLINE_RESPONSE_INVALID",
                    )
                    entry = _DailyCacheEntry(
                        bars=bars[-required:],
                        source="THS_APP",
                        stored_at=now,
                    )
                    with self._state_lock:
                        self._stats["app_fallbacks"] += 1
                except Exception as error:
                    source_errors["app_kline"] = self._error_code(error)

            if entry is not None:
                with self._state_lock:
                    self._cache[key] = entry
                return self._page(
                    symbol,
                    period,
                    entry,
                    limit,
                    cached=False,
                    stale=False,
                    source_error=None,
                    source_errors=source_errors,
                )

            if cached_entry is not None:
                with self._state_lock:
                    self._stats["stale_cache_hits"] += 1
                return self._page(
                    symbol,
                    period,
                    cached_entry,
                    limit,
                    cached=True,
                    stale=True,
                    source_error="KLINE_SOURCES_UNAVAILABLE",
                    source_errors=source_errors,
                )

            with self._state_lock:
                self._stats["failures"] += 1
            return MarketSeriesPage(
                symbol=symbol,
                period=period,
                bars=(),
                adjustment="qfq",
                source_error="KLINE_SOURCES_UNAVAILABLE",
                source_errors=source_errors,
            )

    def daily_kline_stats(self) -> dict[str, int]:
        with self._state_lock:
            return {
                "cache_entries": len(self._cache),
                **self._stats,
            }
