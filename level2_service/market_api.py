"""Authenticated market-user and grouped-watchlist HTTP routes."""

from __future__ import annotations

import re
from collections import deque
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable

import asyncio
import secrets

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .market_accounts import DuplicateUserError, MarketUser, WatchlistGroup, WatchlistItem
from .parsed_values import SymbolLookup


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


class WatchlistItemResponse(BaseModel):
    symbol: str
    name: str
    market: str


class WatchlistGroupResponse(BaseModel):
    id: int
    name: str
    sort_order: int
    items: list[WatchlistItemResponse]


class WatchlistsResponse(BaseModel):
    groups: list[WatchlistGroupResponse]


def _user_response(user: MarketUser) -> MarketUserResponse:
    return MarketUserResponse.model_validate(user, from_attributes=True)


def _item_response(item: WatchlistItem) -> WatchlistItemResponse:
    return WatchlistItemResponse.model_validate(item, from_attributes=True)


def _group_response(group: WatchlistGroup) -> WatchlistGroupResponse:
    return WatchlistGroupResponse(
        id=group.id,
        name=group.name,
        sort_order=group.sort_order,
        items=[_item_response(item) for item in group.items],
    )


def install_market_routes(
    app: FastAPI,
    *,
    require_admin: Callable[..., Any],
    require_admin_csrf: Callable[..., Any],
    resolve_symbol: Callable[[str], SymbolLookup],
    secure_cookies: bool,
) -> None:
    """Install routes after the parent app has constructed its shared dependencies."""

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
        return WatchlistsResponse(groups=[_group_response(group) for group in accounts.list_watchlists(user.id)])

    @app.post("/api/v1/watchlists/groups", status_code=201, response_model=WatchlistGroupResponse)
    def create_watchlist_group(
        payload: GroupRequest,
        user=Depends(require_ready_csrf),
    ) -> WatchlistGroupResponse:
        accounts, _sessions = stores()
        try:
            return _group_response(accounts.create_group(user.id, payload.name))
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
            return _group_response(accounts.rename_group(user.id, group_id, payload.name))
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
            return _item_response(accounts.add_symbol(user.id, group_id, lookup))
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

    def market_broker():
        broker = app.state.market_data_broker
        if broker is None:
            raise HTTPException(status_code=503, detail="market data is not configured")
        return broker

    @app.get("/api/admin/market")
    def admin_market_health(_admin=Depends(require_admin)):
        return market_broker().stats()

    @app.get("/api/v1/market/symbols/{symbol}/snapshot")
    async def market_snapshot(symbol: str, _user=Depends(require_ready_user)):
        normalized = symbol.strip()
        if not re.fullmatch(r"[0-9]{6}", normalized):
            raise HTTPException(status_code=422, detail="symbol must be a six-digit stock code")
        resolve_symbol(normalized)
        try:
            snapshot = await market_broker().refresh(
                normalized,
                detail=True,
                max_age_seconds=1.5,
            )
        except Exception as error:
            error_code = getattr(error, "error_code", None) or str(error)
            raise HTTPException(status_code=503, detail=error_code) from None
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
        resolve_symbol(normalized)
        try:
            page = await market_broker().series(normalized, period, cursor, limit)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except Exception as error:
            error_code = getattr(error, "error_code", None) or str(error)
            raise HTTPException(status_code=503, detail=error_code) from None
        return page.as_public()

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
                    broker.subscribe(
                        client_id,
                        watchlist_symbols=watchlist,
                        detail_symbols=detail,
                    )
                    for detail_symbol in detail:
                        await broker.refresh(detail_symbol, detail=True, max_age_seconds=1.5)
                elif event is not None and event in done:
                    await websocket.send_json(event.result())
        except (WebSocketDisconnect, asyncio.CancelledError):
            return
        finally:
            broker.unsubscribe(client_id)
