"""Internal bridge endpoints for Guohang Goodluck dark-pool & watchlist integration."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from typing import Any, Callable
from urllib.parse import unquote

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .market_accounts import MarketUser
from .market_data import fixed_market_error_code
from .parsed_values import DirectRequestError, SymbolLookup


_SYMBOL_RE = re.compile(r"^[0-9]{6}\Z")
_DATE_RE = re.compile(r"^[0-9]{8}\Z")


class BridgeGroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=24)


class BridgeGroupOrderRequest(BaseModel):
    group_ids: list[int]


class BridgeSymbolRequest(BaseModel):
    symbol: str


class BridgeSymbolOrderRequest(BaseModel):
    symbols: list[str]


class BridgeMoveSymbolRequest(BaseModel):
    source_group_id: int
    target_group_id: int
    symbol: str
    target_index: int = Field(ge=0)


class BridgeMonitoringUpdateRequest(BaseModel):
    enabled: bool


def _token_matches(provided: str | None, expected: str | None) -> bool:
    if not provided or not expected:
        return False
    left = hashlib.sha256(provided.encode("utf-8")).digest()
    right = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(left, right)


def _validate_symbol(symbol: str) -> str:
    normalized = symbol.strip()
    if not _SYMBOL_RE.fullmatch(normalized):
        raise HTTPException(status_code=422, detail="symbol must be a six-digit stock code")
    return normalized


def _extract_today_fund_flow(store: object, symbol: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if store is None:
        return None, None
    snapshot = store.latest_fund_flow_snapshot(symbol)
    if snapshot is not None:
        return snapshot, (snapshot.get("periods") or {}).get("today")
    try:
        history = store.read_fund_flow_history(symbol)
    except LookupError:
        return None, None
    points = ((history.get("periods") or {}).get("today") or {}).get("points") or []
    if not points:
        return None, None
    latest = points[-1]
    return {
        "trade_date": history.get("trade_date"),
        "minute": latest.get("time"),
    }, latest


def install_goodluck_routes(
    app: FastAPI,
    *,
    resolve_symbol: Callable[[str], SymbolLookup],
    previous_trading_date: Callable[[str], str] | None = None,
) -> None:
    """Install token-authenticated internal bridge routes for Guohang Goodluck."""

    prefix = "/api/internal/goodluck"

    def account_store():
        accounts = getattr(app.state, "market_account_store", None)
        if accounts is None:
            raise HTTPException(status_code=503, detail="market application is not configured")
        return accounts

    def require_goodluck_identity(request: Request) -> tuple[MarketUser, bool]:
        expected_token = getattr(app.state, "goodluck_internal_token", None)
        if not isinstance(expected_token, str) or not expected_token.strip():
            raise HTTPException(status_code=503, detail="goodluck bridge is not configured")
        provided_token = request.headers.get("X-Goodluck-Internal-Token")
        if not _token_matches(provided_token, expected_token):
            raise HTTPException(status_code=401, detail="invalid internal token")
        raw_user = request.headers.get("X-Goodluck-User") or ""
        user_key = unquote(raw_user).strip()
        if not user_key or len(user_key) > 128:
            raise HTTPException(status_code=400, detail="missing goodluck user header")
        is_admin = (request.headers.get("X-Goodluck-Role") or "").strip().lower() == "admin"
        accounts = account_store()
        try:
            user = accounts.get_or_create_external_user("goodluck", user_key)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        if not user.enabled:
            raise HTTPException(status_code=403, detail="market user is disabled")
        return user, is_admin

    def _user_watchlist_items(accounts: Any, user_id: int) -> dict[str, Any]:
        items: dict[str, Any] = {}
        for group in accounts.list_watchlists(user_id):
            for item in group.items:
                items.setdefault(item.symbol, item)
        return items

    def _resolve_item_name(symbol: str, fallback: str) -> str:
        try:
            return resolve_symbol(symbol).name
        except Exception:
            return fallback

    def _schedule_warmup(symbol: str) -> None:
        broker = getattr(app.state, "market_data_broker", None)
        store = getattr(app.state, "research_store", None)
        if broker is None:
            return
        tasks: set[asyncio.Task[Any]] = getattr(app.state, "goodluck_warmup_tasks", None)
        if tasks is None:
            tasks = set()
            app.state.goodluck_warmup_tasks = tasks

        async def _run() -> None:
            try:
                snapshot = await broker.refresh(symbol, detail=True, max_age_seconds=5)
                if store is not None:
                    await asyncio.to_thread(store.record_snapshot, snapshot)
            except Exception as error:
                if store is not None:
                    code = fixed_market_error_code(error, "MARKET_QUOTE_UNAVAILABLE")
                    try:
                        await asyncio.to_thread(store.record_error, symbol, code)
                    except Exception:
                        pass

        task = asyncio.create_task(_run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @app.get(f"{prefix}/symbols")
    def bridge_search_symbols(
        query: str = Query(min_length=2, max_length=32),
        limit: int = Query(default=10, ge=1, le=10),
        _identity=Depends(require_goodluck_identity),
    ) -> list[dict[str, Any]]:
        normalized = query.strip()
        if not 2 <= len(normalized) <= 32:
            raise HTTPException(status_code=422, detail="query must contain 2 to 32 characters")
        search = getattr(app.state, "symbol_search", None)
        if search is None:
            raise HTTPException(status_code=503, detail="symbol search temporarily unavailable")
        try:
            return [
                {
                    "symbol": item.symbol,
                    "name": item.name,
                    "market": item.market,
                    "market_label": item.market_label,
                }
                for item in search(normalized, limit)
            ]
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except DirectRequestError as error:
            detail = (
                error.error_code
                if error.error_code.startswith("SYMBOL_CATALOG_")
                else "symbol search temporarily unavailable"
            )
            raise HTTPException(status_code=503, detail=detail) from None
        except Exception:
            raise HTTPException(status_code=503, detail="symbol search temporarily unavailable") from None

    @app.get(f"{prefix}/symbols/{{symbol}}")
    def bridge_lookup_symbol(
        symbol: str,
        _identity=Depends(require_goodluck_identity),
    ) -> dict[str, str]:
        normalized = _validate_symbol(symbol)
        result = resolve_symbol(normalized)
        return {"symbol": result.symbol, "name": result.name, "market": result.market}

    @app.get(f"{prefix}/watchlists")
    def bridge_get_watchlists(identity=Depends(require_goodluck_identity)) -> dict[str, Any]:
        user, _is_admin = identity
        accounts = account_store()
        groups = accounts.list_watchlists(user.id)
        states = accounts.monitoring_state(user.id)
        store = getattr(app.state, "research_store", None)
        statuses = {
            row["symbol"]: row
            for row in (store.monitoring_status(states) if store is not None else [])
        }
        serialized_groups = []
        for group in groups:
            items = []
            for item in group.items:
                status_row = statuses.get(item.symbol, {})
                items.append(
                    {
                        "symbol": item.symbol,
                        "name": _resolve_item_name(item.symbol, item.name),
                        "market": item.market,
                        "monitoring_enabled": bool(states.get(item.symbol, True)),
                        "latest_trade_date": status_row.get("latest_trade_date"),
                        "latest_time": status_row.get("latest_time"),
                        "last_sync_at": status_row.get("last_sync_at"),
                        "last_error": status_row.get("last_error"),
                    }
                )
            serialized_groups.append(
                {
                    "id": group.id,
                    "name": group.name,
                    "sort_order": group.sort_order,
                    "is_primary": group.is_primary,
                    "items": items,
                }
            )
        return {
            "groups": serialized_groups,
            "limits": {
                "user_count": accounts.user_symbol_count(user.id),
                "user_max": accounts.max_symbols_per_user,
                "global_count": accounts.total_symbol_count(),
                "global_max": accounts.max_total_symbols,
            },
        }

    @app.post(f"{prefix}/watchlists/groups", status_code=201)
    def bridge_create_group(
        payload: BridgeGroupRequest,
        identity=Depends(require_goodluck_identity),
    ) -> dict[str, Any]:
        user, _is_admin = identity
        accounts = account_store()
        try:
            group = accounts.create_group(user.id, payload.name)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return {
            "id": group.id,
            "name": group.name,
            "sort_order": group.sort_order,
            "is_primary": group.is_primary,
            "items": [],
        }

    @app.patch(f"{prefix}/watchlists/groups/{{group_id}}")
    def bridge_rename_group(
        group_id: int,
        payload: BridgeGroupRequest,
        identity=Depends(require_goodluck_identity),
    ) -> dict[str, Any]:
        user, _is_admin = identity
        accounts = account_store()
        try:
            group = accounts.rename_group(user.id, group_id, payload.name)
        except LookupError:
            raise HTTPException(status_code=404, detail="watchlist group not found") from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        return {
            "id": group.id,
            "name": group.name,
            "sort_order": group.sort_order,
            "is_primary": group.is_primary,
            "items": [
                {
                    "symbol": item.symbol,
                    "name": _resolve_item_name(item.symbol, item.name),
                    "market": item.market,
                }
                for item in group.items
            ],
        }

    @app.delete(f"{prefix}/watchlists/groups/{{group_id}}", status_code=204)
    def bridge_delete_group(
        group_id: int,
        identity=Depends(require_goodluck_identity),
    ) -> None:
        user, _is_admin = identity
        accounts = account_store()
        try:
            accounts.delete_group(user.id, group_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="watchlist group not found") from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.put(f"{prefix}/watchlists/groups/order", status_code=204)
    def bridge_reorder_groups(
        payload: BridgeGroupOrderRequest,
        identity=Depends(require_goodluck_identity),
    ) -> None:
        user, _is_admin = identity
        accounts = account_store()
        try:
            accounts.reorder_groups(user.id, payload.group_ids)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.post(f"{prefix}/watchlists/groups/{{group_id}}/symbols", status_code=201)
    async def bridge_add_symbol(
        group_id: int,
        payload: BridgeSymbolRequest,
        identity=Depends(require_goodluck_identity),
    ) -> dict[str, Any]:
        user, _is_admin = identity
        normalized = _validate_symbol(payload.symbol)
        lookup = resolve_symbol(normalized)
        accounts = account_store()
        try:
            item = accounts.add_symbol(user.id, group_id, lookup)
        except LookupError:
            raise HTTPException(status_code=404, detail="watchlist group not found") from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        _schedule_warmup(lookup.symbol)
        return {
            "symbol": item.symbol,
            "name": item.name,
            "market": item.market,
            "monitoring_enabled": True,
        }

    @app.delete(f"{prefix}/watchlists/groups/{{group_id}}/symbols/{{symbol}}", status_code=204)
    def bridge_remove_group_symbol(
        group_id: int,
        symbol: str,
        identity=Depends(require_goodluck_identity),
    ) -> None:
        user, _is_admin = identity
        normalized = _validate_symbol(symbol)
        accounts = account_store()
        target_group = next(
            (group for group in accounts.list_watchlists(user.id) if group.id == group_id),
            None,
        )
        if target_group is None:
            raise HTTPException(status_code=404, detail="watchlist group not found")
        try:
            if target_group.is_primary:
                accounts.remove_symbol_everywhere(user.id, normalized)
            else:
                accounts.remove_symbol(user.id, group_id, normalized)
        except LookupError:
            raise HTTPException(status_code=404, detail="watchlist symbol not found") from None

    @app.delete(f"{prefix}/watchlists/symbols/{{symbol}}", status_code=204)
    def bridge_remove_symbol_everywhere(
        symbol: str,
        identity=Depends(require_goodluck_identity),
    ) -> None:
        user, _is_admin = identity
        normalized = _validate_symbol(symbol)
        accounts = account_store()
        try:
            accounts.remove_symbol_everywhere(user.id, normalized)
        except LookupError:
            pass

    @app.put(f"{prefix}/watchlists/groups/{{group_id}}/symbols/order", status_code=204)
    def bridge_reorder_symbols(
        group_id: int,
        payload: BridgeSymbolOrderRequest,
        identity=Depends(require_goodluck_identity),
    ) -> None:
        user, _is_admin = identity
        accounts = account_store()
        try:
            accounts.reorder_symbols(user.id, group_id, payload.symbols)
        except LookupError:
            raise HTTPException(status_code=404, detail="watchlist group not found") from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.post(f"{prefix}/watchlists/symbols/move", status_code=204)
    def bridge_move_symbol(
        payload: BridgeMoveSymbolRequest,
        identity=Depends(require_goodluck_identity),
    ) -> None:
        user, _is_admin = identity
        accounts = account_store()
        try:
            accounts.move_symbol(
                user.id,
                payload.source_group_id,
                payload.target_group_id,
                _validate_symbol(payload.symbol),
                payload.target_index,
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="watchlist symbol or group not found") from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.patch(f"{prefix}/monitoring/symbols/{{symbol}}")
    def bridge_set_monitoring(
        symbol: str,
        payload: BridgeMonitoringUpdateRequest,
        identity=Depends(require_goodluck_identity),
    ) -> dict[str, Any]:
        user, _is_admin = identity
        normalized = _validate_symbol(symbol)
        accounts = account_store()
        try:
            enabled = accounts.set_monitoring_enabled(user.id, normalized, payload.enabled)
        except LookupError:
            raise HTTPException(status_code=404, detail="watchlist symbol not found") from None
        store = getattr(app.state, "research_store", None)
        status_row = store.monitoring_status([normalized])[0] if store is not None else {}
        return {
            "symbol": normalized,
            "enabled": enabled,
            "latest_trade_date": status_row.get("latest_trade_date"),
            "latest_time": status_row.get("latest_time"),
            "last_sync_at": status_row.get("last_sync_at"),
            "last_error": status_row.get("last_error"),
        }

    @app.post(f"{prefix}/monitoring/symbols/{{symbol}}/backfill")
    async def bridge_backfill_symbol(
        symbol: str,
        identity=Depends(require_goodluck_identity),
    ) -> dict[str, Any]:
        user, _is_admin = identity
        normalized = _validate_symbol(symbol)
        accounts = account_store()
        if normalized not in accounts.monitoring_state(user.id):
            raise HTTPException(status_code=404, detail="watchlist symbol not found")
        broker = getattr(app.state, "market_data_broker", None)
        store = getattr(app.state, "research_store", None)
        if broker is None or store is None:
            raise HTTPException(status_code=503, detail="research monitoring is not configured")
        try:
            snapshot = await broker.refresh(normalized, detail=True, max_age_seconds=0)
            changed = await asyncio.to_thread(store.record_snapshot, snapshot)
        except HTTPException:
            raise
        except Exception as error:
            code = fixed_market_error_code(error, "MARKET_QUOTE_UNAVAILABLE")
            await asyncio.to_thread(store.record_error, normalized, code)
            raise HTTPException(status_code=503, detail=code) from None
        return {"symbol": normalized, "changed_minutes": changed}

    @app.get(f"{prefix}/quotes")
    async def bridge_quotes(
        symbols: str | None = None,
        identity=Depends(require_goodluck_identity),
    ) -> dict[str, Any]:
        user, _is_admin = identity
        accounts = account_store()
        user_items = _user_watchlist_items(accounts, user.id)
        if symbols is not None:
            requested = []
            seen: set[str] = set()
            for raw in symbols.split(","):
                code = raw.strip()
                if code in user_items and code not in seen:
                    seen.add(code)
                    requested.append(code)
                if len(requested) >= 50:
                    break
            target_symbols = requested
        else:
            target_symbols = list(user_items.keys())[:50]

        broker = getattr(app.state, "market_data_broker", None)
        store = getattr(app.state, "research_store", None)
        semaphore = asyncio.Semaphore(6)

        async def fetch_one(code: str) -> tuple[str, dict[str, Any]]:
            item = user_items[code]
            snapshot = None
            async with semaphore:
                if broker is not None:
                    cached = broker.cached_snapshot(code)
                    if cached is not None and not broker.is_market_open():
                        snapshot = cached
                    else:
                        try:
                            snapshot = await broker.refresh(code, detail=False, max_age_seconds=10.0)
                        except Exception:
                            snapshot = cached
                if snapshot is None and store is not None:
                    snapshot = await asyncio.to_thread(store.latest_market_snapshot, code)
            ff_meta, today_ff = await asyncio.to_thread(_extract_today_fund_flow, store, code)
            if snapshot is not None:
                public = snapshot.as_public()
                quote = public.get("quote") or {}
                market_phase = public.get("market_phase")
                phase_value = market_phase.value if hasattr(market_phase, "value") else market_phase
                summary = {
                    "symbol": code,
                    "name": public.get("name") or _resolve_item_name(code, item.name),
                    "market": public.get("market") or item.market,
                    "price": quote.get("price"),
                    "change_percent": quote.get("change_percent"),
                    "turnover_rate": quote.get("turnover_rate"),
                    "market_phase": phase_value or "CLOSED",
                    "source": public.get("source"),
                    "source_time": public.get("source_time"),
                    "stale": bool(public.get("stale", False)),
                }
            else:
                summary = {
                    "symbol": code,
                    "name": _resolve_item_name(code, item.name),
                    "market": item.market,
                    "price": None,
                    "change_percent": None,
                    "turnover_rate": None,
                    "market_phase": "CLOSED",
                    "source": None,
                    "source_time": None,
                    "stale": True,
                }
            summary.update(
                {
                    "today_main_net_inflow": today_ff.get("main_net_inflow") if today_ff else None,
                    "today_main_hidden_inflow": today_ff.get("main_hidden_inflow") if today_ff else None,
                    "today_unit": today_ff.get("unit") if today_ff else None,
                    "fund_flow_trade_date": ff_meta.get("trade_date") if ff_meta else None,
                    "fund_flow_time": ff_meta.get("minute") if ff_meta else None,
                }
            )
            return code, summary

        pairs = await asyncio.gather(*(fetch_one(code) for code in target_symbols))
        return {"items": dict(pairs)}

    @app.get(f"{prefix}/symbols/{{symbol}}/snapshot")
    async def bridge_symbol_snapshot(
        symbol: str,
        identity=Depends(require_goodluck_identity),
    ) -> dict[str, Any]:
        user, _is_admin = identity
        normalized = _validate_symbol(symbol)
        accounts = account_store()
        watchlist_item = _user_watchlist_items(accounts, user.id).get(normalized)
        if watchlist_item is None:
            raise HTTPException(status_code=404, detail="watchlist symbol not found")
        broker = getattr(app.state, "market_data_broker", None)
        if broker is None:
            raise HTTPException(status_code=503, detail="market data is not configured")
        try:
            snapshot = await broker.refresh(normalized, detail=True, max_age_seconds=1.5)
        except Exception as error:
            raise HTTPException(
                status_code=503,
                detail=fixed_market_error_code(error, "MARKET_QUOTE_UNAVAILABLE"),
            ) from None
        public = snapshot.as_public()
        quote = public.get("quote") or {}
        stock_name = public.get("name") or _resolve_item_name(normalized, watchlist_item.name)
        market_phase = public.get("market_phase")
        phase_value = market_phase.value if hasattr(market_phase, "value") else market_phase
        return {
            "symbol": normalized,
            "name": stock_name,
            "market": public.get("market") or watchlist_item.market,
            "source": public.get("source"),
            "source_time": public.get("source_time"),
            "collected_at": public.get("collected_at"),
            "market_phase": phase_value or "CLOSED",
            "price_precision": public.get("price_precision", 2),
            "stale": bool(public.get("stale", False)),
            "metrics": {
                "stock_name": stock_name,
                "current_price": quote.get("price"),
                "change_percent": quote.get("change_percent"),
                "turnover_rate": quote.get("turnover_rate"),
                "large_order_net": quote.get("large_order_net"),
                "large_order_amount": quote.get("large_order_amount"),
                "retail_count": quote.get("retail_count"),
                "macdfs": quote.get("macdfs"),
            },
            "main_fund_flow": public.get("main_fund_flow") or {},
            "capabilities": {
                "l2": (public.get("capabilities") or {}).get("l2")
                or {"available": False, "reason": None},
            },
            "source_errors": public.get("source_errors") or {},
            "stored_trade_dates": public.get("stored_trade_dates") or {},
        }

    @app.get(f"{prefix}/symbols/{{symbol}}/fund-flow/history")
    def bridge_fund_flow_history(
        symbol: str,
        trade_date: str | None = None,
        identity=Depends(require_goodluck_identity),
    ) -> dict[str, Any]:
        user, _is_admin = identity
        normalized = _validate_symbol(symbol)
        if trade_date is not None and not _DATE_RE.fullmatch(trade_date.strip()):
            raise HTTPException(status_code=422, detail="trade_date must be YYYYMMDD")
        accounts = account_store()
        watchlist_item = _user_watchlist_items(accounts, user.id).get(normalized)
        if watchlist_item is None:
            raise HTTPException(status_code=404, detail="watchlist symbol not found")
        store = getattr(app.state, "research_store", None)
        if store is None:
            raise HTTPException(status_code=503, detail="research monitoring is not configured")
        try:
            history = store.read_fund_flow_history(
                normalized,
                trade_date.strip() if trade_date else None,
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="fund flow history not found") from None
        return {**history, "name": _resolve_item_name(normalized, watchlist_item.name)}

    @app.get(f"{prefix}/symbols/{{symbol}}/fund-flow/daily")
    def bridge_fund_flow_daily(
        symbol: str,
        limit: int = Query(default=30, ge=1, le=60),
        identity=Depends(require_goodluck_identity),
    ) -> dict[str, Any]:
        user, _is_admin = identity
        normalized = _validate_symbol(symbol)
        accounts = account_store()
        watchlist_item = _user_watchlist_items(accounts, user.id).get(normalized)
        if watchlist_item is None:
            raise HTTPException(status_code=404, detail="watchlist symbol not found")
        store = getattr(app.state, "research_store", None)
        if store is None:
            raise HTTPException(status_code=503, detail="research monitoring is not configured")
        return {
            **store.read_fund_flow_daily(normalized, limit, previous_trading_date),
            "name": _resolve_item_name(normalized, watchlist_item.name),
        }

    @app.get(f"{prefix}/admin/status")
    def bridge_admin_status(identity=Depends(require_goodluck_identity)) -> dict[str, Any]:
        _user, is_admin = identity
        if not is_admin:
            raise HTTPException(status_code=403, detail="admin access required")
        accounts = account_store()
        broker = getattr(app.state, "market_data_broker", None)
        store = getattr(app.state, "research_store", None)
        catalog = getattr(app.state, "symbol_catalog", None)
        monitored_symbols = accounts.list_monitored_symbols()
        catalog_info = None
        if catalog is not None and callable(getattr(catalog, "status", None)):
            current = catalog.status()
            catalog_info = {
                "active_version": current.active_version,
                "count": current.count,
                "activated_at": current.activated_at.isoformat() if current.activated_at is not None else None,
                "stale": current.stale,
                "checksum": current.checksum,
            }
        return {
            "global_count": accounts.total_symbol_count(),
            "global_max": accounts.max_total_symbols,
            "monitored_count": len(monitored_symbols),
            "market_open": bool(broker.is_market_open()) if broker is not None else False,
            "symbol_catalog": catalog_info,
            "monitoring": store.monitoring_status(monitored_symbols) if store is not None else [],
        }
