from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile, ZipInfo

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from level2_service.main import DeploymentSettings, create_production_app
from level2_service.market_accounts import RedisMarketSessionStore, SQLiteMarketAccountStore
from level2_service.market_data import MarketDataBroker
from level2_service.parsed_values import DualAccountParsedValueSource
from level2_service.runner import DailyCheckState, OpenCVTemplateFallback, long_capture_has_net_heading
from level2_service.symbol_cache import RedisSymbolLookupCache
from scripts.preflight import PreflightError, validate_apk, validate_host_profile


class FakeRedis:
    """Enough of the Redis protocol for construction; no network is used."""

    def delete(self, *_args): pass
    def eval(self, *_args): return False
    def get(self, *_args): return None
    def lpush(self, *_args): pass
    def lrange(self, *_args): return []
    def rpush(self, *_args): pass
    def sadd(self, *_args): pass
    def scard(self, *_args): return 0
    def set(self, *_args): pass
    def setex(self, *_args): pass
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
    def __init__(self, store, navigator, capture_root, control, *, parsed_value_source=None, daily_check_state=None, long_capture_validator=None) -> None:
        self.store = store
        self.navigator = navigator
        self.capture_root = capture_root
        self.control = control
        self.parsed_value_source = parsed_value_source
        self.daily_check_state = daily_check_state
        self.long_capture_validator = long_capture_validator
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


def test_settings_prefers_persisted_admin_password_file(tmp_path: Path) -> None:
    password_file = tmp_path / "password.hash"
    persisted_hash = "$argon2id$persisted"
    password_file.write_text(persisted_hash)

    settings = DeploymentSettings.from_environ(
        {
            "ADMIN_PASSWORD_HASH": "$argon2id$from-env",
            "ADMIN_PASSWORD_FILE": str(password_file),
            "ADMIN_SESSION_SECRET": "s" * 32,
            "ADB_SERIAL": "android:5555",
        }
    )

    assert settings.admin_password_hash == persisted_hash
    assert settings.admin_password_file == password_file.resolve()


def test_settings_parse_frontend_root_and_admin_cookie_secure(tmp_path: Path) -> None:
    frontend_root = tmp_path / "frontend"
    settings = DeploymentSettings.from_environ(
        {
            "ADMIN_PASSWORD_HASH": "$argon2id$example",
            "ADMIN_SESSION_SECRET": "s" * 32,
            "ADB_SERIAL": "android:5555",
            "FRONTEND_ROOT": str(frontend_root),
            "ADMIN_COOKIE_SECURE": "off",
        }
    )
    secure_defaults = DeploymentSettings.from_environ(
        {
            "ADMIN_PASSWORD_HASH": "$argon2id$example",
            "ADMIN_SESSION_SECRET": "s" * 32,
            "ADB_SERIAL": "android:5555",
        }
    )

    assert settings.frontend_root == frontend_root.resolve()
    assert settings.admin_cookie_secure is False
    assert secure_defaults.frontend_root is None
    assert secure_defaults.admin_cookie_secure is True


def test_production_factory_wires_the_configured_frida_runtime_source(tmp_path: Path) -> None:
    settings = DeploymentSettings.from_environ(
        {
            "ADMIN_PASSWORD_HASH": "$argon2id$example",
            "ADMIN_SESSION_SECRET": "s" * 32,
            "CAPTURE_ROOT": str(tmp_path),
            "ADB_SERIAL": "emulator-5554",
            "FRIDA_SERVER_ENDPOINT": "host.docker.internal:27042",
        }
    )

    app = create_production_app(
        settings=settings,
        redis_client_factory=lambda _url: FakeRedis(),
        bridge_factory=FakeBridge,
        runner_factory=FakeRunner,
    )

    assert settings.frida_server_endpoint == "host.docker.internal:27042"
    assert app.state.runner.parsed_value_source.endpoint == "host.docker.internal:27042"
    assert isinstance(app.state.symbol_lookup_cache, RedisSymbolLookupCache)


def test_production_factory_wires_persistent_market_accounts_sessions_and_broker(tmp_path: Path) -> None:
    database_path = tmp_path / "market" / "market.db"
    settings = DeploymentSettings.from_environ(
        {
            "ADMIN_PASSWORD_HASH": "$argon2id$example",
            "ADMIN_SESSION_SECRET": "s" * 32,
            "CAPTURE_ROOT": str(tmp_path / "captures"),
            "MARKET_DATABASE_PATH": str(database_path),
            "ADB_SERIAL": "emulator-5556",
            "FRIDA_SERVER_ENDPOINT": "host.docker.internal:27043",
        }
    )

    app = create_production_app(
        settings=settings,
        redis_client_factory=lambda _url: FakeRedis(),
        bridge_factory=FakeBridge,
        runner_factory=FakeRunner,
    )

    assert settings.market_database_path == database_path.resolve()
    assert database_path.is_file()
    assert isinstance(app.state.market_account_store, SQLiteMarketAccountStore)
    assert isinstance(app.state.market_session_store, RedisMarketSessionStore)
    assert isinstance(app.state.market_data_broker, MarketDataBroker)
    assert app.state.market_data_broker.source is app.state.runner.parsed_value_source


def test_settings_require_all_four_dual_account_device_variables() -> None:
    base = {
        "ADMIN_PASSWORD_HASH": "$argon2id$example",
        "ADMIN_SESSION_SECRET": "s" * 32,
        "CORE_ADB_SERIAL": "emulator-5556",
    }

    with pytest.raises(ValueError, match="CORE_ADB_SERIAL.*CORE_FRIDA_SERVER_ENDPOINT.*FUND_ADB_SERIAL.*FUND_FRIDA_SERVER_ENDPOINT"):
        DeploymentSettings.from_environ(base)


def test_production_factory_wires_two_independent_bridges_and_frida_sources(tmp_path: Path) -> None:
    settings = DeploymentSettings.from_environ(
        {
            "ADMIN_PASSWORD_HASH": "$argon2id$example",
            "ADMIN_SESSION_SECRET": "s" * 32,
            "CAPTURE_ROOT": str(tmp_path),
            "CORE_ADB_SERIAL": "emulator-5556",
            "CORE_FRIDA_SERVER_ENDPOINT": "host.docker.internal:27043",
            "FUND_ADB_SERIAL": "emulator-5554",
            "FUND_FRIDA_SERVER_ENDPOINT": "host.docker.internal:27042",
        }
    )

    app = create_production_app(
        settings=settings,
        redis_client_factory=lambda _url: FakeRedis(),
        bridge_factory=FakeBridge,
        runner_factory=FakeRunner,
    )

    assert settings.dual_account_mode is True
    assert app.state.device_bridges["core_metrics"].serial == "emulator-5556"
    assert app.state.device_bridges["main_fund_flow"].serial == "emulator-5554"
    assert app.state.runner.navigator.bridge is app.state.device_bridges["core_metrics"]
    assert isinstance(app.state.runner.parsed_value_source, DualAccountParsedValueSource)
    assert app.state.runner.parsed_value_source.core_source.endpoint == "host.docker.internal:27043"
    assert app.state.runner.parsed_value_source.core_source.request_scope == "core_metrics"
    assert app.state.runner.parsed_value_source.fund_source.endpoint == "host.docker.internal:27042"
    assert app.state.runner.parsed_value_source.fund_source.request_scope == "main_fund_flow"


def test_production_factory_persists_daily_check_in_the_admin_volume(tmp_path: Path) -> None:
    state_file = tmp_path / "admin" / "daily-check.json"
    settings = DeploymentSettings.from_environ(
        {
            "ADMIN_PASSWORD_HASH": "$argon2id$example",
            "ADMIN_SESSION_SECRET": "s" * 32,
            "CAPTURE_ROOT": str(tmp_path / "captures"),
            "DAILY_CHECK_STATE_FILE": str(state_file),
            "ADB_SERIAL": "emulator-5554",
        }
    )

    app = create_production_app(
        settings=settings,
        redis_client_factory=lambda _url: FakeRedis(),
        bridge_factory=FakeBridge,
        runner_factory=FakeRunner,
    )

    assert settings.daily_check_state_file == state_file.resolve()
    assert isinstance(app.state.runner.daily_check_state, DailyCheckState)
    assert app.state.runner.daily_check_state.path == state_file.resolve()


def test_settings_reject_invalid_admin_cookie_secure() -> None:
    with pytest.raises(ValueError, match="ADMIN_COOKIE_SECURE"):
        DeploymentSettings.from_environ(
            {
                "ADMIN_PASSWORD_HASH": "$argon2id$example",
                "ADMIN_SESSION_SECRET": "s" * 32,
                "ADB_SERIAL": "android:5555",
                "ADMIN_COOKIE_SECURE": "sometimes",
            }
        )


def test_production_factory_propagates_frontend_root_and_http_cookie_security(tmp_path: Path) -> None:
    frontend_root = tmp_path / "frontend"
    frontend_root.mkdir()
    (frontend_root / "index.html").write_text("<html>production frontend</html>", encoding="utf-8")
    (frontend_root / "market.webmanifest").write_text('{"start_url":"/market"}', encoding="utf-8")
    (frontend_root / "market-sw.js").write_text("self.addEventListener('fetch', () => {})", encoding="utf-8")
    settings = DeploymentSettings.from_environ(
        {
            "ADMIN_PASSWORD_HASH": PasswordHasher().hash("admin-secret"),
            "ADMIN_SESSION_SECRET": "s" * 32,
            "CAPTURE_ROOT": str(tmp_path / "captures"),
            "FRONTEND_ROOT": str(frontend_root),
            "ADMIN_COOKIE_SECURE": "0",
            "ADB_SERIAL": "android:5555",
        }
    )
    app = create_production_app(
        settings=settings,
        redis_client_factory=lambda url: FakeRedis(),
        bridge_factory=FakeBridge,
        runner_factory=FakeRunner,
    )

    with TestClient(app, base_url="http://testserver") as client:
        assert client.get("/").text == "<html>production frontend</html>"
        assert client.get("/market").text == "<html>production frontend</html>"
        assert client.get("/market.webmanifest").json()["start_url"] == "/market"
        assert "fetch" in client.get("/market-sw.js").text
        login = client.post("/api/admin/session", json={"password": "admin-secret"})
        assert login.status_code == 204
        assert all("; Secure" not in value for value in login.headers.get_list("set-cookie"))
        assert client.get("/api/admin/session").status_code == 204


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
    assert app.state.runner.long_capture_validator is long_capture_has_net_heading
    assert app.state.device_bridge.serial == "android:5555"
    assert app.state.device_bridge.environment["ADB_SERVER_SOCKET"] == "tcp:adb-server:5037"
    with TestClient(app):
        pass
    assert app.state.runner.calls >= 1
    assert app.state.runner_task.done()


def test_production_factory_loads_calibrated_opencv_templates(tmp_path: Path) -> None:
    template_root = tmp_path / "templates"
    template_root.mkdir()
    (template_root / "search.png").write_bytes(b"search-template")
    settings = DeploymentSettings.from_environ(
        {
            "ADMIN_PASSWORD_HASH": "$argon2id$example",
            "ADMIN_SESSION_SECRET": "s" * 32,
            "CAPTURE_ROOT": str(tmp_path / "captures"),
            "TEMPLATE_ROOT": str(template_root),
            "ADB_SERIAL": "android:5555",
        }
    )

    app = create_production_app(
        settings=settings,
        redis_client_factory=lambda url: FakeRedis(),
        bridge_factory=FakeBridge,
        runner_factory=FakeRunner,
    )

    fallback = app.state.runner.navigator.visual_fallback
    assert isinstance(fallback, OpenCVTemplateFallback)
    assert fallback.templates["search"] == template_root / "search.png"


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


def test_macos_profile_requires_docker_for_the_web_services() -> None:
    with pytest.raises(PreflightError, match="Docker is required"):
        validate_host_profile(
            "macos-avd",
            architecture="arm64",
            cpu_count=4,
            memory_bytes=8 << 30,
            free_bytes=30 << 30,
            docker_available=False,
            apple_silicon=True,
            android_sdk_available=True,
            avd_available=True,
        )
