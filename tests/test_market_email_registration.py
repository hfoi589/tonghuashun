import re

from argon2 import PasswordHasher
from fastapi.testclient import TestClient
import pytest

from level2_service.api import create_app
from level2_service.market_accounts import (
    InMemoryMarketSessionStore,
    MonitorLimitError,
    SQLiteMarketAccountStore,
)
from level2_service.market_mail import MailError, SmtpSettings
from level2_service.parsed_values import SymbolLookup


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail = False

    def send(self, config, to, subject, body) -> None:
        if self.fail:
            raise MailError("SMTP 发送失败")
        self.sent.append((to, subject, body))

    def send_code(self, config, to, purpose, code) -> None:
        if self.fail:
            raise MailError("SMTP 发送失败")
        self.sent.append((to, purpose, code))


def _client(tmp_path, *, configured: bool = True, enabled: bool = True):
    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        secure_admin_cookies=False,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        symbol_lookup=lambda symbol: SymbolLookup(symbol=symbol, name="测试", market="17"),
    )
    mailer = FakeMailer()
    app.state.mail_sender = mailer
    if configured:
        SmtpSettings(accounts).save(
            {
                "host": "smtp.example.com",
                "user": "robot@example.com",
                "password": "secret-pass",
                "sender": "robot@example.com",
                "registration_enabled": enabled,
            }
        )
    return TestClient(app), accounts, mailer


def test_register_flow_requires_correct_code_then_sets_password(tmp_path) -> None:
    client, accounts, mailer = _client(tmp_path)
    body = {"username": "newuser", "email": "New@Example.com"}
    assert client.post("/api/v1/register/code", json=body).status_code == 202
    assert mailer.sent[-1][:2] == ("new@example.com", "register")
    code = mailer.sent[-1][2]
    assert re.fullmatch(r"\d{6}", code)

    wrong = "000000" if code != "000000" else "111111"
    bad = client.post("/api/v1/register/verify", json={"email": "new@example.com", "code": wrong})
    assert bad.status_code == 400
    # No ticket, so the final step cannot be reached.
    assert client.post(
        "/api/v1/register",
        json={"ticket": "nope", "password": "longenough1", "password_confirmation": "longenough1"},
    ).status_code == 400
    assert accounts.find_user_by_email("new@example.com") is None

    good = client.post("/api/v1/register/verify", json={"email": "new@example.com", "code": code})
    assert good.status_code == 200
    ticket = good.json()["ticket"]
    mismatch = client.post(
        "/api/v1/register",
        json={"ticket": ticket, "password": "longenough1", "password_confirmation": "different1"},
    )
    assert mismatch.status_code == 422
    created = client.post(
        "/api/v1/register",
        json={"ticket": ticket, "password": "longenough1", "password_confirmation": "longenough1"},
    )
    assert created.status_code == 201
    assert created.json()["username"] == "newuser"
    assert created.json()["must_change_password"] is False
    assert client.get("/api/v1/watchlists").status_code == 200
    # Ticket is single-use.
    assert client.post(
        "/api/v1/register",
        json={"ticket": ticket, "password": "longenough1", "password_confirmation": "longenough1"},
    ).status_code == 400


def test_code_is_locked_after_five_wrong_attempts(tmp_path) -> None:
    client, _accounts, mailer = _client(tmp_path)
    client.post("/api/v1/register/code", json={"username": "newuser", "email": "a@example.com"})
    code = mailer.sent[-1][2]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        assert client.post(
            "/api/v1/register/verify", json={"email": "a@example.com", "code": wrong}
        ).status_code == 400
    assert client.post(
        "/api/v1/register/verify", json={"email": "a@example.com", "code": code}
    ).status_code == 400


def test_register_rejects_disabled_duplicates_and_unconfigured_mail(tmp_path) -> None:
    client, accounts, _mailer = _client(tmp_path, enabled=False)
    body = {"username": "newuser", "email": "a@example.com"}
    assert client.post("/api/v1/register/code", json=body).status_code == 403
    SmtpSettings(accounts).save({"registration_enabled": True})
    accounts.create_registered_user("taken", "taken@example.com", "longenough1")
    assert client.post(
        "/api/v1/register/code", json={"username": "taken", "email": "b@example.com"}
    ).status_code == 409
    assert client.post(
        "/api/v1/register/code", json={"username": "other", "email": "TAKEN@example.com"}
    ).status_code == 409
    assert client.post(
        "/api/v1/register/code", json={"username": "other", "email": "not-an-email"}
    ).status_code == 422

    unconfigured, _a, _m = _client(tmp_path / "x", configured=False)
    assert unconfigured.post("/api/v1/register/code", json=body).status_code == 403


def test_code_resend_is_rate_limited_and_failed_send_is_reported(tmp_path) -> None:
    client, _accounts, mailer = _client(tmp_path)
    body = {"username": "newuser", "email": "a@example.com"}
    assert client.post("/api/v1/register/code", json=body).status_code == 202
    assert client.post("/api/v1/register/code", json=body).status_code == 429
    mailer.fail = True
    other = {"username": "second", "email": "b@example.com"}
    assert client.post("/api/v1/register/code", json=other).status_code == 502
    mailer.fail = False
    assert client.post("/api/v1/register/code", json=other).status_code == 202


def test_password_reset_flow_and_no_account_enumeration(tmp_path) -> None:
    client, accounts, mailer = _client(tmp_path)
    user = accounts.create_registered_user("trader", "t@example.com", "oldpassword1")

    unknown = client.post("/api/v1/password-reset/code", json={"email": "ghost@example.com"})
    assert unknown.status_code == 202 and unknown.json() == {"sent": True}
    assert mailer.sent == []

    assert client.post("/api/v1/password-reset/code", json={"email": "t@example.com"}).status_code == 202
    code = mailer.sent[-1][2]
    wrong = "000000" if code != "000000" else "111111"
    assert client.post(
        "/api/v1/password-reset/verify", json={"email": "t@example.com", "code": wrong}
    ).status_code == 400
    ticket = client.post(
        "/api/v1/password-reset/verify", json={"email": "t@example.com", "code": code}
    ).json()["ticket"]
    assert client.post(
        "/api/v1/password-reset/confirm",
        json={"ticket": ticket, "password": "short", "password_confirmation": "short"},
    ).status_code == 422
    assert client.post(
        "/api/v1/password-reset/confirm",
        json={"ticket": ticket, "password": "newpassword1", "password_confirmation": "newpassword1"},
    ).status_code == 204
    assert accounts.authenticate("trader", "oldpassword1") is None
    assert accounts.authenticate("trader", "newpassword1").id == user.id
    # A register ticket cannot be spent on a reset.
    assert client.post(
        "/api/v1/password-reset/confirm",
        json={"ticket": ticket, "password": "newpassword2", "password_confirmation": "newpassword2"},
    ).status_code == 400


def test_admin_mail_config_hides_password_and_sends_test(tmp_path) -> None:
    client, _accounts, mailer = _client(tmp_path, configured=False)
    assert client.get("/api/admin/mail/config").status_code == 401
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    headers = {"X-CSRF-Token": client.cookies.get("ths_csrf")}

    initial = client.get("/api/admin/mail/config").json()
    assert initial["host"] == "smtp.exmail.qq.com"
    assert initial["port"] == 465 and initial["ssl"] is True and initial["ready"] is False

    saved = client.put(
        "/api/admin/mail/config",
        headers=headers,
        json={"user": "u@example.com", "password": "topsecret", "sender": "u@example.com"},
    )
    assert saved.status_code == 200
    assert "topsecret" not in saved.text and saved.json()["password_configured"] is True
    # Blank password keeps the stored one.
    kept = client.put("/api/admin/mail/config", headers=headers, json={"password": "", "port": 587, "ssl": False})
    assert kept.json()["ready"] is True and kept.json()["port"] == 587

    assert client.put(
        "/api/admin/mail/config", headers=headers, json={"host": "bad host\r\n"}
    ).status_code == 422
    assert client.post("/api/admin/mail/test", json={"to": "x@example.com"}).status_code == 403
    assert client.post("/api/admin/mail/test", headers=headers, json={"to": "x@example.com"}).status_code == 200
    assert mailer.sent[-1][0] == "x@example.com"
    mailer.fail = True
    assert client.post("/api/admin/mail/test", headers=headers, json={"to": "x@example.com"}).status_code == 502


def test_global_symbol_limit_blocks_new_symbols_but_not_existing_ones(tmp_path) -> None:
    accounts = SQLiteMarketAccountStore(tmp_path / "m.db", max_total_symbols=3)
    first = accounts.create_user("alice", "temporary-123")
    second = accounts.create_user("bobby", "temporary-123")
    g1 = accounts.list_watchlists(first.id)[0].id
    g2 = accounts.list_watchlists(second.id)[0].id
    for code in ("600001", "600002"):
        accounts.add_symbol(first.id, g1, SymbolLookup(code, "n", "17"))
    accounts.add_symbol(second.id, g2, SymbolLookup("600003", "n", "17"))
    with pytest.raises(MonitorLimitError, match="监控数量达到上限，请联系管理员"):
        accounts.add_symbol(second.id, g2, SymbolLookup("600004", "n", "17"))
    # A symbol somebody already watches does not add to the total.
    accounts.add_symbol(second.id, g2, SymbolLookup("600001", "n", "17"))
    assert accounts.total_symbol_count() == 3
    with pytest.raises(MonitorLimitError):
        accounts.merge_primary_symbols(first.id, [SymbolLookup("600009", "n", "17")])


def test_watchlist_api_reports_global_limit_message(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("level2_service.market_api.add_symbol_to_vibe", lambda *_args: None)
    client, accounts, _mailer = _client(tmp_path)
    accounts.max_total_symbols = 1
    user = accounts.create_registered_user("trader", "t@example.com", "oldpassword1")
    assert client.post("/api/v1/session", json={"username": "trader", "password": "oldpassword1"}).status_code == 200
    group = client.get("/api/v1/watchlists").json()["groups"][0]["id"]
    headers = {"X-CSRF-Token": client.cookies.get("ths_market_csrf")}
    assert client.post(f"/api/v1/watchlists/groups/{group}/symbols", headers=headers, json={"symbol": "600001"}).status_code == 201
    over = client.post(f"/api/v1/watchlists/groups/{group}/symbols", headers=headers, json={"symbol": "600002"})
    assert over.status_code == 409
    assert over.json()["detail"] == "监控数量达到上限，请联系管理员"
    del user
