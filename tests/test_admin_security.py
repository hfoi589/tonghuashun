from fastapi.testclient import TestClient
from argon2 import PasswordHasher
from datetime import timedelta
from pathlib import Path

from level2_service.api import create_app


def test_admin_login_is_unavailable_without_an_explicit_password_hash() -> None:
    """A fallback password would silently expose administrative device control."""
    client = TestClient(create_app())

    response = client.post("/api/admin/session", json={"password": "not-configured"})

    assert response.status_code == 503
    assert response.json() == {"detail": "admin login is not configured"}


def test_admin_login_failures_are_throttled_without_affecting_public_requests() -> None:
    client = TestClient(
        create_app(admin_password_hash=PasswordHasher().hash("admin-secret")),
        base_url="https://testserver",
    )

    for _ in range(5):
        assert client.post("/api/admin/session", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/admin/session", json={"password": "wrong"}).status_code == 429
    assert client.post("/api/v1/jobs", json={"symbol": "600938"}).status_code == 202


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


def test_device_lifecycle_actions_preserve_admin_and_csrf_boundaries() -> None:
    """Lifecycle availability must not be disclosed before admin authentication."""
    client = TestClient(
        create_app(admin_password_hash=PasswordHasher().hash("admin-secret")),
        base_url="https://testserver",
    )
    path = "/api/admin/devices/core_metrics/actions"
    payload = {"action": "shutdown"}

    assert client.post(path, json=payload).status_code == 401
    assert client.post(
        "/api/admin/session", json={"password": "admin-secret"}
    ).status_code == 204
    assert client.post(path, json=payload).status_code == 403


def test_admin_session_probe_restores_an_existing_cookie_session() -> None:
    """A page refresh must reuse the valid cookie instead of asking for the password again."""
    client = TestClient(
        create_app(admin_password_hash=PasswordHasher().hash("admin-secret")),
        base_url="https://testserver",
    )

    assert client.get("/api/admin/session").status_code == 401
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    assert client.get("/api/admin/session").status_code == 204


def test_admin_cookies_are_secure_by_default() -> None:
    client = TestClient(
        create_app(admin_password_hash=PasswordHasher().hash("admin-secret")),
        base_url="https://testserver",
    )

    response = client.post("/api/admin/session", json={"password": "admin-secret"})

    assert response.status_code == 204
    assert len(response.headers.get_list("set-cookie")) == 2
    assert all("; Secure" in value for value in response.headers.get_list("set-cookie"))


def test_admin_http_cookie_setting_keeps_session_across_page_refresh() -> None:
    client = TestClient(
        create_app(
            admin_password_hash=PasswordHasher().hash("admin-secret"),
            secure_admin_cookies=False,
        ),
        base_url="http://testserver",
    )

    login = client.post("/api/admin/session", json={"password": "admin-secret"})

    assert login.status_code == 204
    assert len(login.headers.get_list("set-cookie")) == 2
    assert all("; Secure" not in value for value in login.headers.get_list("set-cookie"))
    assert client.get("/api/admin/session").status_code == 204

    logout = client.post(
        "/api/admin/session/logout",
        headers={"X-CSRF-Token": client.cookies.get("ths_csrf")},
    )
    assert logout.status_code == 204
    assert all("; Secure" not in value for value in logout.headers.get_list("set-cookie"))


def test_acquiring_device_lock_pauses_automation_until_explicit_queue_resume() -> None:
    password_hash = PasswordHasher().hash("admin-secret")
    client = TestClient(create_app(admin_password_hash=password_hash), base_url="https://testserver")
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    csrf = client.cookies.get("ths_csrf")

    assert client.post("/api/admin/lock/acquire", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/admin/queue").json() == {"paused": True}
    assert client.post("/api/admin/queue/resume", headers={"X-CSRF-Token": csrf}).status_code == 409
    assert client.post("/api/admin/lock/release", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/admin/queue").json() == {"paused": True}
    assert client.post("/api/admin/queue/resume", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/admin/queue").json() == {"paused": False}


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


def test_admin_can_resume_a_waiting_job_with_csrf() -> None:
    """An administrator needs an explicit safe path to resume a cleared UI gate."""
    from level2_service.models import TaskRecord, TaskStatus
    from level2_service.queue import InMemoryStreams

    store = InMemoryStreams()
    task = TaskRecord(task_id="waiting-job", symbol="SZ.000001")
    store.enqueue(task)
    store.transition(task.task_id, TaskStatus.RUNNING)
    store.transition(task.task_id, TaskStatus.WAITING_ADMIN, error_code="WAITING_ADMIN")
    client = TestClient(create_app(store=store, admin_password_hash=PasswordHasher().hash("admin-secret")), base_url="https://testserver")
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    assert client.post("/api/admin/jobs/waiting-job/resume").status_code == 403
    response = client.post("/api/admin/jobs/waiting-job/resume", headers={"X-CSRF-Token": client.cookies.get("ths_csrf")})
    assert response.status_code == 200
    assert response.json()["status"] == "QUEUED"


def test_admin_can_retry_a_failed_job_with_csrf() -> None:
    from level2_service.models import TaskRecord, TaskStatus
    from level2_service.queue import InMemoryStreams

    store = InMemoryStreams()
    task = TaskRecord(task_id="failed-job", symbol="SZ.000001")
    store.enqueue(task)
    store.transition(task.task_id, TaskStatus.RUNNING)
    store.transition(task.task_id, TaskStatus.FAILED, error_code="DEVICE_OFFLINE")
    client = TestClient(create_app(store=store, admin_password_hash=PasswordHasher().hash("admin-secret")), base_url="https://testserver")
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    csrf = client.cookies.get("ths_csrf")

    assert client.post("/api/admin/jobs/failed-job/retry").status_code == 403
    response = client.post("/api/admin/jobs/failed-job/retry", headers={"X-CSRF-Token": csrf})

    assert response.status_code == 200
    assert response.json()["status"] == "QUEUED"


def test_admin_can_change_password_and_persist_it(tmp_path: Path) -> None:
    password_file = tmp_path / "admin" / "password.hash"
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        password_persist_path=password_file,
    )
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    csrf = client.cookies.get("ths_csrf")

    changed = client.post(
        "/api/admin/password",
        headers={"X-CSRF-Token": csrf},
        json={
            "current_password": "admin-secret",
            "new_password": "new-admin-secret-123",
            "new_password_confirmation": "new-admin-secret-123",
        },
    )

    assert changed.status_code == 204
    assert password_file.read_text().startswith("$argon2id$")
    assert client.get("/api/admin/runner").status_code == 401

    new_client = TestClient(app, base_url="https://testserver")
    assert new_client.post("/api/admin/session", json={"password": "new-admin-secret-123"}).status_code == 204
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 401


def test_admin_password_change_accepts_a_short_non_empty_password() -> None:
    app = create_app(admin_password_hash=PasswordHasher().hash("admin-secret"))
    client = TestClient(app, base_url="https://testserver")

    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    csrf = client.cookies.get("ths_csrf")
    response = client.post(
        "/api/admin/password",
        headers={"X-CSRF-Token": csrf},
        json={
            "current_password": "admin-secret",
            "new_password": "x",
            "new_password_confirmation": "x",
        },
    )

    assert response.status_code == 204
    new_client = TestClient(app, base_url="https://testserver")
    assert new_client.post("/api/admin/session", json={"password": "x"}).status_code == 204


def test_admin_password_change_requires_csrf_and_matching_confirmation(tmp_path: Path) -> None:
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        password_persist_path=tmp_path / "password.hash",
    )
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    payload = {
        "current_password": "admin-secret",
        "new_password": "new-admin-secret-123",
        "new_password_confirmation": "different-secret-123",
    }

    assert client.post("/api/admin/password", json=payload).status_code == 403
    assert client.post(
        "/api/admin/password",
        headers={"X-CSRF-Token": client.cookies.get("ths_csrf")},
        json=payload,
    ).status_code == 422


def test_logout_revokes_session_before_runner_disconnects_it() -> None:
    """The old stream must become unauthorised before its lock-close callback runs."""
    app = create_app(admin_password_hash=PasswordHasher().hash("admin-secret"))
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    session_id = client.cookies.get("ths_admin_session")
    observed: list[bool] = []
    original = app.state.runner_control.disconnect_session

    def checked_disconnect(value: str) -> None:
        observed.append(app.state.admin_sessions.valid_session(value) is None)
        original(value)

    app.state.runner_control.disconnect_session = checked_disconnect
    assert client.post("/api/admin/session/logout", headers={"X-CSRF-Token": client.cookies.get("ths_csrf")}).status_code == 204
    assert observed == [True]
    assert app.state.admin_sessions.valid_session(session_id) is None


def test_expired_admin_session_releases_an_abandoned_device_lock() -> None:
    app = create_app(admin_password_hash=PasswordHasher().hash("admin-secret"))
    app.state.admin_sessions.session_ttl = timedelta(seconds=0)
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    csrf = client.cookies.get("ths_csrf")
    session_id = client.cookies.get("ths_admin_session")
    # The session is already expired when the lock endpoint is reached, so a
    # previously-held lock is simulated directly on the shared control.
    app.state.admin_sessions.session_ttl = timedelta(hours=8)
    app.state.runner_control.lock(session_id)
    app.state.admin_sessions.session_ttl = timedelta(seconds=0)

    assert client.get("/api/admin/runner").status_code == 401
    assert app.state.runner_control.lock_state(session_id) == {"locked": False}
