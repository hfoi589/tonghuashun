from __future__ import annotations

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
