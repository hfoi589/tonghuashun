from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from tests.test_macos_device_lifecycle import (
    FakeCommandRunner,
    lifecycle_responses,
    make_manager,
    module,
    result,
    wait_for_terminal,
)


CORE_SERIAL = "emulator-5556"
CORE_AVD = "THS_CORE_33_ARM64"


class FakeProcessResolver:
    def __init__(self, path: Path = Path("/fake/emulator")) -> None:
        self.path = path
        self.calls: list[int] = []

    def resolve(self, pid: int) -> Path:
        self.calls.append(pid)
        return self.path


def identity_manager(runner: FakeCommandRunner, resolver=None, **kwargs):
    return make_manager(
        runner,
        trusted_emulator_path=Path("/fake/emulator"),
        process_executable_resolver=resolver or FakeProcessResolver(),
        **kwargs,
    )


def add_attached_identity(
    responses: dict[tuple[str, ...], subprocess.CompletedProcess[bytes]],
    *,
    avd_name: str = CORE_AVD,
) -> None:
    command = ("adb", "-s", CORE_SERIAL, "emu", "avd", "name")
    responses[command] = result(command, stdout=f"{avd_name}\nOK\n".encode())


def test_attached_serial_must_report_the_exact_fixed_avd_before_start_action() -> None:
    """A different AVD on emulator-5556 must never receive bridge or App commands."""
    responses = lifecycle_responses(CORE_SERIAL)
    add_attached_identity(responses, avd_name="ATTACKER_AVD")
    runner = FakeCommandRunner(responses)
    manager = identity_manager(runner)

    operation = wait_for_terminal(
        manager,
        manager.submit("core_metrics", "start_and_launch_app").operation_id,
    )

    assert operation.state is module.LifecycleState.ERROR
    assert operation.error_code == "DEVICE_LIFECYCLE_FAILED"
    rendered = "\n".join(" ".join(call) for call in runner.calls)
    assert "watch-macos-device-bridge.sh" not in rendered
    assert "am start" not in rendered


def test_shutdown_rechecks_exact_avd_identity_before_emulator_kill() -> None:
    """Normal shutdown is authorized for the fixed role, not merely the serial string."""
    responses = lifecycle_responses(CORE_SERIAL)
    add_attached_identity(responses, avd_name="ATTACKER_AVD")
    runner = FakeCommandRunner(responses)
    manager = identity_manager(runner)

    operation = wait_for_terminal(
        manager, manager.submit("core_metrics", "shutdown").operation_id
    )

    assert operation.state is module.LifecycleState.ERROR
    assert ("adb", "-s", CORE_SERIAL, "emu", "kill") not in runner.calls


@pytest.mark.parametrize(
    ("adb_devices", "process", "resolver_path"),
    [
        (None, "", Path("/fake/emulator")),
        ("List of devices attached\nemulator-5556\toffline\n", "", Path("/fake/emulator")),
        (
            "List of devices attached\n",
            "123 /fake/emulator -avd ATTACKER_AVD -port 5556\n",
            Path("/fake/emulator"),
        ),
        (
            "List of devices attached\n",
            "123 /fake/emulator -avd THS_CORE_33_ARM64 -port 5556\n",
            Path("/tmp/spoofed-emulator"),
        ),
    ],
    ids=("no-absence-proof", "listed-offline", "wrong-avd", "spoofed-executable"),
)
def test_absent_serial_requires_adb_absence_and_trusted_fixed_port_process(
    adb_devices: str | None,
    process: str,
    resolver_path: Path,
) -> None:
    """STOPPED/STARTING classification fails closed without every identity proof."""
    responses = lifecycle_responses(CORE_SERIAL, booted=False, process=False)
    get_state = ("adb", "-s", CORE_SERIAL, "get-state")
    responses[get_state] = result(get_state, code=1, stderr=b"absent")
    devices = ("adb", "devices")
    if adb_devices is not None:
        responses[devices] = result(devices, stdout=adb_devices.encode())
    else:
        responses[devices] = result(devices, code=1, stderr=b"private detail")
    ps = ("ps", "-axo", "pid=,command=")
    responses[ps] = result(ps, stdout=process.encode())
    if adb_devices and "emulator-5556\toffline" in adb_devices:
        identity = ("adb", "-s", CORE_SERIAL, "emu", "avd", "name")
        responses[identity] = result(identity, code=1, stderr=b"offline")
    runner = FakeCommandRunner(responses)
    manager = identity_manager(
        runner, FakeProcessResolver(resolver_path)
    )

    devices_payload = {
        item["role"]: item for item in manager.devices()
    }

    assert devices_payload["core_metrics"]["state"] == "UNKNOWN"


def test_valid_absent_serial_and_exact_starting_process_reports_starting() -> None:
    """The fixed process is accepted only after proc_pidpath and exact argv validation."""
    responses = lifecycle_responses(CORE_SERIAL, booted=False, process=False)
    get_state = ("adb", "-s", CORE_SERIAL, "get-state")
    responses[get_state] = result(get_state, code=1)
    adb_devices = ("adb", "devices")
    responses[adb_devices] = result(
        adb_devices, stdout=b"List of devices attached\n"
    )
    ps = ("ps", "-axo", "pid=,command=")
    responses[ps] = result(
        ps,
        stdout=(
            b"123 /untrusted/argv0 -avd THS_CORE_33_ARM64 -port 5556 "
            b"-no-snapshot\n"
        ),
    )
    resolver = FakeProcessResolver()
    manager = identity_manager(FakeCommandRunner(responses), resolver)

    payload = {item["role"]: item for item in manager.devices()}

    assert payload["core_metrics"]["state"] == "STARTING"
    assert resolver.calls == [123]


def test_terminal_error_remains_visible_until_a_later_successful_action() -> None:
    """Normal state detection must not erase the latest asynchronous failure alert."""
    responses = lifecycle_responses(CORE_SERIAL, app_running=False)
    add_attached_identity(responses)
    runner = FakeCommandRunner(responses)
    manager = identity_manager(runner)
    failed = wait_for_terminal(
        manager,
        manager.submit("core_metrics", "start_and_launch_app").operation_id,
    )
    assert failed.state is module.LifecycleState.ERROR

    first = {item["role"]: item for item in manager.devices()}["core_metrics"]
    second = {item["role"]: item for item in manager.devices()}["core_metrics"]

    assert first == second
    assert first["state"] == "ERROR"
    assert first["operation_id"] == failed.operation_id
    assert first["error_code"] == "DEVICE_APP_LAUNCH_FAILED"
    assert isinstance(first["updated_at"], str)

    pidof = ("adb", "-s", CORE_SERIAL, "shell", "pidof", "com.hexin.plat.android")
    runner.responses[pidof] = result(pidof, stdout=b"234\n")
    succeeded = wait_for_terminal(
        manager,
        manager.submit("core_metrics", "start_and_launch_app").operation_id,
    )
    latest = {item["role"]: item for item in manager.devices()}["core_metrics"]

    assert succeeded.state is module.LifecycleState.RUNNING
    assert latest["state"] == "RUNNING"
    assert latest["operation_id"] == succeeded.operation_id
    assert latest["error_code"] is None


def test_broker_and_deployer_share_the_same_identity_verifier() -> None:
    """The long-lived broker may not drift to a weaker duplicate parser."""
    deploy = __import__("tests.test_macos_one_click_deploy", fromlist=["_load_macos_deploy"])
    deploy_module = deploy._load_macos_deploy()

    assert module.FixedAvdIdentityVerifier is deploy_module.FixedAvdIdentityVerifier
