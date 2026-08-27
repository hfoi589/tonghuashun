from __future__ import annotations

import base64
import traceback
from datetime import datetime, timezone
from pathlib import Path

from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from fastapi import HTTPException

from level2_service.api import create_app
from level2_service.app_sessions import (
    AccountSessionBundle,
    EncryptedFileSessionProvider,
)
from level2_service.parsed_values import DirectRequestError


def provider(tmp_path: Path) -> EncryptedFileSessionProvider:
    key = base64.urlsafe_b64encode(b"session-encryption-key-material!").decode("ascii")
    return EncryptedFileSessionProvider(tmp_path / "sessions", key)


def authenticated_client(app) -> TestClient:
    client = TestClient(app, base_url="https://testserver")
    assert (
        client.post("/api/admin/session", json={"password": "admin-secret"}).status_code
        == 204
    )
    return client


def test_admin_session_status_requires_authentication_and_hides_credentials(
    tmp_path: Path,
) -> None:
    sessions = provider(tmp_path)
    sessions.put(
        AccountSessionBundle(
            role="main_fund_flow",
            cookie="user=private; sess_tk=private-ticket",
            user_agent="private-user-agent",
            platform="android",
            updated_at=datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc),
        )
    )
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        account_session_provider=sessions,
    )

    anonymous = TestClient(app, base_url="https://testserver")
    assert anonymous.get("/api/admin/account-sessions").status_code == 401

    response = authenticated_client(app).get("/api/admin/account-sessions")

    assert response.status_code == 200
    assert response.json() == {
        "sessions": [
            {
                "role": "core_metrics",
                "state": "MISSING",
                "updated_at": None,
                "error_code": None,
            },
            {
                "role": "main_fund_flow",
                "state": "READY",
                "updated_at": "2026-08-26T08:00:00Z",
                "error_code": None,
            },
        ]
    }
    body = response.text
    assert "private-ticket" not in body
    assert "private-user-agent" not in body


def test_admin_refreshes_a_session_without_submitting_login_credentials(
    tmp_path: Path,
) -> None:
    sessions = provider(tmp_path)
    calls: list[str] = []

    def refresh(role: str) -> AccountSessionBundle:
        calls.append(role)
        return AccountSessionBundle(
            role=role,
            cookie="user=private; sess_tk=private-ticket",
            user_agent="private-user-agent",
            platform="android",
            updated_at=datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc),
        )

    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        account_session_provider=sessions,
        account_session_refreshers={"main_fund_flow": refresh},
    )
    client = authenticated_client(app)
    csrf = client.cookies.get("ths_csrf")

    assert (
        client.post("/api/admin/account-sessions/main_fund_flow/refresh").status_code
        == 403
    )
    response = client.post(
        "/api/admin/account-sessions/main_fund_flow/refresh",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    assert response.json() == {
        "role": "main_fund_flow",
        "state": "READY",
        "updated_at": "2026-08-26T09:00:00Z",
        "error_code": None,
    }
    assert calls == ["main_fund_flow"]
    assert sessions.get("main_fund_flow") is not None
    assert "private-ticket" not in response.text


def test_admin_session_refresh_reports_unknown_and_unavailable_roles(
    tmp_path: Path,
) -> None:
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        account_session_provider=provider(tmp_path),
    )
    client = authenticated_client(app)
    headers = {"X-CSRF-Token": client.cookies.get("ths_csrf")}

    assert (
        client.post(
            "/api/admin/account-sessions/other/refresh", headers=headers
        ).status_code
        == 404
    )
    response = client.post(
        "/api/admin/account-sessions/core_metrics/refresh",
        headers=headers,
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "account session refresh unavailable"}


def test_admin_refresh_rebuilds_http_exception_without_secret_traceback(
    tmp_path: Path,
) -> None:
    secret = "synthetic-admin-refresh-secret-marker"
    sessions = provider(tmp_path)

    def refresh(_role: str) -> AccountSessionBundle:
        raise DirectRequestError("DIRECT_REQUEST_TIMEOUT", secret)

    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        account_session_provider=sessions,
        account_session_refreshers={"core_metrics": refresh},
    )
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None)
        == "/api/admin/account-sessions/{role}/refresh"
    )

    try:
        endpoint("core_metrics", None)
    except HTTPException as caught:
        rendered = "".join(
            traceback.format_exception(type(caught), caught, caught.__traceback__)
        )
    else:
        raise AssertionError("refresh endpoint must fail")

    assert sessions.status("core_metrics").error_code == "DIRECT_REQUEST_TIMEOUT"
    assert secret not in rendered
