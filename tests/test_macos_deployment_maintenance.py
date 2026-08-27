from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
from threading import Thread

import pytest

from level2_service.models import TaskRecord
from level2_service.queue import RedisStreamsStore
from tests.test_redis_store import FakeRedis

from tests.test_macos_one_click_deploy import (
    ROOT,
    FakeDataOnlyAcceptance,
    FakeFileSystem,
    _completed,
    _load_macos_deploy,
    existing_mac_runner,
    make_orchestrator,
)


OWNER = "deployment-owner-token-abcdefghijklmnopqrstuvwxyz"


@contextmanager
def serve(handler: type[BaseHTTPRequestHandler]):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return None


def test_old_api_admin_client_logs_in_and_acquires_lock_without_exposing_password() -> None:
    """The pre-feature API is paused through its existing cookie/CSRF boundary."""
    module = _load_macos_deploy()
    observed: list[tuple[str, str, str | None, str | None]] = []

    class Handler(QuietHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            observed.append(
                (
                    self.path,
                    body,
                    self.headers.get("Cookie"),
                    self.headers.get("X-CSRF-Token"),
                )
            )
            if self.path == "/api/admin/session":
                self.send_response(204)
                self.send_header(
                    "Set-Cookie",
                    "ths_admin_session=safe-session; HttpOnly; SameSite=strict",
                )
                self.send_header(
                    "Set-Cookie", "ths_csrf=safe-csrf; SameSite=strict"
                )
                self.end_headers()
                return
            if self.path == "/api/admin/lock/acquire":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"locked":true}')
                return
            self.send_response(404)
            self.end_headers()

    with serve(Handler) as server:
        client = module.LoopbackAdminMaintenanceClient(
            base_url=f"http://127.0.0.1:{server.server_port}"
        )
        client.acquire_device_lock("private-admin-password")

    assert json.loads(observed[0][1]) == {"password": "private-admin-password"}
    assert observed[1] == (
        "/api/admin/lock/acquire",
        "",
        "ths_admin_session=safe-session; ths_csrf=safe-csrf",
        "safe-csrf",
    )
    assert "private-admin-password" not in repr(client)
    assert "safe-session" not in repr(client)
    assert "safe-csrf" not in repr(client)


def test_old_api_admin_client_rejects_redirect_and_sanitizes_secret_context() -> None:
    """Login credentials cannot follow a redirect or appear in a deployment error."""
    module = _load_macos_deploy()
    redirected: list[str] = []

    class Target(QuietHandler):
        def do_POST(self) -> None:
            redirected.append(self.path)
            self.send_response(204)
            self.end_headers()

    with serve(Target) as target:
        location = f"http://127.0.0.1:{target.server_port}/stolen"

        class Source(QuietHandler):
            def do_POST(self) -> None:
                self.send_response(302)
                self.send_header("Location", location)
                self.end_headers()

        with serve(Source) as source:
            client = module.LoopbackAdminMaintenanceClient(
                base_url=f"http://127.0.0.1:{source.server_port}"
            )
            with pytest.raises(module.DeploymentError) as caught:
                client.acquire_device_lock("private-admin-password")

    assert caught.value.error_code == "DEPLOYMENT_ADMIN_LOCK_FAILED"
    assert caught.value.__cause__ is None
    assert "private-admin-password" not in repr(caught.value)
    assert redirected == []


class LeaseRunner:
    def __init__(self, *, idle: bool = True, acquire: bool = True) -> None:
        self.idle = idle
        self.acquire = acquire
        self.calls: list[tuple[str, ...]] = []
        self.inputs: list[bytes | None] = []

    def run(
        self,
        args: tuple[str, ...],
        timeout: float,
        input_data: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        del timeout
        self.calls.append(args)
        self.inputs.append(input_data)
        rendered = " ".join(args)
        if "THS_DEPLOYMENT_IDLE_CHECK" in rendered:
            return _completed(args, stdout=b"IDLE\n" if self.idle else b"BUSY\n")
        if "THS_HOST_ACQUIRE_DEPLOYMENT_LEASE" in rendered:
            return _completed(args, stdout=b"1\n" if self.acquire else b"0\n")
        if "THS_HOST_RENEW_DEPLOYMENT_LEASE" in rendered:
            return _completed(args, stdout=b"1\n")
        if "THS_HOST_RELEASE_DEPLOYMENT_LEASE" in rendered:
            return _completed(args, stdout=b"1\n")
        return _completed(args)


class RedisEvalRunner:
    """Execute the host's redis-cli EVAL contract against the deterministic Redis model."""

    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.calls: list[tuple[str, ...]] = []
        self.inputs: list[bytes | None] = []

    def run(
        self,
        args: tuple[str, ...],
        timeout: float,
        input_data: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        del timeout
        self.calls.append(args)
        self.inputs.append(input_data)
        eval_index = args.index("EVAL")
        script = args[eval_index + 1]
        key_count = int(args[eval_index + 2])
        tail = list(args[eval_index + 3 :])
        keys = tail[:key_count]
        arguments = tail[key_count:]
        if input_data is not None:
            arguments.append(input_data.decode("ascii"))
        result = self.redis.eval(script, key_count, *keys, *arguments)
        if result is False or result is None:
            output = b"\n"
        elif isinstance(result, bytes):
            output = result + b"\n"
        else:
            output = f"{result}\n".encode("utf-8")
        return _completed(args, stdout=output)


class FakeAdminMaintenance:
    def __init__(self) -> None:
        self.passwords: list[str] = []

    def acquire_device_lock(self, password: str) -> None:
        self.passwords.append(password)


class MemoryOwnerState:
    def __init__(self) -> None:
        self.owner: str | None = None

    def load(self) -> str | None:
        return self.owner

    def store(self, owner: str) -> None:
        self.owner = owner

    def delete(self) -> None:
        self.owner = None


def compose_prefix() -> tuple[str, ...]:
    return (
        "docker",
        "--context",
        "orbstack",
        "compose",
        "--project-name",
        "ths-level2",
        "--env-file",
        ".env",
        "--env-file",
        "deploy/macos.env",
        "-f",
        "deploy/compose.yml",
    )


def test_host_maintenance_waits_idle_and_passes_owner_only_over_stdin() -> None:
    """The lease owner never appears in argv, output, or a Compose environment."""
    module = _load_macos_deploy()
    runner = LeaseRunner()
    admin = FakeAdminMaintenance()
    state = MemoryOwnerState()
    maintenance = module.HostDeploymentMaintenance(
        runner,
        compose_prefix,
        admin,
        state,
        password_reader=lambda: "private-admin-password",
        owner_factory=lambda: OWNER,
        poll_interval_seconds=0.0,
    )

    maintenance.prepare()
    maintenance.renew()
    maintenance.release()

    assert admin.passwords == ["private-admin-password"]
    assert state.owner is None
    rendered = "\n".join(" ".join(call) for call in runner.calls)
    assert OWNER not in rendered
    assert "private-admin-password" not in rendered
    owner_inputs = [item for item in runner.inputs if item]
    assert owner_inputs == [OWNER.encode(), OWNER.encode(), OWNER.encode()]


def test_host_release_script_cleans_only_the_bound_maintenance_task() -> None:
    """Host rollback/release must atomically remove its fixed task, not just the lease."""
    module = _load_macos_deploy()
    runner = LeaseRunner()
    maintenance = module.HostDeploymentMaintenance(
        runner,
        compose_prefix,
        FakeAdminMaintenance(),
        MemoryOwnerState(),
        password_reader=lambda: "private-admin-password",
        owner_factory=lambda: OWNER,
        poll_interval_seconds=0.0,
    )

    maintenance.prepare()
    maintenance.release()

    release_call = next(
        call
        for call in runner.calls
        if "THS_HOST_RELEASE_DEPLOYMENT_LEASE" in " ".join(call)
    )
    assert release_call[release_call.index("EVAL") + 2] == "7"
    key_start = release_call.index("EVAL") + 3
    assert release_call[key_start:key_start + 7] == (
        "ths:jobs:deployment-maintenance",
        "ths:jobs:pending",
        "ths:jobs:task:",
        "ths:jobs:tasks",
        "ths:jobs:events",
        "ths:jobs:symbols",
        "ths:jobs:aliases",
    )
    digest_index = key_start + 7
    assert release_call[digest_index] == hashlib.sha256(OWNER.encode()).hexdigest()


def make_host_release_maintenance(redis: FakeRedis, owner: str = OWNER):
    module = _load_macos_deploy()
    state = MemoryOwnerState()
    state.store(owner)
    runner = RedisEvalRunner(redis)
    maintenance = module.HostDeploymentMaintenance(
        runner,
        compose_prefix,
        FakeAdminMaintenance(),
        state,
        password_reader=lambda: "private-admin-password",
        owner_factory=lambda: owner,
        poll_interval_seconds=0.0,
    )
    maintenance._owner_token = owner
    return module, maintenance


def test_host_release_repairs_alias_and_symbol_index_to_ordinary_canonical() -> None:
    """Rollback cleanup has the same public-history repair as the API store release."""
    redis = FakeRedis()
    store = RedisStreamsStore(redis)
    ordinary = TaskRecord(
        task_id="host-ordinary",
        symbol="601872",
        include_long_capture=False,
    )
    store.enqueue(ordinary)
    assert store.acquire_deployment_lease(OWNER, 60.0)
    bound = store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="host-maintenance",
            symbol="601872",
            include_long_capture=False,
        ),
    )
    assert bound is not None
    redis.hset(store._alias_key, "host-public-alias", bound.task_id)
    redis.hset(store._symbol_index_key, "601872", bound.task_id)
    _module, maintenance = make_host_release_maintenance(redis)

    maintenance.release()

    assert store.get(bound.task_id) is None
    assert store.resolve_task_id("host-public-alias") == ordinary.task_id
    assert redis.hget(store._symbol_index_key, "601872") == ordinary.task_id
    assert store.submit_or_refresh(
        TaskRecord(task_id="host-duplicate", symbol="601872")
    ).task_id == ordinary.task_id


@pytest.mark.parametrize(
    "corruption",
    ["namespace", "owner_digest", "task_id", "symbol", "capture"],
)
def test_host_release_fails_closed_on_maintenance_marker_corruption(
    corruption: str,
) -> None:
    """The host proves the namespace and SHA-256 owner binding before deletion."""
    redis = FakeRedis()
    store = RedisStreamsStore(redis)
    assert store.acquire_deployment_lease(OWNER, 60.0)
    bound = store.bind_deployment_acceptance(
        OWNER,
        TaskRecord(
            task_id="host-corrupt",
            symbol="601872",
            include_long_capture=False,
        ),
    )
    assert bound is not None
    raw = json.loads(redis.get(store._key(bound.task_id)))
    if corruption == "namespace":
        raw["maintenance"]["namespace"] = "ordinary"
    elif corruption == "owner_digest":
        raw["maintenance"]["owner_digest"] = "0" * 64
    elif corruption == "task_id":
        raw["task_id"] = "forged-host-id"
    elif corruption == "symbol":
        raw["symbol"] = "000001"
    else:
        raw["include_long_capture"] = True
    redis.set(store._key(bound.task_id), json.dumps(raw))
    module, maintenance = make_host_release_maintenance(redis)

    with pytest.raises(module.DeploymentError) as caught:
        maintenance.release()

    assert caught.value.error_code == "DEPLOYMENT_MAINTENANCE_RELEASE_FAILED"
    assert redis.get(store._key(bound.task_id)) is not None
    assert store.deployment_lease_status() is not None


def test_host_release_preserves_legacy_unmarked_bound_task_compatibility() -> None:
    """A pre-marker task named by the owner lease remains safely releasable."""
    redis = FakeRedis()
    store = RedisStreamsStore(redis)
    assert store.acquire_deployment_lease(OWNER, 60.0)
    legacy = TaskRecord(
        task_id="host-legacy",
        symbol="601872",
        include_long_capture=False,
    )
    store.enqueue(legacy)
    lease = json.loads(redis.get(store._deployment_lease_key))
    lease["bound_task_id"] = legacy.task_id
    redis.set(store._deployment_lease_key, json.dumps(lease), keepttl=True)
    _module, maintenance = make_host_release_maintenance(redis)

    maintenance.release()

    assert redis.get(store._key(legacy.task_id)) is None
    assert store.deployment_lease_status() is None


def test_host_maintenance_rejects_busy_or_conflicting_lease_without_mutation() -> None:
    """The final Redis acquisition check closes the old-runner race."""
    module = _load_macos_deploy()
    for runner, expected in (
        (LeaseRunner(idle=False), "DEPLOYMENT_TASKS_ACTIVE"),
        (LeaseRunner(acquire=False), "DEPLOYMENT_MAINTENANCE_BUSY"),
    ):
        state = MemoryOwnerState()
        maintenance = module.HostDeploymentMaintenance(
            runner,
            compose_prefix,
            FakeAdminMaintenance(),
            state,
            password_reader=lambda: "private-admin-password",
            owner_factory=lambda: OWNER,
            idle_timeout_seconds=0.0,
            poll_interval_seconds=0.0,
        )

        with pytest.raises(module.DeploymentError) as caught:
            maintenance.prepare()

        assert caught.value.error_code == expected
        assert state.owner is None


class FakeDeploymentMaintenance:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.owner_token = OWNER

    def prepare(self) -> None:
        self.events.append("maintenance-prepare")

    def renew(self) -> None:
        self.events.append("maintenance-renew")

    def release(self) -> None:
        self.events.append("maintenance-release")


class EventAcceptance(FakeDataOnlyAcceptance):
    def __init__(self, events: list[str], error: Exception | None = None) -> None:
        super().__init__(error)
        self.events = events

    def verify(self) -> None:
        self.events.append("strict-acceptance")
        super().verify()


def test_existing_deployment_keeps_lease_across_replacement_and_acceptance() -> None:
    """Release occurs only after new-service health, sessions, and strict task validation."""
    events: list[str] = []
    filesystem = FakeFileSystem()
    runner = existing_mac_runner(
        filesystem=filesystem,
        sessions_ready=True,
        events=events,
    )
    maintenance = FakeDeploymentMaintenance(events)
    acceptance = EventAcceptance(events)

    result = make_orchestrator(
        runner,
        filesystem=filesystem,
        acceptance=acceptance,
        deployment_maintenance=maintenance,
    ).deploy_existing()

    assert result.state == "READY"
    assert events.index("maintenance-prepare") < next(
        index
        for index, call in enumerate(runner.calls)
        if call[-3:] == ("up", "-d", "--build")
    )
    assert events.index("strict-acceptance") < events.index("maintenance-release")
    assert events.count("maintenance-renew") >= 2


def test_acceptance_failure_retains_lease_and_returns_safe_rollback_instructions() -> None:
    """A failed post-replacement probe never silently resumes ordinary queue work."""
    module = _load_macos_deploy()
    events: list[str] = []
    maintenance = FakeDeploymentMaintenance(events)
    acceptance = EventAcceptance(
        events, module.DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
    )
    runner = existing_mac_runner(sessions_ready=True, events=events)

    with pytest.raises(module.DeploymentError) as caught:
        make_orchestrator(
            runner,
            acceptance=acceptance,
            deployment_maintenance=maintenance,
        ).deploy_existing()

    assert caught.value.error_code == "DATA_ONLY_ACCEPTANCE_FAILED"
    assert "maintenance-release" not in events
    assert caught.value.instructions == (
        "Deployment maintenance remains active.",
        "Fix the reported error and rerun the same deployment command, or run the explicit maintenance rollback command.",
    )
