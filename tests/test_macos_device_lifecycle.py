from __future__ import annotations

from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import subprocess
import threading
import time

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "macos-device-lifecycle.py"
spec = spec_from_file_location("macos_device_lifecycle", SCRIPT)
module = module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


class FakeCommandRunner:
    """A deterministic host-command boundary that never starts real processes."""

    def __init__(self, responses: dict[tuple[str, ...], subprocess.CompletedProcess[bytes]] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.responses = responses or {}
        self.sequences: dict[tuple[str, ...], list[subprocess.CompletedProcess[bytes]]] = {}
        self.block_on: tuple[str, ...] | None = None
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, args: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(args)
        if args == self.block_on:
            self.started.set()
            assert self.release.wait(1)
        if queued := self.sequences.get(args):
            return queued.pop(0)
        return self.responses.get(args, subprocess.CompletedProcess(args, 0, b"", b""))


def result(args: tuple[str, ...], code: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args, code, stdout, stderr)


def lifecycle_responses(
    serial: str,
    *,
    booted: bool = True,
    process: bool = True,
    app_running: bool = True,
) -> dict[tuple[str, ...], subprocess.CompletedProcess[bytes]]:
    state = ("adb", "-s", serial, "get-state")
    boot = ("adb", "-s", serial, "shell", "getprop", "sys.boot_completed")
    ps = ("ps", "-axo", "pid=,command=")
    pidof = ("adb", "-s", serial, "shell", "pidof", "com.hexin.plat.android")
    return {
        state: result(state, 0, b"device\n" if booted else b"offline\n"),
        boot: result(boot, 0, b"1\n" if booted else b"0\n"),
        ps: result(ps, 0, f"123 emulator -port {serial.rsplit('-', 1)[1]}\n".encode() if process else b""),
        pidof: result(pidof, 0 if app_running else 1, b"234\n" if app_running else b""),
    }


def running_device_runner(serial: str) -> FakeCommandRunner:
    return FakeCommandRunner(lifecycle_responses(serial))


def make_manager(runner: FakeCommandRunner, **kwargs: object):
    return module.DeviceLifecycleManager(
        runner,
        emulator_bin="emulator",
        boot_timeout_seconds=0.1,
        poll_interval_seconds=0.001,
        **kwargs,
    )


def wait_for_terminal(manager, operation_id: str):
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        operation = manager.operation(operation_id)
        assert operation is not None
        if operation.state not in {module.LifecycleState.STARTING, module.LifecycleState.STOPPING}:
            return operation
        time.sleep(0.001)
    pytest.fail("operation did not reach a terminal state")


def public_device(item: dict[str, object]) -> None:
    forbidden = {"serial", "avd", "port", "command", "stdout", "stderr"}
    assert not forbidden.intersection(item)


def test_manager_accepts_only_fixed_roles_and_actions() -> None:
    manager = make_manager(FakeCommandRunner())
    assert {item["role"] for item in manager.devices()} == {
        "core_metrics", "main_fund_flow"
    }
    with pytest.raises(module.LifecycleRequestError) as unknown_role:
        manager.submit("emulator-5556", "shutdown")
    assert unknown_role.value.error_code == "DEVICE_ROLE_NOT_FOUND"
    with pytest.raises(module.LifecycleRequestError) as unknown_action:
        manager.submit("core_metrics", "shell")
    assert unknown_action.value.error_code == "DEVICE_ACTION_INVALID"


@pytest.mark.parametrize(
    ("responses", "expected"),
    [
        (lifecycle_responses("emulator-5556"), "RUNNING"),
        (lifecycle_responses("emulator-5556", booted=False, process=True), "STARTING"),
        (lifecycle_responses("emulator-5556", booted=False, process=False), "STOPPED"),
    ],
)
def test_devices_maps_safe_host_state_without_sensitive_config(
    responses: dict[tuple[str, ...], subprocess.CompletedProcess[bytes]], expected: str
) -> None:
    manager = make_manager(FakeCommandRunner(responses))
    devices = {item["role"]: item for item in manager.devices()}
    assert devices["core_metrics"]["state"] == expected
    for item in devices.values():
        public_device(item)


def test_shutdown_uses_emulator_kill_and_never_force_stops_app() -> None:
    runner = running_device_runner("emulator-5554")
    state = ("adb", "-s", "emulator-5554", "get-state")
    ps = ("ps", "-axo", "pid=,command=")
    runner.sequences[state] = [result(state, 0, b"device\n"), result(state, 1)]
    runner.sequences[ps] = [result(ps, 0, b"")]
    manager = make_manager(runner)
    operation = manager.submit("main_fund_flow", "shutdown")
    assert wait_for_terminal(manager, operation.operation_id).state is module.LifecycleState.STOPPED
    assert ("adb", "-s", "emulator-5554", "emu", "kill") in runner.calls
    rendered = "\n".join(" ".join(call) for call in runner.calls)
    for forbidden in ("force-stop", "pm clear", "uninstall", "wipe-data"):
        assert forbidden not in rendered


def test_shutdown_of_stopped_device_does_not_send_adb_kill() -> None:
    runner = FakeCommandRunner(lifecycle_responses("emulator-5554", booted=False, process=False))
    manager = make_manager(runner)
    operation = wait_for_terminal(manager, manager.submit("main_fund_flow", "shutdown").operation_id)
    assert operation.state is module.LifecycleState.STOPPED
    assert ("adb", "-s", "emulator-5554", "emu", "kill") not in runner.calls


def test_running_start_skips_emulator_launch_and_opens_fixed_activity() -> None:
    runner = running_device_runner("emulator-5556")
    manager = make_manager(runner)
    operation = wait_for_terminal(manager, manager.submit("core_metrics", "start_and_launch_app").operation_id)
    assert operation.state is module.LifecycleState.RUNNING
    assert not any(call[:2] == ("launchctl", "submit") for call in runner.calls)
    assert (
        "adb", "-s", "emulator-5556", "shell", "am", "start", "-n",
        "com.hexin.plat.android/com.hexin.plat.android.LogoEmptyActivity",
    ) in runner.calls


def test_stopped_start_uses_fixed_commands_and_reaches_running() -> None:
    serial = "emulator-5556"
    runner = FakeCommandRunner(lifecycle_responses(serial, booted=False, process=False))
    state_call = ("adb", "-s", serial, "get-state")
    boot_call = ("adb", "-s", serial, "shell", "getprop", "sys.boot_completed")
    runner.sequences[state_call] = [result(state_call, 0, b"offline\n"), result(state_call, 0, b"device\n")]
    runner.responses[boot_call] = result(boot_call, 0, b"1\n")
    list_avds = ("emulator", "-list-avds")
    runner.responses[list_avds] = result(list_avds, 0, b"THS_CORE_33_ARM64\n")
    manager = make_manager(runner)
    operation = wait_for_terminal(manager, manager.submit("core_metrics", "start_and_launch_app").operation_id)
    assert operation.state is module.LifecycleState.RUNNING
    assert (
        "launchctl", "submit", "-l", "com.ths.avd.5556", "--", "emulator",
        "-avd", "THS_CORE_33_ARM64", "-port", "5556", "-no-snapshot", "-no-audio",
        "-gpu", "host", "-memory", "2048", "-cores", "4",
    ) in runner.calls
    assert (str(SCRIPT.parent / "configure-macos-core-display.sh"), serial, "adb") in runner.calls
    assert (
        str(SCRIPT.parent / "watch-macos-device-bridge.sh"), "--once", serial, "27043", "adb"
    ) in runner.calls


def test_concurrent_action_for_same_role_is_rejected() -> None:
    runner = running_device_runner("emulator-5556")
    runner.block_on = ("adb", "-s", "emulator-5556", "shell", "am", "start", "-n", "com.hexin.plat.android/com.hexin.plat.android.LogoEmptyActivity")
    manager = make_manager(runner)
    operation = manager.submit("core_metrics", "start_and_launch_app")
    assert runner.started.wait(1)
    with pytest.raises(module.LifecycleRequestError) as in_progress:
        manager.submit("core_metrics", "shutdown")
    assert in_progress.value.error_code == "DEVICE_ACTION_IN_PROGRESS"
    runner.release.set()
    wait_for_terminal(manager, operation.operation_id)


def test_operations_for_different_roles_do_not_share_a_lock() -> None:
    core_runner = running_device_runner("emulator-5556")
    core_runner.block_on = ("adb", "-s", "emulator-5556", "shell", "am", "start", "-n", "com.hexin.plat.android/com.hexin.plat.android.LogoEmptyActivity")
    manager = make_manager(core_runner)
    core_operation = manager.submit("core_metrics", "start_and_launch_app")
    assert core_runner.started.wait(1)
    fund_operation = manager.submit("main_fund_flow", "shutdown")
    assert fund_operation.role == "main_fund_flow"
    assert fund_operation.action is module.LifecycleAction.SHUTDOWN
    core_runner.release.set()
    wait_for_terminal(manager, core_operation.operation_id)
    wait_for_terminal(manager, fund_operation.operation_id)


def test_boot_timeout_is_sanitized_to_fixed_error_code() -> None:
    serial = "emulator-5556"
    runner = FakeCommandRunner(lifecycle_responses(serial, booted=False, process=False))
    list_avds = ("emulator", "-list-avds")
    runner.responses[list_avds] = result(list_avds, 0, b"THS_CORE_33_ARM64\n")
    manager = make_manager(runner)
    operation = wait_for_terminal(manager, manager.submit("core_metrics", "start_and_launch_app").operation_id)
    assert operation.state is module.LifecycleState.ERROR
    assert operation.error_code == "DEVICE_BOOT_TIMEOUT"
    assert "stdout" not in operation.public_dict()
    assert "stderr" not in operation.public_dict()
