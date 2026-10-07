"""Authenticated market-user and grouped-watchlist HTTP routes."""

from __future__ import annotations

import re
from collections import deque
from datetime import date, datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable

import asyncio
import secrets

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .market_accounts import DuplicateUserError, MarketUser, WatchlistGroup, WatchlistItem
from .market_data import MARKET_PERIODS, fixed_market_error_code
from .parsed_values import SymbolLookup
from .research_replay import build_replay_markers, calculate_replay_performance
from .research_premium import format_premium_body


class MarketLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class MarketPasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)
    new_password_confirmation: str = Field(min_length=8, max_length=256)


class AdminCreateMarketUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    temporary_password: str = Field(min_length=8, max_length=256)


class AdminUpdateMarketUserRequest(BaseModel):
    enabled: bool | None = None
    temporary_password: str | None = Field(default=None, min_length=8, max_length=256)


class MarketUserResponse(BaseModel):
    id: int
    username: str
    enabled: bool
    must_change_password: bool
    created_at: datetime


class GroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=24)


class GroupOrderRequest(BaseModel):
    group_ids: list[int]


class SymbolRequest(BaseModel):
    symbol: str


class SymbolOrderRequest(BaseModel):
    symbols: list[str]


class MoveSymbolRequest(BaseModel):
    source_group_id: int
    target_group_id: int
    symbol: str
    target_index: int = Field(ge=0)


class MonitoringUpdateRequest(BaseModel):
    enabled: bool


class MonitoringStateResponse(BaseModel):
    symbol: str
    enabled: bool
    latest_trade_date: str | None = None
    latest_time: str | None = None
    last_sync_at: str | None = None
    last_error: str | None = None


class MonitoringBackfillResponse(BaseModel):
    symbol: str
    changed_minutes: list[str]


class MacdSettingsRequest(BaseModel):
    short: int = Field(ge=1, le=200)
    long: int = Field(ge=2, le=200)
    signal: int = Field(ge=1, le=100)
    marker_threshold: float = Field(ge=0, le=1)


class ReplayEvaluateRequest(BaseModel):
    trade_date: str | None = None
    rule_ids: list[str] = []


class WatchlistItemResponse(BaseModel):
    symbol: str
    name: str
    market: str


class WatchlistGroupResponse(BaseModel):
    id: int
    name: str
    sort_order: int
    is_primary: bool
    items: list[WatchlistItemResponse]


class WatchlistsResponse(BaseModel):
    groups: list[WatchlistGroupResponse]


def _user_response(user: MarketUser) -> MarketUserResponse:
    return MarketUserResponse.model_validate(user, from_attributes=True)


def _item_response(
    item: WatchlistItem,
    resolve_symbol: Callable[[str], SymbolLookup] | None = None,
) -> WatchlistItemResponse:
    if resolve_symbol is not None:
        try:
            current = resolve_symbol(item.symbol)
        except Exception:
            current = None
        if current is not None:
            return WatchlistItemResponse(
                symbol=current.symbol,
                name=current.name,
                market=current.market,
            )
    return WatchlistItemResponse.model_validate(item, from_attributes=True)


def _group_response(
    group: WatchlistGroup,
    resolve_symbol: Callable[[str], SymbolLookup] | None = None,
) -> WatchlistGroupResponse:
    return WatchlistGroupResponse(
        id=group.id,
        name=group.name,
        sort_order=group.sort_order,
        is_primary=group.is_primary,
        items=[
            _item_response(item, resolve_symbol)
            for item in group.items
        ],
    )


def install_market_routes(
    app: FastAPI,
    *,
    require_admin: Callable[..., Any],
    require_admin_csrf: Callable[..., Any],
    resolve_symbol: Callable[[str], SymbolLookup],
    secure_cookies: bool,
    previous_trading_date: Callable[[str], str] | None = None,
) -> None:
    """Install routes after the parent app has constructed its shared dependencies."""

    def default_previous_trading_date(value: str) -> str:
        current = datetime.strptime(value, "%Y%m%d").date() - timedelta(days=1)
        while current.weekday() >= 5:
            current -= timedelta(days=1)
        return current.strftime("%Y%m%d")

    previous_trading_date = previous_trading_date or default_previous_trading_date
    login_failures: dict[str, deque[datetime]] = {}
    login_failure_lock = RLock()
    login_failure_window = timedelta(minutes=1)

    def login_allowed(identifier: str) -> bool:
        now = datetime.now(timezone.utc)
        with login_failure_lock:
            failures = login_failures.get(identifier)
            if failures is None:
                return True
            cutoff = now - login_failure_window
            while failures and failures[0] <= cutoff:
                failures.popleft()
            if not failures:
                login_failures.pop(identifier, None)
                return True
            return len(failures) < 5

    def set_market_session_cookies(response: Response, session: Any) -> None:
        response.set_cookie(
            "ths_market_session",
            session.session_id,
            httponly=True,
            samesite="strict",
            secure=secure_cookies,
            max_age=7 * 24 * 60 * 60,
        )
        response.set_cookie(
            "ths_market_csrf",
            session.csrf_token,
            httponly=False,
            samesite="strict",
            secure=secure_cookies,
            max_age=7 * 24 * 60 * 60,
        )

    def stores() -> tuple[Any, Any]:
        accounts = app.state.market_account_store
        sessions = app.state.market_session_store
        if accounts is None or sessions is None:
            raise HTTPException(status_code=503, detail="market application is not configured")
        return accounts, sessions

    def require_market_session(request: Request):
        accounts, sessions = stores()
        session_id = request.cookies.get("ths_market_session")
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=401, detail="market authentication required")
        try:
            user = accounts.get_user(session.user_id)
        except LookupError:
            sessions.revoke(session_id)
            raise HTTPException(status_code=401, detail="market authentication required") from None
        if not user.enabled:
            sessions.revoke(session_id)
            raise HTTPException(status_code=401, detail="market authentication required")
        return session, user

    def require_market_csrf(request: Request, identity=Depends(require_market_session)):
        session, user = identity
        if request.headers.get("X-CSRF-Token") != session.csrf_token:
            raise HTTPException(status_code=403, detail="CSRF token required")
        return session, user

    def require_ready_user(identity=Depends(require_market_session)) -> MarketUser:
        _session, user = identity
        if user.must_change_password:
            raise HTTPException(status_code=403, detail="password change required")
        return user

    def require_ready_csrf(identity=Depends(require_market_csrf)) -> MarketUser:
        _session, user = identity
        if user.must_change_password:
            raise HTTPException(status_code=403, detail="password change required")
        return user

    def websocket_identity(websocket: WebSocket):
        accounts, sessions = stores()
        session = sessions.get(websocket.cookies.get("ths_market_session"))
        if session is None:
            return None
        try:
            user = accounts.get_user(session.user_id)
        except LookupError:
            return None
        if not user.enabled or user.must_change_password:
            return None
        return session, user

    @app.post("/api/v1/session", response_model=MarketUserResponse)
    def market_login(payload: MarketLoginRequest, request: Request, response: Response) -> MarketUserResponse:
        accounts, sessions = stores()
        host = request.client.host if request.client else "unknown"
        identifier = f"{host}:{payload.username.strip().lower()}"
        if not login_allowed(identifier):
            raise HTTPException(
                status_code=429,
                detail="too many failed market logins",
                headers={"Retry-After": "60"},
            )
        user = accounts.authenticate(payload.username, payload.password)
        if user is None:
            with login_failure_lock:
                login_failures.setdefault(identifier, deque()).append(datetime.now(timezone.utc))
            raise HTTPException(status_code=401, detail="invalid credentials")
        with login_failure_lock:
            login_failures.pop(identifier, None)
        session = sessions.create(user.id)
        set_market_session_cookies(response, session)
        return _user_response(user)

    @app.get("/api/v1/session", response_model=MarketUserResponse)
    def market_session(identity=Depends(require_market_session)) -> MarketUserResponse:
        return _user_response(identity[1])

    @app.delete("/api/v1/session", status_code=204)
    def market_logout(
        response: Response,
        identity=Depends(require_market_csrf),
    ) -> None:
        _accounts, sessions = stores()
        session, _user = identity
        sessions.revoke(session.session_id)
        response.delete_cookie("ths_market_session", httponly=True, samesite="strict", secure=secure_cookies)
        response.delete_cookie("ths_market_csrf", httponly=False, samesite="strict", secure=secure_cookies)

    @app.post("/api/v1/session/password", status_code=204)
    def market_change_password(
        payload: MarketPasswordRequest,
        response: Response,
        identity=Depends(require_market_csrf),
    ) -> None:
        if payload.new_password != payload.new_password_confirmation:
            raise HTTPException(status_code=422, detail="new passwords do not match")
        accounts, sessions = stores()
        _session, user = identity
        try:
            accounts.change_password(user.id, payload.current_password, payload.new_password)
        except PermissionError:
            raise HTTPException(status_code=401, detail="invalid current password") from None
        sessions.revoke_user(user.id)
        set_market_session_cookies(response, sessions.create(user.id))

    @app.get("/api/admin/users", response_model=list[MarketUserResponse])
    def list_market_users(_admin=Depends(require_admin)) -> list[MarketUserResponse]:
        accounts, _sessions = stores()
        return [_user_response(user) for user in accounts.list_users()]

    @app.post("/api/admin/users", status_code=201, response_model=MarketUserResponse)
    def create_market_user(
        payload: AdminCreateMarketUserRequest,
        _admin=Depends(require_admin_csrf),
    ) -> MarketUserResponse:
        accounts, _sessions = stores()
        try:
            return _user_response(accounts.create_user(payload.username, payload.temporary_password))
        except DuplicateUserError:
            raise HTTPException(status_code=409, detail="username already exists") from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.patch("/api/admin/users/{user_id}", response_model=MarketUserResponse)
    def update_market_user(
        user_id: int,
        payload: AdminUpdateMarketUserRequest,
        _admin=Depends(require_admin_csrf),
    ) -> MarketUserResponse:
        accounts, sessions = stores()
        if payload.enabled is None and payload.temporary_password is None:
            raise HTTPException(status_code=422, detail="no user change requested")
        try:
            if payload.temporary_password is not None:
                user = accounts.reset_password(user_id, payload.temporary_password)
                sessions.revoke_user(user_id)
            else:
                user = accounts.get_user(user_id)
            if payload.enabled is not None:
                user = accounts.set_user_enabled(user_id, payload.enabled)
                if not payload.enabled:
                    sessions.revoke_user(user_id)
            return _user_response(user)
        except LookupError:
            raise HTTPException(status_code=404, detail="market user not found") from None

    @app.get("/api/v1/watchlists", response_model=WatchlistsResponse)
    def get_watchlists(user=Depends(require_ready_user)) -> WatchlistsResponse:
        accounts, _sessions = stores()
        return WatchlistsResponse(
            groups=[
                _group_response(group, resolve_symbol)
                for group in accounts.list_watchlists(user.id)
            ]
        )

    @app.post("/api/v1/watchlists/groups", status_code=201, response_model=WatchlistGroupResponse)
    def create_watchlist_group(
        payload: GroupRequest,
        user=Depends(require_ready_csrf),
    ) -> WatchlistGroupResponse:
        accounts, _sessions = stores()
        try:
            return _group_response(
                accounts.create_group(user.id, payload.name),
                resolve_symbol,
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.patch("/api/v1/watchlists/groups/{group_id}", response_model=WatchlistGroupResponse)
    def rename_watchlist_group(
        group_id: int,
        payload: GroupRequest,
        user=Depends(require_ready_csrf),
    ) -> WatchlistGroupResponse:
        accounts, _sessions = stores()
        try:
            return _group_response(
                accounts.rename_group(user.id, group_id, payload.name),
                resolve_symbol,
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="watchlist group not found") from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.delete("/api/v1/watchlists/groups/{group_id}", status_code=204)
    def delete_watchlist_group(group_id: int, user=Depends(require_ready_csrf)) -> None:
        accounts, _sessions = stores()
        try:
            accounts.delete_group(user.id, group_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="watchlist group not found") from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.put("/api/v1/watchlists/groups/order", status_code=204)
    def reorder_watchlist_groups(
        payload: GroupOrderRequest,
        user=Depends(require_ready_csrf),
    ) -> None:
        accounts, _sessions = stores()
        try:
            accounts.reorder_groups(user.id, payload.group_ids)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.post(
        "/api/v1/watchlists/groups/{group_id}/symbols",
        status_code=201,
        response_model=WatchlistItemResponse,
    )
    def add_watchlist_symbol(
        group_id: int,
        payload: SymbolRequest,
        user=Depends(require_ready_csrf),
    ) -> WatchlistItemResponse:
        normalized = payload.symbol.strip()
        if not re.fullmatch(r"[0-9]{6}", normalized):
            raise HTTPException(status_code=422, detail="symbol must be a six-digit stock code")
        lookup = resolve_symbol(normalized)
        accounts, _sessions = stores()
        try:
            return _item_response(
                accounts.add_symbol(user.id, group_id, lookup),
                resolve_symbol,
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="watchlist group not found") from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.delete("/api/v1/watchlists/groups/{group_id}/symbols/{symbol}", status_code=204)
    def remove_watchlist_symbol(
        group_id: int,
        symbol: str,
        user=Depends(require_ready_csrf),
    ) -> None:
        accounts, _sessions = stores()
        try:
            accounts.remove_symbol(user.id, group_id, symbol)
        except LookupError:
            raise HTTPException(status_code=404, detail="watchlist symbol not found") from None

    @app.delete("/api/v1/watchlists/symbols/{symbol}", status_code=204)
    def remove_watchlist_symbol_everywhere(
        symbol: str,
        user=Depends(require_ready_csrf),
    ) -> None:
        normalized = symbol.strip()
        if not re.fullmatch(r"[0-9]{6}", normalized):
            raise HTTPException(status_code=422, detail="symbol must be a six-digit stock code")
        accounts, _sessions = stores()
        try:
            accounts.remove_symbol_everywhere(user.id, normalized)
        except LookupError:
            raise HTTPException(status_code=404, detail="watchlist symbol not found") from None

    @app.put("/api/v1/watchlists/groups/{group_id}/symbols/order", status_code=204)
    def reorder_watchlist_symbols(
        group_id: int,
        payload: SymbolOrderRequest,
        user=Depends(require_ready_csrf),
    ) -> None:
        accounts, _sessions = stores()
        try:
            accounts.reorder_symbols(user.id, group_id, payload.symbols)
        except LookupError:
            raise HTTPException(status_code=404, detail="watchlist group not found") from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.post("/api/v1/watchlists/symbols/move", status_code=204)
    def move_watchlist_symbol(
        payload: MoveSymbolRequest,
        user=Depends(require_ready_csrf),
    ) -> None:
        accounts, _sessions = stores()
        try:
            accounts.move_symbol(
                user.id,
                payload.source_group_id,
                payload.target_group_id,
                payload.symbol,
                payload.target_index,
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="watchlist symbol or group not found") from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @app.get("/api/v1/monitoring", response_model=list[MonitoringStateResponse])
    def monitoring_state(user=Depends(require_ready_user)) -> list[MonitoringStateResponse]:
        accounts, _sessions = stores()
        states = accounts.monitoring_state(user.id)
        statuses = {
            item["symbol"]: item
            for item in (
                app.state.research_store.monitoring_status(states)
                if app.state.research_store is not None
                else []
            )
        }
        return [
            MonitoringStateResponse(
                symbol=symbol,
                enabled=enabled,
                **{
                    key: statuses.get(symbol, {}).get(key)
                    for key in (
                        "latest_trade_date", "latest_time", "last_sync_at", "last_error"
                    )
                },
            )
            for symbol, enabled in states.items()
        ]

    @app.get("/api/v1/monitoring/portfolio")
    def monitoring_portfolio(user=Depends(require_ready_user)):
        accounts, _sessions = stores()
        states = accounts.monitoring_state(user.id)
        enabled_symbols = [symbol for symbol, enabled in states.items() if enabled]
        statuses = {
            item["symbol"]: item
            for item in (
                app.state.research_store.monitoring_status(enabled_symbols)
                if app.state.research_store is not None
                else []
            )
        }
        rules = (
            app.state.research_store.push_config(public=True).get("rules", [])
            if app.state.research_store is not None
            else []
        )
        items = []
        for symbol in enabled_symbols:
            series = None
            try:
                series = app.state.research_store.read_series(symbol)
            except (AttributeError, LookupError):
                pass
            latest_signal = None
            latest_price = None
            latest_time = statuses.get(symbol, {}).get("latest_time")
            if series is not None and series["points"]:
                latest = series["points"][-1]
                latest_price = latest.get("price")
                markers = build_replay_markers(
                    symbol,
                    series["points"],
                    rules,
                    marker_threshold=float(series["macd_settings"]["marker_threshold"]),
                )
                latest_markers = [marker for marker in markers if marker["index"] == len(series["points"]) - 1]
                latest_signal = latest_markers[-1] if latest_markers else None
            lookup = resolve_symbol(symbol)
            items.append(
                {
                    "symbol": symbol,
                    "name": lookup.name,
                    "enabled": True,
                    "latest_price": latest_price,
                    "latest_time": latest_time,
                    "latest_trade_date": statuses.get(symbol, {}).get("latest_trade_date"),
                    "last_sync_at": statuses.get(symbol, {}).get("last_sync_at"),
                    "last_error": statuses.get(symbol, {}).get("last_error"),
                    "latest_signal": latest_signal,
                }
            )
        return {"items": items}

    @app.get("/api/v1/monitoring/premium")
    def monitoring_premium(_user=Depends(require_ready_user)):
        snapshot = research_store().premium_snapshot()
        return snapshot or {"as_of": None, "rows": [], "valid_count": 0, "errors": {}}

    @app.patch(
        "/api/v1/monitoring/symbols/{symbol}",
        response_model=MonitoringStateResponse,
        response_model_exclude_none=True,
    )
    def update_monitoring_state(
        symbol: str,
        payload: MonitoringUpdateRequest,
        user=Depends(require_ready_csrf),
    ) -> MonitoringStateResponse:
        normalized = symbol.strip()
        if not re.fullmatch(r"[0-9]{6}", normalized):
            raise HTTPException(status_code=422, detail="symbol must be a six-digit stock code")
        accounts, _sessions = stores()
        try:
            enabled = accounts.set_monitoring_enabled(
                user.id,
                normalized,
                payload.enabled,
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="watchlist symbol not found") from None
        return MonitoringStateResponse(symbol=normalized, enabled=enabled)

    def research_store():
        store = app.state.research_store
        if store is None:
            raise HTTPException(status_code=503, detail="research monitoring is not configured")
        return store

    @app.post(
        "/api/v1/monitoring/symbols/{symbol}/backfill",
        response_model=MonitoringBackfillResponse,
    )
    async def backfill_monitoring_symbol(
        symbol: str,
        user=Depends(require_ready_csrf),
    ) -> MonitoringBackfillResponse:
        normalized = symbol.strip()
        if not re.fullmatch(r"[0-9]{6}", normalized):
            raise HTTPException(status_code=422, detail="symbol must be a six-digit stock code")
        accounts, _sessions = stores()
        if normalized not in accounts.monitoring_state(user.id):
            raise HTTPException(status_code=404, detail="watchlist symbol not found")
        try:
            snapshot = await market_broker().refresh(
                normalized,
                detail=True,
                max_age_seconds=0,
            )
            changed = research_store().record_snapshot(snapshot)
        except HTTPException:
            raise
        except Exception as error:
            code = fixed_market_error_code(error, "MARKET_QUOTE_UNAVAILABLE")
            research_store().record_error(normalized, code)
            raise HTTPException(status_code=503, detail=code) from None
        return MonitoringBackfillResponse(symbol=normalized, changed_minutes=changed)

    @app.get("/api/v1/replay/{symbol}")
    def replay_series(
        symbol: str,
        trade_date: str | None = None,
        user=Depends(require_ready_user),
    ):
        normalized = symbol.strip()
        if normalized not in stores()[0].monitoring_state(user.id):
            raise HTTPException(status_code=404, detail="watchlist symbol not found")
        try:
            return research_store().read_series(normalized, trade_date)
        except LookupError:
            raise HTTPException(status_code=404, detail="research history not found") from None

    @app.get("/api/v1/replay/rules/enabled")
    def enabled_replay_rules(_user=Depends(require_ready_user)):
        config = research_store().push_config(public=True)
        return [rule for rule in config.get("rules", []) if rule.get("enabled")]

    @app.post("/api/v1/replay/{symbol}/evaluate")
    def evaluate_replay(
        symbol: str,
        payload: ReplayEvaluateRequest,
        user=Depends(require_ready_csrf),
    ):
        normalized = symbol.strip()
        if normalized not in stores()[0].monitoring_state(user.id):
            raise HTTPException(status_code=404, detail="watchlist symbol not found")
        try:
            series = research_store().read_series(normalized, payload.trade_date)
        except LookupError:
            raise HTTPException(status_code=404, detail="research history not found") from None
        rules = research_store().push_config(public=True).get("rules", [])
        selected_ids = set(payload.rule_ids)
        selected = [
            rule for rule in rules
            if rule.get("enabled") and (not selected_ids or str(rule.get("id")) in selected_ids)
        ]
        markers = build_replay_markers(
            normalized,
            series["points"],
            selected,
            marker_threshold=float(series["macd_settings"]["marker_threshold"]),
        )
        return {
            "symbol": normalized,
            "trade_date": series["trade_date"],
            "markers": markers,
            "performance": calculate_replay_performance(markers, series["points"]),
        }

    @app.get("/api/admin/research/macd")
    def admin_macd_settings(_admin=Depends(require_admin)):
        return research_store().macd_settings()

    @app.get("/api/admin/monitoring")
    def admin_monitoring_status(_admin=Depends(require_admin)):
        accounts, _sessions = stores()
        return research_store().monitoring_status(accounts.list_monitored_symbols())

    @app.get("/api/admin/market-monitoring-list")
    def admin_market_monitoring_list(_admin=Depends(require_admin)):
        accounts, _sessions = stores()
        return accounts.list_admin_monitored_symbols()

    @app.post("/api/admin/monitoring/refresh")
    async def refresh_admin_monitoring(_admin=Depends(require_admin_csrf)):
        monitor = app.state.research_monitor
        if monitor is None:
            raise HTTPException(status_code=503, detail="research monitor is not configured")
        result = await monitor.poll_once()
        return {"symbols": result}

    @app.get("/api/admin/premium")
    def admin_premium_status(_admin=Depends(require_admin)):
        return research_store().premium_snapshot() or {
            "as_of": None, "rows": [], "valid_count": 0, "errors": {}
        }

    @app.post("/api/admin/premium/refresh")
    async def refresh_admin_premium(_admin=Depends(require_admin_csrf)):
        monitor = app.state.premium_monitor
        if monitor is None:
            raise HTTPException(status_code=503, detail="premium monitor is not configured")
        return await monitor.poll_once()

    @app.post("/api/admin/premium/push")
    def push_admin_premium(_admin=Depends(require_admin_csrf)):
        snapshot = research_store().premium_snapshot()
        if not snapshot or not snapshot.get("valid_count"):
            raise HTTPException(status_code=409, detail="premium snapshot is unavailable")
        dispatcher = app.state.push_dispatcher
        if dispatcher is None:
            raise HTTPException(status_code=503, detail="push dispatcher is not configured")
        return dispatcher.send(
            research_store().push_config(public=False),
            "QDII/LOF 溢价估算",
            format_premium_body(snapshot),
        )

    @app.put("/api/admin/research/macd")
    def update_admin_macd_settings(
        payload: MacdSettingsRequest,
        _admin=Depends(require_admin_csrf),
    ):
        try:
            return research_store().save_macd_settings(payload.model_dump())
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.get("/api/admin/push/config")
    def admin_push_config(_admin=Depends(require_admin)):
        return research_store().push_config(public=True)

    @app.put("/api/admin/push/config")
    def update_admin_push_config(
        payload: dict[str, Any],
        _admin=Depends(require_admin_csrf),
    ):
        try:
            return research_store().save_push_config(payload)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.post("/api/admin/push/test")
    def test_admin_push(_admin=Depends(require_admin_csrf)):
        dispatcher = app.state.push_dispatcher
        if dispatcher is None:
            raise HTTPException(status_code=503, detail="push dispatcher is not configured")
        try:
            return dispatcher.send(
                research_store().push_config(public=False),
                "THS Market 推送测试",
                "推送配置已连通",
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    # Temporary OPM compatibility routes. They adapt to the new stores and
    # authentication model; no OPM collector, cookie source, or global state is used.
    @app.get("/api/watchlist")
    def legacy_watchlist(user=Depends(require_ready_user)):
        accounts, _sessions = stores()
        groups = accounts.list_watchlists(user.id)
        primary = next((group for group in groups if group.is_primary), groups[0] if groups else None)
        states = accounts.monitoring_state(user.id)
        statuses = {
            item["symbol"]: item
            for item in research_store().monitoring_status(states)
        }
        items = []
        for item in primary.items if primary is not None else ():
            current = statuses.get(item.symbol, {})
            items.append(
                {
                    "code": item.symbol,
                    "name": item.name,
                    "active": 1 if states.get(item.symbol, True) else 0,
                    "latestDate": current.get("latest_trade_date"),
                    "latestTime": current.get("latest_time"),
                    "lastSyncAt": current.get("last_sync_at"),
                    "lastError": current.get("last_error"),
                    "tradeDayCount": len(research_store().available_dates(item.symbol)),
                }
            )
        return {"ok": True, "items": items}

    @app.post("/api/watchlist")
    async def update_legacy_watchlist(
        payload: dict[str, Any],
        user=Depends(require_ready_csrf),
    ):
        accounts, _sessions = stores()
        raw_codes = payload.get("codes", payload.get("code", []))
        codes = [raw_codes] if isinstance(raw_codes, str) else list(raw_codes or [])
        normalized = [str(code).strip() for code in codes if re.fullmatch(r"[0-9]{6}", str(code).strip())]
        if not normalized:
            raise HTTPException(status_code=422, detail="symbol must be a six-digit stock code")
        action = str(payload.get("action") or "add").lower()
        if action == "add":
            groups = accounts.list_watchlists(user.id)
            primary = next((group for group in groups if group.is_primary), groups[0] if groups else None)
            if primary is None:
                raise HTTPException(status_code=404, detail="watchlist group not found")
            for symbol in normalized:
                try:
                    accounts.add_symbol(user.id, primary.id, resolve_symbol(symbol))
                except ValueError as error:
                    if "already exists" not in str(error):
                        raise HTTPException(status_code=409, detail=str(error)) from None
            if bool(payload.get("backfill", True)):
                for symbol in normalized:
                    snapshot = await market_broker().refresh(symbol, detail=True, max_age_seconds=0)
                    research_store().record_snapshot(snapshot)
        elif action in {"activate", "deactivate"}:
            for symbol in normalized:
                try:
                    accounts.set_monitoring_enabled(user.id, symbol, action == "activate")
                except LookupError:
                    raise HTTPException(status_code=404, detail="watchlist symbol not found") from None
        elif action == "backfill":
            for symbol in normalized:
                snapshot = await market_broker().refresh(symbol, detail=True, max_age_seconds=0)
                research_store().record_snapshot(snapshot)
        else:
            raise HTTPException(status_code=422, detail="unsupported watchlist action")
        return legacy_watchlist(user)

    @app.post("/api/backfill")
    async def legacy_backfill(payload: dict[str, Any], user=Depends(require_ready_csrf)):
        return await update_legacy_watchlist({**payload, "action": "backfill"}, user)

    @app.post("/api/backfill-all")
    async def legacy_backfill_all(user=Depends(require_ready_csrf)):
        accounts, _sessions = stores()
        symbols = [symbol for symbol, enabled in accounts.monitoring_state(user.id).items() if enabled]
        results = []
        for symbol in symbols:
            try:
                snapshot = await market_broker().refresh(symbol, detail=True, max_age_seconds=0)
                changed = research_store().record_snapshot(snapshot)
                results.append({"code": symbol, "ok": True, "changedMinutes": changed})
            except Exception as error:
                results.append({"code": symbol, "ok": False, "error": fixed_market_error_code(error)})
        return {**legacy_watchlist(user), "results": results}

    @app.get("/api/series")
    def legacy_series(
        code: str,
        date: str | None = None,
        user=Depends(require_ready_user),
    ):
        if code not in stores()[0].monitoring_state(user.id):
            raise HTTPException(status_code=404, detail="watchlist symbol not found")
        try:
            series = research_store().read_series(code, date)
        except LookupError:
            raise HTTPException(status_code=404, detail="research history not found") from None
        points = [
            {
                "time": point["time"],
                "price": point["price"],
                "avg_price": point["average_price"],
                "volume_shares": point["volume"],
                "diff": point["diff"],
                "dea": point["dea"],
                "macd": point["macd"],
                "big_net_wan": point["large_order_net"],
                "mid_net_wan": None,
                "small_net_wan": point["retail_count"],
            }
            for point in series["points"]
        ]
        latest = points[-1]
        return {
            "ok": True,
            "code": code,
            "name": series["name"],
            "date": series["trade_date"],
            "requestedDate": date or series["trade_date"],
            "preClose": series["pre_close"],
            "latest": latest,
            "points": points,
            "meta": {
                "source": series["source"],
                "updatedAt": series["collected_at"],
                "availableDates": series["available_dates"],
                "tradeDayCount": len(series["available_dates"]),
            },
        }

    @app.get("/api/push-rules")
    def legacy_push_rules(_user=Depends(require_ready_user)):
        return {"ok": True, "rules": research_store().push_config(public=True).get("rules", [])}

    @app.get("/api/macd-settings")
    def legacy_macd_settings(_user=Depends(require_ready_user)):
        settings = research_store().macd_settings()
        return {
            "ok": True,
            "settings": {
                "short": settings["short"],
                "long": settings["long"],
                "signal": settings["signal"],
                "markerThreshold": settings["marker_threshold"],
            },
        }

    @app.get("/api/push-config")
    def legacy_push_config(_admin=Depends(require_admin)):
        return {"ok": True, "config": research_store().push_config(public=True)}

    @app.post("/api/push-config")
    def update_legacy_push_config(
        payload: dict[str, Any],
        _admin=Depends(require_admin_csrf),
    ):
        config = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        return {"ok": True, "config": research_store().save_push_config(config)}

    @app.post("/api/macd-settings")
    def update_legacy_macd_settings(
        payload: dict[str, Any],
        _admin=Depends(require_admin_csrf),
    ):
        settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
        saved = research_store().save_macd_settings(
            {
                "short": settings.get("short"),
                "long": settings.get("long"),
                "signal": settings.get("signal"),
                "marker_threshold": settings.get("markerThreshold", settings.get("marker_threshold", 0)),
            }
        )
        return {"ok": True, "settings": {**saved, "markerThreshold": saved["marker_threshold"]}}

    @app.post("/api/push-test")
    def legacy_push_test(_admin=Depends(require_admin_csrf)):
        dispatcher = app.state.push_dispatcher
        if dispatcher is None:
            raise HTTPException(status_code=503, detail="push dispatcher is not configured")
        return dispatcher.send(
            research_store().push_config(public=False),
            "THS Market 推送测试",
            "推送配置已连通",
        )

    @app.post("/api/premium-push-test")
    def legacy_premium_push_test(_admin=Depends(require_admin_csrf)):
        return push_admin_premium(_admin)

    def market_broker():
        broker = app.state.market_data_broker
        if broker is None:
            raise HTTPException(status_code=503, detail="market data is not configured")
        return broker

    @app.get("/api/admin/market")
    def admin_market_health(_admin=Depends(require_admin)):
        health = market_broker().stats()
        catalog = getattr(app.state, "symbol_catalog", None)
        status = getattr(catalog, "status", None)
        if callable(status):
            current = status()
            health["symbol_catalog"] = {
                "active_version": current.active_version,
                "count": current.count,
                "activated_at": (
                    current.activated_at.isoformat()
                    if current.activated_at is not None
                    else None
                ),
                "stale": current.stale,
                "checksum": current.checksum,
            }
        return health

    @app.get("/api/v1/market/symbols/{symbol}/snapshot")
    async def market_snapshot(symbol: str, _user=Depends(require_ready_user)):
        normalized = symbol.strip()
        if not re.fullmatch(r"[0-9]{6}", normalized):
            raise HTTPException(status_code=422, detail="symbol must be a six-digit stock code")
        try:
            snapshot = await market_broker().refresh(
                normalized,
                detail=True,
                max_age_seconds=1.5,
            )
        except Exception as error:
            raise HTTPException(
                status_code=503,
                detail=fixed_market_error_code(
                    error,
                    "MARKET_QUOTE_UNAVAILABLE",
                ),
            ) from None
        return snapshot.as_public()

    @app.get("/api/v1/market/symbols/{symbol}/series")
    async def market_series(
        symbol: str,
        period: str,
        cursor: str | None = None,
        limit: int = 120,
        _user=Depends(require_ready_user),
    ):
        normalized = symbol.strip()
        if not re.fullmatch(r"[0-9]{6}", normalized):
            raise HTTPException(status_code=422, detail="symbol must be a six-digit stock code")
        if period not in MARKET_PERIODS:
            raise HTTPException(
                status_code=422,
                detail="unsupported market series period",
            )
        if not 1 <= limit <= 500:
            raise HTTPException(
                status_code=422,
                detail="series limit must be between 1 and 500",
            )
        try:
            page = await market_broker().series(normalized, period, cursor, limit)
        except Exception as error:
            raise HTTPException(
                status_code=503,
                detail=fixed_market_error_code(
                    error,
                    "MARKET_SERIES_UNAVAILABLE",
                ),
            ) from None
        return page.as_public()

    @app.get("/api/v1/market/symbols/{symbol}/fund-flow/history")
    def fund_flow_history(
        symbol: str,
        trade_date: str | None = None,
        user=Depends(require_ready_user),
    ):
        normalized = symbol.strip()
        if not re.fullmatch(r"[0-9]{6}", normalized):
            raise HTTPException(status_code=422, detail="symbol must be a six-digit stock code")
        if trade_date is not None and not re.fullmatch(r"[0-9]{8}", trade_date.strip()):
            raise HTTPException(status_code=422, detail="trade_date must be YYYYMMDD")
        accounts, _sessions = stores()
        watchlist_item = next(
            (
                item
                for group in accounts.list_watchlists(user.id)
                for item in group.items
                if item.symbol == normalized
            ),
            None,
        )
        if watchlist_item is None:
            raise HTTPException(status_code=404, detail="watchlist symbol not found")
        store = app.state.research_store
        if store is None:
            raise HTTPException(status_code=503, detail="research monitoring is not configured")
        try:
            history = store.read_fund_flow_history(normalized, trade_date.strip() if trade_date else None)
        except LookupError:
            raise HTTPException(status_code=404, detail="fund flow history not found") from None
        return {**history, "name": watchlist_item.name}

    @app.get("/api/v1/market/symbols/{symbol}/fund-flow/daily")
    def fund_flow_daily(
        symbol: str,
        limit: int = 30,
        user=Depends(require_ready_user),
    ):
        normalized = symbol.strip()
        if not re.fullmatch(r"[0-9]{6}", normalized):
            raise HTTPException(status_code=422, detail="symbol must be a six-digit stock code")
        if not 1 <= limit <= 60:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 60")
        accounts, _sessions = stores()
        watchlist_item = next(
            (
                item
                for group in accounts.list_watchlists(user.id)
                for item in group.items
                if item.symbol == normalized
            ),
            None,
        )
        if watchlist_item is None:
            raise HTTPException(status_code=404, detail="watchlist symbol not found")
        store = app.state.research_store
        if store is None:
            raise HTTPException(status_code=503, detail="research monitoring is not configured")
        return {
            **store.read_fund_flow_daily(normalized, limit, previous_trading_date),
            "name": watchlist_item.name,
        }

    def validated_subscription(value: object) -> tuple[set[str], set[str]] | None:
        if not isinstance(value, dict) or value.get("type") != "subscribe":
            return None
        raw_watchlist = value.get("watchlist", [])
        raw_detail = value.get("detail")
        if not isinstance(raw_watchlist, list) or len(raw_watchlist) > 50:
            return None
        watchlist = {str(symbol) for symbol in raw_watchlist}
        detail = set() if raw_detail is None else {str(raw_detail)}
        if any(not re.fullmatch(r"[0-9]{6}", symbol) for symbol in watchlist | detail):
            return None
        return watchlist, detail

    @app.websocket("/api/v1/market/stream")
    async def market_stream(websocket: WebSocket) -> None:
        if websocket_identity(websocket) is None or app.state.market_data_broker is None:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        broker = app.state.market_data_broker
        client_id = secrets.token_urlsafe(18)
        try:
            while True:
                if websocket_identity(websocket) is None:
                    await websocket.close(code=1008)
                    return
                receive = asyncio.create_task(websocket.receive_json())
                event = asyncio.create_task(broker.next_event(client_id)) if broker.has_subscriber(client_id) else None
                tasks = {receive} if event is None else {receive, event}
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if receive in done:
                    raw = receive.result()
                    subscription = validated_subscription(raw)
                    if subscription is None:
                        await websocket.close(code=1003)
                        return
                    watchlist, detail = subscription
                    await websocket.send_json(
                        {
                            "type": "subscribed",
                            "watchlist": sorted(watchlist),
                            "detail": next(iter(detail), None),
                        }
                    )
                    detail_refresh = broker.subscribe(
                        client_id,
                        watchlist_symbols=watchlist,
                        detail_symbols=detail,
                    )
                    for detail_symbol in detail_refresh:
                        await broker.refresh(detail_symbol, detail=True, max_age_seconds=1.5)
                elif event is not None and event in done:
                    await websocket.send_json(event.result())
        except (WebSocketDisconnect, asyncio.CancelledError):
            return
        finally:
            broker.unsubscribe(client_id)
