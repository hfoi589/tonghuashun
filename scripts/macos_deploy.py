#!/usr/bin/env python3
"""Fail-closed one-command deployment for the two fixed macOS Android roles."""

from __future__ import annotations

import argparse
import base64
import ctypes
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fnmatch
import getpass
import hmac
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
from tempfile import NamedTemporaryFile
import time
from typing import Callable, Mapping, Protocol
from urllib.request import Request


PROJECT_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_IMPORT_ROOT))
SCRIPT_IMPORT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_IMPORT_ROOT))

from level2_service.safe_http import SafeHttpError, SafeHttpStatusError, SafeHttpTransport
from macos_device_identity import (
    DarwinProcessExecutableResolver,
    FixedAvdIdentityVerifier,
    FixedAvdPresence,
    IdentityVerificationError,
    ProcessExecutableResolver,
)


APK_SHA256 = "2554490aa3f5e2df17ac0a711311f3f85ee3130008af9bb4ab12510b3d6e971e"
FRIDA_SHA256 = "36ec3d7474b1ac69c4e7ec985612fae771d37ffb71cb94858bc6978f69f5e581"
FRIDA_BINARY_SHA256 = "4eebf1fbc66ff54aba9a9124c2ef8b32b566616388c60e2caa65148a529d826a"
ANDROID_SYSTEM_IMAGE = "system-images;android-33;google_apis;arm64-v8a"
IMAGE_NAME = "ths-level2-api:local"
COMPOSE_PROJECT_NAME = "ths-level2"
MINIMUM_FREE_BYTES = 30 * 1024**3
PACKAGE_NAME = "com.hexin.plat.android"
FIXED_ROLES = {
    "core_metrics": ("THS_CORE_33_ARM64", "emulator-5556"),
    "main_fund_flow": ("THS_API_33_ARM64", "emulator-5554"),
}
FIXED_EMULATOR_PORTS = {
    "core_metrics": 5556,
    "main_fund_flow": 5554,
}
REQUIRED_COMMANDS = (
    "adb",
    "avdmanager",
    "docker",
    "emulator",
    "java",
    "sdkmanager",
)
REQUIRED_ROOT_ENV_KEYS = (
    "ADMIN_PASSWORD_HASH",
    "ADMIN_SESSION_SECRET",
    "THS_SESSION_ENCRYPTION_KEY",
    "THS_DEVICE_LIFECYCLE_TOKEN",
)
REQUIRED_MACOS_ENV = {
    "CORE_ADB_SERIAL": "emulator-5556",
    "CORE_FRIDA_SERVER_ENDPOINT": "host.docker.internal:27043",
    "FUND_ADB_SERIAL": "emulator-5554",
    "FUND_FRIDA_SERVER_ENDPOINT": "host.docker.internal:27042",
    "THS_DEVICE_LIFECYCLE_URL": "http://host.docker.internal:18765",
}
ROOT_ONLY_COMPOSE_KEYS = frozenset(REQUIRED_ROOT_ENV_KEYS)
SANITIZED_AMBIENT_KEYS = ROOT_ONLY_COMPOSE_KEYS | {
    "THS_DEVICE_LIFECYCLE_URL",
}
_SAFE_APK_PATH = re.compile(r"/data/app/[A-Za-z0-9._~+=/-]+/base\.apk")
_SAFE_OPERATION_ID = re.compile(r"[A-Za-z0-9_-]{1,256}")
_SAFE_ADB_SERIAL = re.compile(r"[A-Za-z0-9._:-]+")
_SAFE_ADB_STATES = frozenset(
    {"bootloader", "device", "offline", "recovery", "sideload", "unauthorized"}
)
_ACCEPTANCE_SYMBOL = "601872"
_ACCEPTANCE_REQUIRED_VALUES = (
    "stock_name",
    "current_price",
    "change_percent",
    "turnover_rate",
    "large_order_net",
    "large_order_amount",
    "retail_count",
    "macdfs",
)
_ACCEPTANCE_CAPTURE_KINDS = frozenset(
    {"LARGE_ORDER_NET", "LARGE_ORDER_AMOUNT", "RETAIL_COUNT"}
)
_ACCEPTANCE_INTRADAY_FIELDS = (
    "large_order_net",
    "large_order_amount",
    "retail_count",
)
_ACCEPTANCE_FUND_PERIODS = ("today", "three_day", "five_day")
_ACCEPTANCE_FUND_FIELDS = (
    "main_net_inflow",
    "main_visible_inflow",
    "main_hidden_inflow",
    "retail_inflow",
)
PROVISIONING_JOURNAL_PATH = (
    Path.home() / ".config/ths-device-provisioning.json"
)
_PROVISIONING_STEPS = (
    "PENDING_CREATE",
    "AVD_CREATED",
    "APK_VERIFIED",
    "FRIDA_READY",
    "LOGIN_REQUIRED",
    "ACCEPTANCE_PENDING",
)
SESSION_READINESS_PROBE = """\
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from level2_service.app_sessions import EncryptedFileSessionProvider

ready = False
updated_at = {}
try:
    root = Path(os.environ["THS_SESSION_ROOT"])
    key = os.environ["THS_SESSION_ENCRYPTION_KEY"]
    roles = ("core_metrics", "main_fund_flow")
    files_safe = True
    for role in roles:
        path = root / f"{role}.session"
        metadata = path.lstat()
        files_safe = files_safe and (
            stat.S_ISREG(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and stat.S_IMODE(metadata.st_mode) == 0o600
            and metadata.st_uid == os.getuid()
            and metadata.st_size > 0
        )
    if files_safe:
        provider = EncryptedFileSessionProvider(root, key)
        statuses = [provider.status(role).as_public() for role in roles]
        updated_at = {
            role: status.get("updated_at")
            for role, status in zip(roles, statuses, strict=True)
        }
        ready = all(
            status.get("role") == role
            and status.get("state") == "READY"
            and status.get("error_code") is None
            and isinstance(status.get("updated_at"), str)
            and datetime.fromisoformat(status["updated_at"]).tzinfo is not None
            for role, status in zip(roles, statuses, strict=True)
        )
except Exception:
    ready = False
print(json.dumps({"ready": ready, "updated_at": updated_at}, separators=(",", ":")))
"""
_FIRST_TIME_LOGIN_INSTRUCTIONS = (
    "Open http://127.0.0.1:8001/#admin.",
    "Manually log in and complete verification for each newly created role.",
    "Click the matching role's session refresh.",
    "Rerun scripts/provision-macos-from-image.sh.",
)
_MAINTENANCE_RETAINED_INSTRUCTIONS = (
    "Deployment maintenance remains active.",
    "Fix the reported error and rerun the same deployment command, or run the explicit maintenance rollback command.",
)


@dataclass(frozen=True)
class DeploymentResult:
    mode: str
    state: str
    error_code: str | None = None
    instructions: tuple[str, ...] = ()


class DeploymentError(RuntimeError):
    def __init__(
        self,
        error_code: str,
        *,
        instructions: tuple[str, ...] = (),
    ):
        super().__init__(error_code)
        self.error_code = error_code
        self.instructions = instructions


class CommandRunner(Protocol):
    def run(
        self,
        args: tuple[str, ...],
        timeout: float,
        input_data: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]: ...


class LifecycleBroker(Protocol):
    def device_states(self) -> Mapping[str, str]: ...

    def start_and_launch_app(self, role: str) -> str: ...

    def wait_for_state(
        self, operation_id: str, expected_state: str, timeout_seconds: float
    ) -> None: ...


class DataOnlyAcceptance(Protocol):
    def verify(self) -> None: ...


class DeploymentMaintenance(Protocol):
    @property
    def owner_token(self) -> str: ...

    def prepare(self) -> None: ...

    def renew(self) -> None: ...

    def release(self) -> None: ...

    def rollback(self) -> None: ...


class AdminMaintenanceClient(Protocol):
    def acquire_device_lock(self, password: str) -> None: ...


class DeploymentOwnerState(Protocol):
    def load(self) -> str | None: ...

    def store(self, owner: str) -> None: ...

    def delete(self) -> None: ...


class ProvisioningJournalStore(Protocol):
    def load(self) -> dict[str, str]: ...

    def record_initial_missing(
        self, roles: frozenset[str]
    ) -> dict[str, str]: ...

    def set_step(
        self,
        role: str,
        step: str,
        *,
        created_at: datetime | None = None,
    ) -> None: ...

    def created_at(self, role: str) -> datetime | None: ...

    def complete(self, role: str) -> None: ...


class FileSystem(Protocol):
    def exists(self, path: Path) -> bool: ...

    def read_text(self, path: Path) -> str: ...

    def mode(self, path: Path) -> int: ...

    def which(self, command: str) -> str | None: ...

    def free_bytes(self, path: Path) -> int: ...

    def is_secure_owner_file(self, path: Path) -> bool: ...


class SubprocessCommandRunner:
    """Run fixed argument vectors in the selected project checkout without a shell."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root
        self._environment = dict(os.environ)
        for key in tuple(self._environment):
            if key.startswith("COMPOSE_"):
                self._environment.pop(key, None)
        for key in SANITIZED_AMBIENT_KEYS:
            self._environment.pop(key, None)

    def run(
        self,
        args: tuple[str, ...],
        timeout: float,
        input_data: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            args,
            cwd=self._project_root,
            env=self._environment,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            input=input_data,
            timeout=timeout,
            check=False,
        )


class PathFileSystem:
    def exists(self, path: Path) -> bool:
        return path.exists()

    def read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def mode(self, path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    def which(self, command: str) -> str | None:
        return shutil.which(command)

    def free_bytes(self, path: Path) -> int:
        candidate = path.expanduser().resolve()
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        return shutil.disk_usage(candidate).free

    def is_secure_owner_file(self, path: Path) -> bool:
        try:
            metadata = os.lstat(path)
        except OSError:
            return False
        return (
            stat.S_ISREG(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and stat.S_IMODE(metadata.st_mode) == 0o600
            and metadata.st_uid == os.getuid()
        )


class SecureDeploymentOwnerState:
    """Owner-only recovery state for compare-owner renewal and rollback."""

    _MAX_BYTES = 512

    def __init__(
        self,
        path: Path = Path.home()
        / ".config/ths-deployment-maintenance-owner",
    ) -> None:
        self.path = path.expanduser().resolve()

    def load(self) -> str | None:
        try:
            metadata = os.lstat(self.path)
        except FileNotFoundError:
            return None
        except OSError:
            raise DeploymentError("DEPLOYMENT_MAINTENANCE_STATE_INVALID") from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_size <= 0
            or metadata.st_size > self._MAX_BYTES
        ):
            raise DeploymentError("DEPLOYMENT_MAINTENANCE_STATE_INVALID")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                current = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(current.st_mode)
                    or stat.S_IMODE(current.st_mode) != 0o600
                    or current.st_uid != os.getuid()
                    or current.st_size != metadata.st_size
                ):
                    raise DeploymentError(
                        "DEPLOYMENT_MAINTENANCE_STATE_INVALID"
                    )
                owner = handle.read(self._MAX_BYTES + 1).decode("ascii").strip()
        except DeploymentError:
            raise
        except Exception:
            raise DeploymentError("DEPLOYMENT_MAINTENANCE_STATE_INVALID") from None
        if _SAFE_OPERATION_ID.fullmatch(owner) is None or len(owner) < 32:
            raise DeploymentError("DEPLOYMENT_MAINTENANCE_STATE_INVALID")
        return owner

    def store(self, owner: str) -> None:
        if _SAFE_OPERATION_ID.fullmatch(owner) is None or len(owner) < 32:
            raise DeploymentError("DEPLOYMENT_MAINTENANCE_STATE_INVALID")
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with NamedTemporaryFile(
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                handle.write((owner + "\n").encode("ascii"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            os.chmod(self.path, 0o600)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception:
            raise DeploymentError("DEPLOYMENT_MAINTENANCE_STATE_INVALID") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def delete(self) -> None:
        try:
            if self.path.exists() or self.path.is_symlink():
                metadata = os.lstat(self.path)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                ):
                    raise DeploymentError(
                        "DEPLOYMENT_MAINTENANCE_STATE_INVALID"
                    )
                self.path.unlink()
        except DeploymentError:
            raise
        except OSError:
            raise DeploymentError("DEPLOYMENT_MAINTENANCE_STATE_INVALID") from None


class LoopbackAdminMaintenanceClient:
    """Secret-redacting client for the existing admin login and device lock."""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8001",
        timeout_seconds: float = 10.0,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        try:
            self._transport = SafeHttpTransport(
                base_url.rstrip("/"),
                max_body_bytes=64 * 1024,
            )
        except ValueError:
            raise DeploymentError("DEPLOYMENT_ADMIN_LOCK_FAILED") from None
        self._base_url = base_url.rstrip("/")

    def acquire_device_lock(self, password: str) -> None:
        if not isinstance(password, str) or not password:
            raise DeploymentError("DEPLOYMENT_ADMIN_LOCK_FAILED")
        try:
            body = json.dumps(
                {"password": password}, separators=(",", ":")
            ).encode("utf-8")
            login = self._transport.request(
                Request(
                    f"{self._base_url}/api/admin/session",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ),
                self._timeout_seconds,
            )
            if login.status != 204:
                raise DeploymentError("DEPLOYMENT_ADMIN_LOCK_FAILED")
            session, csrf = self._parse_admin_cookies(login.headers)
            lock = self._transport.request(
                Request(
                    f"{self._base_url}/api/admin/lock/acquire",
                    data=b"",
                    headers={
                        "Cookie": (
                            f"ths_admin_session={session}; ths_csrf={csrf}"
                        ),
                        "X-CSRF-Token": csrf,
                    },
                    method="POST",
                ),
                self._timeout_seconds,
            )
            if lock.status != 200:
                raise DeploymentError("DEPLOYMENT_ADMIN_LOCK_FAILED")
            document = json.loads(lock.body.decode("utf-8"))
            if document != {"locked": True}:
                raise DeploymentError("DEPLOYMENT_ADMIN_LOCK_FAILED")
        except DeploymentError:
            raise
        except Exception:
            raise DeploymentError("DEPLOYMENT_ADMIN_LOCK_FAILED") from None

    @staticmethod
    def _parse_admin_cookies(headers: object | None) -> tuple[str, str]:
        get_all = getattr(headers, "get_all", None)
        values = get_all("Set-Cookie") if callable(get_all) else None
        if not isinstance(values, list):
            raise DeploymentError("DEPLOYMENT_ADMIN_LOCK_FAILED")
        cookies = SimpleCookie()
        for value in values:
            if isinstance(value, str):
                cookies.load(value)
        try:
            session = cookies["ths_admin_session"].value
            csrf = cookies["ths_csrf"].value
        except KeyError:
            raise DeploymentError("DEPLOYMENT_ADMIN_LOCK_FAILED") from None
        if not all(
            re.fullmatch(r"[A-Za-z0-9_-]{8,256}", value)
            for value in (session, csrf)
        ):
            raise DeploymentError("DEPLOYMENT_ADMIN_LOCK_FAILED")
        return session, csrf


_HOST_IDLE_SCRIPT = """
-- THS_DEPLOYMENT_IDLE_CHECK
for _, task_id in ipairs(redis.call('SMEMBERS', KEYS[2])) do
  local payload = redis.call('GET', KEYS[1] .. task_id)
  if payload then
    local task = cjson.decode(payload)
    if task.status == 'RUNNING' or (task.status == 'PARTIAL' and (not task.completed_at or task.completed_at == cjson.null)) then
      return 'BUSY'
    end
  end
end
return 'IDLE'
"""
_HOST_ACQUIRE_LEASE_SCRIPT = """
-- THS_HOST_ACQUIRE_DEPLOYMENT_LEASE
if redis.call('GET', KEYS[1]) then return 0 end
for _, task_id in ipairs(redis.call('SMEMBERS', KEYS[3])) do
  local payload = redis.call('GET', KEYS[2] .. task_id)
  if payload then
    local task = cjson.decode(payload)
    if task.status == 'RUNNING' or (task.status == 'PARTIAL' and (not task.completed_at or task.completed_at == cjson.null)) then
      return -1
    end
  end
end
local lease = cjson.encode({owner_token = ARGV[2], bound_task_id = cjson.null})
local result = redis.call('SET', KEYS[1], lease, 'NX', 'PX', ARGV[1])
if result then return 1 end
return 0
"""
_HOST_RENEW_LEASE_SCRIPT = """
-- THS_HOST_RENEW_DEPLOYMENT_LEASE
local payload = redis.call('GET', KEYS[1])
if not payload then return 0 end
local lease = cjson.decode(payload)
if lease.owner_token ~= ARGV[2] then return 0 end
return redis.call('PEXPIRE', KEYS[1], ARGV[1])
"""
_HOST_RELEASE_LEASE_SCRIPT = """
-- THS_HOST_RELEASE_DEPLOYMENT_LEASE
local payload = redis.call('GET', KEYS[1])
if not payload then return 0 end
local lease = cjson.decode(payload)
if lease.owner_token ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""


class HostDeploymentMaintenance:
    """Pause the old API and preserve one Redis lease across replacement."""

    _LEASE_KEY = "ths:jobs:deployment-maintenance"
    _TASK_PREFIX = "ths:jobs:task:"
    _TASK_INDEX = "ths:jobs:tasks"

    def __init__(
        self,
        runner: CommandRunner,
        compose_prefix: Callable[[], tuple[str, ...]],
        admin_client: AdminMaintenanceClient,
        owner_state: DeploymentOwnerState,
        *,
        password_reader: Callable[[], str],
        owner_factory: Callable[[], str] = lambda: secrets.token_urlsafe(48),
        ttl_seconds: float = 3600.0,
        idle_timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        if not 0 < ttl_seconds <= 7200:
            raise ValueError("invalid deployment lease TTL")
        self._runner = runner
        self._compose_prefix = compose_prefix
        self._admin_client = admin_client
        self._owner_state = owner_state
        self._password_reader = password_reader
        self._owner_factory = owner_factory
        self._ttl_seconds = ttl_seconds
        self._idle_timeout_seconds = idle_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._owner_token: str | None = None

    @property
    def owner_token(self) -> str:
        if self._owner_token is None:
            raise DeploymentError("DEPLOYMENT_MAINTENANCE_NOT_ACTIVE")
        return self._owner_token

    def prepare(self) -> None:
        try:
            password = self._password_reader()
            self._admin_client.acquire_device_lock(password)
        except DeploymentError:
            raise
        except Exception:
            raise DeploymentError("DEPLOYMENT_ADMIN_LOCK_FAILED") from None
        self._wait_for_idle()
        existing_owner = self._owner_state.load()
        owner = existing_owner or self._owner_factory()
        if _SAFE_OPERATION_ID.fullmatch(owner) is None or len(owner) < 32:
            raise DeploymentError("DEPLOYMENT_MAINTENANCE_STATE_INVALID")
        if existing_owner is not None and self._renew_owner(owner):
            self._owner_token = owner
            return
        self._owner_state.store(owner)
        result = self._lease_eval(
            _HOST_ACQUIRE_LEASE_SCRIPT,
            3,
            (self._LEASE_KEY, self._TASK_PREFIX, self._TASK_INDEX),
            (str(int(self._ttl_seconds * 1000)),),
            owner,
        )
        if result != "1":
            self._owner_state.delete()
            raise DeploymentError("DEPLOYMENT_MAINTENANCE_BUSY")
        self._owner_token = owner

    def renew(self) -> None:
        if not self._renew_owner(self.owner_token):
            raise DeploymentError("DEPLOYMENT_MAINTENANCE_LOST")

    def release(self) -> None:
        result = self._lease_eval(
            _HOST_RELEASE_LEASE_SCRIPT,
            1,
            (self._LEASE_KEY,),
            (),
            self.owner_token,
        )
        if result != "1":
            raise DeploymentError("DEPLOYMENT_MAINTENANCE_RELEASE_FAILED")
        self._owner_state.delete()
        self._owner_token = None

    def rollback(self) -> None:
        """Compare-owner release after reacquiring the admin lock; queue stays paused."""
        try:
            password = self._password_reader()
            self._admin_client.acquire_device_lock(password)
        except DeploymentError:
            raise
        except Exception:
            raise DeploymentError("DEPLOYMENT_ADMIN_LOCK_FAILED") from None
        self._wait_for_idle()
        owner = self._owner_state.load()
        if owner is None:
            raise DeploymentError("DEPLOYMENT_MAINTENANCE_NOT_ACTIVE")
        self._owner_token = owner
        self.release()

    def _renew_owner(self, owner: str) -> bool:
        result = self._lease_eval(
            _HOST_RENEW_LEASE_SCRIPT,
            1,
            (self._LEASE_KEY,),
            (str(int(self._ttl_seconds * 1000)),),
            owner,
        )
        return result == "1"

    def _wait_for_idle(self) -> None:
        deadline = time.monotonic() + self._idle_timeout_seconds
        while True:
            result = self._lease_eval(
                _HOST_IDLE_SCRIPT,
                2,
                (self._TASK_PREFIX, self._TASK_INDEX),
                (),
                None,
            )
            if result == "IDLE":
                return
            if result != "BUSY" or time.monotonic() >= deadline:
                raise DeploymentError("DEPLOYMENT_TASKS_ACTIVE")
            time.sleep(max(0.0, self._poll_interval_seconds))

    def _lease_eval(
        self,
        script: str,
        key_count: int,
        keys: tuple[str, ...],
        arguments: tuple[str, ...],
        owner_input: str | None,
    ) -> str:
        command = self._compose_prefix() + (
            "exec",
            "-T",
            "redis",
            "redis-cli",
            "--raw",
        )
        if owner_input is not None:
            command += ("-x",)
        command += ("EVAL", script, str(key_count), *keys, *arguments)
        try:
            result = self._runner.run(
                command,
                30.0,
                owner_input.encode("ascii") if owner_input is not None else None,
            )
        except Exception:
            raise DeploymentError("DEPLOYMENT_MAINTENANCE_UNAVAILABLE") from None
        if result.returncode != 0:
            raise DeploymentError("DEPLOYMENT_MAINTENANCE_UNAVAILABLE")
        try:
            return result.stdout.decode("utf-8").strip()
        except (AttributeError, UnicodeDecodeError):
            raise DeploymentError("DEPLOYMENT_MAINTENANCE_UNAVAILABLE") from None


class ProvisioningJournal:
    """Atomic, mode-0600 journal for only the fixed provisioning roles."""

    _MAX_BYTES = 16_384

    def __init__(self, path: Path = PROVISIONING_JOURNAL_PATH) -> None:
        expanded = path.expanduser()
        self.path = (
            expanded
            if expanded.is_absolute()
            else Path(os.path.abspath(expanded))
        )

    def load(self) -> dict[str, str]:
        return {
            role: entry["step"]
            for role, entry in self._load_entries().items()
        }

    def created_at(self, role: str) -> datetime | None:
        entry = self._load_entries().get(role)
        return entry["created_at"] if entry is not None else None

    def _load_entries(self) -> dict[str, dict[str, object]]:
        try:
            metadata = os.lstat(self.path)
        except FileNotFoundError:
            return {}
        except OSError:
            raise DeploymentError("PROVISIONING_JOURNAL_INVALID") from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_size <= 0
            or metadata.st_size > self._MAX_BYTES
        ):
            raise DeploymentError("PROVISIONING_JOURNAL_INVALID")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                current = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(current.st_mode)
                    or stat.S_IMODE(current.st_mode) != 0o600
                    or current.st_uid != os.getuid()
                    or current.st_size != metadata.st_size
                ):
                    raise DeploymentError("PROVISIONING_JOURNAL_INVALID")
                raw = handle.read(self._MAX_BYTES + 1)
            document = json.loads(raw.decode("utf-8"))
        except DeploymentError:
            raise
        except Exception:
            raise DeploymentError("PROVISIONING_JOURNAL_INVALID") from None
        if len(raw) > self._MAX_BYTES:
            raise DeploymentError("PROVISIONING_JOURNAL_INVALID")
        return self._validate_document(document)

    def record_initial_missing(
        self, roles: frozenset[str]
    ) -> dict[str, str]:
        if not roles.issubset(FIXED_ROLES):
            raise DeploymentError("PROVISIONING_JOURNAL_INVALID")
        entries = self._load_entries()
        changed = False
        for role in FIXED_ROLES:
            if role in roles and role not in entries:
                entries[role] = {
                    "step": "PENDING_CREATE",
                    "created_at": None,
                }
                changed = True
        if changed:
            self._write(entries)
        return {role: entry["step"] for role, entry in entries.items()}

    def set_step(
        self,
        role: str,
        step: str,
        *,
        created_at: datetime | None = None,
    ) -> None:
        entries = self._load_entries()
        entry = entries.get(role)
        current = entry["step"] if entry is not None else None
        transitions = {
            "PENDING_CREATE": "AVD_CREATED",
            "AVD_CREATED": "APK_VERIFIED",
            "APK_VERIFIED": "FRIDA_READY",
            "FRIDA_READY": "LOGIN_REQUIRED",
            "LOGIN_REQUIRED": "ACCEPTANCE_PENDING",
        }
        if (
            role not in FIXED_ROLES
            or step not in _PROVISIONING_STEPS
            or current is None
            or (step != current and transitions.get(current) != step)
        ):
            raise DeploymentError("PROVISIONING_JOURNAL_INVALID")
        if step == "AVD_CREATED" and (
            current != "AVD_CREATED"
            or (entry is not None and entry.get("created_at") is None)
        ):
            if created_at is None or created_at.tzinfo is None:
                raise DeploymentError("PROVISIONING_JOURNAL_INVALID")
        elif created_at is not None:
            raise DeploymentError("PROVISIONING_JOURNAL_INVALID")
        assert entry is not None
        if step != current:
            entry["step"] = step
            if step == "AVD_CREATED":
                entry["created_at"] = created_at
            self._write(entries)
        elif step == "AVD_CREATED" and entry.get("created_at") is None:
            entry["created_at"] = created_at
            self._write(entries)

    def complete(self, role: str) -> None:
        entries = self._load_entries()
        if entries.get(role, {}).get("step") != "ACCEPTANCE_PENDING":
            raise DeploymentError("PROVISIONING_JOURNAL_INVALID")
        del entries[role]
        self._write(entries)

    @staticmethod
    def _validate_document(
        document: object,
    ) -> dict[str, dict[str, object]]:
        if (
            isinstance(document, dict)
            and document.get("version") == 1
            and set(document) == {"version", "roles"}
            and isinstance(document.get("roles"), dict)
        ):
            migrated: dict[str, dict[str, object]] = {}
            legacy_map = {
                "PENDING_CREATE": "PENDING_CREATE",
                "AVD_CREATED": "AVD_CREATED",
                # Re-run digest verification and bridge repair safely; the
                # phase-aware provisioner never reinstalls an exact APK.
                "ASSETS_PROVISIONED": "AVD_CREATED",
            }
            for role, entry in document["roles"].items():
                if (
                    role not in FIXED_ROLES
                    or not isinstance(entry, dict)
                    or set(entry) != {"avd_name", "step"}
                    or entry.get("avd_name") != FIXED_ROLES[role][0]
                    or entry.get("step") not in legacy_map
                ):
                    raise DeploymentError("PROVISIONING_JOURNAL_INVALID")
                migrated[role] = {
                    "step": legacy_map[entry["step"]],
                    "created_at": None,
                }
            return migrated
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "roles"}
            or document.get("version") != 2
            or not isinstance(document.get("roles"), dict)
        ):
            raise DeploymentError("PROVISIONING_JOURNAL_INVALID")
        raw_roles = document["roles"]
        entries: dict[str, dict[str, object]] = {}
        for role, entry in raw_roles.items():
            if (
                role not in FIXED_ROLES
                or not isinstance(entry, dict)
                or set(entry) != {"avd_name", "step", "created_at"}
                or entry.get("avd_name") != FIXED_ROLES[role][0]
                or entry.get("step") not in _PROVISIONING_STEPS
            ):
                raise DeploymentError("PROVISIONING_JOURNAL_INVALID")
            raw_created = entry.get("created_at")
            if raw_created is None:
                created = None
            elif isinstance(raw_created, str):
                try:
                    created = datetime.fromisoformat(raw_created)
                except ValueError:
                    raise DeploymentError(
                        "PROVISIONING_JOURNAL_INVALID"
                    ) from None
                if created.tzinfo is None:
                    raise DeploymentError("PROVISIONING_JOURNAL_INVALID")
            else:
                raise DeploymentError("PROVISIONING_JOURNAL_INVALID")
            if entry["step"] != "PENDING_CREATE" and created is None:
                # Legacy migration is allowed in memory and is stamped before
                # the next mutable phase; version-2 state is always precise.
                raise DeploymentError("PROVISIONING_JOURNAL_INVALID")
            entries[role] = {
                "step": entry["step"],
                "created_at": created,
            }
        return entries

    def _write(self, entries: dict[str, dict[str, object]]) -> None:
        if not set(entries).issubset(FIXED_ROLES) or any(
            entry.get("step") not in _PROVISIONING_STEPS
            for entry in entries.values()
        ):
            raise DeploymentError("PROVISIONING_JOURNAL_INVALID")
        document = {
            "version": 2,
            "roles": {
                role: {
                    "avd_name": FIXED_ROLES[role][0],
                    "step": entries[role]["step"],
                    "created_at": (
                        entries[role]["created_at"].isoformat()
                        if isinstance(entries[role].get("created_at"), datetime)
                        else None
                    ),
                }
                for role in FIXED_ROLES
                if role in entries
            },
        }
        encoded = json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > self._MAX_BYTES:
            raise DeploymentError("PROVISIONING_JOURNAL_INVALID")
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with NamedTemporaryFile(
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            raise DeploymentError("PROVISIONING_JOURNAL_INVALID") from None


class LoopbackLifecycleBroker:
    """Minimal client for the authenticated fixed-role host lifecycle service."""

    def __init__(self, token: str, *, timeout_seconds: float = 5.0) -> None:
        if not token:
            raise DeploymentError("ROOT_ENV_INVALID")
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._base_url = "http://127.0.0.1:18765"

    def device_states(self) -> Mapping[str, str]:
        document = self._request("GET", "/v1/devices")
        devices = document.get("devices")
        if not isinstance(devices, list):
            raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE")
        result: dict[str, str] = {}
        for item in devices:
            if not isinstance(item, dict):
                raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE")
            role = item.get("role")
            state = item.get("state")
            if role not in FIXED_ROLES or not isinstance(state, str):
                raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE")
            result[role] = state
        return result

    def start_and_launch_app(self, role: str) -> str:
        if role not in FIXED_ROLES:
            raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE")
        document = self._request(
            "POST",
            f"/v1/devices/{role}/actions",
            payload={"action": "start_and_launch_app"},
        )
        operation_id = document.get("operation_id")
        if not isinstance(operation_id, str) or not _SAFE_OPERATION_ID.fullmatch(
            operation_id
        ):
            raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE")
        return operation_id

    def wait_for_state(
        self, operation_id: str, expected_state: str, timeout_seconds: float
    ) -> None:
        if not _SAFE_OPERATION_ID.fullmatch(operation_id):
            raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE")
        deadline = time.monotonic() + timeout_seconds
        while True:
            document = self._request("GET", f"/v1/operations/{operation_id}")
            state = document.get("state")
            if state == expected_state:
                return
            if state == "ERROR":
                raise DeploymentError("DEVICE_LIFECYCLE_FAILED")
            if not isinstance(state, str) or time.monotonic() >= deadline:
                raise DeploymentError("DEVICE_LIFECYCLE_TIMEOUT")
            time.sleep(1.0)

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, str] | None = None,
    ) -> dict[str, object]:
        body = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {"Authorization": f"Bearer {self._token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE") from None
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            document = None
        if not isinstance(document, dict):
            raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE")
        return document


class LoopbackDataOnlyAcceptance:
    """Verify one fixed, catalog-confirmed data-only task through the public API."""

    def __init__(
        self,
        *,
        opener: Callable[..., object] | None = None,
        base_url: str = "http://127.0.0.1:8001",
        timeout_seconds: float = 180.0,
        poll_interval_seconds: float = 2.0,
        lease_owner_token: str | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._base_url = base_url.rstrip("/")
        if lease_owner_token is not None and (
            _SAFE_OPERATION_ID.fullmatch(lease_owner_token) is None
            or len(lease_owner_token) < 32
        ):
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
        self._lease_owner_token = lease_owner_token
        wrapped_opener = None
        if opener is not None:
            wrapped_opener = lambda request, timeout: opener(
                request, timeout=timeout
            )
        try:
            self._transport = SafeHttpTransport(
                self._base_url,
                max_body_bytes=1024 * 1024,
                opener=wrapped_opener,
            )
        except ValueError:
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED") from None

    @staticmethod
    def _valid_current_price(value: object) -> bool:
        return isinstance(value, str) and re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.[0-9]{2}", value
        ) is not None

    @staticmethod
    def _valid_change_percent(value: object) -> bool:
        return isinstance(value, str) and re.fullmatch(
            r"-?(?:0|[1-9][0-9]*)\.[0-9]{2}%", value
        ) is not None

    @staticmethod
    def _valid_turnover_rate(value: object) -> bool:
        return isinstance(value, str) and re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.[0-9]{2}%", value
        ) is not None

    @staticmethod
    def _valid_two_decimal_number(value: object) -> bool:
        return isinstance(value, str) and re.fullmatch(
            r"-?(?:0|[1-9][0-9]*)\.[0-9]{2}", value
        ) is not None

    @staticmethod
    def _valid_large_order_amount(value: object) -> bool:
        return isinstance(value, str) and re.fullmatch(
            r"-?(?:0|[1-9][0-9]*)\.[0-9]万", value
        ) is not None

    @staticmethod
    def _valid_macdfs(value: object) -> bool:
        if value == "0.000":
            return True
        return (
            isinstance(value, str)
            and value not in {"+0.000", "-0.000"}
            and re.fullmatch(
                r"[+-](?:0|[1-9][0-9]*)\.[0-9]{3}", value
            )
            is not None
        )

    @staticmethod
    def _valid_fund_unit(value: object) -> bool:
        return value in {"万元", "亿元"}

    @staticmethod
    def _valid_intraday_unit(field: str, value: object) -> bool:
        expected = {
            "large_order_net": None,
            "large_order_amount": "万",
            "retail_count": None,
        }
        return field in expected and value == expected[field]

    @classmethod
    def _valid_intraday_value(cls, field: str, value: object) -> bool:
        if value is None:
            return True
        if field == "large_order_amount":
            return isinstance(value, str) and re.fullmatch(
                r"-?(?:0|[1-9][0-9]*)\.[0-9]", value
            ) is not None
        if field in {"large_order_net", "retail_count"}:
            return cls._valid_two_decimal_number(value)
        return False

    @staticmethod
    def _validate_polled_identity(
        task: dict[str, object], expected_public_id: str
    ) -> None:
        if (
            task.get("public_id") != expected_public_id
            or task.get("symbol") != _ACCEPTANCE_SYMBOL
        ):
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")

    @classmethod
    def _valid_scalar_values(
        cls,
        values: dict[str, object],
        expected_stock_name: str | None,
    ) -> bool:
        stock_name = values.get("stock_name")
        return (
            isinstance(stock_name, str)
            and bool(stock_name)
            and (
                expected_stock_name is None
                or stock_name == expected_stock_name
            )
            and cls._valid_current_price(values.get("current_price"))
            and cls._valid_change_percent(values.get("change_percent"))
            and cls._valid_turnover_rate(values.get("turnover_rate"))
            and cls._valid_two_decimal_number(values.get("large_order_net"))
            and cls._valid_large_order_amount(values.get("large_order_amount"))
            and cls._valid_two_decimal_number(values.get("retail_count"))
            and cls._valid_macdfs(values.get("macdfs"))
        )

    def verify(self) -> None:
        symbol = self._request_json("GET", f"/api/v1/symbols/{_ACCEPTANCE_SYMBOL}")
        confirmed_name = symbol.get("name")
        if (
            symbol.get("symbol") != _ACCEPTANCE_SYMBOL
            or symbol.get("market") != "17"
            or not isinstance(confirmed_name, str)
            or not confirmed_name.strip()
        ):
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
        if self._lease_owner_token is None:
            submitted = self._request_json(
                "POST",
                "/api/v1/jobs",
                payload={
                    "symbol": _ACCEPTANCE_SYMBOL,
                    "include_long_capture": False,
                },
            )
        else:
            submitted = self._request_json(
                "POST",
                "/internal/deployment/acceptance",
                payload={},
                extra_headers={
                    "Authorization": f"Bearer {self._lease_owner_token}"
                },
            )
        public_id = submitted.get("public_id")
        if (
            not isinstance(public_id, str)
            or not _SAFE_OPERATION_ID.fullmatch(public_id)
            or submitted.get("symbol") != _ACCEPTANCE_SYMBOL
        ):
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            task = self._request_json("GET", f"/api/v1/jobs/{public_id}")
            self._validate_polled_identity(task, public_id)
            status = task.get("status")
            if status == "COMPLETED":
                self._validate_completed_task(
                    task,
                    expected_public_id=public_id,
                    expected_stock_name=confirmed_name,
                )
                return
            if status not in {"QUEUED", "RUNNING"}:
                raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
            if time.monotonic() >= deadline:
                raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
            time.sleep(max(0.0, self._poll_interval_seconds))

    @classmethod
    def _validate_completed_task(
        cls,
        task: dict[str, object],
        *,
        expected_public_id: str | None = None,
        expected_stock_name: str | None = None,
    ) -> None:
        values = task.get("values")
        sources = task.get("value_sources")
        source_errors = task.get("source_errors")
        long_capture = task.get("long_capture")
        captures = task.get("captures")
        if (
            task.get("status") != "COMPLETED"
            or task.get("symbol") != _ACCEPTANCE_SYMBOL
            or task.get("include_long_capture") is not False
            or task.get("error_code") is not None
            or not isinstance(values, dict)
            or not isinstance(sources, dict)
            or source_errors
            != {"core_metrics": None, "main_fund_flow": None}
            or not isinstance(long_capture, dict)
            or long_capture.get("status") != "SKIPPED"
            or long_capture.get("url") is not None
            or long_capture.get("expires_at") is not None
            or not isinstance(captures, list)
            or not cls._valid_scalar_values(values, expected_stock_name)
            or any(
                sources.get(field) != "INTERFACE"
                for field in _ACCEPTANCE_REQUIRED_VALUES
            )
            or (
                expected_public_id is not None
                and task.get("public_id") != expected_public_id
            )
        ):
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
        capture_kinds: set[str] = set()
        for capture in captures:
            if (
                not isinstance(capture, dict)
                or not isinstance(capture.get("kind"), str)
                or capture.get("status") != "SKIPPED"
                or capture.get("url") is not None
                or capture.get("expires_at") is not None
            ):
                raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
            capture_kinds.add(capture["kind"])
        if capture_kinds != _ACCEPTANCE_CAPTURE_KINDS or len(captures) != len(
            _ACCEPTANCE_CAPTURE_KINDS
        ):
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
        intraday = values.get("intraday_series")
        intraday_sources = sources.get("intraday_series")
        if not isinstance(intraday, dict) or not isinstance(intraday_sources, dict):
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
        for field in _ACCEPTANCE_INTRADAY_FIELDS:
            curve = intraday.get(field)
            if (
                not isinstance(curve, dict)
                or not cls._valid_intraday_unit(field, curve.get("unit"))
                or not isinstance(curve.get("points"), list)
                or not curve["points"]
                or intraday_sources.get(field) != "INTERFACE"
            ):
                raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
            times: list[str] = []
            valid_values = 0
            for point in curve["points"]:
                if not isinstance(point, dict):
                    raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
                point_time = point.get("time")
                point_value = point.get("value")
                if (
                    not isinstance(point_time, str)
                    or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", point_time)
                    is None
                    or not cls._valid_intraday_value(field, point_value)
                ):
                    raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
                times.append(point_time)
                if isinstance(point_value, str):
                    valid_values += 1
            if (
                any(left >= right for left, right in zip(times, times[1:]))
                or valid_values == 0
            ):
                raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
        fund = values.get("main_fund_flow")
        fund_sources = sources.get("main_fund_flow")
        if not isinstance(fund, dict) or not isinstance(fund_sources, dict):
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
        for period in _ACCEPTANCE_FUND_PERIODS:
            period_values = fund.get(period)
            period_sources = fund_sources.get(period)
            if (
                not isinstance(period_values, dict)
                or not isinstance(period_sources, dict)
                or not cls._valid_fund_unit(period_values.get("unit"))
                or any(
                    not cls._valid_two_decimal_number(period_values.get(field))
                    or period_sources.get(field) != "INTERFACE"
                    for field in _ACCEPTANCE_FUND_FIELDS
                )
            ):
                raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        body = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if extra_headers:
            headers.update(extra_headers)
        request = Request(
            f"{self._base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        request_timeout = max(0.1, min(10.0, self._timeout_seconds))
        try:
            response = self._transport.request(request, request_timeout)
            if not 200 <= response.status < 300:
                raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
            document = json.loads(response.body.decode("utf-8"))
        except DeploymentError:
            raise
        except (SafeHttpError, SafeHttpStatusError):
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED") from None
        except Exception:
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED") from None
        if not isinstance(document, dict):
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED")
        return document


class MacDeploymentOrchestrator:
    def __init__(
        self,
        runner: CommandRunner,
        lifecycle_broker: LifecycleBroker | None,
        filesystem: FileSystem,
        *,
        project_root: Path,
        env_file: Path = Path(".env"),
        process_executable_resolver: ProcessExecutableResolver | None = None,
        data_only_acceptance: DataOnlyAcceptance | None = None,
        provisioning_journal: ProvisioningJournalStore | None = None,
        deployment_maintenance: DeploymentMaintenance | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        health_timeout_seconds: float = 180.0,
        boot_timeout_seconds: float = 180.0,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self._runner = runner
        self._broker = lifecycle_broker
        self._filesystem = filesystem
        self._project_root = project_root.resolve()
        self._env_argument = str(env_file)
        self._env_file = (
            env_file.resolve()
            if env_file.is_absolute()
            else (self._project_root / env_file).resolve()
        )
        self._macos_env = (self._project_root / "deploy/macos.env").resolve()
        self._health_timeout_seconds = health_timeout_seconds
        self._boot_timeout_seconds = boot_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._root_environment: dict[str, str] = {}
        self._trusted_emulator_path: Path | None = None
        self._data_only_acceptance = data_only_acceptance
        self._provisioning_journal = provisioning_journal or ProvisioningJournal()
        self._initial_missing_roles: frozenset[str] | None = None
        self._process_executable_resolver = (
            process_executable_resolver or DarwinProcessExecutableResolver()
        )
        self._deployment_maintenance = deployment_maintenance
        self._now = now

    @property
    def initial_missing_roles(self) -> frozenset[str]:
        return self._initial_missing_roles or frozenset()

    def deploy(self, mode: str = "auto") -> DeploymentResult:
        self._validate_env_file_location()
        if mode == "existing":
            return self.deploy_existing()
        if mode == "provision":
            return self.provision_missing()
        if mode != "auto":
            raise DeploymentError("DEPLOYMENT_MODE_INVALID")
        journaled = self._provisioning_journal.load()
        self._validate_command_presence()
        initial_missing = self._missing_fixed_roles()
        if not initial_missing and not journaled:
            return self.deploy_existing()
        return self.provision_missing(initial_missing)

    def deploy_provision(self) -> DeploymentResult:
        return self.provision_missing()

    def provision_missing(
        self,
        initial_missing_roles: frozenset[str] | None = None,
    ) -> DeploymentResult:
        self._validate_env_file_location()
        self._validate_command_presence()
        actual_missing = (
            initial_missing_roles
            if initial_missing_roles is not None
            else self._missing_fixed_roles()
        )
        self._validate_host_prerequisites(provision_system_image=True)
        journal_steps = self._provisioning_journal.record_initial_missing(
            actual_missing
        )
        provisioning_roles = self._record_initial_missing_roles(
            frozenset(journal_steps)
        )
        self._root_environment = self._ensure_root_environment()
        self._validate_macos_environment()
        self._validate_effective_compose_config()
        self._build_local_image()
        manifest = self._read_image_manifest()
        preserved_roles = tuple(
            role for role in FIXED_ROLES if role not in provisioning_roles
        )
        self._verify_preinstall_fixed_avd_identities(preserved_roles)
        for role in FIXED_ROLES:
            if role not in provisioning_roles:
                continue
            step = journal_steps[role]
            if role in actual_missing:
                if step not in {"PENDING_CREATE", "AVD_CREATED"}:
                    raise DeploymentError("PROVISIONING_JOURNAL_INVALID")
                if step == "PENDING_CREATE":
                    self._create_fixed_avd(role)
                    self._provisioning_journal.set_step(
                        role, "AVD_CREATED", created_at=self._now()
                    )
                    step = "AVD_CREATED"
            elif step == "PENDING_CREATE":
                self._provisioning_journal.set_step(
                    role, "AVD_CREATED", created_at=self._now()
                )
                step = "AVD_CREATED"
            elif (
                step != "PENDING_CREATE"
                and self._provisioning_journal.created_at(role) is None
            ):
                self._provisioning_journal.set_step(
                    role, "AVD_CREATED", created_at=self._now()
                )
                step = "AVD_CREATED"
            if step == "AVD_CREATED":
                self._ensure_provisioning_avd_booted(role)
                self._provision_created_avd(role, "apk")
                self._provisioning_journal.set_step(role, "APK_VERIFIED")
                step = "APK_VERIFIED"
            if step == "APK_VERIFIED":
                self._ensure_provisioning_avd_booted(role)
                self._provision_created_avd(role, "frida")
                self._provisioning_journal.set_step(role, "FRIDA_READY")
                step = "FRIDA_READY"
            if step not in {
                "FRIDA_READY",
                "LOGIN_REQUIRED",
                "ACCEPTANCE_PENDING",
            }:
                raise DeploymentError("PROVISIONING_JOURNAL_INVALID")
        self._install_lifecycle_service()
        for role in FIXED_ROLES:
            if role not in provisioning_roles:
                continue
            if self._provisioning_journal.load().get(role) == "FRIDA_READY":
                self._start_and_launch_roles((role,))
        self._verify_fixed_avd_identities(
            tuple(role for role in FIXED_ROLES if role in provisioning_roles)
        )
        self._verify_installed_apks(manifest["apk"]["sha256"])
        maintenance = self._deployment_maintenance
        maintenance_prepared = False
        try:
            before_replace = self._provisioning_journal.load()
            if maintenance is not None and all(
                step in {"LOGIN_REQUIRED", "ACCEPTANCE_PENDING"}
                for step in before_replace.values()
            ):
                maintenance.prepare()
                maintenance_prepared = True
                maintenance.renew()
            self._validate_effective_compose_config()
            self._compose_up()
            self._wait_for_compose_health()
            for role in provisioning_roles:
                if self._provisioning_journal.load().get(role) == "FRIDA_READY":
                    self._provisioning_journal.set_step(role, "LOGIN_REQUIRED")
            session_statuses = self._session_bundle_statuses()
            if session_statuses is None:
                return DeploymentResult(
                    mode="provision",
                    state="FIRST_TIME_LOGIN_REQUIRED",
                    instructions=(
                        _FIRST_TIME_LOGIN_INSTRUCTIONS
                        + (
                            _MAINTENANCE_RETAINED_INSTRUCTIONS
                            if maintenance_prepared
                            else ()
                        )
                    ),
                )
            for role in provisioning_roles:
                created_at = self._provisioning_journal.created_at(role)
                refreshed_at = session_statuses.get(role)
                if (
                    created_at is None
                    or refreshed_at is None
                    or refreshed_at <= created_at
                ):
                    return DeploymentResult(
                        mode="provision",
                        state="FIRST_TIME_LOGIN_REQUIRED",
                        instructions=(
                            _FIRST_TIME_LOGIN_INSTRUCTIONS
                            + (
                                _MAINTENANCE_RETAINED_INSTRUCTIONS
                                if maintenance_prepared
                                else ()
                            )
                        ),
                    )
            for role in provisioning_roles:
                if self._provisioning_journal.load().get(role) == "LOGIN_REQUIRED":
                    self._provisioning_journal.set_step(
                        role, "ACCEPTANCE_PENDING"
                    )
            if any(
                step != "ACCEPTANCE_PENDING"
                for step in self._provisioning_journal.load().values()
            ):
                raise DeploymentError("PROVISIONING_JOURNAL_INVALID")
            if maintenance is not None and not maintenance_prepared:
                return DeploymentResult(
                    mode="provision",
                    state="FIRST_TIME_LOGIN_REQUIRED",
                    instructions=_FIRST_TIME_LOGIN_INSTRUCTIONS,
                )
            if maintenance is not None:
                maintenance.renew()
            self._verify_data_only_acceptance()
            if maintenance is not None:
                maintenance.release()
            for role in tuple(provisioning_roles):
                self._provisioning_journal.complete(role)
            return DeploymentResult(mode="provision", state="READY")
        except DeploymentError as error:
            if maintenance_prepared and not error.instructions:
                raise DeploymentError(
                    error.error_code,
                    instructions=_MAINTENANCE_RETAINED_INSTRUCTIONS,
                ) from None
            raise

    def deploy_existing(self) -> DeploymentResult:
        maintenance = self._deployment_maintenance
        maintenance_prepared = False
        try:
            self._validate_env_file_location()
            self._validate_host_prerequisites()
            self._require_fixed_avds()
            self._root_environment = self._ensure_root_environment()
            self._validate_macos_environment()
            self._validate_effective_compose_config()
            self._build_local_image()
            manifest = self._read_image_manifest()
            self._verify_preinstall_fixed_avd_identities()
            if maintenance is not None:
                maintenance.prepare()
                maintenance_prepared = True
            self._install_lifecycle_service()
            self._start_stopped_roles()
            self._verify_fixed_avd_identities()
            self._verify_installed_apks(manifest["apk"]["sha256"])
            self._validate_effective_compose_config()
            if maintenance is not None:
                maintenance.renew()
            self._compose_up()
            self._wait_for_compose_health()
            if maintenance is not None:
                maintenance.renew()
                if not self._session_bundles_ready():
                    raise DeploymentError("SESSION_NOT_READY")
                self._verify_data_only_acceptance()
                maintenance.release()
            return DeploymentResult(mode="existing", state="READY")
        except DeploymentError as error:
            if maintenance_prepared and not error.instructions:
                raise DeploymentError(
                    error.error_code,
                    instructions=_MAINTENANCE_RETAINED_INSTRUCTIONS,
                ) from None
            raise

    def _validate_env_file_location(self) -> None:
        try:
            relative = self._env_file.relative_to(self._project_root)
        except ValueError:
            return
        if relative != Path(".env"):
            raise DeploymentError("ENV_FILE_IN_BUILD_CONTEXT")
        dockerignore = self._project_root / ".dockerignore"
        try:
            ignored = self._dockerignore_excludes_root_env(
                self._filesystem.read_text(dockerignore)
            )
        except Exception:
            raise DeploymentError("ROOT_ENV_NOT_IGNORED") from None
        if not ignored:
            raise DeploymentError("ROOT_ENV_NOT_IGNORED")
        self._env_argument = ".env"

    @staticmethod
    def _dockerignore_excludes_root_env(content: str) -> bool:
        excluded = False
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            negated = line.startswith("!")
            pattern = line[1:] if negated else line
            pattern = pattern.removeprefix("/")
            root_pattern = pattern.removeprefix("**/")
            if fnmatch.fnmatchcase(".env", pattern) or fnmatch.fnmatchcase(
                ".env", root_pattern
            ):
                excluded = not negated
        return excluded

    def _validate_command_presence(self) -> None:
        try:
            resolved_commands = {
                command: self._filesystem.which(command)
                for command in REQUIRED_COMMANDS
            }
        except Exception:
            raise DeploymentError("MISSING_PREREQUISITE") from None
        if any(path is None for path in resolved_commands.values()):
            raise DeploymentError("MISSING_PREREQUISITE")
        emulator_path = resolved_commands["emulator"]
        assert emulator_path is not None
        candidate = Path(emulator_path)
        if not candidate.is_absolute() or candidate.name != "emulator":
            raise DeploymentError("MISSING_PREREQUISITE")
        try:
            self._trusted_emulator_path = candidate.resolve()
        except OSError:
            raise DeploymentError("MISSING_PREREQUISITE") from None

    def _validate_host_prerequisites(
        self, *, provision_system_image: bool = False
    ) -> None:
        self._validate_command_presence()
        self._validate_disk_space()
        system = self._run(("uname", "-s"), 10.0, "UNSUPPORTED_HOST")
        architecture = self._run(("uname", "-m"), 10.0, "UNSUPPORTED_HOST")
        if self._stdout_text(system).strip() != "Darwin" or self._stdout_text(
            architecture
        ).strip() not in {"arm64", "aarch64"}:
            raise DeploymentError("UNSUPPORTED_HOST")
        context = self._run(
            ("docker", "--context", "orbstack", "info", "--format", "{{.Name}}"),
            30.0,
            "ORBSTACK_UNAVAILABLE",
        )
        if "orbstack" not in self._stdout_text(context).lower():
            raise DeploymentError("ORBSTACK_UNAVAILABLE")
        java = self._run(("java", "-version"), 10.0, "JAVA_17_REQUIRED")
        java_version = self._stdout_text(java) + self._stderr_text(java)
        if not re.search(r'version\s+"17(?:[."]|$)', java_version):
            raise DeploymentError("JAVA_17_REQUIRED")
        sdk = self._run(
            ("sdkmanager", "--list_installed"),
            60.0,
            (
                "ANDROID_SYSTEM_IMAGE_UNAVAILABLE"
                if provision_system_image
                else "ANDROID_33_ARM64_UNAVAILABLE"
            ),
        )
        if ANDROID_SYSTEM_IMAGE not in self._stdout_text(sdk):
            if not provision_system_image:
                raise DeploymentError("ANDROID_33_ARM64_UNAVAILABLE")
            self._install_android_system_image()

    def _validate_disk_space(self) -> None:
        try:
            free_values = (
                self._filesystem.free_bytes(self._project_root),
                self._filesystem.free_bytes(Path.home() / ".android/avd"),
            )
        except Exception:
            raise DeploymentError("INSUFFICIENT_DISK_SPACE") from None
        if any(value < MINIMUM_FREE_BYTES for value in free_values):
            raise DeploymentError("INSUFFICIENT_DISK_SPACE")

    def _install_android_system_image(self) -> None:
        command = ("sdkmanager", ANDROID_SYSTEM_IMAGE)
        try:
            result = self._runner.run(command, 1800.0, b"")
        except Exception:
            raise DeploymentError("ANDROID_SYSTEM_IMAGE_UNAVAILABLE") from None
        if result.returncode != 0:
            diagnostic = self._safe_process_diagnostic(result).lower()
            if "license" in diagnostic and (
                "not accepted" in diagnostic
                or "not been accepted" in diagnostic
                or "accept the sdk license" in diagnostic
            ):
                raise DeploymentError("ANDROID_LICENSE_REQUIRED")
            raise DeploymentError("ANDROID_SYSTEM_IMAGE_UNAVAILABLE")
        verified = self._run(
            ("sdkmanager", "--list_installed"),
            60.0,
            "ANDROID_SYSTEM_IMAGE_UNAVAILABLE",
        )
        if ANDROID_SYSTEM_IMAGE not in self._stdout_text(verified):
            raise DeploymentError("ANDROID_SYSTEM_IMAGE_UNAVAILABLE")

    @staticmethod
    def _safe_process_diagnostic(
        result: subprocess.CompletedProcess[bytes],
    ) -> str:
        parts: list[str] = []
        for raw in (result.stdout, result.stderr):
            if isinstance(raw, bytes):
                try:
                    parts.append(raw.decode("utf-8"))
                except UnicodeDecodeError:
                    continue
        return "\n".join(parts)

    def _fixed_avds_present(self) -> bool:
        return not self._missing_fixed_roles()

    def _missing_fixed_roles(self) -> frozenset[str]:
        result = self._run(
            ("emulator", "-list-avds"), 30.0, "FIXED_AVD_NOT_FOUND"
        )
        avds = {
            line.strip()
            for line in self._stdout_text(result).splitlines()
            if line.strip()
        }
        return frozenset(
            role for role, (avd, _serial) in FIXED_ROLES.items() if avd not in avds
        )

    def _record_initial_missing_roles(
        self, roles: frozenset[str]
    ) -> frozenset[str]:
        if not roles.issubset(FIXED_ROLES):
            raise DeploymentError("PROVISIONING_STATE_INVALID")
        if self._initial_missing_roles is None:
            self._initial_missing_roles = frozenset(roles)
        elif self._initial_missing_roles != roles:
            raise DeploymentError("PROVISIONING_STATE_INVALID")
        return self._initial_missing_roles

    def _require_fixed_avds(self) -> None:
        if not self._fixed_avds_present():
            raise DeploymentError("FIXED_AVD_NOT_FOUND")

    def _ensure_root_environment(self) -> dict[str, str]:
        try:
            if not self._filesystem.exists(self._env_file):
                setup = self._project_root / "scripts/setup-admin.sh"
                self._run(
                    (str(setup), str(self._env_file)),
                    300.0,
                    "ROOT_ENV_SETUP_FAILED",
                )
            if not self._filesystem.exists(self._env_file):
                raise DeploymentError("ROOT_ENV_INVALID")
            if (
                self._filesystem.mode(self._env_file) != 0o600
                or not self._filesystem.is_secure_owner_file(self._env_file)
            ):
                raise DeploymentError("ROOT_ENV_INVALID")
            values = self._parse_env(self._filesystem.read_text(self._env_file))
            missing = {
                key for key in REQUIRED_ROOT_ENV_KEYS if not values.get(key)
            }
            upgradeable = {
                "THS_SESSION_ENCRYPTION_KEY",
                "THS_DEVICE_LIFECYCLE_TOKEN",
            }
            if missing and missing.issubset(upgradeable):
                setup = self._project_root / "scripts/setup-admin.sh"
                self._run(
                    (
                        str(setup),
                        "--upgrade-existing",
                        str(self._env_file),
                    ),
                    300.0,
                    "ROOT_ENV_SETUP_FAILED",
                )
                if (
                    not self._filesystem.exists(self._env_file)
                    or self._filesystem.mode(self._env_file) != 0o600
                    or not self._filesystem.is_secure_owner_file(self._env_file)
                ):
                    raise DeploymentError("ROOT_ENV_INVALID")
                values = self._parse_env(
                    self._filesystem.read_text(self._env_file)
                )
        except DeploymentError:
            raise
        except Exception:
            raise DeploymentError("ROOT_ENV_INVALID") from None
        if any(not values.get(key) for key in REQUIRED_ROOT_ENV_KEYS):
            raise DeploymentError("ROOT_ENV_INVALID")
        try:
            decoded_key = base64.b64decode(
                values["THS_SESSION_ENCRYPTION_KEY"],
                altchars=b"-_",
                validate=True,
            )
        except (ValueError, TypeError):
            decoded_key = b""
        if len(decoded_key) != 32:
            raise DeploymentError("ROOT_ENV_INVALID")
        return values

    def _validate_macos_environment(self) -> None:
        try:
            if not self._filesystem.exists(self._macos_env):
                raise DeploymentError("MACOS_ENV_INVALID")
            values = self._parse_env(self._filesystem.read_text(self._macos_env))
        except DeploymentError:
            raise DeploymentError("MACOS_ENV_INVALID") from None
        except Exception:
            raise DeploymentError("MACOS_ENV_INVALID") from None
        if ROOT_ONLY_COMPOSE_KEYS.intersection(values):
            raise DeploymentError("MACOS_ENV_INVALID")
        if any(values.get(key) != expected for key, expected in REQUIRED_MACOS_ENV.items()):
            raise DeploymentError("MACOS_ENV_INVALID")

    @staticmethod
    def _parse_env(content: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise DeploymentError("ROOT_ENV_INVALID")
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip()
            if (
                not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
                or name in values
                or name.startswith("COMPOSE_")
            ):
                raise DeploymentError("ROOT_ENV_INVALID")
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values[name] = value
        return values

    def _compose_prefix(self) -> tuple[str, ...]:
        return (
            "docker",
            "--context",
            "orbstack",
            "compose",
            "--project-name",
            COMPOSE_PROJECT_NAME,
            "--env-file",
            self._env_argument,
            "--env-file",
            "deploy/macos.env",
            "-f",
            "deploy/compose.yml",
        )

    def _build_local_image(self) -> None:
        self._run(
            self._compose_prefix() + ("build", "api"),
            1800.0,
            "IMAGE_BUILD_FAILED",
        )

    def _validate_effective_compose_config(self) -> None:
        result = self._run(
            self._compose_prefix() + ("config", "--format", "json"),
            60.0,
            "COMPOSE_CONFIG_INVALID",
        )
        try:
            document = json.loads(self._stdout_text(result))
            services = document["services"]
            api = services["api"]
            redis_service = services["redis"]
            environment = api["environment"]
            ports = api["ports"]
            api_volumes = api["volumes"]
            redis_volumes = redis_service["volumes"]
            volumes = document["volumes"]
        except (KeyError, TypeError, ValueError):
            raise DeploymentError("COMPOSE_CONFIG_INVALID") from None
        expected = {
            "THS_DEVICE_LIFECYCLE_URL": REQUIRED_MACOS_ENV[
                "THS_DEVICE_LIFECYCLE_URL"
            ],
            "THS_DEVICE_LIFECYCLE_TOKEN": self._root_environment[
                "THS_DEVICE_LIFECYCLE_TOKEN"
            ],
            "THS_SESSION_ENCRYPTION_KEY": self._root_environment[
                "THS_SESSION_ENCRYPTION_KEY"
            ],
        }
        expected_project_volumes = {
            name: f"{COMPOSE_PROJECT_NAME}_{name}"
            for name in (
                "capture-data",
                "template-data",
                "admin-data",
                "market-data",
                "redis-data",
            )
        }

        def volume_map(items: object) -> dict[str, str]:
            if not isinstance(items, list):
                raise DeploymentError("COMPOSE_CONFIG_INVALID")
            mapped: dict[str, str] = {}
            for item in items:
                if (
                    not isinstance(item, dict)
                    or item.get("type") != "volume"
                    or not isinstance(item.get("source"), str)
                    or not isinstance(item.get("target"), str)
                ):
                    raise DeploymentError("COMPOSE_CONFIG_INVALID")
                mapped[item["target"]] = item["source"]
            return mapped

        api_mounts = volume_map(api_volumes)
        redis_mounts = volume_map(redis_volumes)
        valid_port = (
            isinstance(ports, list)
            and len(ports) == 1
            and isinstance(ports[0], dict)
            and ports[0].get("target") == 8000
            and str(ports[0].get("published")) == "8001"
            and ports[0].get("protocol") == "tcp"
        )
        volume_names_valid = isinstance(volumes, dict) and all(
            isinstance(volumes.get(source), dict)
            and volumes[source].get("name") == resolved
            for source, resolved in expected_project_volumes.items()
        )
        if (
            document.get("name") != COMPOSE_PROJECT_NAME
            or not isinstance(services, dict)
            or set(services) != {"api", "redis"}
            or not valid_port
            or not volume_names_valid
            or api_mounts.get("/data/captures") != "capture-data"
            or api_mounts.get("/data/templates") != "template-data"
            or api_mounts.get("/data/admin") != "admin-data"
            or api_mounts.get("/data/market") != "market-data"
            or redis_mounts != {"/data": "redis-data"}
            or not isinstance(environment, dict)
            or environment.get("THS_SESSION_ROOT")
            != "/data/admin/ths-sessions"
            or any(
                environment.get(key) != value or not value
                for key, value in expected.items()
            )
        ):
            raise DeploymentError("COMPOSE_CONFIG_INVALID")

    def _read_image_manifest(self) -> dict[str, dict[str, object]]:
        result = self._run(
            (
                "docker",
                "--context",
                "orbstack",
                "run",
                "--rm",
                "--entrypoint",
                "cat",
                IMAGE_NAME,
                "/opt/ths/assets/manifest.json",
            ),
            60.0,
            "IMAGE_ASSET_MANIFEST_INVALID",
        )
        try:
            manifest = json.loads(self._stdout_text(result))
        except ValueError:
            manifest = None
        expected = {
            "apk": {
                "filename": "ths.apk",
                "size": 214_088_292,
                "sha256": APK_SHA256,
                "abis": ["arm64-v8a", "armeabi-v7a"],
            },
            "frida_server": {
                "version": "16.7.19",
                "size": 53_702_368,
                "sha256_xz": FRIDA_SHA256,
                "sha256": FRIDA_BINARY_SHA256,
            },
        }
        if manifest != expected:
            raise DeploymentError("IMAGE_ASSET_MANIFEST_INVALID")
        return manifest

    def _install_lifecycle_service(self) -> None:
        installer = self._project_root / "scripts/install-macos-device-lifecycle.sh"
        self._run(
            (
                str(installer),
                "--project-root",
                str(self._project_root),
                "--env-file",
                str(self._env_file),
            ),
            120.0,
            "DEVICE_LIFECYCLE_INSTALL_FAILED",
        )

    def _create_fixed_avd(self, role: str) -> None:
        avd_name, _serial = FIXED_ROLES[role]
        self._run(
            (
                "avdmanager",
                "create",
                "avd",
                "--name",
                avd_name,
                "--package",
                ANDROID_SYSTEM_IMAGE,
            ),
            300.0,
            "AVD_CREATE_FAILED",
            input_data=b"no\n",
        )

    def _start_created_avd(self, role: str) -> None:
        avd_name, _serial = FIXED_ROLES[role]
        emulator_path = self._trusted_emulator_path
        if emulator_path is None:
            raise DeploymentError("MISSING_PREREQUISITE")
        port = FIXED_EMULATOR_PORTS[role]
        self._run(
            (
                "launchctl",
                "submit",
                "-l",
                f"com.ths.avd.{port}",
                "--",
                str(emulator_path),
                "-avd",
                avd_name,
                "-port",
                str(port),
                "-no-snapshot",
                "-no-audio",
                "-gpu",
                "host",
                "-memory",
                "2048",
                "-cores",
                "4",
            ),
            30.0,
            "DEVICE_LAUNCH_FAILED",
        )
        self._wait_for_created_avd_boot(role)

    def _ensure_provisioning_avd_booted(self, role: str) -> None:
        expected_avd, serial = FIXED_ROLES[role]
        try:
            identity = self._identity_verifier().inspect(
                serial=serial,
                expected_avd=expected_avd,
                emulator_port=FIXED_EMULATOR_PORTS[role],
            )
        except IdentityVerificationError:
            raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH") from None
        if identity.presence in {
            FixedAvdPresence.ATTACHED,
            FixedAvdPresence.STARTING,
        }:
            self._wait_for_created_avd_boot(role)
            return
        self._start_created_avd(role)

    def _wait_for_created_avd_boot(self, role: str) -> None:
        expected_avd, serial = FIXED_ROLES[role]
        deadline = time.monotonic() + self._boot_timeout_seconds
        while True:
            try:
                state = self._runner.run(("adb", "-s", serial, "get-state"), 15.0)
                boot = self._runner.run(
                    (
                        "adb",
                        "-s",
                        serial,
                        "shell",
                        "getprop",
                        "sys.boot_completed",
                    ),
                    15.0,
                )
                ready = (
                    state.returncode == 0
                    and self._stdout_text(state).strip() == "device"
                    and boot.returncode == 0
                    and self._stdout_text(boot).strip() == "1"
                )
            except Exception:
                ready = False
            if ready:
                try:
                    self._identity_verifier().require_attached(
                        serial=serial, expected_avd=expected_avd
                    )
                except IdentityVerificationError:
                    raise DeploymentError(
                        "FIXED_AVD_IDENTITY_MISMATCH"
                    ) from None
                return
            if time.monotonic() >= deadline:
                raise DeploymentError("DEVICE_BOOT_TIMEOUT")
            time.sleep(max(0.0, self._poll_interval_seconds))

    def _provision_created_avd(self, role: str, step: str) -> None:
        if step not in {"apk", "frida"}:
            raise DeploymentError("DEVICE_PROVISION_FAILED")
        self._run(
            (
                "docker",
                "--context",
                "orbstack",
                "run",
                "--rm",
                "--add-host",
                "host.docker.internal:host-gateway",
                "--env",
                "ADB_SERVER_SOCKET=tcp:host.docker.internal:5037",
                "--entrypoint",
                "container-provision-device",
                IMAGE_NAME,
                role,
                step,
            ),
            300.0,
            "DEVICE_PROVISION_FAILED",
        )

    def _verify_fixed_avd_identities(
        self, roles: tuple[str, ...] | None = None
    ) -> None:
        selected = tuple(FIXED_ROLES) if roles is None else roles
        for role in selected:
            expected_avd, serial = FIXED_ROLES[role]
            try:
                self._identity_verifier().require_attached(
                    serial=serial, expected_avd=expected_avd
                )
            except IdentityVerificationError:
                raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH") from None

    def _verify_preinstall_fixed_avd_identities(
        self, roles: tuple[str, ...] | None = None
    ) -> None:
        selected = tuple(FIXED_ROLES) if roles is None else roles
        for role in selected:
            expected_avd, serial = FIXED_ROLES[role]
            try:
                identity = self._identity_verifier().inspect(
                    serial=serial,
                    expected_avd=expected_avd,
                    emulator_port=FIXED_EMULATOR_PORTS[role],
                )
            except IdentityVerificationError:
                raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH") from None
            if (
                identity.presence is FixedAvdPresence.ATTACHED
                and identity.adb_state != "device"
            ):
                raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH")

    def _identity_verifier(self) -> FixedAvdIdentityVerifier:
        trusted = self._trusted_emulator_path
        if trusted is None:
            raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH")
        try:
            return FixedAvdIdentityVerifier(
                self._runner,
                trusted,
                self._process_executable_resolver,
            )
        except IdentityVerificationError:
            raise DeploymentError("FIXED_AVD_IDENTITY_MISMATCH") from None

    def _start_stopped_roles(self) -> None:
        broker = self._broker
        if broker is None:
            broker = LoopbackLifecycleBroker(
                self._root_environment["THS_DEVICE_LIFECYCLE_TOKEN"]
            )
            self._broker = broker
        try:
            states = dict(broker.device_states())
            for role in FIXED_ROLES:
                state = states.get(role)
                if state not in {"RUNNING", "STOPPED"}:
                    raise DeploymentError("DEVICE_LIFECYCLE_NOT_READY")
                operation_id = broker.start_and_launch_app(role)
                broker.wait_for_state(operation_id, "RUNNING", 180.0)
        except DeploymentError:
            raise
        except Exception:
            raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE") from None

    def _start_and_launch_roles(self, roles: tuple[str, ...]) -> None:
        broker = self._broker
        if broker is None:
            broker = LoopbackLifecycleBroker(
                self._root_environment["THS_DEVICE_LIFECYCLE_TOKEN"]
            )
            self._broker = broker
        try:
            for role in roles:
                operation_id = broker.start_and_launch_app(role)
                broker.wait_for_state(operation_id, "RUNNING", 180.0)
        except DeploymentError:
            raise
        except Exception:
            raise DeploymentError("DEVICE_LIFECYCLE_UNAVAILABLE") from None

    def _verify_installed_apks(self, expected_sha256: object) -> None:
        if expected_sha256 != APK_SHA256:
            raise DeploymentError("IMAGE_ASSET_MANIFEST_INVALID")
        for _role, (_avd, serial) in FIXED_ROLES.items():
            path_result = self._run(
                ("adb", "-s", serial, "shell", "pm", "path", PACKAGE_NAME),
                30.0,
                "INSTALLED_APK_UNAVAILABLE",
            )
            base_apk = self._parse_base_apk_path(self._stdout_text(path_result))
            digest_result = self._run(
                ("adb", "-s", serial, "shell", "sha256sum", base_apk),
                60.0,
                "INSTALLED_APK_UNAVAILABLE",
            )
            digest = self._parse_apk_digest(
                self._stdout_text(digest_result), base_apk
            )
            if digest != expected_sha256:
                raise DeploymentError("INSTALLED_APK_MISMATCH")

    @staticmethod
    def _parse_base_apk_path(output: str) -> str:
        candidates = []
        for line in output.splitlines():
            if not line.startswith("package:"):
                continue
            candidate = line.removeprefix("package:")
            if candidate.endswith("/base.apk"):
                candidates.append(candidate)
        if len(candidates) != 1:
            raise DeploymentError("INSTALLED_APK_PATH_INVALID")
        candidate = candidates[0]
        path = PurePosixPath(candidate)
        if (
            not _SAFE_APK_PATH.fullmatch(candidate)
            or not candidate.startswith("/data/app/")
            or ".." in path.parts
            or "//" in candidate
        ):
            raise DeploymentError("INSTALLED_APK_PATH_INVALID")
        return candidate

    @staticmethod
    def _parse_apk_digest(output: str, base_apk: str) -> str:
        match = re.fullmatch(r"([0-9a-f]{64})[ \t]+([^\r\n]+)\r?\n?", output)
        if match is None or match.group(2) != base_apk:
            raise DeploymentError("INSTALLED_APK_PATH_INVALID")
        return match.group(1)

    def _compose_up(self) -> None:
        self._run(
            self._compose_prefix() + ("up", "-d", "--build"),
            1800.0,
            "COMPOSE_REBUILD_FAILED",
        )

    def _session_bundles_ready(self) -> bool:
        return self._session_bundle_statuses() is not None

    def _session_bundle_statuses(self) -> dict[str, datetime] | None:
        result = self._run(
            self._compose_prefix()
            + (
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                SESSION_READINESS_PROBE,
            ),
            30.0,
            "SESSION_STATUS_UNAVAILABLE",
        )
        try:
            document = json.loads(self._stdout_text(result))
        except ValueError:
            raise DeploymentError("SESSION_STATUS_UNAVAILABLE") from None
        if (
            not isinstance(document, dict)
            or set(document) != {"ready", "updated_at"}
            or not isinstance(document.get("ready"), bool)
            or not isinstance(document.get("updated_at"), dict)
        ):
            raise DeploymentError("SESSION_STATUS_UNAVAILABLE")
        if document["ready"] is False:
            return None
        parsed: dict[str, datetime] = {}
        if set(document["updated_at"]) != set(FIXED_ROLES):
            raise DeploymentError("SESSION_STATUS_UNAVAILABLE")
        for role, raw in document["updated_at"].items():
            if not isinstance(raw, str):
                raise DeploymentError("SESSION_STATUS_UNAVAILABLE")
            try:
                timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                raise DeploymentError("SESSION_STATUS_UNAVAILABLE") from None
            if timestamp.tzinfo is None:
                raise DeploymentError("SESSION_STATUS_UNAVAILABLE")
            parsed[role] = timestamp
        return parsed

    def _verify_data_only_acceptance(self) -> None:
        maintenance = self._deployment_maintenance
        acceptance = self._data_only_acceptance or LoopbackDataOnlyAcceptance(
            lease_owner_token=(
                maintenance.owner_token if maintenance is not None else None
            )
        )
        try:
            acceptance.verify()
        except DeploymentError as error:
            if error.error_code == "DATA_ONLY_ACCEPTANCE_FAILED":
                raise
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED") from None
        except Exception:
            raise DeploymentError("DATA_ONLY_ACCEPTANCE_FAILED") from None

    def _wait_for_compose_health(self) -> None:
        deadline = time.monotonic() + self._health_timeout_seconds
        command = self._compose_prefix() + (
            "ps",
            "--format",
            "json",
            "api",
            "redis",
        )
        while True:
            try:
                result = self._runner.run(command, 30.0)
            except Exception:
                result = _failed_process(command)
            if result.returncode == 0 and self._compose_services_healthy(
                self._stdout_text(result)
            ):
                return
            if time.monotonic() >= deadline:
                raise DeploymentError("COMPOSE_HEALTH_TIMEOUT")
            time.sleep(max(0.0, self._poll_interval_seconds))

    @staticmethod
    def _compose_services_healthy(output: str) -> bool:
        try:
            document = json.loads(output)
            items = document if isinstance(document, list) else [document]
        except ValueError:
            items = []
            for line in output.splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    return False
                items.append(item)
        if not all(isinstance(item, dict) for item in items):
            return False
        services = {item.get("Service"): item for item in items}
        return all(
            services.get(name, {}).get("State") == "running"
            and services.get(name, {}).get("Health") == "healthy"
            for name in ("api", "redis")
        )

    def _run(
        self,
        args: tuple[str, ...],
        timeout: float,
        error_code: str,
        *,
        input_data: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            result = self._runner.run(args, timeout, input_data)
        except Exception:
            raise DeploymentError(error_code) from None
        if result.returncode != 0:
            raise DeploymentError(error_code)
        return result

    @staticmethod
    def _stdout_text(result: subprocess.CompletedProcess[bytes]) -> str:
        try:
            return result.stdout.decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            raise DeploymentError("DEPLOYMENT_OUTPUT_INVALID") from None

    @staticmethod
    def _stderr_text(result: subprocess.CompletedProcess[bytes]) -> str:
        try:
            return result.stderr.decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            raise DeploymentError("DEPLOYMENT_OUTPUT_INVALID") from None


def _failed_process(args: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args, 1, b"", b"")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("auto", "existing", "provision"),
        default="auto",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--release-maintenance-lease",
        action="store_true",
        help="compare-owner release after reacquiring admin lock; queue remains paused",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    project_root = args.project_root.resolve()
    filesystem = PathFileSystem()
    runner = SubprocessCommandRunner(project_root)
    orchestrator = MacDeploymentOrchestrator(
        runner,
        None,
        filesystem,
        project_root=project_root,
        env_file=args.env_file,
    )
    maintenance = HostDeploymentMaintenance(
        runner,
        orchestrator._compose_prefix,
        LoopbackAdminMaintenanceClient(),
        SecureDeploymentOwnerState(),
        password_reader=lambda: getpass.getpass(
            "Existing administrator password: "
        ),
    )
    orchestrator._deployment_maintenance = maintenance
    try:
        if args.release_maintenance_lease:
            maintenance.rollback()
            result = DeploymentResult(
                mode="rollback",
                state="MAINTENANCE_RELEASED_QUEUE_PAUSED",
                instructions=(
                    "Release device control and explicitly resume the queue only after verifying device safety.",
                ),
            )
        else:
            result = orchestrator.deploy(args.mode)
    except DeploymentError as error:
        print(error.error_code, file=sys.stderr)
        for instruction in error.instructions:
            print(instruction, file=sys.stderr)
        return 1
    print(json.dumps(asdict(result), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
