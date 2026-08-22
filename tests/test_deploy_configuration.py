from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


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
            "published": "8000",
            "protocol": "tcp",
        }
    ]
    assert api["environment"]["FRONTEND_ROOT"] == "/app/frontend"
    assert api["environment"]["ADMIN_COOKIE_SECURE"] == "0"
    assert api["environment"]["FRIDA_SERVER_ENDPOINT"] == "host.docker.internal:27042"
    assert api["environment"]["DAILY_CHECK_STATE_FILE"] == "/data/admin/daily-check.json"


def test_api_image_embeds_frontend_without_a_caddy_stage() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY --from=frontend-build /src/frontend/dist /app/frontend" in dockerfile
    assert "FROM caddy:" not in dockerfile


def test_caddy_configuration_file_is_removed() -> None:
    assert not (ROOT / "deploy" / "Caddyfile").exists()


def test_macos_environment_example_contains_no_caddy_settings() -> None:
    example = (ROOT / "deploy" / "macos.env.example").read_text(encoding="utf-8")

    assert "APP_PORT=8000" in example
    assert "CADDY_" not in example


def test_api_image_installs_adb_client() -> None:
    """The runner container must have an ADB client to reach the host device."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "android-sdk-platform-tools adb ca-certificates" in dockerfile
