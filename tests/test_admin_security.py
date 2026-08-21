from fastapi.testclient import TestClient
from argon2 import PasswordHasher

from level2_service.api import create_app


def test_admin_login_is_unavailable_without_an_explicit_password_hash() -> None:
    """A fallback password would silently expose administrative device control."""
    client = TestClient(create_app())

    response = client.post("/api/admin/session", json={"password": "not-configured"})

    assert response.status_code == 503
    assert response.json() == {"detail": "admin login is not configured"}


def test_admin_session_and_csrf_protect_runner_lock_controls() -> None:
    """Accepting an unauthenticated or cross-site lock request permits device takeover."""
    password_hash = PasswordHasher().hash("correct-horse-battery-staple")
    client = TestClient(create_app(admin_password_hash=password_hash), base_url="https://testserver")

    assert client.get("/api/admin/runner").status_code == 401
    assert client.post("/api/admin/session", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/admin/session", json={"password": "correct-horse-battery-staple"}).status_code == 204
    assert client.get("/api/admin/runner").json()["state"] == "OFFLINE"

    assert client.post("/api/admin/lock/acquire").status_code == 403
    csrf = client.cookies.get("ths_csrf")
    acquired = client.post("/api/admin/lock/acquire", headers={"X-CSRF-Token": csrf})
    assert acquired.status_code == 200
    assert acquired.json() == {"locked": True}
    assert client.post("/api/admin/lock/release", headers={"X-CSRF-Token": csrf}).json() == {"locked": False}


def test_logout_requires_csrf_and_revokes_the_server_side_session() -> None:
    """Leaving a session valid after logout would preserve device-control access."""
    client = TestClient(
        create_app(admin_password_hash=PasswordHasher().hash("admin-secret")),
        base_url="https://testserver",
    )
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204

    assert client.post("/api/admin/session/logout").status_code == 403
    csrf = client.cookies.get("ths_csrf")
    assert client.post("/api/admin/session/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert client.get("/api/admin/runner").status_code == 401
