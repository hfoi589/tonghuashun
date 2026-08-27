from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from level2_service.main import DeploymentSettings


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_MACOS_COMPOSE_COMMAND = (
    "docker --context orbstack compose --env-file .env "
    "--env-file deploy/macos.env -f deploy/compose.yml up -d --build"
)


def lifecycle_environment(**overrides: str) -> dict[str, str]:
    return {
        "ADMIN_PASSWORD_HASH": "$argon2id$example",
        "ADMIN_SESSION_SECRET": "s" * 32,
        "ADB_SERIAL": "emulator-5556",
        **overrides,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"THS_DEVICE_LIFECYCLE_URL": "http://localhost:18765"},
        {"THS_DEVICE_LIFECYCLE_TOKEN": "secret"},
    ],
)
def test_lifecycle_configuration_requires_url_and_token_together(
    overrides: dict[str, str],
) -> None:
    """A half-configured host control capability must not start accidentally."""
    with pytest.raises(ValueError, match="THS_DEVICE_LIFECYCLE_URL.*TOKEN"):
        DeploymentSettings.from_environ(lifecycle_environment(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"THS_DEVICE_LIFECYCLE_TIMEOUT_SECONDS": "0"},
        {"THS_DEVICE_LIFECYCLE_TIMEOUT_SECONDS": "not-a-number"},
        {"THS_DEVICE_LIFECYCLE_TIMEOUT_SECONDS": "nan"},
        {"THS_DEVICE_LIFECYCLE_TIMEOUT_SECONDS": "inf"},
        {
            "THS_DEVICE_LIFECYCLE_URL": "https://host.docker.internal:18765",
            "THS_DEVICE_LIFECYCLE_TOKEN": "secret",
        },
        {
            "THS_DEVICE_LIFECYCLE_URL": "http://broker.example.test:18765",
            "THS_DEVICE_LIFECYCLE_TOKEN": "secret",
        },
    ],
)
def test_lifecycle_configuration_rejects_unsafe_timeout_or_url(
    overrides: dict[str, str],
) -> None:
    """Unsafe lifecycle connection settings could expose host controls externally."""
    with pytest.raises(ValueError):
        DeploymentSettings.from_environ(lifecycle_environment(**overrides))


def test_missing_lifecycle_settings_leave_the_feature_disabled() -> None:
    """Normal API startup must remain available when lifecycle controls are omitted."""
    settings = DeploymentSettings.from_environ(lifecycle_environment())

    assert settings.device_lifecycle_url is None
    assert settings.device_lifecycle_token is None
    assert settings.device_lifecycle_timeout_seconds == 5.0


def test_complete_lifecycle_settings_remain_enabled() -> None:
    """Removing configured host controls would make the admin lifecycle feature inert."""
    settings = DeploymentSettings.from_environ(
        lifecycle_environment(
            THS_DEVICE_LIFECYCLE_URL="http://host.docker.internal:18765",
            THS_DEVICE_LIFECYCLE_TOKEN="secret",
        )
    )

    assert settings.device_lifecycle_url == "http://host.docker.internal:18765"
    assert settings.device_lifecycle_token == "secret"


def test_compose_injects_lifecycle_settings_without_a_token_default() -> None:
    """A compose fallback token would turn a sample deployment into host access."""
    compose = (ROOT / "deploy" / "compose.yml").read_text(encoding="utf-8")

    assert "THS_DEVICE_LIFECYCLE_URL: ${THS_DEVICE_LIFECYCLE_URL:-}" in compose
    assert "THS_DEVICE_LIFECYCLE_TOKEN: ${THS_DEVICE_LIFECYCLE_TOKEN:-}" in compose
    assert "THS_DEVICE_LIFECYCLE_TIMEOUT_SECONDS: ${THS_DEVICE_LIFECYCLE_TIMEOUT_SECONDS:-5}" in compose


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is not installed")
def test_compose_profiles_parse_without_starting_containers() -> None:
    """A malformed profile would block deployment before any Android workload starts."""
    environment = os.environ | {
        "ADMIN_PASSWORD_HASH": "$argon2id$placeholder",
        "ADMIN_SESSION_SECRET": "s" * 32,
    }
    result = subprocess.run(
        ["docker", "compose", "-f", "deploy/compose.yml", "--profile", "linux-redroid", "config"],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "redroid/redroid:13.0.0_64only-latest" in result.stdout
    assert "host.docker.internal=host-gateway" in result.stdout
    assert "aliases:" in result.stdout
    assert "      - redroid" in result.stdout


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is not installed")
def test_compose_publishes_only_the_fastapi_frontend_and_api() -> None:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            "deploy/macos.env.example",
            "-f",
            "deploy/compose.yml",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    services = config["services"]
    api = services["api"]

    assert "caddy" not in services
    assert api["ports"] == [
        {
            "mode": "ingress",
            "target": 8000,
            "published": "8001",
            "protocol": "tcp",
        }
    ]
    assert api["environment"]["FRONTEND_ROOT"] == "/app/frontend"
    assert api["environment"]["ADMIN_COOKIE_SECURE"] == "0"
    assert api["environment"]["FRIDA_SERVER_ENDPOINT"] == "redroid:27042"
    assert api["environment"]["CORE_ADB_SERIAL"] == "emulator-5556"
    assert api["environment"]["CORE_FRIDA_SERVER_ENDPOINT"] == "host.docker.internal:27043"
    assert api["environment"]["FUND_ADB_SERIAL"] == "emulator-5554"
    assert api["environment"]["FUND_FRIDA_SERVER_ENDPOINT"] == "host.docker.internal:27042"
    assert api["environment"]["CORE_METRICS_TRANSPORT"] == "frida"
    assert api["environment"]["FUND_FLOW_TRANSPORT"] == "frida"
    assert api["environment"]["THS_SESSION_ROOT"] == "/data/admin/ths-sessions"
    assert api["environment"]["DAILY_CHECK_STATE_FILE"] == "/data/admin/daily-check.json"
    assert api["environment"]["SYMBOL_CATALOG_PATH"] == "/data/market/symbol-catalog.db"
    assert api["environment"]["SYMBOL_CATALOG_MAX_AGE_SECONDS"] == "604800"
    assert api["environment"]["SYMBOL_CATALOG_REFRESH_HOUR"] == "16"
    assert api["environment"]["SYMBOL_CATALOG_REFRESH_MINUTE"] == "20"
    assert api["environment"]["PUBLIC_MARKET_TIMEOUT_SECONDS"] == "8"
    assert api["environment"]["MARKET_DIRECT_ENRICHMENT"] == "1"
    assert api["environment"]["MARKET_DIRECT_ENRICHMENT_TTL_SECONDS"] == "15"
    assert api["environment"]["CORE_WARM_CONNECTION_MAX_IDLE_SECONDS"] == "25"


def test_api_image_embeds_frontend_without_a_caddy_stage() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY --from=frontend-build /src/frontend/dist /app/frontend" in dockerfile
    assert "FROM caddy:" not in dockerfile


def test_caddy_configuration_file_is_removed() -> None:
    assert not (ROOT / "deploy" / "Caddyfile").exists()


def test_macos_environment_example_contains_no_caddy_settings() -> None:
    example = (ROOT / "deploy" / "macos.env.example").read_text(encoding="utf-8")

    assert "APP_PORT=8001" in example
    assert "CORE_ADB_SERIAL=emulator-5556" in example
    assert "FUND_ADB_SERIAL=emulator-5554" in example
    assert "CORE_METRICS_TRANSPORT=frida" in example
    assert "FUND_FLOW_TRANSPORT=frida" in example
    assert "THS_SESSION_ROOT=/data/admin/ths-sessions" in example
    assert "SYMBOL_CATALOG_PATH=/data/market/symbol-catalog.db" in example
    assert "MARKET_DIRECT_ENRICHMENT=1" in example
    assert "CADDY_" not in example


def test_macos_deployment_docs_use_the_binding_orbstack_command() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    example = (ROOT / "deploy" / "macos.env.example").read_text(encoding="utf-8")

    assert CANONICAL_MACOS_COMPOSE_COMMAND in readme
    assert CANONICAL_MACOS_COMPOSE_COMMAND in example
    assert "docker compose --env-file deploy/macos.env" not in readme
    assert "docker compose --env-file deploy/macos.env" not in example


def test_macos_example_and_compose_document_a_secretless_lifecycle_configuration() -> None:
    """A sample token or omitted broker settings could expose host lifecycle controls."""
    example = (ROOT / "deploy" / "macos.env.example").read_text(encoding="utf-8")
    compose = (ROOT / "deploy" / "compose.yml").read_text(encoding="utf-8")
    active_assignments = {
        name: value
        for raw_line in example.splitlines()
        if (line := raw_line.strip()) and not line.startswith("#") and "=" in line
        for name, value in [line.split("=", 1)]
    }

    assert active_assignments["THS_DEVICE_LIFECYCLE_URL"] == "http://host.docker.internal:18765"
    assert active_assignments["THS_DEVICE_LIFECYCLE_TIMEOUT_SECONDS"] == "5"
    assert active_assignments.get("THS_DEVICE_LIFECYCLE_TOKEN", "") == ""
    for setting in (
        "THS_DEVICE_LIFECYCLE_URL",
        "THS_DEVICE_LIFECYCLE_TOKEN",
        "THS_DEVICE_LIFECYCLE_TIMEOUT_SECONDS",
    ):
        assert f"{setting}: ${{{setting}:-" in compose


def test_readme_defines_the_unimplemented_private_complete_image_contract() -> None:
    """Operators need the image, preservation, and human-login boundary before provisioning ships."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    normalized_readme = " ".join(readme.split())

    assert "scripts/deploy-macos-one-click.sh --mode auto" in readme
    assert "local/private" in readme
    assert "APK" in readme and "Frida" in readme
    assert "existing AVD" in readme
    assert "FIRST_TIME_LOGIN_REQUIRED" in readme
    assert "INSTALLED_APK_MISMATCH" in readme
    assert "204 MB APK is tracked in Git history" in normalized_readme
    assert "old Docker build context and image excluded it" in normalized_readme
    assert "approved image is local/private-only" in normalized_readme
    assert "does not claim real deployment" in normalized_readme
    assert "clean-Mac acceptance" in normalized_readme


def test_lifecycle_token_docs_define_the_compose_and_host_broker_boundary() -> None:
    """Calling the root environment the token's only location would leave the broker unauthenticated."""
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    handoff = " ".join((ROOT / "handoff.md").read_text(encoding="utf-8").split())

    for document in (readme, handoff):
        assert "root `.env` is the source for Compose/API" in document
        assert "installer copies the same lifecycle Token into the mode-0600 host config" in document
        assert "never exposed through a plist, log, or browser" in document


def test_root_environment_example_documents_the_direct_session_key() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "THS_SESSION_ENCRYPTION_KEY=" in example


def test_api_image_installs_adb_client() -> None:
    """The runner container must have an ADB client to reach the host device."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "android-sdk-platform-tools adb ca-certificates" in dockerfile


def test_dual_macos_bootstrap_preserves_the_fund_avd_and_uses_explicit_serials() -> None:
    script_path = ROOT / "scripts" / "bootstrap-macos-dual-avd.sh"
    script = script_path.read_text(encoding="utf-8")

    assert "THS_API_33_ARM64" in script
    assert "THS_CORE_33_ARM64" in script
    assert "emulator-5554" in script
    assert "emulator-5556" in script
    assert 'launch_avd_if_needed "$core_serial" "$core_avd" 5556' in script
    assert 'adb_for "$fund_serial" install' not in script
    assert "uninstall" not in script
    assert "pm clear" not in script
    assert "force-stop" not in script
    assert "manual" in script.lower()
    adb_lines = [
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith("adb ")
    ]
    assert adb_lines
    assert all(" -s " in f" {line} " for line in adb_lines)


def test_core_display_setup_calibrates_only_the_explicit_device(tmp_path: Path) -> None:
    """The long-capture device must match the runner's 1080x1920 calibration."""
    fake_adb = tmp_path / "adb"
    command_log = tmp_path / "adb.log"
    fake_adb.write_text(
        """#!/bin/sh
echo "$*" >> "$ADB_COMMAND_LOG"
""",
        encoding="utf-8",
    )
    fake_adb.chmod(0o755)
    setup = ROOT / "scripts" / "configure-macos-core-display.sh"
    environment = os.environ | {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "ADB_COMMAND_LOG": str(command_log),
    }

    completed = subprocess.run(
        ["/bin/sh", str(setup), "emulator-5556", str(fake_adb)],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert command_log.read_text(encoding="utf-8").splitlines() == [
        "-s emulator-5556 shell wm size 1080x1920",
        "-s emulator-5556 shell wm density 480",
    ]


def test_macos_bridge_watcher_restores_root_frida_and_forward_after_a_reboot(
    tmp_path: Path,
) -> None:
    """An emulator restart must not leave the role online without its Frida bridge."""
    fake_adb = tmp_path / "adb"
    command_log = tmp_path / "adb.log"
    root_state = tmp_path / "root.state"
    frida_state = tmp_path / "frida.state"
    fake_adb.write_text(
        """#!/bin/sh
echo "$*" >> "$ADB_COMMAND_LOG"
case "$*" in
  *" get-state") echo device ;;
  *" shell getprop sys.boot_completed") echo 1 ;;
  *" shell id")
    if [ -f "$ADB_ROOT_STATE" ]; then echo 'uid=0(root)'; else echo 'uid=2000(shell)'; fi
    ;;
  *" root") : > "$ADB_ROOT_STATE" ;;
  *" shell pidof ths-frida-server")
    if [ -f "$ADB_FRIDA_STATE" ]; then echo 4321; else exit 1; fi
    ;;
  *" shell nohup /data/local/tmp/ths-frida-server"*) : > "$ADB_FRIDA_STATE" ;;
  *" forward tcp:27043 tcp:27042") ;;
  *) echo "unexpected adb command: $*" >&2; exit 9 ;;
esac
""",
        encoding="utf-8",
    )
    fake_adb.chmod(0o755)
    watcher = ROOT / "scripts" / "watch-macos-device-bridge.sh"
    environment = os.environ | {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "ADB_COMMAND_LOG": str(command_log),
        "ADB_ROOT_STATE": str(root_state),
        "ADB_FRIDA_STATE": str(frida_state),
    }

    completed = subprocess.run(
        [str(watcher), "--once", "emulator-5556", "27043", str(fake_adb)],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    commands = command_log.read_text(encoding="utf-8").splitlines()
    root_index = commands.index("-s emulator-5556 root")
    frida_index = next(
        index
        for index, command in enumerate(commands)
        if "shell nohup /data/local/tmp/ths-frida-server" in command
    )
    forward_index = commands.index("-s emulator-5556 forward tcp:27043 tcp:27042")
    assert root_index < frida_index < forward_index
