from __future__ import annotations

import base64
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
import re
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[1]
APK_SHA256 = "2554490aa3f5e2df17ac0a711311f3f85ee3130008af9bb4ab12510b3d6e971e"
FRIDA_SHA256 = "36ec3d7474b1ac69c4e7ec985612fae771d37ffb71cb94858bc6978f69f5e581"
FRIDA_BINARY_SHA256 = "4eebf1fbc66ff54aba9a9124c2ef8b32b566616388c60e2caa65148a529d826a"
PROVISIONER = ROOT / "scripts" / "container-provision-device.sh"
MACOS_DEPLOY = ROOT / "scripts" / "macos_deploy.py"
ONE_CLICK_WRAPPER = ROOT / "scripts" / "deploy-macos-one-click.sh"
PROVISION_WRAPPER = ROOT / "scripts" / "provision-macos-from-image.sh"
_MACOS_DEPLOY_MODULE = None

REQUIRED_ROOT_ENV = (
    "ADMIN_PASSWORD_HASH='$argon2id$example'\n"
    "ADMIN_SESSION_SECRET=session-secret-value\n"
    "THS_SESSION_ENCRYPTION_KEY=MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=\n"
    "THS_DEVICE_LIFECYCLE_TOKEN=lifecycle-secret-value\n"
)
REQUIRED_MACOS_ENV = (
    "CORE_ADB_SERIAL=emulator-5556\n"
    "CORE_FRIDA_SERVER_ENDPOINT=host.docker.internal:27043\n"
    "FUND_ADB_SERIAL=emulator-5554\n"
    "FUND_FRIDA_SERVER_ENDPOINT=host.docker.internal:27042\n"
    "THS_DEVICE_LIFECYCLE_URL=http://host.docker.internal:18765\n"
)
VALID_COMPOSE_ENVIRONMENT = {
    "THS_DEVICE_LIFECYCLE_URL": "http://host.docker.internal:18765",
    "THS_DEVICE_LIFECYCLE_TOKEN": "lifecycle-secret-value",
    "THS_SESSION_ENCRYPTION_KEY": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
}


def test_tracked_image_apk_matches_the_approved_mobile_asset() -> None:
    """Replacing the approved APK must fail before it enters an image build."""
    apk = ROOT / "ths_android_V11_59_03.apk"

    assert apk.is_file()
    assert apk.stat().st_size == 214_088_292
    assert hashlib.sha256(apk.read_bytes()).hexdigest() == APK_SHA256
    with ZipFile(apk) as archive:
        abis = {
            name.split("/", 2)[1]
            for name in archive.namelist()
            if name.startswith("lib/") and name.count("/") >= 2
        }
    assert abis == {"arm64-v8a", "armeabi-v7a"}


def test_image_contains_only_the_pinned_mobile_assets() -> None:
    """An ignored, mutable, or secret-bearing asset would make builds unsafe."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "*.apk\n!ths_android_V11_59_03.apk\n" in dockerignore
    assert (
        "COPY --chmod=0444 ths_android_V11_59_03.apk "
        "/opt/ths/assets/ths.apk"
    ) in dockerfile
    assert APK_SHA256 in dockerfile
    assert "frida-server-16.7.19-android-arm64.xz" in dockerfile
    assert FRIDA_SHA256 in dockerfile
    assert FRIDA_BINARY_SHA256 in dockerfile
    for forbidden in (
        "COPY .env",
        "COPY deploy/macos.env",
        "docker push",
        "docker save",
    ):
        assert forbidden not in dockerfile


def test_image_asset_contract_is_not_overridable_and_final_assets_are_read_only() -> None:
    """Build arguments or writable final assets could bypass the reviewed payload."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    arg_lines = [line for line in dockerfile.splitlines() if line.startswith("ARG ")]

    for fixed_value in (
        APK_SHA256,
        FRIDA_SHA256,
        FRIDA_BINARY_SHA256,
        "frida-server-16.7.19-android-arm64.xz",
    ):
        assert not any(fixed_value in line for line in arg_lines)
    assert not any(re.search(r"(APK|FRIDA).*(URL|SHA|DIGEST|FILE)", line) for line in arg_lines)
    assert "COPY --from=mobile-assets --chmod=0444 /opt/ths/assets/manifest.json /opt/ths/assets/manifest.json" in dockerfile
    assert "COPY --from=mobile-assets --chmod=0444 /opt/ths/assets/ths.apk /opt/ths/assets/ths.apk" in dockerfile
    assert "COPY --from=mobile-assets --chmod=0555 /opt/ths/assets/ths-frida-server /opt/ths/assets/ths-frida-server" in dockerfile
    assert "COPY --chmod=0555 scripts/container-provision-device.sh /usr/local/bin/container-provision-device" in dockerfile


def test_setup_admin_creates_all_four_independent_deployment_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting a deployment secret would leave direct sessions or lifecycle auth unusable."""
    script = ROOT / "scripts" / "setup-admin.py"
    spec = spec_from_file_location("task7_setup_admin", script)
    assert spec and spec.loader
    setup_admin = module_from_spec(spec)
    spec.loader.exec_module(setup_admin)
    passwords = iter(("correct horse battery staple", "correct horse battery staple"))
    monkeypatch.setattr(setup_admin.getpass, "getpass", lambda _prompt: next(passwords))
    env_file = tmp_path / ".env"

    assert setup_admin.main([str(env_file)]) == 0

    assert env_file.stat().st_mode & 0o777 == 0o600
    lines = env_file.read_text(encoding="utf-8").splitlines()
    values = {}
    for line in lines:
        name, value = line.split("=", 1)
        assert name not in values
        values[name] = value
    assert set(values) == {
        "ADMIN_PASSWORD_HASH",
        "ADMIN_SESSION_SECRET",
        "THS_SESSION_ENCRYPTION_KEY",
        "THS_DEVICE_LIFECYCLE_TOKEN",
    }
    assert values["ADMIN_PASSWORD_HASH"].startswith("'$argon2id$")
    assert len(base64.urlsafe_b64decode(values["THS_SESSION_ENCRYPTION_KEY"])) == 32
    assert len(values["ADMIN_SESSION_SECRET"]) >= 48
    assert len(values["THS_DEVICE_LIFECYCLE_TOKEN"]) >= 32


def _write_fake_adb(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "adb.log"
    adb = fake_bin / "adb"
    adb.write_text(
        """#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$ADB_COMMAND_LOG"
case "$*" in
  *" shell getprop sys.boot_completed") printf '%s\n' 1 ;;
  *" shell pm path com.hexin.plat.android")
    if [ "${ADB_PACKAGE_QUERY_FAIL:-0}" = 1 ]; then
      exit 9
    fi
    if [ "${ADB_PACKAGE_PRESENT:-0}" = 1 ]; then
      printf '%s\n' package:/data/app/com.hexin.plat.android/base.apk
    fi
    ;;
esac
""",
        encoding="utf-8",
    )
    adb.chmod(0o755)
    return fake_bin, log


@pytest.mark.parametrize(
    ("role", "serial", "host_port"),
    [
        ("core_metrics", "emulator-5556", "27043"),
        ("main_fund_flow", "emulator-5554", "27042"),
    ],
)
def test_container_provisioner_uses_only_fixed_new_device_commands(
    tmp_path: Path, role: str, serial: str, host_port: str
) -> None:
    """Caller-controlled device commands could mutate the protected existing AVDs."""
    fake_bin, log = _write_fake_adb(tmp_path)
    environment = os.environ | {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "ADB_COMMAND_LOG": str(log),
    }

    completed = subprocess.run(
        ["/bin/sh", str(PROVISIONER), role],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "DEVICE_PROVISION_READY\n"
    assert completed.stderr == ""
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"-s {serial} shell getprop sys.boot_completed",
        f"-s {serial} shell pm path com.hexin.plat.android",
        f"-s {serial} install /opt/ths/assets/ths.apk",
        f"-s {serial} root",
        f"-s {serial} push /opt/ths/assets/ths-frida-server /data/local/tmp/ths-frida-server",
        f"-s {serial} shell chmod 0755 /data/local/tmp/ths-frida-server",
        f"-s {serial} shell nohup /data/local/tmp/ths-frida-server >/dev/null 2>&1 &",
        f"-s {serial} forward tcp:{host_port} tcp:27042",
    ]


def test_container_provisioner_refuses_existing_packages_without_installing(
    tmp_path: Path,
) -> None:
    """Provisioning an existing package could erase or invalidate its logged-in state."""
    fake_bin, log = _write_fake_adb(tmp_path)
    environment = os.environ | {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "ADB_COMMAND_LOG": str(log),
        "ADB_PACKAGE_PRESENT": "1",
    }

    completed = subprocess.run(
        ["/bin/sh", str(PROVISIONER), "main_fund_flow"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "DEVICE_PACKAGE_ALREADY_INSTALLED\n"
    assert not any(" install " in f" {line} " for line in log.read_text().splitlines())


def test_container_provisioner_fails_closed_when_package_lookup_fails(
    tmp_path: Path,
) -> None:
    """A package-manager error must never be treated as permission to install."""
    fake_bin, log = _write_fake_adb(tmp_path)
    environment = os.environ | {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "ADB_COMMAND_LOG": str(log),
        "ADB_PACKAGE_QUERY_FAIL": "1",
    }

    completed = subprocess.run(
        ["/bin/sh", str(PROVISIONER), "core_metrics"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stderr == "DEVICE_PROVISION_FAILED\n"
    assert not any(" install " in f" {line} " for line in log.read_text().splitlines())


@pytest.mark.parametrize(
    "arguments",
    [[], ["unknown"], ["core_metrics", "emulator-9999"]],
)
def test_container_provisioner_accepts_exactly_one_fixed_role(
    tmp_path: Path, arguments: list[str]
) -> None:
    """An arbitrary serial or extra argument would bypass the fixed-role boundary."""
    fake_bin, log = _write_fake_adb(tmp_path)
    completed = subprocess.run(
        ["/bin/sh", str(PROVISIONER), *arguments],
        cwd=ROOT,
        env=os.environ
        | {"PATH": f"{fake_bin}:/usr/bin:/bin", "ADB_COMMAND_LOG": str(log)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert not log.exists()


def test_container_provisioner_source_has_no_reinstall_or_dynamic_command_surface() -> None:
    """Shell evaluation or destructive verbs would expand provisioning beyond new AVDs."""
    source = PROVISIONER.read_text(encoding="utf-8")

    for required in (
        "core_metrics) serial=emulator-5556; host_port=27043 ;;",
        "main_fund_flow) serial=emulator-5554; host_port=27042 ;;",
        "adb install /opt/ths/assets/ths.apk",
    ):
        assert required in source
    for forbidden in (
        "install -r",
        "pm clear",
        "uninstall",
        "wipe-data",
        "delete avd",
        "force-stop",
        "am start",
        "input tap",
        "monkey",
        "eval ",
        "sh -c",
        "bash -c",
        "ADB_SERIAL",
    ):
        assert forbidden not in source


def _load_macos_deploy():
    global _MACOS_DEPLOY_MODULE
    if _MACOS_DEPLOY_MODULE is not None:
        return _MACOS_DEPLOY_MODULE
    assert MACOS_DEPLOY.is_file(), "macOS deployment orchestrator is missing"
    module_name = "task7_macos_deploy"
    spec = spec_from_file_location(module_name, MACOS_DEPLOY)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    _MACOS_DEPLOY_MODULE = module
    return module


def _completed(
    args: tuple[str, ...],
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class FakeFileSystem:
    def __init__(self, *, env_exists: bool = True) -> None:
        self.files: dict[Path, tuple[str, int]] = {
            (ROOT / "deploy/macos.env").resolve(): (REQUIRED_MACOS_ENV, 0o600),
            (ROOT / ".dockerignore").resolve(): (".env\n", 0o644),
        }
        if env_exists:
            self.files[(ROOT / ".env").resolve()] = (REQUIRED_ROOT_ENV, 0o600)
        self.commands = {
            "adb",
            "avdmanager",
            "docker",
            "emulator",
            "java",
            "sdkmanager",
        }

    def exists(self, path: Path) -> bool:
        return path.resolve() in self.files

    def read_text(self, path: Path) -> str:
        return self.files[path.resolve()][0]

    def mode(self, path: Path) -> int:
        return self.files[path.resolve()][1]

    def which(self, command: str) -> str | None:
        return f"/fake/{command}" if command in self.commands else None

    def write_env(self) -> None:
        self.files[(ROOT / ".env").resolve()] = (REQUIRED_ROOT_ENV, 0o600)


class FakeCommandRunner:
    def __init__(
        self,
        *,
        apk_sha256: str = APK_SHA256,
        apk_path: str = "/data/app/~~safe/com.hexin.plat.android-safe/base.apk",
        avds: tuple[str, ...] = ("THS_CORE_33_ARM64", "THS_API_33_ARM64"),
        adb_states: dict[str, str | None] | None = None,
        adb_devices_output: str | None = None,
        adb_devices_returncode: int = 0,
        process_snapshots: list[str] | None = None,
        avd_identity_sequences: dict[str, list[str | None]] | None = None,
        healthy: bool = True,
        compose_environment: dict[str, str] | None = None,
        system_image_installed: bool = True,
        sdkmanager_install_returncode: int = 0,
        sdkmanager_install_stderr: bytes = b"",
        boot_completed: dict[str, str] | None = None,
        sessions_ready: bool = False,
        filesystem: FakeFileSystem | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.apk_sha256 = apk_sha256
        self.apk_path = apk_path
        self.avds = list(avds)
        present_serials = {
            serial
            for avd, serial in (
                ("THS_CORE_33_ARM64", "emulator-5556"),
                ("THS_API_33_ARM64", "emulator-5554"),
            )
            if avd in avds
        }
        self.adb_states = {
            "emulator-5556": "device" if "emulator-5556" in present_serials else None,
            "emulator-5554": "device" if "emulator-5554" in present_serials else None,
            **(adb_states or {}),
        }
        self.adb_devices_output = adb_devices_output
        self.adb_devices_returncode = adb_devices_returncode
        self.process_snapshots = list(process_snapshots or [])
        self.avd_identity_sequences = {
            serial: list(values)
            for serial, values in (avd_identity_sequences or {}).items()
        }
        self.healthy = healthy
        self.compose_environment = (
            dict(VALID_COMPOSE_ENVIRONMENT)
            if compose_environment is None
            else dict(compose_environment)
        )
        self.system_image_installed = system_image_installed
        self.sdkmanager_install_returncode = sdkmanager_install_returncode
        self.sdkmanager_install_stderr = sdkmanager_install_stderr
        self.boot_completed = {
            "emulator-5556": "1",
            "emulator-5554": "1",
            **(boot_completed or {}),
        }
        self.sessions_ready = sessions_ready
        self.filesystem = filesystem
        self.events = events if events is not None else []
        self.calls: list[tuple[str, ...]] = []
        self.inputs: list[bytes | None] = []

    def run(
        self,
        args: tuple[str, ...],
        timeout: float,
        input_data: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        del timeout
        self.calls.append(args)
        self.inputs.append(input_data)
        if args == ("uname", "-s"):
            return _completed(args, stdout=b"Darwin\n")
        if args == ("uname", "-m"):
            return _completed(args, stdout=b"arm64\n")
        if args[:4] == ("docker", "--context", "orbstack", "info"):
            return _completed(args, stdout=b"orbstack\n")
        if args == ("java", "-version"):
            return _completed(args, stderr=b'openjdk version "17.0.12"\n')
        if args == ("sdkmanager", "--list_installed"):
            return _completed(
                args,
                stdout=(
                    b"system-images;android-33;google_apis;arm64-v8a\n"
                    if self.system_image_installed
                    else b"Installed packages:\n"
                ),
            )
        if args == ("sdkmanager", "system-images;android-33;google_apis;arm64-v8a"):
            if self.sdkmanager_install_returncode == 0:
                self.system_image_installed = True
            return _completed(
                args,
                returncode=self.sdkmanager_install_returncode,
                stderr=self.sdkmanager_install_stderr,
            )
        if args == ("emulator", "-list-avds"):
            return _completed(args, stdout=("\n".join(self.avds) + "\n").encode())
        if args[:3] == ("avdmanager", "create", "avd"):
            avd_name = args[args.index("--name") + 1]
            if avd_name in self.avds:
                return _completed(args, returncode=1, stderr=b"AVD already exists")
            self.avds.append(avd_name)
            self.events.append(f"avd-created:{avd_name}")
            return _completed(args)
        if args[:2] == ("launchctl", "submit"):
            avd_name = args[args.index("-avd") + 1]
            serial = {
                "THS_CORE_33_ARM64": "emulator-5556",
                "THS_API_33_ARM64": "emulator-5554",
            }[avd_name]
            self.adb_states[serial] = "device"
            self.events.append(f"launchctl:{avd_name}")
            return _completed(args)
        if len(args) == 4 and args[:2] == ("adb", "-s") and args[3] == "get-state":
            serial = args[2]
            state = self.adb_states[serial]
            self.events.append(f"adb-state:{serial}:{state}")
            if state is None:
                return _completed(args, returncode=1, stderr=b"device absent")
            return _completed(args, stdout=f"{state}\n".encode())
        if len(args) == 6 and args[:2] == ("adb", "-s") and args[3:] == (
            "shell",
            "getprop",
            "sys.boot_completed",
        ):
            serial = args[2]
            self.events.append(f"boot:{serial}:{self.boot_completed[serial]}")
            return _completed(args, stdout=f"{self.boot_completed[serial]}\n".encode())
        if args == ("adb", "devices"):
            if self.adb_devices_output is None:
                attached = [
                    f"{serial}\t{state}"
                    for serial, state in self.adb_states.items()
                    if state is not None
                ]
                output = "List of devices attached\n" + "\n".join(attached) + "\n"
            else:
                output = self.adb_devices_output
            self.events.append(f"adb-devices:{self.adb_devices_returncode}:{output}")
            return _completed(
                args,
                returncode=self.adb_devices_returncode,
                stdout=output.encode(),
                stderr=b"private adb server detail",
            )
        if args == ("ps", "-axo", "pid=,command="):
            snapshot = self.process_snapshots.pop(0) if self.process_snapshots else ""
            self.events.append(f"process-snapshot:{snapshot}")
            return _completed(args, stdout=snapshot.encode())
        if len(args) == 6 and args[:2] == ("adb", "-s") and args[3:] == (
            "emu",
            "avd",
            "name",
        ):
            serial = args[2]
            default_identity = {
                "emulator-5556": "THS_CORE_33_ARM64",
                "emulator-5554": "THS_API_33_ARM64",
            }[serial]
            role = {
                "emulator-5556": "core_metrics",
                "emulator-5554": "main_fund_flow",
            }[serial]
            broker_finished = any(
                event == f"broker-wait:operation-{role}" for event in self.events
            )
            if self.adb_states[serial] is None and not broker_finished:
                self.events.append(f"identity:{serial}:unavailable")
                return _completed(args, returncode=1, stderr=b"device absent")
            sequence = self.avd_identity_sequences.get(serial, [])
            identity = sequence.pop(0) if sequence else default_identity
            self.events.append(f"identity:{serial}:{identity}")
            if identity is None:
                return _completed(args, returncode=1, stderr=b"private adb detail")
            return _completed(args, stdout=f"{identity}\nOK\n".encode())
        if "config" in args and "--format" in args:
            payload = {
                "services": {
                    "api": {"environment": self.compose_environment},
                }
            }
            return _completed(args, stdout=json.dumps(payload).encode())
        if "/opt/ths/assets/manifest.json" in args:
            manifest = {
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
            return _completed(args, stdout=json.dumps(manifest).encode())
        if args and args[0].endswith("install-macos-device-lifecycle.sh"):
            self.events.append("installer")
            return _completed(args, stdout=b"DEVICE_LIFECYCLE_INSTALL_READY\n")
        if args and args[0].endswith("configure-macos-core-display.sh"):
            self.events.append("display-calibration")
            return _completed(args)
        if args and args[0].endswith("setup-admin.sh"):
            self.events.append("setup-admin")
            assert self.filesystem is not None
            self.filesystem.write_env()
            return _completed(args)
        if "pm" in args and "path" in args:
            return _completed(
                args,
                stdout=f"package:{self.apk_path}\n".encode(),
            )
        if "sha256sum" in args:
            path = args[-1]
            return _completed(args, stdout=f"{self.apk_sha256}  {path}\n".encode())
        if "container-provision-device" in args:
            role = args[-1]
            self.events.append(f"container-provision:{role}")
            return _completed(args, stdout=b"DEVICE_PROVISION_READY\n")
        if (
            "exec" in args
            and "api" in args
            and "core_metrics.session" in " ".join(args)
        ):
            return _completed(
                args,
                stdout=b"READY\n" if self.sessions_ready else b"MISSING\n",
            )
        if "ps" in args and "--format" in args:
            health = "healthy" if self.healthy else "starting"
            payload = [
                {"Service": "api", "State": "running", "Health": health},
                {"Service": "redis", "State": "running", "Health": health},
            ]
            return _completed(
                args,
                stdout=json.dumps(payload).encode(),
                stderr=b"private compose diagnostics",
            )
        return _completed(args)


class FakeProcessExecutableResolver:
    def __init__(
        self,
        outcomes: dict[int, Path | Exception] | None = None,
        *,
        default: Path = Path("/fake/emulator"),
    ) -> None:
        self.outcomes = dict(outcomes or {})
        self.default = default
        self.calls: list[int] = []

    def resolve(self, pid: int) -> Path:
        self.calls.append(pid)
        outcome = self.outcomes.get(pid, self.default)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeLifecycleBroker:
    def __init__(
        self,
        states: dict[str, str] | None = None,
        *,
        events: list[str] | None = None,
    ) -> None:
        self.states = states or {
            "core_metrics": "RUNNING",
            "main_fund_flow": "RUNNING",
        }
        self.events = events if events is not None else []
        self.start_calls: list[str] = []
        self.wait_calls: list[tuple[str, str, float]] = []

    def device_states(self) -> dict[str, str]:
        self.events.append("broker-states")
        return dict(self.states)

    def start_and_launch_app(self, role: str) -> str:
        self.events.append(f"broker-start:{role}")
        self.start_calls.append(role)
        return f"operation-{role}"

    def wait_for_state(
        self, operation_id: str, expected_state: str, timeout_seconds: float
    ) -> None:
        self.events.append(f"broker-wait:{operation_id}")
        self.wait_calls.append((operation_id, expected_state, timeout_seconds))


class FakeDataOnlyAcceptance:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def verify(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


def existing_mac_runner(
    *,
    apk_sha256: str = APK_SHA256,
    apk_path: str = "/data/app/~~safe/com.hexin.plat.android-safe/base.apk",
    avds: tuple[str, ...] = ("THS_CORE_33_ARM64", "THS_API_33_ARM64"),
    adb_states: dict[str, str | None] | None = None,
    adb_devices_output: str | None = None,
    adb_devices_returncode: int = 0,
    process_snapshots: list[str] | None = None,
    avd_identity_sequences: dict[str, list[str | None]] | None = None,
    healthy: bool = True,
    compose_environment: dict[str, str] | None = None,
    system_image_installed: bool = True,
    sdkmanager_install_returncode: int = 0,
    sdkmanager_install_stderr: bytes = b"",
    boot_completed: dict[str, str] | None = None,
    sessions_ready: bool = False,
    filesystem: FakeFileSystem | None = None,
    events: list[str] | None = None,
) -> FakeCommandRunner:
    return FakeCommandRunner(
        apk_sha256=apk_sha256,
        apk_path=apk_path,
        avds=avds,
        adb_states=adb_states,
        adb_devices_output=adb_devices_output,
        adb_devices_returncode=adb_devices_returncode,
        process_snapshots=process_snapshots,
        avd_identity_sequences=avd_identity_sequences,
        healthy=healthy,
        compose_environment=compose_environment,
        system_image_installed=system_image_installed,
        sdkmanager_install_returncode=sdkmanager_install_returncode,
        sdkmanager_install_stderr=sdkmanager_install_stderr,
        boot_completed=boot_completed,
        sessions_ready=sessions_ready,
        filesystem=filesystem,
        events=events,
    )


def make_orchestrator(
    runner: FakeCommandRunner,
    *,
    filesystem: FakeFileSystem | None = None,
    broker: FakeLifecycleBroker | None = None,
    process_executable_resolver: FakeProcessExecutableResolver | None = None,
    acceptance: FakeDataOnlyAcceptance | None = None,
    health_timeout_seconds: float = 0.05,
    boot_timeout_seconds: float = 0.05,
    env_file: Path = Path(".env"),
):
    module = _load_macos_deploy()
    filesystem = filesystem or FakeFileSystem()
    runner.filesystem = filesystem
    return module.MacDeploymentOrchestrator(
        runner,
        broker or FakeLifecycleBroker(),
        filesystem,
        project_root=ROOT,
        env_file=env_file,
        process_executable_resolver=(
            process_executable_resolver or FakeProcessExecutableResolver()
        ),
        data_only_acceptance=acceptance or FakeDataOnlyAcceptance(),
        health_timeout_seconds=health_timeout_seconds,
        boot_timeout_seconds=boot_timeout_seconds,
        poll_interval_seconds=0.0,
    )


def test_existing_mode_preserves_both_avds_and_uses_canonical_compose() -> None:
    """A redeploy command must not mutate AVDs, apps, images, or Docker volumes."""
    runner = existing_mac_runner(apk_sha256=APK_SHA256)

    result = make_orchestrator(runner).deploy_existing()

    assert result.mode == "existing"
    assert result.state == "READY"
    rendered = "\n".join(" ".join(call) for call in runner.calls)
    assert "docker --context orbstack compose" in rendered
    assert "--env-file .env --env-file deploy/macos.env" in rendered
    assert "up -d --build" in rendered
    config_indices = [
        index
        for index, call in enumerate(runner.calls)
        if "config" in call and "--format" in call
    ]
    build_index = next(
        index for index, call in enumerate(runner.calls) if call[-2:] == ("build", "api")
    )
    up_index = next(
        index for index, call in enumerate(runner.calls) if call[-3:] == ("up", "-d", "--build")
    )
    assert len(config_indices) == 2
    assert config_indices[0] < build_index
    assert build_index < config_indices[1] < up_index
    for forbidden in (
        "install -r",
        " install ",
        "pm clear",
        "wipe-data",
        "delete avd",
        "docker push",
        "docker save",
        "down -v",
    ):
        assert forbidden not in rendered


def test_existing_mode_requires_both_fixed_avds_before_building() -> None:
    """Treating a partial role set as existing mode could target the wrong device."""
    module = _load_macos_deploy()
    runner = existing_mac_runner(avds=("THS_CORE_33_ARM64",))

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner).deploy_existing()

    assert caught.value.error_code == "FIXED_AVD_NOT_FOUND"
    assert not any("compose" in call and "build" in call for call in runner.calls)


@pytest.mark.parametrize("identity", ["THS_API_33_ARM64", None])
def test_existing_mode_rejects_wrong_or_missing_fixed_serial_identity_before_mutation(
    identity: str | None,
) -> None:
    """A fixed serial must prove its exact AVD before host lifecycle mutation."""
    module = _load_macos_deploy()
    events: list[str] = []
    runner = existing_mac_runner(
        adb_states={"emulator-5556": "device"},
        avd_identity_sequences={"emulator-5556": [identity]},
        events=events,
    )
    broker = FakeLifecycleBroker(events=events)

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner, broker=broker).deploy_existing()

    assert caught.value.error_code == "FIXED_AVD_IDENTITY_MISMATCH"
    assert "installer" not in events
    assert not any(event.startswith("broker-") for event in events)
    assert not any("pm" in call and "path" in call for call in runner.calls)


def test_existing_mode_rejects_wrong_starting_process_avd_before_mutation() -> None:
    """A process on the fixed port must carry exactly the approved -avd argument."""
    module = _load_macos_deploy()
    events: list[str] = []
    runner = existing_mac_runner(
        adb_states={"emulator-5556": None},
        process_snapshots=[
            "123 /fake/emulator -avd THS_API_33_ARM64 -port 5556 -no-audio\n"
        ],
        events=events,
    )
    broker = FakeLifecycleBroker(events=events)

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner, broker=broker).deploy_existing()

    assert caught.value.error_code == "FIXED_AVD_IDENTITY_MISMATCH"
    assert ("adb", "-s", "emulator-5556", "get-state") in runner.calls
    assert ("ps", "-axo", "pid=,command=") in runner.calls
    assert "installer" not in events
    assert not any(event.startswith("broker-") for event in events)


def test_existing_mode_rejects_adb_listing_transport_failure_before_mutation() -> None:
    """A failed get-state needs a successful server listing before absence is trusted."""
    module = _load_macos_deploy()
    events: list[str] = []
    runner = existing_mac_runner(
        adb_states={"emulator-5556": None},
        adb_devices_returncode=1,
        process_snapshots=[""],
        events=events,
    )

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner).deploy_existing()

    assert caught.value.error_code == "FIXED_AVD_IDENTITY_MISMATCH"
    assert ("adb", "devices") in runner.calls
    assert ("ps", "-axo", "pid=,command=") not in runner.calls
    assert "installer" not in events


@pytest.mark.parametrize("state", ["offline", "unauthorized"])
def test_existing_mode_rejects_non_device_adb_states_before_mutation(
    state: str,
) -> None:
    """An attached but unusable fixed serial is not proof of a stopped AVD."""
    module = _load_macos_deploy()
    events: list[str] = []
    runner = existing_mac_runner(
        adb_states={"emulator-5556": state},
        process_snapshots=[""],
        events=events,
    )

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner).deploy_existing()

    assert caught.value.error_code == "FIXED_AVD_IDENTITY_MISMATCH"
    assert ("ps", "-axo", "pid=,command=") not in runner.calls
    assert "installer" not in events


@pytest.mark.parametrize(
    "listing",
    [
        "not an adb devices header\n",
        "List of devices attached\nemulator-5556\tdevice\nemulator-5556\toffline\n",
    ],
)
def test_existing_mode_rejects_malformed_or_ambiguous_adb_listing(
    listing: str,
) -> None:
    """Only an exact successful listing that omits the serial proves absence."""
    module = _load_macos_deploy()
    events: list[str] = []
    runner = existing_mac_runner(
        adb_states={"emulator-5556": None},
        adb_devices_output=listing,
        process_snapshots=[""],
        events=events,
    )

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner).deploy_existing()

    assert caught.value.error_code == "FIXED_AVD_IDENTITY_MISMATCH"
    assert ("ps", "-axo", "pid=,command=") not in runner.calls
    assert "installer" not in events


def test_existing_mode_rejects_spoofed_bare_emulator_argv0() -> None:
    """A bare trusted-looking argv0 cannot replace actual PID executable proof."""
    module = _load_macos_deploy()
    events: list[str] = []
    runner = existing_mac_runner(
        adb_states={"emulator-5556": None},
        process_snapshots=[
            "123 emulator -avd THS_CORE_33_ARM64 -port 5556 -no-audio\n"
        ],
        events=events,
    )
    broker = FakeLifecycleBroker(
        {"core_metrics": "STOPPED", "main_fund_flow": "RUNNING"},
        events=events,
    )
    resolver = FakeProcessExecutableResolver({123: Path("/usr/bin/python3")})

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(
            runner,
            broker=broker,
            process_executable_resolver=resolver,
        ).deploy_existing()

    assert caught.value.error_code == "FIXED_AVD_IDENTITY_MISMATCH"
    assert resolver.calls == [123]
    assert "installer" not in events


def test_existing_mode_rejects_spoofed_trusted_absolute_argv0() -> None:
    """The exact trusted path in command text is not proof of the process image."""
    module = _load_macos_deploy()
    events: list[str] = []
    runner = existing_mac_runner(
        adb_states={"emulator-5556": None},
        process_snapshots=[
            "123 /fake/emulator -avd THS_CORE_33_ARM64 -port 5556 -no-audio\n"
        ],
        events=events,
    )
    broker = FakeLifecycleBroker(
        {"core_metrics": "STOPPED", "main_fund_flow": "RUNNING"},
        events=events,
    )
    resolver = FakeProcessExecutableResolver({123: Path("/usr/bin/python3")})

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(
            runner,
            broker=broker,
            process_executable_resolver=resolver,
        ).deploy_existing()

    assert caught.value.error_code == "FIXED_AVD_IDENTITY_MISMATCH"
    assert resolver.calls == [123]
    assert "installer" not in events


def test_existing_mode_fails_closed_when_process_executable_resolution_fails() -> None:
    """Resolver errors cannot turn a process on the fixed port into a stopped role."""
    module = _load_macos_deploy()
    events: list[str] = []
    runner = existing_mac_runner(
        adb_states={"emulator-5556": None},
        process_snapshots=[
            "123 /fake/emulator -avd THS_CORE_33_ARM64 -port 5556 -no-audio\n"
        ],
        events=events,
    )
    broker = FakeLifecycleBroker(
        {"core_metrics": "STOPPED", "main_fund_flow": "RUNNING"},
        events=events,
    )
    resolver = FakeProcessExecutableResolver(
        {123: OSError("process disappeared")}
    )

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(
            runner,
            broker=broker,
            process_executable_resolver=resolver,
        ).deploy_existing()

    assert caught.value.error_code == "FIXED_AVD_IDENTITY_MISMATCH"
    assert resolver.calls == [123]
    assert "installer" not in events


def test_existing_mode_rejects_actual_process_executable_mismatch() -> None:
    """A fixed-port PID must resolve to the one trusted Emulator executable."""
    module = _load_macos_deploy()
    events: list[str] = []
    runner = existing_mac_runner(
        adb_states={"emulator-5556": None},
        process_snapshots=[
            "123 /fake/emulator -avd THS_CORE_33_ARM64 -port 5556 -no-audio\n"
        ],
        events=events,
    )
    broker = FakeLifecycleBroker(
        {"core_metrics": "STOPPED", "main_fund_flow": "RUNNING"},
        events=events,
    )
    resolver = FakeProcessExecutableResolver(
        {123: Path("/opt/untrusted/emulator")}
    )

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(
            runner,
            broker=broker,
            process_executable_resolver=resolver,
        ).deploy_existing()

    assert caught.value.error_code == "FIXED_AVD_IDENTITY_MISMATCH"
    assert resolver.calls == [123]
    assert "installer" not in events


def test_existing_mode_accepts_exact_actual_process_executable() -> None:
    """Actual executable identity wins even when command argv0 is untrusted text."""
    events: list[str] = []
    runner = existing_mac_runner(
        adb_states={"emulator-5556": None},
        process_snapshots=[
            "123 /usr/bin/python3 -avd THS_CORE_33_ARM64 -port 5556 -no-audio\n"
        ],
        events=events,
    )
    broker = FakeLifecycleBroker(
        {"core_metrics": "STOPPED", "main_fund_flow": "RUNNING"},
        events=events,
    )
    resolver = FakeProcessExecutableResolver({123: Path("/fake/emulator")})

    result = make_orchestrator(
        runner,
        broker=broker,
        process_executable_resolver=resolver,
    ).deploy_existing()

    assert result.state == "READY"
    assert resolver.calls == [123]
    assert events.index(
        "process-snapshot:123 /usr/bin/python3 "
        "-avd THS_CORE_33_ARM64 -port 5556 -no-audio\n"
    ) < events.index(
        "installer",
    )


def test_existing_mode_fails_closed_without_darwin_process_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production cannot authenticate a PID without the Darwin process API."""
    module = _load_macos_deploy()
    events: list[str] = []
    filesystem = FakeFileSystem()
    runner = existing_mac_runner(
        adb_states={"emulator-5556": None},
        process_snapshots=[
            "123 /fake/emulator -avd THS_CORE_33_ARM64 -port 5556 -no-audio\n"
        ],
        filesystem=filesystem,
        events=events,
    )
    broker = FakeLifecycleBroker(
        {"core_metrics": "STOPPED", "main_fund_flow": "RUNNING"},
        events=events,
    )
    orchestrator = module.MacDeploymentOrchestrator(
        runner,
        broker,
        filesystem,
        project_root=ROOT,
        health_timeout_seconds=0.05,
        poll_interval_seconds=0.0,
    )
    monkeypatch.setattr(module.sys, "platform", "linux")

    with pytest.raises(module.DeploymentError) as caught:
        orchestrator.deploy_existing()

    assert caught.value.error_code == "FIXED_AVD_IDENTITY_MISMATCH"
    assert "installer" not in events


@pytest.mark.parametrize(
    "process_line",
    [
        "123 /fake/emulator -avd THS_CORE_33_ARM64 -port 5556 -port\n",
        "not-a-pid /fake/emulator -avd THS_CORE_33_ARM64 -port 5556\n",
        (
            "123 /fake/emulator -avd THS_CORE_33_ARM64 -port 5556\n"
            "124 /fake/emulator -avd THS_CORE_33_ARM64 -port 5556\n"
        ),
    ],
)
def test_existing_mode_rejects_ambiguous_starting_process_options(
    process_line: str,
) -> None:
    """A malformed repeated fixed-port option must not be treated as safely stopped."""
    module = _load_macos_deploy()
    events: list[str] = []
    runner = existing_mac_runner(
        adb_states={"emulator-5556": None},
        process_snapshots=[process_line],
        events=events,
    )

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner).deploy_existing()

    assert caught.value.error_code == "FIXED_AVD_IDENTITY_MISMATCH"
    assert "installer" not in events


def test_existing_mode_fails_closed_on_installed_apk_mismatch_before_compose_up() -> None:
    """Automatically replacing a mismatched APK would destroy protected login state."""
    module = _load_macos_deploy()
    runner = existing_mac_runner(apk_sha256="0" * 64)

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner).deploy_existing()

    assert caught.value.error_code == "INSTALLED_APK_MISMATCH"
    assert not any(call[-3:] == ("up", "-d", "--build") for call in runner.calls)
    rendered = "\n".join(" ".join(call) for call in runner.calls)
    assert " install " not in rendered


@pytest.mark.parametrize(
    "empty_key",
    [
        "THS_DEVICE_LIFECYCLE_URL",
        "THS_DEVICE_LIFECYCLE_TOKEN",
        "THS_SESSION_ENCRYPTION_KEY",
    ],
)
def test_existing_mode_rejects_empty_effective_compose_security_settings(
    empty_key: str,
) -> None:
    """A later env-file or ambient empty value must fail before image build."""
    module = _load_macos_deploy()
    compose_environment = dict(VALID_COMPOSE_ENVIRONMENT)
    compose_environment[empty_key] = ""
    runner = existing_mac_runner(compose_environment=compose_environment)

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner).deploy_existing()

    assert caught.value.error_code == "COMPOSE_CONFIG_INVALID"
    assert any("config" in call and "--format" in call for call in runner.calls)
    assert not any(call[-2:] == ("build", "api") for call in runner.calls)
    assert "installer" not in runner.events


def test_subprocess_runner_removes_ambient_compose_security_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shell environment must not take precedence over the reviewed env files."""
    module = _load_macos_deploy()
    probe = tmp_path / "probe-environment"
    probe.write_text("#!/bin/sh\nexec /usr/bin/env\n", encoding="utf-8")
    probe.chmod(0o755)
    for key in (
        "ADMIN_PASSWORD_HASH",
        "ADMIN_SESSION_SECRET",
        "THS_DEVICE_LIFECYCLE_URL",
        "THS_DEVICE_LIFECYCLE_TOKEN",
        "THS_SESSION_ENCRYPTION_KEY",
    ):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("TASK7_UNRELATED_ENV", "preserved")

    result = module.SubprocessCommandRunner(tmp_path).run((str(probe),), 1.0)

    output = result.stdout.decode("utf-8").splitlines()
    assert result.returncode == 0
    assert "TASK7_UNRELATED_ENV=preserved" in output
    for key in (
        "ADMIN_PASSWORD_HASH",
        "ADMIN_SESSION_SECRET",
        "THS_DEVICE_LIFECYCLE_URL",
        "THS_DEVICE_LIFECYCLE_TOKEN",
        "THS_SESSION_ENCRYPTION_KEY",
    ):
        assert not any(line.startswith(f"{key}=") for line in output)


def test_existing_mode_rejects_an_untrusted_installed_base_apk_path() -> None:
    """A package-manager path must not become an unchecked on-device shell argument."""
    module = _load_macos_deploy()
    runner = existing_mac_runner(
        apk_path="/data/app/safe/../../sdcard/private/base.apk"
    )

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner).deploy_existing()

    assert caught.value.error_code == "INSTALLED_APK_PATH_INVALID"
    assert not any("sha256sum" in call for call in runner.calls)


def test_existing_mode_returns_a_fixed_error_for_missing_prerequisite() -> None:
    """A missing host tool must stop before any deployment side effect."""
    module = _load_macos_deploy()
    filesystem = FakeFileSystem()
    filesystem.commands.remove("sdkmanager")
    runner = existing_mac_runner(filesystem=filesystem)

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner, filesystem=filesystem).deploy_existing()

    assert caught.value.error_code == "MISSING_PREREQUISITE"
    assert runner.calls == []


def test_existing_mode_sanitizes_prerequisite_lookup_failures() -> None:
    """Tool-discovery failures must not expose host filesystem diagnostics."""
    module = _load_macos_deploy()

    class BrokenToolLookup(FakeFileSystem):
        def which(self, command: str) -> str | None:
            raise OSError(f"private path for {command}")

    filesystem = BrokenToolLookup()
    runner = existing_mac_runner(filesystem=filesystem)

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner, filesystem=filesystem).deploy_existing()

    assert caught.value.error_code == "MISSING_PREREQUISITE"
    assert "private path" not in str(caught.value)
    assert runner.calls == []


def test_existing_mode_sanitizes_a_malformed_dual_role_environment() -> None:
    """Malformed host configuration must not leak parser details or enter a build."""
    module = _load_macos_deploy()
    filesystem = FakeFileSystem()
    filesystem.files[(ROOT / "deploy/macos.env").resolve()] = (
        REQUIRED_MACOS_ENV + "malformed-line\n",
        0o600,
    )
    runner = existing_mac_runner(filesystem=filesystem)

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner, filesystem=filesystem).deploy_existing()

    assert caught.value.error_code == "MACOS_ENV_INVALID"
    assert not any("compose" in call and "build" in call for call in runner.calls)


@pytest.mark.parametrize(
    "root_only_key",
    [
        "THS_DEVICE_LIFECYCLE_TOKEN",
        "THS_SESSION_ENCRYPTION_KEY",
    ],
)
def test_existing_mode_rejects_root_only_secrets_in_macos_environment(
    root_only_key: str,
) -> None:
    """A later env file must not override the root secret source, even with empty text."""
    module = _load_macos_deploy()
    filesystem = FakeFileSystem()
    filesystem.files[(ROOT / "deploy/macos.env").resolve()] = (
        REQUIRED_MACOS_ENV + f"{root_only_key}=\n",
        0o600,
    )
    runner = existing_mac_runner(filesystem=filesystem)

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner, filesystem=filesystem).deploy_existing()

    assert caught.value.error_code == "MACOS_ENV_INVALID"
    assert not any("compose" in call and "build" in call for call in runner.calls)


def test_existing_mode_sanitizes_root_environment_file_errors() -> None:
    """Filesystem diagnostics can expose local paths and must become a fixed error."""
    module = _load_macos_deploy()

    class UnreadableRootEnvironment(FakeFileSystem):
        def read_text(self, path: Path) -> str:
            if path.resolve() == (ROOT / ".env").resolve():
                raise OSError("private host path")
            return super().read_text(path)

    filesystem = UnreadableRootEnvironment()
    runner = existing_mac_runner(filesystem=filesystem)

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner, filesystem=filesystem).deploy_existing()

    assert caught.value.error_code == "ROOT_ENV_INVALID"
    assert "private host path" not in str(caught.value)


def test_existing_mode_rejects_a_custom_secret_file_inside_build_context() -> None:
    """A custom in-tree env path could be sent to Docker outside the reviewed ignore rule."""
    module = _load_macos_deploy()
    filesystem = FakeFileSystem()
    runner = existing_mac_runner(filesystem=filesystem)

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(
            runner,
            filesystem=filesystem,
            env_file=Path("private/deployment-secrets.env"),
        ).deploy()

    assert caught.value.error_code == "ENV_FILE_IN_BUILD_CONTEXT"
    assert runner.calls == []


def test_existing_mode_requires_root_env_to_be_excluded_from_build_context() -> None:
    """The canonical root secret file must remain covered by Docker ignore rules."""
    module = _load_macos_deploy()
    filesystem = FakeFileSystem()
    filesystem.files[(ROOT / ".dockerignore").resolve()] = ("*.apk\n", 0o644)
    runner = existing_mac_runner(filesystem=filesystem)

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner, filesystem=filesystem).deploy_existing()

    assert caught.value.error_code == "ROOT_ENV_NOT_IGNORED"
    assert runner.calls == []


def test_existing_mode_allows_a_secret_env_file_outside_build_context(
    tmp_path: Path,
) -> None:
    """Operators may keep the deployment env in a separate protected directory."""
    external_env = (tmp_path / "deployment.env").resolve()
    filesystem = FakeFileSystem(env_exists=False)
    filesystem.files[external_env] = (REQUIRED_ROOT_ENV, 0o600)
    runner = existing_mac_runner(filesystem=filesystem)

    result = make_orchestrator(
        runner,
        filesystem=filesystem,
        env_file=external_env,
    ).deploy_existing()

    assert result.state == "READY"
    rendered = "\n".join(" ".join(call) for call in runner.calls)
    assert f"--env-file {external_env} --env-file deploy/macos.env" in rendered
    assert not any(call[0].endswith("setup-admin.sh") for call in runner.calls)


@pytest.mark.parametrize("env_exists", [False, True])
def test_existing_mode_creates_only_a_missing_root_environment(env_exists: bool) -> None:
    """Re-running setup-admin against an existing file could rotate production secrets."""
    filesystem = FakeFileSystem(env_exists=env_exists)
    runner = existing_mac_runner(filesystem=filesystem)

    make_orchestrator(runner, filesystem=filesystem).deploy_existing()

    setup_calls = [call for call in runner.calls if call[0].endswith("setup-admin.sh")]
    assert len(setup_calls) == (0 if env_exists else 1)
    assert filesystem.read_text(ROOT / ".env") == REQUIRED_ROOT_ENV


def test_existing_mode_allows_a_truly_stopped_role_then_starts_it_through_broker() -> None:
    """No ADB device and no process on the fixed port is a safe stopped role."""
    events: list[str] = []
    filesystem = FakeFileSystem()
    runner = existing_mac_runner(
        adb_states={"emulator-5556": None},
        process_snapshots=[""],
        filesystem=filesystem,
        events=events,
    )
    broker = FakeLifecycleBroker(
        {"core_metrics": "STOPPED", "main_fund_flow": "RUNNING"},
        events=events,
    )

    make_orchestrator(runner, filesystem=filesystem, broker=broker).deploy_existing()

    assert broker.start_calls == ["core_metrics"]
    assert broker.wait_calls == [
        ("operation-core_metrics", "RUNNING", 180.0),
    ]
    pre_fund = events.index("identity:emulator-5554:THS_API_33_ARM64")
    assert events.index("installer") < events.index("broker-start:core_metrics")
    assert events.index("broker-start:core_metrics") < events.index(
        "broker-wait:operation-core_metrics"
    )
    post_core = len(events) - 1 - events[::-1].index(
        "identity:emulator-5556:THS_CORE_33_ARM64"
    )
    post_fund = len(events) - 1 - events[::-1].index(
        "identity:emulator-5554:THS_API_33_ARM64"
    )
    adb_devices_event = next(event for event in events if event.startswith("adb-devices:"))
    assert events.index("adb-state:emulator-5556:None") < events.index(
        adb_devices_event
    )
    assert events.index(adb_devices_event) < events.index("process-snapshot:")
    assert events.index("process-snapshot:") < pre_fund < events.index("installer")
    assert events.index("broker-wait:operation-core_metrics") < post_core < post_fund


def test_existing_mode_rechecks_fixed_serial_identity_after_broker_startup() -> None:
    """Broker startup must not leave a newly attached wrong AVD trusted by serial alone."""
    module = _load_macos_deploy()
    events: list[str] = []
    runner = existing_mac_runner(
        adb_states={"emulator-5556": None},
        process_snapshots=[""],
        avd_identity_sequences={
            "emulator-5556": ["THS_API_33_ARM64"],
        },
        events=events,
    )
    broker = FakeLifecycleBroker(
        {"core_metrics": "STOPPED", "main_fund_flow": "RUNNING"},
        events=events,
    )

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner, broker=broker).deploy_existing()

    assert caught.value.error_code == "FIXED_AVD_IDENTITY_MISMATCH"
    assert events.index("installer") < events.index("broker-start:core_metrics")
    assert events.index("broker-wait:operation-core_metrics") < events.index(
        "identity:emulator-5556:THS_API_33_ARM64"
    )
    assert not any("pm" in call and "path" in call for call in runner.calls)
    assert not any(call[-3:] == ("up", "-d", "--build") for call in runner.calls)


def test_existing_mode_sanitizes_compose_health_timeout() -> None:
    """Compose diagnostics can contain host paths and must not escape fixed errors."""
    module = _load_macos_deploy()
    runner = existing_mac_runner(healthy=False)

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(
            runner, health_timeout_seconds=0.001
        ).deploy_existing()

    assert caught.value.error_code == "COMPOSE_HEALTH_TIMEOUT"
    assert str(caught.value) == "COMPOSE_HEALTH_TIMEOUT"
    assert "private compose diagnostics" not in str(caught.value)


def test_auto_is_the_default_mode_and_two_fixed_avds_choose_existing() -> None:
    """The one-command entrypoint should preserve a complete current Mac by default."""
    module = _load_macos_deploy()
    parser = module.build_argument_parser()
    runner = existing_mac_runner()
    orchestrator = make_orchestrator(runner)

    assert parser.parse_args([]).mode == "auto"
    assert orchestrator.deploy().mode == "existing"


@pytest.mark.parametrize(
    ("avds", "expected_missing"),
    [
        ((), frozenset({"core_metrics", "main_fund_flow"})),
        (("THS_API_33_ARM64",), frozenset({"core_metrics"})),
        (("THS_CORE_33_ARM64",), frozenset({"main_fund_flow"})),
        (
            ("UNRELATED_TEST_AVD", "THS_API_33_ARM64"),
            frozenset({"core_metrics"}),
        ),
    ],
)
def test_auto_provisions_only_the_immutable_initial_missing_role_set(
    avds: tuple[str, ...], expected_missing: frozenset[str]
) -> None:
    """Re-listing after creation could expand provisioning onto a preserved role."""
    runner = existing_mac_runner(avds=avds)
    orchestrator = make_orchestrator(runner)

    result = orchestrator.deploy("auto")

    assert result.mode == "provision"
    assert result.state == "FIRST_TIME_LOGIN_REQUIRED"
    assert orchestrator.initial_missing_roles == expected_missing
    created_roles = {
        {
            "THS_CORE_33_ARM64": "core_metrics",
            "THS_API_33_ARM64": "main_fund_flow",
        }[call[call.index("--name") + 1]]
        for call in runner.calls
        if call[:3] == ("avdmanager", "create", "avd")
    }
    assert created_roles == expected_missing
    assert orchestrator.initial_missing_roles == expected_missing


def test_provisioning_uses_only_fixed_image_avd_launch_and_asset_commands() -> None:
    """A dynamic package, AVD, serial, or asset command would cross the fixed-role boundary."""
    events: list[str] = []
    runner = existing_mac_runner(
        avds=(),
        system_image_installed=False,
        events=events,
    )
    broker = FakeLifecycleBroker(events=events)

    result = make_orchestrator(runner, broker=broker).deploy("auto")

    assert result.state == "FIRST_TIME_LOGIN_REQUIRED"
    sdk_install = (
        "sdkmanager",
        "system-images;android-33;google_apis;arm64-v8a",
    )
    assert sdk_install in runner.calls
    assert runner.inputs[runner.calls.index(sdk_install)] == b""
    expected_creates = [
        (
            "avdmanager",
            "create",
            "avd",
            "--name",
            "THS_CORE_33_ARM64",
            "--package",
            "system-images;android-33;google_apis;arm64-v8a",
        ),
        (
            "avdmanager",
            "create",
            "avd",
            "--name",
            "THS_API_33_ARM64",
            "--package",
            "system-images;android-33;google_apis;arm64-v8a",
        ),
    ]
    assert [
        call for call in runner.calls if call[:3] == ("avdmanager", "create", "avd")
    ] == expected_creates
    for create in expected_creates:
        create_index = runner.calls.index(create)
        assert runner.inputs[create_index] == b"no\n"
        assert "--force" not in create
    assert (
        "launchctl",
        "submit",
        "-l",
        "com.ths.avd.5556",
        "--",
        "/fake/emulator",
        "-avd",
        "THS_CORE_33_ARM64",
        "-port",
        "5556",
        "-no-snapshot",
        "-no-audio",
        "-gpu",
        "host",
        "-memory",
        "2048",
        "-cores",
        "4",
    ) in runner.calls
    assert (
        "launchctl",
        "submit",
        "-l",
        "com.ths.avd.5554",
        "--",
        "/fake/emulator",
        "-avd",
        "THS_API_33_ARM64",
        "-port",
        "5554",
        "-no-snapshot",
        "-no-audio",
        "-gpu",
        "host",
        "-memory",
        "2048",
        "-cores",
        "4",
    ) in runner.calls
    container_calls = [
        call for call in runner.calls if "container-provision-device" in call
    ]
    assert [call[-1] for call in container_calls] == [
        "core_metrics",
        "main_fund_flow",
    ]
    for call in container_calls:
        assert call == (
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
            "ths-level2-api:local",
            call[-1],
        )
    display_calls = [
        call
        for call in runner.calls
        if call and call[0].endswith("configure-macos-core-display.sh")
    ]
    assert display_calls == [
        (
            str(ROOT / "scripts/configure-macos-core-display.sh"),
            "emulator-5556",
            "adb",
        )
    ]
    assert events.index("container-provision:core_metrics") < events.index("installer")
    assert events.index("container-provision:main_fund_flow") < events.index("installer")
    assert events.index("container-provision:core_metrics") < events.index(
        "avd-created:THS_API_33_ARM64"
    )
    assert events.index("installer") < events.index("broker-start:core_metrics")
    assert events.index("installer") < events.index("broker-start:main_fund_flow")
    assert broker.start_calls == ["core_metrics", "main_fund_flow"]
    assert not any(
        call and call[0] in {"brew", "open", "softwareupdate"}
        for call in runner.calls
    )


def test_partial_provisioning_preserves_and_validates_the_existing_role() -> None:
    """A mixed host must not install or push image assets onto its preserved account."""
    runner = existing_mac_runner(avds=("THS_API_33_ARM64",))

    make_orchestrator(runner).deploy("auto")

    assert [
        call[-1] for call in runner.calls if "container-provision-device" in call
    ] == ["core_metrics"]
    assert [
        call[call.index("--name") + 1]
        for call in runner.calls
        if call[:3] == ("avdmanager", "create", "avd")
    ] == ["THS_CORE_33_ARM64"]
    fund_calls = [call for call in runner.calls if "emulator-5554" in call]
    assert any("pm" in call and "path" in call for call in fund_calls)
    assert any("sha256sum" in call for call in fund_calls)
    assert not any(
        any(token in call for token in ("install", "push", "chmod"))
        for call in fund_calls
    )


@pytest.mark.parametrize(
    ("stderr", "expected_error"),
    [
        (b"License for package Android SDK Platform 33 not accepted", "ANDROID_LICENSE_REQUIRED"),
        (b"network download failed with private host details", "ANDROID_SYSTEM_IMAGE_UNAVAILABLE"),
    ],
)
def test_provisioning_sanitizes_system_image_install_failures(
    stderr: bytes, expected_error: str
) -> None:
    """SDK diagnostics and license prompts must collapse to fixed operator-safe errors."""
    module = _load_macos_deploy()
    runner = existing_mac_runner(
        avds=(),
        system_image_installed=False,
        sdkmanager_install_returncode=1,
        sdkmanager_install_stderr=stderr,
    )

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner).deploy("auto")

    assert caught.value.error_code == expected_error
    assert str(caught.value) == expected_error
    sdk_install = (
        "sdkmanager",
        "system-images;android-33;google_apis;arm64-v8a",
    )
    sdk_install_index = runner.calls.index(sdk_install)
    assert runner.inputs[sdk_install_index] == b""
    assert not any(call[:3] == ("avdmanager", "create", "avd") for call in runner.calls)
    assert not any("--licenses" in call for call in runner.calls)
    assert b"yes\n" not in runner.inputs


def test_provisioning_boot_timeout_preserves_the_created_avd_without_cleanup() -> None:
    """A failed first boot must remain resumable instead of deleting partial AVD state."""
    module = _load_macos_deploy()
    runner = existing_mac_runner(
        avds=("THS_API_33_ARM64",),
        boot_completed={"emulator-5556": "0"},
    )

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner, boot_timeout_seconds=0.001).deploy("auto")

    assert caught.value.error_code == "DEVICE_BOOT_TIMEOUT"
    assert "THS_CORE_33_ARM64" in runner.avds
    rendered = "\n".join(" ".join(call) for call in runner.calls)
    for forbidden in ("delete avd", "avdmanager delete", "wipe-data", "rm -rf", "--force"):
        assert forbidden not in rendered
    assert not any("container-provision-device" in call for call in runner.calls)


def test_missing_sessions_return_only_safe_human_onboarding_instructions() -> None:
    """The human gate must be actionable without exposing deployment or account secrets."""
    acceptance = FakeDataOnlyAcceptance()
    runner = existing_mac_runner(sessions_ready=False)

    result = make_orchestrator(runner, acceptance=acceptance).deploy("provision")

    assert result.mode == "provision"
    assert result.state == "FIRST_TIME_LOGIN_REQUIRED"
    assert result.error_code is None
    output = json.dumps(result.__dict__, ensure_ascii=False)
    for required in (
        "http://127.0.0.1:8001/#admin",
        "manually log in and complete verification for each newly created role",
        "click the matching role's session refresh",
        "rerun scripts/provision-macos-from-image.sh",
    ):
        assert required in output.lower()
    for forbidden in (
        "lifecycle-secret-value",
        "session-secret-value",
        "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        "cookie",
        "/.android/avd/",
        "private compose diagnostics",
        "account identity",
    ):
        assert forbidden.lower() not in output.lower()
    assert acceptance.calls == 0


def test_provisioning_rerun_does_not_recreate_or_reinstall_existing_roles() -> None:
    """Resuming after manual login must preserve the AVDs created by the prior invocation."""
    runner = existing_mac_runner(avds=("THS_API_33_ARM64",), sessions_ready=False)
    first = make_orchestrator(runner).deploy("provision")
    first_call_count = len(runner.calls)

    second_orchestrator = make_orchestrator(runner)
    second = second_orchestrator.deploy("provision")
    resumed_calls = runner.calls[first_call_count:]

    assert first.state == second.state == "FIRST_TIME_LOGIN_REQUIRED"
    assert second_orchestrator.initial_missing_roles == frozenset()
    assert not any(
        call[:3] == ("avdmanager", "create", "avd") for call in resumed_calls
    )
    assert not any("container-provision-device" in call for call in resumed_calls)


def test_provisioning_becomes_ready_only_after_sessions_and_data_acceptance() -> None:
    """Session files alone must not bypass the required data-only acceptance task."""
    acceptance = FakeDataOnlyAcceptance()
    runner = existing_mac_runner(sessions_ready=True)

    result = make_orchestrator(runner, acceptance=acceptance).deploy("provision")

    assert result.state == "READY"
    assert acceptance.calls == 1
    assert not any(call[:3] == ("avdmanager", "create", "avd") for call in runner.calls)
    assert not any("container-provision-device" in call for call in runner.calls)


def test_data_only_acceptance_uses_the_confirmed_fixed_symbol_and_eight_metrics() -> None:
    """A READY provisioning result must come from a real data-only completed job."""
    module = _load_macos_deploy()
    required_values = {
        "stock_name": "招商轮船",
        "current_price": "8.12",
        "change_percent": "+1.25%",
        "turnover_rate": "0.72%",
        "large_order_net": "1.23",
        "large_order_amount": "456.7",
        "retail_count": "12.34",
        "macdfs": "+0.123",
    }
    responses = iter(
        [
            {"symbol": "601872", "name": "招商轮船", "market": "17"},
            {"public_id": "safe-public-id", "status": "QUEUED"},
            {"public_id": "safe-public-id", "status": "RUNNING"},
            {
                "public_id": "safe-public-id",
                "symbol": "601872",
                "include_long_capture": False,
                "status": "COMPLETED",
                "values": required_values,
            },
        ]
    )
    requests: list[Request] = []

    class Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self.payload).encode()

    def opener(request: Request, timeout: float):
        assert timeout > 0
        requests.append(request)
        return Response(next(responses))

    acceptance = module.LoopbackDataOnlyAcceptance(
        opener=opener,
        timeout_seconds=0.1,
        poll_interval_seconds=0.0,
    )

    acceptance.verify()

    assert [request.get_method() for request in requests] == [
        "GET",
        "POST",
        "GET",
        "GET",
    ]
    assert requests[0].full_url == "http://127.0.0.1:8001/api/v1/symbols/601872"
    assert json.loads(requests[1].data or b"") == {
        "symbol": "601872",
        "include_long_capture": False,
    }
    assert requests[2].full_url.endswith("/api/v1/jobs/safe-public-id")


def test_provision_wrapper_resolves_the_project_root_and_forces_provision_mode(
    tmp_path: Path,
) -> None:
    """The onboarding wrapper must not expose a mode, interpreter, or checkout ambiguity."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "python.log"
    python3 = fake_bin / "python3"
    python3.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$PWD\" > \"$PROVISION_WRAPPER_LOG\"\n"
        "printf '%s\\n' \"$*\" >> \"$PROVISION_WRAPPER_LOG\"\n",
        encoding="utf-8",
    )
    python3.chmod(0o755)

    completed = subprocess.run(
        [str(PROVISION_WRAPPER), "--env-file", "/tmp/operator.env"],
        cwd=tmp_path,
        env=os.environ
        | {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "PROVISION_WRAPPER_LOG": str(log),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == completed.stderr == ""
    assert log.read_text(encoding="utf-8").splitlines() == [
        str(ROOT),
        "scripts/macos_deploy.py --mode provision --env-file /tmp/operator.env",
    ]


def test_cli_and_wrapper_expose_no_device_or_asset_override_surface() -> None:
    """User-supplied serials, ports, URLs, or images would bypass reviewed constants."""
    module = _load_macos_deploy()
    parser = module.build_argument_parser()
    wrapper = ONE_CLICK_WRAPPER.read_text(encoding="utf-8")

    parsed = parser.parse_args(
        ["--mode", "existing", "--project-root", str(ROOT), "--env-file", ".env"]
    )
    assert parsed.mode == "existing"
    with pytest.raises(SystemExit):
        parser.parse_args(["--serial", "emulator-9999"])
    assert wrapper == (
        "#!/bin/sh\n"
        "set -eu\n"
        'script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'exec "${PYTHON_BIN:-python3}" "$script_dir/macos_deploy.py" "$@"\n'
    )
