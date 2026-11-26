"""App-independent Tonghuashun ETF shareholding fund-flow client and parser."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Callable, Mapping
from urllib.request import Request
from zoneinfo import ZoneInfo

from .market_data import fixed_market_error_code
from .parsed_values import (
    UnsupportedMarketError,
    is_exchange_fund_symbol,
    market_code_for_symbol,
)
from .safe_http import SafeHttpError, SafeHttpTransport


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
ETF_FLOW_BASE_URL = "https://fund.10jqka.com.cn"
ETF_FLOW_PERIODS: dict[str, tuple[int, str]] = {
    "20d": (-19, "近20日"),
    "60d": (-59, "近60日"),
    "120d": (-119, "近120日"),
    "250d": (-249, "近250日"),
}
_SUMMARY_FIELDS = (
    "stockEtfNetBuyFlowDate",
    "stockEtfNetBuyFlow",
    "stockEtfNetBuyFlow3d",
    "stockEtfNetBuyFlow5d",
    "stockEtfNetBuyFlow20d",
    "stockEtfNetBuyFlow60d",
)
_LINE_FIELDS = "stock_etf_net_buy_flow_amount,stock_etf_fund_scale"
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Referer": "https://fund.10jqka.com.cn/",
}
_NON_RETRYABLE_HTTP_STATUSES = frozenset({400, 403, 404})
_SIX_DIGIT_RE = re.compile(r"^[0-9]{6}$")


class EtfFundFlowError(RuntimeError):
    """Fixed sanitized error raised when THS ETF fund-flow upstream fails."""

    def __init__(self, error_code: str = "ETF_FUND_FLOW_UNAVAILABLE") -> None:
        self.error_code = error_code
        super().__init__(error_code)


def _default_is_trading_day(day: date) -> bool:
    return day.weekday() < 5


def _as_shanghai(moment: datetime) -> datetime:
    return (
        moment.astimezone(SHANGHAI_TZ)
        if moment.tzinfo is not None
        else moment.replace(tzinfo=SHANGHAI_TZ)
    )


def last_closed_trading_day(
    today: date,
    is_trading_day: Callable[[date], bool] = _default_is_trading_day,
) -> str:
    """Return YYYY-MM-DD for the most recent trading day strictly before ``today``."""
    cursor = today - timedelta(days=1)
    for _ in range(30):
        if is_trading_day(cursor):
            return cursor.isoformat()
        cursor -= timedelta(days=1)
    return (today - timedelta(days=1)).isoformat()


def expected_etf_trade_date(
    now_sh: datetime,
    is_trading_day: Callable[[date], bool] = _default_is_trading_day,
) -> tuple[str, str, bool]:
    """Return ``(expected_date, target_t1, in_morning_window)`` under T+1 ~09:00 rules."""
    local = _as_shanghai(now_sh)
    target_t1 = last_closed_trading_day(local.date(), is_trading_day)
    on_trading_day = is_trading_day(local.date())
    minute = local.hour * 60 + local.minute
    in_morning_window = on_trading_day and (8 * 60 + 50 <= minute < 10 * 60 + 30)
    if on_trading_day and minute < 8 * 60 + 50:
        expected_date = last_closed_trading_day(
            date.fromisoformat(target_t1),
            is_trading_day,
        )
    else:
        expected_date = target_t1
    return expected_date, target_t1, in_morning_window


def _float_or_none(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _int_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _round_half_up(value: float, places: int = 2) -> float:
    try:
        quant = Decimal("1") if places == 0 else Decimal("1." + "0" * places)
        rounded = float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        rounded = round(value, places)
    return 0.0 if rounded == 0 else rounded


def _tone(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "flat"
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "flat"


def format_etf_flow_text(yuan_value: float | None) -> str | None:
    """Format an amount in yuan into THS ETF 2-decimal 亿/万 text."""
    if yuan_value is None or not math.isfinite(yuan_value):
        return None
    if abs(yuan_value) >= 1e8:
        scaled = _round_half_up(yuan_value / 1e8, 2)
        return f"{scaled:.2f}亿"
    scaled = _round_half_up(yuan_value / 1e4, 2)
    return f"{scaled:.2f}万"


def format_etf_compact_rmb(yuan_value: float | None) -> str:
    """Format holdings net inflow using THS compact RMB rules (e.g. 630.4万)."""
    if yuan_value is None or not math.isfinite(yuan_value):
        return "--"
    abs_val = abs(yuan_value)
    if abs_val >= 1e8:
        scaled, unit = yuan_value / 1e8, "亿"
    elif abs_val >= 1e4:
        scaled, unit = yuan_value / 1e4, "万"
    else:
        rounded = _round_half_up(yuan_value, 2)
        return f"{rounded:.2f}"
    rounded = _round_half_up(scaled, 2)
    if rounded == 0:
        return "0.00"
    text = f"{rounded:.2f}".rstrip("0").rstrip(".")
    return f"{text}{unit}"


async def load_etf_close_prices_with_status(
    broker: object | None,
    symbol: str,
    limit: int = 280,
) -> tuple[dict[str, tuple[float, float | None]], str | None]:
    """Fetch public qfq daily closes mapped by YYYY-MM-DD alongside any non-fatal error code."""
    if broker is None:
        return {}, None
    series_fn = getattr(broker, "series", None)
    if not callable(series_fn):
        return {}, None
    try:
        page = await series_fn(symbol, "day", None, limit)
    except Exception as error:
        return {}, fixed_market_error_code(error, "DAILY_KLINE_UNAVAILABLE")
    bars = getattr(page, "bars", None) or ()
    result: dict[str, tuple[float, float | None]] = {}
    prev_close: float | None = None
    for bar in bars:
        trade_date = getattr(bar, "time", None)
        close_val = _float_or_none(getattr(bar, "close", None))
        if not isinstance(trade_date, str) or close_val is None:
            continue
        change = _round_half_up(close_val - prev_close, 4) if prev_close is not None else None
        result[trade_date] = (close_val, change)
        prev_close = close_val
    page_error = getattr(page, "source_error", None)
    if not result and page_error:
        return {}, fixed_market_error_code(RuntimeError(str(page_error)), str(page_error))
    return result, None


async def load_etf_close_prices(
    broker: object | None,
    symbol: str,
    limit: int = 280,
) -> dict[str, tuple[float, float | None]]:
    """Fetch public qfq daily closes mapped by YYYY-MM-DD, degrading to empty on error."""
    prices, _error = await load_etf_close_prices_with_status(broker, symbol, limit=limit)
    return prices


def _accepts_kwarg(fn: object, name: str) -> bool:
    try:
        sig = inspect.signature(fn)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    param = sig.parameters.get(name)
    if param is not None and param.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    ):
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


async def fetch_etf_flow_payload(
    service: object,
    broker: object | None,
    symbol: str,
    period: str,
) -> dict[str, Any]:
    """Load close prices and invoke ``service.fetch`` while tolerating legacy stubs."""
    close_prices, close_prices_error = await load_etf_close_prices_with_status(broker, symbol)
    kwargs: dict[str, Any] = {"close_prices": close_prices}
    if _accepts_kwarg(service.fetch, "close_prices_error"):  # type: ignore[attr-defined]
        kwargs["close_prices_error"] = close_prices_error
    return await asyncio.to_thread(service.fetch, symbol, period, **kwargs)  # type: ignore[attr-defined]


class SQLiteEtfFundFlowCache:
    """Thread-safe WAL SQLite store preserving ETF sub-responses across restarts."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._closed = False
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS etf_flow_cache (
                cache_key TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                kind TEXT NOT NULL,
                data_date TEXT,
                updated_at REAL NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_etf_flow_cache_updated_at ON etf_flow_cache(updated_at)"
        )
        self._conn.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get(self, cache_key: str) -> tuple[Any, float, str | None] | None:
        with self._lock:
            if self._closed:
                return None
            row = self._conn.execute(
                "SELECT payload_json, updated_at, data_date FROM etf_flow_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        data_date = str(row["data_date"]).strip() if row["data_date"] else None
        return value, float(row["updated_at"]), data_date

    def put(
        self,
        cache_key: str,
        *,
        symbol: str,
        kind: str,
        data_date: str | None,
        updated_at: float,
        value: Any,
    ) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            if self._closed:
                return
            self._conn.execute(
                """
                INSERT INTO etf_flow_cache (
                    cache_key, symbol, kind, data_date, updated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    symbol = excluded.symbol,
                    kind = excluded.kind,
                    data_date = excluded.data_date,
                    updated_at = excluded.updated_at,
                    payload_json = excluded.payload_json
                """,
                (cache_key, symbol, kind, data_date, float(updated_at), encoded),
            )
            self._conn.commit()

    def prune_before(self, cutoff_epoch: float) -> None:
        with self._lock:
            if self._closed:
                return
            self._conn.execute(
                "DELETE FROM etf_flow_cache WHERE updated_at < ?",
                (float(cutoff_epoch),),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()


@dataclass
class _CacheEntry:
    value: Any
    stored_at: float
    updated_at_epoch: float
    data_date: str | None = None
    ttl_override: float | None = None


@dataclass
class _PartResult:
    value: Any
    stale: bool
    error: str | None
    present: bool
    from_cache: bool
    data_date: str | None = None


def _normalize_line_rows(raw: Any) -> list[tuple[str, str, float | None]]:
    if not isinstance(raw, list):
        return []
    out: list[tuple[str, str, float | None]] = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        out.append((str(row[0]), str(row[1]), _float_or_none(row[2])))
    return out


def _derive_summary_from_line(
    raw_line: list[tuple[str, str, float | None]],
) -> dict[str, Any]:
    valid = [(trade_date, flow) for trade_date, _short, flow in raw_line if flow is not None]
    if not valid:
        return {}
    derived: dict[str, Any] = {
        "stockEtfNetBuyFlowDate": valid[-1][0],
        "stockEtfNetBuyFlow": valid[-1][1],
    }
    for window, field in (
        (3, "stockEtfNetBuyFlow3d"),
        (5, "stockEtfNetBuyFlow5d"),
        (20, "stockEtfNetBuyFlow20d"),
        (60, "stockEtfNetBuyFlow60d"),
    ):
        if len(valid) >= window:
            derived[field] = sum(item[1] for item in valid[-window:])
    return derived


class TonghuashunEtfFundFlowService:
    """Fetch, cache, and fall back THS ETF subscription/redemption flow directly over HTTPS."""

    def __init__(
        self,
        *,
        transport: SafeHttpTransport | None = None,
        timeout_seconds: float = 8.0,
        ttl_seconds: float = 300.0,
        max_cache_entries: int = 512,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(SHANGHAI_TZ),
        attempts: int = 3,
        retry_backoff_seconds: float = 0.2,
        morning_pending_ttl_seconds: float = 60.0,
        settled_ttl_seconds: float = 1800.0,
        max_stale_seconds: float = 7 * 86400.0,
        cache_path: Path | None = None,
        is_trading_day: Callable[[date], bool] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_cache_entries <= 0:
            raise ValueError("max_cache_entries must be positive")
        if attempts <= 0:
            raise ValueError("attempts must be positive")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must not be negative")
        if morning_pending_ttl_seconds <= 0:
            raise ValueError("morning_pending_ttl_seconds must be positive")
        if settled_ttl_seconds <= 0:
            raise ValueError("settled_ttl_seconds must be positive")
        if max_stale_seconds < 0:
            raise ValueError("max_stale_seconds must not be negative")

        self.transport = transport or SafeHttpTransport(
            ETF_FLOW_BASE_URL,
            max_body_bytes=2 * 1024 * 1024,
        )
        self.timeout_seconds = timeout_seconds
        self.ttl_seconds = ttl_seconds
        self.max_cache_entries = max_cache_entries
        self.monotonic_clock = monotonic_clock
        self.wall_clock = wall_clock
        self.attempts = attempts
        self.retry_backoff_seconds = retry_backoff_seconds
        self.morning_pending_ttl_seconds = morning_pending_ttl_seconds
        self.settled_ttl_seconds = settled_ttl_seconds
        self.max_stale_seconds = max_stale_seconds
        self.is_trading_day = is_trading_day or _default_is_trading_day
        self.sleeper = sleeper
        self._lock = RLock()
        self._resource_locks: dict[str, Lock] = {}
        self._summary_cache: dict[str, _CacheEntry] = {}
        self._holdings_cache: dict[str, _CacheEntry] = {}
        self._line_cache: dict[tuple[str, str], _CacheEntry] = {}
        self._disk_cache = SQLiteEtfFundFlowCache(cache_path) if cache_path is not None else None
        self._last_prune_mono = monotonic_clock()

    def close(self) -> None:
        if self._disk_cache is not None:
            self._disk_cache.close()

    def _lock_for(self, resource_key: str) -> Lock:
        with self._lock:
            if len(self._resource_locks) > self.max_cache_entries * 4:
                for key, lock in tuple(self._resource_locks.items()):
                    if not lock.locked():
                        self._resource_locks.pop(key, None)
            return self._resource_locks.setdefault(resource_key, Lock())

    def _sleep_backoff(self, attempt: int) -> None:
        delay = self.retry_backoff_seconds * (2 ** attempt)
        if delay > 0:
            self.sleeper(delay)

    def _get_json(self, path: str) -> dict[str, Any]:
        last_code = "ETF_FUND_FLOW_UNAVAILABLE"
        for attempt in range(self.attempts):
            request = Request(
                f"{ETF_FLOW_BASE_URL}{path}",
                headers=dict(_HEADERS),
                method="GET",
            )
            try:
                response = self.transport.request(request, self.timeout_seconds)
            except SafeHttpError:
                last_code = "ETF_FUND_FLOW_UNAVAILABLE"
                if attempt + 1 < self.attempts:
                    self._sleep_backoff(attempt)
                    continue
                raise EtfFundFlowError(last_code) from None

            if response.status in _NON_RETRYABLE_HTTP_STATUSES:
                raise EtfFundFlowError("ETF_FUND_FLOW_HTTP_4XX")
            if response.status == 429:
                last_code = "ETF_FUND_FLOW_RATE_LIMITED"
                if attempt + 1 < self.attempts:
                    self._sleep_backoff(attempt)
                    continue
                raise EtfFundFlowError(last_code)
            if response.status != 200:
                last_code = "ETF_FUND_FLOW_UNAVAILABLE"
                if attempt + 1 < self.attempts:
                    self._sleep_backoff(attempt)
                    continue
                raise EtfFundFlowError(last_code)

            try:
                payload = json.loads(response.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                last_code = "ETF_FUND_FLOW_UNAVAILABLE"
                if attempt + 1 < self.attempts:
                    self._sleep_backoff(attempt)
                    continue
                raise EtfFundFlowError(last_code) from None

            if not isinstance(payload, dict):
                last_code = "ETF_FUND_FLOW_UNAVAILABLE"
                if attempt + 1 < self.attempts:
                    self._sleep_backoff(attempt)
                    continue
                raise EtfFundFlowError(last_code)

            status_code = payload.get("status_code")
            code = payload.get("code")
            if status_code != 0 and code != 0:
                last_code = "ETF_FUND_FLOW_UNAVAILABLE"
                if attempt + 1 < self.attempts:
                    self._sleep_backoff(attempt)
                    continue
                raise EtfFundFlowError(last_code)
            return payload
        raise EtfFundFlowError(last_code)

    def _fresh_ttl_for_entry(
        self,
        entry: _CacheEntry,
        *,
        now_sh: datetime,
        target_t1: str,
        in_morning_window: bool,
    ) -> float:
        if entry.ttl_override is not None:
            return entry.ttl_override
        data_date = entry.data_date
        if in_morning_window and (not data_date or data_date < target_t1):
            base_ttl = min(self.ttl_seconds, self.morning_pending_ttl_seconds)
        elif data_date and data_date >= target_t1:
            base_ttl = self.settled_ttl_seconds
        else:
            base_ttl = self.ttl_seconds

        if self.is_trading_day(now_sh.date()) and (not data_date or data_date < target_t1):
            boundary_0850 = now_sh.replace(hour=8, minute=50, second=0, microsecond=0)
            if now_sh < boundary_0850:
                until_boundary = max(5.0, (boundary_0850 - now_sh).total_seconds())
                base_ttl = min(base_ttl, until_boundary)
        return base_ttl

    def _is_entry_fresh(
        self,
        entry: _CacheEntry,
        *,
        now_mono: float,
        now_sh: datetime,
        target_t1: str,
        in_morning_window: bool,
    ) -> bool:
        if self.is_trading_day(now_sh.date()):
            boundary_0850 = now_sh.replace(hour=8, minute=50, second=0, microsecond=0)
            if (
                now_sh >= boundary_0850
                and entry.updated_at_epoch < boundary_0850.timestamp()
                and (not entry.data_date or entry.data_date < target_t1)
            ):
                return False
        effective_ttl = self._fresh_ttl_for_entry(
            entry,
            now_sh=now_sh,
            target_t1=target_t1,
            in_morning_window=in_morning_window,
        )
        age = max(0.0, now_mono - entry.stored_at)
        return age < effective_ttl

    def _peek_entry(
        self,
        mem_cache: dict[Any, _CacheEntry],
        mem_key: Any,
        disk_key: str,
        *,
        normalize: Callable[[Any], Any] | None = None,
    ) -> _CacheEntry | None:
        now_mono = self.monotonic_clock()
        now_sh = _as_shanghai(self.wall_clock())
        now_epoch = now_sh.timestamp()
        with self._lock:
            entry = mem_cache.get(mem_key)
        if entry is not None:
            return entry
        if self._disk_cache is None:
            return None
        disk_row = self._disk_cache.get(disk_key)
        if disk_row is None:
            return None
        raw_value, updated_at_epoch, data_date = disk_row
        value = normalize(raw_value) if normalize is not None else raw_value
        wall_age = max(0.0, now_epoch - updated_at_epoch)
        hydrated = _CacheEntry(
            value=value,
            stored_at=now_mono - wall_age,
            updated_at_epoch=updated_at_epoch,
            data_date=data_date,
        )
        with self._lock:
            mem_cache.setdefault(mem_key, hydrated)
        return hydrated

    def _store_entry(
        self,
        mem_cache: dict[Any, _CacheEntry],
        mem_key: Any,
        disk_key: str,
        *,
        symbol: str,
        kind: str,
        value: Any,
        data_date: str | None,
    ) -> _CacheEntry:
        now_mono = self.monotonic_clock()
        now_epoch = _as_shanghai(self.wall_clock()).timestamp()
        entry = _CacheEntry(
            value=value,
            stored_at=now_mono,
            updated_at_epoch=now_epoch,
            data_date=data_date,
        )
        with self._lock:
            mem_cache[mem_key] = entry
            while len(mem_cache) > self.max_cache_entries:
                oldest_key = next(iter(mem_cache))
                mem_cache.pop(oldest_key, None)
        if self._disk_cache is not None:
            self._disk_cache.put(
                disk_key,
                symbol=symbol,
                kind=kind,
                data_date=data_date,
                updated_at=now_epoch,
                value=value,
            )
            if self.max_stale_seconds > 0 and now_mono - self._last_prune_mono >= 3600.0:
                self._last_prune_mono = now_mono
                self._disk_cache.prune_before(now_epoch - self.max_stale_seconds)
        return entry

    def _clamp_entry_ttl(
        self,
        mem_cache: dict[Any, _CacheEntry],
        mem_key: Any,
        max_ttl: float,
    ) -> None:
        with self._lock:
            entry = mem_cache.get(mem_key)
            if entry is not None:
                entry.ttl_override = (
                    min(entry.ttl_override, max_ttl)
                    if entry.ttl_override is not None
                    else max_ttl
                )

    def _update_entry_data_date(
        self,
        mem_cache: dict[Any, _CacheEntry],
        mem_key: Any,
        disk_key: str,
        *,
        symbol: str,
        kind: str,
        data_date: str,
    ) -> None:
        with self._lock:
            entry = mem_cache.get(mem_key)
            if entry is None or entry.data_date == data_date:
                return
            entry.data_date = data_date
            updated_at = entry.updated_at_epoch
            value = entry.value
        if self._disk_cache is not None:
            self._disk_cache.put(
                disk_key,
                symbol=symbol,
                kind=kind,
                data_date=data_date,
                updated_at=updated_at,
                value=value,
            )

    @staticmethod
    def _parse_summary(payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict):
            return {}
        indexes = data.get("indexes") or []
        rows = data.get("data") or []
        if not isinstance(indexes, list) or not isinstance(rows, list) or not rows:
            return {}
        first_row = rows[0] if isinstance(rows[0], dict) else {}
        raw_values = first_row.get("values") or []
        index_names: dict[int, str] = {}
        for pos, item in enumerate(indexes):
            if isinstance(item, dict) and item.get("index_id"):
                index_names[pos] = str(item["index_id"])
        mapped: dict[str, Any] = {}
        for pos, item in enumerate(raw_values):
            if not isinstance(item, dict):
                continue
            idx = item.get("idx")
            key = index_names.get(int(idx) if isinstance(idx, int) else pos)
            if key:
                mapped[key] = item.get("value")
        return mapped

    @staticmethod
    def _parse_line(payload: dict[str, Any]) -> list[tuple[str, str, float | None]]:
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        time_range = data.get("time_range") or []
        series_rows = data.get("data") or []
        if not isinstance(time_range, list) or not time_range or not isinstance(series_rows, list):
            return []
        indexes = data.get("indexes") or []
        target_idx = 0
        for pos, item in enumerate(indexes):
            if isinstance(item, dict) and item.get("index_id") == "stock_etf_net_buy_flow_amount":
                target_idx = pos
                break
        row0 = series_rows[0] if series_rows and isinstance(series_rows[0], dict) else {}
        values_list = row0.get("values") or []
        flow_values: list[Any] = []
        for entry in values_list:
            if isinstance(entry, dict) and entry.get("idx") == target_idx:
                flow_values = entry.get("values") or []
                break
        if not flow_values and values_list and isinstance(values_list[0], dict):
            flow_values = values_list[0].get("values") or []

        parsed: list[tuple[str, str, float | None]] = []
        for pos, raw_ts in enumerate(time_range):
            try:
                ts_int = int(str(raw_ts).strip())
            except (TypeError, ValueError):
                continue
            dt = datetime.fromtimestamp(ts_int, tz=SHANGHAI_TZ)
            trade_date = dt.strftime("%Y-%m-%d")
            short_date = dt.strftime("%m-%d")
            raw_val = flow_values[pos] if pos < len(flow_values) else None
            parsed.append((trade_date, short_date, _float_or_none(raw_val)))
        return parsed

    @staticmethod
    def _parse_holdings(payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        entity_info = data.get("entity_info") or []
        entity_list = data.get("entity") or []
        if not isinstance(entity_info, list) or not isinstance(entity_list, list) or not entity_list:
            return []
        col_index: dict[str, int] = {}
        for pos, col in enumerate(entity_info):
            col_name = str(col)
            col_index.setdefault(col_name, pos)
        entities_outer = entity_list[0].get("entities") if isinstance(entity_list[0], dict) else None
        if not isinstance(entities_outer, list) or not entities_outer:
            return []
        rows = entities_outer[0]
        if not isinstance(rows, list):
            return []

        def cell(row: list[Any], name: str) -> Any:
            idx = col_index.get(name)
            return row[idx] if idx is not None and idx < len(row) else None

        items: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, list):
                continue
            raw_code = str(cell(row, "code") or "").strip()
            if ":" in raw_code:
                etf_market, etf_code = raw_code.split(":", 1)
            else:
                etf_market, etf_code = "", raw_code
            etf_name = str(cell(row, "name") or "").strip()
            if not etf_code or not etf_name:
                continue
            net_inflow_raw = _float_or_none(cell(row, "stock_etf_subred_net_buy_flow_amount"))
            streak_days = _int_or_none(cell(row, "etfNetBuyFlowStreakCount"))
            hold_rate = _float_or_none(cell(row, "stock_etf_subred_holdrate"))
            items.append(
                {
                    "symbol": etf_code,
                    "market": etf_market,
                    "name": etf_name,
                    "net_inflow_raw": net_inflow_raw,
                    "net_inflow_text": format_etf_compact_rmb(net_inflow_raw),
                    "tone": _tone(net_inflow_raw),
                    "streak_days": streak_days,
                    "hold_rate": round(hold_rate, 2) if hold_rate is not None else None,
                    "hold_rate_text": f"{hold_rate:.2f}%" if hold_rate is not None else "--",
                }
            )
        items.sort(key=lambda item: (item["net_inflow_raw"] is None, -(item["net_inflow_raw"] or 0.0)))
        return items

    def _fetch_summary_live(self, symbol: str, market: str) -> tuple[dict[str, Any], str | None]:
        fields = ",".join(_SUMMARY_FIELDS)
        payload = self._get_json(
            f"/quotation/data/query/gateway/cache/v1/table/{market}:{symbol}/stock_code/{fields}"
        )
        parsed = self._parse_summary(payload)
        raw_date = parsed.get("stockEtfNetBuyFlowDate")
        data_date = str(raw_date).strip() if raw_date else None
        return parsed, data_date or None

    def _fetch_line_live(
        self,
        symbol: str,
        market: str,
        period: str,
    ) -> tuple[list[tuple[str, str, float | None]], str | None]:
        offset, _label = ETF_FLOW_PERIODS[period]
        now_sh = _as_shanghai(self.wall_clock())
        midnight_unix = int(now_sh.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        payload = self._get_json(
            f"/quotation/data/query/gateway/cache/v1/line/{market}:{symbol}/{_LINE_FIELDS}/DAY_1/{midnight_unix}/{offset}"
        )
        parsed = self._parse_line(payload)
        data_date = parsed[-1][0] if parsed else None
        return parsed, data_date

    def _fetch_holdings_live(self, symbol: str, market: str) -> tuple[list[dict[str, Any]], str | None]:
        payload = self._get_json(
            f"/quotation/stock/capital_tab/v1/etf_list/hold_stock/{market}:{symbol}"
        )
        return self._parse_holdings(payload), None

    def _load_part(
        self,
        *,
        mem_cache: dict[Any, _CacheEntry],
        mem_key: Any,
        disk_key: str,
        symbol: str,
        kind: str,
        fetcher: Callable[[], tuple[Any, str | None]],
        empty_factory: Callable[[], Any],
        target_t1: str,
        in_morning_window: bool,
        force_live: bool = False,
        normalize: Callable[[Any], Any] | None = None,
    ) -> _PartResult:
        with self._lock_for(disk_key):
            now_mono = self.monotonic_clock()
            now_sh = _as_shanghai(self.wall_clock())
            existing = self._peek_entry(mem_cache, mem_key, disk_key, normalize=normalize)
            if (
                not force_live
                and existing is not None
                and self._is_entry_fresh(
                    existing,
                    now_mono=now_mono,
                    now_sh=now_sh,
                    target_t1=target_t1,
                    in_morning_window=in_morning_window,
                )
            ):
                return _PartResult(
                    value=existing.value,
                    stale=False,
                    error=None,
                    present=True,
                    from_cache=True,
                    data_date=existing.data_date,
                )

            try:
                live_value, data_date = fetcher()
            except EtfFundFlowError as error:
                fallback = existing or self._peek_entry(
                    mem_cache,
                    mem_key,
                    disk_key,
                    normalize=normalize,
                )
                if fallback is not None:
                    mono_age = max(0.0, self.monotonic_clock() - fallback.stored_at)
                    wall_age = max(0.0, _as_shanghai(self.wall_clock()).timestamp() - fallback.updated_at_epoch)
                    if max(mono_age, wall_age) <= self.max_stale_seconds:
                        return _PartResult(
                            value=fallback.value,
                            stale=True,
                            error=error.error_code,
                            present=True,
                            from_cache=True,
                            data_date=fallback.data_date,
                        )
                return _PartResult(
                    value=empty_factory(),
                    stale=False,
                    error=error.error_code,
                    present=False,
                    from_cache=False,
                    data_date=None,
                )

            self._store_entry(
                mem_cache,
                mem_key,
                disk_key,
                symbol=symbol,
                kind=kind,
                value=live_value,
                data_date=data_date,
            )
            return _PartResult(
                value=live_value,
                stale=False,
                error=None,
                present=True,
                from_cache=False,
                data_date=data_date,
            )

    def has_settled_cache(self, symbol: str, period: str = "60d") -> bool:
        """Return True without network I/O when all parts are cached through T-1."""
        normalized = str(symbol or "").strip()
        if period not in ETF_FLOW_PERIODS:
            return False
        try:
            market = market_code_for_symbol(normalized)
        except UnsupportedMarketError:
            return False
        if is_exchange_fund_symbol(normalized, market):
            return True

        _, target_t1, _ = expected_etf_trade_date(self.wall_clock(), self.is_trading_day)
        summary_entry = self._peek_entry(
            self._summary_cache,
            normalized,
            f"summary:{normalized}",
        )
        line_entry = self._peek_entry(
            self._line_cache,
            (normalized, period),
            f"line:{normalized}:{period}",
            normalize=_normalize_line_rows,
        )
        holdings_entry = self._peek_entry(
            self._holdings_cache,
            normalized,
            f"holdings:{normalized}",
        )
        for entry in (summary_entry, line_entry, holdings_entry):
            if entry is None or not entry.data_date or entry.data_date < target_t1:
                return False
        return True

    def _empty_response(self, symbol: str, market: str, period: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "market": market,
            "period": period,
            "available": False,
            "data_date": None,
            "expected_trade_date": None,
            "stale": False,
            "degraded": False,
            "update_status": "empty",
            "source_errors": {
                "summary": None,
                "line": None,
                "holdings": None,
                "daily_kline": None,
            },
            "summary": {
                "latest_badge": None,
                "metrics": [],
            },
            "chart": {
                "cumulative_unit": "万",
                "daily_unit": "万",
                "points": [],
            },
            "holdings": {
                "total_count": 0,
                "items": [],
            },
        }

    def fetch(
        self,
        symbol: str,
        period: str = "60d",
        *,
        close_prices: Mapping[str, tuple[float, float | None]] | None = None,
        close_prices_error: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        normalized = str(symbol or "").strip()
        if period not in ETF_FLOW_PERIODS:
            raise ValueError(f"unsupported etf flow period: {period}")
        try:
            market = market_code_for_symbol(normalized)
        except UnsupportedMarketError as error:
            raise ValueError(str(error)) from error

        if is_exchange_fund_symbol(normalized, market):
            return self._empty_response(normalized, market, period)

        now_sh = _as_shanghai(self.wall_clock())
        expected_date, target_t1, in_morning_window = expected_etf_trade_date(
            now_sh,
            self.is_trading_day,
        )

        summary_part = self._load_part(
            mem_cache=self._summary_cache,
            mem_key=normalized,
            disk_key=f"summary:{normalized}",
            symbol=normalized,
            kind="summary",
            fetcher=lambda: self._fetch_summary_live(normalized, market),
            empty_factory=dict,
            target_t1=target_t1,
            in_morning_window=in_morning_window,
            force_live=force,
        )
        line_part = self._load_part(
            mem_cache=self._line_cache,
            mem_key=(normalized, period),
            disk_key=f"line:{normalized}:{period}",
            symbol=normalized,
            kind=f"line:{period}",
            fetcher=lambda: self._fetch_line_live(normalized, market, period),
            empty_factory=list,
            target_t1=target_t1,
            in_morning_window=in_morning_window,
            force_live=force,
            normalize=_normalize_line_rows,
        )
        holdings_part = self._load_part(
            mem_cache=self._holdings_cache,
            mem_key=normalized,
            disk_key=f"holdings:{normalized}",
            symbol=normalized,
            kind="holdings",
            fetcher=lambda: self._fetch_holdings_live(normalized, market),
            empty_factory=list,
            target_t1=target_t1,
            in_morning_window=in_morning_window,
            force_live=force,
        )

        # Heal cross-endpoint date skew when one endpoint updates before the other around 09:00.
        summary_date = str(summary_part.value.get("stockEtfNetBuyFlowDate") or "").strip() or None
        line_last_date = line_part.value[-1][0] if line_part.value else None
        if summary_date and line_last_date and summary_date != line_last_date:
            if summary_date < line_last_date and summary_part.from_cache:
                refreshed_summary = self._load_part(
                    mem_cache=self._summary_cache,
                    mem_key=normalized,
                    disk_key=f"summary:{normalized}",
                    symbol=normalized,
                    kind="summary",
                    fetcher=lambda: self._fetch_summary_live(normalized, market),
                    empty_factory=dict,
                    target_t1=target_t1,
                    in_morning_window=in_morning_window,
                    force_live=True,
                )
                if refreshed_summary.present:
                    summary_part = refreshed_summary
                    summary_date = (
                        str(summary_part.value.get("stockEtfNetBuyFlowDate") or "").strip() or None
                    )
            elif line_last_date < summary_date and line_part.from_cache:
                refreshed_line = self._load_part(
                    mem_cache=self._line_cache,
                    mem_key=(normalized, period),
                    disk_key=f"line:{normalized}:{period}",
                    symbol=normalized,
                    kind=f"line:{period}",
                    fetcher=lambda: self._fetch_line_live(normalized, market, period),
                    empty_factory=list,
                    target_t1=target_t1,
                    in_morning_window=in_morning_window,
                    force_live=True,
                    normalize=_normalize_line_rows,
                )
                if refreshed_line.present:
                    line_part = refreshed_line
                    line_last_date = line_part.value[-1][0] if line_part.value else None

        fresh_dates = [d for d in (summary_date, line_last_date) if d]
        newest_date = max(fresh_dates) if fresh_dates else None
        if (
            newest_date is not None
            and holdings_part.from_cache
            and (not holdings_part.data_date or holdings_part.data_date < newest_date)
        ):
            refreshed_holdings = self._load_part(
                mem_cache=self._holdings_cache,
                mem_key=normalized,
                disk_key=f"holdings:{normalized}",
                symbol=normalized,
                kind="holdings",
                fetcher=lambda: self._fetch_holdings_live(normalized, market),
                empty_factory=list,
                target_t1=target_t1,
                in_morning_window=in_morning_window,
                force_live=True,
            )
            if refreshed_holdings.present:
                holdings_part = refreshed_holdings

        if summary_date and line_last_date and summary_date != line_last_date:
            self._clamp_entry_ttl(
                self._summary_cache,
                normalized,
                self.morning_pending_ttl_seconds,
            )
            self._clamp_entry_ttl(
                self._line_cache,
                (normalized, period),
                self.morning_pending_ttl_seconds,
            )

        if not summary_part.present and not line_part.present and not holdings_part.present:
            raise EtfFundFlowError(
                summary_part.error
                or line_part.error
                or holdings_part.error
                or "ETF_FUND_FLOW_UNAVAILABLE"
            )

        if holdings_part.present and not holdings_part.stale and newest_date:
            self._update_entry_data_date(
                self._holdings_cache,
                normalized,
                f"holdings:{normalized}",
                symbol=normalized,
                kind="holdings",
                data_date=newest_date,
            )

        raw_line: list[tuple[str, str, float | None]] = (
            list(line_part.value) if line_part.present else []
        )
        raw_summary: dict[str, Any] = dict(summary_part.value) if summary_part.present else {}
        if line_part.present:
            derived_summary = _derive_summary_from_line(raw_line)
            if not summary_part.present:
                raw_summary = derived_summary
            elif (
                line_last_date is not None
                and (summary_date is None or line_last_date > summary_date)
            ):
                for key, val in derived_summary.items():
                    raw_summary[key] = val
            else:
                for key, val in derived_summary.items():
                    if raw_summary.get(key) is None:
                        raw_summary[key] = val

        holdings_items: list[dict[str, Any]] = (
            list(holdings_part.value) if holdings_part.present else []
        )

        # Build chart cumulative + daily series in yuan/wan/yi
        running_wan = 0.0
        has_any_flow = False
        interim: list[dict[str, Any]] = []
        last_cumulative_yuan: float | None = None

        for trade_date, short_date, flow_yuan in raw_line:
            if flow_yuan is not None:
                has_any_flow = True
                daily_wan = round(flow_yuan / 1e4, 4)
                running_wan = _round_half_up(running_wan + daily_wan, 2)
                cumulative_yuan: float | None = running_wan * 1e4
                last_cumulative_yuan = cumulative_yuan
            else:
                daily_wan = None
                cumulative_yuan = running_wan * 1e4 if has_any_flow else None
            interim.append(
                {
                    "date": trade_date,
                    "short_date": short_date,
                    "daily_yuan": flow_yuan,
                    "cumulative_yuan": cumulative_yuan,
                }
            )

        cum_uses_yi = any(
            item["cumulative_yuan"] is not None and abs(item["cumulative_yuan"]) >= 1e8
            for item in interim
        )
        daily_uses_yi = any(
            item["daily_yuan"] is not None and abs(item["daily_yuan"]) >= 1e8
            for item in interim
        )
        cumulative_unit = "亿" if cum_uses_yi else "万"
        daily_unit = "亿" if daily_uses_yi else "万"
        cum_divisor = 1e8 if cum_uses_yi else 1e4
        daily_divisor = 1e8 if daily_uses_yi else 1e4

        price_map = close_prices or {}
        chart_points: list[dict[str, Any]] = []
        for item in interim:
            cum_yuan = item["cumulative_yuan"]
            day_yuan = item["daily_yuan"]
            close_entry = price_map.get(item["date"])
            close_price = _round_half_up(close_entry[0], 2) if close_entry is not None else None
            price_change = close_entry[1] if close_entry is not None else None
            chart_points.append(
                {
                    "date": item["date"],
                    "short_date": item["short_date"],
                    "cumulative_value": (
                        _round_half_up(cum_yuan / cum_divisor, 2)
                        if cum_yuan is not None
                        else None
                    ),
                    "cumulative_text": format_etf_flow_text(cum_yuan),
                    "daily_value": (
                        _round_half_up(day_yuan / daily_divisor, 2)
                        if day_yuan is not None
                        else None
                    ),
                    "daily_text": format_etf_flow_text(day_yuan),
                    "close_price": close_price,
                    "price_change": price_change,
                }
            )

        # Build summary header & 4-card metrics
        latest_date_raw = raw_summary.get("stockEtfNetBuyFlowDate")
        latest_date_str = str(latest_date_raw).strip() if latest_date_raw else ""
        latest_flow_yuan = _float_or_none(raw_summary.get("stockEtfNetBuyFlow"))
        latest_badge = None
        if latest_date_str or latest_flow_yuan is not None:
            latest_text = format_etf_flow_text(latest_flow_yuan)
            latest_badge = {
                "date": latest_date_str[-5:] if latest_date_str else "--",
                "full_date": latest_date_str or None,
                "value_text": latest_text or "--",
                "text": (
                    f"{latest_date_str[-5:] if latest_date_str else '--'} 净流入 {latest_text}"
                    if latest_text is not None
                    else (latest_date_str[-5:] if latest_date_str else "--")
                ),
                "tone": _tone(latest_flow_yuan),
            }

        if period == "120d":
            first_key, first_label = "120d", "120日净流入"
            first_yuan = last_cumulative_yuan
        elif period == "250d":
            first_key, first_label = "250d", "250日净流入"
            first_yuan = last_cumulative_yuan
        else:
            first_key, first_label = "60d", "60日净流入"
            first_yuan = _float_or_none(raw_summary.get("stockEtfNetBuyFlow60d"))

        metric_specs = (
            (first_key, first_label, first_yuan),
            ("20d", "20日净流入", _float_or_none(raw_summary.get("stockEtfNetBuyFlow20d"))),
            ("5d", "5日净流入", _float_or_none(raw_summary.get("stockEtfNetBuyFlow5d"))),
            ("3d", "3日净流入", _float_or_none(raw_summary.get("stockEtfNetBuyFlow3d"))),
        )
        metrics: list[dict[str, Any]] = []
        has_summary_metric = False
        for key, label, yuan_val in metric_specs:
            formatted = format_etf_flow_text(yuan_val)
            if formatted is not None:
                has_summary_metric = True
            metrics.append(
                {
                    "key": key,
                    "label": label,
                    "value_text": formatted or "--",
                    "tone": _tone(yuan_val),
                }
            )

        available = bool(
            (latest_badge is not None and latest_flow_yuan is not None)
            or has_summary_metric
            or (chart_points and has_any_flow)
            or holdings_items
        )
        resolved_data_date = latest_date_str or line_last_date or None
        stale = bool(summary_part.stale or line_part.stale or holdings_part.stale)
        degraded = bool(
            not summary_part.present or not line_part.present or not holdings_part.present
        )
        if not available:
            update_status = "empty"
        elif stale:
            update_status = "stale_fallback"
        elif degraded:
            update_status = "partial"
        elif (
            resolved_data_date is not None
            and resolved_data_date < target_t1
            and (in_morning_window or now_sh.hour >= 9)
        ):
            update_status = "pending_morning_update"
        else:
            update_status = "settled"

        return {
            "symbol": normalized,
            "market": market,
            "period": period,
            "available": available,
            "data_date": resolved_data_date,
            "expected_trade_date": expected_date,
            "stale": stale,
            "degraded": degraded,
            "update_status": update_status,
            "source_errors": {
                "summary": summary_part.error,
                "line": line_part.error,
                "holdings": holdings_part.error,
                "daily_kline": close_prices_error,
            },
            "summary": {
                "latest_badge": latest_badge,
                "metrics": metrics,
            },
            "chart": {
                "cumulative_unit": cumulative_unit,
                "daily_unit": daily_unit,
                "points": chart_points,
            },
            "holdings": {
                "total_count": len(holdings_items),
                "items": holdings_items,
            },
        }


class EtfFundFlowMonitor:
    """Morning prewarm monitor refreshing watchlist ETF flow after ~09:05 on trading days."""

    def __init__(
        self,
        accounts: object,
        service: TonghuashunEtfFundFlowService,
        *,
        is_trading_day: Callable[[date], bool] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(SHANGHAI_TZ),
        cooldown_seconds: float = 180.0,
        pacing_seconds: float = 0.25,
        max_symbols_per_cycle: int = 30,
    ) -> None:
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must not be negative")
        if pacing_seconds < 0:
            raise ValueError("pacing_seconds must not be negative")
        if max_symbols_per_cycle <= 0:
            raise ValueError("max_symbols_per_cycle must be positive")
        self.accounts = accounts
        self.service = service
        self.is_trading_day = is_trading_day or _default_is_trading_day
        self.clock = clock
        self.cooldown_seconds = cooldown_seconds
        self.pacing_seconds = pacing_seconds
        self.max_symbols_per_cycle = max_symbols_per_cycle
        self._startup_checked = False
        self._last_attempt_at: dict[str, datetime] = {}

    def _in_morning_prewarm_window(self, now_sh: datetime) -> bool:
        if not self.is_trading_day(now_sh.date()):
            return False
        minute = now_sh.hour * 60 + now_sh.minute
        return 9 * 60 + 5 <= minute <= 11 * 60 + 35

    def _eligible_symbols(self) -> list[str]:
        list_fn = getattr(self.accounts, "list_watchlist_symbols", None)
        if not callable(list_fn):
            return []
        symbols: list[str] = []
        seen: set[str] = set()
        for raw in list_fn() or ():
            symbol = str(raw or "").strip()
            if not _SIX_DIGIT_RE.fullmatch(symbol) or symbol in seen:
                continue
            try:
                market = market_code_for_symbol(symbol)
            except UnsupportedMarketError:
                continue
            if is_exchange_fund_symbol(symbol, market):
                continue
            seen.add(symbol)
            symbols.append(symbol)
        return symbols

    async def poll_once(
        self,
        now: datetime | None = None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        now_sh = _as_shanghai(now or self.clock())
        if (
            not force
            and self._startup_checked
            and not self._in_morning_prewarm_window(now_sh)
        ):
            return {"skipped": True, "warmed": 0, "failed": 0, "settled": 0}

        symbols = await asyncio.to_thread(self._eligible_symbols)
        if not symbols:
            self._startup_checked = True
            return {"skipped": False, "warmed": 0, "failed": 0, "settled": 0}

        warmed = 0
        failed = 0
        settled = 0
        attempted = 0
        completed_all = True

        for symbol in symbols:
            if await asyncio.to_thread(self.service.has_settled_cache, symbol, "60d"):
                settled += 1
                continue
            if attempted >= self.max_symbols_per_cycle:
                completed_all = False
                break
            last_attempt = self._last_attempt_at.get(symbol)
            if (
                not force
                and last_attempt is not None
                and (now_sh - last_attempt).total_seconds() < self.cooldown_seconds
            ):
                continue
            self._last_attempt_at[symbol] = now_sh
            attempted += 1
            try:
                await asyncio.to_thread(self.service.fetch, symbol, "60d")
                warmed += 1
            except Exception:
                failed += 1
            if self.pacing_seconds > 0 and attempted < self.max_symbols_per_cycle:
                await asyncio.sleep(self.pacing_seconds)

        if completed_all:
            self._startup_checked = True
        return {
            "skipped": False,
            "warmed": warmed,
            "failed": failed,
            "settled": settled,
        }
