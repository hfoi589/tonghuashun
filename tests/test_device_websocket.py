from fastapi.testclient import TestClient
from argon2 import PasswordHasher
from base64 import b64decode
from io import BytesIO
from PIL import Image
from threading import Event, Thread
from time import monotonic, sleep

from level2_service.api import create_app
from level2_service.runner import FakeDeviceBridge, RunnerControl


_tiny_png = BytesIO()
Image.new("RGB", (1, 1), "white").save(_tiny_png, format="PNG")
TINY_PNG = _tiny_png.getvalue()


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


def test_admin_devices_reports_both_roles_without_account_or_endpoint_details() -> None:
    core = FakeDeviceBridge(symbol="600938")
    fund = FakeDeviceBridge(symbol="600938")
    client = TestClient(
        create_app(
            admin_password_hash=PasswordHasher().hash("admin-secret"),
            device_bridges={"core_metrics": core, "main_fund_flow": fund},
        ),
        base_url="https://testserver",
    )
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204

    response = client.get("/api/admin/devices")

    assert response.status_code == 200
    assert response.json() == {
        "devices": [
            {
                "role": "core_metrics",
                "label": "八项账号",
                "adb": "ONLINE",
                "app": "ONLINE",
                "frida": "UNKNOWN",
            },
            {
                "role": "main_fund_flow",
                "label": "资金账号",
                "adb": "ONLINE",
                "app": "ONLINE",
                "frida": "UNKNOWN",
            },
        ]
    }
    serialized = response.text.lower()
    assert "serial" not in serialized
    assert "endpoint" not in serialized
    assert "login" not in serialized


def test_two_device_websockets_route_input_only_to_the_target_device() -> None:
    core = FakeDeviceBridge(symbol="600938", screenshot=b"\xff\xd8core\xff\xd9")
    fund = FakeDeviceBridge(symbol="600938", screenshot=b"\xff\xd8fund\xff\xd9")
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        device_bridges={"core_metrics": core, "main_fund_flow": fund},
    )
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    csrf = client.cookies.get("ths_csrf")
    assert client.post("/api/admin/lock/acquire", headers={"X-CSRF-Token": csrf}).status_code == 200

    with client.websocket_connect("wss://testserver/api/admin/devices/core_metrics") as core_socket, client.websocket_connect("wss://testserver/api/admin/devices/main_fund_flow") as fund_socket:
        assert core_socket.receive_json()["locked"] is True
        assert fund_socket.receive_json()["locked"] is True
        core_socket.send_json({"type": "input", "sequence": 1, "event": {"kind": "tap", "x": 0.1, "y": 0.2}})
        fund_socket.send_json({"type": "input", "sequence": 1, "event": {"kind": "swipe", "startX": 0.1, "startY": 0.8, "endX": 0.1, "endY": 0.2}})
        core_socket.receive_json()
        fund_socket.receive_json()

    assert core.inputs == [("tap", 0.1, 0.2)]
    assert fund.inputs == [("swipe", 0.1, 0.8, 0.1, 0.2)]


def test_legacy_device_websocket_maps_to_the_core_metrics_device() -> None:
    core = FakeDeviceBridge(symbol="600938", screenshot=b"\xff\xd8core\xff\xd9")
    fund = FakeDeviceBridge(symbol="600938", screenshot=b"\xff\xd8fund\xff\xd9")
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        device_bridges={"core_metrics": core, "main_fund_flow": fund},
    )
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    csrf = client.cookies.get("ths_csrf")
    client.post("/api/admin/lock/acquire", headers={"X-CSRF-Token": csrf})

    with client.websocket_connect("wss://testserver/api/admin/device") as socket:
        socket.receive_json()
        socket.send_json({"type": "input", "sequence": 1, "event": {"kind": "tap", "x": 0.4, "y": 0.6}})
        socket.receive_json()

    assert core.inputs == [("tap", 0.4, 0.6)]
    assert fund.inputs == []


def test_device_websocket_rejects_anonymous_connection() -> None:
    """The device stream cannot become a public screen-sharing endpoint."""
    client = TestClient(create_app(device_bridge=FakeDeviceBridge(symbol="SZ.000001")), base_url="https://testserver")
    try:
        with client.websocket_connect("wss://testserver/api/admin/device"):
            pass
    except Exception:
        return
    raise AssertionError("anonymous device stream was accepted")


def test_logout_releases_lock_and_rejects_old_websocket_input() -> None:
    """Revoked sessions cannot retain remote input after the admin logs out."""
    app = create_app(admin_password_hash=PasswordHasher().hash("admin-secret"), device_bridge=FakeDeviceBridge(symbol="SZ.000001", screenshot=b"\xff\xd8jpg\xff\xd9"))
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    csrf = client.cookies.get("ths_csrf")
    assert client.post("/api/admin/lock/acquire", headers={"X-CSRF-Token": csrf}).status_code == 200
    with client.websocket_connect("wss://testserver/api/admin/device") as old_socket:
        old_socket.receive_json()
        assert client.post("/api/admin/session/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
        old_socket.send_json({"type": "input", "sequence": 1, "event": {"kind": "tap", "x": 0.1, "y": 0.2}})
        try:
            old_socket.receive_json()
        except Exception:
            pass
        else:
            raise AssertionError("revoked WebSocket remained open")
    assert app.state.device_bridge.inputs == []
    new_admin = TestClient(app, base_url="https://testserver")
    assert new_admin.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    assert new_admin.post("/api/admin/lock/acquire", headers={"X-CSRF-Token": new_admin.cookies.get("ths_csrf")}).json() == {"locked": True}


def test_device_websocket_converts_png_to_jpeg_frame() -> None:
    """ADB's PNG screenshot is converted to the protocol's required JPEG frame."""
    client = TestClient(create_app(admin_password_hash=PasswordHasher().hash("admin-secret"), device_bridge=FakeDeviceBridge(symbol="SZ.000001", screenshot=TINY_PNG)), base_url="https://testserver")
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    assert client.post(
        "/api/admin/lock/acquire",
        headers={"X-CSRF-Token": client.cookies.get("ths_csrf")},
    ).status_code == 200
    with client.websocket_connect("wss://testserver/api/admin/device") as socket:
        socket.receive_json()
        frame = socket.receive_json()
    assert b64decode(frame["data"]).startswith(b"\xff\xd8")


def test_device_websocket_does_not_capture_before_the_admin_takes_control() -> None:
    """An observer-only admin page must not compete with an active collection job for ADB."""
    bridge = FakeDeviceBridge(symbol="SZ.000001", screenshot=b"\xff\xd8jpg\xff\xd9")
    client = TestClient(
        create_app(admin_password_hash=PasswordHasher().hash("admin-secret"), device_bridge=bridge),
        base_url="https://testserver",
    )
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204

    with client.websocket_connect("wss://testserver/api/admin/device") as socket:
        assert socket.receive_json()["locked"] is False
        sleep(0.35)

    assert bridge.capture_attempts == 0


def test_device_websocket_rejects_duplicate_or_older_input_sequences() -> None:
    """Input replay must not make a tap execute twice."""
    client = _authenticated_client()
    csrf = client.cookies.get("ths_csrf")
    assert client.post("/api/admin/lock/acquire", headers={"X-CSRF-Token": csrf}).status_code == 200
    with client.websocket_connect("wss://testserver/api/admin/device") as socket:
        socket.receive_json()
        socket.send_json({"type": "input", "sequence": 2, "event": {"kind": "tap", "x": 0.1, "y": 0.2}})
        socket.receive_json()
        socket.receive_json()
        socket.send_json({"type": "input", "sequence": 2, "event": {"kind": "tap", "x": 0.3, "y": 0.4}})
        try:
            socket.receive_json()
        except Exception:
            pass
        else:
            raise AssertionError("replayed sequence remained open")
    assert client.app.state.device_bridge.inputs == [("tap", 0.1, 0.2)]


def test_device_stream_keeps_two_fps_ticker_running_during_continuous_input() -> None:
    """Two simultaneous streams should keep total screenshot load near the old 4 FPS."""
    bridge = FakeDeviceBridge(symbol="SZ.000001", screenshot=b"\xff\xd8jpg\xff\xd9")
    client = TestClient(create_app(admin_password_hash=PasswordHasher().hash("admin-secret"), device_bridge=bridge), base_url="https://testserver")
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    assert client.post("/api/admin/lock/acquire", headers={"X-CSRF-Token": client.cookies.get("ths_csrf")}).status_code == 200
    with client.websocket_connect("wss://testserver/api/admin/device") as socket:
        socket.receive_json()
        stop = Event()

        def feed() -> None:
            sequence = 1
            while not stop.is_set():
                socket.send_json({"type": "input", "sequence": sequence, "event": {"kind": "tap", "x": 0.1, "y": 0.2}})
                sequence += 1
                sleep(0.002)

        sender = Thread(target=feed)
        sender.start()
        started = monotonic()
        frames = []
        while len(frames) < 2:
            message = socket.receive_json()
            if message.get("type") == "frame":
                frames.append(message)
        stop.set()
        sender.join(timeout=1)

    assert 0.8 <= monotonic() - started < 1.4
    assert len(frames) == 2
    assert bridge.capture_attempts == 2


def test_websocket_disconnect_releases_the_session_device_lock() -> None:
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        device_bridge=FakeDeviceBridge(symbol="SZ.000001", screenshot=b"\xff\xd8jpg\xff\xd9"),
    )
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    csrf = client.cookies.get("ths_csrf")
    assert client.post("/api/admin/lock/acquire", headers={"X-CSRF-Token": csrf}).status_code == 200
    session_id = client.cookies.get("ths_admin_session")

    with client.websocket_connect("wss://testserver/api/admin/device") as socket:
        socket.receive_json()
        socket.close()
        sleep(0.1)

    assert app.state.runner_control.lock_state(session_id) == {"locked": False}
