from __future__ import annotations

from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import subprocess
import threading
import time
from http.client import HTTPConnection

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


class FakeProcessExecutableResolver:
    def resolve(self, _pid: int) -> Path:
        return Path("/fake/emulator")


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
    identity = ("adb", "-s", serial, "emu", "avd", "name")
    adb_devices = ("adb", "devices")
    avd_name = (
        "THS_CORE_33_ARM64" if serial == "emulator-5556" else "THS_API_33_ARM64"
    )
    return {
        state: result(
            state,
            0 if booted or process else 1,
            b"device\n" if booted else (b"offline\n" if process else b""),
        ),
        boot: result(boot, 0, b"1\n" if booted else b"0\n"),
        ps: result(ps, 0, f"123 emulator -port {serial.rsplit('-', 1)[1]}\n".encode() if process else b""),
        pidof: result(pidof, 0 if app_running else 1, b"234\n" if app_running else b""),
        identity: result(identity, stdout=f"{avd_name}\nOK\n".encode()),
        adb_devices: result(adb_devices, stdout=b"List of devices attached\n"),
    }


def running_device_runner(serial: str) -> FakeCommandRunner:
    return FakeCommandRunner(lifecycle_responses(serial))


def make_manager(runner: FakeCommandRunner, **kwargs: object):
    kwargs.setdefault("emulator_bin", "emulator")
    kwargs.setdefault("trusted_emulator_path", Path("/fake/emulator"))
    kwargs.setdefault(
        "process_executable_resolver", FakeProcessExecutableResolver()
    )
    return module.DeviceLifecycleManager(
        runner,
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


def start_test_server(*, token: str, manager):
    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.make_handler(manager, token))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def request(
    server,
    method: str,
    path: str,
    *,
    token: str | None = None,
    json_body: object | None = None,
    raw_body: bytes | None = None,
    content_type: str = "application/json",
):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if json_body is not None:
        raw_body = json.dumps(json_body).encode("utf-8")
    if raw_body is not None:
        headers["Content-Type"] = content_type
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=1)
    connection.request(method, path, body=raw_body, headers=headers)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response.status, json.loads(payload.decode("utf-8"))


@pytest.fixture
def http_server():
    server, thread = start_test_server(token="host-secret", manager=make_manager(FakeCommandRunner()))
    yield server
    server.shutdown()
    server.server_close()
    thread.join(1)


def test_http_service_requires_bearer_token(http_server) -> None:
    """Removing broker authentication would expose host lifecycle controls."""
    status, body = request(http_server, "GET", "/v1/devices")

    assert status == 401
    assert body == {"detail": "DEVICE_AUTH_REQUIRED"}


def test_http_service_rejects_action_fields_other_than_action(http_server) -> None:
    """Accepting serial overrides would let callers bypass fixed device mappings."""
    status, body = request(
        http_server,
        "POST",
        "/v1/devices/core_metrics/actions",
        token="host-secret",
        json_body={"action": "shutdown", "serial": "emulator-9999"},
    )

    assert status == 422
    assert body == {"detail": "DEVICE_ACTION_INVALID"}


def test_http_service_accepts_a_safe_action_and_returns_only_public_fields(http_server) -> None:
    """Returning command or device details would leak host-control implementation data."""
    status, body = request(
        http_server,
        "POST",
        "/v1/devices/core_metrics/actions",
        token="host-secret",
        json_body={"action": "shutdown"},
    )

    assert status == 202
    assert set(body) == {"operation_id", "role", "action", "state", "error_code", "updated_at"}
    assert body["role"] == "core_metrics"
    assert body["action"] == "shutdown"


def test_http_service_returns_404_for_unknown_operation(http_server) -> None:
    """Treating arbitrary operation IDs as valid would blur operation state ownership."""
    status, body = request(http_server, "GET", "/v1/operations/not-an-operation", token="host-secret")

    assert status == 404
    assert body == {"detail": "DEVICE_OPERATION_NOT_FOUND"}


def test_http_service_rejects_oversized_action_body(http_server) -> None:
    """Removing the request cap would permit unbounded loopback request buffering."""
    status, body = request(
        http_server,
        "POST",
        "/v1/devices/core_metrics/actions",
        token="host-secret",
        raw_body=b"x" * 1025,
    )

    assert status == 413
    assert body == {"detail": "DEVICE_REQUEST_TOO_LARGE"}


def test_http_service_rejects_non_json_action_body(http_server) -> None:
    """Parsing arbitrary non-JSON bodies would weaken the fixed action schema."""
    status, body = request(
        http_server,
        "POST",
        "/v1/devices/core_metrics/actions",
        token="host-secret",
        raw_body=b"action=shutdown",
        content_type="application/x-www-form-urlencoded",
    )

    assert status == 400
    assert body == {"detail": "DEVICE_ACTION_INVALID"}


def test_serve_rejects_ipv6_loopback_before_starting_a_server(tmp_path: Path) -> None:
    """Accepting ::1 would select an address family the broker does not support."""
    config = tmp_path / "host.env"
    config.write_text(
        "THS_DEVICE_LIFECYCLE_TOKEN=host-secret\n"
        "THS_DEVICE_LIFECYCLE_BIND_HOST=::1\n"
        "THS_DEVICE_LIFECYCLE_PORT=18765\n"
    )

    with pytest.raises(SystemExit) as rejected:
        module.serve(config)

    assert rejected.value.code == "DEVICE_BIND_NOT_LOOPBACK"


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
    "configs",
    [
        {
            **module.FIXED_CONFIGS,
            "core_metrics": module.DeviceConfig(
                "core_metrics", "OTHER_AVD", "emulator-5556", 5556, 27043, True
            ),
        },
        {
            **module.FIXED_CONFIGS,
            "unapproved": module.DeviceConfig(
                "unapproved", "THS_OTHER", "emulator-5558", 5558, 27045
            ),
        },
    ],
)
def test_manager_rejects_caller_controlled_role_configurations(configs) -> None:
    with pytest.raises(module.LifecycleRequestError) as invalid_config:
        make_manager(FakeCommandRunner(), configs=configs)
    assert invalid_config.value.error_code == "DEVICE_LIFECYCLE_UNCONFIGURED"


def test_manager_rejects_untrusted_emulator_executable() -> None:
    with pytest.raises(module.LifecycleRequestError) as invalid_executable:
        make_manager(FakeCommandRunner(), emulator_bin="/tmp/untrusted-emulator")
    assert invalid_executable.value.error_code == "DEVICE_LIFECYCLE_UNCONFIGURED"


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


def test_shutdown_removes_transient_launchctl_job_to_prevent_respawn() -> None:
    """A submitted emulator job must not relaunch the VM after a normal kill."""
    runner = running_device_runner("emulator-5554")
    state = ("adb", "-s", "emulator-5554", "get-state")
    ps = ("ps", "-axo", "pid=,command=")
    runner.sequences[state] = [result(state, 0, b"device\n"), result(state, 1)]
    runner.sequences[ps] = [result(ps, 0, b"")]
    manager = make_manager(runner)

    operation = manager.submit("main_fund_flow", "shutdown")
    assert wait_for_terminal(manager, operation.operation_id).state is module.LifecycleState.STOPPED

    kill_call = ("adb", "-s", "emulator-5554", "emu", "kill")
    remove_call = ("launchctl", "remove", "com.ths.avd.5554")
    assert kill_call in runner.calls
    assert remove_call in runner.calls
    assert runner.calls.index(kill_call) < runner.calls.index(remove_call)


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


def test_running_start_passes_configured_adb_path_to_host_scripts() -> None:
    """LaunchAgents may have a restricted PATH, so scripts receive absolute adb."""
    runner = running_device_runner("emulator-5556")
    adb_bin = "/opt/homebrew/bin/adb"
    manager = make_manager(runner, adb_bin=adb_bin)

    operation = wait_for_terminal(
        manager, manager.submit("core_metrics", "start_and_launch_app").operation_id
    )

    assert operation.state is module.LifecycleState.RUNNING
    assert (
        str(SCRIPT.parent / "configure-macos-core-display.sh"),
        "emulator-5556",
        adb_bin,
    ) in runner.calls
    assert (
        str(SCRIPT.parent / "watch-macos-device-bridge.sh"),
        "--once",
        "emulator-5556",
        "27043",
        adb_bin,
    ) in runner.calls


def test_stopped_start_uses_fixed_commands_and_reaches_running() -> None:
    serial = "emulator-5556"
    runner = FakeCommandRunner(lifecycle_responses(serial, booted=False, process=False))
    state_call = ("adb", "-s", serial, "get-state")
    boot_call = ("adb", "-s", serial, "shell", "getprop", "sys.boot_completed")
    runner.sequences[state_call] = [
        result(state_call, 1, stderr=b"absent"),
        result(state_call, 0, b"device\n"),
    ]
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


@pytest.mark.parametrize(
    "failed_command",
    [
        (str(SCRIPT.parent / "configure-macos-core-display.sh"), "emulator-5556", "adb"),
        (
            str(SCRIPT.parent / "watch-macos-device-bridge.sh"), "--once",
            "emulator-5556", "27043", "adb",
        ),
    ],
)
def test_required_start_setup_failure_is_sanitized_and_never_launches_app(
    failed_command: tuple[str, ...],
) -> None:
    runner = running_device_runner("emulator-5556")
    runner.responses[failed_command] = result(failed_command, 1, stderr=b"host details")
    manager = make_manager(runner)
    operation = wait_for_terminal(manager, manager.submit("core_metrics", "start_and_launch_app").operation_id)
    assert operation.state is module.LifecycleState.ERROR
    assert operation.error_code == "DEVICE_LIFECYCLE_FAILED"
    assert (
        "adb", "-s", "emulator-5556", "shell", "am", "start", "-n",
        "com.hexin.plat.android/com.hexin.plat.android.LogoEmptyActivity",
    ) not in runner.calls
    assert "host details" not in str(operation.public_dict())


def test_starting_device_waits_for_existing_boot_without_duplicate_emulator_launch() -> None:
    serial = "emulator-5556"
    runner = FakeCommandRunner(lifecycle_responses(serial, booted=False, process=True))
    state_call = ("adb", "-s", serial, "get-state")
    boot_call = ("adb", "-s", serial, "shell", "getprop", "sys.boot_completed")
    runner.sequences[state_call] = [result(state_call, 0, b"offline\n"), result(state_call, 0, b"device\n")]
    runner.responses[boot_call] = result(boot_call, 0, b"1\n")
    manager = make_manager(runner)
    operation = wait_for_terminal(manager, manager.submit("core_metrics", "start_and_launch_app").operation_id)
    assert operation.state is module.LifecycleState.RUNNING
    assert not any(call[:2] == ("launchctl", "submit") for call in runner.calls)


def test_unknown_start_state_fails_without_launching_an_emulator() -> None:
    serial = "emulator-5556"
    runner = FakeCommandRunner(lifecycle_responses(serial, booted=False, process=False))
    ps_call = ("ps", "-axo", "pid=,command=")
    runner.responses[ps_call] = result(ps_call, 1, stderr=b"host details")
    manager = make_manager(runner)
    operation = wait_for_terminal(manager, manager.submit("core_metrics", "start_and_launch_app").operation_id)
    assert operation.state is module.LifecycleState.ERROR
    assert operation.error_code == "DEVICE_LIFECYCLE_FAILED"
    assert not any(call[:2] == ("launchctl", "submit") for call in runner.calls)


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
