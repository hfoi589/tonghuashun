#!/usr/bin/env python3
"""Fail-closed one-command deployment for the two fixed macOS Android roles."""

from __future__ import annotations

import argparse
import base64
import ctypes
from dataclasses import asdict, dataclass
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from typing import Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


APK_SHA256 = "2554490aa3f5e2df17ac0a711311f3f85ee3130008af9bb4ab12510b3d6e971e"
FRIDA_SHA256 = "36ec3d7474b1ac69c4e7ec985612fae771d37ffb71cb94858bc6978f69f5e581"
FRIDA_BINARY_SHA256 = "4eebf1fbc66ff54aba9a9124c2ef8b32b566616388c60e2caa65148a529d826a"
ANDROID_SYSTEM_IMAGE = "system-images;android-33;google_apis;arm64-v8a"
IMAGE_NAME = "ths-level2-api:local"
PACKAGE_NAME = "com.hexin.plat.android"
FIXED_ROLES = {
    "core_metrics": ("THS_CORE_33_ARM64", "emulator-5556"),
    "main_fund_flow": ("THS_API_33_ARM64", "emulator-5554"),
}
FIXED_EMULATOR_PORTS = {
    "core_metrics": 5556,
    "main_fund_flow": 5554,
}
REQUIRED_COMMANDS = (
    "adb",
    "avdmanager",
    "docker",
    "emulator",
    "java",
    "sdkmanager",
)
REQUIRED_ROOT_ENV_KEYS = (
    "ADMIN_PASSWORD_HASH",
    "ADMIN_SESSION_SECRET",
    "THS_SESSION_ENCRYPTION_KEY",
    "THS_DEVICE_LIFECYCLE_TOKEN",
)
REQUIRED_MACOS_ENV = {
    "CORE_ADB_SERIAL": "emulator-5556",
    "CORE_FRIDA_SERVER_ENDPOINT": "host.docker.internal:27043",
    "FUND_ADB_SERIAL": "emulator-5554",
    "FUND_FRIDA_SERVER_ENDPOINT": "host.docker.internal:27042",
    "THS_DEVICE_LIFECYCLE_URL": "http://host.docker.internal:18765",
}
ROOT_ONLY_COMPOSE_KEYS = frozenset(REQUIRED_ROOT_ENV_KEYS)
SANITIZED_AMBIENT_KEYS = ROOT_ONLY_COMPOSE_KEYS | {
    "THS_DEVICE_LIFECYCLE_URL",
}
_SAFE_APK_PATH = re.compile(r"/data/app/[A-Za-z0-9._~+=/-]+/base\.apk")
_SAFE_OPERATION_ID = re.compile(r"[A-Za-z0-9_-]{1,256}")
_SAFE_ADB_SERIAL = re.compile(r"[A-Za-z0-9._:-]+")
_SAFE_ADB_STATES = frozenset(
    {"bootloader", "device", "offline", "recovery", "sideload", "unauthorized"}
)
_ACCEPTANCE_SYMBOL = "601872"
_ACCEPTANCE_REQUIRED_VALUES = (
    "stock_name",
    "current_price",
    "change_percent",
    "turnover_rate",
    "large_order_net",
    "large_order_amount",
    "retail_count",
    "macdfs",
)
_FIRST_TIME_LOGIN_INSTRUCTIONS = (
    "Open http://127.0.0.1:8001/#admin.",
    "Manually log in and complete verification for each newly created role.",
    "Click the matching role's session refresh.",
    "Rerun scripts/provision-macos-from-image.sh.",
)


@dataclass(frozen=True)
class DeploymentResult:
    mode: str
    state: str
    error_code: str | None = None
    instructions: tuple[str, ...] = ()


class DeploymentError(RuntimeError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class CommandRunner(Protocol):
    def run(
        self,
        args: tuple[str, ...],
        timeout: float,
        input_data: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]: ...


class LifecycleBroker(Protocol):
    def device_states(self) -> Mapping[str, str]: ...

    def start_and_launch_app(self, role: str) -> str: ...

    def wait_for_state(
        self, operation_id: str, expected_state: str, timeout_seconds: float
    ) -> None: ...


class DataOnlyAcceptance(Protocol):
    def verify(self) -> None: ...


class FileSystem(Protocol):
    def exists(self, path: Path) -> bool: ...

    def read_text(self, path: Path) -> str: ...

    def mode(self, path: Path) -> int: ...

    def which(self, command: str) -> str | None: ...


class ProcessExecutableResolver(Protocol):
    def resolve(self, pid: int) -> Path: ...


class DarwinProcessExecutableResolver:
    """Resolve a PID to its actual executable image through macOS libproc."""

    _LIBPROC_PATH = "/usr/lib/libproc.dylib"
    _PROC_PIDPATHINFO_MAXSIZE = 4096

    def resolve(self, pid: int) -> Path:
        if sys.platform != "darwin" or pid <= 0:
            raise OSError("proc_pidpath unavailable")
        libproc = ctypes.CDLL(self._LIBPROC_PATH, use_errno=True)
        proc_pidpath = libproc.proc_pidpath
        proc_pidpath.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        proc_pidpath.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(self._PROC_PIDPATHINFO_MAXSIZE)
        length = proc_pidpath(pid, buffer, len(buffer))
        if length <= 0 or length > len(buffer):
            error_number = ctypes.get_errno()
            raise OSError(error_number, "proc_pidpath failed")
        raw_path = buffer.raw[:length].split(b"\0", 1)[0]
        if not raw_path:
            raise OSError("proc_pidpath returned an empty path")
        executable = Path(os.fsdecode(raw_path))
        if not executable.is_absolute():
            raise OSError("proc_pidpath returned a relative path")
        return executable


class SubprocessCommandRunner:
    """Run fixed argument vectors in the selected project checkout without a shell."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root
        self._environment = dict(os.environ)
        for key in SANITIZED_AMBIENT_KEYS:
            self._environment.pop(key, None)

    def run(
        self,
        args: tuple[str, ...],
        timeout: float,
        input_data: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            args,
            cwd=self._project_root,
            env=self._environment,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            input=input_data,
            timeout=timeout,
            check=False,
        )


class PathFileSystem:
    def exists(self, path: Path) -> bool:
        return path.exists()

    def read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def mode(self, path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    def which(self, command: str) -> str | None:
        return shutil.which(command)


class LoopbackLifecycleBroker:
    """Minimal client for the authenticated fixed-role host lifecycle service."""

    def __init__(self, token: str, *, timeout_seconds: float = 5.0) -> None:
        if not token:
            raise DeploymentError("ROOT_ENV_INVALID")
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._base_url = "http://127.0.0.1:18765"

    def device_states(self) -> Mapping[str, str]:
        document = self._request("GET", "/v1/devices")
        devices = document.get("devices")
        if not isinstance(devices, list):
            raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE")
        result: dict[str, str] = {}
        for item in devices:
            if not isinstance(item, dict):
                raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE")
            role = item.get("role")
            state = item.get("state")
            if role not in FIXED_ROLES or not isinstance(state, str):
                raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE")
            result[role] = state
        return result

    def start_and_launch_app(self, role: str) -> str:
        if role not in FIXED_ROLES:
            raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE")
        document = self._request(
            "POST",
            f"/v1/devices/{role}/actions",
            payload={"action": "start_and_launch_app"},
        )
        operation_id = document.get("operation_id")
        if not isinstance(operation_id, str) or not _SAFE_OPERATION_ID.fullmatch(
            operation_id
        ):
            raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE")
        return operation_id

    def wait_for_state(
        self, operation_id: str, expected_state: str, timeout_seconds: float
    ) -> None:
        if not _SAFE_OPERATION_ID.fullmatch(operation_id):
            raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE")
        deadline = time.monotonic() + timeout_seconds
        while True:
            document = self._request("GET", f"/v1/operations/{operation_id}")
            state = document.get("state")
            if state == expected_state:
                return
            if state == "ERROR":
                raise DeploymentError("DEVICE_LIFECYCLE_FAILED")
            if not isinstance(state, str) or time.monotonic() >= deadline:
                raise DeploymentError("DEVICE_LIFECYCLE_TIMEOUT")
            time.sleep(1.0)

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, str] | None = None,
    ) -> dict[str, object]:
        body = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {"Authorization": f"Bearer {self._token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE") from None
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            document = None
        if not isinstance(document, dict):
            raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE")
        return document


class LoopbackDataOnlyAcceptance:
    """Verify one fixed, catalog-confirmed data-only task through the public API."""

    def __init__(
        self,
        *,
        opener: Callable[..., object] | None = None,
        timeout_seconds: float = 180.0,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self._opener = opener or urlopen
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._base_url = "http://127.0.0.1:8001"

    def verify(self) -> None:
        symbol = self._request_json("GET", f"/api/v1/symbols/{_ACCEPTANCE_SYMBOL}")
        if (
            symbol.get("symbol") != _ACCEPTANCE_SYMBOL
            or symbol.get("market") != "17"
            or not isinstance(symbol.get("name"), str)
            or not symbol["name"]
        ):
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
        submitted = self._request_json(
            "POST",
            "/api/v1/jobs",
            payload={
                "symbol": _ACCEPTANCE_SYMBOL,
                "include_long_capture": False,
            },
        )
        public_id = submitted.get("public_id")
        if not isinstance(public_id, str) or not _SAFE_OPERATION_ID.fullmatch(public_id):
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            task = self._request_json("GET", f"/api/v1/jobs/{public_id}")
            status = task.get("status")
            if status == "COMPLETED":
                self._validate_completed_task(task)
                return
            if status not in {"QUEUED", "RUNNING"}:
                raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
            if time.monotonic() >= deadline:
                raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
            time.sleep(max(0.0, self._poll_interval_seconds))

    @staticmethod
    def _validate_completed_task(task: dict[str, object]) -> None:
        values = task.get("values")
        if (
            task.get("symbol") != _ACCEPTANCE_SYMBOL
            or task.get("include_long_capture") is not False
            or not isinstance(values, dict)
            or any(
                not isinstance(values.get(field), str) or not values[field]
                for field in _ACCEPTANCE_REQUIRED_VALUES
            )
        ):
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        body = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {"Content-Type": "application/json"} if body is not None else {}
        request = Request(
            f"{self._base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        request_timeout = max(0.1, min(10.0, self._timeout_seconds))
        try:
            with self._opener(request, request_timeout) as response:  # type: ignore[attr-defined]
                raw = response.read()
            document = json.loads(raw.decode("utf-8"))
        except Exception:
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED") from None
        if not isinstance(document, dict):
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
        return document


class MacDeploymentOrchestrator:
    def __init__(
        self,
        runner: CommandRunner,
        lifecycle_broker: LifecycleBroker | None,
        filesystem: FileSystem,
        *,
        project_root: Path,
        env_file: Path = Path(".env"),
        process_executable_resolver: ProcessExecutableResolver | None = None,
        data_only_acceptance: DataOnlyAcceptance | None = None,
        health_timeout_seconds: float = 180.0,
        boot_timeout_seconds: float = 180.0,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self._runner = runner
        self._broker = lifecycle_broker
        self._filesystem = filesystem
        self._project_root = project_root.resolve()
        self._env_argument = str(env_file)
        self._env_file = (
            env_file.resolve()
            if env_file.is_absolute()
            else (self._project_root / env_file).resolve()
        )
        self._macos_env = (self._project_root / "deploy/macos.env").resolve()
        self._health_timeout_seconds = health_timeout_seconds
        self._boot_timeout_seconds = boot_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._root_environment: dict[str, str] = {}
        self._trusted_emulator_path: Path | None = None
        self._data_only_acceptance = data_only_acceptance
        self._initial_missing_roles: frozenset[str] | None = None
        self._process_executable_resolver = (
            process_executable_resolver or DarwinProcessExecutableResolver()
        )

    @property
    def initial_missing_roles(self) -> frozenset[str]:
        return self._initial_missing_roles or frozenset()

    def deploy(self, mode: str = "auto") -> DeploymentResult:
        self._validate_env_file_location()
        if mode == "existing":
            return self.deploy_existing()
        if mode == "provision":
            return self.provision_missing()
        if mode != "auto":
            raise DeploymentError("DEPLOYMENT_MODE_INVALID")
        self._validate_command_presence()
        initial_missing = self._record_initial_missing_roles(
            self._missing_fixed_roles()
        )
        if not initial_missing:
            return self.deploy_existing()
        return self.provision_missing(initial_missing)

    def deploy_provision(self) -> DeploymentResult:
        return self.provision_missing()

    def provision_missing(
        self,
        initial_missing_roles: frozenset[str] | None = None,
    ) -> DeploymentResult:
        self._validate_env_file_location()
        self._validate_command_presence()
        missing_roles = self._record_initial_missing_roles(
            initial_missing_roles
            if initial_missing_roles is not None
            else self._missing_fixed_roles()
        )
        self._validate_host_prerequisites(provision_system_image=True)
        self._root_environment = self._ensure_root_environment()
        self._validate_macos_environment()
        self._validate_effective_compose_config()
        self._build_local_image()
        manifest = self._read_image_manifest()
        preserved_roles = tuple(
            role for role in FIXED_ROLES if role not in missing_roles
        )
        self._verify_preinstall_fixed_avd_identities(preserved_roles)
        for role in FIXED_ROLES:
            if role not in missing_roles:
                continue
            self._create_fixed_avd(role)
            self._start_created_avd(role)
            self._provision_created_avd(role)
            if role == "core_metrics":
                self._calibrate_created_core()
        self._install_lifecycle_service()
        self._start_and_launch_roles(tuple(FIXED_ROLES))
        self._verify_fixed_avd_identities()
        self._verify_installed_apks(manifest["apk"]["sha256"])
        self._validate_effective_compose_config()
        self._compose_up()
        self._wait_for_compose_health()
        if not self._session_bundles_ready():
            return DeploymentResult(
                mode="provision",
                state="FIRST_TIME_LOGIN_REQUIRED",
                instructions=_FIRST_TIME_LOGIN_INSTRUCTIONS,
            )
        self._verify_data_only_acceptance()
        return DeploymentResult(mode="provision", state="READY")

    def deploy_existing(self) -> DeploymentResult:
        self._validate_env_file_location()
        self._validate_host_prerequisites()
        self._require_fixed_avds()
        self._root_environment = self._ensure_root_environment()
        self._validate_macos_environment()
        self._validate_effective_compose_config()
        self._build_local_image()
        manifest = self._read_image_manifest()
        self._verify_preinstall_fixed_avd_identities()
        self._install_lifecycle_service()
        self._start_stopped_roles()
        self._verify_fixed_avd_identities()
        self._verify_installed_apks(manifest["apk"]["sha256"])
        self._validate_effective_compose_config()
        self._compose_up()
        self._wait_for_compose_health()
        return DeploymentResult(mode="existing", state="READY")

    def _validate_env_file_location(self) -> None:
        try:
            relative = self._env_file.relative_to(self._project_root)
        except ValueError:
            return
        if relative != Path(".env"):
            raise DeploymentError("ENV_FILE_IN_BUILD_CONTEXT")
        dockerignore = self._project_root / ".dockerignore"
        try:
            ignored = self._dockerignore_excludes_root_env(
                self._filesystem.read_text(dockerignore)
            )
        except Exception:
            raise DeploymentError("ROOT_ENV_NOT_IGNORED") from None
        if not ignored:
            raise DeploymentError("ROOT_ENV_NOT_IGNORED")
        self._env_argument = ".env"

    @staticmethod
    def _dockerignore_excludes_root_env(content: str) -> bool:
        excluded = False
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            negated = line.startswith("!")
            pattern = line[1:] if negated else line
            pattern = pattern.removeprefix("/")
            root_pattern = pattern.removeprefix("**/")
            if fnmatch.fnmatchcase(".env", pattern) or fnmatch.fnmatchcase(
                ".env", root_pattern
            ):
                excluded = not negated
        return excluded

    def _validate_command_presence(self) -> None:
        try:
            resolved_commands = {
                command: self._filesystem.which(command)
                for command in REQUIRED_COMMANDS
            }
        except Exception:
            raise DeploymentError("MISSING_PREREQUISITE") from None
        if any(path is None for path in resolved_commands.values()):
            raise DeploymentError("MISSING_PREREQUISITE")
        emulator_path = resolved_commands["emulator"]
        assert emulator_path is not None
        candidate = Path(emulator_path)
        if not candidate.is_absolute() or candidate.name != "emulator":
            raise DeploymentError("MISSING_PREREQUISITE")
        try:
            self._trusted_emulator_path = candidate.resolve()
        except OSError:
            raise DeploymentError("MISSING_PREREQUISITE") from None

    def _validate_host_prerequisites(
        self, *, provision_system_image: bool = False
    ) -> None:
        self._validate_command_presence()
        system = self._run(("uname", "-s"), 10.0, "UNSUPPORTED_HOST")
        architecture = self._run(("uname", "-m"), 10.0, "UNSUPPORTED_HOST")
        if self._stdout_text(system).strip() != "Darwin" or self._stdout_text(
            architecture
        ).strip() not in {"arm64", "aarch64"}:
            raise DeploymentError("UNSUPPORTED_HOST")
        context = self._run(
            ("docker", "--context", "orbstack", "info", "--format", "{{.Name}}"),
            30.0,
            "ORBSTACK_UNAVAILABLE",
        )
        if "orbstack" not in self._stdout_text(context).lower():
            raise DeploymentError("ORBSTACK_UNAVAILABLE")
        java = self._run(("java", "-version"), 10.0, "JAVA_17_REQUIRED")
        java_version = self._stdout_text(java) + self._stderr_text(java)
        if not re.search(r'version\s+"17(?:[."]|$)', java_version):
            raise DeploymentError("JAVA_17_REQUIRED")
        sdk = self._run(
            ("sdkmanager", "--list_installed"),
            60.0,
            (
                "ANDROID_SYSTEM_IMAGE_UNAVAILABLE"
                if provision_system_image
                else "ANDROID_33_ARM64_UNAVAILABLE"
            ),
        )
        if ANDROID_SYSTEM_IMAGE not in self._stdout_text(sdk):
            if not provision_system_image:
                raise DeploymentError("ANDROID_33_ARM64_UNAVAILABLE")
            self._install_android_system_image()

    def _install_android_system_image(self) -> None:
        command = ("sdkmanager", ANDROID_SYSTEM_IMAGE)
        try:
            result = self._runner.run(command, 1800.0, b"")
        except Exception:
            raise DeploymentError("ANDROID_SYSTEM_IMAGE_UNAVAILABLE") from None
        if result.returncode != 0:
            diagnostic = self._safe_process_diagnostic(result).lower()
            if "license" in diagnostic and (
                "not accepted" in diagnostic
                or "not been accepted" in diagnostic
                or "accept the sdk license" in diagnostic
            ):
                raise DeploymentError("ANDROID_LICENSE_REQUIRED")
            raise DeploymentError("ANDROID_SYSTEM_IMAGE_UNAVAILABLE")
        verified = self._run(
            ("sdkmanager", "--list_installed"),
            60.0,
            "ANDROID_SYSTEM_IMAGE_UNAVAILABLE",
        )
        if ANDROID_SYSTEM_IMAGE not in self._stdout_text(verified):
            raise DeploymentError("ANDROID_SYSTEM_IMAGE_UNAVAILABLE")

    @staticmethod
    def _safe_process_diagnostic(
        result: subprocess.CompletedProcess[bytes],
    ) -> str:
        parts: list[str] = []
        for raw in (result.stdout, result.stderr):
            if isinstance(raw, bytes):
                try:
                    parts.append(raw.decode("utf-8"))
                except UnicodeDecodeError:
                    continue
        return "\n".join(parts)

    def _fixed_avds_present(self) -> bool:
        return not self._missing_fixed_roles()

    def _missing_fixed_roles(self) -> frozenset[str]:
        result = self._run(
            ("emulator", "-list-avds"), 30.0, "FIXED_AVD_NOT_FOUND"
        )
        avds = {
            line.strip()
            for line in self._stdout_text(result).splitlines()
            if line.strip()
        }
        return frozenset(
            role for role, (avd, _serial) in FIXED_ROLES.items() if avd not in avds
        )

    def _record_initial_missing_roles(
        self, roles: frozenset[str]
    ) -> frozenset[str]:
        if not roles.issubset(FIXED_ROLES):
            raise DeploymentError("PROVISIONING_STATE_INVALID")
        if self._initial_missing_roles is None:
            self._initial_missing_roles = frozenset(roles)
        elif self._initial_missing_roles != roles:
            raise DeploymentError("PROVISIONING_STATE_INVALID")
        return self._initial_missing_roles

    def _require_fixed_avds(self) -> None:
        if not self._fixed_avds_present():
            raise DeploymentError("FIXED_AVD_NOT_FOUND")

    def _ensure_root_environment(self) -> dict[str, str]:
        try:
            if not self._filesystem.exists(self._env_file):
                setup = self._project_root / "scripts/setup-admin.sh"
                self._run(
                    (str(setup), str(self._env_file)),
                    300.0,
                    "ROOT_ENV_SETUP_FAILED",
                )
            if not self._filesystem.exists(self._env_file):
                raise DeploymentError("ROOT_ENV_INVALID")
            if self._filesystem.mode(self._env_file) != 0o600:
                raise DeploymentError("ROOT_ENV_INVALID")
            values = self._parse_env(self._filesystem.read_text(self._env_file))
        except DeploymentError:
            raise
        except Exception:
            raise DeploymentError("ROOT_ENV_INVALID") from None
        if any(not values.get(key) for key in REQUIRED_ROOT_ENV_KEYS):
            raise DeploymentError("ROOT_ENV_INVALID")
        try:
            decoded_key = base64.b64decode(
                values["THS_SESSION_ENCRYPTION_KEY"],
                altchars=b"-_",
                validate=True,
            )
        except (ValueError, TypeError):
            decoded_key = b""
        if len(decoded_key) != 32:
            raise DeploymentError("ROOT_ENV_INVALID")
        return values

    def _validate_macos_environment(self) -> None:
        try:
            if not self._filesystem.exists(self._macos_env):
                raise DeploymentError("MACOS_ENV_INVALID")
            values = self._parse_env(self._filesystem.read_text(self._macos_env))
        except DeploymentError:
            raise DeploymentError("MACOS_ENV_INVALID") from None
        except Exception:
            raise DeploymentError("MACOS_ENV_INVALID") from None
        if ROOT_ONLY_COMPOSE_KEYS.intersection(values):
            raise DeploymentError("MACOS_ENV_INVALID")
        if any(values.get(key) != expected for key, expected in REQUIRED_MACOS_ENV.items()):
            raise DeploymentError("MACOS_ENV_INVALID")

    @staticmethod
    def _parse_env(content: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise DeploymentError("ROOT_ENV_INVALID")
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip()
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name in values:
                raise DeploymentError("ROOT_ENV_INVALID")
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values[name] = value
        return values

    def _compose_prefix(self) -> tuple[str, ...]:
        return (
            "docker",
            "--context",
            "orbstack",
            "compose",
            "--env-file",
            self._env_argument,
            "--env-file",
            "deploy/macos.env",
            "-f",
            "deploy/compose.yml",
        )

    def _build_local_image(self) -> None:
        self._run(
            self._compose_prefix() + ("build", "api"),
            1800.0,
            "IMAGE_BUILD_FAILED",
        )

    def _validate_effective_compose_config(self) -> None:
        result = self._run(
            self._compose_prefix() + ("config", "--format", "json"),
            60.0,
            "COMPOSE_CONFIG_INVALID",
        )
        try:
            document = json.loads(self._stdout_text(result))
            environment = document["services"]["api"]["environment"]
        except (KeyError, TypeError, ValueError):
            environment = None
        expected = {
            "THS_DEVICE_LIFECYCLE_URL": REQUIRED_MACOS_ENV[
                "THS_DEVICE_LIFECYCLE_URL"
            ],
            "THS_DEVICE_LIFECYCLE_TOKEN": self._root_environment[
                "THS_DEVICE_LIFECYCLE_TOKEN"
            ],
            "THS_SESSION_ENCRYPTION_KEY": self._root_environment[
                "THS_SESSION_ENCRYPTION_KEY"
            ],
        }
        if not isinstance(environment, dict) or any(
            environment.get(key) != value or not value
            for key, value in expected.items()
        ):
            raise DeploymentError("COMPOSE_CONFIG_INVALID")

    def _read_image_manifest(self) -> dict[str, dict[str, object]]:
        result = self._run(
            (
                "docker",
                "--context",
                "orbstack",
                "run",
                "--rm",
                "--entrypoint",
                "cat",
                IMAGE_NAME,
                "/opt/ths/assets/manifest.json",
            ),
            60.0,
            "IMAGE_ASSET_MANIFEST_INVALID",
        )
        try:
            manifest = json.loads(self._stdout_text(result))
        except ValueError:
            manifest = None
        expected = {
            "apk": {
                "filename": "ths.apk",
                "size": 214_088_292,
                "sha256": APK_SHA256,
                "abis": ["arm64-v8a", "armeabi-v7a"],
            },
            "frida_server": {
                "version": "16.7.19",
                "size": 53_702_368,
                "sha256_xz": FRIDA_SHA256,
                "sha256": FRIDA_BINARY_SHA256,
            },
        }
        if manifest != expected:
            raise DeploymentError("IMAGE_ASSET_MANIFEST_INVALID")
        return manifest

    def _install_lifecycle_service(self) -> None:
        installer = self._project_root / "scripts/install-macos-device-lifecycle.sh"
        self._run(
            (
                str(installer),
                "--project-root",
                str(self._project_root),
                "--env-file",
                str(self._env_file),
            ),
            120.0,
            "DEVICE_LIFECYCLE_INSTALL_FAILED",
        )

    def _create_fixed_avd(self, role: str) -> None:
        avd_name, _serial = FIXED_ROLES[role]
        self._run(
            (
                "avdmanager",
                "create",
                "avd",
                "--name",
                avd_name,
                "--package",
                ANDROID_SYSTEM_IMAGE,
            ),
            300.0,
            "AVD_CREATE_FAILED",
            input_data=b"no\n",
        )

    def _start_created_avd(self, role: str) -> None:
        avd_name, _serial = FIXED_ROLES[role]
        emulator_path = self._trusted_emulator_path
        if emulator_path is None:
            raise DeploymentError("MISSING_PREREQUISITE")
        port = FIXED_EMULATOR_PORTS[role]
        self._run(
            (
                "launchctl",
                "submit",
                "-l",
                f"com.ths.avd.{port}",
                "--",
                str(emulator_path),
                "-avd",
                avd_name,
                "-port",
                str(port),
                "-no-snapshot",
                "-no-audio",
                "-gpu",
                "host",
                "-memory",
                "2048",
                "-cores",
                "4",
            ),
            30.0,
            "DEVICE_LAUNCH_FAILED",
        )
        self._wait_for_created_avd_boot(role)

    def _wait_for_created_avd_boot(self, role: str) -> None:
        expected_avd, serial = FIXED_ROLES[role]
        deadline = time.monotonic() + self._boot_timeout_seconds
        while True:
            try:
                state = self._runner.run(("adb", "-s", serial, "get-state"), 15.0)
                boot = self._runner.run(
                    (
                        "adb",
                        "-s",
                        serial,
                        "shell",
                        "getprop",
                        "sys.boot_completed",
                    ),
                    15.0,
                )
                ready = (
                    state.returncode == 0
                    and self._stdout_text(state).strip() == "device"
                    and boot.returncode == 0
                    and self._stdout_text(boot).strip() == "1"
                )
            except Exception:
                ready = False
            if ready:
                self._require_adb_avd_identity(serial, expected_avd)
                return
            if time.monotonic() >= deadline:
                raise DeploymentError("DEVICE_BOOT_TIMEOUT")
            time.sleep(max(0.0, self._poll_interval_seconds))

    def _provision_created_avd(self, role: str) -> None:
        self._run(
            (
                "docker",
                "--context",
                "orbstack",
                "run",
                "--rm",
                "--add-host",
                "host.docker.internal:host-gateway",
                "--env",
                "ADB_SERVER_SOCKET=tcp:host.docker.internal:5037",
                "--entrypoint",
                "container-provision-device",
                IMAGE_NAME,
                role,
            ),
            300.0,
            "DEVICE_PROVISION_FAILED",
        )

    def _calibrate_created_core(self) -> None:
        calibrator = self._project_root / "scripts/configure-macos-core-display.sh"
        self._run(
            (str(calibrator), FIXED_ROLES["core_metrics"][1], "adb"),
            30.0,
            "DEVICE_DISPLAY_CALIBRATION_FAILED",
        )

    def _verify_fixed_avd_identities(
        self, roles: tuple[str, ...] | None = None
    ) -> None:
        selected = tuple(FIXED_ROLES) if roles is None else roles
        for role in selected:
            expected_avd, serial = FIXED_ROLES[role]
            self._require_adb_avd_identity(serial, expected_avd)

    def _verify_preinstall_fixed_avd_identities(
        self, roles: tuple[str, ...] | None = None
    ) -> None:
        selected = tuple(FIXED_ROLES) if roles is None else roles
        for role in selected:
            expected_avd, serial = FIXED_ROLES[role]
            try:
                state_result = self._runner.run(
                    ("adb", "-s", serial, "get-state"), 15.0
                )
            except Exception:
                raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH") from None
            state = (
                self._stdout_text(state_result).strip()
                if state_result.returncode == 0
                else ""
            )
            if state == "device":
                self._require_adb_avd_identity(serial, expected_avd)
                continue
            if state_result.returncode == 0:
                raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH")
            devices = self._list_adb_devices()
            listed_state = devices.get(serial)
            if listed_state == "device":
                self._require_adb_avd_identity(serial, expected_avd)
                continue
            if listed_state is not None:
                raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH")
            process_result = self._run(
                ("ps", "-axo", "pid=,command="),
                15.0,
                "FIXED_AVD_IDENTITY_MISMATCH",
            )
            self._validate_starting_process_identity(
                self._stdout_text(process_result),
                FIXED_EMULATOR_PORTS[role],
                expected_avd,
            )

    def _list_adb_devices(self) -> dict[str, str]:
        result = self._run(
            ("adb", "devices"),
            15.0,
            "FIXED_AVD_IDENTITY_MISMATCH",
        )
        lines = self._stdout_text(result).splitlines()
        if not lines or lines[0].strip() != "List of devices attached":
            raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH")
        devices: dict[str, str] = {}
        for raw_line in lines[1:]:
            line = raw_line.strip()
            if not line:
                continue
            fields = line.split("\t")
            if len(fields) != 2:
                raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH")
            serial, state = (field.strip() for field in fields)
            if (
                not _SAFE_ADB_SERIAL.fullmatch(serial)
                or state not in _SAFE_ADB_STATES
                or serial in devices
            ):
                raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH")
            devices[serial] = state
        return devices

    def _require_adb_avd_identity(self, serial: str, expected_avd: str) -> None:
        result = self._run(
            ("adb", "-s", serial, "emu", "avd", "name"),
            15.0,
            "FIXED_AVD_IDENTITY_MISMATCH",
        )
        lines = [
            line.strip()
            for line in self._stdout_text(result).splitlines()
            if line.strip()
        ]
        if lines and lines[-1] == "OK":
            lines.pop()
        if lines != [expected_avd]:
            raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH")

    def _validate_starting_process_identity(
        self, output: str, port: int, expected_avd: str
    ) -> None:
        candidates: list[str] = []
        port_text = str(port)
        port_marker = re.compile(
            rf"(?:^|\s)-port(?:\s+|=){re.escape(port_text)}(?:\s|$)"
        )
        for raw_line in output.splitlines():
            parts = raw_line.strip().split(maxsplit=1)
            if len(parts) != 2 or re.fullmatch(r"[1-9][0-9]*", parts[0]) is None:
                if port_marker.search(raw_line):
                    raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH")
                continue
            pid = int(parts[0])
            command = parts[1]
            try:
                tokens = shlex.split(command, posix=True)
            except ValueError:
                if port_marker.search(command):
                    raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH") from None
                continue
            port_values = self._option_values(tokens, "-port")
            if port_text not in port_values:
                continue
            avd_values = self._option_values(tokens, "-avd")
            if port_values != [port_text] or len(avd_values) != 1:
                raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH")
            self._require_trusted_emulator_process(pid)
            candidates.append(avd_values[0])
        if not candidates:
            return
        if candidates != [expected_avd]:
            raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH")

    def _require_trusted_emulator_process(self, pid: int) -> None:
        trusted = self._trusted_emulator_path
        if trusted is None:
            raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH")
        try:
            candidate = Path(self._process_executable_resolver.resolve(pid))
            if not candidate.is_absolute():
                raise ValueError("relative executable path")
            resolved = candidate.resolve()
        except Exception:
            raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH") from None
        if resolved != trusted:
            raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH")

    @staticmethod
    def _option_values(tokens: list[str], option: str) -> list[str]:
        values: list[str] = []
        for index, token in enumerate(tokens):
            if token == option:
                if index + 1 >= len(tokens):
                    raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH")
                values.append(tokens[index + 1])
            elif token.startswith(f"{option}="):
                values.append(token.removeprefix(f"{option}="))
        return values

    def _start_stopped_roles(self) -> None:
        broker = self._broker
        if broker is None:
            broker = LoopbackLifecycleBroker(
                self._root_environment["THS_DEVICE_LIFECYCLE_TOKEN"]
            )
            self._broker = broker
        try:
            states = dict(broker.device_states())
            for role in FIXED_ROLES:
                state = states.get(role)
                if state == "RUNNING":
                    continue
                if state != "STOPPED":
                    raise DeploymentError("DEVICE_LIFECYCLE_NOT_READY")
                operation_id = broker.start_and_launch_app(role)
                broker.wait_for_state(operation_id, "RUNNING", 180.0)
        except DeploymentError:
            raise
        except Exception:
            raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE") from None

    def _start_and_launch_roles(self, roles: tuple[str, ...]) -> None:
        broker = self._broker
        if broker is None:
            broker = LoopbackLifecycleBroker(
                self._root_environment["THS_DEVICE_LIFECYCLE_TOKEN"]
            )
            self._broker = broker
        try:
            for role in roles:
                operation_id = broker.start_and_launch_app(role)
                broker.wait_for_state(operation_id, "RUNNING", 180.0)
        except DeploymentError:
            raise
        except Exception:
            raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE") from None

    def _verify_installed_apks(self, expected_sha256: object) -> None:
        if expected_sha256 != APK_SHA256:
            raise DeploymentError("IMAGE_ASSET_MANIFEST_INVALID")
        for _role, (_avd, serial) in FIXED_ROLES.items():
            path_result = self._run(
                ("adb", "-s", serial, "shell", "pm", "path", PACKAGE_NAME),
                30.0,
                "INSTALLED_APK_UNAVAILABLE",
            )
            base_apk = self._parse_base_apk_path(self._stdout_text(path_result))
            digest_result = self._run(
                ("adb", "-s", serial, "shell", "sha256sum", base_apk),
                60.0,
                "INSTALLED_APK_UNAVAILABLE",
            )
            digest = self._parse_apk_digest(
                self._stdout_text(digest_result), base_apk
            )
            if digest != expected_sha256:
                raise DeploymentError("INSTALLED_APK_MISMATCH")

    @staticmethod
    def _parse_base_apk_path(output: str) -> str:
        candidates = []
        for line in output.splitlines():
            if not line.startswith("package:"):
                continue
            candidate = line.removeprefix("package:")
            if candidate.endswith("/base.apk"):
                candidates.append(candidate)
        if len(candidates) != 1:
            raise DeploymentError("INSTALLED_APK_PATH_INVALID")
        candidate = candidates[0]
        path = PurePosixPath(candidate)
        if (
            not _SAFE_APK_PATH.fullmatch(candidate)
            or not candidate.startswith("/data/app/")
            or ".." in path.parts
            or "//" in candidate
        ):
            raise DeploymentError("INSTALLED_APK_PATH_INVALID")
        return candidate

    @staticmethod
    def _parse_apk_digest(output: str, base_apk: str) -> str:
        match = re.fullmatch(r"([0-9a-f]{64})[ \t]+([^\r\n]+)\r?\n?", output)
        if match is None or match.group(2) != base_apk:
            raise DeploymentError("INSTALLED_APK_PATH_INVALID")
        return match.group(1)

    def _compose_up(self) -> None:
        self._run(
            self._compose_prefix() + ("up", "-d", "--build"),
            1800.0,
            "COMPOSE_REBUILD_FAILED",
        )

    def _session_bundles_ready(self) -> bool:
        probe = (
            "from pathlib import Path; "
            "root=Path('/data/admin/ths-sessions'); "
            "names=('core_metrics.session','main_fund_flow.session'); "
            "print('READY' if all((root/name).is_file() for name in names) else 'MISSING')"
        )
        result = self._run(
            self._compose_prefix()
            + ("exec", "-T", "api", "python", "-c", probe),
            30.0,
            "SESSION_STATUS_UNAVAILABLE",
        )
        state = self._stdout_text(result).strip()
        if state == "READY":
            return True
        if state == "MISSING":
            return False
        raise DeploymentError("SESSION_STATUS_UNAVAILABLE")

    def _verify_data_only_acceptance(self) -> None:
        acceptance = self._data_only_acceptance or LoopbackDataOnlyAcceptance()
        try:
            acceptance.verify()
        except DeploymentError as error:
            if error.error_code == "DATA_ONLY_ACCEPTANCE_FAILED":
                raise
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED") from None
        except Exception:
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED") from None

    def _wait_for_compose_health(self) -> None:
        deadline = time.monotonic() + self._health_timeout_seconds
        command = self._compose_prefix() + (
            "ps",
            "--format",
            "json",
            "api",
            "redis",
        )
        while True:
            try:
                result = self._runner.run(command, 30.0)
            except Exception:
                result = _failed_process(command)
            if result.returncode == 0 and self._compose_services_healthy(
                self._stdout_text(result)
            ):
                return
            if time.monotonic() >= deadline:
                raise DeploymentError("COMPOSE_HEALTH_TIMEOUT")
            time.sleep(max(0.0, self._poll_interval_seconds))

    @staticmethod
    def _compose_services_healthy(output: str) -> bool:
        try:
            document = json.loads(output)
            items = document if isinstance(document, list) else [document]
        except ValueError:
            items = []
            for line in output.splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    return False
                items.append(item)
        if not all(isinstance(item, dict) for item in items):
            return False
        services = {item.get("Service"): item for item in items}
        return all(
            services.get(name, {}).get("State") == "running"
            and services.get(name, {}).get("Health") == "healthy"
            for name in ("api", "redis")
        )

    def _run(
        self,
        args: tuple[str, ...],
        timeout: float,
        error_code: str,
        *,
        input_data: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            result = self._runner.run(args, timeout, input_data)
        except Exception:
            raise DeploymentError(error_code) from None
        if result.returncode != 0:
            raise DeploymentError(error_code)
        return result

    @staticmethod
    def _stdout_text(result: subprocess.CompletedProcess[bytes]) -> str:
        try:
            return result.stdout.decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            raise DeploymentError("DEPLOYMENT_OUTPUT_INVALID") from None

    @staticmethod
    def _stderr_text(result: subprocess.CompletedProcess[bytes]) -> str:
        try:
            return result.stderr.decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            raise DeploymentError("DEPLOYMENT_OUTPUT_INVALID") from None


def _failed_process(args: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args, 1, b"", b"")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("auto", "existing", "provision"),
        default="auto",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    project_root = args.project_root.resolve()
    filesystem = PathFileSystem()
    orchestrator = MacDeploymentOrchestrator(
        SubprocessCommandRunner(project_root),
        None,
        filesystem,
        project_root=project_root,
        env_file=args.env_file,
    )
    try:
        result = orchestrator.deploy(args.mode)
    except DeploymentError as error:
        print(error.error_code, file=sys.stderr)
        return 1
    print(json.dumps(asdict(result), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
