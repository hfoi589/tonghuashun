# Admin Dual-Device Lifecycle Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add authenticated administrator controls that safely shut down either macOS Android emulator or start it and open Tonghuashun without clearing data, changing accounts, or exposing arbitrary host commands.

**Architecture:** A token-authenticated Python standard-library service runs as a macOS LaunchAgent on `127.0.0.1:18765` and owns the fixed AVD/ADB command whitelist. FastAPI proxies only fixed role/action requests through the existing admin-session, CSRF, device-lock, and paused-queue boundary; the React admin page polls lifecycle state and renders per-device controls and confirmations.

**Tech Stack:** Python 3 standard library host service, FastAPI/Pydantic, existing Redis/InMemory task stores, React 19, TypeScript, Vitest/Testing Library, shell/launchd, OrbStack Docker Compose.

**Spec:** `docs/superpowers/specs/2026-08-27-admin-device-lifecycle-controls-design.md`

## Global Constraints

- The only allowed lifecycle actions are `start_and_launch_app` and `shutdown`.
- Both `core_metrics` and `main_fund_flow` are explicitly authorized for those two actions.
- Never issue account logout/switching, AVD clone/create/delete/reset, App install/uninstall, `pm clear`, `wipe-data`, `reboot -p`, or `am force-stop`.
- `main_fund_flow` start may open `LogoEmptyActivity` but may not tap, swipe, search, or navigate further.
- The browser and FastAPI callers never supply AVD names, serials, ports, activities, executable paths, or shell arguments.
- Host and API responses/logs expose only role, action, lifecycle state, operation id, timestamps, and fixed error codes.
- Lifecycle controls require an authenticated admin session, valid CSRF, current-session device lock, paused queue, and no running device task.
- Redis, market, admin, session, capture, emulator, and AVD data must be preserved through deployment and acceptance.
- Docker deployment continues to use OrbStack, `.env`, `deploy/macos.env`, port 8001, and no Caddy.

## File Map

- Create `scripts/macos-device-lifecycle.py`: host-side fixed-role lifecycle manager and loopback HTTP server.
- Create `scripts/install-macos-device-lifecycle.sh`: secure config, LaunchAgent, and stable bridge watcher installation.
- Create `tests/test_macos_device_lifecycle.py`: host manager, HTTP authentication, command whitelist, and installer-contract tests.
- Create `level2_service/device_lifecycle.py`: sanitized FastAPI-to-host client and lifecycle types.
- Create `tests/test_device_lifecycle.py`: client parsing, timeout, and error-redaction tests.
- Create `tests/test_device_lifecycle_api.py`: admin auth/CSRF/lock/busy/action API tests.
- Modify `level2_service/queue.py`: expose a conservative `has_running_task()` store predicate.
- Modify `level2_service/api.py`: inject lifecycle client, extend device response, add fixed action endpoint.
- Modify `level2_service/main.py`: parse lifecycle configuration and wire the production client.
- Modify `frontend/src/api.ts`: lifecycle types, device list, and action requests.
- Modify `frontend/src/AdminPage.tsx`: device polling, two per-device buttons, pending/error state, accessible confirmation dialog.
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
are disabled before lock acquisition.

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
not overwrite each other.

- [ ] **Step 4: Write failing confirmation, pending, and routing tests**

Test:

- clicking core shutdown opens `role="alertdialog"` and initially focuses Cancel;
- Cancel and Escape issue no request and restore focus;
- Tab/Shift+Tab remain inside the dialog;
- Confirm sends core + `shutdown` + CSRF;
- fund start sends fund + `start_and_launch_app`;
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
  onLifecycleAction?: (role: DeviceRole, action: DeviceLifecycleAction) => void
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
- Consumes: all Task 1-5 behavior.
- Produces: operator installation, authorization boundary, rollback, and acceptance documentation.

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
- README documents first-time installation and safe operation.

- [ ] **Step 2: Run documentation contract tests and verify failure**

Run:

```bash
/Users/wilson/tonghuashun/.venv/bin/python -m pytest -q \
  tests/test_deployment.py tests/test_deploy_configuration.py
```

Expected: FAIL on missing lifecycle documentation assertions.

- [ ] **Step 3: Update rules and operator documentation**

Document the exact install command:

```bash
scripts/install-macos-device-lifecycle.sh \
  --project-root /Users/wilson/tonghuashun \
  --env-file /Users/wilson/tonghuashun/.env
```

Document that operators must acquire the device lock, wait for running tasks to finish, use
one device action at a time, release the lock, and explicitly resume the queue. Record all
fixed error codes and the rollback sequence from the spec.

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

### Task 7: Full Verification, Installation, Deployment, and Real Dual-Device Acceptance

**Files:**
- Modify only if verification finds a defect in Task 1-6 files.
- Do not add generated LaunchAgent, host config, Token, `.env`, logs, or `graphify-out/` to Git.

**Interfaces:**
- Consumes: complete feature branch.
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

- [ ] **Step 3: Install the macOS lifecycle service**

Run from the feature worktree; the installer copies runtime files into the stable
`~/.local/lib/ths-device-lifecycle/` directory before loading LaunchAgent:

```bash
scripts/install-macos-device-lifecycle.sh \
  --project-root "$(pwd)" \
  --env-file /Users/wilson/tonghuashun/.env
```

Verify without printing the Token:

```bash
launchctl print "gui/$UID/com.ths.device-lifecycle"
test "$(stat -f '%Lp' "$HOME/.config/ths-device-lifecycle.env")" = "600"
```

- [ ] **Step 4: Verify broker reachability from OrbStack**

After Compose receives the Token, call the broker from the API container using Python and the
container environment. Print only status code and safe device states; never print headers or
environment values.

Expected: HTTP 200 and two role entries.

- [ ] **Step 5: Rebuild with the standard deployment command**

From the stable project checkout containing the final code:

```bash
docker --context orbstack compose \
  --env-file .env \
  --env-file deploy/macos.env \
  -f deploy/compose.yml up -d --build
```

Preserve Redis and all named volumes. Wait until API and Redis are healthy and verify
`/openapi.json` and the admin page return 200.

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
Confirm Redis, market, admin/session, capture volumes and both AVD data directories remain.

- [ ] **Step 9: Commit final verified adjustments**

If Task 7 changed tracked documentation or fixes, stage only those exact files and commit:

```bash
git commit -m "fix: complete device lifecycle acceptance"
```

If no tracked files changed, do not create an empty commit.

### Task 8: Final Branch Review and Integration Handoff

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
- LaunchAgent and broker health;
- real core/fund stop-start results;
- login/data preservation evidence;
- post-restart direct task timing and completeness;
- sensitive-information scan result;
- remaining maintenance risk: Android/AVD/App upgrades may require updating fixed startup
  assumptions, but never automatic AVD recreation or App reinstall.
