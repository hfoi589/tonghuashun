#!/usr/bin/env python3
"""Fail-closed one-command deployment for the two fixed macOS Android roles."""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Mapping, Protocol
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
}
_SAFE_APK_PATH = re.compile(r"/data/app/[A-Za-z0-9._~+=/-]+/base\.apk")
_SAFE_OPERATION_ID = re.compile(r"[A-Za-z0-9_-]{1,256}")


@dataclass(frozen=True)
class DeploymentResult:
    mode: str
    state: str
    error_code: str | None = None


class DeploymentError(RuntimeError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


class CommandRunner(Protocol):
    def run(
        self, args: tuple[str, ...], timeout: float
    ) -> subprocess.CompletedProcess[bytes]: ...


class LifecycleBroker(Protocol):
    def device_states(self) -> Mapping[str, str]: ...

    def start_and_launch_app(self, role: str) -> str: ...

    def wait_for_state(
        self, operation_id: str, expected_state: str, timeout_seconds: float
    ) -> None: ...


class FileSystem(Protocol):
    def exists(self, path: Path) -> bool: ...

    def read_text(self, path: Path) -> str: ...

    def mode(self, path: Path) -> int: ...

    def which(self, command: str) -> str | None: ...


class SubprocessCommandRunner:
    """Run fixed argument vectors in the selected project checkout without a shell."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root
        self._environment = dict(os.environ)

    def run(
        self, args: tuple[str, ...], timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            args,
            cwd=self._project_root,
            env=self._environment,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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


class MacDeploymentOrchestrator:
    def __init__(
        self,
        runner: CommandRunner,
        lifecycle_broker: LifecycleBroker | None,
        filesystem: FileSystem,
        *,
        project_root: Path,
        env_file: Path = Path(".env"),
        health_timeout_seconds: float = 180.0,
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
        self._poll_interval_seconds = poll_interval_seconds
        self._root_environment: dict[str, str] = {}

    def deploy(self, mode: str = "auto") -> DeploymentResult:
        if mode == "existing":
            return self.deploy_existing()
        if mode == "provision":
            return self.deploy_provision()
        if mode != "auto":
            raise DeploymentError("DEPLOYMENT_MODE_INVALID")
        self._validate_command_presence()
        if self._fixed_avds_present():
            return self.deploy_existing()
        return self.deploy_provision()

    def deploy_provision(self) -> DeploymentResult:
        raise DeploymentError("PROVISIONING_NOT_IMPLEMENTED")

    def deploy_existing(self) -> DeploymentResult:
        self._validate_host_prerequisites()
        self._require_fixed_avds()
        self._root_environment = self._ensure_root_environment()
        self._validate_macos_environment()
        self._build_local_image()
        manifest = self._read_image_manifest()
        self._install_lifecycle_service()
        self._start_stopped_roles()
        self._verify_installed_apks(manifest["apk"]["sha256"])
        self._compose_up()
        self._wait_for_compose_health()
        return DeploymentResult(mode="existing", state="READY")

    def _validate_command_presence(self) -> None:
        try:
            missing = any(
                self._filesystem.which(command) is None
                for command in REQUIRED_COMMANDS
            )
        except Exception:
            raise DeploymentError("MISSING_PREREQUISITE") from None
        if missing:
            raise DeploymentError("MISSING_PREREQUISITE")

    def _validate_host_prerequisites(self) -> None:
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
            "ANDROID_33_ARM64_UNAVAILABLE",
        )
        if ANDROID_SYSTEM_IMAGE not in self._stdout_text(sdk):
            raise DeploymentError("ANDROID_33_ARM64_UNAVAILABLE")

    def _fixed_avds_present(self) -> bool:
        result = self._run(
            ("emulator", "-list-avds"), 30.0, "FIXED_AVD_NOT_FOUND"
        )
        avds = {line.strip() for line in self._stdout_text(result).splitlines() if line.strip()}
        return {avd for avd, _serial in FIXED_ROLES.values()}.issubset(avds)

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
        self, args: tuple[str, ...], timeout: float, error_code: str
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            result = self._runner.run(args, timeout)
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
