from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile, ZipInfo

import pytest
from fastapi.testclient import TestClient

from level2_service.main import DeploymentSettings, create_production_app
from scripts.preflight import PreflightError, validate_apk, validate_host_profile


class FakeRedis:
    """Enough of the Redis protocol for construction; no network is used."""

    def delete(self, *_args): pass
    def eval(self, *_args): return False
    def get(self, *_args): return None
    def lpush(self, *_args): pass
    def rpush(self, *_args): pass
    def sadd(self, *_args): pass
    def scard(self, *_args): return 0
    def set(self, *_args): pass
    def smembers(self, *_args): return set()
    def srem(self, *_args): pass
    def xadd(self, *_args): pass
    def xrange(self, *_args): return []


class FakeBridge:
    def __init__(self, *, adb: str, serial: str, environment: dict[str, str]) -> None:
        self.adb = adb
        self.serial = serial
        self.environment = environment


class FakeRunner:
    def __init__(self, store, navigator, capture_root, control) -> None:
        self.store = store
        self.navigator = navigator
        self.capture_root = capture_root
        self.control = control
        self.calls = 0

    def run_once(self) -> None:
        self.calls += 1
        self.control.heartbeat("READY")


def test_settings_reject_missing_production_secrets() -> None:
    """Starting an externally reachable admin endpoint without both secrets is unsafe."""
    with pytest.raises(ValueError, match="ADMIN_PASSWORD_HASH"):
        DeploymentSettings.from_environ({"ADMIN_SESSION_SECRET": "x" * 32})
    with pytest.raises(ValueError, match="ADMIN_SESSION_SECRET"):
        DeploymentSettings.from_environ({"ADMIN_PASSWORD_HASH": "$argon2id$example"})


def test_production_factory_wires_one_control_for_api_and_runner(tmp_path: Path) -> None:
    """Separate controls would let the WebSocket lock disagree with the worker state."""
    settings = DeploymentSettings.from_environ(
        {
            "ADMIN_PASSWORD_HASH": "$argon2id$example",
            "ADMIN_SESSION_SECRET": "s" * 32,
            "CAPTURE_ROOT": str(tmp_path),
            "REDIS_URL": "redis://queue:6379/0",
            "ADB_SERIAL": "android:5555",
            "ADB_SERVER_SOCKET": "tcp:adb-server:5037",
            "RUNNER_POLL_INTERVAL_SECONDS": "0.001",
        }
    )
    app = create_production_app(
        settings=settings,
        redis_client_factory=lambda url: FakeRedis(),
        bridge_factory=FakeBridge,
        runner_factory=FakeRunner,
    )

    assert app.state.runner.control is app.state.runner_control
    assert app.state.device_bridge.serial == "android:5555"
    assert app.state.device_bridge.environment["ADB_SERVER_SOCKET"] == "tcp:adb-server:5037"
    with TestClient(app):
        pass
    assert app.state.runner.calls >= 1
    assert app.state.runner_task.done()


def test_apk_validation_accepts_exact_digest_and_arm_library(tmp_path: Path) -> None:
    """A different binary or x86-only APK must not be presented as deployable."""
    apk = tmp_path / "ths.apk"
    with ZipFile(apk, "w") as archive:
        entry = ZipInfo("lib/arm64-v8a/libths.so", date_time=(2020, 1, 1, 0, 0, 0))
        archive.writestr(entry, b"native")

    result = validate_apk(apk, expected_sha256="3b96ca8f75a676c3d98112b88b74249e2b610f979036ec0ba0bf78b679246f17")

    assert result.abis == ("arm64-v8a",)
    with pytest.raises(PreflightError, match="SHA-256"):
        validate_apk(apk, expected_sha256="0" * 64)


def test_apk_validation_rejects_x86_only_binary_even_with_matching_digest(tmp_path: Path) -> None:
    """The ARM Android profiles cannot install an APK with only x86 native code."""
    apk = tmp_path / "x86-only.apk"
    with ZipFile(apk, "w") as archive:
        entry = ZipInfo("lib/x86_64/libths.so", date_time=(2020, 1, 1, 0, 0, 0))
        archive.writestr(entry, b"native")

    with pytest.raises(PreflightError, match="no supported ARM ABI"):
        validate_apk(apk, expected_sha256="63206260dd760757a5f6389eabdce1618ca46f6c7f4aa8cf233b66268266750b")


def test_host_profile_rejects_unsupported_linux_and_non_apple_silicon_mac() -> None:
    """The preflight must block incompatible hosts rather than imply arbitrary VPS support."""
    with pytest.raises(PreflightError, match="amd64"):
        validate_host_profile("linux-redroid", architecture="arm64", cpu_count=4, memory_bytes=8 << 30, free_bytes=30 << 30, docker_available=True, docker_rootless=False, binder_available=True)
    with pytest.raises(PreflightError, match="Apple Silicon"):
        validate_host_profile("macos-avd", architecture="x86_64", cpu_count=4, memory_bytes=8 << 30, free_bytes=30 << 30, apple_silicon=False, android_sdk_available=True, avd_available=True)
