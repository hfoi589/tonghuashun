"""Safe HTTP client for the fixed macOS device lifecycle broker."""

from __future__ import annotations

import json
from math import isfinite
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Callable, Protocol
from urllib.parse import urlsplit
from urllib.request import Request

from .safe_http import SafeHttpError, SafeHttpStatusError, SafeHttpTransport


_ALLOWED_HOSTS = frozenset({"host.docker.internal", "127.0.0.1", "localhost"})
_ALLOWED_ROLES = frozenset({"core_metrics", "main_fund_flow"})
_SAFE_ERROR_CODES = frozenset(
    {
        "DEVICE_ACTION_IN_PROGRESS",
        "DEVICE_AVD_NOT_FOUND",
        "DEVICE_BOOT_TIMEOUT",
        "DEVICE_APP_LAUNCH_FAILED",
        "DEVICE_SHUTDOWN_FAILED",
        "DEVICE_LIFECYCLE_FAILED",
        "DEVICE_LIFECYCLE_UNAVAILABLE",
    }
)
_OPERATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


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


class DeviceLifecycleError(RuntimeError):
    """A broker failure represented by one safe, fixed public error code."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class _Response(Protocol):
    status: int

    def read(self) -> bytes: ...


_Opener = Callable[[Request, float], _Response]


@dataclass(frozen=True, init=False)
class DeviceLifecycleClient:
    """Narrow, token-redacting transport to the local lifecycle broker."""

    _base_url: str
    _token: str = field(repr=False)
    _timeout_seconds: float
    _opener: _Opener = field(repr=False, compare=False)
    _transport: SafeHttpTransport = field(repr=False, compare=False)

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 5.0,
        opener: _Opener | None = None,
    ) -> None:
        normalized_url = self.validate_base_url(base_url)
        if not isinstance(token, str) or not token:
            raise ValueError("device lifecycle token must not be empty")
        if (
            not isinstance(timeout_seconds, (int, float))
            or not isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("device lifecycle timeout must be positive")
        object.__setattr__(self, "_base_url", normalized_url)
        object.__setattr__(self, "_token", token)
        object.__setattr__(self, "_timeout_seconds", float(timeout_seconds))
        object.__setattr__(self, "_opener", opener or (lambda *_args: None))
        object.__setattr__(
            self,
            "_transport",
            SafeHttpTransport(
                normalized_url,
                max_body_bytes=64 * 1024,
                opener=opener,
            ),
        )

    @staticmethod
    def validate_base_url(base_url: str) -> str:
        if not isinstance(base_url, str) or not base_url:
            raise ValueError("device lifecycle URL must be an allowed HTTP URL")
        try:
            parsed = urlsplit(base_url)
            port = parsed.port
        except ValueError:
            raise ValueError(
                "device lifecycle URL must be an allowed HTTP URL"
            ) from None
        if (
            parsed.scheme != "http"
            or parsed.hostname not in _ALLOWED_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("device lifecycle URL must be an allowed HTTP URL")
        host = parsed.hostname
        assert host is not None
        authority = host if port is None else f"{host}:{port}"
        return f"http://{authority}"

    def devices(self) -> tuple[DeviceLifecycleStatus, ...]:
        document = self._request("GET", "/v1/devices", expected_status=200)
        if set(document) != {"devices"} or not isinstance(document["devices"], list):
            self._invalid_response()
        return tuple(self._parse_status(item) for item in document["devices"])

    def submit(
        self,
        role: str,
        action: DeviceLifecycleAction,
    ) -> DeviceLifecycleOperation:
        self._validate_role(role)
        try:
            safe_action = DeviceLifecycleAction(action)
        except (TypeError, ValueError):
            safe_action = None
        if safe_action is None:
            raise DeviceLifecycleError("DEVICE_LIFECYCLE_FAILED") from None
        document = self._request(
            "POST",
            f"/v1/devices/{role}/actions",
            payload={"action": safe_action.value},
            expected_status=202,
        )
        return self._parse_operation(document)

    def operation(self, operation_id: str) -> DeviceLifecycleOperation:
        self._validate_operation_id(operation_id)
        document = self._request(
            "GET",
            f"/v1/operations/{operation_id}",
            expected_status=200,
        )
        return self._parse_operation(document)

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, str] | None = None,
        expected_status: int,
    ) -> dict[str, object]:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload else None
        headers = {"Authorization": f"Bearer {self._token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            response = self._transport.request(request, self._timeout_seconds)
            if response.status != expected_status:
                raise DeviceLifecycleError(
                    self._status_error_code(response.status)
                )
            raw_body = response.body
        except DeviceLifecycleError:
            raise
        except SafeHttpStatusError as error:
            raise DeviceLifecycleError(self._status_error_code(error.status)) from None
        except SafeHttpError:
            raise DeviceLifecycleError("DEVICE_LIFECYCLE_UNAVAILABLE") from None
        except Exception:
            raise DeviceLifecycleError("DEVICE_LIFECYCLE_UNAVAILABLE") from None
        try:
            document = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            document = None
        if document is None:
            self._invalid_response()
        if not isinstance(document, dict):
            self._invalid_response()
        return document

    @staticmethod
    def _status_error_code(status: int) -> str:
        if status == 401:
            return "DEVICE_LIFECYCLE_UNAVAILABLE"
        if status == 409:
            return "DEVICE_ACTION_IN_PROGRESS"
        return "DEVICE_LIFECYCLE_FAILED"

    @staticmethod
    def _validate_role(role: str) -> None:
        if role not in _ALLOWED_ROLES:
            raise DeviceLifecycleError("DEVICE_LIFECYCLE_FAILED")

    @staticmethod
    def _validate_operation_id(operation_id: str) -> None:
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
            raise DeviceLifecycleError("DEVICE_LIFECYCLE_FAILED")

    @classmethod
    def _parse_status(cls, value: object) -> DeviceLifecycleStatus:
        if not isinstance(value, dict):
            cls._invalid_response()
        allowed = {"role", "state", "operation_id", "error_code", "updated_at"}
        if not {"role", "state"}.issubset(value) or not set(value).issubset(allowed):
            cls._invalid_response()
        role = value["role"]
        state = value["state"]
        if not isinstance(role, str) or role not in _ALLOWED_ROLES:
            cls._invalid_response()
        parsed_state = cls._parse_state(state)
        operation_id = cls._optional_operation_id(value.get("operation_id"))
        error_code = cls._optional_error_code(value.get("error_code"))
        updated_at = cls._optional_datetime(value.get("updated_at"))
        return DeviceLifecycleStatus(
            role, parsed_state, operation_id, error_code, updated_at
        )

    @classmethod
    def _parse_operation(cls, value: object) -> DeviceLifecycleOperation:
        expected = {
            "operation_id",
            "role",
            "action",
            "state",
            "error_code",
            "updated_at",
        }
        if not isinstance(value, dict) or set(value) != expected:
            cls._invalid_response()
        operation_id = value["operation_id"]
        role = value["role"]
        action = value["action"]
        state = value["state"]
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
            cls._invalid_response()
        if not isinstance(role, str) or role not in _ALLOWED_ROLES:
            cls._invalid_response()
        parsed_action = cls._parse_action(action)
        parsed_state = cls._parse_state(state)
        error_code = cls._optional_error_code(value["error_code"])
        updated_at = cls._optional_datetime(value["updated_at"])
        if updated_at is None:
            cls._invalid_response()
        return DeviceLifecycleOperation(
            operation_id,
            role,
            parsed_action,
            parsed_state,
            error_code,
            updated_at,
        )

    @classmethod
    def _optional_operation_id(cls, value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, str) and _OPERATION_ID.fullmatch(value):
            return value
        cls._invalid_response()

    @classmethod
    def _optional_error_code(cls, value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, str) and value in _SAFE_ERROR_CODES:
            return value
        cls._invalid_response()

    @classmethod
    def _optional_datetime(cls, value: object) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, str):
            cls._invalid_response()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is None:
            cls._invalid_response()
        if parsed.tzinfo is None:
            cls._invalid_response()
        return parsed

    @classmethod
    def _parse_action(cls, value: object) -> DeviceLifecycleAction:
        try:
            parsed = DeviceLifecycleAction(value)
        except (TypeError, ValueError):
            parsed = None
        if parsed is None:
            cls._invalid_response()
        return parsed

    @classmethod
    def _parse_state(cls, value: object) -> DeviceLifecycleState:
        try:
            parsed = DeviceLifecycleState(value)
        except (TypeError, ValueError):
            parsed = None
        if parsed is None:
            cls._invalid_response()
        return parsed

    @staticmethod
    def _invalid_response() -> None:
        raise DeviceLifecycleError("DEVICE_LIFECYCLE_FAILED") from None
