"""Production-only assembly for the API, Redis queue, ADB bridge, and runner."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from fastapi import FastAPI

from .api import create_app
from .queue import RedisStreamsStore
from .runner import ADBDeviceBridge, Level2Navigator, Level2Runner, RunnerControl


@dataclass(frozen=True)
class DeploymentSettings:
    redis_url: str
    capture_root: Path
    admin_password_hash: str
    admin_session_secret: str
    adb_path: str
    adb_serial: str
    adb_server_socket: str | None
    runner_poll_interval_seconds: float

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> "DeploymentSettings":
        values = os.environ if environ is None else environ
        password_hash = values.get("ADMIN_PASSWORD_HASH", "").strip()
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
        return cls(
            redis_url=values.get("REDIS_URL", "redis://redis:6379/0"),
            capture_root=Path(values.get("CAPTURE_ROOT", "/data/captures")).resolve(),
            admin_password_hash=password_hash,
            admin_session_secret=session_secret,
            adb_path=values.get("ADB_PATH", "adb"),
            adb_serial=adb_serial,
            adb_server_socket=values.get("ADB_SERVER_SOCKET") or None,
            runner_poll_interval_seconds=poll_interval,
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
    store = RedisStreamsStore(redis_client_factory(config.redis_url), capture_root=config.capture_root)
    adb_environment = os.environ.copy()
    if config.adb_server_socket:
        adb_environment["ADB_SERVER_SOCKET"] = config.adb_server_socket
    bridge = bridge_factory(adb=config.adb_path, serial=config.adb_serial, environment=adb_environment)
    control = RunnerControl()
    runner = runner_factory(store, Level2Navigator(bridge), config.capture_root, control)
    app = create_app(
        store=store,
        admin_password_hash=config.admin_password_hash,
        admin_session_secret=config.admin_session_secret,
        capture_root=config.capture_root,
        device_bridge=bridge,
        runner_control=control,
        runner=runner,
        runner_poll_interval_seconds=config.runner_poll_interval_seconds,
    )
    app.state.deployment_settings = config
    app.state.runner = runner
    return app
