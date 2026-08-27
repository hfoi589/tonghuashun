from __future__ import annotations

import copy
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import subprocess

from argon2 import PasswordHasher
import pytest

from tests.test_macos_one_click_deploy import (
    ROOT,
    REQUIRED_MACOS_ENV,
    REQUIRED_ROOT_ENV,
    VALID_COMPOSE_ENVIRONMENT,
    FakeCommandRunner,
    FakeFileSystem,
    FakeProvisioningJournal,
    _completed,
    _load_macos_deploy,
    existing_mac_runner,
    make_orchestrator,
)


def rendered_compose_config() -> dict[str, object]:
    return {
        "name": "ths-level2",
        "services": {
            "api": {
                "environment": {
                    **VALID_COMPOSE_ENVIRONMENT,
                    "THS_SESSION_ROOT": "/data/admin/ths-sessions",
                },
                "ports": [
                    {
                        "mode": "ingress",
                        "target": 8000,
                        "published": "8001",
                        "protocol": "tcp",
                    }
                ],
                "volumes": [
                    {
                        "type": "volume",
                        "source": "capture-data",
                        "target": "/data/captures",
                    },
                    {
                        "type": "volume",
                        "source": "template-data",
                        "target": "/data/templates",
                    },
                    {
                        "type": "volume",
                        "source": "admin-data",
                        "target": "/data/admin",
                    },
                    {
                        "type": "volume",
                        "source": "market-data",
                        "target": "/data/market",
                    },
                ],
            },
            "redis": {
                "volumes": [
                    {
                        "type": "volume",
                        "source": "redis-data",
                        "target": "/data",
                    }
                ]
            },
        },
        "volumes": {
            name: {"name": f"ths-level2_{name}"}
            for name in (
                "capture-data",
                "template-data",
                "admin-data",
                "market-data",
                "redis-data",
            )
        },
    }


class RenderedConfigRunner(FakeCommandRunner):
    def __init__(self, document: dict[str, object]) -> None:
        super().__init__()
        self.document = document

    def run(
        self,
        args: tuple[str, ...],
        timeout: float,
        input_data: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        if "config" in args and "--format" in args:
            self.calls.append(args)
            self.inputs.append(input_data)
            return _completed(args, stdout=json.dumps(self.document).encode())
        return super().run(args, timeout, input_data)


def test_every_compose_command_binds_the_fixed_project_name() -> None:
    """Ambient project selection must not rename volumes or target another stack."""
    runner = existing_mac_runner(sessions_ready=True)

    make_orchestrator(runner).deploy_existing()

    compose_calls = [call for call in runner.calls if "compose" in call]
    assert compose_calls
    assert all(
        call[call.index("--project-name") + 1] == "ths-level2"
        for call in compose_calls
    )


def test_subprocess_runner_strips_every_ambient_compose_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Profiles, files, paths, and project names cannot come from the parent shell."""
    module = _load_macos_deploy()
    probe = tmp_path / "probe-environment"
    probe.write_text("#!/bin/sh\nexec /usr/bin/env\n", encoding="utf-8")
    probe.chmod(0o755)
    hostile = {
        "COMPOSE_PROJECT_NAME": "attacker",
        "COMPOSE_FILE": "/tmp/attacker.yml",
        "COMPOSE_PROFILES": "linux-redroid",
        "COMPOSE_PATH_SEPARATOR": ";",
        "COMPOSE_PARALLEL_LIMIT": "99",
        "COMPOSE_IGNORE_ORPHANS": "1",
    }
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)

    result = module.SubprocessCommandRunner(tmp_path).run((str(probe),), 1.0)

    output = result.stdout.decode("utf-8").splitlines()
    assert result.returncode == 0
    for key in hostile:
        assert not any(line.startswith(f"{key}=") for line in output)


@pytest.mark.parametrize("env_name", ["root", "macos"])
@pytest.mark.parametrize(
    "control",
    [
        "COMPOSE_PROJECT_NAME=attacker",
        "COMPOSE_FILE=/tmp/attacker.yml",
        "COMPOSE_PROFILES=linux-redroid",
        "COMPOSE_PARALLEL_LIMIT=99",
    ],
)
def test_env_files_reject_compose_control_variables(
    env_name: str, control: str
) -> None:
    """A reviewed env file may provide service values, never Compose control plane input."""
    module = _load_macos_deploy()
    filesystem = FakeFileSystem()
    target = ROOT / (".env" if env_name == "root" else "deploy/macos.env")
    original = REQUIRED_ROOT_ENV if env_name == "root" else REQUIRED_MACOS_ENV
    filesystem.files[target.resolve()] = (original + control + "\n", 0o600)
    runner = existing_mac_runner(filesystem=filesystem)

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(runner, filesystem=filesystem).deploy_existing()

    assert caught.value.error_code == (
        "ROOT_ENV_INVALID" if env_name == "root" else "MACOS_ENV_INVALID"
    )
    assert not any(call[-2:] == ("build", "api") for call in runner.calls)


def mutate_project_name(document: dict[str, object]) -> None:
    document["name"] = "attacker"


def mutate_extra_service(document: dict[str, object]) -> None:
    document["services"]["android"] = {"profiles": ["linux-redroid"]}  # type: ignore[index]


def mutate_port(document: dict[str, object]) -> None:
    document["services"]["api"]["ports"][0]["published"] = "9000"  # type: ignore[index]


def mutate_admin_volume(document: dict[str, object]) -> None:
    document["volumes"]["admin-data"]["name"] = "attacker_admin-data"  # type: ignore[index]


def mutate_session_mount(document: dict[str, object]) -> None:
    document["services"]["api"]["environment"]["THS_SESSION_ROOT"] = "/tmp/sessions"  # type: ignore[index]


def mutate_redis_mount(document: dict[str, object]) -> None:
    document["services"]["redis"]["volumes"][0]["target"] = "/tmp"  # type: ignore[index]


def mutate_secret(document: dict[str, object]) -> None:
    document["services"]["api"]["environment"]["THS_DEVICE_LIFECYCLE_TOKEN"] = ""  # type: ignore[index]


@pytest.mark.parametrize(
    "mutate",
    [
        mutate_project_name,
        mutate_extra_service,
        mutate_port,
        mutate_admin_volume,
        mutate_session_mount,
        mutate_redis_mount,
        mutate_secret,
    ],
    ids=(
        "project-name",
        "service-set",
        "api-port",
        "project-volume",
        "session-storage",
        "redis-storage",
        "secret",
    ),
)
def test_rendered_compose_config_rejects_cross_project_or_service_drift(
    mutate,
) -> None:
    """Validation must cover the effective stack before any build or replacement."""
    module = _load_macos_deploy()
    document = rendered_compose_config()
    mutate(document)
    runner = RenderedConfigRunner(document)
    orchestrator = make_orchestrator(runner)
    orchestrator._root_environment = {
        "THS_DEVICE_LIFECYCLE_TOKEN": "lifecycle-secret-value",
        "THS_SESSION_ENCRYPTION_KEY": VALID_COMPOSE_ENVIRONMENT[
            "THS_SESSION_ENCRYPTION_KEY"
        ],
    }

    with pytest.raises(module.DeploymentError) as caught:
        orchestrator._validate_effective_compose_config()

    assert caught.value.error_code == "COMPOSE_CONFIG_INVALID"


def test_rendered_compose_config_accepts_only_the_fixed_macos_stack() -> None:
    """The exact API/Redis project and persistent storage map remains deployable."""
    runner = RenderedConfigRunner(rendered_compose_config())
    orchestrator = make_orchestrator(runner)
    orchestrator._root_environment = {
        "THS_DEVICE_LIFECYCLE_TOKEN": "lifecycle-secret-value",
        "THS_SESSION_ENCRYPTION_KEY": VALID_COMPOSE_ENVIRONMENT[
            "THS_SESSION_ENCRYPTION_KEY"
        ],
    }

    orchestrator._validate_effective_compose_config()


def load_setup_admin():
    path = ROOT / "scripts/setup-admin.py"
    spec = spec_from_file_location("predeploy_setup_admin", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def existing_env_text() -> str:
    return (
        "# operator comment\n"
        f"ADMIN_PASSWORD_HASH='{PasswordHasher().hash('old-password')}'\n"
        "UNRELATED_SETTING=preserve-me\n"
        "ADMIN_SESSION_SECRET=existing-session-secret-value-that-must-survive\n"
        "\n"
    )


def test_setup_admin_atomically_upgrades_only_missing_deployment_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An existing deployment keeps its admin identity and unrelated configuration byte-for-byte."""
    setup_admin = load_setup_admin()
    env_file = tmp_path / ".env"
    original = existing_env_text()
    env_file.write_text(original, encoding="utf-8")
    env_file.chmod(0o600)

    assert setup_admin.main(["--upgrade-existing", str(env_file)]) == 0

    upgraded = env_file.read_text(encoding="utf-8")
    assert upgraded.startswith(original)
    assert upgraded.count("ADMIN_PASSWORD_HASH=") == 1
    assert upgraded.count("ADMIN_SESSION_SECRET=") == 1
    assert upgraded.count("THS_SESSION_ENCRYPTION_KEY=") == 1
    assert upgraded.count("THS_DEVICE_LIFECYCLE_TOKEN=") == 1
    assert env_file.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr()
    for secret_fragment in (
        "existing-session-secret",
        "argon2id",
        "THS_SESSION_ENCRYPTION_KEY=",
        "THS_DEVICE_LIFECYCLE_TOKEN=",
    ):
        assert secret_fragment not in output.out + output.err
    assert list(tmp_path.glob("..env.*")) == []


def test_setup_admin_generates_only_the_one_missing_secret(tmp_path: Path) -> None:
    """A partial upgrade must not rotate a newer secret that is already valid."""
    setup_admin = load_setup_admin()
    lifecycle_token = "existing-lifecycle-token-value-123456"
    env_file = tmp_path / ".env"
    env_file.write_text(
        existing_env_text()
        + f"THS_DEVICE_LIFECYCLE_TOKEN={lifecycle_token}\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    assert setup_admin.main(["--upgrade-existing", str(env_file)]) == 0

    upgraded = env_file.read_text(encoding="utf-8")
    assert upgraded.count(f"THS_DEVICE_LIFECYCLE_TOKEN={lifecycle_token}") == 1
    assert upgraded.count("THS_SESSION_ENCRYPTION_KEY=") == 1


@pytest.mark.parametrize(
    "invalid",
    ["duplicate", "malformed-hash", "empty-session", "wrong-mode", "symlink", "wrong-owner"],
)
def test_setup_admin_rejects_untrusted_existing_env(
    invalid: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unsafe metadata or ambiguous values must fail without replacing the original file."""
    setup_admin = load_setup_admin()
    env_file = tmp_path / ".env"
    original = existing_env_text()
    if invalid == "duplicate":
        original += "ADMIN_SESSION_SECRET=duplicate\n"
    elif invalid == "malformed-hash":
        original = original.replace("'$argon2id$", "'not-an-argon-hash")
    elif invalid == "empty-session":
        original = original.replace(
            "ADMIN_SESSION_SECRET=existing-session-secret-value-that-must-survive",
            "ADMIN_SESSION_SECRET=",
        )
    target = env_file
    if invalid == "symlink":
        real = tmp_path / "real.env"
        real.write_text(original, encoding="utf-8")
        real.chmod(0o600)
        env_file.symlink_to(real)
    else:
        env_file.write_text(original, encoding="utf-8")
        env_file.chmod(0o644 if invalid == "wrong-mode" else 0o600)
    if invalid == "wrong-owner":
        monkeypatch.setattr(setup_admin.os, "getuid", lambda: os.getuid() + 1)

    before = target.read_bytes()
    assert setup_admin.main(["--upgrade-existing", str(env_file)]) == 2

    assert target.read_bytes() == before


def test_setup_admin_keeps_original_when_atomic_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed replacement may not truncate or partially upgrade the deployment env."""
    setup_admin = load_setup_admin()
    env_file = tmp_path / ".env"
    original = existing_env_text()
    env_file.write_text(original, encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setattr(
        setup_admin.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("private path")),
    )

    assert setup_admin.main(["--upgrade-existing", str(env_file)]) == 2

    assert env_file.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("..env.*")) == []


def test_orchestrator_upgrades_an_existing_pre_feature_env_without_password_prompt() -> None:
    """Only a missing file may enter interactive administrator password setup."""
    old_env = (
        "ADMIN_PASSWORD_HASH='$argon2id$example'\n"
        "ADMIN_SESSION_SECRET=session-secret-value\n"
        "UNRELATED=preserved\n"
    )
    filesystem = FakeFileSystem()
    filesystem.files[(ROOT / ".env").resolve()] = (old_env, 0o600)
    runner = existing_mac_runner(filesystem=filesystem, sessions_ready=True)

    make_orchestrator(runner, filesystem=filesystem).deploy_existing()

    upgrade_calls = [
        call
        for call in runner.calls
        if call[0].endswith("setup-admin.sh")
    ]
    assert len(upgrade_calls) == 1
    assert "--upgrade-existing" in upgrade_calls[0]
    assert filesystem.read_text(ROOT / ".env").startswith(old_env)


class DiskFileSystem(FakeFileSystem):
    def __init__(self, free_by_path: dict[Path, int]) -> None:
        super().__init__()
        self.free_by_path = {path.resolve(): value for path, value in free_by_path.items()}
        self.checked: list[Path] = []

    def free_bytes(self, path: Path) -> int:
        resolved = path.resolve()
        self.checked.append(resolved)
        return self.free_by_path.get(resolved, 30 * 1024**3)


@pytest.mark.parametrize("short_path", ["project", "avd"])
def test_disk_preflight_blocks_before_build_or_provisioning_journal_mutation(
    short_path: str,
) -> None:
    """Neither Docker assets nor AVD recovery state may be mutated below the documented floor."""
    module = _load_macos_deploy()
    avd_root = (Path.home() / ".android/avd").resolve()
    free = {
        ROOT.resolve(): 30 * 1024**3,
        avd_root: 30 * 1024**3,
    }
    free[ROOT.resolve() if short_path == "project" else avd_root] -= 1
    filesystem = DiskFileSystem(free)
    events: list[str] = []
    journal = FakeProvisioningJournal(events=events)
    runner = existing_mac_runner(
        avds=("THS_API_33_ARM64",),
        filesystem=filesystem,
        events=events,
    )

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(
            runner,
            filesystem=filesystem,
            journal=journal,
        ).deploy("provision")

    assert caught.value.error_code == "INSUFFICIENT_DISK_SPACE"
    assert not any(event.startswith("journal-") for event in events)
    assert not any(call[-2:] == ("build", "api") for call in runner.calls)
    assert not any(call[:3] == ("avdmanager", "create", "avd") for call in runner.calls)


def test_disk_preflight_accepts_the_exact_documented_boundary() -> None:
    """Exactly 30 GiB on both relevant filesystems is sufficient."""
    boundary = 30 * 1024**3
    filesystem = DiskFileSystem(
        {
            ROOT.resolve(): boundary,
            (Path.home() / ".android/avd").resolve(): boundary,
        }
    )
    runner = existing_mac_runner(filesystem=filesystem, sessions_ready=True)

    result = make_orchestrator(runner, filesystem=filesystem).deploy_existing()

    assert result.state == "READY"
    assert filesystem.checked == [
        ROOT.resolve(),
        (Path.home() / ".android/avd").resolve(),
    ]
