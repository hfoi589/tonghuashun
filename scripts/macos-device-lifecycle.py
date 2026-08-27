#!/usr/bin/env python3
"""Whitelisted macOS emulator lifecycle operations.

This module deliberately exposes only two actions for two fixed emulator roles.
It is usable by the later HTTP broker, but contains no HTTP or caller-provided
command construction.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import os
from pathlib import Path
import secrets
import subprocess
import threading
import time
from typing import Mapping, Protocol


PACKAGE_ACTIVITY = "com.hexin.plat.android/com.hexin.plat.android.LogoEmptyActivity"
PACKAGE_NAME = "com.hexin.plat.android"
SCRIPT_DIRECTORY = Path(__file__).resolve().parent


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
        configs: Mapping[str, DeviceConfig] | None = None,
        boot_timeout_seconds: float = 180.0,
        shutdown_timeout_seconds: float = 60.0,
        command_timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        if configs is not None and dict(configs) != FIXED_CONFIGS:
            raise LifecycleRequestError("DEVICE_LIFECYCLE_UNCONFIGURED")
        if emulator_bin not in TRUSTED_EMULATOR_BINS:
            raise LifecycleRequestError("DEVICE_LIFECYCLE_UNCONFIGURED")
        self._runner = runner
        self._emulator_bin = emulator_bin
        self._configs = dict(FIXED_CONFIGS)
        self._boot_timeout_seconds = boot_timeout_seconds
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._command_timeout_seconds = command_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._operations: dict[str, DeviceOperation] = {}
        self._states = {role: LifecycleState.UNKNOWN for role in self._configs}
        self._role_busy = {role: False for role in self._configs}
        self._lock = threading.Lock()

    def devices(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for role, config in self._configs.items():
            with self._lock:
                busy = self._role_busy[role]
                saved_state = self._states[role]
            state = saved_state if busy else self._detect_state(config)
            if not busy:
                with self._lock:
                    self._states[role] = state
            result.append({"role": role, "state": state.value})
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
        if config.calibrate_display:
            self._run_required(
                (str(SCRIPT_DIRECTORY / "configure-macos-core-display.sh"), config.serial, "adb"),
                "DEVICE_LIFECYCLE_FAILED",
            )
        self._repair_bridge(config)
        self._launch_app(config)
        if not self._app_is_running(config):
            raise LifecycleFailure("DEVICE_APP_LAUNCH_FAILED")
        return LifecycleState.RUNNING

    def _shutdown(self, config: DeviceConfig) -> LifecycleState:
        if self._detect_state(config) is LifecycleState.STOPPED:
            return LifecycleState.STOPPED
        self._run(("adb", "-s", config.serial, "emu", "kill"))
        deadline = time.monotonic() + self._shutdown_timeout_seconds
        while time.monotonic() < deadline:
            if self._detect_state(config) is LifecycleState.STOPPED:
                return LifecycleState.STOPPED
            time.sleep(self._poll_interval_seconds)
        raise LifecycleFailure("DEVICE_SHUTDOWN_FAILED")

    def _wait_for_boot(self, config: DeviceConfig, deadline: float) -> None:
        while time.monotonic() < deadline:
            if self._adb_state(config) == "device" and self._boot_completed(config):
                return
            time.sleep(self._poll_interval_seconds)
        raise LifecycleFailure("DEVICE_BOOT_TIMEOUT")

    def _repair_bridge(self, config: DeviceConfig) -> None:
        self._run_required(
            (
                str(SCRIPT_DIRECTORY / "watch-macos-device-bridge.sh"), "--once",
                config.serial, str(config.frida_host_port), "adb",
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
            if self._adb_state(config) == "device" and self._boot_completed(config):
                return LifecycleState.RUNNING
            process = self._run(("ps", "-axo", "pid=,command="))
        except (subprocess.SubprocessError, OSError):
            return LifecycleState.UNKNOWN
        if process.returncode != 0:
            return LifecycleState.UNKNOWN
        port_marker = f"-port {config.emulator_port}"
        return (
            LifecycleState.STARTING
            if any(port_marker in line for line in self._text(process.stdout).splitlines())
            else LifecycleState.STOPPED
        )

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
