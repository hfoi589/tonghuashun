"""Production-only assembly for the API, Redis queue, ADB bridge, and runner."""

from __future__ import annotations

import os
import socket
from math import isfinite
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Callable, Mapping

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
from .direct_market import (
    Core9528Client,
    Core9528CurveDecoder,
    Core9528TemplateProtocol,
    Core9528WarmPool,
    FundFlowHttpClient,
    ShadowParsedValueSource,
)
from .market_accounts import RedisMarketSessionStore, SQLiteMarketAccountStore
from .market_data import MarketDataBroker, is_china_market_open
from .parsed_values import DualAccountParsedValueSource, FridaParsedValueSource
from .public_market import (
    DirectEnrichedMarketDataSource,
    PublicMarketDataSource,
    SinaPublicQuoteProvider,
    TencentPublicMarketProvider,
)
from .queue import RedisStreamsStore
from .runner import ADBDeviceBridge, DailyCheckState, Level2Navigator, Level2Runner, OpenCVTemplateFallback, RunnerControl, TAB_LABELS, long_capture_has_net_heading
from .security import persist_password_hash
from .symbol_catalog import SinaSymbolCatalogSource, SQLiteSymbolCatalog


@dataclass(frozen=True)
class DeploymentSettings:
    redis_url: str
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
    symbol_catalog_path: Path
    symbol_catalog_max_age_seconds: float
    symbol_catalog_refresh_hour: int
    symbol_catalog_refresh_minute: int
    public_market_timeout_seconds: float
    market_direct_enrichment: bool
    market_direct_enrichment_ttl_seconds: float
    core_warm_connection_max_idle_seconds: float
    core_metrics_transport: str
    fund_flow_transport: str
    device_lifecycle_url: str | None
    device_lifecycle_token: str | None = field(repr=False)
    device_lifecycle_timeout_seconds: float
    ths_session_encryption_key: str | None = field(repr=False)
    ths_session_root: Path

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
        direct_transport_enabled = any(
            mode != "frida" for mode in transport_modes.values()
        )
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
        positive_values: dict[str, float] = {}
        for name, default in (
            ("SYMBOL_CATALOG_MAX_AGE_SECONDS", "604800"),
            ("PUBLIC_MARKET_TIMEOUT_SECONDS", "8"),
            ("MARKET_DIRECT_ENRICHMENT_TTL_SECONDS", "15"),
            ("CORE_WARM_CONNECTION_MAX_IDLE_SECONDS", "25"),
        ):
            try:
                parsed = float(values.get(name, default))
            except ValueError as error:
                raise ValueError(f"{name} must be a positive number") from error
            if parsed <= 0:
                raise ValueError(f"{name} must be a positive number")
            positive_values[name] = parsed
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
        return cls(
            redis_url=values.get("REDIS_URL", "redis://redis:6379/0"),
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
            symbol_catalog_path=Path(
                values.get(
                    "SYMBOL_CATALOG_PATH",
                    str(market_database_path.with_name("symbol-catalog.db")),
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
            device_lifecycle_url=lifecycle_url,
            device_lifecycle_token=lifecycle_token,
            device_lifecycle_timeout_seconds=lifecycle_timeout,
            ths_session_encryption_key=session_encryption_key or None,
            ths_session_root=session_root,
        )


def _redis_client_from_url(url: str) -> object:
    from redis import Redis

    return Redis.from_url(url)


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
    redis_client_factory: Callable[[str], object] = _redis_client_from_url,
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
    redis_client = redis_client_factory(config.redis_url)
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
    account_session_provider = None
    account_session_refreshers: dict[str, Callable] = {}
    direct_core_source = None
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
            )
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
            direct_fund_source = FundFlowHttpClient(account_session_provider)
            fund_source = (
                direct_fund_source
                if config.fund_flow_transport == "direct"
                else ShadowParsedValueSource(
                    frida_fund_source,
                    direct_fund_source,
                    role="main_fund_flow",
                )
            )
        parsed_value_source = DualAccountParsedValueSource(
            core_source,
            fund_source,
            symbol_source=symbol_catalog,
        )
    else:
        parsed_value_source = (
            FridaParsedValueSource(config.frida_server_endpoint)
            if config.frida_server_endpoint
            else None
        )
    runner = runner_factory(
        store,
        navigator,
        config.capture_root,
        control,
        parsed_value_source=parsed_value_source,
        daily_check_state=DailyCheckState(config.daily_check_state_file),
        long_capture_validator=long_capture_has_net_heading,
    )
    market_accounts = SQLiteMarketAccountStore(config.market_database_path)
    market_sessions = RedisMarketSessionStore(redis_client)
    public_market_source = PublicMarketDataSource(
        symbol_catalog,
        TencentPublicMarketProvider(
            timeout_seconds=config.public_market_timeout_seconds
        ),
        SinaPublicQuoteProvider(
            timeout_seconds=config.public_market_timeout_seconds
        ),
    )
    direct_market_enrichment = (
        parsed_value_source
        if config.market_direct_enrichment
        and config.core_metrics_transport == "direct"
        and config.fund_flow_transport == "direct"
        else None
    )
    market_broker = MarketDataBroker(
        DailyKlineMarketDataSource(
            DirectEnrichedMarketDataSource(
                public_market_source,
                direct_market_enrichment,
                ttl_seconds=config.market_direct_enrichment_ttl_seconds,
            ),
            TonghuashunPublicDailyKlineProvider(),
            is_market_open=is_china_market_open,
        ),
        is_market_open=is_china_market_open,
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
        managed_resources=(
            (direct_core_source,)
            if direct_core_source is not None
            else ()
        ),
        market_account_store=market_accounts,
        market_session_store=market_sessions,
        market_data_broker=market_broker,
        account_session_provider=account_session_provider,
        account_session_refreshers=account_session_refreshers,
        device_lifecycle=device_lifecycle,
    )
    app.state.deployment_settings = config
    app.state.runner = runner
    return app
