"""Local, versioned security directory used for symbol discovery and confirmation.

This module deliberately owns security identity only.  It does not provide quote
or indicator data and therefore cannot become a fallback for App metrics.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .parsed_values import (
    DirectRequestError,
    SymbolLookup,
    SymbolLookupNotFoundError,
    UnsupportedMarketError,
    market_code_for_symbol,
)


class SymbolCatalogError(DirectRequestError):
    """A catalog operation failed with a stable, public-safe error code."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


def _http_get(url: str, timeout: float) -> str:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/125.0 Safari/537.36"
            ),
            "Referer": "https://finance.sina.com.cn/",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="strict")


_SINA_NODES = ("hs_a", "etf_hq_fund", "lof_hq_fund")
_NODE_KINDS = {
    "hs_a": {"17", "33", "151"},
    "etf_hq_fund": {"20", "36"},
    "lof_hq_fund": {"20", "36"},
}
_MARKET_LABELS = {"17": "沪A", "33": "深A", "151": "北交", "20": "沪基", "36": "深基"}


def _exchange_for_market(market: str) -> str:
    if market in {"17", "20"}:
        return "sh"
    if market in {"33", "36"}:
        return "sz"
    if market == "151":
        return "bj"
    raise ValueError(f"unknown catalog market: {market}")


def _text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


class SinaSymbolCatalogSource:
    """Read the public Sina category feeds in bounded, deterministic pages."""

    endpoint = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    count_endpoint = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount"

    def __init__(
        self,
        *,
        fetch: Callable[[str, float], str] = _http_get,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.fetch = fetch
        self.timeout_seconds = timeout_seconds
        self.page_size = 100

    def fetch_symbols(self) -> list[SymbolLookup]:
        results: list[SymbolLookup] = []
        seen: dict[tuple[str, str], str] = {}
        for node in _SINA_NODES:
            for item in self._fetch_node(node):
                identity = (item.symbol, item.market)
                previous_name = seen.get(identity)
                if previous_name is None:
                    seen[identity] = item.name
                    results.append(item)
                elif previous_name != item.name:
                    raise SymbolCatalogError(
                        "SYMBOL_CATALOG_SOURCE_INVALID"
                    )
        return results

    def _fetch_node(self, node: str) -> list[SymbolLookup]:
        results: list[SymbolLookup] = []
        expected_count = self._fetch_count(node)
        seen_records = 0
        page = 1
        while seen_records < expected_count:
            params = {
                "page": page,
                "num": self.page_size,
                "sort": "symbol",
                "asc": 1,
                "node": node,
                "symbol": "",
                "_s_r_a": "page",
            }
            try:
                raw = self.fetch(f"{self.endpoint}?{urlencode(params)}", self.timeout_seconds)
            except SymbolCatalogError:
                raise
            except Exception as error:
                raise SymbolCatalogError("SYMBOL_CATALOG_SOURCE_HTTP_ERROR") from error
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise SymbolCatalogError("SYMBOL_CATALOG_SOURCE_INVALID") from error
            if not isinstance(payload, list):
                raise SymbolCatalogError("SYMBOL_CATALOG_SOURCE_INVALID")
            if not payload:
                raise SymbolCatalogError("SYMBOL_CATALOG_SOURCE_INCOMPLETE")
            seen_records += len(payload)
            for row in payload:
                parsed = self._parse_row(row, node)
                if parsed is not None:
                    results.append(parsed)
            page += 1
        return results

    def _fetch_count(self, node: str) -> int:
        try:
            raw = self.fetch(
                f"{self.count_endpoint}?{urlencode({'node': node})}",
                self.timeout_seconds,
            )
        except SymbolCatalogError:
            raise
        except Exception as error:
            raise SymbolCatalogError("SYMBOL_CATALOG_SOURCE_HTTP_ERROR") from error
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise SymbolCatalogError("SYMBOL_CATALOG_SOURCE_INVALID") from error
        if isinstance(payload, dict):
            payload = payload.get("count")
        try:
            count = int(payload)
        except (TypeError, ValueError) as error:
            raise SymbolCatalogError("SYMBOL_CATALOG_SOURCE_INVALID") from error
        if count < 0:
            raise SymbolCatalogError("SYMBOL_CATALOG_SOURCE_INVALID")
        return count

    @staticmethod
    def _parse_row(row: object, node: str) -> SymbolLookup | None:
        if not isinstance(row, dict):
            return None
        symbol = _text(row.get("code"))
        source_symbol = _text(row.get("symbol"))
        name = _text(row.get("name"))
        if symbol is None or source_symbol is None or name is None:
            return None
        try:
            market = market_code_for_symbol(symbol)
        except UnsupportedMarketError:
            return None
        exchange = source_symbol[:2].lower()
        if source_symbol[2:] != symbol:
            raise SymbolCatalogError("SYMBOL_CATALOG_SOURCE_INVALID")
        if exchange != _exchange_for_market(market) or market not in _NODE_KINDS[node]:
            return None
        return SymbolLookup(symbol=symbol, name=name, market=market, market_label=_MARKET_LABELS[market])


@dataclass(frozen=True)
class CatalogRefresh:
    version: int
    count: int
    checksum: str


@dataclass(frozen=True)
class CatalogStatus:
    active_version: int | None
    count: int
    activated_at: datetime | None
    stale: bool
    checksum: str | None = None


class SQLiteSymbolCatalog:
    """Publish complete source snapshots by atomically switching a version pointer."""

    def __init__(
        self,
        path: Path,
        source: object,
        *,
        minimum_security_count: int = 5_000,
        shrink_ratio: float = 0.9,
        stale_after: timedelta = timedelta(days=7),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if minimum_security_count < 1:
            raise ValueError("minimum_security_count must be positive")
        if not 0 < shrink_ratio <= 1:
            raise ValueError("shrink_ratio must be between 0 and 1")
        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        if not callable(getattr(source, "fetch_symbols", None)):
            raise TypeError("source must provide fetch_symbols()")
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.source = source
        self.minimum_security_count = minimum_security_count
        self.shrink_ratio = shrink_ratio
        self.stale_after = stale_after
        self.clock = clock
        self._lock = RLock()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _migrate(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS catalog_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    security_count INTEGER NOT NULL,
                    checksum TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_securities (
                    version_id INTEGER NOT NULL REFERENCES catalog_versions(id) ON DELETE CASCADE,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    market_label TEXT,
                    name_normalized TEXT NOT NULL,
                    PRIMARY KEY(version_id, symbol, market)
                );
                CREATE INDEX IF NOT EXISTS catalog_securities_symbol
                    ON catalog_securities(version_id, symbol, market);
                CREATE INDEX IF NOT EXISTS catalog_securities_name
                    ON catalog_securities(version_id, name_normalized, symbol, market);
                CREATE TABLE IF NOT EXISTS catalog_active (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    version_id INTEGER NOT NULL REFERENCES catalog_versions(id),
                    activated_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _normalize(values: object) -> list[SymbolLookup]:
        if not isinstance(values, list):
            raise SymbolCatalogError("SYMBOL_CATALOG_SOURCE_INVALID")
        normalized: list[SymbolLookup] = []
        seen: dict[tuple[str, str], str] = {}
        for value in values:
            if not isinstance(value, SymbolLookup):
                raise SymbolCatalogError("SYMBOL_CATALOG_SOURCE_INVALID")
            try:
                expected_market = market_code_for_symbol(value.symbol)
            except UnsupportedMarketError as error:
                raise SymbolCatalogError("SYMBOL_CATALOG_SOURCE_INVALID") from error
            name = value.name.strip()
            identity = (value.symbol, value.market)
            if not name or value.market != expected_market:
                raise SymbolCatalogError("SYMBOL_CATALOG_SOURCE_INVALID")
            previous_name = seen.get(identity)
            if previous_name is not None:
                if previous_name != name:
                    raise SymbolCatalogError(
                        "SYMBOL_CATALOG_SOURCE_INVALID"
                    )
                continue
            seen[identity] = name
            normalized.append(
                SymbolLookup(
                    symbol=value.symbol,
                    name=name,
                    market=value.market,
                    market_label=value.market_label,
                    securities_code=value.securities_code,
                )
            )
        return sorted(normalized, key=lambda value: (value.symbol, value.market))

    @staticmethod
    def _checksum(values: list[SymbolLookup]) -> str:
        payload = "\n".join(
            "\t".join(
                (
                    value.symbol,
                    value.market,
                    value.name,
                    value.market_label or "",
                )
            )
            for value in values
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def refresh(self) -> CatalogRefresh:
        try:
            values = self.source.fetch_symbols()
        except SymbolCatalogError:
            raise
        except Exception as error:
            raise SymbolCatalogError("SYMBOL_CATALOG_SOURCE_HTTP_ERROR") from error
        candidates = self._normalize(values)
        checksum = self._checksum(candidates)
        now = self._now()
        with self._lock, self._connect() as connection:
            active = self._active_row(connection)
            minimum = self.minimum_security_count
            if active is not None:
                minimum = max(minimum, int(active["security_count"] * self.shrink_ratio + 0.999999))
            if len(candidates) < minimum:
                raise SymbolCatalogError(
                    "SYMBOL_CATALOG_TOO_SMALL" if active is None else "SYMBOL_CATALOG_SHRINK_REJECTED"
                )
            with connection:
                cursor = connection.execute(
                    """INSERT INTO catalog_versions(
                       source,fetched_at,security_count,checksum
                    ) VALUES(?,?,?,?)""",
                    (
                        type(self.source).__name__,
                        now.isoformat(),
                        len(candidates),
                        checksum,
                    ),
                )
                version = int(cursor.lastrowid)
                connection.executemany(
                    """INSERT INTO catalog_securities(
                       version_id,symbol,name,market,market_label,name_normalized
                    ) VALUES(?,?,?,?,?,?)""",
                    [
                        (
                            version,
                            value.symbol,
                            value.name,
                            value.market,
                            value.market_label,
                            value.name.casefold(),
                        )
                        for value in candidates
                    ],
                )
                connection.execute(
                    """INSERT INTO catalog_active(singleton,version_id,activated_at) VALUES(1,?,?)
                       ON CONFLICT(singleton) DO UPDATE SET
                         version_id=excluded.version_id, activated_at=excluded.activated_at""",
                    (version, now.isoformat()),
                )
                connection.execute(
                    """DELETE FROM catalog_versions
                       WHERE id NOT IN (
                         SELECT id FROM catalog_versions
                         ORDER BY id DESC LIMIT 3
                       )"""
                )
        return CatalogRefresh(
            version=version,
            count=len(candidates),
            checksum=checksum,
        )

    def lookup(self, symbol: str) -> SymbolLookup:
        expected_market = market_code_for_symbol(symbol)
        with self._lock, self._connect() as connection:
            active = self._require_active(connection)
            row = connection.execute(
                """SELECT symbol,name,market,market_label FROM catalog_securities
                   WHERE version_id=? AND symbol=? AND market=?""",
                (active["version_id"], symbol, expected_market),
            ).fetchone()
        if row is None:
            raise SymbolLookupNotFoundError(symbol)
        return SymbolLookup(
            symbol=str(row["symbol"]),
            name=str(row["name"]),
            market=str(row["market"]),
            market_label=_text(row["market_label"]),
        )

    def lookup_symbol(self, symbol: str) -> SymbolLookup:
        return self.lookup(symbol)

    def search(self, query: str, limit: int = 8) -> list[SymbolLookup]:
        normalized = str(query).strip().casefold()
        if not 2 <= len(normalized) <= 32:
            raise ValueError("query must contain 2 to 32 characters")
        if not 1 <= limit <= 8:
            raise ValueError("limit must be between 1 and 8")
        escaped = self._escape_like(normalized)
        contains = f"%{escaped}%"
        prefix = f"{escaped}%"
        with self._lock, self._connect() as connection:
            active = self._require_active(connection)
            rows = connection.execute(
                """SELECT symbol,name,market,market_label FROM catalog_securities
                   WHERE version_id=?
                     AND (symbol LIKE ? ESCAPE '\\' OR name_normalized LIKE ? ESCAPE '\\')
                   ORDER BY CASE
                     WHEN symbol=? THEN 0
                     WHEN symbol LIKE ? ESCAPE '\\' THEN 1
                     WHEN name_normalized=? THEN 2
                     WHEN name_normalized LIKE ? ESCAPE '\\' THEN 3
                     ELSE 4 END,
                     symbol,market
                   LIMIT ?""",
                (active["version_id"], contains, contains, normalized, prefix, normalized, prefix, limit),
            ).fetchall()
        return [
            SymbolLookup(
                symbol=str(row["symbol"]),
                name=str(row["name"]),
                market=str(row["market"]),
                market_label=_text(row["market_label"]),
            )
            for row in rows
        ]

    def search_symbols(
        self,
        query: str,
        limit: int = 8,
    ) -> list[SymbolLookup]:
        return self.search(query, limit)

    def status(self) -> CatalogStatus:
        with self._lock, self._connect() as connection:
            active = self._active_row(connection)
        if active is None:
            return CatalogStatus(
                active_version=None,
                count=0,
                activated_at=None,
                stale=True,
                checksum=None,
            )
        activated_at = datetime.fromisoformat(str(active["activated_at"]))
        return CatalogStatus(
            active_version=int(active["version_id"]),
            count=int(active["security_count"]),
            activated_at=activated_at,
            stale=self._now() - activated_at > self.stale_after,
            checksum=str(active["checksum"]),
        )

    def startup_refresh_required(
        self,
        max_age: timedelta = timedelta(hours=18),
    ) -> bool:
        if max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        status = self.status()
        return (
            status.activated_at is None
            or self._now() - status.activated_at > max_age
        )

    def _require_active(self, connection: sqlite3.Connection) -> sqlite3.Row:
        active = self._active_row(connection)
        if active is None:
            raise SymbolCatalogError("SYMBOL_CATALOG_UNAVAILABLE")
        activated_at = datetime.fromisoformat(str(active["activated_at"]))
        if self._now() - activated_at > self.stale_after:
            raise SymbolCatalogError("SYMBOL_CATALOG_STALE")
        return active

    @staticmethod
    def _active_row(connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT a.version_id,a.activated_at,v.security_count,v.checksum
               FROM catalog_active a JOIN catalog_versions v ON v.id=a.version_id
               WHERE a.singleton=1"""
        ).fetchone()

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
