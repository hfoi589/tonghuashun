"""Production-only assembly for the API, Redis queue, ADB bridge, and runner."""

from __future__ import annotations

import inspect
import os
import socket
import time
from math import isfinite
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from fastapi import FastAPI
from cryptography.fernet import Fernet

from .api import create_app
from .app_sessions import (
    CoreAccountSessionRefresher,
    EncryptedFileSessionProvider,
    FundAccountSessionRefresher,
    capture_core_session_material,
    capture_fund_http_session,
)
from .daily_kline import DailyKlineMarketDataSource, TonghuashunPublicDailyKlineProvider
from .device_lifecycle import DeviceLifecycleClient
from .database_first_fund_flow import DatabaseFirstFundFlowSource
from .etf_fund_flow import EtfFundFlowMonitor, TonghuashunEtfFundFlowService
from .fund_flow_history import (
    FundFlowHistoryMonitor,
    intraday_fund_flow_cap_minute,
    settled_fund_flow_minute,
)
from .direct_market import (
    Core9528Client,
    Core9528CurveDecoder,
    Core9528TemplateProtocol,
    Core9528WarmPool,
    FundFlowHttpClient,
    ShadowParsedValueSource,
)
from .market_accounts import RedisMarketSessionStore, SQLiteMarketAccountStore
from .market_data import LocalTradingCalendar, MarketDataBroker, MarketPhaseResolver, SHANGHAI_TZ, is_china_market_open
from .models import MetricKind
from .parsed_values import (
    FUND_ONLY_METRICS,
    DirectReadOutcome,
    DualAccountParsedValueSource,
    FridaParsedValueSource,
    is_exchange_fund_symbol,
)
from .public_market import (
    DirectEnrichedMarketDataSource,
    PublicMarketDataSource,
    SinaPublicQuoteProvider,
    TencentPublicMarketProvider,
)
from .preopen_market import (
    CoreDirectPreopenQuoteClient,
    PreopenMarketDataSource,
    RedisPreopenQuoteCache,
)
from .queue import RedisStreamsStore
from .research_store import SQLiteResearchStore
from .research_monitor import ResearchMonitor
from .research_push import PushDispatcher
from .research_premium import PublicPremiumProvider
from .premium_monitor import PremiumMonitor
from .session_alert import PreopenSessionProbe, SessionHealthNotifier
from .runner import ADBDeviceBridge, DailyCheckState, Level2Navigator, Level2Runner, OpenCVTemplateFallback, RunnerControl, TAB_LABELS, long_capture_has_net_heading
from .ths_trade import TonghuashunTradeExecutor
from .auto_trade import AutoTradeEngine, SQLiteAutoTradeStore
from .brent_market import BrentMarketMonitor
from .quant_rules import SQLiteQuantRuleStore, enrich_store_with_line_funds
from .security import persist_password_hash
from .symbol_catalog import SinaSymbolCatalogSource, SQLiteSymbolCatalog
from .request_logs import RequestLogStore


@dataclass(frozen=True)
class DeploymentSettings:
    redis_url: str
    redis_connect_timeout_seconds: float
    redis_socket_timeout_seconds: float
    redis_startup_retry_attempts: int
    redis_startup_retry_delay_seconds: float
    capture_root: Path
    admin_password_hash: str
    admin_password_file: Path | None
    admin_session_secret: str
    adb_path: str
    adb_serial: str
    adb_server_socket: str | None
    template_root: Path | None
    runner_poll_interval_seconds: float
    frontend_root: Path | None
    admin_cookie_secure: bool
    frida_server_endpoint: str | None
    dual_account_mode: bool
    core_adb_serial: str | None
    core_frida_server_endpoint: str | None
    fund_adb_serial: str | None
    fund_frida_server_endpoint: str | None
    daily_check_state_file: Path
    market_database_path: Path
    research_database_path: Path
    symbol_catalog_path: Path
    trading_calendar_path: Path
    symbol_catalog_max_age_seconds: float
    symbol_catalog_refresh_hour: int
    symbol_catalog_refresh_minute: int
    public_market_timeout_seconds: float
    market_direct_enrichment: bool
    market_direct_enrichment_ttl_seconds: float
    core_warm_connection_max_idle_seconds: float
    core_metrics_transport: str
    fund_flow_transport: str
    market_preopen_transport: str
    device_lifecycle_url: str | None
    device_lifecycle_token: str | None = field(repr=False)
    device_lifecycle_timeout_seconds: float
    ths_session_encryption_key: str | None = field(repr=False)
    ths_session_root: Path
    fund_flow_three_day_interval_seconds: float = 120.0
    fund_flow_five_day_interval_seconds: float = 300.0
    fund_flow_max_requests_per_second: float = 5.0
    goodluck_internal_token: str | None = field(default=None, repr=False)
    market_max_symbols_per_user: int = 50
    market_max_total_symbols: int = 50
    research_monitor_interval_seconds: float = 25.0
    fund_flow_monitor_interval_seconds: float = 20.0
    core_u2_endpoint: str | None = None
    broker_trade_password: str | None = field(default=None, repr=False)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> "DeploymentSettings":
        values = os.environ if environ is None else environ
        password_file_value = values.get("ADMIN_PASSWORD_FILE", "").strip()
        password_file = Path(password_file_value).expanduser().resolve() if password_file_value else None
        password_hash = values.get("ADMIN_PASSWORD_HASH", "").strip()
        if password_file is not None and password_file.is_file():
            try:
                password_hash = password_file.read_text(encoding="utf-8").strip()
            except OSError as error:
                raise ValueError("ADMIN_PASSWORD_FILE could not be read") from error
        session_secret = values.get("ADMIN_SESSION_SECRET", "")
        if not password_hash.startswith("$argon2id$"):
            raise ValueError("ADMIN_PASSWORD_HASH must contain an Argon2id hash")
        if len(session_secret) < 32:
            raise ValueError("ADMIN_SESSION_SECRET must be at least 32 characters")
        try:
            poll_interval = float(values.get("RUNNER_POLL_INTERVAL_SECONDS", "1"))
        except ValueError as error:
            raise ValueError("RUNNER_POLL_INTERVAL_SECONDS must be a positive number") from error
        if poll_interval <= 0:
            raise ValueError("RUNNER_POLL_INTERVAL_SECONDS must be a positive number")
        dual_names = (
            "CORE_ADB_SERIAL",
            "CORE_FRIDA_SERVER_ENDPOINT",
            "FUND_ADB_SERIAL",
            "FUND_FRIDA_SERVER_ENDPOINT",
        )
        dual_values = {name: values.get(name, "").strip() for name in dual_names}
        dual_account_mode = any(dual_values.values())
        if dual_account_mode and not all(dual_values.values()):
            raise ValueError(
                "dual-account mode requires CORE_ADB_SERIAL, "
                "CORE_FRIDA_SERVER_ENDPOINT, FUND_ADB_SERIAL, and "
                "FUND_FRIDA_SERVER_ENDPOINT"
            )
        legacy_adb_serial = values.get("ADB_SERIAL", "").strip()
        legacy_frida_endpoint = values.get("FRIDA_SERVER_ENDPOINT", "").strip() or None
        if dual_account_mode:
            adb_serial = dual_values["CORE_ADB_SERIAL"]
            frida_server_endpoint = dual_values["CORE_FRIDA_SERVER_ENDPOINT"]
        else:
            adb_serial = legacy_adb_serial
            frida_server_endpoint = legacy_frida_endpoint
            if not adb_serial:
                raise ValueError("ADB_SERIAL is required")
        template_root_value = values.get("TEMPLATE_ROOT", "").strip()
        frontend_root_value = values.get("FRONTEND_ROOT", "").strip()
        capture_root = Path(values.get("CAPTURE_ROOT", "/data/captures")).resolve()
        transport_modes: dict[str, str] = {}
        for name in ("CORE_METRICS_TRANSPORT", "FUND_FLOW_TRANSPORT"):
            mode = values.get(name, "frida").strip().lower() or "frida"
            if mode not in {"frida", "shadow", "direct"}:
                raise ValueError(f"{name} must be frida, shadow, or direct")
            transport_modes[name] = mode
        market_preopen_transport = (
            values.get("MARKET_PREOPEN_TRANSPORT", "off").strip().lower()
            or "off"
        )
        if market_preopen_transport not in {"off", "shadow", "direct"}:
            raise ValueError(
                "MARKET_PREOPEN_TRANSPORT must be off, shadow, or direct"
            )
        direct_transport_enabled = any(
            mode != "frida" for mode in transport_modes.values()
        ) or market_preopen_transport != "off"
        session_encryption_key = values.get("THS_SESSION_ENCRYPTION_KEY", "").strip()
        if direct_transport_enabled and not session_encryption_key:
            raise ValueError(
                "THS_SESSION_ENCRYPTION_KEY is required for shadow or direct transport"
            )
        if direct_transport_enabled:
            try:
                Fernet(session_encryption_key.encode("ascii"))
            except (UnicodeEncodeError, ValueError) as error:
                raise ValueError(
                    "THS_SESSION_ENCRYPTION_KEY must be a URL-safe base64 Fernet key"
                ) from error
        if direct_transport_enabled and not dual_account_mode:
            raise ValueError("shadow and direct transports require dual-account mode")
        session_root = Path(
            values.get("THS_SESSION_ROOT", "/data/admin/ths-sessions")
        ).expanduser().resolve()
        lifecycle_url = values.get("THS_DEVICE_LIFECYCLE_URL", "").strip() or None
        lifecycle_token = values.get("THS_DEVICE_LIFECYCLE_TOKEN", "").strip() or None
        if (lifecycle_url is None) != (lifecycle_token is None):
            raise ValueError(
                "THS_DEVICE_LIFECYCLE_URL and THS_DEVICE_LIFECYCLE_TOKEN "
                "must be provided together"
            )
        try:
            lifecycle_timeout = float(
                values.get("THS_DEVICE_LIFECYCLE_TIMEOUT_SECONDS", "5")
            )
        except ValueError as error:
            raise ValueError(
                "THS_DEVICE_LIFECYCLE_TIMEOUT_SECONDS must be a positive number"
            ) from error
        if not isfinite(lifecycle_timeout) or lifecycle_timeout <= 0:
            raise ValueError(
                "THS_DEVICE_LIFECYCLE_TIMEOUT_SECONDS must be a positive number"
            )
        if lifecycle_url is not None:
            DeviceLifecycleClient.validate_base_url(lifecycle_url)
        cookie_secure_value = values.get("ADMIN_COOKIE_SECURE", "1").strip().lower()
        if cookie_secure_value in {"1", "true", "yes", "on"}:
            admin_cookie_secure = True
        elif cookie_secure_value in {"0", "false", "no", "off"}:
            admin_cookie_secure = False
        else:
            raise ValueError("ADMIN_COOKIE_SECURE must be a boolean value")
        market_database_path = Path(
            values.get(
                "MARKET_DATABASE_PATH",
                str(capture_root.parent / "market" / "market.db"),
            )
        ).expanduser().resolve()
        research_database_path = Path(
            values.get(
                "RESEARCH_DATABASE_PATH",
                str(market_database_path.with_name("research.db")),
            )
        ).expanduser().resolve()
        positive_values: dict[str, float] = {}
        for name, default in (
            ("REDIS_CONNECT_TIMEOUT_SECONDS", "5"),
            ("REDIS_SOCKET_TIMEOUT_SECONDS", "5"),
            ("SYMBOL_CATALOG_MAX_AGE_SECONDS", "604800"),
            ("PUBLIC_MARKET_TIMEOUT_SECONDS", "8"),
            ("MARKET_DIRECT_ENRICHMENT_TTL_SECONDS", "5"),
            ("CORE_WARM_CONNECTION_MAX_IDLE_SECONDS", "25"),
            ("FUND_FLOW_THREE_DAY_INTERVAL_SECONDS", "120"),
            ("FUND_FLOW_FIVE_DAY_INTERVAL_SECONDS", "300"),
            ("FUND_FLOW_MAX_REQUESTS_PER_SECOND", "5"),
            ("RESEARCH_MONITOR_INTERVAL_SECONDS", "25"),
            ("FUND_FLOW_MONITOR_INTERVAL_SECONDS", "20"),
        ):
            try:
                parsed = float(values.get(name, default))
            except ValueError as error:
                raise ValueError(f"{name} must be a positive number") from error
            if not isfinite(parsed) or parsed <= 0:
                raise ValueError(f"{name} must be a positive number")
            positive_values[name] = parsed
        try:
            redis_startup_retry_attempts = int(
                values.get("REDIS_STARTUP_RETRY_ATTEMPTS", "10")
            )
        except ValueError as error:
            raise ValueError(
                "REDIS_STARTUP_RETRY_ATTEMPTS must be a positive integer"
            ) from error
        if redis_startup_retry_attempts <= 0:
            raise ValueError(
                "REDIS_STARTUP_RETRY_ATTEMPTS must be a positive integer"
            )
        try:
            redis_startup_retry_delay = float(
                values.get("REDIS_STARTUP_RETRY_DELAY_SECONDS", "1")
            )
        except ValueError as error:
            raise ValueError(
                "REDIS_STARTUP_RETRY_DELAY_SECONDS must be a non-negative number"
            ) from error
        if not isfinite(redis_startup_retry_delay) or redis_startup_retry_delay < 0:
            raise ValueError(
                "REDIS_STARTUP_RETRY_DELAY_SECONDS must be a non-negative number"
            )
        try:
            catalog_refresh_hour = int(
                values.get("SYMBOL_CATALOG_REFRESH_HOUR", "16")
            )
            catalog_refresh_minute = int(
                values.get("SYMBOL_CATALOG_REFRESH_MINUTE", "20")
            )
        except ValueError as error:
            raise ValueError(
                "SYMBOL_CATALOG_REFRESH_HOUR and "
                "SYMBOL_CATALOG_REFRESH_MINUTE must be integers"
            ) from error
        if not 0 <= catalog_refresh_hour <= 23:
            raise ValueError(
                "SYMBOL_CATALOG_REFRESH_HOUR must be between 0 and 23"
            )
        if not 0 <= catalog_refresh_minute <= 59:
            raise ValueError(
                "SYMBOL_CATALOG_REFRESH_MINUTE must be between 0 and 59"
            )
        enrichment_value = values.get(
            "MARKET_DIRECT_ENRICHMENT",
            "1",
        ).strip().lower()
        if enrichment_value in {"1", "true", "yes", "on"}:
            market_direct_enrichment = True
        elif enrichment_value in {"0", "false", "no", "off"}:
            market_direct_enrichment = False
        else:
            raise ValueError("MARKET_DIRECT_ENRICHMENT must be a boolean value")
        int_values: dict[str, int] = {}
        for name, default in (
            ("MARKET_MAX_SYMBOLS_PER_USER", "50"),
            ("MARKET_MAX_TOTAL_SYMBOLS", "50"),
        ):
            try:
                parsed_int = int(values.get(name, default))
            except ValueError as error:
                raise ValueError(f"{name} must be a positive integer") from error
            if parsed_int <= 0:
                raise ValueError(f"{name} must be a positive integer")
            int_values[name] = parsed_int
        return cls(
            redis_url=values.get("REDIS_URL", "redis://redis:6379/0"),
            redis_connect_timeout_seconds=positive_values[
                "REDIS_CONNECT_TIMEOUT_SECONDS"
            ],
            redis_socket_timeout_seconds=positive_values[
                "REDIS_SOCKET_TIMEOUT_SECONDS"
            ],
            redis_startup_retry_attempts=redis_startup_retry_attempts,
            redis_startup_retry_delay_seconds=redis_startup_retry_delay,
            capture_root=capture_root,
            admin_password_hash=password_hash,
            admin_password_file=password_file,
            admin_session_secret=session_secret,
            adb_path=values.get("ADB_PATH", "adb"),
            adb_serial=adb_serial,
            adb_server_socket=values.get("ADB_SERVER_SOCKET") or None,
            template_root=Path(template_root_value).resolve() if template_root_value else None,
            runner_poll_interval_seconds=poll_interval,
            frontend_root=Path(frontend_root_value).resolve() if frontend_root_value else None,
            admin_cookie_secure=admin_cookie_secure,
            frida_server_endpoint=frida_server_endpoint,
            dual_account_mode=dual_account_mode,
            core_adb_serial=dual_values["CORE_ADB_SERIAL"] or None,
            core_frida_server_endpoint=dual_values["CORE_FRIDA_SERVER_ENDPOINT"] or None,
            fund_adb_serial=dual_values["FUND_ADB_SERIAL"] or None,
            fund_frida_server_endpoint=dual_values["FUND_FRIDA_SERVER_ENDPOINT"] or None,
            daily_check_state_file=Path(
                values.get("DAILY_CHECK_STATE_FILE", "/data/admin/daily-check.json")
            ).expanduser().resolve(),
            market_database_path=market_database_path,
            research_database_path=research_database_path,
            symbol_catalog_path=Path(
                values.get(
                    "SYMBOL_CATALOG_PATH",
                    str(market_database_path.with_name("symbol-catalog.db")),
                )
            ).expanduser().resolve(),
            trading_calendar_path=Path(
                values.get(
                    "TRADING_CALENDAR_PATH",
                    str(market_database_path.with_name("trading-calendar.json")),
                )
            ).expanduser().resolve(),
            symbol_catalog_max_age_seconds=positive_values[
                "SYMBOL_CATALOG_MAX_AGE_SECONDS"
            ],
            symbol_catalog_refresh_hour=catalog_refresh_hour,
            symbol_catalog_refresh_minute=catalog_refresh_minute,
            public_market_timeout_seconds=positive_values[
                "PUBLIC_MARKET_TIMEOUT_SECONDS"
            ],
            market_direct_enrichment=market_direct_enrichment,
            market_direct_enrichment_ttl_seconds=positive_values[
                "MARKET_DIRECT_ENRICHMENT_TTL_SECONDS"
            ],
            core_warm_connection_max_idle_seconds=positive_values[
                "CORE_WARM_CONNECTION_MAX_IDLE_SECONDS"
            ],
            core_metrics_transport=transport_modes["CORE_METRICS_TRANSPORT"],
            fund_flow_transport=transport_modes["FUND_FLOW_TRANSPORT"],
            market_preopen_transport=market_preopen_transport,
            device_lifecycle_url=lifecycle_url,
            device_lifecycle_token=lifecycle_token,
            device_lifecycle_timeout_seconds=lifecycle_timeout,
            fund_flow_three_day_interval_seconds=positive_values[
                "FUND_FLOW_THREE_DAY_INTERVAL_SECONDS"
            ],
            fund_flow_five_day_interval_seconds=positive_values[
                "FUND_FLOW_FIVE_DAY_INTERVAL_SECONDS"
            ],
            fund_flow_max_requests_per_second=positive_values[
                "FUND_FLOW_MAX_REQUESTS_PER_SECOND"
            ],
            ths_session_encryption_key=session_encryption_key or None,
            ths_session_root=session_root,
            goodluck_internal_token=values.get("GOODLUCK_INTERNAL_TOKEN", "").strip() or None,
            market_max_symbols_per_user=int_values["MARKET_MAX_SYMBOLS_PER_USER"],
            market_max_total_symbols=int_values["MARKET_MAX_TOTAL_SYMBOLS"],
            research_monitor_interval_seconds=positive_values[
                "RESEARCH_MONITOR_INTERVAL_SECONDS"
            ],
            fund_flow_monitor_interval_seconds=positive_values[
                "FUND_FLOW_MONITOR_INTERVAL_SECONDS"
            ],
            core_u2_endpoint=values.get("CORE_U2_ENDPOINT", "").strip() or None,
            broker_trade_password=values.get("THS_BROKER_TRADE_PASSWORD", "").strip() or None,
        )


def _redis_client_from_url(
    url: str,
    *,
    socket_connect_timeout: float = 5.0,
    socket_timeout: float = 5.0,
) -> object:
    from redis import Redis

    return Redis.from_url(
        url,
        socket_connect_timeout=socket_connect_timeout,
        socket_timeout=socket_timeout,
        health_check_interval=30,
        retry_on_timeout=True,
    )


def _wait_for_redis_ready(
    redis_client: object,
    *,
    attempts: int,
    delay_seconds: float,
) -> None:
    """Block startup until Redis accepts commands, with a bounded retry loop."""
    ping = getattr(redis_client, "ping", None)
    if not callable(ping):
        return
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            ping()
            return
        except Exception as error:
            last_error = error
            if attempt + 1 < attempts and delay_seconds:
                time.sleep(delay_seconds)
    raise RuntimeError("Redis did not become ready during startup") from last_error


def _device_health_probe(
    bridge: ADBDeviceBridge,
    frida_endpoint: str | None,
) -> Callable[[], Mapping[str, str]]:
    def probe() -> Mapping[str, str]:
        try:
            adb_online = bool(bridge.is_online())
        except Exception:
            adb_online = False
        app_running = getattr(bridge, "app_running", None)
        try:
            app_online = adb_online and (
                bool(app_running()) if callable(app_running) else adb_online
            )
        except Exception:
            app_online = False
        frida_state = "UNKNOWN"
        if frida_endpoint:
            host, separator, port_text = frida_endpoint.rpartition(":")
            if separator and host and port_text.isdigit():
                try:
                    with socket.create_connection((host, int(port_text)), timeout=0.5):
                        frida_state = "ONLINE"
                except OSError:
                    frida_state = "OFFLINE"
        return {
            "adb": "ONLINE" if adb_online else "OFFLINE",
            "app": "ONLINE" if app_online else "OFFLINE",
            "frida": frida_state,
        }

    return probe


def create_production_app(
    *,
    settings: DeploymentSettings | None = None,
    redis_client_factory: Callable[..., object] = _redis_client_from_url,
    bridge_factory: Callable[..., ADBDeviceBridge] = ADBDeviceBridge,
    runner_factory: Callable[..., Level2Runner] = Level2Runner,
    symbol_catalog_source: object | None = None,
    symbol_catalog_factory: Callable[..., SQLiteSymbolCatalog] = (
        SQLiteSymbolCatalog
    ),
) -> FastAPI:
    """Create the only app mode that enables the real Android worker."""
    config = settings or DeploymentSettings.from_environ()
    config.capture_root.mkdir(parents=True, exist_ok=True)
    if redis_client_factory is _redis_client_from_url:
        redis_client = redis_client_factory(
            config.redis_url,
            socket_connect_timeout=config.redis_connect_timeout_seconds,
            socket_timeout=config.redis_socket_timeout_seconds,
        )
    else:
        redis_client = redis_client_factory(config.redis_url)
    _wait_for_redis_ready(
        redis_client,
        attempts=config.redis_startup_retry_attempts,
        delay_seconds=config.redis_startup_retry_delay_seconds,
    )
    store = RedisStreamsStore(redis_client, capture_root=config.capture_root)
    symbol_catalog = symbol_catalog_factory(
        config.symbol_catalog_path,
        symbol_catalog_source or SinaSymbolCatalogSource(),
        stale_after=timedelta(
            seconds=config.symbol_catalog_max_age_seconds
        ),
    )
    adb_environment = os.environ.copy()
    if config.adb_server_socket:
        adb_environment["ADB_SERVER_SOCKET"] = config.adb_server_socket
    if config.dual_account_mode:
        assert config.core_adb_serial is not None
        assert config.fund_adb_serial is not None
        core_bridge = bridge_factory(
            adb=config.adb_path,
            serial=config.core_adb_serial,
            environment=adb_environment,
        )
        fund_bridge = bridge_factory(
            adb=config.adb_path,
            serial=config.fund_adb_serial,
            environment=adb_environment,
        )
        device_bridges = {
            "core_metrics": core_bridge,
            "main_fund_flow": fund_bridge,
        }
        device_health_probes = {
            "core_metrics": _device_health_probe(
                core_bridge,
                config.core_frida_server_endpoint,
            ),
            "main_fund_flow": _device_health_probe(
                fund_bridge,
                config.fund_frida_server_endpoint,
            ),
        }
    else:
        core_bridge = bridge_factory(
            adb=config.adb_path,
            serial=config.adb_serial,
            environment=adb_environment,
        )
        device_bridges = {"core_metrics": core_bridge}
        device_health_probes = {
            "core_metrics": _device_health_probe(
                core_bridge,
                config.frida_server_endpoint,
            )
        }
    control = RunnerControl()
    templates: dict[str, Path] = {}
    if config.template_root:
        search_template = config.template_root / "search.png"
        if search_template.is_file():
            templates["search"] = search_template
        for kind, label in TAB_LABELS.items():
            for filename in (f"{kind.value}.png", f"{label}.png", f"tab-{label}.png", f"tab_{kind.value}.png"):
                candidate = config.template_root / filename
                if candidate.is_file():
                    templates[f"tab:{label}"] = candidate
                    break
    navigator = Level2Navigator(core_bridge, OpenCVTemplateFallback(templates))
    market_accounts = SQLiteMarketAccountStore(
        config.market_database_path,
        max_symbols_per_user=config.market_max_symbols_per_user,
        max_total_symbols=config.market_max_total_symbols,
    )
    trading_calendar = LocalTradingCalendar(config.trading_calendar_path)

    def market_is_open() -> bool:
        local = datetime.now(timezone.utc).astimezone(SHANGHAI_TZ)
        return trading_calendar.is_trading_day(local.date()) and is_china_market_open(local)

    def market_enrichment_is_open() -> bool:
        local = datetime.now(timezone.utc).astimezone(SHANGHAI_TZ)
        if not trading_calendar.is_trading_day(local.date()):
            return False
        minute = local.hour * 60 + local.minute
        return (
            9 * 60 + 30 <= minute <= 11 * 60 + 31
            or 13 * 60 <= minute <= 15 * 60 + 5
        )

    def is_research_post_close_window(moment: datetime) -> bool:
        local = moment.astimezone(SHANGHAI_TZ)
        if not trading_calendar.is_trading_day(local.date()):
            return False
        minute = local.hour * 60 + local.minute
        return 15 * 60 <= minute <= 15 * 60 + 5

    def is_fund_flow_post_close_window(moment: datetime) -> bool:
        local = moment.astimezone(SHANGHAI_TZ)
        if not trading_calendar.is_trading_day(local.date()):
            return False
        minute = local.hour * 60 + local.minute
        return 15 * 60 + 1 <= minute <= 15 * 60 + 6

    def previous_trading_date(value: str) -> str:
        current = datetime.strptime(value, "%Y%m%d").date()
        current -= timedelta(days=1)
        while not trading_calendar.is_trading_day(current):
            current -= timedelta(days=1)
        return current.strftime("%Y%m%d")

    def resolve_trading_date(current: date) -> str:
        if trading_calendar.is_trading_day(current):
            return current.strftime("%Y%m%d")
        return previous_trading_date(current.strftime("%Y%m%d"))

    def latest_settled_trade_date() -> str:
        local = datetime.now(timezone.utc).astimezone(SHANGHAI_TZ)
        if (
            trading_calendar.is_trading_day(local.date())
            and (local.hour * 60 + local.minute) < 9 * 60 + 30
        ):
            return previous_trading_date(local.strftime("%Y%m%d"))
        return resolve_trading_date(local.date())

    def expected_intraday_trade_date() -> str | None:
        local = datetime.now(timezone.utc).astimezone(SHANGHAI_TZ)
        if not trading_calendar.is_trading_day(local.date()):
            return None
        hhmm = local.strftime("%H%M")
        if not ("0930" <= hhmm <= "1505"):
            return None
        return local.strftime("%Y%m%d")

    def intraday_minute_cap(trade_date: str) -> str | None:
        now_sh = datetime.now(timezone.utc).astimezone(SHANGHAI_TZ)
        return intraday_fund_flow_cap_minute(trade_date, now_sh)

    def _supports_force(fn: object) -> bool:
        try:
            sig = inspect.signature(fn)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return "force" in sig.parameters or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )

    research_store = SQLiteResearchStore(
        config.research_database_path,
        resolve_trade_date=resolve_trading_date,
    )
    push_dispatcher = PushDispatcher()
    session_notifier = SessionHealthNotifier(
        research_store,
        push_dispatcher,
    )

    market_phase_resolver = MarketPhaseResolver(
        is_trading_day=trading_calendar.is_trading_day,
    )
    account_session_provider = None
    account_session_refreshers: dict[str, Callable] = {}
    direct_core_source = None
    fund_flow_source = None
    if config.dual_account_mode:
        assert config.core_frida_server_endpoint is not None
        assert config.fund_frida_server_endpoint is not None
        frida_core_source = FridaParsedValueSource(
            config.core_frida_server_endpoint,
            request_scope="core_metrics",
        )
        frida_fund_source = FridaParsedValueSource(
            config.fund_frida_server_endpoint,
            request_scope="main_fund_flow",
        )
        if (
            config.core_metrics_transport != "frida"
            or config.fund_flow_transport != "frida"
        ):
            assert config.ths_session_encryption_key is not None
            account_session_provider = EncryptedFileSessionProvider(
                config.ths_session_root,
                config.ths_session_encryption_key,
                on_error=lambda role, code: session_notifier.notify_error_background(
                    role, code, stage="直连请求"
                ),
                on_put=lambda bundle: session_notifier.notify_recovered_background(
                    bundle.role, stage="管理后台续签"
                ),
            )
            session_notifier.session_provider = account_session_provider
            account_session_refreshers["main_fund_flow"] = FundAccountSessionRefresher(
                partial(
                    capture_fund_http_session,
                    config.fund_frida_server_endpoint,
                )
            )
            account_session_refreshers["core_metrics"] = CoreAccountSessionRefresher(
                partial(
                    capture_core_session_material,
                    config.core_frida_server_endpoint,
                )
            )
        if config.core_metrics_transport == "frida":
            core_source = frida_core_source
        else:
            assert account_session_provider is not None
            core_protocol = Core9528TemplateProtocol(
                response_decoder=Core9528CurveDecoder(),
            )
            direct_core_source = Core9528Client(
                account_session_provider,
                protocol=core_protocol,
                warm_pool=Core9528WarmPool(
                    core_protocol,
                    max_idle_seconds=(
                        config.core_warm_connection_max_idle_seconds
                    ),
                    max_size=2,
                ),
            )
            core_source = (
                direct_core_source
                if config.core_metrics_transport == "direct"
                else ShadowParsedValueSource(
                    frida_core_source,
                    direct_core_source,
                    role="core_metrics",
                )
            )
        if config.fund_flow_transport == "frida":
            fund_source = frida_fund_source
        else:
            assert account_session_provider is not None
            direct_fund_source = FundFlowHttpClient(
                account_session_provider,
                minimum_interval_seconds=config.market_direct_enrichment_ttl_seconds,
                three_day_interval_seconds=config.fund_flow_three_day_interval_seconds,
                five_day_interval_seconds=config.fund_flow_five_day_interval_seconds,
                max_requests_per_second=config.fund_flow_max_requests_per_second,
            )
            fund_source = (
                direct_fund_source
                if config.fund_flow_transport == "direct"
                else ShadowParsedValueSource(
                    frida_fund_source,
                    direct_fund_source,
                    role="main_fund_flow",
                )
            )
        fund_flow_source = fund_source
        database_first_fund_source = DatabaseFirstFundFlowSource(
            fund_source,
            market_accounts,
            research_store,
            is_market_open=market_is_open,
        )
        parsed_value_source = DualAccountParsedValueSource(
            core_source,
            fund_source,
            fund_read_source=database_first_fund_source,
            symbol_source=symbol_catalog,
            fund_market_interval_seconds=config.market_direct_enrichment_ttl_seconds,
        )
    else:
        parsed_value_source = (
            FridaParsedValueSource(config.frida_server_endpoint)
            if config.frida_server_endpoint
            else None
        )
        fund_flow_source = parsed_value_source
        database_first_fund_source = (
            DatabaseFirstFundFlowSource(
                parsed_value_source,
                market_accounts,
                research_store,
                is_market_open=market_is_open,
            )
            if parsed_value_source is not None
            else None
        )
        if database_first_fund_source is not None:
            parsed_value_source = database_first_fund_source
    runner = runner_factory(
        store,
        navigator,
        config.capture_root,
        control,
        parsed_value_source=parsed_value_source,
        daily_check_state=DailyCheckState(config.daily_check_state_file),
        long_capture_validator=long_capture_has_net_heading,
    )
    market_sessions = RedisMarketSessionStore(redis_client)
    public_market_source = PublicMarketDataSource(
        symbol_catalog,
        TencentPublicMarketProvider(
            timeout_seconds=config.public_market_timeout_seconds
        ),
        SinaPublicQuoteProvider(
            timeout_seconds=config.public_market_timeout_seconds
        ),
        database_fallback=(
            research_store.latest_market_snapshot
            if callable(getattr(research_store, "latest_market_snapshot", None))
            else None
        ),
        is_watchlist_symbol=lambda symbol: symbol
        in set(market_accounts.list_watchlist_symbols()),
    )
    direct_market_enrichment = (
        parsed_value_source
        if config.market_direct_enrichment
        and config.core_metrics_transport == "direct"
        and config.fund_flow_transport == "direct"
        else None
    )

    def closed_market_enrichment(symbol: str) -> DirectReadOutcome | None:
        expected_date = latest_settled_trade_date()
        stored = research_store.latest_market_enrichment(
            symbol,
            expected_trade_date=expected_date,
        )
        if stored is not None and any(
            stored.values.get(kind) is not None for kind in FUND_ONLY_METRICS
        ):
            return stored
        if is_exchange_fund_symbol(symbol):
            return stored
        read_fund = getattr(fund_flow_source, "read_direct", None) if fund_flow_source is not None else None
        if not callable(read_fund):
            if database_first_fund_source is None:
                return stored
            read_fund = database_first_fund_source.read_direct
        try:
            raw_fund = (
                read_fund(symbol, force=True)
                if _supports_force(read_fund)
                else read_fund(symbol)
            )
        except Exception:
            return stored
        fund = (
            raw_fund
            if isinstance(raw_fund, DirectReadOutcome)
            else DirectReadOutcome(
                values=raw_fund,
                source_errors={"core_metrics": None, "main_fund_flow": None},
            )
        )
        periods = DatabaseFirstFundFlowSource._periods_from_outcome(
            fund,
            allow_today=True,
        )
        if periods and symbol in set(market_accounts.list_watchlist_symbols()):
            now_sh = datetime.now(timezone.utc).astimezone(SHANGHAI_TZ)
            settled_minute = settled_fund_flow_minute(expected_date, now_sh)
            if settled_minute is not None:
                try:
                    research_store.record_fund_flow(
                        symbol,
                        trade_date=expected_date,
                        minute=settled_minute,
                        periods=periods,
                        captured_at=now_sh,
                    )
                except Exception:
                    pass
        fund_trade_dates = {
            **fund.stored_trade_dates,
            "main_fund_flow": expected_date,
        }
        if stored is None:
            return replace(fund, stored_trade_dates=fund_trade_dates)
        values = dict(stored.values)
        values.update({kind: fund.values.get(kind) for kind in FUND_ONLY_METRICS})
        return DirectReadOutcome(
            values=values,
            source_errors={"core_metrics": None, "main_fund_flow": None},
            intraday_series={**stored.intraday_series, **fund.intraday_series},
            stored_trade_dates={
                **stored.stored_trade_dates,
                **fund_trade_dates,
            },
        )

    def intraday_core_fallback(symbol: str) -> DirectReadOutcome | None:
        today = datetime.now(SHANGHAI_TZ).strftime("%Y%m%d")
        stored = research_store.latest_market_enrichment(
            symbol,
            expected_trade_date=today,
        )
        if stored is None:
            return None
        if stored.stored_trade_dates.get("core_metrics") != today:
            return None
        return stored

    def intraday_fund_flow_fallback(symbol: str) -> dict[str, object] | None:
        now_sh = datetime.now(SHANGHAI_TZ)
        return research_store.latest_fund_flow_snapshot(
            symbol,
            now=now_sh,
            trade_date=now_sh.strftime("%Y%m%d"),
        )

    enriched_market_source = DirectEnrichedMarketDataSource(
        public_market_source,
        direct_market_enrichment,
        ttl_seconds=config.market_direct_enrichment_ttl_seconds,
        is_market_open=market_enrichment_is_open,
        is_watchlist_symbol=lambda symbol: symbol
        in set(market_accounts.list_watchlist_symbols()),
        closed_enrichment=closed_market_enrichment,
        core_fallback=intraday_core_fallback,
        fund_flow_fallback=intraday_fund_flow_fallback,
    )
    public_market_data_source = DailyKlineMarketDataSource(
        enriched_market_source,
        TonghuashunPublicDailyKlineProvider(),
        is_market_open=market_is_open,
    )
    market_source = (
        PreopenMarketDataSource(
            public_market_data_source,
            preopen_client=(
                CoreDirectPreopenQuoteClient(direct_core_source)
                if config.market_preopen_transport == "direct"
                and direct_core_source is not None
                else None
            ),
            enforce_preopen=config.market_preopen_transport == "direct",
            phase_resolver=market_phase_resolver,
            cache=(
                RedisPreopenQuoteCache(redis_client)
                if config.market_preopen_transport != "off"
                else None
            ),
        )
        if config.market_preopen_transport != "off"
        else public_market_data_source
    )
    market_broker = MarketDataBroker(
        market_source,
        is_market_open=market_is_open,
    )
    if runner is not None and hasattr(runner, "market_snapshot_source"):
        runner.market_snapshot_source = market_source
    if runner is not None and hasattr(runner, "market_phase_resolver"):
        runner.market_phase_resolver = market_phase_resolver

    def post_close_backfill(symbol: str):
        base_snap = public_market_source.read_market_snapshot(symbol, detail=True)
        if is_exchange_fund_symbol(symbol, base_snap.market):
            capabilities = dict(base_snap.capabilities)
            capabilities["l2"] = {
                "available": False,
                "reason": "FUND_L2_UNSUPPORTED",
            }
            merged = replace(
                base_snap,
                capabilities=capabilities,
                source_errors={
                    **base_snap.source_errors,
                    "core_metrics": None,
                    "main_fund_flow": None,
                },
            )
            market_broker.seed(merged)
            return merged
        if parsed_value_source is None:
            market_broker.seed(base_snap)
            return base_snap
        best_effort = getattr(parsed_value_source, "read_direct_best_effort", None)
        outcome = (
            best_effort(symbol)
            if callable(best_effort)
            else parsed_value_source.read_direct(symbol)
        )
        expected_date = latest_settled_trade_date()
        values = dict(outcome.values)
        source_errors = dict(outcome.source_errors)
        stored_trade_dates = dict(outcome.stored_trade_dates)
        if any(
            values.get(kind) is not None
            for kind in (
                MetricKind.LARGE_ORDER_NET,
                MetricKind.LARGE_ORDER_AMOUNT,
                MetricKind.RETAIL_COUNT,
                MetricKind.MACDFS,
            )
        ) or any(series.get("points") for series in outcome.intraday_series.values()):
            stored_trade_dates["core_metrics"] = expected_date
        read_fund = getattr(fund_flow_source, "read_direct", None) if fund_flow_source is not None else None
        if callable(read_fund):
            try:
                raw_fund = (
                    read_fund(symbol, force=True)
                    if _supports_force(read_fund)
                    else read_fund(symbol)
                )
                fund_outcome = (
                    raw_fund
                    if isinstance(raw_fund, DirectReadOutcome)
                    else DirectReadOutcome(
                        values=raw_fund,
                        source_errors={"core_metrics": None, "main_fund_flow": None},
                    )
                )
                for kind in FUND_ONLY_METRICS:
                    values[kind] = fund_outcome.values.get(kind)
                source_errors["main_fund_flow"] = fund_outcome.source_errors.get("main_fund_flow")
                periods = DatabaseFirstFundFlowSource._periods_from_outcome(
                    fund_outcome,
                    allow_today=True,
                )
                if periods:
                    now_sh = datetime.now(timezone.utc).astimezone(SHANGHAI_TZ)
                    settled_minute = settled_fund_flow_minute(expected_date, now_sh)
                    if settled_minute is not None:
                        research_store.record_fund_flow(
                            symbol,
                            trade_date=expected_date,
                            minute=settled_minute,
                            periods=periods,
                            captured_at=now_sh,
                        )
                        stored_trade_dates["main_fund_flow"] = expected_date
            except Exception as error:
                source_errors["main_fund_flow"] = (
                    getattr(error, "error_code", None)
                    or source_errors.get("main_fund_flow")
                    or "DIRECT_FUND_FLOW_REQUEST_FAILED"
                )
        final_outcome = DirectReadOutcome(
            values=values,
            source_errors=source_errors,
            intraday_series=dict(outcome.intraday_series),
            stored_trade_dates=stored_trade_dates,
        )
        merged = enriched_market_source._merge(base_snap, final_outcome)
        research_store.record_snapshot(merged)
        market_broker.seed(merged)
        return merged

    def _on_post_close_finalized(symbols: list[str]) -> None:
        if database_first_fund_source is not None:
            database_first_fund_source.invalidate()
        enriched_market_source.invalidate_cache()
        for sym in symbols:
            market_broker.evict(sym)

    research_monitor = ResearchMonitor(
        market_accounts,
        market_broker,
        research_store,
        dispatcher=push_dispatcher,
        symbol_lookup=symbol_catalog.lookup,
        max_concurrent=1,
        is_market_open=market_enrichment_is_open,
        is_post_close_window=is_research_post_close_window,
    )
    fund_flow_monitor = (
        FundFlowHistoryMonitor(
            market_accounts,
            fund_flow_source,
            research_store,
            is_market_open=market_is_open,
            is_post_close_window=is_fund_flow_post_close_window,
            on_post_close_finalized=_on_post_close_finalized,
            session_notifier=session_notifier,
        )
        if fund_flow_source is not None
        else None
    )
    premium_monitor = PremiumMonitor(
        PublicPremiumProvider(timeout_seconds=config.public_market_timeout_seconds),
        research_store,
        push_dispatcher,
        is_trading_day=trading_calendar.is_trading_day,
    )
    device_lifecycle = (
        DeviceLifecycleClient(
            config.device_lifecycle_url,
            config.device_lifecycle_token,
            timeout_seconds=config.device_lifecycle_timeout_seconds,
        )
        if config.device_lifecycle_url is not None
        and config.device_lifecycle_token is not None
        else None
    )
    def invalidate_fund_session() -> None:
        if config.dual_account_mode:
            if hasattr(parsed_value_source, "invalidate_fund"):
                parsed_value_source.invalidate_fund()
        elif parsed_value_source is not None and hasattr(parsed_value_source, "invalidate"):
            parsed_value_source.invalidate()
        enriched_market_source.invalidate_cache()

    etf_flow_service = TonghuashunEtfFundFlowService(
        cache_path=config.research_database_path.parent / "etf-fund-flow.db",
        timeout_seconds=config.public_market_timeout_seconds,
        is_trading_day=trading_calendar.is_trading_day,
    )
    etf_flow_monitor = EtfFundFlowMonitor(
        market_accounts,
        etf_flow_service,
        is_trading_day=trading_calendar.is_trading_day,
    )

    def _resolve_symbol_by_name(name: str) -> str | None:
        cleaned = (name or "").strip()
        if not cleaned:
            return None
        try:
            matches = symbol_catalog.search(cleaned, 5)
        except Exception:
            return None
        for item in matches:
            if item.name == cleaned:
                return item.symbol
        for item in matches:
            if item.name.startswith(cleaned) or cleaned.startswith(item.name):
                return item.symbol
        return None

    trade_serial = (
        config.core_adb_serial if config.dual_account_mode else config.adb_serial
    ) or config.adb_serial
    trade_executor = (
        TonghuashunTradeExecutor(
            adb_serial=trade_serial,
            prohibited_serial=config.fund_adb_serial,
            runner_control=control,
            task_store=store,
            adb_server_socket=config.adb_server_socket,
            u2_endpoint=config.core_u2_endpoint,
            runner_yield_timeout_seconds=2.0,
            name_to_symbol=_resolve_symbol_by_name,
            broker_trade_password=config.broker_trade_password,
        )
        if trade_serial and trade_serial != config.fund_adb_serial
        else None
    )
    session_probe = (
        PreopenSessionProbe(
            is_trading_day=trading_calendar.is_trading_day,
            accounts=market_accounts,
            fund_source=fund_flow_source if config.fund_flow_transport == "direct" else None,
            core_source=direct_core_source if config.core_metrics_transport == "direct" else None,
            session_provider=account_session_provider,
            notifier=session_notifier,
            trade_executor=trade_executor,
        )
        if config.dual_account_mode and account_session_provider is not None
        else None
    )
    auto_trade_store = SQLiteAutoTradeStore(config.research_database_path)
    quant_rule_store = SQLiteQuantRuleStore(config.research_database_path)
    brent_monitor = BrentMarketMonitor(
        research_store,
        auto_backfill=True,
    )
    order_book_provider = TencentPublicMarketProvider(
        timeout_seconds=min(3.0, config.public_market_timeout_seconds)
    )

    def _safe_lookup_symbol(sym: str) -> Any:
        try:
            return symbol_catalog.lookup(sym)
        except Exception:
            return None

    def _fetch_order_books_batch(syms: Sequence[str]) -> dict[str, dict[str, Any]]:
        identities = [looked for sym in syms if (looked := _safe_lookup_symbol(sym)) is not None]
        if not identities:
            return {}
        return order_book_provider.read_order_books_batch(identities)

    auto_trade_engine = AutoTradeEngine(
        auto_store=auto_trade_store,
        research_store=research_store,
        broker=market_broker,
        trade_executor=trade_executor,
        push_dispatcher=push_dispatcher,
        symbol_lookup=symbol_catalog.lookup,
        is_trading_day=trading_calendar.is_trading_day,
        quant_rule_store=quant_rule_store,
        order_book_fetcher=lambda sym, _mkt=None: order_book_provider.read_order_book(
            symbol_catalog.lookup(sym)
        ),
        batch_order_book_fetcher=_fetch_order_books_batch,
    )

    app = create_app(
        store=store,
        admin_password_hash=config.admin_password_hash,
        password_persist_path=config.admin_password_file,
        admin_session_secret=config.admin_session_secret,
        capture_root=config.capture_root,
        device_bridge=core_bridge,
        device_bridges=device_bridges,
        device_health_probes=device_health_probes,
        runner_control=control,
        runner=runner,
        runner_poll_interval_seconds=config.runner_poll_interval_seconds,
        frontend_root=config.frontend_root,
        secure_admin_cookies=config.admin_cookie_secure,
        symbol_search=symbol_catalog.search,
        symbol_lookup=symbol_catalog.lookup,
        symbol_lookup_cache=None,
        symbol_catalog=symbol_catalog,
        symbol_catalog_refresh_hour=config.symbol_catalog_refresh_hour,
        symbol_catalog_refresh_minute=config.symbol_catalog_refresh_minute,
        previous_trading_date=previous_trading_date,
        expected_intraday_trade_date=expected_intraday_trade_date,
        intraday_minute_cap=intraday_minute_cap,
        core_prewarmer=(
            direct_core_source.prewarm
            if direct_core_source is not None
            else None
        ),
        core_session_invalidator=(
            direct_core_source.invalidate
            if direct_core_source is not None
            else None
        ),
        fund_session_invalidator=invalidate_fund_session,
        managed_resources=tuple(
            {
                id(resource): resource
                for resource in (
                    direct_core_source,
                    fund_flow_source,
                    parsed_value_source,
                    etf_flow_service,
                    research_store,
                    auto_trade_store,
                    quant_rule_store,
                )
                if resource is not None and callable(getattr(resource, "close", None))
            }.values()
        ),
        market_account_store=market_accounts,
        market_session_store=market_sessions,
        market_data_broker=market_broker,
        research_store=research_store,
        research_monitor=research_monitor,
        fund_flow_monitor=fund_flow_monitor,
        research_monitor_interval_seconds=config.research_monitor_interval_seconds,
        fund_flow_monitor_interval_seconds=config.fund_flow_monitor_interval_seconds,
        post_close_backfill=post_close_backfill,
        push_dispatcher=push_dispatcher,
        premium_monitor=premium_monitor,
        session_probe=session_probe,
        account_session_provider=account_session_provider,
        account_session_refreshers=account_session_refreshers,
        device_lifecycle=device_lifecycle,
        request_log_store=RequestLogStore(config.research_database_path),
        goodluck_internal_token=config.goodluck_internal_token,
        etf_flow_service=etf_flow_service,
        etf_flow_monitor=etf_flow_monitor,
        trade_executor=trade_executor,
        auto_trade_engine=auto_trade_engine,
        quant_rule_store=quant_rule_store,
        brent_monitor=brent_monitor,
    )
    app.state.deployment_settings = config
    app.state.runner = runner
    return app
