"""Synchronize Market watchlist changes into the matching Vibe workspace ledger."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from .parsed_values import SymbolLookup


class VibeWatchlistSyncError(RuntimeError):
    """The Vibe mirror could not be updated; the caller can safely retry."""


def _backend_url(path: str) -> str:
    base = os.environ.get("VRA_BACKEND_URL", "http://host.docker.internal:8765").rstrip("/")
    return f"{base}/{quote(path.strip('/'), safe='/-._~')}"


def _request(user_id: int, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    token = os.environ.get("VRA_API_TOKEN", "")
    if len(token) < 32:
        raise VibeWatchlistSyncError("Vibe 同步凭据尚未配置")
    headers = {
        "Authorization": f"Bearer {token}",
        "Origin": "http://127.0.0.1",
        "X-THS-Market-User": str(user_id),
        "Accept": "application/json",
    }
    try:
        response = httpx.request(
            method,
            _backend_url(path),
            headers=headers,
            json=body,
            timeout=httpx.Timeout(8.0, connect=3.0),
            trust_env=False,
        )
    except httpx.RequestError:
        raise VibeWatchlistSyncError("Vibe 主机服务暂时不可用") from None
    if response.is_error:
        raise VibeWatchlistSyncError("Vibe 自选同步失败，请重试")
    if response.status_code == 204:
        return None
    try:
        return response.json()
    except ValueError:
        raise VibeWatchlistSyncError("Vibe 自选同步返回了无效响应") from None


def _records(user_id: int) -> list[dict[str, Any]]:
    payload = _request(user_id, "GET", "/ledger/watch")
    records = payload.get("watch") if isinstance(payload, dict) else None
    return [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []


def _save(user_id: int, symbol: SymbolLookup, records: list[dict[str, Any]]) -> None:
    matches = [record for record in records if record.get("symbol") == symbol.symbol]
    if matches:
        current = matches[0]
        if current.get("name") == symbol.name and len(matches) == 1:
            return
        record: dict[str, Any] = {"symbol": symbol.symbol, "name": symbol.name}
        if isinstance(current.get("id"), str) and current["id"]:
            record["id"] = current["id"]
        for duplicate in matches[1:]:
            record_id = duplicate.get("id")
            if isinstance(record_id, str) and record_id:
                _request(user_id, "POST", "/ledger/watch/delete", {"id": record_id})
        _request(user_id, "POST", "/ledger/watch", {"record": record})
        current["name"] = symbol.name
        for duplicate in matches[1:]:
            records.remove(duplicate)
        return
    _request(user_id, "POST", "/ledger/watch", {"record": {"symbol": symbol.symbol, "name": symbol.name}})
    records.append({"symbol": symbol.symbol, "name": symbol.name})


def add_symbol_to_vibe(user_id: int, symbol: SymbolLookup) -> None:
    _save(user_id, symbol, _records(user_id))


def remove_symbol_from_vibe(user_id: int, symbol: str) -> None:
    for record in _records(user_id):
        if record.get("symbol") != symbol:
            continue
        record_id = record.get("id")
        if isinstance(record_id, str) and record_id:
            _request(user_id, "POST", "/ledger/watch/delete", {"id": record_id})


def read_vibe_watchlist(user_id: int) -> list[dict[str, Any]]:
    return _records(user_id)


def reconcile_vibe_watchlist(
    user_id: int,
    market_symbols: list[SymbolLookup],
    resolve_symbol: Callable[[str], SymbolLookup],
    *,
    remove_stale: bool = True,
) -> None:
    """On first integration, merge existing symbols once; later calls mirror Market state."""
    records = _records(user_id)
    market_by_symbol = {item.symbol: item for item in market_symbols}

    # Mirror every current Market symbol into the Vibe ledger used by background jobs.
    for item in market_by_symbol.values():
        _save(user_id, item, records)

    # Remove stale mirrored A-share rows after a Market-side deletion. Vibe-only
    # unsupported six-digit entries remain local, as do Hong Kong and US symbols.
    if not remove_stale:
        return
    seen: set[str] = set()
    for record in records:
        symbol = record.get("symbol")
        if not isinstance(symbol, str) or not re.fullmatch(r"\d{6}", symbol) or symbol in seen:
            continue
        seen.add(symbol)
        try:
            current = resolve_symbol(symbol)
        except HTTPException as error:
            if error.status_code in {404, 409, 422}:
                continue
            raise VibeWatchlistSyncError("证券目录暂时不可用，自选同步稍后重试") from None
        if current.symbol not in market_by_symbol:
            remove_symbol_from_vibe(user_id, current.symbol)
