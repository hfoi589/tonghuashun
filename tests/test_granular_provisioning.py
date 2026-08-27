from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess

import pytest

from tests.test_macos_one_click_deploy import (
    APK_SHA256,
    PROVISIONER,
    ROOT,
    FakeDataOnlyAcceptance,
    FakeFileSystem,
    FakeProvisioningJournal,
    _load_macos_deploy,
    existing_mac_runner,
    make_orchestrator,
)
from tests.test_macos_deployment_maintenance import FakeDeploymentMaintenance


CREATED = datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc)


def test_journal_persists_granular_steps_and_creation_time(tmp_path: Path) -> None:
    """Each interrupted provisioning phase must resume from one exact durable state."""
    module = _load_macos_deploy()
    path = tmp_path / "journal.json"
    journal = module.ProvisioningJournal(path)

    journal.record_initial_missing(frozenset({"core_metrics"}))
    assert journal.load() == {"core_metrics": "PENDING_CREATE"}
    assert journal.created_at("core_metrics") is None
    journal.set_step("core_metrics", "AVD_CREATED", created_at=CREATED)
    journal.set_step("core_metrics", "APK_VERIFIED")
    journal.set_step("core_metrics", "FRIDA_READY")
    journal.set_step("core_metrics", "LOGIN_REQUIRED")
    journal.set_step("core_metrics", "ACCEPTANCE_PENDING")

    assert journal.load() == {"core_metrics": "ACCEPTANCE_PENDING"}
    assert journal.created_at("core_metrics") == CREATED
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document == {
        "version": 2,
        "roles": {
            "core_metrics": {
                "avd_name": "THS_CORE_33_ARM64",
                "step": "ACCEPTANCE_PENDING",
                "created_at": "2026-08-28T04:00:00+00:00",
            }
        },
    }


@pytest.mark.parametrize(
    "step",
    [
        "PENDING_CREATE",
        "AVD_CREATED",
        "APK_VERIFIED",
        "FRIDA_READY",
        "LOGIN_REQUIRED",
    ],
)
def test_journal_rejects_skipped_or_backward_transitions(
    step: str, tmp_path: Path
) -> None:
    """A crash cannot be papered over by jumping past an unverified phase."""
    module = _load_macos_deploy()
    journal = module.ProvisioningJournal(tmp_path / "journal.json")
    journal.record_initial_missing(frozenset({"core_metrics"}))
    transitions = [
        "AVD_CREATED",
        "APK_VERIFIED",
        "FRIDA_READY",
        "LOGIN_REQUIRED",
        "ACCEPTANCE_PENDING",
    ]
    for target in transitions:
        if journal.load()["core_metrics"] == step:
            break
        journal.set_step(
            "core_metrics",
            target,
            created_at=CREATED if target == "AVD_CREATED" else None,
        )
    current = journal.load()["core_metrics"]
    wrong = {
        "PENDING_CREATE": "APK_VERIFIED",
        "AVD_CREATED": "FRIDA_READY",
        "APK_VERIFIED": "LOGIN_REQUIRED",
        "FRIDA_READY": "ACCEPTANCE_PENDING",
        "LOGIN_REQUIRED": "FRIDA_READY",
    }[current]

    with pytest.raises(module.DeploymentError) as caught:
        journal.set_step("core_metrics", wrong)

    assert caught.value.error_code == "PROVISIONING_JOURNAL_INVALID"


def write_resume_adb(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "adb.log"
    adb = tmp_path / "adb"
    adb.write_text(
        f"""#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$ADB_LOG"
case "$*" in
  *" get-state") printf '%s\n' device ;;
  *" shell getprop sys.boot_completed") printf '%s\n' 1 ;;
  *" shell pm path com.hexin.plat.android") printf '%s\n' package:/data/app/safe/base.apk ;;
  *" shell sha256sum /data/app/safe/base.apk") printf '%s  %s\n' '{APK_SHA256}' /data/app/safe/base.apk ;;
  *" root") ;;
  *" push /opt/ths/assets/ths-frida-server /data/local/tmp/ths-frida-server") ;;
  *" shell chmod 0755 /data/local/tmp/ths-frida-server") ;;
  *" shell nohup /data/local/tmp/ths-frida-server"*) ;;
  *" shell pidof ths-frida-server") printf '%s\n' 4321 ;;
  *" forward tcp:27043 tcp:27042") ;;
  *) exit 9 ;;
esac
""",
        encoding="utf-8",
    )
    adb.chmod(0o755)
    return adb, log


def test_existing_exact_apk_is_verified_without_reinstall_then_frida_continues(
    tmp_path: Path,
) -> None:
    """A crash after APK install resumes bridge work without touching App data."""
    adb, log = write_resume_adb(tmp_path)
    environment = os.environ | {"PATH": f"{tmp_path}:/usr/bin:/bin", "ADB_LOG": str(log)}

    apk = subprocess.run(
        [str(PROVISIONER), "core_metrics", "apk"],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    frida = subprocess.run(
        [str(PROVISIONER), "core_metrics", "frida"],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    commands = log.read_text(encoding="utf-8")
    assert apk.returncode == 0, apk.stderr
    assert frida.returncode == 0, frida.stderr
    assert " install " not in f" {commands} "
    assert "sha256sum /data/app/safe/base.apk" in commands
    assert "push /opt/ths/assets/ths-frida-server" in commands
    assert apk.stdout.strip() == "DEVICE_APK_VERIFIED"
    assert frida.stdout.strip() == "DEVICE_FRIDA_READY"


def test_apk_digest_mismatch_fails_before_frida_or_reinstall(tmp_path: Path) -> None:
    """A pre-existing wrong APK remains untouched and blocks journal advancement."""
    adb, log = write_resume_adb(tmp_path)
    content = adb.read_text(encoding="utf-8").replace(APK_SHA256, "0" * 64)
    adb.write_text(content, encoding="utf-8")
    adb.chmod(0o755)

    completed = subprocess.run(
        [str(PROVISIONER), "core_metrics", "apk"],
        cwd=ROOT,
        env=os.environ | {"PATH": f"{tmp_path}:/usr/bin:/bin", "ADB_LOG": str(log)},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    commands = log.read_text(encoding="utf-8")
    assert completed.returncode != 0
    assert completed.stderr.strip() == "INSTALLED_APK_MISMATCH"
    assert " install " not in f" {commands} "
    assert "push /opt/ths/assets/ths-frida-server" not in commands


class TimestampRunner(type(existing_mac_runner())):
    pass


def session_runner(*, updated_at: datetime | None, **kwargs):
    runner = existing_mac_runner(**kwargs)
    runner.session_updated_at = {
        "core_metrics": updated_at,
        "main_fund_flow": updated_at,
    }
    return runner


def test_new_role_stays_journaled_through_login_and_requires_post_creation_refresh() -> None:
    """A READY session captured before AVD creation cannot clear first-time onboarding."""
    module = _load_macos_deploy()
    filesystem = FakeFileSystem()
    journal = FakeProvisioningJournal()
    first_runner = session_runner(
        updated_at=None,
        avds=("THS_API_33_ARM64",),
        filesystem=filesystem,
    )

    first = make_orchestrator(
        first_runner,
        filesystem=filesystem,
        journal=journal,
        now=lambda: CREATED,
    ).deploy("provision")

    assert first.state == "FIRST_TIME_LOGIN_REQUIRED"
    assert journal.steps == {"core_metrics": "LOGIN_REQUIRED"}
    assert journal.created["core_metrics"] == CREATED

    stale_runner = session_runner(
        updated_at=CREATED - timedelta(seconds=1),
        filesystem=filesystem,
    )
    stale = make_orchestrator(
        stale_runner,
        filesystem=filesystem,
        journal=journal,
        now=lambda: CREATED + timedelta(minutes=1),
    ).deploy("provision")

    assert stale.state == "FIRST_TIME_LOGIN_REQUIRED"
    assert journal.steps == {"core_metrics": "LOGIN_REQUIRED"}


def test_fresh_session_advances_to_acceptance_and_clears_only_after_success() -> None:
    """The journal remains recoverable until strict data acceptance is complete."""
    filesystem = FakeFileSystem()
    journal = FakeProvisioningJournal(
        {"core_metrics": "LOGIN_REQUIRED"},
        created={"core_metrics": CREATED},
    )
    acceptance = FakeDataOnlyAcceptance()
    runner = session_runner(
        updated_at=CREATED + timedelta(seconds=1),
        filesystem=filesystem,
    )

    result = make_orchestrator(
        runner,
        filesystem=filesystem,
        journal=journal,
        acceptance=acceptance,
        now=lambda: CREATED + timedelta(minutes=1),
    ).deploy("provision")

    assert result.state == "READY"
    assert acceptance.calls == 1
    assert "journal-step:core_metrics:ACCEPTANCE_PENDING" in journal.events
    assert journal.steps == {}


def test_resume_from_apk_verified_runs_only_frida_phase() -> None:
    """An interrupted run resumes the exact incomplete step without recreate or reinstall."""
    filesystem = FakeFileSystem()
    journal = FakeProvisioningJournal(
        {"core_metrics": "APK_VERIFIED"},
        created={"core_metrics": CREATED},
    )
    runner = session_runner(updated_at=None, filesystem=filesystem)

    result = make_orchestrator(
        runner,
        filesystem=filesystem,
        journal=journal,
        now=lambda: CREATED + timedelta(minutes=1),
    ).deploy("provision")

    provision_calls = [
        call for call in runner.calls if "container-provision-device" in call
    ]
    assert result.state == "FIRST_TIME_LOGIN_REQUIRED"
    assert [call[-1] for call in provision_calls] == ["frida"]
    assert all(call[-2] == "core_metrics" for call in provision_calls)
    assert not any(call[:3] == ("avdmanager", "create", "avd") for call in runner.calls)


def test_acceptance_pending_rerun_holds_lease_across_api_replacement() -> None:
    """Provisioning uses the same owner-bound acceptance exception on its final rerun."""
    events: list[str] = []
    filesystem = FakeFileSystem()
    journal = FakeProvisioningJournal(
        {"core_metrics": "ACCEPTANCE_PENDING"},
        created={"core_metrics": CREATED},
        events=events,
    )
    maintenance = FakeDeploymentMaintenance(events)
    acceptance = FakeDataOnlyAcceptance()
    runner = session_runner(
        updated_at=CREATED + timedelta(minutes=1),
        filesystem=filesystem,
        events=events,
    )

    result = make_orchestrator(
        runner,
        filesystem=filesystem,
        journal=journal,
        acceptance=acceptance,
        deployment_maintenance=maintenance,
        now=lambda: CREATED + timedelta(minutes=2),
    ).deploy("provision")

    assert result.state == "READY"
    assert events.index("maintenance-prepare") < events.index("maintenance-renew")
    assert events[-1] == "journal-complete:core_metrics"
    assert "maintenance-release" in events
