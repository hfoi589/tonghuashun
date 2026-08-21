from fastapi.testclient import TestClient
from argon2 import PasswordHasher

from level2_service.api import create_app
from level2_service.runner import FakeDeviceBridge, RunnerControl


def _authenticated_client() -> TestClient:
    client = TestClient(
        create_app(
            admin_password_hash=PasswordHasher().hash("admin-secret"),
            device_bridge=FakeDeviceBridge(symbol="SZ.000001", screenshot=b"\xff\xd8jpeg\xff\xd9"),
        ),
        base_url="https://testserver",
    )
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    return client


def test_admin_queue_controls_require_csrf_and_report_pause_state() -> None:
    """A cross-site request must not stop or resume public jobs."""
    client = _authenticated_client()
    assert client.post("/api/admin/queue/pause").status_code == 403
    csrf = client.cookies.get("ths_csrf")
    assert client.post("/api/admin/queue/pause", headers={"X-CSRF-Token": csrf}).json() == {"paused": True}
    assert client.get("/api/admin/queue").json() == {"paused": True}
    assert client.post("/api/admin/queue/resume", headers={"X-CSRF-Token": csrf}).json() == {"paused": False}


def test_device_websocket_authenticates_and_validates_typed_input() -> None:
    """Only an admin lock owner may send a normalized documented input envelope."""
    client = _authenticated_client()
    csrf = client.cookies.get("ths_csrf")
    assert client.post("/api/admin/lock/acquire", headers={"X-CSRF-Token": csrf}).status_code == 200

    with client.websocket_connect("wss://testserver/api/admin/device") as socket:
        status = socket.receive_json()
        assert status == {"type": "runner_status", "state": "ADMIN_CONTROL", "locked": True, "sequence": 1}
        socket.send_json({"type": "input", "sequence": 1, "event": {"kind": "tap", "x": 0.25, "y": 0.75}})
        frame = socket.receive_json()
        assert frame["type"] == "frame"
        assert frame["encoding"] == "jpeg"
    assert client.app.state.device_bridge.inputs == [("tap", 0.25, 0.75)]

    with client.websocket_connect("wss://testserver/api/admin/device") as socket:
        socket.receive_json()
        socket.send_json({"type": "input", "sequence": 2, "event": {"kind": "tap", "x": 2, "y": 0.75}})
        try:
            socket.receive_json()
        except Exception:
            pass
        else:
            raise AssertionError("invalid envelope remained open")


def test_device_websocket_rejects_anonymous_connection() -> None:
    """The device stream cannot become a public screen-sharing endpoint."""
    client = TestClient(create_app(device_bridge=FakeDeviceBridge(symbol="SZ.000001")), base_url="https://testserver")
    try:
        with client.websocket_connect("wss://testserver/api/admin/device"):
            pass
    except Exception:
        return
    raise AssertionError("anonymous device stream was accepted")
