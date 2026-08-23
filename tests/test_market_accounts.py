from datetime import datetime, timedelta, timezone

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
    assert store.list_watchlists(first.id)[1].items[0].symbol == "300750"
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
