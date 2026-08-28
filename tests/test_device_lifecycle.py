from __future__ import annotations

import json
import traceback
from io import BytesIO
from socket import timeout as SocketTimeout
from urllib.error import HTTPError

import pytest

from level2_service.device_lifecycle import (
    DeviceLifecycleAction,
    DeviceLifecycleClient,
    DeviceLifecycleError,
    DeviceLifecycleState,
)


class FakeResponse:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        pass


class RecordingOpener:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.requests = []

    def __call__(self, request, timeout: float):
        self.requests.append(request)
        return FakeResponse(self.payload, status=self.status)


def operation_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "operation_id": "op-1",
        "role": "core_metrics",
        "action": "shutdown",
        "state": "STOPPING",
        "error_code": None,
        "updated_at": "2026-08-27T14:00:00Z",
    }
    payload.update(changes)
    return payload


def test_client_submits_only_fixed_role_and_action() -> None:
    """Sending a caller-selected role/action would escape the lifecycle allowlist."""
    opener = RecordingOpener(operation_payload(), status=202)
    client = DeviceLifecycleClient(
        "http://host.docker.internal:18765", "secret", opener=opener
    )

    result = client.submit("core_metrics", DeviceLifecycleAction.SHUTDOWN)

    assert result.operation_id == "op-1"
    assert json.loads(opener.requests[0].data) == {"action": "shutdown"}
    assert opener.requests[0].full_url == (
        "http://host.docker.internal:18765/v1/devices/core_metrics/actions"
    )
    assert "secret" not in repr(client)


@pytest.mark.parametrize(
    ("base_url", "token"),
    [
        ("https://host.docker.internal:18765", "secret"),
        ("http://broker.example.test:18765", "secret"),
        ("http://host.docker.internal:18765/path", "secret"),
        ("http://host.docker.internal:18765", ""),
    ],
)
def test_client_rejects_untrusted_connection_settings(
    base_url: str, token: str
) -> None:
    """Weak URL or token validation could send the host capability elsewhere."""
    with pytest.raises(ValueError):
        DeviceLifecycleClient(base_url, token)


@pytest.mark.parametrize(
    "payload",
    [
        operation_payload(state="NOT_A_STATE"),
        operation_payload(role="emulator-5556"),
        operation_payload(action="shell"),
        operation_payload(updated_at="not-a-date"),
        operation_payload(error_code="token=private"),
        {"operation_id": "op-1"},
    ],
)
def test_client_rejects_invalid_operation_payloads_with_a_fixed_error(
    payload: object,
) -> None:
    """Accepting an unchecked host payload could expose untrusted error detail."""
    client = DeviceLifecycleClient(
        "http://localhost:18765", "secret", opener=RecordingOpener(payload)
    )

    with pytest.raises(DeviceLifecycleError) as failure:
        client.operation("op-1")

    assert failure.value.error_code == "DEVICE_LIFECYCLE_FAILED"
    assert str(failure.value) == "DEVICE_LIFECYCLE_FAILED"


def test_client_parses_safe_device_statuses() -> None:
    """Dropping a lifecycle state would prevent the admin UI from disabling actions."""
    client = DeviceLifecycleClient(
        "http://127.0.0.1:18765",
        "secret",
        opener=RecordingOpener(
            {
                "devices": [
                    {"role": "core_metrics", "state": "RUNNING"},
                    {"role": "main_fund_flow", "state": "STOPPED"},
                ]
            }
        ),
    )

    statuses = client.devices()

    assert [(item.role, item.state, item.operation_id) for item in statuses] == [
        ("core_metrics", DeviceLifecycleState.RUNNING, None),
        ("main_fund_flow", DeviceLifecycleState.STOPPED, None),
    ]


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (401, "DEVICE_LIFECYCLE_UNAVAILABLE"),
        (409, "DEVICE_ACTION_IN_PROGRESS"),
        (422, "DEVICE_LIFECYCLE_FAILED"),
        (500, "DEVICE_LIFECYCLE_FAILED"),
    ],
)
def test_client_maps_http_errors_to_fixed_codes(status: int, expected_code: str) -> None:
    """Forwarding a host error response would leak implementation details to admins."""
    error = HTTPError(
        "http://localhost:18765/v1/devices",
        status,
        "token=private command=adb emu kill",
        None,
        BytesIO(b'{"detail":"token=private command=adb emu kill"}'),
    )

    def failing_opener(_request, _timeout: float):
        raise error

    client = DeviceLifecycleClient(
        "http://localhost:18765", "secret", opener=failing_opener
    )

    with pytest.raises(DeviceLifecycleError) as failure:
        client.devices()

    assert failure.value.error_code == expected_code
    assert str(failure.value) == expected_code
    assert "private" not in repr(failure.value)
    assert "command" not in repr(failure.value)


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("token=private"),
        SocketTimeout("command=adb emu kill"),
        OSError("token=private command=adb"),
    ],
)
def test_client_sanitizes_transport_exceptions(failure: Exception) -> None:
    """Transport exception text must never cross the lifecycle client boundary."""
    def failing_opener(_request, _timeout: float):
        raise failure

    client = DeviceLifecycleClient(
        "http://localhost:18765", "secret", opener=failing_opener
    )

    with pytest.raises(DeviceLifecycleError) as raised:
        client.devices()

    assert raised.value.error_code == "DEVICE_LIFECYCLE_UNAVAILABLE"
    assert str(raised.value) == "DEVICE_LIFECYCLE_UNAVAILABLE"
    assert "private" not in repr(raised.value)
    assert "command" not in repr(raised.value)


def test_client_sanitizes_malformed_json() -> None:
    """Returning malformed host output would expose decoder errors without this guard."""
    class MalformedResponse:
        status = 200

        def read(self) -> bytes:
            return b"token=private command=adb"

        def close(self) -> None:
            pass

    client = DeviceLifecycleClient(
        "http://localhost:18765", "secret", opener=lambda _request, _timeout: MalformedResponse()
    )

    with pytest.raises(DeviceLifecycleError) as failure:
        client.devices()

    assert failure.value.error_code == "DEVICE_LIFECYCLE_FAILED"
    assert "private" not in repr(failure.value)


@pytest.mark.parametrize(
    "payload",
    [
        operation_payload(state="token=private command=adb"),
        operation_payload(action="token=private command=adb"),
        operation_payload(updated_at="token=private command=adb"),
    ],
)
def test_client_discards_sensitive_context_from_invalid_broker_fields(
    payload: object,
) -> None:
    """A parser ValueError must not remain attached to the public fixed error."""
    client = DeviceLifecycleClient(
        "http://localhost:18765", "secret", opener=RecordingOpener(payload)
    )

    with pytest.raises(DeviceLifecycleError) as failure:
        client.operation("op-1")

    assert failure.value.error_code == "DEVICE_LIFECYCLE_FAILED"
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    rendered = "".join(traceback.format_exception(failure.value))
    assert "token=private" not in rendered
    assert "command=adb" not in rendered


def test_client_sanitizes_plain_json_value_errors() -> None:
    """Oversized JSON integers must not escape as decoder ValueErrors."""
    class HugeIntegerResponse:
        status = 200

        def read(self) -> bytes:
            return b'{"devices":[' + b"9" * 5_000 + b"]}"

        def close(self) -> None:
            pass

    client = DeviceLifecycleClient(
        "http://localhost:18765",
        "secret",
        opener=lambda _request, _timeout: HugeIntegerResponse(),
    )

    with pytest.raises(DeviceLifecycleError) as failure:
        client.devices()

    assert failure.value.error_code == "DEVICE_LIFECYCLE_FAILED"
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None


def test_client_sanitizes_response_cleanup_failures() -> None:
    """An exception while closing a host response must not reveal the broker detail."""
    class ResponseWithUnsafeClose(FakeResponse):
        def close(self) -> None:
            raise OSError("token=private command=adb emu kill")

    client = DeviceLifecycleClient(
        "http://localhost:18765",
        "secret",
        opener=lambda _request, _timeout: ResponseWithUnsafeClose(
            {"devices": []}
        ),
    )

    with pytest.raises(DeviceLifecycleError) as failure:
        client.devices()

    assert failure.value.error_code == "DEVICE_LIFECYCLE_UNAVAILABLE"
    assert "private" not in repr(failure.value)
