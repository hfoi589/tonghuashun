"""Public task submission and status API."""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from threading import RLock
from asyncio import FIRST_COMPLETED, CancelledError, Event, TimeoutError, create_task, gather, get_running_loop, sleep, to_thread, wait, wait_for
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import AsyncIterator, Callable, Mapping, Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .app_sessions import (
    ACCOUNT_ROLES,
    AccountSessionBundle,
    SessionProvider,
)
from .device_lifecycle import (
    DeviceLifecycleAction,
    DeviceLifecycleClient,
    DeviceLifecycleError,
    DeviceLifecycleState,
)
from .models import CaptureKind, CaptureStatus, TaskRecord, TaskStatus, ValueSource, utc_now
from .market_api import install_market_routes
from .market_data import is_china_market_open
from .parsed_values import (
    DirectRequestError,
    SymbolLookup,
    SymbolLookupAmbiguousError,
    SymbolLookupNotFoundError,
    UnsupportedMarketError,
    market_code_for_symbol,
    sanitized_direct_error_code,
)
from .queue import InMemoryStreams, QueueFullError, TaskStore
from .runner import (
    ADBDeviceBridge,
    DeviceBridge,
    Level2Runner,
    RunnerControl,
    RunnerMaintenanceError,
    jpeg_base64,
)
from .security import AdminSessionManager, persist_password_hash
from .symbol_cache import SymbolLookupCache
from .request_logs import RequestLogStore
from .workday_calendar import ChinaLegalWorkdayCalendar, default_china_workday_calendar


logger = logging.getLogger(__name__)

def detect_device_type(user_agent: str | None) -> str:
    ua = (user_agent or "").lower()
    if "iphone" in ua: return "iPhone"
    if "ipad" in ua: return "iPad"
    if "huawei" in ua or "honor" in ua: return "华为/荣耀手机"
    if "samsung" in ua: return "三星手机"
    if "xiaomi" in ua or "miui" in ua: return "小米手机"
    if "oppo" in ua: return "OPPO 手机"
    if "vivo" in ua: return "vivo 手机"
    if "android" in ua: return "Android 手机"
    if "macintosh" in ua or "mac os" in ua: return "Mac 电脑"
    if "windows" in ua: return "Windows 电脑"
    if "linux" in ua: return "Linux 电脑"
    return "其他设备"



class AdminLogsResponse(BaseModel):
    items: list[dict]
    total: int
    limit: int
    offset: int


def _request_log_stock_name(app: FastAPI, symbol: str | None) -> str | None:
    if not symbol:
        return None
    try:
        expected_market = market_code_for_symbol(symbol)
    except UnsupportedMarketError:
        return None

    catalog = getattr(app.state, "symbol_catalog", None)
    # Logs may show an exact identity from the active local snapshot while a
    # stale catalog blocks new confirmations; public lookup remains strict.
    lookups = (
        getattr(catalog, "lookup", None),
        getattr(catalog, "lookup_existing", None),
    )
    for lookup in lookups:
        if not callable(lookup):
            continue
        try:
            identity = lookup(symbol)
        except Exception:
            continue
        name = getattr(identity, "name", None)
        if (
            getattr(identity, "symbol", None) == symbol
            and getattr(identity, "market", None) == expected_market
            and isinstance(name, str)
            and name.strip()
        ):
            return name.strip()
    return None


def _request_log_error_detail(response: Response) -> str | None:
    """Extract only the bounded JSON error detail for request diagnostics."""
    body = getattr(response, "body", None)
    if not isinstance(body, (bytes, bytearray)):
        return None
    try:
        payload = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if not isinstance(detail, str):
        return None
    cleaned = " ".join(detail.split())
    return cleaned[:256] or None


async def _refresh_symbol_catalog(catalog: object) -> None:
    refresh = getattr(catalog, "refresh", None)
    if not callable(refresh):
        return
    try:
        await to_thread(refresh)
    except Exception as error:
        code = getattr(error, "error_code", None)
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]{1,80}", code):
            code = type(error).__name__
        logger.warning("symbol catalog refresh failed (%s)", code)

class RunnerWake:
    """Thread-safe wake signal for the lifespan-owned runner loop."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._loop = None
        self._event: Event | None = None

    def bind(self) -> None:
        with self._lock:
            self._loop = get_running_loop()
            self._event = Event()

    def clear(self) -> None:
        with self._lock:
            event = self._event
        if event is not None:
            event.clear()

    def notify(self) -> None:
        with self._lock:
            loop = self._loop
            event = self._event
        if loop is None or event is None:
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass

    async def wait(self, timeout: float) -> None:
        with self._lock:
            event = self._event
        if event is None:
            raise RuntimeError("runner wake is not bound")
        await wait_for(event.wait(), timeout=timeout)


class SubmitTask(BaseModel):
    symbol: str
    include_long_capture: bool = True

    @field_validator("symbol")
    @classmethod
    def normalize_app_search_symbol(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[0-9]{6}", normalized):
            raise ValueError("symbol must be a six-digit stock code")
        return normalized

    @model_validator(mode="after")
    def validate_direct_request_market(self) -> "SubmitTask":
        try:
            market_code_for_symbol(self.symbol)
        except UnsupportedMarketError as error:
            raise ValueError(str(error)) from error
        return self


class LoginRequest(BaseModel):
    password: str = Field(min_length=1)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)
    new_password_confirmation: str = Field(min_length=1)


class CaptureResponse(BaseModel):
    kind: CaptureKind
    status: CaptureStatus
    url: Optional[str]
    expires_at: Optional[datetime]


class MainFundFlowPeriodResponse(BaseModel):
    unit: Optional[str]
    main_net_inflow: Optional[str]
    main_visible_inflow: Optional[str]
    main_hidden_inflow: Optional[str]
    retail_inflow: Optional[str]


class MainFundFlowResponse(BaseModel):
    today: MainFundFlowPeriodResponse
    three_day: MainFundFlowPeriodResponse
    five_day: MainFundFlowPeriodResponse


class MainFundFlowPeriodSourcesResponse(BaseModel):
    main_net_inflow: Optional[ValueSource]
    main_visible_inflow: Optional[ValueSource]
    main_hidden_inflow: Optional[ValueSource]
    retail_inflow: Optional[ValueSource]


class MainFundFlowSourcesResponse(BaseModel):
    today: MainFundFlowPeriodSourcesResponse
    three_day: MainFundFlowPeriodSourcesResponse
    five_day: MainFundFlowPeriodSourcesResponse


class IntradayPointResponse(BaseModel):
    time: str
    value: Optional[str]


class IntradayMetricSeriesResponse(BaseModel):
    unit: Optional[str]
    points: list[IntradayPointResponse]


class IntradaySeriesResponse(BaseModel):
    large_order_net: IntradayMetricSeriesResponse
    large_order_amount: IntradayMetricSeriesResponse
    retail_count: IntradayMetricSeriesResponse


class IntradaySeriesSourcesResponse(BaseModel):
    large_order_net: Optional[ValueSource]
    large_order_amount: Optional[ValueSource]
    retail_count: Optional[ValueSource]


class TaskValuesResponse(BaseModel):
    stock_name: Optional[str]
    current_price: Optional[str]
    change_percent: Optional[str]
    turnover_rate: Optional[str]
    large_order_net: Optional[str]
    large_order_amount: Optional[str]
    retail_count: Optional[str]
    macdfs: Optional[str]
    intraday_series: IntradaySeriesResponse
    main_fund_flow: MainFundFlowResponse


class TaskValueSourcesResponse(BaseModel):
    stock_name: Optional[ValueSource]
    current_price: Optional[ValueSource]
    change_percent: Optional[ValueSource]
    turnover_rate: Optional[ValueSource]
    large_order_net: Optional[ValueSource]
    large_order_amount: Optional[ValueSource]
    retail_count: Optional[ValueSource]
    macdfs: Optional[ValueSource]
    intraday_series: IntradaySeriesSourcesResponse
    main_fund_flow: MainFundFlowSourcesResponse


class LongCaptureResponse(BaseModel):
    status: CaptureStatus
    url: Optional[str]
    expires_at: Optional[datetime]


class SourceErrorsResponse(BaseModel):
    core_metrics: Optional[str]
    main_fund_flow: Optional[str]


class TaskResponse(BaseModel):
    public_id: str
    symbol: str
    include_long_capture: bool
    status: TaskStatus
    error_code: Optional[str]
    source_errors: SourceErrorsResponse
    queue_position: Optional[int]
    created_at: datetime
    collected_at: Optional[datetime]
    captures: list[CaptureResponse]
    values: TaskValuesResponse
    value_sources: TaskValueSourcesResponse
    market_snapshot: Optional[dict] = None
    long_capture: LongCaptureResponse


class DeploymentAcceptanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunnerHealthResponse(BaseModel):
    state: str
    last_heartbeat: Optional[datetime]
    queue_paused: bool


class LockResponse(BaseModel):
    locked: bool


class QueueResponse(BaseModel):
    paused: bool


class DeviceLifecycleActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: DeviceLifecycleAction


class AdminDeviceLifecycleResponse(BaseModel):
    state: str
    operation_id: Optional[str]
    error_code: Optional[str]
    updated_at: Optional[datetime]


class AdminDeviceHealthResponse(BaseModel):
    role: str
    label: str
    adb: str
    app: str
    frida: str
    lifecycle: AdminDeviceLifecycleResponse


class AdminDevicesResponse(BaseModel):
    devices: list[AdminDeviceHealthResponse]


class AccountSessionStatusResponse(BaseModel):
    role: str
    state: str
    updated_at: Optional[datetime]
    error_code: Optional[str]
    expires_at: Optional[date]


class AccountSessionsResponse(BaseModel):
    sessions: list[AccountSessionStatusResponse]


class SymbolLookupResponse(BaseModel):
    symbol: str
    name: str
    market: str


class SymbolSearchResponse(BaseModel):
    symbol: str
    name: str
    market: str
    market_label: Optional[str]


def create_app(
    *,
    store: TaskStore | None = None,
    admin_password_hash: str | None = None,
    capture_root: Path | None = None,
    cleanup_interval_seconds: float = 60.0,
    device_bridge: DeviceBridge | None = None,
    device_bridges: Mapping[str, DeviceBridge] | None = None,
    device_health_probes: Mapping[str, Callable[[], Mapping[str, str]]] | None = None,
    runner_control: RunnerControl | None = None,
    runner: Level2Runner | None = None,
    runner_poll_interval_seconds: float = 1.0,
    admin_session_secret: str | None = None,
    password_persist_path: Path | None = None,
    frontend_root: Path | None = None,
    secure_admin_cookies: bool = True,
    symbol_lookup: Callable[[str], SymbolLookup] | None = None,
    symbol_search: Callable[[str, int], list[SymbolLookup]] | None = None,
    symbol_lookup_cache: SymbolLookupCache | None = None,
    symbol_catalog: object | None = None,
    symbol_catalog_refresh_hour: int = 16,
    symbol_catalog_refresh_minute: int = 20,
    previous_trading_date: Callable[[str], str] | None = None,
    core_prewarmer: Callable[[str | None], None] | None = None,
    core_session_invalidator: Callable[[], None] | None = None,
    managed_resources: tuple[object, ...] = (),
    market_account_store: object | None = None,
    market_session_store: object | None = None,
    market_data_broker: object | None = None,
    research_store: object | None = None,
    research_monitor: object | None = None,
    fund_flow_monitor: object | None = None,
    research_monitor_interval_seconds: float = 25.0,
    push_dispatcher: object | None = None,
    premium_monitor: object | None = None,
    premium_monitor_interval_seconds: float = 15.0,
    account_session_provider: SessionProvider | None = None,
    account_session_refreshers: Mapping[
        str,
        Callable[[str], AccountSessionBundle],
    ]
    | None = None,
    device_lifecycle: DeviceLifecycleClient | None = None,
    request_log_store: RequestLogStore | None = None,
    workday_calendar: ChinaLegalWorkdayCalendar | None = None,
) -> FastAPI:
    """Build an isolated application instance for one service process."""
    if cleanup_interval_seconds <= 0:
        raise ValueError("cleanup_interval_seconds must be positive")
    if runner_poll_interval_seconds <= 0:
        raise ValueError("runner_poll_interval_seconds must be positive")
    if research_monitor_interval_seconds <= 0:
        raise ValueError("research_monitor_interval_seconds must be positive")
    if premium_monitor_interval_seconds <= 0:
        raise ValueError("premium_monitor_interval_seconds must be positive")
    if not 0 <= symbol_catalog_refresh_hour <= 23:
        raise ValueError("symbol_catalog_refresh_hour must be between 0 and 23")
    if not 0 <= symbol_catalog_refresh_minute <= 59:
        raise ValueError(
            "symbol_catalog_refresh_minute must be between 0 and 59"
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = Event()
        app.state.runner_wake.bind()
        for resource in app.state.managed_resources:
            prewarm = getattr(resource, "prewarm", None)
            if callable(prewarm):
                try:
                    prewarm()
                except Exception:
                    pass
        await to_thread(app.state.store.recover_running)
        log_store = app.state.request_log_store
        backfill_names = getattr(log_store, "backfill_stock_names", None)
        if callable(backfill_names):
            try:
                await to_thread(
                    backfill_names,
                    lambda symbol: _request_log_stock_name(app, symbol),
                )
            except Exception as error:
                logger.warning(
                    "request log stock-name backfill failed (%s)",
                    type(error).__name__,
                )
        deduplicate = getattr(app.state.store, "deduplicate_by_symbol", None)
        app.state.task_migration = (
            await to_thread(deduplicate)
            if callable(deduplicate)
            else {"total": 0, "kept": 0, "deleted": 0, "aliases": 0}
        )
        reconcile_active_count = getattr(
            app.state.store,
            "reconcile_active_count",
            None,
        )
        if callable(reconcile_active_count):
            await to_thread(reconcile_active_count)
        logger.info(
            "task migration total=%s kept=%s deleted=%s aliases=%s",
            app.state.task_migration["total"],
            app.state.task_migration["kept"],
            app.state.task_migration["deleted"],
            app.state.task_migration["aliases"],
        )

        async def retention_loop() -> None:
            while not stop.is_set():
                try:
                    await to_thread(app.state.store.cleanup, utc_now())
                except Exception:
                    logger.exception("task retention cleanup failed")
                try:
                    await wait_for(stop.wait(), timeout=cleanup_interval_seconds)
                except TimeoutError:
                    continue

        async def runner_loop() -> None:
            """Run the blocking ADB worker off the API event loop until shutdown."""
            assert runner is not None
            while not stop.is_set():
                app.state.runner_wake.clear()
                claimed = None
                try:
                    claimed = await to_thread(runner.run_once)
                except Exception:
                    # Device failures are surfaced through the authenticated health API.
                    app.state.runner_control.heartbeat("OFFLINE")
                if claimed is not None:
                    continue
                try:
                    await app.state.runner_wake.wait(
                        runner_poll_interval_seconds
                    )
                except TimeoutError:
                    continue

        async def market_loop() -> None:
            while not stop.is_set():
                try:
                    await app.state.market_data_broker.poll_due()
                except Exception:
                    pass
                try:
                    await wait_for(stop.wait(), timeout=0.25)
                except TimeoutError:
                    continue

        async def research_loop() -> None:
            while not stop.is_set():
                try:
                    if app.state.research_monitor is not None:
                        await app.state.research_monitor.poll_once()
                    if app.state.fund_flow_monitor is not None:
                        await app.state.fund_flow_monitor.poll_once()
                except Exception:
                    logger.exception("research monitoring poll failed")
                try:
                    await wait_for(
                        stop.wait(),
                        timeout=research_monitor_interval_seconds,
                    )
                except TimeoutError:
                    continue

        async def premium_loop() -> None:
            while not stop.is_set():
                try:
                    await app.state.premium_monitor.poll_once()
                except Exception:
                    logger.exception("premium monitoring poll failed")
                try:
                    await wait_for(stop.wait(), timeout=premium_monitor_interval_seconds)
                except TimeoutError:
                    continue

        async def symbol_catalog_loop() -> None:
            catalog = app.state.symbol_catalog
            if catalog is None:
                return
            refresh = getattr(catalog, "refresh", None)
            needs_startup_refresh = getattr(
                catalog,
                "startup_refresh_required",
                None,
            )
            if callable(refresh) and (
                not callable(needs_startup_refresh)
                or needs_startup_refresh()
            ):
                await _refresh_symbol_catalog(catalog)
            while not stop.is_set():
                local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
                next_refresh = local_now.replace(
                    hour=symbol_catalog_refresh_hour,
                    minute=symbol_catalog_refresh_minute,
                    second=0,
                    microsecond=0,
                )
                if next_refresh <= local_now:
                    next_refresh += timedelta(days=1)
                delay = max(
                    1.0,
                    (next_refresh - local_now).total_seconds(),
                )
                try:
                    await wait_for(stop.wait(), timeout=delay)
                    continue
                except TimeoutError:
                    pass
                await _refresh_symbol_catalog(catalog)

        app.state.cleanup_stop = stop
        app.state.cleanup_task = create_task(retention_loop())
        app.state.runner_task = create_task(runner_loop()) if runner is not None else None
        app.state.market_task = (
            create_task(market_loop()) if app.state.market_data_broker is not None else None
        )
        app.state.research_task = (
            create_task(research_loop())
            if app.state.research_monitor is not None or app.state.fund_flow_monitor is not None
            else None
        )
        app.state.premium_task = (
            create_task(premium_loop()) if app.state.premium_monitor is not None else None
        )
        app.state.symbol_catalog_task = (
            create_task(symbol_catalog_loop())
            if app.state.symbol_catalog is not None
            else None
        )
        try:
            yield
        finally:
            stop.set()
            app.state.runner_wake.notify()
            await app.state.cleanup_task
            if app.state.runner_task is not None:
                await app.state.runner_task
            if app.state.market_task is not None:
                await app.state.market_task
            if app.state.research_task is not None:
                await app.state.research_task
            if app.state.premium_task is not None:
                await app.state.premium_task
            if app.state.symbol_catalog_task is not None:
                await app.state.symbol_catalog_task
            for resource in reversed(app.state.managed_resources):
                close = getattr(resource, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

    app = FastAPI(title="THS Level2 Capture Service", lifespan=lifespan)

    @app.middleware("http")
    async def request_log_middleware(request: Request, call_next):
        started = time.perf_counter()
        request_payload = {}
        if request.method in {"POST", "PATCH", "PUT"} and (request.url.path == "/api/v1/jobs" or request.url.path.startswith("/api/v1/watchlists")) :
            try:
                request_payload = json.loads((await request.body()).decode("utf-8"))
            except Exception:
                request_payload = {}
        response = await call_next(request)
        if response.status_code >= 400 and not isinstance(getattr(response, "body", None), (bytes, bytearray)):
            body_iterator = getattr(response, "body_iterator", None)
            if body_iterator is not None:
                body = b"".join([
                    chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
                    async for chunk in body_iterator
                ])
                response = Response(
                    content=body,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    background=getattr(response, "background", None),
                )
        path = request.url.path
        log_store = getattr(request.app.state, "request_log_store", None)
        should_log = (
            path.startswith("/api/v1/symbols")
            or (path == "/api/v1/jobs" and request.method == "POST")
            or (path.startswith("/api/v1/jobs/") and request.method != "GET" and "/events" not in path)
            or (path.startswith("/api/v1/market") and request.method == "GET")
            or (path.startswith("/api/v1/watchlists") and request.method in {"POST", "PATCH", "PUT", "DELETE"})
        )
        if log_store is not None and should_log:
            symbol = request_payload.get("symbol") if isinstance(request_payload.get("symbol"), str) else None
            if path == "/api/v1/symbols" and request.query_params.get("query"):
                action = "symbol_search"
            elif path.startswith("/api/v1/symbols/"):
                action = "symbol_lookup"
            elif path == "/api/v1/jobs":
                action = "job_submit"
            elif path.endswith("/retry"):
                action = "job_retry"
            elif path.endswith("/snapshot"):
                action = "market_tab"
            elif path.endswith("/series"):
                action = "market_retry" if request.query_params.get("retry") == "1" else "market_tab"
            elif path.endswith("/fund-flow/history"):
                action = "fund_flow_history"
            elif path.startswith("/api/v1/market"):
                action = "market_query"
            elif "/groups" in path and "/symbols" not in path:
                action = "watchlist_group"
            elif "/symbols" in path:
                action = "watchlist_symbol"
            else:
                action = "query"
            parts = path.split("/")
            for part in parts:
                if len(part) == 6 and part.isdigit(): symbol = part; break
            task_status = None
            public_id = None
            stock_name = None
            if "/jobs/" in path:
                public_id = path.split("/jobs/", 1)[1].split("/", 1)[0]
                task = getattr(request.app.state, "store", None)
                task = task.get(public_id) if task is not None and hasattr(task, "get") else None
                if task is not None:
                    symbol = task.symbol
                    task_status = getattr(task.status, "value", task.status)
                    if action in {"job_submit", "job_retry"}:
                        task_status = "已提交"
            stock_name = _request_log_stock_name(request.app, symbol)
            user_id = None
            user_name = None
            if path.startswith("/api/v1/market") or path.startswith("/api/v1/watchlists"):
                session_store = getattr(request.app.state, "market_session_store", None)
                session = session_store.get(request.cookies.get("ths_market_session")) if session_store is not None else None
                user_id = getattr(session, "user_id", None)
                accounts = getattr(request.app.state, "market_account_store", None)
                user = accounts.get_user(user_id) if accounts is not None and user_id is not None else None
                user_name = getattr(user, "username", None)
            ua = request.headers.get("user-agent")
            device = detect_device_type(ua)
            try:
                from .request_logs import RequestLogEntry
                log_store.record(RequestLogEntry(timestamp=utc_now(), ip=request.client.host if request.client else None, user_id=user_id, user_name=user_name, device_type=device, user_agent=ua, path=path, action=action, symbol=symbol, stock_name=stock_name, status_code=response.status_code, task_status=task_status, error_code=None if response.status_code < 400 else f"HTTP_{response.status_code}", error_detail=_request_log_error_detail(response), duration_ms=(time.perf_counter()-started)*1000, public_id=public_id))
            except Exception:
                logger.exception("request log write failed")
        return response
    app.state.store = store or InMemoryStreams()
    persist = None if password_persist_path is None else lambda value: persist_password_hash(password_persist_path, value)
    app.state.admin_sessions = AdminSessionManager(
        admin_password_hash,
        session_secret=admin_session_secret,
        persist_password_hash=persist,
    )
    app.state.runner_control = runner_control or RunnerControl()
    app.state.runner_wake = RunnerWake()
    primary_bridge = device_bridge or ADBDeviceBridge()
    configured_bridges = dict(device_bridges or {})
    configured_bridges.setdefault("core_metrics", primary_bridge)
    app.state.device_bridges = configured_bridges
    app.state.device_bridge = configured_bridges["core_metrics"]
    app.state.device_health_probes = dict(device_health_probes or {})
    app.state.device_lifecycle = device_lifecycle
    app.state.request_log_store = request_log_store
    app.state.capture_root = (capture_root or Path("captures")).resolve()
    app.state.frontend_root = frontend_root.resolve() if frontend_root is not None else None
    app.state.secure_admin_cookies = secure_admin_cookies
    app.state.symbol_lookup = symbol_lookup
    app.state.symbol_search = symbol_search
    app.state.symbol_verification_enabled = symbol_lookup is not None or symbol_lookup_cache is not None
    app.state.symbol_lookup_cache = symbol_lookup_cache
    app.state.symbol_lookup_cache_lock = RLock()
    app.state.symbol_catalog = symbol_catalog
    app.state.core_prewarmer = core_prewarmer
    app.state.core_session_invalidator = core_session_invalidator
    app.state.managed_resources = tuple(managed_resources)
    app.state.market_account_store = market_account_store
    app.state.market_session_store = market_session_store
    app.state.market_data_broker = market_data_broker
    app.state.research_store = research_store
    app.state.research_monitor = research_monitor
    app.state.fund_flow_monitor = fund_flow_monitor
    app.state.push_dispatcher = push_dispatcher
    app.state.premium_monitor = premium_monitor
    app.state.account_session_provider = account_session_provider
    app.state.workday_calendar = workday_calendar or default_china_workday_calendar()
    app.state.account_session_refreshers = dict(account_session_refreshers or {})
    set_capture_root = getattr(app.state.store, "set_capture_root", None)
    if callable(set_capture_root):
        set_capture_root(app.state.capture_root)

    def require_admin(request: Request):
        session_id = request.cookies.get("ths_admin_session")
        session = app.state.admin_sessions.valid_session(session_id)
        if session is None:
            if session_id:
                app.state.runner_control.disconnect_session(session_id)
            raise HTTPException(status_code=401, detail="admin authentication required")
        return session

    def require_csrf(request: Request, session=Depends(require_admin)):
        if request.headers.get("X-CSRF-Token") != session.csrf_token:
            raise HTTPException(status_code=403, detail="CSRF token required")
        return session

    def task_response(task: TaskRecord) -> TaskResponse:
        public = task.as_public()
        positioner = getattr(app.state.store, "queue_position", None)
        public["queue_position"] = positioner(task.task_id) if callable(positioner) else None
        return TaskResponse.model_validate(public)

    def notify_runner_if_queued(task: TaskRecord) -> None:
        if task.status == TaskStatus.QUEUED:
            app.state.runner_wake.notify()

    def trigger_core_prewarm(symbol: str | None) -> None:
        prewarm = app.state.core_prewarmer
        if not callable(prewarm):
            return
        try:
            prewarm(symbol)
        except Exception:
            pass

    def resolve_symbol(symbol: str) -> SymbolLookup:
        try:
            expected_market = market_code_for_symbol(symbol)
        except UnsupportedMarketError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        try:
            cache = app.state.symbol_lookup_cache
            cached = None
            if cache is not None:
                with app.state.symbol_lookup_cache_lock:
                    cached = cache.get(symbol)
                if cached is not None:
                    if cached.symbol != symbol or cached.market != expected_market:
                        raise DirectRequestError(
                            "SYMBOL_LOOKUP_INVALID",
                            "cached symbol lookup returned a mismatched stock",
                        )
                    trigger_core_prewarm(symbol)
                    return cached
            lookup = app.state.symbol_lookup
            if lookup is None:
                raise HTTPException(
                    status_code=503,
                    detail="symbol lookup temporarily unavailable",
                )
            result = lookup(symbol)
            if result.symbol != symbol or result.market != expected_market:
                raise DirectRequestError(
                    "SYMBOL_LOOKUP_INVALID",
                    "symbol lookup returned a mismatched stock",
                )
            if cache is not None:
                with app.state.symbol_lookup_cache_lock:
                    cache.set(result)
            trigger_core_prewarm(symbol)
            return result
        except SymbolLookupNotFoundError as error:
            raise HTTPException(status_code=404, detail="symbol not found") from error
        except SymbolLookupAmbiguousError as error:
            raise HTTPException(status_code=409, detail="symbol lookup is ambiguous") from error
        except DirectRequestError as error:
            detail = (
                error.error_code
                if error.error_code.startswith("SYMBOL_CATALOG_")
                else "symbol lookup temporarily unavailable"
            )
            raise HTTPException(
                status_code=503,
                detail=detail,
            ) from error
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(
                status_code=503,
                detail="symbol lookup temporarily unavailable",
            ) from error

    @app.get("/api/v1/symbols", response_model=list[SymbolSearchResponse])
    def search_public_symbols(
        query: str = Query(min_length=2, max_length=32),
        limit: int = Query(default=8, ge=1, le=8),
    ) -> list[SymbolSearchResponse]:
        normalized = query.strip()
        if not 2 <= len(normalized) <= 32:
            raise HTTPException(
                status_code=422,
                detail="query must contain 2 to 32 characters",
            )
        search = app.state.symbol_search
        if search is None:
            raise HTTPException(
                status_code=503,
                detail="symbol search temporarily unavailable",
            )
        try:
            return [
                SymbolSearchResponse(
                    symbol=result.symbol,
                    name=result.name,
                    market=result.market,
                    market_label=result.market_label,
                )
                for result in search(normalized, limit)
            ]
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except DirectRequestError as error:
            detail = (
                error.error_code
                if error.error_code.startswith("SYMBOL_CATALOG_")
                else "symbol search temporarily unavailable"
            )
            raise HTTPException(
                status_code=503,
                detail=detail,
            ) from error
        except Exception as error:
            raise HTTPException(
                status_code=503,
                detail="symbol search temporarily unavailable",
            ) from error

    @app.get("/api/v1/symbols/{symbol}", response_model=SymbolLookupResponse)
    def lookup_public_symbol(symbol: str) -> SymbolLookupResponse:
        normalized = symbol.strip()
        if not re.fullmatch(r"[0-9]{6}", normalized):
            raise HTTPException(status_code=422, detail="symbol must be a six-digit stock code")
        result = resolve_symbol(normalized)
        return SymbolLookupResponse(symbol=result.symbol, name=result.name, market=result.market)

    @app.post("/api/v1/jobs", status_code=202, response_model=TaskResponse)
    def submit_task(payload: SubmitTask) -> TaskResponse:
        if app.state.symbol_verification_enabled:
            resolve_symbol(payload.symbol)
        task = TaskRecord(
            task_id=secrets.token_urlsafe(24),
            symbol=payload.symbol,
            include_long_capture=payload.include_long_capture,
        )
        try:
            submitted = app.state.store.submit_or_refresh(task)
        except QueueFullError:
            raise HTTPException(status_code=429, detail="queue is full") from None
        notify_runner_if_queued(submitted)
        return task_response(submitted)

    @app.post(
        "/internal/deployment/acceptance",
        status_code=202,
        response_model=TaskResponse,
    )
    def bind_deployment_acceptance(
        _payload: DeploymentAcceptanceRequest,
        request: Request,
    ) -> TaskResponse:
        authorization = request.headers.get("Authorization")
        if not isinstance(authorization, str) or not authorization.startswith(
            "Bearer "
        ):
            raise HTTPException(
                status_code=401,
                detail="DEPLOYMENT_LEASE_AUTH_REQUIRED",
            )
        owner_token = authorization.removeprefix("Bearer ")
        task = TaskRecord(
            task_id=secrets.token_urlsafe(24),
            symbol="601872",
            include_long_capture=False,
        )
        try:
            bound = app.state.store.bind_deployment_acceptance(
                owner_token, task
            )
        except Exception:
            bound = None
        if bound is None:
            raise HTTPException(
                status_code=409,
                detail="DEPLOYMENT_LEASE_INVALID",
            )
        notify_runner_if_queued(bound)
        return task_response(bound)

    @app.get("/api/v1/jobs/{public_id}", response_model=TaskResponse)
    def get_task(public_id: str) -> TaskResponse:
        task = app.state.store.get(public_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task_response(task)

    @app.get("/api/v1/jobs/{public_id}/fund-flow-history")
    def get_task_fund_flow_history(public_id: str):
        task = app.state.store.get(public_id)
        market_open = is_china_market_open()
        can_use_closed_timeout_history = (
            task is not None
            and task.status == TaskStatus.FAILED
            and task.error_code == "DIRECT_PROTOCOL_RESPONSE_TIMEOUT"
            and not market_open
        )
        if task is None or (
            task.status not in {
                TaskStatus.COMPLETED,
                TaskStatus.PARTIAL,
                TaskStatus.MARKET_SNAPSHOT,
            }
            and not can_use_closed_timeout_history
        ):
            raise HTTPException(status_code=404, detail="fund flow history not found")
        accounts = app.state.market_account_store
        research_store = app.state.research_store
        if accounts is None or research_store is None:
            raise HTTPException(status_code=404, detail="fund flow history not found")
        list_symbols = getattr(accounts, "list_watchlist_symbols", None)
        if not callable(list_symbols) or task.symbol not in set(list_symbols()):
            raise HTTPException(status_code=404, detail="fund flow history not found")
        trade_date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
        try:
            history = research_store.read_fund_flow_history(task.symbol, trade_date if market_open else None)
        except LookupError:
            raise HTTPException(status_code=404, detail="fund flow history not found") from None
        points = history["periods"]["today"]["points"]
        if not points and not market_open:
            for previous_date in history.get("available_dates", []):
                if previous_date == history.get("trade_date"):
                    continue
                try:
                    previous_history = research_store.read_fund_flow_history(task.symbol, previous_date)
                except LookupError:
                    continue
                previous_points = previous_history["periods"]["today"]["points"]
                if previous_points:
                    history = previous_history
                    points = previous_points
                    break
        if not points:
            raise HTTPException(status_code=404, detail="fund flow history not found")
        return {
            "symbol": task.symbol,
            "trade_date": history["trade_date"],
            "period": "today",
            "points": points,
        }

    @app.get("/api/v1/jobs/{public_id}/fund-flow-daily")
    def get_task_fund_flow_daily(public_id: str, limit: int = Query(30, ge=1, le=60)):
        task = app.state.store.get(public_id)
        market_open = is_china_market_open()
        can_use_closed_timeout_history = (
            task is not None
            and task.status == TaskStatus.FAILED
            and task.error_code == "DIRECT_PROTOCOL_RESPONSE_TIMEOUT"
            and not market_open
        )
        if task is None or (
            task.status not in {
                TaskStatus.COMPLETED,
                TaskStatus.PARTIAL,
                TaskStatus.MARKET_SNAPSHOT,
            }
            and not can_use_closed_timeout_history
        ):
            raise HTTPException(status_code=404, detail="fund flow daily not found")
        accounts = app.state.market_account_store
        research_store = app.state.research_store
        if accounts is None or research_store is None:
            raise HTTPException(status_code=404, detail="fund flow daily not found")
        list_symbols = getattr(accounts, "list_watchlist_symbols", None)
        if not callable(list_symbols) or task.symbol not in set(list_symbols()):
            raise HTTPException(status_code=404, detail="fund flow daily not found")
        try:
            daily = research_store.read_fund_flow_daily(
                task.symbol,
                limit,
                previous_trading_date,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return {
            **daily,
            "name": task.values.get("stock_name") or task.symbol,
        }

    @app.post("/api/v1/jobs/{public_id}/retry", status_code=202, response_model=TaskResponse)
    def retry_public_job(public_id: str) -> TaskResponse:
        task = app.state.store.get(public_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        try:
            retried = app.state.store.refresh_task(task.task_id)
        except QueueFullError:
            raise HTTPException(status_code=429, detail="queue is full") from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        notify_runner_if_queued(retried)
        return task_response(retried)

    @app.get("/api/v1/jobs/{public_id}/captures/{kind}")
    def get_capture(public_id: str, kind: CaptureKind):
        task = app.state.store.get(public_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        capture = task.captures[kind]
        if capture.status.value == "EXPIRED":
            raise HTTPException(status_code=410, detail="capture has expired")
        if capture.status.value != "READY" or capture.path is None:
            raise HTTPException(status_code=404, detail="capture is not available")
        path = capture.path.resolve()
        try:
            path.relative_to(app.state.capture_root)
        except ValueError:
            raise HTTPException(status_code=404, detail="capture is not available") from None
        if not path.is_file():
            raise HTTPException(status_code=404, detail="capture is not available")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/v1/jobs/{public_id}/capture")
    def get_long_capture(public_id: str):
        task = app.state.store.get(public_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        capture = task.long_capture
        if capture.status == CaptureStatus.EXPIRED:
            raise HTTPException(status_code=410, detail="capture has expired")
        if capture.status != CaptureStatus.READY or capture.path is None:
            raise HTTPException(status_code=404, detail="capture is not available")
        path = capture.path.resolve()
        try:
            path.relative_to(app.state.capture_root)
        except ValueError:
            raise HTTPException(status_code=404, detail="capture is not available") from None
        if not path.is_file():
            raise HTTPException(status_code=404, detail="capture is not available")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/v1/jobs/{public_id}/events")
    async def task_events(public_id: str, request: Request, after: int = 0, once: bool = False):
        task = await to_thread(app.state.store.get, public_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        canonical_id = task.task_id

        async def event_stream() -> AsyncIterator[str]:
            cursor_reader = getattr(app.state.store, "events_after_cursor", None)
            if callable(cursor_reader):
                raw_cursor = request.headers.get("last-event-id")
                cursor: str | None = (
                    raw_cursor
                    if raw_cursor is not None
                    and re.fullmatch(r"[0-9]+(?:-[0-9]+)?", raw_cursor)
                    else None
                )
                while not await request.is_disconnected():
                    events, cursor = await to_thread(
                        cursor_reader,
                        canonical_id,
                        cursor,
                    )
                    for event in events:
                        payload = json.dumps(
                            {"public_id": canonical_id, "status": event["data"]}
                        )
                        yield f"id: {event['id']}\nevent: {event['event']}\ndata: {payload}\n\n"
                    if once:
                        return
                    if not events:
                        yield ": keepalive\n\n"
                    await sleep(1)
                return

            event_index = after
            while not await request.is_disconnected():
                events = await to_thread(app.state.store.events_after, canonical_id, event_index)
                for event in events:
                    payload = json.dumps({"public_id": canonical_id, "status": event["data"]})
                    yield f"event: {event['event']}\ndata: {payload}\n\n"
                event_index += len(events)
                if once:
                    return
                if not events:
                    yield ": keepalive\n\n"
                await sleep(1)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/admin/session", status_code=204)
    def admin_login(payload: LoginRequest, request: Request, response: Response) -> None:
        sessions = app.state.admin_sessions
        if not sessions.configured:
            raise HTTPException(status_code=503, detail="admin login is not configured")
        identifier = request.client.host if request.client else "unknown"
        if not sessions.login_allowed(identifier):
            raise HTTPException(
                status_code=429,
                detail="too many failed admin logins",
                headers={"Retry-After": "60"},
            )
        session = sessions.authenticate(payload.password)
        if session is None:
            sessions.record_login_failure(identifier)
            raise HTTPException(status_code=401, detail="invalid credentials")
        sessions.record_login_success(identifier)
        response.set_cookie("ths_admin_session", session.session_id, httponly=True, samesite="strict", secure=secure_admin_cookies)
        response.set_cookie("ths_csrf", session.csrf_token, httponly=False, samesite="strict", secure=secure_admin_cookies)

    @app.get("/api/admin/session", status_code=204)
    def admin_session_probe(_session=Depends(require_admin)) -> None:
        """Confirm that the browser's secure administrator cookie is still valid."""

    @app.post("/api/admin/password", status_code=204)
    def admin_change_password(payload: PasswordChangeRequest, session=Depends(require_csrf)) -> None:
        if payload.new_password != payload.new_password_confirmation:
            raise HTTPException(status_code=422, detail="new passwords do not match")
        if not app.state.admin_sessions.change_password(payload.current_password, payload.new_password):
            raise HTTPException(status_code=401, detail="invalid current password")
        app.state.runner_control.disconnect_session(session.session_id)

    @app.post("/api/admin/session/logout", status_code=204)
    def admin_logout(response: Response, session=Depends(require_csrf)) -> None:
        app.state.admin_sessions.revoke(session.session_id)
        app.state.runner_control.disconnect_session(session.session_id)
        response.delete_cookie("ths_admin_session", httponly=True, samesite="strict", secure=secure_admin_cookies)
        response.delete_cookie("ths_csrf", httponly=False, samesite="strict", secure=secure_admin_cookies)

    @app.get("/api/admin/logs", response_model=AdminLogsResponse)
    def admin_logs(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), symbol: str | None = None, status: str | None = None, ip: str | None = None, action: str | None = None, stock_name: str | None = None, device_type: str | None = None, user_agent: str | None = None, user_name: str | None = None, error_code: str | None = None, public_id: str | None = None, user_id: str | None = None, from_date: str | None = Query(None, alias="from"), to_date: str | None = Query(None, alias="to"), _session=Depends(require_admin)) -> AdminLogsResponse:
        store = app.state.request_log_store
        if store is None:
            return AdminLogsResponse(items=[], total=0, limit=limit, offset=offset)
        items, total = store.list(limit, offset, {"symbol": symbol, "status": status, "ip": ip, "action": action, "stock_name": stock_name, "user_name": user_name, "device_type": device_type, "user_agent": user_agent, "error_code": error_code, "public_id": public_id, "user_id": user_id, "from": from_date, "to": to_date})
        return AdminLogsResponse(items=[item.__dict__ for item in items], total=total, limit=limit, offset=offset)

    @app.get("/api/admin/runner")
    def runner_health(_session=Depends(require_admin)) -> RunnerHealthResponse:
        return RunnerHealthResponse.model_validate(app.state.runner_control.health())

    def public_account_session_status(role: str) -> AccountSessionStatusResponse:
        provider = app.state.account_session_provider
        if provider is None:
            raise HTTPException(
                status_code=503,
                detail="account session storage unavailable",
            )
        status = provider.status(role)
        public = status.as_public()
        expiry = (
            app.state.workday_calendar.expiry_date(status.updated_at)
            if role == "main_fund_flow"
            else None
        )
        public["expires_at"] = expiry.isoformat() if expiry is not None else None
        return AccountSessionStatusResponse.model_validate(public)

    @app.get(
        "/api/admin/account-sessions",
        response_model=AccountSessionsResponse,
    )
    def account_session_statuses(
        _session=Depends(require_admin),
    ) -> AccountSessionsResponse:
        return AccountSessionsResponse(
            sessions=[public_account_session_status(role) for role in ACCOUNT_ROLES]
        )

    @app.post(
        "/api/admin/account-sessions/{role}/refresh",
        response_model=AccountSessionStatusResponse,
    )
    def refresh_account_session(
        role: str,
        _session=Depends(require_csrf),
    ) -> AccountSessionStatusResponse:
        if role not in ACCOUNT_ROLES:
            raise HTTPException(status_code=404, detail="account role not found")
        provider = app.state.account_session_provider
        refresher = app.state.account_session_refreshers.get(role)
        if provider is None or refresher is None:
            raise HTTPException(
                status_code=503,
                detail="account session refresh unavailable",
            )
        try:
            bundle = refresher(role)
            if bundle.role != role:
                raise DirectRequestError(
                    "DIRECT_SESSION_UNAVAILABLE",
                    "account session refresher returned a mismatched role",
                )
            provider.put(bundle)
            if role == "core_metrics":
                invalidator = app.state.core_session_invalidator
                if callable(invalidator):
                    invalidator()
                trigger_core_prewarm(None)
        except DirectRequestError as error:
            provider.mark_error(
                role,
                sanitized_direct_error_code(
                    error.error_code, "DIRECT_SESSION_UNAVAILABLE"
                ),
            )
            raise HTTPException(
                status_code=503,
                detail="account session refresh failed",
            ) from None
        except Exception:
            provider.mark_error(role, "DIRECT_SESSION_UNAVAILABLE")
            raise HTTPException(
                status_code=503,
                detail="account session refresh failed",
            ) from None
        return public_account_session_status(role)

    @app.get("/api/admin/lock")
    def lock_state(session=Depends(require_admin)) -> LockResponse:
        return LockResponse.model_validate(app.state.runner_control.lock_state(session.session_id))

    @app.post("/api/admin/lock/acquire")
    def acquire_lock(session=Depends(require_csrf)) -> LockResponse:
        if not app.state.runner_control.lock(session.session_id):
            raise HTTPException(status_code=409, detail="runner is locked by another admin")
        return LockResponse(locked=True)

    @app.post("/api/admin/lock/release")
    def release_lock(session=Depends(require_csrf)) -> LockResponse:
        if not app.state.runner_control.release(session.session_id):
            detail = (
                "DEVICE_ACTION_IN_PROGRESS"
                if app.state.runner_control.has_active_operation
                else "runner lock is not owned by this admin"
            )
            raise HTTPException(status_code=409, detail=detail)
        return LockResponse(locked=False)

    @app.post("/api/admin/jobs/{public_id}/resume", response_model=TaskResponse)
    def resume_waiting_job(public_id: str, _session=Depends(require_csrf)) -> TaskResponse:
        task = app.state.store.get(public_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        try:
            resumed = app.state.store.requeue_waiting(task.task_id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        notify_runner_if_queued(resumed)
        return task_response(resumed)

    @app.post("/api/admin/jobs/{public_id}/retry", response_model=TaskResponse)
    def retry_failed_job(public_id: str, _session=Depends(require_csrf)) -> TaskResponse:
        task = app.state.store.get(public_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        try:
            retried = app.state.store.retry_failed(task.task_id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        notify_runner_if_queued(retried)
        return task_response(retried)

    @app.get("/api/admin/queue", response_model=QueueResponse)
    def queue_status(_session=Depends(require_admin)) -> QueueResponse:
        return QueueResponse(paused=app.state.runner_control.queue_paused)

    @app.post("/api/admin/queue/pause", response_model=QueueResponse)
    def pause_queue(_session=Depends(require_csrf)) -> QueueResponse:
        app.state.runner_control.pause_queue()
        return QueueResponse(paused=True)

    @app.post("/api/admin/queue/resume", response_model=QueueResponse)
    def resume_queue(_session=Depends(require_csrf)) -> QueueResponse:
        if not app.state.runner_control.resume_queue():
            detail = (
                "DEVICE_ACTION_IN_PROGRESS"
                if app.state.runner_control.has_active_operation
                else "release device control before resuming the queue"
            )
            raise HTTPException(status_code=409, detail=detail)
        app.state.runner_wake.notify()
        return QueueResponse(paused=False)

    device_labels = {
        "core_metrics": "八项账号",
        "main_fund_flow": "资金账号",
    }
    lifecycle_error_statuses = {
        "DEVICE_ACTION_IN_PROGRESS": 409,
        "DEVICE_BOOT_TIMEOUT": 504,
        "DEVICE_AVD_NOT_FOUND": 503,
        "DEVICE_APP_LAUNCH_FAILED": 503,
        "DEVICE_SHUTDOWN_FAILED": 503,
        "DEVICE_LIFECYCLE_FAILED": 503,
        "DEVICE_LIFECYCLE_UNAVAILABLE": 503,
    }

    def safe_lifecycle_error_code(error_code: str | None) -> str | None:
        if error_code is None or error_code in lifecycle_error_statuses:
            return error_code
        return "DEVICE_LIFECYCLE_FAILED"

    def unconfigured_lifecycle() -> AdminDeviceLifecycleResponse:
        return AdminDeviceLifecycleResponse(
            state=DeviceLifecycleState.UNCONFIGURED.value,
            operation_id=None,
            error_code=None,
            updated_at=None,
        )

    def lifecycle_health_by_role() -> dict[str, AdminDeviceLifecycleResponse]:
        lifecycle = app.state.device_lifecycle
        if lifecycle is None:
            return {role: unconfigured_lifecycle() for role in device_labels}
        try:
            statuses = lifecycle.devices()
        except DeviceLifecycleError as error:
            fallback = AdminDeviceLifecycleResponse(
                state=DeviceLifecycleState.UNKNOWN.value,
                operation_id=None,
                error_code=safe_lifecycle_error_code(error.error_code),
                updated_at=None,
            )
        except Exception:
            fallback = AdminDeviceLifecycleResponse(
                state=DeviceLifecycleState.UNKNOWN.value,
                operation_id=None,
                error_code="DEVICE_LIFECYCLE_FAILED",
                updated_at=None,
            )
        else:
            by_role = {status.role: status for status in statuses}
            control = app.state.runner_control
            for status in statuses:
                control.reconcile_operation(
                    status.role,
                    status.operation_id,
                    status.state.value,
                )
            return {
                role: (
                    AdminDeviceLifecycleResponse(
                        state=status.state.value,
                        operation_id=status.operation_id,
                        error_code=safe_lifecycle_error_code(status.error_code),
                        updated_at=status.updated_at,
                    )
                    if (status := by_role.get(role)) is not None
                    else AdminDeviceLifecycleResponse(
                        state=DeviceLifecycleState.UNKNOWN.value,
                        operation_id=None,
                        error_code="DEVICE_LIFECYCLE_FAILED",
                        updated_at=None,
                    )
                )
                for role in device_labels
            }
        return {role: fallback for role in device_labels}

    def role_device_health(
        role: str,
        bridge: DeviceBridge,
        lifecycle_health: Mapping[str, AdminDeviceLifecycleResponse] | None = None,
    ) -> AdminDeviceHealthResponse:
        if lifecycle_health is None:
            lifecycle_health = lifecycle_health_by_role()
        probe = app.state.device_health_probes.get(role)
        if probe is not None:
            try:
                health = dict(probe())
            except Exception:
                health = {}
            adb_state = health.get("adb", "OFFLINE")
            app_state = health.get("app", "OFFLINE")
            frida_state = health.get("frida", "OFFLINE")
        else:
            try:
                online = bool(bridge.is_online())
            except Exception:
                online = False
            adb_state = "ONLINE" if online else "OFFLINE"
            app_state = adb_state
            frida_state = "UNKNOWN"
        return AdminDeviceHealthResponse(
            role=role,
            label=device_labels[role],
            adb=adb_state,
            app=app_state,
            frida=frida_state,
            lifecycle=lifecycle_health[role],
        )

    @app.get("/api/admin/devices", response_model=AdminDevicesResponse)
    def admin_devices(_session=Depends(require_admin)) -> AdminDevicesResponse:
        lifecycle_health = lifecycle_health_by_role()
        devices: list[AdminDeviceHealthResponse] = []
        for role in ("core_metrics", "main_fund_flow"):
            bridge = app.state.device_bridges.get(role)
            if bridge is not None:
                devices.append(
                    role_device_health(role, bridge, lifecycle_health)
                )
        return AdminDevicesResponse(devices=devices)

    @app.post(
        "/api/admin/devices/{role}/actions",
        response_model=AdminDeviceLifecycleResponse,
        status_code=202,
    )
    def admin_device_action(
        role: str,
        payload: DeviceLifecycleActionRequest,
        session=Depends(require_csrf),
    ) -> AdminDeviceLifecycleResponse:
        if role not in device_labels:
            raise HTTPException(status_code=404, detail="device role not found")
        control = app.state.runner_control
        lifecycle = app.state.device_lifecycle
        try:
            with control.maintenance(session.session_id, app.state.store):
                if lifecycle is None:
                    raise DeviceLifecycleError("DEVICE_LIFECYCLE_UNAVAILABLE")
                operation = lifecycle.submit(role, payload.action)
                if operation.state in {
                    DeviceLifecycleState.STARTING,
                    DeviceLifecycleState.STOPPING,
                } and not control.begin_operation(role, operation.operation_id):
                    raise DeviceLifecycleError("DEVICE_LIFECYCLE_FAILED")
        except RunnerMaintenanceError as error:
            raise HTTPException(status_code=409, detail=error.error_code) from None
        except DeviceLifecycleError as error:
            error_code = safe_lifecycle_error_code(error.error_code)
            if error_code is None:
                error_code = "DEVICE_LIFECYCLE_FAILED"
            raise HTTPException(
                status_code=lifecycle_error_statuses[error_code],
                detail=error_code,
            ) from None
        except Exception:
            raise HTTPException(
                status_code=503,
                detail="DEVICE_LIFECYCLE_FAILED",
            ) from None
        return AdminDeviceLifecycleResponse(
            state=operation.state.value,
            operation_id=operation.operation_id,
            error_code=safe_lifecycle_error_code(operation.error_code),
            updated_at=operation.updated_at,
        )

    async def stream_device_role(
        websocket: WebSocket,
        role: str,
        *,
        include_device_health: bool,
    ) -> None:
        session = app.state.admin_sessions.valid_session(websocket.cookies.get("ths_admin_session"))
        if session is None:
            await websocket.close(code=1008)
            return
        bridge = app.state.device_bridges.get(role)
        if bridge is None:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        control = app.state.runner_control
        loop = get_running_loop()
        unregister_socket = control.register_socket(
            session.session_id,
            lambda: loop.call_soon_threadsafe(lambda: create_task(_close_device_socket(websocket, 1008))),
        )
        await websocket.send_json(control.status(session.session_id))
        if include_device_health:
            health = await to_thread(role_device_health, role, bridge)
            await websocket.send_json(
                {"type": "device_status", **health.model_dump(mode="json")}
            )

        def session_is_valid() -> bool:
            valid = app.state.admin_sessions.valid_session(session.session_id)
            if valid is None:
                control.disconnect_session(session.session_id)
                return False
            return True

        async def receive_input() -> None:
            last_input_sequence = 0
            while True:
                if not session_is_valid():
                    await _close_device_socket(websocket, 1008)
                    return
                try:
                    raw = await websocket.receive_json()
                except WebSocketDisconnect:
                    return
                if not session_is_valid():
                    await _close_device_socket(websocket, 1008)
                    return
                event = _validated_input(raw)
                input_sequence = raw.get("sequence") if isinstance(raw, dict) else None
                if event is None or not isinstance(input_sequence, int) or input_sequence <= last_input_sequence:
                    await _close_device_socket(websocket, 1003)
                    return
                last_input_sequence = input_sequence
                if control.authorizes_input(session.session_id):
                    await to_thread(_forward_input, bridge, event)

        async def emit_frames() -> None:
            frame_sequence = 0
            while True:
                await sleep(0.5)
                if not session_is_valid():
                    await _close_device_socket(websocket, 1008)
                    return
                if not control.authorizes_input(session.session_id):
                    await websocket.send_json(control.status(session.session_id))
                    continue
                try:
                    encoded = jpeg_base64(await to_thread(bridge.screenshot_png))
                except Exception:
                    await websocket.send_json(control.status(session.session_id))
                    continue
                frame_sequence += 1
                await websocket.send_json({"type": "frame", "encoding": "jpeg", "sequence": frame_sequence, "capturedAt": utc_now().isoformat(), "data": encoded})
                if include_device_health and frame_sequence % 10 == 0:
                    health = await to_thread(role_device_health, role, bridge)
                    await websocket.send_json(
                        {"type": "device_status", **health.model_dump(mode="json")}
                    )
                await websocket.send_json(control.status(session.session_id))

        receiver = create_task(receive_input())
        ticker = create_task(emit_frames())
        try:
            _done, pending = await wait({receiver, ticker}, return_when=FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await gather(receiver, ticker, return_exceptions=True)
        except (WebSocketDisconnect, CancelledError):
            return
        finally:
            for task in (receiver, ticker):
                if not task.done():
                    task.cancel()
            await gather(receiver, ticker, return_exceptions=True)
            unregister_socket()

    @app.websocket("/api/admin/device")
    async def legacy_device_stream(websocket: WebSocket) -> None:
        await stream_device_role(websocket, "core_metrics", include_device_health=False)

    @app.websocket("/api/admin/devices/{role}")
    async def device_stream(websocket: WebSocket, role: str) -> None:
        await stream_device_role(websocket, role, include_device_health=True)

    install_market_routes(
        app,
        require_admin=require_admin,
        require_admin_csrf=require_csrf,
        resolve_symbol=resolve_symbol,
        secure_cookies=secure_admin_cookies,
        previous_trading_date=previous_trading_date,
    )

    if app.state.frontend_root is not None:
        index_file = app.state.frontend_root / "index.html"
        assets_root = app.state.frontend_root / "assets"
        if index_file.is_file():
            if assets_root.is_dir():
                app.mount("/assets", StaticFiles(directory=assets_root), name="frontend-assets")

            def frontend_public_file(filename: str, media_type: str):
                path = app.state.frontend_root / filename
                if not path.is_file():
                    raise HTTPException(status_code=404, detail="Not Found")
                return FileResponse(path, media_type=media_type)

            @app.get("/market.webmanifest", include_in_schema=False)
            def market_manifest():
                return frontend_public_file("market.webmanifest", "application/manifest+json")

            @app.get("/market-sw.js", include_in_schema=False)
            def market_service_worker():
                return frontend_public_file("market-sw.js", "text/javascript")

            @app.get("/market-icon.svg", include_in_schema=False)
            def market_icon():
                return frontend_public_file("market-icon.svg", "image/svg+xml")

            @app.api_route("/{frontend_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
            def frontend_fallback(frontend_path: str):
                if frontend_path == "api" or frontend_path.startswith("api/"):
                    raise HTTPException(status_code=404, detail="Not Found")
                return FileResponse(index_file, media_type="text/html")

    return app


def _validated_input(value: object) -> dict | None:
    """Validate the documented input envelope without retaining key values."""
    if not isinstance(value, dict) or value.get("type") != "input" or not isinstance(value.get("sequence"), int):
        return None
    event = value.get("event")
    if not isinstance(event, dict) or event.get("kind") not in {"tap", "swipe", "key"}:
        return None
    kind = event["kind"]
    if kind == "tap":
        if not _normalised(event.get("x")) or not _normalised(event.get("y")):
            return None
    elif kind == "swipe":
        if not all(_normalised(event.get(field)) for field in ("startX", "startY", "endX", "endY")):
            return None
    elif not isinstance(event.get("key"), str) or event.get("action") not in {"down", "up"}:
        return None
    return event


async def _close_device_socket(websocket: WebSocket, code: int) -> None:
    """Close once: logout and the loop may observe the same revocation."""
    if websocket.application_state != WebSocketState.CONNECTED:
        return
    try:
        await websocket.close(code=code)
    except (RuntimeError, WebSocketDisconnect):
        return


def _normalised(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1


def _forward_input(bridge: DeviceBridge, event: dict) -> None:
    """Dispatch only normalised events; deliberately do not log keyboard payloads."""
    if event["kind"] == "tap":
        bridge.tap(float(event["x"]), float(event["y"]))
    elif event["kind"] == "swipe":
        bridge.swipe(float(event["startX"]), float(event["startY"]), float(event["endX"]), float(event["endY"]))
    else:
        bridge.key(event["key"], event["action"])
