"""Internal bridge endpoints for Guohang Goodluck dark-pool, intraday & premium integration."""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import hmac
import inspect
import json
import re
from threading import RLock
import time
from typing import Any, AsyncIterator, Callable, Literal
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .brent_market import (
    BRENT_SYMBOL,
    backfill_brent_to_research_store,
    build_brent_premium_panel,
)
from .etf_fund_flow import ETF_FLOW_PERIODS, EtfFundFlowError, fetch_etf_flow_payload
from .fund_flow_history import extract_quote_trade_date, read_fund_flow_history_for_view
from .market_accounts import MarketUser
from .market_data import fixed_market_error_code
from .quant_rules import (
    backfill_intraday_history,
    compact_quant_rule_record,
    enrich_store_with_line_funds,
    evaluate_quant_strategy,
    export_quant_dataset,
    get_quant_rule_schema,
)
from .device_lifecycle import (
    DeviceLifecycleAction,
    DeviceLifecycleError,
    DeviceLifecycleState,
)
from .ths_trade import TradeExecutionError
from .parsed_values import (
    DirectRequestError,
    SymbolLookup,
    UnsupportedMarketError,
    is_exchange_fund_symbol,
    market_code_for_symbol,
)
from .research_monitor import (
    format_push_price_and_change,
    format_push_symbol,
    resolve_push_symbol_name,
)
from .research_premium import format_premium_body
from .research_replay import (
    build_replay_markers,
    calculate_replay_performance,
    evaluate_rule_detail,
)


_SYMBOL_RE = re.compile(r"^[0-9]{6}\Z")
_QUANT_SYMBOL_RE = re.compile(r"^(?:[0-9]{6}|BRN0Y)\Z")
_DATE_RE = re.compile(r"^[0-9]{8}\Z")
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


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


class BridgeMacdSettingsRequest(BaseModel):
    short: int = Field(ge=2, le=200)
    long: int = Field(ge=3, le=200)
    signal: int = Field(ge=1, le=100)
    marker_threshold: float = Field(default=0.005, ge=0, le=1)


class BridgeReplayRequest(BaseModel):
    symbol: str
    trade_date: str | None = None
    rule_ids: list[str] = Field(default_factory=list)
    rules: list[dict[str, Any]] | None = None


class BridgePushTestRequest(BaseModel):
    channel: str | None = None
    config: dict[str, Any] | None = None


class BridgePushRuleTestRequest(BaseModel):
    symbol: str
    trade_date: str | None = None
    rule: dict[str, Any]
    send_push: bool = False
    config: dict[str, Any] | None = None


class BridgeTradeOrderRequest(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    price: str | float
    quantity: int = Field(ge=100, le=1000000)
    dry_run: bool = False
    auto_cancel: bool = False
    idempotency_key: str | None = None


class BridgeTradePrestageRequest(BaseModel):
    symbol: str
    side: Literal["buy", "sell"] = "buy"
    price: str | float
    quantity: int = Field(default=100, ge=100, le=1000000)


class BridgeTradeCancelRequest(BaseModel):
    entrust_no: str | None = None
    symbol: str | None = None
    side: Literal["buy", "sell"] | None = None


class BridgeTradeDeviceActionRequest(BaseModel):
    action: Literal["start", "shutdown"]


class BridgeSymbolRuleBinding(BaseModel):
    signal_source: Literal["intraday_macd_flow", "lof_premium", "combo_confirm", "quant_rule"] = "quant_rule"
    quant_rule_id: str | None = None
    enabled: bool = True
    priority: int = Field(default=10, ge=1, le=100)
    weight_scale: float = Field(default=1.0, ge=0.1, le=2.0)


class BridgeSymbolQuantProfileRequest(BaseModel):
    enabled: bool = True
    signal_source: Literal["intraday_macd_flow", "lof_premium", "combo_confirm", "quant_rule"] | None = None
    quant_rule_id: str | None = None
    sizing_mode: Literal["model_auto", "fixed_qty"] | None = None
    capital_cap: float | None = Field(default=None, ge=100.0, le=10000000.0)
    order_quantity: int | None = Field(default=None, ge=100, le=1000000)
    order_book_level: int | None = Field(default=None, ge=1, le=5)
    rule_bindings: list[BridgeSymbolRuleBinding] | None = Field(default=None, max_length=8)


class BridgeUpsertSymbolStrategyRequest(BridgeSymbolQuantProfileRequest):
    reconcile_broker: bool = True


class BridgeAutoTradeConfigRequest(BaseModel):
    enabled: bool = True
    signal_source: Literal["intraday_macd_flow", "lof_premium", "combo_confirm", "quant_rule"] = "intraday_macd_flow"
    quant_rule_id: str | None = None
    execution_mode: Literal["dry_run", "semi_auto", "full_auto"] = "semi_auto"
    sizing_mode: Literal["model_auto", "fixed_qty"] | None = None
    symbols: list[str] = Field(min_length=1, max_length=50)
    order_quantity: int = Field(ge=100, le=1000000)
    pricing_mode: Literal["counterparty_book", "fixed_ticks"] = "counterparty_book"
    order_book_level: int = Field(default=3, ge=1, le=5)
    slippage_ticks: int = Field(ge=0, le=20)
    max_slippage_pct: float = Field(default=1.5, ge=0.1, le=10.0)
    refresh_book_on_confirm: bool = True
    auto_cancel_seconds: int = Field(ge=0, le=600)
    daily_max_buy_amount: float = Field(ge=100.0, le=10000000.0)
    block_non_continuous_session: bool = True
    symbol_profiles: dict[str, BridgeSymbolQuantProfileRequest] | None = None
    reconcile_broker: bool = False


class BridgeQuantBackfillRequest(BaseModel):
    symbol: str = "160723"
    start_date: str = "20260701"
    end_date: str | None = None


class BridgeQuantEvaluateRequest(BaseModel):
    rule_id: str | None = None
    symbol: str = "160723"
    start_date: str | None = None
    end_date: str | None = None
    primary_horizon_minutes: int | None = Field(default=None, ge=1, le=30)
    min_move_pct: float | None = Field(default=None, ge=0.0, le=5.0)
    data_scope: Literal["auto", "all", "flow_only"] | None = None
    morning_start: str | None = None
    morning_end: str | None = None
    max_daily_trades: int | None = Field(default=None, ge=1, le=20)
    round_trip_fee_pct: float | None = Field(default=None, ge=0.0, le=1.0)
    capital_cap: float | None = Field(default=None, ge=1000.0, le=10000000.0)
    signal_limit: int = Field(default=200, ge=1, le=5000)
    macd_settings: dict[str, Any] | None = None
    buy_rule: dict[str, Any] | None = None
    sell_rule: dict[str, Any] | None = None
    position_sizing: dict[str, Any] | None = None
    eval_criteria: dict[str, Any] | None = None


class BridgeQuantRuleUpsertRequest(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=80)
    description: str = ""
    source: Literal["ai", "manual", "builtin"] = "ai"
    symbols: list[str] = Field(default_factory=lambda: ["160723"])
    macd_settings: dict[str, Any] | None = None
    buy_rule: dict[str, Any] | None = None
    sell_rule: dict[str, Any] | None = None
    position_sizing: dict[str, Any] | None = None
    eval_criteria: dict[str, Any] | None = None
    enabled: bool = True


class BridgeAutoTradeSimulateRequest(BaseModel):
    symbol: str | None = None
    side: Literal["buy", "sell"] = "buy"
    viewer_open: bool | None = None


class BridgeAutoTradeDecideRequest(BaseModel):
    action: Literal["approve", "reject"]
    dry_run: bool = False
    override_price: str | float | None = None
    override_quantity: int | None = Field(default=None, ge=100, le=1000000)
    override_book_level: int | None = Field(default=None, ge=1, le=5)
    idempotency_key: str | None = None


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


def _validate_quant_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if not _QUANT_SYMBOL_RE.fullmatch(normalized):
        raise HTTPException(status_code=422, detail="symbol must be a six-digit code or BRN0Y")
    return normalized


def _validate_optional_date(trade_date: str | None) -> str | None:
    if trade_date is None:
        return None
    cleaned = trade_date.strip().replace("-", "")
    if not cleaned:
        return None
    if not _DATE_RE.fullmatch(cleaned):
        raise HTTPException(status_code=422, detail="trade_date must be YYYYMMDD")
    return cleaned


def _supports_kwarg(fn: object, name: str) -> bool:
    try:
        sig = inspect.signature(fn)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if name in sig.parameters:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


def _supports_force_kwarg(fn: object) -> bool:
    return _supports_kwarg(fn, "force")


def _extract_today_fund_flow(
    store: object,
    symbol: str,
    *,
    trade_date: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if store is None:
        return None, None
    snapshot = (
        store.latest_fund_flow_snapshot(symbol, trade_date=trade_date)
        if trade_date is not None
        else store.latest_fund_flow_snapshot(symbol)
    )
    if snapshot is not None:
        return snapshot, (snapshot.get("periods") or {}).get("today")
    try:
        history = (
            store.read_fund_flow_history(symbol, trade_date)
            if trade_date is not None
            else store.read_fund_flow_history(symbol)
        )
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


def _merge_push_config_for_test(current: dict[str, Any], incoming: dict[str, Any] | None) -> dict[str, Any]:
    if not incoming or not isinstance(incoming, dict):
        return current
    current_groups = {
        str(g.get("id")): g for g in (current.get("bark_groups") or []) if isinstance(g, dict) and g.get("id")
    }
    groups = []
    raw_groups = incoming.get("bark_groups", current.get("bark_groups", []))
    for idx, raw_group in enumerate(raw_groups if isinstance(raw_groups, list) else []):
        if not isinstance(raw_group, dict):
            continue
        gid = str(raw_group.get("id") or f"bark-{idx + 1}")
        prev = current_groups.get(gid, {})
        secret = str(raw_group.get("device_key") or prev.get("device_key") or "")
        groups.append(
            {
                "id": gid,
                "name": str(raw_group.get("name") or gid),
                "base_url": str(raw_group.get("base_url") or prev.get("base_url") or "https://api.day.app").rstrip("/"),
                "device_key": secret,
            }
        )
    cur_sc3 = current.get("sc3_bot") or {}
    in_sc3 = incoming.get("sc3_bot") if isinstance(incoming.get("sc3_bot"), dict) else {}
    merged_sc3 = {**cur_sc3, **in_sc3}
    if not str(in_sc3.get("token") or "").strip():
        merged_sc3["token"] = cur_sc3.get("token", "")

    cur_wecom = current.get("wecom") or {}
    in_wecom = incoming.get("wecom") if isinstance(incoming.get("wecom"), dict) else {}
    merged_wecom = {**cur_wecom, **in_wecom}
    if not str(in_wecom.get("corp_secret") or "").strip():
        merged_wecom["corp_secret"] = cur_wecom.get("corp_secret", "")

    return {
        **current,
        "enabled": bool(incoming.get("enabled", current.get("enabled", False))),
        "premium_push_enabled": bool(incoming.get("premium_push_enabled", current.get("premium_push_enabled", True))),
        "bark_groups": groups,
        "sc3_bot": merged_sc3,
        "wecom": merged_wecom,
        "rules": incoming.get("rules", current.get("rules", [])),
    }


def _build_macd_cross_dots(points: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    dots: list[dict[str, Any]] = []
    for idx in range(1, len(points)):
        prev = points[idx - 1]
        curr = points[idx]
        prev_delta = float(prev.get("diff") or 0.0) - float(prev.get("dea") or 0.0)
        curr_delta = float(curr.get("diff") or 0.0) - float(curr.get("dea") or 0.0)
        macd_val = float(curr.get("macd") or 0.0)
        if abs(macd_val) < threshold:
            continue
        if prev_delta <= 0 < curr_delta:
            dots.append({"index": idx, "time": curr.get("time"), "kind": "red", "diff": curr.get("diff"), "macd": macd_val})
        elif prev_delta >= 0 > curr_delta:
            dots.append({"index": idx, "time": curr.get("time"), "kind": "green", "diff": curr.get("diff"), "macd": macd_val})
    return dots


def install_goodluck_routes(
    app: FastAPI,
    *,
    resolve_symbol: Callable[[str], SymbolLookup],
    previous_trading_date: Callable[[str], str] | None = None,
    expected_intraday_trade_date: Callable[[], str | None] | None = None,
    intraday_minute_cap: Callable[[str], str | None] | None = None,
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

    def require_goodluck_admin(identity: tuple[MarketUser, bool] = Depends(require_goodluck_identity)) -> MarketUser:
        user, is_admin = identity
        if not is_admin:
            raise HTTPException(status_code=403, detail="admin access required")
        return user

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

    def _resolved_market(symbol: str, stored_market: str) -> str:
        try:
            return market_code_for_symbol(symbol)
        except UnsupportedMarketError:
            return stored_market

    def _supports_darkpool(symbol: str, market: str | None = None) -> bool:
        return not is_exchange_fund_symbol(symbol, market)

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
                backfill = getattr(app.state, "post_close_backfill", None)
                snapshot = None
                if (
                    not broker.is_market_open()
                    and callable(backfill)
                    and _supports_darkpool(symbol)
                ):
                    snapshot = await asyncio.to_thread(backfill, symbol)
                    if snapshot is not None:
                        seed = getattr(broker, "seed", None)
                        if callable(seed):
                            seed(snapshot)
                if snapshot is None:
                    snapshot = await broker.refresh(symbol, detail=True, max_age_seconds=5)
                if store is not None and snapshot is not None:
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

    def _enrich_intraday_points(store: Any, symbol: str, trade_date: str, points: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ff_by_time: dict[str, dict[str, Any]] = {}
        try:
            ff_history = store.read_fund_flow_history(symbol, trade_date)
            today_pts = ((ff_history.get("periods") or {}).get("today") or {}).get("points") or []
            for item in today_pts:
                if isinstance(item, dict) and item.get("time"):
                    ff_by_time[str(item["time"])] = item
        except Exception:
            ff_by_time = {}

        enriched: list[dict[str, Any]] = []
        for point in points:
            ff = ff_by_time.get(str(point.get("time") or ""), {})
            enriched.append(
                {
                    **point,
                    "main_net_inflow": ff.get("main_net_inflow"),
                    "main_visible_inflow": ff.get("main_visible_inflow"),
                    "main_hidden_inflow": ff.get("main_hidden_inflow"),
                    "retail_inflow": ff.get("retail_inflow"),
                    "fund_unit": ff.get("unit"),
                }
            )
        return enriched

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
                    "supports_darkpool": _supports_darkpool(item.symbol, item.market),
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
    ) -> dict[str, Any]:
        normalized = _validate_symbol(symbol)
        result = resolve_symbol(normalized)
        return {
            "symbol": result.symbol,
            "name": result.name,
            "market": result.market,
            "supports_darkpool": _supports_darkpool(result.symbol, result.market),
        }

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
                market = _resolved_market(item.symbol, item.market)
                items.append(
                    {
                        "symbol": item.symbol,
                        "name": _resolve_item_name(item.symbol, item.name),
                        "market": market,
                        "supports_darkpool": _supports_darkpool(item.symbol, market),
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
                    "market": _resolved_market(item.symbol, item.market),
                    "supports_darkpool": _supports_darkpool(
                        item.symbol, _resolved_market(item.symbol, item.market)
                    ),
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
            "supports_darkpool": _supports_darkpool(item.symbol, item.market),
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
            evict = getattr(broker, "evict", None)
            if callable(evict):
                evict(normalized)
            backfill = getattr(app.state, "post_close_backfill", None)
            snapshot = None
            if (
                not broker.is_market_open()
                and callable(backfill)
                and _supports_darkpool(normalized)
            ):
                snapshot = await asyncio.to_thread(backfill, normalized)
                if snapshot is not None:
                    seed = getattr(broker, "seed", None)
                    if callable(seed):
                        seed(snapshot)
            if snapshot is None:
                if _supports_force_kwarg(broker.refresh):
                    snapshot = await broker.refresh(
                        normalized,
                        detail=True,
                        max_age_seconds=0,
                        force=True,
                    )
                else:
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
        quote_limit = max(
            100,
            int(getattr(accounts, "max_symbols_per_user", 50)),
            int(getattr(accounts, "max_total_symbols", 50)),
        )
        if symbols is not None:
            requested = []
            seen: set[str] = set()
            for raw in symbols.split(","):
                code = raw.strip()
                if code in user_items and code not in seen:
                    seen.add(code)
                    requested.append(code)
                if len(requested) >= quote_limit:
                    break
            target_symbols = requested
        else:
            target_symbols = list(user_items.keys())[:quote_limit]

        broker = getattr(app.state, "market_data_broker", None)
        store = getattr(app.state, "research_store", None)
        expected_today = (
            expected_intraday_trade_date()
            if callable(expected_intraday_trade_date)
            else None
        )
        batch_quotes_fn = (
            getattr(broker, "refresh_quotes_batch", None)
            if broker is not None
            else None
        )
        batch_snapshots: dict[str, Any] = {}
        if callable(batch_quotes_fn) and target_symbols:
            try:
                batch_snapshots = await batch_quotes_fn(
                    target_symbols,
                    max_age_seconds=10.0,
                )
            except Exception:
                batch_snapshots = {}

        darkpool_codes = [
            code
            for code in target_symbols
            if _supports_darkpool(code, _resolved_market(code, user_items[code].market))
        ]
        quote_dates = {
            parsed_date
            for snapshot in batch_snapshots.values()
            if snapshot is not None
            and (parsed_date := extract_quote_trade_date(getattr(snapshot, "source_time", None))) is not None
        }
        ff_trade_date = (
            next(iter(quote_dates))
            if len(quote_dates) == 1
            else expected_today
        )
        batch_ff_reader = (
            getattr(store, "latest_today_fund_flow_map", None)
            if store is not None
            else None
        )
        ff_map: dict[str, dict[str, Any]] = {}
        if darkpool_codes and callable(batch_ff_reader):
            try:
                if ff_trade_date is not None and _supports_kwarg(batch_ff_reader, "trade_date"):
                    ff_map = await asyncio.to_thread(batch_ff_reader, darkpool_codes, trade_date=ff_trade_date)
                else:
                    ff_map = await asyncio.to_thread(batch_ff_reader, darkpool_codes)
            except Exception:
                ff_map = {}

        semaphore = asyncio.Semaphore(6)

        async def fetch_one(code: str) -> tuple[str, dict[str, Any]]:
            item = user_items[code]
            snapshot = None
            if callable(batch_quotes_fn):
                snapshot = batch_snapshots.get(code)
                if snapshot is None and store is not None:
                    snapshot = await asyncio.to_thread(store.latest_market_snapshot, code)
            else:
                async with semaphore:
                    if broker is not None:
                        cached = broker.cached_snapshot(code)
                        cached_phase = (
                            getattr(cached.market_phase, "value", cached.market_phase)
                            if cached is not None
                            else None
                        )
                        if (
                            cached is not None
                            and not broker.is_market_open()
                            and cached_phase in {"CLOSED", "BREAK"}
                        ):
                            snapshot = cached
                        else:
                            try:
                                snapshot = await broker.refresh(code, detail=False, max_age_seconds=10.0)
                            except Exception:
                                snapshot = cached
                    if snapshot is None and store is not None:
                        snapshot = await asyncio.to_thread(store.latest_market_snapshot, code)
            resolved_market = _resolved_market(code, item.market)
            supports_dp = _supports_darkpool(code, resolved_market)
            snap_source_time = getattr(snapshot, "source_time", None) if snapshot is not None else None
            target_ff_date = extract_quote_trade_date(snap_source_time) or ff_trade_date
            if supports_dp:
                if callable(batch_ff_reader):
                    entry = ff_map.get(code)
                    ff_meta, today_ff = entry, entry
                else:
                    ff_meta, today_ff = await asyncio.to_thread(
                        _extract_today_fund_flow,
                        store,
                        code,
                        trade_date=target_ff_date,
                    )
                if (
                    ff_meta is not None
                    and target_ff_date is not None
                    and ff_meta.get("trade_date") != target_ff_date
                ):
                    ff_meta, today_ff = None, None
            else:
                ff_meta, today_ff = None, None
            if snapshot is not None:
                public = snapshot.as_public()
                quote = public.get("quote") or {}
                market_phase = public.get("market_phase")
                phase_value = market_phase.value if hasattr(market_phase, "value") else market_phase
                summary = {
                    "symbol": code,
                    "name": public.get("name") or _resolve_item_name(code, item.name),
                    "market": public.get("market") or resolved_market,
                    "supports_darkpool": supports_dp,
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
                    "market": resolved_market,
                    "supports_darkpool": supports_dp,
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
        market = public.get("market") or _resolved_market(normalized, watchlist_item.market)
        supports_dp = _supports_darkpool(normalized, market)
        return {
            "symbol": normalized,
            "name": stock_name,
            "market": market,
            "supports_darkpool": supports_dp,
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
                "open": quote.get("open"),
                "high": quote.get("high"),
                "low": quote.get("low"),
                "amount": quote.get("amount"),
                "large_order_net": quote.get("large_order_net"),
                "large_order_amount": quote.get("large_order_amount"),
                "retail_count": quote.get("retail_count"),
                "macdfs": quote.get("macdfs"),
            },
            "main_fund_flow": (public.get("main_fund_flow") or {}) if supports_dp else {},
            "capabilities": {
                "l2": (public.get("capabilities") or {}).get("l2")
                or {"available": False, "reason": None},
                "darkpool": {
                    "available": supports_dp,
                    "reason": None if supports_dp else "FUND_DARKPOOL_UNSUPPORTED",
                },
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
        if not _supports_darkpool(normalized, _resolved_market(normalized, watchlist_item.market)):
            return {
                "symbol": normalized,
                "name": _resolve_item_name(normalized, watchlist_item.name),
                "supports_darkpool": False,
                "trade_date": None,
                "available_dates": [],
                "latest_sync_at": None,
                "last_error": None,
                "periods": {
                    "today": {"points": []},
                    "three_day": {"points": []},
                    "five_day": {"points": []},
                },
            }
        store = getattr(app.state, "research_store", None)
        if store is None:
            raise HTTPException(status_code=503, detail="research monitoring is not configured")
        try:
            history = read_fund_flow_history_for_view(
                store,
                normalized,
                trade_date.strip() if trade_date else None,
                expected_intraday_trade_date=expected_intraday_trade_date,
                intraday_minute_cap=intraday_minute_cap,
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="fund flow history not found") from None
        return {
            **history,
            "name": _resolve_item_name(normalized, watchlist_item.name),
            "supports_darkpool": True,
        }

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
        if not _supports_darkpool(normalized, _resolved_market(normalized, watchlist_item.market)):
            return {
                "symbol": normalized,
                "name": _resolve_item_name(normalized, watchlist_item.name),
                "supports_darkpool": False,
                "limit": limit,
                "baseline_trade_date": None,
                "points": [],
            }
        store = getattr(app.state, "research_store", None)
        if store is None:
            raise HTTPException(status_code=503, detail="research monitoring is not configured")
        return {
            **store.read_fund_flow_daily(normalized, limit, previous_trading_date),
            "name": _resolve_item_name(normalized, watchlist_item.name),
            "supports_darkpool": True,
        }

    @app.get(f"{prefix}/symbols/{{symbol}}/etf-flow")
    async def bridge_etf_flow(
        symbol: str,
        period: str = "60d",
        identity=Depends(require_goodluck_identity),
    ) -> dict[str, Any]:
        user, _is_admin = identity
        normalized = _validate_symbol(symbol)
        if period not in ETF_FLOW_PERIODS:
            raise HTTPException(status_code=422, detail="unsupported etf flow period")
        accounts = account_store()
        watchlist_item = _user_watchlist_items(accounts, user.id).get(normalized)
        if watchlist_item is None:
            raise HTTPException(status_code=404, detail="watchlist symbol not found")
        service = getattr(app.state, "etf_flow_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="etf fund flow service is not configured")
        try:
            payload = await fetch_etf_flow_payload(
                service,
                getattr(app.state, "market_data_broker", None),
                normalized,
                period,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except EtfFundFlowError as error:
            raise HTTPException(
                status_code=503,
                detail=fixed_market_error_code(error, "ETF_FUND_FLOW_UNAVAILABLE"),
            ) from None
        except Exception as error:
            raise HTTPException(
                status_code=503,
                detail=fixed_market_error_code(error, "ETF_FUND_FLOW_UNAVAILABLE"),
            ) from None
        return {
            **payload,
            "name": _resolve_item_name(normalized, watchlist_item.name),
        }

    # ---- Oil & Gas QDII/LOF Premium Panel ----

    @app.get(f"{prefix}/premium")
    async def bridge_get_premium(_identity=Depends(require_goodluck_identity)) -> dict[str, Any]:
        store = getattr(app.state, "research_store", None)
        monitor = getattr(app.state, "premium_monitor", None)
        brent_monitor = getattr(app.state, "brent_monitor", None)
        snapshot = await asyncio.to_thread(store.premium_snapshot) if store is not None else None
        if snapshot is None and monitor is not None:
            try:
                snapshot = await monitor.poll_once(force=True)
            except Exception:
                snapshot = None
        push_enabled = False
        if store is not None:
            try:
                push_cfg = await asyncio.to_thread(store.push_config, public=True)
                push_enabled = bool(push_cfg.get("premium_push_enabled"))
            except Exception:
                push_enabled = False
        brent_panel = None
        if store is not None:
            try:
                brent_panel = await asyncio.to_thread(build_brent_premium_panel, store, brent_monitor)
            except Exception:
                brent_panel = None
        base = snapshot or {"as_of": None, "rows": [], "valid_count": 0, "errors": {}}
        return {**base, "push_enabled": push_enabled, "brent": brent_panel}

    @app.post(f"{prefix}/premium/refresh")
    async def bridge_refresh_premium(_admin=Depends(require_goodluck_admin)) -> dict[str, Any]:
        monitor = getattr(app.state, "premium_monitor", None)
        store = getattr(app.state, "research_store", None)
        brent_monitor = getattr(app.state, "brent_monitor", None)
        if monitor is None:
            raise HTTPException(status_code=503, detail="premium monitor is not configured")
        snapshot = await monitor.poll_once(force=True)
        if brent_monitor is not None:
            try:
                await brent_monitor.poll_once(force=True)
            except Exception:
                pass
        push_enabled = False
        if store is not None:
            push_cfg = await asyncio.to_thread(store.push_config, public=True)
            push_enabled = bool(push_cfg.get("premium_push_enabled"))
        brent_panel = None
        if store is not None:
            try:
                brent_panel = await asyncio.to_thread(build_brent_premium_panel, store, brent_monitor)
            except Exception:
                brent_panel = None
        return {**snapshot, "push_enabled": push_enabled, "brent": brent_panel}

    @app.post(f"{prefix}/premium/push")
    async def bridge_push_premium(_admin=Depends(require_goodluck_admin)) -> dict[str, Any]:
        store = getattr(app.state, "research_store", None)
        dispatcher = getattr(app.state, "push_dispatcher", None)
        if store is None or dispatcher is None:
            raise HTTPException(status_code=503, detail="push dispatcher is not configured")
        snapshot = await asyncio.to_thread(store.premium_snapshot)
        if not snapshot or not snapshot.get("valid_count"):
            raise HTTPException(status_code=409, detail="暂无有效的油气溢价估值快照，请先刷新")
        config = await asyncio.to_thread(store.push_config, public=False)
        try:
            return await asyncio.to_thread(
                dispatcher.send,
                config,
                "[手动] QDII/LOF 溢价估算",
                format_premium_body(snapshot),
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    # ---- Admin-only Intraday Monitoring, Replay & Push Rules ----

    @app.get(f"{prefix}/intraday/series")
    async def bridge_intraday_series(
        symbol: str,
        trade_date: str | None = None,
        refresh: bool = False,
        rule_ids: str | None = None,
        admin_user: MarketUser = Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        normalized = _validate_symbol(symbol)
        selected_date = _validate_optional_date(trade_date)
        store = getattr(app.state, "research_store", None)
        broker = getattr(app.state, "market_data_broker", None)
        monitor = getattr(app.state, "research_monitor", None)
        accounts = account_store()
        if store is None:
            raise HTTPException(status_code=503, detail="research monitoring is not configured")

        now_sh = datetime.now(_SHANGHAI_TZ)
        today_str = now_sh.strftime("%Y%m%d")
        synced_at = now_sh.isoformat(timespec="seconds")
        market_open = bool(broker.is_market_open()) if broker is not None else False
        refresh_error: str | None = None

        if refresh and (selected_date is None or selected_date == today_str) and broker is not None and market_open:
            try:
                live_snap = await broker.refresh(normalized, detail=True, max_age_seconds=0)
                changed = await asyncio.to_thread(store.record_snapshot, live_snap)
                if is_exchange_fund_symbol(normalized):
                    try:
                        await asyncio.to_thread(
                            enrich_store_with_line_funds,
                            store,
                            normalized,
                            trade_date=today_str,
                            now=now_sh,
                        )
                    except Exception:
                        pass
                if changed and monitor is not None and getattr(monitor, "dispatcher", None) is not None:
                    try:
                        await monitor._push_changed(normalized, changed)
                    except Exception:
                        pass
            except Exception as error:
                refresh_error = fixed_market_error_code(error, "MARKET_QUOTE_UNAVAILABLE")
                try:
                    await asyncio.to_thread(store.record_error, normalized, refresh_error)
                except Exception:
                    pass

        try:
            series = await asyncio.to_thread(store.read_series, normalized, selected_date)
        except LookupError:
            if selected_date is None and broker is not None:
                try:
                    live_snap = await broker.refresh(normalized, detail=True, max_age_seconds=0)
                    await asyncio.to_thread(store.record_snapshot, live_snap)
                    series = await asyncio.to_thread(store.read_series, normalized, None)
                except Exception as error:
                    code = fixed_market_error_code(error, "MARKET_QUOTE_UNAVAILABLE")
                    raise HTTPException(status_code=404, detail=code) from None
            else:
                raise HTTPException(status_code=404, detail="本地尚无该交易日分时数据，请先执行补采") from None

        user_items = _user_watchlist_items(accounts, admin_user.id)
        fallback_name = user_items[normalized].name if normalized in user_items else normalized
        stock_name = series.get("name") or _resolve_item_name(normalized, fallback_name)
        enriched_points = await asyncio.to_thread(
            _enrich_intraday_points,
            store,
            normalized,
            series["trade_date"],
            series["points"],
        )
        macd_settings = series["macd_settings"]
        threshold = float(macd_settings.get("marker_threshold", 0))
        macd_dots = _build_macd_cross_dots(enriched_points, threshold)

        replay_payload: dict[str, Any] | None = None
        if rule_ids is not None and rule_ids.strip():
            push_cfg = await asyncio.to_thread(store.push_config, public=True)
            all_rules = push_cfg.get("rules") or []
            wanted_ids = {rid.strip() for rid in rule_ids.split(",") if rid.strip()}
            selected_rules = [
                {**r, "enabled": True}
                for r in all_rules
                if isinstance(r, dict) and ("*" in wanted_ids or str(r.get("id") or "") in wanted_ids)
            ]
            markers = build_replay_markers(normalized, enriched_points, selected_rules, marker_threshold=threshold)
            performance = calculate_replay_performance(markers, enriched_points)
            replay_payload = {"markers": markers, "performance": performance}

        latest_pt = enriched_points[-1] if enriched_points else {}
        return {
            "symbol": normalized,
            "name": stock_name,
            "market": series["market"],
            "trade_date": series["trade_date"],
            "requested_date": selected_date or series["trade_date"],
            "source": series["source"],
            "collected_at": series["collected_at"],
            "synced_at": synced_at,
            "market_open": market_open,
            "refresh_error": refresh_error,
            "pre_close": series["pre_close"],
            "available_dates": series["available_dates"],
            "macd_settings": macd_settings,
            "macd_dots": macd_dots,
            "latest": latest_pt,
            "points": enriched_points,
            "replay": replay_payload,
        }

    @app.post(f"{prefix}/intraday/replay")
    async def bridge_intraday_replay(
        payload: BridgeReplayRequest,
        _admin: MarketUser = Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        normalized = _validate_symbol(payload.symbol)
        selected_date = _validate_optional_date(payload.trade_date)
        store = getattr(app.state, "research_store", None)
        if store is None:
            raise HTTPException(status_code=503, detail="research monitoring is not configured")
        try:
            series = await asyncio.to_thread(store.read_series, normalized, selected_date)
        except LookupError:
            raise HTTPException(status_code=404, detail="research history not found") from None
        points = await asyncio.to_thread(
            _enrich_intraday_points,
            store,
            normalized,
            series["trade_date"],
            series["points"],
        )
        threshold = float(series["macd_settings"].get("marker_threshold", 0))
        if payload.rules is not None:
            active_rules = [{**r, "enabled": True} for r in payload.rules if isinstance(r, dict)]
        else:
            push_cfg = await asyncio.to_thread(store.push_config, public=True)
            wanted = set(payload.rule_ids)
            active_rules = [
                {**r, "enabled": True}
                for r in (push_cfg.get("rules") or [])
                if isinstance(r, dict) and (not wanted or str(r.get("id") or "") in wanted)
            ]
        markers = build_replay_markers(normalized, points, active_rules, marker_threshold=threshold)
        performance = calculate_replay_performance(markers, points)
        return {
            "symbol": normalized,
            "trade_date": series["trade_date"],
            "markers": markers,
            "performance": performance,
        }

    @app.post(f"{prefix}/intraday/backfill-all")
    async def bridge_intraday_backfill_all(
        admin_user: MarketUser = Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        accounts = account_store()
        broker = getattr(app.state, "market_data_broker", None)
        store = getattr(app.state, "research_store", None)
        if broker is None or store is None:
            raise HTTPException(status_code=503, detail="research monitoring is not configured")
        user_symbols = list(_user_watchlist_items(accounts, admin_user.id).keys())
        target_symbols = user_symbols or accounts.list_monitored_symbols()
        max_batch = max(50, int(getattr(accounts, "max_total_symbols", 50)))
        semaphore = asyncio.Semaphore(4)

        async def _backfill_one(code: str) -> dict[str, Any]:
            async with semaphore:
                try:
                    evict = getattr(broker, "evict", None)
                    if callable(evict):
                        evict(code)
                    backfill = getattr(app.state, "post_close_backfill", None)
                    snap = None
                    if (
                        not broker.is_market_open()
                        and callable(backfill)
                        and _supports_darkpool(code)
                    ):
                        snap = await asyncio.to_thread(backfill, code)
                        if snap is not None:
                            seed = getattr(broker, "seed", None)
                            if callable(seed):
                                seed(snap)
                    if snap is None:
                        if _supports_force_kwarg(broker.refresh):
                            snap = await broker.refresh(
                                code,
                                detail=True,
                                max_age_seconds=0,
                                force=True,
                            )
                        else:
                            snap = await broker.refresh(code, detail=True, max_age_seconds=0)
                    changed = await asyncio.to_thread(store.record_snapshot, snap)
                    return {"symbol": code, "ok": True, "changed_count": len(changed)}
                except Exception as error:
                    err_code = fixed_market_error_code(error, "MARKET_QUOTE_UNAVAILABLE")
                    await asyncio.to_thread(store.record_error, code, err_code)
                    return {"symbol": code, "ok": False, "error": err_code}

        results = await asyncio.gather(*(_backfill_one(s) for s in target_symbols[:max_batch]))
        return {
            "count": len(results),
            "results": list(results),
            "synced_at": datetime.now(_SHANGHAI_TZ).isoformat(timespec="seconds"),
        }

    @app.get(f"{prefix}/intraday/macd-settings")
    async def bridge_get_macd_settings(_admin=Depends(require_goodluck_admin)) -> dict[str, Any]:
        store = getattr(app.state, "research_store", None)
        if store is None:
            raise HTTPException(status_code=503, detail="research monitoring is not configured")
        return await asyncio.to_thread(store.macd_settings)

    @app.put(f"{prefix}/intraday/macd-settings")
    async def bridge_update_macd_settings(
        payload: BridgeMacdSettingsRequest,
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        store = getattr(app.state, "research_store", None)
        if store is None:
            raise HTTPException(status_code=503, detail="research monitoring is not configured")
        try:
            return await asyncio.to_thread(store.save_macd_settings, payload.model_dump())
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.get(f"{prefix}/intraday/push-config")
    async def bridge_get_push_config(_admin=Depends(require_goodluck_admin)) -> dict[str, Any]:
        store = getattr(app.state, "research_store", None)
        if store is None:
            raise HTTPException(status_code=503, detail="research monitoring is not configured")
        return await asyncio.to_thread(store.push_config, public=True)

    @app.put(f"{prefix}/intraday/push-config")
    async def bridge_save_push_config(
        payload: dict[str, Any],
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        store = getattr(app.state, "research_store", None)
        if store is None:
            raise HTTPException(status_code=503, detail="research monitoring is not configured")
        try:
            return await asyncio.to_thread(store.save_push_config, payload)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.post(f"{prefix}/intraday/push-test")
    async def bridge_test_push(
        payload: BridgePushTestRequest,
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        store = getattr(app.state, "research_store", None)
        dispatcher = getattr(app.state, "push_dispatcher", None)
        if store is None or dispatcher is None:
            raise HTTPException(status_code=503, detail="push dispatcher is not configured")
        saved = await asyncio.to_thread(store.push_config, public=False)
        merged = _merge_push_config_for_test(saved, payload.config)
        try:
            return await asyncio.to_thread(
                dispatcher.send,
                merged,
                "国行好运器 · 分时监控推送测试",
                "推送通道已连通",
                only_channel=payload.channel,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.post(f"{prefix}/intraday/push-rule-test")
    async def bridge_test_push_rule(
        payload: BridgePushRuleTestRequest,
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        normalized = _validate_symbol(payload.symbol)
        selected_date = _validate_optional_date(payload.trade_date)
        store = getattr(app.state, "research_store", None)
        dispatcher = getattr(app.state, "push_dispatcher", None)
        if store is None:
            raise HTTPException(status_code=503, detail="research monitoring is not configured")
        try:
            series = await asyncio.to_thread(store.read_series, normalized, selected_date)
        except LookupError:
            raise HTTPException(status_code=404, detail="本地尚无该标的分时数据") from None
        points = await asyncio.to_thread(
            _enrich_intraday_points,
            store,
            normalized,
            series["trade_date"],
            series["points"],
        )
        threshold = float(series["macd_settings"].get("marker_threshold", 0))
        evaluation = evaluate_rule_detail(
            normalized,
            points,
            payload.rule,
            marker_threshold=threshold,
        )
        markers = build_replay_markers(
            normalized,
            points,
            [{**payload.rule, "enabled": True}],
            marker_threshold=threshold,
        )
        stock_name = resolve_push_symbol_name(normalized, series.get("name"), resolve_symbol)
        latest_point = points[-1] if points else None
        push_result = None
        if payload.send_push and dispatcher is not None:
            saved = await asyncio.to_thread(store.push_config, public=False)
            merged = _merge_push_config_for_test(saved, payload.config)
            side_label = "卖点" if payload.rule.get("side") == "sell" else "买点"
            title = f"[测试] {side_label} · {payload.rule.get('title') or '分时提醒'}"
            display_symbol = format_push_symbol(normalized, stock_name)
            eval_time = str(evaluation.get("time") or "").strip()
            eval_price = evaluation.get("price")
            price_part = (
                format_push_price_and_change(
                    normalized,
                    float(eval_price),
                    latest_point,
                    series.get("pre_close"),
                )
                if eval_price is not None
                else ""
            )
            head = " ".join(part for part in (display_symbol, eval_time, price_part) if part)
            body = f"{head} · {evaluation['summary']}" if head else str(evaluation["summary"])
            try:
                push_result = await asyncio.to_thread(dispatcher.send, merged, title, body)
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from None
        return {
            "symbol": normalized,
            "name": stock_name or normalized,
            "trade_date": series["trade_date"],
            "evaluation": evaluation,
            "day_hit_count": len(markers),
            "day_hits": markers[-10:],
            "push_result": push_result,
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
        monitored_symbols = [
            symbol
            for symbol in accounts.list_monitored_symbols()
            if _supports_darkpool(symbol)
        ]
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
        premium_info = None
        if store is not None:
            snap = store.premium_snapshot()
            if snap:
                premium_info = {
                    "as_of": snap.get("as_of"),
                    "valid_count": snap.get("valid_count", 0),
                    "error_count": len(snap.get("errors") or {}),
                }
        return {
            "global_count": accounts.total_symbol_count(),
            "global_max": accounts.max_total_symbols,
            "monitored_count": len(monitored_symbols),
            "market_open": bool(broker.is_market_open()) if broker is not None else False,
            "symbol_catalog": catalog_info,
            "premium": premium_info,
            "monitoring": store.monitoring_status(monitored_symbols) if store is not None else [],
        }

    # ---- Admin-only Broker Trade Execution (Guotai Haitong on core emulator) ----

    def _require_trade_executor() -> Any:
        executor = getattr(app.state, "trade_executor", None)
        if executor is None:
            raise HTTPException(
                status_code=503,
                detail={"error_code": "TRADE_UNCONFIGURED", "message": "交易执行器未配置"},
            )
        return executor

    def _resolve_idempotency_key(request: Request, body_key: str | None) -> str | None:
        raw = str(request.headers.get("X-Idempotency-Key") or body_key or "").strip()
        if not raw:
            return None
        if len(raw) > 128 or not re.fullmatch(r"[A-Za-z0-9._:/-]+", raw):
            raise HTTPException(
                status_code=422,
                detail={"error_code": "TRADE_INVALID_IDEMPOTENCY_KEY", "message": "幂等键格式无效"},
            )
        return raw

    def _payload_hash(data: Any) -> str:
        encoded = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _check_idempotency_cache(scope: str, key: str, payload_hash: str) -> dict[str, Any] | None:
        engine = getattr(app.state, "auto_trade_engine", None)
        store = getattr(engine, "auto_store", None) if engine is not None else None
        if store is not None and callable(getattr(store, "check_idempotency", None)):
            state, hit = store.check_idempotency(
                scope=scope,
                idempotency_key=key,
                payload_hash=payload_hash,
                now_epoch=time.time(),
            )
            if state == "conflict":
                raise HTTPException(
                    status_code=409,
                    detail={"error_code": "TRADE_IDEMPOTENCY_CONFLICT", "message": "幂等键与已有请求参数不一致"},
                )
            if state == "hit" and hit is not None:
                body = dict(hit["body"])
                body["idempotent_replayed"] = True
                return body
            return None
        mem_lock = getattr(app.state, "_trade_idemp_lock", None)
        if mem_lock is None:
            mem_lock = RLock()
            app.state._trade_idemp_lock = mem_lock
            app.state._trade_idemp_cache = {}
        mem_cache: dict[tuple[str, str], tuple[str, dict[str, Any], float]] = app.state._trade_idemp_cache
        now_ep = time.time()
        with mem_lock:
            for k in [k for k, (_, _, exp) in mem_cache.items() if exp <= now_ep]:
                mem_cache.pop(k, None)
            entry = mem_cache.get((scope, key))
            if entry is None:
                return None
            saved_hash, saved_body, _ = entry
            if saved_hash != payload_hash:
                raise HTTPException(
                    status_code=409,
                    detail={"error_code": "TRADE_IDEMPOTENCY_CONFLICT", "message": "幂等键与已有请求参数不一致"},
                )
            body = dict(saved_body)
            body["idempotent_replayed"] = True
            return body

    def _save_idempotency_cache(scope: str, key: str, payload_hash: str, body: dict[str, Any]) -> None:
        engine = getattr(app.state, "auto_trade_engine", None)
        store = getattr(engine, "auto_store", None) if engine is not None else None
        if store is not None and callable(getattr(store, "save_idempotency", None)):
            store.save_idempotency(
                scope=scope,
                idempotency_key=key,
                payload_hash=payload_hash,
                status_code=200,
                body=body,
                now_epoch=time.time(),
            )
            return
        mem_lock = getattr(app.state, "_trade_idemp_lock", None)
        mem_cache = getattr(app.state, "_trade_idemp_cache", None)
        if mem_lock is not None and isinstance(mem_cache, dict):
            with mem_lock:
                mem_cache[(scope, key)] = (payload_hash, dict(body), time.time() + 3600.0)

    def _raise_trade_http_error(error: TradeExecutionError) -> None:
        raise HTTPException(
            status_code=error.status_code,
            detail={"error_code": error.error_code, "message": error.message},
        ) from None

    _TRADE_DEVICE_ROLE = "core_metrics"
    _LIFECYCLE_ERROR_MESSAGES: dict[str, tuple[int, str]] = {
        "DEVICE_ACTION_IN_PROGRESS": (409, "交易虚拟机正在开机或关机中，请稍候"),
        "DEVICE_BOOT_TIMEOUT": (504, "交易虚拟机启动超时，请稍后重试"),
        "DEVICE_AVD_NOT_FOUND": (503, "未找到交易虚拟机配置镜像，请检查宿主机 AVD"),
        "DEVICE_APP_LAUNCH_FAILED": (503, "交易虚拟机已开机，但同花顺应用启动失败"),
        "DEVICE_SHUTDOWN_FAILED": (503, "交易虚拟机关闭失败，请稍后重试"),
        "DEVICE_LIFECYCLE_UNAVAILABLE": (503, "宿主机虚拟机生命周期服务未开启或不可用"),
    }

    def _raise_lifecycle_http_error(code: str) -> None:
        status_code, msg = _LIFECYCLE_ERROR_MESSAGES.get(
            code, (503, "交易虚拟机控制操作失败，请稍后重试")
        )
        raise HTTPException(
            status_code=status_code,
            detail={"error_code": code, "message": msg},
        ) from None

    def _reset_trade_executor_cache(exec_obj: Any) -> None:
        if exec_obj is None:
            return
        exec_obj._device = None
        exec_obj._last_verified_at = 0.0
        exec_obj._prestaged_state = None

    def _read_trade_device_status_sync() -> dict[str, Any]:
        lifecycle = getattr(app.state, "device_lifecycle", None)
        control = getattr(app.state, "runner_control", None)
        executor = getattr(app.state, "trade_executor", None)
        device_serial = getattr(executor, "adb_serial", "emulator-5556")
        lc_state = "UNCONFIGURED" if lifecycle is None else "UNKNOWN"
        operation_id: str | None = None
        error_code: str | None = None
        updated_at: str | None = None

        if lifecycle is not None:
            try:
                statuses = lifecycle.devices()
            except DeviceLifecycleError as error:
                error_code = error.error_code
                lc_state = DeviceLifecycleState.UNKNOWN.value
            except Exception:
                error_code = "DEVICE_LIFECYCLE_FAILED"
                lc_state = DeviceLifecycleState.UNKNOWN.value
            else:
                by_role = {s.role: s for s in statuses}
                if control is not None:
                    for s in statuses:
                        control.reconcile_operation(s.role, s.operation_id, s.state.value)
                core_status = by_role.get(_TRADE_DEVICE_ROLE)
                if core_status is not None:
                    lc_state = core_status.state.value
                    operation_id = core_status.operation_id
                    error_code = core_status.error_code
                    updated_at = core_status.updated_at.isoformat() if core_status.updated_at else None
                    tracked_op = getattr(app.state, "goodluck_device_op_id", None)
                    if (
                        tracked_op
                        and lc_state in {"RUNNING", "STOPPED", "ERROR"}
                        and control is not None
                    ):
                        app.state.goodluck_device_op_id = None
                        control.resume_queue()
                else:
                    error_code = "DEVICE_LIFECYCLE_FAILED"
                    lc_state = DeviceLifecycleState.UNKNOWN.value

        if lc_state in {"STOPPED", "STOPPING", "STARTING"} and executor is not None:
            lock = getattr(executor, "_lock", None)
            if lock is not None and lock.acquire(blocking=False):
                try:
                    _reset_trade_executor_cache(executor)
                finally:
                    lock.release()

        probes = getattr(app.state, "device_health_probes", None) or {}
        probe = probes.get(_TRADE_DEVICE_ROLE)
        bridges = getattr(app.state, "device_bridges", None) or {}
        bridge = bridges.get(_TRADE_DEVICE_ROLE) or getattr(app.state, "device_bridge", None)
        adb_state = "OFFLINE"
        app_state = "OFFLINE"
        if lc_state == "RUNNING" or lifecycle is None:
            if callable(probe):
                try:
                    health = dict(probe())
                except Exception:
                    health = {}
                adb_state = str(health.get("adb") or "OFFLINE")
                app_state = str(health.get("app") or adb_state)
            elif bridge is not None:
                try:
                    online = bool(bridge.is_online())
                except Exception:
                    online = False
                adb_state = "ONLINE" if online else "OFFLINE"
                app_state = adb_state

        ready = (
            lc_state == "RUNNING" or (lifecycle is None and adb_state == "ONLINE")
        ) and adb_state == "ONLINE"
        return {
            "role": _TRADE_DEVICE_ROLE,
            "device_serial": device_serial,
            "lifecycle_configured": lifecycle is not None,
            "state": lc_state,
            "adb": adb_state,
            "app": app_state,
            "ready": ready,
            "operation_id": operation_id,
            "error_code": error_code,
            "updated_at": updated_at,
        }

    async def _watch_trade_device_op(op_id: str, action: str) -> None:
        lifecycle = getattr(app.state, "device_lifecycle", None)
        control = getattr(app.state, "runner_control", None)
        executor = getattr(app.state, "trade_executor", None)
        if lifecycle is None:
            return
        try:
            final_state: str | None = None
            for _ in range(95):
                await asyncio.sleep(2.0)
                try:
                    op = await asyncio.to_thread(lifecycle.operation, op_id)
                except Exception:
                    break
                st = op.state.value
                if control is not None:
                    control.reconcile_operation(op.role, op.operation_id, st)
                if st in {"RUNNING", "STOPPED", "ERROR"}:
                    final_state = st
                    break
            if control is not None:
                if getattr(app.state, "goodluck_device_op_id", None) == op_id:
                    app.state.goodluck_device_op_id = None
                control.resume_queue()
            if action == "start" and final_state == "RUNNING" and executor is not None:
                keep_warm_fn = getattr(executor, "keep_warm", None)
                if callable(keep_warm_fn):
                    await asyncio.sleep(4.0)
                    try:
                        await asyncio.to_thread(keep_warm_fn, side="buy")
                    except Exception:
                        pass
        except asyncio.CancelledError:
            return

    def _submit_trade_device_action_sync(action: Literal["start", "shutdown"]) -> dict[str, Any]:
        _read_trade_device_status_sync()
        lifecycle = getattr(app.state, "device_lifecycle", None)
        if lifecycle is None:
            _raise_lifecycle_http_error("DEVICE_LIFECYCLE_UNAVAILABLE")

        control = getattr(app.state, "runner_control", None)
        if control is not None:
            with control._maintenance_gate:
                if control._lock_owner is not None:
                    raise HTTPException(
                        status_code=409,
                        detail={"error_code": "TRADE_DEVICE_BUSY", "message": "设备当前被后台维护控制台接管，请先释放"},
                    )

        executor = getattr(app.state, "trade_executor", None)
        if action == "shutdown":
            task_store = getattr(app.state, "store", None)
            if (
                task_store is not None
                and callable(getattr(task_store, "has_running_task", None))
                and task_store.has_running_task()
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"error_code": "TRADE_DEVICE_BUSY", "message": "交易虚拟机正在执行采集队列任务，请稍后再关机"},
                )
            if executor is not None:
                lock = getattr(executor, "_lock", None)
                if lock is not None:
                    if not lock.acquire(blocking=False):
                        raise HTTPException(
                            status_code=409,
                            detail={"error_code": "TRADE_DEVICE_BUSY", "message": "交易虚拟机正在执行委托操作，请稍后再关机"},
                        )
                    try:
                        _reset_trade_executor_cache(executor)
                    finally:
                        lock.release()

        target_action = (
            DeviceLifecycleAction.START_AND_LAUNCH_APP
            if action == "start"
            else DeviceLifecycleAction.SHUTDOWN
        )
        try:
            operation = lifecycle.submit(_TRADE_DEVICE_ROLE, target_action)
        except DeviceLifecycleError as error:
            _raise_lifecycle_http_error(error.error_code)
        except Exception:
            _raise_lifecycle_http_error("DEVICE_LIFECYCLE_FAILED")

        if control is not None:
            if operation.state in {DeviceLifecycleState.STARTING, DeviceLifecycleState.STOPPING}:
                control.begin_operation(_TRADE_DEVICE_ROLE, operation.operation_id)
                app.state.goodluck_device_op_id = operation.operation_id
            else:
                control.reconcile_operation(operation.role, operation.operation_id, operation.state.value)
                app.state.goodluck_device_op_id = None
                control.resume_queue()

        status_doc = _read_trade_device_status_sync()
        status_doc["state"] = operation.state.value
        status_doc["operation_id"] = operation.operation_id
        status_doc["error_code"] = operation.error_code
        status_doc["updated_at"] = operation.updated_at.isoformat()
        status_doc["ready"] = status_doc["state"] == "RUNNING" and status_doc["adb"] == "ONLINE"
        return status_doc

    @app.get(f"{prefix}/trade/device")
    async def bridge_trade_device_status(_admin=Depends(require_goodluck_admin)) -> dict[str, Any]:
        return await asyncio.to_thread(_read_trade_device_status_sync)

    @app.post(f"{prefix}/trade/device")
    async def bridge_trade_device_action(
        payload: BridgeTradeDeviceActionRequest,
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        status_doc = await asyncio.to_thread(_submit_trade_device_action_sync, payload.action)
        op_id = status_doc.get("operation_id")
        if (
            isinstance(op_id, str)
            and op_id
            and status_doc.get("state") in {"STARTING", "STOPPING"}
        ):
            tasks = getattr(app.state, "goodluck_warmup_tasks", None)
            task = asyncio.create_task(_watch_trade_device_op(op_id, payload.action))
            if isinstance(tasks, set):
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        return status_doc

    @app.get(f"{prefix}/trade/account")
    async def bridge_trade_account(
        force: bool = Query(default=False),
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        executor = _require_trade_executor()
        try:
            max_age = 0.0 if force else 25.0
            if _supports_kwarg(executor.get_account_summary, "max_age_seconds"):
                return await asyncio.to_thread(executor.get_account_summary, max_age_seconds=max_age)
            return await asyncio.to_thread(executor.get_account_summary)
        except TradeExecutionError as error:
            _raise_trade_http_error(error)

    @app.get(f"{prefix}/trade/orders")
    async def bridge_trade_orders(
        force: bool = Query(default=False),
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        executor = _require_trade_executor()
        try:
            max_age = 0.0 if force else 25.0
            if _supports_kwarg(executor.get_today_orders, "max_age_seconds"):
                return await asyncio.to_thread(executor.get_today_orders, max_age_seconds=max_age)
            return await asyncio.to_thread(executor.get_today_orders)
        except TradeExecutionError as error:
            _raise_trade_http_error(error)

    @app.get(f"{prefix}/trade/trades")
    async def bridge_trade_trades(
        force: bool = Query(default=False),
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        executor = _require_trade_executor()
        try:
            max_age = 0.0 if force else 25.0
            if _supports_kwarg(executor.get_today_trades, "max_age_seconds"):
                return await asyncio.to_thread(executor.get_today_trades, max_age_seconds=max_age)
            return await asyncio.to_thread(executor.get_today_trades)
        except TradeExecutionError as error:
            _raise_trade_http_error(error)

    def _prepare_trade_order_params(
        symbol: str,
        price: str | float,
        quantity: int,
    ) -> tuple[SymbolLookup, str, int]:
        normalized = _validate_symbol(symbol)
        if quantity % 100 != 0:
            raise HTTPException(
                status_code=422,
                detail={"error_code": "TRADE_INVALID_QUANTITY", "message": "委托数量必须为 100 的整数倍"},
            )
        lookup = resolve_symbol(normalized)
        precision = 3 if is_exchange_fund_symbol(lookup.symbol, lookup.market) else 2
        raw_price = str(price).strip()
        try:
            price_val = float(raw_price)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail={"error_code": "TRADE_INVALID_PRICE", "message": "委托价格必须为有效正数"},
            ) from None
        if not (price_val > 0 and price_val < 1_000_000):
            raise HTTPException(
                status_code=422,
                detail={"error_code": "TRADE_INVALID_PRICE", "message": "委托价格必须大于 0"},
            )
        return lookup, f"{price_val:.{precision}f}", precision

    @app.post(f"{prefix}/trade/prestage")
    async def bridge_trade_prestage_order(
        payload: BridgeTradePrestageRequest,
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        executor = _require_trade_executor()
        lookup, formatted_price, precision = _prepare_trade_order_params(
            payload.symbol,
            payload.price,
            payload.quantity,
        )
        try:
            return await asyncio.to_thread(
                executor.prestage_order,
                symbol=lookup.symbol,
                expected_name=lookup.name,
                side=payload.side,
                price=formatted_price,
                quantity=payload.quantity,
                precision=precision,
            )
        except TradeExecutionError as error:
            _raise_trade_http_error(error)

    @app.post(f"{prefix}/trade/orders")
    async def bridge_trade_place_order(
        request: Request,
        payload: BridgeTradeOrderRequest,
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        executor = _require_trade_executor()
        idemp_key = _resolve_idempotency_key(request, payload.idempotency_key)
        lookup, formatted_price, precision = _prepare_trade_order_params(
            payload.symbol,
            payload.price,
            payload.quantity,
        )
        req_hash = (
            _payload_hash(
                {
                    "symbol": lookup.symbol,
                    "side": payload.side,
                    "price": formatted_price,
                    "quantity": payload.quantity,
                    "dry_run": payload.dry_run,
                    "auto_cancel": payload.auto_cancel,
                }
            )
            if idemp_key
            else ""
        )
        if idemp_key:
            cached = await asyncio.to_thread(_check_idempotency_cache, "trade:orders", idemp_key, req_hash)
            if cached is not None:
                return cached
        try:
            result = await asyncio.to_thread(
                executor.place_order,
                symbol=lookup.symbol,
                expected_name=lookup.name,
                side=payload.side,
                price=formatted_price,
                quantity=payload.quantity,
                precision=precision,
                dry_run=payload.dry_run,
                auto_cancel=payload.auto_cancel,
            )
            if idemp_key and isinstance(result, dict):
                await asyncio.to_thread(_save_idempotency_cache, "trade:orders", idemp_key, req_hash, result)
            return result
        except TradeExecutionError as error:
            _raise_trade_http_error(error)

    @app.post(f"{prefix}/trade/cancel")
    async def bridge_trade_cancel_order(
        payload: BridgeTradeCancelRequest,
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        executor = _require_trade_executor()
        entrust_no = (payload.entrust_no or "").strip() or None
        symbol = _validate_symbol(payload.symbol) if payload.symbol and payload.symbol.strip() else None
        if entrust_no is None and symbol is None:
            raise HTTPException(
                status_code=422,
                detail={"error_code": "TRADE_INVALID_CANCEL", "message": "撤单请提供合同编号或证券代码"},
            )
        expected_name = resolve_symbol(symbol).name if symbol else None
        engine = getattr(app.state, "auto_trade_engine", None)
        scheduler = getattr(engine, "_scheduler", None) if engine is not None else None
        try:
            if scheduler is not None and callable(getattr(scheduler, "hold", None)):
                p0 = int(getattr(scheduler, "P0_CRITICAL", 0))
                async with scheduler.hold(p0, side=payload.side, symbol=symbol):
                    return await asyncio.to_thread(
                        executor.cancel_order,
                        entrust_no=entrust_no,
                        symbol=symbol,
                        expected_name=expected_name,
                        side=payload.side,
                    )
            return await asyncio.to_thread(
                executor.cancel_order,
                entrust_no=entrust_no,
                symbol=symbol,
                expected_name=expected_name,
                side=payload.side,
            )
        except TradeExecutionError as error:
            _raise_trade_http_error(error)

    # ---- Admin-only Quantitative Strategy & Auto-Trading Control ----

    def _require_auto_trade_engine() -> Any:
        engine = getattr(app.state, "auto_trade_engine", None)
        if engine is None:
            raise HTTPException(
                status_code=503,
                detail={"error_code": "TRADE_UNCONFIGURED", "message": "量化自动交易策略引擎未配置"},
            )
        return engine

    @app.get(f"{prefix}/trade/strategy")
    async def bridge_trade_strategy_status(
        viewer_open: str | None = Query(default=None),
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        engine = _require_auto_trade_engine()
        if viewer_open is not None:
            normalized = viewer_open.strip().lower()
            if normalized in {"1", "true", "yes"}:
                engine.set_viewer_open(True)
            elif normalized in {"0", "false", "no"}:
                engine.set_viewer_open(False)
        return await asyncio.to_thread(engine.get_status)

    @app.get(f"{prefix}/trade/strategy/live-eval")
    async def bridge_trade_strategy_live_eval(
        symbol: str | None = Query(default=None),
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        engine = _require_auto_trade_engine()
        normalized = _validate_symbol(symbol) if symbol and symbol.strip() else None
        eval_fn = getattr(engine, "evaluate_live_snapshot", None)
        if not callable(eval_fn):
            raise HTTPException(status_code=501, detail="live strategy indicator evaluation unavailable")
        return await asyncio.to_thread(eval_fn, normalized)

    @app.put(f"{prefix}/trade/strategy")
    async def bridge_trade_strategy_update(
        payload: BridgeAutoTradeConfigRequest,
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        engine = _require_auto_trade_engine()
        if payload.order_quantity % 100 != 0:
            raise HTTPException(
                status_code=422,
                detail={"error_code": "TRADE_INVALID_QUANTITY", "message": "委托数量必须为 100 的整数倍"},
            )
        normalized_symbols = [_validate_symbol(sym) for sym in payload.symbols]
        patch: dict[str, Any] = {
            "enabled": payload.enabled,
            "signal_source": payload.signal_source,
            "quant_rule_id": payload.quant_rule_id,
            "execution_mode": payload.execution_mode,
            "symbols": normalized_symbols,
            "order_quantity": payload.order_quantity,
            "pricing_mode": payload.pricing_mode,
            "order_book_level": payload.order_book_level,
            "slippage_ticks": payload.slippage_ticks,
            "max_slippage_pct": payload.max_slippage_pct,
            "refresh_book_on_confirm": payload.refresh_book_on_confirm,
            "auto_cancel_seconds": payload.auto_cancel_seconds,
            "daily_max_buy_amount": payload.daily_max_buy_amount,
            "block_non_continuous_session": payload.block_non_continuous_session,
        }
        if payload.sizing_mode is not None:
            patch["sizing_mode"] = payload.sizing_mode
        if payload.symbol_profiles is not None:
            norm_profiles: dict[str, dict[str, Any]] = {}
            for raw_sym, prof in payload.symbol_profiles.items():
                norm_sym = _validate_symbol(raw_sym)
                if prof.order_quantity is not None and prof.order_quantity % 100 != 0:
                    raise HTTPException(
                        status_code=422,
                        detail={"error_code": "TRADE_INVALID_QUANTITY", "message": f"{norm_sym} 委托数量必须为 100 的整数倍"},
                    )
                norm_profiles[norm_sym] = {
                    k: v
                    for k, v in prof.model_dump(exclude_unset=True).items()
                    if v is not None or k in {"enabled", "quant_rule_id"}
                }
            patch["symbol_profiles"] = norm_profiles
        try:
            reconcile_fn = getattr(engine, "reconcile_and_update_config", None)
            if callable(reconcile_fn):
                return await asyncio.to_thread(
                    reconcile_fn,
                    patch,
                    reconcile_broker=payload.reconcile_broker,
                )
            return await asyncio.to_thread(engine.update_config, patch)
        except TradeExecutionError as error:
            _raise_trade_http_error(error)

    @app.put(f"{prefix}/trade/strategy/symbols/{{symbol}}")
    async def bridge_trade_strategy_upsert_symbol(
        symbol: str,
        payload: BridgeUpsertSymbolStrategyRequest,
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        normalized = _validate_symbol(symbol)
        if payload.order_quantity is not None and payload.order_quantity % 100 != 0:
            raise HTTPException(
                status_code=422,
                detail={"error_code": "TRADE_INVALID_QUANTITY", "message": "委托数量必须为 100 的整数倍"},
            )
        engine = _require_auto_trade_engine()
        prof_patch = {
            k: v
            for k, v in payload.model_dump(exclude={"reconcile_broker"}, exclude_unset=True).items()
            if v is not None or k in {"enabled", "quant_rule_id"}
        }
        try:
            return await asyncio.to_thread(
                engine.upsert_symbol_profile,
                normalized,
                prof_patch,
                reconcile_broker=payload.reconcile_broker,
            )
        except TradeExecutionError as error:
            _raise_trade_http_error(error)

    @app.delete(f"{prefix}/trade/strategy/symbols/{{symbol}}")
    async def bridge_trade_strategy_remove_symbol(
        symbol: str,
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        normalized = _validate_symbol(symbol)
        engine = _require_auto_trade_engine()
        try:
            return await asyncio.to_thread(engine.remove_symbol_profile, normalized)
        except TradeExecutionError as error:
            _raise_trade_http_error(error)

    @app.get(f"{prefix}/trade/order-book/{{symbol}}")
    async def bridge_trade_order_book(
        symbol: str,
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        normalized = _validate_symbol(symbol)
        lookup = resolve_symbol(normalized)
        engine = _require_auto_trade_engine()
        get_ob = engine.get_order_book
        try:
            supports_force = "force_refresh" in inspect.signature(get_ob).parameters
        except (TypeError, ValueError):
            supports_force = False
        book = await (
            asyncio.to_thread(get_ob, lookup.symbol, lookup.market, force_refresh=True)
            if supports_force
            else asyncio.to_thread(get_ob, lookup.symbol, lookup.market)
        )
        if book is None:
            raise HTTPException(
                status_code=503,
                detail={"error_code": "MARKET_QUOTE_UNAVAILABLE", "message": "实时五档盘口暂不可用，请稍后重试"},
            )
        return book

    @app.post(f"{prefix}/trade/strategy/reset-circuit")
    async def bridge_trade_strategy_reset_circuit(_admin=Depends(require_goodluck_admin)) -> dict[str, Any]:
        engine = _require_auto_trade_engine()
        return await asyncio.to_thread(engine.reset_circuit)

    @app.post(f"{prefix}/trade/strategy/simulate")
    async def bridge_trade_strategy_simulate(
        payload: BridgeAutoTradeSimulateRequest,
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        engine = _require_auto_trade_engine()
        sym = _validate_symbol(payload.symbol) if payload.symbol and payload.symbol.strip() else None
        try:
            return await engine.simulate_signal(
                symbol=sym,
                side=payload.side,
                viewer_open=payload.viewer_open,
            )
        except TradeExecutionError as error:
            _raise_trade_http_error(error)

    @app.post(f"{prefix}/trade/strategy/signals/{{signal_id}}/decide")
    async def bridge_trade_strategy_decide(
        signal_id: str,
        request: Request,
        payload: BridgeAutoTradeDecideRequest,
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        engine = _require_auto_trade_engine()
        idemp_key = _resolve_idempotency_key(request, payload.idempotency_key)
        override_price = (
            str(payload.override_price).strip()
            if payload.override_price is not None and str(payload.override_price).strip()
            else None
        )
        req_hash = (
            _payload_hash(
                {
                    "signal_id": signal_id,
                    "action": payload.action,
                    "dry_run": payload.dry_run,
                    "override_price": override_price,
                    "override_quantity": payload.override_quantity,
                    "override_book_level": payload.override_book_level,
                }
            )
            if idemp_key
            else ""
        )
        if idemp_key:
            cached = await asyncio.to_thread(_check_idempotency_cache, f"trade:decide:{signal_id}", idemp_key, req_hash)
            if cached is not None:
                return cached
        try:
            result = await engine.decide_signal(
                signal_id,
                action=payload.action,
                dry_run=payload.dry_run,
                override_price=override_price,
                override_quantity=payload.override_quantity,
                override_book_level=payload.override_book_level,
            )
            if idemp_key and isinstance(result, dict):
                await asyncio.to_thread(
                    _save_idempotency_cache, f"trade:decide:{signal_id}", idemp_key, req_hash, result
                )
            return result
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail={"error_code": "TRADE_SIGNAL_NOT_FOUND", "message": "未找到对应的交易信号记录"},
            ) from None
        except TradeExecutionError as error:
            _raise_trade_http_error(error)

    @app.get(f"{prefix}/trade/events")
    async def bridge_trade_events(
        request: Request,
        viewer_open: str | None = Query(default=None),
        _admin=Depends(require_goodluck_admin),
    ) -> StreamingResponse:
        engine = _require_auto_trade_engine()
        if viewer_open is not None:
            normalized = viewer_open.strip().lower()
            if normalized in {"1", "true", "yes"}:
                engine.set_viewer_open(True)
            elif normalized in {"0", "false", "no"}:
                engine.set_viewer_open(False)

        async def _event_stream() -> AsyncIterator[str]:
            q = engine.subscribe()
            try:
                snap = await asyncio.to_thread(engine.get_status)
                yield f"event: snapshot\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15.0)
                        ev_type = str(ev.get("type") or "message")
                        yield f"event: {ev_type}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                engine.unsubscribe(q)

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ---- Admin-only Quantitative Strategy Research, AI Rule API & Backtest ----

    def _require_quant_stores() -> tuple[Any, Any]:
        research_store = getattr(app.state, "research_store", None)
        quant_store = getattr(app.state, "quant_rule_store", None)
        if research_store is None or quant_store is None:
            raise HTTPException(status_code=503, detail="量化规则复盘库未配置")
        return research_store, quant_store

    @app.get(f"{prefix}/quant/schema")
    def bridge_quant_schema(_admin=Depends(require_goodluck_admin)) -> dict[str, Any]:
        return get_quant_rule_schema()

    @app.get(f"{prefix}/quant/dataset")
    async def bridge_quant_dataset(
        symbol: str = "160723",
        start_date: str = "20260701",
        end_date: str | None = None,
        mode: Literal["summary", "daily", "minutes"] = "summary",
        limit_days: int = Query(default=30, ge=1, le=200),
        short: int = Query(default=10, ge=2, le=200),
        long: int = Query(default=20, ge=3, le=200),
        signal: int = Query(default=5, ge=1, le=100),
        marker_threshold: float = Query(default=0.00001, ge=0.0, le=1.0),
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        research_store, _ = _require_quant_stores()
        normalized = _validate_quant_symbol(symbol)
        clean_start = _validate_optional_date(start_date) or "20260701"
        clean_end = _validate_optional_date(end_date)
        return await asyncio.to_thread(
            export_quant_dataset,
            research_store,
            normalized,
            start_date=clean_start,
            end_date=clean_end,
            mode=mode,
            limit_days=limit_days,
            macd_settings={
                "short": short,
                "long": max(short + 1, long),
                "signal": signal,
                "marker_threshold": marker_threshold,
            },
        )

    @app.post(f"{prefix}/quant/backfill-history")
    async def bridge_quant_backfill_history(
        payload: BridgeQuantBackfillRequest,
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        research_store, _ = _require_quant_stores()
        normalized = _validate_quant_symbol(payload.symbol)
        clean_start = _validate_optional_date(payload.start_date) or "20260301"
        clean_end = _validate_optional_date(payload.end_date)
        if normalized == BRENT_SYMBOL:
            try:
                return await asyncio.to_thread(
                    backfill_brent_to_research_store,
                    research_store,
                    start_date=clean_start,
                    end_date=clean_end,
                )
            except Exception as error:
                raise HTTPException(status_code=503, detail=f"BRN0Y 历史数据补齐失败: {error}") from None
        try:
            lookup = resolve_symbol(normalized)
            sym_name, sym_market = lookup.name, lookup.market
        except Exception:
            sym_name, sym_market = None, None
        try:
            return await asyncio.to_thread(
                backfill_intraday_history,
                research_store,
                normalized,
                start_date=clean_start,
                end_date=clean_end,
                name=sym_name,
                market=sym_market,
            )
        except Exception as error:
            raise HTTPException(status_code=503, detail=f"历史分时补齐失败: {error}") from None

    @app.post(f"{prefix}/quant/evaluate")
    async def bridge_quant_evaluate(
        payload: BridgeQuantEvaluateRequest,
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        research_store, quant_store = _require_quant_stores()
        normalized = _validate_quant_symbol(payload.symbol)
        clean_start = _validate_optional_date(payload.start_date)
        clean_end = _validate_optional_date(payload.end_date)
        if payload.rule_id and payload.buy_rule is None and payload.sell_rule is None:
            stored = await asyncio.to_thread(quant_store.get_rule, payload.rule_id)
            if stored is None:
                raise HTTPException(status_code=404, detail="未找到指定量化规则")
            spec = dict(stored)
            if payload.position_sizing is not None:
                spec["position_sizing"] = payload.position_sizing
        else:
            spec = {
                "id": payload.rule_id or "adhoc-eval",
                "symbols": [normalized],
                "macd_settings": payload.macd_settings,
                "buy_rule": payload.buy_rule,
                "sell_rule": payload.sell_rule,
                "position_sizing": payload.position_sizing,
                "eval_criteria": payload.eval_criteria,
            }
        try:
            return await asyncio.to_thread(
                evaluate_quant_strategy,
                research_store,
                spec,
                symbol=normalized,
                start_date=clean_start,
                end_date=clean_end,
                primary_horizon_minutes=payload.primary_horizon_minutes,
                min_move_pct=payload.min_move_pct,
                data_scope=payload.data_scope,
                morning_start=payload.morning_start,
                morning_end=payload.morning_end,
                max_daily_trades=payload.max_daily_trades,
                round_trip_fee_pct=payload.round_trip_fee_pct,
                capital_cap=payload.capital_cap,
                signal_limit=payload.signal_limit,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.get(f"{prefix}/quant/rules")
    async def bridge_quant_list_rules(
        compact: bool = Query(default=False),
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        research_store, quant_store = _require_quant_stores()
        rules = await asyncio.to_thread(quant_store.list_rules)
        for idx, item in enumerate(rules):
            if item.get("latest_eval") is None:
                res = await asyncio.to_thread(
                    quant_store.evaluate_and_save_rule,
                    item["id"],
                    research_store,
                )
                rules[idx] = res["rule"]
        if compact:
            rules = [compact_quant_rule_record(item) for item in rules]
        return {"rules": rules}

    @app.get(f"{prefix}/quant/rules/{{rule_id}}")
    async def bridge_quant_get_rule(
        rule_id: str,
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        research_store, quant_store = _require_quant_stores()
        rule = await asyncio.to_thread(quant_store.get_rule, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="未找到指定量化规则")
        if rule.get("latest_eval") is None:
            res = await asyncio.to_thread(
                quant_store.evaluate_and_save_rule,
                rule["id"],
                research_store,
            )
            rule = res["rule"]
        return {"rule": rule}

    @app.post(f"{prefix}/quant/rules")
    async def bridge_quant_create_or_upsert_rule(
        payload: BridgeQuantRuleUpsertRequest,
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        research_store, quant_store = _require_quant_stores()
        try:
            saved = await asyncio.to_thread(
                quant_store.upsert_rule,
                payload.model_dump(),
                research_store=research_store,
                rule_id=payload.id,
            )
            return {"rule": saved}
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.put(f"{prefix}/quant/rules/{{rule_id}}")
    async def bridge_quant_update_rule(
        rule_id: str,
        payload: BridgeQuantRuleUpsertRequest,
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        research_store, quant_store = _require_quant_stores()
        try:
            saved = await asyncio.to_thread(
                quant_store.upsert_rule,
                payload.model_dump(),
                research_store=research_store,
                rule_id=rule_id,
            )
            return {"rule": saved}
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.delete(f"{prefix}/quant/rules/{{rule_id}}", status_code=204)
    async def bridge_quant_delete_rule(
        rule_id: str,
        _admin=Depends(require_goodluck_admin),
    ) -> None:
        _, quant_store = _require_quant_stores()
        try:
            await asyncio.to_thread(quant_store.delete_rule, rule_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="未找到指定量化规则") from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.post(f"{prefix}/quant/rules/{{rule_id}}/evaluate")
    async def bridge_quant_evaluate_rule(
        rule_id: str,
        payload: BridgeQuantEvaluateRequest | None = None,
        _admin=Depends(require_goodluck_admin),
    ) -> dict[str, Any]:
        research_store, quant_store = _require_quant_stores()
        req = payload or BridgeQuantEvaluateRequest()
        try:
            return await asyncio.to_thread(
                quant_store.evaluate_and_save_rule,
                rule_id,
                research_store,
                symbol=_validate_quant_symbol(req.symbol) if req.symbol else None,
                start_date=_validate_optional_date(req.start_date),
                end_date=_validate_optional_date(req.end_date),
                primary_horizon_minutes=req.primary_horizon_minutes,
                min_move_pct=req.min_move_pct,
                data_scope=req.data_scope,
                morning_start=req.morning_start,
                morning_end=req.morning_end,
                max_daily_trades=req.max_daily_trades,
                round_trip_fee_pct=req.round_trip_fee_pct,
                capital_cap=req.capital_cap,
                signal_limit=req.signal_limit,
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="未找到指定量化规则") from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
