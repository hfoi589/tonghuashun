# Admin Dual-Device Lifecycle and Complete Image Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add authenticated dual-emulator controls and a complete APK/Frida-bearing Docker image with one-command existing-Mac redeployment plus an interactive fresh-Mac provisioning path.

**Architecture:** A token-authenticated Python standard-library service runs as a macOS LaunchAgent on `127.0.0.1:18765` and owns the fixed AVD/ADB command whitelist. FastAPI proxies fixed role/action requests through the existing admin boundary; the React admin page controls and refreshes both roles. The Docker image carries hash-pinned APK/Frida assets, while a host orchestrator preserves existing AVDs or creates only missing fixed AVDs and pauses for normal human login.

**Tech Stack:** Python 3 standard library host service, FastAPI/Pydantic, existing Redis/InMemory task stores, React 19, TypeScript, Vitest/Testing Library, multi-stage Docker/OCI assets, shell/launchd, Android SDK tools, OrbStack Docker Compose.

**Spec:** `docs/superpowers/specs/2026-08-27-admin-device-lifecycle-controls-design.md`

## Global Constraints

- The only allowed lifecycle actions are `start_and_launch_app` and `shutdown`.
- Both `core_metrics` and `main_fund_flow` are explicitly authorized for those two actions.
- Never issue account logout/switching, AVD clone/create/delete/reset, App install/uninstall, `pm clear`, `wipe-data`, `reboot -p`, or `am force-stop`.
- `main_fund_flow` start may open `LogoEmptyActivity` but may not tap, swipe, search, or navigate further.
- The browser and FastAPI callers never supply AVD names, serials, ports, activities, executable paths, or shell arguments.
- Host and API responses/logs expose only role, action, lifecycle state, operation id, timestamps, and fixed error codes.
- Lifecycle controls require an authenticated admin session, valid CSRF, current-session device lock, paused queue, and no running device task.
- The image contains only the fixed APK SHA-256 `2554490aa3f5e2df17ac0a711311f3f85ee3130008af9bb4ab12510b3d6e971e` and Frida Server 16.7.19 xz SHA-256 `36ec3d7474b1ac69c4e7ec985612fae771d37ffb71cb94858bc6978f69f5e581`.
- The decompressed Frida Server is exactly 53702368 bytes with SHA-256 `4eebf1fbc66ff54aba9a9124c2ef8b32b566616388c60e2caa65148a529d826a`.
- Existing AVD mode never installs/reinstalls an App; an installed APK mismatch fails closed.
- Provisioning installs APK/Frida only into roles whose fixed AVD was created by that invocation; pre-existing roles are never overwritten.
- First-time account login, captcha, device verification, permissions, and Android/third-party license acceptance remain human steps.
- The APK-bearing image is local/private only; scripts never push, save, export, or publish it.
- Redis, market, admin, session, capture, emulator, and AVD data must be preserved through deployment and acceptance.
- Docker deployment continues to use OrbStack, `.env`, `deploy/macos.env`, port 8001, and no Caddy.
- Existing mode requires 8 GiB free on the project, Android AVD, and resolved OrbStack data filesystems; provisioning mode requires 30 GiB on each.
- Resolve OrbStack external storage read-only from `~/.orbstack/vmconfig.json` key `data_dir`; reject missing, malformed, relative, or nonexistent paths and ignore ambient overrides.

## File Map

- Create `scripts/macos-device-lifecycle.py`: host-side fixed-role lifecycle manager and loopback HTTP server.
- Create `scripts/install-macos-device-lifecycle.sh`: secure config, LaunchAgent, and stable bridge watcher installation.
- Create `tests/test_macos_device_lifecycle.py`: host manager, HTTP authentication, command whitelist, and installer-contract tests.
- Modify `Dockerfile` and `.dockerignore`: include only the fixed APK, download/verify pinned Frida, and publish a safe asset manifest.
- Create `scripts/container-provision-device.sh`: image-contained fixed-role installer used only for newly created AVDs.
- Create `scripts/deploy-macos-one-click.sh`: auto-detect existing/provisioning mode and run the canonical OrbStack deployment.
- Create `scripts/provision-macos-from-image.sh`: create only missing fixed AVDs and install image assets into those roles.
- Modify `scripts/setup-admin.py`: generate session encryption and lifecycle secrets for a new deployment.
- Create `tests/test_macos_one_click_deploy.py`: image, existing-mode, provisioning, fail-closed, and forbidden-command contracts.
- Create `level2_service/device_lifecycle.py`: sanitized FastAPI-to-host client and lifecycle types.
- Create `tests/test_device_lifecycle.py`: client parsing, timeout, and error-redaction tests.
- Create `tests/test_device_lifecycle_api.py`: admin auth/CSRF/lock/busy/action API tests.
- Modify `level2_service/queue.py`: expose a conservative `has_running_task()` store predicate.
- Modify `level2_service/api.py`: inject lifecycle client, extend device response, add fixed action endpoint.
- Modify `level2_service/main.py`: parse lifecycle configuration and wire the production client.
- Modify `frontend/src/api.ts`: lifecycle types, device list, and action requests.
- Modify `frontend/src/AdminPage.tsx`: device polling, two per-device buttons, both-role session refresh, pending/error state, accessible confirmation dialog.
- Modify `frontend/src/AdminPage.test.tsx`: lifecycle button, dialog, role routing, state, and error regressions.
- Modify `frontend/src/styles.css`: existing admin-style lifecycle controls, danger action, dialog, and mobile layout.
- Modify `deploy/compose.yml` and `deploy/macos.env.example`: lifecycle URL/token/timeout wiring.
- Modify `tests/test_deploy_configuration.py` and `tests/test_deployment.py`: configuration and forbidden-command guards.
- Modify `AGENTS.md`, `README.md`, and `handoff.md`: new authorization boundary, installation, operations, and rollback.

---

### Task 1: Host Lifecycle State Machine and Command Whitelist

**Files:**
- Create: `scripts/macos-device-lifecycle.py`
- Create: `tests/test_macos_device_lifecycle.py`

**Interfaces:**
- Produces: `DeviceConfig`, `LifecycleState`, `LifecycleAction`, `DeviceOperation`, `CommandRunner`, and `DeviceLifecycleManager` inside the host script.
- Produces: `DeviceLifecycleManager.devices() -> list[dict[str, object]]`, `submit(role: str, action: str) -> DeviceOperation`, and `operation(operation_id: str) -> DeviceOperation | None` for Task 2.

- [ ] **Step 1: Write failing tests for fixed mappings and safe status detection**

Load the script without adding it to the package:

```python
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "macos-device-lifecycle.py"
spec = spec_from_file_location("macos_device_lifecycle", SCRIPT)
module = module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)
```

Add a `FakeCommandRunner` that records exact argument tuples and returns configured
`subprocess.CompletedProcess` values. Test:

```python
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
```

Test status mapping for boot-complete device, emulator process without ADB, and no
process/no ADB. Assert public dictionaries contain no `serial`, `avd`, `port`,
`command`, `stdout`, or `stderr` keys.

- [ ] **Step 2: Run the focused tests and verify the import/API failures**

Run:

```bash
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_device_lifecycle.py
```

Expected: FAIL because the host script and lifecycle types do not exist.

- [ ] **Step 3: Implement the fixed lifecycle model and command runner**

Implement these exact public types:

```python
class LifecycleState(str, Enum):
    UNCONFIGURED = "UNCONFIGURED"
    UNKNOWN = "UNKNOWN"
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class LifecycleAction(str, Enum):
    START_AND_LAUNCH_APP = "start_and_launch_app"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class DeviceConfig:
    role: str
    avd_name: str
    serial: str
    emulator_port: int
    frida_host_port: int
    calibrate_display: bool = False


@dataclass
class DeviceOperation:
    operation_id: str
    role: str
    action: LifecycleAction
    state: LifecycleState
    error_code: str | None
    updated_at: datetime

    def public_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "role": self.role,
            "action": self.action.value,
            "state": self.state.value,
            "error_code": self.error_code,
            "updated_at": self.updated_at.isoformat(),
        }
```

Use an injected `CommandRunner.run(args: tuple[str, ...], timeout: float) ->
CompletedProcess[bytes]`. The production runner must call `subprocess.run()` with
`shell=False`, a fixed environment, captured output, and bounded timeouts.

Build the role map only from validated host config:

```python
configs = {
    "core_metrics": DeviceConfig(
        "core_metrics", "THS_CORE_33_ARM64", "emulator-5556", 5556, 27043, True
    ),
    "main_fund_flow": DeviceConfig(
        "main_fund_flow", "THS_API_33_ARM64", "emulator-5554", 5554, 27042, False
    ),
}
```

Detect `RUNNING` only when ADB state is `device` and `sys.boot_completed` is `1`.
Use the fixed emulator port in a parsed `ps -axo pid=,command=` result to distinguish
`STARTING` from `STOPPED`; never accept a caller-supplied process pattern.

- [ ] **Step 4: Add failing start/shutdown/idempotency tests**

Test the exact action sequences:

```python
def test_shutdown_uses_emulator_kill_and_never_force_stops_app() -> None:
    runner = running_device_runner("emulator-5554")
    manager = make_manager(runner)
    operation = manager.submit("main_fund_flow", "shutdown")
    wait_for_terminal(manager, operation.operation_id)
    assert ("adb", "-s", "emulator-5554", "emu", "kill") in runner.calls
    rendered = "\n".join(" ".join(call) for call in runner.calls)
    for forbidden in ("force-stop", "pm clear", "uninstall", "wipe-data"):
        assert forbidden not in rendered
```

Add corresponding tests for:

- already stopped shutdown returns `STOPPED` without an ADB kill;
- already running start skips Emulator launch but opens the fixed Activity;
- stopped start uses fixed `launchctl submit`, waits for ADB and boot complete,
  calibrates only core, runs the per-role bridge repair, launches the fixed Activity,
  and reaches `RUNNING`;
- same-role concurrent action returns `DEVICE_ACTION_IN_PROGRESS`;
- core and fund operations use independent locks;
- boot timeout maps to `DEVICE_BOOT_TIMEOUT` without command output in the operation.

- [ ] **Step 5: Implement asynchronous idempotent actions**

`submit()` must create an operation id with `secrets.token_urlsafe(18)`, store the
operation under a lock, transition to `STARTING`/`STOPPING`, and run the fixed action
in a daemon thread. Split the implementation into the fixed private methods
`_start_and_launch_app(config)`, `_shutdown(config)`, `_wait_for_boot(config, deadline)`,
`_repair_bridge(config)`, and `_launch_app(config)`. Each method receives only the
validated `DeviceConfig`; none accepts raw caller data.

The start command is exactly:

```python
(
    "launchctl", "submit", "-l", f"com.ths.avd.{config.emulator_port}", "--",
    emulator_bin, "-avd", config.avd_name, "-port", str(config.emulator_port),
    "-no-snapshot", "-no-audio", "-gpu", "host", "-memory", "2048",
    "-cores", "4",
)
```

Bridge repair calls the repository watcher with `--once`, the fixed serial, and fixed
Frida host port. App launch is exactly:

```python
("adb", "-s", config.serial, "shell", "am", "start", "-n",
 "com.hexin.plat.android/com.hexin.plat.android.LogoEmptyActivity")
```

- [ ] **Step 6: Run host lifecycle tests**

Run:

```bash
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_device_lifecycle.py
```

Expected: PASS with no real ADB or Emulator invocation.

- [ ] **Step 7: Commit Task 1**

```bash
git add -- scripts/macos-device-lifecycle.py tests/test_macos_device_lifecycle.py
git commit -m "feat: add macos device lifecycle manager"
```

### Task 2: Loopback HTTP Service and LaunchAgent Installer

**Files:**
- Modify: `scripts/macos-device-lifecycle.py`
- Create: `scripts/install-macos-device-lifecycle.sh`
- Modify: `tests/test_macos_device_lifecycle.py`
- Modify: `tests/test_deployment.py`

**Interfaces:**
- Consumes: `DeviceLifecycleManager` from Task 1.
- Produces: bearer-authenticated `GET /v1/devices`, `POST /v1/devices/{role}/actions`, and `GET /v1/operations/{operation_id}` on host loopback.
- Produces: a LaunchAgent and stable per-role bridge watcher jobs for deployment tasks.

- [ ] **Step 1: Write failing HTTP authentication and schema tests**

Start the server on `127.0.0.1` with port `0` in a test thread. Assert:

```python
def test_http_service_requires_bearer_token_and_rejects_extra_fields() -> None:
    server = start_test_server(
        token="host-secret",
        manager=make_manager(FakeCommandRunner()),
    )
    assert request(server, "GET", "/v1/devices").status == 401
    assert request(
        server,
        "POST",
        "/v1/devices/core_metrics/actions",
        token="host-secret",
        json={"action": "shutdown", "serial": "emulator-9999"},
    ).status == 422
```

Also assert successful action submission returns 202 and only the safe public fields;
unknown operation returns 404; request body larger than 1024 bytes returns 413; non-JSON
body returns 400.

- [ ] **Step 2: Run the HTTP tests and verify failure**

Run:

```bash
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_device_lifecycle.py -k http
```

Expected: FAIL because no HTTP handler exists.

- [ ] **Step 3: Implement the loopback server**

Add `LifecycleRequestHandler(BaseHTTPRequestHandler)` and:

```python
def serve(config_path: Path) -> None:
    settings = load_settings(config_path)
    if settings.bind_host not in {"127.0.0.1", "::1"}:
        raise SystemExit("DEVICE_BIND_NOT_LOOPBACK")
    manager = DeviceLifecycleManager(settings.devices, SubprocessCommandRunner(settings))
    handler = make_handler(manager=manager, token=settings.token)
    server = ThreadingHTTPServer((settings.bind_host, settings.port), handler)
    server.serve_forever()
```

Use `hmac.compare_digest()` for bearer-token comparison, `Content-Length <= 1024`,
exact JSON field sets, `application/json`, and fixed safe error documents:

```json
{"detail":"DEVICE_ACTION_INVALID"}
```

Override `log_message()` so it logs no Authorization header, request body, query string,
or exception text.

- [ ] **Step 4: Write failing installer contract tests**

In `tests/test_deployment.py`, read the installer as text and assert it:

- writes `~/.config/ths-device-lifecycle.env` with mode 0600;
- writes `~/Library/LaunchAgents/com.ths.device-lifecycle.plist`;
- copies the service and watcher into `~/.local/lib/ths-device-lifecycle/` and points every
  plist at that stable installed copy;
- installs stable bridge watcher jobs for 27042 and 27043;
- loads with `launchctl bootstrap gui/$UID` and `kickstart -k`;
- never places the Token inside the plist or prints it;
- never contains any forbidden device mutation command.

- [ ] **Step 5: Implement the installer**

Implement this CLI:

```text
scripts/install-macos-device-lifecycle.sh \
  --project-root /Users/wilson/tonghuashun \
  --env-file /Users/wilson/tonghuashun/.env
```

The script must:

1. validate macOS, `python3`, `adb`, `emulator`, `launchctl`, both existing AVDs,
   and the stable watcher path;
2. generate `THS_DEVICE_LIFECYCLE_TOKEN` with `secrets.token_urlsafe(32)` only when
   absent from the supplied `.env`, writing without printing the value;
3. copy the lifecycle service and watcher into `~/.local/lib/ths-device-lifecycle/` using
   mode 0700 for directories and 0755 for executables;
4. write a 0600 host config containing the same Token and exact fixed mappings;
5. write plist files without secrets and point them only at the installed copy;
6. bootout old lifecycle/bridge labels, bootstrap the stable plists, then kickstart;
7. print only safe success/failure messages.

Use a temporary file plus `mv` for `.env` and host-config updates; preserve existing
file permissions and unrelated lines.

- [ ] **Step 6: Run host and deployment tests**

Run:

```bash
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_device_lifecycle.py tests/test_deployment.py
```

Expected: PASS.

- [ ] **Step 7: Commit Task 2**

```bash
git add -- scripts/macos-device-lifecycle.py \
  scripts/install-macos-device-lifecycle.sh \
  tests/test_macos_device_lifecycle.py tests/test_deployment.py
git commit -m "feat: expose safe macos lifecycle broker"
```

### Task 3: Sanitized Backend Lifecycle Client and Configuration

**Files:**
- Create: `level2_service/device_lifecycle.py`
- Create: `tests/test_device_lifecycle.py`
- Modify: `level2_service/main.py`
- Modify: `tests/test_deploy_configuration.py`
- Modify: `deploy/compose.yml`
- Modify: `deploy/macos.env.example`

**Interfaces:**
- Produces: `DeviceLifecycleState`, `DeviceLifecycleAction`, `DeviceLifecycleStatus`, `DeviceLifecycleOperation`, `DeviceLifecycleError`, and `DeviceLifecycleClient`.
- Produces: `DeviceLifecycleClient.devices()`, `submit(role, action)`, and `operation(operation_id)` for Task 4.

- [ ] **Step 1: Write failing client parsing and redaction tests**

Use an injected opener callable and assert:

```python
def test_client_submits_only_fixed_role_and_action() -> None:
    opener = RecordingOpener({
        "operation_id": "op-1",
        "role": "core_metrics",
        "action": "shutdown",
        "state": "STOPPING",
        "error_code": None,
        "updated_at": "2026-08-27T14:00:00Z",
    })
    client = DeviceLifecycleClient(
        "http://host.docker.internal:18765", "secret", opener=opener
    )
    result = client.submit("core_metrics", "shutdown")
    assert result.operation_id == "op-1"
    assert json.loads(opener.requests[0].data) == {"action": "shutdown"}
    assert "secret" not in repr(client)
```

Test invalid state/error responses, 401/409/422/500 mapping, timeout, malformed JSON,
and an upstream exception containing `token=private`/command text. All public errors must
contain only a fixed code.

- [ ] **Step 2: Run client tests and verify failure**

Run:

```bash
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q tests/test_device_lifecycle.py
```

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement the client and strict models**

Implement:

```python
class DeviceLifecycleAction(StrEnum):
    START_AND_LAUNCH_APP = "start_and_launch_app"
    SHUTDOWN = "shutdown"


class DeviceLifecycleState(StrEnum):
    UNCONFIGURED = "UNCONFIGURED"
    UNKNOWN = "UNKNOWN"
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class DeviceLifecycleStatus:
    role: str
    state: DeviceLifecycleState
    operation_id: str | None
    error_code: str | None
    updated_at: datetime | None


@dataclass(frozen=True)
class DeviceLifecycleOperation:
    operation_id: str
    role: str
    action: DeviceLifecycleAction
    state: DeviceLifecycleState
    error_code: str | None
    updated_at: datetime


class DeviceLifecycleClient:
    def devices(self) -> tuple[DeviceLifecycleStatus, ...]:
        return tuple(self._request("GET", "/v1/devices", expected="devices"))

    def submit(
        self,
        role: str,
        action: DeviceLifecycleAction,
    ) -> DeviceLifecycleOperation:
        return self._request(
            "POST",
            f"/v1/devices/{role}/actions",
            payload={"action": action.value},
            expected="operation",
        )

    def operation(self, operation_id: str) -> DeviceLifecycleOperation:
        return self._request(
            "GET",
            f"/v1/operations/{operation_id}",
            expected="operation",
        )
```

Only allow base URLs with `http` and hostname in
`{"host.docker.internal", "127.0.0.1", "localhost"}`. Store the Token in a
`repr=False` field. Parse exact safe keys and sanitize every upstream failure to a fixed
`DeviceLifecycleError.error_code`.

- [ ] **Step 4: Write failing production configuration tests**

Extend `tests/test_deploy_configuration.py` to assert:

- URL and Token must be provided together;
- timeout is positive;
- invalid scheme/host is rejected;
- missing URL/Token produces `device_lifecycle=None` rather than blocking startup;
- Compose injects URL, Token, and timeout without a default Token value.

- [ ] **Step 5: Wire configuration and production injection**

Add to `AppConfig`:

```python
device_lifecycle_url: str | None
device_lifecycle_token: str | None
device_lifecycle_timeout_seconds: float
```

Parse `THS_DEVICE_LIFECYCLE_URL`, `THS_DEVICE_LIFECYCLE_TOKEN`, and
`THS_DEVICE_LIFECYCLE_TIMEOUT_SECONDS=5`. In `create_production_app()`, construct one
`DeviceLifecycleClient` only when URL and Token are both present, then pass it to
`create_app(device_lifecycle=device_lifecycle)`.

- [ ] **Step 6: Run client/config tests**

Run:

```bash
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_device_lifecycle.py tests/test_deploy_configuration.py
```

Expected: PASS.

- [ ] **Step 7: Commit Task 3**

```bash
git add -- level2_service/device_lifecycle.py level2_service/main.py \
  deploy/compose.yml deploy/macos.env.example \
  tests/test_device_lifecycle.py tests/test_deploy_configuration.py
git commit -m "feat: add device lifecycle client configuration"
```

### Task 4: Authenticated Device Lifecycle Admin API

**Files:**
- Modify: `level2_service/queue.py`
- Modify: `level2_service/api.py`
- Create: `tests/test_device_lifecycle_api.py`
- Modify: `tests/test_admin_security.py`
- Modify: `tests/test_device_websocket.py`

**Interfaces:**
- Consumes: `DeviceLifecycleClient` and lifecycle types from Task 3.
- Produces: extended `GET /api/admin/devices` and `POST /api/admin/devices/{role}/actions` for Task 5.
- Produces: `TaskStore.has_running_task() -> bool`.

- [ ] **Step 1: Write failing store busy-predicate tests**

Add tests for both `InMemoryStreams` and Redis-backed store contract:

```python
def test_store_reports_only_running_or_partial_tasks_as_device_busy() -> None:
    store = InMemoryStreams()
    task = TaskRecord(task_id="job-1", symbol="SZ.000001")
    store.enqueue(task)
    assert store.has_running_task() is False
    store.transition(task.task_id, TaskStatus.RUNNING)
    assert store.has_running_task() is True
    store.transition(task.task_id, TaskStatus.COMPLETED)
    assert store.has_running_task() is False
```

Use `RUNNING` and `PARTIAL` as busy; `QUEUED`, `WAITING_ADMIN`, and terminal states are not
actively using a device once the admin lock has paused new claims.

- [ ] **Step 2: Implement `has_running_task()`**

Add the method to `TaskStore`, scan in-memory records under the claim lock, and scan the
Redis index using safe deserialization. This endpoint is admin-only and rare; correctness is
preferred over adding another Redis index.

- [ ] **Step 3: Write failing API security/action tests**

Create a `FakeDeviceLifecycle` with deterministic statuses and recorded calls. Test:

```python
def test_admin_device_action_requires_csrf_lock_and_idle_runner() -> None:
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        device_lifecycle=FakeDeviceLifecycle(),
    )
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/api/admin/devices/core_metrics/actions").status_code == 401
    login_admin(client)
    assert client.post(
        "/api/admin/devices/core_metrics/actions",
        json={"action": "shutdown"},
    ).status_code == 403
    csrf = client.cookies.get("ths_csrf")
    assert client.post(
        "/api/admin/devices/core_metrics/actions",
        headers={"X-CSRF-Token": csrf},
        json={"action": "shutdown"},
    ).status_code == 409
```

After acquiring the lock, assert both roles accept both actions and the fake receives only
the fixed role/action. Also test busy task 409, invalid role 404, extra JSON field 422,
unconfigured lifecycle 503, upstream fixed 409/503/504 mapping, and no secret text in
response/logs.

- [ ] **Step 4: Implement strict request/response models and endpoint**

Add models with `ConfigDict(extra="forbid")`:

```python
class DeviceLifecycleActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: DeviceLifecycleAction


class AdminDeviceLifecycleResponse(BaseModel):
    state: str
    operation_id: str | None
    error_code: str | None
    updated_at: datetime | None
```

Extend `AdminDeviceHealthResponse` with `lifecycle`. Inject
`device_lifecycle: DeviceLifecycleClient | None` into `create_app()`. The action endpoint:

1. resolves only the two device roles;
2. uses the session returned by `require_csrf`;
3. requires `runner_control.authorizes_input(session.session_id)`;
4. requires `queue_paused` and `not store.has_running_task()`;
5. calls `device_lifecycle.submit(role, payload.action)`;
6. returns HTTP 202 with the safe operation state;
7. maps only fixed error codes to 409/503/504.

- [ ] **Step 5: Keep WebSocket health compatible**

Update device health payload tests so the initial `device_status` includes lifecycle data
when configured and `UNCONFIGURED` otherwise. Do not let lifecycle polling change frame or
input authorization semantics.

- [ ] **Step 6: Run API/security/device tests**

Run:

```bash
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_device_lifecycle_api.py tests/test_admin_security.py \
  tests/test_device_websocket.py
```

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```bash
git add -- level2_service/queue.py level2_service/api.py \
  tests/test_device_lifecycle_api.py tests/test_admin_security.py \
  tests/test_device_websocket.py
git commit -m "feat: expose locked admin device lifecycle actions"
```

### Task 5: Per-Device Admin Controls and Accessible Confirmations

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/AdminPage.tsx`
- Modify: `frontend/src/AdminPage.test.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: the Task 4 device list and action endpoint.
- Produces: per-role start/shutdown controls, lifecycle status, progress/error feedback, and confirmation dialog.

- [ ] **Step 1: Write failing API type and request tests through AdminPage**

Add TypeScript types:

```typescript
export type DeviceRole = 'core_metrics' | 'main_fund_flow'
export type DeviceLifecycleState =
  | 'UNCONFIGURED' | 'UNKNOWN' | 'STOPPED' | 'STARTING'
  | 'RUNNING' | 'STOPPING' | 'ERROR'
export type DeviceLifecycleAction = 'start_and_launch_app' | 'shutdown'

export interface AdminDeviceHealth {
  role: DeviceRole
  label: string
  adb: string
  app: string
  frida: string
  lifecycle: {
    state: DeviceLifecycleState
    operation_id: string | null
    error_code: string | null
    updated_at: string | null
  }
}
```

In `AdminPage.test.tsx`, mock `/api/admin/devices` with one running and one stopped device.
Assert both cards render both button labels, role-specific lifecycle text, and that controls
are disabled before lock acquisition. Mock `/api/admin/account-sessions` with both roles and
assert each device card shows its own session state and refresh button.

- [ ] **Step 2: Run the focused frontend test and verify failure**

Run:

```bash
cd frontend && npm test -- --run src/AdminPage.test.tsx
```

Expected: FAIL because types, polling, and buttons do not exist.

- [ ] **Step 3: Implement API methods and device polling**

Add:

```typescript
devices: () => request<{ devices: AdminDeviceHealth[] }>('/api/admin/devices'),
deviceAction: (
  role: DeviceRole,
  action: DeviceLifecycleAction,
  csrfToken: string,
) => request<AdminDeviceHealth['lifecycle']>(`/api/admin/devices/${role}/actions`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
  body: JSON.stringify({ action }),
}),
```

In `AdminPage`, load devices after login/session restoration and during manual health refresh.
Poll every 2 seconds while any device is `STARTING`/`STOPPING`; otherwise use the existing
15-second health cadence. Store pending/error state keyed by role so simultaneous cards do
not overwrite each other. Replace the fund-only session state with a role-keyed session map;
load both roles after authentication and let each device card call the existing
`refreshAccountSession(role, csrf)` endpoint.

- [ ] **Step 4: Write failing confirmation, pending, and routing tests**

Test:

- clicking core shutdown opens `role="alertdialog"` and initially focuses Cancel;
- Cancel and Escape issue no request and restore focus;
- Tab/Shift+Tab remain inside the dialog;
- Confirm sends core + `shutdown` + CSRF;
- fund start sends fund + `start_and_launch_app`;
- core session refresh calls `core_metrics` and fund session refresh calls
  `main_fund_flow`, with pending/error text scoped to the matching card;
- pending text and disabled buttons are scoped to the selected card;
- a safe API error renders a card-local `role="alert"`;
- fund warning says the action never switches account, clears data, reinstalls, or navigates;
- `RUNNING + app OFFLINE` leaves start enabled; `RUNNING + app ONLINE` disables it;
- `STOPPED` disables shutdown; `UNCONFIGURED` disables both.

- [ ] **Step 5: Implement controls and dialog**

Extend `DeviceViewportProps`:

```typescript
interface DeviceViewportProps {
  locked: boolean
  active: boolean
  streamUrl?: string
  title?: string
  warning?: string
  role: DeviceRole
  lifecycle?: AdminDeviceHealth['lifecycle']
  actionPending?: DeviceLifecycleAction | null
  actionError?: string | null
  sessionStatus?: AccountSessionStatus | null
  sessionRefreshPending?: boolean
  onLifecycleAction?: (role: DeviceRole, action: DeviceLifecycleAction) => void
  onRefreshSession?: (role: DeviceRole) => void
}
```

Render a `.device-lifecycle-controls` block before scroll controls. Add one shared
`DeviceLifecycleDialog` at `AdminPage` level using refs for Cancel/Confirm/trigger focus,
Escape handling, and a two-element focus trap. Confirmation copy must exactly match the
spec, including the additional fund-account protection sentence.

Use the existing admin visual language: operational density, neutral border, one red danger
button, no gradients, no nested decorative cards, visible disabled/focus states, and full-width
buttons below 520px.

- [ ] **Step 6: Run frontend tests and build**

Run:

```bash
cd frontend
npm test -- --run src/AdminPage.test.tsx
npm test
npm run build
```

Expected: all pass.

- [ ] **Step 7: Commit Task 5**

```bash
git add -- frontend/src/api.ts frontend/src/AdminPage.tsx \
  frontend/src/AdminPage.test.tsx frontend/src/styles.css
git commit -m "feat: add admin virtual machine controls"
```

### Task 6: Documentation, Rules, and Deployment Contract

**Files:**
- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `handoff.md`
- Modify: `deploy/macos.env.example`
- Modify: `tests/test_deployment.py`
- Modify: `tests/test_deploy_configuration.py`

**Interfaces:**
- Consumes: all Task 1-5 behavior and the approved complete-image/provisioning design.
- Produces: operator installation, image asset policy, existing/fresh Mac flows, authorization boundary, rollback, and acceptance documentation.

- [ ] **Step 1: Write failing deployment/rules assertions**

Add tests that assert:

- `deploy/macos.env.example` documents URL and timeout but leaves Token empty;
- Compose loads all three lifecycle variables;
- AGENTS explicitly permits only normal shutdown and start+launch for both roles through the
  authenticated lifecycle broker;
- AGENTS still forbids account changes, clear/reinstall, `force-stop`, AVD mutation, and in-App
  navigation on fund;
- handoff documents LaunchAgent installation, state/error semantics, standard deployment,
  manual queue recovery, and rollback without deleting data volumes;
- README documents the APK/Frida-bearing image, existing-AVD one-command deployment,
  interactive first-time provisioning, and local/private-only image boundary.

- [ ] **Step 2: Run documentation contract tests and verify failure**

Run:

```bash
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_deployment.py tests/test_deploy_configuration.py
```

Expected: FAIL on missing lifecycle documentation assertions.

- [ ] **Step 3: Update rules and operator documentation**

Document the normal one-command entry:

```bash
scripts/deploy-macos-one-click.sh --mode auto
```

Document that operators must acquire the device lock, wait for running tasks to finish, use
one device action at a time, release the lock, and explicitly resume the queue. Record all
fixed error codes and the rollback sequence from the spec. Correct the prior README claim
that the APK is absent from Git history; state instead that it is excluded from the old image
but deliberately included in the new local/private image after fixed digest verification.

- [ ] **Step 4: Run documentation/deployment tests**

Run:

```bash
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_deployment.py tests/test_deploy_configuration.py
git diff --check
```

Expected: PASS and no whitespace errors.

- [ ] **Step 5: Commit Task 6**

```bash
git add -- AGENTS.md README.md handoff.md deploy/macos.env.example \
  tests/test_deployment.py tests/test_deploy_configuration.py
git commit -m "docs: document dual device lifecycle operations"
```

### Task 7: Complete APK/Frida Image and Existing-Mac One-Command Deployment

**Files:**
- Modify: `Dockerfile`
- Modify: `.dockerignore`
- Create: `scripts/container-provision-device.sh`
- Create: `scripts/macos_deploy.py`
- Create: `scripts/deploy-macos-one-click.sh`
- Modify: `scripts/setup-admin.py`
- Create: `tests/test_macos_one_click_deploy.py`
- Modify: `tests/test_deploy_configuration.py`

**Interfaces:**
- Consumes: Task 2's lifecycle installer/broker and the fixed asset hashes from the approved spec.
- Produces: image assets under `/opt/ths/assets`, fixed-role container provisioner, `MacDeploymentOrchestrator`, and `scripts/deploy-macos-one-click.sh --mode auto|existing|provision`.
- Produces: `MacDeploymentOrchestrator.deploy_existing() -> DeploymentResult` for Task 8 and Task 9.

- [ ] **Step 1: Write failing image asset contract tests**

In `tests/test_macos_one_click_deploy.py`, assert the tracked APK exists and matches the exact
size, digest, and ARM ABI preflight. Read Dockerfile and `.dockerignore` and assert:

```python
APK_SHA256 = "2554490aa3f5e2df17ac0a711311f3f85ee3130008af9bb4ab12510b3d6e971e"
FRIDA_SHA256 = "36ec3d7474b1ac69c4e7ec985612fae771d37ffb71cb94858bc6978f69f5e581"
FRIDA_BINARY_SHA256 = "4eebf1fbc66ff54aba9a9124c2ef8b32b566616388c60e2caa65148a529d826a"


def test_image_contains_only_the_pinned_mobile_assets() -> None:
    dockerfile = Path("Dockerfile").read_text()
    dockerignore = Path(".dockerignore").read_text()
    assert "!ths_android_V11_59_03.apk" in dockerignore
    assert "COPY --chmod=0444 ths_android_V11_59_03.apk /opt/ths/assets/ths.apk" in dockerfile
    assert APK_SHA256 in dockerfile
    assert "frida-server-16.7.19-android-arm64.xz" in dockerfile
    assert FRIDA_SHA256 in dockerfile
    for forbidden in ("COPY .env", "COPY deploy/macos.env", "docker push", "docker save"):
        assert forbidden not in dockerfile
```

Also assert no `ARG` can override the two URLs/digests and that the final stage copies a
read-only `manifest.json`, APK, Frida binary, and `container-provision-device` executable.

- [ ] **Step 2: Run image contract tests and verify failure**

Run:

```bash
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py -k image
```

Expected: FAIL because the APK is ignored and the image has no asset stage.

- [ ] **Step 3: Implement the hash-pinned asset stage**

Keep the existing frontend/API stages. Add a `mobile-assets` stage that installs only
`ca-certificates curl xz-utils`, copies the exact APK, downloads the exact Frida URL, verifies
both digests, decompresses Frida, and writes:

```json
{
  "apk": {
    "filename": "ths.apk",
    "size": 214088292,
    "sha256": "2554490aa3f5e2df17ac0a711311f3f85ee3130008af9bb4ab12510b3d6e971e",
    "abis": ["arm64-v8a", "armeabi-v7a"]
  },
  "frida_server": {
    "version": "16.7.19",
    "size": 53702368,
    "sha256_xz": "36ec3d7474b1ac69c4e7ec985612fae771d37ffb71cb94858bc6978f69f5e581",
    "sha256": "4eebf1fbc66ff54aba9a9124c2ef8b32b566616388c60e2caa65148a529d826a"
  }
}
```

Copy assets into the API stage with APK/manifest mode 0444 and Frida mode 0555. Add OCI
labels for both digests. Change `.dockerignore` by retaining `*.apk` and adding only
`!ths_android_V11_59_03.apk` immediately after it.

- [ ] **Step 4: Write failing setup-secret and fixed container-provisioner tests**

Extend tests to run `scripts/setup-admin.py` with patched `getpass.getpass` and assert a new
0600 env contains exactly one each of:

- `ADMIN_PASSWORD_HASH`;
- `ADMIN_SESSION_SECRET`;
- `THS_SESSION_ENCRYPTION_KEY` as a valid 32-byte URL-safe Fernet key;
- `THS_DEVICE_LIFECYCLE_TOKEN`.

For `scripts/container-provision-device.sh`, assert the only input is one role, role mapping is
fixed, and commands are limited to install the read-only APK, root/push/chmod/start fixed Frida,
and fixed port forwarding. Assert it contains no reinstall flag `-r`, clear/uninstall/wipe,
account action, navigation, arbitrary serial variable, or shell-evaluated caller input.

- [ ] **Step 5: Implement secret setup and image-contained provisioner**

Use Python `base64.urlsafe_b64encode(os.urandom(32))` for
`THS_SESSION_ENCRYPTION_KEY`; keep the existing exclusive 0600 creation behavior.

`container-provision-device.sh ROLE` must map:

```sh
core_metrics) serial=emulator-5556; host_port=27043 ;;
main_fund_flow) serial=emulator-5554; host_port=27042 ;;
*) exit 2 ;;
```

It must verify `sys.boot_completed=1`, refuse when the package already exists, execute
`adb install /opt/ths/assets/ths.apk` without `-r`, push the fixed Frida binary, chmod 0755,
start it, and create the fixed forward. It never opens the App; the lifecycle broker does that
after installation.

- [ ] **Step 6: Write failing existing-AVD orchestrator tests**

Import `scripts/macos_deploy.py` with an injected `CommandRunner`, `LifecycleBroker`, and
`FileSystem`. Test:

```python
def test_existing_mode_preserves_both_avds_and_uses_canonical_compose() -> None:
    runner = existing_mac_runner(apk_sha256=APK_SHA256)
    result = make_orchestrator(runner).deploy_existing()
    assert result.mode == "existing"
    rendered = "\n".join(" ".join(call) for call in runner.calls)
    assert "docker --context orbstack compose" in rendered
    assert "--env-file .env --env-file deploy/macos.env" in rendered
    for forbidden in ("install -r", " install ", "pm clear", "wipe-data", "delete avd", "docker push", "docker save", "down -v"):
        assert forbidden not in rendered
```

Add tests for:

- exact two AVD detection;
- APK mismatch returns `INSTALLED_APK_MISMATCH` before Compose rebuild;
- missing prerequisite returns a fixed error;
- `.env` missing invokes setup-admin once; existing `.env` is never overwritten;
- lifecycle installer runs before broker start calls;
- broker starts only a stopped role and waits for `RUNNING`;
- Compose health timeout is sanitized;
- default mode is `auto`, and two existing AVDs choose `existing`.

- [ ] **Step 7: Implement the existing-AVD deployment path**

Implement exact result/error types:

```python
@dataclass(frozen=True)
class DeploymentResult:
    mode: str
    state: str
    error_code: str | None = None


class DeploymentError(RuntimeError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code
```

`MacDeploymentOrchestrator.deploy_existing()` must:

1. validate Apple Silicon, required commands, and mode-specific disk floors (existing 8 GiB; provisioning 30 GiB) on the project, Android AVD, and OrbStack `vmconfig.json:data_dir` filesystems;
2. validate both fixed AVD names from `emulator -list-avds`;
3. create `.env` only when absent and validate its mode/required keys;
4. build `ths-level2-api:local` with OrbStack Compose;
5. read `/opt/ths/assets/manifest.json` from the image without exposing secrets;
6. install/update the host lifecycle service from Task 2;
7. start stopped roles through the broker and wait for `RUNNING`;
8. retrieve each installed base-APK path with fixed ADB calls, validate the path format,
   execute fixed `sha256sum` on-device, and require the manifest digest;
9. run the canonical Compose `up -d --build` command without `down`;
10. wait for API/Redis health and return `READY`.

The wrapper is exactly:

```sh
#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "${PYTHON_BIN:-python3}" "$script_dir/macos_deploy.py" "$@"
```

The CLI supports `--mode auto|existing|provision`, `--project-root`, and `--env-file`; it
accepts no AVD, serial, image, APK, Frida, port, URL, or command overrides.

- [ ] **Step 8: Run focused deployment tests and an image build**

Run:

```bash
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py tests/test_deploy_configuration.py
docker --context orbstack build --target api -t ths-level2-api:asset-test .
docker --context orbstack run --rm --entrypoint python \
  ths-level2-api:asset-test -c \
  'import hashlib,json,pathlib; p=pathlib.Path("/opt/ths/assets"); m=json.loads((p/"manifest.json").read_text()); assert hashlib.sha256((p/"ths.apk").read_bytes()).hexdigest()==m["apk"]["sha256"]; print("assets=verified")'
```

Expected: tests pass, image builds, and output is only `assets=verified`.

- [ ] **Step 9: Commit Task 7**

```bash
git add -- Dockerfile .dockerignore scripts/container-provision-device.sh \
  scripts/macos_deploy.py scripts/deploy-macos-one-click.sh scripts/setup-admin.py \
  tests/test_macos_one_click_deploy.py tests/test_deploy_configuration.py
git commit -m "feat: package mobile assets for one-command deployment"
```

### Task 8: Fresh-Mac and Partial-AVD Interactive Provisioning

**Files:**
- Modify: `scripts/macos_deploy.py`
- Create: `scripts/provision-macos-from-image.sh`
- Modify: `tests/test_macos_one_click_deploy.py`
- Modify: `frontend/src/AdminPage.tsx`
- Modify: `frontend/src/AdminPage.test.tsx`

**Interfaces:**
- Consumes: Task 7's image assets, container provisioner, orchestrator, and Task 5's both-role session UI.
- Produces: `MacDeploymentOrchestrator.provision_missing() -> DeploymentResult` and a recoverable first-time onboarding state.

- [ ] **Step 1: Write failing missing-AVD classification tests**

Test `--mode auto` with exact AVD sets:

| Existing AVDs | Expected behavior |
| --- | --- |
| both fixed AVDs | `deploy_existing()` |
| neither | create/install both roles |
| fund only | preserve fund, create/install core only |
| core only | preserve core, create/install fund only |
| unknown extra AVDs plus fixed subset | ignore unknown names; operate only fixed roles |

Assert the orchestrator records the initially missing roles immutably and never changes that
set after commands run.

- [ ] **Step 2: Write failing provisioning command and preservation tests**

Using a fake runner, assert for each initially missing role:

- `sdkmanager` uses only `system-images;android-33;google_apis;arm64-v8a`;
- `avdmanager create avd` uses the fixed name and contains no `--force`;
- fixed `launchctl submit` starts the role and waits for boot;
- the one-shot asset container calls `container-provision-device ROLE`;
- core display calibration runs only for core;
- lifecycle broker opens the App after asset installation.

For each pre-existing role, assert none of create/install/push/chmod commands occur and its
installed APK digest is checked exactly as in existing mode.

Test missing SDK license maps to `ANDROID_LICENSE_REQUIRED`, missing system image download to
`ANDROID_SYSTEM_IMAGE_UNAVAILABLE`, boot timeout to `DEVICE_BOOT_TIMEOUT`, and partial failure
leaves all created AVD files intact.

- [ ] **Step 3: Implement provisioning with fixed commands only**

`provision_missing()` must:

1. perform common preflight but never install OrbStack/Homebrew/JDK and never pipe `yes` into
   Android licenses;
2. run `sdkmanager --list_installed`; when the fixed system image is absent, call
   `sdkmanager <fixed-image>` and sanitize a license failure;
3. create only the initial missing-role set with fixed `avdmanager create avd --name ...
   --package ...`, without `--force`;
4. start each new role sequentially and wait for boot;
5. run the image-contained provisioner only for that new role through a short-lived container
   using `ADB_SERVER_SOCKET=tcp:host.docker.internal:5037`;
6. install/update the host lifecycle service, then call the broker to open the App;
7. run the standard Compose deployment;
8. return `FIRST_TIME_LOGIN_REQUIRED` while either encrypted role session file is absent;
9. on a later run, preserve the now-existing AVDs and return `READY` only after both session
   files exist and a data-only acceptance task completes.

The host shell wrapper must contain only stable project-root resolution and execute:

```sh
python3 scripts/macos_deploy.py --mode provision "$@"
```

- [ ] **Step 4: Write failing human-gate and resumability tests**

Assert provisioning output contains only safe instructions:

- open `http://127.0.0.1:8001/#admin`;
- manually log in/verify each newly created role;
- click the matching role's session refresh;
- rerun the same one-command script.

It must not print admin password, lifecycle Token, session key, Cookie, AVD data path, command
stderr, or account identity. A rerun with AVDs present but missing session files must not create
or install again and must remain `FIRST_TIME_LOGIN_REQUIRED`.

- [ ] **Step 5: Extend both-role session UI tests if Task 5 did not fully cover onboarding**

Verify each device card shows its `AccountSessionStatus`, refresh button, updated timestamp,
pending state, and fixed-error response. This step may add only missing onboarding assertions;
do not redesign the Task 5 controls.

- [ ] **Step 6: Run provisioning and frontend tests**

Run:

```bash
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_macos_one_click_deploy.py
cd frontend
npm test -- --run src/AdminPage.test.tsx
npm run build
```

Expected: all pass; no real AVD/ADB/SDK command runs in automated tests.

- [ ] **Step 7: Commit Task 8**

```bash
git add -- scripts/macos_deploy.py scripts/provision-macos-from-image.sh \
  tests/test_macos_one_click_deploy.py frontend/src/AdminPage.tsx \
  frontend/src/AdminPage.test.tsx
git commit -m "feat: add interactive fresh mac provisioning"
```

### Task 9: Full Verification, Installation, Deployment, and Real Dual-Device Acceptance

**Files:**
- Modify only if verification finds a defect in Task 1-8 files or measured evidence must be
  updated in `handoff.md`.
- Do not add generated LaunchAgent, host config, Token, `.env`, logs, or `graphify-out/` to Git.

**Interfaces:**
- Consumes: complete Task 1-8 feature branch.
- Produces: installed host helper, deployed API/frontend, and acceptance evidence.

- [ ] **Step 1: Run the complete automated verification**

Run:

```bash
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q
cd frontend && npm test && npm run build
git diff --check
```

Expected: zero failures. Record the final Python and frontend test counts in `handoff.md` if
they differ from the Task 6 draft.

- [ ] **Step 2: Request independent code review**

Use `superpowers:requesting-code-review` over:

```bash
BASE_SHA=$(git merge-base main HEAD)
HEAD_SHA=$(git rev-parse HEAD)
```

Fix every Critical and Important finding. Re-run the covering tests after each fix and the
complete verification after the final fix.

- [ ] **Step 3: Run the current-Mac one-command redeployment**

Record the two AVD names, data-directory identities, installed APK digests, session-file
presence, and Docker named-volume identities using read-only commands. Then run from the
feature worktree:

```bash
scripts/deploy-macos-one-click.sh \
  --mode auto \
  --project-root "$(pwd)" \
  --env-file /Users/wilson/tonghuashun/.env
```

Expected: existing mode, no install/reinstall command, and `READY`. Verify without printing
the Token:

```bash
launchctl print "gui/$UID/com.ths.device-lifecycle"
test "$(stat -f '%Lp' "$HOME/.config/ths-device-lifecycle.env")" = "600"
```

- [ ] **Step 4: Verify image assets and broker reachability from OrbStack**

From the API container, verify `/opt/ths/assets/manifest.json`, APK digest, executable Frida
binary, and broker `/v1/devices`. Use the container environment for authentication but print
only `assets=verified`, HTTP status, and safe role/state values; never print headers or env.

Expected: assets verified, HTTP 200, and two role entries.

- [ ] **Step 5: Verify deployed service and stored state preservation**

The one-command script already ran the canonical Compose build. Wait until API and Redis are
healthy and verify `/openapi.json`, `/market`, and the admin page return 200. Compare the
recorded AVD/data/session/volume identities from Step 3 and assert none changed or disappeared.

Confirm the deployed container environment has lifecycle URL/timeout and direct transports,
but do not print any secret value.

- [ ] **Step 6: Run core real lifecycle acceptance**

In the administrator page:

1. acquire device control;
2. pause/wait until no running task remains;
3. confirm core shutdown and wait for `STOPPED`;
4. submit `601872` with `include_long_capture=false` while core is stopped and assert 8/8,
   three intraday curves, and three fund periods;
5. confirm core start and wait for `RUNNING`, App online, Frida online, 1080×1920/480 display;
6. visually verify the existing core account remains logged in without entering credentials.

- [ ] **Step 7: Run fund real lifecycle acceptance**

Keeping the queue paused and operating sequentially:

1. confirm fund shutdown and wait for `STOPPED`;
2. confirm fund start and wait for `RUNNING`, App online, and Frida online;
3. visually verify the same protected fund account remains logged in;
4. do not tap, swipe, search, change account, or navigate the fund App;
5. submit another data-only `601872` task and assert complete core/fund sources.

- [ ] **Step 8: Security and persistence scan**

Assert API responses, task records, API logs, broker logs, and admin DOM contain no lifecycle
Token, Cookie, auth packet, request packet, serial, AVD name, command, stdout, or stderr.
Confirm the image contains no `.env`, session bundle, AVD directory, Redis/market/admin data,
capture, or logs. Confirm Redis, market, admin/session, capture volumes and both AVD data
directories remain.

Run the complete provisioning test suite with fake commands and record that fresh/partial Mac
paths pass. Unless a separate clean Mac is actually available, report first-time provisioning
as automated-path verified rather than claiming a real clean-Mac acceptance.

- [ ] **Step 9: Commit final verified adjustments**

If Task 9 changed tracked documentation or fixes, stage only those exact files and commit:

```bash
git commit -m "fix: complete device lifecycle acceptance"
```

If no tracked files changed, do not create an empty commit.

### Task 10: Final Branch Review and Integration Handoff

**Files:**
- No planned code changes.

**Interfaces:**
- Consumes: verified feature branch and acceptance evidence.
- Produces: a merge-ready branch and exact release summary.

- [ ] **Step 1: Verify branch cleanliness and history**

Run:

```bash
git status --short --branch
git log --oneline --decorate "$(git merge-base main HEAD)"..HEAD
```

Expected: no tracked or untracked generated files in the worktree; only intentional commits.

- [ ] **Step 2: Report merge-ready evidence**

Report:

- branch and HEAD SHA;
- automated test counts and build result;
- image asset manifest/digest verification and final image size;
- existing-Mac one-command deployment result and proof that neither App was reinstalled;
- fresh/partial-Mac provisioning test result and whether a real clean Mac was available;
- LaunchAgent and broker health;
- real core/fund stop-start results;
- login/data preservation evidence;
- post-restart direct task timing and completeness;
- sensitive-information scan result;
- remaining maintenance risk: Android/AVD/App upgrades may require updating fixed startup
  assumptions and pinned image assets, but never automatic replacement of existing AVDs or
  automatic reinstall into an existing logged-in device.
