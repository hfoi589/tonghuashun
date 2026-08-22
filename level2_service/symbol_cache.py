"""Persistent cache for stock codes already verified through the THS App."""

from __future__ import annotations

import json
from threading import RLock
from typing import Protocol

from .parsed_values import SymbolLookup, UnsupportedMarketError, market_code_for_symbol


class SymbolLookupCache(Protocol):
    def get(self, symbol: str) -> SymbolLookup | None: ...

    def set(self, value: SymbolLookup) -> None: ...


class InMemorySymbolLookupCache:
    """Deterministic cache for local app factories and tests."""

    def __init__(self) -> None:
        self._values: dict[str, SymbolLookup] = {}
        self._lock = RLock()

    def get(self, symbol: str) -> SymbolLookup | None:
        with self._lock:
            return self._values.get(symbol)

    def set(self, value: SymbolLookup) -> None:
        with self._lock:
            self._values[value.symbol] = value


class RedisSymbolLookupCache:
    """Store verified stock metadata in Redis without an expiry."""

    def __init__(self, client: object, *, key_prefix: str = "ths:symbols:") -> None:
        for method in ("get", "set"):
            if not callable(getattr(client, method, None)):
                raise TypeError(f"RedisSymbolLookupCache requires redis client method: {method}")
        self.client = client
        self.key_prefix = key_prefix

    def get(self, symbol: str) -> SymbolLookup | None:
        raw = self.client.get(f"{self.key_prefix}{symbol}")
        if raw is None:
            return None
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            payload = json.loads(raw)
            value = SymbolLookup(
                symbol=str(payload["symbol"]),
                name=str(payload["name"]),
                market=str(payload["market"]),
                market_label=_optional_text(payload.get("market_label")),
                securities_code=_optional_text(payload.get("securities_code")),
            )
            if value.symbol != symbol or not value.name.strip():
                return None
            if market_code_for_symbol(value.symbol) != value.market:
                return None
            return value
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnsupportedMarketError):
            return None

    def set(self, value: SymbolLookup) -> None:
        payload = json.dumps(
            {
                "symbol": value.symbol,
                "name": value.name,
                "market": value.market,
                "market_label": value.market_label,
                "securities_code": value.securities_code,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.client.set(f"{self.key_prefix}{value.symbol}", payload)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
