"""Production-only assembly for the API, Redis queue, ADB bridge, and runner."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from fastapi import FastAPI

from .api import create_app
from .parsed_values import FridaParsedValueSource
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
        adb_serial = values.get("ADB_SERIAL", "").strip()
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
            frida_server_endpoint=values.get("FRIDA_SERVER_ENDPOINT", "").strip() or None,
            daily_check_state_file=Path(
                values.get("DAILY_CHECK_STATE_FILE", "/data/admin/daily-check.json")
            ).expanduser().resolve(),
        )


def _redis_client_from_url(url: str) -> object:
    from redis import Redis

    return Redis.from_url(url)


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
    bridge = bridge_factory(adb=config.adb_path, serial=config.adb_serial, environment=adb_environment)
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
    navigator = Level2Navigator(bridge, OpenCVTemplateFallback(templates))
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
        device_bridge=bridge,
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
