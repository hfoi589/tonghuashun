"""Production-only assembly for the API, Redis queue, ADB bridge, and runner."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from fastapi import FastAPI

from .api import create_app
from .parsed_values import DualAccountParsedValueSource, FridaParsedValueSource
from .queue import RedisStreamsStore
from .runner import ADBDeviceBridge, DailyCheckState, Level2Navigator, Level2Runner, OpenCVTemplateFallback, RunnerControl, TAB_LABELS, long_capture_has_net_heading
from .security import persist_password_hash
from .symbol_cache import RedisSymbolLookupCache


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
        cookie_secure_value = values.get("ADMIN_COOKIE_SECURE", "1").strip().lower()
        if cookie_secure_value in {"1", "true", "yes", "on"}:
            admin_cookie_secure = True
        elif cookie_secure_value in {"0", "false", "no", "off"}:
            admin_cookie_secure = False
        else:
            raise ValueError("ADMIN_COOKIE_SECURE must be a boolean value")
        return cls(
            redis_url=values.get("REDIS_URL", "redis://redis:6379/0"),
            capture_root=Path(values.get("CAPTURE_ROOT", "/data/captures")).resolve(),
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
) -> FastAPI:
    """Create the only app mode that enables the real Android worker."""
    config = settings or DeploymentSettings.from_environ()
    config.capture_root.mkdir(parents=True, exist_ok=True)
    redis_client = redis_client_factory(config.redis_url)
    store = RedisStreamsStore(redis_client, capture_root=config.capture_root)
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
    if config.dual_account_mode:
        assert config.core_frida_server_endpoint is not None
        assert config.fund_frida_server_endpoint is not None
        parsed_value_source = DualAccountParsedValueSource(
            FridaParsedValueSource(
                config.core_frida_server_endpoint,
                request_scope="core_metrics",
            ),
            FridaParsedValueSource(
                config.fund_frida_server_endpoint,
                request_scope="main_fund_flow",
            ),
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
        symbol_lookup=parsed_value_source.lookup_symbol if parsed_value_source is not None else None,
        symbol_lookup_cache=RedisSymbolLookupCache(redis_client),
    )
    app.state.deployment_settings = config
    app.state.runner = runner
    return app
