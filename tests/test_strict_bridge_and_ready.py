from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from tests.test_macos_deployment_maintenance import FakeDeploymentMaintenance
from tests.test_macos_one_click_deploy import (
    ROOT,
    FakeDataOnlyAcceptance,
    FakeLifecycleBroker,
    _load_macos_deploy,
    existing_mac_runner,
    make_orchestrator,
)


@pytest.mark.parametrize(
    "stage",
    ["boot", "root", "root-wait", "frida-start", "forward"],
)
def test_bridge_once_fails_closed_at_every_required_stage(
    stage: str, tmp_path: Path
) -> None:
    """The lifecycle broker must see a nonzero result for every incomplete repair."""
    fake_adb = tmp_path / "adb"
    state = tmp_path / "state"
    fake_adb.write_text(
        """#!/bin/sh
set -u
printf '%s\n' 'private-cookie command detail' >&2
case "$*" in
  *" get-state")
    if [ "$FAIL_STAGE" = boot ]; then exit 9; fi
    if [ "$FAIL_STAGE" = root-wait ] && [ -f "$ADB_STATE_FILE" ]; then exit 9; fi
    printf '%s\n' device
    ;;
  *" shell getprop sys.boot_completed")
    if [ "$FAIL_STAGE" = boot ]; then printf '%s\n' 0; else printf '%s\n' 1; fi
    ;;
  *" shell id")
    if [ -f "$ADB_STATE_FILE" ]; then printf '%s\n' 'uid=0(root)'; else printf '%s\n' 'uid=2000(shell)'; fi
    ;;
  *" root")
    [ "$FAIL_STAGE" != root ] || exit 9
    : > "$ADB_STATE_FILE"
    ;;
  *" shell pidof ths-frida-server")
    [ "$FAIL_STAGE" = frida-start ] && exit 1
    printf '%s\n' 4321
    ;;
  *" shell nohup /data/local/tmp/ths-frida-server"*)
    [ "$FAIL_STAGE" != frida-start ] || exit 9
    ;;
  *" forward tcp:27043 tcp:27042")
    [ "$FAIL_STAGE" != forward ] || exit 9
    ;;
  *) exit 9 ;;
esac
""",
        encoding="utf-8",
    )
    fake_adb.chmod(0o755)
    fake_sleep = tmp_path / "sleep"
    fake_sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_sleep.chmod(0o755)
    watcher = ROOT / "scripts/watch-macos-device-bridge.sh"

    completed = subprocess.run(
        [str(watcher), "--once", "emulator-5556", "27043", str(fake_adb)],
        cwd=ROOT,
        env=os.environ
        | {
            "FAIL_STAGE": stage,
            "ADB_STATE_FILE": str(state),
            "PATH": f"{tmp_path}:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode != 0
    assert "private-cookie" not in completed.stdout + completed.stderr
    assert "command detail" not in completed.stdout + completed.stderr


def test_existing_mode_repairs_running_and_stopped_roles_before_ready() -> None:
    """A broker RUNNING label alone is not bridge/App readiness proof."""
    events: list[str] = []
    broker = FakeLifecycleBroker(
        {"core_metrics": "RUNNING", "main_fund_flow": "STOPPED"},
        events=events,
    )
    maintenance = FakeDeploymentMaintenance(events)
    acceptance = FakeDataOnlyAcceptance()
    runner = existing_mac_runner(
        sessions_ready=True,
        events=events,
    )

    result = make_orchestrator(
        runner,
        broker=broker,
        acceptance=acceptance,
        deployment_maintenance=maintenance,
    ).deploy_existing()

    assert result.state == "READY"
    assert broker.start_calls == ["core_metrics", "main_fund_flow"]
    assert acceptance.calls == 1


def test_existing_mode_never_returns_ready_from_api_redis_health_alone() -> None:
    """Both encrypted sessions are required before strict acceptance and lease release."""
    module = _load_macos_deploy()
    events: list[str] = []
    maintenance = FakeDeploymentMaintenance(events)
    acceptance = FakeDataOnlyAcceptance()
    runner = existing_mac_runner(
        healthy=True,
        sessions_ready=False,
        events=events,
    )

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(
            runner,
            acceptance=acceptance,
            deployment_maintenance=maintenance,
        ).deploy_existing()

    assert caught.value.error_code == "SESSION_NOT_READY"
    assert acceptance.calls == 0
    assert "maintenance-release" not in events


def test_running_but_unhealthy_broker_action_blocks_ready_and_retains_lease() -> None:
    """A failed bridge/App repair on an already running role is terminal for deployment."""
    module = _load_macos_deploy()
    events: list[str] = []

    class UnhealthyBroker(FakeLifecycleBroker):
        def wait_for_state(self, operation_id, expected_state, timeout_seconds):
            super().wait_for_state(operation_id, expected_state, timeout_seconds)
            raise module.DeploymentError("DEVICE_LIFECYCLE_FAILED")

    maintenance = FakeDeploymentMaintenance(events)
    broker = UnhealthyBroker(events=events)

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(
            existing_mac_runner(sessions_ready=True, events=events),
            broker=broker,
            deployment_maintenance=maintenance,
        ).deploy_existing()

    assert caught.value.error_code == "DEVICE_LIFECYCLE_FAILED"
    assert "maintenance-release" not in events
