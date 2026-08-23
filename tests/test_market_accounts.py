from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from level2_service.market_accounts import (
    DuplicateUserError,
    InMemoryMarketSessionStore,
    SQLiteMarketAccountStore,
)
from level2_service.parsed_values import SymbolLookup


def test_sqlite_accounts_persist_users_and_require_the_temporary_password_to_change(tmp_path) -> None:
    path = tmp_path / "market.db"
    first = SQLiteMarketAccountStore(path)

    user = first.create_user("Wilson", "temporary-123")

    assert user.username == "wilson"
    assert user.must_change_password is True
    assert first.authenticate("WILSON", "temporary-123").id == user.id
    assert first.authenticate("wilson", "wrong-password") is None

    restored = SQLiteMarketAccountStore(path)
    assert restored.get_user(user.id).username == "wilson"
    changed = restored.change_password(user.id, "temporary-123", "permanent-456")
    assert changed.must_change_password is False
    assert restored.authenticate("wilson", "temporary-123") is None
    assert restored.authenticate("wilson", "permanent-456").id == user.id


def test_sqlite_accounts_reject_duplicate_usernames_case_insensitively(tmp_path) -> None:
    store = SQLiteMarketAccountStore(tmp_path / "market.db")
    store.create_user("Trader", "temporary-123")

    with pytest.raises(DuplicateUserError):
        store.create_user("trader", "another-123")


def test_watchlists_are_grouped_ordered_and_isolated_by_user(tmp_path) -> None:
    store = SQLiteMarketAccountStore(tmp_path / "market.db")
    first = store.create_user("first", "temporary-123")
    second = store.create_user("second", "temporary-123")
    default_group = store.list_watchlists(first.id)[0]
    growth = store.create_group(first.id, "成长")
    lookup = SymbolLookup(symbol="300750", name="宁德时代", market="33")

    store.add_symbol(first.id, growth.id, lookup)

    assert [group.name for group in store.list_watchlists(first.id)] == ["自选", "成长"]
    first_watchlists = store.list_watchlists(first.id)
    assert len(first_watchlists[0].items) == 1
    assert first_watchlists[0].items[0].symbol == "300750"
    assert first_watchlists[0].items[0].name == "宁德时代"
    assert first_watchlists[1].items[0].symbol == "300750"
    assert first_watchlists[1].items[0].name == "宁德时代"
    assert store.list_watchlists(second.id)[0].items == ()
    with pytest.raises(LookupError):
        store.add_symbol(second.id, growth.id, lookup)
    with pytest.raises(LookupError):
        store.add_symbol(first.id, default_group.id + 1000, lookup)


def test_watchlist_reordering_and_moving_symbols_is_atomic(tmp_path) -> None:
    store = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = store.create_user("trader", "temporary-123")
    first, second = store.list_watchlists(user.id)[0], store.create_group(user.id, "观察")
    a = SymbolLookup(symbol="600938", name="中国海油", market="17")
    b = SymbolLookup(symbol="601872", name="招商轮船", market="17")
    store.add_symbol(user.id, first.id, a)
    store.add_symbol(user.id, first.id, b)

    store.reorder_symbols(user.id, first.id, ["601872", "600938"])
    store.move_symbol(user.id, first.id, second.id, "600938", 0)

    watchlists = store.list_watchlists(user.id)
    assert [item.symbol for item in watchlists[0].items] == ["601872"]
    assert [item.symbol for item in watchlists[1].items] == ["600938"]


def test_moving_a_symbol_inside_one_group_reorders_without_duplicating_it(tmp_path) -> None:
    store = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = store.create_user("trader", "temporary-123")
    group = store.list_watchlists(user.id)[0]
    for symbol, name in (("600938", "中国海油"), ("601872", "招商轮船")):
        store.add_symbol(user.id, group.id, SymbolLookup(symbol=symbol, name=name, market="17"))

    store.move_symbol(user.id, group.id, group.id, "601872", 0)

    assert [item.symbol for item in store.list_watchlists(user.id)[0].items] == ["601872", "600938"]


def test_custom_group_add_uses_immutable_primary_group_after_rename_and_reorder(tmp_path) -> None:
    store = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = store.create_user("trader", "temporary-123")
    primary = store.list_watchlists(user.id)[0]
    custom = store.create_group(user.id, "航运")
    renamed_primary = store.rename_group(user.id, primary.id, "核心自选")
    store.reorder_groups(user.id, [custom.id, renamed_primary.id])

    store.add_symbol(
        user.id,
        custom.id,
        SymbolLookup(symbol="601872", name="招商轮船", market="17"),
    )

    groups = {group.id: group for group in store.list_watchlists(user.id)}
    assert groups[primary.id].is_primary is True
    assert groups[custom.id].is_primary is False
    assert [item.symbol for item in groups[primary.id].items] == ["601872"]
    assert [item.symbol for item in groups[custom.id].items] == ["601872"]


def test_custom_group_add_refreshes_stale_primary_name_and_market(tmp_path) -> None:
    store = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = store.create_user("trader", "temporary-123")
    primary = store.list_watchlists(user.id)[0]
    custom = store.create_group(user.id, "航运")
    store.add_symbol(
        user.id,
        primary.id,
        SymbolLookup(symbol="601872", name="旧名称", market="99"),
    )

    store.add_symbol(
        user.id,
        custom.id,
        SymbolLookup(symbol="601872", name="招商轮船", market="17"),
    )

    groups = {group.id: group for group in store.list_watchlists(user.id)}
    assert groups[primary.id].items[0] == groups[custom.id].items[0]
    assert groups[primary.id].items[0].name == "招商轮船"
    assert groups[primary.id].items[0].market == "17"


def test_existing_market_database_migrates_an_immutable_primary_group(tmp_path) -> None:
    path = tmp_path / "market.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE market_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE watchlist_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES market_users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            UNIQUE(user_id, name)
        );
        CREATE TABLE watchlist_items (
            group_id INTEGER NOT NULL REFERENCES watchlist_groups(id) ON DELETE CASCADE,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            market TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            PRIMARY KEY(group_id, symbol)
        );
        INSERT INTO market_users(id,username,password_hash,created_at)
        VALUES(1,'trader','unused','2026-08-24T00:00:00+00:00');
        INSERT INTO watchlist_groups(id,user_id,name,sort_order) VALUES(1,1,'自选',1);
        INSERT INTO watchlist_groups(id,user_id,name,sort_order) VALUES(2,1,'航运',0);
        INSERT INTO watchlist_items(group_id,symbol,name,market,sort_order)
        VALUES(2,'601872','招商轮船','17',0);
        """
    )
    connection.close()

    store = SQLiteMarketAccountStore(path)
    groups = {group.id: group for group in store.list_watchlists(1)}

    assert groups[1].is_primary is True
    assert groups[2].is_primary is False
    assert groups[1].items == groups[2].items


def test_primary_group_duplication_does_not_consume_the_symbol_limit_twice(tmp_path) -> None:
    store = SQLiteMarketAccountStore(tmp_path / "market.db", max_symbols_per_user=1)
    user = store.create_user("trader", "temporary-123")
    first_custom = store.create_group(user.id, "航运")
    second_custom = store.create_group(user.id, "观察")
    store.add_symbol(
        user.id,
        first_custom.id,
        SymbolLookup(symbol="601872", name="招商轮船", market="17"),
    )

    with pytest.raises(ValueError, match="watchlist symbol limit reached"):
        store.add_symbol(
            user.id,
            second_custom.id,
            SymbolLookup(symbol="300750", name="宁德时代", market="33"),
        )

    groups = store.list_watchlists(user.id)
    assert {item.symbol for group in groups for item in group.items} == {"601872"}


def test_primary_watchlist_group_cannot_be_deleted(tmp_path) -> None:
    store = SQLiteMarketAccountStore(tmp_path / "market.db")
    user = store.create_user("trader", "temporary-123")
    primary = store.list_watchlists(user.id)[0]
    store.create_group(user.id, "航运")

    with pytest.raises(ValueError, match="primary watchlist group cannot be deleted"):
        store.delete_group(user.id, primary.id)

    assert next(group for group in store.list_watchlists(user.id) if group.id == primary.id).is_primary is True


def test_market_sessions_expire_and_can_be_revoked_per_user() -> None:
    now = datetime(2026, 8, 23, tzinfo=timezone.utc)
    sessions = InMemoryMarketSessionStore(
        ttl=timedelta(days=7),
        now=lambda: now,
    )
    first = sessions.create(7)
    second = sessions.create(7)

    assert sessions.get(first.session_id).user_id == 7
    sessions.revoke_user(7)
    assert sessions.get(first.session_id) is None
    assert sessions.get(second.session_id) is None
