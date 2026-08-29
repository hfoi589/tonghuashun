from __future__ import annotations

import base64
import os
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile, ZipInfo

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from level2_service.main import DeploymentSettings, create_production_app
from level2_service.app_sessions import EncryptedFileSessionProvider
from level2_service.daily_kline import DailyKlineMarketDataSource
from level2_service.direct_market import (
    Core9528Client,
    Core9528CurveDecoder,
    Core9528TemplateProtocol,
    FundFlowHttpClient,
    ShadowParsedValueSource,
)
from level2_service.market_accounts import RedisMarketSessionStore, SQLiteMarketAccountStore
from level2_service.market_data import MarketDataBroker
from level2_service.parsed_values import (
    DirectRequestError,
    DualAccountParsedValueSource,
    FridaParsedValueSource,
)
from level2_service.public_market import (
    DirectEnrichedMarketDataSource,
    PublicMarketDataSource,
)
from level2_service.runner import DailyCheckState, OpenCVTemplateFallback, long_capture_has_net_heading
from level2_service.symbol_catalog import SQLiteSymbolCatalog
from scripts.preflight import PreflightError, validate_apk, validate_host_profile


INSTALLER = Path(__file__).parents[1] / "scripts" / "install-macos-device-lifecycle.sh"


class FakeRedis:
    """Enough of the Redis protocol for construction; no network is used."""

    def delete(self, *_args): pass
    def eval(self, *_args): return False
    def get(self, *_args): return None
    def hdel(self, *_args): pass
    def hget(self, *_args): return None
    def hgetall(self, *_args): return {}
    def hset(self, *_args, **_kwargs): pass
    def lpush(self, *_args): pass
    def lrange(self, *_args): return []
    def lrem(self, *_args): return 0
    def rpush(self, *_args): pass
    def sadd(self, *_args): pass
    def scard(self, *_args): return 0
    def set(self, *_args): pass
    def setex(self, *_args): pass
    def smembers(self, *_args): return set()
    def srem(self, *_args): pass
    def xadd(self, *_args): pass
    def xdel(self, *_args): return 0
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


def test_lifecycle_installer_uses_stable_secret_and_launchagent_locations() -> None:
    """Referencing a worktree could leave the host broker broken after cleanup."""
    installer = INSTALLER.read_text(encoding="utf-8")

    assert ".config/ths-device-lifecycle.env" in installer
    assert "chmod 0600" in installer
    assert "Library/LaunchAgents/com.ths.device-lifecycle.plist" in installer
    assert ".local/lib/ths-device-lifecycle/" in installer
    assert "macos-device-lifecycle.py" in installer
    assert "macos_device_identity.py" in installer
    assert "watch-macos-device-bridge.sh" in installer
    assert "com.ths.device-bridge.27042.plist" in installer
    assert "com.ths.device-bridge.27043.plist" in installer


def test_lifecycle_installer_launches_only_stable_copies_without_secrets() -> None:
    """Embedding a token or repository path in a plist would expose or destabilize the broker."""
    installer = INSTALLER.read_text(encoding="utf-8")

    assert "launchctl bootstrap gui/$UID" in installer
    assert "launchctl kickstart -k gui/$UID/com.ths.device-lifecycle" in installer
    plist_writers = installer.split("write_service_plist() {", 1)[1].split(
        "write_service_plist\n", 1
    )[0]
    assert "THS_DEVICE_LIFECYCLE_TOKEN" not in plist_writers
    assert "$token" not in plist_writers
    assert "${project_root}" not in plist_writers
    assert "printf '%s\\n' \"$token\"" not in installer


def test_lifecycle_installer_excludes_forbidden_device_mutations() -> None:
    """Adding destructive ADB or AVD commands would violate the protected-device boundary."""
    installer = INSTALLER.read_text(encoding="utf-8")

    forbidden = ("force-stop", "pm clear", "install -r", " uninstall", "wipe-data", "avdmanager create")
    assert not [command for command in forbidden if command in installer]


def test_agents_define_the_narrow_dual_role_lifecycle_authorization() -> None:
    """Broader fund-device access would bypass the preserved account protections."""
    agents = (Path(__file__).parents[1] / "AGENTS.md").read_text(encoding="utf-8")

    for language_marker, broker_phrase in (
        ("## 本地部署", "已认证的 lifecycle broker"),
        ("## Local deployment", "authenticated lifecycle broker"),
    ):
        section = agents.split(language_marker, 1)[1]
        assert broker_phrase in section
        assert "start_and_launch_app" in section
        assert "shutdown" in section
    for forbidden in ("退出登录", "切换账号", "清数据", "重装/卸载 App", "force-stop", "自动页面导航"):
        assert forbidden in agents
    for forbidden in ("log it out", "switch accounts", "clear the App", "reinstall/uninstall", "force-stop", "navigate it automatically"):
        assert forbidden in agents


def test_handoff_documents_lifecycle_installation_queue_recovery_and_safe_rollback() -> None:
    """A lifecycle operation without its lock and rollback procedure risks protected state."""
    handoff = (Path(__file__).parents[1] / "handoff.md").read_text(encoding="utf-8")
    normalized_handoff = " ".join(handoff.split())

    for required in (
        "install-macos-device-lifecycle.sh",
        "LaunchAgent",
        "UNCONFIGURED",
        "DEVICE_LIFECYCLE_UNAVAILABLE",
        "DEVICE_SHUTDOWN_FAILED",
        "scripts/deploy-macos-one-click.sh --mode auto",
        "取得当前会话设备锁",
        "等待运行中的设备任务结束",
        "一次只操作一台设备",
        "显式恢复队列",
        "不删除 AVD、登录数据、",
        "会话包或 Docker 数据卷",
    ):
        assert required in normalized_handoff


def write_fake_tool(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def fake_lifecycle_environment(
    tmp_path: Path,
    *,
    emulator_fails: bool = False,
    install_fails: bool = False,
    emulator_in_path: bool = True,
) -> tuple[dict[str, str], Path]:
    tools = tmp_path / "tools"
    tools.mkdir()
    launch_log = tmp_path / "launchctl.log"
    write_fake_tool(tools / "uname", "printf '%s\\n' Darwin")
    write_fake_tool(tools / "python3", 'exec "$REAL_PYTHON" "$@"')
    write_fake_tool(tools / "adb", "exit 0")
    if install_fails:
        write_fake_tool(
            tools / "install",
            "printf '%s\\n' 'install stdout port=27043'\nprintf '%s\\n' 'install stderr path=/private/secret' >&2\nexit 25",
        )
    if emulator_in_path:
        if emulator_fails:
            emulator_body = "printf '%s\\n' 'emulator internal serial=emulator-5554' >&2\nexit 23"
        else:
            emulator_body = "[ \"${1:-}\" = -list-avds ] || exit 24\nprintf '%s\\n' THS_CORE_33_ARM64 THS_API_33_ARM64"
        write_fake_tool(tools / "emulator", emulator_body)
    write_fake_tool(tools / "launchctl", 'printf "%s\\n" "$*" >> "$FAKE_LAUNCHCTL_LOG"')
    environment = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "REAL_PYTHON": sys.executable,
        "FAKE_LAUNCHCTL_LOG": str(launch_log),
    }
    return environment, launch_log


def test_lifecycle_installer_runs_in_fake_home_with_safe_stable_artifacts(tmp_path: Path) -> None:
    """A real host install must not rely on the repository after the installer exits."""
    environment, launch_log = fake_lifecycle_environment(tmp_path)
    home = Path(environment["HOME"])
    home.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("UNRELATED_SETTING=preserved\n", encoding="utf-8")
    env_file.chmod(0o644)

    completed = subprocess.run(
        [str(INSTALLER), "--project-root", str(INSTALLER.parents[1]), "--env-file", str(env_file)],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == "DEVICE_LIFECYCLE_INSTALL_READY\n"
    assert completed.stderr == ""
    assert env_file.stat().st_mode & 0o777 == 0o600
    env_contents = env_file.read_text(encoding="utf-8")
    assert "UNRELATED_SETTING=preserved" in env_contents
    token = next(line.split("=", 1)[1] for line in env_contents.splitlines() if line.startswith("THS_DEVICE_LIFECYCLE_TOKEN="))
    host_config = home / ".config" / "ths-device-lifecycle.env"
    assert host_config.stat().st_mode & 0o777 == 0o600
    assert f"THS_DEVICE_LIFECYCLE_TOKEN={token}" in host_config.read_text(encoding="utf-8")
    runtime = home / ".local" / "lib" / "ths-device-lifecycle"
    assert {path.name for path in runtime.iterdir()} == {
        "macos-device-lifecycle.py",
        "macos_device_identity.py",
        "watch-macos-device-bridge.sh",
        "configure-macos-core-display.sh",
    }
    assert (runtime / "macos_device_identity.py").stat().st_mode & 0o777 == 0o644
    assert all(
        path.stat().st_mode & 0o777 == 0o755
        for path in runtime.iterdir()
        if path.name != "macos_device_identity.py"
    )
    plists = home / "Library" / "LaunchAgents"
    for plist in plists.glob("com.ths.device*.plist"):
        content = plist.read_text(encoding="utf-8")
        assert str(runtime) in content
        assert str(INSTALLER.parents[1]) not in content
        assert token not in content
    assert "bootstrap" in launch_log.read_text(encoding="utf-8")


def test_lifecycle_installer_uses_fixed_absolute_emulator_fallback_when_path_missing(
    tmp_path: Path,
) -> None:
    """A PATH-missing emulator must propagate the verified fixed binary to the broker."""
    environment, launch_log = fake_lifecycle_environment(
        tmp_path,
        emulator_in_path=False,
    )
    fallback = Path(
        "/opt/homebrew/share/android-commandlinetools/emulator/emulator"
    )
    assert fallback.is_file() and os.access(fallback, os.X_OK)
    avd_home = tmp_path / "avd"
    avd_home.mkdir()
    for avd_name in ("THS_CORE_33_ARM64", "THS_API_33_ARM64"):
        (avd_home / f"{avd_name}.ini").write_text(
            f"path=/nonexistent\npath.rel=avd/{avd_name}.avd\ntarget=android-33\n",
            encoding="utf-8",
        )
    environment.pop("ANDROID_HOME", None)
    environment.pop("ANDROID_SDK_ROOT", None)
    environment["ANDROID_AVD_HOME"] = str(avd_home)
    environment["ANDROID_EMULATOR_HOME"] = str(tmp_path)
    environment["EMULATOR_BIN"] = "/tmp/untrusted-emulator"
    home = Path(environment["HOME"])
    home.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("UNRELATED_SETTING=preserved\n", encoding="utf-8")

    completed = subprocess.run(
        [
            str(INSTALLER),
            "--project-root",
            str(INSTALLER.parents[1]),
            "--env-file",
            str(env_file),
        ],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == "DEVICE_LIFECYCLE_INSTALL_READY\n"
    assert completed.stderr == ""
    host_config = home / ".config" / "ths-device-lifecycle.env"
    config = host_config.read_text(encoding="utf-8")
    assert f"THS_DEVICE_LIFECYCLE_EMULATOR_BIN={fallback}\n" in config
    assert "THS_DEVICE_LIFECYCLE_EMULATOR_BIN=emulator\n" not in config
    service_plist = (
        home / "Library/LaunchAgents/com.ths.device-lifecycle.plist"
    ).read_text(encoding="utf-8")
    assert str(host_config) in service_plist
    assert "bootstrap" in launch_log.read_text(encoding="utf-8")


def test_lifecycle_installer_fails_when_path_and_controlled_emulator_fallback_are_missing(
    tmp_path: Path,
) -> None:
    """The installer remains fail-closed when no trusted emulator binary exists."""
    environment, _launch_log = fake_lifecycle_environment(
        tmp_path,
        emulator_in_path=False,
    )
    home = Path(environment["HOME"])
    home.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("UNRELATED_SETTING=preserved\n", encoding="utf-8")

    completed = subprocess.run(
        [
            str(INSTALLER),
            "--project-root",
            str(INSTALLER.parents[1]),
            "--env-file",
            str(env_file),
        ],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "DEVICE_LIFECYCLE_INSTALL_FAILED\n"


def test_lifecycle_installer_suppresses_failing_tool_output(tmp_path: Path) -> None:
    """Passing through emulator diagnostics could expose protected-device identifiers."""
    environment, _launch_log = fake_lifecycle_environment(tmp_path, emulator_fails=True)
    Path(environment["HOME"]).mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("UNRELATED_SETTING=preserved\n", encoding="utf-8")

    completed = subprocess.run(
        [str(INSTALLER), "--project-root", str(INSTALLER.parents[1]), "--env-file", str(env_file)],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "DEVICE_LIFECYCLE_INSTALL_FAILED\n"


def test_lifecycle_installer_suppresses_failing_file_utility_output(tmp_path: Path) -> None:
    """A failed copy utility must not print local paths before the fixed error code."""
    environment, _launch_log = fake_lifecycle_environment(tmp_path, install_fails=True)
    Path(environment["HOME"]).mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("UNRELATED_SETTING=preserved\n", encoding="utf-8")

    completed = subprocess.run(
        [str(INSTALLER), "--project-root", str(INSTALLER.parents[1]), "--env-file", str(env_file)],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "DEVICE_LIFECYCLE_INSTALL_FAILED\n"


class StaticCatalogSource:
    @staticmethod
    def fetch_symbols():
        from level2_service.parsed_values import SymbolLookup

        return [SymbolLookup("601872", "招商轮船", "17")]


def static_catalog_factory(path, source, **kwargs):
    return SQLiteSymbolCatalog(
        path,
        source,
        minimum_security_count=1,
        **kwargs,
    )


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
    assert secure_defaults.symbol_catalog_max_age_seconds == 604800
    assert secure_defaults.symbol_catalog_refresh_hour == 16
    assert secure_defaults.symbol_catalog_refresh_minute == 20
    assert secure_defaults.public_market_timeout_seconds == 8
    assert secure_defaults.market_direct_enrichment is True
    assert secure_defaults.market_direct_enrichment_ttl_seconds == 5
    assert secure_defaults.core_warm_connection_max_idle_seconds == 25
    assert secure_defaults.redis_connect_timeout_seconds == 5
    assert secure_defaults.redis_socket_timeout_seconds == 5
    assert secure_defaults.redis_startup_retry_attempts == 10
    assert secure_defaults.redis_startup_retry_delay_seconds == 1


def test_production_startup_retries_until_redis_is_ready(tmp_path: Path) -> None:
    attempts: list[int] = []

    class ReadinessRedis(FakeRedis):
        def ping(self) -> bool:
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError("redis is still starting")
            return True

    settings = DeploymentSettings.from_environ(
        {
            "ADMIN_PASSWORD_HASH": "$argon2id$example",
            "ADMIN_SESSION_SECRET": "s" * 32,
            "CAPTURE_ROOT": str(tmp_path),
            "ADB_SERIAL": "emulator-5554",
            "REDIS_STARTUP_RETRY_ATTEMPTS": "3",
            "REDIS_STARTUP_RETRY_DELAY_SECONDS": "0",
        }
    )

    create_production_app(
        settings=settings,
        redis_client_factory=lambda _url: ReadinessRedis(),
        bridge_factory=FakeBridge,
        runner_factory=FakeRunner,
    )

    assert len(attempts) == 3


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
    assert app.state.symbol_search.__self__ is app.state.symbol_catalog
    assert app.state.symbol_lookup.__self__ is app.state.symbol_catalog
    assert app.state.symbol_lookup_cache is None


def test_production_symbol_lookup_uses_the_public_catalog_not_frida(
    tmp_path: Path,
) -> None:
    settings = DeploymentSettings.from_environ(
        {
            "ADMIN_PASSWORD_HASH": "$argon2id$example",
            "ADMIN_SESSION_SECRET": "s" * 32,
            "CAPTURE_ROOT": str(tmp_path / "captures"),
            "SYMBOL_CATALOG_PATH": str(tmp_path / "symbols.db"),
            "ADB_SERIAL": "emulator-5556",
            "FRIDA_SERVER_ENDPOINT": "host.docker.internal:27043",
        }
    )
    app = create_production_app(
        settings=settings,
        redis_client_factory=lambda _url: FakeRedis(),
        bridge_factory=FakeBridge,
        runner_factory=FakeRunner,
        symbol_catalog_source=StaticCatalogSource(),
        symbol_catalog_factory=static_catalog_factory,
    )
    app.state.symbol_catalog.refresh()

    result = app.state.symbol_lookup("601872")

    assert result.name == "招商轮船"
    assert app.state.symbol_lookup.__self__ is app.state.symbol_catalog
    assert app.state.symbol_search.__self__ is app.state.symbol_catalog
    assert app.state.symbol_lookup_cache is None


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
    assert isinstance(app.state.market_data_broker.source, DailyKlineMarketDataSource)
    enriched = app.state.market_data_broker.source.base_source
    assert isinstance(enriched, DirectEnrichedMarketDataSource)
    assert isinstance(enriched.base_source, PublicMarketDataSource)
    assert enriched.direct_source is None
    assert app.state.market_data_broker.stats()["daily_kline"] == {
        "cache_entries": 0,
        "public_successes": 0,
        "fallback_successes": 0,
        "stale_cache_hits": 0,
        "failures": 0,
    }


def test_settings_require_all_four_dual_account_device_variables() -> None:
    base = {
        "ADMIN_PASSWORD_HASH": "$argon2id$example",
        "ADMIN_SESSION_SECRET": "s" * 32,
        "CORE_ADB_SERIAL": "emulator-5556",
    }

    with pytest.raises(ValueError, match="CORE_ADB_SERIAL.*CORE_FRIDA_SERVER_ENDPOINT.*FUND_ADB_SERIAL.*FUND_FRIDA_SERVER_ENDPOINT"):
        DeploymentSettings.from_environ(base)


def direct_encryption_key() -> str:
    return base64.urlsafe_b64encode(b"session-encryption-key-material!").decode("ascii")


def dual_environment(tmp_path: Path) -> dict[str, str]:
    return {
        "ADMIN_PASSWORD_HASH": "$argon2id$example",
        "ADMIN_SESSION_SECRET": "s" * 32,
        "CAPTURE_ROOT": str(tmp_path / "captures"),
        "CORE_ADB_SERIAL": "emulator-5556",
        "CORE_FRIDA_SERVER_ENDPOINT": "host.docker.internal:27043",
        "FUND_ADB_SERIAL": "emulator-5554",
        "FUND_FRIDA_SERVER_ENDPOINT": "host.docker.internal:27042",
        "THS_SESSION_ENCRYPTION_KEY": direct_encryption_key(),
        "THS_SESSION_ROOT": str(tmp_path / "sessions"),
    }


def test_settings_validate_direct_transport_modes_and_encryption_key(tmp_path: Path) -> None:
    invalid = dual_environment(tmp_path)
    invalid["FUND_FLOW_TRANSPORT"] = "other"
    with pytest.raises(ValueError, match="FUND_FLOW_TRANSPORT"):
        DeploymentSettings.from_environ(invalid)

    missing_key = dual_environment(tmp_path)
    missing_key["FUND_FLOW_TRANSPORT"] = "direct"
    missing_key.pop("THS_SESSION_ENCRYPTION_KEY")
    with pytest.raises(ValueError, match="THS_SESSION_ENCRYPTION_KEY"):
        DeploymentSettings.from_environ(missing_key)

    invalid_key = dual_environment(tmp_path)
    invalid_key["FUND_FLOW_TRANSPORT"] = "direct"
    invalid_key["THS_SESSION_ENCRYPTION_KEY"] = "not-a-fernet-key"
    with pytest.raises(ValueError, match="THS_SESSION_ENCRYPTION_KEY"):
        DeploymentSettings.from_environ(invalid_key)

    defaults = DeploymentSettings.from_environ(dual_environment(tmp_path))
    assert defaults.core_metrics_transport == "frida"
    assert defaults.fund_flow_transport == "frida"


@pytest.mark.parametrize(
    ("mode", "expected_type"),
    [
        ("direct", FundFlowHttpClient),
        ("shadow", ShadowParsedValueSource),
    ],
)
def test_production_factory_wires_the_fund_http_transport(
    tmp_path: Path,
    mode: str,
    expected_type: type,
) -> None:
    environment = dual_environment(tmp_path)
    environment["FUND_FLOW_TRANSPORT"] = mode
    settings = DeploymentSettings.from_environ(environment)

    app = create_production_app(
        settings=settings,
        redis_client_factory=lambda _url: FakeRedis(),
        bridge_factory=FakeBridge,
        runner_factory=FakeRunner,
    )

    source = app.state.runner.parsed_value_source
    assert isinstance(source.fund_source, expected_type)
    assert isinstance(app.state.account_session_provider, EncryptedFileSessionProvider)
    assert set(app.state.account_session_refreshers) == {"core_metrics", "main_fund_flow"}


def test_production_factory_wires_the_verified_core_decoder(tmp_path: Path) -> None:
    environment = dual_environment(tmp_path)
    environment["CORE_METRICS_TRANSPORT"] = "direct"
    settings = DeploymentSettings.from_environ(environment)

    app = create_production_app(
        settings=settings,
        redis_client_factory=lambda _url: FakeRedis(),
        bridge_factory=FakeBridge,
        runner_factory=FakeRunner,
    )

    source = app.state.runner.parsed_value_source
    assert isinstance(source.core_source, Core9528Client)
    assert isinstance(source.core_source.protocol, Core9528TemplateProtocol)
    assert isinstance(
        source.core_source.protocol.response_decoder,
        Core9528CurveDecoder,
    )


def test_production_core_direct_requires_session_material_before_socket_activity(
    tmp_path: Path,
) -> None:
    environment = dual_environment(tmp_path)
    environment["CORE_METRICS_TRANSPORT"] = "direct"
    app = create_production_app(
        settings=DeploymentSettings.from_environ(environment),
        redis_client_factory=lambda _url: FakeRedis(),
        bridge_factory=FakeBridge,
        runner_factory=FakeRunner,
    )
    client = app.state.runner.parsed_value_source.core_source
    client.session_provider.get = lambda _role: None
    client.protocol.socket_factory = lambda *_args: (_ for _ in ()).throw(
        AssertionError("missing session material must not create a socket")
    )

    with pytest.raises(DirectRequestError) as caught:
        client.read_direct("601872")

    assert caught.value.error_code == "DIRECT_SESSION_UNAVAILABLE"


def test_production_core_direct_keeps_market_snapshots_on_public_sources(
    tmp_path: Path,
) -> None:
    environment = dual_environment(tmp_path)
    environment["CORE_METRICS_TRANSPORT"] = "direct"
    app = create_production_app(
        settings=DeploymentSettings.from_environ(environment),
        redis_client_factory=lambda _url: FakeRedis(),
        bridge_factory=FakeBridge,
        runner_factory=FakeRunner,
    )

    market_source = app.state.market_data_broker.source.base_source

    assert isinstance(market_source, DirectEnrichedMarketDataSource)
    assert isinstance(market_source.base_source, PublicMarketDataSource)
    assert market_source.direct_source is None


def test_production_market_enables_l2_only_when_both_transports_are_direct(
    tmp_path: Path,
) -> None:
    environment = dual_environment(tmp_path)
    environment["CORE_METRICS_TRANSPORT"] = "direct"
    environment["FUND_FLOW_TRANSPORT"] = "direct"
    app = create_production_app(
        settings=DeploymentSettings.from_environ(environment),
        redis_client_factory=lambda _url: FakeRedis(),
        bridge_factory=FakeBridge,
        runner_factory=FakeRunner,
    )

    market_source = app.state.market_data_broker.source.base_source

    assert isinstance(market_source, DirectEnrichedMarketDataSource)
    assert market_source.direct_source is app.state.runner.parsed_value_source


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
    assert app.state.symbol_search.__self__ is app.state.symbol_catalog
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
        symbol_catalog_source=StaticCatalogSource(),
        symbol_catalog_factory=static_catalog_factory,
    )
    app.state.symbol_catalog.refresh()

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
        symbol_catalog_source=StaticCatalogSource(),
        symbol_catalog_factory=static_catalog_factory,
    )
    app.state.symbol_catalog.refresh()

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
