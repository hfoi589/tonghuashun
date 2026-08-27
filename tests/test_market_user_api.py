from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from level2_service.api import create_app
from level2_service.market_accounts import InMemoryMarketSessionStore, SQLiteMarketAccountStore
from level2_service.parsed_values import SymbolLookup


def _market_client(tmp_path):
    accounts = SQLiteMarketAccountStore(tmp_path / "market.db")
    app = create_app(
        admin_password_hash=PasswordHasher().hash("admin-secret"),
        secure_admin_cookies=False,
        market_account_store=accounts,
        market_session_store=InMemoryMarketSessionStore(),
        symbol_lookup=lambda symbol: SymbolLookup(
            symbol=symbol,
            name={"601872": "招商轮船", "300750": "宁德时代"}.get(symbol, "测试股票"),
            market="17" if symbol.startswith("6") else "33",
        ),
    )
    return TestClient(app), accounts


def _create_user(client: TestClient, username: str = "trader") -> None:
    assert client.post("/api/admin/session", json={"password": "admin-secret"}).status_code == 204
    created = client.post(
        "/api/admin/users",
        headers={"X-CSRF-Token": client.cookies.get("ths_csrf")},
        json={"username": username, "temporary_password": "temporary-123"},
    )
    assert created.status_code == 201
    client.post(
        "/api/admin/session/logout",
        headers={"X-CSRF-Token": client.cookies.get("ths_csrf")},
    )


def _login_and_change_password(client: TestClient, username: str = "trader") -> None:
    login = client.post(
        "/api/v1/session",
        json={"username": username, "password": "temporary-123"},
    )
    assert login.status_code == 200
    assert login.json()["must_change_password"] is True
    assert client.get("/api/v1/watchlists").status_code == 403
    changed = client.post(
        "/api/v1/session/password",
        headers={"X-CSRF-Token": client.cookies.get("ths_market_csrf")},
        json={
            "current_password": "temporary-123",
            "new_password": "permanent-456",
            "new_password_confirmation": "permanent-456",
        },
    )
    assert changed.status_code == 204


def test_admin_creates_users_and_user_manages_grouped_watchlists(tmp_path) -> None:
    client, _accounts = _market_client(tmp_path)
    _create_user(client)
    _login_and_change_password(client)

    session = client.get("/api/v1/session")
    assert session.status_code == 200
    assert session.json()["username"] == "trader"
    watchlists = client.get("/api/v1/watchlists")
    assert [group["name"] for group in watchlists.json()["groups"]] == ["自选"]

    group = client.post(
        "/api/v1/watchlists/groups",
        headers={"X-CSRF-Token": client.cookies.get("ths_market_csrf")},
        json={"name": "航运"},
    )
    assert group.status_code == 201
    group_id = group.json()["id"]
    added = client.post(
        f"/api/v1/watchlists/groups/{group_id}/symbols",
        headers={"X-CSRF-Token": client.cookies.get("ths_market_csrf")},
        json={"symbol": "601872"},
    )
    assert added.status_code == 201
    assert added.json() == {"symbol": "601872", "name": "招商轮船", "market": "17"}
    updated_groups = client.get("/api/v1/watchlists").json()["groups"]
    assert updated_groups[0]["items"] == [
        {"symbol": "601872", "name": "招商轮船", "market": "17"}
    ]
    assert updated_groups[1]["items"] == [
        {"symbol": "601872", "name": "招商轮船", "market": "17"}
    ]

    removed = client.delete(
        "/api/v1/watchlists/symbols/601872",
        headers={"X-CSRF-Token": client.cookies.get("ths_market_csrf")},
    )

    assert removed.status_code == 204
    synchronized_groups = client.get("/api/v1/watchlists").json()["groups"]
    assert all(group["items"] == [] for group in synchronized_groups)


def test_watchlist_reads_the_current_catalog_name(tmp_path) -> None:
    client, _accounts = _market_client(tmp_path)
    _create_user(client)
    _login_and_change_password(client)
    group_id = client.get("/api/v1/watchlists").json()["groups"][0]["id"]
    assert client.post(
        f"/api/v1/watchlists/groups/{group_id}/symbols",
        headers={"X-CSRF-Token": client.cookies.get("ths_market_csrf")},
        json={"symbol": "601872"},
    ).status_code == 201
    client.app.state.symbol_lookup = lambda symbol: SymbolLookup(
        symbol=symbol,
        name="招商轮船新名",
        market="17",
    )

    item = client.get("/api/v1/watchlists").json()["groups"][0]["items"][0]

    assert item == {
        "symbol": "601872",
        "name": "招商轮船新名",
        "market": "17",
    }


def test_market_user_write_routes_require_csrf_and_logout_revokes_session(tmp_path) -> None:
    client, _accounts = _market_client(tmp_path)
    _create_user(client)
    _login_and_change_password(client)

    assert client.post("/api/v1/watchlists/groups", json={"name": "观察"}).status_code == 403
    csrf = client.cookies.get("ths_market_csrf")
    assert client.delete("/api/v1/session", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert client.get("/api/v1/session").status_code == 401


def test_disabled_market_user_cannot_keep_using_an_existing_session(tmp_path) -> None:
    client, accounts = _market_client(tmp_path)
    _create_user(client)
    _login_and_change_password(client)
    user = accounts.list_users()[0]

    accounts.set_user_enabled(user.id, False)

    assert client.get("/api/v1/session").status_code == 401


def test_market_login_is_rate_limited_after_repeated_password_failures(tmp_path) -> None:
    client, _accounts = _market_client(tmp_path)
    _create_user(client)

    for _ in range(5):
        assert client.post(
            "/api/v1/session",
            json={"username": "trader", "password": "wrong-password"},
        ).status_code == 401

    blocked = client.post(
        "/api/v1/session",
        json={"username": "trader", "password": "temporary-123"},
    )

    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "60"


def test_password_change_revokes_preexisting_sessions_and_rotates_the_current_cookie(tmp_path) -> None:
    client, _accounts = _market_client(tmp_path)
    _create_user(client)
    login = client.post(
        "/api/v1/session",
        json={"username": "trader", "password": "temporary-123"},
    )
    assert login.status_code == 200
    old_session = client.cookies.get("ths_market_session")
    old_csrf = client.cookies.get("ths_market_csrf")
    second = TestClient(client.app)
    second.cookies.set("ths_market_session", old_session)

    changed = client.post(
        "/api/v1/session/password",
        headers={"X-CSRF-Token": old_csrf},
        json={
            "current_password": "temporary-123",
            "new_password": "permanent-456",
            "new_password_confirmation": "permanent-456",
        },
    )

    assert changed.status_code == 204
    assert client.cookies.get("ths_market_session") != old_session
    assert client.get("/api/v1/session").status_code == 200
    assert second.get("/api/v1/session").status_code == 401
