#!/usr/bin/env python3
"""Whitelisted macOS emulator lifecycle operations.

This module deliberately exposes only two actions for two fixed emulator roles.
It is usable by the later HTTP broker, but contains no HTTP or caller-provided
command construction.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import argparse
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time
from typing import Mapping, Protocol
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


PACKAGE_ACTIVITY = "com.hexin.plat.android/com.hexin.plat.android.LogoEmptyActivity"
PACKAGE_NAME = "com.hexin.plat.android"
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from macos_device_identity import (
    DarwinProcessExecutableResolver,
    FixedAvdIdentityVerifier,
    FixedAvdPresence,
    IdentityVerificationError,
    ProcessExecutableResolver,
)

MAX_REQUEST_BODY_BYTES = 1024


class LifecycleState(str, Enum):
    UNCONFIGURED = "UNCONFIGURED"
    UNKNOWN = "UNKNOWN"
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class LifecycleAction(str, Enum):
    START_AND_LAUNCH_APP = "start_and_launch_app"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class DeviceConfig:
    role: str
    avd_name: str
    serial: str
    emulator_port: int
    frida_host_port: int
    calibrate_display: bool = False


@dataclass(frozen=True)
class HostSettings:
    bind_host: str
    port: int
    token: str
    environment: Mapping[str, str]


@dataclass
class DeviceOperation:
    operation_id: str
    role: str
    action: LifecycleAction
    state: LifecycleState
    error_code: str | None
    updated_at: datetime

    def public_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "role": self.role,
            "action": self.action.value,
            "state": self.state.value,
            "error_code": self.error_code,
            "updated_at": self.updated_at.isoformat(),
        }


class CommandRunner(Protocol):
    def run(
        self, args: tuple[str, ...], timeout: float
    ) -> subprocess.CompletedProcess[bytes]: ...


class SubprocessCommandRunner:
    """The sole production process boundary; it never invokes a shell."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = dict(environment) if environment is not None else {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        }

    def run(
        self, args: tuple[str, ...], timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            args,
            shell=False,
            env=self._environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )


class LifecycleRequestError(ValueError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class LifecycleFailure(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


FIXED_CONFIGS = {
    "core_metrics": DeviceConfig(
        "core_metrics", "THS_CORE_33_ARM64", "emulator-5556", 5556, 27043, True
    ),
    "main_fund_flow": DeviceConfig(
        "main_fund_flow", "THS_API_33_ARM64", "emulator-5554", 5554, 27042, False
    ),
}
TRUSTED_EMULATOR_BINS = frozenset(
    {
        "emulator",
        "/opt/homebrew/share/android-commandlinetools/emulator/emulator",
    }
)


class DeviceLifecycleManager:
    def __init__(
        self,
        runner: CommandRunner,
        *,
        emulator_bin: str = "emulator",
        adb_bin: str = "adb",
        configs: Mapping[str, DeviceConfig] | None = None,
        boot_timeout_seconds: float = 180.0,
        shutdown_timeout_seconds: float = 60.0,
        command_timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 1.0,
        trusted_emulator_path: Path | None = None,
        process_executable_resolver: ProcessExecutableResolver | None = None,
    ) -> None:
        if configs is not None and dict(configs) != FIXED_CONFIGS:
            raise LifecycleRequestError("DEVICE_LIFECYCLE_UNCONFIGURED")
        if emulator_bin not in TRUSTED_EMULATOR_BINS:
            raise LifecycleRequestError("DEVICE_LIFECYCLE_UNCONFIGURED")
        if not isinstance(adb_bin, str) or not adb_bin:
            raise LifecycleRequestError("DEVICE_LIFECYCLE_UNCONFIGURED")
        self._runner = runner
        self._emulator_bin = emulator_bin
        self._adb_bin = adb_bin
        if trusted_emulator_path is None:
            resolved = (
                emulator_bin
                if Path(emulator_bin).is_absolute()
                else shutil.which(emulator_bin)
            )
            if not resolved:
                raise LifecycleRequestError("DEVICE_LIFECYCLE_UNCONFIGURED")
            trusted_emulator_path = Path(resolved)
        try:
            self.FixedAvdIdentityVerifier = FixedAvdIdentityVerifier
            self._identity = FixedAvdIdentityVerifier(
                runner,
                trusted_emulator_path,
                process_executable_resolver
                or DarwinProcessExecutableResolver(),
                timeout_seconds=command_timeout_seconds,
            )
        except IdentityVerificationError:
            raise LifecycleRequestError("DEVICE_LIFECYCLE_UNCONFIGURED") from None
        self._configs = dict(FIXED_CONFIGS)
        self._boot_timeout_seconds = boot_timeout_seconds
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._command_timeout_seconds = command_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._operations: dict[str, DeviceOperation] = {}
        self._latest_operations: dict[str, DeviceOperation] = {}
        self._states = {role: LifecycleState.UNKNOWN for role in self._configs}
        self._role_busy = {role: False for role in self._configs}
        self._lock = threading.Lock()

    def devices(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for role, config in self._configs.items():
            with self._lock:
                busy = self._role_busy[role]
                saved_state = self._states[role]
                latest = self._latest_operations.get(role)
            preserve_error = (
                not busy
                and saved_state is LifecycleState.ERROR
                and latest is not None
                and latest.error_code is not None
            )
            state = (
                saved_state
                if busy or preserve_error
                else self._detect_state(config)
            )
            if not busy and not preserve_error:
                with self._lock:
                    self._states[role] = state
            result.append(
                {
                    "role": role,
                    "state": state.value,
                    "operation_id": latest.operation_id if latest else None,
                    "error_code": latest.error_code if latest else None,
                    "updated_at": (
                        latest.updated_at.isoformat() if latest else None
                    ),
                }
            )
        return result

    def submit(self, role: str, action: str) -> DeviceOperation:
        config = self._configs.get(role)
        if config is None:
            raise LifecycleRequestError("DEVICE_ROLE_NOT_FOUND")
        try:
            parsed_action = LifecycleAction(action)
        except ValueError as exc:
            raise LifecycleRequestError("DEVICE_ACTION_INVALID") from exc
        with self._lock:
            if self._role_busy[role]:
                raise LifecycleRequestError("DEVICE_ACTION_IN_PROGRESS")
            initial_state = (
                LifecycleState.STARTING
                if parsed_action is LifecycleAction.START_AND_LAUNCH_APP
                else LifecycleState.STOPPING
            )
            operation = DeviceOperation(
                secrets.token_urlsafe(18), role, parsed_action, initial_state, None, self._now()
            )
            self._operations[operation.operation_id] = operation
            self._latest_operations[role] = operation
            self._role_busy[role] = True
            self._states[role] = initial_state
        worker = threading.Thread(
            target=self._run_operation, args=(operation.operation_id, config), daemon=True
        )
        worker.start()
        return operation

    def operation(self, operation_id: str) -> DeviceOperation | None:
        with self._lock:
            return self._operations.get(operation_id)

    def _run_operation(self, operation_id: str, config: DeviceConfig) -> None:
        with self._lock:
            operation = self._operations[operation_id]
        try:
            if operation.action is LifecycleAction.START_AND_LAUNCH_APP:
                final_state = self._start_and_launch_app(config)
            else:
                final_state = self._shutdown(config)
            self._finish(operation_id, final_state, None)
        except LifecycleFailure as exc:
            self._finish(operation_id, LifecycleState.ERROR, exc.error_code)
        except (subprocess.SubprocessError, OSError):
            self._finish(operation_id, LifecycleState.ERROR, "DEVICE_LIFECYCLE_FAILED")

    def _finish(
        self, operation_id: str, state: LifecycleState, error_code: str | None
    ) -> None:
        with self._lock:
            operation = self._operations[operation_id]
            operation.state = state
            operation.error_code = error_code
            operation.updated_at = self._now()
            self._states[operation.role] = state
            self._latest_operations[operation.role] = operation
            self._role_busy[operation.role] = False

    def _start_and_launch_app(self, config: DeviceConfig) -> LifecycleState:
        initial_state = self._detect_state(config)
        if initial_state is LifecycleState.STOPPED:
            self._require_avd(config)
            self._run_required(
                (
                    "launchctl", "submit", "-l", f"com.ths.avd.{config.emulator_port}", "--",
                    self._emulator_bin, "-avd", config.avd_name, "-port", str(config.emulator_port),
                    "-no-snapshot", "-no-audio", "-gpu", "host", "-memory", "2048", "-cores", "4",
                ),
                "DEVICE_LIFECYCLE_FAILED",
            )
            self._wait_for_boot(config, time.monotonic() + self._boot_timeout_seconds)
        elif initial_state is LifecycleState.STARTING:
            self._wait_for_boot(config, time.monotonic() + self._boot_timeout_seconds)
        elif initial_state is LifecycleState.UNKNOWN:
            raise LifecycleFailure("DEVICE_LIFECYCLE_FAILED")
        self._require_identity(config)
        if config.calibrate_display:
            self._run_required(
                (
                    str(SCRIPT_DIRECTORY / "configure-macos-core-display.sh"),
                    config.serial,
                    self._adb_bin,
                ),
                "DEVICE_LIFECYCLE_FAILED",
            )
        self._repair_bridge(config)
        self._launch_app(config)
        if not self._app_is_running(config):
            raise LifecycleFailure("DEVICE_APP_LAUNCH_FAILED")
        return LifecycleState.RUNNING

    def _shutdown(self, config: DeviceConfig) -> LifecycleState:
        state = self._detect_state(config)
        if state is LifecycleState.STOPPED:
            return LifecycleState.STOPPED
        if state is LifecycleState.UNKNOWN:
            raise LifecycleFailure("DEVICE_LIFECYCLE_FAILED")
        self._require_identity(config)
        self._run(("adb", "-s", config.serial, "emu", "kill"))
        # ``launchctl submit`` keeps the fixed emulator label alive and can
        # immediately respawn QEMU after the emulator console accepts ``kill``.
        # Remove only this service-owned transient label so the normal shutdown
        # can settle at STOPPED instead of being reported as a timeout.
        self._run(("launchctl", "remove", f"com.ths.avd.{config.emulator_port}"))
        deadline = time.monotonic() + self._shutdown_timeout_seconds
        while time.monotonic() < deadline:
            if self._detect_state(config) is LifecycleState.STOPPED:
                return LifecycleState.STOPPED
            time.sleep(self._poll_interval_seconds)
        raise LifecycleFailure("DEVICE_SHUTDOWN_FAILED")

    def _wait_for_boot(self, config: DeviceConfig, deadline: float) -> None:
        while time.monotonic() < deadline:
            if self._adb_state(config) == "device" and self._boot_completed(config):
                self._require_identity(config)
                return
            time.sleep(self._poll_interval_seconds)
        raise LifecycleFailure("DEVICE_BOOT_TIMEOUT")

    def _repair_bridge(self, config: DeviceConfig) -> None:
        self._run_required(
            (
                str(SCRIPT_DIRECTORY / "watch-macos-device-bridge.sh"), "--once",
                config.serial, str(config.frida_host_port), self._adb_bin,
            ),
            "DEVICE_LIFECYCLE_FAILED",
        )

    def _launch_app(self, config: DeviceConfig) -> None:
        self._run(
            (
                "adb", "-s", config.serial, "shell", "am", "start", "-n", PACKAGE_ACTIVITY,
            )
        )

    def _detect_state(self, config: DeviceConfig) -> LifecycleState:
        try:
            identity = self._identity.inspect(
                serial=config.serial,
                expected_avd=config.avd_name,
                emulator_port=config.emulator_port,
            )
            if identity.presence is FixedAvdPresence.ATTACHED:
                if identity.adb_state == "device" and self._boot_completed(config):
                    return LifecycleState.RUNNING
                return LifecycleState.STARTING
            if identity.presence is FixedAvdPresence.STARTING:
                return LifecycleState.STARTING
            return LifecycleState.STOPPED
        except IdentityVerificationError:
            return LifecycleState.UNKNOWN

    def _require_identity(self, config: DeviceConfig) -> None:
        try:
            self._identity.require_attached(
                serial=config.serial, expected_avd=config.avd_name
            )
        except IdentityVerificationError:
            raise LifecycleFailure("DEVICE_LIFECYCLE_FAILED") from None

    def _require_avd(self, config: DeviceConfig) -> None:
        available = self._run((self._emulator_bin, "-list-avds"))
        if available.returncode != 0 or config.avd_name not in self._text(available.stdout).splitlines():
            raise LifecycleFailure("DEVICE_AVD_NOT_FOUND")

    def _adb_state(self, config: DeviceConfig) -> str:
        response = self._run(("adb", "-s", config.serial, "get-state"))
        return self._text(response.stdout).strip() if response.returncode == 0 else ""

    def _boot_completed(self, config: DeviceConfig) -> bool:
        response = self._run(
            ("adb", "-s", config.serial, "shell", "getprop", "sys.boot_completed")
        )
        return response.returncode == 0 and self._text(response.stdout).strip() == "1"

    def _app_is_running(self, config: DeviceConfig) -> bool:
        response = self._run(("adb", "-s", config.serial, "shell", "pidof", PACKAGE_NAME))
        return response.returncode == 0 and bool(self._text(response.stdout).strip())

    def _run(self, args: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
        return self._runner.run(args, self._command_timeout_seconds)

    def _run_required(
        self, args: tuple[str, ...], error_code: str
    ) -> subprocess.CompletedProcess[bytes]:
        response = self._run(args)
        if response.returncode != 0:
            raise LifecycleFailure(error_code)
        return response

    @staticmethod
    def _text(value: bytes | str | None) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return value or ""

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)


def load_settings(config_path: Path) -> HostSettings:
    """Load only the broker settings from its owner-readable host config."""
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit("DEVICE_LIFECYCLE_UNCONFIGURED") from exc
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    token = values.get("THS_DEVICE_LIFECYCLE_TOKEN", "")
    bind_host = values.get("THS_DEVICE_LIFECYCLE_BIND_HOST", "127.0.0.1")
    try:
        port = int(values.get("THS_DEVICE_LIFECYCLE_PORT", "18765"))
    except ValueError as exc:
        raise SystemExit("DEVICE_LIFECYCLE_UNCONFIGURED") from exc
    if not token or not 1 <= port <= 65535:
        raise SystemExit("DEVICE_LIFECYCLE_UNCONFIGURED")
    environment = {"PATH": values.get("PATH", os.environ.get("PATH", ""))}
    if "THS_DEVICE_LIFECYCLE_EMULATOR_BIN" in values:
        environment["THS_DEVICE_LIFECYCLE_EMULATOR_BIN"] = values[
            "THS_DEVICE_LIFECYCLE_EMULATOR_BIN"
        ]
    return HostSettings(bind_host, port, token, environment)


class LifecycleRequestHandler(BaseHTTPRequestHandler):
    """Narrow loopback-only HTTP boundary for the fixed lifecycle manager."""

    manager: DeviceLifecycleManager
    token: str

    def do_GET(self) -> None:  # noqa: N802 - HTTP verb hook
        if not self._authorized():
            return
        route = urlsplit(self.path)
        if route.query:
            self._error(404, "DEVICE_ROUTE_NOT_FOUND")
        elif route.path == "/v1/devices":
            self._send(200, {"devices": self.manager.devices()})
        elif route.path.startswith("/v1/operations/"):
            operation_id = route.path.removeprefix("/v1/operations/")
            if not operation_id or "/" in operation_id:
                self._error(404, "DEVICE_OPERATION_NOT_FOUND")
                return
            operation = self.manager.operation(operation_id)
            if operation is None:
                self._error(404, "DEVICE_OPERATION_NOT_FOUND")
                return
            self._send(200, operation.public_dict())
        else:
            self._error(404, "DEVICE_ROUTE_NOT_FOUND")

    def do_POST(self) -> None:  # noqa: N802 - HTTP verb hook
        if not self._authorized():
            return
        route = urlsplit(self.path)
        prefix = "/v1/devices/"
        suffix = "/actions"
        if route.query or not route.path.startswith(prefix) or not route.path.endswith(suffix):
            self._error(404, "DEVICE_ROUTE_NOT_FOUND")
            return
        role = route.path[len(prefix):-len(suffix)]
        if not role or "/" in role:
            self._error(404, "DEVICE_ROUTE_NOT_FOUND")
            return
        payload = self._action_payload()
        if payload is None:
            return
        if set(payload) != {"action"} or not isinstance(payload["action"], str):
            self._error(422, "DEVICE_ACTION_INVALID")
            return
        try:
            operation = self.manager.submit(role, payload["action"])
        except LifecycleRequestError as exc:
            status = 409 if exc.error_code == "DEVICE_ACTION_IN_PROGRESS" else 422
            self._error(status, exc.error_code)
            return
        self._send(202, operation.public_dict())

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization")
        expected = f"Bearer {self.token}"
        try:
            valid = header is not None and hmac.compare_digest(header, expected)
        except TypeError:
            valid = False
        if not valid:
            self._error(401, "DEVICE_AUTH_REQUIRED")
            return False
        return True

    def _action_payload(self) -> dict[str, object] | None:
        if self.headers.get("Content-Type") != "application/json":
            self._error(400, "DEVICE_ACTION_INVALID")
            return None
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._error(400, "DEVICE_ACTION_INVALID")
            return None
        if length < 0:
            self._error(400, "DEVICE_ACTION_INVALID")
            return None
        if length > MAX_REQUEST_BODY_BYTES:
            self._error(413, "DEVICE_REQUEST_TOO_LARGE")
            return None
        try:
            parsed = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "DEVICE_ACTION_INVALID")
            return None
        if not isinstance(parsed, dict):
            self._error(422, "DEVICE_ACTION_INVALID")
            return None
        return parsed

    def _send(self, status: int, document: Mapping[str, object]) -> None:
        body = json.dumps(document, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, error_code: str) -> None:
        self._send(status, {"detail": error_code})

    def log_message(self, _format: str, *_args: object) -> None:
        """Never log request paths, headers, bodies, or exception details."""


def make_handler(manager: DeviceLifecycleManager, token: str) -> type[LifecycleRequestHandler]:
    if not token:
        raise ValueError("token must not be empty")

    class BoundLifecycleRequestHandler(LifecycleRequestHandler):
        pass

    BoundLifecycleRequestHandler.manager = manager
    BoundLifecycleRequestHandler.token = token
    return BoundLifecycleRequestHandler


def serve(config_path: Path) -> None:
    settings = load_settings(config_path)
    if settings.bind_host != "127.0.0.1":
        raise SystemExit("DEVICE_BIND_NOT_LOOPBACK")
    emulator_bin = settings.environment.get(
        "THS_DEVICE_LIFECYCLE_EMULATOR_BIN", "emulator"
    )
    resolved_emulator = shutil.which(
        emulator_bin, path=settings.environment.get("PATH")
    )
    if not resolved_emulator:
        raise SystemExit("DEVICE_LIFECYCLE_UNCONFIGURED")
    resolved_adb = shutil.which("adb", path=settings.environment.get("PATH"))
    if not resolved_adb:
        raise SystemExit("DEVICE_LIFECYCLE_UNCONFIGURED")
    manager = DeviceLifecycleManager(
        SubprocessCommandRunner(settings.environment),
        emulator_bin=emulator_bin,
        adb_bin=resolved_adb,
        trusted_emulator_path=Path(resolved_emulator),
    )
    server = ThreadingHTTPServer(
        (settings.bind_host, settings.port), make_handler(manager, settings.token)
    )
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    serve(args.config)


if __name__ == "__main__":
    main()
